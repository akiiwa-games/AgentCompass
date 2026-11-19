"""Leave-one-out error of the estimation methods, the evidence behind accounts.json `estimation_order` (read-only).

    python -X utf8 audit/estimation_loo.py [--references DIR] [--out cells.json]

Builds the five-axis scores in memory from the shipped references (nothing is written unless `--out` names a file) and,
for every measured High cell, hides it and estimates it again with each method the build declares, from the rest of the
fleet: the same-series sibling shifted along the tier fit (by the declared version distance, series.nearest: a tied
group gives its median), the tier fit, the downward derivation from the larger model (`tier_down`), the size step from
the same or the nearest version, and the model's own ability median; each effort cell measured with High is estimated
again by the panel effort step and, at an edge effort, by the fleet's edge step. It prints per method the number of
cells, the median and 90th percentile absolute error and the mean error (panel z), and the sibling against the tier fit
on the same cells by version distance, the comparison `estimation_order.sibling_first_up_to_distance` rests on. Relatives
are those the build uses (series.groups: an ability facet the maker's same-size rows of every line, responsiveness its own
line first); each responsiveness cell is also estimated from the maker's other lines as the build does
(`Scorer.cross_line`: `cross_line_extrapolation` where the lines share two same-version pairs, else `cross_line_median`),
and the cross-line estimate against the tier fit on the cells both estimate is printed, the evidence of
`estimation_order.speed_cross_line_before_fit`. Time/Task, which has no tier fit, is scored apart (`cross_line_time_loo`):
every High time read on Artificial Analysis, hidden, against the median of the other lines' read High times at the nearest
version distance up to the declared one, and against the own line's nearest read relatives on the same cells (absolute
log error). The Pro line has no Time/Task or responsiveness reading to score its declared floor against.

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
        def usable(pool):
            return [b for b in pool.get(m, ()) if b != m
                    and (facets[b].get(key) or {}).get('status', '').startswith('measured')
                    and (facets[b].get(key) or {}).get('value') is not None and tier[b]['value'] is not None]
        ability = fa.rises(key)
        pool = scorer.sibs if ability else scorer.line_sibs            # as the build reads them (Scorer.complete)
        groups = series.nearest(m, usable(pool), pool.get(m, ()), scorer.versions, scorer.small, scorer.same,
                                scorer.line_of, scorer.order['sibling_first_up_to_distance'] if ability else None)
        if groups:
            d, group = groups[0]
            e = scorer.group_estimate(m, group, d, key, facets, fits, {}, tier)
            out['sibling_shift'] = {'value': e['value'], 'base': group, 'distance': d}
        if not ability:
            e = scorer.cross_line(m, key, facets, fits, {}, tier, without=m)
            if e is not None:
                out[e['estimate']['method']] = {'value': e['value'], 'base': e['estimate']['base_model'],
                                                'distance': e['estimate']['version_distance']}
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


def against(cells, a, b, facet='.'.join(fa.SPEED_KEY)):
    """(n, median |error| of method a, of method b) over the High cells of `facet` both estimate."""
    both = [c for c in cells if c['facet'] == facet and c['effort'] == 'high' and a in c['est'] and b in c['est']]
    med = lambda k: round(float(np.median([abs(c['est'][k]['value'] - c['truth']) for c in both])), 3) if both else None  # noqa: E731
    return len(both), med(a), med(b)


def cross_line_time_loo(references=REFERENCES):
    """{'cross_line_median': (n, median |log error|), 'own_line': (n, ...), 'both': (n, cross, own)} over the High
    Time/Task cells read on Artificial Analysis: each hidden and estimated from the median of the read High times of the
    nearest relatives of another line (same maker and size, version distance up to the declared one), and of its own line."""
    refs = Path(references)
    acc = json.loads((refs / 'accounts.json').read_text(encoding='utf-8-sig'))
    models = json.loads((refs / 'five-axis-scores.json').read_text(encoding='utf-8-sig'))['models']
    read = {m: r['duration_multiplier']['minutes_by_effort']['high'] for m, r in models.items()
            if (r['duration_multiplier']['basis_by_effort'].get('high') or {}).get('method') == 'aa'}
    pool, line_of = series.groups(acc, 'ability'), series.lines(acc)
    versions, small = series.versions(acc), series.small_rows(acc)
    up_to = fa.estimation_order(acc)['sibling_first_up_to_distance']
    errs = {'cross': {}, 'own': {}}
    for m, truth in read.items():
        for kind, keep in (('cross', lambda b: line_of.get(b) != line_of.get(m)), ('own', lambda b: line_of.get(b) == line_of.get(m))):
            cands = [b for b in pool.get(m, ()) if b != m and b in read and keep(b)]
            groups = series.nearest(m, cands, pool.get(m, ()), versions, small, None, line_of, up_to if kind == 'cross' else None)
            if groups and groups[0][0] is not None and (kind == 'own' or groups[0][0] <= up_to):
                est = float(np.median([read[b] for b in groups[0][1]]))
                errs[kind][m] = abs(float(np.log(est / truth)))
    both = sorted(set(errs['cross']) & set(errs['own']))
    med = lambda xs: round(float(np.median(xs)), 3) if xs else None  # noqa: E731
    return {'cross_line_median': (len(errs['cross']), med(list(errs['cross'].values()))),
            'own_line': (len(errs['own']), med(list(errs['own'].values()))),
            'both': (len(both), med([errs['cross'][m] for m in both]), med([errs['own'][m] for m in both]))}


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
    print('\nresponsiveness at High, on the cells both estimate (median |error|):')
    for a, b in (('sibling_shift', 'cross_line_median'), ('cross_line_median', 'tier_fit'),
                 ('cross_line_extrapolation', 'tier_fit'), ('cross_line_extrapolation', 'cross_line_median')):
        n, ea, eb = against(cells, a, b)
        print(f'  {a} against {b}: n={n} {ea} against {eb}')
    t = cross_line_time_loo(args.references)
    print('\nTime/Task at High read on Artificial Analysis (median |ln(estimate / reading)|):')
    print(f"  other lines' median n={t['cross_line_median'][0]} {t['cross_line_median'][1]}; own line n={t['own_line'][0]} "
          f"{t['own_line'][1]}; on the same cells n={t['both'][0]}: other lines {t['both'][1]}, own line {t['both'][2]}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
