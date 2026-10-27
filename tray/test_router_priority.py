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


class PriorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'router-overrides.json'
        self.a, self.r = priority.modules()
        self.now = datetime(2026, 10, 16, 11, tzinfo=timezone.utc)
        self.usage = {'accounts': {'codex': {'status': 'fresh', 'available': True,
            'fetched_at': (self.now-timedelta(minutes=1)).isoformat(),
            'windows': [{'id': 'usage', 'used_pct': 77, 'remaining_pct_before_reset': 23,
                         'resets_at': (self.now+timedelta(days=6)).isoformat()}]}}}
        for p in [patch.dict(os.environ, {'ROUTER_OVERRIDES': str(self.path)}),
                  patch.object(self.r, 'usage_snapshot', return_value=self.usage),
                  patch.object(self.a.host_policy, 'load_policy', return_value={'allowed_accounts': ['codex']})]:
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

    def test_open_captures_real_baseline_and_duplicate_click_does_not_extend_or_stack(self):
        self.assertIn('已开启', self.enable())
        row = self.rows()[0]; before = self.path.read_bytes()
        self.assertEqual(row['reset_confirmation'], 'replenishment')
        self.assertEqual(row['boost'], .35)
        self.assertEqual(row['baseline_windows']['usage']['fetched_at'], self.usage['accounts']['codex']['fetched_at'])
        self.assertEqual(row['baseline_windows']['usage']['used_pct'], 77)
        self.assertIn('未叠加', self.enable(3))
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(row['until'], (self.now+timedelta(hours=24)).isoformat())

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
        _, _, applied = self.a.apply_overrides(self.rows(), self.usage, self.a.load_accounts(), later)
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
        with patch.object(self.a.host_policy, 'load_policy', return_value=None):
            choices = priority.targets(self.a, self.a.load_accounts())
        self.assertEqual([t['label'] for t in choices], ['Astra', 'Argon', 'Opus 5.5'])
        self.assertEqual([t['account'] for t in choices], ['codex', 'antigravity', 'claude'])

    def test_switch_model_is_atomic_and_failed_baseline_preserves_current_choice(self):
        self.enable(); before = self.path.read_bytes()
        with patch.object(self.a.host_policy, 'load_policy', return_value=None):
            target = next(t for t in priority.targets(self.a, self.a.load_accounts()) if t['label']=='Argon')
            with self.assertRaises(ValueError):
                priority.enable(target['account'], target['model'], target['window'], 24, self.now)
            self.assertEqual(self.path.read_bytes(), before)
            obs = copy.deepcopy(self.usage['accounts']['codex'])
            obs['windows'][0]['id'] = target['window']
            self.usage['accounts']['antigravity'] = obs
            priority.enable(target['account'], target['model'], target['window'], 24, self.now)
        self.assertEqual([r['model'] for r in self.rows()], ['gemini-4-argon-high'])
        self.assertEqual(priority.snapshot(self.now)['label'], '优先中：Argon')

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
        self.a.apply_overrides(self.rows(), self.usage, self.a.load_accounts(), later)
        self.assertEqual(json.loads(self.path.read_text())['last_reset']['at'], later.isoformat())
        self.assertEqual(priority.snapshot(later)['label'], '已重置')
        self.assertEqual(priority.snapshot(later+timedelta(hours=12))['label'], '重置前优先')

    def test_receipt_does_not_overwrite_a_concurrent_edit_or_fake_a_reset_on_expiry(self):
        self.enable(); original = self.rows()[0]
        changed = dict(original, boost=.4)
        self.path.write_text(json.dumps({'overrides': [changed]}))
        before = self.path.read_bytes()
        self.a.observe_landed_overrides({original['id']: original}, self.path, self.now)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(priority.snapshot(self.now+timedelta(hours=24))['label'], '重置前优先')
        self.assertNotIn('last_reset', json.loads(self.path.read_text()))

    def test_concurrent_clicks_create_exactly_one_priority(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            messages = list(pool.map(lambda _: self.enable(), range(4)))
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(sum('已开启' in m for m in messages), 1)


if __name__ == '__main__':
    unittest.main()
