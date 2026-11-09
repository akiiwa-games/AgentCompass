"""The five-axis path (round 047, rounds 2-5): off by default; when on, the tier is folded into the axes, usd merges cited
sources, capacity comes down the cited ladder (community, time-to-exhaust, vendor multiplier; an empty ladder is a data
error that still prices scarcity live), ties go to the lower opportunity cost then the larger surplus, and the classifier
may answer the axis demand itself."""
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
    return {'models': rows, 'floors': FLOORS, 'capacity': CAPACITY}


def window(usd, age=3):
    return {'unit': 'usd', 'capacity_usd': usd, 'status': 'ok', 'age_days': age}


# Community capacity per family / plan / window, as five_axis.capacity writes it (Max 20x weekly_fable left out).
CAPACITY = {'claude': {'max20': {'seven_day': window(1700.0), 'five_hour': window(270.0)}},
            'codex': {'pro': {'usage': window(615.0)}}}


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

    def test_capacity_is_the_community_figure_of_the_account_plan(self):
        acc = copy.deepcopy(self.accounts)
        acc['scoring']['five_axis'] = {'enabled': True}
        acc['accounts']['claude']['capacity_change'] = {'seven_day': {'at': '2026-11-01T00:00:00Z', 'usd': 9999.0}}
        acc['accounts']['pi']['family'] = 'pi'      # discovery is off here; on a host it names the family
        out = allocator.five_axis_overlay(acc, document(acc['models']))
        spec = out['accounts']['claude']
        self.assertEqual(spec['capacity_usd'], {'seven_day': 1700.0, 'five_hour': 270.0})
        self.assertNotIn('capacity_change', spec)
        # Max 20x weekly_fable has no community value: unknown, with neither the template figure nor the burn prior.
        self.assertTrue(spec['capacity_unknown'])
        self.assertEqual(spec['capacity_source']['weekly_fable']['status'], 'missing')
        self.assertIsNone(allocator.template_capacity_usd(spec, 'weekly_fable'))
        self.assertIsNone(allocator.window_capacity_usd(spec, 'weekly_fable', NOW))
        self.assertNotIn('weekly_fable', spec.get('burn_pct_per_worker_hour') or {})
        self.assertEqual(out['accounts']['codex']['capacity_usd'], {'usage': 615.0})
        self.assertFalse(out['accounts']['codex'].get('capacity_unknown'))
        # Pi signs in with ChatGPT: the Codex account's plan and windows.
        self.assertEqual(out['accounts']['pi']['capacity_usd'], {'usage': 615.0})
        self.assertEqual(out['accounts']['pi']['capacity_source']['usage']['plan'], 'pro')
        # Antigravity has no community source: every window unknown.
        self.assertEqual(out['accounts']['antigravity']['capacity_usd'], {})
        self.assertTrue(out['accounts']['antigravity']['capacity_unknown'])

    def test_gate_reads_a_per_difficulty_axis_ratio(self):
        cfg = allocator.five_axis_config({'five_axis': {'enabled': True}})
        anchor = dict.fromkeys(allocator.FIVE_AXES, 0.8)
        overlay = {'floors': {**FLOORS, 'axis_ratio': {'routine': 1.0, 'hard': 0.9, 'coupled': 0.7, 'frontier': 1.0}},
                   'anchor_scores': anchor}
        gates = {d: allocator.five_axis_gate({'difficulty': d, 'critical': False}, demand(0, 2, 2, 0), overlay, cfg)
                 for d in ('hard', 'coupled')}
        self.assertAlmostEqual(gates['coupled']['axis_floors']['global_rigor'], round(0.7 * 0.9 * 0.8, 4))
        self.assertAlmostEqual(gates['hard']['axis_floors']['global_rigor'], round(0.9 * 0.8 * 0.8, 4))
        scalar = allocator.five_axis_gate({'difficulty': 'coupled', 'critical': False}, demand(0, 2, 2, 0),
                                          {'floors': FLOORS, 'anchor_scores': anchor}, cfg)
        self.assertAlmostEqual(scalar['axis_floors']['global_rigor'], round(0.9 * 0.9 * 0.8, 4))

    def test_equal_scores_go_to_the_lower_opportunity_cost_then_the_larger_surplus(self):
        # Round 5: the price no longer breaks ties (it handed every saturated tie to free models); quota scarcity does,
        # then the stronger qualifying model, so in the ample state the strongest qualifying model wins.
        def cand(name, usd, level, oc=0.0, score=0.5):
            return {'id': name, 'score': score, 'five_axis_price': {'usd_per_hour': usd},
                    'components': {'five_axis_level': level, 'five_axis_gate': 0.5, 'opportunity_cost': oc}}
        pool = [cand('free', 0.0, 0.55), cand('dear_scarce', 20.0, 0.95, oc=0.3), cand('cheap', 5.0, 0.6),
                cand('dear', 20.0, 0.9), cand('best', 30.0, 0.9, score=0.6), cand('free_scarce', 0.0, 0.55, oc=0.1)]
        self.assertEqual([c['id'] for c in sorted(pool, key=allocator.five_axis_order)],
                         ['best', 'dear', 'cheap', 'free', 'free_scarce', 'dear_scarce'])
        self.assertEqual(allocator.FIVE_AXIS_DEFAULTS['overqualification_weight'], 0.05)

    def test_gate_level_blends_in_the_equal_weight_level(self):
        axes = {'divergence': 0.2, 'global_rigor': 1.0, 'module_precision': 1.0, 'vision_tools': 0.4}
        dm = demand(0, 2, 2, 0)
        level, _ = allocator.five_axis_level(axes, dm)
        self.assertEqual(allocator.five_axis_gate_level(axes, dm)[0], level)
        self.assertAlmostEqual(allocator.five_axis_gate_level(axes, dm, 1.0, 1.0)[0], 0.65)
        self.assertAlmostEqual(allocator.five_axis_gate_level(axes, dm, 1.0, 0.5)[0], 0.5 * level + 0.5 * 0.65)
        cfg = allocator.five_axis_config({'five_axis': {'enabled': True}})
        gate = allocator.five_axis_gate({'difficulty': 'hard', 'critical': False}, dm,
                                        {'floors': {**FLOORS, 'level_blend': 1.0}, 'anchor_scores': axes}, cfg)
        self.assertEqual((gate['level_blend'], gate['anchor_level']), (1.0, 0.65))

    def test_capacity_unknown_window_still_prices_scarcity_from_live_percent_and_reset(self):
        acc = copy.deepcopy(self.accounts)
        spec = acc['accounts']['codex']
        for key in ('capacity_usd', 'capacity_change', 'burn_pct_per_worker_hour'):
            spec.pop(key, None)
        spec['capacity_unknown'] = True
        doc = document(acc['models'])
        doc['capacity'] = {}

        def codex_cost(state):
            d = self.plan(acc, doc, state=state, profile={'axis_demand': demand(0, 1, 2, 0)})
            rows = [c for c in d['ranking'] if c['bundle']['account'] == 'codex' and not c['excluded']]
            self.assertTrue(rows, state)
            self.assertIn('data error: no cited capacity (capacity ladder empty); scarcity from the live',
                          ' '.join(rows[0]['notes']))
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


class CommunityCapacityTests(unittest.TestCase):
    """Round 4: window capacity from cited community measurements, kept current: a stale source is a yellow note, a
    source that is gone (not fetched, or its sentence no longer on the page) gives no value."""

    @classmethod
    def setUpClass(cls):
        import community
        import five_axis
        import refresh
        cls.cm, cls.fa, cls.rf = community, five_axis, refresh

    WOKEY = ('<html><body><p>Data updated 2026-11-02</p><p>Claude Pro ($20/mo)</p><p>: ≈ $155 of API use a week '
             '(IQR $146–173) · 24.2M uncached tokens · ≈ 13 full 5-hour windows</p>'
             '<p>Claude Max 20x ($200/mo)</p><p>: ≈ $1,549 of API use a week (IQR) · 1M tokens · ≈ 5.7 full 5-hour windows</p>'
             '<p>Fable 5.1 can use only half the weekly limit.</p>'
             '<p>value every plan at one fixed traffic split: Claude Code traffic on Wokey in the week to 2026-11-02, about '
             '92% of tokens cache reads.</p><p>Claude Max 20x $200/mo $1,549 ($1,216–$1,697), ≈ $6,711/mo 34× 180M 9.9M '
             '$272</p><p>Claude Max 20x: 92% cache reads, 1.98B tokens a week</p></body></html>')

    def pages(self, folder, wokey=WOKEY):
        import tempfile
        target = Path(tempfile.mkdtemp(dir=folder))
        files = {}
        if wokey is not None:
            (target / 'community-wokey-claude.html').write_text(wokey, encoding='utf-8')
            files['community-wokey-claude.html'] = {'url': 'https://wokey.ai/claude-usage-limits', 'status': 'ok',
                                                    'fetched_at': '2026-11-09T01:00:00+00:00'}
        return target, files

    def test_readings_carry_source_date_and_derivation(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            target, files = self.pages(folder)
            rows, obs, rules, errors, mix = self.cm.extract(target, files)
        got = {(o['plan'], o['window']): o for o in obs if o['source'] == 'community.wokey.claude'}
        self.assertEqual(got[('pro', 'seven_day')]['value'], 155.0)
        self.assertEqual(got[('pro', 'five_hour')]['value'], round(155 / 13, 2))
        self.assertEqual(got[('max20', 'seven_day')]['value'], 1549.0)
        self.assertEqual({o['as_of'] for o in got.values()}, {'2026-11-02'})
        self.assertEqual({o['kind'] for o in got.values()}, {'community'})
        self.assertEqual(rules[0]['window'], 'weekly_fable')
        self.assertEqual(rows['community.wokey.claude']['fetched_at'], '2026-11-09T01:00:00+00:00')
        # The community token mix (option B): 92% cache reads stated, the rest split by the Max 20x row (9.9M of 180M).
        self.assertEqual(mix['cache_read'], 0.92)
        self.assertAlmostEqual(mix['output'], 0.08 * 9.9 / 180, places=5)
        self.assertAlmostEqual(mix['input'] + mix['output'] + mix['cache_read'], 1.0, places=4)
        self.assertEqual((mix['as_of'], mix['kind']), ('2026-11-02', 'community'))
        # The other pages were not fetched: red, and they give no value.
        missing = {e['source'] for e in errors if e['reason'] == 'source_missing'}
        self.assertEqual(missing, set(self.cm.SOURCES) - {'community.wokey.claude'})
        self.assertTrue(all(e['level'] == 'red' for e in errors if e['reason'] == 'source_missing'))

    def test_a_page_that_lost_its_sentence_gives_no_value(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            target, files = self.pages(folder, self.WOKEY.replace('Claude Max 20x', 'Claude Max 25x'))
            _, obs, _, errors, _ = self.cm.extract(target, files)
        self.assertFalse([o for o in obs if o['plan'] == 'max20'])
        self.assertIn('mix_not_found', {e['reason'] for e in errors})       # the mix row is the Max 20x row too
        lost = [e for e in errors if e['reason'] == 'reading_not_found']
        self.assertEqual({(e['plan'], e['window']) for e in lost}, {('max20', 'seven_day'), ('max20', 'five_hour')})
        self.assertTrue(all(e['level'] == 'red' for e in lost))

    def test_a_two_track_diagram_gives_a_value_only_when_both_tracks_agree(self):
        same = ('Capacity: $129.00 / week │ │ Capacity: $129.00 / week │ │ Burst: $20.00 / 5h │ │ Burst: $20.00 / 5h',)
        import re
        m = re.search(self.cm.DIAGRAM, same[0])
        self.assertEqual((self.cm.both_tracks(0)(m.groups()), self.cm.both_tracks(2)(m.groups())), (129.0, 20.0))
        m = re.search(self.cm.DIAGRAM, same[0].replace('Capacity: $129.00 / week │ │ Capacity: $129.00', 'Capacity: $129.00 / week │ │ Capacity: $175.00'))
        with self.assertRaises(ValueError):
            self.cm.both_tracks(0)(m.groups())

    def meas(self, fresh_as_of='2026-11-02', old_as_of='2026-10-01'):
        def ob(plan, wid, value, src, as_of):
            return {'family': 'claude', 'plan': plan, 'window': wid, 'value': value, 'source': src, 'kind': 'community',
                    'url': 'https://example.org/' + src, 'as_of': as_of, 'fetched_at': '2026-11-09T01:00:00+00:00',
                    'method': 'm'}
        return {'capacity': {
            'observations': [ob('max20', 'seven_day', 1500.0, 'community.a', fresh_as_of),
                             ob('max20', 'seven_day', 1900.0, 'community.b', old_as_of),
                             ob('max20', 'five_hour', 270.0, 'community.a', fresh_as_of)],
            'rules': [{'family': 'claude', 'window': 'weekly_fable', 'of_window': 'seven_day', 'ratio': 0.5,
                       'statement': 'half', 'original': 'https://example.org/original', 'source': 'community.a',
                       'kind': 'community', 'url': 'https://example.org/community.a', 'as_of': fresh_as_of}],
            'errors': [], 'not_found': {}}}

    PLANS = {'claude': {'max20': {'windows': {'five_hour': {}, 'seven_day': {}, 'weekly_fable': {}}},
                        'max5': {'windows': {'five_hour': {}, 'seven_day': {}}},
                        'max': {'alias': 'max5'}}}

    def test_capacity_merges_flags_stale_and_derives_fable(self):
        now = datetime(2026, 11, 9, tzinfo=timezone.utc)
        caps, errors = self.fa.capacity(self.meas(), self.PLANS, {'claude-opus-5-5': {'value': 20.0}}, now)
        week = caps['claude']['max20']['seven_day']
        self.assertEqual(week['capacity_usd'], 1700.0)
        self.assertEqual((week['status'], week['age_days']), ('ok', 7))
        self.assertEqual(week['ref_hours'], 85.0)
        self.assertEqual({e['source']: e['stale'] for e in week['sources']}, {'community.a': False, 'community.b': True})
        fable = caps['claude']['max20']['weekly_fable']
        self.assertEqual(fable['capacity_usd'], 850.0)
        self.assertEqual(fable['sources'][0]['kind'], 'derived')
        self.assertEqual(len(fable['sources'][0]['derived_from']), 2)
        stale = [e for e in errors if e['reason'] == 'stale_source']
        self.assertEqual([(e['source'], e['level'], e['age_days']) for e in stale], [('community.b', 'yellow', 39)])
        # Max 5x has nothing on any rung: a red data error, and the `max` alias shows the same without a second error.
        self.assertIsNone(caps['claude']['max5']['seven_day']['capacity_usd'])
        self.assertEqual(caps['claude']['max5']['seven_day']['status'], 'error')
        self.assertEqual(caps['claude']['max']['seven_day']['alias_of'], 'max5')
        red = {(e['plan'], e['window']) for e in errors if e['reason'] == 'capacity_ladder_empty'}
        self.assertEqual(red, {('max5', 'five_hour'), ('max5', 'seven_day')})
        self.assertEqual(week['rung'], 1)

    def test_the_ladder_takes_exhaust_reports_then_vendor_multipliers_and_keeps_the_cross_check(self):
        now = datetime(2026, 11, 9, tzinfo=timezone.utc)
        meas = self.meas()
        meas['capacity']['rules'].append({'type': 'plan_multiplier', 'family': 'claude', 'plan': 'max5', 'of_plan': 'max20',
                                          'factor': 0.25, 'statement': 'a quarter', 'source': 'vendor.x', 'kind': 'vendor',
                                          'url': 'https://example.org/vendor', 'as_of': '2026-11-08'})
        meas['capacity']['exhaust'] = [{'family': 'claude', 'plan': 'max5', 'window': 'five_hour', 'model': 'claude-opus-5-5',
                                        'hours': 4.0, 'source': 'community.c', 'kind': 'community',
                                        'url': 'https://example.org/c', 'as_of': '2026-11-05', 'quote': 'ran out after 4h'}]
        caps, errors = self.fa.capacity(meas, self.PLANS, {'claude-opus-5-5': {'value': 20.0}}, now)
        five = caps['claude']['max5']['five_hour']
        self.assertEqual((five['rung'], five['capacity_usd'], five['sources'][0]['kind']), (2, 80.0, 'derived'))
        self.assertEqual(five['cross_check']['value'], round(0.25 * 270.0, 2))
        week = caps['claude']['max5']['seven_day']
        self.assertEqual((week['rung'], week['capacity_usd']), (3, 425.0))
        self.assertEqual(week['sources'][0]['derived_from'][0]['source'], 'community.a')
        self.assertNotIn('cross_check', week)
        self.assertFalse([e for e in errors if e['reason'] == 'capacity_ladder_empty'])
        # An exhaust report of a model with no usd gives no value: the vendor rung takes over.
        caps, _ = self.fa.capacity(meas, self.PLANS, {}, now)
        self.assertEqual(caps['claude']['max5']['five_hour']['rung'], 3)

    def test_all_sources_stale_turns_the_window_stale_and_refresh_exits_yellow_or_red(self):
        now = datetime(2026, 12, 15, tzinfo=timezone.utc)
        caps, errors = self.fa.capacity(self.meas(), self.PLANS, {}, now)
        self.assertEqual(caps['claude']['max20']['seven_day']['status'], 'stale')
        meas = self.meas()
        meas['sources'] = {'community.a': {'kind': 'community', 'as_of': '2026-11-02'}}
        doc = {'source_ages': self.fa.source_ages(meas, datetime(2026, 11, 9, tzinfo=timezone.utc)), 'capacity_errors': []}
        self.assertEqual(self.rf.staleness(doc)[1], 'ok')
        doc['source_ages'] = self.fa.source_ages(meas, now)
        self.assertEqual(self.rf.staleness(doc)[1], 'stale')
        doc['capacity_errors'] = errors
        rows, worst = self.rf.staleness(doc)
        self.assertEqual(worst, 'missing')
        self.assertIn('claude/max5/seven_day', {r['what'] for r in rows})
        meas['missing_sources'] = {'metr.time_horizon_p50': {'reason': 'not used: the anchor is not listed'}}
        unused = self.rf.staleness({'source_ages': self.fa.source_ages(meas, datetime(2026, 11, 9, tzinfo=timezone.utc))})
        self.assertEqual(unused[1], 'ok')


class DerivedUsdTests(unittest.TestCase):
    """Round 4: benchmarks give relative prices only; a model without a community usd is derived from a measured model
    of the same provider, with both citations."""

    @classmethod
    def setUpClass(cls):
        import five_axis
        cls.fa = five_axis

    def test_derived_usd_scales_a_measured_same_provider_model_by_the_benchmark_ratio(self):
        measured = {'gpt-6.1-sol': {'value': 24.0, 'used': ['community.x'], 'sources': {'community.x': {'url': 'u'}}}}
        bench = {'gpt-6.1-sol': {'value': 10.0, 'used': ['aa.cost_time']}, 'gpt-6-luna': {'value': 5.0, 'used': ['aa.cost_time']},
                 'claude-opus-5-5': {'value': 20.0, 'used': ['aa.cost_time']}, 'kimi-k3': {'value': 2.0, 'used': []}}
        out = self.fa.derive_usd(measured, bench)
        self.assertEqual(set(out), {'gpt-6-luna'})           # no measured Anthropic model; kimi has no provider match
        luna = out['gpt-6-luna']
        self.assertEqual((luna['value'], luna['kind']), (12.0, 'derived'))
        self.assertEqual(luna['sources']['community']['model'], 'gpt-6.1-sol')
        self.assertEqual(luna['sources']['ratio']['value'], 0.5)
        self.assertEqual(luna['sources']['ratio']['denominator']['used'], ['aa.cost_time'])

    def test_community_basis_uses_measured_then_derived_else_missing(self):
        def e(src, kind, value):
            return {'source': src, 'kind': kind, 'effort': 'high', 'value': value}
        meas = {'models': {'gpt-6.1-sol': {'usd_per_hour': [e('aa.cost_time', 'benchmark', 10.0), e('community.x', 'community', 24.0)]},
                           'gpt-6-luna': {'usd_per_hour': [e('aa.cost_time', 'benchmark', 5.0)]},
                           'claude-opus-5-5': {'usd_per_hour': [e('aa.cost_time', 'benchmark', 20.0)]}}}
        scorer = self.fa.Scorer(meas, {'models': dict.fromkeys(meas['models'], {})})
        rows = {m: {'fields': f} for m, f in meas['models'].items()}
        usd = self.fa.USD_BASES['community']['fn'](scorer, rows, meas, None)
        self.assertEqual((usd['gpt-6.1-sol']['value'], usd['gpt-6.1-sol']['kind']), (24.0, 'community'))
        self.assertEqual((usd['gpt-6-luna']['value'], usd['gpt-6-luna']['kind']), (12.0, 'derived'))
        self.assertEqual(usd['claude-opus-5-5']['status'], 'missing')
        bench = self.fa.USD_BASES['benchmark']['fn'](scorer, rows, meas, None)
        self.assertEqual(bench['claude-opus-5-5']['value'], 20.0)

    def test_community_mix_basis_reprices_aa_throughput_at_the_community_split(self):
        import measurements
        builder = measurements.Builder.__new__(measurements.Builder)
        builder.target, builder.manifest = Path('.'), {'aa-models.html': {'status': 'ok', 'url': 'https://aa', 'fetched_at': '2026-11-09'}}
        builder.ok = lambda name: True
        builder.source_rows = {'aa.cost_time': {'file': 'aa-models.html', 'url': 'https://aa', 'fetched_at': '2026-11-09T00:00:00+00:00',
                                                'sha256': None, 'as_of': '2026-11-09'}}
        builder.mix = {'cache_read': 0.92, 'input': 0.0756, 'output': 0.0044, 'source': 'community.wokey.claude',
                       'url': 'https://wokey.ai/claude-usage-limits', 'as_of': '2026-11-02', 'quotes': []}
        builder.names = {'m': {'aa.cost_time': 'M'}, 'unlisted': {}}
        builder.out = {'m': {}, 'unlisted': {}}
        builder.missing = {}
        builder.aa_variants = lambda: {'M': {'high': {'name': 'M (high)', 'outputSpeed': 100.0, 'price1mOutputTokens': 20.0,
                                                      'price1mInputTokens': 4.0, 'cacheHitPrice': 0.2},
                                             'low': {'name': 'M (low)', 'outputSpeed': 100.0, 'price1mOutputTokens': 20.0,
                                                     'price1mInputTokens': 4.0}}}      # no cache price: no entry
        builder.community_mix()
        entries = builder.out['m']['usd_per_hour']
        self.assertEqual([e['effort'] for e in entries], ['high'])
        per_m = 20.0 + 0.0756 / 0.0044 * 4.0 + 0.92 / 0.0044 * 0.2
        self.assertAlmostEqual(entries[0]['value'], round(100 * 3600 / 1e6 * per_m, 3))
        self.assertEqual(entries[0]['kind'], 'derived')
        cites = builder.source_rows['derived.community_mix']['citations']
        self.assertEqual({c['source'] for c in cites}, {'aa.price', 'community.wokey.claude'})
        self.assertEqual(builder.source_rows['derived.community_mix']['as_of'], '2026-11-02')
        self.assertNotIn('usd_per_hour', builder.out['unlisted'])
        scorer = self.fa.Scorer({'models': builder.out}, {'models': {'m': {}, 'unlisted': {}}})
        usd = self.fa.USD_BASES['community_mix']['fn'](scorer, {k: {'fields': v} for k, v in builder.out.items()}, {}, None)
        self.assertEqual(usd['m']['value'], entries[0]['value'])
        self.assertEqual(usd['unlisted']['status'], 'missing')
        self.assertEqual(self.fa.pick_duration(usd['m'], {'duration_multiplier': 1.35})['value'], 1.0)


if __name__ == '__main__':
    unittest.main()
