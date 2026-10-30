"""Contract: only subscription accounts join automatic routing by default.

Metered (API) and unknown billing stay recognized and launchable, but are chosen
only by an explicit selection or a user's explicit bundle enable. Oh My Pi
(Paseo's built-in `omp` provider) is the first metered Paseo launcher.
"""
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch

import accounts_override
import allocator
import dispatch_registry
import host_policy
import provider_inventory as inventory

from router_test_support import isolated_router_state, temporary_root

BUNDLE = 'venice_glm52_high'
TASK = {'objective': 'Reply with exactly OK', 'hours': 0.1}


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup, discovery=False, policy=False)


def omp_accounts(billing='metered'):
    accounts = allocator.load_accounts(allocator.REFERENCES / 'accounts.json')
    spec = accounts['accounts']['omp']
    spec.update(billing=billing, verified=True)
    if billing is None:
        spec.pop('billing')  # an account entry alone is not consent to automatic routing
    return accounts


def omp_row(decision):
    return next(row for row in decision['ranking'] if row['id'] == BUNDLE)


class AutomaticRoutingTests(unittest.TestCase):
    def plan(self, accounts, constraints=None):
        return allocator.plan({**TASK, 'constraints': constraints or {}}, {}, accounts=accounts, overrides=[],
                              host_policy_config=None)

    def test_metered_and_unknown_billing_are_not_automatic(self):
        for billing, reason in (('metered', 'metered access is a last resort'),
                                ('unknown', 'unknown billing: explicit selection only'),
                                (None, 'unknown billing: explicit selection only')):
            with self.subTest(billing=billing):
                decision = self.plan(omp_accounts(billing))
                self.assertNotEqual(decision['choice'], BUNDLE)
                self.assertIn(reason, omp_row(decision)['excluded'])

    def test_explicit_model_selects_metered_omp_bundle(self):
        for model in ('glm-5.2', 'venice-api/zai-org-glm-5-2', BUNDLE):
            with self.subTest(model=model):
                decision = self.plan(omp_accounts(), {'model': model})
                self.assertEqual(decision['choice'], BUNDLE)
                self.assertEqual(decision['selected_bundle']['launch_model'], 'venice-api/zai-org-glm-5-2')

    def test_user_explicit_enable_opts_into_automatic_routing(self):
        accounts = omp_accounts('unknown')
        accounts_override.apply(accounts, {'accounts': {'omp': {'bundles': {BUNDLE: {'disabled': False}}}}})
        self.assertTrue(accounts['accounts']['omp']['bundles'][BUNDLE]['user_enabled'])
        self.assertNotIn('unknown billing: explicit selection only', omp_row(self.plan(accounts))['excluded'])

    def test_subscription_accounts_are_unchanged(self):
        decision = self.plan(omp_accounts('subscription'))
        self.assertFalse([r for r in omp_row(decision)['excluded'] if 'billing' in r or 'metered' in r])

    def test_dispatch_recheck_keeps_explicit_selection_for_pinned_bundle(self):
        # dispatch_registry.prepare re-plans a user_override with pinned_bundle and no model
        # constraint; the explicit choice must survive that recheck end to end.
        decision = allocator.plan({**TASK, 'pinned_bundle': BUNDLE}, {}, accounts=omp_accounts(), overrides=[],
                                  host_policy_config=None)
        self.assertFalse([r for r in omp_row(decision)['excluded'] if 'metered' in r or 'billing' in r])
        other = allocator.plan({**TASK, 'pinned_bundle': 'sol_high'}, {}, accounts=omp_accounts(), overrides=[],
                               host_policy_config=None)
        self.assertIn('metered access is a last resort', omp_row(other)['excluded'])


class OmpLaunchTests(unittest.TestCase):
    def test_paseo_launch_request_uses_omp_provider_mode_and_thinking(self):
        bundle = omp_accounts()['accounts']['omp']['bundles'][BUNDLE]
        request = dispatch_registry.launch_request(bundle, {'title': 't', 'initialPrompt': 'p'})
        self.assertEqual(request['provider'], 'omp/venice-api/zai-org-glm-5-2')
        self.assertEqual(request['settings'], {'modeId': 'full', 'thinkingOptionId': 'high'})


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.home = temporary_root(self.addCleanup)
        self.now = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)

    def discover(self, providers, omp_installed):
        path = self.home / '.paseo/config.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'agents': {'providers': providers}}), encoding='utf-8')
        with patch.object(inventory.shutil, 'which',
                          side_effect=lambda name, **kwargs: 'omp.exe' if omp_installed and name == 'omp' else None):
            return inventory.discover(self.home, {'PATH': ''}, self.now)

    def test_omp_is_metered_and_follows_paseo_switch(self):
        for label, providers, installed, status in (
            ('not configured', {}, True, 'disabled'),
            ('disabled', {'omp': {'enabled': False}}, True, 'disabled'),
            ('enabled, installed', {'omp': {'enabled': True}}, True, 'ready'),
            ('enabled, missing', {'omp': {'enabled': True}}, False, 'missing_client'),
        ):
            with self.subTest(case=label):
                row = self.discover(providers, installed)['accounts']['omp']
                self.assertEqual((row['family'], row['billing'], row['status']), ('omp', 'metered', status))

    def test_paseo_builtins_are_recognized_without_invented_models(self):
        snapshot = self.discover({'copilot': {'enabled': False}, 'muse': {'enabled': True}}, False)
        self.assertEqual(snapshot['accounts']['copilot']['status'], 'disabled')
        muse = snapshot['accounts']['muse']
        self.assertEqual((muse['family'], muse['billing'], muse['status']), ('muse', 'unknown', 'ready'))
        self.assertNotIn('models', muse)
        accounts = inventory.apply(allocator.load_accounts(allocator.reference_fleet_path()), snapshot)
        self.assertFalse((accounts['accounts'].get('muse') or {}).get('bundles'))

    def test_metered_ready_account_is_not_auto_enrolled_by_host_policy(self):
        policy = self.home / 'host-policy.json'
        policy.write_text(json.dumps({'allowed_accounts': ['codex'], 'known_accounts': ['codex']}), encoding='utf-8')
        accounts = omp_accounts()
        accounts['_discovery'] = {'accounts': {'omp': {'status': 'ready', 'family': 'omp'}}}
        with patch.dict(inventory.os.environ, {'ROUTER_HOST_POLICY': str(policy)}):
            self.assertEqual(host_policy.sync_discovered(accounts)['added'], [])
            self.assertFalse(host_policy.account_allowed(host_policy.load_policy(), 'omp'))


if __name__ == '__main__':
    unittest.main()
