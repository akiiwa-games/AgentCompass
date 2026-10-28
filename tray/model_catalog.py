"""Cached, read-only model discovery. Listing models never creates a routing bundle."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import threading
import time
import urllib.request
import urllib.parse

import owned_process

TTL_SECONDS = 600
QUERY_TIMEOUT = 25
_refresh_lock = threading.Lock()


def read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def query_proxy(spec: dict) -> list[dict]:
    """GET only the configured proxy's models endpoint; never forward credentials on redirects."""
    sub = spec.get('litellm') or spec
    base = str(sub.get('endpoint') or sub.get('base_url') or spec.get('endpoint') or spec.get('base_url') or '').rstrip('/')
    for suffix in ('/v1/chat/completions', '/v1/responses', '/v1/messages', '/v1'):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
            break
    if urllib.parse.urlsplit(base).scheme not in ('http', 'https'):
        raise ValueError('Unsupported proxy endpoint')
    env_key = sub.get('api_key_env') or spec.get('api_key_env')
    key = sub.get('api_key') or spec.get('api_key') or (os.environ.get(env_key) if env_key else None)
    headers = {'Accept': 'application/json'}
    if key:
        headers['Authorization'] = f'Bearer {key}'
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, message, headers, new_url):
            return None
    opener = urllib.request.build_opener(NoRedirect())
    request = urllib.request.Request(base + '/v1/models', headers=headers, method='GET')
    with opener.open(request, timeout=8) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('Model list too large')
    document = json.loads(raw)
    rows = document.get('data') if isinstance(document, dict) else None
    if not isinstance(rows, list):
        raise ValueError('Invalid proxy model list')
    return [{'id': row['id'], 'label': row.get('name') or row['id']} for row in rows
            if isinstance(row, dict) and isinstance(row.get('id'), str)]


def query_paseo(spec: dict) -> list[dict]:
    provider = spec['provider']
    cli = os.environ.get('PASEO_CLI') or shutil.which('paseo')
    if not cli and os.name == 'nt':
        candidate = Path.home() / 'AppData/Local/Programs/Paseo/resources/bin/paseo.cmd'
        cli = str(candidate) if candidate.is_file() else None
    if not cli:
        raise RuntimeError('Paseo CLI unavailable')
    result = owned_process.run([cli, 'provider', 'models', provider, '--json'], timeout=QUERY_TIMEOUT)
    if result.returncode:
        raise RuntimeError('Model discovery unavailable')
    value = json.loads(result.stdout)
    rows = value if isinstance(value, list) else value.get('models')
    if not isinstance(rows, list):
        raise ValueError('Invalid model discovery result')
    return [{'id': row['id'], 'label': row.get('label') or row.get('model') or row['id']}
            for row in rows if isinstance(row, dict) and isinstance(row.get('id'), str)]


PROBE_TYPES = {'paseo_provider': query_paseo, 'openai_compatible': query_proxy,
               'static': lambda spec: spec.get('configured_models') or []}
PASEO_HARNESSES = {'claude', 'codex', 'pi', 'opencode', 'grok', 'antigravity-resilient'}


def probe_definition(provider: str, definition: dict | None = None) -> dict:
    spec = {**(definition or {}), 'provider': provider}
    declared = spec.get('model_probe')
    if isinstance(declared, dict):
        spec.update(declared)
    elif isinstance(declared, str):
        spec['type'] = declared
    sub = spec.get('litellm') if isinstance(spec.get('litellm'), dict) else {}
    endpoint = spec.get('endpoint') or spec.get('base_url') or sub.get('endpoint') or sub.get('base_url')
    spec.setdefault('type', 'openai_compatible' if endpoint else
                    'paseo_provider' if spec.get('harness', provider) in PASEO_HARNESSES else 'static')
    if not isinstance(spec['type'], str) or spec['type'] not in PROBE_TYPES:
        spec['type'] = 'unsupported'
    return spec


def query(provider: str, *, definition: dict | None = None) -> list[dict]:
    spec = probe_definition(provider, definition)
    return PROBE_TYPES[spec['type']](spec)


def refresh(providers: list[str], path: Path, *, now: float | None = None, definitions: dict | None = None, force=False) -> dict:
    """Call from a background job only. Failures keep prior metadata and retry after the TTL."""
    now = time.time() if now is None else now
    if not _refresh_lock.acquire(blocking=False):
        return read(path)
    try:
        document = read(path)
        entries = document.setdefault('providers', {})
        due = [provider for provider in dict.fromkeys(providers)
               if force or now - (entries.get(provider, {}).get('attempted_at') or 0) >= TTL_SECONDS
               or entries.get(provider, {}).get('type') != probe_definition(provider, (definitions or {}).get(provider))['type']]
        def fetch(provider):
            try:
                definition = (definitions or {}).get(provider)
                models = query(provider, definition=definition) if definition else query(provider)
                kind = probe_definition(provider, definition)['type']
                return provider, {'models': models, 'ok': True, 'attempted_at': now, 'checked_at': now, 'type': kind}
            except (RuntimeError, OSError, ValueError, KeyError, TypeError, owned_process.OwnedProcessError):
                return provider, {**entries.get(provider, {}), 'ok': False, 'attempted_at': now,
                                  'type': probe_definition(provider, (definitions or {}).get(provider))['type']}
        # At most three bounded, owned read-only processes. No dispatch or model calls.
        with ThreadPoolExecutor(max_workers=3) as pool:
            for provider, value in pool.map(fetch, due):
                entries[provider] = value
        if due:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f'{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
            temporary.write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')
            temporary.replace(path)
        return document
    finally:
        _refresh_lock.release()
