"""Rebuild every shipped calibrated file and generated capability view in topological order, validate the result, and
report each source's age. Run once per release, as a step of preparing it; between releases only what the maintainer
publishes on purpose (`agentcompass data publish`, published/<schema>/) reaches installs.

`agentcompass data refresh [--no-fetch] [--data DIR] [--report PATH] [--distrust SOURCE] [--accept-panel-change] [--model ID ...]`
`python -X utf8 refresh.py [--no-fetch] [--data DIR] [--report PATH] [--distrust SOURCE] [--accept-panel-change] [--model ID ...] [--references DIR]`

`--model ID` (repeatable) adds or updates only those models' measurement rows, conflicts and checks. It fetches the
boards into a model entry of the cache (`DIR/models/<id>/`: the pages with their own `fetch-manifest.json`, url,
fetched_at and sha256 per file, and `entry.json` naming the model and the fetch time), never into the release snapshot
(the pages at the top of `DIR`), which it does not touch. Step 2 then builds the measurements from the release snapshot
and takes each model that has an entry, in catalog order, from its own entry's pages (merge_model_rows), so a later
`--no-fetch` rebuild, which does the same, writes byte for byte the same files (on the same day: the source ages count
days). Rule: with the panel scale unchanged, the model's measurement row equals that of a full refresh over the same
boards. A board z-score is put on the snapshot's scale by the board's own drift (board_drift: the median,
over the panel members the board lists in both fetches, of the change of their raw readings; at least two members, else
the entry is left out on a board that moved), z = (raw - drift - anchor_raw) / sd_panel, applied only where the members
show a common shift (|drift| larger than their median absolute deviation around it; otherwise shift 0), and states it in
`drift_correction` (calibration/README.md, To add a model); on a board that did not move this is the plain merge. The
`tier` is the entry's board reading put on the release snapshot's tier scale or through its index map
(_tier_on_release_scale), after the same drift. Otherwise an entry is left out
only when its source has no scale in the snapshot, or, on a board that did not move, its anchor reading moved; anything
else that cannot be merged, or a model no board lists, stops the run before anything is written. `--model` never changes another row's measured value: when the release
snapshot and the other entries do not give every other row and the panel exactly as shipped (a cache that is not this
release's, or a catalog edit that moved another row), the run stops with nothing written and no entry stored. A panel
model or the anchor is refused: it changes the scale and needs the full refresh. Fleet fits are frozen per release
(owner, 2026-11-19): the merged document lists the entries' models in `model_entries`, and steps 3..6 fit, step, link,
target, cap and anchor on the release snapshot's rows only, scoring an entry's rows by those fits, so every other row and
every fitted quantity stays as shipped; the run prints how many values of other models moved (none). The full refresh (a new release snapshot) reads every row from the boards and retires the model
entries; without a release snapshot in `DIR` (no `fetch-manifest.json`), `--model` merges into the shipped measurements.

Topological chain:
1. Fetch (skipped with `--no-fetch`; `--model` fetches into the model's entry instead): `sources.fetch` downloads the benchmark boards and community pages into `DIR` and
   writes `fetch-manifest.json` (url, fetched_at, sha256 per file); `sources.fetch_releases` then downloads, one page
   each, the Artificial Analysis release pages of the catalogue models whose release the leaderboard page names as
   deprecated without records (`measurements.aa_release_plan`). A source that fails is recorded missing and its old
   copy deleted, so a value is never carried over silently from an earlier download.
2. `measurements.py` -> `references/public-measurements.json`.
3. `five_axis.py` -> `references/five-axis-scores.json`. Both documents are built before either is written: when the
   panel's members differ from the shipped `five-axis-scores.json`, the run prints the change (`five_axis.guard_panel`),
   writes no reference file and exits 2, unless `--accept-panel-change` records it in `anchor.panel_change`.
4. `capability_catalog.py` -> `references/model-capabilities.json` and `references/model-capabilities.md`.
5. Validate: provenance checks (`reference_provenance.validate_references`), `references/user-scores.json` maintainer
   schema, and generated capability views.
6. Report: community usd-per-hour check rows against the `usd_per_hour` in use, then per source its kind, data date, age in
   days on the day of the run and status: `ok`; `stale` (older than `data_age.STALE_DAYS`, yellow: still used); `missing`
   (not fetched, or a community sentence no longer on the page: red, no value). Capacity windows with an empty ladder are
   listed as red. The ages are computed here, from the dates the files hold: the files themselves hold no age.

Exit status: 0 all fresh, 1 something stale (yellow), 2 something missing or invalid (red), so a scheduler or CI job can
alert on it; 3 (`REFUSED`, never a staleness status) when the run refused and wrote nothing: a `--model` that is not a
catalog row, is in the panel or is the anchor, that no board lists or whose entry cannot be merged, or a panel change
without `--accept-panel-change`.
"""
import argparse
import copy
import json
import re
import shutil
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

CAL_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = CAL_DIR.parent / 'scripts'
for _folder in (str(CAL_DIR), str(SCRIPTS_DIR)):
    if _folder not in sys.path:
        sys.path.insert(0, _folder)

import capability_catalog  # noqa: E402
import data_age  # noqa: E402
import five_axis  # noqa: E402
import measurements  # noqa: E402
import reference_provenance  # noqa: E402
import sources  # noqa: E402
import tiers  # noqa: E402
import user_scores  # noqa: E402

LEVEL = {'ok': 0, 'unused': 0, 'stale': 1, 'missing': 2}   # unused: fetched, left out by design (METR)
REFUSED = 3                 # the run refused and wrote nothing; LEVEL never uses it


MODEL_ENTRIES = 'models'    # the cache's subdirectory of model entries, one per model added with --model
ENTRY_FILE = 'entry.json'
ENTRY_SCHEMA = 'agent-compass.calibration-model-entry/1'
MODEL_ID = re.compile(r'[a-z0-9][a-z0-9._-]*')     # a catalog id that is also a directory name
MIN_DRIFT_MEMBERS = 2      # panel members a board's drift is measured on, at least
DRIFT_FIELDS = ('drift', 'n', 'members', 'spread', 'common_shift', 'snapshot_as_of', 'fetched_as_of')
STAGING = '.fetching'       # the entry pages being fetched, under MODEL_ENTRIES; never read as an entry


class Refused(ValueError):
    """A `--model` run that cannot go ahead; raised before anything is written."""


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
    """Run the provenance checks, `user-scores.json` schema validation, and generated capability-view checks."""
    ref_dir = Path(references) if references is not None else (sources.REPO / 'references')
    validate_user_scores(ref_dir / 'user-scores.json')
    reference_provenance.validate_references(ref_dir)
    stale_views = capability_catalog.check_files(references=ref_dir)
    if stale_views:
        raise ValueError('stale generated capability views: ' + ', '.join(stale_views))


# Measurement fields standardized over the downloaded board itself (tiers.py: arena.ai Overall over its own High rows,
# the AA index aligned onto it through the models on both boards). No shipped constant enters them, so the full refresh
# computes them from the download alone and the row merge keeps them exactly as built.
DOWNLOAD_SCALE_FIELDS = ('tier',)
_SCALE_TEXT = re.compile(r'x_anchor \S+ \((?P<anchor>[^)]+)\), sd_panel \S+ over \d+ panel models')
_SCALE_KEYS = ('anchor', 'anchor_raw', 'sd_panel')
ROUNDED_SCALE_NOTE = ('the recorded scale has 6 significant digits, so a recomputed z can differ in the fourth decimal '
                      'from one on the unrounded scale')


def refuse_panel_models(names, acc, shipped):
    """Raise for a named model the row merge cannot add: not a catalog row, or one that defines the scale."""
    declared = acc.get('calibration_panel') or {}
    scale_models = {declared.get('anchor'), *(declared.get('models') or ()), *(declared.get('members') or ()),
                    *((shipped.get('panel') or {}).get('models') or ())}
    for name in names:
        if name not in (acc.get('models') or {}):
            raise Refused(f'--model {name}: accounts.json has no row for it; add the catalog row first')
        if name in scale_models:
            raise Refused(f'--model {name}: it is in the calibration panel (or is the anchor), so it changes the scale '
                             'of every board; run the full refresh (agentcompass data refresh) instead')


def _member_raw(entry):
    """The raw reading an entry scores: `raw`, else the raw of the effort whose z the entry picked (aa.index)."""
    if 'raw' in entry:
        return entry['raw']
    picked = [e for e, z in (entry.get('z_by_effort') or {}).items() if z == entry.get('z')]
    return (entry.get('raw_by_effort') or {}).get(picked[0]) if picked else None


def board_drift(src, shipped, fresh):
    """How the board `src` moved between the release snapshot (`shipped`) and this fetch (`fresh`), measured on the
    declared panel (the shipped document's panel members): {drift, n, members, spread {min, max, mad}, common_shift,
    snapshot_as_of, fetched_as_of, moved}. drift is the median over the members the board lists in both of (raw now - raw
    then); moved is whether any member's raw differs; n is how many members it used; common_shift is whether the members
    show one shift, |drift| > mad (the correction assumes one, so it is applied only then)."""
    diffs = {}
    for m in (shipped.get('panel') or {}).get('models') or ():
        then = next((e for items in ((shipped.get('models') or {}).get(m) or {}).values() for e in items
                     if e.get('source') == src), None)
        now = next((e for items in ((fresh.get('models') or {}).get(m) or {}).values() for e in items
                    if e.get('source') == src), None)
        a, b = (_member_raw(then) if then else None), (_member_raw(now) if now else None)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            diffs[m] = b - a
    values = sorted(diffs.values())
    drift = statistics.median(values) if values else 0.0
    mad = statistics.median(abs(v - drift) for v in values) if values else 0.0
    return {'drift': round(drift, 6), 'n': len(values), 'members': sorted(diffs),
            'spread': {'min': round(values[0], 6) if values else 0.0, 'max': round(values[-1], 6) if values else 0.0,
                       'mad': round(mad, 6)},
            'common_shift': round(abs(drift), 6) > round(mad, 6),
            'snapshot_as_of': ((shipped.get('sources') or {}).get(src) or {}).get('as_of'),
            'fetched_as_of': ((fresh.get('sources') or {}).get(src) or {}).get('as_of'),
            'moved': any(v != 0 for v in values)}


def _on_shipped_scale(field, entry, shipped_sources, fresh_sources, drift=None):
    """(entry as merged, how, None) or (None, None, why it is left out) for one freshly built measurement entry; `how`
    is 'as built', 'rescaled', 'drift corrected' or 'no common shift'. On a board that moved since the release snapshot
    (`drift`, from board_drift: some panel member's reading differs) the raw readings are corrected by the board's drift
    where its members show a common shift (`common_shift`), else taken as they are (shift 0, the spread recorded), and
    converted with the shipped anchor and sd_panel; with fewer than two panel members to measure the drift the entry is
    left out.
    On a board that did not move: as built on the same scale, else rescaled, and left out when the anchor reading moved.
    A z-score whose source has no shipped scale is left out; anything else that cannot be merged raises, so nothing is
    written."""
    src = entry.get('source')
    if field in DOWNLOAD_SCALE_FIELDS:
        return _tier_on_release_scale(entry, shipped_sources, drift)
    fresh_scale = (fresh_sources.get(src) or {}).get('scale')
    if not fresh_scale or not any(key in entry for key in ('raw', 'raw_by_effort', 'z')):
        return entry, 'as built', None      # a scale-free reading: price, time, modality, the index value itself
    shipped_scale = (shipped_sources.get(src) or {}).get('scale')
    if not shipped_scale:
        return None, None, 'its source has no shipped scale and the entry is a z-score'
    moved = bool(drift and drift['moved'])
    if moved and drift['n'] < MIN_DRIFT_MEMBERS:
        return None, None, (f"the board moved since the release snapshot and {drift['n']} panel member(s) are listed in "
                            f'both, fewer than {MIN_DRIFT_MEMBERS}, so its drift is not measured')
    if not moved:
        if (shipped_scale['anchor'], shipped_scale['anchor_raw']) != (fresh_scale['anchor'], fresh_scale['anchor_raw']):
            return None, None, (f"anchor reading moved ({shipped_scale['anchor']}: {shipped_scale['anchor_raw']} shipped, "
                                f"{fresh_scale['anchor_raw']} now)")
        if all(shipped_scale[k] == fresh_scale[k] for k in _SCALE_KEYS):
            return entry, 'as built', None      # the same scale: the full refresh's own value
    shift = drift['drift'] if moved and drift['common_shift'] else 0.0
    anchor_raw, sd = shipped_scale['anchor_raw'], shipped_scale['sd_panel']
    out = dict(entry)
    if 'raw' in entry:
        out['value'] = round((entry['raw'] - shift - anchor_raw) / sd, 4)
    if 'raw_by_effort' in entry:
        out['z_by_effort'] = {e: round((r - shift - anchor_raw) / sd, 4) for e, r in entry['raw_by_effort'].items()}
    if 'z' in entry:                # the picked effort's z (aa.index), found by which effort carried it
        picked = [e for e, z in (entry.get('z_by_effort') or {}).items() if z == entry['z']]
        if not picked or picked[0] not in out.get('z_by_effort', {}):
            raise Refused(f"{src}: the entry z {entry['z']} matches none of its listed efforts; nothing written")
        out['z'] = out['z_by_effort'][picked[0]]
    if isinstance(entry.get('method'), str) and 'sd_panel' in entry['method']:
        if not _SCALE_TEXT.search(entry['method']):
            raise Refused(f'{src}: the method text names sd_panel in a wording this merge cannot restate on the '
                             'shipped scale; nothing written')
        out['method'] = _SCALE_TEXT.sub(lambda m: (f"x_anchor {anchor_raw:.6g} ({m['anchor']}), sd_panel {sd:.6g} over "
                                                   f"{shipped_scale['n_panel']} panel models"), entry['method'])
    if moved:
        out['drift_correction'] = {k: drift[k] for k in DRIFT_FIELDS}
        if not drift['common_shift']:
            out['method'] += (f"; reading taken after the release snapshot on a board whose {drift['n']} panel members show "
                              f"no common shift ({drift['snapshot_as_of']} -> {drift['fetched_as_of']}: median change "
                              f"{drift['drift']:.6g}, not larger than their median absolute deviation "
                              f"{drift['spread']['mad']:.6g}), so not corrected")
            return out, 'no common shift', None
        out['method'] += (f"; reading taken after the release snapshot, corrected by the board's drift {shift:.6g} (median "
                          f"over {drift['n']} panel members of the change {drift['snapshot_as_of']} -> "
                          f"{drift['fetched_as_of']}, larger than their median absolute deviation "
                          f"{drift['spread']['mad']:.6g}) before the conversion")
        return out, 'drift corrected', None
    return out, 'rescaled', None


_TIER_TEXT = re.compile(r'standardized against (?P<anchor>.+?) \(\S+, sd \S+\): tier \S+$')


def _tier_on_release_scale(entry, shipped_sources, drift):
    """(entry as merged, how, None) or (None, None, why) for a model entry's tier (owner 2026-11-19, round 6 item 8: a
    model entry is scored by the release's fits): an arena.ai Overall reading standardized with the release snapshot's
    anchor rating and sd (`tier_scale`), an Artificial Analysis Intelligence Index reading put through the release
    snapshot's map onto that scale (`tier_map`), each after the board's drift where its panel members show a common shift
    (`drift`: board_drift of arena Overall, or of `aa.index` for the index), as for every board."""
    src = entry.get('source')
    row = shipped_sources.get(src) or {}
    moved = bool(drift and drift['moved'])
    if moved and drift['n'] < MIN_DRIFT_MEMBERS:
        return None, None, (f"the board moved since the release snapshot and {drift['n']} panel member(s) are listed in "
                            f'both, fewer than {MIN_DRIFT_MEMBERS}, so its drift is not measured')
    shift = drift['drift'] if moved and drift['common_shift'] else 0.0
    out = dict(entry)
    if src == 'tier.arena_overall':
        scale = row.get('tier_scale')
        if not scale or 'raw' not in entry:
            raise Refused(f'{src}: the release snapshot records no tier scale (rebuild it with this code); nothing written')
        out['value'] = round((entry['raw'] - shift - scale['anchor_raw']) / scale['sd'], 4)
        out['method'] = _TIER_TEXT.sub(lambda m: (f"standardized against {m['anchor']} ({scale['anchor_raw']:.1f}, sd "
                                                  f"{scale['sd']:.2f}): tier {out['value']:.4f}"), entry['method'])
    elif src == 'tier.aa_index':
        pts = (row.get('tier_map') or {}).get('points')
        if not pts:
            raise Refused(f'{src}: the release snapshot records no tier map (rebuild it with this code); nothing written')
        fit = tiers.Interpolation([p[0] for p in pts], [p[1] for p in pts], [p[2] for p in pts])
        x = entry['board_value'] - shift
        low, high = fit.neighbours(x)
        out.update({'value': round(float(fit(x)), 4), 'board_value': x, 'neighbours': {'below': low, 'above': high},
                    'n': fit.n, **fit.spread(),
                    'method': (f"{tiers.OVERALL_BOARDS['aa.index'][0]} {x:g} at High "
                               f"interpolated onto the arena.ai Overall High scale between {low['models']} and "
                               f"{high['models']} of the {fit.n} models listed at High on both boards")})
    else:
        raise Refused(f'{src}: a tier source this merge cannot put on the release scale; nothing written')
    if moved:
        out['drift_correction'] = {k: drift[k] for k in DRIFT_FIELDS}
        return out, ('drift corrected' if drift['common_shift'] else 'no common shift'), None
    return out, 'release scale', None


def merge_model_rows(shipped, fresh, names):
    """(measurements, left_out, notes): the shipped document with only the named models' rows, conflicts and checks
    taken from `fresh`, board z-scores on the shipped scale; every other row, the panel and the scales stay as shipped.
    left_out lists (model, field, source, reason) for each entry not merged; notes state what else the merge did.
    A named model with no measurement entry at all raises (no board lists it)."""
    merged, left_out, notes = copy.deepcopy(shipped), [], []
    shipped_sources, fresh_sources = shipped.get('sources') or {}, fresh.get('sources') or {}
    rescaled, corrected, tier_boards, drifts = 0, {}, set(), {}
    for name in names:
        entries = (fresh.get('models') or {}).get(name) or {}
        if not any(entries.values()):
            raise Refused(f'--model {name}: no board lists it (the fetched boards give it no measurement); check its '
                             'listed_as names; nothing written')
        row = {}
        for field, items in entries.items():
            for entry in items:
                src = entry.get('source')
                board = 'aa.index' if src == 'tier.aa_index' else src     # the index's tier moves with the index board
                if board not in drifts:
                    drifts[board] = board_drift(board, shipped, fresh)
                kept, how, reason = _on_shipped_scale(field, entry, shipped_sources, fresh_sources, drifts[board])
                if kept is None:
                    left_out.append((name, field, entry.get('source'), reason))
                    continue
                row.setdefault(field, []).append(kept)
                rescaled += how == 'rescaled'
                if how in ('drift corrected', 'no common shift'):
                    corrected[src] = kept['drift_correction']
                src = entry.get('source')
                if src not in merged['sources'] and src in fresh_sources:
                    merged['sources'][src] = fresh_sources[src]
                _cite_release_page(merged['sources'].get(src), entry, fresh_sources, src)
                shipped_sha = (shipped_sources.get(src) or {}).get('sha256')
                if field in DOWNLOAD_SCALE_FIELDS and shipped_sha != (fresh_sources.get(src) or {}).get('sha256'):
                    tier_boards.add(src)
        merged['models'][name] = row
    order = list(fresh.get('models') or ())
    merged['models'] = dict(sorted(merged['models'].items(),
                                   key=lambda kv: (kv[0] not in order, order.index(kv[0]) if kv[0] in order else 0)))
    for key in ('conflicts', 'checks'):
        merged[key] = _in_build_order([c for c in shipped.get(key) or () if c.get('model') not in names],
                                      [c for c in fresh.get(key) or () if c.get('model') in names], fresh.get(key) or [])
    if rescaled:
        notes.append(f'{rescaled} board z-scores recomputed on the shipped scale (the panel scale moved since the '
                     f'shipped fetch); {ROUNDED_SCALE_NOTE}')
    for src, d in sorted(corrected.items()):
        notes.append(f"{src} moved since the release snapshot: drift {d['drift']:.6g} over {d['n']} panel members "
                     f"(spread {d['spread']['min']:.6g} .. {d['spread']['max']:.6g}, median absolute deviation "
                     f"{d['spread']['mad']:.6g}; {d['snapshot_as_of']} -> {d['fetched_as_of']}), "
                     + ('corrected before the conversion to the shipped scale' if d['common_shift'] else
                        'not corrected: the members show no common shift (|drift| not above the median absolute deviation)'))
    for src in sorted(tier_boards):
        notes.append(f"the tier from {src} is read on the fetched board, a different file from the shipped fetch, and put "
                     "on the release snapshot's tier scale; the other models' tiers stay as shipped")
    # the rows a model entry gave: five_axis scores them by the release's fits and never fits over them (item 8)
    merged['model_entries'] = sorted({*(shipped.get('model_entries') or ()), *names})
    merged['generated_at'] = measurements.inputs_date(merged)
    return merged, left_out, notes


def _cite_release_page(row, entry, fresh_sources, src):
    """Give the shipped source row `row` of `src` the Artificial Analysis release page `entry` cites (the entry's url is
    one of the fetched rows' `release_pages`), so every url an entry cites has its source row: the page's row under
    `release_pages` and, a source being as old as the oldest of its files, its date in `as_of`; a derived source row
    takes the fresh citation of that url under `citations`."""
    page = next(((n, p) for fresh in fresh_sources.values() for n, p in (fresh.get('release_pages') or {}).items()
                 if p['url'] == entry.get('url')), None)
    if page is None or row is None:
        return
    if 'citations' in row:
        row['citations'] += [c for c in fresh_sources[src].get('citations') or () if c.get('url') == page[1]['url']
                             and c not in row['citations']]
    elif page[0] not in row.get('release_pages', {}):
        row.setdefault('release_pages', {})[page[0]] = page[1]
        row['as_of'] = min(row['as_of'], page[1]['fetched_at'][:10])


def _in_build_order(kept, added, built):
    """`kept` with each row of `added` placed where the full build (`built`, the fresh list) has it: right after the
    nearest row before it in `built` that is in the result, else first."""
    out = list(kept)
    for row in added:
        before = built[:built.index(row)]
        anchor = next((r for r in reversed(before) if r in out), None)
        out.insert(0 if anchor is None else len(out) - out[::-1].index(anchor), row)
    return out


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
    moved, models, fields, largest = 0, set(), {}, (0.0, None)
    for model, row in (after.get('models') or {}).items():
        if model in names:
            continue
        old = dict(_numbers(((before or {}).get('models') or {}).get(model)))
        for path, value in _numbers(row):
            if path in old and old[path] != value:
                moved += 1
                models.add(model)
                fields[path[0]] = fields.get(path[0], 0) + 1
                if abs(value - old[path]) > largest[0]:
                    largest = (abs(value - old[path]), f"{model} {'.'.join(path)}")
    if not moved:
        return 'no value of another model moved'
    by_field = ', '.join(f'{field} {count}' for field, count in sorted(fields.items(), key=lambda kv: (-kv[1], kv[0])))
    return (f'{moved} values of {len(models)} other models moved, by field: {by_field} (a model entry reached a fleet '
            f'fit, which the release freezes); largest change {largest[0]:.4g} at {largest[1]}')


def fetch_sources(target, references=None):
    """Step 1: `sources.fetch`, then the Artificial Analysis release pages the fetched leaderboard page and the
    catalogue in `references` ask for (`measurements.aa_release_plan`)."""
    ref_dir = Path(references) if references is not None else (sources.REPO / 'references')
    acc = json.loads((ref_dir / 'accounts.json').read_text(encoding='utf-8-sig'))
    sources.fetch(target)
    sources.fetch_releases(target, measurements.aa_release_files(measurements.aa_release_plan(target, acc['models'], sources.effort_readings(acc))[0]))


def model_entries(target):
    """{model: entry.json document} of the model entries in the cache `target` (`target/models/<id>/`)."""
    root = Path(target) / MODEL_ENTRIES
    out = {}
    for folder in sorted(root.iterdir()) if root.is_dir() else ():
        if folder.name.startswith('.'):
            continue            # pages being fetched or stored, not an entry yet
        path = folder / ENTRY_FILE
        doc = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
        if doc.get('schema') != ENTRY_SCHEMA or doc.get('model') != folder.name:
            raise ValueError(f'{folder}: not a model entry ({ENTRY_FILE} of schema {ENTRY_SCHEMA} naming {folder.name}); '
                             'remove the directory or run `agentcompass data refresh --model <id>` again')
        out[folder.name] = doc
    return out


def store_model_entry(target, name, pages, fetched_at):
    """Copy the fetched pages `pages` (a directory with its fetch-manifest.json) to `name`'s entry in the cache `target`,
    replacing an older entry of `name`; no other file of the cache is written."""
    dest = Path(target) / MODEL_ENTRIES / name
    tmp = dest.with_name(f'.{name}.new')
    shutil.rmtree(tmp, ignore_errors=True)
    pages = Path(pages)
    shutil.copytree(pages, tmp, ignore=lambda folder, names: [MODEL_ENTRIES] if Path(folder) == pages else [])
    (tmp / ENTRY_FILE).write_text(json.dumps({
        'schema': ENTRY_SCHEMA, 'model': name, 'fetched_at': fetched_at,
        'statement': (f'the pages `agentcompass data refresh --model {name}` fetched, each with its url, fetched_at and '
                      f"sha256 in {sources.MANIFEST}; every rebuild takes {name}'s rows from them and every other row "
                      'from the release snapshot, until the full refresh retires the entry')},
        indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    if dest.exists():
        shutil.rmtree(dest)
    tmp.rename(dest)


def fetch_model_pages(target, references=None):
    """Download into the staging directory of the cache's model entries what the full refresh downloads (fetch_sources:
    the boards and the release pages the plan chooses, the panel's among them, so the entry's boards have their own
    scales) and return it; boards transcribed from the browser are copied from the release snapshot (a fetch keeps them,
    as `sources.fetch` does). Nothing at the top of `target` (the release snapshot) is written."""
    target = Path(target)
    staging = target / MODEL_ENTRIES / STAGING
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    known = sources.manifest(target) if (target / sources.MANIFEST).is_file() else {}
    transcribed = {n: r for n, r in known.items() if r.get('kind') == 'transcribed'}
    for name in transcribed:
        shutil.copyfile(target / name, staging / name)
    (staging / sources.MANIFEST).write_text(json.dumps({'files': transcribed}, indent=1), encoding='utf-8')
    fetch_sources(staging, references)
    return staging


def retire_model_entries(target):
    """The full refresh reads every row from its new release snapshot: remove the model entries; return their ids."""
    names = sorted(model_entries(target))
    shutil.rmtree(Path(target) / MODEL_ENTRIES, ignore_errors=True)
    return names


def rebuild(target, now=None, references=None, distrust=(), meas_out=None, scores_out=None, accept_panel_change=False,
            models=(), pages=None):
    """Steps 2..5 over the cache `target`: the release snapshot, and each model entry's rows from its own pages.
    `models` names the models the directory `pages` was fetched for: their rows come from `pages`, every other row and
    the panel must stay as shipped (else Refused, nothing written), `pages` becomes their entry when the files are
    written, and the returned five-axis document carries `_model_merge` {left_out, notes, blast_radius} to print."""
    target = Path(target)
    ref_dir = Path(references) if references is not None else (sources.REPO / 'references')
    meas_path = Path(meas_out) if meas_out is not None else (ref_dir / 'public-measurements.json')
    scores_path = Path(scores_out) if scores_out is not None else (ref_dir / 'five-axis-scores.json')

    acc = json.loads((ref_dir / 'accounts.json').read_text(encoding='utf-8-sig'))
    plans = json.loads((ref_dir / 'plan-templates.json').read_text(encoding='utf-8-sig'))
    reference_provenance.validate_accounts_provenance(acc)
    validate_user_scores(ref_dir / 'user-scores.json')

    shipped = json.loads(meas_path.read_text(encoding='utf-8-sig')) if meas_path.is_file() else None
    entries = {n: target / MODEL_ENTRIES / n for n in model_entries(target)}
    entries.update({n: Path(pages) for n in models} if pages is not None else {})
    for name in entries:
        if name not in (acc.get('models') or {}):
            raise Refused(f'model entry {target / MODEL_ENTRIES / name}: accounts.json has no row {name}; remove the entry')
    refuse_panel_models(list(entries), acc, shipped or {})
    if (target / sources.MANIFEST).is_file():
        built = measurements.Builder(target, acc).build()      # the release snapshot
    elif models and shipped is not None:
        built = copy.deepcopy(shipped)      # no release snapshot in the cache: the shipped document stands for it
    else:
        sources.manifest(target)            # raises: the cache has no fetch-manifest.json
    # What the release had (round 8): its rows are those of the published measurements that no model entry gave, plus
    # those an entry gave a row the release had without readings (`released_entry_rows`). A released row with readings
    # belongs to the release's fits and changes with the next release (the full refresh), so no entry may take its readings
    # away from them; a released row without readings takes its readings from the entry and keeps its place in the
    # declared structure (five_axis.Scorer.new_entries); any other row is new.
    release = shipped if shipped is not None else built
    given = set(release.get('model_entries') or ())
    for name in entries:
        read = sorted(f for f, items in ((release.get('models') or {}).get(name) or {}).items() if items)
        if read and name not in given:
            raise Refused((f'--model {name}' if name in models else f'model entry {target / MODEL_ENTRIES / name}')
                          + f": the release reads {name} ({', '.join(read)}); those readings belong to the release's fits "
                          'and change with the next release (the full refresh), not by a model entry; '
                          + ('nothing written and no entry stored' if name in models else 'remove the entry'))
    released = sorted(n for n in entries if n in (release.get('released_entry_rows') or ())
                      or (n in (release.get('models') or {}) and n not in given))
    left_out, notes = [], []
    for name in (m for m in acc['models'] if m in entries):    # catalog order, so the result never depends on history
        fresh = measurements.Builder(entries[name], acc).build()
        built, out, said = merge_model_rows(built, fresh, [name])
        if name in models:
            left_out += out
            notes += said
    built.pop('released_entry_rows', None)
    if released:
        built['released_entry_rows'] = released
    merge = None
    if models:
        if shipped is not None:
            moved = sorted(m for m in {*shipped['models'], *built['models']} - set(models)
                           if shipped['models'].get(m) != built['models'].get(m))
            panel = shipped.get('panel') != built.get('panel')
            if moved or panel:
                raise Refused(
                    f"--model {', '.join(models)}: the release snapshot in {target} and its other model entries do not "
                    f"give the shipped rows of {len(moved)} other models ({', '.join(moved[:5])}"
                    f"{', ...' if len(moved) > 5 else ''}){' nor the shipped panel' if panel else ''}, so the merge would "
                    "change them (a cache that is not this release's, or a catalog edit that moved another row); "
                    'nothing written and no entry stored')
        before = json.loads(scores_path.read_text(encoding='utf-8-sig')) if scores_path.is_file() else None
        merge = {'left_out': left_out, 'notes': notes, 'before': before}
    meas = built
    raw = (json.dumps(meas, indent=1, ensure_ascii=False) + '\n').encode('utf-8')
    # Built before anything is written: a refused panel change (five_axis.PanelChangeRefused) leaves every file as it was.
    doc = five_axis.build(meas, acc, plans, distrust=distrust, meas_bytes=raw, now=now,
                          previous=five_axis.previous_doc(scores_path), accept_panel_change=accept_panel_change)
    if pages is not None:
        stamp = (now or datetime.now(timezone.utc)).isoformat(timespec='seconds')
        for name in models:
            store_model_entry(target, name, pages, stamp)
    meas_path.parent.mkdir(parents=True, exist_ok=True)
    meas_path.write_bytes(raw)
    scores_path.parent.mkdir(parents=True, exist_ok=True)
    scores_path.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')

    if scores_path.resolve() == (ref_dir / 'five-axis-scores.json').resolve():
        capability_catalog.write_default_files(accounts=acc, references=ref_dir)
        validate(ref_dir)
    if merge is not None:
        doc = {**doc, '_model_merge': {'left_out': merge['left_out'], 'notes': merge['notes'],
                                       'blast_radius': blast_radius(merge['before'], doc, models)}}
    return meas, doc


def staleness(doc, now=None):
    """(rows, worst status): one row per source, aged on `now` (today) from the date the scores file holds
    (`source_dates`; data_age), and one per capacity window left without a value."""
    rows = []
    for src, row in sorted((doc.get('source_dates') or {}).items()):
        stale = False
        if row.get('missing'):
            status = 'unused' if str(row.get('reason') or '').startswith('not used') else 'missing'
        else:
            stale = data_age.is_stale(row.get('as_of'), now)
            status = 'stale' if stale else 'ok'
        component = measurements.CAPABILITY_SOURCES.get(src) if stale else None
        rows.append({'what': src, 'kind': row.get('kind'), 'as_of': row.get('as_of'),
                     'age_days': None if row.get('missing') else data_age.age_days(row.get('as_of'), now),
                     'status': status, 'note': row.get('reason') or (
                         f"stale capability source ({'.'.join(component[:2])}): still used; refresh the source"
                         if component else row.get('as_of_kind') or '')})
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


def markdown(rows, generated_at, checks=(), inversions=()):
    """The run report: the community usd checks, the measured pairs out of the size order (`inversions`: a reading kept
    as read, not a defect, five_axis.Scorer.bound_across_size) and the source ages."""
    out = []
    if checks:
        out += ['Community usd per worker-hour / the usd_per_hour in use (1.0 = agreement)', '',
                '| Model | Community $/h | usd_per_hour | Community / usd_per_hour | Source | Data date |',
                '| :--- | ---: | ---: | ---: | :--- | :--- |']
        out += [f"| {c['model']} | {c['value']} | {c['usd_per_hour']} | {c['community_over_usd']} | {c['source']} "
                f"| {c['as_of']} |" for c in checks]
        out.append('')
    if inversions:
        out += ['Values out of the size order that measurements keep, kept as read (a smaller model above the larger one of '
                'the same maker and product that dominates it; measured: the value is a measurement, else its own measured '
                'efforts hold it there)', '',
                '| Smaller model | Dominated by | Facet | Effort | Value | Measured | Dominating value | Measured |',
                '| :--- | :--- | :--- | :--- | ---: | :--- | ---: | :--- |']
        out += [f"| {i['model']} | {i['dominated_by']} | {i['facet']} | {i['effort']} | {i['value']} | "
                f"{'yes' if i['measured'] else 'no'} | {i['dominating_value']} | {'yes' if i['dominating_measured'] else 'no'} |"
                for i in inversions]
        out.append('')
    out += [f'Source ages at {generated_at} (stale after {data_age.STALE_DAYS} days)', '',
           '| Source / window | Kind | Data date | Age (days) | Status | Note |', '| :--- | :--- | :--- | ---: | :--- | :--- |']
    for r in rows:
        out.append(f"| {r['what']} | {r['kind'] or ''} | {r['as_of'] or ''} | {'' if r['age_days'] is None else r['age_days']} "
                   f"| {r['status']} | {r['note']} |")
    return '\n'.join(out) + '\n'


def main(argv=None, prog=None):
    ap = argparse.ArgumentParser(prog=prog, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog='exit status: 0 fresh, 1 stale, 2 missing or invalid, 3 refused (nothing written)')
    ap.add_argument('--data', help='directory holding calibration downloads (default: ~/.cache/agentcompass-calibration)')
    ap.add_argument('--no-fetch', action='store_true', help='rebuild from the cached sources already in DIR')
    ap.add_argument('--report', help='also write the source-age table (markdown) here')
    ap.add_argument('--distrust', action='append', default=[], help='a source id to leave out of every field (repeatable)')
    ap.add_argument('--references', help='references directory to rebuild and validate (default: skills/agent-compass/references)')
    ap.add_argument('--accept-panel-change', action='store_true',
                    help='write the rebuilt files although the panel members differ from the shipped five-axis-scores.json')
    ap.add_argument('--model', action='append', default=[],
                    help="add or update only this model's measurement rows, on the shipped scale (repeatable)")
    args = ap.parse_args(argv)
    target = sources.data_dir(args.data, create=not args.no_fetch)
    staging = None
    try:
        if args.model:          # refused before any download
            if args.no_fetch:
                raise Refused("--model fetches that model's own pages; --no-fetch already rebuilds every model entry in "
                              f'{target / MODEL_ENTRIES} from its own pages, so run it without --model')
            for name in args.model:
                if not MODEL_ID.fullmatch(name):
                    raise Refused(f'--model {name}: not a catalog id (lower-case letters, digits, ".", "_", "-")')
            ref_dir = Path(args.references) if args.references is not None else (sources.REPO / 'references')
            refuse_panel_models(args.model, json.loads((ref_dir / 'accounts.json').read_text(encoding='utf-8-sig')),
                                json.loads((ref_dir / 'public-measurements.json').read_text(encoding='utf-8-sig')))
            staging = fetch_model_pages(target, args.references)
        elif args.no_fetch:
            sources.manifest(target)
        else:
            fetch_sources(target, args.references)
            retired = retire_model_entries(target)
            if retired:
                print(f"Retired model entries (the new release snapshot reads their rows): {', '.join(retired)}")
        _, doc = rebuild(target, references=args.references, distrust=args.distrust,
                         accept_panel_change=args.accept_panel_change, models=args.model, pages=staging)
    except (Refused, five_axis.PanelChangeRefused) as exc:
        sys.stderr.write(f'{exc}\n')
        return REFUSED
    except (OSError, ValueError) as exc:
        sys.stderr.write(f'{exc}\n')
        return 2
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
    if '_model_merge' in doc:
        merge = doc['_model_merge']
        print(f"Merged rows: {', '.join(args.model)}, stored as their entries in {target / MODEL_ENTRIES} "
              '(other rows, panel, sources and targets as shipped)')
        for model, field, src, reason in merge['left_out']:
            print(f'  left out: {model} {field} {src}: {reason}; its scores use the remaining entries')
        for note in merge['notes']:
            print(f'  note: {note}')
        print(f"Blast radius: {merge['blast_radius']}")
        print()
    now = datetime.now(timezone.utc)
    rows, worst = staleness(doc, now)
    text = markdown(rows, now.isoformat(timespec='seconds'), doc.get('usd_checks') or (),
                    doc.get('measured_size_inversions') or ())
    ref_dir = Path(args.references) if args.references is not None else (sources.REPO / 'references')
    suggestions = user_scores.suggest_scores(scope='public', public_path=ref_dir / 'user-scores.json', five_data=doc,
                                             accounts=json.loads((ref_dir / 'accounts.json').read_text(encoding='utf-8-sig')))
    if suggestions['suggestions']:
        text += (f"\nManual score review suggestions ({suggestions['count']}; manual scores stay authoritative, "
                 "run `agentcompass score suggest --public` for details)\n")
    print(text)
    if args.report:
        Path(args.report).write_text(text, encoding='utf-8', newline='\n')
    return LEVEL[worst]


if __name__ == '__main__':
    sys.exit(main())
