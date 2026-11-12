"""Public measurements the router's model rows, floors and effort curves are fitted on.

`python -X utf8 sources.py fetch [--data DIR]` downloads them; every other script reads the same directory.
Raw downloads stay outside git (default: ~/.cache/agentcompass-calibration, or AGENTCOMPASS_CALIBRATION_DATA).

- LMArena leaderboard dataset (Hugging Face lmarena-ai/leaderboard-dataset, `latest` split): WebDev, Agent and its
  sub-boards, Text (style control), Vision.
- Artificial Analysis models leaderboard (HTML): Intelligence Index per model variant, including effort variants, and
  the page's embedded model records (cost and time per task, time to first answer token, prices, sub-benchmarks).
- Design Arena leaderboards (designarena.ai; blind human votes, Bradley-Terry ratings): Website, 3D Design and SVG.
  The page embeds each board's rows (`modelId`, wins, losses, battles, rating) and the model list (id, display name).
- OpenRouter `/api/v1/models` (prices and input modalities).
- Community real-usage measurements of subscription window capacity (wokey.ai, finopsllm.com, inventivehq.com,
  the eneasf Antigravity telemetry findings):
  `community.py`. `refresh.py` (`agentcompass data refresh`) runs fetch, `measurements.py`, `five_axis.py`,
  `capability_catalog.py`, `public_data.py manifest --write`, validation and the source-age report in one command.

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
# Design Arena boards: file -> (url, the board's `category` in the page's embedded `boards` list).
DESIGN_ARENA = {'designarena-website.html': ('https://www.designarena.ai/leaderboard?section=website&board=website', 'website'),
                'designarena-3d.html': ('https://www.designarena.ai/leaderboard?section=3d&board=3d-design', '3d'),
                'designarena-svg.html': ('https://www.designarena.ai/leaderboard?section=art&board=svgs', 'svg')}
PAGES = {'aa-models.html': 'https://artificialanalysis.ai/leaderboards/models',
         'openrouter-models.json': 'https://openrouter.ai/api/v1/models',
         **{name: url for name, (url, _) in DESIGN_ARENA.items()}}
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


def data_dir(arg=None, *, create=True):
    path = Path(arg or os.environ.get('AGENTCOMPASS_CALIBRATION_DATA') or Path.home() / '.cache' / 'agentcompass-calibration')
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def fetch(target, only=None):
    """Download every source (or the file names in `only`, keeping the other manifest rows as they are)."""
    agent = {'User-Agent': 'Mozilla/5.0 (agentcompass calibration)'}
    jobs = [(HF.format(b), target / f'{b}.parquet') for b in ARENA_BOARDS] + [(u, target / n) for n, u in PAGES.items()]
    known = dict(manifest(target)) if (target / MANIFEST).exists() else {}
    # Boards transcribed from the user's browser (arena_capture.py) are imported, not fetched: a fetch keeps their rows.
    files = {n: r for n, r in known.items() if r.get('kind') == 'transcribed'}
    if only:
        jobs = [(u, p) for u, p in jobs if p.name in set(only)]
        files = known
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
    """{file name: manifest row} read from `fetch-manifest.json`."""
    path = Path(target) / MANIFEST
    if not path.is_file():
        raise FileNotFoundError(
            f'{path}: missing {MANIFEST} in {target}; run `agentcompass data refresh` (without --no-fetch) '
            f'to fetch calibration sources first, or delete "{target}".'
        )
    return json.loads(path.read_text(encoding='utf-8'))['files']


def accounts():
    return json.loads(ACCOUNTS.read_text(encoding='utf-8'))


def base_name(name):
    s = re.sub(r'\s*\(.*\)\s*$', '', str(name)).strip()
    if not re.search(r'\bqwen[\d.-]*\s+max$', s, re.I):
        s = re.sub(r'\s+(xhigh|high|medium|low|max)$', '', s, flags=re.I).strip()
    return s


def variant(name):
    m = re.search(r'\(([^)]*)\)\s*$', str(name))
    return m.group(1) if m else ''


def arena_rows(target, board, category='overall'):
    """[(value, lower, upper, listed name, publish date)] for every listed row of `board`/`category`."""
    import pandas as pd
    d = pd.read_parquet(target / f'{board}.parquet')
    d = d[d.category == category]
    col, lo, hi = ('rating', 'rating_lower', 'rating_upper') if 'rating' in d.columns else ('score', 'score_ci_lower', 'score_ci_upper')
    return [(float(r[col]), float(r[lo]), float(r[hi]), str(r.model_name), str(r.leaderboard_publish_date))
            for _, r in d.iterrows()]


def _text(raw):
    t = re.sub(r'<script.*?</script>|<style.*?</style>', ' ', raw, flags=re.S)
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', t)))


EFFORT_WORDS = {'max': 'max', 'xhigh': 'xhigh', 'high': 'high', 'medium': 'medium', 'low': 'low',
                'minimal': 'minimal', 'non-reasoning': 'none', 'none': 'none'}


def artificial_analysis(target, field='index'):
    """{base model: {effort or '': value}} from the AA models leaderboard table; `field` is 'index' (Intelligence Index)
    or 'cost' (cost per task, USD)."""
    html_path = target / 'aa-models.html'
    if not html_path.exists():
        return {}
    out = defaultdict(dict)
    raw = html_path.read_text(encoding='utf-8', errors='ignore')
    table = re.search(r'<table.*?</table>', raw, re.S).group(0)
    for row in re.findall(r'<tr.*?</tr>', table, re.S):
        cells = [re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', c))).strip()
                 for c in re.findall(r'<t[hd].*?</t[hd]>', row, re.S)]
        if len(cells) < 4 or not re.fullmatch(r'\d+(\.\d+)?', cells[3]):
            continue
        b = base_name(cells[0])
        tag = variant(cells[0]).replace(' with fallback', '').strip().lower()
        if field == 'cost':
            cost = re.fullmatch(r'\$([\d.]+)', cells[4])
            if not cost:
                continue
            value = float(cost.group(1))
        else:
            value = float(cells[3])
        out[b][EFFORT_WORDS.get(tag, tag)] = value
    return dict(out)


AA_RSC_PUSH = re.compile(r'self\.__next_f\.push\(\[1,("(?:\\.|[^"\\])*")\]\)', re.S)   # Next.js RSC push chunks


def aa_records(target):
    """The model records embedded in the AA leaderboard page (Next.js RSC stream): one dict per listed variant with
    `name`, `intelligenceIndex`, `intelligenceIndexCostPerTask` (USD), `intelligenceIndexTimePerTask` (s), prices and
    `scores` (sub-benchmarks, fractions)."""
    html_path = target / 'aa-models.html'
    if not html_path.exists():
        return []
    dec, out, seen = json.JSONDecoder(), [], set()
    raw = html_path.read_text(encoding='utf-8', errors='ignore')
    stream = ''.join(json.loads(c) for c in AA_RSC_PUSH.findall(raw))
    for m in re.finditer(r'\{"slug":"', stream):
        try:
            obj, _ = dec.raw_decode(stream, m.start())
        except ValueError:
            continue
        name = obj.get('name')
        if 'intelligenceIndex' in obj and name not in seen:
            seen.add(name)
            out.append(obj)
    return out


def aa_effort(name):
    """Effort a listed variant runs at ('' when the listing names none)."""
    tag = variant(name).lower()
    for part in (p.strip() for p in tag.split(',')):
        if part in EFFORT_WORDS:
            return EFFORT_WORDS[part]
    bare = re.sub(r'\s*\(.*\)\s*$', '', str(name)).strip()
    if not re.search(r'\bqwen[\d.-]*\s+max$', bare, re.I):
        m = re.search(r'\s+(xhigh|high|medium|low|max)$', bare, re.I)
        if m:
            return EFFORT_WORDS[m.group(1).lower()]
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


def openrouter_modalities(target):
    """{model id: [input_modalities]} from OpenRouter's `/api/v1/models` `architecture.input_modalities`."""
    rows = json.loads((target / 'openrouter-models.json').read_text(encoding='utf-8')).get('data') or []
    out = {}
    for r in rows:
        mods = (r.get('architecture') or {}).get('input_modalities')
        if r.get('id') and isinstance(mods, list) and mods:
            out[r['id']] = [str(m).lower() for m in mods if isinstance(m, str)]
    return out


def designarena(target, filename):
    """{model id: {'rating', 'battles', 'wins', 'losses', 'name'}} of one Design Arena board page: the main (public) board
    of the page's embedded `boards` list (not its `subcategoryBoards`), with each id's display name from the embedded
    model list. The board publishes no standard error (`btStdErr` is null) and no data date."""
    raw = (target / filename).read_text(encoding='utf-8', errors='ignore').replace('\\"', '"')
    category = DESIGN_ARENA[filename][1]
    m = re.search(r'"boards":\[\{"arenaType":"models","category":"' + re.escape(category) + r'"[^\[]*?"data":(\[)', raw)
    if not m:
        return {}
    rows, _ = json.JSONDecoder().raw_decode(raw, m.start(1))
    names = dict(re.findall(r'"id":"([^"]+)","displayName":"([^"]+)"', raw))
    return {r['modelId']: {'rating': float(r['elo']), 'battles': r.get('battles'), 'wins': r.get('wins'),
                           'losses': r.get('losses'), 'name': names.get(r['modelId'], r['modelId'])}
            for r in rows if isinstance(r, dict) and r.get('modelId') and isinstance(r.get('elo'), (int, float))}


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
