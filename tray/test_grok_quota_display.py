"""AC's single Grok bar is independent of which execution runner is allowed."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

import router_data

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills' / 'agent-compass' / 'scripts'))
import provider_inventory as real_inventory


class GrokQuotaDisplayTests(unittest.TestCase):
    def test_grok_or_bot_or_both_use_one_bar_and_one_refresh(self):
        now = datetime.now(timezone.utc)
        reset = (now + timedelta(days=2)).isoformat()
        for allowed in (['grok'], ['grok-bot'], ['grok', 'grok-bot'], []):
            with self.subTest(allowed=allowed):
                allocator, router = Mock(), Mock()
                inventory = Mock(quota_sources=real_inventory.quota_sources)
                # The fleet document declares the pool; nothing is pooled by name.
                allocator.load_accounts.return_value = {'quota_sources': {'grok-bot': 'grok'}, 'accounts': {
                    name: {'windows': {'credits': {'hours': 168}}, 'bundles': {}}
                    for name in ('grok', 'grok-bot')}, '_discovery': {'accounts': {
                    name: {'status': 'ready', 'family': name} for name in ('grok', 'grok-bot')}}}
                allocator.host_policy.load_policy.return_value = {'allowed_accounts': allowed}
                allocator.host_policy.account_allowed.side_effect = lambda policy, name: policy is None or name in policy['allowed_accounts']
                allocator.canonical_window_id.side_effect = lambda spec, wid: wid
                allocator.window_capacity_usd.return_value = None
                window = {'id': 'credits', 'remaining_pct_before_reset': 44,
                          'observed_remaining_pct': 44, 'resets_at': reset,
                          'seconds_until_reset': 172800, 'reset_time_passed': False}
                router.usage_snapshot.return_value = {'accounts': {
                    'grok': {'status': 'fresh', 'windows': [window]},
                    'grok-bot': {'status': 'stale', 'windows': [{**window, 'remaining_pct_before_reset': 99}]}}}
                with patch.object(router_data, 'modules', return_value=(allocator, router)), \
                     patch.dict(sys.modules, {'provider_inventory': inventory}), \
                     patch.object(router_data, '_read_json', return_value={}), \
                     patch.object(router_data, 'pool_reservations', return_value={}), \
                     patch.object(router_data.plugins, 'load', return_value={}), \
                     patch.object(router_data, 'collect_usage_now') as collect:
                    cards = router_data.account_cards()
                    self.assertEqual([c['account'] for c in cards], ['grok'] if allowed else [])
                    if allowed:
                        self.assertEqual(cards[0]['label'], 'Grok')
                        self.assertTrue(cards[0]['host_enabled'])
                        self.assertEqual(cards[0]['windows'][0]['remaining_pct'], 44)
                        self.assertEqual(cards[0]['windows'][0]['reset_in'], router_data.countdown(172800))
                        self.assertTrue(cards[0]['refreshable'])
                        router_data.refresh_usage(['grok-bot', 'grok'])
                        collect.assert_called_once_with(['grok'])


if __name__ == '__main__':
    unittest.main()
