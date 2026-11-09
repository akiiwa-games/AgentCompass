"""Public measurements per model, field and source: `references/public-measurements.json`.

`python -X utf8 measurements.py [--data DIR] [--out PATH]`

Every value carries its provenance (source, as_of, fetched_at, url, method). A source that was not fetched, or that
does not list a model, adds nothing: there are no defaults here. The file is the input of `five_axis.py`; the router
does not read it.

Fields
- `tier`: the tier fits of `tiers.py` (LMArena WebDev/Agent through the six anchors; SWE-bench, Terminal-Bench 2.0 and
  the AA index as secondary fits), plus the user's anchor and judgment tiers (source `user`). Kept for the old-to-new
  comparison only: the router no longer gates on a scalar tier (round 047 round 2 folds the tier into the axes).
- `usd_per_hour`, one entry per (source, effort), every entry with its `kind` (`benchmark` / `vendor` / `community`):
  - `aa.cost_time` (benchmark): Artificial Analysis cost per task x 3600 / time per task (round 046, section 2.3, method A).
  - `openrouter.cost_time` (vendor price on benchmark tokens): the AA figure repriced at the OpenRouter output price,
    only where that price differs from AA's (an equal price adds no independent evidence).
  - `derived.community_mix` (derived, round 4 option B): AA's measured output speed per hour priced at the community
    real-traffic token mix (wokey's fixed split, about 92% cache reads; community.py) and AA's list prices, so usd per
    worker-hour is on the scale the community capacities are valued on. Cites AA and the wokey mix.
- `price`: input / output / cache-read USD per million tokens (OpenRouter, AA), for cross-checks only.
- The four capability axes of the tray radar, one entry per (component, source). The user's mapping of 2026-11-09
  (round 7): the arena.ai Agent boards measure the axes their category names, captured from the user's browser
  (`arena_capture.py`; arena.ai is behind bot protection), the chat boards and the other public boards stay components:
  - `divergence`: chat (LMArena Text overall), creative (LMArena Creative Writing, EQ-Bench Creative Writing v3).
  - `global_rigor`: agentic (arena.ai Agent / Code, Net Improvement), long horizon (METR p50 time horizon, SWE-bench
    Verified 1-4 h bucket, arena.ai Alignment deceptive completion), long context (AA-LCR), anti-hallucination
    (AA Omniscience Index, arena.ai Alignment false attribution).
  - `module_precision`: coding (SWE-bench Verified <15 min bucket), terminal (AA Terminal-Bench 4.0, vals.ai
    Terminal-Bench 2.0), alignment (arena.ai Alignment Index: acting only as asked, reporting faithfully).
  - `vision_tools`: vision (LMArena Vision, AA MMMU-Pro), GUI design (LMArena WebDev), GUI (OSWorld-Verified, general
    models only; arena.ai Agent / Work), tools (AA tau2-bench, AA AutomationBench, arena.ai Agent / Work).
  The Alignment Index is split by what it measures: the index itself (unauthorized actions, false attribution and
  deceptive completion together) is module precision; of its rates, false attribution (misstating what the user said)
  is a fabricated claim, anti-hallucination, and deceptive completion (reporting unfinished work as done) is the
  long-horizon failure. arena.ai Agent / Overall and Agent / Chat are cross-checks only, not components. A model scores 0 on vision and GUI only from a cited vendor statement that it
    takes no image input (accounts row `text_only_source` {url, as_of, statement}, source `vendor.text_only`); the
    local `visual` / `text_only` routing flags are not a source (review F1, 2026-11-09), so without a statement and
    without a board the component is missing. A public board that lists the model wins over a statement.

Where the old tier came from: its primary fit is LMArena WebDev and Agent through the anchors, moved from the best
listed variant to High by the AA effort gain (efforts.high_tier). The High correction scales every LMArena component
and the arena.ai Agent category values of the model by High tier / best-variant tier (both list each model at its best
variant), so the axes are read at High as the tier was; the Alignment Index names no variant and is not corrected.

One conversion for every capability source, relative to the anchor model (ANCHOR, Opus 5.5) rather than to the board's
best, so a new model at the top of a board moves no other model's value (review F3, 2026-11-09; round 2 divided by the
board best, which an Elo board does not scale linearly): value = ANCHOR_VALUE x s / s_anchor, capped at 1. The anchor
sits at ANCHOR_VALUE (0.8) on every component it is measured on, so 1.0 is 1.25 times the anchor.
- Percent and fraction boards: s is the success rate (per difficulty bucket where the board has buckets).
- Elo boards: s / s_anchor is twice the expected win rate against the anchor, 2 / (1 + 10^((anchor - rating) / 400)).
- Net-difference boards (arena.ai Agent Net Improvement, -100..100 %; AA Omniscience Index: correct minus incorrect,
  -100..100): s is the share of positive outcomes, (1 + d / bound) / 2, a fixed map with no board term.
- arena.ai Alignment: the index is a 0-100 score (s = index / 100); its failure rates are percentages (s = 1 - rate).
- METR: s is the p50 time horizon in minutes.
- A board that does not list the anchor is bridged: its reference success is the median, over at least MIN_BRIDGE of
  our models it lists, of s x ANCHOR_VALUE / the model's anchored value on the same component (else the same axis) from
  boards that do list the anchor. With fewer shared models the board cannot be anchored and is recorded as missing.
  A bridged board's reference moves only when a model listed both on it and on an anchored mate is added (it joins
  the median); a model new to the anchored boards alone moves nothing.

`capacity`: community real-usage measurements of subscription window capacity (`community.py`, round 4): every
observation with its source, url, data date and fetch time; stated ratios between windows (`rules`); `errors` (a page
not fetched or no longer carrying its sentence gives no value); `not_found`.

usd_per_hour comes only from public sources (review F2, 2026-11-09): AA cost per task over time per task, and the same
repriced at the OpenRouter output price. The shipped accounts values (a local 14-day calibration sample) and the
community burn figures (measured locally, no external citation) are not sources.
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import arena_capture
import community
import efforts
import sources
import tiers

ANCHOR = 'claude-opus-5-5'      # every capability value is relative to this model (the router's fit targets too)
ANCHOR_VALUE = 0.8              # the anchor's value on each component; 1.0 = 1.25 x the anchor
MIN_BRIDGE = 3                  # shared models needed to bridge a board that does not list the anchor
NET_BOUND = {'arena.agent_code': 100.0, 'arena.agent_work_tools': 100.0, 'arena.agent_work_gui': 100.0,
             'aa.omniscience_index': 100.0}
OUT = sources.REPO / 'references' / 'public-measurements.json'
SCHEMA = 'agent-compass.public-measurements/1'

# Names the automatic matcher cannot derive (a dated or differently worded listing of the same model).
EXTRA_NAMES = {
    'beam-free': {'arena': 'Beam 1.0 501B A23B'},       # the Alignment Index lists it as `Beam` (matched automatically)
    'deepseek-v4.1-flash-free': {'aa': 'DeepSeek V4.1 Flash 1017'},
    'deepseek-v4-pro': {'aa': 'DeepSeek V4 Pro 0813', 'swe': 'DeepSeek V4 Pro 0813'},
    'muse-wonder-1.0': {'aa': 'Muse Wonder'},
    'nemotron-3-ultra-free': {'aa': 'Nemotron 3 Ultra 550B A55B'},
    'qwen3.8-max': {'aa': 'Qwen3.8 Max'},
}
# Tiers the user set (README: the anchors; accounts basis: the rest). Kept as source `user`, with the row's own basis.
USER_TIERS = {**{k: 'anchor tier (calibration/README.md)' for k in tiers.ANCHORS},
              'gemini-4-argon-high': 'hand-set tier (accounts basis cites AA v4.3.2; not a tiers.py fit)',
              'grok-bot': 'aligned with Opus by the user (accounts basis)',
              'dot': 'vendor statement that Dot runs on Astra (accounts basis)',
              'gpt-6-pro': 'ceiling anchor 5.0 (accounts basis)'}
USER_TIER_DATE = '2026-11-04'
# Labels for the provenance `source` of each capability measurement: (axis, component, kind).
CAPABILITY_SOURCES = {
    'lmarena.text_overall': ('divergence', 'chat', 'elo'),
    'lmarena.creative_writing': ('divergence', 'creative', 'elo'),
    'eqbench.creative_writing_v3': ('divergence', 'creative', 'elo'),
    'arena.agent_code': ('global_rigor', 'agentic', 'net'),
    'metr.time_horizon_p50': ('global_rigor', 'long_horizon', 'ratio'),
    'vals.swebench_1_4h': ('global_rigor', 'long_horizon', 'fraction'),
    'arena.agent_alignment_deceptive_completion': ('global_rigor', 'long_horizon', 'fraction'),
    'aa.lcr': ('global_rigor', 'long_context', 'fraction'),
    'aa.omniscience_index': ('global_rigor', 'anti_hallucination', 'net'),
    'arena.agent_alignment_false_attribution': ('global_rigor', 'anti_hallucination', 'fraction'),
    'vals.swebench_lt15m': ('module_precision', 'coding', 'fraction'),
    'aa.terminalbench_4': ('module_precision', 'terminal', 'fraction'),
    'vals.terminal_bench_2': ('module_precision', 'terminal', 'fraction'),
    'arena.agent_alignment': ('module_precision', 'alignment', 'fraction'),
    'lmarena.vision': ('vision_tools', 'vision', 'elo'),
    'aa.mmmu_pro': ('vision_tools', 'vision', 'fraction'),
    'lmarena.webdev': ('vision_tools', 'gui_design', 'elo'),
    'osworld.verified': ('vision_tools', 'gui', 'fraction'),
    'arena.agent_work_gui': ('vision_tools', 'gui', 'net'),
    'aa.tau2_bench': ('vision_tools', 'tools', 'fraction'),
    'aa.automationbench': ('vision_tools', 'tools', 'fraction'),
    'arena.agent_work_tools': ('vision_tools', 'tools', 'net'),
}
# arena.ai boards transcribed from the user's browser (arena_capture.py): source -> (capture, category, value, method).
ARENA_CAPTURES = {
    'arena.agent_code': ('agent', 'Code', lambda r: (r.get('net_improvement') or [None])[0],
                         'arena.ai Agent / Code (Best Coding Agents), Net Improvement %, best listed variant'),
    'arena.agent_work_tools': ('agent', 'Work', lambda r: (r.get('net_improvement') or [None])[0],
                               'arena.ai Agent / Work (Best Agents for Work), Net Improvement %, best listed variant'),
    'arena.agent_work_gui': ('agent', 'Work', lambda r: (r.get('net_improvement') or [None])[0],
                             'arena.ai Agent / Work (Best Agents for Work), Net Improvement %, best listed variant'),
    'arena.agent_alignment': ('alignment', None, lambda r: r['alignment_index'][0] / 100.0,
                              'arena.ai Agent Alignment Index (Best Aligned Agents), index / 100'),
    'arena.agent_alignment_false_attribution': ('alignment', None, lambda r: 1.0 - r['false_attribution'][0] / 100.0,
                                                'arena.ai Agent Alignment, 1 - false attribution rate'),
    'arena.agent_alignment_deceptive_completion': ('alignment', None, lambda r: 1.0 - r['deceptive_completion'][0] / 100.0,
                                                   'arena.ai Agent Alignment, 1 - deceptive completion rate'),
}
# arena.ai sources listed at the model's best variant, read at High like LMArena (the Alignment Index names no variant).
HIGH_CORRECTED = ('lmarena.', 'arena.agent_code', 'arena.agent_work_')
# The kind of each source (decision 2, 2026-11-09): vendor = the vendor's own statement or price list, benchmark = a
# third-party measurement, community = a community or packaged estimate with a stated basis.
SOURCE_KIND = {**dict.fromkeys(CAPABILITY_SOURCES, 'benchmark'), 'lmarena.tier_fit': 'benchmark', 'derived.community_mix': 'derived',
               'secondary.tier_fit': 'benchmark', 'user': 'user', 'vendor.text_only': 'vendor', 'aa.cost_time': 'benchmark',
               'openrouter.cost_time': 'vendor',
               'aa.price': 'vendor', 'openrouter.price': 'vendor', 'aa.index': 'benchmark'}
AXES = {'divergence': ('chat', 'creative'),
        'global_rigor': ('agentic', 'long_horizon', 'long_context', 'anti_hallucination'),
        'module_precision': ('coding', 'terminal', 'alignment'),
        'vision_tools': ('vision', 'gui_design', 'gui', 'tools')}
# The Omniscience Index (correct minus incorrect answers; abstaining scores 0) is the record's top-level `omniscience`. Its
# non-hallucination rate alone is not used: it rewards abstaining (Haiku 5.5, Grok 4.7 and Argon lead it, Opus 5.5 sits
# at 0.50).
AA_SCORE = {'aa.lcr': 'artificial-analysis-long-context-reasoning', 'aa.omniscience_index': 'omniscience',
            'aa.terminalbench_4': 'terminalbench-4-0', 'aa.mmmu_pro': 'mmmu-pro', 'aa.tau2_bench': 'tau2-bench',
            'aa.automationbench': 'automationbench-aa'}
FILE_OF = {'lmarena': None, 'eqbench': 'eqbench-creative-writing.js', 'metr': 'metr-benchmark-results.yaml',
           'osworld': 'osworld-verified.xlsx', 'aa': 'aa-models.html', 'openrouter': 'openrouter-models.json'}
ARENA_FILE = {'lmarena.text_overall': 'text_style_control.parquet', 'lmarena.creative_writing': 'text_style_control.parquet',
              'lmarena.webdev': 'webdev.parquet', 'lmarena.vision': 'vision.parquet',
              **{src: arena_capture.FILES[spec[0]] for src, spec in ARENA_CAPTURES.items()}}
VALS_FILE = {'vals.swebench_1_4h': 'swebench.html', 'vals.swebench_lt15m': 'swebench.html', 'vals.terminal_bench_2': 'terminal-bench-2.html'}


def key(name):
    return ''.join(ch for ch in str(name).lower() if ch.isalnum())


def candidates(model):
    """Normalised spellings a source may list `model` under: the id, without an effort suffix, without a free-listing
    suffix (the same model served free), without a preview suffix. Exact matches only, never fuzzy."""
    out = [model]
    for suffix in ('-high', '-medium', '-low', '-free', '-preview'):
        out += [m[: -len(suffix)] for m in list(out) if m.endswith(suffix)]
    return [key(m) for m in out]


class Matcher:
    def __init__(self, names, strip=None):
        self.by_key = {}
        for n in names:
            k = key(strip(n) if strip else n)
            self.by_key.setdefault(k, n)

    def find(self, model, explicit=None):
        if explicit:
            return self.by_key.get(key(explicit))       # the board's own listing of that name
        return next((self.by_key[k] for k in candidates(model) if k in self.by_key), None)


def success(kind, value, bound=1.0):
    """s on the source's own absolute scale (no board term); see the module docstring. Elo has no absolute s."""
    if kind == 'net':
        return (1 + value / bound) / 2
    return value


def relative(kind, value, ref, bound=1.0):
    """s / s_ref: `ref` is the reference rating on an Elo board, else the reference success."""
    if kind == 'elo':
        return 2 / (1 + 10 ** ((ref - value) / 400))
    return success(kind, value, bound) / ref if ref > 0 else 0.0


def anchored_value(kind, value, ref, bound=1.0):
    return round(min(1.0, max(0.0, ANCHOR_VALUE * relative(kind, value, ref, bound))), 4)


def anchor_boards(raw, kinds, comps, anchor=ANCHOR):
    """Anchor-relative values for every board (module docstring).

    raw {source: {model: raw value}}, kinds {source: (kind, bound)}, comps {source: (axis, component)}. Returns
    ({source: {model: value}}, {source: reference}, {source: why it could not be anchored})."""
    values, refs, failed = {}, {}, {}
    for src, board in raw.items():
        kind, bound = kinds[src]
        if anchor in board:
            ref = board[anchor] if kind == 'elo' else success(kind, board[anchor], bound)
            refs[src] = {'reference': round(ref, 4), 'how': f'{anchor} listed'}
            values[src] = {m: anchored_value(kind, v, ref, bound) for m, v in board.items()}
    anchored = dict(values)
    for src, board in raw.items():
        if src in anchored:
            continue
        kind, bound = kinds[src]
        axis, comp = comps[src]
        if kind == 'elo':
            failed[src] = f'{anchor} is not listed, and an Elo board is not bridged'
            continue
        for level, mates in (('component', [o for o in anchored if comps[o] == (axis, comp)]),
                             ('axis', [o for o in anchored if comps[o][0] == axis])):
            def mate(m, mates=mates):
                vals = [anchored[o][m] for o in mates if anchored[o].get(m, 0) > 0]
                return sum(vals) / len(vals) if vals else None
            shared = [m for m in board if mate(m) and success(kind, board[m], bound) > 0]
            if len(shared) >= MIN_BRIDGE:
                break
        if len(shared) < MIN_BRIDGE:
            failed[src] = (f'{anchor} is not listed, and only {len(shared)} listed models have an anchored value on the '
                           f'same {level} (a bridge needs {MIN_BRIDGE})')
            continue
        ref = float(np.median([success(kind, board[m], bound) * ANCHOR_VALUE / mate(m) for m in shared]))
        refs[src] = {'reference': round(ref, 4), 'shared': sorted(shared),
                     'how': f'bridged through {", ".join(sorted(mates))} on {len(shared)} shared models (same {level})'}
        values[src] = {m: anchored_value(kind, v, ref, bound) for m, v in board.items()}
    return values, refs, failed


def vals_date(target, page):
    m = re.search(r'Updated (\d+)/(\d+)/(\d+)', sources._text((target / page).read_text(encoding='utf-8', errors='ignore')))
    if not m:
        return None
    mo, d, y = (int(g) for g in m.groups())
    return f'{y + 2000 if y < 100 else y:04d}-{mo:02d}-{d:02d}'


class Builder:
    def __init__(self, target, acc):
        self.target, self.acc = target, acc
        self.models = {k: v for k, v in acc['models'].items() if isinstance(v, dict)}
        self.manifest = sources.manifest(target)
        self.source_rows, self.out, self.names = {}, {m: {} for m in self.models}, {m: {} for m in self.models}
        self.missing = {}

    def ok(self, filename):
        return (self.manifest.get(filename) or {}).get('status') == 'ok' and (self.target / filename).exists()

    def register(self, source, filename, as_of, method, count, note=None):
        row = self.manifest.get(filename) or {}
        self.source_rows[source] = {'file': filename, 'url': row.get('url'), 'fetched_at': row.get('fetched_at'),
                                    'sha256': row.get('sha256'), 'as_of': as_of or (row.get('fetched_at') or '')[:10],
                                    'as_of_kind': 'data date' if as_of else 'fetch date (the source publishes no data date)',
                                    'method': method, 'models_listed': count, 'kind': SOURCE_KIND.get(source, 'benchmark'),
                                    **({'note': note} if note else {})}

    def add(self, model, field, source, value, method, **extra):
        meta = self.source_rows[source]
        entry = {'source': source, 'kind': meta.get('kind', SOURCE_KIND.get(source, 'benchmark')), 'value': value,
                 'as_of': meta['as_of'], 'fetched_at': meta['fetched_at'], 'url': meta['url'], 'method': method, **extra}
        self.out[model].setdefault(field, []).append(entry)

    def miss(self, source, filename, reason):
        self.missing[source] = {'file': filename, 'reason': reason,
                                'url': (self.manifest.get(filename) or {}).get('url') or sources.PAGES.get(filename)}

    # --- capability boards ----------------------------------------------------------------------
    def boards(self):
        """{source: (board {name: raw}, as_of, method text, name-strip function)}"""
        t, out = self.target, {}
        arena_specs = {'lmarena.text_overall': ('text_style_control', 'overall'),
                       'lmarena.creative_writing': ('text_style_control', 'creative_writing'),
                       'lmarena.webdev': ('webdev', 'overall'), 'lmarena.vision': ('vision', 'overall')}
        for src, (board, cat) in arena_specs.items():
            if not self.ok(ARENA_FILE[src]):
                self.miss(src, ARENA_FILE[src], 'not fetched'); continue
            rows = sources.arena(t, board, cat)
            out[src] = ({n: r[0] for n, r in rows.items()}, next(iter(rows.values()))[4] if rows else None,
                        f'LMArena {board}/{cat}, best listed variant', None, {n: r[3] for n, r in rows.items()})
        for src, (kind, category, value, method) in ARENA_CAPTURES.items():
            doc = arena_capture.read(t, kind) if self.ok(arena_capture.FILES[kind]) else None
            page = ((doc or {}).get('categories') or {}).get(category) if category else doc
            if not page:
                self.miss(src, arena_capture.FILES[kind], f'not captured: transcribe {arena_capture.URLS[kind]}'
                          + (f' ({category})' if category else '') + ' in the browser and run arena_capture.py import')
                continue
            rows = arena_capture.best_variant(page['rows'], value)
            out[src] = ({n: v for n, (v, _) in rows.items()}, page['as_of'],
                        f'{method}; transcribed {(doc or {}).get("captured_at", "")[:10]} by {(doc or {}).get("transcribed_by")}'
                        + (f'; {page["sign_rule"]}' if page.get('sign_rule') else ''),
                        (lambda n: re.sub(r'\s+(High|Max|xHigh|Medium|Low)$', '', n)) if kind == 'alignment' else None,
                        {n: r['name'] for n, (_, r) in rows.items()})
        if self.ok('eqbench-creative-writing.js'):
            out['eqbench.creative_writing_v3'] = (sources.eqbench(t), None, 'EQ-Bench Creative Writing v3 Elo',
                                                  lambda n: n.split('/')[-1], {})
        else:
            self.miss('eqbench.creative_writing_v3', 'eqbench-creative-writing.js', 'not fetched')
        if self.ok('metr-benchmark-results.yaml'):
            out['metr.time_horizon_p50'] = (sources.metr(t), None, 'METR Horizon v1.1 p50 time horizon (minutes)',
                                            lambda n: re.sub(r'_inspect$', '', n), {})
        else:
            self.miss('metr.time_horizon_p50', 'metr-benchmark-results.yaml', 'not fetched')
        if self.ok('swebench.html'):
            _, buckets = sources.vals(t, 'swebench.html')
            date = vals_date(t, 'swebench.html')
            for src, bucket in (('vals.swebench_1_4h', '1–4hr'), ('vals.swebench_lt15m', '<15min')):
                board = {m: b[bucket][0] / b[bucket][1] for m, b in buckets.items() if bucket in b and b[bucket][1]}
                out[src] = (board, date, f'vals.ai SWE-bench Verified, {bucket} fix-time bucket, resolved share', None,
                            {m: f'{buckets[m][bucket][0]}/{buckets[m][bucket][1]}' for m in board})
        else:
            for src in ('vals.swebench_1_4h', 'vals.swebench_lt15m'):
                self.miss(src, 'swebench.html', 'not fetched')
        if self.ok('terminal-bench-2.html'):
            tb2, _ = sources.vals(t, 'terminal-bench-2.html')
            out['vals.terminal_bench_2'] = ({m: v / 100 for m, v in tb2.items()}, vals_date(t, 'terminal-bench-2.html'),
                                            'vals.ai Terminal-Bench 2.0 accuracy', None, {})
        else:
            self.miss('vals.terminal_bench_2', 'terminal-bench-2.html', 'not fetched')
        if self.ok('osworld-verified.xlsx'):
            out['osworld.verified'] = (sources.osworld(t), None,
                                       'OSWorld-Verified success rate, general models without an agent framework, best max-steps run',
                                       lambda n: re.sub(r'\[1m\]$', '', n), {})
        else:
            self.miss('osworld.verified', 'osworld-verified.xlsx', 'not fetched')
        if self.ok('aa-models.html'):
            recs = self.aa_by_base()
            for src, score in AA_SCORE.items():
                board = {}
                for base, (rec, eff) in recs.items():
                    v = rec.get(score) if score == 'omniscience' else (rec.get('scores') or {}).get(score)
                    if isinstance(v, (int, float)):
                        board[base] = float(v)
                out[src] = (board, None, f'Artificial Analysis `{score}` at the High variant (else the only listing)', None, {})
        else:
            for src in AA_SCORE:
                self.miss(src, 'aa-models.html', 'not fetched')
        return out

    def aa_variants(self):
        """{base name: {effort: record}} for AA listings whose variant tag is an effort or a date only."""
        out = {}
        for rec in sources.aa_records(self.target):
            tag = sources.variant(rec['name'])
            parts = [p.strip().lower() for p in tag.split(',') if p.strip()]
            allowed = all(p in sources.EFFORT_WORDS or p in ('default fallback', 'with fallback') or re.fullmatch(r'\d{4}', p)
                          for p in parts)
            if not allowed:
                continue
            out.setdefault(sources.base_name(rec['name']), {})[sources.aa_effort(rec['name'])] = rec
        return out

    def aa_by_base(self):
        """{base name: (record at High, else the effort-less listing, else the highest index; effort)}"""
        out = {}
        for base, by_eff in self.aa_variants().items():
            eff = 'high' if 'high' in by_eff else ('' if '' in by_eff else max(by_eff, key=lambda e: by_eff[e].get('intelligenceIndex') or 0))
            out[base] = (by_eff[eff], eff)
        return out

    def capabilities(self):
        boards, raw, names = self.boards(), {}, {}
        for src, (board, as_of, method, strip, listed) in boards.items():
            filename = ARENA_FILE.get(src) or VALS_FILE.get(src) or FILE_OF[src.split('.')[0]]
            self.register(src, filename, as_of, method, len(board))
            match = Matcher(board, strip)
            family = src.split('.')[0]
            explicit_key = {'lmarena': 'arena', 'arena': 'arena', 'aa': 'aa', 'vals': 'swe' if 'swebench' in src else 'tb2'}.get(family)
            raw[src], names[src] = {}, {}
            for model in self.models:
                exp = (EXTRA_NAMES.get(model) or {}).get(explicit_key) or (tiers.NAMES.get(model) or {}).get(explicit_key) \
                    or (tiers.ANCHORS.get(model) if family in ('lmarena', 'arena', 'aa') else None)
                name = match.find(model, exp) or (match.find(model) if exp else None)
                if name is not None:
                    raw[src][model], names[src][model] = board[name], name
        kinds = {src: (CAPABILITY_SOURCES[src][2], NET_BOUND.get(src, 1.0)) for src in raw}
        values, refs, failed = anchor_boards(raw, kinds, {src: CAPABILITY_SOURCES[src][:2] for src in raw})
        for src, why in failed.items():
            self.source_rows[src]['not_anchored'] = why
            self.miss(src, self.source_rows[src]['file'], f'not used: {why}')
        for src, board_values in values.items():
            axis, comp, _ = CAPABILITY_SOURCES[src]
            method, listed = boards[src][2], boards[src][4]
            self.source_rows[src]['anchor'] = refs[src]
            for model, value in board_values.items():
                extra = {}
                ratio = self.high_ratio.get(model) if src.startswith(HIGH_CORRECTED) else None
                if ratio is not None and ratio != 1.0:
                    # LMArena and the arena.ai Agent tables list the best variant; read it at High as the tier fit does.
                    extra = {'best_variant_value': value, 'high_ratio': ratio}
                    value = round(min(1.0, value * ratio), 4)
                name = names[src][model]
                self.names[model][src] = name
                self.add(model, axis, src, value, f'{method}; {ANCHOR_VALUE:g} x s / s_anchor ({refs[src]["how"]}), capped at 1'
                         + ('; x High/best-variant tier ratio' if extra else ''), component=comp,
                         listed_as=listed.get(name, name), raw=round(raw[src][model], 4), reference=refs[src]['reference'], **extra)
        self.conflicts = []
        for model, info in self.models.items():
            stated = info.get('text_only_source')
            if not (isinstance(stated, dict) and stated.get('url')):
                continue        # the local visual / text_only flags are routing data, not a source (review F1)
            for comp in ('vision', 'gui'):
                listed = [e['source'] for e in self.out[model].get('vision_tools', []) if e.get('component') == comp]
                if listed:      # a board measured image input on a model a vendor statement calls text-only
                    self.conflicts.append({'model': model, 'field': f'vision_tools.{comp}', 'reason': 'sources_disagree',
                                           'detail': f'vendor statement says text-only, but {", ".join(listed)} list the model',
                                           'resolution': 'the public leaderboard wins (user, 2026-11-09)'})
                    continue
                self.out[model].setdefault('vision_tools', []).append(
                    {'source': 'vendor.text_only', 'kind': 'vendor', 'value': 0.0, 'component': comp,
                     'as_of': stated.get('as_of'), 'fetched_at': None, 'url': stated['url'], 'statement': stated.get('statement'),
                     'method': 'vendor statement that the model takes no image input: vision and GUI are 0, not unknown'})

    # --- tier -------------------------------------------------------------------------------------
    def tier(self):
        ef = efforts.Efforts(self.target, self.acc)
        self.high_ratio = {}
        fitter = ef.fitter
        webdev = self.manifest.get('webdev.parquet') or {}
        self.register('lmarena.tier_fit', 'webdev.parquet', next(iter(fitter.boards['webdev'].values()))[4],
                      'tiers.py: LMArena WebDev/Agent mapped through the six anchor rows, mean of the boards', len(fitter.boards['webdev']))
        self.register('secondary.tier_fit', 'swebench.html', vals_date(self.target, 'swebench.html'),
                      'tiers.py secondary: SWE-bench Verified, Terminal-Bench 2.0 and the AA index regressed on the primary tiers', None)
        arena = Matcher(set(fitter.boards['webdev']) | set(fitter.boards['agent']))
        swe, tb2, aa = Matcher(fitter.swe), Matcher(fitter.tb2), Matcher(fitter.aa)
        for model, info in self.models.items():
            if model in tiers.ANCHORS:
                pass
            else:
                names = dict(tiers.NAMES.get(model) or {})
                for k, m in (('arena', arena), ('swe', swe), ('tb2', tb2), ('aa', aa)):
                    explicit = (EXTRA_NAMES.get(model) or {}).get(k) or names.get(k)
                    found = m.find(model, explicit) or (m.find(model) if not explicit else None)
                    if found:
                        names[k] = found
                    else:
                        names.pop(k, None)
                saved = tiers.NAMES.get(model)
                tiers.NAMES[model] = names
                try:
                    e = fitter.fit(model)
                finally:
                    if saved is None:
                        tiers.NAMES.pop(model, None)
                    else:
                        tiers.NAMES[model] = saved
                if e['method'] == 'primary':
                    high, corr = ef.high_tier(model, e['tier'])     # apply_rows.py: a best-variant (Max) fit moved to High
                    if corr and e['tier']:
                        self.high_ratio[model] = round(high / e['tier'], 4)
                    self.add(model, 'tier', 'lmarena.tier_fit', high,
                             'tiers.py primary fit' + (', moved to High by the AA effort gain (efforts.high_tier)' if corr else ''),
                             fits=e['fits'], best_variant_tier=e['tier'], high_correction=corr, listed_as=names.get('arena'))
                elif e['method'] == 'same_as':
                    self.add(model, 'tier', 'secondary.tier_fit', e['tier'], f"tiers.py SAME_AS {e['same_as']}: {e['why']}",
                             same_as=e['same_as'])
                elif e['method'] == 'secondary':
                    self.add(model, 'tier', 'secondary.tier_fit', e['tier'], 'tiers.py secondary fit',
                             fits={k: v[1] for k, v in e['secondary'].items()},
                             listed_as={k: names.get(k) for k in e['secondary']},
                             vendor_reported={k: v[4] for k, v in e['secondary'].items() if v[4]})
            if model in USER_TIERS:
                self.out[model].setdefault('tier', []).append(
                    {'source': 'user', 'value': info.get('tier'), 'as_of': USER_TIER_DATE, 'url': None, 'method': USER_TIERS[model]})

    # --- price and usd per hour ------------------------------------------------------------------
    def usd(self):
        if not self.ok('aa-models.html'):
            self.miss('aa.cost_time', 'aa-models.html', 'not fetched')
            return
        variants = self.aa_variants()
        self.register('aa.cost_time', 'aa-models.html', None,
                      'intelligenceIndexCostPerTask * 3600 / intelligenceIndexTimePerTask (audit round 046 section 2.3, method A)',
                      len(variants))
        self.register('aa.price', 'aa-models.html', None, 'AA listed price1mInputTokens / price1mOutputTokens / cacheHitPrice', len(variants))
        self.register('aa.index', 'aa-models.html', None, 'AA Intelligence Index at the High variant (else the only listing)', len(variants))
        match = Matcher(variants)
        for model in self.models:
            exp = (EXTRA_NAMES.get(model) or {}).get('aa') or (tiers.NAMES.get(model) or {}).get('aa') or tiers.ANCHORS.get(model)
            base = match.find(model, exp) or (match.find(model) if exp else None)
            if not base:
                continue
            self.names[model]['aa.cost_time'] = base
            for eff, rec in sorted(variants[base].items()):
                c, t = rec.get('intelligenceIndexCostPerTask'), rec.get('intelligenceIndexTimePerTask')
                if c and t:
                    self.add(model, 'usd_per_hour', 'aa.cost_time', round(c * 3600 / t, 3), 'cost per task x 3600 / time per task',
                             effort=eff or 'default', listed_as=rec['name'], cost_per_task=round(c, 5), time_per_task_s=round(t, 2))
            rec = variants[base].get('high') or next(iter(variants[base].values()))
            if isinstance(rec.get('intelligenceIndex'), (int, float)):
                self.add(model, 'intelligence_index', 'aa.index', round(rec['intelligenceIndex'], 2), 'AA Intelligence Index',
                         listed_as=rec['name'])
            price = {k: rec.get(src) for k, src in (('input', 'price1mInputTokens'), ('output', 'price1mOutputTokens'),
                                                    ('cache_read', 'cacheHitPrice')) if isinstance(rec.get(src), (int, float))}
            if price:
                self.add(model, 'price', 'aa.price', price, 'USD per million tokens', listed_as=rec['name'])
        if self.ok('openrouter-models.json'):
            table = sources.openrouter(self.target)
            self.register('openrouter.price', 'openrouter-models.json', None, 'OpenRouter /api/v1/models pricing (USD per million tokens)', len(table))
            self.register('openrouter.cost_time', 'openrouter-models.json', None,
                          'AA cost per task x (OpenRouter output price / AA output price) x 3600 / AA time per task', len(table))
            match = Matcher(table, lambda n: n.split('/')[-1])
            for model in self.models:
                name = match.find(model)
                if name:
                    self.add(model, 'price', 'openrouter.price', {k: round(v, 4) for k, v in table[name].items()},
                             'USD per million tokens', listed_as=name)
                    self.repriced(model, table[name], name)
        else:
            self.miss('openrouter.price', 'openrouter-models.json', 'not fetched')

    def repriced(self, model, price, listed):
        """`openrouter.cost_time`: each AA cost/time entry at the OpenRouter output price, where that price differs."""
        aa_price = next((e['value'] for e in self.out[model].get('price', []) if e['source'] == 'aa.price'), None) or {}
        ours, theirs = price.get('output'), aa_price.get('output')
        if not ours or not theirs or abs(ours / theirs - 1) < 0.01:
            return
        for e in [e for e in self.out[model].get('usd_per_hour', []) if e['source'] == 'aa.cost_time']:
            self.add(model, 'usd_per_hour', 'openrouter.cost_time', round(e['value'] * ours / theirs, 3),
                     f'AA cost/time repriced at the OpenRouter output price {ours:g} (AA lists {theirs:g}) per million tokens',
                     effort=e['effort'], listed_as=listed)

    # --- window capacity (community real usage) ---------------------------------------------------
    def capacity(self):
        rows, obs, rules, errors, mix = community.extract(self.target, self.manifest)
        self.source_rows.update(rows)
        self.mix = mix
        return {'unit': 'usd of API-priced real usage per full window', 'observations': obs, 'rules': rules,
                'exhaust': community.EXHAUST, 'exhaust_rejected': community.EXHAUST_REJECTED,
                'hourly_checks': getattr(community.extract, 'hourly', []),
                'errors': errors, 'not_found': community.NOT_FOUND, 'token_mix': mix}

    def community_mix(self):
        """`derived.community_mix` usd per worker-hour (option B, user 2026-11-09): AA's measured output speed per hour
        times the vendor list price of one output token's share of the community token mix (output + input + cache
        reads, community.token_mix). Needs the mix, the output speed and all three AA prices; else no entry."""
        mix = getattr(self, 'mix', None)
        if not mix or not self.ok('aa-models.html'):
            self.miss('derived.community_mix', 'aa-models.html', 'no community token mix' if not mix else 'not fetched')
            return
        per_out = {k: mix[k] / mix['output'] for k in ('input', 'cache_read')}
        aa = self.source_rows['aa.cost_time']
        self.source_rows['derived.community_mix'] = {
            'file': aa['file'], 'url': aa['url'], 'fetched_at': aa['fetched_at'], 'sha256': aa['sha256'],
            'as_of': min(mix['as_of'], aa['as_of']), 'as_of_kind': 'the older of the mix data date and the AA fetch date',
            'method': (f"outputSpeed x 3600 x (output price + {per_out['input']:.2f} x input price + "
                       f"{per_out['cache_read']:.1f} x cache-read price) / 1e6"),
            'models_listed': None, 'kind': 'derived',
            'citations': [{'what': 'output speed and list prices', 'source': 'aa.price', 'url': aa['url'], 'as_of': aa['as_of']},
                          {'what': 'community token mix', 'source': mix['source'], 'url': mix['url'], 'as_of': mix['as_of'],
                           'quotes': mix['quotes']}]}
        variants = self.aa_variants()
        for model, base in self.names.items():
            base = base.get('aa.cost_time')
            if not base:
                continue
            for eff, rec in sorted(variants[base].items()):
                speed = rec.get('outputSpeed')
                prices = [rec.get(k) for k in ('price1mOutputTokens', 'price1mInputTokens', 'cacheHitPrice')]
                if not isinstance(speed, (int, float)) or speed <= 0 or not all(isinstance(x, (int, float)) for x in prices):
                    continue
                p_out, p_in, p_cr = prices
                per_m_output = p_out + per_out['input'] * p_in + per_out['cache_read'] * p_cr
                self.add(model, 'usd_per_hour', 'derived.community_mix', round(speed * 3600 / 1e6 * per_m_output, 3),
                         'AA output speed x community token mix at AA list prices', effort=eff or 'default',
                         listed_as=rec['name'], output_tokens_per_hour=round(speed * 3600),
                         usd_per_million_output_at_mix=round(per_m_output, 3),
                         prices={'output': p_out, 'input': p_in, 'cache_read': p_cr}, mix_source=mix['source'],
                         mix_as_of=mix['as_of'])

    def build(self):
        self.tier()
        self.capabilities()
        self.usd()
        capacity = self.capacity()
        self.community_mix()
        missing_sources = {**self.missing}
        for name, row in self.manifest.items():
            if row.get('status') != 'ok':
                missing_sources.setdefault(name, {'file': name, 'url': row.get('url'), 'reason': row.get('error')})
        return {'schema': SCHEMA, 'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                'conversion': f'capability value = min(1, {ANCHOR_VALUE:g} x s / s_anchor), anchor {ANCHOR}; see calibration/measurements.py',
                'sources': self.source_rows, 'missing_sources': missing_sources, 'conflicts': self.conflicts,
                'axes': {a: list(c) for a, c in AXES.items()}, 'capacity': capacity,
                'models': {m: f for m, f in self.out.items()}}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data')
    ap.add_argument('--out', default=str(OUT))
    args = ap.parse_args()
    doc = Builder(sources.data_dir(args.data), sources.accounts()).build()
    Path(args.out).write_text(json.dumps(doc, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    fields = ('tier', 'usd_per_hour', 'divergence', 'global_rigor', 'module_precision', 'vision_tools')
    n = len(doc['models'])
    print(f"{n} models -> {args.out}")
    for f in fields:
        print(f'  {f:18s} {sum(1 for v in doc["models"].values() if v.get(f))}/{n} models with at least one source')
    cap = doc['capacity']
    print(f"  capacity           {len(cap['observations'])} community observations, {len(cap['rules'])} rules, "
          f"{sum(e['level'] == 'red' for e in cap['errors'])} red errors")
    for s, row in doc['missing_sources'].items():
        print(f'  missing source {s}: {row.get("reason")}')


if __name__ == '__main__':
    sys.exit(main())
