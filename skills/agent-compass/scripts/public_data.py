"""Published reference data: the maintainer's changes between releases reach installs without a product update.

Every install ships the reference set under references/ (FILES) with references/public-data.json, the manifest that
publishes it: the sha256 of every file, the data schema and the publication time. The maintainer changes data between
releases only on purpose (a new model, a corrected hand-written score or launch row) and publishes it with
`agentcompass data publish`; the public-data workflow fails a push whose manifest does not match the files. No board is
read here: the fetching refresh of the boards runs once, when a release is prepared (calibration/refresh.py).

This module fetches that manifest and the files from the repository's main branch over plain HTTPS (no credentials,
proxies from the environment) into <state dir>/public-data/, verifies every hash, and makes the router read the fetched
set instead of the shipped one only when the set is complete, every schema in it is one this code reads and it was
published after the shipped copy. A newer data schema is noted and ignored, never half-applied; an older one, or a set
this code rejects, is noted once per published version and never downloaded again.

Precedence: shipped < fetched < the user's own settings. Nothing here writes accounts-override.json, the local
user-scores.json or host policy; their loaders layer them on top of whichever set is in effect. Shipped references that
differ from their own manifest (a checkout with local edits) are used as they are.

Off switch: AGENTCOMPASS_PUBLIC_DATA=off, or {"enabled": false} in <state dir>/public-data-config.json.
Layout and options: ../references/router-internals.md (Reference data).
"""
from datetime import datetime, timedelta, timezone
import atexit
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time
import urllib.request
import uuid

# `agentcompass data` launches this file with `python -I`, and doctor loads it by path. Neither puts the script
# directory on sys.path, so the sibling imports below would fail. Same insertion as route_judge.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import state_upgrade  # noqa: E402

SCHEMA_VERSION = 7              # the newest data schema this code reads; bump with any breaking data change
# Schema 7 (2026-11-18, v0.7.0): level 3 is the panel maximum. five-axis-scores.json is `agent-compass.five-axis-scores/6`
# and user-scores.json `schema_version = 3`. Sets of schema 6 and older (v0.6.x) are not read; the files of each schema
# are checked against their own readers as well (check_set).
MIN_SCHEMA_VERSION = 7
UPDATE_APP = 'update AgentCompass to the latest release to receive new reference data'
# What a run-time reader reads (router-internals.md, Reference data). Left out on purpose: level-3-conversion.json (the
# frozen conversion of the user-scores upgrade step, owned with that code), public-measurements.json (read only by the
# maintainer's refresh) and the generated model-capabilities.{json,md} (documentation views, no run-time reader).
FILES = ('accounts.json', 'plan-templates.json', 'classifier-cases.json', 'five-axis-scores.json', 'user-scores.json')
MANIFEST = 'public-data.json'
CONFIG_FILE = 'public-data-config.json'
INTERVAL_HOURS = 6              # at most one check per interval after a successful one
RETRY_MINUTES = 30              # after a failed (or still running) attempt
TIMEOUT_SECONDS = 5             # per request; the routing thread never waits on it
EXIT_GRACE_SECONDS = 2          # a background check may delay process exit by this much at most
STAGING_MAX_AGE_SECONDS = 3600  # an interrupted download's staging directory is removed after this
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


def cache_dir(environ=None):
    return state_upgrade.state_dir(environ=environ) / 'public-data'


def _read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def published_url():
    """The repository's published references (agentcompass/origin.py PUBLIC_DATA_URL), or None when this skill has no
    package next to it to name them. Found by path: the router scripts do not import the agentcompass package."""
    up = list(Path(__file__).resolve().parents)
    # a checkout or the Windows product (<root>/skills/agent-compass/scripts), a wheel (agentcompass/data/skills/...)
    for origin in (up[3] / 'agentcompass' / 'origin.py', *(up[4:5] and [up[4] / 'origin.py'])):
        if origin.is_file() and origin.parent.name == 'agentcompass':
            spec = importlib.util.spec_from_file_location('agentcompass_origin', origin)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module.PUBLIC_DATA_URL
    return None


def config(environ=None):
    """Effective switch, interval and source; the environment beats the config file."""
    env = _env(environ)
    stored = _read_json(state_upgrade.state_dir(environ=environ) / CONFIG_FILE) or {}
    enabled, basis = stored.get('enabled') is not False, CONFIG_FILE if 'enabled' in stored else 'default'
    switch = str(env.get('AGENTCOMPASS_PUBLIC_DATA') or '').strip().lower()
    if switch:
        enabled, basis = switch not in OFF_WORDS, 'AGENTCOMPASS_PUBLIC_DATA'
    url = str(env.get('AGENTCOMPASS_PUBLIC_DATA_URL') or stored.get('url') or published_url() or '').strip()
    hours = env.get('AGENTCOMPASS_PUBLIC_DATA_INTERVAL_HOURS') or stored.get('interval_hours')
    try:
        hours = max(0.0, float(hours)) if hours not in (None, '') else INTERVAL_HOURS
    except (TypeError, ValueError):
        hours = INTERVAL_HOURS
    url = url if not url or url.endswith('/') else url + '/'
    return {'enabled': enabled, 'switch': basis, 'url': url or None, 'interval_hours': hours}


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
    version = set_version(manifest)
    same = isinstance(previous, dict) and previous.get('version') == version and _parse(previous.get('published_at'))
    return {'schema_version': SCHEMA_VERSION, 'version': version,
            'published_at': previous['published_at'] if same else _stamp(now or _now()), 'files': files}


def _manifest_bytes(manifest):
    return (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def validate_files(directory):
    """Raise ValueError naming the file when a file of the set is not one this code reads: the provenance checks
    (reference_provenance.py, which also holds user-scores.json to its schema), the five-axis score schema
    (axis_scores.require_current_schema) and the classifier cases' sections."""
    import axis_scores
    import reference_provenance
    directory = Path(directory)
    reference_provenance.validate_references(directory)
    try:
        axis_scores.require_current_schema(json.loads((directory / 'five-axis-scores.json').read_text(encoding='utf-8-sig')))
    except ValueError as exc:
        raise ValueError(f'five-axis-scores.json is invalid: {exc}') from None
    cases = json.loads((directory / 'classifier-cases.json').read_text(encoding='utf-8-sig'))
    missing = [key for key in ('route_definitions', 'dimension_rubric', 'calibration_cases') if not cases.get(key)]
    if missing:
        raise ValueError(f'classifier-cases.json is invalid: no {", ".join(missing)}')


def check_set(directory, manifest, *, validate=True):
    """('ok' | 'no_manifest' | 'schema' | 'incomplete' | 'modified' | 'invalid', detail) for a directory and its manifest.
    ``validate`` also runs validate_files; without it only completeness, schema and hashes are checked."""
    if not isinstance(manifest, dict):
        return 'no_manifest', f'no {MANIFEST}'
    schema = manifest.get('schema_version')
    if not isinstance(schema, int) or isinstance(schema, bool) or not MIN_SCHEMA_VERSION <= schema <= SCHEMA_VERSION:
        return 'schema', f'data schema {schema!r}; this install reads {_schema_range()}'
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
    if validate:
        try:
            validate_files(directory)
        except Exception as exc:                # a set of another shape must be refused, never crash the reader
            return 'invalid', f'{type(exc).__name__}: {exc}' if not isinstance(exc, ValueError) else str(exc)
    return 'ok', ''


def publish(directory=None, now=None):
    """The one act that publishes the reference set in ``directory``: check every file, then write the manifest when
    missing or changed (an unchanged set keeps its manifest byte-identical, `published_at` included)."""
    directory = Path(directory or BUNDLED)
    validate_files(directory)
    path = directory / MANIFEST
    fresh = build_manifest(directory, _read_json(path), now=now)
    data = _manifest_bytes(fresh)
    if not path.is_file() or path.read_bytes() != data:
        _write_bytes(path, data)
    unpin()
    return fresh


def check_published(directory=None):
    """'' when the manifest in ``directory`` publishes exactly its files, else what is wrong (the public-data workflow)."""
    directory = Path(directory or BUNDLED)
    path = directory / MANIFEST
    current = _read_json(path)
    if current is None:
        return f'{path} is missing; run: agentcompass data publish'
    try:
        fresh = build_manifest(directory, current)
    except (OSError, ValueError) as exc:
        return f'the reference set in {directory} is not readable: {exc}'
    if fresh != current:
        changed = [n for n in FILES if (current.get('files') or {}).get(n) != fresh['files'][n]]
        what = f"{', '.join(changed)} changed" if changed else f"the data schema is {SCHEMA_VERSION}"
        return f'{path} does not publish the files beside it ({what}); run: agentcompass data publish'
    state, detail = check_set(directory, current)
    return '' if state == 'ok' else f'{path} failed the reference data check ({state}): {detail}'


def read_state(environ=None):
    return _read_json(cache_dir(environ) / 'state.json') or {}


def _write_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex[:8]}.tmp')
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _write_json(path, document):
    _write_bytes(path, _manifest_bytes(document))


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
        return {**result, 'note': f'no {MANIFEST} beside the shipped references; they are used as they are'}
    shipped_state, detail = check_set(bundled, shipped, validate=False)
    if not cfg['enabled']:
        return {**result, 'note': f"published data channel is off ({cfg['switch']})"}
    if shipped_state == 'modified':
        return {**result, 'note': f'shipped references have local edits ({detail}); they are used as they are'}
    version = read_state(environ).get('version')
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
    """The one accessor of the reference set: the file to read for a shipped reference ``path``, the fetched copy when
    the channel selects it, else ``path``."""
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
    """Keep this process on the set it starts with, so a check finishing mid-run cannot mix two sets."""
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
    """Check the published manifest and fetch a changed set. Returns a status dict; never raises for network errors.
    'state_not_saved' is the one local failure (the state directory is not writable)."""
    cfg = config(environ)
    if not cfg['enabled']:
        return {'status': 'disabled', 'switch': cfg['switch']}
    if not cfg['url']:
        return {'status': 'unavailable', 'detail': 'no published location: agentcompass/origin.py is not beside this skill'}
    fetch = fetch or http_get
    now = now or _now()
    bundled = Path(bundled or BUNDLED)
    cache = cache_dir(environ)
    state = read_state(environ)
    if not force and not due(state, cfg, now):
        return {'status': 'not_due', 'checked_at': state.get('checked_at'), 'version': state.get('version')}
    state['attempt_at'] = _stamp(now)
    try:
        _write_json(cache / 'state.json', state)  # claim the attempt so concurrent runs do not repeat it
    except OSError as error:
        return {'status': 'state_not_saved', 'error': str(error)}
    try:
        result = _refresh(cfg, cache, state, fetch, now, bundled, timeout)
    except Exception as error:
        state['last_error'] = f'{type(error).__name__}: {str(error)[:300]}'
        result = {'status': 'failed', 'error': state['last_error']}
    try:
        _write_json(cache / 'state.json', state)
    except OSError as error:
        result = {'status': 'state_not_saved', 'error': str(error)}
    return result


def _schema_range():
    return (f'schema {SCHEMA_VERSION}' if MIN_SCHEMA_VERSION == SCHEMA_VERSION
            else f'schema {MIN_SCHEMA_VERSION}..{SCHEMA_VERSION}')


def reader_id():
    """Content hash of the code that judges a published set. A set it rejected is recorded with it (`rejected_by`), so
    an updated install, whose reader differs, looks at that set again."""
    digest = hashlib.sha256()
    here = Path(__file__)
    for path in (here, here.with_name('reference_provenance.py'), here.with_name('user_scores.py'),
                 here.with_name('axis_scores.py')):
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def _accept(state, manifest, cfg, now, **extra):
    state.update(version=set_version(manifest), published_at=manifest['published_at'],
                 schema_version=manifest['schema_version'], checked_at=_stamp(now), last_error=None, note=None,
                 url=cfg['url'], **extra)
    for key in ('remote_schema_version', 'rejected_version', 'rejected_by'):
        state.pop(key, None)


def _refresh(cfg, cache, state, fetch, now, bundled, timeout):
    raw = fetch(cfg['url'] + MANIFEST, timeout)
    manifest = json.loads(raw.decode('utf-8-sig'))
    if not isinstance(manifest, dict) or not isinstance(manifest.get('files'), dict):
        raise ValueError('published manifest has no files table')
    schema = manifest.get('schema_version')
    if isinstance(schema, int) and schema > SCHEMA_VERSION:
        state.update(checked_at=_stamp(now), last_error=None, remote_schema_version=schema,
                     note=(f'published data uses schema {schema}; this install reads up to {SCHEMA_VERSION}. '
                           f'The last accepted set stays in effect: {UPDATE_APP}.'))
        return {'status': 'schema_too_new', 'remote_schema_version': schema, 'schema_version': SCHEMA_VERSION}
    if isinstance(schema, int) and schema < MIN_SCHEMA_VERSION:
        state.update(checked_at=_stamp(now), last_error=None, remote_schema_version=schema,
                     note=(f'published data uses schema {schema}; this install reads {_schema_range()}. The last '
                           'accepted set stays in effect; nothing to run, it is replaced when data for this release is '
                           'published.'))
        return {'status': 'schema_too_old', 'remote_schema_version': schema, 'schema_version': SCHEMA_VERSION}
    if not isinstance(schema, int) or _parse(manifest.get('published_at')) is None:
        raise ValueError('published manifest has no usable schema_version or published_at')
    version = set_version(manifest)
    if state.get('rejected_version') == version and state.get('rejected_by') == reader_id():
        state.update(checked_at=_stamp(now), last_error=None)
        return {'status': 'rejected', 'version': version, 'detail': state.get('note')}
    target = cache / 'sets' / version
    if check_set(target, _read_json(target / MANIFEST))[0] == 'ok':
        _accept(state, manifest, cfg, now)
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
        verdict, detail = check_set(staging, manifest)
        if verdict != 'ok':
            state.update(checked_at=_stamp(now), last_error=None, rejected_version=version, rejected_by=reader_id(),
                         note=(f'published set {version} is not usable by this install ({verdict}: {detail}). '
                               f'The last accepted set stays in effect: {UPDATE_APP}; if it is already the latest '
                               'release, the published data is defective and must be republished.'))
            return {'status': 'rejected', 'version': version, 'detail': detail}
        try:
            os.replace(staging, target)
        except OSError:
            if check_set(target, _read_json(target / MANIFEST))[0] != 'ok':
                raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    previous = state.get('version')
    _accept(state, manifest, cfg, now, fetched_at=_stamp(now))
    _prune(cache / 'sets', {version, previous})
    return {'status': 'updated', 'version': version, 'previous': previous, 'downloaded': downloaded}


def _read_bytes(path):
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def _prune(sets, keep):
    """Keep the current and previous sets; remove other sets and staging directories an interrupted run left."""
    try:
        children = list(sets.iterdir())
    except OSError:
        return
    for child in children:
        if not child.is_dir() or child.name in keep:
            continue
        if child.name.startswith('.staging-'):
            try:
                if time.time() - child.stat().st_mtime < STAGING_MAX_AGE_SECONDS:
                    continue                    # possibly a concurrent run's download in progress
            except OSError:
                continue
        shutil.rmtree(child, ignore_errors=True)


def refresh_in_background(environ=None, bundled=None):
    """Pin this process's set, then start a daemon check when one is due. Returns the thread or None. The run never
    waits for it; process exit waits at most EXIT_GRACE_SECONDS. Nothing here can fail the run that calls it."""
    try:
        pin(bundled, environ)
        cfg = config(environ)
        if not cfg['enabled'] or not cfg['url'] or not due(read_state(environ), cfg):
            return None
    except Exception:
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
    """What doctor, `agentcompass providers` and `agentcompass data` print. Read only."""
    cfg = config(environ)
    chosen = selection(bundled, environ)
    state = read_state(environ)
    notes = list(dict.fromkeys(note for note in (chosen['note'], state.get('note')) if note))  # why this set, and what the last check found
    return {'enabled': cfg['enabled'], 'switch': cfg['switch'], 'source': chosen['source'], 'version': chosen['version'],
            'published_at': chosen['published_at'], 'schema_version': chosen['schema_version'],
            'reads_schema': SCHEMA_VERSION, 'bundled_version': chosen['bundled_version'], 'dir': chosen['dir'],
            'note': '; '.join(notes) or None, 'fetched_at': state.get('fetched_at'),
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
       agentcompass data publish [--check]
       agentcompass data refresh [--no-fetch] [--data DIR] [--report FILE] [--distrust SOURCE] [--model ID ...]

Shows which reference data (accounts.json, plan-templates.json, classifier-cases.json, five-axis-scores.json,
user-scores.json) routing reads: the copy shipped with this install, or a newer set the maintainer published on the
AgentCompass repository's main branch.

--refresh   check the published data now and fetch it when it changed (ignores the check interval). Exits 0 when the
            install keeps a valid set in effect, also offline; 1 only when the state directory cannot be written.

Maintainers (in a git checkout):
  agentcompass data publish          check the reference set and write references/public-data.json (publishes it)
  agentcompass data publish --check  fail when references/public-data.json does not publish the files beside it
  agentcompass data refresh ...      rebuild the calibrated data; the fetching refresh runs once, when a release is prepared
Off switch: AGENTCOMPASS_PUBLIC_DATA=off, or {"enabled": false} in ~/.agent-compass/public-data-config.json.
"""


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if any(arg in ('-h', '--help') for arg in argv):
        sys.stdout.write(USAGE)
        return 0
    if argv[:1] == ['publish']:
        if argv[1:] == ['--check']:
            problem = check_published()
            if problem:
                sys.stderr.write(problem + '\n')
                return 1
            current = _read_json(BUNDLED / MANIFEST)
            print(f"{BUNDLED / MANIFEST} publishes the files beside it ({current['version']}, published "
                  f"{current['published_at']}, schema {current['schema_version']})")
            return 0
        if argv[1:]:
            sys.stderr.write(USAGE)
            return 2
        try:
            fresh = publish()
        except (OSError, ValueError) as exc:
            sys.stderr.write(f'not published: {exc}\n')
            return 1
        print(json.dumps(fresh, indent=2))
        return 0
    if argv and argv[0] not in ('status', '--refresh'):
        sys.stderr.write(USAGE)
        return 2
    if argv[:1] == ['--refresh']:
        outcome = refresh(force=True, timeout=10)
        unpin()
        print(json.dumps({'refresh': outcome, **status()}, ensure_ascii=False, indent=2))
        return 1 if outcome.get('status') == 'state_not_saved' else 0
    print(json.dumps(status(), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
