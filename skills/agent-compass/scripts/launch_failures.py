"""Short cooldowns after the host definitively refused to create a worker.

Contract: a definitive Paseo create failure (`host_creation_failed`) cools down what failed, so the next
roll does not pick it again a few seconds later. A failure class that means the provider itself never came
up (`provider_refresh_timeout`, `provider_unavailable`) cools the whole account; any other class cools only
that account's model. The first failure cools for COOLDOWN_MINUTES; each consecutive failure doubles it up
to MAX_COOLDOWN_MINUTES. A worker that later starts on the account clears its entries. Uncertain outcomes
(timeouts, lost replies) never record here: the worker may exist. No remote error text is stored.
"""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import override_store

COOLDOWN_MINUTES = 15
MAX_COOLDOWN_MINUTES = 120
ACCOUNT_WIDE = ('provider_refresh_timeout', 'provider_unavailable')


def enabled():
    return os.environ.get('ROUTER_LAUNCH_FAILURES', '').strip().lower() != 'off'


def store_path():
    override = os.environ.get('ROUTER_LAUNCH_FAILURES', '').strip()
    if override and override.lower() != 'off':
        return Path(override)
    return Path(os.environ.get('ROUTER_STATE_DIR') or Path.home() / '.agent-compass') / 'launch-failures.json'


def _read(path):
    import json
    try:
        doc = json.loads(path.read_text(encoding='utf-8'))
        return doc if isinstance(doc, dict) and isinstance(doc.get('entries'), dict) else {'entries': {}}
    except (FileNotFoundError, ValueError):
        return {'entries': {}}


def _time(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def key(account, model=None):
    return f'{account}' if model is None else f'{account}/{model}'


def record(bundle, failure_class, *, now=None, path=None):
    account = (bundle or {}).get('account')
    if not enabled() or not isinstance(account, str) or not account:
        return None
    now = now or datetime.now(timezone.utc)
    path = path or store_path()
    k = key(account) if failure_class in ACCOUNT_WIDE else key(account, bundle.get('model'))
    with override_store.locked(path):
        doc = _read(path)
        previous = doc['entries'].get(k) or {}
        until = _time(previous.get('until'))
        # Consecutive: no start cleared the previous entry, and it ended less than MAX_COOLDOWN_MINUTES ago.
        recent = until is not None and until > now - timedelta(minutes=MAX_COOLDOWN_MINUTES)
        streak = int(previous.get('streak') or 0) + 1 if recent else 1
        minutes = min(MAX_COOLDOWN_MINUTES, COOLDOWN_MINUTES * 2 ** (streak - 1))
        start = max(now, until) if until and until > now and streak > 1 else now
        entry = {'failure_class': failure_class, 'failed_at': now.isoformat(), 'streak': streak,
                 'until': (start + timedelta(minutes=minutes)).isoformat()}
        doc['entries'][k] = entry
        override_store.write(path, doc)
    return entry


def active(*, now=None, path=None):
    """Entries still cooling down, keyed by `account` or `account/model`."""
    now = now or datetime.now(timezone.utc)
    path = path or store_path()
    if not path.exists():
        return {}
    return {k: v for k, v in _read(path)['entries'].items()
            if isinstance(v, dict) and (_time(v.get('until')) or now) > now}


def reason(bundle, entries, now=None):
    """Exclusion text for a bundle under an active cooldown, else None."""
    if not entries:
        return None
    now = now or datetime.now(timezone.utc)
    account = bundle.get('account')
    for k in (key(account), key(account, bundle.get('model'))):
        entry = entries.get(k)
        if entry:
            until = _time(entry.get('until'))
            left = max(1, round((until - now).total_seconds() / 60)) if until else COOLDOWN_MINUTES
            scope = 'account' if k == key(account) else 'model'
            return (f"host refused to create a worker on this {scope} ({entry.get('failure_class') or 'unknown'}); "
                    f"cooling down {left} more min, then reroute")
    return None


def clear(account, *, path=None):
    """A worker started on this account: drop its cooldowns. No write when there is nothing to drop."""
    path = path or store_path()
    if not enabled() or not isinstance(account, str) or not path.exists():
        return False
    with override_store.locked(path):
        doc = _read(path)
        keys = [k for k in doc['entries'] if k == account or k.startswith(account + '/')]
        if not keys:
            return False
        for k in keys:
            doc['entries'].pop(k)
        override_store.write(path, doc)
    return True
