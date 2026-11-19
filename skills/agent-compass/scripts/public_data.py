"""Published reference data: the maintainer's changes between releases reach installs without a product update.

Every install ships the reference set under references/ (FILES) with references/public-data.json, its manifest: the
sha256 of every file, the data schema and the publication time. The maintainer publishes a set on purpose with
`agentcompass data publish`, which accepts the set (accept: this code's own readers load it and an offline plan
completes with it) and writes it to published/<schema>/ at the repository root, beside nothing else. Editing
references/ publishes nothing. No board is read here: the fetching refresh of the boards runs once, when a release is
prepared (calibration/refresh.py).

An install fetches the manifest of the schema it reads from the repository's main branch over plain HTTPS (no
credentials, proxies from the environment) into <state dir>/public-data/, verifies every hash and runs the same
acceptance before it records the set. A process takes one reference set, the whole of it in memory, the first time it
reads one (held) and keeps it; a long-lived process moves to a newer set only as a whole (retake). A fetched set is used
only when it is complete, accepted by this code and published after the shipped copy. If a reader fails on it at run
time all the same with a data error, before the run had any effect, the run falls back to the shipped set as a whole and
the set is set aside for SET_ASIDE_HOURS (with_fallback); a refusal, an environment failure or an error after an effect
propagates unchanged and blames nothing.

Precedence: shipped < fetched < the user's own settings. Nothing here writes accounts-override.json, the local
user-scores.json or host policy; their loaders layer them on top of whichever set is held. Shipped references that differ
from their own manifest (a checkout with local edits) are used as they are.

Off switch: AGENTCOMPASS_PUBLIC_DATA=off, or {"enabled": false} in <state dir>/public-data-config.json.
Layout and options: ../references/router-internals.md (Reference data).
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

# `agentcompass data` launches this file with `python -I`, and doctor loads it by path. Neither puts the script
# directory on sys.path, so the sibling imports below would fail. Same insertion as route_judge.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import override_store  # noqa: E402
import state_upgrade  # noqa: E402

# The data schema binds the shape of the reference files to the code that reads them: a set of schema N is fetched only
# by an install whose SCHEMA_VERSION is N, from published/N/. Bump it with any change after which an installed reader
# of the previous schema would misread data written for this code (a new required field, a renamed key, a changed
# meaning); `publish --check-released` runs every released reader of this schema against a new publication.
SCHEMA_VERSION = 8
# Schema 7 (2026-11-18, v0.7.0): level 3 is the panel maximum. five-axis-scores.json is `agent-compass.five-axis-scores/6`
# and user-scores.json `schema_version = 3`.
# Schema 8 (2026-11-19, v0.7.1): a value at every (model, effort) a host offers, `none` to `ultra`. five-axis-scores.json
# is `agent-compass.five-axis-scores/12` (`effort_steps`, `size_steps`, `effort_readings`, cells that may carry
# `drift_correction`); accounts.json gains `effort_readings`, `series_versions`, `estimation_order`, the `listed_as`
# and `listed_as_source` board identities and the panel `members`. A schema-7 reader refuses the scores file.
UPDATE_APP = 'update AgentCompass to the latest release to receive new reference data'
# What a run-time reader reads (router-internals.md, Reference data). Left out on purpose: level-3-conversion.json (the
# frozen conversion of the user-scores upgrade step, owned with that code), public-measurements.json (read only by the
# maintainer's refresh) and the generated model-capabilities.{json,md} (documentation views, no run-time reader).
FILES = ('accounts.json', 'plan-templates.json', 'classifier-cases.json', 'five-axis-scores.json', 'user-scores.json')
MANIFEST = 'public-data.json'
CONFIG_FILE = 'public-data-config.json'
INTERVAL_HOURS = 6              # at most one check per interval after a successful one
RETRY_MINUTES = 30              # after a failed (or still running) attempt
TIMEOUT_SECONDS = 5             # per request
LOCK_SECONDS = 10               # waiting for another process's state update
STAGING_MAX_AGE_SECONDS = 3600  # an interrupted download's staging directory is removed after this
FUTURE_TOLERANCE = timedelta(days=1)
MAX_BYTES = 8 * 1024 * 1024
OFF_WORDS = ('0', 'off', 'false', 'no', 'disabled')
BUNDLED = Path(__file__).resolve().parents[1] / 'references'
# The published sets, one directory per data schema, at the repository root (a checkout only; never in a wheel or the
# product, which ship references/).
PUBLISHED = Path(__file__).resolve().parents[3] / 'published'
USER_AGENT = 'AgentCompass-public-data'
# A set a reader failed on at run time is set aside this long, then tried again (at once when a newer set is published or
# this install's code changes). A run-time failure is evidence, not proof (the acceptance is the proof), so the blame
# expires: a truly bad set costs each install one failed-then-shipped run per period.
SET_ASIDE_HOURS = 24
# What a reader raises on data of a shape it does not expect: Python's own lookup, type, attribute and arithmetic errors,
# and ReferenceDataError, which the readers' explicit shape checks raise. Any other ValueError is the router's answer (an
# input error, a refusal such as a dispatch_registry state refusal or named_model_unavailable) and an OSError or sqlite3
# error the environment: neither re-runs anything nor blames a set.
DATA_ERRORS = (LookupError, TypeError, AttributeError, ArithmeticError)


class ReferenceDataError(ValueError):
    """A reference file does not have the shape this code reads: a reader's explicit shape check, or a non-finite
    number. A ValueError so that callers that report input errors report it the same way."""

_LOCK = threading.RLock()
_HELD = {}
_CANDIDATE = []
_FALLBACK = {}
_READER = {}
_EFFECTS = [0]      # effects this process has had (effect()); with_fallback re-runs only what came before the first
_CARRIED = []       # per with_fallback call: the answers of paid calls its first run made, carried into its re-run


def is_data_error(error):
    """Whether ``error`` says the data has a shape a reader does not expect (DATA_ERRORS, ReferenceDataError)."""
    return isinstance(error, (ReferenceDataError, *DATA_ERRORS))


def _non_finite(name):
    def refuse(token):
        raise ReferenceDataError(f'{name} holds {token}, which is not a finite number')
    return refuse


def parse_document(data, name):
    """The one parse of a reference file, shipped or fetched: JSON whose numbers are all finite. NaN, Infinity and a
    literal too large for a float are a named ReferenceDataError, never a value."""
    def number(text):
        value = float(text)
        if value in (float('inf'), float('-inf')):
            raise ReferenceDataError(f'{name} holds {text}, which is not a finite number')
        return value
    try:
        text = data.decode('utf-8-sig') if isinstance(data, (bytes, bytearray)) else data
        return json.loads(text, parse_constant=_non_finite(name), parse_float=number)
    except ReferenceDataError:
        raise
    except ValueError as exc:
        raise ReferenceDataError(f'{name} is not valid JSON: {exc}') from None


def carried(key, call):
    """``call()``, a paid call (a classifier model call), made at most once per with_fallback call: its re-run gets the
    answer the first run got under ``key``, so a fall-back never pays for the same question twice. Outside
    with_fallback it is just ``call()``. ``key`` names the question independently of the reference set."""
    if not _CARRIED:
        return call()
    store = _CARRIED[-1]
    if key not in store:
        store[key] = call()
    return store[key]


def effect(what):
    """Called by every writer whose repetition a re-run would duplicate: a ledger row, a dispatch reservation or row, a
    launch request, an override. From here on a failure of this run propagates as it is and is never re-run on the
    shipped set (with_fallback). ``what`` names the writer, for a reader of the code."""
    with _LOCK:
        _EFFECTS[0] += 1


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


def _read_bytes(path):
    try:
        return override_store.retry_denied(Path(path).read_bytes)
    except OSError:
        return None


def _read_json(path):
    data = _read_bytes(path)
    try:
        value = json.loads(data.decode('utf-8-sig')) if data is not None else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def published_url():
    """The repository's published sets (agentcompass/origin.py PUBLIC_DATA_URL), or None when this skill has no package
    next to it to name them. Found by path: the router scripts do not import the agentcompass package."""
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
    """Effective switch, interval and source; the environment beats the config file. ``url`` is the published set of
    the schema this code reads (<base>/<SCHEMA_VERSION>/)."""
    env = _env(environ)
    switch = str(env.get('AGENTCOMPASS_PUBLIC_DATA') or '').strip().lower()
    if switch in OFF_WORDS:                     # off by the environment: nothing of the state directory is read
        return {'enabled': False, 'switch': 'AGENTCOMPASS_PUBLIC_DATA', 'url': None, 'interval_hours': INTERVAL_HOURS}
    stored = _read_json(state_upgrade.state_dir(environ=environ) / CONFIG_FILE) or {}
    enabled, basis = stored.get('enabled') is not False, CONFIG_FILE if 'enabled' in stored else 'default'
    if switch:
        enabled, basis = True, 'AGENTCOMPASS_PUBLIC_DATA'
    base = str(env.get('AGENTCOMPASS_PUBLIC_DATA_URL') or stored.get('url') or published_url() or '').strip()
    hours = env.get('AGENTCOMPASS_PUBLIC_DATA_INTERVAL_HOURS') or stored.get('interval_hours')
    try:
        hours = max(0.0, float(hours)) if hours not in (None, '') else INTERVAL_HOURS
    except (TypeError, ValueError):
        hours = INTERVAL_HOURS
    base = base if not base or base.endswith('/') else base + '/'
    return {'enabled': enabled, 'switch': basis, 'url': f'{base}{SCHEMA_VERSION}/' if base else None,
            'interval_hours': hours}


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def set_version(manifest):
    """Content id of a manifest: the schema and each file's hash."""
    files = manifest.get('files') or {}
    lines = [f"schema {manifest.get('schema_version')}"] + [f"{name} {(files.get(name) or {}).get('sha256')}" for name in FILES]
    return sha256_bytes('\n'.join(lines).encode('utf-8'))[:12]


def build_manifest(directory, previous=None, now=None):
    """Manifest for the files in ``directory``. ``published_at`` moves only when the content changes, and then to a
    time later than the publication it replaces."""
    directory = Path(directory)
    files = {}
    for name in FILES:
        data = (directory / name).read_bytes()
        parse_document(data, name)
        files[name] = {'sha256': sha256_bytes(data), 'bytes': len(data)}
    manifest = {'schema_version': SCHEMA_VERSION, 'files': files}
    version = set_version(manifest)
    before = _parse((previous or {}).get('published_at')) if isinstance(previous, dict) else None
    if before is not None and previous.get('version') == version:
        published = previous['published_at']
    else:
        moment = (now or _now()).replace(microsecond=0)
        published = _stamp(max(moment, before + timedelta(seconds=1)) if before is not None else moment)
    return {'schema_version': SCHEMA_VERSION, 'version': version, 'published_at': published, 'files': files}


def _manifest_bytes(manifest):
    return (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def validate_files(directory):
    """Raise ValueError naming the file when a file of the set fails the provenance checks (reference_provenance.py,
    which also holds user-scores.json to its schema), the five-axis score schema or the classifier cases' sections."""
    import axis_scores
    import reference_provenance
    directory = Path(directory)
    for name in FILES:
        parse_document((directory / name).read_bytes(), name)
    reference_provenance.validate_references(directory)
    try:
        axis_scores.require_current_schema(json.loads((directory / 'five-axis-scores.json').read_text(encoding='utf-8-sig')))
    except ValueError as exc:
        raise ValueError(f'five-axis-scores.json is invalid: {exc}') from None
    cases = json.loads((directory / 'classifier-cases.json').read_text(encoding='utf-8-sig'))
    missing = [key for key in ('route_definitions', 'dimension_rubric', 'calibration_cases') if not cases.get(key)]
    if missing:
        raise ValueError(f'classifier-cases.json is invalid: no {", ".join(missing)}')


def _acceptance_steps():
    """(label, step) for accept: every run-time reader of the set, then an offline plan on the reference fleet (what
    `plan --demo --dry-run` runs) and one on the set's own fleet priced as the reference fleet prices its families
    (demo_plan.priced_fleet), which must have a selectable candidate; both serialized as the CLI prints them. Read by
    the candidate set; no network, no classifier."""
    import account_fleet
    import axis_scores
    import demo_plan
    import provider_inventory
    import route_judge
    import scorer_client
    import user_scores
    import worker_sessions

    def fleet():
        return document(account_fleet.REFERENCES / 'accounts.json')

    def planned(result, selectable=False):
        route_judge.render(result)              # what the CLI prints: strict JSON, no NaN or Infinity
        if selectable and not any(not c.get('excluded') for c in result.get('ranking') or []):
            raise ValueError(f"no selectable candidate among {len(result.get('ranking') or [])}")

    def classifier_cases():
        cases = scorer_client.load_classifier_cases()
        missing = [k for k in ('route_definitions', 'dimension_rubric', 'calibration_cases') if not cases.get(k)]
        if missing:
            raise ValueError(f'the classifier reads no {", ".join(missing)}')
        scorer_client.scorer_evidence(demo_plan.sample_document()['task'])

    return (
        ('accounts.json launch values', lambda: _raise_problems(account_fleet.launch_problems(fleet()))),
        ('accounts.json through load_accounts', lambda: account_fleet.load_accounts(document=fleet())),
        ('scoring', lambda: worker_sessions.rotation_threshold(worker_sessions.shipped_scoring())),
        ('five-axis-scores.json', axis_scores.load_five_axis),
        ('user-scores.json', user_scores.load_public),
        ('classifier-cases.json', classifier_cases),
        ('plan-templates.json', lambda: provider_inventory.plan_table(fleet(), 'codex')),
        ('plan on the reference fleet', lambda: planned(demo_plan.plan(demo_plan.sample_document()))),
        ('plan on the fleet of the set', lambda: planned(
            demo_plan.plan(demo_plan.sample_document(), fleet=demo_plan.priced_fleet(fleet())), selectable=True)),
    )


def _raise_problems(problems):
    if problems:
        raise ValueError('; '.join(problems[:5]))


def accept(directory):
    """Raise ValueError naming the step and the cause when this code cannot route with the set in ``directory``; the one
    acceptance of a reference set, run by `data publish` and by an install before it records a fetched set. Costs about
    a second (the loaders and two offline plans); an install runs it once per newly published set, in its background
    check, never on a caller's path."""
    directory = Path(directory)
    validate_files(directory)
    with candidate(directory):
        for label, step in _acceptance_steps():
            try:
                step()
            except Exception as exc:            # whatever a reader raises on this set refuses the set, never the caller
                raise ValueError(f'{label}: {type(exc).__name__}: {str(exc)[:300]}') from None


def check_set(directory, manifest):
    """('ok' | 'no_manifest' | 'schema' | 'incomplete' | 'modified', detail): the manifest is of the schema this code
    reads, has a publication time, lists every file, and every file in ``directory`` matches its sha256."""
    if not isinstance(manifest, dict):
        return 'no_manifest', f'no {MANIFEST}'
    schema = manifest.get('schema_version')
    if schema != SCHEMA_VERSION or isinstance(schema, bool):
        return 'schema', f'data schema {schema!r}; this install reads schema {SCHEMA_VERSION}'
    if _parse(manifest.get('published_at')) is None:
        return 'incomplete', 'manifest has no published_at'
    files = manifest.get('files') if isinstance(manifest.get('files'), dict) else {}
    for name in FILES:
        want = (files.get(name) or {}).get('sha256')
        if not isinstance(want, str):
            return 'incomplete', f'{name} is not in the manifest'
        data = _read_bytes(Path(directory) / name)
        if data is None:
            return 'incomplete', f'{name} is missing'
        if sha256_bytes(data) != want:
            return 'modified', f'{name} does not match its sha256'
    return 'ok', ''


def published_dir(root=None, schema=SCHEMA_VERSION):
    return Path(root or PUBLISHED) / str(schema)


def publish(references=None, root=None, now=None):
    """The one act that publishes a reference set: accept the set in ``references``, then write it with its manifest to
    published/<schema>/ and the manifest to references/public-data.json (what a release ships). An unchanged set
    keeps both byte-identical, `published_at` included."""
    references = Path(references or BUNDLED)
    accept(references)
    target = published_dir(root)
    previous = _read_json(target / MANIFEST)
    fresh = build_manifest(references, previous, now=now)
    data = _manifest_bytes(fresh)
    for name in FILES:
        content = (references / name).read_bytes()
        if _read_bytes(target / name) != content:
            _write_bytes(target / name, content)
    for path in (target / MANIFEST, references / MANIFEST):
        if _read_bytes(path) != data:
            _write_bytes(path, data)
    return fresh


def check_published(root=None, references=None, previous=None, release=False, now=None):
    """'' when published/<schema>/ is a set this code accepts, consistent with its manifest and published later than
    ``previous`` (the manifest it replaces); with ``release`` also when it is what references/ ships. Else what is wrong
    (the public-data workflow and the release job)."""
    target = published_dir(root)
    references = Path(references or BUNDLED)
    manifest = _read_json(target / MANIFEST)
    if manifest is None:
        return f'{target / MANIFEST} is missing: nothing is published for schema {SCHEMA_VERSION}; run: agentcompass data publish'
    try:
        fresh = build_manifest(target, manifest)
    except (OSError, ValueError) as exc:
        return f'the published set in {target} is not readable: {exc}'
    if fresh != manifest:
        changed = [n for n in FILES if (manifest.get('files') or {}).get(n) != fresh['files'][n]]
        what = f"{', '.join(changed)} changed" if changed else f'its manifest is not schema {SCHEMA_VERSION}'
        return f'{target} is not consistent ({what}); only `agentcompass data publish` writes it'
    when = _parse(manifest['published_at'])
    if when > (now or _now()) + FUTURE_TOLERANCE:
        return f"{target / MANIFEST} is published in the future ({manifest['published_at']})"
    before = _parse((previous or {}).get('published_at')) if isinstance(previous, dict) else None
    if before is not None and previous.get('version') != manifest['version'] and when <= before:
        return (f"{target / MANIFEST} is published at {manifest['published_at']}, not later than the publication it replaces "
                f"({previous['published_at']}): installs would ignore it; run: agentcompass data publish")
    try:
        accept(target)
    except (OSError, ValueError) as exc:
        return f'this code does not accept the published set in {target}: {exc}'
    if release:
        differ = [n for n in (*FILES, MANIFEST) if _read_bytes(references / n) != _read_bytes(target / n)]
        if differ:
            return (f'a release ships references/ as the latest published set of schema {SCHEMA_VERSION}, but '
                    f'{", ".join(differ)} differ from {target}; run: agentcompass data publish')
    return ''


def _git(*args, cwd=None):
    done = subprocess.run(['git', *args], cwd=cwd or PUBLISHED.parent, capture_output=True, timeout=120)
    return done.stdout if done.returncode == 0 else None


def manifest_at(revision):
    """The published manifest of this schema at a git ``revision``, or None when there was none. A revision that is
    not a commit of this repository (unknown, or unreachable after a force push) is a ValueError: the check against
    the previous publication cannot run, and that is never a pass. A commit id this checkout does not have (the
    workflow's base after a force push to main) says so and what to run, so the failure is not read as bad data."""
    if _git('rev-parse', '--verify', '--quiet', f'{revision}^{{commit}}') is None:
        if re.fullmatch(r'[0-9a-fA-F]{7,64}', revision or ''):
            raise ValueError(
                f'--since {revision}: the base commit is not in this checkout (a force push to main removed it, or the '
                'checkout is shallow), so the published set cannot be checked against the publication it replaces; '
                'this says nothing about the published data. Fetch that commit if it still exists '
                f'(git fetch origin {revision}), or run the check against the commit main had before the push: '
                'agentcompass data publish --check --since <that commit>')
        raise ValueError(f'--since {revision}: not a commit of this repository (unknown or unreachable revision); the '
                         'published set cannot be checked against the publication it replaces')
    data = _git('show', f'{revision}:published/{SCHEMA_VERSION}/{MANIFEST}')
    try:
        return json.loads(data.decode('utf-8-sig')) if data else None
    except ValueError:
        return None


def check_frozen(root=None, since=None):
    """'' when every published set of an older schema (published/<n>/, n != SCHEMA_VERSION) still matches its own
    manifest and, with ``since`` (a revision), is unchanged since then: an older schema's set is frozen, never edited, for
    installs of that schema keep reading it. Else what is wrong."""
    root = Path(root or PUBLISHED)
    for folder in sorted(p for p in (root.iterdir() if root.is_dir() else ()) if p.is_dir() and p.name.isdigit()):
        if folder.name == str(SCHEMA_VERSION):
            continue
        manifest = _read_json(folder / MANIFEST)
        listed = (manifest or {}).get('files') if isinstance((manifest or {}).get('files'), dict) else None
        if not listed:
            return f'{folder} has no readable {MANIFEST}: a published set of an older schema is frozen'
        for name, entry in listed.items():
            data = _read_bytes(folder / name)
            if data is None or sha256_bytes(data) != (entry or {}).get('sha256'):
                return f'{folder / name} does not match its manifest: a published set of an older schema is frozen, never edited'
        if since is not None:
            done = subprocess.run(['git', 'diff', '--quiet', since, '--', f'published/{folder.name}'], cwd=root.parent,
                                  capture_output=True, timeout=120)
            if done.returncode != 0:
                return (f'{folder} changed since {since}: a published set of an older schema is frozen, never edited '
                        f'(installs of schema {folder.name} keep reading it)')
    return ''


def check_released(root=None):
    """Run the acceptance of every released version that reads this schema (git tags v*) against the published set:
    what those installs will run. Returns (problems, checked tags)."""
    target = published_dir(root).resolve()
    problems, checked = [], []
    for tag in (_git('tag', '--list', 'v*') or b'').decode().split():
        source = _git('show', f'{tag}:skills/agent-compass/scripts/public_data.py')
        if not source or b'\ndef accept(' not in source or f'\nSCHEMA_VERSION = {SCHEMA_VERSION}\n'.encode() not in source:
            continue
        archive = _git('archive', '--format=tar', tag, 'skills/agent-compass', 'agentcompass/origin.py')
        if archive is None:
            problems.append(f'{tag}: git archive failed')
            continue
        with tempfile.TemporaryDirectory(prefix='agentcompass-released-') as folder:
            with tarfile.open(fileobj=__import__('io').BytesIO(archive)) as tar:
                tar.extractall(folder, filter='data')
            script = Path(folder) / 'skills' / 'agent-compass' / 'scripts' / 'public_data.py'
            done = subprocess.run([sys.executable, '-I', '-X', 'utf8', str(script), 'accept', str(target)],
                                  capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300,
                                  env={**os.environ, 'AGENTCOMPASS_PUBLIC_DATA': 'off'})
        checked.append(tag)
        if done.returncode != 0:
            problems.append(f'{tag} does not accept it: {(done.stderr or done.stdout).strip()[-400:]}')
    return problems, checked


def read_state(environ=None):
    return _read_json(cache_dir(environ) / 'state.json') or {}


def _update_state(environ, change):
    """Read, change and write state.json under the process-safe lock (override_store); returns change(state)."""
    path = cache_dir(environ) / 'state.json'
    with override_store.locked(path, timeout=LOCK_SECONDS):
        state = _read_json(path) or {}
        result = change(state)
        override_store.write(path, state)
    return result


def _write_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex[:8]}.tmp')
    temporary.write_bytes(data)
    override_store.retry_denied(lambda: os.replace(temporary, path))


def reader_id():
    """Content hash of the code that reads a reference set (every router script except tests). A set is recorded as
    accepted or rejected by this id, so an install whose code changed accepts a fetched set again before using it."""
    if 'id' not in _READER:
        digest = hashlib.sha256()
        here = Path(__file__).resolve().parent
        for path in sorted(here.glob('*.py')):
            if not path.name.startswith(('test_', 'conftest')):
                digest.update(path.name.encode() + b'\0' + path.read_bytes())
        _READER['id'] = digest.hexdigest()[:16]
    return _READER['id']


def _content_id(files):
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode() + b'\0' + sha256_bytes(files[name]).encode())
    return digest.hexdigest()[:12]


def _shipped_set(bundled, note=None, enabled=True):
    manifest = _read_json(bundled / MANIFEST)
    files = {name: data for name in FILES for data in [_read_bytes(bundled / name)] if data is not None}
    return {'source': 'bundled', 'dir': str(bundled), 'enabled': enabled, 'note': note,
            'version': (manifest or {}).get('version'), 'published_at': (manifest or {}).get('published_at'),
            'schema_version': (manifest or {}).get('schema_version'), 'bundled_version': (manifest or {}).get('version'),
            'files': files, 'id': f'bundled:{_content_id(files)}'}


def _take(bundled, environ):
    """The reference set a process holds: every file read once, verified in memory, so nothing on disk can split it."""
    cfg = config(environ)
    shipped = _shipped_set(bundled, enabled=cfg['enabled'])
    manifest = _read_json(bundled / MANIFEST)
    if manifest is None:
        return {**shipped, 'note': f'no {MANIFEST} beside the shipped references; they are used as they are'}
    if not cfg['enabled']:
        return {**shipped, 'note': f"published data channel is off ({cfg['switch']})"}
    edited = [n for n in FILES if n not in shipped['files']
              or sha256_bytes(shipped['files'][n]) != ((manifest.get('files') or {}).get(n) or {}).get('sha256')]
    if edited:
        return {**shipped, 'note': f'shipped references have local edits ({", ".join(edited)}); they are used as they are'}
    state = read_state(environ)
    version = state.get('version')
    if not isinstance(version, str) or not version.isalnum():
        return {**shipped, 'note': 'nothing fetched yet'}
    schema = _held_schema(state, environ)
    if schema != SCHEMA_VERSION:
        _drop_other_schema(environ)
        return {**shipped, 'note': (f'the shipped set is used: the fetched set {version} of data schema {schema!r} (left by '
                                    f'an earlier version) was removed, since this install reads schema {SCHEMA_VERSION}')}
    if state.get('rejected_version') == version and state.get('rejected_by') == reader_id():
        return {**shipped, 'note': state.get('note') or f'fetched set {version} was rejected by this install'}
    if state.get('accepted_by') != reader_id():
        return {**shipped, 'note': f'fetched set {version} is not yet accepted by this version of the code; the next check does'}
    aside = set_aside(state)
    if aside is not None and aside['version'] == version:
        return {**shipped, 'note': aside['note']}
    if _ended_set_aside(state):
        _clear_set_aside(environ)               # ended: cleared where it is read, so nothing still reports it
    directory = cache_dir(environ) / 'sets' / version
    fetched = _read_json(directory / MANIFEST)
    files = {name: data for name in FILES for data in [_read_bytes(directory / name)] if data is not None}
    listed = (fetched or {}).get('files') if isinstance((fetched or {}).get('files'), dict) else {}
    if (fetched is None or fetched.get('schema_version') != SCHEMA_VERSION or set_version(fetched) != version
            or set(files) != set(FILES)
            or any(sha256_bytes(files[n]) != (listed.get(n) or {}).get('sha256') for n in FILES)):
        return {**shipped, 'note': f'fetched set {version} is not complete on disk; the shipped set is used'}
    if _parse(fetched.get('published_at')) is None or _parse(fetched['published_at']) <= _parse(manifest.get('published_at')):
        return {**shipped, 'note': 'shipped references are as new as the fetched set'}
    return {**shipped, 'source': 'fetched', 'dir': str(directory), 'version': version, 'note': None,
            'published_at': fetched['published_at'], 'schema_version': fetched['schema_version'], 'files': files,
            'id': f'fetched:{version}'}


def _held_schema(state, environ):
    """The data schema of the fetched set state.json records: as recorded, else as its manifest on disk declares."""
    schema = state.get('schema_version')
    if schema is None:
        schema = (_read_json(cache_dir(environ) / 'sets' / state['version'] / MANIFEST) or {}).get('schema_version')
    return schema


def _drop_other_schema(environ):
    """Remove a fetched set of a data schema this code does not read, where it is read: the state that records it and
    every cached set not of SCHEMA_VERSION. It is derived state (a cache an earlier version's channel wrote), rebuilt by
    the next check, never migrated. Best effort: a set that stays is still never taken."""
    cache = cache_dir(environ)

    def change(state):
        if isinstance(state.get('version'), str) and state['version'].isalnum() and                 _held_schema(state, environ) != SCHEMA_VERSION:
            for key in STATE_SET_KEYS:
                state.pop(key, None)
        try:
            folders = list((cache / 'sets').iterdir())
        except OSError:
            folders = []
        for folder in folders:
            if folder.is_dir() and not folder.name.startswith('.staging-') and                     (_read_json(folder / MANIFEST) or {}).get('schema_version') != SCHEMA_VERSION:
                shutil.rmtree(folder, ignore_errors=True)
    try:
        _update_state(environ, change)
    except (OSError, TimeoutError):
        pass


def _key(bundled, environ):
    """Which held set: the shipped directory and the state location as configured (resolved only when taken)."""
    return (str(Path(bundled or BUNDLED)), str(_env(environ).get('ROUTER_STATE_DIR') or ''))


def held(bundled=None, environ=None):
    """The reference set this process reads: taken whole the first time and kept (retake moves to a newer one). Inside
    accept(), the candidate set instead."""
    if _CANDIDATE:
        return _CANDIDATE[-1]
    key = _key(bundled, environ)
    with _LOCK:
        if key not in _HELD:
            _HELD[key] = _take(Path(key[0]), environ)
        return _HELD[key]


def document(path, environ=None):
    """The one accessor of the reference set: the parsed document of reference file ``path`` (references/<name>) from
    the set this process holds. Each call parses afresh, so a caller may change what it gets."""
    path = Path(path)
    if path.name not in FILES:
        raise ValueError(f'{path.name} is not a file of the reference set ({", ".join(FILES)})')
    data = held(path.parent, environ)['files'].get(path.name)
    if data is None:                            # a shipped directory without this file: the error names the path
        data = path.read_bytes()
    return parse_document(data, path.name)


def set_id(bundled=None, environ=None):
    """Content id of the held set, for caches of what is derived from it."""
    return held(bundled, environ)['id']


def retake(bundled=None, environ=None):
    """Move this process to the set it would take now, as a whole (the tray at its defined point); without ``bundled``,
    every set it holds."""
    with _LOCK:
        if bundled is None:
            _HELD.clear()
        else:
            _HELD.pop(_key(bundled, environ), None)
    return held(bundled, environ)


def release():
    """Forget every held set (tests, and `agentcompass data` after a refresh)."""
    with _LOCK:
        _HELD.clear()
        _FALLBACK.clear()


@contextmanager
def candidate(directory):
    """Hold the set in ``directory`` (all of it, read now) for the readers called inside; accept() only."""
    directory = Path(directory)
    files = {name: data for name in FILES for data in [_read_bytes(directory / name)] if data is not None}
    missing = [n for n in FILES if n not in files]
    if missing:
        raise ValueError(f'the set in {directory} has no {", ".join(missing)}')
    with _LOCK:
        _CANDIDATE.append({'source': 'candidate', 'dir': str(directory), 'files': files,
                           'id': f'candidate:{_content_id(files)}:{uuid.uuid4().hex[:6]}'})
    try:
        yield
    finally:
        with _LOCK:
            _CANDIDATE.pop()


def fall_back(error, bundled=None, environ=None):
    """After a reader raised ``error``: True when the held set was a fetched one, which this process now replaces with
    the shipped set as a whole so the run can be repeated. A second failure while a fall-back is pending marks it, so
    settle() never blames the fetched set for a failure the shipped set produces as well."""
    key = _key(bundled, environ)
    with _LOCK:
        pending = _FALLBACK.get(key)
        if pending is not None:
            pending['failed_again'] = True
            return False
        chosen = _HELD.get(key)
        if _CANDIDATE or chosen is None or chosen['source'] != 'fetched' or not is_data_error(error):
            return False
        _FALLBACK[key] = {'version': chosen['version'], 'error': f'{type(error).__name__}: {str(error)[:200]}',
                          'failed_again': False}
        _HELD[key] = _shipped_set(Path(key[0]), note=f"fetched set {chosen['version']} failed this run; the shipped set is used")
    return True


def set_aside(state, now=None):
    """The set-aside in effect for this code, or None: recorded by this code (`set_aside_by`), begun no later than now
    (a start in the future is a clock that ran ahead, not a set-aside: the rule for a check time in the future), no
    longer than SET_ASIDE_HOURS (what settle writes), and not yet over. ``{version, at, until, error, note}``."""
    now = now or _now()
    at, until = _parse(state.get('set_aside_at')), _parse(state.get('set_aside_until'))
    if (not state.get('set_aside_version') or state.get('set_aside_by') != reader_id() or at is None or until is None
            or at > now or now >= until or until - at > timedelta(hours=SET_ASIDE_HOURS)):
        return None
    return {'version': state['set_aside_version'], 'at': state['set_aside_at'], 'until': state['set_aside_until'],
            'error': state.get('set_aside_error'), 'note': state.get('set_aside_note')}


SET_ASIDE_KEYS = ('set_aside_version', 'set_aside_by', 'set_aside_at', 'set_aside_until', 'set_aside_error',
                  'set_aside_note')
# Everything state.json says about a fetched set and the checks that found it: dropped with a set of another schema, so
# the next check is due at once.
STATE_SET_KEYS = ('version', 'published_at', 'schema_version', 'fetched_at', 'checked_at', 'attempt_at', 'accepted_by',
                  'url', 'note', 'last_error', 'remote_schema_version', 'rejected_version', 'rejected_by', *SET_ASIDE_KEYS)


def _ended_set_aside(state, now=None):
    """A recorded set-aside that holds for no code any more: this code's that is not in effect, or anyone's whose
    period is over. Another code's set-aside still running is left to that code (installs may share the state)."""
    if not state.get('set_aside_version') or set_aside(state, now) is not None:
        return False
    until = _parse(state.get('set_aside_until'))
    return state.get('set_aside_by') == reader_id() or until is None or (now or _now()) >= until


def _clear_set_aside(environ):
    """Drop a set-aside that has ended (best effort: one that stays is still ignored by set_aside())."""
    def change(state):
        if _ended_set_aside(state):
            for key in SET_ASIDE_KEYS:
                state.pop(key, None)
    try:
        _update_state(environ, change)
    except (OSError, TimeoutError):
        pass


def settle(bundled=None, environ=None):
    """Close a pending fall-back: when the shipped set succeeded where the fetched one failed, set the fetched set aside
    for SET_ASIDE_HOURS (doctor names it with the error); it is taken again after that, or at once when a newer set is
    published or this install's code changes. Not a permanent rejection: only the acceptance proves a set unusable."""
    key = _key(bundled, environ)
    with _LOCK:
        pending = _FALLBACK.pop(key, None)
    if pending is None or pending['failed_again']:
        return None
    at = _now().replace(microsecond=0)
    until = _stamp(at + timedelta(hours=SET_ASIDE_HOURS))
    note = (f"published set {pending['version']} failed this install's readers at run time ({pending['error']}); it is "
            f'set aside until {until} and the shipped set is used. It is tried again then, or at once when a newer set is '
            f'published or this install is updated')

    def change(state):
        state.update(set_aside_version=pending['version'], set_aside_by=reader_id(), set_aside_at=_stamp(at),
                     set_aside_until=until, set_aside_error=pending['error'], set_aside_note=note)
    try:
        _update_state(environ, change)
    except (OSError, TimeoutError):
        pass                                    # the next run fails over again and records it
    return note


def with_fallback(call, *args, **kwargs):
    """Run ``call``. When a reader fails on a fetched set with a data error (is_data_error) before the run had any
    effect (effect()), run it again on the shipped set as a whole, then settle(). After an effect, for a refusal or an
    environment failure, the error propagates unchanged and nothing is re-run or blamed."""
    before = _EFFECTS[0]
    _CARRIED.append({})
    try:
        try:
            result = call(*args, **kwargs)
        except Exception as exc:
            if _EFFECTS[0] != before or not fall_back(exc):
                settle()
                raise
            try:
                result = call(*args, **kwargs)
            except Exception as again:
                fall_back(again)
                settle()
                raise
        settle()
        return result
    finally:
        _CARRIED.pop()


def due(state, cfg, now=None):
    """Whether a check is due. A recorded time in the future (a clock that ran ahead) is no check at all."""
    now = now or _now()
    checked, attempt = (_parse(state.get(key)) for key in ('checked_at', 'attempt_at'))
    checked = checked if checked is not None and checked <= now else None
    attempt = attempt if attempt is not None and attempt <= now else None
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


def _claim(cfg, now, force):
    def change(state):
        if not force and not due(state, cfg, now):
            return {'status': 'not_due', 'checked_at': state.get('checked_at'), 'version': state.get('version')}
        state['attempt_at'] = _stamp(now)
        return None
    return change


def refresh(force=False, environ=None, fetch=None, now=None, bundled=None, timeout=TIMEOUT_SECONDS, claimed=False):
    """Check the published manifest, fetch and accept a changed set. Returns a status dict; never raises. 'busy' (another
    process holds the state) is no failure; 'state_not_saved' (the state directory is not writable) is the one."""
    cfg = config(environ)
    if not cfg['enabled']:
        return {'status': 'disabled', 'switch': cfg['switch']}
    if not cfg['url']:
        return {'status': 'unavailable', 'detail': 'no published location: agentcompass/origin.py is not beside this skill'}
    fetch = fetch or http_get
    now = now or _now()
    bundled = Path(bundled or BUNDLED)
    cache = cache_dir(environ)
    try:
        early = None if claimed else _update_state(environ, _claim(cfg, now, force))
    except TimeoutError:
        return {'status': 'busy', 'detail': 'another process is updating the reference data state'}
    except OSError as error:
        return {'status': 'state_not_saved', 'error': str(error)}
    if early:
        return early
    try:
        result, changes, drop = _refresh(cfg, cache, read_state(environ), fetch, now, bundled, timeout)
    except Exception as error:
        message = f'{type(error).__name__}: {str(error)[:300]}'
        result, changes, drop = {'status': 'failed', 'error': message}, {'last_error': message}, ()

    def change(state):
        state.update(changes)
        for key in drop:
            state.pop(key, None)
    try:
        _update_state(environ, change)
    except TimeoutError:
        result = {**result, 'state': 'busy: recorded by the next check'}
    except OSError as error:
        result = {'status': 'state_not_saved', 'error': str(error)}
    return result


def _schema_note(schema):
    if schema > SCHEMA_VERSION:
        return f'published data uses schema {schema}; this install reads {SCHEMA_VERSION}. The last accepted set stays: {UPDATE_APP}.'
    return f'the published set of schema {SCHEMA_VERSION} declares schema {schema}; it is not read'


def _refresh(cfg, cache, state, fetch, now, bundled, timeout):
    """(result, state changes, state keys to drop) of one check; the caller writes the state under the lock."""
    checked = {'checked_at': _stamp(now), 'last_error': None}
    try:
        raw = fetch(cfg['url'] + MANIFEST, timeout)
    except urllib.error.URLError as error:
        # Nothing published for this schema yet (the normal state before its first publication): a completed check.
        missing = (getattr(error, 'code', None) == 404 if isinstance(error, urllib.error.HTTPError)
                   else isinstance(error.reason, FileNotFoundError))
        if not missing:
            raise
        return {'status': 'unpublished', 'detail': f'nothing is published for schema {SCHEMA_VERSION} yet'}, checked, ()
    manifest = json.loads(raw.decode('utf-8-sig'))
    if not isinstance(manifest, dict) or not isinstance(manifest.get('files'), dict):
        raise ValueError('published manifest has no files table')
    schema = manifest.get('schema_version')
    if isinstance(schema, int) and not isinstance(schema, bool) and schema != SCHEMA_VERSION:
        return ({'status': 'schema_too_new' if schema > SCHEMA_VERSION else 'schema_mismatch', 'remote_schema_version': schema,
                 'schema_version': SCHEMA_VERSION}, {**checked, 'remote_schema_version': schema, 'note': _schema_note(schema)}, ())
    if not isinstance(schema, int) or _parse(manifest.get('published_at')) is None:
        raise ValueError('published manifest has no usable schema_version or published_at')
    version = set_version(manifest)
    if state.get('rejected_version') == version and state.get('rejected_by') == reader_id():
        return {'status': 'rejected', 'version': version, 'detail': state.get('note')}, checked, ()
    target = cache / 'sets' / version
    if check_set(target, _read_json(target / MANIFEST))[0] == 'ok':
        if state.get('version') == version and state.get('accepted_by') == reader_id():
            return {'status': 'unchanged', 'version': version}, checked, ('remote_schema_version',)
        verdict = _accepted(target, version)
        if verdict:
            return verdict
        return _accepted_result(manifest, cfg, now, state, version, [], 'accepted', cache)
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
            (staging / name).write_bytes(data)   # parsed strictly by the acceptance: a defect of this version rejects it
        (staging / MANIFEST).write_bytes(raw)
        verdict = _accepted(staging, version)
        if verdict:
            return verdict
        try:
            os.replace(staging, target)
        except OSError:
            if check_set(target, _read_json(target / MANIFEST))[0] != 'ok':
                raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return _accepted_result(manifest, cfg, now, state, version, downloaded, 'updated', cache)


def _accepted(directory, version):
    """None when this code accepts the set in ``directory``, else the rejection (result, changes, drop)."""
    try:
        accept(directory)
    except (OSError, ValueError) as exc:
        note = (f'published set {version} is not usable by this install ({exc}). The last accepted set stays in effect: '
                f'{UPDATE_APP}; if it is already the latest release, the published data is defective and must be republished.')
        return ({'status': 'rejected', 'version': version, 'detail': str(exc)},
                {'checked_at': _stamp(_now()), 'last_error': None, 'rejected_version': version, 'rejected_by': reader_id(),
                 'note': note}, ())
    return None


def _accepted_result(manifest, cfg, now, state, version, downloaded, status, cache):
    previous = state.get('version')
    changes = {'version': version, 'published_at': manifest['published_at'], 'schema_version': manifest['schema_version'],
               'checked_at': _stamp(now), 'last_error': None, 'note': None, 'url': cfg['url'], 'accepted_by': reader_id()}
    if status == 'updated':
        changes['fetched_at'] = _stamp(now)
    _prune(cache / 'sets', {version, previous})
    return ({'status': status, 'version': version, 'previous': previous, 'downloaded': downloaded}, changes,
            ('remote_schema_version', 'rejected_version', 'rejected_by', *SET_ASIDE_KEYS))


def _prune(sets, keep):
    """Keep the current and previous sets; remove other sets and staging directories an interrupted run left. A process
    holding a removed set is unaffected: it read the whole set when it took it."""
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


def refresh_in_background(environ=None):
    """Start a due check in its own short-lived process (`public_data.py --refresh --claimed`) and return it, or None.
    The caller never waits for it: it pays one state read and, when a check is due, one locked state write and the
    process start. The child is bounded by TIMEOUT_SECONDS per request and the acceptance. Nothing here can fail the
    run that calls it."""
    try:
        cfg = config(environ)
        if not cfg['enabled'] or not cfg['url'] or not due(read_state(environ), cfg):
            return None
        if _update_state(environ, _claim(cfg, _now(), False)):
            return None                         # another process claimed it first
        flags = 0x00000008 | 0x00000200 | 0x08000000 if os.name == 'nt' else 0  # detached, own group, no window
        return subprocess.Popen([sys.executable, '-I', '-X', 'utf8', str(Path(__file__).resolve()), '--refresh', '--claimed'],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env=dict(_env(environ)), creationflags=flags, close_fds=True,
                                start_new_session=os.name != 'nt')
    except Exception:
        return None


def reference_alert(bundled=None, environ=None):
    """The plan's warning when the published set is not used because this code refused it (the acceptance rejected it,
    or a run-time failure set it aside), naming why; None otherwise. Nothing is left out silently."""
    chosen = held(bundled, environ)
    if chosen['source'] == 'fetched' or not chosen.get('enabled'):
        return None
    state = read_state(environ)
    aside = set_aside(state)
    if aside is not None:
        message = aside['note']
    elif state.get('rejected_version') and state.get('rejected_by') == reader_id():
        message = state.get('note')
    else:
        return None
    return {'code': 'reference_data_refused', 'severity': 'warn', 'account': None,
            'message': message or 'the published reference set was refused by this install; the shipped set is used'}


def status(environ=None, bundled=None):
    """What doctor, `agentcompass providers` and `agentcompass data` print. Read only."""
    cfg = config(environ)
    chosen = held(bundled, environ)
    state = read_state(environ)
    notes = list(dict.fromkeys(note for note in (chosen['note'], state.get('note')) if note))  # why this set, and what the last check found
    return {'enabled': cfg['enabled'], 'switch': cfg['switch'], 'source': chosen['source'], 'version': chosen['version'],
            'published_at': chosen['published_at'], 'schema_version': chosen['schema_version'],
            'reads_schema': SCHEMA_VERSION, 'bundled_version': chosen['bundled_version'], 'dir': chosen['dir'],
            'note': '; '.join(notes) or None, 'fetched_at': state.get('fetched_at'),
            'checked_at': state.get('checked_at'), 'last_error': state.get('last_error'), 'url': cfg['url'],
            'set_aside': ({k: v for k, v in aside.items() if k != 'note'} if (aside := set_aside(state)) else None),
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
       agentcompass data publish [--check [--since REV] [--release] | --check-released]
       agentcompass data refresh [--no-fetch] [--data DIR] [--report FILE] [--distrust SOURCE] [--model ID ...]

Shows which reference data (accounts.json, plan-templates.json, classifier-cases.json, five-axis-scores.json,
user-scores.json) routing reads: the copy shipped with this install, or a newer set the maintainer published for this
install's data schema on the AgentCompass repository's main branch.

--refresh   check the published data now, fetch and accept it when it changed (ignores the check interval). Exits 0
            when the install keeps a valid set in effect, also offline or when another process is checking; 1 only
            when the state directory cannot be written.

Maintainers (in a git checkout):
  agentcompass data publish          accept references/ and write it to published/<schema>/ (publishes it)
  agentcompass data publish --check  fail unless published/<schema>/ is consistent, accepted by this code and, with
                                     --since REV (a commit; any other revision fails), published later than at REV;
                                     --release: and what references/ ships. Sets of older schemas are frozen.
  agentcompass data publish --check-released
                                     run every released reader of this schema (tags v*) against the published set
  agentcompass data refresh ...      rebuild the calibrated data; the fetching refresh runs once, when a release is prepared
Off switch: AGENTCOMPASS_PUBLIC_DATA=off, or {"enabled": false} in ~/.agent-compass/public-data-config.json.
"""


def _publish_main(args):
    if args == ['--check-released']:
        problems, checked = check_released()
        for line in problems:
            sys.stderr.write(line + '\n')
        print(f"released readers of schema {SCHEMA_VERSION} checked: {', '.join(checked) or 'none released yet'}")
        return 1 if problems else 0
    if args[:1] == ['--check']:
        rest, previous, release, since = args[1:], None, False, None
        while rest:
            if rest[0] == '--release':
                release, rest = True, rest[1:]
            elif rest[0] == '--since' and len(rest) > 1:
                since, rest = rest[1], rest[2:]
                try:
                    previous = manifest_at(since)
                except ValueError as exc:
                    sys.stderr.write(f'{exc}\n')
                    return 1
            else:
                sys.stderr.write(USAGE)
                return 2
        problem = check_published(previous=previous, release=release) or check_frozen(since=since)
        if problem:
            sys.stderr.write(problem + '\n')
            return 1
        current = _read_json(published_dir() / MANIFEST)
        print(f"{published_dir()} is published ({current['version']}, published {current['published_at']}, schema "
              f"{current['schema_version']}){' and is what references/ ships' if release else ''}")
        return 0
    if args:
        sys.stderr.write(USAGE)
        return 2
    try:
        fresh = publish()
    except (OSError, ValueError) as exc:
        sys.stderr.write(f'not published: {exc}\n')
        return 1
    print(json.dumps(fresh, indent=2))
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if any(arg in ('-h', '--help') for arg in argv):
        sys.stdout.write(USAGE)
        return 0
    if argv[:1] == ['publish']:
        return _publish_main(argv[1:])
    if argv[:1] == ['accept'] and len(argv) == 2:
        try:
            accept(argv[1])
        except (OSError, ValueError) as exc:
            sys.stderr.write(f'not accepted: {exc}\n')
            return 1
        print(f'accepted: {argv[1]}')
        return 0
    if argv == ['--refresh', '--claimed']:     # the background check refresh_in_background started
        refresh(claimed=True)
        return 0
    if argv and argv[0] not in ('status', '--refresh'):
        sys.stderr.write(USAGE)
        return 2
    if argv[:1] == ['--refresh']:
        outcome = refresh(force=True, timeout=10)
        release()
        print(json.dumps({'refresh': outcome, **status()}, ensure_ascii=False, indent=2))
        return 1 if outcome.get('status') == 'state_not_saved' else 0
    print(json.dumps(status(), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    # The readers import `public_data`; run as a script this file is `__main__`, a second copy whose held and candidate
    # sets they would never see. Run the imported module instead.
    import public_data
    raise SystemExit(public_data.main())
