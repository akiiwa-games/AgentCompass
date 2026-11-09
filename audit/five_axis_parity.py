"""Sampled routing parity: today's router against the five-axis path (round 047, round 7). Read-only.

    python -X utf8 audit/five_axis_parity.py [--json out.json] [--md out.md]

Each task profile (calibration/five_axis_tasks.py, with hand-labelled `axis_demand` standing in for the classifier's)
runs under several synthetic quota states on three fresh-install fleets (route_simulation's discovery snapshots and
usage documents, so the input is what `roll` would feed the allocator). Three algorithms:

- old: shipped behaviour (`scoring.five_axis` absent).
- new: five-axis path as shipped in five-axis-scores.json: community capacity, usd on the community token-mix basis
  (option B), capacity ladder, a pacing shadow price on every long window (round 6); round 7: the axes are the only
  dimension system and quality is the continuous demand fit with shortfall_weight 1 (no difficulty floors), ties to
  the lower opportunity cost then the higher level.
- new_benchmark_usd: the same with usd on the AA/OpenRouter benchmark basis (the round-3 scale), to isolate option B.
- new_no_pacing: the new path with `pace` off (shortage-only economy: nothing charged while a window is far from empty).
- new_shortfall_2: the new path with shortfall_weight 2 (a point below a demanded target costs 3 instead of 2).

`none` decisions record their cause: the exclusion reasons of every candidate, grouped (digits stripped).

Each new decision on a non-routine task also records whether a clearly weaker model won it: the winner's demand-weighted
axis level more than WEAKER below the strongest scored candidate's, with the score margin and what bought it (fit lost
against opportunity cost saved).

This is a sample, not the 180k-row grid (LEAN rules, 2026-11-07).
"""
import argparse
import copy
import json
import os
import re
import shutil
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'skills' / 'agent-compass'
sys.path.insert(0, str(SKILL / 'scripts'))
sys.path.insert(0, str(SKILL / 'calibration'))
import allocator  # noqa: E402
import route_simulation as sim  # noqa: E402
import five_axis_tasks  # noqa: E402

NOW = datetime(2026, 11, 9, 4, 0, tzinfo=timezone.utc)
TASKS = five_axis_tasks.TASKS
FLEETS = {
    'max20+pro+ultra20+opencode': {'accounts': {'claude': ('claude', 'max20'), 'codex': ('codex', 'pro'),
                                                 'antigravity': ('antigravity', 'ultra20'), 'opencode': ('opencode', 'free')}},
    'pro+plus+opencode': {'accounts': {'claude': ('claude', 'pro'), 'codex': ('codex', 'plus'), 'opencode': ('opencode', 'free')}},
    'max20+pro+pi+dot+grok+opencode': {'accounts': {'claude': ('claude', 'max20'), 'codex': ('codex', 'pro'), 'pi': ('pi', 'pi'),
                                                     'dot': ('dot', 'dot'), 'grok': ('grok', 'supergrok'),
                                                     'opencode': ('opencode', 'free')}},
}
EXTRA_CATALOGS = {'pi': ['gpt-6.1-sol', 'gpt-6-astra', 'gpt-6.1-terra'], 'dot': ['dot'], 'grok': ['grok-4.7', 'grok-bot']}
EXTRA_STATES = {'claude fable-weekly tight': [('claude', ('weekly_fable',), 'tight')]}
STATES = ('ample', 'claude scarce', 'codex scarce', 'antigravity gemini scarce', 'claude tight', 'codex tight',
          'claude fable-weekly tight', '5h-only scarce')
NEW = ('new', 'new_benchmark_usd', 'new_no_pacing', 'new_shortfall_2')
WEAKER = 0.05   # axis units below the strongest scored candidate's level: a clearly weaker model


def algos(folder):
    import five_axis
    import sources
    meas = json.loads((SKILL / 'references' / 'public-measurements.json').read_text(encoding='utf-8'))
    plans = json.loads((SKILL / 'references' / 'plan-templates.json').read_text(encoding='utf-8'))
    paths = {}
    for name, kwargs in (('benchmark-usd', {'usd_basis': 'benchmark'}),):
        paths[name] = Path(folder) / f'five-axis-{name}.json'
        paths[name].write_text(json.dumps(five_axis.build(meas, sources.accounts(), plans, **kwargs)), encoding='utf-8')
    return {'old': None, 'new': {'enabled': True},
            'new_benchmark_usd': {'enabled': True, 'path': str(paths['benchmark-usd'])},
            'new_no_pacing': {'enabled': True, 'pace': False},
            'new_shortfall_2': {'enabled': True, 'shortfall_weight': 2.0}}


def fleet_accounts(name):
    sim.ACCOUNT_SETS[name] = FLEETS[name]
    return sim.accounts_for('fresh', name)


def with_algo(accounts, cfg):
    acc = copy.deepcopy(accounts)
    if cfg is None:
        acc['scoring'].pop('five_axis', None)
    else:
        acc['scoring']['five_axis'] = dict(cfg)
    return acc


def label(bundle):
    return f"{bundle['model']}@{bundle['account']}" if bundle and bundle.get('model') else 'none'


def top_components(decision):
    top = next((c for c in decision['ranking'] if c['score'] is not None), None)
    if not top:
        return None
    c = top['components']
    keep = ('fit', 'ability', 'opportunity_cost', 'overqualification', 'effort_penalty', 'free_usage', 'five_axis_level',
            'five_axis_shortfall', 'route_fit', 'vector_fit', 'subjective_fit', 'reset_bonus', 'shortfall')
    return {'id': top['id'], 'score': top['score'], **{k: c[k] for k in keep if k in c}}


def base_model(model):
    """The model without an effort suffix (Antigravity lists Opus at High as its own row)."""
    return re.sub(r'-(low|medium|high|xhigh|max)$', '', str(model))


def weaker_win(decision, cls):
    """On a non-routine task: the winner against the strongest scored candidate (highest demand-weighted level), when the
    winner is more than WEAKER below it; else None."""
    if cls.startswith('routine'):
        return None
    scored = [c for c in decision['ranking'] if c['score'] is not None and 'five_axis_level' in c['components']]
    win = next((c for c in scored if c['id'] == decision.get('choice')), None)
    if not win:
        return None
    # Model against model, each at its best scored effort: a lower effort of the strongest model is not a weaker model.
    top = {}
    for c in scored:
        m = base_model(c['bundle']['model'])
        if m not in top or c['components']['five_axis_level'] > top[m]['components']['five_axis_level']:
            top[m] = c
    best = max(top.values(), key=lambda c: c['components']['five_axis_level'])
    own = top[base_model(win['bundle']['model'])]
    gap = best['components']['five_axis_level'] - own['components']['five_axis_level']
    if gap <= WEAKER:
        return None
    # The margin is against the strongest model's best-scoring bundle (its effort that the economy liked most).
    rival = max((c for c in scored if base_model(c['bundle']['model']) == base_model(best['bundle']['model'])
                 and c['bundle']['account'] == best['bundle']['account']), key=lambda c: c['score'])
    k, b = win['components'], rival['components']
    return {'winner': label(win['bundle']), 'winner_effort': win['bundle'].get('effort'), 'winner_level': k['five_axis_level'],
            'winner_best_level': own['components']['five_axis_level'],
            'strongest': label(rival['bundle']), 'strongest_effort': rival['bundle'].get('effort'),
            'strongest_level': best['components']['five_axis_level'], 'gap': round(gap, 4),
            'score_margin': round(win['score'] - rival['score'], 4),
            'other': round((win['score'] - rival['score']) - (0.4 * (k['fit'] - b['fit']) + b['opportunity_cost'] - k['opportunity_cost']), 4),
            'fit_lost': round(0.4 * (b['fit'] - k['fit']), 4),
            'cost_saved': round(b['opportunity_cost'] - k['opportunity_cost'], 4),
            'demand': win.get('five_axis_demand')}


def candidate(decision, model_label):
    for c in decision['ranking']:
        if label(c.get('bundle')) == model_label and c['score'] is not None:
            return c
    return None


def driver(new_dec, old_label, new_label):
    """Which score term moved the choice: the new winner against the old winner inside the new ranking."""
    if old_label == new_label:
        return None
    if old_label == 'none':
        return {'kind': 'old had no eligible bundle', 'reason': 'old had no eligible bundle'}
    if new_label == 'none':
        return {'kind': 'new has no eligible bundle', 'reason': 'new has no eligible bundle'}
    o_new, n_new = candidate(new_dec, old_label), candidate(new_dec, new_label)
    if o_new is None:
        loser = next((c for c in new_dec['ranking'] if label(c.get('bundle')) == old_label), None)
        why = (loser or {}).get('excluded') or ['not a candidate']
        return {'kind': 'old choice excluded', 'reason': why[0]}
    terms = {}
    for k, sign in (('fit', 1), ('opportunity_cost', -1), ('overqualification', -1), ('effort_penalty', -1),
                    ('free_usage', 1), ('reset_bonus', 1), ('continuity', 1)):
        a, b = n_new['components'].get(k) or 0, o_new['components'].get(k) or 0
        d = sign * (0.4 if k == 'fit' else 1.0) * (a - b)
        if abs(d) > 1e-4:
            terms[k] = round(d, 4)
    if 'fit' in terms:
        demand = n_new.get('five_axis_demand') or {}
        comp = {a: ('capability' if a == 'vision_tools' else a) for a in allocator.FIVE_AXES}
        terms['fit_axes'] = {a: round(((n_new['components'].get(comp[a]) or 0) - (o_new['components'].get(comp[a]) or 0)) * w, 3)
                             for a, w in demand.items() if w}
    main = max((k for k in terms if k != 'fit_axes'), key=lambda k: abs(terms[k]), default=None)
    if main == 'fit' and terms.get('fit_axes'):
        main = f"fit ({max(terms['fit_axes'], key=lambda a: abs(terms['fit_axes'][a]))})"
    return {'kind': 'score', 'main': main, 'terms': terms}


def fable_vs_opus(decision):
    """The best Fable 5.1 and Opus 5.5 candidates of one decision (any account or effort) and the score margin."""
    best = {}
    for c in decision['ranking']:
        if c['score'] is None:
            continue
        model = str(c['bundle'].get('model') or '')
        key = 'fable' if 'fable-5-1' in model or model.startswith('fable') else 'opus' if 'opus-5-5' in model else None
        if key and (key not in best or c['score'] > best[key]['score']):
            k = c['components']
            best[key] = {'label': label(c['bundle']), 'bundle': c['id'], 'score': c['score'], 'fit': k.get('fit'),
                         'opportunity_cost': k.get('opportunity_cost'), 'level': k.get('five_axis_level')}
    if 'fable' not in best:
        return None
    out = {'fable': best['fable'], 'opus': best.get('opus')}
    out['margin'] = round(best['fable']['score'] - best['opus']['score'], 4) if best.get('opus') else None
    return out


def none_cause(decision):
    """For a decision with no selection: {reason group: candidates}, each candidate counted under its first exclusion."""
    if decision.get('selected_bundle'):
        return None
    out = Counter()
    for c in decision.get('ranking') or ():
        reasons = c.get('excluded') or []
        if reasons:
            out[re.sub(r'\s+', ' ', re.sub(r'[\d.]+', '#', str(reasons[0])))[:110]] += 1
    return dict(out.most_common())


def run(variants_cfg):
    rows = []
    for fleet in FLEETS:
        accounts, pools = fleet_accounts(fleet)
        hp = sim.host_policy_for('fresh', accounts, fleet)
        variants = {name: with_algo(accounts, cfg) for name, cfg in variants_cfg.items()}
        for state in STATES:
            usage = sim.usage_for(state, accounts, NOW, pools)
            for label_, cls, prof in TASKS:
                task = {'objective': label_, 'profile': dict(prof), 'expected_hours': prof['expected_hours']}
                if prof.get('work_kind'):
                    task['work_kind'] = prof['work_kind']
                out = {'fleet': fleet, 'state': state, 'task': label_, 'class': cls}
                decisions = {}
                for name, acc in variants.items():
                    d = allocator.plan(copy.deepcopy(task), usage, NOW, accounts=copy.deepcopy(acc), overrides=[], records=[],
                                       host_policy_config=hp)
                    decisions[name] = d
                    out[name] = label(d.get('selected_bundle'))
                    out[f'{name}_bundle'] = d.get('choice')
                    out[f'{name}_top'] = top_components(d)
                for name in NEW:
                    out[f'{name}_driver'] = driver(decisions[name], out['old'], out[name])
                out['new_weaker'] = weaker_win(decisions['new'], cls)
                out['fable_vs_opus'] = fable_vs_opus(decisions['new'])
                for name in ('old',) + NEW:
                    out[f'{name}_none'] = none_cause(decisions[name])
                rows.append(out)
    return rows


def account_of(lbl):
    return lbl.split('@')[-1] if '@' in lbl else 'none'


def why(r, name):
    d = r.get(f'{name}_driver') or {}
    if d.get('kind') == 'score':
        return d.get('main')
    return d.get('reason') if 'eligible' in d.get('kind', '') else f"old excluded: {d.get('reason')}"


def markdown(rows):
    names = ('old',) + NEW
    share = {n: Counter(r[n] for r in rows) for n in names}
    n = len(rows)
    lines = [f'# Five-axis sampled parity, round 8 ({n} decisions: {len(TASKS)} tasks x {len(STATES)} states x {len(FLEETS)} fleets)', '']
    lines += ['## Selection share (model@account)', '',
              '| model@account | old | new (arena usd) | new, benchmark usd | new, no pacing | new, shortfall weight 2 |',
              '| :--- | ---: | ---: | ---: | ---: | ---: |']
    for k in sorted(set().union(*share.values()), key=lambda k: (-share['old'][k], -share['new'][k])):
        lines.append(f"| {k} | {share['old'][k]} | {share['new'][k]} | {share['new_benchmark_usd'][k]} | {share['new_no_pacing'][k]} | "
                     f"{share['new_shortfall_2'][k]} |")
    lines += ['', '| | ' + ' | '.join(NEW) + ' |', '| :--- | ' + ' | '.join('---:' for _ in NEW) + ' |',
              '| changed vs old | ' + ' | '.join(str(sum(r[x] != r['old'] for r in rows)) for x in NEW) + ' |',
              '| changed vs new | ' + ' | '.join(str(sum(r[x] != r['new'] for r in rows)) for x in NEW) + ' |', '']
    lines += ['## `none` decisions and their causes', '', '| algorithm | none | per fleet |', '| :--- | ---: | :--- |']
    for name in names:
        per = Counter(r['fleet'] for r in rows if r[name] == 'none')
        lines.append(f"| {name} | {share[name]['none']} | " + ', '.join(f'{f}: {per[f]}' for f in FLEETS) + ' |')
    for name in ('old', 'new', 'new_benchmark_usd'):
        causes = Counter()
        for r in rows:
            for reason, k in (r.get(f'{name}_none') or {}).items():
                causes[reason] += k
        lines += ['', f'Exclusion reasons over the candidates of `none` decisions ({name}; candidate counts):', '']
        lines += [f'- {k}: {v}' for k, v in causes.most_common(12)]
    lines.append('')
    weak = [r for r in rows if r.get('new_weaker')]
    hard = [r for r in rows if not r['class'].startswith('routine')]
    lines += [f'## Non-routine tasks won by a clearly weaker model (new; level more than {WEAKER} below the strongest scored)', '',
              f'- {len(weak)} of {len(hard)} non-routine decisions', '']
    if weak:
        lines += ['Strongest = the highest-level model at its best effort; its bundle shown is the effort that scored best, '
                  'against which fit lost (0.4 x fit), cost saved (opportunity cost) and the margin are read; other = the rest '
                  '(reset bonus, deferred start, continuity).', '',
                  '| fleet | state | task | demand d/r/m/v | winner | level | strongest (best-scoring effort) | best level | gap | fit lost | cost saved | other | margin |',
                  '| :--- | :--- | :--- | :--- | :--- | ---: | :--- | ---: | ---: | ---: | ---: | ---: | ---: |']
        for r in sorted(weak, key=lambda r: -r['new_weaker']['gap']):
            w = r['new_weaker']
            dm = '/'.join(str((w['demand'] or {}).get(a, '?')) for a in allocator.FIVE_AXES)
            lines.append(f"| {r['fleet']} | {r['state']} | {r['task']} | {dm} | {w['winner']} {w['winner_effort']} | "
                         f"{w['winner_level']} | {w['strongest']} {w['strongest_effort']} | {w['strongest_level']} | {w['gap']} | "
                         f"{w['fit_lost']} | {w['cost_saved']} | {w['other']} | {w['score_margin']} |")
        lines.append('')
    for fleet in FLEETS:
        sub = [r for r in rows if r['fleet'] == fleet]
        lines += [f'## Account split per state, {fleet} (old → new)', '']
        accts = sorted({account_of(r[k]) for r in sub for k in ('old', 'new')})
        lines += ['| state | ' + ' | '.join(accts) + ' |', '| :--- | ' + ' | '.join('---:' for _ in accts) + ' |']
        for s in STATES:
            o = Counter(account_of(r['old']) for r in sub if r['state'] == s)
            w = Counter(account_of(r['new']) for r in sub if r['state'] == s)
            lines.append(f'| {s} | ' + ' | '.join(f'{o[a]}→{w[a]}' for a in accts) + ' |')
        lines.append('')
    for title, test in (('Terra', lambda m: 'terra' in m), ('Flash', lambda m: 'gemini-3.8-flash' in m),
                        ('Fable', lambda m: 'fable' in m),
                        ('free models', lambda m: m.endswith('@opencode') or m.split('@')[0].endswith('-free')),
                        ('dot / grok / pi', lambda m: account_of(m) in ('dot', 'grok', 'pi'))):
        lines += [f'## Where {title} win', '']
        for name in names:
            hits = [r for r in rows if test(r[name])]
            lines.append(f'- {name}: {len(hits)} — ' + ', '.join(f"{k} ({v})" for k, v in Counter(
                f"{r['task']} / {r['state']}" for r in hits).most_common(8)))
        lines.append('')
    lines += ['## Fable 5.1 against Opus 5.5 (new)', '']
    duel = [r for r in rows if r.get('fable_vs_opus')]
    ahead = [r for r in duel if r['fable_vs_opus']['margin'] is not None and r['fable_vs_opus']['margin'] > 0]
    won = [r for r in rows if 'fable' in r['new']]
    lines += [f'- decisions with a scored Fable candidate: {len(duel)}; Fable scores above the best Opus: {len(ahead)}; '
              f'Fable chosen: {len(won)}', '']
    if ahead:
        lines += ['| fleet | state | task | Fable bundle | score | fit | opp. cost | best Opus | score | fit | opp. cost | margin | chosen |',
                  '| :--- | :--- | :--- | :--- | ---: | ---: | ---: | :--- | ---: | ---: | ---: | ---: | :--- |']
        for r in sorted(ahead, key=lambda r: -r['fable_vs_opus']['margin']):
            f, o = r['fable_vs_opus']['fable'], r['fable_vs_opus']['opus']
            lines.append(f"| {r['fleet']} | {r['state']} | {r['task']} | {f['bundle']} | {f['score']} | {f['fit']} | "
                         f"{f['opportunity_cost']} | {o['bundle']} | {o['score']} | {o['fit']} | {o['opportunity_cost']} | "
                         f"{r['fable_vs_opus']['margin']} | {r['new']} |")
    margins = sorted(r['fable_vs_opus']['margin'] for r in duel if r['fable_vs_opus']['margin'] is not None)
    if margins:
        lines += ['', f'Fable minus best Opus over every decision with both scored: max {margins[-1]}, '
                      f'median {margins[len(margins) // 2]}, min {margins[0]}']
    lines.append('')
    drivers = Counter(why(r, 'new') for r in rows if r['new'] != r['old'])
    lines += ['## Main drivers of changed selections (new)', '', '| driver | decisions |', '| :--- | ---: |']
    lines += [f'| {k} | {v} |' for k, v in drivers.most_common()]
    lines.append('')
    for name in NEW:
        changed = [r for r in rows if r[name] != r['old']]
        lines += [f'## Changed selections ({name}): {len(changed)} of {n}', '',
                  '| fleet | state | task | old | new | main driver |', '| :--- | :--- | :--- | :--- | :--- | :--- |']
        lines += [f"| {r['fleet']} | {r['state']} | {r['task']} | {r['old']} | {r[name]} | {why(r, name)} |" for r in changed]
        lines.append('')
    return '\n'.join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--json')
    ap.add_argument('--md')
    args = ap.parse_args()
    os.environ['ROUTER_ACCOUNTS_FILE'] = str(SKILL / 'references' / 'accounts.json')
    sim.CATALOGS.update(EXTRA_CATALOGS)
    sim.STATES.update(EXTRA_STATES)
    folder = tempfile.mkdtemp(prefix='five-axis-parity-')
    try:
        sim.LIVE_PATHS, _ = sim.sandbox_state(folder)     # discovery bookkeeping lands in the throwaway state dir
        rows = run(algos(folder))
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1, ensure_ascii=False, default=str), encoding='utf-8', newline='\n')
    text = markdown(rows)
    if args.md:
        Path(args.md).write_text(text + '\n', encoding='utf-8', newline='\n')
    else:
        print(text)


if __name__ == '__main__':
    sys.exit(main())
