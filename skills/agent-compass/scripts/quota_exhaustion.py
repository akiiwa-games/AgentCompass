"""Durable confirmed quota failures, independent of optimistic usage caches.

Contract: an explicit quota-exhausted response latches the quota windows the failing
model draws (the whole account when they are unknown); other windows stay usable.
Network errors and generic 429s do not qualify. A latched window is released by a
successful reading newer than the latch that shows it under 100% used, by that
reading's reset time passing, or by an explicit clear; an account-wide latch narrows
to the windows such a reading shows exhausted. Elapsed time and estimated reset dates
alone never prove recovery. No remote error text is stored.
"""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import override_store


def store_path():
    return Path(os.environ.get('ROUTER_STATE_DIR') or Path.home() / '.agent-compass') / 'quota-exhaustion.json'


def _read(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {'accounts': {}}


def _time(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, AttributeError):
        return None


def record(account, error, *, windows=None, at=None, source=None, path=None):
    if not isinstance(error, str) or not re.search(
        r'individual quota reached|quota[_ ](?:exceeded|exhausted)|insufficient_quota|'
        r'(?:usage|credit|spending|weekly|monthly) limit (?:reached|exceeded)|'
        r'usage_limit_reached|insufficient credits|额度(?:已)?(?:耗尽|用尽)', error, re.I
    ):
        return False
    path = path or store_path()
    at = at or datetime.now(timezone.utc).isoformat()
    with override_store.locked(path):
        doc = _read(path)
        previous = doc.setdefault('accounts', {}).get(account) or {}
        # Replaying an old observation must not resurrect a manually cleared latch.
        if (_time(previous.get('observed_at')) or datetime.min.replace(tzinfo=timezone.utc)) >= _time(at):
            return False
        entry = {'observed_at': at, 'source': source, 'blocked': True}
        # Windows accumulate while latched; unknown windows latch the whole account.
        if windows and not (previous.get('blocked') and not previous.get('windows')):
            entry['windows'] = sorted(set(windows) | set(previous.get('windows') or [] if previous.get('blocked') else []))
        doc['accounts'][account] = entry
        override_store.write(path, doc)
    return True


ALL = '*'


def exhausted_windows(account, usage=None, *, path=None, now=None):
    """Window ids still latched for this account; {ALL} for an account-wide latch, empty when free."""
    path = path or store_path()
    row = (_read(path).get('accounts') or {}).get(account) or {}
    if not row.get('blocked'):
        return set()
    latched = set(row.get('windows') or []) or {ALL}
    usage = usage or {}
    now = now or datetime.now(timezone.utc)
    fetched = _time(usage.get('fetched_at'))
    observed = _time(row.get('observed_at'))
    readings = {w['id']: w for w in usage.get('windows') or [] if isinstance(w, dict) and isinstance(w.get('id'), str)}
    remaining = latched
    if usage.get('available') is True and not usage.get('stale') and fetched and observed and fetched > observed and readings:
        exhausted = set()
        for wid, window in readings.items():
            used = window.get('used_pct')
            reset = _time(window.get('resets_at'))
            if isinstance(used, (int, float)) and 0 <= used < 100 or reset is not None and reset <= now:
                continue
            exhausted.add(wid)
        remaining = exhausted if ALL in latched else (latched & exhausted) | (latched - set(readings))
    if remaining == latched:
        return latched
    if not remaining:
        return set() if clear(account, path=path, expected_observed_at=row['observed_at']) else latched
    with override_store.locked(path):
        doc = _read(path)
        current = doc.setdefault('accounts', {}).get(account) or {}
        if current.get('observed_at') != row['observed_at']:
            return latched  # A concurrent, newer failure outranks this recovery observation.
        current['windows'] = sorted(remaining)
        override_store.write(path, doc)
    return remaining


def blocked(account, usage=None, *, path=None, windows=None, now=None):
    """True when the latch covers the whole account, or any of `windows` (the ids a launch would draw)."""
    latched = exhausted_windows(account, usage, path=path, now=now)
    return ALL in latched or bool(windows is not None and latched & set(windows))


def clear(account, *, path=None, expected_observed_at=None):
    path = path or store_path()
    with override_store.locked(path):
        doc = _read(path)
        row = doc.setdefault('accounts', {}).setdefault(account, {})
        if expected_observed_at is not None and row.get('observed_at') != expected_observed_at:
            return False  # A concurrent, newer failure outranks this recovery observation.
        row.update(blocked=False, observed_at=datetime.now(timezone.utc).isoformat())
        override_store.write(path, doc)
        return True


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--clear', metavar='ACCOUNT', help='Explicitly clear one confirmed quota failure')
    args = parser.parse_args()
    if args.clear:
        clear(args.clear)
    print(json.dumps(_read(store_path()), ensure_ascii=False, indent=2))
