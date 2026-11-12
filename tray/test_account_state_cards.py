"""The account list says each account's state, its reason and its last good reading."""
from pathlib import Path
import json
import sys
import unittest
from unittest.mock import Mock, patch

import router_data
import panel_quota

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills' / 'agent-compass' / 'scripts'))
import account_fleet
import axis_scores
import host_policy
import quota_economy
import quota_pools
import provider_inventory as real_inventory


class AccountStateCardTests(unittest.TestCase):
    def card(self, row, *, name='codex', spec=None, usage=None, allowed=True):
        fake, router = Mock(), Mock()
        inventory = Mock(quota_sources=real_inventory.quota_sources)
        account_spec = {'windows': {}, 'bundles': {}} if spec is None else spec
        fake.load_accounts.return_value = {'accounts': {name: account_spec},
                                                '_discovery': {'accounts': {name: row}}}
        fake.host_policy.load_policy.return_value = None
        fake.host_policy.account_allowed.return_value = allowed
        router.usage_snapshot.return_value = {'accounts': {}} if usage is None else {'accounts': {name: usage}}
        with patch.object(router_data, 'modules', return_value=(fake, router)), \
             patch.object(account_fleet, 'load_accounts', fake.load_accounts), \
             patch.object(host_policy, 'load_policy', fake.host_policy.load_policy), \
             patch.object(host_policy, 'account_allowed', fake.host_policy.account_allowed), \
             patch.object(axis_scores, 'five_axis_overlay', fake.five_axis_overlay), \
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
        self.assertEqual(panel_quota.card_title(card), 'Codex')
        self.assertEqual(panel_quota.quota_tooltip_text(card), 'Codex')

    def test_the_card_names_the_state_and_the_hover_gives_the_reason(self):
        for status, badge, title in (
                ('usage_failing', '读数失败：read_failed（上次成功 11-07 09:00）', 'Codex · 读数失败'),
                ('usage_failing', '读数失败，已排除派发：read_failed（上次成功 11-07 09:00）', 'Codex · 读数失败'),
                ('usage_failing', 'Usage reading failing, excluded from dispatch: read_failed (last good never)', 'Codex · Usage reading failing'),
                ('plan_inactive', '套餐失效，已排除派发：plan free', 'Codex · 套餐失效'),
                ('auth_failed', '登录被拒绝，已排除派发：x', 'Codex · 登录被拒绝'),
                ('agy_account_flapping', '账号来回切换，已排除派发：x', 'Codex · 账号来回切换')):
            with self.subTest(badge=badge):
                card = {'label': 'Codex', 'status': status, 'badge': badge, 'kind': 'warn'}
                self.assertEqual(panel_quota.card_title(card), title)
                self.assertIn(badge, panel_quota.quota_tooltip_text(card))

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

    def test_failing_reading_never_presents_old_window_as_fresh(self):
        fake, router = Mock(), Mock()
        inventory = Mock(quota_sources=real_inventory.quota_sources)
        fake.load_accounts.return_value = {
            'accounts': {'codex': {'windows': {'usage': {'hours': 168}}, 'bundles': {}}},
            '_discovery': {'accounts': {'codex': self.failing()}},
        }
        fake.host_policy.load_policy.return_value = None
        fake.host_policy.account_allowed.return_value = True
        fake.canonical_window_id.side_effect = lambda spec, wid: wid
        fake.capacity_usd.return_value = None
        router.usage_snapshot.return_value = {
            'accounts': {
                'codex': {
                    'status': 'fresh',
                    'fetched_at': '2026-11-07T01:00:00+00:00',
                    'observation_age_seconds': 3600,
                    'windows': [{'id': 'usage', 'observed_remaining_pct': 78.0, 'seconds_until_reset': 86400}],
                }
            }
        }
        with patch.object(router_data, 'modules', return_value=(fake, router)), \
             patch.object(account_fleet, 'load_accounts', fake.load_accounts), \
             patch.object(host_policy, 'load_policy', fake.host_policy.load_policy), \
             patch.object(host_policy, 'account_allowed', fake.host_policy.account_allowed), \
             patch.object(quota_pools, 'canonical_window_id', fake.canonical_window_id), \
             patch.object(quota_economy, 'capacity_usd', fake.capacity_usd), \
             patch.object(axis_scores, 'five_axis_overlay', fake.five_axis_overlay), \
             patch.dict(sys.modules, {'provider_inventory': inventory}), \
             patch.object(router_data, '_read_json', return_value={}), \
             patch.object(router_data, 'pool_reservations', return_value={}), \
             patch.object(router_data.router_priority, 'targets', return_value=[]), \
             patch.object(router_data.plugins, 'load', return_value={}):
            card = router_data.account_cards()[0]
        self.assertEqual(card['fetched_at'], '2026-11-07T01:00:00+00:00')
        self.assertEqual(card['usage_status'], 'stale')
        self.assertTrue(card['windows'][0]['stale'])
        self.assertIn('旧读数', panel_quota.card_read_stamp(card))

    def test_stale_but_available_reading_stays_ready_and_only_unavailable_upgrades_to_failing(self):
        fake, router = Mock(), Mock()
        inventory = Mock(quota_sources=real_inventory.quota_sources)
        ready_row = {'family': 'claude', 'identity': 'i', 'status': 'ready',
                     'last_success_at': '2026-11-08T14:00:00+00:00'}
        fake.load_accounts.return_value = {
            'accounts': {'claude-2': {'label': 'Claude 2', 'windows': {'seven_day': {'hours': 168}}, 'bundles': {}}},
            '_discovery': {'accounts': {'claude-2': ready_row}},
        }
        fake.host_policy.load_policy.return_value = None
        fake.host_policy.account_allowed.return_value = True
        fake.canonical_window_id.side_effect = lambda spec, wid: wid
        fake.capacity_usd.return_value = None
        # Case 1: Magpie gateway readAt > 10m sets stale=True, available=True, no error -> must stay ready ("就绪").
        router.usage_snapshot.return_value = {
            'accounts': {
                'claude-2': {
                    'status': 'stale',
                    'stale': True,
                    'available': True,
                    'fetched_at': '2026-11-08T14:00:00+00:00',
                    'observation_age_seconds': 660,
                    'windows': [{'id': 'seven_day', 'observed_remaining_pct': 74.0, 'seconds_until_reset': 70000}],
                }
            }
        }
        with patch.object(router_data, 'modules', return_value=(fake, router)), \
             patch.object(account_fleet, 'load_accounts', fake.load_accounts), \
             patch.object(host_policy, 'load_policy', fake.host_policy.load_policy), \
             patch.object(host_policy, 'account_allowed', fake.host_policy.account_allowed), \
             patch.object(quota_pools, 'canonical_window_id', fake.canonical_window_id), \
             patch.object(quota_economy, 'capacity_usd', fake.capacity_usd), \
             patch.object(axis_scores, 'five_axis_overlay', fake.five_axis_overlay), \
             patch.dict(sys.modules, {'provider_inventory': inventory}), \
             patch.object(router_data, '_read_json', return_value={}), \
             patch.object(router_data, 'pool_reservations', return_value={}), \
             patch.object(router_data.router_priority, 'targets', return_value=[]), \
             patch.object(router_data.plugins, 'load', return_value={}):
            card = router_data.account_cards()[0]
        self.assertEqual(card['status'], 'ready')
        self.assertEqual(card['badge'], '就绪')
        self.assertEqual(card['kind'], 'ok')
        self.assertEqual(panel_quota.card_title(card), 'Claude 2')
        self.assertEqual(card['usage_status'], 'stale')
        self.assertTrue(card['windows'][0]['stale'])

        # Case 2: available=False upgrades to usage_failing with localized text (never raw token 'stale_reading').
        router.usage_snapshot.return_value['accounts']['claude-2']['available'] = False
        with patch.object(router_data, 'modules', return_value=(fake, router)), \
             patch.object(account_fleet, 'load_accounts', fake.load_accounts), \
             patch.object(host_policy, 'load_policy', fake.host_policy.load_policy), \
             patch.object(host_policy, 'account_allowed', fake.host_policy.account_allowed), \
             patch.object(quota_pools, 'canonical_window_id', fake.canonical_window_id), \
             patch.object(quota_economy, 'capacity_usd', fake.capacity_usd), \
             patch.object(axis_scores, 'five_axis_overlay', fake.five_axis_overlay), \
             patch.dict(sys.modules, {'provider_inventory': inventory}), \
             patch.object(router_data, '_read_json', return_value={}), \
             patch.object(router_data, 'pool_reservations', return_value={}), \
             patch.object(router_data.router_priority, 'targets', return_value=[]), \
             patch.object(router_data.plugins, 'load', return_value={}):
            card_unavail = router_data.account_cards()[0]
        self.assertEqual(card_unavail['status'], 'usage_failing')
        self.assertEqual(panel_quota.card_title(card_unavail), 'Claude 2 · 读数失败')
        self.assertNotIn('stale_reading', card_unavail['badge'])
        self.assertIn('额度接口暂不可用', card_unavail['badge'])

    def test_five_axis_free_pool_capacity_unknown_shows_mode_b_warning_and_local_spend(self):
        fake, router = Mock(), Mock()
        inventory = Mock(quota_sources=real_inventory.quota_sources)
        fake.load_accounts.return_value = {
            'accounts': {
                'opencode': {
                    'label': 'OpenCode Zen',
                    'capacity_unknown': True,
                    'five_axis_free_pool': True,
                    'local_spend_usd': 1.85,
                    'local_spend_tokens': 42000,
                    'local_spend_source': 'opencode_db_token_list_price_lower_bound',
                    'windows': {},
                    'bundles': {},
                }
            },
            '_discovery': {'accounts': {'opencode': {'family': 'opencode', 'status': 'ready'}}},
        }
        fake.host_policy.load_policy.return_value = None
        fake.host_policy.account_allowed.return_value = True
        router.usage_snapshot.return_value = {'accounts': {}}
        with patch.object(router_data, 'modules', return_value=(fake, router)), \
             patch.object(account_fleet, 'load_accounts', fake.load_accounts), \
             patch.object(host_policy, 'load_policy', fake.host_policy.load_policy), \
             patch.object(host_policy, 'account_allowed', fake.host_policy.account_allowed), \
             patch.object(axis_scores, 'five_axis_overlay', fake.five_axis_overlay), \
             patch.dict(sys.modules, {'provider_inventory': inventory}), \
             patch.object(router_data, '_read_json', return_value={}), \
             patch.object(router_data, 'pool_reservations', return_value={}), \
             patch.object(router_data.router_priority, 'targets', return_value=[]), \
             patch.object(router_data.plugins, 'load', return_value={}):
            card = router_data.account_cards()[0]
        self.assertEqual(card['kind'], 'warn')
        self.assertTrue(card.get('capacity_unknown'))
        self.assertIn('不参与自动路由', card['badge'])
        self.assertIn('capacity_unknown', card['usage_note'])
        self.assertIn('$1.85', card['usage_note'])

    def test_plan_unknown_mode_b_card_shows_reason_and_excludes_auto_routing(self):
        fake, router = Mock(), Mock()
        inventory = Mock(quota_sources=real_inventory.quota_sources)
        reason = "unrecognized Antigravity tier 'standard-tier'"
        spec = {
            'label': 'Antigravity',
            'plan': 'unknown',
            'capacity_unknown': True,
            'plan_unknown_reason': reason,
            'windows': {'gemini-weekly': {'hours': 168}, 'gemini-5h': {'hours': 5}},
            'bundles': {},
        }
        fake.load_accounts.return_value = {
            'accounts': {'antigravity': spec},
            '_discovery': {'accounts': {'antigravity': {'family': 'antigravity', 'status': 'ready'}}},
        }
        fake.five_axis_overlay.side_effect = lambda doc, *a, **kw: doc
        fake.host_policy.load_policy.return_value = None
        fake.host_policy.account_allowed.return_value = True
        fake.canonical_window_id.side_effect = lambda s, wid: wid
        fake.capacity_usd.return_value = None
        router.usage_snapshot.return_value = {
            'accounts': {
                'antigravity': {
                    'status': 'ok',
                    'available': True,
                    'fetched_at': '2026-11-08T14:00:00+00:00',
                    'observation_age_seconds': 30,
                    'windows': [
                        {'id': 'gemini-weekly', 'observed_remaining_pct': 82.0, 'seconds_until_reset': 86400},
                        {'id': 'gemini-5h', 'observed_remaining_pct': 95.0, 'seconds_until_reset': 7200},
                    ],
                }
            }
        }
        with patch.object(router_data, 'modules', return_value=(fake, router)), \
             patch.object(account_fleet, 'load_accounts', fake.load_accounts), \
             patch.object(host_policy, 'load_policy', fake.host_policy.load_policy), \
             patch.object(host_policy, 'account_allowed', fake.host_policy.account_allowed), \
             patch.object(quota_pools, 'canonical_window_id', fake.canonical_window_id), \
             patch.object(quota_economy, 'capacity_usd', fake.capacity_usd), \
             patch.object(axis_scores, 'five_axis_overlay', fake.five_axis_overlay), \
             patch.dict(sys.modules, {'provider_inventory': inventory}), \
             patch.object(router_data, '_read_json', return_value={}), \
             patch.object(router_data, 'pool_reservations', return_value={}), \
             patch.object(router_data.router_priority, 'targets', return_value=[]), \
             patch.object(router_data.plugins, 'load', return_value={}):
            card = router_data.account_cards()[0]
        self.assertEqual(card['kind'], 'warn')
        self.assertTrue(card.get('capacity_unknown'))
        self.assertEqual(card.get('plan_unknown_reason'), reason)
        self.assertEqual(panel_quota.card_title(card), 'Antigravity · 套餐未知（Mode B）')
        self.assertIn('不参与自动路由', card['badge'])
        self.assertIn(reason, card['badge'])
        self.assertIn(reason, card['usage_note'])
        self.assertEqual(len(card['windows']), 2)

    def _usd_row(self, status='ready'):
        return {
            'family': 'openai', 'status': status, 'plan': 'metered USD',
            'billing': 'metered', 'backend_kind': 'metered_usd',
        }

    def _usd_spec(self):
        return {
            'label': 'USD', 'billing': 'metered', 'backend_kind': 'metered_usd',
            'verified': True, 'windows': {'balance': {}}, 'bundles': {},
        }

    def test_permission_denial_shows_no_number_and_the_hover_says_why(self):
        import i18n
        card = self.card(
            self._usd_row(), name='usd', spec=self._usd_spec(),
            usage={
                'backend_kind': 'metered_usd', 'available': False, 'status': 'stale',
                'windows': [], 'summary': None,
                'usage_details': {'kind': 'metered_usd', 'query': 'no_permission'},
            },
        )
        shown = json.dumps({
            'badge': card['badge'], 'usage_note': card['usage_note'], 'windows': card['windows'],
            'status': card['status'],
        }, ensure_ascii=False)
        self.assertNotEqual(card['status'], 'usage_failing')
        self.assertIsNone(card['usage_note'])
        self.assertEqual(card['windows'], [])
        self.assertNotIn('$', shown)
        self.assertNotIn('已用', shown)
        self.assertNotIn('读不到', shown)
        self.assertEqual(card['usage_hover'], '无查询权限')
        self.assertIn('无查询权限', panel_quota.quota_tooltip_text(card))
        self.assertEqual(i18n.english('无查询权限'), 'No permission to query usage')

    def test_a_read_failure_hover_is_the_reason_and_not_permission(self):
        reason = '/key/info 返回 500'
        card = self.card(
            self._usd_row(), name='usd', spec=self._usd_spec(),
            usage={
                'backend_kind': 'metered_usd', 'available': False, 'error': reason,
                'windows': [],
                'usage_details': {'kind': 'metered_usd', 'query': 'failed', 'read_error': reason},
            },
        )
        self.assertEqual(card['status'], 'usage_failing')
        self.assertEqual(card['usage_hover'], reason)
        tip = panel_quota.quota_tooltip_text(card)
        self.assertIn(reason, tip)
        self.assertNotIn('无查询权限', tip)
        self.assertNotIn('无查询权限', card['usage_hover'])

    def test_legacy_meter_badge_wins_over_manual_only(self):
        import i18n
        import state_upgrade
        card = self.card(
            self._usd_row('legacy_meter'), name='usd', spec=self._usd_spec(), allowed=False,
        )
        self.assertEqual(card['kind'], 'warn')
        self.assertIn('旧的额度字段残留', card['badge'])
        self.assertNotIn('仅手动使用', card['badge'])
        self.assertEqual(
            i18n.english(state_upgrade.LEGACY_METER_REASON),
            'Old quota fields remain. Run agentcompass setup to clear the old fields.',
        )
        self.assertNotIn('托盘', card['badge'])


if __name__ == '__main__':
    unittest.main()
