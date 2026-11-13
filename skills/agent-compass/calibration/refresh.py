"""Rebuild every shipped calibrated file, generated capability view and `public-data.json` in topological order,
validate the result, and report each source's age.

`agentcompass data refresh [--no-fetch] [--data DIR] [--report PATH] [--distrust SOURCE] [--model ID ...]`
`python -X utf8 refresh.py [--no-fetch] [--data DIR] [--report PATH] [--distrust SOURCE] [--model ID ...] [--references DIR]`

`--model ID` (repeatable) adds or updates only those models' measurement rows: step 2 builds the measurements from the
boards as usual, but writes to the shipped `public-measurements.json` only the named rows, each board z-score recomputed
from its raw reading on the shipped scale of its source (`sources[<id>].scale`: z = (raw - anchor_raw) / sd_panel).
Every other row, the panel, the source table and the targets stay as shipped. An entry is left out when its source is
not in the shipped table, its scale is not recorded there (the arena.ai Overall `tier` field), or the anchor's raw
reading moved between the shipped fetch and this one. A panel model or the anchor is refused: it changes the scale and
needs the full refresh. Steps 3..7 run as usual; values fitted over all measured models (tier fits, fleet effort ratios)
still move for other models, and the run prints how many did.

Topological chain:
1. Fetch (skipped with `--no-fetch`): `sources.fetch` downloads the benchmark boards and community pages into `DIR` and
   writes `fetch-manifest.json` (url, fetched_at, sha256 per file). A source that fails is recorded missing and its old
   copy deleted, so a value is never carried over silently from an earlier download.
2. `measurements.py` -> `references/public-measurements.json`.
3. `five_axis.py` -> `references/five-axis-scores.json`.
4. `capability_catalog.py` -> `references/model-capabilities.json` and `references/model-capabilities.md`.
5. `public_data.py manifest --write` -> `references/public-data.json` (unchanged content keeps the manifest byte-identical).
6. Validate: `public-data` manifest and provenance checks, `references/user-scores.json` maintainer schema, and generated
   capability views.
7. Report: community usd-per-hour check rows against the `usd_per_hour` in use, then per source its kind, data date, age in
   days and status: `ok`; `stale` (older than `five_axis.STALE_DAYS`, yellow: still used); `missing` (not fetched, or a
   community sentence no longer on the page: red, no value). Capacity windows with an empty ladder are listed as red.

Exit status: 0 all fresh, 1 something stale (yellow), 2 something missing or invalid (red), so a scheduler or CI job can
alert on it.
"""
import argparse
import copy
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

CAL_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = CAL_DIR.parent / 'scripts'
for _folder in (str(CAL_DIR), str(SCRIPTS_DIR)):
    if _folder not in sys.path:
        sys.path.insert(0, _folder)

import capability_catalog  # noqa: E402
import five_axis  # noqa: E402
import measurements  # noqa: E402
import public_data  # noqa: E402
import sources  # noqa: E402
import user_scores  # noqa: E402

LEVEL = {'ok': 0, 'unused': 0, 'stale': 1, 'missing': 2}   # unused: fetched, left out by design (METR)


def _preserve_generated_at(path, doc):
    """Keep the existing `generated_at` when every other field in `doc` is unchanged, avoiding timestamp-only churn."""
    target = Path(path)
    if not target.is_file():
        return doc
    try:
        previous = json.loads(target.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return doc
    if not isinstance(previous, dict):
        return doc
    old_stamp = previous.get('generated_at')
    if not isinstance(old_stamp, str) or not old_stamp.strip():
        return doc
    if {k: v for k, v in previous.items() if k != 'generated_at'} == {k: v for k, v in doc.items() if k != 'generated_at'}:
        return {**doc, 'generated_at': old_stamp}
    return doc


def validate_user_scores(path):
    """Validate `references/user-scores.json` strictly against the maintainer manual-score schema."""
    target = Path(path)
    if not target.is_file():
        raise ValueError(f'missing user-scores.json: {target}')
    try:
        raw = json.loads(target.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        raise ValueError(f'corrupt user-scores.json ({target}): {exc}') from exc
    if not isinstance(raw, dict):
        raise ValueError('user-scores.json must be a JSON object')
    extra_top = set(raw) - {'schema_version', 'scores'}
    if extra_top:
        raise ValueError(f'unexpected top-level keys in user-scores.json: {", ".join(sorted(extra_top))}')
    if 'schema_version' not in raw or type(raw['schema_version']) is not int or raw['schema_version'] != user_scores.SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported user-scores schema_version {raw.get('schema_version')!r}; expected {user_scores.SCHEMA_VERSION}"
        )
    scores = raw.get('scores')
    if not isinstance(scores, dict):
        raise ValueError('user-scores.json scores must be an object mapping model -> {target: entry}')
    for model, targets in scores.items():
        if not isinstance(model, str) or not model.strip() or not isinstance(targets, dict) or not targets:
            raise ValueError(f'Invalid model entry {model!r} in user-scores.json')
        for score_target, rec in targets.items():
            if not isinstance(rec, dict):
                raise ValueError(f'Invalid score record for {model} {score_target}')
            extra_keys = set(rec) - {'level', 'as_of', 'kind', 'note'}
            if extra_keys:
                raise ValueError(f'Unexpected keys in user-scores.json for {model} {score_target}: {", ".join(sorted(extra_keys))}')
            if 'level' not in rec or 'kind' not in rec:
                raise ValueError(f'Missing required level/kind in user-scores.json for {model} {score_target}')
            stamp = rec.get('as_of')
            if not isinstance(stamp, str) or not user_scores.DATE_RE.match(stamp.strip()):
                raise ValueError(f'Missing or invalid YYYY-MM-DD as_of date in user-scores.json for {model} {score_target}')
            if 'note' in rec and (not isinstance(rec['note'], str) or not rec['note'].strip()):
                raise ValueError(f'Invalid note in user-scores.json for {model} {score_target}')
    return user_scores.validate_document(raw, expected_kind='maintainer')


def validate(references=None):
    """Run public-data checks, `user-scores.json` schema validation, and generated capability-view checks."""
    ref_dir = Path(references) if references is not None else (sources.REPO / 'references')
    validate_user_scores(ref_dir / 'user-scores.json')
    manifest_path = ref_dir / public_data.MANIFEST
    manifest = public_data._read_json(manifest_path)
    state, detail = public_data.check_set(ref_dir, manifest)
    if state != 'ok':
        raise ValueError(f'public-data validation failed ({state}): {detail}')
    expected = public_data.build_manifest(ref_dir, manifest)
    if expected != manifest:
        raise ValueError(f'{manifest_path} does not match the files in {ref_dir}')
    stale_views = capability_catalog.check_files(references=ref_dir)
    if stale_views:
        raise ValueError('stale generated capability views: ' + ', '.join(stale_views))
    return manifest


# Measurement fields whose values are z-scores on a scale the source table does not record (tiers.py: arena.ai Overall
# standardized over the whole board, and the AA index aligned onto it). A row merge cannot put them on the shipped scale.
UNRECORDED_SCALE_FIELDS = ('tier',)
_SCALE_TEXT = re.compile(r'x_anchor \S+ \((?P<anchor>[^)]+)\), sd_panel \S+ over \d+ panel models')


def refuse_panel_models(names, acc, shipped):
    """Raise for a named model the row merge cannot add: not a catalog row, or one that defines the scale."""
    declared = acc.get('calibration_panel') or {}
    scale_models = {declared.get('anchor'), *(declared.get('models') or ()),
                    *((shipped.get('panel') or {}).get('models') or ())}
    for name in names:
        if name not in (acc.get('models') or {}):
            raise ValueError(f'--model {name}: accounts.json has no row for it; add the catalog row first')
        if name in scale_models:
            raise ValueError(f'--model {name}: it is in the calibration panel (or is the anchor), so it changes the scale '
                             'of every board; run the full refresh (agentcompass data refresh) instead')


def _on_shipped_scale(field, entry, shipped_sources, fresh_sources):
    """(entry on the shipped scale, None), or (None, why it is left out), for one freshly built measurement entry."""
    src = entry.get('source')
    if src not in shipped_sources:
        return None, 'source not in the shipped source table'
    if field in UNRECORDED_SCALE_FIELDS:
        return None, 'source has no shipped scale'
    fresh_scale = (fresh_sources.get(src) or {}).get('scale')
    if not fresh_scale or not any(key in entry for key in ('raw', 'raw_by_effort', 'z')):
        return entry, None          # a scale-free reading: price, time, modality, the index value itself
    shipped_scale = shipped_sources[src].get('scale')
    if not shipped_scale:
        return None, 'source has no shipped scale'
    if (shipped_scale['anchor'], shipped_scale['anchor_raw']) != (fresh_scale['anchor'], fresh_scale['anchor_raw']):
        return None, (f"anchor reading moved ({shipped_scale['anchor']}: {shipped_scale['anchor_raw']} shipped, "
                      f"{fresh_scale['anchor_raw']} now)")
    anchor_raw, sd = shipped_scale['anchor_raw'], shipped_scale['sd_panel']
    out = dict(entry)
    if 'raw' in entry:
        out['value'] = round((entry['raw'] - anchor_raw) / sd, 4)
    if 'raw_by_effort' in entry:
        out['z_by_effort'] = {e: round((r - anchor_raw) / sd, 4) for e, r in entry['raw_by_effort'].items()}
    if 'z' in entry:                # the picked effort's z (aa.index), found by which effort carried it
        picked = [e for e, z in (entry.get('z_by_effort') or {}).items() if z == entry['z']]
        if not picked or picked[0] not in out.get('z_by_effort', {}):
            return None, 'its z matches no listed effort'
        out['z'] = out['z_by_effort'][picked[0]]
    if isinstance(entry.get('method'), str):
        out['method'] = _SCALE_TEXT.sub(lambda m: (f"x_anchor {anchor_raw:.6g} ({m['anchor']}), sd_panel {sd:.6g} over "
                                                   f"{shipped_scale['n_panel']} panel models"), entry['method'])
    return out, None


def merge_model_rows(shipped, fresh, names, now=None):
    """(measurements, left_out): the shipped document with only the named models' rows taken from `fresh`, on the
    shipped scale; every other row, the panel, the sources and the checks stay as shipped. left_out lists
    (model, field, source, reason) for each entry not merged."""
    merged, left_out = copy.deepcopy(shipped), []
    for name in names:
        row = {}
        for field, entries in ((fresh.get('models') or {}).get(name) or {}).items():
            for entry in entries:
                kept, reason = _on_shipped_scale(field, entry, shipped.get('sources') or {}, fresh.get('sources') or {})
                if kept is None:
                    left_out.append((name, field, entry.get('source'), reason))
                else:
                    row.setdefault(field, []).append(kept)
        merged['models'][name] = row
    merged['conflicts'] = ([c for c in shipped.get('conflicts') or () if c.get('model') not in names]
                           + [c for c in fresh.get('conflicts') or () if c.get('model') in names])
    merged['generated_at'] = (now or datetime.now(timezone.utc)).isoformat(timespec='seconds')
    return merged, left_out


def _numbers(value, path=()):
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _numbers(item, (*path, str(key)))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _numbers(item, (*path, str(i)))


def blast_radius(before, after, names):
    """One line: how many numeric values of models other than `names` moved between two five-axis documents."""
    moved, models, largest = 0, set(), (0.0, None)
    for model, row in (after.get('models') or {}).items():
        if model in names:
            continue
        old = dict(_numbers(((before or {}).get('models') or {}).get(model)))
        for path, value in _numbers(row):
            if path in old and old[path] != value:
                moved += 1
                models.add(model)
                if abs(value - old[path]) > largest[0]:
                    largest = (abs(value - old[path]), f"{model} {'.'.join(path)}")
    if not moved:
        return 'no value of another model moved'
    return (f'{moved} values of {len(models)} other models moved (values fitted over all measured models); largest '
            f'change {largest[0]:.4g} at {largest[1]}')


def rebuild(target, now=None, references=None, distrust=(), meas_out=None, scores_out=None, models=()):
    """Steps 2..6. With `models`, step 2 merges only those rows into the shipped measurements (merge_model_rows), and
    the returned five-axis document carries `_model_merge` {left_out, blast_radius} for the caller to print."""
    target = Path(target)
    ref_dir = Path(references) if references is not None else (sources.REPO / 'references')
    meas_path = Path(meas_out) if meas_out is not None else (ref_dir / 'public-measurements.json')
    scores_path = Path(scores_out) if scores_out is not None else (ref_dir / 'five-axis-scores.json')

    acc = json.loads((ref_dir / 'accounts.json').read_text(encoding='utf-8-sig'))
    plans = json.loads((ref_dir / 'plan-templates.json').read_text(encoding='utf-8-sig'))
    public_data.validate_accounts_provenance(acc)
    validate_user_scores(ref_dir / 'user-scores.json')

    built = measurements.Builder(target, acc, now=now).build()
    merge = None
    if models:
        shipped = json.loads(meas_path.read_text(encoding='utf-8-sig'))
        refuse_panel_models(models, acc, shipped)
        before = json.loads(scores_path.read_text(encoding='utf-8-sig')) if scores_path.is_file() else None
        built, left_out = merge_model_rows(shipped, built, models, now=now)
        merge = {'left_out': left_out, 'before': before}
    meas = _preserve_generated_at(meas_path, built)
    raw = (json.dumps(meas, indent=1, ensure_ascii=False) + '\n').encode('utf-8')
    meas_path.parent.mkdir(parents=True, exist_ok=True)
    meas_path.write_bytes(raw)

    doc = _preserve_generated_at(
        scores_path,
        five_axis.build(meas, acc, plans, distrust=distrust, meas_bytes=raw, now=now),
    )
    scores_path.parent.mkdir(parents=True, exist_ok=True)
    scores_path.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')

    if scores_path.resolve() == (ref_dir / 'five-axis-scores.json').resolve():
        capability_catalog.write_default_files(accounts=acc, references=ref_dir)
        public_data.write_manifest(ref_dir, now=now)
        validate(ref_dir)
    if merge is not None:
        doc = {**doc, '_model_merge': {'left_out': merge['left_out'],
                                       'blast_radius': blast_radius(merge['before'], doc, models)}}
    return meas, doc


def staleness(doc):
    """(rows, worst status): one row per source and per capacity window left without a value."""
    rows = []
    stale_capability = {e['source']: e for e in doc.get('source_errors') or () if e.get('reason') == 'stale_source'}
    for src, row in sorted((doc.get('source_ages') or {}).items()):
        if row.get('missing'):
            status = 'unused' if str(row.get('reason') or '').startswith('not used') else 'missing'
        else:
            status = 'stale' if row.get('stale') else 'ok'
        rows.append({'what': src, 'kind': row.get('kind'), 'as_of': row.get('as_of'), 'age_days': row.get('age_days'),
                     'status': status, 'note': row.get('reason') or (
                         f"stale capability source ({stale_capability[src]['component']}): "
                         f"{stale_capability[src]['effect']}" if src in stale_capability else row.get('as_of_kind') or '')})
    for e in doc.get('capacity_errors') or ():
        if e.get('level') != 'red':
            continue
        what = e.get('source') or '/'.join(str(e.get(k)) for k in ('family', 'plan', 'window') if e.get(k))
        if e.get('source') and e.get('reason') == 'reading_not_found':
            what = f"{e['source']} -> {e.get('family')}/{e.get('plan')}/{e.get('window')}"
        rows.append({'what': what, 'kind': 'community' if e.get('source') else 'capacity', 'as_of': None, 'age_days': None,
                     'status': 'missing', 'note': e.get('reason')})
    worst = max((LEVEL[r['status']] for r in rows), default=0)
    return rows, {0: 'ok', 1: 'stale', 2: 'missing'}[worst]


def markdown(rows, generated_at, checks=()):
    out = []
    if checks:
        out += ['Community usd per worker-hour / the usd_per_hour in use (1.0 = agreement)', '',
                '| Model | Community $/h | usd_per_hour | Community / usd_per_hour | Source | Data date |',
                '| :--- | ---: | ---: | ---: | :--- | :--- |']
        out += [f"| {c['model']} | {c['value']} | {c['usd_per_hour']} | {c['community_over_usd']} | {c['source']} "
                f"| {c['as_of']} |" for c in checks]
        out.append('')
    out += [f'Source ages at {generated_at} (stale after {five_axis.STALE_DAYS} days)', '',
           '| Source / window | Kind | Data date | Age (days) | Status | Note |', '| :--- | :--- | :--- | ---: | :--- | :--- |']
    for r in rows:
        out.append(f"| {r['what']} | {r['kind'] or ''} | {r['as_of'] or ''} | {'' if r['age_days'] is None else r['age_days']} "
                   f"| {r['status']} | {r['note']} |")
    return '\n'.join(out) + '\n'


def main(argv=None, prog=None):
    ap = argparse.ArgumentParser(prog=prog, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data', help='directory holding calibration downloads (default: ~/.cache/agentcompass-calibration)')
    ap.add_argument('--no-fetch', action='store_true', help='rebuild from the cached sources already in DIR')
    ap.add_argument('--report', help='also write the source-age table (markdown) here')
    ap.add_argument('--distrust', action='append', default=[], help='a source id to leave out of every field (repeatable)')
    ap.add_argument('--references', help='references directory to rebuild and validate (default: skills/agent-compass/references)')
    ap.add_argument('--model', action='append', default=[],
                    help="add or update only this model's measurement rows, on the shipped scale (repeatable)")
    args = ap.parse_args(argv)
    target = sources.data_dir(args.data, create=not args.no_fetch)
    try:
        if args.model:          # refused before any download
            ref_dir = Path(args.references) if args.references is not None else (sources.REPO / 'references')
            refuse_panel_models(args.model, json.loads((ref_dir / 'accounts.json').read_text(encoding='utf-8-sig')),
                                json.loads((ref_dir / 'public-measurements.json').read_text(encoding='utf-8-sig')))
        if args.no_fetch:
            sources.manifest(target)
        else:
            sources.fetch(target)
        _, doc = rebuild(target, references=args.references, distrust=args.distrust, models=args.model)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f'{exc}\n')
        return 2
    if '_model_merge' in doc:
        merge = doc['_model_merge']
        print(f"Merged rows: {', '.join(args.model)} (other rows, panel, sources and targets as shipped)")
        for model, field, src, reason in merge['left_out']:
            print(f'  left out: {model} {field} {src}: {reason}')
        print(f"Blast radius: {merge['blast_radius']}")
        print()
    rows, worst = staleness(doc)
    text = markdown(rows, datetime.now(timezone.utc).isoformat(timespec='seconds'), doc.get('usd_checks') or ())
    print(text)
    if args.report:
        Path(args.report).write_text(text, encoding='utf-8', newline='\n')
    return LEVEL[worst]


if __name__ == '__main__':
    sys.exit(main())
