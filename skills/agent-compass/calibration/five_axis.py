"""Five-axis scores, usd_per_hour, duration and plan capacity from `references/public-measurements.json`.

`python -X utf8 five_axis.py [--distrust SOURCE ...] [--source-priority A,B,...] [--user-overrides PATH] [--out PATH]`

Writes `references/five-axis-scores.json`, which the router reads only when `scoring.five_axis.enabled` is set (off by
default; see allocator.five_axis_overlay). Round 047, round 2 (user decisions of 2026-11-09):

- No scalar tier. The four capability axes (divergence, global_rigor, module_precision, vision_tools) are the only
  model ability (round 7: the only dimension system; no difficulty floors, the router fits the scores to the task's
  demand continuously against targets of the anchor value x demand / 3, written here as `anchor`).
- Each axis is the mean of its components, each component the mean of its trusted sources (`measurements.py` converts
  every source the same way, relative to the anchor model: 0.8 x s / s_anchor, so a new top model moves nothing). A
  component no trusted source measures is backfilled from public data, a linear fit of the component on the AA
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
  give cost and time for the model; a model with neither is mode B. `benchmark` (the AA/OpenRouter merge above) and
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


def pick_duration(usd, info):
    used = set(usd.get('used') or ())
    if used & set(TIME_BASED):
        return {'value': 1.0, 'source': 'usd merge includes ' + ', '.join(sorted(used & set(TIME_BASED))),
                'kind': 'benchmark', 'method': 'the hourly figure already prices the model pace (user, 2026-11-09)'}
    shipped = info.get('duration_multiplier')
    if shipped is not None:
        return {'value': float(shipped), 'source': 'accounts.duration_multiplier', 'kind': 'community',
                'method': 'shipped duration_multiplier, kept: no time-based usd source for this model'}
    return {'value': 1.0, 'source': None, 'kind': None, 'method': 'no speed data: neutral 1.0'}


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

    @staticmethod
    def fit(points):
        if len(points) < MIN_FIT_POINTS:
            return None
        x, y = np.array([p[0] for p in points]), np.array([p[1] for p in points])
        b, a = np.polyfit(x, y, 1)
        if b <= 0:      # ability falling with the index is not a usable predictor
            return None
        return {'a': float(a), 'b': float(b), 'r': float(np.corrcoef(x, y)[0, 1]), 'n': len(points),
                'y_min': float(y.min()), 'y_max': float(y.max())}

    @staticmethod
    def predict(f, x):
        """The fit at x, kept inside the range its measured points reach (round 6: no extrapolation past them)."""
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
        # Component backfill: every unmeasured component from the component's fit on the AA index (over every measured
        # model that has it), so a partial axis does not average only its measured components.
        for r in rows.values():
            for a, comps in AXES.items():
                for c in comps:
                    name, x, f = self.backfill(fits, (a, c), r['preds'])
                    if c not in r['comps'][a] and f:
                        r['comps'][a][c] = {'value': self.predict(f, x), 'sources': {},
                                            'status': 'backfilled', 'backfill': {'predictor': name, 'x': x}}
        # Pessimistic unknowns for partial axes (round 8, user): any expected component of the axis that remains
        # unmeasured and has no backfill fit counts as 0, so a single board (e.g. Alignment alone) cannot carry the axis.
        anchor_comps = (rows.get(measurements.ANCHOR) or {}).get('comps') or {}
        expected = {a: [c for c in comps if c in (anchor_comps.get(a) or {})
                        or any(fits.get((a, c, n)) for n in PREDICTORS)] or list(comps)
                    for a, comps in AXES.items()}
        for r in rows.values():
            r['axes'] = {}
            for a, comps in AXES.items():
                present = [c for c in comps if c in r['comps'][a]]
                measured = [c for c in present if r['comps'][a][c]['status'] == 'measured']
                denom = [c for c in comps if c in present or c in expected[a]]
                missing = [c for c in denom if c not in present]
                status = ('missing' if not present else 'measured' if len(measured) == len(denom)
                          else 'partial' if (measured or missing) else 'backfilled')
                vals = [r['comps'][a][c]['value'] if c in r['comps'][a] else 0.0 for c in denom] if present else []
                r['axes'][a] = {'value': mean(vals), 'status': status,
                                'components': r['comps'][a], 'missing_components': missing}
        # Axis backfill for a model with no component on the axis: the axis fit on the index.
        for a in AXES:
            for name in PREDICTORS:
                pts = [(r['preds'][name], r['axes'][a]['value']) for r in rows.values()
                       if r['axes'][a]['status'] in ('measured', 'partial') and name in r['preds']]
                fits[(a, None, name)] = self.fit(pts)
        for m, r in rows.items():
            for a, ax in r['axes'].items():
                if ax['status'] != 'missing':
                    continue
                name, x, f = self.backfill(fits, (a, None), r['preds'])
                if f:
                    ax.update(value=self.predict(f, x), status='backfilled',
                              backfill={'predictor': name, 'x': x, 'level': 'axis'})
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
    and time. When `--source-priority` is given, it falls through `[*priority, 'arena.agent_sessions', 'derived.community_mix']`
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


def legacy(info):
    return {k: info.get(k) for k in ('tier', 'usd_per_hour', 'duration_multiplier', 'subjective', 'capabilities', 'visual')}


def build(meas, acc, plans, distrust=(), meas_bytes=b'', priority=None, overrides=(), usd_basis='community_mix', now=None):
    scorer = Scorer(meas, acc, distrust, overrides)
    rows, fits = scorer.score()
    usd = USD_BASES[usd_basis]['fn'](scorer, rows, meas, priority)
    caps, cap_errors = capacity(meas, plans, usd, now, usd_basis)
    models, errors = {}, []
    for m, r in rows.items():
        model_errors = []
        if usd[m]['status'] == 'missing':
            model_errors.append({'model': m, 'field': 'usd_per_hour', 'reason': 'no_cited_source', 'level': 'red_if_metered',
                                 'effect': 'out of automatic routing on a metered account (billed or quota windows); a named '
                                           'dispatch runs with its cost unpriced; free and unmetered accounts are unaffected'})
        for a, ax in r['axes'].items():
            if ax.get('flag') == 'missing':
                model_errors.append({'model': m, 'field': a, 'reason': 'no_trusted_source', 'level': 'red_if_demanded',
                                     'effect': 'scores 0; a task that demands this axis leaves automatic routing for the model'})
            elif ax.get('missing_components') and ax['status'] != 'inherited':
                model_errors.append({'model': m, 'field': f"{a}.{','.join(ax['missing_components'])}",
                                     'reason': 'component_unmeasured_no_fit', 'level': 'yellow',
                                     'effect': 'missing components count as 0 in the axis mean (pessimistic partial axis)'})
        errors += model_errors
        idx = scorer.trusted(r['fields'].get('intelligence_index'))
        u = usd[m]['value']
        models[m] = {'axes': r['axes'], 'scores': {a: ax['value'] for a, ax in r['axes'].items()},
                     'missing_axes': [a for a, ax in r['axes'].items() if ax.get('flag') == 'missing'],
                     'usd_per_hour': usd[m], 'duration_multiplier': pick_duration(usd[m], acc['models'][m]),
                     'intelligence_index': idx[0]['value'] if idx else None,
                     'economy_static': round(1 / (1 + u / 15), 3) if u is not None else None,
                     'legacy_public_tier': legacy_tier(scorer.trusted(r['fields'].get('tier'))),
                     'data_errors': model_errors, 'legacy': legacy(acc['models'][m])}
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
        mark = {'backfilled': '*', 'inherited': '^', 'user_override': 'u'}
        cells = ' '.join(f"{r['scores'][a]:>9.2f}{mark.get(r['axes'][a]['status'], '!' if r['axes'][a].get('flag') else ' ')}"
                         for a in AXES)
        print(f"{m:28s} {u if u is not None else '-':>7}  {cells}")
    print('* backfilled  ^ inherited (vendor statement)  u user override  ! missing (0)')
    for fam, plans in doc['capacity'].items():
        for plan, wins in plans.items():
            print(f'capacity {fam}/{plan}: ' + ', '.join(
                f"{w}={r['capacity_usd'] if r['capacity_usd'] is not None else '-'}({r['status']}, rung {r['rung']})"
                for w, r in wins.items()))


if __name__ == '__main__':
    sys.exit(main())
