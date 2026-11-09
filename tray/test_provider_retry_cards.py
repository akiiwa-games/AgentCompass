"""An account (or model) held because its provider kept retrying without progress shows as unavailable in the account
list, with the provider's own message and when the router tries it again (2026-11-09, opencode free usage)."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

import router_data
import router_window

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills' / 'agent-compass' / 'scripts'))
import provider_inventory as real_inventory

MESSAGE = 'Free usage exceeded, subscribe to Go'


class ProviderRetryCardTests(unittest.TestCase):
    def card(self, holds, row=None):
        allocator, router = Mock(), Mock()
        inventory = Mock(quota_sources=real_inventory.quota_sources)
        allocator.load_accounts.return_value = {'accounts': {'opencode': {'bundles': {}}},
                                                '_discovery': {'accounts': {'opencode': row or {
                                                    'family': 'opencode', 'status': 'ready'}}}}
        allocator.host_policy.load_policy.return_value = None
        allocator.host_policy.account_allowed.return_value = True
        allocator.provider_retry.active.return_value = holds
        router.usage_snapshot.return_value = {'accounts': {}}
        with patch.object(router_data, 'modules', return_value=(allocator, router)), \
             patch.dict(sys.modules, {'provider_inventory': inventory}), \
             patch.object(router_data, '_read_json', return_value={}), \
             patch.object(router_data, 'pool_reservations', return_value={}), \
             patch.object(router_data.router_priority, 'targets', return_value=[]), \
             patch.object(router_data.plugins, 'load', return_value={}):
            return router_data.account_cards()[0]

    def test_an_account_hold_marks_the_ready_account_unavailable_with_the_provider_message(self):
        card = self.card({'opencode': {'account': 'opencode', 'model': None, 'message': MESSAGE, 'retry_at': None}})
        self.assertEqual(card['kind'], 'warn')
        self.assertTrue(card['retry_hold'])
        self.assertIn(MESSAGE, card['badge'])
        self.assertIn('整个账号已排除派发', card['badge'])
        self.assertIn('手动清除', card['badge'])   # no retry time: it waits for a success or a clear
        self.assertNotEqual(router_window.card_title(card), card['label'])   # the card itself says it is in trouble

    def test_a_model_hold_names_the_model_and_its_next_retry(self):
        retry = datetime.now(timezone.utc) + timedelta(minutes=4)
        card = self.card({'opencode/glm-5.3-flash-free': {'account': 'opencode', 'model': 'glm-5.3-flash-free',
                                                          'message': MESSAGE, 'retry_at': retry.isoformat()}})
        self.assertEqual(card['kind'], 'warn')
        self.assertIn(MESSAGE, card['badge'])
        self.assertIn(retry.astimezone().strftime('%m-%d %H:%M'), card['badge'])

    def test_no_hold_leaves_the_card_alone_and_another_problem_keeps_its_text(self):
        card = self.card({})
        self.assertEqual((card['kind'], card['retry_hold']), ('ok', False))
        failing = self.card({'opencode': {'account': 'opencode', 'model': None, 'message': MESSAGE}},
                            row={'family': 'opencode', 'status': 'auth_failed', 'reason': 'credentials_rejected'})
        self.assertIn('credentials_rejected', failing['badge'])
        self.assertIn(MESSAGE, failing['badge'])


def test_the_model_list_marks_a_held_model_and_badges_a_held_account(tmp_path, monkeypatch):
    from test_model_catalog import settings
    import provider_retry
    target = settings(tmp_path, monkeypatch)
    store = target.parent / provider_retry.FILE
    monkeypatch.setenv('ROUTER_PROVIDER_RETRY', str(store))
    monkeypatch.setattr(router_data, 'account_badge', lambda *args: ('就绪', 'ok'))
    at = datetime.now(timezone.utc)
    bundle = {'account': 'codex', 'provider': 'codex', 'model': 'gpt-6.1-sol'}
    entry = provider_retry.record(bundle, {'failure_code': 'provider_retry_stuck', 'provider_message': MESSAGE,
                                           'at': at.isoformat()},
                                  dispatch_id='d1', classify=lambda *args: ('model', {'source': 'system_one'}))
    row = next(row for row in router_data.harness_model_rows() if row['harness'] == 'codex')
    sol = next(model for model in row['models'] if model['model'] == 'gpt-6.1-sol')
    retry = datetime.fromisoformat(entry['retry_at']).astimezone().strftime('%m-%d %H:%M')
    assert sol['launch_unavailable'] == f'提供方持续重试，已排除派发，下次重试 {retry}'
    assert not target.exists()   # a hold is not a setting
    provider_retry.record({**bundle, 'model': 'gpt-6.1-terra'}, {'failure_code': 'provider_retry_stuck',
                          'provider_message': MESSAGE, 'at': at.isoformat()},
                          dispatch_id='d2', classify=lambda *args: ('account', {'source': 'system_one'}))
    row = next(row for row in router_data.harness_model_rows() if row['harness'] == 'codex')
    assert row['kind'] == 'warn' and MESSAGE in row['badge']
    assert json.loads(store.read_text(encoding='utf-8'))['entries']['codex']['retry_at'] is None


if __name__ == '__main__':
    unittest.main()
