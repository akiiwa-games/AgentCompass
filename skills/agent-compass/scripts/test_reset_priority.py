"""Natural and anticipated resets share time-dependent surplus economics."""
import copy
from datetime import datetime, timedelta, timezone
import unittest

import allocator
from router_test_support import isolated_router_state, usage_windows


class ResetPriorityTests(unittest.TestCase):
    def setUp(self):
        isolated_router_state(self.addCleanup)
        self.now = datetime(2026, 10, 29, tzinfo=timezone.utc)
        self.accounts = allocator.load_accounts()
        self.task = {'work_kind': 'general_implementation', 'difficulty': 'hard', 'expected_hours': 0.5,
                     'needs': {'local_only': True}, 'disable_fast': True}
        self.usage = usage_windows(self.now, {
            'codex': [('usage', 90, 100)],
            'claude': [('five_hour', 90, 3), ('seven_day', 90, 100), ('weekly_fable', 100, 100)],
            'antigravity': [('gemini-5h', 90, 3), ('gemini-weekly', 90, 100), ('3p-5h', 90, 3), ('3p-weekly', 90, 100)],
        })
        # Hold observed burn constant to isolate only time remaining.
        self.accounts['accounts']['codex']['observed'] = {'usage': {'pct_per_hour_24h': .1}}

    def plan(self, usage=None, overrides=None, **task):
        return allocator.plan(dict(self.task, **task), usage or self.usage, self.now,
                              accounts=self.accounts, overrides=overrides or [])

    def row(self, plan, name='astra_high'):
        return next(r for r in plan['ranking'] if r['id'] == name)

    def rule(self, hours, **extra):
        return dict({'account': 'codex', 'model': 'gpt-6-astra', 'intent': 'panel:boost',
                     'until': (self.now + timedelta(hours=hours)).isoformat(), 'boost': .35}, **extra)

    def test_natural_and_expected_bonus_and_score_increase_and_win(self):
        natural, expected = [], []
        original = copy.deepcopy(self.usage)
        for hours in (24, 12, 6, 3, 1):
            usage = copy.deepcopy(self.usage)
            usage['accounts']['codex']['windows'][0].update(
                seconds_until_reset=hours * 3600, resets_at=(self.now + timedelta(hours=hours)).isoformat())
            n = self.row(self.plan(usage))
            e_plan = self.plan(overrides=[self.rule(hours)])
            e = self.row(e_plan)
            natural.append((n['components']['reset_bonus'], n['score']))
            expected.append((e['components']['reset_bonus'], e['score']))
            self.assertEqual(natural[-1], expected[-1])
            self.assertNotIn('boost', e['components'])
            self.assertNotIn('reset_bonus', self.row(e_plan, 'sol_high')['components'])
            if hours <= 6:
                self.assertGreater(e['components']['reset_bonus'], .8)
                self.assertEqual(e_plan['selected_bundle']['account'], 'codex')
        for rows in (natural, expected):
            for before, after in zip(rows, rows[1:]):
                self.assertGreater(after[0], before[0])
                self.assertGreater(after[1], before[1])
        self.assertEqual(self.usage, original)

    def test_effective_time_shrinks_demand_without_changing_elapsed_burn(self):
        spec = copy.deepcopy(self.accounts['accounts']['codex'])
        spec.pop('observed', None)
        obs = self.usage['accounts']['codex']['windows'][0]
        p = self.accounts['scoring']
        natural = allocator.window_state('usage', spec['windows']['usage'], obs, spec, self.now, 24)
        effective = allocator.window_state('usage', spec['windows']['usage'], obs, spec, self.now, 24,
                                           expected_reset_at=self.now + timedelta(hours=2))
        self.assertEqual((effective['reset_hours'], effective['end_hours'], effective['remaining']), (2, 2, 90))
        n = allocator.demand_forecast(spec, 'usage', natural, p)
        e = allocator.demand_forecast(spec, 'usage', effective, p)
        self.assertEqual(n[2], e[2])
        self.assertAlmostEqual(e[0], n[0] * .02)
        for hours in (120,):
            later = allocator.window_state('usage', spec['windows']['usage'], obs, spec, self.now, 24,
                                           expected_reset_at=self.now + timedelta(hours=hours))
            self.assertEqual(later['reset_hours'], 100)
        unknown = dict(obs, seconds_until_reset=None, resets_at=None)
        fallback = allocator.window_state('usage', spec['windows']['usage'], unknown, spec, self.now, 24,
                                          expected_reset_at=self.now + timedelta(hours=2))
        self.assertTrue(fallback['known'])
        self.assertEqual(fallback['reset_hours'], 2)

    def test_expected_scoping_legacy_boost_and_fast_recalculation(self):
        for rule in (self.rule(3), self.rule(3, intent='legacy', stop_on_reset=True),
                     self.rule(3, intent='legacy', expected_reset_at=(self.now + timedelta(hours=3)).isoformat()),
                     self.rule(3, window='seven_day')):
            plan = self.plan(overrides=[rule], disable_fast=False)
            self.assertGreater(self.row(plan)['components'].get('reset_bonus', 0), .9)
            self.assertNotIn('reset_bonus', self.row(plan, 'sol_high')['components'])
        plain = self.rule(3, intent='user-priority')
        row = self.row(self.plan(overrides=[plain]))
        self.assertEqual(row['components']['boost'], .35)
        wrong = self.rule(3, window='unrelated')
        self.assertNotIn('reset_bonus', self.row(self.plan(overrides=[wrong]))['components'])

    def test_replenishment_retires_priority_and_expected_time_cannot_refill(self):
        rule = self.rule(3, stop_on_reset=True, reset_confirmation='replenishment',
                         created_at=(self.now - timedelta(hours=1)).isoformat(),
                         baseline_windows={'usage': {'used_pct': 60,
                             'fetched_at': (self.now - timedelta(hours=1, minutes=1)).isoformat(),
                             'resets_at': (self.now + timedelta(hours=100)).isoformat()}})
        usage = copy.deepcopy(self.usage)
        usage['accounts']['codex'].update(status='fresh', fetched_at=(self.now-timedelta(minutes=1)).isoformat())
        self.assertTrue(allocator.override_reset_landed(rule, usage, self.accounts, self.now))
        row = self.row(self.plan(usage, [rule]))
        self.assertNotIn('reset_bonus', row['components'])
        low = copy.deepcopy(self.usage)
        low['accounts']['codex']['windows'][0]['remaining_pct_before_reset'] = 5.1
        row = self.row(self.plan(low, [self.rule(1)], expected_hours=2))
        self.assertTrue(row['excluded'])
        self.assertIsNone(row['available_in_hours'])

    def test_surplus_cost_discount_and_stale_data(self):
        p = self.accounts['scoring']
        spec = self.accounts['accounts']['codex']
        state = {'remaining': 40, 'reset_hours': 1, 'end_hours': 1, 'short': False}
        forecast = (20, 10, 20, 'observed')
        near = allocator.window_cost(spec, 'usage', state, 2, forecast, p, {'notes': []}, self.now)
        stale = allocator.window_cost(spec, 'usage', dict(state, stale=True), 2, forecast, p, {'notes': []}, self.now)
        self.assertLess(near['opportunity_cost'], stale['opportunity_cost'])
        self.assertGreater(near['reset_bonus'], 0)
        self.assertNotIn('reset_bonus', stale)
        self.assertAlmostEqual(near['opportunity_cost'], stale['opportunity_cost'] * (1-near['reset_sunk_weight']), places=3)


if __name__ == '__main__':
    unittest.main()
