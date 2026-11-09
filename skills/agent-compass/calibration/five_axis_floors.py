"""Difficulty floors on the five-axis scale (round 047, round 2): the scalar tier folded into the axes.

Called by `five_axis.py`; writes the `floors` block of `references/five-axis-scores.json`.

A task's floor is `ratio[difficulty]` times the anchor model's demand-weighted axis level on the task's own demand
(critical work: `critical_ratio[difficulty]`), and every axis the task demands at level 2 or more needs at least
`axis_ratio x share x` the anchor's score on that axis. Floors relative to an anchor do not drift when a new
leaderboard top lowers every board-best-relative score: the anchor's level falls with the others.

Calibration (the user's rule: in the ample state the qualifying set per difficulty stays close to today's):
- Cases: the 53 calibration cases of references/classifier-cases.json (real briefs with their expected profiles; no
  classifier `axis_demand` yet, so their demand is allocator.five_axis_fallback_levels) and the 40 synthetic profiles of
  five_axis_tasks.py (hand-labelled axis_demand), which add the coupled and frontier work the cases lack.
- Models: every model row with a shipped tier and no missing axis, except the synthetic alignments (`SYNTHETIC`).
- Today: the shipped tier at High (allocator.effort_ability) against the route's floor plus critical headroom.
- New: the demanded-axis level against the floor, plus the per-axis floors.
- For each `axis_ratio` on a grid, each difficulty's ratio maximises agreement over its non-critical (case, model)
  pairs and the critical ratio over its critical pairs (a difficulty without critical cases keeps today's
  floor-plus-headroom over floor); the `axis_ratio` with the most agreement overall wins. Ties take the middle ratio.
- `axis_per_tier` is the slope of the equal-weight axis level on the shipped tier: it converts spans written in tier
  points (saturation, overqualification) to the axis scale.
"""
import json
import sys
from collections import defaultdict

import numpy as np

import five_axis_tasks
import sources

sys.path.insert(0, str(sources.REPO / 'scripts'))
import allocator  # noqa: E402

CASES = sources.REPO / 'references' / 'classifier-cases.json'
ANCHOR = 'claude-opus-5-5'
SYNTHETIC = ('dot', 'grok-bot', 'gpt-6-pro')       # tiers that are routing alignments, not measurements
DIFFICULTIES = ('routine', 'hard', 'coupled', 'frontier')
RATIOS = np.round(np.arange(0.30, 1.0001, 0.0025), 4)
AXIS_RATIOS = (0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0)


def cases(acc):
    policy = acc['scoring']
    try:
        routes = (allocator.load_capabilities(accounts=acc) or {}).get('routes') or {}
    except Exception:       # a capability catalog that cannot compose here falls back to the policy floors
        routes = {}
    out = []
    listed = [(c.get('id'), c.get('expected_profile') or {}) for c in
              json.loads(CASES.read_text(encoding='utf-8-sig')).get('calibration_cases') or []]
    listed += [(label, prof) for label, _, prof in five_axis_tasks.TASKS]
    for case_id, prof in listed:
        if not prof.get('reasoning_depth'):
            continue
        kind = prof.get('work_kind') or allocator.profile_kind(prof, policy)
        difficulty = allocator.profile_difficulty(prof, policy)
        critical = prof.get('error_cost', 1) >= 3
        floor = allocator.ability_floor(policy, routes.get(kind), difficulty)
        headroom = allocator.critical_headroom(policy, difficulty) if critical else 0.0
        demand, source = allocator.five_axis_demand({'profile': prof, 'kind': kind, 'divergence': prof.get('divergence')})
        out.append({'id': case_id, 'demand_source': source, 'difficulty': difficulty, 'critical': critical, 'old_floor': floor,
                    'old_gate': floor + headroom, 'demand': demand})
    return out


def models(doc, acc):
    policy, curves = acc['scoring'], acc.get('effort_curves') or {}
    out = {}
    for m, row in doc['models'].items():
        info = acc['models'].get(m) or {}
        if m in SYNTHETIC or row.get('missing_axes') or info.get('tier') is None:
            continue
        out[m] = {'scores': row['scores'], 'old': allocator.effort_ability(policy, curves.get(m) or {}, info, 'high')}
    return out


def max_share(scores, anchor, demand, axis_ratio):
    """The largest floor share at which the model still qualifies on this demand, and the binding term."""
    level, eff = allocator.five_axis_level(scores, demand)
    a_level, a_eff = allocator.five_axis_level(anchor, demand)
    terms = [(level / a_level if a_level > 0 else 0.0, f'level {level:.3f} vs anchor {a_level:.3f}')]
    for axis, w in demand.items():
        if w >= allocator.FIVE_AXIS_DEFAULTS['strong_level'] and a_eff[axis] > 0:
            terms.append((eff[axis] / (axis_ratio * a_eff[axis]), f'{axis} {eff[axis]:.3f} vs anchor {a_eff[axis]:.3f}'))
    return min(terms)


def best_share(pairs, axis_ratio_shares):
    """(share, agreeing pairs) maximising agreement; ties take the middle of the best run."""
    if not pairs:
        return None, 0
    counts = [sum((top >= r - 1e-9) == old for top, old in pairs) for r in RATIOS]
    peak = max(counts)
    best = [r for r, n in zip(RATIOS, counts) if n == peak]
    return float(best[len(best) // 2]), peak


def calibrate(doc, acc):
    rows = models(doc, acc)
    anchor = doc['models'][ANCHOR]['scores']
    all_cases = cases(acc)
    groups = defaultdict(list)          # (difficulty, critical) -> [(scores, demand, old qualifies, model, case)]
    for c in all_cases:
        for m, r in rows.items():
            groups[(c['difficulty'], c['critical'])].append((r['scores'], c['demand'], r['old'] >= c['old_gate'] - 1e-9, m, c))
    policy = acc['scoring']
    best = None
    for k in AXIS_RATIOS:
        tops = {g: [(max_share(sc, anchor, dm, k), old, m, c) for sc, dm, old, m, c in pairs] for g, pairs in groups.items()}
        ratio, crit, agree = {}, {}, 0
        for d in DIFFICULTIES:
            ratio[d], n = best_share([(t[0], old) for t, old, _, _ in tops.get((d, False), [])], k)
            agree += n
            if ratio[d] is None:        # no case at this difficulty: today's floor over the anchor's tier
                ratio[d] = round(float(policy['ability_floor'][d]) / float(acc['models'][ANCHOR]['tier']), 4)
            cpairs = [(t[0], old) for t, old, _, _ in tops.get((d, True), [])]
            if cpairs:
                crit[d], n = best_share(cpairs, k)
                agree += n
            else:
                floor = float(policy['ability_floor'][d])
                crit[d] = round(ratio[d] * (floor + allocator.critical_headroom(policy, d)) / floor, 4)
        if best is None or agree > best[0]:
            best = (agree, k, ratio, crit, tops)
    _, k, ratio, crit, tops = best
    report, changes = {}, {}
    for (d, critical), rows_ in sorted(tops.items()):
        share = crit[d] if critical else ratio[d]
        key = f'{d}{" critical" if critical else ""}'
        n_agree = 0
        per = defaultdict(lambda: {'enters': 0, 'leaves': 0, 'cases': 0, 'why_leaves': None})
        for (top, why), old, m, c in rows_:
            new = top >= share - 1e-9
            per[m]['cases'] += 1
            n_agree += new == old
            if new and not old:
                per[m]['enters'] += 1
            elif old and not new:
                per[m]['leaves'] += 1
                per[m]['why_leaves'] = per[m]['why_leaves'] or f"{c['id']}: {why} (needs share {share:g}, reaches {top:.3f})"
        report[key] = {'share': share, 'pairs': len(rows_), 'cases': len({c['id'] for _, _, _, c in rows_}),
                       'agreement': round(n_agree / len(rows_), 3) if rows_ else None}
        changes[key] = {m: v for m, v in sorted(per.items()) if v['enters'] or v['leaves']}
    tiers = [(float(acc['models'][m]['tier']), float(np.mean([r['scores'][a] for a in allocator.FIVE_AXES]))) for m, r in rows.items()]
    slope = float(np.polyfit([t for t, _ in tiers], [v for _, v in tiers], 1)[0]) if len(tiers) >= 5 else 0.2
    return {'anchor': ANCHOR, 'ratio': ratio, 'critical_ratio': crit, 'axis_ratio': k,
            'axis_per_tier': round(slope, 4), 'agreement': report, 'changes': changes,
            'calibrated_on': {'cases': [str(CASES.relative_to(sources.REPO)), 'calibration/five_axis_tasks.py'],
                              'n_cases': len(all_cases), 'n_models': len(rows),
                              'demand': 'classifier cases: allocator.five_axis_fallback_levels; synthetic profiles: hand-labelled axis_demand',
                              'today': 'shipped tier at High vs route ability_floor + critical headroom'},
            'drift': ('Floors are shares of the anchor model level on the same demand, so a new leaderboard top that lowers '
                      'every board-best-relative score lowers the anchor level and the floor together. Re-run this '
                      'calibration only when the anchor model itself changes.')}
