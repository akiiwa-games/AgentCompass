"""Quota windows and pools: which windows a bundle draws, their state, and the launch blockers they raise."""

from datetime import datetime, timedelta, timezone
import re

import window_scope
import provider_inventory
import account_fleet
import axis_scores
import quota_economy


WINDOW_ALIASES = {'seven_day': 'usage', 'usage-10080m': 'usage', 'usage-5h': 'five_hour', 'usage-300m': 'five_hour'}


def canonical_window_id(spec, wid):
    windows = (spec or {}).get('windows') or {}
    if wid in windows:
        return wid
    mapped = WINDOW_ALIASES.get(wid)
    return mapped if mapped in windows else wid


def live_dispatch_holds(records):
    """One live hold per worker. The newest row for an agent_id counts; a row with no agent_id still counts."""
    bare = []
    newest = {}
    for rec in records or ():
        if not isinstance(rec, dict):
            continue
        agent = rec.get('agent_id')
        if not isinstance(agent, str) or not agent:
            bare.append(rec)
            continue
        prev = newest.get(agent)
        if prev is None or _hold_is_newer(rec, prev):
            newest[agent] = rec
    return bare + list(newest.values())


def _hold_is_newer(rec, prev):
    rec_at = parse_time(rec.get('at') or rec.get('created_at') or rec.get('updated_at'))
    prev_at = parse_time(prev.get('at') or prev.get('created_at') or prev.get('updated_at'))
    if rec_at is not None and (prev_at is None or rec_at >= prev_at):
        return True
    if rec_at is None and prev_at is None:
        return str(rec.get('dispatch_id') or '') >= str(prev.get('dispatch_id') or '')
    return False


def inflight_reserved_usd(account, spec, wid, wspec, state, win_cap_usd, records, accounts, policy, now):
    """Hold API-equivalent budget only for live dispatch leases, once per dispatch ID.

    Active commitments remain fully reserved until terminal/release/lease expiry.
    One worker's older open round is not added to its newer round. This is conservative
    while usage catches up; provider percentages cannot reliably attribute consumption
    to one concurrent worker. Plan/select/record never reserve.
    """
    if not win_cap_usd or win_cap_usd <= 0:
        return 0.0
    active = {}
    for rec in live_dispatch_holds(records):
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


def parse_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def clamp(value, low, high):
    return max(low, min(high, value))


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
    names = (account_fleet.canonical(bundle.get('model') or '', accounts), bundle.get('model') or '')
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
    cap = quota_economy.capacity_usd(spec, wid)
    small = cap is not None and cap < policy.get('small_short_capacity_usd', 50)
    return (small,
            policy.get('small_short_window_floor_pct', 25) if small else policy.get('short_window_floor_pct', 10),
            policy.get('small_short_safety_margin_pct', 12) if small else policy.get('safety_margin_pct', 5))


def reserve_inflight(account, spec, wid, wspec, state, records, accounts, policy, now):
    """Apply active in-flight reservation deduction to a known window state; returns (state, reserved_usd, reserved_pct)."""
    if not state or not state.get('known') or not records:
        return state, 0.0, 0.0
    win_cap_usd = quota_economy.capacity_usd(spec, wid)
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
        accounts = axis_scores.five_axis_overlay(accounts)
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
    model = account_fleet.canonical(bundle.get('model') or '', accounts)
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
            if state['known'] and state['remaining'] <= margin and quota_economy.unit_rate(spec, wid, model, account_fleet.model_spec(accounts, model)):
                if state['reset_hours'] == float('inf'):
                    reason = (f"{wid} has {state['remaining']:.1f}% left, within the {margin:g}% safety margin "
                              "(this balance does not reset)")
                else:
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


def metered_usd_uncapped(spec):
    """A dollar account with no budget has no window and is not excluded for exhaustion.

    It stays a metered last resort. A missing reading is the existing data-gap path;
    this predicate is only the account whose configuration states no budget.
    """
    if not isinstance(spec, dict) or spec.get('backend_kind') != 'metered_usd':
        return False
    cap = spec.get('capacity_usd')
    balance = cap.get('balance') if isinstance(cap, dict) else None
    return not isinstance(balance, (int, float)) or isinstance(balance, bool) or balance <= 0


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
        cap = quota_economy.capacity_usd(account_spec, wid)
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
    elif (row['short'] or account_fleet.account_family(account_spec) == 'antigravity' or spec.get('starts_on_first_use')) and remaining == 100:
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


def cadence_window_state(account, spec, acct_usage, records, now):
    """(wspec, state, limit_count) for an account-configured dispatch cadence window (e.g. ChatGPT Web 5 per 12h),
    or None when the account declares no `cadence`. Priced on current-period used dispatches and remaining period hours."""
    cad = (spec or {}).get('cadence')
    if not isinstance(cad, dict):
        return None
    win_sec, cap_raw = cad.get('window_seconds'), cad.get('limit')
    if (isinstance(win_sec, bool) or not isinstance(win_sec, (int, float)) or win_sec <= 0
            or isinstance(cap_raw, bool) or not isinstance(cap_raw, (int, float)) or cap_raw <= 0):
        return None
    win_sec, cap = float(win_sec), float(cap_raw)
    period_h = win_sec / 3600.0
    wspec = {'hours': period_h}
    usage_row = acct_usage if isinstance(acct_usage, dict) else {}
    acct_stale = usage_row.get('status') == 'stale' or usage_row.get('stale') is True
    raw_windows = usage_row.get('windows')
    raw_obs = None
    if isinstance(raw_windows, list):
        raw_obs = next((w for w in raw_windows if isinstance(w, dict) and w.get('id') == 'cadence'), None)
    elif isinstance(raw_windows, dict) and isinstance(raw_windows.get('cadence'), dict):
        raw_obs = raw_windows['cadence']
    obs = cycle_observation(wspec, raw_obs, usage_row, now, read_failed_ok=True) if raw_obs is not None else None
    if obs is not None:
        rem_pct = None
        if obs.get('used') is not None and not isinstance(obs.get('used'), bool):
            rem_pct = clamp(100.0 * (cap - float(obs['used'])) / cap, 0.0, 100.0)
        elif obs.get('used_count') is not None and not isinstance(obs.get('used_count'), bool):
            rem_pct = clamp(100.0 * (cap - float(obs['used_count'])) / cap, 0.0, 100.0)
        elif obs.get('remaining_count') is not None and not isinstance(obs.get('remaining_count'), bool):
            rem_pct = clamp(100.0 * float(obs['remaining_count']) / cap, 0.0, 100.0)
        else:
            rem_pct = obs.get('remaining_pct_before_reset')
            if rem_pct is None:
                rem_pct = obs.get('observed_remaining_pct')
            if rem_pct is None and obs.get('used_pct') is not None:
                rem_pct = clamp(100.0 - float(obs['used_pct']), 0.0, 100.0)
        seconds = obs.get('seconds_until_reset')
        if seconds is None and (natural_reset := parse_time(obs.get('resets_at'))) is not None:
            seconds = (natural_reset - now).total_seconds()
        unstarted = bool(obs.get('unstarted'))
        if seconds is None and rem_pct is not None:
            seconds = win_sec
            if rem_pct >= 100.0 - 1e-9:
                unstarted = True
        if rem_pct is not None and seconds is not None and float(seconds) > 0:
            fetched = parse_time(usage_row.get('fetched_at'))
            active_ids = set()
            for r in live_dispatch_holds(records):
                if not isinstance(r, dict) or r.get('stage') != 'dispatch' or not r.get('dispatch_id'):
                    continue
                if (r.get('bundle') or {}).get('account') != account:
                    continue
                exp = parse_time(r.get('lease_until'))
                if r.get('state') not in ('prepared', 'launching', 'launched', 'started') or exp is None or exp <= now:
                    continue
                t_rec = parse_time(r.get('at') or r.get('created_at') or r.get('updated_at'))
                if fetched is not None and t_rec is not None and t_rec <= fetched:
                    continue
                active_ids.add(r['dispatch_id'])
            if active_ids:
                rem_pct = max(0.0, float(rem_pct) - 100.0 * len(active_ids) / cap)
            reset_h = float(seconds) / 3600.0
            state = {
                'id': 'cadence', 'hours': period_h, 'short': False, 'known': True, 'stale': acct_stale,
                'full': 100.0, 'remaining': float(rem_pct), 'reset_hours': reset_h, 'end_hours': reset_h,
                'expiry_hours': None, 'target_remaining': 100.0 * min(1.0, reset_h / period_h),
                'reset_at': obs.get('resets_at'), 'unstarted': unstarted and float(rem_pct) >= 100.0 - 1e-9,
            }
            return wspec, state, cap
    by_dispatch = {}
    anon_times = []
    for r in live_dispatch_holds(records):
        if not isinstance(r, dict) or (r.get('bundle') or {}).get('account') != account:
            continue
        stage = r.get('stage')
        t = None
        if stage in ('select', 'record') and r.get('choice') not in (None, 'none'):
            cand_t = parse_time(r.get('at'))
            if cand_t is not None and 0.0 <= (now - cand_t).total_seconds() < win_sec:
                t = cand_t
        elif stage == 'dispatch' and r.get('state') in ('prepared', 'launching', 'launched', 'started'):
            exp = parse_time(r.get('lease_until'))
            if exp is not None and exp > now:
                cand_t = parse_time(r.get('at') or r.get('created_at') or r.get('updated_at')) or now
                if (now - cand_t).total_seconds() < win_sec:
                    t = cand_t if cand_t <= now else now
        if t is None:
            continue
        did = r.get('dispatch_id')
        if isinstance(did, str) and did:
            prev = by_dispatch.get(did)
            by_dispatch[did] = t if prev is None or t < prev else prev
        else:
            anon_times.append(t)
    events = [*by_dispatch.values(), *anon_times]
    used = float(len(events))
    rem_pct = clamp(100.0 * (cap - used) / cap, 0.0, 100.0)
    if events:
        oldest = min(events)
        seconds = max(1e-6, win_sec - max(0.0, (now - oldest).total_seconds()))
        unstarted = False
    else:
        seconds = win_sec
        unstarted = True
    reset_h = seconds / 3600.0
    state = {
        'id': 'cadence', 'hours': period_h, 'short': False, 'known': True, 'stale': False,
        'full': 100.0, 'remaining': rem_pct, 'reset_hours': reset_h, 'end_hours': reset_h,
        'expiry_hours': None, 'target_remaining': 100.0 * min(1.0, reset_h / period_h),
        'reset_at': None, 'unstarted': unstarted,
    }
    return wspec, state, cap
