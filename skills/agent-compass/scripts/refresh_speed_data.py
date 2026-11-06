"""Offline maintainer script to compute model `responsiveness` (0..1) from community speed benchmarks.

Reads a `speed-data.json` file (Artificial Analysis primary, OpenRouter SSR fallback when AA has no speed entry) or
scrapes Artificial Analysis Next.js RSC streams when invoked manually by a maintainer (`--scrape-aa-html` /
`--scrape-aa`), computes per-turn latency `T_turn = (ttft_s + L / output_tps) * harness_overhead` (`L = 400`), and maps
it onto `0..1` via `scoring.responsiveness` in `references/accounts.json` and per-effort curves in `effort_curves`.
Never makes network calls during routing.
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import urllib.request

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import allocator
import capability_catalog
import public_data


DEFAULT_ACCOUNTS_PATH = SCRIPT_DIR.parent / 'references' / 'accounts.json'
AA_LEADERBOARD_URL = 'https://artificialanalysis.ai/leaderboards/models'
AA_ARCHIVE_MODEL_URL = 'https://artificialanalysis.ai/models/kimi-k3'
EFFORT_ORDER_KEYS = ('none', 'minimal', 'default', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')
NEXT_F_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,("(?:\\.|[^"\\])*")\]\)', re.S)
RSC_LINE_RE = re.compile(r'^([0-9a-fA-F]+):(.+)$')
NAME_VARIANT_RE = re.compile(r'\(([^()]*)\)\s*$')


def infer_model_harness(model_name, accounts=None):
    """Primary subscription CLI harness for a canonical model in `accounts.json`."""
    name = str(model_name or '').lower()
    if isinstance(accounts, dict):
        for acc_name, spec in (accounts.get('accounts') or {}).items():
            for bid, bundle in (spec.get('bundles') or {}).items():
                if bundle.get('auto') or spec.get('billing') == 'metered':
                    continue
                if allocator.canonical(bundle.get('model') or '', accounts) == model_name:
                    return allocator.bundle_harness(bundle, acc_name, bid)
    if name.startswith('gemini-'):
        return 'antigravity'
    if name.startswith('claude-'):
        return 'claude'
    if name.startswith('gpt-'):
        return 'codex'
    if name.startswith('grok-'):
        return 'grok'
    if name == 'dot':
        return 'dot'
    return 'default'


def normalize_effort_label(raw_effort, entry_name=''):
    """Normalize AA / OpenRouter effort labels into router effort keys."""
    eff = str(raw_effort or '').strip().lower()
    if eff == 'none (low)':
        return 'low'
    if eff.startswith('default'):
        return 'default'
    if eff in EFFORT_ORDER_KEYS:
        return eff
    match = NAME_VARIANT_RE.search(str(entry_name or ''))
    if match:
        tokens = [t.strip().lower() for t in match.group(1).split(',') if t.strip()]
        if 'non-reasoning' in tokens and 'low' in tokens:
            return 'low'
        for tok in tokens:
            if tok == 'non-reasoning':
                return 'none'
            if tok in ('low', 'medium', 'high', 'xhigh', 'max', 'minimal'):
                return tok
    return eff or 'default'


def infer_aa_effort(row):
    """Extract normalized effort from an Artificial Analysis RSC model object."""
    eff_obj = row.get('effort')
    if isinstance(eff_obj, dict) and eff_obj.get('slug'):
        return normalize_effort_label(eff_obj.get('slug'), row.get('name') or '')
    if isinstance(eff_obj, str) and eff_obj.strip():
        return normalize_effort_label(eff_obj, row.get('name') or '')
    from_name = normalize_effort_label('', row.get('name') or '')
    if from_name != 'default':
        return from_name
    if row.get('isReasoning') is False:
        return 'none'
    return 'default'


def parse_aa_rsc_html(html_text):
    """Parse Artificial Analysis Next.js RSC stream (`self.__next_f.push([1,"..."])`) from HTML text.

    Supports both `/leaderboards/models` (`initialModels` array with top-level `outputSpeed` /
    `timeToFirstAnswerToken`) and `/models/<slug>` full model objects (`headline.outputSpeed`,
    `headline.timeToFirstAnswerToken`, `intelligenceIndexOutputTokensPerTask.reasoning`, with `$hex` dereferencing).
    Returns `{slug: {slug, name, effort, is_reasoning, output_tps, ttft_s, ttft_raw_s, reasoning_tokens_per_turn}}`.
    """
    chunks = NEXT_F_PUSH_RE.findall(str(html_text or ''))
    if not chunks:
        return {}
    pieces = []
    for raw_chunk in chunks:
        try:
            pieces.append(json.loads(raw_chunk))
        except ValueError:
            continue
    stream = ''.join(pieces)
    objects = {}
    for line in stream.splitlines():
        m = RSC_LINE_RE.match(line.strip())
        if not m:
            continue
        try:
            objects[m.group(1)] = json.loads(m.group(2))
        except ValueError:
            continue

    def deref(val, depth=0, seen=frozenset()):
        if depth > 8:
            return val
        if isinstance(val, str) and val.startswith('$') and len(val) > 1:
            ref_id = val[1:]
            if ref_id in objects and ref_id not in seen:
                return deref(objects[ref_id], depth + 1, seen | {ref_id})
            return val
        if isinstance(val, list):
            return [deref(item, depth + 1, seen) for item in val]
        if isinstance(val, dict):
            return {k: deref(v, depth + 1, seen) for k, v in val.items()}
        return val

    extracted = {}

    def visit(node):
        if isinstance(node, dict):
            slug = node.get('slug')
            name = node.get('name')
            headline = node.get('headline') if isinstance(node.get('headline'), dict) else node
            tps = headline.get('outputSpeed')
            ttft_ans = headline.get('timeToFirstAnswerToken')
            ttft_first = headline.get('timeToFirstToken')
            ttft = ttft_ans if ttft_ans is not None else ttft_first
            if (isinstance(slug, str) and slug.strip() and isinstance(name, str) and name.strip()
                    and isinstance(tps, (int, float)) and not isinstance(tps, bool) and float(tps) > 0
                    and isinstance(ttft, (int, float)) and not isinstance(ttft, bool) and float(ttft) >= 0):
                tok_block = node.get('intelligenceIndexOutputTokensPerTask')
                r_tok = tok_block.get('reasoning') if isinstance(tok_block, dict) else None
                if r_tok is None:
                    r_tok = (node.get('reasoning_output_tokens')
                             if node.get('reasoning_output_tokens') is not None
                             else headline.get('outputTokens'))
                entry = {
                    'slug': slug.strip(),
                    'name': name.strip(),
                    'effort': infer_aa_effort(node),
                    'is_reasoning': node.get('isReasoning'),
                    'output_tps': round(float(tps), 2),
                    'ttft_s': round(float(ttft), 2),
                    'ttft_raw_s': round(float(ttft_first), 2) if isinstance(ttft_first, (int, float)) else None,
                    'reasoning_tokens_per_turn': (
                        round(float(r_tok)) if isinstance(r_tok, (int, float)) and not isinstance(r_tok, bool) else None
                    ),
                }
                prev = extracted.get(entry['slug'])
                if prev is None or (prev.get('reasoning_tokens_per_turn') is None and entry['reasoning_tokens_per_turn'] is not None):
                    extracted[entry['slug']] = entry
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    for obj in objects.values():
        visit(deref(obj))
    return extracted


def fetch_aa_rsc_models(urls=(AA_LEADERBOARD_URL, AA_ARCHIVE_MODEL_URL), timeout=60):
    """Maintainer-only helper to fetch and merge AA RSC model speed records from public pages."""
    headers = {'User-Agent': 'Mozilla/5.0 (agentcompass speed calibration)'}
    merged = {}
    # Parse archive/full page first if multiple URLs are given, then overlay active leaderboard values.
    for url in reversed(list(urls)):
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html_text = resp.read().decode('utf-8', errors='ignore')
        rows = parse_aa_rsc_html(html_text)
        for slug, item in rows.items():
            if slug in merged and merged[slug].get('reasoning_tokens_per_turn') is not None and item.get('reasoning_tokens_per_turn') is None:
                item = {**item, 'reasoning_tokens_per_turn': merged[slug]['reasoning_tokens_per_turn']}
            merged[slug] = item
    return merged


def _valid_speed_entry(entry):
    if not isinstance(entry, dict):
        return False
    tps = entry.get('output_tps')
    ttft = entry.get('ttft_s')
    return (isinstance(tps, (int, float)) and not isinstance(tps, bool) and float(tps) > 0
            and isinstance(ttft, (int, float)) and not isinstance(ttft, bool) and float(ttft) >= 0)


def _normalize_input_models(speed_data, doc):
    """Normalize either `speed-data.json` (`models: {id: {status, entries, note}}`) or flat `entries: [...]`."""
    if isinstance(speed_data.get('models'), dict):
        out = {}
        for raw_model, m_row in speed_data['models'].items():
            if not isinstance(m_row, dict):
                continue
            model = allocator.canonical(str(raw_model), doc)
            out[model] = {
                'status': str(m_row.get('status') or 'ok').strip().lower(),
                'entries': [e for e in (m_row.get('entries') or []) if isinstance(e, dict)],
                'note': str(m_row.get('note') or '').strip(),
            }
        return out
    if isinstance(speed_data.get('entries'), list):
        by_model = {}
        for entry in speed_data['entries']:
            if not isinstance(entry, dict) or not entry.get('router_model'):
                continue
            model = allocator.canonical(str(entry['router_model']), doc)
            by_model.setdefault(model, {'status': 'ok', 'entries': [], 'note': ''})['entries'].append(entry)
        return by_model
    raise ValueError('speed_data must be an object with a `models` mapping or an `entries` list')


def _select_model_entries(entries):
    """Select valid speed entries for a model: Artificial Analysis is primary; OpenRouter is fallback only when AA has none."""
    valid = [e for e in entries if _valid_speed_entry(e)]
    if not valid:
        return [], None
    aa_entries = [
        e for e in valid
        if str(e.get('source') or e.get('source_id') or '').startswith('artificial_analysis')
    ]
    if aa_entries:
        return aa_entries, 'artificial_analysis'
    primary_source = str(valid[0].get('source') or valid[0].get('source_id') or 'openrouter')
    return valid, primary_source


def apply_speed_data(accounts, speed_data):
    """Return `(updated_accounts, report)` after computing per-effort `responsiveness` from `speed_data`."""
    if not isinstance(accounts, dict) or not isinstance(accounts.get('models'), dict):
        raise ValueError('accounts must be a valid accounts.json object with a models table')
    if not isinstance(speed_data, dict):
        raise ValueError('speed_data must be a JSON object')

    doc = copy.deepcopy(accounts)
    policy = doc.get('scoring') or {}
    cfg = allocator.responsiveness_config(policy)
    raw_date = str(speed_data.get('fetched_at') or speed_data.get('collected_at') or '').strip()
    fetched_date = raw_date[:10] if len(raw_date) >= 10 else (raw_date or 'unspecified-date')
    normalized_models = _normalize_input_models(speed_data, doc)

    updated_models = {}
    default_models = []
    not_applicable_models = []
    curves_table = doc.setdefault('effort_curves', {})

    for model, info in doc['models'].items():
        if model.startswith('_') or not isinstance(info, dict):
            continue
        m_row = normalized_models.get(model)
        status = m_row['status'] if m_row else 'missing'
        note = (m_row.get('note') if m_row else '') or ''

        if status == 'not_applicable':
            info['responsiveness'] = 'not_applicable'
            info['responsiveness_status'] = 'not_applicable'
            info['responsiveness_basis'] = (
                f"not_applicable ({fetched_date}): special web/packet channel outside standard token-stream "
                f"observe-act loops; excluded on realtime_loop tasks."
                + (f" {note}" if note else "")
            )
            if isinstance(curves_table.get(model), dict):
                curves_table[model].pop('responsiveness', None)
            not_applicable_models.append(model)
            continue

        selected_entries, source_kind = _select_model_entries(m_row['entries'] if m_row else [])
        if status != 'ok' or not selected_entries:
            info.pop('responsiveness', None)
            if m_row and status == 'missing':
                info['responsiveness_status'] = 'missing'
                info['responsiveness_basis'] = (
                    f"missing ({fetched_date}): no public speed measurement in Artificial Analysis or OpenRouter; "
                    f"routes with conservative default ({float(cfg.get('default', 0.52)):g})."
                    + (f" {note}" if note else "")
                )
            else:
                info.pop('responsiveness_status', None)
                info.pop('responsiveness_basis', None)
            if isinstance(curves_table.get(model), dict):
                curves_table[model].pop('responsiveness', None)
            default_models.append(model)
            continue

        info.pop('responsiveness_status', None)
        harness = infer_model_harness(model, doc)
        o_tok = float(cfg.get('output_tokens_per_turn', 400))

        by_effort = {}
        for ent in selected_entries:
            entry_name = str(ent.get('source_entry_name') or ent.get('source_name') or model).strip()
            eff = normalize_effort_label(ent.get('effort'), entry_name)
            if eff not in by_effort:
                by_effort[eff] = ent

        ordered_efforts = [e for e in EFFORT_ORDER_KEYS if e in by_effort] + [
            e for e in by_effort if e not in EFFORT_ORDER_KEYS
        ]
        effort_scores = {}
        effort_turns = {}
        effort_parts = []
        for eff in ordered_efforts:
            ent = by_effort[eff]
            turn_s = allocator.compute_turn_seconds(
                ent['ttft_s'], ent['output_tps'], effort=eff,
                output_tokens=o_tok, harness=harness, policy=policy,
            )
            score = allocator.turn_seconds_to_responsiveness(turn_s, policy)
            effort_scores[eff] = round(score, 3)
            effort_turns[eff] = round(turn_s, 2)
            effort_parts.append(
                f"{eff}: {turn_s:.2f}s->{score:.3f} "
                f"({float(ent['output_tps']):g} tok/s, ttft {float(ent['ttft_s']):g}s)"
            )

        default_effort = info.get('default_effort') or 'high'
        if default_effort in effort_scores:
            base_effort = default_effort
        elif 'default' in effort_scores:
            base_effort = 'default'
        elif 'max' in effort_scores:
            base_effort = 'max'
        else:
            base_effort = ordered_efforts[0]

        base_score = effort_scores[base_effort]
        base_turn = effort_turns[base_effort]
        primary_ent = by_effort[base_effort]
        primary_name = str(primary_ent.get('source_entry_name') or primary_ent.get('source_name') or model).strip()
        measured_date = str(
            primary_ent.get('measured_at') or primary_ent.get('accessed_at') or fetched_date
        ).strip()

        fallback_tag = (
            '; openrouter fallback (AA has no speed entry; p50_latency is first-chunk prefill latency without thinking time)'
            if source_kind == 'openrouter' else ''
        )
        basis = (
            f"{source_kind} ({primary_name}, {measured_date}): "
            f"T_turn=ttft_s+{int(o_tok)}/output_tps [{'; '.join(effort_parts)}]; "
            f"base@{base_effort}={base_turn:.2f}s -> responsiveness={base_score:.3f}"
            f"{fallback_tag}"
        )
        info['responsiveness'] = base_score
        info['responsiveness_basis'] = basis
        curves_table.setdefault(model, {})['responsiveness'] = effort_scores

        updated_models[model] = {
            'responsiveness': base_score,
            'turn_seconds': base_turn,
            'base_effort': base_effort,
            'by_effort': effort_scores,
            'turn_seconds_by_effort': effort_turns,
            'source': source_kind,
            'source_entry_name': primary_name,
            'measured_at': measured_date,
            'basis': basis,
        }

    report = {
        'fetched_at': raw_date or fetched_date,
        'updated_count': len(updated_models),
        'default_count': len(default_models),
        'not_applicable_count': len(not_applicable_models),
        'updated_models': updated_models,
        'default_models': sorted(default_models),
        'not_applicable_models': sorted(not_applicable_models),
    }
    return doc, report


def refresh_file(input_path, accounts_path=DEFAULT_ACCOUNTS_PATH, write=False, update_manifest=False):
    """Read `input_path` (`speed-data.json`), compute responsiveness on `accounts_path`, and optionally write back."""
    speed_data = json.loads(Path(input_path).read_text(encoding='utf-8-sig'))
    accounts_file = Path(accounts_path)
    accounts = json.loads(accounts_file.read_text(encoding='utf-8-sig'))
    updated_accounts, report = apply_speed_data(accounts, speed_data)
    if write:
        accounts_file.write_text(json.dumps(updated_accounts, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        if accounts_file.resolve() == DEFAULT_ACCOUNTS_PATH.resolve():
            capability_catalog.write_default_files()
        if update_manifest:
            manifest = public_data.build_manifest(accounts_file.parent)
            (accounts_file.parent / 'public-data.json').write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, help='Path to speed-data.json')
    parser.add_argument('--accounts', type=Path, default=DEFAULT_ACCOUNTS_PATH, help='Path to references/accounts.json')
    parser.add_argument('--write', action='store_true', help='Write updated responsiveness values back to accounts.json')
    parser.add_argument('--update-manifest', action='store_true',
                        help='Also update references/public-data.json sha256 manifest when --write is set')
    parser.add_argument('--scrape-aa-html', type=Path,
                        help='Offline maintainer helper: parse an Artificial Analysis HTML file RSC stream and print JSON')
    parser.add_argument('--scrape-aa', action='store_true',
                        help='Maintainer-only: fetch live Artificial Analysis RSC streams and print extracted speed rows')
    args = parser.parse_args(argv)
    if args.scrape_aa_html:
        rows = parse_aa_rsc_html(Path(args.scrape_aa_html).read_text(encoding='utf-8', errors='ignore'))
        print(json.dumps({'fetched_at': datetime.now(timezone.utc).isoformat(), 'count': len(rows), 'models': rows},
                         ensure_ascii=False, indent=2))
        return 0
    if args.scrape_aa:
        rows = fetch_aa_rsc_models()
        print(json.dumps({'fetched_at': datetime.now(timezone.utc).isoformat(), 'count': len(rows), 'models': rows},
                         ensure_ascii=False, indent=2))
        return 0
    if not args.input:
        parser.error('--input is required unless --scrape-aa-html or --scrape-aa is given')
    report = refresh_file(args.input, args.accounts, write=args.write, update_manifest=args.update_manifest)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
