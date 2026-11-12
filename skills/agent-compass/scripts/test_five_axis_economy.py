"""Quota economy v3 (round 13; advisor report v3 with the owner's D1-D3): the cost of a candidate is the integral of the
window's quota price over its own spend, N = F - cost, the price comes from the account's own value ladder and the period's
own need probability, substitutes enter only through their own slack, and no 5-hour window ever prices."""
import copy
from datetime import datetime, timedelta, timezone
import math
import os
import tempfile
import random
import sys
import unittest
from unittest.mock import patch

import allocator
import axis_scores
import candidate_eval
import candidate_ranking
import ledger_snapshot
import quota_economy
import quota_pools
import model_latch
import provider_retry
import route_simulation as sim
from router_test_support import isolated_router_state
from test_fleet_parity import shipped_template, snapshot


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup, discovery=False, policy=False)


NOW = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)
WEEK = 168.0
PROFILE = {'reasoning_depth': 2, 'code_complexity': 2, 
           'needs_visual': False, 'needs_gui': False, 'local_only': True, 'wants_fast': False, 'read_only_review': False, 'interaction_style': 'engineering', 'multi_task': False,
           'task_count': '1', 'expected_hours': 1.0}
MID, FRONTIER, CREATIVE = (2, 3, 2, 0), (2, 3, 3, 0), (3, 1, 0, 0)


def rec_of(capacity=1000.0, used_frac=0.5, elapsed=0.5, reset_hours=None, period=WEEK, lump=0.02):
    """A window record as `FiveAxisEconomy.record` returns it; `lump` is the burst size as a share of the capacity (the
    shipped fleet's 2 h task is about 1.8 % of a Max20 week)."""
    return {'capacity_usd': capacity, 'used_usd': capacity * used_frac, 'elapsed_frac': elapsed,
            'line_now_usd': capacity * elapsed, 'period_hours': period, 'lump_usd': capacity * lump,
            'reset_hours': (1 - elapsed) * period if reset_hours is None else reset_hours, 'unstarted': False}


def ladder_for(spend_scale=1.0, with_outside=0.0):
    """A realistic ladder: three models (cheap weak, middle, dear strong) over the real demand grid and panel z-score targets."""
    anchor_targets = axis_scores.load_five_axis()['anchor']['targets']
    models = [(0.29 * spend_scale, {'divergence': -2.2, 'coding': -1.8, 'aesthetics': -1.9, 'tool_operation': {'responsiveness': 1.0, 'image_recognition': -1.5}}),
              (12.59 * spend_scale, {'divergence': -0.8, 'coding': -0.6, 'aesthetics': -0.6, 'tool_operation': {'responsiveness': 0.2, 'image_recognition': -0.5}}),
              (22.86 * spend_scale, {'divergence': 0.0, 'coding': 0.0, 'aesthetics': 0.0, 'tool_operation': {'responsiveness': 0.0, 'image_recognition': 0.0}})]
    hulls = []
    for d in quota_economy.FIVE_AXIS_DEMAND_GRID:
        targets = axis_scores.five_axis_targets(d, anchor_targets)
        pts = [(s, axis_scores.five_axis_fit(sc, d, targets)[0]) for s, sc in models]
        hulls.append(quota_economy.five_axis_upper_hull(pts, with_outside * max(f for _, f in pts)))
    return quota_economy.five_axis_value_ladder(hulls)


class Fleet:
    """Claude Max20 and Codex Pro on the shipped public data, each window on its own clock; the cheap Haiku and Luna lines
    are added as bundles so the ladders have the same three rungs the advisor's probe had."""

    def __init__(self):
        self.accounts = sim.fresh_accounts(shipped_template(), snapshot())

    def plan(self, claude_used, codex_used, ec=0.5, ex=0.5, axes=MID, hours=2.0, allowed=('claude', 'codex'), claude2=None,
             five_hour_used=0.05, mutate=None, codex_reading=True, claude_long=True, usage_hook=None, local_only=True, **task):
        acc = copy.deepcopy(self.accounts)
        cl, cx = acc['accounts']['claude']['bundles'], acc['accounts']['codex']['bundles']
        for key in [k for k in cx if k.startswith('pi_')]:
            cx.pop(key)
        cl['haiku55_high'] = {**cl['sonnet55_high'], 'model': 'claude-haiku-5-5'}
        cx['terra_high'] = {**cx['sol_high'], 'model': 'gpt-6.1-terra'}
        cx['luna_high'] = {**cx['sol_high'], 'model': 'gpt-6-luna'}
        if mutate:
            mutate(acc)
        usage = sim.snapshot(sim.synthetic_document(acc, NOW, sim.STATES['ample']), acc, NOW)

        def week(used, e):
            return {'remaining_pct_before_reset': 100.0 * (1 - used), 'seconds_until_reset': (1 - e) * WEEK * 3600}
        usage['accounts']['claude']['windows'] = [
            {'id': 'seven_day', **week(claude_used, ec)}, {'id': 'weekly_fable', **week(claude_used, ec)},
            {'id': 'five_hour', 'remaining_pct_before_reset': 100.0 * (1 - five_hour_used), 'seconds_until_reset': 5 * 3600}]
        if not claude_long:
            usage['accounts']['claude']['windows'] = usage['accounts']['claude']['windows'][2:]
        usage['accounts']['codex']['windows'] = [{'id': 'usage', **week(codex_used, ex)}] if codex_reading else []
        if claude2 is not None:
            used2, e2 = claude2
            usage['accounts']['claude-2']['windows'] = [
                {'id': 'seven_day', **week(used2, e2)},
                {'id': 'five_hour', 'remaining_pct_before_reset': 100.0 * (1 - five_hour_used), 'seconds_until_reset': 5 * 3600}]
        if 'codex' not in allowed:
            usage['accounts'].pop('codex', None)
        if usage_hook:
            usage_hook(usage)
        profile = {**PROFILE, 'axis_demand': dict(zip(axis_scores.FIVE_AXES, axes)), 'expected_hours': hours, 'local_only': local_only}
        pub = copy.deepcopy(axis_scores.load_public_user_scores())
        pub.setdefault('scores', {}).setdefault('claude-haiku-5-5', {})['coding.faithfulness'] = {
            'level': 1.0, 'as_of': '2026-11-11', 'kind': 'maintainer',
        }
        with patch.object(axis_scores, 'load_public_user_scores', return_value=pub):
            return allocator.plan({'objective': 't', 'hours': hours, 'profile': profile, **task}, usage, NOW, accounts=acc,
                                  overrides=[], records=[], host_policy_config={'allowed_accounts': list(allowed)})


def eligible(decision):
    return [c for c in decision['ranking'] if not c.get('excluded') and c.get('score') is not None]


def row(decision, bundle_id, fast=False):
    return next(c for c in eligible(decision) if (c.get('base_id') or c['id']) == bundle_id and bool(c['bundle'].get('fast')) == fast)


def price_of(decision, account):
    return next(c['components']['quota_price'] for c in eligible(decision) if c['bundle']['account'] == account)


class NeedTailTests(unittest.TestCase):
    def test_the_incomplete_beta_matches_known_values(self):
        beta = quota_economy.regularized_incomplete_beta
        self.assertAlmostEqual(beta(2.509, 4.763, 0.4312), 0.7053, places=3)      # the advisor's worked tail
        self.assertAlmostEqual(beta(1.0, 1.0, 0.3), 0.3, places=9)                  # uniform
        self.assertAlmostEqual(beta(3.0, 1.0, 0.5), 0.125, places=9)                # x^a when b = 1
        self.assertAlmostEqual(beta(1.0, 4.0, 0.2), 1 - 0.8 ** 4, places=9)
        self.assertEqual(beta(2.0, 3.0, 0.0), 0.0)
        self.assertEqual(beta(2.0, 3.0, 1.0), 1.0)
        self.assertAlmostEqual(beta(7.0, 2.0, 0.3) + beta(2.0, 7.0, 0.7), 1.0, places=9)
        self.assertEqual(beta(4000.0, 3.0, 0.4), 0.0)       # underflow side

    def test_on_the_line_the_mean_demand_to_come_equals_the_remaining_capacity(self):
        """Advisor test 5 / v3.1 test 4 (line identity): at U = e C the predicted demand still to come,
        (U + q)(1 - e)/(e + s) with s = q / C, is exactly C - U for every burst size q, so the recovery line is where the
        expected demand just fills the period. With the v3.1 burst size the distribution is close to symmetric: pi on the
        line is within 0.45 to 0.65, below one half well under the line and above it a little way over it."""
        rng = random.Random(7)
        for _ in range(25):
            cap, e, share = rng.uniform(5.0, 5000.0), rng.uniform(0.05, 0.95), rng.uniform(0.005, 0.1)
            q = share * cap
            rec = rec_of(cap, e, e, lump=share)
            s_ = q / cap
            self.assertAlmostEqual((e * cap + q) * (1 - e) / (e + s_), cap - e * cap, places=9)
            # the integer-lump mean of the continuous-extension tail is the negative-binomial mean: q * sum_r P(K >= r)
            mean = q * sum(quota_economy.five_axis_need_tail(rec, r * q) for r in range(1, int(8 * cap / q)))
            self.assertAlmostEqual(mean / (cap * (1 - e)), 1.0, delta=0.01)
            self.assertLess(quota_economy.five_axis_need_probability(rec_of(cap, 0.5 * e, e, lump=share), 0.5 * e * cap), 0.5)
            self.assertGreater(quota_economy.five_axis_need_probability(rec_of(cap, min(1.0, e + 0.15), e, lump=share), (e + 0.15) * cap), 0.5)

    def test_the_tail_grows_with_use_falls_toward_the_reset_and_is_a_probability(self):
        previous = -1.0
        for u in range(0, 100, 2):
            p = quota_economy.five_axis_need_probability(rec_of(1000.0, u / 100, 0.5), u * 10.0)
            self.assertTrue(0.0 <= p <= 1.0)
            self.assertGreaterEqual(p, previous - 1e-12)
            previous = p
        late = [quota_economy.five_axis_need_probability(rec_of(1000.0, 0.5, e), 500.0) for e in (0.2, 0.5, 0.8, 0.95, 0.999)]
        self.assertEqual(late, sorted(late, reverse=True))
        self.assertLess(late[-1], 0.05)
        self.assertEqual(quota_economy.five_axis_need_tail(rec_of(1000.0, 0.5, 1.0), 100.0), 0.0)     # the reset is now
        self.assertEqual(quota_economy.five_axis_need_tail(rec_of(1000.0, 0.5, 0.5), 0.0), 1.0)       # no room left: sure

    def test_a_slack_reads_only_its_own_window(self):
        tight, loose = rec_of(1000.0, 0.95, 0.5), rec_of(1000.0, 0.05, 0.5)
        self.assertLess(quota_economy.five_axis_substitute_slack(tight, 10.0), 0.1)
        self.assertGreater(quota_economy.five_axis_substitute_slack(loose, 10.0), 0.6)
        self.assertEqual(quota_economy.five_axis_substitute_slack(tight, 60.0), 0.0)        # the task does not even fit


class PriceTests(unittest.TestCase):
    def test_no_step_at_the_line_and_a_positive_price_below_it(self):
        """Advisor test 4: scan the use at a fixed clock; the cost of a fixed spend rises strictly, is positive under the line
        and the step across the line is no more than twice its neighbours; at a fixed use it falls to zero toward the reset."""
        ladder, spend, costs = ladder_for(), 22.86, []
        for u in range(1, 100):
            costs.append(quota_economy.five_axis_spend_cost(rec_of(1704.5, u / 100, 0.5), ladder, spend, 1.0)['cost'])
        self.assertTrue(all(b > a for a, b in zip(costs, costs[1:])))
        self.assertGreater(costs[20], 0.0)           # 21% used at the half-way clock: below the line, still not free
        steps = [b - a for a, b in zip(costs, costs[1:])]
        across = steps[49]                           # 50% -> 51%
        self.assertLessEqual(across, 2 * max(steps[48], steps[50]))
        later = [quota_economy.five_axis_spend_cost(rec_of(1704.5, 0.6, e), ladder, spend, 0.5 * (1 - e) * WEEK)['cost']
                 for e in (0.3, 0.5, 0.7, 0.9, 0.99, 0.9999)]     # the task ends before the reset each time
        self.assertEqual(later, sorted(later, reverse=True))
        self.assertLess(later[-1], 0.01)

    def test_units_do_not_matter_and_the_task_length_cancels(self):
        """Advisor test 6: scaling every dollar leaves the cost unchanged; scaling the task's hours and spends scales the
        price by 1/h so h * lambda is the same."""
        base_cost = quota_economy.five_axis_spend_cost(rec_of(1704.5, 0.6, 0.5), ladder_for(), 22.86, 1.0)
        for k in (0.01, 3.0, 250.0):
            scaled = quota_economy.five_axis_spend_cost(rec_of(1704.5 * k, 0.6, 0.5), ladder_for(k), 22.86 * k, 1.0)
            self.assertAlmostEqual(scaled['cost'], base_cost['cost'], places=4)
            self.assertAlmostEqual(scaled['lambda_start'] * k, base_cost['lambda_start'], places=6)
        for hours in (0.5, 2.0, 40.0):       # h times the task's hours: the spends scale with h
            lam = quota_economy.five_axis_spend_cost(rec_of(1704.5, 0.6, 0.5), ladder_for(hours), 22.86 * hours, hours)['lambda_start']
            self.assertAlmostEqual(lam * hours, base_cost['lambda_start'], places=6)

    def test_the_cost_table_agrees_with_direct_integration(self):
        ladder, rec = ladder_for(), rec_of(1704.5, 0.6, 0.5)
        for spend in (0.29, 3.0, 22.86, 137.15, 457.0):
            got = quota_economy.five_axis_spend_cost(rec, ladder, spend, 1.0, spend_max=457.0)['cost']
            n, direct = 4000, 0.0
            for i in range(n):
                direct += quota_economy.five_axis_quota_price(rec, ladder, rec['used_usd'] + (i + 0.5) * spend / n) * spend / n
            self.assertAlmostEqual(got, direct, delta=max(2e-4, 0.01 * direct))

    def test_a_task_across_the_reset_pays_its_second_part_on_a_fresh_period(self):
        """Advisor test 10 (the post-reset share): 2h to the reset in a 6h task, a third of the spend before it."""
        ladder, spend, hours = ladder_for(), 137.0, 6.0
        rec = rec_of(1704.5, 0.6, 1 - 2 / WEEK, reset_hours=2.0)
        got = quota_economy.five_axis_spend_cost(rec, ladder, spend, hours, spend, {})
        before = quota_economy.five_axis_spend_cost({**rec, 'reset_hours': math.inf}, ladder, spend / 3, hours)['cost']
        fresh = {'capacity_usd': 1704.5, 'used_usd': 0.0, 'elapsed_frac': 0.0, 'reset_hours': math.inf,
                 'lump_usd': rec['lump_usd']}
        after = quota_economy.five_axis_spend_cost(fresh, ladder, spend * 2 / 3, hours)['cost']
        self.assertAlmostEqual(got['spend_before'], spend / 3, places=6)
        self.assertAlmostEqual(got['cost'], before + after, delta=2e-3)
        self.assertLess(got['need_start'], 0.25)         # 2h to the reset: the first dollars mostly lapse
        # the same 60% used with a full week left prices all of it in the old period and is dearer
        week_left = quota_economy.five_axis_spend_cost(rec_of(1704.5, 0.6, 0.5), ladder, spend, hours)['cost']
        self.assertGreater(week_left, got['cost'])

    def test_different_clocks_give_different_prices_for_the_same_use(self):
        """Advisor test 10 (desync)."""
        ladder = ladder_for()
        costs = {e: quota_economy.five_axis_spend_cost(rec_of(1704.5, 0.6, e), ladder, 45.0, 2.0)['cost'] for e in (0.2, 0.5, 0.8)}
        self.assertEqual(len({round(v, 6) for v in costs.values()}), 3)
        self.assertGreater(costs[0.2], costs[0.5])
        self.assertGreater(costs[0.5], costs[0.8])

    def test_the_outside_option_lowers_the_price_and_only_down_to_its_own_certainty(self):
        """Advisor test 2 (the limit): an outside certain to have room that does the work at the full fit makes the price
        zero, none at all gives the highest price, and anything between is in between."""
        rec = rec_of(1704.5, 0.6, 0.5)
        prices = [quota_economy.five_axis_spend_cost(rec, ladder_for(with_outside=v), 22.86, 2.0)['cost'] for v in (0.0, 0.3, 0.6, 1.0)]
        self.assertEqual(prices, sorted(prices, reverse=True))
        self.assertEqual(prices[-1], 0.0)
        self.assertGreater(prices[0], 0.0)

    def test_the_upper_hull_and_the_ladder(self):
        hull = quota_economy.five_axis_upper_hull([(1.0, 0.4), (3.0, 0.45), (4.0, 0.7), (9.0, 0.7), (2.0, 0.39)], 0.1)
        self.assertEqual(hull[0], (0.0, 0.1))
        self.assertEqual(hull[-1], (4.0, 0.7))        # the cheapest of the highest fits
        slopes = [(b[1] - a[1]) / (b[0] - a[0]) for a, b in zip(hull, hull[1:])]
        self.assertEqual(slopes, sorted(slopes, reverse=True))      # concave
        self.assertEqual(quota_economy.five_axis_upper_hull([(1.0, 0.3)], 0.5), [])
        ladder = quota_economy.five_axis_value_ladder([hull, hull])
        self.assertEqual(ladder['steps'][0][0], 1.0)
        self.assertEqual([s for _, s in ladder['steps']], sorted(s for _, s in ladder['steps']))
        self.assertTrue(all(0 < r <= 1 for r, _ in ladder['steps']))
        self.assertEqual(quota_economy.five_axis_value_ladder([[(0.0, 0.5)]])['steps'], [])

    def test_the_outside_value_walks_the_alternatives_by_fit(self):
        self.assertEqual(quota_economy.five_axis_outside_value([], 0.6), 0.0)
        self.assertAlmostEqual(quota_economy.five_axis_outside_value([(0.5, 1.0), (0.6, 1.0)], 0.6), 0.6)
        self.assertAlmostEqual(quota_economy.five_axis_outside_value([(0.7, 0.5), (0.4, 1.0)], 0.6), 0.5 * 0.6 + 0.5 * 0.4)
        self.assertAlmostEqual(quota_economy.five_axis_outside_value([(0.7, 0.0)], 0.6), 0.0)


class FleetEconomyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fleet = Fleet()

    def test_two_tight_windows_do_not_discount_each_other_to_zero(self):
        """Advisor test 1 (collapse): Claude 60% / Codex 90% at half time, then both 90%. Both windows price, the over-line
        Codex candidates pay, and the over-line Codex Sol is not chosen (round 11 chose it at cost 0)."""
        for used in ((0.6, 0.9), (0.9, 0.9)):
            d = self.fleet.plan(*used)
            self.assertGreater(price_of(d, 'claude'), 0.0, used)
            self.assertGreater(price_of(d, 'codex'), 0.0, used)
            for c in eligible(d):
                if c['bundle']['account'] == 'codex' and c['components']['fit'] > 0.55:
                    self.assertGreater(c['components']['opportunity_cost'], 0.0, c['id'])
        # Monotonicity (C rises with used share): raising Codex's used share with all else equal never lowers a
        # candidate's C and never raises an over-line candidate's rank. Asserted as a property, not as ids.
        self.assert_used_share_monotone(lambda used: self.fleet.plan(0.6, used), (0.6, 0.75, 0.9))

    def assert_used_share_monotone(self, plan_at, levels, account='codex'):
        """Raising `account`'s used share never lowers any candidate's C, and an over-line candidate on that pool
        (need probability at the end of its run above one half: past the line) never climbs past a candidate on
        another pool. Siblings on the same tight pool may reorder among themselves, so the rank is taken relative
        to the other pools."""
        prev = None
        for used in levels:
            d = plan_at(used)
            rows = eligible(d)
            cost = {c['id']: c['components']['opportunity_cost'] for c in rows}
            pos = {c['id']: i for i, c in enumerate(rows)}
            mine = {c['id'] for c in rows if c['bundle']['account'] == account}
            over = {c['id'] for c in rows if c['id'] in mine and (c.get('economy') or {}).get('need_end_max', 0) > 0.5}
            if prev is not None:
                p_cost, p_pos, p_mine, p_over = prev
                for cid in cost.keys() & p_cost.keys():
                    self.assertGreaterEqual(cost[cid] + 1e-9, p_cost[cid], (cid, used))
                for x in p_over & pos.keys():
                    for y in (pos.keys() & p_pos.keys()) - mine - p_mine:
                        if p_pos[y] < p_pos[x]:
                            self.assertLess(pos[y], pos[x], (x, 'climbed past', y, used))
            prev = (cost, pos, mine, over)
        self.assertTrue(prev[3], 'the fixture must put at least one candidate over the line')

    def test_a_substitute_only_helps_as_much_as_it_is_likely_to_have_room(self):
        """Advisor test 2: Codex fixed at 90%, Claude swept; Codex's price never falls as Claude fills and at 100% equals
        the price with no substitute at all."""
        prices = [price_of(self.fleet.plan(u, 0.9), 'codex') for u in (0.1, 0.3, 0.5, 0.6, 0.9)]
        self.assertEqual(prices, sorted(prices))
        self.assertLess(prices[0], 0.5 * prices[-1])
        alone = price_of(self.fleet.plan(0.5, 0.9, allowed=('codex',)), 'codex')
        full = self.fleet.plan(1.0, 0.9)         # Claude is out of room: excluded, so it cannot be anyone's outside point
        self.assertTrue(all(c['bundle']['account'] != 'claude' for c in eligible(full)))
        self.assertAlmostEqual(price_of(full, 'codex'), alone, places=9)
        self.assertGreater(price_of(self.fleet.plan(0.99, 0.9), 'codex'), prices[-1] * 0.98)

    def test_identical_windows_get_the_same_positive_price(self):
        """Advisor test 3 (symmetry): claude and claude-2 on the same plan, same use, same clock."""
        def same_plan(acc):
            acc['accounts']['claude-2']['plan'] = acc['accounts']['claude']['plan']
            acc['accounts']['claude-2']['windows'] = copy.deepcopy(acc['accounts']['claude']['windows'])
            three = {k: v for k, v in acc['accounts']['claude']['bundles'].items()
                     if v['model'] in ('claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5-5') and v['effort'] == 'high'}
            acc['accounts']['claude']['bundles'] = three
            acc['accounts']['claude-2']['bundles'] = {f'claude-2__{k}': {**v, 'account': 'claude-2'} for k, v in three.items()}
        d = self.fleet.plan(0.9, 0.9, allowed=('claude', 'claude-2'), claude2=(0.9, 0.5), mutate=same_plan)
        one, two = price_of(d, 'claude'), price_of(d, 'claude-2')
        self.assertGreater(one, 0.0)
        self.assertAlmostEqual(one, two, places=9)

    def test_speed_only_breaks_ties_on_n_and_a_faster_candidate_never_displaces_a_higher_n(self):
        """Owner decision of round 13b: the post-ranking speed rule is gone. The order is N, then wait, then the
        earliest-expiring quota, then the faster completion, then fewer USD. (1) A fast variant is its own candidate at its
        real spend; 2h before the weekly reset the cost of both is below N's resolution, so the fast one wins on time (round 15). (2) The under-line mid-week case keeps a plain variant on top instead of Opus+fast.
        (3) Where the quota costs nothing the fast variant and its base tie on N and the faster one wins by completion time."""
        sprint = self.fleet.plan(0.6, 0.5, ec=1 - 2 / WEEK)
        for c in eligible(sprint):
            self.assertNotEqual(c['components'].get('decided_by'), 'speed')
        opus = row(sprint, 'opus55_high')
        fast = row(sprint, 'opus55_high', fast=True)
        # (1) 2h to the weekly reset with 40% left: with the v3.1 burst size nothing of the week is likely to be needed,
        # so both variants cost less than the 4-place resolution of N, tie on N, and the fast one wins on completion time
        for c in (opus, fast):
            self.assertLess(c['components']['opportunity_cost'], 5e-5)
        self.assertEqual(round(opus['score'], 4), round(fast['score'], 4))
        self.assertAlmostEqual(opus['components']['fit'], fast['components']['fit'])
        self.assertGreater(fast['expected_usd'], opus['expected_usd'])
        self.assertLess(fast['expected_hours'], opus['expected_hours'])
        self.assertEqual(fast['components']['decided_by'], 'completion_hours')
        self.assertEqual(sprint['choice'], fast['id'])
        # (2) well under the line (20% used at half time) the week is not likely to be needed either (v3.1: the line is
        # where pi is about one half, the curve is narrow), so the best candidate costs less than the resolution of N; on
        # the line (50% used) the quota has a price again and a plain variant stays above Opus+fast
        under = self.fleet.plan(0.2, 0.5, ec=0.5)
        self.assertLess(eligible(under)[0]['components']['opportunity_cost'], 5e-5)
        mid_week = self.fleet.plan(0.5, 0.5, ec=0.5)
        top = mid_week['ranking'][0]
        self.assertFalse(top['bundle'].get('fast'))
        self.assertGreater(top['score'], row(mid_week, 'opus55_high', fast=True)['score'])
        self.assertEqual([c['score'] for c in eligible(mid_week)], sorted((c['score'] for c in eligible(mid_week)), reverse=True))
        # (3) where the quota costs nothing (2h before the weekly reset, plenty left) the fast variant ties its base on N
        # and the faster one wins by completion time
        free = self.fleet.plan(0.6, 0.5, allowed=('claude',), ec=1 - 2 / WEEK)
        plain, quick = row(free, 'opus55_high'), row(free, 'opus55_high', fast=True)
        self.assertLess(plain['components']['opportunity_cost'], 5e-5)
        self.assertEqual(round(plain['score'], 4), round(quick['score'], 4))
        self.assertLess(quick['expected_hours'], plain['expected_hours'])
        order = [c['bundle'].get('fast', False) for c in eligible(free) if (c.get('base_id') or c['id']) == 'opus55_high']
        self.assertEqual(order, [True, False])
        self.assertEqual(quick['components']['decided_by'], 'completion_hours')

    def test_the_order_is_n_then_wait_then_speed_then_usd_with_no_earliest_reset_tiebreak(self):
        def cand(score, wait=0.0, reset=None, hours=1.0, usd=1.0, level=0.5, place=0):
            return {'score': score, 'available_in_hours': wait, 'expected_hours': hours, 'duration_hours': hours, 'expected_usd': usd,
                    'components': {'earliest_reset_hours': reset, 'five_axis_level': level, 'harness_place': place}}
        base = cand(0.5, reset=10.0)
        for better, worse in ((cand(0.5001, reset=99.0), base),
                              (cand(0.5, wait=0.0, reset=99.0), cand(0.5, wait=2.0, reset=1.0)),
                              (cand(0.5, reset=99.0, hours=0.5, usd=9.0), base),
                              (cand(0.5, reset=99.0, usd=0.5), base),
                              (cand(0.5, level=0.8), base),
                              (cand(0.5, place=0), cand(0.5, place=1))):
            self.assertLess(candidate_ranking.five_axis_order(better), candidate_ranking.five_axis_order(worse))
        # R2-F4: earliest_reset_hours never separates two candidates; reset proximity acts only through C inside N.
        self.assertEqual(candidate_ranking.five_axis_order(cand(0.5, reset=1.0)), candidate_ranking.five_axis_order(cand(0.5, reset=99.0)))
        self.assertEqual(candidate_ranking.five_axis_order(base), candidate_ranking.five_axis_order(cand(0.5, reset=None)))
        self.assertEqual(candidate_ranking.five_axis_deciding_key(cand(0.5, reset=10.0, hours=0.5), base), 'completion_hours')
        self.assertNotIn('earliest_reset_hours', candidate_ranking.FIVE_AXIS_ORDER_KEYS)

    def test_fable_5_1_cannot_beat_opus_5_5(self):
        """Advisor test 8, without the speed rule: at equal fit Fable 5.1 trails Opus 5.5 on N, and in the creative task, where
        their fits are equal, nothing but N, the earliest expiring quota or speed could separate them."""
        d = self.fleet.plan(0.3, 0.5, axes=FRONTIER, hours=6.0)
        opus, fable = row(d, 'opus55_high'), row(d, 'fable51_high')
        self.assertGreaterEqual(opus['components']['fit'], fable['components']['fit'])
        self.assertGreater(opus['score'], fable['score'])
        ids = [c['id'] for c in eligible(d)]
        self.assertLess(ids.index('opus55_high'), ids.index('fable51_high'))
        creative = self.fleet.plan(0.3, 0.5, axes=(2, 1, 0, 0), hours=0.3)
        o, f = row(creative, 'opus55_high'), row(creative, 'fable51_high')
        self.assertAlmostEqual(o['components']['fit'], f['components']['fit'])
        ids = [c['id'] for c in eligible(creative) if not c['bundle'].get('fast')]
        self.assertLess(ids.index('opus55_high'), ids.index('fable51_high'))

    def test_a_pool_whose_reading_is_missing_is_no_substitute_and_is_named(self):
        """Advisor test 9: Codex's reading is gone. Its candidates leave automatic routing with a surfaced data error, it is
        nobody's outside point, and Claude's price is the one it has with no Codex at all."""
        d = self.fleet.plan(0.6, 0.9, codex_reading=False)
        gone = [c for c in d['ranking'] if c['bundle']['account'] == 'codex']
        self.assertTrue(gone)
        for c in gone:
            self.assertTrue(c['excluded'])
            self.assertIn(candidate_eval.QUOTA_READING_UNAVAILABLE, c['excluded'][0])
        self.assertTrue(any(candidate_eval.QUOTA_READING_UNAVAILABLE in w for w in d['decision']['warnings']))
        alone = self.fleet.plan(0.6, 0.9, allowed=('claude',))
        self.assertEqual(price_of(d, 'claude'), price_of(alone, 'claude'))
        self.assertEqual(d['choice'], alone['choice'])

    def test_a_five_hour_window_never_enters_a_tail_a_ladder_or_a_price(self):
        """The owner's D3: none of the economics use 5-hour windows. Whatever a 5-hour window reads, no tail, price or ladder is
        computed on it, its row is unpriced, and the answer is the same."""
        seen = []
        real = quota_economy.five_axis_need_tail

        def spy(rec, room):
            seen.append(rec.get('period_hours'))
            return real(rec, room)
        with patch.object(quota_economy, 'five_axis_need_tail', spy):
            relaxed = self.fleet.plan(0.6, 0.9, five_hour_used=0.05)
            squeezed = self.fleet.plan(0.6, 0.9, five_hour_used=0.7)
        self.assertTrue(seen)
        self.assertTrue(all(p is None or p >= 24.0 for p in seen), set(seen))
        for d in (relaxed, squeezed):
            for c in eligible(d):
                cap = c.get('capacity') or {}
                for w in (c.get('economy') or {}).get('windows', ()):
                    self.assertNotEqual(w['window'], 'five_hour')
                if cap.get('window') == 'five_hour':
                    self.assertFalse(cap['priced'])
                    self.assertEqual(cap['cost'], 0.0)
        self.assertEqual([(c['id'], c['score']) for c in eligible(relaxed) if c['bundle']['account'] == 'codex'],
                         [(c['id'], c['score']) for c in eligible(squeezed) if c['bundle']['account'] == 'codex'])
        for a, b in zip(eligible(relaxed), eligible(squeezed)):
            if a['bundle']['account'] == 'claude' and a['id'] == b['id']:
                self.assertEqual(a['components']['opportunity_cost'], b['components']['opportunity_cost'])

    def test_a_window_that_never_resets_is_priced_on_use_alone_and_serialises(self):
        """A starter allowance (no reset) is a window whose clock never runs: the price depends on its use only and the
        plan stays valid JSON (no infinite hours leak into the ranking)."""
        import json

        def starter(acc):
            acc['accounts']['codex']['windows'] = {'usage': {'hours': None}}
        a = self.fleet.plan(0.6, 0.5, ex=0.1, mutate=starter)
        b = self.fleet.plan(0.6, 0.5, ex=0.9, mutate=starter)
        json.dumps(a['ranking'], allow_nan=False)
        json.dumps(ledger_snapshot.decision_snapshot(a), allow_nan=False)
        sol = row(a, 'sol_high')
        self.assertIsNone(sol['economy']['windows'][0]['reset_in_hours'])
        self.assertGreater(sol['components']['opportunity_cost'], 0.0)
        self.assertEqual(sol['components']['opportunity_cost'], row(b, 'sol_high')['components']['opportunity_cost'])

    def test_the_snapshot_carries_what_the_page_reads(self):
        d = self.fleet.plan(0.6, 0.9)
        snap = ledger_snapshot.decision_snapshot(d)
        rows = [r for r in snap['ranking'] if r.get('economy')]
        self.assertTrue(rows)
        top = rows[0]
        self.assertIn('decided_by', top['economy'])
        window = top['economy']['windows'][0]
        for key in ('window', 'quota_source', 'capacity_usd', 'used_usd', 'elapsed_hours', 'reset_in_hours', 'spend_usd', 'cost',
                    'need_start', 'need_end', 'quota_price'):
            self.assertIn(key, window, key)
        blocks = snap['decision']['system']['quota_windows']
        self.assertEqual({b['window'] for b in blocks}, {'seven_day', 'weekly_fable', 'usage'})
        for key in ('label', 'capacity_usd', 'used_usd', 'elapsed_hours', 'reset_in_hours', 'need_probability',
                    'quota_price', 'substitute_slack', 'outside_fit'):
            self.assertIn(key, blocks[0], key)
        self.assertEqual(next(b for b in blocks if b['window'] == 'usage')['capacity_usd'], 615.5)
        import json
        json.dumps(snap, allow_nan=False)


class PriceIndependenceTests(unittest.TestCase):
    """Owner decisions of round 14: a window's price depends on the account's own windows and on the metered substitutes' long
    windows, and on nothing else: not on a purpose-limited channel (Dot, ChatGPT Web), not on the current task's flags, and
    not on any 5-hour clock."""

    def setUp(self):
        self.fleet = Fleet()

    @staticmethod
    def metered(d):
        rows = {(c['id'], bool(c['bundle'].get('fast')), c['bundle']['account']): (c['components']['opportunity_cost'], c['score'])
                for c in d['ranking'] if not c.get('excluded') and c.get('score') is not None
                and c['bundle']['account'] in ('claude', 'codex')}
        windows = {w['label']: (w['quota_price'], w['substitute_slack'], w['outside_fit']) for w in d['decision']['system']['quota_windows']}
        return rows, windows

    def test_toggling_local_only_leaves_every_metered_price_bit_identical_and_dot_wins_when_its_fit_ties(self):
        """Dot is eligible only when the work may leave the machine, which says nothing about how scarce Claude's or Codex's
        weeks are. It is no outside point of any window, so every metered window's price and every metered candidate's cost
        is the same whether Dot is in the plan or not; and where its capped fit ties the best, its cost of about 0 wins."""
        kw = dict(allowed=('claude', 'codex', 'dot'), axes=(1, 1, 0, 0), hours=0.3)
        shut = self.fleet.plan(0.6, 0.9, local_only=True, **kw)
        open_ = self.fleet.plan(0.6, 0.9, local_only=False, **kw)
        self.assertFalse([c for c in eligible(shut) if c['bundle']['account'] == 'dot'])
        self.assertTrue([c for c in eligible(open_) if c['bundle']['account'] == 'dot'])
        self.assertEqual(self.metered(shut), self.metered(open_))
        dot = next(c for c in eligible(open_) if c['bundle']['account'] == 'dot')
        best_metered = max(c['components']['fit'] for c in eligible(open_) if c['bundle']['account'] != 'dot')
        self.assertAlmostEqual(dot['components']['fit'], best_metered)          # its capped fit ties the best
        self.assertEqual(dot['components']['opportunity_cost'], 0.0)
        self.assertEqual(open_['selected_bundle']['account'], 'dot')
        self.assertNotEqual(shut['selected_bundle']['account'], 'dot')
        # the same on a frontier task: the metered prices still do not move whether Dot may take the work or not
        hard_shut = self.fleet.plan(0.6, 0.9, local_only=True, allowed=('claude', 'codex', 'dot'), axes=FRONTIER, hours=6.0)
        hard_open = self.fleet.plan(0.6, 0.9, local_only=False, allowed=('claude', 'codex', 'dot'), axes=FRONTIER, hours=6.0)
        self.assertEqual(self.metered(hard_shut), self.metered(hard_open))

    def test_varying_only_a_substitutes_5h_usage_leaves_every_other_windows_price_identical(self):
        """Claude's week is 90% free while Codex's is on its line. Claude is Codex's substitute, and whether Claude's 5-hour
        window is empty, nearly full (the work would wait for it) or even closes Claude out for the task never changes what
        a dollar of Codex's week costs, nor Claude's own prices."""
        runs = [self.fleet.plan(0.1, 0.5, five_hour_used=u, axes=FRONTIER, hours=2.0) for u in (0.0, 0.5, 0.9, 0.95, 0.995)]
        waits = [[c.get('available_in_hours') for c in eligible(d) if c['bundle']['account'] == 'claude'] for d in runs]
        self.assertTrue(any(any(w for w in ws) for ws in waits) or any(not ws for ws in waits), waits)   # 5h really did bite
        base = self.metered_prices(runs[0])
        for d in runs[1:]:
            self.assertEqual(self.metered_prices(d), base)

    @staticmethod
    def metered_prices(d):
        out = {}
        for w in d['decision']['system']['quota_windows']:
            out[w['label']] = (w['quota_price'], w['need_probability'], w['substitute_slack'], w['outside_fit'])
        codex = {c['id']: c['components']['opportunity_cost'] for c in eligible(d)
                 if c['bundle']['account'] == 'codex' and not c['bundle'].get('fast')}
        return out, codex


class FastVariantIdentityTests(unittest.TestCase):
    """Owner decision of round 14: a fast-mode variant is its own candidate with its own id (`<bundle id>+fast`), so a bundle
    id names exactly one variant. The launch identity (`selected_bundle.bundle_id`) stays the bundle's own id and the `fast`
    flag says how to launch it."""

    def setUp(self):
        self.fleet = Fleet()

    def plan(self, **task):
        # 2h before the weekly reset the quota costs nothing: the fast variant ties its base on N and wins on speed
        return self.fleet.plan(0.6, 0.5, allowed=('claude',), ec=1 - 2 / WEEK, **task)

    def assert_equal_n_fast_ranks_first(self, d):
        """User rule (2026-11-10): quota about to expire is optimised for speed. A fast variant that ties its base on N
        ranks ahead of it, decided by completion time."""
        rows = eligible(d)
        pos = {c['id']: i for i, c in enumerate(rows)}
        pairs = [(c, next(b for b in rows if b['id'] == c['base_id'])) for c in rows
                 if c['bundle'].get('fast') and c.get('base_id') in pos]
        tied = [(f, b) for f, b in pairs if f['score'] == b['score']]
        self.assertTrue(tied, 'near the reset at least one fast variant must tie its base on N')
        for f, b in tied:
            self.assertLess(f['duration_hours'], b['duration_hours'], f['id'])
            self.assertLess(pos[f['id']], pos[b['id']], f['id'])
            self.assertEqual(candidate_ranking.five_axis_deciding_key(f, b), 'completion_hours')
        return tied

    def test_the_variants_have_distinct_ids_and_the_fast_one_launches_as_its_base_bundle(self):
        d = self.plan()
        tied = self.assert_equal_n_fast_ranks_first(d)
        if d['selected_bundle'].get('fast'):
            self.assertEqual(d['choice'], d['selected_bundle']['bundle_id'] + '+fast')
        ids = [c['id'] for c in eligible(d)]
        self.assertEqual(len(ids), len(set(ids)))
        for f, b in tied:
            self.assertEqual(f['id'], b['id'] + '+fast')
        fast = self.plan(pinned_bundle='opus55_high+fast')
        self.assertEqual(fast['selected_bundle']['bundle_id'], 'opus55_high')
        self.assertTrue(fast['selected_bundle']['fast'])

    def test_a_pinned_bundle_gets_exactly_that_variant(self):
        base = self.plan(pinned_bundle='opus55_high')
        self.assertEqual(base['choice'], 'opus55_high')
        self.assertFalse(base['selected_bundle'].get('fast'))
        base_row = next(c for c in base['ranking'] if c['id'] == 'opus55_high')
        usd_per_hour = base_row['five_axis_price']['usd_per_hour']
        self.assertAlmostEqual(base['selected_bundle']['expected_usd'], 2.0 * usd_per_hour, places=1)   # hours x cited usd per hour
        price_mult = axis_scores.load_five_axis()['models']['claude-opus-5-5']['fast_mode']['price_multiplier']
        fast = self.plan(pinned_bundle='opus55_high+fast')
        self.assertEqual(fast['choice'], 'opus55_high+fast')
        self.assertTrue(fast['selected_bundle']['fast'])
        self.assertEqual(fast['selected_bundle']['bundle_id'], 'opus55_high')
        self.assertAlmostEqual(fast['selected_bundle']['expected_usd'], 2.0 * usd_per_hour * price_mult, places=1)
        forced = self.plan(pinned_bundle='opus55_high', force_fast=True)       # --fast on a named bundle: that bundle, fast
        self.assertTrue(forced['selected_bundle']['fast'])
        self.assertEqual(forced['selected_bundle']['bundle_id'], 'opus55_high')
        self.assertEqual(forced['choice'], 'opus55_high+fast')

    def test_alternatives_keep_the_base_variant_and_the_table_shows_which_row_is_fast(self):
        import demo_plan
        d = self.plan()
        self.assert_equal_n_fast_ranks_first(d)
        winner = d['choice']
        self.assertTrue(d['selected_bundle'].get('fast'), 'a tied fast variant outranks its base, so the winner is fast')
        base_id = d['selected_bundle']['bundle_id']
        self.assertIn(base_id, d['decision']['alternatives'])
        table = demo_plan.format_table(d)
        rows = {line.split()[1]: line for line in table.splitlines() if line.split()[:1] and line.split()[0].isdigit()}
        self.assertIn(winner, rows)
        self.assertIn(base_id, rows)
        self.assertTrue(rows[winner].rstrip().endswith('winner'))
        self.assertTrue(rows[base_id].rstrip().endswith('eligible'))


class ReadFailureAndHoldTests(unittest.TestCase):
    """Owner decision of round 13b: a failed usage read (even a 429 from the usage endpoint) is a read failure, so the latest
    successful reading of the CURRENT reset period prices the account, labelled with its read time; with none the account
    goes to mode B. A provider hold (`provider_retry`, `model_latch`) is a quota/launch failure signal and always wins over a
    reading, stale or not."""

    def setUp(self):
        self.fleet = Fleet()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        env = {'ROUTER_PROVIDER_RETRY': os.path.join(self.dir.name, 'provider-retry.json'),
               'ROUTER_MODEL_LATCH': os.path.join(self.dir.name, 'model-unavailable.json')}
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def dated(usage):
        """Real readings carry the reset instant of each window, which is how a reading is placed in its reset period."""
        for w in usage['accounts']['claude']['windows']:
            w['resets_at'] = (NOW + timedelta(seconds=w['seconds_until_reset'])).isoformat()

    @classmethod
    def failing(cls, read_at):
        def hook(usage):
            cls.dated(usage)
            usage['accounts']['claude'].update(status='stale', available=False, stale=True, fetched_at=read_at.isoformat(),
                                               error_status=429, error_code='rate_limit_error')
        return hook

    @staticmethod
    def bundle(name, model):
        return {'account': name, 'provider': name, 'model': model, 'launch_model': model}

    def test_a_429_from_the_usage_endpoint_prices_the_latest_reading_of_this_period_and_says_when_it_was_read(self):
        fresh = self.fleet.plan(0.6, 0.5, axes=FRONTIER, hours=6.0, usage_hook=self.dated)
        read_at = NOW - timedelta(hours=3)
        failed = self.fleet.plan(0.6, 0.5, axes=FRONTIER, hours=6.0, usage_hook=self.failing(read_at))
        claude = [c for c in eligible(failed) if c['bundle']['account'] == 'claude']
        self.assertTrue(claude)
        for c in claude:
            self.assertEqual(c['components']['reading_at'], read_at.isoformat())
            self.assertTrue(any('priced from the reading taken at ' + read_at.isoformat() in n for n in c['notes']))
        self.assertTrue(any(read_at.isoformat() in w and 'last good usage reading' in w
                            for w in failed['decision']['warnings']))
        # the same reading prices the same: the scores of the real candidates are the fresh ones (a stale state never
        # renews, so a candidate that would have waited for a reset may gain a fast twin; those are left out here)
        same = lambda d: [(c['id'], c['score']) for c in eligible(d)
                          if c['bundle']['account'] == 'claude' and not c['bundle'].get('fast')]
        self.assertEqual(same(fresh), same(failed))
        self.assertNotIn('reading_at', fresh['ranking'][0]['components'])
        snap = ledger_snapshot.decision_snapshot(failed)
        top_claude = next(r for r in snap['ranking'] if r.get('id') and 'reading_at' in (r.get('components') or {}))
        self.assertEqual(top_claude['components']['reading_at'], read_at.isoformat())

    def test_a_failed_read_with_no_reading_in_the_current_period_is_mode_b(self):
        """The only reading is from before this period's start (the week reset since): it describes another cycle, so the
        account leaves automatic routing with a surfaced data error and is no substitute for the others."""
        old = NOW - timedelta(hours=200)
        d = self.fleet.plan(0.6, 0.5, usage_hook=self.failing(old))
        gone = [c for c in d['ranking'] if c['bundle']['account'] == 'claude']
        self.assertTrue(gone)
        for c in gone:
            self.assertTrue(c['excluded'])
            self.assertIn(candidate_eval.QUOTA_READING_UNAVAILABLE, c['excluded'][0])
        self.assertTrue(any(candidate_eval.QUOTA_READING_UNAVAILABLE in w for w in d['decision']['warnings']))
        self.assertEqual(d['selected_bundle']['account'], 'codex')

    def test_a_provider_hold_on_a_model_wins_over_a_stale_reading_and_another_model_is_chosen_in_the_same_roll(self):
        hook = self.failing(NOW - timedelta(hours=3))
        free = self.fleet.plan(0.3, 0.9, axes=FRONTIER, hours=6.0, allowed=('claude',), usage_hook=hook)
        model = free['selected_bundle']['model']
        self.assertEqual(free['selected_bundle']['account'], 'claude')
        entry = provider_retry.record(
            self.bundle('claude', model),
            {'failure_code': 'provider_turn_failed', 'at': NOW.isoformat(), 'provider_message': 'rate limited, quota exhausted'},
            dispatch_id='d-1', at=NOW, classify=lambda *a: ('model', {}), accounts=self.fleet.accounts)
        self.assertIsNotNone(entry)
        again = self.fleet.plan(0.3, 0.9, axes=FRONTIER, hours=6.0, allowed=('claude',), usage_hook=hook)
        held = [c for c in again['ranking'] if c['bundle']['model'] == model and c['bundle']['account'] == 'claude']
        self.assertTrue(held)
        for c in held:
            self.assertTrue(any(provider_retry.EXCLUSION_PREFIX in r for r in c['excluded']), c['excluded'])
        self.assertEqual([c for c in eligible(again) if c['bundle']['model'] == model], [])       # no fast twin either
        self.assertNotEqual(again['selected_bundle']['model'], model)
        self.assertEqual(again['selected_bundle']['account'], 'claude')
        self.assertTrue(any(w.startswith('claude/') for w in again['decision']['warnings']))

    def test_a_provider_hold_on_a_quota_pool_keeps_that_pool_out_even_though_its_stale_reading_shows_quota(self):
        """Claude's Fable windows form their own quota pool next to the seven-day one. A hold the classifier scoped to the
        account holds the pool of the failed model only; the stale reading, which still shows quota, does not undo it."""
        hook = self.failing(NOW - timedelta(hours=3))
        base = self.fleet.plan(0.3, 0.9, axes=FRONTIER, hours=6.0, allowed=('claude',), usage_hook=hook)
        fable = [c for c in eligible(base) if 'fable' in c['bundle']['model']]
        self.assertTrue(fable)
        pool = {'id': 'weekly_fable', 'models': sorted({c['bundle']['model'] for c in fable})}
        with patch.object(provider_retry, 'resolve_pool', return_value=pool):      # the pools come from structure (pool_of)
            entry = provider_retry.record(
                self.bundle('claude', fable[0]['bundle']['model']),
                {'failure_code': 'provider_turn_failed', 'at': NOW.isoformat(), 'provider_message': 'quota exhausted for this plan'},
                dispatch_id='d-2', at=NOW, classify=lambda *a: ('account', {}), accounts=self.fleet.accounts)
        self.assertIsNotNone(entry)
        self.assertEqual(entry['pool']['id'], 'weekly_fable')
        after = self.fleet.plan(0.3, 0.9, axes=FRONTIER, hours=6.0, allowed=('claude',), usage_hook=hook)
        held = [c for c in after['ranking'] if c['bundle']['account'] == 'claude' and any(provider_retry.EXCLUSION_PREFIX in r for r in c['excluded'])]
        self.assertTrue(held)
        self.assertTrue(all('fable' in c['bundle']['model'] for c in held))
        self.assertEqual([c for c in eligible(after) if 'fable' in c['bundle']['model']], [])
        self.assertTrue([c for c in eligible(after) if c['bundle']['account'] == 'claude'])      # the other pool still routes

    def test_an_invalid_model_latch_excludes_the_model_and_another_model_is_chosen_in_the_same_roll(self):
        free = self.fleet.plan(0.3, 0.9, axes=FRONTIER, hours=6.0, allowed=('claude',))
        model = free['selected_bundle']['model']
        self.assertIsNotNone(model_latch.record(self.bundle('claude', model), 'INVALID_MODEL', at=NOW))
        latched = self.fleet.plan(0.3, 0.9, axes=FRONTIER, hours=6.0, allowed=('claude',))
        self.assertNotEqual(latched['selected_bundle']['model'], model)
        self.assertEqual([c for c in eligible(latched) if c['bundle']['model'] == model], [])
        rows = [c for c in latched['ranking'] if c['bundle']['model'] == model and c['bundle']['account'] == 'claude']
        self.assertTrue(rows)
        for c in rows:
            self.assertTrue(any('model launch failed' in r for r in c['excluded']), c['excluded'])


class DurationKeyTests(unittest.TestCase):
    """One basis for the time a task takes (owner, 2026-11-10: Artificial Analysis Time/Task at the same effort): the hours
    that price the spend and the window are the hours the "faster" tie-break compares, per candidate effort."""

    @classmethod
    def setUpClass(cls):
        cls.fleet = Fleet()
        cls.data = axis_scores.load_five_axis()

    def record(self, model):
        return self.data['models'][model]['duration_multiplier']

    def info(self, model):
        dur = self.record(model)
        return {'five_axis': {'duration': {k: copy.deepcopy(dur[k]) for k in ('status', 'by_effort', 'basis_by_effort')}}}

    def minutes(self, model, effort):
        return self.record(model)['minutes_by_effort'][effort]

    def test_the_hours_follow_the_aa_time_at_the_candidates_effort(self):
        """A 0.3 h creative task with Dot available: Opus 5.5 is faster than Fable 5.1 at High on Artificial Analysis, and the
        hours that priced the spend are the hours the faster key compares."""
        d = self.fleet.plan(0.05, 0.5, ec=0.9, axes=CREATIVE, hours=0.3, allowed=('claude', 'codex', 'dot'))
        opus, fable = row(d, 'opus55_high'), row(d, 'fable51_high')
        for c in (opus, fable):
            self.assertEqual(c['components']['duration_source'], 'aa')
        self.assertLess(self.minutes('claude-opus-5-5', 'high'), self.minutes('claude-fable-5-1', 'high'))
        self.assertAlmostEqual(fable['duration_hours'] / opus['duration_hours'],
                               self.minutes('claude-fable-5-1', 'high') / self.minutes('claude-opus-5-5', 'high'), places=2)
        self.assertGreater(fable['expected_hours'], opus['expected_hours'])
        tied_opus, tied_fable = dict(opus), dict(fable)
        tied_fable['score'] = tied_opus['score']
        self.assertLess(candidate_ranking.five_axis_order(tied_opus), candidate_ranking.five_axis_order(tied_fable))
        self.assertEqual(candidate_ranking.five_axis_deciding_key(tied_opus, tied_fable), 'completion_hours')
        self.assertIn('time at high', ' '.join(opus['notes']))

    def test_the_spend_is_usd_per_hour_times_those_hours(self):
        d = self.fleet.plan(0.05, 0.5, ec=0.9, axes=CREATIVE, hours=6.0, allowed=('claude', 'codex'))
        for bundle_id, model in (('opus55_high', 'claude-opus-5-5'), ('sol_high', 'gpt-6.1-sol')):
            c = row(d, bundle_id)
            usd = self.data['models'][model]['usd_per_hour']['value']
            self.assertAlmostEqual(c['expected_usd'], c['duration_hours'] * usd, delta=0.01 + 0.005 * usd, msg=bundle_id)

    def test_every_effort_pair_follows_the_aa_time_at_that_effort_including_max(self):
        opus, fable = self.info('claude-opus-5-5'), self.info('claude-fable-5-1')
        seen = set()
        for effort in ('low', 'medium', 'high', 'xhigh', 'max'):
            m_opus, basis = quota_economy.duration_at(opus, effort)
            m_fable, _ = quota_economy.duration_at(fable, effort)
            self.assertEqual(basis['method'], 'aa', effort)
            aa_o, aa_f = self.minutes('claude-opus-5-5', effort), self.minutes('claude-fable-5-1', effort)
            self.assertEqual(m_opus < m_fable, aa_o < aa_f, effort)
            seen.add(aa_o < aa_f)
        self.assertEqual(seen, {True, False})        # Opus is faster at the lower efforts and slower at max (public data)

    def test_an_effort_with_no_time_has_none_and_no_other_efforts_time_stands_in(self):
        opus = self.info('claude-opus-5-5')
        del opus['five_axis']['duration']['by_effort']['xhigh']
        self.assertEqual(quota_economy.duration_at(opus, 'xhigh'), (None, None))
        self.assertEqual(quota_economy.duration_at(opus, 'high')[1]['method'], 'aa')
        self.assertEqual(quota_economy.duration_at(opus, None), quota_economy.duration_at(opus, 'high'))    # no effort: the single configuration
        self.assertEqual(quota_economy.duration_at({}, 'high'), (None, None))

    def test_a_fast_variant_shortens_the_candidates_hours_and_keeps_the_source(self):
        d = self.fleet.plan(0.6, 0.5, ec=1 - 2 / WEEK)
        base, fast = row(d, 'opus55_high'), row(d, 'opus55_high', fast=True)
        self.assertEqual(fast['components']['duration_source'], base['components']['duration_source'])
        self.assertLess(fast['duration_hours'], base['duration_hours'])

    def test_the_snapshot_row_names_the_duration_source(self):
        d = self.fleet.plan(0.05, 0.5, ec=0.9, axes=CREATIVE, hours=0.3, allowed=('claude', 'codex', 'dot'))
        snap = ledger_snapshot.decision_snapshot(d)
        top = next(r for r in snap['ranking'] if r.get('components'))
        self.assertIn('duration_source', top['components'])


def window_reports(decision):
    return {b['label']: b for b in decision['decision']['system']['quota_windows']}


class BurstSizeTests(unittest.TestCase):
    """Round 15 (advisor v3.1, option A, owner): the burst size q_w = min(C_w, mean over the demand grid of the account's own
    best candidate's spend x min(1, hours to the weekly reset / its hours)), with no 5-hour quantity in it."""

    @classmethod
    def setUpClass(cls):
        cls.fleet = Fleet()

    def plan(self, *args, hours=2.0, **kwargs):
        """`Fleet.plan` with the task's hours also given at the top level, where `plan()` reads them."""
        return self.fleet.plan(*args, hours=hours, expected_hours=hours, **kwargs)

    def markov(self, w):
        """E[demand to come] / room, the Markov bound on pi at the window's current use."""
        cap, used, q = w['capacity_usd'], w['used_usd'], w['lump_usd']
        e = 1.0 - w['reset_in_hours'] / w['period_hours']
        return (used + q) * (1.0 - e) / ((e + q / cap) * (cap - used))

    def before_the_reset(self, decision):
        """Candidates whose whole task finishes before the weekly reset (nothing falls into the next period)."""
        return [c for c in eligible(decision) if all(w['spend_after_reset'] == 0.0 for w in c['economy']['windows'])]

    def test_two_hours_before_the_weekly_reset_nothing_of_the_week_is_needed_and_the_fast_twin_wins_on_time(self):
        """Advisor v3.1 test 1: 2 h (and 10 min) before the weekly reset with 40 to 99 % left on Max20 and on Codex Pro,
        the need probability is at most the Markov bound, every candidate that finishes before the reset costs less than N's
        4-place resolution, the fast variant ties its base on N and wins on completion time."""
        for tau, hours in ((2.0, 2.0), (1 / 6, 0.1)):
            e = 1.0 - tau / WEEK
            for left in (0.40, 0.60, 0.80, 0.90, 0.99):
                d = self.plan(1 - left, 1 - left, ec=e, ex=e, hours=hours)
                for label, w in window_reports(d).items():
                    self.assertLessEqual(w['need_probability'], self.markov(w) + 1e-12, (label, left, tau))
                    self.assertGreater(w['lump_usd'], 0.0)
                    self.assertLess(w['lump_usd'], w['capacity_usd'] * 0.1, (label, left, tau))
                inside = self.before_the_reset(d)
                self.assertGreaterEqual(len(inside), 4, (left, tau))
                for c in inside:
                    self.assertLess(c['components']['opportunity_cost'], 5e-5, (c['id'], left, tau))
                pairs = 0
                ids = {c['id'] for c in inside}
                for fast in [c for c in inside if c['bundle'].get('fast') and c['base_id'] in ids]:
                    base = row(d, fast['base_id'])
                    self.assertEqual(round(base['score'], 4), round(fast['score'], 4), (fast['id'], left, tau))
                    self.assertLess(candidate_ranking.five_axis_order(fast), candidate_ranking.five_axis_order(base), (fast['id'], left, tau))
                    self.assertEqual(candidate_ranking.five_axis_deciding_key(fast, base), 'completion_hours')
                    pairs += 1
                self.assertGreaterEqual(pairs, 1, (left, tau))
                self.assertTrue(d['selected_bundle'].get('fast'), (left, tau))

    def test_the_5h_clock_usage_and_existence_move_neither_the_price_nor_the_probability_nor_the_lump(self):
        """Advisor v3.1 test 2: the same fleet with the 5-hour window's reset, its use, and finally the window itself changed
        or removed: every candidate's cost and need probability, and every weekly window's lump, are bit-identical."""
        def with_five_hour(used, seconds):
            def hook(usage):
                for w in usage['accounts']['claude']['windows']:
                    if w['id'] == 'five_hour':
                        w['seconds_until_reset'] = seconds
            return dict(five_hour_used=used, usage_hook=hook)

        def without_five_hour(acc):
            windows = acc['accounts']['claude']['windows']
            self.assertIn('five_hour', windows)
            windows.pop('five_hour')

        def drop_reading(usage):
            usage['accounts']['claude']['windows'] = [w for w in usage['accounts']['claude']['windows'] if w['id'] != 'five_hour']

        runs = [self.plan(0.55, 0.45, ec=0.6, **with_five_hour(used, secs))
                for used, secs in ((0.05, 5 * 3600), (0.5, 3600), (0.8, 4 * 3600), (0.95, 600))]
        runs.append(self.plan(0.55, 0.45, ec=0.6, mutate=without_five_hour, usage_hook=drop_reading))

        def table(d):
            return ({c['id']: (c['components']['opportunity_cost'], c['components']['need_probability'],
                               c['components']['quota_price']) for c in eligible(d)},
                    {k: (w['need_probability'], w['lump_usd'], w['quota_price']) for k, w in window_reports(d).items()})
        first_costs, first_windows = table(runs[0])
        for d in runs[1:]:
            costs, windows = table(d)
            shared = set(first_costs) & set(costs)
            self.assertGreaterEqual(len(shared), 8)
            for key in shared:
                self.assertEqual(costs[key], first_costs[key], key)
            self.assertEqual(windows, first_windows)

    def test_an_account_with_and_without_a_short_window_pays_the_same_weekly_cost(self):
        """Advisor v3.1 test 3: two accounts on the same plan with the same weekly state and bundles, one with a 5-hour window
        and one without: the same weekly cost, need probability and lump."""
        def two_accounts(acc):
            acc['accounts']['claude-2']['plan'] = acc['accounts']['claude']['plan']
            acc['accounts']['claude-2']['windows'] = {k: copy.deepcopy(v) for k, v in acc['accounts']['claude']['windows'].items()
                                                      if k != 'five_hour'}
            three = {k: v for k, v in acc['accounts']['claude']['bundles'].items()
                     if v['model'] in ('claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5-5') and v['effort'] == 'high'}
            acc['accounts']['claude']['bundles'] = three
            acc['accounts']['claude-2']['bundles'] = {f'claude-2__{k}': {**v, 'account': 'claude-2'} for k, v in three.items()}

        def hook(usage):
            usage['accounts']['claude-2']['windows'] = [w for w in usage['accounts']['claude-2']['windows'] if w['id'] != 'five_hour']
        d = self.plan(0.6, 0.5, ec=0.5, allowed=('claude', 'claude-2'), claude2=(0.6, 0.5), mutate=two_accounts, usage_hook=hook)
        reports = window_reports(d)
        for label in ('seven_day', 'weekly_fable'):
            if f'claude/{label}' in reports and f'claude-2/{label}' in reports:
                a, b = reports[f'claude/{label}'], reports[f'claude-2/{label}']
                self.assertEqual((a['lump_usd'], a['need_probability'], a['quota_price']),
                                 (b['lump_usd'], b['need_probability'], b['quota_price']))
        self.assertTrue(any(k.startswith('claude-2/') for k in reports))
        for model in ('opus55_high', 'sonnet55_high', 'haiku55_high'):
            one, two = row(d, model), row(d, f'claude-2__{model}')
            self.assertEqual(one['components']['opportunity_cost'], two['components']['opportunity_cost'], model)

    def test_the_pi_curve_is_monotone_in_use_and_non_increasing_toward_the_reset_for_any_burst_size(self):
        rng = random.Random(11)
        for _ in range(12):
            cap, share = rng.uniform(10.0, 4000.0), rng.uniform(0.003, 0.1)
            previous = -1.0
            for u in range(0, 101, 4):
                p = quota_economy.five_axis_need_probability(rec_of(cap, u / 100, 0.5, lump=share), cap * u / 100)
                self.assertGreaterEqual(p, previous - 1e-12)
                previous = p
            late = [quota_economy.five_axis_need_probability(rec_of(cap, 0.5, e, lump=share), 0.5 * cap) for e in (0.2, 0.5, 0.8, 0.95, 0.999)]
            self.assertEqual(late, sorted(late, reverse=True))

    def test_the_burst_is_cut_at_the_reset_and_is_the_whole_task_when_the_task_ends_before_it(self):
        """Advisor v3.1 test 5: a task longer than the hours to the reset spends only the share before it, so the lump is at
        most the dearest candidate's dollars per hour x the hours to the reset; a task that ends before the reset gives the
        same lump whatever the reset time is."""
        short_a = self.plan(0.5, 0.5, ec=0.5, hours=0.25)
        short_b = self.plan(0.5, 0.5, ec=0.9, hours=0.25)           # 17 h to the reset, still more than the task
        for label, w in window_reports(short_a).items():
            self.assertEqual(w['lump_usd'], window_reports(short_b)[label]['lump_usd'], label)
        tau = 2.0
        long_task = self.plan(0.5, 0.5, ec=1 - tau / WEEK, ex=1 - tau / WEEK, hours=20.0)
        for label, w in window_reports(long_task).items():
            account = label.split('/')[0]
            # every candidate of the account counts, those the capacity check keeps out too: at the Time/Task they now take
            # (Fable 5.1 27 h for this 20 h task) the dearest Fable lines no longer fit the weekly Fable window
            rate = max(c['expected_usd'] / c['expected_hours'] for c in long_task['ranking']
                       if c['bundle']['account'] == account and not c['bundle'].get('fast') and c['expected_hours'] > 0)
            self.assertLessEqual(w['lump_usd'], rate * tau * 1.0001, label)
            whole = window_reports(self.plan(0.5, 0.5, ec=0.5, hours=20.0)).get(label)
            if whole is not None:
                self.assertLess(w['lump_usd'], whole['lump_usd'], label)

    def test_scaling_dollars_leaves_the_probability_and_scaling_the_task_scales_the_burst(self):
        """Advisor v3.1 test 6: multiplying C, U and q by a constant leaves pi unchanged; a task twice as long (still ending
        before the reset) has twice the burst."""
        for k in (0.01, 3.0, 250.0):
            for share in (0.01, 0.04):
                self.assertAlmostEqual(quota_economy.five_axis_need_probability(rec_of(1000.0, 0.45, 0.5, lump=share), 450.0),
                                       quota_economy.five_axis_need_probability(rec_of(1000.0 * k, 0.45, 0.5, lump=share), 450.0 * k), places=12)
        one = self.plan(0.5, 0.5, ec=0.5, hours=1.0)
        two = self.plan(0.5, 0.5, ec=0.5, hours=2.0)
        for label, w in window_reports(one).items():
            self.assertAlmostEqual(window_reports(two)[label]['lump_usd'] / w['lump_usd'], 2.0, delta=0.02, msg=label)

    def test_a_really_tight_week_still_prices_the_work_2h_before_the_reset(self):
        """Advisor v3.1 test 7: 5 % of the week left two hours before the reset: the work may well be needed (the advisor's
        pi = 0.040 with the 2 h burst of $30.61), and the dear candidate pays more than N's resolution. (In a whole fleet the
        5 % week lets no big candidate start at all, so this is a window-level check; the whole-fleet check uses 8 %.)"""
        e = 1.0 - 2.0 / WEEK
        rec = rec_of(1704.5, 0.95, e, reset_hours=2.0, lump=30.61 / 1704.5)
        self.assertAlmostEqual(quota_economy.five_axis_need_probability(rec, rec['used_usd']), 0.0401, places=3)
        cost = quota_economy.five_axis_spend_cost(rec, ladder_for(), 45.72, 2.0)['cost']
        self.assertGreater(cost, 1e-3)
        d = self.plan(0.92, 0.92, ec=e, ex=e)
        for label, w in window_reports(d).items():
            if w['used_usd'] > 0:
                self.assertGreater(w['need_probability'], 0.0, label)
                self.assertLessEqual(w['need_probability'], self.markov(w) + 1e-12, label)

    def test_a_lump_is_required_and_the_fresh_period_carries_its_own(self):
        """The tail never falls back to the whole capacity: a record without `lump_usd` is an error (round 15); the part of
        a task after the reset is priced with the new period's own untruncated burst."""
        bare = rec_of(1000.0, 0.5, 0.5)
        bare.pop('lump_usd')
        with self.assertRaises(KeyError):
            quota_economy.five_axis_need_tail(bare, 100.0)
        ladder = ladder_for()
        rec = rec_of(1704.5, 0.6, 1 - 2 / WEEK, reset_hours=2.0, lump=0.001)
        small = quota_economy.five_axis_spend_cost(rec, ladder, 137.0, 6.0, fresh_lump=17.0)['cost']
        big = quota_economy.five_axis_spend_cost(rec, ladder, 137.0, 6.0, fresh_lump=170.0)['cost']
        self.assertNotEqual(small, big)



class PoolIndependenceTests(unittest.TestCase):
    """Round 16 (owner policy, review R2): a window price does not depend on the current task flags. Each account ladder, outside
    point and lump come from the account own candidates (the economy pool, evaluated with the task flags neutralised) and
    not from the ones this task may pick or from whether they fit the remaining capacity; the remaining capacity enters only
    through the window own state (room, used, reset clock)."""

    @classmethod
    def setUpClass(cls):
        cls.fleet = Fleet()

    def plan(self, *args, hours=2.0, **kwargs):
        return self.fleet.plan(*args, hours=hours, expected_hours=hours, **kwargs)

    def economies(self, *args, **kwargs):
        """(decision, the FiveAxisEconomy objects the plan built), so the unrounded ladder, lump and outside values compare."""
        built = []
        real = quota_economy.FiveAxisEconomy.report

        def spy(econ):
            built.append(econ)
            return real(econ)
        with patch.object(quota_economy.FiveAxisEconomy, 'report', spy):
            d = self.plan(*args, **kwargs)
        return d, built[-1]

    def fingerprint(self, econ):
        out = {}
        for wkey in sorted(econ.points):
            ladder = econ.ladder(wkey)
            out[wkey] = (repr(ladder['steps']), econ.lump(wkey), econ.spend_max.get(wkey),
                         repr(econ.outside_at_task(wkey)), len(econ.points[wkey]))
        out['units'] = sorted(map(repr, econ.units))
        return out

    FLAGS = {
        'visual': {'needs': {'visual': True}},
        'critical': {'needs': {'critical': True}},
        'outbound_denied': {'needs': {'outbound_ok': False}},
        'file_transfer': {'needs': {'file_transfer': True}},
        'disable_fast': {'needs': {'disable_fast': True}},
        'force_fast': {'needs': {'force_fast': True}},
        'named_model': {'constraints': {'model': 'claude-opus-5-5'}},
        'named_effort': {'constraints': {'effort': 'high'}},
        'metered_ok': {'metered_ok': True},
        'not_local_only': {'local_only': False},
    }

    def test_toggling_each_task_flag_leaves_every_windows_price_inputs_bit_identical(self):
        d0, base = self.economies(0.5, 0.5)
        reference = self.fingerprint(base)
        self.assertGreaterEqual(len([k for k in reference if k != 'units']), 3)       # claude x2 and codex weekly windows
        reports = {w['label']: w for w in d0['decision']['system']['quota_windows']}
        sets = {}
        for name, flag in self.FLAGS.items():
            d, econ = self.economies(0.5, 0.5, **flag)
            self.assertEqual(self.fingerprint(econ), reference, name)
            for w in d['decision']['system']['quota_windows']:
                for key in ('quota_price', 'need_probability', 'lump_usd', 'substitute_slack', 'outside_fit', 'best_fit',
                            'ladder_steps', 'candidates'):
                    self.assertEqual(w[key], reports[w['label']][key], (name, w['label'], key))
            sets[name] = {c['id'] for c in eligible(d)}
        # the flags do change who may run (otherwise the check would be vacuous), they just do not change a price
        self.assertNotEqual(sets['disable_fast'], {c['id'] for c in eligible(d0)})
        self.assertNotEqual(sets['named_model'], {c['id'] for c in eligible(d0)})

    def test_a_flag_that_holds_candidates_back_does_not_move_a_candidates_own_cost(self):
        """The candidates that stay eligible are priced against the same ladder, so with the same spend their cost is the
        same when a constraint or task flag holds other candidates back."""
        d0 = self.plan(0.5, 0.5)
        d1 = self.plan(0.5, 0.5, constraints={'effort': 'high'})
        both = {c['id']: c for c in eligible(d0)}
        shared = [c for c in eligible(d1) if c['id'] in both and c['bundle'].get('effort') == both[c['id']]['bundle'].get('effort')]
        self.assertTrue(shared)
        for c in shared:
            self.assertEqual(c['components']['quota_price'], both[c['id']]['components']['quota_price'], c['id'])
            self.assertEqual(c['components']['need_probability'], both[c['id']]['components']['need_probability'], c['id'])

    def test_real_data_economy_ladders_and_outside_alternatives_are_non_empty(self):
        """Q7 (2026-11-11): with fit zeroed at each category's panel weakest (t1), real public data produces non-empty
        value ladders and positive outside alternatives across priced windows."""
        d, econ = self.economies(0.5, 0.5)
        windows = d['decision']['system']['quota_windows']
        self.assertGreaterEqual(len(windows), 3)
        for w in windows:
            self.assertGreater(w['ladder_steps'], 0, w['label'])
            self.assertGreater(w['outside_fit'], 0.0, w['label'])
            self.assertGreater(w['best_fit'], 0.0, w['label'])
        for wkey in econ.points:
            self.assertTrue(econ.ladder(wkey)['steps'], wkey)
            self.assertTrue(econ._outside(wkey, 0), wkey)

    def test_the_usage_sweep_through_the_old_safety_margin_boundary_moves_the_lump_and_cost_smoothly(self):
        """Review R2(b): Claude at 93 % -> 94 % used changed the lump 21.87 -> 0.59 and Haiku cost 43 times, because the
        candidates that stopped fitting left the ladder. The lump is now constant (it does not read the remaining capacity),
        and the price and the cost move only as the need probability does."""
        used = [0.90 + 0.005 * i for i in range(10)]            # 90 % .. 94.5 %
        windows, haiku = [], []
        for u in used:
            d = self.plan(u, 0.5)
            windows.append(window_reports(d)['claude/seven_day'])
            haiku.append(next(c for c in eligible(d) if c['id'] == 'haiku55_high')['components']['opportunity_cost'])
        self.assertEqual(len({w['lump_usd'] for w in windows}), 1)
        prices = [w['quota_price'] for w in windows]
        self.assertEqual(prices, sorted(prices))
        for a, b in zip(prices, prices[1:]):
            self.assertLess(b / a, 1.1)
        self.assertEqual(haiku, sorted(haiku))
        for a, b in zip(haiku, haiku[1:]):
            self.assertLess(b / a, 1.1)
        self.assertLess(haiku[-1] / haiku[0], 1.5)

    def test_a_candidate_that_no_longer_fits_the_remaining_capacity_still_stands_in_its_accounts_ladder(self):
        """At 96 % used no Claude candidate fits a 2 h task, yet the Claude window still has its pool: ladder steps and lump
        of the Codex window do not change with it (Codex outside option moves only through the slack that Claude window
        state gives, which is the remaining capacity entering as room)."""
        d_low, econ_low = self.economies(0.5, 0.5)
        d_high, econ_high = self.economies(0.96, 0.5)
        self.assertFalse([c for c in eligible(d_high) if c['bundle']['account'] == 'claude'])
        key = next(k for k in econ_low.points if k[0] == 'claude')
        self.assertEqual(econ_high.ladder(key)['steps'], econ_low.ladder(key)['steps'])
        self.assertEqual(econ_high.lump(key), econ_low.lump(key))
        codex = next(k for k in econ_low.points if k[0] == 'codex')
        self.assertEqual(econ_high.lump(codex), econ_low.lump(codex))
        self.assertEqual(len(econ_high.points[codex]), len(econ_low.points[codex]))


class WeeklyWindowTests(unittest.TestCase):
    """Round 15 (owner: "every account should have a weekly quota"): a quota account whose weekly (long reset-period) window
    is missing or unread is a surfaced mode-B data error. It is not routable on the five-axis path and it is never a free
    outside point (slack 1) for another account's window. Round 16 (review R1): the error does not depend on the account's
    other exclusions, so a candidate held back only by its 5-hour window gets it too and never enters the economy."""

    @classmethod
    def setUpClass(cls):
        cls.fleet = Fleet()

    def plan(self, *args, hours=2.0, **kwargs):
        # Keep an explicitly expensive synthetic candidate to exercise simultaneous short-window and weekly exclusions
        # independently of public price changes (Fable 5.2's usd is a series conversion from Fable 5.1).
        data = copy.deepcopy(axis_scores.load_five_axis())
        data['models']['claude-fable-5-2']['usd_per_hour']['value'] = 100.0
        with patch.object(axis_scores, 'load_five_axis', return_value=data):
            return self.fleet.plan(*args, hours=hours, expected_hours=hours, **kwargs)

    def only_five_hour(self, acc):
        acc['accounts']['claude']['windows'] = {'five_hour': acc['accounts']['claude']['windows']['five_hour']}

    def claude_rows(self, d):
        return [c for c in d['ranking'] if c['bundle']['account'] == 'claude']

    def codex_price(self, d):
        return price_of(d, 'codex')

    def has_error(self, row, code):
        return any(code in r for r in row['excluded'])

    def held_by_five_hour(self, row):
        return any(r.startswith('five_hour ') for r in row['excluded'])

    def assert_every_row_has_one_of(self, d, codes):
        rows = self.claude_rows(d)
        self.assertTrue(rows)
        for c in rows:
            self.assertTrue(any(self.has_error(c, code) for code in codes), (c['id'], c['excluded']))
        return rows

    def test_a_weekly_window_that_is_not_read_while_the_5h_window_is_leaves_routing_and_is_no_outside_point(self):
        for hours in (2.0, 6.0):
            alone = self.plan(0.5, 0.5, allowed=('codex',), hours=hours)
            d = self.plan(0.5, 0.5, allowed=('claude', 'codex'), claude_long=False, hours=hours)    # only five_hour is read
            rows = self.assert_every_row_has_one_of(d, (candidate_eval.QUOTA_LONG_UNREAD, candidate_eval.QUOTA_READING_UNAVAILABLE))
            self.assertTrue(any(self.has_error(c, candidate_eval.QUOTA_LONG_UNREAD) for c in rows), hours)
            self.assertTrue(any(candidate_eval.QUOTA_LONG_UNREAD in w and 'claude' in w for w in d['decision']['warnings']))
            self.assertNotEqual(d['selected_bundle']['account'], 'claude')
            # claude is not Codex price source: Codex weekly price is the one it has with claude absent
            self.assertAlmostEqual(self.codex_price(d), self.codex_price(alone), places=12, msg=hours)

    def test_a_candidate_held_back_only_by_its_5h_window_still_gets_the_weekly_error_and_is_no_outside_point(self):
        """Review R1: with a 2 h (and a 6 h) task some Claude candidates cannot start on the 5-hour window alone. They used to
        skip the weekly data error (it required no other exclusion), entered the economy as `waiting` with no long window
        state, and became a slack-1.0 free outside point that cut Codex price by 10.6 %."""
        for hours in (2.0, 6.0):
            alone = self.plan(0.5, 0.5, allowed=('codex',), hours=hours)
            d = self.plan(0.5, 0.5, allowed=('claude', 'codex'), claude_long=False, hours=hours)
            held = [c for c in self.claude_rows(d) if self.held_by_five_hour(c)]
            self.assertTrue(held, hours)
            for c in held:
                self.assertTrue(any(self.has_error(c, code) for code in candidate_eval.QUOTA_DATA_ERRORS), (c['id'], c['excluded']))
            self.assertAlmostEqual(self.codex_price(d), self.codex_price(alone), places=12, msg=hours)
            # nothing of Claude is priced, and every window sees the same outside option as with Claude absent
            windows, windows_alone = d['decision']['system']['quota_windows'], alone['decision']['system']['quota_windows']
            self.assertFalse([w for w in windows if w['quota_source'] == 'claude'])
            self.assertEqual(windows, windows_alone)

    def test_an_account_with_no_weekly_window_declared_is_a_data_error_and_no_outside_point(self):
        for hours in (2.0, 6.0):
            alone = self.plan(0.5, 0.5, allowed=('codex',), hours=hours)
            d = self.plan(0.5, 0.5, allowed=('claude', 'codex'), mutate=self.only_five_hour, claude_long=False, hours=hours)
            self.assert_every_row_has_one_of(d, (candidate_eval.QUOTA_NO_LONG_WINDOW,))
            self.assertTrue([c for c in self.claude_rows(d) if self.held_by_five_hour(c)], hours)       # incl. the 5h-held ones
            warning = next(w for w in d['decision']['warnings'] if candidate_eval.QUOTA_NO_LONG_WINDOW in w)
            self.assertIn('claude', warning)
            self.assertAlmostEqual(self.codex_price(d), self.codex_price(alone), places=12, msg=hours)
        # naming the model dispatches it with the error surfaced, as for an unknown capacity
        named = self.plan(0.5, 0.5, allowed=('claude',), mutate=self.only_five_hour, claude_long=False, pinned_bundle='opus55_high')
        self.assertEqual(named['selected_bundle']['bundle_id'], 'opus55_high')
        self.assertTrue(any(candidate_eval.QUOTA_NO_LONG_WINDOW in n for n in next(c for c in named['ranking'] if c['id'] == 'opus55_high')['notes']))

    def test_a_bundle_that_draws_no_window_at_all_is_the_same_data_error(self):
        """The shape of a metered API account with no budget window (its auto bundles carry an explicit empty `draws`)."""
        def no_draws(acc):
            for b in acc['accounts']['claude']['bundles'].values():
                b['draws'] = []
        for hours in (2.0, 6.0):
            alone = self.plan(0.5, 0.5, allowed=('codex',), hours=hours)
            d = self.plan(0.5, 0.5, allowed=('claude', 'codex'), mutate=no_draws, hours=hours)
            self.assert_every_row_has_one_of(d, (candidate_eval.QUOTA_NO_LONG_WINDOW,))
            self.assertNotEqual(d['selected_bundle']['account'], 'claude')
            self.assertAlmostEqual(self.codex_price(d), self.codex_price(alone), places=12, msg=hours)

    def test_an_account_with_no_reading_at_all_is_the_reading_unavailable_error_incl_the_5h_held_candidates(self):
        def nothing_read(usage):
            usage['accounts']['claude']['windows'] = []
        for hours in (2.0, 6.0):
            alone = self.plan(0.5, 0.5, allowed=('codex',), hours=hours)
            d = self.plan(0.5, 0.5, allowed=('claude', 'codex'), usage_hook=nothing_read, hours=hours)
            self.assert_every_row_has_one_of(d, (candidate_eval.QUOTA_READING_UNAVAILABLE,))
            self.assertAlmostEqual(self.codex_price(d), self.codex_price(alone), places=12, msg=hours)
        # a candidate of an account that is read, held by the 5-hour clock, with no weekly state of its own
        d = self.plan(0.5, 0.5, allowed=('claude', 'codex'), claude_long=False)
        fable = next(c for c in d['ranking'] if c['id'] == 'fable52_high')
        self.assertTrue(self.held_by_five_hour(fable) and self.has_error(fable, candidate_eval.QUOTA_READING_UNAVAILABLE), fable['excluded'])

    def test_the_economy_never_builds_an_entry_for_a_candidate_with_no_long_window_state(self):
        """Defence in depth: even if some path lets a stateless candidate through, the economy skips it, so it cannot become an
        outside point whatever the data error says."""
        overlaid = axis_scores.five_axis_overlay(copy.deepcopy(self.fleet.accounts))
        stateless = {'bundle': {'account': 'claude', 'id': 'x'}, 'components': {'fit': 0.9}, 'expected_usd': 5.0,
                     '_five_window_states': {},
                     '_five_values': {'divergence': 0.0, 'coding': 0.0, 'aesthetics': 0.0,
                                      'tool_operation': {'responsiveness': 0.0, 'image_recognition': 0.0}}}
        econ = quota_economy.FiveAxisEconomy([stateless], overlaid, {}, {'hours': 2.0})
        self.assertEqual(econ.entries, [])
        self.assertEqual(econ.units, {})

    def test_a_weekly_window_that_is_read_keeps_the_account_in_routing(self):
        d = self.plan(0.5, 0.5, allowed=('claude', 'codex'))
        self.assertTrue([c for c in eligible(d) if c['bundle']['account'] == 'claude'])
        self.assertFalse([w for w in d['decision'].get('warnings') or [] if 'weekly quota' in w or 'no weekly' in w])

    def test_a_free_pools_auto_bundles_draw_the_allowance_that_the_overlay_declares(self):
        acc = copy.deepcopy(self.fleet.accounts)
        acc['accounts']['pool'] = {'plan': 'free', 'billing': 'subscription', 'free_allowance': True, 'verified': True,
                                   'bundles': {'auto:pool:opencode:x': {'provider': 'opencode', 'harness': 'opencode',
                                                                         'model': 'glm-5.3-flash-free', 'draws': [], 'auto': True},
                                               'named': {'provider': 'opencode', 'harness': 'opencode', 'model': 'glm-5.3-flash-free',
                                                         'draws': ['elsewhere']}}}
        out = axis_scores.five_axis_overlay(acc)['accounts']['pool']
        self.assertEqual(list(out['windows']), ['allowance'])
        self.assertEqual(out['bundles']['auto:pool:opencode:x']['draws'], ['allowance'])
        self.assertEqual(out['bundles']['named']['draws'], ['elsewhere'])       # an explicit non-empty draw is left alone
        self.assertTrue(out['capacity_unknown'])                                 # and the unsourced capacity stays mode B


class SnapshotQuotaLineTests(unittest.TestCase):
    """Round 16 (review J1): the README quota page reads `snapshot.quota_line.windows` and `ranking[*].quota_line`; the allocator is
    their single writer and the existing `system.quota_windows` / `ranking[*].economy` stay."""

    @classmethod
    def setUpClass(cls):
        cls.fleet = Fleet()
        cls.d = cls.fleet.plan(0.5, 0.5, hours=2.0, expected_hours=2.0)
        cls.snap = ledger_snapshot.decision_snapshot(cls.d)

    def test_every_priced_window_is_written_once_keyed_by_its_label_with_the_fields_the_page_draws(self):
        windows = self.snap['quota_line']['windows']
        reports = {w['label']: w for w in self.d['decision']['system']['quota_windows']}
        self.assertEqual(set(windows), set(reports))
        self.assertGreaterEqual(len(windows), 3)
        for label, w in windows.items():
            r = reports[label]
            self.assertEqual(w['label'], label)
            for src, dst in (('capacity_usd', 'capacity_usd'), ('used_usd', 'used_usd'), ('elapsed_hours', 'elapsed_hours'),
                             ('reset_in_hours', 'reset_in_hours')):
                self.assertEqual(w[dst], r[src], (label, dst))
            self.assertEqual(w['account'], r['quota_source'])
            self.assertEqual(w['window_id'], r['window'])
            self.assertEqual(label, f"{w['account']}/{w['window_id']}")
            self.assertGreater(w['capacity_usd'], 0)
            self.assertAlmostEqual(w['elapsed_hours'] + w['reset_in_hours'], 168.0, places=1)
        # the old readers keep their fields
        self.assertTrue(self.snap['decision']['system']['quota_windows'])
        self.assertTrue(all('economy' in c for c in self.snap['ranking'] if not c['excluded']))

    def test_the_chosen_candidates_row_names_its_binding_window_its_hours_and_its_spend_of_it(self):
        windows = self.snap['quota_line']['windows']
        rows = [c for c in self.snap['ranking'] if c['id'] == self.snap['choice']]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        line = row['quota_line']
        self.assertIn(line['window'], windows)
        priced = {f"{w['quota_source']}/{w['window']}": w for w in row['economy']['windows']}
        self.assertEqual(line['window'], f"{priced[line['window']]['quota_source']}/{row['economy']['binding_window']}")
        self.assertEqual(line['spend_usd'], priced[line['window']]['spend_usd'])
        self.assertEqual(line['hours'], row['expected_hours'])
        self.assertGreater(line['spend_usd'], 0)

    def test_every_eligible_row_that_prices_a_window_has_a_line_and_a_blocked_row_has_none(self):
        for c in self.snap['ranking']:
            if c['excluded']:
                self.assertNotIn('quota_line', c)
            elif c['economy']['binding_window']:
                self.assertIn(c['quota_line']['window'], self.snap['quota_line']['windows'])

    def test_the_block_is_plain_json_and_absent_when_nothing_was_priced(self):
        import json
        json.dumps(self.snap)
        self.assertIsNone(ledger_snapshot.snapshot_quota_line([]))
        self.assertIsNone(ledger_snapshot.candidate_quota_line({'economy': {'binding_window': None, 'windows': []}}))
        self.assertIsNone(ledger_snapshot.candidate_quota_line({}))

    def test_a_fresh_plan_or_a_snapshot_without_the_block_gets_the_same_block_derived(self):
        fresh = ledger_snapshot.with_quota_line(self.d)
        self.assertEqual(fresh['quota_line'], self.snap['quota_line'])
        chosen = next(c for c in fresh['ranking'] if c['id'] == self.d['choice'])
        snapped = next(c for c in self.snap['ranking'] if c['id'] == self.snap['choice'])
        self.assertEqual(chosen['quota_line'], snapped['quota_line'])
        bare = {**self.snap, 'ranking': [{k: v for k, v in c.items() if k != 'quota_line'} for c in self.snap['ranking']]}
        bare.pop('quota_line')
        derived = ledger_snapshot.with_quota_line(bare)
        self.assertEqual(derived['quota_line'], self.snap['quota_line'])
        self.assertEqual(next(c for c in derived['ranking'] if c['id'] == self.snap['choice'])['quota_line'],
                         snapped['quota_line'])
        self.assertIs(ledger_snapshot.with_quota_line(self.snap), self.snap)
        legacy = {'ranking': [], 'choice': None}
        self.assertIs(ledger_snapshot.with_quota_line(legacy), legacy)


class DurationUnknownTests(unittest.TestCase):
    """A bundle of a model the data has no time for at all (GPT-OSS 120B: no Artificial Analysis or arena listing and no
    same-series sibling) is out of automatic routing, and nothing stands in; a named dispatch still runs, on the task's own
    hours, with the data error on the row. A model whose efforts are only partly listed has the rest from a cited ratio."""

    @classmethod
    def setUpClass(cls):
        cls.fleet = Fleet()

    @staticmethod
    def grok_bundles(acc):
        cx = acc['accounts']['codex']['bundles']
        cx['oss_high'] = {**cx['sol_high'], 'model': 'gpt-oss-120b', 'effort': 'high'}
        cx['flash_low'] = {**cx['sol_high'], 'model': 'gemini-3.8-flash-high', 'effort': 'low'}
        cx['grok_max'] = {**cx['sol_high'], 'model': 'grok-4.7', 'effort': 'max'}

    def test_automatic_routing_leaves_the_effort_with_no_time_and_keeps_the_effort_that_has_one(self):
        d = self.fleet.plan(0.5, 0.5, mutate=self.grok_bundles)
        ranking = {c['id']: c for c in d['ranking']}
        self.assertIn('no Time/Task for gpt-oss-120b at high', ' '.join(ranking['oss_high']['excluded']))
        self.assertIsNone(ranking['oss_high']['score'])
        self.assertNotIn('oss_high', [c['id'] for c in eligible(d)])
        # Gemini 3.8 Flash lists High/Medium on Artificial Analysis: Low routes on the fleet's low/medium duration ratio, cited,
        # and has its own measured Low capability scores.
        self.assertIsNotNone(ranking['flash_low']['score'])
        self.assertEqual(ranking['flash_low']['components']['duration_source'], 'fleet_effort_ratio')
        # Grok 4.7 also gets its Max duration from fleet_effort_ratio, while Mode B excludes Max for missing Max capability data (R2-F5).
        self.assertEqual(ranking['grok_max']['components']['duration_source'], 'fleet_effort_ratio')
        self.assertIsNone(ranking['grok_max']['score'])

    def test_a_named_dispatch_keeps_the_bundle_with_the_tasks_own_hours_and_the_data_error(self):
        d = self.fleet.plan(0.5, 0.5, mutate=self.grok_bundles, pinned_bundle='oss_high', hours=2.0)
        self.assertEqual(d['choice'], 'oss_high')
        c = next(c for c in d['ranking'] if c['id'] == 'oss_high')
        self.assertEqual((c['excluded'], c['duration_hours'], c['expected_hours']), ([], 2.0, 2.0))
        self.assertTrue(any('no Time/Task for gpt-oss-120b at high' in n and 'dispatched because it was named' in n for n in c['notes']), c['notes'])


class LaunchBlockersTests(unittest.TestCase):
    """The capacities a window is priced on are the overlaid data's: raw accounts are overlaid per call, and a caller that
    overlays once gets the same answer; a purpose-limited channel or free starter allowance never blocks."""

    @classmethod
    def setUpClass(cls):
        cls.fleet = Fleet()

    def test_raw_and_overlaid_accounts_give_the_same_blockers(self):
        raw = copy.deepcopy(self.fleet.accounts)
        overlaid = axis_scores.five_axis_overlay(copy.deepcopy(raw))
        bundle = {'account': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'high', 'bundle_id': 'sol_high'}
        for used in (0.1, 0.999):
            usage = {'accounts': {'codex': {'windows': [{'id': 'usage', 'remaining_pct_before_reset': 100.0 * (1 - used),
                                                          'seconds_until_reset': 3600}]}}}
            blockers = quota_pools.launch_blockers(bundle, overlaid, usage, NOW)
            self.assertEqual(quota_pools.launch_blockers(bundle, raw, usage, NOW), blockers, used)
            self.assertEqual(bool(blockers), used > 0.9, used)         # not vacuous: a used-up window blocks, a fresh one does not

    def test_an_unmetered_or_free_pool_account_never_blocks_a_launch(self):
        overlaid = axis_scores.five_axis_overlay(copy.deepcopy(self.fleet.accounts))
        for flag in ('unmetered', 'five_axis_free_pool'):
            accounts = copy.deepcopy(overlaid)
            accounts['accounts']['codex'][flag] = True
            usage = {'accounts': {'codex': {'windows': [{'id': 'usage', 'remaining_pct_before_reset': 0.0, 'seconds_until_reset': 3600}]}}}
            self.assertEqual(quota_pools.launch_blockers({'account': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'high'}, accounts, usage, NOW), [], flag)


class ChatGPTWebCadenceTests(unittest.TestCase):
    """Owner decision (2026-11-11): ChatGPT Web's cadence window (5 per 12h in accounts.json) is priced like any other quota
    window from the current-period used count and time left in the cadence period; Dot stays unmetered as-is."""

    @classmethod
    def setUpClass(cls):
        cls.accounts = sim.fresh_accounts(shipped_template(), snapshot())

    def _plan(self, *, claude_plan='pro', codex_plan='plus', claude_used=0.15, codex_used=0.15,
              claude_reset_h=84.0, codex_reset_h=84.0, seats_used=0, cadence_reset_h=9.0,
              divergence=1, hours=0.3, local_only=True, records=None):
        acc = copy.deepcopy(self.accounts)
        acc['accounts']['claude']['plan'] = claude_plan
        acc['accounts']['codex']['plan'] = codex_plan
        usage = sim.snapshot(sim.synthetic_document(acc, NOW, sim.STATES['ample']), acc, NOW)
        usage['accounts']['claude']['windows'] = [
            {'id': 'seven_day', 'remaining_pct_before_reset': 100.0 * (1.0 - claude_used), 'seconds_until_reset': claude_reset_h * 3600},
            {'id': 'five_hour', 'remaining_pct_before_reset': 95.0, 'seconds_until_reset': 5 * 3600},
        ]
        if claude_plan == 'max_20x':
            usage['accounts']['claude']['windows'].insert(
                1, {'id': 'weekly_fable', 'remaining_pct_before_reset': 100.0 * (1.0 - claude_used), 'seconds_until_reset': claude_reset_h * 3600}
            )
        usage['accounts']['codex']['windows'] = [
            {'id': 'usage', 'remaining_pct_before_reset': 100.0 * (1.0 - codex_used), 'seconds_until_reset': codex_reset_h * 3600},
        ]
        if records is None:
            if seats_used == 0 and cadence_reset_h is not None:
                usage['accounts'].setdefault('chatgpt-web', {})['windows'] = [
                    {'id': 'cadence', 'used': 0, 'seconds_until_reset': cadence_reset_h * 3600},
                ]
                recs = []
            else:
                age_h = 12.0 - cadence_reset_h
                recs = [
                    {'stage': 'record', 'dispatch_id': f'cw-{i}', 'choice': 'pro_max',
                     'bundle': {'account': 'chatgpt-web', 'model': 'gpt-6-pro'},
                     'at': (NOW - timedelta(hours=age_h) + timedelta(minutes=5 * i)).isoformat()}
                    for i in range(seats_used)
                ]
        else:
            recs = records
        profile = {
            'reasoning_depth': divergence, 'code_complexity': 0,
            'axis_demand': {'divergence': divergence, 'coding': 0, 'aesthetics': 0, 'tool_operation': 0},
            'needs_visual': False, 'needs_gui': False, 'local_only': local_only,
            'wants_fast': False, 'read_only_review': True,
            'interaction_style': 'advisory', 'multi_task': False, 'task_count': '1',
            'expected_hours': hours,
        }
        return allocator.plan(
            {'objective': 'research', 'hours': hours, 'profile': profile},
            usage, NOW, accounts=acc, overrides=[], records=recs,
            host_policy_config={'allowed_accounts': ['claude', 'codex', 'chatgpt-web', 'dot']},
        )

    def test_l1_local_research_with_4_of_5_seats_used_does_not_pick_pro_max_and_c_rises_with_used_seats(self):
        costs = []
        for seats in range(5):
            d = self._plan(claude_plan='pro', codex_plan='plus', claude_used=0.35, codex_used=0.35,
                           seats_used=seats, cadence_reset_h=9.0, divergence=1, hours=0.3, local_only=True)
            cw = window_reports(d)['chatgpt-web/cadence']
            self.assertEqual(cw['capacity_usd'], 5.0)
            self.assertEqual(cw['used_usd'], float(seats))
            pro = row(d, 'pro_max')
            costs.append(pro['components']['opportunity_cost'])
        self.assertLess(costs[0], 5e-3)
        self.assertTrue(all(b > a for a, b in zip(costs, costs[1:])), costs)

        # Monotonicity in used seats: with all else equal, one more used seat never lowers pro_max's C and never
        # raises its rank. Asserted as a property, not as which bundle wins.
        for cl_plan, cx_plan, used in (('pro', 'plus', 0.10), ('pro', 'plus', 0.35), ('max_20x', 'pro', 0.10)):
            prev = None
            for seats in range(5):
                d = self._plan(claude_plan=cl_plan, codex_plan=cx_plan, claude_used=used, codex_used=used,
                               seats_used=seats, cadence_reset_h=9.0, divergence=1, hours=0.3, local_only=True)
                ids = [c['id'] for c in eligible(d)]
                pro = row(d, 'pro_max')
                now_ = (pro['components']['opportunity_cost'], ids.index('pro_max') if 'pro_max' in ids else len(ids))
                if prev is not None:
                    self.assertGreaterEqual(now_[0] + 1e-12, prev[0], (cl_plan, cx_plan, used, seats))
                    self.assertGreaterEqual(now_[1], prev[1], (cl_plan, cx_plan, used, seats))
                prev = now_

        # Dot stays unmetered (no cadence window, zero opportunity cost).
        d_remote = self._plan(claude_plan='pro', codex_plan='plus', claude_used=0.35, codex_used=0.35,
                              seats_used=4, cadence_reset_h=9.0, divergence=1, hours=0.3, local_only=False)
        dot_row = row(d_remote, 'dot_text')
        self.assertEqual(dot_row['components']['opportunity_cost'], 0.0)
        self.assertNotIn('dot/cadence', window_reports(d_remote))

    def test_zero_of_five_used_near_end_of_cadence_period_follows_expiring_quota_speed_rule(self):
        # 10 minutes left in the 12h cadence period with 0 of 5 seats used: C(pro_max) is near zero (~0).
        # When local quota is also expiring (2h to weekly reset, C ≈ 0), candidates tie on N and the existing
        # expiring-quota speed rule breaks the tie by completion_hours (AA Time/Task, +fast where available).
        d_tie = self._plan(claude_plan='max_20x', codex_plan='pro', claude_used=0.50, codex_used=0.50,
                           claude_reset_h=2.0, codex_reset_h=84.0,
                           seats_used=0, cadence_reset_h=1 / 6, divergence=1, hours=0.3, local_only=True)
        pro_tie = row(d_tie, 'pro_max')
        opus_fast = row(d_tie, 'opus55_medium', fast=True)
        self.assertLess(pro_tie['components']['opportunity_cost'], 5e-5)
        self.assertLess(opus_fast['components']['opportunity_cost'], 5e-5)
        self.assertEqual(round(pro_tie['score'], 4), round(opus_fast['score'], 4))
        self.assertLess(opus_fast['duration_hours'], pro_tie['duration_hours'])
        self.assertEqual(candidate_ranking.five_axis_deciding_key(opus_fast, pro_tie), 'completion_hours')
        self.assertTrue(d_tie['selected_bundle'].get('fast'))
        ids = [c['id'] for c in eligible(d_tie)]
        self.assertLess(ids.index(opus_fast['id']), ids.index('pro_max'))


if __name__ == '__main__':
    unittest.main()
