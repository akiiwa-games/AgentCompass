"""Effort curves from Artificial Analysis effort variants.

`python -X utf8 efforts.py [--data DIR] [--json OUT] [--apply]`

A model's curve, per listed effort e (calibration reads Artificial Analysis only; no tier is involved):
- `ability_mult[e]` = Intelligence Index at e / Intelligence Index at High. The router scales a model's thinking
  categories by `ability_mult[e] / ability_mult['high']` (allocator.five_axis_effort_ratio).

The cost and time of an effort are not a curve here: the time a task takes at each effort is the Artificial Analysis
Time/Task at that effort (five_axis.durations, with its series effort conversion), and the spend is that time at the
model's usd_per_hour.

`high_ratio(by_effort, listed_effort)` is the other use: arena.ai and LMArena list a model at its best variant (e.g.
"GPT-6 Luna (Max)") while the category scores mean ability at High, so a value read from those boards is moved to High by
the Intelligence Index at High over the index at the listed variant (measurements.py).
"""
import argparse
import json
import sys
from pathlib import Path

import sources

AA_NAMES = {'claude-opus-5-5': 'Claude Opus 5.5', 'gpt-6-astra': 'GPT-6 Astra', 'gpt-6.1-sol': 'GPT-6.1 Sol',
            'claude-sonnet-5-5': 'Claude Sonnet 5.5', 'grok-4.7': 'Grok 4.7', 'gemini-3.8-flash-high': 'Gemini 3.8 Flash',
            'claude-haiku-5-5': 'Claude Haiku 5.5', 'gpt-6-luna': 'GPT-6 Luna', 'gpt-6.1-terra': 'GPT-6.1 Terra',
            'deepseek-v4.1-flash-free': 'DeepSeek V4.1 Flash 1017', 'claude-fable-5-1': 'Claude Fable 5.1',
            'claude-fable-5-2': 'Claude Fable 5.2', 'gemini-4-carbon': 'Gemini 4 Carbon',
            'gemini-4-argon-high': 'Gemini 4 Argon'}
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
VARIANT_EFFORT = {'max': 'max', 'xhigh': 'xhigh', 'high': 'high', 'medium': 'medium', 'low': 'low'}


def variant_effort(listed):
    tag = sources.variant(listed).split(',')[0].strip().lower()
    return VARIANT_EFFORT.get(tag)


def high_ratio(by_effort, listed):
    """Index at High / index at the effort `listed` names, or None when either reading or the variant tag is missing."""
    effort = variant_effort(listed)
    if not effort or effort not in by_effort or 'high' not in by_effort or not by_effort[effort]:
        return None
    return by_effort['high'] / by_effort[effort]


class Efforts:
    def __init__(self, target):
        self.index = sources.artificial_analysis(target)

    def curve(self, key):
        idx = self.index.get(AA_NAMES[key], {})
        if 'high' not in idx:
            return None
        ability = {e: round(idx[e] / idx['high'], 4) for e in EFFORTS if e in idx}
        # More thinking never makes a model less able: a reading below the next lower effort's (AA noise, e.g. Fable 5.1
        # Max 49 under XHigh 50) is raised to it.
        best = None
        for e in EFFORTS:
            if e in ability:
                best = ability[e] = ability[e] if best is None else max(best, ability[e])
        return {'ability_mult': ability}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data')
    ap.add_argument('--json')
    ap.add_argument('--apply', action='store_true', help='write the curves into accounts.json and the reference fixture')
    args = ap.parse_args()
    ef = Efforts(sources.data_dir(args.data))
    out = {'curves': {}}
    for key in AA_NAMES:
        c = ef.curve(key)
        out['curves'][key] = c
        print(f'{key:26s} {c and c["ability_mult"]}')
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=1), encoding='utf-8')
    if args.apply:
        apply(out)


SOURCE = ('Artificial Analysis Intelligence Index by effort, models leaderboard '
          '(https://artificialanalysis.ai/leaderboards/models); ability_mult = index at the effort / index at High '
          '(calibration/efforts.py)')


BASIS = ('Per-model effort curves. `ability_mult` is the Artificial Analysis Intelligence Index at each effort over its index at '
         'High: the router scales the thinking categories by it (a single-effort model has 1.0 at every listed effort, so '
         'effort does not scale it). The time and spend of an effort are not here: they come from the Artificial Analysis '
         'Time/Task at that effort in five-axis-scores.json. Each entry names its `calibration_source`; recalibrate an '
         'entry by replacing its numbers and source together. Grok has no medium or max entry, Flash no low, xhigh or max.')


def apply(out):
    for path in (sources.ACCOUNTS, sources.REPO / 'scripts' / 'fixtures' / 'reference-accounts.json'):
        raw = Path(path).read_bytes().decode('utf-8')
        crlf = '\r\n' in raw
        doc = json.loads(raw)
        curves = doc['effort_curves']
        for key, c in out['curves'].items():
            if not c or key not in doc['models']:
                continue
            old = curves.get(key) or {}
            row = {'ability_mult': c['ability_mult'],
                   **({'responsiveness': old['responsiveness']} if 'responsiveness' in old else {})}
            row['calibration_source'] = SOURCE
            curves[key] = row
        curves['basis'] = BASIS
        text = json.dumps(doc, indent=2, ensure_ascii=False) + '\n'
        Path(path).write_bytes((text.replace('\n', '\r\n') if crlf else text).encode('utf-8'))


if __name__ == '__main__':
    sys.exit(main())
