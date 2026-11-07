"""Where the provider usage cache lives, and the one way readers combine its copies.

The collector (usage_core.py) owns the cache. Its file is ``<state dir>/usage-cache.json``, normally
``~/.agent-compass/usage-cache.json``. Usage Float, an optional display plugin, used to be the only writer and
kept the cache at ``~/.usage-float/usage-cache.json``; that file is the *legacy* copy.

Rules, shared by every reader and by the collector:

* ``ROUTER_USAGE_CACHE`` (or an explicit path) names the only file that is read or written. Nothing else is consulted.
* Otherwise the collector writes the canonical file only. On its first write it seeds the canonical file from the
  legacy one, so existing history, per-credential attempt times and backoffs carry over.
* Readers look at the canonical and the legacy file and take, per provider, the observation with the newest
  ``fetched_at`` (the canonical file wins a tie). A standalone Usage Float that still writes the legacy file therefore
  cannot hide behind an older canonical reading, and an old legacy reading cannot hide a newer canonical one.
  Freshness is still judged by the consumer from ``fetched_at``; merging never makes a reading look newer.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

CACHE_ENV = 'ROUTER_USAGE_CACHE'
STATE_ENV = 'ROUTER_STATE_DIR'
CACHE_FILE = 'usage-cache.json'
MAX_BYTES = 5 * 1024 * 1024


def _environ(environ):
    return os.environ if environ is None else environ


def _home(home=None, environ=None):
    return Path(home) if home is not None else Path.home()


def _registry(name):
    """A Windows user variable set after the parent process started (the router's user_env sees these too)."""
    if os.name != 'nt':
        return ''
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
            value = winreg.QueryValueEx(key, name)[0]
    except OSError:
        return ''
    return value if isinstance(value, str) else ''


def override(environ=None):
    """The explicit cache file from ROUTER_USAGE_CACHE, or None. Without an ``environ`` the Windows user variables count too."""
    value = str(_environ(environ).get(CACHE_ENV) or '').strip()
    if not value and environ is None:
        value = _registry(CACHE_ENV).strip()
    return Path(os.path.expandvars(value)).expanduser() if value else None


def state_dir(home=None, environ=None):
    configured = str(_environ(environ).get(STATE_ENV) or '').strip()
    return Path(os.path.expandvars(configured)).expanduser() if configured else _home(home) / '.agent-compass'


def canonical_path(home=None, environ=None):
    return state_dir(home, environ) / CACHE_FILE


def legacy_path(home=None):
    return _home(home) / '.usage-float' / CACHE_FILE


def write_path(home=None, environ=None):
    return override(environ) or canonical_path(home, environ)


def read_paths(explicit=None, home=None, environ=None):
    """Files a reader consults, in priority order."""
    if explicit is not None:
        return [Path(os.path.expandvars(str(explicit))).expanduser()]
    forced = override(environ)
    if forced is not None:
        return [forced]
    return list(dict.fromkeys([canonical_path(home, environ), legacy_path(home)]))


def home_of(path):
    """Home directory a cache file implies (``~/.agent-compass/x`` and ``~/.usage-float/x`` both mean ``~``)."""
    path = Path(path)
    return path.parent.parent if path.parent.name in ('.agent-compass', '.usage-float') else path.parent


def _stamp(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def read_document(path):
    """One cache file as a dict with a non-empty ``providers`` table; raises OSError/ValueError otherwise."""
    path = Path(path)
    if path.stat().st_size > MAX_BYTES:
        raise ValueError('Oversized cache')
    document = json.loads(path.read_text(encoding='utf-8-sig'))
    rows = document.get('providers') if isinstance(document, dict) else None
    if not isinstance(rows, dict) or not rows:
        raise ValueError('Missing providers')
    return document


def merge(documents):
    """[(path, document)] in priority order -> (document, {provider: path}); newest ``fetched_at`` wins per provider."""
    if not documents:
        return {}, {}
    first_path, first = documents[0]
    merged = dict(first)
    providers = dict(first['providers'])
    polls = dict(first['poll']) if isinstance(first.get('poll'), dict) else {}
    origin = {name: str(first_path) for name in providers}
    first_at = _stamp(first.get('fetched_at'))
    for path, document in documents[1:]:
        other_polls = document.get('poll') if isinstance(document.get('poll'), dict) else {}
        for name, row in document['providers'].items():
            if not isinstance(row, dict):
                continue
            current = providers.get(name)
            seen = _stamp(row.get('fetched_at'))
            held = _stamp(current.get('fetched_at')) if isinstance(current, dict) else None
            current_error = (isinstance(current, dict) and held is None
                             and bool(current.get('error') or current.get('error_status')
                                      or current.get('error_code') or current.get('available') is False))
            if current_error:
                current_at = _stamp(current.get('error_at')) or first_at
                diff_cred = bool(row.get('credential_key') and current.get('credential_key')
                                 and row.get('credential_key') != current.get('credential_key'))
                take = diff_cred and seen is not None and (current_at is None or seen > current_at)
            else:
                take = current is None or (seen is not None and (held is None or seen > held))
            if take:
                providers[name] = row
                origin[name] = str(path)
                if name in other_polls:
                    polls[name] = other_polls[name]
                else:
                    polls.pop(name, None)
    merged['providers'] = providers
    merged['poll'] = polls
    return merged, origin


def read(explicit=None, home=None, environ=None):
    """(merged document or None, {provider: source path}, attempts). ``attempts`` lists unreadable files."""
    documents, attempts = [], []
    for path in read_paths(explicit, home, environ):
        try:
            documents.append((path, read_document(path)))
        except FileNotFoundError:
            attempts.append({'path': str(path), 'status': 'missing'})
        except (OSError, ValueError, UnicodeError):
            attempts.append({'path': str(path), 'status': 'unreadable_or_invalid'})
    if not documents:
        return None, {}, attempts
    document, origin = merge(documents)
    return document, origin, attempts


def providers(home=None, environ=None):
    """The merged provider table, or {} when no cache can be read (for display and sampling readers)."""
    document, _, _ = read(None, home, environ)
    return document['providers'] if document else {}


def seed_from_legacy(home=None, environ=None):
    """Copy the legacy cache to the canonical path when the canonical file does not exist yet.

    Never runs when ROUTER_USAGE_CACHE is set, and never touches the legacy file. Returns True when it copied.
    """
    if override(environ) is not None:
        return False
    target = canonical_path(home, environ)
    source = legacy_path(home)
    if target.exists() or not source.is_file():
        return False
    temporary = target.with_name(f'{target.name}.{uuid.uuid4().hex}.tmp')
    try:
        document = read_document(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(document, indent=2, allow_nan=False), encoding='utf-8')
        # Another process may have created the canonical file in the meantime; keep theirs.
        if target.exists():
            return False
        temporary.replace(target)
    except (OSError, ValueError, UnicodeError):
        return False
    finally:
        temporary.unlink(missing_ok=True)
    return True
