"""Tests for the single calibration refresh entry point (`calibration/refresh.py` and `agentcompass data refresh`)."""
from datetime import datetime, timedelta, timezone
import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_ROOT = REPO_ROOT / 'skills' / 'agent-compass'
CAL_DIR = SKILL_ROOT / 'calibration'
SCRIPTS_DIR = SKILL_ROOT / 'scripts'
for _p in (str(CAL_DIR), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import arena_capture  # noqa: E402
import capability_catalog  # noqa: E402
import data_age  # noqa: E402
import five_axis  # noqa: E402
import measurements  # noqa: E402
import reference_provenance  # noqa: E402
import refresh  # noqa: E402
import sources  # noqa: E402

# accounts.json's effort_readings declaration, and as sources.effort_readings reads it
ACCOUNT_READINGS = {'effort_readings': {'labels': {'non-reasoning': 'none', 'reasoning': 'default'}, 'reads': {'default': 'high'}}}
READINGS = sources.effort_readings(ACCOUNT_READINGS)


def setUpModule():
    """No test downloads: a code path that reaches the network fails the test at once (and is recorded as a failed fetch)."""
    guard = patch.object(sources.urllib.request, 'urlopen', side_effect=AssertionError('a test must not use the network'))
    guard.start()
    unittest.addModuleCleanup(guard.stop)


PANEL_MODELS = (
    ('claude-opus-5-5', 'Claude Opus 5.5', 'Anthropic'),
    ('gpt-6-astra', 'GPT-6 Astra', 'OpenAI'),
    ('claude-sonnet-5-5', 'Claude Sonnet 5.5', 'Anthropic'),
    ('gpt-6.1-sol', 'GPT-6.1 Sol', 'OpenAI'),
    ('grok-4.7', 'Grok 4.7', 'xAI'),
    ('gemini-3.8-flash-high', 'Gemini 3.8 Flash', 'Google'),
    ('claude-fable-5-2', 'Claude Fable 5.2', 'Anthropic'),
    ('claude-haiku-5-5', 'Claude Haiku 5.5', 'Anthropic'),
    ('gemini-4-carbon', 'Gemini 4 Carbon', 'Google'),
    ('gpt-6-luna', 'GPT-6 Luna', 'OpenAI'),
)


def _write_file(target: Path, name: str, content: bytes | str, url: str, fetched_at: str, manifest_files: dict, **extra):
    path = target / name
    data = content.encode('utf-8') if isinstance(content, str) else content
    path.write_bytes(data)
    manifest_files[name] = {
        'url': url,
        'fetched_at': fetched_at,
        'status': 'ok',
        'bytes': len(data),
        'sha256': hashlib.sha256(data).hexdigest(),
        **extra,
    }


def release_slug(model_id):
    """The slug the fixture gives a model's Artificial Analysis release."""
    return model_id.replace('.', '-')


def aa_release_shape(record, release):
    """A leaderboard record as its release page embeds it: the sub-benchmarks flat, the cost per task as `{cost: {total}}`,
    output speed and time to first answer token under `headline`, and `release` naming the page's own release."""
    scores = record['scores']
    return {'id': record['id'], 'name': record['name'], 'release': {'deprecatedTo': None, 'name': release[1], 'slug': release[0]},
            'deprecated': True, 'intelligenceIndex': record['intelligenceIndex'],
            'intelligenceIndexTimePerTask': record['intelligenceIndexTimePerTask'],
            'intelligenceIndexCostPerTask': {'cost': {'total': record['intelligenceIndexCostPerTask']}, 'evaluations': []},
            'price1mInputTokens': record['price1mInputTokens'], 'price1mOutputTokens': record['price1mOutputTokens'],
            'cacheHitPrice': record['cacheHitPrice'],
            'terminalBench40': scores['terminalbench-4-0'], 'frontierCode20': scores['frontiercode-2-0'],
            'mmmuPro': scores['mmmu-pro'],
            'headline': {'outputSpeed': record['outputSpeed'], 'timeToFirstToken': record['timeToFirstAnswerToken'],
                         'timeToFirstAnswerToken': record['timeToFirstAnswerToken']}}


def rsc_page(payload):
    return f'<html><body><script>self.__next_f.push([1,{json.dumps(json.dumps(payload, separators=(",", ":")))}])</script></body></html>'


def build_calibration_fixture(root: Path, as_of_dt: datetime | None = None, arena_partial=(), aa_only=(),
                              aa_deprecated=()) -> tuple[Path, Path]:
    """Create a self-contained `(data_dir, references_dir)` fixture with fresh cited sources and zero network calls.
    `arena_partial` (slug, display, lab) rows are listed on the arena.ai text boards and Artificial Analysis only, so they
    stay out of the panel; `aa_only` rows are listed on Artificial Analysis only. `aa_deprecated` model ids are released
    on Artificial Analysis as deprecated: the leaderboard page names the release without records and the release page
    (fetched, in the manifest) embeds them, as the site serves them."""
    moment = (as_of_dt or datetime.now(timezone.utc)).astimezone(timezone.utc)
    iso_date = moment.date().isoformat()
    long_date = f"{moment.strftime('%B')} {moment.day}, {moment.year}"
    day_first_date = f"{moment.day} {moment.strftime('%B')} {moment.year}"
    fetched_at = moment.isoformat(timespec='seconds')

    data_dir = root / 'cal-data'
    refs_dir = root / 'references'
    data_dir.mkdir(parents=True, exist_ok=True)
    refs_dir.mkdir(parents=True, exist_ok=True)

    for name in ('accounts.json', 'plan-templates.json', 'classifier-cases.json', 'user-scores.json'):
        shutil.copyfile(reference_provenance.REFERENCES / name, refs_dir / name)
    # The fixture's boards list PANEL_MODELS: it declares them as its panel (calibration_panel.members, measurements.py).
    accounts = json.loads((refs_dir / 'accounts.json').read_text(encoding='utf-8'))
    accounts['calibration_panel']['members'] = [m for m, _, _ in PANEL_MODELS]
    text = json.dumps(accounts, indent=2, ensure_ascii=False) + '\n'
    (refs_dir / 'accounts.json').write_text(text, encoding='utf-8', newline='\n')

    manifest_files: dict[str, dict] = {}

    # 1. LMArena parquet boards (distinct vectors per board/category so duplicate_board never triggers).
    def write_parquet(board: str, categories: list[tuple[str, float, float]], models=PANEL_MODELS):
        rows = []
        for cat, base, step in categories:
            for idx, (_, display, _) in enumerate(models):
                val = base - idx * step - (idx % 3) * 0.7
                rows.append({
                    'category': cat,
                    'model_name': f'{display} (High)',
                    'rating': val,
                    'rating_lower': val - 8.0,
                    'rating_upper': val + 8.0,
                    'leaderboard_publish_date': iso_date,
                })
        buf = data_dir / f'{board}.parquet'
        pd.DataFrame(rows).to_parquet(buf, index=False)
        raw = buf.read_bytes()
        manifest_files[buf.name] = {
            'url': sources.HF.format(board),
            'fetched_at': fetched_at,
            'status': 'ok',
            'bytes': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest(),
        }

    write_parquet('text_style_control', [('overall', 1480.0, 14.0), ('creative_writing', 1465.0, 16.5)],
                  (*PANEL_MODELS, *arena_partial))
    write_parquet('webdev', [('overall', 1440.0, 18.2)])
    write_parquet('vision', [('overall', 1410.0, 15.3)])
    for unused_board in ('agent', 'agent_bash_reliability', 'agent_task_outcome_explicit', 'agent_steerability'):
        write_parquet(unused_board, [('overall', 1350.0, 11.0)])

    # 2. Artificial Analysis RSC stream with multi-effort curves (the record shape the page serves). The page names every
    # release (`modelNames`), embeds records only for the releases it does not mark deprecated, and a deprecated release
    # has a page of its own.
    rsc_records, releases, release_records = [], [], {}
    effort_offsets = (('low', -6.0, 0.55, 1.4), ('medium', -2.5, 0.78, 1.9), ('high', 0.0, 1.0, 2.6), ('xhigh', 1.8, 1.35, 3.4))
    for idx, (slug, display, _) in enumerate((*PANEL_MODELS, *arena_partial, *aa_only)):
        base_idx = 78.0 - idx * 3.1
        names = []
        for eff, idx_shift, time_mult, ttfa_base in effort_offsets:
            listed = f'{display} ({eff.capitalize()})'
            names.append(listed)
            ii_val = round(base_idx + idx_shift, 2)
            cost_task = round((1.8 - idx * 0.08) * time_mult, 2)
            tb4 = round(max(0.12, min(0.95, 0.76 - idx * 0.042 + idx_shift * 0.008)), 4)
            fc2 = round(max(0.10, min(0.94, 0.71 - idx * 0.039 + idx_shift * 0.009 - (idx % 2) * 0.011)), 4)
            mmmu = round(max(0.15, min(0.96, 0.82 - idx * 0.036 + idx_shift * 0.006 + (idx % 3) * 0.007)), 4)
            record = {
                'id': f'{slug}-{eff}',
                'release': {'slug': release_slug(slug), 'name': display},
                'name': listed,
                'intelligenceIndex': ii_val,
                'intelligenceIndexCostPerTask': cost_task,
                'intelligenceIndexTimePerTask': round((180.0 + idx * 14.0) * time_mult, 2),
                'timeToFirstAnswerToken': round(ttfa_base + idx * 0.35, 3),
                'outputSpeed': round(95.0 - idx * 4.5, 2),
                'price1mInputTokens': round(4.0 - idx * 0.25, 2),
                'price1mOutputTokens': round(20.0 - idx * 1.2, 2),
                'cacheHitPrice': round(0.4 - idx * 0.02, 3),
                'scores': {
                    'terminalbench-4-0': tb4,
                    'frontiercode-2-0': fc2,
                    'mmmu-pro': mmmu,
                },
            }
            if slug in aa_deprecated:
                release_records.setdefault(slug, []).append(aa_release_shape(record, (release_slug(slug), display)))
            else:
                rsc_records.append(record)
        releases.append({'slug': release_slug(slug), 'name': display, 'deprecated': slug in aa_deprecated,
                         'releaseDate': '2026-09-01', 'modelNames': names})
    releases.append({'slug': 'retired-model', 'name': 'Retired Model', 'deprecated': True, 'releaseDate': '2025-01-01',
                     'modelNames': ['Retired Model (High)']})        # no catalogue row is listed under it
    _write_file(data_dir, 'aa-models.html', rsc_page([*rsc_records, *releases]), sources.PAGES['aa-models.html'],
                fetched_at, manifest_files)
    for slug, records in release_records.items():
        own = release_slug(slug)
        comparison = dict(records[0], name='Comparison Model (High)', intelligenceIndex=1.0,
                          release={'deprecatedTo': None, 'name': 'Comparison', 'slug': 'comparison'})       # another release's record
        lean = {'name': records[0]['name'], 'intelligenceIndex': 1.0, 'outputSpeed': 1.0}       # a table row: no release key
        _write_file(data_dir, sources.AA_RELEASE_FILE.format(own), rsc_page({'models': [*records, comparison, lean]}),
                    sources.AA_RELEASE_URL.format(own), fetched_at, manifest_files)

    # 3. Design Arena boards.
    for board_idx, (fname, (url, category)) in enumerate(sources.DESIGN_ARENA.items()):
        data_rows = []
        id_defs = []
        for idx, (slug, display, _) in enumerate(PANEL_MODELS):
            mid = f'id-{board_idx}-{slug}'
            elo = round(1320.0 + board_idx * 25.0 - idx * (19.0 + board_idx * 2.3) - (idx % 3) * (board_idx + 1) * 1.4, 2)
            data_rows.append({'modelId': mid, 'elo': elo, 'battles': 800 - idx * 20, 'wins': 420 - idx * 10, 'losses': 380 - idx * 10})
            id_defs.append({'id': mid, 'displayName': display})
        da_html = (
            '<html><body><script>'
            + json.dumps({'models': id_defs, 'boards': [{'arenaType': 'models', 'category': category, 'data': data_rows}]}, separators=(',', ':'))
            + '</script></body></html>'
        )
        _write_file(data_dir, fname, da_html, url, fetched_at, manifest_files)

    # 4. OpenRouter models JSON.
    or_rows = []
    for idx, (slug, _, lab) in enumerate(PANEL_MODELS):
        or_rows.append({
            'id': f'{lab.lower()}/{slug}',
            'pricing': {
                'prompt': f'{(4.0 - idx * 0.25) / 1e6:.10f}',
                'completion': f'{(20.0 - idx * 1.2) / 1e6:.10f}',
                'input_cache_read': f'{(0.4 - idx * 0.02) / 1e6:.10f}',
            },
            'architecture': {'input_modalities': ['text', 'image']},
        })
    _write_file(
        data_dir,
        'openrouter-models.json',
        json.dumps({'data': or_rows}, indent=2) + '\n',
        sources.PAGES['openrouter-models.json'],
        fetched_at,
        manifest_files,
    )

    # 5. Browser-captured arena.ai Agent & Alignment boards (`kind='transcribed'`).
    agent_overall_rows = []
    agent_code_rows = []
    alignment_rows = []
    for idx, (_, display, lab) in enumerate(PANEL_MODELS):
        listed = f'{display} (High)'
        agent_overall_rows.append({
            'rank': idx + 1,
            'name': listed,
            'lab': lab,
            'preliminary': False,
            'net_improvement': [round(38.0 - idx * 2.7, 2), 1.5],
            'sessions': 1200 - idx * 40,
            'cost_per_task_usd': round(1.6 - idx * 0.09, 3),
            'time_per_task_min': round(4.5 + idx * 0.25, 2),
            'output_tokens_per_task': 6500 - idx * 180,
        })
        agent_code_rows.append({
            'rank': idx + 1,
            'name': listed,
            'lab': lab,
            'preliminary': False,
            'net_improvement': [round(42.0 - idx * 3.1 - (idx % 2) * 0.8, 2), 1.6],
            'sessions': 900 - idx * 30,
        })
        alignment_rows.append({
            'rank': idx + 1,
            'name': listed,
            'lab': lab,
            'alignment_index': [round(88.0 - idx * 2.9 - (idx % 3) * 0.6, 2), 1.2],
        })
    _write_file(
        data_dir,
        arena_capture.FILES['agent'],
        json.dumps({
            'url': arena_capture.URLS['agent'],
            'captured_at': fetched_at,
            'transcribed_by': 'fixture',
            'categories': {
                'Overall': {'as_of': iso_date, 'sessions': 15000, 'rows': agent_overall_rows},
                'Code': {'as_of': iso_date, 'sessions': 9000, 'rows': agent_code_rows},
            },
        }, indent=1) + '\n',
        arena_capture.URLS['agent'],
        fetched_at,
        manifest_files,
        kind='transcribed',
    )
    _write_file(
        data_dir,
        arena_capture.FILES['alignment'],
        json.dumps({
            'url': arena_capture.URLS['alignment'],
            'captured_at': fetched_at,
            'transcribed_by': 'fixture',
            'as_of': iso_date,
            'rows': alignment_rows,
        }, indent=1) + '\n',
        arena_capture.URLS['alignment'],
        fetched_at,
        manifest_files,
        kind='transcribed',
    )

    # 6. Community & vendor capacity pages.
    pages = {
        'community-wokey-claude.html': (
            f'<html><body><p>Data updated {iso_date}</p>'
            '<p>Claude Pro ($20/mo) : ≈ $155 of API use a week · 24.2M uncached tokens · ≈ 13 full 5-hour windows</p>'
            '<p>Claude Ultra (Max 20x) ($200/mo) : ≈ $1,549 of API use a week · 1M tokens · ≈ 5.7 full 5-hour windows</p>'
            '<p>Fable 5.1 can use only half the weekly limit.</p>'
            f'<p>value every plan at one fixed traffic split: Claude Code traffic on Wokey in the week to {iso_date}, '
            'about 92% of tokens cache reads.</p>'
            '<p>Claude Ultra (Max 20x) $200/mo $1,549 ($1,216–$1,697), ≈ $6,711/mo 34× 180M 9.9M $272</p>'
            '<p>Claude Ultra (Max 20x): 92% cache reads, 1.98B tokens a week</p></body></html>'
        ),
        'community-wokey-codex.html': (
            f'<html><body><p>Data updated {iso_date}</p>'
            '<p>ChatGPT Plus (Codex) ($20/mo) : ≈ $95 of API use a week · 12M tokens · ≈ 8 full 5-hour windows</p>'
            '<p>ChatGPT Pro $200 (Codex) ($200/mo) : ≈ $1,120 of API use a week · 90M tokens</p></body></html>'
        ),
        'community-finops-codex-plus.html': (
            f'<html><body><p>Updated {long_date}</p>'
            '<p>ChatGPT Plus has been worth a median $105 of API-priced usage across tracked weeks.</p></body></html>'
        ),
        'community-finops-codex-pro100.html': (
            f'<html><body><p>Updated {long_date}</p>'
            '<p>A full Pro $100 week priced at a median $540 across heavy sessions.</p>'
            '<p>32.0 h 100% $640 Upgrade from Plus</p></body></html>'
        ),
        'community-finops-claude-max20.html': (
            f'<html><body><p>Updated {long_date}</p>'
            '<p>About $1.6k of API-equivalent usage at today rates.</p>'
            '<p>How big is the Claude Max 5x weekly limit? At least $420 of API usage a week.</p>'
            '<p>the $85 and $95 windows are what a full, deliberate session holds.</p></body></html>'
        ),
        'community-inventive-codex.html': (
            f'<html><body><p>By Alex · {iso_date}</p>'
            '<p>Pro $200 (new 10× tier): about $1100 of API-priced usage a week.</p>'
            '<p>Pro $100: about $500–580 a week.</p></body></html>'
        ),
        'community-eneasf-antigravity.md': (
            f'On Monday {day_first_date}, our test harness drove the first recorded empirical weekly run.\n'
            'This yields a $20.00 USD 5-hour burst allowance and a $129.00 USD weekly allowance.\n'
            'Capacity: $129.00 / week │ │ Capacity: $129.00 / week │ │ Burst: $20.00 / 5h │ │ Burst: $20.00 / 5h\n'
        ),
        'vendor-google-ultra.html': (
            '<html><body><p>Based on your specific Google AI Ultra plan, you get 5x or 20x usage quota in Gemini and '
            'Google Antigravity compared to the Google AI Pro plan.</p></body></html>'
        ),
        'vendor-anthropic-max.html': (
            "<html><body><p>The Max plan includes five times the Pro plan's usage every five hours and every week.</p></body></html>"
        ),
        'vendor-openai-pro100.html': (
            '<html><body><p>This new tier offers 5x more Codex usage than Plus.</p></body></html>'
        ),
    }
    for fname, html_text in pages.items():
        _write_file(data_dir, fname, html_text, sources.COMMUNITY_PAGES[fname], fetched_at, manifest_files)

    (data_dir / sources.MANIFEST).write_text(
        json.dumps({'files': manifest_files}, indent=1) + '\n', encoding='utf-8', newline='\n'
    )
    return data_dir, refs_dir


class RefreshEntryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='refresh-entry-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.now = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
        self.data_dir, self.refs_dir = build_calibration_fixture(self.tmp, self.now)

    def _run_refresh(self, *extra_args):
        out, err = io.StringIO(), io.StringIO()
        with patch('sys.stdout', out), patch('sys.stderr', err):
            code = refresh.main(['--data', str(self.data_dir), '--references', str(self.refs_dir), *extra_args])
        return code, out.getvalue(), err.getvalue()

    def test_no_fetch_rebuilds_every_file_validates_and_keeps_unchanged_content_byte_identical(self):
        generated = (
            'public-measurements.json',
            'five-axis-scores.json',
            'model-capabilities.json',
            'model-capabilities.md',
        )
        shipped = ('accounts.json', 'plan-templates.json', 'classifier-cases.json', 'five-axis-scores.json',
                   'user-scores.json')

        def shipped_bytes():
            return {name: (self.refs_dir / name).read_bytes() for name in shipped}

        for name in generated:
            self.assertFalse((self.refs_dir / name).exists(), name)

        report_path = self.tmp / 'source-age.md'
        code, stdout, stderr = self._run_refresh('--no-fetch', '--report', str(report_path))
        self.assertEqual(code, 0, stderr + stdout)
        self.assertEqual(stderr, '')
        self.assertTrue(report_path.is_file())
        self.assertIn('Source ages at ', report_path.read_text(encoding='utf-8'))

        for name in generated:
            self.assertTrue((self.refs_dir / name).is_file(), f'{name} was not generated')

        refresh.validate(self.refs_dir)
        reference_provenance.validate_references(self.refs_dir)
        self.assertEqual(capability_catalog.check_files(references=self.refs_dir), [])

        before = shipped_bytes()

        # 1. Re-running `--no-fetch` (including at a later timestamp) keeps unchanged reference files byte-identical.
        code2, stdout2, stderr2 = self._run_refresh('--no-fetch')
        self.assertEqual(code2, 0, stderr2 + stdout2)
        self.assertEqual(shipped_bytes(), before)

        refresh.rebuild(self.data_dir, now=self.now + timedelta(hours=5), references=self.refs_dir)
        self.assertEqual(shipped_bytes(), before)

        # 2. Deleting generated capability views and re-running `--no-fetch` rebuilds them; the rest stays byte-identical.
        (self.refs_dir / 'model-capabilities.json').unlink()
        (self.refs_dir / 'model-capabilities.md').unlink()
        code3, _, stderr3 = self._run_refresh('--no-fetch')
        self.assertEqual(code3, 0, stderr3)
        self.assertTrue((self.refs_dir / 'model-capabilities.json').is_file())
        self.assertTrue((self.refs_dir / 'model-capabilities.md').is_file())
        self.assertEqual(shipped_bytes(), before)

        # 3. Clean rebuild from scratch at fixed `now` twice produces the exact same bytes.
        t_fixed = self.now - timedelta(minutes=10)
        for name in ('public-measurements.json', 'five-axis-scores.json', 'model-capabilities.json', 'model-capabilities.md'):
            (self.refs_dir / name).unlink()
        refresh.rebuild(self.data_dir, now=t_fixed, references=self.refs_dir)
        fixed_bytes = shipped_bytes()
        for name in ('public-measurements.json', 'five-axis-scores.json', 'model-capabilities.json', 'model-capabilities.md'):
            (self.refs_dir / name).unlink()
        refresh.rebuild(self.data_dir, now=t_fixed, references=self.refs_dir)
        self.assertEqual(shipped_bytes(), fixed_bytes)

    def test_a_panel_change_is_refused_unless_accepted_and_then_recorded(self):
        """Brief 2026-11-13: when the panel's members differ from the shipped five-axis-scores.json the refresh prints the
        change, writes nothing and exits 3 (refused); `--accept-panel-change` writes it and records it in `anchor.panel_change`,
        and a later run with the same members carries that record forward."""
        code, stdout, stderr = self._run_refresh('--no-fetch')
        self.assertEqual(code, 0, stderr + stdout)
        scores = self.refs_dir / 'five-axis-scores.json'
        doc = json.loads(scores.read_text(encoding='utf-8'))
        members = list(doc['anchor']['panel'])
        self.assertGreater(len(members), 1)
        self.assertIsNone(doc['anchor']['panel_change'])
        dropped = members[-1]
        doc['anchor']['panel'] = [*members[:-1], 'gone-model']        # the shipped panel had gone-model, not `dropped`
        scores.write_text(json.dumps(doc, indent=1) + '\n', encoding='utf-8', newline='\n')
        before = {p.name: p.read_bytes() for p in self.refs_dir.iterdir() if p.is_file()}

        code, stdout, stderr = self._run_refresh('--no-fetch')
        self.assertEqual(code, refresh.REFUSED, stderr + stdout)
        self.assertNotIn(code, refresh.LEVEL.values())
        self.assertIn(f'added {dropped}', stderr)
        self.assertIn('removed gone-model', stderr)
        self.assertIn('--accept-panel-change', stderr)
        for category in ('creativity', 'coding', 'aesthetics', 'tool_operation'):
            self.assertIn(f'  {category}', stderr)
        self.assertEqual({p.name: p.read_bytes() for p in self.refs_dir.iterdir() if p.is_file()}, before)

        code, stdout, stderr = self._run_refresh('--no-fetch', '--accept-panel-change')
        self.assertEqual(code, 0, stderr + stdout)
        record = json.loads(scores.read_text(encoding='utf-8'))['anchor']['panel_change']
        self.assertEqual((record['added'], record['removed']), ([dropped], ['gone-model']))
        self.assertRegex(record['date'], r'^\d{4}-\d{2}-\d{2}$')
        self.assertEqual(set(record['shifts']), {'creativity', 'coding', 'aesthetics', 'tool_operation'})

        code, stdout, stderr = self._run_refresh('--no-fetch')
        self.assertEqual(code, 0, stderr + stdout)
        self.assertEqual(json.loads(scores.read_text(encoding='utf-8'))['anchor']['panel_change'], record)

    def test_missing_source_exits_2_as_before(self):
        # Missing board source (`designarena-svg.html`) rebuilds, reports missing in source-age table, and exits 2.
        (self.data_dir / 'designarena-svg.html').unlink()
        manifest_path = self.data_dir / sources.MANIFEST
        fetch_doc = json.loads(manifest_path.read_text(encoding='utf-8'))
        fetch_doc['files']['designarena-svg.html'] = {
            'url': sources.PAGES['designarena-svg.html'],
            'fetched_at': self.now.isoformat(timespec='seconds'),
            'status': 'missing',
            'error': 'URLError: offline',
        }
        manifest_path.write_text(json.dumps(fetch_doc, indent=1) + '\n', encoding='utf-8', newline='\n')

        code, stdout, stderr = self._run_refresh('--no-fetch')
        self.assertEqual(code, 2, stderr + stdout)
        self.assertIn('| designarena.svg |', stdout)
        self.assertIn('| missing |', stdout)
        # Every output file is still rebuilt and valid.
        refresh.validate(self.refs_dir)

        # A calibration directory without fetch-manifest.json fails with that exact cause, including default data_dir().
        empty_dir = self.tmp / 'empty-cal'
        empty_dir.mkdir()
        out, err = io.StringIO(), io.StringIO()
        with patch('sys.stdout', out), patch('sys.stderr', err):
            empty_code = refresh.main(['--no-fetch', '--data', str(empty_dir), '--references', str(self.refs_dir)])
        self.assertEqual(empty_code, 2)
        self.assertIn(str(empty_dir / 'fetch-manifest.json'), err.getvalue())

        default_cache = self.tmp / 'default-cal-cache'
        default_cache.mkdir()
        (default_cache / 'stale.parquet').write_bytes(b'old')
        out2, err2 = io.StringIO(), io.StringIO()
        with patch.object(sources, 'data_dir', return_value=default_cache), \
                patch('sys.stdout', out2), patch('sys.stderr', err2):
            default_code = refresh.main(['--no-fetch', '--references', str(self.refs_dir)])
        self.assertEqual(default_code, 2)
        self.assertIn(str(default_cache / 'fetch-manifest.json'), err2.getvalue())

    def test_validation_checks_user_scores_schema_and_reference_provenance(self):
        user_scores_path = self.refs_dir / 'user-scores.json'
        good_user_scores = user_scores_path.read_text(encoding='utf-8')

        bad_docs = [
            ('kind_not_maintainer', {
                'schema_version': 2,
                'scores': {'dot': {'aesthetics': {'level': 2.5, 'as_of': '2026-11-11', 'kind': 'user'}}},
            }),
            ('missing_schema_version', {
                'scores': {'dot': {'aesthetics': {'level': 2.5, 'as_of': '2026-11-11', 'kind': 'maintainer'}}},
            }),
            ('boolean_schema_version', {
                'schema_version': True,
                'scores': {'dot': {'aesthetics': {'level': 2.5, 'as_of': '2026-11-11', 'kind': 'maintainer'}}},
            }),
            ('missing_as_of', {
                'schema_version': 2,
                'scores': {'dot': {'aesthetics': {'level': 2.5, 'kind': 'maintainer'}}},
            }),
            ('unexpected_record_key', {
                'schema_version': 2,
                'scores': {'dot': {'aesthetics': {'level': 2.5, 'as_of': '2026-11-11', 'kind': 'maintainer', 'typo': 1}}},
            }),
        ]
        for label, bad_doc in bad_docs:
            with self.subTest(case=label):
                user_scores_path.write_text(json.dumps(bad_doc, indent=2) + '\n', encoding='utf-8', newline='\n')
                code, _, stderr = self._run_refresh('--no-fetch')
                self.assertEqual(code, 2, label)
                self.assertTrue(stderr.strip(), label)

        user_scores_path.write_text(good_user_scores, encoding='utf-8', newline='\n')

        # Uncited provenance in accounts.json fails validation and exits 2.
        accounts_path = self.refs_dir / 'accounts.json'
        accounts = json.loads(accounts_path.read_text(encoding='utf-8'))
        accounts['same_model']['grok-bot']['kind'] = 'user'
        accounts_path.write_text(json.dumps(accounts, indent=2) + '\n', encoding='utf-8', newline='\n')
        code, _, stderr = self._run_refresh('--no-fetch')
        self.assertEqual(code, 2)
        self.assertIn('grok-bot', stderr)

    def test_default_mode_fetches_before_rebuilding_in_topological_order(self):
        events = []
        orig_write_views = capability_catalog.write_default_files
        orig_validate = refresh.validate

        def fake_fetch(target):
            events.append(('fetch', Path(target)))

        def tracked_views(*args, **kwargs):
            events.append(('capability_catalog',))
            return orig_write_views(*args, **kwargs)

        def tracked_validate(*args, **kwargs):
            events.append(('validate',))
            return orig_validate(*args, **kwargs)

        with patch.object(sources, 'fetch', side_effect=fake_fetch), \
                patch.object(capability_catalog, 'write_default_files', side_effect=tracked_views), \
                patch.object(refresh, 'validate', side_effect=tracked_validate):
            code, _, stderr = self._run_refresh()
        self.assertEqual(code, 0, stderr)
        self.assertEqual([e[0] for e in events], ['fetch', 'capability_catalog', 'validate'])
        self.assertEqual(events[0][1], self.data_dir)


def _measurements(scale, models, conflicts=(), checks=(), tier_sha='aaaa'):
    """A minimal measurements document: one scaled board `b.code`, one scale-free source `s.price`, the tier source."""
    return {'generated_at': '2026-11-01T00:00:00+00:00', 'panel': {'models': ['anchor-m', 'panel-m']},
            'sources': {'b.code': {'scale': scale}, 's.price': {},
                        'tier.arena_overall': {'sha256': tier_sha,
                                               'tier_scale': {'anchor': 'anchor-m', 'anchor_raw': 1200.0, 'sd': 50.0}}},
            'conflicts': list(conflicts), 'checks': list(checks), 'models': models}


def _board_entry(raw, scale, raw_by_effort=None, as_of='2026-11-12'):
    entry = {'source': 'b.code', 'value': round((raw - scale['anchor_raw']) / scale['sd_panel'], 4), 'raw': raw,
             'as_of': as_of, 'fetched_at': f'{as_of}T08:00:00+00:00',
             'method': (f"board; z = (x - x_anchor) / sd_panel; x_anchor {scale['anchor_raw']:.6g} (anchor-m), "
                        f"sd_panel {scale['sd_panel']:.6g} over {scale['n_panel']} panel models")}
    if raw_by_effort:
        entry['raw_by_effort'] = raw_by_effort
        entry['z_by_effort'] = {e: round((r - scale['anchor_raw']) / scale['sd_panel'], 4) for e, r in raw_by_effort.items()}
    return entry


class ModelRowMergeTests(unittest.TestCase):
    """Rule (calibration/refresh.py, --model): only the named rows, conflicts and checks change; every entry the full
    refresh writes for the model is kept, a board z on the shipped source scale (z = (raw - anchor_raw) / sd_panel), the
    tier on the release snapshot's tier scale or map (round 6 item 8); an entry is left out only when its anchor reading
    moved or it is a z-score whose source has no shipped scale; a panel model, the anchor or a model no board lists is
    refused."""
    SHIPPED_SCALE = {'anchor': 'anchor-m', 'anchor_raw': 50.0, 'sd_panel': 10.0, 'n_panel': 12}
    FRESH_SCALE = {'anchor': 'anchor-m', 'anchor_raw': 50.0, 'sd_panel': 8.0, 'n_panel': 11}   # the panel shrank

    def shipped(self):
        return _measurements(self.SHIPPED_SCALE, {
            'anchor-m': {'coding': [_board_entry(50.0, self.SHIPPED_SCALE)]},
            'panel-m': {'coding': [_board_entry(40.0, self.SHIPPED_SCALE)]},
            'old-m': {'coding': [_board_entry(45.0, self.SHIPPED_SCALE)]}},
            conflicts=[{'model': 'old-m', 'field': 'image_input'}],
            checks=[{'model': 'old-m', 'check': 'c1'}, {'source': 'b.code', 'check': 'board-level'}])

    def fresh(self, scale=None, tier_sha='aaaa'):
        scale = scale or self.FRESH_SCALE
        return _measurements(scale, {
            'anchor-m': {'coding': [_board_entry(50.0, scale)]},
            'panel-m': {'coding': [_board_entry(40.0, scale)]},     # the panel's readings did not move
            'old-m': {'coding': [_board_entry(47.0, scale)]},
            'new-m': {'coding': [_board_entry(62.0, scale, {'low': 55.0, 'high': 62.0})],
                      'price': [{'source': 's.price', 'value': {'input': 1.0}, 'as_of': '2026-11-13'}],
                      'tier': [{'source': 'tier.arena_overall', 'value': 0.3, 'raw': 1215.0,
                                'method': 'arena.ai Overall 1215.0 at High standardized against anchor-m (1200.0, sd 50.00): '
                                          'tier 0.3000'}]}},
            conflicts=[{'model': 'new-m', 'field': 'image_input'}, {'model': 'old-m', 'field': 'x'}],
            checks=[{'model': 'old-m', 'check': 'c1'}, {'model': 'new-m', 'check': 'high_outside_adjacent_efforts'},
                    {'source': 'b.code', 'check': 'board-level'}],
            tier_sha=tier_sha)

    def test_only_the_named_rows_change(self):
        shipped = self.shipped()
        merged, _, _ = refresh.merge_model_rows(shipped, self.fresh(), ['new-m'])
        for key in ('panel', 'sources'):
            self.assertEqual(merged[key], shipped[key])
        for model in ('anchor-m', 'panel-m', 'old-m'):
            self.assertEqual(merged['models'][model], shipped['models'][model])
        self.assertEqual(sorted(merged['models']), ['anchor-m', 'new-m', 'old-m', 'panel-m'])
        # In the order the full build writes them, not appended.
        self.assertEqual(merged['conflicts'], [{'model': 'new-m', 'field': 'image_input'}, {'model': 'old-m', 'field': 'image_input'}])
        self.assertEqual(merged['checks'], [{'model': 'old-m', 'check': 'c1'},
                                            {'model': 'new-m', 'check': 'high_outside_adjacent_efforts'},
                                            {'source': 'b.code', 'check': 'board-level'}])

    def test_board_z_is_raw_on_the_shipped_scale_and_scale_free_entries_keep_their_own_dates(self):
        merged, _, notes = refresh.merge_model_rows(self.shipped(), self.fresh(), ['new-m'])
        row, scale = merged['models']['new-m'], self.SHIPPED_SCALE
        board = row['coding'][0]
        z = lambda raw: round((raw - scale['anchor_raw']) / scale['sd_panel'], 4)
        self.assertEqual(board['value'], z(board['raw']))
        self.assertEqual(board['z_by_effort'], {e: z(r) for e, r in board['raw_by_effort'].items()})
        self.assertIn(f"sd_panel {scale['sd_panel']:.6g} over {scale['n_panel']} panel models", board['method'])
        self.assertEqual((board['as_of'], board['fetched_at']), ('2026-11-12', '2026-11-12T08:00:00+00:00'))
        self.assertEqual(row['price'], self.fresh()['models']['new-m']['price'])
        self.assertTrue(any(refresh.ROUNDED_SCALE_NOTE in note for note in notes))

    def test_on_an_unchanged_scale_the_entry_is_kept_as_built(self):
        fresh = self.fresh(self.SHIPPED_SCALE)
        merged, left_out, notes = refresh.merge_model_rows(self.shipped(), fresh, ['new-m'])
        self.assertEqual(merged['models']['new-m'], fresh['models']['new-m'])
        self.assertEqual((left_out, notes), ([], []))
        self.assertEqual(merged['model_entries'], ['new-m'])
        _, _, notes = refresh.merge_model_rows(self.shipped(), self.fresh(self.SHIPPED_SCALE, tier_sha='bbbb'), ['new-m'])
        self.assertEqual(len(notes), 1)
        self.assertIn('tier.arena_overall', notes[0])

    def test_the_tier_is_put_on_the_release_snapshots_tier_scale_whatever_the_fresh_fetch_standardized(self):
        """Rule (refresh._tier_on_release_scale, round 6 item 8): an arena.ai Overall reading is standardized with the
        release snapshot's anchor rating and sd (tier = (raw - anchor_raw) / sd), not the fresh fetch's; without a
        recorded tier scale the merge stops."""
        shipped, fresh = self.shipped(), self.fresh(self.SHIPPED_SCALE)
        shipped['sources']['tier.arena_overall']['tier_scale'] = {'anchor': 'anchor-m', 'anchor_raw': 1190.0, 'sd': 40.0}
        merged, _, _ = refresh.merge_model_rows(shipped, fresh, ['new-m'])
        tier = merged['models']['new-m']['tier'][0]
        self.assertEqual(tier['value'], round((1215.0 - 1190.0) / 40.0, 4))
        self.assertTrue(tier['method'].endswith('standardized against anchor-m (1190.0, sd 40.00): tier 0.6250'), tier)
        del shipped['sources']['tier.arena_overall']['tier_scale']
        with self.assertRaisesRegex(refresh.Refused, 'no tier scale'):
            refresh.merge_model_rows(shipped, fresh, ['new-m'])

    def test_an_index_tier_is_read_through_the_release_snapshots_map_after_the_index_boards_drift(self):
        """Rule (refresh._tier_on_release_scale, round 6 item 8): an Artificial Analysis Intelligence Index reading goes
        through the release snapshot's index -> tier map (`tier_map`), the index board's common shift taken off first."""
        shipped, fresh = self.shipped(), self.fresh(self.SHIPPED_SCALE)
        shipped['sources']['tier.aa_index'] = {'tier_map': {'points': [[40.0, -1.0, 'a'], [50.0, 0.0, 'b'], [60.0, 1.0, 'c']]}}
        fresh['sources']['tier.aa_index'] = {}
        fresh['models']['new-m']['tier'] = [{'source': 'tier.aa_index', 'value': 9.9, 'board_value': 57.0, 'method': 'x'}]
        drift = {**dict.fromkeys(refresh.DRIFT_FIELDS), 'moved': True, 'n': 3, 'drift': 2.0, 'common_shift': True,
                 'spread': {'min': 1.5, 'max': 2.5, 'mad': 0.5}, 'snapshot_as_of': '2026-11-01', 'fetched_as_of': '2026-11-12'}
        still = {**dict.fromkeys(refresh.DRIFT_FIELDS), 'moved': False}
        with patch.object(refresh, 'board_drift', side_effect=lambda board, *_: drift if board == 'aa.index' else still):
            merged, _, _ = refresh.merge_model_rows(shipped, fresh, ['new-m'])
        tier = merged['models']['new-m']['tier'][0]
        self.assertEqual((tier['value'], tier['board_value'], tier['n']), (0.5, 55.0, 3))
        self.assertEqual(tier['neighbours'], {'below': {'x': 50.0, 'tier': 0.0, 'models': ['b']},
                                              'above': {'x': 60.0, 'tier': 1.0, 'models': ['c']}})
        self.assertEqual(tier['drift_correction']['drift'], 2.0)

    def test_a_moved_anchor_reading_or_an_unshipped_scale_leaves_the_entry_out_with_the_reason(self):
        moved = dict(self.FRESH_SCALE, anchor_raw=51.0)
        merged, left_out, _ = refresh.merge_model_rows(self.shipped(), self.fresh(moved), ['new-m'])
        self.assertNotIn('coding', merged['models']['new-m'])
        self.assertEqual(merged['models']['new-m']['tier'], self.fresh()['models']['new-m']['tier'])
        reasons = {(field, src): why for _, field, src, why in left_out}
        self.assertEqual(set(reasons), {('coding', 'b.code')})
        self.assertIn('anchor reading moved', reasons[('coding', 'b.code')])
        shipped = self.shipped()
        shipped['sources']['b.code'] = {}
        _, left_out, _ = refresh.merge_model_rows(shipped, self.fresh(), ['new-m'])
        self.assertEqual(left_out, [('new-m', 'coding', 'b.code', 'its source has no shipped scale and the entry is a z-score')])

    def test_a_model_no_board_lists_or_an_entry_that_cannot_be_merged_stops_the_merge(self):
        with self.assertRaisesRegex(ValueError, 'no board lists it'):
            refresh.merge_model_rows(self.shipped(), self.fresh(), ['absent-m'])
        fresh = self.fresh()
        fresh['models']['new-m']['coding'][0]['method'] = 'board; sd_panel restated in another wording'
        with self.assertRaisesRegex(ValueError, 'nothing written'):
            refresh.merge_model_rows(self.shipped(), fresh, ['new-m'])

    def test_a_panel_model_the_anchor_or_a_model_without_a_row_is_refused(self):
        acc = {'models': {m: {} for m in ('anchor-m', 'panel-m', 'sentinel-m', 'new-m')},
               'calibration_panel': {'anchor': 'anchor-m', 'models': ['anchor-m', 'sentinel-m']}}
        for name in ('anchor-m', 'panel-m', 'sentinel-m'):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'calibration panel'):
                refresh.refuse_panel_models([name], acc, self.shipped())
        with self.assertRaisesRegex(ValueError, 'no row'):
            refresh.refuse_panel_models(['unknown-m'], acc, self.shipped())
        refresh.refuse_panel_models(['new-m'], acc, self.shipped())

    def test_the_command_refuses_a_panel_model_before_any_download(self):
        root = Path(tempfile.mkdtemp(prefix='refresh-model-'))
        self.addCleanup(shutil.rmtree, root, True)
        shutil.copyfile(reference_provenance.REFERENCES / 'accounts.json', root / 'accounts.json')
        anchor = json.loads((root / 'accounts.json').read_text(encoding='utf-8'))['calibration_panel']['anchor']
        (root / 'public-measurements.json').write_text(json.dumps({'panel': {'models': []}}), encoding='utf-8')
        err = io.StringIO()
        with patch.object(sources, 'fetch', side_effect=AssertionError('fetched')), patch('sys.stderr', err):
            code = refresh.main(['--data', str(root / 'data'), '--references', str(root), '--model', anchor])
        self.assertEqual(code, refresh.REFUSED)
        self.assertNotIn(code, refresh.LEVEL.values())
        self.assertIn('full refresh', err.getvalue())

    def test_adding_a_non_panel_model_keeps_the_scale_targets_ceilings_and_panel_record(self):
        """End to end over synthetic boards: the shipped panel, sd_panel, panel_change, level 1..3 targets and level-3
        ceilings stay (round 6 item 8: a model entry never enters a target or a ceiling)."""
        root = Path(tempfile.mkdtemp(prefix='refresh-model-e2e-'))
        self.addCleanup(shutil.rmtree, root, True)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        shipped_data, refs = build_calibration_fixture(root / 'shipped', now)
        refresh.rebuild(shipped_data, now=now, references=refs)
        added = 'gpt-5.6-sol'
        with patch(f'{__name__}.PANEL_MODELS', (*PANEL_MODELS, (added, 'GPT-5.6 Sol', 'OpenAI'))):
            fresh_data, _ = build_calibration_fixture(root / 'fresh', now)
        read = lambda name: json.loads((refs / name).read_text(encoding='utf-8'))
        meas_before, scores_before = read('public-measurements.json'), read('five-axis-scores.json')
        refresh.rebuild(shipped_data, now=now, references=refs, models=[added], pages=fresh_data)
        meas_after, scores_after = read('public-measurements.json'), read('five-axis-scores.json')
        self.assertTrue(meas_after['models'][added])
        self.assertEqual({m: r for m, r in meas_after['models'].items() if m != added},
                         {m: r for m, r in meas_before['models'].items() if m != added})
        self.assertEqual(meas_after['panel'], meas_before['panel'])
        for key in ('panel', 'panel_n', 'sd_panel', 'panel_change'):
            self.assertEqual(scores_after['anchor'].get(key), scores_before['anchor'].get(key), key)

        def walk(node, path=()):
            if isinstance(node, dict):
                for key, value in node.items():
                    yield from walk(value, (*path, key))
            else:
                yield path, node
        self.assertTrue(any('ceiling' in path for path, _ in walk(scores_after['anchor']['targets'])))
        self.assertEqual(scores_after['anchor']['targets'], scores_before['anchor']['targets'])

    def test_with_an_unchanged_panel_the_model_row_and_scores_equal_a_full_refresh(self):
        """Rule: for a named non-panel model, --model gives the same measurement row and checks as a full refresh over
        the same boards, for a model arena.ai lists and for one listed only on the index board; its five-axis block is
        the full refresh's measurements scored with the model as a model entry (round 6 item 8: the release's fits)."""
        cases = (('gemini-3.9-flash-high', 'gemini-3.8-flash-high', ('Gemini 3.9 Flash', 'Google'), 'arena'),
                 ('grok-4.9', 'grok-4.7', ('Grok 4.9', 'xAI'), 'aa'))
        now = datetime.now(timezone.utc).replace(microsecond=0)
        for added, sibling, (display, lab), listing in cases:
            with self.subTest(model=added):
                root = Path(tempfile.mkdtemp(prefix='refresh-model-eq-'))
                self.addCleanup(shutil.rmtree, root, True)
                partial = [(added, display, lab)] if listing == 'arena' else []
                aa_only = [(added, display, lab)] if listing == 'aa' else []
                boards, refs = build_calibration_fixture(root / 'shipped', now, arena_partial=partial, aa_only=aa_only)
                refresh.rebuild(boards, now=now, references=refs)
                acc = json.loads((refs / 'accounts.json').read_text(encoding='utf-8'))
                self.assertNotIn(added, acc['models'])
                row = copy.deepcopy(acc['models'][sibling])
                row['listed_as'] = {'aa': display, **({'arena': display} if listing == 'arena' else {})}
                acc['models'][added] = row
                (refs / 'accounts.json').write_text(json.dumps(acc, indent=1, ensure_ascii=False) + '\n',
                                                    encoding='utf-8', newline='\n')
                full_refs = root / 'full'
                shutil.copytree(refs, full_refs)
                refresh.rebuild(boards, now=now, references=full_refs)
                refresh.rebuild(boards, now=now, references=refs, models=[added], pages=boards)
                read = lambda where, name: json.loads((where / name).read_text(encoding='utf-8'))
                merged_meas, full_meas = read(refs, 'public-measurements.json'), read(full_refs, 'public-measurements.json')
                merged_scores, full_scores = read(refs, 'five-axis-scores.json'), read(full_refs, 'five-axis-scores.json')
                self.assertEqual(merged_meas['panel'], full_meas['panel'])
                self.assertEqual([e['source'] for e in full_meas['models'][added]['tier']],
                                 ['tier.arena_overall' if listing == 'arena' else 'tier.aa_index'])
                self.assertEqual(merged_meas['models'][added], full_meas['models'][added])
                self.assertEqual(merged_meas['checks'], full_meas['checks'])
                self.assertEqual(merged_meas['conflicts'], full_meas['conflicts'])
                self.assertEqual(merged_meas['model_entries'], [added])
                as_entry = five_axis.build({**full_meas, 'model_entries': [added]}, acc,
                                           json.loads((refs / 'plan-templates.json').read_text(encoding='utf-8')), now=now)
                self.assertEqual(merged_scores['models'][added], as_entry['models'][added])

    def test_the_blast_radius_counts_only_other_models_values(self):
        before = {'models': {'a': {'x': 1.0, 'y': {'z': 2}}, 'new-m': {'x': 0}}}
        after = {'models': {'a': {'x': 1.5, 'y': {'z': 2}}, 'new-m': {'x': 9}}}
        self.assertIn('1 values of 1 other models moved, by field: x 1', refresh.blast_radius(before, after, ['new-m']))
        self.assertEqual(refresh.blast_radius(before, before, ['new-m']), 'no value of another model moved')


class ModelEntryTests(unittest.TestCase):
    """Rule (calibration/refresh.py, --model): what --model read is kept as that model's own dated entry in the cache
    (DIR/models/<id>/: its pages, each with url and fetched_at), separate from the release snapshot; a later --no-fetch
    rebuild takes every other row from the snapshot and the model's rows from its entry, byte for byte the same files;
    --model never changes another row's measured value and never overwrites a page of the release snapshot."""
    GENERATED = ('public-measurements.json', 'five-axis-scores.json', 'model-capabilities.json', 'model-capabilities.md')
    ADDED = 'gpt-5.6-sol'

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='refresh-entry-model-'))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.now = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
        self.cache, self.refs = build_calibration_fixture(self.root / 'shipped', self.now)
        refresh.rebuild(self.cache, now=self.now, references=self.refs)        # the release: shipped = snapshot build
        with patch(f'{__name__}.PANEL_MODELS', (*PANEL_MODELS, (self.ADDED, 'GPT-5.6 Sol', 'OpenAI'))):
            self.pages, _ = build_calibration_fixture(self.root / 'fresh', self.now)     # newer boards listing it

    def generated(self):
        return {name: (self.refs / name).read_bytes() for name in self.GENERATED}

    def snapshot(self):
        return {str(f.relative_to(self.cache)): hashlib.sha256(f.read_bytes()).hexdigest()
                for f in self.cache.rglob('*') if f.is_file() and f.relative_to(self.cache).parts[0] != refresh.MODEL_ENTRIES}

    def test_a_no_fetch_rebuild_after_model_writes_the_same_bytes_from_the_entry(self):
        refresh.rebuild(self.cache, now=self.now, references=self.refs, models=[self.ADDED], pages=self.pages)
        after_model = self.generated()
        self.assertTrue(json.loads(after_model['public-measurements.json'])['models'][self.ADDED])
        self.assertEqual(list(refresh.model_entries(self.cache)), [self.ADDED])
        refresh.rebuild(self.cache, now=self.now, references=self.refs)     # what `--no-fetch` runs
        self.assertEqual(self.generated(), after_model)
        self.assertEqual(refresh.retire_model_entries(self.cache), [self.ADDED])
        self.assertFalse((self.cache / refresh.MODEL_ENTRIES).exists())

    def test_adding_an_entry_and_removing_it_again_writes_every_generated_file_as_it_was(self):
        """Rule (round 7, no clock in a generated file): the generated files are a function of their inputs; their date
        is the latest date of a reading (measurements.inputs_date) and measurements_sha256 hashes that content, so adding a
        model entry and retiring it, on later runs, gives back every byte."""
        start = self.generated()
        refresh.rebuild(self.cache, now=self.now + timedelta(hours=1), references=self.refs, models=[self.ADDED],
                        pages=self.pages)
        self.assertNotEqual(self.generated(), start)
        self.assertEqual(refresh.retire_model_entries(self.cache), [self.ADDED])
        refresh.rebuild(self.cache, now=self.now + timedelta(days=3), references=self.refs)
        self.assertEqual(self.generated(), start)

    def test_model_fetches_into_its_own_entry_and_never_writes_a_page_of_the_release_snapshot(self):
        before = self.snapshot()

        def fetch(target, only=None):      # the download, served from the newer boards
            for f in self.pages.iterdir():
                shutil.copyfile(f, Path(target) / f.name)
        out = io.StringIO()
        with patch.object(sources, 'fetch', side_effect=fetch), patch.object(sources, 'fetch_releases'), \
                patch('sys.stdout', out), patch('sys.stderr', out):
            code = refresh.main(['--data', str(self.cache), '--references', str(self.refs), '--model', self.ADDED])
        self.assertIn(code, refresh.LEVEL.values(), out.getvalue())
        self.assertEqual(self.snapshot(), before)
        entry = self.cache / refresh.MODEL_ENTRIES / self.ADDED
        doc = refresh.model_entries(self.cache)[self.ADDED]
        self.assertEqual((doc['schema'], doc['model']), (refresh.ENTRY_SCHEMA, self.ADDED))
        self.assertTrue(doc['fetched_at'])
        self.assertEqual(sources.manifest(entry), sources.manifest(self.pages))
        self.assertEqual(sorted(p.name for p in (self.cache / refresh.MODEL_ENTRIES).iterdir()), [self.ADDED])

    def assert_no_other_row_moved(self, added, before, after, rows_added=0):
        """Every other row and every top-level quantity of five-axis-scores.json as before; only the added model's own
        row, its records in the per-model lists, the tier-source counts (their total grows by the rows added) and
        measurements_sha256 may differ."""
        self.assertTrue(after['models'][added])
        self.assertEqual({m: r for m, r in after['models'].items() if m != added},
                         {m: r for m, r in before['models'].items() if m != added})
        others = lambda doc, key: [e for e in doc[key] if e.get('model') != added]
        for key in sorted(set(before) | set(after)):
            if key in ('models', 'generated_at', 'measurements_sha256', 'tier_sources'):
                continue
            with self.subTest(model=added, key=key):
                if isinstance(after.get(key), list) and any(isinstance(e, dict) and 'model' in e for e in after[key]):
                    self.assertEqual(others(after, key), others(before, key))
                else:
                    self.assertEqual(after.get(key), before.get(key))
        self.assertEqual(sum(after['tier_sources'].values()), sum(before['tier_sources'].values()) + rows_added)

    def test_adding_a_model_changes_no_other_row_fit_step_link_target_or_panel(self):
        """Rule (owner 2026-11-19, round 6 item 8; round 8 item 4): fleet fits are frozen per release; a model entry's
        rows never enter a fit, a step, a link, a correlation, a target, a ceiling or the panel, and the declared
        structure (version ladders, distances, relatives) stays the release's. Three cases: a released row the release
        snapshot does not read (it keeps its place in the structure), a new row (it takes none), and a released row the
        snapshot reads (refused: its readings belong to the release's fits)."""
        read = lambda: json.loads((self.refs / 'five-axis-scores.json').read_text(encoding='utf-8'))
        before = read()                                       # 1. a released row without readings
        _, doc = refresh.rebuild(self.cache, now=self.now, references=self.refs, models=[self.ADDED], pages=self.pages)
        self.assertEqual(doc['_model_merge']['blast_radius'], 'no value of another model moved')
        self.assert_no_other_row_moved(self.ADDED, before, read())
        meas = json.loads((self.refs / 'public-measurements.json').read_text(encoding='utf-8'))
        self.assertEqual((meas['model_entries'], meas['released_entry_rows']), ([self.ADDED], [self.ADDED]))

        new, display = 'gpt-5.65-sol', 'GPT-5.65 Sol'           # 2. a new row, at a version of its own in its series
        acc = json.loads((self.refs / 'accounts.json').read_text(encoding='utf-8'))
        acc['models'][new] = {**copy.deepcopy(acc['models'][self.ADDED]), 'listed_as': {'arena': display, 'aa': display}}
        acc['series_versions']['versions'][new] = 5.65
        (self.refs / 'accounts.json').write_text(json.dumps(acc, indent=1, ensure_ascii=False) + '\n', encoding='utf-8',
                                                newline='\n')
        with patch(f'{__name__}.PANEL_MODELS', (*PANEL_MODELS, (new, display, 'OpenAI'))):
            pages, _ = build_calibration_fixture(self.root / 'fresh-new', self.now)
        before = read()
        _, doc = refresh.rebuild(self.cache, now=self.now, references=self.refs, models=[new], pages=pages)
        self.assertEqual(doc['_model_merge']['blast_radius'], 'no value of another model moved')
        self.assert_no_other_row_moved(new, before, read(), rows_added=1)
        meas = json.loads((self.refs / 'public-measurements.json').read_text(encoding='utf-8'))
        self.assertEqual((meas['model_entries'], meas['released_entry_rows']), (sorted([self.ADDED, new]), [self.ADDED]))

        read = next(m for m, r in meas['models'].items()             # 3. a released row the release reads
                    if any(r.values()) and m not in meas['model_entries'] and m not in meas['panel']['models'])
        files, stored = self.generated(), refresh.model_entries(self.cache)
        with self.assertRaisesRegex(refresh.Refused, f"--model {read}: the release reads {read} "
                                                     ".*belong to the release's fits.*no entry stored"):
            refresh.rebuild(self.cache, now=self.now, references=self.refs, models=[read], pages=self.pages)
        self.assertEqual((self.generated(), refresh.model_entries(self.cache)), (files, stored))

    def test_model_never_changes_another_rows_measured_value(self):
        shipped = json.loads((self.refs / 'public-measurements.json').read_text(encoding='utf-8'))
        refresh.rebuild(self.cache, now=self.now, references=self.refs, models=[self.ADDED], pages=self.pages)
        merged = json.loads((self.refs / 'public-measurements.json').read_text(encoding='utf-8'))
        self.assertEqual({m: r for m, r in merged['models'].items() if m != self.ADDED},
                         {m: r for m, r in shipped['models'].items() if m != self.ADDED})
        # a shipped row the cache does not give (a snapshot that is not this release's): refused, nothing written
        other = next(m for m, r in shipped['models'].items() if m != self.ADDED and r.get('coding'))
        shipped['models'][other]['coding'][0]['value'] += 1.0
        (self.refs / 'public-measurements.json').write_text(json.dumps(shipped, indent=1, ensure_ascii=False) + '\n',
                                                           encoding='utf-8', newline='\n')
        shutil.rmtree(self.cache / refresh.MODEL_ENTRIES)
        files = self.generated()
        with self.assertRaisesRegex(refresh.Refused, other):
            refresh.rebuild(self.cache, now=self.now, references=self.refs, models=[self.ADDED], pages=self.pages)
        self.assertEqual(self.generated(), files)
        self.assertEqual(refresh.model_entries(self.cache), {})


class BoardDriftTests(unittest.TestCase):
    """Rule (calibration/refresh.py board_drift, --model): a reading taken after the release snapshot is put on the
    shipped scale by the board's own drift, the median over the panel members the board lists in both fetches of
    (raw now - raw then), and then converted with the shipped anchor and sd_panel, the drift applied only where the
    members show a common shift (|drift| > their median absolute deviation); with fewer than two such members the
    reading is left out; on a board that did not move the merge is the plain one."""
    SCALE = {'anchor': 'anchor-m', 'anchor_raw': 50.0, 'sd_panel': 10.0, 'n_panel': 3}
    PANEL = {'anchor-m': 50.0, 'panel-m': 40.0, 'third-m': 45.0}

    def doc(self, readings, scale, as_of):
        out = _measurements(scale, {m: {'coding': [_board_entry(r, scale, as_of=as_of)]} for m, r in readings.items()})
        out['panel'] = {'models': sorted(self.PANEL)}
        out['sources']['b.code']['as_of'] = as_of
        return out

    def shipped(self):
        return self.doc(self.PANEL, self.SCALE, '2026-11-10')

    def fresh(self, shift, members=None, new_raw=62.0, each=None):
        """The board `shift` higher for every model (`each`: a member's own change instead); `new-m` measured at
        `new_raw` before the shift."""
        listed = {m: r + (each or {}).get(m, shift) for m, r in self.PANEL.items() if members is None or m in members}
        scale = dict(self.SCALE, anchor_raw=listed.get('anchor-m', self.SCALE['anchor_raw'] + shift))
        doc = self.doc(listed, scale, '2026-11-19')
        doc['models']['new-m'] = {'coding': [_board_entry(new_raw + shift, scale, {'low': 55.0 + shift, 'high': new_raw + shift},
                                                          as_of='2026-11-19')]}
        return doc

    def test_a_common_shift_is_corrected_back_to_the_unshifted_reading(self):
        merged, left_out, notes = refresh.merge_model_rows(self.shipped(), self.fresh(3.0), ['new-m'])
        board = merged['models']['new-m']['coding'][0]
        z = lambda raw: round((raw - self.SCALE['anchor_raw']) / self.SCALE['sd_panel'], 4)
        self.assertEqual(left_out, [])
        self.assertEqual(board['value'], z(62.0))
        self.assertEqual(board['z_by_effort'], {'low': z(55.0), 'high': z(62.0)})
        self.assertEqual(board['drift_correction'], {
            'drift': 3.0, 'n': 3, 'members': sorted(self.PANEL), 'spread': {'min': 3.0, 'max': 3.0, 'mad': 0.0},
            'common_shift': True, 'snapshot_as_of': '2026-11-10', 'fetched_as_of': '2026-11-19'})
        self.assertTrue(any('b.code moved since the release snapshot: drift 3' in n for n in notes))

    def test_the_drift_is_the_members_median_change_when_they_show_a_common_shift(self):
        # members moved 3, 3 and 9: median 3 (the mean would be 5), deviation median 0 < 3, so corrected by 3
        each = {'anchor-m': 3.0, 'panel-m': 3.0, 'third-m': 9.0}
        merged, _, _ = refresh.merge_model_rows(self.shipped(), self.fresh(3.0, each=each), ['new-m'])
        board = merged['models']['new-m']['coding'][0]
        self.assertEqual(board['value'], round((62.0 - 50.0) / 10.0, 4))
        self.assertEqual((board['drift_correction']['drift'], board['drift_correction']['spread']['mad'],
                          board['drift_correction']['common_shift']), (3.0, 0.0, True))

    def test_without_a_common_shift_the_reading_is_merged_plain_and_the_spread_recorded(self):
        # members moved -3, 0.5 and 4: median 0.5, deviation median 3.5 >= 0.5, so no common shift: shift 0
        each = {'anchor-m': -3.0, 'panel-m': 0.5, 'third-m': 4.0}
        merged, left_out, notes = refresh.merge_model_rows(self.shipped(), self.fresh(0.5, each=each), ['new-m'])
        board = merged['models']['new-m']['coding'][0]
        self.assertEqual(left_out, [])
        self.assertEqual(board['value'], round((62.0 + 0.5 - 50.0) / 10.0, 4))      # its own raw, not corrected
        self.assertEqual(board['drift_correction'], {
            'drift': 0.5, 'n': 3, 'members': sorted(self.PANEL), 'spread': {'min': -3.0, 'max': 4.0, 'mad': 3.5},
            'common_shift': False, 'snapshot_as_of': '2026-11-10', 'fetched_as_of': '2026-11-19'})
        self.assertIn('no common shift', board['method'])
        self.assertTrue(any('b.code moved' in n and 'not corrected' in n for n in notes))

    def test_with_one_panel_member_in_both_fetches_the_reading_is_left_out(self):
        merged, left_out, _ = refresh.merge_model_rows(self.shipped(), self.fresh(3.0, members={'anchor-m'}), ['new-m'])
        self.assertNotIn('coding', merged['models']['new-m'])
        self.assertEqual([(f, src) for _, f, src, _ in left_out], [('coding', 'b.code')])
        self.assertIn('1 panel member(s) are listed in both', left_out[0][3])

    def test_on_a_board_that_did_not_move_the_merge_is_the_plain_one(self):
        shipped = self.shipped()
        for scale in (self.SCALE, dict(self.SCALE, sd_panel=8.0)):        # the same scale, and a rescaled one
            with self.subTest(sd=scale['sd_panel']):
                fresh = self.fresh(0.0)
                fresh['sources']['b.code']['scale'] = scale
                merged, left_out, notes = refresh.merge_model_rows(shipped, fresh, ['new-m'])
                entry = fresh['models']['new-m']['coding'][0]
                plain, _, _ = refresh._on_shipped_scale('coding', entry, shipped['sources'], fresh['sources'])
                self.assertEqual(merged['models']['new-m']['coding'], [plain])
                self.assertNotIn('drift_correction', plain)
                self.assertFalse(any('moved since the release snapshot' in n for n in notes))


class AaRecordTests(unittest.TestCase):
    """`sources.aa_records`: a record is the object that owns an `intelligenceIndex` key, whatever its other keys."""

    def _records(self, payload):
        with tempfile.TemporaryDirectory() as tmp:
            page = f'<html><body><script>self.__next_f.push([1,{json.dumps(json.dumps(payload))}])</script></body></html>'
            (Path(tmp) / 'aa-models.html').write_text(page, encoding='utf-8', newline='\n')
            return sources.aa_records(Path(tmp))

    def test_the_owner_of_the_index_key_is_read_whatever_its_leading_keys_and_nesting(self):
        leading_id = {'id': 'a1', 'release': {'slug': 'model-a', 'name': 'Model A'}, 'name': 'Model A (High)',
                      'intelligenceIndex': 51.5, 'scores': {'mmmu-pro': 0.7}}
        leading_slug = {'slug': 'model-b-high', 'name': 'Model B (High)', 'intelligenceIndex': 48.0}
        index_first = {'intelligenceIndex': None, 'name': 'Model C'}
        payload = {'models': [leading_id, leading_slug], 'wrapped': {'rows': [{'cell': index_first}]},
                   'release': {'slug': 'model-a', 'name': 'Model A', 'modelNames': ['Model A (High)']}}
        self.assertEqual(self._records(payload), [leading_id, leading_slug, index_first])

    def test_a_listing_is_read_once_and_an_object_without_a_name_is_no_record(self):
        record = {'id': 'a1', 'name': 'Model A (High)', 'intelligenceIndex': 51.5}
        payload = [record, dict(record, id='a1-again'), {'intelligenceIndex': 40.0}]
        self.assertEqual(self._records(payload), [record])


class AaReleasePageTests(unittest.TestCase):
    """Artificial Analysis embeds records in its leaderboard page only for releases it does not mark deprecated; a
    deprecated release is named without records and has a page of its own. Rules: a catalogue model that no leaderboard
    record matches, but a deprecated release is listed under, is read from that release's page; a reading of a release
    page equals the reading of the same record on the leaderboard; each reading cites the page it came from; a page that
    cannot be read is a surfaced missing source, never an older copy."""
    DEPRECATED = ('gpt-6-astra', 'gpt-6.1-sol')

    def setUp(self):
        self.now = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)

    def fixture(self, **kwargs):
        root = Path(tempfile.mkdtemp(prefix='aa-release-'))
        self.addCleanup(shutil.rmtree, root, True)
        return build_calibration_fixture(root, self.now, **kwargs)

    def build(self, data):
        refs = data.parent / 'references'
        acc = json.loads((refs / 'accounts.json').read_text(encoding='utf-8'))
        return measurements.Builder(data, acc).build()

    def build_with(self, data, models):
        """The measurements of `data` for the catalogue `models` (the shipped one, with rows added)."""
        refs = data.parent / 'references'
        acc = json.loads((refs / 'accounts.json').read_text(encoding='utf-8'))
        return measurements.Builder(data, {**acc, 'models': models}).build()

    @staticmethod
    def page_url(model):
        return sources.AA_RELEASE_URL.format(release_slug(model))

    @staticmethod
    def catalogue(data):
        return json.loads((data.parent / 'references' / 'accounts.json').read_text(encoding='utf-8'))['models']

    @staticmethod
    def rewrite_page(data, model, change):
        """Apply `change` to every own-release record of `model`'s release page in `data`."""
        page = data / sources.AA_RELEASE_FILE.format(release_slug(model))
        payload = json.loads(json.loads(sources.AA_RSC_PUSH.findall(page.read_text(encoding='utf-8'))[0]))
        for record in payload['models']:
            if record.get('release', {}).get('slug') == release_slug(model):
                change(record)
        page.write_text(rsc_page(payload), encoding='utf-8')

    def test_a_release_page_reading_equals_the_leaderboard_reading_of_the_same_record_and_cites_the_page(self):
        data, _ = self.fixture(aa_deprecated=self.DEPRECATED)
        on_leaderboard = self.build(self.fixture()[0])
        on_pages = self.build(data)
        read = [r for r in measurements.aa_records_read(data, self.catalogue(data), READINGS)[0] if 'page' in r]
        self.assertTrue(read)
        self.assertEqual({sources.base_name(r['name']) for r in read}, {'GPT-6 Astra', 'GPT-6.1 Sol'})
        self.assertNotIn(1.0, [r['intelligenceIndex'] for r in read])      # a comparison record or a table row was read
        cited = ('url', 'fetched_at', 'as_of')
        for model, fields in on_pages['models'].items():
            for field, entries in fields.items():
                for entry in entries:
                    from_page = entry.get('url') == self.page_url(model)
                    if model in self.DEPRECATED and entry['source'].startswith(('aa.', 'derived.community_mix')):
                        self.assertTrue(from_page, (model, field, entry['source']))
                    else:
                        self.assertNotIn(entry.get('url'), {self.page_url(m) for m in self.DEPRECATED}, (model, field))
        strip = lambda doc: {m: {f: [{k: v for k, v in e.items() if k not in cited} for e in es] for f, es in fields.items()}
                             for m, fields in doc['models'].items()}
        self.assertEqual(strip(on_pages), strip(on_leaderboard))
        self.assertEqual(on_pages['panel'], on_leaderboard['panel'])
        self.assertEqual({s: r.get('scale') for s, r in on_pages['sources'].items()},
                         {s: r.get('scale') for s, r in on_leaderboard['sources'].items()})
        self.assertEqual(set(on_pages['sources']['aa.index']['release_pages']),
                         {sources.AA_RELEASE_FILE.format(release_slug(m)) for m in self.DEPRECATED})
        self.assertNotIn('release_pages', on_leaderboard['sources']['aa.index'])
        mix = [c['url'] for c in on_pages['sources']['derived.community_mix']['citations'] if c['source'] == 'aa.price']
        self.assertEqual(sorted(mix[1:]), sorted(self.page_url(m) for m in self.DEPRECATED))      # each page cited, after the leaderboard
        self.assertEqual(on_pages['missing_sources'], on_leaderboard['missing_sources'])

    def test_the_source_is_as_old_as_the_oldest_page_read(self):
        data, _ = self.fixture(aa_deprecated=self.DEPRECATED)
        manifest_path = data / sources.MANIFEST
        doc = json.loads(manifest_path.read_text(encoding='utf-8'))
        page = sources.AA_RELEASE_FILE.format(release_slug('gpt-6-astra'))
        older = (self.now - timedelta(days=30)).isoformat(timespec='seconds')
        doc['files'][page]['fetched_at'] = older
        manifest_path.write_text(json.dumps(doc), encoding='utf-8')
        built = self.build(data)
        self.assertEqual(built['sources']['aa.index']['as_of'], older[:10])
        self.assertEqual(built['sources']['aa.index']['fetched_at'], self.now.isoformat(timespec='seconds'))
        self.assertGreaterEqual(data_age.age_days(five_axis.source_dates(built)['aa.index']['as_of'], self.now), 30)
        gpt6 = next(e for e in built['models']['gpt-6-astra']['price'] if e['source'] == 'aa.price')
        self.assertEqual((gpt6['url'], gpt6['fetched_at'], gpt6['as_of']), (self.page_url('gpt-6-astra'), older, older[:10]))

    def test_a_page_that_cannot_be_read_is_a_surfaced_missing_source_and_its_model_gets_no_reading(self):
        page = sources.AA_RELEASE_FILE.format(release_slug('gpt-6-astra'))
        for case in ('fetch failed', 'never fetched', 'no record of its release'):
            with self.subTest(case=case):
                data, _ = self.fixture(aa_deprecated=('gpt-6-astra',))
                manifest_path = data / sources.MANIFEST
                doc = json.loads(manifest_path.read_text(encoding='utf-8'))
                if case == 'fetch failed':
                    (data / page).unlink()
                    doc['files'][page] = {'url': self.page_url('gpt-6-astra'), 'fetched_at': self.now.isoformat(), 'status': 'missing',
                                          'error': 'URLError: offline'}
                elif case == 'never fetched':
                    (data / page).unlink()
                    del doc['files'][page]
                else:
                    (data / page).write_text(rsc_page({'models': [{'name': 'Other (High)', 'intelligenceIndex': 1.0,
                                                                    'release': {'slug': 'other', 'name': 'Other'}}]}), encoding='utf-8')
                manifest_path.write_text(json.dumps(doc), encoding='utf-8')
                built = self.build(data)
                row = built['missing_sources'][page]
                self.assertEqual(row['url'], self.page_url('gpt-6-astra'))
                self.assertTrue(five_axis.source_dates(built)[page]['missing'])
                self.assertEqual(row['reason'], {'fetch failed': 'URLError: offline',
                                                 'never fetched': 'not fetched; run `agentcompass data refresh`',
                                                 'no record of its release': 'the page carries no record of its release for '
                                                                             'gpt-6-astra (no record of the planned base name '
                                                                             'that the matcher finds for it)'}[case])
                readings = [e for fields in built['models']['gpt-6-astra'].values() for e in fields
                            if e['source'].startswith(('aa.', 'tier.aa', 'derived.community_mix'))]
                self.assertEqual(readings, [])
                self.assertEqual('release_pages' in built['sources']['aa.index'], case == 'no record of its release')    # fetched

    @staticmethod
    def release(slug, name, deprecated, names, date):
        return {'slug': slug, 'name': name, 'deprecated': deprecated, 'releaseDate': date, 'modelNames': names}

    def plan(self, releases, catalogue, leaderboard=({'name': 'Model A (High)', 'intelligenceIndex': 50.0},)):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / 'aa-models.html').write_text(rsc_page([*leaderboard, *releases]), encoding='utf-8')
            return measurements.aa_release_plan(target, catalogue, READINGS)

    def test_the_releases_to_read_are_the_deprecated_ones_a_catalogue_model_is_listed_under(self):
        release = self.release
        releases = [release('model-a', 'Model A', False, ['Model A (High)'], '2026-09-01'),
                    release('model-a-old', 'Model A', True, ['Model A (High)'], '2025-01-01'),    # the leaderboard has a record
                    release('model-b', 'Model B', True, ['Model B (High)', 'Model B (Low)'], '2026-09-01'),
                    release('model-c-new', 'Model C (0902)', True, [], '2026-09-02'),      # one unnamed variant: listed by its name
                    release('model-c-old', 'Model C', True, [], '2026-08-03'),
                    release('model-f-new', 'Model F (0902)', True, [], '2026-09-02'),
                    release('model-f-old', 'Model F', True, [], '2026-08-03'),
                    release('model-g', 'Model G', True, ['Model G (High)'], '2025-01-01'),
                    release('model-e', 'Model E', False, [], '2026-09-01'),                # not deprecated, no record yet
                    release('retired', 'Retired', True, ['Retired (High)'], '2025-01-01')]
        catalogue = {'model-a': {}, 'model-b': {'listed_as': {'aa': 'Model B'}}, 'model-c': {'listed_as': {'aa': 'Model C'}},
                     'model-c-free': {}, 'model-f': {}, 'model-d': {}, 'model-e': {}, 'note': 'a note, not a row',
                     'model-h': {'listed_as': {'aa': 'Model A'}},                  # declared name the leaderboard lists
                     'model-g': {'listed_as': {'aa': 'Another Name'}}}             # declared name it does not, the id it does
        leaderboard = ({'name': 'Model A (High)', 'intelligenceIndex': 50.0}, {'name': 'Model G (High)', 'intelligenceIndex': 40.0})
        self.assertEqual(self.plan([], catalogue, leaderboard=()), ({}, {}))      # no release named
        plan, ambiguous = self.plan(releases, catalogue, leaderboard)
        self.assertEqual(ambiguous, {})
        # model-a, model-h, model-g are on the leaderboard (by id, by declared name, by id with another declared name): not read
        # from a deprecated release; model-b is the only release of its base; a dated release name ("Model C (0902)") is
        # another snapshot with a base name of its own (sources.base_name keeps the date, round 5 review A), so model-c,
        # model-c-free and model-f read the undated release of their base name; model-d, model-e, retired: no deprecated
        # release.
        self.assertEqual(plan, {'model-b': ('model-b', 'Model B'), 'model-c': ('model-c-old', 'Model C'),
                                'model-c-free': ('model-c-old', 'Model C'), 'model-f': ('model-f-old', 'Model F')})
        self.assertEqual(measurements.aa_release_files(plan),
                         {f'aa-release-{s}.html': sources.AA_RELEASE_URL.format(s) for s in ('model-b', 'model-c-old', 'model-f-old')})

    def test_the_release_pages_are_listed_in_slug_order_whatever_the_hash_seed(self):
        """The file written from the plan (`release_pages` in public-measurements.json) must not depend on the order a set
        iterates in, which follows PYTHONHASHSEED: the files are listed in slug order. Forty slugs make an accidentally
        sorted set order practically impossible."""
        plan = {f'model-{i}': (f'release-{(i * 7) % 40:02d}', 'Base') for i in range(40)}
        slugs = [f'release-{i:02d}' for i in range(40)]
        files = measurements.aa_release_files(plan)
        self.assertEqual(list(files), [sources.AA_RELEASE_FILE.format(s) for s in slugs])
        self.assertEqual(list(files.values()), [sources.AA_RELEASE_URL.format(s) for s in slugs])

    def test_a_slug_that_cannot_be_a_url_and_a_file_name_names_no_release(self):
        hostile = ('../escape', 'a/b', 'ok/../x', 'x y', 'a%2fb', 'a?b=1', '..', '.hidden', 'é')
        releases = [self.release(slug, f'Hostile {i}', True, [f'Hostile {i} (High)'], '2025-01-01') for i, slug in enumerate(hostile)]
        catalogue = {f'hostile-{i}': {'listed_as': {'aa': f'Hostile {i}'}} for i in range(len(hostile))}
        self.assertEqual(self.plan(releases, catalogue), ({}, {}))
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'aa-models.html').write_text(rsc_page(releases), encoding='utf-8')
            self.assertEqual(sources.aa_releases(Path(tmp)), [])

    def test_releases_of_a_base_name_whose_newest_cannot_be_told_are_ambiguous_not_chosen(self):
        release = self.release
        releases = [release('x-new', 'Model X', True, [], '2026-09-02'), release('x-old', 'Model X', True, [], None),
                    release('y-1', 'Model Y', True, [], '2026-09-02'), release('y-2', 'Model Y', True, [], '2026-09-02'),
                    release('z-only', 'Model Z', True, [], None),           # one release: nothing to choose
                    release('z-dated', 'Model Z (0902)', True, [], '2026-09-02')]     # a dated name: another base name
        plan, ambiguous = self.plan(releases, {'model-x': {}, 'model-y': {}, 'model-z': {}})
        self.assertEqual(plan, {'model-z': ('z-only', 'Model Z')})
        self.assertEqual(set(ambiguous), {'model-x', 'model-y'})
        self.assertIn('x-old: None', ambiguous['model-x'])
        self.assertIn('x-new', ambiguous['model-x'])

    def test_an_ambiguous_release_is_a_surfaced_missing_source_and_its_model_gets_no_reading(self):
        data, _ = self.fixture(aa_deprecated=('gpt-6-astra',))
        payload = json.loads(json.loads(sources.AA_RSC_PUSH.findall((data / 'aa-models.html').read_text(encoding='utf-8'))[0]))
        twin = dict(next(r for r in payload if r.get('slug') == 'gpt-6-astra'), slug='gpt-6-astra-twin')
        (data / 'aa-models.html').write_text(rsc_page([*payload, twin]), encoding='utf-8')
        built = self.build(data)
        self.assertIn('the newest cannot be told', built['missing_sources']['aa.release/gpt-6-astra']['reason'])
        readings = [e for fields in built['models']['gpt-6-astra'].values() for e in fields if e['source'].startswith(('aa.', 'tier.aa'))]
        self.assertEqual(readings, [])

    def test_the_fetch_asks_for_the_releases_the_plan_chose(self):
        with tempfile.TemporaryDirectory() as tmp:
            target, refs = Path(tmp) / 'data', Path(tmp) / 'refs'
            target.mkdir()
            refs.mkdir()
            (refs / 'accounts.json').write_text(json.dumps({'models': {'model-b': {}}, **ACCOUNT_READINGS}), encoding='utf-8')
            (target / 'aa-models.html').write_text(rsc_page([self.release('model-b', 'Model B', True, [], '2026-09-01')]),
                                                   encoding='utf-8')
            with patch.object(sources, 'fetch') as fetch, patch.object(sources, 'fetch_releases') as fetch_releases:
                refresh.fetch_sources(target, refs)
        fetch.assert_called_once_with(target)
        fetch_releases.assert_called_once_with(target, {'aa-release-model-b.html': sources.AA_RELEASE_URL.format('model-b')})

    def test_the_fetch_records_each_page_and_a_failure_is_missing_never_an_older_copy(self):
        good, bad, old = (sources.AA_RELEASE_FILE.format(s) for s in ('good', 'bad', 'old'))
        urls = {good: sources.AA_RELEASE_URL.format('good'), bad: sources.AA_RELEASE_URL.format('bad')}
        body, requested = b'<html>release page</html>', []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return body

        def urlopen(request, timeout):
            requested.append((request.full_url, dict(request.header_items())))
            if request.full_url == urls[bad]:
                raise OSError('offline')
            return Response()

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            kept = {'url': 'https://example.org/x', 'fetched_at': 'earlier', 'status': 'ok', 'bytes': 1, 'sha256': 'x'}
            (target / bad).write_bytes(b'older copy of bad')
            (target / old).write_bytes(b'no longer wanted')
            (target / 'aa-release-notes.html').write_bytes(b'not a page the manifest records')
            (target / sources.MANIFEST).write_text(json.dumps({'files': {
                'x.json': kept, bad: dict(kept, url=urls[bad]), old: dict(kept)}}), encoding='utf-8')
            with patch.object(sources.urllib.request, 'urlopen', urlopen), patch('sys.stdout', io.StringIO()):
                sources.fetch_releases(target, urls)
            files = json.loads((target / sources.MANIFEST).read_text(encoding='utf-8'))['files']
            self.assertEqual((target / good).read_bytes(), body)
            self.assertEqual({k: files[good][k] for k in ('url', 'status', 'bytes', 'sha256')},
                             {'url': urls[good], 'status': 'ok', 'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()})
            self.assertTrue(datetime.fromisoformat(files[good]['fetched_at']).tzinfo)
            self.assertFalse((target / bad).exists())
            self.assertEqual({k: files[bad][k] for k in ('url', 'status', 'error')},
                             {'url': urls[bad], 'status': 'missing', 'error': 'OSError: offline'})
            self.assertFalse((target / old).exists())
            self.assertNotIn(old, files)
            self.assertEqual((target / 'aa-release-notes.html').read_bytes(), b'not a page the manifest records')
            self.assertEqual(files['x.json'], kept)
            self.assertEqual(sorted(files), sorted(['x.json', good, bad]))
        self.assertEqual([u for u, _ in requested], list(urls.values()))        # one plain request per page
        for _, headers in requested:
            self.assertEqual(set(headers), {'User-agent'})
            self.assertNotIn(str(Path.home().name), headers['User-agent'])

    def test_a_release_page_is_read_only_when_the_plan_chooses_it_and_only_for_the_base_names_it_chose_it_for(self):
        data, _ = self.fixture(aa_deprecated=('gpt-6-astra',))
        baseline = self.build(data)
        # a page of a release the leaderboard carries (the plan does not ask for it), left in the cache and dated long ago
        wanted, stale = (sources.AA_RELEASE_FILE.format(release_slug(m)) for m in ('gpt-6-astra', 'claude-opus-5-5'))
        shutil.copyfile(data / wanted, data / stale)
        self.rewrite_page(data, 'claude-opus-5-5', lambda r: r.update(intelligenceIndex=1.0))
        manifest_path = data / sources.MANIFEST
        doc = json.loads(manifest_path.read_text(encoding='utf-8'))
        doc['files'][stale] = dict(doc['files'][wanted], url=self.page_url('claude-opus-5-5'), fetched_at='2020-01-01T00:00:00+00:00')
        for row in (doc['files'][stale], {'url': self.page_url('claude-opus-5-5'), 'status': 'missing', 'error': 'offline'}):
            with self.subTest(row=row['status']):
                doc['files'][stale] = row
                manifest_path.write_text(json.dumps(doc), encoding='utf-8')
                built = self.build(data)
                self.assertEqual(built['sources']['aa.index']['as_of'], baseline['sources']['aa.index']['as_of'])
                self.assertEqual(set(built['sources']['aa.index']['release_pages']), {wanted})
                self.assertEqual(built['missing_sources'], baseline['missing_sources'])
                self.assertEqual(built['models'], baseline['models'])
        # a chosen page's record of a base name the plan did not choose it for (here one the leaderboard carries) is not read
        board = sources.aa_records(data)
        self.rewrite_page(data, 'gpt-6-astra', lambda r: r.update(name=board[0]['name'], intelligenceIndex=1.0))
        records = measurements.aa_records_read(data, self.catalogue(data), READINGS)[0]
        self.assertEqual([r for r in records if r['name'] == board[0]['name']], [board[0]])

    def test_a_page_with_no_figure_for_its_model_is_reported_not_used_and_one_with_some_is_not(self):
        """Rule (measurements.Builder.release_gaps, round 5): a page that lists the release without any figure is a source
        not used (its scores come from the declared estimation methods), not a missing one; a page with some figures is
        read for what it has."""
        data, _ = self.fixture(aa_deprecated=('gpt-6-astra', 'gpt-6.1-sol'))

        def emptied(record):
            for field in ('intelligenceIndex', 'intelligenceIndexTimePerTask', 'price1mInputTokens', 'price1mOutputTokens',
                          'cacheHitPrice', 'terminalBench40', 'frontierCode20', 'mmmuPro', 'intelligenceIndexCostPerTask', 'headline'):
                record[field] = None

        def no_index(record):       # a release AA has no index of: the rest of its page is still served
            record['intelligenceIndex'] = None
            record['intelligenceIndexCostPerTask'] = record['intelligenceIndexTimePerTask'] = None

        self.rewrite_page(data, 'gpt-6-astra', emptied)
        self.rewrite_page(data, 'gpt-6.1-sol', no_index)
        built = self.build(data)
        empty, partial = (sources.AA_RELEASE_FILE.format(release_slug(m)) for m in ('gpt-6-astra', 'gpt-6.1-sol'))
        self.assertEqual(built['missing_sources'][empty]['reason'],
                         'not used: the page lists the release of gpt-6-astra without a figure; its scores come from the '
                         'declared estimation methods')
        self.assertNotIn(partial, built['missing_sources'])
        read = lambda model: {e['source'] for fields in built['models'][model].values() for e in fields
                              if e['source'].startswith(('aa.', 'tier.aa'))}
        self.assertEqual(read('gpt-6-astra'), set())
        self.assertIn('aa.price', read('gpt-6.1-sol'))
        self.assertNotIn('aa.index', read('gpt-6.1-sol'))         # a null index is no reading of the index, and no error

    def test_a_record_of_a_shape_the_reader_does_not_know_makes_its_page_unreadable_naming_the_field(self):
        cases = {'intelligenceIndexCostPerTask.cost is float': lambda r: r.update(intelligenceIndexCostPerTask={'cost': 3.2}),
                 'intelligenceIndexCostPerTask.cost is list': lambda r: r.update(intelligenceIndexCostPerTask={'cost': [1]}),
                 'intelligenceIndexCostPerTask is list': lambda r: r.update(intelligenceIndexCostPerTask=[1]),
                 'intelligenceIndexCostPerTask is float': lambda r: r.update(intelligenceIndexCostPerTask=3.2),
                 'intelligenceIndexCostPerTask.cost.total is str': lambda r: r.update(
                     intelligenceIndexCostPerTask={'cost': {'total': '3.2'}}),
                 'headline is str': lambda r: r.update(headline='fast'),
                 'headline.outputSpeed is str': lambda r: r.update(headline={'outputSpeed': '60', 'timeToFirstAnswerToken': 1.0}),
                 'price1mInputTokens is str': lambda r: r.update(price1mInputTokens='2'),
                 'mmmuPro is bool': lambda r: r.update(mmmuPro=True)}
        page = sources.AA_RELEASE_FILE.format(release_slug('gpt-6-astra'))
        for field, change in cases.items():
            with self.subTest(field=field):
                data, _ = self.fixture(aa_deprecated=('gpt-6-astra',))
                self.rewrite_page(data, 'gpt-6-astra', change)
                records, errors = sources.aa_release_records(data, [page])
                self.assertEqual(records, [])
                self.assertIn(page, errors[page])
                self.assertIn(field, errors[page])
                built = self.build(data)
                self.assertEqual(built['missing_sources'][page]['reason'], f'unreadable: {errors[page]}')
                self.assertEqual([e for fields in built['models']['gpt-6-astra'].values() for e in fields
                                  if e['source'].startswith(('aa.', 'tier.aa'))], [])

    def test_a_value_a_page_leaves_unmeasured_is_no_figure_and_not_an_error(self):
        data, _ = self.fixture(aa_deprecated=('gpt-6-astra',))
        page = sources.AA_RELEASE_FILE.format(release_slug('gpt-6-astra'))
        self.rewrite_page(data, 'gpt-6-astra', lambda r: r.update(headline=None, intelligenceIndexCostPerTask=None, mmmuPro=None))
        records, errors = sources.aa_release_records(data, [page])
        self.assertEqual(errors, {})
        self.assertTrue(records)
        for record in records:
            self.assertIsNone(record['outputSpeed'])
            self.assertIsNone(record['intelligenceIndexCostPerTask'])
            self.assertNotIn('mmmu-pro', record['scores'])

    def test_the_cost_per_task_of_a_page_record_is_the_total_of_its_cost(self):
        record = {'name': 'Model (High)', 'intelligenceIndex': 50.0,
                  'intelligenceIndexCostPerTask': {'cost': {'total': 1.25, 'input': 9.0}, 'evaluations': []}}
        self.assertEqual(sources.aa_release_record(record, 'p.html')['intelligenceIndexCostPerTask'], 1.25)

    def test_a_merged_model_row_that_cites_a_release_page_gives_each_source_row_the_page(self):
        data, _ = self.fixture(aa_deprecated=('gpt-6-astra',))
        shipped = self.build(self.fixture()[0])
        fresh = self.build(data)
        page, url = sources.AA_RELEASE_FILE.format(release_slug('gpt-6-astra')), self.page_url('gpt-6-astra')
        merged, _, _ = refresh.merge_model_rows(copy.deepcopy(shipped), fresh, ['gpt-6-astra'])
        cited = {e['source'] for fields in merged['models']['gpt-6-astra'].values() for e in fields if e.get('url') == url}
        self.assertTrue(cited >= {'aa.index', 'aa.price', 'aa.cost_time', 'derived.community_mix'}, cited)
        for src in cited:
            row = merged['sources'][src]
            if src == 'derived.community_mix':
                self.assertIn(url, [c['url'] for c in row['citations']])
            else:
                self.assertEqual(row['release_pages'][page], fresh['sources'][src]['release_pages'][page])
        old = (self.now - timedelta(days=30)).isoformat(timespec='seconds')       # a page older than the shipped source
        for row in fresh['sources'].values():
            if page in row.get('release_pages', {}):
                row['release_pages'][page]['fetched_at'] = old
        merged, _, _ = refresh.merge_model_rows(copy.deepcopy(shipped), fresh, ['gpt-6-astra'])
        self.assertEqual(merged['sources']['aa.index']['as_of'], old[:10])
        merged, _, _ = refresh.merge_model_rows(copy.deepcopy(shipped), self.build(data), ['gpt-6-astra'])
        urls = [c['url'] for c in merged['sources']['derived.community_mix']['citations']]
        self.assertEqual(len(urls), len(set(urls)))         # several entries cite the page: its citation is added once

    def test_a_release_page_url_is_the_release_slug_under_the_models_path(self):
        self.assertEqual(sources.AA_RELEASE_URL.format('gpt-6-astra'), 'https://artificialanalysis.ai/models/gpt-6-astra')

    def test_a_base_name_has_the_records_of_one_release_only(self):
        """Rule: a catalogue row reads the records of the release the plan chose for it. Two rows of one base name read the
        same release; a release page's records of a base name the plan chose another release for are not read."""
        data, _ = self.fixture(aa_deprecated=('gpt-6-astra',))
        catalogue = {**self.catalogue(data), 'gpt-6-astra-free': {}}
        plan = measurements.aa_records_read(data, catalogue, READINGS)[1]
        self.assertEqual(plan['gpt-6-astra'], plan['gpt-6-astra-free'])
        built = self.build_with(data, catalogue)
        read = lambda model: {(e['source'], e['listed_as'], e['url']) for fields in built['models'][model].values() for e in fields
                              if e['source'].startswith('aa.') and 'url' in e}
        self.assertTrue(read('gpt-6-astra'))
        self.assertEqual(read('gpt-6-astra'), read('gpt-6-astra-free'))
        self.assertNotIn('aa-release-gpt-6-astra.html', built['missing_sources'])
        # a release listing two base names, the second of which a newer release lists too
        releases = [self.release('old', 'Old', True, ['B (High)', 'B2 (High)'], '2026-01-01'),
                    self.release('new', 'New', True, ['B2 (High)'], '2026-02-01')]
        with tempfile.TemporaryDirectory() as tmp:
            target, files = Path(tmp), {}
            (target / 'aa-models.html').write_text(rsc_page(releases), encoding='utf-8')
            for slug in ('old', 'new'):
                records = [{'name': f'{base} (High)', 'intelligenceIndex': value, 'release': {'slug': slug, 'name': slug}}
                           for base, value in (('B', 1.0), ('B2', 2.0 if slug == 'old' else 3.0))
                           if (slug, base) != ('new', 'B')]
                (target / f'aa-release-{slug}.html').write_text(rsc_page({'models': records}), encoding='utf-8')
                files[f'aa-release-{slug}.html'] = {'url': 'u', 'fetched_at': '2026-11-18T00:00:00+00:00', 'status': 'ok'}
            (target / sources.MANIFEST).write_text(json.dumps({'files': files}), encoding='utf-8')
            records, plan, _, _ = measurements.aa_records_read(target, {'b': {}, 'b2': {}}, READINGS)
        self.assertEqual(plan, {'b': ('old', 'B'), 'b2': ('new', 'B2')})
        self.assertEqual({(r['name'], r['page']) for r in records if 'page' in r},
                         {('B (High)', 'aa-release-old.html'), ('B2 (High)', 'aa-release-new.html')})

    def test_a_planned_page_whose_file_is_gone_is_a_surfaced_missing_source(self):
        data, _ = self.fixture(aa_deprecated=('gpt-6-astra',))
        page = sources.AA_RELEASE_FILE.format(release_slug('gpt-6-astra'))
        (data / page).unlink()          # the manifest row still says ok
        built = self.build(data)
        self.assertEqual(built['missing_sources'][page]['reason'], 'not fetched; run `agentcompass data refresh`')
        self.assertEqual(built['missing_sources'][page]['url'], self.page_url('gpt-6-astra'))
        self.assertNotIn('release_pages', built['sources']['aa.index'])

    def test_a_full_fetch_removes_the_release_pages_it_no_longer_records(self):
        """Rule: after `fetch` and `fetch_releases`, the release pages on disk are the ones the manifest records and the plan
        chose; a file the manifest never recorded as a release page is not touched."""
        old, new = (sources.AA_RELEASE_FILE.format(s) for s in ('old', 'new'))
        listing = rsc_page([self.release('new', 'New', True, [], '2026-09-01')])

        def download(jobs, files):
            for url, path in jobs:
                path.write_text(listing if path.name == 'aa-models.html' else 'page', encoding='utf-8')
                files[path.name] = {'url': url, 'fetched_at': '2026-11-18T00:00:00+00:00', 'status': 'ok'}

        with tempfile.TemporaryDirectory() as tmp:
            target, refs = Path(tmp) / 'data', Path(tmp) / 'refs'
            target.mkdir()
            refs.mkdir()
            (refs / 'accounts.json').write_text(json.dumps({'models': {'new': {}}, **ACCOUNT_READINGS}), encoding='utf-8')
            (target / old).write_text('an earlier release page', encoding='utf-8')
            (target / 'aa-release-notes.html').write_text('not a page the manifest records', encoding='utf-8')
            row = {'url': 'u', 'fetched_at': '2026-11-01T00:00:00+00:00', 'status': 'ok'}
            (target / sources.MANIFEST).write_text(json.dumps({'files': {old: row, 'imported.json': dict(row, kind='transcribed')}}),
                                                   encoding='utf-8')
            with patch.object(sources, '_download', download), patch('sys.stdout', io.StringIO()):
                refresh.fetch_sources(target, refs)
            self.assertEqual(sorted(p.name for p in target.glob('aa-release-*.html')), sorted([new, 'aa-release-notes.html']))
            files = json.loads((target / sources.MANIFEST).read_text(encoding='utf-8'))['files']
            self.assertNotIn(old, files)
            self.assertIn(new, files)
            self.assertIn('imported.json', files)

    def test_a_record_with_any_one_figure_is_a_reading(self):
        for field, value in (('intelligenceIndex', 1.0), ('intelligenceIndexCostPerTask', 0.1), ('intelligenceIndexTimePerTask', 9.0),
                             ('outputSpeed', 60.0), ('timeToFirstAnswerToken', 2.0), ('price1mInputTokens', 1.0),
                             ('price1mOutputTokens', 5.0), ('cacheHitPrice', 0.1)):
            with self.subTest(field=field):
                self.assertTrue(sources.aa_has_figure({field: value, 'scores': {}}))
        self.assertTrue(sources.aa_has_figure({'scores': {'mmmu-pro': 0.5}}))
        self.assertFalse(sources.aa_has_figure({'name': 'x', 'intelligenceIndex': None, 'outputSpeed': None, 'scores': {}}))

    def test_a_page_with_only_a_speed_or_only_a_score_is_not_empty(self):
        for case, change in (('speed', lambda r: r.update(**{f: None for f in ('intelligenceIndex', 'intelligenceIndexTimePerTask',
                                                                               'price1mInputTokens', 'price1mOutputTokens',
                                                                               'cacheHitPrice', 'terminalBench40', 'frontierCode20',
                                                                               'mmmuPro', 'intelligenceIndexCostPerTask')},
                                                           headline={'outputSpeed': 60.0, 'timeToFirstAnswerToken': None})),
                             ('score', lambda r: r.update(**{f: None for f in ('intelligenceIndex', 'intelligenceIndexTimePerTask',
                                                                               'price1mInputTokens', 'price1mOutputTokens',
                                                                               'cacheHitPrice', 'terminalBench40', 'frontierCode20',
                                                                               'intelligenceIndexCostPerTask', 'headline')}))):
            with self.subTest(case=case):
                data, _ = self.fixture(aa_deprecated=('gpt-6-astra',))
                self.rewrite_page(data, 'gpt-6-astra', change)
                self.assertEqual(self.build(data)['missing_sources'], {})

    def test_a_page_the_manifest_does_not_list_as_fetched_is_not_read(self):
        data, _ = self.fixture(aa_deprecated=('gpt-6-astra',))
        page = sources.AA_RELEASE_FILE.format(release_slug('gpt-6-astra'))
        self.assertTrue(sources.aa_release_records(data, [page])[0])
        manifest_path = data / sources.MANIFEST
        doc = json.loads(manifest_path.read_text(encoding='utf-8'))
        doc['files'][page]['status'] = 'missing'        # the file is still on disk
        manifest_path.write_text(json.dumps(doc), encoding='utf-8')
        self.assertEqual(sources.aa_release_records(data, [page]), ([], {}))

    def test_a_page_with_one_unreadable_record_returns_none_of_its_records(self):
        data, _ = self.fixture(aa_deprecated=('gpt-6-astra',))
        page = sources.AA_RELEASE_FILE.format(release_slug('gpt-6-astra'))
        seen = []
        self.rewrite_page(data, 'gpt-6-astra', lambda r: (seen.append(r), r.update(price1mInputTokens='2') if len(seen) == 2 else None))
        self.assertGreater(len(seen), 2)
        records, errors = sources.aa_release_records(data, [page])
        self.assertEqual(records, [])
        self.assertIn('price1mInputTokens', errors[page])

    def test_a_deprecated_model_listed_only_on_the_index_board_gets_its_tier_from_its_page_and_cites_it(self):
        data, refs = self.fixture(aa_only=[('grok-4.9', 'Grok 4.9', 'xAI')], aa_deprecated=('grok-4.9',))
        catalogue = self.catalogue(data)
        catalogue['grok-4.9'] = dict(copy.deepcopy(catalogue['grok-4.7']), listed_as={'aa': 'Grok 4.9'})
        built = self.build_with(data, catalogue)
        tier = built['models']['grok-4.9']['tier']
        self.assertEqual([e['source'] for e in tier], ['tier.aa_index'])
        self.assertEqual(tier[0]['url'], self.page_url('grok-4.9'))
        self.assertIn(sources.AA_RELEASE_FILE.format('grok-4-9'), built['sources']['tier.aa_index']['release_pages'])

    def test_a_row_the_matcher_resolves_to_another_base_name_than_the_plan_chose_is_a_surfaced_missing_source(self):
        """Rule: a planned row that resolves to another base name than the plan chose it for is reported, not silent. The
        release "Foo Free" has a record named "Foo Free Plus (High)": the matcher (which reads `-free` as the same model)
        resolves the row `foo-free` to `Foo`."""
        data, _ = self.fixture()
        listing = (data / 'aa-models.html').read_text(encoding='utf-8')
        payload = json.loads(json.loads(sources.AA_RSC_PUSH.findall(listing)[0]))
        shown = [self.release('rx', 'Foo Free', True, ['Foo Free'], '2026-01-01'), self.release('ry', 'Foo', True, [], '2026-01-01')]
        (data / 'aa-models.html').write_text(rsc_page([*payload, *shown]), encoding='utf-8')
        manifest_path = data / sources.MANIFEST
        doc = json.loads(manifest_path.read_text(encoding='utf-8'))
        for slug, name, value in (('rx', 'Foo Free Plus (High)', 10.0), ('ry', 'Foo (High)', 40.0)):
            record = {'name': name, 'intelligenceIndex': value, 'release': {'slug': slug, 'name': slug}}
            page = sources.AA_RELEASE_FILE.format(slug)
            (data / page).write_text(rsc_page({'models': [record]}), encoding='utf-8')
            doc['files'][page] = {'url': sources.AA_RELEASE_URL.format(slug), 'fetched_at': self.now.isoformat(), 'status': 'ok'}
        manifest_path.write_text(json.dumps(doc), encoding='utf-8')
        built = self.build_with(data, {**self.catalogue(data), 'foo-free': {}, 'foo': {}})
        row = built['missing_sources']['aa-release-rx.html']
        self.assertEqual(row['url'], sources.AA_RELEASE_URL.format('rx'))
        self.assertIn("foo-free resolves to 'Foo', not 'Foo Free'", row['reason'])
        self.assertNotIn('aa-release-ry.html', built['missing_sources'])
        read = lambda model: [e for fields in built['models'][model].values() for e in fields if e['source'] == 'aa.index']
        self.assertEqual([e['listed_as'] for e in read('foo')], ['Foo (High)'])

    def test_a_release_chosen_for_two_base_names_keeps_the_records_of_both(self):
        releases = [self.release('old', 'Old', True, ['B (High)', 'B2 (High)'], '2026-01-01')]
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / 'aa-models.html').write_text(rsc_page(releases), encoding='utf-8')
            records = [{'name': f'{base} (High)', 'intelligenceIndex': 1.0, 'release': {'slug': 'old', 'name': 'old'}}
                       for base in ('B', 'B2', 'Other')]
            (target / 'aa-release-old.html').write_text(rsc_page({'models': records}), encoding='utf-8')
            (target / sources.MANIFEST).write_text(json.dumps({'files': {'aa-release-old.html': {
                'url': 'u', 'fetched_at': '2026-11-18T00:00:00+00:00', 'status': 'ok'}}}), encoding='utf-8')
            read, plan, _, _ = measurements.aa_records_read(target, {'b': {}, 'b2': {}}, READINGS)
        self.assertEqual(plan, {'b': ('old', 'B'), 'b2': ('old', 'B2')})
        self.assertEqual(sorted(r['name'] for r in read if 'page' in r), ['B (High)', 'B2 (High)'])        # not 'Other'

    def test_a_fetch_removes_only_release_page_files_among_the_rows_it_drops_and_none_for_a_partial_fetch(self):
        page, other = sources.AA_RELEASE_FILE.format('old'), 'other.json'
        row = {'url': 'u', 'fetched_at': '2026-11-01T00:00:00+00:00', 'status': 'ok'}
        for only in (None, ['aa-models.html']):
            with self.subTest(only=only), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp)
                for name in (page, other):
                    (target / name).write_text('earlier download', encoding='utf-8')
                (target / sources.MANIFEST).write_text(json.dumps({'files': {page: row, other: row}}), encoding='utf-8')
                with patch.object(sources, '_download', lambda jobs, files: None):
                    sources.fetch(target, only=only)
                files = json.loads((target / sources.MANIFEST).read_text(encoding='utf-8'))['files']
                self.assertTrue((target / other).exists())      # a dropped row of another file leaves its file
                self.assertEqual((target / page).exists(), only is not None)
                self.assertEqual(sorted(files), sorted([page, other]) if only else [])

    def test_every_sub_benchmark_the_pipeline_reads_has_a_field_on_the_release_page(self):
        self.assertLessEqual(set(measurements.AA_SCORE.values()), set(sources.AA_RELEASE_SCORE_FIELD))


if __name__ == '__main__':
    unittest.main()
