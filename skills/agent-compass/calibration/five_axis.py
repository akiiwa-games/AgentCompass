"""Five-axis scores, usd_per_hour, duration, plan capacity and difficulty floors from `references/public-measurements.json`.

`python -X utf8 five_axis.py [--distrust SOURCE ...] [--source-priority A,B,...] [--user-overrides PATH] [--out PATH]`

Writes `references/five-axis-scores.json`, which the router reads only when `scoring.five_axis.enabled` is set (off by
default; see allocator.five_axis_overlay). Round 047, round 2 (user decisions of 2026-11-09):

- No scalar tier. The four capability axes (divergence, global_rigor, module_precision, vision_tools) are the only
  model ability; difficulty floors are requirements on the axes a task demands (`floors`, calibrated below).
- Each axis is the mean of its components, each component the mean of its trusted sources (`measurements.py` converts
  every source the same way, relative to the anchor model: 0.8 x s / s_anchor, so a new top model moves nothing). A
  component no trusted source measures is backfilled from public data, a linear fit of the component on the AA
  Intelligence Index over the models that have it, else on the public LMArena tier fit; a fit needs MIN_FIT_POINTS
  measured points and a positive slope (review F1: one public point is not a fit). Without a fit the component stays
  missing and the axis averages the components it has (a yellow data note). An axis with no component at all is
  backfilled the same way on the axis; still empty, it scores 0 and is `missing`. A task that demands a missing axis
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
- Plan capacity stays the packaged plan-templates figure (list-price USD of usage, with its provenance and basis);
  its hours of each window's reference model are that figure over the reference model's cited usd (review F2: not the
  shipped local sample). The router reads the USD figure unchanged.
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
TIME_BASED = ('aa.cost_time', 'openrouter.cost_time')
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
PREDICTORS = ('aa.index', 'tier')      # backfill predictors in order of preference
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
        """{predictor: x} for the public predictors the model has: the AA index, the public LMArena tier fit."""
        out = {}
        idx = self.trusted(fields.get('intelligence_index'))
        if idx:
            out['aa.index'] = idx[0]['value']
        tier = [e for e in self.trusted(fields.get('tier')) if e['source'] in PUBLIC_TIER_SOURCES]
        if tier:
            out['tier'] = tier[0]['value']
        return out

    @staticmethod
    def backfill(fits, key, preds):
        """(predictor, x, fit) for the first predictor, AA index then tier, the model has and that has a usable fit."""
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
        return {'a': float(a), 'b': float(b), 'r': float(np.corrcoef(x, y)[0, 1]), 'n': len(points)}

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
        # Component backfill: every unmeasured component from the component's fit on the AA index, else on the public
        # LMArena tier fit (each fit over every measured model that has that predictor), so a partial axis does not
        # average only its measured components.
        for r in rows.values():
            for a, comps in AXES.items():
                for c in comps:
                    name, x, f = self.backfill(fits, (a, c), r['preds'])
                    if c not in r['comps'][a] and f:
                        r['comps'][a][c] = {'value': round(float(np.clip(f['a'] + f['b'] * x, 0, 1)), 4), 'sources': {},
                                            'status': 'backfilled', 'backfill': {'predictor': name, 'x': x}}
        for r in rows.values():
            r['axes'] = {}
            for a, comps in AXES.items():
                present = [c for c in comps if c in r['comps'][a]]
                measured = [c for c in present if r['comps'][a][c]['status'] == 'measured']
                status = ('missing' if not present else 'measured' if len(measured) == len(comps)
                          else 'partial' if measured else 'backfilled')
                r['axes'][a] = {'value': mean([r['comps'][a][c]['value'] for c in present]), 'status': status,
                                'components': r['comps'][a], 'missing_components': [c for c in comps if c not in present]}
        # Axis backfill for a model with no component on the axis: the axis fit on the index, else on the public tier.
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
                    ax.update(value=round(float(np.clip(f['a'] + f['b'] * x, 0, 1)), 4), status='backfilled',
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


def capacity(acc, plans, usd):
    """Packaged window capacity with its hours of the window's reference model at the cited reference usd."""
    factors, out = {}, {}
    for fam, windows in REFERENCE_MODELS.items():
        for wid, ref in windows.items():
            factors.setdefault(fam, {})[wid] = {'reference_model': ref, 'cited_usd_per_hour': usd[ref]['value'], 'factor': 1.0,
                                                'note': 'capacity_usd is list-price usage USD, the scale of the cited usd'}
    for fam, table in plans.items():
        if not isinstance(table, dict):
            continue
        for plan, spec in table.items():
            if not isinstance(spec, dict) or not isinstance(spec.get('capacity_usd'), dict):
                continue
            for wid, cap in spec['capacity_usd'].items():
                f = (factors.get(fam) or {}).get(wid) or {}
                ref_usd = f.get('cited_usd_per_hour')
                out.setdefault(fam, {}).setdefault(plan, {})[wid] = {
                    'unit': 'usd', 'capacity_usd': cap, 'reference_model': f.get('reference_model'),
                    'ref_hours': round(cap / ref_usd, 1) if ref_usd else None,
                    'status': 'ok' if ref_usd else 'reference usd missing (hours unknown; the USD capacity still applies)',
                    'source': {'kind': 'community', 'provenance': spec.get('provenance'), 'basis': spec.get('basis'),
                               'url': None, 'as_of': acc.get('reviewed_at')}}
    return factors, out


def legacy(info):
    return {k: info.get(k) for k in ('tier', 'usd_per_hour', 'duration_multiplier', 'subjective', 'capabilities', 'visual')}


def build(meas, acc, plans, distrust=(), meas_bytes=b'', priority=None, overrides=(), floors=True):
    scorer = Scorer(meas, acc, distrust, overrides)
    rows, fits = scorer.score()
    usd = {m: pick_usd(scorer.trusted(r['fields'].get('usd_per_hour')), priority) for m, r in rows.items()}
    factors, caps = capacity(acc, plans, usd)
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
                                     'effect': 'the axis averages the components it has'})
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
           'backfill_fits': fits, 'capacity_conversion': factors, 'capacity': caps,
           'models': models, 'data_errors': errors}
    if floors:
        import five_axis_floors
        doc['floors'] = five_axis_floors.calibrate(doc, acc)
    return doc


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--measurements', default=str(measurements.OUT))
    ap.add_argument('--distrust', action='append', default=[], help='a source id to leave out of every field (repeatable)')
    ap.add_argument('--source-priority', default='', help='comma-separated usd sources; the first with a value wins over the median')
    ap.add_argument('--user-overrides', help='JSON list of dated {model, axis, value, as_of, note} overrides (source `user`)')
    ap.add_argument('--out', default=str(OUT))
    args = ap.parse_args()
    raw = Path(args.measurements).read_bytes()
    overrides = json.loads(Path(args.user_overrides).read_text(encoding='utf-8')) if args.user_overrides else []
    priority = [s.strip() for s in args.source_priority.split(',') if s.strip()]
    doc = build(json.loads(raw), sources.accounts(), json.loads(PLANS.read_text(encoding='utf-8')), args.distrust, raw,
                priority, overrides)
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
    fl = doc.get('floors') or {}
    print('floors:', json.dumps({k: fl.get(k) for k in ('anchor', 'ratio', 'critical_ratio', 'axis_ratio', 'agreement')}))


if __name__ == '__main__':
    sys.exit(main())
