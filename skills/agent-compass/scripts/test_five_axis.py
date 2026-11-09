"""The five-axis shadow path (round 047): off by default, and when on it gates by rule B and fits on the radar axes."""
import copy
from datetime import datetime, timezone
import unittest

import allocator
import route_simulation as sim
from router_test_support import isolated_router_state
from test_fleet_parity import shipped_template, snapshot


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup, discovery=False, policy=False)


NOW = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)
PROFILE = {'reasoning_depth': 2, 'code_complexity': 2, 'context_scale': 1, 'divergence': 1, 'hallucination_risk': 2,
           'error_cost': 2, 'needs_visual': False, 'needs_gui': False, 'local_only': True, 'realtime_loop': False,
           'wants_fast': False, 'read_only_review': False, 'interaction_style': 'engineering', 'multi_task': False,
           'task_count': '1', 'expected_hours': 1.0}


def scores(models, **override):
    """A five-axis document: every model fully measured unless `override` names it."""
    rows = {}
    for name, info in models.items():
        if not isinstance(info, dict):
            continue
        rows[name] = {'tier': {'value': info.get('tier', 3.0)}, 'usd_per_hour': {'value': 5.0},
                      'scores': dict.fromkeys(allocator.FIVE_AXES, 0.8), 'axes': {}}
    for name, row in override.items():
        rows[name] = row
    return {'models': rows, 'capacity_conversion': {'claude': {'seven_day': {'factor': 2.0}, 'five_hour': {'factor': 2.0}}}}


class FiveAxisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.accounts = sim.fresh_accounts(shipped_template(), snapshot())
        cls.policy = {'allowed_accounts': list(cls.accounts['accounts'])}
        cls.usage = sim.snapshot(sim.synthetic_document(cls.accounts, NOW, sim.STATES['ample']), cls.accounts, NOW)

    def plan(self, accounts, **task):
        return allocator.plan({'objective': 't', 'profile': dict(PROFILE), **task}, self.usage, NOW,
                              accounts=copy.deepcopy(accounts), overrides=[], records=[], host_policy_config=self.policy)

    def enabled(self, data, **cfg):
        acc = copy.deepcopy(self.accounts)
        acc['scoring']['five_axis'] = {'enabled': True, **cfg}
        return allocator.five_axis_overlay(acc, data)

    def test_off_by_default_leaves_accounts_and_scores_untouched(self):
        self.assertNotIn('five_axis', self.accounts['scoring'])
        self.assertIs(allocator.five_axis_overlay(self.accounts), self.accounts)
        decision = self.plan(self.accounts)
        top = next(c for c in decision['ranking'] if c['score'] is not None)
        self.assertNotIn('five_axis_match', top['components'])

    def test_missing_tier_leaves_automatic_routing_but_a_named_model_runs(self):
        acc = copy.deepcopy(self.accounts)
        acc['scoring']['five_axis'] = {'enabled': True}
        target = 'claude-opus-5-5'
        doc = scores(acc['models'], **{target: {'tier': {'value': None}, 'usd_per_hour': {'value': 20.0},
                                                'scores': dict.fromkeys(allocator.FIVE_AXES, 1.0), 'axes': {}}})
        original = allocator.load_five_axis
        allocator.load_five_axis = lambda path=None: doc
        try:
            auto = self.plan(acc)
            named = self.plan(acc, constraints={'model': target})
        finally:
            allocator.load_five_axis = original
        opus = [c for c in auto['ranking'] if allocator.canonical(c['bundle']['model'], acc) == target]
        self.assertTrue(opus and all(any('tier has no trusted public source' in e for e in c['excluded']) for c in opus))
        chosen = next(c for c in named['ranking'] if c['id'] == named['choice'])
        self.assertEqual(allocator.canonical(chosen['bundle']['model'], acc), target)
        self.assertIn('tier has no trusted public source', ' '.join(chosen['notes']))

    def test_missing_usd_excludes_only_on_a_billed_account(self):
        acc = self.enabled(scores(self.accounts['models'], **{
            'claude-sonnet-5-5': {'tier': {'value': 3.9}, 'usd_per_hour': {'value': None},
                                  'scores': dict.fromkeys(allocator.FIVE_AXES, 0.8), 'axes': {}}}))
        info = allocator.model_spec(acc, 'claude-sonnet-5-5')
        self.assertEqual(info['five_axis']['usd_status'], 'missing')
        self.assertNotIn('usd_per_hour', info)

    def test_strict_capacity_is_unknown_and_converted_rescales_the_shipped_figure(self):
        doc = scores(self.accounts['models'])
        strict = self.enabled(doc)
        claude = strict['accounts']['claude']
        self.assertTrue(claude.get('capacity_unknown'))
        self.assertNotIn('capacity_usd', claude)
        self.assertIsNone(allocator.template_capacity_usd(claude, 'seven_day'))
        converted = self.enabled(doc, capacity='converted')['accounts']['claude']
        shipped = self.accounts['accounts']['claude'].get('capacity_usd', {}).get('seven_day') \
            or allocator.template_capacity_usd(self.accounts['accounts']['claude'], 'seven_day')
        self.assertAlmostEqual(converted['capacity_usd']['seven_day'], round(shipped * 2.0, 2))

    def test_demand_follows_profile_fields(self):
        cfg = allocator.five_axis_config({'five_axis': {'enabled': True}})
        task = {'profile': dict(PROFILE), 'divergence': 1}
        base = allocator.five_axis_demand(task, cfg)
        creative = allocator.five_axis_demand({**task, 'divergence': 3}, cfg)
        visual = allocator.five_axis_demand({**task, 'needs_visual': True}, cfg)
        self.assertGreater(creative['divergence'], base['divergence'])
        self.assertGreater(visual['vision_tools'], base['vision_tools'])
        self.assertEqual(allocator.five_axis_demand({'profile': {**PROFILE, 'code_complexity': 0}, 'divergence': 1}, cfg)
                         .get('module_precision'), cfg['demand']['module_precision']['engineering_style'])

    def test_match_is_the_demand_weighted_mean_of_axis_scores(self):
        info = {'tier': 3.0, 'five_axis': {'scores': {'divergence': 1.0, 'global_rigor': 0.5, 'module_precision': 0.0,
                                                      'vision_tools': 0.0}}}
        match, _ = allocator.five_axis_match({'divergence': 1.0, 'global_rigor': 1.0}, info,
                                             self.accounts['scoring'], {}, 'high')
        self.assertAlmostEqual(match, 0.75)


if __name__ == '__main__':
    unittest.main()
