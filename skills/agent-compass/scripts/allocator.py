"""Deterministic worker allocation: capability gates, task fit across the four R2 capability categories
(`divergence`, `coding`, `aesthetics`, `tool_operation`), continuity with a coupled chain's existing worker,
and the opportunity cost of the quota the work would use.

Scores are panel standard deviations (`z = (x - x_anchor) / sd_panel`, anchor `claude-opus-5-5` at High = 0) from
`references/five-axis-scores.json`. In `five_axis_fit`, each demanded category's zero point is its level-1 target `t1`
(the panel's weakest model), level 2 targets the panel median `t2`, and level 3 targets the anchor `t3`:
`fit = sum_a w_a * (min(z_a, t_a) - t1_a) / sum_a w_a` (scores below `t1_a` may go below zero; nothing above `t_a`
adds extra credit, and economy outside alternatives require positive fit above zero). Every number that drives a choice
lives in `references/accounts.json` or `references/five-axis-scores.json` and is returned in the per-candidate breakdown.
"""

from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import sys

# Also support the path-loaded allocator and python -I entrypoints.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import account_state
import fast_modes
import window_scope
import host_policy
import launch_failures
import live_models
import model_latch
import provider_retry
import override_store
import antigravity_telemetry
import provider_inventory
import public_data
import user_scores
import accounts_override
import auto_bundles
import cwd_policy
from model_resolution import DIRECT_SOURCES, resolve_canonical_model, resolve_model as _resolve_model, split_model_effort

REFERENCES = Path(__file__).resolve().parents[1] / 'references'
WINDOW_ALIASES = {'seven_day': 'usage', 'usage-10080m': 'usage', 'usage-5h': 'five_hour', 'usage-300m': 'five_hour'}


def canonical_window_id(spec, wid):
    windows = (spec or {}).get('windows') or {}
    if wid in windows:
        return wid
    mapped = WINDOW_ALIASES.get(wid)
    return mapped if mapped in windows else wid


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


METERED_LAST_RESORT = 'metered access is a last resort'
QUOTA_READING_UNAVAILABLE = 'quota reading unavailable (no current observation of any window of this account, so its spend cannot be priced)'
# Round 15 (owner): every quota account has a weekly (long reset-period) window; one without is a surfaced data error, never a
# 5-hour-only account that stands in for other accounts' work as a free outside point.
QUOTA_LONG_UNREAD = ('weekly quota reading unavailable (the account has a long reset-period window but no current or last-good '
                     'observation of it, only its 5-hour window is read, so its spend cannot be priced)')
QUOTA_NO_LONG_WINDOW = ('no weekly quota window (neither the account configuration nor the public capacity data gives this '
                        'account a long reset-period window, so its spend cannot be priced)')
QUOTA_DATA_ERRORS = (QUOTA_READING_UNAVAILABLE, QUOTA_LONG_UNREAD, QUOTA_NO_LONG_WINDOW)


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


def capacity_usd(spec, wid):
    """API-priced dollars one full window allows (the account's `capacity_usd`: community data by plan, or a metered
    account's own prepaid budget), or None when it is unknown."""
    value = (spec.get('capacity_usd') or {}).get(wid)
    return float(value) if value else None


EFFORT_ORDER = ('low', 'medium', 'high', 'xhigh', 'max', 'ultra')


def scoring_effort(bundle, info):
    """The effort a bundle is scored at: its own, else its model's `fixed_effort` (a harness that cannot set one runs
    at the upstream's fixed level; the launcher still passes no effort)."""
    return bundle.get('effort') or (info or {}).get('fixed_effort')


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


def unit_rate(spec, wid, model, info=None):
    """Percent of the window this bundle spends per worker-hour, or None when the window has no cited capacity: the hour's
    list-price dollars (the model's `usd_per_hour`, a rate, so effort is in the hours and not here) over the window's
    dollars (`capacity_usd`). A model with no cited usd is unpriced: 0.0, and rule B keeps it out of automatic routing on a
    metered account."""
    capacity, usd = capacity_usd(spec, wid), (info or {}).get('usd_per_hour')
    if capacity and usd:
        return 100 * usd / capacity
    return 0.0 if usd_unpriced(model, info) else None


def bundle_task_usd(spec, model, hours, info=None, context_burn=1.0):
    """Expected API-equivalent task budget in USD for `hours` active worker-hours (`hours * usd_per_hour * context_burn`)."""
    if spec.get('unlimited') or spec.get('unmetered'):
        return 0.0
    return hours * model_usd_per_hour(model, info) * context_burn


def duration_at(info, effort):
    """(multiplier, basis) of the time a task takes this model at `effort`, a multiple of the anchor's Artificial Analysis
    Time/Task at High: the data's `duration.by_effort` and the basis of that entry (a listing at the effort, a fitted arena
    listing, a series effort conversion or a declared same model). A bundle that names no effort runs the model's single
    configuration, which the data keys High. (None, None) when the data has no time at that effort: nothing stands in."""
    five = ((info or {}).get('five_axis') or {}).get('duration') or {}
    key = effort or 'high'
    mult = (five.get('by_effort') or {}).get(key)
    if isinstance(mult, (int, float)) and not isinstance(mult, bool) and mult > 0:
        return float(mult), (five.get('basis_by_effort') or {}).get(key) or {}
    return None, None


def usd_unpriced(model, info=None):
    """True when the model has no cited `usd_per_hour` (the data's value was removed, never defaulted). It prices at 0.0 and
    leaves automatic routing on a metered account."""
    return (info or {}).get('usd_per_hour') is None


def model_usd_per_hour(model, info=None):
    """List-price dollars of one active worker-hour at High (`usd_per_hour`). A model without one prices at 0 instead of a
    silent default: rule B keeps it out of automatic routing on a metered account, and a named dispatch carries a note
    that its cost is not priced."""
    usd = (info or {}).get('usd_per_hour')
    return 0.0 if usd is None else float(usd)


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
    """The model row for a name: `exact`/`alias` read the row itself; a name that is not a row (a family guess, an unknown
    name) has none, so it carries no scores and no price (rule B)."""
    resolved = resolve_model(model, accounts)
    value = accounts.get('models', {}).get(resolved['key']) if resolved['source'] in DIRECT_SOURCES else None
    return value if isinstance(value, dict) else {}


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
                              **{k: bundle[k] for k in ('modeId', 'disabled', 'disabled_by_default', 'efforts', 'launch_model',
                                                        'discovery_unavailable', 'draws',
                                                        'auto', 'billing', 'score_source', 'score_basis',
                                                        'availability', 'compatibility', 'compatibility_reason') if k in bundle}}
    return out


INTERACTION_STYLES = ('engineering', 'exploration', 'text_only', 'advisory')
PROFILE_INTEGERS = {'reasoning_depth': (1, 4), 'code_complexity': (0, 3)}
PROFILE_FLAGS = ('needs_visual', 'needs_gui', 'local_only', 'wants_fast', 'read_only_review')
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
DIFFICULTY_ORDER = ('routine', 'hard', 'coupled', 'frontier')
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
        'needs_visual': {'type': 'boolean', 'description': 'True when the work must look at images, screenshots or other visual input.'},
        'needs_gui': {'type': 'boolean', 'description': 'True when the work must operate a desktop or browser GUI through screenshots, mouse and keyboard.'},
        'local_only': {'type': 'boolean', 'description': (
            'True when the task explicitly depends on existing files, directories, or repositories on this machine '
            '(reading, copying, searching, editing, building, or testing un-inlined local files) or on local processes/GUI/'
            'environment. False when all required inputs are inlined in the prompt or fetched from the public web '
            '(web_research, self_contained_code, text_in_text_out, direction_reasoning, complex_build_plan)—even if the '
            'brief has a workspace header or names a local target path where the finished output will be saved.')},
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
PROFILE_KINDS = ('general_implementation', 'frontend_implementation', 'debug_review', 'bounded_bulk_work', 'tool_execution',
                 'self_contained_code', 'exploration_analysis', 'research_packet', 'computer_use', 'visual_analysis',
                 'text_in_text_out', 'web_research', 'dot_execution', 'direction_reasoning', 'complex_build_plan',
                 'other_or_mixed')
GENERIC_CALLER_KINDS = (None, 'general_implementation', 'tool_execution', 'debug_review', 'other_or_mixed')


def validate_profile(raw):
    """A task profile with every field checked, or ValueError. `reason`, `work_kind` and the hours estimate may be omitted."""
    if not isinstance(raw, dict):
        raise ValueError('task.profile must be an object')
    profile = {}
    if raw.get('work_kind') is not None:
        if not isinstance(raw['work_kind'], str) or raw['work_kind'] not in PROFILE_KINDS:
            raise ValueError('task.profile.work_kind must be one of ' + ', '.join(PROFILE_KINDS))
        profile['work_kind'] = raw['work_kind']
    for name, (low, high) in PROFILE_INTEGERS.items():
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
    # The hours estimate is the classifier's to give; with no caller `expected_hours` it is the task's budget.
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
    # The classifier always answers the category demand.
    if raw.get('axis_demand') is None:
        raise ValueError('task.profile.axis_demand is required')
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
    """The label for a profile's reasoning depth, from the policy's `profile.difficulty_by_depth` (a display label: nothing
    scores on it)."""
    return policy['profile']['difficulty_by_depth'][str(profile['reasoning_depth'])]


def named_route(profile):
    """A classifier route other than the unmatched bucket. None when the profile did not name one."""
    if not isinstance(profile, dict):
        return None
    named = profile.get('work_kind')
    if named in PROFILE_KINDS and named != 'other_or_mixed':
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


def named_effort_bundles(task, ctx):
    """Rows for the effort the user's pin names on a model the account has no row at that level for, so the score decides
    among them exactly as it does among written rows. The launch contract is what offers a level; each row is the
    model's own carrier with a distinct id, so it never passes for the carrier itself."""
    effort = task.get('constraint_effort')
    if not effort or task.get('constraint_bundle'):
        return {}
    accounts, groups = ctx['accounts'], {}
    for bundle in ctx['bundles'].values():
        model = row_key(bundle['model'], accounts)
        if task.get('constraint_model') and not model_pin_matches(task, bundle, model):
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
            levels = [effort]
            if (effort not in allowed) if allowed is not None else not task.get('constraint_model'):
                levels = []
            for level in levels:
                if level in written or (isinstance(carrier.get('efforts'), list) and level not in carrier['efforts']):
                    continue
                written.add(level)
                row_id = f"{carrier['id']}@{level}"
                found[row_id] = {**{k: v for k, v in carrier.items()}, 'id': row_id, 'effort': level,
                                 'pin_carrier': carrier['id']}
    return found


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


# --- Scoring: the four category scores and the quota economy (the only scoring) -----------------------------------------
# Model data comes from references/five-axis-scores.json (calibration/five_axis.py, cited public sources only). The four
# categories (user, 2026-11-11): divergence, coding, aesthetics, tool_operation. Every board is read as a panel standard
# score: z = (x - x_anchor) / sd_panel, the anchor (Opus 5.5) at High is 0 and one unit is one panel standard deviation;
# parallel boards of one facet are linked and averaged, the facets of a category combine by `min`, and a facet a model
# lacks is estimated (same-series sibling, else the fit on the arena.ai Overall tier) and marked estimated.
# - A task demands levels 0..3 on the four categories (the classifier's `axis_demand`, always produced) and nothing
#   else scores or gates on quality: no scalar tier, no route preference or restriction lists, no harness quality
#   factors, no difficulty floors, no free-usage bonus.
# - Targets come from the panel (five-axis-scores.json `anchor.targets`): level 1 the panel's weakest, level 2 its median,
#   level 3 the anchor. tool_operation asks per facet: level 1 nothing, level 2 responsiveness, level 3 responsiveness
#   and image recognition, never less of a facet than the level below.
# - Fit (five_axis_fit) = sum_a w_a x (min(z_a, t_a) - t1_a) / sum_a w_a over the demanded categories, w_a the demand
#   level, t_a the level's target and t1_a the level-1 target (the panel's weakest). It is in panel standard deviations:
#   0 is the panel's weakest model on every demanded category, the anchor scores t_a - t1_a, and no score above the
#   target earns more. A candidate below the panel's weakest scores below 0. The economy reads the same fit, and 0 is
#   its total loss (a candidate at or below it is never an outside alternative).
# - Continuity: continuing a related worker (coupling high, the same bundle) is worth one demand level, the largest
#   positive t3 - t2 among the task's highest-demand categories (else 0); the 2M continuation cap (worker_worn) stands.
# - Missing data (rule B): a demanded category with no data at all, or no cited usd on a billed account, leaves
#   automatic routing; a named model still runs, with the data error as a warning.
# - Plan capacity is the cited figure of the capacity ladder (community, time-to-exhaust, vendor multiplier), priced at
#   the merged usd scale; a window with an empty ladder is a data error that still prices scarcity from its live
#   percentage and reset time.
# - Economy: every long window charges the candidate's burn (usd over the window's capacity) times the window's quota
#   price (FiveAxisEconomy), counted in hours of the window's reference model.
FIVE_AXIS_FILE = REFERENCES / 'five-axis-scores.json'
USER_SCORES_FILE = REFERENCES / 'user-scores.json'
FIVE_AXES = ('divergence', 'coding', 'aesthetics', 'tool_operation')
FIVE_AXIS_DEFAULTS = {
    # fit and continuity are both in panel standard deviations: one unit each, not a hand-set trade-off
    'weights': {'fit': 1.0, 'continuity': 1.0},
    'demanded_level': 1,         # rule B: a category demanded at this level or above must have data
}
# What each demand level 0..3 means per category: the one text both the schema of the JSON classifiers and the questions of
# the System-One (Jev) classifier are built from (user, 2026-11-11).
FIVE_AXIS_DEMAND_LEVELS = {
    'divergence': ('Divergent work: creative writing, brainstorming, research synthesis and data analysis, and decomposing '
                   'a problem from first principles outside the existing architecture.', (
        'execute a known spec or checklist',
        'small judgment calls or wording choices',
        'several options or hypotheses to generate and compare, or a research or data-analysis question to investigate '
        'and synthesize',
        'open-ended creative writing, open research synthesis across many sources, or a first-principles redesign outside '
        'the existing architecture')),
    'coding': ('Coding ability: reading, writing, reviewing and changing code correctly, doing exactly what was asked and '
               'reporting it honestly.', (
        'no code read or written',
        'a small script or config change, or a result a test verifies',
        'a production module with its tests, or a review or audit of one',
        'changes across coupled modules, concurrency, protocol or core-library work, a whole-system review or audit, or '
        'a defect would be costly or irreversible')),
    'aesthetics': ('Visual and design judgment: GUI and interaction design, layout and typesetting, tables and charts, '
                   '2D or 3D modeling.', (
        'no visual output',
        'plain output where only tidy formatting matters',
        'a user-facing screen, page, chart or document whose look matters',
        'design-led work: a new interface or interaction, a visual identity, a 3D scene or model')),
    'tool_operation': ('Operating tools fast: per-turn speed inside a tool loop, and reading images. Writing or fixing '
                       'software about real-time systems is coding, not a live loop.', (
        'reads files and runs a few shell commands',
        'routine tool or API calls',
        'heavy tool or API orchestration, or the worker itself stays inside a live external system (a game, a ticket '
        'queue, an order book, a live page or chat) where slow turns degrade the outcome',
        'must read images or screenshots inside the loop, or operate a desktop or browser GUI through screenshots')),
}
FIVE_AXIS_DEMAND_SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': list(FIVE_AXES),
    'description': ('How much the task needs each capability category of the model, 0..3. Economy is not a task demand: '
                    'the router prices it from cost and live quota.'),
    'properties': {axis: {'type': 'integer', 'minimum': 0, 'maximum': 3,
                          'description': intro + ' ' + '; '.join(f'{i} = {text}' for i, text in enumerate(levels)) + '.'}
                   for axis, (intro, levels) in FIVE_AXIS_DEMAND_LEVELS.items()},
}
# tool_operation's facets per demand level (calibration/five_axis.TOOL_LEVEL_FACETS): level 1 imposes no ability requirement.
TOOL_LEVEL_FACETS = {1: (), 2: ('responsiveness',), 3: ('responsiveness', 'image_recognition')}
IMAGE_INPUT_LEVEL = 3           # tool_operation at this level must read images: a text-only model leaves the task
# The demand fields of the four-axis taxonomy before 2026-11-11, read only from in-flight ledger records.
LEGACY_AXES = ('divergence', 'global_rigor', 'module_precision', 'vision_tools')
LEGACY_VISION_TO_TOOL = {0: 0, 1: 2, 2: 3, 3: 3}


def five_axis_config(policy):
    raw = (policy or {}).get('five_axis')
    raw = raw if isinstance(raw, dict) else {}
    return {**FIVE_AXIS_DEFAULTS, **raw, 'weights': {**FIVE_AXIS_DEFAULTS['weights'], **(raw.get('weights') or {})}}


def load_five_axis(path=None, *, use_effective=True):
    default_file = REFERENCES / 'five-axis-scores.json'
    target = Path(path) if path is not None else (public_data.effective(default_file) if use_effective else default_file)
    return json.loads(target.read_text(encoding='utf-8'))


def load_public_user_scores(path=None, *, use_effective=True):
    default_file = REFERENCES / 'user-scores.json'
    target = Path(path) if path is not None else (public_data.effective(default_file) if use_effective else default_file)
    return user_scores.load_public(target, effective=False)


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


def ledger_profile(profile):
    """A recorded profile in the current shape: the one read-side mapping for in-flight ledger records written before the
    four categories (user, 2026-11-11). divergence as is; coding = max(global_rigor, module_precision); tool_operation
    from vision_tools 0->0, 1->2, 2->3, 3->3, and at least 2 when the record marked `realtime_loop` (its live loop is
    tool_operation level 2 now); aesthetics 0 (the old record carries nothing about it). A profile already in the current
    shape, or not a dict, comes back unchanged; validation stays with validate_profile."""
    if not isinstance(profile, dict):
        return profile
    old = profile.get('axis_demand')
    if not (isinstance(old, dict) and set(old) == set(LEGACY_AXES)) and 'realtime_loop' not in profile:
        return profile
    out = {k: v for k, v in profile.items() if k != 'realtime_loop'}
    if isinstance(old, dict) and set(old) == set(LEGACY_AXES):
        try:
            vision = LEGACY_VISION_TO_TOOL[int(old['vision_tools'])]
            out['axis_demand'] = {'divergence': old['divergence'],
                                  'coding': max(int(old['global_rigor']), int(old['module_precision'])),
                                  'aesthetics': 0,
                                  'tool_operation': max(vision, 2) if profile.get('realtime_loop') is True else vision}
        except (KeyError, TypeError, ValueError):
            return out
    elif profile.get('realtime_loop') is True and isinstance(old, dict) and isinstance(old.get('tool_operation'), int):
        out['axis_demand'] = {**old, 'tool_operation': max(old['tool_operation'], 2)}
    return out


def opencode_list_prices(models_table):
    """{name or alias: (table name, cited list price row)} for models whose public data carries an input and output list
    price in USD per million tokens (`five_axis.list_price`, from the data's `price`: AA first, OpenRouter second)."""
    prices = {}
    for name, info in (models_table or {}).items():
        price = ((info or {}).get('five_axis') or {}).get('list_price') if isinstance(info, dict) else None
        value = (price or {}).get('value') if isinstance(price, dict) else None
        if isinstance(value, dict) and all(isinstance(value.get(k), (int, float)) and not isinstance(value.get(k), bool)
                                           for k in ('input', 'output')):
            for key in (name, *(info.get('aliases') or ())):
                prices[str(key)] = (name, price)
    return prices


def opencode_token_usd(price, tok_in, tok_out, tok_cache_read, tok_cache_write):
    """List-price dollars of one model's tokens: input and cache writes at the input price, cache reads at the cited
    cache-read price (0 when the data has none: a lower bound), output (with reasoning) at the output price."""
    value = price['value']
    return (float(tok_in or 0) * value['input'] + float(tok_cache_write or 0) * value['input']
            + float(tok_cache_read or 0) * float(value.get('cache_read') or 0.0)
            + float(tok_out or 0) * value['output']) / 1e6


def opencode_local_spend(models_table, discovery=None, *, read_local_db=True):
    """(spend, unpriced): lower-bound USD per model from local `opencode.db` token counts at the model's public list
    price, and the sorted ids of models with tokens but no list price in the data (round 12 S6). A model id matches a
    table name or alias exactly; there is no prefix guessing and no invented price, so a model without a cited price is
    a surfaced gap (mode B), never a $0 or a borrowed rate. When `read_local_db=False` (fresh/public evaluation),
    no machine-local `opencode.db` is read."""
    import sqlite3
    prices = opencode_list_prices(models_table)
    readings = []       # (source, model id, input, output + reasoning, cache read, cache write)
    disc_models = (((discovery or {}).get('accounts') or {}).get('opencode') or {}).get('usage', {}).get('models') or {}
    for model_id, m_row in disc_models.items():
        if isinstance(m_row, dict):
            readings.append(('discovery', model_id, m_row.get('input'),
                             float(m_row.get('output') or 0) + float(m_row.get('reasoning') or 0),
                             m_row.get('cache_read'), m_row.get('cache_write')))
    db_path = (Path(os.environ.get('OPENCODE_DB_PATH')
                    or (Path.home() / '.local' / 'share' / 'opencode' / 'opencode.db'))
               if read_local_db else None)
    if db_path is not None and db_path.exists():
        try:
            conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=1.0)
            try:
                rows = conn.execute(
                    "SELECT json_extract(data, '$.modelID'), "
                    "SUM(COALESCE(json_extract(data, '$.tokens.input'), 0)), "
                    "SUM(COALESCE(json_extract(data, '$.tokens.output'), 0) + COALESCE(json_extract(data, '$.tokens.reasoning'), 0)), "
                    "SUM(COALESCE(json_extract(data, '$.tokens.cache.read'), 0)), "
                    "SUM(COALESCE(json_extract(data, '$.tokens.cache.write'), 0)) "
                    "FROM message WHERE json_extract(data, '$.role') = 'assistant' "
                    "GROUP BY json_extract(data, '$.modelID')"
                ).fetchall()
            finally:
                conn.close()
        except Exception:
            rows = []
        readings += [('db', *row) for row in rows if row[0]]
    by_source = {'discovery': {}, 'db': {}}
    unpriced = set()
    for source, model_id, tok_in, tok_out, tok_cr, tok_cw in readings:
        if float(tok_in or 0) + float(tok_out or 0) + float(tok_cr or 0) + float(tok_cw or 0) <= 0:
            continue
        hit = prices.get(str(model_id))
        if hit is None:
            unpriced.add(str(model_id))
            continue
        name, price = hit
        by_source[source][name] = by_source[source].get(name, 0.0) + opencode_token_usd(price, tok_in, tok_out, tok_cr, tok_cw)
    spend = {}
    for name in set(by_source['discovery']) | set(by_source['db']):     # the two reads can overlap: keep the larger
        usd = max(by_source['discovery'].get(name, 0.0), by_source['db'].get(name, 0.0))
        if usd > 0:
            spend[name] = round(usd, 4)
    return spend, sorted(unpriced)


def five_axis_overlay(accounts, data=None, *, read_local_state=True, public_scores=None, local_scores=None):
    """A copy of `accounts` carrying the scoring data of references/five-axis-scores.json plus manual scores
    (`references/user-scores.json` maintainer layer, then local `user-scores.json` when `read_local_state=True`).

    Each model row gets `five_axis` {categories (the scores file's category cells: value, by_effort, facets), scores,
    missing, estimates, axis_status, tier, usd_status, image_input, fast_mode, duration: {status, by_effort,
    basis_by_effort, efforts_without_time}}: the time a task takes per effort as a multiple of
    the anchor's Artificial Analysis Time/Task at High, each with its cited basis (`duration_at`); usd_per_hour becomes the
    merged cited value (an unsourced usd is removed, not defaulted). Each windowed account's capacity is the
    data's community real-usage figure for its plan and window (round 4), replacing the packaged figure and any dated
    change; a window without one is marked unknown, with no template fallback. Free pools are ordinary quota pools with
    C/U/t0/T (round 11): when capacity is unreadable, they enter mode B (`capacity_unknown`), out of automatic routing."""
    import copy
    explicit_data = data is not None
    cfg_path = five_axis_config(accounts.get('scoring')).get('path')
    data = data if explicit_data else (load_five_axis(cfg_path) if read_local_state
                                       else load_five_axis(cfg_path, use_effective=False))
    out = copy.deepcopy(accounts)
    rows = data.get('models') or {}
    for name, info in (out.get('models') or {}).items():
        if not isinstance(info, dict):
            continue
        row = rows.get(name)
        usd = ((row or {}).get('usd_per_hour') or {}).get('value')
        img = (row or {}).get('image_input') or {'status': 'missing'}
        fast = (row or {}).get('fast_mode') or {'status': 'not_offered'}
        dur_row = (row or {}).get('duration_multiplier') or {}
        price = (row or {}).get('price')       # public list price, USD per million tokens (aa.price, else openrouter.price)
        cats = (row or {}).get('axes') or {}
        info['five_axis'] = {'categories': {a: cats[a] for a in FIVE_AXES if isinstance(cats.get(a), dict)},
                             'scores': {a: (cats.get(a) or {}).get('value') for a in FIVE_AXES},
                             'missing': list(FIVE_AXES) if row is None else list(row.get('missing_axes') or ()),
                             'estimates': {} if row is None else estimate_summary(row),
                             'axis_status': {a: (cats.get(a) or {}).get('status', 'missing') for a in FIVE_AXES},
                             'tier': (row or {}).get('tier') or {'value': None, 'method': 'none'},
                             'usd_status': 'ok' if usd is not None else 'missing',
                             'image_input': img,
                             'fast_mode': fast,
                             'duration': {k: dur_row[k] for k in ('status', 'by_effort', 'basis_by_effort',
                                                                  'efforts_without_time') if k in dur_row},
                             **({'list_price': price} if isinstance(price, dict) and price.get('value') else {})}
        if usd is not None:
            info['usd_per_hour'] = usd
        else:
            info.pop('usd_per_hour', None)
    local_spend, local_unpriced = opencode_local_spend(
        out.get('models') or {}, out.get('_discovery'), read_local_db=bool(read_local_state))
    community = data.get('capacity') or {}
    for name, spec in (out.get('accounts') or {}).items():
        if not isinstance(spec, dict):
            continue
        spec.pop('observed', None)          # never feed 3h/24h historical rates into five-axis economics
        # A free pool is OpenCode's free starter allowance, declared by the account's own `free_allowance` field (or a
        # free plan), never by its name. `unmetered` alone does not make one: Dot and ChatGPT Web are purpose-limited
        # channels that usually cannot be used up, and stay in automatic routing (owner, 2026-11-10).
        is_free_pool = bool(spec.get('free_allowance') or str(spec.get('plan') or '') == 'free')
        family = str(provider_inventory.account_family(name, out, out.get('_discovery') or {}) or account_family(spec) or '')
        family = family.removesuffix('-resilient')
        if is_free_pool and spec.get('billing') != 'metered':
            # Round 11 (user 2026-11-10): "free" models are an ordinary quota pool (same C/U/t0/T; a starter allowance
            # has T = inf so recovery line is 0 and X = S). Remove unmetered / unlimited / $0 spend on the five-axis path.
            spec.pop('unmetered', None)
            spec.pop('unlimited', None)
            spec['five_axis_free_pool'] = True
            if not spec.get('windows'):
                spec['windows'] = {'allowance': {'hours': None}}
                spec['capacity_usd'] = {}
                spec['capacity_unknown'] = True
                spec['capacity_source'] = {'allowance': {'status': 'missing', 'plan': spec.get('plan') or 'free'}}
                # Auto bundles were derived while this account declared no window, so they carry an explicit empty `draws`
                # (authoritative): every model of a free pool spends the one allowance, so they draw it (round 15).
                for b in (spec.get('bundles') or {}).values():
                    if isinstance(b, dict) and b.get('draws') == []:
                        b['draws'] = ['allowance']
            acct_spend = 0.0
            seen_models = set()
            for b in (spec.get('bundles') or {}).values():
                if not isinstance(b, dict):
                    continue
                m_key = canonical(b.get('model') or '', out)
                if m_key in local_spend and m_key not in seen_models:
                    seen_models.add(m_key)
                    acct_spend += local_spend[m_key]
            if acct_spend == 0.0 and family == 'opencode':      # opencode.db is this family's own usage record
                acct_spend = sum(local_spend.values())
            if acct_spend > 0:
                spec['local_spend_usd'] = round(acct_spend, 4)
                spec['local_spend_source'] = 'opencode.db token counts x public list price (lower bound)'
            if family == 'opencode' and local_unpriced:
                # no cited list price for these models: their spend is a surfaced gap, not $0 and not a borrowed rate
                spec['local_spend_gap'] = list(local_unpriced)
        if not spec.get('windows') or spec.get('unmetered'):
            continue
        plan = provider_inventory.configured_plan(family, spec.get('plan'))
        if family == 'pi':      # Pi signs in with ChatGPT and spends the Codex account's allowance: its plan's windows
            family, plan = 'codex', next((provider_inventory.configured_plan('codex', s.get('plan'))
                                          for n, s in (out.get('accounts') or {}).items() if isinstance(s, dict)
                                          and str(provider_inventory.account_family(n, out, out.get('_discovery') or {})
                                                  or account_family(s) or '') == 'codex'), plan)
        rows_ = (community.get(family) or {}).get(plan) or {}
        explicit_caps = spec.get('capacity_usd') if isinstance(spec.get('capacity_usd'), dict) else {}
        caps = {wid: float(rows_[wid]['capacity_usd']) for wid in spec['windows']
                if isinstance((rows_.get(wid) or {}).get('capacity_usd'), (int, float))}
        # A free allowance or a metered account (a prepaid $200 proxy) states its own capacity: community data measures
        # subscription plans only. A subscription's packaged figure is not a declaration and stays unknown (round 11).
        if not caps and explicit_caps and (is_free_pool or spec.get('billing') == 'metered'):
            caps = {wid: float(v) for wid, v in explicit_caps.items()
                    if wid in spec['windows'] and isinstance(v, (int, float)) and v > 0}
        spec['capacity_usd'] = caps
        spec['capacity_source'] = {wid: {'status': ('explicit' if wid in caps and wid not in rows_
                                                    else (rows_.get(wid) or {}).get('status', 'missing')),
                                         'age_days': (rows_.get(wid) or {}).get('age_days'), 'plan': plan or None}
                                   for wid in spec['windows']}
        unknown = [wid for wid in spec['windows'] if wid not in caps]
        if unknown:
            spec['capacity_unknown'] = True
        else:
            spec.pop('capacity_unknown', None)
    anchor = data.get('anchor') or {}
    targets = anchor.get('targets') or {}
    if not anchor.get('model') or set(targets) != set(FIVE_AXES):
        raise ValueError('five-axis-scores.json names no anchor model or no panel targets for the four categories: the fit '
                         'has nothing to stand on')
    out['_five_axis'] = {'generated_at': data.get('generated_at'), 'anchor': anchor['model'], 'targets': targets,
                         'panel_n': anchor.get('panel_n')}
    pub_manual = public_scores if public_scores is not None else (
        None if explicit_data else (load_public_user_scores() if read_local_state
                                    else load_public_user_scores(use_effective=False))
    )
    loc_manual = local_scores if local_scores is not None else (user_scores.load_local() if read_local_state else None)
    user_scores.apply_overlay(out.setdefault('models', {}), targets, public_scores=pub_manual, local_scores=loc_manual)
    for info in (out.get('models') or {}).values():
        if isinstance(info, dict) and isinstance(info.get('five_axis'), dict):
            info['five_axis']['shares'] = five_axis_shares(five_axis_values(info['five_axis'], 'high'), targets)
    return out


def five_axis_demand(task):
    """{category: level 0..3}: the classifier's `axis_demand` as it stands (validated with the profile). An all-zero answer
    is a task that asks nothing of the categories: every model fits it alike and the economy decides."""
    return dict(task['profile']['axis_demand'])


def estimate_summary(row):
    """{'category.facet': provenance} for the facets of a five-axis-scores row that were estimated (a same-series sibling
    or the fit on the tier) rather than measured."""
    return {f'{a}.{f}': dict(comp.get('estimate') or {})
            for a, ax in (row.get('axes') or {}).items() for f, comp in (ax.get('facets') or {}).items()
            if comp.get('status') == 'estimated'}


def _at_effort(cell, effort):
    """A score cell's value at `effort`: its per-effort entry in `by_effort` when `by_effort` is present (an effort-less
    bundle runs its single configuration, keyed `'high'` or its lone entry), else `cell['value']` for minimal cells."""
    if not isinstance(cell, dict):
        return None
    if 'by_effort' in cell and isinstance(cell.get('by_effort'), dict):
        by = cell['by_effort']
        if not by:
            return None
        value = by.get(effort) if effort else (by.get('high') if 'high' in by else cell.get('value'))
        return None if value is None else float(value)
    value = cell.get('value')
    return None if value is None else float(value)


def five_axis_values(state, effort):
    """{category: z}, with tool_operation as {facet: z}: this model's panel standard scores at `effort` (None where it has
    no value). The thinking categories carry the model's own additive effort shift (five_axis.py); responsiveness is
    measured per effort."""
    cats = (state or {}).get('categories') or {}
    out = {a: _at_effort(cats.get(a), effort) for a in FIVE_AXES if a != 'tool_operation'}
    facets = ((cats.get('tool_operation') or {}).get('facets')) or {}
    out['tool_operation'] = {f: _at_effort(facets.get(f), effort) for f in TOOL_LEVEL_FACETS[3]}
    return out


def five_axis_targets(demand, targets):
    """{category: the demanded level's target row} from five-axis-scores.json `anchor.targets`, for every demanded category
    that asks for something (tool_operation level 1 asks for nothing)."""
    out = {}
    for a, w in demand.items():
        if w <= 0:
            continue
        row = ((targets.get(a) or {}).get('levels') or {}).get(str(w)) or {}
        if a == 'tool_operation':
            if row.get('by_facet'):
                out[a] = {'by_facet': {f: {'target': r['target'], 'zero': r['zero']} for f, r in row['by_facet'].items()}}
        elif row.get('target') is not None:
            out[a] = {'target': row['target'], 'zero': row['zero']}
    return out


def five_axis_fit(values, demand, targets):
    """(fit, {category: row}): sum_a w_a x (min(z_a, t_a) - t1_a) / sum_a w_a over the demanded categories that ask for
    something, in panel standard deviations (module comment above). tool_operation reads min over its level's facets of
    (min(z_f, t_f) - t1_f). A category or facet without a value stands at its zero (the panel's weakest): rule B keeps
    such a model out of automatic routing, so this only prices a named dispatch."""
    rows, value, total = {}, 0.0, 0
    for a, w in demand.items():
        tgt = targets.get(a)
        if w <= 0 or not tgt:
            continue
        if 'by_facet' in tgt:
            parts = {}
            for f, t in tgt['by_facet'].items():
                z = (values.get(a) or {}).get(f)
                parts[f] = {'score': None if z is None else round(z, 4), 'target': t['target'], 'zero': t['zero'],
                            'term': round((min(z, t['target']) - t['zero']) if z is not None else 0.0, 4)}
            scored = list(parts.values())
            term = min(p['term'] for p in scored)
            rows[a] = {'demand': w, 'facets': parts, 'term': term,
                       'shortfall': round(max(max(0.0, p['target'] - p['score']) if p['score'] is not None
                                              else p['target'] - p['zero'] for p in scored), 4)}
        else:
            z = values.get(a)
            term = round((min(z, tgt['target']) - tgt['zero']) if z is not None else 0.0, 4)
            rows[a] = {'demand': w, 'score': None if z is None else round(z, 4), 'target': tgt['target'],
                       'zero': tgt['zero'], 'term': term,
                       'shortfall': round(max(0.0, tgt['target'] - z) if z is not None else tgt['target'] - tgt['zero'], 4)}
        value += w * term
        total += w
    return (value / total if total else 0.0), rows


def five_axis_shares(values, targets):
    """{category: 0..1} for display (the tray radar): (min(z, t3) - t1) / (t3 - t1), where t1 is the level-1 target (the
    panel's weakest) and t3 the level-3 target (the anchor): 0 at the panel's weakest, 1 at the level the hardest demand
    asks, never more (the fit gives no credit above a target either). tool_operation takes the weakest of its level-3
    facets when all required level-3 facets have a value; None where any required facet has no value."""
    def share(z, row):
        if z is None or not row or row.get('target') is None or row['target'] <= row['zero']:
            return None
        return round(max(0.0, (min(z, row['target']) - row['zero']) / (row['target'] - row['zero'])), 4)
    out = {}
    for a in FIVE_AXES:
        level3 = ((targets.get(a) or {}).get('levels') or {}).get('3') or {}
        if a == 'tool_operation':
            parts = [share((values.get(a) or {}).get(f), r) for f, r in (level3.get('by_facet') or {}).items()]
            out[a] = min(parts) if parts and all(p is not None for p in parts) else None
        else:
            out[a] = share(values.get(a), level3)
    return out


def five_axis_level(rows):
    """The demand-weighted mean of the candidate's scores over the demanded categories (tool_operation: the weakest of its
    level's facets), uncapped: the last tie-break between equal net scores. A missing value stands at its zero."""
    total, value = 0, 0.0
    for r in rows.values():
        if 'facets' in r:
            z = min(p['score'] if p['score'] is not None else p['zero'] for p in r['facets'].values())
        else:
            z = r['score'] if r['score'] is not None else r['zero']
        value += r['demand'] * z
        total += r['demand']
    return value / total if total else 0.0


def five_axis_continuity_value(demand, targets):
    """The worth of continuing a related worker, in panel standard deviations (user, 2026-11-11): one demand level, the
    largest positive t3 - t2 among all demanded categories (five-axis-scores.json `anchor.targets.<category>.continuity`),
    else 0. A zero-gap category such as `tool_operation` never wipes out a demanded thinking category's continuity."""
    gaps = [(targets.get(a) or {}).get('continuity') for a, w in demand.items() if w >= 1]
    return max([g for g in gaps if isinstance(g, (int, float)) and g > 0], default=0.0)


def five_axis_window_recovery(state, cap_usd):
    """One quota window on its own clock (quota economy v3): capacity C, used U (in-flight leases included), elapsed
    fraction e of the period and the reset. `line_now_usd` = C * e is the recovery line: the pace at which the period's
    capacity just runs out. The expected demand still to come at U = C * e equals the remaining capacity (the mean identity
    in `five_axis_need_tail`); `five_axis_need_tail` there is near one half (0.47 to 0.63 with the burst size of v3.1), so
    the line is a reference pace and not an exact probability contour.

    Starter allowances (`hours is None` or `inf`, `reset_hours` inf) never reset: the clock does not run, e = 0. Rolling
    windows that start their clock on first use (`state.get('unstarted')`) are at e = 0 with U = 0. Nothing here reads a
    history: only the period's own readings."""
    if not cap_usd or cap_usd <= 0 or not state or not state.get('known'):
        return None
    full = float(state.get('full', 100.0) or 100.0)
    pool_cap = cap_usd * (full / 100.0)
    rem_pct = float(state.get('remaining', full))
    used_usd = max(0.0, (full - rem_pct) / 100.0 * cap_usd)
    hours = state.get('hours')
    reset_h = state.get('reset_hours')
    if hours is None or reset_h is None or hours == float('inf') or reset_h == float('inf') or float(hours) <= 0:
        return {'capacity_usd': pool_cap, 'used_usd': used_usd, 'elapsed_frac': 0.0, 'line_now_usd': 0.0,
                'period_hours': None, 'reset_hours': float('inf'), 'unstarted': False}
    L = float(hours)
    if state.get('unstarted') and used_usd <= 1e-9:
        return {'capacity_usd': pool_cap, 'used_usd': 0.0, 'elapsed_frac': 0.0, 'line_now_usd': 0.0,
                'period_hours': L, 'reset_hours': L, 'unstarted': True}
    tau = max(0.0, min(L, float(reset_h)))
    elapsed = (L - tau) / L
    return {'capacity_usd': pool_cap, 'used_usd': used_usd, 'elapsed_frac': elapsed, 'line_now_usd': pool_cap * elapsed,
            'period_hours': L, 'reset_hours': tau, 'unstarted': False}


def _beta_continued_fraction(a, b, x):
    """Continued fraction of the incomplete beta function (modified Lentz), the series both tails use."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    d = 1.0 / (tiny if abs(d) < tiny else d)
    h = d
    for m in range(1, 500):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (tiny if abs(d) < tiny else d)
        c = 1.0 + aa / c
        c = tiny if abs(c) < tiny else c
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (tiny if abs(d) < tiny else d)
        c = 1.0 + aa / c
        c = tiny if abs(c) < tiny else c
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-13:
            break
    return h


def regularized_incomplete_beta(a, b, x):
    """I_x(a, b) for real a, b > 0 and 0 <= x <= 1 (pure Python; the continuous extension of the negative-binomial tail)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_front = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x))
    direct = x < (a + 1.0) / (a + b + 2.0)
    if log_front < -700.0:      # the front factor underflows: the tail on that side is zero to double precision
        return 0.0 if direct else 1.0
    front = math.exp(log_front)
    if direct:
        return min(1.0, front * _beta_continued_fraction(a, b, x) / a)
    return max(0.0, 1.0 - front * _beta_continued_fraction(b, a, 1.0 - x) / b)


def five_axis_need_tail(rec, room_usd):
    """P(the demand that reaches this window before its reset is at least `room_usd`), from the period's own readings.

    Quota economy v3.1 (advisor report v3 section 1.2 with the burst size of v3.1, owner option A, round 15): consumption
    arrives in lumps of size q = `rec['lump_usd']`; the prior is one virtual lump at the recovery-line pace and the
    observation is the U / q lumps seen in the elapsed fraction e of the period. The lumps still to come are negative
    binomial, K ~ NegBin(n + 1, p) with n = U / q, s = q / C and p = (e + s) / (1 + s), so
    P(K * q >= room) = I_{(1 - e) / (1 + s)}(room / q, n + 1). The mean of the demand still to come is
    (U + q) (1 - e) / (e + s), which at U = e C is exactly the remaining capacity C - U for every q (the mean identity).
    `rec` is `five_axis_window_recovery`'s answer plus `lump_usd`, which `FiveAxisEconomy.lump` builds from the account's own
    candidates and the window's own reset clock; a record without it is an error, there is no fallback to C (round 15).
    Only long reset-period windows are priced: a 5-hour window never calls this."""
    lump = min(float(rec['capacity_usd']), float(rec['lump_usd']))      # KeyError when the record has no lump: no default
    if room_usd <= 0:
        return 1.0
    cap = float(rec['capacity_usd'])
    e = float(rec.get('elapsed_frac') or 0.0)
    if cap <= 0 or e >= 1.0 or lump <= 0:
        return 0.0
    s = lump / cap
    return regularized_incomplete_beta(room_usd / lump, float(rec['used_usd']) / lump + 1.0, (1.0 - e) / (1.0 + s))


def five_axis_need_probability(rec, level_usd):
    """pi_w(y): the probability the dollar at cumulative use `level_usd` of this window is needed before its reset."""
    return five_axis_need_tail(rec, float(rec['capacity_usd']) - float(level_usd))


def five_axis_substitute_slack(rec, spend_usd):
    """a_v: the effective availability of window `rec` for a displaced task of `spend_usd`: `(1 - (1 - e)^2 (U + spend)/C)`
    times `1 - need_tail(room)`, where `(1 - e)^2` is the remaining-period arrival share times remaining horizon fraction
    and `(U + spend) / C` is the committed share. Any finite billed window with period time still to run stays strictly
    below `1.0` (so another pool's value ladder never collapses to zero when a substitute shares its top model), while
    vanishing quadratically near the reset (`e -> 1`). Reads only this window's own state."""
    cap = float(rec['capacity_usd'])
    used_after = float(rec['used_usd']) + float(spend_usd)
    room = cap - used_after
    if cap <= 0 or room <= 0:
        return 0.0
    e = float(rec.get('elapsed_frac') or 0.0)
    rem_time = max(0.0, 1.0 - e)
    committed = min(1.0, max(0.0, used_after / cap))
    p_rec = rec if 'price_lump_usd' not in rec else {**rec, 'lump_usd': rec['price_lump_usd']}
    return (1.0 - (rem_time ** 2) * committed) * (1.0 - five_axis_need_tail(p_rec, room))


def five_axis_outside_value(alternatives, own_best_fit):
    """V: the expected best fit still available elsewhere when this account's work is displaced. `alternatives` is a list
    of (fit, slack); they are tried by fit descending, each is open with its own slack probability and counts at most
    `own_best_fit`; when none has room the loss is total (0)."""
    value, none_open = 0.0, 1.0
    for fit, slack in sorted(alternatives, key=lambda t: (-t[0], -t[1])):
        value += none_open * slack * min(fit, own_best_fit)
        none_open *= (1.0 - slack)
    return value


def five_axis_upper_hull(points, outside_fit):
    """Upper concave envelope, ascending in spend, of (spend, fit) points above the outside option (0, outside_fit) up to
    the cheapest highest-fit point. Each edge is a step of the account's value ladder; [] when no point beats the outside."""
    best = [(s, f) for s, f in points if s > 0 and f > outside_fit + 1e-12]
    if not best:
        return []
    fmax = max(f for _, f in best)
    top = min(s for s, f in best if f >= fmax - 1e-9)
    chosen, running = [], outside_fit
    for s, f in sorted((p for p in best if p[0] <= top + 1e-12), key=lambda p: (p[0], -p[1])):
        if f > running + 1e-12:         # dominated points (dearer and no better) cannot be on the envelope
            chosen.append((s, f))
            running = f
    hull = [(0.0, outside_fit)]
    for s, f in chosen:
        while len(hull) >= 2 and ((hull[-1][1] - hull[-2][1]) * (s - hull[-2][0])
                                  <= (f - hull[-2][1]) * (hull[-1][0] - hull[-2][0])):
            hull.pop()
        hull.append((s, f))
    return hull


def five_axis_value_ladder(hulls):
    """The account's merged value ladder from one upper hull per demand point: {'steps': [(rho_j, sigma_j)], 'full_usd'}
    with sigma ascending. A step is one edge of a hull (giving up the dearer end saves its spend and costs its fit, a slope
    sigma = d fit / d USD for this task); rho_j is the share of the full-demand spend still needed once every step
    cheaper in slope than sigma_j has been given up. Equal slopes are one group, priced at the group's rho."""
    steps, full = [], 0.0
    for hull in hulls:
        if len(hull) < 2:
            continue
        full += hull[-1][0]
        for lo, hi in zip(hull, hull[1:]):
            steps.append(((hi[1] - lo[1]) / (hi[0] - lo[0]), hi[0] - lo[0]))
    ladder, given_up = [], 0.0
    steps.sort()
    i = 0
    while i < len(steps):
        slope, j, group = steps[i][0], i, 0.0
        while j < len(steps) and steps[j][0] - slope <= 1e-12 * max(1.0, abs(slope)):
            group += steps[j][1]
            j += 1
        ladder.append(((full - given_up) / full, slope))
        given_up += group
        i = j
    return {'steps': ladder, 'full_usd': full}


_CLASSIFIER_HORIZON_CACHE = {}


def classifier_cases_horizon_hours():
    """Longest calibrated task duration (hours) in `references/classifier-cases.json`, read via `public_data.effective`."""
    target = public_data.effective(REFERENCES / 'classifier-cases.json')
    try:
        stamp = target.stat().st_mtime_ns
    except OSError:
        return 0.0
    cached = _CLASSIFIER_HORIZON_CACHE.get(target)
    if cached and cached[0] == stamp:
        return cached[1]
    doc = json.loads(target.read_text(encoding='utf-8-sig'))
    hrs = [float(ep['expected_hours']) for c in (doc.get('calibration_cases') or ())
           for ep in [c.get('expected_profile') or {}] if isinstance(ep.get('expected_hours'), (int, float))]
    val = max(hrs, default=0.0)
    _CLASSIFIER_HORIZON_CACHE[target] = (stamp, val)
    return val


def five_axis_quota_price(rec, ladder, level_usd):
    """lambda_w(y): fit lost elsewhere per USD at cumulative use y, integrated over the account's value ladder via the
    negative-binomial depletion tail `need_tail((C - y) / rho_j)`. Rises strictly with used share `y / C` and vanishes
    at the reset (`e -> 1`), while remaining exact-unit invariant (`$ * k` scales `lambda` by `1/k`). When built by
    `FiveAxisEconomy`, `rec['price_lump_usd']` prices future arrivals at `max(task_hours, calibrated_horizon_hours)`
    cut at `reset_hours`, so short tasks at mid-week still reflect billed vs free, pool used share, and `+fast` burn."""
    cap = float(rec['capacity_usd'])
    e = float(rec.get('elapsed_frac') or 0.0)
    if cap <= 0 or e >= 1.0:
        return 0.0
    room = cap - float(level_usd)
    p_rec = rec if 'price_lump_usd' not in rec else {**rec, 'lump_usd': rec['price_lump_usd']}
    price, prev = 0.0, 0.0
    for rho, slope in ladder['steps']:
        tail = five_axis_need_tail(p_rec, room / rho)
        price += (slope - prev) * tail
        prev = slope
    return price


FIVE_AXIS_PRICE_NODES = 48     # nodes of the price table a window's candidates are integrated on (even)


def _quadratic_integral(lam, k, t):
    """Integral over [x_k, x_k + t * step], in units of `step`, of the quadratic through nodes c-1, c, c+1."""
    c = min(max(k, 1), len(lam) - 2)
    a = (lam[c + 1] - lam[c - 1]) / 2.0
    b = (lam[c + 1] - 2.0 * lam[c] + lam[c - 1]) / 2.0

    def prim(s):
        return lam[c] * s + a * s * s / 2.0 + b * s ** 3 / 3.0
    s0 = k - c
    return prim(s0 + t) - prim(s0)


def five_axis_price_table(rec, ladder, spend_max, nodes=FIVE_AXIS_PRICE_NODES):
    """lambda_w sampled at U + k * step, k = 0..nodes, with its cumulative integral (piecewise quadratic through three
    neighbouring nodes), so the cost of any spend up to `spend_max` is one lookup. None when there is nothing to price."""
    if spend_max <= 0 or not ladder['steps']:
        return None
    nodes += nodes % 2
    step = spend_max / nodes
    used = float(rec['used_usd'])
    lam = [five_axis_quota_price(rec, ladder, used + k * step) for k in range(nodes + 1)]
    cum = [0.0]
    for k in range(nodes):
        cum.append(cum[-1] + _quadratic_integral(lam, k, 1.0) * step)
    return {'start': used, 'step': step, 'nodes': nodes, 'lam': lam, 'cum': cum}


def five_axis_table_integral(table, spend_usd):
    """integral of lambda over [U, U + spend] from a `five_axis_price_table`; a spend past its range uses its last node."""
    if table is None or spend_usd <= 0:
        return 0.0
    x = min(spend_usd, table['step'] * table['nodes']) / table['step']
    k = min(int(x), table['nodes'] - 1)
    return table['cum'][k] + _quadratic_integral(table['lam'], k, x - k) * table['step']


def five_axis_spend_cost(rec, ladder, spend_usd, task_hours, spend_max=None, tables=None, fresh_lump=None,
                         fresh_price_lump=None):
    """cost_i = integral over [U, U + S] of lambda_w(y) dy: the fit the rest of the period loses because this candidate
    takes `spend_usd` of window `rec` (same unit as the fit, so N = F - cost needs no weight). A task that crosses the
    reset is split by time: the part before it is priced on the current period, the part after it on a fresh period
    (n = 0, e = 0). `spend_max` (default `spend_usd`) is the largest spend any candidate takes of this window; `tables`
    caches the price tables built for it ('now', 'fresh'). `fresh_lump` is the new period's untruncated burst size (the
    record's own `lump_usd` when not given). Returns {'cost', 'lambda_start', 'need_start', 'need_end',
    'spend_before', 'spend_after'}."""
    tables = {} if tables is None else tables
    spend = max(0.0, float(spend_usd))
    spend_max = max(spend, float(spend_max or 0.0))
    hours = max(1e-9, float(task_hours or 0.0))
    tau = rec.get('reset_hours')
    crossing = tau is not None and tau != float('inf') and float(tau) < hours - 1e-12
    before = spend * (float(tau) / hours) if crossing else spend
    after = spend - before
    if 'now' not in tables:
        tables['now'] = five_axis_price_table(rec, ladder, spend_max)
    table = tables['now']
    lam0 = table['lam'][0] if table else five_axis_quota_price(rec, ladder, rec['used_usd'])
    cost = five_axis_table_integral(table, before)
    need_start = five_axis_need_probability(rec, rec['used_usd'])
    need_end = five_axis_need_probability(rec, float(rec['used_usd']) + before)
    if crossing and after > 0:
        cap = float(rec['capacity_usd'])
        fresh = {'capacity_usd': cap, 'used_usd': 0.0, 'elapsed_frac': 0.0,
                 'lump_usd': rec['lump_usd'] if fresh_lump is None else fresh_lump,
                 **({'price_lump_usd': fresh_price_lump} if fresh_price_lump is not None else {})}
        if 'fresh' not in tables:
            tables['fresh'] = five_axis_price_table(fresh, ladder, spend_max)
        cost += five_axis_table_integral(tables['fresh'], after)
        need_end = five_axis_need_probability(fresh, min(after, cap))
    return {'cost': cost, 'lambda_start': lam0, 'need_start': need_start, 'need_end': need_end,
             'spend_before': before, 'spend_after': after}


# The demands a window's value ladder is read over: every mix of the three thinking categories (tool_operation 0, so a
# window's price does not move with per-turn speed or image reading, which belong to the current task alone).
FIVE_AXIS_DEMAND_GRID = tuple(
    {'divergence': d1, 'coding': d2, 'aesthetics': d3, 'tool_operation': 0}
    for d1 in range(4) for d2 in range(4) for d3 in range(4) if (d1 or d2 or d3)
)


class FiveAxisEconomy:
    """Quota economy v3 for one plan (advisor report v3, owner decisions D1-D3 of round 13).

    `pool` is the account's own candidates, evaluated with the current task's flags neutralised (`rank`'s economy pool), so
    a window's price does not move with local_only, vision, a named model or whether a candidate fits the
    remaining capacity now. The remaining capacity enters only through the window's own state (room, used, reset clock).

    Every long reset-period window w = (quota source, window id) is priced on its own state and clock: its value ladder
    comes from the candidates that draw it and, as the ladder's outside point only, from the startable candidates that do
    not (each with its own `five_axis_substitute_slack`, which reads that candidate's windows and no price). The price graph
    is therefore acyclic and one pass gives the unique answer. 5-hour windows (`short`) keep their feasibility role in
    `evaluate_bundle` and never enter a price, a tail or a ladder here."""

    def __init__(self, pool, accounts, policy, task, extra=()):
        self.grid = FIVE_AXIS_DEMAND_GRID
        targets = [five_axis_targets(d, accounts['_five_axis']['targets']) for d in self.grid]
        self.task_gi = len(self.grid)           # the extra fit slot: the task's own demand (diagnostics only)
        self._task_hours = float(task.get('hours') or task.get('expected_hours') or 0.0)
        self._horizon_hours = classifier_cases_horizon_hours()
        fit_cache = {}
        self.entries = []
        self.spend_max = {}
        self.units = {}
        self.unit_wkeys = {}
        self.points = {}
        self._tables, self._ladders, self._alts, self._recs, self._lumps, self._price_lumps = {}, {}, {}, {}, {}, {}
        for c in [*pool, *extra]:
            qsrc = provider_inventory.quota_source(c['bundle']['account'], accounts)
            states = {wid: (st, cap) for wid, (_, st, cap, _) in (c.get('_five_window_states') or {}).items()
                      if not st.get('short')}
            if not states:
                continue        # no long-window state: nothing to ladder and never an outside point (a data error upstream)
            values = c.get('_five_values') or {}
            key = json.dumps(values, sort_keys=True)
            if key not in fit_cache:
                fit_cache[key] = tuple(
                    five_axis_fit(values, d, t)[0] if all(values.get(a) is not None for a, w in d.items() if w > 0) else -1.0
                    for d, t in zip(self.grid, targets)
                )
            entry = {'cand': c, 'qsrc': qsrc, 'startable': not c.get('_five_long_wait'),
                     'unmetered': bool(c.get('_five_unmetered')), 'metered': bool(c.get('_five_metered')),
                     'spend': float(c.get('expected_usd') or 0.0), 'states': states,
                     'hours': float(c.get('_five_econ_hours') or c.get('expected_hours') or 0.0),
                     'fits': fit_cache[key] + (float(c['components'].get('fit') or 0.0),),
                     'unit': (qsrc, frozenset(states))}
            self.entries.append(entry)
            fast = c.get('_five_fast_candidate')
            peak = max(entry['spend'], float(fast['step_usd']) if fast else 0.0)
            for wid in states:
                self.spend_max[(qsrc, wid)] = max(self.spend_max.get((qsrc, wid), 0.0), peak)
        for e in self.entries:
            if e['startable'] and not e['unmetered'] and not e['metered']:
                self.units.setdefault(e['unit'], []).append(e)
                self.unit_wkeys[e['unit']] = frozenset((e['qsrc'], wid) for wid in e['states'])
        drawn = {}
        for e in self.entries:
            for wid in e['states']:
                drawn.setdefault((e['qsrc'], wid), []).append(e)
        for wkey, rows in drawn.items():        # a window nobody can start on now is laddered by its own waiting candidates
            self.points[wkey] = [e for e in rows if e['startable']] or rows

    def rec(self, entry, wid):
        st, cap = entry['states'][wid]
        return self.record((entry['qsrc'], wid), st, cap)

    def record(self, wkey, st, cap):
        """The window's recovery record with its burst size `lump_usd` attached (None when the window is unreadable)."""
        rec = five_axis_window_recovery(st, cap)
        if rec is not None:
            rec['lump_usd'] = self.lump(wkey, rec['reset_hours'])
            rec['price_lump_usd'] = self.price_lump(wkey, rec['reset_hours'])
        return rec

    def lump(self, wkey, reset_hours=None):
        """q_w, the window's burst size (advisor v3.1, option A, owner round 15): the average over the demand grid of what one
        task like the current one spends of this window on the account's own best candidate for that demand (the cheapest
        of the highest fit), cut at the window's own reset: a task longer than the hours to the reset spends only the
        share `reset_hours / hours` of its dollars before it. `reset_hours` None means the untruncated size (a fresh
        period). Capped by the window's capacity. It reads only this account's candidates, their public `usd_per_hour` and
        durations, and the weekly clock: no 5-hour window, no outside point, no constant."""
        tau = float('inf') if reset_hours is None else float(reset_hours)
        key = (wkey, tau)
        got = self._lumps.get(key)
        if got is None:
            own = [e for e in (self.points.get(wkey) or []) if e['spend'] > 0]
            total = 0.0
            for gi in range(len(self.grid)):
                if own:
                    top = max(own, key=lambda e: (round(e['fits'][gi], 9), -e['spend']))
                    share = 1.0 if top['hours'] <= 0 or tau == float('inf') else min(1.0, tau / top['hours'])
                    total += top['spend'] * share
            cap = max((float(e['states'][wkey[1]][1]) for e in own), default=0.0)
            got = self._lumps[key] = min(cap, total / len(self.grid)) if own else 0.0
        return got

    def price_lump(self, wkey, reset_hours=None):
        """Burst size used in `five_axis_quota_price`: `max(lump_usd, horizon_lump_usd)` where `horizon_lump_usd` prices
        future arrivals at `max(task_hours, classifier_cases_horizon_hours())`, cut at `reset_hours`."""
        base_q = self.lump(wkey, reset_hours)
        if self._task_hours <= 0 or self._horizon_hours <= self._task_hours:
            return base_q
        tau = float('inf') if reset_hours is None else float(reset_hours)
        key = (wkey, tau)
        got = self._price_lumps.get(key)
        if got is None:
            own = [e for e in (self.points.get(wkey) or []) if e['spend'] > 0 and e['hours'] > 0]
            if not own:
                got = base_q
            else:
                scale = self._horizon_hours / self._task_hours
                total = 0.0
                for gi in range(len(self.grid)):
                    top = max(own, key=lambda e: (round(e['fits'][gi], 9), -e['spend']))
                    h_ref = top['hours'] * scale
                    share = 1.0 if h_ref <= 0 or tau == float('inf') else min(1.0, tau / h_ref)
                    total += (top['spend'] * scale) * share
                cap = max((float(e['states'][wkey[1]][1]) for e in own), default=0.0)
                got = max(base_q, min(cap, total / len(self.grid)))
            self._price_lumps[key] = got
        return got

    def _alt(self, unit, gi):
        """(fit, slack) of the best candidate of an outside unit at demand point gi (ties: cheapest)."""
        got = self._alts.get((unit, gi))
        if got is None:
            rows = self.units[unit]
            best = max(rows, key=lambda e: (round(e['fits'][gi], 9), -e['spend']))
            slack = 1.0
            for wid in best['states']:
                rec = self.rec(best, wid)
                if rec is not None:
                    slack = min(slack, five_axis_substitute_slack(rec, best['spend']))
            got = self._alts[(unit, gi)] = (best['fits'][gi], slack)
        return got

    def _outside(self, wkey, gi):
        return [self._alt(u, gi) for u in self.units if wkey not in self.unit_wkeys[u]]

    def ladder(self, wkey):
        """The account's value ladder for window `wkey`, averaged over the demand grid (owner D1)."""
        got = self._ladders.get(wkey)
        if got is None:
            own = self.points.get(wkey) or []
            hulls = []
            for gi in range(len(self.grid)):
                pts = [(e['spend'], e['fits'][gi]) for e in own if e['spend'] > 0]
                if pts:
                    outside = five_axis_outside_value([(f, a) for f, a in self._outside(wkey, gi) if f > 0],
                                                      max(f for _, f in pts))
                    hulls.append(five_axis_upper_hull(pts, outside))
            got = self._ladders[wkey] = five_axis_value_ladder(hulls)
        return got

    def outside_at_task(self, wkey):
        """(V, own best fit, P(some outside candidate has room)) at the task's own demand, for the report."""
        own = [e for e in (self.points.get(wkey) or []) if e['spend'] > 0]
        alts = [(f, a) for f, a in self._outside(wkey, self.task_gi) if f > 0]
        best = max((e['fits'][self.task_gi] for e in own), default=0.0)
        none_open = 1.0
        for _, a in alts:
            none_open *= (1.0 - a)
        return five_axis_outside_value(alts, best), best, 1.0 - none_open

    def window_cost(self, wkey, rec, spend, econ_hours):
        tables = self._tables.setdefault((wkey, round(rec['used_usd'], 6), round(rec['elapsed_frac'], 9),
                                          round(rec['capacity_usd'], 6), round(rec['lump_usd'], 6),
                                          round(rec.get('price_lump_usd', rec['lump_usd']), 6)), {})
        return five_axis_spend_cost(rec, self.ladder(wkey), spend, econ_hours, self.spend_max.get(wkey, spend), tables,
                                    fresh_lump=self.lump(wkey), fresh_price_lump=self.price_lump(wkey))

    def report(self):
        """One block per priced window for the plan's `system.quota_windows` (and the README page): its state and its
        price now. Only windows some eligible candidate draws appear."""
        out = []
        for wkey in sorted(self.points):
            e = self.points[wkey][0]
            rec = self.rec(e, wkey[1])
            if rec is None:
                continue
            ladder = self.ladder(wkey)
            outside, best, slack = self.outside_at_task(wkey)
            period = rec['period_hours']
            out.append({
                'label': f'{wkey[0]}/{wkey[1]}', 'quota_source': wkey[0], 'window': wkey[1],
                'capacity_usd': round(rec['capacity_usd'], 2), 'used_usd': round(rec['used_usd'], 2),
                'recovery_line_usd': round(rec['line_now_usd'], 2),
                'period_hours': None if period is None else round(period, 2),
                'elapsed_hours': None if period is None else round(period - rec['reset_hours'], 2),
                'reset_in_hours': None if rec['reset_hours'] == float('inf') else round(rec['reset_hours'], 2),
                'need_probability': round(five_axis_need_probability(rec, rec['used_usd']), 4),
                'lump_usd': round(rec['lump_usd'], 2),
                'quota_price': round(five_axis_quota_price(rec, ladder, rec['used_usd']), 6) if ladder['steps'] else 0.0,
                'ladder_steps': len(ladder['steps']), 'outside_fit': round(outside, 4), 'best_fit': round(best, 4),
                'substitute_slack': round(slack, 4), 'candidates': len(self.points[wkey]),
            })
        return out


def five_axis_order(candidate):
    """Lexicographic order on the five-axis path (quota economy v3, owner decision of round 13b + F3), in the order of the tuple:
    1. Net score N = F - cost descending (exact float, never rounded: the quota economy C is a term of N, never a tie-break)
    2. Wait before the start ascending (round 10: a candidate that waits for a reset trails one that starts now; the design
       has no such key, it keeps `--can-wait-hours` candidates behind immediate ones at equal N)
    3. Earliest-expiring quota ascending: breaks ONLY exact ties on N after C is fully in N
    4. Faster completion time (wait_hours + expected_hours) ascending; speed only breaks exact ties on N
    5. Fewer list-price USD spent ascending
    6. Demand-weighted category level descending (five_axis_level), then sibling harness place ascending."""
    k = candidate['components']
    reset = k.get('earliest_reset_hours')
    reset_key = float('inf') if reset is None else round(float(reset), 4)
    wait_h = float(candidate.get('available_in_hours') or 0.0)
    done_key = round(wait_h + float(candidate['duration_hours']), 4)
    usd = float(candidate.get('expected_usd') if candidate.get('expected_usd') is not None else float('inf'))
    return (-float(candidate['score']), round(wait_h, 4), reset_key, done_key,
            round(usd, 4), -round(float(k.get('five_axis_level', 0.0)), 4), k.get('harness_place', 0))


FIVE_AXIS_ORDER_KEYS = ('net_score', 'wait_hours', 'earliest_reset_hours', 'completion_hours', 'usd', 'axis_level',
                        'harness_place')


def five_axis_deciding_key(first, second):
    """The name of the first key of `five_axis_order` on which `first` beats `second` ('tie' when none does)."""
    for name, a, b in zip(FIVE_AXIS_ORDER_KEYS, five_axis_order(first), five_axis_order(second)):
        if a != b:
            return name
    return 'tie'


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


def cycle_observation(window, observed, usage_row, now, read_failed_ok=False):
    """The window's reading, or None when a reading that is not current cannot describe the current cycle.

    Usage only grows inside a cycle, so an old reading still bounds what is left until its reset. No reading
    describes the cycle after its own reset has passed. A reading that is not current cannot either when the
    account was unavailable, the reading has no time or is from the future, or it was taken more than the
    window's `hours` before its reset (an earlier cycle). A provider rate-limit observation is always kept.
    `read_failed_ok` (the five-axis path, owner decision of round 13b): an account whose latest read failed still has its
    last successful reading of this cycle, which is current-period data and is kept, labelled by the caller with its read
    time. A failing usage endpoint (even a 429 from it) is a read failure, not a quota signal; quota failures arrive as
    provider holds (`provider_retry`, `model_latch`), which evaluate_bundle applies before any reading is consulted.
    """
    if observed is None or observed.get('rate_limited') is True:
        return observed
    if observed.get('reset_time_passed') is True:
        return None
    if usage_row.get('status') in (None, 'fresh'):
        return observed     # current, or a caller that does not classify freshness (route_judge.usage_snapshot does)
    fetched = parse_time(usage_row.get('fetched_at'))
    if ((usage_row.get('available') is False and not read_failed_ok) or fetched is None
            or (fetched - now).total_seconds() > 60):
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
    cap = capacity_usd(spec, wid)
    small = cap is not None and cap < policy.get('small_short_capacity_usd', 50)
    return (small,
            policy.get('small_short_window_floor_pct', 25) if small else policy.get('short_window_floor_pct', 10),
            policy.get('small_short_safety_margin_pct', 12) if small else policy.get('safety_margin_pct', 5))


def reserve_inflight(account, spec, wid, wspec, state, records, accounts, policy, now):
    """Apply active in-flight reservation deduction to a known window state; returns (state, reserved_usd, reserved_pct)."""
    if not state or not state.get('known') or not records:
        return state, 0.0, 0.0
    win_cap_usd = capacity_usd(spec, wid)
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

    The capacities a window is priced on are the public data's, as in `plan`: `accounts` should be `five_axis_overlay`
    output, which a caller probing many bundles builds once and passes down (raw accounts are overlaid here, per call).
    """
    accounts = accounts if isinstance(accounts, dict) else {}
    if '_five_axis' not in accounts:
        accounts = five_axis_overlay(accounts)
    spec = (accounts.get('accounts') or {}).get(bundle.get('account'))
    if not isinstance(spec, dict):
        # An account the configuration does not describe still has its readings: every window binds.
        spec = {}
        accounts = {**accounts, 'accounts': {**(accounts.get('accounts') or {}), bundle['account']: spec}}
    if spec.get('unmetered') or spec.get('five_axis_free_pool'):
        return []       # a purpose-limited channel or a free starter allowance never blocks a launch
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
            if state['known'] and state['remaining'] <= margin and unit_rate(spec, wid, model, model_spec(accounts, model)):
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


def window_is_short(wspec, policy):
    """True for a 5-hour-class window (a reset period shorter than `short_window_hours`): it decides whether work can start
    now and never enters the five-axis economy."""
    hours = (wspec or {}).get('hours')
    return hours is not None and hours != float('inf') and hours < policy['short_window_hours']


def window_state(wid, spec, observed, account_spec, now, short_hours, account_stale=False, expected_reset_at=None):
    """Remaining capacity, time to the end of the allowance and how sure we are."""
    hours = spec.get('hours')
    starter = hours is None or hours == float('inf')
    expires = parse_time(account_spec.get('expires_at'))
    row = {'id': wid, 'hours': hours, 'short': (hours is not None and hours != float('inf') and hours < short_hours),
           'known': False, 'stale': bool(account_stale)}
    if starter:
        cap = capacity_usd(account_spec, wid)
        if cap is None or cap <= 0:
            return row
        full = 100.0 * pool_size(observed)
        row['full'] = full
        remaining = (observed or {}).get('remaining_pct_before_reset')
        if remaining is None:
            remaining = (observed or {}).get('observed_remaining_pct')
        if remaining is None:
            used_usd = (observed or {}).get('used_usd')
            if used_usd is None:
                used_usd = account_spec.get('local_spend_usd', 0.0)
            remaining = max(0.0, full - 100.0 * float(used_usd or 0.0) / cap)
        expiry_hours = (expires - now).total_seconds() / 3600 if expires is not None and expires > now else None
        end_hours = expiry_hours if expiry_hours is not None else float('inf')
        row.update({'known': True, 'remaining': remaining, 'reset_hours': float('inf'), 'end_hours': end_hours,
                    'expiry_hours': expiry_hours, 'target_remaining': 0.0, 'reset_at': None})
        return row
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
        row['unstarted'] = True
    elif (row['short'] or account_family(account_spec) == 'antigravity' or spec.get('starts_on_first_use')) and remaining == 100:
        row['unstarted'] = True
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
    policy, accounts = ctx['policy'], ctx['accounts']
    spec = accounts['accounts'][bundle['account']]
    # Tables read by the name's own resolution. `model` is its key, for the matching that follows; `own_model` is which
    # model the row runs, for what the user named.
    model = canonical(bundle['model'], accounts)
    own_model = row_key(bundle['model'], accounts)
    info = model_spec(accounts, bundle['model'])
    kind = task['kind']
    base_hours = task['hours']
    base_chain_hours = task['chain_hours']
    result = {'id': bundle['id'], 'bundle': {k: bundle.get(k) for k in ('provider', 'harness', 'account', 'model', 'effort', 'modeId', 'launch_model', 'draws', 'pin_carrier')
                                              if bundle.get(k) is not None},
              'excluded': [], 'notes': [], 'components': {}, 'score': None}
    five = ctx['five_axis']
    now = ctx['now']

    # Economy pool mode (round 16, owner policy: a window's price must not depend on the current task's flags): the pool is
    # what an account can run at all, evaluated with the task's flags neutralised (`rank` builds that task). It keeps the
    # account-level exclusions (host policy, checkbox, discovery, holds, unverified, data errors, weekly window) and drops
    # the ones that belong to the task: local-only, outbound, file transfer, image input, a named constraint, the demanded
    # categories, whether the remaining capacity fits THIS task, and the 5-hour windows.
    pool = bool(task.get('_economy_pool'))

    def exclude(reason):
        result['excluded'].append(reason)

    def task_exclude(reason):
        """An exclusion that belongs to the current task, not to the account: the ranking keeps it, the pool does not."""
        if not pool:
            result['excluded'].append(reason)

    def short_exclude(reason):
        """An exclusion by a 5-hour window alone: the work cannot start now, but the account still has its long windows, so
        the economy (which prices only long windows) keeps this candidate as a substitute and in its own ladder."""
        if pool:
            return
        result['excluded'].append(reason)
        result.setdefault('_five_short_reasons', []).append(reason)

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
    unoffered = live_models.reason(bundle, ctx.get('live_models'), bundle['account'], spec)
    if unoffered:
        exclude(unoffered)
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
    account_out = bool(result['excluded'])      # out by the host, the checkbox, a hold or billing: not by this task
    if info.get('excluded_when') == 'local_only' and task['local_only']:
        task_exclude("the parent marked this work local-only (it must run on this machine: in-place edits, local tools, secrets or live accounts); this worker runs remotely")
    if info.get('excluded_when') == 'local_only' and task.get('outbound_ok') is False:
        task_exclude('the parent disallowed outbound work; this worker runs remotely')
    if info.get('text_only') and task.get('file_transfer'):
        task_exclude('this text-only runner cannot transfer or apply local files')
    # Harness input limits apply to every bundle on the harness, manual or auto (HARNESS_CAPABILITIES).
    harness = bundle.get('harness') or bundle_harness(bundle, bundle.get('account'), bundle.get('id'))
    if task['needs_visual'] and not harness_accepts_visual(harness, model):
        task_exclude(f'{harness} cannot accept image attachments for {model}, so it cannot take visual or GUI work')
    if task['constraint_model'] and not model_pin_matches(task, bundle, own_model):
        task_exclude('explicit model constraint')
    if task.get('constraint_effort') and str(scoring_effort(bundle, info) or '').lower() != task['constraint_effort']:
        task_exclude('explicit effort constraint')
    # Rule B: a demanded category with no data at all, no cited usd on a metered account, or unknown image_input modality
    # when the task demands image input (tool_operation 3), leaves automatic routing; naming the model (--model, or its
    # bundle) still dispatches it, with the data error as a warning and its cost unpriced.
    state = info.get('five_axis') or {'missing': list(FIVE_AXES), 'usd_status': 'missing'}
    billed = not (spec.get('unmetered') or spec.get('unlimited'))
    named = not pool and (model_pin_matches(task, bundle, own_model) or task.get('pinned_bundle') == bundle['id'])
    img_info = state.get('image_input') or {'status': 'missing'}
    needs_image_input = task['five_axis_demand'].get('tool_operation', 0) >= IMAGE_INPUT_LEVEL and not pool
    eff = scoring_effort(bundle, info)
    values = five_axis_values(state, eff)
    if needs_image_input and img_info.get('status') == 'no':
        exclude(f"model does not accept image input ({img_info.get('source') or 'public data'})")

    def _missing_targets(axis, level):
        if level < five['demanded_level']:
            return []
        if axis == 'tool_operation':
            selected = TOOL_LEVEL_FACETS.get(level) or ()
            if not selected:
                return []
            return [f'tool_operation.{f}' for f in selected if (values.get('tool_operation') or {}).get(f) is None]
        if values.get(axis) is not None:
            return []
        cat_cell = (state.get('categories') or {}).get(axis) or {}
        facets = cat_cell.get('facets') if isinstance(cat_cell, dict) else None
        req = user_scores.CATEGORY_FACETS.get(axis) or ()
        if isinstance(facets, dict) and len(req) > 1:
            miss_f = [f'{axis}.{f}' for f in req if _at_effort(facets.get(f), eff) is None]
            if miss_f and len(miss_f) < len(req):
                return miss_f
        return [axis]

    blind, missing_targets = [], []
    if not pool:
        for a, w in task['five_axis_demand'].items():
            miss = _missing_targets(a, w)
            if miss:
                blind.append(a)
                missing_targets.extend(miss)
    if blind:
        first_target = missing_targets[0]
        eff_note = f' at effort {eff}' if eff and any(
            ((state.get('categories') or {}).get(a) or {}).get('value') is not None for a in blind
        ) else ''
        blind_msg = (
            f'no trusted data for the demanded category {", ".join(blind)} on {model}{eff_note} '
            f'(missing {", ".join(missing_targets)}; fill with `agentcompass score set {model} {first_target} <1..3>` '
            f'or in the tray Manual Scores panel)'
        )
    else:
        blind_msg = None
    errors = (
        ([blind_msg] if blind_msg else [])
        + (['usd_per_hour has no cited source on a metered account'] if billed and state.get('usd_status') != 'ok' else [])
        + (['image_input modality has no cited source'] if needs_image_input and img_info.get('status') not in ('yes', 'no') else [])
    )
    for error in errors:
        if named:
            unpriced = ' (its cost is not priced)' if error.startswith('usd_per_hour') else ''
            result['notes'].append(f'data error: {error}; dispatched because it was named{unpriced}')
            result.setdefault('data_errors', []).append(error)
        else:
            exclude(f'data error: {error} (out of automatic routing; name the model to dispatch it)')
    manual_applied = state.get('manual_scores') or {}
    if manual_applied:
        summary_parts = [f"{t}={r['level']:g} (z={r['z']:+.4f}, {r['kind']}, {r['as_of']})"
                         for t, r in sorted(manual_applied.items())]
        result['notes'].append('manual capability scores applied: ' + '; '.join(summary_parts))
        result['manual_scores'] = dict(manual_applied)
    estimated = state.get('estimates') or {}
    if estimated:
        result['notes'].append('facets estimated (same-series sibling or the fit on the tier; no board of their own): '
                               + ', '.join(sorted(estimated)))
        result['estimated_axes'] = sorted({name.split('.')[0] for name in estimated})
    # One basis for the time a task takes, its spend and the window it needs (`duration_at`): Time/Task at this effort. Where
    # the data has none, the candidate leaves automatic routing; a named dispatch carries the task's own hours estimate
    # unscaled and the data error, and nothing stands in for the model's pace.
    dur_mult, dur_basis = duration_at(info, eff)
    if dur_mult is None:
        error = (f'no Time/Task for {model} at {eff or "its single configuration"} (Artificial Analysis, arena.ai Agent on '
                 f'its scale, or a same-series effort conversion)')
        if named:
            result['notes'].append(f'data error: {error}; dispatched because it was named, hours are the task\'s own estimate')
            result.setdefault('data_errors', []).append(error)
        else:
            exclude(f'data error: {error} (out of automatic routing at this effort; name the model to dispatch it)')
    hours = base_hours if dur_mult is None else base_hours * dur_mult
    chain_hours = base_chain_hours if dur_mult is None else base_chain_hours * dur_mult
    result['components']['duration_source'] = (dur_basis or {}).get('method') or 'unknown'
    if dur_mult is not None:
        result['notes'].append(f'time at {eff or "its single configuration"}: {hours:.2f}h from '
                               f'{(dur_basis or {}).get("method")} ({(dur_basis or {}).get("source") or "series"})')
    expires = parse_time(spec.get('expires_at'))
    if expires is not None and (expires - now).total_seconds() < hours * 3600:
        task_exclude('subscription ends before the work would finish')

    worker = task['worker']
    resumes = (
        bool(worker)
        and worker.get('bundle') == bundle['id']
        and not (task.get('read_only_review') and worker.get('read_only_review') is False)
    )
    worn = worker_worn(worker, policy)
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
    context_burn = 1 + context_extra
    # `usd_per_hour` is the model's dollars per hour of its own work (a rate), so the spend is usd/h x the hours this
    # candidate takes at its effort, and the window's quota rate carries no second effort factor.
    econ_hours, econ_chain_hours = hours, chain_hours
    step_usd = bundle_task_usd(spec, model, econ_hours, info, context_burn)
    chain_usd = bundle_task_usd(spec, model, econ_chain_hours, info, context_burn)
    available_at = None
    five_window_states = {}
    long_applicable = False         # the bundle draws at least one long reset-period window (read or not)
    if spec.get('unlimited'):
        result['notes'].append('unlimited usage')
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
    # Purpose-limited unmetered channels (Dot, ChatGPT Web) have no quota to relieve: never another window's substitute.
    result['_five_unmetered'] = bool(spec.get('unmetered') or spec.get('unlimited'))
    # A metered (API) account is a last resort: it has a ladder of its own but is never another window's substitute.
    result['_five_metered'] = billing == 'metered'
    if not spec.get('unmetered'):
        acct_usage = ctx['usage'].get('accounts', {}).get(bundle['account']) or {}
        acct_stale = acct_usage.get('status') == 'stale' or acct_usage.get('stale') is True
        latched = latched_windows(accounts, bundle['account'])
        named = not pool and (model_pin_matches(task, bundle, own_model) or task.get('pinned_bundle') == bundle['id'])
        for wid, wspec, observed in applicable_windows(spec, model, acct_usage, bundle, accounts):
            result.setdefault('_five_axis_windows', []).append(wid)
            long_applicable = long_applicable or not window_is_short(wspec, policy)
            observed = cycle_observation(wspec, observed, acct_usage, now, read_failed_ok=True)
            refusal = window_refusal(wid, wspec, observed, acct_usage, now, latched)
            if refusal:
                (short_exclude if window_is_short(wspec, policy) else exclude)(refusal)
            expected = window_reset_expectation(ctx.get('overrides', ()), bundle['account'], model, wid, spec, accounts, now)
            state = window_state(wid, wspec, observed, spec, now, policy['short_window_hours'],
                                 account_stale=acct_stale, expected_reset_at=expected)
            unit = unit_rate(spec, wid, model, info)
            cap_usd_w = capacity_usd(spec, wid)
            if cap_usd_w is None:
                if state['known'] and state['remaining'] <= policy['safety_margin_pct']:
                    exclude(f'{wid} has {state["remaining"]:.0f}% left, inside the {policy["safety_margin_pct"]}% reserve '
                            '(capacity unknown, so the need cannot be priced)')
                    result['wait_until_hours'] = max(result.get('wait_until_hours', 0), state['reset_hours'])
                    continue
                err = f'{wid} capacity_unknown (quota unknown, spend unpriced)'
                if named:
                    local_u = spec.get('local_spend_usd')
                    local_note = f'; local opencode.db lower-bound spend ~${local_u:.2f}' if isinstance(local_u, (int, float)) and local_u > 0 else ''
                    result['notes'].append(f'data error: {err}; dispatched because it was named{local_note}')
                    result.setdefault('data_errors', []).append(err)
                else:
                    exclude(f'data error: {err} (out of automatic routing; name the model to dispatch it)')
                continue
            if not state['known']:
                if state['short']:
                    w_len = float(wspec.get('hours') or 0) or econ_hours
                    step = None if unit is None else min(econ_hours, w_len) * unit * context_burn
                    margin_pct = short_window_limits(spec, wid, policy, now)[2]
                    if step is not None and 100.0 - step < margin_pct:
                        short_exclude(f'{wid} full capacity (100%) is short of the ~{step:.1f}% the work needs plus {margin_pct}% margin')
                    elif (observed and (reset_dt := parse_time(observed.get('resets_at'))) is not None
                          and reset_dt > now and observed.get('reset_time_passed') is not True):
                        wait = (reset_dt - now).total_seconds() / 3600
                        short_exclude(f'{wid} has an active reset in {wait:.1f}h with unknown remaining percentage')
                continue
            state, reserved_usd, reserved_pct = reserve_inflight(
                bundle['account'], spec, wid, wspec, state, ctx.get('records'), accounts, policy, now)
            if reserved_usd > 0:
                result['notes'].append(f'{wid}: -${reserved_usd:.2f} (-{reserved_pct:.1f}%) reserved by in-flight {bundle["account"]} dispatch')
            if state['short']:
                w_len = float(wspec.get('hours') or 0) or econ_hours
                tau = state.get('reset_hours', w_len)
                h_before = min(econ_hours, w_len) if (tau is None or tau == float('inf') or state.get('unstarted')) else min(econ_hours, max(0.0, float(tau)))
                step = None if unit is None else h_before * unit * context_burn
                step_period = None if unit is None else min(max(0.0, econ_hours - h_before), w_len) * unit * context_burn
                small_short, floor_pct, margin_pct = short_window_limits(spec, wid, policy, now)
                empty = state['remaining'] <= floor_pct
                if empty or (step is not None and state['remaining'] - step < margin_pct):
                    wait = state['reset_hours']
                    what = 'is empty' if empty else f'has {state["remaining"]:.0f}%, short of the ~{step:.1f}% the work needs,'
                    post_period = None if unit is None else min(econ_hours, w_len) * unit * context_burn
                    if post_period is not None and 100.0 - post_period < margin_pct:
                        short_exclude(f'{wid} full capacity (100%) is short of the ~{post_period:.1f}% the work needs plus {margin_pct}% margin')
                    elif (not small_short and not state['stale'] and 0 < wait <= policy['defer_if_reset_within_hours']
                          and wait >= state.get('natural_reset_hours', wait)):
                        available_at = max(available_at or 0, wait)
                        result['notes'].append(f'{wid} {what} for {wait:.1f}h; start after its reset')
                        renewed = after_reset(state, w_len)
                        if renewed is not None:
                            renewed['unstarted'] = True
                            five_window_states[wid] = (wspec, renewed, cap_usd_w, unit)
                    else:
                        short_exclude(f'{wid} {what} for {wait:.1f}h')
                    continue
                if step_period is not None and 100.0 - step_period < margin_pct:
                    short_exclude(f'{wid} full capacity (100%) is short of the ~{step_period:.1f}% the work needs plus {margin_pct}% margin')
                    continue
                five_window_states[wid] = (wspec, state, cap_usd_w, unit)
                continue
            if unit is None:
                continue
            w_len = wspec.get('hours')
            tau = state.get('reset_hours')
            req_hours = econ_chain_hours if task['coupling'] == 'high' else econ_hours
            h_before = req_hours if (tau is None or tau == float('inf') or w_len is None) else min(req_hours, max(0.0, float(tau)))
            need = h_before * unit * context_burn
            if (not pool and state['remaining'] - need < policy['safety_margin_pct'] and w_len is not None
                    and state['reset_hours'] <= policy['defer_if_reset_within_hours']):
                renewed = after_reset(state, w_len)
                if renewed is not None and renewed['expiry_hours'] is not None and renewed['expiry_hours'] < hours:
                    renewed = None
                if renewed is not None and 100.0 - min(req_hours, float(w_len)) * unit * context_burn >= policy['safety_margin_pct']:
                    state = renewed
                    available_at = max(available_at or 0, renewed['deferred_hours'])
                    result['_five_long_wait'] = True        # a long window must renew first: no absorber of other work now
                    need = min(req_hours, float(w_len)) * unit * context_burn
                    result['notes'].append(f'{wid} renews in {renewed["deferred_hours"]:.1f}h; start then (assumes a full renewal)')
            if state['remaining'] - need < policy['safety_margin_pct']:
                if state['reset_hours'] == float('inf'):
                    task_exclude(f'{wid} has {state["remaining"]:.0f}% left; the work needs ~{need:.1f}% plus a {policy["safety_margin_pct"]}% margin '
                                 '(starter allowance does not reset)')
                else:
                    task_exclude(f'{wid} has {state["remaining"]:.0f}% left; the work needs ~{need:.1f}% plus a {policy["safety_margin_pct"]}% margin '
                                 f'and it resets in {state["reset_hours"] / 24:.1f} days')
                    result['wait_until_hours'] = max(result.get('wait_until_hours', 0), state['reset_hours'])
            elif w_len is not None and req_hours > h_before:
                rem_need = min(req_hours - h_before, float(w_len)) * unit * context_burn
                if 100.0 - rem_need < policy['safety_margin_pct']:
                    task_exclude(f'{wid} full capacity (100%) is short of the ~{rem_need:.1f}% the work needs per period plus {policy["safety_margin_pct"]}% margin')
            five_window_states[wid] = (wspec, state, cap_usd_w, unit)
            if state['stale'] and 'capacity from a stale observation' not in result['notes']:
                result['notes'].append('capacity from a stale observation')
            if acct_usage.get('status') != 'fresh' and acct_usage.get('fetched_at') and not result['components'].get('reading_at'):
                # Priced from the last successful reading of this reset period (owner decision of round 13b): labelled
                # with its read time so plan, roll and the tray say how old the quota figure is.
                result['components']['reading_at'] = acct_usage['fetched_at']
                result['notes'].append(f"{wid}: priced from the reading taken at {acct_usage['fetched_at']} "
                                       f"(the latest read did not succeed; it is the last good reading of this reset period)")
        if spec.get('capacity_unknown') and not result.get('_five_axis_windows'):
            err = 'capacity_unknown (quota unknown, spend unpriced)'
            if named:
                local_u = spec.get('local_spend_usd')
                local_note = f'; local opencode.db lower-bound spend ~${local_u:.2f}' if isinstance(local_u, (int, float)) and local_u > 0 else ''
                result['notes'].append(f'data error: {err}; dispatched because it was named{local_note}')
                result.setdefault('data_errors', []).append(err)
            else:
                exclude(f'data error: {err} (out of automatic routing; name the model to dispatch it)')
        if (not spec.get('unlimited') and not account_out and not result.get('data_errors')
                and not any(not st.get('short') for _, st, _, _ in five_window_states.values())):
            # Priced on the weekly window or not at all (round 15): a candidate with no long reset-period window in its
            # state cannot be priced and cannot stand in for another account's work, so it leaves automatic routing as
            # a surfaced data error. Three causes, three messages: nothing of the account can be read (round 13); the
            # weekly window is declared but not read while the 5-hour window is; no weekly window exists at all.
            if not result.get('_five_axis_windows') or not long_applicable:
                err = QUOTA_NO_LONG_WINDOW if not long_applicable else QUOTA_READING_UNAVAILABLE
            elif five_window_states:
                err = QUOTA_LONG_UNREAD
            else:
                err = QUOTA_READING_UNAVAILABLE
            if named:
                result['notes'].append(f'data error: {err}; dispatched because it was named')
                result.setdefault('data_errors', []).append(err)
            else:
                exclude(f'data error: {err} (out of automatic routing; name the model to dispatch it)')
        result['_five_window_states'] = five_window_states
        result['_five_econ_hours'] = econ_hours
        result['_five_chain_hours'] = econ_chain_hours
        result['_five_context_burn'] = context_burn
        if not five_window_states and not result['excluded'] and not result.get('data_errors'):
            result['notes'].append('no current capacity observation; unknown is not exhausted')
    if (pool or (not task.get('disable_fast') and available_at is None)) and not result['excluded'] and not result.get('_five_long_wait'):
        fast_info = ((info.get('five_axis') or {}).get('fast_mode')) or {}
        if fast_info.get('status') == 'ok' and fast_modes.supported(harness, model, info):
            f_price = float(fast_info.get('price_multiplier') or 2.0)
            f_speed = float(fast_info.get('speed_multiplier') or 1.0)
            f_econ_h = econ_hours / f_speed if f_speed > 0 else econ_hours
            f_chain_h = econ_chain_hours / f_speed if f_speed > 0 else econ_chain_hours
            f_hours = hours / f_speed if f_speed > 0 else hours
            f_step_usd = step_usd * f_price
            f_chain_usd = chain_usd * f_price
            fast_ok = bool(five_window_states)
            for wid, (wspec, st, cap_usd_w, unit) in five_window_states.items():
                if pool and st.get('short'):
                    continue
                if unit is None:
                    fast_ok = False
                    break
                if pool:
                    continue        # the pool asks what the account can run, not whether it fits the remaining capacity now
                f_unit = unit * f_price * f_speed * context_burn     # per fast hour; x f_econ_h is price x the normal spend
                w_len = wspec.get('hours')
                tau = st.get('reset_hours')
                if st['short']:
                    w_l = float(w_len or 0) or f_econ_h
                    h_bef = min(f_econ_h, w_l) if (tau is None or tau == float('inf') or st.get('unstarted')) else min(f_econ_h, max(0.0, float(tau)))
                    f_step = h_bef * f_unit
                    f_per = min(max(0.0, f_econ_h - h_bef), w_l) * f_unit
                    _, fl_pct, mg_pct = short_window_limits(spec, wid, policy, now)
                    if st['remaining'] <= fl_pct or st['remaining'] - f_step < mg_pct or 100.0 - f_per < mg_pct:
                        fast_ok = False
                        break
                else:
                    req_h = f_chain_h if task['coupling'] == 'high' else f_econ_h
                    h_bef = req_h if (tau is None or tau == float('inf') or w_len is None) else min(req_h, max(0.0, float(tau)))
                    f_need = h_bef * f_unit
                    if st['remaining'] - f_need < policy['safety_margin_pct']:
                        fast_ok = False
                        break
                    if w_len is not None and req_h > h_bef:
                        f_rem = min(req_h - h_bef, float(w_len)) * f_unit
                        if 100.0 - f_rem < policy['safety_margin_pct']:
                            fast_ok = False
                            break
            if task.get('force_fast') and not fast_ok:
                exclude(f'fast mode ({f_price:g}x burn) exceeds remaining window safety margin')
            elif fast_ok:
                result['_five_fast_candidate'] = {
                    'price_multiplier': f_price, 'speed_multiplier': f_speed,
                    'econ_hours': f_econ_h, 'chain_hours': f_chain_h, 'hours': f_hours,
                    'step_usd': f_step_usd, 'chain_usd': f_chain_usd, 'source': fast_info.get('source'),
                }
        elif task.get('force_fast'):
            err = f"fast_mode has no cited public pricing/speed for {model} ({fast_info.get('reason') or fast_info.get('status') or 'missing'})"
            result['notes'].append(f'data error: {err}; fast mode not enabled')
            result.setdefault('data_errors', []).append(err)
    result['capacity'] = None           # the economy sets the priced window (apply_five_axis_economy)
    result['expected_hours'] = round(hours, 2)
    result['duration_hours'] = round(hours, 4)
    if dur_mult is not None:
        result['components']['duration_multiplier'] = round(dur_mult, 4)
    result['expected_pct'] = result['chain_pct'] = None
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
    # Quality is the continuous fit of the category scores at this effort to the demand, and nothing else (the effort's
    # extra burn is priced in its usd and its gain in the categories' effort shift). The economy decides.
    fit, axis_rows = five_axis_fit(values, task['five_axis_demand'], task['five_axis_targets'])
    result['_five_values'] = values         # the economy re-reads this candidate's scores at its effort over the demand grid
    # The candidate's four category scores at this effort: what the radar and the explanation show (tool_operation: the
    # weaker of its facets that have a value).
    tool = [z for z in values['tool_operation'].values() if z is not None]
    shown = {**{a: values[a] for a in FIVE_AXES if a != 'tool_operation'}, 'tool_operation': min(tool) if tool else None}
    result['components'].update({a: None if v is None else round(v, 4) for a, v in shown.items()})
    result['components'].update({f'share_{a}': v for a, v in
                                 five_axis_shares(values, ctx['accounts']['_five_axis']['targets']).items()})
    result['components'].update(five_axis_level=round(five_axis_level(axis_rows), 4),
                                five_axis_shortfall=round(sum(r['demand'] * r['shortfall'] for r in axis_rows.values())
                                                          / max(1, sum(r['demand'] for r in axis_rows.values())), 4))
    result['five_axis_demand'] = task['five_axis_demand']
    result['five_axis_fit'] = axis_rows
    usd_rate = 0.0 if spec.get('unlimited') or spec.get('unmetered') or spec.get('billing') == 'unknown' else         model_usd_per_hour(model, info)       # dollars per hour of this candidate's own work; effort is in its duration
    result['five_axis_price'] = {'usd_per_hour': round(usd_rate, 4)}
    result['components'].update({'fit': round(fit, 3), 'continuity': 0.0})
    if action == 'resume' and task['coupling'] in ('medium', 'high'):
        # a related task (--resume or an auto-matched continuation of the same chain) continues its worker: one demand level's worth
        result['components']['continuity'] = round(task['five_axis_continuity'], 4)
    return result


def candidate_for_bundle(rows, bundle_id, fast=False):
    """The ranking row for a bundle id and a fast flag: `<id>` is the plain variant, `<id>+fast` the fast one, and a fast
    flag with a plain id is the fast variant of that bundle. None when there is no such row."""
    for c in rows:
        if c['id'] == bundle_id and (not fast or c['bundle'].get('fast')):
            return c
    return next((c for c in rows if c.get('base_id') == bundle_id and bool(c['bundle'].get('fast')) == bool(fast)), None)


def five_axis_make_fast(c, fast_cand, reason):
    """Turn candidate `c` into its fast-mode variant at the vendor's burn and speed (a separate candidate, priced at its
    real spend; round 13). The variant gets its own id (`fast_modes.variant_id`) and remembers the bundle it launches as
    (`base_id`), so a pinned bundle and the alternatives name exactly one variant (round 14)."""
    c['base_id'] = c['id']
    c['id'] = fast_modes.variant_id(c['id'])
    c['bundle']['fast'] = True
    c['components']['fast'] = True
    if reason:
        c['components']['fast_reason'] = reason
    c['expected_hours'] = round(fast_cand['hours'], 2)
    c['duration_hours'] = round(fast_cand['hours'], 4)
    c['expected_usd'] = round(fast_cand['step_usd'], 2)
    c['chain_usd'] = round(fast_cand['chain_usd'], 2)
    c['notes'].append(f"fast mode ({fast_cand['price_multiplier']:g}x burn, up to {fast_cand['speed_multiplier']:g}x "
                      f"output speed: the vendor's upper bound, applied to the whole duration)")


def apply_five_axis_economy(candidates, eligible, task, accounts, policy, pool=None):
    """Quota economy v3 (advisor report v3; owner decisions of round 13): cost_i = integral over the candidate's own spend
    of the window's quota price lambda_w(y) dy, N = F - cost, with lambda_w from the account's own value ladder (averaged
    over the 63-point demand grid) and the period's own need probability pi_w(y). Only long reset-period windows price; the
    5-hour windows keep their feasibility role. A multi-window candidate pays its most expensive window (never both).
    Fast-mode variants are separate candidates appended to `candidates` and `eligible` at their real spend (a forced fast
    replaces the base). Returns the per-window report for the plan's `system.quota_windows`."""
    # The ladders, outside points and lumps come from the account's own pool (`rank` evaluates it with the task's flags
    # neutralised, owner round 16): neither a task flag nor whether a candidate fits the remaining capacity reaches a price.
    econ = FiveAxisEconomy(eligible if pool is None else pool, accounts, policy, task)
    base_hours = max(1e-6, float(task['hours'] or 0.0))

    def eval_windows(c, w_states, own_qsrc, spend, chain_usd, econ_h):
        rows = []
        for wid, (wspec, st, cap_usd_w, _) in w_states.items():
            rec = (five_axis_window_recovery(st, cap_usd_w) if st.get('short')
                   else econ.record((own_qsrc, wid), st, cap_usd_w))
            if rec is None:
                continue
            rem = st['remaining']
            rem_usd = rem * cap_usd_w / 100.0
            step_pct = (100.0 * spend / cap_usd_w) if cap_usd_w > 0 else 0.0
            chain_pct = (100.0 * chain_usd / cap_usd_w) if cap_usd_w > 0 else 0.0
            reset_h = rec['reset_hours']
            end_h = st['end_hours']
            period = rec['period_hours']
            row = {
                'window': wid, 'quota_source': own_qsrc,
                'short': bool(st.get('short')),
                'unstarted': bool(rec.get('unstarted')),
                'remaining': rem, 'after': rem - step_pct, 'step_pct': step_pct, 'chain_pct': chain_pct,
                'capacity_usd': round(cap_usd_w, 2), 'remaining_usd': round(rem_usd, 2),
                'reserved_usd': round(st.get('reserved_usd', 0.0), 2),
                'expected_usd': round(spend, 2), 'chain_usd': round(chain_usd, 2), 'after_usd': round(rem_usd - spend, 2),
                'used_usd': round(rec['used_usd'], 2), 'line_now_usd': round(rec['line_now_usd'], 2),
                'period_hours': None if period is None else round(period, 2),
                'elapsed_hours': None if period is None else round(period - reset_h, 2),
                'reset_hours': None if reset_h == float('inf') else reset_h,
                'end_hours': None if end_h == float('inf') else end_h, 'expires_first': end_h < reset_h,
                'stale': st['stale'], 'priced': False, 'cost': 0.0, 'opportunity_cost': 0.0,
                **({k: st[k] for k in ('natural_reset_hours', 'expected_reset_at') if k in st}),
            }
            if not row['short'] and spend > 0:
                got = econ.window_cost((own_qsrc, wid), rec, spend, econ_h)
                row.update(priced=True, cost=float(got['cost']), opportunity_cost=float(got['cost']),
                           quota_price=round(got['lambda_start'], 6), need_start=round(got['need_start'], 4),
                           need_end=round(got['need_end'], 4), spend_before_reset=round(got['spend_before'], 2),
                           spend_after_reset=round(got['spend_after'], 2))
            rows.append(row)
        if not rows:
            return None
        binding = max(rows, key=lambda r: (r['cost'], not r['short'], -r['after']))
        priced = [r for r in rows if r['priced']]
        return binding, {
            'windows': [{k: r[k] for k in ('quota_source', 'window', 'capacity_usd', 'used_usd', 'period_hours',
                                           'elapsed_hours', 'cost', 'quota_price', 'need_start',
                                           'need_end', 'spend_before_reset', 'spend_after_reset')}
                        | {'spend_usd': r['expected_usd'],
                           'reset_in_hours': None if r['reset_hours'] is None else round(r['reset_hours'], 2)}
                        for r in priced],
            'cost': binding['cost'], 'need_end_max': max((r['need_end'] for r in priced), default=0.0),
            'binding_window': binding['window'] if binding['priced'] else None,
        }

    def price(c, w_states, own_qsrc, spend, chain_usd, econ_h):
        got = eval_windows(c, w_states, own_qsrc, spend, chain_usd, econ_h)
        binding, summary = got if got else (None, {'windows': [], 'cost': 0.0, 'need_end_max': 0.0, 'binding_window': None})
        if binding is not None:
            c['capacity'] = binding
            c['expected_pct'] = round(binding['step_pct'], 2)
            c['chain_pct'] = round(binding['chain_pct'], 2)
        cost = float(summary['cost'])
        c['economy'] = summary
        c['components']['opportunity_cost'] = cost
        c['components']['quota_price'] = binding.get('quota_price', 0.0) if binding and binding['priced'] else 0.0
        c['components']['need_probability'] = binding.get('need_start', 0.0) if binding and binding['priced'] else 0.0
        c['components']['need_probability_end'] = summary['need_end_max']
        resets = [w['reset_in_hours'] for w in summary['windows'] if w['reset_in_hours'] is not None]
        c['components']['earliest_reset_hours'] = min(resets) if resets else None
        if binding is not None and binding['priced']:
            _, _, slack = econ.outside_at_task((own_qsrc, binding['window']))
            c['components']['substitute_slack'] = round(slack, 4)
        if isinstance(c.get('five_axis_price'), dict):
            c['five_axis_price'].update(cost=cost, quota_price=c['components']['quota_price'],
                                        need_probability=c['components']['need_probability'],
                                        need_probability_end=c['components']['need_probability_end'])

    variants = []
    for c in list(eligible):
        w_states = c.get('_five_window_states') or {}
        own_qsrc = provider_inventory.quota_source(c['bundle']['account'], accounts)
        econ_h = float(c.get('_five_econ_hours') or base_hours)
        step_usd = float(c.get('expected_usd') or 0.0)
        chain_usd = float(c.get('chain_usd') or step_usd)
        fast_cand = c.get('_five_fast_candidate') if w_states else None
        if fast_cand and task.get('force_fast'):
            five_axis_make_fast(c, fast_cand, 'user_override')
            price(c, w_states, own_qsrc, fast_cand['step_usd'], fast_cand['chain_usd'], fast_cand['econ_hours'])
            continue
        if fast_cand:
            twin = {**c, 'bundle': dict(c['bundle']), 'components': dict(c['components']), 'notes': list(c['notes']),
                    'excluded': list(c['excluded']),
                    'five_axis_price': dict(c['five_axis_price']) if isinstance(c.get('five_axis_price'), dict) else None}
            five_axis_make_fast(twin, fast_cand, 'net_score')
            price(twin, w_states, own_qsrc, fast_cand['step_usd'], fast_cand['chain_usd'], fast_cand['econ_hours'])
            variants.append(twin)
        price(c, w_states, own_qsrc, step_usd, chain_usd, econ_h)
    candidates.extend(variants)
    eligible.extend(variants)
    report = econ.report()
    for c in candidates:
        for priv in ('_five_axis_windows', '_five_window_states', '_five_econ_hours', '_five_chain_hours',
                     '_five_context_burn', '_five_fast_candidate', '_five_values', '_five_short_reasons',
                     '_five_long_wait', '_five_unmetered', '_five_metered'):
            c.pop(priv, None)
    return report


def rank(task, usage, policy, accounts, capabilities, now, unavailable=(), overrides=(), records=(),
         host_policy_config=None, *, read_local_state=True):
    state_policy = (host_policy.load_policy() if read_local_state else {'allowed_accounts': list((accounts.get('accounts') or {}).keys())}) if host_policy_config is None else host_policy_config
    ctx = {'policy': policy, 'accounts': accounts, 'capabilities': capabilities, 'usage': usage, 'now': now,
           'unavailable': set(unavailable), 'bundles': bundles(accounts),
           'host_policy': state_policy,
           'overrides': list(overrides), 'records': list(records or ()),
           'launch_failures': launch_failures.active() if (read_local_state and launch_failures.enabled()) else {},
           'model_latches': model_latch.active(now=now) if read_local_state else {},
           'provider_retries': provider_retry.active(now=now) if read_local_state else {},
           'live_models': live_models.load_or_refresh(
               accounts=accounts,
               allowed=lambda name: host_policy.account_allowed(state_policy, name),
               now=now,
           ) if read_local_state else {'accounts': {}}}
    pool_bundles = dict(ctx['bundles'])         # the catalog, before a task-named effort adds bundles to it
    ctx['bundles'].update(named_effort_bundles(task, ctx))
    five = ctx['five_axis'] = five_axis_config(policy)
    demand = five_axis_demand(task)
    panel_targets = accounts['_five_axis']['targets']
    task = {**task, 'five_axis_demand': demand, 'five_axis_targets': five_axis_targets(demand, panel_targets),
            'five_axis_continuity': five_axis_continuity_value(demand, panel_targets)}
    candidates = [evaluate_bundle(b, task, ctx) for b in ctx['bundles'].values()]
    eligible = [c for c in candidates if not c['excluded']]
    pool_task = {**task, 'local_only': False, 'outbound_ok': None, 'file_transfer': False,
                 'needs_visual': False, 'needs_gui': False, 'wants_fast': False,
                 'read_only_review': False, 'constraint_model': None, 'constraint_effort': None, 'pinned_bundle': None,
                 'metered_ok': True, 'metered_fallback': False, 'disable_fast': False, 'force_fast': False,
                 '_economy_pool': True}
    pool = [c for c in (evaluate_bundle(b, pool_task, ctx) for b in pool_bundles.values()) if not c['excluded']]
    quota_windows = apply_five_axis_economy(candidates, eligible, task, accounts, policy, pool=pool)
    # Net score N = fit + continuity (both in panel standard deviations) + a temporary boost - the economy's cost (already
    # in score units). No normalization over the candidate set.
    weights = dict(five['weights'])
    kind = task['kind']
    for c in eligible:
        raw_score = (sum(weights[k] * c['components'][k] for k in weights) + c['components'].get('boost', 0)
                     - c['components']['opportunity_cost'])
        # Evaluate at IEEE-754 unit scale so F = 0 has the same ~2.2e-16 float64 resolution as F > 0 (shift invariance).
        c['score'] = float((1.0 + raw_score) - 1.0)
    # Equal scores go to the candidate drawing on the earliest-expiring quota, then faster completion, then fewer USD,
    # then higher level; the harness order only separates siblings of one model, account and effort.
    harness_places(eligible, kind, policy, capabilities, (task['profile'] or {}).get('interaction_style'))
    eligible.sort(key=five_axis_order)
    for i, c in enumerate(eligible):
        c['components']['decided_by'] = (five_axis_deciding_key(c, eligible[i + 1]) if i + 1 < len(eligible)
                                         else 'only_candidate')
    system = {'weights': {k: round(v, 3) for k, v in weights.items()}, '_live_models': ctx['live_models'],
              'quota_windows': quota_windows}
    return candidates, eligible, system


def sibling_key(candidate):
    """Bundles that differ only in harness (Codex or Pi running the same model on the same account)."""
    bundle = candidate['bundle']
    return bundle['account'], bundle['model'], bundle.get('effort')


def harness_group(candidate):
    """Siblings that would also do the same thing: resuming the owner is not the same choice as starting its twin."""
    return sibling_key(candidate) + (candidate['action'],)


def harness_places(eligible, kind, policy, capabilities, style=None):
    """Five-axis path: each candidate's place behind its best sibling (same account, model, effort and action) in the
    harness order `harness_places` computes, as `harness_place`; the score is not touched."""
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
    admits it except work that needs vision and work the caller or classifier marks needs.local_only (it must run on
    this machine). dot_execution marked local-only is a contradiction, so Dot is not offered and the plan asks the user.
    Explicit false is kept. Route implies fill a flag only when the caller and the profile left it unset."""
    route = capabilities['routes'].get(kind) or {}
    resolved = resolve_modality_needs(needs, profile, route.get('implies') or {})
    if resolved.get('local_only') or resolved.get('visual'):
        return False
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


COUPLINGS = ('low', 'medium', 'high')       # chain.coupling; high is a related task that continues its worker


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
            'wants_fast': wants_fast,
            'read_only_review': read_only_review,
        }
    if profile is None:
        raise ValueError('task.profile is required: the classifier always produces it, with its axis_demand')
    kind = task_kind({**task, 'profile': profile}, capabilities, policy)
    derived = profile_difficulty(profile, policy)
    difficulty = task.get('difficulty') or derived
    source = 'caller' if task.get('difficulty') else 'profile'
    if difficulty not in DIFFICULTY_ORDER:
        raise ValueError('difficulty must be one of ' + ', '.join(DIFFICULTY_ORDER))
    chain = task.get('chain') if isinstance(task.get('chain'), dict) else {}
    coupling = chain.get('coupling') or 'low'
    if coupling not in COUPLINGS:
        raise ValueError('chain.coupling must be low, medium or high')
    if not (task.get('expected_hours') or profile.get('expected_hours')):
        raise ValueError('expected_hours is required: the caller gives it (--hours) or the classifier estimates it in task.profile')
    hours = float(task.get('expected_hours') or profile['expected_hours'])
    constraints = task.get('constraints') if isinstance(task.get('constraints'), dict) else {}
    constraint_model, constraint_effort = named_pin(constraints, task, accounts)
    implies = capabilities['routes'][kind].get('implies') or {}
    # Caller false stands. The profile fills an unset flag. Route implies fill only what both left unset.
    needs.update(resolve_modality_needs(needs, profile, implies))
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
            'profile': profile, 'profile_difficulty': derived,
            'needs_gui': needs_gui,
            'wants_fast': wants_fast,
            'read_only_review': read_only_review,
            'force_fast': bool(task.get('force_fast') or needs.get('force_fast')),
            'disable_fast': bool(task.get('disable_fast') or needs.get('disable_fast')),
            'pinned_bundle': task.get('pinned_bundle'),
            'chain_hours': float(chain.get('total_hours') or hours), 'coupling': coupling, 'worker': worker,
            'handoff_ready': bool(chain.get('handoff_ready')), 'needs_visual': needs['visual'],
            'local_only': bool(needs.get('local_only')),
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
    pinned = task.get('pinned_bundle')
    rows = [c for c in candidates if (pinned and candidate_for_bundle([c], pinned, task.get('force_fast')) is c)
            or (not pinned and not any(r in off_pin for r in c['excluded']))]
    named = task.get('pinned_bundle') or ':'.join(x for x in (task.get('constraint_model'), task.get('constraint_effort')) if x)
    rows = [c for c in rows if 'marked unavailable by the parent' not in c['excluded']] or rows
    if not rows:
        return [f'{named}: no configured bundle offers it']
    return [f"{named} via {c['id']}: {'; '.join(c['excluded'])}" for c in rows if c['excluded']][:4]


def plan(raw_task, usage, now=None, unavailable=(), records=None, accounts=None, capabilities=None, overrides=None,
         host_policy_config=None, *, read_local_state=True):
    accounts = accounts_override.normalize(accounts or load_accounts())
    capabilities = capabilities or load_capabilities(accounts=accounts)
    now = now or datetime.now(timezone.utc)
    usage = provider_inventory.merge_usage(usage, accounts, now)
    active_overrides = (load_overrides(now) if read_local_state else []) if overrides is None else overrides
    usage, accounts, applied = apply_overrides(active_overrides, usage, accounts, now)
    accounts = five_axis_overlay(accounts, read_local_state=read_local_state)
    policy = accounts['scoring']
    task = normalize_task(raw_task, policy, capabilities, accounts)
    if task['worker'] is None and task['chain_id'] and records:
        for record in reversed(records):
            if record.get('chain') == task['chain_id'] and (record.get('bundle') or {}).get('bundle_id'):
                w = {'bundle': record['bundle']['bundle_id'], 'from_ledger': True}
                wid = record.get('worker_id') or (record.get('worker') or {}).get('agent_id')
                if wid and read_local_state:
                    w['agent_id'] = wid
                    w.update(session_state(wid))
                task['worker'] = w
                break
    candidates, eligible, system = rank(task, usage, policy, accounts, capabilities, now, unavailable, applied, records,
                                        host_policy_config, read_local_state=read_local_state)
    # Metered last resort: only when no subscription bundle is eligible, re-rank with checked metered bundles admitted.
    if not eligible and any(c['excluded'] == [METERED_LAST_RESORT] for c in candidates):
        task['metered_fallback'] = True
        candidates, eligible, system = rank(task, usage, policy, accounts, capabilities, now, unavailable, applied,
                                            records, host_policy_config, read_local_state=read_local_state)
    worker_id = (task['worker'] or {}).get('bundle')
    resume = next((c for c in candidates if c['id'] == worker_id), None)
    decision = {'task': {k: task[k] for k in ('kind', 'difficulty', 'hours', 'chain_hours', 'coupling')}, 'system': system,
                'assumptions': assumptions(candidates, accounts)}
    cwd_warnings = [f'{name}: cwd_regex and legacy allowed_cwd both apply (intersection)'
                    for name, spec in accounts['accounts'].items() if spec.get('cwd_regex') and spec.get('allowed_cwd')]
    if cwd_warnings:
        decision.setdefault('warnings', []).extend(cwd_warnings)
    for err in QUOTA_DATA_ERRORS:
        unread = sorted({c['bundle']['account'] for c in candidates if any(err in str(r) for r in c['excluded'])})
        if unread:
            decision.setdefault('warnings', []).append(
                f"{', '.join(unread)}: {err}; out of automatic routing and no substitute for other accounts")
    aged = {}
    for c in candidates:
        at = c['components'].get('reading_at')
        if at and not c['excluded']:
            aged.setdefault(c['bundle']['account'], at)
    for name, at in sorted(aged.items()):
        decision.setdefault('warnings', []).append(
            f"{name}: priced from its last good usage reading, taken at {at} in the current reset period "
            f"(the latest read failed); a provider hold on a model or pool still wins over this reading")
    state_policy = (host_policy.load_policy() if read_local_state else {'allowed_accounts': list((accounts.get('accounts') or {}).keys())}) if host_policy_config is None else host_policy_config
    state_warnings = account_state.warnings(accounts.get('_discovery'), accounts['accounts'],
                                            lambda name: host_policy.account_allowed(state_policy, name))
    if state_warnings:
        decision.setdefault('warnings', []).extend(state_warnings)
    live_doc = system.pop('_live_models', None) or (live_models.load_or_refresh(
        accounts=accounts,
        allowed=lambda name: host_policy.account_allowed(state_policy, name),
        now=now,
    ) if read_local_state else {'accounts': {}})
    latches = model_latch.active(now=now) if read_local_state else {}
    latch_warnings = live_models.warnings(
        live_doc,
        accounts,
        lambda name: host_policy.account_allowed(state_policy, name),
        covered=set(latches),
    )
    latch_warnings += model_latch.warnings(latches, lambda name: host_policy.account_allowed(state_policy, name)) if read_local_state else []
    latch_warnings += provider_retry.warnings(provider_retry.active(now=now),
                                              lambda name: host_policy.account_allowed(state_policy, name)) if read_local_state else []
    if latch_warnings:
        # First: the roll answer shows only the first few warnings, and a model that cannot start must be seen.
        decision['warnings'] = [*latch_warnings, *decision.get('warnings', [])]
    decision['task'].update(difficulty_source=task['difficulty_source'], axis_demand=five_axis_demand(task),
                            max_recommended_effort=None)
    if task.get('wants_fast'):
        decision['task']['wants_fast'] = True
    if task.get('read_only_review'):
        decision['task']['read_only_review'] = True
    if task.get('metered_fallback'):
        decision['task']['metered_fallback'] = True
    if task['profile']:
        decision['task'].update(profile=task['profile'], profile_difficulty=task['profile_difficulty'])
    if applied:
        decision['overrides'] = applied
    ranked_candidates = [*eligible, *[c for c in candidates if c['excluded']]]
    result = {'stage': 'plan', 'source': 'deterministic_allocator', 'observed_at': now.isoformat(), 'decision': decision,
              'ranking': ranked_candidates, 'usage': usage}
    if not eligible:
        closest = sorted((c for c in candidates if c['excluded']), key=lambda c: (len(c['excluded']), -c['components']['fit']))[:2]
        named_block = named_pin_block(task, candidates)
        decision.update({'action': 'none', 'needs_user_decision': True,
                         'reason': 'No eligible bundle. Closest: ' + ' | '.join(
                             f"{c['id']}: {'; '.join(c['excluded'])}" for c in closest)})
        # When a bundle is held back only by quota, say when that quota returns, so the parent can wait.
        waits = sorted((h, c['id']) for c in candidates if c['excluded'] and (h := quota_wait_hours(c, usage)) is not None)
        if waits:
            hours, bundle_id = waits[0]
            decision['next_reset'] = {'bundle': bundle_id, 'hours': round(hours, 1),
                                      'at': (now + timedelta(hours=hours)).isoformat()}
            decision['reason'] += f'. {bundle_id} is held back by quota that returns in {hours:.1f}h: wait, or decide otherwise.'
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
        candidate_for_bundle(pool, task['pinned_bundle'], task.get('force_fast'))
        if task.get('pinned_bundle') else None
    )
    top = pinned_cand or pool[0]
    better = eligible[0]
    if better is not top and better['available_in_hours']:
        decision['better_if_waiting'] = {'bundle': better['id'], 'hours': round(better['available_in_hours'], 1),
                                         'score_gain': round(better['score'] - top['score'], 4)}
    forced = pinned_cand is not None
    decision.update({'action': top['action'], 'needs_user_decision': False,
                     'continuity': ('high-coupling chain stays with its owner'
                                    if (top is resume and top['action'] == 'resume' and not forced) else None)})
    if (top['available_in_hours'] or 0) > can_wait:
        # can_wait_hours is a limit, not a preference: say so instead of quietly starting later than allowed
        decision.update({'needs_user_decision': True, 'wait_hours': round(top['available_in_hours'], 1),
                         'reason': (f"The chain owner {top['id']} can resume in {top['available_in_hours']:.1f}h"
                                    if top is resume and top['action'] == 'resume'
                                    else f"Nothing eligible can start within {can_wait:g}h; the best, {top['id']}, can start in {top['available_in_hours']:.1f}h")
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
    if top.get('data_errors'):
        for note in top.get('notes') or ():
            if str(note).startswith('data error:'):
                w_note = f"{top['id']}: {note}"
                if w_note not in decision.get('warnings', ()):
                    decision.setdefault('warnings', []).append(w_note)
    if top['bundle'].get('fast'):
        decision['fast'] = True
        decision['fast_reason'] = top['components'].get('fast_reason')
    decision.setdefault('reason', explain(top, system))
    # No required ability: the fit and the economy chose this effort.
    chosen = top['bundle'].get('effort')
    decision['task']['max_recommended_effort'] = chosen if chosen in EFFORT_ORDER else None
    result['choice'] = top['id']
    result['selected_bundle'] = dict(top['bundle'], bundle_id=top.get('base_id') or top['id'], expected_usd=top.get('expected_usd'),
                                     expected_hours=top.get('expected_hours'))
    result['expected_usd'] = top.get('expected_usd')
    result['expected_hours'] = top.get('expected_hours')
    result['decision']['alternatives'] = list(dict.fromkeys(c['id'] for c in pool if c['id'] != top['id']))[:3]
    if top['available_in_hours']:
        decision.update({'action': 'defer', 'start_after_hours': round(top['available_in_hours'], 1)})
    return result


SNAPSHOT_FULL, SNAPSHOT_COMPACT, SNAPSHOT_EXCLUDED = 6, 8, 16
SNAPSHOT_CAPACITY_KEYS = ('window', 'remaining', 'after', 'step_pct', 'chain_pct', 'capacity_usd', 'remaining_usd',
                          'expected_usd', 'reset_hours', 'end_hours', 'expires_first',
                          'opportunity_cost', 'line_now_usd', 'used_usd', 'quota_source', 'period_hours', 'elapsed_hours',
                          'priced', 'cost', 'quota_price', 'need_start', 'need_end', 'spend_before_reset',
                          'spend_after_reset', 'stale')
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
    excluded = [str(r)[:220] for r in (candidate.get('excluded') or [])[:3]]
    row = {'id': candidate.get('id'), 'bundle': bundle, 'score': _round(candidate.get('score')), 'action': candidate.get('action'),
           'excluded': excluded}
    if candidate.get('estimated_axes'):
        row['estimated_axes'] = list(candidate['estimated_axes'])
    if candidate.get('manual_scores'):
        row['manual_scores'] = copy.deepcopy(candidate['manual_scores'])
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
        shares = tuple(f'share_{a}' for a in FIVE_AXES)
        keep = ('fit', 'opportunity_cost', *FIVE_AXES, *shares, 'continuity',
                'boost',
                'five_axis_level', 'quota_price', 'need_probability', 'need_probability_end', 'substitute_slack',
                'decided_by', 'earliest_reset_hours', 'reading_at', 'duration_multiplier', 'duration_source')
        # a category score or share of 0 is a real value (the anchor's score, the panel's weakest share), kept
        row['components'] = {k: _round(comps[k]) for k in keep
                             if comps.get(k) is not None and (k in FIVE_AXES or k in shares or comps[k])}
        for k in ('fit', 'opportunity_cost'):
            if k in comps:
                row['components'][k] = _round(comps[k])
    econ = candidate.get('economy')
    if isinstance(econ, dict) and not excluded:
        # What the README page reads (round 13): per priced window the candidate's spend, cost and need probability at the
        # start and at the end of that spend, plus the key that separated it from the next row.
        row['economy'] = {'cost': _round(econ.get('cost')), 'need_end_max': _round(econ.get('need_end_max')),
                          'binding_window': econ.get('binding_window'),
                          'decided_by': comps.get('decided_by'),
                          'windows': [{k: _round(v) for k, v in w.items()}
                                      for w in econ.get('windows') or ()]}
        if 'expected_usd' not in row and candidate.get('expected_usd') is not None:
            row['expected_usd'] = _round(candidate['expected_usd'])
        if 'expected_hours' not in row and candidate.get('expected_hours') is not None:
            row['expected_hours'] = _round(candidate['expected_hours'])
        line = candidate_quota_line(candidate)
        if line is not None:
            row['quota_line'] = line
    return row


def candidate_quota_line(candidate):
    """The window this candidate's cost is binding on, as the README quota page reads it (round 16, J1): {window: 'source/window'
    (the key in the snapshot's quota_line.windows), hours: the candidate's hours, spend_usd: its spend of that window}. None
    when the candidate prices no long window. The allocator is the one writer; `economy` and `quota_windows` stay as they are."""
    econ = candidate.get('economy')
    bound = econ.get('binding_window') if isinstance(econ, dict) else None
    if not bound:
        return None
    for w in econ.get('windows') or ():
        if w.get('window') == bound and w.get('quota_source') is not None:
            hours = candidate.get('expected_hours')
            return {'window': f"{w['quota_source']}/{bound}", 'hours': _round(hours if hours is not None else 0.0),
                    'spend_usd': _round(w.get('spend_usd'))}
    return None


def snapshot_quota_line(quota_windows):
    """`snapshot.quota_line` for the README quota page: every priced long window of the decision, keyed by its label, with the
    capacity C, the use U and the clock the page draws (elapsed hours since the period's start, hours to the reset; None =
    never resets). `account` and `window_id` are the usage collector's keys, where the page may look its readings up."""
    windows = {}
    for w in quota_windows or ():
        label = w.get('label')
        if not label:
            continue
        windows[label] = {'label': label, 'capacity_usd': w.get('capacity_usd'), 'used_usd': w.get('used_usd'),
                          'elapsed_hours': w.get('elapsed_hours'), 'reset_in_hours': w.get('reset_in_hours'),
                          'account': w.get('quota_source'), 'window_id': w.get('window')}
    return {'windows': windows} if windows else None


def decision_snapshot(result, task=None):
    """The decision as the allocator made it, small enough for one ledger line, or None when `result` has no ranking.

    Holds the task demand, the scoring system in force (weights, harness order) and the ranking
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
        **({'quota_line': quota_line} if (quota_line := snapshot_quota_line(system.get('quota_windows'))) else {}),
        'decision': {**{k: decision[k] for k in SNAPSHOT_DECISION_KEYS if decision.get(k) is not None},
                     'task': {k: v for k, v in d_task.items() if k != 'profile'} | (
                         {'profile': clean_profile} if clean_profile is not None else {}),
                     'system': {'weights': system.get('weights'),
                                'quota_windows': system.get('quota_windows')}},
        'ranking': rows,
        'ranking_total': len(ranking), 'ranking_eligible': len(eligible),
        'ranking_omitted': {'eligible': max(0, len(eligible) - SNAPSHOT_FULL - SNAPSHOT_COMPACT),
                            'excluded': max(0, len(blocked) - SNAPSHOT_EXCLUDED)},
    }


def assumptions(candidates, accounts):
    notes = []
    for c in candidates:
        if c['excluded']:
            continue
        spec = accounts['accounts'][c['bundle']['account']]
        if parse_time(spec.get('expires_at')) and spec.get('expires_precision') == 'date':
            notes.append(f"{c['bundle']['account']}: expiry is a date only; end of day assumed")
    return sorted(set(notes))


def explain(top, system):
    parts = [f"{top['id']} ({top['action']}) scored {top['score']:.4f}"]
    cap = top.get('capacity')
    if cap:
        rem_usd_str = f" (${cap['remaining_usd']:.2f})" if cap.get('remaining_usd') is not None else ""
        exp_usd_str = f" (~${top['expected_usd']:.2f})" if top.get('expected_usd') is not None else ""
        priced = (f"need probability {cap['need_start']:.2f} -> {cap['need_end']:.2f}, quota price "
                  f"{cap['quota_price']:.5f} fit/USD" if cap.get('priced') else 'a short window: feasibility only, not priced')
        parts.append(f"{cap['window']}: {cap['remaining']:.0f}% left{rem_usd_str}, work ~{top['expected_pct']}%{exp_usd_str} "
                     f"(chain ~{top['chain_pct']}%), {priced}, "
                     + (f"before its {'expiry' if cap['expires_first'] else 'reset'} in {cap['end_hours']:.0f}h, "
                        if cap.get('end_hours') is not None else 'with no reset, ')
                     + f"opportunity cost {top['components']['opportunity_cost']:.4f}")
    if top['bundle'].get('fast'):
        parts.append(f"fast mode enabled ({top['components'].get('fast_reason') or 'priced as its own candidate'})")
    if top['components'].get('boost'):
        parts.append(f"includes a temporary user-override boost of {top['components']['boost']:+.2f}")
    if top['components'].get('continuity'):
        parts.append(f"continuing its related worker adds {top['components']['continuity']:.2f} (one demand level)")
    return '; '.join(parts)
