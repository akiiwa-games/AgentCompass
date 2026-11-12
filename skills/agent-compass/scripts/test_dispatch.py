"""Dispatch lifecycle tests: isolated DB, fake host, no live creation or services."""
import copy
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pytest

import allocator
import account_fleet
import axis_scores
import provider_inventory
import quota_economy
import quota_pools
import worker_sessions
import dispatch_flow as flow
import dispatch_registry as registry

from router_test_support import isolated_router_state, profile, profile_for, temporary_root


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup, policy=False)


def test_host_bridge_does_not_open_a_console():
    """The panel's live-model refresh starts the bridge without a Windows console."""
    calls = []

    def launch(args, **kwargs):
        calls.append(kwargs)
        kwargs['stdout'].write(b'{"ok": true}')
        return SimpleNamespace(wait=lambda timeout: None)

    with patch.object(flow.subprocess, 'Popen', side_effect=launch):
        assert flow.host_call({'operation': 'models'})['ok']
    if os.name == 'nt':
        assert calls[0]['creationflags'] & subprocess.CREATE_NO_WINDOW
    else:
        assert calls[0]['creationflags'] == 0


class DispatchTests(unittest.TestCase):
    def test_fast_opt_out_after_record_is_rechecked_at_prepare(self):
        # Claude fast mode has cited pricing and speed; the fixture's Opus bundle is switched on.
        self.accounts['accounts']['claude-lab']['bundles']['lab_opus55_high']['disabled'] = False
        self.bundle = dict(account_fleet.bundles(self.accounts)['lab_opus55_high'], bundle_id='lab_opus55_high', fast=True)
        with patch.object(account_fleet, 'load_accounts', return_value=self.accounts):
            decision = self.issue(stage='record', choice='constraint', constraint='user_override',
                                  basis='Diagnostic fixture')
        self.accounts['models']['claude-opus-5-5']['fast_capable'] = False
        with self.assertRaisesRegex(ValueError, 'fast_capable'):
            self.prepare(decision)
        self.accounts['models']['claude-opus-5-5']['fast_capable'] = True
        row = self.prepare(decision)
        self.assertTrue(row['bundle']['fast'])
        self.assertEqual(row['request']['settings']['features'], {'fast_mode': True})
        self.accounts['models']['claude-opus-5-5']['fast_capable'] = False
        import route_judge
        with patch.object(account_fleet, 'load_accounts', return_value=self.accounts), \
                patch.object(route_judge, 'usage_snapshot', return_value=self.usage):
            with self.assertRaisesRegex(ValueError, 'fast_capable'):
                registry.claim(row['request'], parent_id='parent-a', call_id='fixture', now=self.now)
            self.assertEqual(registry.inspect(row['dispatch_id'])['state'], 'prepared')
            self.accounts['models']['claude-opus-5-5']['fast_capable'] = True
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
        self.accounts = account_fleet.load_accounts(account_fleet.reference_fleet_path())
        self.accounts['accounts']['claude-lab']['plan'] = 'max20'       # in-process planning sees a Max 20x account; the CLI subprocesses read the fixture credential's Pro plan
        (self.root / 'host.json').write_text(json.dumps({'allowed_accounts': ['claude-lab', 'codex', 'antigravity']}))
        env = patch.dict(os.environ, {
            'ROUTER_DISPATCH_DB': str(self.root / 'dispatch.sqlite3'),
            'ROUTER_OVERRIDES': str(self.root / 'overrides.json'),
            'ROUTER_CAPACITY': str(self.root / 'capacity.json'),
            'ROUTER_LEDGER': str(self.root / 'ledger.jsonl'),
            'ROUTER_LAUNCH_FAILURES': str(self.root / 'launch-failures.json'),
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
        self.task['profile'] = profile_for(self.task)    # the CLI subprocesses of these tests plan this task too
        self.usage = {'accounts': {'claude-lab': {'status': 'fresh', 'fetched_at': self.now.isoformat(), 'windows': [
            {'id': wid, 'used_pct': 100 - rem, 'remaining_pct_before_reset': rem,
             'resets_at': (self.now + timedelta(hours=hours)).isoformat(), 'seconds_until_reset': hours * 3600}
            for wid, rem, hours in [('five_hour', 100, 2.5), ('seven_day', 100, 130)]]}}}
        # The router prices a claude-lab launch only from a current quota observation, so the cache the CLI reads has one.
        # It is bound to the fixture's credential, as the collector writes it.
        key = registry.provider_inventory.discover(home=self.root)['accounts']['claude-lab']['credential_key']
        # The plan is the live OAuth profile's, cached with the reading (plan_source profile_api), never the login file's.
        reading = {'fetched_at': self.now.isoformat(), 'stale': False, 'available': True, 'credential_key': key,
                   'plan_label': 'pro', 'plan_source': 'profile_api',
                   'windows': self.usage['accounts']['claude-lab']['windows']}
        (self.root / 'usage.json').write_text(json.dumps({'providers': {'claude-lab': reading}}))
        self.bundle = dict(account_fleet.bundles(self.accounts)['lab_sonnet55_high'], bundle_id='lab_sonnet55_high')
        self.launch = {'title': 'Fixture task', 'initialPrompt': 'Only fixture work'}

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
                 'status': status, 'updatedAt': (max(self.now, registry.utcnow()) + timedelta(seconds=10)).isoformat(),
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
            self.assertEqual(quota_pools.inflight_reserved_usd('claude-lab', spec, 'five_hour', spec['windows']['five_hour'],
                {'remaining': 40}, 18, [{'stage': stage, 'expected_usd': 100, 'bundle': self.bundle}],
                self.accounts, self.accounts['scoring'], self.now), 0)

    def test_local_disable_after_prepare_blocks_launch_and_reenable_recovers(self):
        prepared = self.prepare()
        snapshot = {'accounts': {'claude-lab': {'family': 'claude', 'status': 'paused',
                    'reason': 'account_disabled_in_magpie', 'plan': 'pro'}}, 'launchers': {}}
        paused = provider_inventory.apply(copy.deepcopy(self.accounts), snapshot)
        with patch.object(account_fleet, 'load_accounts', return_value=paused):
            with self.assertRaisesRegex(ValueError, 'account_disabled_in_magpie'):
                registry.claim(prepared['request'], parent_id='parent-a', now=self.now)
        self.assertEqual(registry.inspect(prepared['dispatch_id'])['state'], 'prepared')
        snapshot['accounts']['claude-lab'].update(status='ready', reason=None)
        restored = provider_inventory.apply(copy.deepcopy(self.accounts), snapshot)
        with patch.object(account_fleet, 'load_accounts', return_value=restored):
            self.assertEqual(registry.claim(prepared['request'], parent_id='parent-a', now=self.now)['state'], 'launching')

    @pytest.mark.slow
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
        priced = axis_scores.five_axis_overlay(account_fleet.load_accounts())     # what the CLI plans with
        info = priced['models']['gpt-6-astra']
        at = lambda effort: quota_economy.bundle_task_usd(priced['accounts']['codex'], 'gpt-6-astra',
            self.task['expected_hours'] * quota_economy.duration_at(info, effort)[0], info)
        # Priced at the effort the user named: a Low run spends less than the same work at High, and the reservation says so.
        self.assertGreater(row['expected_usd'], 0)
        self.assertLess(row['expected_usd'], round(at('high'), 2))
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

    @pytest.mark.slow
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
        for changes in ({'account': 'claude-lab'}, {'provider': 'grok'}, {'model': 'unknown-model'}):
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

    @pytest.mark.slow
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
            self.assertEqual(result['state'], 'create_failed')
            self.assertEqual(result['error'], 'host_creation_failed')
            events = registry.inspect(row['dispatch_id'])['events']
            self.assertEqual(events[-1]['reason'], 'host_creation_failed:provider_refresh_timeout')
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

    @pytest.mark.slow
    def test_concurrent_prepare_checks_and_reserves_atomically(self):
        # On the Pro plan of the fixture credential one task takes about three quarters of the five-hour window.
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
        self.assertTrue(again['creating'])
        self.assertNotIn('observe_error', again)
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
        accounts = axis_scores.five_axis_overlay(self.accounts)
        account = accounts['accounts']['antigravity']
        account['capacity_usd'] = {'gemini-weekly': 120.0, 'gemini-5h': 18.0}      # fixture capacity: the units are what is tested
        for model in ('gemini-4-argon-high', 'gemini-3.8-flash-high'):
            info = accounts['models'][model]
            budget = quota_economy.bundle_task_usd(account, model, 1, info=info)
            for wid in ('gemini-weekly', 'gemini-5h'):
                pct = quota_economy.unit_rate(account, wid, model, info=info)
                self.assertAlmostEqual(pct * quota_economy.capacity_usd(account, wid) / 100, budget)

    @pytest.mark.slow
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


def wait_for_exit(pid, seconds):
    """True once the process `pid` has exited (or never existed), waiting at most `seconds`."""
    if os.name == 'nt':
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x00100000, False, int(pid))      # SYNCHRONIZE
        if not handle:
            return True
        try:
            return kernel32.WaitForSingleObject(handle, int(seconds * 1000)) == 0
        finally:
            kernel32.CloseHandle(handle)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if os.waitpid(int(pid), os.WNOHANG)[0]:
                return True
        except ChildProcessError:
            return True
        time.sleep(0.02)
    return False


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
            workspace=None, task_id='test-task', parent_id=None, parent_host=None, parent_address=None, provider=None, model=None, effort=None,
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
        with patch.object(flow, 'host_call', host), patch.object(account_fleet, 'load_accounts', return_value=self.accounts):
            (result, code), seen = self.roll(decision, brief=None, task='Rename foo to bar in util.py', hours=None, task_id=None)
        self.assertEqual(code, 0)
        self.assertEqual((result['status'], result['worker']['model']), ('launched', self.bundle['model']))
        # A launched worker has an agent id, so the compact result also carries its continuation verdict (roll.run).
        self.assertEqual(set(result) - {'worker', 'reason'},
                         {'status', 'next_action', 'next_argv', 'assumptions', 'continuation', 'details'})
        self.assertIn('No completion notification is confirmed', result['next_action'])
        self.assertIn('budgeted 0.4h from the classifier estimate, confidence medium', result['assumptions'][0])
        self.assertEqual(seen['task']['objective'], 'Rename foo to bar in util.py')
        self.assertNotIn('expected_hours', seen['task'])
        self.assertEqual(seen['task']['hours_source'], 'classifier_estimate')
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
                                  hours=None, title=None, workspace=None, task_id=None, parent_id=None,
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
            with self.subTest(status), patch.object(provider_inventory, 'current', return_value=inventory), \
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
        with patch.object(provider_inventory, 'current', return_value=inventory), \
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
        rec_file = self.root / 'grok-record.json'
        rec_file.unlink(missing_ok=True)
        with patch.dict(os.environ, {'FAKE_GROK_EXIT': '0'}):
            result, code = self.grok_roll(command)
        deadline = time.monotonic() + 5.0
        while not (rec_file.is_file() and rec_file.stat().st_size > 0) and time.monotonic() < deadline:
            time.sleep(0.02)
        record = json.loads(rec_file.read_text(encoding='utf-8'))
        prompt = self.assert_worker_prompt(result, parent='parent-a')
        run_dir = Path(result['worker']['output']).parent
        self.assertEqual((result['launched'], result['dispatch_mode'], code), (True, 'cli', 0))
        self.assertEqual(result['caller_action'], 'already_launched_do_not_respawn')
        self.assertEqual(seen[-1], ('grok', 'grok'))
        self.assertEqual(record['argv'], ['--always-approve', '-m', 'grok-4.7', '--reasoning-effort', 'high',
                                          '--cwd', str(Path.cwd()), '--prompt-file', str(run_dir / 'brief.md')])
        self.assertEqual((record['cwd'], record['stdin'], record['prompt']), (str(Path.cwd()), '', prompt))
        self.assertEqual(record['env']['GROK_FIXTURE_ACCOUNT'], 'bound')
        self.assertEqual((record['env']['AGENTCOMPASS_WORKER'], record['env']['ROUTER_PARENT_AGENT_ID']), ('1', 'parent-a'))
        self.assertFalse({'CLAUDECODE', 'CODEX_HOME'} & set(record['env']))
        self.assertIsNone(result['worker']['exit'])
        while not Path(result['worker']['output']).read_text(encoding='utf-8').strip() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(Path(result['worker']['output']).read_text(encoding='utf-8').strip(), 'grok reply')
        # The worker still holds its output file until it exits; tearing the temp root down first raced it (WinError 32).
        self.assertTrue(wait_for_exit(result['worker']['pid'], 10.0), result['worker']['pid'])

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
                result, code = self.grok_roll(command)
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
        bundles = account_fleet.bundles(self.accounts)
        brief = ['--print']
        cases = (('claude-lab via its Paseo definition', 'lab_sonnet55_high',
                  ['claude', '-p', '--model', 'claude-sonnet-5-5', '--effort', 'high', '--permission-mode', 'bypassPermissions', '--settings', '{"fastMode": false}'],
                  'C:/fixture/.claude-lab', self.brief),
                 ('claude does not inherit the caller account', 'sonnet55_high',
                  ['claude', '-p', '--model', 'claude-sonnet-5-5', '--effort', 'high', '--permission-mode', 'bypassPermissions', '--settings', '{"fastMode": false}'],
                  None, self.brief),
                 ('agy takes the brief as its last argument', 'flash_high',
                  ['agy', '--dangerously-skip-permissions', '--model', bundles['flash_high']['model'], '--add-dir',
                   str(Path.cwd())] + brief, None, None))
        for label, choice, argv, config_dir, stdin in cases:
            calls = []

            class Process:
                pid = 4242

                def __init__(self, command, stdin=None, stdout=None, env=None, **_):
                    calls.append({'argv': command, 'env': env,
                                  'stdin': stdin.read().decode() if hasattr(stdin, 'read') else None})
                    stdout.write(b'worker reply')
            bundle = dict(bundles[choice], bundle_id=choice)
            decision = dict(stage='plan', choice=choice, selected_bundle=bundle, decision={'action': 'start'})
            with self.subTest(label), patch.object(roll, 'CLI_RUNS_DIR', self.root / 'runs'), \
                    patch.object(roll.subprocess, 'Popen', Process), \
                    patch.object(flow, 'host_call', side_effect=AssertionError('no Paseo')):
                (result, code), _ = self.roll(decision, parent='', parent_id='parent-a', parent_host='paseo', env=host)
                self.assertEqual((result['launched'], code), (True, 0))
                prompt = self.assert_worker_prompt(result, parent='parent-a')
                self.assertEqual(calls[0]['env']['AGENTCOMPASS_WORKER'], '1')
                self.assertEqual(calls[0]['env']['ROUTER_PARENT_AGENT_ID'], 'parent-a')
                self.assertEqual(calls[0]['argv'], argv + [prompt] if stdin is None else argv)
                self.assertEqual(calls[0]['env'].get('CLAUDE_CONFIG_DIR'), config_dir)
                self.assertFalse({'CLAUDECODE', 'CODEX_HOME'} & set(calls[0]['env']))
                self.assertEqual(calls[0]['stdin'], prompt if stdin is not None else None)
                self.assertIsNone(result['worker'].get('exit'))
                self.assertIn('4242', result.get('next', ''))

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
            'schema_version': 2,
            'accounts': {
                'team-proxy': {
                    'billing': 'metered',
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
                    'AGENTCOMPASS_ACCOUNTS_OVERRIDE': str(override),
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
                    'AGENTCOMPASS_ACCOUNTS_OVERRIDE': str(override),
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
        decision = dict(stage='plan', choice='gpt_pro', selected_bundle={
            'provider': 'chatgpt-web', 'account': 'codex', 'model': 'gpt-6-pro'}, decision={'action': 'start'})
        (result, code), _ = self.roll(decision)
        self.assertEqual((code, result['launched']), (0, False))
        command = result['next_argv']
        self.assertEqual(command[command.index('--prompt') + 1], result['worker_prompt_file'])
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
                self.assertGreater(row['expected_usd'], 0.0)      # priced at the model's cited list price, not free

    def test_fast_mode_bundle_emits_paseo_fast_feature_and_doubles_expected_usd(self):
        # Claude fast mode is cited ($16/$80 against $8/$40, 2x): Opus 5.5 on a Max 20x plan, the fixture's bundle switched on.
        self.accounts['accounts']['claude-lab']['bundles']['lab_opus55_high']['disabled'] = False
        task = {**self.task, 'task_id': 'task-normal-1', 'work_kind': 'general_implementation'}
        task['profile'] = profile_for(task)
        normal_task = {**task, 'pinned_bundle': 'lab_opus55_high', 'disable_fast': True}
        normal_plan = allocator.plan(normal_task, self.usage, self.now, accounts=self.accounts)
        self.assertEqual(normal_plan['choice'], 'lab_opus55_high')
        normal_dec = registry.issue(dict(normal_plan, stage='plan'), task, self.now)
        normal_row = registry.prepare(normal_dec['decision_id'], task, self.launch, self.usage, self.accounts, self.now)
        self.assertEqual(normal_row['request']['settings']['features'], {'fast_mode': False})

        fast_task = {**task, 'task_id': 'task-fast-1', 'pinned_bundle': 'lab_opus55_high', 'force_fast': True}
        fast_plan = allocator.plan(fast_task, self.usage, self.now, accounts=self.accounts)
        self.assertTrue(fast_plan['selected_bundle'].get('fast'))
        fast_dec = registry.issue(dict(fast_plan, stage='plan'), fast_task, self.now)
        fast_row = registry.prepare(fast_dec['decision_id'], fast_task, self.launch, self.usage, self.accounts, self.now)
        self.assertEqual(fast_row['request']['settings'],
                         {'modeId': 'bypassPermissions', 'thinkingOptionId': 'high', 'features': {'fast_mode': True}})
        self.assertAlmostEqual(fast_row['expected_usd'], normal_row['expected_usd'] * 2.0, delta=0.05)

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
            with patch.object(worker_sessions, 'session_state', return_value=worn_state):
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
                patch.object(account_fleet, 'load_accounts', return_value=self.accounts), \
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
            'work_kind': 'debug_review', 'reasoning_depth': 2, 'code_complexity': 0, 
            'needs_visual': False, 'needs_gui': False,
            'local_only': True, 'wants_fast': False, 'read_only_review': True,
            'interaction_style': 'engineering', 'multi_task': False, 'task_count': '1',
            'axis_demand': {'divergence': 1, 'coding': 2, 'aesthetics': 0, 'tool_operation': 0},
            'reason': 'review'})
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
        def fake_record_constraint(a, t, classified=None):
            t['profile'] = review_prof
            base_dec = {'stage': 'record', 'choice': 'lab_sonnet55_high', 'constraint': 'user_override',
                        'basis': a.basis, 'task_profile': review_prof, 'selected_bundle': self.bundle}
            return {**base_dec, **registry.issue(base_dec, t, self.now)}

        judge_named = type('Judge', (), {
            'classify_pinned': staticmethod(lambda t, a: {'task': dict(t, profile=review_prof), 'scorer': None,
                                                         'hours_estimate': None, 'alerts': []}),
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


class PinnedRollTests(unittest.TestCase):
    """`roll --provider/--account/--model --basis` pins the model only: the task is classified like an automatic roll's,
    and the pinned bundle is priced and reserved from that profile."""
    setUp, issue = DispatchTests.setUp, DispatchTests.issue

    def args(self, **changes):
        import argparse
        return argparse.Namespace(**{
            'basis': 'User asked for Sonnet', 'provider': 'claude-lab', 'account': 'claude-lab',
            'model': 'claude-sonnet-5-5', 'effort': 'high', 'input': None, 'usage_cache': None, 'accounts': None,
            'constraint': 'user_override', 'specialty': None, 'reason_category': None, 'fast': False, 'parent_id': None,
            'dry_run': False, 'no_score': False, 'score': False, 'score_timeout': 10, 'keep_whole': None, 'hours': None,
            **changes})

    def unhoured(self):
        # What roll builds without --hours: no expected_hours, no profile, the classifier asked for its estimate.
        task = {k: v for k, v in self.task.items() if k not in ('profile', 'expected_hours', 'work_kind', 'difficulty')}
        return dict(task, hours_source='classifier_estimate')

    def judged(self, scored):
        import route_judge
        return route_judge, (patch.object(account_fleet, 'load_accounts', return_value=self.accounts),
                             patch.object(route_judge, 'usage_snapshot', return_value=copy.deepcopy(self.usage)),
                             patch.object(route_judge, 'score_task', return_value=scored))

    def test_a_pinned_roll_gets_the_classifier_hours_and_a_nonzero_reservation(self):
        import roll
        classified_profile = dict(profile_for(self.task), expected_hours=0.7, hours_confidence='medium')
        judge, patches = self.judged((classified_profile, {'source': 'system_one', 'model': 'fixture-scorer'}))
        task, args = self.unhoured(), self.args()
        with patches[0], patches[1], patches[2] as scorer:
            classified = judge.classify_pinned(task, args)
            decision = judge.record_constraint(args, task, classified)
        scorer.assert_called_once()                       # the classifier ran although the model was named
        self.assertEqual(decision['hours_estimate'], {'hours': 0.7, 'source': 'classifier_estimate', 'confidence': 'medium'})
        self.assertEqual(decision['task_profile']['expected_hours'], 0.7)
        self.assertFalse(decision['classifier_skipped'])
        self.assertEqual(decision['selected_bundle']['bundle_id'], 'lab_sonnet55_high')
        self.assertGreater(decision['expected_usd'], 0)    # priced like any other launch
        self.assertEqual(roll.hours_assumption(args, decision),
                         '--hours not given: budgeted 0.7h from the classifier estimate, confidence medium; an estimate, '
                         'not a measurement. Pass --hours for a large or unusually small task.')
        self.assertEqual(registry.reservations(self.now), [])
        row = registry.prepare(decision['decision_id'], task, self.launch, self.usage, accounts=self.accounts, now=self.now)
        self.assertGreater(row['expected_usd'], 0)
        held = registry.reservations(self.now)
        self.assertEqual([r['dispatch_id'] for r in held], [row['dispatch_id']])
        spec = self.accounts['accounts']['claude-lab']
        self.assertGreater(quota_pools.inflight_reserved_usd('claude-lab', spec, 'five_hour', spec['windows']['five_hour'],
                           {'remaining': 100}, 1000, held, self.accounts, self.accounts['scoring'], self.now), 0)

    def test_no_classifier_and_no_hours_says_so_and_never_prints_none(self):
        import roll
        judge, patches = self.judged((None, {'source': 'scorer_unavailable', 'fallback_reason': 'all_classifiers_failed'}))
        task = self.unhoured()
        ns = self.args(brief=None, task=task['objective'], file=[], apply_to=None, local_only='local daemon',
                       title=None, workspace=None, task_id=task['task_id'], wait=0, parent_id='parent-a',
                       parent_host=None, parent_address=None, verbose=True, resume=None, new_worker=True)
        with patches[0], patches[1], patches[2], \
                patch.object(roll, 'resolve_parent', return_value=('parent-a', 'paseo', 'parent-a')), \
                patch.object(registry, 'pending', return_value=[]), \
                patch.object(registry, 'reusable_workers', return_value=[]):
            result, code = roll.run(ns, judge)
        self.assertEqual((code, result['status'], result['launched']), (2, 'blocked', False))
        self.assertIn('--hours was not given, so there is no hours estimate', result['cause'])
        self.assertIn('cannot be priced or reserved, so nothing was launched', result['cause'])
        self.assertEqual(result['assumptions'], [
            '--hours not given and no classifier estimate: no hours were budgeted, and none were invented. '
            'Pass --hours, or roll again when the classifier answers.'])
        said = ' '.join([result['cause'], result['next'], *result['assumptions']])
        self.assertNotIn('None', said)
        self.assertNotIn('Noneh', json.dumps(result, ensure_ascii=False))
        self.assertEqual(registry.reservations(self.now), [])
        # An estimate missing its figure is "no estimate", never "budgeted Noneh".
        self.assertNotIn('None', roll.hours_assumption(ns, {'hours_estimate': {'source': 'caller_profile', 'hours': None}}))


class CreateLifecycleTests(unittest.TestCase):
    """A create nobody has seen finish: the daemon's durable record decides, and only an ended one is terminal."""
    setUp, snapshot, issue, prepare = DispatchTests.setUp, DispatchTests.snapshot, DispatchTests.issue, DispatchTests.prepare

    def claimed(self, provider=None):
        row = self.prepare()
        if provider:
            with registry.transaction() as connection:
                stored = registry.get(connection, 'dispatches', row['dispatch_id'])
                stored['bundle']['provider'] = provider
                registry.save(connection, stored)
        return self.claim_row(row)

    def claim_row(self, row):
        import route_judge
        with patch.object(account_fleet, 'load_accounts', return_value=self.accounts), \
                patch.object(route_judge, 'usage_snapshot', return_value=self.usage):
            return registry.claim(registry.inspect(row['dispatch_id'])['request'], parent_id='parent-a', now=self.now)

    def observe_only(self, creation):
        def host(request):
            self.assertEqual(request['operation'], 'observe')
            return {'ok': True, 'agent': None, 'missing': False, 'creation': creation}
        return host

    def test_daemon_failure_after_the_bridge_gave_up_is_terminal_releasable_and_rerollable(self):
        # dispatch 32e17e1b: the daemon recorded a definitive failure 131 s in, long after the bridge's 25 s cut-off.
        import launch_failures
        store = self.root / 'launch-failures.json'
        with patch.dict(os.environ, {'ROUTER_LAUNCH_FAILURES': str(store)}):
            row = self.claimed()
            host = self.observe_only({'phase': 'failed', 'outcome_unknown': False, 'failure_class': 'model_unavailable'})
            result = flow.launch(row['dispatch_id'], 'parent-a', host, watch_seconds=0)
            self.assertEqual(result['state'], 'create_failed')
            self.assertEqual(result['error'], 'create_failed')
            self.assertEqual(result['next_action'], flow.CREATE_FAILED_NEXT)   # AgentCompass relaunches it
            self.assertNotIn('continuation_argv', result)
            self.assertEqual(registry.reservations(), [])
            self.assertEqual(registry.release(row['dispatch_id'], 'parent-a', 'give up')['state'], 'create_failed')
            self.assertTrue(json.loads(store.read_text(encoding='utf-8')))
            launch_failures.clear('claude-lab')
            self.assertEqual(self.prepare(self.issue())['state'], 'prepared')

    def test_a_request_never_admitted_fails_only_after_its_provider_deadline(self):
        for provider, seconds in (('claude-lab', registry.CREATE_DEADLINE),
                                  ('antigravity-resilient', registry.CREATE_DEADLINES['antigravity-resilient'])):
            with self.subTest(provider=provider):
                row = self.claimed(provider)
                launched = registry.parse(row['launched_at'])
                self.assertEqual(registry.create_deadline(row), launched + timedelta(seconds=seconds))
                self.assertGreater(seconds, registry.CREATE_SEND_WINDOW + flow.HOST_TIMEOUT)
                before = flow.reconcile(row['dispatch_id'], self.observe_only(None), now=launched + timedelta(seconds=seconds - 1))
                self.assertEqual(before[0]['state'], 'launching')
                self.assertTrue(before[0]['creating'])
                after = flow.reconcile(row['dispatch_id'], self.observe_only(None), now=launched + timedelta(seconds=seconds))
                self.assertEqual(after[0]['state'], 'create_failed')
                self.assertEqual(registry.inspect(row['dispatch_id'])['events'][-1]['reason'], 'host_never_admitted_create')
                registry.reservations()
                with registry.transaction() as connection:
                    connection.execute('DELETE FROM dispatches')

    def test_an_admitted_create_stays_open_past_its_deadline_because_the_host_cannot_cancel_it(self):
        row = self.claimed()
        late = registry.parse(row['launched_at']) + timedelta(seconds=registry.CREATE_DEADLINE + 5)
        observed = flow.reconcile(row['dispatch_id'], self.observe_only({'phase': 'accepted'}), now=late)
        self.assertEqual(observed[0]['state'], 'launching')
        self.assertIn('no cancel', observed[0]['next_action'])
        with self.assertRaisesRegex(ValueError, 'Observe terminal status'):
            registry.release(row['dispatch_id'], 'parent-a', 'give up')
        # The lease lapses, but the expired row still refuses a release that would free a worker about to appear.
        registry.reservations(now=late + timedelta(seconds=registry.RUN_LEASE))
        self.assertEqual(registry.inspect(row['dispatch_id'])['state'], 'expired')
        with self.assertRaisesRegex(ValueError, 'still creating'):
            registry.release(row['dispatch_id'], 'parent-a', 'give up')
        # When it does arrive, the same dispatch binds it.
        agent = self.snapshot(row, updatedAt=(late + timedelta(seconds=1)).isoformat())
        recovered = flow.reconcile(row['dispatch_id'], lambda _: {'ok': True, 'agent': agent}, now=late + timedelta(seconds=2))
        self.assertEqual(recovered[0]['state'], 'started')

    # What the bridge emits when the daemon answers {snapshot: null, error: 'Previously created agent no longer exists'}.
    REMOVED = {'phase': 'completed', 'outcome_unknown': False, 'agent_id': None, 'agent_created': True}

    def test_a_worker_removed_before_it_was_observed_ends_the_dispatch_and_frees_the_workspace(self):
        ws_dir = self.root / 'W'
        ws_dir.mkdir()
        task = dict(self.task, task_id='removed', expected_hours=0.001)
        launch = {'title': 'A task', 'initialPrompt': 'Only fixture work', 'cwd': str(ws_dir)}
        row = registry.prepare(self.issue(task)['decision_id'], task, launch, self.usage, accounts=self.accounts,
                               now=self.now, workspace=str(ws_dir))
        claimed = self.claim_row(row)
        self.assertTrue(registry.writers_at(str(ws_dir), now=self.now))
        # An expired lease that once saw the create in flight: the state the review found stuck.
        with registry.transaction() as connection:
            stored = registry.get(connection, 'dispatches', claimed['dispatch_id'])
            stored.update(state='expired', creation={'phase': 'accepted', 'in_flight': True})
            registry.save(connection, stored)
        settled = flow.reconcile(claimed['dispatch_id'], self.observe_only(self.REMOVED), now=self.now)
        self.assertEqual(settled[0]['state'], 'canceled')
        self.assertIn('removed', settled[0]['next_action'])
        self.assertEqual(registry.writers_at(str(ws_dir), now=self.now), [])
        self.assertEqual(registry.release(claimed['dispatch_id'], 'parent-a', 'give up')['state'], 'canceled')

    def test_a_worker_that_failed_at_its_prompt_and_was_removed_is_not_reported_as_never_created(self):
        row = self.claimed()
        store = self.root / 'prompt-failed.json'
        creation = {'phase': 'failed', 'outcome_unknown': True, 'agent_id': row['dispatch_id'], 'agent_created': True, 'failure_class': 'unknown'}
        with patch.dict(os.environ, {'ROUTER_LAUNCH_FAILURES': str(store)}):
            settled = flow.reconcile(row['dispatch_id'], self.observe_only(creation), now=self.now)
        self.assertEqual(settled[0]['state'], 'canceled')
        self.assertIn('part of the work', settled[0]['next_action'])
        self.assertNotIn('safe to roll', settled[0]['next_action'])
        self.assertFalse(store.exists())

    def test_a_failure_before_any_worker_existed_stays_create_failed(self):
        row = self.claimed()
        creation = {'phase': 'failed', 'outcome_unknown': True, 'agent_id': row['dispatch_id'], 'agent_created': False, 'failure_class': 'unknown'}
        settled = flow.reconcile(row['dispatch_id'], self.observe_only(creation), now=self.now)
        self.assertEqual(settled[0]['state'], 'create_failed')
        self.assertEqual(settled[0]['next_action'], flow.REQUEST_CREATE_FAILED_NEXT)
        cand_row = self.claim_row(self.prepare(self.issue(dict(self.task, task_id='cand-fail', expected_hours=0.001)),
                                               dict(self.task, task_id='cand-fail', expected_hours=0.001)))
        cand_settled = flow.reconcile(cand_row['dispatch_id'],
                                      self.observe_only({'phase': 'failed', 'outcome_unknown': False, 'agent_created': False,
                                                         'failure_class': 'model_unavailable'}), now=self.now)
        self.assertEqual(cand_settled[0]['next_action'], flow.CREATE_FAILED_NEXT)
        with patch.dict(os.environ, {'AGENTCOMPASS_ROLL_FALLBACK': 'off'}):
            self.assertIn('safe to roll', flow.public(registry.inspect(cand_row['dispatch_id']))['next_action'])

    def test_roll_reports_a_removed_worker_as_over_not_as_an_unconfirmed_delivery(self):
        import roll
        row = self.prepare()

        def host(request):
            if request['operation'] == 'create':
                return {'ok': False, 'sent': True, 'error': 'host_timeout'}
            return {'ok': True, 'agent': None, 'creation': self.REMOVED}
        args = SimpleNamespace(title='fixture', workspace=None, wait=0)
        decision = {'decision_id': row['decision_id'], 'selected_bundle': self.bundle}
        with patch.object(flow, 'host_call', host), patch.object(registry, 'prepare', return_value=row):
            result, code = roll.paseo(args, {}, decision, self.task, 'fixture', 'parent-a', self.usage)
        self.assertEqual((roll.status_of(result, code), code), ('blocked', 2))
        self.assertIn('removed', result['next'])
        self.assertNotIn('continuation_argv', result['worker'])

    def test_a_resume_is_never_settled_from_the_create_record(self):
        row = self.claimed()
        with registry.transaction() as connection:
            stored = registry.get(connection, 'dispatches', row['dispatch_id'])
            stored['operation'] = 'resume'
            registry.save(connection, stored)
        much_later = registry.parse(row['launched_at']) + timedelta(seconds=registry.CREATE_DEADLINE + 5)
        self.assertEqual(registry.resolve_creation(row['dispatch_id'], None, now=much_later), (registry.inspect(row['dispatch_id']), False))

    def cooldowns(self):
        store = self.root / 'cooldowns.json'
        return store, patch.dict(os.environ, {'ROUTER_LAUNCH_FAILURES': str(store)})

    def test_an_unknown_or_request_invalid_create_refusal_ends_the_dispatch_but_cools_nothing_down(self):
        store, scope = self.cooldowns()
        with scope:
            for idx, creation in enumerate((
                {'phase': 'failed', 'outcome_unknown': True, 'failure_class': 'unknown'},
                {'phase': 'failed', 'outcome_unknown': False, 'failure_class': 'unknown'},
                {'phase': 'failed', 'outcome_unknown': False, 'failure_class': 'request_invalid'},
            )):
                task = dict(self.task, task_id=f'nocool-{idx}', expected_hours=0.001)
                row = self.claim_row(self.prepare(self.issue(task), task))
                settled = flow.settle_create(row, creation, self.now, None)
                self.assertEqual(settled['state'], 'create_failed')
        self.assertFalse(store.exists())

    def test_two_processes_settling_one_row_count_one_failure(self):
        store, scope = self.cooldowns()
        with scope:
            stale = self.claimed()  # Both read the row while it was still open.
            for _ in range(2):
                flow.settle_create(stale, {'phase': 'failed', 'outcome_unknown': False, 'failure_class': 'model_unavailable'}, self.now, None)
        entries = json.loads(store.read_text(encoding='utf-8'))['entries']
        self.assertEqual([entry['streak'] for entry in entries.values()], [1])

    def test_a_cooldown_that_cannot_be_written_stops_neither_the_settlement_nor_the_other_rows(self):
        rows = []
        for task_id in ('one', 'two'):
            task = dict(self.task, task_id=task_id, expected_hours=0.001)
            rows.append(self.claim_row(self.prepare(self.issue(task), task)))
        import launch_failures
        with patch.object(launch_failures, 'record', side_effect=PermissionError(5, 'denied')):
            settled = flow.reconcile(host=self.observe_only({'phase': 'failed', 'outcome_unknown': False, 'failure_class': 'model_unavailable'}), now=self.now)
        self.assertEqual({row['dispatch_id'] for row in settled}, {row['dispatch_id'] for row in rows})
        self.assertEqual({row['state'] for row in settled}, {'create_failed'})

    def test_a_cooldown_store_that_cannot_be_cleared_does_not_lose_the_observation(self):
        row = self.claimed()
        import launch_failures
        with patch.object(launch_failures, 'clear', side_effect=PermissionError(5, 'denied')):
            observed = registry.observe(row['dispatch_id'], self.snapshot(row), now=self.now + timedelta(seconds=11))
        self.assertEqual(observed['state'], 'started')

    def test_a_quota_store_that_cannot_be_written_does_not_lose_the_observation(self):
        row = self.claimed()
        import quota_exhaustion
        with patch.object(quota_exhaustion, 'record', side_effect=PermissionError(5, 'denied')):
            observed = registry.observe(row['dispatch_id'], self.snapshot(row, status='error', lastError='quota exceeded'),
                                        now=self.now + timedelta(seconds=11))
        self.assertEqual(observed['state'], 'failed')

    def test_a_quota_store_read_denied_for_a_moment_is_retried(self):
        import override_store
        import quota_exhaustion
        store = self.root / 'quota.json'
        store.write_text('{"accounts": {"seen": {}}}', encoding='utf-8')
        real, calls = Path.read_text, []

        def read_text(path, *args, **options):
            calls.append(path)
            if len(calls) < 3:
                raise PermissionError(5, 'denied')
            return real(path, *args, **options)
        with patch.object(Path, 'read_text', read_text), patch.object(override_store.time, 'sleep'):
            self.assertEqual(quota_exhaustion._read(store), {'accounts': {'seen': {}}})
        self.assertEqual(len(calls), 3)

    def test_a_store_replace_denied_for_a_moment_is_retried(self):
        import override_store
        store = self.root / 'store.json'
        real, calls = os.replace, []

        def replace(source, target):
            calls.append(source)
            if len(calls) < 3:
                raise PermissionError(5, 'denied')
            real(source, target)
        with patch.object(override_store.os, 'replace', replace), patch.object(override_store.time, 'sleep'):
            override_store.write(store, {'entries': {}})
        self.assertEqual(json.loads(store.read_text(encoding='utf-8')), {'entries': {}})
        self.assertEqual(len(calls), 3)

    def test_a_cooldown_reader_denied_sees_none_and_a_writer_denied_keeps_the_store(self):
        import launch_failures
        import override_store
        store = self.root / 'denied.json'
        launch_failures.record(self.bundle, 'model_unavailable', path=store)
        before = store.read_text(encoding='utf-8')
        with patch.object(Path, 'read_text', side_effect=PermissionError(5, 'denied')), patch.object(override_store.time, 'sleep'):
            self.assertEqual(launch_failures.active(path=store), {})
            with self.assertRaises(PermissionError):
                launch_failures.record(self.bundle, 'model_unavailable', path=store)
        self.assertEqual(store.read_text(encoding='utf-8'), before)

    def sent_create(self, pause, **prepare):
        """The create a launch sends when the process stalls for `pause` seconds right after the claim."""
        import account_fleet
        import route_judge
        row = self.prepare() if not prepare else registry.prepare(self.issue()['decision_id'], self.task, self.launch, self.usage,
                                                                  accounts=self.accounts, now=self.now, **prepare)
        sent, offset = {}, [0]
        real_now, real_claim = registry.utcnow, registry.claim

        def claim(*args, **kwargs):
            claimed = real_claim(*args, **kwargs)
            offset[0] = pause
            return claimed

        def host(request):
            if request['operation'] == 'resume':
                return {'ok': False, 'sent': False, 'error': 'worker_not_found'}
            if request['operation'] == 'create':
                sent.update(request)
                return {'ok': False, 'sent': True, 'error': 'host_timeout'}
            return {'ok': True, 'agent': None, 'creation': None}
        with patch.object(registry, 'claim', claim), patch.object(registry, 'utcnow', lambda: real_now() + timedelta(seconds=offset[0])), \
                patch.object(account_fleet, 'load_accounts', return_value=self.accounts), \
                patch.object(route_judge, 'usage_snapshot', return_value=self.usage):
            flow.launch(row['dispatch_id'], 'parent-a', host, watch_seconds=0)
        launched = registry.parse(registry.inspect(row['dispatch_id'])['launched_at'])
        return sent, launched, real_now

    def test_a_stall_after_the_claim_cannot_move_the_send_window(self):
        sent, launched, _ = self.sent_create(300)
        self.assertEqual(registry.parse(sent['send_by']), launched + timedelta(seconds=registry.CREATE_SEND_WINDOW))
        self.assertLessEqual(registry.parse(sent['send_by']), registry.parse(sent['create_deadline']))

    def test_a_resume_falling_back_to_create_gets_a_fresh_window_capped_at_the_deadline(self):
        sent, launched, real_now = self.sent_create(30, resume_agent_id='agent-old', fallback_prompt='Only fixture work')
        self.assertLess(abs((registry.parse(sent['send_by']) - (real_now() + timedelta(seconds=30 + registry.CREATE_SEND_WINDOW))).total_seconds()), 10)
        self.assertLess(registry.parse(sent['send_by']), registry.parse(sent['create_deadline']))
        with registry.transaction() as connection:
            connection.execute('DELETE FROM dispatches')
        sent, launched, _ = self.sent_create(300, resume_agent_id='agent-old', fallback_prompt='Only fixture work')
        self.assertEqual(registry.parse(sent['send_by']), registry.parse(sent['create_deadline']))

    def test_the_create_carries_its_send_window_and_deadline(self):
        row = self.prepare()
        sent = {}

        def host(request):
            if request['operation'] == 'create':
                sent.update(request)
                return {'ok': False, 'sent': True, 'error': 'host_timeout'}
            return {'ok': True, 'agent': None, 'creation': None}
        flow.launch(row['dispatch_id'], 'parent-a', host, watch_seconds=0)
        launched = registry.parse(registry.inspect(row['dispatch_id'])['launched_at'])
        self.assertEqual(registry.parse(sent['send_by']), launched + timedelta(seconds=registry.CREATE_SEND_WINDOW))
        self.assertEqual(registry.parse(sent['create_deadline']), launched + timedelta(seconds=registry.CREATE_DEADLINE))

    def test_inside_the_window_nothing_reads_as_a_failure(self):
        import roll
        from types import SimpleNamespace
        row = self.prepare()

        def host(request):
            if request['operation'] == 'create':
                return {'ok': False, 'sent': True, 'error': 'host_timeout'}
            return {'ok': True, 'agent': None, 'missing': False, 'creation': {'phase': 'accepted'},
                    'completion_notification': {'status': 'watching'}}
        args = SimpleNamespace(title='fixture', workspace=None, wait=0)
        decision = {'decision_id': row['decision_id'], 'selected_bundle': self.bundle}
        with patch.object(flow, 'host_call', host), patch.object(registry, 'prepare', return_value=row):
            result, code = roll.paseo(args, {}, decision, self.task, 'fixture', 'parent-a', self.usage)
        status = roll.status_of(result, code)
        self.assertEqual((status, code), ('pending', 0))
        worker = result['worker']
        self.assertEqual((worker['state'], worker['creating'], worker['creation_phase']), ('launching', True, 'accepted'))
        self.assertNotIn('observe_error', worker)
        self.assertNotIn('error', worker)
        compact = roll.compact(result, status, 'detail.json')
        self.assertIn('not a failure', compact['next_action'])
        self.assertIn('Do not roll again', compact['next_action'])
        self.assertNotIn('cause', compact)
        self.assertTrue(compact['next_argv'])

    def test_a_host_that_cannot_be_read_says_so_without_settling(self):
        row = self.claimed()
        late = registry.parse(row['launched_at']) + timedelta(hours=1)
        observed = flow.reconcile(row['dispatch_id'], lambda _: {'ok': False, 'error': 'host_timeout'}, now=late)
        self.assertEqual(observed[0]['error'], 'host_timeout')
        self.assertIsNone(registry.inspect(row['dispatch_id']).get('creation'))

    def test_release_settles_a_finished_create_first(self):
        row = self.claimed()
        args = SimpleNamespace(stage='dispatch', dispatch_action='release', dispatch_id=row['dispatch_id'],
                               parent_id='parent-a', basis='cancel')
        with patch.object(flow, 'host_call', self.observe_only({'phase': 'failed', 'failure_class': 'unknown'})):
            self.assertEqual(flow.cli(args)['state'], 'create_failed')


class ReleaseTests(unittest.TestCase):
    """release only ends what is over, so a host it cannot read leaves an unsettled launch alone."""
    setUp, snapshot, issue, prepare = DispatchTests.setUp, DispatchTests.snapshot, DispatchTests.issue, DispatchTests.prepare
    claimed, claim_row = CreateLifecycleTests.claimed, CreateLifecycleTests.claim_row

    def release(self, row, host):
        args = SimpleNamespace(stage='dispatch', dispatch_action='release', dispatch_id=row['dispatch_id'],
                               parent_id='parent-a', basis='give up')
        with patch.object(flow, 'host_call', host):
            return flow.cli(args)

    def lapsed(self, **changes):
        row = self.claimed()
        with registry.transaction() as connection:
            stored = registry.get(connection, 'dispatches', row['dispatch_id'])
            stored.update(state='expired', **changes)
            registry.save(connection, stored)
        return row

    def test_an_expired_launch_is_not_released_while_the_host_cannot_be_read(self):
        for changes in ({}, {'agent_id': 'live-worker'}):
            with self.subTest(changes=changes):
                row = self.lapsed(**changes)
                with self.assertRaisesRegex(ValueError, 'could not be read'):
                    self.release(row, lambda _: {'ok': False, 'error': 'host_timeout'})
                self.assertEqual(registry.inspect(row['dispatch_id'])['state'], 'expired')
                with registry.transaction() as connection:
                    connection.execute('DELETE FROM dispatches')

    def resumed(self, **stored_changes):
        row = registry.prepare(self.issue()['decision_id'], self.task, self.launch, self.usage, accounts=self.accounts,
                               now=self.now, resume_agent_id='old-worker', fallback_prompt='Only fixture work')
        row = self.claim_row(row)
        with registry.transaction() as connection:
            stored = registry.get(connection, 'dispatches', row['dispatch_id'])
            stored.update(stored_changes)
            registry.save(connection, stored)
        return stored

    def previous_turn(self, row, **changes):
        # The worker as a00ecb3a and 742b43c9 saw it: idle since long before this launch.
        return self.snapshot(row, status='idle', id='old-worker', attentionReason='finished',
                             lastUserMessageAt=(self.now - timedelta(days=2)).isoformat(), **changes)

    def never_bound(self, row):
        labels = dict(row['request']['labels'], **{'router.dispatch_id': 'dispatch-before', 'router.task_id': 'task-before'})
        return self.previous_turn(row, labels=labels, updatedAt=(self.now - timedelta(days=2)).isoformat())

    def test_a_resume_that_never_bound_can_be_released_once_its_lease_has_lapsed(self):
        row = self.resumed(lease_until=registry.stamp(self.now - timedelta(seconds=1)))
        released = self.release(row, lambda _: {'ok': True, 'agent': self.never_bound(row)})
        self.assertEqual(released['state'], 'canceled')
        self.assertFalse(registry.unresolved(registry.inspect(row['dispatch_id']), registry.utcnow()))

    def test_a_resume_that_never_bound_says_so_while_its_lease_runs(self):
        row = self.resumed()
        with self.assertRaises(ValueError) as refused:
            self.release(row, lambda _: {'ok': True, 'agent': self.never_bound(row)})
        self.assertIn("does not carry this dispatch's labels", str(refused.exception))
        self.assertNotIn('could not be read', str(refused.exception))
        self.assertIn(row['lease_until'], str(refused.exception))
        self.assertEqual(registry.inspect(row['dispatch_id'])['state'], 'launching')

    def test_a_resume_that_never_bound_is_still_refused_while_the_host_cannot_be_read(self):
        row = self.resumed(lease_until=registry.stamp(self.now - timedelta(seconds=1)))
        with self.assertRaisesRegex(ValueError, 'could not be read'):
            self.release(row, lambda _: {'ok': False, 'error': 'host_timeout'})
        self.assertEqual(registry.inspect(row['dispatch_id'])['state'], 'expired')

    def test_a_bound_resume_whose_turn_never_landed_keeps_no_lease_and_can_be_released(self):
        row = self.resumed()
        idle = self.previous_turn(row)
        watched = registry.observe(row['dispatch_id'], idle, now=self.now + timedelta(minutes=5))
        self.assertEqual((watched['state'], watched['lease_until']), ('launched', row['lease_until']))
        lapsed = registry.observe(row['dispatch_id'], idle, now=self.now + timedelta(minutes=11))
        self.assertEqual(lapsed['state'], 'expired')
        self.assertEqual(registry.observe(row['dispatch_id'], idle, now=self.now + timedelta(minutes=12))['state'], 'expired')
        self.assertEqual(self.release(row, lambda _: {'ok': True, 'agent': idle})['state'], 'canceled')

    def test_a_turn_that_lands_after_the_lease_brings_the_dispatch_back(self):
        landed = (self.now + timedelta(minutes=12)).isoformat()
        for changes, state in (({'status': 'running'}, 'started'),
                               ({'lastUserMessageAt': landed, 'status': 'initializing', 'attentionReason': None}, 'launched')):
            with self.subTest(state=state):
                row = self.resumed(task_id=state)
                registry.observe(row['dispatch_id'], self.previous_turn(row), now=self.now + timedelta(minutes=11))
                back = registry.observe(row['dispatch_id'], {**self.previous_turn(row), **changes},
                                        now=self.now + timedelta(minutes=12))
                self.assertEqual(back['state'], state)
                self.assertGreater(registry.parse(back['lease_until']), self.now + timedelta(minutes=12))
                with self.assertRaisesRegex(ValueError, 'Observe terminal status'):
                    registry.release(row['dispatch_id'], 'parent-a', 'give up', now=self.now + timedelta(minutes=12))
                with registry.transaction() as connection:
                    connection.execute('DELETE FROM dispatches')

    def watcher_marker(self, row):
        state = self.root / 'watcher-state'
        patcher = patch.dict(os.environ, {'ROUTER_STATE_DIR': str(state)})
        patcher.start()
        self.addCleanup(patcher.stop)
        notifications = state / 'paseo-notifications'
        notifications.mkdir(parents=True)
        (notifications / f"{row['dispatch_id']}.json").write_text('{}', encoding='utf-8')
        return notifications / f"{row['dispatch_id']}.json.creation-failed"

    def test_release_stops_the_dispatchs_watcher(self):
        row = self.resumed(lease_until=registry.stamp(self.now - timedelta(seconds=1)))
        marker = self.watcher_marker(row)
        self.release(row, lambda _: {'ok': True, 'agent': self.never_bound(row)})
        self.assertTrue(marker.exists())

    def test_release_of_what_was_already_over_leaves_its_watcher_to_finish(self):
        # A finished worker's notice may still be on its way.
        row = self.resumed()
        marker = self.watcher_marker(row)
        with registry.transaction() as connection:
            stored = registry.get(connection, 'dispatches', row['dispatch_id'])
            stored['state'] = 'completed'
            registry.save(connection, stored)
        self.assertEqual(self.release(row, lambda _: {'ok': True, 'agent': self.never_bound(row)})['state'], 'completed')
        self.assertFalse(marker.exists())

    def test_release_with_no_watcher_leaves_nothing_behind(self):
        row = self.prepare()
        marker = self.watcher_marker(row)
        (marker.parent / f"{row['dispatch_id']}.json").unlink()
        self.assertEqual(self.release(row, lambda _: self.fail('nothing was sent'))['state'], 'canceled')
        self.assertFalse(marker.exists())

    def test_a_marker_that_cannot_be_written_does_not_undo_the_release(self):
        row = self.resumed(lease_until=registry.stamp(self.now - timedelta(seconds=1)))
        marker = self.watcher_marker(row)
        with patch.object(Path, 'write_text', side_effect=PermissionError(5, 'denied')):
            self.assertEqual(self.release(row, lambda _: {'ok': True, 'agent': self.never_bound(row)})['state'], 'canceled')
        self.assertFalse(marker.exists())

    def test_the_same_release_goes_through_once_the_host_says_it_is_over(self):
        row = self.lapsed(agent_id='gone-worker')
        released = self.release(row, lambda _: {'ok': True, 'agent': None, 'missing': True})
        self.assertEqual(released['state'], 'canceled')

    def test_a_dispatch_never_sent_needs_no_host_to_be_released(self):
        row = self.prepare()

        def host(_):
            self.fail('nothing was sent, so there is nothing to observe')
        self.assertEqual(self.release(row, host)['state'], 'canceled')


class WatchLimitTests(unittest.TestCase):
    """--watch-seconds against the real host_call: a real process that never answers, whose child keeps stdout open."""
    setUp, snapshot, issue, prepare = DispatchTests.setUp, DispatchTests.snapshot, DispatchTests.issue, DispatchTests.prepare
    claimed, claim_row = CreateLifecycleTests.claimed, CreateLifecycleTests.claim_row

    @pytest.mark.slow
    def test_watch_seconds_is_a_hard_limit_on_a_hung_bridge(self):
        bridge = self.root / 'hung_bridge.mjs'
        bridge.write_text("import {spawn} from 'node:child_process';\n"
                          "spawn(process.execPath, ['-e', 'setTimeout(() => {}, 40000)'], {stdio: 'inherit', detached: true}).unref();\n"
                          "setTimeout(() => {}, 60000);\n", encoding='utf-8')
        row = self.claimed()
        with patch.dict(os.environ, {'ROUTER_PASEO_BRIDGE': str(bridge)}):
            started = time.monotonic()
            result = flow.launch(row['dispatch_id'], 'parent-a', watch_seconds=2, poll_seconds=1)
            elapsed = time.monotonic() - started
            self.assertEqual(result['state'], 'launching')
            self.assertEqual(result['observe_error'], 'host_timeout')
            self.assertTrue(result['continuation_argv'])
            self.assertLess(elapsed, 3.5)
            started = time.monotonic()
            flow.launch(row['dispatch_id'], 'parent-a', watch_seconds=0, poll_seconds=1)
            self.assertLess(time.monotonic() - started, flow.OBSERVE_FLOOR + 1.5)

    def test_each_observation_is_cut_at_what_is_left_of_the_watch(self):
        row = self.claimed()
        timeouts = []

        def host(request):
            timeouts.append(request['timeout_seconds'])
            return {'ok': False, 'error': 'host_timeout'}
        with patch.object(flow, 'OBSERVE_FLOOR', 0.1):
            flow.launch(row['dispatch_id'], 'parent-a', host, watch_seconds=2, poll_seconds=1)
        self.assertGreater(len(timeouts), 1)
        self.assertLessEqual(timeouts[0], 2)
        self.assertEqual(timeouts, sorted(timeouts, reverse=True))

    def run_on_a_clock(self, watch, poll, observation):
        """launch on a virtual clock against a host that takes `observation` seconds, up to the timeout it is given."""
        row = self.claimed()
        clock = [0.0]
        timeouts = []

        def host(request):
            timeouts.append(request['timeout_seconds'])
            clock[0] += min(observation, request['timeout_seconds'])
            return {'ok': False, 'error': 'host_timeout'}
        fake = SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
        with patch.object(flow, 'time', fake):
            flow.launch(row['dispatch_id'], 'parent-a', host, watch_seconds=watch, poll_seconds=poll)
        with registry.transaction() as connection:
            connection.execute('DELETE FROM dispatches')
        return clock[0], timeouts

    def test_no_watch_overruns_whatever_its_observations_cost(self):
        for watch, poll in ((10, 5), (10, 1), (7, 5), (30, 5), (1, 1), (120, 5)):
            for observation in (0.2, 1, 3, 4.6, 5.5, 9, 60):
                with self.subTest(watch=watch, poll=poll, observation=observation):
                    elapsed, _ = self.run_on_a_clock(watch, poll, observation)
                    self.assertLessEqual(elapsed, watch + 0.001)

    def test_only_a_first_observation_can_exceed_a_short_watch(self):
        elapsed, timeouts = self.run_on_a_clock(0, 1, 60)
        self.assertEqual((elapsed, timeouts), (flow.OBSERVE_FLOOR, [flow.OBSERVE_FLOOR]))

    def test_a_clock_crossing_the_floor_between_reads_cannot_make_a_negative_sleep(self):
        row = self.claimed()
        clock = [0.0]

        def monotonic():
            clock[0] += 0.01
            return clock[0]

        def sleep(seconds):
            if seconds < 0:
                raise ValueError('sleep length must be non-negative')
            clock[0] += seconds

        def host(_):
            if clock[0] < 4:  # The next read leaves the floor plus 5 ms, and a read after it less than the floor.
                clock[0] = 10.01 - flow.OBSERVE_FLOOR - 0.015
            return {'ok': False, 'error': 'host_timeout'}
        with patch.object(flow, 'time', SimpleNamespace(monotonic=monotonic, sleep=sleep)):
            self.assertEqual(flow.launch(row['dispatch_id'], 'parent-a', host, watch_seconds=10, poll_seconds=5)['state'], 'launching')

    def test_a_sleep_leaves_room_for_the_next_observation(self):
        _, timeouts = self.run_on_a_clock(10, 5, 4.6)
        self.assertEqual([round(t, 1) for t in timeouts], [10, 5])

    def test_an_expired_launch_that_recovers_is_observed_once_on_a_zero_watch(self):
        row = self.claimed()
        with registry.transaction() as connection:
            stored = registry.get(connection, 'dispatches', row['dispatch_id'])
            stored['state'] = 'expired'
            registry.save(connection, stored)
        observations = []

        def host(request):
            observations.append(request['operation'])
            return {'ok': True, 'agent': self.snapshot(row, updatedAt=registry.stamp(registry.utcnow()))}
        self.assertEqual(flow.launch(row['dispatch_id'], 'parent-a', host, watch_seconds=0)['state'], 'started')
        self.assertEqual(observations, ['observe'])

    def test_a_killed_bridge_is_reaped(self):
        calls = []

        class Hung:
            def __init__(self, *args, **kwargs):
                pass

            def wait(self, timeout=None):
                calls.append(('wait', timeout))
                if timeout is not None:
                    raise flow.subprocess.TimeoutExpired('node', timeout)

            def kill(self):
                calls.append(('kill', None))
        with patch.object(flow.subprocess, 'Popen', Hung):
            result = flow.host_call({'operation': 'observe', 'timeout_seconds': 1})
        self.assertEqual(result['error'], 'host_timeout')
        self.assertEqual(calls, [('wait', 1), ('kill', None), ('wait', None)])


class WorkspaceWriterTests(unittest.TestCase):
    """One writer per canonical workspace directory, decided from recorded dispatches, the host's running agents and the classifier field."""
    setUp, snapshot, issue = DispatchTests.setUp, DispatchTests.snapshot, DispatchTests.issue

    def ws_dir(self, name='W'):
        target = self.root / name
        target.mkdir(parents=True, exist_ok=True)
        return str(target)

    def prepare_in(self, workspace=None, task_id='writer', **options):
        ws = workspace if workspace is not None else self.ws_dir('W')
        task = dict(self.task, task_id=task_id, expected_hours=0.001)
        launch = {'title': 'A task', 'initialPrompt': 'Only fixture work', 'cwd': ws}
        return registry.prepare(self.issue(task)['decision_id'], task, launch, self.usage,
                                accounts=self.accounts, now=self.now, workspace=ws, **options)

    def test_each_open_state_of_a_writer_holds_the_workspace_and_terminal_ones_do_not(self):
        first = self.prepare_in()
        for state in ('prepared', 'launching', 'launched', 'started'):
            with self.subTest(state=state):
                with registry.transaction() as connection:
                    row = registry.get(connection, 'dispatches', first['dispatch_id'])
                    row['state'] = state
                    registry.save(connection, row)
                with self.assertRaises(registry.WorkspaceBusy) as caught:
                    self.prepare_in(task_id='second')
                self.assertEqual(caught.exception.holders[0]['dispatch_id'], first['dispatch_id'])
        for state in ('completed', 'failed', 'create_failed', 'canceled'):
            with self.subTest(state=state):
                with registry.transaction() as connection:
                    row = registry.get(connection, 'dispatches', first['dispatch_id'])
                    row['state'] = state
                    registry.save(connection, row)
                other = self.prepare_in(task_id='third-' + state)
                with registry.transaction() as connection:
                    connection.execute('DELETE FROM dispatches WHERE id=?', (other['dispatch_id'],))

    def test_other_workspaces_and_no_workspace_are_unaffected(self):
        self.prepare_in()
        self.assertEqual(self.prepare_in(self.ws_dir('other'), task_id='elsewhere')['state'], 'prepared')

    def test_read_only_coexists_both_ways(self):
        ws = self.ws_dir('W')
        reader = self.prepare_in(ws, task_id='reader', access='read_only')
        writer = self.prepare_in(ws, task_id='writer')
        self.assertEqual(self.prepare_in(ws, task_id='reader-2', access='read_only')['state'], 'prepared')
        self.assertEqual(registry.writers_at(ws, now=self.now)[0]['dispatch_id'], writer['dispatch_id'])
        self.assertEqual(reader['access'], 'read_only')

    def test_a_row_recorded_before_access_existed_counts_as_a_writer(self):
        row = self.prepare_in()
        with registry.transaction() as connection:
            stored = registry.get(connection, 'dispatches', row['dispatch_id'])
            del stored['access']
            registry.save(connection, stored)
        with self.assertRaises(registry.WorkspaceBusy):
            self.prepare_in(task_id='second')

    def test_access_is_the_flag_then_classifier_access_then_read_only_review_then_write_and_never_the_text(self):
        import roll
        cases = (
            (None, None, 'write'), (None, False, 'write'), (None, 'true', 'write'), (None, 1, 'write'),
            (None, True, 'read_only'), ('write', True, 'write'), ('read-only', False, 'read_only'), ('read-only', None, 'read_only'),
            (None, {'access': 'read-only'}, 'read_only'), (None, {'access': 'write'}, 'write'),
            (None, {'access': 'write', 'read_only_review': True}, 'write'),
            ('write', {'access': 'read-only'}, 'write'), ('read-only', {'access': 'write'}, 'read_only'),
        )
        for flag, field, expected in cases:
            with self.subTest(flag=flag, field=field):
                # A title and brief full of review words change nothing.
                args = SimpleNamespace(access=flag, title='Read-only independent code review 只读审查', brief=None)
                self.assertEqual(roll.task_access(args, field), expected)

    def holders(self, agents, parent='parent-a'):
        return flow.workspace_holders(self.ws_dir('W'), parent, host=lambda _: {'ok': True, 'agents': agents, 'placeable': True})

    def test_a_running_router_agent_holds_the_workspace_even_after_its_dispatch_completed(self):
        row = self.prepare_in()
        with registry.transaction() as connection:
            stored = registry.get(connection, 'dispatches', row['dispatch_id'])
            stored['state'] = 'completed'
            registry.save(connection, stored)
        holders, error, _ = self.holders([{'id': 'a1', 'status': 'running', 'dispatch_id': row['dispatch_id']}])
        self.assertIsNone(error)
        self.assertEqual([(h['agent_id'], h['agent_status']) for h in holders], [('a1', 'running')])

    def test_running_read_only_agents_and_the_caller_itself_do_not_hold_it(self):
        reader = self.prepare_in(task_id='reader', access='read_only')
        holders, _, _ = self.holders([{'id': 'a1', 'status': 'running', 'dispatch_id': reader['dispatch_id']},
                                   {'id': 'parent-a', 'status': 'running', 'dispatch_id': 'unknown-dispatch'}])
        self.assertEqual(holders, [])
        holders, _, _ = self.holders([{'id': 'a2', 'status': 'running', 'dispatch_id': 'unknown-dispatch'}])
        self.assertEqual(holders[0]['agent_id'], 'a2')

    def started_as(self, agent, task_id):
        row = self.prepare_in(task_id=task_id)
        with registry.transaction() as connection:
            stored = registry.get(connection, 'dispatches', row['dispatch_id'])
            stored.update(state='started', launched_at=registry.stamp(self.now), agent_id=agent,
                          lease_until=registry.stamp(self.now + timedelta(seconds=600)))
            registry.save(connection, stored)
        return row

    def test_the_callers_own_dispatch_does_not_hold_its_workspace_for_it(self):
        own = self.started_as('parent-a', 'coordinator')
        running = [{'id': 'parent-a', 'status': 'running', 'dispatch_id': own['dispatch_id']}]
        self.assertEqual(flow.workspace_holders(self.ws_dir('W'), 'parent-a', host=lambda _: {'ok': True, 'agents': running}), ([], None, None))
        self.assertEqual(self.prepare_in(task_id='next')['state'], 'prepared')

    def test_anothers_dispatch_still_holds_it_and_so_do_the_callers_own_workers(self):
        ws = self.ws_dir('W')
        self.started_as('parent-a', 'coordinator')
        other = self.started_as('someone-else', 'other')
        holders, _, _ = flow.workspace_holders(ws, 'parent-a', host=lambda _: {'ok': True, 'agents': []})
        self.assertEqual([h['dispatch_id'] for h in holders], [other['dispatch_id']])
        with self.assertRaises(registry.WorkspaceBusy) as caught:
            self.prepare_in(ws, task_id='next')
        self.assertEqual([h['dispatch_id'] for h in caught.exception.holders], [other['dispatch_id']])
        # As seen by the other agent, the coordinator is the one in its way.
        self.assertEqual(len(registry.writers_at(ws, caller='someone-else', now=self.now)), 1)

    def test_an_unreadable_host_leaves_recorded_writers_in_force(self):
        row = self.prepare_in()
        holders, error, _ = flow.workspace_holders(self.ws_dir('W'), 'parent-a', host=lambda _: {'ok': False, 'error': 'host_timeout'})
        self.assertEqual(error, 'host_timeout')
        self.assertEqual(holders[0]['dispatch_id'], row['dispatch_id'])

    def test_concurrent_prepare_lets_exactly_one_writer_in(self):
        ws = self.ws_dir('W')
        decisions = []
        for index in range(2):
            task = dict(self.task, task_id=f'racer-{index}', expected_hours=0.001)
            decisions.append((self.issue(task)['decision_id'], task))
        launch = {'title': 'A task', 'initialPrompt': 'Only fixture work', 'cwd': ws}
        with ThreadPoolExecutor(2) as pool:
            def attempt(item):
                try:
                    return registry.prepare(item[0], item[1], dict(launch), self.usage,
                                            accounts=self.accounts, now=self.now, workspace=ws)['state']
                except registry.WorkspaceBusy:
                    return 'busy'
            self.assertEqual(sorted(pool.map(attempt, decisions)), ['busy', 'prepared'])


class RollLifecycleTests(unittest.TestCase):
    """roll end to end against a fake host and a fake judge."""
    setUp, snapshot, issue = DispatchTests.setUp, DispatchTests.snapshot, DispatchTests.issue
    roll = RollTests.roll
    brief = RollTests.brief

    def cooldowns(self):
        store = self.root / 'cooldowns.json'
        return store, patch.dict(os.environ, {'ROUTER_LAUNCH_FAILURES': str(store)})

    def setUp(self):
        DispatchTests.setUp(self)
        self.creates = []
        self.create_requests = []
        self.running = []
        self.placeable = True
        self.placement_error = None
        self.enclosing_directories = []

    def ws_dir(self, name='W'):
        target = self.root / name
        target.mkdir(parents=True, exist_ok=True)
        return str(target)

    def host(self, request):
        operation = request['operation']
        if operation == 'workspace_agents':
            out = {'ok': True, 'agents': list(self.running), 'cwd': request.get('cwd'), 'placeable': self.placeable}
            if not self.placeable and self.placement_error:
                out['error'] = self.placement_error
            if self.enclosing_directories:
                out['enclosing_directories'] = list(self.enclosing_directories)
            return out
        if operation == 'create':
            self.creates.append(request['dispatch_id'])
            self.create_requests.append(dict(request['request']))
            return {'ok': False, 'sent': True, 'error': 'host_timeout'}
        return {'ok': True, 'agent': None, 'creation': {'phase': 'accepted'}}

    def decision(self):
        return {'stage': 'plan', 'choice': 'lab_sonnet55_high', 'selected_bundle': self.bundle,
                'decision': {'action': 'start'}, 'task_profile': {'interaction_style': 'engineering'}}

    def fresh_decision(self):
        decision = self.decision()
        decision.pop('decision_id', None)
        return decision

    def go(self, **flags):
        flags.setdefault('hours', 0.001)
        flags.setdefault('task', None)
        flags.setdefault('task_id', None)
        flags.setdefault('new_worker', False)
        flags.setdefault('resume', None)
        flags.setdefault('fast', False)
        flags.setdefault('keep_whole', None)
        flags.setdefault('access', None)
        flags.setdefault('account', None)
        flags.setdefault('verbose', True)
        with patch.object(flow, 'host_call', self.host), patch.object(flow, 'launch', wraps=flow.launch):
            (result, _), _ = self.roll(self.fresh_decision(), reachable=True, **flags)
        return result

    def test_no_interaction_style_exempts_a_brief_from_the_writer_rule(self):
        # text_only on a real ledger entry was a brief that committed code and synced it.
        ws = self.ws_dir('W')
        first = self.go(workspace=ws)
        for style in ('exploration', 'text_only', 'advisory'):
            with self.subTest(style=style), patch.object(self, 'decision', lambda: {
                    **RollLifecycleTests.decision(self), 'task_profile': {'interaction_style': style}}):
                result = self.go(workspace=ws, new_worker=True)
                self.assertEqual(result['status'], 'blocked')
                self.assertEqual(result['blocked_by'][0]['dispatch_id'], first['worker']['dispatch_id'])
        self.assertEqual(len(self.creates), 1)
        # Classifier access='read-only' or read_only_review=True exempts the brief as read_only unless --access write overrides it.
        for ro_profile in ({'interaction_style': 'exploration', 'access': 'read-only'},
                           {'interaction_style': 'engineering', 'read_only_review': True}):
            with self.subTest(profile=ro_profile), patch.object(self, 'decision', lambda: {
                    **RollLifecycleTests.decision(self), 'task_profile': ro_profile}):
                ro_result = self.go(workspace=ws, new_worker=True)
                self.assertEqual(ro_result['status'], 'pending')
                self.assertEqual(ro_result['worker']['access'], 'read_only')
                forced_write = self.go(workspace=ws, new_worker=True, access='write')
                self.assertEqual(forced_write['status'], 'blocked')
        self.assertEqual(len(self.creates), 3)

    def in_cwd(self, cwd):
        return patch.dict(os.environ, {'PASEO_AGENT_CWD': cwd})

    def test_no_workspace_names_other_writers_in_the_same_directory_and_blocks_nothing(self):
        dir_a = self.ws_dir('work-A')
        with self.in_cwd(dir_a):
            first = self.go()
            second = self.go(new_worker=True)
        self.assertEqual(second['status'], 'pending')
        self.assertEqual(len(self.creates), 2)
        self.assertNotIn('warnings', first)
        self.assertTrue(any(first['worker']['dispatch_id'] in note for note in second['warnings']))
        self.assertTrue(any(first['worker']['dispatch_id'] in note for note in second['assumptions']))
        alt = (dir_a.replace('/', '\\').lower() + '\\') if os.name == 'nt' else (dir_a + '/')
        with self.in_cwd(alt):
            compact = self.go(new_worker=True, verbose=False)
        self.assertEqual(compact['status'], 'pending')
        self.assertIn(first['worker']['dispatch_id'], compact['warnings'][0])

    def resuming(self, cwd):
        import roll
        worker = {'agent_id': 'worker-x'}
        return (patch.object(registry, 'reusable_workers', return_value=[worker]),
                patch.object(roll, '_bundle_matches_worker', return_value=True),
                patch.object(roll, 'match_reusable_worker', return_value=worker),
                patch.object(registry, 'paseo_agent_record', return_value={'cwd': cwd} if cwd else None))

    def test_a_resume_with_no_workspace_warns_about_where_the_worker_lives_not_the_parent(self):
        dir_y = self.ws_dir('work-Y')
        dir_p = self.ws_dir('work-P')
        with self.in_cwd(dir_y):
            there = self.go(task_id='writer-there')['worker']['dispatch_id']
        with self.in_cwd(dir_p):
            here = self.go(task_id='writer-here')['worker']['dispatch_id']
            workers, matches, match, record = self.resuming(dir_y)
            with workers, matches, match, record:
                resumed = self.go(task_id='follow-up')
        row = registry.inspect(resumed['worker']['dispatch_id'])
        self.assertEqual(row['operation'], 'resume')
        self.assertEqual(Path(row['cwd']).resolve(), Path(dir_y).resolve())
        self.assertEqual(resumed['status'], 'pending')
        self.assertTrue(any(there in note for note in resumed['warnings']))
        self.assertFalse(any(here in note for note in resumed['warnings']))

    def test_a_resumed_worker_of_unknown_directory_is_taken_to_be_in_the_parents(self):
        dir_p = self.ws_dir('work-P2')
        with self.in_cwd(dir_p):
            here = self.go(task_id='writer-here')['worker']['dispatch_id']
            workers, matches, match, record = self.resuming(None)
            with workers, matches, match, record:
                resumed = self.go(task_id='follow-up')
        self.assertEqual(registry.inspect(resumed['worker']['dispatch_id'])['operation'], 'resume')
        self.assertTrue(any(here in note for note in resumed['warnings']))

    def test_no_warning_elsewhere_for_a_reader_or_once_the_writer_has_ended(self):
        dir_a = self.ws_dir('warn-A')
        dir_b = self.ws_dir('warn-B')
        with self.in_cwd(dir_a):
            self.go()
            reader = self.go(new_worker=True, access='read-only')
        self.assertNotIn('warnings', reader)
        with self.in_cwd(dir_b):
            self.assertNotIn('warnings', self.go(new_worker=True))
        with registry.transaction() as connection:
            for row in registry.rows(connection, 'dispatches'):
                row['state'] = 'completed'
                registry.save(connection, row)
        with self.in_cwd(dir_a):
            self.assertNotIn('warnings', self.go(new_worker=True))

    def test_an_observed_worker_counts_in_the_directory_it_runs_in(self):
        ws = self.ws_dir('W')
        moved = self.ws_dir('moved-W')
        first = self.go(workspace=ws)
        row = registry.inspect(first['worker']['dispatch_id'])
        snapshot = self.snapshot(row, updatedAt=registry.stamp(registry.utcnow()), cwd=moved, status='running')
        registry.observe(row['dispatch_id'], snapshot)
        with self.in_cwd(moved):
            result = self.go(new_worker=True)
        self.assertIn(first['worker']['dispatch_id'], result['warnings'][0])

    def test_same_canonical_directory_across_spellings_and_hosts_blocks_second_writer(self):
        import host_placement
        ws_path = self.root / 'MixedCaseDir'
        ws_path.mkdir(parents=True, exist_ok=True)
        canon = host_placement.canonical_dir(str(ws_path))
        slash_variant = str(ws_path).replace('\\', '/') + '/'
        case_variant = (str(ws_path).upper() + '\\') if os.name == 'nt' else slash_variant

        # First writer claimed on Paseo with slash variant -> blocks Paseo with case/backslash variant, and holds zero host IDs.
        first = self.go(workspace=slash_variant)
        self.assertEqual(first['status'], 'pending')
        stored = registry.inspect(first['worker']['dispatch_id'])
        self.assertEqual(stored['workspace'], canon)
        self.assertNotIn('workspaceId', stored['request'])

        blocked_paseo = self.go(workspace=case_variant, new_worker=True)
        self.assertEqual(blocked_paseo['status'], 'blocked')
        self.assertEqual(blocked_paseo['error'], 'workspace_has_active_writer')
        self.assertEqual(blocked_paseo['blocked_by'][0]['dispatch_id'], first['worker']['dispatch_id'])

        # Same canonical directory also blocks writers on native/orca/t3/herdr/cli, and vice versa.
        for surface, parent_host, parent_id, parent_addr in (
            ('orca', 'orca', 'orca-sess', 'orca_session_id:orca-sess'),
            ('t3', 't3', 't3-thread', 't3-thread'),
            ('native', 'native', 'cindy-sess', 'run:1'),
        ):
            with self.subTest(blocked_surface=surface):
                (native_res, native_code), _ = self.roll(
                    self.fresh_decision(), parent='', reachable=False, workspace=case_variant, new_worker=True,
                    parent_host=parent_host, parent_id=parent_id, parent_address=parent_addr,
                    hours=0.001, task=None, task_id=None, resume=None, fast=False, keep_whole=None,
                    access=None, account=None, verbose=True)
                self.assertEqual((native_res['status'], native_code), ('blocked', 2))
                self.assertEqual(native_res['blocked_by'][0]['dispatch_id'], first['worker']['dispatch_id'])

        # Conversely, a writer claimed on a native host blocks a subsequent Paseo writer in the same directory.
        ws_native = self.ws_dir('native-held')
        (held_native, held_code), _ = self.roll(
            self.fresh_decision(), parent='', reachable=False, workspace=ws_native, new_worker=True,
            parent_host='orca', parent_id='orca-owner', parent_address='orca_session_id:orca-owner',
            hours=0.001, task=None, task_id='native-writer', resume=None, fast=False, keep_whole=None,
            access=None, account=None, verbose=True)
        self.assertEqual((held_native['dispatch_mode'], held_code), ('native', 0))
        self.assertEqual(held_native['workspace'], host_placement.canonical_dir(ws_native))
        after_native = self.go(workspace=ws_native.replace('\\', '/') + '/', new_worker=True)
        self.assertEqual(after_native['status'], 'blocked')
        self.assertEqual(after_native['error'], 'workspace_has_active_writer')

    def test_invalid_workspace_directory_or_host_id_launches_nothing_and_records_no_cooldown(self):
        missing = str(self.root / 'does-not-exist')
        regular_file = self.root / 'not-a-dir.txt'
        regular_file.write_text('x', encoding='utf-8')
        store, scope = self.cooldowns()
        with scope:
            for bad in (missing, str(regular_file), 'wks_c40ae4f1bd0432d3'):
                with self.subTest(bad=bad), patch.object(flow, 'host_call', side_effect=AssertionError('must not touch host')):
                    (res, code), seen = self.roll(
                        self.fresh_decision(), reachable=True, workspace=bad,
                        hours=0.001, task=None, task_id=None, new_worker=False, resume=None, fast=False,
                        keep_whole=None, access=None, account=None, verbose=True)
                self.assertEqual((res['status'], code), ('error', 1))
                self.assertEqual(res['error'], 'invalid_workspace_directory')
                self.assertEqual(res['caller_action'], 'check_error')
                self.assertEqual(seen, {})
        self.assertEqual(self.creates, [])
        self.assertFalse(store.exists())

    def test_paseo_unplaceable_directory_refuses_before_create_with_no_cooldown_or_fallback(self):
        ws = self.ws_dir('games/space_trader')
        enclosing = str(self.root / 'games')
        self.placeable = False
        self.placement_error = 'paseo_workspace_not_found_for_directory'
        self.enclosing_directories = [enclosing]
        store, scope = self.cooldowns()
        with scope:
            res = self.go(workspace=ws)
        self.assertEqual(res['status'], 'error')
        self.assertEqual(res['error'], 'paseo_workspace_not_found_for_directory')
        self.assertEqual(res['caller_action'], 'check_error')
        self.assertIn(enclosing, res['next'])
        self.assertEqual(self.creates, [])
        self.assertFalse(store.exists())

    def test_a_writer_in_a_workspace_is_named_to_a_roll_with_none_before_its_worker_exists(self):
        checkout = self.ws_dir('checkout')
        first = self.go(workspace=checkout)
        with self.in_cwd(checkout):
            result = self.go(new_worker=True)
        self.assertIn(first['worker']['dispatch_id'], result['warnings'][0])

    def test_an_unreadable_host_refuses_a_workspace_roll_instead_of_failing_open(self):
        ws = self.ws_dir('W')
        self.placeable = False
        self.placement_error = 'paseo_workspaces_unreadable'
        self.enclosing_directories = []
        result = self.go(workspace=ws)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['error'], 'paseo_workspaces_unreadable')
        self.assertEqual(self.creates, [])

    def test_new_worker_beats_the_open_dispatch_of_the_same_brief(self):
        first = self.go()
        again = self.go()
        self.assertEqual(again['worker']['dispatch_id'], first['worker']['dispatch_id'])
        self.assertEqual(len(self.creates), 1)
        fresh = self.go(new_worker=True)
        self.assertNotEqual(fresh['worker']['dispatch_id'], first['worker']['dispatch_id'])
        self.assertEqual(len(self.creates), 2)

    def test_second_writer_in_a_workspace_is_blocked_even_with_new_worker(self):
        ws = self.ws_dir('W')
        first = self.go(workspace=ws)
        self.assertEqual(first['status'], 'pending')
        result = self.go(workspace=ws, new_worker=True)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['error'], 'workspace_has_active_writer')
        self.assertEqual(result['blocked_by'][0]['dispatch_id'], first['worker']['dispatch_id'])
        for needle in (first['worker']['dispatch_id'], '--access read-only'):
            self.assertIn(needle, result['next'])
        self.assertNotIn('--allow-concurrent', result['next'])
        self.assertEqual(len(self.creates), 1)

    def test_compact_blocked_names_the_holder_and_the_way_out(self):
        ws = self.ws_dir('W')
        first = self.go(workspace=ws)
        compact = self.go(workspace=ws, new_worker=True, verbose=False)
        self.assertEqual(compact['status'], 'blocked')
        self.assertEqual(compact['blocked_by'][0]['dispatch_id'], first['worker']['dispatch_id'])
        self.assertIn('--access read-only', compact['next_action'])
        self.assertNotIn('--allow-concurrent', compact['next_action'])
        self.assertEqual(compact['cause'], 'workspace_has_active_writer')

    def test_a_running_router_agent_blocks_a_workspace_with_no_open_dispatch(self):
        ws = self.ws_dir('W')
        self.running = [{'id': 'old-worker', 'status': 'running', 'dispatch_id': 'unknown'}]
        result = self.go(workspace=ws)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['blocked_by'][0]['agent_id'], 'old-worker')
        self.assertEqual(self.creates, [])

    def test_read_only_flag_gets_through_and_workspace_infers_local_only_reason(self):
        import host_placement
        import roll
        ws = self.ws_dir('W')
        canon = host_placement.canonical_dir(ws)
        self.go(workspace=ws)
        reader = self.go(workspace=ws, new_worker=True, access='read-only')
        self.assertEqual(reader['status'], 'pending')
        self.assertEqual(reader['worker']['access'], 'read_only')
        args = SimpleNamespace(hours=0.5, task_id='t-ws', local_only=None, workspace=canon, keep_whole=None,
                               file=[], provider=None, account=None, model=None, effort=None, basis=None,
                               fast=False, can_wait_hours=None)
        task = roll.build_task(args, 'implement feature', 'parent-a')
        self.assertEqual(task['needs'], {'local_only': True, 'local_only_reason': f'inferred from --workspace {canon}'})
        explicit = SimpleNamespace(**{**vars(args), 'local_only': 'needs local DB'})
        self.assertEqual(roll.build_task(explicit, 'implement feature', 'parent-a')['needs'],
                         {'local_only': True, 'local_only_reason': 'needs local DB'})

    def test_exit_status_of_a_settled_create_separates_candidate_from_request_refusal(self):
        import roll

        def settled_unclassified(request):
            return {'ok': True, 'agent': None, 'creation': {'phase': 'failed', 'failure_class': 'unknown'}}

        def settled_candidate(request):
            return {'ok': True, 'agent': None, 'creation': {'phase': 'failed', 'failure_class': 'model_unavailable'}}

        args = SimpleNamespace(title='fixture', workspace=None)
        decision = {'decision_id': 'unused', 'selected_bundle': self.bundle}

        first = self.go()
        with patch.object(flow, 'host_call', settled_unclassified), \
                patch.object(registry, 'prepare', return_value=registry.inspect(first['worker']['dispatch_id'])):
            req_result, req_code = roll.paseo(args, {}, decision, self.task, 'fixture', 'parent-a', self.usage)
        self.assertEqual((roll.status_of(req_result, req_code), req_code), ('error', 1))
        self.assertEqual(req_result['caller_action'], 'check_error')

        # Unclassified refusal recorded no cooldown, so the same bundle can still be prepared and launched next.
        second = self.go(new_worker=True)
        with patch.object(flow, 'host_call', settled_candidate), patch.dict(os.environ, {'AGENTCOMPASS_ROLL_FALLBACK': 'off'}), \
                patch.object(registry, 'prepare', return_value=registry.inspect(second['worker']['dispatch_id'])):
            result, code = roll.paseo(args, {}, decision, self.task, 'fixture', 'parent-a', self.usage)
        self.assertEqual((roll.status_of(result, code), code), ('error', 1))
        self.assertEqual(result['caller_action'], 'roll_again_safe')
        self.assertIn('safe to roll', result['next'])


def started(row_or_request, status='running', **changes):
    """The host snapshot of the worker a create request (or a prepared row) asked for, as the bridge projects it."""
    request = row_or_request.get('request') if isinstance(row_or_request.get('request'), dict) else row_or_request
    dispatch_id = request['labels']['router.dispatch_id'] if 'labels' in request else row_or_request['dispatch_id']
    provider, _, model = request['provider'].partition('/')
    settings = request.get('settings') or {}
    return {'id': dispatch_id, 'provider': provider, 'model': model, 'status': status,
            'currentModeId': settings.get('modeId'), 'thinkingOptionId': settings.get('thinkingOptionId'),
            'updatedAt': registry.stamp(registry.utcnow() + timedelta(seconds=5)), 'labels': request['labels'], **changes}


class ModelLatchTests(unittest.TestCase):
    """2026-11-09: agy stopped offering Gemini 4 Argon, discovery still listed it, and three rolls in a row picked it and
    failed. A create the provider refused with a structured model code latches that model out of routing."""
    setUp_dispatch, snapshot = DispatchTests.setUp, DispatchTests.snapshot
    issue, prepare = DispatchTests.issue, DispatchTests.prepare
    brief = RollTests.brief

    def setUp(self):
        self.setUp_dispatch()
        # A second account that can take the work when the first one's model is refused.
        self.usage['accounts']['codex'] = {'status': 'fresh', 'fetched_at': self.now.isoformat(), 'windows': [
            {'id': 'usage', 'used_pct': 0, 'remaining_pct_before_reset': 100,
             'resets_at': (self.now + timedelta(hours=100)).isoformat(), 'seconds_until_reset': 100 * 3600}]}
        self.store = self.root / 'model-unavailable.json'
        env = patch.dict(os.environ, {'ROUTER_MODEL_LATCH': str(self.store), 'ROUTER_STATE_DIR': str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.creates, self.choices, self.chosen, self.refused = [], [], [], None
        self.refusal = {'ok': False, 'sent': False, 'error': 'host_creation_failed', 'failure_class': 'unknown',
                        'failure_code': 'INVALID_MODEL'}

    def host(self, request):
        if request['operation'] == 'create':
            self.creates.append(request['request']['provider'])
            return dict(self.refusal)
        return {'ok': True, 'agent': None, 'creation': None}

    def roll(self, host=None):
        """One real roll: the fake judge plans with the allocator, so a latch changes what is chosen."""
        import argparse
        import roll
        brief = self.root / 'task.md'
        brief.write_bytes(self.brief.encode('utf-8'))
        args = argparse.Namespace(brief=brief, file=[], apply_to=None, local_only=None, hours=0.5, title=None,
            workspace=None, task_id=f'test-task-{len(self.choices)}', parent_id=None, parent_host=None,
            parent_address=None, provider=None, model=None, effort=None, basis=None, usage_cache=None, dry_run=False,
            verbose=False, task=None, new_worker=True, resume=None, fast=False, keep_whole=None, access=None,
            account=None)

        def plan_stage(document, _):
            planned = allocator.plan(dict(self.task), self.usage, self.now, accounts=self.accounts)
            self.choices.append(planned['choice'])
            self.chosen.append(planned.get('selected_bundle') or {})
            return dict(planned, **registry.issue(planned, document['task'], self.now)), 0
        judge = type('Judge', (), {'plan_stage': staticmethod(plan_stage),
                                   'usage_snapshot': staticmethod(lambda _: self.usage),
                                   'subscription_env': staticmethod(lambda account=None: dict(os.environ)),
                                   'cli_prefix': staticmethod(lambda name: [name])})
        with patch.dict(os.environ, {'PASEO_AGENT_ID': 'parent-a', 'ROUTER_PARENT_AGENT_ID': ''}), \
                patch.object(roll, 'WORKER_PROMPTS_DIR', self.root / 'worker-prompts'), \
                patch.object(roll, 'RESULTS_DIR', self.root / 'roll-results'), \
                patch.object(flow, 'paseo_reachable', return_value=True), \
                patch.object(flow, 'host_call', host or self.host), \
                patch.object(account_fleet, 'load_accounts', return_value=self.accounts):
            return roll.run(args, judge)

    def refuse_first(self, request):
        """Paseo refuses the model of the first create with INVALID_MODEL and starts anything else."""
        if request['operation'] == 'create':
            provider = request['request']['provider']
            self.creates.append(provider)
            self.refused = self.refused or provider
            if provider == self.refused:
                return dict(self.refusal)
            return {'ok': True, 'sent': True, 'agent': started(request), 'completion_notification': {'status': 'watching'}}
        return {'ok': True, 'agent': None, 'creation': None}

    def test_a_refused_model_is_not_chosen_by_the_next_roll_and_the_roll_says_why(self):
        import model_latch
        # The same roll relaunches on the next candidate (2026-11-10: the parent no longer rolls again).
        first, code = self.roll(self.refuse_first)
        self.assertEqual((first['status'], code), ('launched', 0))
        self.assertEqual(self.creates[0], self.refused)
        top = self.chosen[0]
        self.assertNotEqual(self.chosen[1]['model'], top['model'])       # every effort of that model
        self.assertIn('INVALID_MODEL', first['attempts'][0])
        self.assertIn('switched to', first['attempts'][0])
        latched = set(model_latch.active())
        self.assertEqual(len(latched), 1)
        second, _ = self.roll(self.refuse_first)
        self.assertNotEqual(self.chosen[2]['model'], top['model'])
        self.assertEqual(len(self.creates), 3)
        self.assertNotIn(self.refused, self.creates[1:])
        self.assertTrue(any(w.startswith(f'{next(iter(latched))}: model launch failed: unavailable')
                            for w in second['warnings']), second)
        # The user's model checkboxes are untouched: the latch lives in its own file.
        self.assertFalse((self.root / 'accounts-override.json').exists())
        plan = allocator.plan(dict(self.task), self.usage, self.now, accounts=self.accounts)
        excluded = next(c for c in plan['ranking'] if c['id'] == self.choices[0])['excluded']
        self.assertTrue(any('next retry at' in e for e in excluded), excluded)

    def test_a_failure_without_a_model_code_latches_nothing(self):
        import model_latch
        self.refusal.update(failure_code='PROVIDER_CRASHED')
        self.roll()
        self.refusal.pop('failure_code')
        self.roll()
        self.assertEqual(model_latch.active(), {})

    def test_a_worker_that_errors_after_create_is_relaunched_on_the_next_candidate(self):
        # The incident path: Paseo created the agent, it ran for a moment, then stopped in error with only text. Every
        # created worker errors here, so AgentCompass relaunches until no candidate is left and then says so.
        def host(request):
            if request['operation'] == 'create':
                self.creates.append(request['request']['provider'])
                return {'ok': True, 'sent': True, 'agent': None}
            row = registry.inspect(request['dispatch_id'])
            return {'ok': True, 'agent': started(row, 'error', lastError='invalid model selection')}
        result, code = self.roll(host)
        self.assertEqual((result['status'], code), ('blocked', 2))
        self.assertEqual(result['cause'], 'no_candidate_left')
        self.assertIn('every startable candidate', result['next_action'])
        self.assertEqual(len(result['attempts']), len(self.creates))
        self.assertTrue(all('worker_error' in line for line in result['attempts']), result['attempts'])
        self.assertEqual(len(set(self.creates)), len(self.creates))   # each bundle once
        detail = json.loads(Path(result['details']).read_text(encoding='utf-8'))
        self.assertEqual((detail['cause'], detail['next_action']), (result['cause'], result['next_action']))
        import model_latch
        self.assertEqual(model_latch.active(), {})   # Error text is never read, so nothing is latched from it.

    def test_each_unlock_condition(self):
        import model_latch
        from datetime import datetime, timezone
        t0 = datetime(2026, 11, 9, 2, 46, tzinfo=timezone.utc)
        bundle = {'account': 'antigravity', 'provider': 'antigravity-resilient', 'model': 'gemini-4-argon',
                  'launch_model': 'gemini-4-argon-high'}
        entry = model_latch.record(bundle, 'INVALID_MODEL', at=t0, source='d1')
        self.assertEqual(entry['model'], 'gemini-4-argon-high')
        self.assertIsNone(model_latch.record(bundle, 'unknown', at=t0))
        latched = {'antigravity/gemini-4-argon-high'}
        # Retry time: data, the same period as an account latch.
        retry = t0 + timedelta(hours=model_latch.RETRY_HOURS)
        self.assertEqual(model_latch.RETRY_HOURS, model_latch.account_state.LATCH_RETRY_HOURS)
        self.assertEqual(set(model_latch.active(now=retry - timedelta(seconds=1), catalog={})), latched)
        self.assertEqual(model_latch.active(now=retry, catalog={}), {})
        self.assertIn(retry.isoformat(timespec='minutes'), model_latch.reason(bundle, model_latch.active(now=t0, catalog={})))
        # Discovery: only a probe made after the latch that lists the model.
        def catalog(checked, models):
            return {'providers': {'antigravity-resilient': {'ok': True, 'type': 'paseo_provider',
                                                            'checked_at': checked.timestamp(), 'models': models}}}
        listed = [{'id': 'gemini-4-argon-high'}]
        self.assertEqual(set(model_latch.active(now=t0, catalog=catalog(t0 - timedelta(minutes=1), listed))), latched)
        self.assertEqual(set(model_latch.active(now=t0, catalog=catalog(t0 + timedelta(minutes=1), []))), latched)
        self.assertEqual(model_latch.active(now=t0, catalog=catalog(t0 + timedelta(minutes=1), listed)), {})
        # A start: only one launched after the latch, on the same model.
        self.assertFalse(model_latch.clear('antigravity', 'gemini-4-argon-high', launched_at=(t0 - timedelta(seconds=1)).isoformat()))
        self.assertFalse(model_latch.clear('antigravity', 'gemini-4-argon-low', launched_at=(t0 + timedelta(seconds=1)).isoformat()))
        self.assertTrue(model_latch.clear('antigravity', 'gemini-4-argon-high', launched_at=(t0 + timedelta(seconds=1)).isoformat()))
        # By hand.
        model_latch.record(bundle, 'invalid_model', at=t0)
        self.assertTrue(model_latch.clear('antigravity', 'gemini-4-argon-high'))
        self.assertEqual(model_latch.active(now=t0, catalog={}), {})

    def test_a_started_worker_on_the_latched_model_clears_it(self):
        import model_latch
        row = self.prepare(self.issue())
        registry.claim(row['request'], now=self.now)
        model_latch.record(self.bundle, 'INVALID_MODEL', at=self.now - timedelta(minutes=1))
        self.assertTrue(model_latch.active())
        registry.observe(row['dispatch_id'], self.snapshot(row), now=self.now)
        self.assertEqual(model_latch.active(), {})
        plan = allocator.plan(dict(self.task), self.usage, self.now, accounts=self.accounts)
        sonnet = next(c for c in plan['ranking'] if c['id'] == 'lab_sonnet55_high')
        self.assertFalse(any('next retry at' in e for e in sonnet['excluded']), sonnet['excluded'])


class ProviderRetryTests(unittest.TestCase):
    """2026-11-09: an opencode free-model worker sat `running` on "Provider retry (attempt 1): Free usage exceeded,
    subscribe to Go" while the account looked ready and the router kept choosing opencode; after opencode 2.x the same
    limit fails the turn at once. The bridge records either with the provider's message; the classifier, never code,
    decides what it blocks."""
    setUp_dispatch, snapshot = DispatchTests.setUp, DispatchTests.snapshot
    issue, prepare = DispatchTests.issue, DispatchTests.prepare
    MESSAGE = 'Free usage exceeded, subscribe to Go'
    # opencode 2.2.6 on LS, daemon log `handleStreamEvent: turn_failed`, 2026-11-09.
    V2_MESSAGE = ('Free usage limit reached. Connect a provider or subscribe to OpenCode Go to keep going, '
                  'or switch models.')

    def setUp(self):
        import provider_retry
        self.setUp_dispatch()
        # No good reading on file: a later good reading of the account would lift its hold, which these tests do not exercise.
        (self.root / 'usage.json').write_text(json.dumps({'providers': {}}))
        self.store = self.root / provider_retry.FILE
        env = patch.dict(os.environ, {'ROUTER_PROVIDER_RETRY': str(self.store), 'ROUTER_STATE_DIR': str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.scopes, self.asked = [], []
        self.real_classify = provider_retry.classify_scope

        def classify(account, provider, model, message, failure):
            self.asked.append((account, provider, model, message, failure))
            return (self.scopes.pop(0) if self.scopes else 'unknown'), {'source': 'system_one', 'scorer_type': 'fake'}
        classifier = patch.object(provider_retry, 'classify_scope', side_effect=classify)
        classifier.start()
        self.addCleanup(classifier.stop)
        self.other = dict(account_fleet.bundles(self.accounts)['lab_opus55_high'], bundle_id='lab_opus55_high')
        self.third = dict(account_fleet.bundles(self.accounts)['lab_fable51_high'], bundle_id='lab_fable51_high')

    def stuck(self, at, **changes):
        return {'reason': 'provider retry stuck', 'failure_code': 'provider_retry_stuck', 'provider_message': self.MESSAGE,
                'next': None, 'attempts': 5, 'retrying_since': (at - timedelta(minutes=5)).isoformat(),
                'at': at.isoformat(), 'partial_work': False, 'cancelled': True, **changes}

    def launched(self, task_id=None):
        task = {**self.task, 'task_id': task_id} if task_id else None
        row = self.prepare(self.issue(task=task), task=task)
        registry.claim(row['request'], now=self.now)
        return row

    def excluded(self, bundle_id):
        plan = allocator.plan(dict(self.task), self.usage, self.now, accounts=self.accounts)
        return plan, next(c for c in plan['ranking'] if c['id'] == bundle_id)['excluded']

    def test_a_stuck_terminal_fails_the_dispatch_and_holds_the_model_with_the_provider_message(self):
        import provider_retry
        row = self.launched()
        self.assertEqual(registry.observe(row['dispatch_id'], self.snapshot(row), now=self.now)['state'], 'started')
        # The bridge cancelled the turn, so the host now says idle; the watcher's terminal says why.
        at = self.now + timedelta(seconds=20)
        done = registry.observe(row['dispatch_id'], self.snapshot(row, status='idle', attentionReason='finished'),
                                now=self.now + timedelta(seconds=30), terminal=self.stuck(at))
        self.assertEqual((done['state'], done['failure_code']), ('failed', 'provider_retry_stuck'))
        self.assertIn(flow.RETRY_STUCK_CLEAN, flow.public(done)['next_action'])
        self.assertEqual(self.asked, [('claude-lab', 'claude-lab', 'claude-sonnet-5-5', self.MESSAGE, 'provider_retry_stuck')])
        held = provider_retry.active(now=at)
        self.assertEqual(set(held), {'claude-lab/claude-sonnet-5-5'})   # unknown scope starts at the model
        self.assertEqual(held['claude-lab/claude-sonnet-5-5']['retry_at'], (at + timedelta(seconds=120)).isoformat())
        plan, excluded = self.excluded('lab_sonnet55_high')
        self.assertTrue(any(self.MESSAGE in e for e in excluded), excluded)
        self.assertNotIn(plan['choice'], ('lab_sonnet55_high', 'lab_sonnet55_medium'))
        self.assertTrue(any(w.startswith('claude-lab/claude-sonnet-5-5: provider failure hold') and self.MESSAGE in w
                            and 'agentcompass hold --clear' in w for w in plan['decision']['warnings']), plan['decision'])
        # Observing the same terminal again records nothing more.
        registry.observe(row['dispatch_id'], self.snapshot(row, status='idle'), now=self.now + timedelta(seconds=40),
                         terminal=self.stuck(at))
        self.assertEqual(len(self.asked), 1)

    def test_the_parent_is_told_about_partial_work_and_a_failed_stop(self):
        for detail, expected in (({'partial_work': True, 'cancelled': True}, flow.RETRY_STUCK_PARTIAL),
                                 ({'partial_work': False, 'cancelled': False}, flow.RETRY_STUCK_RUNNING),
                                 ({}, flow.RETRY_STUCK_PARTIAL)):   # an old receipt without the flags may have worked
            with self.subTest(detail=detail):
                row = {'state': 'failed', 'agent_id': 'a', 'failure_code': 'provider_retry_stuck',
                       'failure_detail': detail, 'dispatch_id': 'd', 'bundle': self.bundle}
                self.assertIn('part of the work' if expected != flow.RETRY_STUCK_RUNNING else 'cancel_agent',
                              flow.public(row)['next_action'])
                self.assertTrue(flow.public(row)['next_action'].endswith(expected))
        self.assertNotIn('roll this brief again and the router', flow.RETRY_STUCK_PARTIAL)

    def test_a_turn_that_failed_with_the_provider_message_is_classified_and_held_the_same_way(self):
        import provider_retry
        self.scopes = ['account']
        row = self.launched()
        registry.observe(row['dispatch_id'], self.snapshot(row), now=self.now)
        at = self.now + timedelta(seconds=15)
        terminal = {'reason': 'error', 'status': 'error', 'attentionReason': 'error', 'at': at.isoformat(),
                    'failure_code': 'provider_turn_failed', 'provider_message': self.V2_MESSAGE}
        done = registry.observe(row['dispatch_id'], self.snapshot(row, status='error'),
                                now=self.now + timedelta(seconds=20), terminal=terminal)
        self.assertEqual((done['state'], done['failure_code']), ('failed', 'provider_turn_failed'))
        self.assertEqual(flow.public(done)['next_action'], flow.TURN_FAILED_NEXT)
        self.assertEqual(self.asked[0][3:], (self.V2_MESSAGE, 'provider_turn_failed'))
        held = provider_retry.active(now=self.now + timedelta(days=30))
        self.assertEqual(list(held), ['claude-lab'])
        self.assertEqual((held['claude-lab']['failure_code'], held['claude-lab']['message']),
                         ('provider_turn_failed', self.V2_MESSAGE))
        _, excluded = self.excluded('lab_opus55_high')
        self.assertTrue(any('provider turn failed' in e and self.V2_MESSAGE in e for e in excluded), excluded)

    def test_an_error_the_classifier_reads_as_not_the_provider_holds_nothing(self):
        import provider_retry
        self.scopes = ['none']
        terminal = {'failure_code': 'provider_turn_failed', 'provider_message': 'ENOENT: no such file tests/x.py',
                    'at': self.now.isoformat()}
        self.assertIsNone(provider_retry.record(self.bundle, terminal, dispatch_id='d-none'))
        self.assertEqual(provider_retry.active(now=self.now), {})
        self.assertIsNone(provider_retry.record(self.bundle, terminal, dispatch_id='d-none'))   # read once
        self.assertEqual(len(self.asked), 1)

    def test_an_account_scope_holds_every_model_until_cleared_or_a_named_model_there_completes(self):
        import provider_retry
        self.scopes = ['account']
        at = self.now - timedelta(minutes=1)
        entry = provider_retry.record(self.bundle, self.stuck(at), dispatch_id='d-stuck')
        self.assertEqual((entry['model'], entry['scope_source'], entry['retry_at']), (None, 'classifier', None))
        # A quota that blocks the whole account does not lift by waiting.
        later = self.now + timedelta(days=30)
        self.assertEqual(set(provider_retry.active(now=later)), {'claude-lab'})
        self.assertIn(self.MESSAGE, provider_retry.reason(self.other, provider_retry.active(now=later)))
        _, excluded = self.excluded('lab_opus55_high')
        self.assertTrue(any('on this account' in e for e in excluded), excluded)
        with self.assertRaisesRegex(ValueError, 'provider failure hold'):
            self.launched('routed-task')   # routing's own pick is refused
        self.assertFalse(provider_retry.clear('claude-lab', 'claude-sonnet-5-5', include_account=True,
                                              launched_at=(at - timedelta(seconds=1)).isoformat()))   # launched before
        # A model the user names is the probe: prepare lets it through. Review 2 R1: Paseo shows a probe the parent
        # cancelled as idle/finished too, so only a turn the watcher saw end on the model's reply lifts the hold.
        for task_id, terminal, lifted in (('probe-unseen', None, False),            # no watcher terminal at all
                                          ('probe-cancelled', {'replied': False}, False),
                                          ('probe-replied', {'replied': True}, True)):
            with self.subTest(task_id=task_id):
                task = {**self.task, 'task_id': task_id}
                row = self.prepare(self.issue(task=task, constraint='user_override'), task=task)
                registry.claim(row['request'], now=self.now)
                registry.observe(row['dispatch_id'], self.snapshot(row), now=self.now)
                self.assertTrue(provider_retry.active(now=self.now))   # running is no success: a stuck worker runs too
                seen = self.now + timedelta(seconds=30)
                done = registry.observe(row['dispatch_id'], self.snapshot(row, status='idle', attentionReason='finished'),
                                        now=seen, terminal=terminal and {
                                            'reason': 'finished', 'status': 'idle', 'attentionReason': 'finished',
                                            'at': seen.isoformat(), **terminal})
                self.assertEqual(done['state'], 'completed')
                self.assertEqual(provider_retry.active(now=self.now) == {}, lifted)

    def test_a_failed_turn_read_as_the_worker_or_the_task_input_is_the_worker_error_and_holds_nothing(self):
        # Review 2 R3/R5: not the provider's doing, so neither a hold nor provider wording.
        import provider_retry
        message = 'prompt is too long: 251234 tokens > 200000 maximum'
        for scope, code, expected in (('none', 'worker_turn_failed', flow.WORKER_TURN_FAILED_NEXT),
                                      ('task_input', 'task_input_rejected', flow.TASK_INPUT_NEXT)):
            with self.subTest(scope=scope):
                self.scopes = [scope]
                row = self.launched(f'task-{scope}')
                registry.observe(row['dispatch_id'], self.snapshot(row), now=self.now)
                at = self.now + timedelta(seconds=15)
                terminal = {'reason': 'error', 'status': 'error', 'attentionReason': 'error', 'at': at.isoformat(),
                            'failure_code': 'provider_turn_failed', 'provider_message': message}
                done = registry.observe(row['dispatch_id'], self.snapshot(row, status='error'),
                                        now=self.now + timedelta(seconds=20), terminal=terminal)
                self.assertEqual((done['state'], done['failure_code']), ('failed', code))
                self.assertEqual(provider_retry.dispatch_scope(row['dispatch_id']), scope)
                next_action = flow.public(done)['next_action']
                self.assertEqual(next_action, expected)
                self.assertNotIn('Its provider ended the turn', next_action)
                self.assertNotIn('Provider error', next_action)
                self.assertEqual(provider_retry.active(now=at), {})
        self.assertIn('task_input', provider_retry.SCOPE_SCHEMA['properties']['scope']['enum'])
        self.assertIn('`task_input`', provider_retry.SCOPE_INSTRUCTIONS)

    def test_an_account_answer_on_antigravity_holds_only_the_quota_pool_of_the_failed_model(self):
        # 2026-11-09: Antigravity's Gemini and third-party models draw different windows; the record reads the installed
        # accounts itself to find the pool, and routing keeps the other pool.
        import provider_retry
        self.scopes = ['account']
        third = dict(account_fleet.bundles(self.accounts)['agy_opus55_high'], bundle_id='agy_opus55_high')
        entry = provider_retry.record(third, self.stuck(self.now), dispatch_id='d-agy')
        self.assertEqual(set(provider_retry.active(now=self.now)), {'antigravity#3p-5h+3p-weekly'})
        self.assertEqual(entry['pool']['windows'], {'3p-5h': [{'models_except': ['gemini']}],
                                                    '3p-weekly': [{'models_except': ['gemini']}]})
        _, excluded = self.excluded('agy_sonnet55_high')
        self.assertTrue(any('quota pool 3p-5h+3p-weekly' in e and self.MESSAGE in e for e in excluded), excluded)
        _, excluded = self.excluded('flash_high')
        self.assertFalse(any(e.startswith(provider_retry.EXCLUSION_PREFIX) for e in excluded), excluded)
        plan = allocator.plan(dict(self.task), self.usage, self.now, accounts=self.accounts)
        self.assertTrue(any(w.startswith('antigravity#3p-5h+3p-weekly: provider failure hold on this quota pool')
                            for w in plan['decision']['warnings']), plan['decision']['warnings'])

    def test_clearing_one_model_keeps_the_account_hold(self):
        # Review 2 R7: `hold --clear ACCOUNT/MODEL` is that model's hold only.
        import provider_retry
        self.scopes = ['account', 'model']
        provider_retry.record(self.other, self.stuck(self.now), dispatch_id='d-account')
        provider_retry.record(self.bundle, self.stuck(self.now), dispatch_id='d-model')
        self.assertEqual(set(provider_retry.active(now=self.now)), {'claude-lab', 'claude-lab/claude-sonnet-5-5'})
        self.assertTrue(provider_retry.clear_key('claude-lab/claude-sonnet-5-5'))
        self.assertEqual(set(provider_retry.active(now=self.now)), {'claude-lab'})
        self.assertFalse(provider_retry.clear_key('claude-lab/claude-opus-5-5'))   # the account hold is not that model's
        self.assertEqual(set(provider_retry.active(now=self.now)), {'claude-lab'})
        self.assertTrue(provider_retry.clear_key('claude-lab'))
        self.assertEqual(provider_retry.active(now=self.now), {})

    def test_the_clear_command_lifts_a_hold(self):
        import provider_retry
        self.scopes = ['account']
        provider_retry.record(self.bundle, self.stuck(self.now), dispatch_id='d-clear')
        self.assertFalse(provider_retry.clear_key('codex'))
        self.assertEqual(provider_retry.main(['--clear', 'claude-lab']), 0)
        self.assertEqual(provider_retry.active(now=self.now), {})

    def test_unknown_scope_widens_to_the_account_when_a_second_model_fails_the_same_way(self):
        import provider_retry
        at = self.now
        provider_retry.record(self.bundle, self.stuck(at), dispatch_id='d1')
        self.assertEqual(set(provider_retry.active(now=at)), {'claude-lab/claude-sonnet-5-5'})
        widened = provider_retry.record(self.other, self.stuck(at + timedelta(minutes=1)), dispatch_id='d2')
        self.assertEqual((widened['model'], widened['scope_source']), (None, 'widened'))
        self.assertEqual(widened['models'], ['claude-opus-5-5', 'claude-sonnet-5-5'])
        # Review 2 R2: two guesses are still guesses: a widened account hold backs off like a model hold.
        self.assertEqual(widened['retry_at'], (at + timedelta(minutes=1, seconds=120)).isoformat())
        self.assertFalse(provider_retry.indefinite(widened))
        self.assertIn('next retry at', provider_retry.retry_text(widened))
        self.assertIn('claude-lab', provider_retry.active(now=at + timedelta(minutes=2)))
        self.assertNotIn('claude-lab', provider_retry.active(now=at + timedelta(days=1)))

    def test_a_transient_or_model_hold_never_widens_a_later_unknown_one(self):
        # Review F1: offline, opencode retries `fetch failed`; the classifier reads A as transient, then cannot be reached
        # for B (unknown). That must not become a permanent account hold.
        import provider_retry
        for first in ('transient', 'model'):
            with self.subTest(first=first):
                self.store.unlink(missing_ok=True)
                self.scopes = [first, 'unknown']
                provider_retry.record(self.bundle, self.stuck(self.now), dispatch_id=f'a-{first}')
                entry = provider_retry.record(self.other, self.stuck(self.now + timedelta(minutes=1)),
                                              dispatch_id=f'b-{first}')
                self.assertEqual((entry['model'], entry['scope_source']), ('claude-opus-5-5', 'unknown_default'))
                self.assertNotIn('claude-lab', provider_retry.active(now=self.now + timedelta(days=1)))

    def test_a_scope_the_classifier_names_is_not_widened(self):
        import provider_retry
        for second in ('model', 'transient'):
            with self.subTest(second=second):
                self.store.unlink(missing_ok=True)
                self.scopes = ['unknown', second]
                provider_retry.record(self.bundle, self.stuck(self.now), dispatch_id=f'a-{second}')
                entry = provider_retry.record(self.other, self.stuck(self.now), dispatch_id=f'b-{second}')
                self.assertEqual((entry['model'], entry['scope']), ('claude-opus-5-5', second))
                self.assertNotIn('claude-lab', provider_retry.active(now=self.now))

    def test_a_model_hold_backs_off_like_a_failing_usage_reading_and_the_provider_next_wins(self):
        import provider_retry
        import usage_core
        at, seen = self.now, []
        self.scopes = ['model'] * 5
        for index in range(5):
            entry = provider_retry.record(self.bundle, self.stuck(at), dispatch_id=f'd{index}')
            seconds = (provider_retry._time(entry['retry_at']) - at).total_seconds()
            seen.append(seconds)
            self.assertEqual(seconds, usage_core.failure_retry_seconds(index + 1, usage_core.IDLE_REFRESH_SECONDS))
            at = provider_retry._time(entry['retry_at'])
        self.assertEqual(seen, [120, 240, 480, 960, 1800])   # 2 min doubling, capped at the idle poll
        self.assertIsNone(provider_retry.record(self.bundle, self.stuck(at), dispatch_id='d0'))  # once per dispatch
        self.assertEqual(provider_retry.active(now=at + timedelta(seconds=1)), {})   # then the next roll re-probes it
        self.store.unlink()
        nxt = self.now + timedelta(hours=1)
        entry = provider_retry.record(self.bundle, self.stuck(self.now, next=nxt.isoformat()), dispatch_id='n1')
        self.assertEqual(entry['retry_at'], nxt.isoformat())

    def test_a_watcher_receipt_is_held_before_the_registry_observes_it(self):
        import provider_retry
        row = self.launched()
        folder = self.root / 'paseo-notifications'
        folder.mkdir()
        (folder / f"{row['dispatch_id']}.json").write_text(json.dumps({
            'dispatch_id': row['dispatch_id'], 'parent_id': 'parent-a', 'status': 'delivered',
            'terminal': self.stuck(self.now)}), encoding='utf-8')
        (folder / 'other.json').write_text(json.dumps({'dispatch_id': 'other', 'terminal': {'reason': 'finished'}}))
        written = provider_retry.ingest()
        self.assertEqual([(e['source'], e['message']) for e in written], [(row['dispatch_id'], self.MESSAGE)])
        self.assertEqual(provider_retry.ingest(), [])
        self.assertEqual(registry.inspect(row['dispatch_id'])['state'], 'launching')   # the registry decides its state

    def test_the_scope_comes_from_the_configured_classifier_and_a_silent_one_reads_as_unknown(self):
        import route_judge
        asked = []   # the real classify_scope, with the classifier backend faked

        def run(scorer_type, instructions, text, schema, timeout, config, account=None):
            asked.append(text)
            return {'scope': 'account', 'reason': 'a free-usage quota shared by the free models'}, 'claude', 'haiku'
        with patch.object(route_judge, 'scorer_accounts', return_value=['claude']), \
                patch.object(route_judge, 'load_scorer_config', return_value={'scorer_type': 'claude_haiku_low'}), \
                patch.object(route_judge, 'run_structured', side_effect=run):
            scope, meta = self.real_classify('opencode', 'opencode', 'glm-5.3-flash-free', self.V2_MESSAGE,
                                             'provider_turn_failed')
        self.assertEqual((scope, meta['source']), ('account', 'system_one'))
        self.assertIn(self.V2_MESSAGE, asked[0])
        self.assertIn('provider_turn_failed', asked[0])
        with patch.object(route_judge, 'scorer_accounts', return_value=['claude']), \
                patch.object(route_judge, 'load_scorer_config', return_value={'scorer_type': 'claude_haiku_low'}), \
                patch.object(route_judge, 'run_structured', return_value=({'scope': 'quota'}, 'claude', 'haiku')):
            self.assertEqual(self.real_classify('opencode', 'opencode', 'm', self.MESSAGE)[0], 'unknown')
        self.assertEqual(self.real_classify('opencode', 'opencode', 'm', None)[0], 'unknown')


class LiveModelCheckTests(unittest.TestCase):
    """2026-11-10: `roll` picked `argon_high` (`antigravity-resilient/gemini-4-argon-high`) when Antigravity offered only
    six starter models; Paseo rejected it at launch with `INVALID_MODEL`. Candidate providers' live model lists are read
    before ranking, cached briefly, and matched structurally by exact `id` and Paseo `aliases`."""
    setUp_dispatch, snapshot = DispatchTests.setUp, DispatchTests.snapshot
    issue, prepare = DispatchTests.issue, DispatchTests.prepare
    brief = RollTests.brief

    def setUp(self):
        import live_models
        import provider_retry
        self.setUp_dispatch()
        self.accounts['accounts']['antigravity']['plan'] = 'pro'     # a plan with a cited community capacity
        for bid, bundle in self.accounts['accounts']['antigravity']['bundles'].items():
            bundle['disabled'] = bundle.get('disabled', False) or bid.startswith('agy_opus')    # Argon is the divergence pick under test
        self.live_store = self.root / live_models.FILE
        self.retry_store = self.root / provider_retry.FILE
        self.latch_store = self.root / 'model-unavailable.json'
        env = patch.dict(os.environ, {
            'AGENTCOMPASS_LIVE_MODELS': str(self.live_store),
            'ROUTER_PROVIDER_RETRY': str(self.retry_store),
            'ROUTER_MODEL_LATCH': str(self.latch_store),
            'ROUTER_STATE_DIR': str(self.root),
        })
        env.start()
        self.addCleanup(env.stop)
        self.creates, self.choices, self.fetched_providers = [], [], []
        self.usage['accounts']['antigravity'] = {
            'status': 'fresh',
            'fetched_at': self.now.isoformat(),
            'windows': [
                {
                    'id': wid,
                    'used_pct': 100 - rem,
                    'remaining_pct_before_reset': rem,
                    'resets_at': (self.now + timedelta(hours=hours)).isoformat(),
                    'seconds_until_reset': hours * 3600,
                }
                for wid, rem, hours in [
                    ('gemini-5h', 90, 4.0),
                    ('gemini-weekly', 95, 120.0),
                    ('3p-5h', 90, 4.0),
                    ('3p-weekly', 95, 120.0),
                ]
            ],
        }
        # Task where `argon_high` wins when offered, and the next candidate wins in the same roll when excluded.
        self.creative_task = {
            'id': 'creative-task',
            'task_id': 'creative-task',
            'work_kind': 'exploration_analysis',
            'difficulty': 'hard',
            'expected_hours': 0.5,
            'needs': {'local_only': True},
            'profile': profile(work_kind='exploration_analysis', interaction_style='exploration', code_complexity=0,
                               access='read-only', local_only=True, expected_hours=0.5,
                               axis_demand={'divergence': 3, 'coding': 0, 'aesthetics': 0, 'tool_operation': 0}),
        }
        self.live_providers = {
            'antigravity-resilient': {
                'ok': True,
                'status': 'ready',
                'models': [
                    {'id': 'gemini-3.8-flash-high'},
                    {'id': 'gemini-3.8-flash-medium'},
                    {'id': 'gemini-3.8-flash-low'},
                    {'id': 'claude-sonnet-4-6'},
                    {'id': 'claude-opus-4-6-thinking'},
                    {'id': 'gpt-oss-120b-medium'},
                ],
            },
            'claude-lab': {
                'ok': True,
                'status': 'ready',
                'models': [
                    {'id': 'claude-opus-5-5', 'aliases': ['claude-opus-5-5[1m]']},
                    {'id': 'claude-sonnet-5-5', 'aliases': ['claude-sonnet-5-5[1m]']},
                    {'id': 'claude-fable-5-1'},
                ],
            },
            'codex': {
                'ok': True,
                'status': 'ready',
                'models': [
                    {'id': 'gpt-6.1-sol'},
                    {'id': 'gpt-6-astra'},
                ],
            },
            'pi': {
                'ok': True,
                'status': 'ready',
                'models': [
                    {'id': 'openai-codex/gpt-6.1-sol'},
                    {'id': 'openai-codex/gpt-6-astra'},
                ],
            },
        }

    def fetch_live(self, providers, cwd=None, timeout=10):
        self.fetched_providers.append(list(providers))
        return {
            'ok': True,
            'fetched_at': self.now.isoformat(),
            'providers': {p: self.live_providers[p] for p in providers if p in self.live_providers},
        }

    def host(self, request):
        if request['operation'] == 'create':
            self.creates.append(request['request']['provider'])
            return {
                'ok': True,
                'sent': True,
                'agent': started(request),
                'completion_notification': {'status': 'watching', 'pid': 123},
            }
        return {'ok': True, 'agent': None, 'creation': None}

    def roll(self, host=None, task=None):
        import argparse
        import roll
        active_task = dict(task or self.creative_task)
        brief = self.root / 'task.md'
        brief.write_bytes(self.brief.encode('utf-8'))
        args = argparse.Namespace(
            brief=brief, file=[], apply_to=None, local_only=None, hours=0.5, title=None,
            workspace=None, task_id=f'live-task-{len(self.choices)}', wait=0, parent_id=None, parent_host=None,
            parent_address=None, provider=None, model=None, effort=None, basis=None, usage_cache=None, dry_run=False,
            verbose=False, task=None, new_worker=True, resume=None, fast=False, keep_whole=None, access=None,
            allow_concurrent=None, account=None,
        )

        def plan_stage(document, _):
            planned = allocator.plan(dict(active_task), self.usage, self.now, accounts=self.accounts)
            self.choices.append(planned['choice'])
            return dict(planned, **registry.issue(planned, document['task'], self.now)), 0

        judge = type('Judge', (), {
            'plan_stage': staticmethod(plan_stage),
            'usage_snapshot': staticmethod(lambda _: self.usage),
            'subscription_env': staticmethod(lambda account=None: dict(os.environ)),
            'cli_prefix': staticmethod(lambda name: [name]),
        })
        with patch.dict(os.environ, {'PASEO_AGENT_ID': 'parent-a', 'ROUTER_PARENT_AGENT_ID': ''}), \
                patch.object(roll, 'WORKER_PROMPTS_DIR', self.root / 'worker-prompts'), \
                patch.object(roll, 'RESULTS_DIR', self.root / 'roll-results'), \
                patch.object(flow, 'paseo_reachable', return_value=True), \
                patch.object(flow, 'host_call', host or self.host), \
                patch.object(account_fleet, 'load_accounts', return_value=self.accounts):
            return roll.run(args, judge)

    def test_a_bundle_missing_from_the_live_list_is_excluded_with_reason_and_the_next_one_launches_in_the_same_roll(self):
        import live_models
        # Without live filtering (when gemini-4-argon-high IS offered), argon_high wins a divergence-heavy task.
        full_providers = copy.deepcopy(self.live_providers)
        full_providers['antigravity-resilient']['models'].append({'id': 'gemini-4-argon-high'})
        with patch.object(live_models, 'fetch_from_paseo', side_effect=lambda provs, **kw: {
            'ok': True, 'fetched_at': self.now.isoformat(), 'providers': {p: full_providers[p] for p in provs}
        }):
            baseline = allocator.plan(dict(self.creative_task), self.usage, self.now, accounts=self.accounts)
        winner = baseline['choice']     # whichever bundle wins with the full list; the incident list must move off it
        self.assertNotIn(winner, (None, 'none'))
        live_models.invalidate()

        # Incident reproduction: antigravity-resilient offers only the 6 starter models (no gemini-4-argon-high).
        with patch.object(live_models, 'fetch_from_paseo', side_effect=self.fetch_live):
            result, code = self.roll()
            self.assertEqual((result['status'], code), ('launched', 0))
            self.assertNotEqual(self.choices[0], winner)
            self.assertEqual(len(self.creates), 1)
            self.assertNotEqual(self.creates[0], 'antigravity-resilient/gemini-4-argon-high')
            self.assertTrue(
                any('provider no longer offers gemini-4-argon-high' in w for w in result.get('warnings') or ()),
                result,
            )
            # Both plan and prepare inside the roll shared the 60s cache, so Paseo was queried only once.
            self.assertEqual(len(self.fetched_providers), 1)
            plan = allocator.plan(dict(self.creative_task), self.usage, self.now, accounts=self.accounts)
            self.assertEqual(len(self.fetched_providers), 1)
            argon_row = next(c for c in plan['ranking'] if c['id'] == 'argon_high')
            self.assertIn('provider no longer offers gemini-4-argon-high', argon_row['excluded'])
            flash_row = next(c for c in plan['ranking'] if c['id'] == 'flash_high')
            self.assertNotIn('provider no longer offers gemini-3.8-flash-high', flash_row['excluded'])
            # Past the 60s TTL, the cache refreshes.
            allocator.plan(dict(self.creative_task), self.usage,
                           self.now + timedelta(seconds=live_models.TTL_SECONDS + 1), accounts=self.accounts)
            self.assertEqual(len(self.fetched_providers), 2)

    def test_an_unreadable_live_list_does_not_exclude_and_surfaces_model_list_unavailable_plus_invalid_model_latch(self):
        import live_models
        import model_latch
        import provider_retry
        # 1. Paseo down / unreachable when reading models: do not exclude argon_high; surface "model list unavailable".
        with patch.object(live_models, 'fetch_from_paseo', return_value={'ok': False, 'error': 'host_connection_failed'}):
            plan = allocator.plan(dict(self.creative_task), self.usage, self.now, accounts=self.accounts)
            self.assertEqual(plan['choice'], 'argon_high')
            argon_row = next(c for c in plan['ranking'] if c['id'] == 'argon_high')
            self.assertEqual(argon_row['excluded'], [])
            self.assertTrue(
                any('model list unavailable' in w for w in plan['decision'].get('warnings') or ()),
                plan['decision'],
            )
        live_models.invalidate()

        # 2. Provider disabled in Paseo: do not exclude on live-model basis; surface "model list unavailable".
        disabled_providers = copy.deepcopy(self.live_providers)
        disabled_providers['antigravity-resilient'] = {
            'ok': False, 'status': 'disabled', 'error': 'provider_disabled', 'models': []
        }
        with patch.object(live_models, 'fetch_from_paseo', side_effect=lambda provs, **kw: {
            'ok': True, 'fetched_at': self.now.isoformat(), 'providers': {p: disabled_providers[p] for p in provs}
        }):
            plan2 = allocator.plan(dict(self.creative_task), self.usage, self.now, accounts=self.accounts)
            self.assertEqual(plan2['choice'], 'argon_high')
            self.assertTrue(
                any('antigravity (antigravity-resilient): model list unavailable (provider_disabled)' in w
                    for w in plan2['decision'].get('warnings') or ()),
                plan2['decision'],
            )
        live_models.invalidate()

        # 3. Fallback when the live list was unreadable and launch fails with INVALID_MODEL: one mechanism, the
        # existing model latch, holds only that model (no provider_retry hold) and gives one warning on the next roll.
        def reject_argon(request):
            if request['operation'] == 'create' and request['request']['provider'] == 'antigravity-resilient/gemini-4-argon-high':
                self.creates.append(request['request']['provider'])
                return {'ok': False, 'sent': False, 'error': 'host_creation_failed',
                        'failure_class': 'model_unavailable', 'failure_code': 'INVALID_MODEL'}
            return self.host(request)

        with patch.object(live_models, 'fetch_from_paseo', return_value={'ok': False, 'error': 'host_connection_failed'}):
            first, code1 = self.roll(host=reject_argon)
            # The same roll goes on to the next candidate (2026-11-10).
            self.assertEqual((first['status'], code1), ('launched', 0))
            self.assertEqual(self.creates[0], 'antigravity-resilient/gemini-4-argon-high')
            self.assertEqual(len(self.creates), 2)
            self.assertNotEqual(self.choices[1], 'argon_high')
            self.assertIn('INVALID_MODEL', first['attempts'][0])
            self.assertEqual(set(model_latch.active(now=self.now)), {'antigravity/gemini-4-argon-high'})
            self.assertEqual(provider_retry.active(now=self.now), {})
            # The refusal invalidated the cached list, so the relaunch's plan read it again.
            reread = json.loads(self.live_store.read_text(encoding='utf-8'))['providers']['antigravity-resilient']
            self.assertEqual(reread['error'], 'host_connection_failed')
        # The refused model is now also missing from the live list: still one warning line for it.
        with patch.object(live_models, 'fetch_from_paseo', side_effect=self.fetch_live):
            second, code2 = self.roll(host=reject_argon)
            self.assertEqual((second['status'], code2), ('launched', 0))
            self.assertNotEqual(self.choices[2], 'argon_high')
            plan = allocator.plan(dict(self.creative_task), self.usage, self.now, accounts=self.accounts)
            lines = [w for w in plan['decision'].get('warnings') or () if 'gemini-4-argon-high' in w]
            self.assertEqual(len(lines), 1, lines)

    def test_a_provider_paseo_does_not_know_gives_no_warning_and_no_exclusion(self):
        """llmproxy (LiteLLM, harness codex) is not a Paseo provider: the bridge answers `unknown_provider`."""
        import live_models
        providers = copy.deepcopy(self.live_providers)
        providers['antigravity-resilient'] = {'ok': False, 'status': 'unknown_provider', 'error': 'unknown_provider',
                                              'models': []}
        with patch.object(live_models, 'fetch_from_paseo', side_effect=lambda provs, **kw: {
                'ok': True, 'providers': {p: providers[p] for p in provs if p in providers}}):
            plan = allocator.plan(dict(self.creative_task), self.usage, self.now, accounts=self.accounts)
        self.assertEqual(plan['choice'], 'argon_high')
        self.assertFalse([w for w in plan['decision'].get('warnings') or () if 'model list' in w or 'no longer' in w])
        self.assertIsNone(live_models.unavailable_detail(providers['antigravity-resilient']))

    def test_cache_writes_merge_under_the_lock_and_respect_a_later_invalidation(self):
        """Review 2026-11-10: a refresh wrote its whole read-time snapshot back, losing a concurrent refresh of another
        provider and an invalidation made during its fetch."""
        import live_models
        entry = lambda mid: {'ok': True, 'models': [{'id': mid}]}
        live_models.load_or_refresh(['x'], fetch=lambda provs: {'ok': True, 'providers': {'x': entry('x1')}})

        def slow_a(provs):
            # While A fetches: B refreshes another provider, and x is invalidated.
            live_models.load_or_refresh(['b'], fetch=lambda p: {'ok': True, 'providers': {'b': entry('b1')}})
            live_models.invalidate('x')
            return {'ok': True, 'providers': {'a': entry('a1'), 'x': entry('x2')}}

        answer = live_models.load_or_refresh(['a', 'x'], force=True, fetch=slow_a)
        self.assertEqual(set(answer['providers']), {'a', 'x'})  # this call still answers with what it read
        stored = json.loads(self.live_store.read_text(encoding='utf-8'))['providers']
        self.assertEqual(set(stored), {'a', 'b'})  # b survives; x stays evicted until the next refresh
        live_models.load_or_refresh(['x'], fetch=lambda provs: {'ok': True, 'providers': {'x': entry('x3')}})
        stored = json.loads(self.live_store.read_text(encoding='utf-8'))['providers']
        self.assertEqual(stored['x']['models'], [{'id': 'x3'}])

    def test_an_invalidation_before_any_cache_file_still_beats_an_in_flight_refresh(self):
        """Review round 2: with no cache file yet, invalidate() returned early, so a refresh already fetching wrote the
        evicted list back."""
        import live_models
        entry = lambda mid: {'ok': True, 'models': [{'id': mid}]}
        self.assertFalse(self.live_store.exists())

        def slow(provs):
            live_models.invalidate()  # INVALID_MODEL arrives while this fetch is in flight
            return {'ok': True, 'providers': {'x': entry('x-old')}}

        live_models.load_or_refresh(['x'], force=True, fetch=slow)
        stored = json.loads(self.live_store.read_text(encoding='utf-8'))['providers']
        self.assertNotIn('x', stored)

    def test_a_bundle_the_user_disabled_gives_no_live_list_warning(self):
        """Review round 2: an unchecked bundle is `disabled: True` at runtime; a provider dropping its model must not
        warn on every plan."""
        import live_models
        accounts = copy.deepcopy(self.accounts)
        live_doc = {'providers': copy.deepcopy(self.live_providers)}
        self.assertTrue(any('gemini-4-argon-high' in w for w in live_models.warnings(live_doc, accounts)))
        accounts['accounts']['antigravity']['bundles']['argon_high']['disabled'] = True
        self.assertFalse(any('gemini-4-argon-high' in w for w in live_models.warnings(live_doc, accounts)))

    def test_structural_model_id_and_paseo_alias_matching(self):
        import live_models
        claude_entry = {
            'ok': True,
            'status': 'ready',
            'models': [
                {'id': 'claude-fable-5', 'aliases': ['claude-fable-5[1m]'], 'label': 'Fable 5'},
                {'id': 'claude-opus-5-5', 'aliases': ['claude-opus-5-5[1m]'], 'label': 'Opus 5.5'},
            ],
        }
        # Exact id match.
        self.assertTrue(live_models.offers_model('claude', 'claude-fable-5', claude_entry))
        self.assertTrue(live_models.offers_model('claude', 'claude/claude-fable-5', claude_entry))
        # Paseo alias match.
        self.assertTrue(live_models.offers_model('claude', 'claude-fable-5[1m]', claude_entry))
        self.assertTrue(live_models.offers_model('claude', 'claude/claude-fable-5[1m]', claude_entry))
        # Prefix normalization on provider-scoped model ids (e.g. opencode/deepseek-v4.1-flash-free).
        opencode_entry = {'ok': True, 'status': 'ready', 'models': [{'id': 'opencode/deepseek-v4.1-flash-free'}]}
        self.assertTrue(live_models.offers_model('opencode', 'opencode/deepseek-v4.1-flash-free', opencode_entry))
        self.assertTrue(live_models.offers_model('opencode', 'deepseek-v4.1-flash-free', opencode_entry))
        # No substring, prefix, or family-keyword matching: gemini-4-argon-high is not matched by gemini-4-argon-low.
        agy_entry = {'ok': True, 'status': 'ready', 'models': [{'id': 'gemini-4-argon-low'}, {'id': 'gemini-4-argon'}]}
        self.assertFalse(live_models.offers_model('antigravity-resilient', 'gemini-4-argon-high', agy_entry))
        self.assertFalse(live_models.offers_model('claude', 'claude-fable-5-1', claude_entry))



if __name__ == '__main__':
    unittest.main()
