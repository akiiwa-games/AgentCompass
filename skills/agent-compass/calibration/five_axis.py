"""Five-axis scores, tier, usd_per_hour and plan capacity from `references/public-measurements.json`.

`python -X utf8 five_axis.py [--distrust SOURCE ...] [--out PATH]`

Writes `references/five-axis-scores.json`, which the router reads only when `scoring.five_axis.enabled` is set (off by
default; see allocator.five_axis_overlay). Nothing here falls back to a default:

- tier: the public fit when one exists, else the user's tier (source `user`), else missing. A missing tier takes the
  model out of automatic routing (an explicit --model still dispatches it, with a warning).
- usd_per_hour: AA cost per task x 3600 / time per task at High (else the effort-less listing, else the median of the
  listed efforts; the hourly cost is nearly flat across efforts). Missing on a billed account takes the model out of
  automatic routing; free and unmetered accounts need no price.
- The four capability axes (divergence, global_rigor, module_precision, vision_tools): each axis is the mean of its
  components, each component the mean of its trusted sources (`measurements.py` converts every source to 0..1 the same
  way). An axis with no component is backfilled from public data: a linear fit of the axis on the AA Intelligence Index,
  else on a public tier, over the models that have the axis measured. An axis still empty scores 0 and is flagged yellow.
- economy is not a model property: the router prices it at run time (usd_per_hour against the window's capacity and
  live quota). `economy_static` is the tray's static view, 1 / (1 + usd_per_hour / 15), free = 1.
- Plan capacity is counted in hours of a reference model. A community figure is kept only with a public URL; none of the
  shipped figures has one, so every window is `unknown` (the router then reads live percentages only). The
  `comparison_only` block converts each shipped capacity to the AA scale (shipped USD / shipped reference usd x AA
  reference usd) so the parity run can show what the usd change alone does; it is not a public value.
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
SCHEMA = 'agent-compass.five-axis-scores/1'
AXES = measurements.AXES
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
# Window -> the model whose hours count the window's capacity (the plan's own flagship on that pool).
REFERENCE_MODELS = {
    'claude': {'five_hour': 'claude-opus-5-5', 'seven_day': 'claude-opus-5-5', 'weekly_fable': 'claude-fable-5-1'},
    'codex': {'usage': 'gpt-6.1-sol', 'five_hour': 'gpt-6.1-sol'},
    'pi': {'usage': 'gpt-6.1-sol'},
    'antigravity': {'gemini-weekly': 'gemini-4-argon-high', 'gemini-5h': 'gemini-4-argon-high',
                    '3p-weekly': 'claude-opus-5-5', '3p-5h': 'claude-opus-5-5'},
}
PUBLIC_TIER_SOURCES = ('lmarena.tier_fit', 'secondary.tier_fit')


def mean(xs):
    return round(float(np.mean(xs)), 4) if xs else None


def pick_tier(entries):
    public = [e for e in entries if e['source'] in PUBLIC_TIER_SOURCES]
    user = [e for e in entries if e['source'] == 'user']
    chosen = public[0] if public else (user[0] if user else None)
    if chosen is None:
        return {'value': None, 'status': 'missing', 'source': None}
    return {'value': chosen['value'], 'status': 'ok', 'source': chosen['source'], 'as_of': chosen.get('as_of'),
            'method': chosen.get('method'), 'alternatives': [{'source': e['source'], 'value': e['value']} for e in entries if e is not chosen]}


def pick_usd(entries):
    by_effort = {e['effort']: e['value'] for e in entries}
    if not by_effort:
        return {'value': None, 'status': 'missing', 'by_effort': {}}
    if 'high' in by_effort:
        value, how = by_effort['high'], 'High listing'
    elif 'default' in by_effort:
        value, how = by_effort['default'], 'the only listing (no effort named)'
    else:
        listed = {k: v for k, v in by_effort.items() if k in EFFORTS} or by_effort
        value, how = round(statistics.median(listed.values()), 3), f'median of the listed efforts {sorted(listed)} (no High listing)'
    e = entries[0]
    return {'value': value, 'status': 'ok', 'source': e['source'], 'as_of': e.get('as_of'), 'fetched_at': e.get('fetched_at'),
            'url': e.get('url'), 'method': f'cost per task x 3600 / time per task, {how}', 'by_effort': by_effort}


class Scorer:
    def __init__(self, meas, acc, distrust=()):
        self.meas, self.acc = meas, acc
        self.distrust = set(distrust)
        self.models = {k: v for k, v in acc['models'].items() if isinstance(v, dict)}

    def trusted(self, entries):
        return [e for e in entries or () if e['source'] not in self.distrust]

    def axis(self, fields, axis):
        comps = {}
        for e in self.trusted(fields.get(axis)):
            comps.setdefault(e['component'], {})[e['source']] = e['value']
        out = {c: {'value': mean(list(srcs.values())), 'sources': srcs} for c, srcs in comps.items()}
        present = [c for c in AXES[axis] if c in out]
        value = mean([out[c]['value'] for c in present])
        status = 'missing' if not present else ('measured' if len(present) == len(AXES[axis]) else 'partial')
        return {'value': value, 'status': status, 'components': out, 'missing_components': [c for c in AXES[axis] if c not in out]}

    def predictor(self, fields, tier):
        idx = self.trusted(fields.get('intelligence_index'))
        if idx:
            return 'aa.index', idx[0]['value']
        if tier['status'] == 'ok' and tier['source'] in PUBLIC_TIER_SOURCES:
            return 'tier', tier['value']
        return None, None

    def score(self):
        rows = {}
        for m in self.models:
            fields = self.meas['models'].get(m) or {}
            tier = pick_tier(self.trusted(fields.get('tier')))
            usd = pick_usd(self.trusted(fields.get('usd_per_hour')))
            rows[m] = {'tier': tier, 'usd_per_hour': usd, 'axes': {a: self.axis(fields, a) for a in AXES},
                       'predictor': self.predictor(fields, tier)}
        fits = {}
        for a in AXES:
            for name in ('aa.index', 'tier'):
                pts = [(r['predictor'][1], r['axes'][a]['value']) for r in rows.values()
                       if r['axes'][a]['status'] != 'missing' and r['predictor'][0] == name]
                if len(pts) >= 5:
                    x, y = np.array([p[0] for p in pts]), np.array([p[1] for p in pts])
                    b, c = np.polyfit(x, y, 1)
                    fits[(a, name)] = {'a': float(c), 'b': float(b), 'r': float(np.corrcoef(x, y)[0, 1]), 'n': len(pts)}
        for m, r in rows.items():
            name, x = r['predictor']
            for a, ax in r['axes'].items():
                if ax['status'] != 'missing':
                    continue
                fit = fits.get((a, name)) if name else None
                if fit:
                    ax.update(value=round(float(np.clip(fit['a'] + fit['b'] * x, 0, 1)), 4), status='backfilled',
                              backfill={'predictor': name, 'x': x, **{k: round(v, 4) for k, v in fit.items()}})
                else:
                    ax.update(value=0.0, flag='yellow', reason='no trusted source and no public predictor to backfill from')
        return rows, fits


def capacity(acc, plans, scores):
    legacy_usd = {m: (acc['models'].get(m) or {}).get('usd_per_hour') for fam in REFERENCE_MODELS.values() for m in fam.values()}
    factors, out = {}, {}
    for fam, windows in REFERENCE_MODELS.items():
        for wid, ref in windows.items():
            new = scores[ref]['usd_per_hour']['value']
            old = legacy_usd.get(ref)
            factors.setdefault(fam, {})[wid] = {'reference_model': ref, 'legacy_usd_per_hour': old, 'aa_usd_per_hour': new,
                                                'factor': round(new / old, 4) if new and old else None}
    for fam, table in plans.items():
        if not isinstance(table, dict):
            continue
        for plan, spec in table.items():
            if not isinstance(spec, dict) or not isinstance(spec.get('capacity_usd'), dict):
                continue
            for wid, cap in spec['capacity_usd'].items():
                f = (factors.get(fam) or {}).get(wid) or {}
                old = f.get('legacy_usd_per_hour')
                ref_hours = round(cap / old, 1) if old else None
                out.setdefault(fam, {}).setdefault(plan, {})[wid] = {
                    'unit': 'reference_model_hours', 'reference_model': f.get('reference_model'), 'ref_hours': None,
                    'status': 'unknown', 'reason': 'no public URL for the shipped figure (community or local measurement)',
                    'legacy_capacity_usd': cap,
                    'comparison_only': {'ref_hours': ref_hours,
                                        'capacity_usd_aa': round(ref_hours * f['aa_usd_per_hour'], 1)
                                        if ref_hours and f.get('aa_usd_per_hour') else None}}
    return factors, out


def legacy(info):
    return {k: info.get(k) for k in ('tier', 'usd_per_hour', 'duration_multiplier', 'subjective', 'capabilities', 'visual')}


def build(meas, acc, plans, distrust=(), meas_bytes=b''):
    rows, fits = Scorer(meas, acc, distrust).score()
    factors, caps = capacity(acc, plans, rows)
    models, errors = {}, []
    for m, r in rows.items():
        usd = r['usd_per_hour']['value']
        model_errors = []
        if r['tier']['status'] == 'missing':
            model_errors.append({'model': m, 'field': 'tier', 'reason': 'no_trusted_source', 'level': 'red',
                                 'effect': 'out of automatic routing; an explicit --model still dispatches it, with a warning'})
        if r['usd_per_hour']['status'] == 'missing':
            model_errors.append({'model': m, 'field': 'usd_per_hour', 'reason': 'no_trusted_source', 'level': 'red_if_billed',
                                 'effect': 'out of automatic routing on a billed account; free and unmetered accounts are unaffected'})
        for a, ax in r['axes'].items():
            if ax.get('flag') == 'yellow':
                model_errors.append({'model': m, 'field': a, 'reason': 'no_trusted_source', 'level': 'yellow',
                                     'effect': 'scores 0 on this axis'})
        errors += model_errors
        models[m] = {'tier': r['tier'], 'usd_per_hour': r['usd_per_hour'],
                     'axes': {a: {k: v for k, v in ax.items()} for a, ax in r['axes'].items()},
                     'scores': {a: ax['value'] for a, ax in r['axes'].items()},
                     'economy_static': round(1 / (1 + usd / 15), 3) if usd is not None else None,
                     'data_errors': model_errors, 'legacy': legacy(acc['models'][m])}
    return {'schema': SCHEMA, 'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'measurements_sha256': hashlib.sha256(meas_bytes).hexdigest() if meas_bytes else None,
            'distrusted_sources': sorted(distrust),
            'method': __doc__.strip().splitlines()[0],
            'backfill_fits': {f'{a}~{n}': {k: round(v, 4) for k, v in f.items()} for (a, n), f in fits.items()},
            'capacity_conversion': factors, 'capacity': caps,
            'models': models, 'data_errors': errors}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--measurements', default=str(measurements.OUT))
    ap.add_argument('--distrust', action='append', default=[], help='a source id to leave out (repeatable)')
    ap.add_argument('--out', default=str(OUT))
    args = ap.parse_args()
    raw = Path(args.measurements).read_bytes()
    doc = build(json.loads(raw), sources.accounts(), json.loads(PLANS.read_text(encoding='utf-8')), args.distrust, raw)
    Path(args.out).write_text(json.dumps(doc, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    print(f'{len(doc["models"])} models -> {args.out}')
    print(f'{"model":28s} {"tier":>5} {"usd/h":>7}  ' + ' '.join(f'{a[:10]:>10}' for a in AXES))
    for m, r in doc['models'].items():
        usd = r['usd_per_hour']['value']
        cells = ' '.join(f"{r['scores'][a]:>9.2f}{'*' if r['axes'][a]['status'] == 'backfilled' else ('!' if r['axes'][a].get('flag') else ' ')}"
                         for a in AXES)
        print(f"{m:28s} {r['tier']['value'] if r['tier']['value'] is not None else '-':>5} {usd if usd is not None else '-':>7}  {cells}")
    print('* backfilled   ! no source (0, yellow)')


if __name__ == '__main__':
    sys.exit(main())
