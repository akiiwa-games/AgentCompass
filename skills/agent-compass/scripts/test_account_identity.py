"""Account metadata, isolated homes and explicit quota reserves are independent of IDs."""
import copy
import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

import allocator
import accounts_override
import provider_inventory
import roll
import route_judge as router
from router_test_support import isolated_router_state


class AccountIdentityTests(unittest.TestCase):
    def setUp(self):
        self.root = isolated_router_state(self.addCleanup)
        self.now = datetime.now(timezone.utc)
        self.fleet = json.loads(allocator.reference_fleet_path().read_text(encoding='utf-8'))
        self.fleet['accounts'] = {'team-backup': {
            'family': 'claude', 'billing': 'subscription', 'verified': True,
            'windows': {'five_hour': {'hours': 5}},
            'bundles': {'team_worker': {'provider': 'team-backup', 'harness': 'claude',
                                      'model': 'claude-sonnet-5-5', 'effort': 'high'}},
        }}
        self.fleet['dispatch_efforts']['team-backup'] = {'claude-sonnet-5-5': ['high']}
        self.inventory = {'accounts': {'team-backup': {
            'family': 'claude', 'home': str(self.root / 'profiles' / 'backup'), 'status': 'ready'}},
            'launchers': {'team-backup': {'family': 'claude', 'status': 'ready'}}}
        self.fleet['_discovery'] = self.inventory
        self.enterContext(patch.object(router, 'load_allocator', return_value=allocator))
        self.enterContext(patch.object(allocator, 'load_accounts', return_value=self.fleet))
        self.enterContext(patch.object(provider_inventory, 'current', return_value=self.inventory))

    def usage(self, remaining):
        return {'read_ok': True, 'accounts': {'team-backup': {'status': 'fresh', 'windows': [{
            'id': 'five_hour', 'used_pct': 100 - remaining,
            'remaining_pct_before_reset': remaining, 'reset_time_passed': False,
            'seconds_until_reset': 3600, 'resets_at': (self.now + timedelta(hours=1)).isoformat(),
        }]}}}

    def test_select_uses_configured_reserve_and_neutral_default(self):
        candidate = {'provider': 'team-backup', 'account': 'team-backup',
                     'model': 'claude-sonnet-5-5', 'quality_fit': 'sufficient'}
        for reserve, expected in ((None, True), (25, False), (10, True)):
            with self.subTest(reserve=reserve):
                window = self.fleet['accounts']['team-backup']['windows']['five_hour']
                window.pop('launch_min_remaining_pct', None)
                if reserve is not None:
                    window['launch_min_remaining_pct'] = reserve
                with patch.object(router, 'usage_snapshot', return_value=self.usage(15)) as snapshot:
                    result = router.build_request('select', {'task': {'objective': 'Review'},
                                                           'candidates': {'worker': candidate}})
                self.assertEqual('worker' in result['state']['candidates'], expected)
                self.assertIn('team-backup', snapshot.call_args.args[1])

    def test_record_uses_configured_reserve_and_neutral_default(self):
        args = argparse.Namespace(provider='team-backup', account='team-backup', model='claude-sonnet-5-5',
            effort='high', basis='Explicit test choice', dry_run=True, input=None, usage_cache=None,
            accounts=None, constraint='user_override', specialty=None, parent_id='test-parent')
        with patch.object(router, 'usage_snapshot', return_value=self.usage(15)):
            self.assertEqual(router.record_constraint(args)['selected_bundle']['account'], 'team-backup')
            self.fleet['accounts']['team-backup']['windows']['five_hour']['launch_min_remaining_pct'] = 25
            with self.assertRaisesRegex(ValueError, 'quota|floor|exhausted'):
                router.record_constraint(args)

    def test_judge_selects_arbitrary_account_and_obeys_reserve(self):
        empty = self.usage(0)['accounts']['team-backup']
        self.fleet['accounts']['primary-team'] = copy.deepcopy(self.fleet['accounts']['team-backup'])
        self.inventory['accounts']['primary-team'] = {'family': 'claude'}
        self.fleet['accounts']['primary-team']['windows']['five_hour']['launch_min_remaining_pct'] = 25
        self.fleet['accounts']['team-backup']['windows']['five_hour']['launch_min_remaining_pct'] = 25
        usage = self.usage(26)
        usage['accounts']['primary-team'] = empty
        self.assertEqual(router.resolve_judge('sonnet', usage)['account'], 'team-backup')
        self.assertIn('sonnet', router.judge_order(usage, 'auto'))
        usage['accounts']['team-backup'] = self.usage(15)['accounts']['team-backup']
        self.assertNotIn('sonnet', router.judge_order(usage, 'auto'))

    def test_judge_uses_neutral_reserve_without_private_default(self):
        self.assertEqual(router.resolve_judge('sonnet', self.usage(15))['account'], 'team-backup')
        self.assertIn('sonnet', router.judge_order(self.usage(15), 'auto'))

    def test_subscription_credentials_follow_discovered_home(self):
        with patch.dict(router.os.environ, {'CLAUDE_CODE_OAUTH_TOKEN': 'inherited',
                                          'ANTHROPIC_API_KEY': 'inherited'}):
            env = router.subscription_env('team-backup')
        self.assertEqual(env['CLAUDE_CONFIG_DIR'], self.inventory['accounts']['team-backup']['home'])
        self.assertEqual(env['CLAUDE_SECURESTORAGE_CONFIG_DIR'], env['CLAUDE_CONFIG_DIR'])
        self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN', env)
        self.assertNotIn('ANTHROPIC_API_KEY', env)

    def test_name_does_not_prove_claude_family(self):
        self.inventory['accounts']['claude-impostor'] = {'family': 'codex', 'home': str(self.root / 'other')}
        with patch.dict(router.os.environ, {}, clear=True):
            self.assertNotIn('CLAUDE_CONFIG_DIR', router.subscription_env('claude-impostor'))
        self.assertIsNone(router.claude_session_limit('claude-impostor', self.root, self.now, self.inventory))

    def test_session_limit_reads_discovered_home(self):
        log = Path(self.inventory['accounts']['team-backup']['home']) / 'projects' / 'work' / 'session.jsonl'
        log.parent.mkdir(parents=True)
        reset = self.now + timedelta(hours=1)
        log.write_text(json.dumps({'timestamp': self.now.isoformat(), 'message': {'content': [{
            'type': 'text', 'text': "You've hit your session limit · resets " + reset.strftime('%I:%M%p') + ' (UTC)'}]}})
            + '\n', encoding='utf-8')
        limit = router.claude_session_limit('team-backup', self.root, self.now, self.inventory)
        self.assertIsNotNone(limit)
        self.assertGreater(limit['resets_at'], self.now)

    def test_cli_fallback_and_harness_use_discovered_family(self):
        with patch.object(roll.dispatch_flow, 'paseo_config', return_value={}), \
                patch.object(roll, '_router_override_proxy_spec', return_value=None):
            family, env = roll.provider_definition('team-backup')
        self.assertEqual(family, 'claude')
        self.assertEqual(env['CLAUDE_CONFIG_DIR'], self.inventory['accounts']['team-backup']['home'])
        self.fleet['accounts']['team-backup']['bundles']['team_worker'].pop('harness')
        self.assertEqual(allocator.bundles(self.fleet)['team_worker']['harness'], 'claude')

    def test_session_telemetry_uses_discovered_family_and_custom_home(self):
        record = self.root / '.paseo' / 'agents' / 'workspace' / 'worker.json'
        record.parent.mkdir(parents=True)
        record.write_text(json.dumps({'persistence': {'provider': 'team-backup', 'sessionId': 'session'}}))
        log = Path(self.inventory['accounts']['team-backup']['home']) / 'projects' / 'work' / 'session.jsonl'
        log.parent.mkdir(parents=True)
        log.write_text(json.dumps({'type': 'assistant', 'message': {'usage': {'input_tokens': 123}}}) + '\n')
        with patch.object(provider_inventory, 'discover', return_value=self.inventory):
            self.assertEqual(allocator.session_state('worker', self.root)['context_tokens'], 123)

    def test_missing_arbitrary_account_is_removed_by_family(self):
        inventory = {'accounts': {'primary-team': {'family': 'claude', 'status': 'missing_credentials'}},
                     'launchers': {}}
        with patch.object(provider_inventory, '_apply_auto_bundles'):
            effective = provider_inventory.apply(copy.deepcopy(self.fleet), inventory)
        self.assertNotIn('team-backup', effective['accounts'])

    def test_shared_quota_preferences_use_family_instead_of_prefix(self):
        for provider, family, expected in (('other-team', 'claude', 'other-team'),
                                           ('team-impostor', 'codex', 'team-backup')):
            with self.subTest(provider=provider):
                fleet = copy.deepcopy(self.fleet)
                fleet['accounts'][provider] = {'family': family}
                self.assertEqual(accounts_override.preference_account(
                    {'provider': provider}, 'team-backup', fleet), expected)


if __name__ == '__main__':
    unittest.main()
