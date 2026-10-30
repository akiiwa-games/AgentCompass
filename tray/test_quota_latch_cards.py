"""A per-window quota latch zeroes only that window on the card; the others show their reading."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

import router_data

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills' / 'agent-compass' / 'scripts'))
import provider_inventory as real_inventory


class QuotaLatchCardTests(unittest.TestCase):
    def card(self, row):
        allocator, router = Mock(), Mock()
        inventory = Mock(quota_sources=real_inventory.quota_sources)
        allocator.load_accounts.return_value = {'accounts': {'antigravity': {
            'windows': {w: {'hours': 5} for w in ('gemini-5h', '3p-weekly')}, 'bundles': {}}},
            '_discovery': {'accounts': {'antigravity': row}}}
        allocator.host_policy.load_policy.return_value = None
        allocator.host_policy.account_allowed.return_value = True
        allocator.canonical_window_id.side_effect = lambda spec, wid: wid
        allocator.window_capacity_usd.return_value = None
        reset = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        router.usage_snapshot.return_value = {'accounts': {'antigravity': {'status': 'fresh', 'windows': [
            {'id': 'gemini-5h', 'observed_remaining_pct': 99, 'resets_at': reset, 'seconds_until_reset': 10800},
            {'id': '3p-weekly', 'observed_remaining_pct': 0, 'resets_at': reset, 'seconds_until_reset': 10800}]}}}
        with patch.object(router_data, 'modules', return_value=(allocator, router)), \
             patch.dict(sys.modules, {'provider_inventory': inventory}), \
             patch.object(router_data, '_read_json', return_value={}), \
             patch.object(router_data, 'pool_reservations', return_value={}), \
             patch.object(router_data.router_priority, 'targets', return_value=[]), \
             patch.object(router_data.plugins, 'load', return_value={}):
            card = router_data.account_cards()[0]
        return card, {w['id']: w['remaining_pct'] for w in card['windows']}

    def test_one_latched_window_leaves_the_others_and_the_badge_alone(self):
        card, remaining = self.card({'family': 'antigravity', 'status': 'ready', 'exhausted_windows': ['3p-weekly']})
        self.assertEqual(remaining, {'gemini-5h': 99, '3p-weekly': 0.0})
        self.assertEqual((card['kind'], card['usage_status']), ('ok', 'fresh'))

    def test_account_wide_latch_still_zeroes_every_window(self):
        card, remaining = self.card({'family': 'antigravity', 'status': 'quota_exhausted'})
        self.assertEqual(remaining, {'gemini-5h': 0.0, '3p-weekly': 0.0})
        self.assertEqual(card['usage_status'], 'quota_exhausted')


if __name__ == '__main__':
    unittest.main()
