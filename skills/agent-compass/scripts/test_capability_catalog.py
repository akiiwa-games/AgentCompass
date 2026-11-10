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
            'models': {'custom-model': {}},
            'capability_policy': {'routes': {'custom-route': {'implies': {'visual': True}}}},
        }
        self.five = {'models': {'custom-model': {
            'tier': {'value': 3.8, 'method': 'arena_overall'}, 'usd_per_hour': {'value': 12.5},
            'image_input': {'status': 'ok'},
            'axes': {'divergence': {'value': 0.7, 'status': 'estimated'}, 'global_rigor': {'value': 0.8, 'status': 'measured'},
                     'module_precision': {'value': 0.6, 'status': 'partial'}, 'vision_tools': {'status': 'missing'}}}}}

    def test_the_route_policy_is_the_accounts_policy(self):
        before = deepcopy(self.accounts)
        policy = allocator.load_capabilities(accounts=self.accounts)
        self.assertEqual(policy['routes'], self.accounts['capability_policy']['routes'])
        policy['routes']['custom-route']['implies'] = {}
        self.assertEqual(self.accounts, before)

    def test_generated_views_follow_the_same_synthetic_source(self):
        views = capability_catalog.generated(self.accounts)
        self.assertIn('custom-route', views['model-capabilities.json'])
        md = capability_catalog.markdown(self.accounts, self.five)
        row = next(line for line in md.splitlines() if '`custom-model`' in line)
        self.assertIn('3.80', row)
        self.assertIn('12.5', row)

    def test_shipped_views_match_the_shipped_accounts(self):
        """Round 058 review F6: a merge can leave model-capabilities.json/.md behind accounts.json without a conflict;
        the shipped views must be what capability_catalog.py generates from the shipped accounts.json now."""
        import json
        accounts = json.loads((capability_catalog.REFERENCES / 'accounts.json').read_text(encoding='utf-8-sig'))
        for name, content in capability_catalog.generated(accounts).items():
            shipped = (capability_catalog.REFERENCES / name).read_text(encoding='utf-8')
            self.assertEqual(shipped, content, f'{name} is stale: run `python -X utf8 capability_catalog.py`')

    def test_estimated_and_partial_categories_are_marked_in_the_markdown_view(self):
        md = capability_catalog.markdown(self.accounts, self.five)
        row = next(line for line in md.splitlines() if '`custom-model`' in line)
        self.assertIn('0.70~ | 0.80 | 0.60! | missing |', row)

    def test_dot_takes_every_route_unless_the_work_is_local_or_visual(self):
        policy = {'routes': {'custom-route': {}}}
        self.assertTrue(allocator.dot_may_take('custom-route', {}, policy))
        self.assertFalse(allocator.dot_may_take('custom-route', {'local_only': True}, policy))
        self.assertFalse(allocator.dot_may_take('custom-route', {'visual': True}, policy))

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
