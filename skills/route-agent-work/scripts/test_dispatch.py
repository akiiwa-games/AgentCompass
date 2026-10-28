"""Dispatch lifecycle tests: isolated DB, fake host, no live creation or services."""
import copy
import base64
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import allocator
import dispatch_flow as flow
import dispatch_registry as registry


class DispatchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='dispatch-test-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        discovery = patch.object(allocator.provider_inventory, 'current', return_value={'accounts': {}, 'launchers': {}})
        discovery.start()
        self.addCleanup(discovery.stop)
        # Subprocess integration also uses isolated, signed-in local fixtures.
        for folder, filename, document in (
            ('.codex', 'auth.json', {'tokens': {'access_token': 'fixture-codex', 'account_id': 'fixture-account',
                'id_token': 'header.' + base64.urlsafe_b64encode(json.dumps({
                    'https://api.openai.com/auth': {'chatgpt_plan_type': 'pro'}}).encode()).decode().rstrip('=') + '.signature'}}),
            ('.claude-2', '.credentials.json', {'claudeAiOauth': {'accessToken': 'fixture-claude', 'subscriptionType': 'pro'}}),
            ('.paseo', 'config.json', {'agents': {'providers': {'claude-2': {'extends': 'claude', 'enabled': True,
               'env': {'CLAUDE_CONFIG_DIR': str(self.root / '.claude-2')}}}}}),
        ):
            (self.root / folder).mkdir()
            (self.root / folder / filename).write_text(json.dumps(document), encoding='utf-8')
        self.now = registry.utcnow()
        self.accounts = allocator.load_accounts(allocator.REFERENCES / 'accounts.json')
        (self.root / 'host.json').write_text(json.dumps({'allowed_accounts': ['claude-2', 'codex', 'antigravity']}))
        (self.root / 'usage.json').write_text(json.dumps({'providers': {'codex': {}}}))
        env = patch.dict(os.environ, {
            'ROUTER_DISPATCH_DB': str(self.root / 'dispatch.sqlite3'),
            'ROUTER_OVERRIDES': str(self.root / 'overrides.json'),
            'ROUTER_CAPACITY': str(self.root / 'capacity.json'),
            'ROUTER_LEDGER': str(self.root / 'ledger.jsonl'),
            'ROUTER_USAGE_CACHE': str(self.root / 'usage.json'),
            'ROUTER_HOST_POLICY': str(self.root / 'host.json'), 'PYTHONDONTWRITEBYTECODE': '1',
            'ROUTER_DISABLE_LIVE_SCORER': '1',
            'ROUTER_PROVIDER_HOME': str(self.root), 'PASEO_HOME': str(self.root / '.paseo'),
            'PASEO_AGENT_ID': '',
        })
        env.start()
        self.addCleanup(env.stop)
        self.task = {'task_id': 'test-task', 'parent_agent_id': 'parent-a', 'objective': 'fixture work',
                     'work_kind': 'exploration_analysis', 'difficulty': 'hard', 'expected_hours': 0.7,
                     'needs': {'local_only': True, 'local_only_reason': 'requires local daemon state'}}
        self.usage = {'accounts': {'claude-2': {'status': 'fresh', 'fetched_at': self.now.isoformat(), 'windows': [
            {'id': wid, 'used_pct': 100 - rem, 'remaining_pct_before_reset': rem,
             'resets_at': (self.now + timedelta(hours=hours)).isoformat(), 'seconds_until_reset': hours * 3600}
            for wid, rem, hours in [('five_hour', 40, 2.5), ('seven_day', 61, 130)]]}}}
        self.bundle = dict(allocator.bundles(self.accounts)['sonnet55_high_2'], bundle_id='sonnet55_high_2')
        self.launch = {'title': 'Fixture task', 'initialPrompt': 'Only fixture work', 'workspaceId': 'workspace-fixture'}

    def issue(self, task=None, **changes):
        result = dict(stage='plan', choice='sonnet55_high_2', selected_bundle=self.bundle,
                      decision={'action': 'start'})
        result.update(changes)
        return registry.issue(result, task or self.task, self.now)

    def prepare(self, decision=None, task=None):
        return registry.prepare((decision or self.issue())['decision_id'], task or self.task, self.launch,
                                self.usage, accounts=self.accounts, now=self.now)

    def snapshot(self, row, status='running', **changes):
        value = {'id': 'agent-fixture', 'provider': 'claude-2', 'model': 'claude-sonnet-5-5',
                 'currentModeId': 'bypassPermissions', 'thinkingOptionId': 'high',
                 'status': status, 'updatedAt': (self.now + timedelta(seconds=10)).isoformat(),
                 'labels': row['request']['labels']}
        value.update(changes)
        return value

    def test_bare_plan_select_record_do_not_reserve_or_allow_unbound_dispatch(self):
        for stage in ('plan', 'select', 'record'):
            decision = self.issue(stage=stage)
            self.assertTrue(decision['bound'])
        self.assertEqual(registry.reservations(self.now), [])
        unbound = self.issue(task={'objective': 'legacy'})
        with self.assertRaisesRegex(ValueError, 'actual addressable parent'):
            registry.prepare(unbound['decision_id'], {'objective': 'legacy'}, self.launch, self.usage, now=self.now)
        spec = self.accounts['accounts']['claude-2']
        for stage in ('plan', 'select', 'record'):
            self.assertEqual(allocator.inflight_reserved_usd('claude-2', spec, 'five_hour', spec['windows']['five_hour'],
                {'remaining': 40}, 18, [{'stage': stage, 'expected_usd': 100, 'bundle': self.bundle}],
                self.accounts, self.accounts['scoring'], self.now), 0)

    def test_local_disable_after_prepare_blocks_launch_and_reenable_recovers(self):
        prepared = self.prepare()
        snapshot = {'accounts': {'claude-2': {'family': 'claude', 'status': 'paused',
                    'reason': 'account_disabled_in_magpie', 'plan': 'pro'}}, 'launchers': {}}
        paused = allocator.provider_inventory.apply(copy.deepcopy(self.accounts), snapshot)
        with patch.object(allocator, 'load_accounts', return_value=paused):
            with self.assertRaisesRegex(ValueError, 'account_disabled_in_magpie'):
                registry.claim(prepared['request'], parent_id='parent-a', now=self.now)
        self.assertEqual(registry.inspect(prepared['dispatch_id'])['state'], 'prepared')
        snapshot['accounts']['claude-2'].update(status='ready', reason=None)
        restored = allocator.provider_inventory.apply(copy.deepcopy(self.accounts), snapshot)
        with patch.object(allocator, 'load_accounts', return_value=restored):
            self.assertEqual(registry.claim(prepared['request'], parent_id='parent-a', now=self.now)['state'], 'launching')

    def test_cli_explicit_astra_low_override_is_bound_and_capacity_priced_at_low(self):
        packet = self.root / 'task.json'
        packet.write_text(json.dumps({'task': self.task}))
        command = [sys.executable, '-B', str(Path(registry.__file__).with_name('route_judge.py')),
            'record', '--input', str(packet), '--constraint', 'user_override', '--basis',
            'Synthetic test fixture: explicitly request Astra low', '--provider', 'codex',
            '--account', 'codex', '--model', 'gpt-6-astra', '--effort', 'low']
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        decision = json.loads(result.stdout)
        self.assertTrue(decision['bound'])
        self.assertEqual(registry.reservations(), [])
        row = self.prepare(decision)
        self.assertEqual(row['request']['provider'], 'codex/gpt-6-astra')
        self.assertEqual(row['request']['settings'], {'thinkingOptionId': 'low', 'modeId': 'full-access'})
        expected = allocator.bundle_task_usd(self.accounts['accounts']['codex'], 'gpt-6-astra', 'low',
            self.task['expected_hours'], self.accounts['scoring'], self.accounts['effort_curves']['gpt-6-astra'],
            self.accounts['models']['gpt-6-astra'])
        self.assertAlmostEqual(row['expected_usd'], round(expected, 2))
        altered = copy.deepcopy(row['request'])
        altered['settings']['thinkingOptionId'] = 'high'
        with self.assertRaisesRegex(ValueError, 'configuration changed'):
            registry.claim(altered, parent_id='parent-a')
        with self.assertRaisesRegex(ValueError, 'another parent'):
            registry.claim(row['request'], parent_id='other-parent')
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'setup'))
        import router_gate_hook as gate
        with patch.dict(os.environ, {'ROUTER_GATE_BYPASS': '0'}):
            allowed = gate.evaluate_payload({'tool_name': 'create_agent', 'tool_input': row['request'],
                'parent_agent_id': 'parent-a', 'tool_use_id': 'override-fixture'})
        self.assertEqual(allowed, {})

    def override_decision(self, effort='low', **changes):
        bundle = {'provider': 'codex', 'account': 'codex', 'model': 'gpt-6-astra', 'effort': effort}
        result = dict(stage='record', constraint='user_override', basis='Synthetic explicit override fixture',
                      choice='constraint', selected_bundle=bundle)
        result.update(changes)
        return registry.issue(result, self.task, self.now)

    def test_override_requires_explicit_record_and_known_effort_identity(self):
        for changes in ({'stage': 'plan'}, {'constraint': 'hard_constraint'}, {'basis': ''}):
            with self.assertRaisesRegex(ValueError, 'requires a new route'):
                self.prepare(self.override_decision(**changes))
        with self.assertRaisesRegex(ValueError, 'no known'):
            self.override_decision(effort='turbo')
        for changes in ({'account': 'claude-2'}, {'provider': 'pi'}, {'model': 'unknown-model'}):
            bundle = dict(provider='codex', account='codex', model='gpt-6-astra', effort='low')
            bundle.update(changes)
            with self.assertRaisesRegex(ValueError, 'no known'):
                self.override_decision(selected_bundle=bundle)

    def test_explicit_low_override_preserves_host_policy_and_capacity_checks(self):
        decision = self.override_decision()
        (self.root / 'host.json').write_text(json.dumps({'allowed_accounts': ['claude-2']}))
        with self.assertRaisesRegex(ValueError, 'host policy'):
            self.prepare(decision)
        (self.root / 'host.json').write_text(json.dumps({'allowed_accounts': ['codex']}))
        exhausted = {'accounts': {'codex': {'status': 'fresh', 'fetched_at': self.now.isoformat(), 'windows': [
            {'id': 'usage', 'used_pct': 99, 'remaining_pct_before_reset': 1,
             'seconds_until_reset': 86400, 'resets_at': (self.now + timedelta(days=1)).isoformat()}]}}}
        with self.assertRaisesRegex(ValueError, 'Capacity/availability'):
            registry.prepare(decision['decision_id'], self.task, self.launch, exhausted, now=self.now)

    def test_fresh_claude2_eighty_percent_protects_prepare_mcp_and_sdk_claim(self):
        import route_judge
        self.task['expected_hours'] = 0.001
        row = self.prepare()  # Existing quota permits preparation; it can change before launch.
        raw = {'providers': {'claude-2': {'available': True, 'stale': False, 'fetched_at': self.now.isoformat(),
            'windows': [{'id': 'five_hour', 'used_pct': 80,
                         'resets_at': (self.now + timedelta(hours=2)).isoformat()}]}}}
        (self.root / 'usage.json').write_text(json.dumps(raw))
        usage = route_judge.usage_snapshot(now=self.now)
        other_task = dict(self.task, task_id='another-task')
        decision = self.issue(other_task)
        with self.assertRaisesRegex(ValueError, 'launch protection'):
            registry.prepare(decision['decision_id'], other_task, self.launch, usage, now=self.now)
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'setup'))
        import router_gate_hook as gate
        with patch.dict(os.environ, {'ROUTER_GATE_BYPASS': '0'}):
            blocked = gate.evaluate_payload({'tool_name': 'create_agent', 'tool_input': row['request'],
                'parent_agent_id': 'parent-a'}, now=self.now)
        self.assertEqual(blocked['decision'], 'block')
        calls = []
        with self.assertRaisesRegex(ValueError, 'launch protection'):
            flow.launch(row['dispatch_id'], 'parent-a', host=lambda request: calls.append(request), watch_seconds=0)
        self.assertEqual(calls, [])
        self.assertEqual(registry.inspect(row['dispatch_id'])['state'], 'prepared')
        # Stale or inactive observations do not introduce a new launch hard-stop.
        for changes in ({'stale': True}, {'fetched_at': (self.now - timedelta(hours=1)).isoformat()},
                        {'windows': [{'id': 'five_hour', 'used_pct': 80, 'resets_at': self.now.isoformat()}]}):
            observation = copy.deepcopy(raw)
            observation['providers']['claude-2'].update(changes)
            (self.root / 'usage.json').write_text(json.dumps(observation))
            registry.check_launch_quota(row['bundle'], self.accounts, route_judge.usage_snapshot(now=self.now), self.now)

    def test_cli_actual_parent_blocks_foreign_prepare_launch_release_but_operator_works(self):
        decision = self.issue()
        packet = self.root / 'dispatch.json'
        packet.write_text(json.dumps({'decision_id': decision['decision_id'], 'task': self.task, 'launch': self.launch}))
        script = str(Path(registry.__file__).with_name('route_judge.py'))
        def cli(*args, actual=None):
            env = dict(os.environ)
            env.pop('PASEO_AGENT_ID', None)
            if actual is not None:
                env['PASEO_AGENT_ID'] = actual
            return subprocess.run([sys.executable, '-B', script, *args], env=env,
                                  capture_output=True, text=True, timeout=30)
        prepare_args = ('dispatch', '--dispatch-action', 'prepare', '--input', str(packet))
        denied = cli(*prepare_args, actual='foreign-parent')
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn('actual PASEO_AGENT_ID', denied.stdout)
        self.assertEqual(registry.reservations(), [])
        prepared = cli(*prepare_args, actual='parent-a')
        self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
        dispatch_id = json.loads(prepared.stdout)['dispatch_id']
        for action in (('launch', '--watch-seconds', '0'),
                       ('dispatch', '--dispatch-action', 'release', '--basis', 'fixture cancellation')):
            denied = cli(*action, '--dispatch-id', dispatch_id, '--parent-id', 'parent-a', actual='foreign-parent')
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn('actual PASEO_AGENT_ID', denied.stdout)
            self.assertEqual(registry.inspect(dispatch_id)['state'], 'prepared')
        # Non-agent operator can explicitly identify the parent; no actual agent is launched.
        same = cli(*prepare_args)
        self.assertEqual(json.loads(same.stdout)['dispatch_id'], dispatch_id)
        released = cli('dispatch', '--dispatch-action', 'release', '--dispatch-id', dispatch_id,
                       '--parent-id', 'parent-a', '--basis', 'operator fixture cancellation')
        self.assertEqual(json.loads(released.stdout)['state'], 'canceled')
        receipt = cli('launch', '--dispatch-id', dispatch_id, '--parent-id', 'parent-a', '--watch-seconds', '0')
        self.assertEqual(json.loads(receipt.stdout)['state'], 'canceled')

    def test_prepare_is_idempotent_rejects_changed_task_and_revises_prompt_in_place(self):
        decision = self.issue()
        first = self.prepare(decision)
        self.assertEqual(self.prepare(decision)['dispatch_id'], first['dispatch_id'])
        self.assertEqual(len(registry.reservations(self.now)), 1)
        for task in (dict(self.task, parent_agent_id='parent-b'), dict(self.task, task_id='other'),
                     dict(self.task, objective='different work')):
            with self.assertRaises(ValueError):
                self.prepare(decision, task)
        revised = registry.prepare(decision['decision_id'], self.task,
            dict(self.launch, initialPrompt='revised work', title='revised'), self.usage, now=self.now)
        self.assertEqual(revised['dispatch_id'], first['dispatch_id'])
        self.assertEqual(revised['request']['initialPrompt'], 'revised work')
        self.assertEqual(revised['request']['labels'], first['request']['labels'])
        self.assertEqual(len(registry.reservations(self.now)), 1)
        self.assertEqual(registry.claim(revised['request'], parent_id='parent-a', now=self.now)['state'], 'launching')

    def test_claim_cannot_replay_or_change_bundle_account_task_or_mode(self):
        row = self.prepare()
        for key in ('router.account', 'router.task_id', 'router.parent_id', 'router.decision_id'):
            request = copy.deepcopy(row['request'])
            request['labels'][key] = 'other'
            with self.assertRaises(ValueError):
                registry.claim(request, now=self.now)
        changed = dict(row['request'], provider='claude/claude-sonnet-5-5')
        with self.assertRaises(ValueError):
            registry.claim(changed, now=self.now)
        with self.assertRaises(ValueError):
            registry.claim(row['request'], parent_id='other', now=self.now)
        first = registry.claim(row['request'], parent_id='parent-a', call_id='call-a', now=self.now)
        self.assertEqual(first['state'], 'launching')
        self.assertEqual(registry.claim(row['request'], call_id='call-a', now=self.now)['state'], 'launching')
        with self.assertRaisesRegex(ValueError, 'already claimed'):
            registry.claim(row['request'], call_id='call-b', now=self.now)

    def test_start_duplicate_ack_terminal_release_and_wrong_agent(self):
        row = self.prepare()
        registry.claim(row['request'], now=self.now)
        snapshot = self.snapshot(row)
        started = registry.observe(row['dispatch_id'], snapshot, now=self.now + timedelta(seconds=10))
        self.assertEqual(started['state'], 'started')
        again = registry.observe(row['dispatch_id'], snapshot, now=self.now + timedelta(seconds=11))
        self.assertEqual(again['events'], started['events'])
        with self.assertRaises(ValueError):
            registry.observe(row['dispatch_id'], dict(snapshot, id='wrong-agent'), now=self.now + timedelta(seconds=12))
        terminal = registry.observe(row['dispatch_id'], self.snapshot(row, 'idle', attentionReason='finished'),
                                    now=self.now + timedelta(seconds=12))
        self.assertEqual(terminal['state'], 'completed')
        self.assertEqual(registry.reservations(self.now + timedelta(seconds=13)), [])
        self.assertEqual(registry.observe(row['dispatch_id'], snapshot)['state'], 'completed')

    def test_expired_leases_release_and_cannot_launch(self):
        row = self.prepare()
        late = self.now + timedelta(seconds=registry.PREPARE_LEASE + 1)
        self.assertEqual(registry.reservations(late), [])
        with self.assertRaises(ValueError):
            registry.claim(row['request'], now=late)
        self.assertEqual(registry.inspect(row['dispatch_id'])['state'], 'expired')

    def test_cancel_before_launch_and_reject_freeing_a_running_agent(self):
        row = self.prepare()
        with self.assertRaises(ValueError):
            registry.release(row['dispatch_id'], 'other-parent', 'cancel')
        result = registry.release(row['dispatch_id'], 'parent-a', 'canceled before submission')
        self.assertEqual(result['state'], 'canceled')
        self.assertEqual(registry.release(row['dispatch_id'], 'parent-a', 'again')['events'], result['events'])
        row2 = self.prepare(self.issue())
        registry.claim(row2['request'], now=self.now)
        registry.observe(row2['dispatch_id'], self.snapshot(row2), now=self.now)
        with self.assertRaisesRegex(ValueError, 'terminal status'):
            registry.release(row2['dispatch_id'], 'parent-a', 'cannot release live worker')

    def test_planning_reconciles_finished_agents_and_dry_read_has_no_side_effects(self):
        row = self.prepare()
        registry.claim(row['request'], now=self.now)
        registry.observe(row['dispatch_id'], self.snapshot(row), now=self.now)
        terminal = {'ok': True, 'agent': self.snapshot(row, 'idle', attentionReason='finished')}
        with patch.object(flow, 'host_call', return_value=terminal) as host:
            before = registry.db_path().read_bytes()
            self.assertEqual(len(flow.reservation_records(read_only=True)), 1)
            host.assert_not_called()
            self.assertEqual(registry.db_path().read_bytes(), before)
            self.assertEqual(flow.reservation_records(), [])
            host.assert_called_once()
        self.assertEqual(registry.inspect(row['dispatch_id'])['state'], 'completed')

    def test_authoritative_missing_agent_releases_but_unknown_submission_does_not(self):
        row = self.prepare()
        registry.claim(row['request'], now=self.now)
        flow.reconcile(row['dispatch_id'], host=lambda _: {'ok': True, 'agent': None})
        self.assertEqual(registry.inspect(row['dispatch_id'])['state'], 'launching')
        registry.observe(row['dispatch_id'], self.snapshot(row), now=self.now)
        flow.reconcile(row['dispatch_id'], host=lambda _: {'ok': True, 'agent': None, 'missing': True})
        self.assertEqual(registry.inspect(row['dispatch_id'])['state'], 'canceled')

    def test_lease_renews_from_host_and_failed_agent_releases(self):
        row = self.prepare()
        registry.claim(row['request'], now=self.now)
        later = self.now + timedelta(seconds=registry.RUN_LEASE - 1)
        started = registry.observe(row['dispatch_id'], self.snapshot(row), now=later)
        self.assertGreater(registry.parse(started['lease_until']), self.now + timedelta(seconds=registry.RUN_LEASE))
        failed = registry.observe(row['dispatch_id'], self.snapshot(row, 'error'), now=later)
        self.assertEqual(failed['state'], 'failed')
        self.assertEqual(registry.reservations(later), [])

    def test_concurrent_prepare_checks_and_reserves_atomically(self):
        jobs = []
        for number in range(2):
            task = dict(self.task, task_id='task-' + str(number))
            decision = self.issue(task)
            packet = self.root / (str(number) + '.json')
            packet.write_text(json.dumps({'decision_id': decision['decision_id'], 'task': task,
                                          'launch': self.launch, 'usage': self.usage}))
            jobs.append(packet)
        source = """import json,sys
sys.path.insert(0,sys.argv[1])
import dispatch_registry as r
p=json.load(open(sys.argv[2]))
try:
 print(json.dumps({'ok':True,'row':r.prepare(p['decision_id'],p['task'],p['launch'],p['usage'])['dispatch_id']}))
except ValueError as e:
 print(json.dumps({'ok':False,'error':str(e)}))
"""
        procs = [subprocess.Popen([sys.executable, '-B', '-c', source, str(Path(registry.__file__).parent), str(p)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for p in jobs]
        try:
            replies = []
            for proc in procs:
                out, err = proc.communicate(timeout=20)
                self.assertEqual(proc.returncode, 0, err)
                replies.append(json.loads(out))
            self.assertEqual(sum(r['ok'] for r in replies), 1, replies)
            self.assertEqual(len(registry.reservations()), 1)
        finally:
            for proc in procs:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()

    def test_failed_start_and_ambiguous_create_have_different_lifecycles(self):
        row = self.prepare()
        calls = []
        def rejected(request):
            calls.append(request['operation'])
            return {'ok': False, 'sent': False, 'error': 'fixture_pre_send_failure'}
        result = flow.launch(row['dispatch_id'], 'parent-a', rejected, watch_seconds=0)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(calls, ['create'])
        self.assertEqual(registry.reservations(), [])
        row2 = self.prepare(self.issue())
        def uncertain(request):
            calls.append(request['operation'])
            return {'ok': False, 'sent': True} if request['operation'] == 'create' else {'ok': True, 'agent': None}
        result = flow.launch(row2['dispatch_id'], 'parent-a', uncertain, watch_seconds=0)
        self.assertEqual(result['state'], 'launching')
        self.assertEqual(result['continuation_argv'][-6:],
                         ['--dispatch-id', row2['dispatch_id'], '--parent-id', 'parent-a', '--watch-seconds', '120'])
        previous_creates = calls.count('create')
        flow.launch(row2['dispatch_id'], 'parent-a', uncertain, watch_seconds=0)
        self.assertEqual(calls.count('create'), previous_creates)
        recovered = flow.reconcile(row2['dispatch_id'], host=lambda _: {'ok': True, 'agent': self.snapshot(row2, 'idle', attentionReason='finished')})
        self.assertEqual(recovered[0]['state'], 'completed')
        self.assertEqual(registry.reservations(), [])

    def test_initial_idle_is_not_finished_and_wrong_settings_not_acked(self):
        row = self.prepare()
        registry.claim(row['request'], now=self.now)
        initial = registry.observe(row['dispatch_id'], self.snapshot(row, 'idle'), now=self.now)
        self.assertEqual(initial['state'], 'launched')
        for changes in ({'currentModeId': 'read-only'}, {'thinkingOptionId': 'low'},
                        {'model': 'other'}, {'provider': 'claude'}):
            with self.assertRaises(ValueError):
                registry.observe(row['dispatch_id'], self.snapshot(row, **changes), now=self.now)

    def test_permission_wait_is_not_terminal(self):
        row = self.prepare()
        registry.claim(row['request'], now=self.now)
        registry.observe(row['dispatch_id'], self.snapshot(row), now=self.now)
        waiting = registry.observe(row['dispatch_id'], self.snapshot(row, 'idle', attentionReason='permission'), now=self.now)
        self.assertEqual(waiting['state'], 'started')
        self.assertNotIn('released_at', waiting)
        waiting = registry.observe(row['dispatch_id'], self.snapshot(row, 'idle', pendingPermissionCount=1), now=self.now)
        self.assertEqual(waiting['state'], 'started')

    def test_launch_recovers_expired_started_lease_by_observation_only(self):
        row = self.prepare()
        registry.claim(row['request'], now=self.now)
        registry.observe(row['dispatch_id'], self.snapshot(row), now=self.now)
        registry.reservations(now=self.now + timedelta(seconds=registry.RUN_LEASE + 1))
        calls = []
        def host(request):
            calls.append(request['operation'])
            return {'ok': True, 'agent': self.snapshot(row, 'idle', attentionReason='finished')}
        result = flow.launch(row['dispatch_id'], 'parent-a', host, watch_seconds=0)
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(calls, ['observe'])

    def test_argon_and_flash_budget_units_agree_in_both_windows(self):
        account = self.accounts['accounts']['antigravity']
        for model in ('gemini-4-argon-high', 'gemini-3.8-flash-high'):
            info = self.accounts['models'][model]
            budget = allocator.bundle_task_usd(account, model, 'high', 1, self.accounts['scoring'], info=info)
            for wid in ('gemini-weekly', 'gemini-5h'):
                pct = allocator.unit_rate(account, wid, model, 'high', self.accounts['scoring'], info=info)
                self.assertAlmostEqual(pct * allocator.window_capacity_usd(account, wid) / 100, budget)

    def test_cli_prepare_launch_and_automatic_status_reconcile_fake_host(self):
        # Exercise the executable entrypoints, not only the in-memory state machine.
        import route_judge
        raw = {'providers': {'claude-2': {'fetched_at': self.now.isoformat(), 'stale': False,
                    'available': True, 'windows': self.usage['accounts']['claude-2']['windows']}}}
        (self.root / 'usage.json').write_text(json.dumps(raw))
        packet = self.root / 'packet.json'
        script = str(Path(route_judge.__file__))
        def cli(*args):
            result = subprocess.run([sys.executable, '-B', script, *args], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout)
        self.task['constraints'] = {'bundle': 'sonnet55_high_2'}
        task_file = self.root / 'task.json'
        task_file.write_text(json.dumps({'task': self.task}))
        decision = cli('plan', '--input', str(task_file))
        self.assertTrue(decision['bound'])
        self.assertEqual(decision['choice'], 'sonnet55_high_2')
        self.assertEqual(registry.reservations(), [])
        packet.write_text(json.dumps({'decision_id': decision['decision_id'], 'task': self.task, 'launch': self.launch}))
        prepared = cli('dispatch', '--input', str(packet))
        fake = self.root / 'fake-host.mjs'
        fake.write_text("""import fs from 'node:fs';
const p=JSON.parse(fs.readFileSync(0,'utf8')); const f=process.env.FAKE_HOST_STATE;
if(p.operation==='create') {
 const agent={id:p.dispatch_id,provider:'claude-2',model:'claude-sonnet-5-5',status:'running',
 currentModeId:'bypassPermissions',thinkingOptionId:'high',updatedAt:new Date().toISOString(),labels:p.request.labels};
 fs.writeFileSync(f,JSON.stringify(agent)); console.log(JSON.stringify({ok:true,agent}));
} else {const agent=JSON.parse(fs.readFileSync(f,'utf8'));agent.status='idle';agent.attentionReason='finished';
 agent.updatedAt=new Date().toISOString();console.log(JSON.stringify({ok:true,agent}));}
""")
        with patch.dict(os.environ, {'ROUTER_PASEO_BRIDGE': str(fake), 'FAKE_HOST_STATE': str(self.root / 'agent.json')}):
            result = cli('launch', '--dispatch-id', prepared['dispatch_id'], '--parent-id', 'parent-a', '--watch-seconds', '0')
            self.assertEqual(result['state'], 'completed')
            # Idempotent launch never creates a second agent after terminal receipt.
            self.assertEqual(cli('launch', '--dispatch-id', prepared['dispatch_id'], '--parent-id', 'parent-a', '--watch-seconds', '0')['state'], 'completed')
        self.assertEqual(registry.reservations(), [])


class RollTests(unittest.TestCase):
    """roll: brief in, routed launch out. Reuses the isolated registry; no live host, Dot or scorer."""
    setUp, snapshot = DispatchTests.setUp, DispatchTests.snapshot

    def roll(self, decision, parent='parent-a', reachable=False, env=None, **flags):
        import argparse
        import roll
        brief = self.root / 'task.md'
        brief.write_text(self.brief, encoding='utf-8')
        args = argparse.Namespace(brief=brief, file=[], apply_to=None, local_only=None, hours=0.5, title=None,
            workspace=None, task_id='test-task', wait=0, parent_id=None, parent_host=None, parent_address=None, provider=None, model=None, effort=None,
            basis=None, usage_cache=None, dry_run=False)
        vars(args).update(flags)
        seen = {}

        def plan_stage(document, _):
            seen.update(document)
            return dict(decision, **registry.issue(decision, document['task'], self.now)), 0
        judge = type('Judge', (), {'plan_stage': staticmethod(plan_stage),
                                   'usage_snapshot': staticmethod(lambda _: self.usage),
                                   'subscription_env': staticmethod(lambda account=None: dict(os.environ)),
                                   'cli_prefix': staticmethod(lambda name: [name])})
        base_env = {
            'PASEO_AGENT_ID': parent,
            'ROUTER_PARENT_AGENT_ID': '',
            'ANTIGRAVITY_CONVERSATION_ID': '',
            'CLAUDE_SESSION_ID': '',
            'CLAUDE_CODE_SESSION_ID': '',
            'CODEX_THREAD_ID': '',
            'CODEX_SESSION_ID': '',
            'PI_SESSION_ID': '',
            'OPENCODE_SESSION_ID': '',
            'CLAUDECODE': '',
            'CODEX_SANDBOX': '',
            'CODEX_CI': '',
            **(env or {}),
        }
        with patch.dict(os.environ, base_env), \
                patch.object(roll, 'WORKER_PROMPTS_DIR', self.root / 'worker-prompts'), \
                patch.object(flow, 'paseo_reachable', return_value=reachable):
            return roll.run(args, judge), seen

    brief = '# Fixture brief\n\nKeep only the current rules.\n'

    def assert_worker_prompt(self, result, parent=None):
        import roll
        prompt = Path(result['worker_prompt_file']).read_text(encoding='utf-8')
        self.assertTrue(prompt.startswith(self.brief + '\n'))
        self.assertIn(roll.WORKER_COMMUNICATION.read_text(encoding='utf-8'), prompt)
        if parent:
            self.assertIn(f'Parent session agent ID: `{parent}`.', prompt)
        self.assertEqual((self.root / 'task.md').read_text(encoding='utf-8'), self.brief)
        return prompt

    def test_paseo_route_launches_the_brief_with_communication_contract_and_routed_bundle(self):
        calls = []

        def host(request):
            calls.append(request)
            if request['operation'] == 'create':
                return {'ok': True, 'agent': self.snapshot({'request': request['request']})}
            return {'ok': True, 'agent': None}
        decision = dict(stage='plan', choice='sonnet55_high_2', selected_bundle=self.bundle, decision={'action': 'start'})
        with patch.object(flow, 'host_call', host):
            (result, code), seen = self.roll(decision)
        self.assertEqual(code, 0)
        self.assertTrue(result['launched'])
        self.assertEqual(seen['task']['objective'], self.brief)
        request = calls[0]['request'] if calls[0]['operation'] == 'create' else next(
            c['request'] for c in calls if c['operation'] == 'create')
        self.assertEqual(request['initialPrompt'], self.assert_worker_prompt(result, parent='parent-a'))
        self.assertEqual(request['title'], 'Fixture brief')
        self.assertEqual(request['provider'], 'claude-2/' + (self.bundle.get('launch_model') or self.bundle['model']))
        self.assertEqual(request['settings']['thinkingOptionId'], self.bundle['effort'])

    def test_routes_that_cannot_launch_here_do_not_launch(self):
        paseo = dict(stage='plan', choice='sonnet55_high_2', selected_bundle=self.bundle, decision={'action': 'start'})
        cases = (('user decision', dict(paseo, decision={'action': 'start', 'needs_user_decision': True}), 'parent-a', 2),
                 ('no worker', dict(stage='plan', choice='none', selected_bundle={}, decision={}), 'parent-a', 2))
        for label, decision, parent, expected in cases:
            with self.subTest(label), patch.object(flow, 'host_call', side_effect=AssertionError('must not launch')):
                (result, code), _ = self.roll(decision, parent=parent)
                self.assertEqual((result['launched'], code), (False, expected))
                self.assertIn('next', result)
                self.assertNotIn('worker_prompt_file', result)
                self.assertFalse((self.root / 'worker-prompts').exists())

    def test_missing_parent_stops_before_routing_even_with_daemon(self):
        for parent in ('', ' ', 'top-level', 'unknown'):
            with self.subTest(parent=parent), patch.object(flow, 'host_call', side_effect=AssertionError('must not launch')):
                with self.assertRaisesRegex(ValueError, 'actual addressable parent'):
                    self.roll({}, parent=parent, reachable=True)
        self.assertFalse((self.root / 'worker-prompts').exists())
        self.assertEqual(registry.reservations(), [])

    def test_explicit_parent_and_native_address_survive_native_handoff(self):
        decision = dict(stage='plan', choice='sonnet55_high_2', selected_bundle=self.bundle, decision={'action': 'start'})
        with patch.object(flow, 'host_call', side_effect=AssertionError('must not switch to Paseo')):
            (result, code), seen = self.roll(decision, parent='', parent_host='native',
                parent_id='coordinator-session', parent_address='run:coordinator-run', reachable=True)
        self.assertEqual((result['launched'], code), (False, 0))
        prompt = self.assert_worker_prompt(result, parent='coordinator-session')
        self.assertIn('run:coordinator-run', prompt)
        self.assertIn('request_user_input_async', prompt)
        self.assertEqual(seen['task']['parent_agent_id'], 'coordinator-session')
        self.assertIn('--spec', result['next'])
        with self.assertRaisesRegex(ValueError, '--parent-address'):
            self.roll(decision, parent='', parent_host='native', parent_id='coordinator-session')
        with self.assertRaisesRegex(ValueError, 'actual PASEO_AGENT_ID'):
            self.roll(decision, parent_id='another-session')
        with self.assertRaisesRegex(ValueError, 'message address must equal'):
            self.roll(decision, parent_address='another-session')

    def test_dry_run_without_parent_does_not_launch(self):
        with patch.object(flow, 'host_call', side_effect=AssertionError('must not launch')):
            (result, code), _ = self.roll({'choice': 'none'}, parent='', dry_run=True)
        self.assertEqual((result['launched'], code), (False, 0))
        self.assertNotIn('worker_prompt_file', result)

    def test_no_daemon_runs_the_routed_bundle_through_its_cli_on_the_providers_account(self):
        import roll
        (self.root / 'paseo').mkdir()
        (self.root / 'paseo' / 'config.json').write_text(json.dumps({'agents': {'providers': {
            'claude-2': {'extends': 'claude', 'env': {'CLAUDE_CONFIG_DIR': 'C:/fixture/.claude-2'}}}}}))
        host = {'PASEO_HOME': str(self.root / 'paseo'), 'CLAUDECODE': '1', 'CLAUDE_CONFIG_DIR': 'C:/host/.claude-2',
                'CODEX_HOME': 'C:/host/.codex-2'}
        bundles = allocator.bundles(self.accounts)
        brief = ['--print']
        cases = (('claude-2 via its Paseo definition', 'sonnet55_high_2', 0,
                  ['claude', '-p', '--model', 'claude-sonnet-5-5', '--effort', 'high', '--permission-mode', 'bypassPermissions'],
                  'C:/fixture/.claude-2', self.brief),
                 ('claude does not inherit the caller account', 'sonnet55_high', 5,
                  ['claude', '-p', '--model', 'claude-sonnet-5-5', '--effort', 'high', '--permission-mode', 'bypassPermissions'],
                  None, self.brief),
                 ('agy takes the brief as its last argument', 'flash_high', 0,
                  ['agy', '--dangerously-skip-permissions', '--model', bundles['flash_high']['model'], '--add-dir',
                   str(Path.cwd())] + brief, None, None))
        for label, choice, wait, argv, config_dir, stdin in cases:
            calls = []

            class Process:
                pid = 4242

                def __init__(self, command, stdin=None, stdout=None, env=None, **_):
                    calls.append({'argv': command, 'env': env,
                                  'stdin': stdin.read().decode() if hasattr(stdin, 'read') else None})
                    stdout.write(b'worker reply')

                def wait(self, timeout=None):
                    return 0
            bundle = dict(bundles[choice], bundle_id=choice)
            decision = dict(stage='plan', choice=choice, selected_bundle=bundle, decision={'action': 'start'})
            with self.subTest(label), patch.object(roll, 'CLI_RUNS_DIR', self.root / 'runs'), \
                    patch.object(roll.subprocess, 'Popen', Process), \
                    patch.object(flow, 'host_call', side_effect=AssertionError('no Paseo')):
                (result, code), _ = self.roll(decision, parent='', parent_id='parent-a', parent_host='paseo', env=host, wait=wait)
                self.assertEqual((result['launched'], code), (True, 0))
                prompt = self.assert_worker_prompt(result, parent='parent-a')
                self.assertEqual(calls[0]['env']['ROUTER_WORKER'], '1')
                self.assertEqual(calls[0]['env']['ROUTER_PARENT_AGENT_ID'], 'parent-a')
                self.assertEqual(calls[0]['argv'], argv + [prompt] if stdin is None else argv)
                self.assertEqual(calls[0]['env'].get('CLAUDE_CONFIG_DIR'), config_dir)
                self.assertFalse({'CLAUDECODE', 'CODEX_HOME'} & set(calls[0]['env']))
                self.assertEqual(calls[0]['stdin'], prompt if stdin is not None else None)
                self.assertEqual(result['worker'].get('reply'), 'worker reply' if wait else None)
                self.assertEqual('4242' in result.get('next', ''), not wait)

    def test_no_daemon_and_no_cli_does_not_launch(self):
        import roll
        decision = dict(stage='plan', choice='x', selected_bundle={'provider': 'acp', 'account': 'x', 'model': 'm'},
                        decision={'action': 'start'})
        with patch.object(roll.subprocess, 'Popen', side_effect=AssertionError('must not launch')):
            (result, code), _ = self.roll(decision, parent='', parent_id='parent-a', parent_host='paseo', env={'PASEO_HOME': str(self.root / 'none')})
        self.assertEqual((result['launched'], code), (False, 0))
        self.assertIn('no CLI', result['next'])
        self.assertIn('worker_prompt_file', result['next'])
        self.assert_worker_prompt(result, parent='parent-a')

    def test_auto_environment_dispatch_and_litellm_proxy_cli_launch(self):
        import roll
        override = self.root / 'accounts-override.json'
        override.write_text(json.dumps({
            'accounts': {
                'team-proxy': {
                    'backend_kind': 'litellm',
                    'endpoint': 'https://proxy.example.test',
                    'api_key': 'sk-proxy-test',
                    'models': ['gpt-6.1-sol'],
                }
            }
        }))
        calls = []

        class Process:
            pid = 5151

            def __init__(self, command, stdin=None, stdout=None, env=None, **_):
                calls.append({'argv': command, 'env': env})
                stdout.write(b'proxy reply')

            def wait(self, timeout=None):
                return 0

        decision = dict(stage='plan', choice='team_proxy_sol',
                        selected_bundle={'provider': 'team-proxy', 'account': 'team-proxy', 'model': 'gpt-6.1-sol', 'effort': 'high'},
                        decision={'action': 'start'})
        with patch.object(roll, 'CLI_RUNS_DIR', self.root / 'runs'), \
                patch.object(roll.subprocess, 'Popen', Process), \
                patch.object(flow, 'host_call', side_effect=AssertionError('router-only proxy must not call Paseo')):
            (result, code), _ = self.roll(
                decision,
                parent='',
                parent_id=None,
                parent_host=None,
                env={
                    'CLAUDE_SESSION_ID': 'claude-host-session-1',
                    'ROUTE_MANAGER_ACCOUNTS_OVERRIDE': str(override),
                    'PASEO_HOME': str(self.root / 'none'),
                },
                reachable=True,
                wait=5,
            )
        self.assertEqual((result['launched'], code), (True, 0))
        self.assertEqual(result['dispatch_mode'], 'cli')
        self.assertEqual(result['caller_action'], 'already_launched_do_not_respawn')
        self.assertEqual(result['parent_id'], 'claude-host-session-1')
        self.assertEqual(calls[0]['env']['OPENAI_BASE_URL'], 'https://proxy.example.test/v1')
        self.assertEqual(calls[0]['env']['OPENAI_API_KEY'], 'sk-proxy-test')

        # Multi-model proxy dispatches Claude models via `claude` harness + ANTHROPIC_* env, not `codex`.
        calls.clear()
        claude_proxy_decision = dict(
            stage='plan',
            choice='team_proxy_sonnet',
            selected_bundle={'provider': 'team-proxy', 'harness': 'claude', 'account': 'team-proxy',
                             'model': 'claude-sonnet-5-5', 'effort': 'high'},
            decision={'action': 'start'},
        )
        with patch.object(roll, 'CLI_RUNS_DIR', self.root / 'runs'), \
                patch.object(roll.subprocess, 'Popen', Process), \
                patch.object(flow, 'host_call', side_effect=AssertionError('router-only proxy must not call Paseo')):
            (c_res, c_code), _ = self.roll(
                claude_proxy_decision,
                parent='',
                parent_id=None,
                parent_host=None,
                env={
                    'CLAUDE_SESSION_ID': 'claude-host-session-1',
                    'ROUTE_MANAGER_ACCOUNTS_OVERRIDE': str(override),
                    'PASEO_HOME': str(self.root / 'none'),
                },
                reachable=True,
                wait=5,
            )
        self.assertEqual((c_res['launched'], c_code), (True, 0))
        self.assertEqual(c_res['route']['bundle']['harness'], 'claude')
        self.assertEqual(calls[0]['argv'][0], 'claude')
        self.assertEqual(calls[0]['env']['ANTHROPIC_BASE_URL'], 'https://proxy.example.test')
        self.assertEqual(calls[0]['env']['ANTHROPIC_API_KEY'], 'sk-proxy-test')

        # Auto-detected non-Paseo host session (e.g. Antigravity / Claude Code) with a standard provider
        # must launch via CLI even when Paseo daemon is reachable in the background.
        calls.clear()
        std_decision = dict(stage='plan', choice='sonnet55_high_2', selected_bundle=self.bundle, decision={'action': 'start'})
        with patch.object(roll, 'CLI_RUNS_DIR', self.root / 'runs'), \
                patch.object(roll.subprocess, 'Popen', Process), \
                patch.object(flow, 'host_call', side_effect=AssertionError('auto-detected non-Paseo host must not call Paseo')):
            (std_res, std_code), _ = self.roll(
                std_decision,
                parent='',
                parent_id=None,
                parent_host=None,
                env={'ANTIGRAVITY_CONVERSATION_ID': 'agy-conv-123'},
                reachable=True,
                wait=5,
            )
        self.assertEqual((std_res['launched'], std_code), (True, 0))
        self.assertEqual(std_res['dispatch_mode'], 'cli')
        self.assertEqual(std_res['parent_id'], 'agy-conv-123')

        self_decision = dict(stage='plan', choice='self', selected_bundle={}, decision={'action': 'self'})
        (self_res, self_code), _ = self.roll(
            self_decision,
            parent='',
            parent_id=None,
            env={'ANTIGRAVITY_CONVERSATION_ID': 'agy-conv-123'},
        )
        self.assertEqual((self_res['launched'], self_code), (False, 2))
        self.assertEqual(self_res['dispatch_mode'], 'self')
        self.assertEqual(self_res['caller_action'], 'execute_self')
        self.assertEqual(self_res['parent_id'], 'agy-conv-123')

    def test_dot_route_hands_the_brief_and_files_to_the_dot_runner(self):
        import roll
        source = self.root / 'x.py'
        source.write_text('foo = 1\n')
        ran = []

        def fake_run(command, **_):
            ran.append(command)
            return subprocess.CompletedProcess(command, 0, 'dot-task-1\n', 'task_id=dot-task-1\n')
        decision = dict(stage='plan', choice='dot_text', selected_bundle={'provider': 'dot', 'account': 'dot', 'model': 'dot'},
                        decision={'action': 'start'})
        with patch.object(roll.subprocess, 'run', fake_run):
            (result, code), _ = self.roll(decision, file=[source], apply_to=self.root)
        command = ran[0]
        self.assertEqual(code, 0)
        self.assertEqual(command[command.index('--prompt-file') + 1], result['worker_prompt_file'])
        self.assert_worker_prompt(result, parent='parent-a')
        self.assertEqual(command[command.index('--file') + 1], str(source))
        self.assertIn('--no-wait', command)
        self.assertEqual(result['worker']['task_id'], 'dot-task-1')
        self.assertEqual(result['continuation_argv'][4:7], ['status', '--task-id', 'dot-task-1'])

    def test_grok_bot_external_route_preserves_parent_and_receipt(self):
        import run_grok_bot as runner
        decision = dict(stage='record', choice='constraint', selected_bundle={
            'provider': 'grok-bot', 'account': 'grok-bot', 'model': 'grok-bot'}, decision={'action': 'start'})
        receipt = {'provider': 'grok-bot', 'task_id': 'a' * 32, 'state': 'accepted', 'delivery': 'accepted'}
        with patch.object(runner, 'submit', return_value=receipt) as submit, \
                patch.object(flow, 'host_call', side_effect=AssertionError('external runner must not call Paseo')):
            (result, code), _ = self.roll(decision)
        self.assertEqual((code, result['launched'], result['dispatch_mode']), (0, True, 'grok-bot'))
        prompt = self.assert_worker_prompt(result, parent='parent-a')
        self.assertEqual(submit.call_args.args[:2], (prompt, 'parent-a'))
        self.assertEqual(result['worker'], receipt)
        self.assertIn(receipt['task_id'], result['continuation_argv'])

    def test_consult_route_and_deferred_command_include_the_communication_contract(self):
        import roll
        decision = dict(stage='plan', choice='gpt_pro', selected_bundle={
            'provider': 'chatgpt-web', 'account': 'codex', 'model': 'gpt-6-pro'}, decision={'action': 'start'})
        for wait in (0, 5):
            with self.subTest(wait=wait), patch.object(roll.subprocess, 'run', return_value=
                    subprocess.CompletedProcess([], 0, '', '')) as run:
                (result, code), _ = self.roll(decision, wait=wait)
                self.assertEqual((code, result['launched']), (0, wait > 0))
                command = run.call_args.args[0] if wait else result['next_argv']
                if not wait:
                    run.assert_not_called()
                self.assertEqual(command[command.index('--prompt') + 1], result['worker_prompt_file'])
                self.assert_worker_prompt(result, parent='parent-a')

    def test_named_model_without_user_quote_is_refused(self):
        with self.assertRaisesRegex(ValueError, '--basis'):
            self.roll({}, model='claude-opus-5-5')

    def test_missing_or_non_positive_hours_is_refused(self):
        for bad in (None, 0, -1, False, float('inf')):
            with self.subTest(hours=bad), self.assertRaisesRegex(ValueError, '--hours'):
                self.roll({}, hours=bad)

    def test_opencode_deepseek_explicit_override_prepares_paseo_request(self):
        (self.root / 'host.json').write_text(json.dumps({'allowed_accounts': ['claude-2', 'codex', 'antigravity', 'opencode']}))
        for effort in ('high', 'default'):
            with self.subTest(effort=effort):
                task = dict(self.task, task_id=f'opencode-{effort}')
                result = {'stage': 'record', 'choice': 'constraint', 'constraint': 'user_override',
                          'basis': 'User asked for DeepSeek',
                          'selected_bundle': {'provider': 'opencode', 'account': 'opencode',
                                              'model': 'opencode/deepseek-v4.1-flash-free', 'effort': effort}}
                decision = registry.issue(result, task, self.now)
                row = registry.prepare(decision['decision_id'], task, self.launch, self.usage, self.accounts, self.now)
                self.assertEqual(row['request']['provider'], 'opencode/opencode/deepseek-v4.1-flash-free')
                self.assertEqual(row['request']['settings'], {'modeId': 'build', 'thinkingOptionId': effort})
                self.assertEqual(row['expected_usd'], 0.0)

    def test_fast_mode_bundle_emits_paseo_fast_feature_and_doubles_expected_usd(self):
        codex_usage = {'accounts': {'codex': {'status': 'fresh', 'fetched_at': self.now.isoformat(), 'windows': [
            {'id': 'usage', 'used_pct': 15, 'remaining_pct_before_reset': 85,
             'resets_at': (self.now + timedelta(hours=100)).isoformat(), 'seconds_until_reset': 100 * 3600}]}}}
        codex_task = {**self.task, 'task_id': 'task-normal-1', 'work_kind': 'general_implementation',
                      'profile': {'reasoning_depth': 2, 'code_complexity': 2, 'context_scale': 1, 'divergence': 1,
                                  'hallucination_risk': 2, 'error_cost': 2, 'needs_visual': False, 'needs_gui': False,
                                  'local_only': True, 'interaction_style': 'engineering', 'reason': 'test'}}
        normal_plan = allocator.plan(codex_task, codex_usage, self.now, accounts=self.accounts)
        self.assertEqual(normal_plan['choice'], 'sol_high')
        normal_dec = registry.issue(dict(normal_plan, stage='plan'), codex_task, self.now)
        normal_row = registry.prepare(normal_dec['decision_id'], codex_task, self.launch, codex_usage, self.accounts, self.now)
        self.assertNotIn('features', normal_row['request']['settings'])

        fast_task = {**codex_task, 'task_id': 'task-fast-1', 'needs': {**codex_task['needs'], 'fast': True}}
        fast_plan = allocator.plan(fast_task, codex_usage, self.now, accounts=self.accounts)
        self.assertTrue(fast_plan['selected_bundle'].get('fast'))
        fast_dec = registry.issue(dict(fast_plan, stage='plan'), fast_task, self.now)
        fast_row = registry.prepare(fast_dec['decision_id'], fast_task, self.launch, codex_usage, self.accounts, self.now)
        self.assertEqual(fast_row['request']['settings'],
                         {'modeId': 'full-access', 'thinkingOptionId': 'high', 'features': {'fast_mode': True}})
        self.assertAlmostEqual(fast_row['expected_usd'], normal_row['expected_usd'] * 2.0, delta=0.02)


if __name__ == '__main__':
    unittest.main()
