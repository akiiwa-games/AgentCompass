"""Task demand (reasoning depth x property difficulty) is matched against model ability x its own effort curve."""
import copy
from datetime import datetime, timedelta, timezone
import json
import unittest

import allocator
from router_test_support import isolated_router_state, usage_windows


NOW = datetime(2026, 10, 29, 3, 0, tzinfo=timezone.utc)
WORKING = {'reasoning_depth': 2, 'code_complexity': 2, 'context_scale': 1, 'hallucination_risk': 2, 'error_cost': 2,
           'needs_visual': False, 'needs_gui': False, 'local_only': True, 'interaction_style': 'engineering',
           'reason': 'test profile'}
LIGHT = dict(WORKING, reasoning_depth=1, code_complexity=1, hallucination_risk=1, error_cost=1)
HEAVY_FRONTIER = dict(WORKING, reasoning_depth=4, code_complexity=3, context_scale=3, hallucination_risk=3, error_cost=3)


def quota(codex=(80, 100), claude=(80, 100), reset_codex=None):
    """Healthy weekly pools; `codex`/`claude` are (remaining %, hours to reset)."""
    return usage_windows(NOW, {
        'claude': [('five_hour', 90, 3), ('seven_day', claude[0], claude[1]), ('weekly_fable', 100, 100)],
        'claude-lab': [('five_hour', 90, 3), ('seven_day', claude[0], claude[1]), ('weekly_fable', 100, 100)],
        'codex': [('usage', codex[0], reset_codex or codex[1])],
        'antigravity': [('gemini-5h', 90, 3), ('gemini-weekly', 80, 100), ('3p-5h', 90, 3), ('3p-weekly', 80, 100)],
    })


class EffortMatchingTests(unittest.TestCase):
    def setUp(self):
        isolated_router_state(self.addCleanup)
        self.accounts = allocator.load_accounts()
        self.policy = self.accounts['scoring']

    def plan(self, profile, usage=None, accounts=None, hours=1, **task):
        body = {'work_kind': 'general_implementation', 'profile': profile, 'expected_hours': hours,
                'needs': {'local_only': True}, 'disable_fast': True, **task}
        return allocator.plan(body, usage or quota(), NOW, accounts=accounts or copy.deepcopy(self.accounts), overrides=[])

    @staticmethod
    def rows(plan):
        return {row['id']: row for row in plan['ranking']}

    def demand(self, profile, **kw):
        return self.plan(profile, **kw)['decision']['task']['demand']

    # ---- task side -------------------------------------------------------------------------------------------

    def test_required_ability_is_continuous_in_depth_and_every_property(self):
        base = self.demand(WORKING)
        self.assertEqual(base['source'], 'profile')
        self.assertAlmostEqual(base['required_ability'], base['floor'] + base['property_extra'], places=3)
        # Depth multiplies the properties: the same property load asks more at a higher depth, and each property
        # moves the requirement by its own weight without a step at any level.
        by_depth = [self.demand(dict(WORKING, reasoning_depth=d))['required_ability'] for d in (1, 2, 3, 4)]
        self.assertEqual(by_depth, sorted(by_depth))
        self.assertEqual(len(set(by_depth)), 4)
        for prop, levels in (('code_complexity', (0, 1, 2, 3)), ('context_scale', (1, 2, 3)), ('divergence', (1, 2, 3)),
                             ('hallucination_risk', (1, 2, 3)), ('error_cost', (1, 2))):
            values = [self.demand(dict(WORKING, **{prop: level}))['required_ability'] for level in levels]
            self.assertEqual(values, sorted(values), prop)
            self.assertGreater(values[-1], values[0], prop)
        light, heavy = self.demand(dict(WORKING, reasoning_depth=3, code_complexity=0, hallucination_risk=1, error_cost=1)), \
            self.demand(dict(WORKING, reasoning_depth=3, code_complexity=3, context_scale=3, divergence=3, hallucination_risk=3))
        self.assertGreater(heavy['property_extra'], light['property_extra'] * 3)
        # The same heavy properties ask more of a deep task than of a shallow one (depth x property, not depth alone).
        shallow = self.demand(dict(heavy_props(), reasoning_depth=1))
        deep = self.demand(dict(heavy_props(), reasoning_depth=4))
        self.assertGreater(deep['property_extra'], shallow['property_extra'] * 3.9)

    def test_critical_and_precision_work_raise_the_requirement_and_work_without_profile_uses_its_difficulty(self):
        plain = self.demand(WORKING)
        critical = self.demand(dict(WORKING, error_cost=3))
        self.assertAlmostEqual(critical['critical_headroom'], self.policy['critical_headroom'])
        self.assertGreater(critical['required_ability'] - plain['required_ability'], self.policy['critical_headroom'] - 1e-6)
        precise = self.demand(dict(WORKING, reasoning_depth=3, hallucination_risk=3, divergence=2, code_complexity=2))
        self.assertGreater(precise['precision_headroom'], 0)
        for difficulty, depth in (('routine', 1), ('hard', 2), ('coupled', 3), ('frontier', 4)):
            plan = allocator.plan({'work_kind': 'general_implementation', 'difficulty': difficulty, 'needs': {'local_only': True}},
                                  quota(), NOW, accounts=copy.deepcopy(self.accounts), overrides=[])
            demand = plan['decision']['task']['demand']
            self.assertEqual((demand['source'], demand['reasoning_depth']), ('difficulty', depth))
            self.assertAlmostEqual(demand['property_load'], 0.5)
        self.assertEqual(self.policy['demand']['property_span'], 0.5)

    def test_every_property_requirement_grows_with_depth_around_depth_two(self):
        needs = {d: allocator.profile_requirements(dict(WORKING, reasoning_depth=d, code_complexity=3, context_scale=3,
                                                         hallucination_risk=3), self.policy) for d in (1, 2, 3, 4)}
        for dim in ('coding', 'exploration', 'anti_hallucination'):
            levels = [needs[d][dim][0] for d in (1, 2, 3, 4)]
            self.assertEqual(levels, sorted(levels), dim)
            self.assertLess(levels[0], levels[1])
        self.assertEqual(allocator.profile_requirements(WORKING, self.policy)['coding'][0], 0.65)    # depth 2 is the table

    # ---- model side -------------------------------------------------------------------------------------------

    def test_effective_ability_is_tier_times_the_models_own_curve(self):
        curves = self.accounts['effort_curves']
        opus, sonnet = self.accounts['models']['claude-opus-5-5'], self.accounts['models']['claude-sonnet-5-5']
        for model, info in (('claude-opus-5-5', opus), ('claude-sonnet-5-5', sonnet), ('gpt-6-astra', self.accounts['models']['gpt-6-astra']),
                            ('gpt-6.1-sol', self.accounts['models']['gpt-6.1-sol']),
                            ('gemini-4-argon-high', self.accounts['models']['gemini-4-argon-high']),
                            ('claude-fable-5-1', self.accounts['models']['claude-fable-5-1'])):
            self.assertIn('calibration_source', curves[model], model)
            high = allocator.effort_ability(self.policy, curves[model], info, 'high')
            self.assertAlmostEqual(high, info['tier'])
            ability = [allocator.effort_ability(self.policy, curves[model], info, e) for e in ('low', 'medium', 'high', 'xhigh', 'max')
                       if e in curves[model]['ability']]
            self.assertEqual(ability, sorted(ability), model)
        # Models respond to effort differently: Sonnet gains more from Max than Sol, and pays far more for it.
        gain = lambda model, effort: allocator.effort_ability(self.policy, curves[model], self.accounts['models'][model], effort) \
            - self.accounts['models'][model]['tier']
        burn = lambda model, effort: allocator.burn_scale(self.policy, curves[model], effort)
        self.assertGreater(gain('claude-sonnet-5-5', 'max'), gain('gpt-6.1-sol', 'max') * 3)
        self.assertGreater(burn('claude-sonnet-5-5', 'max'), burn('gpt-6.1-sol', 'max') * 2.5)
        self.assertAlmostEqual(burn('claude-opus-5-5', 'high'), 1.0)
        self.assertGreater(burn('claude-opus-5-5', 'max'), 3.0)
        self.assertLess(gain('claude-opus-5-5', 'max') / self.accounts['models']['claude-opus-5-5']['tier'], 0.1)   # ~4x burn, ~9% ability

    def test_curves_accept_multiplier_forms(self):
        info = {'tier': 4.0}
        curves = {'ability_mult': {'medium': 0.9, 'high': 1.0, 'max': 1.5}, 'burn_mult': {'medium': 0.5, 'max': 4.0}}
        self.assertAlmostEqual(allocator.effort_ability(self.policy, curves, info, 'max'), 6.0)
        self.assertAlmostEqual(allocator.effort_ability(self.policy, curves, info, 'medium'), 3.6)
        self.assertAlmostEqual(allocator.effort_ability(self.policy, curves, info, 'xhigh'), 4.0 + self.policy['effort_ability']['xhigh'])
        self.assertEqual(allocator.burn_scale(self.policy, curves, 'max'), 4.0)
        self.assertEqual(allocator.burn_scale(self.policy, curves, 'medium'), 0.5)
        self.assertAlmostEqual(allocator.burn_scale(self.policy, {}, 'xhigh'), self.policy['effort_burn']['xhigh'] / self.policy['effort_burn']['high'])
        spec = {'billing': 'subscription', 'windows': {'w': {'hours': 168}}, 'capacity_usd': {'w': 100}}
        high = allocator.unit_rate(spec, 'w', 'm', 'high', self.policy, curves, {'usd_per_hour': 5}, NOW)
        self.assertAlmostEqual(allocator.unit_rate(spec, 'w', 'm', 'max', self.policy, curves, {'usd_per_hour': 5}, NOW) / high, 4.0)
        self.assertAlmostEqual(allocator.bundle_task_usd(spec, 'm', 'max', 2, self.policy, curves, {'usd_per_hour': 5})
                               / allocator.bundle_task_usd(spec, 'm', 'high', 2, self.policy, curves, {'usd_per_hour': 5}), 4.0)
        caps = allocator.effort_scaled_capabilities({'tier': 4.0, 'capabilities': {'reasoning': 0.8, 'visual': 0.7}}, self.policy, curves, 'medium')
        self.assertAlmostEqual(caps['reasoning'], 0.72)       # thinking dimensions scale with the effort coefficient
        self.assertAlmostEqual(caps['visual'], 0.7)           # seeing does not

    # ---- the match --------------------------------------------------------------------------------------------

    def test_strong_model_medium_serves_moderate_work_when_quota_is_tight(self):
        tight = quota(claude=(25, 100), codex=(25, 100))
        plan = self.plan(dict(WORKING, reasoning_depth=3, code_complexity=2), tight)
        rows = self.rows(plan)
        self.assertEqual(rows['opus55_medium']['excluded'], [])
        self.assertGreaterEqual(rows['opus55_medium']['components']['ability_value'], rows['opus55_medium']['components']['required_ability'])
        self.assertGreater(rows['opus55_medium']['score'], rows['opus55_high']['score'])
        self.assertGreater(rows['opus55_high']['score'], rows['opus55_xhigh']['score'])
        self.assertEqual(plan['selected_bundle']['effort'], 'medium')
        # The same medium bundle is not available to work it cannot serve: frontier floor 4.2 vs Sol 3.6 - 0.2.
        frontier = self.rows(self.plan(HEAVY_FRONTIER, tight))
        self.assertTrue(any('floor' in reason for reason in frontier['sol_medium']['excluded']))

    def test_demanding_work_selects_the_effort_that_saturates_the_requirement(self):
        healthy = self.plan(HEAVY_FRONTIER)
        rows = self.rows(healthy)
        required = healthy['decision']['task']['demand']['required_ability']
        self.assertEqual(healthy['choice'], 'opus55_max')
        self.assertEqual(healthy['decision']['task']['max_recommended_effort'], 'max')
        self.assertGreaterEqual(rows['opus55_max']['components']['ability_value'], required)
        for lower in ('opus55_xhigh', 'opus55_high', 'opus55_medium'):
            self.assertLess(rows[lower]['components']['ability_value'], required)
            if rows[lower]['excluded']:        # critical work: below the floor plus headroom (4.5) is out, not just worse
                self.assertTrue(any('ability' in reason for reason in rows[lower]['excluded']))
                continue
            self.assertGreater(rows[lower]['components']['shortfall'], 0)
            self.assertGreater(rows['opus55_max']['score'], rows[lower]['score'])
        self.assertEqual(rows['opus55_xhigh']['excluded'], [])
        self.assertTrue(rows['opus55_medium']['excluded'])
        # Quota that cannot afford 3.3x burn steps down to the cheapest effort that still comes closest.
        tight = self.plan(HEAVY_FRONTIER, quota(claude=(25, 100)))
        self.assertIn(tight['selected_bundle']['effort'], ('high', 'xhigh'))
        self.assertLess(allocator.EFFORT_ORDER.index(tight['selected_bundle']['effort']), allocator.EFFORT_ORDER.index('max'))

    def test_moderate_work_never_pays_for_xhigh_or_max(self):
        for profile in (LIGHT, WORKING, dict(WORKING, reasoning_depth=3, code_complexity=1, hallucination_risk=1, error_cost=1)):
            for usage in (quota(), quota(claude=(95, 100), codex=(95, 100))):
                plan = self.plan(profile, usage)
                self.assertNotIn(plan['selected_bundle'].get('effort'), ('xhigh', 'max'), profile)
                rows = self.rows(plan)
                for model_high, model_up in (('sol_high', 'sol_max'), ('opus55_high', 'opus55_max')):
                    self.assertGreater(rows[model_high]['score'], rows[model_up]['score'])

    def test_expiring_surplus_quota_raises_the_thinking_effort(self):
        profile = dict(WORKING, reasoning_depth=3, code_complexity=1, hallucination_risk=1, error_cost=1)
        calm = self.plan(profile, quota(codex=(70, 100), claude=(30, 100)), hours=2)
        sprint_usage = quota(codex=(70, 100), claude=(30, 100), reset_codex=3)
        sprint = self.plan(profile, sprint_usage, hours=2)
        order = allocator.EFFORT_ORDER.index
        self.assertEqual(sprint['selected_bundle']['account'], 'codex')
        self.assertGreater(order(sprint['selected_bundle']['effort']), order(calm['selected_bundle']['effort']))
        row = self.rows(sprint)[sprint['choice']]['components']
        self.assertIn('sprint_quality', row)
        self.assertGreater(row['reset_bonus'], 0.5)
        # The extra burn that would otherwise be charged is waived in proportion to the window's sunk weight.
        calm_up = self.rows(calm)['sol_max']['components']['effort_penalty']
        sprint_up = self.rows(sprint)['sol_max']['components'].get('effort_penalty', 0)
        self.assertLess(sprint_up, calm_up * 0.25)

    def test_calibration_changes_the_choice_without_touching_the_task_side(self):
        """Opus Max at 4x the burn is picked only when its curve says the extra ability is real."""
        weak, strong = copy.deepcopy(self.accounts), copy.deepcopy(self.accounts)
        for accounts, ability in ((weak, 0.05), (strong, 0.6)):
            accounts['effort_curves']['claude-opus-5-5']['ability']['max'] = ability
            accounts['effort_curves']['claude-opus-5-5']['burn']['max'] = 0.7 * 4.0
        usage = quota(codex=(25, 100))
        for accounts, expected in ((weak, False), (strong, True)):
            plan = self.plan(HEAVY_FRONTIER, usage, accounts=accounts)
            self.assertEqual(plan['selected_bundle']['model'] == 'claude-opus-5-5' and plan['selected_bundle']['effort'] == 'max', expected)
            row = self.rows(plan)['opus55_max']
            self.assertAlmostEqual(row['expected_usd'] / self.rows(plan)['opus55_high']['expected_usd'], 4.0, places=1)

    def test_no_bundle_is_tied_to_a_reasoning_depth(self):
        accounts = allocator.load_accounts()
        for path in (allocator.REFERENCES / 'accounts.json', allocator.reference_fleet_path()):
            document = json.loads(path.read_text(encoding='utf-8-sig'))
            for owner, spec in document['accounts'].items():
                for bundle_id, bundle in (spec.get('bundles') or {}).items():
                    self.assertNotIn('max_reasoning_depth', bundle.get('when') or {}, f'{path.name}:{owner}:{bundle_id}')
        for bundle_id in ('sol_medium', 'pi_sol_medium', 'agy_opus55_low', 'agy_opus55_medium', 'opus55_medium'):
            self.assertNotIn('when', allocator.bundles(accounts)[bundle_id])
        # An older local fleet that still carries the rule keeps working: the rule is honoured when present.
        legacy = copy.deepcopy(self.accounts)
        legacy['accounts']['codex']['bundles']['sol_medium']['when'] = {'max_reasoning_depth': 1}
        self.assertNotIn('sol_medium', self.rows(self.plan(dict(WORKING, reasoning_depth=3), accounts=legacy)))
        self.assertIn('sol_medium', self.rows(self.plan(dict(WORKING, reasoning_depth=3))))


def heavy_props():
    return dict(WORKING, code_complexity=3, context_scale=3, divergence=3, hallucination_risk=3, error_cost=2)


class ShippedBaselineTests(unittest.TestCase):
    """The checked-in fleets carry the calibrated baseline, not the flat 3.3 prior."""

    def documents(self):
        return {path.name: json.loads(path.read_text(encoding='utf-8-sig'))
                for path in (allocator.REFERENCES / 'accounts.json', allocator.reference_fleet_path())}

    def test_models_and_curves_are_calibrated_in_both_fleets(self):
        expected_tier = {'claude-opus-5-5': 4.6, 'gpt-6-astra': 4.2, 'gemini-4-argon-high': 4.0, 'claude-sonnet-5-5': 3.9,
                         'gpt-6.1-sol': 3.6, 'grok-4.7': 3.4, 'gemini-3.8-flash-high': 3.0, 'deepseek-v4.1-flash-free': 2.8}
        for name, document in self.documents().items():
            self.assertEqual(document['scoring']['reset_bonus_max'], 1.2, name)
            tiers = {model: spec['tier'] for model, spec in document['models'].items() if isinstance(spec, dict)}
            self.assertGreater(len(set(tiers.values())), 5, name)
            for model, tier in expected_tier.items():
                self.assertEqual(tiers[model], tier, (name, model))
            for model in ('claude-opus-5-5', 'claude-sonnet-5-5', 'claude-fable-5-1', 'gpt-6-astra', 'gpt-6.1-sol', 'gemini-4-argon-high'):
                curve = document['effort_curves'][model]
                self.assertTrue(curve['calibration_source'], (name, model))
                self.assertEqual(curve['ability']['high'] if 'high' in curve['ability'] else 0.0, 0.0)
            demand = document['scoring']['demand']
            self.assertEqual(set(demand['property_weights']),
                             {'code_complexity', 'context_scale', 'divergence', 'hallucination_risk', 'error_cost'})
            for key, value in allocator.DEMAND_DEFAULTS.items():
                self.assertEqual(demand[key], value, (name, key))

    def test_shipped_template_matches_the_fixture_where_the_baseline_is_shared(self):
        template, fixture = (self.documents()[n] for n in ('accounts.json', 'reference-accounts.json'))
        self.assertEqual(template['models'], fixture['models'])
        self.assertEqual(template['effort_curves'], fixture['effort_curves'])
        self.assertEqual(template['scoring']['demand'], fixture['scoring']['demand'])
        self.assertEqual(template['scoring']['extra_effort_penalty'], fixture['scoring']['extra_effort_penalty'])
        for bundle in ('opus55_medium', 'opus55_max', 'sonnet55_medium'):
            self.assertIn(bundle, template['accounts']['claude']['bundles'])


if __name__ == '__main__':
    unittest.main()
