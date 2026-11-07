"""Dispatch lifecycle tests: isolated DB, fake host, no live creation or services."""
import copy
import base64
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import allocator
import dispatch_flow as flow
import dispatch_registry as registry

from router_test_support import isolated_router_state, temporary_root


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup, policy=False)


class DispatchTests(unittest.TestCase):
    def test_fast_opt_out_after_record_is_rechecked_at_prepare(self):
        self.bundle = dict(allocator.bundles(self.accounts)['sol_high'], bundle_id='sol_high', fast=True)
        with patch.object(allocator, 'load_accounts', return_value=self.accounts):
            decision = self.issue(stage='record', choice='constraint', constraint='user_override',
                                  basis='Diagnostic fixture')
        self.accounts['models']['gpt-6.1-sol']['fast_capable'] = False
        with self.assertRaisesRegex(ValueError, 'fast_capable'):
            self.prepare(decision)
        self.accounts['models']['gpt-6.1-sol']['fast_capable'] = True
        row = self.prepare(decision)
        self.assertTrue(row['bundle']['fast'])
        self.assertEqual(row['request']['settings']['features']['service_tier'], 'priority')
        self.accounts['models']['gpt-6.1-sol']['fast_capable'] = False
        import route_judge
        with patch.object(allocator, 'load_accounts', return_value=self.accounts), \
                patch.object(route_judge, 'usage_snapshot', return_value=self.usage):
            with self.assertRaisesRegex(ValueError, 'fast_capable'):
                registry.claim(row['request'], parent_id='parent-a', call_id='fixture', now=self.now)
            self.assertEqual(registry.inspect(row['dispatch_id'])['state'], 'prepared')
            self.accounts['models']['gpt-6.1-sol']['fast_capable'] = True
            self.assertEqual(registry.claim(row['request'], parent_id='parent-a',
                                           call_id='fixture', now=self.now)['state'], 'launching')

    def test_pi_request_omits_unsupported_mode_including_saved_legacy_value(self):
        for legacy_mode in (None, 'full-access'):
            bundle = {'provider': 'pi', 'model': 'gpt-6-astra',
                      'launch_model': 'openai-codex/gpt-6-astra', 'effort': 'high', 'modeId': legacy_mode}
            request = registry.launch_request(bundle, {'title': 'Pi fixture', 'initialPrompt': 'Read fixture'})
            self.assertEqual(request['provider'], 'pi/openai-codex/gpt-6-astra')
            self.assertEqual(request['settings'], {'thinkingOptionId': 'high'})

    def setUp(self):
        self.root = temporary_root(self.addCleanup)
        # Subprocess integration also uses isolated, signed-in local fixtures.
        for folder, filename, document in (
            ('.codex', 'auth.json', {'tokens': {'access_token': 'fixture-codex', 'account_id': 'fixture-account',
                'id_token': 'header.' + base64.urlsafe_b64encode(json.dumps({
                    'https://api.openai.com/auth': {'chatgpt_plan_type': 'pro'}}).encode()).decode().rstrip('=') + '.signature'}}),
            ('.claude-lab', '.credentials.json', {'claudeAiOauth': {'accessToken': 'fixture-claude', 'subscriptionType': 'pro'}}),
            ('.paseo', 'config.json', {'agents': {'providers': {'claude-lab': {'extends': 'claude', 'enabled': True,
               'env': {'CLAUDE_CONFIG_DIR': str(self.root / '.claude-lab')}}}}}),
        ):
            (self.root / folder).mkdir()
            (self.root / folder / filename).write_text(json.dumps(document), encoding='utf-8')
        self.now = registry.utcnow()
        self.accounts = allocator.load_accounts(allocator.reference_fleet_path())
        (self.root / 'host.json').write_text(json.dumps({'allowed_accounts': ['claude-lab', 'codex', 'antigravity']}))
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
        # Launchers are admitted by discovered family, not by name: report the fixture's Claude account.
        discovered = patch.object(registry.provider_inventory, 'current', return_value={
            'accounts': {}, 'launchers': {'claude-lab': {'family': 'claude', 'status': 'ready'}}})
        discovered.start()
        self.addCleanup(discovered.stop)
        self.task = {'task_id': 'test-task', 'parent_agent_id': 'parent-a', 'objective': 'fixture work',
                     'work_kind': 'exploration_analysis', 'difficulty': 'hard', 'expected_hours': 0.7,
                     'needs': {'local_only': True, 'local_only_reason': 'requires local daemon state'}}
        self.usage = {'accounts': {'claude-lab': {'status': 'fresh', 'fetched_at': self.now.isoformat(), 'windows': [
            {'id': wid, 'used_pct': 100 - rem, 'remaining_pct_before_reset': rem,
             'resets_at': (self.now + timedelta(hours=hours)).isoformat(), 'seconds_until_reset': hours * 3600}
            for wid, rem, hours in [('five_hour', 40, 2.5), ('seven_day', 61, 130)]]}}}
        self.bundle = dict(allocator.bundles(self.accounts)['lab_sonnet55_high'], bundle_id='lab_sonnet55_high')
        self.launch = {'title': 'Fixture task', 'initialPrompt': 'Only fixture work', 'workspaceId': 'workspace-fixture'}

    def issue(self, task=None, **changes):
        result = dict(stage='plan', choice='lab_sonnet55_high', selected_bundle=self.bundle,
                      decision={'action': 'start'})
        result.update(changes)
        return registry.issue(result, task or self.task, self.now)

    def prepare(self, decision=None, task=None):
        return registry.prepare((decision or self.issue())['decision_id'], task or self.task, self.launch,
                                self.usage, accounts=self.accounts, now=self.now)

    def snapshot(self, row, status='running', **changes):
        value = {'id': 'agent-fixture', 'provider': 'claude-lab', 'model': 'claude-sonnet-5-5',
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
        spec = self.accounts['accounts']['claude-lab']
        for stage in ('plan', 'select', 'record'):
            self.assertEqual(allocator.inflight_reserved_usd('claude-lab', spec, 'five_hour', spec['windows']['five_hour'],
                {'remaining': 40}, 18, [{'stage': stage, 'expected_usd': 100, 'bundle': self.bundle}],
                self.accounts, self.accounts['scoring'], self.now), 0)

    def test_local_disable_after_prepare_blocks_launch_and_reenable_recovers(self):
        prepared = self.prepare()
        snapshot = {'accounts': {'claude-lab': {'family': 'claude', 'status': 'paused',
                    'reason': 'account_disabled_in_magpie', 'plan': 'pro'}}, 'launchers': {}}
        paused = allocator.provider_inventory.apply(copy.deepcopy(self.accounts), snapshot)
        with patch.object(allocator, 'load_accounts', return_value=paused):
            with self.assertRaisesRegex(ValueError, 'account_disabled_in_magpie'):
                registry.claim(prepared['request'], parent_id='parent-a', now=self.now)
        self.assertEqual(registry.inspect(prepared['dispatch_id'])['state'], 'prepared')
        snapshot['accounts']['claude-lab'].update(status='ready', reason=None)
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
        self.assertEqual(row['request']['settings'], {'thinkingOptionId': 'low', 'modeId': 'full-access', 'features': {'service_tier': 'default'}})
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

    def test_cli_pi_fast_is_rejected_before_recording_an_unlaunchable_feature(self):
        pi_home = self.root / '.pi' / 'agent'
        pi_home.mkdir(parents=True)
        (pi_home / 'auth.json').write_text(json.dumps({'openai-codex': {
            'access': 'fixture-pi', 'accountId': 'fixture-account'}}))
        command = [sys.executable, '-B', str(Path(registry.__file__).with_name('route_judge.py')),
            'record', '--constraint', 'user_override', '--basis', 'Synthetic unsupported fast fixture',
            '--provider', 'pi', '--account', 'codex', '--model', 'gpt-6-astra', '--effort', 'high', '--fast']
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Provider 'pi' does not support fast mode", json.loads(result.stdout)['detail'])
        self.assertEqual(registry.reservations(), [])

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
        for changes in ({'account': 'claude-lab'}, {'provider': 'pi'}, {'model': 'unknown-model'}):
            bundle = dict(provider='codex', account='codex', model='gpt-6-astra', effort='low')
            bundle.update(changes)
            with self.assertRaisesRegex(ValueError, 'no known'):
                self.override_decision(selected_bundle=bundle)

    def test_explicit_low_override_preserves_host_policy_and_capacity_checks(self):
        decision = self.override_decision()
        (self.root / 'host.json').write_text(json.dumps({'allowed_accounts': ['claude-lab']}))
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
        raw = {'providers': {'claude-lab': {'available': True, 'stale': False, 'fetched_at': self.now.isoformat(),
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
            observation['providers']['claude-lab'].update(changes)
            (self.root / 'usage.json').write_text(json.dumps(observation))
            registry.check_launch_quota(row['bundle'], self.accounts, route_judge.usage_snapshot(now=self.now), self.now)
        # A local 429 session limit in .claude-lab blocks launch until resets_at even when usage.json is stale.
        stale_obs = copy.deepcopy(raw)
        stale_obs['providers']['claude-lab'].update({'stale': True, 'fetched_at': (self.now - timedelta(minutes=20)).isoformat()})
        (self.root / 'usage.json').write_text(json.dumps(stale_obs))
        log_file = self.root / '.claude-lab' / 'projects' / 'proj' / 'rate-limited.jsonl'
        log_file.parent.mkdir(parents=True, exist_ok=True)
        reset_utc = (self.now + timedelta(hours=2)).strftime('%I:%M%p').lstrip('0').lower()
        log_file.write_text(json.dumps({
            'type': 'assistant',
            'timestamp': (self.now - timedelta(minutes=2)).isoformat(),
            'isApiErrorMessage': True,
            'error': 'rate_limit',
            'message': {'content': [{'type': 'text', 'text': f"API Error: Request rejected (429) · Claude Code: You've hit your session limit · resets {reset_utc} (UTC)"}]},
        }) + '\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'launch protection'):
            registry.check_launch_quota(row['bundle'], self.accounts, route_judge.usage_snapshot(now=self.now), self.now)
        log_file.unlink()
        (self.root / '.agent-compass' / 'rate-limits.json').unlink(missing_ok=True)

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

    def test_launch_and_terminal_recovery_expose_notification_receipt(self):
        row = self.prepare()
        notice = {'status': 'watching', 'pid': 123, 'state_file': 'fixture.json'}
        calls = []

        def host(request):
            calls.append(request['operation'])
            return {'ok': True, 'agent': self.snapshot(row, 'idle', attentionReason='finished'),
                    'completion_notification': notice}

        result = flow.launch(row['dispatch_id'], 'parent-a', host, watch_seconds=0)
        self.assertEqual(result['completion_notification'], notice)
        self.assertEqual(calls, ['create'])
        notice['status'] = 'delivered'
        result = flow.launch(row['dispatch_id'], 'parent-a', host, watch_seconds=0)
        self.assertEqual(result['completion_notification']['status'], 'delivered')
        self.assertEqual(calls, ['create', 'observe'])

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

    def launched(self):
        row = self.prepare(self.issue())
        registry.claim(row['request'], now=self.now)
        registry.observe(row['dispatch_id'], self.snapshot(row), now=self.now)
        return row

    def test_late_observation_keeps_a_finished_worker_completed(self):
        # 11-05: a worker finished, Paseo collected its idle runtime an hour later (status closed), and the first
        # observation after that recorded it canceled. 11-01: a finished worker read as failed after a plugin reload.
        later = self.now + timedelta(hours=2)
        closed = self.launched()
        registry.observe(closed['dispatch_id'], self.snapshot(closed, 'closed', attentionReason='finished',
                                                              updatedAt=later.isoformat()), now=later)
        self.assertEqual(registry.inspect(closed['dispatch_id'])['state'], 'completed')

        seen_finished = {'reason': 'finished', 'status': 'idle', 'at': (self.now + timedelta(minutes=5)).isoformat()}
        for snapshot in ({'status': 'error'}, {'status': 'closed'}, {'archivedAt': later.isoformat(), 'status': 'idle'},
                         {'status': 'idle', 'providerUnavailable': True}):
            row = self.launched()
            registry.observe(row['dispatch_id'], self.snapshot(row, updatedAt=later.isoformat(), **snapshot),
                             now=later, terminal=seen_finished)
            self.assertEqual(registry.inspect(row['dispatch_id'])['state'], 'completed', snapshot)

        # Without a finish the same snapshots still mean what they did, and an older finish does not count.
        cases = (({'status': 'closed'}, None, 'canceled'), ({'status': 'error'}, None, 'failed'),
                 ({'status': 'error'}, {'reason': 'error', 'at': later.isoformat()}, 'failed'),
                 ({'status': 'closed'}, {'reason': 'finished', 'at': (self.now - timedelta(days=1)).isoformat()}, 'canceled'))
        for snapshot, terminal, expected in cases:
            row = self.launched()
            registry.observe(row['dispatch_id'], self.snapshot(row, updatedAt=later.isoformat(), **snapshot),
                             now=later, terminal=terminal)
            self.assertEqual(registry.inspect(row['dispatch_id'])['state'], expected, (snapshot, terminal))

        # Reconcile hands the notifier's sighting through from the bridge's observe answer.
        row = self.launched()
        answer = {'ok': True, 'agent': self.snapshot(row, 'error', updatedAt=later.isoformat()),
                  'completion_notification': {'status': 'delivered', 'terminal': seen_finished}}
        flow.reconcile(row['dispatch_id'], host=lambda _: answer, now=later)
        self.assertEqual(registry.inspect(row['dispatch_id'])['state'], 'completed')

    def test_resumed_worker_previous_finish_is_not_this_dispatch_finish(self):
        # Resume now relabels before sending; the worker then still shows the previous turn's idle/finished.
        row = registry.prepare(self.issue()['decision_id'], self.task, self.launch, self.usage,
                               accounts=self.accounts, now=self.now, resume_agent_id='agent-old')
        registry.claim(row['request'], now=self.now)
        later = (self.now + timedelta(seconds=30)).isoformat()
        stale = self.snapshot(row, 'idle', id='agent-old', attentionReason='finished', updatedAt=later,
                              lastUserMessageAt=(self.now - timedelta(days=14)).isoformat())
        self.assertEqual(registry.observe(row['dispatch_id'], stale, now=self.now)['state'], 'launched')
        self.assertEqual(registry.observe(row['dispatch_id'], dict(stale, status='closed'), now=self.now)['state'],
                         'canceled')
        row = registry.prepare(self.issue()['decision_id'], self.task, self.launch, self.usage,
                               accounts=self.accounts, now=self.now, resume_agent_id='agent-old')
        registry.claim(row['request'], now=self.now)
        landed = dict(self.snapshot(row, 'idle', id='agent-old', attentionReason='finished', updatedAt=later),
                      lastUserMessageAt=later)
        self.assertEqual(registry.observe(row['dispatch_id'], landed, now=self.now)['state'], 'completed')

    def test_reuse_skips_closed_and_long_idle_workers(self):
        agents = self.root / '.paseo' / 'agents' / 'workspace'
        agents.mkdir(parents=True)
        rows = {}
        for name, status in (('idle', 'idle'), ('closed', 'closed')):
            row = self.prepare(self.issue())
            registry.claim(row['request'], now=self.now)
            registry.observe(row['dispatch_id'], self.snapshot(row, id=f'agent-{name}'), now=self.now)
            registry.observe(row['dispatch_id'], self.snapshot(row, 'idle', attentionReason='finished', id=f'agent-{name}'),
                             now=self.now)
            (agents / f'agent-{name}.json').write_text(json.dumps(
                {'id': f'agent-{name}', 'status': status, 'labels': {'router.parent_id': 'parent-a'}}), encoding='utf-8')
            rows[name] = row
        ids = [w['agent_id'] for w in registry.reusable_workers('parent-a', now=self.now + timedelta(minutes=1))]
        self.assertEqual(ids, ['agent-idle'])
        stale = self.now + timedelta(hours=registry.REUSE_MAX_IDLE_HOURS, minutes=1)
        self.assertEqual(registry.reusable_workers('parent-a', now=stale), [])

    def test_reuse_needs_overlap_with_the_new_brief(self):
        import argparse
        import roll
        for banned in ('_MATCH_STOP_WORDS', '_match_tokens'):
            self.assertFalse(hasattr(roll, banned), f'{banned} must be removed from roll')
        worker = {'agent_id': 'agent-review', 'task_ids': ['roll-old'], 'brief_paths': ['/tmp/step1.md'],
                  'chain_ids': ['chain-1'], 'titles': ['Independent review: s01-telegram fix (b)'],
                  'updated_at': self.now.isoformat()}
        args = argparse.Namespace(new_worker=False, resume=None, task_id='roll-new', title=None)
        task = {'task_id': 'roll-new', 'brief_path': '/tmp/step1-rev.md'}
        # Stem overlap ('step1' vs 'step1-rev') and title word overlap ('telegram', 'fix', 'review') never match
        self.assertIsNone(roll.match_reusable_worker(args, task, '# Telegram fix: second review round', 'parent-a',
                                                     workers=[worker]))
        # Exact task_id matches
        by_tid = roll.match_reusable_worker(
            argparse.Namespace(new_worker=False, resume=None, task_id='roll-old', title=None),
            {'task_id': 'roll-old', 'brief_path': None}, '# Any text', 'parent-a', workers=[worker])
        self.assertEqual(by_tid['agent_id'], 'agent-review')
        # Exact brief_path matches
        by_brief = roll.match_reusable_worker(
            args, {'task_id': 'roll-new', 'brief_path': '/tmp/step1.md'}, '# Any text', 'parent-a', workers=[worker])
        self.assertEqual(by_brief['agent_id'], 'agent-review')
        # Explicit dispatch chain matches
        by_chain = roll.match_reusable_worker(
            args, {'task_id': 'roll-new', 'brief_path': None, 'chain': 'chain-1'}, '# Any text', 'parent-a',
            workers=[worker])
        self.assertEqual(by_chain['agent_id'], 'agent-review')
        # Explicit --resume matches
        by_resume = roll.match_reusable_worker(
            argparse.Namespace(new_worker=False, resume='agent-review', task_id='roll-new', title=None),
            task, '# Any text', 'parent-a', workers=[worker])
        self.assertEqual(by_resume['agent_id'], 'agent-review')

    def test_a_review_never_goes_back_to_a_worker_that_wrote_the_code(self):
        import roll
        for banned in ('_REVIEW_RE', '_AUTHORING_RE', 'is_review_title'):
            self.assertFalse(hasattr(roll, banned), f'{banned} must be removed from roll')
        review_prof = {'work_kind': 'debug_review', 'code_complexity': 0, 'read_only_review': True}
        fix_prof = {'work_kind': 'debug_review', 'code_complexity': 2, 'read_only_review': False}
        self.assertTrue(roll.is_readonly_review_profile(review_prof))
        self.assertTrue(roll.is_readonly_review_profile({'work_kind': 'debug_review', 'code_complexity': 0}))
        self.assertFalse(roll.is_readonly_review_profile(fix_prof))
        self.assertFalse(roll.is_readonly_review_profile({'work_kind': 'general_implementation', 'code_complexity': 0}))
        implementer = {
            'agent_id': 'agent-fixer',
            'titles': ['Independent review of the s01 telegram change'],
            'profiles': [fix_prof, review_prof],
            'read_only_review': False,
        }
        reviewer = {
            'agent_id': 'agent-review',
            'titles': ['任务：修复"代码包增量更新"审查发现的问题'],
            'profiles': [review_prof],
            'read_only_review': True,
        }
        unclassified = {
            'agent_id': 'agent-unknown',
            'titles': ['Unclassified worker'],
            'profiles': [],
        }
        self.assertEqual([w['agent_id'] for w in roll.independent_reviewers([implementer, reviewer, unclassified])],
                         ['agent-review'])

    def test_definitive_create_failure_cools_the_account_down_and_a_start_clears_it(self):
        import launch_failures
        store = self.root / 'launch-failures.json'
        with patch.dict(os.environ, {'ROUTER_LAUNCH_FAILURES': str(store)}):
            row = self.prepare(self.issue())
            refused = {'ok': False, 'sent': False, 'error': 'host_creation_failed',
                       'failure_class': 'provider_refresh_timeout'}
            result = flow.launch(row['dispatch_id'], 'parent-a', host=lambda _: refused, watch_seconds=0)
            self.assertEqual(result['state'], 'failed')
            self.assertEqual(result['error'], 'host_creation_failed')
            events = registry.inspect(row['dispatch_id'])['events']
            self.assertEqual(events[-1]['reason'], 'host_rejected_before_create:host_creation_failed:provider_refresh_timeout')
            plan = allocator.plan(dict(self.task), self.usage, self.now, accounts=self.accounts)
            for candidate in plan['ranking']:
                if candidate['bundle'].get('account') == 'claude-lab':
                    self.assertTrue(any('cooling down' in e for e in candidate['excluded']), candidate['id'])
            self.assertNotEqual(plan['choice'], 'lab_sonnet55_high')
            # A pending decision for the cooling bundle is refused at prepare instead of launching again.
            with self.assertRaisesRegex(ValueError, 'cooling down'):
                self.prepare(self.issue())
            launch_failures.clear('claude-lab')
            self.launched()
            self.assertEqual(launch_failures.active(), {})

    def test_launch_failure_cooldown_scope_and_backoff(self):
        import launch_failures
        from datetime import datetime, timezone
        store = self.root / 'cooldown.json'
        t0 = datetime(2026, 11, 3, 5, 1, tzinfo=timezone.utc)
        with patch.dict(os.environ, {'ROUTER_LAUNCH_FAILURES': str(store)}):
            bundle = {'account': 'claude-2', 'model': 'claude-sonnet-5-5'}
            other = {'account': 'claude-2', 'model': 'claude-opus-5-5'}
            launch_failures.record(bundle, 'model_unavailable', now=t0)
            active = launch_failures.active(now=t0)
            self.assertIsNotNone(launch_failures.reason(bundle, active, now=t0))
            self.assertIsNone(launch_failures.reason(other, active, now=t0))   # model-scoped
            launch_failures.record(bundle, 'provider_refresh_timeout', now=t0)
            active = launch_failures.active(now=t0)
            self.assertIn('account', launch_failures.reason(other, active, now=t0))
            first = launch_failures.record(bundle, 'provider_refresh_timeout', now=t0 + timedelta(seconds=17))
            self.assertEqual(first['streak'], 2)
            until = datetime.fromisoformat(first['until'])
            self.assertEqual(until - (t0 + timedelta(minutes=launch_failures.COOLDOWN_MINUTES)), timedelta(minutes=30))
            self.assertEqual(launch_failures.active(now=until + timedelta(seconds=1)), {})
            much_later = until + timedelta(minutes=launch_failures.MAX_COOLDOWN_MINUTES + 1)
            self.assertEqual(launch_failures.record(bundle, 'provider_refresh_timeout', now=much_later)['streak'], 1)
            # Uncertain outcomes never cool anything down.
            with patch.dict(os.environ, {'ROUTER_LAUNCH_FAILURES': 'off'}):
                self.assertIsNone(launch_failures.record(bundle, 'unknown'))
                self.assertFalse(launch_failures.enabled())

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

    def test_roll_lost_create_then_unavailable_observation_is_pending_and_recovers_same_worker(self):
        import roll
        from types import SimpleNamespace
        row=self.prepare()
        calls=[]
        reachable=False
        finished=False
        def host(request):
            calls.append(request['operation'])
            notice={'status':'delivered' if finished else 'watching'}
            if request['operation']=='create':
                return {'ok':False,'sent':True,'error':'create_answer_lost','completion_notification':notice}
            if not reachable:
                return {'ok':False,'error':'observe_temporarily_unavailable'}
            return {'ok':True,'agent':self.snapshot(row,'idle' if finished else 'running',
                **({'attentionReason':'finished'} if finished else {})), 'completion_notification':notice}
        args=SimpleNamespace(title='fixture',workspace=None,wait=0)
        decision={'decision_id':row['decision_id'],'selected_bundle':self.bundle}
        with patch.object(flow,'host_call',side_effect=host), patch.object(registry,'prepare',return_value=row):
            unknown,code=roll.paseo(args,{},decision,self.task,'fixture','parent-a',self.usage)
            self.assertEqual(code,0)
            self.assertEqual(registry.inspect(row['dispatch_id'])['state'],'launching')
            self.assertIsNone(unknown['launched'])
            compact=roll.compact(unknown,roll.status_of(unknown,code),'detail.json')
            self.assertEqual(compact['status'],'pending')
            self.assertEqual(compact['next_argv'][-6:],['--dispatch-id',row['dispatch_id'],
                '--parent-id','parent-a','--watch-seconds','120'])
            self.assertNotIn('completion arrives as a notification',compact['next_action'])
            reachable=True
            recovered,code=roll.paseo(args,{},decision,self.task,'fixture','parent-a',self.usage)
            self.assertEqual(code,0)
            self.assertTrue(recovered['launched'])
            self.assertEqual(recovered['worker']['dispatch_id'],row['dispatch_id'])
            self.assertEqual(recovered['worker']['agent_id'],'agent-fixture')
            self.assertEqual(recovered['worker']['state'],'started')
            self.assertEqual(calls.count('create'),1)
            finished=True
            completed,code=roll.paseo(args,{},decision,self.task,'fixture','parent-a',self.usage)
            self.assertTrue(completed['launched'])
            self.assertEqual(completed['worker']['state'],'completed')
            self.assertEqual(completed['worker']['completion_notification']['status'],'delivered')
            self.assertEqual(calls.count('create'),1)
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

    def test_a_create_cut_off_before_the_host_says_why_the_continuation_cannot_bind(self):
        # The roll is killed while its bridge is mid-create (zero_dispatch S29 on CI): the bridge's answer never
        # arrives, and no host agent carries the dispatch labels. The continuation observes only, never creates again.
        row = self.prepare()
        calls = []

        def host(request):
            calls.append(request['operation'])
            if request['operation'] == 'create':
                return {'ok': False, 'sent': True, 'error': 'invalid_host_response'}
            self.assertIsNone(request['agent_id'])
            return {'ok': True, 'agent': None, 'missing': False}

        first = flow.launch(row['dispatch_id'], 'parent-a', host, watch_seconds=0)
        self.assertEqual((first['state'], first['agent_id']), ('launching', None))
        again = flow.launch(row['dispatch_id'], 'parent-a', host, watch_seconds=0)
        self.assertEqual(again['state'], 'launching')
        self.assertEqual(again['observe_error'], 'agent_not_observed')
        self.assertIn('continuation', again)
        self.assertEqual(calls, ['create', 'observe', 'observe'])

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
        raw = {'providers': {'claude-lab': {'fetched_at': self.now.isoformat(), 'stale': False,
                    'available': True, 'windows': self.usage['accounts']['claude-lab']['windows']}}}
        (self.root / 'usage.json').write_text(json.dumps(raw))
        packet = self.root / 'packet.json'
        script = str(Path(route_judge.__file__))
        def cli(*args):
            result = subprocess.run([sys.executable, '-B', script, *args], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout)
        self.task['constraints'] = {'bundle': 'lab_sonnet55_high'}
        task_file = self.root / 'task.json'
        task_file.write_text(json.dumps({'task': self.task}))
        decision = cli('plan', '--input', str(task_file))
        self.assertTrue(decision['bound'])
        self.assertEqual(decision['choice'], 'lab_sonnet55_high')
        self.assertEqual(registry.reservations(), [])
        packet.write_text(json.dumps({'decision_id': decision['decision_id'], 'task': self.task, 'launch': self.launch}))
        prepared = cli('dispatch', '--input', str(packet))
        fake = self.root / 'fake-host.mjs'
        fake.write_text("""import fs from 'node:fs';
const p=JSON.parse(fs.readFileSync(0,'utf8')); const f=process.env.FAKE_HOST_STATE;
if(p.operation==='create') {
 const agent={id:p.dispatch_id,provider:'claude-lab',model:'claude-sonnet-5-5',status:'running',
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


def roll_module_for_tests():
    import roll
    return roll


class RollTests(unittest.TestCase):
    """roll: brief in, routed launch out. Reuses the isolated registry; no live host, Dot or scorer."""
    setUp, snapshot = DispatchTests.setUp, DispatchTests.snapshot

    def roll(self, decision, parent='parent-a', reachable=False, env=None, **flags):
        import argparse
        import roll
        brief = self.root / 'task.md'
        brief.write_bytes(self.brief.encode('utf-8'))
        args = argparse.Namespace(brief=brief, file=[], apply_to=None, local_only=None, hours=0.5, title=None,
            workspace=None, task_id='test-task', wait=0, parent_id=None, parent_host=None, parent_address=None, provider=None, model=None, effort=None,
            basis=None, usage_cache=None, dry_run=False, verbose='task' not in flags)    # tests of the full result ask for it; inline-task tests check the short one
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
                patch.object(roll, 'RESULTS_DIR', self.root / 'roll-results'), \
                patch.object(flow, 'paseo_reachable', return_value=reachable):
            return roll.run(args, judge), seen

    brief = '# Fixture brief\n\nKeep only the current rules.\n'

    def assert_worker_prompt(self, result, parent=None):
        import roll
        prompt = Path(result['worker_prompt_file']).read_bytes().decode('utf-8')
        self.assertTrue(prompt.startswith(self.brief + '\n'))
        self.assertIn(roll.WORKER_COMMUNICATION.read_text(encoding='utf-8'), prompt)
        if parent:
            self.assertIn(f'Parent session agent ID: `{parent}`.', prompt)
        self.assertEqual((self.root / 'task.md').read_bytes().decode('utf-8'), self.brief)
        return prompt

    def test_paseo_route_launches_the_brief_with_communication_contract_and_routed_bundle(self):
        calls = []

        def host(request):
            calls.append(request)
            if request['operation'] == 'create':
                return {'ok': True, 'agent': self.snapshot({'request': request['request']})}
            return {'ok': True, 'agent': None}
        decision = dict(stage='plan', choice='lab_sonnet55_high', selected_bundle=self.bundle, decision={'action': 'start'})
        with patch.object(flow, 'host_call', host):
            (result, code), seen = self.roll(decision)
        self.assertEqual(code, 0)
        self.assertTrue(result['launched'])
        self.assertEqual(seen['task']['objective'], self.brief)
        request = calls[0]['request'] if calls[0]['operation'] == 'create' else next(
            c['request'] for c in calls if c['operation'] == 'create')
        self.assertEqual(request['initialPrompt'], self.assert_worker_prompt(result, parent='parent-a'))
        self.assertEqual(request['title'], 'Fixture brief')
        self.assertEqual(request['provider'], 'claude-lab/' + (self.bundle.get('launch_model') or self.bundle['model']))
        self.assertEqual(request['settings']['thinkingOptionId'], self.bundle['effort'])

    def test_inline_task_without_hours_gets_a_compact_result_and_states_the_assumption(self):
        def host(request):
            return {'ok': True, 'agent': self.snapshot({'request': request['request']}) if request['operation'] == 'create' else None}
        decision = dict(stage='plan', choice='lab_sonnet55_high', selected_bundle=self.bundle,
                        decision={'action': 'start'},
                        hours_estimate={'hours': 0.4, 'source': 'classifier_estimate', 'confidence': 'medium'})
        with patch.object(flow, 'host_call', host):
            (result, code), seen = self.roll(decision, brief=None, task='Rename foo to bar in util.py', hours=None, task_id=None)
        self.assertEqual(code, 0)
        self.assertEqual((result['status'], result['worker']['model']), ('launched', self.bundle['model']))
        self.assertEqual(set(result) - {'worker', 'reason'}, {'status', 'next_action', 'next_argv', 'assumptions', 'details'})
        self.assertIn('No completion notification is confirmed', result['next_action'])
        self.assertIn('budgeted 0.4h from the classifier estimate, confidence medium', result['assumptions'][0])
        self.assertEqual(seen['task']['objective'], 'Rename foo to bar in util.py')
        self.assertNotIn('expected_hours', seen['task'])
        self.assertEqual(seen['task']['hours_source'], 'classifier_difficulty')
        full = json.loads(Path(result['details']).read_text(encoding='utf-8'))
        self.assertTrue(full['launched'] and 'worker' in full)

    def test_long_multiline_unicode_task_reaches_the_worker_whole_through_stdin(self):
        import io
        import roll as roll_module
        line = '第{}行：修复“解析器”\t\\路径 `code` $HOME %PATH% "quote" \'x\' 🙂\r\n'
        text = ''.join(line.format(i) for i in range(4000)) + '  尾部空白  \n\n'
        self.assertGreater(len(text), 100_000)  # far beyond any command-line limit
        decision = dict(stage='plan', choice='self', decision={'action': 'start'})
        stdin = type('S', (), {'buffer': io.BytesIO(text.encode('utf-8'))})()
        with patch.object(roll_module.sys, 'stdin', stdin):
            (result, code), seen = self.roll(decision, brief=None, task='-', hours=None, task_id=None)
        self.assertEqual(seen['task']['objective'], text)  # the classifier gets the whole text, not a head/tail view
        self.assertNotIn('omitted', json.dumps(result, ensure_ascii=False))

    def test_worker_prompt_and_brief_file_keep_the_original_bytes(self):
        import argparse
        import roll as roll_module
        text = ('长文本\r\n第二行\n' * 5000) + '  末尾  \n'
        with patch.object(roll_module, 'WORKER_PROMPTS_DIR', self.root / 'wp'):
            full, path = roll_module.prepare_worker_prompt(text, 'parent-a')
        saved = path.read_bytes().decode('utf-8')
        self.assertTrue(saved.startswith(text))
        self.assertEqual(hashlib.sha256(saved[:len(text)].encode()).hexdigest(), hashlib.sha256(text.encode()).hexdigest())
        brief = self.root / 'crlf.md'
        brief.write_bytes(text.encode('utf-8'))
        self.assertEqual(roll_module.read_task_text(argparse.Namespace(brief=brief, task=None)), text)

    def test_verbose_keeps_the_full_result(self):
        decision = dict(stage='plan', choice='self', decision={'action': 'start'})
        (result, code), seen = self.roll(decision, hours=2)
        self.assertEqual((code, result['status'], result['dispatch_mode'], seen['task']['expected_hours']), (2, 'self', 'self', 2.0))
        self.assertNotIn('assumptions', result)

    def test_a_brief_gets_the_same_short_result_as_inline_text(self):
        decision = dict(stage='plan', choice='self', decision={'action': 'start'})
        with patch.object(roll_module_for_tests(), 'RESULTS_DIR', self.root / 'results'):
            (result, code), _ = self.roll(decision, verbose=False)
        self.assertEqual((code, result['status']), (2, 'self'))
        self.assertEqual(sorted(result), ['details', 'next_action', 'status'])
        self.assertTrue(Path(result['details']).is_file())
        self.assertEqual(json.loads(Path(result['details']).read_text(encoding='utf-8'))['dispatch_mode'], 'self')

    def test_status_never_reports_uncertain_delivery_as_not_launched(self):
        import roll as roll_module
        self.assertEqual(roll_module.status_of({'launched': None, 'caller_action': 'check_delivery_do_not_resend'}, 1), 'pending')
        self.assertEqual(roll_module.status_of({'launched': False, 'dispatch_mode': 'none'}, 2), 'blocked')
        self.assertEqual(roll_module.status_of({'launched': False, 'dispatch_mode': 'paseo'}, 1), 'error')
        compact = roll_module.compact({'launched': None, 'worker': {'continuation_argv': ['x', 'status']}, 'route': {}}, 'pending', 'p')
        self.assertEqual((compact['next_argv'], 'rolling' in compact['next_action'] or 'roll again' in compact['next_action']), (['x', 'status'], True))

    def test_rolling_the_same_task_again_returns_its_dispatch_without_classifying_or_launching(self):
        creates = []

        def host(request):
            if request['operation'] == 'create':
                creates.append(request)
                return {'ok': True, 'agent': self.snapshot({'request': request['request']})}
            return {'ok': True, 'agent': None}
        decision = dict(stage='plan', choice='lab_sonnet55_high', selected_bundle=self.bundle, decision={'action': 'start'})
        with patch.object(flow, 'host_call', host):
            (first, _), _ = self.roll(decision)
            (again, code), seen = self.roll(decision)
        self.assertEqual((len(creates), seen, code), (1, {}, 0))  # one create, no second classification
        self.assertEqual((first['launched'], again['launched'], again['caller_action']), (True, None, 'run_next_argv'))
        self.assertEqual(again['worker']['continuation_argv'][4:9], ['launch', '--dispatch-id', again['worker']['dispatch_id'], '--parent-id', 'parent-a'])
        self.assertFalse(any(part.endswith('.py') for part in again['worker']['continuation_argv']))
        self.assertEqual(again['status'], 'pending')

    def test_compact_states_say_exactly_what_the_caller_must_do(self):
        import roll as roll_module
        bundle = {'provider': 'claude', 'account': 'a', 'model': 'm', 'effort': 'high', 'modeId': 'bypassPermissions'}
        route = {'bundle': bundle, 'reason': 'r'}
        native = roll_module.compact({'route': route, 'worker_prompt_file': 'p.md', 'dispatch_mode': 'native', 'next': 'long'}, 'native', 'd')
        self.assertEqual((native['route_bundle'], native['worker_prompt_file']), (bundle, 'p.md'))
        self.assertIn('modeId', native['next_action'])
        running = roll_module.compact({'route': route, 'dispatch_mode': 'cli', 'launched': True,
                                       'worker': {'pid': 7, 'exit': None, 'output': 'o.txt', 'stderr': 'e.txt'}}, 'launched', 'd')
        self.assertIn('PID 7', running['next_action'])
        self.assertEqual((running['worker_output'], running['worker_stderr']), ('o.txt', 'e.txt'))
        self.assertNotIn('notification', running['next_action'])
        failed = roll_module.compact({'route': route, 'launched': False, 'error': 'upstream said no'}, 'error', 'd')
        self.assertEqual(failed['cause'], 'upstream said no')

    def test_several_briefs_are_classified_together_then_routed_one_after_another(self):
        import argparse
        import roll
        briefs = []
        for n in (1, 2):
            path = self.root / f'line{n}.md'
            path.write_text(f'# Line {n}')
            briefs.append(path)
        order, prefetched = [], []

        def plan_stage(document, _):
            order.append(document['task']['objective'])
            return dict(stage='plan', choice='self', selected_bundle={}, decision={'action': 'self'}), 0
        judge = type('Judge', (), {'plan_stage': staticmethod(plan_stage),
                                   'prefetch_scores': staticmethod(lambda tasks, timeout: prefetched.append(
                                       [t['objective'] for t in tasks]))})
        args = argparse.Namespace(brief=briefs, task=None, file=[], apply_to=None, local_only=None, keep_whole=None,
                                  hours=None, title=None, workspace=None, task_id=None, wait=0, parent_id=None,
                                  parent_host=None, parent_address=None, provider=None, account=None, model=None,
                                  effort=None, basis=None, usage_cache=None, dry_run=False, verbose=False, fast=False,
                                  score_timeout=30)
        with patch.dict(os.environ, {'PASEO_AGENT_ID': 'parent-a'}), patch.object(roll, 'RESULTS_DIR', self.root / 'r'):
            result, code = roll.run_batch(args, judge)
            self.assertEqual(prefetched, [['# Line 1', '# Line 2']])
            self.assertEqual(order, ['# Line 1', '# Line 2'])
            self.assertEqual((result['status'], [r['status'] for r in result['results']], code), ('batch', ['self', 'self'], 2))
            with self.assertRaisesRegex(ValueError, '--hours'):
                roll.run_batch(argparse.Namespace(**{**vars(args), 'hours': 2.0}), judge)
            with self.assertRaisesRegex(ValueError, 'twice'):
                roll.run_batch(argparse.Namespace(**{**vars(args), 'brief': [briefs[0], briefs[0]]}), judge)

    def test_a_multi_task_brief_is_handed_back_to_the_parent_unlaunched(self):
        decision = dict(stage='split', choice='split', task_count='3', reason='three deliverables', next='split it')
        with patch.object(flow, 'host_call', side_effect=AssertionError('a split brief must not launch anything')):
            (res, code), _ = self.roll(decision, verbose=False)
        self.assertEqual((res['status'], code), ('split', 2))
        self.assertEqual((res['task_count'], res['next_action'], res['reason']), ('3', 'split it', 'three deliverables'))
        self.assertNotIn('worker', res)

    def test_ready_grok_acp_launcher_launches_through_paseo_not_the_cli_fallback(self):
        import roll as roll_module
        (self.root / '.paseo' / 'config.json').write_text(json.dumps({'agents': {'providers': {
            'grok': {'extends': 'acp', 'enabled': True, 'command': ['grok-fixture', 'stdio']}}}}))
        bundle = {'provider': 'grok', 'account': 'grok', 'model': 'grok-4.7', 'effort': 'high', 'bundle_id': 'grok47_high'}
        decision = dict(stage='plan', choice='grok47_high', selected_bundle=bundle, decision={'action': 'start'})
        (self.root / 'host.json').write_text(json.dumps({'allowed_accounts': ['grok']}))
        for status, launched in (('ready', True), ('disabled', False)):
            calls = []

            def host(request):
                calls.append(request)
                if request['operation'] == 'create':
                    return {'ok': True, 'agent': self.snapshot({'request': request['request']}, provider='grok',
                                                               model='grok-4.7', currentModeId=None)}
                return {'ok': True, 'agent': None}
            inventory = {'accounts': {'grok': {'family': 'grok', 'provider': 'grok', 'sources': ['native', 'paseo', 'usage-cache'],
                                               'status': status, 'plan': 'subscription', 'billing': 'subscription'}},
                         'launchers': {'grok': {'family': 'grok', 'status': status}}}
            with self.subTest(status), patch.object(allocator.provider_inventory, 'current', return_value=inventory), \
                    patch.object(flow, 'host_call', host), \
                    patch.object(roll_module.subprocess, 'Popen', side_effect=AssertionError('must not use the CLI')):
                if launched:
                    (result, code), _ = self.roll(decision)
                    request = next(c['request'] for c in calls if c['operation'] == 'create')
                    self.assertEqual((result['launched'], result['dispatch_mode'], code), (True, 'paseo', 0))
                    self.assertEqual(request['provider'], 'grok/grok-4.7')
                    self.assertEqual(request['settings'], {'thinkingOptionId': 'high'})
                else:
                    (result, code), _ = self.roll(decision, task_id='second-task')  # the same task would return the first dispatch
                    self.assertEqual((result['launched'], result['dispatch_mode'], calls), (False, 'native', []))

    FAKE_GROK = (
        "import json, os, sys\n"
        "out = {'argv': sys.argv[1:], 'cwd': os.getcwd(), 'stdin': sys.stdin.read(), 'env': dict(os.environ),\n"
        "       'prompt': open(sys.argv[sys.argv.index('--prompt-file') + 1], encoding='utf-8').read()}\n"
        "open(os.environ['FAKE_GROK_RECORD'], 'w', encoding='utf-8').write(json.dumps(out))\n"
        "print('grok reply')\n"
        "sys.exit(int(os.environ.get('FAKE_GROK_EXIT', '0')))\n")

    def grok_roll(self, command, **flags):
        import roll as roll_module
        (self.root / '.paseo' / 'config.json').write_text(json.dumps({'agents': {'providers': {
            'grok': {'extends': 'acp', 'enabled': True, 'env': {'GROK_FIXTURE_ACCOUNT': 'bound'}}}}}))
        (self.root / 'host.json').write_text(json.dumps({'allowed_accounts': ['grok']}))
        bundle = {'provider': 'grok', 'account': 'grok', 'model': 'grok-4.7', 'effort': 'high', 'bundle_id': 'grok47_high'}
        decision = dict(stage='plan', choice='grok47_high', selected_bundle=bundle, decision={'action': 'start'})
        inventory = {'accounts': {}, 'launchers': {'grok': {'family': 'grok', 'status': 'ready'}}}
        host = {'PASEO_HOME': str(self.root / '.paseo'), 'CLAUDECODE': '1', 'CODEX_HOME': 'C:/host/.codex-work',
                'FAKE_GROK_RECORD': str(self.root / 'grok-record.json')}
        with patch.object(allocator.provider_inventory, 'current', return_value=inventory), \
                patch.object(roll_module, 'CLI_RUNS_DIR', self.root / 'runs'), \
                patch.object(flow, 'host_call', side_effect=AssertionError('no Paseo')), \
                patch.object(roll_module, 'cli_command', command):
            return self.roll(decision, parent='', parent_id='parent-a', parent_host='paseo', env=host, **flags)[0]

    def test_no_daemon_grok_runs_headless_with_prompt_file_on_its_bound_account(self):
        fake = self.root / 'fake_grok.py'
        fake.write_text(self.FAKE_GROK, encoding='utf-8')
        seen = []

        def command(family, provider, judge):
            seen.append((family, provider))
            return [sys.executable, '-I', str(fake)]
        for label, exit_code, expected in (('success', '0', 0), ('failed run keeps its reply and exit', '3', 1)):
            with self.subTest(label), patch.dict(os.environ, {'FAKE_GROK_EXIT': exit_code}):
                result, code = self.grok_roll(command, wait=30)
            record = json.loads((self.root / 'grok-record.json').read_text(encoding='utf-8'))
            prompt = self.assert_worker_prompt(result, parent='parent-a')
            run_dir = Path(result['worker']['output']).parent
            self.assertEqual((result['launched'], result['dispatch_mode'], code), (True, 'cli', expected))
            self.assertEqual(result['caller_action'], 'already_launched_do_not_respawn')
            self.assertEqual(seen[-1], ('grok', 'grok'))
            self.assertEqual(record['argv'], ['--always-approve', '-m', 'grok-4.7', '--reasoning-effort', 'high',
                                              '--cwd', str(Path.cwd()), '--prompt-file', str(run_dir / 'brief.md')])
            self.assertEqual((record['cwd'], record['stdin'], record['prompt']), (str(Path.cwd()), '', prompt))
            self.assertEqual(record['env']['GROK_FIXTURE_ACCOUNT'], 'bound')
            self.assertEqual((record['env']['ROUTER_WORKER'], record['env']['ROUTER_PARENT_AGENT_ID']), ('1', 'parent-a'))
            self.assertFalse({'CLAUDECODE', 'CODEX_HOME'} & set(record['env']))
            self.assertEqual((result['worker']['exit'], result['worker']['reply'].strip()), (int(exit_code), 'grok reply'))

    def test_grok_launch_failure_reports_native_handoff_without_a_second_start(self):
        import roll as roll_module
        starts = []

        def missing(command, **_):
            starts.append(command)
            raise FileNotFoundError(command[0])

        def not_installed(*_):
            raise RuntimeError('Subscription CLI is not installed.')
        for label, command in (('no installed CLI', not_installed), ('executable vanished', lambda *_: ['grok-missing'])):
            with self.subTest(label), patch.object(roll_module.subprocess, 'Popen', missing):
                result, code = self.grok_roll(command, wait=30)
            self.assertEqual((result['launched'], result['dispatch_mode'], code), (False, 'native', 1))
            self.assertIn('worker_prompt_file', result['next'])
        self.assertEqual(starts, [['grok-missing', '--always-approve', '-m', 'grok-4.7', '--reasoning-effort', 'high',
                                   '--cwd', str(Path.cwd()), '--prompt-file', starts[0][-1]]])

    def test_grok_cli_command_uses_the_providers_configured_executable_then_discovery(self):
        import roll as roll_module
        bin_dir = self.root / 'grok-bin'
        bin_dir.mkdir()
        agent, grok = bin_dir / 'agent.exe', bin_dir / 'grok.exe'
        agent.write_bytes(b'')
        judge = type('Judge', (), {'cli_prefix': staticmethod(lambda name: ['discovered-' + name])})

        def configure(providers):
            (self.root / '.paseo' / 'config.json').write_text(json.dumps({'agents': {'providers': providers}}))
        with patch.dict(os.environ, {'PASEO_HOME': str(self.root / '.paseo')}):
            configure({'grok': {'extends': 'acp', 'command': [str(agent), 'agent', '--always-approve', 'stdio']},
                       'grok-2': {'extends': 'grok'}})
            self.assertEqual(roll_module.cli_command('grok', 'grok', judge), [str(agent)])
            grok.write_bytes(b'')  # the sibling grok binary is the CLI when the definition names agent
            self.assertEqual(roll_module.cli_command('grok', 'grok-2', judge), [str(grok)])
            agent.unlink()
            grok.unlink()
            with self.assertRaisesRegex(RuntimeError, 'configured for .grok.*missing'):
                roll_module.cli_command('grok', 'grok', judge)
            for command in (['wrapper.cmd', 'stdio'], ['npx', '-y', 'some-grok-acp']):
                configure({'grok': {'extends': 'acp', 'command': command}})
                with self.assertRaisesRegex(RuntimeError, 'not a Grok executable'):
                    roll_module.cli_command('grok', 'grok', judge)
            bare = self.root / 'on-path' / 'agent.exe'
            bare.parent.mkdir()
            bare.write_bytes(b'')
            configure({'grok': {'extends': 'acp', 'command': ['agent', 'stdio']}})
            with patch.object(roll_module.shutil, 'which', lambda name: str(bare) if name == 'agent' else None):
                self.assertEqual(roll_module.cli_command('grok', 'grok', judge), [str(bare)])
            (self.root / 'agent').write_bytes(b'')  # a file in the worker's cwd is never the configured command
            here = os.getcwd()
            os.chdir(self.root)
            self.addCleanup(os.chdir, here)
            with patch.object(roll_module.shutil, 'which', return_value=None),                     self.assertRaisesRegex(RuntimeError, 'missing: agent'):
                roll_module.cli_command('grok', 'grok', judge)
            os.chdir(here)
            configure({'grok': {'extends': 'acp'}})
            self.assertEqual(roll_module.cli_command('grok', 'grok', judge), ['discovered-grok'])
            self.assertEqual(roll_module.cli_command('codex', 'codex', judge), ['discovered-codex'])

    def test_routes_that_cannot_launch_here_do_not_launch(self):
        paseo = dict(stage='plan', choice='lab_sonnet55_high', selected_bundle=self.bundle, decision={'action': 'start'})
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

    def test_roll_inside_orca_and_t3_hands_back_the_host_command_without_paseo(self):
        decision = dict(stage='plan', choice='lab_sonnet55_high', selected_bundle=self.bundle, decision={'action': 'start'})
        cases = (({'ORCA_AGENT_SESSION_ID': 'orca-sess', 'ORCA_TERMINAL_HANDLE': 'term_1'}, {}, 'orca', 'orca_session_id:orca-sess', 'worker-start'),
                 ({'CODEX_THREAD_ID': 'thread-7'}, {'parent_host': 't3'}, 't3', 'thread-7', 'delegate_task'))
        for env, flags, host, address, command in cases:
            with self.subTest(host=host), patch.object(flow, 'host_call', side_effect=AssertionError('must not use Paseo')):
                (result, code), _ = self.roll(decision, parent='', env=env, reachable=True, **flags)
            self.assertEqual((result['launched'], result['dispatch_mode'], code), (False, 'native', 0))
            self.assertEqual((result['parent_host'], result['parent_address']), (host, address))
            self.assertIn(command, result['next'])
            self.assertIn(f'Parent host: `{host}`. Parent message address: `{address}`.', self.assert_worker_prompt(result))

    def test_explicit_parent_and_native_address_survive_native_handoff(self):
        decision = dict(stage='plan', choice='lab_sonnet55_high', selected_bundle=self.bundle, decision={'action': 'start'})
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
            'claude-lab': {'extends': 'claude', 'env': {'CLAUDE_CONFIG_DIR': 'C:/fixture/.claude-lab'}}}}}))
        host = {'PASEO_HOME': str(self.root / 'paseo'), 'CLAUDECODE': '1', 'CLAUDE_CONFIG_DIR': 'C:/host/.claude-lab',
                'CODEX_HOME': 'C:/host/.codex-work'}
        bundles = allocator.bundles(self.accounts)
        brief = ['--print']
        cases = (('claude-lab via its Paseo definition', 'lab_sonnet55_high', 0,
                  ['claude', '-p', '--model', 'claude-sonnet-5-5', '--effort', 'high', '--permission-mode', 'bypassPermissions', '--settings', '{"fastMode": false}'],
                  'C:/fixture/.claude-lab', self.brief),
                 ('claude does not inherit the caller account', 'sonnet55_high', 5,
                  ['claude', '-p', '--model', 'claude-sonnet-5-5', '--effort', 'high', '--permission-mode', 'bypassPermissions', '--settings', '{"fastMode": false}'],
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
        std_decision = dict(stage='plan', choice='lab_sonnet55_high', selected_bundle=self.bundle, decision={'action': 'start'})
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
        self.assertEqual(result['continuation_argv'][4:8], ['dot', 'status', '--task-id', 'dot-task-1'])
        self.assertFalse(any(part.endswith('.py') for part in result['continuation_argv']))

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
                if not wait:
                    self.assertEqual(command[4], 'chatgpt-web')
                self.assert_worker_prompt(result, parent='parent-a')

    def test_named_model_without_user_quote_is_refused(self):
        with self.assertRaisesRegex(ValueError, '--basis'):
            self.roll({}, model='claude-opus-5-5')

    def test_non_positive_hours_is_refused(self):
        for bad in (0, -1, False, float("inf")):
            with self.subTest(hours=bad), self.assertRaisesRegex(ValueError, '--hours'):
                self.roll({}, hours=bad)

    def test_opencode_deepseek_explicit_override_prepares_paseo_request(self):
        (self.root / 'host.json').write_text(json.dumps({'allowed_accounts': ['claude-lab', 'codex', 'antigravity', 'opencode']}))
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
                                  'local_only': True, 'wants_fast': False, 'read_only_review': False,
                                  'interaction_style': 'engineering', 'multi_task': False, 'task_count': '1',
                                  'reason': 'test'}}
        normal_plan = allocator.plan(codex_task, codex_usage, self.now, accounts=self.accounts)
        self.assertEqual(normal_plan['choice'], 'sol_high')
        normal_dec = registry.issue(dict(normal_plan, stage='plan'), codex_task, self.now)
        normal_row = registry.prepare(normal_dec['decision_id'], codex_task, self.launch, codex_usage, self.accounts, self.now)
        self.assertEqual(normal_row['request']['settings']['features'], {'service_tier': 'default'})

        fast_task = {**codex_task, 'task_id': 'task-fast-1', 'needs': {**codex_task['needs'], 'fast': True}}
        fast_plan = allocator.plan(fast_task, codex_usage, self.now, accounts=self.accounts)
        self.assertTrue(fast_plan['selected_bundle'].get('fast'))
        fast_dec = registry.issue(dict(fast_plan, stage='plan'), fast_task, self.now)
        fast_row = registry.prepare(fast_dec['decision_id'], fast_task, self.launch, codex_usage, self.accounts, self.now)
        self.assertEqual(fast_row['request']['settings'],
                         {'modeId': 'full-access', 'thinkingOptionId': 'high', 'features': {'service_tier': 'priority'}})
        self.assertAlmostEqual(fast_row['expected_usd'], normal_row['expected_usd'] * 2.0, delta=0.02)

    def test_followup_brief_resumes_completed_worker_and_new_worker_flag_forces_create(self):
        operations = []
        active_labels = {}

        def host(request):
            op = request['operation']
            operations.append((op, request.get('agent_id'), request.get('dispatch_id')))
            if op in ('create', 'resume'):
                active_labels.clear()
                active_labels.update(request['request']['labels'])
                return {'ok': True, 'agent': self.snapshot(
                    {'request': request['request']}, status='running', id='agent-fixture')}
            return {'ok': True, 'agent': self.snapshot(
                {'request': {'labels': dict(active_labels)}},
                status='idle', attentionReason='finished', id='agent-fixture')}

        def real_plan_stage(document, _):
            task = dict(document['task'])
            task.setdefault('work_kind', 'exploration_analysis')
            task.setdefault('difficulty', 'hard')
            planned = allocator.plan(task, self.usage, self.now, accounts=self.accounts)
            res = dict(planned, stage='plan', _allocation_task=task)
            res.update(registry.issue(res, document['task'], self.now, allocation_task=task))
            return res, 0

        import argparse
        import roll
        b1 = self.root / 'step1.md'
        b2 = self.root / 'step1-rev.md'
        b3 = self.root / 'step1-fresh.md'
        b1.write_text('# Step 1: Initial implementation\n', encoding='utf-8')
        b2.write_text('# Step 1-Rev: Address review comments\n', encoding='utf-8')
        b3.write_text('# Step 1-Fresh: Independent task\n', encoding='utf-8')
        judge = type('Judge', (), {'plan_stage': staticmethod(real_plan_stage),
                                   'usage_snapshot': staticmethod(lambda _: self.usage),
                                   'subscription_env': staticmethod(lambda account=None: dict(os.environ)),
                                   'cli_prefix': staticmethod(lambda name: [name])})
        base_args = dict(task=None, file=[], apply_to=None, local_only='local daemon test', keep_whole=None,
                         hours=0.5, title=None, workspace=None, task_id=None, wait=0, parent_id=None,
                         parent_host=None, parent_address=None, provider=None, account=None, model=None,
                         effort=None, basis=None, usage_cache=None, dry_run=False, verbose=True, fast=False,
                         resume=None, new_worker=False)
        with patch.dict(os.environ, {'PASEO_AGENT_ID': 'parent-a'}), \
                patch.object(roll, 'WORKER_PROMPTS_DIR', self.root / 'wp'), \
                patch.object(roll, 'RESULTS_DIR', self.root / 'rr'), \
                patch.object(flow, 'host_call', host):
            res1, code1 = roll.run(argparse.Namespace(brief=b1, **base_args), judge)
            self.assertEqual((code1, res1['launched'], res1['worker']['state']), (0, True, 'completed'))
            self.assertEqual([op for op, _, _ in operations], ['create', 'observe'])

            # A different brief path with stem/token overlap ('step1-rev.md') does NOT auto-resume without --resume
            operations.clear()
            res_unmatched, code_unmatched = roll.run(argparse.Namespace(brief=b2, **base_args), judge)
            self.assertEqual((code_unmatched, res_unmatched['launched']), (0, True))
            self.assertEqual(operations[0][0], 'create')
            self.assertFalse(res_unmatched['worker'].get('resumed', False))

            # Explicit --resume (or exact brief_path/task_id) resumes the worker
            operations.clear()
            res2, code2 = roll.run(argparse.Namespace(brief=b2, **{**base_args, 'resume': 'agent-fixture'}), judge)
            self.assertEqual((code2, res2['launched'], res2['worker']['state']), (0, True, 'completed'))
            self.assertEqual(operations[0][:2], ('resume', 'agent-fixture'))
            self.assertTrue(res2['worker'].get('resumed'))
            self.assertEqual(res2['worker']['agent_id'], 'agent-fixture')
            # Resumed turns carry a slim initialPrompt (brief + parent address footer, no repeated contract)
            contract_text = roll.WORKER_COMMUNICATION.read_text(encoding='utf-8').strip()
            resume_req = registry.inspect(operations[0][2])['request']
            self.assertIn('# Step 1-Rev: Address review comments', resume_req.get('initialPrompt', ''))
            self.assertIn('Parent session agent ID: `parent-a`.', resume_req.get('initialPrompt', ''))
            self.assertNotIn(contract_text, resume_req.get('initialPrompt', ''))
            # Calling prepare_worker_prompt twice on already-injected text does not duplicate the contract
            once, _ = roll.prepare_worker_prompt('Do work', 'parent-a')
            twice, _ = roll.prepare_worker_prompt(once, 'parent-a')
            self.assertEqual(once, twice)
            self.assertEqual(twice.count('Parent session agent ID: `parent-a`.'), 1)

            # --new-worker forces a fresh create even when brief_path matches
            operations.clear()
            res3, code3 = roll.run(argparse.Namespace(brief=b1, **{**base_args, 'new_worker': True}), judge)
            self.assertEqual((code3, res3['launched']), (0, True))
            self.assertEqual(operations[0][0], 'create')
            self.assertFalse(res3['worker'].get('resumed', False))

    def test_followup_brief_rotates_worn_worker_at_two_million_tokens(self):
        operations = []
        active_labels = {}

        def host(request):
            op = request['operation']
            operations.append((op, request.get('agent_id')))
            if op in ('create', 'resume'):
                active_labels.clear()
                active_labels.update(request['request']['labels'])
                return {'ok': True, 'agent': self.snapshot(
                    {'request': request['request']}, status='running', id='agent-worn')}
            return {'ok': True, 'agent': self.snapshot(
                {'request': {'labels': dict(active_labels)}},
                status='idle', attentionReason='finished', id='agent-worn')}

        def real_plan_stage(document, _):
            task = dict(document['task'])
            task.setdefault('work_kind', 'exploration_analysis')
            task.setdefault('difficulty', 'hard')
            planned = allocator.plan(task, self.usage, self.now, accounts=self.accounts)
            res = dict(planned, stage='plan', _allocation_task=task)
            res.update(registry.issue(res, document['task'], self.now, allocation_task=task))
            return res, 0

        import argparse
        import roll
        b1 = self.root / 'turn1.md'
        b1.write_text('# Turn 1: Heavy exploration\n', encoding='utf-8')
        judge = type('Judge', (), {'plan_stage': staticmethod(real_plan_stage),
                                   'usage_snapshot': staticmethod(lambda _: self.usage),
                                   'subscription_env': staticmethod(lambda account=None: dict(os.environ)),
                                   'cli_prefix': staticmethod(lambda name: [name])})
        base_args = dict(task=None, file=[], apply_to=None, local_only='local daemon test', keep_whole=None,
                         hours=0.5, title=None, workspace=None, task_id=None, wait=0, parent_id=None,
                         parent_host=None, parent_address=None, provider=None, account=None, model=None,
                         effort=None, basis=None, usage_cache=None, dry_run=False, verbose=True, fast=False,
                         resume=None, new_worker=False)
        worn_state = {'compactions': 3, 'compacted_tokens': 1_800_000,
                      'context_tokens': 300_000, 'historical_context_tokens': 2_100_000}
        with patch.dict(os.environ, {'PASEO_AGENT_ID': 'parent-a'}), \
                patch.object(roll, 'WORKER_PROMPTS_DIR', self.root / 'wp'), \
                patch.object(roll, 'RESULTS_DIR', self.root / 'rr'), \
                patch.object(flow, 'host_call', host):
            roll.run(argparse.Namespace(brief=b1, **base_args), judge)
            operations.clear()
            with patch.object(allocator, 'session_state', return_value=worn_state):
                # Explicit --resume on a worn worker must raise ValueError rather than silently creating a new worker
                with self.assertRaisesRegex(ValueError, '--resume'):
                    roll.run(argparse.Namespace(brief=b1, **{**base_args, 'resume': 'agent-worn'}), judge)
                # Automatic reuse via exact brief_path rotates to a fresh worker
                res2, code2 = roll.run(argparse.Namespace(brief=b1, **base_args), judge)
            self.assertEqual((code2, res2['launched']), (0, True))
            self.assertEqual(operations[0][0], 'create')
            self.assertFalse(res2['worker'].get('resumed', False))

    def test_batch_revision_briefs_resume_matching_workers_without_collision(self):
        operations = []
        agent_labels = {}
        created_count = 0

        def host(request):
            nonlocal created_count
            op = request['operation']
            if op == 'create':
                created_count += 1
                aid = f'worker-1{"a" if created_count == 1 else "b"}'
                agent_labels[aid] = dict(request['request']['labels'])
                operations.append(('create', aid, request['request']['title']))
                return {'ok': True, 'agent': self.snapshot({'request': request['request']}, status='running', id=aid)}
            if op == 'resume':
                aid = request['agent_id']
                agent_labels[aid] = dict(request['request']['labels'])
                operations.append(('resume', aid, request['request']['title']))
                return {'ok': True, 'agent': self.snapshot({'request': request['request']}, status='running', id=aid)}
            aid = request.get('agent_id')
            return {'ok': True, 'agent': self.snapshot(
                {'request': {'labels': dict(agent_labels[aid])}},
                status='idle', attentionReason='finished', id=aid)}

        def real_plan_stage(document, _):
            task = dict(document['task'])
            task.setdefault('work_kind', 'exploration_analysis')
            task.setdefault('difficulty', 'hard')
            planned = allocator.plan(task, self.usage, self.now, accounts=self.accounts)
            res = dict(planned, stage='plan', _allocation_task=task)
            res.update(registry.issue(res, document['task'], self.now, allocation_task=task))
            return res, 0

        import argparse
        import roll
        b1a = self.root / 'brief-step1a.md'
        b1b = self.root / 'brief-step1b.md'
        b1a.write_text('# Step 1A: Refactor auth module\n', encoding='utf-8')
        b1b.write_text('# Step 1B: Refactor storage module\n', encoding='utf-8')
        judge = type('Judge', (), {'plan_stage': staticmethod(real_plan_stage),
                                   'prefetch_scores': staticmethod(lambda tasks, timeout: None),
                                   'usage_snapshot': staticmethod(lambda _: self.usage),
                                   'subscription_env': staticmethod(lambda account=None: dict(os.environ)),
                                   'cli_prefix': staticmethod(lambda name: [name])})
        batch_args = dict(task=None, file=[], apply_to=None, local_only=None, keep_whole=None,
                          hours=None, title=None, workspace=None, task_id=None, wait=0, parent_id=None,
                          parent_host=None, parent_address=None, provider=None, account=None, model=None,
                          effort=None, basis=None, usage_cache=None, dry_run=False, verbose=False, fast=False,
                          score_timeout=30, resume=None, new_worker=False)
        with patch.dict(os.environ, {'PASEO_AGENT_ID': 'parent-a'}), \
                patch.object(roll, 'WORKER_PROMPTS_DIR', self.root / 'wp'), \
                patch.object(roll, 'RESULTS_DIR', self.root / 'rr'), \
                patch.object(flow, 'host_call', host):
            r1, c1 = roll.run_batch(argparse.Namespace(brief=[b1a, b1b], **batch_args), judge)
            self.assertEqual(c1, 0)
            self.assertEqual([(op, aid) for op, aid, _ in operations], [('create', 'worker-1a'), ('create', 'worker-1b')])

            operations.clear()
            # Re-rolling the exact brief_paths in reverse order resumes each matching worker without collision
            r2, c2 = roll.run_batch(argparse.Namespace(brief=[b1b, b1a], **batch_args), judge)
            self.assertEqual(c2, 0)
            self.assertEqual([(op, aid) for op, aid, _ in operations], [('resume', 'worker-1b'), ('resume', 'worker-1a')])
            self.assertTrue(all(item['worker'].get('resumed') for item in r2['results']))

    def test_review_independence_and_explicit_resume_and_blocked_status(self):
        import argparse
        import roll
        # m9: empty --local-only is rejected
        ns_empty = argparse.Namespace(brief=None, task='Do work', file=[], apply_to=None, local_only='   ',
                                      keep_whole=None, hours=1.0, title=None, workspace=None, task_id='t-1',
                                      fast=False, resume=None, new_worker=False)
        with self.assertRaisesRegex(ValueError, '--local-only'):
            roll.build_task(ns_empty, 'Do work', {'id': 'p-1'})

        # m5: scorer_unavailable maps to status='blocked' and reason='scorer_unavailable' in both full and compact output
        judge_blocked = type('Judge', (), {
            'plan_stage': staticmethod(lambda doc, _: (
                {'stage': 'plan', 'status': 'blocked', 'choice': 'none', 'error': 'scorer_unavailable',
                 'scorer': {'source': 'scorer_unavailable', 'fallback_reason': 'no_classifier_available'}}, 2)),
            'usage_snapshot': staticmethod(lambda _: self.usage),
        })
        ns = argparse.Namespace(brief=None, task='Do work', file=[], apply_to=None, local_only='local daemon',
                                keep_whole=None, hours=1.0, title=None, workspace=None, task_id='t-blocked',
                                wait=0, parent_id='parent-a', parent_host=None, parent_address=None,
                                provider=None, account=None, model=None, effort=None, basis=None,
                                usage_cache=None, dry_run=False, verbose=True, fast=False,
                                resume=None, new_worker=False, no_score=False)
        with patch.object(roll, 'resolve_parent', return_value=('parent-a', 'paseo', 'parent-a')):
            res_b, code_b = roll.run(ns, judge_blocked)
            self.assertEqual((code_b, res_b['status'], res_b['reason'], res_b['error']),
                             (2, 'blocked', 'scorer_unavailable', 'scorer_unavailable'))
            comp = roll.compact(res_b, res_b['status'], None)
            self.assertEqual((comp['status'], comp['reason'], comp['error']),
                             ('blocked', 'scorer_unavailable', 'scorer_unavailable'))

        # m7: prepare() does not copy implementer chain_id onto a fresh read-only review worker
        review_task = dict(self.task, task_id='t-review', chain={'id': 'chain-impl-1', 'worker': {'agent_id': 'agent-impl', 'bundle': 'lab_sonnet55_high', 'read_only_review': False}}, profile={
            'work_kind': 'debug_review', 'reasoning_depth': 2, 'code_complexity': 0, 'context_scale': 1,
            'divergence': 1, 'hallucination_risk': 2, 'error_cost': 2, 'needs_visual': False, 'needs_gui': False,
            'local_only': True, 'realtime_loop': False, 'wants_fast': False, 'read_only_review': True,
            'interaction_style': 'engineering', 'multi_task': False, 'task_count': '1', 'reason': 'review'})
        review_dec = registry.issue({'stage': 'plan', 'choice': 'lab_sonnet55_high', 'selected_bundle': self.bundle,
                                     'decision': {'action': 'start'}}, review_task, self.now)
        rev_row = registry.prepare(review_dec['decision_id'], review_task, self.launch, self.usage,
                                   accounts=self.accounts, now=self.now, resume_agent_id=None)
        self.assertIsNone(rev_row.get('chain_id'))
        registry.claim(rev_row['request'], now=self.now)
        registry.observe(rev_row['dispatch_id'], self.snapshot(rev_row, 'idle', attentionReason='finished'), now=self.now)

        # M1 & m6: named-model review rejects explicit --resume of an implementer worker
        impl_worker = {'agent_id': 'agent-impl', 'task_ids': ['t-impl'], 'brief_paths': [],
                       'chain_id': 'chain-impl-1', 'session_state': {'context_tokens': 1000, 'compacted_tokens': 0},
                       'profiles': [{'work_kind': 'general_implementation', 'code_complexity': 2, 'read_only_review': False}],
                       'read_only_review': False, 'bundle_id': 'lab_sonnet55_high',
                       'bundle': self.bundle}
        review_prof = dict(review_task['profile'])
        def fake_record_constraint(a, t):
            t['profile'] = review_prof
            base_dec = {'stage': 'record', 'choice': 'lab_sonnet55_high', 'constraint': 'user_override',
                        'basis': a.basis, 'task_profile': review_prof, 'selected_bundle': self.bundle}
            return {**base_dec, **registry.issue(base_dec, t, self.now)}

        judge_named = type('Judge', (), {
            'record_constraint': staticmethod(fake_record_constraint),
            'usage_snapshot': staticmethod(lambda _: self.usage),
        })
        ns_named_resume = argparse.Namespace(
            brief=None, task='Review the diff only', file=[], apply_to=None, local_only='local daemon',
            keep_whole=None, hours=0.5, title=None, workspace=None, task_id='t-impl',
            wait=0, parent_id='parent-a', parent_host=None, parent_address=None,
            provider='claude-lab', account='claude-lab', model='claude-sonnet-5-5', effort='high',
            basis='User asked for Sonnet review', usage_cache=None, dry_run=False, verbose=True,
            fast=False, resume='agent-impl', new_worker=False, no_score=False)
        with patch.object(roll, 'resolve_parent', return_value=('parent-a', 'paseo', 'parent-a')), \
                patch.object(registry, 'pending', return_value=[]), \
                patch.object(registry, 'reusable_workers', return_value=[impl_worker]), \
                patch.object(roll.dispatch_flow, 'paseo_reachable', return_value=True), \
                patch.object(roll.dispatch_flow, 'reconcile'), \
                patch.object(roll.dispatch_flow, 'launch', return_value={'state': 'running', 'agent_id': 'fresh-rev', 'observed_at': self.now.isoformat()}):
            with self.assertRaisesRegex(ValueError, 'previously wrote or modified code and cannot be resumed'):
                roll.run(ns_named_resume, judge_named)
            # Without explicit --resume, even with matching task_id='t-impl', named-model review does not reuse implementer
            ns_named_auto = argparse.Namespace(**{**vars(ns_named_resume), 'resume': None})
            res_auto, code_auto = roll.run(ns_named_auto, judge_named)
            self.assertEqual(code_auto, 0)
            self.assertFalse(res_auto['worker'].get('resumed'))


if __name__ == '__main__':
    unittest.main()
