"""Category scores, usd_per_hour, duration and plan capacity from `references/public-measurements.json`.

`python -X utf8 five_axis.py [--distrust SOURCE ...] [--user-overrides PATH] [--measurements PATH] [--out PATH]`

Writes `references/five-axis-scores.json`, the only model scoring the router reads (allocator.five_axis_overlay). The
user's taxonomy (2026-11-11): four categories, each made of facets, each facet read from public boards; the router
scores on the four categories and the quota economy together. Nothing else scores a model: the tier is not a score.

| category | facet <- boards | combination |
|---|---|---|
| divergence | creative_writing <- lmarena.creative_writing | the facet |
| coding | execution <- arena.agent_code, aa.terminalbench_4, aa.frontiercode_2; faithfulness <- arena.agent_alignment | min |
| aesthetics | design <- lmarena.webdev, designarena.website, designarena.3d, designarena.svg | the facet |
| tool_operation | responsiveness <- aa.ttfa (-ln seconds); image_recognition <- aa.mmmu_pro, lmarena.vision | per level |

- Scale (measurements.py): every board is a panel standard score, z = (x - x_anchor) / sd_panel, the anchor (Opus 5.5)
  at High 0 and one unit one standard deviation over the panel (the fleet models on every scoring board that lists all six
  anchor rows; its n is written to `anchor.panel_n`). No bridging, no kind conversion, no cap.
- Parallel boards of one facet are linked (`link`): each board is centred on the anchor's High value, then mean-mean
  linked over the panel members it shares with the full-panel boards, then the facet is re-centred on the anchor; the
  facet is the mean of its linked boards. The offsets are written to `links`.
- Efforts (`board_curves`, `listed_shift`): an Artificial Analysis board reads the candidate's own effort row; a missing
  row takes the same-series sibling's step, else the fleet median step (n >= 3), else holds the nearest listed value. An
  arena or LMArena board lists one variant, so it shifts by the same model's (or series') z(effort) - z(listed effort) on
  the same facet's AA board; with no AA board for the facet it does not shift and the facet is `measured_partial`.
- tool_operation by level (TOOL_LEVEL_FACETS): level 1 asks no ability (the router's image-input check sits at level 3),
  level 2 responsiveness, level 3 min(responsiveness, image recognition); `by_level` holds both per effort.
- A facet a model lacks is estimated: the nearest same-series sibling's value moved along the facet's fit on the tier by
  the tier difference (calibration/series.py), else the fit at the model's tier. The fit is kept without a skill gate
  (user, 2026-11-11: fit plus periodic recalibration) and its record keeps r, skill, loo_rmse, n and the slope
  (`estimate_fits`). Estimates score at their value, status `estimated`; a direct measurement replaces one at the next
  refresh. A category computes from the facets that have a value; one with a facet that has none is partial (a yellow
  `facet_unmeasured_no_estimate`), and one with no value at all is `missing` (mode B when a task demands it).
- Targets (`anchor.targets`, `targets`): per category and level, level 1 the panel's weakest value at High, level 2 the
  panel median, level 3 the anchor; `zero` is the level-1 target, the fit's zero point. tool_operation asks per facet,
  and a level never asks less of a facet than the level below (user, 2026-11-11: the anchor's responsiveness, 0, is
  below the panel median, so level 3 asks the median of it). `continuity` is t3 - t2 (tool_operation: the smallest over
  the facets both levels ask for); the router counts the largest positive one among the task's highest demands.
- Checks: structural ones drop a listing (a variant tag that is not an effort, a raw value out of range, one listing
  matched to two models, a duplicate board); statistical ones only note (`checks`).
- The tier (calibration/tiers.py) is the model's overall level: the arena.ai Overall board mapped onto the user's scale
  through the six anchor rows, else another overall board interpolated onto it (`tier.aa_index`), else the nearest
  same-series sibling's tier. Its one use is estimating a facet a model lacks.
- A row that `accounts.json` `same_model` declares to be the very model another row names (a vendor statement with its
  url, or a dated user statement, kind `user`) takes that model's facets as they stand, measured or estimated (method
  `same_model`), for the categories the row `carries`, and its Time/Task. The other categories are the channel's own.
- A dated user override (`--user-overrides`, source `user`) replaces a public axis value; none is shipped.
- usd_per_hour has one basis (rounds 8-9, user 2026-11-09 "全部都用arena"): `arena.agent_sessions` (arena.ai Agent / Overall
  real sessions, Cost/Task P50 / Time/Task P50 x 60, kind `community`), each source's value at High (else its effort-less
  listing, else the median of its listed efforts). For a model arena does not list with cost and time, the labelled
  option B (`derived.community_mix`, `fallback: True`: AA's measured output speed per hour priced at the community token
  mix at list prices); a model with a same-series sibling that has a direct value takes that sibling's times the ratio
  of the two cited API list prices over the full community token mix (`convert_usd`, `converted`). A model with none of
  these has no usd: out of automatic routing on a metered account (billed or quota windows), and a named dispatch runs
  with its cost unpriced. `--distrust` removes a source from every field.
- Plan capacity comes down a ladder (rounds 4-5, user 2026-11-09: "都用社区真实使用量 且保持最新"; capacity is never
  left unknown), per window of each plan in plan-templates.json, recording the rung: 1 the median of the cited community
  observations (`community.py`), or a window ratio a community source states (Fable weekly = half the weekly limit);
  2 a community time-to-exhaust report x the reported model's usd; 3 a vendor-published plan multiplier x the sibling
  plan's value (also kept as a cross-check where a lower rung won). All three empty is a red data error surfaced in
  routing notes (the live percentage still prices scarcity); the packaged template figure is never a fallback. A source
  older than STALE_DAYS (data date, else fetch date) is a yellow note; a source not fetched, or whose sentence is gone,
  contributes nothing (red).
- Each window's reference-model hours are its capacity over that model's usd_per_hour.
- economy is priced by the router at run time from usd_per_hour, the duration and the window's capacity and recovery.
"""
import argparse
import hashlib
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import measurements
import series
import sources

OUT = sources.REPO / 'references' / 'five-axis-scores.json'
PLANS = sources.REPO / 'references' / 'plan-templates.json'
SCHEMA = 'agent-compass.five-axis-scores/2'
AXES = measurements.AXES
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
# The facets each tool_operation level asks for (user, 2026-11-11): level 1 no ability beyond the image-input check of
# level 3, level 2 responsiveness, level 3 responsiveness and image recognition; their `min`.
TOOL_LEVEL_FACETS = {1: (), 2: ('responsiveness',), 3: ('responsiveness', 'image_recognition')}
# Window -> the model whose hours count the window's capacity (the plan's own flagship on that pool).
REFERENCE_MODELS = {
    'claude': {'five_hour': 'claude-opus-5-5', 'seven_day': 'claude-opus-5-5', 'weekly_fable': 'claude-fable-5-1'},
    'codex': {'usage': 'gpt-6.1-sol', 'five_hour': 'gpt-6.1-sol'},
    'pi': {'usage': 'gpt-6.1-sol'},
    'antigravity': {'gemini-weekly': 'gemini-4-argon-high', 'gemini-5h': 'gemini-4-argon-high',
                    '3p-weekly': 'claude-opus-5-5', '3p-5h': 'claude-opus-5-5'},
}
TIER_DIRECT = ('user.anchor_tier', 'tier.arena_overall')    # a tier read off arena.ai Overall (the anchors are rows of it)
TIER_OTHER = ('tier.aa_index',)     # a non-arena overall board interpolated onto that scale; no other source is a tier
MIN_FIT_POINTS = 8      # models a type-score-on-tier fit needs (review F1: one public point is not a fit)


def mean(xs):
    return round(float(np.mean(xs)), 4) if xs else None


def source_value(entries):
    """One source's figure: High, else its effort-less listing, else the median of its listed efforts."""
    by_effort = {e.get('effort') or 'default': e['value'] for e in entries}
    if 'high' in by_effort:
        return by_effort['high'], 'High listing'
    if 'default' in by_effort:
        return by_effort['default'], 'the only listing (no effort named)'
    listed = {k: v for k, v in by_effort.items() if k in EFFORTS} or by_effort
    return round(statistics.median(listed.values()), 3), f'median of the listed efforts {sorted(listed)}'


def pick_usd(entries):
    """usd_per_hour from one source's listings (`entries`, all of the first entry's source): its value at High, else its
    effort-less listing, else the median of its listed efforts."""
    if not entries:
        return {'value': None, 'status': 'missing', 'sources': {}, 'merge': None}
    src = entries[0]['source']
    rows = [e for e in entries if e['source'] == src]
    value, how = source_value(rows)
    cited = {'value': value, 'how': how, 'kind': rows[0].get('kind'), 'as_of': rows[0].get('as_of'),
             'url': rows[0].get('url'), 'method': rows[0].get('method'),
             'by_effort': {r.get('effort') or 'default': r['value'] for r in rows}}
    if rows[0].get('sessions') is not None:
        cited['sessions'] = rows[0]['sessions']
    return {'value': value, 'status': 'ok', 'merge': f'{src}: {how}', 'used': [src], 'sources': {src: cited}}


def pick_image_input(entries):
    """Image-input modality from cited public sources (round 11, user 2026-11-10): a public vision/GUI benchmark listing
    wins over a vendor/OpenRouter modality flag; without any source, status is `unknown` (mode B when tool_operation is 3)."""
    if not entries:
        return {'value': None, 'status': 'unknown', 'source': None, 'kind': None, 'sources': [],
                'reason': 'no cited public modality source'}
    bench_yes = [e for e in entries if e.get('kind') == 'benchmark' and e.get('value') is True]
    if bench_yes:
        e0 = bench_yes[0]
        return {'value': True, 'status': 'yes', 'source': e0['source'], 'kind': 'benchmark',
                'as_of': e0.get('as_of'), 'url': e0.get('url'), 'method': e0.get('method'),
                'sources': [e['source'] for e in bench_yes]}
    yes = [e for e in entries if e.get('value') is True]
    if yes:
        e0 = yes[0]
        return {'value': True, 'status': 'yes', 'source': e0['source'], 'kind': e0.get('kind'),
                'as_of': e0.get('as_of'), 'url': e0.get('url'), 'method': e0.get('method'),
                'sources': [e['source'] for e in yes]}
    no = [e for e in entries if e.get('value') is False]
    if no:
        e0 = no[0]
        return {'value': False, 'status': 'no', 'source': e0['source'], 'kind': e0.get('kind'),
                'as_of': e0.get('as_of'), 'url': e0.get('url'), 'method': e0.get('method'),
                'sources': [e['source'] for e in no]}
    return {'value': None, 'status': 'unknown', 'source': None, 'kind': None, 'sources': [],
            'reason': 'no cited public modality source'}


def pick_fast_mode(model, entries, models=None):
    """Public fast-mode price and speed multipliers (round 11, user 2026-11-10)."""
    if entries:
        e0 = entries[0]
        val = e0.get('value') or {}
        return {'status': 'ok', 'price_multiplier': val.get('price_multiplier'),
                'speed_multiplier': val.get('speed_multiplier'), 'source': e0['source'],
                'kind': e0.get('kind', 'vendor'), 'as_of': e0.get('as_of'), 'url': e0.get('url'),
                'pricing_url': e0.get('pricing_url'), 'fast_price': e0.get('fast_price'),
                'standard_price': e0.get('standard_price'), 'method': e0.get('method')}
    if bool(((models or {}).get(model) or {}).get('fast_capable')):
        return {'status': 'missing', 'price_multiplier': None, 'speed_multiplier': None, 'source': None,
                'reason': 'provider does not publish a fast-mode burn or speed multiplier'}
    return {'status': 'unsupported', 'price_multiplier': None, 'speed_multiplier': None, 'source': None}


def pick_price(entries):
    """Public list price (USD per million tokens) for converting local token counts (e.g. opencode.db) into USD."""
    for src in ('aa.price', 'openrouter.price', 'vendor.price'):
        for e in entries or ():
            if e.get('source') == src and isinstance(e.get('value'), dict):
                v = e['value']
                if (v.get('output') or 0) > 0 or (v.get('input') or 0) > 0:
                    return {'value': v, 'source': src, 'kind': e.get('kind', 'vendor'),
                            'as_of': e.get('as_of'), 'url': e.get('url'), 'listed_as': e.get('listed_as')}
    return None


class Scorer:
    def __init__(self, meas, acc, distrust=(), overrides=()):
        self.meas, self.acc = meas, acc
        self.distrust = set(distrust)
        self.overrides = list(overrides)
        self.models = {k: v for k, v in acc['models'].items() if isinstance(v, dict)}

    def trusted(self, entries):
        return [e for e in entries or () if e['source'] not in self.distrust]

    def tier_records(self, rows, index, board):
        """{model: {'value', 'method', 'sources', ...}}: arena Overall, else the mean of the other overall boards'
        interpolated tiers, else the nearest same-series sibling's tier, else value None."""
        out = {}
        for m, r in rows.items():
            entries = self.trusted(r['fields'].get('tier'))
            direct = [e for e in entries if e['source'] in TIER_DIRECT]
            other = [e for e in entries if e['source'] in TIER_OTHER]
            chosen, method = (direct, 'anchor' if direct and direct[0]['source'] == 'user.anchor_tier' else 'arena_overall') \
                if direct else (other, 'other_overall')
            if chosen:
                cite = [{k: e.get(k) for k in ('source', 'value', 'as_of', 'url', 'listed_as', 'neighbours', 'n') if e.get(k) is not None}
                        for e in chosen]
                out[m] = {'value': round(float(np.mean([e['value'] for e in chosen])), 3), 'method': method, 'sources': cite}
        direct_tier = {m: t['value'] for m, t in out.items()}
        sibs, same = series.groups(self.acc), series.same_model(self.acc)
        for m in rows:
            if m in out:
                continue
            bases = series.nearest(m, [b for b in sibs.get(m, ()) if b in direct_tier], index, board, direct_tier, same)
            out[m] = ({'value': direct_tier[bases[0]], 'method': 'sibling', 'base_model': bases[0], 'sources': []} if bases
                      else {'value': None, 'method': 'none', 'sources': []})
        return out

    @staticmethod
    def fit(points, require_skill=False):
        """Linear fit of y on x with its leave-one-out record, or (None, why) under MIN_FIT_POINTS models, with one x
        for all, or (when `require_skill` is true, as for the AA-on-arena duration fit) without a positive slope and
        out-of-sample skill. Facet-on-tier fits have no skill gate (user, 2026-11-11: sibling conversion, else the
        Overall fit, recalibrated as measurements arrive): the record carries r, the out-of-sample skill and the
        leave-one-out RMSE so a weak fit shows."""
        if len(points) < MIN_FIT_POINTS:
            return None, f'{len(points)} models with both (a fit needs {MIN_FIT_POINTS})'
        x, y = np.array([p[0] for p in points]), np.array([p[1] for p in points])
        if float(np.ptp(x)) == 0.0:
            return None, 'every model has the same tier'
        b, a = np.polyfit(x, y, 1)
        if require_skill and b <= 0:
            return None, f'slope {b:.3f} is not positive'
        n = len(x)
        err = base = 0.0
        for i in range(n):
            keep = np.arange(n) != i
            if float(np.ptp(x[keep])) == 0.0:
                return None, 'every other model has the same tier'
            bi, ai = np.polyfit(x[keep], y[keep], 1)
            err += (y[i] - (ai + bi * x[i])) ** 2
            base += (y[i] - y[keep].mean()) ** 2
        skill = float(1.0 - err / base) if base > 0 else 0.0
        if require_skill and skill <= 0:
            return None, f"out-of-sample skill {skill:.3f} (leave-one-out error not below the mean predictor's)"
        return {'a': float(a), 'b': float(b), 'r': float(np.corrcoef(x, y)[0, 1]),
                'skill': skill, 'loo_rmse': float(np.sqrt(err / n)), 'n': n,
                'x_min': float(x.min()), 'x_max': float(x.max()), 'y_min': float(y.min()), 'y_max': float(y.max())}, None

    @staticmethod
    def predict(f, x):
        """The fit at tier x, x clamped to the tiers the fit saw and the value kept inside the range its measured points
        reach (round 6, round 058 review F2: a tier above the best sampled model is not extrapolated)."""
        x = float(np.clip(x, f['x_min'], f['x_max']))
        return round(float(np.clip(f['a'] + f['b'] * x, f['y_min'], f['y_max'])), 4)

    @staticmethod
    def fit_record(fid, f):
        return {'fit': fid, 'r': round(f['r'], 4), 'skill': round(f['skill'], 4), 'loo_rmse': round(f['loo_rmse'], 4),
                'n': f['n'], 'slope': round(f['b'], 4)}

    # --- boards -> facets ---------------------------------------------------------------------------------------
    def board_entries(self, fields):
        """{(category, facet): {source: entry}} of the trusted capability entries."""
        out = {}
        for a in AXES:
            for e in self.trusted(fields.get(a)):
                if e.get('facet') and isinstance(e.get('value'), (int, float)):
                    out.setdefault((a, e['facet']), {}).setdefault(e['source'], e)
        return out

    @staticmethod
    def aa_rows(entry):
        """{effort: z} of an Artificial Analysis entry; an effort-less listing is the model's one configuration, keyed High."""
        rows = {k: v for k, v in (entry.get('z_by_effort') or {}).items() if k in EFFORTS or k == 'default'}
        if 'default' in rows:
            rows.setdefault('high', rows.pop('default'))
        return {k: v for k, v in rows.items() if k in EFFORTS}

    def fill_efforts(self, rows, model, src, aa_all, sibs):
        """({effort: z} over EFFORTS, {effort: basis}) of one AA board for `model`: its listed efforts as listed; a missing
        effort from the nearest listed one by the nearest same-series sibling's step on this board, else the fleet median
        step (z(e) - z(e0) over the models listing both, at least MIN_FLEET_RATIO_N); never held flat."""
        out, basis = dict(rows), {e: {'method': 'listed'} for e in rows}
        board = aa_all.get(src) or {}
        for e in EFFORTS:
            if e in out or not rows:
                continue
            e0 = nearest_effort(rows, e)
            sib = next((b for b in sibs if e in (board.get(b) or {}) and e0 in board[b]
                        and float(np.ptp(list(board[b].values()))) > 0), None)
            if sib:
                step = board[sib][e] - board[sib][e0]
                out[e], basis[e] = round(rows[e0] + step, 4), {'method': 'series_effort', 'base_model': sib, 'from_effort': e0,
                                                               'step': round(step, 4)}
                continue
            steps = [r[e] - r[e0] for r in board.values() if e in r and e0 in r and float(np.ptp(list(r.values()))) > 0]
            if len(steps) >= MIN_FLEET_RATIO_N:
                step = float(np.median(steps))
                out[e], basis[e] = round(rows[e0] + step, 4), {'method': 'fleet_effort_step', 'from_effort': e0,
                                                               'step': round(step, 4), 'n': len(steps)}
        return out, basis

    @staticmethod
    def _has_effort_curve(aa_map):
        return bool(aa_map and len(aa_map) >= 2 and float(np.ptp(list(aa_map.values()))) > 0)

    def aa_capability_boards(self, rows):
        """({aa_source: {model: {effort: z}}}, {aa_source: {model: high_z}}) for AA capability boards (excluding aa.ttfa)."""
        aa_own, aa_high = {}, {}
        for m, r in rows.items():
            for e in self.trusted(r['fields'].get('intelligence_index')):
                if e.get('source') == 'aa.index' and isinstance(e.get('z_by_effort'), dict):
                    mapped = self.aa_rows(e)
                    if mapped:
                        aa_own.setdefault('aa.index', {})[m] = mapped
                        if 'high' in mapped:
                            aa_high.setdefault('aa.index', {})[m] = mapped['high']
            for (a, f), srcs in r['boards'].items():
                for src, e in srcs.items():
                    if src.startswith('aa.') and src != measurements.AA_TTFA:
                        mapped = self.aa_rows(e)
                        if mapped:
                            aa_own.setdefault(src, {})[m] = mapped
                            if 'high' in mapped:
                                aa_high.setdefault(src, {})[m] = mapped['high']
                        elif isinstance(e.get('value'), (int, float)) and e.get('listed_effort') in (None, 'high'):
                            aa_high.setdefault(src, {})[m] = float(e['value'])
        return aa_own, aa_high

    def compute_effort_correlations(self, rows, aa_high):
        """{non_aa_source: {'board', 'r', 'n', 'candidates'}} ranking AA capability boards by Pearson r over shared models."""
        non_aa = {}
        for m, r in rows.items():
            for (a, f), srcs in r['boards'].items():
                for src, e in srcs.items():
                    if not src.startswith('aa.') and isinstance(e.get('value'), (int, float)):
                        non_aa.setdefault(src, {})[m] = float(e['value'])
        corrs = {}
        for src, vals in sorted(non_aa.items()):
            cands = []
            for aa_src, avals in sorted(aa_high.items()):
                shared = [m for m in vals if m in avals]
                if len(shared) < MIN_FIT_POINTS:
                    continue
                x = np.array([vals[m] for m in shared], dtype=float)
                y = np.array([avals[m] for m in shared], dtype=float)
                if float(np.ptp(x)) <= 0 or float(np.ptp(y)) <= 0:
                    continue
                r_val = float(np.corrcoef(x, y)[0, 1])
                if r_val > 0:
                    cands.append({'board': aa_src, 'r': round(r_val, 4), 'n': len(shared)})
            cands.sort(key=lambda c: (-c['r'], -c['n'], c['board']))
            if cands:
                corrs[src] = {'board': cands[0]['board'], 'r': cands[0]['r'], 'n': cands[0]['n'], 'candidates': cands}
        return corrs

    def board_curves(self, rows):
        """{model: {(category, facet): {source: {'by_effort', 'effort_shift', 'listed_as'}}}} before linking."""
        aa_all = {}
        for m, r in rows.items():
            for (a, f), srcs in r['boards'].items():
                for src, e in srcs.items():
                    if e['source'].startswith('aa.'):
                        aa_all.setdefault(src, {})[m] = self.aa_rows(e)
        aa_cap_own, aa_cap_high = self.aa_capability_boards(rows)
        for aa_src, per_m in aa_cap_own.items():
            aa_all.setdefault(aa_src, {}).update(per_m)
        self.effort_correlations = self.compute_effort_correlations(rows, aa_cap_high)
        self._aa_cap_own = aa_cap_own
        self._aa_all = aa_all
        sib_order = {m: series.nearest(m, self.sibs.get(m, ()), self.index, self.board, self.direct_tier, self.same)
                     for m in rows}
        curves = {}
        for m, r in rows.items():
            out = curves.setdefault(m, {})
            filled = {}
            for (a, f), srcs in r['boards'].items():
                for src, e in srcs.items():
                    if src.startswith('aa.'):
                        own_aa = self.aa_rows(e)
                        if self._has_effort_curve(own_aa) or any(self._has_effort_curve((aa_cap_own.get(s) or {}).get(m)) for s in aa_cap_own):
                            filled[src] = self.fill_efforts(own_aa, m, src, aa_all, sib_order[m])
                        else:
                            filled[src] = (dict(own_aa), {k: {'method': 'listed'} for k in own_aa})
            for (a, f), srcs in r['boards'].items():
                for src, e in srcs.items():
                    if src in filled:
                        by, basis = filled[src]
                        has_multi = len(by) >= 2 and float(np.ptp(list(by.values()))) > 0
                        out.setdefault((a, f), {})[src] = {'by_effort': by, 'effort_basis': basis,
                                                           'effort_shift': 'measured' if has_multi else 'single_aa',
                                                           'listed_as': e.get('listed_as')}
                        continue
                    listed = e.get('listed_effort')
                    native = self.aa_rows(e) if isinstance(e.get('z_by_effort'), dict) else {}
                    ref_eff = listed if listed in EFFORTS else 'high'
                    if not native:
                        native = {ref_eff: round(float(e['value']), 4)}
                    by, shifted, how = self.listed_shift(m, a, f, src, native, ref_eff, aa_cap_own, aa_all)
                    out.setdefault((a, f), {})[src] = {'by_effort': by, 'effort_shift': 'aa' if shifted else 'none',
                                                       'effort_shift_basis': how, 'listed_as': e.get('listed_as'),
                                                       'listed_effort': listed}
        return curves

    def listed_shift(self, m, a, f, src, native, ref_eff, aa_cap_own, aa_all):
        """({effort: z}, shifted_bool, basis) for a non-AA board `src`. Uses ONLY `m`'s own per-effort AA data on the most
        correlated AA board (recording board, r, n); if none exists, keeps only `native` and flags `measured_partial`."""
        corr_entry = self.effort_correlations.get(src)
        candidates = list((corr_entry or {}).get('candidates') or ())
        if not candidates:
            # Small synthetic sets (< MIN_FIT_POINTS models) fall back only to same-facet AA boards where `m` has >=2 efforts.
            same_facet = [s for s in measurements.CAPABILITY_SOURCES
                          if s.startswith('aa.') and s != measurements.AA_TTFA and measurements.CAPABILITY_SOURCES[s] == (a, f)]
            candidates = [{'board': s, 'r': None, 'n': len(aa_all.get(s) or {}), 'same_facet': True} for s in same_facet]
        chosen = None
        for cand in candidates:
            own = (aa_cap_own.get(cand['board']) or {}).get(m)
            if self._has_effort_curve(own):
                chosen = (cand, own)
                break
        if chosen is None:
            why = (f"model {m} has no multi-effort Artificial Analysis curve on correlated boards"
                   if candidates else f"no correlated Artificial Analysis board with >= {MIN_FIT_POINTS} shared models")
            return dict(native), False, why
        cand, own = chosen
        aa_curve, _ = self.fill_efforts(own, m, cand['board'], aa_all, [])
        shared_anchors = [e for e in native if e in aa_curve]
        if not shared_anchors:
            return dict(native), False, f"model {m} AA curve on {cand['board']} lacks reference effort {ref_eff}"
        by = dict(native)
        for x in EFFORTS:
            if x in by or x not in aa_curve:
                continue
            e0 = min(shared_anchors, key=lambda k: (abs(EFFORTS.index(k) - EFFORTS.index(x)), EFFORTS.index(k)))
            by[x] = round(native[e0] + (aa_curve[x] - aa_curve[e0]), 4)
        basis = {'board': cand['board'], 'r': cand['r'], 'n': cand['n'], 'model': m, 'from_effort': ref_eff,
                 **({'same_facet': True} if cand.get('same_facet') else {})}
        return by, True, basis

    def link(self, curves, panel):
        """Standardise full-panel boards on the panel's High rows (anchor @ High = 0, sd_panel @ High = 1), mean-mean link
        parallel full-panel boards, mean-sigma link secondary (partial-panel) boards onto the core over shared panel models,
        then re-center the facet on the anchor at High."""
        anchor = measurements.ANCHOR
        facets = {key for c in curves.values() for key in c}
        links = {}
        for key in facets:
            srcs = sorted({src for c in curves.values() for src in c.get(key, {})})
            a0 = {src: ((curves.get(anchor) or {}).get(key) or {}).get(src, {}).get('by_effort', {}).get('high') for src in srcs}
            for c in curves.values():
                for src, rec in (c.get(key) or {}).items():
                    if a0.get(src) is not None:
                        rec['by_effort'] = {e: v - a0[src] for e, v in rec['by_effort'].items()}
            high = {src: {m: c[key][src]['by_effort']['high'] for m, c in curves.items()
                          if 'high' in (c.get(key) or {}).get(src, {}).get('by_effort', {})} for src in srcs}
            full = [src for src in srcs if len(panel) >= 2 and all(m in high[src] for m in panel)]
            meas_sources = self.meas.get('sources') or {}
            scales, offsets = {}, {}
            for src in full:
                if src in meas_sources:
                    sd_h = statistics.stdev(high[src][m] for m in panel)
                    sc = (1.0 / sd_h) if sd_h > 0 else 1.0
                    scales[src] = sc
                    if sc != 1.0:
                        for c in curves.values():
                            rec = (c.get(key) or {}).get(src)
                            if rec is not None:
                                rec['by_effort'] = {e: v * sc for e, v in rec['by_effort'].items()}
                        high[src] = {m: v * sc for m, v in high[src].items()}
                else:
                    scales[src] = 1.0
            if len(srcs) > 1 and full:
                means = {src: float(np.mean([high[src][m] for m in panel])) for src in full}
                grand = float(np.mean(list(means.values())))
                offsets = {src: grand - means[src] for src in full}
                core = {m: float(np.mean([high[src][m] + offsets[src] for src in full])) for m in panel}
                for src in srcs:
                    if src not in full:
                        shared = [m for m in panel if m in high[src]]
                        if len(shared) >= 2:
                            mu_c = float(np.mean([core[m] for m in shared]))
                            sd_c = statistics.stdev(core[m] for m in shared)
                            mu_s = float(np.mean([high[src][m] for m in shared]))
                            sd_s = statistics.stdev(high[src][m] for m in shared)
                            sc = (sd_c / sd_s) if sd_s > 0 else 1.0
                            off = mu_c - sc * mu_s
                            scales[src], offsets[src] = sc, off
                            for c in curves.values():
                                rec = (c.get(key) or {}).get(src)
                                if rec is not None:
                                    rec['by_effort'] = {e: v * sc for e, v in rec['by_effort'].items()}
                            high[src] = {m: v * sc for m, v in high[src].items()}
                        elif shared:
                            scales[src] = 1.0
                            offsets[src] = float(np.mean([core[m] - high[src][m] for m in shared]))
            else:
                for src in srcs:
                    scales.setdefault(src, 1.0)
                    offsets.setdefault(src, 0.0)
            own = [src for src in ((curves.get(anchor) or {}).get(key) or {}) if anchor in high.get(src, {})]
            center = float(np.mean([high[src][anchor] + offsets.get(src, 0.0) for src in own])) if own else 0.0
            for c in curves.values():
                for src, rec in (c.get(key) or {}).items():
                    off = offsets.get(src, 0.0) - center
                    rec['link_scale'] = round(scales.get(src, 1.0), 4)
                    rec['link_offset'] = round(off, 4)
                    rec['by_effort'] = {e: round(v + off, 4) for e, v in rec['by_effort'].items()}
            links['.'.join(key)] = {
                'boards': srcs,
                'scales': {k: round(v, 4) for k, v in scales.items()},
                'offsets': {k: round(offsets.get(k, 0.0) - center, 4) for k in srcs},
                'sd_at_high': {src: round(statistics.stdev(c[key][src]['by_effort']['high'] for m in panel for c in [curves[m]]), 4)
                               for src in full},
                'full_panel_boards': full,
                'panel_n': len(panel),
            }
        return links

    def facet_value(self, recs):
        common = [e for e in EFFORTS if all(e in r['by_effort'] for r in recs.values())]
        effs = common or [e for e in EFFORTS if any(e in r['by_effort'] for r in recs.values())]
        by = {e: round(float(np.mean([r['by_effort'][e] for r in recs.values() if e in r['by_effort']])), 4) for e in effs}
        partial = [src for src, r in recs.items() if r.get('effort_shift') in ('none', 'single_aa')]
        unshifted_non_aa = [src for src, r in recs.items() if r.get('effort_shift') == 'none']
        return {'value': by.get('high'), 'by_effort': by, 'status': 'measured_partial' if unshifted_non_aa else 'measured',
                'sources': {src: {'by_effort': r['by_effort'], 'listed_as': r.get('listed_as'),
                                  'effort_shift': r.get('effort_shift'),
                                  'link_scale': r.get('link_scale', 1.0), 'link_offset': r.get('link_offset'),
                                  **({'effort_shift_basis': r['effort_shift_basis']} if r.get('effort_shift_basis') else {}),
                                  **({'effort_basis': r['effort_basis']} if r.get('effort_basis') else {})}
                            for src, r in recs.items()},
                **({'measured_partial': {'no_effort_shift': partial}} if unshifted_non_aa else {})}

    def _model_aa_curve_for_tier_fit(self, m):
        """Return `(aa_board, {effort: z})` for `m` when `m` has its own multi-effort AA curve containing High, else None."""
        aa_cap_own = getattr(self, '_aa_cap_own', None) or {}
        aa_all = getattr(self, '_aa_all', None) or {}
        for aa_src in ('aa.index', 'aa.terminalbench_4', 'aa.frontiercode_2', 'aa.mmmu_pro'):
            own = (aa_cap_own.get(aa_src) or {}).get(m)
            if self._has_effort_curve(own):
                filled, _ = self.fill_efforts(own, m, aa_src, aa_all, [])
                if 'high' in filled and self._has_effort_curve(filled):
                    return aa_src, filled
        return None

    def estimate(self, m, base, key, facets, fits, rejected, tier):
        """Facet `key` of model `m` (module docstring): the nearest same-series sibling's curve moved along the facet's
        tier fit by the two tiers' difference, else the fit at the model's tier shifted only where `m` has its own AA curve."""
        fid = f'{key[0]}.{key[1]}~tier'
        f = fits.get(key)
        t = tier[m]['value']
        if base is not None:
            vb = facets[base][key]
            shift = f['b'] * (t - tier[base]['value']) if f and tier[base]['value'] is not None else 0.0
            by = {e: round(v + shift, 4) for e, v in vb['by_effort'].items()}
            est = {'method': 'sibling_shift', 'base_model': base, 'base_value': vb['value'], 'base_tier': tier[base]['value'],
                   'tier': t, 'tier_source': tier[m]['method'], 'shift': round(shift, 4),
                   **(self.fit_record(fid, f) if f else {'shift_basis': f"shift 0: no fit, {rejected.get(fid)}"})}
            if len(by) == 1 and 'high' in by:
                aa_pair = self._model_aa_curve_for_tier_fit(m)
                if aa_pair is not None:
                    aa_src, own = aa_pair
                    v_high = by['high']
                    by = {e: round(v_high + (own[e] - own['high']), 4) for e in EFFORTS if e in own}
                    est['effort'] = f'shifted from High along {m} on {aa_src}'
        else:
            v = self.predict(f, t)
            aa_pair = self._model_aa_curve_for_tier_fit(m)
            if aa_pair is not None:
                aa_src, own = aa_pair
                by = {e: round(v + (own[e] - own['high']), 4) for e in EFFORTS if e in own}
                effort_note = f'shifted from High along {m} on {aa_src}'
            else:
                by = {'high': v}
                effort_note = 'High only: no multi-effort Artificial Analysis curve for this model'
            est = {'method': 'tier_fit', 'tier': t, 'tier_source': tier[m]['method'], **self.fit_record(fid, f),
                   'effort': effort_note}
            if not f['x_min'] <= t <= f['x_max']:
                est['tier_clamped_to'] = f['x_min'] if t < f['x_min'] else f['x_max']
        return {'value': by.get('high'), 'by_effort': by, 'status': 'estimated', 'sources': {}, 'estimate': est}

    @staticmethod
    def category(a, facets):
        """{'value', 'by_effort', 'status', ...} of category `a` from its facet records. Never silently skips a missing
        required facet (review F1): `coding` requires both facets, and `tool_operation` requires each level's facets."""
        names = AXES[a]
        present = [f for f in names if f in facets]
        missing = [f for f in names if f not in facets]
        if not present:
            return {'value': None, 'by_effort': {}, 'status': 'missing', 'facets': {}, 'missing_facets': missing,
                    'flag': 'missing', 'reason': 'no trusted source, no sibling and no fit for any of its facets'}

        def combine(selected):
            miss = [f for f in selected if f not in facets]
            if miss:
                return {}, [f for f in selected if f in facets], miss
            effs = [e for e in EFFORTS if all(e in facets[f]['by_effort'] for f in selected)]
            return ({e: round(min(facets[f]['by_effort'][e] for f in selected), 4) for e in effs},
                    list(selected), [])

        out = {'facets': {f: facets[f] for f in present}, 'missing_facets': missing}
        if a == 'tool_operation':
            levels = {}
            for level, selected in TOOL_LEVEL_FACETS.items():
                if selected:
                    by_l, used, miss_l = combine(selected)
                    levels[str(level)] = {'by_effort': by_l, 'facets': used,
                                          **({'missing_facets': miss_l} if miss_l else {})}
            out['by_level'] = levels
            by = levels['3']['by_effort']
        else:
            by, _, _ = combine(names)
            if missing:
                out['flag'] = 'partial'
        statuses = {facets[f]['status'] for f in present}
        out.update(value=by.get('high'), by_effort=by,
                   status='partial' if missing else 'estimated' if 'estimated' in statuses
                   else 'measured_partial' if 'measured_partial' in statuses else 'measured')
        return out

    def score(self):
        rows = {}
        for m in self.models:
            fields = self.meas['models'].get(m) or {}
            rows[m] = {'fields': fields, 'boards': self.board_entries(fields)}
        self.index = {m: ((self.trusted(r['fields'].get('intelligence_index')) or [{}])[0]).get('value') for m, r in rows.items()}
        self.board = series.boards(self.meas)
        self.sibs, self.same = series.groups(self.acc), series.same_model(self.acc)
        tier = self.tier_records(rows, self.index, self.board)
        self.direct_tier = {m: t['value'] for m, t in tier.items() if t['method'] in ('anchor', 'arena_overall', 'other_overall')}
        panel = [m for m in (self.meas.get('panel') or {}).get('models') or () if m in rows]
        self.effort_correlations = {}
        self._aa_cap_own = {}
        curves = self.board_curves(rows)
        links = self.link(curves, panel)
        facets = {m: {key: self.facet_value(recs) for key, recs in c.items()} for m, c in curves.items()}
        keys = [(a, f) for a, fs in AXES.items() for f in fs]
        fits, rejected = {}, {}
        for key in keys:
            pts = [(self.direct_tier[m], facets[m][key]['value']) for m in rows
                   if key in facets[m] and facets[m][key].get('value') is not None and m in self.direct_tier]
            fits[key], why = self.fit(pts)
            if why:
                rejected[f'{key[0]}.{key[1]}~tier'] = why
        declared = [m for m in rows if (self.same.get(m) or {}).get('model') in rows]
        for m in rows:
            if m in declared or tier[m]['value'] is None:
                continue
            for key in keys:
                if key in facets[m]:
                    continue
                bases = [b for b in series.nearest(m, self.sibs.get(m, ()), self.index, self.board, self.direct_tier, self.same)
                         if (facets[b].get(key) or {}).get('status', '').startswith('measured')
                         and (facets[b].get(key) or {}).get('value') is not None and tier[b]['value'] is not None]
                if bases or fits.get(key):
                    facets[m][key] = self.estimate(m, bases[0] if bases else None, key, facets, fits, rejected, tier)
        # A `same_model` row is the very model it declares: it takes that model's facets as they stand, for the
        # categories it carries. Its own measurement, when it has one, stands.
        for m in declared:
            entry = self.same[m]
            carries = entry.get('carries')
            for key, rec in facets[entry['model']].items():
                if key in facets[m] or (carries is not None and key[0] not in carries):
                    continue
                facets[m][key] = {'value': rec['value'], 'by_effort': dict(rec['by_effort']), 'sources': {}, 'status': 'estimated',
                                  'estimate': {'method': 'same_model', 'base_model': entry['model'], 'base_status': rec['status'],
                                               **{k: entry[k] for k in ('kind', 'statement', 'url', 'as_of') if entry.get(k)}}}
        for m, r in rows.items():
            carries = (self.same.get(m) or {}).get('carries')
            r['axes'] = {}
            for a in AXES:
                own = {f: facets[m][(a, f)] for f in AXES[a] if (a, f) in facets[m]}
                if carries is not None and a not in carries and m in declared:
                    own = {f: rec for f, rec in own.items() if rec.get('status', '').startswith('measured')}
                r['axes'][a] = self.category(a, own)
        self.apply_overrides(rows)
        fit_doc = {f'{a}.{c}~tier': {k: round(v, 4) for k, v in f.items()} for (a, c), f in fits.items() if f}
        return rows, tier, fit_doc, rejected, links, self.targets(rows, panel)

    def targets(self, rows, panel):
        """{category: {'levels': {level: ...}, 'continuity'}} from the panel's values at High (module docstring): level 1
        the panel's weakest, level 2 its median, level 3 the anchor; the fit's zero is the panel weakest. divergence, coding
        and aesthetics read the category value. tool_operation reads its facets one by one (`by_facet`): each level asks
        for the facets TOOL_LEVEL_FACETS selects, its level 1 for nothing, and a level never asks less of a facet than the
        level below it (user, 2026-11-11: the anchor's responsiveness at High, 0, is below the panel median, so level 3
        asks max(level 2, anchor) of it). `continuity` is t3 - t2 (tool_operation: the smallest over the facets both
        levels ask for, none when they share none)."""
        anchor = rows.get(measurements.ANCHOR, {}).get('axes') or {}

        def stats(pick):
            vals = [v for m in panel if (v := pick(rows[m]['axes'])) is not None]
            if not vals:
                return None
            return {'min': round(float(min(vals)), 4), 'median': round(float(np.median(vals)), 4),
                    'anchor': None if pick(anchor) is None else round(float(pick(anchor)), 4), 'n': len(vals)}
        out = {}
        for a in AXES:
            facet_targets = {}
            for f in AXES[a]:
                fst = stats(lambda ax, a=a, f=f: ((ax[a].get('facets') or {}).get(f) or {}).get('value'))
                if fst is not None:
                    t1_f, t2_f = fst['min'], max(fst['min'], fst['median'])
                    raw_t3 = fst['anchor'] if fst['anchor'] is not None else t2_f
                    t3_f = max(t2_f, raw_t3)
                    facet_targets[f] = {'levels': {'1': {'target': t1_f, 'zero': t1_f, 'statistic': 'panel minimum', 'n': fst['n']},
                                                   '2': {'target': t2_f, 'zero': t1_f, 'statistic': 'panel median', 'n': fst['n']},
                                                   '3': {'target': t3_f, 'zero': t1_f, 'statistic': f'anchor {measurements.ANCHOR}', 'n': fst['n'],
                                                         **({'raised_to_level_below': t2_f} if t2_f > raw_t3 else {})}}}
            if a != 'tool_operation':
                st = stats(lambda ax, a=a: ax[a].get('value'))
                levels = {str(level): ({'target': st[key], 'zero': st['min'], 'statistic': name, 'n': st['n']} if st else
                                       {'target': None, 'zero': None, 'reason': 'no panel model has a value'})
                          for level, key, name in ((1, 'min', 'panel minimum'), (2, 'median', 'panel median'),
                                                   (3, 'anchor', f'anchor {measurements.ANCHOR}'))}
                gap = (levels['3']['target'] - levels['2']['target']) if (
                    st and levels['3']['target'] is not None and levels['2']['target'] is not None
                ) else None
                out[a] = {'levels': levels, 'facets': facet_targets, 'continuity': None if gap is None else round(gap, 4)}
                continue
            per = {f: stats(lambda ax, f=f: ((ax['tool_operation'].get('facets') or {}).get(f) or {}).get('value'))
                   for f in AXES[a]}
            levels, prev = {}, {}
            for level in (1, 2, 3):
                chosen = TOOL_LEVEL_FACETS[level]
                if not chosen:
                    levels[str(level)] = {'by_facet': {}, 'reason': 'no ability requirement'}
                    continue
                by = {}
                for f in chosen:
                    st = per[f]
                    if st is None:
                        continue
                    own = st['median'] if level == 2 else st['anchor']
                    target = max(own, prev[f]) if f in prev and own is not None else own
                    by[f] = {'target': target, 'zero': st['min'], 'n': st['n'],
                             'statistic': 'panel median' if level == 2 else f'anchor {measurements.ANCHOR}',
                             **({'raised_to_level_below': prev[f]} if f in prev and own is not None and prev[f] > own else {})}
                levels[str(level)] = {'by_facet': by}
                prev = {f: r['target'] for f, r in by.items()}
            shared = [f for f in levels['2']['by_facet'] if f in levels['3']['by_facet']]
            gap = min(levels['3']['by_facet'][f]['target'] - levels['2']['by_facet'][f]['target'] for f in shared) if shared else None
            out[a] = {'levels': levels, 'facets': facet_targets, 'continuity': None if gap is None else round(gap, 4)}
        return out

    def apply_overrides(self, rows):
        for o in self.overrides:
            if o.get('model') in rows and o.get('axis') in AXES and o.get('as_of') and isinstance(o.get('value'), (int, float)):
                ax = rows[o['model']]['axes'][o['axis']]
                ax.update(public_value=ax.get('value'), value=float(o['value']),
                          by_effort=dict.fromkeys(EFFORTS, float(o['value'])), status='user_override',
                          override={'source': 'user', 'as_of': o['as_of'], 'note': o.get('note')})
                ax.pop('flag', None)


PAIR_EFFORTS = ('high', 'medium', 'xhigh', 'low', 'max')    # the effort a model's arena/AA pair is read at: nearest High first
AA_TIME, ARENA_TIME = 'aa.cost_time', 'arena.agent_sessions'
MIN_FLEET_RATIO_N = 3       # models that must list both efforts before their time ratio is cited for a model that lists neither


def time_rows(entries, source):
    """{effort: row} of `source`'s Time/Task listings (minutes > 0). An effort-less listing is the model's single
    configuration and is keyed High, the basis of the anchor's own listing, unless High is also listed; efforts outside
    EFFORTS (a non-reasoning variant) are not routable efforts and are left out."""
    rows = {}
    for e in entries or ():
        t = e.get('time_per_task_min')
        if e.get('source') == source and isinstance(t, (int, float)) and not isinstance(t, bool) and t > 0:
            rows[e.get('effort') or 'default'] = e
    if 'default' in rows:
        rows.setdefault('high', rows.pop('default'))
    return {k: v for k, v in rows.items() if k in EFFORTS}


def anchor_minutes(anchor_rows):
    """The anchor model's Artificial Analysis Time/Task at High in minutes. Without that row every duration would be on a
    scale nothing ties to a cited value, so the build stops."""
    row = anchor_rows.get('high')
    if not row:
        raise ValueError(f'the anchor {measurements.ANCHOR} has no Artificial Analysis Time/Task at High in the public '
                         'measurements: durations cannot be put on one cited scale')
    return row['time_per_task_min']


def nearest_effort(known, effort):
    """The listed effort of `known` closest to `effort` on the effort ladder (the lower one on a tie)."""
    at = EFFORTS.index(effort)
    return min(known, key=lambda e: (abs(EFFORTS.index(e) - at), EFFORTS.index(e)))


def durations(scorer, rows, acc, index, board, tier):
    """({model: duration record}, fit or None, why the fit was rejected or None): one basis for the time a task takes,
    Artificial Analysis Time/Task at the same effort, as a multiple of the anchor's at High (module docstring)."""
    anchor = anchor_minutes(time_rows(scorer.trusted((rows.get(measurements.ANCHOR) or {}).get('fields', {}).get('throughput')),
                                      AA_TIME))
    aa = {m: time_rows(scorer.trusted(r['fields'].get('throughput')), AA_TIME) for m, r in rows.items()}
    arena = {m: time_rows(scorer.trusted(r['fields'].get('throughput')), ARENA_TIME) for m, r in rows.items()}
    pairs = []
    for m in rows:
        shared = next((e for e in PAIR_EFFORTS if e in aa[m] and e in arena[m]), None)
        if shared:
            pairs.append((arena[m][shared]['time_per_task_min'], aa[m][shared]['time_per_task_min']))
    fit, why = Scorer.fit(pairs, require_skill=True)
    fit_rec = Scorer.fit_record('aa_time~arena_time', fit) if fit else None
    minutes, basis = {}, {}
    for m in rows:
        minutes[m], basis[m] = {}, {}
        if aa[m]:
            for e, row in aa[m].items():
                minutes[m][e] = row['time_per_task_min']
                basis[m][e] = {'method': 'aa', 'source': AA_TIME, 'listed_as': row.get('listed_as'),
                               'as_of': row.get('as_of'), 'url': row.get('url')}
        elif arena[m] and fit:
            for e, row in arena[m].items():
                x = row['time_per_task_min']
                minutes[m][e] = round(Scorer.predict(fit, x), 3)
                basis[m][e] = {'method': 'arena_fit', 'source': ARENA_TIME, 'arena_minutes': x, **fit_rec,
                               'listed_as': row.get('listed_as'), 'as_of': row.get('as_of'), 'url': row.get('url')}
    same = series.same_model(acc)
    for m, entry in same.items():
        base = entry.get('model')
        if m in rows and 'duration' in (entry.get('carries') or ()) and base in rows:
            for e, t in list(minutes[base].items()):
                if e not in minutes[m]:
                    minutes[m][e] = t
                    basis[m][e] = {'method': 'same_model', 'base_model': base, 'url': entry.get('url'), 'as_of': entry.get('as_of'),
                                   'statement': entry.get('statement')}
    sibs = series.groups(acc)
    tiers = {m: t['value'] for m, t in tier.items()}
    measured = {m for m in rows if minutes[m]}
    for m in rows:
        if minutes[m] or (m in same and 'duration' not in (same[m].get('carries') or ())):
            continue        # a row declared to be another model's channel that does not carry its time keeps the channel's own
        for b in series.nearest(m, [b for b in sibs.get(m, ()) if b in measured], index, board, tiers, same):
            for e, t in minutes[b].items():
                minutes[m][e] = t
                basis[m][e] = {'method': 'series', 'base_model': b, 'source': basis[b][e].get('source'),
                               'base_method': basis[b][e]['method'], 'series': sorted([m, *sibs.get(m, ())])}
            break
    fleet = {}

    def fleet_ratio(e, e0):
        """(median, n, q1, q3) of AA time[e] / time[e0] over the models that list both, or None under 3 models."""
        if (e, e0) not in fleet:
            xs = [t[e]['time_per_task_min'] / t[e0]['time_per_task_min'] for t in aa.values() if e in t and e0 in t]
            fleet[(e, e0)] = None if len(xs) < MIN_FLEET_RATIO_N else (
                float(np.median(xs)), len(xs), float(np.percentile(xs, 25)), float(np.percentile(xs, 75)))
        return fleet[(e, e0)]

    def fleet_position(e, lo, up):
        """(median p, n, q1, q3) of the position of effort `e` between `lo` and `up` on the log-time scale,
        p = log(t_e / t_lo) / log(t_up / t_lo), over the models whose Artificial Analysis listing has all three efforts
        (and a lo that differs from up), or None under 3 models."""
        if (e, lo, up) not in fleet:
            ps = []
            for t in aa.values():
                if e in t and lo in t and up in t:
                    span = float(np.log(t[up]['time_per_task_min'] / t[lo]['time_per_task_min']))
                    if span != 0.0:
                        ps.append(float(np.log(t[e]['time_per_task_min'] / t[lo]['time_per_task_min'])) / span)
            fleet[(e, lo, up)] = None if len(ps) < MIN_FLEET_RATIO_N else (
                float(np.median(ps)), len(ps), float(np.percentile(ps, 25)), float(np.percentile(ps, 75)))
        return fleet[(e, lo, up)]
    out = {}
    for m, r in rows.items():
        direct = dict(minutes[m])
        if direct:
            bases = series.nearest(m, sibs.get(m, ()), index, board, tiers, same)
            known = dict(direct)

            def side(effort, pool):
                at = EFFORTS.index(effort)
                return ([k for k in pool if EFFORTS.index(k) < at], [k for k in pool if EFFORTS.index(k) > at])

            def hold(t, rec, effort, pool):
                """An effort outside the range of `pool` is never faster than a lower effort of it (nor slower than a higher
                one): held at that time, and marked, when the estimate would be."""
                below, above = side(effort, pool)
                if below:
                    bound, label, hit = max(pool[k] for k in below), 'a lower listed effort', None
                    hit = t < bound
                else:
                    bound, label = min(pool[k] for k in above), 'a higher listed effort'
                    hit = t > bound
                if hit:
                    t = bound
                    rec['bounded_by'] = label
                    rec['note'] = (rec['note'] + '; ' if rec.get('note') else '') + f'held at the time of {label}'
                return round(t, 3), rec
            # 1. outside the listed range, the nearest same-series sibling's ratio from the nearest listed effort
            for e in EFFORTS:
                below, above = side(e, direct)
                if e in direct or (below and above):
                    continue
                e0 = nearest_effort(direct, e)
                for b in bases:
                    if e in aa[b] and e0 in aa[b]:
                        ratio = aa[b][e]['time_per_task_min'] / aa[b][e0]['time_per_task_min']
                        known[e], basis[m][e] = hold(direct[e0] * ratio, {
                            'method': 'series_effort', 'base_model': b, 'from_effort': e0, 'ratio': round(ratio, 4),
                            'source': AA_TIME}, e, direct)
                        break
            # 2. the rest, with the model's own listed and sibling-derived efforts as the endpoints (an effort estimated in
            # this step is never an endpoint of another)
            anchors = dict(known)
            for e in EFFORTS:
                if e in anchors:
                    continue
                below, above = side(e, anchors)
                if below and above:
                    # between two efforts of its own: both endpoints are honoured, and the fleet says where on the log-time
                    # scale between them the missing effort sits
                    lo, up = max(below, key=EFFORTS.index), min(above, key=EFFORTS.index)
                    got = fleet_position(e, lo, up)
                    if got:
                        med, n, q1, q3 = got
                        p = min(max(med, 0.0), 1.0)
                        known[e] = round(anchors[lo] * (anchors[up] / anchors[lo]) ** p, 3)
                        basis[m][e] = {'method': 'fleet_effort_position', 'lower_effort': lo, 'upper_effort': up,
                                       'position': round(med, 4), 'n': n, 'iqr': [round(q1, 4), round(q3, 4)], 'source': AA_TIME,
                                       'note': f'fleet effort position {e} in {lo}..{up}, n={n}, IQR {q1:.2f}-{q3:.2f}'}
                        if p != med:
                            basis[m][e]['bounded_by'] = 'position held within [0, 1]'
                            basis[m][e]['note'] += f'; the median position {med:.2f} is held at {p:.0f}'
                    continue
                e0 = nearest_effort(anchors, e)
                got = fleet_ratio(e, e0)
                if got:
                    med, n, q1, q3 = got
                    known[e], basis[m][e] = hold(anchors[e0] * med, {
                        'method': 'fleet_effort_ratio', 'from_effort': e0, 'ratio': round(med, 4), 'n': n,
                        'iqr': [round(q1, 4), round(q3, 4)], 'source': AA_TIME,
                        'note': f'fleet effort ratio {e}/{e0}, n={n}, IQR {q1:.2f}-{q3:.2f}'}, e, anchors)
            minutes[m].update({e: t for e, t in known.items() if e not in direct})
        out[m] = {'status': 'ok' if minutes[m] else 'missing', 'anchor_minutes': anchor,
                  'by_effort': {e: round(t / anchor, 4) for e, t in minutes[m].items()},
                  'minutes_by_effort': dict(minutes[m]), 'basis_by_effort': basis[m],
                  'efforts_without_time': [e for e in EFFORTS if e not in minutes[m]]}
    return out, fit_rec, why


STALE_DAYS = 21         # a community value older than this (data date, else fetch date) is a yellow note


def age_days(as_of, now):
    try:
        return (now.date() - datetime.fromisoformat(str(as_of)[:10]).date()).days
    except ValueError:
        return None


def cite(o, now, **extra):
    age = age_days(o.get('as_of'), now)
    return ({k: o.get(k) for k in ('source', 'kind', 'value', 'url', 'as_of', 'fetched_at', 'method')} |
            {'age_days': age, 'stale': age is None or age > STALE_DAYS} | extra)


def window_capacity(fam, plan, wid, ctx, rung_max=3):
    """One window of one plan down the capacity ladder (round 5): rung 1 the community observations (else a window
    ratio a community source states, rung 1 derived), rung 2 time-to-exhaust reports x the reported model's usd, rung 3
    a vendor plan multiplier x the sibling plan's value. The first rung with a value wins; rung 3 is also recorded as a
    cross-check where a lower rung won."""
    now, usd = ctx['now'], ctx['usd']
    entries, rung = [cite(o, now, rung=1) for o in ctx['obs'].get((fam, plan, wid), ())], 1
    rule = ctx['ratios'].get((fam, wid))
    if not entries and rule:
        base = window_capacity(fam, plan, rule['of_window'], {**ctx, 'ratios': {}}, rung_max)
        if base['capacity_usd'] is not None:
            age = max(age_days(rule.get('as_of'), now) or 0, base['age_days'])
            entries, rung = [{'source': rule['source'], 'kind': 'derived', 'value': round(rule['ratio'] * base['capacity_usd'], 2),
                              'url': rule['url'], 'as_of': rule['as_of'], 'fetched_at': rule.get('fetched_at'),
                              'method': f"{rule['ratio']:g} x the {rule['of_window']} ({rule['statement']}; {rule['original']})",
                              'derived_from': base['sources'], 'age_days': age, 'stale': age > STALE_DAYS,
                              'rung': base['rung']}], base['rung']
    if not entries and rung_max >= 2:
        for r in ctx['exhaust'].get((fam, plan, wid), ()):
            rate = (usd.get(r['model']) or {}).get('value')
            if rate:
                entries.append(cite(r, now, rung=2, kind='derived', value=round(r['hours'] * rate, 2),
                                    method=f"{r['hours']:g} h of {r['model']} until the window ran out x its usd {rate:g}/h "
                                           "(usd_per_hour)", report=r.get('quote')))
        rung = 2
    mult = ctx['mults'].get((fam, plan))
    sibling = None
    if mult and rung_max >= 3:
        base = window_capacity(fam, mult['of_plan'], wid, {**ctx, 'mults': {}}, 2)
        if base['capacity_usd'] is not None:
            age = max(age_days(mult.get('as_of'), now) or 0, base['age_days'])
            sibling = {'source': mult['source'], 'kind': 'derived', 'value': round(mult['factor'] * base['capacity_usd'], 2),
                       'url': mult['url'], 'as_of': mult['as_of'], 'fetched_at': mult.get('fetched_at'),
                       'method': f"{mult['factor']:g} x {fam}/{mult['of_plan']} {wid} ({mult['statement']})",
                       'derived_from': base['sources'], 'age_days': age, 'stale': age > STALE_DAYS, 'rung': 3}
    if not entries and sibling:
        entries, rung, sibling = [sibling], 3, None
    ref = (REFERENCE_MODELS.get(fam) or {}).get(wid)
    ref_usd = ((usd.get(ref) or {}).get('value')) if ref else None
    if not entries:
        return {'unit': 'usd', 'capacity_usd': None, 'status': 'error', 'rung': None, 'sources': [], 'reference_model': ref,
                'ref_hours': None, 'age_days': None}
    value = round(statistics.median(e['value'] for e in entries), 2)
    newest = min(e['age_days'] if e['age_days'] is not None else 10 ** 6 for e in entries)
    row = {'unit': 'usd', 'capacity_usd': value, 'status': 'stale' if newest > STALE_DAYS else 'ok', 'rung': rung,
           'age_days': newest, 'merge': f'median of {len(entries)} value(s), rung {rung}', 'sources': entries,
           'reference_model': ref, 'ref_hours': round(value / ref_usd, 1) if ref_usd else None}
    if sibling:
        row['cross_check'] = {'rung': 3, 'value': sibling['value'], 'method': sibling['method'],
                              'ratio_to_value': round(sibling['value'] / value, 2) if value else None}
    return row


def resolve_plan(table, plan):
    """(plan whose windows apply, alias or None) for a plan-templates entry that may be `{"alias": ...}`."""
    target, alias = plan, None
    for _ in range(3):
        nxt = (table.get(target) or {}).get('alias')
        if not isinstance(nxt, str):
            break
        target = alias = nxt
    return target, alias


def capacity(meas, plans, usd, now=None):
    """({family: {plan: {window: capacity row}}}, data errors) down the capacity ladder from `meas`."""
    now = now or datetime.now(timezone.utc)
    cap = meas.get('capacity') or {}
    obs = {}
    for o in cap.get('observations') or ():
        obs.setdefault((o['family'], o['plan'], o['window']), []).append(o)
    rules = cap.get('rules') or ()
    ctx = {'now': now, 'usd': usd, 'obs': obs,
           'ratios': {(r['family'], r['window']): r for r in rules if r.get('type', 'window_ratio') == 'window_ratio'},
           'mults': {(r['family'], r['plan']): r for r in rules if r.get('type') == 'plan_multiplier'},
           'exhaust': {}}
    for r in cap.get('exhaust') or ():
        ctx['exhaust'].setdefault((r['family'], r['plan'], r['window']), []).append(r)
    errors = [{'field': 'capacity', **e} for e in cap.get('errors') or ()]
    out, stale = {}, {}
    for fam, table in plans.items():
        if not isinstance(table, dict):
            continue
        for plan, spec in table.items():
            if not isinstance(spec, dict):
                continue
            target, alias = resolve_plan(table, plan)
            for wid in (table.get(target) or {}).get('windows') or {}:
                row = window_capacity(fam, target, wid, ctx)
                if alias:
                    row['alias_of'] = alias
                out.setdefault(fam, {}).setdefault(plan, {})[wid] = row
                if alias:
                    continue
                if row['status'] == 'error':
                    errors.append({'field': 'capacity', 'family': fam, 'plan': plan, 'window': wid, 'level': 'red',
                                   'reason': 'capacity_ladder_empty',
                                   'effect': 'no community measurement, time-to-exhaust report or vendor multiplier: a data '
                                             'error surfaced in routing notes; the live percentage still prices scarcity'})
                for e in row['sources']:
                    if e['stale'] and e['kind'] in ('community', 'vendor'):
                        stale.setdefault(e['source'], {'age_days': e['age_days'], 'as_of': e['as_of'], 'windows': []})
                        stale[e['source']]['windows'].append(f'{fam}/{plan}/{wid}')
    for src, row in sorted(stale.items()):
        errors.append({'field': 'capacity', 'source': src, 'level': 'yellow', 'reason': 'stale_source',
                       'age_days': row['age_days'], 'as_of': row['as_of'], 'threshold_days': STALE_DAYS,
                       'windows': row['windows'], 'effect': 'still used in the median; refresh the source'})
    return out, errors


def source_ages(meas, now=None):
    """{source: {kind, as_of, fetched_at, age_days, stale}} for every source the measurements cite."""
    now = now or datetime.now(timezone.utc)
    out = {}
    for src, row in sorted((meas.get('sources') or {}).items()):
        age = age_days(row.get('as_of'), now)
        out[src] = {'kind': row.get('kind'), 'as_of': row.get('as_of'), 'as_of_kind': row.get('as_of_kind'),
                    'fetched_at': row.get('fetched_at'), 'age_days': age, 'stale': age is None or age > STALE_DAYS}
    for src, row in sorted((meas.get('missing_sources') or {}).items()):
        out[src] = {'kind': None, 'missing': True, 'reason': row.get('reason'), 'url': row.get('url')}
    return out


def source_errors(ages):
    """Yellow `stale_source` errors for every capability source (measurements.CAPABILITY_SOURCES) older than STALE_DAYS,
    the same shape as the capacity ones: a stale benchmark still feeds the category scores, so it is surfaced, not only
    a stale capacity reading. A source not anchored is already a missing source (`source_ages`)."""
    return [{'field': 'capability', 'source': src, 'level': 'yellow', 'reason': 'stale_source',
             'component': '.'.join(measurements.CAPABILITY_SOURCES[src][:2]), 'age_days': row['age_days'],
             'as_of': row['as_of'], 'threshold_days': STALE_DAYS, 'effect': 'still used; refresh the source'}
            for src, row in sorted(ages.items())
            if src in measurements.CAPABILITY_SOURCES and not row.get('missing') and row.get('stale')]


# --- usd per worker-hour ------------------------------------------------------------------------------------------


def arena_usd_miss_reason(scorer, model, raw_entries, meas):
    """True reason `arena.agent_sessions` gave no value for `model` (round 9 F3: never say 'not listed' when the source
    was distrusted, not captured, or listed the model without Cost/Task or Time/Task)."""
    if any(e.get('source') == 'arena.agent_sessions' for e in raw_entries or ()) and 'arena.agent_sessions' in scorer.distrust:
        return 'arena.agent_sessions distrusted'
    missing = ((meas or {}).get('missing_sources') or {}).get('arena.agent_sessions')
    if missing:
        return f"arena.agent_sessions unavailable ({missing.get('reason') or 'not captured'})"
    unpriced = ((((meas or {}).get('sources') or {}).get('arena.agent_sessions') or {}).get('unpriced') or {}).get(model)
    if unpriced:
        return unpriced['reason']
    return 'not listed on arena.ai Agent / Overall'


USD_ORDER = ('arena.agent_sessions', 'derived.community_mix')


def usd_community_mix(scorer, rows, meas):
    """The one usd_per_hour basis (rounds 8-9, user 2026-11-09): `arena.agent_sessions` (arena.ai Agent / Overall real
    sessions, Cost/Task P50 / Time/Task P50 x 60, kind `community`), else the labelled option B (`derived.community_mix`,
    AA throughput x community token mix x list price; `fallback: True`) for a model arena does not list with cost and
    time; `convert_usd` then replaces that for a model with a same-series sibling that has a direct value. No median is
    taken across sources on different scales."""
    out = {}
    for m, r in rows.items():
        raw = r['fields'].get('usd_per_hour') or ()
        by_src = {}
        for e in scorer.trusted(raw):
            by_src.setdefault(e['source'], []).append(e)
        chosen = next((s for s in USD_ORDER if s in by_src), None)
        if not chosen:
            out[m] = {'value': None, 'status': 'missing', 'sources': {}, 'merge': None,
                      'reason': arena_usd_miss_reason(scorer, m, raw, meas)}
            continue
        picked = pick_usd(by_src[chosen])
        is_fb = chosen != 'arena.agent_sessions'
        kind = by_src[chosen][0].get('kind') or ('derived' if is_fb else 'community')
        merge = f"fallback ({arena_usd_miss_reason(scorer, m, raw, meas)}): {picked['merge']}" if is_fb else picked['merge']
        out[m] = {**picked, 'merge': merge, 'kind': kind, 'fallback': is_fb}
    return out


def axis_evidence(axes):
    """Round 058 review F5: per axis, the measured sources that are not Artificial Analysis (the AA boards are one
    source); `aa_only` when no axis has any, though some axis has a value."""
    indep = {a: sorted({s for c in (ax.get('facets') or {}).values() if str(c.get('status', '')).startswith('measured')
                        for s in (c.get('sources') or {}) if not s.startswith('aa.')}) for a, ax in axes.items()}
    valued = any(ax.get('status') in ('measured', 'partial', 'estimated') for ax in axes.values())
    return {'independent_sources': indep, 'aa_only': valued and not any(indep.values())}


def token_mix(meas):
    """The community token mix shares (public-measurements.json `capacity.token_mix`), every numeric component."""
    mix = (meas.get('capacity') or {}).get('token_mix') or {}
    shares = {k: v for k, v in mix.items() if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0}
    return shares if sum(shares.values()) > 0 else {}


def list_price_ratio(price_t, price_b, mix):
    """The ratio of two cited API list prices (pick_price) over the token mix, or None when either lacks a component."""
    if not mix or price_t is None or price_b is None:
        return None
    if any(not isinstance(p['value'].get(c), (int, float)) for p in (price_t, price_b) for c in mix):
        return None
    cost_t, cost_b = (sum(p['value'][c] * share for c, share in mix.items()) for p in (price_t, price_b))
    return cost_t / cost_b if cost_b > 0 else None


def convert_usd(usd, rows, scorer, meas, acc, tier):
    """Series conversion of usd_per_hour: a model without a direct value (`derived` or missing) and with a same-series
    sibling that has one takes the nearest such sibling's value times the ratio of the two cited API list prices over
    the full community token mix."""
    direct = {m for m, u in usd.items() if u.get('value') is not None and u.get('kind') not in ('derived', 'converted')}
    sibs = series.groups(acc)
    index = {m: ((scorer.trusted(r['fields'].get('intelligence_index')) or [{}])[0]).get('value') for m, r in rows.items()}
    tier = {m: t['value'] for m, t in tier.items()}
    mix = token_mix(meas)
    for m in rows:
        if m in direct:
            continue
        for b in series.nearest(m, [b for b in sibs.get(m, ()) if b in direct], index, series.boards(meas), tier,
                                series.same_model(acc)):
            prices = {k: pick_price(scorer.trusted(rows[k]['fields'].get('price'))) for k in (m, b)}
            ratio = list_price_ratio(prices[m], prices[b], mix)
            if ratio is None:
                continue
            usd[m] = {'value': round(usd[b]['value'] * ratio, 3), 'status': 'ok', 'kind': 'converted', 'fallback': False,
                      'merge': f'series conversion: usd of {b} x API list price ratio {ratio:.4f} over the community token mix',
                      'used': [],
                      'sources': {'base': {**usd[b], 'model': b}, 'ratio': round(ratio, 4), 'prices': prices, 'token_mix': mix}}
            break


def build(meas, acc, plans, distrust=(), meas_bytes=b'', overrides=(), now=None):
    scorer = Scorer(meas, acc, distrust, overrides)
    rows, tier, fits, rejected, links, targets = scorer.score()
    usd = usd_community_mix(scorer, rows, meas)
    convert_usd(usd, rows, scorer, meas, acc, tier)
    caps, cap_errors = capacity(meas, plans, usd, now)
    ages = source_ages(meas, now)
    index = {m: ((scorer.trusted(r['fields'].get('intelligence_index')) or [{}])[0]).get('value') for m, r in rows.items()}
    dur, dur_fit, dur_rejected = durations(scorer, rows, acc, index, series.boards(meas), tier)
    models, errors = {}, []
    for m, r in rows.items():
        model_errors = []
        if usd[m]['status'] == 'missing':
            model_errors.append({'model': m, 'field': 'usd_per_hour', 'reason': 'no_cited_source', 'level': 'red_if_metered',
                                 'effect': 'out of automatic routing on any quota-pool or metered account; a named '
                                           'dispatch runs with its cost unpriced'})
        if dur[m]['status'] == 'missing':
            model_errors.append({'model': m, 'field': 'duration_multiplier', 'reason': 'no_time_per_task', 'level': 'red',
                                 'effect': 'no Artificial Analysis Time/Task, and no arena.ai Agent Time/Task with a fit that has '
                                           'skill: out of automatic routing at every effort; a named dispatch carries the '
                                           'task\'s own hours estimate unscaled'})
        elif dur[m]['efforts_without_time']:
            model_errors.append({'model': m, 'field': 'duration_multiplier', 'reason': 'effort_without_time', 'level': 'yellow',
                                 'effect': 'out of automatic routing at ' + ', '.join(dur[m]['efforts_without_time'])
                                           + ' (no Time/Task at that effort and no same-series sibling to convert it); '
                                             'the listed efforts still route'})
        img = pick_image_input(scorer.trusted(r['fields'].get('image_input')))
        if img['status'] == 'unknown':
            model_errors.append({'model': m, 'field': 'image_input', 'reason': 'no_cited_source',
                                 'level': 'red_if_vision_demanded',
                                 'effect': 'unknown image-input modality; excluded from automatic routing when '
                                           'tool_operation is 3 (mode B)'})
        fm = pick_fast_mode(m, scorer.trusted(r['fields'].get('fast_mode')), scorer.models)
        if fm['status'] == 'missing':
            model_errors.append({'model': m, 'field': 'fast_mode', 'reason': 'no_cited_source', 'level': 'yellow',
                                 'effect': 'fast mode is not priced or enabled from a policy multiplier on the five-axis path'})
        for a, ax in r['axes'].items():
            if ax.get('flag') == 'missing':
                model_errors.append({'model': m, 'field': a, 'reason': 'no_trusted_source', 'level': 'red_if_demanded',
                                     'effect': 'no value: a task that demands this category leaves automatic routing for the '
                                               'model (mode B)'})
            elif ax.get('missing_facets'):
                model_errors.append({'model': m, 'field': f"{a}.{','.join(ax['missing_facets'])}",
                                     'reason': 'facet_unmeasured_no_estimate', 'level': 'red_if_demanded',
                                     'effect': 'missing facet with no estimate: excluded under mode B at levels requiring this facet'})
        evidence = axis_evidence(r['axes'])
        if evidence['aa_only']:
            model_errors.append({'model': m, 'field': 'axes', 'reason': 'aa_only', 'level': 'yellow',
                                 'effect': 'every axis value comes from Artificial Analysis (the Intelligence Index and AA '
                                           'boards collinear with it): one source, not independent evidence'})
        if tier[m]['method'] == 'none':
            model_errors.append({'model': m, 'field': 'tier', 'reason': 'no_overall_board', 'level': 'yellow',
                                 'effect': 'no arena.ai Overall reading, no other overall board and no same-series sibling: '
                                           'its type scores that no board measures stay missing (mode B when demanded)'})
        errors += model_errors
        idx = scorer.trusted(r['fields'].get('intelligence_index'))
        models[m] = {'axes': r['axes'], 'scores': {a: ax.get('value') for a, ax in r['axes'].items()},
                     'missing_axes': [a for a, ax in r['axes'].items() if ax.get('flag') == 'missing'],
                     'usd_per_hour': usd[m], 'duration_multiplier': dur[m],
                     'image_input': img, 'fast_mode': fm,
                     'price': pick_price(scorer.trusted(r['fields'].get('price'))),
                     'intelligence_index': idx[0]['value'] if idx else None,
                     'tier': tier[m], 'evidence': evidence, 'data_errors': model_errors}
    doc = {'schema': SCHEMA, 'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
           'measurements_sha256': hashlib.sha256(meas_bytes).hexdigest() if meas_bytes else None,
           'distrusted_sources': sorted(distrust),
           'user_overrides': list(overrides),
           'method': __doc__.strip().splitlines()[0],
           'estimate_fits': fits, 'rejected_fits': rejected,
           'duration_fit': dur_fit, 'duration_fit_rejected': dur_rejected,
           'tier_sources': {k: sum(1 for t in tier.values() if t['method'] == k)
                            for k in ('anchor', 'arena_overall', 'other_overall', 'sibling', 'none')},
           'capacity': caps, 'capacity_stale_days': STALE_DAYS,
           'capacity_not_found': (meas.get('capacity') or {}).get('not_found') or {},
           'capacity_errors': cap_errors, 'source_ages': ages, 'source_errors': source_errors(ages),
           'usd_checks': [{**h, 'usd_per_hour': (usd.get(h['model']) or {}).get('value'),
                           'community_over_usd': (round(h['value'] / usd[h['model']]['value'], 2)
                                                    if (usd.get(h['model']) or {}).get('value') else None)}
                          for h in (meas.get('capacity') or {}).get('hourly_checks') or ()],
           'anchor': {'model': measurements.ANCHOR, 'value': 0.0, 'scale': 'panel standard score',
                      'panel': (meas.get('panel') or {}).get('models') or [], 'panel_n': (meas.get('panel') or {}).get('n'),
                      'targets': targets},
           'links': links, 'effort_correlations': scorer.effort_correlations, 'checks': meas.get('checks') or [],
           'models': models, 'data_errors': errors}
    return doc


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--measurements', default=str(measurements.OUT))
    ap.add_argument('--distrust', action='append', default=[], help='a source id to leave out of every field (repeatable)')
    ap.add_argument('--user-overrides', help='JSON list of dated {model, axis, value, as_of, note} overrides (source `user`)')
    ap.add_argument('--out', default=str(OUT))
    args = ap.parse_args()
    raw = Path(args.measurements).read_bytes()
    overrides = json.loads(Path(args.user_overrides).read_text(encoding='utf-8')) if args.user_overrides else []
    doc = build(json.loads(raw), sources.accounts(), json.loads(PLANS.read_text(encoding='utf-8')), args.distrust, raw,
                overrides=overrides)
    Path(args.out).write_text(json.dumps(doc, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    print(f'{len(doc["models"])} models -> {args.out}')
    print(f'{"model":28s} {"usd/h":>7}  ' + ' '.join(f'{a[:10]:>10}' for a in AXES))
    for m, r in doc['models'].items():
        u = r['usd_per_hour']['value']
        mark = {'estimated': '~', 'user_override': 'u'}
        cells = ' '.join((f"{r['scores'][a]:>9.2f}" if r['scores'][a] is not None else f"{'-':>9}")
                         + mark.get(r['axes'][a]['status'], '!' if r['axes'][a].get('flag') else ' ') for a in AXES)
        print(f"{m:28s} {u if u is not None else '-':>7}  {cells}")
    print('~ estimated (sibling or tier fit)  u user override  ! missing')
    print('tier sources:', doc['tier_sources'])
    for fam, plans in doc['capacity'].items():
        for plan, wins in plans.items():
            print(f'capacity {fam}/{plan}: ' + ', '.join(
                f"{w}={r['capacity_usd'] if r['capacity_usd'] is not None else '-'}({r['status']}, rung {r['rung']})"
                for w, r in wins.items()))


if __name__ == '__main__':
    sys.exit(main())
