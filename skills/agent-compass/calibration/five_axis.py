"""Five-axis scores, usd_per_hour, duration and plan capacity from `references/public-measurements.json`.

`python -X utf8 five_axis.py [--distrust SOURCE ...] [--source-priority A,B,...] [--user-overrides PATH] [--out PATH]`

Writes `references/five-axis-scores.json`, which the router reads only when `scoring.five_axis.enabled` is set (off by
default; see allocator.five_axis_overlay). Round 047, round 2 (user decisions of 2026-11-09):

- No scalar tier. The four capability axes (divergence, global_rigor, module_precision, vision_tools) are the only
  model ability (round 7: the only dimension system; no difficulty floors, the router fits the scores to the task's
  demand continuously against targets of the anchor value x demand / 3, written here as `anchor`).
- Each axis is the mean of its components, each component the mean of its trusted sources (`measurements.py` converts
  every source the same way, relative to the anchor model: 0.8 x s / s_anchor, so a new top model moves nothing). A
  counted component a model does not measure comes from its nearest same-series sibling that does (series conversion,
  calibration/series.py; round 058, user 2026-11-10): the sibling's value moved along the component's AA-index fit by
  the two index readings, kept inside the fit's measured range, else the sibling's value. Only for a model with no
  such sibling is the component backfilled from public data, a linear fit of the component on the AA
  Intelligence Index over the models that have it; a fit needs MIN_FIT_POINTS measured points and a positive slope
  (review F1: one public point is not a fit), and a backfilled value stays inside the range the measured points reach
  (round 6: a fit extrapolated past the best measured model gave Fable long_horizon 1.0). The public tier fit is no
  longer a predictor: tier is folded into the axes (round 2), so predicting an axis from it would count it twice.
  Without a fit the component stays missing and the axis averages the components it has (a yellow data note). An
  axis with no component at all is backfilled the same way on the axis; still empty, it scores 0 and is `missing`. A task that demands a missing axis
  leaves automatic routing for that model (an explicit --model still dispatches it).
- A model whose own boards are empty inherits another model's thinking axes only when a vendor statement names the
  underlying model (`INHERIT`, cited); otherwise it is --model only.
- A dated user override (`--user-overrides`, source `user`) replaces a public axis value; none is shipped.
- usd_per_hour merges every cited public source (`measurements.py`: AA cost/time and its OpenRouter repricing; the
  shipped local calibration-sample values are not a source, review F2): each source's value at High (else its
  effort-less listing, else the median of its listed efforts), then the median over sources, or the first available
  source of `--source-priority`. `--distrust` removes a source from every field. A model with no cited usd leaves
  automatic routing on a metered account (billed or quota windows); a named dispatch runs with its cost unpriced.
- duration_multiplier is 1.0 when a time-based source (AA cost/time, OpenRouter repricing of it) is in the merge, since
  its hourly figure already prices the model's own pace; otherwise the shipped multiplier stays, cited as community.
- Plan capacity comes down a ladder (rounds 4-5, user 2026-11-09: "都用社区真实使用量 且保持最新"; capacity is never
  left unknown), per window of each plan in plan-templates.json, recording the rung: 1 the median of the cited community
  observations (`community.py`), or a window ratio a community source states (Fable weekly = half the weekly limit);
  2 a community time-to-exhaust report x the reported model's usd; 3 a vendor-published plan multiplier x the sibling
  plan's value (also kept as a cross-check where a lower rung won). All three empty is a red data error surfaced in
  routing notes (the live percentage still prices scarcity); the packaged template figure is never a fallback. A source
  older than STALE_DAYS (data date, else fetch date) is a yellow note; a source not fetched, or whose sentence is gone,
  contributes nothing (red).
- usd_per_hour has a pluggable basis (`--usd-basis`, `USD_BASES`). Default `community_mix` (rounds 8-9, user
  2026-11-09 "全部都用arena"): `arena.agent_sessions` (arena.ai Agent / Overall real sessions, Cost/Task P50 / Time/Task P50
  x 60, kind `community`) is the primary source; option B (`derived.community_mix`, AA's measured output speed per hour
  priced at the community token mix at list prices) is a labelled fallback (`fallback: True`) only when arena does not
  give cost and time for the model and no same-series sibling has a direct value: with one, the value is that sibling's
  times the ratio of the two cited API list prices over the full community token mix (series conversion, `converted`).
  A model with none of these is mode B. `benchmark` (the AA/OpenRouter merge above) and
  `community` (measured community usd per worker-hour, else `derived` from a measured same-provider model x the benchmark
  ratio, both citations; option A) stay for comparison. Each window's reference-model hours are its capacity over that
  model's arena usd.
- economy is priced by the router at run time; `economy_static` = 1 / (1 + usd / 15) is the tray's static view.
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
THINKING = ('divergence', 'global_rigor', 'module_precision')
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
TIME_BASED = ('arena.agent_sessions', 'aa.cost_time', 'openrouter.cost_time', 'derived.community_mix')
# Window -> the model whose hours count the window's capacity (the plan's own flagship on that pool).
REFERENCE_MODELS = {
    'claude': {'five_hour': 'claude-opus-5-5', 'seven_day': 'claude-opus-5-5', 'weekly_fable': 'claude-fable-5-1'},
    'codex': {'usage': 'gpt-6.1-sol', 'five_hour': 'gpt-6.1-sol'},
    'pi': {'usage': 'gpt-6.1-sol'},
    'antigravity': {'gemini-weekly': 'gemini-4-argon-high', 'gemini-5h': 'gemini-4-argon-high',
                    '3p-weekly': 'claude-opus-5-5', '3p-5h': 'claude-opus-5-5'},
}
# A model whose own boards are empty inherits the thinking axes of the model a vendor statement names (decision 4).
# Vision and GUI stay its own: they are properties of the channel, not of the underlying model.
INHERIT = {
    'dot': {'from': 'gpt-6-astra', 'kind': 'vendor', 'statement': 'OpenAI documents Dot as Astra-powered',
            'cited_in': 'references/accounts.json scoring.basis (reviewed 2026-10-27)', 'url': None},
}
PUBLIC_TIER_SOURCES = ('lmarena.tier_fit', 'secondary.tier_fit')
PREDICTORS = ('aa.index',)      # backfill predictors in order of preference (round 6: not the folded-away tier)
MIN_FIT_POINTS = 8      # measured points a backfill regression needs (review F1)


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


def pick_usd(entries, priority=None):
    """Merge every cited usd_per_hour source: median over sources, or the first of `priority` that has a value."""
    by_source = {}
    for e in entries:
        by_source.setdefault(e['source'], []).append(e)
    if not by_source:
        return {'value': None, 'status': 'missing', 'sources': {}, 'merge': None}
    values = {}
    for src, rows in by_source.items():
        value, how = source_value(rows)
        values[src] = {'value': value, 'how': how, 'kind': rows[0].get('kind'), 'as_of': rows[0].get('as_of'),
                       'url': rows[0].get('url'), 'method': rows[0].get('method'),
                       'by_effort': {r.get('effort') or 'default': r['value'] for r in rows}}
        if rows[0].get('sessions') is not None:
            values[src]['sessions'] = rows[0]['sessions']
    chosen = next((s for s in priority or () if s in values), None)
    if chosen:
        value, merge = values[chosen]['value'], f'source priority: {chosen}'
        used = [chosen]
    else:
        value = round(statistics.median(v['value'] for v in values.values()), 3)
        merge, used = f'median of {len(values)} cited source(s)', sorted(values)
    spread = (round(max(v['value'] for v in values.values()) / min(v['value'] for v in values.values()), 2)
              if len(values) > 1 and min(v['value'] for v in values.values()) > 0 else None)
    return {'value': value, 'status': 'ok', 'merge': merge, 'used': used, 'sources': values, 'max_over_min': spread}


FAST_CAPABLE_MODELS = frozenset(('gpt-6.1-sol', 'gpt-6.1-terra', 'gpt-6-astra', 'gpt-6-sol', 'gpt-6-luna',
                                 'claude-opus-5-5', 'claude-opus-5', 'claude-opus-4-8', 'claude-fable-5-2'))


def _effort_pick(rows, field='value'):
    """Pick High, else default, else median of listed efforts for `field` across `rows`."""
    valid = [r for r in rows if isinstance(r.get(field), (int, float)) and r[field] > 0]
    if not valid:
        return None, None, None
    by_eff = {r.get('effort') or 'default': r for r in valid}
    if 'high' in by_eff:
        return by_eff['high'][field], 'High listing', by_eff['high']
    if 'default' in by_eff:
        return by_eff['default'][field], 'the only listing (no effort named)', by_eff['default']
    listed = {k: v for k, v in by_eff.items() if k in EFFORTS} or by_eff
    val = round(statistics.median(r[field] for r in listed.values()), 3)
    rep = next(iter(listed.values()))
    return val, f'median of the listed efforts {sorted(listed)}', rep


def pick_duration(usd, info=None, throughput=(), anchor_throughput=()):
    """Completion-speed multiplier relative to the anchor model (`claude-opus-5-5`), from public sources only (round 11,
    user 2026-11-10): primary = `arena.agent_sessions` Time/Task (P50) (storing Time/Task and Output Tokens/Task);
    fallback = `aa.cost_time` Time/Task (or outputSpeed). Never returns a flat 1.0 across the fleet.

    Whichever source decides `value`, the result also carries the Artificial Analysis Time/Task of every listed effort
    (`aa`: minutes and the multiplier against the anchor at High) so the router's "faster" tie-break can compare two
    candidates at the same effort from one source (round 14b); a model with no AA row has no `aa`."""
    by_src, anchor_by_src = {}, {}
    for e in throughput or ():
        by_src.setdefault(e['source'], []).append(e)
    for e in anchor_throughput or ():
        anchor_by_src.setdefault(e['source'], []).append(e)
    result = _pick_duration_value(by_src, anchor_by_src)
    aa = _aa_time_by_effort(by_src.get('aa.cost_time'), anchor_by_src.get('aa.cost_time'))
    if aa:
        result['aa'] = aa
    return result


def _aa_time_by_effort(rows, anchor_rows):
    """AA Time/Task per listed effort: {'by_effort': {effort: minutes}, 'multiplier_by_effort': {effort: minutes / the
    anchor's minutes at High}, 'anchor_minutes', 'as_of', 'url', 'source'} or None without a usable row."""
    times = {(r.get('effort') or 'default'): r['time_per_task_min'] for r in rows or ()
             if isinstance(r.get('time_per_task_min'), (int, float)) and not isinstance(r.get('time_per_task_min'), bool)
             and r['time_per_task_min'] > 0}
    if not times:
        return None
    anc_s, _, _ = _effort_pick(anchor_rows or (), 'time_per_task_s')
    anc_min = round((anc_s or 719.3) / 60.0, 3)
    rep = next(r for r in rows if (r.get('effort') or 'default') in times)
    return {'source': 'aa.cost_time', 'kind': 'benchmark', 'as_of': rep.get('as_of'), 'url': rep.get('url'),
            'anchor_minutes': anc_min, 'by_effort': times,
            'multiplier_by_effort': {e: round(t / anc_min, 4) for e, t in times.items()}}


def _pick_duration_value(by_src, anchor_by_src):
    if 'arena.agent_sessions' in by_src:
        rows = by_src['arena.agent_sessions']
        t_min, how, rep = _effort_pick(rows, 'time_per_task_min')
        anc_rows = anchor_by_src.get('arena.agent_sessions') or ()
        anc_min, _, _ = _effort_pick(anc_rows, 'time_per_task_min')
        anc_min = anc_min or 16.3
        if t_min and anc_min:
            tok = rep.get('output_tokens_per_task') if rep else None
            by_eff_mult = {r.get('effort') or 'default': round(r['time_per_task_min'] / anc_min, 4)
                           for r in rows if isinstance(r.get('time_per_task_min'), (int, float)) and r['time_per_task_min'] > 0}
            by_eff_time = {r.get('effort') or 'default': r['time_per_task_min']
                           for r in rows if isinstance(r.get('time_per_task_min'), (int, float)) and r['time_per_task_min'] > 0}
            by_eff_tok = {r.get('effort') or 'default': r['output_tokens_per_task']
                          for r in rows if isinstance(r.get('output_tokens_per_task'), (int, float))}
            return {
                'value': round(t_min / anc_min, 4), 'status': 'ok', 'source': 'arena.agent_sessions',
                'kind': 'community', 'fallback': False, 'as_of': rep.get('as_of'), 'url': rep.get('url'),
                'method': f'arena.ai Agent / Overall Time/Task (P50) {t_min:g} min / anchor {anc_min:g} min ({how})',
                'time_per_task_min': t_min, 'anchor_time_per_task_min': anc_min,
                'output_tokens_per_task': tok,
                'output_tokens_per_min': round(tok / t_min, 1) if isinstance(tok, (int, float)) and t_min > 0 else None,
                'by_effort': by_eff_mult, 'time_per_task_min_by_effort': by_eff_time,
                'output_tokens_per_task_by_effort': by_eff_tok,
            }
    if 'aa.cost_time' in by_src:
        rows = by_src['aa.cost_time']
        t_s, how, rep = _effort_pick(rows, 'time_per_task_s')
        anc_rows = anchor_by_src.get('aa.cost_time') or ()
        anc_s, _, _ = _effort_pick(anc_rows, 'time_per_task_s')
        anc_s = anc_s or 719.3
        if t_s and anc_s:
            tps = rep.get('output_tps') if rep else None
            by_eff_mult = {r.get('effort') or 'default': round(r['time_per_task_s'] / anc_s, 4)
                           for r in rows if isinstance(r.get('time_per_task_s'), (int, float)) and r['time_per_task_s'] > 0}
            return {
                'value': round(t_s / anc_s, 4), 'status': 'ok', 'source': 'aa.cost_time',
                'kind': 'benchmark', 'fallback': True, 'as_of': rep.get('as_of'), 'url': rep.get('url'),
                'method': f'fallback (not on arena.ai Agent / Overall): AA Time/Task {t_s:g} s / anchor {anc_s:g} s ({how})',
                'time_per_task_s': t_s, 'anchor_time_per_task_s': anc_s,
                'time_per_task_min': round(t_s / 60.0, 3), 'output_tps': tps, 'by_effort': by_eff_mult,
            }
        tps, how_tps, rep_tps = _effort_pick(rows, 'output_tps')
        anc_tps, _, _ = _effort_pick(anc_rows, 'output_tps')
        anc_tps = anc_tps or 83.29
        if tps and anc_tps:
            return {
                'value': round(anc_tps / tps, 4), 'status': 'ok', 'source': 'aa.cost_time',
                'kind': 'benchmark', 'fallback': True, 'as_of': rep_tps.get('as_of'), 'url': rep_tps.get('url'),
                'method': f'fallback (AA outputSpeed): anchor {anc_tps:g} tok/s / {tps:g} tok/s ({how_tps})',
                'output_tps': tps,
            }
    return {'value': None, 'status': 'missing', 'source': None, 'kind': None,
            'method': 'no cited public completion-speed source'}


def pick_image_input(entries):
    """Image-input modality from cited public sources (round 11, user 2026-11-10): a public vision/GUI benchmark listing
    wins over a vendor/OpenRouter modality flag; without any source, status is `unknown` (mode B when vision_tools >= 2)."""
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


def pick_fast_mode(model, entries):
    """Public fast-mode price and speed multipliers (round 11, user 2026-11-10)."""
    if entries:
        e0 = entries[0]
        val = e0.get('value') or {}
        return {'status': 'ok', 'price_multiplier': val.get('price_multiplier'),
                'speed_multiplier': val.get('speed_multiplier'), 'source': e0['source'],
                'kind': e0.get('kind', 'vendor'), 'as_of': e0.get('as_of'), 'url': e0.get('url'),
                'pricing_url': e0.get('pricing_url'), 'fast_price': e0.get('fast_price'),
                'standard_price': e0.get('standard_price'), 'method': e0.get('method')}
    if model in FAST_CAPABLE_MODELS:
        return {'status': 'missing', 'price_multiplier': None, 'speed_multiplier': None, 'source': None,
                'reason': 'provider does not publish a fast-mode burn or speed multiplier'}
    return {'status': 'unsupported', 'price_multiplier': None, 'speed_multiplier': None, 'source': None}


def pick_price(entries):
    """Public list price (USD per million tokens) for converting local token counts (e.g. opencode.db) into USD."""
    for src in ('aa.price', 'openrouter.price'):
        for e in entries or ():
            if e.get('source') == src and isinstance(e.get('value'), dict):
                v = e['value']
                if (v.get('output') or 0) > 0 or (v.get('input') or 0) > 0:
                    return {'value': v, 'source': src, 'kind': e.get('kind', 'vendor'),
                            'as_of': e.get('as_of'), 'url': e.get('url'), 'listed_as': e.get('listed_as')}
    return None


def legacy_tier(entries):
    public = [e for e in entries if e['source'] in PUBLIC_TIER_SOURCES]
    user = [e for e in entries if e['source'] == 'user']
    chosen = public[0] if public else (user[0] if user else None)
    return {'value': chosen['value'] if chosen else None, 'source': chosen['source'] if chosen else None,
            'note': 'comparison only; the router no longer gates on a scalar tier'}


class Scorer:
    def __init__(self, meas, acc, distrust=(), overrides=()):
        self.meas, self.acc = meas, acc
        self.distrust = set(distrust)
        self.overrides = list(overrides)
        self.models = {k: v for k, v in acc['models'].items() if isinstance(v, dict)}

    def trusted(self, entries):
        return [e for e in entries or () if e['source'] not in self.distrust]

    def components(self, fields, axis):
        comps = {}
        for e in self.trusted(fields.get(axis)):
            comps.setdefault(e['component'], {})[e['source']] = e['value']
        return {c: {'value': mean(list(srcs.values())), 'sources': srcs, 'status': 'measured'} for c, srcs in comps.items()}

    def predictors(self, fields):
        """{predictor: x} for the public predictors the model has: the AA index."""
        idx = self.trusted(fields.get('intelligence_index'))
        return {'aa.index': idx[0]['value']} if idx else {}

    @staticmethod
    def backfill(fits, key, preds):
        """(predictor, x, fit) for the first predictor the model has that has a usable fit."""
        for name in PREDICTORS:
            if name in preds and fits.get((*key, name)):
                return name, preds[name], fits[(*key, name)]
        return None, None, None

    def provenance(self, target, name, x, f, row):
        """Where an estimate comes from: the predictor and its reading, and the fit (id in `backfill_fits`, r, n)."""
        entry = (self.trusted(row['fields'].get('intelligence_index')) or [{}])[0] if name == 'aa.index' else {}
        out = {'predictor': name, 'x': x, 'fit': f'{target}~{name}', 'r': round(f['r'], 4), 'n': f['n'],
               **{k: entry[k] for k in ('as_of', 'listed_as') if entry.get(k)}}
        if not f['x_min'] <= x <= f['x_max']:      # predict() clamps the reading to the sampled range
            out['x_clamped'] = f['x_min'] if x < f['x_min'] else f['x_max']
        return out

    def convert(self, m, base, a, c, rows, fits):
        """Component `c` of axis `a` for model `m` from the same-series `base` (see `score`)."""
        vb = rows[base]['comps'][a][c]['value']
        x, xb = rows[m]['preds'].get('aa.index'), rows[base]['preds'].get('aa.index')
        f = fits.get((a, c, 'aa.index'))
        conv = {'base_model': base, 'base_value': vb, 'method': 'series conversion'}
        value = vb
        if f and x is not None and xb is not None:
            raw = vb + f['b'] * (x - xb)
            value = round(float(np.clip(raw, f['y_min'], f['y_max'])), 4)
            conv.update(predictor='aa.index', x=x, x_base=xb, shift=round(f['b'] * (x - xb), 4), fit=f'{a}.{c}~aa.index')
            if not f['y_min'] <= raw <= f['y_max']:
                conv['y_clamped'] = f['y_min'] if raw < f['y_min'] else f['y_max']
        return {'value': value, 'sources': {}, 'status': 'converted', 'conversion': conv}

    @staticmethod
    def fit(points):
        if len(points) < MIN_FIT_POINTS:
            return None
        x, y = np.array([p[0] for p in points]), np.array([p[1] for p in points])
        b, a = np.polyfit(x, y, 1)
        if b <= 0:      # ability falling with the index is not a usable predictor
            return None
        return {'a': float(a), 'b': float(b), 'r': float(np.corrcoef(x, y)[0, 1]), 'n': len(points),
                'x_min': float(x.min()), 'x_max': float(x.max()), 'y_min': float(y.min()), 'y_max': float(y.max())}

    @staticmethod
    def predict(f, x):
        """The fit at x, with x clamped to the range of the sampled predictor readings and the value kept inside the
        range its measured points reach (round 6; round 058 review F2: a reading above the best sampled model is not
        extrapolated, it gets the fit at that model's reading)."""
        x = float(np.clip(x, f.get('x_min', x), f.get('x_max', x)))
        return round(float(np.clip(f['a'] + f['b'] * x, f['y_min'], f['y_max'])), 4)

    def score(self):
        rows = {}
        for m in self.models:
            fields = self.meas['models'].get(m) or {}
            rows[m] = {'fields': fields, 'preds': self.predictors(fields),
                       'comps': {a: self.components(fields, a) for a in AXES}}
        fits = {}
        for a, comps in AXES.items():
            for c in comps:
                for name in PREDICTORS:
                    pts = [(r['preds'][name], r['comps'][a][c]['value']) for r in rows.values()
                           if c in r['comps'][a] and r['comps'][a][c]['status'] == 'measured' and name in r['preds']]
                    fits[(a, c, name)] = self.fit(pts)
        # Series conversion (round 058, user 2026-11-10: "后续同系列新出，缺少实测数据的情况下，先按照同系列的全分数进行折算，
        # 不要留下任何一个默认值或者fallback"): a component a model does not measure, that a same-series sibling does
        # (calibration/series.py), is the nearest such sibling's value moved along the component's fit on the AA index
        # by the two models' index difference (not clamped: both are readings), kept inside the fit's measured range;
        # without a fit or a shared index reading it is the sibling's value. Only the components an axis counts for
        # every model (`expected`: the anchor measures it or it has a fit) are filled, so a fully measured model's axes
        # stay its own.
        # Expected components of an axis: those the anchor model measures or that have a usable fit.
        anchor_comps = (rows.get(measurements.ANCHOR) or {}).get('comps') or {}
        expected = {a: [c for c in comps if ((anchor_comps.get(a) or {}).get(c) or {}).get('status') == 'measured'
                        or any(fits.get((a, c, n)) for n in PREDICTORS)] or list(comps)
                    for a, comps in AXES.items()}
        sibs = series.groups(self.acc)
        index = {m: r['preds'].get('aa.index') for m, r in rows.items()}
        tier = {m: (self.models.get(m) or {}).get('tier') for m in rows}
        board = series.boards(self.meas)
        for m, r in rows.items():
            for a, comps in AXES.items():
                for c in expected[a]:
                    if c in r['comps'][a]:
                        continue
                    bases = [b for b in series.nearest(m, sibs.get(m, ()), index, board, tier)
                             if (rows[b]['comps'][a].get(c) or {}).get('status') == 'measured']
                    if bases:
                        r['comps'][a][c] = self.convert(m, bases[0], a, c, rows, fits)
        # Component backfill, for a model no same-series sibling gives the component: the component's fit on the AA
        # index over every model that measures it. A component stays missing only without a usable fit or without the
        # model's predictor; the pessimistic zero below applies to exactly those.
        for r in rows.values():
            for a, comps in AXES.items():
                for c in comps:
                    name, x, f = self.backfill(fits, (a, c), r['preds'])
                    if c not in r['comps'][a] and f:
                        r['comps'][a][c] = {'value': self.predict(f, x), 'sources': {},
                                            'status': 'backfilled', 'backfill': self.provenance(f'{a}.{c}', name, x, f, r)}
        # Pessimistic unknowns for partial axes (round 8 & round 11, user): any expected component of the axis that
        # remains neither measured nor estimated (no fit, or no AA index for the model) counts as 0, so a single board
        # cannot carry the axis for a model nothing else can calibrate.
        for r in rows.values():
            r['axes'] = {}
            for a, comps in AXES.items():
                present = [c for c in comps if c in r['comps'][a]]
                measured = [c for c in present if r['comps'][a][c]['status'] == 'measured']
                denom = [c for c in comps if c in present or c in expected[a]]
                missing = [c for c in denom if c not in present]
                kinds = {r['comps'][a][c]['status'] for c in present}
                status = ('missing' if not present else 'measured' if len(measured) == len(denom)
                          else 'partial' if (measured or missing) else 'converted' if kinds == {'converted'}
                          else 'backfilled')
                vals = [r['comps'][a][c]['value'] if c in r['comps'][a] else 0.0 for c in denom] if present else []
                r['axes'][a] = {'value': mean(vals), 'status': status,
                                'components': r['comps'][a], 'missing_components': missing}
        # Axis backfill for a model with no component on the axis: the axis fit on the index.
        for a in AXES:
            for name in PREDICTORS:
                pts = [(r['preds'][name], r['axes'][a]['value']) for r in rows.values()
                       if r['axes'][a]['status'] in ('measured', 'partial') and not r['axes'][a]['missing_components']
                       and name in r['preds']]
                fits[(a, None, name)] = self.fit(pts)
        for m, r in rows.items():
            for a, ax in r['axes'].items():
                if ax['status'] != 'missing':
                    continue
                name, x, f = self.backfill(fits, (a, None), r['preds'])
                if f:
                    ax.update(value=self.predict(f, x), status='backfilled',
                              backfill={**self.provenance(a, name, x, f, r), 'level': 'axis'})
                else:
                    ax.update(value=0.0, flag='missing', reason='no trusted source and no public predictor to backfill from')
        self.inherit(rows)
        self.apply_overrides(rows)
        return rows, {f'{a}.{c}~{n}' if c else f'{a}~{n}': {k: round(v, 4) for k, v in f.items()}
                      for (a, c, n), f in fits.items() if f}

    def inherit(self, rows):
        for m, rule in INHERIT.items():
            if m not in rows or rule['from'] not in rows or rule['from'] in self.distrust:
                continue
            r, src = rows[m], rows[rule['from']]
            for a in THINKING:
                if r['axes'][a]['status'] == 'missing':
                    r['axes'][a] = {'value': src['axes'][a]['value'], 'status': 'inherited', 'components': {},
                                    'missing_components': [], 'inherited': {'from': rule['from'], **{k: v for k, v in rule.items() if k != 'from'}}}

    def apply_overrides(self, rows):
        for o in self.overrides:
            if o.get('model') in rows and o.get('axis') in AXES and o.get('as_of') and isinstance(o.get('value'), (int, float)):
                ax = rows[o['model']]['axes'][o['axis']]
                ax.update(public_value=ax['value'], value=float(o['value']), status='user_override',
                          override={'source': 'user', 'as_of': o['as_of'], 'note': o.get('note')})
                ax.pop('flag', None)


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
                                           f"({ctx['basis']})", report=r.get('quote')))
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


def capacity(meas, plans, usd, now=None, basis='community_mix'):
    """({family: {plan: {window: capacity row}}}, data errors) down the capacity ladder from `meas`."""
    now = now or datetime.now(timezone.utc)
    cap = meas.get('capacity') or {}
    obs = {}
    for o in cap.get('observations') or ():
        obs.setdefault((o['family'], o['plan'], o['window']), []).append(o)
    rules = cap.get('rules') or ()
    ctx = {'now': now, 'usd': usd, 'obs': obs, 'basis': basis,
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


# --- usd per worker-hour bases ------------------------------------------------------------------------------------
PROVIDER_PREFIX = (('claude-', 'anthropic'), ('gpt-', 'openai'), ('gemini-', 'google'), ('grok-', 'xai'))


def provider(model):
    return next((p for prefix, p in PROVIDER_PREFIX if str(model).startswith(prefix)), None)


def derive_usd(measured, bench):
    """`derived` usd for models with no measured community value: the community usd of a measured model of the same
    provider times the benchmark usd ratio of the two (round 4: benchmarks give relative prices only). The measured
    model is the one whose benchmark usd is closest (least extrapolation). Both citations are kept."""
    out = {}
    for m, b in bench.items():
        if m in measured or (b or {}).get('value') is None or provider(m) is None:
            continue
        bases = [k for k in measured if provider(k) == provider(m) and (bench.get(k) or {}).get('value')]
        if not bases:
            continue
        base = min(bases, key=lambda k: (abs(float(np.log(b['value'] / bench[k]['value']))), k))
        ratio = b['value'] / bench[base]['value']
        out[m] = {'value': round(measured[base]['value'] * ratio, 3), 'status': 'ok', 'kind': 'derived',
                  'merge': f'derived: community usd of {base} x benchmark usd ratio {m} / {base} = {ratio:.3f}',
                  'used': [], 'max_over_min': None,
                  'sources': {'community': {**measured[base], 'model': base},
                              'ratio': {'value': round(ratio, 4),
                                        'numerator': {'model': m, 'value': b['value'], 'used': b.get('used')},
                                        'denominator': {'model': base, 'value': bench[base]['value'],
                                                        'used': bench[base].get('used')}}}}
    return out


def usd_benchmark(scorer, rows, meas, priority):
    return {m: pick_usd([e for e in scorer.trusted(r['fields'].get('usd_per_hour'))
                         if e.get('kind') in ('benchmark', 'vendor')], priority) for m, r in rows.items()}


def usd_community(scorer, rows, meas, priority):
    """Measured community usd per worker-hour (usd_per_hour entries of kind `community`), else `derived`, else missing."""
    bench = usd_benchmark(scorer, rows, meas, priority)            # the ratio is benchmark against benchmark only
    measured = {}
    for m, r in rows.items():
        entries = [e for e in scorer.trusted(r['fields'].get('usd_per_hour')) if e.get('kind') == 'community']
        if entries:
            measured[m] = {**pick_usd(entries), 'kind': 'community'}
    derived = derive_usd({m: {'value': v['value'], 'used': v['used'], 'sources': v['sources']} for m, v in measured.items()},
                         bench)
    return {m: measured.get(m) or derived.get(m) or {'value': None, 'status': 'missing', 'sources': {}, 'merge': None}
            for m in rows}


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


def usd_community_mix(scorer, rows, meas, priority):
    """Rounds 8-9 (user, 2026-11-09): `arena.agent_sessions` (arena.ai Agent / Overall real sessions, Cost/Task P50 ÷
    Time/Task P50 × 60, kind `community`) is the primary source; option B (`derived.community_mix`, AA throughput ×
    community token mix × list price) is a labelled fallback (`fallback: True`) for models arena does not list with cost
    and time; `convert_usd` then replaces it for a model with a same-series sibling that has a direct value. When `--source-priority` is given, it falls through `[*priority, 'arena.agent_sessions', 'derived.community_mix']`
    and never takes a median across mixed scales (review F4)."""
    order = [*(priority or ()), 'arena.agent_sessions', 'derived.community_mix']
    out = {}
    for m, r in rows.items():
        raw = r['fields'].get('usd_per_hour') or ()
        trusted = scorer.trusted(raw)
        by_src = {}
        for e in trusted:
            by_src.setdefault(e['source'], []).append(e)
        chosen = next((s for s in order if s in by_src), None)
        if not chosen:
            why = arena_usd_miss_reason(scorer, m, raw, meas)
            out[m] = {'value': None, 'status': 'missing', 'sources': {}, 'merge': None, 'reason': why}
            continue
        picked = pick_usd(by_src[chosen], [chosen])
        is_fb = chosen != 'arena.agent_sessions'
        kind = by_src[chosen][0].get('kind') or ('derived' if is_fb else 'community')
        if chosen == 'derived.community_mix' and 'derived.community_mix' not in (priority or ()):
            why = arena_usd_miss_reason(scorer, m, raw, meas)
            merge = f"fallback ({why}): {picked.get('merge') or 'derived.community_mix'}"
        elif chosen in (priority or ()) and chosen != 'arena.agent_sessions':
            merge = f'source priority: {chosen}'
        else:
            merge = picked.get('merge')
        out[m] = {**picked, 'merge': merge, 'kind': kind, 'fallback': is_fb}
    return out


USD_BASES = {
    'community_mix': {'fn': usd_community_mix,
                      'scale': 'arena.ai Agent / Overall real sessions (Cost/Task P50 / Time/Task P50 x 60, kind community); '
                               'option B (AA output speed x community token mix at list prices) as labelled fallback',
                      'note': 'round 8, user 2026-11-09: primary = arena.agent_sessions; fallback = derived.community_mix '
                              '(fallback: True) when arena does not list the model with cost and time'},
    'benchmark': {'fn': usd_benchmark, 'scale': 'AA benchmark tokens at list prices (AA cost/time; OpenRouter repricing)',
                  'note': 'not the community real-usage scale of the capacities; kept for comparison'},
    'community': {'fn': usd_community, 'scale': 'community real-usage measurements per worker-hour, else derived',
                  'note': 'option A (not chosen): a model with no measured or derivable value is mode B'},
}


def axis_evidence(axes):
    """Round 058 review F5: per axis, the measured sources that are not Artificial Analysis (AA boards and every value
    backfilled from the AA index are one source); `aa_only` when no axis has any, though some axis has a value."""
    indep = {a: sorted({s for c in (ax.get('components') or {}).values() if c.get('status') == 'measured'
                        for s in (c.get('sources') or {}) if not s.startswith('aa.')}) for a, ax in axes.items()}
    valued = any(ax.get('status') in ('measured', 'partial', 'backfilled', 'converted') for ax in axes.values())
    return {'independent_sources': indep, 'aa_only': valued and not any(indep.values())}


def legacy(info):
    return {k: info.get(k) for k in ('tier', 'usd_per_hour', 'duration_multiplier', 'subjective', 'capabilities', 'visual')}


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


def convert_usd(usd, rows, scorer, meas, acc):
    """Series conversion of usd_per_hour: a model without a direct value (`derived` or missing) and with a same-series
    sibling that has one takes the nearest such sibling's value times the ratio of the two cited API list prices over
    the full community token mix."""
    direct = {m for m, u in usd.items() if u.get('value') is not None and u.get('kind') not in ('derived', 'converted')}
    sibs = series.groups(acc)
    index = {m: r['preds'].get('aa.index') for m, r in rows.items()}
    tier = {m: (scorer.models.get(m) or {}).get('tier') for m in rows}
    mix = token_mix(meas)
    for m in rows:
        if m in direct:
            continue
        for b in series.nearest(m, [b for b in sibs.get(m, ()) if b in direct], index, series.boards(meas), tier):
            prices = {k: pick_price(scorer.trusted(rows[k]['fields'].get('price'))) for k in (m, b)}
            ratio = list_price_ratio(prices[m], prices[b], mix)
            if ratio is None:
                continue
            usd[m] = {'value': round(usd[b]['value'] * ratio, 3), 'status': 'ok', 'kind': 'converted', 'fallback': False,
                      'merge': f'series conversion: usd of {b} x API list price ratio {ratio:.4f} over the community token mix',
                      'used': [], 'max_over_min': None,
                      'sources': {'base': {**usd[b], 'model': b}, 'ratio': round(ratio, 4), 'prices': prices, 'token_mix': mix}}
            break


def build(meas, acc, plans, distrust=(), meas_bytes=b'', priority=None, overrides=(), usd_basis='community_mix', now=None):
    scorer = Scorer(meas, acc, distrust, overrides)
    rows, fits = scorer.score()
    usd = USD_BASES[usd_basis]['fn'](scorer, rows, meas, priority)
    convert_usd(usd, rows, scorer, meas, acc)
    caps, cap_errors = capacity(meas, plans, usd, now, usd_basis)
    anchor_tp = scorer.trusted(((rows.get(measurements.ANCHOR) or {}).get('fields') or {}).get('throughput'))
    models, errors = {}, []
    for m, r in rows.items():
        model_errors = []
        if usd[m]['status'] == 'missing':
            model_errors.append({'model': m, 'field': 'usd_per_hour', 'reason': 'no_cited_source', 'level': 'red_if_metered',
                                 'effect': 'out of automatic routing on any quota-pool or metered account; a named '
                                           'dispatch runs with its cost unpriced'})
        dur = pick_duration(usd[m], acc['models'][m], scorer.trusted(r['fields'].get('throughput')), anchor_tp)
        if dur['status'] == 'missing':
            model_errors.append({'model': m, 'field': 'duration_multiplier', 'reason': 'no_cited_source', 'level': 'yellow',
                                 'effect': 'no public completion-speed measurement'})
        img = pick_image_input(scorer.trusted(r['fields'].get('image_input')))
        if img['status'] == 'unknown':
            model_errors.append({'model': m, 'field': 'image_input', 'reason': 'no_cited_source',
                                 'level': 'red_if_vision_demanded',
                                 'effect': 'unknown image-input modality; excluded from automatic routing when '
                                           'vision_tools >= 2 (mode B)'})
        fm = pick_fast_mode(m, scorer.trusted(r['fields'].get('fast_mode')))
        if fm['status'] == 'missing':
            model_errors.append({'model': m, 'field': 'fast_mode', 'reason': 'no_cited_source', 'level': 'yellow',
                                 'effect': 'fast mode is not priced or enabled from a policy multiplier on the five-axis path'})
        for a, ax in r['axes'].items():
            if ax.get('flag') == 'missing':
                model_errors.append({'model': m, 'field': a, 'reason': 'no_trusted_source', 'level': 'red_if_demanded',
                                     'effect': 'scores 0; a task that demands this axis leaves automatic routing for the model'})
            elif ax.get('missing_components') and ax['status'] != 'inherited':
                model_errors.append({'model': m, 'field': f"{a}.{','.join(ax['missing_components'])}",
                                     'reason': 'component_unmeasured_no_fit', 'level': 'yellow',
                                     'effect': 'missing components count as 0 in the axis mean (pessimistic partial axis)'})
        evidence = axis_evidence(r['axes'])
        if evidence['aa_only']:
            model_errors.append({'model': m, 'field': 'axes', 'reason': 'aa_only', 'level': 'yellow',
                                 'effect': 'every axis value comes from Artificial Analysis (the Intelligence Index and AA '
                                           'boards collinear with it): one source, not independent evidence'})
        errors += model_errors
        idx = scorer.trusted(r['fields'].get('intelligence_index'))
        u = usd[m]['value']
        models[m] = {'axes': r['axes'], 'scores': {a: ax['value'] for a, ax in r['axes'].items()},
                     'missing_axes': [a for a, ax in r['axes'].items() if ax.get('flag') == 'missing'],
                     'usd_per_hour': usd[m], 'duration_multiplier': dur,
                     'image_input': img, 'fast_mode': fm,
                     'price': pick_price(scorer.trusted(r['fields'].get('price'))),
                     'intelligence_index': idx[0]['value'] if idx else None,
                     'economy_static': round(1 / (1 + u / 15), 3) if u is not None else None,
                     'legacy_public_tier': legacy_tier(scorer.trusted(r['fields'].get('tier'))),
                     'evidence': evidence, 'data_errors': model_errors, 'legacy': legacy(acc['models'][m])}
    doc = {'schema': SCHEMA, 'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
           'measurements_sha256': hashlib.sha256(meas_bytes).hexdigest() if meas_bytes else None,
           'distrusted_sources': sorted(distrust), 'source_priority': list(priority or ()),
           'user_overrides': list(overrides),
           'method': __doc__.strip().splitlines()[0],
           'backfill_fits': fits,
           'usd_basis': {'name': usd_basis, 'scale': USD_BASES[usd_basis]['scale'], 'note': USD_BASES[usd_basis]['note']},
           'capacity': caps, 'capacity_stale_days': STALE_DAYS,
           'capacity_not_found': (meas.get('capacity') or {}).get('not_found') or {},
           'capacity_errors': cap_errors, 'source_ages': source_ages(meas, now),
           'usd_checks': [{**h, 'basis': usd_basis, 'basis_value': (usd.get(h['model']) or {}).get('value'),
                           'community_over_basis': (round(h['value'] / usd[h['model']]['value'], 2)
                                                    if (usd.get(h['model']) or {}).get('value') else None)}
                          for h in (meas.get('capacity') or {}).get('hourly_checks') or ()],
           'anchor': {'model': measurements.ANCHOR, 'value': measurements.ANCHOR_VALUE},
           'models': models, 'data_errors': errors}
    return doc


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--measurements', default=str(measurements.OUT))
    ap.add_argument('--distrust', action='append', default=[], help='a source id to leave out of every field (repeatable)')
    ap.add_argument('--source-priority', default='', help='comma-separated usd sources; the first with a value wins over the median')
    ap.add_argument('--user-overrides', help='JSON list of dated {model, axis, value, as_of, note} overrides (source `user`)')
    ap.add_argument('--usd-basis', default='community_mix', choices=sorted(USD_BASES))
    ap.add_argument('--out', default=str(OUT))
    args = ap.parse_args()
    raw = Path(args.measurements).read_bytes()
    overrides = json.loads(Path(args.user_overrides).read_text(encoding='utf-8')) if args.user_overrides else []
    priority = [s.strip() for s in args.source_priority.split(',') if s.strip()]
    doc = build(json.loads(raw), sources.accounts(), json.loads(PLANS.read_text(encoding='utf-8')), args.distrust, raw,
                priority, overrides, usd_basis=args.usd_basis)
    Path(args.out).write_text(json.dumps(doc, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    print(f'{len(doc["models"])} models -> {args.out}')
    print(f'{"model":28s} {"usd/h":>7}  ' + ' '.join(f'{a[:10]:>10}' for a in AXES))
    for m, r in doc['models'].items():
        u = r['usd_per_hour']['value']
        mark = {'backfilled': '*', 'converted': '~', 'inherited': '^', 'user_override': 'u'}
        cells = ' '.join(f"{r['scores'][a]:>9.2f}{mark.get(r['axes'][a]['status'], '!' if r['axes'][a].get('flag') else ' ')}"
                         for a in AXES)
        print(f"{m:28s} {u if u is not None else '-':>7}  {cells}")
    print('* backfilled  ~ series conversion  ^ inherited (vendor statement)  u user override  ! missing (0)')
    for fam, plans in doc['capacity'].items():
        for plan, wins in plans.items():
            print(f'capacity {fam}/{plan}: ' + ', '.join(
                f"{w}={r['capacity_usd'] if r['capacity_usd'] is not None else '-'}({r['status']}, rung {r['rung']})"
                for w, r in wins.items()))


if __name__ == '__main__':
    sys.exit(main())
