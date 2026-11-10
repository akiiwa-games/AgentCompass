"""Tier: a model's overall level, read off the arena.ai Overall board and mapped onto the user's tier scale.

`python -X utf8 tiers.py [--data DIR] [--json OUT] [model-id ...]`

The tier is not a score. It equals the arena.ai Overall position of the model (the Text Arena overall leaderboard,
`lmarena.text_overall`, the vote-based dataset: not the WebDev or Agent boards), and it has one use: estimating a type
score a model lacks (`five_axis.py`, through the fitted line of that type score on tier). Nothing routes on it.

Sources, in order (user decisions of 2026-11-11):

1. arena.ai Overall. The board is mapped onto the tier scale by piecewise-linear interpolation through the user's six
   anchor rows at their best listed variant (anchors a board orders against the user scale are pooled to one tier);
   outside the anchor range the Flash-Opus slope applies, floor 1.0. The anchors are inputs, never outputs.
2. A model that arena Overall does not list takes its relative position on another, non-arena overall (aggregate)
   leaderboard, interpolated onto the tier scale through the models listed on both that board and arena Overall:
   piecewise-linearly between the neighbouring common models (tied or inverted neighbours pooled, as the anchors are),
   beyond the ends along the map's end-to-end slope, floor 1.0. The board used is the Artificial Analysis
   Intelligence Index (`AA_INDEX`); a board enters only if it is a cited public aggregate board, never a single-skill
   benchmark. When several such boards list a model its tier is the mean of the interpolated tiers. Each reading records
   its board, date, the two neighbours used and the overlap n.
3. A model on no overall board at all takes the tier of its nearest same-series sibling (`five_axis.py`, calibration/
   series.py); with none it has no tier, and a type score it lacks is a data error (mode B), never a default.
"""
import argparse
import json
import sys
from datetime import datetime, timezone

import numpy as np

import sources

ANCHORS = {'claude-opus-5-5': 'Claude Opus 5.5', 'gpt-6-astra': 'GPT-6 Astra', 'claude-sonnet-5-5': 'Claude Sonnet 5.5',
           'gpt-6.1-sol': 'GPT-6.1 Sol', 'grok-4.7': 'Grok 4.7', 'gemini-3.8-flash-high': 'Gemini 3.8 Flash'}
# The user's judgment of the six anchor rows: the tier scale itself (calibration/README.md).
ANCHOR_TIERS = {'claude-opus-5-5': 4.6, 'gpt-6-astra': 4.2, 'claude-sonnet-5-5': 3.9, 'gpt-6.1-sol': 3.6,
                'grok-4.7': 3.4, 'gemini-3.8-flash-high': 3.0}
ANCHOR_TIER_DATE = '2026-11-04'
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
    'claude-fable-5-2': {'arena': 'Claude Fable 5.2', 'aa': 'Claude Fable 5.2'},
    'claude-haiku-5-5': {'arena': 'Claude Haiku 5.5', 'aa': 'Claude Haiku 5.5'},
    'gpt-6-luna': {'arena': 'GPT-6 Luna', 'aa': 'GPT-6 Luna'},
    'gpt-6.1-terra': {'arena': 'GPT-6.1 Terra', 'aa': 'GPT-6.1 Terra'},
    'grok-4.7-build-fast': {},
    'flint-40b-free': {},
}
# Non-arena overall (aggregate) boards that may place a model on the tier scale: id -> (label, cited url).
OVERALL_BOARDS = {'aa.index': ('Artificial Analysis Intelligence Index', 'https://artificialanalysis.ai/leaderboards/models')}


def norm(name):
    return ''.join(ch for ch in str(name).lower() if ch.isalnum())


def pooled(xs, ys):
    """Points (x ascending, tied x averaged) with y made non-decreasing by pooling adjacent violators."""
    by_x = {}
    for x, y in zip(xs, ys):
        by_x.setdefault(float(x), []).append(float(y))
    pts = sorted((x, float(np.mean(v)), len(v)) for x, v in by_x.items())
    blocks = [[y * n, n, 1] for _, y, n in pts]       # [weighted sum, weight, points in the block]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][0] / blocks[i][1] > blocks[i + 1][0] / blocks[i + 1][1]:
            nxt = blocks.pop(i + 1)
            blocks[i] = [blocks[i][0] + nxt[0], blocks[i][1] + nxt[1], blocks[i][2] + nxt[2]]
            i = max(i - 1, 0)
        else:
            i += 1
    flat = [s / w for s, w, k in blocks for _ in range(k)]
    return [p[0] for p in pts], flat


class Interpolation:
    """x -> tier by piecewise-linear interpolation through (x, tier) points; beyond the ends the map's end-to-end slope
    applies, floor 1.0. `neighbours(x)` names the two points a reading lies between (or the end segment it lies beyond)."""

    def __init__(self, xs, ys, names=None):
        order = sorted(range(len(xs)), key=lambda i: (xs[i], i))
        self.names = [names[i] for i in order] if names else None
        raw_x = [float(xs[i]) for i in order]
        raw_y = [float(ys[i]) for i in order]
        # Tied x keep one point (their mean); inverted neighbours are pooled so the map never decreases.
        self.x, self.y = pooled(raw_x, raw_y)
        self.who = None
        if self.names:
            grouped = {}
            for n, x in zip(self.names, raw_x):
                grouped.setdefault(x, []).append(n)
            self.who = [grouped[x] for x in self.x]
        self.n = len(raw_x)

    def __call__(self, x):
        xs, ys = self.x, self.y
        if len(xs) < 2:
            raise ValueError('an interpolation needs at least two distinct points')
        if x <= xs[0]:
            return max(1.0, ys[0] + self.slope * (x - xs[0]))
        if x >= xs[-1]:
            return max(1.0, ys[-1] + self.slope * (x - xs[-1]))
        return max(1.0, float(np.interp(x, xs, ys)))

    @property
    def slope(self):
        """End-to-end slope of the map (lowest to highest point), applied beyond the ends. The slope of the end segment
        alone is not used: after pooling it can be a single steep step (Artificial Analysis 59 -> 60 is 2.4 tier per
        point, which put a model five points above the top at tier 18.8)."""
        return (self.y[-1] - self.y[0]) / (self.x[-1] - self.x[0])

    def neighbours(self, x):
        """(lower point, upper point) as {'x', 'tier', 'models'}; a reading beyond an end gets the end segment."""
        xs = self.x
        j = int(np.searchsorted(xs, x, side='right'))
        j = min(max(j, 1), len(xs) - 1)
        out = []
        for k in (j - 1, j):
            point = {'x': xs[k], 'tier': round(self.y[k], 3)}
            if self.who:
                point['models'] = self.who[k]
            out.append(point)
        return out[0], out[1]


class Fitter:
    def __init__(self, target):
        read = datetime.fromtimestamp((target / 'aa-models.html').stat().st_mtime, timezone.utc).date().isoformat() \
            if (target / 'aa-models.html').exists() else None
        self.aa_read = read
        self.overall = sources.arena(target, 'text_style_control')
        self.overall_map = self.anchor_map(self.overall)
        aa = sources.artificial_analysis(target)
        self.aa = {k: max(v.values()) for k, v in aa.items()}
        self.maps = {'aa.index': self.board_map(self.aa)}

    @staticmethod
    def anchor_map(board):
        pts = [(board[name][0], ANCHOR_TIERS[k], k) for k, name in ANCHORS.items()]
        return Interpolation([p[0] for p in pts], [p[1] for p in pts], [p[2] for p in pts])

    def arena_overall_tier(self, arena_name):
        row = self.overall.get(arena_name)
        return None if row is None else float(self.overall_map(row[0]))

    def common(self, board):
        """[(board value, arena Overall tier, model name)] for the models listed on `board` and on arena Overall."""
        arena = {norm(sources.base_name(n)): n for n in self.overall}
        out = []
        for name, value in board.items():
            hit = arena.get(norm(sources.base_name(name)))
            if hit is not None:
                out.append((float(value), self.arena_overall_tier(hit), name))
        return out

    def board_map(self, board):
        pts = self.common(board)
        return Interpolation([p[0] for p in pts], [p[1] for p in pts], [p[2] for p in pts]) if len(pts) >= 3 else None

    def other_overall(self, listed):
        """{board id: reading} for the non-arena overall boards that list the model (`listed`: board id -> its name there)."""
        out = {}
        for board, mapping in self.maps.items():
            name = listed.get(board)
            if not name or mapping is None or name not in self.aa:
                continue
            x = self.aa[name]
            low, high = mapping.neighbours(x)
            out[board] = {'board': OVERALL_BOARDS[board][0], 'source': board, 'url': OVERALL_BOARDS[board][1],
                          'as_of': self.aa_read, 'listed_as': name, 'value': x, 'tier': round(float(mapping(x)), 3),
                          'neighbours': {'below': low, 'above': high}, 'n': mapping.n}
        return out


def basis_arena(row, tier):
    value, lo, hi, listed, date = row
    return (f'arena.ai Overall {date} {listed} {value:.0f} [{lo:.0f},{hi:.0f}] mapped through the six anchor rows at their '
            f'best listed variant (piecewise linear, calibration/tiers.py): tier {tier:.2f}')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('models', nargs='*')
    ap.add_argument('--data')
    ap.add_argument('--json')
    args = ap.parse_args()
    fitter = Fitter(sources.data_dir(args.data))
    for board, mapping in fitter.maps.items():
        print(f'{board}: {mapping.n if mapping else 0} models common with arena Overall')
    result = {}
    for key in args.models or list(NAMES):
        names = NAMES.get(key, {})
        arena = fitter.overall.get(names.get('arena'))
        entry = {'id': key}
        if key in ANCHOR_TIERS:
            entry.update(method='anchor', tier=ANCHOR_TIERS[key])
        elif arena:
            entry.update(method='arena_overall', tier=round(fitter.arena_overall_tier(names['arena']), 2))
        else:
            others = fitter.other_overall({'aa.index': names.get('aa')})
            entry.update(method='other_overall' if others else 'none', boards=others,
                         tier=round(float(np.mean([o['tier'] for o in others.values()])), 2) if others else None)
        result[key] = entry
        print(f"{key:30s} {entry['method']:14s} tier {entry['tier']}")
    if args.json:
        json.dump(result, open(args.json, 'w', encoding='utf-8'), indent=1, default=str)


if __name__ == '__main__':
    sys.exit(main())
