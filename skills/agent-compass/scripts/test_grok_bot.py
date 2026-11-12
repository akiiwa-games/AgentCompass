"""Grok Bot adapter boundaries: upstream CLI, receipt recovery, correlation and routing."""
import json
from datetime import datetime, timedelta, timezone
from contextlib import closing
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import subprocess
import sqlite3
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pytest

import provider_inventory
import allocator
import account_fleet
import quota_pools
import dispatch_registry
import route_judge
import roll
import run_grok_bot as runner

from router_test_support import isolated_router_state, temporary_root


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup, discovery=False, policy=False)


class GrokBotTests(unittest.TestCase):
    def setUp(self):
        self.root = temporary_root(self.addCleanup)
        state = patch.object(runner, 'STATE', self.root / 'grok-bot')
        state.start()
        self.addCleanup(state.stop)
        runner.STATE.mkdir()
        (runner.STATE / 'config.json').write_text(json.dumps({'bot_id': 'bot-1'}))

    def accepted(self):
        with patch.object(runner, 'gbot', side_effect=[
            [{'id': 'bot-1', 'name': 'Fixture'}],
            {'delivery': 'accepted', 'messageId': 'message-1'},
        ]):
            return runner.submit('Do the task', 'parent-1', 'source-1')

    def thread(self, entries, root=True):
        return {'target': {'id': 'bot-1'}, 'thread' if root else 'transcript': {'entries': entries}}

    def reply(self, task, state='completed', text='Answer'):
        return {'id': 'reply-1', 'kind': 'send-message',
                'message': {'type': 'text', 'content': text + '\n' + runner.marker(task['task_id'], state)}}

    def test_acceptance_is_not_completion_and_repeated_submit_never_replays(self):
        receipt = self.accepted()
        self.assertEqual(receipt['state'], 'accepted')
        with patch.object(runner, 'gbot', side_effect=AssertionError('must not send')):
            self.assertEqual(runner.submit('Do the task', 'parent-1', 'source-1'), receipt)
        entries = [
            {'role': 'user', 'text': runner.marker(receipt['task_id'], 'completed')},
            {'role': 'tool', 'text': runner.marker(receipt['task_id'], 'completed')},
            self.reply({'task_id': 'another-task'}),
            {'kind': 'send-message', 'message': {'type': 'text', 'content': 'Working on it'}},
        ]
        with patch.object(runner, 'gbot', return_value=self.thread(entries)) as call:
            self.assertEqual(runner.status(receipt['task_id'])['state'], 'accepted')
            self.assertIn('--root', call.call_args.args)
        entries.append(self.reply(receipt))
        with patch.object(runner, 'gbot', return_value=self.thread(entries)):
            done = runner.status(receipt['task_id'])
        self.assertEqual((done['state'], done['reply']), ('completed', 'Answer'))
        with patch.object(runner, 'gbot', side_effect=AssertionError('cached result')):
            self.assertEqual(runner.status(receipt['task_id']), done)

    def test_unknown_send_and_interrupted_send_are_durable_and_readable_without_replay(self):
        with patch.object(runner, 'gbot', side_effect=[
            [{'id': 'bot-1'}], runner.TransportError('timeout'),
        ]):
            receipt = runner.submit('Task', 'parent', 'task')
        self.assertEqual((receipt['state'], receipt['delivery']), ('unknown', 'unknown'))
        with patch.object(runner, 'gbot', side_effect=AssertionError('no replay')):
            self.assertEqual(runner.submit('Task', 'parent', 'task'), receipt)
        with patch.object(runner, 'gbot', return_value=self.thread([self.reply(receipt)], root=False)):
            completed = runner.status(receipt['task_id'])
        self.assertEqual(completed['state'], 'completed')
        self.assertEqual(completed['send_error'], 'timeout')
        self.assertEqual(completed['delivery'], 'unknown')
        prompt = self.root / 'prompt.md'
        prompt.write_text('Task')
        args = SimpleNamespace(file=[], apply_to=None, local_only=None, wait=0)
        with patch.object(runner, 'submit', return_value=completed):
            result, code = roll.grok_bot(args, {'parent_id': 'parent'}, prompt, {'task_id': 'task'},
                {'account': 'grok-bot', 'model': 'grok-bot'})
        self.assertEqual((code, result['launched'], result['completed'], result['caller_action']),
                         (0, True, True, 'completed'))
        self.assertNotIn('continuation_argv', result)
        with patch.object(runner, 'gbot', side_effect=[[{'id': 'bot-1'}], KeyboardInterrupt]):
            with self.assertRaises(KeyboardInterrupt):
                runner.submit('Another task', 'parent', 'another-task')
        with patch.object(runner, 'gbot', side_effect=AssertionError('no replay after crash')):
            interrupted = runner.submit('Another task', 'parent', 'another-task')
        self.assertEqual(interrupted['state'], 'unknown')

    def test_needs_input_approval_and_read_failure_are_not_completed_or_resent(self):
        task = self.accepted()
        pending = {'kind': 'send-message', 'message': {
            'type': 'auto-review-approval', 'approval': {'status': 'pending', 'requestId': 'approval-1'}}}
        for entries in ([pending], [self.reply(task, 'needs_input', 'Which option?')]):
            with self.subTest(entries=entries), patch.object(runner, 'gbot', return_value=self.thread(entries)) as call:
                self.assertEqual(runner.status(task['task_id'])['state'], 'needs_input')
                self.assertEqual(call.call_args.args[0], 'thread')
        with patch.object(runner, 'gbot', side_effect=runner.TransportError('read timeout')):
            view = runner.status(task['task_id'])
            self.assertEqual(view['state'], 'needs_input')
            self.assertIn('read_error', view)
        with patch.object(runner, 'gbot', return_value=self.thread([self.reply(task)])):
            self.assertEqual(runner.status(task['task_id'])['state'], 'completed')

    def test_receipt_store_failures_preserve_delivery_and_only_presend_failure_is_unsent(self):
        prompt = self.root / 'prompt.md'
        prompt.write_text('Synthetic receipt persistence task')
        args = SimpleNamespace(file=[], apply_to=None, local_only=None, wait=0)
        def invoke(task='store-test'):
            return roll.grok_bot(args, {'parent_id': 'fixture-parent'}, prompt, {'task_id': task},
                                 {'account': 'grok-bot', 'model': 'grok-bot'})
        real_write = runner.override_store.write
        calls = []
        def fail_final_write(path, receipt):
            calls.append(receipt['state'])
            if len(calls) == 2:
                raise OSError('synthetic disk failure')
            real_write(path, receipt)
        with patch.object(runner, 'gbot', side_effect=[
                [{'id': 'bot-1'}], {'delivery': 'accepted', 'messageId': 'fixture-message'}]) as send, \
             patch.object(runner.override_store, 'write', side_effect=fail_final_write):
            result, code = invoke()
        self.assertEqual((code, result['launched'], result['worker']['delivery']), (0, True, 'accepted'))
        self.assertEqual(send.call_count, 2)  # bot listing plus exactly one send
        self.assertIn('persistence_error', result['worker'])
        task_id = result['worker']['task_id']
        self.assertIn(task_id, result['continuation_argv'])
        self.assertEqual(json.loads(runner.receipt_path(task_id).read_text())['delivery'], 'unknown')
        with patch.object(runner, 'gbot', side_effect=AssertionError('must not resend')):
            replay, _ = invoke()
            self.assertIsNone(replay['launched'])
            self.assertIn(task_id, replay['continuation_argv'])
            for error in (TimeoutError('busy'), OSError('unreadable')):
                with self.subTest(error=type(error).__name__), \
                     patch.object(runner.override_store, 'locked', side_effect=error):
                    busy, _ = invoke()
                    self.assertIsNone(busy['launched'])
                    self.assertEqual(busy['worker']['task_id'], task_id)
                    self.assertIn(task_id, busy['continuation_argv'])
        with patch.object(runner, 'gbot', return_value=[{'id': 'bot-1'}]) as transport, \
             patch.object(runner.override_store, 'write', side_effect=OSError('initial write failed')):
            unsent, code = invoke('never-sent')
        self.assertEqual((code, unsent['launched'], unsent['caller_action']), (1, False, 'check_error'))
        transport.assert_called_once_with('bots', 'list')

    def test_shared_grok_quota_controls_plan_and_launch_without_bot_cache_fallback(self):
        now = datetime.now(timezone.utc)
        # The fixture fleet declares the pool (quota_sources); Grok's meter supplies the window.
        snapshot = {'accounts': {'grok-bot': {'family': 'grok-bot', 'status': 'ready'},
                                 'grok': {'family': 'grok', 'status': 'ready', 'usage_windows': ['credits']}},
                    'launchers': {'grok-bot': {'family': 'grok-bot', 'status': 'ready'}}}
        accounts = provider_inventory.apply(account_fleet.load_accounts(account_fleet.reference_fleet_path()), snapshot)
        self.assertEqual(set(accounts['accounts']['grok-bot']['windows']), {'credits'})
        cache = self.root / 'shared-usage.json'
        reset = (now + timedelta(days=2)).isoformat()
        for used, stale, missing, blocked in ((100, False, False, True), (56, False, False, False),
                (100, True, False, True), (None, False, False, False), (100, False, True, False)):
            with self.subTest(used=used, stale=stale, missing=missing):
                grok = {'available': True, 'stale': stale, 'fetched_at': now.isoformat(),
                        'windows': [{'id': 'credits', 'used_pct': used, 'resets_at': reset}]}
                # A conflicting retired cache must never override the shared source.
                providers = {'grok-bot': {**grok, 'windows': [{'id': 'weekly', 'used_pct': 0, 'resets_at': reset}]}}
                if not missing:
                    providers['grok'] = grok
                cache.write_text(json.dumps({'providers': providers}))
                usage = route_judge.usage_snapshot(cache, accounts=['grok-bot'], now=now)
                # No public source gives the credits window a capacity, so the bot runs only when it is named.
                plan = allocator.plan({'work_kind': 'text_in_text_out', 'difficulty': 'routine', 'expected_hours': .2,
                                       'constraints': {'model': 'grok-bot'}},
                                      usage, now, accounts=accounts, records=[], overrides=[],
                                      host_policy_config={'allowed_accounts': ['grok-bot']})
                candidate = next(c for c in plan['ranking'] if c['id'] == 'grok_bot')
                self.assertEqual(bool(candidate['excluded']), blocked)
                self.assertEqual(plan['choice'] == 'grok_bot', not blocked)
                bot = plan['usage']['accounts']['grok-bot']
                self.assertEqual(bot['windows'], plan['usage']['accounts']['grok']['windows'])
                self.assertEqual(bot['status'], plan['usage']['accounts']['grok']['status'])
                self.assertGreater(candidate['expected_usd'], 0)
                if blocked and not stale:
                    with self.assertRaisesRegex(ValueError, 'credits.*launch protection'):
                        dispatch_registry.check_launch_quota(candidate['bundle'], accounts, usage, now)
                else:
                    dispatch_registry.check_launch_quota(candidate['bundle'], accounts, usage, now)

    def test_shared_capacity_and_reservations_keep_execution_enablement_separate(self):
        now = datetime.now(timezone.utc)
        snapshot = {'accounts': {
            'grok': {'family': 'grok', 'status': 'ready', 'usage_windows': ['credits']},
            'grok-bot': {'family': 'grok-bot', 'status': 'ready'}}, 'launchers': {}}
        accounts = provider_inventory.apply(account_fleet.load_accounts(account_fleet.reference_fleet_path()), snapshot)
        source = accounts['accounts']['grok']
        source['observed'] = {'credits': {'pct_per_hour_24h': 2.0}}
        source['capacity_usd'] = {'credits': 200}
        provider_inventory.apply_shared_quota(accounts)
        bot = accounts['accounts']['grok-bot']
        for key in ('windows', 'observed', 'capacity_usd'):
            self.assertEqual(bot[key], source[key])
        records = [{'stage': 'dispatch', 'dispatch_id': name, 'state': 'started',
                    'lease_until': (now + timedelta(hours=1)).isoformat(),
                    'bundle': {'account': name, 'model': 'grok-bot' if name == 'grok-bot' else 'grok-4.7'},
                    'expected_usd': 10} for name in ('grok', 'grok-bot', 'codex')]
        for account in ('grok', 'grok-bot'):
            reserved = quota_pools.inflight_reserved_usd(account, accounts['accounts'][account],
                'credits', {}, {}, 200, records + records, accounts, accounts['scoring'], now)
            self.assertEqual(reserved, 20)  # Shared reservations counted once, other pools excluded.
        # Canonical execution identities stay independent (including an explicit Grok disable).
        self.assertEqual(account_fleet.bundles(accounts)['grok_bot']['account'], 'grok-bot')
        source['bundles']['grok47_high']['disabled'] = True
        self.assertFalse(bot['bundles']['grok_bot'].get('disabled', False))

    def test_missing_receipt_foreign_or_unrecognized_thread_cannot_complete(self):
        task = self.accepted()
        for data in ({}, {'target': {'id': 'foreign'}, 'thread': {'entries': [self.reply(task)]}},
                     {'target': {'id': 'bot-1'}, 'thread': {}}):
            with self.subTest(data=data), patch.object(runner, 'gbot', return_value=data):
                view = runner.status(task['task_id'])
                self.assertEqual(view['state'], 'accepted')
                self.assertIn('read_error', view)
        with self.assertRaises(ValueError):
            runner.status('../config')

    def test_cli_argv_is_not_a_shell_and_does_not_leak_errors_or_auto_bind_codex(self):
        fake_cli = self.root / 'gbot.mjs'
        fake_cli.touch()
        with patch.object(runner, 'CLI', fake_cli), patch.object(runner.shutil, 'which', return_value='node'), \
                patch.dict(runner.os.environ, {'CODEX_THREAD_ID': 'caller-secret'}), \
                patch.object(runner, '_run_cli', return_value=subprocess.CompletedProcess([], 0, '[]', '')) as run:
            runner.gbot('send', '--no-history', '--', 'bot-1', 'a\n$() "b"')
            argv = run.call_args.args[0]
            self.assertEqual(argv[-2:], ['bot-1', 'a\n$() "b"'])
            self.assertLess(argv.index('--json'), argv.index('--'))
            self.assertNotIn('CODEX_THREAD_ID', run.call_args.kwargs['env'])
            self.assertNotIn('shell', run.call_args.kwargs)
            run.return_value = subprocess.CompletedProcess([], 1, '{"error":"TOKEN-SECRET"}', 'TOKEN-SECRET')
            with self.assertRaises(runner.TransportError) as error:
                runner.gbot('bots', 'list')
            self.assertNotIn('TOKEN-SECRET', str(error.exception))
            run.return_value = subprocess.CompletedProcess([], 1, '', json.dumps({
                'error': {'code': 'render-failed', 'message': 'Set CURSOR_ACCESS_TOKEN, or gateway session.'}}))
            with self.assertRaises(runner.TransportError) as error:
                runner.gbot('bots', 'list')
            # An actionable public troubleshooting link is contractual; shortcut prose is not.
            self.assertIn('/issues/147', str(error.exception))
            self.assertNotIn('CURSOR_ACCESS_TOKEN', str(error.exception))

    def test_doctor_preserves_only_controlled_http_diagnostic(self):
        import io
        from contextlib import redirect_stdout
        fake_cli = self.root / 'gbot.mjs'
        fake_cli.touch()
        for message, diagnostic in [
            ('listAgents failed: 417 Expectation Failed synthetic-secret https://private.invalid',
             {'category': 'http_error', 'operation': 'listAgents', 'status': 417}),
            ('listAgents failed: 503 synthetic-secret',
             {'category': 'http_error', 'operation': 'listAgents', 'status': 503}),
            ('EnsureSandBox failed: 401 synthetic-secret',
             {'category': 'http_error', 'operation': 'EnsureSandBox', 'status': 401}),
            ('synthetic-secret listAgents failed: 417', None),
        ]:
            with self.subTest(message=message), patch.object(runner, 'CLI', fake_cli), \
                    patch.object(runner.shutil, 'which', return_value='node'), \
                    patch.object(sys, 'argv', ['run_grok_bot.py', 'doctor']), \
                    patch.object(runner, '_run_cli', return_value=subprocess.CompletedProcess(
                        [], 1, '', json.dumps({'error': {'code': 'render-failed', 'message': message}}))):
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(runner.main(), 1)
                text = out.getvalue()
                self.assertNotIn('synthetic-secret', text)
                self.assertNotIn('private.invalid', text)
                self.assertEqual(json.loads(text).get('diagnostic'), diagnostic)

    @pytest.mark.slow
    @unittest.skipUnless(runner.os.name == 'nt', 'Windows process-tree contract')
    def test_cli_timeout_cleans_owned_tree_and_remains_uncertain(self):
        import time
        import win32api
        import win32event
        import win32process
        pidfile = self.root / 'synthetic-child.pid'
        child = "import time; time.sleep(60)"
        parent = ("import subprocess,sys,pathlib; "
                  "p=subprocess.Popen([sys.executable,'-S','-c'," + repr(child) + "], "
                  "stdout=sys.stdout,stderr=sys.stderr,creationflags=0x08000000); "
                  "f=pathlib.Path(sys.argv[1]); t=f.with_suffix('.tmp'); "
                  "t.write_text(str(p.pid)); t.replace(f)")
        handles = []
        def observe():
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if pidfile.exists():
                    try:
                        handles.append(win32api.OpenProcess(0x00100000 | 0x1000 | 1, False, int(pidfile.read_text())))
                        return
                    except (ValueError, OSError):
                        pass
                time.sleep(0.005)
        watcher = threading.Thread(target=observe)
        watcher.start()
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                runner._run_cli([sys.executable, '-S', '-c', parent, str(pidfile)],
                                timeout=2.5, env=runner.os.environ.copy())
            watcher.join(8)
            self.assertFalse(watcher.is_alive())
            self.assertEqual(len(handles), 1)
            self.assertEqual(win32event.WaitForSingleObject(handles[0], 0), win32event.WAIT_OBJECT_0)
            self.assertNotEqual(win32process.GetExitCodeProcess(handles[0]), 259)
        finally:
            watcher.join(8)
            for handle in handles:
                if win32event.WaitForSingleObject(handle, 0) == win32event.WAIT_TIMEOUT:
                    win32api.TerminateProcess(handle, 1)
                    win32event.WaitForSingleObject(handle, 5000)
                handle.Close()
        fake_cli = self.root / 'gbot.mjs'
        fake_cli.touch()
        with patch.object(runner, 'CLI', fake_cli), patch.object(runner.shutil, 'which', return_value='node'), \
                patch.object(runner, '_run_cli', side_effect=subprocess.TimeoutExpired('fixture', runner.CLI_TIMEOUT)):
            with self.assertRaises(runner.TransportError) as error:
                runner.gbot('send', '--', 'fixture-bot', 'fixture prompt')
            self.assertEqual(error.exception.delivery, 'unknown')

    @pytest.mark.slow
    @unittest.skipUnless(runner.os.name == 'nt', 'Windows Job assignment')
    def test_job_assignment_failure_does_not_execute_cli(self):
        import win32job
        marker = self.root / 'should-not-exist'
        command = [sys.executable, '-S', '-c', 'import pathlib,sys; pathlib.Path(sys.argv[1]).touch()', str(marker)]
        with patch.object(win32job, 'AssignProcessToJobObject', side_effect=RuntimeError('fixture assignment failure')):
            with self.assertRaises(runner.TransportError) as error:
                runner._run_cli(command, timeout=2, env=runner.os.environ.copy())
            self.assertEqual(error.exception.delivery, 'rejected')
            self.assertEqual(error.exception.diagnostic['component'], 'process_ownership')
        self.assertFalse(marker.exists())

    @unittest.skipUnless(runner.os.name == 'nt', 'Windows runtime diagnostic')
    def test_missing_job_runtime_is_reported_before_any_cli_launch(self):
        import builtins
        original_import = builtins.__import__
        def missing(name, *args, **kwargs):
            if name == 'win32job':
                raise ImportError('synthetic-private-import-detail')
            return original_import(name, *args, **kwargs)
        fake_cli = self.root / 'gbot.mjs'
        fake_cli.touch()
        with patch.object(runner, 'CLI', fake_cli), patch.object(runner.shutil, 'which', return_value='node'), \
                patch.object(builtins, '__import__', side_effect=missing), \
                patch.object(runner.subprocess, 'Popen') as launch:
            with self.assertRaises(runner.TransportError) as error:
                runner.gbot('bots', 'list')
            launch.assert_not_called()
            self.assertEqual(error.exception.delivery, 'rejected')
            self.assertEqual(error.exception.diagnostic, {'category': 'local_runtime', 'component': 'pywin32'})
            self.assertIn('pywin32', str(error.exception))
            self.assertNotIn('Node', str(error.exception))
            self.assertNotIn('synthetic-private-import-detail', str(error.exception))

    def test_configure_uses_exact_live_bot_and_writes_only_identifier(self):
        with patch.object(runner, 'gbot', return_value=[{'id': 'bot-2', 'name': 'Fixture', 'private': 'secret'}]):
            with self.assertRaises(ValueError):
                runner.configure('Fixture')
            runner.configure('bot-2')
        self.assertEqual(json.loads((runner.STATE / 'config.json').read_text()), {'bot_id': 'bot-2'})

    def test_discovery_routes_remote_text_automatically_without_replacing_grok(self):
        home = self.root / 'home'
        home.mkdir()
        cache = self.root / 'usage-cache.json'
        cache.write_text(json.dumps({'providers': {'grok': {'windows': [{'id': 'credits'}]}}}))
        env = {'PATH': '', 'ROUTER_STATE_DIR': str(self.root), 'ROUTER_USAGE_CACHE': str(cache)}
        fake_cli = self.root / 'gbot.mjs'
        fake_cli.touch()
        with patch.object(provider_inventory, 'GROK_BOT_CLI', fake_cli), \
                patch.object(provider_inventory.shutil, 'which', return_value='node'):
            snapshot = provider_inventory.discover(home, env)
        row = snapshot['accounts']['grok-bot']
        self.assertEqual((row['status'], row['billing'], row['execution'], row['dispatch']),
                         ('ready', 'unknown', 'remote', 'automatic'))
        self.assertIn('grok', snapshot['accounts'])
        applied = provider_inventory.apply(account_fleet.load_accounts(account_fleet.reference_fleet_path()), snapshot)
        bundle = account_fleet.bundles(applied)['grok_bot']
        self.assertEqual((bundle['provider'], bundle['account'], bundle['model'], bundle['effort']),
                         ('grok-bot', 'grok-bot', 'grok-bot', None))
        task = {'objective': 'Rewrite this supplied paragraph as concise prose.', 'work_kind': 'text_in_text_out',
                'difficulty': 'routine', 'expected_hours': 0.2}
        def plan(needs=None, allowed=True, usage=None, model=None):
            return allocator.plan({**task, 'needs': needs or {}, **({'constraints': {'model': model}} if model else {})},
                                  usage or {'accounts': {}},
                                  accounts=applied, records=[], overrides=[],
                                  host_policy_config={'allowed_accounts': ['grok-bot'] if allowed else []})
        # Without a detected Grok subscription tier, the bot is out of automatic routing with a surfaced Mode-B
        # plan-unknown error, while a named dispatch still reaches it.
        unnamed = plan()
        self.assertEqual(unnamed['choice'], 'none')
        excluded_text = ' '.join(r for c in unnamed['ranking'] if c['id'] == 'grok_bot' for r in c['excluded'])
        self.assertIn('capacity_unknown', excluded_text)
        self.assertIn('plan unknown', excluded_text)
        named = plan(model='grok-bot')
        self.assertEqual(named['choice'], 'grok_bot')
        candidate = next(c for c in named['ranking'] if c['id'] == 'grok_bot')
        self.assertGreater(candidate['expected_usd'], 0)
        self.assertIsNone(candidate['expected_pct'])
        self.assertIn('capacity_unknown', ' '.join(candidate['notes']))
        self.assertIn('plan unknown', ' '.join(candidate['notes']))
        for needs, reason in [({'local_only': True}, 'local-only'), ({'file_transfer': True}, 'local files'),
                              ({'outbound_ok': False}, 'outbound')]:
            with self.subTest(needs=needs):
                rejected = next(c for c in plan(needs)['ranking'] if c['id'] == 'grok_bot')
                self.assertTrue(any(reason in r for r in rejected['excluded']), rejected['excluded'])
        # grok-bot carries a dated maintainer declaration that it reads images: visual tasks are not excluded on modality
        visual_row = next(c for c in plan({'visual': True})['ranking'] if c['id'] == 'grok_bot')
        self.assertFalse(any('image_input' in r for r in visual_row['excluded']), visual_row['excluded'])
        self.assertTrue(any('host policy' in r for c in plan(allowed=False)['ranking']
                            if c['id'] == 'grok_bot' for r in c['excluded']))
        applied['accounts']['grok-bot']['bundles']['grok_bot']['disabled'] = 'user disabled'
        self.assertNotEqual(plan(model='grok-bot')['choice'], 'grok_bot')      # the checkbox beats naming the model
        applied['accounts']['grok-bot']['bundles']['grok_bot'].pop('disabled')
        now = datetime.now(timezone.utc)
        weekly = {'id': 'credits', 'remaining_pct_before_reset': 1,
                  'resets_at': (now + timedelta(days=2)).isoformat(), 'seconds_until_reset': 172800}
        observation = {'status': 'fresh', 'fetched_at': now.isoformat(), 'windows': [weekly]}
        depleted = plan(usage={'accounts': {'grok': observation}})
        self.assertTrue(any('credits' in r for c in depleted['ranking']
                            if c['id'] == 'grok_bot' for r in c['excluded']))
        stale = {**observation, 'status': 'stale', 'stale': True,
                 'windows': [{**weekly, 'remaining_pct_before_reset': None, 'observed_remaining_pct': 1}]}
        self.assertEqual(plan(usage={'accounts': {'grok': stale}})['choice'], 'none')
        (runner.STATE / 'config.json').unlink()
        snapshot = provider_inventory.discover(home, env)
        self.assertEqual(snapshot['accounts']['grok-bot']['status'], 'missing_configuration')

    def test_detected_plan_runtime_rules_scarcity_exhaustion_and_unknown_tier_exclusion(self):
        import axis_scores
        now = datetime(2026, 11, 12, 4, tzinfo=timezone.utc)
        home = self.root / 'tier-home'
        (home / '.grok').mkdir(parents=True, exist_ok=True)
        (home / '.grok' / 'auth.json').write_text(json.dumps({
            'https://auth.x.ai::client': {'key': 'tok-1', 'expires_at': (now + timedelta(hours=2)).isoformat()}
        }))
        fake_cli = self.root / 'tier-gbot.mjs'
        fake_cli.touch()
        (runner.STATE / 'config.json').write_text(json.dumps({'bot_id': 'bot-2'}))
        cache_path = self.root / 'tier-usage.json'
        env = {'PATH': '', 'ROUTER_STATE_DIR': str(self.root), 'ROUTER_USAGE_CACHE': str(cache_path)}
        reset_iso = (now + timedelta(days=5)).isoformat()
        task = {
            'objective': 'Draft a multi-perspective architectural trade-off memo',
            'expected_hours': 1.0,
            'needs': {'local_only': False},
            'profile': {
                'work_kind': 'text_in_text_out', 'reasoning_depth': 3, 'code_complexity': 0,
                'needs_visual': False, 'needs_gui': False, 'local_only': False, 'wants_fast': False,
                'read_only_review': False, 'access': 'read-only', 'expected_hours': 1.0,
                'hours_confidence': 'medium', 'urgency': 'normal', 'interaction_style': 'exploration',
                'multi_task': False, 'task_count': '1',
                'axis_demand': {'divergence': 2, 'coding': 0, 'aesthetics': 1, 'tool_operation': 0},
            },
        }

        def evaluate_grok_bot(plan_label, used_pct, tier_error=None, *, host_policy_cfg=None, read_local=False):
            grok_row = {
                'provider_id': 'grok', 'available': True, 'stale': False, 'fetched_at': now.isoformat(),
                'plan_label': plan_label, 'plan_source': 'settings_api' if plan_label else None,
                'windows': [{'id': 'credits', 'used_pct': used_pct, 'resets_at': reset_iso}],
            }
            if tier_error:
                grok_row['usage_details'] = {'plan_tier_error': tier_error}
            cache_path.write_text(json.dumps({'providers': {
                'grok': grok_row,
                'claude': {
                    'provider_id': 'claude', 'available': True, 'stale': False, 'fetched_at': now.isoformat(),
                    'plan_label': 'pro', 'plan_source': 'profile_api',
                    'windows': [
                        {'id': 'five_hour', 'used_pct': 10.0, 'resets_at': (now + timedelta(hours=4)).isoformat()},
                        {'id': 'seven_day', 'used_pct': 10.0, 'resets_at': reset_iso},
                    ],
                },
            }}))
            with patch.object(provider_inventory, 'GROK_BOT_CLI', fake_cli), \
                    patch.object(provider_inventory.shutil, 'which', return_value='node'):
                snap = provider_inventory.discover(home, env, now=now)
            # Keep an outside alternative ('claude') active so opportunity_cost prices against a positive-fit rival.
            snap['accounts']['claude'] = {
                'family': 'claude', 'status': 'ready', 'billing': 'subscription',
                'plan': 'pro', 'plan_source': 'profile_api', 'usage_binding': 'matches',
                'credential_key': 'c1', 'identity': 'c1',
            }
            fleet = provider_inventory.apply(account_fleet.load_accounts(account_fleet.reference_fleet_path()), snap)
            usage = route_judge.usage_snapshot(cache_path, accounts=['grok-bot', 'claude'], now=now)
            res = allocator.plan(task, usage, now=now, accounts=fleet, records=[], overrides=[],
                                 host_policy_config=host_policy_cfg, read_local_state=read_local)
            return next(c for c in res['ranking'] if c['id'] == 'grok_bot'), fleet

        # Rule: undetectable tier -> surfaced Mode-B exclusion naming unknown plan and the collector's reason.
        unknown_cand, _ = evaluate_grok_bot(None, 4.0, tier_error='settings HTTP 500; user HTTP 503')
        unknown_excl = ' '.join(unknown_cand['excluded'])
        self.assertIn('capacity_unknown', unknown_excl)
        self.assertIn('plan unknown', unknown_excl)
        self.assertIn('settings HTTP 500', unknown_excl)

        # Rule: detected tier -> eligible in public defaults even when local host-policy excludes grok-bot on disk;
        # usd/h equals Opus 5.5's; nearest-Claude-plan scaling evidence is surfaced; higher used_pct -> higher opportunity cost.
        local_policy = self.root / 'host-policy.json'
        local_policy.write_text(json.dumps({'allowed_accounts': ['claude']}))
        with patch.object(allocator.host_policy, 'POLICY_PATH', local_policy, create=True):
            low_cand, low_fleet = evaluate_grok_bot('SuperGrok', 5.0, host_policy_cfg=None, read_local=False)
            high_cand, _ = evaluate_grok_bot('SuperGrok', 85.0, host_policy_cfg=None, read_local=False)
        self.assertEqual(low_cand['excluded'], [])
        self.assertEqual(high_cand['excluded'], [])
        overlaid = axis_scores.five_axis_overlay(low_fleet)
        self.assertAlmostEqual(low_cand['five_axis_price']['usd_per_hour'],
                               overlaid['models']['claude-opus-5-5']['usd_per_hour'], places=3)
        self.assertIn('nearest plan by price is claude/pro', ' '.join(low_cand['notes']))
        self.assertGreater(high_cand['components']['opportunity_cost'], low_cand['components']['opportunity_cost'])

        # Rule: exhausted credits window excludes grok-bot.
        exhausted_cand, _ = evaluate_grok_bot('SuperGrok', 99.0, host_policy_cfg=None, read_local=False)
        self.assertTrue(any('credits' in r for r in exhausted_cand['excluded']), exhausted_cand['excluded'])

    def test_roll_delivery_preserves_receipts_without_replay(self):
        prompt = self.root / 'prompt.md'
        prompt.write_text('Task')
        bundle = {'provider': 'grok-bot', 'account': 'grok-bot', 'model': 'grok-bot'}
        cases = [
            ('accepted', 'accepted', True),
            ('unknown', 'unknown', None),
            ('rejected', 'failed', False),
        ]
        for delivery, state, launched in cases:
            receipt = {'task_id': 'a' * 32, 'state': state, 'delivery': delivery}
            args = SimpleNamespace(file=[], apply_to=None, local_only=None)
            with self.subTest(delivery=delivery), \
                    patch.object(runner, 'submit', return_value=receipt) as submit:
                result, code = roll.grok_bot(args, {'parent_id': 'parent'}, prompt, {'task_id': 'task'}, bundle)
            submit.assert_called_once()
            self.assertIs(result['launched'], launched)
            self.assertEqual(result['dispatch_mode'], 'grok-bot')
            self.assertEqual(code, 1 if delivery == 'rejected' else 0)
            self.assertEqual('continuation_argv' in result, delivery != 'rejected')
            self.assertNotIn('read_error', receipt)
        for option, value in [('file', [prompt]), ('local_only', 'local dependency'), ('apply_to', self.root)]:
            with self.subTest(option=option), patch.object(runner, 'submit', side_effect=AssertionError('no send')):
                with self.assertRaises(ValueError):
                    roll.grok_bot(SimpleNamespace(**{**vars(args), option: value}), {}, prompt, {}, bundle)

    @pytest.mark.slow
    def test_production_roll_records_known_contract_before_mocked_external_send(self):
        brief = self.root / 'brief.md'
        brief.write_text('Reply with the fixture marker. Do not use tools.')
        host_policy = self.root / 'host-policy.json'
        host_policy.write_text(json.dumps({'allowed_accounts': ['codex'], 'known_accounts': ['codex']}))
        usage = self.root / 'usage.json'
        usage.write_text('{"providers": {}}')
        env = {key: value for key, value in runner.os.environ.items()
               if key.upper() in ('PATH', 'SYSTEMROOT', 'TEMP', 'TMP')}
        env.update(USERPROFILE=str(self.root), ROUTER_STATE_DIR=str(self.root),
                   ROUTER_PROVIDER_HOME=str(self.root), PASEO_HOME=str(self.root / 'paseo'),
                   ROUTER_HOST_POLICY=str(host_policy), ROUTER_OVERRIDES=str(self.root / 'overrides.json'),
                   ROUTER_DISPATCH_DB=str(self.root / 'dispatch.sqlite3'),
                   ROUTER_LEDGER=str(self.root / 'ledger.jsonl'),
                   ROUTER_CAPACITY=str(self.root / 'capacity.json'),
                   DOT2API_BASE_URL='http://127.0.0.1:1',
                   # The fixture fleet declares the Grok pool; discovery reads Grok's meter from this cache.
                   ROUTER_ACCOUNTS_FILE=str(account_fleet.reference_fleet_path()), ROUTER_USAGE_CACHE=str(usage),
                   HOME=str(self.root), ROUTER_DISABLE_LIVE_SCORER='1', PASEO_AGENT_ID='fixture-parent')
        script = '''
import json, sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv.pop(1))
import route_judge
import run_grok_bot as runner
import provider_inventory as inventory
fake_cli = Path(sys.argv.pop(1))
real_which = runner.shutil.which
def fixture_which(name, *args, **kwargs):
    return 'fixture-node' if name == 'node' else real_which(name, *args, **kwargs)

def accept(text, parent, task):
    return {'provider': 'grok-bot', 'task_id': 'a' * 32, 'state': 'accepted',
            'delivery': 'accepted', 'message_id': 'fixture-message', 'mocked_send': True}
from router_test_support import profile
fixture_profile = profile(work_kind='web_research', reasoning_depth=2, code_complexity=0, interaction_style='exploration',
                          reason='fixture', access='read-only', axis_demand={'divergence': 1, 'coding': 1, 'aesthetics': 0, 'tool_operation': 0})
with patch.object(runner, 'submit', side_effect=accept), patch.object(
        inventory, 'GROK_BOT_CLI', fake_cli), patch.object(runner.shutil, 'which', side_effect=fixture_which), patch.object(
        route_judge, 'score_task', return_value=(fixture_profile, {'source': 'system_one', 'model': 'fixture-scorer'})), patch.object(
        route_judge.dispatch_flow, 'launch', side_effect=AssertionError('unexpected external provider launch')):
    raise SystemExit(route_judge.main())
'''
        fake_cli = self.root / 'gbot.mjs'
        fake_cli.touch()
        command = [sys.executable, '-X', 'utf8', '-c', script, str(Path(roll.__file__).parent), str(fake_cli),
                   'roll', '--brief', str(brief), '--hours', '0.02', '--provider', 'grok-bot',
                   '--account', 'grok-bot', '--model', 'grok-bot', '--basis', 'Fixture explicit Bot request',
                   '--usage-cache', str(usage), '--verbose']
        dry = subprocess.run(command + ['--dry-run'], capture_output=True, text=True, encoding='utf-8', env=env, timeout=30)
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        result = json.loads(dry.stdout)
        self.assertEqual(result['dispatch_mode'], 'dry_run')
        self.assertEqual(result['route']['bundle']['provider'], 'grok-bot')
        self.assertFalse((runner.STATE / 'tasks').exists())
        proc = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', env=env, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual((result['dispatch_mode'], result['launched']), ('grok-bot', True))
        self.assertTrue(result['worker']['mocked_send'])
        self.assertIn('grok-bot', json.loads(host_policy.read_text())['allowed_accounts'])
        with closing(sqlite3.connect(env['ROUTER_DISPATCH_DB'])) as connection:
            decisions = [json.loads(row[0]) for row in connection.execute('SELECT data FROM decisions')]
        self.assertEqual(len(decisions), 1)
        self.assertTrue(decisions[0]['bound'])
        self.assertTrue(decisions[0]['dispatchable'])
        self.assertEqual(decisions[0]['bundle']['bundle_id'], 'grok_bot')
        self.assertIsNone(decisions[0]['bundle']['effort'])
        ledger = json.loads(Path(env['ROUTER_LEDGER']).read_text().splitlines()[-1])
        self.assertGreater(ledger.get('expected_usd'), 0)
        # Ordinary automatic roll uses the same record/registry path, with only Bot transport mocked.
        host_policy.write_text(json.dumps({'allowed_accounts': ['grok-bot']}))
        automatic = list(command)
        for flag in ('--provider', '--account', '--model', '--basis'):
            index = automatic.index(flag)
            del automatic[index:index + 2]
        automatic[automatic.index('--hours') + 1] = '0.2'
        blocked_no_score = subprocess.run(automatic + ['--no-score', '--task-id', 'fixture-no-score'],
                                          capture_output=True, text=True, encoding='utf-8', env=env, timeout=30)
        self.assertEqual(blocked_no_score.returncode, 2, blocked_no_score.stdout + blocked_no_score.stderr)
        self.assertEqual(json.loads(blocked_no_score.stdout)['status'], 'blocked')
        # No public source gives the Grok credits window a capacity, so the bot is out of automatic routing (a surfaced
        # data error) and runs only when it is named.
        automatic += ['--task-id', 'fixture-automatic']
        proc = subprocess.run(automatic, capture_output=True, text=True, encoding='utf-8', env=env, timeout=30)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        auto_result = json.loads(proc.stdout)
        self.assertEqual(auto_result['route']['choice'], 'none')
        self.assertNotIn('mocked_send', proc.stdout)
        for incompatible in (['--file', str(brief)], ['--apply-to', str(self.root)],
                             ['--local-only', 'local git repository and pytest environment']):
            with self.subTest(incompatible=incompatible):
                proc = subprocess.run(automatic + incompatible, capture_output=True, text=True,
                                      encoding='utf-8', env=env, timeout=30)
                self.assertNotIn('mocked_send', proc.stdout)
                self.assertFalse(json.loads(proc.stdout)['launched'])
        # Shared Grok admission governs the explicit record path. The real transport remains mocked throughout.
        for used, stale, blocked in ((100, False, True), (20, False, False), (100, True, False), (None, False, False)):
            usage.write_text(json.dumps({'providers': {'grok': {
                'available': True, 'stale': stale, 'fetched_at': datetime.now(timezone.utc).isoformat(),
                'windows': [{'id': 'credits', 'used_pct': used,
                             'resets_at': (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()}]}}}))
            with self.subTest(used=used, stale=stale):
                proc = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', env=env, timeout=30)
                if blocked:
                    self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                    self.assertNotIn('mocked_send', proc.stdout)
                    self.assertIn('credits', proc.stdout)
                else:
                    self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                    self.assertTrue(json.loads(proc.stdout)['worker']['mocked_send'])
        # The same production resolver must still reject unsupported identities/efforts.
        for flag, value in [('--model', 'grok-4.7'), ('--provider', 'unknown-bot'), ('--effort', 'high')]:
            args = list(command)
            if flag in args:
                args[args.index(flag) + 1] = value
            else:
                args += [flag, value]
            with self.subTest(flag=flag):
                proc = subprocess.run(args, capture_output=True, text=True, encoding='utf-8', env=env, timeout=30)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn('no known provider/account/model/effort contract', proc.stdout)
                self.assertNotIn('mocked_send', proc.stdout)
        host_policy.write_text(json.dumps({'allowed_accounts': []}))
        proc = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', env=env, timeout=30)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        blocked = json.loads(proc.stdout)
        self.assertFalse(blocked['launched'])
        self.assertEqual(blocked['caller_action'], 'check_host_policy')
        self.assertIn('excluded by host policy', blocked['route']['reason'])
        self.assertEqual(blocked['host_policy_violation']['account'], 'grok-bot')
        self.assertEqual(blocked['host_policy_violation']['policy_path'], str(host_policy))
        self.assertIn(str(host_policy), blocked['next'])
        self.assertNotIn('mocked_send', proc.stdout)



    @unittest.skipUnless(runner.CLI.is_file() and runner.shutil.which('node'), 'Pinned artifact installation and Node required')
    def test_pinned_upstream_cli_against_loopback_gateway(self):
        """Exercise the shipped CLI parser/JSON/schema, with upstream test mode denying live hosts."""
        calls = []
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append((self.path, payload))
                if self.path == '/api/listAgents':
                    value = {'agents': [{'id': 'bot-1', 'name': 'Fixture'}]}
                elif self.path == '/api/sendPrompt':
                    value = {'messageId': 'message-1'}
                elif self.path == '/api/getAgentThread':
                    receipt = json.loads(next((runner.STATE / 'tasks').glob('*.json')).read_text())
                    value = {'entries': [test.reply(receipt)]}
                else:
                    self.send_error(404)
                    return
                data = json.dumps(value).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.dict(runner.os.environ, {
                **{key: value for key, value in runner.os.environ.items()
                   if key.upper() in ('PATH', 'SYSTEMROOT', 'TEMP', 'TMP')},
                'GROK_BOT_TEST': '1', 'GROK_BOT_GATEWAY_URL': f'http://127.0.0.1:{server.server_port}',
                'GROK_BOT_GATEWAY_TOKEN': 'fixture-token',
            }, clear=True):
                receipt = runner.submit('Reply with a marker; no tools.', 'fixture-parent', 'fixture-task')
                self.assertEqual((receipt['state'], receipt.get('message_id')), ('accepted', 'message-1'))
                self.assertEqual(runner.status(receipt['task_id'])['reply'], 'Answer')
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()
        sends = [body for path, body in calls if path == '/api/sendPrompt']
        self.assertEqual(len(sends), 1)
        self.assertIn('clientNonce', sends[0])
        self.assertIn(runner.marker(receipt['task_id'], 'completed'), sends[0]['prompt'])
        self.assertEqual([body for path, body in calls if path == '/api/getAgentThread'],
                         [{'id': 'bot-1', 'rootId': 'message-1'}])


if __name__ == '__main__':
    unittest.main()
