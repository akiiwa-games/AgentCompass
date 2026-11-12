"""Where the provider usage cache lives, and how readers access it.

The collector (usage_core.py) owns ``<state dir>/usage-cache.json``, normally ``~/.agent-compass/usage-cache.json``.
``ROUTER_USAGE_CACHE`` (or an explicit path) overrides the single file read or written.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime
from pathlib import Path

CACHE_ENV = 'ROUTER_USAGE_CACHE'
STATE_ENV = 'ROUTER_STATE_DIR'
CACHE_FILE = 'usage-cache.json'
MAX_BYTES = 5 * 1024 * 1024
# The collector reads an idle account only every 30 minutes; while its per-account cadence decisions keep arriving,
# a reading counts as current for that cadence plus one collector tick of grace.
POLL_GRACE_SECONDS = 600
POLL_MAX_SECONDS = 3600


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


def write_path(home=None, environ=None):
    return override(environ) or canonical_path(home, environ)


def read_paths(explicit=None, home=None, environ=None):
    """The single cache file a reader consults."""
    if explicit is not None:
        return [Path(os.path.expandvars(str(explicit))).expanduser()]
    forced = override(environ)
    if forced is not None:
        return [forced]
    return [canonical_path(home, environ)]


def home_of(path):
    """Home directory a cache file implies (``~/.agent-compass/x`` means ``~``)."""
    path = Path(path)
    return path.parent.parent if path.parent.name == '.agent-compass' else path.parent


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


def read(explicit=None, home=None, environ=None):
    """(document or None, {provider: source path}, attempts). ``attempts`` lists missing or unreadable files."""
    path = read_paths(explicit, home, environ)[0]
    try:
        document = read_document(path)
    except FileNotFoundError:
        return None, {}, [{'path': str(path), 'status': 'missing'}]
    except (OSError, ValueError, UnicodeError):
        return None, {}, [{'path': str(path), 'status': 'unreadable_or_invalid'}]
    origin = {name: str(path) for name in document['providers']}
    return document, origin, []


def providers(home=None, environ=None):
    """The provider table, or {} when no cache can be read (for display and sampling readers)."""
    document, _, _ = read(None, home, environ)
    return document['providers'] if document else {}


def poll_max_age(poll, now, default):
    """Freshness limit for one account: its published cadence plus grace, while that decision is itself recent."""
    if not isinstance(poll, dict):
        return default
    seconds, at = poll.get('seconds'), poll.get('at')
    now_ts = now.timestamp() if isinstance(now, datetime) else float(now)
    if (not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not math.isfinite(seconds)
            or not 0 < seconds <= POLL_MAX_SECONDS
            or not isinstance(at, (int, float)) or isinstance(at, bool) or not math.isfinite(at)):
        return default
    if not 0 <= now_ts - at <= default:     # the collector stopped deciding: fall back
        return default
    return max(default, seconds + POLL_GRACE_SECONDS)
