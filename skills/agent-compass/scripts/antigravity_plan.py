"""Antigravity subscription tier, read from Google the way agy itself learns it.

`agy -p /usage` reports only bucket fractions; the tier that sizes those buckets arrives when agy calls
cloudcode's loadCodeAssist at start-up and is never written to disk. This module makes the same call with
agy's own signed-in token (the OS keyring entry service "gemini", user "antigravity") and keeps the answer
in <state dir>/antigravity-plan.json. Discovery reads only that file; routing commands refresh it beside the
run (refresh_in_background), never on the routing thread. Nothing is stored except the tier id and name.

An expired token (agy renews it on its next start) or a failed request leaves the last good answer in place and is
retried after RETRY_MINUTES, but that answer stops counting once it is PLAN_MAX_AGE_HOURS old: an old tier is no longer
evidence of the tier. A lost sign-in drops it at once. A tier with no paid plan behind it (`free-tier`) reads as `Free`,
whichever response field carried it.

agy keeps one keyring slot, and every long-lived agy process writes its own account's refreshed token back into it, so
after a re-login the slot can flip between two Google accounts every hour (round 059, agy-account-switching-report.md:
the old account's processes refresh at :38, the new one's at :49-:52). The cache therefore records whose tier it holds:
a digest of the keyring account (the id_token `email` claim, sha256; never the token or the address), and the
identities read within one token lifetime. The lifetime is the token's own: the id_token's `exp - iat`, else the
interval between two successive writes of the same account's token (their `expiry` values); a keyring whose lifetime
cannot be read is reported as the data error `agy_token_lifetime_unreadable`, never guessed. A different account in the
slot than the cached tier's is re-read at once. Two accounts within one token lifetime is `flapping`: every agy process
rewrites the slot at least once per lifetime, so both accounts are still live and the tier belongs to neither for sure;
the plan reads as unknown (provider_inventory reports `agy_account_flapping` with the cause and the fix). A `free-tier`
answer counts only once the same account answers it again with a renewed token (another `expiry`), so one reading
inside a flip never latches an account inactive.
"""
from datetime import datetime, timedelta, timezone
import atexit
import base64
import hashlib
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
# A storage bound on the keyring readings kept, not a decision threshold: the window is the token lifetime.
MAX_OBSERVATIONS = 32
LIFETIME_UNREADABLE = 'agy_token_lifetime_unreadable'
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


def _within(observations, now, seconds):
    return [o for o in observations or () if isinstance(o, dict) and _parse(o.get('at'))
            and now - _parse(o['at']) <= timedelta(seconds=seconds)]


def flapping(document, now=None):
    """{'accounts': n, 'window_minutes', 'since'} when the keyring held more than one account within one token lifetime,
    else None (also when the lifetime is unreadable: that is the data error `lifetime_error`, not a verdict)."""
    now = now or _now()
    lifetime = document.get('lifetime_seconds')
    if not isinstance(lifetime, (int, float)) or lifetime <= 0:
        return None
    seen = [o for o in _within(document.get('observations'), now, lifetime) if o.get('identity')]
    accounts = {o['identity'] for o in seen}
    if len(accounts) < 2:
        return None
    return {'accounts': len(accounts), 'window_minutes': round(lifetime / 60, 1), 'since': min(o['at'] for o in seen)}


def free_confirmed(document):
    """A `free-tier` answer counts once the same account gave it again with a renewed token (module docstring)."""
    return document.get('tier_id') not in FREE_TIER_IDS or bool(document.get('free_confirmed'))


def cached_state(state_dir, now=None):
    """{'plan': cached_plan, 'flapping': flapping(...) or None, 'data_error': a lifetime error or None} from the cache."""
    document = read_cache(state_dir)
    return {'plan': cached_plan(state_dir, now, document), 'flapping': flapping(document, now),
            'data_error': document.get('lifetime_error')}


def cached_plan(state_dir, now=None, document=None):
    """The tier name last read from Google ("Google AI Ultra"), or None when there is none, it has expired, the keyring
    account is flapping, or a free answer is not yet confirmed (module docstring)."""
    document = read_cache(state_dir) if document is None else document
    if flapping(document, now) or not free_confirmed(document):
        return None
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


def _blob(read_blob=None):
    try:
        return (read_blob or _keyring_blob)()
    except Exception:
        return None


def identity_of(blob):
    """sha256 digest of the keyring account's email (the id_token `email` claim, or agy's own `id_token_email`), or None.
    The address itself is never kept."""
    try:
        document = json.loads(blob)
    except (TypeError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    token = document.get('token') if isinstance(document.get('token'), dict) else {}
    email = (document.get('id_token_email') or token.get('id_token_email')
             or _claims(token.get('id_token') or document.get('id_token')).get('email'))
    if not isinstance(email, str) or not email.strip():
        return None
    return 'sha256:' + hashlib.sha256(email.strip().lower().encode('utf-8')).hexdigest()[:16]


def _token(blob):
    try:
        document = json.loads(blob)
    except (TypeError, ValueError):
        return {}
    token = document.get('token') if isinstance(document, dict) else None
    return token if isinstance(token, dict) else {}


def _claims(id_token):
    if not isinstance(id_token, str) or id_token.count('.') != 2:
        return {}
    part = id_token.split('.')[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
    except (ValueError, TypeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def reading_of(blob):
    """{'identity', 'token_expiry', 'lifetime_seconds'} of one keyring blob, from local data only. The expiry tells one
    token from its renewal; the lifetime is the id_token's `exp - iat` when it carries both, else None."""
    token = _token(blob) if blob else {}
    claims = _claims(token.get('id_token'))
    issued, expires = claims.get('iat'), claims.get('exp')
    lifetime = expires - issued if all(isinstance(v, (int, float)) for v in (issued, expires)) and expires > issued else None
    expiry = _parse(token.get('expiry'))
    return {'identity': identity_of(blob) if blob else None, 'token_expiry': _stamp(expiry) if expiry else None,
            'lifetime_seconds': lifetime}


def observed_lifetime(observations, identity):
    """Seconds between two successive tokens of one account (their expiry values), or None: agy writes a renewed token
    each lifetime, so the step between expiries is the interval between keyring writes."""
    expiries = sorted({_parse(o.get('token_expiry')) for o in observations or ()
                       if isinstance(o, dict) and o.get('identity') == identity and _parse(o.get('token_expiry'))})
    steps = [(b - a).total_seconds() for a, b in zip(expiries, expiries[1:])]
    return steps[-1] if steps else None


def current_reading(read_blob=None):
    """reading_of the blob now in agy's keyring slot (a local read, no network)."""
    return reading_of(_blob(read_blob))


def read_token(read_blob=None, blob=None):
    """agy's access token and its expiry, or (None, reason)."""
    blob = _blob(read_blob) if blob is None else blob
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


def due(document, now=None, reading=None):
    """A refresh is due on the interval; at once when another account is in the keyring slot than the cached tier's;
    and, while the account flaps or a free answer awaits its confirmation, as soon as the token has been renewed."""
    now = now or _now()
    checked = _parse(document.get('checked_at'))
    if checked is None:
        return True
    reading = reading or {}
    if reading.get('identity') is not None and reading['identity'] != document.get('identity'):
        return True
    unsettled = flapping(document, now) or not free_confirmed(document)
    if unsettled and reading.get('token_expiry') and reading['token_expiry'] != document.get('token_expiry'):
        return True
    if unsettled:
        wait = timedelta(minutes=RETRY_MINUTES)
    elif document.get('plan') and not document.get('error'):
        wait = timedelta(hours=INTERVAL_HOURS)
    else:
        wait = timedelta(minutes=RETRY_MINUTES)
    return now - checked >= wait


def refresh(state_dir, read_blob=None, fetch=None, now=None, timeout=TIMEOUT_SECONDS):
    """Ask Google for the tier and update the cache; a failure keeps the previous plan. Returns the cache."""
    now = now or _now()
    previous = read_cache(state_dir)
    blob = _blob(read_blob)
    reading = reading_of(blob)
    identity = reading['identity']
    # The previous tier stays only while the same account is in the slot: another account's tier is not this one's.
    same = identity == previous.get('identity')
    keep = {k: previous[k] for k in ('plan', 'tier_id', 'source', 'read_at', 'free_token', 'free_confirmed')
            if k in previous} if same else {}
    seen = [o for o in previous.get('observations') or () if isinstance(o, dict) and _parse(o.get('at'))]
    if identity:
        seen.append({'identity': identity, 'at': _stamp(now), 'token_expiry': reading['token_expiry']})
    lifetime = reading['lifetime_seconds'] or (observed_lifetime(seen, identity) if identity else None)
    if lifetime:
        seen = _within(seen, now, lifetime)
    document = {**keep, 'checked_at': _stamp(now), 'identity': identity, 'token_expiry': reading['token_expiry'],
                'observations': seen[-MAX_OBSERVATIONS:]}
    if lifetime:
        document['lifetime_seconds'] = lifetime
    elif identity:
        document['lifetime_error'] = (f'{LIFETIME_UNREADABLE}: the keyring token has no id_token iat/exp and no '
                                      'renewal has been seen yet, so account flapping cannot be judged')
    token, reason = read_token(blob=blob or '')
    if reason == 'agy_not_signed_in':
        document = {'checked_at': _stamp(now), 'observations': seen[-MAX_OBSERVATIONS:]}
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
            first = previous.get('free_token') if same and previous.get('tier_id') in FREE_TIER_IDS else None
            document.update(tier, read_at=_stamp(now))
            document.pop('error', None)
            if tier.get('tier_id') in FREE_TIER_IDS:
                # Confirmed by a second free answer on a renewed token of the same account.
                document['free_token'] = first or reading['token_expiry']
                document['free_confirmed'] = bool(document.get('free_confirmed') or (
                    first and reading['token_expiry'] and reading['token_expiry'] != first))
            else:
                document.pop('free_token', None)
                document.pop('free_confirmed', None)
    _write_cache(state_dir, document)
    return document


def refresh_in_background(state_dir, environ=None):
    """Start a daemon refresh when one is due and agy is installed. Returns the thread or None."""
    import shutil
    if str(_env(environ).get('AGENTCOMPASS_ANTIGRAVITY_PLAN') or '').lower() in ('0', 'off', 'false', 'no'):
        return None
    if not shutil.which('agy', path=_env(environ).get('PATH')):
        return None
    if not due(read_cache(state_dir), reading=current_reading()):
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
