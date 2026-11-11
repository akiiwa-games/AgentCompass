"""Public measurements per model, field and source: `references/public-measurements.json`.

`python -X utf8 measurements.py [--data DIR] [--out PATH]`

Every value carries its provenance (source, as_of, fetched_at, url, method). A source that was not fetched, or that
does not list a model, adds nothing: there are no defaults here. The file is the input of `five_axis.py`; the router
does not read it.

Fields
- `tier`: the model's overall level on the user's scale (`tiers.py`), never a score. One entry per source: the six
  anchor rows' own tiers (`user.anchor_tier`); the arena.ai Overall board mapped through the anchors
  (`tier.arena_overall`); and, only for a model arena Overall does not list, its relative position on another
  non-arena overall board interpolated onto the tier scale through the models both list (`tier.aa_index`, the
  Artificial Analysis Intelligence Index), with the two neighbours and the overlap n. five_axis.py combines them and
  uses the tier for one thing: estimating a type score the model lacks.
- `usd_per_hour`, one entry per (source, effort), every entry with its `kind` (`community` / `derived`):
  - `arena.agent_sessions` (community): arena.ai Agent / Overall real sessions, Cost/Task P50 / Time/Task P50 x 60.
  - `derived.community_mix` (derived, round 4 option B): AA's measured output speed per hour priced at the community
    real-traffic token mix (wokey's fixed split, about 92% cache reads; community.py) and AA's list prices, so usd per
    worker-hour is on the scale the community capacities are valued on. Cites AA and the wokey mix.
- `price`: input / output / cache-read USD per million tokens (OpenRouter, AA), for cross-checks only.
- The four categories (user, 2026-11-11, Revision 2 of the taxonomy report), one entry per (facet, source); a facet is one
  ability, its boards are parallel measurements of it (CAPABILITY_SOURCES):
  - `divergence` creative_writing: LMArena Text, Creative Writing (style control).
  - `coding` execution: arena.ai Agent / Code (Net Improvement), AA Terminal-Bench 4.0 and AA FrontierCode 2.0;
    faithfulness: the arena.ai Agent Alignment Index (acting only as asked, reporting faithfully).
  - `aesthetics` design: LMArena WebDev, and Design Arena Website, 3D Design and SVG (blind human votes; each correlates
    0.81-0.89 with WebDev over the fleet models both list, as parallel as the execution boards are with each other).
  - `tool_operation` responsiveness: AA time to first answer token per effort row, read as -ln(seconds) (a ratio scale:
    halving the time is the same step anywhere); image_recognition: AA MMMU-Pro and LMArena Vision.
  Image input: a model a vision board lists takes image input (`image_input`); a cited vendor statement that it takes
  none (`vendor.text_only`), or OpenRouter's input modalities, otherwise. A public board that lists the model wins.

One conversion for every board, a panel standard score relative to the anchor model (ANCHOR, Opus 5.5):
z = (x - x_anchor) / sd_panel(x), x the board's raw value (Elo, rating, success rate, index; -ln seconds for the time to
first answer token). The panel is the fleet models listed on every scoring board that lists all six anchor rows
(`tiers.ANCHORS`); sd_panel is the sample standard deviation of the board over the panel. A board that lists the
anchor and at least MIN_PANEL panel members is a source; any other is recorded as missing with the reason. Nothing is
capped or bridged: a new model at the top of a board moves no other model, and the anchor is 0 on every board.
Artificial Analysis boards list every effort variant: each entry carries the score at High (else the effort-less
listing) and `z_by_effort`, every listed effort on the same scale. Other boards list one variant per model (`listed_as`,
its effort in `listed_effort` when the listing names one); five_axis.py moves it to other efforts on the same facet's AA
board and links parallel boards.

Checks (`checks`, round 059 section 5): structural failures leave the (model, source) out, recorded as missing; a
statistical failure is a yellow note and the value is used.
- structural: a listing whose variant tag names something other than an effort (another harness); one board listing
  matched by two fleet rows that are neither the same model (`same_model`) nor a series; a raw value outside the
  board's range (a fraction outside 0..1, a non-positive time); two boards with identical raw vectors (the second is
  a duplicate and not used).
- statistical: on an Artificial Analysis board, High outside the range of the model's adjacent listed efforts.

`capacity`: community real-usage measurements of subscription window capacity (`community.py`, round 4): every
observation with its source, url, data date and fetch time; stated ratios between windows (`rules`); `errors` (a page
not fetched or no longer carrying its sentence gives no value); `not_found`.

usd_per_hour comes only from public sources (review F2, 2026-11-09): the arena.ai Agent sessions, and AA's measured output
speed at the community token mix. The shipped accounts values (a local 14-day calibration sample) and the community burn
figures (measured locally, no external citation) are not sources.
"""
import argparse
import json
import math
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

import arena_capture
import community
import series
import sources
import tiers

ANCHOR = 'claude-opus-5-5'      # every capability score is relative to this model (0 on every board)
MIN_PANEL = 8                   # panel members a board must list to be a source (as five_axis.MIN_FIT_POINTS)
OUT = sources.REPO / 'references' / 'public-measurements.json'
SCHEMA = 'agent-compass.public-measurements/1'

# Names the automatic matcher cannot derive (a dated or differently worded listing of the same model).
EXTRA_NAMES = {
    'beam-free': {'arena': 'Beam 1.0 501B A23B'},       # the Alignment Index lists it as `Beam` (matched automatically)
    'deepseek-v4.1-flash-free': {'aa': 'DeepSeek V4.1 Flash 1017'},
    'deepseek-v4-pro': {'aa': 'DeepSeek V4 Pro 0813', 'swe': 'DeepSeek V4 Pro 0813'},
    'muse-wonder-1.0': {'aa': 'Muse Wonder'},
    'nemotron-3-ultra-free': {'aa': 'Nemotron 3 Ultra 550B A55B'},
    'nemotron-3.5-super-vl-free': {'arena': 'NVIDIA-Nemotron-3.5-Super-VL-120B-A12B', 'aa': 'Nemotron 3.5 Super VL'},
    'qwen3.8-max': {'aa': 'Qwen3.8 Max'},
}
# Every scoring board: source -> (category, facet). Parallel boards of one facet are linked and averaged (five_axis.py).
CAPABILITY_SOURCES = {
    'lmarena.creative_writing': ('divergence', 'creative_writing'),
    'arena.agent_code': ('coding', 'execution'),
    'aa.terminalbench_4': ('coding', 'execution'),
    'aa.frontiercode_2': ('coding', 'execution'),
    'arena.agent_alignment': ('coding', 'faithfulness'),
    'lmarena.webdev': ('aesthetics', 'design'),
    'designarena.website': ('aesthetics', 'design'),
    'designarena.3d': ('aesthetics', 'design'),
    'designarena.svg': ('aesthetics', 'design'),
    'aa.ttfa': ('tool_operation', 'responsiveness'),
    'aa.mmmu_pro': ('tool_operation', 'image_recognition'),
    'lmarena.vision': ('tool_operation', 'image_recognition'),
}
AXES = {'divergence': ('creative_writing',), 'coding': ('execution', 'faithfulness'), 'aesthetics': ('design',),
        'tool_operation': ('responsiveness', 'image_recognition')}
# arena.ai boards transcribed from the user's browser (arena_capture.py): source -> (capture, category, value, method).
ARENA_CAPTURES = {
    'arena.agent_code': ('agent', 'Code', lambda r: (r.get('net_improvement') or [None])[0],
                         'arena.ai Agent / Code (Best Coding Agents), Net Improvement %, best listed variant'),
    'arena.agent_alignment': ('alignment', None, lambda r: r['alignment_index'][0],
                              'arena.ai Agent Alignment Index (Best Aligned Agents), 0-100'),
}
# LMArena boards: source -> (dataset board, category).
LMARENA = {'lmarena.creative_writing': ('text_style_control', 'creative_writing'), 'lmarena.webdev': ('webdev', 'overall'),
           'lmarena.vision': ('vision', 'overall')}
DESIGN_ARENA = {'designarena.website': 'designarena-website.html', 'designarena.3d': 'designarena-3d.html',
                'designarena.svg': 'designarena-svg.html'}
# Artificial Analysis boards read per effort: source -> sub-benchmark id of the record's `scores` (fractions).
AA_SCORE = {'aa.terminalbench_4': 'terminalbench-4-0', 'aa.frontiercode_2': 'frontiercode-2-0', 'aa.mmmu_pro': 'mmmu-pro'}
AA_TTFA = 'aa.ttfa'             # the record's `timeToFirstAnswerToken` (seconds), read as -ln(seconds)
# Value ranges of the raw readings (structural check): fractions 0..1, the Net Improvement -100..100 %, the index 0..100.
RAW_RANGE = {**dict.fromkeys(AA_SCORE, (0.0, 1.0)), 'arena.agent_code': (-100.0, 100.0),
             'arena.agent_alignment': (0.0, 100.0)}
# The kind of each source (decision 2, 2026-11-09): vendor = the vendor's own statement or price list, benchmark = a
# third-party measurement, community = a community or packaged estimate with a stated basis.
SOURCE_KIND = {**dict.fromkeys(CAPABILITY_SOURCES, 'benchmark'), 'derived.community_mix': 'derived',
               'tier.arena_overall': 'benchmark', 'tier.aa_index': 'benchmark', 'user.anchor_tier': 'user', 'vendor.text_only': 'vendor', 'aa.cost_time': 'benchmark',
               'arena.agent_sessions': 'community',
               'aa.price': 'vendor', 'openrouter.price': 'vendor', 'openrouter.modality': 'vendor',
               'vendor.fast_mode': 'vendor', 'aa.index': 'benchmark'}
IMAGE_BENCHMARKS = ('lmarena.vision', 'aa.mmmu_pro')
# Public vendor fast-mode specifications (round 11, user 2026-11-10): only models with a cited vendor price and speed
# multiplier get a fast_mode measurement; other fast-capable models have no public multiplier (surfaced gap).
VENDOR_FAST_MODE = {
    'claude-opus-5-5': {
        'price_multiplier': 2.0, 'speed_multiplier': 2.5,
        'fast_price': {'input': 8.0, 'output': 40.0}, 'standard_price': {'input': 4.0, 'output': 20.0},
        'as_of': '2026-11-10', 'url': 'https://code.claude.com/docs/en/fast-mode',
        'pricing_url': 'https://platform.claude.com/docs/en/about-claude/pricing',
        'statement': 'Anthropic Claude Code fast mode & API pricing (read 2026-11-10): Claude Opus 5.5 fast mode is '
                     '$8/$40 per MTok (2x standard $4/$20) with up to 2.5x output tokens per second.',
    },
    'claude-fable-5-2': {
        'price_multiplier': 2.0, 'speed_multiplier': 2.5,
        'fast_price': {'input': 16.0, 'output': 80.0}, 'standard_price': {'input': 8.0, 'output': 40.0},
        'as_of': '2026-11-10', 'url': 'https://code.claude.com/docs/en/fast-mode',
        'pricing_url': 'https://platform.claude.com/docs/en/about-claude/pricing',
        'statement': 'Anthropic Claude Code fast mode & API pricing (read 2026-11-10): Claude Fable 5.2 fast mode is '
                     '$16/$80 per MTok (2x standard $8/$40) with up to 2.5x output tokens per second.',
    },
}
ARENA_FILE = {**{src: f'{board}.parquet' for src, (board, _) in LMARENA.items()},
              **{src: arena_capture.FILES[spec[0]] for src, spec in ARENA_CAPTURES.items()}, **DESIGN_ARENA,
              **dict.fromkeys([*AA_SCORE, AA_TTFA], 'aa-models.html')}
EFFORT_RANK = {e: i for i, e in enumerate(('low', 'medium', 'high', 'xhigh', 'max'))}
EFFORT_TAG = re.compile(r'^(max|xhigh|high|medium|low|minimal|non-reasoning|default fallback|with fallback|thinking|\d{4}|\d{8}|\d{4}-\d{2}-\d{2})$')  # an effort, or a release date


def key(name):
    return ''.join(ch for ch in str(name).lower() if ch.isalnum())


def candidates(model):
    """Normalised spellings a source may list `model` under: the id, without an effort suffix, without a free-listing
    suffix (the same model served free), without a preview or `-1.0` suffix. Exact matches only, never fuzzy."""
    out = [model]
    for suffix in ('-high', '-medium', '-low', '-free', '-preview', '-1.0'):
        out += [m[: -len(suffix)] for m in list(out) if m.endswith(suffix)]
    return [key(m) for m in out]


class Matcher:
    def __init__(self, names, strip=None):
        self.by_key, dated = {}, {}
        for n in names:
            base = sources.base_name(n)
            s = strip(base) if strip else base
            for raw in (s, base, strip(n) if strip else n, n):
                k = key(raw)
                if k:
                    self.by_key.setdefault(k, n)
            m = re.match(r'^(.*?)[-\s]+(\d{8}|\d{4})$', str(s).strip())
            if m:
                dk = key(m.group(1))
                if dk and (dk not in dated or m.group(2) > dated[dk][0]):
                    dated[dk] = (m.group(2), n)
        for dk, (_, n) in dated.items():
            self.by_key.setdefault(dk, n)

    def find(self, model, explicit=None):
        if explicit:
            return self.by_key.get(key(explicit))       # the board's own listing of that name
        return next((self.by_key[k] for k in candidates(model) if k in self.by_key), None)


class Builder:
    def __init__(self, target, acc):
        self.target, self.acc = target, acc
        self.models = {k: v for k, v in acc['models'].items() if isinstance(v, dict)}
        self.manifest = sources.manifest(target)
        self.source_rows, self.out, self.names = {}, {m: {} for m in self.models}, {m: {} for m in self.models}
        self.missing = {}
        self.checks = []

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
        """{source: {'board': {name: raw}, 'as_of', 'method', 'strip', 'listed': {name: listed variant}, 'extra': {name:
        {...}}, and for Artificial Analysis 'by_effort': {name: {effort: raw}}}} of every scoring board fetched."""
        t, out = self.target, {}
        for src, (board, cat) in LMARENA.items():
            if not self.ok(ARENA_FILE[src]):
                self.miss(src, ARENA_FILE[src], 'not fetched'); continue
            rows = sources.arena(t, board, cat)
            out[src] = {'board': {n: r[0] for n, r in rows.items()}, 'as_of': next(iter(rows.values()))[4] if rows else None,
                        'method': f'LMArena {board}/{cat} rating, best listed variant', 'strip': None,
                        'listed': {n: r[3] for n, r in rows.items()},
                        'extra': {n: {'ci': [round(r[1], 1), round(r[2], 1)]} for n, r in rows.items()}}
        for src, (kind, category, value, method) in ARENA_CAPTURES.items():
            doc = arena_capture.read(t, kind) if self.ok(arena_capture.FILES[kind]) else None
            page = ((doc or {}).get('categories') or {}).get(category) if category else doc
            if not page:
                self.miss(src, arena_capture.FILES[kind], f'not captured: transcribe {arena_capture.URLS[kind]}'
                          + (f' ({category})' if category else '') + ' in the browser and run arena_capture.py import')
                continue
            rows = arena_capture.best_variant(page['rows'], value)
            out[src] = {'board': {n: v for n, (v, _) in rows.items()}, 'as_of': page['as_of'],
                        'method': f'{method}; transcribed {(doc or {}).get("captured_at", "")[:10]} by {(doc or {}).get("transcribed_by")}'
                                  + (f'; {page["sign_rule"]}' if page.get('sign_rule') else ''),
                        'strip': (lambda n: re.sub(r'\s+(High|Max|xHigh|Medium|Low)$', '', n)) if kind == 'alignment' else None,
                        'listed': {n: r['name'] for n, (_, r) in rows.items()}, 'extra': {}}
        for src, filename in DESIGN_ARENA.items():
            if not self.ok(filename):
                self.miss(src, filename, 'not fetched'); continue
            rows = sources.designarena(t, filename)
            if not rows:
                self.miss(src, filename, 'the page carries no main board rows'); continue
            out[src] = {'board': {r['name']: r['rating'] for r in rows.values()}, 'as_of': None,
                        'method': (f'Design Arena {sources.DESIGN_ARENA[filename][1]} board (main public board): Bradley-Terry '
                                   'rating of blind human pairwise votes; no standard error published'),
                        'strip': None, 'listed': {r['name']: r['name'] for r in rows.values()},
                        'extra': {r['name']: {'battles': r['battles'], 'designarena_id': mid} for mid, r in rows.items()}}
        if self.ok('aa-models.html'):
            variants = self.aa_variants()
            for src in [*AA_SCORE, AA_TTFA]:
                board, by_effort, listed = {}, {}, {}
                for base, recs in variants.items():
                    vals = {(eff or 'default'): (v, rec['name']) for eff, rec in recs.items()
                            if (v := self.aa_value(src, rec)) is not None}
                    if not vals:
                        continue
                    pick = next((e for e in ('high', 'default') if e in vals), None) or \
                        min(vals, key=lambda e: abs(EFFORT_RANK.get(e, 2) - EFFORT_RANK['high']))
                    board[base], listed[base] = vals[pick][0], vals[pick][1]
                    by_effort[base] = {e: round(v, 6) for e, (v, _) in vals.items()}
                what = ('time to first answer token, read as -ln(seconds)' if src == AA_TTFA else f'`{AA_SCORE[src]}`')
                out[src] = {'board': board, 'as_of': None, 'strip': None, 'listed': listed, 'extra': {},
                            'by_effort': by_effort,
                            'method': f'Artificial Analysis {what}, every listed effort; the score is the High row (else the only listing)'}
        else:
            for src in [*AA_SCORE, AA_TTFA]:
                self.miss(src, 'aa-models.html', 'not fetched')
        return out

    def aa_value(self, src, rec):
        """One AA record's raw reading on `src`, or None. A time that is not positive is out of range (a structural check)."""
        if src == AA_TTFA:
            t = rec.get('timeToFirstAnswerToken')
            if not isinstance(t, (int, float)) or isinstance(t, bool):
                return None
            if t <= 0:
                self.checks.append({'source': src, 'listed_as': rec.get('name'), 'check': 'raw_out_of_range',
                                    'level': 'structural', 'evidence': f'time to first answer token {t} s is not positive'})
                return None
            return -math.log(float(t))
        v = (rec.get('scores') or {}).get(AA_SCORE[src])
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

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
        for src, b in boards.items():
            self.register(src, ARENA_FILE[src], b['as_of'], b['method'], len(b['board']))
            match = Matcher(b['board'], b['strip'])
            family = src.split('.')[0]
            explicit_key = {'lmarena': 'arena', 'arena': 'arena', 'aa': 'aa'}.get(family)
            raw[src], names[src] = {}, {}
            for model in self.models:
                exp = ((EXTRA_NAMES.get(model) or {}).get(explicit_key) or (tiers.NAMES.get(model) or {}).get(explicit_key)
                       or (tiers.ANCHORS.get(model) if family in ('lmarena', 'arena', 'aa') else None)) if explicit_key else None
                name = match.find(model, exp) or (match.find(model) if exp else None)
                if name is not None:
                    raw[src][model], names[src][model] = b['board'][name], name
        self.structural(boards, raw, names)
        panel, core, scale = self.panel(raw)
        self.panel_doc = {'models': panel, 'n': len(panel), 'core_boards': core, 'min_panel': MIN_PANEL,
                          'rule': 'the fleet models listed on every scoring board that lists all six anchor rows '
                                  f'({", ".join(tiers.ANCHORS)}); sd_panel is the sample standard deviation over them'}
        for src, sc in scale.items():
            axis, facet = CAPABILITY_SOURCES[src]
            b = boards[src]
            self.source_rows[src]['scale'] = {'anchor': ANCHOR, 'anchor_raw': round(sc['anchor_raw'], 6),
                                              'sd_panel': round(sc['sd'], 6), 'n_panel': sc['n'], 'core': src in core}
            how = (f"z = (x - x_anchor) / sd_panel; x_anchor {sc['anchor_raw']:.6g} ({ANCHOR}), sd_panel {sc['sd']:.6g} "
                   f"over {sc['n']} panel models")
            for model, x in raw[src].items():
                name = names[src][model]
                listed = b['listed'].get(name, name)
                extra = dict(b['extra'].get(name) or {})
                if 'by_effort' in b:
                    extra['z_by_effort'] = {e: round((v - sc['anchor_raw']) / sc['sd'], 4) for e, v in b['by_effort'][name].items()}
                    extra['raw_by_effort'] = b['by_effort'][name]
                else:
                    extra['listed_effort'] = sources.aa_effort(listed) or None
                self.names[model][src] = name
                self.add(model, axis, src, round((x - sc['anchor_raw']) / sc['sd'], 4), f"{b['method']}; {how}",
                         facet=facet, listed_as=listed, raw=round(x, 6), **extra)
                if src in IMAGE_BENCHMARKS:
                    self.add(model, 'image_input', src, True, f"{b['method']}: measured on image input", listed_as=listed)
        self.conflicts = []
        for model, info in self.models.items():
            stated = info.get('text_only_source')
            if not (isinstance(stated, dict) and stated.get('url')):
                continue        # the local visual / text_only flags are routing data, not a source (review F1)
            bench_img = [e['source'] for e in self.out[model].get('image_input', []) if e.get('kind') == 'benchmark']
            if bench_img:       # a board measured image input on a model a vendor statement calls text-only
                self.conflicts.append({'model': model, 'field': 'image_input', 'reason': 'sources_disagree',
                                       'detail': f'vendor statement says text-only, but {", ".join(bench_img)} list the model',
                                       'resolution': 'the public leaderboard wins (user, 2026-11-09)'})
                continue
            self.out[model].setdefault('image_input', []).append(
                {'source': 'vendor.text_only', 'kind': 'vendor', 'value': False,
                 'as_of': stated.get('as_of'), 'fetched_at': None, 'url': stated['url'],
                 'statement': stated.get('statement'), 'method': 'vendor statement that the model takes no image input'})
        if self.ok('openrouter-models.json'):
            mods_table = sources.openrouter_modalities(self.target)
            self.register('openrouter.modality', 'openrouter-models.json', None,
                          'OpenRouter /api/v1/models architecture.input_modalities', len(mods_table))
            match = Matcher(mods_table, lambda n: n.split('/')[-1])
            for model in self.models:
                name = match.find(model)
                if not name:
                    continue
                mods = mods_table[name]
                has_img = 'image' in mods
                bench_img = [e['source'] for e in self.out[model].get('image_input', []) if e.get('kind') == 'benchmark']
                if not has_img and bench_img:
                    self.conflicts.append({'model': model, 'field': 'image_input', 'reason': 'sources_disagree',
                                           'detail': f'OpenRouter lists input_modalities={mods}, but {", ".join(bench_img)} measured image input',
                                           'resolution': 'the public leaderboard wins (user, 2026-11-09)'})
                self.add(model, 'image_input', 'openrouter.modality', has_img,
                         f'OpenRouter architecture.input_modalities ({", ".join(mods)})',
                         listed_as=name, input_modalities=mods)

    def structural(self, boards, raw, names):
        """The structural checks of the module docstring: a failing (model, source) leaves `raw`, recorded in `checks`."""
        def drop(model, src, check, evidence):
            self.checks.append({'model': model, 'source': src, 'check': check, 'level': 'structural', 'evidence': evidence})
            raw[src].pop(model, None)
            names[src].pop(model, None)
        groups, same = series.groups(self.acc), series.same_model(self.acc)
        for src, b in boards.items():
            for model, name in list(names[src].items()):
                listed = b['listed'].get(name, name)
                bad = [p for p in (q.strip().lower() for q in sources.variant(listed).split(',')) if p and not EFFORT_TAG.match(p)]
                if bad:
                    drop(model, src, 'variant_not_effort', f'listed as {listed!r}: the tag names {", ".join(bad)}, not an effort')
                    continue
                lo_hi = RAW_RANGE.get(src)
                if lo_hi and not lo_hi[0] <= raw[src][model] <= lo_hi[1]:
                    drop(model, src, 'raw_out_of_range', f'{raw[src][model]} outside {lo_hi[0]:g}..{lo_hi[1]:g}')
            by_name = {}
            for model, name in names[src].items():
                by_name.setdefault(name, []).append(model)
            for name, models in by_name.items():
                related = all(m2 in groups.get(m1, ()) or (same.get(m1) or {}).get('model') == m2
                              or (same.get(m2) or {}).get('model') == m1 for m1 in models for m2 in models if m1 != m2)
                if not related:
                    for model in models:
                        drop(model, src, 'listing_matched_twice', f'{name!r} matched {", ".join(sorted(models))}, neither the '
                                                                   'same model nor one series')
            if 'by_effort' in b:        # statistical: High between the adjacent listed efforts
                for model, name in names[src].items():
                    row = b['by_effort'].get(name) or {}
                    if 'high' in row and 'medium' in row and 'xhigh' in row and \
                            not min(row['medium'], row['xhigh']) - 1e-9 <= row['high'] <= max(row['medium'], row['xhigh']) + 1e-9:
                        self.checks.append({'model': model, 'source': src, 'check': 'high_outside_adjacent_efforts',
                                            'level': 'statistical', 'evidence': f"High {row['high']:.4g}, Medium {row['medium']:.4g}, "
                                                                                  f"xHigh {row['xhigh']:.4g}"})
        seen = {}
        for src in sorted(raw):
            vector = tuple(sorted((m, round(v, 9)) for m, v in raw[src].items()))
            if len(vector) >= 3 and vector in seen:
                self.checks.append({'source': src, 'check': 'duplicate_board', 'level': 'structural',
                                    'evidence': f'raw values identical to {seen[vector]} on {len(vector)} models'})
                self.miss(src, ARENA_FILE[src], f'not used: identical to {seen[vector]}')
                raw[src], names[src] = {}, {}
            else:
                seen.setdefault(vector, src)

    def panel(self, raw):
        """(panel models, core boards, {source: {'anchor_raw', 'sd', 'n'}}) of the module docstring."""
        core = [src for src, board in raw.items() if board and all(a in board for a in tiers.ANCHORS)]
        panel = sorted(set.intersection(*(set(raw[src]) for src in core))) if core else []
        scale = {}
        for src, board in raw.items():
            members = [m for m in panel if m in board]
            if ANCHOR not in board:
                why = f'{ANCHOR} is not listed: the board has no point the scale stands on'
            elif len(members) < MIN_PANEL:
                why = f'{len(members)} panel models listed (a source needs {MIN_PANEL})'
            elif statistics.stdev(board[m] for m in members) == 0:
                why = 'every panel model has the same value'
            else:
                scale[src] = {'anchor_raw': board[ANCHOR], 'sd': statistics.stdev(board[m] for m in members), 'n': len(members)}
                continue
            self.source_rows[src]['not_scaled'] = why
            self.miss(src, self.source_rows[src]['file'], f'not used: {why}')
        return panel, core, scale

    # --- tier -------------------------------------------------------------------------------------
    def tier(self):
        """The tier entries (module docstring)."""
        fitter = tiers.Fitter(self.target)
        overall = fitter.overall
        as_of = max(r[4] for r in overall.values())
        self.register('user.anchor_tier', 'text_style_control.parquet', tiers.ANCHOR_TIER_DATE,
                      "the user's judgment of the six anchor rows (calibration/README.md): the tier scale itself", len(tiers.ANCHORS))
        self.register('tier.arena_overall', 'text_style_control.parquet', as_of,
                      'tiers.py: arena.ai Overall (Text, style control) mapped through the six anchor rows at their best listed '
                      'variant, piecewise linear, floor 1.0', len(overall))
        self.register('tier.aa_index', 'aa-models.html', None,
                      'tiers.py: Artificial Analysis Intelligence Index position interpolated onto the tier scale through the '
                      'models listed on both it and arena.ai Overall; only for a model arena Overall does not list',
                      len(fitter.aa))
        arena, aa = Matcher(overall), Matcher(fitter.aa)
        for model in self.models:
            extra_names = EXTRA_NAMES.get(model) or {}
            names = tiers.NAMES.get(model) or {}
            if model in tiers.ANCHORS:
                self.add(model, 'tier', 'user.anchor_tier', tiers.ANCHOR_TIERS[model], 'anchor tier (calibration/README.md)')
            explicit = extra_names.get('arena') or names.get('arena') or tiers.ANCHORS.get(model)
            found = arena.find(model, explicit) or (arena.find(model) if explicit else None)
            if found:
                row = overall[found]
                if model not in tiers.ANCHORS:
                    t = fitter.arena_overall_tier(found)
                    self.add(model, 'tier', 'tier.arena_overall', round(t, 3), tiers.basis_arena(row, t), listed_as=row[3],
                             arena_value=round(row[0], 1))
            else:
                explicit = extra_names.get('aa') or names.get('aa')
                aa_found = aa.find(model, explicit) or (aa.find(model) if explicit else None)
                if aa_found:
                    for board, r in fitter.other_overall({'aa.index': aa_found}).items():
                        self.add(model, 'tier', 'tier.aa_index', r['tier'],
                                 f"{r['board']} {r['value']:g} interpolated onto the tier scale between {r['neighbours']['below']['models']} "
                                 f"and {r['neighbours']['above']['models']} of the {r['n']} models listed on both it and arena.ai Overall",
                                 listed_as=r['listed_as'], board_value=r['value'], neighbours=r['neighbours'], n=r['n'])

    # --- price and usd per hour ------------------------------------------------------------------
    def usd(self):
        if not self.ok('aa-models.html'):
            self.miss('aa.cost_time', 'aa-models.html', 'not fetched')
            return
        variants = self.aa_variants()
        self.register('aa.cost_time', 'aa-models.html', None,
                      'AA Intelligence Index Time/Task (s) and outputSpeed (tok/s) per effort variant',
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
                t = rec.get('intelligenceIndexTimePerTask')
                speed = rec.get('outputSpeed')
                if t or (isinstance(speed, (int, float)) and speed > 0):
                    self.add(model, 'throughput', 'aa.cost_time', round(t / 60.0, 3) if t else None,
                             'AA Intelligence Index Time/Task (s) and outputSpeed (tok/s)',
                             effort=eff or 'default', listed_as=rec['name'],
                             time_per_task_s=round(t, 2) if t else None,
                             time_per_task_min=round(t / 60.0, 3) if t else None,
                             output_tps=round(speed, 2) if isinstance(speed, (int, float)) and speed > 0 else None)
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
            match = Matcher(table, lambda n: n.split('/')[-1])
            for model in self.models:
                name = match.find(model)
                if name:
                    self.add(model, 'price', 'openrouter.price', {k: round(v, 4) for k, v in table[name].items()},
                             'USD per million tokens', listed_as=name)
        else:
            self.miss('openrouter.price', 'openrouter-models.json', 'not fetched')
        self.arena_usd()

    def arena_usd(self):
        """`arena.agent_sessions` usd per worker-hour (round 8, user 2026-11-09): arena.ai Agent / Overall real sessions,
        Cost/Task (P50) / Time/Task (P50) x 60 per listed model/effort variant."""
        fname = arena_capture.FILES['agent']
        if not self.ok(fname):
            self.miss('arena.agent_sessions', fname, 'not captured: arena_capture.py import ...')
            return
        doc = arena_capture.read(self.target, 'agent')
        page = ((doc or {}).get('categories') or {}).get('Overall')
        if not page or not page.get('rows'):
            self.miss('arena.agent_sessions', fname, 'Agent / Overall not in capture')
            return
        by_base = {}
        for r in page['rows']:
            if r.get('name'):
                by_base.setdefault(sources.base_name(r['name']), []).append(r)
        priced = {b: [r for r in rows if isinstance(r.get('cost_per_task_usd'), (int, float))
                      and isinstance(r.get('time_per_task_min'), (int, float)) and r['time_per_task_min'] > 0]
                  for b, rows in by_base.items()}
        self.register('arena.agent_sessions', fname, page.get('as_of'),
                      'arena.ai Agent / Overall real sessions: Cost/Task (P50) / Time/Task (P50) x 60',
                      sum(1 for rows in priced.values() if rows))
        self.source_rows['arena.agent_sessions']['sessions'] = page.get('sessions')
        self.source_rows['arena.agent_sessions']['url'] = (doc or {}).get('url') or arena_capture.URLS['agent']
        unpriced = {}
        match = Matcher(by_base)
        for model in self.models:
            exp = ((EXTRA_NAMES.get(model) or {}).get('arena') or (tiers.NAMES.get(model) or {}).get('arena')
                   or tiers.ANCHORS.get(model))
            base = match.find(model, exp) or (match.find(model) if exp else None)
            if not base:
                continue
            self.names[model]['arena.agent_sessions'] = base
            for r in by_base[base]:
                t = r.get('time_per_task_min')
                tok = r.get('output_tokens_per_task')
                if isinstance(t, (int, float)) and t > 0:
                    eff = sources.aa_effort(r['name']) or 'default'
                    self.add(model, 'throughput', 'arena.agent_sessions', round(t, 2),
                             'arena.ai Agent / Overall: Time/Task (P50) min and Output Tokens/Task (P50)',
                             effort=eff, listed_as=r['name'], time_per_task_min=round(t, 2),
                             output_tokens_per_task=round(tok) if isinstance(tok, (int, float)) else None,
                             output_tokens_per_min=round(tok / t, 1) if isinstance(tok, (int, float)) and t > 0 else None,
                             sessions=r.get('sessions'))
            if not priced[base]:
                r0 = by_base[base][0]
                missing_cols = [col for col, key_ in (('Cost/Task', 'cost_per_task_usd'), ('Time/Task', 'time_per_task_min'))
                                if not (isinstance(r0.get(key_), (int, float)) and r0[key_] > 0)]
                unpriced[model] = {'listed_as': r0['name'], 'missing': missing_cols,
                                   'reason': f"listed on arena.ai Agent / Overall as {r0['name']} without {' or '.join(missing_cols)}"}
                continue
            for r in priced[base]:
                c, t = r['cost_per_task_usd'], r['time_per_task_min']
                tok = r.get('output_tokens_per_task')
                eff = sources.aa_effort(r['name']) or 'default'
                self.add(model, 'usd_per_hour', 'arena.agent_sessions', round(c * 60.0 / t, 3),
                         'arena.ai Agent / Overall: Cost/Task (P50) / Time/Task (P50) x 60',
                         effort=eff, listed_as=r['name'], cost_per_task_usd=round(c, 4),
                         time_per_task_min=round(t, 2),
                         output_tokens_per_task=round(tok) if isinstance(tok, (int, float)) else None,
                         sessions=r.get('sessions'))
        if unpriced:
            self.source_rows['arena.agent_sessions']['unpriced'] = unpriced

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

    def fast_mode(self):
        """Cited vendor fast-mode price and speed multipliers (round 11, user 2026-11-10)."""
        self.source_rows['vendor.fast_mode'] = {
            'file': None, 'url': 'https://code.claude.com/docs/en/fast-mode', 'fetched_at': None, 'sha256': None,
            'as_of': '2026-11-10', 'as_of_kind': 'data date',
            'method': 'Anthropic Claude Code fast-mode and API pricing documentation (read 2026-11-10)',
            'models_listed': len(VENDOR_FAST_MODE), 'kind': 'vendor',
            'pricing_url': 'https://platform.claude.com/docs/en/about-claude/pricing',
        }
        for model, spec in VENDOR_FAST_MODE.items():
            if model not in self.models:
                continue
            self.add(model, 'fast_mode', 'vendor.fast_mode',
                     {'price_multiplier': spec['price_multiplier'], 'speed_multiplier': spec['speed_multiplier']},
                     spec['statement'], fast_price=spec['fast_price'], standard_price=spec['standard_price'],
                     pricing_url=spec['pricing_url'])

    def build(self):
        self.tier()
        self.capabilities()
        self.usd()
        self.fast_mode()
        capacity = self.capacity()
        self.community_mix()
        missing_sources = {**self.missing}
        for name, row in self.manifest.items():
            if row.get('status') != 'ok':
                missing_sources.setdefault(name, {'file': name, 'url': row.get('url'), 'reason': row.get('error')})
        return {'schema': SCHEMA, 'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                'conversion': f'z = (x - x_anchor) / sd_panel per board, anchor {ANCHOR}; see calibration/measurements.py',
                'panel': self.panel_doc, 'checks': self.checks,
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
    fields = ('tier', 'usd_per_hour', *AXES)
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
