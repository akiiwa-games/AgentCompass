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
import public_data  # noqa: E402
import refresh  # noqa: E402
import sources  # noqa: E402


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


def build_calibration_fixture(root: Path, as_of_dt: datetime | None = None, arena_partial=(), aa_only=()) -> tuple[Path, Path]:
    """Create a self-contained `(data_dir, references_dir)` fixture with fresh cited sources and zero network calls.
    `arena_partial` (slug, display, lab) rows are listed on the arena.ai text boards and Artificial Analysis only, so they
    stay out of the panel; `aa_only` rows are listed on Artificial Analysis only."""
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
        shutil.copyfile(public_data.BUNDLED / name, refs_dir / name)

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

    # 2. Artificial Analysis HTML table + RSC stream with multi-effort curves.
    tr_rows = []
    rsc_records = []
    effort_offsets = (('low', -6.0, 0.55, 1.4), ('medium', -2.5, 0.78, 1.9), ('high', 0.0, 1.0, 2.6), ('xhigh', 1.8, 1.35, 3.4))
    for idx, (slug, display, _) in enumerate((*PANEL_MODELS, *arena_partial, *aa_only)):
        base_idx = 78.0 - idx * 3.1
        for eff, idx_shift, time_mult, ttfa_base in effort_offsets:
            listed = f'{display} ({eff.capitalize()})'
            ii_val = round(base_idx + idx_shift, 2)
            cost_task = round((1.8 - idx * 0.08) * time_mult, 2)
            tr_rows.append(
                f'<tr><td>{listed}</td><td>128k</td><td>80</td><td>{ii_val}</td><td>${cost_task:.2f}</td></tr>'
            )
            tb4 = round(max(0.12, min(0.95, 0.76 - idx * 0.042 + idx_shift * 0.008)), 4)
            fc2 = round(max(0.10, min(0.94, 0.71 - idx * 0.039 + idx_shift * 0.009 - (idx % 2) * 0.011)), 4)
            mmmu = round(max(0.15, min(0.96, 0.82 - idx * 0.036 + idx_shift * 0.006 + (idx % 3) * 0.007)), 4)
            rsc_records.append({
                'slug': f'{slug}-{eff}',
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
            })
    rsc_payload = json.dumps(rsc_records, separators=(',', ':'))
    aa_html = (
        '<html><body><table>' + ''.join(tr_rows) + '</table>'
        f'<script>self.__next_f.push([1,{json.dumps(rsc_payload)}])</script></body></html>'
    )
    _write_file(data_dir, 'aa-models.html', aa_html, sources.PAGES['aa-models.html'], fetched_at, manifest_files)

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
            '<p>Claude Max 20x ($200/mo) : ≈ $1,549 of API use a week · 1M tokens · ≈ 5.7 full 5-hour windows</p>'
            '<p>Fable 5.1 can use only half the weekly limit.</p>'
            f'<p>value every plan at one fixed traffic split: Claude Code traffic on Wokey in the week to {iso_date}, '
            'about 92% of tokens cache reads.</p>'
            '<p>Claude Max 20x $200/mo $1,549 ($1,216–$1,697), ≈ $6,711/mo 34× 180M 9.9M $272</p>'
            '<p>Claude Max 20x: 92% cache reads, 1.98B tokens a week</p></body></html>'
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
        public_data.unpin()
        self.addCleanup(public_data.unpin)
        self.tmp = Path(tempfile.mkdtemp(prefix='refresh-entry-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.now = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
        self.data_dir, self.refs_dir = build_calibration_fixture(self.tmp, self.now)

    def _run_refresh(self, *extra_args):
        out, err = io.StringIO(), io.StringIO()
        with patch('sys.stdout', out), patch('sys.stderr', err):
            code = refresh.main(['--data', str(self.data_dir), '--references', str(self.refs_dir), *extra_args])
        return code, out.getvalue(), err.getvalue()

    def test_no_fetch_rebuilds_every_file_validates_and_keeps_unchanged_manifest_byte_identical(self):
        generated = (
            'public-measurements.json',
            'five-axis-scores.json',
            'model-capabilities.json',
            'model-capabilities.md',
            'public-data.json',
        )
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

        manifest = refresh.validate(self.refs_dir)
        self.assertEqual(public_data.check_set(self.refs_dir, manifest), ('ok', ''))
        self.assertEqual(capability_catalog.check_files(references=self.refs_dir), [])

        manifest_bytes = (self.refs_dir / 'public-data.json').read_bytes()

        # 1. Re-running `--no-fetch` (including at a later timestamp) keeps an unchanged manifest byte-identical.
        code2, stdout2, stderr2 = self._run_refresh('--no-fetch')
        self.assertEqual(code2, 0, stderr2 + stdout2)
        self.assertEqual((self.refs_dir / 'public-data.json').read_bytes(), manifest_bytes)

        refresh.rebuild(self.data_dir, now=self.now + timedelta(hours=5), references=self.refs_dir)
        self.assertEqual((self.refs_dir / 'public-data.json').read_bytes(), manifest_bytes)

        # 2. Deleting generated capability views and re-running `--no-fetch` rebuilds them and keeps manifest byte-identical.
        (self.refs_dir / 'model-capabilities.json').unlink()
        (self.refs_dir / 'model-capabilities.md').unlink()
        code3, _, stderr3 = self._run_refresh('--no-fetch')
        self.assertEqual(code3, 0, stderr3)
        self.assertTrue((self.refs_dir / 'model-capabilities.json').is_file())
        self.assertTrue((self.refs_dir / 'model-capabilities.md').is_file())
        self.assertEqual((self.refs_dir / 'public-data.json').read_bytes(), manifest_bytes)

        # 3. Clean rebuild from scratch at fixed `now` also produces the exact same manifest bytes.
        t_fixed = self.now - timedelta(minutes=10)
        for name in ('public-measurements.json', 'five-axis-scores.json', 'model-capabilities.json', 'model-capabilities.md'):
            (self.refs_dir / name).unlink()
        refresh.rebuild(self.data_dir, now=t_fixed, references=self.refs_dir)
        fixed_manifest_bytes = (self.refs_dir / 'public-data.json').read_bytes()
        for name in ('public-measurements.json', 'five-axis-scores.json', 'model-capabilities.json', 'model-capabilities.md'):
            (self.refs_dir / name).unlink()
        refresh.rebuild(self.data_dir, now=t_fixed, references=self.refs_dir)
        self.assertEqual((self.refs_dir / 'public-data.json').read_bytes(), fixed_manifest_bytes)

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
        for category in ('divergence', 'coding', 'aesthetics', 'tool_operation'):
            self.assertIn(f'  {category}', stderr)
        self.assertEqual({p.name: p.read_bytes() for p in self.refs_dir.iterdir() if p.is_file()}, before)

        code, stdout, stderr = self._run_refresh('--no-fetch', '--accept-panel-change')
        self.assertEqual(code, 0, stderr + stdout)
        record = json.loads(scores.read_text(encoding='utf-8'))['anchor']['panel_change']
        self.assertEqual((record['added'], record['removed']), ([dropped], ['gone-model']))
        self.assertRegex(record['date'], r'^\d{4}-\d{2}-\d{2}$')
        self.assertEqual(set(record['shifts']), {'divergence', 'coding', 'aesthetics', 'tool_operation'})

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

    def test_validation_checks_user_scores_schema_and_public_data_integrity(self):
        user_scores_path = self.refs_dir / 'user-scores.json'
        good_user_scores = user_scores_path.read_text(encoding='utf-8')

        bad_docs = [
            ('kind_not_maintainer', {
                'schema_version': 1,
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
                'schema_version': 1,
                'scores': {'dot': {'aesthetics': {'level': 2.5, 'kind': 'maintainer'}}},
            }),
            ('unexpected_record_key', {
                'schema_version': 1,
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
        orig_write_manifest = public_data.write_manifest

        def fake_fetch(target):
            events.append(('fetch', Path(target)))

        def tracked_views(*args, **kwargs):
            events.append(('capability_catalog',))
            return orig_write_views(*args, **kwargs)

        def tracked_manifest(*args, **kwargs):
            events.append(('manifest',))
            return orig_write_manifest(*args, **kwargs)

        with patch.object(sources, 'fetch', side_effect=fake_fetch), \
                patch.object(capability_catalog, 'write_default_files', side_effect=tracked_views), \
                patch.object(public_data, 'write_manifest', side_effect=tracked_manifest):
            code, _, stderr = self._run_refresh()
        self.assertEqual(code, 0, stderr)
        self.assertEqual([e[0] for e in events], ['fetch', 'capability_catalog', 'manifest'])
        self.assertEqual(events[0][1], self.data_dir)


def _measurements(scale, models, conflicts=(), checks=(), tier_sha='aaaa'):
    """A minimal measurements document: one scaled board `b.code`, one scale-free source `s.price`, the tier source."""
    return {'generated_at': '2026-11-01T00:00:00+00:00', 'panel': {'models': ['anchor-m', 'panel-m']},
            'sources': {'b.code': {'scale': scale}, 's.price': {}, 'tier.arena_overall': {'sha256': tier_sha}},
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
    tier as built; an entry is left out only when its anchor reading moved or it is a z-score whose source has no shipped
    scale; a panel model, the anchor or a model no board lists is refused."""
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
            'panel-m': {'coding': [_board_entry(41.0, scale)]},
            'old-m': {'coding': [_board_entry(47.0, scale)]},
            'new-m': {'coding': [_board_entry(62.0, scale, {'low': 55.0, 'high': 62.0})],
                      'price': [{'source': 's.price', 'value': {'input': 1.0}, 'as_of': '2026-11-13'}],
                      'tier': [{'source': 'tier.arena_overall', 'value': 0.3}]}},
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

    def test_on_an_unchanged_scale_and_for_the_tier_the_entry_is_kept_as_built(self):
        fresh = self.fresh(self.SHIPPED_SCALE)
        merged, left_out, notes = refresh.merge_model_rows(self.shipped(), fresh, ['new-m'])
        self.assertEqual(merged['models']['new-m'], fresh['models']['new-m'])
        self.assertEqual((left_out, notes), ([], []))
        _, _, notes = refresh.merge_model_rows(self.shipped(), self.fresh(self.SHIPPED_SCALE, tier_sha='bbbb'), ['new-m'])
        self.assertEqual(len(notes), 1)
        self.assertIn('tier.arena_overall', notes[0])

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
        shutil.copyfile(public_data.BUNDLED / 'accounts.json', root / 'accounts.json')
        anchor = json.loads((root / 'accounts.json').read_text(encoding='utf-8'))['calibration_panel']['anchor']
        (root / 'public-measurements.json').write_text(json.dumps({'panel': {'models': []}}), encoding='utf-8')
        err = io.StringIO()
        with patch.object(sources, 'fetch', side_effect=AssertionError('fetched')), patch('sys.stderr', err):
            code = refresh.main(['--data', str(root / 'data'), '--references', str(root), '--model', anchor])
        self.assertEqual(code, refresh.REFUSED)
        self.assertNotIn(code, refresh.LEVEL.values())
        self.assertIn('full refresh', err.getvalue())

    def test_adding_a_non_panel_model_keeps_the_scale_targets_and_panel_record_and_a_ceiling_moves_only_to_it(self):
        """End to end over synthetic boards and the five-axis-scores/3 file: the shipped panel, sd_panel, panel_change
        and level 1..3 targets stay; a level-3 ceiling (highest measured model, any catalogued model) stays unless the
        added model now sets it."""
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
        refresh.rebuild(fresh_data, now=now, references=refs, models=[added])
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
        strip = lambda targets: {p: v for p, v in walk(targets) if 'ceiling' not in p}
        self.assertEqual(strip(scores_after['anchor']['targets']), strip(scores_before['anchor']['targets']))

        def ceilings(targets):
            out = {}
            for path, value in walk(targets):
                if 'ceiling' in path and path[-1] in ('value', 'model'):
                    out.setdefault(path[:path.index('ceiling')], {})[path[-1]] = value
            return out
        before, after = ceilings(scores_before['anchor']['targets']), ceilings(scores_after['anchor']['targets'])
        self.assertEqual(set(after), set(before))
        for where, cap in after.items():
            if cap != before[where]:
                self.assertEqual(cap['model'], added, where)
                self.assertGreaterEqual(cap['value'], before[where]['value'], where)

    def test_with_an_unchanged_panel_the_model_row_and_scores_equal_a_full_refresh(self):
        """Rule: for a named non-panel model, --model gives the same measurement row, checks and five-axis block as a
        full refresh over the same boards, for a model arena.ai lists and for one listed only on the index board."""
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
                refresh.rebuild(boards, now=now, references=refs, models=[added])
                refresh.rebuild(boards, now=now, references=full_refs)
                read = lambda where, name: json.loads((where / name).read_text(encoding='utf-8'))
                merged_meas, full_meas = read(refs, 'public-measurements.json'), read(full_refs, 'public-measurements.json')
                merged_scores, full_scores = read(refs, 'five-axis-scores.json'), read(full_refs, 'five-axis-scores.json')
                self.assertEqual(merged_meas['panel'], full_meas['panel'])
                self.assertEqual([e['source'] for e in full_meas['models'][added]['tier']],
                                 ['tier.arena_overall' if listing == 'arena' else 'tier.aa_index'])
                self.assertEqual(merged_meas['models'][added], full_meas['models'][added])
                self.assertEqual(merged_meas['checks'], full_meas['checks'])
                self.assertEqual(merged_meas['conflicts'], full_meas['conflicts'])
                self.assertEqual(merged_scores['models'][added], full_scores['models'][added])

    def test_the_blast_radius_counts_only_other_models_values(self):
        before = {'models': {'a': {'x': 1.0, 'y': {'z': 2}}, 'new-m': {'x': 0}}}
        after = {'models': {'a': {'x': 1.5, 'y': {'z': 2}}, 'new-m': {'x': 9}}}
        self.assertIn('1 values of 1 other models moved, by field: x 1', refresh.blast_radius(before, after, ['new-m']))
        self.assertEqual(refresh.blast_radius(before, before, ['new-m']), 'no value of another model moved')


if __name__ == '__main__':
    unittest.main()
