"""A week of steady demand through the five-axis path (round 047, rounds 6-7). Read-only.

    python -X utf8 audit/five_axis_pacing_sim.py [--json out.json] [--md out.md] [--every HOURS]

One task arrives every `--every` hours for a whole week, cycling through calibration/five_axis_tasks.py in a fixed
shuffled order, so the mix is the same at the start, middle and end of the week. Each decision sees the usage the
earlier decisions left: the chosen bundle's expected usd is charged to every window it draws on (long windows reset
at the end of the week, 5-hour windows every 5 hours). Two economies:

- pacing: the five-axis path as shipped (each long window charges burn x its pacing shadow price, round 6).
- no_pacing: the same with `pace` off (shortage-only economy: nothing is charged while the window is far from empty).

The report gives, per third of the week, the model shares and where each long window stands against an even pace,
and every decision a fast-burning model (usd/h above the cheapest qualifying billed candidate's) won: its fit edge over
the best cheaper candidate against its extra opportunity cost.
"""
import argparse
import copy
import json
import random
import sys
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import five_axis_parity as fp  # noqa: E402

allocator, sim = fp.allocator, fp.sim
WEEK = 168.0
FLEETS = ('max20+pro+ultra20+opencode', 'pro+plus+opencode')
ECONOMIES = {'pacing': {'enabled': True}, 'no_pacing': {'enabled': True, 'pace': False}}


def windows_of(accounts):
    out = {}
    for name, spec in sim.metered_accounts(accounts).items():
        out[name] = {wid: float(w.get('hours') or WEEK) for wid, w in spec['windows'].items()}
    return out


def document(wins, used, t, now):
    providers = {}
    for name, ws in wins.items():
        rows = []
        for wid, hours in ws.items():
            end = (int(t // hours) + 1) * hours if hours < WEEK else WEEK
            rows.append({'id': wid, 'used_pct': round(min(100.0, used[name][wid]), 3),
                         'resets_at': (now + timedelta(hours=end - t)).isoformat()})
        providers[name] = {'fetched_at': now.isoformat(), 'stale': False, 'available': True, 'windows': rows}
    return {'providers': providers}


def run(fleet, economy, every):
    accounts, pools = fp.fleet_accounts(fleet)
    hp = sim.host_policy_for('fresh', accounts, fleet)
    acc = fp.with_algo(accounts, ECONOMIES[economy])
    overlaid = allocator.five_axis_overlay(copy.deepcopy(acc), allocator.load_five_axis())
    wins = windows_of(accounts)
    used = {n: {w: 0.0 for w in ws} for n, ws in wins.items()}
    order = list(fp.TASKS)
    random.Random(6).shuffle(order)
    start = fp.NOW
    rows, t, i = [], 0.0, 0
    while t < WEEK:
        for name, ws in wins.items():          # 5-hour windows renew at their block boundary
            for wid, hours in ws.items():
                if hours < WEEK and t > 0 and int(t // hours) != int((t - every) // hours):
                    used[name][wid] = 0.0
        now = start + timedelta(hours=t)
        usage = sim.snapshot(document(wins, used, t, now), accounts, now)
        label_, cls, prof = order[i % len(order)]
        task = {'objective': label_, 'profile': dict(prof), 'expected_hours': prof['expected_hours']}
        if prof.get('work_kind'):
            task['work_kind'] = prof['work_kind']
        d = allocator.plan(task, usage, now, accounts=copy.deepcopy(acc), overrides=[], records=[], host_policy_config=hp)
        chosen = next((c for c in d['ranking'] if c['id'] == d.get('choice')), None)
        row = {'t': t, 'task': label_, 'class': cls, 'choice': fp.label(d.get('selected_bundle')),
               'used_before': {n: {w: round(v, 2) for w, v in ws.items()} for n, ws in used.items()}}
        if chosen and chosen.get('expected_usd'):
            bundle = chosen['bundle']
            spec = overlaid['accounts'].get(bundle['account']) or {}
            for wid, _w, _o in allocator.applicable_windows(spec, bundle['model'], None, bundle, overlaid):
                cap = (spec.get('capacity_usd') or {}).get(wid)
                if cap and wid in used.get(bundle['account'], {}):
                    used[bundle['account']][wid] += 100.0 * chosen['expected_usd'] / cap
            row.update(usd=chosen['expected_usd'], usd_per_hour=(chosen.get('five_axis_price') or {}).get('usd_per_hour'),
                       components={k: chosen['components'].get(k) for k in ('fit', 'opportunity_cost', 'five_axis_level',
                                                                              'five_axis_shortfall')},
                       pacing=((chosen.get('capacity') or {}).get('pacing')))
            row['edge'] = edge(d, chosen)
        rows.append(row)
        t += every
        i += 1
    return rows, {n: {w: round(v, 1) for w, v in ws.items()} for n, ws in used.items()}


def edge(decision, chosen):
    """The winner against the best-scoring cheaper billed or free candidate: fit edge (score units) and extra cost."""
    rate = (chosen.get('five_axis_price') or {}).get('usd_per_hour') or 0.0
    cheaper = [c for c in decision['ranking'] if c['score'] is not None and c is not chosen
               and ((c.get('five_axis_price') or {}).get('usd_per_hour') or 0.0) < rate]
    if not cheaper:
        return None
    alt = max(cheaper, key=lambda c: c['score'])
    w = 0.4
    return {'alternative': fp.label(alt['bundle']), 'alt_usd_per_hour': (alt.get('five_axis_price') or {}).get('usd_per_hour'),
            'fit_edge': round(w * (chosen['components']['fit'] - alt['components']['fit']), 4),
            'extra_cost': round(chosen['components']['opportunity_cost'] - alt['components']['opportunity_cost'], 4),
            'score_margin': round(chosen['score'] - alt['score'], 4)}


def third(t):
    return 'start' if t < WEEK / 3 else 'middle' if t < 2 * WEEK / 3 else 'end'


def markdown(results, every):
    out = [f'# Five-axis weekly pacing simulation, round 9 (one task every {every:g}h, {int(WEEK // every)} tasks a week)', '']
    for fleet, by_econ in results.items():
        out += [f'## {fleet}', '']
        for econ, (rows, final) in by_econ.items():
            out += [f'### {econ}', '', '| model@account | start | middle | end |', '| :--- | ---: | ---: | ---: |']
            share = defaultdict(Counter)
            for r in rows:
                share[r['choice']][third(r['t'])] += 1
            for m, c in sorted(share.items(), key=lambda kv: -sum(kv[1].values())):
                out.append(f"| {m} | {c['start']} | {c['middle']} | {c['end']} |")
            out += ['', 'Long windows against an even pace (used % at the third\'s end / elapsed %):', '']
            long_ids = sorted({(n, w) for r in rows for n, ws in r['used_before'].items() for w in ws
                               if not w.endswith('5h') and w != 'five_hour'})
            out += ['| window | ' + ' | '.join(f'{p}%' for p in (33, 67, 100)) + ' |', '| :--- | ---: | ---: | ---: |']
            for n, w in long_ids:
                marks = []
                for edge_t in (WEEK / 3, 2 * WEEK / 3):
                    nxt = next((r for r in rows if r['t'] >= edge_t), None)
                    marks.append(nxt['used_before'][n][w] if nxt else None)
                marks.append(final[n][w])
                out.append(f'| {n}/{w} | ' + ' | '.join('-' if v is None else f'{v:.1f}' for v in marks) + ' |')
            fast = [r for r in rows if r.get('edge') and (r.get('usd_per_hour') or 0) >= 30]
            out += ['', f'Fast-burning wins (usd/h >= 30): {len(fast)}', '']
            if fast:
                out += ['| t (h) | task | winner | $/h | best cheaper | $/h | fit edge | extra cost | margin |',
                        '| ---: | :--- | :--- | ---: | :--- | ---: | ---: | ---: | ---: |']
                out += [f"| {r['t']:g} | {r['task']} | {r['choice']} | {r['usd_per_hour']} | {r['edge']['alternative']} | "
                        f"{r['edge']['alt_usd_per_hour']} | {r['edge']['fit_edge']} | {r['edge']['extra_cost']} | "
                        f"{r['edge']['score_margin']} |" for r in fast]
            prices = [r['pacing']['price'] for r in rows if r.get('pacing')]
            if prices:
                by = defaultdict(list)
                for r in rows:
                    if r.get('pacing'):
                        by[third(r['t'])].append(r['pacing']['price'])
                out += ['', 'Pacing price of the winner\'s binding window (mean per third): ' + ', '.join(
                    f'{k} {sum(v) / len(v):.3f}' for k, v in by.items())]
            out.append('')
    return '\n'.join(out)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--json')
    ap.add_argument('--md')
    ap.add_argument('--every', type=float, default=3.0)
    args = ap.parse_args(argv)
    results = {f: {e: run(f, e, args.every) for e in ECONOMIES} for f in FLEETS}
    text = markdown(results, args.every)
    if args.md:
        Path(args.md).write_text(text, encoding='utf-8', newline='\n')
    if args.json:
        Path(args.json).write_text(json.dumps({f: {e: {'rows': r, 'final_used': u} for e, (r, u) in v.items()}
                                               for f, v in results.items()}, indent=1), encoding='utf-8', newline='\n')
    print(text)


if __name__ == '__main__':
    main()
