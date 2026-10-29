"""Host exclusions apply at dispatch boundaries; fixtures never read real host state."""
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch

import allocator
import host_policy
import route_judge as router

from router_test_support import isolated_router_state, temporary_root


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup, policy=False)

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
        self.accounts = allocator.load_accounts(allocator.reference_fleet_path())
        self.now = datetime(2026, 10, 11, tzinfo=timezone.utc)

    def write(self, data):
        self.path.write_text(json.dumps(data), encoding='utf-8')

    def plan(self, task=None, **kwargs):
        return allocator.plan(task or {'work_kind': 'general_implementation', 'difficulty': 'hard'},
                              {'accounts': {}}, now=self.now, accounts=self.accounts, **kwargs)

    def candidate(self, account):
        return {'provider': account, 'account': account, 'model': 'verified', 'effort': 'high',
                'quality_fit': 'review', 'available': True}

    def request(self, stage='select', accounts=('claude', 'codex', 'codex-2', 'grok', 'api-proxy', 'dot', 'chatgpt-web', 'antigravity')):
        with patch.object(router, 'usage_snapshot', return_value={'accounts': {}}):
            return router.build_request(stage, {'task': {'objective': 'review'},
                                               'candidates': {a: self.candidate(a) for a in accounts}})

    def test_missing_default_preserves_defaults(self):
        self.path.unlink()
        self.assertIsNone(host_policy.load_policy())
        self.assertTrue(host_policy.account_allowed(None, 'claude'))
        self.assertEqual(router.judge_order({}, 'auto'), ['codex', 'sonnet'])

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
            result = self.plan({'work_kind': 'general_implementation', 'difficulty': 'hard',
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
        self.assertEqual(router.judge_order({}, 'auto'), [])

    def test_excluded_coupled_owner_is_blocked(self):
        result = self.plan({'work_kind': 'general_implementation', 'difficulty': 'coupled',
                            'chain': {'id': 'a', 'coupling': 'high', 'worker': {'bundle': 'opus55_high'}}})
        self.assertIn('excluded by host policy', result['decision']['resume_blocked_by'])
        self.assertTrue(result['decision']['needs_user_decision'])

    def test_select_filters_all_disallowed_accounts(self):
        request = self.request()
        self.assertEqual(set(request['state']['candidates']), set(LS['allowed_accounts']))
        self.assertEqual(set(request['state']['host_policy_excluded']), {'claude', 'codex-2', 'grok', 'api-proxy'})

    def test_judges_auto_explicit_and_account_intersection(self):
        self.assertEqual(router.judge_order({}, 'auto'), ['codex'])
        self.assertEqual(router.judge_order({}, 'codex'), ['codex'])
        with self.assertRaisesRegex(host_policy.HostPolicyError, 'excluded by host policy'):
            router.judge_order({}, 'sonnet')
        self.write({'allowed_accounts': ['codex'], 'judges': ['sonnet']})
        self.assertEqual(router.judge_order({}, 'auto'), [])

    def test_delegate_and_select_denied_explicit_judge_never_call_cli(self):
        with patch.object(router, 'cli_prefix', side_effect=AssertionError('model call forbidden')):
            for stage in ('delegate', 'select'):
                with self.assertRaises(host_policy.HostPolicyError):
                    router.evaluate(self.request(stage), requested='sonnet')

    def test_no_allowed_judge_continues_self_without_cli(self):
        self.write({'allowed_accounts': ['codex'], 'judges': []})
        with patch.object(router, 'cli_prefix', side_effect=AssertionError('model call forbidden')):
            result = router.evaluate(self.request('delegate'))
        self.assertEqual(result['source'], 'judge_unavailable')
        self.assertIn('continue feasible parent work', result['next'])

    def test_prepared_select_rechecks_current_policy(self):
        request = self.request(accounts=('codex',))
        self.write({'allowed_accounts': []})
        with patch.object(router, 'cli_prefix', side_effect=AssertionError('model call forbidden')):
            result = router.evaluate(request)
        self.assertEqual(result['choice'], 'none')

    def test_record_keeps_constraint_and_flags_violation_in_ledger(self):
        task = self.directory/'task.json'
        task.write_text(json.dumps({'task': {'work_kind': 'general_implementation', 'difficulty': 'hard'}}))
        args = router.argparse.Namespace(basis='User explicit retained owner', provider='claude', account='claude',
                model='claude-opus-5-5', effort='high', input=task, usage_cache=None, accounts=None,
                constraint='user_override', specialty=None)
        with patch.object(router, 'ledger_path', return_value=self.directory/'ledger.jsonl'), \
             patch.object(router, 'usage_snapshot', return_value={'accounts': {}}), \
             patch.object(allocator, 'capacity_override_path', return_value=self.directory/'absent-capacity.json'), \
             patch.object(router, 'cli_prefix', side_effect=AssertionError('model call forbidden')):
            result = router.record_constraint(args)
            ledger = router.read_ledger()
        self.assertFalse(result['host_policy_violation']['dispatch_allowed'])
        self.assertEqual(ledger[0]['host_policy_violation'], result['host_policy_violation'])
        self.assertFalse(result['agrees'])
        self.assertIn('excluded by host policy', result['override_cost']['excluded'])

    def test_same_policy_at_different_paths_has_identical_allocation(self):
        first = self.plan()
        other = self.directory/'other-user'/'profile.json'
        other.parent.mkdir()
        other.write_bytes(self.path.read_bytes())
        with patch.object(host_policy, 'policy_path', return_value=(other, True)):
            second = self.plan()
        self.assertEqual(first, second)
        self.assertNotIn(str(self.directory), json.dumps(first))

    def test_usage_selection_intersects_both_lists(self):
        defaults = ['claude', 'codex', 'codex-2', 'grok', 'antigravity']
        self.assertEqual(host_policy.usage_providers(host_policy.load_policy(), defaults), ['codex', 'antigravity'])
        self.assertEqual(host_policy.usage_providers(None, defaults), defaults)
        self.assertEqual(host_policy.usage_providers({'allowed_accounts': ['codex'], 'usage_providers': ['grok']}, defaults), [])

    def registered(self, **states):
        return {'_discovery': {'accounts': {name: {'status': state} for name, state in states.items()}},
                'accounts': {name: {'verified': state == 'ready', 'bundles': {
                    name: {'provider': name, 'model': 'service'}}} for name, state in states.items()}}

    def test_legacy_migration_preserves_exclusions_and_new_ready_accounts_enroll_once(self):
        self.write({'allowed_accounts': ['codex'], 'judges': ['codex'], 'usage_providers': ['codex']})
        baseline = self.registered(codex='ready', claude='ready')
        before = self.path.read_bytes()
        self.assertEqual(host_policy.sync_discovered(baseline)['mode'], 'legacy')
        self.assertEqual(self.path.read_bytes(), before)
        result = host_policy.sync_discovered(baseline, enable=True)
        self.assertEqual(result['ready_but_excluded'], ['claude'])
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
        accounts['accounts']['banned_bundle']['bundles']['banned_bundle']['disabled'] = 'user disabled'
        accounts['accounts']['expired']['expires_at'] = '2000-01-01T00:00:00Z'
        self.assertEqual(host_policy.sync_discovered(accounts)['added'], [])
        self.assertEqual(host_policy.load_policy()['known_accounts'], ['codex'])
        # Obtaining the missing registered contract later is sufficient; no second consent flag needed.
        accounts['accounts']['unregistered'] = {'verified': True, 'bundles': {'real': {'provider': 'unregistered', 'model': 'real'}}}
        self.assertEqual(host_policy.sync_discovered(accounts)['added'], ['unregistered'])
        self.assertFalse(host_policy.account_allowed(host_policy.load_policy(), 'arbitrary-request-name'))


if __name__ == '__main__':
    unittest.main()
