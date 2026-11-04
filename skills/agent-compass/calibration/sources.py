"""Public measurements the router's model rows, floors and effort curves are fitted on.

`python -X utf8 sources.py fetch [--data DIR]` downloads them; every other script reads the same directory.
Raw downloads stay outside git (default: ~/.cache/agentcompass-calibration, or AGENTCOMPASS_CALIBRATION_DATA).

- LMArena leaderboard dataset (Hugging Face lmarena-ai/leaderboard-dataset, `latest` split): WebDev, Agent and its
  sub-boards, Text (style control), Vision.
- vals.ai SWE-bench Verified (per fix-time bucket) and Terminal-Bench 2.0 leaderboards (HTML).
- Artificial Analysis models leaderboard (HTML): Intelligence Index per model variant, including effort variants.
"""
import argparse
import html
import json
import os
import re
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

HF = 'https://huggingface.co/datasets/lmarena-ai/leaderboard-dataset/resolve/main/{}/latest-00000-of-00001.parquet'
ARENA_BOARDS = ('webdev', 'agent', 'agent_bash_reliability', 'agent_task_outcome_explicit', 'agent_steerability',
                'text_style_control', 'vision')
PAGES = {'swebench.html': 'https://www.vals.ai/benchmarks/swebench',
         'terminal-bench-2.html': 'https://www.vals.ai/benchmarks/terminal-bench-2',
         'aa-models.html': 'https://artificialanalysis.ai/leaderboards/models'}
REPO = Path(__file__).resolve().parents[1]
ACCOUNTS = REPO / 'references' / 'accounts.json'


def data_dir(arg=None):
    path = Path(arg or os.environ.get('AGENTCOMPASS_CALIBRATION_DATA') or Path.home() / '.cache' / 'agentcompass-calibration')
    path.mkdir(parents=True, exist_ok=True)
    return path


def fetch(target):
    agent = {'User-Agent': 'Mozilla/5.0 (agentcompass calibration)'}
    jobs = [(HF.format(b), target / f'{b}.parquet') for b in ARENA_BOARDS] + [(u, target / n) for n, u in PAGES.items()]
    for url, path in jobs:
        with urllib.request.urlopen(urllib.request.Request(url, headers=agent), timeout=60) as resp:
            path.write_bytes(resp.read())
        print(f'{path.name:36s} {path.stat().st_size:>9} bytes  {url}')


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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('command', choices=['fetch', 'list'])
    ap.add_argument('--data')
    args = ap.parse_args()
    target = data_dir(args.data)
    if args.command == 'fetch':
        fetch(target)
    else:
        for p in sorted(target.iterdir()):
            print(p.name, p.stat().st_size)


if __name__ == '__main__':
    sys.exit(main())
