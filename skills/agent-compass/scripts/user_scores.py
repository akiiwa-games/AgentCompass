"""Manual capability score layer: maintainer public scores + local user overrides.

Precedence (highest to lowest):
  1. Local user override (`<ROUTER_STATE_DIR>/user-scores.json`, `kind: "user"`, local only)
  2. Shipped/fetched maintainer score (`references/user-scores.json`, `kind: "maintainer"`, public data, no source citation needed)
  3. Public board measurement (`measured` / `measured_partial`)
  4. Sibling or tier-fit estimate / series conversion (`estimated` / `converted`)

Scale (demand-level scale, mapped dynamically to panel z-scores from `anchor.targets`):
  - `1` = panel weakest (`t1`)
  - `2` = panel median (`t2`)
  - `3` = anchor Opus 5.5 (`t3`)
  - Decimals in `[1, 3]` interpolate piecewise linearly through `(1, t1), (2, t2), (3, t3)`.
  - On `tool_operation.responsiveness`, the Opus 5.5 anchor is at or below the panel median, so `t3` is raised to `t2`
    (`t2 == t3`): levels `>= 2` mean "at least the panel median" (`[2, 3]` maps to `t2`, and `> 3` extrapolates above
    `t2` by `t2 - t1`).
  - Values `> 3` extrapolate by `t3 - t2` (or `t2 - t1` when `t3 == t2`). Because `t3 - t2` can be narrow when the
    anchor sits close to the panel median (for example `coding.faithfulness`), `z_to_level` can exceed `3.0` steeply on
    measured models above the anchor; UI displays should cap the primary `1..3` scale indicator at `3+` (or show the raw
    `+Δz SD` above anchor alongside) rather than rendering unbounded extrapolated level numbers.
  - Values `< 1` extrapolate by `t2 - t1`.
  - Manual capability scores never alter `image_input` modality; modality requires a cited benchmark, vendor source, or
    dated `kind: "maintainer"` declaration in `accounts.json`.

Tray UI Python API contract:
  - `user_scores.VALID_TARGETS`: tuple of valid `<category>` and `<category>.<facet>` strings.
  - `user_scores.z_to_level(z, t1, t2, t3)` / `user_scores.level_for_target_z(target, z, targets_table)`: inverse map
    from panel z-score onto the `1..3` demand-level scale for displaying current model values.
  - `user_scores.list_scores(model=None, *, scope="all")` -> `{"local": {...}, "public": {...}, "effective": {...}}`
    with each entry annotated with its current mapped `z` against `five-axis-scores.json` `anchor.targets`.
  - `user_scores.set_score(model, target, level, *, public=False, note=None, as_of=None)` -> saved entry + path.
  - `user_scores.clear_score(model=None, target=None, *, clear_all=False, public=False)` -> `{"removed": int, "path": str}`.
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

import override_store
import public_data

SCHEMA_VERSION = 1
FILE_NAME = 'user-scores.json'
ENV_VAR = 'ROUTER_USER_SCORES'
REFERENCES = Path(__file__).resolve().parents[1] / 'references'
PUBLIC_SCORES_FILE = REFERENCES / FILE_NAME

CATEGORIES = ('divergence', 'coding', 'aesthetics', 'tool_operation')
CATEGORY_FACETS = {
    'divergence': ('creative_writing',),
    'coding': ('execution', 'faithfulness'),
    'aesthetics': ('design',),
    'tool_operation': ('responsiveness', 'image_recognition'),
}
TOOL_LEVEL_FACETS = {1: (), 2: ('responsiveness',), 3: ('responsiveness', 'image_recognition')}
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
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
    return public_data.state_dir(environ) / FILE_NAME


def public_scores_path(path=None, *, effective: bool = True, environ=None) -> Path:
    base = Path(path) if path is not None else PUBLIC_SCORES_FILE
    return public_data.effective(base, environ) if effective and path is None else base


def _public_lock_path(public_path: Path, environ=None) -> Path:
    """Return a sidecar lock path inside `<ROUTER_STATE_DIR>`, never inside `references/`."""
    digest = hashlib.sha256(str(Path(public_path).resolve()).encode('utf-8')).hexdigest()[:12]
    return public_data.state_dir(environ) / f'public-scores-{digest}.lock'


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
        raise ValueError('Score level must be a finite number on the 1..3 demand scale, not a boolean.')
    try:
        value = float(level)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'Score level {level!r} must be a finite number on the 1..3 demand scale.') from exc
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
    """Validate and return a normalized `{schema_version: 1, scores: {model: {target: entry}}}` document."""
    if not isinstance(doc, dict):
        raise ValueError('user-scores document must be a JSON object')
    ver = doc.get('schema_version', SCHEMA_VERSION)
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
            lv = normalize_level(rec.get('level'))
            as_of = normalize_date(rec.get('as_of') or rec.get('date'))
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


def load_local(path=None, environ=None) -> dict:
    """Load and validate `<ROUTER_STATE_DIR>/user-scores.json`; raises `ValueError` if an existing file is corrupt."""
    target = local_scores_path(path, environ)
    if not target.is_file():
        return empty_document()
    try:
        raw = json.loads(target.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        raise ValueError(f'Corrupt local user-scores file {target}: {exc}') from exc
    try:
        return validate_document(raw, expected_kind='user')
    except ValueError as exc:
        raise ValueError(f'Invalid local user-scores file {target}: {exc}') from exc


def load_public(path=None, *, effective: bool = True, environ=None) -> dict:
    target = public_scores_path(path, effective=effective, environ=environ)
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
    """Return `(t1, t2, t3)` on the current panel z-scale for `category` or `category.facet`."""
    cat_block = (targets_table or {}).get(category) or {}
    if facet is not None:
        f_levels = (((cat_block.get('facets') or {}).get(facet) or {}).get('levels')) or {}
        if all(isinstance((f_levels.get(k) or {}).get('target'), (int, float)) for k in ('1', '2', '3')):
            return (float(f_levels['1']['target']), float(f_levels['2']['target']), float(f_levels['3']['target']))
        if category == 'tool_operation':
            levels = cat_block.get('levels') or {}
            r3 = ((levels.get('3') or {}).get('by_facet') or {}).get(facet) or {}
            r2 = ((levels.get('2') or {}).get('by_facet') or {}).get(facet) or {}
            if isinstance(r3.get('zero'), (int, float)) and isinstance(r3.get('target'), (int, float)):
                t1 = float(r3['zero'])
                t3 = float(r3['target'])
                t2 = float(r2['target']) if isinstance(r2.get('target'), (int, float)) else 0.5 * (t1 + t3)
                return t1, t2, t3
    levels = cat_block.get('levels') or {}
    if all(isinstance((levels.get(k) or {}).get('target'), (int, float)) for k in ('1', '2', '3')):
        return (float(levels['1']['target']), float(levels['2']['target']), float(levels['3']['target']))
    raise ValueError(f'Panel targets for {category}{"." + facet if facet else ""} are missing from anchor.targets')


def level_to_z(level: float, t1: float, t2: float, t3: float) -> float:
    """Map demand-level `level` (`1 -> t1`, `2 -> t2`, `3 -> t3`) onto panel z-scores.

    Decimals in `[1, 3]` interpolate piecewise linearly; `> 3` extrapolates by `t3 - t2` (falling back to `t2 - t1`
    when `t3 == t2`, as on `tool_operation.responsiveness`, where levels `>= 2` mean "at least the panel median");
    `< 1` extrapolates by `t2 - t1`.
    """
    lv = normalize_level(level)
    t1_f, t2_f, t3_f = float(t1), float(t2), float(t3)
    step_lo = t2_f - t1_f
    step_hi = t3_f - t2_f
    if lv < 1.0:
        return t1_f + (lv - 1.0) * step_lo
    if lv <= 2.0:
        return t1_f + (lv - 1.0) * step_lo
    if lv <= 3.0:
        return t2_f + (lv - 2.0) * step_hi
    extrap = step_hi if step_hi > 0.0 else step_lo
    return t3_f + (lv - 3.0) * extrap


def z_to_level(z: float, t1: float, t2: float, t3: float) -> float:
    """Inverse of `level_to_z`: map panel z-score `z` onto the `1..3` demand-level scale (`t1 -> 1`, `t2 -> 2`, `t3 -> 3`).

    - Below `t2`, interpolates/extrapolates with slope `1 / (t2 - t1)`.
    - When `t3 > t2`, `[t2, t3]` maps linearly onto `[2, 3]` and `z > t3` extrapolates by `1 / (t3 - t2)` (which can be
      steep when `t3 - t2` is narrow; UI displays should clamp/badge above `3` as `3+`).
    - When `t3 == t2` (`tool_operation.responsiveness`, where levels `>= 2` mean "at least the panel median"), `z == t2`
      maps to `2.0` and `z > t3` extrapolates above `3.0` by `1 / (t2 - t1)`, matching `level_to_z`.
    """
    if isinstance(z, bool):
        raise ValueError('Panel z-score must be a finite float, not a boolean.')
    z_f = float(z)
    if not math.isfinite(z_f):
        raise ValueError('Panel z-score must be finite.')
    t1_f, t2_f, t3_f = float(t1), float(t2), float(t3)
    step_lo = t2_f - t1_f
    step_hi = t3_f - t2_f
    if z_f <= t2_f:
        if step_lo <= 0.0:
            return 2.0 if z_f >= t2_f else 1.0
        return round(1.0 + (z_f - t1_f) / step_lo, 4)
    if step_hi > 0.0:
        if z_f <= t3_f:
            return round(2.0 + (z_f - t2_f) / step_hi, 4)
        return round(3.0 + (z_f - t3_f) / step_hi, 4)
    if step_lo <= 0.0:
        return 3.0
    return round(3.0 + (z_f - t3_f) / step_lo, 4)


def level_for_target_z(target: str, z: float | None, targets_table: dict) -> float | None:
    """Convert a panel `z` score on `target` (`<category>` or `<category>.<facet>`) onto the `1..3` scale, or None."""
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


def _write_public_and_manifest(public_path: Path, document: dict) -> None:
    text = json.dumps(document, ensure_ascii=False, indent=2) + '\n'
    _atomic_write_text(public_path, text)
    manifest_path = public_path.parent / public_data.MANIFEST
    if manifest_path.parent == REFERENCES or manifest_path.is_file():
        previous = json.loads(manifest_path.read_text(encoding='utf-8-sig')) if manifest_path.is_file() else None
        manifest = public_data.build_manifest(public_path.parent, previous)
        _atomic_write_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
        public_data.unpin()


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
        p_path = public_scores_path(public_path, effective=False)
        if not allow_non_repo and not is_repo_checkout(p_path.parent):
            raise ValueError('--public writes references/user-scores.json and is only supported inside a git checkout.')
        with override_store.locked(_public_lock_path(p_path)):
            doc = load_public(p_path, effective=False)
            doc['scores'].setdefault(model_key, {})[target] = entry
            doc['scores'] = dict(sorted((m, dict(sorted(ts.items()))) for m, ts in doc['scores'].items() if ts))
            _write_public_and_manifest(p_path, doc)
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
        p_path = public_scores_path(public_path, effective=False)
        if not allow_non_repo and not is_repo_checkout(p_path.parent):
            raise ValueError('--public modifies references/user-scores.json and is only supported inside a git checkout.')
        with override_store.locked(_public_lock_path(p_path)):
            doc = load_public(p_path, effective=False)
            removed = _remove(doc)
            _write_public_and_manifest(p_path, doc)
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
        out['public_path'] = str(public_scores_path(public_path))
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


def _reference_effort_deltas(five_state: dict, own_cell: dict | None = None) -> dict[str, float] | None:
    """Return `{effort: delta_from_high}` from `own_cell` or another non-flat capability curve containing `high`."""
    candidates = []
    if isinstance(own_cell, dict):
        candidates.append(own_cell)
    cats = (five_state or {}).get('categories') or {}
    for cat in ('coding', 'divergence', 'aesthetics'):
        cell = cats.get(cat)
        if isinstance(cell, dict):
            candidates.append(cell)
            for f_cell in (cell.get('facets') or {}).values():
                if isinstance(f_cell, dict):
                    candidates.append(f_cell)
    for cell in candidates:
        by = cell.get('by_effort') or {}
        if len(by) >= 2 and by.get('high') is not None:
            vals = [float(v) for v in by.values() if v is not None]
            if vals and max(vals) - min(vals) > 1e-9:
                base = float(by['high'])
                return {e: float(v) - base for e, v in by.items() if v is not None and e in EFFORTS}
    return None


def _manual_by_effort(z_val: float, deltas: dict[str, float] | None) -> dict[str, float]:
    if deltas is None:
        return {e: z_val for e in EFFORTS}
    return {e: round(z_val + d, 4) for e, d in deltas.items()}


def _apply_one_layer(five_state: dict, entries: dict, targets_table: dict) -> None:
    """Mutate `five_state` (`info['five_axis']`) in place with one layer (`maintainer` or `user`) of manual scores."""
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
            for f in CATEGORY_FACETS[cat]:
                zf = round(level_to_z(rec['level'], *target_triad(targets_table, cat, f)), 4)
                z_map[f] = zf
                deltas = (
                    None if (cat == 'tool_operation' and f == 'responsiveness')
                    else _reference_effort_deltas(five_state, facets.get(f))
                )
                facets[f] = {
                    'value': zf,
                    'by_effort': _manual_by_effort(zf, deltas),
                    'status': status,
                    'manual': {**rec, 'target': target, 'z': zf},
                }
                estimates.pop(f'{cat}.{f}', None)
            z_val = round(min(z_map.values()), 4)
            cat_cell = _recompute_category_from_facets(cat, cat_cell, status)
            cat_cell['manual'] = {**rec, 'target': target, 'z': z_val}
            # Remove any lower-layer facet manual tags under this category that whole-category just superseded
            for sub in list(manual):
                if sub == cat or sub.startswith(cat + '.'):
                    manual.pop(sub, None)
            manual[target] = {**rec, 'z': z_val}
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
            deltas = (
                None if (cat == 'tool_operation' and facet == 'responsiveness')
                else _reference_effort_deltas(five_state, facets.get(facet))
            )
            facets[facet] = {
                'value': z_val,
                'by_effort': _manual_by_effort(z_val, deltas),
                'status': status,
                'manual': {**rec, 'target': target, 'z': z_val},
            }
            estimates.pop(f'{cat}.{facet}', None)
            cat_cell = _recompute_category_from_facets(cat, cat_cell, status)
            manual[target] = {**rec, 'z': z_val}
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
            _apply_one_layer(five_state, entries, targets_table)


def cli_main(argv: list[str] | None = None) -> int:
    """CLI handler for `agentcompass score set|list|clear ...`."""
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog='agentcompass score',
        description='Set, list, or clear manual capability scores on the 1..3 demand-level scale '
                    '(1 = panel weakest, 2 = panel median, 3 = anchor Opus 5.5).',
    )
    sub = parser.add_subparsers(dest='subcommand', required=True)

    p_set = sub.add_parser('set', help='Set a manual score for <model> on <category>[.<facet>] at <level>.')
    p_set.add_argument('model', help='Model id or alias (e.g. gpt-6-sol, grok-bot).')
    p_set.add_argument('target', help='Category or category.facet (e.g. coding, coding.faithfulness, tool_operation.image_recognition).')
    p_set.add_argument('level', type=float, help='Demand-level score (1 = panel weakest, 2 = panel median, 3 = anchor Opus 5.5; decimals allowed).')
    p_set.add_argument('--note', help='Optional short note recorded with the manual score.')
    p_set.add_argument('--as-of', dest='as_of', help='Optional YYYY-MM-DD date (default: today UTC).')
    p_set.add_argument('--public', action='store_true',
                       help='Write to references/user-scores.json (kind=maintainer) and refresh public-data.json (git checkout only).')

    p_list = sub.add_parser('list', help='List manual scores (both maintainer public and local user by default).')
    p_list.add_argument('model', nargs='?', help='Optional model id to filter by.')
    grp = p_list.add_mutually_exclusive_group()
    grp.add_argument('--public', action='store_true', help='List only shipped/fetched maintainer scores.')
    grp.add_argument('--local', action='store_true', help='List only local user overrides.')

    p_clear = sub.add_parser('clear', help='Clear manual score(s) for <model> [target], or --all.')
    p_clear.add_argument('model', nargs='?', help='Model id to clear.')
    p_clear.add_argument('target', nargs='?', help='Optional category or category.facet to clear.')
    p_clear.add_argument('--all', dest='clear_all', action='store_true', help='Clear all manual scores in the selected store.')
    p_clear.add_argument('--public', action='store_true',
                         help='Clear from references/user-scores.json and refresh public-data.json (git checkout only).')

    args = parser.parse_args(argv)
    import allocator
    try:
        accounts = json.loads(allocator.fleet_document_path().read_text(encoding='utf-8-sig'))
        five = allocator.load_five_axis()
        targets_table = (five.get('anchor') or {}).get('targets') or {}
    except Exception:
        accounts, targets_table = None, {}

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
    except ValueError as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(cli_main())
