"""Grok Bot adapter boundaries: upstream CLI, receipt recovery, correlation and routing."""
import json
from datetime import datetime, timedelta, timezone
from contextlib import closing
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import provider_inventory
import allocator
import dispatch_registry
import route_judge
import roll
import run_grok_bot as runner


class GrokBotTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='grok-bot-test-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
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

    def test_native_trial_admission_uses_observation_without_inventing_a_cycle(self):
        now = datetime.now(timezone.utc)
        snapshot = {'accounts': {'grok-bot': {'family': 'grok-bot', 'status': 'ready'}},
                    'launchers': {'grok-bot': {'family': 'grok-bot', 'status': 'ready'}}}
        accounts = provider_inventory.apply(allocator.load_accounts(allocator.REFERENCES / 'accounts.json'), snapshot)
        self.assertNotIn('hours', accounts['accounts']['grok-bot']['windows']['trial'])
        cache = self.root / 'trial-usage.json'
        for reset in (None, (now + timedelta(days=2)).isoformat()):
            for used, stale, blocked in ((100, False, True), (20, False, False),
                                         (100, True, False), (None, False, False)):
                with self.subTest(reset=reset, used=used, stale=stale):
                    cache.write_text(json.dumps({'providers': {'grok-bot': {
                        'available': True, 'stale': stale, 'fetched_at': now.isoformat(),
                        'windows': [{'id': 'trial', 'used_pct': used, 'resets_at': reset}]}}}))
                    usage = route_judge.usage_snapshot(cache, now=now)
                    plan = allocator.plan({'work_kind': 'text_in_text_out', 'difficulty': 'routine', 'expected_hours': .2},
                                          usage, now, accounts=accounts, records=[], overrides=[],
                                          host_policy_config={'allowed_accounts': ['grok-bot']})
                    candidate = next(c for c in plan['ranking'] if c['id'] == 'grok_bot')
                    self.assertEqual(bool(candidate['excluded']), blocked)
                    self.assertEqual(plan['choice'] == 'grok_bot', not blocked)
                    self.assertIsNone(candidate['expected_usd'])
                    self.assertIsNone(candidate['expected_pct'])
                    self.assertEqual(usage['accounts']['grok-bot']['windows'][0]['resets_at'], reset)
                    if blocked:
                        with self.assertRaisesRegex(ValueError, 'trial.*launch protection'):
                            dispatch_registry.check_launch_quota(candidate['bundle'], accounts, usage, now)
                    else:
                        dispatch_registry.check_launch_quota(candidate['bundle'], accounts, usage, now)

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
                patch.object(runner.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '[]', '')) as run:
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
            self.assertIn('keep it awake', str(error.exception))
            self.assertIn('Ctrl+Q', str(error.exception))
            self.assertIn('/issues/147', str(error.exception))

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
            ('synthetic-secret listAgents failed: 417', None),
        ]:
            with self.subTest(message=message), patch.object(runner, 'CLI', fake_cli), \
                    patch.object(runner.shutil, 'which', return_value='node'), \
                    patch.object(sys, 'argv', ['run_grok_bot.py', 'doctor']), \
                    patch.object(runner.subprocess, 'run', return_value=subprocess.CompletedProcess(
                        [], 1, '', json.dumps({'error': {'code': 'render-failed', 'message': message}}))):
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(runner.main(), 1)
                text = out.getvalue()
                self.assertNotIn('synthetic-secret', text)
                self.assertNotIn('private.invalid', text)
                self.assertEqual(json.loads(text).get('diagnostic'), diagnostic)

    def test_configure_uses_exact_live_bot_and_writes_only_identifier(self):
        with patch.object(runner, 'gbot', return_value=[{'id': 'bot-2', 'name': 'Fixture', 'private': 'secret'}]):
            with self.assertRaises(ValueError):
                runner.configure('Fixture')
            runner.configure('bot-2')
        self.assertEqual(json.loads((runner.STATE / 'config.json').read_text()), {'bot_id': 'bot-2'})

    def test_discovery_routes_remote_text_automatically_without_replacing_grok(self):
        home = self.root / 'home'
        home.mkdir()
        env = {'PATH': '', 'ROUTER_STATE_DIR': str(self.root)}
        fake_cli = self.root / 'gbot.mjs'
        fake_cli.touch()
        with patch.object(provider_inventory, 'GROK_BOT_CLI', fake_cli), \
                patch.object(provider_inventory.shutil, 'which', return_value='node'):
            snapshot = provider_inventory.discover(home, env)
        row = snapshot['accounts']['grok-bot']
        self.assertEqual((row['status'], row['billing'], row['execution'], row['dispatch']),
                         ('ready', 'unknown', 'remote', 'automatic'))
        self.assertIn('grok', snapshot['accounts'])
        applied = provider_inventory.apply(allocator.load_accounts(allocator.REFERENCES / 'accounts.json'), snapshot)
        bundle = allocator.bundles(applied)['grok_bot']
        self.assertEqual((bundle['provider'], bundle['account'], bundle['model'], bundle['effort']),
                         ('grok-bot', 'grok-bot', 'grok-bot', None))
        task = {'objective': 'Rewrite this supplied paragraph as concise prose.', 'work_kind': 'text_in_text_out',
                'difficulty': 'routine', 'expected_hours': 0.2}
        def plan(needs=None, allowed=True, usage=None):
            return allocator.plan({**task, 'needs': needs or {}}, usage or {'accounts': {}},
                                  accounts=applied, records=[], overrides=[],
                                  host_policy_config={'allowed_accounts': ['grok-bot'] if allowed else []})
        selected = plan()
        self.assertEqual(selected['choice'], 'grok_bot')
        candidate = next(c for c in selected['ranking'] if c['id'] == 'grok_bot')
        self.assertIsNone(candidate['expected_usd'])
        self.assertIsNone(candidate['expected_pct'])
        self.assertNotIn('free_usage', candidate['components'])
        self.assertGreater(candidate['components']['opportunity_cost'], 0)
        for needs, reason in [({'local_only': True}, 'local-only'), ({'file_transfer': True}, 'local files'),
                              ({'visual': True}, 'visual input'), ({'outbound_ok': False}, 'outbound')]:
            with self.subTest(needs=needs):
                rejected = next(c for c in plan(needs)['ranking'] if c['id'] == 'grok_bot')
                self.assertTrue(any(reason in r for r in rejected['excluded']))
        self.assertTrue(any('host policy' in r for c in plan(allowed=False)['ranking']
                            if c['id'] == 'grok_bot' for r in c['excluded']))
        applied['accounts']['grok-bot']['bundles']['grok_bot']['disabled'] = 'user disabled'
        self.assertNotEqual(plan()['choice'], 'grok_bot')
        applied['accounts']['grok-bot']['bundles']['grok_bot'].pop('disabled')
        now = datetime.now(timezone.utc)
        weekly = {'id': 'weekly', 'remaining_pct_before_reset': 1,
                  'resets_at': (now + timedelta(days=2)).isoformat(), 'seconds_until_reset': 172800}
        observation = {'status': 'fresh', 'fetched_at': now.isoformat(), 'windows': [weekly]}
        depleted = plan(usage={'accounts': {'grok-bot': observation}})
        self.assertTrue(any('launch protection' in r for c in depleted['ranking']
                            if c['id'] == 'grok_bot' for r in c['excluded']))
        stale = {**observation, 'status': 'stale', 'stale': True,
                 'windows': [{**weekly, 'remaining_pct_before_reset': None, 'observed_remaining_pct': 1}]}
        self.assertEqual(plan(usage={'accounts': {'grok-bot': stale}})['choice'], 'grok_bot')
        (runner.STATE / 'config.json').unlink()
        snapshot = provider_inventory.discover(home, env)
        self.assertEqual(snapshot['accounts']['grok-bot']['status'], 'missing_configuration')

    def test_roll_receipt_and_continuation_cover_accepted_unknown_and_rejected(self):
        prompt = self.root / 'prompt.md'
        prompt.write_text('Task')
        args = SimpleNamespace(file=[], apply_to=None, local_only=None, wait=0)
        bundle = {'provider': 'grok-bot', 'account': 'grok-bot', 'model': 'grok-bot'}
        for delivery, state, launched in [('accepted', 'accepted', True), ('unknown', 'unknown', None),
                                          ('rejected', 'failed', False)]:
            receipt = {'task_id': 'a' * 32, 'state': state, 'delivery': delivery}
            with self.subTest(delivery=delivery), patch.object(runner, 'submit', return_value=receipt):
                result, code = roll.grok_bot(args, {'parent_id': 'parent'}, prompt, {'task_id': 'task'}, bundle)
            self.assertEqual(result['launched'], launched)
            self.assertEqual(result['dispatch_mode'], 'grok-bot')
            self.assertEqual(code, 1 if delivery == 'rejected' else 0)
            self.assertEqual('continuation_argv' in result, delivery != 'rejected')
        for option, value in [('file', [prompt]), ('local_only', 'local dependency'), ('apply_to', self.root)]:
            with self.subTest(option=option), patch.object(runner, 'submit', side_effect=AssertionError('no send')):
                with self.assertRaises(ValueError):
                    roll.grok_bot(SimpleNamespace(**{**vars(args), option: value}), {}, prompt, {}, bundle)

    def test_roll_wait_failure_preserves_accepted_receipt_and_continuation(self):
        prompt = self.root / 'prompt.md'
        prompt.write_text('Task')
        args = SimpleNamespace(file=[], apply_to=None, local_only=None, wait=30)
        bundle = {'provider': 'grok-bot', 'account': 'grok-bot', 'model': 'grok-bot'}
        receipt = {'task_id': 'a' * 32, 'state': 'accepted', 'delivery': 'accepted',
                   'message_id': 'message-1'}
        for error in (TimeoutError('Override store is busy'), OSError('Receipt temporarily unreadable')):
            with self.subTest(error=type(error).__name__), \
                    patch.object(runner, 'submit', return_value=receipt) as submit, \
                    patch.object(runner, 'status', side_effect=error) as status:
                result, code = roll.grok_bot(args, {'parent_id': 'parent'}, prompt, {'task_id': 'task'}, bundle)
            submit.assert_called_once()
            status.assert_called_once_with(receipt['task_id'], 30)
            self.assertEqual(code, 0)
            self.assertIs(result['launched'], True)
            self.assertEqual(result['caller_action'], 'already_launched_do_not_respawn')
            self.assertEqual(result['worker'], {**receipt, 'read_error': str(error)})
            self.assertIn(receipt['task_id'], result['continuation_argv'])
            self.assertNotIn('read_error', receipt)

    @unittest.skipUnless(runner.CLI.is_file() and runner.shutil.which('node'), 'npm ci and Node required')
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
                   PASEO_AGENT_ID='fixture-parent')
        script = '''
import json, sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv.pop(1))
import route_judge
import run_grok_bot as runner
def accept(text, parent, task):
    return {'provider': 'grok-bot', 'task_id': 'a' * 32, 'state': 'accepted',
            'delivery': 'accepted', 'message_id': 'fixture-message', 'mocked_send': True}
with patch.object(runner, 'submit', side_effect=accept), patch.object(
        route_judge.dispatch_flow, 'launch', side_effect=AssertionError('unexpected external provider launch')):
    raise SystemExit(route_judge.main())
'''
        command = [sys.executable, '-X', 'utf8', '-c', script, str(Path(roll.__file__).parent),
                   'roll', '--brief', str(brief), '--hours', '0.02', '--provider', 'grok-bot',
                   '--account', 'grok-bot', '--model', 'grok-bot', '--basis', 'Fixture explicit Bot request',
                   '--usage-cache', str(usage), '--wait', '0']
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
        self.assertIsNone(ledger.get('expected_usd'))
        # Ordinary automatic roll uses the same record/registry path, with only Bot transport mocked.
        host_policy.write_text(json.dumps({'allowed_accounts': ['grok-bot']}))
        automatic = list(command)
        for flag in ('--provider', '--account', '--model', '--basis'):
            index = automatic.index(flag)
            del automatic[index:index + 2]
        automatic[automatic.index('--hours') + 1] = '0.2'
        automatic += ['--no-score', '--task-id', 'fixture-automatic']
        proc = subprocess.run(automatic, capture_output=True, text=True, encoding='utf-8', env=env, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        auto_result = json.loads(proc.stdout)
        self.assertEqual(auto_result['route']['choice'], 'grok_bot')
        self.assertTrue(auto_result['worker']['mocked_send'])
        with closing(sqlite3.connect(env['ROUTER_DISPATCH_DB'])) as connection:
            decisions = [json.loads(row[0]) for row in connection.execute('SELECT data FROM decisions')]
        auto_decision = next(d for d in decisions if d['task_id'] == 'fixture-automatic')
        self.assertTrue(auto_decision['bound'] and auto_decision['dispatchable'])
        self.assertIsNone(auto_decision['constraint'])
        self.assertEqual(auto_decision['bundle']['bundle_id'], 'grok_bot')
        self.assertIsNone(auto_decision['bundle'].get('expected_usd'))
        for incompatible in (['--file', str(brief)], ['--apply-to', str(self.root)],
                             ['--local-only', 'local git repository and pytest environment']):
            with self.subTest(incompatible=incompatible):
                proc = subprocess.run(automatic + incompatible, capture_output=True, text=True,
                                      encoding='utf-8', env=env, timeout=30)
                self.assertNotIn('mocked_send', proc.stdout)
                self.assertFalse(json.loads(proc.stdout)['launched'])
        # A native trial need not report a reset. Automatic and explicit record
        # admission must agree; the real transport remains mocked throughout.
        for used, stale, blocked in ((100, False, True), (20, False, False),
                                     (100, True, False), (None, False, False)):
            usage.write_text(json.dumps({'providers': {'grok-bot': {
                'available': True, 'stale': stale, 'fetched_at': datetime.now(timezone.utc).isoformat(),
                'windows': [{'id': 'trial', 'used_pct': used, 'resets_at': None}]}}}))
            for entry in (automatic, command):
                with self.subTest(explicit=entry is command, used=used, stale=stale):
                    proc = subprocess.run(entry, capture_output=True, text=True, encoding='utf-8', env=env, timeout=30)
                    if blocked:
                        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                        self.assertNotIn('mocked_send', proc.stdout)
                        if entry is command:
                            self.assertIn('trial', proc.stdout)
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

    @unittest.skipUnless(runner.CLI.is_file() and runner.shutil.which('node'), 'npm ci and Node required')
    def test_actual_roll_explicit_override_dry_run_with_isolated_configuration(self):
        brief = self.root / 'brief.md'
        brief.write_text('Reply with the fixture marker. Do not use tools.')
        host_policy = self.root / 'host-policy.json'
        host_policy.write_text(json.dumps({'allowed_accounts': ['grok-bot']}))
        usage = self.root / 'usage.json'
        usage.write_text('{"providers": {}}')
        env = {key: value for key, value in runner.os.environ.items()
               if key.upper() in ('PATH', 'SYSTEMROOT', 'TEMP', 'TMP')}
        env.update(USERPROFILE=str(self.root), ROUTER_STATE_DIR=str(self.root),
                   ROUTER_PROVIDER_HOME=str(self.root), PASEO_HOME=str(self.root / 'paseo'),
                   ROUTER_HOST_POLICY=str(host_policy), ROUTER_OVERRIDES=str(self.root / 'overrides.json'))
        command = [sys.executable, '-X', 'utf8', str(Path(roll.__file__).with_name('route_judge.py')),
                   'roll', '--brief', str(brief), '--hours', '0.1', '--provider', 'grok-bot',
                   '--account', 'grok-bot', '--model', 'grok-bot', '--basis', 'Fixture explicit Bot request',
                   '--usage-cache', str(usage), '--dry-run']
        proc = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', env=env, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result['dispatch_mode'], 'dry_run')
        self.assertEqual(result['route']['bundle']['provider'], 'grok-bot')
        self.assertFalse((runner.STATE / 'tasks').exists())

    @unittest.skipUnless(runner.CLI.is_file() and runner.shutil.which('node'), 'npm ci and Node required')
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
