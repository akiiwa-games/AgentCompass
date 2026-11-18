"""Host exclusions apply at dispatch boundaries; fixtures never read real host state."""
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import allocator
import account_fleet
import host_policy
import route_judge as router
import decision_ledger
import dispatch_registry
import subscription_cli
import subscription_judge
import usage_reading

from router_test_support import isolated_router_state, profile, temporary_root


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup, policy=False)

@contextmanager
def override_file(path, accounts):
    path.write_text(json.dumps({'schema_version': 2, 'accounts': accounts}), encoding='utf-8')
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


@contextmanager
def cooling(launch_failures, now):
    bundle = {'account': 'claude', 'provider': 'claude', 'model': 'claude-opus-5-5'}
    with patch.object(launch_failures, 'enabled', return_value=True):
        launch_failures.record(bundle, 'model_unavailable', now=now)
        try:
            yield
        finally:
            launch_failures.clear('claude')


LS = {'allowed_accounts': ['codex', 'antigravity', 'dot', 'chatgpt-web'],
      'judges': ['codex'], 'usage_providers': ['codex', 'antigravity']}


class HostPolicyTests(unittest.TestCase):
    def setUp(self):
        self.directory = temporary_root(self.addCleanup)
        registry_env = patch.dict(router.os.environ, {'ROUTER_DISPATCH_DB': str(self.directory / 'dispatch.sqlite3')})
        registry_env.start()
        self.addCleanup(registry_env.stop)
        self.path = self.directory/'host-policy.json'
        self.write(LS)
        patcher = patch.object(host_policy, 'policy_path', return_value=(self.path, False))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.accounts = account_fleet.load_accounts(account_fleet.reference_fleet_path())
        self.now = datetime(2026, 10, 11, tzinfo=timezone.utc)

    def write(self, data):
        self.path.write_text(json.dumps(data), encoding='utf-8')

    def plan(self, task=None, **kwargs):
        return allocator.plan(task or {'work_kind': 'code_implementation', 'difficulty': 'hard'},
                              {'accounts': {}}, now=self.now, accounts=self.accounts, **kwargs)

    def candidate(self, account):
        return {'provider': account, 'account': account, 'model': 'verified', 'effort': 'high',
                'quality_fit': 'review', 'available': True}

    def request(self, stage='select', accounts=('claude', 'codex', 'codex-work', 'grok', 'api-proxy', 'dot', 'chatgpt-web', 'antigravity')):
        with patch.object(usage_reading, 'usage_snapshot', return_value={'accounts': {}}):
            return subscription_judge.build_request(stage, {'task': {'objective': 'review'},
                                               'candidates': {a: self.candidate(a) for a in accounts}})

    def test_missing_default_preserves_defaults(self):
        self.path.unlink()
        self.assertIsNone(host_policy.load_policy())
        self.assertTrue(host_policy.account_allowed(None, 'claude'))
        self.assertEqual(subscription_judge.judge_order({}, 'auto'), ['codex', 'sonnet'])

    def test_missing_explicit_override_is_error(self):
        self.path.unlink()
        with patch.object(host_policy, 'policy_path', return_value=(self.path, True)):
            with self.assertRaisesRegex(host_policy.HostPolicyError, 'missing'):
                self.plan()

    def test_environment_override(self):
        # Exercise resolution, independently of this suite's isolated loader path.
        with patch.dict(host_policy.os.environ, {'ROUTER_HOST_POLICY': str(self.path)}):
            self.assertEqual(self.original_policy_path(), (self.path, True))

    original_policy_path = staticmethod(host_policy.policy_path)

    def test_invalid_profiles_fail_closed(self):
        for value in ('{', '[]', '{}', '{"allowed_accounts":"codex"}',
                      '{"allowed_accounts":[1]}', '{"allowed_accounts":[],"judge":[]}',
                      '{"allowed_accounts":[],"judges":["claude"]}',
                      '{"allowed_accounts":[],"usage_providers":null}'):
            with self.subTest(value=value):
                self.path.write_text(value, encoding='utf-8')
                with self.assertRaises(host_policy.HostPolicyError):
                    self.plan()
                with self.assertRaises(host_policy.HostPolicyError):
                    self.request('delegate')
        self.path.write_bytes(b'\xff')
        with self.assertRaisesRegex(host_policy.HostPolicyError, 'UTF-8'):
            host_policy.load_policy()

    def test_whitelist_applies_to_all_plan_calls_and_cannot_be_overridden(self):
        for unavailable in ([], ['sol_xhigh']):
            result = self.plan({'work_kind': 'code_implementation', 'difficulty': 'hard',
                                'allowed_accounts': ['claude'], 'host_policy': None}, unavailable=unavailable)
            for row in result['ranking']:
                if row['bundle']['account'] not in LS['allowed_accounts']:
                    self.assertIn('excluded by host policy', row['excluded'])
            self.assertIn(result['selected_bundle']['account'], LS['allowed_accounts'])
            if unavailable:
                row = next(r for r in result['ranking'] if r['id'] == 'sol_xhigh')
                self.assertIn('marked unavailable by the parent', row['excluded'])

    def test_empty_whitelist_is_not_default(self):
        self.write({'allowed_accounts': []})
        self.assertEqual(self.plan()['choice'], 'none')
        self.assertEqual(subscription_judge.judge_order({}, 'auto'), [])

    def test_excluded_coupled_owner_is_blocked(self):
        result = self.plan({'work_kind': 'code_implementation', 'difficulty': 'coupled',
                            'chain': {'id': 'a', 'coupling': 'high', 'worker': {'bundle': 'opus55_high'}}})
        self.assertIn('excluded by host policy', result['decision']['resume_blocked_by'])
        self.assertTrue(result['decision']['needs_user_decision'])

    def test_select_filters_all_disallowed_accounts(self):
        request = self.request()
        self.assertEqual(set(request['state']['candidates']), set(LS['allowed_accounts']))
        self.assertEqual(set(request['state']['host_policy_excluded']), {'claude', 'codex-work', 'grok', 'api-proxy'})

    def test_judges_auto_explicit_and_account_intersection(self):
        self.assertEqual(subscription_judge.judge_order({}, 'auto'), ['codex'])
        self.assertEqual(subscription_judge.judge_order({}, 'codex'), ['codex'])
        with self.assertRaisesRegex(host_policy.HostPolicyError, 'excluded by host policy'):
            subscription_judge.judge_order({}, 'sonnet')
        self.write({'allowed_accounts': ['codex'], 'judges': ['sonnet']})
        self.assertEqual(subscription_judge.judge_order({}, 'auto'), [])

    def test_delegate_and_select_denied_explicit_judge_never_call_cli(self):
        with patch.object(subscription_cli, 'cli_prefix', side_effect=AssertionError('model call forbidden')):
            for stage in ('delegate', 'select'):
                with self.assertRaises(host_policy.HostPolicyError):
                    subscription_judge.evaluate(self.request(stage), requested='sonnet')

    def test_no_allowed_judge_continues_self_without_cli(self):
        self.write({'allowed_accounts': ['codex'], 'judges': []})
        with patch.object(subscription_cli, 'cli_prefix', side_effect=AssertionError('model call forbidden')):
            result = subscription_judge.evaluate(self.request('delegate'))
        self.assertEqual(result['source'], 'judge_unavailable')
        self.assertIn('continue feasible parent work', result['next'])

    def test_prepared_select_rechecks_current_policy(self):
        request = self.request(accounts=('codex',))
        self.write({'allowed_accounts': []})
        with patch.object(subscription_cli, 'cli_prefix', side_effect=AssertionError('model call forbidden')):
            result = subscription_judge.evaluate(request)
        self.assertEqual(result['choice'], 'none')

    def record(self, constraint='user_override', model='claude-opus-5-5', usage=None):
        """record_constraint for a route on `claude`, an account outside LS (out of automatic routing)."""
        task = self.directory/'task.json'
        task.write_text(json.dumps({'task': {'work_kind': 'code_implementation', 'difficulty': 'hard', 'profile': profile()}}))
        args = router.argparse.Namespace(basis='User: run this on Opus on claude', provider='claude', account='claude',
                model=model, effort='high', input=task, usage_cache=None, accounts=None,
                constraint=constraint, specialty=None)
        with patch.object(decision_ledger, 'ledger_path', return_value=self.directory/'ledger.jsonl'), \
             patch.object(usage_reading, 'usage_snapshot', return_value=usage or {'accounts': {}}), \
             patch.object(subscription_cli, 'cli_prefix', side_effect=AssertionError('model call forbidden')):
            return router.record_constraint(args), decision_ledger.read_ledger()

    def test_a_route_the_user_named_outside_automatic_routing_dispatches_with_a_warning(self):
        # Rule (router-internals.md, Pins): allowed_accounts governs automatic routing; the user's own naming
        # (user_override) reaches an account outside it, says so in the decision, and is launch authority.
        result, ledger = self.record()
        self.assertNotIn('host_policy_violation', result)
        self.assertNotIn('host_policy_violation', ledger[-1])
        # The one field the audit filters on, written by the ledger owner.
        self.assertEqual(ledger[-1]['outside_automatic_routing'], 'claude')
        self.assertTrue(any(w.startswith('account claude is out of automatic routing on this host')
                            and w.endswith('launched because the user named it in this request')
                            for w in result['warnings']), result.get('warnings'))
        self.assertTrue(dispatch_registry.decision_record(result['decision_id'])['dispatchable'])
        # The router by itself would still not choose it.
        self.assertIn('excluded by host policy', result['override_cost']['excluded'])

    def test_a_project_rule_does_not_override_this_hosts_switch(self):
        # Rule: a hard_constraint (or any constraint but user_override) is shared between machines and does not override
        # one machine's switch; on an account in automatic routing it is launch authority as before.
        for constraint in ('hard_constraint', 'dot_closed_loop'):
            with self.subTest(constraint=constraint):
                result, ledger = self.record(constraint)
                violation = result['host_policy_violation']
                self.assertFalse(violation['dispatch_allowed'])
                self.assertEqual(violation['account'], 'claude')
                self.assertIn('--constraint user_override', violation['reason'])
                self.assertEqual(ledger[-1]['host_policy_violation'], violation)
                self.assertNotIn('outside_automatic_routing', ledger[-1])      # refused, so nothing reached past it
                self.assertFalse(dispatch_registry.decision_record(result['decision_id'])['dispatchable'])
        self.write({**LS, 'allowed_accounts': [*LS['allowed_accounts'], 'claude']})
        result, ledger = self.record('hard_constraint')
        self.assertNotIn('outside_automatic_routing', ledger[-1])
        result, ledger = self.record()
        self.assertNotIn('outside_automatic_routing', ledger[-1])             # in automatic routing: nothing to mark
        result, _ = self.record('hard_constraint')
        self.assertNotIn('host_policy_violation', result)
        self.assertFalse(any('out of automatic routing' in w for w in result.get('warnings', ())))

    def test_facts_still_refuse_a_named_route_outside_automatic_routing(self):
        # Rule 3: naming lifts the preference switch only; no sign-in, a model the account does not offer, a model
        # unchecked in model management, an exhausted window and a launch cooldown still refuse.
        now = datetime.now(timezone.utc)
        exhausted = {'accounts': {'claude': {'status': 'fresh', 'fetched_at': now.isoformat(), 'available': True,
                                             'windows': [{'id': 'five_hour', 'used_pct': 100,
                                                          'resets_at': (now + timedelta(hours=2)).isoformat()}]}}}
        override = Path(router.os.environ['AGENTCOMPASS_ACCOUNTS_OVERRIDE'])
        import launch_failures
        import provider_inventory
        cases = (
            ('no sign-in', lambda: patch.object(provider_inventory, 'current', return_value={
                'accounts': {'claude': {'family': 'claude', 'status': 'auth_failed', 'reason': 'credentials_rejected'}},
                'launchers': {}}),
             {}, 'credentials_rejected'),
            ('model not offered', None, {'model': 'gpt-6-astra'}, 'named_model_unavailable|no known provider/account/model'),
            ('unchecked', lambda: override_file(override, {'claude': {'bundles': {'opus55_high': {'disabled': True},
                                                                                 'opus55_medium': {'disabled': True},
                                                                                 'opus55_low': {'disabled': True},
                                                                                 'opus55_xhigh': {'disabled': True},
                                                                                 'opus55_max': {'disabled': True}}}}),
             {}, 'disabled'),
            ('exhausted window', None, {'usage': exhausted}, 'five_hour'),
            ('cooldown', lambda: cooling(launch_failures, now), {}, 'cooling down'),
        )
        for label, context, kwargs, cause in cases:
            with self.subTest(fact=label), (context() if context else nullcontext()):
                with self.assertRaisesRegex(ValueError, cause):
                    self.record(**kwargs)

    def test_automatic_resume_never_picks_a_worker_out_of_automatic_routing_or_on_an_unchecked_model(self):
        # Rule 1: resuming an idle worker without being told to is the router's choice, so both preference switches
        # apply; an explicit --resume reads only the gate's facts (roll._auto_resumable / _worker_gate_refusal).
        import roll
        accounts = account_fleet.load_accounts()
        policy = host_policy.load_policy()
        worker = {'agent_id': 'w1', 'dispatch_id': 'd1', 'bundle_id': 'opus55_high',
                  'bundle': {'account': 'claude', 'provider': 'claude', 'model': 'claude-opus-5-5', 'effort': 'high',
                             'bundle_id': 'opus55_high'}}
        with patch.object(roll, '_worker_gate_refusal', return_value=None):
            self.assertFalse(roll._auto_resumable(worker, policy, accounts=accounts))
            self.assertTrue(roll._auto_resumable(worker, {'allowed_accounts': ['claude']}, accounts=accounts))
            for row in accounts['accounts']['claude']['bundles'].values():
                if row.get('model') == 'claude-opus-5-5':
                    row['disabled'] = True
            self.assertFalse(roll._auto_resumable(worker, {'allowed_accounts': ['claude']}, accounts=accounts))
        with patch.object(roll, '_worker_gate_refusal', return_value='quota window five_hour is unavailable'):
            self.assertFalse(roll._auto_resumable(worker, None, accounts=account_fleet.load_accounts()))

    def test_same_policy_at_different_paths_has_identical_allocation(self):
        first = self.plan()
        other = self.directory/'other-user'/'profile.json'
        other.parent.mkdir()
        other.write_bytes(self.path.read_bytes())
        with patch.object(host_policy, 'policy_path', return_value=(other, True)):
            second = self.plan()
        self.assertEqual(first, second)
        self.assertNotIn(str(self.directory), json.dumps(first))

    def test_usage_selection_ignores_dispatch_exclusions_and_respects_explicit_usage_providers(self):
        defaults = ['claude', 'codex', 'codex-work', 'grok', 'antigravity']
        self.assertEqual(host_policy.usage_providers(host_policy.load_policy(), defaults), ['codex', 'antigravity'])
        self.assertEqual(host_policy.usage_providers(None, defaults), defaults)
        self.assertEqual(host_policy.usage_providers({'allowed_accounts': ['codex']}, defaults), defaults)
        self.assertEqual(host_policy.usage_providers({'allowed_accounts': []}, defaults), defaults)
        self.assertEqual(host_policy.usage_providers({'allowed_accounts': ['codex'], 'usage_providers': ['grok']}, defaults), ['grok'])
        self.assertEqual(host_policy.usage_providers({'allowed_accounts': ['codex'], 'usage_providers': []}, defaults), [])
        # Dispatch exclusions still strictly govern routing/allocation even when usage is collected for all accounts.
        self.write({'allowed_accounts': ['codex']})
        planned = self.plan({'work_kind': 'code_implementation', 'difficulty': 'hard', 'profile': profile()})
        # The fixture has no quota reading, so codex itself may be out (mode B); what matters is that nothing else is chosen.
        self.assertIn((planned.get('selected_bundle') or {}).get('account'), (None, 'codex'))
        for row in planned['ranking']:
            if row['bundle']['account'] != 'codex':
                self.assertIn('excluded by host policy', row['excluded'])

    def registered(self, **states):
        return {'_discovery': {'accounts': {name: {'status': state} for name, state in states.items()}},
                'accounts': {name: {'billing': 'subscription', 'verified': state == 'ready', 'bundles': {
                    name: {'provider': name, 'model': 'service'}}} for name, state in states.items()}}

    def test_known_accounts_preserves_exclusions_and_new_ready_accounts_enroll_once(self):
        baseline = self.registered(codex='ready', claude='ready')
        self.write({'allowed_accounts': ['codex'], 'known_accounts': ['codex', 'claude'],
                    'judges': ['codex'], 'usage_providers': ['codex']})
        result = host_policy.sync_discovered(baseline)
        self.assertEqual(result['added'], [])
        self.assertEqual(host_policy.load_policy()['allowed_accounts'], ['codex'])
        next_accounts = self.registered(codex='ready', claude='ready', **{'grok-bot': 'missing_configuration', 'codex-work': 'ready'})
        # First observation need not be ready: it must not become a permanent manual deny.
        self.assertEqual(host_policy.sync_discovered(next_accounts)['added'], ['codex-work'])
        self.assertNotIn('grok-bot', host_policy.load_policy()['known_accounts'])
        next_accounts = self.registered(codex='ready', claude='ready', **{'grok-bot': 'ready', 'codex-work': 'ready'})
        self.assertEqual(host_policy.sync_discovered(next_accounts)['added'], ['grok-bot'])
        document = host_policy.load_policy()
        self.assertEqual(document['judges'], ['codex'])
        self.assertEqual(document['usage_providers'], ['codex'])
        after = self.path.read_bytes()
        self.assertEqual(host_policy.sync_discovered(next_accounts)['added'], [])
        self.assertEqual(self.path.read_bytes(), after)
        # Manually disable an enrolled account; neither refresh nor temporary disappearance resurrects it.
        document['allowed_accounts'].remove('grok-bot')
        self.write(document)
        host_policy.sync_discovered(baseline)
        host_policy.sync_discovered(next_accounts)
        self.assertNotIn('grok-bot', host_policy.load_policy()['allowed_accounts'])
        self.assertNotIn('claude', host_policy.load_policy()['allowed_accounts'])

    def test_missing_contract_or_expired_disabled_removed_rejected_account_is_not_enrolled(self):
        self.write({'allowed_accounts': ['codex'], 'known_accounts': ['codex']})
        accounts = self.registered(codex='ready', removed='removed', disabled='disabled', rejected='paused',
                                   expired='ready', unregistered='ready', no_bundles='ready', banned_bundle='ready')
        del accounts['accounts']['unregistered']
        accounts['accounts']['no_bundles']['bundles'] = {}
        accounts['accounts']['banned_bundle']['bundles']['banned_bundle']['disabled'] = True
        accounts['accounts']['expired']['expires_at'] = '2000-01-01T00:00:00Z'
        self.assertEqual(host_policy.sync_discovered(accounts)['added'], [])
        self.assertEqual(host_policy.load_policy()['known_accounts'], ['codex'])
        # Obtaining the missing registered contract later is sufficient; no second consent flag needed.
        accounts['accounts']['unregistered'] = {'billing': 'subscription', 'verified': True, 'bundles': {'real': {'provider': 'unregistered', 'model': 'real'}}}
        self.assertEqual(host_policy.sync_discovered(accounts)['added'], ['unregistered'])
        self.assertFalse(host_policy.account_allowed(host_policy.load_policy(), 'arbitrary-request-name'))


if __name__ == '__main__':
    unittest.main()
