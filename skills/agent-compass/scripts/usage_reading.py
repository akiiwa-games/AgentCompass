"""Read the collector's usage cache into the routing usage snapshot, and the windows of a reading that bind a model."""
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import time

import env_vars
import provider_inventory
import usage_cache
import window_scope


USAGE_CACHE_ENV = 'ROUTER_USAGE_CACHE'
METERED_NAMES_ENV = 'ROUTER_METERED_NAMES'
MAX_AGE_SECONDS = 900
FLAG_CONFLICT_SECONDS = 24 * 3600   # collector says stale=false but the observation is this old
DEFAULT_METERED_ACCOUNTS = ()    # metered/proxy accounts are discovered dynamically via billing='metered' or ROUTER_METERED_NAMES


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def finite_number(value, low=0, high=100):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and low <= value <= high)


SESSION_LIMIT_RE = re.compile(
    r'hit your (?:session )?limit\s*[·•-]\s*resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\(UTC\)',
    re.I,
)


def parse_session_limit_reset(text, hit_at):
    match = SESSION_LIMIT_RE.search(text or '')
    if not match or hit_at is None:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'pm':
        hour += 12
    minute = int(match.group(2) or 0)
    reset = hit_at.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if reset <= hit_at:
        reset += timedelta(days=1)
    if (reset - hit_at).total_seconds() > 6 * 3600:
        reset -= timedelta(days=1)
    return reset if reset > hit_at else None


def _usage_home(source_path):
    env_home = os.environ.get('ROUTER_PROVIDER_HOME')
    if not source_path:
        return Path(env_home).expanduser() if env_home else None
    path = Path(source_path).resolve()
    if path == usage_cache.canonical_path().resolve():   # the state dir may sit anywhere (ROUTER_STATE_DIR)
        return Path(env_home).expanduser() if env_home else Path.home()
    return usage_cache.home_of(path)


def claude_session_limit(account, home_dir, now, inventory=None):
    """Detect an active 5h session-limit 429 from local Claude session logs or persisted rate-limits.json."""
    if not home_dir:
        return None
    inventory = provider_inventory.discover(home=home_dir) if inventory is None else inventory
    discovered = (inventory.get('accounts') or {}).get(account) or {}
    if discovered.get('family') != 'claude' or not discovered.get('home'):
        return None
    try:
        is_real_home = home_dir.resolve() == Path.home().resolve()
    except OSError:
        is_real_home = False
    state_dir = (Path(os.environ.get('ROUTER_STATE_DIR')).expanduser()
                 if is_real_home and os.environ.get('ROUTER_STATE_DIR')
                 else home_dir / '.agent-compass')
    store_path = state_dir / 'rate-limits.json'
    persisted = {}
    if store_path.is_file():
        try:
            loaded = json.loads(store_path.read_text(encoding='utf-8-sig'))
            if isinstance(loaded, dict):
                persisted = loaded
        except (OSError, ValueError):
            persisted = {}
    latest_hit = None
    rec = persisted.get(account)
    if isinstance(rec, dict):
        hit_ts = timestamp(rec.get('hit_at'))
        reset_ts = timestamp(rec.get('resets_at'))
        if hit_ts and reset_ts and hit_ts <= now < reset_ts and (now - hit_ts).total_seconds() <= 5 * 3600:
            latest_hit = (hit_ts, reset_ts)
    proj_dir = Path(discovered['home']) / 'projects'
    if proj_dir.is_dir():
        cutoff = min(now.timestamp(), time.time()) - 5 * 3600
        try:
            candidates = []
            for p in proj_dir.glob('*/*.jsonl'):
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    continue
                if mtime >= cutoff:
                    candidates.append((mtime, p))
            candidates.sort(key=lambda x: x[0], reverse=True)
        except OSError:
            candidates = []
        for _, p in candidates[:5]:
            try:
                size = p.stat().st_size
                with p.open('rb') as handle:
                    if size > 32768:
                        handle.seek(size - 32768)
                    chunk = handle.read().decode('utf-8', errors='ignore')
            except OSError:
                continue
            for line in chunk.splitlines():
                if 'limit' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                ts = timestamp(entry.get('timestamp'))
                if ts is None or ts > now + timedelta(seconds=60):
                    continue
                reset = parse_session_limit_reset(json.dumps(entry, ensure_ascii=False), ts)
                if reset and ts <= now < reset and (now - ts).total_seconds() <= 5 * 3600:
                    if latest_hit is None or ts > latest_hit[0]:
                        latest_hit = (ts, reset)
    if latest_hit:
        hit_ts, reset_ts = latest_hit
        active = {k: v for k, v in persisted.items()
                  if isinstance(v, dict) and (r := timestamp(v.get('resets_at'))) is not None and r > now}
        new_entry = {'window': 'five_hour', 'used_pct': 100.0,
                     'hit_at': hit_ts.isoformat(), 'resets_at': reset_ts.isoformat()}
        if active.get(account) != new_entry:
            active[account] = new_entry
            try:
                state_dir.mkdir(parents=True, exist_ok=True)
                store_path.write_text(json.dumps(active, indent=2), encoding='utf-8')
            except OSError:
                pass
        return {'window': 'five_hour', 'used_pct': 100.0, 'hit_at': hit_ts, 'resets_at': reset_ts}
    return None


def window_state(window, now, observation_current=True):
    reset = timestamp(window.get('resets_at'))
    used = window.get('used_pct')
    # Providers (e.g. Magpie/Claude at 5h session limit) can report >100% utilization
    # (such as 101.0%); clamp finite values >100 to 100.0 instead of dropping to None.
    used = (100.0 if used > 100 else used) if finite_number(used, low=0, high=float('inf')) else None
    passed = reset <= now if reset else None
    # A Magpie pool of N same-plan subscriptions reports their mean usage; what is left is N accounts' worth, in
    # percent of one account (the unit of capacity_usd), so two fresh accounts are 200%.
    count = window.get('accounts')
    count = count if isinstance(count, int) and not isinstance(count, bool) and count > 1 else 1
    left = count * (100 - used) if used is not None else None
    return {
        'id': window.get('id') if isinstance(window.get('id'), str) else None,
        'used_pct': used,
        'resets_at': reset.isoformat() if reset else None,
        'seconds_until_reset': max(0, (reset - now).total_seconds()) if reset else None,
        'reset_time_passed': passed,
        'observed_remaining_pct': left,
        'remaining_pct_before_reset': left if passed is False and observation_current else None,
        **({'accounts': count} if count > 1 else {}),
        **({'rate_limited': True} if window.get('rate_limited') is True and passed is False else {}),
        **({'models': window['models']} if isinstance(window.get('models'), list) and window['models']
           and all(isinstance(model, str) and model for model in window['models']) else {}),
    }


def cache_paths(explicit=None):
    """Files the usage reading may come from: one named file (--usage-cache or ROUTER_USAGE_CACHE), else the
    collector's canonical cache (see usage_cache.py)."""
    return usage_cache.read_paths(explicit, environ={**os.environ, USAGE_CACHE_ENV: env_vars.user_env(USAGE_CACHE_ENV) or ''})


def _metered_usd_details(details):
    """Spent and budget dollars from a collector row. None when this is not that reading.

    A permission or other miss carries no spend number. Routing still sees no window.
    """
    if not isinstance(details, dict) or details.get('kind') != 'metered_usd':
        return None
    spent = details.get('spent_usd')
    if isinstance(spent, bool) or not isinstance(spent, (int, float)):
        query = details.get('query')
        if query == 'no_permission':
            return {'kind': 'metered_usd', 'query': 'no_permission'}
        read_error = details.get('read_error')
        if query == 'failed' and isinstance(read_error, str) and read_error.strip():
            return {'kind': 'metered_usd', 'query': 'failed', 'read_error': read_error.strip()}
        return None
    budget = details.get('budget_usd')
    budget_usd = float(budget) if isinstance(budget, (int, float)) and not isinstance(budget, bool) else None
    return {'kind': 'metered_usd', 'spent_usd': float(spent), 'budget_usd': budget_usd}


def usage_snapshot(path=None, accounts=None, now=None, max_age_seconds=MAX_AGE_SECONDS, quota_sources=None, inventory=None):
    """Read the real collector cache; project an allowlist, never credentials or reset grants."""
    now = now or datetime.now(timezone.utc)
    attempts = []
    providers = polls = source = None
    for candidate in cache_paths(path):
        try:
            document = usage_cache.read_document(candidate)
            providers = document['providers']
            polls = document.get('poll') if isinstance(document.get('poll'), dict) else {}
            source = str(Path(candidate).resolve())
            break
        except (OSError, ValueError, UnicodeError):
            attempts.append({'path': str(candidate), 'status': 'unreadable_or_invalid'})
    if providers is None:
        return {'status': 'unknown', 'read_ok': False, 'read_at': now.isoformat(),
                'cache_path': None, 'attempts': attempts, 'accounts': {},
                'reason': 'No readable provider usage cache; locate the collector snapshot.'}
    requested = set(providers) if accounts is None else set(accounts)
    # Accounts the fleet document pools onto another account read that account's row.
    if quota_sources is None:
        import account_fleet
        quota_sources = account_fleet.configured_quota_sources()
    sources = quota_sources
    requested |= {sources[name] for name in requested if name in sources}
    home_dir = _usage_home(source)
    if inventory is None:
        default_home = Path(os.environ.get('ROUTER_PROVIDER_HOME') or Path.home())
        inventory = provider_inventory.discover(home=None if home_dir == default_home else home_dir)
    result = {}
    for account in sorted(requested):
        source_account = sources.get(account, account)
        row = providers.get(source_account)
        if not isinstance(row, dict):
            result[account] = {'status': 'unknown', 'reason': 'account_missing', 'windows': []}
            continue
        fetched = timestamp(row.get('fetched_at'))
        age = (now - fetched).total_seconds() if fetched else None
        allowed = usage_cache.poll_max_age(polls.get(source_account), now, max_age_seconds)
        current = (age is not None and 0 <= age <= allowed
                   and row.get('stale') is not True and row.get('available') is not False)
        raw_windows = row.get('windows')
        windows = [window_state(w, now, current) for w in
                   (raw_windows if isinstance(raw_windows, list) else []) if isinstance(w, dict)]
        limit = claude_session_limit(account, home_dir, now, inventory)
        rate_limited_until = None
        if limit is not None:
            fresh_recovered = (
                current and fetched is not None and fetched > limit['hit_at'] + timedelta(seconds=30)
                and any(w.get('id') in ('five_hour', '5h') and w.get('used_pct') is not None and w['used_pct'] < 100
                        for w in windows)
            )
            if not fresh_recovered:
                rate_limited_until = limit['resets_at'].isoformat()
                limit_win = window_state({
                    'id': 'five_hour',
                    'used_pct': 100.0,
                    'resets_at': rate_limited_until,
                    'rate_limited': True,
                }, now, observation_current=True)
                replaced = False
                for idx, w in enumerate(windows):
                    if w.get('id') in ('five_hour', '5h'):
                        windows[idx] = limit_win
                        replaced = True
                        break
                if not replaced:
                    windows.append(limit_win)
        metered_usd = row.get('backend_kind') == 'metered_usd'
        # A dollar balance has no reset, so remaining_pct_before_reset stays unset.
        # The reading is still current when the used share or the spent summary is known.
        balance_known = metered_usd and (
            any(w.get('observed_remaining_pct') is not None for w in windows)
            or isinstance(row.get('summary'), str)
        )
        usable = any(w['remaining_pct_before_reset'] is not None for w in windows) or balance_known
        result[account] = {
            **({'quota_source': source_account} if source_account != account else {}),
            'status': 'fresh' if current and usable else 'stale' if fetched else 'unknown',
            'fetched_at': fetched.isoformat() if fetched else None,
            'observation_age_seconds': age,
            **({'max_age_seconds': allowed} if allowed != max_age_seconds else {}),
            'available': row.get('available') if isinstance(row.get('available'), bool) else None,
            'stale': row.get('stale') if isinstance(row.get('stale'), bool) else None,
            # The collector can keep reporting stale=false for an account it stopped refreshing.
            'flag_conflict': bool(age is not None and age > FLAG_CONFLICT_SECONDS
                                  and row.get('stale') is False),
            'windows': windows,
            'subscription_expires_at': None,
            **({'rate_limited_until': rate_limited_until} if rate_limited_until else {}),
            **({'billing': row['billing']} if isinstance(row.get('billing'), str) else {}),
            **({'backend_kind': row['backend_kind']} if isinstance(row.get('backend_kind'), str) else {}),
            **({'summary': row['summary']} if metered_usd and isinstance(row.get('summary'), str) else {}),
            **({'usage_details': _metered_usd_details(row.get('usage_details'))} if _metered_usd_details(row.get('usage_details')) else {}),
            # A Magpie pool's per-account readings, for display only (hover detail); routing uses the windows.
            **({'pool_members': details['members']} if isinstance(details := row.get('usage_details'), dict)
               and details.get('kind') == 'magpie_pool' and isinstance(details.get('members'), list) else {}),
        }
    return {
        'status': 'cached_observations', 'read_ok': True, 'read_at': now.isoformat(),
        'cache_path': source, 'attempts': attempts, 'max_age_seconds': max_age_seconds,
        'accounts': result,
        'missing_accounts': [k for k, v in result.items() if v.get('reason') == 'account_missing'],
        'interpretation': 'Freshness uses each fetched_at, collector flags and reset time. '
            'Historical usage is readable but not current capacity. A reset is not subscription expiry. '
            'Unknown or stale telemetry is not zero, unlimited quota or subscription unavailability. '
            'Provider percentages are not equal tokens. A window binds only the models its declared scope names.',
    }


def metered_names():
    """Pay-per-use accounts are a last resort, so they are never suggested as spare capacity."""
    configured = {s.strip().lower() for s in (env_vars.user_env(METERED_NAMES_ENV) or '').split(',') if s.strip()}
    return configured | set(DEFAULT_METERED_ACCOUNTS)


def window_spec(account_spec, window_id):
    """The account spec's windows.<id> for a reading's window id (provider aliases resolved), or {}."""
    windows = (account_spec or {}).get('windows') if isinstance(account_spec, dict) else None
    if not isinstance(windows, dict) or not window_id:
        return {}
    import quota_pools
    spec = windows.get(quota_pools.canonical_window_id(account_spec, window_id))
    return spec if isinstance(spec, dict) else {}


def binding_windows(row, model='', account_spec=None):
    """The reading's windows that bind this model, by their data-declared scope (window_scope)."""
    return [w for w in row.get('windows') or []
            if window_scope.binds(model, w, window_spec(account_spec, w.get('id')))]


def applicable_windows(row, model='', account_spec=None):
    """Current windows that bind this model: binding_windows with a remaining percentage before reset."""
    return [w for w in binding_windows(row, model, account_spec) if w.get('remaining_pct_before_reset') is not None]


def tightest_window(row, model='', account_spec=None):
    return min(applicable_windows(row, model, account_spec), key=lambda w: w['remaining_pct_before_reset'], default=None)
