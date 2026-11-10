"""Quota economy v3 (round 13; advisor report v3 with the owner's D1-D3): the cost of a candidate is the integral of the
window's quota price over its own spend, N = F - cost, the price comes from the account's own value ladder and the period's
own need probability, substitutes enter only through their own slack, and no 5-hour window ever prices."""
import copy
from datetime import datetime, timezone
import math
import random
import sys
import unittest
from unittest.mock import patch

import allocator
import route_simulation as sim
from router_test_support import isolated_router_state
from test_fleet_parity import shipped_template, snapshot


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup, discovery=False, policy=False)


NOW = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)
WEEK = 168.0
PROFILE = {'reasoning_depth': 2, 'code_complexity': 2, 'context_scale': 1, 'divergence': 1, 'hallucination_risk': 2,
           'error_cost': 2, 'needs_visual': False, 'needs_gui': False, 'local_only': True, 'realtime_loop': False,
           'wants_fast': False, 'read_only_review': False, 'interaction_style': 'engineering', 'multi_task': False,
           'task_count': '1', 'expected_hours': 1.0}
MID, FRONTIER, CREATIVE = (2, 3, 2, 0), (2, 3, 3, 0), (3, 1, 0, 0)


def rec_of(capacity=1000.0, used_frac=0.5, elapsed=0.5, reset_hours=None, period=WEEK):
    """A window record as `five_axis_window_recovery` returns it."""
    return {'capacity_usd': capacity, 'used_usd': capacity * used_frac, 'elapsed_frac': elapsed,
            'line_now_usd': capacity * elapsed, 'period_hours': period,
            'reset_hours': (1 - elapsed) * period if reset_hours is None else reset_hours, 'unstarted': False}


def ladder_for(spend_scale=1.0, with_outside=0.0):
    """A realistic ladder: three models (cheap weak, middle, dear strong) over the real demand grid and their real shape."""
    models = [(0.29 * spend_scale, {'divergence': 0.5, 'global_rigor': 0.5, 'module_precision': 0.45, 'vision_tools': 0.5}),
              (12.59 * spend_scale, {'divergence': 0.6, 'global_rigor': 0.65, 'module_precision': 0.6, 'vision_tools': 0.7}),
              (22.86 * spend_scale, {'divergence': 0.73, 'global_rigor': 0.8, 'module_precision': 0.8, 'vision_tools': 0.8})]
    hulls = []
    for d in allocator.FIVE_AXIS_DEMAND_GRID:
        targets = allocator.five_axis_targets(d)
        pts = [(s, allocator.five_axis_fit(sc, d, targets, 1.0)[0]) for s, sc in models]
        hulls.append(allocator.five_axis_upper_hull(pts, with_outside * max(f for _, f in pts)))
    return allocator.five_axis_value_ladder(hulls)


class Fleet:
    """Claude Max20 and Codex Pro on the shipped public data, each window on its own clock; the cheap Haiku and Luna lines
    are added as bundles so the ladders have the same three rungs the advisor's probe had."""

    def __init__(self):
        self.accounts = sim.fresh_accounts(shipped_template(), snapshot())

    def plan(self, claude_used, codex_used, ec=0.5, ex=0.5, axes=MID, hours=2.0, allowed=('claude', 'codex'), claude2=None,
             five_hour_used=0.05, mutate=None, codex_reading=True, **task):
        acc = copy.deepcopy(self.accounts)
        acc['scoring']['five_axis'] = {'enabled': True}
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
        usage['accounts']['codex']['windows'] = [{'id': 'usage', **week(codex_used, ex)}] if codex_reading else []
        if claude2 is not None:
            used2, e2 = claude2
            usage['accounts']['claude-2']['windows'] = [
                {'id': 'seven_day', **week(used2, e2)},
                {'id': 'five_hour', 'remaining_pct_before_reset': 100.0 * (1 - five_hour_used), 'seconds_until_reset': 5 * 3600}]
        if 'codex' not in allowed:
            usage['accounts'].pop('codex', None)
        profile = {**PROFILE, 'axis_demand': dict(zip(allocator.FIVE_AXES, axes)), 'expected_hours': hours}
        return allocator.plan({'objective': 't', 'hours': hours, 'profile': profile, **task}, usage, NOW, accounts=acc,
                              overrides=[], records=[], host_policy_config={'allowed_accounts': list(allowed)})


def eligible(decision):
    return [c for c in decision['ranking'] if not c.get('excluded') and c.get('score') is not None]


def row(decision, bundle_id, fast=False):
    return next(c for c in eligible(decision) if c['id'] == bundle_id and bool(c['bundle'].get('fast')) == fast)


def price_of(decision, account):
    return next(c['components']['quota_price'] for c in eligible(decision) if c['bundle']['account'] == account)


class NeedTailTests(unittest.TestCase):
    def test_the_incomplete_beta_matches_known_values(self):
        beta = allocator.regularized_incomplete_beta
        self.assertAlmostEqual(beta(2.509, 4.763, 0.4312), 0.7053, places=3)      # the advisor's worked tail
        self.assertAlmostEqual(beta(1.0, 1.0, 0.3), 0.3, places=9)                  # uniform
        self.assertAlmostEqual(beta(3.0, 1.0, 0.5), 0.125, places=9)                # x^a when b = 1
        self.assertAlmostEqual(beta(1.0, 4.0, 0.2), 1 - 0.8 ** 4, places=9)
        self.assertEqual(beta(2.0, 3.0, 0.0), 0.0)
        self.assertEqual(beta(2.0, 3.0, 1.0), 1.0)
        self.assertAlmostEqual(beta(7.0, 2.0, 0.3) + beta(2.0, 7.0, 0.7), 1.0, places=9)
        self.assertEqual(beta(4000.0, 3.0, 0.4), 0.0)       # underflow side

    def test_on_the_line_the_mean_demand_to_come_equals_the_remaining_capacity(self):
        """Advisor test 5 (line identity): at U = e C the predicted demand still to come, (U + C)(1 - e)/(e + 1), is exactly
        C - U, so the recovery line is where the expected demand just fills the period. With the single-window lump q = C
        (owner D3) the distribution is skewed, so the probability pi = 1/2 contour sits below the line, near 0.73 e C; pi
        is still below 1/2 well under the line and above it on the line."""
        rng = random.Random(7)
        for _ in range(25):
            cap, e = rng.uniform(5.0, 5000.0), rng.uniform(0.05, 0.95)
            rec = rec_of(cap, e, e)
            mean = cap * sum(allocator.five_axis_need_tail(rec, r * cap) for r in range(1, 400))
            # the integer-lump mean of the continuous-extension tail is the negative-binomial mean (U + C)(1 - e)/(e + 1)
            self.assertAlmostEqual(mean / (cap * (1 - e)), 1.0, delta=0.03)
            self.assertLess(allocator.five_axis_need_probability(rec_of(cap, 0.5 * e, e), 0.5 * e * cap), 0.5)
            self.assertGreater(allocator.five_axis_need_probability(rec_of(cap, e, e), e * cap), 0.5)
            self.assertAlmostEqual((e * cap + cap) * (1 - e) / (e + 1), cap - e * cap, places=9)

    def test_the_tail_grows_with_use_falls_toward_the_reset_and_is_a_probability(self):
        previous = -1.0
        for u in range(0, 100, 2):
            p = allocator.five_axis_need_probability(rec_of(1000.0, u / 100, 0.5), u * 10.0)
            self.assertTrue(0.0 <= p <= 1.0)
            self.assertGreaterEqual(p, previous - 1e-12)
            previous = p
        late = [allocator.five_axis_need_probability(rec_of(1000.0, 0.5, e), 500.0) for e in (0.2, 0.5, 0.8, 0.95, 0.999)]
        self.assertEqual(late, sorted(late, reverse=True))
        self.assertLess(late[-1], 0.05)
        self.assertEqual(allocator.five_axis_need_tail(rec_of(1000.0, 0.5, 1.0), 100.0), 0.0)     # the reset is now
        self.assertEqual(allocator.five_axis_need_tail(rec_of(1000.0, 0.5, 0.5), 0.0), 1.0)       # no room left: sure

    def test_a_slack_reads_only_its_own_window(self):
        tight, loose = rec_of(1000.0, 0.95, 0.5), rec_of(1000.0, 0.05, 0.5)
        self.assertLess(allocator.five_axis_substitute_slack(tight, 10.0), 0.1)
        self.assertGreater(allocator.five_axis_substitute_slack(loose, 10.0), 0.6)
        self.assertEqual(allocator.five_axis_substitute_slack(tight, 60.0), 0.0)        # the task does not even fit


class PriceTests(unittest.TestCase):
    def test_no_step_at_the_line_and_a_positive_price_below_it(self):
        """Advisor test 4: scan the use at a fixed clock; the cost of a fixed spend rises strictly, is positive under the line
        and the step across the line is no more than twice its neighbours; at a fixed use it falls to zero toward the reset."""
        ladder, spend, costs = ladder_for(), 22.86, []
        for u in range(1, 100):
            costs.append(allocator.five_axis_spend_cost(rec_of(1704.5, u / 100, 0.5), ladder, spend, 1.0)['cost'])
        self.assertTrue(all(b > a for a, b in zip(costs, costs[1:])))
        self.assertGreater(costs[20], 0.0)           # 21% used at the half-way clock: below the line, still not free
        steps = [b - a for a, b in zip(costs, costs[1:])]
        across = steps[49]                           # 50% -> 51%
        self.assertLessEqual(across, 2 * max(steps[48], steps[50]))
        later = [allocator.five_axis_spend_cost(rec_of(1704.5, 0.6, e), ladder, spend, 0.5 * (1 - e) * WEEK)['cost']
                 for e in (0.3, 0.5, 0.7, 0.9, 0.99, 0.9999)]     # the task ends before the reset each time
        self.assertEqual(later, sorted(later, reverse=True))
        self.assertLess(later[-1], 0.01)

    def test_units_do_not_matter_and_the_task_length_cancels(self):
        """Advisor test 6: scaling every dollar leaves the cost unchanged; scaling the task's hours and spends scales the
        price by 1/h so h * lambda is the same."""
        base_cost = allocator.five_axis_spend_cost(rec_of(1704.5, 0.6, 0.5), ladder_for(), 22.86, 1.0)
        for k in (0.01, 3.0, 250.0):
            scaled = allocator.five_axis_spend_cost(rec_of(1704.5 * k, 0.6, 0.5), ladder_for(k), 22.86 * k, 1.0)
            self.assertAlmostEqual(scaled['cost'], base_cost['cost'], places=4)
            self.assertAlmostEqual(scaled['lambda_start'] * k, base_cost['lambda_start'], places=6)
        for hours in (0.5, 2.0, 40.0):       # h times the task's hours: the spends scale with h
            lam = allocator.five_axis_spend_cost(rec_of(1704.5, 0.6, 0.5), ladder_for(hours), 22.86 * hours, hours)['lambda_start']
            self.assertAlmostEqual(lam * hours, base_cost['lambda_start'], places=6)

    def test_the_cost_table_agrees_with_direct_integration(self):
        ladder, rec = ladder_for(), rec_of(1704.5, 0.6, 0.5)
        for spend in (0.29, 3.0, 22.86, 137.15, 457.0):
            got = allocator.five_axis_spend_cost(rec, ladder, spend, 1.0, spend_max=457.0)['cost']
            n, direct = 4000, 0.0
            for i in range(n):
                direct += allocator.five_axis_quota_price(rec, ladder, rec['used_usd'] + (i + 0.5) * spend / n) * spend / n
            self.assertAlmostEqual(got, direct, delta=max(2e-4, 0.01 * direct))

    def test_a_task_across_the_reset_pays_its_second_part_on_a_fresh_period(self):
        """Advisor test 10 (the post-reset share): 2h to the reset in a 6h task, a third of the spend before it."""
        ladder, spend, hours = ladder_for(), 137.0, 6.0
        rec = rec_of(1704.5, 0.6, 1 - 2 / WEEK, reset_hours=2.0)
        got = allocator.five_axis_spend_cost(rec, ladder, spend, hours, spend, {})
        before = allocator.five_axis_spend_cost({**rec, 'reset_hours': allocator.math.inf}, ladder, spend / 3, hours)['cost']
        fresh = {'capacity_usd': 1704.5, 'used_usd': 0.0, 'elapsed_frac': 0.0, 'reset_hours': allocator.math.inf}
        after = allocator.five_axis_spend_cost(fresh, ladder, spend * 2 / 3, hours)['cost']
        self.assertAlmostEqual(got['spend_before'], spend / 3, places=6)
        self.assertAlmostEqual(got['cost'], before + after, delta=2e-3)
        self.assertLess(got['need_start'], 0.25)         # 2h to the reset: the first dollars mostly lapse
        # the same 60% used with a full week left prices all of it in the old period and is dearer
        week_left = allocator.five_axis_spend_cost(rec_of(1704.5, 0.6, 0.5), ladder, spend, hours)['cost']
        self.assertGreater(week_left, got['cost'])

    def test_different_clocks_give_different_prices_for_the_same_use(self):
        """Advisor test 10 (desync)."""
        ladder = ladder_for()
        costs = {e: allocator.five_axis_spend_cost(rec_of(1704.5, 0.6, e), ladder, 45.0, 2.0)['cost'] for e in (0.2, 0.5, 0.8)}
        self.assertEqual(len({round(v, 6) for v in costs.values()}), 3)
        self.assertGreater(costs[0.2], costs[0.5])
        self.assertGreater(costs[0.5], costs[0.8])

    def test_the_outside_option_lowers_the_price_and_only_down_to_its_own_certainty(self):
        """Advisor test 2 (the limit): an outside certain to have room that does the work at the full fit makes the price
        zero, none at all gives the highest price, and anything between is in between."""
        rec = rec_of(1704.5, 0.6, 0.5)
        prices = [allocator.five_axis_spend_cost(rec, ladder_for(with_outside=v), 22.86, 2.0)['cost'] for v in (0.0, 0.3, 0.6, 1.0)]
        self.assertEqual(prices, sorted(prices, reverse=True))
        self.assertEqual(prices[-1], 0.0)
        self.assertGreater(prices[0], 0.0)

    def test_the_upper_hull_and_the_ladder(self):
        hull = allocator.five_axis_upper_hull([(1.0, 0.4), (3.0, 0.45), (4.0, 0.7), (9.0, 0.7), (2.0, 0.39)], 0.1)
        self.assertEqual(hull[0], (0.0, 0.1))
        self.assertEqual(hull[-1], (4.0, 0.7))        # the cheapest of the highest fits
        slopes = [(b[1] - a[1]) / (b[0] - a[0]) for a, b in zip(hull, hull[1:])]
        self.assertEqual(slopes, sorted(slopes, reverse=True))      # concave
        self.assertEqual(allocator.five_axis_upper_hull([(1.0, 0.3)], 0.5), [])
        ladder = allocator.five_axis_value_ladder([hull, hull])
        self.assertEqual(ladder['steps'][0][0], 1.0)
        self.assertEqual([s for _, s in ladder['steps']], sorted(s for _, s in ladder['steps']))
        self.assertTrue(all(0 < r <= 1 for r, _ in ladder['steps']))
        self.assertEqual(allocator.five_axis_value_ladder([[(0.0, 0.5)]])['steps'], [])

    def test_the_outside_value_walks_the_alternatives_by_fit(self):
        self.assertEqual(allocator.five_axis_outside_value([], 0.6), 0.0)
        self.assertAlmostEqual(allocator.five_axis_outside_value([(0.5, 1.0), (0.6, 1.0)], 0.6), 0.6)
        self.assertAlmostEqual(allocator.five_axis_outside_value([(0.7, 0.5), (0.4, 1.0)], 0.6), 0.5 * 0.6 + 0.5 * 0.4)
        self.assertAlmostEqual(allocator.five_axis_outside_value([(0.7, 0.0)], 0.6), 0.0)


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
            self.assertNotEqual(d['selected_bundle']['bundle_id'].split('_')[0], 'sol', used)
            self.assertEqual(d['choice'], 'haiku55_high', used)
        self.assertEqual(self.fleet.plan(0.6, 0.9, axes=FRONTIER, hours=6.0)['choice'], 'opus55_high')

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

    def test_an_expiring_sprint_takes_fast_and_a_window_ahead_of_its_line_does_not(self):
        """Advisor test 7: 2h to the reset with 40% left costs nothing and takes Opus in fast mode; 60% used at half time
        takes neither Opus nor fast."""
        sprint = self.fleet.plan(0.6, 0.5, ec=1 - 2 / WEEK)
        top = sprint['ranking'][0]
        self.assertEqual(top['bundle']['model'], 'claude-opus-5-5')
        self.assertTrue(top['bundle'].get('fast'))
        self.assertEqual(top['components']['decided_by'], 'speed')
        plain = row(sprint, top['id'], fast=False)
        self.assertLess(top['expected_hours'], plain['expected_hours'])
        self.assertAlmostEqual(top['components']['fit'], plain['components']['fit'])
        ahead = self.fleet.plan(0.6, 0.5)
        self.assertLess(top['components']['opportunity_cost'], 0.35 * row(ahead, 'opus55_high')['components']['opportunity_cost'])
        self.assertFalse(ahead['selected_bundle'].get('fast'))
        self.assertNotEqual(ahead['selected_bundle']['model'], 'claude-opus-5-5')
        # fast variants are separate candidates at their real spend, not a switch on the base one
        fast = row(sprint, top['id'], fast=True)
        self.assertGreater(fast['expected_usd'], plain['expected_usd'])

    def test_fable_5_1_cannot_beat_opus_5_5_and_only_the_speed_rule_can_swap_equal_fit(self):
        """Advisor test 8."""
        d = self.fleet.plan(0.3, 0.5, axes=FRONTIER, hours=6.0)
        opus, fable = row(d, 'opus55_high'), row(d, 'fable51_high')
        self.assertGreaterEqual(opus['components']['fit'], fable['components']['fit'])
        self.assertGreater(opus['score'], fable['score'])
        self.assertEqual(d['selected_bundle']['bundle_id'], 'opus55_high')
        creative = self.fleet.plan(0.3, 0.5, axes=CREATIVE, hours=0.3)
        o, f = row(creative, 'opus55_high'), row(creative, 'fable51_high')
        self.assertAlmostEqual(o['components']['fit'], f['components']['fit'])
        for c in eligible(creative):
            if c['components'].get('decided_by') == 'speed':
                self.assertLess(c['economy']['need_end_max'], 0.5)

    def test_a_pool_whose_reading_is_missing_is_no_substitute_and_is_named(self):
        """Advisor test 9: Codex's reading is gone. Its candidates leave automatic routing with a surfaced data error, it is
        nobody's outside point, and Claude's price is the one it has with no Codex at all."""
        d = self.fleet.plan(0.6, 0.9, codex_reading=False)
        gone = [c for c in d['ranking'] if c['bundle']['account'] == 'codex']
        self.assertTrue(gone)
        for c in gone:
            self.assertTrue(c['excluded'])
            self.assertIn(allocator.QUOTA_READING_UNAVAILABLE, c['excluded'][0])
        self.assertTrue(any(allocator.QUOTA_READING_UNAVAILABLE in w for w in d['decision']['warnings']))
        alone = self.fleet.plan(0.6, 0.9, allowed=('claude',))
        self.assertEqual(price_of(d, 'claude'), price_of(alone, 'claude'))
        self.assertEqual(d['choice'], alone['choice'])

    def test_a_five_hour_window_never_enters_a_tail_a_ladder_or_a_price(self):
        """The owner's D3: none of the economics use 5-hour windows. Whatever a 5-hour window reads, no tail, price or ladder is
        computed on it, its row is unpriced, and the answer is the same."""
        seen = []
        real = allocator.five_axis_need_tail

        def spy(rec, room):
            seen.append(rec.get('period_hours'))
            return real(rec, room)
        with patch.object(allocator, 'five_axis_need_tail', spy):
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
        json.dumps(allocator.decision_snapshot(a), allow_nan=False)
        sol = row(a, 'sol_high')
        self.assertIsNone(sol['economy']['windows'][0]['reset_in_hours'])
        self.assertGreater(sol['components']['opportunity_cost'], 0.0)
        self.assertEqual(sol['components']['opportunity_cost'], row(b, 'sol_high')['components']['opportunity_cost'])

    def test_the_snapshot_carries_what_the_page_reads(self):
        d = self.fleet.plan(0.6, 0.9)
        snap = allocator.decision_snapshot(d)
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


if __name__ == '__main__':
    unittest.main()
