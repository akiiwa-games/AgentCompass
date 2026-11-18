"""Manual capability score layer: maintainer public scores + local user overrides.

Precedence (highest to lowest):
  1. Local user override (`<ROUTER_STATE_DIR>/user-scores.json`, `kind: "user"`, local only)
  2. Maintainer score (`references/user-scores.json` shipped or published between releases, `kind: "maintainer"`, public
     data, no source citation needed)
  3. Public board measurement (`measured` / `measured_partial`)
  4. Sibling or tier-fit estimate / series conversion (`estimated` / `converted`)

Scale (demand-level scale, mapped dynamically to panel z-scores from `anchor.targets`; schema 3, owner 2026-11-18):
  - `1` = the panel's weakest (`t1`), `2` = the panel median (`t2`), `3` = the panel's best (`t3`, the maximum of the
    panel's measured or interpolated High values on the facet). The anchor `claude-opus-5-5` is the origin of `z` (`z = 0`), not a level:
    it is level 3 only on a facet where it is the panel's best.
  - Decimals in `[1, 3]` interpolate piecewise linearly through `(1, t1), (2, t2), (3, t3)`.
  - `3` is not a top: a value above `t3` is a level above 3, extrapolated with the step from level 2 to 3 (`t3 - t2`), and a
    value below `t1` a level below 1 (step `t2 - t1`). No clamp: shown as the number it is.
  - A scale needs `t1 < t2 < t3`; the builder refuses a panel that does not give it, and the maps here refuse it too.
  - Levels written before schema 3 (3 = the anchor) keep their position: at or below 2 unchanged, above 2 stretched by the
    anchor's level on the facet (`convert_level`, `LOCAL_UPGRADES`, `references/level-3-conversion.json`).
  - Manual capability scores never alter `image_input` modality; modality requires a cited benchmark, vendor source, or
    dated `kind: "maintainer"` declaration in `accounts.json`.

Tray UI Python API contract:
  - `user_scores.VALID_TARGETS`: tuple of valid `<category>` and `<category>.<facet>` strings.
  - `user_scores.z_to_level(z, t1, t2, t3)` / `user_scores.level_for_target_z(target, z, targets_table)`: inverse map
    from panel z-score onto the demand-level scale (1 = panel weakest, 3 = panel best) for displaying current model values.
  - `user_scores.list_scores(model=None, *, scope="all")` -> `{"local": {...}, "public": {...}, "effective": {...}}`
    with each entry annotated with its current mapped `z` against `five-axis-scores.json` `anchor.targets`.
  - `user_scores.set_score(model, target, level, *, public=False, note=None, as_of=None)` -> saved entry + path.
  - `user_scores.clear_score(model=None, target=None, *, clear_all=False, public=False)` -> `{"removed": int, "path": str}`.
  - `user_scores.suggest_scores(model=None, *, scope="all", threshold=0.4)` -> read-only comparison of manual scores
    against underlying public board measurements (`measured_drift`), never modifying any file.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys

# `agentcompass score` runs this file with python -I, which leaves the script directory off sys.path. Same insertion as
# route_judge.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import override_store  # noqa: E402
import public_data  # noqa: E402
import state_upgrade  # noqa: E402

SCHEMA_VERSION = 3
FILE_NAME = 'user-scores.json'
ENV_VAR = 'ROUTER_USER_SCORES'
REFERENCES = Path(__file__).resolve().parents[1] / 'references'
PUBLIC_SCORES_FILE = REFERENCES / FILE_NAME

CATEGORIES = ('creativity', 'coding', 'aesthetics', 'tool_operation')
CATEGORY_FACETS = {
    'creativity': ('creative_writing',),
    'coding': ('execution', 'faithfulness'),
    'aesthetics': ('design',),
    'tool_operation': ('responsiveness', 'image_recognition'),
}
TOOL_LEVEL_FACETS = {1: (), 2: ('responsiveness',), 3: ('responsiveness', 'image_recognition')}
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
SUGGEST_DEFAULT_THRESHOLD = 0.4
MEASURED_DATA_STATUSES = ('measured', 'measured_partial', 'interpolated')
VALID_TARGETS = tuple(
    [cat for cat in CATEGORIES]
    + [f'{cat}.{facet}' for cat in CATEGORIES for facet in CATEGORY_FACETS[cat]]
)
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def empty_document() -> dict:
    return {'schema_version': SCHEMA_VERSION, 'scores': {}}


def local_scores_path(path=None, environ=None) -> Path:
    if path is not None:
        return Path(path)
    env = os.environ if environ is None else environ
    configured = str(env.get(ENV_VAR) or '').strip()
    if configured:
        return Path(os.path.expandvars(configured)).expanduser()
    return state_upgrade.state_dir(environ=environ) / FILE_NAME


def public_scores_path(path=None) -> Path:
    """The maintainer file `--public` writes: always the shipped references/user-scores.json of this checkout."""
    return Path(path) if path is not None else PUBLIC_SCORES_FILE


def public_scores_read_path(path=None) -> Path:
    """The maintainer file routing reads: the published set in effect (public_data.effective) unless a path is given."""
    return Path(path) if path is not None else public_data.effective(PUBLIC_SCORES_FILE)


def _public_lock_path(public_path: Path) -> Path:
    """Return a sidecar lock path inside `<ROUTER_STATE_DIR>`, never inside `references/`."""
    digest = hashlib.sha256(str(Path(public_path).resolve()).encode('utf-8')).hexdigest()[:12]
    return state_upgrade.state_dir() / f'public-scores-{digest}.lock'


def _atomic_write_text(path: Path, content: str) -> None:
    """Atomically write UTF-8 LF text via a sibling `.tmp` file and `os.replace`."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    try:
        with open(tmp, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def is_repo_checkout(references_dir: Path | None = None) -> bool:
    ref = Path(references_dir) if references_dir is not None else REFERENCES
    for parent in (ref, *ref.parents):
        if (parent / '.git').exists():
            return True
    return False


def parse_target(target: str) -> tuple[str, str | None]:
    raw = str(target or '').strip()
    if '.' not in raw:
        if raw in CATEGORIES:
            return raw, None
    else:
        cat, facet = raw.split('.', 1)
        if cat in CATEGORY_FACETS and facet in CATEGORY_FACETS[cat]:
            return cat, facet
    raise ValueError(f'Unknown score target {target!r}; expected one of: ' + ', '.join(VALID_TARGETS))


def normalize_level(level) -> float:
    if isinstance(level, bool):
        raise ValueError('Score level must be a finite number on the demand-level scale, not a boolean.')
    try:
        value = float(level)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'Score level {level!r} must be a finite number on the demand-level scale.') from exc
    if not math.isfinite(value):
        raise ValueError('Score level must be finite.')
    return round(value, 6)


def normalize_date(as_of: str | None = None) -> str:
    if as_of is None or not str(as_of).strip():
        return datetime.now(timezone.utc).date().isoformat()
    text = str(as_of).strip()
    if not DATE_RE.match(text):
        raise ValueError(f'Date {as_of!r} must be YYYY-MM-DD.')
    return text


def validate_document(doc, *, expected_kind: str | None = None) -> dict:
    """Validate and return a normalized `{schema_version: 3, scores: {model: {target: entry}}}` document."""
    if not isinstance(doc, dict):
        raise ValueError('user-scores document must be a JSON object')
    ver = doc.get('schema_version')
    if ver != SCHEMA_VERSION:
        raise ValueError(f'Unsupported user-scores schema_version {ver!r}; expected {SCHEMA_VERSION}')
    raw_scores = doc.get('scores')
    if raw_scores is None:
        raw_scores = {}
    if not isinstance(raw_scores, dict):
        raise ValueError('user-scores.scores must be an object mapping model -> {target: entry}')
    out_scores = {}
    for model, targets in raw_scores.items():
        m_name = str(model or '').strip()
        if not m_name or not isinstance(targets, dict):
            raise ValueError(f'Invalid model entry {model!r} in user-scores')
        clean_targets = {}
        for target, rec in targets.items():
            parse_target(target)
            if not isinstance(rec, dict):
                raise ValueError(f'Invalid score record for {m_name} {target}')
            if 'date' in rec:
                raise ValueError(f'Legacy field "date" is no longer supported for {m_name} {target}; use "as_of"')
            lv = normalize_level(rec.get('level'))
            as_of = normalize_date(rec.get('as_of'))
            kind = str(rec.get('kind') or (expected_kind or 'user')).strip()
            if kind not in ('user', 'maintainer'):
                raise ValueError(f'Invalid manual score kind {kind!r} for {m_name} {target}')
            if expected_kind is not None and kind != expected_kind:
                raise ValueError(f'Expected manual score kind {expected_kind!r} for {m_name} {target}, got {kind!r}')
            entry = {'level': lv, 'as_of': as_of, 'kind': kind}
            if rec.get('note'):
                entry['note'] = str(rec['note']).strip()
            clean_targets[target] = entry
        if clean_targets:
            out_scores[m_name] = clean_targets
    return {'schema_version': SCHEMA_VERSION, 'scores': out_scores}


def _local_schema_1_to_2(doc: dict) -> dict:
    """Schema 1 (through v0.6.6) named the first category `divergence`; schema 2 names it `creativity`."""
    scores = doc.get('scores')
    if not isinstance(scores, dict):
        return {**doc, 'schema_version': 2}
    renamed = {}
    for model, targets in scores.items():
        if not isinstance(targets, dict):
            renamed[model] = targets
            continue
        renamed[model] = {
            ('creativity' + target[len('divergence'):] if str(target).split('.')[0] == 'divergence' else target): entry
            for target, entry in targets.items()
        }
    return {**doc, 'schema_version': 2, 'scores': renamed}


LEVEL3_CONVERSION_FILE = REFERENCES / 'level-3-conversion.json'
ROUND_LEVELS = 2        # decimals of a converted level


def anchor_levels(schema: int) -> dict:
    """{'<category>.<facet>': A} for user-scores schema `schema`: the level the old level 3 (the anchor) has on the facet
    scale of v0.7.0 (`references/level-3-conversion.json`, declared with its provenance and frozen, so the conversion gives the
    same levels on every later version)."""
    doc = json.loads(LEVEL3_CONVERSION_FILE.read_text(encoding='utf-8'))
    return {target: float(level) for target, level in doc['conversions'][str(schema)]['anchor_levels'].items()}


def above_3_slopes(schema: int) -> dict:
    """{'<category>.<facet>': s} for user-scores schema `schema`: on a facet whose old scale was a plateau (`t2 == t3`, level 3 =
    the panel median) the new levels per old level above 3 (`references/level-3-conversion.json`, declared and frozen)."""
    doc = json.loads(LEVEL3_CONVERSION_FILE.read_text(encoding='utf-8'))
    return {target: float(slope) for target, slope in doc['conversions'][str(schema)]['above_3_slopes'].items()}


def convert_level(level: float, anchor_level: float, *, above_3_slope: float | None = None, ndigits: int = ROUND_LEVELS) -> float:
    """A level written with level 3 = the anchor, carried onto the scale whose level 3 is the panel maximum. Levels 1 and 2 are
    the panel minimum and median on both scales, so a level at or below 2 stays; above 2 the position between the median and
    the old level 3 is kept: `2 + (L - 2) * (A - 2)`, A = the anchor's level on the facet (the segment above 3 continues with the
    same slope). A facet whose old scale was a plateau (A = 2: every level in (2, 3] meant the median) gives 2 up to level 3 and,
    above it, `2 + (L - 3) * s`, `s` = the declared `above_3_slope`. Against the panel the old scale was read with, the value is
    the same."""
    lv = normalize_level(level)
    if lv <= 2.0:
        return lv
    if above_3_slope is not None:
        return round(2.0 + max(lv - 3.0, 0.0) * above_3_slope, ndigits)
    return round(2.0 + (lv - 2.0) * (anchor_level - 2.0), ndigits)


def _facet_of(target: str) -> tuple[str, str | None]:
    """(category, facet) of a score target; a category of one facet stands for that facet's scale."""
    cat, facet = parse_target(target)
    if facet is None and len(CATEGORY_FACETS[cat]) == 1:
        facet = CATEGORY_FACETS[cat][0]
    return cat, facet


def convert_scores(scores: dict, anchor_level: dict, above_3_slope: dict, *, ndigits: int = ROUND_LEVELS) -> dict:
    """`scores` ({model: {target: entry}}, levels with level 3 = the anchor) converted by `convert_level` with the facet's
    `anchor_level` ({'<category>.<facet>': A}) and, on a plateau facet, its `above_3_slope`. An entry for a category of several facets (`coding`, `tool_operation`) above
    level 2 is written per facet (their anchor levels differ), except for a facet the model has an entry of its own for, which
    stands; at or below 2 it stays one entry. Everything but `level` is kept."""
    out = {}
    for model, entries in scores.items():
        converted = {}
        explicit = {t for t in entries if parse_target(t)[1] is not None}
        for target, entry in entries.items():
            cat, facet = parse_target(target)
            if facet is None and len(CATEGORY_FACETS[cat]) > 1:
                if normalize_level(entry['level']) <= 2.0:
                    converted[target] = dict(entry)
                    continue
                for f in CATEGORY_FACETS[cat]:
                    if f'{cat}.{f}' not in explicit:
                        key = f'{cat}.{f}'
                        converted[key] = {**entry, 'level': convert_level(entry['level'], anchor_level[key], above_3_slope=above_3_slope.get(key), ndigits=ndigits)}
                continue
            cat, facet = _facet_of(target)
            key = f'{cat}.{facet}'
            converted[target] = {**entry, 'level': convert_level(entry['level'], anchor_level[key], above_3_slope=above_3_slope.get(key), ndigits=ndigits)}
        out[model] = dict(sorted(converted.items()))
    return out


def _local_schema_2_to_3(doc: dict) -> dict:
    """Schema 2 (v0.6.7 and v0.6.8) levels were written with level 3 = the anchor; schema 3 levels have level 3 = the panel
    maximum. Each entry is converted by `convert_scores` with the declared anchor levels and slopes of `references/level-3-conversion.json`,
    the same data and function that converted the maintainer file."""
    scores = doc.get('scores')
    if not isinstance(scores, dict):
        return {**doc, 'schema_version': 3}
    converted = convert_scores({m: t for m, t in scores.items() if isinstance(t, dict)}, anchor_levels(2), above_3_slopes(2))
    return {**doc, 'schema_version': 3, 'scores': {**scores, **converted}}


# The user's own scores are kept across updates: {schema a released version wrote: the step to the next schema}.
LOCAL_UPGRADES = {1: _local_schema_1_to_2, 2: _local_schema_2_to_3}


def upgrade_local_document(doc):
    """`doc` carried into `SCHEMA_VERSION` through `LOCAL_UPGRADES`; a document of another shape is returned as it is
    and fails validation. Nothing is written here: the file takes the current schema the next time a score is saved."""
    while isinstance(doc, dict) and doc.get('schema_version') in LOCAL_UPGRADES:
        doc = LOCAL_UPGRADES[doc['schema_version']](doc)
    return doc


def _fix(target) -> str:
    import state_upgrade
    return ('edit the file: correct or remove the entry the message names, or '
            + state_upgrade.fix_delete(target, 'this discards every score you set yourself; set them again with '
                                              '`agentcompass score set`'))


def load_local(path=None, environ=None) -> dict:
    """Load and validate `<ROUTER_STATE_DIR>/user-scores.json`, carrying a file an earlier release wrote into the current
    schema (`upgrade_local_document`); raises `ValueError` if an existing file is corrupt or of an unknown shape."""
    target = local_scores_path(path, environ)
    if not target.is_file():
        return empty_document()
    import state_upgrade
    try:
        raw = json.loads(target.read_text(encoding='utf-8-sig'))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(state_upgrade.format_issue(
            target, f'corrupt local user-scores file ({exc})', _fix(target),
        )) from exc
    try:
        return validate_document(upgrade_local_document(raw), expected_kind='user')
    except ValueError as exc:
        raise ValueError(state_upgrade.format_issue(
            target, f'invalid local user-scores file ({exc})', _fix(target),
        )) from exc


def load_public(path=None) -> dict:
    target = public_scores_read_path(path)
    if not target.is_file():
        return empty_document()
    raw = json.loads(target.read_text(encoding='utf-8-sig'))
    return validate_document(raw, expected_kind='maintainer')


def resolve_model_key(model: str, accounts: dict | None = None) -> str:
    raw = str(model or '').strip()
    if not raw:
        raise ValueError('Model id must not be empty.')
    if isinstance(accounts, dict):
        models = accounts.get('models') or {}
        aliases = accounts.get('aliases') or {}
        if raw in models:
            return raw
        if raw in aliases and aliases[raw] in models:
            return aliases[raw]
        leaf = raw.rsplit('/', 1)[-1]
        if leaf in models:
            return leaf
        if leaf in aliases and aliases[leaf] in models:
            return aliases[leaf]
    return raw


def target_triad(targets_table: dict, category: str, facet: str | None = None) -> tuple[float, float, float]:
    """Return `(t1, t2, t3)` on the current panel z-scale for `category` or `category.facet` (a category of one facet reads
    that facet's scale)."""
    cat_block = (targets_table or {}).get(category) or {}
    if facet is None and len(CATEGORY_FACETS.get(category) or ()) == 1:
        facet = CATEGORY_FACETS[category][0]
    levels = (((cat_block.get('facets') or {}).get(facet) or {}).get('levels') if facet is not None else cat_block.get('levels')) or {}
    if all(isinstance((levels.get(k) or {}).get('target'), (int, float)) for k in ('1', '2', '3')):
        return (float(levels['1']['target']), float(levels['2']['target']), float(levels['3']['target']))
    raise ValueError(f'Panel targets for {category}{"." + facet if facet else ""} are missing from anchor.targets')


def _scale(t1, t2, t3) -> tuple[float, float, float]:
    t1_f, t2_f, t3_f = float(t1), float(t2), float(t3)
    if not t1_f < t2_f < t3_f:
        raise ValueError(f'A level scale needs t1 < t2 < t3, got ({t1_f}, {t2_f}, {t3_f}).')
    return t1_f, t2_f, t3_f


def level_to_z(level: float, t1: float, t2: float, t3: float) -> float:
    """Map demand-level `level` (`1 -> t1`, `2 -> t2`, `3 -> t3`) onto panel z-scores: linear between the points, and
    beyond them with the step of the nearest pair (`t3 - t2` above 3, `t2 - t1` below 1)."""
    lv = normalize_level(level)
    t1_f, t2_f, t3_f = _scale(t1, t2, t3)
    if lv <= 2.0:
        return t1_f + (lv - 1.0) * (t2_f - t1_f)
    return t2_f + (lv - 2.0) * (t3_f - t2_f)


def z_to_level(z: float, t1: float, t2: float, t3: float) -> float:
    """Inverse of `level_to_z`: the demand level of panel z-score `z` (`t1 -> 1`, `t2 -> 2`, `t3 -> 3`), unclamped: a value
    above the panel maximum is a level above 3."""
    if isinstance(z, bool):
        raise ValueError('Panel z-score must be a finite float, not a boolean.')
    z_f = float(z)
    if not math.isfinite(z_f):
        raise ValueError('Panel z-score must be finite.')
    t1_f, t2_f, t3_f = _scale(t1, t2, t3)
    if z_f <= t2_f:
        return round(1.0 + (z_f - t1_f) / (t2_f - t1_f), 4)
    return round(2.0 + (z_f - t2_f) / (t3_f - t2_f), 4)


def level_for_target_z(target: str, z: float | None, targets_table: dict) -> float | None:
    """Convert a panel `z` score on `target` (`<category>` or `<category>.<facet>`) onto the facet's level scale (1 = panel
    minimum, 2 = median, 3 = maximum, unclamped above and below), or None."""
    if z is None:
        return None
    cat, facet = parse_target(target)
    if cat == 'tool_operation' and facet is None:
        facet = 'image_recognition'
    return z_to_level(z, *target_triad(targets_table, cat, facet))


def mapped_z_for_target(target: str, level: float, targets_table: dict) -> float:
    cat, facet = parse_target(target)
    if facet is None:
        zs = [level_to_z(level, *target_triad(targets_table, cat, f)) for f in CATEGORY_FACETS[cat]]
        return round(min(zs), 4)
    return round(level_to_z(level, *target_triad(targets_table, cat, facet)), 4)


def _write_public(public_path: Path, document: dict) -> None:
    _atomic_write_text(public_path, json.dumps(document, ensure_ascii=False, indent=2) + '\n')


def set_score(model: str, target: str, level: float, *, public: bool = False, note: str | None = None,
              as_of: str | None = None, accounts: dict | None = None, local_path=None, public_path=None,
              allow_non_repo: bool = False) -> dict:
    """Set one manual score (`kind='maintainer'` when `public=True`, else `kind='user'` locally)."""
    parse_target(target)
    lv = normalize_level(level)
    stamp = normalize_date(as_of)
    model_key = resolve_model_key(model, accounts)
    kind = 'maintainer' if public else 'user'
    entry = {'level': lv, 'as_of': stamp, 'kind': kind}
    if note is not None and str(note).strip():
        entry['note'] = str(note).strip()
    if public:
        p_path = public_scores_path(public_path)
        if not allow_non_repo and not is_repo_checkout(p_path.parent):
            raise ValueError('--public writes references/user-scores.json and is only supported inside a git checkout.')
        with override_store.locked(_public_lock_path(p_path)):
            doc = load_public(p_path)
            doc['scores'].setdefault(model_key, {})[target] = entry
            doc['scores'] = dict(sorted((m, dict(sorted(ts.items()))) for m, ts in doc['scores'].items() if ts))
            _write_public(p_path, doc)
        return {'model': model_key, 'target': target, 'entry': entry, 'scope': 'public', 'path': str(p_path)}
    l_path = local_scores_path(local_path)
    with override_store.locked(l_path):
        doc = load_local(l_path)
        doc['scores'].setdefault(model_key, {})[target] = entry
        doc['scores'] = dict(sorted((m, dict(sorted(ts.items()))) for m, ts in doc['scores'].items() if ts))
        override_store.write(l_path, doc)
    return {'model': model_key, 'target': target, 'entry': entry, 'scope': 'local', 'path': str(l_path)}


def clear_score(model: str | None = None, target: str | None = None, *, clear_all: bool = False,
                public: bool = False, accounts: dict | None = None, local_path=None, public_path=None,
                allow_non_repo: bool = False) -> dict:
    """Clear manual scores for one `model` + optional `target`, or all scores when `clear_all=True`."""
    if not clear_all and not (model and str(model).strip()):
        raise ValueError('Specify a model to clear (optionally with target), or pass --all.')
    if target is not None:
        parse_target(target)
    model_key = resolve_model_key(model, accounts) if (model and not clear_all) else None

    def _remove(doc: dict) -> int:
        scores = doc.get('scores') or {}
        if clear_all:
            count = sum(len(ts) for ts in scores.values())
            doc['scores'] = {}
            return count
        keys = [k for k in (model_key, str(model).strip()) if k and k in scores]
        removed = 0
        for key in dict.fromkeys(keys):
            if target is None:
                removed += len(scores[key])
                scores.pop(key, None)
            elif target in scores[key]:
                scores[key].pop(target, None)
                removed += 1
                if not scores[key]:
                    scores.pop(key, None)
        return removed

    if public:
        p_path = public_scores_path(public_path)
        if not allow_non_repo and not is_repo_checkout(p_path.parent):
            raise ValueError('--public modifies references/user-scores.json and is only supported inside a git checkout.')
        with override_store.locked(_public_lock_path(p_path)):
            doc = load_public(p_path)
            removed = _remove(doc)
            _write_public(p_path, doc)
        return {'removed': removed, 'scope': 'public', 'path': str(p_path)}
    l_path = local_scores_path(local_path)
    with override_store.locked(l_path):
        doc = load_local(l_path)
        removed = _remove(doc)
        override_store.write(l_path, doc)
    return {'removed': removed, 'scope': 'local', 'path': str(l_path)}


def _annotate_scores(scores: dict, targets_table: dict | None) -> dict:
    out = {}
    for model, entries in sorted((scores or {}).items()):
        out[model] = {}
        for target, rec in sorted((entries or {}).items()):
            item = dict(rec)
            if targets_table:
                try:
                    item['z'] = mapped_z_for_target(target, rec['level'], targets_table)
                except ValueError:
                    pass
            out[model][target] = item
    return out


def list_scores(model: str | None = None, *, scope: str = 'all', accounts: dict | None = None,
                targets_table: dict | None = None, local_path=None, public_path=None) -> dict:
    """List manual scores across `public` (`maintainer`), `local` (`user`), and merged `effective`."""
    if scope not in ('all', 'local', 'public'):
        raise ValueError(f'Unknown scope {scope!r}; expected all, local, or public.')
    model_key = resolve_model_key(model, accounts) if model else None
    pub_scores = load_public(public_path)['scores'] if scope in ('all', 'public') else {}
    loc_scores = load_local(local_path)['scores'] if scope in ('all', 'local') else {}
    if model_key:
        match_names = {model_key, str(model).strip()}
        pub_scores = {m: v for m, v in pub_scores.items() if m in match_names}
        loc_scores = {m: v for m, v in loc_scores.items() if m in match_names}
    effective = copy.deepcopy(pub_scores)
    for m, entries in loc_scores.items():
        effective.setdefault(m, {}).update(copy.deepcopy(entries))
    out = {'scope': scope}
    if scope in ('all', 'public'):
        out['public'] = _annotate_scores(pub_scores, targets_table)
        out['public_path'] = str(public_scores_read_path(public_path))
    if scope in ('all', 'local'):
        out['local'] = _annotate_scores(loc_scores, targets_table)
        out['local_path'] = str(local_scores_path(local_path))
    out['effective'] = _annotate_scores(effective, targets_table)
    return out


def _facet_has_any_score(f_cell) -> bool:
    if not isinstance(f_cell, dict):
        return False
    if f_cell.get('value') is not None:
        return True
    return any(v is not None for v in (f_cell.get('by_effort') or {}).values())


def _recompute_category_from_facets(category: str, cat_cell: dict, status: str) -> dict:
    facets = cat_cell.get('facets') or {}
    req = CATEGORY_FACETS[category]
    if category != 'tool_operation':
        missing_f = [f for f in req if not _facet_has_any_score(facets.get(f))]
        if missing_f:
            return {
                'value': None, 'by_effort': {}, 'facets': facets,
                'missing_facets': missing_f, 'status': 'partial', 'flag': 'partial',
            }
        shared = [
            e for e in EFFORTS
            if all((facets[f].get('by_effort') or {}).get(e) is not None for f in req)
        ]
        by_eff = {e: round(min(float(facets[f]['by_effort'][e]) for f in req), 4) for e in shared}
        val = by_eff.get('high')
        if val is None and all(facets[f].get('value') is not None for f in req):
            val = round(min(float(facets[f]['value']) for f in req), 4)
        res = {'value': val, 'by_effort': by_eff, 'facets': facets, 'status': status if val is not None else 'partial'}
        if val is None or not by_eff:
            res['flag'] = 'partial'
        return res
    # tool_operation: recompute by_level for levels 1..3 + category level-3 summary
    by_level = {'1': {'facets': [], 'value': 0.0, 'by_effort': {e: 0.0 for e in EFFORTS}}}
    for lvl in (2, 3):
        need = TOOL_LEVEL_FACETS[lvl]
        miss = [f for f in need if not _facet_has_any_score(facets.get(f))]
        if miss:
            by_level[str(lvl)] = {'facets': list(need), 'missing_facets': miss, 'value': None, 'by_effort': {}}
        else:
            shared = [
                e for e in EFFORTS
                if all((facets[f].get('by_effort') or {}).get(e) is not None for f in need)
            ]
            by_eff = {e: round(min(float(facets[f]['by_effort'][e]) for f in need), 4) for e in shared}
            val = by_eff.get('high')
            if val is None and all(facets[f].get('value') is not None for f in need):
                val = round(min(float(facets[f]['value']) for f in need), 4)
            by_level[str(lvl)] = {'facets': list(need), 'value': val, 'by_effort': by_eff}
    l3 = by_level['3']
    res = {
        'value': l3['value'],
        'by_effort': dict(l3['by_effort']),
        'by_level': by_level,
        'facets': facets,
        'status': status if l3['value'] is not None else 'partial',
    }
    if l3.get('missing_facets'):
        res['missing_facets'] = list(l3['missing_facets'])
        res['flag'] = 'partial' if facets else 'missing'
    elif l3['value'] is None or not l3['by_effort']:
        res['flag'] = 'partial'
    return res


def _measured_efforts(cell: dict | None) -> list[str]:
    """The efforts of a facet cell that hold a board measurement: every effort of a `measured*` cell, else the cell's own
    `measured_efforts` (a High filled by five_axis.fill_high, or a lower manual layer), else none."""
    if not isinstance(cell, dict):
        return []
    by = cell.get('by_effort') or {}
    if str(cell.get('status') or '').startswith('measured'):
        return [e for e in EFFORTS if by.get(e) is not None]
    return [e for e in cell.get('measured_efforts') or () if by.get(e) is not None]


def _pav(values: list[float]) -> list[float]:
    """`values` made non-decreasing by pooling adjacent violators."""
    blocks = [[v, 1] for v in values]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][0] / blocks[i][1] > blocks[i + 1][0] / blocks[i + 1][1]:
            blocks[i] = [blocks[i][0] + blocks[i + 1][0], blocks[i][1] + blocks[i + 1][1]]
            blocks.pop(i + 1)
            i = max(i - 1, 0)
        else:
            i += 1
    return [s / n for s, n in blocks for _ in range(n)]


def rises_with_effort(cat: str, facet: str) -> bool:
    """The direction of a facet's curve in effort, as the calibration declares it (`calibration/five_axis.py`: ability facets are
    pooled to never decrease with effort; `tool_operation.responsiveness` falls as effort rises and is left unpooled)."""
    return (cat, facet) != ('tool_operation', 'responsiveness')


def _manual_by_effort(z_val: float, own_cell: dict | None, rises: bool,
                      fixed_effort: str | None = None) -> tuple[dict[str, float], list[str]]:
    """(`by_effort`, measured efforts) of a facet a manual score sets to `z_val` (user 2026-11-12). The score sets High,
    and also the model's `fixed_effort` when it declares one (its only configuration, which the score judges). Efforts
    the facet measured keep their values; its other efforts follow the facet's own curve moved to `z_val` at High, and
    stay missing when it has none. The curve is then made monotone in the facet's direction (`rises_with_effort`) with the
    set efforts held at `z_val`: a facet that rises with effort is non-decreasing (efforts below the set ones isotonic and at
    most `z_val`, above them isotonic and at least `z_val`), one that falls is non-increasing (below: at least `z_val`, above:
    at most `z_val`); efforts between two set ones are `z_val`. An effort that bound moves is no longer listed as measured."""
    pinned = [e for e in EFFORTS if e in ('high', fixed_effort)]
    by = {e: float(v) for e, v in ((own_cell or {}).get('by_effort') or {}).items() if e in EFFORTS and v is not None}
    measured = [e for e in _measured_efforts(own_cell) if e not in pinned]
    out = {e: by[e] for e in measured}
    out.update(dict.fromkeys(pinned, z_val))
    if 'high' in by:
        out.update({e: z_val + v - by['high'] for e, v in by.items() if e not in out})
    out = {e: out[e] for e in EFFORTS if e in out}
    sign = 1.0 if rises else -1.0       # a falling facet is bounded as its mirror image
    signed = {e: sign * v for e, v in out.items()}
    held = sign * z_val
    lo, hi = EFFORTS.index(pinned[0]), EFFORTS.index(pinned[-1])
    below = [e for e in signed if EFFORTS.index(e) < lo]
    above = [e for e in signed if EFFORTS.index(e) > hi]
    signed.update({e: min(v, held) for e, v in zip(below, _pav([signed[e] for e in below]))})
    signed.update({e: max(v, held) for e, v in zip(above, _pav([signed[e] for e in above]))})
    signed.update({e: held for e in signed if lo < EFFORTS.index(e) < hi})
    out = {e: round(sign * signed[e], 4) for e in signed}
    return out, [e for e in measured if out[e] == round(by[e], 4)]


def _facet_data_reference(own_cell: dict | None, cat: str, facet: str, manual_level: float,
                          targets_table: dict) -> dict:
    """Read-only snapshot of the underlying public calibration cell before manual overlay (`user > maintainer` preserved)."""
    prev_manual = (own_cell or {}).get('manual') if isinstance(own_cell, dict) else None
    if isinstance(prev_manual, dict) and isinstance(prev_manual.get('data_reference'), dict):
        base = dict(prev_manual['data_reference'])
        base_lvl = base.get('level')
        base['delta_level'] = round(float(base_lvl) - float(manual_level), 4) if isinstance(base_lvl, (int, float)) else None
        return base
    base_status = str((own_cell or {}).get('status') or 'missing') if isinstance(own_cell, dict) else 'missing'
    base_z = (own_cell or {}).get('value') if isinstance(own_cell, dict) else None
    if base_z is None and isinstance(own_cell, dict) and isinstance(own_cell.get('by_effort'), dict):
        base_z = own_cell['by_effort'].get('high')
    base_level = None
    if isinstance(base_z, (int, float)) and not isinstance(base_z, bool):
        base_z = round(float(base_z), 4)
        if targets_table:
            try:
                base_level = z_to_level(base_z, *target_triad(targets_table, cat, facet))
            except ValueError:
                base_level = None
    else:
        base_z = None
    raw_sources = (own_cell or {}).get('sources') if isinstance(own_cell, dict) else None
    sources = sorted(str(k) for k in raw_sources.keys()) if isinstance(raw_sources, dict) else []
    delta_level = round(base_level - float(manual_level), 4) if base_level is not None else None
    return {
        'z': base_z,
        'level': base_level,
        'status': base_status,
        'sources': sources,
        'delta_level': delta_level,
    }


def _category_data_reference(facet_refs: dict[str, dict], manual_level: float) -> dict:
    present_levels = [r['level'] for r in facet_refs.values() if isinstance(r.get('level'), (int, float))]
    present_zs = [r['z'] for r in facet_refs.values() if isinstance(r.get('z'), (int, float))]
    sources = sorted({s for r in facet_refs.values() for s in (r.get('sources') or ())})
    if not present_levels:
        return {'z': None, 'level': None, 'status': 'missing', 'sources': sources, 'delta_level': None}
    if len(present_levels) < len(facet_refs):
        return {'z': None, 'level': None, 'status': 'partial', 'sources': sources, 'delta_level': None}
    level = round(min(present_levels), 4)
    z = round(min(present_zs), 4) if present_zs else None
    statuses = [str(r.get('status') or 'missing') for r in facet_refs.values()]
    if all(s in MEASURED_DATA_STATUSES for s in statuses):
        status = 'measured' if all(s == 'measured' for s in statuses) else 'measured_partial'
    elif any(s == 'estimated' for s in statuses):
        status = 'estimated'
    else:
        status = statuses[0]
    return {
        'z': z,
        'level': level,
        'status': status,
        'sources': sources,
        'delta_level': round(level - float(manual_level), 4),
    }


def _apply_one_layer(five_state: dict, entries: dict, targets_table: dict, fixed_effort: str | None = None) -> None:
    """Mutate `five_state` (`info['five_axis']`) in place with one layer (`maintainer` or `user`) of manual scores;
    `fixed_effort` is the model's declared only effort, if any."""
    if not entries:
        return
    cats = five_state.setdefault('categories', {})
    manual = five_state.setdefault('manual_scores', {})
    estimates = five_state.setdefault('estimates', {})
    # Apply whole-category entries first, then specific facet entries within the same precedence layer.
    ordered = sorted(entries.items(), key=lambda kv: ('.' in kv[0], kv[0]))
    for target, rec in ordered:
        cat, facet = parse_target(target)
        status = rec['kind']     # 'maintainer' or 'user'
        cat_cell = copy.deepcopy(cats.get(cat)) if isinstance(cats.get(cat), dict) else {'facets': {}}
        facets = copy.deepcopy(cat_cell.get('facets')) if isinstance(cat_cell.get('facets'), dict) else {}
        cat_cell['facets'] = facets
        if facet is None:
            z_map = {}
            facet_refs = {}
            for f in CATEGORY_FACETS[cat]:
                zf = round(level_to_z(rec['level'], *target_triad(targets_table, cat, f)), 4)
                z_map[f] = zf
                f_ref = _facet_data_reference(facets.get(f), cat, f, rec['level'], targets_table)
                facet_refs[f] = f_ref
                by_eff, measured = _manual_by_effort(zf, facets.get(f), rises_with_effort(cat, f), fixed_effort)
                facets[f] = {
                    'value': zf,
                    'by_effort': by_eff,
                    'measured_efforts': measured,
                    'status': status,
                    'manual': {**rec, 'target': target, 'z': zf, 'data_reference': f_ref},
                }
                estimates.pop(f'{cat}.{f}', None)
            z_val = round(min(z_map.values()), 4)
            cat_ref = _category_data_reference(facet_refs, rec['level'])
            cat_cell = _recompute_category_from_facets(cat, cat_cell, status)
            cat_cell['manual'] = {**rec, 'target': target, 'z': z_val, 'data_reference': cat_ref}
            # Remove any lower-layer facet manual tags under this category that whole-category just superseded
            for sub in list(manual):
                if sub == cat or sub.startswith(cat + '.'):
                    manual.pop(sub, None)
            manual[target] = {**rec, 'z': z_val, 'data_reference': cat_ref}
        else:
            if (cat != 'tool_operation' and not facets and not cat_cell.get('missing_facets')
                    and cat_cell.get('flag') not in ('missing', 'partial')
                    and (cat_cell.get('value') is not None or cat_cell.get('by_effort'))):
                # Minimal whole-category cell (no separate facet cells stored): preserve sibling facets' category score.
                for sib in CATEGORY_FACETS[cat]:
                    if sib != facet:
                        facets[sib] = {
                            'value': cat_cell.get('value'),
                            'by_effort': copy.deepcopy(cat_cell.get('by_effort') or {}),
                            'status': cat_cell.get('status', 'measured'),
                        }
            z_val = round(level_to_z(rec['level'], *target_triad(targets_table, cat, facet)), 4)
            f_ref = _facet_data_reference(facets.get(facet), cat, facet, rec['level'], targets_table)
            by_eff, measured = _manual_by_effort(z_val, facets.get(facet), rises_with_effort(cat, facet), fixed_effort)
            facets[facet] = {
                'value': z_val,
                'by_effort': by_eff,
                'measured_efforts': measured,
                'status': status,
                'manual': {**rec, 'target': target, 'z': z_val, 'data_reference': f_ref},
            }
            estimates.pop(f'{cat}.{facet}', None)
            cat_cell = _recompute_category_from_facets(cat, cat_cell, status)
            manual[target] = {**rec, 'z': z_val, 'data_reference': f_ref}
        cats[cat] = cat_cell
        five_state.setdefault('scores', {})[cat] = cat_cell.get('value')
        five_state.setdefault('axis_status', {})[cat] = cat_cell.get('status', status)
        if (cat_cell.get('value') is not None or cat_cell.get('by_effort')) and cat in five_state.get('missing', ()):
            five_state['missing'] = [m for m in five_state['missing'] if m != cat]
    five_state['user_scores'] = {k: v for k, v in manual.items() if v.get('kind') == 'user'}


def apply_overlay(models_table: dict, targets_table: dict, *, public_scores: dict | None = None,
                  local_scores: dict | None = None, aliases: dict | None = None) -> None:
    """Apply `public_scores` (`maintainer`) then `local_scores` (`user`) onto `accounts['models'][m]['five_axis']`."""
    pub = (public_scores or {}).get('scores') if isinstance(public_scores, dict) and 'scores' in public_scores else (public_scores or {})
    loc = (local_scores or {}).get('scores') if isinstance(local_scores, dict) and 'scores' in local_scores else (local_scores or {})
    if not pub and not loc:
        return
    alias_to_canonical = {}
    for name, info in (models_table or {}).items():
        if isinstance(info, dict):
            alias_to_canonical[name] = name
            for al in info.get('aliases') or ():
                alias_to_canonical[str(al)] = name
    for al, target in (aliases or {}).items():
        if isinstance(target, str) and target in (models_table or {}):
            alias_to_canonical[str(al)] = target

    def _canonical(raw_model: str) -> str:
        raw = str(raw_model or '').strip()
        if raw in alias_to_canonical:
            return alias_to_canonical[raw]
        leaf = raw.rsplit('/', 1)[-1]
        if leaf in alias_to_canonical:
            return alias_to_canonical[leaf]
        return raw

    for layer in (pub, loc):
        grouped: dict[str, dict] = {}
        for raw_model, entries in (layer or {}).items():
            if not isinstance(entries, dict) or not entries:
                continue
            canon = _canonical(raw_model)
            grouped.setdefault(canon, {}).update(entries)
        for canon, entries in grouped.items():
            info = models_table.setdefault(canon, {})
            if not isinstance(info, dict):
                continue
            five_state = info.setdefault('five_axis', {
                'categories': {}, 'scores': {a: None for a in CATEGORIES},
                'missing': list(CATEGORIES), 'estimates': {},
                'axis_status': {a: 'missing' for a in CATEGORIES},
                'tier': {'value': None, 'method': 'none'},
                'usd_status': 'missing', 'image_input': {'status': 'missing'},
                'fast_mode': {'status': 'not_offered'}, 'duration': {},
            })
            _apply_one_layer(five_state, entries, targets_table, info.get('fixed_effort'))


def suggest_scores(model: str | None = None, *, scope: str = 'all',
                   threshold: float = SUGGEST_DEFAULT_THRESHOLD, accounts: dict | None = None,
                   five_data: dict | None = None, local_path=None, public_path=None) -> dict:
    """Read-only review suggestions comparing manual scores against underlying public board measurements.

    Manual scores (`user > maintainer > measured > estimated`) always remain authoritative in routing; this function
    never modifies `user-scores.json` and only surfaces entries where direct public board measurements (`measured`,
    `measured_partial`, `interpolated`) diverge by `>= threshold` demand levels (`measured_drift`).
    """
    if scope not in ('all', 'local', 'public'):
        raise ValueError(f'Unknown scope {scope!r}; expected all, local, or public.')
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold) or threshold < 0:
        raise ValueError('Suggestion threshold must be a non-negative finite number.')
    thresh = round(float(threshold), 6)
    import account_fleet
    import axis_scores
    acc = copy.deepcopy(accounts) if isinstance(accounts, dict) else json.loads(
        account_fleet.fleet_document_path().read_text(encoding='utf-8-sig')
    )
    five = five_data if isinstance(five_data, dict) else axis_scores.load_five_axis()
    base = axis_scores.five_axis_overlay(
        acc, data=five, read_local_state=False, public_scores={'scores': {}}, local_scores={'scores': {}},
    )
    targets_table = (five.get('anchor') or {}).get('targets') or {}
    model_key = resolve_model_key(model, base) if model else None
    pub_scores = load_public(public_path)['scores'] if scope in ('all', 'public') else {}
    loc_scores = load_local(local_path)['scores'] if scope in ('all', 'local') else {}
    merged: dict[str, dict[str, tuple[str, dict]]] = {}
    if scope in ('all', 'public'):
        for m, entries in pub_scores.items():
            canon = resolve_model_key(m, base)
            for t, rec in entries.items():
                merged.setdefault(canon, {})[t] = ('public', rec)
    if scope in ('all', 'local'):
        for m, entries in loc_scores.items():
            canon = resolve_model_key(m, base)
            if t_all := [k for k in merged.get(canon, {}) if any(t == k or k.startswith(t + '.') for t in entries if '.' not in t)]:
                for k in t_all:
                    merged[canon].pop(k, None)
            for t, rec in entries.items():
                merged.setdefault(canon, {})[t] = ('local', rec)
    if model_key:
        match_names = {model_key, str(model).strip()}
        merged = {m: v for m, v in merged.items() if m in match_names}
    suggestions = []
    models_map = base.get('models') or {}
    for canon, t_map in sorted(merged.items()):
        raw_cats = (((models_map.get(canon) or {}).get('five_axis') or {}).get('categories')) or {}
        for target, (entry_scope, rec) in sorted(t_map.items()):
            cat, facet = parse_target(target)
            raw_cat = raw_cats.get(cat) if isinstance(raw_cats.get(cat), dict) else {}
            raw_facets = raw_cat.get('facets') if isinstance(raw_cat.get('facets'), dict) else {}
            if facet is None:
                f_refs = {f: _facet_data_reference(raw_facets.get(f), cat, f, rec['level'], targets_table)
                          for f in CATEGORY_FACETS[cat]}
                ref = _category_data_reference(f_refs, rec['level'])
            else:
                ref = _facet_data_reference(raw_facets.get(facet), cat, facet, rec['level'], targets_table)
            if ref['status'] not in MEASURED_DATA_STATUSES or ref['delta_level'] is None:
                continue
            if abs(ref['delta_level']) < thresh:
                continue
            note_text = str(rec.get('note') or '')
            manual_z = mapped_z_for_target(target, rec['level'], targets_table) if targets_table else None
            delta_z = round(float(ref['z']) - float(manual_z), 4) if (ref.get('z') is not None and manual_z is not None) else None
            suggestions.append({
                'model': canon,
                'target': target,
                'scope': entry_scope,
                'kind': rec['kind'],
                'manual_level': rec['level'],
                'manual_z': manual_z,
                'as_of': rec['as_of'],
                **({'note': note_text} if note_text else {}),
                'data_level': ref['level'],
                'data_z': ref['z'],
                'delta_level': ref['delta_level'],
                'delta_z': delta_z,
                'data_status': ref['status'],
                'sources': ref['sources'],
                'reason': 'measured_drift',
            })
    suggestions.sort(key=lambda r: (-abs(float(r['delta_level'])), r['model'], r['target']))
    return {
        'scope': scope,
        'threshold': thresh,
        'count': len(suggestions),
        'suggestions': suggestions,
    }


def cli_main(argv: list[str] | None = None) -> int:
    """CLI handler for `agentcompass score set|list|clear|suggest ...`."""
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog='agentcompass score',
        description='Set, list, clear, or review manual capability scores on the demand-level scale '
                    '(1 = panel weakest, 2 = panel median, 3 = panel best; a level above 3 is better than the panel best).',
    )
    sub = parser.add_subparsers(dest='subcommand', required=True)

    p_set = sub.add_parser('set', help='Set a manual score for <model> on <category>[.<facet>] at <level>.')
    p_set.add_argument('model', help='Model id or alias (e.g. gpt-6-sol, grok-bot).')
    p_set.add_argument('target', help='Category or category.facet (e.g. creativity, coding.faithfulness, tool_operation.image_recognition).')
    p_set.add_argument('level', type=float, help='Demand-level score (1 = panel weakest, 2 = panel median, 3 = panel best, higher allowed; decimals allowed).')
    p_set.add_argument('--note', help='Optional short note recorded with the manual score.')
    p_set.add_argument('--as-of', dest='as_of', help='Optional YYYY-MM-DD date (default: today UTC).')
    p_set.add_argument('--public', action='store_true',
                       help='Write to references/user-scores.json (kind=maintainer) (git checkout only).')

    p_list = sub.add_parser('list', help='List manual scores (both maintainer public and local user by default).')
    p_list.add_argument('model', nargs='?', help='Optional model id to filter by.')
    grp = p_list.add_mutually_exclusive_group()
    grp.add_argument('--public', action='store_true', help='List only maintainer scores (shipped or published).')
    grp.add_argument('--local', action='store_true', help='List only local user overrides.')

    p_clear = sub.add_parser('clear', help='Clear manual score(s) for <model> [target], or --all.')
    p_clear.add_argument('model', nargs='?', help='Model id to clear.')
    p_clear.add_argument('target', nargs='?', help='Optional category or category.facet to clear.')
    p_clear.add_argument('--all', dest='clear_all', action='store_true', help='Clear all manual scores in the selected store.')
    p_clear.add_argument('--public', action='store_true',
                         help='Clear from references/user-scores.json (git checkout only).')

    p_sug = sub.add_parser('suggest', help='Read-only review suggestions where public board data diverges from manual scores.')
    p_sug.add_argument('model', nargs='?', help='Optional model id to filter by.')
    p_sug.add_argument('--threshold', type=float, default=SUGGEST_DEFAULT_THRESHOLD,
                       help=f'Minimum |data_level - manual_level| to flag on measured boards (default: {SUGGEST_DEFAULT_THRESHOLD:g}).')
    sgrp = p_sug.add_mutually_exclusive_group()
    sgrp.add_argument('--public', action='store_true', help='Inspect only maintainer scores (shipped or published).')
    sgrp.add_argument('--local', action='store_true', help='Inspect only local user overrides.')

    args = parser.parse_args(argv)
    import account_fleet
    import axis_scores
    try:
        accounts = json.loads(account_fleet.fleet_document_path().read_text(encoding='utf-8-sig'))
        five = axis_scores.load_five_axis()
        targets_table = (five.get('anchor') or {}).get('targets') or {}
    except Exception:
        accounts, five, targets_table = None, None, {}

    try:
        if args.subcommand == 'set':
            res = set_score(args.model, args.target, args.level, public=args.public, note=args.note,
                            as_of=args.as_of, accounts=accounts)
            if targets_table:
                res['z'] = mapped_z_for_target(args.target, res['entry']['level'], targets_table)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        if args.subcommand == 'list':
            scope = 'public' if args.public else ('local' if args.local else 'all')
            res = list_scores(args.model, scope=scope, accounts=accounts, targets_table=targets_table)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        if args.subcommand == 'clear':
            res = clear_score(args.model, args.target, clear_all=args.clear_all, public=args.public, accounts=accounts)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        if args.subcommand == 'suggest':
            scope = 'public' if args.public else ('local' if args.local else 'all')
            res = suggest_scores(args.model, scope=scope, threshold=args.threshold, accounts=accounts, five_data=five)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
    except ValueError as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(cli_main())
