"""Durable confirmed quota failures, independent of optimistic usage caches.

Contract: an explicit quota-exhausted response blocks subsequent launches on that
account. Network errors and generic 429s do not qualify. Only a newer successful
positive quota observation or an explicit clear releases the latch; elapsed time
and estimated reset dates alone never prove recovery. No remote error text is stored.
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


def record(account, error, *, at=None, source=None, path=None):
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
        doc['accounts'][account] = {'observed_at': at, 'source': source, 'blocked': True}
        override_store.write(path, doc)
    return True


def blocked(account, usage=None, *, path=None):
    path = path or store_path()
    row = (_read(path).get('accounts') or {}).get(account) or {}
    if not row.get('blocked'):
        return False
    usage = usage or {}
    fetched = _time(usage.get('fetched_at'))
    observed = _time(row.get('observed_at'))
    windows = usage.get('windows') or []
    positive = bool(windows) and all(
        isinstance(w.get('used_pct'), (int, float)) and 0 <= w['used_pct'] < 100
        for w in windows)
    if usage.get('available') is True and not usage.get('stale') and fetched and observed and fetched > observed and positive:
        return not clear(account, path=path, expected_observed_at=row['observed_at'])
    return True


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
