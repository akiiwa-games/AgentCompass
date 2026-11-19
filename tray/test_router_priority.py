import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import router_priority as priority
from router_test_support import isolated_router_state

import account_fleet
import host_policy
import quota_overrides
import usage_reading


class PriorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'router-overrides.json'
        # Exercise the checkout's core, not a developer's installed skill copy.
        import allocator
        import route_judge
        self.a, self.r = allocator, route_judge
        isolated_router_state(self.addCleanup)
        self.now = datetime(2026, 10, 16, 11, tzinfo=timezone.utc)
        self.usage = {'accounts': {'codex': {'status': 'fresh', 'available': True,
            'fetched_at': (self.now-timedelta(minutes=1)).isoformat(),
            'windows': [{'id': 'usage', 'used_pct': 77, 'remaining_pct_before_reset': 23,
                         'resets_at': (self.now+timedelta(days=6)).isoformat()}]}}}
        for p in [patch.object(priority, 'modules', return_value=(self.a, self.r)),
                  patch.dict(os.environ, {'ROUTER_OVERRIDES': str(self.path)}),
                  patch.object(usage_reading, 'usage_snapshot', return_value=self.usage),
                  patch.object(host_policy, 'load_policy', return_value={'allowed_accounts': ['codex']})]:
            p.start(); self.addCleanup(p.stop)

    def enable(self, hours=24):
        return priority.enable('codex', 'gpt-6-astra', 'usage', hours, self.now)

    def rows(self):
        return json.loads(self.path.read_text(encoding='utf-8'))['overrides']

    def test_snapshot_reads_only_and_offers_scoped_non_disabled_targets(self):
        view = priority.snapshot(self.now)
        self.assertEqual(view['entries'], [])
        self.assertFalse(self.path.exists())
        self.assertTrue(view['targets'])
        self.assertEqual({t['account'] for t in view['targets']}, {'codex'})
        self.assertEqual(len(view['targets']), len({(t['account'], t['model'], t['window']) for t in view['targets']}))

    def test_open_captures_real_baseline_and_switching_hours_updates_deadline_without_stacking(self):
        self.assertIn('已开启', self.enable())
        row = self.rows()[0]; before = self.path.read_bytes()
        self.assertEqual(row['reset_confirmation'], 'replenishment')
        self.assertEqual(row['boost'], .35)
        self.assertEqual(row['baseline_windows']['usage']['fetched_at'], self.usage['accounts']['codex']['fetched_at'])
        self.assertEqual(row['baseline_windows']['usage']['used_pct'], 77)
        self.assertIn('未叠加', self.enable(24))
        self.assertEqual(before, self.path.read_bytes())
        self.assertIn('已更新', self.enable(1))
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['id'], row['id'])
        self.assertEqual(self.rows()[0]['until'], (self.now+timedelta(hours=1)).isoformat())
        self.assertEqual(self.rows()[0]['baseline_windows'], row['baseline_windows'])

    def test_countdown_follows_time_and_a_rule_without_boost_shows_no_bonus(self):
        import router_data
        with patch.object(router_data, 'modules', return_value=(self.a, self.r)):
            router_data.add_boost('codex', 'gpt-6-astra', hours=12, now=self.now)
            self.assertNotIn('boost', self.rows()[0])
            first = router_data.overrides(self.now)[0]
            later = router_data.overrides(self.now + timedelta(hours=11))[0]
        self.assertEqual((first['remaining_hours'], later['remaining_hours']), (12, 1))
        self.assertIsNone(first['boost'])
        self.assertNotIn('加成', priority.snapshot(self.now)['label'])
        # A countdown-only rule still retires at the existing quota safety margin.
        self.usage['accounts']['codex']['windows'][0].update(used_pct=96, remaining_pct_before_reset=4)
        self.assertEqual(priority.snapshot(self.now)['entries'], [])

    def test_new_short_and_half_day_menu_durations(self):
        self.assertEqual(priority.DURATIONS, (1, 3, 6, 12, 24))
        for hours in (1, 12):
            self.enable(hours)
            row = self.rows()[0]
            self.assertEqual(row['until'], (self.now + timedelta(hours=hours)).isoformat())
            priority.cancel(row['id'], priority.revision(row))

    def test_invalid_or_stale_observation_cannot_open_an_unconfirmable_rule(self):
        original = copy.deepcopy(self.usage)
        for changes in ({'status': 'stale'}, {'stale': True}, {'account': 'other'}, {'available': False},
                        {'fetched_at': (self.now-timedelta(minutes=16)).isoformat()},
                        {'fetched_at': (self.now+timedelta(seconds=1)).isoformat()}):
            self.usage['accounts']['codex'] = {**original['accounts']['codex'], **changes}
            with self.assertRaisesRegex(ValueError, '额度缺失'):
                self.enable()
            self.assertFalse(self.path.exists())
        self.usage['accounts']['codex'] = original['accounts']['codex']
        for value in (None, float('nan'), -1, 101, True):
            self.usage['accounts']['codex']['windows'][0].update(used_pct=value, remaining_pct_before_reset=None)
            with self.assertRaisesRegex(ValueError, '缺少有效额度'):
                self.enable()
            self.assertFalse(self.path.exists())

    def test_policy_and_catalog_rechecked_when_action_is_invoked(self):
        for args in [('claude', 'claude-sonnet-5-5', 'seven_day', 24),
                     ('codex', 'made-up-model', 'usage', 24), ('codex', 'gpt-6-astra', 'other', 24),
                     ('codex', 'gpt-6-astra', 'usage', 999)]:
            with self.assertRaises(ValueError):
                priority.enable(*args, now=self.now)
        self.assertFalse(self.path.exists())

    def test_existing_cli_rule_is_visible_and_not_duplicated_or_rebaselined(self):
        self.enable(); row = self.rows()[0]
        row.update(id='original', intent='user-explicit', window=None)
        self.path.write_text(json.dumps({'v': 1, 'overrides': [row]}), encoding='utf-8')
        before = self.path.read_bytes()
        view = priority.snapshot(self.now)
        self.assertEqual(view['entries'][0]['id'], 'original')
        self.assertTrue(next(t for t in view['targets'] if t['model']=='gpt-6-astra')['selected'])
        self.assertIn('已有临时规则', self.enable())
        self.assertEqual(before, self.path.read_bytes())

    def test_cancel_is_scoped_version_checked_and_backed_up(self):
        self.enable(); row = self.rows()[0]
        other = dict(row, id='another', model='gpt-6.1-sol')
        self.path.write_text(json.dumps({'v': 1, 'other_setting': 42, 'overrides': [row, other]}), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '已被其他操作更新'):
            priority.cancel(row['id'], 'stale-menu-version')
        self.assertEqual(len(self.rows()), 2)
        priority.cancel(row['id'], priority.revision(row))
        self.assertEqual(self.rows(), [other])
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['other_setting'], 42)
        self.assertEqual(len(list(self.path.parent.glob('backups/*/router-overrides.json'))), 1)
        self.assertIn('已关闭', priority.cancel(row['id'], priority.revision(row)))

    def test_router_owns_retirement_and_tray_observes_it_without_writing(self):
        self.enable(); row = self.rows()[0]; before = self.path.read_bytes()
        later = self.now+timedelta(minutes=1)
        self.usage['accounts']['codex']['fetched_at'] = later.isoformat()
        self.usage['accounts']['codex']['windows'][0].update(used_pct=0, remaining_pct_before_reset=100)
        view = priority.snapshot(later)
        self.assertEqual(view['label'], '已重置')
        self.assertEqual(len(self.rows()), 1)  # UI records fact; router still retires the rule.
        self.assertEqual(self.rows()[0]['reset_confirmed_at'], later.isoformat())
        self.usage['accounts']['codex']['status'] = 'stale'
        self.usage['accounts']['codex']['windows'][0]['used_pct'] = 99
        _, _, applied = quota_overrides.apply_overrides(self.rows(), self.usage, account_fleet.load_accounts(), later)
        self.assertEqual(applied, [])
        self.assertEqual(self.rows(), [])
        self.assertEqual(priority.snapshot(later)['label'], '已重置')
        self.assertEqual(priority.snapshot(later+timedelta(hours=12)-timedelta(seconds=1))['label'], '已重置')
        self.assertEqual(priority.snapshot(later+timedelta(hours=12))['label'], '重置前优先')

    def test_expiry_changes_display_without_writing_or_touching_other_state(self):
        self.enable(3); before = self.path.read_bytes()
        self.assertEqual(priority.snapshot(self.now+timedelta(hours=3))['entries'], [])
        self.assertEqual(self.path.read_bytes(), before)

    def test_malformed_file_is_not_silently_replaced(self):
        for value in ('{bad', '{"overrides": {}}', '{"overrides": [null]}'):
            self.path.write_text(value)
            with self.assertRaises(ValueError):
                self.enable()
            self.assertEqual(self.path.read_text(), value)

    def test_only_top_models_and_weekly_windows_are_offered(self):
        with patch.object(host_policy, 'load_policy', return_value=None):
            choices = priority.targets(account_fleet.load_accounts())
        self.assertEqual({t['model'] for t in choices}, {'gpt-6-astra', 'gemini-4-argon-high', 'claude-opus-5-5'})
        self.assertEqual({t['account'] for t in choices}, {'codex', 'antigravity', 'claude'})

    def test_a_target_stored_under_a_moved_name_reaches_the_bundle_of_its_row(self):
        """Rule (router_priority.targets): a target's model matches a bundle's as the same row (account_fleet.row_key),
        so a name whose maker moved it reaches the bundle that runs the row it means now."""
        decl = lambda m: {'model': m, 'kind': 'vendor', 'url': 'https://example.com/x', 'as_of': '2026-11-19', 'statement': 's'}
        accounts = account_fleet.load_accounts()
        accounts['models'] = {**accounts['models'], 'deepseek-v4-pro': {}, 'deepseek-v4-pro-0813': {}}
        accounts['provider_names'] = {**accounts['provider_names'], 'defaults': {
            **accounts['provider_names']['defaults'],
            'deepseek-ai/deepseek-v4-pro': decl('deepseek-v4-pro'),
            'deepseek-v4-pro': decl('deepseek-v4-pro-0813'),
            'astra-moved-name': decl('gpt-6-astra'),
        }}
        accounts['priority_targets'] = [{'account': 'codex', 'model': 'astra-moved-name', 'label': 'Moved'}]
        with patch.object(host_policy, 'load_policy', return_value=None):
            choices = priority.targets(accounts)
        self.assertEqual([(t['account'], t['model'], t['label']) for t in choices], [('codex', 'gpt-6-astra', 'Moved')])

    def test_a_bare_target_on_a_launcher_account_reads_as_its_bundle_does(self):
        """Rule (account_fleet.stored_model, round 3): on a launcher account (manual bundles, a launcher's catalog)
        derive reads names with no serving provider, so a stored bare name does too: a provider declaration keyed by
        the account's provider id does not turn the stored target away from the bundle it names."""
        accounts = account_fleet.load_accounts()
        accounts['provider_names'] = {**accounts['provider_names'], 'rows': {**accounts['provider_names']['rows'], 'codex': {
            'gpt-6-astra': {'model': None, 'status': 'other_weights', 'kind': 'community', 'url': 'https://example.com/x',
                            'as_of': '2026-11-19', 'statement': 's'}}}}
        # another spelling of the bundle's model, so the target is matched by resolution, not by the same text
        accounts['priority_targets'] = [{'account': 'codex', 'model': 'GPT-6-Astra', 'label': 'Astra'}]
        with patch.object(host_policy, 'load_policy', return_value=None):
            choices = priority.targets(accounts)
        self.assertEqual([(t['account'], t['model']) for t in choices], [('codex', 'gpt-6-astra')])

    def test_switch_model_is_atomic_and_failed_baseline_preserves_current_choice(self):
        self.enable(); before = self.path.read_bytes()
        with patch.object(host_policy, 'load_policy', return_value=None):
            target = next(t for t in priority.targets(account_fleet.load_accounts()) if t['model']=='gemini-4-argon-high')
            with self.assertRaises(ValueError):
                priority.enable(target['account'], target['model'], target['window'], 24, self.now)
            self.assertEqual(self.path.read_bytes(), before)
            obs = copy.deepcopy(self.usage['accounts']['codex'])
            obs['windows'][0]['id'] = target['window']
            self.usage['accounts']['antigravity'] = obs
            priority.enable(target['account'], target['model'], target['window'], 24, self.now)
        self.assertEqual([r['model'] for r in self.rows()], ['gemini-4-argon-high'])
        self.assertTrue(priority.snapshot(self.now)['label'].startswith('优先中：Argon'))

    def test_receipt_clock_does_not_slide_and_restart_can_read_it(self):
        self.enable()
        later = self.now+timedelta(minutes=1)
        self.usage['accounts']['codex']['fetched_at'] = later.isoformat()
        self.usage['accounts']['codex']['windows'][0]['used_pct'] = 0
        self.assertEqual(priority.snapshot(later)['label'], '已重置')
        before = self.path.read_bytes()
        self.assertEqual(priority.snapshot(later+timedelta(hours=11))['label'], '已重置')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(priority.snapshot(later+timedelta(hours=12))['label'], '重置前优先')
        self.assertEqual(self.path.read_bytes(), before)

    def test_router_without_tray_records_reset_and_manual_cancel_does_not(self):
        self.enable(); row = self.rows()[0]
        priority.cancel(row['id'], priority.revision(row))
        self.assertNotIn('last_reset', json.loads(self.path.read_text()))
        self.assertEqual(priority.snapshot(self.now)['label'], '重置前优先')
        self.enable()
        later = self.now+timedelta(minutes=1)
        self.usage['accounts']['codex']['fetched_at'] = later.isoformat()
        self.usage['accounts']['codex']['windows'][0]['used_pct'] = 0
        quota_overrides.apply_overrides(self.rows(), self.usage, account_fleet.load_accounts(), later)
        self.assertEqual(json.loads(self.path.read_text())['last_reset']['at'], later.isoformat())
        self.assertEqual(priority.snapshot(later)['label'], '已重置')
        self.assertEqual(priority.snapshot(later+timedelta(hours=12))['label'], '重置前优先')

    def test_receipt_does_not_overwrite_a_concurrent_edit_or_fake_a_reset_on_expiry(self):
        self.enable(); original = self.rows()[0]
        changed = dict(original, boost=.4)
        self.path.write_text(json.dumps({'overrides': [changed]}))
        before = self.path.read_bytes()
        quota_overrides.observe_landed_overrides({original['id']: original}, self.path, self.now)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(priority.snapshot(self.now+timedelta(hours=24))['label'], '重置前优先')
        self.assertNotIn('last_reset', json.loads(self.path.read_text()))

    def test_concurrent_clicks_create_exactly_one_priority(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            messages = list(pool.map(lambda _: self.enable(), range(4)))
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(sum('已开启' in m for m in messages), 1)

    def test_repository_id_and_moved_name_targets_keep_identity_selection_and_labels(self):
        """Rule (router_priority + pin_names_bundle): a repository-id target matches only its own bundle beside the
        moved bare spelling (-0813), stays selected and non-stacking across enable/snapshot, and a moved-name target
        keeps its configured label in active entries."""
        decl = lambda m: {'model': m, 'kind': 'vendor', 'url': 'https://example.com/x', 'as_of': '2026-11-19', 'statement': 's'}
        accounts = account_fleet.load_accounts()
        accounts['models'] = {**accounts['models'], 'deepseek-v4-pro': {}, 'deepseek-v4-pro-0813': {}}
        accounts['provider_names'] = {**accounts['provider_names'], 'defaults': {
            **accounts['provider_names']['defaults'],
            'deepseek-ai/deepseek-v4-pro': decl('deepseek-v4-pro'),
            'deepseek-v4-pro': decl('deepseek-v4-pro-0813'),
            'astra-moved-name': decl('gpt-6-astra'),
        }}
        accounts['accounts']['codex']['bundles'] = {
            **accounts['accounts']['codex']['bundles'],
            'auto:codex:codex:deepseek-ai/DeepSeek-V4-Pro': {
                'provider': 'codex', 'harness': 'codex', 'model': 'deepseek-ai/DeepSeek-V4-Pro',
                'launch_model': 'deepseek-ai/DeepSeek-V4-Pro', 'effort': 'high', 'draws': ['usage']},
            'auto:codex:codex:deepseek-v4-pro-0813': {
                'provider': 'codex', 'harness': 'codex', 'model': 'deepseek-v4-pro-0813',
                'launch_model': 'deepseek-v4-pro', 'effort': 'high', 'draws': ['usage']},
        }
        accounts['priority_targets'] = [
            {'account': 'codex', 'model': 'deepseek-ai/DeepSeek-V4-Pro', 'label': 'Repo V4 Pro'},
            {'account': 'codex', 'model': 'deepseek-v4-pro', 'label': 'Moved V4 Pro'},
            {'account': 'codex', 'model': 'astra-moved-name', 'label': 'Moved Astra'},
        ]
        with patch.object(account_fleet, 'load_accounts', return_value=accounts):
            choices = priority.targets(accounts)
            self.assertEqual([(t['model'], t['label']) for t in choices], [
                ('deepseek-ai/DeepSeek-V4-Pro', 'Repo V4 Pro'),
                ('deepseek-v4-pro-0813', 'Moved V4 Pro'),
                ('gpt-6-astra', 'Moved Astra'),
            ])
            self.assertIn('已开启', priority.enable('codex', 'deepseek-ai/DeepSeek-V4-Pro', 'usage', 24, self.now))
            self.assertEqual([r['model'] for r in self.rows()], ['deepseek-ai/DeepSeek-V4-Pro'])
            view = priority.snapshot(self.now)
            by_label = {t['label']: t['selected'] for t in view['targets']}
            self.assertEqual(by_label, {'Repo V4 Pro': True, 'Moved V4 Pro': False, 'Moved Astra': False})
            self.assertTrue(view['entries'][0]['label'].startswith('Repo V4 Pro · '))
            self.assertIn('未叠加', priority.enable('codex', 'deepseek-ai/DeepSeek-V4-Pro', 'usage', 24, self.now))
            self.assertIn('已更新', priority.enable('codex', 'deepseek-ai/DeepSeek-V4-Pro', 'usage', 6, self.now))
            self.assertEqual(len(self.rows()), 1)

            self.assertIn('已开启', priority.enable('codex', 'gpt-6-astra', 'usage', 12, self.now))
            self.assertEqual([r['model'] for r in self.rows()], ['gpt-6-astra'])
            moved_view = priority.snapshot(self.now)
            self.assertTrue(moved_view['entries'][0]['label'].startswith('Moved Astra · '))
            self.assertEqual({t['label']: t['selected'] for t in moved_view['targets']},
                             {'Repo V4 Pro': False, 'Moved V4 Pro': False, 'Moved Astra': True})

            # Without explicit `draws`, model-scoped weekly windows bind `deepseek-ai/DeepSeek-V4-Pro` to `w_repo`
            # (`deepseek-v4-pro`) and `deepseek-v4-pro` to `w_moved` (`deepseek-v4-pro-0813`) without double-resolving:
            split_accounts = json.loads(json.dumps(accounts))
            split_accounts['accounts']['codex']['windows'] = {
                'w_repo': {'hours': 168, 'models_except': ['deepseek-v4-pro-0813']},
                'w_moved': {'hours': 168, 'models': ['deepseek-v4-pro-0813']},
            }
            for bid in ('auto:codex:codex:deepseek-ai/DeepSeek-V4-Pro', 'auto:codex:codex:deepseek-v4-pro-0813'):
                split_accounts['accounts']['codex']['bundles'][bid].pop('draws', None)
            self.assertEqual([(t['label'], t['window']) for t in priority.targets(split_accounts)[:2]],
                             [('Repo V4 Pro', 'w_repo'), ('Moved V4 Pro', 'w_moved')])

            import router_data
            rows = [{
                'account': 'codex',
                'providers': ['codex'],
                'models': [
                    {'model': 'deepseek-v4-pro', 'model_label': 'Repo V4 Pro', 'enabled': True, 'partial': False,
                     'bundle_ids': ['b1'], 'bundle_refs': [], 'tags': [], 'auto': True, 'auto_stale': False,
                     'efforts_on': ['high'], 'efforts_off': []},
                    {'model': 'deepseek-v4-pro-0813', 'model_label': 'Moved V4 Pro', 'enabled': True, 'partial': False,
                     'bundle_ids': ['b2'], 'bundle_refs': [], 'tags': [], 'auto': True, 'auto_stale': False,
                     'efforts_on': ['high'], 'efforts_off': []},
                ],
            }]
            decorated = router_data.decorate_model_rows(rows, {'providers': {
                'codex': {'ok': True, 'type': 'paseo_provider', 'checked_at': 100.0,
                          'models': ['deepseek-ai/DeepSeek-V4-Pro']}
            }}, split_accounts)
            by_model = {g['model']: g['availability'] for g in decorated[0]['models']}
            self.assertEqual(by_model, {'deepseek-v4-pro': 'detected', 'deepseek-v4-pro-0813': 'missing'})


if __name__ == '__main__':
    unittest.main()
