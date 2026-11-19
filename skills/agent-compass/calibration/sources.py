"""Public measurements the router's model rows, floors and effort curves are fitted on.

`python -X utf8 sources.py fetch [--data DIR]` downloads them; every other script reads the same directory.
Raw downloads stay outside git (default: ~/.cache/agentcompass-calibration, or AGENTCOMPASS_CALIBRATION_DATA).

- LMArena leaderboard dataset (Hugging Face lmarena-ai/leaderboard-dataset, `latest` split): WebDev, Agent and its
  sub-boards, Text (style control), Vision.
- Artificial Analysis models leaderboard (HTML): Intelligence Index per model variant, including effort variants, and
  the page's embedded model records (cost and time per task, time to first answer token, prices, sub-benchmarks).
  The page embeds records only for releases Artificial Analysis does not mark deprecated; a deprecated release is named
  without records. Its own page (`https://artificialanalysis.ai/models/<slug>`, one per release a catalogue model is
  listed under, chosen by `measurements.aa_release_plan`, fetched by `fetch_releases`) embeds the same figures in
  another shape, which `aa_release_records` reads into the leaderboard's record shape.
- Design Arena leaderboards (designarena.ai; blind human votes, Bradley-Terry ratings): Website, 3D Design and SVG.
  The page embeds each board's rows (`modelId`, wins, losses, battles, rating) and the model list (id, display name).
- OpenRouter `/api/v1/models` (prices and input modalities).
- Community real-usage measurements of subscription window capacity (wokey.ai, finopsllm.com, inventivehq.com,
  the eneasf Antigravity telemetry findings):
  `community.py`. `refresh.py` (`agentcompass data refresh`) runs fetch, `measurements.py`, `five_axis.py`,
  `capability_catalog.py`, validation and the source-age report in one command.

`fetch` writes `fetch-manifest.json` beside the downloads: each file's url, fetched_at (UTC), sha256 and size, or the
error when a source could not be fetched. A source that fails is recorded as missing, never replaced by an older copy.
The Artificial Analysis release pages are fetched by `fetch_releases` (`agentcompass data refresh` runs it after `fetch`)
into the same directory and manifest, as `aa-release-<slug>.html`, under the same rule. The fetching refresh then records in
the manifest the catalogue rows the snapshot was read for (`release_rows`, round 9): the rows of the release.
"""
import argparse
import bisect
import hashlib
import html
import json
import os
import re
import sys
import urllib.request
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
AA_SLUG = r'[A-Za-z0-9][A-Za-z0-9._-]*'     # a release slug as the site writes it; it becomes part of a url and a file name
AA_RELEASE_URL = 'https://artificialanalysis.ai/models/{}'      # one Artificial Analysis release page, by release slug
AA_RELEASE_FILE = 'aa-release-{}.html'
AA_RELEASE_NAME = re.compile(rf'aa-release-(?P<slug>{AA_SLUG})\.html')     # the file name AA_RELEASE_FILE gives a slug
MANIFEST = 'fetch-manifest.json'
RELEASE_ROWS = 'release_rows'    # the manifest's record of the release's rows (release_rows)
REPO = Path(__file__).resolve().parents[1]
ACCOUNTS = REPO / 'references' / 'accounts.json'
AGENT = {'User-Agent': 'Mozilla/5.0 (agentcompass calibration)'}


def data_dir(arg=None, *, create=True):
    path = Path(arg or os.environ.get('AGENTCOMPASS_CALIBRATION_DATA') or Path.home() / '.cache' / 'agentcompass-calibration')
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def _download(jobs, files):
    """Download each `(url, path)` and record its manifest row in `files`; a failed download deletes the file."""
    for url, path in jobs:
        stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=AGENT), timeout=60) as resp:
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


def fetch(target, only=None):
    """Download every source (or the file names in `only`, keeping the other manifest rows as they are)."""
    jobs = [(HF.format(b), target / f'{b}.parquet') for b in ARENA_BOARDS] + [(u, target / n) for n, u in PAGES.items()]
    known = dict(manifest(target)) if (target / MANIFEST).exists() else {}
    # Boards transcribed from the user's browser (arena_capture.py) are imported, not fetched: a fetch keeps their rows.
    files = {n: r for n, r in known.items() if r.get('kind') == 'transcribed'}
    if only:
        jobs = [(u, p) for u, p in jobs if p.name in set(only)]
        files = known
    for name in known.keys() - files.keys():        # a release page the rebuilt manifest no longer records goes with its row
        if AA_RELEASE_NAME.fullmatch(name):
            (target / name).unlink(missing_ok=True)
    _download(jobs, files)
    (target / MANIFEST).write_text(json.dumps({'files': files}, indent=1), encoding='utf-8')


def fetch_releases(target, pages):
    """Download the Artificial Analysis release pages `pages` ({file name: url}, from `measurements.aa_release_files`)
    beside the other sources, keeping every other manifest row. A release page the manifest records that is not in
    `pages` is removed, file and row: a file the manifest does not record as a release page is never touched."""
    files = dict(manifest(target))
    for name in [n for n in files if AA_RELEASE_NAME.fullmatch(n) and n not in pages]:
        (target / name).unlink(missing_ok=True)
        del files[name]
    _download([(url, target / name) for name, url in pages.items()], files)
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


def release_rows(target):
    """The rows of the release this snapshot was read for (`fetch-manifest.json` `release_rows`, written once by the
    fetching refresh, `record_release_rows`), or None where the manifest has no record."""
    rows = json.loads((Path(target) / MANIFEST).read_text(encoding='utf-8')).get(RELEASE_ROWS)
    return None if rows is None else list(rows)


def record_release_rows(target, rows):
    """Record `rows` as the rows of the release the snapshot in `target` was read for, beside its files."""
    path = Path(target) / MANIFEST
    doc = json.loads(path.read_text(encoding='utf-8'))
    doc[RELEASE_ROWS] = sorted(rows)
    path.write_text(json.dumps(doc, indent=1), encoding='utf-8')


def accounts():
    return json.loads(ACCOUNTS.read_text(encoding='utf-8'))


# A date in a listing's variant tag ("Qwen-Max (0919)", "DeepSeek-V4-Pro (High, 20260813)") names a snapshot: it stays part
# of the base name, so a row reads that listing only when its own name carries the date or its `listed_as` names the
# listing with a cited identity statement (round 5 review A).
SNAPSHOT_TAG = re.compile(r'^(\d{8}|\d{6}|\d{4}|\d{4}-\d{2}(?:-\d{2})?)$')


def base_name(name):
    s = re.sub(r'\s*\(.*\)\s*$', '', str(name)).strip()
    if not re.search(r'\bqwen[\d.-]*\s+max$', s, re.I):
        s = re.sub(r'\s+(xhigh|high|medium|low|max)$', '', s, flags=re.I).strip()
    return ' '.join([s, *(p.strip() for p in variant(name).split(',') if SNAPSHOT_TAG.match(p.strip()))])


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
                'minimal': 'minimal', 'none': 'none'}


def effort_readings(acc):
    """accounts.json `effort_readings` ({'labels', 'reads'}): how a board's reasoning label reads, one rule for every
    model (`labels`: label -> `none`, reasoning switched off, or `default`, a listing without an effort), and the measured
    effort `default` reads (`reads`). A build without the declaration stops."""
    er = (acc or {}).get('effort_readings') or {}
    labels, reads = er.get('labels'), er.get('reads')
    if (not isinstance(labels, dict) or not labels or not set(labels.values()) <= {'none', 'default'}
            or set(labels) & set(EFFORT_WORDS) or not isinstance(reads, dict) or 'default' not in reads):
        raise ValueError('accounts.json effort_readings must map each reasoning label a board writes (`labels`) to none '
                         'or default, and name the measured effort `default` reads (`reads`)')
    return {'labels': {str(k).lower(): v for k, v in labels.items()}, 'reads': dict(reads)}


AA_RSC_PUSH = re.compile(r'self\.__next_f\.push\(\[1,("(?:\\.|[^"\\])*")\]\)', re.S)   # Next.js RSC push chunks
# The sub-benchmarks the pipeline reads (measurements.AA_SCORE ids), under the field a release page's record holds them in.
AA_RELEASE_SCORE_FIELD = {'terminalbench-4-0': 'terminalBench40', 'frontiercode-2-0': 'frontierCode20', 'mmmu-pro': 'mmmuPro'}


def _aa_stream(path):
    """The page's RSC stream: its push chunks decoded and joined."""
    raw = Path(path).read_text(encoding='utf-8', errors='ignore')
    return ''.join(json.loads(c) for c in AA_RSC_PUSH.findall(raw))


def _aa_owners(stream, pattern):
    """The innermost JSON object around each match of `pattern` in `stream`: the nearest opening brace before the match
    whose object reaches past it."""
    dec = json.JSONDecoder()
    opens = [m.start() for m in re.finditer(r'\{', stream)]
    for hit in re.finditer(pattern, stream):
        for start in reversed(opens[:bisect.bisect_left(opens, hit.start())]):
            try:
                obj, end = dec.raw_decode(stream, start)
            except ValueError:
                continue
            if end <= hit.start():
                continue
            yield obj
            break


def aa_records(target):
    """The model records embedded in the AA leaderboard page (Next.js RSC stream): one dict per listed variant with
    `name`, `intelligenceIndex`, `intelligenceIndexCostPerTask` (USD), `intelligenceIndexTimePerTask` (s), prices and
    `scores` (sub-benchmarks, fractions).

    A record is the object that owns an `intelligenceIndex` key, whatever its other keys and their order: the page
    has changed the leading key of its records (2026-11-18, `slug` moved under `release`). Since about 2026-11-17 the
    page embeds records only for releases AA does not mark deprecated (`aa_releases`)."""
    html_path = target / 'aa-models.html'
    if not html_path.exists():
        return []
    out, seen = [], set()
    for obj in _aa_owners(_aa_stream(html_path), r'"intelligenceIndex"\s*:'):
        name = obj.get('name')
        if 'intelligenceIndex' in obj and isinstance(name, str) and name not in seen:
            seen.add(name)
            out.append(obj)
    return out


def aa_releases(target):
    """The releases the AA leaderboard page names, each as the page writes it: `slug`, `name`, `deprecated`,
    `releaseDate` and `modelNames` (the listings of its records; empty when the release lists one unnamed variant).
    Only the page's releases whose slug can be a url and file name."""
    html_path = target / 'aa-models.html'
    if not html_path.exists():
        return []
    out, seen = [], set()
    for obj in _aa_owners(_aa_stream(html_path), r'"modelNames"\s*:'):
        slug = obj.get('slug')
        if isinstance(slug, str) and re.fullmatch(AA_SLUG, slug) and isinstance(obj.get('name'), str) and slug not in seen:
            seen.add(slug)
            out.append(obj)
    return out


AA_FIGURES = ('intelligenceIndex', 'intelligenceIndexCostPerTask', 'intelligenceIndexTimePerTask', 'outputSpeed',
              'timeToFirstAnswerToken', 'price1mInputTokens', 'price1mOutputTokens', 'cacheHitPrice')    # a record's figures


def _aa_number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _aa_figure(value, where):
    """A figure a release page serves: a number, or null (absent) for one AA has not measured, which is how the page
    writes it for a record without an index. Any other value is a shape the reader does not know."""
    if value is not None and _aa_number(value) is None:
        raise ValueError(f'{where} is {type(value).__name__}, not a number or null')
    return value


def _aa_object(value, where):
    """A nested object a release page serves, or {} when it is null (absent); any other value is an unknown shape."""
    if value is not None and not isinstance(value, dict):
        raise ValueError(f'{where} is {type(value).__name__}, not an object or null')
    return value or {}


def aa_has_figure(rec):
    """Whether a record carries any figure the pipeline reads (the index, a time, a speed, a price, a sub-benchmark)."""
    return any(_aa_number(rec.get(f)) is not None for f in AA_FIGURES) or bool(rec.get('scores'))


def aa_release_record(obj, page):
    """One record of a release page as a leaderboard record. The page holds the same figures under other fields: the
    sub-benchmarks flat (`AA_RELEASE_SCORE_FIELD`), the cost per task as `{cost: {total}}`, output speed and time to
    first answer token under `headline`. `page` is the file the record was read from. A value of a shape other than
    the one named here raises ValueError, naming the page, the record and the field."""
    where = f"{page}: record {obj['name']!r}"
    head = _aa_object(obj.get('headline'), f'{where}: headline')
    cost = _aa_object(_aa_object(obj.get('intelligenceIndexCostPerTask'), f'{where}: intelligenceIndexCostPerTask')
                      .get('cost'), f'{where}: intelligenceIndexCostPerTask.cost')

    def figure(source, field, path=None):
        return _aa_figure(source.get(field), f'{where}: {path or field}')

    return {'name': obj['name'], 'page': page, 'intelligenceIndex': figure(obj, 'intelligenceIndex'),
            'intelligenceIndexCostPerTask': figure(cost, 'total', 'intelligenceIndexCostPerTask.cost.total'),
            'intelligenceIndexTimePerTask': figure(obj, 'intelligenceIndexTimePerTask'),
            'outputSpeed': figure(head, 'outputSpeed', 'headline.outputSpeed'),
            'timeToFirstAnswerToken': figure(head, 'timeToFirstAnswerToken', 'headline.timeToFirstAnswerToken'),
            'price1mInputTokens': figure(obj, 'price1mInputTokens'), 'price1mOutputTokens': figure(obj, 'price1mOutputTokens'),
            'cacheHitPrice': figure(obj, 'cacheHitPrice'),
            'scores': {sid: v for sid, field in AA_RELEASE_SCORE_FIELD.items() if (v := figure(obj, field)) is not None}}


def aa_release_records(target, names):
    """(records, errors): the records of the release pages `names` (file names, from `measurements.aa_release_plan`),
    in the leaderboard's record shape plus `page` (the file name), and {file name: why it cannot be read} for a page
    whose record has a shape `aa_release_record` does not know (none of that page's records is returned). A release
    page also embeds other releases' records for comparison; only the records of the page's own release
    (`release.slug` the page's slug) are read. A page the manifest does not list as fetched is not read."""
    out, errors, files = [], {}, manifest(target)
    for name in sorted(names):
        if (files.get(name) or {}).get('status') != 'ok' or not (target / name).exists():
            continue
        slug, seen, page = AA_RELEASE_NAME.fullmatch(name)['slug'], set(), []
        try:
            for obj in _aa_owners(_aa_stream(target / name), r'"release"\s*:\s*\{'):
                release = obj.get('release')
                if (isinstance(release, dict) and release.get('slug') == slug and 'intelligenceIndex' in obj
                        and isinstance(obj.get('name'), str) and obj['name'] not in seen):
                    seen.add(obj['name'])
                    page.append(aa_release_record(obj, name))
        except ValueError as exc:
            errors[name] = str(exc)
        else:
            out += page
    return out, errors


def _tag_parts(name):
    return [p.strip() for p in variant(name).lower().split(',') if p.strip()]


def aa_effort(name, readings):
    """Effort a listed variant runs at ('' when the listing names none: the model's one configuration). A reasoning label
    reads as `readings['labels']` (accounts.json effort_readings, `effort_readings`) declares: a `none` label is reasoning
    switched off, whatever effort the tag names besides; a `default` label is a listing without an effort, unless the tag
    names one, which then wins."""
    parts = _tag_parts(name)
    if any(readings['labels'].get(p) == 'none' for p in parts):
        return 'none'
    for part in parts:
        if part in EFFORT_WORDS:
            return EFFORT_WORDS[part]
    bare = re.sub(r'\s*\(.*\)\s*$', '', str(name)).strip()
    if not re.search(r'\bqwen[\d.-]*\s+max$', bare, re.I):
        m = re.search(r'\s+(xhigh|high|medium|low|max)$', bare, re.I)
        if m:
            return EFFORT_WORDS[m.group(1).lower()]
    return ''


def listing_rank(name, readings):
    """Preference among one base name's listings read at the same effort (lower first), or None for a listing not read:
    a listing without a reasoning label before one with it (an effort label wins over a bare reasoning label at that
    effort); of the reasoning-off listings the bare one, then the one at the effort `default` reads
    (`readings['reads']`); another reasoning-off listing at a named effort is not read."""
    parts = _tag_parts(name)
    named = next((EFFORT_WORDS[p] for p in parts if p in EFFORT_WORDS), None)
    if aa_effort(name, readings) == 'none':
        return 0 if named in (None, 'none') else 1 if named == readings['reads']['default'] else None
    return int(any(p in readings['labels'] for p in parts))


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
