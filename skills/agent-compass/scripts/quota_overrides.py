"""Manual quota overrides: the `override` stage that writes them, loading, landing on reset, retiring, and applying them
to the usage reading."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path

import override_store
import account_fleet
import quota_pools


def overrides_path():
    configured = os.environ.get('ROUTER_OVERRIDES')
    return Path(configured).expanduser() if configured else Path.home() / '.agent-compass' / 'router-overrides.json'


def load_overrides(now, path=None, usage=None, accounts=None, prune=True):
    """Overrides the user asked for that have not yet run out (`until`) or landed their reset, written by `agentcompass override`."""
    target = path or overrides_path()
    if not target.exists():
        return []
    import state_upgrade
    issue = state_upgrade.check_router_overrides(target)
    if issue:
        raise ValueError(issue)
    try:
        document = json.loads(target.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        raise ValueError(state_upgrade.format_issue(
            target, f'router overrides invalid JSON ({exc})', state_upgrade.fix_delete(target),
        )) from exc
    rows = document.get('overrides') if isinstance(document, dict) else None
    active, landed, depleted = [], {}, {}
    for o in rows or []:
        if not (isinstance(o, dict) and o.get('account')
                and (until := quota_pools.parse_time(o.get('until')) or expected_reset_time(o)) is not None and until > now):
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
    model = account_fleet.canonical(override['model'], accounts) if override.get('model') else None
    binding = {wid: wspec for wid, wspec, _ in quota_pools.applicable_windows(spec, model, {})} if model else spec.get('windows', {})
    for window in row.get('windows') or ():
        if not isinstance(window, dict):
            continue
        wid = quota_pools.canonical_window_id(spec, window.get('id'))
        if override.get('window'):
            if wid != quota_pools.canonical_window_id(spec, override['window']):
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
    confirmed = quota_pools.parse_time(override.get('reset_confirmed_at'))
    created = quota_pools.parse_time(override.get('created_at'))
    if confirmed is not None and created is not None and created <= confirmed <= now:
        return True  # Persist an observed reset; later stale/refilled quota cannot undo it.
    baselines = override.get('baseline_windows')
    if not isinstance(baselines, dict) or not baselines:
        return False
    now = now or datetime.now(timezone.utc)
    account = override.get('account')
    spec = ((accounts or {}).get('accounts') or {}).get(account) or {}
    row = ((usage or {}).get('accounts') or {}).get(account) or {}
    fetched = quota_pools.parse_time(row.get('fetched_at'))
    created = quota_pools.parse_time(override.get('created_at'))
    if (row.get('status') != 'fresh' or row.get('stale') is True or row.get('available') is False
            or fetched is None or created is None or fetched <= created
            or not 0 <= (now - fetched).total_seconds() <= 900
            or row.get('account', account) != account):
        return False
    for window in row.get('windows') or ():
        if not isinstance(window, dict):
            continue
        wid = quota_pools.canonical_window_id(spec, window.get('id'))
        if override.get('window') and wid != quota_pools.canonical_window_id(spec, override['window']):
            continue
        base = baselines.get(wid) or baselines.get(window.get('id'))
        if not isinstance(base, dict) or base.get('account', account) != account:
            continue
        baseline_time = quota_pools.parse_time(base.get('fetched_at'))
        if baseline_time is None or fetched <= baseline_time:
            continue
        if base.get('observation_id') and base['observation_id'] == row.get('observation_id'):
            continue
        obs_reset, base_reset = quota_pools.parse_time(window.get('resets_at')), quota_pools.parse_time(base.get('resets_at'))
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
            confirmed = quota_pools.parse_time(o.get('reset_confirmed_at'))
            if confirmed is None or not quota_pools.parse_time(o['created_at']) <= confirmed <= now:
                confirmed = now
            confirmations.append(confirmed)
            if not retire:
                rows.append({**o, 'reset_confirmed_at': confirmed.isoformat()})
        if not confirmations:
            return
        last = document.get('last_reset') or {}
        previous = quota_pools.parse_time(last.get('at')) if isinstance(last, dict) else None
        newest = max(confirmations)
        if previous is None or previous > now or newest > previous:
            last = {'at': newest.isoformat()}
        updated = {**document, 'overrides': rows, 'last_reset': last}
        if updated != document:
            override_store.write(target, updated)


def expected_reset_time(override):
    """A reset expectation is a deadline, never evidence of replenished capacity."""
    explicit = quota_pools.parse_time(override.get('expected_reset_at') or override.get('resets_at'))
    if explicit is not None:
        return explicit
    if override.get('stop_on_reset') or override.get('intent') in ('tray:anticipated-reset', 'panel:boost'):
        return quota_pools.parse_time(override.get('until'))
    return None


def window_reset_expectation(overrides, account, model, wid, spec, accounts, now):
    """Earliest active expectation for this model and its bound long window."""
    matches = []
    for rule in overrides:
        reset = expected_reset_time(rule)
        until = quota_pools.parse_time(rule.get('until'))
        if (rule.get('account') != account or reset is None or reset <= now
                or (until is not None and until <= now)
                or (rule.get('model') and account_fleet.canonical(rule['model'], accounts) != model)):
            continue
        if rule.get('window'):
            if quota_pools.canonical_window_id(spec, rule['window']) != wid:
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
        until = quota_pools.parse_time(o.get('until')) or expected_reset_time(o)
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
        expires = quota_pools.parse_time(o.get('expires_at'))
        resets = expected_reset_time(o)
        if expires is not None:
            spec['expires_at'], spec['expires_precision'] = expires.isoformat(), 'time'
        if resets is not None:
            for window in ((usage.get('accounts') or {}).get(o['account']) or {}).get('windows', []):
                wid = quota_pools.canonical_window_id(spec, window.get('id'))
                hours = (spec.get('windows', {}).get(wid) or {}).get('hours', 168)
                if (wid == quota_pools.canonical_window_id(spec, o['window'])) if o.get('window') else hours >= short:
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


def manage_overrides(args, now=None):
    with override_store.locked(overrides_path()):
        return _manage_overrides_locked(args, now)


def _manage_overrides_locked(args, now=None):
    """Upsert priority/reset intent or expiry, under the caller-held store lock."""
    import decision_ledger
    import usage_reading
    now = now or datetime.now(timezone.utc)
    path = overrides_path()
    try:
        document = json.loads(path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        document = {}
    rows = [o for o in (document.get('overrides') or []) if isinstance(o, dict)]
    accounts = account_fleet.load_accounts()
    usage = usage_reading.usage_snapshot(getattr(args, 'usage_cache', None), None, now)
    active = load_overrides(now, path, usage=usage, accounts=accounts, prune=False)
    if args.list:
        return {'stage': 'override', 'path': str(path), 'active': active}
    if args.clear:
        kept = [] if args.clear == 'all' else [o for o in rows if o.get('id') != args.clear]
        if len(kept) == len(rows):
            raise ValueError(f'No override with id {args.clear!r}.')
        removed = len(rows) - len(kept)
        rows = kept
    else:
        if not args.account or not (args.basis or '').strip():
            raise ValueError('override requires --account and --basis (quote the user).')
        if args.account not in accounts['accounts']:
            raise ValueError(f'Unknown account {args.account!r}.')

        def moment(at, hours, name):
            if at and hours is not None:
                raise ValueError(f'Give --{name}-at or --{name}-in, not both.')
            if hours is not None:
                return now + timedelta(hours=hours)
            if at:
                parsed = quota_pools.parse_time(at)
                if parsed is None:
                    raise ValueError(f'--{name}-at needs an ISO time with a zone, e.g. 2026-10-12T21:00+08:00.')
                return parsed
            return None

        resets, expires = moment(args.resets_at, args.resets_in, 'resets'), moment(args.expires_at, args.expires_in, 'expires')
        if args.window and args.window not in accounts['accounts'][args.account].get('windows', {}):
            raise ValueError(f'{args.account} has no window {args.window!r}.')
        explicit_stop_on_reset = bool(getattr(args, 'stop_on_reset', False))
        stop_on_reset = (explicit_stop_on_reset or (resets is not None)
                         or bool(getattr(args, 'reset_confirmation', None)) or bool(getattr(args, 'rebaseline', False)))
        if args.until:
            until = quota_pools.parse_time(args.until)
        elif stop_on_reset:
            until = max([t for t in (resets, expires) if t] + [now + timedelta(hours=24)])
        else:
            until = max([t for t in (resets, expires) if t] or [now + timedelta(hours=24)])
        if until is None or until <= now:
            raise ValueError('--until must be a future ISO time with a zone.')
        baseline_windows = None
        if stop_on_reset:
            acct_row = (usage.get('accounts') or {}).get(args.account) or {}
            fetched = quota_pools.parse_time(acct_row.get('fetched_at'))
            if (acct_row.get('status') == 'fresh' and acct_row.get('stale') is not True
                    and acct_row.get('available') is not False
                    and acct_row.get('account', args.account) == args.account
                    and fetched is not None and 0 <= (now - fetched).total_seconds() <= 900):
                spec = accounts['accounts'][args.account]
                short = accounts['scoring']['short_window_hours']
                bw = {}
                for w in acct_row.get('windows') or ():
                    if not isinstance(w, dict):
                        continue
                    wid = quota_pools.canonical_window_id(spec, w.get('id'))
                    hours = (spec.get('windows', {}).get(wid) or {}).get('hours', 168)
                    if (wid == quota_pools.canonical_window_id(spec, args.window)) if args.window else hours >= short:
                        used_val = w.get('used_pct')
                        rem_val = w.get('remaining_pct_before_reset')
                        if rem_val is None:
                            rem_val = w.get('observed_remaining_pct')
                        if used_val is None and rem_val is not None:
                            used_val = 100.0 - float(rem_val)
                        row_bw = {
                            k: v for k, v in (
                                ('account', args.account),
                                ('fetched_at', acct_row.get('fetched_at')),
                                ('observation_id', acct_row.get('observation_id')),
                                ('used_pct', used_val),
                                ('remaining_pct', rem_val),
                                ('resets_at', w.get('resets_at')),
                            ) if v is not None
                        }
                        if row_bw:
                            bw[wid] = row_bw
                if bw:
                    baseline_windows = bw
        rows = [o for o in rows if (until_o := quota_pools.parse_time(o.get('until'))) is not None and until_o > now
                and not override_reset_landed(o, usage, accounts, now)
                and not override_depleted(o, usage, accounts)]
        intent = (getattr(args, 'intent', None) or ' '.join(args.basis.split())).strip()
        model = account_fleet.canonical(args.model, accounts) if args.model else None
        boost = 0.1 if args.boost is None else args.boost
        if not expires and override_depleted({'account': args.account, 'model': model, 'window': args.window,
                                                        'boost': boost}, usage, accounts):
            raise ValueError(f'{args.account} is at or below the {accounts["scoring"]["safety_margin_pct"]}% safety margin; '
                             'the router dispatches no new work there, so a priority would be retired at once.')
        key = (intent, args.account, model, args.window)
        previous = next((o for o in rows if (
            o.get('intent') or ' '.join(o.get('basis', '').split()), o.get('account'),
            account_fleet.canonical(o.get('model'), accounts) if o.get('model') else None, o.get('window')) == key), None)
        rebaseline = bool(getattr(args, 'rebaseline', False))
        if rebaseline:
            if previous is None:
                raise ValueError('--rebaseline requires an existing matching intent/account/model/window.')
            if not baseline_windows or any(
                    not isinstance(w.get('used_pct'), (int, float)) or not 0 <= w['used_pct'] <= 100
                    or quota_pools.parse_time(w.get('resets_at')) is None
                    for w in baseline_windows.values()):
                raise ValueError('--rebaseline needs fresh, valid quota observations; the existing override is unchanged.')
        confirmation = (getattr(args, 'reset_confirmation', None)
                        or (previous or {}).get('reset_confirmation') or 'cycle')
        if confirmation not in ('cycle', 'replenishment'):
            raise ValueError('Unknown reset confirmation mode.')
        entry_id = hashlib.sha256(json.dumps(key, ensure_ascii=False).encode('utf-8')).hexdigest()[:24]
        if previous and previous.get('stop_on_reset'):
            stop_on_reset = True  # A boost-only update is not permission to disable reset retirement.
        entry = {'id': previous['id'] if previous else entry_id, 'intent': intent,
                 'account': args.account, 'window': args.window, 'model': model,
                 'expected_reset_at': resets.isoformat() if resets else (previous or {}).get('expected_reset_at', (previous or {}).get('resets_at')),
                 'expires_at': expires.isoformat() if expires else (previous or {}).get('expires_at'),
                 'boost': boost, 'until': until.isoformat(),
                 'stop_on_reset': True if stop_on_reset else None,
                 'baseline_windows': baseline_windows, 'reset_confirmation': confirmation,
                 'basis': args.basis.strip()[:400], 'created_at': now.isoformat()}
        if previous:
            entry['created_at'] = previous.get('created_at', entry['created_at'])
            if not args.until:
                entry['until'] = previous['until']
            entry['baseline_windows'] = baseline_windows if rebaseline else previous.get('baseline_windows') or baseline_windows
            if rebaseline:
                entry['baseline_repaired_at'] = now.isoformat()
            elif previous.get('baseline_repaired_at'):
                entry['baseline_repaired_at'] = previous['baseline_repaired_at']
        rows = [o for o in rows if (
            o.get('intent') or ' '.join(o.get('basis', '').split()), o.get('account'),
            account_fleet.canonical(o.get('model'), accounts) if o.get('model') else None, o.get('window')) != key]
        rows.append({k: v for k, v in entry.items() if v is not None})
        removed = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    override_store.write(path, {**document, 'v': 1, 'overrides': rows})
    ledger_row = {'v': 1, 'at': now.isoformat(), 'stage': 'override',
                  'action': 'clear' if args.clear else 'upsert', 'removed': removed}
    if not args.clear:
        ledger_row.update({'account': args.account, 'model': model, 'basis': args.basis.strip()[:400],
                           'boost': boost, 'until': until.isoformat()})
    decision_ledger.append_ledger(ledger_row)
    return {'stage': 'override', 'path': str(path), 'removed': removed, 'active': load_overrides(now, path, usage=usage, accounts=accounts, prune=False)}
