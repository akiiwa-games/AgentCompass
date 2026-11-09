"""Public measurements the router's model rows, floors and effort curves are fitted on.

`python -X utf8 sources.py fetch [--data DIR]` downloads them; every other script reads the same directory.
Raw downloads stay outside git (default: ~/.cache/agentcompass-calibration, or AGENTCOMPASS_CALIBRATION_DATA).

- LMArena leaderboard dataset (Hugging Face lmarena-ai/leaderboard-dataset, `latest` split): WebDev, Agent and its
  sub-boards, Text (style control), Vision.
- vals.ai SWE-bench Verified (per fix-time bucket) and Terminal-Bench 2.0 leaderboards (HTML).
- Artificial Analysis models leaderboard (HTML): Intelligence Index per model variant, including effort variants, and
  the page's embedded model records (cost and time per task, prices, sub-benchmarks).
- OpenRouter `/api/v1/models` (prices), EQ-Bench Creative Writing v3, METR time horizons v1.1 and the OSWorld-Verified
  results sheet: the five-axis measurements (`measurements.py`).
- Community real-usage measurements of subscription window capacity (wokey.ai, finopsllm.com, inventivehq.com,
  the eneasf Antigravity telemetry findings):
  `community.py`. `refresh.py` re-runs fetch, measurements and five_axis on a schedule and reports source age.

`fetch` writes `fetch-manifest.json` beside the downloads: each file's url, fetched_at (UTC), sha256 and size, or the
error when a source could not be fetched. A source that fails is recorded as missing, never replaced by an older copy.
"""
import argparse
import hashlib
import html
import json
import os
import re
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

HF = 'https://huggingface.co/datasets/lmarena-ai/leaderboard-dataset/resolve/main/{}/latest-00000-of-00001.parquet'
ARENA_BOARDS = ('webdev', 'agent', 'agent_bash_reliability', 'agent_task_outcome_explicit', 'agent_steerability',
                'text_style_control', 'vision')
PAGES = {'swebench.html': 'https://www.vals.ai/benchmarks/swebench',
         'terminal-bench-2.html': 'https://www.vals.ai/benchmarks/terminal-bench-2',
         'aa-models.html': 'https://artificialanalysis.ai/leaderboards/models',
         'openrouter-models.json': 'https://openrouter.ai/api/v1/models',
         'eqbench-creative-writing.js': 'https://eqbench.com/creative_writing.js',
         'metr-benchmark-results.yaml': 'https://metr.org/assets/benchmark_results_1_1.yaml',
         'osworld-verified.xlsx': 'https://os-world.github.io/static/data/osworld_verified_results.xlsx'}
# Window capacity pages read by community.py: community real-usage measurements (round 047, round 4) and the vendor
# plan multipliers of the capacity ladder (round 5).
COMMUNITY_PAGES = {'community-wokey-claude.html': 'https://wokey.ai/claude-usage-limits',
                   'community-wokey-codex.html': 'https://wokey.ai/codex-usage-limits',
                   'community-finops-codex-plus.html': 'https://finopsllm.com/research/codex-usage-limits-chatgpt-pro',
                   'community-finops-codex-pro100.html': 'https://finopsllm.com/research/codex-pro-100-heavy-user-resets-log',
                   'community-finops-claude-max20.html': 'https://finopsllm.com/research/claude-max-20x-vs-5x-weekly-limit',
                   'community-inventive-codex.html': 'https://inventivehq.com/blog/codex-subscription-usage-measured',
                   'community-eneasf-antigravity.md':
                       'https://raw.githubusercontent.com/eneasf/antigravity-token-dashboard/main/docs/FINDINGS.md',
                   # vendor plan multipliers (capacity ladder rung 3, round 5)
                   'vendor-google-ultra.html': 'https://support.google.com/googleone/answer/16286513?hl=en',
                   'vendor-anthropic-max.html': 'https://support.claude.com/en/articles/11049741-what-is-the-max-plan',
                   'vendor-openai-pro100.html': 'https://community.openai.com/t/introducing-new-100-month-pro-tier/1378752'}
PAGES.update(COMMUNITY_PAGES)
MANIFEST = 'fetch-manifest.json'
REPO = Path(__file__).resolve().parents[1]
ACCOUNTS = REPO / 'references' / 'accounts.json'


def data_dir(arg=None):
    path = Path(arg or os.environ.get('AGENTCOMPASS_CALIBRATION_DATA') or Path.home() / '.cache' / 'agentcompass-calibration')
    path.mkdir(parents=True, exist_ok=True)
    return path


def fetch(target, only=None):
    """Download every source (or the file names in `only`, keeping the other manifest rows as they are)."""
    agent = {'User-Agent': 'Mozilla/5.0 (agentcompass calibration)'}
    jobs = [(HF.format(b), target / f'{b}.parquet') for b in ARENA_BOARDS] + [(u, target / n) for n, u in PAGES.items()]
    files = {}
    if only:
        jobs = [(u, p) for u, p in jobs if p.name in set(only)]
        files = dict(manifest(target)) if (target / MANIFEST).exists() else {}
    for url, path in jobs:
        stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=agent), timeout=60) as resp:
                body = resp.read()
        except Exception as exc:     # a failed source is recorded as missing; its stale copy must not be read
            path.unlink(missing_ok=True)
            files[path.name] = {'url': url, 'fetched_at': stamp, 'status': 'missing', 'error': f'{type(exc).__name__}: {exc}'}
            print(f'{path.name:36s} MISSING  {url}  ({exc})')
            continue
        path.write_bytes(body)
        files[path.name] = {'url': url, 'fetched_at': stamp, 'status': 'ok', 'bytes': len(body),
                            'sha256': hashlib.sha256(body).hexdigest()}
        print(f'{path.name:36s} {len(body):>9} bytes  {url}')
    (target / MANIFEST).write_text(json.dumps({'files': files}, indent=1), encoding='utf-8')


def manifest(target):
    """{file name: manifest row}. Downloads made before the manifest existed get their url and file time."""
    path = target / MANIFEST
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))['files']
    urls = {f'{b}.parquet': HF.format(b) for b in ARENA_BOARDS} | PAGES
    out = {}
    for name, url in urls.items():
        f = target / name
        out[name] = ({'url': url, 'status': 'ok', 'bytes': f.stat().st_size, 'sha256': hashlib.sha256(f.read_bytes()).hexdigest(),
                      'fetched_at': datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).isoformat(timespec='seconds')}
                     if f.exists() else {'url': url, 'status': 'missing', 'error': 'not downloaded'})
    return out


def accounts():
    return json.loads(ACCOUNTS.read_text(encoding='utf-8'))


def base_name(name):
    return re.sub(r'\s*\(.*\)\s*$', '', str(name)).strip()


def variant(name):
    m = re.search(r'\(([^)]*)\)\s*$', str(name))
    return m.group(1) if m else ''


def arena(target, board, category='overall'):
    """{base model name: (value, lower, upper, listed name, publish date)} at the model's best listed variant."""
    import pandas as pd
    d = pd.read_parquet(target / f'{board}.parquet')
    d = d[d.category == category]
    col, lo, hi = ('rating', 'rating_lower', 'rating_upper') if 'rating' in d.columns else ('score', 'score_ci_lower', 'score_ci_upper')
    out = {}
    for _, r in d.iterrows():
        row = (float(r[col]), float(r[lo]), float(r[hi]), r.model_name, str(r.leaderboard_publish_date))
        b = base_name(r.model_name)
        if b not in out or row[0] > out[b][0]:
            out[b] = row
    return out


def _text(raw):
    t = re.sub(r'<script.*?</script>|<style.*?</style>', ' ', raw, flags=re.S)
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', t)))


def vals(target, page):
    """({model: overall %}, {model: {bucket: (resolved, total)}}) from a vals.ai benchmark page."""
    raw = html.unescape((target / page).read_text(encoding='utf-8', errors='ignore'))
    txt = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', raw))
    rows = {m: float(s) for _, m, _, s in re.findall(r' (\d{1,3}) ([A-Z][\w .\-()/]+?) (Mini-SWE-agent|Terminus 2) (\d+\.\d+)% \$', txt)}
    buckets = defaultdict(dict)
    for m, k, a, n, _ in re.findall(r'title="([^"]+?) · ([^:]+): (\d+)/(\d+) resolved \((\d+)%\)"', raw):
        buckets[m][re.sub(r'[^\w<>–]', '', k)] = (int(a), int(n))
    return rows, dict(buckets)


EFFORT_WORDS = {'max': 'max', 'xhigh': 'xhigh', 'high': 'high', 'medium': 'medium', 'low': 'low',
                'minimal': 'minimal', 'non-reasoning': 'none'}


def artificial_analysis(target, field='index'):
    """{base model: {effort or '': value}} from the AA models leaderboard table; `field` is 'index' (Intelligence Index)
    or 'cost' (cost per task, USD)."""
    raw = (target / 'aa-models.html').read_text(encoding='utf-8', errors='ignore')
    table = re.search(r'<table.*?</table>', raw, re.S).group(0)
    out = defaultdict(dict)
    for row in re.findall(r'<tr.*?</tr>', table, re.S):
        cells = [re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', c))).strip()
                 for c in re.findall(r'<t[hd].*?</t[hd]>', row, re.S)]
        if len(cells) < 4 or not re.fullmatch(r'\d+(\.\d+)?', cells[3]):
            continue
        tag = variant(cells[0]).replace(' with fallback', '').strip().lower()
        if field == 'cost':
            cost = re.fullmatch(r'\$([\d.]+)', cells[4])
            if not cost:
                continue
            value = float(cost.group(1))
        else:
            value = float(cells[3])
        out[base_name(cells[0])][EFFORT_WORDS.get(tag, tag)] = value
    return dict(out)


AA_RSC_PUSH = re.compile(r'self\.__next_f\.push\(\[1,("(?:\\.|[^"\\])*")\]\)', re.S)   # as refresh_speed_data


def aa_records(target):
    """The model records embedded in the AA leaderboard page (Next.js RSC stream): one dict per listed variant with
    `name`, `intelligenceIndex`, `intelligenceIndexCostPerTask` (USD), `intelligenceIndexTimePerTask` (s), prices and
    `scores` (sub-benchmarks, fractions)."""
    raw = (target / 'aa-models.html').read_text(encoding='utf-8', errors='ignore')
    stream = ''.join(json.loads(c) for c in AA_RSC_PUSH.findall(raw))
    dec, out, seen = json.JSONDecoder(), [], set()
    for m in re.finditer(r'\{"slug":"', stream):
        try:
            obj, _ = dec.raw_decode(stream, m.start())
        except ValueError:
            continue
        if 'intelligenceIndex' in obj and obj.get('name') not in seen:
            seen.add(obj.get('name'))
            out.append(obj)
    return out


def aa_effort(name):
    """Effort a listed AA variant runs at ('' when the listing names none)."""
    tag = variant(name).lower()
    for part in (p.strip() for p in tag.split(',')):
        if part in EFFORT_WORDS:
            return EFFORT_WORDS[part]
    return ''


def openrouter(target):
    """{model id: {'input', 'output', 'cache_read'} USD per million tokens} from OpenRouter's public model list."""
    rows = json.loads((target / 'openrouter-models.json').read_text(encoding='utf-8')).get('data') or []
    out = {}
    for r in rows:
        p = r.get('pricing') or {}
        try:
            out[r['id']] = {k: float(p[src]) * 1e6 for k, src in (('input', 'prompt'), ('output', 'completion'),
                                                                     ('cache_read', 'input_cache_read')) if p.get(src) not in (None, '')}
        except (TypeError, ValueError, KeyError):
            continue
    return out


def eqbench(target):
    """{model name: Elo} from EQ-Bench Creative Writing v3 (the CSV the leaderboard page embeds)."""
    raw = (target / 'eqbench-creative-writing.js').read_text(encoding='utf-8', errors='ignore')
    m = re.search(r'leaderboardDataCreativeWritingV3\s*=\s*`(.*?)`', raw, re.S)
    out = {}
    if not m:
        return out
    lines = [ln.strip() for ln in m.group(1).strip().splitlines() if ln.strip()]
    head = lines[0].split(',')
    for ln in lines[1:]:
        cells = dict(zip(head, ln.split(',')))
        try:
            out[cells['model_name'].lstrip('*').strip()] = float(cells['elo_score'])
        except (KeyError, ValueError):
            continue
    return out


def metr(target):
    """{METR run name: p50 time horizon in minutes} from METR's published time-horizon results (v1.1)."""
    import yaml
    doc = yaml.safe_load((target / 'metr-benchmark-results.yaml').read_text(encoding='utf-8'))
    out = {}
    for name, row in (doc.get('results') or {}).items():
        est = (((row or {}).get('metrics') or {}).get('p50_horizon_length') or {}).get('estimate')
        if isinstance(est, (int, float)):
            out[name] = float(est)
    return out


def osworld(target):
    """{model: success fraction} for OSWorld-Verified rows of a general model (no agent framework), best max-steps run."""
    import openpyxl
    ws = openpyxl.load_workbook(target / 'osworld-verified.xlsx', read_only=True).worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    head = list(rows[0])
    col = {k: head.index(k) for k in ('Model', 'Approach type', 'Success rate')}
    out = {}
    for r in rows[1:]:
        if r[col['Approach type']] != 'General model' or not isinstance(r[col['Success rate']], (int, float)):
            continue
        name, value = str(r[col['Model']]).strip(), float(r[col['Success rate']]) / 100
        out[name] = max(value, out.get(name, 0.0))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('command', choices=['fetch', 'list'])
    ap.add_argument('--data')
    ap.add_argument('--only', action='append', help='fetch only this file name (repeatable); other manifest rows stay')
    args = ap.parse_args()
    target = data_dir(args.data)
    if args.command == 'fetch':
        fetch(target, args.only)
    else:
        for p in sorted(target.iterdir()):
            print(p.name, p.stat().st_size)


if __name__ == '__main__':
    sys.exit(main())
