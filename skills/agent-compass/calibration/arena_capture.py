"""arena.ai leaderboards transcribed from the user's own browser (round 047, round 7). arena.ai sits behind bot protection,
so these boards are not fetched: the user captures the page text and this script imports it as a dated source.

    python -X utf8 arena_capture.py import CAPTURE.json [...] [--data DIR] [--by NAME]
    python -X utf8 arena_capture.py show [--data DIR]

A capture is the JSON the browser capture writes, the same shape on every refresh:
- the Agent board's categories, {"Overall": text, "Code": text, "Chat": text, "Work": text} (https://arena.ai/leaderboard/agent),
- one page, {"url": ..., "text": ...}: the Agent Alignment Index (https://arena.ai/leaderboard/agent/alignment), WebDev
  (https://arena.ai/leaderboard/code/webdev) or Text (https://arena.ai/leaderboard/chat/text),
optionally wrapped in a JSON string. `import` parses each page (a page that does not parse is an error, never a partial
board), keeps the date the page shows (`as_of`), its session or vote count and every row, and writes
`arena-agent-categories.json` / `arena-agent-alignment.json` / `arena-webdev.json` / `arena-text.json` into the
calibration data directory with a fetch-manifest row (`transcribed_by`, `captured_at`, sha256). measurements.py reads them:

- arena.agent_code (Agent / Code, Net Improvement) -> global_rigor.agentic
- arena.agent_work_tools (Agent / Work, Net Improvement) -> vision_tools.tools (the board has no GUI column, so one type)
- arena.agent_alignment (Alignment Index) -> module_precision.alignment; its two fabrication rates go to global_rigor:
  false attribution (misreporting what the user said) -> anti_hallucination, deceptive completion (reporting unfinished
  work as done) -> long_horizon
- Agent / Overall and Agent / Chat: cross-check columns only (`show`), not components.
- WebDev and Text are read from the Hugging Face dataset (sources.py); their captures are a cross-check of it.

Each Agent row also carries the median cost and time per task of real sessions, a community usd-per-hour cross-check
(`show`; not a usd source).
"""
import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import sources

FILES = {'agent': 'arena-agent-categories.json', 'alignment': 'arena-agent-alignment.json',
         'webdev': 'arena-webdev.json', 'text': 'arena-text.json'}
URLS = {'agent': 'https://arena.ai/leaderboard/agent', 'alignment': 'https://arena.ai/leaderboard/agent/alignment',
        'webdev': 'https://arena.ai/leaderboard/code/webdev', 'text': 'https://arena.ai/leaderboard/chat/text'}
AGENT_COLUMNS = ('net_improvement', 'confirmed_success', 'praise_vs_complaint', 'steerability', 'bash_reliability',
                 'tool_hallucination')
MONTHS = {m: i for i, m in enumerate(('january', 'february', 'march', 'april', 'may', 'june', 'july', 'august', 'september',
                                      'october', 'november', 'december'), 1)}
# Lab names the run-together Alignment page glues to the model name (the Agent page prints them on their own line).
LABS = ('Anthropic', 'OpenAI', 'Google', 'Meta', 'SpaceXAI', 'xAI', 'Tencent', 'Moonshot', 'DeepSeek', 'Xiaomi', 'Alibaba',
        'Z.ai', 'StepFun', 'Reflection AI', 'Thinking Machines', 'Thinky', 'MiniMax', 'Mistral', 'NVIDIA', 'Amazon', 'Microsoft', 'ByteDance',
        'Baidu', 'Cohere', 'IBM', 'Meituan', 'Ant Group', 'Kuaishou', 'Prime Intellect', 'Nous Research', 'AI21')


def page_date(text):
    m = re.search(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? (\d{1,2}), (\d{4})', text)
    if not m:
        raise ValueError('no page date')
    month = next(i for name, i in MONTHS.items() if name.startswith(m.group(1).lower()))
    return f'{int(m.group(3)):04d}-{month:02d}-{int(m.group(2)):02d}'


def count(text, word):
    text = re.sub(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? \d{1,2}, \d{4}', ' ', text)   # runs into the count
    m = re.search(r'(\d[\d,]*) ' + word, text)
    return int(m.group(1).replace(',', '')) if m else None


def number(cell):
    cell = cell.strip()
    if cell in ('', 'N/A', '-'):
        return None
    m = re.fullmatch(r'\$?([\d,.]+)\s*(K|M|min|h|s)?', cell)
    if not m:
        return None
    v = float(m.group(1).replace(',', ''))
    return v * {'K': 1e3, 'M': 1e6, 'h': 60.0, 's': 1 / 60}.get(m.group(2), 1.0)


def parse_agent(text):
    """One Agent category page: {as_of, sessions, rows: [{rank, name, lab, preliminary, <column>: (value, ci), sessions,
    cost_per_task_usd, time_per_task_min, output_tokens_per_task}]}."""
    head = text.split('Price $/M', 1)
    if len(head) != 2:
        raise ValueError('no Agent table header')
    body = head[1]
    row = re.compile(r'(?m)^(\d+)\n\d+\n\d+\n\t\n(?P<name>[^\n]+)\n(?P<lab>[^\n]+?)\n(?P<pre>·\nPreliminary\n)?\t\n'
                     r'(?P<cells>(?:(?:-?[\d.]+%\n±[\d.]+%|N/A)\n\t\n?){5}(?:-?[\d.]+%\n±[\d.]+%|N/A)\n)'
                     r'\t(?P<sessions>[\d,]+)\t(?P<cost>[^\t]*)\t(?P<time>[^\t]*)\t(?P<tokens>[^\t]*)\t(?P<price>[^\n]*)')
    rows = []
    for m in row.finditer(body):
        cells = re.findall(r'(-?[\d.]+)%\n±([\d.]+)%|N/A', m.group('cells'))
        if len(cells) != len(AGENT_COLUMNS):
            raise ValueError(f'row {m.group(1)}: {len(cells)} metric cells')
        r = {'rank': int(m.group(1)), 'name': m.group('name').strip(), 'lab': m.group('lab').split(' · ')[0].strip(),
             'preliminary': bool(m.group('pre')), 'sessions': int(m.group('sessions').replace(',', '')),
             'cost_per_task_usd': number(m.group('cost')), 'time_per_task_min': number(m.group('time')),
             'output_tokens_per_task': number(m.group('tokens'))}
        for col, (v, ci) in zip(AGENT_COLUMNS, cells):
            r[col] = [float(v), float(ci)] if v else None
        rows.append(r)
    listed = count(text, 'models')
    if not rows or [r['rank'] for r in rows] != list(range(1, len(rows) + 1)) or (listed and len(rows) != listed):
        raise ValueError(f'parsed {len(rows)} rows, page lists {listed}')
    return {'as_of': page_date(text), 'sessions': count(text, 'sessions'), 'rows': rows,
            'sign_rule': signed_net_improvement(rows)}


def signed_net_improvement(rows):
    """The page text drops the minus sign. The table is sorted by Net Improvement, so the true values fall down the
    ranks: the magnitudes fall to the row before the first rise and every row from that rise on is negative. The last
    row before the rise may be either sign (both are within its interval of zero); it stays positive and is flagged.
    Other columns keep their magnitude (`sign_lost`)."""
    vals = [r['net_improvement'][0] for r in rows]
    turn = next((i for i in range(1, len(vals)) if vals[i] > vals[i - 1] + 1e-9), len(vals))
    tail = vals[turn:]
    if any(b < a - 1e-9 for a, b in zip(tail, tail[1:])):
        raise ValueError('Net Improvement magnitudes do not fall then rise down the ranks; the sign cannot be rebuilt')
    for i, r in enumerate(rows):
        r['net_improvement'][0] = -vals[i] if i >= turn else vals[i]
        r['sign_lost'] = [c for c in AGENT_COLUMNS[1:] if r.get(c)]
    if 0 < turn < len(rows):
        rows[turn - 1]['sign_ambiguous'] = True
    return (f'Net Improvement signs rebuilt from the rank order: rows {turn + 1}..{len(rows)} negative'
            + (f', row {turn} ambiguous (kept positive)' if 0 < turn < len(rows) else '') if turn < len(rows)
            else 'every Net Improvement positive')


def split_name_lab(head):
    for lab in sorted(LABS, key=len, reverse=True):
        if head.endswith(lab) and len(head) > len(lab):
            return head[:-len(lab)].strip(), lab
    return None, None


def parse_alignment(text):
    """The Alignment Index page (its text runs together): {as_of, sessions, rows: [{rank, name, lab, alignment_index:
    (value, ci), unauthorized_action, false_attribution, deceptive_completion: (pct, low, high)}]}."""
    start = text.find('Deceptive completion')
    if start < 0:
        raise ValueError('no Alignment table header')
    body = text[start + len('Deceptive completion'):]
    rate = r'(\d+\.\d\d)%([\d.]+)–([\d.]+)%'
    cell = re.compile(r'(?P<head>.+?)(?P<idx>\d{2}\.\d)±(?P<ci>\d+\.\d)' + rate * 3)
    rows, pos, errors = [], 0, []
    listed = count(text, 'models')
    while len(rows) < (listed or 10 ** 6):
        m = cell.match(body, pos)
        if not m:
            break
        pos = m.end()
        n = len(rows) + 1
        head = m.group('head')
        if not head.startswith(str(n)):
            raise ValueError(f'row {n}: unexpected rank in {head[:30]!r}')
        name, lab = split_name_lab(re.sub(r'^\d+', '', head[len(str(n)):]))
        if name is None:
            errors.append(f'row {n}: lab not recognised in {head!r}')
        g = [float(x) for x in m.groups()[3:]]
        rows.append({'rank': n, 'name': name, 'lab': lab, 'alignment_index': [float(m.group('idx')), float(m.group('ci'))],
                     'unauthorized_action': g[0:3], 'false_attribution': g[3:6], 'deceptive_completion': g[6:9]})
    if not rows or (listed and len(rows) != listed) or errors:
        raise ValueError(f'parsed {len(rows)} rows, page lists {listed}; ' + '; '.join(errors))
    return {'as_of': page_date(text), 'sessions': count(text, 'sessions'), 'rows': rows}


def load(path):
    raw = json.loads(Path(path).read_text(encoding='utf-8'))
    return json.loads(raw) if isinstance(raw, str) else raw


def kind_of(doc):
    if isinstance(doc, dict) and {'Overall', 'Code', 'Work'} <= set(doc):
        return 'agent'
    text = (doc or {}).get('text') or ''
    url = (doc or {}).get('url') or ''
    if 'Alignment Index' in text[:400] or url.endswith('/agent/alignment'):
        return 'alignment'
    if 'WebDev' in text[:200] or url.endswith('/code/webdev'):
        return 'webdev'
    if 'Text Arena' in text[:200] or url.endswith('/chat/text'):
        return 'text'
    raise ValueError('not a known arena.ai capture')


def normalise(doc, by, captured_at):
    kind = kind_of(doc)
    base = {'source': 'arena.ai, transcribed from the user\'s browser', 'url': doc.get('url') or URLS[kind] if kind != 'agent'
            else URLS['agent'], 'transcribed_by': by, 'captured_at': captured_at}
    if kind == 'agent':
        return kind, {**base, 'categories': {cat: parse_agent(doc[cat]) for cat in doc if isinstance(doc[cat], str) and doc[cat]}}
    if kind == 'alignment':
        return kind, {**base, **parse_alignment(doc['text'])}
    return kind, {**base, 'as_of': page_date(doc['text']), 'text': doc['text']}


def import_captures(paths, target, by):
    files = sources.manifest(target) if (target / sources.MANIFEST).exists() else {}
    for path in paths:
        stamp = datetime.fromtimestamp(Path(path).stat().st_mtime, timezone.utc).isoformat(timespec='seconds')
        kind, doc = normalise(load(path), by, stamp)
        body = (json.dumps(doc, indent=1, ensure_ascii=False) + '\n').encode('utf-8')
        (target / FILES[kind]).write_bytes(body)
        files[FILES[kind]] = {'url': doc['url'], 'fetched_at': stamp, 'status': 'ok', 'bytes': len(body),
                              'sha256': hashlib.sha256(body).hexdigest(), 'transcribed_by': by, 'kind': 'transcribed',
                              'as_of': doc.get('as_of') or min(c['as_of'] for c in doc['categories'].values())}
        (target / sources.MANIFEST).write_text(json.dumps({'files': files}, indent=1), encoding='utf-8')
        print(f'{FILES[kind]:32s} {kind:9s} as of {files[FILES[kind]]["as_of"]}  from {path}')


def read(target, kind):
    """The imported capture of `kind`, or None when it was never imported (a missing source, never an older copy)."""
    row = sources.manifest(target).get(FILES[kind]) or {}
    path = target / FILES[kind]
    if row.get('status') != 'ok' or not path.exists():
        return None
    return json.loads(path.read_text(encoding='utf-8'))


def best_variant(rows, value):
    """{base model name: (value, row)} at the model's best listed variant, as sources.arena reads the dataset."""
    out = {}
    for r in rows:
        if not r.get('name'):
            continue
        v = value(r)
        if v is None:
            continue
        b = sources.base_name(r['name'])
        if b not in out or v > out[b][0]:
            out[b] = (v, r)
    return out


def usd_per_hour(rows):
    """{listed name: community usd per active hour} from the median cost and time per task of real sessions."""
    return {r['name']: round(r['cost_per_task_usd'] / (r['time_per_task_min'] / 60.0), 2) for r in rows
            if r.get('cost_per_task_usd') and r.get('time_per_task_min')}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    imp = sub.add_parser('import')
    imp.add_argument('paths', nargs='+')
    imp.add_argument('--data')
    imp.add_argument('--by', default='user (browser capture)')
    show = sub.add_parser('show')
    show.add_argument('--data')
    args = ap.parse_args(argv)
    target = sources.data_dir(args.data)
    if args.cmd == 'import':
        import_captures(args.paths, target, args.by)
        return 0
    for kind in FILES:
        doc = read(target, kind)
        if doc is None:
            print(f'{kind}: not imported')
            continue
        if kind == 'agent':
            for cat, page in doc['categories'].items():
                print(f'agent/{cat}: as of {page["as_of"]}, {page["sessions"]} sessions, {len(page["rows"])} models')
        else:
            print(f'{kind}: as of {doc["as_of"]}' + (f', {doc.get("sessions")} sessions, {len(doc["rows"])} models'
                                                     if 'rows' in doc else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main())
