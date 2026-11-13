"""Public reference data channel: keep the shipped references current between releases.

Every install ships accounts.json, plan-templates.json, classifier-cases.json, five-axis-scores.json and
user-scores.json under references/, with references/public-data.json listing their sha256, the data schema and when
the set was published. This module fetches that manifest and the files from the repository's main branch over plain
HTTPS (no credentials, proxies from the environment) into <state dir>/public-data/, verifies every hash, and makes the
router read the fetched set instead of the shipped one only when the set is complete, its schema is one this code reads
and it was published after the shipped copy. A newer schema is noted and ignored, never half-applied.

Precedence: shipped < fetched < the user's own settings. Nothing here writes
accounts-override.json, local user-scores.json or host policy; their loaders layer them on top of whichever set is in effect.
Shipped references that differ from their own manifest (a checkout with local edits) are used as they are.

Off switch: AGENTCOMPASS_PUBLIC_DATA=off, or {"enabled": false} in <state dir>/public-data-config.json.
Layout and options: ../references/router-internals.md (Public reference data).
"""
from datetime import datetime, timedelta, timezone
import atexit
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import threading
import urllib.request
import uuid

# `agentcompass data` launches this file with `python -I`. Isolated mode does not put the
# script directory on sys.path, so the sibling import of user_scores would fail and a valid
# fetched set would be rejected. Same insertion as route_judge.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))

_ISO_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def _valid_public_declaration(entry):
    if not isinstance(entry, dict) or entry.get('kind') == 'user':
        return False
    url = str(entry.get('url') or '').strip()
    if url.startswith('https://'):
        return True
    as_of = str(entry.get('as_of') or '').strip()
    return entry.get('kind') == 'maintainer' and bool(_ISO_DATE.match(as_of))


def _valid_capacity_rule(entry):
    if not _valid_public_declaration(entry):
        return False
    for key in ('target_family', 'target_window', 'reference_family', 'reference_window'):
        if not isinstance(entry.get(key), str) or not entry[key].strip():
            return False
    forbidden = {'plan', 'base_plan', 'plan_price_usd_per_month', 'base_plan_price_usd_per_month', 'capacity_usd'}
    if forbidden & set(entry) or any(isinstance(v, (int, float)) and not isinstance(v, bool) for v in entry.values()):
        return False
    return True


def _valid_vendor_price_source(entry):
    if not isinstance(entry, dict) or entry.get('kind') != 'vendor':
        return False
    url = str(entry.get('url') or '').strip()
    as_of = str(entry.get('as_of') or '').strip()
    return url.startswith('https://') and bool(_ISO_DATE.match(as_of))


def validate_plan_templates_provenance(doc):
    """Ensure every `price_source` declared in plan-templates.json is a dated vendor https citation."""
    if not isinstance(doc, dict):
        raise ValueError('plan-templates.json must be a JSON object')
    for family, plans in doc.items():
        if not isinstance(plans, dict):
            continue
        for plan, tpl in plans.items():
            if not isinstance(tpl, dict):
                continue
            if 'price_source' in tpl and not _valid_vendor_price_source(tpl.get('price_source')):
                raise ValueError(f'plan-templates[{family!r}][{plan!r}].price_source requires kind="vendor", https url and ISO as_of')


def validate_accounts_provenance(doc):
    """Ensure every capability/equivalence declaration in accounts.json has a public https URL or dated maintainer kind."""
    if not isinstance(doc, dict):
        raise ValueError('accounts.json must be a JSON object')
    for row, entry in (doc.get('same_model') or {}).items():
        if not _valid_public_declaration(entry):
            raise ValueError(f'same_model[{row!r}] requires an https url or dated kind="maintainer" (with as_of)')
        if 'capacity' in entry:
            raise ValueError(f'same_model[{row!r}].capacity may not hard-code a plan or price; use rule-only capacity_rule')
        if 'capacity_rule' in entry and not _valid_capacity_rule(entry.get('capacity_rule')):
            raise ValueError(
                f'same_model[{row!r}].capacity_rule requires dated kind="maintainer" (or https url) and family/window rule fields without plan or price literals'
            )
    for model, spec in (doc.get('models') or {}).items():
        if not isinstance(spec, dict):
            continue
        if spec.get('text_only') and not _valid_public_declaration(spec.get('text_only_source')):
            raise ValueError(f'models[{model!r}].text_only requires a cited or dated maintainer text_only_source (with as_of)')
        if 'image_input' in spec and not _valid_public_declaration(spec.get('image_input_source')):
            raise ValueError(f'models[{model!r}].image_input requires a cited or dated maintainer image_input_source (with as_of)')
        for field in ('text_only_source', 'image_input_source'):
            if field in spec and not _valid_public_declaration(spec.get(field)):
                raise ValueError(f'models[{model!r}].{field} requires an https url or dated kind="maintainer" (with as_of)')
        _validate_model_data(model, spec)
    _validate_catalog_tables(doc)


LISTING_BOARDS = ('arena', 'aa')        # calibration/tiers.py LISTING_BOARDS: `listed_as` keys
LIST_PRICE_FIELDS = ('input', 'cache_write_5m', 'cache_write_1h', 'cache_read', 'output')


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def _price_table(value, fields):
    return isinstance(value, dict) and set(value) == set(fields) and all(_number(value[f]) for f in fields)


def _validate_model_data(model, spec):
    """Shape of the per-model reference fields (`listed_as`, `list_price`, `vendor_fast_mode`, `fast_capable`)."""
    if 'listed_as' in spec:
        names = spec['listed_as']
        if (not isinstance(names, dict) or not names or not set(names) <= set(LISTING_BOARDS)
                or not all(isinstance(v, str) and v.strip() for v in names.values())):
            raise ValueError(f'models[{model!r}].listed_as maps {"/".join(LISTING_BOARDS)} to the name each board lists the model under')
    if 'list_price' in spec:
        if not _price_table(spec['list_price'], LIST_PRICE_FIELDS):
            raise ValueError(f'models[{model!r}].list_price needs numeric {", ".join(LIST_PRICE_FIELDS)} (USD per million tokens)')
        if not _valid_vendor_price_source(spec.get('list_price_source')):
            raise ValueError(f'models[{model!r}].list_price requires list_price_source with kind="vendor", https url and ISO as_of')
    if 'vendor_fast_mode' in spec:
        fast = spec['vendor_fast_mode']
        ok = (isinstance(fast, dict) and _number(fast.get('price_multiplier')) and _number(fast.get('speed_multiplier'))
              and all(_price_table(fast.get(k), ('input', 'output', 'cache_read')) for k in ('fast_price', 'standard_price'))
              and all(str(fast.get(k) or '').startswith('https://') for k in ('url', 'pricing_url'))
              and bool(_ISO_DATE.match(str(fast.get('as_of') or ''))) and isinstance(fast.get('statement'), str))
        if not ok:
            raise ValueError(f'models[{model!r}].vendor_fast_mode needs price_multiplier, speed_multiplier, fast_price, '
                             'standard_price, https url and pricing_url, ISO as_of and the vendor statement')
    if 'fast_capable' in spec and not isinstance(spec['fast_capable'], bool):
        raise ValueError(f'models[{model!r}].fast_capable must be true or false')


def _validate_catalog_tables(doc):
    """The catalog-wide model tables: `families` (model_resolution) and `calibration_panel` (calibration/measurements.py)."""
    models = {k for k, v in (doc.get('models') or {}).items() if isinstance(v, dict)}
    families = doc.get('families')
    if not isinstance(families, list):
        raise ValueError('accounts.json needs a families list of {pattern, models}')
    for i, entry in enumerate(families):
        if not isinstance(entry, dict) or not isinstance(entry.get('pattern'), str) or not isinstance(entry.get('models'), list):
            raise ValueError(f'families[{i}] needs a pattern and a models list')
        try:
            re.compile(entry['pattern'])
        except re.error as exc:
            raise ValueError(f'families[{i}].pattern {entry["pattern"]!r} is not a regular expression: {exc}') from None
        unknown = [m for m in entry['models'] if m not in models]
        if unknown:
            raise ValueError(f'families[{i}] ({entry["pattern"]}) names models without a row: {", ".join(map(str, unknown))}')
    panel = doc.get('calibration_panel')
    if not _valid_public_declaration(panel) or not isinstance(panel.get('anchor'), str) or not isinstance(panel.get('models'), list):
        raise ValueError('accounts.json needs calibration_panel {anchor, models} as a dated maintainer declaration')
    unknown = [m for m in [panel['anchor'], *panel['models']] if m not in models]
    if unknown or panel['anchor'] not in panel['models']:
        raise ValueError(f'calibration_panel anchor and models must be model rows, the anchor among the models'
                         + (f' (no row: {", ".join(map(str, unknown))})' if unknown else ''))

SCHEMA_VERSION = 3              # the newest data schema this code reads; bump with any breaking data change
# Schema 3 (2026-11-11) switches to the four-category taxonomy (`divergence`, `coding`, `aesthetics`, `tool_operation`),
# ships `five-axis-scores.json` and maintainer `user-scores.json` in the public channel, and rejects schema-2 sets
# whose `classifier-cases.json` used the retired four-axis `axis_demand` keys.
MIN_SCHEMA_VERSION = 3
FILES = ('accounts.json', 'plan-templates.json', 'classifier-cases.json', 'five-axis-scores.json', 'user-scores.json')
MANIFEST = 'public-data.json'
CONFIG_FILE = 'public-data-config.json'
DEFAULT_URL = 'https://raw.githubusercontent.com/akiiwa-games/AgentCompass/main/skills/agent-compass/references/'
INTERVAL_HOURS = 6              # at most one check per interval after a successful one
RETRY_MINUTES = 30              # after a failed (or still running) attempt
TIMEOUT_SECONDS = 5             # per request; the routing thread never waits on it
EXIT_GRACE_SECONDS = 2          # a background refresh may delay process exit by this much at most
MAX_BYTES = 8 * 1024 * 1024
OFF_WORDS = ('0', 'off', 'false', 'no', 'disabled')
BUNDLED = Path(__file__).resolve().parents[1] / 'references'
USER_AGENT = 'AgentCompass-public-data'

_MEMO = {}
_PINNED = {}


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


def state_dir(environ=None):
    configured = str(_env(environ).get('ROUTER_STATE_DIR') or '').strip()
    if configured:
        return Path(os.path.expandvars(configured)).expanduser()
    home = str(_env(environ).get('USERPROFILE') or _env(environ).get('HOME') or '') if environ is not None else ''
    return (Path(home) if home else Path.home()) / '.agent-compass'


def cache_dir(environ=None):
    return state_dir(environ) / 'public-data'


def _read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def config(environ=None):
    """Effective switch, interval and source; the environment beats the config file."""
    env = _env(environ)
    stored = _read_json(state_dir(environ) / CONFIG_FILE) or {}
    enabled, basis = stored.get('enabled') is not False, f'{CONFIG_FILE}' if 'enabled' in stored else 'default'
    switch = str(env.get('AGENTCOMPASS_PUBLIC_DATA') or '').strip().lower()
    if switch:
        enabled, basis = switch not in OFF_WORDS, 'AGENTCOMPASS_PUBLIC_DATA'
    url = str(env.get('AGENTCOMPASS_PUBLIC_DATA_URL') or stored.get('url') or DEFAULT_URL).strip()
    hours = env.get('AGENTCOMPASS_PUBLIC_DATA_INTERVAL_HOURS') or stored.get('interval_hours')
    try:
        hours = max(0.0, float(hours)) if hours not in (None, '') else INTERVAL_HOURS
    except (TypeError, ValueError):
        hours = INTERVAL_HOURS
    return {'enabled': enabled, 'switch': basis, 'url': url if url.endswith('/') else url + '/', 'interval_hours': hours}


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def set_version(manifest):
    """Content id of a manifest: the schema and each file's hash."""
    files = manifest.get('files') or {}
    lines = [f"schema {manifest.get('schema_version')}"] + [f"{name} {(files.get(name) or {}).get('sha256')}" for name in FILES]
    return sha256_bytes('\n'.join(lines).encode('utf-8'))[:12]


def build_manifest(directory, previous=None, now=None):
    """Manifest for the files in ``directory``. ``published_at`` moves only when the content changes."""
    directory = Path(directory)
    files = {}
    for name in FILES:
        data = (directory / name).read_bytes()
        json.loads(data.decode('utf-8-sig'))
        files[name] = {'sha256': sha256_bytes(data), 'bytes': len(data)}
    manifest = {'schema_version': SCHEMA_VERSION, 'files': files}
    manifest['version'] = set_version(manifest)
    same = isinstance(previous, dict) and previous.get('version') == manifest['version'] and _parse(previous.get('published_at'))
    manifest['published_at'] = previous['published_at'] if same else _stamp(now or _now())
    return {'schema_version': manifest['schema_version'], 'version': manifest['version'],
            'published_at': manifest['published_at'], 'files': files}


def write_manifest(directory=None, now=None):
    """Build and write ``public-data.json`` in ``directory`` when missing or changed; unchanged manifests stay byte-identical."""
    directory = Path(directory or BUNDLED)
    path = directory / MANIFEST
    current = _read_json(path)
    fresh = build_manifest(directory, current, now=now)
    expected = (json.dumps(fresh, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    if not path.is_file() or path.read_bytes() != expected:
        _write_json(path, fresh)
    unpin()
    return fresh


def check_set(directory, manifest):
    """('ok' | 'no_manifest' | 'schema' | 'incomplete' | 'modified', detail) for a directory and its manifest."""
    if not isinstance(manifest, dict):
        return 'no_manifest', 'no public-data.json'
    schema = manifest.get('schema_version')
    if not isinstance(schema, int) or not MIN_SCHEMA_VERSION <= schema <= SCHEMA_VERSION:
        return 'schema', f'data schema {schema!r}; this install reads {MIN_SCHEMA_VERSION}..{SCHEMA_VERSION}'
    if _parse(manifest.get('published_at')) is None:
        return 'incomplete', 'manifest has no published_at'
    files = manifest.get('files') if isinstance(manifest.get('files'), dict) else {}
    for name in FILES:
        want = (files.get(name) or {}).get('sha256')
        if not isinstance(want, str):
            return 'incomplete', f'{name} is not in the manifest'
        try:
            data = (Path(directory) / name).read_bytes()
        except OSError:
            return 'incomplete', f'{name} is missing'
        if sha256_bytes(data) != want:
            return 'modified', f'{name} does not match its sha256'
        if name == 'accounts.json':
            try:
                validate_accounts_provenance(json.loads(data.decode('utf-8-sig')))
            except Exception as exc:
                return 'incomplete', f'accounts.json is invalid: {exc}'
        if name == 'plan-templates.json':
            try:
                validate_plan_templates_provenance(json.loads(data.decode('utf-8-sig')))
            except Exception as exc:
                return 'incomplete', f'plan-templates.json is invalid: {exc}'
        if name == 'user-scores.json':
            try:
                import user_scores
                user_scores.validate_document(json.loads(data.decode('utf-8-sig')), expected_kind='maintainer')
            except Exception as exc:
                return 'incomplete', f'user-scores.json is invalid: {exc}'
    return 'ok', ''


def read_state(environ=None):
    return _read_json(cache_dir(environ) / 'state.json') or {}


def _write_json(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex[:8]}.tmp')
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
    os.replace(temporary, path)


def _mtime(path):
    try:
        return Path(path).stat().st_mtime_ns
    except OSError:
        return None


def selection(bundled=None, environ=None):
    """Which reference set this process reads: {'source': 'bundled'|'fetched', 'dir', 'version', ...}. Read only."""
    bundled = Path(bundled or BUNDLED)
    cfg = config(environ)
    cache = cache_dir(environ)
    pin_key = (str(bundled), str(cache))
    if pin_key in _PINNED:
        return _PINNED[pin_key]
    key = (pin_key, cfg['enabled'], _mtime(cache / 'state.json'), _mtime(cache / 'sets'), _mtime(bundled / MANIFEST),
           *(_mtime(bundled / name) for name in FILES))
    if key not in _MEMO:
        if len(_MEMO) > 32:
            _MEMO.clear()
        _MEMO[key] = _select(bundled, cfg, environ)
    return _MEMO[key]


def _select(bundled, cfg, environ):
    shipped = _read_json(bundled / MANIFEST)
    result = {'source': 'bundled', 'dir': str(bundled), 'enabled': cfg['enabled'], 'note': None,
              'version': (shipped or {}).get('version'), 'published_at': (shipped or {}).get('published_at'),
              'schema_version': (shipped or {}).get('schema_version'), 'bundled_version': (shipped or {}).get('version')}
    if shipped is None:
        return result  # not a references directory this channel manages
    shipped_state, detail = check_set(bundled, shipped)
    if not cfg['enabled']:
        return {**result, 'note': f"public data channel is off ({cfg['switch']})"}
    if shipped_state == 'modified':
        return {**result, 'note': f'shipped references have local edits ({detail}); they are used as they are'}
    state = read_state(environ)
    version = state.get('version')
    if not isinstance(version, str) or not version.isalnum():
        return {**result, 'note': 'nothing fetched yet'}
    directory = cache_dir(environ) / 'sets' / version
    fetched = _read_json(directory / MANIFEST)
    fetched_state, detail = check_set(directory, fetched)
    if fetched_state != 'ok' or set_version(fetched) != version:
        return {**result, 'note': f'fetched set {version} is not usable ({detail or "version mismatch"})'}
    if shipped_state == 'ok' and _parse(fetched['published_at']) <= _parse(shipped.get('published_at')):
        return {**result, 'note': 'shipped references are as new as the fetched set'}
    return {**result, 'source': 'fetched', 'dir': str(directory), 'version': version,
            'published_at': fetched['published_at'], 'schema_version': fetched['schema_version']}


def effective(path, environ=None):
    """The file to read for a shipped reference path: the fetched copy when the channel selects it, else ``path``."""
    path = Path(path)
    if path.name not in FILES:
        return path
    try:
        chosen = selection(path.parent, environ)
    except Exception:
        return path
    fetched = Path(chosen['dir']) / path.name
    return fetched if chosen['source'] == 'fetched' and fetched.is_file() else path


def pin(bundled=None, environ=None):
    """Keep this process on the set it starts with, so a refresh finishing mid-run cannot mix two sets."""
    bundled = Path(bundled or BUNDLED)
    chosen = selection(bundled, environ)
    _PINNED[(str(bundled), str(cache_dir(environ)))] = chosen
    return chosen


def unpin():
    _PINNED.clear()
    _MEMO.clear()


def due(state, cfg, now=None):
    now = now or _now()
    checked, attempt = _parse(state.get('checked_at')), _parse(state.get('attempt_at'))
    if checked is not None and now - checked < timedelta(hours=cfg['interval_hours']):
        return False
    if attempt is not None and (checked is None or attempt > checked) and now - attempt < timedelta(minutes=RETRY_MINUTES):
        return False
    return True


def http_get(url, timeout=TIMEOUT_SECONDS):
    """Plain HTTPS GET; urllib takes HTTPS_PROXY/HTTP_PROXY/NO_PROXY (and the Windows proxy settings) from the system."""
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, 'Cache-Control': 'no-cache'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError(f'{url} is larger than {MAX_BYTES} bytes')
    return data


def refresh(force=False, environ=None, fetch=None, now=None, bundled=None, timeout=TIMEOUT_SECONDS):
    """Check the published manifest and fetch a changed set. Returns a status dict; never raises for network errors."""
    cfg = config(environ)
    if not cfg['enabled']:
        return {'status': 'disabled', 'switch': cfg['switch']}
    fetch = fetch or http_get
    now = now or _now()
    bundled = Path(bundled or BUNDLED)
    cache = cache_dir(environ)
    state = read_state(environ)
    if not force and not due(state, cfg, now):
        return {'status': 'not_due', 'checked_at': state.get('checked_at'), 'version': state.get('version')}
    state['attempt_at'] = _stamp(now)
    try:
        _write_json(cache / 'state.json', state)  # claim the attempt so concurrent rolls do not repeat it
        result = _refresh(cfg, cache, state, fetch, now, bundled, timeout)
    except Exception as error:
        state['last_error'] = f'{type(error).__name__}: {str(error)[:300]}'
        result = {'status': 'failed', 'error': state['last_error']}
    try:
        _write_json(cache / 'state.json', state)
    except OSError as error:
        result = {'status': 'failed', 'error': f'state not saved: {error}'}
    return result


def _refresh(cfg, cache, state, fetch, now, bundled, timeout):
    raw = fetch(cfg['url'] + MANIFEST, timeout)
    manifest = json.loads(raw.decode('utf-8-sig'))
    if not isinstance(manifest, dict) or not isinstance(manifest.get('files'), dict):
        raise ValueError('published manifest has no files table')
    schema = manifest.get('schema_version')
    if isinstance(schema, int) and schema > SCHEMA_VERSION:
        state.update(checked_at=_stamp(now), last_error=None, remote_schema_version=schema,
                     note=(f'published data uses schema {schema}; this install reads up to {SCHEMA_VERSION}. '
                           'It is ignored until AgentCompass is updated.'))
        return {'status': 'schema_too_new', 'remote_schema_version': schema, 'schema_version': SCHEMA_VERSION}
    if not isinstance(schema, int) or schema < MIN_SCHEMA_VERSION or _parse(manifest.get('published_at')) is None:
        raise ValueError('published manifest has no usable schema_version or published_at')
    version = set_version(manifest)
    target = cache / 'sets' / version
    if check_set(target, _read_json(target / MANIFEST))[0] == 'ok':
        state.update(version=version, published_at=manifest['published_at'], schema_version=schema,
                     checked_at=_stamp(now), last_error=None, note=None, url=cfg['url'])
        state.pop('remote_schema_version', None)
        return {'status': 'unchanged', 'version': version}
    staging = cache / 'sets' / f'.staging-{uuid.uuid4().hex[:8]}'
    staging.mkdir(parents=True, exist_ok=True)
    try:
        local = [bundled] + ([cache / 'sets' / state['version']] if isinstance(state.get('version'), str) else [])
        downloaded = []
        for name in FILES:
            want = (manifest['files'].get(name) or {}).get('sha256')
            if not isinstance(want, str):
                raise ValueError(f'published manifest does not list {name}')
            data = next((d for d in (_read_bytes(folder / name) for folder in local) if d is not None
                         and sha256_bytes(d) == want), None)
            if data is None:
                data = fetch(cfg['url'] + name, timeout)
                downloaded.append(name)
                if sha256_bytes(data) != want:
                    raise ValueError(f'{name} does not match the published sha256; nothing was applied')
            json.loads(data.decode('utf-8-sig'))
            (staging / name).write_bytes(data)
        (staging / MANIFEST).write_bytes(raw)
        try:
            os.replace(staging, target)
        except OSError:
            if check_set(target, _read_json(target / MANIFEST))[0] != 'ok':
                raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    previous = state.get('version')
    state.update(version=version, published_at=manifest['published_at'], schema_version=schema, fetched_at=_stamp(now),
                 checked_at=_stamp(now), last_error=None, note=None, url=cfg['url'])
    state.pop('remote_schema_version', None)
    _prune(cache / 'sets', {version, previous})
    return {'status': 'updated', 'version': version, 'previous': previous, 'downloaded': downloaded}


def _read_bytes(path):
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def _prune(sets, keep):
    try:
        children = list(sets.iterdir())
    except OSError:
        return
    for child in children:
        if child.is_dir() and child.name not in keep and not child.name.startswith('.staging-'):
            shutil.rmtree(child, ignore_errors=True)


def refresh_in_background(environ=None, bundled=None):
    """Pin this process's set, then start a daemon refresh when one is due. Returns the thread or None."""
    pin(bundled, environ)
    cfg = config(environ)
    if not cfg['enabled'] or not due(read_state(environ), cfg):
        return None
    snapshot = dict(_env(environ))

    def work():
        try:
            refresh(environ=snapshot, bundled=bundled)
        except Exception:
            pass

    thread = threading.Thread(target=work, name='agentcompass-public-data', daemon=True)
    thread.start()
    atexit.register(thread.join, EXIT_GRACE_SECONDS)
    return thread


def status(environ=None, bundled=None):
    """What doctor and `agentcompass data` print. Read only."""
    cfg = config(environ)
    chosen = selection(bundled, environ)
    state = read_state(environ)
    return {'enabled': cfg['enabled'], 'switch': cfg['switch'], 'source': chosen['source'], 'version': chosen['version'],
            'published_at': chosen['published_at'], 'schema_version': chosen['schema_version'],
            'reads_schema': SCHEMA_VERSION, 'bundled_version': chosen['bundled_version'], 'dir': chosen['dir'],
            'note': chosen['note'] or state.get('note'), 'fetched_at': state.get('fetched_at'),
            'checked_at': state.get('checked_at'), 'last_error': state.get('last_error'), 'url': cfg['url'],
            'interval_hours': cfg['interval_hours'], 'cache': str(cache_dir(environ))}


def summary_line(report):
    """One line for doctor."""
    when = f", fetched {report['fetched_at']}" if report.get('fetched_at') else ''
    checked = f", checked {report['checked_at']}" if report.get('checked_at') else ''
    line = (f"{report['source']} {report.get('version') or '?'} (published {report.get('published_at') or '?'}, "
            f"schema {report.get('schema_version')}{when}{checked})")
    if not report.get('enabled'):
        line += ' - channel off'
    for key in ('note', 'last_error'):
        if report.get(key):
            line += f"; {key.replace('_', ' ')}: {report[key]}"
    return line


USAGE = """usage: agentcompass data [--refresh]
       agentcompass data refresh [--no-fetch] [--data DIR] [--report FILE] [--distrust SOURCE]

Shows which public reference data (accounts.json, plan-templates.json, classifier-cases.json, five-axis-scores.json,
user-scores.json) routing reads: the copy shipped with this install, or the newer set fetched from the AgentCompass
repository's main branch.

--refresh   check the published data now and fetch it when it changed (ignores the refresh interval)

Maintainers (in a git checkout):
  agentcompass data refresh [--no-fetch]   rebuild calibrated data, capability views and public-data.json, then validate
  public_data.py manifest [--check | --write]   verify or rewrite references/public-data.json after hand edits
Off switch: AGENTCOMPASS_PUBLIC_DATA=off, or {"enabled": false} in ~/.agent-compass/public-data-config.json.
"""


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if any(arg in ('-h', '--help') for arg in argv):
        sys.stdout.write(USAGE)
        return 0
    if argv[:1] == ['manifest']:
        path = BUNDLED / MANIFEST
        if '--write' in argv:
            fresh = write_manifest(BUNDLED)
            state, detail = check_set(BUNDLED, fresh)
            if state != 'ok':
                sys.stderr.write(f'{path} failed public-data check ({state}): {detail}\n')
                return 1
            print(json.dumps(fresh, indent=2))
            return 0
        current = _read_json(path)
        fresh = build_manifest(BUNDLED, current)
        if fresh != current:
            sys.stderr.write(f'{path} is stale; run: python -X utf8 {Path(__file__).name} manifest --write\n')
            return 1
        state, detail = check_set(BUNDLED, current)
        if state != 'ok':
            sys.stderr.write(f'{path} failed public-data check ({state}): {detail}\n')
            return 1
        print(f"{path} is current ({fresh['version']}, published {fresh['published_at']})")
        return 0
    if argv and argv[0] not in ('status', '--refresh'):
        sys.stderr.write(USAGE)
        return 2
    if argv[:1] == ['--refresh']:
        outcome = refresh(force=True, timeout=10)
        unpin()
        print(json.dumps({'refresh': outcome, **status()}, ensure_ascii=False, indent=2))
        return 1 if outcome.get('status') == 'failed' else 0
    print(json.dumps(status(), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
