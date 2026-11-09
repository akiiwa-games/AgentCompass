"""Deterministic worker allocation: capability, task fit, continuity with the chain's existing worker, and the
opportunity cost of the quota the work would use.

No model is involved. Every number that drives a choice is in references/accounts.json or
references/model-capabilities.json and is returned in the per-candidate breakdown, so a surprising
choice can be traced to the input that caused it.
"""

from datetime import datetime, timedelta, timezone
import json
import math
from math import erf, exp, isnan, pi, sqrt
import os
from pathlib import Path
import re
import statistics
import sys

# Also support the path-loaded allocator and python -I entrypoints.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import account_state
import fast_modes
import window_scope
import host_policy
import launch_failures
import model_latch
import provider_retry
import override_store
import antigravity_telemetry
import provider_inventory
import public_data
import accounts_override
import auto_bundles
import cwd_policy
from model_resolution import DIRECT_SOURCES, generic_model_spec, resolve_canonical_model, resolve_model as _resolve_model, split_model_effort

REFERENCES = Path(__file__).resolve().parents[1] / 'references'
WINDOW_ALIASES = {'seven_day': 'usage', 'usage-10080m': 'usage', 'usage-5h': 'five_hour', 'usage-300m': 'five_hour'}


def canonical_window_id(spec, wid):
    windows = (spec or {}).get('windows') or {}
    if wid in windows:
        return wid
    mapped = WINDOW_ALIASES.get(wid)
    return mapped if mapped in windows else wid


def capacity_override_path():
    configured = os.environ.get('ROUTER_CAPACITY')
    return Path(configured).expanduser() if configured else Path.home() / '.agent-compass' / 'capacity.json'


def overrides_path():
    configured = os.environ.get('ROUTER_OVERRIDES')
    return Path(configured).expanduser() if configured else Path.home() / '.agent-compass' / 'router-overrides.json'


def load_overrides(now, path=None, usage=None, accounts=None, prune=True):
    """Overrides the user asked for that have not yet run out (`until`) or landed their reset, written by `agentcompass override`."""
    target = path or overrides_path()
    try:
        document = json.loads(target.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return []
    rows = document.get('overrides') if isinstance(document, dict) else None
    active, landed, depleted = [], {}, {}
    for o in rows or []:
        if not (isinstance(o, dict) and o.get('account')
                and (until := parse_time(o.get('until')) or expected_reset_time(o)) is not None and until > now):
            continue
        if (usage is not None or accounts is not None) and override_reset_landed(o, usage, accounts, now):
            if o.get('id'):
                landed[o['id']] = o
            continue
        if override_depleted(o, usage, accounts):
            if o.get('id'):
                depleted[o['id']] = o
            continue
        active.append(o)
    if landed and prune:
        prune_landed_overrides(landed, target, now)
    if depleted and prune:
        retire_overrides(depleted, target)
    return active


def override_depleted(override, usage, accounts):
    """A priority whose quota is down to the safety margin (retires at 5%).

    At or below `safety_margin_pct` the allocator already refuses new work there, so the boost can only mislead; the
    rule is retired instead of waiting for its reset or `until`. Only a fresh reading counts, only weekly windows (or the
    override's own window) that bind its model, and a rule that also carries an expiry date is kept for that fact."""
    if (not isinstance(override, dict) or not (override.get('boost') or expected_reset_time(override))
            or override.get('expires_at')):
        return False
    account = override.get('account')
    spec = ((accounts or {}).get('accounts') or {}).get(account)
    row = ((usage or {}).get('accounts') or {}).get(account) or {}
    if (spec is None or row.get('status') != 'fresh' or row.get('stale') is True or row.get('available') is False
            or row.get('account', account) != account):
        return False
    scoring = accounts.get('scoring') or {}
    margin, short = scoring.get('safety_margin_pct', 5), scoring.get('short_window_hours', 24)
    model = canonical(override['model'], accounts) if override.get('model') else None
    binding = {wid: wspec for wid, wspec, _ in applicable_windows(spec, model, {})} if model else spec.get('windows', {})
    for window in row.get('windows') or ():
        if not isinstance(window, dict):
            continue
        wid = canonical_window_id(spec, window.get('id'))
        if override.get('window'):
            if wid != canonical_window_id(spec, override['window']):
                continue
        elif wid not in binding or binding[wid].get('hours', 168) < short:
            continue
        remaining = window.get('remaining_pct_before_reset')
        if remaining is None and isinstance(window.get('used_pct'), (int, float)):
            remaining = 100 - window['used_pct']
        if isinstance(remaining, (int, float)) and not isinstance(remaining, bool) and remaining <= margin:
            return True
    return False


def retire_overrides(retired, path=None):
    """Delete exactly the rows seen as retired, under the shared lock; no reset receipt, since nothing reset."""
    target = path or overrides_path()
    with override_store.locked(target):
        try:
            document = json.loads(target.read_text(encoding='utf-8-sig'))
        except FileNotFoundError:
            return
        rows = [o for o in document.get('overrides') or [] if not (isinstance(o, dict) and retired.get(o.get('id')) == o)]
        if len(rows) != len(document.get('overrides') or []):
            override_store.write(target, {**document, 'overrides': rows})


def override_reset_landed(override, usage, accounts=None, now=None):
    """Confirm observed replenishment, not passage of a predicted reset time.

    Scheduled resets also require cycle advancement. Explicit one-off replenishment
    intents accept an unchanged cycle, since providers can reset quota mid-week.
    Untimestamped legacy baselines must be explicitly repaired from a real observation.
    """
    if not isinstance(override, dict) or not override.get('stop_on_reset'):
        return False
    now = now or datetime.now(timezone.utc)
    confirmed = parse_time(override.get('reset_confirmed_at'))
    created = parse_time(override.get('created_at'))
    if confirmed is not None and created is not None and created <= confirmed <= now:
        return True  # Persist an observed reset; later stale/refilled quota cannot undo it.
    baselines = override.get('baseline_windows')
    if not isinstance(baselines, dict) or not baselines:
        return False
    now = now or datetime.now(timezone.utc)
    account = override.get('account')
    spec = ((accounts or {}).get('accounts') or {}).get(account) or {}
    row = ((usage or {}).get('accounts') or {}).get(account) or {}
    fetched = parse_time(row.get('fetched_at'))
    created = parse_time(override.get('created_at'))
    if (row.get('status') != 'fresh' or row.get('stale') is True or row.get('available') is False
            or fetched is None or created is None or fetched <= created
            or not 0 <= (now - fetched).total_seconds() <= 900
            or row.get('account', account) != account):
        return False
    for window in row.get('windows') or ():
        if not isinstance(window, dict):
            continue
        wid = canonical_window_id(spec, window.get('id'))
        if override.get('window') and wid != canonical_window_id(spec, override['window']):
            continue
        base = baselines.get(wid) or baselines.get(window.get('id'))
        if not isinstance(base, dict) or base.get('account', account) != account:
            continue
        baseline_time = parse_time(base.get('fetched_at'))
        if baseline_time is None or fetched <= baseline_time:
            continue
        if base.get('observation_id') and base['observation_id'] == row.get('observation_id'):
            continue
        obs_reset, base_reset = parse_time(window.get('resets_at')), parse_time(base.get('resets_at'))
        if obs_reset is None or base_reset is None or obs_reset <= now:
            continue
        cycle_advance = (obs_reset - base_reset).total_seconds()
        confirmation = override.get('reset_confirmation', 'cycle')
        if confirmation == 'replenishment':
            # An explicit temporary-reset intent may land without moving the weekly
            # reset date. Never accept an older cycle or a small clock correction.
            if cycle_advance != 0 and cycle_advance <= 120:
                continue
        elif confirmation != 'cycle' or cycle_advance <= 120:
            continue
        def used(value):
            try:
                result = value.get('used_pct')
                if result is None:
                    remaining = value.get('remaining_pct', value.get('remaining_pct_before_reset', value.get('observed_remaining_pct')))
                    result = 100 - float(remaining)
                result = float(result)
                return result if 0 <= result <= 100 else None
            except (TypeError, ValueError):
                return None
        observed_used, baseline_used = used(window), used(base)
        if observed_used is not None and baseline_used is not None and observed_used < baseline_used - 1:
            return True
    return False


def prune_landed_overrides(landed, path=None, now=None):
    """Retire confirmed overrides and retain their first-confirmation display receipt."""
    observe_landed_overrides(landed, path, now, retire=True)


def observe_landed_overrides(landed, path=None, now=None, retire=False):
    """Latch only exact observed versions under the shared lock; never slide the clock.

    The tray can retain a reset fact without retiring a rule. The next allocation
    consumes that fact and removes its boost, even if usage has since risen again.
    """
    if not landed:
        return
    target = path or overrides_path()
    with override_store.locked(target):
        try:
            document = json.loads(target.read_text(encoding='utf-8-sig'))
        except FileNotFoundError:
            return
        now = now or datetime.now(timezone.utc)
        rows, confirmations = [], []
        for o in document.get('overrides') or []:
            if not isinstance(o, dict) or landed.get(o.get('id')) != o:
                rows.append(o)
                continue
            confirmed = parse_time(o.get('reset_confirmed_at'))
            if confirmed is None or not parse_time(o['created_at']) <= confirmed <= now:
                confirmed = now
            confirmations.append(confirmed)
            if not retire:
                rows.append({**o, 'reset_confirmed_at': confirmed.isoformat()})
        if not confirmations:
            return
        last = document.get('last_reset') or {}
        previous = parse_time(last.get('at')) if isinstance(last, dict) else None
        newest = max(confirmations)
        if previous is None or previous > now or newest > previous:
            last = {'at': newest.isoformat()}
        updated = {**document, 'overrides': rows, 'last_reset': last}
        if updated != document:
            override_store.write(target, updated)


def expected_reset_time(override):
    """A reset expectation is a deadline, never evidence of replenished capacity."""
    explicit = parse_time(override.get('expected_reset_at') or override.get('resets_at'))
    if explicit is not None:
        return explicit
    if override.get('stop_on_reset') or override.get('intent') in ('tray:anticipated-reset', 'panel:boost'):
        return parse_time(override.get('until'))
    return None


def window_reset_expectation(overrides, account, model, wid, spec, accounts, now):
    """Earliest active expectation for this model and its bound long window."""
    matches = []
    for rule in overrides:
        reset = expected_reset_time(rule)
        until = parse_time(rule.get('until'))
        if (rule.get('account') != account or reset is None or reset <= now
                or (until is not None and until <= now)
                or (rule.get('model') and canonical(rule['model'], accounts) != model)):
            continue
        if rule.get('window'):
            if canonical_window_id(spec, rule['window']) != wid:
                continue
        elif spec['windows'][wid].get('hours', 168) < accounts['scoring']['short_window_hours']:
            continue
        matches.append(reset)
    return min(matches) if matches else None


def apply_overrides(overrides, usage, accounts, now):
    """Apply priority/expiry intent without replacing observed reset or capacity facts."""
    if not overrides:
        return usage, accounts, []
    active, landed, depleted = [], {}, {}
    for o in overrides:
        until = parse_time(o.get('until')) or expected_reset_time(o)
        if until is None or until <= now:
            continue
        if override_reset_landed(o, usage, accounts, now):
            if o.get('id'):
                landed[o['id']] = o
            continue
        if override_depleted(o, usage, accounts):
            if o.get('id'):
                depleted[o['id']] = o
            continue
        active.append(o)
    if landed:
        prune_landed_overrides(landed, now=now)
    if depleted:
        retire_overrides(depleted)
    if not active:
        return usage, accounts, []
    usage, accounts = json.loads(json.dumps(usage, default=str)), json.loads(json.dumps(accounts, default=str))
    short = accounts['scoring']['short_window_hours']
    applied = []
    for o in active:
        spec = accounts['accounts'].get(o['account'])
        if spec is None:
            continue
        expires = parse_time(o.get('expires_at'))
        resets = expected_reset_time(o)
        if expires is not None:
            spec['expires_at'], spec['expires_precision'] = expires.isoformat(), 'time'
        if resets is not None:
            for window in ((usage.get('accounts') or {}).get(o['account']) or {}).get('windows', []):
                wid = canonical_window_id(spec, window.get('id'))
                hours = (spec.get('windows', {}).get(wid) or {}).get('hours', 168)
                if (wid == canonical_window_id(spec, o['window'])) if o.get('window') else hours >= short:
                    window['observed_reset_at'] = window.get('resets_at')
                    window['expected_reset_at'] = resets.isoformat()
                    window['reset_expectation_status'] = 'overdue_uncertain' if resets <= now else 'unconfirmed'
        intent = {k: o[k] for k in ('id', 'intent', 'account', 'window', 'model', 'expires_at', 'boost',
                                    'until', 'stop_on_reset', 'reset_confirmation', 'basis') if o.get(k) is not None}
        if resets is not None:
            intent.update(expected_reset_at=resets.isoformat(),
                          reset_expectation_status='overdue_uncertain' if resets <= now else 'unconfirmed')
        applied.append(intent)
    return usage, accounts, applied


_AGENT_RECORD_CACHE = {}
_SESSION_LOG_CACHE = {}
_SESSION_STATE_CACHE = {}


def session_log(agent_id, home=None):
    """(provider, log path) of a Paseo agent's harness session, or None. Paseo keeps the harness session id in
    ~/.paseo/agents/<workspace>/<agent id>.json; Codex, Claude and Pi keep one log per session."""
    explicit_home = Path(home) if home else None
    home = explicit_home or Path.home()
    agents_root = home / '.paseo' / 'agents' if (home / '.paseo' / 'agents').is_dir() else home / 'agents'
    if not isinstance(agent_id, str) or any(c in agent_id for c in '*?[]/\\') or agent_id in ('.', '..'):
        return None
    rec_key = (str(agents_root), agent_id)
    record = _AGENT_RECORD_CACHE.get(rec_key)
    if record is not None and not record.is_file():
        record = None
    try:
        if record is None:
            record = next(agents_root.glob(f'*/{agent_id}.json'), None)
            if record is not None:
                _AGENT_RECORD_CACHE[rec_key] = record
        persistence = json.loads(record.read_text(encoding='utf-8')).get('persistence') or {} if record else {}
    except (OSError, ValueError):
        return None
    if not isinstance(persistence, dict):
        return None
    if persistence.get('provider') in ('antigravity', 'antigravity-resilient'):
        return antigravity_telemetry.transcript(persistence, home)
    provider, session = persistence.get('provider'), persistence.get('sessionId')
    if not provider or not session or any(c in str(session) for c in '*?[]/\\'):
        return None
    log_key = (str(home), provider, str(session))
    cached_log = _SESSION_LOG_CACHE.get(log_key)
    if cached_log is not None and cached_log[1].is_file():
        return cached_log
    if (explicit_home is None
            and not hasattr(provider_inventory.discover, '_mock_name')
            and not hasattr(provider_inventory.current, '_mock_name')):
        inventory = provider_inventory.current()
    else:
        inventory = provider_inventory.discover(home=explicit_home)
    row = (inventory.get('accounts') or {}).get(provider) or {}
    family = row.get('family') or provider_inventory.launcher_family(provider)
    root = Path(row['home']) if row.get('home') else home / ('.pi/agent' if family == 'pi' else '.' + str(family))
    patterns = {'codex': [f'sessions/**/rollout-*-{session}.jsonl', f'archived_sessions/rollout-*-{session}.jsonl'],
                'claude': [f'projects/*/{session}.jsonl'],
                'pi': [f'sessions/**/*{session}*.jsonl']}
    for pattern in patterns.get(family, []):
        found = next(root.glob(pattern), None)
        if found:
            _SESSION_LOG_CACHE[log_key] = (family, found)
            return family, found
    return None


def session_state(agent_id, home=None):
    """{'compactions', 'compacted_tokens', 'context_tokens', 'historical_context_tokens'} from the agent's session log.
    `compacted_tokens` sums the historical pre-compaction context each compaction replaced (Codex: the last positive reading
    before the event; Claude `preTokens` / `cumulativeDroppedTokens`; Pi `tokensBefore`); `context_tokens` is the latest
    active reading; `historical_context_tokens` is their uncompressed sum (`compacted_tokens + context_tokens`)."""
    located = session_log(agent_id, home)
    if not located:
        return {'telemetry_status': 'unknown', 'telemetry_reason': 'session_log_unavailable'}
    provider, path = located
    if provider in ('antigravity', 'antigravity-resilient'):
        return antigravity_telemetry.state(path)
    state_key = None
    try:
        st = path.stat()
        state_key = (provider, str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        state_key = None
    if state_key is not None and state_key in _SESSION_STATE_CACHE:
        return dict(_SESSION_STATE_CACHE[state_key])
    compactions, compacted, context = 0, 0, None
    try:
        with open(path, encoding='utf-8', errors='ignore') as handle:
            for line in handle:
                if provider == 'codex':
                    if '"type":"compacted"' in line:
                        compactions, compacted, context = compactions + 1, compacted + (context or 0), None
                    elif '"token_count"' in line and '"last_token_usage"' in line:
                        usage = ((json.loads(line).get('payload') or {}).get('info') or {}).get('last_token_usage') or {}
                        inp = usage.get('input_tokens')
                        if isinstance(inp, (int, float)) and not isinstance(inp, bool) and inp > 0:
                            context = int(inp)
                elif provider == 'claude':
                    if '"compact_boundary"' in line:
                        meta = json.loads(line).get('compactMetadata') or {}
                        before = meta.get('preTokens')
                        dropped = meta.get('cumulativeDroppedTokens')
                        post = meta.get('postTokens')
                        compactions += 1
                        compacted = max(compacted + (before or context or 0), dropped if isinstance(dropped, int) else 0)
                        context = post if isinstance(post, int) and post > 0 else None
                    elif '"assistant"' in line and '"usage"' in line:
                        usage = (json.loads(line).get('message') or {}).get('usage') or {}
                        cur = sum(usage.get(k) or 0 for k in ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'))
                        if cur > 0:
                            context = cur
                elif '"type":"compaction"' in line:
                    before = json.loads(line).get('tokensBefore')
                    compactions, compacted, context = compactions + 1, compacted + (before or context or 0), None
                elif '"usage"' in line and '"assistant"' in line:
                    usage = (json.loads(line).get('message') or {}).get('usage') or {}
                    context = sum(usage.get(k) or 0 for k in ('input', 'cacheRead', 'cacheWrite')) or context
    except (OSError, ValueError):
        return {}
    result = {'compactions': compactions, 'compacted_tokens': compacted,
              'historical_context_tokens': compacted + (context or 0),
              **({'context_tokens': context} if context is not None else {})}
    if state_key is not None:
        _SESSION_STATE_CACHE[state_key] = dict(result)
    return result


def worker_historical_tokens(worker):
    """Total historical uncompressed context tokens (sum of pre-compaction contexts + current active context),
    never just the post-compaction compressed window."""
    if not isinstance(worker, dict):
        return 0
    compacted = worker.get('compacted_tokens') or 0
    current = worker.get('context_tokens') or 0
    hist = worker.get('historical_context_tokens') or 0
    return max(hist, compacted + current)


def worker_worn(worker, policy):
    """True when the worker has reached `rotate_after_carried_tokens` (default 2M)
    measured by historical uncompressed context tokens (never compressed window size)."""
    if not isinstance(worker, dict):
        return False
    return worker_historical_tokens(worker) >= policy.get('rotate_after_carried_tokens', 2000000)


MEASURED_MAX_AGE_DAYS = 14      # older measurements describe a past regime
OBSERVED_MAX_AGE_HOURS = 2      # the live burn rate is only worth acting on while it is current


METERED_LAST_RESORT = 'metered access is a last resort'


def accounts_override_path(environ=None):
    env = os.environ if environ is None else environ
    configured = (env.get('ROUTER_ACCOUNTS_OVERRIDE') or env.get('AGENTCOMPASS_ACCOUNTS_OVERRIDE')
                  or env.get('ROUTE_MANAGER_ACCOUNTS_OVERRIDE'))
    if configured:
        return Path(configured).expanduser()
    state_dir = env.get('ROUTER_STATE_DIR')
    if state_dir:
        return Path(state_dir).expanduser() / 'accounts-override.json'
    return Path.home() / '.agent-compass' / 'accounts-override.json'


def fleet_document_path(environ=None):
    """The public accounts.json (the fetched copy when newer than the shipped one). Accounts, plans and capacities
    come from discovery and public data; personal choices live in accounts-override.json. No machine-local fleet
    document exists. ROUTER_ACCOUNTS_FILE pins an explicit document for tests and fixtures only."""
    env = os.environ if environ is None else environ
    configured = (env.get('ROUTER_ACCOUNTS_FILE') or '').strip()
    if configured and Path(configured).expanduser().is_file():
        return Path(configured).expanduser()
    return public_data.effective(REFERENCES / 'accounts.json', environ)


def configured_quota_sources(environ=None):
    """Declared quota pools from the fleet document, without running discovery."""
    try:
        doc = json.loads(fleet_document_path(environ).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {}
    return provider_inventory.quota_sources(doc)


def reference_fleet_path():
    """Checked-in fleet fixture for tests. It is not read from the user home."""
    return Path(__file__).resolve().parent / 'fixtures' / 'reference-accounts.json'


def load_accounts(path=None):
    source = Path(path) if path is not None else fleet_document_path()
    accounts = accounts_override.normalize(json.loads(source.read_text(encoding='utf-8-sig')))
    if path is None:
        user_file = accounts_override_path()
        user_doc = None
        if user_file.is_file():
            try:
                loaded = json.loads(user_file.read_text(encoding='utf-8-sig'))
                if isinstance(loaded, dict):
                    user_doc = loaded
            except (OSError, ValueError):
                user_doc = None
        if user_doc is not None:
            user_doc = accounts_override.migrate(user_doc, accounts)
            if isinstance(user_doc.get('scoring'), dict):
                accounts['scoring'] = accounts_override.merge(accounts['scoring'], user_doc['scoring'])
            route_floors = (accounts.get('scoring') or {}).get('route_ability_floor')
            if isinstance(route_floors, dict) and isinstance(accounts.get('capability_policy'), dict):
                routes = accounts['capability_policy'].setdefault('routes', {})
                for r_name, r_floors in route_floors.items():
                    if isinstance(r_floors, dict) and isinstance(routes.get(r_name), dict):
                        routes[r_name]['ability_floor'] = accounts_override.merge(
                            routes[r_name].get('ability_floor') or {}, r_floors)
            for m_name, m_spec in (user_doc.get('models') or {}).items():
                if isinstance(m_spec, dict) and isinstance(accounts.get('models', {}).get(m_name), dict):
                    if isinstance(m_spec.get('subjective'), dict):
                        accounts['models'][m_name]['subjective'] = accounts_override.merge(
                            accounts['models'][m_name].get('subjective', {}), m_spec['subjective'])
        provider_inventory.apply(accounts)
        if user_doc is not None:
            accounts_override.apply(accounts, user_doc)
            for owner, spec in accounts.get('accounts', {}).items():
                for bid, bundle in (spec.get('bundles') or {}).items():
                    account = accounts_override.preference_account(bundle, owner, accounts)
                    harnesses = (user_doc.get('harness_overrides') or {}).get(account) or {}
                    harness = accounts_override.harness(bundle, owner, bid, bundle_harness)
                    if (harnesses.get(harness) or {}).get('disabled'):
                        bundle['_harness_disabled'] = True
                        bundle['_model_disabled'] = bundle.get('disabled', False)
                        bundle['disabled'] = True
    override = None if path else capacity_override_path()
    if override is not None:
        try:
            measured = json.loads(override.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            measured = {}
        apply_measured(accounts, measured if isinstance(measured, dict) else {}, datetime.now(timezone.utc))
    provider_inventory.apply_shared_quota(accounts)
    try:
        # An account hold from when the account had one pool now holds only its failed models' pools (provider_retry).
        provider_retry.scope_account_holds(accounts)
    except OSError:
        pass
    if path is None:
        try:
            accounts['_host_policy_sync'] = host_policy.sync_discovered(accounts)
        except (OSError, host_policy.HostPolicyError) as exc:
            # Discovery still reports local facts; existing policy checks retain the denial.
            accounts['_host_policy_sync'] = {'mode': 'error', 'added': [], 'error': str(exc)}
    return accounts_override.normalize(accounts)


def apply_measured(accounts, measured, now):
    """Layer the usage readings from the tray service's capacity.json (each window's used percent and its 3h and 24h
    rise, confirmed within `OBSERVED_MAX_AGE_HOURS`) over accounts.json; they feed the demand forecast. Consumption is
    priced from accounts.json alone (list price against community capacity): the tray's usage fit
    is no longer read."""
    generated = parse_time(measured.get('generated_at'))
    if measured.get('v') != 2 or generated is None or now - generated > timedelta(days=MEASURED_MAX_AGE_DAYS):
        return
    age_hours = (now - generated).total_seconds() / 3600
    accounts['_measured'] = {'generated_at': measured['generated_at'], 'age_hours': round(age_hours, 2)}
    if age_hours <= OBSERVED_MAX_AGE_HOURS:
        for name, windows in (measured.get('observed') or {}).items():
            if name in accounts['accounts'] and isinstance(windows, dict):
                spec = accounts['accounts'][name]
                accounts['accounts'][name]['observed'] = {
                    canonical_window_id(spec, wid): row for wid, row in windows.items() if isinstance(row, dict)
                    and (confirmed := parse_time(row.get('confirmed_at'))) is not None
                    and (now - confirmed).total_seconds() <= OBSERVED_MAX_AGE_HOURS * 3600}


def capacity_usd(spec, wid, now):
    """API-priced dollars one full window allows (accounts `capacity_usd`, with a dated `capacity_change`), or None."""
    change = (spec.get('capacity_change') or {}).get(wid) or {}
    at = parse_time(change.get('at'))
    if at is not None and now >= at and change.get('usd'):
        return float(change['usd'])
    value = (spec.get('capacity_usd') or {}).get(wid)
    return float(value) if value else None


def effort_value(policy, curves, name, effort, default):
    """One effort level's value from the model's own curve (accounts `effort_curves`), else the policy-wide curve.

    Models do not respond to effort alike: on the 2026-10-09 evidence Sonnet gains 9 index points from High to Max
    and costs six times as much, Sol and Astra gain 2 and cost about twice as much."""
    own = (curves or {}).get(name) or {}
    return own[effort] if effort in own else policy[f'effort_{name}'].get(effort, default)


EFFORT_ORDER = ('low', 'medium', 'high', 'xhigh', 'max', 'ultra')


def effort_ability(policy, curves, info, effort):
    """The model's effective tier at one effort: its base tier times the curve's `ability_mult` for that effort, else
    the tier plus the curve's additive `ability` change from High. Thinking harder multiplies what the model can do;
    it does not replace it, so a weak model's Max stays below a strong model's High unless the data say otherwise."""
    tier = float((info or {}).get('tier', 0) or 0)
    mult = (curves or {}).get('ability_mult') or {}
    if effort in mult:
        return tier * float(mult[effort])
    return tier + effort_value(policy, curves, 'ability', effort, 0)


def scoring_effort(bundle, info):
    """The effort a bundle is scored at: its own, else its model's `fixed_effort` (a harness that cannot set one runs
    at the upstream's fixed level; the launcher still passes no effort)."""
    return bundle.get('effort') or (info or {}).get('fixed_effort')


def burn_scale(policy, curves, effort):
    """Quota one task spends at `effort` relative to High: the curve's `burn_mult`, else its `burn` over High's `burn`."""
    mult = (curves or {}).get('burn_mult') or {}
    if effort in mult:
        return float(mult[effort])
    reference = effort_value(policy, curves, 'burn', 'high', 1.0)
    return effort_value(policy, curves, 'burn', effort, reference) / reference


def account_family(spec):
    if not isinstance(spec, dict):
        return None
    family = spec.get('family')
    if isinstance(family, str) and family.strip():
        return family.strip().removesuffix('-resilient')
    for bundle in (spec.get('bundles') or {}).values():
        if not isinstance(bundle, dict):
            continue
        raw = bundle.get('family') or bundle.get('harness') or bundle.get('provider')
        if isinstance(raw, str) and raw.strip():
            return raw.strip().removesuffix('-resilient')
    return None


def template_capacity_usd(spec, wid):
    """Fallback window USD capacity from plan-templates.json when `spec` does not carry `capacity_usd` directly."""
    family = account_family(spec)
    if not family or spec.get('capacity_unknown'):
        return None
    plan_key = provider_inventory.configured_plan(family, spec.get('plan')) if isinstance(spec, dict) else ''
    plan = provider_inventory.plan_defaults(None, family, plan_key) if plan_key else None
    if plan is None and family not in ('claude', 'codex'):
        plan = provider_inventory.plan_defaults(None, family, 'default')
    cap = (plan or {}).get('capacity_usd', {}).get(wid) if isinstance(plan, dict) else None
    return float(cap) if isinstance(cap, (int, float)) and not isinstance(cap, bool) and cap > 0 else None


def unit_rate(spec, wid, model, effort, policy, curves=None, info=None, now=None):
    """Percent of the window this bundle spends on work that takes one worker-hour at High, or None when the account
    has no rate. With a `capacity_usd` for the window and a `usd_per_hour` for the model, that is the hour's list-price
    dollars over the window's dollars; otherwise the model's or account's percent-per-hour prior. Either way the effort
    curve's cost per task relative to High scales it: a higher effort spends about the same per hour but runs longer
    (Sol max 1.58 / 0.7 = 2.3 times High per task)."""
    reference = effort_value(policy, curves, 'burn', 'high', 1.0)
    scale = burn_scale(policy, curves, effort)
    capacity, usd = capacity_usd(spec, wid, now or datetime.now(timezone.utc)), (info or {}).get('usd_per_hour')
    if capacity is None and usd is not None:
        capacity = template_capacity_usd(spec, wid)
    if capacity and usd:
        return 100 * usd / capacity * scale
    if five_axis_usd_missing(info):
        return 0.0      # no cited usd: unpriced, not the burn prior (rule B notes it on a named dispatch)
    model_rates = (info or {}).get('burn_pct_per_worker_hour') or {}
    rate = model_rates.get(wid, spec.get('burn_pct_per_worker_hour', {}).get(wid))
    return None if rate is None else rate * reference * scale


def demand_forecast(spec, wid, state, policy):
    """(mean, sd, rate, source): percent of the window that other work will draw before the window ends (its reset, or
    the plan's expiry if sooner). A weekly window uses the observed 24h rate while capacity.json is fresh; without one
    (a rolling window that began less than a day ago) the average since the period began, once
    `demand_min_elapsed_hours` have passed; otherwise the straight-line schedule. A 3h rate says nothing about a week
    (at night it is zero). A short window uses the 3h rate and has no forecast without it."""
    row = (spec.get('observed') or {}).get(wid) or {}
    rate, source = None, 'observed'
    if state['short']:
        rate = row.get('pct_per_hour_3h')
        if rate is None:
            rate = row.get('pct_per_hour_24h')
        if not isinstance(rate, (int, float)) or rate < 0:
            return None
    else:
        rate = row.get('pct_per_hour_24h')
        # Expected reset shortens future demand, not the elapsed observation period.
        elapsed = state['hours'] - state.get('natural_reset_hours', state['reset_hours'])
        if (not isinstance(rate, (int, float)) or rate < 0) and elapsed >= policy['demand_min_elapsed_hours'] and 'remaining' in state:
            rate, source = max(0.0, state.get('full', 100.0) - state['remaining']) / elapsed, 'period average'
        if not isinstance(rate, (int, float)) or rate < 0:
            rate, source = state.get('full', 100.0) / state['hours'], 'straight-line schedule'
        if source == 'observed' and state.get('remaining', 100) < state.get('target_remaining', 0):
            rate = max(rate, float(policy.get('demand_pace_floor_ratio', 0.5)) * state.get('full', 100.0) / state['hours'])
    mean = rate * max(0.0, state['end_hours'])
    # the floor shrinks with the time left, so quota about to lapse costs nothing
    floor = policy['demand_sd_floor_pct'] * min(1.0, max(0.0, state['end_hours']) / 24)
    return mean, max(policy['demand_cv'] * mean, floor, 1e-6), rate, source


def expected_shortage(remaining, mean, sd):
    """E[(D - remaining)+] in percent for demand D ~ Normal(mean, sd): how much wanted work the window cannot serve."""
    z = (mean - remaining) / sd
    return sd * exp(-z * z / 2) / sqrt(2 * pi) + (mean - remaining) * (1 + erf(z / sqrt(2))) / 2


def shortage_probability(remaining, mean, sd):
    return (1 + erf((mean - remaining) / (sd * sqrt(2)))) / 2


def window_capacity_usd(spec, wid, now=None):
    """API-equivalent USD capacity for a window: explicit `capacity_usd` when set, else the plan template's
    `capacity_usd` for the account family, else the equivalent pool USD derived from `burn_pct_per_worker_hour`."""
    explicit = capacity_usd(spec, wid, now or datetime.now(timezone.utc))
    if explicit is not None:
        return explicit
    derived = template_capacity_usd(spec, wid)
    if derived is not None:
        return derived
    rate = (spec.get('burn_pct_per_worker_hour') or {}).get(wid)
    return round(100.0 * 2.0 / float(rate), 2) if isinstance(rate, (int, float)) and rate > 0 else None


def bundle_task_usd(spec, model, effort, hours, policy, curves=None, info=None, context_scale=1.0, fast=False):
    """Expected API-equivalent task budget in USD for `hours` active worker-hours (`hours * usd_per_hour * effort_scale * context_scale * fast_scale`)."""
    if spec.get('unlimited') or spec.get('unmetered'):
        return 0.0
    scale = burn_scale(policy, curves, effort)
    fast_scale = fast_modes.codex_burn_multiplier(policy, spec) if fast else 1.0
    return hours * model_usd_per_hour(model, info) * scale * context_scale * fast_scale


USD_PER_HOUR_DEFAULTS = {'gemini-4-argon-high': 4.0, 'gemini-3.8-flash-high': 0.5, 'grok-4.7': 1.5}
DURATION_MULTIPLIER_DEFAULTS = {'gemini-4-argon-high': 1.35, 'gpt-6.1-sol': 1.35}


def model_duration_multiplier(model, info=None):
    """Relative single-task duration multiplier vs baseline (slower models like Argon and Sol 6.1 take ~1.35x longer)."""
    mult = (info or {}).get('duration_multiplier')
    value = float(mult if mult is not None else DURATION_MULTIPLIER_DEFAULTS.get(model, 1.0))
    return value if value > 0 else 1.0


def five_axis_usd_missing(info):
    """True on the five-axis path when the model has no cited usd_per_hour (its value was removed, not defaulted)."""
    return (info or {}).get('usd_per_hour') is None and ((info or {}).get('five_axis') or {}).get('usd_status') == 'missing'


def model_usd_per_hour(model, info=None):
    """List-price dollars of one active worker-hour at High (accounts `usd_per_hour`, else a prior). On the five-axis
    path a model without a cited usd prices at 0 instead of the prior: rule B keeps it out of automatic routing on a
    metered account, and a named dispatch carries a note that its cost is not priced (review F4)."""
    usd = (info or {}).get('usd_per_hour')
    if usd is None and five_axis_usd_missing(info):
        return 0.0
    return float(usd if usd is not None else USD_PER_HOUR_DEFAULTS.get(model, 4.0))


def inflight_reserved_usd(account, spec, wid, wspec, state, win_cap_usd, records, accounts, policy, now):
    """Hold API-equivalent budget only for live dispatch leases, once per dispatch ID.

    Active commitments remain fully reserved until terminal/release/lease expiry.
    This is conservative while usage catches up; provider percentages cannot reliably
    attribute consumption to one concurrent worker. Plan/select/record never reserve.
    """
    if not win_cap_usd or win_cap_usd <= 0:
        return 0.0
    active = {}
    for rec in records or ():
        if not isinstance(rec, dict) or rec.get('stage') != 'dispatch' or not rec.get('dispatch_id'):
            continue
        expiry = parse_time(rec.get('lease_until'))
        if rec.get('state') not in ('prepared', 'launching', 'launched', 'started') or expiry is None or expiry <= now:
            continue
        bundle = rec.get('bundle') or {}
        if provider_inventory.quota_source(bundle.get('account'), accounts) != provider_inventory.quota_source(account, accounts):
            continue
        bound = dict(bundle)
        bound.setdefault('account', account)
        if wid not in bundle_pools(bound, accounts):
            continue
        amount = rec.get('expected_usd')
        if isinstance(amount, (int, float)) and not isinstance(amount, bool) and amount > 0:
            active[rec['dispatch_id']] = float(amount)
    return sum(active.values())


def five_axis_pacing(state, forecast, policy, pacing):
    """The pacing shadow price of one long window on the five-axis path (round 6, user 2026-11-09): how much of a
    marginal unit of the window other work will want before the window ends. Projected demand over the
    rest of the window (the allocator's forecast: observed rate, else the period average, else the straight line, never
    below `demand_pace_floor_ratio` of the straight line) over the usable remainder, times where usage stands against an
    even pace ((used + k) / (elapsed + k), k = `pace_smoothing`; ahead of pace raises it). It is not zero while the
    window is far from empty: a window that will be needed later is worth something at every point of the period. Not
    capped at 1: above 1 demand outruns the window, and the price has to keep rising to ration it (a cap held the price
    flat from the first hour of a heavy week, so running ahead of pace could not raise it)."""
    full, hours = state.get('full', 100.0), max(float(state['hours']), 1e-6)
    rate = max(float(forecast[2] or 0.0), float(policy.get('demand_pace_floor_ratio', 0.5)) * full / hours)
    demand = rate * max(0.0, state['end_hours'])
    usable = state['remaining'] - policy['safety_margin_pct']
    elapsed = clamp(1.0 - state.get('natural_reset_hours', state['reset_hours']) / hours, 0.0, 1.0)
    used = clamp(1.0 - state['remaining'] / full, 0.0, 1.0)
    k = float(pacing.get('pace_smoothing', 0.1))
    pace = (used + k) / (elapsed + k)
    pressure = demand / usable if usable > 0 else float('inf')
    pressure = min(pressure, 99.0)      # an empty usable remainder: the shortage term and the reserve take over
    return {'price': round(pressure * pace, 4), 'pressure': round(pressure, 4),
            'pace': round(pace, 4), 'demand_pct': round(demand, 1), 'used_pct': round(100 * used, 1),
            'elapsed_pct': round(100 * elapsed, 1)}


def window_cost(spec, wid, state, step, forecast, policy, result, now=None, step_usd=None, unit=None, pacing=None):
    """One window's share of the opportunity cost: the other work that spending `step` here would push past the
    window's end (expected shortage with the step minus without it), valued at list price against the window's USD
    capacity (explicit `capacity_usd`, else the derived pool) times the opportunity exchange
    (`exchange_rate`, preferring `scoring.opportunity_exchange`). Only a window with no USD
    capacity at all counts worker-hours, at this bundle's own burn (`unit`, %/worker-hour) or the account prior if
    higher: dividing a high-burn model's shortage by a cheap model's prior inflated it (Argon 1.6%/h over Flash
    0.2%/h was 8x, fixed 2026-10-27).

    On the five-axis path (`pacing`, round 6) displaced work is counted in hours of the window's reference model at
    `score_per_displaced_worker_hour` (option-B usd runs about 6.6x the shipped usd scale the per-usd exchange was set on:
    Opus $49.2/h against $7.5/h), and the window also charges its pacing cost: the step in reference-model hours times
    the same exchange times `five_axis_pacing`. The window costs the larger of the two (both price the same displaced
    work: the shortage term its tail, the pacing term its expectation), plus the scarcity term."""
    mean, sd, rate, source = forecast
    remaining, end = state['remaining'], state['end_hours']
    usable = remaining - policy['safety_margin_pct']       # other work cannot use the reserve either
    shortage = expected_shortage(usable - step, mean, sd) - expected_shortage(usable, mean, sd)
    shortage_prob = shortage_probability(usable - step, mean, sd)
    now_dt = now or datetime.now(timezone.utc)
    capacity = capacity_usd(spec, wid, now_dt)
    eff_cap_usd = capacity or window_capacity_usd(spec, wid, now_dt)
    real_step_usd = (step * capacity / 100.0) if capacity else (step_usd if step_usd is not None else (step * eff_cap_usd / 100.0 if eff_cap_usd else None))
    rem_usd = (remaining * eff_cap_usd / 100.0) if eff_cap_usd else None
    after_usd = (rem_usd - real_step_usd) if (rem_usd is not None and real_step_usd is not None) else None
    ref_usd = (pacing or {}).get('reference_usd_per_hour')
    pace_row = None
    if eff_cap_usd and ref_usd:
        displaced = shortage * eff_cap_usd / 100
        per_hour = exchange_rate(policy, 'shortage_loss_per_worker_hour')
        cost = per_hour * displaced / ref_usd
        if real_step_usd is not None and pacing.get('pace', True):
            pace_row = five_axis_pacing(state, forecast, policy, pacing)
            pace_row['reference_usd_per_hour'] = round(ref_usd, 4)
            pace_row['cost'] = round(per_hour * real_step_usd / ref_usd * pace_row['price'], 4)
            cost = max(cost, pace_row['cost'])
    elif eff_cap_usd:
        displaced = shortage * eff_cap_usd / 100
        cost = exchange_rate(policy, 'shortage_loss_per_usd') * displaced
    else:
        rate = max(float((spec.get('burn_pct_per_worker_hour') or {}).get(wid) or 0.0), float(unit or 0.0))
        displaced = shortage / rate if rate > 0 else 0.0
        cost = exchange_rate(policy, 'shortage_loss_per_worker_hour') * displaced
    scarcity_ref = float(policy.get('scarcity_usable_ref_pct', 45.0))
    scarcity = (shortage_prob * clamp(1.0 - max(0.0, usable - step) / scarcity_ref, 0.0, 1.0) ** 1.5
                if not state.get('short') and scarcity_ref > 0 else 0.0)
    scarcity_cost = float(policy.get('scarcity_penalty_max', 0.06)) * scarcity
    cost += scarcity_cost
    expires_first = end < state['reset_hours']
    horizon = float(policy.get('reset_horizon_hours') or 0)
    if state.get('expected_reset_at'):
        horizon = max(horizon, state['reset_hours'])
    sunk_weight = 0.0
    excess_pct = max(0.0, (usable - step) - mean)
    if (horizon > 0 and not state.get('short') and not state.get('stale') and not state.get('deferred_hours')
            and 0 < state['reset_hours'] <= horizon and usable - step > 0):
        base = clamp(1.0 - state['reset_hours'] / horizon, 0.0, 1.0)
        # Smooth sprint: at 24/12/6/3/1h, default bonus is .26/.58/.84/1.00/1.13
        # for a fully surplus window. Both natural and anticipated resets use it.
        urgency = (0.4 * base + 0.6 * exp(-state['reset_hours'] / 12.0)) ** float(policy.get('reset_urgency_power', 1.0))
        surplus_ref = max(1e-6, float(policy.get('reset_surplus_ref_pct', 15)))
        surplus = (1.0 - shortage_prob) * clamp(excess_pct / surplus_ref, 0.0, 1.0)
        sunk_weight = round(urgency * surplus, 4)
    reset_bonus = round(float(policy.get('reset_bonus_max', 1.2)) * sunk_weight, 4) if sunk_weight > 0 else 0.0
    cost *= 1.0 - sunk_weight
    scarcity_cost *= 1.0 - sunk_weight
    if pace_row is not None:
        pace_row['cost'] = round(pace_row['cost'] * (1.0 - sunk_weight), 4)
    if source == 'observed' and mean > remaining:
        result['notes'].append(f"{wid}: other work at the observed {rate:.2f}%/h wants ~{mean:.0f}% before its "
                               f"{'expiry' if expires_first else 'reset'} in {end:.0f}h; {remaining:.0f}% is left")
    return {'window': wid, 'remaining': remaining, 'after': remaining - step, 'step_pct': step,
            **({'capacity_usd': round(eff_cap_usd, 2), 'remaining_usd': round(rem_usd, 2),
                'reserved_usd': round(state.get('reserved_usd', 0.0), 2),
                'expected_usd': round(real_step_usd, 2), 'after_usd': round(after_usd, 2)}
               if eff_cap_usd and rem_usd is not None and real_step_usd is not None else {}),
            'reset_hours': state['reset_hours'], 'end_hours': end, 'expires_first': expires_first,
            **({k: state[k] for k in ('natural_reset_hours', 'expected_reset_at') if k in state}),
            'demand_pct': round(mean, 1), 'demand_source': source,
            'shortage_probability': round(shortage_prob, 3),
            'scarcity_cost': round(scarcity_cost, 4),
            ('usd_displaced' if eff_cap_usd else 'worker_hours_displaced'): round(displaced, 3),
            **({'reset_sunk_weight': sunk_weight, 'reset_bonus': reset_bonus, 'surplus_pct': round(excess_pct, 1)}
               if reset_bonus > 0 else {}),
            **({'pacing': pace_row} if pace_row is not None else {}),
            'opportunity_cost': round(cost, 4)}


def load_capabilities(path=None, accounts=None):
    if path is not None:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    from capability_catalog import compose
    return compose(load_accounts() if accounts is None else accounts)


def parse_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def clamp(value, low, high):
    return max(low, min(high, value))


def models_table(accounts):
    return {k: v for k, v in accounts['models'].items() if isinstance(v, dict)}


def resolve_model(model, accounts):
    """model_resolution.resolve_model against this document: {'key', 'source', 'effort', 'bare'}."""
    return _resolve_model(model, accounts.get('models', {}), accounts.get('aliases', {}))


def canonical(model, accounts):
    return resolve_canonical_model(model, accounts.get('models', {}), accounts.get('aliases', {})) or model


def model_spec(accounts, model):
    """The score row for a name, by how it resolved: `exact`/`alias` read the row itself, `family` the target shrunk by
    auto_bundles (tier -0.3) exactly as a discovered family model is, `unknown` the baseline."""
    resolved = resolve_model(model, accounts)
    if resolved['source'] == 'family':
        return {**auto_bundles.default_profile(model, accounts)['spec'], 'usd_per_hour': auto_bundles.ESTIMATED_USD_PER_HOUR,
                'usd_estimated': True, 'score_estimated': True}
    value = accounts.get('models', {}).get(resolved['key'])
    return value if isinstance(value, dict) else generic_model_spec(accounts)


def model_curves(accounts, model):
    """This name's `effort_curves` row. A family guess has none: the flagship's speed and curve are not measured on it."""
    resolved = resolve_model(model, accounts)
    if resolved['source'] not in DIRECT_SOURCES:
        return {}
    return (accounts.get('effort_curves') or {}).get(resolved['key']) or {}


def model_leaf(model):
    return split_model_effort(model)[0].rsplit('/', 1)[-1]


def row_key(model, accounts):
    """Which model a row runs: the resolved key for its calibrated id or another spelling of it, its own name for a family
    guess or an unknown model. `canonical` folds a family guess to its target for scoring; two rows are the same model
    only when this says so."""
    resolved = resolve_model(model, accounts)
    return resolved['key'] if resolved['source'] in DIRECT_SOURCES else model_leaf(model).lower()


def lookup_names(model, accounts=None):
    """Names a table may hold this model under: its resolved key, then its raw spellings. A family guess never answers
    for the flagship it folded to, so its key is left out."""
    raw = split_model_effort(model)[0]
    names = [raw, model_leaf(model)]
    if accounts:
        resolved = resolve_model(model, accounts)
        if resolved['source'] in DIRECT_SOURCES:
            names.insert(0, resolved['key'])
    return list(dict.fromkeys(name for name in names if name))


def bundle_harness(bundle, account=None, bundle_id=None, *, family=None):
    """Execution harness family for a bundle (distinct from its billing account or gateway provider)."""
    explicit = str((bundle or {}).get('harness') or '').strip().lower()
    if explicit:
        return explicit
    prov = str((bundle or {}).get('provider') or account or '').strip().lower()
    bid = str(bundle_id or (bundle or {}).get('id') or '').strip().lower()
    launch_m = str((bundle or {}).get('launch_model') or '').strip().lower()
    model = str((bundle or {}).get('model') or '').strip().lower()
    family = family or (prov if prov in ('claude', 'codex', 'pi', 'omp', 'opencode', 'grok', 'grok-bot',
                                         'antigravity', 'antigravity-resilient') else None)
    if family:
        return 'antigravity-resilient' if family == 'antigravity' else family
    if bid.startswith('pi_') or '__pi_' in bid or launch_m.startswith('openai-codex/'):
        return 'pi'
    if launch_m.startswith('opencode/'):
        return 'opencode'
    if prov in ('dot', 'chatgpt-web'):
        return prov
    if 'claude' in model:
        return 'claude'
    return 'codex'


def bundles(accounts):
    """Flat list of launchable bundles with their account and execution harness attached."""
    out = {}
    inventory = accounts.get('_discovery') or {}
    for account, spec in accounts['accounts'].items():
        for bundle_id, bundle in spec.get('bundles', {}).items():
            provider = bundle.get('provider', account)
            row = ((inventory.get('launchers') or {}).get(provider)
                   or (inventory.get('accounts') or {}).get(provider)
                   or ((accounts.get('accounts') or {}).get(provider) or {}))
            # `model:effort` is a fixed effort: the name is the model, the suffix the only level this row runs at.
            model_name, fixed = split_model_effort(bundle['model'])
            out[bundle_id] = {'id': bundle_id, 'account': account, 'provider': provider,
                              'harness': bundle_harness(bundle, account, bundle_id, family=row.get('family')),
                              'model': model_name, 'effort': bundle.get('effort') or fixed,
                              **({'effort_fixed': True} if fixed else {}),
                              **{k: bundle[k] for k in ('modeId', 'disabled', 'disabled_by_default', 'efforts', 'launch_model', 'when',
                                                        'discovery_unavailable', 'draws',
                                                        'auto', 'billing', 'score_source', 'score_basis', 'cost_estimated',
                                                        'availability', 'compatibility', 'compatibility_reason') if k in bundle}}
    return out


def route_preference(preferred, peers=None):
    """Drop a peer whose anchor is also listed. The peer then shares that anchor's place."""
    peers = peers or {}
    present = set(preferred)
    collapsed = []
    for name in preferred:
        anchor = peers.get(name)
        if anchor and anchor in present:
            continue
        collapsed.append(name)
    return collapsed


def fit_score(kind, model, capabilities, accounts):
    """First of the route's preferred list is 1.0. Behind it a `strict_order` route (the user's ranked order, e.g.
    visual) steps down evenly to 0.2; on any other route the fallbacks sit in a narrow band, so ability and opportunity
    cost choose among them. Stepping 0.2 per place starved every model the lists put low or left out.
    A configured route-order peer shares its anchor's place."""
    route = capabilities['routes'].get(kind) or {}
    raw = [canonical(m, accounts) for m in route.get('preferred', [])]
    peers = capabilities.get('route_fit_peers') or {}
    preferred = route_preference(raw, peers)
    if not preferred and not raw:
        return 0.6
    anchor = peers.get(model)
    if anchor and anchor in preferred and model in raw:
        place = preferred.index(anchor)
    else:
        place = preferred.index(model) if model in preferred else None
    if place is None and route.get('capability_fit'):
        # A route that names the capability it needs ranks the models it does not list by their vectors, so a model
        # joins or leaves with its subscription and no route edit (router upgrade 2026-10-19; was a flat 0.3).
        return round(0.2 + 0.5 * capability_score(route['capability_fit'], model, accounts), 4)
    if route.get('strict_order'):
        if place is None:
            return 0.3
        step = 0.2 if len(preferred) <= 5 else 0.8 / (len(preferred) - 1)
        return max(0.2, round(1 - step * place, 4))
    if place == 0:
        return 1.0
    band = accounts['scoring'].get('fallback_fit', {})
    start, step, floor = band.get('start', 0.6), band.get('step', 0.04), band.get('floor', 0.4)
    return floor if place is None else max(floor, start - step * (place - 1))


CAPABILITY_DIMENSIONS = ('reasoning', 'coding', 'exploration', 'anti_hallucination', 'visual', 'gui')
SUBJECTIVE_DIMENSIONS = ('divergence', 'global_rigor', 'module_precision')
INTERACTION_STYLES = ('engineering', 'exploration', 'text_only', 'advisory')
PROFILE_INTEGERS = {'reasoning_depth': (1, 4), 'code_complexity': (0, 3), 'context_scale': (1, 3),
                    'divergence': (1, 3), 'hallucination_risk': (1, 3), 'error_cost': (1, 3)}
PROFILE_FLAGS = ('needs_visual', 'needs_gui', 'local_only', 'realtime_loop', 'wants_fast', 'read_only_review')
REALTIME_LOOP_QUESTION = (
    'True ONLY when the worker agent itself must stay inside a running external system\'s continuous observe-act loop '
    'across turns where slow per-turn model latency directly degrades the live outcome (e.g. personally playing a live '
    'game / dailies / dungeons / AFK loop, sniping tickets / courses / appointments / flash sales live, watching a live '
    'order book or API stream and placing orders in real time, polling a live page / app / API to act immediately when a '
    'state appears, interacting with live-stream chat). False for ALL software engineering, design, debugging, or analysis '
    'tasks—even when the subject matter is real-time, polling, games, dashboards, or flash sales (e.g. writing or fixing a '
    'ticket/game/automation script or bot, implementing real-time page refresh or message push, fixing page polling logic, '
    'designing a live trading dashboard frontend, diagnosing client loop waits or refresh latency, adding a countdown modal '
    'or live chat component, fixing flash-sale page CSS, analyzing game/AFK logs, or a one-off GUI click/setting toggle).')
WANTS_FAST_QUESTION = (
    'True ONLY when the assignment affirmatively requests fast mode, priority service tier, urgent/ASAP turnaround, '
    'or a pre-reset quota sprint (e.g. "pre-reset sprint", "before quota resets", "重置前冲刺") for the task itself, '
    'and does not negate or opt out of fast mode (e.g. "do not use fast mode", "no fast mode", or "not urgent" -> '
    'false). False for action adverbs inside runtime steps (e.g. "click Save immediately when the button appears"), '
    'ordinary mentions of quota/reset without requesting fast execution, or ordinary tasks without an explicit speed/fast request.')
READ_ONLY_REVIEW_QUESTION = (
    'True ONLY when the assignment is a read-only code, diff, branch, security, or release review/audit that inspects '
    'existing material and reports findings without writing or modifying repository production/test code (code_complexity = 0, '
    'even if the reviewer saves its review report file to a specified output path). False when the worker implements, '
    'fixes, or refactors code (including fixing issues found by a prior review), runs operational/release commands, '
    'or performs general exploration/research that is not a code/diff/release audit.')
ACCESS_LEVELS = ('read-only', 'write')
ACCESS_QUESTION = (
    'Whether this brief only reads, inspects, reviews, or analyzes without modifying workspace files or state '
    '("read-only"), or writes, edits, creates, or deletes workspace files or runs mutating commands ("write"). '
    'Any brief with read_only_review=true is "read-only"; any brief with code_complexity > 0 is "write".')
DEPTH_BY_DIFFICULTY = {'routine': 1, 'hard': 2, 'coupled': 3, 'frontier': 4}
DIFFICULTY_ORDER = ('routine', 'hard', 'coupled', 'frontier')
HARD_BY_DEFAULT = ('debug_review', 'general_implementation', 'frontend_implementation', 'computer_use',
                   'exploration_analysis', 'research_packet')
# The System-One task profile (spec 2026-10-19). Integers carry minimum/maximum, never an integer enum: Gemini's
# protobuf schema rejects integer enums ("enum[0] cannot be empty").
HOURS_RANGE = (0.05, 24.0)
CONFIDENCE_LEVELS = ('low', 'medium', 'high')
URGENCY_LEVELS = ('unknown', 'low', 'normal', 'high')
# Whether the brief bundles separate tasks. One question for every scorer (LLM schema field, Jev noul/choice); the
# parent, not the router, then works out what the tasks are.
TASK_COUNTS = ('1', '2', '3', '>3')
MULTI_TASK_QUESTION = (
    'True when the brief bundles more than one separate task: deliverables that different workers could produce at the '
    'same time without waiting for each other\'s result (e.g. collect reference images, compare game engines, design '
    'the gameplay loop). False for one task, however many steps: investigate-fix-test-release, implement plus its '
    'tests, a review plus its report, the remaining steps of a handoff, a brief that already assigns one share of a '
    'larger task, or a sequence where each part needs the previous part\'s output.')
TASK_COUNT_QUESTION = 'How many separate tasks, in the sense of multi_task, the brief holds: 1 when multi_task is false.'
TASK_PROFILE_SCHEMA = {
    'type': 'object',
    'properties': {
        'reasoning_depth': {'type': 'integer', 'minimum': 1, 'maximum': 4, 'description': (
            'How hard the thinking is, not how long the brief is. 1 = routine or direct execution with obvious steps, even '
            'when the brief is long and sectioned (run an existing script, checklist, drift round, build/install/switch release '
            'runbook; a mechanical edit or constant tweak; click and screenshot; reformat text); 2 = standard engineering or '
            'analysis with a clear direction (implement or fix one module, a standard read-only review of a diff/module, '
            'execute a structured research packet and report metrics, diagnose one subsystem); 3 = cross-module architectural '
            'design/refactor, subtle concurrency or distributed-invariant debugging, or orchestrating a multi-stage research '
            'line; 4 = frontier mathematical, strategic or architectural critique and formulation.')},
        'code_complexity': {'type': 'integer', 'minimum': 0, 'maximum': 3, 'description': (
            'Production code the worker itself writes or modifies (NOT the complexity of code being read or reviewed). '
            '0 = none: ANY read-only code/release/diff review or independent audit (even when saving a review report file '
            'to a specified path), diagnosis that only proposes, running existing CLI/release/test commands, log or data '
            'exploration, text drafting; 1 = a small script, config/constant tweak, localized helper, or analysis scripts '
            'inside a research packet; 2 = a production module implemented, fixed or refactored with its unit tests; '
            '3 = complex multi-module, concurrency, protocol or core-library changes.')},
        'context_scale': {'type': 'integer', 'minimum': 1, 'maximum': 3, 'description': (
            'Functional scope of the code or system that must be understood, NOT the number of paths the brief mentions. '
            'Ignore boilerplate: role docs, AGENTS.md, skill/README/runbook docs, prior reports or reviews to read, receipt/'
            'output/log paths, and a module\'s paired unit-test file. 1 = one module/component or one subsystem\'s runbook '
            '(+ its tests and the boilerplate); 2 = several coupled functional modules or subsystems whose interaction must be '
            'reasoned about, or a multi-source dataset/research panel; 3 = whole-repository or cross-system architecture.')},
        'divergence': {'type': 'integer', 'minimum': 1, 'maximum': 3, 'description': (
            '1 = convergent: a known target, spec, checklist, review scope or a fix whose cause is already identified; '
            '2 = multi-hypothesis: the cause of a bug, regression or performance problem is unknown and several candidate '
            'explanations must be tested, or several given design options must be compared; 3 = open-ended/creative: formulate a '
            'new scheme, cultural/gameplay motif proposals, creative narrative/design ideation, or first-principles architecture redesign.')},
        'hallucination_risk': {'type': 'integer', 'minimum': 1, 'maximum': 3, 'description': (
            '1 = a compiler, unit test or script deterministically verifies the result; 2 = mixed engineering plus judgment; '
            '3 = a fabricated claim or missed defect would not be caught by a test runner: read-only code review/audit, '
            'quantitative metric or backtest reporting, production checklist/state verification, external research synthesis.')},
        'error_cost': {'type': 'integer', 'minimum': 1, 'maximum': 3, 'description': (
            '1 = low (read-only, sandboxed, can be rerun); 2 = medium; 3 = high (production release or a critical '
            'irreversible operation).')},
        'needs_visual': {'type': 'boolean', 'description': 'True when the work must look at images, screenshots or other visual input.'},
        'needs_gui': {'type': 'boolean', 'description': 'True when the work must operate a desktop or browser GUI through screenshots, mouse and keyboard.'},
        'local_only': {'type': 'boolean', 'description': (
            'True when the task explicitly depends on existing files, directories, or repositories on this machine '
            '(reading, copying, searching, editing, building, or testing un-inlined local files) or on local processes/GUI/'
            'environment. False when all required inputs are inlined in the prompt or fetched from the public web '
            '(web_research, self_contained_code, text_in_text_out, direction_reasoning, complex_build_plan)—even if the '
            'brief has a workspace header or names a local target path where the finished output will be saved.')},
        'realtime_loop': {'type': 'boolean', 'description': REALTIME_LOOP_QUESTION},
        'wants_fast': {'type': 'boolean', 'description': WANTS_FAST_QUESTION},
        'read_only_review': {'type': 'boolean', 'description': READ_ONLY_REVIEW_QUESTION},
        'access': {'type': 'string', 'enum': list(ACCESS_LEVELS), 'description': ACCESS_QUESTION},
        'interaction_style': {'type': 'string', 'enum': list(INTERACTION_STYLES), 'description': (
            'engineering = write or change software; exploration = explore a codebase, run experiments or backtests, '
            'analyze logs or data; text_only = pure text in and out or public web lookup; advisory = direction '
            'reasoning or complex build planning.')},
        'expected_hours': {'type': 'number', 'minimum': HOURS_RANGE[0], 'maximum': HOURS_RANGE[1], 'description': (
            'Active worker-hours this task needs at an ordinary agent pace (a few tool calls = 0.1; one module fix with a '
            'test = 0.5; a multi-file feature or investigation = 1-3; a long multi-stage build = 4 or more). Estimate '
            'work, never money, quota or balances. Use 0.5 when the text gives no basis and set hours_confidence low.')},
        'hours_confidence': {'type': 'string', 'enum': list(CONFIDENCE_LEVELS), 'description': (
            'How well the text supports expected_hours: high = scope and steps are explicit; medium = scope is clear but steps '
            'are not; low = guess.')},
        'urgency': {'type': 'string', 'enum': list(URGENCY_LEVELS), 'description': (
            'unknown unless the text itself states a deadline, an incident or "asap"; then high/normal/low. Never infer urgency '
            'from topic or tone. Shown in the result so the caller can see it; fast mode is controlled by wants_fast or --fast, not urgency.')},
        'multi_task': {'type': 'boolean', 'description': MULTI_TASK_QUESTION},
        'task_count': {'type': 'string', 'enum': list(TASK_COUNTS), 'description': TASK_COUNT_QUESTION},
    },
    'required': list(PROFILE_INTEGERS) + list(PROFILE_FLAGS) + ['access', 'expected_hours', 'hours_confidence', 'urgency', 'interaction_style',
                                                                 'multi_task', 'task_count'],
}
# kind -> (code_complexity, context_scale, hallucination_risk, error_cost, interaction_style) for the profile schema
KIND_TRAITS = {
    'general_implementation': (2, 1, 2, 2, 'engineering'),
    'frontend_implementation': (2, 1, 2, 2, 'engineering'),
    'debug_review': (2, 2, 3, 2, 'engineering'),
    'bounded_bulk_work': (1, 2, 1, 1, 'engineering'),
    'tool_execution': (1, 1, 1, 2, 'engineering'),
    'self_contained_code': (2, 1, 1, 1, 'engineering'),
    'exploration_analysis': (0, 2, 3, 1, 'exploration'),
    'research_packet': (1, 2, 3, 1, 'exploration'),
    'computer_use': (0, 1, 2, 2, 'engineering'),
    'visual_analysis': (0, 1, 3, 1, 'exploration'),
    'text_in_text_out': (0, 1, 2, 1, 'text_only'),
    'web_research': (0, 2, 2, 1, 'text_only'),
    'dot_execution': (1, 1, 2, 1, 'text_only'),
    'direction_reasoning': (0, 2, 3, 1, 'advisory'),
    'complex_build_plan': (0, 3, 3, 2, 'advisory'),
    'other_or_mixed': (1, 1, 2, 2, 'engineering'),
}
GENERIC_CALLER_KINDS = (None, 'general_implementation', 'tool_execution', 'debug_review', 'other_or_mixed')


def default_difficulty(kind):
    return 'hard' if kind in HARD_BY_DEFAULT else 'routine'


def default_divergence(kind=None, style=None):
    """Default divergence level (1..3) from structured route/style when omitted by a legacy fixture."""
    if kind == 'computer_use':
        return 1
    if kind in ('complex_build_plan', 'direction_reasoning') or style == 'advisory':
        return 3
    return 1


def validate_profile(raw):
    """A task profile with every field checked, or ValueError. `reason`, `work_kind`, `divergence`, and `realtime_loop` may be omitted in legacy fixtures."""
    if not isinstance(raw, dict):
        raise ValueError('task.profile must be an object')
    profile = {}
    if raw.get('work_kind') is not None:
        if not isinstance(raw['work_kind'], str) or raw['work_kind'] not in KIND_TRAITS:
            raise ValueError('task.profile.work_kind must be one of ' + ', '.join(KIND_TRAITS))
        profile['work_kind'] = raw['work_kind']
    for name, (low, high) in PROFILE_INTEGERS.items():
        if name == 'divergence' and name not in raw:
            continue
        value = raw.get(name)
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f'task.profile.{name} must be an integer from {low} to {high}')
        profile[name] = value
    for name in PROFILE_FLAGS:
        if name == 'realtime_loop' and name not in raw:
            continue
        if not isinstance(raw.get(name), bool):
            raise ValueError(f'task.profile.{name} must be true or false')
        profile[name] = raw[name]
    if profile.get('read_only_review') is True and profile.get('code_complexity') != 0:
        raise ValueError(
            f"task.profile.read_only_review true contradicts task.profile.code_complexity {profile.get('code_complexity')!r} (must be 0)"
        )
    if raw.get('access') is not None:
        if raw['access'] not in ACCESS_LEVELS:
            raise ValueError('task.profile.access must be one of ' + ', '.join(ACCESS_LEVELS))
        if raw['access'] == 'read-only' and profile.get('code_complexity') != 0:
            raise ValueError(
                f"task.profile.access 'read-only' contradicts task.profile.code_complexity {profile.get('code_complexity')!r} (must be 0)"
            )
        if profile.get('read_only_review') is True and raw['access'] == 'write':
            raise ValueError("task.profile.access 'write' contradicts task.profile.read_only_review true")
        profile['access'] = raw['access']
    # The hours/urgency estimate is optional: legacy fixtures and scorers that cannot produce it (Jev) leave it out.
    hours = raw.get('expected_hours')
    if hours is not None:
        try:
            finite = not isinstance(hours, bool) and isinstance(hours, (int, float)) and math.isfinite(hours)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError('task.profile.expected_hours must be a finite number')
        profile['expected_hours'] = round(min(max(float(hours), HOURS_RANGE[0]), HOURS_RANGE[1]), 2)
    for name, levels in (('hours_confidence', CONFIDENCE_LEVELS), ('urgency', URGENCY_LEVELS)):
        if raw.get(name) is not None:
            if raw[name] not in levels:
                raise ValueError(f'task.profile.{name} must be one of ' + ', '.join(levels))
            profile[name] = raw[name]
    if raw.get('interaction_style') not in INTERACTION_STYLES:
        raise ValueError('task.profile.interaction_style must be one of ' + ', '.join(INTERACTION_STYLES))
    profile['interaction_style'] = raw['interaction_style']
    reason = raw.get('reason')
    if isinstance(reason, str) and reason.strip():
        profile['reason'] = reason.strip()[:300].rstrip()
    if not isinstance(raw.get('multi_task'), bool):
        raise ValueError('task.profile.multi_task must be true or false')
    profile['multi_task'] = raw['multi_task']
    profile['task_count'] = task_count_bucket(raw.get('task_count'))
    if profile['multi_task'] != (profile['task_count'] != '1'):
        raise ValueError(f"task.profile.multi_task {str(profile['multi_task']).lower()} contradicts "
                         f"task.profile.task_count {profile['task_count']!r}")
    # The five-axis demand (round 047): asked of the classifier only while scoring.five_axis is on; optional otherwise.
    if raw.get('axis_demand') is not None:
        profile['axis_demand'] = validate_axis_demand(raw['axis_demand'])
    return profile


class InvalidCallerProfile(ValueError):
    """A profile the caller wrote failed validation; the classifier's own failures retry or block instead."""


def validate_caller_profile(raw):
    try:
        return validate_profile(raw)
    except ValueError as exc:
        raise InvalidCallerProfile(f'invalid_caller_profile: {exc}') from None


def task_count_bucket(value):
    """The schema's task-count band for a band or a positive whole number; anything else is an invalid profile."""
    if isinstance(value, str) and value in TASK_COUNTS:
        return value
    whole = value if isinstance(value, int) and not isinstance(value, bool) else (
        int(value) if isinstance(value, float) and value.is_integer() else None)
    if whole is not None and whole >= 1:
        return TASK_COUNTS[min(whole, 4) - 1]
    raise ValueError('task.profile.task_count must be one of ' + ', '.join(TASK_COUNTS) + ' or a positive whole number')


def profile_difficulty(profile, policy):
    table = (policy.get('profile') or {}).get('difficulty_by_depth') or {}
    return table.get(str(profile['reasoning_depth'])) or {1: 'routine', 2: 'hard', 3: 'coupled', 4: 'frontier'}[profile['reasoning_depth']]


def named_route(profile):
    """A classifier route other than the unmatched bucket. None when the profile did not name one."""
    if not isinstance(profile, dict):
        return None
    named = profile.get('work_kind')
    if named in KIND_TRAITS and named != 'other_or_mixed':
        return named
    return None


def profile_kind(profile, policy):
    """The route a profile implies when the caller named none.

    A named route stands. needs_gui becomes computer_use only when the profile did not name one;
    the GUI requirement itself is needs_visual / needs_gui, not a route rewrite."""
    named = named_route(profile)
    if named:
        return named
    if profile['needs_gui']:
        return 'computer_use'
    if profile['needs_visual'] and profile['code_complexity'] == 0 and profile['interaction_style'] != 'text_only':
        return 'visual_analysis'
    if profile['interaction_style'] == 'engineering' and profile['code_complexity'] == 0:
        return 'tool_execution'
    return ((policy.get('profile') or {}).get('kind_by_style') or {}).get(profile['interaction_style'], 'other_or_mixed')


def task_kind(task, capabilities, policy=None):
    """The route plan() uses: the caller's work_kind, else the profile's when no work_kind was given, else other_or_mixed."""
    routes = capabilities['routes']
    if not isinstance(task, dict):
        return 'other_or_mixed'
    prof = task.get('profile') if isinstance(task.get('profile'), dict) else None
    caller = task.get('work_kind')
    named = named_route(prof)
    # The classifier's route stands when the caller left the kind unset or repeated it.
    # A generic caller kind plus needs_gui becomes computer_use only when the profile named no real route.
    if named in routes and (caller is None or caller == named):
        return named
    if prof and prof.get('needs_gui') and caller in GENERIC_CALLER_KINDS and named is None:
        return 'computer_use'
    if caller in routes:
        return task['work_kind']
    if not task.get('work_kind') and prof is not None:
        try:
            kind = profile_kind(validate_profile(prof), policy or {})
        except ValueError:
            return 'other_or_mixed'
        return kind if kind in routes else 'other_or_mixed'
    return 'other_or_mixed'


def capability_vector(info):
    """The model's six capability values on 0..1; `visual` falls back to the legacy visual order value."""
    raw = (info or {}).get('capabilities') or {}
    legacy = (info or {}).get('visual')
    out = {}
    for dim in CAPABILITY_DIMENSIONS:
        value = raw.get(dim)
        if dim == 'visual' and value is None:
            value = legacy if isinstance(legacy, (int, float)) and not isinstance(legacy, bool) else 0.0
        out[dim] = clamp(float(value or 0.0), 0.0, 1.0)
    return out


def capability_score(weights, model, accounts):
    """Weighted mean of the model's capability values over the dimensions a route names."""
    caps = capability_vector(model_spec(accounts, model))
    total = sum(weights.values())
    return sum(w * caps[d] for d, w in weights.items() if d in caps) / total if total else 0.0


DEMAND_DEFAULTS = {
    # accounts `scoring.demand` overrides any of these. Tier points unless noted.
    'property_span': 0.5,         # extra ability the heaviest properties ask of the deepest task
    'default_load': 0.5,          # property load assumed for work without a profile (only its difficulty is known)
    'depth_coupling': 0.06,       # per-depth growth of every property's capability requirement around depth 2
    'shortfall_weight': 0.35,     # score lost per tier point of effective ability below the task's required ability
    'sprint_effort_weight': 0.06, # score a fully surplus, about-to-reset window adds for the biggest effort step above High
    'precision_headroom': 0.15,   # extra required ability for high-precision work that is not critical (critical has its own)
    'property_weights': {'code_complexity': 1.0, 'context_scale': 0.8, 'divergence': 0.8, 'hallucination_risk': 1.0,
                         'error_cost': 0.5},
}
EFFORT_SCALED_CAPABILITIES = ('reasoning', 'coding', 'exploration', 'anti_hallucination')


def demand_config(policy):
    cfg = {**DEMAND_DEFAULTS, 'property_weights': dict(DEMAND_DEFAULTS['property_weights'])}
    own = (policy or {}).get('demand') if isinstance((policy or {}).get('demand'), dict) else {}
    for key, value in own.items():
        if key == 'property_weights' and isinstance(value, dict):
            cfg['property_weights'].update(value)
        elif key in cfg:
            cfg[key] = value
    return cfg


def ability_floor(policy, route, difficulty):
    """The minimum ability a route's work needs at a difficulty: the route's own measured floor when its data declares
    one (`ability_floor` in capability_policy.routes), otherwise the scoring floor for that difficulty."""
    own = (route or {}).get('ability_floor') or {}
    return float(own[difficulty]) if difficulty in own else float(policy['ability_floor'][difficulty])


def critical_headroom(policy, difficulty):
    """Extra ability critical work needs above the difficulty's floor: `critical_headroom_by_difficulty[difficulty]` when
    the data sets one (it keeps a critical gate in place when a floor moves), otherwise `critical_headroom`."""
    own = policy.get('critical_headroom_by_difficulty') or {}
    return float(own[difficulty]) if difficulty in own else float(policy['critical_headroom'])


RESPONSIVENESS_DEFAULTS = {
    'floor': 0.50,
    'default': 0.52,
    'weight': 0.35,
    'fast_turn_seconds': 4.0,
    'slow_turn_seconds': 45.0,
    'output_tokens_per_turn': 400,
    'harness_overhead': {
        'default': 1.0,
        'antigravity': 1.0,
        'antigravity-resilient': 1.0,
        'codex': 1.0,
        'claude': 1.0,
        'pi': 1.0,
        'opencode': 1.0,
        'grok': 1.0,
    },
    'local_telemetry': {
        'min_samples': 20,
        'max_blend': 0.3,
        'max_shift': 0.15,
    },
}

REALTIME_EFFORT_ORDER = ('none', 'minimal', 'default', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')


def _effort_rank(effort):
    eff = str(effort or 'high')
    return REALTIME_EFFORT_ORDER.index(eff) if eff in REALTIME_EFFORT_ORDER else REALTIME_EFFORT_ORDER.index('high')


def responsiveness_config(policy):
    """Merged `scoring.responsiveness` policy with defaults."""
    cfg = {
        **RESPONSIVENESS_DEFAULTS,
        'harness_overhead': dict(RESPONSIVENESS_DEFAULTS['harness_overhead']),
        'local_telemetry': dict(RESPONSIVENESS_DEFAULTS['local_telemetry']),
    }
    own = (policy or {}).get('responsiveness') if isinstance((policy or {}).get('responsiveness'), dict) else {}
    for key, value in own.items():
        if key in ('harness_overhead', 'local_telemetry') and isinstance(value, dict):
            cfg[key].update(value)
        elif key in cfg or key in ('basis', 'reasoning_tokens_by_effort'):
            cfg[key] = value
    return cfg


def harness_overhead_multiplier(harness, policy=None):
    """Per-turn CLI harness overhead coefficient (e.g. antigravity/codex/claude wrapper & streaming overhead)."""
    table = responsiveness_config(policy).get('harness_overhead') or {}
    if harness and isinstance(table.get(harness), (int, float)) and not isinstance(table.get(harness), bool):
        return max(0.1, float(table[harness]))
    default = table.get('default', 1.0)
    return max(0.1, float(default) if isinstance(default, (int, float)) and not isinstance(default, bool) else 1.0)


def compute_turn_seconds(ttft_s, output_tps, effort='high', reasoning_tokens=None, output_tokens=None,
                         harness=None, policy=None):
    """Estimated seconds per live-loop turn: `T_turn = (ttft_s + output_tokens / output_tps) * harness_overhead`.

    Artificial Analysis `ttft_s` (`headline.timeToFirstAnswerToken`) already includes single-turn thinking time;
    `reasoning_tokens` is a multi-turn benchmark cumulative total and is never added to `ttft_s`."""
    del effort, reasoning_tokens
    cfg = responsiveness_config(policy)
    if output_tokens is None:
        output_tokens = float(cfg.get('output_tokens_per_turn', 400))
    tps = max(1e-6, float(output_tps))
    base_turn = max(0.0, float(ttft_s)) + max(0.0, float(output_tokens)) / tps
    return base_turn * harness_overhead_multiplier(harness, policy)


def turn_seconds_to_responsiveness(turn_seconds, policy=None):
    """Map per-turn duration in seconds onto 0..1 using `[fast_turn_seconds -> 1.0, slow_turn_seconds -> 0.0]`."""
    cfg = responsiveness_config(policy)
    fast = float(cfg.get('fast_turn_seconds', 4.0))
    slow = max(fast + 1e-6, float(cfg.get('slow_turn_seconds', 45.0)))
    return round(clamp((slow - float(turn_seconds)) / (slow - fast), 0.0, 1.0), 4)


def speed_telemetry_path(state_dir=None):
    base = Path(state_dir).expanduser() if state_dir else public_data.state_dir()
    return base / 'speed-telemetry.json'


def local_speed_telemetry(model, effort, base_responsiveness, policy=None, state_dir=None):
    """Optional local speed shrinkage once local samples reach `min_samples` (bounded by `max_blend` and `max_shift`).
    Missing or low-sample local telemetry leaves `base_responsiveness` untouched."""
    path = speed_telemetry_path(state_dir)
    try:
        if not path.is_file():
            return base_responsiveness, False
        doc = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return base_responsiveness, False
    models = doc.get('models') if isinstance(doc, dict) else None
    names = [model] if isinstance(model, str) else list(model or ())
    row = next((models[name] for name in names if isinstance(models.get(name), dict)), None) if isinstance(models, dict) else None
    if not isinstance(row, dict):
        return base_responsiveness, False
    by_effort = row.get('by_effort') if isinstance(row.get('by_effort'), dict) else {}
    target = by_effort.get(effort) if isinstance(by_effort.get(effort), dict) else row
    samples = target.get('samples', 0)
    if isinstance(samples, bool) or not isinstance(samples, (int, float)):
        return base_responsiveness, False
    cfg = responsiveness_config(policy).get('local_telemetry') or {}
    min_samples = max(1, int(cfg.get('min_samples', 20)))
    if samples < min_samples:
        return base_responsiveness, False
    local_resp = target.get('responsiveness')
    if isinstance(local_resp, bool) or not isinstance(local_resp, (int, float)):
        turn_s = target.get('median_turn_seconds', target.get('turn_seconds'))
        if isinstance(turn_s, bool) or not isinstance(turn_s, (int, float)):
            return base_responsiveness, False
        local_resp = turn_seconds_to_responsiveness(float(turn_s), policy)
    local_resp = clamp(float(local_resp), 0.0, 1.0)
    max_blend = clamp(float(cfg.get('max_blend', 0.3)), 0.0, 1.0)
    max_shift = clamp(float(cfg.get('max_shift', 0.15)), 0.0, 1.0)
    blend = min(max_blend, max_blend * min(1.0, float(samples) / float(min_samples * 2)))
    delta = clamp(blend * (local_resp - base_responsiveness), -max_shift, max_shift)
    return round(clamp(base_responsiveness + delta, 0.0, 1.0), 4), True


def _lookup_curve_responsiveness(curves_resp, effort, default_effort='high'):
    if not isinstance(curves_resp, dict) or not curves_resp:
        return None
    eff = str(effort or default_effort or 'high')
    if eff in curves_resp:
        val = curves_resp.get(eff)
        if isinstance(val, (int, float)) and not isinstance(val, bool) and not isnan(float(val)):
            return float(val)
    if len(curves_resp) == 1:
        only_val = next(iter(curves_resp.values()))
        if isinstance(only_val, (int, float)) and not isinstance(only_val, bool) and not isnan(float(only_val)):
            return float(only_val)
    if eff in ('high', 'xhigh', 'max', 'ultra'):
        fallbacks = ('high', 'default', 'max', 'xhigh')
    elif eff in ('none', 'minimal', 'low', 'default'):
        fallbacks = ('low', 'none', 'default', 'medium')
    else:
        fallbacks = ('medium', 'low', 'default', 'none', 'high')
    for fb in fallbacks:
        val = curves_resp.get(fb)
        if isinstance(val, (int, float)) and not isinstance(val, bool) and not isnan(float(val)):
            return float(val)
    return None


def dispatch_table(provider, accounts):
    """`dispatch_efforts` row for a launch provider. A second account reads its main family's row (claude-2 -> claude)."""
    tables = accounts.get('dispatch_efforts') if isinstance(accounts, dict) else None
    if not isinstance(tables, dict):
        return None
    name = str(provider or '')
    inventory = accounts.get('_discovery') if isinstance(accounts.get('_discovery'), dict) else {}
    spec = (accounts.get('accounts') or {}).get(name)
    for candidate in (name, ((inventory.get('launchers') or {}).get(name) or {}).get('family'),
                      ((inventory.get('accounts') or {}).get(name) or {}).get('family'),
                      account_family(spec) if isinstance(spec, dict) else None,
                      re.sub(r'[-_]\d+$', '', name)):
        if candidate and isinstance(tables.get(candidate), dict):
            return tables[candidate]
    return None


def dispatch_allowed_efforts(bundle, accounts, model=None):
    """Efforts the launch contract offers this bundle's model: None when the provider has no table at all (a gateway).
    A table that does not list the model leaves what the row itself lists, and nothing when it lists none, so a step-down
    never lands on an unverified flag."""
    table = dispatch_table((bundle or {}).get('provider') or (bundle or {}).get('account'), accounts)
    if table is None:
        return None
    raw = (bundle or {}).get('model')
    for name in (lookup_names(raw, accounts) if raw else [model]):
        if table.get(name):
            return tuple(table[name])
    own = (bundle or {}).get('efforts')
    return tuple(own) if isinstance(own, (list, tuple)) else ()


def model_pin_matches(task, bundle, model):
    """Does the task's named model (or bundle id) name this bundle? Compares the resolved key and the raw spelling."""
    named = task.get('constraint_model')
    if not named:
        return False
    raw = str((bundle or {}).get('model') or '')
    return (named in ((bundle or {}).get('id'), model, raw)
            or model_leaf(named).lower() == model_leaf(raw).lower())


def exact_pin(task, bundle, model):
    """The user named this bundle, or its model and effort: it keeps that effort and is only warned about when slow."""
    bid = (bundle or {}).get('id')
    if bid and bid in (task.get('pinned_bundle'), task.get('constraint_bundle'), task.get('constraint_model')):
        return True
    return bool(task.get('constraint_effort')) and model_pin_matches(task, bundle, model)


def bundle_named(task, bundle, model):
    return exact_pin(task, bundle, model) or model_pin_matches(task, bundle, model)


def has_fixed_effort(bundle, accounts):
    """`model:effort` and `model-effort` launch ids run at that level only."""
    if (bundle or {}).get('effort_fixed'):
        return True
    launch_id = str((bundle or {}).get('launch_model') or (bundle or {}).get('model') or '')
    known = accounts if isinstance(accounts, dict) and 'models' in accounts else {'models': {}}
    return split_model_effort(launch_id)[1] is not None or auto_bundles.split_effort(launch_id, known)[1] is not None


def can_step_down(bundle, info, accounts):
    """A row fixed at its level, or listing no level below its own, has nowhere to step."""
    if has_fixed_effort(bundle, accounts):
        return False
    own = bundle.get('efforts')
    if not isinstance(own, (list, tuple)):
        return True
    rank = _effort_rank(scoring_effort(bundle, info))
    return any(_effort_rank(level) < rank for level in own)


def named_effort_bundles(task, ctx):
    """Rows for levels the user's pin reaches on a model the account has no row at that level for: the effort they named,
    or, for a model named alone on `realtime_loop` work, every level that clears the floor, so the score decides among
    them exactly as it does among written rows. The launch contract is what offers a level; each row is the model's own
    carrier with a distinct id, so it never passes for the carrier itself."""
    effort = task.get('constraint_effort')
    level_choice = not effort and bool(task.get('realtime_loop') and task.get('constraint_model'))
    if (not effort and not level_choice) or task.get('constraint_bundle'):
        return {}
    accounts, groups = ctx['accounts'], {}
    for bundle in ctx['bundles'].values():
        model = row_key(bundle['model'], accounts)
        if task.get('constraint_model') and not model_pin_matches(task, bundle, model):
            continue
        if level_choice and exact_pin(task, bundle, model):
            continue
        groups.setdefault((bundle['account'], bundle.get('harness'), model), []).append(bundle)
    found = {}
    for (_, _, model), rows in groups.items():
        written = {str(scoring_effort(r, model_spec(accounts, r['model'])) or '').lower() for r in rows}
        carriers = [r for r in rows if (not r.get('disabled') or r.get('disabled_by_default'))
                    and not r.get('discovery_unavailable') and not has_fixed_effort(r, accounts)]
        # Each level goes to the first row that offers it, so the answer does not depend on the order of the rows.
        for carrier in carriers:
            allowed = dispatch_allowed_efforts(carrier, accounts, model)
            if effort:
                levels = [effort]
                if (effort not in allowed) if allowed is not None else not task.get('constraint_model'):
                    levels = []
            else:
                levels = [e for e in clearing_levels(carrier, ctx) if allowed is None or e in allowed]
            for level in levels:
                if level in written or (isinstance(carrier.get('efforts'), list) and level not in carrier['efforts']):
                    continue
                written.add(level)
                row_id = f"{carrier['id']}@{level}"
                found[row_id] = {**{k: v for k, v in carrier.items() if k != 'when'}, 'id': row_id, 'effort': level,
                                 'pin_carrier': carrier['id']}
    return found


def clearing_levels(bundle, ctx):
    """The levels in this model's speed curve at or above the realtime floor. A level has no curve to clear on a harness
    that does not step, or on a name with no measured curve of its own."""
    accounts, policy = ctx['accounts'], ctx['policy']
    harness = bundle.get('harness') or bundle_harness(bundle, bundle.get('account'), bundle.get('id'))
    provider = bundle.get('provider') or bundle.get('account')
    if harness in ('antigravity', 'antigravity-resilient') or provider in ('antigravity', 'antigravity-resilient'):
        return []
    curves, info = model_curves(accounts, bundle['model']), model_spec(accounts, bundle['model'])
    speeds = curves.get('responsiveness')
    if not isinstance(speeds, dict):
        return []
    floor = float(responsiveness_config(policy)['floor'])
    ability = curves.get('ability')
    levels = []
    for level in REALTIME_EFFORT_ORDER:
        if level not in speeds or (isinstance(ability, dict) and ability and level not in ability):
            continue
        value, source = bundle_responsiveness(bundle, info, policy, curves, state_dir=ctx.get('state_dir'), effort=level,
                                              harness=harness, accounts=accounts)
        if source != 'not_applicable' and value is not None and value >= floor:
            levels.append(level)
    return levels


def bundle_responsiveness(bundle, info, policy=None, curves=None, state_dir=None, effort=None, harness=None, accounts=None):
    """`(responsiveness_0_to_1, source)` for `bundle` at `effort` (or its scoring effort).
    `source` is `'measured'`, `'local_blended'`, `'default'` (when community speed data is absent),
    or `'not_applicable'` (for special web/packet channels such as `grok-bot` and `dot`)."""
    if isinstance(info, dict) and isinstance(info.get('spec'), dict):
        info = info['spec']
    if isinstance(policy, dict) and ('models' in policy or 'accounts' in policy) and 'scoring' in policy:
        accounts = policy
        policy = (curves if isinstance(curves, dict) and ('ability_gate' in curves or 'responsiveness' in curves)
                  else accounts.get('scoring'))
        curves = model_curves(accounts, str((bundle or {}).get('model') or ''))
    cfg = responsiveness_config(policy)
    info = info if isinstance(info, dict) else {}
    info_resp = info.get('responsiveness')
    if info_resp == 'not_applicable' or info.get('responsiveness_status') == 'not_applicable':
        return None, 'not_applicable'
    eff = effort or scoring_effort(bundle or {}, info) or info.get('default_effort') or 'high'
    harness = harness or (bundle or {}).get('harness') or bundle_harness(
        bundle or {}, (bundle or {}).get('account'), (bundle or {}).get('id'))
    curves_resp = (curves or {}).get('responsiveness') if isinstance(curves, dict) else None
    default_eff = info.get('default_effort') or 'high'
    measured = _lookup_curve_responsiveness(curves_resp, eff, default_eff)
    if measured is None:
        if isinstance(info_resp, dict):
            measured = _lookup_curve_responsiveness(info_resp, eff, default_eff)
        elif (isinstance(info_resp, (int, float)) and not isinstance(info_resp, bool)
              and not isnan(float(info_resp)) and not info.get('score_estimated')
              and eff == default_eff):
            measured = float(info_resp)
    if measured is None and isinstance(info.get('speed_metrics'), dict) and eff == default_eff:
        sm = info['speed_metrics']
        ttft = sm.get('ttft_s')
        tps = sm.get('output_tps')
        if (isinstance(ttft, (int, float)) and not isinstance(ttft, bool)
                and isinstance(tps, (int, float)) and not isinstance(tps, bool) and tps > 0):
            turn_s = compute_turn_seconds(
                ttft, tps, effort=eff,
                output_tokens=sm.get('output_tokens_per_turn'),
                harness=harness, policy=policy,
            )
            measured = turn_seconds_to_responsiveness(turn_s, policy)
    if measured is None:
        base_val = clamp(float(cfg.get('default', 0.52)), 0.0, 1.0)
        source = 'default'
    else:
        base_val = clamp(measured, 0.0, 1.0)
        source = 'measured'
    names = lookup_names(str((bundle or {}).get('model') or ''), accounts)
    adjusted, blended = local_speed_telemetry(names, eff, base_val, policy=policy, state_dir=state_dir)
    if blended:
        return adjusted, 'local_blended'
    return round(base_val, 4), source


def cooling_reason(bundle, entries, accounts, now=None):
    """launch_failures.reason for this row or any entry recorded under another spelling of its model: one model has one
    cooldown, and a family guess is its own model."""
    found = launch_failures.reason(bundle, entries, now=now)
    if found or not entries:
        return found
    own = row_key(bundle.get('model') or '', accounts)
    prefix = launch_failures.key(bundle.get('account'), '')
    for entry in entries:
        if entry.startswith(prefix) and row_key(entry[len(prefix):], accounts) == own:
            return launch_failures.reason({**bundle, 'model': entry[len(prefix):]}, entries, now=now)
    return None


def resolve_realtime_effort(bundle, task_or_info, ctx_or_accounts=None, model_or_policy=None,
                            info=None, policy=None, curves=None, gate=0.0, *,
                            harness=None, all_bundles=None, task=None):
    """Evaluate `bundle` at its configured effort on a `realtime_loop` task, stepping down to a faster effort in
    `curves['responsiveness']` when its configured effort is below `floor`, the bundle's provider/harness supports
    that effort in `dispatch_efforts`, and no lower-effort sibling bundle on the same `(account, harness,
    canonical_model)` already clears both `floor` and `gate`.

    Returns `(effective_effort, resp_val, resp_source, stepped_from)`."""
    if info is None and (
        (isinstance(ctx_or_accounts, dict) and ('models' in ctx_or_accounts or 'accounts' in ctx_or_accounts))
        or (isinstance(model_or_policy, dict) and ('models' in model_or_policy or 'accounts' in model_or_policy))
    ):
        if isinstance(ctx_or_accounts, dict) and ('models' in ctx_or_accounts or 'accounts' in ctx_or_accounts):
            accounts = ctx_or_accounts
            policy = model_or_policy if isinstance(model_or_policy, dict) else (accounts.get('scoring') or {})
        else:
            accounts = model_or_policy
            policy = ctx_or_accounts if isinstance(ctx_or_accounts, dict) else (accounts.get('scoring') or {})
        info = (task_or_info.get('spec') if isinstance(task_or_info, dict) and isinstance(task_or_info.get('spec'), dict)
                else task_or_info)
        model = canonical((bundle or {}).get('model') or '', accounts)
        curves = model_curves(accounts, (bundle or {}).get('model') or '')
        if isinstance(all_bundles, dict):
            bundles_map = {bid: ({**b, 'id': b.get('id') or bid} if isinstance(b, dict) else b)
                           for bid, b in all_bundles.items()}
        elif isinstance(all_bundles, (list, tuple)):
            bundles_map = {b.get('id', f'b{i}'): b for i, b in enumerate(all_bundles) if isinstance(b, dict)}
        else:
            bundles_map = bundles(accounts)
        ctx = {'accounts': accounts, 'policy': policy, 'bundles': bundles_map}
        task = task if isinstance(task, dict) else {'realtime_loop': True}
    else:
        task = task_or_info if isinstance(task_or_info, dict) else {}
        ctx = ctx_or_accounts if isinstance(ctx_or_accounts, dict) else {}
        accounts = ctx.get('accounts') or {}
        model = model_or_policy

    state_dir = ctx.get('state_dir')
    raw_effort = scoring_effort(bundle, info)
    eff_harness = harness or bundle.get('harness') or bundle_harness(bundle, bundle.get('account'), bundle.get('id'))
    resp_val, resp_source = bundle_responsiveness(
        bundle, info, policy, curves, state_dir=state_dir, effort=raw_effort, harness=eff_harness, accounts=accounts)
    if resp_source == 'not_applicable' or resp_val is None:
        return raw_effort, resp_val, resp_source, None
    resp_floor = float(responsiveness_config(policy)['floor'])
    if resp_val >= resp_floor:
        return raw_effort, resp_val, resp_source, None
    curves_resp = (curves or {}).get('responsiveness') if isinstance(curves, dict) else None
    if not isinstance(curves_resp, dict) or not curves_resp:
        return raw_effort, resp_val, resp_source, None

    provider = bundle.get('provider') or bundle.get('account')
    if eff_harness in ('antigravity', 'antigravity-resilient') or provider in ('antigravity', 'antigravity-resilient'):
        return raw_effort, resp_val, resp_source, None
    if has_fixed_effort(bundle, accounts):
        return raw_effort, resp_val, resp_source, None
    own_model = row_key(bundle.get('model') or model or '', accounts)
    # A named effort stays as named. A named model alone still chooses among its efforts like any other candidate.
    if exact_pin(task, bundle, own_model):
        return raw_effort, resp_val, resp_source, None
    slowest_pass = bool(task.get('slow_pin_fallback')) and model_pin_matches(task, bundle, own_model)

    unavailable_ids = set(ctx.get('unavailable') or ())
    failures_map = ctx.get('launch_failures')
    siblings = []
    user_disabled = set()
    for bid, b in (ctx.get('bundles') or {}).items():
        if not isinstance(b, dict):
            continue
        sib_id = b.get('id') or bid
        if b.get('account') != bundle.get('account'):
            continue
        if (b.get('harness') or bundle_harness(b, b.get('account'), sib_id)) != eff_harness:
            continue
        if row_key(b.get('model') or '', accounts) != own_model:
            continue
        if b.get('disabled') and not b.get('disabled_by_default'):
            user_disabled.add(str(scoring_effort(b, info) or '').lower())
        if sib_id in unavailable_ids:
            continue
        if b.get('disabled') or b.get('discovery_unavailable') or not bundle_applies(b, task):
            continue
        sib_bundle = {**b, 'id': sib_id}
        if cooling_reason(sib_bundle, failures_map, accounts, now=ctx.get('now')) or \
                model_latch.reason(sib_bundle, ctx.get('model_latches')) or \
                provider_retry.reason(sib_bundle, ctx.get('provider_retries')):
            continue
        siblings.append(sib_bundle)
    self_id = bundle.get('id')
    if not slowest_pass:
        for sib in siblings:
            if (self_id and sib.get('id') == self_id) or (not self_id and sib.get('effort') == bundle.get('effort')):
                continue
            sib_eff = scoring_effort(sib, info)
            sib_resp, sib_src = bundle_responsiveness(
                sib, info, policy, curves, state_dir=state_dir, effort=sib_eff, harness=eff_harness, accounts=accounts)
            sib_ab = effort_ability(policy, curves, info, sib_eff)
            if sib_src != 'not_applicable' and sib_resp is not None and sib_resp >= resp_floor and sib_ab >= gate:
                return raw_effort, resp_val, resp_source, None
        # A row that cannot step does not stand in for the rows that can.
        steppable = [s for s in siblings if can_step_down(s, info, accounts)]
        if steppable:
            step_down_peers = [
                s for s in steppable
                if effort_ability(policy, curves, info, scoring_effort(s, info)) >= gate
            ] or steppable
            min_rank = min(_effort_rank(scoring_effort(s, info)) for s in step_down_peers)
            if _effort_rank(raw_effort) > min_rank:
                return raw_effort, resp_val, resp_source, None

    # The levels this candidate can run at: the launch contract, what the row itself offers, minus what the user unchecked.
    allowed_efforts = dispatch_allowed_efforts(bundle, accounts, model)
    own_efforts = bundle.get('efforts') if isinstance(bundle.get('efforts'), (list, tuple)) else None
    ability_curve = (curves or {}).get('ability') if isinstance(curves, dict) else None
    raw_rank = _effort_rank(raw_effort)
    faster = [
        eff for eff in REALTIME_EFFORT_ORDER
        if eff in curves_resp
        and _effort_rank(eff) < raw_rank
        and (not (isinstance(ability_curve, dict) and ability_curve) or eff in ability_curve)
        and (allowed_efforts is None or eff in allowed_efforts)
        and (own_efforts is None or eff in own_efforts)
        and eff not in user_disabled
    ]
    faster.sort(key=_effort_rank, reverse=True)
    best_clearing_both = None
    best_clearing_resp = None
    timed = []
    for cand_eff in faster:
        cand_resp, cand_src = bundle_responsiveness(
            bundle, info, policy, curves, state_dir=state_dir, effort=cand_eff, harness=eff_harness, accounts=accounts)
        if cand_src == 'not_applicable' or cand_resp is None:
            continue
        timed.append((cand_resp, cand_eff, cand_src))
        if cand_resp < resp_floor:
            continue
        if best_clearing_resp is None:
            best_clearing_resp = (cand_eff, cand_resp, cand_src)
        if effort_ability(policy, curves, info, cand_eff) >= gate:
            best_clearing_both = (cand_eff, cand_resp, cand_src)
            break
    chosen = best_clearing_both or best_clearing_resp
    if chosen is not None:
        return chosen[0], chosen[1], chosen[2], raw_effort
    if slowest_pass and timed:
        # Nothing clears the floor on the model the user named: its fastest level, warned about by the caller.
        able = [row for row in timed if effort_ability(policy, curves, info, row[1]) >= gate] or timed
        best = max(able, key=lambda row: row[0])
        if best[0] > resp_val:
            return best[1], best[0], best[2], raw_effort
    return raw_effort, resp_val, resp_source, None


def task_demand(profile, difficulty, critical, divergence, policy, high_precision=False, floor=None):
    """The task's continuous ability demand: the difficulty's floor (plus critical headroom) and an extra that grows with
    reasoning depth times the mean difficulty of the task's properties (code, context, divergence, hallucination risk,
    error cost). A candidate saturates the demand when its effective ability (base tier x its effort's thinking
    coefficient) reaches `required_ability`; the saturation interval runs one `saturation_span` above it."""
    cfg = demand_config(policy)
    floor = float(policy['ability_floor'][difficulty]) if floor is None else float(floor)
    headroom = critical_headroom(policy, difficulty) if critical else 0.0
    precision = float(cfg['precision_headroom']) if high_precision and not critical else 0.0
    span = float((policy.get('profile') or {}).get('saturation_span', 0.3))
    if profile:
        depth = profile['reasoning_depth']
        levels = {'code_complexity': profile['code_complexity'] / 3.0,
                  'context_scale': (profile['context_scale'] - 1) / 2.0,
                  'divergence': (divergence - 1) / 2.0,
                  'hallucination_risk': (profile['hallucination_risk'] - 1) / 2.0,
                  # error cost 3 is critical work, which already carries `critical_headroom`
                  'error_cost': float(min(profile['error_cost'], 2) - 1)}
        weights = cfg['property_weights']
        total = sum(weights.get(k, 0.0) for k in levels)
        load = sum(weights.get(k, 0.0) * v for k, v in levels.items()) / total if total else 0.0
        source = 'profile'
    else:
        depth = DIFFICULTY_ORDER.index(difficulty) + 1 if difficulty in DIFFICULTY_ORDER else 2
        levels, load, source = {}, float(cfg['default_load']), 'difficulty'
    extra = float(cfg['property_span']) * (depth / 4.0) * load
    required = floor + headroom + precision + extra
    return {'required_ability': round(required, 4), 'floor': floor, 'critical_headroom': headroom, 'precision_headroom': precision,
            'property_extra': round(extra, 4), 'reasoning_depth': depth, 'property_load': round(load, 4),
            'properties': {k: round(v, 3) for k, v in levels.items()}, 'saturation_span': span,
            'saturates_at': round(required + span, 4), 'source': source}


def effort_scaled_capabilities(info, policy, curves, effort):
    """capability_vector with the thinking dimensions multiplied by this effort's coefficient: effective tier over the
    effort-neutral High tier. Perception (visual, gui) does not improve by thinking longer."""
    caps = capability_vector(info)
    base = effort_ability(policy, curves, info, 'high')
    ratio = effort_ability(policy, curves, info, effort) / base if base > 0 else 1.0
    return {dim: clamp(v * ratio, 0.0, 1.0) if dim in EFFORT_SCALED_CAPABILITIES else v for dim, v in caps.items()}


def saturating_effort(policy, curves, info, required, efforts=EFFORT_ORDER):
    """Lowest effort on the model's own curve whose effective ability reaches `required` (None when none does)."""
    for effort in efforts:
        if effort_ability(policy, curves, info, effort) >= required - 1e-9:
            return effort
    return None


def profile_requirements(profile, policy):
    """{dimension: (required level, weight)} for the dimensions the profile needs (accounts `scoring.profile`)."""
    config = policy.get('profile') or {}
    table, weights = config['requirements'], config.get('requirement_weights') or {}
    # Depth multiplies every property's difficulty: the same code or context load asks more of the model in a deeper
    # task. Depth 2 is the table's own scale; the reasoning level is already indexed by depth.
    amp = 1.0 + demand_config(policy)['depth_coupling'] * (profile['reasoning_depth'] - 2)

    def need(level):
        return round(min(0.98, level * amp), 4)

    out = {'reasoning': (table['reasoning'][str(profile['reasoning_depth'])], weights.get('reasoning', 1.0))}
    if profile['code_complexity']:
        out['coding'] = (need(table['coding'][str(profile['code_complexity'])]), weights.get('coding', 1.0))
    explore = 'exploration_style' if profile['interaction_style'] == 'exploration' else 'exploration'
    out['exploration'] = (need(table['exploration'][str(profile['context_scale'])]), weights.get(explore, 0.5))
    risky = 'anti_hallucination_high' if profile['hallucination_risk'] >= 3 else 'anti_hallucination'
    out['anti_hallucination'] = (need(table['anti_hallucination'][str(profile['hallucination_risk'])]), weights.get(risky, 0.5))
    if profile['needs_visual'] or profile['needs_gui']:
        out['visual'] = (table['visual'], weights.get('visual', 1.0))
    if profile['needs_gui']:
        out['gui'] = (table['gui'][str(profile['reasoning_depth'])], weights.get('gui', 1.5))
    return out


def vector_fit(requirements, caps):
    """Weighted mean of min(1, capability / requirement): meeting a need is full marks, exceeding it earns nothing."""
    total = sum(weight for _, weight in requirements.values())
    if not total:
        return 1.0
    return sum(weight * (1.0 if need <= 0 else min(1.0, caps.get(dim, 0.0) / need))
               for dim, (need, weight) in requirements.items()) / total


def subjective_vector(info):
    """The model's three subjective capability ratings (`divergence`, `global_rigor`, `module_precision`) on 0..1."""
    raw = (info or {}).get('subjective') or {}
    return {dim: clamp(float(raw.get(dim, 0.5)), 0.0, 1.0) for dim in SUBJECTIVE_DIMENSIONS}


def vision_tools_score(info):
    """Fourth radar capability: explicit rating, otherwise vision/GUI baseline."""
    subjective = (info or {}).get('subjective') or {}
    caps = (info or {}).get('capabilities') or {}
    def rating(value):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.0
        return clamp(number, 0.0, 1.0) if not isnan(number) else 0.0
    if subjective.get('capability') is not None:
        return rating(subjective['capability'])
    return round(max(rating(caps.get('visual')), rating(caps.get('gui'))), 2)


SUBJECTIVE_DIVERGENCE_SHARE = 0.4    # the divergence share a multi-hypothesis (divergence 2) task adds to its convergent blend


def convergent_weights(depth, code, scale, style):
    """module_precision / global_rigor blend for convergent work. Global rigor grows with scope and depth in steps, so no
    one-level change in context_scale flips the task between the two (2026-10-27: scale 1 -> 2 used to swing from 100%
    module precision to 100% global rigor). Heavy global rigor (>= 0.8) is kept for depth >= 3 with scope, scale 3 or
    code 3."""
    if code == 3 or scale == 3 or (depth >= 3 and scale >= 2):
        g = 0.85
    elif depth >= 3 or code == 0:
        g = 0.75 if scale >= 2 else 0.65          # read-only review, audit, diagnosis, analysis: global rigor leads
    elif code >= 1 and style != 'exploration':
        g = 0.25 if scale >= 2 else 0.0           # production code: module precision leads
    else:
        g = 0.65 if scale >= 2 else 0.5           # research packet scripts / quantitative exploration
    return {k: v for k, v in (('module_precision', round(1 - g, 2)), ('global_rigor', g)) if v > 0}


def subjective_weights(profile, kind=None):
    """Weights over `SUBJECTIVE_DIMENSIONS` implied by a task profile without expanding route categories. They sum to 1,
    so `subjective_fit` never inflates a partial weight into a whole dimension."""
    if not isinstance(profile, dict):
        return {}
    depth = profile['reasoning_depth']
    code = profile['code_complexity']
    scale = profile['context_scale']
    style = profile['interaction_style']
    is_gui = kind == 'computer_use' or bool(profile.get('needs_gui'))
    div = min(profile.get('divergence', 1), 2) if is_gui else profile.get('divergence', 1)
    if depth == 1 and div == 1 and scale <= 2 and code <= 2:
        return {}
    if div >= 3 or kind == 'direction_reasoning':
        g = 0.15 if scale >= 2 else 0.05
        return {'divergence': round(1 - g, 2), 'global_rigor': g}
    if kind == 'frontend_implementation':
        return {'global_rigor': 0.65, 'divergence': 0.35}
    base = convergent_weights(depth, 0 if is_gui else code, scale, style)
    if div == 2 or kind == 'complex_build_plan':     # multi-hypothesis diagnosis or build plans rely on global rigor + divergence
        g = max(0.75, base.get('global_rigor', 0.0))
        base = {k: v for k, v in (('module_precision', round(1 - g, 2)), ('global_rigor', g)) if v > 0}
        share = SUBJECTIVE_DIVERGENCE_SHARE
        return {'divergence': share, **{k: round(v * (1 - share), 3) for k, v in base.items()}}
    return base


def subjective_fit(weights, subj):
    """Weighted mean of the model's subjective scores over the task's active subjective dimensions."""
    total = sum(weights.values())
    if not total:
        return 1.0
    return sum(w * subj.get(dim, 0.5) for dim, w in weights.items()) / total


# --- Five-axis path (round 047, off by default) ------------------------------------------------------------------------
# `scoring.five_axis.enabled` turns it on. Model data then comes from references/five-axis-scores.json
# (calibration/five_axis.py, cited sources only). User decisions of 2026-11-09:
# - One dimension system (round 7). A task demands levels 0..3 on the four capability axes (the classifier's
#   `axis_demand`; without it, the fallback mapping below) and nothing else scores or gates on quality: no scalar tier,
#   no subjective or capability vectors, no route preference or restriction lists, no harness quality factors, no
#   difficulty floors, no free-usage bonus. Benchmarks and boards are only components of the axes.
# - Quality is the continuous fit of the model's axis scores (thinking axes scaled by its effort curve) to the demand
#   (five_axis_fit): the demand-weighted mean of the scores, minus `shortfall_weight` x the shortfall below each
#   demanded axis's target (the anchor model's value x demand / 3). No floor excludes a model; the economy decides.
# - Missing data (rule B): a demanded axis with no data at all, or no cited usd on a billed account, leaves automatic
#   routing; a named model still runs, with the data error as a warning. With availability and holds, that is the
#   only exclusion.
# - Plan capacity is the cited figure of the capacity ladder (community, time-to-exhaust, vendor multiplier), priced at
#   the merged usd scale; a window with an empty ladder is a data error that still prices scarcity from its live
#   percentage and reset time.
# - Economy (round 6, user): no overqualification term. Every long window charges the candidate's burn (option-B usd
#   over the window's capacity) times a pacing shadow price that is not zero while the window is far from empty
#   (five_axis_pacing), counted in hours of the window's reference model at score_per_displaced_worker_hour.
FIVE_AXIS_FILE = REFERENCES / 'five-axis-scores.json'
FIVE_AXES = ('divergence', 'global_rigor', 'module_precision', 'vision_tools')
FIVE_AXIS_THINKING = ('divergence', 'global_rigor', 'module_precision')
# effort_curves basis: an `ability` step is the AA Intelligence Index change times 0.0495 tier per index point. Without
# a tier, the same step is a relative change of the model's own index.
FIVE_AXIS_INDEX_PER_TIER = 1 / 0.0495
FIVE_AXIS_ANCHOR_VALUE = 0.8     # the anchor model's value on every axis (calibration/measurements.py ANCHOR_VALUE)
FIVE_AXIS_DEFAULTS = {
    'weights': {'fit': 0.4, 'ability': 0.0, 'continuity': 0.25},
    'demanded_level': 1,         # rule B: an axis demanded at this level or above must have data
    'shortfall_weight': 1.0,     # extra fit lost per point of axis score below the demanded target (round 7)
    'pace': True,                # the pacing cost of each long window (round 6); False = shortage-only, for audits
    'pace_smoothing': 0.1,       # k in the pacing price's (used + k) / (elapsed + k): keeps the period start finite
}
FIVE_AXIS_DEMAND_SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': list(FIVE_AXES),
    'description': ('How much the task needs each capability axis of the model, 0..3. Economy is not a task demand: '
                    'the router prices it from cost and live quota.'),
    'properties': {
        'divergence': {'type': 'integer', 'minimum': 0, 'maximum': 3, 'description': (
            'Divergent and exploratory thinking. 0 = execute a known spec or checklist; 1 = small judgment calls; '
            '2 = several hypotheses or options to test or compare, open exploration of a codebase or data; '
            '3 = open-ended creative or first-principles design, new schemes, ideation.')},
        'global_rigor': {'type': 'integer', 'minimum': 0, 'maximum': 3, 'description': (
            'Rigor across the whole problem: long context, long horizon, no fabricated claims. 0 = trivial and '
            'mechanically checked; 1 = one module or a result a test verifies; 2 = several coupled parts, or claims no '
            'test checks (review, audit, metrics, research synthesis); 3 = whole-system or long multi-stage work, or a '
            'missed defect is costly or irreversible.')},
        'module_precision': {'type': 'integer', 'minimum': 0, 'maximum': 3, 'description': (
            'Precise code the worker itself writes (as code_complexity). 0 = writes no production code; 1 = a small '
            'script or config change; 2 = a production module with its tests; 3 = multi-module, concurrency, protocol '
            'or core-library changes.')},
        'vision_tools': {'type': 'integer', 'minimum': 0, 'maximum': 3, 'description': (
            'Vision, GUI and tool use beyond reading files and running shell commands. 0 = text and code only; '
            '1 = heavy tool or API orchestration, browser fetches; 2 = must read images or screenshots; 3 = must '
            'operate a desktop or browser GUI through screenshots.')},
    },
}


def five_axis_config(policy):
    raw = (policy or {}).get('five_axis')
    if not isinstance(raw, dict) or not raw.get('enabled'):
        return None
    return {**FIVE_AXIS_DEFAULTS, **raw, 'weights': {**FIVE_AXIS_DEFAULTS['weights'], **(raw.get('weights') or {})}}


def five_axis_scorer_enabled(accounts=None):
    """True when the classifier should also answer `axis_demand` (only while the five-axis path is on)."""
    try:
        return five_axis_config((accounts or load_accounts()).get('scoring')) is not None
    except Exception:       # a broken or missing accounts file keeps the classifier schema unchanged
        return False


def load_five_axis(path=None):
    return json.loads(Path(path or FIVE_AXIS_FILE).read_text(encoding='utf-8'))


def validate_axis_demand(raw):
    if not isinstance(raw, dict) or set(raw) != set(FIVE_AXES):
        raise ValueError('task.profile.axis_demand must give exactly ' + ', '.join(FIVE_AXES))
    out = {}
    for axis in FIVE_AXES:
        value = raw[axis]
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 3:
            raise ValueError(f'task.profile.axis_demand.{axis} must be an integer from 0 to 3')
        out[axis] = value
    return out


def five_axis_overlay(accounts, data=None):
    """A copy of `accounts` carrying the five-axis data, or `accounts` itself when the path is off.

    Each model row gets `five_axis` {scores, missing, index, usd_status}; usd_per_hour and duration_multiplier become
    the merged cited values (an unsourced usd is removed, not defaulted). The scalar tier is left in place but the
    five-axis path does not read it. Each windowed account's capacity is the data's community real-usage figure for
    its plan and window (round 4), replacing the packaged figure and any dated change; a window without one is marked
    unknown, with no template fallback."""
    cfg = five_axis_config(accounts.get('scoring'))
    if cfg is None:
        return accounts
    import copy
    data = data if data is not None else load_five_axis(cfg.get('path'))
    out = copy.deepcopy(accounts)
    rows = data.get('models') or {}
    indexes = [r.get('intelligence_index') for r in rows.values() if isinstance(r.get('intelligence_index'), (int, float))]
    median_index = statistics.median(indexes) if indexes else 45.0
    for name, info in (out.get('models') or {}).items():
        if not isinstance(info, dict):
            continue
        row = rows.get(name)
        usd = ((row or {}).get('usd_per_hour') or {}).get('value')
        info['five_axis'] = {'scores': {a: float(((row or {}).get('scores') or {}).get(a) or 0.0) for a in FIVE_AXES},
                             'missing': list(FIVE_AXES) if row is None else list(row.get('missing_axes') or ()),
                             'index': (row or {}).get('intelligence_index') or median_index,
                             'usd_status': 'ok' if usd is not None else 'missing'}
        if usd is not None:
            info['usd_per_hour'] = usd
        else:
            info.pop('usd_per_hour', None)
        duration = ((row or {}).get('duration_multiplier') or {}).get('value')
        if duration is not None:
            info['duration_multiplier'] = float(duration)
    community = data.get('capacity') or {}
    for name, spec in (out.get('accounts') or {}).items():
        if not isinstance(spec, dict) or not spec.get('windows') or spec.get('unmetered'):
            continue
        family = str(provider_inventory.account_family(name, out, out.get('_discovery') or {}) or account_family(spec) or '')
        family = family.removesuffix('-resilient')
        plan = provider_inventory.configured_plan(family, spec.get('plan'))
        if family == 'pi':      # Pi signs in with ChatGPT and spends the Codex account's allowance: its plan's windows
            family, plan = 'codex', next((provider_inventory.configured_plan('codex', s.get('plan'))
                                          for n, s in (out.get('accounts') or {}).items() if isinstance(s, dict)
                                          and str(provider_inventory.account_family(n, out, out.get('_discovery') or {})
                                                  or account_family(s) or '') == 'codex'), plan)
        rows_ = (community.get(family) or {}).get(plan) or {}
        caps = {wid: float(rows_[wid]['capacity_usd']) for wid in spec['windows']
                if isinstance((rows_.get(wid) or {}).get('capacity_usd'), (int, float))}
        spec['capacity_usd'] = caps
        spec.pop('capacity_change', None)       # the community measurement is the capacity in effect when measured
        # Displaced work is counted in hours of the window's reference model (round 6), at its merged usd.
        spec['reference_usd_per_hour'] = {
            wid: float(v) for wid in spec['windows']
            if isinstance(v := (((rows.get((rows_.get(wid) or {}).get('reference_model') or '') or {}).get('usd_per_hour')
                                 or {}).get('value')), (int, float)) and v > 0}
        spec['capacity_source'] = {wid: {'status': (rows_.get(wid) or {}).get('status', 'missing'),
                                         'age_days': (rows_.get(wid) or {}).get('age_days'), 'plan': plan or None}
                                   for wid in spec['windows']}
        unknown = [wid for wid in spec['windows'] if wid not in caps]
        if unknown:
            spec['capacity_unknown'] = True
            # Nor the local percent-per-hour priors: an unknown window is priced from its live percentage only.
            burn = {w: r for w, r in (spec.get('burn_pct_per_worker_hour') or {}).items() if w not in unknown}
            spec['burn_pct_per_worker_hour'] = burn
            for info in (out.get('models') or {}).values():
                if isinstance(info, dict) and isinstance(info.get('burn_pct_per_worker_hour'), dict):
                    info['burn_pct_per_worker_hour'] = {w: r for w, r in info['burn_pct_per_worker_hour'].items()
                                                        if w not in unknown}
    anchor = data.get('anchor') or {}
    out['_five_axis'] = {'generated_at': data.get('generated_at'), 'anchor': anchor.get('model'),
                         'anchor_value': float(anchor.get('value') or FIVE_AXIS_ANCHOR_VALUE), 'median_index': median_index}
    return out


def five_axis_fallback_levels(task):
    """Axis demand levels when the classifier gave none: from the profile fields, or the work kind's traits without a
    profile. Only a fallback; the classifier answers `axis_demand` itself while the five-axis path is on."""
    prof = task.get('profile') or {}
    if prof:
        depth, code, scale = prof['reasoning_depth'], prof['code_complexity'], prof['context_scale']
        risk, cost, style = prof['hallucination_risk'], prof['error_cost'], prof['interaction_style']
        visual, gui = bool(prof.get('needs_visual')), bool(prof.get('needs_gui'))
    else:
        code, scale, risk, cost, style = KIND_TRAITS.get(task.get('kind'), KIND_TRAITS['other_or_mixed'])
        depth = DEPTH_BY_DIFFICULTY.get(task.get('difficulty'), 2)
        visual, gui = bool(task.get('needs_visual')), bool(task.get('needs_gui'))
    div = int(task.get('divergence') or prof.get('divergence') or 1)
    kind = task.get('kind') or prof.get('work_kind')
    return {
        'divergence': min(3, div - 1 + (1 if style in ('exploration', 'advisory') else 0)),
        'global_rigor': min(3, max(1, depth - 1, risk - 1, scale - 1) + (1 if cost >= 3 else 0)),
        'module_precision': int(code),
        'vision_tools': 3 if gui else 2 if visual else 1 if kind in ('tool_execution', 'computer_use') else 0,
    }


def five_axis_demand(task):
    """({axis: level 0..3}, source): the classifier's axis_demand, else the fallback mapping. An all-zero answer gives
    no fit, so it is replaced by the fallback and labelled as such (review F5), never as 'classifier'."""
    given = (task.get('profile') or {}).get('axis_demand')
    fallback = 'profile_fields_fallback' if task.get('profile') else 'kind_traits_fallback'
    if given and any(given.values()):
        return dict(given), 'classifier'
    demand = five_axis_fallback_levels(task)
    source = f'{fallback} (classifier axis_demand all zero)' if given else fallback
    if not any(demand.values()):        # a trivial task still asks the least rigor, so a fit exists
        demand['global_rigor'] = 1
        source += ' + minimum global_rigor 1'
    return demand, source


def five_axis_effort_ratio(info, policy, curves, effort):
    """How much the thinking axes scale at `effort` relative to High: the curve's `ability_mult` ratio, else its
    additive tier step read as a change of the model's own AA index (effort_curves basis, 0.0495 tier per point)."""
    mult = (curves or {}).get('ability_mult') or {}
    if effort in mult and mult.get('high'):
        return float(mult[effort]) / float(mult['high'])
    index = float(((info or {}).get('five_axis') or {}).get('index') or 45.0)
    step = effort_value(policy, curves, 'ability', effort, 0) - effort_value(policy, curves, 'ability', 'high', 0)
    return max(0.0, 1.0 + step * FIVE_AXIS_INDEX_PER_TIER / index)


def five_axis_level(scores, demand, ratio=1.0):
    """(demand-weighted mean of the axis scores, {axis: effort-scaled score}); thinking axes scale by `ratio`, capped at 1."""
    total = sum(w for w in demand.values() if w > 0)
    eff = {a: clamp(float(scores.get(a, 0.0)) * (ratio if a in FIVE_AXIS_THINKING else 1.0), 0.0, 1.0) for a in FIVE_AXES}
    if not total:
        return 0.0, eff
    return sum(w * eff[a] for a, w in demand.items() if w > 0) / total, eff


def five_axis_targets(demand, anchor_value=FIVE_AXIS_ANCHOR_VALUE):
    """{axis: target score} for every demanded axis: the anchor model's value x demand / 3, so demand 3 asks for the
    anchor's level and demand 1 a third of it. Scores are anchor-relative at the source, so the targets do not move
    when a new model tops a board."""
    return {a: round(anchor_value * w / 3.0, 4) for a, w in demand.items() if w > 0}


def five_axis_fit(scaled, demand, targets, shortfall_weight=1.0):
    """(fit, {axis: row}): the demand-weighted mean over the demanded axes of score - shortfall_weight x
    max(0, target - score). Continuous and increasing in every demanded score; a shortfall costs (1 + weight) per
    point, a surplus earns 1 per point. Undemanded axes do not count."""
    total = sum(w for w in demand.values() if w > 0)
    rows, value = {}, 0.0
    for a, w in demand.items():
        if w <= 0:
            continue
        short = max(0.0, targets[a] - scaled[a])
        rows[a] = {'demand': w, 'score': round(scaled[a], 4), 'target': targets[a], 'shortfall': round(short, 4)}
        value += w * (scaled[a] - shortfall_weight * short)
    return (value / total if total else 0.0), rows


def five_axis_wait_cost(wait_hours, demand, targets, policy):
    """Score cost of waiting `wait_hours` for a window reset on the five-axis path (rounds 8-9, user 2026-11-09).

    Replaces the fixed 0.15 `deferred_start_penalty` when the caller allows waiting (`can_wait_hours > 0`). One hour of
    wait is priced at `shortage_loss_per_worker_hour` and scaled linearly by `(1 - target)`, where `target` is the task's
    demand-weighted target score on 0..1 (linear like `shortfall_weight = 1`, with no extra exponent): routine tasks
    (low target) pay a high waiting cost and start now; high-demand tasks (target near the anchor 0.8) pay a smaller
    waiting cost and prefer waiting for a capable model when `can_wait_hours` permits.
    """
    if not wait_hours or wait_hours <= 0:
        return 0.0
    total = sum(w for w in demand.values() if w > 0)
    target = sum(w * targets[a] for a, w in demand.items() if w > 0) / total if total else 0.0
    per_hour = exchange_rate(policy, 'shortage_loss_per_worker_hour')
    return round(per_hour * float(wait_hours) * (1.0 - target), 4)


def five_axis_order(candidate):
    """Sort key on the five-axis path: score, then opportunity cost (quota scarcity) ascending, then the demand-weighted
    axis level descending (round 5, user-approved; round 7: no floor to measure it from), then the sibling harness place
    (the same model, account and effort on another harness; never decides between models)."""
    k = candidate['components']
    return (-candidate['score'], round(k.get('opportunity_cost', 0.0), 4), -k.get('five_axis_level', 0.0),
            k.get('harness_place', 0))


HARNESS_CAPABILITIES = {
    'antigravity-resilient': {
        # No prompt image attachments; but the CLI's own read_file tool shows local image files to multimodal models.
        'accepts_image_input': False,
        'reads_image_files': True,
    },
}


def harness_accepts_visual(harness, model, needs_attachment=False):
    """True when visual work can reach this model on this harness (manual and auto bundles alike).

    The router dispatches text briefs only; visual workers read local image files themselves, so a task is
    treated as file-based unless the caller says it needs prompt attachments. The model's own multimodality
    is checked separately (models table `visual`)."""
    key = 'antigravity-resilient' if harness == 'antigravity' else harness
    cap = HARNESS_CAPABILITIES.get(key)
    if not cap:
        return True
    if needs_attachment:
        return bool(cap.get('accepts_image_input', True))
    return bool(cap.get('reads_image_files', True) or cap.get('accepts_image_input', True))


def bundle_applies(bundle, task):
    """A bundle with a `when` rule is offered only to the work it names (e.g. medium efforts on routine, Pro advisor on frontier)."""
    rule = bundle.get('when') or {}
    if rule and task.get('pinned_bundle') == bundle.get('id'):
        return True  # a user pin names this exact bundle; its `when` scope does not apply
    if 'max_reasoning_depth' in rule:
        return bool(task.get('profile')) and task['profile']['reasoning_depth'] <= rule['max_reasoning_depth']
    if 'min_difficulty' in rule:
        diff, req = task.get('difficulty'), rule['min_difficulty']
        if diff in DIFFICULTY_ORDER and req in DIFFICULTY_ORDER and DIFFICULTY_ORDER.index(diff) < DIFFICULTY_ORDER.index(req):
            return False
    return True


def _explicit_draws(bundle):
    """The bundle's draws list when the key is present, else None. An empty list is explicit."""
    if not isinstance(bundle, dict) or 'draws' not in bundle:
        return None
    raw = bundle.get('draws')
    if not isinstance(raw, (list, tuple)):
        return []
    found = []
    for wid in raw:
        if isinstance(wid, str) and wid and wid not in found:
            found.append(wid)
    return found


def bundle_pools(bundle, accounts, observed_windows=None):
    """Window ids this bundle draws, in declaration order (configured windows, then observed-only ones).

    An explicit `draws` on the passed bundle wins, including an empty list. Otherwise the catalog
    bundle's `draws` wins. Otherwise each window's data-declared scope decides (window_scope.binds on the
    canonical model): the configured spec's `models`/`models_except` and the reading's own `models`.
    A window no source scopes binds every model, observed-only windows included.
    """
    bundle = bundle if isinstance(bundle, dict) else {}
    accounts = accounts if isinstance(accounts, dict) else {}
    specs = accounts.get('accounts') if isinstance(accounts.get('accounts'), dict) else {}
    account = bundle.get('account') if isinstance(bundle.get('account'), str) else None
    spec = specs.get(account) if account else None
    bid = bundle.get('id') or bundle.get('bundle_id')
    if not isinstance(spec, dict):
        spec = None
        for name, candidate in specs.items():
            if isinstance(candidate, dict) and bid and bid in (candidate.get('bundles') or {}):
                account, spec = name, candidate
                break
    configured = spec.get('windows') if isinstance(spec, dict) and isinstance(spec.get('windows'), dict) else {}
    observed_windows = observed_windows or {}
    windows = {**configured, **{wid: {} for wid in observed_windows if wid not in configured}}
    declared = _explicit_draws(bundle)
    if declared is None and isinstance(spec, dict) and bid:
        catalog = (spec.get('bundles') or {}).get(bid)
        if isinstance(catalog, dict):
            declared = _explicit_draws(catalog)
    if declared is not None:
        return [wid for wid in declared if wid in windows]
    names = (canonical(bundle.get('model') or '', accounts), bundle.get('model') or '')
    return [wid for wid, wspec in windows.items() if window_scope.binds_any(names, wspec, observed_windows.get(wid))]


def applicable_windows(account_spec, model, usage_row, bundle=None, accounts=None):
    """(window id, spec, observed window) for windows that bind this model or bundle.

    With a bundle, explicit draws (including []) remain authoritative. Otherwise
    historical model tags and known observed scopes determine applicability.
    Observation-only hard limits have no invented capacity, burn rate or hours.
    """
    observed = {}
    for w in (usage_row or {}).get('windows', []):
        wid = w.get('id')
        if wid:
            observed.setdefault(canonical_window_id(account_spec, wid), w)
    configured = (account_spec or {}).get('windows') or {}
    windows = {**configured, **{wid: {} for wid in observed if wid not in configured}}
    if bundle is None:
        ids = bundle_pools({'account': '_billing', 'model': model or ''},
                           {'accounts': {'_billing': account_spec}}, observed)
    else:
        if accounts is None:
            acct = bundle.get('account') if isinstance(bundle, dict) and isinstance(bundle.get('account'), str) else '_billing'
            accounts = {'accounts': {acct: account_spec}}
            if not isinstance(bundle, dict) or 'account' not in bundle:
                bundle = {**(bundle or {}), 'account': acct}
        ids = bundle_pools(bundle, accounts, observed)
    return [(wid, windows[wid], observed.get(wid)) for wid in ids if wid in windows]


def below_launch_floor(window, observed, usage_row, now):
    """Existing launch protection, shared by ranking and dispatch."""
    floor = window.get('launch_min_remaining_pct')
    reset = parse_time((observed or {}).get('resets_at'))
    seconds = (observed or {}).get('seconds_until_reset')
    active_reset = (reset is not None and reset > now) or (
        reset is None and isinstance(seconds, (int, float)) and seconds > 0 and (observed or {}).get('reset_time_passed') is not True)
    if floor is not None and (observed or {}).get('rate_limited') is True and active_reset:
        return True
    fetched = parse_time(usage_row.get('fetched_at'))
    if (usage_row.get('status') != 'fresh' or usage_row.get('stale') is True or usage_row.get('available') is False
            or ('fetched_at' in usage_row and (fetched is None or not -60 <= (now - fetched).total_seconds() <= 900))):
        return False
    remaining = (observed or {}).get('remaining_pct_before_reset')
    if isinstance(remaining, (int, float)) and remaining <= 0 and (reset is None or active_reset):
        return True
    return bool(floor is not None and active_reset
                and isinstance(remaining, (int, float)) and remaining <= floor)


def latched_windows(accounts, account):
    """Windows a confirmed quota failure latched for this account (provider_inventory reads quota_exhaustion)."""
    return set((((accounts.get('_discovery') or {}).get('accounts') or {}).get(account) or {}).get('exhausted_windows') or ())


def cycle_observation(window, observed, usage_row, now):
    """The window's reading, or None when a reading that is not current cannot describe the current cycle.

    Usage only grows inside a cycle, so an old reading still bounds what is left until its reset. No reading
    describes the cycle after its own reset has passed. A reading that is not current cannot either when the
    account was unavailable, the reading has no time or is from the future, or it was taken more than the
    window's `hours` before its reset (an earlier cycle). A provider rate-limit observation is always kept.
    """
    if observed is None or observed.get('rate_limited') is True:
        return observed
    if observed.get('reset_time_passed') is True:
        return None
    if usage_row.get('status') in (None, 'fresh'):
        return observed     # current, or a caller that does not classify freshness (route_judge.usage_snapshot does)
    fetched = parse_time(usage_row.get('fetched_at'))
    if usage_row.get('available') is False or fetched is None or (fetched - now).total_seconds() > 60:
        return None
    reset, hours = parse_time(observed.get('resets_at')), window.get('hours')
    if reset is not None and isinstance(hours, (int, float)) and fetched < reset - timedelta(hours=hours):
        return None
    return observed


def used_up_in_cycle(observed, now):
    """A reading from this cycle (cycle_observation) at 0% left holds until its reset, however old it is."""
    if not observed or observed.get('reset_time_passed') is True:
        return False
    left = observed.get('observed_remaining_pct')
    if left is None:
        left = observed.get('remaining_pct_before_reset')
    reset = parse_time(observed.get('resets_at'))
    seconds = observed.get('seconds_until_reset')
    active_reset = (reset is not None and reset > now) or (
        reset is None and isinstance(seconds, (int, float)) and seconds > 0)
    return active_reset and isinstance(left, (int, float)) and left <= 0


def window_refusal(wid, window, observed, usage_row, now, latched=()):
    """Why a drawn window refuses any launch now, whatever the task's size; None when it does not."""
    if wid in latched:
        return f'{wid}: quota confirmed exhausted; refresh quota or replan'
    if below_launch_floor(window, observed, usage_row, now):
        return f'{wid}: confirmed exhausted or below launch floor; refresh quota or replan'
    return None


def short_window_limits(spec, wid, policy, now):
    """(small, floor %, safety margin %) of a short window; a small window keeps the larger reserve."""
    cap = capacity_usd(spec, wid, now)
    small = cap is not None and cap < policy.get('small_short_capacity_usd', 50)
    return (small,
            policy.get('small_short_window_floor_pct', 25) if small else policy.get('short_window_floor_pct', 10),
            policy.get('small_short_safety_margin_pct', 12) if small else policy.get('safety_margin_pct', 5))


def reserve_inflight(account, spec, wid, wspec, state, records, accounts, policy, now):
    """Apply active in-flight reservation deduction to a known window state; returns (state, reserved_usd, reserved_pct)."""
    if not state or not state.get('known') or not records:
        return state, 0.0, 0.0
    win_cap_usd = window_capacity_usd(spec, wid, now)
    reserved_usd = inflight_reserved_usd(account, spec, wid, wspec, state, win_cap_usd, records, accounts, policy, now)
    if reserved_usd > 0 and win_cap_usd:
        reserved_pct = min(state['remaining'], round(100.0 * reserved_usd / win_cap_usd, 2))
        state = {**state, 'observed_remaining': state['remaining'],
                 'remaining': max(0.0, state['remaining'] - reserved_pct),
                 'reserved_usd': round(reserved_usd, 2)}
        return state, reserved_usd, reserved_pct
    return state, 0.0, 0.0


def launch_blockers(bundle, accounts, usage, now, latched=None, records=None):
    """[(window id, reason)] for windows this bundle draws that stop a launch now, whatever the task's size.

    These are the task-independent outcomes of evaluate_bundle for "start now": its exclusions (window_refusal:
    the confirmed-exhaustion latch and the launch floor; a short window with an active reset but no reading) and
    the windows it excludes or defers to their reset, which no task size escapes (a short window at or under its
    floor; a priced window at or under the safety margin; a window this cycle's reading shows used up). Active
    in-flight reservations (`records`) are deducted from remaining capacity just as evaluate_bundle deducts them.
    `usage` is route_judge.usage_snapshot after provider_inventory.merge_usage; windows the model does not draw
    (window_scope) never appear.
    """
    accounts = accounts if isinstance(accounts, dict) else {}
    spec = (accounts.get('accounts') or {}).get(bundle.get('account'))
    if not isinstance(spec, dict):
        # An account the configuration does not describe still has its readings: every window binds.
        spec = {}
        accounts = {**accounts, 'accounts': {**(accounts.get('accounts') or {}), bundle['account']: spec}}
    if spec.get('unmetered'):
        return []
    policy = accounts.get('scoring') or {}
    row = ((usage or {}).get('accounts') or {}).get(bundle['account']) or {}
    stale = row.get('status') == 'stale' or row.get('stale') is True
    latched = latched_windows(accounts, bundle['account']) if latched is None else set(latched)
    model = canonical(bundle.get('model') or '', accounts)
    found = []
    for wid, wspec, observed in applicable_windows(spec, model, row, bundle, accounts):
        observed = cycle_observation(wspec, observed, row, now)
        reset_label = (observed or {}).get('resets_at') or f"in {((observed or {}).get('seconds_until_reset') or 0) / 3600:.1f}h"
        reason = window_refusal(wid, wspec, observed, row, now, latched) or (
            f"{wid}: used up until its reset at {reset_label}; start after its reset"
            if used_up_in_cycle(observed, now) else None)
        state = None if reason else window_state(wid, wspec, observed, spec, now, policy.get('short_window_hours', 24),
                                                 account_stale=stale)
        if state is not None and state['known']:
            state, _, _ = reserve_inflight(bundle['account'], spec, wid, wspec, state, records, accounts, policy, now)
        if state is None:
            pass
        elif state['known'] and isinstance(wspec.get('launch_min_remaining_pct'), (int, float)) and state['remaining'] <= float(wspec['launch_min_remaining_pct']):
            floor = float(wspec['launch_min_remaining_pct'])
            reason = f"{wid}: confirmed exhausted or below launch floor ({state['remaining']:.0f}% left, floor {floor:g}%); refresh quota or replan"
        elif not state['short']:
            # Priced work needs remaining - need >= safety margin; no task size passes at or under the margin.
            margin = policy.get('safety_margin_pct', 5)
            if state['known'] and state['remaining'] <= margin and unit_rate(
                    spec, wid, model, scoring_effort(bundle, model_spec(accounts, model)), policy, (accounts.get('effort_curves') or {}).get(model, {}),
                    model_spec(accounts, model), now):
                reason = (f"{wid} has {state['remaining']:.1f}% left, within the {margin:g}% safety margin, "
                          f"for {state['reset_hours']:.1f}h")
        elif not state['known']:
            reset = parse_time((observed or {}).get('resets_at'))
            seconds = (observed or {}).get('seconds_until_reset')
            if ((reset is not None and reset > now)
                    or (reset is None and ((isinstance(seconds, (int, float)) and seconds > 0)
                                           or (observed or {}).get('reset_time_passed') is False))) and (observed or {}).get('reset_time_passed') is not True:
                wait = (reset - now).total_seconds() / 3600 if reset is not None else (seconds / 3600 if isinstance(seconds, (int, float)) else 0.0)
                reason = f'{wid} has an active reset in {wait:.1f}h with unknown remaining percentage'
        elif state['remaining'] <= (floor := short_window_limits(spec, wid, policy, now)[1]):
            reason = (f"{wid} is empty ({state['remaining']:.0f}% left, floor {floor:g}%) "
                      f"for {state['reset_hours']:.1f}h; start after its reset")
        if reason:
            found.append((wid, reason))
    return found


QUOTA_REASON = re.compile(r'^([\w.-]+)(?::| has | is )')


def quota_wait_hours(candidate, usage):
    """Hours until every quota window that excludes a candidate has reset, or None when anything other than its own
    account's quota windows (ability, policy, model management, route) excludes it."""
    account = (candidate.get('bundle') or {}).get('account')
    windows = (((usage or {}).get('accounts') or {}).get(account) or {}).get('windows') or []
    resets = {str(w.get('id')): w.get('seconds_until_reset') for w in windows if isinstance(w, dict)}
    hours = 0.0
    for reason in candidate.get('excluded') or []:
        match = QUOTA_REASON.match(str(reason))
        seconds = resets.get(match.group(1)) if match else None
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
            return None
        hours = max(hours, seconds / 3600)
    return hours


def pool_size(observed):
    """How many accounts a window reading pools (a Magpie pool reports `accounts: N`); 1 for a single account."""
    count = (observed or {}).get('accounts')
    return count if isinstance(count, int) and not isinstance(count, bool) and count > 1 else 1


def window_state(wid, spec, observed, account_spec, now, short_hours, account_stale=False, expected_reset_at=None):
    """Remaining capacity, time to the end of the allowance and how sure we are."""
    hours = spec.get('hours')
    expires = parse_time(account_spec.get('expires_at'))
    row = {'id': wid, 'hours': hours, 'short': hours is not None and hours < short_hours, 'known': False, 'stale': bool(account_stale)}
    if observed is None or hours is None:
        return row
    # Percentages are of one account's allowance; a pool of N accounts holds N x 100 when full.
    row['full'] = full = 100.0 * pool_size(observed)
    remaining = observed.get('remaining_pct_before_reset')
    if remaining is None:
        remaining = observed.get('observed_remaining_pct')
        row['stale'] = row['stale'] or remaining is not None
    seconds = observed.get('seconds_until_reset')
    if seconds is None and (natural_reset := parse_time(observed.get('resets_at'))) is not None:
        seconds = (natural_reset - now).total_seconds()
    if row['short'] and remaining == 100 and seconds is None and observed.get('resets_at') is None:
        # An unstarted rolling short window (used_pct == 0, resets_at == null) has full 5h capacity ready.
        seconds = hours * 3600
        row['stale'] = bool(account_stale)
    expected_hours = ((expected_reset_at - now).total_seconds() / 3600
                      if expected_reset_at is not None and expected_reset_at > now else None)
    if expected_hours is not None:
        row.update(natural_reset_hours=seconds / 3600 if seconds is not None else hours,
                   expected_reset_at=expected_reset_at.isoformat())
        seconds = min(seconds, expected_hours * 3600) if seconds is not None else expected_hours * 3600
    if remaining is None or seconds is None:
        return row
    end_hours = seconds / 3600
    expiry_hours = (expires - now).total_seconds() / 3600 if expires is not None and expires > now else None
    if expiry_hours is not None:
        end_hours = min(end_hours, expiry_hours)
    row.update({'known': True, 'remaining': remaining, 'reset_hours': seconds / 3600, 'end_hours': end_hours,
                'expiry_hours': expiry_hours, 'target_remaining': full * min(1.0, end_hours / hours),
                'reset_at': observed.get('resets_at')})
    return row


def after_reset(state, hours):
    """The same allowance once it renews, assuming a full renewal. None when the plan ends first."""
    wait = state['reset_hours']
    if state.get('expected_reset_at') and wait < state.get('natural_reset_hours', wait):
        return None  # A prediction must never authorize work against an imagined refill.
    if wait <= 0 or state.get('stale'):
        return None
    expiry = state['expiry_hours']
    left = None if expiry is None else expiry - wait
    if left is not None and left <= 0:
        return None
    end = state['hours'] if left is None else min(state['hours'], left)
    full = state.get('full', 100.0)
    return dict(state, remaining=full, reset_hours=wait + state['hours'], end_hours=end,
                target_remaining=full * min(1.0, end / state['hours']), deferred_hours=wait,
                expiry_hours=left)


def account_cwd_allowed(spec, cwd=None):
    """True when the account has no allowed_cwd restriction or when cwd falls within one of its allowed_cwd paths."""
    if not cwd_policy.matches((spec or {}).get('cwd_regex'), cwd):
        return False
    allowed = (spec or {}).get('allowed_cwd')
    if not allowed:
        return True
    entries = [allowed] if isinstance(allowed, str) else [x for x in allowed if isinstance(x, str) and x.strip()]
    if not entries:
        return True
    raw_actual = Path(str(cwd or Path.cwd())).expanduser()
    abs_actual = Path(os.path.abspath(str(raw_actual)))
    abs_parts = tuple(p.lower().rstrip('\\/') for p in abs_actual.parts if p not in ('\\', '/'))
    for raw in entries:
        item = raw.strip()
        cand = Path(item).expanduser()
        if cand.is_absolute() or ':' in item or item.startswith(('/', '\\')):
            abs_cand = Path(os.path.abspath(str(cand)))
            cand_parts = tuple(p.lower().rstrip('\\/') for p in abs_cand.parts if p not in ('\\', '/'))
            if cand_parts and len(abs_parts) >= len(cand_parts) and abs_parts[:len(cand_parts)] == cand_parts:
                return True
        else:
            cand_parts = tuple(p.lower().rstrip('\\/') for p in cand.parts if p not in ('\\', '/'))
            n = len(cand_parts)
            if n and any(abs_parts[i:i + n] == cand_parts for i in range(len(abs_parts) - n + 1)):
                return True
    return False


def evaluate_bundle(bundle, task, ctx):
    policy, accounts, capabilities = ctx['policy'], ctx['accounts'], ctx['capabilities']
    spec = accounts['accounts'][bundle['account']]
    # Tables read by the name's own resolution. `model` is its key, for the matching that follows; `own_model` is which
    # model the row runs, for what the user named.
    model = canonical(bundle['model'], accounts)
    own_model = row_key(bundle['model'], accounts)
    info = model_spec(accounts, bundle['model'])
    curves = model_curves(accounts, bundle['model'])
    kind = task['kind']
    dur_mult = model_duration_multiplier(model, info)
    base_hours = task['hours']
    base_chain_hours = task['chain_hours']
    hours = base_hours * dur_mult
    chain_hours = base_chain_hours * dur_mult
    result = {'id': bundle['id'], 'bundle': {k: bundle.get(k) for k in ('provider', 'harness', 'account', 'model', 'effort', 'modeId', 'launch_model', 'draws', 'pin_carrier')
                                              if bundle.get(k) is not None},
              'excluded': [], 'notes': [],
              'components': {**subjective_vector(info), 'capability': vision_tools_score(info)}, 'score': None}
    visual_override = (info.get('subjective') or {}).get('capability')
    five = ctx.get('five_axis')
    if five:
        axes = (info.get('five_axis') or {}).get('scores') or {}
        result['components'] = {**{a: axes.get(a, 0.0) for a in FIVE_AXIS_THINKING}, 'capability': axes.get('vision_tools', 0.0)}
        # The tier folded into the axes: this model's level on this task at each effort is its demand-weighted axis
        # level. Carried as a task-specific tier and an `ability_mult` curve, so the effort machinery below reads it
        # unchanged; the model's own tier is not read, and the level decides nothing but the last tie-break.
        five_info, five_curves = info, curves
        levels = {e: five_axis_level(axes, task['five_axis_demand'], five_axis_effort_ratio(info, policy, curves, e))[0]
                  for e in set(EFFORT_ORDER) | {'none', 'default'} | set((curves or {}).get('ability') or {})}
        level_high = levels['high']
        info = {**info, 'tier': level_high}
        curves = {**(curves or {}), 'ability_mult': {e: (v / level_high if level_high > 0 else 0.0) for e, v in levels.items()}}
    elif visual_override is not None:
        result['components']['capability_override'] = visual_override
    else:
        caps = info.get('capabilities') or {}
        result['components'].update({key: caps.get(key, 0) for key in ('visual', 'gui')})
    now = ctx['now']

    def exclude(reason):
        result['excluded'].append(reason)

    execution_account = accounts_override.preference_account(bundle, bundle['account'], accounts)
    cwd_spec = accounts['accounts'].get(execution_account) or spec
    if not account_cwd_allowed(cwd_spec, task.get('cwd')):
        exclude(f"outside account cwd restriction ({cwd_spec['cwd_regex']})" if cwd_spec.get('cwd_regex') else
                f"outside account allowed_cwd ({cwd_spec.get('allowed_cwd')})")

    if not host_policy.account_allowed(ctx['host_policy'], bundle['account']):
        exclude('excluded by host policy')
    # Off by default (superseded in public data) is not the user's checkbox: naming the model still reaches it.
    if bundle.get('disabled') and not (bundle.get('disabled_by_default') and bundle_named(task, bundle, own_model)):
        exclude('unchecked in model management')
    if bundle.get('discovery_unavailable'):
        exclude(bundle['discovery_unavailable'] if isinstance(bundle['discovery_unavailable'], str)
                else 'model not detected by discovery for 7+ days')
    if bundle['id'] in ctx['unavailable']:
        exclude('marked unavailable by the parent')
    cooling = cooling_reason(bundle, ctx.get('launch_failures'), accounts)
    if cooling:
        exclude(cooling)
    latched_model = model_latch.reason(bundle, ctx.get('model_latches'))
    if latched_model:
        exclude(latched_model)
    retrying = provider_retry.reason(bundle, ctx.get('provider_retries'))
    if retrying:
        exclude(retrying)
    # Only subscription accounts join automatic routing. A checked metered (API) bundle is a last resort:
    # explicit selection, task.metered_ok, or plan()'s fallback pass when no subscription can take the work.
    # Unknown billing stays explicit selection only.
    billing = accounts_override.manual_only_billing(bundle['account'], accounts)
    if billing and not task['metered_ok'] and not (billing == 'metered' and task.get('metered_fallback')):
        if (not model_pin_matches(task, bundle, own_model) and task.get('pinned_bundle') != bundle['id']
                and not any(o.get('account') == bundle['account'] for o in ctx['overrides'])):
            exclude(METERED_LAST_RESORT if billing == 'metered'
                    else f'{billing} billing: explicit selection only')
    if not spec.get('verified', True) and not task['include_unverified']:
        exclude('account not verified as launchable')
    if info.get('advisor_only') and kind not in info.get('advisor_for', ()):
        exclude('advisor only; cannot execute')
    if info.get('excluded_when') == 'local_only' and task['local_only']:
        exclude("the parent marked this work local-only (it must run on this machine: in-place edits, local tools, secrets or live accounts); this worker runs remotely")
    if info.get('excluded_when') == 'local_only' and task.get('outbound_ok') is False:
        exclude('the parent disallowed outbound work; this worker runs remotely')
    if info.get('text_only') and task.get('file_transfer'):
        exclude('this text-only runner cannot transfer or apply local files')
    route = capabilities['routes'].get(kind) or {}
    # Route lists and the models table's visual ability are another dimension set: the five-axis path reads neither
    # (round 7); vision is the vision_tools axis, and a model with no vision data leaves routing by rule B.
    if not five and route.get('restrict_models') is True and route.get('preferred'):
        allowed = {canonical(m, accounts) for m in route['preferred']}
        if model not in allowed:
            exclude(f'{kind} is a specialist route')
    if not five and task['needs_visual'] and not info.get('visual'):
        exclude('task needs visual input')
    # Harness input limits apply to every bundle on the harness, manual or auto (HARNESS_CAPABILITIES).
    harness = bundle.get('harness') or bundle_harness(bundle, bundle.get('account'), bundle.get('id'))
    if task['needs_visual'] and not harness_accepts_visual(harness, model):
        exclude(f'{harness} cannot accept image attachments for {model}, so it cannot take visual or GUI work')
    if task['constraint_model'] and not model_pin_matches(task, bundle, own_model):
        exclude('explicit model constraint')
    if task.get('constraint_effort') and str(scoring_effort(bundle, info) or '').lower() != task['constraint_effort']:
        exclude('explicit effort constraint')
    if five:
        # Rule B: a demanded axis with no data at all, or no cited usd on a metered account (billed or quota windows),
        # leaves automatic routing; naming the model (--model, or its bundle) still dispatches it, with the data error as
        # a warning and its cost unpriced (model_usd_per_hour returns 0, never the 4.0 prior).
        state = info.get('five_axis') or {'missing': list(FIVE_AXES), 'usd_status': 'missing'}
        billed = not (spec.get('unmetered') or spec.get('unlimited'))
        named = model_pin_matches(task, bundle, own_model) or task.get('pinned_bundle') == bundle['id']
        blind = [a for a, w in task['five_axis_demand'].items() if w >= five['demanded_level'] and a in state.get('missing', ())]
        errors = ([f'no trusted data for the demanded axis {", ".join(blind)}'] if blind else []) + (
            ['usd_per_hour has no cited source on a metered account'] if billed and state.get('usd_status') != 'ok' else [])
        for error in errors:
            if named:
                unpriced = ' (its cost is not priced)' if error.startswith('usd_per_hour') else ''
                result['notes'].append(f'data error: {error}; dispatched because it was named{unpriced}')
                result.setdefault('data_errors', []).append(error)
            else:
                exclude(f'data error: {error} (out of automatic routing; name the model to dispatch it)')
    demand = task['demand']
    target = demand['required_ability']
    floor = demand['floor']
    headroom = critical_headroom(policy, task['difficulty']) if task['critical'] else 0.0
    if five:
        target = floor = headroom = 0.0       # no difficulty floor on the five-axis path (round 7)
    gate = floor + headroom
    eff = scoring_effort(bundle, info)
    resp_val = resp_source = stepped_from = None
    if task.get('realtime_loop'):
        resp_cfg = responsiveness_config(policy)
        eff, resp_val, resp_source, stepped_from = resolve_realtime_effort(
            bundle, task, ctx, model, info, policy, curves, gate)
        if stepped_from is not None:
            result['bundle']['effort'] = eff
            result['bundle']['realtime_stepped_from'] = stepped_from
            if task.get('slow_pin_fallback') and model_pin_matches(task, bundle, own_model):
                result['bundle']['named_fastest'] = True
            result['components']['effort_stepped_down_from'] = stepped_from
            result['notes'].append(f'effort stepped down from {stepped_from} to {eff} for realtime_loop')
    # Model ability x this model's own thinking coefficient at this effort (accounts `effort_curves`). The task side is
    # depth x property difficulty (`task['demand']`); no effort tier is tied to a depth.
    ability = effort_ability(policy, curves, info, eff)
    result['components']['ability_value'] = round(ability, 2)
    effective = ability
    high_ability = effort_ability(policy, curves, info, 'high')
    if not five and ability < gate:
        result['below_floor'] = True
        exclude(f'ability {ability:.1f} below the {task["difficulty"]} floor {floor}'
                + (f' plus the {headroom:g} headroom critical work needs' if task['critical'] else '')
                + (f' after the realtime step-down from {stepped_from} to {eff}' if stepped_from else ''))
    if task.get('realtime_loop'):
        resp_cfg = responsiveness_config(policy)
        result['components']['responsiveness_source'] = resp_source
        pinned_choice = exact_pin(task, bundle, own_model) or (
            bool(task.get('slow_pin_fallback')) and model_pin_matches(task, bundle, own_model))
        if resp_source == 'not_applicable' or resp_val is None:
            result['components']['responsiveness'] = None
            if pinned_choice:
                warn = (f"pinned {bundle.get('id')} ({model}@{eff}) responsiveness is not_applicable "
                        f"for realtime_loop (too slow per turn for a live loop); kept per explicit user selection")
                result['components']['realtime_slow_pin_warning'] = warn
                result['notes'].append(warn)
            else:
                result['below_floor'] = True
                result['too_slow'] = True
                exclude(f"{bundle['model']}@{eff}: responsiveness not_applicable for this channel (too slow per turn for a live loop)")
        else:
            result['components']['responsiveness'] = round(resp_val, 3)
            if resp_source == 'default':
                result['notes'].append('responsiveness: default')
            resp_floor = float(resp_cfg['floor'])
            if resp_val < resp_floor:
                if pinned_choice:
                    warn = (f"pinned {bundle.get('id')} ({model}@{eff}) responsiveness {resp_val:.2f} is below "
                            f"the realtime_loop floor {resp_floor:g} (too slow per turn for a live loop); "
                            f"kept per explicit user selection")
                    result['components']['realtime_slow_pin_warning'] = warn
                    result['notes'].append(warn)
                else:
                    result['below_floor'] = True
                    result['too_slow'] = True
                    exclude(f"{bundle['model']}@{eff}: responsiveness {resp_val:.2f} below the realtime_loop floor {resp_floor:g} "
                            f'(too slow per turn for a live loop)')
    expires = parse_time(spec.get('expires_at'))
    if expires is not None and (expires - now).total_seconds() < hours * 3600:
        exclude('subscription ends before the work would finish')

    worker = task['worker']
    worn = worker_worn(worker, policy)
    resumes = (
        bool(worker)
        and worker.get('bundle') == bundle['id']
        and not (task.get('read_only_review') and worker.get('read_only_review') is False)
    )
    context_extra = 0.0
    if resumes:
        carried = worker_historical_tokens(worker)
        if not worn:
            limit = max(250000, policy.get('rotate_after_carried_tokens', 2000000))
            context_extra = policy['context_burn_extra_at_250k'] * clamp(carried / limit, 0, 1)
        if worker.get('telemetry_status') == 'partial':
            result['notes'].append('session telemetry partial; carried tokens are a lower bound')
        if worker.get('compacted_tokens') is None and worker.get('compactions') is None and worker.get('historical_context_tokens') is None:
            result['notes'].append('compaction history unknown (give chain.worker.agent_id); resuming')
        elif worn:
            result['notes'].append(f"the worker has carried {carried / 1e6:.1f}M historical tokens of context over "
                                   f"{worker.get('compactions', '?')} compactions; rotate with a handoff packet")
    context_scale = 1 + context_extra
    step_pct = chain_pct = None
    step_usd = bundle_task_usd(spec, model, eff, hours, policy, curves, info, context_scale)
    chain_usd = bundle_task_usd(spec, model, eff, chain_hours, policy, curves, info, context_scale)
    binding = None
    opportunity = 0.0
    unknown = False
    available_at = None
    if spec.get('unlimited'):
        result['notes'].append('unlimited usage')
    # Free capacity is priced as zero opportunity cost below. The extra free-usage preference is declared per account
    # in public data (`free_usage_bonus`), and only for a measured model row (not `score_estimated`) whose ability meets
    # the required ability; other free accounts compete on their zero opportunity cost alone.
    if not five and spec.get('free_usage_bonus') and not info.get('score_estimated') and ability >= target:
        result['components']['free_usage'] = policy['unlimited_bonus']
    boost = sum(float(o.get('boost') or 0) for o in ctx.get('overrides', ())
                if o['account'] == bundle['account'] and (not o.get('model') or canonical(o['model'], accounts) == model)
                and (expected_reset_time(o) is None or expected_reset_time(o) <= now))
    for intent in ctx.get('overrides', ()):
        if (intent['account'] == bundle['account'] and intent.get('expected_reset_at')
                and (not intent.get('model') or canonical(intent['model'], accounts) == model)):
            result['notes'].append(f"expected reset {intent['expected_reset_at']}: {intent['reset_expectation_status']}; capacity requires a fresh observation")
    if boost:
        result['components']['boost'] = round(boost, 3)
        result['notes'].append(f'temporary boost {boost:+.2f} from a user override')
    if not spec.get('unmetered'):
        acct_usage = ctx['usage'].get('accounts', {}).get(bundle['account']) or {}
        acct_stale = acct_usage.get('status') == 'stale' or acct_usage.get('stale') is True
        latched = latched_windows(accounts, bundle['account'])
        for wid, wspec, observed in applicable_windows(spec, model, acct_usage, bundle, accounts):
            observed = cycle_observation(wspec, observed, acct_usage, now)
            refusal = window_refusal(wid, wspec, observed, acct_usage, now, latched)
            if refusal:
                exclude(refusal)
            expected = window_reset_expectation(ctx.get('overrides', ()), bundle['account'], model, wid, spec, accounts, now)
            state = window_state(wid, wspec, observed, spec, now, policy['short_window_hours'],
                                 account_stale=acct_stale, expected_reset_at=expected)
            unit = unit_rate(spec, wid, model, eff, policy, curves, info, now)
            if not state['known']:
                if state['short']:
                    step = None if unit is None else hours * unit * context_scale
                    margin_pct = short_window_limits(spec, wid, policy, now)[2]
                    if step is not None and 100.0 - step < margin_pct:
                        exclude(f'{wid} full capacity (100%) is short of the ~{step:.1f}% the work needs plus {margin_pct}% margin')
                    elif (observed and (reset_dt := parse_time(observed.get('resets_at'))) is not None
                          and reset_dt > now and observed.get('reset_time_passed') is not True):
                        wait = (reset_dt - now).total_seconds() / 3600
                        exclude(f'{wid} has an active reset in {wait:.1f}h with unknown remaining percentage')
                continue
            state, reserved_usd, reserved_pct = reserve_inflight(
                bundle['account'], spec, wid, wspec, state, ctx.get('records'), accounts, policy, now)
            if reserved_usd > 0:
                result['notes'].append(f'{wid}: -${reserved_usd:.2f} (-{reserved_pct:.1f}%) reserved by in-flight {bundle["account"]} dispatch')
            if state['short']:
                step = None if unit is None else hours * unit * context_scale
                small_short, floor_pct, margin_pct = short_window_limits(spec, wid, policy, now)
                empty = state['remaining'] <= floor_pct
                if empty or (step is not None and state['remaining'] - step < margin_pct):
                    wait = state['reset_hours']
                    what = 'is empty' if empty else f'has {state["remaining"]:.0f}%, short of the ~{step:.1f}% the work needs,'
                    if (not small_short and not state['stale'] and 0 < wait <= policy['defer_if_reset_within_hours']
                            and wait >= state.get('natural_reset_hours', wait)):
                        available_at = max(available_at or 0, wait)
                        result['notes'].append(f'{wid} {what} for {wait:.1f}h; start after its reset')
                    else:
                        exclude(f'{wid} {what} for {wait:.1f}h')
                    continue        # the work starts after the reset, when the short window is full again
                continue            # running a standard short window dry only delays work until its reset; the weekly window prices the loss
            if unit is None and five:
                # Capacity unknown: the need cannot be priced in percent, but the live percentage and the reset time
                # still give the window's scarcity (and its reset bonus) and the reserve still holds.
                if state['remaining'] <= policy['safety_margin_pct']:
                    exclude(f'{wid} has {state["remaining"]:.0f}% left, inside the {policy["safety_margin_pct"]}% reserve '
                            '(capacity unknown, so the need cannot be priced)')
                    result['wait_until_hours'] = max(result.get('wait_until_hours', 0), state['reset_hours'])
                    continue
                row = window_cost(spec, wid, state, 0.0, demand_forecast(spec, wid, state, policy), policy, result, now,
                                  step_usd=None, unit=None)
                row.update(chain_pct=0.0, stale=state['stale'], capacity_unknown=True)
                opportunity += row['opportunity_cost']
                result['notes'].append(f'{wid}: data error: no cited capacity (capacity ladder empty); scarcity from the live '
                                       f'{state["remaining"]:.0f}% and the reset in {state["reset_hours"]:.1f}h')
                if binding is None or (row['opportunity_cost'], -row['after']) > (binding['opportunity_cost'], -binding['after']):
                    binding = row
                continue
            if unit is None:
                result['notes'].append(f'{wid}: observed {state["remaining"]:.1f}% remaining'
                                       + (' (stale)' if acct_stale or state['stale'] else '')
                                       + '; consumption rate unknown')
                continue
            step = hours * unit * context_scale
            chain = chain_hours * unit * context_scale
            need = chain if task['coupling'] == 'high' else step
            if state['remaining'] - need < policy['safety_margin_pct'] and state['reset_hours'] <= policy['defer_if_reset_within_hours']:
                renewed = after_reset(state, wspec['hours'])
                if renewed is not None and renewed['expiry_hours'] is not None and renewed['expiry_hours'] < hours:
                    renewed = None
                if renewed is not None:
                    state = renewed
                    available_at = max(available_at or 0, renewed['deferred_hours'])
                    result['notes'].append(f'{wid} renews in {renewed["deferred_hours"]:.1f}h; start then (assumes a full renewal)')
            if state['remaining'] - need < policy['safety_margin_pct']:
                exclude(f'{wid} has {state["remaining"]:.0f}% left; the work needs ~{need:.1f}% plus a {policy["safety_margin_pct"]}% margin '
                        f'and it resets in {state["reset_hours"] / 24:.1f} days')
                result['wait_until_hours'] = max(result.get('wait_until_hours', 0), state['reset_hours'])
            pacing = ({**five, 'reference_usd_per_hour': (spec.get('reference_usd_per_hour') or {}).get(wid)}
                      if five else None)
            row = window_cost(spec, wid, state, step, demand_forecast(spec, wid, state, policy), policy, result, now, step_usd=step_usd,
                              unit=unit * context_scale, pacing=pacing)
            opportunity += row['opportunity_cost']
            row.update(chain_pct=chain, chain_usd=round(chain_usd, 2), stale=state['stale'])
            if binding is None or (row['opportunity_cost'], -row['after']) > (binding['opportunity_cost'], -binding['after']):
                binding = row
        if binding:
            step_pct, chain_pct = binding['step_pct'], binding['chain_pct']
            if binding.get('reset_bonus'):
                result['components']['reset_bonus'] = binding['reset_bonus']
                result['notes'].append(f"{binding['window']}: resets in {binding['reset_hours']:.1f}h with ~{binding['surplus_pct']:.0f}% "
                                       f"surplus above demand; reset_bonus {binding['reset_bonus']:+.2f}")
            if binding['stale']:
                result['notes'].append('capacity from a stale observation')
        else:
            unknown = True
            opportunity += policy['unknown_capacity_penalty']
            result['notes'].append('no current capacity observation; unknown is not exhausted')
    fast_mult = fast_modes.codex_burn_multiplier(policy, spec)
    if (harness == 'codex' and fast_modes.supported(harness, model, info) and not task.get('disable_fast')
            and not result['excluded'] and available_at is None):
        horizon = float(policy.get('reset_horizon_hours') or 0)
        in_horizon = bool(binding and not binding.get('stale') and not binding.get('expires_first')
                          and 0 < binding.get('reset_hours', 0) < horizon)
        if task.get('force_fast') and (unknown or spec.get('unmetered')):
            result['bundle']['fast'] = True
            result['components']['fast'] = True
            result['components']['fast_reason'] = 'user_override'
            step_usd, chain_usd = step_usd * fast_mult, chain_usd * fast_mult
            result['notes'].append(f'fast mode enabled (user_override: {fast_mult:g}x quota burn)')
        elif (task.get('force_fast') or task.get('wants_fast') or in_horizon) and binding and not binding.get('stale'):
            acct_usage = ctx['usage'].get('accounts', {}).get(bundle['account']) or {}
            acct_stale = acct_usage.get('status') == 'stale' or acct_usage.get('stale') is True
            fast_ok, fast_opp, fast_binding = True, 0.0, None
            for wid, wspec, observed in applicable_windows(spec, model, acct_usage, bundle, accounts):
                observed = cycle_observation(wspec, observed, acct_usage, now)
                expected = window_reset_expectation(ctx.get('overrides', ()), bundle['account'], model, wid, spec, accounts, now)
                st = window_state(wid, wspec, observed, spec, now, policy['short_window_hours'],
                                  account_stale=acct_stale, expected_reset_at=expected)
                if not st['known']:
                    continue
                unit = unit_rate(spec, wid, model, eff, policy, curves, info, now)
                win_cap_usd = window_capacity_usd(spec, wid, now)
                res_usd = inflight_reserved_usd(bundle['account'], spec, wid, wspec, st, win_cap_usd,
                                                ctx.get('records'), accounts, policy, now)
                if res_usd > 0 and win_cap_usd:
                    res_pct = min(st['remaining'], round(100.0 * res_usd / win_cap_usd, 2))
                    st = {**st, 'observed_remaining': st['remaining'],
                          'remaining': max(0.0, st['remaining'] - res_pct), 'reserved_usd': round(res_usd, 2)}
                if st['short']:
                    f_step = None if unit is None else hours * unit * context_scale * fast_mult
                    _, fl_pct, mg_pct = short_window_limits(spec, wid, policy, now)
                    if st['remaining'] <= fl_pct or (f_step is not None and st['remaining'] - f_step < mg_pct):
                        fast_ok = False
                        break
                    continue
                if unit is None:
                    continue
                f_step = hours * unit * context_scale * fast_mult
                f_chain = chain_hours * unit * context_scale * fast_mult
                f_need = f_chain if task['coupling'] == 'high' else f_step
                if st['remaining'] - f_need < policy['safety_margin_pct']:
                    fast_ok = False
                    break
                f_row = window_cost(spec, wid, st, f_step, demand_forecast(spec, wid, st, policy), policy,
                                    {'notes': []}, now, step_usd=step_usd * fast_mult, unit=unit * context_scale * fast_mult)
                fast_opp += f_row['opportunity_cost']
                f_row.update(chain_pct=f_chain, chain_usd=round(chain_usd * fast_mult, 2), stale=st['stale'])
                if fast_binding is None or (f_row['opportunity_cost'], -f_row['after']) > (fast_binding['opportunity_cost'], -fast_binding['after']):
                    fast_binding = f_row
            if task.get('force_fast') and not fast_ok:
                exclude(f'fast mode ({fast_mult:g}x burn) exceeds remaining window safety margin')
            elif fast_ok and fast_binding:
                f_usable = fast_binding['after'] - policy['safety_margin_pct']
                f_shortage = fast_binding['shortage_probability']
                f_sunk = fast_binding.get('reset_sunk_weight', 0.0)
                extra_opp = max(0.0, fast_opp - opportunity)
                max_sh = float(policy.get('fast_max_shortage_prob', 0.2))
                max_ex_opp = float(policy.get('fast_max_extra_opportunity_cost', 0.015))
                plentiful_ref = float(policy.get('fast_plentiful_usable_pct', 45.0))
                sprint_thresh = float(policy.get('fast_sprint_sunk_threshold', 0.25))
                is_sprint = bool(in_horizon and (five or task['difficulty'] != 'routine' or task.get('wants_fast'))
                                 and f_shortage <= max_sh
                                 and f_sunk >= (0.05 if task.get('wants_fast') else sprint_thresh))
                is_plentiful = bool(task.get('wants_fast') and f_usable >= plentiful_ref
                                    and f_shortage <= max_sh and extra_opp <= max_ex_opp)
                if is_sprint or is_plentiful or task.get('force_fast'):
                    old_reset = result['components'].get('reset_bonus', 0.0)
                    new_reset = fast_binding.get('reset_bonus', 0.0)
                    bonus_max = float(policy.get('fast_bonus_max', 0.04))
                    raw_bonus = (bonus_max if (task.get('wants_fast') and is_sprint)
                                 else round(0.75 * bonus_max, 4) if task.get('wants_fast')
                                 else round(0.5 * bonus_max * f_sunk, 4))
                    comp_delta = max(0.0, (fast_opp - opportunity) + (old_reset - new_reset))
                    fast_reason = ('pre_reset_sprint' if is_sprint
                                   else 'user_override' if task.get('force_fast')
                                   else 'fast_requested_plentiful_quota')
                    result['bundle']['fast'] = True
                    result['components']['fast'] = True
                    result['components']['fast_reason'] = fast_reason
                    result['components']['fast_bonus'] = round(raw_bonus + comp_delta, 4)
                    if new_reset:
                        result['components']['reset_bonus'] = new_reset
                    else:
                        result['components'].pop('reset_bonus', None)
                    binding, opportunity = fast_binding, fast_opp
                    step_pct, chain_pct = fast_binding['step_pct'], fast_binding['chain_pct']
                    step_usd, chain_usd = step_usd * fast_mult, chain_usd * fast_mult
                    result['notes'].append(f'fast mode enabled ({fast_reason}: {fast_mult:g}x quota burn, '
                                           f'{fast_binding["after"]:.0f}% left after step)')
    paid_fast = (harness == 'claude' and fast_modes.supported(harness, model, info)
                 and task.get('force_fast') and not task.get('disable_fast'))
    if paid_fast:
        # Preserve existing eligibility/protection checks above. Their subscription
        # observations do NOT establish a credits balance or budget for this run.
        result['bundle'].update(fast=True, fast_billing=fast_modes.claude_billing(spec))
        result['components'].update(fast=True, fast_reason='explicit_paid_opt_in')
        result['components'].pop('reset_bonus', None)
        binding, step_pct, chain_pct = None, None, None
        opportunity = policy['unknown_capacity_penalty']
        result['notes'].append('Claude fast uses paid billing outside subscription limits; '
                               'credit entitlement and remaining balance are unknown; provider enforces access. '
                               'Existing subscription launch protections are retained conservatively.')
    result['capacity'] = binding
    result['expected_hours'] = round(hours, 2)
    if dur_mult != 1.0:
        result['components']['duration_multiplier'] = round(dur_mult, 2)
    result['expected_pct'] = None if step_pct is None else round(step_pct, 2)
    result['chain_pct'] = None if chain_pct is None else round(chain_pct, 2)
    result['expected_usd'] = None if paid_fast or spec.get('billing') == 'unknown' else round(step_usd, 2)
    result['chain_usd'] = None if paid_fast or spec.get('billing') == 'unknown' else round(chain_usd, 2)
    result['available_in_hours'] = available_at
    action = 'start'
    if worker:
        if worn:
            action = 'rotate'
        else:
            action = 'resume' if resumes else 'switch'
    result['action'] = action
    if five:
        # Round 7 (user 2026-11-09): quality is the continuous fit of the axis scores at this effort to the demand, and
        # nothing else: no route order, vectors, graded ability, effort penalty, sprint quality or step-down rule (the
        # effort's extra burn is priced in its usd and its gain in the effort-scaled axes). The economy decides.
        _, scaled = five_axis_level(axes, task['five_axis_demand'], five_axis_effort_ratio(five_info, policy, five_curves, eff))
        fit, axis_rows = five_axis_fit(scaled, task['five_axis_demand'], task['five_axis_targets'], float(five['shortfall_weight']))
        result['components'].update(five_axis_level=round(effective, 4),
                                    five_axis_shortfall=round(sum(r['demand'] * r['shortfall'] for r in axis_rows.values())
                                                              / max(1, sum(r['demand'] for r in axis_rows.values())), 4))
        result['five_axis_demand'] = task['five_axis_demand']
        result['five_axis_fit'] = axis_rows
        ability_score = 0.0
        effort_scale = burn_scale(policy, curves, eff)
        sprint_sunk = (binding or {}).get('reset_sunk_weight', 0.0)
        usd_rate = 0.0 if spec.get('unlimited') or spec.get('unmetered') or spec.get('billing') == 'unknown' else \
            model_usd_per_hour(model, info) * max(effort_scale, 0.0)
        result['five_axis_price'] = {'usd_per_hour': round(usd_rate, 4), 'sprint_sunk': sprint_sunk}
    else:
        fit = fit_score(kind, model, capabilities, accounts)
        if (route.get('preferred') and canonical(route['preferred'][0], accounts) == 'dot'
                and (task['local_only'] or 'dot_text' in ctx['unavailable']) and model != 'dot'):
            fit = fit_score(kind, model, {'routes': {kind: {**route, 'preferred': route['preferred'][1:]}}}, accounts)
        visual = info.get('visual')
        if (task['needs_visual'] and kind != 'visual_analysis' and not route.get('capability_fit')
                and isinstance(visual, (int, float)) and not isinstance(visual, bool)):
            fit = (fit + visual) / 2     # the user's visual ability order counts alongside the work kind's fit
        if task['profile'] is not None and (not route.get('strict_order') or route.get('capability_fit')):
            # A user-ranked strict order (visual_analysis) stays as stated; elsewhere the profile's needs share the fit.
            # Route fit is not capped. Listed place, ability, the capability vector and the subjective vector decide.
            vfit = vector_fit(task['requirements'], effort_scaled_capabilities(info, policy, curves, eff))
            s_weights = task.get('subjective_weights') or {}
            s_cfg = (policy.get('profile') or {}).get('subjective') if isinstance(info.get('subjective'), dict) else None
            result['components']['route_fit'] = round(fit, 3)
            result['components']['vector_fit'] = round(vfit, 3)
            if s_weights and isinstance(s_cfg, dict):
                subj_scale = min(1.0, effective / high_ability) if high_ability > 0 else 1.0
                sfit = subjective_fit(s_weights, {k: v * subj_scale for k, v in subjective_vector(info).items()})
                result['components']['subjective_fit'] = round(sfit, 3)
                w_s = float(s_cfg.get('weight', 0.65))
                w_v = float(s_cfg.get('vector_weight', 0.2))
                w_r = float(s_cfg.get('route_weight', max(0.0, 1.0 - w_s - w_v)))
                fit = w_r * fit + w_v * vfit + w_s * sfit
            else:
                weight = (policy.get('profile') or {}).get('vector_weight', 0.5)
                fit = (1 - weight) * fit + weight * vfit
        if task.get('realtime_loop') and resp_val is not None:
            if task.get('needs_gui') and not route.get('capability_fit'):
                cu_cap = (capabilities['routes'].get('computer_use') or {}).get('capability_fit') or {'gui': 1.0}
                gui_fit = capability_score(cu_cap, model, accounts)
                result['components']['gui_fit'] = round(gui_fit, 3)
                fit = 0.5 * fit + 0.5 * gui_fit
            w_resp = clamp(float(responsiveness_config(policy).get('weight', 0.35)), 0.0, 1.0)
            fit = (1.0 - w_resp) * fit + w_resp * resp_val
        cfg = demand_config(policy)
        sunk_weight = (binding or {}).get('reset_sunk_weight', 0.0)
        sprint_sunk = 0.0 if task['difficulty'] == 'routine' else sunk_weight
        effort_scale = burn_scale(policy, curves, eff)
        # Near a reset with surplus quota the extra burn of a higher effort is quota that would lapse, so it is not charged
        # on non-routine work; routine (depth 1) work keeps its effort and overqualification discipline.
        effort_penalty = round(policy.get('extra_effort_penalty', 0.0) * max(0.0, effort_scale - 1.0) * (1.0 - sprint_sunk), 4)
        if effort_penalty:
            result['components']['effort_penalty'] = effort_penalty
        effort_gain = max(0.0, effective - high_ability)
        saturation = float((policy.get('profile') or {}).get('saturation_span', 0.3))
        if sprint_sunk > 0 and effort_gain > 0:
            sprint_quality = round(float(cfg['sprint_effort_weight']) * sprint_sunk * clamp(effort_gain / saturation, 0, 1), 4)
            if sprint_quality:
                result['components']['sprint_quality'] = sprint_quality
        # Effort buys ability credit only up to what the task asks of it: above High, up to the required ability. A task
        # that High already satisfies gets nothing from xhigh/max, and the more it demands the more of the curve counts.
        credited = min(effective, max(high_ability, target))
        shortfall = max(0.0, target - effective)
        if shortfall:
            result['components']['shortfall'] = round(float(cfg['shortfall_weight']) * shortfall, 4)
        result['components']['required_ability'] = round(target, 3)
        span = policy.get('ability_span', 0.6)
        if kind in ('frontend_implementation', 'complex_build_plan', 'direction_reasoning') and not task['high_precision']:
            # Once sufficient reasoning is available, UI and planning/scheme quality are decided by route and subjective fit.
            span = (policy.get('profile') or {}).get('saturation_span', 0.3)
        # Graded ability counts from the difficulty's score origin, not from the gate: the measured floors only decide who may
        # take the work, so lowering a floor does not flatten the quality difference between the models above it.
        # Never below the gate: a floor raised above the shipped origin (the tray edits floors) must not hand the models
        # that only just clear it a free share of the ability score.
        origin = max(floor, float((policy.get('ability_score_origin') or {}).get(task['difficulty'], floor)))
        ability_score = clamp((credited - origin - ((gate - floor) if task['high_precision'] else 0.0)) / span, 0, 1)
        if task['high_precision']:
            result['components']['quality_bonus'] = round(float(policy.get('high_precision_effort_weight', 0.14)) * effort_gain, 4)
        if task['minimum_sufficient']:
            # Ordinary profiled work: meeting the floor is sufficient, so every model at or above it gets full ability
            # (2026-10-27: Flash at exactly the routine floor scored 0 and lost to anything 0.3 above it), and list price
            # is charged for ability beyond floor + saturation_span, so a sufficient cheaper model wins when quota is plentiful.
            config = policy.get('profile') or {}
            span = config.get('saturation_span', 0.3)
            ability_score = 1.0 if effective >= gate else 0.0
            # Excess is measured from the task's required ability, not the bare floor: heavier properties raise the
            # saturation interval, so a strong model's surplus shrinks as the work gets harder.
            excess = clamp((effective - target - span) / config.get('overqualification_span', 0.6), 0, 1)
            usd = 0.0 if spec.get('unlimited') or spec.get('unmetered') else model_usd_per_hour(model, info) * max(effort_scale, 0.0)
            # The price factor is no longer capped at the reference price (2026-10-27), so a $22.6/h model pays 2.26x what a
            # $10/h one does. GUI work keeps the old cap, and above reasoning_depth 1 is exempt: tier measures reasoning, and
            # the GUI specialist is what that work needs. A small GUI task stays open to Sol and Flash (computer_use route).
            price = 1.0 if spec.get('billing') == 'unknown' else usd / config.get('overqualification_ref_usd_per_hour', 10.0)
            gui = task['kind'] == 'computer_use' or bool((task['profile'] or {}).get('needs_gui'))
            gui_capable = capability_vector(info).get('gui', 0.0) >= 0.7
            price = min(1.0, price) if (gui and gui_capable) else min(config.get('overqualification_price_cap', 3.0), price)
            if ((gui and gui_capable) or kind == 'frontend_implementation') and (task['profile'] or {}).get('reasoning_depth', 1) >= 2:
                price = 0.0
            overqualified = round(config.get('overqualification_weight', 0.08) * excess * price * (1.0 - sprint_sunk), 4)
            if overqualified:
                result['components']['overqualification'] = overqualified
        if task['difficulty'] != 'routine' and effective < high_ability:
            # On non-routine work (depth >= 2), stepping below High (medium/low) sacrifices thinking depth even when a high
            # base tier still clears the floor; High therefore leads under normal quota and steps down only under scarcity.
            ability_score = clamp(ability_score - 0.2 * (high_ability - effective) / policy.get('ability_span', 0.6), 0, 1)
    result['components'].update({
        'fit': round(fit, 3),
        'ability': round(ability_score, 3),
        'continuity': 0.0,
        'opportunity_cost': round(opportunity, 4)})
    if worker and not worn:
        result['components']['continuity'] = 1.0 if action == 'resume' else 0.0
    return result


def _rate_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def resolve_opportunity_exchange(policy):
    """The one rate `window_cost` and plan metadata read.

    `scoring.opportunity_exchange.score_per_displaced_usd` / `score_per_displaced_worker_hour` win.
    The same names inside that object (`shortage_loss_per_usd` / `shortage_loss_per_worker_hour`) are the
    next fallback. Top-level `shortage_loss_*` keys are compatibility only. When a top-level key is also
    set and differs, the named rate still wins and the warning says the old key is deprecated.
    This does not rewrite the policy.
    """
    policy = policy if isinstance(policy, dict) else {}
    block = policy.get('opportunity_exchange') if isinstance(policy.get('opportunity_exchange'), dict) else {}
    resolved, sources, warnings = {}, {}, []
    for legacy, named in (('shortage_loss_per_usd', 'score_per_displaced_usd'),
                          ('shortage_loss_per_worker_hour', 'score_per_displaced_worker_hour')):
        named_value = _rate_number(block.get(named))
        label = f'opportunity_exchange.{named}'
        if named_value is None:
            named_value = _rate_number(block.get(legacy))
            label = f'opportunity_exchange.{legacy}'
        legacy_value = _rate_number(policy.get(legacy))
        if named_value is not None:
            resolved[legacy] = named_value
            sources[legacy] = label
            if legacy_value is not None and legacy_value != named_value:
                warnings.append(f'{legacy} is deprecated and was ignored ({legacy_value:g}); '
                                f'{label} ({named_value:g}) is the rate')
        elif legacy_value is not None:
            resolved[legacy] = legacy_value
            sources[legacy] = legacy
        else:
            resolved[legacy] = None
            sources[legacy] = None
    return resolved, warnings, sources


def exchange_rate(policy, key):
    """Score points charged per displaced USD, or per displaced worker-hour. Raises KeyError if neither source sets it."""
    resolved, _warnings, _sources = resolve_opportunity_exchange(policy)
    value = resolved.get(key)
    if value is None:
        raise KeyError(key)
    return value


def opportunity_exchange(policy):
    """Quality-versus-quota rate for plan metadata. Same numbers `exchange_rate` returns."""
    resolved, warnings, sources = resolve_opportunity_exchange(policy)
    per_usd = resolved.get('shortage_loss_per_usd')
    per_hour = resolved.get('shortage_loss_per_worker_hour')
    if per_usd is None or per_hour is None:
        missing = 'shortage_loss_per_usd' if per_usd is None else 'shortage_loss_per_worker_hour'
        raise KeyError(missing)
    block = policy.get('opportunity_exchange') if isinstance(policy, dict) and isinstance(policy.get('opportunity_exchange'), dict) else {}
    basis = block.get('basis') or (
        'opportunity_exchange.score_per_displaced_usd and score_per_displaced_worker_hour are this rate: '
        'score points per USD of expected displaced subscription capacity, and per displaced worker-hour '
        'when a window has no USD capacity. Top-level shortage_loss_* keys are a deprecated fallback.')
    return {'name': 'opportunity_exchange',
            'shortage_loss_per_usd': per_usd, 'shortage_loss_per_worker_hour': per_hour,
            'score_per_displaced_usd': per_usd, 'score_per_displaced_worker_hour': per_hour,
            'usd_per_score_point': round(1.0 / per_usd, 2) if per_usd else None,
            'worker_hours_per_score_point': round(1.0 / per_hour, 2) if per_hour else None,
            'source': sources,
            'warnings': warnings,
            'basis': basis,
            'cost_rule': 'Eligibility uses the tightest applicable window: every window must clear its reserve. '
                         'Opportunity cost sums each window\'s shortage charge at this exchange. The binding window '
                         'is the one with the highest opportunity cost; capacity, expected percent and reset bonus follow it.'}


def harness_quality_factor(bundle, kind, policy, accounts=None):
    """Absolute harness quality multiplied into weighted fit and ability. Default 1.0.

    Most specific match wins: by_model[model][harness], by_work_kind[kind][harness], by_harness[harness],
    then the first matching rule, then default. This is not the Codex/Pi sibling preference; that stays
    in apply_harness_order because the order flips by work kind and only applies when a sibling is eligible.
    """
    table = policy.get('harness_factors') if isinstance(policy.get('harness_factors'), dict) else {}
    harness = (bundle or {}).get('harness') or bundle_harness(bundle, (bundle or {}).get('account'), (bundle or {}).get('id'))
    names = lookup_names(str((bundle or {}).get('model') or ''), accounts)

    def as_factor(value):
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    by_model = table.get('by_model') or {}
    found = next((as_factor((by_model.get(name) or {}).get(harness)) for name in names if name in by_model), None)
    if found is None:
        found = as_factor(((table.get('by_work_kind') or {}).get(kind) or {}).get(harness))
    if found is None:
        found = as_factor((table.get('by_harness') or {}).get(harness))
    if found is None:
        for rule in table.get('rules') or ():
            if not isinstance(rule, dict):
                continue
            if rule.get('harness') and rule.get('harness') != harness:
                continue
            if rule.get('work_kind') and rule.get('work_kind') != kind:
                continue
            if rule.get('model') and rule.get('model') not in names:
                continue
            prefix = rule.get('model_prefix')
            if prefix and not any(name.lower().startswith(str(prefix)) for name in names):
                continue
            found = as_factor(rule.get('factor'))
            if found is not None:
                break
    if found is None:
        found = as_factor(table.get('default'))
    return 1.0 if found is None else found


def fit_ability_value(components, weights):
    return float(weights.get('fit', 0) or 0) * float(components.get('fit') or 0) + float(weights.get('ability', 0) or 0) * float(components.get('ability') or 0)


def harness_factor_report(policy):
    table = policy.get('harness_factors') if isinstance(policy.get('harness_factors'), dict) else {}
    default = table.get('default', 1.0)
    return {'default': float(default) if isinstance(default, (int, float)) and not isinstance(default, bool) else 1.0,
            'applies_to': 'weighted fit and ability',
            'rules': [rule for rule in (table.get('rules') or []) if isinstance(rule, dict)],
            'basis': table.get('basis') or ''}


def rank(task, usage, policy, accounts, capabilities, now, unavailable=(), overrides=(), records=(), host_policy_config=None):
    ctx = {'policy': policy, 'accounts': accounts, 'capabilities': capabilities, 'usage': usage, 'now': now,
           'unavailable': set(unavailable), 'bundles': bundles(accounts),
           'host_policy': host_policy.load_policy() if host_policy_config is None else host_policy_config,
           'overrides': list(overrides), 'records': list(records or ()),
           'launch_failures': launch_failures.active() if launch_failures.enabled() else {},
           'model_latches': model_latch.active(now=now),
           'provider_retries': provider_retry.active(now=now)}
    ctx['bundles'].update(named_effort_bundles(task, ctx))
    five = five_axis_config(policy) if accounts.get('_five_axis') else None     # set only by five_axis_overlay
    if five:
        ctx['five_axis'] = five
        demand, demand_source = five_axis_demand(task)
        targets = five_axis_targets(demand, accounts['_five_axis'].get('anchor_value', FIVE_AXIS_ANCHOR_VALUE))
        task = {**task, 'five_axis_demand': demand, 'five_axis_demand_source': demand_source, 'five_axis_targets': targets}
    # A bundle's `when` scope names a difficulty or a reasoning depth: not read on the five-axis path (round 7).
    candidates = [evaluate_bundle(b, task, ctx) for b in ctx['bundles'].values()
                  if five or bundle_applies(b, task) or exact_pin(task, b, row_key(b['model'], accounts))]
    eligible = [c for c in candidates if not c['excluded']]
    # Weighted fit and ability are the value of this model on this harness. harness_factor (default 1) scales
    # that value only. Opportunity cost is already in score units at the opportunity_exchange rate, so it is
    # subtracted as it is. No normalization over the candidate set.
    weights = dict(five['weights'] if five else policy['weights'])
    weights['continuity'] *= policy['coupling_continuity_multiplier'][task['coupling']] if task['worker'] else 0
    kind = task['kind']
    for c in eligible:
        parts = {k: c['components'][k] for k in weights}
        quality = 1.0 if five else harness_quality_factor(c['bundle'], kind, policy, accounts)
        c['components']['harness_factor'] = round(quality, 4)
        value = fit_ability_value(parts, weights)
        score = (sum(weights[k] * parts[k] for k in weights) + c['components'].get('free_usage', 0)
                 + c['components'].get('boost', 0) + c['components'].get('reset_bonus', 0)
                 + c['components'].get('quality_bonus', 0) + c['components'].get('fast_bonus', 0)
                 + c['components'].get('sprint_quality', 0)
                 - c['components']['opportunity_cost']
                 - c['components'].get('effort_penalty', 0) - c['components'].get('overqualification', 0)
                 - c['components'].get('shortfall', 0))
        if quality != 1.0:
            score += (quality - 1.0) * value
        c['score'] = round(score, 4)
        if c['available_in_hours']:
            if five:
                wait_cost = five_axis_wait_cost(c['available_in_hours'], task['five_axis_demand'], task['five_axis_targets'], policy)
                c['components']['wait_cost'] = wait_cost
                c['score'] = round(c['score'] - wait_cost, 4)
            else:
                c['score'] = round(c['score'] - policy['deferred_start_penalty'], 4)
    if five:
        # Equal scores go to the candidate drawing on the least scarce quota, then the higher axis level; the harness
        # order only separates siblings of one model, account and effort, as a tie-break and never in the score.
        harness_places(eligible, kind, policy, capabilities, (task['profile'] or {}).get('interaction_style'))
        eligible.sort(key=five_axis_order)
    else:
        apply_harness_order(eligible, kind, policy, capabilities, (task['profile'] or {}).get('interaction_style'), weights)
        eligible.sort(key=lambda c: -c['score'])
    exchange = opportunity_exchange(policy)
    return candidates, eligible, {'weights': {k: round(v, 3) for k, v in weights.items()},
                                  'shortage_loss_per_usd': exchange['shortage_loss_per_usd'],
                                  'shortage_loss_per_worker_hour': exchange['shortage_loss_per_worker_hour'],
                                  'opportunity_exchange': exchange,
                                  'harness_factors': harness_factor_report(policy)}


def sibling_key(candidate):
    """Bundles that differ only in harness (Codex or Pi running the same model on the same account)."""
    bundle = candidate['bundle']
    return bundle['account'], bundle['model'], bundle.get('effort')


def harness_group(candidate):
    """Siblings that would also do the same thing: resuming the owner is not the same choice as starting its twin."""
    return sibling_key(candidate) + (candidate['action'],)


def harness_places(eligible, kind, policy, capabilities, style=None):
    """Five-axis path: each candidate's place behind its best sibling (same account, model, effort and action) in the
    harness order apply_harness_order reads, as `harness_place`; the score is not touched."""
    by_style = ((policy.get('profile') or {}).get('harness_by_style') or {}).get(style) if style else None
    order = (capabilities['routes'].get(kind) or {}).get('harness_order') or by_style or policy.get('harness_order') or []

    def place(candidate):
        b = candidate['bundle']
        harness = b.get('harness') or bundle_harness(b, b.get('account'), b.get('id'))
        return order.index(harness) if harness in order else order.index(b.get('provider')) if b.get('provider') in order else len(order)

    groups = {}
    for c in eligible:
        groups.setdefault(harness_group(c), []).append(c)
    for siblings in groups.values():
        best = min(place(c) for c in siblings)
        for c in siblings:
            c['components']['harness_place'] = place(c) - best


def apply_harness_order(eligible, kind, policy, capabilities, style=None, weights=None):
    """Sibling harness preference, as a coefficient on the fit/ability value.

    Order is the route's harness_order, else the profile style (`scoring.profile.harness_by_style`), else the
    policy default. It ranks only bundles that share account, model, effort and action. The best sibling keeps
    factor 1 and its score, so this never changes which model wins. Each place behind subtracts exactly
    `harness_step` (the historical flat penalty). The same quantity as a coefficient on the already
    harness_factor-scaled fit/ability value is `harness_order_factor = 1 - harness_step * places / value`.
    A static harness_factors table cannot replace this: the order flips by work kind and style, and a lone
    harness is not penalized just because a preferred sibling is absent.
    """
    by_style = ((policy.get('profile') or {}).get('harness_by_style') or {}).get(style) if style else None
    order = (capabilities['routes'].get(kind) or {}).get('harness_order') or by_style or policy.get('harness_order') or []
    step = policy.get('harness_step', 0.03)
    weights = weights or {}

    def place(candidate):
        b = candidate['bundle']
        harness = b.get('harness') or bundle_harness(b, b.get('account'), b.get('id'))
        if harness in order:
            return order.index(harness)
        prov = b.get('provider')
        return order.index(prov) if prov in order else len(order)

    groups = {}
    for c in eligible:
        groups.setdefault(harness_group(c), []).append(c)
    for siblings in groups.values():
        if len(siblings) < 2:
            for c in siblings:
                c['components'].setdefault('harness_order_factor', 1.0)
            continue
        best = min(place(c) for c in siblings)
        for c in siblings:
            behind = place(c) - best
            if not behind:
                c['components']['harness_order_factor'] = 1.0
                continue
            penalty = step * behind
            quality = c['components'].get('harness_factor', 1.0) or 1.0
            scaled = quality * fit_ability_value(c['components'], weights)
            c['components']['harness_order_factor'] = round(1.0 - penalty / scaled, 4) if scaled > 0 else None
            c['score'] = round(c['score'] - penalty, 4)
            c['components']['harness'] = round(-penalty, 3)


def _flag_present(mapping, key):
    return isinstance(mapping, dict) and key in mapping and mapping.get(key) is not None


def resolve_modality_needs(needs, profile, implies):
    """Resolve visual and local_only.

    An explicit caller true or false wins, including false. A profile value fills a flag the caller
    left unset (a missing profile key stays unset). Route implies fill only what both left unset.
    needs_gui still forces visual unless the caller explicitly set visual=false.
    """
    needs = needs if isinstance(needs, dict) else {}
    profile = profile if isinstance(profile, dict) else None
    implies = implies if isinstance(implies, dict) else {}
    if _flag_present(needs, 'visual'):
        visual = bool(needs.get('visual'))
    elif profile is not None and (_flag_present(profile, 'needs_visual') or _flag_present(profile, 'needs_gui')):
        visual = bool(profile.get('needs_visual') or profile.get('needs_gui'))
    else:
        visual = bool(implies.get('visual'))
    if profile is not None and profile.get('needs_gui') and not _flag_present(needs, 'visual'):
        visual = True
    if _flag_present(needs, 'local_only'):
        local_only = bool(needs.get('local_only'))
    elif profile is not None and _flag_present(profile, 'local_only'):
        local_only = bool(profile.get('local_only'))
    else:
        local_only = bool(implies.get('local_only'))
    return {'visual': visual, 'local_only': local_only}


def dot_may_take(kind, needs, capabilities, profile=None):
    """Whether Dot is a candidate. It has its own computer and several ways to receive data and return files, so every route
    admits it except specialist routes that do not list it, work that needs vision, and work the caller or classifier
    marks needs.local_only (it must run on this machine). needs.independent_text is no longer read: it used to be the gate. dot_execution marked
    local-only is a contradiction, so Dot is not offered and the plan asks the user.
    Explicit false is kept. Route implies fill a flag only when the caller and the profile left it unset."""
    route = capabilities['routes'].get(kind) or {}
    resolved = resolve_modality_needs(needs, profile, route.get('implies') or {})
    if resolved.get('local_only') or resolved.get('visual'):
        return False
    if route.get('restrict_models') is True and route.get('preferred'):
        return 'dot' in route['preferred']
    return True


def named_pin(constraints, task, accounts):
    """(model the user named, effort the user named) from the constraints or the pinned_* request fields.

    The effort comes from its own field, else from the name (`model:effort`). A name that only a family guess could
    place stays itself: naming gemini-2.5-pro never launches the flagship the family folds to."""
    effort = constraints.get('effort') or task.get('pinned_effort')
    effort = effort.strip().lower() if isinstance(effort, str) and effort.strip() else None
    raw = constraints.get('model') or task.get('pinned_model')
    if not raw:
        return constraints.get('bundle') or None, effort
    resolved = resolve_model(raw, accounts)
    name = resolved['key'] if resolved['source'] in DIRECT_SOURCES else split_model_effort(raw)[0]
    return name, effort or resolved['effort']


def normalize_task(raw, policy, capabilities, accounts):
    task = dict(raw or {})
    profile = validate_caller_profile(task['profile']) if task.get('profile') is not None else None
    needs = dict(task.get('needs')) if isinstance(task.get('needs'), dict) else {}
    if not _flag_present(needs, 'gui') and _flag_present(task, 'needs_gui'):
        needs['gui'] = bool(task['needs_gui'])
    if not _flag_present(needs, 'visual') and _flag_present(task, 'needs_visual'):
        needs['visual'] = bool(task['needs_visual'])
    if not _flag_present(needs, 'local_only') and _flag_present(task, 'local_only'):
        needs['local_only'] = bool(task['local_only'])
    if _flag_present(needs, 'realtime_loop'):
        realtime_loop = bool(needs['realtime_loop'])
    elif _flag_present(task, 'realtime_loop'):
        realtime_loop = bool(task['realtime_loop'])
    elif profile is not None and 'realtime_loop' in profile:
        realtime_loop = bool(profile['realtime_loop'])
    else:
        realtime_loop = False
    if _flag_present(needs, 'fast'):
        wants_fast = bool(needs['fast'])
    elif _flag_present(task, 'fast'):
        wants_fast = bool(task['fast'])
    elif _flag_present(task, 'wants_fast'):
        wants_fast = bool(task['wants_fast'])
    elif profile is not None and 'wants_fast' in profile:
        wants_fast = bool(profile['wants_fast'])
    else:
        wants_fast = False
    if _flag_present(needs, 'read_only_review'):
        read_only_review = bool(needs['read_only_review'])
    elif _flag_present(task, 'read_only_review'):
        read_only_review = bool(task['read_only_review'])
    elif profile is not None and 'read_only_review' in profile:
        read_only_review = bool(profile['read_only_review'])
    else:
        read_only_review = False
    if profile is not None:
        profile = {
            **profile,
            'realtime_loop': realtime_loop,
            'wants_fast': wants_fast,
            'read_only_review': read_only_review,
        }
    kind = task_kind({**task, 'profile': profile}, capabilities, policy)
    derived = profile_difficulty(profile, policy) if profile else None
    difficulty = task.get('difficulty') or derived or default_difficulty(kind)
    source = 'caller' if task.get('difficulty') else 'profile' if derived else 'route_default'
    if difficulty not in policy['ability_floor']:
        raise ValueError('difficulty must be one of ' + ', '.join(policy['ability_floor']))
    chain = task.get('chain') if isinstance(task.get('chain'), dict) else {}
    coupling = chain.get('coupling') or 'low'
    if coupling not in policy['coupling_continuity_multiplier']:
        raise ValueError('chain.coupling must be low, medium or high')
    hours = float(task.get('expected_hours') or policy['default_hours'][difficulty])
    constraints = task.get('constraints') if isinstance(task.get('constraints'), dict) else {}
    constraint_model, constraint_effort = named_pin(constraints, task, accounts)
    if profile and needs.get('critical') is None:
        needs['critical'] = profile['error_cost'] >= 3
    implies = capabilities['routes'][kind].get('implies') or {}
    # Caller false stands. The profile fills an unset flag. Route implies fill only what both left unset.
    needs.update(resolve_modality_needs(needs, profile, implies))
    critical = bool(needs.get('critical'))
    depth = profile['reasoning_depth'] if profile else None
    div = (profile.get('divergence') if profile else None) or default_divergence(
        kind, profile['interaction_style'] if profile else None)
    mechanical_runbook = bool(profile and depth == 1 and profile['code_complexity'] == 0 and div == 1)
    unlocked = ((difficulty in policy.get('effort_unlock_difficulties', ('coupled', 'frontier'))
                 or (critical and not mechanical_runbook)
                 or (depth is not None and depth >= 3)
                 or bool(profile and depth >= 2 and profile['hallucination_risk'] >= 3
                         and (div >= 2 or profile['code_complexity'] >= 2))))
    high_precision = bool(unlocked and (difficulty == 'frontier' or (critical and not mechanical_runbook) or
                          (profile and profile['hallucination_risk'] >= 3 and
                           (((div >= 2 or profile['code_complexity'] >= 2)
                             and kind not in ('complex_build_plan', 'direction_reasoning')
                             and profile['interaction_style'] != 'advisory')
                            or (depth >= 3 and profile['code_complexity'] >= 3)))))
    worker = dict(chain['worker']) if isinstance(chain.get('worker'), dict) else None
    if worker and worker.get('agent_id'):
        hist = session_state(worker['agent_id'])
        for k, v in hist.items():
            if k in ('compactions', 'compacted_tokens', 'historical_context_tokens') and isinstance(v, (int, float)):
                worker[k] = max(worker.get(k) or 0, v)
            else:
                worker.setdefault(k, v)
    if _flag_present(needs, 'gui'):
        needs_gui = bool(needs['gui'])
    elif _flag_present(task, 'needs_gui'):
        needs_gui = bool(task['needs_gui'])
    elif profile is not None and _flag_present(profile, 'needs_gui'):
        needs_gui = bool(profile['needs_gui'])
    else:
        needs_gui = bool(kind == 'computer_use')
    return {'kind': kind, 'difficulty': difficulty, 'difficulty_source': source, 'hours': hours,
            'profile': profile, 'profile_difficulty': derived, 'divergence': div,
            'realtime_loop': realtime_loop, 'needs_gui': needs_gui,
            'requirements': profile_requirements(profile, policy) if profile else None,
            'subjective_weights': subjective_weights({**profile, 'divergence': div}, kind) if profile else None,
            'demand': task_demand(profile, difficulty, critical, div, policy, high_precision,
                                  ability_floor(policy, capabilities['routes'][kind], difficulty)),
            'effort_unlocked': unlocked,
            'high_precision': high_precision,
            'minimum_sufficient': bool(profile and depth <= 2 and profile['code_complexity'] < 3
                                       and div == 1 and (profile['error_cost'] <= 2 or mechanical_runbook)
                                       and (not critical or mechanical_runbook)),
            'wants_fast': wants_fast,
            'read_only_review': read_only_review,
            'force_fast': bool(task.get('force_fast') or needs.get('force_fast')),
            'disable_fast': bool(task.get('disable_fast') or needs.get('disable_fast')),
            'pinned_bundle': task.get('pinned_bundle'),
            'chain_hours': float(chain.get('total_hours') or hours), 'coupling': coupling, 'worker': worker,
            'handoff_ready': bool(chain.get('handoff_ready')), 'needs_visual': needs['visual'],
            'local_only': bool(needs.get('local_only')), 'critical': critical,
            'file_transfer': bool(needs.get('file_transfer')), 'outbound_ok': needs.get('outbound_ok'),
            'constraint_bundle': constraints.get('bundle') if isinstance(constraints.get('bundle'), str) else None,
            'constraint_model': constraint_model, 'constraint_effort': constraint_effort,
            'metered_ok': bool(task.get('metered_ok')), 'include_unverified': bool(task.get('include_unverified')),
            'can_wait_hours': float(task.get('can_wait_hours') or 0), 'chain_id': chain.get('id'),
            'cwd': task.get('cwd')}


def named_pin_block(task, candidates):
    """Why what the user named cannot run, one line per row; [] when the task names nothing."""
    if not (task.get('constraint_model') or task.get('constraint_effort') or task.get('pinned_bundle')):
        return []
    off_pin = ('explicit model constraint', 'explicit effort constraint')
    rows = [c for c in candidates if c['id'] == task.get('pinned_bundle')
            or (not task.get('pinned_bundle') and not any(r in off_pin for r in c['excluded']))]
    named = task.get('pinned_bundle') or ':'.join(x for x in (task.get('constraint_model'), task.get('constraint_effort')) if x)
    rows = [c for c in rows if 'marked unavailable by the parent' not in c['excluded']] or rows
    if not rows:
        return [f'{named}: no configured bundle offers it']
    return [f"{named} via {c['id']}: {'; '.join(c['excluded'])}" for c in rows if c['excluded']][:4]


def plan(raw_task, usage, now=None, unavailable=(), records=None, accounts=None, capabilities=None, overrides=None,
         host_policy_config=None):
    accounts = accounts_override.normalize(accounts or load_accounts())
    capabilities = capabilities or load_capabilities(accounts=accounts)
    now = now or datetime.now(timezone.utc)
    usage = provider_inventory.merge_usage(usage, accounts, now)
    usage, accounts, applied = apply_overrides(load_overrides(now) if overrides is None else overrides, usage, accounts, now)
    accounts = five_axis_overlay(accounts)
    policy = accounts['scoring']
    task = normalize_task(raw_task, policy, capabilities, accounts)
    if task['worker'] is None and task['chain_id'] and records:
        for record in reversed(records):
            if record.get('chain') == task['chain_id'] and (record.get('bundle') or {}).get('bundle_id'):
                w = {'bundle': record['bundle']['bundle_id'], 'from_ledger': True}
                wid = record.get('worker_id') or (record.get('worker') or {}).get('agent_id')
                if wid:
                    w['agent_id'] = wid
                    w.update(session_state(wid))
                task['worker'] = w
                break
    if task['worker'] and task['worker'].get('bundle'):
        # A retired proxy id resumes as its auto bundle; an id that no longer exists means no continuity.
        import auto_bundles
        legacy = (accounts.get('_auto_bundles') or {}).get('legacy_aliases') or {}
        resolved = auto_bundles.resolve_bundle_id(task['worker']['bundle'], bundles(accounts), legacy)
        if resolved and resolved != task['worker']['bundle']:
            task['worker'] = {**task['worker'], 'bundle': resolved}
    candidates, eligible, system = rank(task, usage, policy, accounts, capabilities, now, unavailable, applied, records,
                                        host_policy_config)
    # Metered last resort: only when no subscription bundle is eligible, re-rank with checked metered bundles admitted.
    if not eligible and any(c['excluded'] == [METERED_LAST_RESORT] for c in candidates):
        task['metered_fallback'] = True
        candidates, eligible, system = rank(task, usage, policy, accounts, capabilities, now, unavailable, applied,
                                            records, host_policy_config)
    # A model named without an effort, too slow at every level it can run: its fastest level, with a warning.
    if (not eligible and task.get('realtime_loop') and task['constraint_model']
            and not task.get('constraint_effort') and not task.get('constraint_bundle')
            and any(c.get('too_slow') and len(c['excluded']) == 1 for c in candidates)):
        task['slow_pin_fallback'] = True
        candidates, eligible, system = rank(task, usage, policy, accounts, capabilities, now, unavailable, applied,
                                            records, host_policy_config)
        speeds = [c['components'].get('responsiveness') or 0.0 for c in eligible]
        for c in list(eligible):
            if (c['components'].get('responsiveness') or 0.0) < max(speeds):
                c['excluded'].append('a faster effort of the named model is available')
                eligible.remove(c)
    worker_id = (task['worker'] or {}).get('bundle')
    resume = next((c for c in candidates if c['id'] == worker_id), None)
    decision = {'task': {k: task[k] for k in ('kind', 'difficulty', 'critical', 'hours', 'chain_hours', 'coupling')}, 'system': system,
                'assumptions': assumptions(candidates, accounts)}
    exchange_warnings = list((system.get('opportunity_exchange') or {}).get('warnings') or [])
    if exchange_warnings:
        decision['warnings'] = exchange_warnings
    cwd_warnings = [f'{name}: cwd_regex and legacy allowed_cwd both apply (intersection)'
                    for name, spec in accounts['accounts'].items() if spec.get('cwd_regex') and spec.get('allowed_cwd')]
    if cwd_warnings:
        decision.setdefault('warnings', []).extend(cwd_warnings)
    state_policy = host_policy.load_policy() if host_policy_config is None else host_policy_config
    state_warnings = account_state.warnings(accounts.get('_discovery'), accounts['accounts'],
                                            lambda name: host_policy.account_allowed(state_policy, name))
    if state_warnings:
        decision.setdefault('warnings', []).extend(state_warnings)
    latch_warnings = model_latch.warnings(model_latch.active(now=now),
                                          lambda name: host_policy.account_allowed(state_policy, name))
    latch_warnings += provider_retry.warnings(provider_retry.active(now=now),
                                              lambda name: host_policy.account_allowed(state_policy, name))
    if latch_warnings:
        # First: the roll answer shows only the first few warnings, and a model that cannot start must be seen.
        decision['warnings'] = [*latch_warnings, *decision.get('warnings', [])]
    decision['task'].update(difficulty_source=task['difficulty_source'],
                            high_precision=task['high_precision'],
                            demand=task['demand'], max_recommended_effort=None)
    if task.get('wants_fast'):
        decision['task']['wants_fast'] = True
    if task.get('read_only_review'):
        decision['task']['read_only_review'] = True
    if task.get('realtime_loop'):
        decision['task']['realtime_loop'] = True
    if task.get('metered_fallback'):
        decision['task']['metered_fallback'] = True
    if task['profile']:
        decision['task'].update(profile=task['profile'], profile_difficulty=task['profile_difficulty'],
                                minimum_sufficient=task['minimum_sufficient'])
    if applied:
        decision['overrides'] = applied
    result = {'stage': 'plan', 'source': 'deterministic_allocator', 'observed_at': now.isoformat(), 'decision': decision,
              'ranking': sorted(candidates, key=lambda c: (c['score'] is None, -(c['score'] or 0))), 'usage': usage}
    if not eligible:
        closest = sorted((c for c in candidates if c['excluded']), key=lambda c: (len(c['excluded']), -c['components']['fit']))[:2]
        named_block = named_pin_block(task, candidates)
        decision.update({'action': 'none', 'needs_user_decision': True,
                         'reason': 'No eligible bundle. Closest: ' + ' | '.join(
                             f"{c['id']}: {'; '.join(c['excluded'])}" for c in closest)})
        # The floor is a hard gate at every difficulty, so nothing below it is offered instead. When a bundle that clears it
        # is held back only by quota, say when that quota returns, so the parent can wait rather than pick a lower tier.
        waits = sorted((h, c['id']) for c in candidates if c['excluded'] and (h := quota_wait_hours(c, usage)) is not None)
        five = bool(accounts.get('_five_axis'))       # no difficulty floor on the five-axis path (round 7)
        admitted = [c for c in candidates if not c.get('below_floor') and 'excluded by host policy' not in c['excluded']]
        if not admitted and not five:
            decision['reason'] = (f'No bundle on the usable accounts clears the {task["difficulty"]} floor '
                                  f'{task["demand"]["floor"]:g}; nothing below it is offered instead. ' + decision['reason'])
        if waits:
            hours, bundle_id = waits[0]
            decision['next_reset'] = {'bundle': bundle_id, 'hours': round(hours, 1),
                                      'at': (now + timedelta(hours=hours)).isoformat()}
            decision['reason'] += (f'. {bundle_id} is held back by quota that returns in {hours:.1f}h: wait, or decide otherwise.'
                                   if five else
                                   f'. Nothing below the {task["difficulty"]} floor is offered instead; {bundle_id} clears it '
                                   f'and its quota returns in {hours:.1f}h: wait, or decide otherwise.')
        if named_block:
            # What the user named cannot run. Say why and stop; nothing else stands in for it.
            decision['blocked'] = 'named_model_unavailable'
            decision.setdefault('warnings', []).extend(named_block)
            decision['reason'] = named_block[0]
            result['next'] = ('The model the user named cannot run now: ' + '; '.join(named_block)[:300]
                              + '. Tell the user; do not substitute another model.')
        result['choice'] = 'none'
        return result
    can_wait = task['can_wait_hours']
    ready = [c for c in eligible if (c['available_in_hours'] or 0) <= can_wait]
    pool = ready or eligible
    pinned_cand = (
        next((c for c in pool if c['id'] == task.get('pinned_bundle')), None)
        if task.get('pinned_bundle') else None
    )
    top = pinned_cand or pool[0]
    better = eligible[0]
    if better is not top and better['available_in_hours']:
        decision['better_if_waiting'] = {'bundle': better['id'], 'hours': round(better['available_in_hours'], 1),
                                         'score_gain': round(better['score'] - top['score'], 4)}
    forced = pinned_cand is not None
    if task['coupling'] == 'high' and resume is not None and not resume['excluded'] and resume['action'] == 'resume' and not forced:
        top, forced = resume, True        # a coupled chain finishes on its owner while that owner is not worn
    decision.update({'action': top['action'], 'needs_user_decision': False,
                     'continuity': 'high-coupling chain stays with its owner' if (forced and pinned_cand is None) else None})
    if (top['available_in_hours'] or 0) > can_wait:
        # can_wait_hours is a limit, not a preference: say so instead of quietly starting later than allowed
        decision.update({'needs_user_decision': True, 'wait_hours': round(top['available_in_hours'], 1),
                         'reason': (f"The chain owner {top['id']} can resume in {top['available_in_hours']:.1f}h"
                                    if forced else f"Nothing eligible can start within {can_wait:g}h; the best, {top['id']}, can start in {top['available_in_hours']:.1f}h")
                         + '. Wait, allow a longer wait, or mark it unavailable and replan.'})
    # A worker held only by the old route gives way when the user names something else.
    pin_moved = resume is not None and (
        ('explicit model constraint' in resume['excluded'] or 'explicit effort constraint' in resume['excluded'])
        or bool(task.get('pinned_bundle') and task['pinned_bundle'] != resume['id']))
    if (task['worker'] and not worker_worn(task['worker'], policy) and task['coupling'] == 'high'
            and resume is not None and resume['excluded'] and not pin_moved):
        decision.update({'needs_user_decision': True, 'drift_risk': 'high',
                         'resume_blocked_by': resume['excluded'],
                         'wait_hours': resume.get('wait_until_hours'),
                         'reason': 'The chain owner cannot continue now. Waiting keeps one owner; switching delivers sooner with handoff and drift risk.'})
    # The judge weighs models, not efforts, harnesses or accounts of one model: the margin is to the best other model.
    rival = next((c for c in pool if c is not top
                  and row_key(c['bundle']['model'], accounts) != row_key(top['bundle']['model'], accounts)), None)
    margin = round(top['score'] - rival['score'], 4) if rival is not None and not forced else None
    decision['margin'] = margin
    decision['needs_judgment'] = bool(margin is not None and margin < policy['needs_judgment_margin'])
    if top['bundle'].get('fast'):
        decision['fast'] = True
        decision['fast_reason'] = top['components'].get('fast_reason')
    if task.get('realtime_loop'):
        decision['responsiveness'] = ('default' if top['components'].get('responsiveness_source') == 'default'
                                      else top['components'].get('responsiveness'))
        slow_pin_warn = top['components'].get('realtime_slow_pin_warning')
        if slow_pin_warn:
            decision.setdefault('warnings', []).append(slow_pin_warn)
    decision.setdefault('reason', explain(top, system))
    if task.get('realtime_loop') and top['components'].get('realtime_slow_pin_warning'):
        warn_msg = top['components']['realtime_slow_pin_warning']
        if warn_msg not in (decision.get('reason') or ''):
            decision['reason'] = f"{decision['reason']}; {warn_msg}"
    top_model = row_key(top['bundle']['model'], accounts)
    offered = sorted({c['bundle'].get('effort') for c in candidates if row_key(c['bundle']['model'], accounts) == top_model
                      and c['bundle'].get('effort') in EFFORT_ORDER}, key=EFFORT_ORDER.index) or EFFORT_ORDER
    if accounts.get('_five_axis'):
        # No required ability on the five-axis path (round 7): the fit and the economy chose this effort.
        chosen = top['bundle'].get('effort')
        decision['task']['max_recommended_effort'] = chosen if chosen in EFFORT_ORDER else None
    else:
        saturating = saturating_effort(policy, model_curves(accounts, top['bundle']['model']), model_spec(accounts, top['bundle']['model']),
                                       task['demand']['required_ability'], offered)
        # The ceiling past which more thinking buys nothing for this task on the chosen model: High unless the task's
        # required ability needs more, None when even the model's top effort falls short.
        deeper = any(EFFORT_ORDER.index(e) > EFFORT_ORDER.index('high') for e in offered if e in EFFORT_ORDER)
        decision['task']['max_recommended_effort'] = (
            ('high' if not deeper else None) if saturating is None
            else 'high' if EFFORT_ORDER.index(saturating) <= EFFORT_ORDER.index('high') else saturating)
    result['choice'] = top['id']
    result['selected_bundle'] = dict(top['bundle'], bundle_id=top['id'], expected_usd=top.get('expected_usd'),
                                     expected_hours=top.get('expected_hours'))
    result['expected_usd'] = top.get('expected_usd')
    result['expected_hours'] = top.get('expected_hours')
    result['decision']['alternatives'] = [c['id'] for c in pool if c is not top][:3]
    if top['available_in_hours']:
        decision.update({'action': 'defer', 'start_after_hours': round(top['available_in_hours'], 1)})
    return result


SNAPSHOT_FULL, SNAPSHOT_COMPACT, SNAPSHOT_EXCLUDED = 6, 8, 16
SNAPSHOT_CAPACITY_KEYS = ('window', 'remaining', 'after', 'step_pct', 'chain_pct', 'capacity_usd', 'remaining_usd',
                          'expected_usd', 'reset_hours', 'end_hours', 'expires_first', 'demand_pct', 'demand_source',
                          'shortage_probability', 'scarcity_cost', 'opportunity_cost', 'reset_sunk_weight', 'reset_bonus',
                          'surplus_pct', 'stale')
SNAPSHOT_DECISION_KEYS = ('action', 'needs_user_decision', 'margin', 'needs_judgment', 'reason', 'alternatives', 'continuity',
                          'fast', 'fast_reason', 'responsiveness', 'wait_hours', 'start_after_hours', 'better_if_waiting', 'warnings')


def _round(value, places=4):
    if isinstance(value, bool) or not isinstance(value, float):
        return value
    return round(value, places)


def _snapshot_row(candidate, detail):
    """One candidate as it was scored. `detail` keeps every component, the binding window and the notes; the rest keep
    what the comparison table and radar read. Floats are rounded to 4 places to keep a ledger line small."""
    bundle = {k: v for k, v in (candidate.get('bundle') or {}).items() if v is not None and k != 'launch_model'}
    comps = candidate.get('components') or {}
    excluded = [str(r)[:160] for r in (candidate.get('excluded') or [])[:3]]
    row = {'id': candidate.get('id'), 'bundle': bundle, 'score': candidate.get('score'), 'action': candidate.get('action'),
           'excluded': excluded}
    if detail:
        row['components'] = {k: _round(v) for k, v in comps.items()}
        row.update({k: _round(candidate.get(k)) for k in ('expected_pct', 'chain_pct', 'expected_usd', 'chain_usd',
                                                           'expected_hours', 'available_in_hours') if candidate.get(k) is not None})
        cap = candidate.get('capacity')
        if isinstance(cap, dict):
            row['capacity'] = {k: _round(cap[k]) for k in SNAPSHOT_CAPACITY_KEYS if k in cap}
        notes = [str(n)[:200] for n in (candidate.get('notes') or [])[:4]]
        if notes:
            row['notes'] = notes
    elif excluded:
        row['components'] = {}
    else:
        keep = ('fit', 'ability', 'opportunity_cost', 'capability', 'divergence', 'global_rigor', 'module_precision',
                'responsiveness', 'responsiveness_source',
                'reset_bonus', 'boost', 'free_usage', 'overqualification', 'effort_penalty')
        row['components'] = {k: _round(comps[k]) for k in keep if k in comps and comps[k]}
        for k in ('fit', 'ability', 'opportunity_cost'):
            if k in comps:
                row['components'][k] = _round(comps[k])
    return row


def decision_snapshot(result, task=None):
    """The decision as the allocator made it, small enough for one ledger line, or None when `result` has no ranking.

    Holds the task demand, the scoring system in force (weights, opportunity exchange, harness factors) and the ranking
    with each candidate's component scores: every component for the best `SNAPSHOT_FULL` eligible candidates, the
    comparison-table fields for the next `SNAPSHOT_COMPACT`, and the first reasons for `SNAPSHOT_EXCLUDED` blocked ones.
    Readers replay this as written and never re-plan the task against later quota or configuration."""
    ranking = result.get('ranking') if isinstance(result, dict) else None
    if not isinstance(ranking, list):
        return None
    decision = result.get('decision') or {}
    eligible = [c for c in ranking if not c.get('excluded') and c.get('score') is not None]
    blocked = [c for c in ranking if c.get('excluded') or c.get('score') is None]
    system = decision.get('system') or {}
    exchange = system.get('opportunity_exchange') or {}
    rows = ([_snapshot_row(c, True) for c in eligible[:SNAPSHOT_FULL]]
            + [_snapshot_row(c, False) for c in eligible[SNAPSHOT_FULL:SNAPSHOT_FULL + SNAPSHOT_COMPACT]]
            + [_snapshot_row(c, False) for c in blocked[:SNAPSHOT_EXCLUDED]])
    d_task = decision.get('task') or {}
    task_in = task if isinstance(task, dict) else (result.get('task') if isinstance(result.get('task'), dict) else {})
    raw_profile = d_task.get('profile') if isinstance(d_task.get('profile'), dict) else (
        task_in.get('profile') if isinstance(task_in.get('profile'), dict) else None)
    clean_profile = {k: v for k, v in raw_profile.items() if k != 'reason'} if isinstance(raw_profile, dict) else None
    task_meta = {k: v for k, v in (
        ('objective', (task_in.get('objective') or '')[:200] or None),
        ('work_kind', task_in.get('work_kind') or d_task.get('kind')),
        ('profile', clean_profile),
    ) if v is not None}
    return {
        'v': 1, 'stage': result.get('stage'), 'source': result.get('source'), 'observed_at': result.get('observed_at'),
        'task': task_meta,
        'choice': result.get('choice'),
        'selected_bundle': result.get('selected_bundle'),
        'expected_usd': _round(result.get('expected_usd')),
        'decision': {**{k: decision[k] for k in SNAPSHOT_DECISION_KEYS if decision.get(k) is not None},
                     'task': {k: v for k, v in d_task.items() if k != 'profile'} | (
                         {'profile': clean_profile} if clean_profile is not None else {}),
                     'system': {'weights': system.get('weights'),
                                'shortage_loss_per_usd': system.get('shortage_loss_per_usd'),
                                'shortage_loss_per_worker_hour': system.get('shortage_loss_per_worker_hour'),
                                'opportunity_exchange': {k: exchange[k] for k in (
                                    'score_per_displaced_usd', 'score_per_displaced_worker_hour', 'usd_per_score_point',
                                    'worker_hours_per_score_point') if k in exchange},
                                'harness_factors': {k: (system.get('harness_factors') or {}).get(k)
                                                    for k in ('default', 'applies_to', 'rules')}}},
        'ranking': rows,
        'ranking_total': len(ranking), 'ranking_eligible': len(eligible),
        'ranking_omitted': {'eligible': max(0, len(eligible) - SNAPSHOT_FULL - SNAPSHOT_COMPACT),
                            'excluded': max(0, len(blocked) - SNAPSHOT_EXCLUDED)},
    }


def assumptions(candidates, accounts):
    notes = []
    meta = accounts.get('_measured')
    if not meta:
        notes.append('no fresh capacity.json from the tray capacity service: demand falls back to the straight-line schedule')
    elif meta['age_hours'] > 6:
        notes.append(f"usage readings are {meta['age_hours']:.0f}h old; is the tray capacity service running?")
    for c in candidates:
        if c['excluded']:
            continue
        spec = accounts['accounts'][c['bundle']['account']]
        basis = spec.get('burn_basis', '')
        if 'confidence: none' in basis or 'Placeholder' in basis:
            notes.append(f"{c['bundle']['account']}: consumption is a placeholder estimate")
        if parse_time(spec.get('expires_at')) and spec.get('expires_precision') == 'date':
            notes.append(f"{c['bundle']['account']}: expiry is a date only; end of day assumed")
    return sorted(set(notes))


def explain(top, system):
    parts = [f"{top['id']} ({top['action']}) scored {top['score']}"]
    cap = top.get('capacity')
    if cap:
        rem_usd_str = f" (${cap['remaining_usd']:.2f})" if cap.get('remaining_usd') is not None else ""
        exp_usd_str = f" (~${top['expected_usd']:.2f})" if top.get('expected_usd') is not None else ""
        parts.append(f"{cap['window']}: {cap['remaining']:.0f}% left{rem_usd_str}, work ~{top['expected_pct']}%{exp_usd_str} "
                     f"(chain ~{top['chain_pct']}%), other work wants ~{cap['demand_pct']:.0f}% ({cap['demand_source']}) "
                     f"before its {'expiry' if cap['expires_first'] else 'reset'} in {cap['end_hours']:.0f}h, "
                     f"opportunity cost {top['components']['opportunity_cost']}")
    exchange = (system or {}).get('opportunity_exchange') or {}
    if exchange.get('score_per_displaced_usd'):
        parts.append(f"value/cost exchange {exchange['score_per_displaced_usd']:g} score per displaced USD "
                     f"(${exchange['usd_per_score_point']:g} displaced USD per score point)")
    factor = top['components'].get('harness_factor')
    if isinstance(factor, (int, float)) and abs(factor - 1.0) > 1e-9:
        parts.append(f"harness factor {factor:g} on fit and ability")
    if top['components'].get('reset_bonus'):
        parts.append(f"includes an expiring-reset bonus of {top['components']['reset_bonus']:+.2f}")
    if top['bundle'].get('fast'):
        parts.append(f"fast mode enabled ({top['components'].get('fast_reason', 'plentiful_quota')}, "
                     f"bonus {top['components'].get('fast_bonus', 0):+.2f})")
    if top['components'].get('boost'):
        parts.append(f"includes a temporary user-override boost of {top['components']['boost']:+.2f}")
    if top['components'].get('responsiveness_source') == 'default':
        parts.append('responsiveness: default')
    elif top['components'].get('responsiveness') is not None:
        parts.append(f"responsiveness {top['components']['responsiveness']:.2f}")
    return '; '.join(parts)
