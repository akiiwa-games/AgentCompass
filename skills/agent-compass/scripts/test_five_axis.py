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

    def test_unsourced_usd_is_never_priced_at_the_default(self):
        acc = copy.deepcopy(self.accounts)
        acc['scoring']['five_axis'] = {'enabled': True}
        target = 'claude-sonnet-5-5'
        doc = document(acc['models'], **{target: row(0.8, None)})
        out = allocator.five_axis_overlay(acc, doc)
        info = allocator.model_spec(out, target)
        self.assertEqual(allocator.model_usd_per_hour(target, info), 0.0)
        self.assertEqual(allocator.unit_rate({'windows': {'w': {}}}, 'w', target, 'high', acc['scoring'], info=info), 0.0)
        auto = self.plan(acc, doc, profile={'axis_demand': demand(0, 1, 2, 0)})
        self.assertTrue(all(any('usd_per_hour has no cited source on a metered account' in e for e in c['excluded'])
                            for c in self.rows_for(auto, target)))
        named = self.plan(acc, doc, profile={'axis_demand': demand(0, 1, 2, 0)}, constraints={'model': target})
        chosen = next(c for c in named['ranking'] if c['id'] == named['choice'])
        self.assertEqual(allocator.canonical(chosen['bundle']['model'], acc), target)
        self.assertIn('its cost is not priced', ' '.join(chosen['notes']))

    def test_capacity_is_scaled_by_the_data_conversion_factor(self):
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
        trivial, source = allocator.five_axis_demand({'profile': {**PROFILE, 'axis_demand': demand(0, 0, 0, 0)}})
        self.assertEqual(trivial, fallback)        # an all-zero answer is replaced by the fallback ...
        self.assertNotEqual(source, 'classifier')   # ... and labelled as one (review F5)
        self.assertIn('fallback', source)

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


class AnchorRelativeTests(unittest.TestCase):
    """Review F1 and F3 (2026-11-09): values are relative to the anchor at the source, so a new top model moves no
    floor; a backfill fit needs enough measured points; local flags and samples are not sources."""

    @classmethod
    def setUpClass(cls):
        import five_axis
        import measurements
        cls.fa, cls.ms = five_axis, measurements

    def boards(self):
        anchor = self.ms.ANCHOR
        raw = {'arena.text': {anchor: 1500.0, 'b': 1400.0, 'c': 1450.0, 'd': 1300.0},
               'aa.frac': {anchor: 0.70, 'b': 0.50, 'c': 0.60, 'd': 0.40},
               'aa.net': {anchor: 30.0, 'b': -10.0, 'c': 10.0, 'd': 0.0},
               'vals.frac': {'b': 0.45, 'c': 0.55, 'd': 0.35, 'e': 0.25}}      # no anchor: bridged on b, c, d
        kinds = {'arena.text': ('elo', 1.0), 'aa.frac': ('fraction', 1.0), 'aa.net': ('net', 100.0),
                 'vals.frac': ('fraction', 1.0)}
        comps = {'arena.text': ('global_rigor', 'reasoning'), 'aa.frac': ('global_rigor', 'long_context'),
                 'aa.net': ('global_rigor', 'anti_hallucination'), 'vals.frac': ('global_rigor', 'long_horizon')}
        return raw, kinds, comps

    def test_a_new_top_model_moves_no_value_and_no_floor(self):
        raw, kinds, comps = self.boards()
        before, _, failed = self.ms.anchor_boards(raw, kinds, comps)
        self.assertEqual(failed, {})
        self.assertEqual(before['arena.text'][self.ms.ANCHOR], self.ms.ANCHOR_VALUE)
        grown = {src: dict(board) for src, board in raw.items()}
        grown['arena.text']['newtop'] = max(raw['arena.text'].values()) + 100       # +100 Elo over the board
        grown['aa.frac']['newtop'], grown['aa.net']['newtop'] = 0.95, 60.0
        # vals.frac (bridged) keeps its reference while the new model is not on it; a model listed on a bridged board and
        # an anchored one joins the bridge median (documented in measurements.py).
        after, _, _ = self.ms.anchor_boards(grown, kinds, comps)
        for src, values in before.items():
            self.assertEqual({m: after[src][m] for m in values}, values, src)
        # The floor is a share of the anchor level on the task's demand: unchanged anchor scores, unchanged gate.
        acc = sim.fresh_accounts(shipped_template(), snapshot())
        acc['scoring']['five_axis'] = {'enabled': True}
        cfg = allocator.five_axis_config(acc['scoring'])
        task = {'difficulty': 'hard', 'critical': False}
        gates = []
        for extra in ({}, {'newtop': row(1.0, 30.0)}):
            out = allocator.five_axis_overlay(acc, document(acc['models'], **{self.ms.ANCHOR: row(0.8, 21.7)}, **extra))
            gates.append(allocator.five_axis_gate(task, demand(1, 2, 2, 0), out['_five_axis'], cfg))
        self.assertEqual(gates[0], gates[1])

    def test_board_best_scaling_would_have_drifted(self):
        """The round-2 conversion (win rate against the board best) moves b relative to the anchor; this one does not."""
        old = lambda board, m: 1 / (1 + 10 ** ((max(board.values()) - board[m]) / 400))
        raw, kinds, comps = self.boards()
        board = raw['arena.text']
        ratio = old(board, 'b') / old(board, self.ms.ANCHOR)
        board_new = {**board, 'newtop': max(board.values()) + 100}
        self.assertLess(old(board_new, 'b') / old(board_new, self.ms.ANCHOR), ratio - 0.03)

    def test_a_backfill_fit_needs_enough_points_and_a_positive_slope(self):
        few = [(10.0 * i, 0.1 * i) for i in range(1, self.fa.MIN_FIT_POINTS)]
        self.assertIsNone(self.fa.Scorer.fit(few))
        enough = [(10.0 * i, 0.1 * i) for i in range(1, self.fa.MIN_FIT_POINTS + 1)]
        self.assertIsNotNone(self.fa.Scorer.fit(enough))
        falling = [(10.0 * i, 1 - 0.1 * i) for i in range(1, self.fa.MIN_FIT_POINTS + 1)]
        self.assertIsNone(self.fa.Scorer.fit(falling))

    def test_shipped_data_has_no_local_flag_or_sample_sources(self):
        import json
        ref = Path(__file__).resolve().parents[1] / 'references'
        meas = json.loads((ref / 'public-measurements.json').read_text(encoding='utf-8'))
        used = {e['source'] for fields in meas['models'].values() for entries in fields.values() for e in entries}
        self.assertFalse(used & {'accounts.visual', 'packaged.accounts', 'community.burn'})
        scores = json.loads((ref / 'five-axis-scores.json').read_text(encoding='utf-8'))
        self.assertEqual(scores['floors']['anchor'], self.ms.ANCHOR)
        self.assertAlmostEqual(min(scores['models'][self.ms.ANCHOR]['scores'][a] for a in ('divergence', 'module_precision')),
                               self.ms.ANCHOR_VALUE, places=2)


class SourceMergeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import five_axis
        cls.fa = five_axis

    def entries(self):
        return [{'source': 'aa.cost_time', 'kind': 'benchmark', 'effort': 'high', 'value': 20.0},
                {'source': 'aa.cost_time', 'kind': 'benchmark', 'effort': 'low', 'value': 18.0},
                {'source': 'openrouter.cost_time', 'kind': 'vendor', 'effort': 'high', 'value': 10.0},
                {'source': 'community.example', 'kind': 'community', 'effort': 'high', 'value': 8.0}]

    def test_usd_is_the_median_of_cited_sources_or_the_first_priority(self):
        merged = self.fa.pick_usd(self.entries())
        self.assertEqual(merged['value'], 10.0)
        self.assertEqual(sorted(merged['used']), ['aa.cost_time', 'community.example', 'openrouter.cost_time'])
        self.assertEqual(merged['sources']['community.example']['kind'], 'community')
        self.assertEqual(self.fa.pick_usd(self.entries(), ['community.example'])['value'], 8.0)
        self.assertEqual(self.fa.pick_usd([])['status'], 'missing')

    def test_distrust_removes_a_source_from_every_field(self):
        meas = {'models': {'m': {'usd_per_hour': self.entries(), 'divergence': [
            {'source': 'eqbench.creative_writing_v3', 'component': 'creative', 'value': 0.2},
            {'source': 'lmarena.creative_writing', 'component': 'creative', 'value': 0.8}]}}}
        scorer = self.fa.Scorer(meas, {'models': {'m': {}}}, distrust=['community.example', 'eqbench.creative_writing_v3'])
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
