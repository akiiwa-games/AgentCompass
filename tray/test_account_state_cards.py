"""The account list says each account's state, its reason and its last good reading."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

import router_data
import router_window

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills' / 'agent-compass' / 'scripts'))
import provider_inventory as real_inventory


class AccountStateCardTests(unittest.TestCase):
    def card(self, row):
        allocator, router = Mock(), Mock()
        inventory = Mock(quota_sources=real_inventory.quota_sources)
        allocator.load_accounts.return_value = {'accounts': {'codex': {'windows': {}, 'bundles': {}}},
                                                '_discovery': {'accounts': {'codex': row}}}
        allocator.host_policy.load_policy.return_value = None
        allocator.host_policy.account_allowed.return_value = True
        router.usage_snapshot.return_value = {'accounts': {}}
        with patch.object(router_data, 'modules', return_value=(allocator, router)), \
             patch.dict(sys.modules, {'provider_inventory': inventory}), \
             patch.object(router_data, '_read_json', return_value={}), \
             patch.object(router_data, 'pool_reservations', return_value={}), \
             patch.object(router_data.router_priority, 'targets', return_value=[]), \
             patch.object(router_data.plugins, 'load', return_value={}):
            return router_data.account_cards()[0]

    def failing(self, **extra):
        return {'family': 'codex', 'identity': 'i', 'status': 'usage_failing', 'reason': 'read_failed: 更新失败',
                'last_success_at': '2026-11-07T01:00:00+00:00', 'usage_excluded': False, **extra}

    def test_a_failing_reading_shows_its_reason_and_last_good_reading(self):
        card = self.card(self.failing())
        self.assertEqual(card['kind'], 'warn')
        self.assertEqual((card['status'], card['status_reason']), ('usage_failing', 'read_failed: 更新失败'))
        self.assertEqual(card['last_success_at'], '2026-11-07T01:00:00+00:00')
        self.assertIn('read_failed: 更新失败', card['badge'])
        self.assertIn(router_data.read_time_text(card['last_success_at']), card['badge'])
        self.assertNotIn('已排除派发', card['badge'])

    def test_an_excluded_account_says_so(self):
        for row, words in ((self.failing(usage_excluded=True), '已排除派发'),
                           ({'family': 'codex', 'identity': 'i', 'status': 'plan_inactive', 'reason': 'plan free'}, '套餐失效'),
                           ({'family': 'codex', 'identity': 'i', 'status': 'auth_failed', 'reason': 'credentials_rejected_by_collector'}, '登录被拒绝')):
            with self.subTest(status=row['status']):
                card = self.card(row)
                self.assertEqual(card['kind'], 'warn')
                self.assertIn(words, card['badge'])
                self.assertIn(row['reason'], card['badge'])

    def test_no_good_reading_says_never(self):
        self.assertIn('从未', self.card(self.failing(last_success_at=None))['badge'])

    def test_ready_stays_quiet_on_the_card(self):
        card = {'label': 'Codex', 'status': 'ready', 'badge': '就绪', 'kind': 'ok'}
        self.assertEqual(router_window.card_title(card), 'Codex')
        self.assertEqual(router_window.quota_tooltip_text(card), 'Codex')

    def test_the_card_names_the_state_and_the_hover_gives_the_reason(self):
        for status, badge, title in (
                ('usage_failing', '读数失败：read_failed（上次成功 11-07 09:00）', 'Codex · 读数失败'),
                ('usage_failing', '读数失败，已排除派发：read_failed（上次成功 11-07 09:00）', 'Codex · 读数失败'),
                ('usage_failing', 'Usage reading failing, excluded from dispatch: read_failed (last good never)', 'Codex · Usage reading failing'),
                ('plan_inactive', '套餐失效，已排除派发：plan free', 'Codex · 套餐失效'),
                ('auth_failed', '登录被拒绝，已排除派发：x', 'Codex · 登录被拒绝')):
            with self.subTest(badge=badge):
                card = {'label': 'Codex', 'status': status, 'badge': badge, 'kind': 'warn'}
                self.assertEqual(router_window.card_title(card), title)
                self.assertIn(badge, router_window.quota_tooltip_text(card))

    def test_a_grace_period_account_can_still_take_work(self):
        dispatchable = real_inventory.account_state.dispatchable
        self.assertTrue(dispatchable({'status': 'usage_failing', 'usage_excluded': False}))
        self.assertFalse(dispatchable({'status': 'usage_failing', 'usage_excluded': True}))

    def test_usage_refused_402_account_card_shows_auto_retry_time(self):
        cached = {
            'error_status': 402,
            'error_code': 'deactivated_workspace',
            'error_at': '2026-11-08T04:00:00+00:00',
        }
        retry_at = real_inventory.account_state.usage_refused_retry_at(
            cached, poll={'seconds': 300, 'at': 1762574400.0}
        )
        self.assertEqual(retry_at, '2026-11-08T04:05:00+00:00')
        reason = real_inventory.account_state.usage_refused_reason(cached, retry_at=retry_at)
        self.assertIn('auto-retry at 2026-11-08T04:05:00+00:00', reason)
        card = self.card({
            'family': 'codex',
            'identity': 'i',
            'status': 'plan_inactive',
            'reason': reason,
            'plan_inactive_retry_at': retry_at,
        })
        self.assertEqual(card['kind'], 'warn')
        self.assertIn('套餐失效，已排除派发', card['badge'])
        self.assertIn('auto-retry at 2026-11-08T04:05:00+00:00', card['badge'])


if __name__ == '__main__':
    unittest.main()
