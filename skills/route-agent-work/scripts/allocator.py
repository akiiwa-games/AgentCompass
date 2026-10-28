"""Deterministic worker allocation: capability, task fit, continuity with the chain's existing worker, and the
opportunity cost of the quota the work would use.

No model is involved. Every number that drives a choice is in references/accounts.json or
references/model-capabilities.json and is returned in the per-candidate breakdown, so a surprising
choice can be traced to the input that caused it.
"""

from datetime import datetime, timedelta, timezone
import json
from math import erf, exp, pi, sqrt
import os
from pathlib import Path
import sys

# Also support the path-loaded allocator and python -I entrypoints.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import host_policy
import override_store
import antigravity_telemetry
import provider_inventory
from model_resolution import generic_model_spec, resolve_canonical_model

REFERENCES = Path(__file__).resolve().parents[1] / 'references'
HARD_ROUTE_BASIS = ('User require', 'Standing user order')   # routes with these bases restrict the model set
WINDOW_ALIASES = {'seven_day': 'usage', 'usage-10080m': 'usage', 'usage-5h': 'five_hour', 'usage-300m': 'five_hour'}


def canonical_window_id(spec, wid):
    windows = (spec or {}).get('windows') or {}
    if wid in windows:
        return wid
    mapped = WINDOW_ALIASES.get(wid)
    return mapped if mapped in windows else wid


def capacity_override_path():
    configured = os.environ.get('ROUTER_CAPACITY')
    return Path(configured).expanduser() if configured else Path.home() / '.route-manager' / 'capacity.json'


def overrides_path():
    configured = os.environ.get('ROUTER_OVERRIDES')
    return Path(configured).expanduser() if configured else Path.home() / '.route-manager' / 'router-overrides.json'


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
        if not (isinstance(o, dict) and o.get('account') and (until := parse_time(o.get('until'))) is not None and until > now):
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
    """A priority whose quota is down to the safety margin (user 2026-10-16: "到5%的时候取消").

    At or below `safety_margin_pct` the allocator already refuses new work there, so the boost can only mislead; the
    rule is retired instead of waiting for its reset or `until`. Only a fresh reading counts, only weekly windows (or the
    override's own window) that bind its model, and a rule that also carries an expiry date is kept for that fact."""
    if not isinstance(override, dict) or not override.get('boost') or override.get('expires_at'):
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


def apply_overrides(overrides, usage, accounts, now):
    """Apply priority/expiry intent without replacing observed reset or capacity facts."""
    if not overrides:
        return usage, accounts, []
    active, landed, depleted = [], {}, {}
    for o in overrides:
        until = parse_time(o.get('until'))
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
        resets = parse_time(o.get('expected_reset_at') or o.get('resets_at'))
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
    patterns = {'codex': [f'.codex/sessions/**/rollout-*-{session}.jsonl', f'.{provider}/sessions/**/rollout-*-{session}.jsonl',
                          f'.codex-*/sessions/**/rollout-*-{session}.jsonl',
                          f'.codex/archived_sessions/rollout-*-{session}.jsonl', f'.{provider}/archived_sessions/rollout-*-{session}.jsonl',
                          f'.codex-*/archived_sessions/rollout-*-{session}.jsonl'],
                'claude': [f'.claude/projects/*/{session}.jsonl', f'.{provider}/projects/*/{session}.jsonl',
                           f'.claude-*/projects/*/{session}.jsonl'],
                'pi': [f'.pi/agent/sessions/**/*{session}*.jsonl']}
    for pattern in patterns.get(provider.split('-')[0], []):
        found = next(home.glob(pattern), None)
        if found:
            return provider, found
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
                if provider.startswith('codex'):
                    if '"type":"compacted"' in line:
                        compactions, compacted, context = compactions + 1, compacted + (context or 0), None
                    elif '"token_count"' in line and '"last_token_usage"' in line:
                        usage = ((json.loads(line).get('payload') or {}).get('info') or {}).get('last_token_usage') or {}
                        inp = usage.get('input_tokens')
                        if isinstance(inp, (int, float)) and not isinstance(inp, bool) and inp > 0:
                            context = int(inp)
                elif provider.startswith('claude'):
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
    return Path.home() / '.route-manager' / 'accounts-override.json'


def load_accounts(path=None):
    accounts = json.loads((path or REFERENCES / 'accounts.json').read_text(encoding='utf-8-sig'))
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
            if isinstance(user_doc.get('scoring'), dict):
                accounts['scoring'] = user_doc['scoring']
            for m_name, m_spec in (user_doc.get('models') or {}).items():
                if isinstance(m_spec, dict) and isinstance(accounts.get('models', {}).get(m_name), dict):
                    if isinstance(m_spec.get('subjective'), dict):
                        accounts['models'][m_name]['subjective'] = m_spec['subjective']
        provider_inventory.apply(accounts)
        if user_doc is not None:
            for a_name, a_spec in (user_doc.get('accounts') or {}).items():
                target_acc = (accounts.get('accounts') or {}).get(a_name)
                if isinstance(a_spec, dict) and isinstance(target_acc, dict):
                    if a_spec.get('verified') is False:
                        target_acc['verified'] = False
                    elif a_spec.get('verified') is True and target_acc.get('discovery_status') in (None, 'ready'):
                        target_acc['verified'] = True
                    if 'allowed_cwd' in a_spec:
                        target_acc['allowed_cwd'] = a_spec['allowed_cwd']
                    for b_id, b_spec in (a_spec.get('bundles') or {}).items():
                        target_b = (target_acc.get('bundles') or {}).get(b_id)
                        if isinstance(b_spec, dict) and isinstance(target_b, dict):
                            if 'disabled' in b_spec:
                                target_b['disabled'] = b_spec['disabled']
                            else:
                                target_b.pop('disabled', None)
    override = None if path else capacity_override_path()
    if override is not None:
        try:
            measured = json.loads(override.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            measured = {}
        apply_measured(accounts, measured if isinstance(measured, dict) else {}, datetime.now(timezone.utc))
    if path is None:
        try:
            accounts['_host_policy_sync'] = host_policy.sync_discovered(accounts)
        except (OSError, host_policy.HostPolicyError) as exc:
            # Discovery still reports local facts; existing policy checks retain the denial.
            accounts['_host_policy_sync'] = {'mode': 'error', 'added': [], 'error': str(exc)}
    return accounts


def apply_measured(accounts, measured, now):
    """Layer the usage readings from the tray service's capacity.json (each window's used percent and its 3h and 24h
    rise, confirmed within `OBSERVED_MAX_AGE_HOURS`) over accounts.json; they feed the demand forecast. Consumption is
    priced from accounts.json alone (list price against community capacity, user 2026-10-12): the tray's usage fit
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


def unit_rate(spec, wid, model, effort, policy, curves=None, info=None, now=None):
    """Percent of the window this bundle spends on work that takes one worker-hour at High, or None when the account
    has no rate. With a `capacity_usd` for the window and a `usd_per_hour` for the model, that is the hour's list-price
    dollars over the window's dollars; otherwise the model's or account's percent-per-hour prior. Either way the effort
    curve's cost per task relative to High scales it: a higher effort spends about the same per hour but runs longer
    (Sol max 1.58 / 0.7 = 2.3 times High per task)."""
    reference = effort_value(policy, curves, 'burn', 'high', 1.0)
    scale = effort_value(policy, curves, 'burn', effort, reference) / reference
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
        elapsed = state['hours'] - state['reset_hours']
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
    derived = {'gemini-weekly': 625.0, 'gemini-5h': 182.0, '3p-weekly': 88.0, '3p-5h': 17.6}.get(wid)
    if derived is not None:
        return derived
    rate = (spec.get('burn_pct_per_worker_hour') or {}).get(wid)
    return round(100.0 * 2.0 / float(rate), 2) if isinstance(rate, (int, float)) and rate > 0 else None


def bundle_task_usd(spec, model, effort, hours, policy, curves=None, info=None, context_scale=1.0, fast=False):
    """Expected API-equivalent task budget in USD for `hours` active worker-hours (`hours * usd_per_hour * effort_scale * context_scale * fast_scale`)."""
    if spec.get('unlimited') or spec.get('unmetered'):
        return 0.0
    reference = effort_value(policy, curves, 'burn', 'high', 1.0)
    scale = effort_value(policy, curves, 'burn', effort, reference) / reference
    fast_scale = float(policy.get('fast_burn_multiplier', 2.0)) if fast else 1.0
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
        if bundle.get('account') != account:
            continue
        model = canonical(bundle.get('model') or '', accounts)
        tags = wspec.get('models')
        if tags and not any(tag in model for tag in tags) and tag_group(model) not in tags:
            continue
        if 'fable' in wid and 'fable' not in model:
            continue
        amount = rec.get('expected_usd')
        if isinstance(amount, (int, float)) and not isinstance(amount, bool) and amount > 0:
            active[rec['dispatch_id']] = float(amount)
    return sum(active.values())


def window_cost(spec, wid, state, step, forecast, policy, result, now=None, step_usd=None, unit=None):
    """One window's share of the opportunity cost: the other work that spending `step` here would push past the
    window's end (expected shortage with the step minus without it), valued at list price against the window's USD
    capacity (explicit `capacity_usd`, else the derived pool) times `shortage_loss_per_usd`. Only a window with no USD
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
        displaced, cost = shortage * eff_cap_usd / 100, policy['shortage_loss_per_usd'] * shortage * eff_cap_usd / 100
    else:
        rate = max(float((spec.get('burn_pct_per_worker_hour') or {}).get(wid) or 0.0), float(unit or 0.0))
        displaced = shortage / rate if rate > 0 else 0.0
        cost = policy['shortage_loss_per_worker_hour'] * displaced
    scarcity_ref = float(policy.get('scarcity_usable_ref_pct', 45.0))
    scarcity = (shortage_prob * clamp(1.0 - max(0.0, usable - step) / scarcity_ref, 0.0, 1.0) ** 1.5
                if not state.get('short') and scarcity_ref > 0 else 0.0)
    scarcity_cost = float(policy.get('scarcity_penalty_max', 0.06)) * scarcity
    cost += scarcity_cost
    expires_first = end < state['reset_hours']
    horizon = float(policy.get('reset_horizon_hours') or 0)
    sunk_weight = 0.0
    excess_pct = max(0.0, (usable - step) - mean)
    if (horizon > 0 and not state.get('short') and not state.get('stale') and not state.get('deferred_hours')
            and 0 < state['reset_hours'] < horizon and usable - step > 0):
        urgency = clamp(1.0 - state['reset_hours'] / horizon, 0.0, 1.0) ** float(policy.get('reset_urgency_power', 1.0))
        surplus_ref = max(1e-6, float(policy.get('reset_surplus_ref_pct', 15)))
        surplus = (1.0 - shortage_prob) * clamp(excess_pct / surplus_ref, 0.0, 1.0)
        sunk_weight = round(urgency * surplus, 4)
    reset_bonus = round(float(policy.get('reset_bonus_max', 0.0)) * sunk_weight, 4) if sunk_weight > 0 else 0.0
    if source == 'observed' and mean > remaining:
        result['notes'].append(f"{wid}: other work at the observed {rate:.2f}%/h wants ~{mean:.0f}% before its "
                               f"{'expiry' if expires_first else 'reset'} in {end:.0f}h; {remaining:.0f}% is left")
    return {'window': wid, 'remaining': remaining, 'after': remaining - step, 'step_pct': step,
            **({'capacity_usd': round(eff_cap_usd, 2), 'remaining_usd': round(rem_usd, 2),
                'reserved_usd': round(state.get('reserved_usd', 0.0), 2),
                'expected_usd': round(real_step_usd, 2), 'after_usd': round(after_usd, 2)}
               if eff_cap_usd and rem_usd is not None and real_step_usd is not None else {}),
            'reset_hours': state['reset_hours'], 'end_hours': end, 'expires_first': expires_first,
            'demand_pct': round(mean, 1), 'demand_source': source,
            'shortage_probability': round(shortage_prob, 3),
            'scarcity_cost': round(scarcity_cost, 4),
            ('usd_displaced' if eff_cap_usd else 'worker_hours_displaced'): round(displaced, 3),
            **({'reset_sunk_weight': sunk_weight, 'reset_bonus': reset_bonus, 'surplus_pct': round(excess_pct, 1)}
               if reset_bonus > 0 else {}),
            'opportunity_cost': round(cost, 4)}


def load_capabilities(path=None):
    return json.loads((path or REFERENCES / 'model-capabilities.json').read_text(encoding='utf-8-sig'))


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


def bundle_harness(bundle, account=None, bundle_id=None):
    """Execution harness family for a bundle (distinct from its billing account or gateway provider)."""
    explicit = str((bundle or {}).get('harness') or '').strip().lower()
    if explicit:
        return explicit
    prov = str((bundle or {}).get('provider') or account or '').strip().lower()
    bid = str(bundle_id or (bundle or {}).get('id') or '').strip().lower()
    launch_m = str((bundle or {}).get('launch_model') or '').strip().lower()
    model = str((bundle or {}).get('model') or '').strip().lower()
    if prov == 'pi' or prov.startswith('pi-') or bid.startswith('pi_') or '__pi_' in bid or launch_m.startswith('openai-codex/'):
        return 'pi'
    if prov == 'opencode' or prov.startswith('opencode-') or launch_m.startswith('opencode/'):
        return 'opencode'
    if prov in ('antigravity', 'antigravity-resilient') or prov.startswith('antigravity-'):
        return 'antigravity-resilient'
    if prov in ('dot', 'chatgpt-web'):
        return prov
    if prov == 'claude' or prov.startswith('claude-') or 'claude' in model:
        return 'claude'
    return 'codex'


def bundles(accounts):
    """Flat list of launchable bundles with their account and execution harness attached."""
    out = {}
    for account, spec in accounts['accounts'].items():
        for bundle_id, bundle in spec.get('bundles', {}).items():
            provider = bundle.get('provider', account)
            out[bundle_id] = {'id': bundle_id, 'account': account, 'provider': provider,
                              'harness': bundle_harness(bundle, account, bundle_id),
                              'model': bundle['model'], 'effort': bundle.get('effort'),
                              **{k: bundle[k] for k in ('modeId', 'disabled', 'launch_model', 'when', 'discovery_unavailable') if k in bundle}}
    return out


def fit_score(kind, model, capabilities, accounts):
    """First of the route's preferred list is 1.0. Behind it a `strict_order` route (the user's ranked order, e.g.
    visual) steps down evenly to 0.2; on any other route the fallbacks sit in a narrow band, so ability and opportunity
    cost choose among them. Stepping 0.2 per place starved every model the lists put low or left out."""
    route = capabilities['routes'].get(kind) or {}
    preferred = [canonical(m, accounts) for m in route.get('preferred', [])]
    if not preferred:
        return 0.6
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
        'reason': {'type': 'string', 'description': 'One concise sentence about the task itself that justifies the scores.'},
    },
    'required': list(PROFILE_INTEGERS) + list(PROFILE_FLAGS) + ['interaction_style', 'reason'],
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
    return validate_profile({
        'work_kind': kind,
        'reasoning_depth': depth, 'code_complexity': code, 'context_scale': context, 'divergence': divergence,
        'hallucination_risk': risk, 'error_cost': cost,
        'needs_visual': bool(needs.get('visual') or implies.get('visual') or gui), 'needs_gui': gui,
        'local_only': bool(needs.get('local_only') or implies.get('local_only')), 'interaction_style': style,
        'reason': f'heuristic from work_kind={kind}, difficulty={difficulty}' + ('' if task.get('difficulty') else ' (route default)')})


def profile_difficulty(profile, policy):
    table = (policy.get('profile') or {}).get('difficulty_by_depth') or {}
    return table.get(str(profile['reasoning_depth'])) or {1: 'routine', 2: 'hard', 3: 'coupled', 4: 'frontier'}[profile['reasoning_depth']]


def profile_kind(profile, policy):
    """The route a profile implies when the caller named none."""
    if profile['needs_gui']:
        return 'computer_use'
    if profile.get('work_kind') in KIND_TRAITS and profile['work_kind'] != 'other_or_mixed':
        return profile['work_kind']
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
    if prof and prof.get('needs_gui') and task.get('work_kind') in (None, 'general_implementation', 'tool_execution', 'debug_review', 'other_or_mixed'):
        return 'computer_use'
    if task.get('work_kind') in routes:
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


def profile_requirements(profile, policy):
    """{dimension: (required level, weight)} for the dimensions the profile needs (accounts `scoring.profile`)."""
    config = policy.get('profile') or {}
    table, weights = config['requirements'], config.get('requirement_weights') or {}
    out = {'reasoning': (table['reasoning'][str(profile['reasoning_depth'])], weights.get('reasoning', 1.0))}
    if profile['code_complexity']:
        out['coding'] = (table['coding'][str(profile['code_complexity'])], weights.get('coding', 1.0))
    explore = 'exploration_style' if profile['interaction_style'] == 'exploration' else 'exploration'
    out['exploration'] = (table['exploration'][str(profile['context_scale'])], weights.get(explore, 0.5))
    risky = 'anti_hallucination_high' if profile['hallucination_risk'] >= 3 else 'anti_hallucination'
    out['anti_hallucination'] = (table['anti_hallucination'][str(profile['hallucination_risk'])], weights.get(risky, 0.5))
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


def bundle_applies(bundle, task):
    """A bundle with a `when` rule is offered only to the work it names (e.g. medium efforts on routine, Pro advisor on frontier)."""
    rule = bundle.get('when') or {}
    if 'max_reasoning_depth' in rule:
        return bool(task.get('profile')) and task['profile']['reasoning_depth'] <= rule['max_reasoning_depth']
    if 'min_difficulty' in rule:
        diff, req = task.get('difficulty'), rule['min_difficulty']
        if diff in DIFFICULTY_ORDER and req in DIFFICULTY_ORDER and DIFFICULTY_ORDER.index(diff) < DIFFICULTY_ORDER.index(req):
            return False
    return True


def applicable_windows(account_spec, model, usage_row):
    """(window id, spec, observed window) for windows that bind this model."""
    observed = {}
    for w in (usage_row or {}).get('windows', []):
        wid = w.get('id')
        if wid:
            observed.setdefault(canonical_window_id(account_spec, wid), w)
    out = []
    for wid, spec in account_spec.get('windows', {}).items():
        tags = spec.get('models')
        if tags and not any(tag in model for tag in tags) and not (tag_group(model) in tags):
            continue
        if 'fable' in wid and 'fable' not in model:
            continue
        out.append((wid, spec, observed.get(wid)))
    return out


def below_launch_floor(window, observed, usage_row, now):
    """Existing launch protection, shared by unknown-burn ranking and dispatch."""
    fetched = parse_time(usage_row.get('fetched_at'))
    if (usage_row.get('status') != 'fresh' or usage_row.get('stale') is True or fetched is None
            or not -60 <= (now - fetched).total_seconds() <= 900):
        return False
    floor = window.get('launch_min_remaining_pct')
    reset = parse_time((observed or {}).get('resets_at'))
    remaining = (observed or {}).get('remaining_pct_before_reset')
    # An allowance with no declared cycle can still be freshly exhausted. Do not
    # invent a reset or use an observation whose reported reset has already passed.
    no_cycle = window.get('hours') is None
    if no_cycle and reset is None:
        remaining = (observed or {}).get('observed_remaining_pct', remaining)
    return bool(floor is not None and ((reset and reset > now) or (no_cycle and reset is None))
                and isinstance(remaining, (int, float)) and remaining <= floor)


def tag_group(model):
    if model.startswith('gemini'):
        return 'gemini'
    return '3p'


def window_state(wid, spec, observed, account_spec, now, short_hours, account_stale=False):
    """Remaining capacity, time to the end of the allowance and how sure we are."""
    hours = spec.get('hours')
    expires = parse_time(account_spec.get('expires_at'))
    row = {'id': wid, 'hours': hours, 'short': hours is not None and hours < short_hours, 'known': False, 'stale': False}
    if observed is None or hours is None:
        return row
    remaining = observed.get('remaining_pct_before_reset')
    if remaining is None:
        remaining = observed.get('observed_remaining_pct')
        row['stale'] = remaining is not None
    seconds = observed.get('seconds_until_reset')
    if row['short'] and remaining == 100 and seconds is None and observed.get('resets_at') is None:
        # An unstarted rolling short window (used_pct == 0, resets_at == null) has full 5h capacity ready.
        seconds = hours * 3600
        row['stale'] = bool(account_stale)
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
    result = {'id': bundle['id'], 'bundle': {k: bundle.get(k) for k in ('provider', 'harness', 'account', 'model', 'effort', 'modeId', 'launch_model')
                                              if bundle.get(k) is not None},
              'excluded': [], 'notes': [], 'components': {}, 'score': None}
    now = ctx['now']

    def exclude(reason):
        result['excluded'].append(reason)

    if not account_cwd_allowed(spec, task.get('cwd')):
        exclude(f"outside account allowed_cwd ({spec.get('allowed_cwd')})")

    if not host_policy.account_allowed(ctx['host_policy'], bundle['account']):
        exclude('excluded by host policy')
    if bundle.get('disabled'):
        exclude(bundle['disabled'])
    if bundle.get('discovery_unavailable'):
        exclude(bundle['discovery_unavailable'])
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
    if route.get('basis', '').startswith(HARD_ROUTE_BASIS) and route.get('preferred'):
        allowed = {canonical(m, accounts) for m in route['preferred']}
        if model not in allowed:
            exclude(f'{kind} is a specialist route')
    if task['needs_visual'] and not info.get('visual'):
        exclude('task needs visual input')
    if task['constraint_model'] and task['constraint_model'] not in (bundle['id'], model):
        exclude('explicit model constraint')
    ability = info.get('tier', 0) + effort_value(policy, curves, 'ability', bundle.get('effort'), 0)
    result['components']['ability_value'] = round(ability, 2)
    effective = ability
    high_gain = effort_value(policy, curves, 'ability', 'high', 0)
    if not task['effort_unlocked'] and effort_value(policy, curves, 'ability', bundle.get('effort'), 0) > high_gain:
        # Above High buys nothing on routine/hard work; its burn and extra_effort_penalty still count (spec 3.3.1).
        effective = info.get('tier', 0) + high_gain
        result['components']['effort_capped'] = True
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
                if o['account'] == bundle['account'] and (not o.get('model') or canonical(o['model'], accounts) == model))
    for intent in ctx.get('overrides', ()):
        if intent['account'] == bundle['account'] and intent.get('expected_reset_at'):
            result['notes'].append(f"expected reset {intent['expected_reset_at']}: {intent['reset_expectation_status']}; capacity requires a fresh observation")
    if boost:
        result['components']['boost'] = round(boost, 3)
        result['notes'].append(f'temporary boost {boost:+.2f} from a user override')
    if not spec.get('unmetered'):
        acct_usage = ctx['usage'].get('accounts', {}).get(bundle['account']) or {}
        acct_stale = acct_usage.get('status') == 'stale' or acct_usage.get('stale') is True
        for wid, wspec, observed in applicable_windows(spec, model, acct_usage):
            if wspec.get('hours') is None:
                if below_launch_floor(wspec, observed, acct_usage, now):
                    exclude(f'{wid} is below its existing launch protection ({wspec["launch_min_remaining_pct"]}% remaining floor)')
                if observed:
                    result['notes'].append(f'{wid}: native allowance has no known cycle; consumption rate unknown')
            state = window_state(wid, wspec, observed, spec, now, policy['short_window_hours'], account_stale=acct_stale)
            if not state['known']:
                continue
            unit = unit_rate(spec, wid, model, bundle.get('effort'), policy, curves, info, now)
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
                    if not small_short and not state['stale'] and 0 < wait <= policy['defer_if_reset_within_hours']:
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
    fast_mult = float(policy.get('fast_burn_multiplier', 2.0))
    if info.get('fast_capable') and not task.get('disable_fast') and not result['excluded'] and available_at is None:
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
            for wid, wspec, observed in applicable_windows(spec, model, acct_usage):
                st = window_state(wid, wspec, observed, spec, now, policy['short_window_hours'], account_stale=acct_stale)
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
    result['capacity'] = binding
    result['expected_hours'] = round(hours, 2)
    if dur_mult != 1.0:
        result['components']['duration_multiplier'] = round(dur_mult, 2)
    result['expected_pct'] = None if step_pct is None else round(step_pct, 2)
    result['chain_pct'] = None if chain_pct is None else round(chain_pct, 2)
    result['expected_usd'] = None if spec.get('billing') == 'unknown' else round(step_usd, 2)
    result['chain_usd'] = None if spec.get('billing') == 'unknown' else round(chain_usd, 2)
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
        prof = task['profile']
        div = task.get('divergence', prof.get('divergence', 1))
        span_floor = floor + (policy.get('profile') or {}).get('saturation_span', 0.3)
        if ((div >= 2 or prof['context_scale'] == 3 or prof['code_complexity'] == 3
             or (kind == 'debug_review' and prof['code_complexity'] == 0)
             or (prof['reasoning_depth'] == 1 and info.get('tier', 0) > span_floor))
                and kind != 'frontend_implementation' and model not in ('gemini-3.8-flash-high', 'dot', 'gpt-6-pro')
                and not route.get('strict_order') and not route.get('capability_fit')):
            band = accounts['scoring'].get('fallback_fit', {})
            fit = min(fit, band.get('start', 0.6) + band.get('step', 0.015))
        vfit = vector_fit(task['requirements'], capability_vector(info))
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
    reference_burn = effort_value(policy, curves, 'burn', 'high', 1.0)
    effort_scale = effort_value(policy, curves, 'burn', bundle.get('effort'), reference_burn) / reference_burn
    effort_penalty = round(policy.get('extra_effort_penalty', 0.0) * max(0.0, effort_scale - 1.0), 4)
    if effort_penalty:
        result['components']['effort_penalty'] = effort_penalty
    span = policy.get('ability_span', 0.6)
    if kind in ('frontend_implementation', 'complex_build_plan', 'direction_reasoning') and not task['high_precision']:
        # Once sufficient reasoning is available, UI and planning/scheme quality are decided by route and subjective fit.
        span = (policy.get('profile') or {}).get('saturation_span', 0.3)
    ability_score = clamp((effective - (gate if task['high_precision'] else floor)) / span, 0, 1)
    if task['high_precision']:
        effort_gain = max(0.0, effort_value(policy, curves, 'ability', bundle.get('effort'), 0) - high_gain)
        result['components']['quality_bonus'] = round(float(policy.get('high_precision_effort_weight', 0.14)) * effort_gain, 4)
    if task['minimum_sufficient']:
        # Ordinary profiled work: meeting the floor is sufficient, so every model at or above it gets full ability
        # (2026-10-27: Flash at exactly the routine floor scored 0 and lost to anything 0.3 above it), and list price
        # is charged for ability beyond floor + saturation_span, so a sufficient cheaper model wins when quota is plentiful.
        config = policy.get('profile') or {}
        span = config.get('saturation_span', 0.3)
        ability_score = 1.0 if effective >= gate else 0.0
        excess = clamp((effective - gate - span) / config.get('overqualification_span', 0.6), 0, 1)
        usd = 0.0 if spec.get('unlimited') or spec.get('unmetered') else model_usd_per_hour(model, info)
        sunk_weight = (binding or {}).get('reset_sunk_weight', 0.0)
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


def rank(task, usage, policy, accounts, capabilities, now, unavailable=(), overrides=(), records=(), host_policy_config=None):
    ctx = {'policy': policy, 'accounts': accounts, 'capabilities': capabilities, 'usage': usage, 'now': now,
           'unavailable': set(unavailable), 'bundles': bundles(accounts),
           'host_policy': host_policy.load_policy() if host_policy_config is None else host_policy_config,
           'overrides': list(overrides), 'records': list(records or ())}
    candidates = [evaluate_bundle(b, task, ctx) for b in ctx['bundles'].values() if bundle_applies(b, task)]
    eligible = [c for c in candidates if not c['excluded']]
    # The value terms are weighted; the opportunity cost is already in score units (worker-hours displaced times the
    # loss per worker-hour), so it is subtracted as it is. No normalization over the candidate set: a bundle's score
    # does not move because another candidate joined or left.
    weights = dict(policy['weights'])
    weights['continuity'] *= policy['coupling_continuity_multiplier'][task['coupling']] if task['worker'] else 0
    for c in eligible:
        parts = {k: c['components'][k] for k in weights}
        c['score'] = round(sum(weights[k] * parts[k] for k in weights) + c['components'].get('free_usage', 0)
                           + c['components'].get('boost', 0) + c['components'].get('reset_bonus', 0)
                           + c['components'].get('quality_bonus', 0) + c['components'].get('fast_bonus', 0)
                           - c['components']['opportunity_cost']
                           - c['components'].get('effort_penalty', 0) - c['components'].get('overqualification', 0), 4)
        if c['available_in_hours']:
            c['score'] = round(c['score'] - policy['deferred_start_penalty'], 4)
    apply_harness_order(eligible, task['kind'], policy, capabilities, (task['profile'] or {}).get('interaction_style'))
    eligible.sort(key=lambda c: -c['score'])
    return candidates, eligible, {'weights': {k: round(v, 3) for k, v in weights.items()},
                                  'shortage_loss_per_usd': policy['shortage_loss_per_usd'],
                                  'shortage_loss_per_worker_hour': policy['shortage_loss_per_worker_hour']}


def sibling_key(candidate):
    """Bundles that differ only in harness (Codex or Pi running the same model on the same account)."""
    bundle = candidate['bundle']
    return bundle['account'], bundle['model'], bundle.get('effort')


def harness_group(candidate):
    """Siblings that would also do the same thing: resuming the owner is not the same choice as starting its twin."""
    return sibling_key(candidate) + (candidate['action'],)


def apply_harness_order(eligible, kind, policy, capabilities, style=None):
    """Among eligible siblings, a bundle loses harness_step per place its harness sits behind the best one in the
    route's harness_order, else the profile's interaction style (`scoring.profile.harness_by_style`), else the policy
    default. The best sibling keeps its score, so the harness never changes which model wins."""
    by_style = ((policy.get('profile') or {}).get('harness_by_style') or {}).get(style) if style else None
    order = (capabilities['routes'].get(kind) or {}).get('harness_order') or by_style or policy.get('harness_order') or []
    step = policy.get('harness_step', 0.03)

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
            continue
        best = min(place(c) for c in siblings)
        for c in siblings:
            behind = place(c) - best
            if behind:
                c['score'] = round(c['score'] - step * behind, 4)
                c['components']['harness'] = round(-step * behind, 3)


def dot_may_take(kind, needs, capabilities):
    """Whether Dot is a candidate. It has its own computer and several ways to receive data and return files, so every route
    admits it except specialist routes that do not list it, work that needs vision, and work the parent marks needs.local_only
    (it must run on this machine). needs.independent_text is no longer read: it used to be the gate. dot_execution marked
    local-only is a contradiction, so Dot is not offered and the plan asks the user."""
    route = capabilities['routes'].get(kind) or {}
    needs = {**(route.get('implies') or {}), **{k: v for k, v in (needs or {}).items() if v}}
    if needs.get('local_only') or needs.get('visual'):
        return False
    if route.get('basis', '').startswith(HARD_ROUTE_BASIS) and route.get('preferred'):
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
    if profile:
        # The caller's explicit needs win; the profile fills what the caller left out. A GUI task must see the screen.
        for need, value in (('visual', profile['needs_visual'] or profile['needs_gui']),
                            ('local_only', profile['local_only']), ('critical', profile['error_cost'] >= 3)):
            if needs.get(need) is None:
                needs[need] = value
        if profile['needs_gui']:
            needs['visual'] = True
    implies = capabilities['routes'][kind].get('implies') or {}
    for need in ('visual', 'local_only'):
        needs[need] = bool(needs.get(need) or implies.get(need))
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
            'effort_unlocked': unlocked,
            'high_precision': high_precision,
            'minimum_sufficient': bool(profile and depth <= 2 and profile['code_complexity'] < 3
                                       and div == 1 and (profile['error_cost'] <= 2 or mechanical_runbook)
                                       and (not critical or mechanical_runbook)),
            'wants_fast': bool(needs.get('fast') or task.get('fast') or infer_fast_preference(task.get('objective'))),
            'force_fast': bool(task.get('force_fast') or needs.get('force_fast')),
            'disable_fast': bool(task.get('disable_fast') or needs.get('disable_fast')),
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
    accounts = accounts or load_accounts()
    capabilities = capabilities or load_capabilities()
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
    candidates, eligible, system = rank(task, usage, policy, accounts, capabilities, now, unavailable, applied, records,
                                        host_policy_config)
    if not eligible and not task['metered_ok'] and any(c['excluded'] == ['metered access is a last resort'] for c in candidates):
        candidates, eligible, system = rank({**task, 'metered_ok': True}, usage, policy, accounts, capabilities, now,
                                            unavailable, applied, records, host_policy_config)
    worker_id = (task['worker'] or {}).get('bundle')
    resume = next((c for c in candidates if c['id'] == worker_id), None)
    decision = {'task': {k: task[k] for k in ('kind', 'difficulty', 'critical', 'hours', 'chain_hours', 'coupling')}, 'system': system,
                'assumptions': assumptions(candidates, accounts)}
    decision['task'].update(difficulty_source=task['difficulty_source'],
                            high_precision=task['high_precision'],
                            max_recommended_effort=None if task['effort_unlocked'] else 'high')
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
    result['choice'] = top['id']
    result['selected_bundle'] = dict(top['bundle'], bundle_id=top['id'], expected_usd=top.get('expected_usd'),
                                     expected_hours=top.get('expected_hours'))
    result['expected_usd'] = top.get('expected_usd')
    result['expected_hours'] = top.get('expected_hours')
    result['decision']['alternatives'] = [c['id'] for c in pool if c is not top][:3]
    if top['available_in_hours']:
        decision.update({'action': 'defer', 'start_after_hours': round(top['available_in_hours'], 1)})
    return result


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
    if top['components'].get('reset_bonus'):
        parts.append(f"includes an expiring-reset bonus of {top['components']['reset_bonus']:+.2f}")
    if top['bundle'].get('fast'):
        parts.append(f"fast mode enabled ({top['components'].get('fast_reason', 'plentiful_quota')}, "
                     f"bonus {top['components'].get('fast_bonus', 0):+.2f})")
    if top['components'].get('boost'):
        parts.append(f"includes a temporary user-override boost of {top['components']['boost']:+.2f}")
    return '; '.join(parts)
