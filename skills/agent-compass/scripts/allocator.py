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
import sys

# Also support the path-loaded allocator and python -I entrypoints.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fast_modes
import host_policy
import override_store
import antigravity_telemetry
import provider_inventory
import accounts_override
import cwd_policy
from model_resolution import generic_model_spec, resolve_canonical_model

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
    """Overrides the user asked for that have not yet run out (`until`) or landed their reset, written by `route_judge.py override`."""
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


def session_log(agent_id, home=None):
    """(provider, log path) of a Paseo agent's harness session, or None. Paseo keeps the harness session id in
    ~/.paseo/agents/<workspace>/<agent id>.json; Codex, Claude and Pi keep one log per session."""
    home = Path(home) if home else Path.home()
    agents_root = home / '.paseo' / 'agents' if (home / '.paseo' / 'agents').is_dir() else home / 'agents'
    if not isinstance(agent_id, str) or any(c in agent_id for c in '*?[]/\\') or agent_id in ('.', '..'):
        return None
    try:
        record = next(agents_root.glob(f'*/{agent_id}.json'), None)
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
    inventory = provider_inventory.discover(home=home)
    row = (inventory.get('accounts') or {}).get(provider) or {}
    family = row.get('family') or provider_inventory.launcher_family(provider)
    root = Path(row['home']) if row.get('home') else home / ('.pi/agent' if family == 'pi' else '.' + str(family))
    patterns = {'codex': [f'sessions/**/rollout-*-{session}.jsonl', f'archived_sessions/rollout-*-{session}.jsonl'],
                'claude': [f'projects/*/{session}.jsonl'],
                'pi': [f'sessions/**/*{session}*.jsonl']}
    for pattern in patterns.get(family, []):
        found = next(root.glob(pattern), None)
        if found:
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
    return {'compactions': compactions, 'compacted_tokens': compacted,
            'historical_context_tokens': compacted + (context or 0),
            **({'context_tokens': context} if context is not None else {})}


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


def accounts_override_path(environ=None):
    env = os.environ if environ is None else environ
    configured = env.get('ROUTER_ACCOUNTS_OVERRIDE') or env.get('ROUTE_MANAGER_ACCOUNTS_OVERRIDE')
    if configured:
        return Path(configured).expanduser()
    state_dir = env.get('ROUTER_STATE_DIR')
    if state_dir:
        return Path(state_dir).expanduser() / 'accounts-override.json'
    return Path.home() / '.agent-compass' / 'accounts-override.json'


def local_fleet_path(environ=None):
    """Full account document for this machine, when one has been saved.

    The shipped accounts.json is a neutral template. A file named accounts.local.json in the router
    state directory replaces it as the base document; the sparse accounts-override.json still applies
    on top. A pinned override path means this process is a test or another home, so the real machine
    fleet stays out of it. An explicit load_accounts(path) never comes here.
    """
    env = os.environ if environ is None else environ
    configured = (env.get('ROUTER_ACCOUNTS_FILE') or '').strip()
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_file() else None
    # An explicit state directory is the whole router home for this process. Do not fall
    # through to the real user fleet when that directory has no saved document.
    state = (env.get('ROUTER_STATE_DIR') or '').strip()
    if state:
        candidate = Path(state).expanduser() / 'accounts.local.json'
        return candidate if candidate.is_file() else None
    override = (env.get('ROUTER_ACCOUNTS_OVERRIDE') or env.get('ROUTE_MANAGER_ACCOUNTS_OVERRIDE') or '').strip()
    if override:
        candidate = Path(override).expanduser().parent / 'accounts.local.json'
        return candidate if candidate.is_file() else None
    try:
        candidate = Path.home() / '.agent-compass' / 'accounts.local.json'
    except (OSError, RuntimeError):
        return None
    return candidate if candidate.is_file() else None


def fleet_document_path(environ=None):
    local = local_fleet_path(environ)
    return local if local is not None else REFERENCES / 'accounts.json'


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


def burn_scale(policy, curves, effort):
    """Quota one task spends at `effort` relative to High: the curve's `burn_mult`, else its `burn` over High's `burn`."""
    mult = (curves or {}).get('burn_mult') or {}
    if effort in mult:
        return float(mult[effort])
    reference = effort_value(policy, curves, 'burn', 'high', 1.0)
    return effort_value(policy, curves, 'burn', effort, reference) / reference


def unit_rate(spec, wid, model, effort, policy, curves=None, info=None, now=None):
    """Percent of the window this bundle spends on work that takes one worker-hour at High, or None when the account
    has no rate. With a `capacity_usd` for the window and a `usd_per_hour` for the model, that is the hour's list-price
    dollars over the window's dollars; otherwise the model's or account's percent-per-hour prior. Either way the effort
    curve's cost per task relative to High scales it: a higher effort spends about the same per hour but runs longer
    (Sol max 1.58 / 0.7 = 2.3 times High per task)."""
    reference = effort_value(policy, curves, 'burn', 'high', 1.0)
    scale = burn_scale(policy, curves, effort)
    capacity, usd = capacity_usd(spec, wid, now or datetime.now(timezone.utc)), (info or {}).get('usd_per_hour')
    if capacity is None and wid in ('gemini-weekly', 'gemini-5h') and usd is not None:
        capacity = window_capacity_usd(spec, wid, now)
    if capacity and usd:
        return 100 * usd / capacity * scale
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
            rate, source = max(0.0, 100 - state['remaining']) / elapsed, 'period average'
        if not isinstance(rate, (int, float)) or rate < 0:
            rate, source = 100 / state['hours'], 'straight-line schedule'
        if source == 'observed' and state.get('remaining', 100) < state.get('target_remaining', 0):
            rate = max(rate, float(policy.get('demand_pace_floor_ratio', 0.5)) * 100.0 / state['hours'])
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
    """API-equivalent USD capacity for a window: explicit `capacity_usd` when set, else the equivalent pool USD derived from
    the window's standard worker-hour burn prior (`antigravity` gemini-weekly $625, gemini-5h $182, 3p-weekly $88, 3p-5h $17.6)."""
    explicit = capacity_usd(spec, wid, now or datetime.now(timezone.utc))
    if explicit is not None:
        return explicit
    # Routing-scale prior for Google AI Ultra 20x (not billed USD); mirrors plan-templates.json antigravity.ultra20.
    derived = {'gemini-weekly': 625.0, 'gemini-5h': 182.0, '3p-weekly': 88.0, '3p-5h': 17.6}.get(wid)
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


def model_usd_per_hour(model, info=None):
    """List-price dollars of one active worker-hour at High (accounts `usd_per_hour`, else a prior)."""
    usd = (info or {}).get('usd_per_hour')
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


def window_cost(spec, wid, state, step, forecast, policy, result, now=None, step_usd=None, unit=None):
    """One window's share of the opportunity cost: the other work that spending `step` here would push past the
    window's end (expected shortage with the step minus without it), valued at list price against the window's USD
    capacity (explicit `capacity_usd`, else the derived pool) times the opportunity exchange
    (`exchange_rate`, preferring `scoring.opportunity_exchange`). Only a window with no USD
    capacity at all counts worker-hours, at this bundle's own burn (`unit`, %/worker-hour) or the account prior if
    higher: dividing a high-burn model's shortage by a cheap model's prior inflated it (Argon 1.6%/h over Flash
    0.2%/h was 8x, fixed 2026-10-27)."""
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
    if eff_cap_usd:
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


def canonical(model, accounts):
    return resolve_canonical_model(model, accounts.get('models', {}), accounts.get('aliases', {})) or model


def model_spec(accounts, model):
    """Score advertised names using calibrated rows, family priors, then a baseline."""
    name = canonical(model, accounts)
    value = accounts.get('models', {}).get(name)
    return value if isinstance(value, dict) else generic_model_spec()


def bundle_harness(bundle, account=None, bundle_id=None, *, family=None):
    """Execution harness family for a bundle (distinct from its billing account or gateway provider)."""
    explicit = str((bundle or {}).get('harness') or '').strip().lower()
    if explicit:
        return explicit
    prov = str((bundle or {}).get('provider') or account or '').strip().lower()
    bid = str(bundle_id or (bundle or {}).get('id') or '').strip().lower()
    launch_m = str((bundle or {}).get('launch_model') or '').strip().lower()
    model = str((bundle or {}).get('model') or '').strip().lower()
    family = family or (prov if prov in ('claude', 'codex', 'pi', 'opencode', 'grok', 'grok-bot',
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
            out[bundle_id] = {'id': bundle_id, 'account': account, 'provider': provider,
                              'harness': bundle_harness(bundle, account, bundle_id, family=row.get('family')),
                              'model': bundle['model'], 'effort': bundle.get('effort'),
                              **{k: bundle[k] for k in ('modeId', 'disabled', 'launch_model', 'when', 'discovery_unavailable', 'draws',
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
PROFILE_FLAGS = ('needs_visual', 'needs_gui', 'local_only')
DEPTH_BY_DIFFICULTY = {'routine': 1, 'hard': 2, 'coupled': 3, 'frontier': 4}
DIFFICULTY_ORDER = ('routine', 'hard', 'coupled', 'frontier')
HARD_BY_DEFAULT = ('debug_review', 'general_implementation', 'frontend_implementation', 'computer_use',
                   'exploration_analysis', 'research_packet')
DIVERGENT_WORDS = ('brainstorm', 'creative', 'novel', 'innovat', 'propose', 'formulate', 'scheme', 'roadmap',
                   'hypothesis', 'ideat', 'divergent', '制定方案', '设计方案', '新方案', '创造性', '发散', '创新',
                   '新方向', '提出假设', '路线图', '架构创新')
# The System-One task profile (spec 2026-10-19). Integers carry minimum/maximum, never an integer enum: Gemini's
# protobuf schema rejects integer enums ("enum[0] cannot be empty").
HOURS_RANGE = (0.05, 24.0)
CONFIDENCE_LEVELS = ('low', 'medium', 'high')
URGENCY_LEVELS = ('unknown', 'low', 'normal', 'high')
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
            '0 = none: ANY read-only code/release/diff review, independent audit, diagnosis that only proposes, running '
            'existing CLI/release/test commands, log or data exploration, text drafting; 1 = a small script, config/constant '
            'tweak, localized helper, or analysis scripts inside a research packet; 2 = a production module implemented, '
            'fixed or refactored with its unit tests; 3 = complex multi-module, concurrency, protocol or core-library changes.')},
        'context_scale': {'type': 'integer', 'minimum': 1, 'maximum': 3, 'description': (
            'Functional scope of the code or system that must be understood, NOT the number of paths the brief mentions. '
            'Ignore boilerplate: role docs, AGENTS.md, skill/README/runbook docs, prior reports or reviews to read, receipt/'
            'output/log paths, and a module\'s paired unit-test file. 1 = one module/component or one subsystem\'s runbook '
            '(+ its tests and the boilerplate); 2 = several coupled functional modules or subsystems whose interaction must be '
            'reasoned about, or a multi-source dataset/research panel; 3 = whole-repository or cross-system architecture.')},
        'divergence': {'type': 'integer', 'minimum': 1, 'maximum': 3, 'description': (
            '1 = convergent: a known target, spec, checklist, review scope or a fix whose cause is already identified; '
            '2 = multi-hypothesis: the cause of a bug, regression or performance problem is unknown and several candidate '
            'explanations must be tested, or several given design options must be compared; 3 = open-ended: formulate a new '
            'scheme, creative research hypotheses, strategy or architecture direction.')},
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
            'True when the work depends on this machine: local processes, SSH, large local files, its screen, or the '
            'local git checkout and test suite.')},
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
            'from topic or tone. Shown in the result so the caller can see it; it never changes the owner, the effort or fast mode.')},
        'reason': {'type': 'string', 'description': 'One concise sentence about the task itself that justifies the scores.'},
    },
    'required': list(PROFILE_INTEGERS) + list(PROFILE_FLAGS) + ['expected_hours', 'hours_confidence', 'urgency', 'interaction_style', 'reason'],
}
# kind -> (code_complexity, context_scale, hallucination_risk, error_cost, interaction_style) for the heuristic profile
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
GUI_WORDS = ('click', 'screenshot', ' gui', 'desktop app', 'mouse', '点击', '截图', '桌面', '鼠标')
# Same pattern as route_judge.OBJECTIVE_LOCAL_RUNTIME_RE. Kept here so the allocator does not import route_judge
# (route_judge.load_allocator execs this file). A caller true/false still wins over both patterns.
OBJECTIVE_LOCAL_RUNTIME_RE = re.compile(
    r'\b(?:ssh|ssh-agent|vps|localhost|127\.0\.0\.1|pytest|unittest|sqlite|daemon|worktree|adb|emulator|intranet|webhook|credentials?)\b',
    re.I,
)
LOCAL_MACHINE_RE = re.compile(
    r'(?:[A-Za-z]:[\\/]|~/|\\\\)|\b(?:working tree|git checkout|local repo|this repo|local daemon|local git)\b',
    re.I,
)
GENERIC_CALLER_KINDS = (None, 'general_implementation', 'tool_execution', 'debug_review', 'other_or_mixed')
FAST_REQUEST_WORDS = (
    'use fast mode', 'enable fast mode', 'in fast mode', 'with fast mode',
    'complete quickly', 'finish quickly', 'as quickly as possible', 'quick turnaround', 'fast turnaround',
    'urgent task', 'urgently', 'time-sensitive', 'time sensitive', 'asap',
    'pre-reset sprint', 'before quota resets', 'before the reset',
    '较快完成', '快速完成', '尽快完成', '尽早完成', '尽快出结果', '快速出结果',
    '加急', '赶时间', '时间紧', '火速', '重置前冲刺', '重置前消纳', '临期冲刺',
    '开启fast', '开启 fast', '使用fast', '使用 fast', '用fast', '用 fast', '允许使用fast', '允许使用 fast',
)


def infer_fast_preference(objective):
    """True when the objective explicitly asks for fast turnaround, pre-reset sprint, or fast mode."""
    text = f' {(objective or "").lower()} '
    return any(w in text for w in FAST_REQUEST_WORDS)


def default_difficulty(kind):
    return 'hard' if kind in HARD_BY_DEFAULT else 'routine'


READONLY_REVIEW_PHRASES = (
    '(read-only)', 'read-only review', 'read-only investigation', 'read-only code', 'read-only audit',
    'read-only release', 'read-only diff', 'read-only diagnostic', 'read-only inspection',
    'independent review', 'independent code and', 'independent audit',
    'no repo code edits', 'write no application code', 'write no production code',
    'do not edit repo', 'do not modify repo', 'do not change repo',
    '只读审阅', '只读代码审查', '只读审查', '只读排查', '独立代码审查', '独立审查', '不修改仓库代码',
)


def is_readonly_review_task(objective, profile=None):
    """True when the objective or scorer reason explicitly states a read-only code/release review or audit."""
    if isinstance(profile, dict) and profile.get('needs_gui'):
        return False
    text = f" {(objective or '').lower()} {((profile or {}).get('reason') or '').lower()} "
    return any(p in text for p in READONLY_REVIEW_PHRASES)


def infer_divergence(objective, kind=None, style=None):
    """Heuristic divergence level (1..3) when omitted by a legacy fixture or offline fallback."""
    if kind == 'computer_use':
        return 1
    if kind in ('complex_build_plan', 'direction_reasoning') or style == 'advisory':
        return 3
    text = f' {(objective or "").lower()} '
    if any(w in text for w in DIVERGENT_WORDS):
        return 3
    return 1


def validate_profile(raw):
    """A task profile with every field checked, or ValueError. `reason`, `work_kind`, and `divergence` may be omitted in legacy fixtures."""
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
        if not isinstance(raw.get(name), bool):
            raise ValueError(f'task.profile.{name} must be true or false')
        profile[name] = raw[name]
    # The hours/urgency estimate is optional: legacy fixtures and scorers that cannot produce it (Jev) leave it out.
    hours = raw.get('expected_hours')
    if hours is not None:
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not math.isfinite(hours):
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
    profile['reason'] = reason.strip()[:300] if isinstance(reason, str) else ''
    return profile


def infer_kind_from_objective(objective):
    """Offline route fallback: prefer explicit deliverable/action phrases over incidental domain nouns."""
    text = f' {(objective or "").lower()} '
    if any(w in text for w in ('click the', 'click save', 'mouse', 'keyboard', 'operate the browser',
                               'operate the desktop', '点击', '鼠标', '键盘', '操作浏览器', '操作桌面')):
        return 'computer_use'
    rules = (
        ('direction_reasoning', ('formal proof', 'mathematical critique', 'optimal-stopping', 'frontier reasoning',
                                 'critique the architecture', 'prove or refute', '形式化证明', '数学推导', '架构批判', '战略权衡')),
        ('complex_build_plan', ('migration plan', 'migration spec', 'implementation specification', 'multi-phase',
                                'system architecture plan', '迁移方案', '迁移计划', '分阶段设计', '系统建设方案')),
        ('bounded_bulk_work', ('bulk ', 'batch rename', 'mechanical refactor', 'uniform rule', 'codemod',
                               '批量', '机械替换', '统一替换')),
        ('web_research', ('web survey', 'web research', 'vendor survey', 'public documentation', 'search the web', 'search public', 'open-web',
                          'literature survey', '网页调研', '文献综述', '厂商调研', '搜索官网', '公开文档')),
        ('text_in_text_out', ('translate', 'translation', 'draft a document', 'draft documentation', 'rewrite the text',
                             'summarize the text', '翻译', '撰写文档', '文案', '润色', '摘要')),
        ('visual_analysis', ('visual diff', 'design diff', 'inspect screenshot', 'compare screenshot', 'inspect the image',
                             'inspect the screenshot', 'inspect the supplied screenshot', 'compare the screenshots',
                             'design inspection', '视觉对比', '截图对比', '检查截图', '设计稿检查', '看图分析')),
        ('debug_review', ('bug', 'fix ', 'deadlock', 'race condition', 'regression', 'review the diff', 'code review',
                          'diff review', 'read-only review', '(read-only)', 'independent review', 'invariant audit',
                          'security audit', 'memory leak', 'diagnos', 'rendering glitch', 'intermittent failure',
                          '报错', '排查', '修复', '死锁', '代码审查', '只读审阅', '只读审查', '安全审计', '内存泄漏')),
        ('frontend_implementation', ('frontend', 'react', 'css', 'tailwind', 'mockup', 'desktop ui', 'game ui',
                                     '界面', '前端', '样式', '组件')),
        ('research_packet', ('backtest', 'quantitative experiment', 'benchmark suite', 'statistical evaluation',
                             'research packet', 'simulation', 'data analysis pipeline', '回测', '定量实验', '评测', '仿真', '统计检验')),
        ('tool_execution', ('runbook', 'checklist', 'deploy', 'release commands', 'health check', 'run the test suite',
                            '巡检', '发版', '运行测试', '操作手册', '发布流程')),
        ('exploration_analysis', ('explore', 'investigate', 'trace', 'map the codebase', 'architecture exploration',
                                  '调研', '分析', '梳理', '探索仓库', '架构追踪')),
        ('self_contained_code', ('standalone', 'self-contained', 'algorithm', 'isolated module',
                                 '独立算法', '独立脚本', '自包含', '实现算法')),
    )
    for kind, keywords in rules:
        if any(word in text for word in keywords):
            return kind
    return 'general_implementation' if objective and objective.strip() else 'other_or_mixed'


def infer_task_profile(task, capabilities):
    """Deterministic task profile from what the caller already said (work kind, difficulty, needs, objective).

    The offline twin of the System-One scorer: same schema, no model call, so tests, --dry-run and an unavailable agy
    still produce a profile. It sees only what the caller wrote, so it mostly restates the route defaults."""
    task = task if isinstance(task, dict) else {}
    routes = capabilities['routes']
    objective = task.get('objective') if isinstance(task.get('objective'), str) else ''
    kind = task.get('work_kind') if task.get('work_kind') in routes else infer_kind_from_objective(objective)
    needs = task.get('needs') if isinstance(task.get('needs'), dict) else {}
    implies = routes[kind].get('implies') or {}
    difficulty = task.get('difficulty') if task.get('difficulty') in DEPTH_BY_DIFFICULTY else default_difficulty(kind)
    depth = DEPTH_BY_DIFFICULTY[difficulty]
    code, context, risk, cost, style = KIND_TRAITS.get(kind, KIND_TRAITS['other_or_mixed'])
    if is_readonly_review_task(objective):
        code = 0
        if kind == 'general_implementation':
            kind = 'debug_review'
    gui = kind == 'computer_use' or (kind == 'other_or_mixed' and any(w in f' {objective.lower()}' for w in GUI_WORDS))
    if depth >= 3:
        context = max(context, 2)
        if style == 'engineering' and code:
            code = 3
    if needs.get('critical'):
        cost = 3
    divergence = infer_divergence(objective, kind, style)
    caller_set_local = 'local_only' in needs and needs.get('local_only') is not None
    evidence = ' '.join(str(task.get(key) or '') for key in ('objective', 'context', 'environment', 'cwd'))
    local_hit = OBJECTIVE_LOCAL_RUNTIME_RE.search(evidence) or LOCAL_MACHINE_RE.search(evidence)
    if caller_set_local:
        local_only = bool(needs.get('local_only'))
        local_note = ''
    elif local_hit:
        local_only = True
        local_note = '; objective names a local runtime, path, or machine dependency'
    elif implies.get('local_only'):
        local_only = True
        local_note = ''
    else:
        local_only = False
        local_note = ''
    reason = (f'heuristic from work_kind={kind}, difficulty={difficulty}'
              + ('' if task.get('difficulty') else ' (route default)') + local_note)
    return validate_profile({
        'work_kind': kind,
        'reasoning_depth': depth, 'code_complexity': code, 'context_scale': context, 'divergence': divergence,
        'hallucination_risk': risk, 'error_cost': cost,
        'needs_visual': bool(needs.get('visual') or implies.get('visual') or gui), 'needs_gui': gui,
        'local_only': local_only, 'interaction_style': style, 'reason': reason})


def profile_difficulty(profile, policy):
    table = (policy.get('profile') or {}).get('difficulty_by_depth') or {}
    return table.get(str(profile['reasoning_depth'])) or {1: 'routine', 2: 'hard', 3: 'coupled', 4: 'frontier'}[profile['reasoning_depth']]


def named_route(profile):
    """A classifier route other than the unmatched bucket. None when the profile did not name one."""
    named = profile.get('work_kind') if isinstance(profile, dict) else None
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


def task_demand(profile, difficulty, critical, divergence, policy, high_precision=False):
    """The task's continuous ability demand: the difficulty's floor (plus critical headroom) and an extra that grows with
    reasoning depth times the mean difficulty of the task's properties (code, context, divergence, hallucination risk,
    error cost). A candidate saturates the demand when its effective ability (base tier x its effort's thinking
    coefficient) reaches `required_ability`; the saturation interval runs one `saturation_span` above it."""
    cfg = demand_config(policy)
    floor = float(policy['ability_floor'][difficulty])
    headroom = float(policy['critical_headroom']) if critical else 0.0
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


def legacy_model_binds_window(wid, wspec, model):
    """Historical windows.models tag match. `model` is used as given, not canonicalized."""
    model = model or ''
    tags = wspec.get('models') if isinstance(wspec, dict) else None
    if tags and not any(tag in model for tag in tags) and tag_group(model) not in tags:
        return False
    if 'fable' in wid and 'fable' not in model:
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


def bundle_pools(bundle, accounts):
    """Window ids this bundle draws, in declaration order (or account-window order for the tag fallback).

    An explicit `draws` on the passed bundle wins, including an empty list. Otherwise the catalog
    bundle's `draws` wins. Otherwise the historical windows.models tag match runs on the canonical model.
    Only ids that exist on the account are returned.
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
    windows = spec.get('windows') if isinstance(spec, dict) and isinstance(spec.get('windows'), dict) else {}
    declared = _explicit_draws(bundle)
    if declared is None and isinstance(spec, dict) and bid:
        catalog = (spec.get('bundles') or {}).get(bid)
        if isinstance(catalog, dict):
            declared = _explicit_draws(catalog)
    if declared is not None:
        return [wid for wid in declared if wid in windows]
    model = canonical(bundle.get('model') or '', accounts)
    return [wid for wid, wspec in windows.items() if legacy_model_binds_window(wid, wspec, model)]


def applicable_windows(account_spec, model, usage_row, bundle=None, accounts=None):
    """(window id, spec, observed window) for windows that bind this model or bundle.

    Without a bundle, the historical tag match runs on the model string as given.
    With a bundle, `bundle_pools` is the only source.
    """
    observed = {}
    for w in (usage_row or {}).get('windows', []):
        wid = w.get('id')
        if wid:
            observed.setdefault(canonical_window_id(account_spec, wid), w)
    windows = (account_spec or {}).get('windows') or {}
    if bundle is None:
        ids = [wid for wid, wspec in windows.items() if legacy_model_binds_window(wid, wspec, model or '')]
    else:
        if accounts is None:
            acct = bundle.get('account') if isinstance(bundle, dict) and isinstance(bundle.get('account'), str) else '_billing'
            accounts = {'accounts': {acct: account_spec}}
            if not isinstance(bundle, dict) or 'account' not in bundle:
                bundle = {**(bundle or {}), 'account': acct}
        ids = bundle_pools(bundle, accounts)
    return [(wid, windows[wid], observed.get(wid)) for wid in ids if wid in windows]


def below_launch_floor(window, observed, usage_row, now):
    """Existing launch protection, shared by unknown-burn ranking and dispatch."""
    floor = window.get('launch_min_remaining_pct')
    reset = parse_time((observed or {}).get('resets_at'))
    if floor is not None and (observed or {}).get('rate_limited') is True and reset is not None and reset > now:
        return True
    fetched = parse_time(usage_row.get('fetched_at'))
    if (usage_row.get('status') != 'fresh' or usage_row.get('stale') is True or fetched is None
            or not -60 <= (now - fetched).total_seconds() <= 900):
        return False
    remaining = (observed or {}).get('remaining_pct_before_reset')
    return bool(floor is not None and reset is not None and reset > now
                and isinstance(remaining, (int, float)) and remaining <= floor)


def tag_group(model):
    if model.startswith('gemini'):
        return 'gemini'
    return '3p'


def window_state(wid, spec, observed, account_spec, now, short_hours, account_stale=False, expected_reset_at=None):
    """Remaining capacity, time to the end of the allowance and how sure we are."""
    hours = spec.get('hours')
    expires = parse_time(account_spec.get('expires_at'))
    row = {'id': wid, 'hours': hours, 'short': hours is not None and hours < short_hours, 'known': False, 'stale': bool(account_stale)}
    if observed is None or hours is None:
        return row
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
                'expiry_hours': expiry_hours, 'target_remaining': 100 * min(1.0, end_hours / hours),
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
    return dict(state, remaining=100.0, reset_hours=wait + state['hours'], end_hours=end,
                target_remaining=100 * min(1.0, end / state['hours']), deferred_hours=wait,
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
    model = canonical(bundle['model'], accounts)
    info = model_spec(accounts, model)
    curves = accounts.get('effort_curves', {}).get(model, {})
    kind = task['kind']
    dur_mult = model_duration_multiplier(model, info)
    base_hours = task['hours']
    base_chain_hours = task['chain_hours']
    hours = base_hours * dur_mult
    chain_hours = base_chain_hours * dur_mult
    result = {'id': bundle['id'], 'bundle': {k: bundle.get(k) for k in ('provider', 'harness', 'account', 'model', 'effort', 'modeId', 'launch_model', 'draws')
                                              if bundle.get(k) is not None},
              'excluded': [], 'notes': [],
              'components': {**subjective_vector(info), 'capability': vision_tools_score(info)}, 'score': None}
    visual_override = (info.get('subjective') or {}).get('capability')
    if visual_override is not None:
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
    if bundle.get('disabled'):
        exclude('disabled by user')
    if bundle.get('discovery_unavailable'):
        exclude(bundle['discovery_unavailable'] if isinstance(bundle['discovery_unavailable'], str)
                else 'model not detected by discovery for 7+ days')
    if bundle['id'] in ctx['unavailable']:
        exclude('marked unavailable by the parent')
    if spec.get('billing') == 'metered' and not task['metered_ok']:
        if (task['constraint_model'] != bundle['id']
                and not any(o.get('account') == bundle['account'] for o in ctx['overrides'])):
            exclude('metered access is a last resort')
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
    if route.get('restrict_models') is True and route.get('preferred'):
        allowed = {canonical(m, accounts) for m in route['preferred']}
        if model not in allowed:
            exclude(f'{kind} is a specialist route')
    if task['needs_visual'] and not info.get('visual'):
        exclude('task needs visual input')
    # Harness input limits apply to every bundle on the harness, manual or auto (HARNESS_CAPABILITIES).
    harness = bundle.get('harness') or bundle_harness(bundle, bundle.get('account'), bundle.get('id'))
    if task['needs_visual'] and not harness_accepts_visual(harness, model):
        exclude(f'{harness} cannot accept image attachments for {model}, so it cannot take visual or GUI work')
    if task['constraint_model'] and task['constraint_model'] not in (bundle['id'], model):
        exclude('explicit model constraint')
    # Model ability x this model's own thinking coefficient at this effort (accounts `effort_curves`). The task side is
    # depth x property difficulty (`task['demand']`); no effort tier is tied to a depth.
    ability = effort_ability(policy, curves, info, bundle.get('effort'))
    result['components']['ability_value'] = round(ability, 2)
    effective = ability
    high_ability = effort_ability(policy, curves, info, 'high')
    demand = task['demand']
    target = demand['required_ability']
    floor = policy['ability_floor'][task['difficulty']]
    gate = floor + (policy['critical_headroom'] if task['critical'] else 0)
    if ability < gate:
        exclude(f'ability {ability:.1f} below the {task["difficulty"]} floor {floor}'
                + (f' plus the {policy["critical_headroom"]} headroom critical work needs' if task['critical'] else ''))
    expires = parse_time(spec.get('expires_at'))
    if expires is not None and (expires - now).total_seconds() < hours * 3600:
        exclude('subscription ends before the work would finish')

    worker = task['worker']
    worn = worker_worn(worker, policy)
    resumes = bool(worker) and worker.get('bundle') == bundle['id']
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
    step_usd = bundle_task_usd(spec, model, bundle.get('effort'), hours, policy, curves, info, context_scale)
    chain_usd = bundle_task_usd(spec, model, bundle.get('effort'), chain_hours, policy, curves, info, context_scale)
    binding = None
    opportunity = 0.0
    unknown = False
    available_at = None
    if spec.get('unlimited'):
        result['notes'].append('unlimited usage')
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
        for wid, wspec, observed in applicable_windows(spec, model, acct_usage, bundle, accounts):
            expected = window_reset_expectation(ctx.get('overrides', ()), bundle['account'], model, wid, spec, accounts, now)
            state = window_state(wid, wspec, observed, spec, now, policy['short_window_hours'],
                                 account_stale=acct_stale, expected_reset_at=expected)
            unit = unit_rate(spec, wid, model, bundle.get('effort'), policy, curves, info, now)
            if not state['known']:
                if state['short']:
                    step = None if unit is None else hours * unit * context_scale
                    cap = capacity_usd(spec, wid, now)
                    small_short = cap is not None and cap < policy.get('small_short_capacity_usd', 50)
                    margin_pct = policy.get('small_short_safety_margin_pct', 12) if small_short else policy['safety_margin_pct']
                    if step is not None and 100.0 - step < margin_pct:
                        exclude(f'{wid} full capacity (100%) is short of the ~{step:.1f}% the work needs plus {margin_pct}% margin')
                    elif (observed and (reset_dt := parse_time(observed.get('resets_at'))) is not None
                          and reset_dt > now and observed.get('reset_time_passed') is not True):
                        wait = (reset_dt - now).total_seconds() / 3600
                        exclude(f'{wid} has an active reset in {wait:.1f}h with unknown remaining percentage')
                continue
            win_cap_usd = window_capacity_usd(spec, wid, now)
            reserved_usd = inflight_reserved_usd(bundle['account'], spec, wid, wspec, state, win_cap_usd,
                                                 ctx.get('records'), accounts, policy, now)
            if reserved_usd > 0 and win_cap_usd:
                reserved_pct = min(state['remaining'], round(100.0 * reserved_usd / win_cap_usd, 2))
                state = {**state, 'observed_remaining': state['remaining'],
                         'remaining': max(0.0, state['remaining'] - reserved_pct),
                         'reserved_usd': round(reserved_usd, 2)}
                result['notes'].append(f'{wid}: -${reserved_usd:.2f} (-{reserved_pct:.1f}%) reserved by in-flight {bundle["account"]} dispatch')
            if state['short']:
                step = None if unit is None else hours * unit * context_scale
                cap = capacity_usd(spec, wid, now)
                small_short_ref = policy.get('small_short_capacity_usd', 50)
                small_short = cap is not None and cap < small_short_ref
                floor_pct = policy.get('small_short_window_floor_pct', 25) if small_short else policy['short_window_floor_pct']
                margin_pct = policy.get('small_short_safety_margin_pct', 12) if small_short else policy['safety_margin_pct']
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
            if unit is None:
                result['notes'].append(f'{wid}: observed {state["remaining"]:.1f}% remaining'
                                       + (' (stale)' if acct_stale or state['stale'] else '')
                                       + '; consumption rate unknown')
                if below_launch_floor(wspec, observed, acct_usage, now):
                    exclude(f'{wid} is below its existing launch protection ({wspec["launch_min_remaining_pct"]}% remaining floor)')
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
            row = window_cost(spec, wid, state, step, demand_forecast(spec, wid, state, policy), policy, result, now, step_usd=step_usd,
                              unit=unit * context_scale)
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
                expected = window_reset_expectation(ctx.get('overrides', ()), bundle['account'], model, wid, spec, accounts, now)
                st = window_state(wid, wspec, observed, spec, now, policy['short_window_hours'],
                                  account_stale=acct_stale, expected_reset_at=expected)
                if not st['known']:
                    continue
                unit = unit_rate(spec, wid, model, bundle.get('effort'), policy, curves, info, now)
                win_cap_usd = window_capacity_usd(spec, wid, now)
                res_usd = inflight_reserved_usd(bundle['account'], spec, wid, wspec, st, win_cap_usd,
                                                ctx.get('records'), accounts, policy, now)
                if res_usd > 0 and win_cap_usd:
                    res_pct = min(st['remaining'], round(100.0 * res_usd / win_cap_usd, 2))
                    st = {**st, 'observed_remaining': st['remaining'],
                          'remaining': max(0.0, st['remaining'] - res_pct), 'reserved_usd': round(res_usd, 2)}
                if st['short']:
                    f_step = None if unit is None else hours * unit * context_scale * fast_mult
                    cap = capacity_usd(spec, wid, now)
                    small_short = cap is not None and cap < policy.get('small_short_capacity_usd', 50)
                    fl_pct = policy.get('small_short_window_floor_pct', 25) if small_short else policy['short_window_floor_pct']
                    mg_pct = policy.get('small_short_safety_margin_pct', 12) if small_short else policy['safety_margin_pct']
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
                is_sprint = bool(in_horizon and f_shortage <= max_sh
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
        vfit = vector_fit(task['requirements'], effort_scaled_capabilities(info, policy, curves, bundle.get('effort')))
        s_weights = task.get('subjective_weights') or {}
        s_cfg = (policy.get('profile') or {}).get('subjective') if isinstance(info.get('subjective'), dict) else None
        result['components']['route_fit'] = round(fit, 3)
        result['components']['vector_fit'] = round(vfit, 3)
        if s_weights and isinstance(s_cfg, dict):
            sfit = subjective_fit(s_weights, subjective_vector(info))
            result['components']['subjective_fit'] = round(sfit, 3)
            w_s = float(s_cfg.get('weight', 0.65))
            w_v = float(s_cfg.get('vector_weight', 0.2))
            w_r = float(s_cfg.get('route_weight', max(0.0, 1.0 - w_s - w_v)))
            fit = w_r * fit + w_v * vfit + w_s * sfit
        else:
            weight = (policy.get('profile') or {}).get('vector_weight', 0.5)
            fit = (1 - weight) * fit + weight * vfit
    cfg = demand_config(policy)
    sunk_weight = (binding or {}).get('reset_sunk_weight', 0.0)
    effort_scale = burn_scale(policy, curves, bundle.get('effort'))
    # Near a reset with surplus quota the extra burn of a higher effort is quota that would lapse, so it is not charged.
    effort_penalty = round(policy.get('extra_effort_penalty', 0.0) * max(0.0, effort_scale - 1.0) * (1.0 - sunk_weight), 4)
    if effort_penalty:
        result['components']['effort_penalty'] = effort_penalty
    effort_gain = max(0.0, effective - high_ability)
    saturation = float((policy.get('profile') or {}).get('saturation_span', 0.3))
    if sunk_weight > 0 and effort_gain > 0:
        sprint_quality = round(float(cfg['sprint_effort_weight']) * sunk_weight * clamp(effort_gain / saturation, 0, 1), 4)
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
    ability_score = clamp((credited - (gate if task['high_precision'] else floor)) / span, 0, 1)
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
        overqualified = round(config.get('overqualification_weight', 0.08) * excess * price * (1.0 - sunk_weight), 4)
        if overqualified:
            result['components']['overqualification'] = overqualified
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


def harness_quality_factor(bundle, kind, policy):
    """Absolute harness quality multiplied into weighted fit and ability. Default 1.0.

    Most specific match wins: by_model[model][harness], by_work_kind[kind][harness], by_harness[harness],
    then the first matching rule, then default. This is not the Codex/Pi sibling preference; that stays
    in apply_harness_order because the order flips by work kind and only applies when a sibling is eligible.
    """
    table = policy.get('harness_factors') if isinstance(policy.get('harness_factors'), dict) else {}
    harness = (bundle or {}).get('harness') or bundle_harness(bundle, (bundle or {}).get('account'), (bundle or {}).get('id'))
    model = str((bundle or {}).get('model') or '')

    def as_factor(value):
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    found = as_factor(((table.get('by_model') or {}).get(model) or {}).get(harness))
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
            if rule.get('model') and rule.get('model') != model:
                continue
            prefix = rule.get('model_prefix')
            if prefix and not model.startswith(str(prefix)):
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
           'overrides': list(overrides), 'records': list(records or ())}
    candidates = [evaluate_bundle(b, task, ctx) for b in ctx['bundles'].values() if bundle_applies(b, task)]
    eligible = [c for c in candidates if not c['excluded']]
    # Weighted fit and ability are the value of this model on this harness. harness_factor (default 1) scales
    # that value only. Opportunity cost is already in score units at the opportunity_exchange rate, so it is
    # subtracted as it is. No normalization over the candidate set.
    weights = dict(policy['weights'])
    weights['continuity'] *= policy['coupling_continuity_multiplier'][task['coupling']] if task['worker'] else 0
    kind = task['kind']
    for c in eligible:
        parts = {k: c['components'][k] for k in weights}
        quality = harness_quality_factor(c['bundle'], kind, policy)
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
            c['score'] = round(c['score'] - policy['deferred_start_penalty'], 4)
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
    needs_gui still forces visual, which is the GUI requirement and not a work_kind rewrite.
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
    if profile is not None and profile.get('needs_gui'):
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


def normalize_task(raw, policy, capabilities, accounts):
    task = dict(raw or {})
    profile = validate_profile(task['profile']) if task.get('profile') is not None else None
    kind = task_kind(task, capabilities, policy)
    if profile and is_readonly_review_task(task.get('objective'), profile):
        profile = dict(profile, code_complexity=0)
        if profile.get('work_kind') == 'general_implementation':
            profile['work_kind'] = 'debug_review'
        if kind == 'general_implementation':
            kind = 'debug_review'
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
    needs = dict(task.get('needs')) if isinstance(task.get('needs'), dict) else {}
    if profile and needs.get('critical') is None:
        needs['critical'] = profile['error_cost'] >= 3
    implies = capabilities['routes'][kind].get('implies') or {}
    # Caller false stands. The profile fills an unset flag. Route implies fill only what both left unset.
    needs.update(resolve_modality_needs(needs, profile, implies))
    critical = bool(needs.get('critical'))
    depth = profile['reasoning_depth'] if profile else None
    raw_div = (profile.get('divergence') if profile else None) or infer_divergence(
        task.get('objective'), kind, profile['interaction_style'] if profile else None)
    div = 1 if ((kind == 'computer_use' or (profile and profile.get('needs_gui'))) and raw_div >= 3) else raw_div
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
    return {'kind': kind, 'difficulty': difficulty, 'difficulty_source': source, 'hours': hours,
            'profile': profile, 'profile_difficulty': derived, 'divergence': div,
            'requirements': profile_requirements(profile, policy) if profile else None,
            'subjective_weights': subjective_weights({**profile, 'divergence': div}, kind) if profile else None,
            'demand': task_demand(profile, difficulty, critical, div, policy, high_precision),
            'effort_unlocked': unlocked,
            'high_precision': high_precision,
            'minimum_sufficient': bool(profile and depth <= 2 and profile['code_complexity'] < 3
                                       and div == 1 and (profile['error_cost'] <= 2 or mechanical_runbook)
                                       and (not critical or mechanical_runbook)),
            'wants_fast': bool(needs.get('fast') or task.get('fast') or infer_fast_preference(task.get('objective'))),
            'force_fast': bool(task.get('force_fast') or needs.get('force_fast')),
            'disable_fast': bool(task.get('disable_fast') or needs.get('disable_fast')),
            'pinned_bundle': task.get('pinned_bundle'),
            'chain_hours': float(chain.get('total_hours') or hours), 'coupling': coupling, 'worker': worker,
            'handoff_ready': bool(chain.get('handoff_ready')), 'needs_visual': needs['visual'],
            'local_only': bool(needs.get('local_only')), 'critical': critical,
            'file_transfer': bool(needs.get('file_transfer')), 'outbound_ok': needs.get('outbound_ok'),
            'constraint_model': canonical(constraints.get('model') or constraints.get('bundle') or '', accounts) or None,
            'metered_ok': bool(task.get('metered_ok')), 'include_unverified': bool(task.get('include_unverified')),
            'can_wait_hours': float(task.get('can_wait_hours') or 0), 'chain_id': chain.get('id'),
            'cwd': task.get('cwd')}


def plan(raw_task, usage, now=None, unavailable=(), records=None, accounts=None, capabilities=None, overrides=None,
         host_policy_config=None):
    accounts = accounts_override.normalize(accounts or load_accounts())
    capabilities = capabilities or load_capabilities(accounts=accounts)
    now = now or datetime.now(timezone.utc)
    usage = provider_inventory.merge_usage(usage, accounts, now)
    usage, accounts, applied = apply_overrides(load_overrides(now) if overrides is None else overrides, usage, accounts, now)
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
    if not eligible and not task['metered_ok'] and any(c['excluded'] == ['metered access is a last resort'] for c in candidates):
        candidates, eligible, system = rank({**task, 'metered_ok': True}, usage, policy, accounts, capabilities, now,
                                            unavailable, applied, records, host_policy_config)
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
    decision['task'].update(difficulty_source=task['difficulty_source'],
                            high_precision=task['high_precision'],
                            demand=task['demand'], max_recommended_effort=None)
    if task.get('wants_fast'):
        decision['task']['wants_fast'] = True
    if task['profile']:
        decision['task'].update(profile=task['profile'], profile_difficulty=task['profile_difficulty'],
                                minimum_sufficient=task['minimum_sufficient'])
    if applied:
        decision['overrides'] = applied
    result = {'stage': 'plan', 'source': 'deterministic_allocator', 'observed_at': now.isoformat(), 'decision': decision,
              'ranking': sorted(candidates, key=lambda c: (c['score'] is None, -(c['score'] or 0))), 'usage': usage}
    if not eligible:
        closest = sorted((c for c in candidates if c['excluded']), key=lambda c: (len(c['excluded']), -c['components']['fit']))[:2]
        decision.update({'action': 'none', 'needs_user_decision': True,
                         'reason': 'No eligible bundle. Closest: ' + ' | '.join(
                             f"{c['id']}: {'; '.join(c['excluded'])}" for c in closest)})
        result['choice'] = 'none'
        return result
    can_wait = task['can_wait_hours']
    ready = [c for c in eligible if (c['available_in_hours'] or 0) <= can_wait]
    pool = ready or eligible
    top = pool[0]
    better = eligible[0]
    if better is not top and better['available_in_hours']:
        decision['better_if_waiting'] = {'bundle': better['id'], 'hours': round(better['available_in_hours'], 1),
                                         'score_gain': round(better['score'] - top['score'], 4)}
    forced = False
    if task['coupling'] == 'high' and resume is not None and not resume['excluded'] and resume['action'] == 'resume':
        top, forced = resume, True        # a coupled chain finishes on its owner while that owner is not worn
    decision.update({'action': top['action'], 'needs_user_decision': False,
                     'continuity': 'high-coupling chain stays with its owner' if forced else None})
    if (top['available_in_hours'] or 0) > can_wait:
        # can_wait_hours is a limit, not a preference: say so instead of quietly starting later than allowed
        decision.update({'needs_user_decision': True, 'wait_hours': round(top['available_in_hours'], 1),
                         'reason': (f"The chain owner {top['id']} can resume in {top['available_in_hours']:.1f}h"
                                    if forced else f"Nothing eligible can start within {can_wait:g}h; the best, {top['id']}, can start in {top['available_in_hours']:.1f}h")
                         + '. Wait, allow a longer wait, or mark it unavailable and replan.'})
    if (task['worker'] and not worker_worn(task['worker'], policy) and task['coupling'] == 'high'
            and resume is not None and resume['excluded']):
        decision.update({'needs_user_decision': True, 'drift_risk': 'high',
                         'resume_blocked_by': resume['excluded'],
                         'wait_hours': resume.get('wait_until_hours'),
                         'reason': 'The chain owner cannot continue now. Waiting keeps one owner; switching delivers sooner with handoff and drift risk.'})
    # The judge weighs models, not efforts, harnesses or accounts of one model: the margin is to the best other model.
    rival = next((c for c in pool if c is not top and c['bundle']['model'] != top['bundle']['model']), None)
    margin = round(top['score'] - rival['score'], 4) if rival is not None and not forced else None
    decision['margin'] = margin
    decision['needs_judgment'] = bool(margin is not None and margin < policy['needs_judgment_margin'])
    if top['bundle'].get('fast'):
        decision['fast'] = True
        decision['fast_reason'] = top['components'].get('fast_reason')
    decision.setdefault('reason', explain(top, system))
    top_model = canonical(top['bundle']['model'], accounts)
    offered = sorted({c['bundle'].get('effort') for c in candidates if canonical(c['bundle']['model'], accounts) == top_model
                      and c['bundle'].get('effort') in EFFORT_ORDER}, key=EFFORT_ORDER.index) or EFFORT_ORDER
    saturating = saturating_effort(policy, accounts.get('effort_curves', {}).get(top_model, {}), model_spec(accounts, top_model),
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
                          'fast', 'fast_reason', 'wait_hours', 'start_after_hours', 'better_if_waiting', 'warnings')


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
    return '; '.join(parts)
