"""Providers that kept retrying a turn without progress, held out of dispatch until they are retried.

Contract: the Paseo bridge's watcher (paseo_dispatch_bridge.mjs `stuckProviderRetry`) stops a router worker whose provider
keeps retrying one request with nothing else happening past its bounds, and records a terminal sighting with
`failure_code` `provider_retry_stuck`, the provider's own message verbatim and its next retry time when it gave one.
Any provider whose retries Paseo surfaces that way qualifies (opencode `session.status` retry, pi `auto_retry_start`,
omp's auto-retry status line); none is special-cased here. This module records each such dispatch once in
<state dir>/provider-retry.json:
  - Scope: the configured System-One classifier reads the provider's message and answers `model` (this model only),
    `account` (every model of the account, e.g. a free-usage quota all free models share), `transient`, or `unknown`.
    No code reads the message. `unknown`, or no classifier answer, holds the model and widens to the account once a
    second model of the same account is held the same way; `transient` and `model` hold only the model.
  - A model hold backs off like a failing usage reading (usage_core.failure_retry_seconds): 2 minutes, doubling with
    each consecutive hold of the same model, capped at the idle poll interval; a later `next` from the provider wins.
    Past its retry time the next roll may pick the model again and so re-probes it.
  - An account hold has no retry time: a quota that blocks the whole account (2026-11-09: opencode "Free usage
    exceeded" on a free model never used before, after a long idle) does not lift by waiting.
  - A dispatch on the account launched after a hold that completes clears the account's hold and that model's
    (dispatch_registry.observe). A start does not: a stuck worker is running.
  - `provider_retry.py --clear ACCOUNT[/MODEL]` clears by hand.
The message is stored, capped, because the account list must show it; nothing here decides from it.
"""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

import account_state
import override_store

FAILURE_CODE = 'provider_retry_stuck'
FILE = 'provider-retry.json'
MESSAGE_LIMIT = 2000
SCOPES = ('model', 'account', 'transient', 'unknown')
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
    'A coding agent sent a request to a model provider. The provider kept answering with an error and retrying, and '
    'the request never got through. Read the provider\'s own message below and say what it blocks:\n'
    '- `model`: only this model is affected; other models of the same account would still work (for example this model '
    'is overloaded, removed, or not included in the plan).\n'
    '- `account`: every model of this account or login is blocked (for example a usage quota, credit balance, spending '
    'limit or rate limit shared by the account or by all of its free models, an expired or unpaid subscription, or '
    'rejected credentials).\n'
    '- `transient`: a short fault expected to clear by itself within minutes that is not about quota or billing (a '
    'network error, a server error, a timeout).\n'
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


def classify_scope(account, provider, model, message):
    """(scope, metadata) from the configured System-One classifier; `unknown` when it cannot answer."""
    if not isinstance(message, str) or not message:
        return 'unknown', {'source': 'no_message'}
    import route_judge
    text = json.dumps({'account': account, 'provider': provider, 'model': model, 'provider_message': message},
                      ensure_ascii=False, indent=2)
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


def _recent(entry, now):
    if _account_hold(entry):
        return True
    retry = _time((entry or {}).get('retry_at'))
    return retry is not None and retry > now - timedelta(hours=STREAK_HOURS)


def record(bundle, failure, *, dispatch_id, at=None, classify=None, path=None):
    """Hold the bundle's model or account for a `provider_retry_stuck` terminal sighting. Once per dispatch; returns
    the entry written, or None when nothing was (disabled, recorded before, or not this failure)."""
    failure = failure if isinstance(failure, dict) else {}
    account, model = (bundle or {}).get('account'), launch_model(bundle)
    if (not enabled() or failure.get('failure_code') != FAILURE_CODE or not isinstance(account, str)
            or not isinstance(model, str) or not isinstance(dispatch_id, str)):
        return None
    path = path or store_path()
    if dispatch_id in _read(path)['dispatches']:
        return None
    provider = bundle.get('provider') or account
    message = failure.get('provider_message') if isinstance(failure.get('provider_message'), str) else None
    message = message[:MESSAGE_LIMIT] if message else None
    # The classifier can take seconds: ask it before taking the lock.
    scope, meta = (classify or classify_scope)(account, provider, model, message)
    scope = scope if scope in SCOPES else 'unknown'
    at = _time(at) or _time(failure.get('at')) or datetime.now(timezone.utc)
    nxt = _time(failure.get('next'))
    with override_store.locked(path):
        doc = _read(path)
        if dispatch_id in doc['dispatches']:
            return None
        entries = doc['entries']
        others = sorted(v.get('model') for k, v in entries.items()
                        if isinstance(v, dict) and v.get('account') == account and v.get('model') not in (None, model)
                        and _recent(v, at))
        widened = scope == 'unknown' and bool(others)
        held = key(account) if scope == 'account' or widened else key(account, model)
        previous = entries.get(held) if isinstance(entries.get(held), dict) else {}
        streak = int(previous.get('streak') or 0) + 1 if _recent(previous, at) else 1
        retry = None
        if held != key(account):
            retry = at + timedelta(seconds=retry_seconds(streak))
            if nxt and nxt > retry:
                retry = nxt
        entry = {'account': account, 'provider': provider, 'model': None if held == key(account) else model,
                 'failure_code': FAILURE_CODE, 'scope': scope,
                 'scope_source': 'widened' if widened else ('classifier' if scope != 'unknown' else 'unknown_default'),
                 'classifier': {k: meta.get(k) for k in ('source', 'scorer_type', 'provider', 'model', 'reason',
                                                          'fallback_reason') if meta.get(k)},
                 'message': message, 'next': nxt.isoformat() if nxt else None,
                 'attempts': failure.get('attempts'), 'retrying_since': failure.get('retrying_since'),
                 'observed_at': at.isoformat(), 'streak': streak, 'retry_at': retry.isoformat() if retry else None,
                 'source': dispatch_id}
        if held == key(account):
            entry['models'] = sorted({*(previous.get('models') or ()), *others, model})
        entries[held] = entry
        doc['dispatches'][dispatch_id] = at.isoformat()
        # Old dispatch receipts only guard against recording one twice; a week is longer than any watch.
        cutoff = at - timedelta(days=7)
        doc['dispatches'] = {k: v for k, v in doc['dispatches'].items() if (_time(v) or at) >= cutoff}
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
            if isinstance(v, dict) and (_account_hold(v) or (_time(v.get('retry_at')) or now) > now)}


def entry_for(bundle, entries):
    """The hold that keeps this bundle out: its account's, else its model's."""
    if not entries:
        return None
    account = (bundle or {}).get('account')
    return entries.get(key(account)) or entries.get(key(account, launch_model(bundle))) \
        or entries.get(key(account, (bundle or {}).get('model')))


def retry_text(entry):
    """When the router tries the held key again; an account hold waits for a completed dispatch or a clear."""
    if _account_hold(entry):
        return 'held until a dispatch on this account completes or it is cleared'
    return f"next retry at {(entry or {}).get('retry_at') or 'unknown'}"


def describe(entry):
    scope = 'account' if _account_hold(entry) else 'model'
    return (f"provider kept retrying without progress on this {scope} ({entry.get('message') or 'no message'}); "
            f"{retry_text(entry)}")


def reason(bundle, entries):
    """Exclusion text for a held bundle, else None."""
    entry = entry_for(bundle, entries)
    if not entry:
        return None
    return describe(entry) + f"; or clear with `provider_retry.py --clear {key(entry['account'], entry.get('model'))}`"


def warnings(entries, allowed=None):
    """One `plan`/`roll` line per hold. `allowed(account)` skips accounts this host does not use."""
    return [f"{k}: {describe(v)}; excluded from dispatch"
            for k, v in sorted((entries or {}).items()) if allowed is None or allowed(v.get('account'))]


def clear(account, model=None, *, launched_at=None, path=None):
    """Drop the account's hold and `model`'s (every hold of the account when `model` is None). With `launched_at`, only
    holds observed before that launch: a dispatch that began before the hold says nothing about it."""
    path = path or store_path()
    if not enabled() or not isinstance(account, str) or not path.exists():
        return False
    launched = _time(launched_at) if launched_at is not None else None
    with override_store.locked(path):
        doc = _read(path)
        keys = [k for k, v in doc['entries'].items() if isinstance(v, dict) and v.get('account') == account
                and (model is None or v.get('model') in (None, model))
                and (launched_at is None or (launched and (_time(v.get('observed_at')) or launched) < launched))]
        if not keys:
            return False
        for k in keys:
            doc['entries'].pop(k)
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
        if not isinstance(terminal, dict) or terminal.get('failure_code') != FAILURE_CODE or dispatch_id in seen:
            continue
        try:
            row = dispatch_registry.inspect(dispatch_id, registry_path)
        except (ValueError, OSError):
            continue
        entry = record(row.get('bundle'), terminal, dispatch_id=dispatch_id, classify=classify, path=path)
        if entry:
            written.append(entry)
    return written


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--clear', metavar='ACCOUNT[/MODEL]', help='Clear one held model, or ACCOUNT for all its holds')
    args = parser.parse_args()
    if args.clear:
        account_name, _, model_name = args.clear.partition('/')
        clear(account_name, model_name or None)
    print(json.dumps(_read(store_path())['entries'], ensure_ascii=False, indent=2))
