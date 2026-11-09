"""Sampled routing parity: today's router against the five-axis shadow path (round 047). Read-only.

    python -X utf8 audit/five_axis_parity.py [--json out.json] [--md out.md]

Each task profile below runs under several synthetic quota states on two fresh-install fleets (route_simulation's
discovery snapshots and usage documents, so the input is what `roll` would feed the allocator). Three algorithms:

- old: shipped behaviour (`scoring.five_axis` absent).
- new: five-axis fit, public tier gate, AA usd_per_hour, plan capacity `strict` (no public source, so unknown).
- new_converted: the same with the shipped capacity rescaled to the AA price scale (comparison only, not public data);
  it isolates what the usd change does while capacity still prices quota.

This is a sample, not the 180k-row grid (LEAN rules, 2026-11-07).
"""
import argparse
import copy
import json
import os
import shutil
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills' / 'agent-compass' / 'scripts'))
import allocator  # noqa: E402
import route_simulation as sim  # noqa: E402

NOW = datetime(2026, 11, 9, 4, 0, tzinfo=timezone.utc)
FLEETS = {
    'max20+pro+ultra20+opencode': {'accounts': {'claude': ('claude', 'max20'), 'codex': ('codex', 'pro'),
                                                 'antigravity': ('antigravity', 'ultra20'), 'opencode': ('opencode', 'free')}},
    'pro+plus+opencode': {'accounts': {'claude': ('claude', 'pro'), 'codex': ('codex', 'plus'), 'opencode': ('opencode', 'free')}},
}
STATES = ('ample', 'claude scarce', 'codex scarce', 'antigravity gemini scarce', 'claude tight', 'codex tight', '5h-only scarce')
ALGOS = {'old': None, 'new': {'enabled': True, 'capacity': 'strict', 'overqualification_ref_usd_per_hour': 29.0},
         'new_converted': {'enabled': True, 'capacity': 'converted', 'overqualification_ref_usd_per_hour': 29.0}}
BASE = {'reasoning_depth': 2, 'code_complexity': 2, 'context_scale': 1, 'divergence': 1, 'hallucination_risk': 2,
        'error_cost': 2, 'needs_visual': False, 'needs_gui': False, 'local_only': True, 'realtime_loop': False,
        'wants_fast': False, 'read_only_review': False, 'interaction_style': 'engineering', 'multi_task': False,
        'task_count': '1', 'expected_hours': 1.0, 'hours_confidence': 'medium', 'urgency': 'unknown'}


def p(**kw):
    return {**BASE, **kw}


# (label, class, profile). Classes: routine / hard / coupled / frontier x engineering / exploration / visual / text.
TASKS = [
    ('run a release checklist', 'routine-eng', p(reasoning_depth=1, code_complexity=0, hallucination_risk=1, expected_hours=0.3)),
    ('tweak a constant + test', 'routine-eng', p(reasoning_depth=1, code_complexity=1, hallucination_risk=1, error_cost=1, expected_hours=0.2)),
    ('small helper script', 'routine-eng', p(reasoning_depth=1, code_complexity=1, error_cost=1, expected_hours=0.4)),
    ('bulk rename across files', 'routine-eng', p(reasoning_depth=1, code_complexity=1, context_scale=2, hallucination_risk=1, expected_hours=1.0)),
    ('reformat text', 'routine-text', p(reasoning_depth=1, code_complexity=0, interaction_style='text_only', local_only=False, error_cost=1, expected_hours=0.1)),
    ('summarise a public page', 'routine-text', p(reasoning_depth=1, code_complexity=0, interaction_style='text_only', local_only=False, error_cost=1, hallucination_risk=2, expected_hours=0.2)),
    ('click and screenshot a setting', 'routine-gui', p(reasoning_depth=1, code_complexity=0, needs_visual=True, needs_gui=True, error_cost=1, expected_hours=0.2)),
    ('describe a screenshot', 'routine-visual', p(reasoning_depth=1, code_complexity=0, needs_visual=True, interaction_style='exploration', error_cost=1, expected_hours=0.2)),
    ('implement one module + tests', 'hard-eng', p()),
    ('fix a known bug in one module', 'hard-eng', p(hallucination_risk=1, expected_hours=0.5)),
    ('frontend component', 'hard-eng', p(work_kind='frontend_implementation', needs_visual=True, expected_hours=1.5)),
    ('frontend page, no screenshots', 'hard-eng', p(work_kind='frontend_implementation', expected_hours=1.5)),
    ('read-only diff review', 'hard-review', p(code_complexity=0, read_only_review=True, hallucination_risk=3, error_cost=1, expected_hours=0.5)),
    ('security review of a module', 'hard-review', p(code_complexity=0, read_only_review=True, hallucination_risk=3, context_scale=2, error_cost=2, expected_hours=1.0)),
    ('diagnose a flaky test, cause unknown', 'hard-debug', p(divergence=2, hallucination_risk=2, context_scale=2, expected_hours=1.5)),
    ('diagnose perf regression', 'hard-debug', p(divergence=2, code_complexity=1, context_scale=2, interaction_style='exploration', expected_hours=2.0)),
    ('log / data exploration', 'hard-explore', p(code_complexity=0, interaction_style='exploration', hallucination_risk=3, context_scale=2, error_cost=1, expected_hours=1.0)),
    ('research packet with scripts', 'hard-explore', p(code_complexity=1, interaction_style='exploration', hallucination_risk=3, context_scale=2, error_cost=1, expected_hours=2.0)),
    ('backtest and report metrics', 'hard-explore', p(code_complexity=1, interaction_style='exploration', hallucination_risk=3, error_cost=1, expected_hours=3.0)),
    ('web research synthesis', 'hard-text', p(code_complexity=0, interaction_style='text_only', local_only=False, hallucination_risk=3, context_scale=2, error_cost=1, expected_hours=1.0)),
    ('draft a design doc', 'hard-text', p(code_complexity=0, interaction_style='text_only', divergence=2, local_only=False, error_cost=1, expected_hours=1.0)),
    ('analyse UI screenshots for bugs', 'hard-visual', p(code_complexity=0, needs_visual=True, interaction_style='exploration', hallucination_risk=3, error_cost=1, expected_hours=0.5)),
    ('GUI workflow automation', 'hard-gui', p(code_complexity=0, needs_visual=True, needs_gui=True, expected_hours=1.0)),
    ('run tests and fix failures', 'hard-tool', p(work_kind='tool_execution', code_complexity=1, hallucination_risk=1, expected_hours=0.5)),
    ('self-contained algorithm', 'hard-eng', p(work_kind='self_contained_code', local_only=False, hallucination_risk=1, error_cost=1, expected_hours=0.5)),
    ('production release run', 'hard-ops', p(code_complexity=0, error_cost=3, hallucination_risk=2, expected_hours=0.5)),
    ('cross-module refactor', 'coupled-eng', p(reasoning_depth=3, code_complexity=3, context_scale=2, expected_hours=4.0)),
    ('concurrency bug, cause unknown', 'coupled-debug', p(reasoning_depth=3, code_complexity=2, context_scale=2, divergence=2, hallucination_risk=3, expected_hours=3.0)),
    ('protocol change across services', 'coupled-eng', p(reasoning_depth=3, code_complexity=3, context_scale=3, error_cost=3, expected_hours=6.0)),
    ('architecture review, read-only', 'coupled-review', p(reasoning_depth=3, code_complexity=0, read_only_review=True, context_scale=3, hallucination_risk=3, error_cost=1, expected_hours=2.0)),
    ('multi-stage research line', 'coupled-explore', p(reasoning_depth=3, code_complexity=1, context_scale=2, interaction_style='exploration', divergence=2, hallucination_risk=3, error_cost=1, expected_hours=4.0)),
    ('complex build plan', 'coupled-advisory', p(reasoning_depth=3, code_complexity=0, context_scale=3, interaction_style='advisory', divergence=2, hallucination_risk=3, local_only=False, expected_hours=1.5)),
    ('large feature with UI', 'coupled-eng', p(reasoning_depth=3, code_complexity=3, context_scale=2, needs_visual=True, expected_hours=5.0)),
    ('game design ideation', 'coupled-creative', p(reasoning_depth=3, code_complexity=0, interaction_style='text_only', divergence=3, local_only=False, error_cost=1, expected_hours=1.0)),
    ('long GUI task', 'coupled-gui', p(reasoning_depth=3, code_complexity=0, needs_visual=True, needs_gui=True, context_scale=2, expected_hours=2.0)),
    ('frontier architecture critique', 'frontier-advisory', p(reasoning_depth=4, code_complexity=0, context_scale=3, interaction_style='advisory', divergence=3, hallucination_risk=3, local_only=False, error_cost=2, expected_hours=1.5)),
    ('frontier strategy formulation', 'frontier-advisory', p(reasoning_depth=4, code_complexity=0, context_scale=2, interaction_style='advisory', divergence=3, hallucination_risk=3, local_only=False, error_cost=1, expected_hours=1.0)),
    ('frontier core-library rewrite', 'frontier-eng', p(reasoning_depth=4, code_complexity=3, context_scale=3, divergence=2, hallucination_risk=3, error_cost=3, expected_hours=8.0)),
    ('frontier math proof', 'frontier-text', p(reasoning_depth=4, code_complexity=0, interaction_style='text_only', divergence=3, hallucination_risk=3, local_only=False, error_cost=1, expected_hours=2.0)),
    ('frontier research synthesis', 'frontier-explore', p(reasoning_depth=4, code_complexity=1, context_scale=3, interaction_style='exploration', divergence=2, hallucination_risk=3, error_cost=1, expected_hours=4.0)),
]


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
    keep = ('fit', 'ability', 'opportunity_cost', 'overqualification', 'effort_penalty', 'free_usage', 'five_axis_match',
            'route_fit', 'vector_fit', 'subjective_fit', 'reset_bonus', 'shortfall')
    return {'id': top['id'], 'score': top['score'], **{k: c[k] for k in keep if k in c}}


def candidate(decision, model_label):
    for c in decision['ranking']:
        if label(c.get('bundle')) == model_label and c['score'] is not None:
            return c
    return None


def driver(old_dec, new_dec, old_label, new_label):
    """Which score term moved the choice: compare the new winner and the old winner inside the new ranking."""
    if old_label == new_label:
        return None
    if old_label == 'none':
        return {'kind': 'old had no eligible bundle', 'reason': 'old had no eligible bundle'}
    if new_label == 'none':
        return {'kind': 'new has no eligible bundle', 'reason': 'new has no eligible bundle'}
    o_new = candidate(new_dec, old_label)
    n_new = candidate(new_dec, new_label)
    if o_new is None:
        loser = next((c for c in new_dec['ranking'] if label(c.get('bundle')) == old_label), None)
        why = (loser or {}).get('excluded') or ['not a candidate']
        return {'kind': 'old choice excluded', 'reason': why[0]}
    terms = {}
    w = (n_new.get('_weights') or {})
    for k, sign in (('fit', 1), ('opportunity_cost', -1), ('overqualification', -1), ('effort_penalty', -1),
                    ('free_usage', 1), ('reset_bonus', 1), ('continuity', 1)):
        a, b = n_new['components'].get(k) or 0, o_new['components'].get(k) or 0
        weight = 0.4 if k == 'fit' else 1.0
        d = sign * weight * (a - b)
        if abs(d) > 1e-4:
            terms[k] = round(d, 4)
    if 'fit' in terms:
        axes = {a: round(((n_new['components'].get(a) if a != 'vision_tools' else n_new['components'].get('capability')) or 0)
                         - ((o_new['components'].get(a) if a != 'vision_tools' else o_new['components'].get('capability')) or 0), 3)
                for a in allocator.FIVE_AXES}
        demand = n_new.get('five_axis_demand') or {}
        weighted = {a: round(axes[a] * demand.get(a, 0), 3) for a in axes if demand.get(a)}
        terms['fit_axes'] = weighted
    main = max((k for k in terms if k != 'fit_axes'), key=lambda k: abs(terms[k]), default=None)
    if main == 'fit' and terms.get('fit_axes'):
        main_axis = max(terms['fit_axes'], key=lambda a: abs(terms['fit_axes'][a]))
        main = f'fit ({main_axis})'
    return {'kind': 'score', 'main': main, 'terms': terms}


def run():
    rows = []
    for fleet in FLEETS:
        accounts, pools = fleet_accounts(fleet)
        hp = sim.host_policy_for('fresh', accounts, fleet)
        variants = {name: with_algo(accounts, cfg) for name, cfg in ALGOS.items()}
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
                for name in ('new', 'new_converted'):
                    out[f'{name}_driver'] = driver(decisions['old'], decisions[name], out['old'], out[name])
                rows.append(out)
    return rows


def summarise(rows):
    summary = {}
    for name in ('old', 'new', 'new_converted'):
        summary[name] = dict(Counter(r[name] for r in rows).most_common())
    changes = {n: [r for r in rows if r[n] != r['old']] for n in ('new', 'new_converted')}
    return summary, changes


def model_of(lbl):
    return lbl.split('@')[0]


def family_split(rows, name):
    out = defaultdict(Counter)
    for r in rows:
        acct = r[name].split('@')[-1] if '@' in r[name] else 'none'
        out[r['state']][acct] += 1
    return {s: dict(c) for s, c in out.items()}


def markdown(rows):
    summary, changes = summarise(rows)
    n = len(rows)
    lines = [f'# Five-axis sampled parity ({n} decisions: {len(TASKS)} tasks x {len(STATES)} states x {len(FLEETS)} fleets)', '']
    lines.append('## Selection share (model@account)')
    lines.append('')
    labels = sorted(set().union(*[set(s) for s in summary.values()]), key=lambda k: -summary['old'].get(k, 0))
    lines.append('| model@account | old | new (strict capacity) | new (converted capacity) |')
    lines.append('| :--- | ---: | ---: | ---: |')
    for k in labels:
        lines.append(f"| {k} | {summary['old'].get(k, 0)} | {summary['new'].get(k, 0)} | {summary['new_converted'].get(k, 0)} |")
    lines.append('')
    for name in ('new', 'new_converted'):
        lines.append(f'## Account split per state ({name} vs old)')
        lines.append('')
        old, new = family_split(rows, 'old'), family_split(rows, name)
        accts = sorted({a for s in list(old.values()) + list(new.values()) for a in s})
        lines.append('| state | ' + ' | '.join(f'{a} old→new' for a in accts) + ' |')
        lines.append('| :--- | ' + ' | '.join('---:' for _ in accts) + ' |')
        for s in STATES:
            lines.append(f'| {s} | ' + ' | '.join(f"{old.get(s, {}).get(a, 0)}→{new.get(s, {}).get(a, 0)}" for a in accts) + ' |')
        lines.append('')
        lines.append(f'## Changed selections ({name}): {len(changes[name])} of {n}')
        lines.append('')
        lines.append('| fleet | state | task | old | new | main driver |')
        lines.append('| :--- | :--- | :--- | :--- | :--- | :--- |')
        for r in changes[name]:
            d = r[f'{name}_driver'] or {}
            why = d.get('main') if d.get('kind') == 'score' else (d.get('reason') if 'eligible' in d.get('kind', '') else f"old excluded: {d.get('reason')}")
            lines.append(f"| {r['fleet']} | {r['state']} | {r['task']} | {r['old']} | {r[name]} | {why} |")
        lines.append('')
    return '\n'.join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--json')
    ap.add_argument('--md')
    args = ap.parse_args()
    os.environ['ROUTER_ACCOUNTS_FILE'] = str(ROOT / 'skills' / 'agent-compass' / 'references' / 'accounts.json')
    folder = tempfile.mkdtemp(prefix='five-axis-parity-')
    try:
        sim.LIVE_PATHS, _ = sim.sandbox_state(folder)     # discovery bookkeeping lands in the throwaway state dir
        rows = run()
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1, ensure_ascii=False, default=str), encoding='utf-8')
    text = markdown(rows)
    if args.md:
        Path(args.md).write_text(text + '\n', encoding='utf-8')
    else:
        print(text)


if __name__ == '__main__':
    sys.exit(main())
