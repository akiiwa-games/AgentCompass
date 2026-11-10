"""Effort curves from Artificial Analysis effort variants, on the tier scale.

`python -X utf8 efforts.py [--data DIR] [--apply]`

- k (tier per Intelligence Index point) is the slope of the AA-to-tier regression over every model with an LMArena
  WebDev/Agent tier (tiers.Fitter.secondary['aa']). The 2026-10-09 curves used 0.1 tier per point.
- A model's curve: ability[e] = k x (AA[e] - AA[high]); burn[e] = 0.7 x cost per task[e] / cost per task[high].
- Double counting: a measured row's tier comes from LMArena at the model's best listed variant (e.g. "GPT-6 Luna (Max)"),
  while a tier means ability at High. Where AA lists both efforts, the High tier is the measured tier minus
  k x (AA[variant] - AA[high]), plus the same boost of the anchors the board map interpolates between (they are also
  listed at their best variants), so a model that responds to effort like its neighbouring anchors keeps its tier.
"""
import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np

import sources
import tiers

AA_NAMES = {'claude-opus-5-5': 'Claude Opus 5.5', 'gpt-6-astra': 'GPT-6 Astra', 'gpt-6.1-sol': 'GPT-6.1 Sol',
            'claude-sonnet-5-5': 'Claude Sonnet 5.5', 'grok-4.7': 'Grok 4.7', 'gemini-3.8-flash-high': 'Gemini 3.8 Flash',
            'claude-haiku-5-5': 'Claude Haiku 5.5', 'gpt-6-luna': 'GPT-6 Luna', 'gpt-6.1-terra': 'GPT-6.1 Terra',
            'deepseek-v4.1-flash-free': 'DeepSeek V4.1 Flash 1017', 'claude-fable-5-1': 'Claude Fable 5.1',
            'claude-fable-5-2': 'Claude Fable 5.2', 'gemini-4-carbon': 'Gemini 4 Carbon'}
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
VARIANT_EFFORT = {'max': 'max', 'xhigh': 'xhigh', 'high': 'high', 'medium': 'medium', 'low': 'low'}


def variant_effort(listed):
    tag = sources.variant(listed).split(',')[0].strip().lower()
    return VARIANT_EFFORT.get(tag)


class Efforts:
    def __init__(self, target, acc):
        self.fitter = tiers.Fitter(target, acc)
        self.k = self.fitter.secondary['aa']['b']
        self.index = sources.artificial_analysis(target)
        self.cost = sources.artificial_analysis(target, 'cost')
        self.models = acc['models']

    def curve(self, key):
        idx, cost = self.index.get(AA_NAMES[key], {}), self.cost.get(AA_NAMES[key], {})
        if 'high' not in idx:
            return None
        ability = {e: round(self.k * (idx[e] - idx['high']), 2) for e in EFFORTS if e in idx}
        # More thinking never makes a model less able: a reading below the next lower effort's (AA noise, e.g. Fable 5.1
        # Max 49 under XHigh 50) is raised to it.
        best = None
        for e in EFFORTS:
            if e in ability:
                best = ability[e] = ability[e] if best is None else max(best, ability[e])
        burn = {e: round(0.7 * cost[e] / cost['high'], 2) for e in EFFORTS if e in cost and cost.get('high')}
        return {'ability': ability, 'burn': burn or None}

    def boost(self, key, board):
        """k x (AA at the variant LMArena lists on `board` - AA at High), or None without both readings."""
        name = tiers.ANCHORS.get(key) or tiers.NAMES.get(key, {}).get('arena') or AA_NAMES.get(key)
        row = self.fitter.boards[board].get(name)
        idx = self.index.get(AA_NAMES.get(key, ''), {})
        effort = variant_effort(row[3]) if row else None
        if not effort or effort not in idx or 'high' not in idx:
            return None
        return self.k * (idx[effort] - idx['high'])

    def anchor_boost(self, board, tier):
        pts = sorted((self.models[k]['tier'], self.boost(k, board) or 0.0) for k in tiers.ANCHORS)
        return float(np.interp(tier, [p[0] for p in pts], [p[1] for p in pts]))

    def high_tier(self, key, measured):
        """(tier at High, per-board corrections) for a measured row, or (measured, {}) without effort readings."""
        corr = {}
        for board in ('webdev', 'agent'):
            b = self.boost(key, board)
            if b is not None:
                corr[board] = round(b - self.anchor_boost(board, measured), 2)
        if not corr:
            return measured, {}
        return round(measured - float(np.mean(list(corr.values()))), 2), corr


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data')
    ap.add_argument('--json')
    ap.add_argument('--apply', action='store_true', help='write the curves into accounts.json and the reference fixture')
    args = ap.parse_args()
    ef = Efforts(sources.data_dir(args.data), sources.accounts())
    print(f"k = {ef.k:.4f} tier per AA index point (r {ef.fitter.secondary['aa']['r']:.2f}, n {ef.fitter.secondary['aa']['n']}); "
          'the 2026-10-09 curves used 0.1')
    out = {'k': ef.k, 'curves': {}, 'high_tiers': {}}
    for key in AA_NAMES:
        c = ef.curve(key)
        out['curves'][key] = c
        old = (sources.accounts()['effort_curves'].get(key) or {}).get('ability')
        print(f'{key:26s} new {c and c["ability"]}  old {old}')
    for key in AA_NAMES:
        if key in tiers.ANCHORS:
            continue
        fit = ef.fitter.fit(key)
        if fit['method'] != 'primary':
            continue
        measured = fit['tier']
        high, corr = ef.high_tier(key, measured)
        out['high_tiers'][key] = (measured, high, corr)
        print(f'{key:26s} measured {measured} -> at High {high}  corrections {corr}')
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=1), encoding='utf-8')
    if args.apply:
        apply(out, ef.k)


SOURCE = ('Artificial Analysis Intelligence Index and cost per task by effort, models leaderboard read 2026-11-04; '
          'ability = {k:.4f} tier per index point x (index - index at High), the AA-to-tier slope over the models with an '
          'LMArena tier (calibration/efforts.py); burn = 0.7 x cost per task / cost at High')


def apply(out, k):
    for path in (sources.ACCOUNTS, sources.REPO / 'scripts' / 'fixtures' / 'reference-accounts.json'):
        raw = Path(path).read_bytes().decode('utf-8')
        crlf = '\r\n' in raw
        doc = json.loads(raw)
        curves = doc['effort_curves']
        for key, c in out['curves'].items():
            if not c or key not in doc['models']:
                continue
            row = {'ability': c['ability'], 'burn': c['burn'] or (curves.get(key) or {}).get('burn') or {'high': 0.7}}
            row['calibration_source'] = SOURCE.format(k=k)
            curves[key] = row
        curves['basis'] = re.sub(r'`ability` is the Artificial Analysis.*?(?=Each entry names)',
                                 '`ability` is the Artificial Analysis Intelligence Index change from High at each effort times '
                                 f'{k:.4f} tier per index point (the AA-to-tier slope over the models with an LMArena tier; the '
                                 '2026-10-09 curves used 0.1, which doubled every effort gain and loss); `burn` is 0.7 times the '
                                 'Artificial Analysis cost per task relative to High. ', curves['basis'], flags=re.S)
        text = json.dumps(doc, indent=2, ensure_ascii=False) + '\n'
        Path(path).write_bytes((text.replace('\n', '\r\n') if crlf else text).encode('utf-8'))


if __name__ == '__main__':
    sys.exit(main())
