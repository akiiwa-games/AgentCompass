"""Ground-truth recording tests: realistic transcripts, real git checkouts, and quota-economy isolation."""
from __future__ import annotations

import ast
import base64
import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import account_fleet
import allocator
import dispatch_flow as flow
import dispatch_registry as registry
import ledger_snapshot
import provider_inventory
import roll
import route_judge
import run_grok_bot as grok_runner

from router_test_support import isolated_router_state, profile_for, temporary_root

DOT_SCRIPTS = Path(__file__).resolve().parents[2] / 'dot-subagent' / 'scripts'
if str(DOT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(DOT_SCRIPTS))
import run_dot_task as dot_runner  # noqa: E402


ALLOCATION_MODULES = (
    'allocator.py',
    'account_fleet.py',
    'worker_sessions.py',
    'task_intake.py',
    'axis_scores.py',
    'quota_economy.py',
    'quota_pools.py',
    'quota_overrides.py',
    'candidate_eval.py',
    'candidate_ranking.py',
    'ledger_snapshot.py',
)

GROUND_TRUTH_ONLY_KEYS = frozenset({
    'ground_truth_recorded_at',
    'telemetry_harness',
    'worker_session_id',
    'worker_session_reason',
    'active_seconds',
    'idle_threshold_seconds',
    'active_seconds_reason',
    'token_usage',
    'token_usage_reason',
    'initial_active_seconds',
    'followup_active_seconds',
    'first_followup_at',
    'followup_turns',
    'followup_boundary_reason',
    'files_changed',
    'lines_added',
    'lines_deleted',
    'lines_changed',
    'git_diff_basis',
    'git_changes_reason',
    'launch_commit',
    'launch_git_toplevel',
    'launch_commit_reason',
})


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup, policy=False)


def _git_run(cwd: Path, *args: str) -> str:
    kwargs: dict = {
        'cwd': str(cwd),
        'stdin': subprocess.DEVNULL,
        'capture_output': True,
        'text': True,
        'encoding': 'utf-8',
        'check': True,
        'env': {**os.environ, 'GIT_OPTIONAL_LOCKS': '0', 'LC_ALL': 'C'},
    }
    if os.name == 'nt':
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(['git', *args], **kwargs).stdout.strip()


def _init_git_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _git_run(path, 'init')
    _git_run(path, 'config', 'user.email', 'worker@example.invalid')
    _git_run(path, 'config', 'user.name', 'Fixture Worker')
    (path / 'app.py').write_text('def add(a, b):\n    return a + b\n', encoding='utf-8', newline='\n')
    (path / 'README.md').write_text('# Demo\nLine two\n', encoding='utf-8', newline='\n')
    _git_run(path, 'add', 'app.py', 'README.md')
    _git_run(path, 'commit', '-m', 'Initial commit')
    return _git_run(path, 'rev-parse', '--verify', 'HEAD')


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as handle:
        for rec in records:
            handle.write(json.dumps(rec, ensure_ascii=False) + '\n')


class GroundTruthRecordingTests(unittest.TestCase):
    def setUp(self):
        self.root = temporary_root(self.addCleanup)
        for folder, filename, document in (
            (
                '.codex',
                'auth.json',
                {
                    'tokens': {
                        'access_token': 'fixture-codex',
                        'account_id': 'fixture-account',
                        'id_token': 'header.'
                        + base64.urlsafe_b64encode(
                            json.dumps({'https://api.openai.com/auth': {'chatgpt_plan_type': 'pro'}}).encode()
                        )
                        .decode()
                        .rstrip('=')
                        + '.signature',
                    }
                },
            ),
            ('.claude-lab', '.credentials.json', {'claudeAiOauth': {'accessToken': 'fixture-claude', 'subscriptionType': 'pro'}}),
            (
                '.paseo',
                'config.json',
                {
                    'agents': {
                        'providers': {
                            'claude-lab': {
                                'extends': 'claude',
                                'enabled': True,
                                'env': {'CLAUDE_CONFIG_DIR': str(self.root / '.claude-lab')},
                            },
                            'codex': {'enabled': True, 'env': {'CODEX_HOME': str(self.root / '.codex')}},
                            'antigravity-resilient': {'enabled': True},
                            'pi': {'enabled': True},
                        }
                    }
                },
            ),
        ):
            (self.root / folder).mkdir(parents=True, exist_ok=True)
            (self.root / folder / filename).write_text(json.dumps(document), encoding='utf-8', newline='\n')

        self.now = datetime(2026, 11, 12, 9, 0, 0, tzinfo=timezone.utc)
        self.accounts = account_fleet.load_accounts(account_fleet.reference_fleet_path())
        self.accounts['accounts']['claude-lab']['plan'] = 'max20'
        (self.root / 'host.json').write_text(
            json.dumps({'allowed_accounts': ['claude-lab', 'codex', 'antigravity', 'pi', 'grok-bot', 'dot']}),
            encoding='utf-8',
            newline='\n',
        )
        env = patch.dict(
            os.environ,
            {
                'ROUTER_DISPATCH_DB': str(self.root / 'dispatch.sqlite3'),
                'ROUTER_OVERRIDES': str(self.root / 'overrides.json'),
                'ROUTER_CAPACITY': str(self.root / 'capacity.json'),
                'ROUTER_LEDGER': str(self.root / 'ledger.jsonl'),
                'ROUTER_LAUNCH_FAILURES': str(self.root / 'launch-failures.json'),
                'ROUTER_USAGE_CACHE': str(self.root / 'usage.json'),
                'ROUTER_HOST_POLICY': str(self.root / 'host.json'),
                'PYTHONDONTWRITEBYTECODE': '1',
                'ROUTER_DISABLE_LIVE_SCORER': '1',
                'ROUTER_PROVIDER_HOME': str(self.root),
                'PASEO_HOME': str(self.root / '.paseo'),
                'PASEO_AGENT_ID': '',
            },
        )
        env.start()
        self.addCleanup(env.stop)

        discovered = patch.object(
            registry.provider_inventory,
            'current',
            return_value={
                'accounts': {},
                'launchers': {
                    'claude-lab': {'family': 'claude', 'status': 'ready'},
                    'codex': {'family': 'codex', 'status': 'ready'},
                    'antigravity-resilient': {'family': 'antigravity-resilient', 'status': 'ready'},
                    'pi': {'family': 'pi', 'status': 'ready'},
                },
            },
        )
        discovered.start()
        self.addCleanup(discovered.stop)

        acct_patch = patch.object(account_fleet, 'load_accounts', return_value=self.accounts)
        acct_patch.start()
        self.addCleanup(acct_patch.stop)

        self.usage = {
            'read_ok': True,
            'accounts': {
                'claude-lab': {
                    'status': 'fresh',
                    'fetched_at': self.now.isoformat(),
                    'windows': [
                        {
                            'id': wid,
                            'used_pct': 100 - rem,
                            'remaining_pct_before_reset': rem,
                            'observed_remaining_pct': rem,
                            'resets_at': (self.now + timedelta(hours=hours)).isoformat(),
                            'seconds_until_reset': hours * 3600,
                        }
                        for wid, rem, hours in [('five_hour', 90, 3.0), ('seven_day', 85, 120.0)]
                    ],
                },
                'codex': {
                    'status': 'fresh',
                    'fetched_at': self.now.isoformat(),
                    'windows': [
                        {
                            'id': 'usage',
                            'used_pct': 10,
                            'remaining_pct_before_reset': 90,
                            'observed_remaining_pct': 90,
                            'resets_at': (self.now + timedelta(hours=120.0)).isoformat(),
                            'seconds_until_reset': 120 * 3600,
                        }
                    ],
                },
                'antigravity': {
                    'status': 'fresh',
                    'fetched_at': self.now.isoformat(),
                    'windows': [
                        {
                            'id': wid,
                            'used_pct': 10,
                            'remaining_pct_before_reset': 90,
                            'observed_remaining_pct': 90,
                            'resets_at': (self.now + timedelta(hours=hours)).isoformat(),
                            'seconds_until_reset': hours * 3600,
                        }
                        for wid, hours in [('gemini-5h', 4.0), ('gemini-weekly', 120.0), ('3p-5h', 4.0), ('3p-weekly', 120.0)]
                    ],
                },
            },
        }
        usage_patch = patch.object(route_judge, 'usage_snapshot', return_value=self.usage)
        usage_patch.start()
        self.addCleanup(usage_patch.stop)

        self.task = {
            'task_id': 'gt-task-1',
            'parent_agent_id': 'parent-gt',
            'objective': 'Implement ground truth test fixture',
            'work_kind': 'general_implementation',
            'difficulty': 'hard',
            'expected_hours': 0.5,
        }
        self.task['profile'] = profile_for(self.task)

    def _issue_and_claim(self, bundle: dict, *, cwd: str | None = None, task: dict | None = None,
                         resume_agent_id: str | None = None, access: str = 'write', now: datetime | None = None,
                         user_override: bool = False) -> dict:
        t = task or self.task
        at = now or self.now
        choice = bundle.get('bundle_id') or bundle.get('id') or 'custom_bundle'
        is_override = user_override or (choice not in account_fleet.bundles(self.accounts))
        decision = registry.issue(
            {
                'stage': 'record' if is_override else 'plan',
                'choice': 'constraint' if is_override else choice,
                'selected_bundle': bundle,
                'decision': {'action': 'start'},
                **({'constraint': 'user_override', 'basis': 'Synthetic test fixture'} if is_override else {}),
            },
            t,
            at,
        )
        accounts = self.accounts
        if choice not in account_fleet.bundles(self.accounts):
            accounts = json.loads(json.dumps(self.accounts))
            acct_name = bundle['account']
            accounts['accounts'].setdefault(acct_name, {'plan': 'pro', 'billing': 'subscription', 'verified': True, 'windows': {}, 'bundles': {}})
            accounts['accounts'][acct_name]['bundles'][choice] = dict(bundle, id=choice)
            self.usage['accounts'].setdefault(
                acct_name,
                {
                    'status': 'fresh',
                    'fetched_at': at.isoformat(),
                    'windows': [],
                },
            )
        with patch.object(account_fleet, 'load_accounts', return_value=accounts):
            prepared = registry.prepare(
                decision['decision_id'],
                t,
                {'title': 'Fixture dispatch', 'initialPrompt': 'Run fixture brief'},
                self.usage,
                accounts=accounts,
                now=at,
                cwd=cwd,
                resume_agent_id=resume_agent_id,
                access=access,
            )
            claimed = registry.claim(prepared['request'], parent_id=t['parent_agent_id'], call_id=f'call-{prepared["dispatch_id"]}', now=at)
        return claimed

    def _write_paseo_agent(self, agent_id: str, provider: str, session_id: str | None, cwd: str | None = None) -> None:
        doc = {
            'id': agent_id,
            'provider': provider,
            'lastStatus': 'idle',
            'cwd': cwd,
            'persistence': {'provider': provider, **({'sessionId': session_id} if session_id else {})},
        }
        path = self.root / '.paseo' / 'agents' / 'ws-main' / f'{agent_id}.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc), encoding='utf-8', newline='\n')

    def _snapshot(self, row: dict, agent_id: str, status: str, at: datetime, *, reason: str | None = None, cwd: str | None = None) -> dict:
        provider, _, model = row['request']['provider'].partition('/')
        settings = row['request']['settings']
        snap = {
            'id': agent_id,
            'provider': provider,
            'model': model,
            'status': status,
            'updatedAt': at.isoformat(),
            'labels': row['request']['labels'],
        }
        if settings.get('modeId'):
            snap['currentModeId'] = settings['modeId']
        if settings.get('thinkingOptionId'):
            snap['thinkingOptionId'] = settings['thinkingOptionId']
        if reason:
            snap['attentionReason'] = reason
        if cwd is not None:
            snap['cwd'] = cwd
        return snap

    def test_claude_completed_dispatch_records_active_seconds_tokens_git_diff_and_followup_boundary(self):
        repo = self.root / 'worker-repo'
        initial_sha = _init_git_repo(repo)
        bundle = dict(account_fleet.bundles(self.accounts)['lab_sonnet55_high'], bundle_id='lab_sonnet55_high')
        claimed = self._issue_and_claim(bundle, cwd=str(repo))
        self.assertEqual(claimed['launch_commit'], initial_sha)
        self.assertIsNone(claimed['launch_commit_reason'])

        # Worker commits 2 files, leaves 1 unstaged tracked edit, writes 1 tracked binary file, and leaves 1 untracked scratch file.
        (repo / 'app.py').write_text('def add(a, b):\n    return int(a) + int(b)\n\ndef sub(a, b):\n    return a - b\n', encoding='utf-8', newline='\n')
        (repo / 'helper.py').write_text('CONST = 42\n', encoding='utf-8', newline='\n')
        (repo / 'icon.bin').write_bytes(b'\x00\x01\x02\xff\xfe')
        _git_run(repo, 'add', 'app.py', 'helper.py', 'icon.bin')
        _git_run(repo, 'commit', '-m', 'Worker feature commit')
        # Unstaged modification to README.md (delete 1 line, add 2 lines)
        (repo / 'README.md').write_text('# Demo\nUpdated line two\nAdded line three\n', encoding='utf-8', newline='\n')
        # Untracked scratch file must NOT pollute tracked git diff against launch_commit
        (repo / 'scratch.tmp').write_text('temporary debug notes\n', encoding='utf-8', newline='\n')

        agent_id = 'agent-claude-1'
        session_id = 'sess-claude-real-1'
        self._write_paseo_agent(agent_id, 'claude-lab', session_id, cwd=str(repo))

        did = claimed['dispatch_id']
        transcript = self.root / '.claude-lab' / 'projects' / 'worker-repo' / f'{session_id}.jsonl'
        _write_jsonl(
            transcript,
            [
                {
                    'type': 'user',
                    'timestamp': '2026-11-12T09:00:05Z',
                    'message': {
                        'role': 'user',
                        'content': [
                            {
                                'type': 'text',
                                'text': f'<paseo-system version="1" kind="agent-message" message-id="{did}">\nImplement feature\n</paseo-system>',
                            }
                        ],
                    },
                },
                # Streaming chunk 1 of msg_01 (partial output_tokens=2, should be superseded by final msg_01 chunk)
                {
                    'type': 'assistant',
                    'timestamp': '2026-11-12T09:00:20Z',
                    'message': {
                        'id': 'msg_01',
                        'model': 'claude-sonnet-5-5',
                        'content': [{'type': 'tool_use', 'id': 'toolu_1', 'name': 'Read', 'input': {'file_path': 'app.py'}}],
                        'usage': {
                            'input_tokens': 400,
                            'cache_creation_input_tokens': 1200,
                            'cache_read_input_tokens': 3000,
                            'output_tokens': 2,
                        },
                    },
                },
                # Final chunk of msg_01
                {
                    'type': 'assistant',
                    'timestamp': '2026-11-12T09:00:25Z',
                    'message': {
                        'id': 'msg_01',
                        'model': 'claude-sonnet-5-5',
                        'content': [{'type': 'tool_use', 'id': 'toolu_1', 'name': 'Read', 'input': {'file_path': 'app.py'}}],
                        'usage': {
                            'input_tokens': 400,
                            'cache_creation_input_tokens': 1200,
                            'cache_read_input_tokens': 3000,
                            'output_tokens': 45,
                        },
                    },
                },
                # Tool result user message (must NOT count as a parent follow-up turn)
                {
                    'type': 'user',
                    'timestamp': '2026-11-12T09:00:27Z',
                    'message': {
                        'role': 'user',
                        'content': [{'type': 'tool_result', 'tool_use_id': 'toolu_1', 'content': 'def add(a, b): ...'}],
                    },
                },
                # Synthetic local status message (ignored for token usage)
                {
                    'type': 'assistant',
                    'timestamp': '2026-11-12T09:01:00Z',
                    'message': {
                        'id': 'msg_syn',
                        'model': '<synthetic>',
                        'content': [{'type': 'text', 'text': 'status'}],
                        'usage': {'input_tokens': 0, 'output_tokens': 0},
                    },
                },
                # Final reply of initial brief turn at +120s from 09:00:05Z -> 09:02:05Z
                {
                    'type': 'assistant',
                    'timestamp': '2026-11-12T09:02:05Z',
                    'message': {
                        'id': 'msg_02',
                        'model': 'claude-sonnet-5-5',
                        'content': [{'type': 'text', 'text': 'Initial implementation finished.'}],
                        'usage': {
                            'input_tokens': 250,
                            'cache_creation_input_tokens': 300,
                            'cache_read_input_tokens': 4600,
                            'output_tokens': 110,
                        },
                    },
                },
                # 20-minute (1200s > RUN_LEASE=600s) idle gap before parent follow-up at 09:22:05Z
                {
                    'type': 'user',
                    'timestamp': '2026-11-12T09:22:05Z',
                    'message': {
                        'role': 'user',
                        'content': '<paseo-system version="1" kind="agent-message" message-id="99999999-1111-2222-3333-444444444444">\nPlease also update README.\n</paseo-system>',
                    },
                },
                # Follow-up assistant work ending 65s later at 09:23:10Z
                {
                    'type': 'assistant',
                    'timestamp': '2026-11-12T09:23:10Z',
                    'message': {
                        'id': 'msg_03',
                        'model': 'claude-sonnet-5-5',
                        'content': [{'type': 'text', 'text': 'Updated README as requested.'}],
                        'usage': {
                            'input_tokens': 150,
                            'cache_creation_input_tokens': 100,
                            'cache_read_input_tokens': 5200,
                            'output_tokens': 60,
                        },
                    },
                },
            ],
        )

        # Observe running then reconcile completed via dispatch_flow
        registry.observe(
            did,
            self._snapshot(claimed, agent_id, 'running', self.now + timedelta(seconds=15), cwd=str(repo)),
            now=self.now + timedelta(seconds=15),
        )
        done_at = self.now + timedelta(minutes=24)
        reconciled = flow.reconcile(
            did,
            host=lambda payload: {
                'ok': True,
                'agent': self._snapshot(claimed, agent_id, 'idle', done_at, reason='finished', cwd=str(repo)),
            },
            now=done_at,
        )
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0]['state'], 'completed')

        stored = registry.inspect(did)
        self.assertEqual(stored['state'], 'completed')
        self.assertEqual(stored['telemetry_harness'], 'claude')
        self.assertEqual(stored['worker_session_id'], session_id)
        self.assertIsNone(stored['worker_session_reason'])
        self.assertEqual(stored['idle_threshold_seconds'], registry.RUN_LEASE)
        self.assertEqual(stored['active_seconds'], 185.0)
        self.assertIsNone(stored['active_seconds_reason'])

        # Follow-up boundary: 120s up to first parent follow-up, 65s from follow-up onward
        self.assertEqual(stored['initial_active_seconds'], 120.0)
        self.assertEqual(stored['followup_active_seconds'], 65.0)
        self.assertEqual(stored['first_followup_at'], '2026-11-12T09:22:05Z')
        self.assertEqual(stored['user_turns'], 2)
        self.assertEqual(stored['followup_turns'], 1)
        self.assertIsNone(stored['followup_boundary_reason'])

        # Deduplicated Claude token usage (msg_01 final + msg_02 + msg_03)
        self.assertIsNone(stored['token_usage_reason'])
        self.assertEqual(
            stored['token_usage'],
            {
                'input_tokens': 800,
                'output_tokens': 215,
                'cache_read_tokens': 12800,
                'cache_write_tokens': 1600,
                'raw_input_tokens': 800,
            },
        )

        # Git working-tree diff relative to launch_commit:
        # app.py (+4/-1), helper.py (+1/-0), icon.bin (binary: +0/-0, 1 file), README.md (+2/-1); scratch.tmp ignored
        self.assertEqual(stored['files_changed'], 4)
        self.assertEqual(stored['lines_added'], 7)
        self.assertEqual(stored['lines_deleted'], 2)
        self.assertEqual(stored['lines_changed'], 9)
        self.assertEqual(stored['git_diff_basis'], 'working_tree_vs_launch_commit')
        self.assertIsNone(stored['git_changes_reason'])

    def test_codex_single_turn_and_resumed_dispatch_slice_and_fallback_message_id_lookup(self):
        repo = self.root / 'codex-repo'
        _init_git_repo(repo)
        bundle = dict(account_fleet.bundles(self.accounts)['astra_high'], bundle_id='astra_high')

        # Dispatch 1: create without Paseo persistence.sessionId on disk -> located via message-id fallback
        claimed1 = self._issue_and_claim(bundle, cwd=str(repo))
        did1 = claimed1['dispatch_id']
        agent_id = 'agent-codex-1'
        session_id = '019a7000-1111-7222-8333-444455556666'
        rollout_path = self.root / '.codex' / 'sessions' / '2026' / '11' / '12' / f'rollout-2026-11-12T09-00-00-{session_id}.jsonl'

        _write_jsonl(
            rollout_path,
            [
                {'timestamp': '2026-11-12T09:00:01Z', 'type': 'session_meta', 'payload': {'id': session_id, 'cwd': str(repo)}},
                {'timestamp': '2026-11-12T09:00:01Z', 'type': 'turn_context', 'payload': {'model': 'gpt-6-astra', 'effort': 'high'}},
                {
                    'timestamp': '2026-11-12T09:00:02Z',
                    'type': 'response_item',
                    'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': '<environment_context>\n<cwd>' + str(repo) + '</cwd>\n</environment_context>'}]},
                },
                {
                    'timestamp': '2026-11-12T09:00:02Z',
                    'type': 'event_msg',
                    'payload': {
                        'type': 'user_message',
                        'message': f'<paseo-system version="1" kind="agent-message" message-id="{did1}">\nInspect repository.\n</paseo-system>',
                    },
                },
                {
                    'timestamp': '2026-11-12T09:00:42Z',
                    'type': 'event_msg',
                    'payload': {
                        'type': 'token_count',
                        'info': {
                            'total_token_usage': {
                                'input_tokens': 10000,
                                'cached_input_tokens': 6500,
                                'output_tokens': 400,
                                'reasoning_output_tokens': 180,
                                'total_tokens': 10400,
                            },
                            'last_token_usage': {
                                'input_tokens': 10000,
                                'cached_input_tokens': 6500,
                                'output_tokens': 400,
                                'reasoning_output_tokens': 180,
                                'total_tokens': 10400,
                            },
                        },
                    },
                },
                # Duplicate end-of-turn token_count emitted with identical total_tokens=10400
                {
                    'timestamp': '2026-11-12T09:01:31Z',
                    'type': 'event_msg',
                    'payload': {
                        'type': 'token_count',
                        'info': {
                            'total_token_usage': {
                                'input_tokens': 10000,
                                'cached_input_tokens': 6500,
                                'output_tokens': 400,
                                'reasoning_output_tokens': 180,
                                'total_tokens': 10400,
                            },
                            'last_token_usage': {
                                'input_tokens': 10000,
                                'cached_input_tokens': 6500,
                                'output_tokens': 400,
                                'reasoning_output_tokens': 180,
                                'total_tokens': 10400,
                            },
                        },
                    },
                },
            ],
        )

        done1 = self.now + timedelta(minutes=2)
        row1 = registry.observe(
            did1,
            self._snapshot(claimed1, agent_id, 'idle', done1, reason='finished', cwd=str(repo)),
            now=done1,
        )
        self.assertEqual(row1['telemetry_harness'], 'codex')
        self.assertEqual(row1['worker_session_id'], session_id)
        self.assertEqual(row1['active_seconds'], 89.0)
        self.assertEqual(row1['initial_active_seconds'], 89.0)
        self.assertEqual(row1['followup_active_seconds'], 0.0)
        self.assertIsNone(row1['first_followup_at'])
        self.assertEqual(row1['user_turns'], 1)
        self.assertEqual(row1['followup_turns'], 0)
        self.assertEqual(row1['followup_boundary_reason'], 'no_parent_followup')
        self.assertEqual(
            row1['token_usage'],
            {
                'input_tokens': 3500,
                'output_tokens': 400,
                'cache_read_tokens': 6500,
                'cache_write_tokens': None,
                'cache_write_reason': 'codex_does_not_expose_cache_write_tokens',
                'raw_input_tokens': 10000,
                'reasoning_output_tokens': 180,
            },
        )
        # Clean repo -> 0 changed files/lines
        self.assertEqual((row1['files_changed'], row1['lines_changed']), (0, 0))
        self.assertIsNone(row1['git_changes_reason'])

        # Dispatch 2: resume the same Codex worker 30 minutes later in the same rollout file
        self._write_paseo_agent(agent_id, 'codex', session_id, cwd=str(repo))
        t2_start = self.now + timedelta(minutes=30)
        task2 = dict(self.task, task_id='gt-task-2', objective='Follow-up resumed task on same Codex worker')
        task2['profile'] = profile_for(task2)
        claimed2 = self._issue_and_claim(bundle, cwd=str(repo), task=task2, resume_agent_id=agent_id, now=t2_start)
        did2 = claimed2['dispatch_id']
        self.assertEqual(claimed2['operation'], 'resume')

        # Append Dispatch 2 events to the existing rollout file
        existing_lines = [json.loads(line) for line in rollout_path.read_text(encoding='utf-8').splitlines() if line.strip()]
        existing_lines.extend(
            [
                {
                    'timestamp': '2026-11-12T09:30:10Z',
                    'type': 'event_msg',
                    'payload': {
                        'type': 'user_message',
                        'message': f'<paseo-system version="1" kind="agent-message" message-id="{did2}">\nNow modify app.py.\n</paseo-system>',
                    },
                },
                {
                    'timestamp': '2026-11-12T09:30:55Z',
                    'type': 'event_msg',
                    'payload': {
                        'type': 'token_count',
                        'info': {
                            'total_token_usage': {
                                'input_tokens': 25000,
                                'cached_input_tokens': 21000,
                                'output_tokens': 950,
                                'reasoning_output_tokens': 430,
                                'total_tokens': 25950,
                            },
                            'last_token_usage': {
                                'input_tokens': 15000,
                                'cached_input_tokens': 14500,
                                'output_tokens': 550,
                                'reasoning_output_tokens': 250,
                                'total_tokens': 15550,
                            },
                        },
                    },
                },
            ]
        )
        _write_jsonl(rollout_path, existing_lines)
        (repo / 'app.py').write_text('def add(a, b):\n    return a + b + 0\n', encoding='utf-8', newline='\n')

        registry.observe(
            did2,
            self._snapshot(claimed2, agent_id, 'running', t2_start + timedelta(seconds=20), cwd=str(repo)),
            now=t2_start + timedelta(seconds=20),
        )
        done2 = t2_start + timedelta(minutes=2)
        row2 = registry.observe(
            did2,
            self._snapshot(claimed2, agent_id, 'idle', done2, reason='finished', cwd=str(repo)),
            now=done2,
        )
        # Dispatch 2 measures only its own 45s slice and subtracts Dispatch 1's cumulative token baseline
        self.assertEqual(row2['active_seconds'], 45.0)
        self.assertEqual(row2['initial_active_seconds'], 45.0)
        self.assertEqual(row2['followup_active_seconds'], 0.0)
        self.assertEqual(
            row2['token_usage'],
            {
                'input_tokens': 500,
                'output_tokens': 550,
                'cache_read_tokens': 14500,
                'cache_write_tokens': None,
                'cache_write_reason': 'codex_does_not_expose_cache_write_tokens',
                'raw_input_tokens': 15000,
                'reasoning_output_tokens': 250,
            },
        )
        self.assertEqual((row2['files_changed'], row2['lines_added'], row2['lines_deleted'], row2['lines_changed']), (1, 1, 1, 2))

    def test_git_edge_cases_nongit_cwd_and_missing_launch_commit_never_default_to_zero(self):
        bundle = dict(account_fleet.bundles(self.accounts)['lab_sonnet55_high'], bundle_id='lab_sonnet55_high')

        # 1) Non-git directory -> null with 'not_a_git_checkout'
        plain_dir = self.root / 'not-a-repo'
        plain_dir.mkdir()
        claimed_nongit = self._issue_and_claim(bundle, cwd=str(plain_dir))
        self.assertIsNone(claimed_nongit['launch_commit'])
        self.assertEqual(claimed_nongit['launch_commit_reason'], 'not_a_git_checkout')
        done_nongit = registry.observe(
            claimed_nongit['dispatch_id'],
            self._snapshot(claimed_nongit, 'agent-ng', 'idle', self.now + timedelta(minutes=1), reason='finished', cwd=str(plain_dir)),
            now=self.now + timedelta(minutes=1),
        )
        self.assertIsNone(done_nongit['files_changed'])
        self.assertIsNone(done_nongit['lines_added'])
        self.assertIsNone(done_nongit['lines_deleted'])
        self.assertIsNone(done_nongit['lines_changed'])
        self.assertEqual(done_nongit['git_changes_reason'], 'not_a_git_checkout')
        self.assertIsNone(done_nongit['active_seconds'])
        self.assertEqual(done_nongit['active_seconds_reason'], 'session_transcript_not_found')

        # 2) Dispatch prepared without cwd that jumps directly to completed inside a git repo (no launch_commit recorded)
        repo = self.root / 'late-cwd-repo'
        _init_git_repo(repo)
        task_late = dict(self.task, task_id='gt-task-late')
        task_late['profile'] = profile_for(task_late)
        claimed_late = self._issue_and_claim(bundle, cwd=None, task=task_late)
        self.assertIsNone(claimed_late['launch_commit'])
        self.assertEqual(claimed_late['launch_commit_reason'], 'worker_cwd_unknown')
        done_late = registry.observe(
            claimed_late['dispatch_id'],
            self._snapshot(claimed_late, 'agent-late', 'idle', self.now + timedelta(minutes=1), reason='finished', cwd=str(repo)),
            now=self.now + timedelta(minutes=1),
        )
        self.assertIsNone(done_late['files_changed'])
        self.assertIsNone(done_late['lines_changed'])
        self.assertEqual(done_late['git_changes_reason'], 'worker_cwd_unknown')

        # 3) Unborn git repository -> null with 'git_head_unborn'
        unborn = self.root / 'unborn-repo'
        unborn.mkdir()
        _git_run(unborn, 'init')
        task_unborn = dict(self.task, task_id='gt-task-unborn')
        task_unborn['profile'] = profile_for(task_unborn)
        with patch.object(subprocess, 'Popen', side_effect=AssertionError('prepare must not spawn subprocesses')):
            claimed_unborn = self._issue_and_claim(bundle, cwd=str(unborn), task=task_unborn)
        self.assertIsNone(claimed_unborn['launch_commit'])
        self.assertEqual(claimed_unborn['launch_commit_reason'], 'git_head_unborn')

        # 4) Packed-refs repository and linked git worktree resolve launch_commit without spawning subprocesses
        _git_run(repo, 'pack-refs', '--all', '--prune')
        expected_packed_sha = _git_run(repo, 'rev-parse', '--verify', 'HEAD')
        wt_dir = self.root / 'linked-wt'
        _git_run(repo, 'worktree', 'add', '-b', 'wt-branch', str(wt_dir), 'HEAD')
        task_packed = dict(self.task, task_id='gt-task-packed')
        task_packed['profile'] = profile_for(task_packed)
        task_wt = dict(self.task, task_id='gt-task-wt')
        task_wt['profile'] = profile_for(task_wt)
        with patch.object(subprocess, 'Popen', side_effect=AssertionError('prepare must not spawn subprocesses')):
            claimed_packed = self._issue_and_claim(bundle, cwd=str(repo), task=task_packed, access='read_only')
            claimed_wt = self._issue_and_claim(bundle, cwd=str(wt_dir), task=task_wt)
        self.assertEqual(claimed_packed['launch_commit'], expected_packed_sha)
        self.assertEqual(claimed_wt['launch_commit'], expected_packed_sha)

    def test_unsupported_harnesses_record_null_with_reason_antigravity_pi_grok_bot_and_dot(self):
        # 1) Antigravity (argon) via Paseo
        argon_bundle = dict(account_fleet.bundles(self.accounts)['argon_high'], bundle_id='argon_high')
        task_ag = dict(self.task, task_id='gt-ag')
        task_ag['profile'] = profile_for(task_ag)
        claimed_ag = self._issue_and_claim(argon_bundle, cwd=None, task=task_ag, user_override=True)
        done_ag = registry.observe(
            claimed_ag['dispatch_id'],
            self._snapshot(claimed_ag, 'agent-argon', 'idle', self.now + timedelta(minutes=1), reason='finished'),
            now=self.now + timedelta(minutes=1),
        )
        self.assertEqual(done_ag['telemetry_harness'], 'antigravity')
        for key in ('active_seconds', 'idle_threshold_seconds', 'token_usage', 'initial_active_seconds', 'followup_active_seconds', 'first_followup_at', 'user_turns'):
            self.assertIsNone(done_ag[key], key)
        self.assertEqual(done_ag['active_seconds_reason'], 'antigravity_session_telemetry_unsupported')
        self.assertEqual(done_ag['token_usage_reason'], 'antigravity_session_telemetry_unsupported')
        self.assertEqual(done_ag['followup_boundary_reason'], 'antigravity_session_telemetry_unsupported')

        # 1b) Antigravity (carbon) via Paseo
        carbon_bundle = {
            'bundle_id': 'carbon_high',
            'id': 'carbon_high',
            'provider': 'antigravity-resilient',
            'harness': 'antigravity-resilient',
            'account': 'antigravity',
            'model': 'gemini-4-carbon-high',
            'effort': 'high',
            'modeId': 'full-access',
        }
        self.accounts['models'].setdefault('gemini-4-carbon-high', {})
        self.accounts['accounts']['antigravity']['bundles']['carbon_high'] = dict(carbon_bundle)
        task_cb = dict(self.task, task_id='gt-cb')
        task_cb['profile'] = profile_for(task_cb)
        claimed_cb = self._issue_and_claim(carbon_bundle, cwd=None, task=task_cb, user_override=True)
        done_cb = registry.observe(
            claimed_cb['dispatch_id'],
            self._snapshot(claimed_cb, 'agent-carbon', 'idle', self.now + timedelta(minutes=1), reason='finished'),
            now=self.now + timedelta(minutes=1),
        )
        self.assertEqual(done_cb['telemetry_harness'], 'antigravity')
        self.assertIsNone(done_cb['active_seconds'])
        self.assertIsNone(done_cb['token_usage'])
        self.assertEqual(done_cb['active_seconds_reason'], 'antigravity_session_telemetry_unsupported')

        # 2) Pi via Paseo
        pi_bundle = dict(account_fleet.bundles(self.accounts)['pi_astra_high'], bundle_id='pi_astra_high')
        task_pi = dict(self.task, task_id='gt-pi')
        task_pi['profile'] = profile_for(task_pi)
        claimed_pi = self._issue_and_claim(pi_bundle, cwd=None, task=task_pi, user_override=True)
        done_pi = registry.observe(
            claimed_pi['dispatch_id'],
            self._snapshot(claimed_pi, 'agent-pi', 'idle', self.now + timedelta(minutes=1), reason='finished'),
            now=self.now + timedelta(minutes=1),
        )
        self.assertEqual(done_pi['telemetry_harness'], 'pi')
        for key in ('active_seconds', 'token_usage', 'initial_active_seconds', 'followup_active_seconds', 'user_turns'):
            self.assertIsNone(done_pi[key], key)
        self.assertEqual(done_pi['active_seconds_reason'], 'pi_session_telemetry_unsupported')
        self.assertEqual(done_pi['token_usage_reason'], 'pi_session_telemetry_unsupported')
        self.assertEqual(done_pi['followup_boundary_reason'], 'pi_session_telemetry_unsupported')

        # 3) Grok Bot via roll.grok_bot + run_grok_bot.status
        grok_state = self.root / 'grok-bot-state'
        grok_state.mkdir()
        (grok_state / 'config.json').write_text(json.dumps({'bot_id': 'bot-1'}), encoding='utf-8', newline='\n')
        prompt_file = self.root / 'grok-prompt.md'
        prompt_file.write_text('Research public docs', encoding='utf-8', newline='\n')
        grok_bundle = {'provider': 'grok-bot', 'account': 'grok-bot', 'model': 'grok-bot'}
        task_gb = {'task_id': 'gt-grok-bot', 'parent_agent_id': 'parent-gt', 'objective': 'Research'}
        dec_gb = registry.issue({'stage': 'record', 'choice': 'grok-bot', 'selected_bundle': grok_bundle, 'decision': {'action': 'start'}}, task_gb, self.now)

        with patch.object(grok_runner, 'STATE', grok_state):
            with patch.object(grok_runner, 'gbot', side_effect=[[{'id': 'bot-1'}], {'delivery': 'accepted', 'messageId': 'msg-gb-1'}]):
                res_gb, code_gb = roll.grok_bot(
                    SimpleNamespace(file=[], apply_to=None, local_only=None, workspace=None),
                    {'parent_id': 'parent-gt'},
                    prompt_file,
                    task_gb,
                    grok_bundle,
                    decision=dec_gb,
                )
            self.assertEqual(code_gb, 0)
            gb_tid = res_gb['worker']['task_id']
            # External launch must NOT appear in Paseo pending() or reservations()
            self.assertEqual(registry.pending(), [])
            self.assertEqual(registry.reservations(self.now), [])

            thread_reply = {
                'target': {'id': 'bot-1'},
                'thread': {
                    'entries': [
                        {
                            'kind': 'send-message',
                            'message': {'content': f'Done answer\n{grok_runner.marker(gb_tid, "completed")}'},
                        }
                    ]
                },
            }
            with patch.object(grok_runner, 'gbot', return_value=thread_reply):
                status_gb = grok_runner.status(gb_tid)
            self.assertEqual(status_gb['state'], 'completed')

        stored_gb = registry.inspect(f'grok-bot:{gb_tid}')
        self.assertEqual(stored_gb['state'], 'completed')
        self.assertEqual(stored_gb['telemetry_harness'], 'grok-bot')
        self.assertIsNone(stored_gb['active_seconds'])
        self.assertEqual(stored_gb['active_seconds_reason'], 'grok_bot_remote_telemetry_unavailable')
        self.assertIsNone(stored_gb['token_usage'])
        self.assertEqual(stored_gb['token_usage_reason'], 'grok_bot_remote_telemetry_unavailable')
        self.assertIsNone(stored_gb['initial_active_seconds'])
        self.assertEqual(stored_gb['followup_boundary_reason'], 'grok_bot_remote_telemetry_unavailable')
        self.assertIsNone(stored_gb['files_changed'])
        self.assertEqual(stored_gb['git_changes_reason'], 'external_remote_runner')

        # 4) Dot via roll.dot + run_dot_task.status_command
        dot_bundle = {'provider': 'dot', 'account': 'dot', 'model': 'dot'}
        task_dot = {'task_id': 'gt-dot', 'parent_agent_id': 'parent-gt', 'objective': 'Public vendor research'}
        dec_dot = registry.issue({'stage': 'plan', 'choice': 'dot', 'selected_bundle': dot_bundle, 'decision': {'action': 'start'}}, task_dot, self.now)
        fake_proc = subprocess.CompletedProcess(args=['python'], returncode=0, stdout='dot_task_99\n', stderr='task_id=dot_task_99\n')
        with patch.object(roll.subprocess, 'run', return_value=fake_proc):
            res_dot, code_dot = roll.dot(
                SimpleNamespace(file=[], apply_to=None),
                {'parent_id': 'parent-gt'},
                prompt_file,
                task=task_dot,
                decision=dec_dot,
            )
        self.assertEqual((code_dot, res_dot['worker']['task_id']), (0, 'dot_task_99'))
        self.assertEqual(registry.pending(), [])

        with patch.object(dot_runner, 'task_view', return_value={'status': 'completed', 'result': {'content': 'Dot research report'}}), \
                contextlib.redirect_stdout(io.StringIO()):
            rc_dot = dot_runner.status_command({}, 'dot_task_99')
        self.assertEqual(rc_dot, 0)

        stored_dot = registry.inspect('dot:dot_task_99')
        self.assertEqual(stored_dot['state'], 'completed')
        self.assertEqual(stored_dot['telemetry_harness'], 'dot')
        self.assertIsNone(stored_dot['active_seconds'])
        self.assertEqual(stored_dot['active_seconds_reason'], 'dot_remote_telemetry_unavailable')
        self.assertIsNone(stored_dot['token_usage'])
        self.assertEqual(stored_dot['token_usage_reason'], 'dot_remote_telemetry_unavailable')
        self.assertIsNone(stored_dot['initial_active_seconds'])
        self.assertEqual(stored_dot['followup_boundary_reason'], 'dot_remote_telemetry_unavailable')
        self.assertIsNone(stored_dot['files_changed'])
        self.assertEqual(stored_dot['git_changes_reason'], 'external_remote_runner')

    def test_allocation_and_quota_economy_never_read_ground_truth_fields(self):
        # 1) Static check: allocator.plan and all split allocation / quota-economy modules never import
        # dispatch_ground_truth or reference any ground-truth field name.
        scripts_dir = Path(allocator.__file__).resolve().parent
        for mod_name in ALLOCATION_MODULES:
            src = (scripts_dir / mod_name).read_text(encoding='utf-8-sig')
            tree = ast.parse(src, filename=mod_name)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotEqual(alias.name, 'dispatch_ground_truth', f'{mod_name} must not import dispatch_ground_truth')
                elif isinstance(node, ast.ImportFrom):
                    self.assertNotEqual(node.module, 'dispatch_ground_truth', f'{mod_name} must not import from dispatch_ground_truth')
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    self.assertNotIn(
                        node.value,
                        GROUND_TRUTH_ONLY_KEYS,
                        f'{mod_name} references ground-truth key {node.value!r}',
                    )

        # 2) Behavioral invariance: allocator.plan produces identical rankings, expected_usd, economy and snapshot
        # regardless of extreme or null ground-truth payloads in dispatch.sqlite3, records, or chain worker dicts.
        plan_task = {
            'task_id': 'alloc-invariance',
            'objective': 'Implement feature module',
            'expected_hours': 0.8,
            'profile': profile_for({'work_kind': 'general_implementation', 'difficulty': 'hard', 'expected_hours': 0.8}),
            'chain': {
                'id': 'chain-1',
                'coupling': 'medium',
                'worker': {
                    'agent_id': 'worker-clean',
                    'bundle': 'lab_sonnet55_high',
                    'read_only_review': False,
                    'compactions': 0,
                    'compacted_tokens': 0,
                    'context_tokens': 40_000,
                    'historical_context_tokens': 40_000,
                },
            },
        }
        baseline = allocator.plan(json.loads(json.dumps(plan_task)), self.usage, self.now, records=[], accounts=self.accounts)
        baseline_snap = ledger_snapshot.decision_snapshot(baseline)

        poison_gt = {
            'ground_truth_recorded_at': self.now.isoformat(),
            'telemetry_harness': 'claude',
            'worker_session_id': 'sess-poison',
            'worker_session_reason': None,
            'active_seconds': 999_999.0,
            'idle_threshold_seconds': 600,
            'active_seconds_reason': None,
            'token_usage': {
                'input_tokens': 90_000_000,
                'output_tokens': 25_000_000,
                'cache_read_tokens': 400_000_000,
                'cache_write_tokens': 80_000_000,
                'raw_input_tokens': 90_000_000,
            },
            'token_usage_reason': None,
            'initial_active_seconds': 500_000.0,
            'followup_active_seconds': 499_999.0,
            'first_followup_at': self.now.isoformat(),
            'user_turns': 42,
            'followup_turns': 41,
            'followup_boundary_reason': None,
            'files_changed': 10_000,
            'lines_added': 800_000,
            'lines_deleted': 200_000,
            'lines_changed': 1_000_000,
            'git_diff_basis': 'working_tree_vs_launch_commit',
            'git_changes_reason': None,
            'launch_commit': 'a' * 40,
            'launch_git_toplevel': str(self.root),
            'launch_commit_reason': None,
        }

        # Persist a completed row with extreme ground truth in dispatch.sqlite3
        bundle = dict(account_fleet.bundles(self.accounts)['lab_sonnet55_high'], bundle_id='lab_sonnet55_high')
        claimed = self._issue_and_claim(bundle)
        registry.observe(
            claimed['dispatch_id'],
            self._snapshot(claimed, 'worker-clean', 'idle', self.now + timedelta(minutes=1), reason='finished'),
            now=self.now + timedelta(minutes=1),
        )
        with registry.transaction() as connection:
            stored = registry.get(connection, 'dispatches', claimed['dispatch_id'])
            stored.update(poison_gt)
            registry.save(connection, stored)

        poisoned_task = json.loads(json.dumps(plan_task))
        poisoned_task['chain']['worker'].update(poison_gt)
        completed_record_with_gt = {
            'stage': 'dispatch',
            'dispatch_id': claimed['dispatch_id'],
            'state': 'completed',
            'at': self.now.isoformat(),
            'choice': 'lab_sonnet55_high',
            'bundle': bundle,
            'expected_usd': 5.0,
            'usage': self.usage['accounts'],
            **poison_gt,
        }
        live_reservations = registry.reservations(self.now + timedelta(minutes=2))
        self.assertEqual(live_reservations, [])

        with_poison = allocator.plan(
            poisoned_task,
            self.usage,
            self.now,
            records=[*live_reservations, completed_record_with_gt],
            accounts=self.accounts,
        )
        self.assertEqual(with_poison, baseline)
        self.assertEqual(ledger_snapshot.decision_snapshot(with_poison), baseline_snap)

    def test_codex_followup_turn_boundary_and_subsequent_dispatch_end_slicing(self):
        repo = self.root / 'codex-followup-repo'
        _init_git_repo(repo)
        bundle = dict(account_fleet.bundles(self.accounts)['astra_high'], bundle_id='astra_high')
        claimed1 = self._issue_and_claim(bundle, cwd=str(repo))
        did1 = claimed1['dispatch_id']
        agent_id = 'agent-codex-fu'
        session_id = '019a7000-aaaa-7bbb-8ccc-ddddeeeeffff'
        self._write_paseo_agent(agent_id, 'codex', session_id, cwd=str(repo))
        registry.observe(
            did1,
            self._snapshot(claimed1, agent_id, 'running', self.now + timedelta(seconds=10), cwd=str(repo)),
            now=self.now + timedelta(seconds=10),
        )

        # Insert a later dispatch row on the same agent so `end_idx` stops before its message-id
        later_did = 'ffffffff-1111-2222-3333-444444444444'
        with registry.transaction() as connection:
            later_row = dict(
                claimed1,
                dispatch_id=later_did,
                decision_id='dec-later',
                agent_id=agent_id,
                created_at=(self.now + timedelta(minutes=40)).isoformat(),
                launched_at=(self.now + timedelta(minutes=40)).isoformat(),
                state='completed',
            )
            registry.save(connection, later_row)

        rollout_path = self.root / '.codex' / 'sessions' / '2026' / '11' / '12' / f'rollout-2026-11-12T09-00-00-{session_id}.jsonl'
        _write_jsonl(
            rollout_path,
            [
                {'timestamp': '2026-11-12T09:00:00Z', 'type': 'session_meta', 'payload': {'id': session_id}},
                {
                    'timestamp': '2026-11-12T09:00:05Z',
                    'type': 'event_msg',
                    'payload': {'type': 'user_message', 'message': f'<paseo-system message-id="{did1}">Initial brief</paseo-system>'},
                },
                {
                    'timestamp': '2026-11-12T09:01:35Z',
                    'type': 'event_msg',
                    'payload': {
                        'type': 'token_count',
                        'info': {
                            'total_token_usage': {'total_tokens': 5300},
                            'last_token_usage': {'input_tokens': 5000, 'cached_input_tokens': 4000, 'output_tokens': 300},
                        },
                    },
                },
                # 15-minute idle wait (> 600s) before parent follow-up
                {
                    'timestamp': '2026-11-12T09:16:35Z',
                    'type': 'event_msg',
                    'payload': {'type': 'user_message', 'message': '<paseo-system message-id="followup-msg-1">Please refine edge case</paseo-system>'},
                },
                {
                    'timestamp': '2026-11-12T09:17:25Z',
                    'type': 'event_msg',
                    'payload': {
                        'type': 'token_count',
                        'info': {
                            'total_token_usage': {'total_tokens': 11500},
                            'last_token_usage': {'input_tokens': 6000, 'cached_input_tokens': 5500, 'output_tokens': 200},
                        },
                    },
                },
                # Later dispatch on the same worker must be excluded from did1's slice
                {
                    'timestamp': '2026-11-12T09:40:10Z',
                    'type': 'event_msg',
                    'payload': {'type': 'user_message', 'message': f'<paseo-system message-id="{later_did}">Unrelated next task</paseo-system>'},
                },
                {
                    'timestamp': '2026-11-12T09:42:10Z',
                    'type': 'event_msg',
                    'payload': {
                        'type': 'token_count',
                        'info': {
                            'total_token_usage': {'total_tokens': 99999},
                            'last_token_usage': {'input_tokens': 80000, 'cached_input_tokens': 70000, 'output_tokens': 8499},
                        },
                    },
                },
            ],
        )

        done1 = self.now + timedelta(minutes=20)
        row1 = registry.observe(
            did1,
            self._snapshot(claimed1, agent_id, 'idle', done1, reason='finished', cwd=str(repo)),
            now=done1,
        )
        self.assertEqual(row1['active_seconds'], 140.0)
        self.assertEqual(row1['initial_active_seconds'], 90.0)
        self.assertEqual(row1['followup_active_seconds'], 50.0)
        self.assertEqual(row1['first_followup_at'], '2026-11-12T09:16:35Z')
        self.assertEqual(row1['user_turns'], 2)
        self.assertEqual(row1['followup_turns'], 1)
        self.assertEqual(
            row1['token_usage'],
            {
                'input_tokens': 1500,
                'output_tokens': 500,
                'cache_read_tokens': 9500,
                'cache_write_tokens': None,
                'cache_write_reason': 'codex_does_not_expose_cache_write_tokens',
                'raw_input_tokens': 11000,
            },
        )

    def test_read_only_paseo_roll_captures_cwd_and_launch_commit_without_same_cwd_warning(self):
        repo = self.root / 'shared-repo'
        expected_sha = _init_git_repo(repo)
        bundle = dict(account_fleet.bundles(self.accounts)['lab_sonnet55_high'], bundle_id='lab_sonnet55_high')

        # Active writer already in `repo`
        writer_task = dict(self.task, task_id='writer-1')
        writer_task['profile'] = profile_for(writer_task)
        self._issue_and_claim(bundle, cwd=str(repo), task=writer_task, access='write')

        # Read-only review dispatch in the same `repo` via roll.paseo
        ro_profile = profile_for({'work_kind': 'debug_review', 'difficulty': 'hard', 'expected_hours': 0.3})
        ro_profile.update(code_complexity=0, read_only_review=True, access='read-only')
        ro_task = {
            'task_id': 'ro-review-1',
            'parent_agent_id': 'parent-gt',
            'objective': 'Review code only',
            'expected_hours': 0.3,
            'profile': ro_profile,
        }
        decision = registry.issue(
            {
                'stage': 'plan',
                'choice': 'lab_sonnet55_high',
                'selected_bundle': bundle,
                'task_profile': ro_profile,
                'decision': {'action': 'start'},
            },
            ro_task,
            self.now,
        )
        decision_payload = {
            'decision_id': decision['decision_id'],
            'choice': 'lab_sonnet55_high',
            'selected_bundle': bundle,
            'task_profile': ro_profile,
            'decision': {'action': 'start'},
        }
        args = SimpleNamespace(workspace=None, access='read-only', resume=None, new_worker=False, title='Read-only review')
        out = {'stage': 'roll', 'parent_id': 'parent-gt', 'parent_host': 'paseo', 'parent_address': 'parent-gt'}

        def fake_launch(dispatch_id, parent_id, watch_seconds=0):
            row = registry.inspect(dispatch_id)
            claimed = registry.claim(row['request'], parent_id=parent_id, call_id='ro-call', now=self.now)
            observed = registry.observe(
                dispatch_id,
                self._snapshot(claimed, 'agent-ro', 'running', self.now + timedelta(seconds=5), cwd=str(repo)),
                now=self.now + timedelta(seconds=5),
            )
            return dict(flow.public(observed), completion_notification={'status': 'watching'})

        with patch.dict(os.environ, {'PASEO_AGENT_CWD': str(repo)}), \
                patch.object(registry, 'utcnow', return_value=self.now), \
                patch.object(flow, 'reconcile', return_value=[]), \
                patch.object(flow, 'launch', side_effect=fake_launch):
            res, code = roll.paseo(args, out, decision_payload, ro_task, 'Review code only', 'parent-gt', self.usage)

        self.assertEqual(code, 0)
        self.assertNotIn('warnings', res)
        ro_did = res['worker']['dispatch_id']
        ro_row = registry.inspect(ro_did)
        self.assertEqual(ro_row['access'], 'read_only')
        self.assertEqual(ro_row['cwd'], str(repo))
        self.assertEqual(ro_row['launch_commit'], expected_sha)


if __name__ == '__main__':
    unittest.main()
