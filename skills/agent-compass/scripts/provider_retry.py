"""Providers that failed a router worker, held out of dispatch until they are retried or cleared.

Contract: the Paseo bridge's watcher records a terminal sighting carrying the provider's own message verbatim when
  - `provider_retry_stuck`: it stopped a worker whose provider kept retrying one request with nothing else happening
    for its whole window (`stuckProviderRetry`; opencode 1.x `session.status` retry, pi `auto_retry_start`, omp's
    auto-retry status line), with the provider's next retry time when it gave one, or
  - `provider_turn_failed`: the worker's turn ended in error with the provider's error text (agent.lastError; opencode
    2.x fails a free model at once: "Free usage limit reached. Connect a provider or subscribe to OpenCode Go ...").
None is special-cased here. This module records each such dispatch once in <state dir>/provider-retry.json:
  - Scope: the configured System-One classifier reads the provider's message and answers `model` (this model only),
    `account` (every model of the account, e.g. a free-usage quota all free models share), `transient`, `none` (not
    the provider refusing or failing to serve, e.g. a tool or code error), `task_input` (this task's own input was
    refused, e.g. a prompt too long for the model), or `unknown`. `none` and `task_input` hold nothing, and a failed
    turn read that way is the worker's error, not the provider's (`outcome_code`).
    No code reads the message. `unknown`, or no classifier answer, holds the model and widens to the account once a
    second model of the same account is held the same way, i.e. a recent hold whose scope was also `unknown` (a
    `transient` or `model` hold never widens anything); `transient` and `model` hold only the model.
  - Every hold backs off like a failing usage reading (usage_core.failure_retry_seconds): 2 minutes, doubling with
    each consecutive hold of the same key, capped at the idle poll interval; a later `next` from the provider wins.
    Past its retry time the next roll may pick it again and so re-probes it. That includes an account hold widened
    from two `unknown` answers: guesses never hold an account for good.
  - Only an account hold the classifier itself scoped `account` has no retry time (`indefinite`): a quota that blocks
    the whole account (2026-11-09: opencode "Free usage exceeded" on a free model never used before, after a long
    idle) does not lift by waiting.
  - Lifting: the tray's clear button, `agentcompass hold --clear ACCOUNT` (every hold of it) or `ACCOUNT/MODEL` (that
    model's hold only); a probe: routing never picks a held bundle, but a model the user names explicitly
    (`roll --model`) is dispatched anyway, and a dispatch launched after the hold whose turn ended on the model's reply
    clears the account's hold and that model's (dispatch_registry.observe; a start, a stop or a cancel does not); or,
    on an account with a usage reader, a good usage reading newer than the hold, or a different credential (re-login),
    which lifts the account hold and its model holds (`lift_after_reading`, from provider_inventory.apply).
The message is stored, capped, because the account list must show it; nothing here decides from it.
"""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys

# `agentcompass hold` runs this file with `python -I`, which leaves its own folder off the path.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
import account_state
import override_store

FAILURE_CODE = 'provider_retry_stuck'
TURN_FAILED_CODE = 'provider_turn_failed'
FAILURE_CODES = (FAILURE_CODE, TURN_FAILED_CODE)
# A failed turn whose message the classifier reads as not the provider's doing.
WORKER_TURN_FAILED_CODE = 'worker_turn_failed'
TASK_INPUT_CODE = 'task_input_rejected'
# Every exclusion `reason` returns starts with this, so prepare can let an explicitly named model through as a probe.
EXCLUSION_PREFIX = 'provider failure hold'
CLEAR_COMMAND = 'agentcompass hold --clear'
FILE = 'provider-retry.json'
MESSAGE_LIMIT = 2000
SCOPES = ('model', 'account', 'transient', 'none', 'task_input', 'unknown')
NO_HOLD = ('none', 'task_input')
# A hold whose retry time passed longer ago than this starts a new streak, and no longer widens a second model's hold.
STREAK_HOURS = account_state.LATCH_RETRY_HOURS
CLASSIFIER_TIMEOUT_SECONDS = 25
SCOPE_SCHEMA = {
    'type': 'object',
    'properties': {
        'scope': {'type': 'string', 'enum': list(SCOPES)},
        'reason': {'type': 'string', 'description': 'One short sentence: what in the message decides the scope.'},
    },
    'required': ['scope', 'reason'],
}
SCOPE_INSTRUCTIONS = (
    'You are a System-One classifier. Do NOT call tools, run commands, or read files. Return ONLY structured JSON.\n'
    'A coding agent sent a request to a model provider. The request failed: either the provider kept answering with an '
    'error and retrying without getting through (`failure`: provider_retry_stuck), or the agent\'s turn ended with this '
    'error (`failure`: provider_turn_failed). Read the provider\'s own message below and say what it blocks:\n'
    '- `model`: only this model is affected; other models of the same account would still work (for example this model '
    'is overloaded, removed, or not included in the plan).\n'
    '- `account`: every model of this account or login is blocked (for example a usage quota, credit balance, spending '
    'limit or rate limit shared by the account or by all of its free models, an expired or unpaid subscription, or '
    'rejected credentials).\n'
    '- `transient`: a short fault expected to clear by itself within minutes that is not about quota or billing (a '
    'network error, a server error, a timeout).\n'
    '- `none`: the message is not the provider refusing or failing to serve the model (for example a tool, file or '
    'code error inside the agent\'s own work, or the user stopping it).\n'
    '- `task_input`: the provider refused this one request because of the task\'s own input, not because of the '
    'provider, the model or the account: another task would go through (for example the prompt or context is too long '
    'for the model, an attachment is too large or unsupported, or the request content is malformed or refused).\n'
    '- `unknown`: the message does not say which.\n'
    'Answer from the meaning of the message in any language, not from single words.'
)


def enabled():
    return os.environ.get('ROUTER_PROVIDER_RETRY', '').strip().lower() != 'off'


def state_dir():
    return Path(os.environ.get('ROUTER_STATE_DIR') or Path.home() / '.agent-compass')


def store_path():
    override = os.environ.get('ROUTER_PROVIDER_RETRY', '').strip()
    if override and override.lower() != 'off':
        return Path(override)
    return state_dir() / FILE


def _time(value):
    try:
        moment = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _read(path):
    try:
        doc = json.loads(override_store.retry_denied(lambda: Path(path).read_text(encoding='utf-8')))
    except (FileNotFoundError, ValueError):
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    entries = doc.get('entries') if isinstance(doc.get('entries'), dict) else {}
    dispatches = doc.get('dispatches') if isinstance(doc.get('dispatches'), dict) else {}
    return {'entries': entries, 'dispatches': dispatches}


def launch_model(bundle):
    return (bundle or {}).get('launch_model') or (bundle or {}).get('model')


def key(account, model=None):
    return account if model is None else f'{account}/{model}'


def validate_scope(answer):
    scope = (answer or {}).get('scope') if isinstance(answer, dict) else None
    if scope not in SCOPES:
        raise ValueError('Classifier answered no known scope.')
    reason = answer.get('reason')
    return {'scope': scope, **({'reason': reason[:300]} if isinstance(reason, str) and reason else {})}


def classify_scope(account, provider, model, message, failure=FAILURE_CODE):
    """(scope, metadata) from the configured System-One classifier; `unknown` when it cannot answer."""
    if not isinstance(message, str) or not message:
        return 'unknown', {'source': 'no_message'}
    import route_judge
    text = json.dumps({'account': account, 'provider': provider, 'model': model, 'failure': failure,
                       'provider_message': message}, ensure_ascii=False, indent=2)
    answer, meta = route_judge.classify_structured(SCOPE_INSTRUCTIONS, text, SCOPE_SCHEMA, validate_scope,
                                                   timeout=CLASSIFIER_TIMEOUT_SECONDS)
    if answer is None:
        return 'unknown', meta
    return answer['scope'], {**meta, **({'reason': answer['reason']} if answer.get('reason') else {})}


def retry_seconds(streak):
    import usage_core
    return usage_core.failure_retry_seconds(streak, usage_core.IDLE_REFRESH_SECONDS)


def _account_hold(entry):
    return isinstance(entry, dict) and entry.get('model') is None


def indefinite(entry):
    """Held until lifted, never by waiting: only an account hold the classifier itself scoped `account`."""
    return _account_hold(entry) and entry.get('scope') == 'account'


def outcome_code(code, scope):
    """The dispatch's failure code once the classifier read its message: a failed turn that is not the provider's
    doing is the worker's error."""
    if code == TURN_FAILED_CODE and scope == 'task_input':
        return TASK_INPUT_CODE
    if code == TURN_FAILED_CODE and scope == 'none':
        return WORKER_TURN_FAILED_CODE
    return code


def _receipt_time(value):
    return _time(value.get('at') if isinstance(value, dict) else value)


def dispatch_scope(dispatch_id, path=None):
    """The scope this dispatch's failure was recorded with, or None when it was not (or before scopes were kept)."""
    if not enabled():
        return None
    value = _read(path or store_path())['dispatches'].get(dispatch_id)
    return value.get('scope') if isinstance(value, dict) else None


def _recent(entry, now):
    if indefinite(entry):
        return True
    retry = _time((entry or {}).get('retry_at'))
    return retry is not None and retry > now - timedelta(hours=STREAK_HOURS)


def record(bundle, failure, *, dispatch_id, at=None, classify=None, path=None):
    """Hold the bundle's model or account for a `provider_retry_stuck` terminal sighting. Once per dispatch; returns
    the entry written, or None when nothing was (disabled, recorded before, or not this failure)."""
    failure = failure if isinstance(failure, dict) else {}
    account, model = (bundle or {}).get('account'), launch_model(bundle)
    code = failure.get('failure_code')
    if (not enabled() or code not in FAILURE_CODES or not isinstance(account, str)
            or not isinstance(model, str) or not isinstance(dispatch_id, str)):
        return None
    path = path or store_path()
    if dispatch_id in _read(path)['dispatches']:
        return None
    provider = bundle.get('provider') or account
    message = failure.get('provider_message') if isinstance(failure.get('provider_message'), str) else None
    message = message[:MESSAGE_LIMIT] if message else None
    # The classifier can take seconds: ask it before taking the lock.
    scope, meta = (classify or classify_scope)(account, provider, model, message, code)
    scope = scope if scope in SCOPES else 'unknown'
    at = _time(at) or _time(failure.get('at')) or datetime.now(timezone.utc)
    nxt = _time(failure.get('next'))
    with override_store.locked(path):
        doc = _read(path)
        if dispatch_id in doc['dispatches']:
            return None
        doc['dispatches'][dispatch_id] = {'at': at.isoformat(), 'scope': scope}
        if scope in NO_HOLD:
            # Read, and not the provider's doing: nothing to hold, and never read again.
            override_store.write(path, doc)
            return None
        entries = doc['entries']
        # Only another model held for the same unknown reason widens: a transient blip or a model-only refusal on it
        # says nothing about the account.
        others = sorted(v.get('model') for k, v in entries.items()
                        if isinstance(v, dict) and v.get('account') == account and v.get('model') not in (None, model)
                        and v.get('scope') == 'unknown' and _recent(v, at))
        widened = scope == 'unknown' and bool(others)
        held = key(account) if scope == 'account' or widened else key(account, model)
        previous = entries.get(held) if isinstance(entries.get(held), dict) else {}
        streak = int(previous.get('streak') or 0) + 1 if _recent(previous, at) else 1
        retry = None
        # Only the classifier's own `account` answer holds without a retry time; a widened guess backs off.
        if not (held == key(account) and scope == 'account'):
            retry = at + timedelta(seconds=retry_seconds(streak))
            if nxt and nxt > retry:
                retry = nxt
        entry = {'account': account, 'provider': provider, 'model': None if held == key(account) else model,
                 'failure_code': code, 'scope': scope,
                 'scope_source': 'widened' if widened else ('classifier' if scope != 'unknown' else 'unknown_default'),
                 'classifier': {k: meta.get(k) for k in ('source', 'scorer_type', 'provider', 'model', 'reason',
                                                          'fallback_reason') if meta.get(k)},
                 'message': message, 'next': nxt.isoformat() if nxt else None,
                 'attempts': failure.get('attempts'), 'retrying_since': failure.get('retrying_since'),
                 'observed_at': at.isoformat(), 'streak': streak, 'retry_at': retry.isoformat() if retry else None,
                 'source': dispatch_id}
        if held == key(account):
            entry['models'] = sorted({*(previous.get('models') or ()), *others, model})
            # The login the hold was taken under: a re-login with another credential lifts it (lift_after_reading).
            credential = account_state._latch_context(account)[0]
            if credential:
                entry['credential_key'] = credential
        entries[held] = entry
        # Old dispatch receipts only guard against recording one twice; a week is longer than any watch.
        cutoff = at - timedelta(days=7)
        doc['dispatches'] = {k: v for k, v in doc['dispatches'].items() if (_receipt_time(v) or at) >= cutoff}
        override_store.write(path, doc)
    return entry


def active(*, now=None, path=None):
    """Holds still in effect, keyed `account` or `account/model`. Reading never writes."""
    if not enabled():
        return {}
    now = now or datetime.now(timezone.utc)
    path = path or store_path()
    if not path.exists():
        return {}
    try:
        entries = _read(path)['entries']
    except PermissionError:
        return {}
    return {k: v for k, v in entries.items()
            if isinstance(v, dict) and (indefinite(v) or (_time(v.get('retry_at')) or now) > now)}


def entry_for(bundle, entries):
    """The hold that keeps this bundle out: its account's, else its model's."""
    if not entries:
        return None
    account = (bundle or {}).get('account')
    return entries.get(key(account)) or entries.get(key(account, launch_model(bundle))) \
        or entries.get(key(account, (bundle or {}).get('model')))


def retry_text(entry):
    """When the router tries the held key again; an indefinite hold waits for a lift."""
    if indefinite(entry):
        return ('held until cleared (tray clear button or `agentcompass hold --clear ACCOUNT`), a model named on it '
                'completes, or (accounts with a usage reader) a good usage reading after it or a re-login')
    return f"next retry at {(entry or {}).get('retry_at') or 'unknown'}"


def describe(entry):
    scope = 'account' if _account_hold(entry) else 'model'
    what = 'turn failed' if entry.get('failure_code') == TURN_FAILED_CODE else 'kept retrying without progress'
    return (f"{EXCLUSION_PREFIX} on this {scope}: provider {what} ({entry.get('message') or 'no message'}); "
            f"{retry_text(entry)}")


def reason(bundle, entries):
    """Exclusion text for a held bundle, else None."""
    entry = entry_for(bundle, entries)
    if not entry:
        return None
    return describe(entry) + f"; clear with `{CLEAR_COMMAND} {key(entry['account'], entry.get('model'))}`"


def warnings(entries, allowed=None):
    """One `plan`/`roll` line per hold. `allowed(account)` skips accounts this host does not use."""
    return [f"{k}: {describe(v)}; excluded from dispatch; clear with `{CLEAR_COMMAND} {k}`"
            for k, v in sorted((entries or {}).items()) if allowed is None or allowed(v.get('account'))]


def clear(account, model=None, *, include_account=False, launched_at=None, path=None):
    """Drop every hold of the account when `model` is None; else `model`'s hold only, and the account's own too with
    `include_account` (a completed probe). With `launched_at`, only holds observed before that launch: a dispatch
    that began before the hold says nothing about it."""
    path = path or store_path()
    if not enabled() or not isinstance(account, str) or not path.exists():
        return False
    launched = _time(launched_at) if launched_at is not None else None
    wanted = (None, model) if include_account else (model,)
    with override_store.locked(path):
        doc = _read(path)
        keys = [k for k, v in doc['entries'].items() if isinstance(v, dict) and v.get('account') == account
                and (model is None or v.get('model') in wanted)
                and (launched_at is None or (launched and (_time(v.get('observed_at')) or launched) < launched))]
        if not keys:
            return False
        for k in keys:
            doc['entries'].pop(k)
        override_store.write(path, doc)
    return True


def _lifts(entry, last_good, credential_key):
    if not _account_hold(entry):
        return False
    held_key = entry.get('credential_key')
    if held_key and credential_key and held_key != credential_key:
        return True
    good, observed = _time(last_good), _time(entry.get('observed_at'))
    return bool(good and observed and good > observed)


def lift_after_reading(account, *, last_good=None, credential_key=None, path=None):
    """Re-login recovery for an account with a usage reader (provider_inventory.apply): a good usage reading newer than
    the account's hold, or a credential other than the one it was taken under, lifts the account hold and its model
    holds. Compare-and-clear: a hold recorded after the read is kept. Returns whether anything was lifted."""
    path = path or store_path()
    if not enabled() or not isinstance(account, str) or not path.exists():
        return False
    try:
        entry = _read(path)['entries'].get(key(account))
    except PermissionError:
        return False
    if not _lifts(entry, last_good, credential_key):
        return False
    with override_store.locked(path):
        doc = _read(path)
        entry = doc['entries'].get(key(account))
        if not _lifts(entry, last_good, credential_key):
            return False
        doc['entries'] = {k: v for k, v in doc['entries'].items()
                          if not (isinstance(v, dict) and v.get('account') == account)}
        override_store.write(path, doc)
    return True


def ingest(*, classify=None, registry_path=None, notifications=None, path=None):
    """Record every stuck-retry sighting the bridge's watchers wrote that is not recorded yet, so a roll excludes it
    before the registry next observes that dispatch. Returns the entries written."""
    if not enabled():
        return []
    folder = Path(notifications) if notifications else state_dir() / 'paseo-notifications'
    if not folder.is_dir():
        return []
    path = path or store_path()
    seen = _read(path)['dispatches']
    import dispatch_registry
    written = []
    for file in folder.glob('*.json'):
        try:
            receipt = json.loads(file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        terminal = receipt.get('terminal') if isinstance(receipt, dict) else None
        dispatch_id = receipt.get('dispatch_id') if isinstance(receipt, dict) else None
        if not isinstance(terminal, dict) or terminal.get('failure_code') not in FAILURE_CODES or dispatch_id in seen:
            continue
        try:
            row = dispatch_registry.inspect(dispatch_id, registry_path)
        except (ValueError, OSError):
            continue
        entry = record(row.get('bundle'), terminal, dispatch_id=dispatch_id, classify=classify, path=path)
        if entry:
            written.append(entry)
    return written


def clear_key(value):
    """Clear `ACCOUNT` (every hold of it) or `ACCOUNT/MODEL` (that model's hold only) by hand."""
    account_name, _, model_name = str(value or '').partition('/')
    return clear(account_name, model_name or None) if account_name else False


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog='agentcompass hold', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--clear', metavar='ACCOUNT[/MODEL]', help='Clear one held model, or ACCOUNT for all its holds')
    args = parser.parse_args(argv)
    if args.clear and not clear_key(args.clear):
        print(f'nothing held for {args.clear}')
    print(json.dumps(_read(store_path())['entries'], ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
