"""Leave-one-out error of the estimation methods, the evidence behind accounts.json `estimation_order` (read-only).

    python -X utf8 audit/estimation_loo.py [--references DIR] [--out cells.json]

Builds the five-axis scores in memory from the shipped references (nothing is written unless `--out` names a file) and,
for every measured High cell, hides it and estimates it again with each method the build declares, from the rest of the
fleet: the same-series sibling shifted along the tier fit (by the declared version distance, series.nearest: a tied
group gives its median), the tier fit, the downward derivation from the larger model (`tier_down`), the size step from
the same or the nearest version, and the model's own ability median; each effort cell measured with High is estimated
again by the panel effort step and, at an edge effort, by the fleet's edge step. It prints per method the number of
cells, the median and 90th percentile absolute error and the mean error (panel z), and the sibling against the tier fit
on the same cells by version distance, the comparison `estimation_order.sibling_first_up_to_distance` rests on.

The release that re-measures this updates `estimation_order.evidence` by hand; the refresh never reorders by itself.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills' / 'agent-compass' / 'calibration'))
sys.path.insert(0, str(ROOT / 'skills' / 'agent-compass' / 'scripts'))
import five_axis as fa  # noqa: E402
import series  # noqa: E402

REFERENCES = ROOT / 'skills' / 'agent-compass' / 'references'


def fit_without(scorer, facets, rows, key, m):
    """The facet's tier fit from the fleet without `m`."""
    pts = [(scorer.direct_tier[x], facets[x][key]['value']) for x in rows if x != m and x in scorer.direct_tier
           and (facets[x].get(key) or {}).get('value') is not None and facets[x][key]['status'] != 'estimated']
    return scorer.fit(pts, require_skill=True)[0]


def cell_estimates(scorer, facets, keys, rows, declared, tier, m, key):
    """{method: {'value', ...}} for the hidden High cell (m, key)."""
    out = {}
    fits = {key: fit_without(scorer, facets, rows, key, m)}
    if tier[m]['value'] is not None and m not in declared:
        usable = [b for b in scorer.sibs.get(m, ()) if b != m
                  and (facets[b].get(key) or {}).get('status', '').startswith('measured')
                  and (facets[b].get(key) or {}).get('value') is not None and tier[b]['value'] is not None]
        groups = series.nearest(m, usable, scorer.sibs.get(m, ()), scorer.versions, scorer.small, scorer.same)
        if groups:
            d, group = groups[0]
            e = scorer.group_estimate(m, group, d, key, facets, fits, {}, tier)
            out['sibling_shift'] = {'value': e['value'], 'base': group, 'distance': d}
        if fits[key]:
            out['tier_fit'] = {'value': scorer.estimate(m, None, key, facets, fits, {}, tier)['value']}
    rel = scorer.size_of.get(m)
    if rel:
        big, version = rel['model'], rel['version']
        theirs = facets[big].get(key)
        if theirs and theirs.get('value') is not None:
            own = facets[m]
            if version == 'same' and fa.rises(key):
                shared = [k for k in keys if k != key and fa.rises(k) and (own.get(k) or {}).get('status') in fa.TARGET_STATUSES
                          and (facets[big].get(k) or {}).get('status') in fa.TARGET_STATUSES]
                if shared:
                    gap = float(np.mean([own[k]['value'] - facets[big][k]['value'] for k in shared]))
                    out['tier_down'] = {'value': theirs['value'] + min(gap, 0.0), 'n': len(shared)}
            hidden = facets[m].pop(key)
            try:
                table = scorer.size_step_table(facets, keys).get('.'.join(key))
            finally:
                facets[m][key] = hidden
            if table is not None:
                move = min(table['step'], 0.0) if fa.rises(key) else table['step']
                out[f'size_step_{version}'] = {'value': theirs['value'] + move, 'n': table['n']}
    if fa.rises(key):
        have = [k for k in keys if k != key and fa.rises(k) and (facets[m].get(k) or {}).get('value') is not None]
        if len(have) >= fa.MIN_OWN_FACETS:
            out['own_ability_median'] = {'value': float(np.median([facets[m][k]['value'] for k in have])), 'n': len(have)}
    return out


def effort_cells(scorer, facets, keys, rows):
    """Cells measured with High at another effort, estimated by the panel effort step and the fleet edge step."""
    panel = [p for p in (scorer.meas.get('panel') or {}).get('models') or () if p in rows]
    edges = {'low': 'medium', 'max': 'xhigh'}      # an edge effort and the effort next to it
    cells = []
    for m in rows:
        for key in keys:
            rec = facets[m].get(key) or {}
            have = set(rec.get('measured_efforts') or ())
            if 'high' not in have:
                continue
            by = rec['by_effort']
            for e in sorted(have - {'high', 'none'}):
                xs = [facets[p][key]['by_effort'][e] - facets[p][key]['by_effort']['high'] for p in panel if p != m
                      and {e, 'high'} <= set((facets[p].get(key) or {}).get('measured_efforts') or ())]
                if len(xs) >= 2:
                    cells.append({'model': m, 'facet': '.'.join(key), 'effort': e, 'truth': by[e],
                                  'est': {'panel_effort_step': {'value': by['high'] + float(np.median(xs)), 'n': len(xs)}}})
            for e, near in edges.items():
                if {e, near} <= have:
                    xs = [fs[key]['by_effort'][e] - fs[key]['by_effort'][near] for x, fs in facets.items() if x != m
                          and {e, near} <= set((fs.get(key) or {}).get('measured_efforts') or ())]
                    if len(xs) >= 2:
                        cells.append({'model': m, 'facet': '.'.join(key), 'effort': e, 'truth': by[e],
                                      'est': {'fleet_edge_step': {'value': by[near] + float(np.median(xs)), 'n': len(xs)}}})
    return cells


@contextlib.contextmanager
def instrumented(cells):
    """Scorer.tier_records and Scorer.complete wrapped for one in-memory build, restored afterwards."""
    orig_tier, orig_complete = fa.Scorer.tier_records, fa.Scorer.complete

    def tier_records(self, rows):
        self._loo_tier = orig_tier(self, rows)
        return self._loo_tier

    def complete(self, facets, keys, rows, declared=()):
        for m in rows:
            for key in keys:
                rec = facets[m].get(key) or {}
                if (rec.get('estimate') or {}).get('method') or 'high' not in (rec.get('measured_efforts') or ()):
                    continue
                cells.append({'model': m, 'facet': '.'.join(key), 'effort': 'high', 'truth': rec['by_effort']['high'],
                              'est': cell_estimates(self, facets, keys, rows, declared, self._loo_tier, m, key)})
        cells.extend(effort_cells(self, facets, keys, rows))
        return orig_complete(self, facets, keys, rows, declared)

    fa.Scorer.tier_records, fa.Scorer.complete = tier_records, complete
    try:
        yield
    finally:
        fa.Scorer.tier_records, fa.Scorer.complete = orig_tier, orig_complete


def measure(references=REFERENCES):
    """The leave-one-out cells of the shipped references: [{model, facet, effort, truth, est: {method: {value, ...}}}]."""
    refs = Path(references)
    raw = (refs / 'public-measurements.json').read_bytes()
    shipped = json.loads((refs / 'five-axis-scores.json').read_text(encoding='utf-8-sig'))
    acc = json.loads((refs / 'accounts.json').read_text(encoding='utf-8-sig'))
    plans = json.loads((refs / 'plan-templates.json').read_text(encoding='utf-8-sig'))
    cells = []
    with instrumented(cells):
        fa.build(json.loads(raw.decode('utf-8-sig')), acc, plans, distrust=shipped['distrusted_sources'], meas_bytes=raw,
                 now=datetime.fromisoformat(shipped['generated_at']), previous=shipped)
    return cells


def method_name(method, e):
    if method != 'sibling_shift':
        return method
    d = e['distance']
    return f"sibling_shift d={'?' if d is None else d if d < 2 else '2+'}"


def summary(cells):
    """[(method, n, median |error|, p90 |error|, mean error)] sorted by method name."""
    errs = {}
    for c in cells:
        for method, e in c['est'].items():
            errs.setdefault(method_name(method, e), []).append(e['value'] - c['truth'])
    out = []
    for k in sorted(errs):
        a = np.array(errs[k])
        out.append((k, len(a), round(float(np.median(np.abs(a))), 3), round(float(np.percentile(np.abs(a), 90)), 3),
                    round(float(np.mean(a)), 3)))
    return out


def sibling_against_fit(cells):
    """[(distance 0 | 1 | '2+', n, median |error| sibling, median |error| tier fit)] on the cells both estimate."""
    out = []
    for d in (0, 1, 2):
        both = [c for c in cells if 'sibling_shift' in c['est'] and 'tier_fit' in c['est']
                and c['est']['sibling_shift']['distance'] is not None and min(c['est']['sibling_shift']['distance'], 2) == d]
        if both:
            out.append((d if d < 2 else '2+', len(both),
                        round(float(np.median([abs(c['est']['sibling_shift']['value'] - c['truth']) for c in both])), 3),
                        round(float(np.median([abs(c['est']['tier_fit']['value'] - c['truth']) for c in both])), 3)))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--references', default=str(REFERENCES))
    ap.add_argument('--out', help='write the cells as JSON to this file')
    args = ap.parse_args(argv)
    cells = measure(args.references)
    if args.out:
        Path(args.out).write_text(json.dumps(cells, indent=0), encoding='utf-8', newline='\n')
    print(f"{'method':24s} {'n':>5s} {'med|e|':>7s} {'p90|e|':>7s} {'bias':>7s}")
    for k, n, med, p90, bias in summary(cells):
        print(f'{k:24s} {n:5d} {med:7.3f} {p90:7.3f} {bias:7.3f}')
    print('\nsibling against tier fit on the same cells (median |error|):')
    for d, n, sib, fit in sibling_against_fit(cells):
        print(f'  distance {d}: n={n} sibling {sib:.3f}, tier fit {fit:.3f}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
