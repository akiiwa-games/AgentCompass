"""The five-axis path (round 047, round 2): off by default; when on, the tier is folded into the axes, usd merges cited
sources, capacity-unknown windows keep scarcity, and the classifier may answer the axis demand itself."""
import copy
from datetime import datetime, timezone
from pathlib import Path
import sys
import types
import unittest

import allocator
import route_judge
import route_simulation as sim
from router_test_support import isolated_router_state
from test_fleet_parity import shipped_template, snapshot

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'calibration'))


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup, discovery=False, policy=False)


NOW = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)
PROFILE = {'reasoning_depth': 2, 'code_complexity': 2, 'context_scale': 1, 'divergence': 1, 'hallucination_risk': 2,
           'error_cost': 2, 'needs_visual': False, 'needs_gui': False, 'local_only': True, 'realtime_loop': False,
           'wants_fast': False, 'read_only_review': False, 'interaction_style': 'engineering', 'multi_task': False,
           'task_count': '1', 'expected_hours': 1.0}
FLOORS = {'anchor': 'claude-opus-5-5', 'ratio': {'routine': 0.6, 'hard': 0.8, 'coupled': 0.9, 'frontier': 0.95},
          'critical_ratio': {'routine': 0.65, 'hard': 0.85, 'coupled': 0.93, 'frontier': 0.97}, 'axis_ratio': 0.9,
          'axis_per_tier': 0.16}


def demand(div, rigor, code, vision):
    return {'divergence': div, 'global_rigor': rigor, 'module_precision': code, 'vision_tools': vision}


def row(scores=0.8, usd=5.0, missing=()):
    scores = scores if isinstance(scores, dict) else dict.fromkeys(allocator.FIVE_AXES, scores)
    return {'scores': scores, 'missing_axes': list(missing), 'usd_per_hour': {'value': usd},
            'duration_multiplier': {'value': 1.0}, 'intelligence_index': 50.0}


def document(models, **override):
    rows = {name: row() for name, info in models.items() if isinstance(info, dict)}
    rows['claude-opus-5-5'] = row(1.0, 15.0)
    rows.update(override)
    return {'models': rows, 'floors': FLOORS,
            'capacity_conversion': {'claude': {'seven_day': {'factor': 2.0}, 'five_hour': {'factor': 2.0}}}}


class FiveAxisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.accounts = sim.fresh_accounts(shipped_template(), snapshot())
        cls.policy = {'allowed_accounts': list(cls.accounts['accounts'])}

    def usage(self, accounts, state='ample'):
        return sim.snapshot(sim.synthetic_document(accounts, NOW, sim.STATES[state]), accounts, NOW)

    def plan(self, accounts, doc, state='ample', **task):
        acc = copy.deepcopy(accounts)
        acc['scoring']['five_axis'] = {'enabled': True}
        original = allocator.load_five_axis
        allocator.load_five_axis = lambda path=None: doc
        try:
            profile = {**PROFILE, **task.pop('profile', {})}
            return allocator.plan({'objective': 't', 'profile': profile, **task}, self.usage(acc, state), NOW,
                                  accounts=acc, overrides=[], records=[], host_policy_config=self.policy)
        finally:
            allocator.load_five_axis = original

    def rows_for(self, decision, model):
        return [c for c in decision['ranking'] if allocator.canonical(c['bundle']['model'], self.accounts) == model]

    def test_off_by_default_leaves_accounts_and_scores_untouched(self):
        self.assertNotIn('five_axis', self.accounts['scoring'])
        self.assertIs(allocator.five_axis_overlay(self.accounts), self.accounts)
        decision = allocator.plan({'objective': 't', 'profile': dict(PROFILE)}, self.usage(self.accounts), NOW,
                                  accounts=copy.deepcopy(self.accounts), overrides=[], records=[], host_policy_config=self.policy)
        top = next(c for c in decision['ranking'] if c['score'] is not None)
        self.assertNotIn('five_axis_level', top['components'])

    def test_gate_follows_the_demanded_axes_not_a_scalar_tier(self):
        # Sol: precise coder, weak divergence. It clears a hard coding task and fails a hard creative one.
        doc = document(self.accounts['models'], **{'gpt-6.1-sol': row(demand(0.2, 0.9, 1.0, 0.5))})
        coding = self.plan(self.accounts, doc, profile={'axis_demand': demand(0, 1, 2, 0)})
        creative = self.plan(self.accounts, doc, profile={'axis_demand': demand(3, 1, 0, 0), 'code_complexity': 0})
        sol_high = lambda d: next(c for c in self.rows_for(d, 'gpt-6.1-sol') if c['bundle'].get('effort') == 'high')
        self.assertFalse([e for e in sol_high(coding)['excluded'] if 'floor' in e])
        reasons = ' '.join(sol_high(creative)['excluded'])
        self.assertIn('below the hard floor', reasons)
        self.assertIn('divergence', reasons)        # the strongly demanded axis has its own floor
        self.assertEqual(creative['decision']['task']['difficulty'], 'hard')

    def test_missing_demanded_axis_leaves_automatic_routing_but_a_named_model_runs(self):
        target = 'claude-sonnet-5-5'
        doc = document(self.accounts['models'], **{target: row(demand(0.0, 0.95, 0.95, 0.9), missing=['divergence'])})
        blind = self.plan(self.accounts, doc, profile={'axis_demand': demand(2, 1, 2, 0)})
        sighted = self.plan(self.accounts, doc, profile={'axis_demand': demand(0, 1, 2, 0)})
        named = self.plan(self.accounts, doc, profile={'axis_demand': demand(2, 1, 2, 0)}, constraints={'model': target})
        self.assertTrue(all(any('no trusted data for the demanded axis divergence' in e for e in c['excluded'])
                            for c in self.rows_for(blind, target)))
        self.assertFalse(any('no trusted data' in e for c in self.rows_for(sighted, target) for e in c['excluded']))
        chosen = next(c for c in named['ranking'] if c['id'] == named['choice'])
        self.assertEqual(allocator.canonical(chosen['bundle']['model'], self.accounts), target)
        self.assertIn('no trusted data for the demanded axis divergence', ' '.join(chosen['notes']))

    def test_missing_usd_is_removed_not_defaulted(self):
        acc = copy.deepcopy(self.accounts)
        acc['scoring']['five_axis'] = {'enabled': True}
        out = allocator.five_axis_overlay(acc, document(acc['models'], **{'claude-sonnet-5-5': row(0.8, None)}))
        info = allocator.model_spec(out, 'claude-sonnet-5-5')
        self.assertEqual(info['five_axis']['usd_status'], 'missing')
        self.assertNotIn('usd_per_hour', info)

    def test_capacity_is_rescaled_in_reference_model_hours(self):
        acc = copy.deepcopy(self.accounts)
        acc['scoring']['five_axis'] = {'enabled': True}
        shipped = acc['accounts']['claude'].get('capacity_usd', {}).get('seven_day') \
            or allocator.template_capacity_usd(acc['accounts']['claude'], 'seven_day')
        out = allocator.five_axis_overlay(acc, document(acc['models']))
        self.assertAlmostEqual(out['accounts']['claude']['capacity_usd']['seven_day'], round(shipped * 2.0, 2))
        self.assertFalse(out['accounts']['claude'].get('capacity_unknown'))

    def test_capacity_unknown_window_still_prices_scarcity_from_live_percent_and_reset(self):
        acc = copy.deepcopy(self.accounts)
        spec = acc['accounts']['codex']
        for key in ('capacity_usd', 'capacity_change', 'burn_pct_per_worker_hour'):
            spec.pop(key, None)
        spec['capacity_unknown'] = True
        doc = document(acc['models'])
        doc['capacity_conversion'] = {}

        def codex_cost(state):
            d = self.plan(acc, doc, state=state, profile={'axis_demand': demand(0, 1, 2, 0)})
            rows = [c for c in d['ranking'] if c['bundle']['account'] == 'codex' and not c['excluded']]
            self.assertTrue(rows, state)
            self.assertIn('capacity unknown; scarcity from the live', ' '.join(rows[0]['notes']))
            return rows[0]['components']['opportunity_cost']

        ample, tight = codex_cost('ample'), codex_cost('codex tight')
        self.assertGreater(tight, ample)
        self.assertGreater(tight, 0.0)

    def test_classifier_axis_demand_is_parsed_and_used(self):
        prof = allocator.validate_profile({**PROFILE, 'axis_demand': demand(1, 2, 3, 0)})
        self.assertEqual(prof['axis_demand'], demand(1, 2, 3, 0))
        self.assertEqual(allocator.five_axis_demand({'profile': prof}), (demand(1, 2, 3, 0), 'classifier'))
        for bad in (demand(1, 2, 4, 0), {**demand(1, 2, 3, 0), 'economy': 1}, {'divergence': 1}, demand(True, 2, 3, 0)):
            with self.assertRaises(ValueError):
                allocator.validate_profile({**PROFILE, 'axis_demand': bad})
        fallback, source = allocator.five_axis_demand({'profile': dict(PROFILE)})
        self.assertEqual(source, 'profile_fields_fallback')
        self.assertEqual(fallback['module_precision'], 2)
        trivial, _ = allocator.five_axis_demand({'profile': {**PROFILE, 'axis_demand': demand(0, 0, 0, 0)}})
        self.assertEqual(trivial['global_rigor'], 1)

    def test_scorer_asks_for_axis_demand_only_while_the_path_is_on(self):
        caps = {'routes': {'general_implementation': {}, 'other_or_mixed': {}}}
        stub = lambda on: types.SimpleNamespace(TASK_PROFILE_SCHEMA=allocator.TASK_PROFILE_SCHEMA,
                                                FIVE_AXIS_DEMAND_SCHEMA=allocator.FIVE_AXIS_DEMAND_SCHEMA,
                                                five_axis_scorer_enabled=lambda: on)
        on, off = route_judge.scorer_schema(stub(True), caps), route_judge.scorer_schema(stub(False), caps)
        self.assertIn('axis_demand', on['properties'])
        self.assertIn('axis_demand', on['required'])
        self.assertNotIn('axis_demand', off['properties'])
        self.assertEqual(off, route_judge.scorer_schema(allocator, caps))     # the shipped policy leaves it off


class SourceMergeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import five_axis
        cls.fa = five_axis

    def entries(self):
        return [{'source': 'aa.cost_time', 'kind': 'benchmark', 'effort': 'high', 'value': 20.0},
                {'source': 'aa.cost_time', 'kind': 'benchmark', 'effort': 'low', 'value': 18.0},
                {'source': 'openrouter.cost_time', 'kind': 'vendor', 'effort': 'high', 'value': 10.0},
                {'source': 'packaged.accounts', 'kind': 'community', 'effort': 'high', 'value': 8.0}]

    def test_usd_is_the_median_of_cited_sources_or_the_first_priority(self):
        merged = self.fa.pick_usd(self.entries())
        self.assertEqual(merged['value'], 10.0)
        self.assertEqual(sorted(merged['used']), ['aa.cost_time', 'openrouter.cost_time', 'packaged.accounts'])
        self.assertEqual(merged['sources']['packaged.accounts']['kind'], 'community')
        self.assertEqual(self.fa.pick_usd(self.entries(), ['packaged.accounts'])['value'], 8.0)
        self.assertEqual(self.fa.pick_usd([])['status'], 'missing')

    def test_distrust_removes_a_source_from_every_field(self):
        meas = {'models': {'m': {'usd_per_hour': self.entries(), 'divergence': [
            {'source': 'eqbench.creative_writing_v3', 'component': 'creative', 'value': 0.2},
            {'source': 'lmarena.creative_writing', 'component': 'creative', 'value': 0.8}]}}}
        scorer = self.fa.Scorer(meas, {'models': {'m': {}}}, distrust=['packaged.accounts', 'eqbench.creative_writing_v3'])
        usd = self.fa.pick_usd(scorer.trusted(meas['models']['m']['usd_per_hour']))
        self.assertEqual(usd['value'], 15.0)
        self.assertEqual(scorer.components(meas['models']['m'], 'divergence')['creative']['value'], 0.8)

    def test_duration_is_neutral_only_when_a_time_based_source_is_merged(self):
        timed = self.fa.pick_duration(self.fa.pick_usd(self.entries()), {'duration_multiplier': 1.35})
        untimed = self.fa.pick_duration(self.fa.pick_usd(self.entries()[3:]), {'duration_multiplier': 1.35})
        self.assertEqual(timed['value'], 1.0)
        self.assertEqual((untimed['value'], untimed['kind']), (1.35, 'community'))


if __name__ == '__main__':
    unittest.main()
