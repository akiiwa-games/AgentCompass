"""Antigravity subscription tier, read from Google the way agy itself learns it.

`agy -p /usage` reports only bucket fractions; the tier that sizes those buckets arrives when agy calls
cloudcode's loadCodeAssist at start-up and is never written to disk. This module makes the same call with
agy's own signed-in token (the OS keyring entry service "gemini", user "antigravity") and keeps the answer
in <state dir>/antigravity-plan.json. Discovery reads only that file; routing commands refresh it beside the
run (refresh_in_background), never on the routing thread. Nothing is stored except the tier id and name.

An expired token (agy renews it on its next start) or a failed request leaves the last good answer in place and is
retried after RETRY_MINUTES, but that answer stops counting once it is PLAN_MAX_AGE_HOURS old: an old tier is no longer
evidence of the tier. A lost sign-in drops it at once. A tier with no paid plan behind it (`free-tier`) reads as `Free`, whichever response field
carried it.
"""
from datetime import datetime, timedelta, timezone
import atexit
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import urllib.error
import urllib.request

ENDPOINT = 'https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist'
KEYRING_SERVICE, KEYRING_USER = 'gemini', 'antigravity'
CACHE_FILE = 'antigravity-plan.json'
INTERVAL_HOURS = 12
PLAN_MAX_AGE_HOURS = 48
RETRY_MINUTES = 30
TIMEOUT_SECONDS = 8
EXIT_GRACE_SECONDS = 2
# Tier ids with no paid Google AI plan behind them. `standard-tier` is not here: it can be a paid Code Assist plan.
FREE_TIER_IDS = frozenset(('free-tier',))
PLATFORMS = {'win32': 'WINDOWS_AMD64', 'darwin': 'DARWIN_ARM64', 'linux': 'LINUX_AMD64'}


def _env(environ):
    return os.environ if environ is None else environ


def _now():
    return datetime.now(timezone.utc)


def _stamp(moment):
    return moment.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _parse(value):
    try:
        moment = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def cache_path(state_dir):
    return Path(state_dir) / CACHE_FILE


def read_cache(state_dir):
    try:
        document = json.loads(cache_path(state_dir).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


def cached_plan(state_dir, now=None):
    """The tier name last read from Google ("Google AI Ultra"), or None when there is none or it has expired."""
    document = read_cache(state_dir)
    plan = document.get('plan')
    read_at = _parse(document.get('read_at'))
    if not isinstance(plan, str) or not plan or read_at is None:
        return None
    if (now or _now()) - read_at >= timedelta(hours=PLAN_MAX_AGE_HOURS):
        return None
    # The tier id decides, whichever field carried it: Google returns `free-tier` under `paidTier` too
    # ("Antigravity Starter Quota", seen 2026-11-10), and that is still no paid plan.
    return 'Free' if document.get('tier_id') in FREE_TIER_IDS else plan


def _write_cache(state_dir, document):
    path = cache_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(document, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def _keyring_blob():
    """agy's go-keyring entry as text, or None."""
    if sys.platform == 'win32':
        import ctypes
        from ctypes import wintypes as w

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [('Flags', w.DWORD), ('Type', w.DWORD), ('TargetName', w.LPWSTR), ('Comment', w.LPWSTR),
                        ('LastWritten', w.FILETIME), ('CredentialBlobSize', w.DWORD),
                        ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)), ('Persist', w.DWORD),
                        ('AttributeCount', w.DWORD), ('Attributes', ctypes.c_void_p), ('TargetAlias', w.LPWSTR),
                        ('UserName', w.LPWSTR)]
        advapi = ctypes.WinDLL('advapi32')
        pointer = ctypes.POINTER(CREDENTIAL)()
        if not advapi.CredReadW(f'{KEYRING_SERVICE}:{KEYRING_USER}', 1, 0, ctypes.byref(pointer)):
            return None
        try:
            entry = pointer.contents
            return bytes(entry.CredentialBlob[:entry.CredentialBlobSize]).decode('utf-8', 'replace')
        finally:
            advapi.CredFree(pointer)
    command = (['security', 'find-generic-password', '-s', KEYRING_SERVICE, '-a', KEYRING_USER, '-w']
               if sys.platform == 'darwin' else
               ['secret-tool', 'lookup', 'service', KEYRING_SERVICE, 'username', KEYRING_USER])
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    text = done.stdout.strip() if done.returncode == 0 else ''
    if text.startswith('go-keyring-base64:'):
        text = base64.b64decode(text.split(':', 1)[1]).decode('utf-8', 'replace')
    return text or None


def read_token(read_blob=None):
    """agy's access token and its expiry, or (None, reason)."""
    try:
        blob = (read_blob or _keyring_blob)()
    except Exception:
        blob = None
    if not blob:
        return None, 'agy_not_signed_in'
    try:
        token = json.loads(blob).get('token') or {}
    except (ValueError, AttributeError):
        return None, 'agy_keyring_unreadable'
    access = token.get('access_token')
    if not isinstance(access, str) or not access:
        return None, 'agy_not_signed_in'
    expiry = _parse(token.get('expiry'))
    if expiry and expiry <= _now() + timedelta(seconds=30):
        return None, 'agy_token_expired'
    return access, None


def load_code_assist(access_token, timeout=TIMEOUT_SECONDS):
    body = {'metadata': {'ideType': 'ANTIGRAVITY', 'pluginType': 'GEMINI',
                         'platform': PLATFORMS.get(sys.platform, 'PLATFORM_UNSPECIFIED')}}
    request = urllib.request.Request(ENDPOINT, data=json.dumps(body).encode(), method='POST', headers={
        'Authorization': f'Bearer {access_token}', 'Content-Type': 'application/json', 'User-Agent': 'antigravity'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def tier_from(response):
    """The paid tier when there is one (paidTier carries the Google AI plan), else the current tier."""
    for key in ('paidTier', 'currentTier'):
        tier = response.get(key) if isinstance(response, dict) else None
        if isinstance(tier, dict) and (tier.get('name') or tier.get('id')):
            return {'tier_id': tier.get('id'), 'plan': tier.get('name') or tier.get('id'), 'source': key}
    return None


def due(document, now=None):
    now = now or _now()
    checked = _parse(document.get('checked_at'))
    if checked is None:
        return True
    wait = timedelta(hours=INTERVAL_HOURS) if document.get('plan') and not document.get('error') \
        else timedelta(minutes=RETRY_MINUTES)
    return now - checked >= wait


def refresh(state_dir, read_blob=None, fetch=None, now=None, timeout=TIMEOUT_SECONDS):
    """Ask Google for the tier and update the cache; a failure keeps the previous plan. Returns the cache."""
    now = now or _now()
    previous = read_cache(state_dir)
    keep = {k: previous[k] for k in ('plan', 'tier_id', 'source', 'read_at') if k in previous}
    document = {**keep, 'checked_at': _stamp(now)}
    token, reason = read_token(read_blob)
    if reason == 'agy_not_signed_in':
        document = {'checked_at': _stamp(now)}
    if token is None:
        document['error'] = reason
    else:
        try:
            tier = tier_from((fetch or load_code_assist)(token, timeout))
        except urllib.error.HTTPError as error:
            tier, document['error'] = None, f'http_{error.code}'
        except Exception as error:
            tier, document['error'] = None, type(error).__name__
        else:
            if tier is None:
                document['error'] = 'no_tier_in_response'
        if tier:
            document.update(tier, read_at=_stamp(now))
            document.pop('error', None)
    _write_cache(state_dir, document)
    return document


def refresh_in_background(state_dir, environ=None):
    """Start a daemon refresh when one is due and agy is installed. Returns the thread or None."""
    import shutil
    if str(_env(environ).get('AGENTCOMPASS_ANTIGRAVITY_PLAN') or '').lower() in ('0', 'off', 'false', 'no'):
        return None
    if not shutil.which('agy', path=_env(environ).get('PATH')) or not due(read_cache(state_dir)):
        return None

    def work():
        try:
            refresh(state_dir)
        except Exception:
            pass

    thread = threading.Thread(target=work, name='agentcompass-antigravity-plan', daemon=True)
    thread.start()
    atexit.register(thread.join, EXIT_GRACE_SECONDS)
    return thread


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from public_data import state_dir as _state_dir
    print(json.dumps(refresh(_state_dir()), indent=2))
