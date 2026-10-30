"""Effective catalogue and account-policy contracts; synthetic inputs only."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import allocator
import capability_catalog
import route_judge


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        self.accounts = {
            'models': {'custom-model': {'tier': 3.8, 'capabilities': {'coding': .8}}},
            'capability_policy': {'routes': {'custom-route': {'preferred': ['custom-model']}}},
        }

    def test_effective_models_and_routes_have_one_authority(self):
        before = deepcopy(self.accounts)
        policy = allocator.load_capabilities(accounts=self.accounts)
        self.assertEqual(policy['routes'], self.accounts['capability_policy']['routes'])
        self.assertEqual(policy['mainstream_defaults']['models'], self.accounts['models'])
        policy['mainstream_defaults']['models']['custom-model']['tier'] = 1
        self.assertEqual(self.accounts, before)
        self.accounts['models']['custom-model']['tier'] = 4.5
        self.assertEqual(allocator.load_capabilities(accounts=self.accounts)
                         ['mainstream_defaults']['models']['custom-model']['tier'], 4.5)

    def test_generated_views_follow_the_same_synthetic_source(self):
        views = capability_catalog.generated(self.accounts)
        self.assertIn('custom-model', views['model-capabilities.json'])
        self.assertIn('custom-route', views['model-capabilities.json'])
        self.assertIn('custom-model', views['model-capabilities.md'])
        self.assertIn('3.8', views['model-capabilities.md'])

    def test_only_explicit_state_restricts_route_models(self):
        route = {'preferred': ['another-model'], 'basis': 'Standing user order: old prose'}
        policy = {'routes': {'custom-route': route}}
        self.assertTrue(allocator.dot_may_take('custom-route', {}, policy))
        route['restrict_models'] = True
        self.assertFalse(allocator.dot_may_take('custom-route', {}, policy))
        route['restrict_models'] = False
        self.assertTrue(allocator.dot_may_take('custom-route', {}, policy))

    def test_route_peer_alignment_requires_explicit_configuration(self):
        accounts = {'models': {}, 'scoring': {}}
        policy = {'routes': {'custom-route': {'preferred': ['anchor', 'peer', 'other']}}}
        first = allocator.fit_score('custom-route', 'anchor', policy, accounts)
        self.assertLess(allocator.fit_score('custom-route', 'peer', policy, accounts), first)
        policy['route_fit_peers'] = {'peer': 'anchor'}
        self.assertEqual(allocator.fit_score('custom-route', 'peer', policy, accounts), first)

    def test_dispatch_cadence_uses_configured_account_and_threshold(self):
        now = datetime(2026, 10, 29, tzinfo=timezone.utc)
        records = [{'stage': 'record', 'choice': 'custom', 'at': now.isoformat(),
                    'bundle': {'account': 'workspace-account', 'provider': 'custom'}}] * 3
        self.assertIsNone(route_judge.account_cadence_alert(records, now, 'workspace-account', {}))
        spec = {'cadence': {'window_seconds': 3600, 'limit': 3}}
        self.assertIsNotNone(route_judge.account_cadence_alert(records, now, 'workspace-account', spec))
        self.assertIsNone(route_judge.account_cadence_alert(records, now, 'other-account', spec))
        spec['cadence']['limit'] = 4
        self.assertIsNone(route_judge.account_cadence_alert(records, now, 'workspace-account', spec))

    def test_remote_adapter_names_do_not_suppress_capacity_alerts(self):
        fake_allocator = SimpleNamespace(load_accounts=lambda: {'accounts': {}})
        for provider in ('dot', 'chatgpt-web', 'custom-remote'):
            with self.subTest(provider=provider), \
                    patch.object(route_judge, 'LEDGER_WRITES', False), \
                    patch.object(route_judge, 'append_ledger'), \
                    patch.object(route_judge, 'load_allocator', return_value=fake_allocator), \
                    patch.object(route_judge, 'capacity_alerts', return_value=[{'code': 'capacity-proof'}]) as alerts:
                result = {'stage': 'plan', 'selected_bundle': {
                    'provider': provider, 'account': 'workspace-account', 'expected_usd': 0}}
                route_judge.finalize(result, None, 'auto', 0, records=[])
                alerts.assert_called_once()
                self.assertIn('capacity-proof', [row['code'] for row in result['alerts']])


if __name__ == '__main__':
    unittest.main()
