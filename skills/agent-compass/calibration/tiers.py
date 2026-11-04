"""Measured tiers for model rows, on the scale the user's six anchor rows define.

`python -X utf8 tiers.py [--data DIR] [--json OUT] [model-id ...]`

Primary measurement (the rule every measured row follows): LMArena WebDev and Agent. Each board is mapped onto the tier
scale by piecewise-linear interpolation through the anchor rows at their best listed variant (anchors a board orders
against the user scale are pooled to one tier); outside the anchor range the Flash-Opus slope applies, floor 1.0. The
tier is the mean of the boards a model is on.

Secondary, only for a model on neither board: vals.ai SWE-bench Verified, Terminal-Bench 2.0 and the Artificial Analysis
Intelligence Index, each regressed on tier over the models that have a primary tier; the tier is the mean of the
available secondary fits, and the basis names them and their r.

The anchors are the user's judgment. They are inputs here, never outputs.
"""
import argparse
import json
import sys

import numpy as np

import sources

ANCHORS = {'claude-opus-5-5': 'Claude Opus 5.5', 'gpt-6-astra': 'GPT-6 Astra', 'claude-sonnet-5-5': 'Claude Sonnet 5.5',
           'gpt-6.1-sol': 'GPT-6.1 Sol', 'grok-4.7': 'Grok 4.7', 'gemini-3.8-flash-high': 'Gemini 3.8 Flash'}
# accounts.json model id -> the name each public source lists it under (None: not listed there).
NAMES = {
    'gpt-oss-120b': {'arena': 'gpt-oss-120b', 'swe': 'GPT OSS 120B', 'tb2': 'GPT OSS 120B', 'aa': 'gpt-oss-120b'},
    'gemini-3.1-flash-lite': {'arena': 'Gemini 3.1 Flash-Lite Preview', 'swe': 'Gemini 3.1 Flash Lite Preview',
                              'tb2': 'Gemini 3.1 Flash Lite Preview'},
    'kimi-k2.7-code': {'arena': 'Kimi K2.7 Code', 'swe': 'Kimi K2.7 Code', 'aa': 'Kimi K2.7 Code'},
    'qwen3.6-plus': {'arena': 'Qwen3.6-Plus', 'swe': 'Qwen 3.6 Plus', 'tb2': 'Qwen 3.6 Plus'},
    'claude-opus-4-7': {'arena': 'Claude Opus 4.7', 'swe': 'Claude Opus 4.7', 'tb2': 'Claude Opus 4.7'},
    'claude-opus-4-8': {'arena': 'Claude Opus 4.8', 'swe': 'Claude Opus 4.8', 'tb2': 'Claude Opus 4.8'},
    'grok-4.5': {'arena': 'Grok 4.5', 'swe': 'Grok 4.5'},
    'grok-4.6': {'arena': 'Grok 4.6', 'swe': 'Grok 4.6'},
    'gemini-3.1-pro-preview': {'arena': 'Gemini 3.1 Pro Preview', 'swe': 'Gemini 3.1 Pro Preview (02/26)',
                               'tb2': 'Gemini 3.1 Pro Preview (02/26)', 'aa': 'Gemini 3.1 Pro Preview'},
    'gemini-3.7-flash': {'arena': 'Gemini 3.7 Flash', 'swe': 'Gemini 3.7 Flash'},
    'kimi-k2.6': {'arena': 'Kimi K2.6', 'swe': 'Kimi K2.6', 'tb2': 'Kimi K2.6'},
    'qwen3-max': {'arena': 'Qwen3-Max', 'tb2': 'Qwen 3 Max'},
    'qwen3.7-max': {'arena': 'Qwen3.7-Max', 'swe': 'Qwen 3.7 Max', 'tb2': 'Qwen 3.7 Max'},
    'doubao-seed-2-1-pro-260628': {'arena': 'Seed2.1 Pro Preview'},
    'nemotron-3.5-lightning-free': {'arena': 'NVIDIA-Nemotron-3.5-Lightning-30B-A3B', 'swe': 'Nemotron 3.5 Lightning',
                                    'aa': 'Nemotron 3.5 Lightning'},
    'qwen3-coder-next': {'aa': 'Qwen3 Coder Next'},
    'gemini-4-carbon': {'arena': 'Gemini 4 Carbon', 'aa': 'Gemini 4 Carbon'},
    'claude-fable-5-1': {'arena': 'Claude Fable 5.1', 'aa': 'Claude Fable 5.1'},
    'claude-haiku-5-5': {'arena': 'Claude Haiku 5.5', 'aa': 'Claude Haiku 5.5'},
    'gpt-6-luna': {'arena': 'GPT-6 Luna', 'aa': 'GPT-6 Luna'},
    'gpt-6.1-terra': {'arena': 'GPT-6.1 Terra', 'aa': 'GPT-6.1 Terra'},
    'grok-4.7-build-fast': {},
    'flint-40b-free': {},
}
# Scores a vendor reports on the same harness as the vals.ai boards (used only when no independent board lists the model).
VENDOR = {
    'qwen3-coder-next': {'swe': (71.1, 'Qwen3-Coder-Next technical report (arXiv 2603.00729), Mini-SWE-agent'),
                         'tb2': (36.2, 'same report, Terminus 2 (json)')},
}
# The same model served differently: it takes the named row's measured tier.
SAME_AS = {
    'grok-4.7-build-fast': ('grok-4.7', 'Grok 4.7 on faster serving, available in Grok Build and Cursor (xAI launch coverage, '
                                        '2026-09-21); no board lists it separately'),
}
# Unmeasured on any comparable board (vendor scores on other benchmarks only): the data's unmeasured default applies.
UNMEASURED = {
    'flint-40b-free': 'Recursive Flint (39.8B MoE, released 2026-10-21) reports only SWE-bench Pro v2 itself; no '
                      'independent board lists it',
}
LABEL = {'webdev': 'WebDev', 'agent': 'Agent', 'vision': 'Vision', 'hard': 'Text hard prompts (style control)',
         'text': 'Text (style control)'}


class Fitter:
    def __init__(self, target, acc):
        self.models = acc['models']
        self.boards = {'webdev': sources.arena(target, 'webdev'), 'agent': sources.arena(target, 'agent'),
                       'vision': sources.arena(target, 'vision'),
                       'hard': sources.arena(target, 'text_style_control', 'hard_prompts'),
                       'text': sources.arena(target, 'text_style_control')}
        self.maps = {b: self.tier_map(b) for b in ('webdev', 'agent')}
        self.swe, _ = sources.vals(target, 'swebench.html')
        self.tb2, _ = sources.vals(target, 'terminal-bench-2.html')
        aa = sources.artificial_analysis(target)
        self.aa = {k: max(v.values()) for k, v in aa.items()}
        self.secondary = {name: self.regress(table) for name, table in (('swe', self.swe), ('tb2', self.tb2), ('aa', self.aa))}

    def tier_map(self, board):
        pts = sorted((self.boards[board][n][0], self.models[k]['tier']) for k, n in ANCHORS.items())
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        blocks = [[y, 1] for y in ys]
        i = 0
        while i < len(blocks) - 1:
            if blocks[i][0] > blocks[i + 1][0]:
                (a, n), (c, m) = blocks[i], blocks.pop(i + 1)
                blocks[i] = [(a * n + c * m) / (n + m), n + m]
                i = max(i - 1, 0)
            else:
                i += 1
        ys = [v for v, n in blocks for _ in range(n)]
        slope = (ys[-1] - ys[0]) / (xs[-1] - xs[0])

        def f(x):
            if x <= xs[0]:
                return max(1.0, ys[0] + slope * (x - xs[0]))
            if x >= xs[-1]:
                return ys[-1] + slope * (x - xs[-1])
            return float(np.interp(x, xs, ys))
        return f

    def primary(self, arena_name):
        fits = {b: self.maps[b](self.boards[b][arena_name][0]) for b in ('webdev', 'agent') if arena_name in self.boards[b]}
        return fits

    def primary_tier_by_base(self):
        """Primary tier of every model on WebDev or Agent, keyed by its Arena base name."""
        names = set(self.boards['webdev']) | set(self.boards['agent'])
        return {n: float(np.mean(list(self.primary(n).values()))) for n in names}

    def regress(self, table):
        """Linear fit tier ~ score over models listed both in `table` and on the primary boards (names normalised)."""
        key = lambda s: ''.join(ch for ch in s.lower() if ch.isalnum())
        prim = {key(n): t for n, t in self.primary_tier_by_base().items()}
        pairs = [(v, prim[key(sources.base_name(m))]) for m, v in table.items() if key(sources.base_name(m)) in prim]
        x, y = np.array([p[0] for p in pairs]), np.array([p[1] for p in pairs])
        b, a = np.polyfit(x, y, 1)
        return {'a': float(a), 'b': float(b), 'r': float(np.corrcoef(x, y)[0, 1]), 'n': len(pairs)}

    def fit(self, key):
        names = NAMES.get(key, {})
        out = {'id': key, 'sources': [], 'fits': {}}
        arena_name = names.get('arena')
        if arena_name:
            out['fits'] = {b: round(v, 2) for b, v in self.primary(arena_name).items()}
            for b in ('webdev', 'agent', 'vision', 'hard', 'text'):
                if arena_name in self.boards[b]:
                    out['sources'].append((b, self.boards[b][arena_name]))
        if out['fits']:
            out['tier'] = round(max(1.0, float(np.mean(list(out['fits'].values())))), 2)
            out['method'] = 'primary'
            return out
        if key in SAME_AS:
            target, why = SAME_AS[key]
            out.update(tier=self.models[target]['tier'], method='same_as', same_as=target, why=why)
            return out
        sec = {}
        for name, table in (('swe', self.swe), ('tb2', self.tb2), ('aa', self.aa)):
            listed = names.get(name)
            value, note = (table[listed], None) if listed and listed in table else VENDOR.get(key, {}).get(name, (None, None))
            if value is not None:
                reg = self.secondary[name]
                sec[name] = (value, round(max(1.0, reg['a'] + reg['b'] * value), 2), round(reg['r'], 2), reg['n'], note)
        out['secondary'] = sec
        if sec:
            out['tier'] = round(max(1.0, float(np.mean([v[1] for v in sec.values()]))), 2)
            out['method'] = 'secondary'
        else:
            out['method'] = 'unmeasured'
        return out


def basis(entry):
    if entry['method'] == 'unmeasured':
        return None
    if entry['method'] == 'same_as':
        return f"{entry['why']}. tier {entry['tier']} = the measured {entry['same_as']} row (calibration/tiers.py SAME_AS)."

    parts = []
    for b, (val, lo, hi, name, date) in entry['sources']:
        if b == 'agent':
            parts.append(f'{LABEL[b]} {date} {name} score {val:.3f} [{lo:.3f},{hi:.3f}]')
        else:
            parts.append(f'{LABEL[b]} {date} {name} {val:.0f} [{lo:.0f},{hi:.0f}]')
    if entry['method'] == 'primary':
        return ('LMArena: ' + '; '.join(parts) + f". tier {entry['tier']} = mean of the per-board fits {entry['fits']}; "
                'each board is mapped by piecewise-linear interpolation through the six anchor rows at their best listed '
                'variant (calibration/tiers.py).')
    board = {'swe': 'SWE-bench Verified', 'tb2': 'Terminal-Bench 2.0', 'aa': 'Artificial Analysis Intelligence Index'}
    sec = '; '.join((f'{board[k]} {v[0]:g} as reported in {v[4]}' if v[4] else f'{board[k]} {v[0]:g} (vals.ai/AA board)')
                    + f' -> tier {v[1]} (r {v[2]}, n {v[3]})' for k, v in entry['secondary'].items())
    lead = ('LMArena (not on WebDev/Agent): ' + '; '.join(parts) + '. ') if parts else ''
    return (lead + f'Secondary measurement, 2026-11-04: {sec}. tier {entry["tier"]} = mean of these fits (each floored at 1.0 like the board maps), each a linear '
            'regression of tier on the score over the models that have an LMArena WebDev/Agent tier (calibration/tiers.py).')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('models', nargs='*')
    ap.add_argument('--data')
    ap.add_argument('--json')
    args = ap.parse_args()
    fitter = Fitter(sources.data_dir(args.data), sources.accounts())
    for name, reg in fitter.secondary.items():
        print(f'secondary {name}: tier = {reg["a"]:.2f} + {reg["b"]:.4f} x score  r {reg["r"]:.2f} n {reg["n"]}')
    result = {}
    for key in args.models or list(NAMES):
        e = fitter.fit(key)
        e['basis'] = basis(e)
        result[key] = e
        print(f"{key:30s} {e['method']:10s} tier {e.get('tier', '-'):>5}  {e.get('fits') or e.get('secondary') or ''}")
    if args.json:
        json.dump(result, open(args.json, 'w', encoding='utf-8'), indent=1, default=str)


if __name__ == '__main__':
    sys.exit(main())
