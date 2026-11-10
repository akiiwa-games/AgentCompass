"""The five-axis path (round 047, rounds 2-7): off by default; when on, the axes are the only dimension system (no tier,
vectors, route lists, harness factors or difficulty floors; round 7), quality is the continuous fit of the axis scores to
the demand with a shortfall penalty, usd merges cited sources, capacity comes down the cited ladder (community,
time-to-exhaust, vendor multiplier; an empty ladder is a data error that still prices scarcity live), every long window
charges burn x a pacing shadow price, ties go to the lower opportunity cost then the higher level, and the classifier may
answer the axis demand itself."""
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
ANCHOR = {'model': 'claude-opus-5-5', 'value': 0.8}


def demand(div, rigor, code, vision):
    return {'divergence': div, 'global_rigor': rigor, 'module_precision': code, 'vision_tools': vision}


def row(scores=0.8, usd=5.0, missing=(), image_input='yes', dur=1.0, fast=None):
    scores = scores if isinstance(scores, dict) else dict.fromkeys(allocator.FIVE_AXES, scores)
    img_val = True if image_input == 'yes' else False if image_input == 'no' else None
    out = {'scores': scores, 'missing_axes': list(missing),
           'usd_per_hour': {'value': usd, 'list_price_per_m': {'input': 0.1, 'output': 0.4}},
           'duration_multiplier': {'value': dur, 'status': 'ok', 'source': 'arena.agent_sessions'},
           'intelligence_index': 50.0,
           'image_input': {'value': img_val, 'status': image_input,
                           'source': 'aa.modality' if image_input in ('yes', 'no') else None}}
    if fast is not None:
        out['fast_mode'] = fast
    return out


def document(models, **override):
    rows = {name: row() for name, info in models.items() if isinstance(info, dict)}
    rows['claude-opus-5-5'] = row(1.0, 15.0)
    rows.update(override)
    return {'models': rows, 'anchor': ANCHOR, 'capacity': CAPACITY}


def window(usd, age=3, ref=None):
    return {'unit': 'usd', 'capacity_usd': usd, 'status': 'ok', 'age_days': age, 'reference_model': ref}


# Community capacity per family / plan / window, as five_axis.capacity writes it (Max 20x weekly_fable left out).
CAPACITY = {'claude': {'max20': {'seven_day': window(1700.0, ref='claude-opus-5-5'),
                                 'five_hour': window(270.0, ref='claude-opus-5-5')}},
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

    def test_the_fit_follows_the_demanded_axes_continuously_and_no_floor_excludes(self):
        # Sol: precise coder, weak divergence. Round 7: nothing excludes it on a creative task; its fit pays for the
        # shortfall on the demanded axis instead, continuously.
        doc = document(self.accounts['models'], **{'gpt-6.1-sol': row(demand(0.2, 0.9, 1.0, 0.5))})
        coding = self.plan(self.accounts, doc, profile={'axis_demand': demand(0, 1, 2, 0)})
        creative = self.plan(self.accounts, doc, profile={'axis_demand': demand(3, 1, 0, 0), 'code_complexity': 0})
        sol_high = lambda d: next(c for c in self.rows_for(d, 'gpt-6.1-sol') if c['bundle'].get('effort') == 'high')
        for d in (coding, creative):
            self.assertFalse([e for c in d['ranking'] for e in c['excluded'] if 'floor' in e])
            self.assertFalse([c for c in d['ranking'] if c.get('below_floor')])
        self.assertEqual(sol_high(creative)['excluded'], [])
        self.assertGreater(sol_high(creative)['five_axis_fit']['divergence']['shortfall'], 0.5)
        self.assertGreater(sol_high(coding)['components']['fit'], sol_high(creative)['components']['fit'] + 0.3)
        self.assertEqual(sol_high(coding)['components']['five_axis_shortfall'], 0.0)

    def test_fit_is_capped_at_the_anchor_target_with_shortfall_penalty_and_no_bonus_above_need(self):
        targets = allocator.five_axis_targets(demand(3, 1, 0, 2))
        self.assertEqual(targets, {'divergence': 0.8, 'global_rigor': round(0.8 / 3, 4), 'vision_tools': round(1.6 / 3, 4)})
        scaled = {'divergence': 0.7, 'global_rigor': 0.9, 'module_precision': 0.1, 'vision_tools': 0.5}
        fit, rows = allocator.five_axis_fit(scaled, demand(3, 1, 0, 2), targets, 1.0)
        # Round 11: per axis min(s, t) - 1.0 * max(0, t - s); global_rigor 0.9 is capped at target 0.2667.
        expected = (3 * (0.7 - 0.1) + 1 * round(0.8 / 3, 4) + 2 * (0.5 - (round(1.6 / 3, 4) - 0.5))) / 6
        self.assertAlmostEqual(fit, expected, places=4)
        self.assertEqual(rows['global_rigor']['capped_score'], round(0.8 / 3, 4))
        self.assertNotIn('module_precision', rows)           # an undemanded axis does not count
        # Above target: no bonus above the task's need.
        nudged_above = allocator.five_axis_fit({**scaled, 'global_rigor': 0.98}, demand(3, 1, 0, 2), targets, 1.0)[0]
        self.assertEqual(nudged_above, fit)
        # Below target: continuous with slope 2 * d / sum_d.
        for axis, eps in (('divergence', 0.01), ('vision_tools', -0.04)):
            nudged = allocator.five_axis_fit({**scaled, axis: scaled[axis] + eps}, demand(3, 1, 0, 2), targets, 1.0)[0]
            self.assertLess(abs(nudged - fit), 2 * abs(eps))
            self.assertEqual(nudged > fit, eps > 0)

    def test_no_other_dimension_set_moves_the_five_axis_decision(self):
        """Round 7 (user): in the five-axis path nothing but the axes scores or gates on quality. Perturb every other
        dimension set (tier, subjective and capability vectors, the visual ability, route preference/restriction/
        capability lists, route and policy ability floors, harness quality factors, the free-usage bonus, bundle `when`
        scopes, graded-ability spans, effort penalty and quality-bonus weights): every candidate's score and exclusions
        must stay the same. Fails as soon as one of them reaches the five-axis path again."""
        doc = document(self.accounts['models'], **{'gpt-6.1-sol': row(demand(0.6, 0.9, 1.0, 0.5), usd=4.0),
                                                   'claude-sonnet-5-5': row(demand(0.7, 0.75, 0.7, 0.8), usd=9.0)})
        tasks = [{'profile': {'axis_demand': demand(0, 1, 2, 0)}},
                 {'profile': {'axis_demand': demand(3, 2, 0, 0), 'code_complexity': 0, 'interaction_style': 'exploration'}},
                 {'profile': {'axis_demand': demand(1, 3, 3, 0), 'reasoning_depth': 3, 'code_complexity': 3, 'error_cost': 3,
                              'hallucination_risk': 3}},
                 {'profile': {'axis_demand': demand(0, 1, 1, 2), 'needs_visual': True}, 'work_kind': 'visual_analysis'}]
        bent = copy.deepcopy(self.accounts)
        for i, info in enumerate(m for m in bent['models'].values() if isinstance(m, dict)):
            info['tier'] = 1.0 + (i * 7) % 5
            info['subjective'] = {k: (i % 3) / 2 for k in ('quality', 'divergence', 'rigor', 'capability', 'speed')}
            info['capabilities'] = {'visual': i % 2, 'gui': (i + 1) % 2, 'reasoning': 0.1 * (i % 10)}
            info['visual'] = bool(i % 2)
            info['score_estimated'] = bool(i % 3)
        policy = bent['scoring']
        policy['harness_factors'] = {'default': 0.3, 'by_harness': {'claude': 2.0, 'codex': 0.1}}
        policy['ability_floor'] = {k: 9.0 for k in policy['ability_floor']}
        policy['route_ability_floor'] = {'code_implementation': {'routine': 9.5}}
        policy['ability_score_origin'] = {k: 0.0 for k in policy['ability_floor']}
        policy.update(ability_span=0.05, extra_effort_penalty=0.5, high_precision_effort_weight=2.0, unlimited_bonus=0.9,
                      deferred_start_penalty=9.0)
        policy.setdefault('profile', {}).update(saturation_span=0.01, vector_weight=1.0,
                                                subjective={'weight': 1.0, 'vector_weight': 0.0, 'route_weight': 0.0})
        for spec in bent['accounts'].values():
            if isinstance(spec, dict):
                spec['free_usage_bonus'] = True
                for b in (spec.get('bundles') or {}).values():
                    if isinstance(b, dict):
                        b['when'] = {'min_difficulty': 'frontier', 'max_reasoning_depth': 1}
        caps = copy.deepcopy(allocator.load_capabilities(accounts=self.accounts))
        bent_caps = copy.deepcopy(caps)
        for route in bent_caps['routes'].values():
            route['preferred'] = ['dot', 'grok-4.7']
            route['restrict_models'] = True
            route['capability_fit'] = {'gui': 1.0}
            route['strict_order'] = True
            route['ability_floor'] = {'routine': 9.0, 'hard': 9.0, 'coupled': 9.0, 'frontier': 9.0}

        def run(accounts, capabilities, task):
            acc = copy.deepcopy(accounts)
            acc['scoring']['five_axis'] = {'enabled': True}
            original = allocator.load_five_axis
            allocator.load_five_axis = lambda path=None: doc
            try:
                t = copy.deepcopy(task)
                profile = {**PROFILE, **t.pop('profile', {})}
                d = allocator.plan({'objective': 't', 'profile': profile, **t}, self.usage(acc), NOW, accounts=acc,
                                   capabilities=copy.deepcopy(capabilities), overrides=[], records=[],
                                   host_policy_config=self.policy)
            finally:
                allocator.load_five_axis = original
            return d['choice'], {c['id']: (c['score'], tuple(c['excluded'])) for c in d['ranking']}

        for task in tasks:
            shipped, perturbed = run(self.accounts, caps, task), run(bent, bent_caps, task)
            self.assertEqual(shipped[0], perturbed[0], task)
            diff = {k: (v, perturbed[1].get(k)) for k, v in shipped[1].items() if perturbed[1].get(k) != v}
            self.assertEqual(diff, {}, task)

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
        self.assertNotIn('reference_usd_per_hour', spec)     # round 11: replaced by per-window beta_w
        # Max 20x weekly_fable has no community value in CAPACITY here: unknown, with neither the template figure nor the burn prior.
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
        # OpenCode ("free" pool) is an ordinary quota pool on the five-axis path: no unmetered/unlimited flag, capacity_unknown.
        self.assertFalse(out['accounts']['opencode'].get('unmetered'))
        self.assertFalse(out['accounts']['opencode'].get('unlimited'))
        self.assertTrue(out['accounts']['opencode']['capacity_unknown'])

    def test_tie_break_order_net_score_then_earliest_expiring_under_line_then_faster_then_fewer_usd(self):
        """Round 11 (advisor report v2 §0, §4-5): order is N, then earliest-expiring under-line quota, then faster
        completion (throughput / fast mode), then fewer USD, then axis level."""
        def cand(name, score=0.5, reset_u=60.0, own_over=0.0, hours=1.0, usd=10.0, level=0.8, wait=0.0):
            return {'id': name, 'score': score, 'available_in_hours': wait or None,
                    'expected_hours': hours, 'expected_usd': usd,
                    'components': {'earliest_under_line_reset_hours': reset_u, 'own_over_line_usd': own_over,
                                   'five_axis_level': level, 'harness_place': 0}}
        pool = [
            cand('later_reset', score=0.5, reset_u=110.0, hours=0.2, usd=1.0),
            cand('slower_expensive', score=0.5, reset_u=60.0, hours=0.8, usd=20.0),
            cand('faster_dear', score=0.5, reset_u=60.0, hours=0.2, usd=20.0),
            cand('faster_cheap', score=0.5, reset_u=60.0, hours=0.2, usd=5.0),
            cand('higher_n', score=0.6, reset_u=None, own_over=50.0, hours=2.0, usd=50.0),
            cand('borrowed_over_line', score=0.5, reset_u=None, own_over=15.0, hours=0.1, usd=1.0),
        ]
        self.assertEqual([c['id'] for c in sorted(pool, key=allocator.five_axis_order)],
                         ['higher_n', 'faster_cheap', 'faster_dear', 'slower_expensive', 'later_reset', 'borrowed_over_line'])
        self.assertEqual(allocator.FIVE_AXIS_DEFAULTS['weights']['fit'], 1.0)
        for gone in ('overqualification_weight', 'strong_level', 'pace', 'pace_smoothing'):
            self.assertNotIn(gone, allocator.FIVE_AXIS_DEFAULTS, gone)
        for gone in ('five_axis_overqualification', 'five_axis_gate', 'five_axis_gate_level',
                     'five_axis_pacing', 'five_axis_wait_cost'):
            self.assertFalse(hasattr(allocator, gone), gone)

    def test_recovery_line_x_within_period_across_reset_unstarted_and_infinite_reset(self):
        """Round 11 (advisor report v2 §1-2, §5.1): recovery line l(t) = C * (t - t0) / L, over-line spend X segmented
        across resets, unstarted rolling windows start at t0 = now, and starter allowances (T = inf) have l(t) = 0 so X = S."""
        # 1. Behind the line (50% elapsed = $850 line, 30% used = $510, δ = +$340): a $100 2h task has X = 0.
        behind = {'known': True, 'full': 100.0, 'hours': 168.0, 'remaining': 70.0, 'reset_hours': 84.0}
        r_behind = allocator.five_axis_over_line_usd(behind, 1700.0, 100.0, 2.0)
        self.assertAlmostEqual(r_behind['line_now_usd'], 850.0, places=2)
        self.assertAlmostEqual(r_behind['used_usd'], 510.0, places=2)
        self.assertAlmostEqual(r_behind['under_line_usd'], 340.0, places=2)
        self.assertEqual(r_behind['over_line_usd'], 0.0)
        # 2. Exactly on the line (50% elapsed = $850, 50% used = $850): only spend above recovery C/L * h ($20.24) is over line.
        on_line = {'known': True, 'full': 100.0, 'hours': 168.0, 'remaining': 50.0, 'reset_hours': 84.0}
        r_on = allocator.five_axis_over_line_usd(on_line, 1700.0, 50.0, 2.0)
        self.assertAlmostEqual(r_on['over_line_usd'], 50.0 - 1700.0 * 2.0 / 168.0, places=3)
        # 3. Ahead of the line (50% elapsed = $850, 70% used = $1190): l(t1) = $870.24 < U, so A_0 = 0 and X = full S ($50).
        ahead = {'known': True, 'full': 100.0, 'hours': 168.0, 'remaining': 30.0, 'reset_hours': 84.0}
        r_ahead = allocator.five_axis_over_line_usd(ahead, 1700.0, 50.0, 2.0)
        self.assertAlmostEqual(r_ahead['over_line_usd'], 50.0, places=3)
        # 4. Segmenting across a reset (tau_reset = 2h, h = 6h, R = $681.8, S = 6 * 22.86 = $137.16):
        #    Pre-reset 2h spends $45.72 <= R ($681.8) so X_0 = 0; post-reset 4h spends $91.44 vs 4 * 1704.5/168 = $40.58 -> X_1 = 50.86.
        cross = {'known': True, 'full': 100.0, 'hours': 168.0, 'remaining': 40.0, 'reset_hours': 2.0}
        r_cross = allocator.five_axis_over_line_usd(cross, 1704.5, 6.0 * 22.86, 6.0)
        self.assertAlmostEqual(r_cross['over_line_usd'], 4.0 * 22.86 - 1704.5 * 4.0 / 168.0, places=2)
        # 5. Unstarted rolling window (t0 = now, U = 0): l(now) = 0, allowance over h is C * h / L.
        unstarted = {'known': True, 'full': 100.0, 'hours': 168.0, 'remaining': 100.0, 'reset_hours': 168.0, 'unstarted': True}
        r_unstarted = allocator.five_axis_over_line_usd(unstarted, 2580.0, 6.0 * 22.86, 6.0)
        self.assertTrue(r_unstarted['unstarted'])
        self.assertAlmostEqual(r_unstarted['over_line_usd'], 6.0 * 22.86 - 2580.0 * 6.0 / 168.0, places=2)
        # 6. Starter allowance (T = inf, hours = None): l(t) = 0, so every dollar of spend is above the line (X = S).
        starter = {'known': True, 'full': 100.0, 'hours': None, 'remaining': 80.0, 'reset_hours': float('inf')}
        r_starter = allocator.five_axis_over_line_usd(starter, 50.0, 9.0, 2.0)
        self.assertEqual(r_starter['line_now_usd'], 0.0)
        self.assertEqual(r_starter['over_line_usd'], 9.0)

    def test_nested_windows_and_5h_plus_weekly_take_the_larger_over_line_x(self):
        """Round 11 (advisor report v2 §2, §5): 5h + weekly and nested pools (weekly_fable ⊂ seven_day) each compute their
        own over-line spend X and take the maximum (never double-counting)."""
        acc = copy.deepcopy(self.accounts)
        acc['scoring']['five_axis'] = {'enabled': True}
        doc = document(acc['models'], **{'claude-fable-5-1': row(0.85, usd=36.11)})
        doc['capacity'] = {
            'claude': {'max20': {
                'seven_day': window(1700.0, ref='claude-opus-5-5'),
                'five_hour': window(270.0, ref='claude-opus-5-5'),
                'weekly_fable': window(850.0, ref='claude-fable-5-1'),
            }},
            'codex': {'pro': {'usage': window(615.0)}},
        }
        acc = allocator.five_axis_overlay(acc, doc)
        usage = self.usage(acc, 'ample')
        # Set seven_day behind line (80% left at mid-week -> under-line $510), but weekly_fable ahead of line (30% left at mid-week).
        for w in usage['accounts']['claude']['windows']:
            if w['id'] == 'seven_day':
                w['remaining_pct_before_reset'] = 80.0
                w['seconds_until_reset'] = 84 * 3600
            elif w['id'] == 'five_hour':
                w['remaining_pct_before_reset'] = 90.0
                w['seconds_until_reset'] = int(2.5 * 3600)
            elif w['id'] == 'weekly_fable':
                w['remaining_pct_before_reset'] = 30.0
                w['seconds_until_reset'] = 84 * 3600
        original = allocator.load_five_axis
        allocator.load_five_axis = lambda path=None: doc
        try:
            d = allocator.plan({'objective': 't', 'hours': 1.0, 'profile': {**PROFILE, 'axis_demand': demand(2, 2, 2, 0)}},
                               usage, NOW, accounts=acc, overrides=[], records=[], host_policy_config=self.policy)
        finally:
            allocator.load_five_axis = original
        fable_high = next(c for c in d['ranking'] if c['id'] == 'fable51_high')
        opus_high = next(c for c in d['ranking'] if c['id'] == 'opus55_high')
        # Opus draws seven_day + five_hour (both behind line -> X = 0); Fable also draws weekly_fable (ahead of line -> X = $36.11).
        self.assertEqual(opus_high['components']['own_over_line_usd'], 0.0)
        self.assertAlmostEqual(fable_high['components']['own_over_line_usd'], 36.11, places=2)
        self.assertEqual(fable_high['capacity']['window'], 'weekly_fable')

    def test_substitute_pool_absorbs_over_line_spend_and_advisor_desync_example(self):
        """Round 11 (advisor report v2 §2, §6.3): a desynchronized same-model substitute pool absorbs over-line spend with
        its non-expiring under-line quota. Verifies the advisor's §6.3 example (Pool A 2h to reset with 40% left, Pool B
        6 days to reset on the line): 3h task picks Pool A with fast mode (entire spend before reset), 6h task picks
        Pool A in normal mode (saving $45.7 before reset while Pool B absorbs the post-reset $50.9)."""
        acc = copy.deepcopy(self.accounts)
        acc.pop('_discovery', None)
        acc['scoring']['five_axis'] = {'enabled': True}
        # Configure Pool A (`claude`, C=1704.5) and Pool B (`claude-2`, C=2580.0, 1 account on its recovery line).
        acc['accounts']['claude']['windows'] = {'seven_day': {'hours': 168}, 'five_hour': {'hours': 5}}
        acc['accounts']['claude-2']['windows'] = {'seven_day': {'hours': 168}, 'five_hour': {'hours': 5}}
        acc['accounts']['claude-2']['plan'] = 'pool_b'
        acc['accounts']['claude']['bundles'] = {
            'opus55_high': {'provider': 'claude', 'harness': 'claude', 'account': 'claude', 'model': 'claude-opus-5-5', 'effort': 'high'},
        }
        acc['accounts']['claude-2']['bundles'] = {
            'claude-2__opus55_high': {'provider': 'claude', 'harness': 'claude', 'account': 'claude-2', 'model': 'claude-opus-5-5', 'effort': 'high'},
        }
        fast_cfg = {'status': 'ok', 'price_multiplier': 2.0, 'speed_multiplier': 1.5, 'source': 'anthropic.fast_mode'}
        base_rows = {name: row(0.5, usd=5.0) for name, info in acc['models'].items() if isinstance(info, dict)}
        doc = document(
            acc['models'],
            **{
                **base_rows,
                'claude-opus-5-5': row(demand(0.7333, 0.8000, 0.8000, 0.8000), usd=22.86, dur=1.0, fast=fast_cfg),
                'claude-sonnet-5-5': row(demand(0.6000, 0.6500, 0.6000, 0.7000), usd=12.83, dur=0.98),
                'claude-haiku-5-5': row(demand(0.5000, 0.5000, 0.4500, 0.5000), usd=2.67, dur=0.11),
                'glm-5.3-flash-free': row(demand(0.6820, 0.5580, 0.5940, 0.5000), usd=1.50, image_input='no'),
            },
        )
        doc['capacity'] = {
            'claude': {
                'max20': {'seven_day': window(1704.5, ref='claude-opus-5-5'),
                          'five_hour': window(1000.0, ref='claude-opus-5-5')},
                'pool_b': {'seven_day': window(2580.0, ref='claude-opus-5-5'),
                           'five_hour': window(1000.0, ref='claude-opus-5-5')},
            },
            'codex': {'pro': {'usage': window(615.0)}},
        }
        usage = self.usage(acc, 'ample')
        # Pool A (`claude`): 2h until reset, 40% remaining (R = $681.8).
        usage['accounts']['claude']['windows'] = [
            {'id': 'seven_day', 'remaining_pct_before_reset': 40.0, 'seconds_until_reset': 2 * 3600},
            {'id': 'five_hour', 'remaining_pct_before_reset': 95.0, 'seconds_until_reset': 5 * 3600},
        ]
        # Pool B (`claude-2`): 144h (6 days) until reset, exactly on the recovery line (elapsed 24/168 = 14.2857% -> 85.7143% left).
        usage['accounts']['claude-2'] = {
            'status': 'fresh', 'available': True, 'fetched_at': NOW.isoformat(),
            'windows': [
                {'id': 'seven_day', 'remaining_pct_before_reset': 100.0 * 144.0 / 168.0, 'seconds_until_reset': 144 * 3600},
                {'id': 'five_hour', 'remaining_pct_before_reset': 95.0, 'seconds_until_reset': 5 * 3600},
            ],
        }

        def run_case(hours):
            original = allocator.load_five_axis
            allocator.load_five_axis = lambda path=None: doc
            try:
                return allocator.plan(
                    {'objective': 'frontier rewrite', 'expected_hours': hours,
                     'profile': {**PROFILE, 'axis_demand': demand(2, 3, 3, 0), 'expected_hours': hours}},
                    copy.deepcopy(usage), NOW, accounts=copy.deepcopy(acc),
                    overrides=[], records=[], host_policy_config={'allowed_accounts': ['claude', 'claude-2']},
                )
            finally:
                allocator.load_five_axis = original

        # 3h task: Pool A (`opus55_high`) wins and enables fast mode (finishes in 2h before reset, X_own = 0, N = 0.7333).
        d3 = run_case(3.0)
        self.assertEqual(d3['choice'], 'opus55_high')
        self.assertTrue(d3['selected_bundle'].get('fast'))
        top3 = next(c for c in d3['ranking'] if c['id'] == 'opus55_high')
        b3 = next(c for c in d3['ranking'] if c['id'] == 'claude-2__opus55_high')
        self.assertAlmostEqual(top3['score'], 0.7333, places=3)
        self.assertEqual(top3['components']['own_over_line_usd'], 0.0)
        self.assertEqual(top3['components']['net_over_line_usd'], 0.0)
        self.assertAlmostEqual(b3['components']['own_over_line_usd'], 68.58 - 3.0 * (2580.0 / 168.0), places=1)
        self.assertAlmostEqual(b3['components']['net_over_line_usd'],
                               b3['components']['own_over_line_usd'] - 1.0 * (1704.5 / 168.0), places=1)

        # 6h task: Pool A (`opus55_high`) wins in normal mode (fast mode stays off because post-reset fast spend exceeds line;
        # Pool A normal has own_over ~50.9 absorbed by Pool B's 92.1 under-line allowance -> net_over = 0, N = 0.7333 > Pool B's 0.7278).
        d6 = run_case(6.0)
        self.assertEqual(d6['choice'], 'opus55_high')
        self.assertFalse(d6['selected_bundle'].get('fast'))
        top6 = next(c for c in d6['ranking'] if c['id'] == 'opus55_high')
        b6 = next(c for c in d6['ranking'] if c['id'] == 'claude-2__opus55_high')
        self.assertAlmostEqual(top6['components']['own_over_line_usd'], 4.0 * 22.86 - 4.0 * (1704.5 / 168.0), places=1)
        self.assertEqual(top6['components']['net_over_line_usd'], 0.0)
        self.assertAlmostEqual(top6['score'], 0.7333, places=3)
        self.assertAlmostEqual(b6['components']['own_over_line_usd'], 6.0 * 22.86 - 6.0 * (2580.0 / 168.0), places=1)
        self.assertAlmostEqual(b6['components']['net_over_line_usd'],
                               b6['components']['own_over_line_usd'] - 4.0 * (1704.5 / 168.0), places=1)
        self.assertLess(b6['score'], top6['score'])

    def test_image_input_exclusion_and_unknown_modality_mode_b(self):
        """Round 11 item 4: when vision_tools >= 2, models with public image_input == 'no' are excluded; models with
        unknown modality leave automatic routing under Mode B (dispatched with warning only when named via --model)."""
        doc = document(
            self.accounts['models'],
            **{
                'gpt-6.1-sol': row(0.85, usd=6.12, image_input='no'),
                'claude-sonnet-5-5': row(0.85, usd=12.83, image_input='unknown'),
                'claude-opus-5-5': row(0.85, usd=22.86, image_input='yes'),
            },
        )
        # Level 2 (reading images) and Level 3 (operating GUI via screenshots):
        for vis_level in (2, 3):
            d = self.plan(self.accounts, doc, profile={'axis_demand': demand(1, 2, 2, vis_level)})
            sol_rows = self.rows_for(d, 'gpt-6.1-sol')
            sonnet_rows = self.rows_for(d, 'claude-sonnet-5-5')
            opus_rows = [c for c in self.rows_for(d, 'claude-opus-5-5') if not c['excluded']]
            self.assertTrue(all(any('model does not accept image input' in e for e in c['excluded']) for c in sol_rows))
            self.assertTrue(all(any('image_input modality has no cited source' in e for e in c['excluded']) for c in sonnet_rows))
            self.assertTrue(opus_rows)
        # Naming the unknown-modality model via --model dispatches it with a Mode B warning:
        named = self.plan(self.accounts, doc, profile={'axis_demand': demand(1, 2, 2, 2)},
                          constraints={'model': 'claude-sonnet-5-5'})
        chosen = next(c for c in named['ranking'] if c['id'] == named['choice'])
        self.assertEqual(allocator.canonical(chosen['bundle']['model'], self.accounts), 'claude-sonnet-5-5')
        self.assertIn('data error: image_input modality has no cited source; dispatched because it was named',
                      ' '.join(chosen['notes']))
        # Naming a model whose public data explicitly says no image input stays blocked even when named:
        blocked = self.plan(self.accounts, doc, profile={'axis_demand': demand(1, 2, 2, 2)},
                            constraints={'model': 'gpt-6.1-sol'})
        self.assertEqual(blocked['choice'], 'none')
        # Level 0-1 (no image input needed): neither is excluded for modality.
        text_only = self.plan(self.accounts, doc, profile={'axis_demand': demand(1, 2, 2, 1)})
        self.assertFalse(any('image input' in e or 'image_input' in e
                             for c in text_only['ranking'] for e in c['excluded']))

    def test_free_pool_mode_b_and_opencode_db_local_spend_lower_bound(self):
        """Round 11 item 5: 'free' models are an ordinary quota pool; when capacity is unreadable (opencode), they are
        marked capacity_unknown (out of automatic routing, surfaced in plan warnings and tray warnings, runnable via
        --model with 'quota unknown, spend unpriced' warning, and opencode.db token counts x public list price give U)."""
        import account_state
        tray_dir = str(Path(__file__).resolve().parents[3] / 'tray')
        if tray_dir not in sys.path:
            sys.path.insert(0, tray_dir)
        import router_data
        doc = document(self.accounts['models'], **{'glm-5.3-flash-free': row(0.75, usd=1.50, image_input='no')})
        acc = copy.deepcopy(self.accounts)
        acc['accounts']['opencode']['bundles'] = {
            'glm53_free': {'provider': 'opencode', 'harness': 'opencode', 'account': 'opencode', 'model': 'glm-5.3-flash-free'}
        }
        acc.setdefault('_discovery', {}).setdefault('accounts', {})['opencode'] = {
            'usage': {'models': {'glm-5.3-flash-free': {'input': 1_000_000, 'output': 500_000, 'cache_read': 0, 'cost': 0.0}}}
        }
        auto = self.plan(acc, doc, profile={'axis_demand': demand(1, 2, 2, 0)})
        glm_rows = self.rows_for(auto, 'glm-5.3-flash-free')
        self.assertTrue(glm_rows)
        self.assertTrue(all(any('capacity_unknown (quota unknown, spend unpriced)' in e for e in c['excluded'])
                            for c in glm_rows))
        self.assertTrue(any('opencode: capacity_unknown — quota unknown, spend unpriced' in w
                            for w in auto['decision'].get('warnings', ())))
        # Naming --model glm-5.3-flash-free runs it with the "quota unknown, spend unpriced" warning and local spend U:
        named = self.plan(acc, doc, profile={'axis_demand': demand(1, 2, 2, 0)},
                          constraints={'model': 'glm-5.3-flash-free'})
        chosen = next(c for c in named['ranking'] if c['id'] == named['choice'])
        self.assertEqual(allocator.canonical(chosen['bundle']['model'], acc), 'glm-5.3-flash-free')
        self.assertIn('capacity_unknown (quota unknown, spend unpriced); dispatched because it was named',
                      ' '.join(chosen['notes']))
        self.assertIn('local opencode.db lower-bound spend', ' '.join(chosen['notes']))
        # Tray / account_state warnings surface opencode when five_axis is enabled:
        overlaid = copy.deepcopy(acc)
        overlaid['scoring']['five_axis'] = {'enabled': True}
        overlaid = allocator.five_axis_overlay(overlaid, doc)
        self.assertGreater(overlaid['accounts']['opencode'].get('local_spend_usd', 0.0), 0.0)
        ws = account_state.warnings(overlaid.get('_discovery'), overlaid['accounts'])
        self.assertTrue(any('opencode:' in w and 'quota unknown, spend unpriced' in w for w in ws), ws)
        self.assertTrue(overlaid['accounts']['opencode'].get('five_axis_free_pool'))

    def test_fable_5_2_on_five_axis_path_and_never_beats_opus_5_5_via_artifacts(self):
        """Round 11 item 6: claude-fable-5-2 has public five-axis scores (partial axes count missing components as 0),
        labelled option-B fallback usd_per_hour, Anthropic fast-mode pricing ($16/$80 = 2x, 2.5x speed), and never beats
        Opus 5.5 via data artifacts."""
        shipped = allocator.load_five_axis()
        fable52 = shipped['models']['claude-fable-5-2']
        opus55 = shipped['models']['claude-opus-5-5']
        self.assertEqual(fable52['usd_per_hour']['kind'], 'derived')
        self.assertTrue(fable52['usd_per_hour']['fallback'])
        self.assertGreater(fable52['usd_per_hour']['value'], opus55['usd_per_hour']['value'])
        self.assertEqual(fable52['fast_mode']['status'], 'ok')
        self.assertEqual((fable52['fast_mode']['price_multiplier'], fable52['fast_mode']['speed_multiplier']), (2.0, 2.5))
        self.assertEqual(fable52['image_input']['status'], 'unknown')
        # Partial axes on Fable 5.2 count missing Arena components as 0, so its module_precision and vision_tools are <= Opus 5.5.
        self.assertLessEqual(fable52['scores']['module_precision'], opus55['scores']['module_precision'])
        self.assertLessEqual(fable52['scores']['vision_tools'], opus55['scores']['vision_tools'])

    def test_no_history_read_by_five_axis_economics(self):
        """Round 11 item 2: five-axis economics reads ONLY current-period window state (C, U, t0, T) + in-flight leases +
        public model data. Fails if demand_forecast, window_cost, usage_samples, capacity.json, or completed ledger
        history is read or changes any five-axis score."""
        from unittest.mock import patch
        doc = document(self.accounts['models'])
        acc = copy.deepcopy(self.accounts)
        acc['scoring']['five_axis'] = {'enabled': True}
        usage = self.usage(acc, 'ample')
        # Inject poisoned 24h burn rates into usage windows and completed history into records:
        usage_poisoned = copy.deepcopy(usage)
        for row_u in usage_poisoned['accounts'].values():
            for w in row_u.get('windows', ()):
                w['pct_per_hour_24h'] = 999.0
                w['historical_samples'] = [1, 2, 3]
        completed_history = [
            {'ts': '2026-11-04T08:00:00Z', 'status': 'completed',
             'bundle': {'account': 'claude', 'model': 'claude-opus-5-5', 'effort': 'high', 'bundle_id': 'opus55_high'},
             'expected_usd': 500.0, 'expected_hours': 10.0}
        ]
        original = allocator.load_five_axis
        allocator.load_five_axis = lambda path=None: doc
        try:
            with patch.object(allocator, 'demand_forecast', side_effect=AssertionError('demand_forecast must not be called on five-axis path')), \
                    patch.object(allocator, 'window_cost', side_effect=AssertionError('window_cost must not be called on five-axis path')):
                clean = allocator.plan({'objective': 't', 'profile': {**PROFILE, 'axis_demand': demand(2, 3, 2, 0)}},
                                       usage, NOW, accounts=copy.deepcopy(acc), overrides=[], records=[],
                                       host_policy_config=self.policy)
                poisoned = allocator.plan({'objective': 't', 'profile': {**PROFILE, 'axis_demand': demand(2, 3, 2, 0)}},
                                          usage_poisoned, NOW, accounts=copy.deepcopy(acc), overrides=[],
                                          records=completed_history, host_policy_config=self.policy)
        finally:
            allocator.load_five_axis = original
        self.assertEqual(clean['choice'], poisoned['choice'])
        self.assertEqual({c['id']: c['score'] for c in clean['ranking']},
                         {c['id']: c['score'] for c in poisoned['ranking']})

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

    def test_can_wait_hours_defers_only_for_higher_net_score_without_fixed_wait_cost_constant(self):
        """Rounds 8-11: no fixed 0.15 delay penalty and no 0.06 five_axis_wait_cost constant on the five-axis path.
        Without `can_wait_hours`, `plan()` starts the best ready-now candidate immediately; when `can_wait_hours` is given,
        routine tasks (already reaching the capped fit on a ready-now owner) still start now while frontier tasks prefer
        waiting for the higher-N model's short-window reset."""
        self.assertFalse(hasattr(allocator, 'five_axis_wait_cost'))
        base = {name: row(0.55, usd=5.0) for name, info in self.accounts['models'].items() if isinstance(info, dict)}
        doc = document(self.accounts['models'],
                       **{**base,
                          'claude-opus-5-5': row(demand(0.733, 0.800, 0.800, 0.800), usd=22.86),
                          'gpt-6.1-sol': row(demand(0.587, 0.788, 0.787, 0.732), usd=6.12)})
        # Without can_wait_hours: resolves to the best immediately startable candidate (never defer, never needs_user_decision).
        immediate = self.plan(self.accounts, doc, state='5h-only scarce',
                              profile={'axis_demand': demand(3, 3, 0, 0), 'code_complexity': 0})
        self.assertEqual(immediate['decision']['action'], 'start')
        self.assertFalse(immediate['decision']['needs_user_decision'])
        self.assertEqual(immediate['selected_bundle']['account'], 'codex')
        self.assertIn(immediate['decision']['better_if_waiting']['bundle'], ('opus55_xhigh', 'opus55_max'))
        # With can_wait_hours: routine starts now, frontier defers for the capable model's reset.
        routine = self.plan(self.accounts, doc, state='5h-only scarce', can_wait_hours=4.0,
                            profile={'axis_demand': demand(0, 1, 1, 0)})
        frontier = self.plan(self.accounts, doc, state='5h-only scarce', can_wait_hours=4.0,
                             profile={'axis_demand': demand(3, 3, 0, 0), 'code_complexity': 0})
        self.assertEqual(routine['decision']['action'], 'start')
        self.assertEqual(routine['selected_bundle']['account'], 'codex')
        self.assertEqual(frontier['decision']['action'], 'defer')
        self.assertEqual(frontier['selected_bundle']['account'], 'claude')
        self.assertGreater(frontier['decision']['start_after_hours'], 0.0)

    def test_roll_launches_best_ready_now_without_can_wait_and_never_launches_a_defer(self):
        """Rounds 9-10 (F1 + --can-wait-hours CLI): roll without --can-wait-hours launches the best ready-now candidate
        when 5h is empty on the top owner; when --can-wait-hours is passed on the CLI, plan and roll return
        start_after_hours (caller_action='wait_for_reset') and roll never launches into an empty window."""
        import contextlib
        import io
        import json
        from unittest.mock import patch
        import roll
        base = {name: row(0.55, usd=5.0) for name, info in self.accounts['models'].items() if isinstance(info, dict)}
        doc = document(self.accounts['models'],
                       **{**base,
                          'claude-opus-5-5': row(demand(0.733, 0.800, 0.800, 0.800), usd=22.86),
                          'gpt-6.1-sol': row(demand(0.587, 0.788, 0.787, 0.732), usd=6.12)})
        launched_bundles = []

        def run_roll_cli(extra_argv=()):
            acc = copy.deepcopy(self.accounts)
            acc['scoring']['five_axis'] = {'enabled': True}
            usage = self.usage(acc, '5h-only scarce')
            profile = {**PROFILE, 'axis_demand': demand(3, 3, 0, 0), 'code_complexity': 0}

            def fake_plan_stage(payload, cli_args):
                t = dict(payload['task'], profile=profile)
                if getattr(cli_args, 'can_wait_hours', None) is not None and t.get('can_wait_hours') is None:
                    t['can_wait_hours'] = cli_args.can_wait_hours
                original = allocator.load_five_axis
                allocator.load_five_axis = lambda path=None: doc
                try:
                    d = allocator.plan(t, usage, NOW, accounts=acc,
                                       overrides=[], records=[], host_policy_config=self.policy)
                finally:
                    allocator.load_five_axis = original
                return d, 0

            argv = ['route_judge.py', 'roll', '--task', 'frontier proof', '--task-id', 't-f1', '--hours', '2.0',
                    '--parent-id', 'parent-1', '--new-worker', *extra_argv]
            buf = io.StringIO()
            with patch.object(sys, 'argv', argv), \
                    patch.object(route_judge, 'plan_stage', side_effect=fake_plan_stage), \
                    patch.object(route_judge, 'usage_snapshot', return_value=usage), \
                    patch.object(route_judge.public_data, 'refresh_in_background'), \
                    patch.object(route_judge.antigravity_plan, 'refresh_in_background'), \
                    patch.object(roll, 'resolve_parent', return_value=('parent-1', 'cli', 'parent-1')), \
                    patch.object(roll.registry, 'pending', return_value=[]), \
                    patch.object(roll.registry, 'reusable_workers', return_value=[]), \
                    patch.object(roll, 'can_launch_via_paseo', return_value=False), \
                    patch.object(roll, 'cli', side_effect=lambda _a, out, b, _txt, _j: (
                        launched_bundles.append((b['account'], b['model'])) or
                        (dict(out, launched=True, dispatch_mode='cli',
                              caller_action='already_launched_do_not_respawn',
                              worker={'pid': 123, 'output': 'o', 'stderr': 'e'}), 0))), \
                    contextlib.redirect_stdout(buf):
                code = route_judge.main()
            return json.loads(buf.getvalue()), code

        res_now, code_now = run_roll_cli()
        self.assertEqual(code_now, 0)
        self.assertEqual(res_now['status'], 'launched')
        self.assertEqual(res_now['worker']['account'], 'codex')
        self.assertEqual(launched_bundles, [('codex', 'gpt-6.1-sol')])

        launched_bundles.clear()
        res_defer, code_defer = run_roll_cli(['--can-wait-hours', '4'])
        self.assertEqual(code_defer, 2)
        self.assertEqual(res_defer['status'], 'blocked')
        self.assertEqual(res_defer['caller_action'], 'wait_for_reset')
        self.assertEqual(res_defer['worker']['account'], 'claude')
        self.assertEqual(res_defer['start_after_hours'], 3.0)
        self.assertEqual(launched_bundles, [])


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

    def test_a_new_top_model_moves_no_value_and_no_target(self):
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
        # The fit's targets are the anchor value x demand / 3: a new top model moves none of them.
        acc = sim.fresh_accounts(shipped_template(), snapshot())
        acc['scoring']['five_axis'] = {'enabled': True}
        targets = []
        for extra in ({}, {'newtop': row(1.0, 30.0)}):
            out = allocator.five_axis_overlay(acc, document(acc['models'], **{self.ms.ANCHOR: row(0.8, 21.7)}, **extra))
            targets.append(allocator.five_axis_targets(demand(1, 2, 2, 0), out['_five_axis']['anchor_value']))
        self.assertEqual(targets[0], targets[1])
        self.assertEqual(targets[0]['global_rigor'], round(self.ms.ANCHOR_VALUE * 2 / 3, 4))

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

    def test_a_backfill_stays_inside_the_measured_range_and_tier_is_no_predictor(self):
        import five_axis
        self.assertEqual(five_axis.PREDICTORS, ('aa.index',))
        f = five_axis.Scorer.fit([(x, 0.5 + 0.03 * x) for x in range(10)])     # measured 0.50 .. 0.77
        self.assertEqual((f['y_min'], f['y_max']), (0.5, 0.77))
        self.assertEqual(five_axis.Scorer.predict(f, 20), 0.77)       # 1.1 on the line: capped at the best measured
        self.assertEqual(five_axis.Scorer.predict(f, -5), 0.5)
        self.assertEqual(five_axis.Scorer.predict(f, 5), 0.65)
        scorer = types.SimpleNamespace(trusted=lambda entries: list(entries or ()))
        self.assertEqual(five_axis.Scorer.predictors(scorer, {'tier': [{'source': 'lmarena.tier_fit', 'value': 4.08}]}), {})

    def test_shipped_data_has_no_local_flag_or_sample_sources(self):
        import json
        ref = Path(__file__).resolve().parents[1] / 'references'
        meas = json.loads((ref / 'public-measurements.json').read_text(encoding='utf-8'))
        used = {e['source'] for fields in meas['models'].values() for entries in fields.values() for e in entries}
        self.assertFalse(used & {'accounts.visual', 'packaged.accounts', 'community.burn'})
        scores = json.loads((ref / 'five-axis-scores.json').read_text(encoding='utf-8'))
        self.assertEqual(scores['anchor'], {'model': self.ms.ANCHOR, 'value': self.ms.ANCHOR_VALUE})
        self.assertNotIn('floors', scores)                  # round 7: no difficulty floors
        self.assertAlmostEqual(min(scores['models'][self.ms.ANCHOR]['scores'][a] for a in ('divergence', 'module_precision')),
                               self.ms.ANCHOR_VALUE, places=2)

    def test_partial_axis_counts_missing_expected_components_as_zero(self):
        """Round 8 (user): pessimistic unknowns for partial axes. A model backed by a single component on an axis where
        the anchor has two components (e.g. module_precision from Alignment alone, no terminal and no AA index fit)
        counts the missing component as 0."""
        meas = {'models': {
            self.ms.ANCHOR: {'module_precision': [{'source': 'arena.agent_alignment', 'component': 'alignment', 'value': 0.8},
                                                  {'source': 'aa.terminalbench_4', 'component': 'terminal', 'value': 0.8}]},
            'single_board': {'module_precision': [{'source': 'arena.agent_alignment', 'component': 'alignment', 'value': 0.724}]},
        }}
        scorer = self.fa.Scorer(meas, {'models': {self.ms.ANCHOR: {}, 'single_board': {}}})
        rows, _ = scorer.score()
        self.assertEqual(rows[self.ms.ANCHOR]['axes']['module_precision']['value'], 0.8)
        ax = rows['single_board']['axes']['module_precision']
        self.assertEqual(ax['status'], 'partial')
        self.assertEqual(ax['missing_components'], ['terminal'])
        self.assertAlmostEqual(ax['value'], 0.724 / 2, places=4)


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

    def test_duration_is_from_arena_time_per_task_or_aa_fallback_never_flat_one(self):
        arena_tp = [{'source': 'arena.agent_sessions', 'kind': 'community', 'effort': 'high',
                     'time_per_task_min': 11.7, 'output_tokens_per_task': 19400, 'as_of': '2026-11-08'}]
        anchor_tp = [{'source': 'arena.agent_sessions', 'kind': 'community', 'effort': 'max',
                      'time_per_task_min': 16.3, 'output_tokens_per_task': 24300, 'as_of': '2026-11-08'}]
        d_arena = self.fa.pick_duration(self.fa.pick_usd(self.entries()), {'duration_multiplier': 1.35}, arena_tp, anchor_tp)
        self.assertEqual((d_arena['value'], d_arena['source'], d_arena['fallback']), (round(11.7 / 16.3, 4), 'arena.agent_sessions', False))
        self.assertEqual((d_arena['time_per_task_min'], d_arena['output_tokens_per_task']), (11.7, 19400))
        aa_tp = [{'source': 'aa.cost_time', 'kind': 'benchmark', 'effort': 'high', 'output_tps': 166.58}]
        d_aa = self.fa.pick_duration(self.fa.pick_usd(self.entries()), {'duration_multiplier': 1.35}, aa_tp,
                                     [{'source': 'aa.cost_time', 'effort': 'high', 'output_tps': 83.29}])
        self.assertEqual((d_aa['value'], d_aa['source'], d_aa['fallback']), (0.5, 'aa.cost_time', True))
        d_missing = self.fa.pick_duration(self.fa.pick_usd(self.entries()), {'duration_multiplier': 1.35}, (), ())
        self.assertEqual(d_missing['status'], 'missing')
        shipped = allocator.load_five_axis()
        mults = {m: r['duration_multiplier']['value'] for m, r in shipped['models'].items()
                 if r.get('duration_multiplier', {}).get('value') is not None}
        self.assertGreater(len(set(mults.values())), 5)


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
        self.assertTrue(usd['m']['fallback'])
        self.assertEqual(usd['m']['kind'], 'derived')
        self.assertEqual(usd['unlisted']['status'], 'missing')
        self.assertEqual(self.fa.pick_duration(usd['m'], {'duration_multiplier': 1.35},
                                               [{'source': 'aa.cost_time', 'effort': 'high', 'output_tps': 100.0}],
                                               [{'source': 'aa.cost_time', 'effort': 'high', 'output_tps': 50.0}])['value'], 0.5)
        # Round 8: when arena.agent_sessions is present, it is the primary source (kind community, fallback False).
        builder.out['m']['usd_per_hour'].append({'source': 'arena.agent_sessions', 'kind': 'community', 'effort': 'max',
                                                 'value': 6.125, 'as_of': '2026-11-08', 'url': 'https://arena.ai/leaderboard/agent',
                                                 'method': 'Cost/Task (P50) / Time/Task (P50) x 60', 'sessions': 1234})
        usd_arena = self.fa.USD_BASES['community_mix']['fn'](scorer, {k: {'fields': v} for k, v in builder.out.items()}, {}, None)
        self.assertEqual(usd_arena['m']['value'], 6.125)
        self.assertEqual(usd_arena['m']['used'], ['arena.agent_sessions'])
        self.assertEqual(usd_arena['m']['kind'], 'community')
        self.assertFalse(usd_arena['m']['fallback'])
        self.assertEqual(usd_arena['m']['sources']['arena.agent_sessions']['sessions'], 1234)

    def test_arena_matcher_handles_dated_and_versioned_labels_and_reports_true_miss_reason(self):
        """Round 9 F3: dated/variant labels such as `DeepSeek-V4.1-Flash-20261017 (Max)` and `Muse Wonder (Max)` match
        their models; a miss gives the true reason (without Cost/Task, distrusted, or not captured), not 'not listed'."""
        import measurements
        matcher = measurements.Matcher(['DeepSeek-V4.1-Flash-20261017 (Max)', 'DeepSeek-V4.1-Flash-20260910 (Max)',
                                        'Muse Wonder (Max)', 'Beam 1.0 501B A23B'])
        self.assertEqual(matcher.find('deepseek-v4.1-flash-free'), 'DeepSeek-V4.1-Flash-20261017 (Max)')
        self.assertEqual(matcher.find('muse-wonder-1.0'), 'Muse Wonder (Max)')
        shipped = allocator.load_five_axis()
        ds = shipped['models']['deepseek-v4.1-flash-free']['usd_per_hour']
        mw = shipped['models']['muse-wonder-1.0']['usd_per_hour']
        self.assertEqual((ds['value'], ds['kind'], ds['fallback'], ds['used']), (1.084, 'community', False, ['arena.agent_sessions']))
        self.assertEqual((mw['value'], mw['kind'], mw['fallback'], mw['used']), (21.771, 'community', False, ['arena.agent_sessions']))
        # A model listed on arena.ai Agent / Overall without Cost/Task says so instead of 'not listed'.
        nem = shipped['models']['nemotron-3.5-super-vl-free']['usd_per_hour']
        self.assertIn('without Cost/Task', nem['merge'])
        self.assertNotIn('not listed', nem['merge'])
        beam = shipped['models']['beam-free']['usd_per_hour']
        self.assertIn('without Cost/Task', beam['reason'])
        # Distrusting arena.agent_sessions records 'arena.agent_sessions distrusted', not 'not listed'.
        meas = {'models': {'m': {'usd_per_hour': [
            {'source': 'arena.agent_sessions', 'kind': 'community', 'effort': 'max', 'value': 6.125},
            {'source': 'derived.community_mix', 'kind': 'derived', 'effort': 'high', 'value': 13.688},
        ]}}}
        scorer = self.fa.Scorer(meas, {'models': {'m': {}}}, distrust=('arena.agent_sessions',))
        usd = self.fa.usd_community_mix(scorer, {'m': {'fields': meas['models']['m']}}, meas, None)
        self.assertEqual((usd['m']['value'], usd['m']['fallback']), (13.688, True))
        self.assertIn('arena.agent_sessions distrusted', usd['m']['merge'])
        self.assertNotIn('not listed', usd['m']['merge'])

    def test_usd_community_mix_with_source_priority_falls_through_without_cross_scale_median(self):
        """Round 9 F4: `usd_community_mix` with `--source-priority` naming a missing source falls through the priority
        order to `arena.agent_sessions` (then `derived.community_mix`), never taking a median of arena + AA + derived."""
        meas = {'models': {'gemini-4-argon-high': {'usd_per_hour': [
            {'source': 'aa.cost_time', 'kind': 'benchmark', 'effort': 'high', 'value': 7.50},
            {'source': 'openrouter.cost_time', 'kind': 'vendor', 'effort': 'high', 'value': 23.83},
            {'source': 'arena.agent_sessions', 'kind': 'community', 'effort': 'high', 'value': 6.658},
            {'source': 'derived.community_mix', 'kind': 'derived', 'effort': 'high', 'value': 31.901},
        ]}}}
        scorer = self.fa.Scorer(meas, {'models': {'gemini-4-argon-high': {}}})
        rows = {'gemini-4-argon-high': {'fields': meas['models']['gemini-4-argon-high']}}
        usd = self.fa.usd_community_mix(scorer, rows, meas, priority=['user'])
        self.assertEqual(usd['gemini-4-argon-high']['value'], 6.658)
        self.assertEqual(usd['gemini-4-argon-high']['used'], ['arena.agent_sessions'])
        self.assertEqual(usd['gemini-4-argon-high']['kind'], 'community')
        self.assertFalse(usd['gemini-4-argon-high']['fallback'])
        # Naming an available source in priority picks that source alone without cross-scale median.
        usd_aa = self.fa.usd_community_mix(scorer, rows, meas, priority=['aa.cost_time'])
        self.assertEqual(usd_aa['gemini-4-argon-high']['value'], 7.50)
        self.assertEqual(usd_aa['gemini-4-argon-high']['used'], ['aa.cost_time'])
        self.assertEqual(usd_aa['gemini-4-argon-high']['kind'], 'benchmark')
        self.assertTrue(usd_aa['gemini-4-argon-high']['fallback'])



class ArenaCaptureTests(unittest.TestCase):
    """arena.ai boards transcribed from the user's browser (round 7): parsed from the page text, dated, and mapped."""
    @classmethod
    def setUpClass(cls):
        import arena_capture
        import measurements
        cls.ac, cls.ms = arena_capture, measurements

    @staticmethod
    def agent_page(nets):
        rows = []
        for i, net in enumerate(nets, 1):
            cells = '\n\t\n'.join(f'{v}%\n±0.50%' for v in (net, 3.0, 4.0, 5.0, 6.0, 0.9))
            rows.append(f'{i}\n{i}\n{i + 1}\n\t\nModel {chr(64 + i)} (Max)\nLab · Proprietary\n\t\n{cells}\n'
                        f'\t1,234\t$2.00\t30 min\t50K\t$1 / $5\n')
        return ('Agent Arena | Capabilities\nCode\nNov 8, 2026\n9,999 sessions\n' + f'{len(nets)} models\n'
                + 'Rank\n\t\nModel\n\t\nNet Improvement\n\t\nCost/Task (P50)\n\t\nPrice $/M\n\n\n' + '\n'.join(rows))

    def test_an_agent_page_parses_and_its_lost_minus_signs_are_rebuilt_from_the_rank_order(self):
        page = self.ac.parse_agent(self.agent_page([9.5, 4.0, 1.2, 0.3, 2.0, 7.5]))   # the capture drops the minus
        self.assertEqual((page['as_of'], page['sessions'], len(page['rows'])), ('2026-11-08', 9999, 6))
        self.assertEqual([r['net_improvement'][0] for r in page['rows']], [9.5, 4.0, 1.2, 0.3, -2.0, -7.5])
        self.assertTrue(page['rows'][3]['sign_ambiguous'])
        self.assertEqual(self.ac.usd_per_hour(page['rows'])['Model A (Max)'], 4.0)       # $2 per 30-minute task
        with self.assertRaises(ValueError):           # magnitudes that fall, rise and fall again cannot be signed
            self.ac.parse_agent(self.agent_page([9.5, 1.0, 3.0, 2.0]))

    def test_the_run_together_alignment_page_splits_rank_name_lab_and_rates(self):
        text = ('Agent Arena | Alignment IndexOctober 16, 202679,468 sessions2 modelsRankModelAlignment Index'
                'Unauthorized actionFalse attributionDeceptive completion'
                '114GPT-6.1 SolOpenAI87.9±1.50.89%0.53–1.48%1.98%1.47–2.68%2.34%1.77–3.10%'
                '214Claude Sonnet 5.5 HighAnthropic79.5±1.11.83%1.40–2.38%5.84%5.04–6.76%9.53%8.54–10.61%Footer')
        page = self.ac.parse_alignment(text)
        self.assertEqual((page['as_of'], page['sessions']), ('2026-10-16', 79468))
        self.assertEqual([(r['name'], r['lab']) for r in page['rows']], [('GPT-6.1 Sol', 'OpenAI'), ('Claude Sonnet 5.5 High', 'Anthropic')])
        self.assertEqual(page['rows'][1]['deceptive_completion'], [9.53, 8.54, 10.61])
        self.assertEqual(page['rows'][0]['alignment_index'], [87.9, 1.5])

    def test_the_users_mapping_of_the_agent_boards_onto_the_axes(self):
        cap = self.ms.CAPABILITY_SOURCES
        self.assertEqual(cap['arena.agent_code'][:2], ('global_rigor', 'agentic'))
        self.assertEqual({cap[s][:2] for s in ('arena.agent_work_tools', 'arena.agent_work_gui')},
                         {('vision_tools', 'tools'), ('vision_tools', 'gui')})
        self.assertEqual(cap['arena.agent_alignment'][:2], ('module_precision', 'alignment'))
        self.assertEqual(cap['arena.agent_alignment_false_attribution'][:2], ('global_rigor', 'anti_hallucination'))
        self.assertEqual(cap['arena.agent_alignment_deceptive_completion'][:2], ('global_rigor', 'long_horizon'))
        self.assertEqual(cap['lmarena.webdev'][:2], ('vision_tools', 'gui_design'))
        self.assertEqual(cap['lmarena.text_overall'][:2], ('divergence', 'chat'))
        self.assertNotIn('lmarena.agent', cap)         # Agent / Overall is a cross-check only

    def test_a_tab_that_was_never_captured_is_a_surfaced_missing_source(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            builder = self.ms.Builder(Path(tmp), {'models': {}})
            builder.boards()
        for src in self.ms.ARENA_CAPTURES:
            self.assertIn('not captured', builder.missing[src]['reason'], src)


if __name__ == '__main__':
    unittest.main()
