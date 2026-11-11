"""Tier: a model's overall level at High, read off the arena.ai Overall board relative to the anchor.

`python -X utf8 tiers.py [--data DIR] [--json OUT] [model-id ...]`

The tier is not a score. It is the continuous standard score of the model's High (or effort-less single-configuration)
row on the arena.ai Overall leaderboard (`lmarena.text_overall`, `text_style_control` / `overall`, centered on
`claude-opus-5-5` at High = 0.0), and it has one use: estimating a capability facet a model lacks (`five_axis.py`,
through a validated `facet~tier` linear fit). Nothing routes on it.

Sources, in order:

1. arena.ai Overall at High (`tier.arena_overall`). Only a model's High row (or its effort-less single-configuration
   listing when no effort variant is named) enters; `Max`, `xHigh`, `Medium`, `Low`, and non-effort quantized tags are
   never substituted for High. Values are standardized directly as `(rating - rating_anchor) / sd_board` with no
   hand-set anchor tiers and no PAV plateau across distinct Elo ratings.
2. Artificial Analysis Intelligence Index at High (`tier.aa_index`). A model that arena Overall does not list at High
   takes its High (or effort-less single-configuration) reading on `aa.index`, aligned onto the continuous
   `arena_overall` scale over the models listed at High on both boards (monotone pooled piecewise-linear interpolation,
   end-to-end slope beyond the ends, no floor).
3. Same-series sibling (`five_axis.py`, `calibration/series.py`). A model on neither overall board at High takes the
   tier of its nearest same-series sibling; with none it has no tier, and any unmeasured facet stays missing (mode B).
"""
import argparse
import json
import re
import statistics
import sys
from datetime import datetime, timezone

import numpy as np

import sources

ANCHOR_MODEL = 'claude-opus-5-5'
# Sentinel fleet models whose shared board coverage defines the calibration panel (measurements.py).
ANCHORS = {'claude-opus-5-5': 'Claude Opus 5.5', 'gpt-6-astra': 'GPT-6 Astra', 'claude-sonnet-5-5': 'Claude Sonnet 5.5',
           'gpt-6.1-sol': 'GPT-6.1 Sol', 'grok-4.7': 'Grok 4.7', 'gemini-3.8-flash-high': 'Gemini 3.8 Flash'}
EFFORT_TAG = re.compile(
    r'^(max|xhigh|high|medium|low|minimal|non-reasoning|none|default|default fallback|with fallback|'
    r'thinking|non-thinking|reasoning|instant|thinking\s+\d+k|\d+k|\d{2,4}|\d{6}|\d{8}|'
    r'\d{2,4}-\d{2}(?:-\d{2})?|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{4})$'
)
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


def _valid_variant_tag(listed_name):
    tag = sources.variant(listed_name)
    return not [p for p in (q.strip().lower() for q in tag.split(',')) if p and not EFFORT_TAG.match(p)]


def _variant_rank_key(listed_name, value):
    tag = sources.variant(listed_name)
    date_m = re.search(r'(\d{8}|\d{6}|\d{4}-\d{2}-\d{2}|\d{4})', tag)
    date_key = date_m.group(1).replace('-', '') if date_m else ''
    thinking = 1 if re.search(r'\b(thinking|reasoning)\b', tag, re.I) and not re.search(r'\bnon-thinking\b', tag, re.I) else 0
    return (thinking, len(date_key), date_key, float(value))


def arena_high_rows(rows):
    """{base model name: (value, lower, upper, listed name, publish date)} restricted to High or effort-less rows."""
    grouped = {}
    for row in rows:
        listed = row[3]
        if not listed or not _valid_variant_tag(listed):
            continue
        eff = sources.aa_effort(listed) or ''
        if eff not in ('high', ''):
            continue
        grouped.setdefault(sources.base_name(listed), {}).setdefault(eff, []).append(row)
    out = {}
    for base, by_eff in grouped.items():
        chosen = by_eff.get('high') or by_eff.get('')
        if chosen:
            out[base] = max(chosen, key=lambda r: _variant_rank_key(r[3], r[0]))
    return out


def aa_high_index(records):
    """{base model name: intelligenceIndex} restricted strictly to High or effort-less single-configuration rows."""
    grouped = {}
    for rec in records:
        name = rec.get('name')
        idx = rec.get('intelligenceIndex')
        if not name or not isinstance(idx, (int, float)) or isinstance(idx, bool) or idx <= 0:
            continue
        if not _valid_variant_tag(name):
            continue
        eff = sources.aa_effort(name) or ''
        if eff not in ('high', ''):
            continue
        grouped.setdefault(sources.base_name(name), {})[eff] = float(idx)
    out = {}
    for base, by_eff in grouped.items():
        if 'high' in by_eff:
            out[base] = by_eff['high']
        elif '' in by_eff:
            out[base] = by_eff['']
    return out


class Interpolation:
    """x -> tier by piecewise-linear interpolation through (x, tier) points; beyond the ends the map's end-to-end slope
    applies. `neighbours(x)` names the two points a reading lies between (or the end segment it lies beyond)."""

    def __init__(self, xs, ys, names=None, floor=None):
        order = sorted(range(len(xs)), key=lambda i: (xs[i], i))
        self.names = [names[i] for i in order] if names else None
        self.floor = floor
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
            val = ys[0] + self.slope * (x - xs[0])
        elif x >= xs[-1]:
            val = ys[-1] + self.slope * (x - xs[-1])
        else:
            val = float(np.interp(x, xs, ys))
        return max(self.floor, val) if self.floor is not None else val

    @property
    def slope(self):
        """End-to-end slope of the map (lowest to highest point), applied beyond the ends."""
        return (self.y[-1] - self.y[0]) / (self.x[-1] - self.x[0])

    def neighbours(self, x):
        """(lower point, upper point) as {'x', 'tier', 'models'}; a reading beyond an end gets the end segment."""
        xs = self.x
        j = int(np.searchsorted(xs, x, side='right'))
        j = min(max(j, 1), len(xs) - 1)
        out = []
        for k in (j - 1, j):
            point = {'x': xs[k], 'tier': round(self.y[k], 4)}
            if self.who:
                point['models'] = self.who[k]
            out.append(point)
        return out[0], out[1]


class Fitter:
    def __init__(self, target):
        read = datetime.fromtimestamp((target / 'aa-models.html').stat().st_mtime, timezone.utc).date().isoformat() \
            if (target / 'aa-models.html').exists() else None
        self.aa_read = read
        self.overall = arena_high_rows(sources.arena_rows(target, 'text_style_control', 'overall'))
        self.anchor_raw, self.sd = self.overall_scale(self.overall)
        self.aa = aa_high_index(sources.aa_records(target))
        self.maps = {'aa.index': self.board_map(self.aa)}

    @staticmethod
    def overall_scale(board):
        anchor_name = ANCHORS[ANCHOR_MODEL]
        if anchor_name not in board:
            raise ValueError(f'anchor {anchor_name!r} has no High row on arena.ai Overall')
        vals = [float(r[0]) for r in board.values()]
        sd = statistics.stdev(vals) if len(vals) >= 2 else 1.0
        if sd <= 0:
            raise ValueError('arena.ai Overall High rows have zero standard deviation')
        return float(board[anchor_name][0]), float(sd)

    def arena_overall_tier(self, arena_name):
        row = self.overall.get(arena_name)
        return None if row is None else (float(row[0]) - self.anchor_raw) / self.sd

    def common(self, board):
        """[(board value, arena Overall tier, model name)] for the models listed at High on `board` and on arena Overall."""
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
                          'as_of': self.aa_read, 'listed_as': name, 'value': x, 'tier': round(float(mapping(x)), 4),
                          'neighbours': {'below': low, 'above': high}, 'n': mapping.n}
        return out


def basis_arena(row, tier, anchor_raw, sd):
    value, lo, hi, listed, date = row
    return (f'arena.ai Overall {date} {listed} {value:.1f} [{lo:.1f},{hi:.1f}] at High standardized against '
            f'{ANCHORS[ANCHOR_MODEL]} ({anchor_raw:.1f}, sd {sd:.2f}): tier {tier:.4f}')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('models', nargs='*')
    ap.add_argument('--data')
    ap.add_argument('--json')
    args = ap.parse_args()
    fitter = Fitter(sources.data_dir(args.data))
    for board, mapping in fitter.maps.items():
        print(f'{board}: {mapping.n if mapping else 0} models common with arena Overall at High')
    result = {}
    for key in args.models or list(NAMES):
        names = NAMES.get(key, {})
        explicit = names.get('arena') or ANCHORS.get(key)
        arena = fitter.overall.get(explicit) if explicit else None
        entry = {'id': key}
        if arena:
            entry.update(method='arena_overall', tier=round(fitter.arena_overall_tier(explicit), 4))
        else:
            others = fitter.other_overall({'aa.index': names.get('aa') or ANCHORS.get(key)})
            entry.update(method='other_overall' if others else 'none', boards=others,
                         tier=round(float(np.mean([o['tier'] for o in others.values()])), 4) if others else None)
        result[key] = entry
        print(f"{key:30s} {entry['method']:14s} tier {entry['tier']}")
    if args.json:
        Path_out = open(args.json, 'w', encoding='utf-8', newline='\n')
        with Path_out as fh:
            json.dump(result, fh, indent=1, default=str)


if __name__ == '__main__':
    sys.exit(main())
