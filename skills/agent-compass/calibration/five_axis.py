"""Category scores, usd_per_hour, duration and plan capacity from `references/public-measurements.json`.

`python -X utf8 five_axis.py [--distrust SOURCE ...] [--user-overrides PATH] [--out PATH]`

Writes `references/five-axis-scores.json`, the only model scoring the router reads (allocator.five_axis_overlay). The
user's model of scoring (2026-11-11): every board gives a type score (a component), the type scores belong to the four
categories (divergence, global_rigor, module_precision, vision_tools), and the router scores on the four categories and
the quota economy together. Nothing else scores a model: the tier is not a score.

- Each category is the mean of its type scores (components), each type score the mean of its trusted sources
  (`measurements.py` converts every source the same way, relative to the anchor model: 0.8 x s / s_anchor, so a new top
  model moves nothing). The router fits the categories to the task's demand continuously against targets of the anchor
  value x demand / 3, written here as `anchor`.
- The tier (calibration/tiers.py) is the model's overall level: the arena.ai Overall board mapped onto the user's scale
  through the six anchor rows. A model arena Overall does not list takes its relative position on another non-arena overall
  board interpolated onto that scale (`tier.aa_index`; several boards would combine by the mean of their interpolated
  tiers); a model on no overall board takes the tier of its nearest same-series sibling; with none it has no tier. The
  tier has one use: bridging a type score the model lacks (`estimate` below).
- A type score a model lacks is estimated through its tier: a linear fit of that type score on tier over the models that
  have both (direct tiers only), kept to the positive slopes with out-of-sample skill (leave-one-out error below the
  error of predicting the mean) over at least MIN_FIT_POINTS models, and its value kept inside the range the measured
  points reach. With a same-series sibling that measures the type score (calibration/series.py, round 058, user
  2026-11-10) the estimate is the nearest such sibling's value moved along the fit by the two tiers' difference; where
  the fit has no skill the tier does not predict that type score, so the shift is 0 and the estimate is the sibling's
  value (the record says `shift 0: fit rejected, ...`); without a sibling it is the fit at the model's tier. The record
  keeps the fit (r, n, skill), the tier and its source, the base, the tier difference and the shift (`estimate`, status
  `estimated`). A direct measurement replaces the estimate at the next refresh. Without a tier, or with neither a sibling
  nor a fit with skill, the type score stays missing, and so does the category when it has none. A category with some
  type scores measured or estimated and others missing is `partial` (a yellow note, owner 2026-11-09 "ranking-axis gaps
  → yellow, scored 0"): it averages over the type scores the anchor model measures or a fit with skill covers, a missing one counting 0, and the
  model stays in automatic routing. A category with none is `missing`, scores 0, and a task that demands it leaves
  automatic routing for that model (an explicit --model still dispatches it).
- A row that `accounts.json` `same_model` declares to be the very model another row names (a vendor statement with its
  url, or a dated user statement, kind `user`) takes that model's type scores as they stand, measured or estimated
  (method `same_model`), for the categories the row `carries`, and its Time/Task; the declared model is also its tier
  base before any other sibling. The other categories are the channel's own and stay missing.
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
THINKING = ('divergence', 'global_rigor', 'module_precision')
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
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


FAST_CAPABLE_MODELS = frozenset(('gpt-6.1-sol', 'gpt-6.1-terra', 'gpt-6-astra', 'gpt-6-sol', 'gpt-6-luna',
                                 'claude-opus-5-5', 'claude-opus-5', 'claude-opus-4-8', 'claude-fable-5-2'))


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
    def fit(points):
        """Linear fit of a type score on tier, or (None, why) when it has no usable skill."""
        if len(points) < MIN_FIT_POINTS:
            return None, f'{len(points)} models with both (a fit needs {MIN_FIT_POINTS})'
        x, y = np.array([p[0] for p in points]), np.array([p[1] for p in points])
        if float(np.ptp(x)) == 0.0:
            return None, 'every model has the same tier'
        b, a = np.polyfit(x, y, 1)
        if b <= 0:      # ability falling with the tier is not a usable estimate
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
        skill = 1.0 - err / base if base > 0 else 0.0
        if skill <= 0:
            return None, f'out-of-sample skill {skill:.3f} (leave-one-out error not below the mean predictor\'s)'
        return {'a': float(a), 'b': float(b), 'r': float(np.corrcoef(x, y)[0, 1]), 'skill': float(skill), 'n': n,
                'x_min': float(x.min()), 'x_max': float(x.max()), 'y_min': float(y.min()), 'y_max': float(y.max())}, None

    @staticmethod
    def predict(f, x):
        """The fit at tier x, x clamped to the tiers the fit saw and the value kept inside the range its measured points
        reach (round 6, round 058 review F2: a tier above the best sampled model is not extrapolated)."""
        x = float(np.clip(x, f['x_min'], f['x_max']))
        return round(float(np.clip(f['a'] + f['b'] * x, f['y_min'], f['y_max'])), 4)

    @staticmethod
    def fit_record(fid, f):
        return {'fit': fid, 'r': round(f['r'], 4), 'skill': round(f['skill'], 4), 'n': f['n']}

    def estimate(self, m, base, a, c, rows, fits, rejected, tier):
        """Type score `c` of category `a` for model `m` through its tier (module docstring)."""
        fid = f'{a}.{c}~tier'
        f = fits.get((a, c))
        t = tier[m]['value']
        est = {'method': 'tier_fit', 'tier': t, 'tier_source': tier[m]['method']}
        if base is not None:
            vb = rows[base]['comps'][a][c]['value']
            est.update(method='sibling_shift', base_model=base, base_value=vb, base_tier=tier[base]['value'])
            value = vb
            if f and tier[base]['value'] is not None:
                diff = t - tier[base]['value']
                raw = vb + f['b'] * diff
                value = round(float(np.clip(raw, f['y_min'], f['y_max'])), 4)
                est.update(self.fit_record(fid, f), tier_difference=round(diff, 4), shift=round(f['b'] * diff, 4))
                if not f['y_min'] <= raw <= f['y_max']:
                    est['clamped_to'] = f['y_min'] if raw < f['y_min'] else f['y_max']
            else:
                # No skill: the tier does not predict this type score, so the tier difference moves nothing.
                est.update(tier_difference=round(t - tier[base]['value'], 4), shift=0.0,
                           shift_basis=f"shift 0: fit rejected, {rejected[fid]}")
        else:
            value = self.predict(f, t)
            est.update(self.fit_record(fid, f))
            if not f['x_min'] <= t <= f['x_max']:
                est['tier_clamped_to'] = f['x_min'] if t < f['x_min'] else f['x_max']
        return {'value': value, 'sources': {}, 'status': 'estimated', 'estimate': est}

    def score(self):
        rows = {}
        for m in self.models:
            fields = self.meas['models'].get(m) or {}
            rows[m] = {'fields': fields, 'comps': {a: self.components(fields, a) for a in AXES}}
        index = {m: ((self.trusted(r['fields'].get('intelligence_index')) or [{}])[0]).get('value') for m, r in rows.items()}
        board = series.boards(self.meas)
        tier = self.tier_records(rows, index, board)
        direct_tier = {m: t['value'] for m, t in tier.items() if t['method'] in ('anchor', 'arena_overall', 'other_overall')}
        fits, rejected = {}, {}
        for a, comps in AXES.items():
            for c in comps:
                pts = [(direct_tier[m], r['comps'][a][c]['value']) for m, r in rows.items()
                       if c in r['comps'][a] and r['comps'][a][c]['status'] == 'measured' and m in direct_tier]
                fits[(a, c)], why = self.fit(pts)
                if why:
                    rejected[f'{a}.{c}~tier'] = why
        # Expected type scores of a category: those the anchor model measures or that have a usable fit.
        anchor_comps = (rows.get(measurements.ANCHOR) or {}).get('comps') or {}
        expected = {a: [c for c in comps if ((anchor_comps.get(a) or {}).get(c) or {}).get('status') == 'measured' or fits.get((a, c))]
                    or list(comps) for a, comps in AXES.items()}
        sibs, same = series.groups(self.acc), series.same_model(self.acc)
        declared_rows = []
        for m, r in rows.items():
            if (same.get(m) or {}).get('model') in rows:
                declared_rows.append(m)     # takes its declared model's type scores once those are all in, below
                continue
            if tier[m]['value'] is None:
                continue
            carries = (same.get(m) or {}).get('carries')
            for a, comps in AXES.items():
                if carries is not None and a not in carries:
                    continue        # a category the row's channel does not share with the model behind it stays missing
                for c in comps:
                    if c in r['comps'][a] or not (c in expected[a] or fits.get((a, c))):
                        continue
                    bases = [b for b in series.nearest(m, sibs.get(m, ()), index, board, direct_tier, same)
                             if (rows[b]['comps'][a].get(c) or {}).get('status') == 'measured' and tier[b]['value'] is not None]
                    if bases or fits.get((a, c)):
                        r['comps'][a][c] = self.estimate(m, bases[0] if bases else None, a, c, rows, fits, rejected, tier)
        # A `same_model` row is the very model it declares: it takes that model's type scores as they stand, measured or
        # estimated, for the categories it carries. Its own measurement, when it has one, stands.
        for m in declared_rows:
            entry = same[m]
            base = rows[entry['model']]
            carries = entry.get('carries')
            for a in AXES:
                if carries is not None and a not in carries:
                    continue        # a category the row's channel does not share with the model behind it stays missing
                for c, rec in base['comps'][a].items():
                    if c in rows[m]['comps'][a]:
                        continue
                    rows[m]['comps'][a][c] = {
                        'value': rec['value'], 'sources': {}, 'status': 'estimated',
                        'estimate': {'method': 'same_model', 'base_model': entry['model'], 'base_status': rec['status'],
                                     **{k: entry[k] for k in ('kind', 'statement', 'url', 'as_of') if entry.get(k)}}}
        # Pessimistic unknowns for partial categories (round 8 & round 11, user): a type score that remains neither
        # measured nor estimated counts as 0, so a single board cannot carry a category for a model nothing else
        # can calibrate.
        for r in rows.values():
            r['axes'] = {}
            for a, comps in AXES.items():
                present = [c for c in comps if c in r['comps'][a]]
                measured = [c for c in present if r['comps'][a][c]['status'] == 'measured']
                denom = [c for c in comps if c in present or c in expected[a]]
                missing = [c for c in denom if c not in present]
                status = ('missing' if not present else 'partial' if missing
                          else 'measured' if len(measured) == len(denom) else 'estimated')
                vals = [r['comps'][a][c]['value'] if c in r['comps'][a] else 0.0 for c in denom] if present else []
                r['axes'][a] = {'value': mean(vals), 'status': status,
                                'components': r['comps'][a], 'missing_components': missing}
                if status == 'missing':
                    r['axes'][a].update(value=0.0, flag='missing',
                                        reason='no trusted source and no tier to estimate its type scores from')
        self.apply_overrides(rows)
        fit_doc = {f'{a}.{c}~tier': {k: round(v, 4) for k, v in f.items()} for (a, c), f in fits.items() if f}
        return rows, tier, fit_doc, rejected

    def apply_overrides(self, rows):
        for o in self.overrides:
            if o.get('model') in rows and o.get('axis') in AXES and o.get('as_of') and isinstance(o.get('value'), (int, float)):
                ax = rows[o['model']]['axes'][o['axis']]
                ax.update(public_value=ax['value'], value=float(o['value']), status='user_override',
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
    fit, why = Scorer.fit(pairs)
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
    indep = {a: sorted({s for c in (ax.get('components') or {}).values() if c.get('status') == 'measured'
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
    rows, tier, fits, rejected = scorer.score()
    usd = usd_community_mix(scorer, rows, meas)
    convert_usd(usd, rows, scorer, meas, acc, tier)
    caps, cap_errors = capacity(meas, plans, usd, now)
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
                                           'vision_tools >= 2 (mode B)'})
        fm = pick_fast_mode(m, scorer.trusted(r['fields'].get('fast_mode')))
        if fm['status'] == 'missing':
            model_errors.append({'model': m, 'field': 'fast_mode', 'reason': 'no_cited_source', 'level': 'yellow',
                                 'effect': 'fast mode is not priced or enabled from a policy multiplier on the five-axis path'})
        for a, ax in r['axes'].items():
            if ax.get('flag') == 'missing':
                model_errors.append({'model': m, 'field': a, 'reason': 'no_trusted_source', 'level': 'red_if_demanded',
                                     'effect': 'scores 0; a task that demands this axis leaves automatic routing for the model'})
            elif ax.get('missing_components'):
                model_errors.append({'model': m, 'field': f"{a}.{','.join(ax['missing_components'])}",
                                     'reason': 'component_unmeasured_no_fit', 'level': 'yellow',
                                     'effect': 'missing components count as 0 in the axis mean (pessimistic partial axis)'})
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
        models[m] = {'axes': r['axes'], 'scores': {a: ax['value'] for a, ax in r['axes'].items()},
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
           'capacity_errors': cap_errors, 'source_ages': source_ages(meas, now),
           'usd_checks': [{**h, 'usd_per_hour': (usd.get(h['model']) or {}).get('value'),
                           'community_over_usd': (round(h['value'] / usd[h['model']]['value'], 2)
                                                    if (usd.get(h['model']) or {}).get('value') else None)}
                          for h in (meas.get('capacity') or {}).get('hourly_checks') or ()],
           'anchor': {'model': measurements.ANCHOR, 'value': measurements.ANCHOR_VALUE},
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
        cells = ' '.join(f"{r['scores'][a]:>9.2f}{mark.get(r['axes'][a]['status'], '!' if r['axes'][a].get('flag') else ' ')}"
                         for a in AXES)
        print(f"{m:28s} {u if u is not None else '-':>7}  {cells}")
    print('~ estimated through the tier  u user override  ! missing (0)')
    print('tier sources:', doc['tier_sources'])
    for fam, plans in doc['capacity'].items():
        for plan, wins in plans.items():
            print(f'capacity {fam}/{plan}: ' + ', '.join(
                f"{w}={r['capacity_usd'] if r['capacity_usd'] is not None else '-'}({r['status']}, rung {r['rung']})"
                for w, r in wins.items()))


if __name__ == '__main__':
    sys.exit(main())
