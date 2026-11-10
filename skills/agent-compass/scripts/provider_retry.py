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
    No code reads the message. `unknown`, or no classifier answer, holds the model and widens to its pool once a
    second model of the same pool is held the same way, i.e. a recent hold whose scope was also `unknown` (a
    `transient` or `model` hold never widens anything); `transient` and `model` hold only the model.
  - Pools: one account (one Paseo provider) can hold several independent quotas, so an `account` answer, or a widened
    guess, holds the quota pool that contains the failed model, keyed `account#pool`, and the whole account only when
    the account has a single pool. The pools come from structure, never from names (`pool_of`): models that draw a
    common quota window are one pool (allocator.bundle_pools: the windows' `models`/`models_except` scopes, the usage
    readings' own `models`, a bundle's explicit `draws`; Antigravity's Gemini and third-party windows), and where one
    provider of the account launches models under several provider prefixes of its own (`opencode/...` Zen free models
    and `opencode-go/...` Go models: OpenCode's own provider ids), each prefix is its own pool. Widening never crosses
    a pool: a guess about one quota says nothing about another. An account hold taken while the account had one pool
    becomes a hold of its failed models' pools once the account has several (`scope_account_holds`).
  - Every hold backs off like a failing usage reading (usage_core.failure_retry_seconds): 2 minutes, doubling with
    each consecutive hold of the same key, capped at the idle poll interval; a later `next` from the provider wins.
    Past its retry time the next roll may pick it again and so re-probes it. That includes an account hold widened
    from two `unknown` answers: guesses never hold an account for good.
  - Only an account or pool hold the classifier itself scoped `account` has no retry time (`indefinite`): a quota that
    blocks the whole account (2026-11-09: opencode "Free usage exceeded" on a free model never used before, after a long
    idle) does not lift by waiting.
  - Lifting: the tray's clear button (one pool at a time when pools are held), `agentcompass hold --clear ACCOUNT`
    (every hold of it), `ACCOUNT#POOL` (that pool's hold and its models') or `ACCOUNT/MODEL` (that model's hold only);
    a probe: routing never picks a held bundle, but a model the user names explicitly (`roll --model`) is dispatched
    anyway, and a dispatch launched after the hold whose turn ended on the model's reply clears the account's hold,
    its pool's and that model's (dispatch_registry.observe; a start, a stop or a cancel does not); or, on an account
    with a usage reader, a good usage reading newer than a hold, or a different credential (re-login), which lifts that
    account or pool hold and its model holds (`lift_after_reading`, from provider_inventory.apply).
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
INVALID_MODEL_CODE = 'INVALID_MODEL'
MODEL_FAILURE_CODES = ('invalid_model', 'model_not_found', 'unknown_model', 'unsupported_model')
FAILURE_CODES = (FAILURE_CODE, TURN_FAILED_CODE, INVALID_MODEL_CODE)


def is_model_failure_code(code):
    return isinstance(code, str) and code.strip().lower() in MODEL_FAILURE_CODES

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


POOL_SEPARATOR = '#'


def pool_key(account, pool_id):
    return f'{account}{POOL_SEPARATOR}{pool_id}'


def hold_pool(entry):
    """The pool an account-level hold covers, or None for a hold of the whole account (or of a model)."""
    pool = entry.get('pool') if isinstance(entry, dict) and entry.get('model') is None else None
    return pool if isinstance(pool, dict) and isinstance(pool.get('id'), str) and pool['id'] else None


def entry_key(entry):
    """The key a hold is stored and cleared under: `account`, `account#pool` or `account/model`."""
    pool = hold_pool(entry)
    return pool_key(entry['account'], pool['id']) if pool else key(entry['account'], entry.get('model'))


def _prefix(model):
    """The provider prefix of a launch model id (`opencode-go` in `opencode-go/kimi-k3`), else None."""
    head, sep, rest = str(model or '').partition('/')
    return head if sep and head and rest else None


def pool_of(bundle, accounts, observed=None):
    """The quota pool of the bundle's account that contains its model, or None when the account has a single pool.

    Structure only: the account's bundles are its models. Where one provider of the account launches models under
    more than one provider prefix of its own (OpenCode's `opencode/` and `opencode-go/`), each prefix is a bucket;
    within a bucket, models drawing a common quota window (allocator.bundle_pools, `observed` being the account's
    usage reading by window id) are one pool, and models drawing no window share one. The pool keeps its member models
    and, when windows split the bucket, each window's scope, so `in_pool` can place a model without the accounts.
    """
    account, model = (bundle or {}).get('account'), launch_model(bundle)
    specs = (accounts or {}).get('accounts') if isinstance((accounts or {}).get('accounts'), dict) else {}
    spec = specs.get(account) if isinstance(account, str) else None
    if not isinstance(spec, dict) or not isinstance(model, str):
        return None
    import allocator
    import window_scope
    members = {}
    for bid, row in (spec.get('bundles') or {}).items():
        if isinstance(row, dict) and isinstance(launch_model(row), str):
            members.setdefault(launch_model(row), {**row, 'id': bid, 'account': account})
    members.setdefault(model, dict(bundle))
    prefixes = {}
    for row in members.values():
        prefixes.setdefault(row.get('provider') or account, set()).add(_prefix(launch_model(row)))

    def bucket(row):
        provider = row.get('provider') or account
        return (provider, _prefix(launch_model(row))) if len(prefixes[provider]) > 1 else None
    observed = observed if isinstance(observed, dict) else {}
    groups = []   # (bucket, member models, windows drawn)
    for name, row in sorted(members.items()):
        own, windows = bucket(row), set(allocator.bundle_pools(row, accounts, observed))
        joined = [g for g in groups if g[0] == own and (g[2] & windows or not (g[2] or windows))]
        groups = [g for g in groups if g not in joined] + [
            (own, {name}.union(*(g[1] for g in joined)), windows.union(*(g[2] for g in joined)))]
    if len(groups) < 2:
        return None
    own, models, windows = next(g for g in groups if model in g[1])
    split = sum(1 for g in groups if g[0] == own) > 1
    parts = ([own[1] or own[0]] if own else []) + (['+'.join(sorted(windows)) or 'no-window'] if split else [])
    pool = {'id': ':'.join(parts) or 'rest', 'models': sorted(models)}
    if own:
        pool.update(provider=own[0], prefix=own[1])
    if split and windows:
        configured = spec.get('windows') if isinstance(spec.get('windows'), dict) else {}
        pool['windows'] = {wid: [scope for scope in (window_scope.scope_of(configured.get(wid)),
                                                     window_scope.scope_of(observed.get(wid))) if scope]
                           for wid in sorted(windows)}
    return pool


def in_pool(bundle, pool):
    """Does this bundle's model draw on the pool (`pool_of`)? Its member models, else the same provider prefix and a
    window of the pool it draws (explicit `draws`, else the window's scope)."""
    model = launch_model(bundle)
    if not isinstance(pool, dict) or not isinstance(model, str):
        return False
    if model in (pool.get('models') or ()):
        return True
    if 'prefix' in pool and ((bundle.get('provider') or bundle.get('account')) != pool.get('provider')
                             or _prefix(model) != pool.get('prefix')):
        return False
    windows = pool.get('windows') if isinstance(pool.get('windows'), dict) else None
    if not windows:
        return 'prefix' in pool
    draws = bundle.get('draws')
    if isinstance(draws, (list, tuple)):
        return bool(set(draws) & set(windows))
    import window_scope
    names = (bundle.get('model') or '', model)
    return any(scopes and window_scope.binds_any(names, *scopes) for scopes in windows.values())


def resolve_pool(bundle, accounts=None):
    """`pool_of` against the installed accounts and the account's cached usage reading; None (the whole account) when
    they cannot be read."""
    try:
        import allocator
        if accounts is None:
            accounts = allocator.load_accounts()
        observed = {}
        try:
            import usage_cache
            spec = (accounts.get('accounts') or {}).get(bundle.get('account')) or {}
            row = usage_cache.providers().get(bundle.get('account')) or {}
            for window in row.get('windows') or ():
                if isinstance(window, dict) and window.get('id'):
                    observed.setdefault(allocator.canonical_window_id(spec, window['id']), window)
        except Exception:   # no readable reading: the configured scopes alone
            observed = {}
        return pool_of(bundle, accounts, observed)
    except Exception:
        return None


def validate_scope(answer):
    scope = (answer or {}).get('scope') if isinstance(answer, dict) else None
    if scope not in SCOPES:
        raise ValueError('Classifier answered no known scope.')
    reason = answer.get('reason')
    return {'scope': scope, **({'reason': reason[:300]} if isinstance(reason, str) and reason else {})}


def classify_scope(account, provider, model, message, failure=FAILURE_CODE):
    """(scope, metadata) from the configured System-One classifier; `unknown` when it cannot answer."""
    if is_model_failure_code(failure):
        return 'model', {'source': 'failure_code', 'reason': f'provider rejected model with {failure}'}
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
    """Held until lifted, never by waiting: only an account or pool hold the classifier itself scoped `account`."""
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


def record(bundle, failure, *, dispatch_id, at=None, classify=None, path=None, accounts=None):
    """Hold the bundle's model, pool or account for a provider failure sighting. Once per dispatch; returns the entry
    written, or None when nothing was (disabled, recorded before, or not this failure). `accounts` places the model in
    its pool (`resolve_pool`); without it the installed accounts are read."""
    failure = failure if isinstance(failure, dict) else {}
    account, model = (bundle or {}).get('account'), launch_model(bundle)
    code = failure.get('failure_code')
    model_code = is_model_failure_code(code)
    if (not enabled() or (code not in FAILURE_CODES and not model_code) or not isinstance(account, str)
            or not isinstance(model, str) or not isinstance(dispatch_id, str)):
        return None
    path = path or store_path()
    if dispatch_id in _read(path)['dispatches']:
        return None
    provider = bundle.get('provider') or account
    message = failure.get('provider_message') if isinstance(failure.get('provider_message'), str) else None
    message = message[:MESSAGE_LIMIT] if message else None
    if model_code:
        scope, meta = 'model', {'source': 'failure_code', 'reason': f'provider rejected model with {code}'}
        if not message:
            message = f'provider no longer offers {model}'
    else:
        # The classifier can take seconds: ask it before taking the lock.
        scope, meta = (classify or classify_scope)(account, provider, model, message, code)
    scope = scope if scope in SCOPES else 'unknown'
    # Reading the accounts can take a moment too, and a scope that holds nothing needs no pool.
    pool = resolve_pool(bundle, accounts) if scope not in NO_HOLD else None
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
        # Only another model of the same pool held for the same unknown reason widens: a transient blip or a
        # model-only refusal on it says nothing about the account, and a guess about one quota nothing about another.
        others = sorted(v.get('model') for k, v in entries.items()
                        if isinstance(v, dict) and v.get('account') == account and v.get('model') not in (None, model)
                        and v.get('scope') == 'unknown' and _recent(v, at)
                        and (pool is None or _in_held_pool(v, pool)))
        widened = scope == 'unknown' and bool(others)
        wide = scope == 'account' or widened
        held = (pool_key(account, pool['id']) if pool else key(account)) if wide else key(account, model)
        previous = entries.get(held) if isinstance(entries.get(held), dict) else {}
        streak = int(previous.get('streak') or 0) + 1 if _recent(previous, at) else 1
        retry = None
        # Only the classifier's own `account` answer holds without a retry time; a widened guess backs off.
        if not (wide and scope == 'account'):
            retry = at + timedelta(seconds=retry_seconds(streak))
            if nxt and nxt > retry:
                retry = nxt
        entry = {'account': account, 'provider': provider, 'model': None if wide else model,
                 'failure_code': code, 'scope': scope,
                 'scope_source': 'failure_code' if model_code else (
                     'widened' if widened else ('classifier' if scope != 'unknown' else 'unknown_default')),
                 'classifier': {k: meta.get(k) for k in ('source', 'scorer_type', 'provider', 'model', 'reason',
                                                          'fallback_reason') if meta.get(k)},
                 'message': message, 'next': nxt.isoformat() if nxt else None,
                 'attempts': failure.get('attempts'), 'retrying_since': failure.get('retrying_since'),
                 'observed_at': at.isoformat(), 'streak': streak, 'retry_at': retry.isoformat() if retry else None,
                 'source': dispatch_id}
        if pool:
            # A pool hold covers the pool; a model hold remembers its pool, so lifting the pool lifts it too.
            entry.update({'pool': pool} if wide else {'pool_id': pool['id']})
        if wide:
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


def _account_hold_pools(entry, accounts):
    """{pool id: (pool, models)} for an account hold's recorded models once the account has several pools, else None
    (the account still has one pool, or the hold names no model)."""
    pools = {}
    for model in entry.get('models') or ():
        if not isinstance(model, str):
            continue
        pool = resolve_pool({'account': entry.get('account'), 'provider': entry.get('provider'), 'model': model,
                             'launch_model': model}, accounts)
        if pool is None:
            return None
        pools.setdefault(pool['id'], (pool, []))[1].append(model)
    return pools or None


def scope_account_holds(accounts, *, path=None):
    """An account hold taken while its account had a single pool (or before pools were kept) holds only the pools of
    the models it was recorded for once the account has several: a Zen free-usage hold on `opencode` must not keep
    blocking OpenCode Go connected after it. Rewrites such holds into pool holds (allocator.load_accounts, which knows
    the accounts), keeping their scope, retry time and login; returns the keys written. A pool hold already recorded
    for the same pool is newer and is kept."""
    if not enabled() or not isinstance(accounts, dict):
        return []
    path = path or store_path()
    if not path.exists():
        return []
    try:
        entries = _read(path)['entries']
    except PermissionError:
        return []
    plans = {k: _account_hold_pools(v, accounts) for k, v in entries.items()
             if _account_hold(v) and not hold_pool(v) and k == key(v.get('account'))}
    plans = {k: pools for k, pools in plans.items() if pools}
    if not plans:
        return []
    written = []
    with override_store.locked(path):
        doc = _read(path)
        changed = False
        for k, pools in plans.items():
            entry = doc['entries'].get(k)
            # Compare-and-swap: a hold rewritten or recorded again since the read is left for the next pass.
            if not (isinstance(entry, dict) and not hold_pool(entry)
                    and entry.get('observed_at') == entries[k].get('observed_at')):
                continue
            doc['entries'].pop(k)
            changed = True
            for pool_id, (pool, models) in sorted(pools.items()):
                held = pool_key(entry['account'], pool_id)
                if held not in doc['entries']:
                    doc['entries'][held] = {**entry, 'pool': pool, 'models': sorted(models), 'scoped_from': k}
                    written.append(held)
        if changed:
            override_store.write(path, doc)
    return written


def active(*, now=None, path=None):
    """Holds still in effect, keyed `account`, `account#pool` or `account/model`. Reading never writes."""
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


def pool_holds(entries, account):
    """The account's pool holds among `entries`, by key."""
    return {k: v for k, v in sorted((entries or {}).items())
            if isinstance(v, dict) and v.get('account') == account and hold_pool(v)}


def _model_bundle(entry):
    return {'account': entry.get('account'), 'provider': entry.get('provider'), 'model': entry.get('model'),
            'launch_model': entry.get('model')}


def _in_held_pool(entry, pool):
    """Is this hold `pool`'s own, or a model hold of a model in `pool`?"""
    if entry.get('model') is None:
        return (hold_pool(entry) or {}).get('id') == pool.get('id')
    return entry.get('pool_id') == pool.get('id') or in_pool(_model_bundle(entry), pool)


def entry_for(bundle, entries):
    """The hold that keeps this bundle out: its account's, else its pool's, else its model's."""
    if not entries:
        return None
    account = (bundle or {}).get('account')
    if entries.get(key(account)):
        return entries[key(account)]
    pooled = next((v for v in pool_holds(entries, account).values() if in_pool(bundle, hold_pool(v))), None)
    return pooled or entries.get(key(account, launch_model(bundle))) \
        or entries.get(key(account, (bundle or {}).get('model')))


def retry_text(entry):
    """When the router tries the held key again; an indefinite hold waits for a lift."""
    if indefinite(entry):
        return (f'held until cleared (tray clear button or `{CLEAR_COMMAND} {entry_key(entry)}`), a model named on '
                'it completes, or (accounts with a usage reader) a good usage reading after it or a re-login')
    return f"next retry at {(entry or {}).get('retry_at') or 'unknown'}"


def describe(entry):
    pool = hold_pool(entry)
    scope = f"quota pool {pool['id']} of the account" if pool else 'account' if _account_hold(entry) else 'model'
    what = (f"no longer offers model ({entry.get('failure_code')})"
            if is_model_failure_code(entry.get('failure_code'))
            else 'turn failed' if entry.get('failure_code') == TURN_FAILED_CODE
            else 'kept retrying without progress')
    return (f"{EXCLUSION_PREFIX} on this {scope}: provider {what} ({entry.get('message') or 'no message'}); "
            f"{retry_text(entry)}")


def reason(bundle, entries):
    """Exclusion text for a held bundle, else None."""
    entry = entry_for(bundle, entries)
    if not entry:
        return None
    return describe(entry) + f"; clear with `{CLEAR_COMMAND} {entry_key(entry)}`"


def warnings(entries, allowed=None):
    """One `plan`/`roll` line per hold. `allowed(account)` skips accounts this host does not use."""
    return [f"{k}: {describe(v)}; excluded from dispatch; clear with `{CLEAR_COMMAND} {k}`"
            for k, v in sorted((entries or {}).items()) if allowed is None or allowed(v.get('account'))]


def _pool_members(entries, account, pool_id):
    """Keys of `pool_id`'s hold and of the model holds in that pool."""
    pool = hold_pool(entries.get(pool_key(account, pool_id))) or {'id': pool_id}
    return [k for k, v in entries.items() if isinstance(v, dict) and v.get('account') == account
            and _in_held_pool(v, pool)]


def clear(account, model=None, *, include_account=False, launched_at=None, path=None, pool=None, bundle=None):
    """Drop every hold of the account when neither `model` nor `pool` is given; with `pool` (an id), that pool's hold
    and its models' holds; else `model`'s hold only, and with `include_account` (a completed probe) also the account's
    own and the pool hold that covers the model (placed by `bundle` when given). With `launched_at`, only holds
    observed before that launch: a dispatch that began before the hold says nothing about it."""
    path = path or store_path()
    if not enabled() or not isinstance(account, str) or not path.exists():
        return False
    launched = _time(launched_at) if launched_at is not None else None
    probe = {**(bundle or {}), 'account': account, 'launch_model': model}

    def wanted(v):
        if model is None or v.get('model') == model:
            return True
        held = hold_pool(v)
        return include_account and v.get('model') is None and (held is None or in_pool(probe, held))
    with override_store.locked(path):
        doc = _read(path)
        members = set(_pool_members(doc['entries'], account, pool)) if pool is not None else None
        keys = [k for k, v in doc['entries'].items() if isinstance(v, dict) and v.get('account') == account
                and (k in members if members is not None else wanted(v))
                and (launched_at is None or (launched and (_time(v.get('observed_at')) or launched) < launched))]
        if not keys:
            return False
        for k in keys:
            doc['entries'].pop(k)
        override_store.write(path, doc)
    return True


def clear_launch_hold(account, model, *, launched_at=None, path=None):
    """Lift a create-time `INVALID_MODEL` hold once a worker launched after the hold has started on that model."""
    path = path or store_path()
    if not enabled() or not isinstance(account, str) or not isinstance(model, str) or not path.exists():
        return False
    launched = _time(launched_at) if launched_at is not None else None
    target = key(account, model)
    with override_store.locked(path):
        doc = _read(path)
        entry = doc['entries'].get(target)
        if not (isinstance(entry, dict) and is_model_failure_code(entry.get('failure_code'))):
            return False
        if launched_at is not None and not (launched and (_time(entry.get('observed_at')) or launched) < launched):
            return False
        doc['entries'].pop(target, None)
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


def _lifted(entries, account, last_good, credential_key):
    """Keys a reading lifts: every hold of the account when its account hold lifts, else each lifting pool hold with
    its pool's model holds."""
    if _lifts(entries.get(key(account)), last_good, credential_key):
        return [k for k, v in entries.items() if isinstance(v, dict) and v.get('account') == account]
    return sorted({k for pool_hold in pool_holds(entries, account).values()
                   if _lifts(pool_hold, last_good, credential_key)
                   for k in _pool_members(entries, account, hold_pool(pool_hold)['id'])})


def lift_after_reading(account, *, last_good=None, credential_key=None, path=None):
    """Re-login recovery for an account with a usage reader (provider_inventory.apply): a good usage reading newer than
    the account's hold or one of its pool holds, or a credential other than the one that hold was taken under, lifts
    that hold and its model holds (the pool's, for a pool hold). Compare-and-clear: a hold recorded after the read is
    kept. Returns whether anything was lifted."""
    path = path or store_path()
    if not enabled() or not isinstance(account, str) or not path.exists():
        return False
    try:
        if not _lifted(_read(path)['entries'], account, last_good, credential_key):
            return False
    except PermissionError:
        return False
    with override_store.locked(path):
        doc = _read(path)
        lifted = _lifted(doc['entries'], account, last_good, credential_key)
        if not lifted:
            return False
        for k in lifted:
            doc['entries'].pop(k)
        override_store.write(path, doc)
    return True


def ingest(*, classify=None, registry_path=None, notifications=None, path=None, accounts=None):
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
        if accounts is None:
            try:   # read once for every sighting, to place each failed model in its pool
                import allocator
                accounts = allocator.load_accounts()
            except Exception:
                accounts = {}
        entry = record(row.get('bundle'), terminal, dispatch_id=dispatch_id, classify=classify, path=path,
                       accounts=accounts)
        if entry:
            written.append(entry)
    return written


def clear_key(value):
    """Clear `ACCOUNT` (every hold of it), `ACCOUNT#POOL` (that pool's hold and its models') or `ACCOUNT/MODEL` (that
    model's hold only) by hand."""
    head, _, model_name = str(value or '').partition('/')
    account_name, _, pool_id = head.partition(POOL_SEPARATOR)
    if not account_name:
        return False
    if pool_id:
        return clear(account_name, pool=pool_id)
    return clear(account_name, model_name or None)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog='agentcompass hold', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--clear', metavar='ACCOUNT[#POOL|/MODEL]',
                        help='Clear one held model, one held quota pool, or ACCOUNT for all its holds')
    args = parser.parse_args(argv)
    if args.clear and not clear_key(args.clear):
        print(f'nothing held for {args.clear}')
    print(json.dumps(_read(store_path())['entries'], ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
