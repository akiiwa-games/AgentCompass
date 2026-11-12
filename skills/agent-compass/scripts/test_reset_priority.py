"""Anticipated resets shorten a window's horizon; a user boost is scoped to its account, model and window and retires on a landed reset."""
import copy
from datetime import datetime, timedelta, timezone
import unittest

import allocator
import account_fleet
import quota_overrides
import quota_pools
from router_test_support import isolated_router_state, usage_windows


class ResetPriorityTests(unittest.TestCase):
    def setUp(self):
        isolated_router_state(self.addCleanup)
        self.now = datetime(2026, 10, 29, tzinfo=timezone.utc)
        self.accounts = account_fleet.load_accounts()
        self.task = {'work_kind': 'general_implementation', 'difficulty': 'hard', 'expected_hours': 0.5,
                     'needs': {'local_only': True}, 'disable_fast': True}
        self.usage = usage_windows(self.now, {
            'codex': [('usage', 90, 100)],
            'claude': [('five_hour', 90, 3), ('seven_day', 90, 100), ('weekly_fable', 100, 100)],
            'antigravity': [('gemini-5h', 90, 3), ('gemini-weekly', 90, 100), ('3p-5h', 90, 3), ('3p-weekly', 90, 100)],
        })

    def plan(self, usage=None, overrides=None, **task):
        return allocator.plan(dict(self.task, **task), usage or self.usage, self.now,
                              accounts=self.accounts, overrides=overrides or [])

    def row(self, plan, name='astra_high'):
        return next(r for r in plan['ranking'] if r['id'] == name)

    def rule(self, hours, **extra):
        return dict({'account': 'codex', 'model': 'gpt-6-astra', 'intent': 'panel:boost',
                     'until': (self.now + timedelta(hours=hours)).isoformat(), 'boost': .35}, **extra)

    def test_boost_applies_only_to_its_account_and_model_and_is_not_written_back(self):
        original = copy.deepcopy(self.usage)
        plan = self.plan(overrides=[self.rule(3, intent='user-priority')])
        self.assertEqual(self.row(plan)['components']['boost'], .35)
        self.assertNotIn('boost', self.row(plan, 'sol_high')['components'])
        # A boost tied to an expected reset gives nothing until that reset has come.
        for rule in (self.rule(3), self.rule(3, intent='legacy', stop_on_reset=True)):
            self.assertNotIn('boost', self.row(self.plan(overrides=[rule]))['components'])
        wrong = self.rule(3, window='unrelated')
        self.assertNotIn('boost', self.row(self.plan(overrides=[wrong]))['components'])
        self.assertEqual(self.usage, original)

    def test_expected_reset_shortens_the_horizon_and_cannot_extend_it(self):
        spec = copy.deepcopy(self.accounts['accounts']['codex'])
        spec.pop('observed', None)
        obs = self.usage['accounts']['codex']['windows'][0]
        effective = quota_pools.window_state('usage', spec['windows']['usage'], obs, spec, self.now, 24,
                                           expected_reset_at=self.now + timedelta(hours=2))
        self.assertEqual((effective['reset_hours'], effective['end_hours'], effective['remaining']), (2, 2, 90))
        for hours in (120,):
            later = quota_pools.window_state('usage', spec['windows']['usage'], obs, spec, self.now, 24,
                                           expected_reset_at=self.now + timedelta(hours=hours))
            self.assertEqual(later['reset_hours'], 100)
        unknown = dict(obs, seconds_until_reset=None, resets_at=None)
        fallback = quota_pools.window_state('usage', spec['windows']['usage'], unknown, spec, self.now, 24,
                                          expected_reset_at=self.now + timedelta(hours=2))
        self.assertTrue(fallback['known'])
        self.assertEqual(fallback['reset_hours'], 2)

    def test_replenishment_retires_priority_and_expected_time_cannot_refill(self):
        rule = self.rule(3, stop_on_reset=True, reset_confirmation='replenishment',
                         created_at=(self.now - timedelta(hours=1)).isoformat(),
                         baseline_windows={'usage': {'used_pct': 60,
                             'fetched_at': (self.now - timedelta(hours=1, minutes=1)).isoformat(),
                             'resets_at': (self.now + timedelta(hours=100)).isoformat()}})
        usage = copy.deepcopy(self.usage)
        usage['accounts']['codex'].update(status='fresh', fetched_at=(self.now-timedelta(minutes=1)).isoformat())
        self.assertTrue(quota_overrides.override_reset_landed(rule, usage, self.accounts, self.now))
        row = self.row(self.plan(usage, [rule]))
        self.assertNotIn('boost', row['components'])
        low = copy.deepcopy(self.usage)
        low['accounts']['codex']['windows'][0]['remaining_pct_before_reset'] = 5.1
        row = self.row(self.plan(low, [self.rule(1)], expected_hours=2))
        self.assertTrue(row['excluded'])
        self.assertIsNone(row['available_in_hours'])


if __name__ == '__main__':
    unittest.main()
