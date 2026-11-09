"""Sampled routing parity: today's router against the five-axis path (round 047, round 4). Read-only.

    python -X utf8 audit/five_axis_parity.py [--json out.json] [--md out.md]

Each task profile (calibration/five_axis_tasks.py, with hand-labelled `axis_demand` standing in for the classifier's)
runs under several synthetic quota states on three fresh-install fleets (route_simulation's discovery snapshots and
usage documents, so the input is what `roll` would feed the allocator). Three algorithms:

- old: shipped behaviour (`scoring.five_axis` absent).
- new: five-axis path as shipped in five-axis-scores.json: axis-demand gate, community capacity, usd on the community
  token-mix basis (option B), tie-break cost then ability surplus, overqualification weight 0.05.
- new_benchmark_usd: the same with usd on the AA/OpenRouter benchmark basis (the round-3 scale), to isolate option B.
- new_oq0: the new path with overqualification_weight 0 (ties still go to the cheaper candidate).
- new_no_eqbench: the new path with EQ-Bench Creative Writing distrusted.

`none` decisions record their cause: the exclusion reasons of every candidate, grouped (digits stripped).

Each new decision also records how close it sits to a floor: the winner's demanded-axis level over its gate, and every
candidate (scored or excluded by a floor) within 1% of its floor.

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
NEW = ('new', 'new_benchmark_usd', 'new_oq0', 'new_no_eqbench')
NEAR = 0.01     # within 1% of a floor


def algos(folder):
    import five_axis
    import sources
    meas = json.loads((SKILL / 'references' / 'public-measurements.json').read_text(encoding='utf-8'))
    plans = json.loads((SKILL / 'references' / 'plan-templates.json').read_text(encoding='utf-8'))
    paths = {}
    for name, kwargs in (('no-eqbench', {'distrust': ['eqbench.creative_writing_v3']}), ('benchmark-usd', {'usd_basis': 'benchmark'})):
        paths[name] = Path(folder) / f'five-axis-{name}.json'
        paths[name].write_text(json.dumps(five_axis.build(meas, sources.accounts(), plans, **kwargs)), encoding='utf-8')
    return {'old': None, 'new': {'enabled': True},
            'new_benchmark_usd': {'enabled': True, 'path': str(paths['benchmark-usd'])},
            'new_oq0': {'enabled': True, 'overqualification_weight': 0.0},
            'new_no_eqbench': {'enabled': True, 'path': str(paths['no-eqbench'])}}


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
            'five_axis_gate', 'route_fit', 'vector_fit', 'subjective_fit', 'reset_bonus', 'shortfall')
    return {'id': top['id'], 'score': top['score'], **{k: c[k] for k in keep if k in c}}


FLOOR_MSG = re.compile(r'(?:demanded-axis level|\w+) ([\d.]+) below (?:the \w+ floor|its per-axis floor) ([\d.]+)')


def near_floor(decision):
    """{winner_margin, near: [candidates within NEAR of a floor, relative]} for one five-axis decision."""
    near, winner = [], None
    for c in decision['ranking']:
        lbl = label(c.get('bundle'))
        k = c.get('components') or {}
        if c['score'] is not None and k.get('five_axis_gate'):
            margin = k['five_axis_level'] / k['five_axis_gate'] - 1
            if c['id'] == decision.get('choice'):
                winner = round(margin, 4)
            if abs(margin) <= NEAR:
                near.append((lbl, round(margin, 4)))
            continue
        for e in c.get('excluded') or ():
            m = FLOOR_MSG.search(e)
            if m and float(m.group(2)) > 0:
                margin = float(m.group(1)) / float(m.group(2)) - 1
                if abs(margin) <= NEAR:
                    near.append((lbl, round(margin, 4)))
                break
    return {'winner_margin': winner, 'near': near}


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
                out['new_floor'] = near_floor(decisions['new'])
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
    lines = [f'# Five-axis sampled parity, round 4 ({n} decisions: {len(TASKS)} tasks x {len(STATES)} states x {len(FLEETS)} fleets)', '']
    lines += ['## Selection share (model@account)', '',
              '| model@account | old | new (usd option B) | new, benchmark usd | new, oq 0 | new without EQ-Bench |',
              '| :--- | ---: | ---: | ---: | ---: | ---: |']
    for k in sorted(set().union(*share.values()), key=lambda k: (-share['old'][k], -share['new'][k])):
        lines.append(f"| {k} | {share['old'][k]} | {share['new'][k]} | {share['new_benchmark_usd'][k]} | {share['new_oq0'][k]} | "
                     f"{share['new_no_eqbench'][k]} |")
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
    win_near = [r for r in rows if r['new_floor']['winner_margin'] is not None and r['new_floor']['winner_margin'] <= NEAR]
    any_near = [r for r in rows if r['new_floor']['near']]
    lines += [f'## Decisions within {NEAR:.0%} of a floor (new)', '',
              f'- winner within {NEAR:.0%} above its gate: {len(win_near)} of {n}',
              f'- any candidate within {NEAR:.0%} of its floor (a re-rating of that size could flip it): {len(any_near)} of {n}',
              '- most frequent near-floor candidates: ' + ', '.join(f'{k} ({v})' for k, v in Counter(
                  lbl for r in any_near for lbl, _ in r['new_floor']['near']).most_common(10)), '']
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
