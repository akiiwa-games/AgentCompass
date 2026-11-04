"""A clean install routes like a calibrated fleet: shipped template + discovery, no accounts.local.json.

The expected values are the ones a hand-written machine fleet used to set (2026-11-04). Only the
shipped references and a synthetic discovery snapshot are read; the user home is never touched.
"""
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch

import accounts_override
import allocator
import provider_inventory as inventory

from router_test_support import isolated_router_state, temporary_root


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup, discovery=False, policy=False)


def shipped_template():
    return accounts_override.normalize(json.loads((allocator.REFERENCES / 'accounts.json').read_text(encoding='utf-8-sig')))


def snapshot():
    """Claude Max 20x, a second Claude Pro login, Codex Pro, Antigravity with a quota reading but no
    reported tier, a Pro ChatGPT web bridge, OpenCode free, Dot, Grok and the Grok Bot."""
    rows = {
        'claude': {'family': 'claude', 'status': 'ready', 'plan': 'max20', 'billing': 'subscription',
                   'sources': ['native'], 'identity': 'id-claude', 'quota_account': 'claude'},
        'claude-2': {'family': 'claude', 'status': 'ready', 'plan': 'pro', 'billing': 'subscription',
                     'sources': ['native', 'paseo'], 'identity': 'id-claude-2', 'quota_account': 'claude-2'},
        'codex': {'family': 'codex', 'status': 'ready', 'plan': 'pro', 'billing': 'subscription',
                  'sources': ['native'], 'identity': 'id-codex', 'quota_account': 'codex'},
        'antigravity': {'family': 'antigravity', 'provider': 'antigravity-resilient', 'status': 'ready',
                        'plan': None, 'billing': 'subscription', 'sources': ['native', 'usage-cache']},
        'opencode': {'family': 'opencode', 'status': 'ready', 'plan': 'free', 'billing': 'subscription', 'sources': ['native']},
        'grok': {'family': 'grok', 'status': 'ready', 'plan': 'subscription', 'billing': 'subscription',
                 'sources': ['native', 'usage-cache'], 'usage_windows': ['credits']},
        'dot': {'family': 'dot', 'status': 'ready', 'sources': ['native']},
        'chatgpt-web': {'family': 'chatgpt-web', 'status': 'ready', 'plan': 'pro', 'sources': ['native']},
        'grok-bot': {'family': 'grok-bot', 'status': 'ready', 'billing': 'unknown', 'sources': ['native']},
    }
    launchers = {name: {'family': row['family'], 'status': 'ready'} for name, row in rows.items()}
    launchers['antigravity-resilient'] = {'family': 'antigravity-resilient', 'status': 'ready'}
    return {'accounts': rows, 'launchers': launchers}


class CleanInstallParityTests(unittest.TestCase):
    def setUp(self):
        self.accounts = inventory.apply(shipped_template(), snapshot())
        self.specs = self.accounts['accounts']

    def test_subscription_capacity_comes_from_the_discovered_plan(self):
        claude, codex = self.specs['claude'], self.specs['codex']
        self.assertEqual(claude['capacity_usd'], {'seven_day': 2380, 'five_hour': 376, 'weekly_fable': 1190})
        self.assertEqual(claude['burn_pct_per_worker_hour']['seven_day'], 0.27)
        self.assertEqual(claude['burn_pct_per_worker_hour']['weekly_fable'], 0.27)
        self.assertEqual(codex['capacity_usd'], {'usage': 325})
        self.assertEqual(codex['burn_pct_per_worker_hour'], {'usage': 3.48})
        self.assertTrue(claude['verified'] and codex['verified'])

    def test_antigravity_without_a_reported_tier_routes_on_the_documented_default(self):
        agy = self.specs['antigravity']
        self.assertEqual(agy['capacity_usd'], {'gemini-weekly': 625, 'gemini-5h': 182, '3p-weekly': 88, '3p-5h': 17.6})
        self.assertEqual((agy['burn_pct_per_worker_hour']['gemini-weekly'], agy['burn_pct_per_worker_hour']['gemini-5h']),
                         (0.08, 0.27))
        self.assertTrue(agy['verified'])  # a quota reading is the sign-in proof
        self.assertTrue(all(allocator.capacity_usd(agy, wid, datetime.now(timezone.utc)) for wid in agy['windows']))

    def test_a_named_smaller_antigravity_plan_beats_the_default(self):
        template = shipped_template()
        template['accounts']['antigravity']['plan'] = 'Google AI Pro'
        agy = inventory.apply(template, snapshot())['accounts']['antigravity']
        self.assertEqual(agy['capacity_usd']['gemini-weekly'], 31)

    def test_intrinsic_unmetered_accounts_and_the_pro_web_cadence(self):
        for name in ('opencode', 'dot'):
            self.assertTrue(self.specs[name]['unmetered'] and self.specs[name]['unlimited'], name)
            self.assertTrue(self.specs[name]['verified'], name)
        web = self.specs['chatgpt-web']
        self.assertEqual(web['cadence'], {'window_seconds': 43200, 'limit': 5})
        self.assertTrue(web['unmetered'])
        self.assertEqual(web['bundles']['pro_max']['when'], {'min_difficulty': 'frontier'})

    def test_second_account_comes_from_discovery(self):
        second = self.specs['claude-2']
        self.assertEqual(second['capacity_usd'], {'seven_day': 156, 'five_hour': 18})
        self.assertTrue(second['verified'])
        self.assertEqual(self.accounts['dispatch_efforts']['claude-2'], self.accounts['dispatch_efforts']['claude'])
        self.assertTrue(second['bundles'])
        self.assertTrue(all(b['provider'] == 'claude-2' for b in second['bundles'].values()))

    def test_grok_bot_draws_on_the_grok_meter_and_routes_automatically(self):
        inventory.apply_shared_quota(self.accounts)
        self.assertEqual(self.specs['grok-bot']['quota_source'], 'grok')
        self.assertIsNone(accounts_override.manual_only_billing('grok-bot', self.accounts))

    def test_calibrated_scoring_and_route_policy_are_the_public_defaults(self):
        scoring = self.accounts['scoring']
        self.assertEqual(scoring['profile']['subjective'], {'weight': 0.49, 'vector_weight': 0.34, 'route_weight': 0.17})
        self.assertEqual(scoring['harness_order'], ['codex', 'pi'])
        self.assertEqual(scoring['profile']['harness_by_style'], {'engineering': ['codex', 'pi'], 'exploration': ['pi', 'codex']})
        self.assertEqual({(r['harness'], r['factor']) for r in scoring['harness_factors']['rules']},
                         {('antigravity', 0.9), ('antigravity-resilient', 0.9)})
        self.assertEqual((scoring['unlimited_bonus'], scoring['fast_burn_multiplier']), (0.18, 2.0))
        policy = allocator.load_capabilities(accounts=self.accounts)
        self.assertEqual(policy['remote_reroute_guard'], {'enabled': True, 'bundle_id': 'dot_text', 'window_seconds': 600})
        self.assertEqual(policy['route_fit_peers'], {'grok-bot': 'claude-opus-5-5'})
        self.assertEqual(policy['routes']['dot_execution']['preferred'], ['dot'])
        self.assertTrue(policy['routes']['complex_build_plan']['restrict_models'])
        self.assertEqual(policy['routes']['computer_use']['preferred'], ['gpt-6-astra', 'gpt-6.1-sol'])
        self.assertEqual([t['label'] for t in self.accounts['priority_targets']], ['Astra', 'Argon', 'Opus 5.5'])

    def test_a_sparse_scoring_override_keeps_the_new_defaults(self):
        merged = accounts_override.merge(self.accounts['scoring'], {'safety_margin_pct': 7})
        self.assertEqual((merged['safety_margin_pct'], merged['unlimited_bonus']), (7, 0.18))
        self.assertEqual(merged['profile']['subjective']['weight'], 0.49)

    def test_discovered_models_default_from_public_data_alone(self):
        snap = snapshot()
        advertised = {'claude': ['claude-opus-5-5', 'claude-opus-4-8', 'claude-haiku-4-5', 'claude-haiku-5-5', 'claude-opus-9-1'],
                      'codex': ['gpt-6.1-sol', 'gpt-6.1-terra', 'gpt-6-luna', 'gpt-6-sol', 'gpt-5.6-terra'],
                      'grok': ['grok-4.7', 'grok-4.7-build-fast', 'grok-4.6']}
        for name, models in advertised.items():
            snap['accounts'][name]['models'] = models
        accounts = inventory.apply(shipped_template(), snap)
        superseded = shipped_template()['superseded']
        auto = {(b['provider'], b['launch_model']): b['disabled'] for spec in accounts['accounts'].values()
                for b in spec['bundles'].values() if b.get('auto')}
        self.assertTrue(auto)
        for (provider, model), disabled in auto.items():
            with self.subTest(provider=provider, model=model):
                self.assertEqual(disabled, model in superseded)
        self.assertEqual({m for (_, m), off in auto.items() if off},
                         {'claude-opus-4-8', 'claude-haiku-4-5', 'gpt-6-sol', 'gpt-5.6-terra', 'grok-4.6'})
        self.assertFalse(auto['claude', 'claude-opus-9-1'])  # newer than the data: checked

    def test_free_usage_bonus_needs_a_calibrated_sufficient_model(self):
        snap = snapshot()
        # Unknown to the data (generic prior) and an estimated public row: both checked, neither earns the bonus.
        snap['accounts']['opencode']['models'] = ['opencode/zzz-new-free', 'opencode/nemotron-3.5-super-vl-free']
        accounts = inventory.apply(shipped_template(), snap)
        task = {'objective': 'Fix a failing unit test', 'work_kind': 'general_implementation', 'difficulty': 'routine',
                'hours': 1, 'needs': {'local_only': True}}
        decision = allocator.plan(task, {'read_ok': True, 'accounts': {}}, datetime.now(timezone.utc),
                                  accounts=accounts, overrides=[], host_policy_config=None)
        for bid in ('auto:opencode:opencode:zzz-new-free', 'auto:opencode:opencode:nemotron-3.5-super-vl-free'):
            free = next(c for c in decision['ranking'] if c['id'] == bid)
            self.assertFalse(free['excluded'], bid)
            self.assertNotIn('free_usage', free['components'], bid)
            self.assertNotEqual(decision['choice'], bid)

    def test_template_stays_free_of_personal_choices(self):
        document = shipped_template()
        bundles = [b for spec in document['accounts'].values() for b in spec.get('bundles', {}).values()]
        self.assertFalse([b for b in bundles if b.get('disabled')])
        self.assertFalse([name for name, spec in document['accounts'].items() if spec.get('model_defaults')])
        self.assertFalse([name for name, spec in document['accounts'].items() if spec.get('verified')])


class DiscoveredTierTests(unittest.TestCase):
    def setUp(self):
        self.home = temporary_root(self.addCleanup)
        self.now = datetime(2026, 11, 4, tzinfo=timezone.utc)

    def write(self, path, value):
        target = self.home / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value), encoding='utf-8')

    def discover(self):
        with patch.object(inventory, '_which', lambda name, path=None: '/bin/' + name if name in ('agy', 'opencode') else None):
            return inventory.discover(self.home, {'PATH': ''}, self.now)['accounts']

    def test_antigravity_quota_reading_proves_sign_in_and_installed_cli_alone_does_not(self):
        self.assertNotIn('usage-cache', self.discover()['antigravity']['sources'])
        self.assertFalse(inventory.launch_verified(self.discover()['antigravity'], False))
        self.write('.usage-float/usage-cache.json', {'providers': {'antigravity': {'windows': [
            {'id': 'gemini-weekly', 'used_pct': 25}]}}})
        row = self.discover()['antigravity']
        self.assertIn('usage-cache', row['sources'])
        self.assertTrue(inventory.launch_verified(row, False))

    def test_opencode_free_needs_no_sign_in(self):
        row = self.discover()['opencode']
        self.assertTrue(inventory.launch_verified(row, False))
        self.assertEqual(inventory.account_state_label(row), 'ready')

    def test_ready_chatgpt_web_bridge_reports_a_pro_seat(self):
        self.write('.codex-chatgpt-web-consult/config.json', {'mode': 'browser-only', 'browserInteractionMode': 'automatic',
                                                             'host': '127.0.0.1', 'port': 8790, 'proAvailable': False})
        self.assertNotIn('plan', self.discover()['chatgpt-web'])
        self.write('.codex-chatgpt-web-consult/config.json', {'mode': 'browser-only', 'browserInteractionMode': 'automatic',
                                                             'host': '127.0.0.1', 'port': 8790, 'proAvailable': True})
        self.assertEqual(self.discover()['chatgpt-web']['plan'], 'pro')


if __name__ == '__main__':
    unittest.main()
