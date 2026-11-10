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
import unittest

import numpy as np

import allocator
import route_judge
import route_simulation as sim
from router_test_support import isolated_router_state
from test_fleet_parity import shipped_template, snapshot

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'calibration'))


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup, discovery=False, policy=False)


NOW = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)
PROFILE = {'reasoning_depth': 2, 'code_complexity': 2, 
           'needs_visual': False, 'needs_gui': False, 'local_only': True, 'realtime_loop': False,
           'wants_fast': False, 'read_only_review': False, 'interaction_style': 'engineering', 'multi_task': False,
           'task_count': '1', 'expected_hours': 1.0}
ANCHOR = {'model': 'claude-opus-5-5', 'value': 0.8}
EFFORT_NAMES = ('low', 'medium', 'high', 'xhigh', 'max')


def demand(div, rigor, code, vision):
    return {'divergence': div, 'global_rigor': rigor, 'module_precision': code, 'vision_tools': vision}


def row(scores=0.8, usd=5.0, missing=(), image_input='yes', dur=1.0, fast=None, price=True):
    scores = scores if isinstance(scores, dict) else dict.fromkeys(allocator.FIVE_AXES, scores)
    img_val = True if image_input == 'yes' else False if image_input == 'no' else None
    out = {'scores': scores, 'missing_axes': list(missing),
           'usd_per_hour': {'value': usd},
           'duration_multiplier': {'status': 'ok', 'by_effort': dict.fromkeys(EFFORT_NAMES, dur),
                                   'basis_by_effort': {e: {'method': 'aa', 'source': 'aa.cost_time'} for e in EFFORT_NAMES},
                                   'efforts_without_time': []},
           'intelligence_index': 50.0,
           'image_input': {'value': img_val, 'status': image_input,
                           'source': 'aa.modality' if image_input in ('yes', 'no') else None}}
    if price:       # the data's `price` row: public USD per million tokens with its source (aa.price, openrouter.price)
        out['price'] = {'value': {'input': 0.1, 'output': 0.4, 'cache_read': 0.01}, 'source': 'aa.price', 'kind': 'vendor',
                        'as_of': '2026-11-09', 'url': 'https://artificialanalysis.ai/leaderboards/models'}
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
        targets = allocator.five_axis_targets(demand(3, 1, 0, 2), ANCHOR['value'])
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

    def test_missing_demanded_axis_leaves_automatic_routing_but_a_named_model_runs(self):
        target = 'claude-sonnet-5-5'
        doc = document(self.accounts['models'], **{target: row(demand(0.0, 0.95, 0.95, 0.9), missing=['divergence'])})
        blind = self.plan(self.accounts, doc, profile={'axis_demand': demand(2, 1, 2, 0)})
        sighted = self.plan(self.accounts, doc, profile={'axis_demand': demand(0, 1, 2, 0)})
        named = self.plan(self.accounts, doc, profile={'axis_demand': demand(2, 1, 2, 0)}, constraints={'model': target})
        self.assertTrue(all(any('no trusted data for the demanded category divergence' in e for e in c['excluded'])
                            for c in self.rows_for(blind, target)))
        self.assertFalse(any('no trusted data' in e for c in self.rows_for(sighted, target) for e in c['excluded']))
        chosen = next(c for c in named['ranking'] if c['id'] == named['choice'])
        self.assertEqual(allocator.canonical(chosen['bundle']['model'], self.accounts), target)
        self.assertIn('no trusted data for the demanded category divergence', ' '.join(chosen['notes']))

    def test_missing_usd_is_removed_not_defaulted(self):
        acc = copy.deepcopy(self.accounts)
        out = allocator.five_axis_overlay(acc, document(acc['models'], **{'claude-sonnet-5-5': row(0.8, None)}))
        info = allocator.model_spec(out, 'claude-sonnet-5-5')
        self.assertEqual(info['five_axis']['usd_status'], 'missing')
        self.assertNotIn('usd_per_hour', info)

    def test_unsourced_usd_is_never_priced_at_the_default(self):
        acc = copy.deepcopy(self.accounts)
        target = 'claude-sonnet-5-5'
        doc = document(acc['models'], **{target: row(0.8, None)})
        out = allocator.five_axis_overlay(acc, doc)
        info = allocator.model_spec(out, target)
        self.assertEqual(allocator.model_usd_per_hour(target, info), 0.0)
        self.assertEqual(allocator.unit_rate({'windows': {'w': {}}}, 'w', target, info), 0.0)
        auto = self.plan(acc, doc, profile={'axis_demand': demand(0, 1, 2, 0)})
        self.assertTrue(all(any('usd_per_hour has no cited source on a metered account' in e for e in c['excluded'])
                            for c in self.rows_for(auto, target)))
        named = self.plan(acc, doc, profile={'axis_demand': demand(0, 1, 2, 0)}, constraints={'model': target})
        chosen = next(c for c in named['ranking'] if c['id'] == named['choice'])
        self.assertEqual(allocator.canonical(chosen['bundle']['model'], acc), target)
        self.assertIn('its cost is not priced', ' '.join(chosen['notes']))

    def test_capacity_is_the_community_figure_of_the_account_plan(self):
        acc = copy.deepcopy(self.accounts)
        acc['accounts']['pi']['family'] = 'pi'      # discovery is off here; on a host it names the family
        out = allocator.five_axis_overlay(acc, document(acc['models']))
        spec = out['accounts']['claude']
        self.assertEqual(spec['capacity_usd'], {'seven_day': 1700.0, 'five_hour': 270.0})
        # Max 20x weekly_fable has no community value in CAPACITY here: unknown, with neither the template figure nor the burn prior.
        self.assertTrue(spec['capacity_unknown'])
        self.assertEqual(spec['capacity_source']['weekly_fable']['status'], 'missing')
        self.assertIsNone(allocator.capacity_usd(spec, 'weekly_fable'))
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

    def test_tie_break_order_net_score_then_wait_then_faster_then_fewer_usd_then_level(self):
        """Quota economy v3 (advisor report v3 §1.6): order is N, then the wait before the start, then completion time
        (throughput / fast mode), then fewer USD, then axis level, then the sibling harness place. How soon quota lapses
        and what a substitute could take are inside N already, so no key reads them."""
        def cand(name, score=0.5, hours=1.0, usd=10.0, level=0.8, wait=0.0, place=0):
            return {'id': name, 'score': score, 'available_in_hours': wait or None,
                    'expected_hours': hours, 'duration_hours': hours, 'expected_usd': usd,
                    'components': {'five_axis_level': level, 'harness_place': place}}
        pool = [
            cand('slower_expensive', hours=0.8, usd=20.0),
            cand('faster_dear', hours=0.2, usd=20.0),
            cand('faster_cheap', hours=0.2, usd=5.0),
            cand('higher_n', score=0.6, hours=2.0, usd=50.0),
            cand('waits_but_quick', hours=0.1, usd=1.0, wait=3.0),
            cand('faster_cheap_higher_level', hours=0.2, usd=5.0, level=0.9),
            cand('faster_cheap_later_harness', hours=0.2, usd=5.0, level=0.9, place=1),
        ]
        self.assertEqual([c['id'] for c in sorted(pool, key=allocator.five_axis_order)],
                         ['higher_n', 'faster_cheap_higher_level', 'faster_cheap_later_harness', 'faster_cheap',
                          'faster_dear', 'slower_expensive', 'waits_but_quick'])
        self.assertEqual(allocator.five_axis_deciding_key(pool[2], pool[1]), 'usd')
        self.assertEqual(allocator.five_axis_deciding_key(pool[3], pool[2]), 'net_score')
        self.assertEqual(allocator.five_axis_deciding_key(pool[1], pool[0]), 'completion_hours')
        self.assertEqual(allocator.five_axis_deciding_key(pool[5], pool[2]), 'axis_level')
        self.assertEqual(allocator.five_axis_deciding_key(pool[5], pool[6]), 'harness_place')
        self.assertEqual(allocator.FIVE_AXIS_DEFAULTS['weights']['fit'], 1.0)
        for gone in ('overqualification_weight', 'strong_level', 'pace', 'pace_smoothing'):
            self.assertNotIn(gone, allocator.FIVE_AXIS_DEFAULTS, gone)
        for gone in ('five_axis_overqualification', 'five_axis_gate', 'five_axis_gate_level',
                     'five_axis_pacing', 'five_axis_wait_cost', 'five_axis_beta', 'five_axis_beta_fallback',
                     'five_axis_over_line_usd', 'five_axis_non_expiring_under_line_usd'):
            self.assertFalse(hasattr(allocator, gone), gone)

    def test_window_clock_across_reset_unstarted_and_infinite_reset(self):
        """Quota economy v3: each window is read on its own clock. `five_axis_window_recovery` gives the elapsed fraction e of
        its period (the recovery line is C e), a rolling window that starts on first use is at e = 0, and a starter
        allowance (T = inf) never advances."""
        rec = allocator.five_axis_window_recovery(
            {'known': True, 'full': 100.0, 'hours': 168.0, 'remaining': 70.0, 'reset_hours': 84.0}, 1700.0)
        self.assertAlmostEqual(rec['capacity_usd'], 1700.0)
        self.assertAlmostEqual(rec['used_usd'], 510.0)
        self.assertAlmostEqual(rec['elapsed_frac'], 0.5)
        self.assertAlmostEqual(rec['line_now_usd'], 850.0)
        cross = allocator.five_axis_window_recovery(
            {'known': True, 'full': 100.0, 'hours': 168.0, 'remaining': 40.0, 'reset_hours': 2.0}, 1704.5)
        self.assertAlmostEqual(cross['elapsed_frac'], 1 - 2 / 168)
        self.assertAlmostEqual(cross['reset_hours'], 2.0)
        unstarted = allocator.five_axis_window_recovery(
            {'known': True, 'full': 100.0, 'hours': 168.0, 'remaining': 100.0, 'reset_hours': 168.0, 'unstarted': True}, 2580.0)
        self.assertTrue(unstarted['unstarted'])
        self.assertEqual((unstarted['elapsed_frac'], unstarted['used_usd']), (0.0, 0.0))
        starter = allocator.five_axis_window_recovery(
            {'known': True, 'full': 100.0, 'hours': None, 'remaining': 80.0, 'reset_hours': float('inf')}, 50.0)
        self.assertEqual((starter['elapsed_frac'], starter['line_now_usd'], starter['reset_hours']), (0.0, 0.0, float('inf')))
        self.assertAlmostEqual(starter['used_usd'], 10.0)
        self.assertIsNone(allocator.five_axis_window_recovery({'known': False}, 100.0))
        # The starter allowance's clock never runs: its need probability depends on use only.
        self.assertGreater(allocator.five_axis_need_probability({**starter, 'lump_usd': 5.0}, starter['used_usd']), 0.0)

    def test_nested_windows_and_5h_plus_weekly_price_only_the_dearest_long_window_once(self):
        """Quota economy v3: a candidate drawing the 5-hour window, the weekly window and the nested weekly_fable window
        pays its dearest long window once (never the sum); the 5-hour window keeps only feasibility."""
        acc = copy.deepcopy(self.accounts)
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
        # seven_day behind its line (80% left at mid-week), weekly_fable well ahead of it (30% left at mid-week).
        for w in usage['accounts']['claude']['windows']:
            if w['id'] == 'seven_day':
                w['remaining_pct_before_reset'] = 25.0
                w['seconds_until_reset'] = 84 * 3600
            elif w['id'] == 'five_hour':
                w['remaining_pct_before_reset'] = 90.0
                w['seconds_until_reset'] = int(2.5 * 3600)
            elif w['id'] == 'weekly_fable':
                w['remaining_pct_before_reset'] = 12.0
                w['seconds_until_reset'] = 84 * 3600
        # Claude alone: with no other account to take displaced work the windows price (round 15: the burst size is small, so a
        # window that another account could cover is nearly free; both weekly windows are tight here so that none of them can)
        original = allocator.load_five_axis
        allocator.load_five_axis = lambda path=None: doc
        try:
            d = allocator.plan({'objective': 't', 'hours': 1.0, 'profile': {**PROFILE, 'axis_demand': demand(2, 2, 2, 0)}},
                               usage, NOW, accounts=acc, overrides=[], records=[], host_policy_config={'allowed_accounts': ['claude']})
        finally:
            allocator.load_five_axis = original
        fable_high = next(c for c in d['ranking'] if c['id'] == 'fable51_high')
        opus_high = next(c for c in d['ranking'] if c['id'] == 'opus55_high')
        fable_windows = {w['window']: w for w in fable_high['economy']['windows']}
        self.assertEqual(set(fable_windows), {'seven_day', 'weekly_fable'})        # no 5-hour window prices
        self.assertEqual({w['window'] for w in opus_high['economy']['windows']}, {'seven_day'})
        dearest = max(fable_windows, key=lambda w: fable_windows[w]['cost'])
        self.assertEqual(fable_high['capacity']['window'], dearest)                # the dearer of its two long windows
        self.assertAlmostEqual(fable_high['components']['opportunity_cost'], fable_windows[dearest]['cost'], places=4)
        self.assertLess(fable_high['components']['opportunity_cost'],
                        sum(w['cost'] for w in fable_windows.values()))             # never the sum
        self.assertGreater(fable_high['components']['opportunity_cost'], opus_high['components']['opportunity_cost'])

    def test_a_pool_close_to_its_reset_is_cheaper_than_one_on_its_line_and_has_no_substitute_discount(self):
        """Quota economy v3 on the round-11 desync example (Pool A `claude`: 2h to reset with 40% left; Pool B `claude-2`:
        144h to reset exactly on the old recovery line): every window is priced on its own state and clock, with no
        pool absorbing another's spend. The pool about to reset pays far less than the one with a week still to run; a
        longer task that crosses Pool A's reset pays its second part on a fresh period."""
        acc = copy.deepcopy(self.accounts)
        acc.pop('_discovery', None)
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
        usage['accounts']['claude']['windows'] = [
            {'id': 'seven_day', 'remaining_pct_before_reset': 40.0, 'seconds_until_reset': 2 * 3600},
            {'id': 'five_hour', 'remaining_pct_before_reset': 95.0, 'seconds_until_reset': 5 * 3600},
        ]
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

        def pools(d):
            plain = lambda i: next(c for c in d['ranking'] if c['id'] == i)
            return plain('opus55_high'), plain('claude-2__opus55_high')

        for hours in (1.0, 1.9):        # the task ends before Pool A's reset: its last dollars lapse, so they cost less
            d = run_case(hours)
            pool_a, pool_b = pools(d)
            self.assertAlmostEqual(pool_a['components']['fit'], pool_b['components']['fit'])
            w_a0, w_b0 = pool_a['economy']['windows'][0], pool_b['economy']['windows'][0]
            self.assertLess(w_a0['need_start'], 1e-6)                    # 2h to the reset with 40% left: nothing of it is needed
            self.assertGreater(w_b0['need_start'], 0.4)                  # on its line, a week to run: a coin flip
            self.assertLessEqual(pool_a['components']['opportunity_cost'], pool_b['components']['opportunity_cost'])
            # both cost less than N's resolution (round 15: Pool A can take Pool B's work, Pool B's price is nil); the
            # earliest-expiring quota goes first, then the faster completion
            self.assertEqual((d['ranking'][0].get('base_id') or d['ranking'][0]['id']), 'opus55_high')
            w_a = pool_a['economy']['windows'][0]
            self.assertEqual(w_a['window'], 'seven_day')
            self.assertAlmostEqual(w_a['elapsed_hours'], 166.0)
            self.assertEqual(w_a['spend_after_reset'], 0.0)
        # 2h before the reset with a 3h task: one hour of it falls into Pool A's next period, where its first dollar is needed
        # again with the new period's own odds, so the plain Opus on Pool A pays for that hour and trails Pool B. Its fast
        # variant finishes the 3h task in 2h, before the reset: nothing crosses and it is free again (fast mode is its own
        # candidate at its real spend, so this is a result of the price and not a rule).
        d3 = run_case(3.0)
        a3, b3 = pools(d3)
        self.assertGreater(a3['economy']['windows'][0]['spend_after_reset'], 0.0)
        self.assertGreater(a3['components']['opportunity_cost'], b3['components']['opportunity_cost'])
        self.assertLess(a3['score'], b3['score'])
        fast3 = next(c for c in d3['ranking'] if c['id'] == 'opus55_high+fast')
        self.assertEqual(fast3['economy']['windows'][0]['spend_after_reset'], 0.0)
        self.assertEqual(round(fast3['score'], 4), round(b3['score'], 4))
        for sec_left in (7200, 600):    # near the reset both variants cost nothing: they tie on N and the faster one goes first
            usage['accounts']['claude']['windows'][0]['seconds_until_reset'] = sec_left
            near = run_case(0.1)
            fast = next(c for c in near['ranking'] if c['id'] == 'opus55_high+fast')
            plain = next(c for c in near['ranking'] if c['id'] == 'opus55_high' and not c['bundle'].get('fast'))
            self.assertEqual(round(plain['score'], 4), round(fast['score'], 4))
            self.assertLess(fast['expected_hours'], plain['expected_hours'])
            self.assertTrue(near['selected_bundle'].get('fast'))
        usage['accounts']['claude']['windows'][0]['seconds_until_reset'] = 2 * 3600
        # 6h: four of the six hours fall in the next period of Pool A, where the first dollar is again a coin flip. Each
        # pool is still priced on its own clock and Pool B (14% into its week, on its line) is the cheaper one for it.
        d6 = run_case(6.0)
        cross, other = pools(d6)
        w_cross = cross['economy']['windows'][0]
        self.assertAlmostEqual(w_cross['spend_before_reset'], cross['expected_usd'] * 2.0 / 6.0, places=1)
        self.assertAlmostEqual(w_cross['spend_after_reset'], cross['expected_usd'] * 4.0 / 6.0, places=1)
        self.assertGreater(cross['components']['opportunity_cost'], other['components']['opportunity_cost'])
        self.assertEqual((d6['ranking'][0].get('base_id') or d6['ranking'][0]['id']), 'claude-2__opus55_high')

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
        overlaid = allocator.five_axis_overlay(overlaid, doc)
        self.assertGreater(overlaid['accounts']['opencode'].get('local_spend_usd', 0.0), 0.0)
        ws = account_state.warnings(overlaid.get('_discovery'), overlaid['accounts'])
        self.assertTrue(any('opencode:' in w and 'quota unknown, spend unpriced' in w for w in ws), ws)
        self.assertTrue(overlaid['accounts']['opencode'].get('five_axis_free_pool'))

    # ---- round 12 -------------------------------------------------------------------------------------------------

    def test_a_new_model_without_a_sourced_price_never_wins_through_a_default(self):
        """A model without a cited `usd_per_hour` is unpriced and leaves automatic routing on a metered account; no
        default price makes it the cheapest option."""
        acc = copy.deepcopy(self.accounts)
        acc['models']['newcomer-9'] = copy.deepcopy(acc['models']['claude-opus-5-5'])
        acc['models']['newcomer-9'].pop('usd_per_hour', None)
        acc['accounts']['claude']['bundles']['newcomer_high'] = {'model': 'newcomer-9', 'effort': 'high'}
        self.assertTrue(allocator.usd_unpriced('newcomer-9', acc['models']['newcomer-9']))
        self.assertEqual(allocator.model_usd_per_hour('newcomer-9', acc['models']['newcomer-9']), 0.0)
        for state in ('ample', 'claude scarce'):
            decision = allocator.plan({'objective': 't', 'profile': {**PROFILE, 'reasoning_depth': 3}},
                                      self.usage(acc, state), NOW, accounts=copy.deepcopy(acc), overrides=[], records=[],
                                      host_policy_config=self.policy)
            rows = [c for c in decision['ranking'] if c['bundle']['model'] == 'newcomer-9']
            self.assertTrue(rows)
            self.assertTrue(all(any('usd_per_hour has no cited source' in e for e in c['excluded']) for c in rows))
            self.assertNotEqual(decision['choice'], rows[0]['id'])

    def test_every_shipped_model_carries_a_sourced_price_or_says_it_has_none(self):
        shipped = allocator.load_five_axis()
        for name, row in shipped['models'].items():
            usd = row['usd_per_hour']
            if usd.get('status') == 'ok':
                self.assertTrue(usd.get('sources'), name)
            else:
                self.assertIsNone(usd.get('value'), name)
                self.assertIn('no_cited_source', {e['reason'] for e in row['data_errors']}, name)

    def test_fable_5_2_is_converted_from_fable_5_1(self):
        """User 2026-11-10 ("后续同系列新出，缺少实测数据的情况下，先按照同系列的全分数进行折算，不要留下任何一个默认值或者
        fallback"): every type score claude-fable-5-2 does not measure is claude-fable-5-1's (its same-series sibling) moved along
        the type score's fit on tier by the two tiers' difference, and its usd_per_hour is 5.1's times the API list price
        ratio over the community token mix. No pessimistic 0, no option-B fallback. Fast-mode pricing ($16/$80 = 2x, 2.5x
        speed) and the unknown image input (mode B for vision work) stay its own."""
        shipped = allocator.load_five_axis()
        fable52, fable51 = shipped['models']['claude-fable-5-2'], shipped['models']['claude-fable-5-1']
        usd = fable52['usd_per_hour']
        self.assertEqual((usd['kind'], usd['fallback'], usd['sources']['base']['model']), ('converted', False, 'claude-fable-5-1'))
        self.assertAlmostEqual(usd['value'], round(fable51['usd_per_hour']['value'] * usd['sources']['ratio'], 3), places=2)
        self.assertAlmostEqual(usd['sources']['ratio'], 0.8, places=4)      # $8/$40/$0.20 against $10/$50/$0.25
        self.assertEqual(fable52['fast_mode']['status'], 'ok')
        self.assertEqual((fable52['fast_mode']['price_multiplier'], fable52['fast_mode']['speed_multiplier']), (2.0, 2.5))
        self.assertEqual(fable52['image_input']['status'], 'unknown')
        fits = shipped['estimate_fits']
        t, tb = fable52['tier']['value'], fable51['tier']['value']
        estimated = 0
        for axis, ax in fable52['axes'].items():
            for comp, row in ax['components'].items():
                self.assertIn(row['status'], ('measured', 'estimated'), (axis, comp))
                if row['status'] != 'estimated':
                    continue
                estimated += 1
                est, base = row['estimate'], fable51['axes'][axis]['components'][comp]
                if f'{axis}.{comp}~tier' not in fits:      # no usable fit (rejected, reported): the sibling's value as it is
                    self.assertIn(f'{axis}.{comp}~tier', shipped['rejected_fits'])
                    self.assertEqual((est['method'], row['value']), ('sibling_shift', base['value']))
                    continue
                f = fits[f'{axis}.{comp}~tier']
                if base['status'] == 'measured':
                    self.assertEqual((est['method'], est['base_model']), ('sibling_shift', 'claude-fable-5-1'))
                    expected = base['value'] + f['b'] * (t - tb)
                else:       # the sibling has no measurement either: the fit at the tier
                    self.assertEqual(est['method'], 'tier_fit')
                    expected = f['a'] + f['b'] * min(max(t, f['x_min']), f['x_max'])
                self.assertAlmostEqual(row['value'], min(max(expected, f['y_min']), f['y_max']), places=2)
        self.assertTrue(estimated)
        # Its own measurements are all Artificial Analysis: labelled, not counted as independent evidence.
        self.assertTrue(fable52['evidence']['aa_only'])
        self.assertIn('aa_only', {e['reason'] for e in fable52['data_errors']})
        self.assertFalse(fable51['evidence']['aa_only'])

    def test_no_history_read_by_five_axis_economics(self):
        """Round 11 item 2: five-axis economics reads ONLY current-period window state (C, U, t0, T) + in-flight leases +
        public model data. Fails if usage samples, capacity.json, or completed ledger
        history is read or changes any five-axis score."""
        doc = document(self.accounts['models'])
        acc = copy.deepcopy(self.accounts)
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
            if True:
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
        self.assertEqual(allocator.five_axis_demand({'profile': prof}), demand(1, 2, 3, 0))
        for bad in (demand(1, 2, 4, 0), {**demand(1, 2, 3, 0), 'economy': 1}, {'divergence': 1}, demand(True, 2, 3, 0)):
            with self.assertRaises(ValueError):
                allocator.validate_profile({**PROFILE, 'axis_demand': bad})
        # No axis_demand is no profile: the classifier always answers it, nothing is derived from other fields.
        without = {k: v for k, v in PROFILE.items() if k != 'axis_demand'}
        with self.assertRaises(ValueError):
            allocator.validate_profile(without)
        # An all-zero answer asks nothing of the categories; it is taken as it is.
        self.assertEqual(allocator.five_axis_demand({'profile': {**PROFILE, 'axis_demand': demand(0, 0, 0, 0)}}), demand(0, 0, 0, 0))

    def test_the_scorer_always_asks_for_axis_demand(self):
        caps = {'routes': {'general_implementation': {}, 'other_or_mixed': {}}}
        schema = route_judge.scorer_schema(allocator, caps)
        self.assertIn('axis_demand', schema['properties'])
        self.assertIn('axis_demand', schema['required'])
        self.assertEqual(schema['properties']['axis_demand'], allocator.FIVE_AXIS_DEMAND_SCHEMA)

    def test_can_wait_hours_defers_only_for_higher_net_score_without_fixed_wait_cost_constant(self):
        """Rounds 8-11: no fixed 0.15 delay penalty and no 0.06 five_axis_wait_cost constant on the five-axis path.
        Without `can_wait_hours`, `plan()` starts the best ready-now candidate immediately; when `can_wait_hours` is given,
        routine tasks (already reaching the capped fit on a ready-now owner) still start now while frontier tasks prefer
        waiting for the higher-N model's short-window reset (round 13b: the same holds for a routine task whose waiting
        candidate has the higher N, since a faster candidate no longer displaces a higher one; in round 15 no routine
        candidate has the higher N any more)."""
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
        # Round 13b: no rule lets a faster candidate displace a higher N, so with `can_wait_hours` the routine task also waits
        # when (and only when) the candidate that waits has the higher net score than the best one that can start now.
        ready_now = max((c for c in routine['ranking'] if not c.get('excluded') and not c.get('available_in_hours')),
                        key=lambda c: c['score'])
        self.assertEqual(routine['ranking'][0]['score'], max(c['score'] for c in routine['ranking'] if not c.get('excluded')))
        # round 15: with the v3.1 burst size no candidate's quota costs anything here, so the routine task's waiting candidates
        # do not have a higher N than the best one that can start now, and it starts now
        self.assertEqual(round(routine['ranking'][0]['score'], 4), round(ready_now['score'], 4))
        self.assertEqual(routine['decision']['action'], 'start')
        self.assertEqual(ready_now['bundle']['account'], 'codex')
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

    def test_a_fit_needs_enough_points_a_positive_slope_and_out_of_sample_skill(self):
        fit = self.fa.Scorer.fit
        few = [(4.0 + 0.1 * i, 0.5 + 0.02 * i) for i in range(self.fa.MIN_FIT_POINTS - 1)]
        found, why = fit(few)
        self.assertIsNone(found)
        self.assertIn(f'a fit needs {self.fa.MIN_FIT_POINTS}', why)
        enough = [(4.0 + 0.1 * i, 0.5 + 0.02 * i) for i in range(self.fa.MIN_FIT_POINTS)]
        found, why = fit(enough)
        self.assertIsNone(why)
        self.assertEqual(found['n'], self.fa.MIN_FIT_POINTS)
        falling = [(4.0 + 0.1 * i, 0.9 - 0.02 * i) for i in range(self.fa.MIN_FIT_POINTS)]
        self.assertIn('not positive', fit(falling)[1])
        # a positive but noise-only slope has no out-of-sample skill: it is rejected, never used
        noise = [(float(i), 0.5 if i % 2 == 0 else 0.7) for i in range(10)]
        found, why = fit(noise)
        self.assertIsNone(found)
        self.assertIn('skill', why)

    def test_an_estimate_stays_inside_the_range_the_measured_points_reach(self):
        f, _ = self.fa.Scorer.fit([(float(x), 0.5 + 0.03 * x) for x in range(10)])     # measured 0.50 .. 0.77
        self.assertEqual((f['y_min'], f['y_max']), (0.5, 0.77))
        self.assertEqual(self.fa.Scorer.predict(f, 20), 0.77)       # a tier above the best sampled model is not extrapolated
        self.assertEqual(self.fa.Scorer.predict(f, -5), 0.5)
        self.assertEqual(self.fa.Scorer.predict(f, 5), 0.65)

    def test_shipped_data_has_no_local_flag_or_sample_sources(self):
        import json
        ref = Path(__file__).resolve().parents[1] / 'references'
        meas = json.loads((ref / 'public-measurements.json').read_text(encoding='utf-8'))
        used = {e['source'] for fields in meas['models'].values() for entries in fields.values() for e in entries}
        self.assertFalse(used & {'accounts.visual', 'packaged.accounts', 'community.burn'})
        scores = json.loads((ref / 'five-axis-scores.json').read_text(encoding='utf-8'))
        self.assertEqual(scores['anchor'], {'model': self.ms.ANCHOR, 'value': self.ms.ANCHOR_VALUE})
        self.assertNotIn('floors', scores)                  # no difficulty floors
        # the anchor sits at its value on every type score it is measured on
        for axis, row in scores['models'][self.ms.ANCHOR]['axes'].items():
            for name, comp in row['components'].items():
                if comp['status'] == 'measured':
                    self.assertEqual(comp['value'], self.ms.ANCHOR_VALUE, f'{axis}.{name}')

    def tiered(self, tier, **fields):
        """A model's measurements with its tier read off the arena.ai Overall board."""
        return {'tier': [{'source': 'tier.arena_overall', 'value': tier, 'as_of': '2026-11-09'}], **fields}

    def calibration_set(self, extra):
        """Ten models measured on two module_precision type scores (alignment = 0.3 + 0.1 x tier, terminal = 0.2 + 0.06 x
        tier, exactly on a line) with their tiers, and the models in `extra`."""
        models = {}
        for i in range(10):
            t = round(3.0 + 0.2 * i, 2)
            models[f'm{i}'] = self.tiered(t, module_precision=[
                {'source': 'arena.agent_alignment', 'component': 'alignment', 'value': round(0.3 + 0.1 * t, 4)},
                {'source': 'aa.terminalbench_4', 'component': 'terminal', 'value': round(0.2 + 0.06 * t, 4)}])
        models[self.ms.ANCHOR] = models.pop('m9')
        models.update(extra)
        return {'models': models}

    def score(self, meas):
        return self.fa.Scorer(meas, {'models': {m: {} for m in meas['models']}}).score()

    def test_the_tier_is_read_only_from_the_overall_boards(self):
        measured = self.tiered(4.0)
        by_other = {'tier': [{'source': 'tier.aa_index', 'value': 3.5}]}
        stray = {'tier': [{'source': 'lmarena.webdev', 'value': 4.4}, {'source': 'lmarena.tier_fit', 'value': 4.4}]}
        meas = self.calibration_set({'claude-fable-5-1': measured, 'claude-fable-5-2': {},
                                     'by_other': by_other, 'stray': stray, 'bare': {}})
        _, tier, _, _ = self.score(meas)
        self.assertEqual((tier['claude-fable-5-1']['value'], tier['claude-fable-5-1']['method']), (4.0, 'arena_overall'))
        self.assertEqual((tier['by_other']['value'], tier['by_other']['method']), (3.5, 'other_overall'))
        # no overall board: the same-series sibling's tier, and with no sibling no tier at all
        self.assertEqual((tier['claude-fable-5-2']['value'], tier['claude-fable-5-2']['method'], tier['claude-fable-5-2']['base_model']),
                         (4.0, 'sibling', 'claude-fable-5-1'))
        # a WebDev or Agent reading is not a tier source, even filed under the tier field
        self.assertEqual((tier['stray']['value'], tier['stray']['method']), (None, 'none'))
        self.assertEqual((tier['bare']['value'], tier['bare']['method']), (None, 'none'))

    def test_the_tier_is_never_a_score_input(self):
        same = {'module_precision': [{'source': 'arena.agent_alignment', 'component': 'alignment', 'value': 0.7},
                                     {'source': 'aa.terminalbench_4', 'component': 'terminal', 'value': 0.7}]}
        meas = self.calibration_set({'low': self.tiered(3.0, **same), 'high': self.tiered(4.9, **same)})
        rows, _, _, _ = self.score(meas)
        self.assertEqual(rows['low']['axes']['module_precision']['value'], rows['high']['axes']['module_precision']['value'])
        self.assertEqual(rows['low']['axes']['module_precision']['status'], 'measured')

    def test_every_measured_category_is_the_same_whatever_the_tier_rows_say(self):
        """Behavioural: the tier rows as read, reversed (a model's tier becomes 8 - tier) and absent give the same category
        values for every model that has its type scores measured. The tier only bridges a missing type score."""
        meas = self.calibration_set({'extra': self.tiered(4.4, module_precision=[
            {'source': 'arena.agent_alignment', 'component': 'alignment', 'value': 0.61},
            {'source': 'aa.terminalbench_4', 'component': 'terminal', 'value': 0.52}])})

        def values(m):
            rows, _, _, _ = self.score(m)
            return {name: {a: r['axes'][a].get('value') for a in r['axes']} for name, r in rows.items()}

        def retier(fn):
            out = copy.deepcopy(meas)
            for fields in out['models'].values():
                fn(fields)
            return out
        reversed_ = retier(lambda f: f['tier'][0].update(value=round(8.0 - f['tier'][0]['value'], 2)))
        absent = retier(lambda f: f.pop('tier'))
        original = values(meas)
        self.assertEqual(values(reversed_), original)
        self.assertEqual(values(absent), original)
        self.assertEqual(original['extra']['module_precision'], values(meas)['extra']['module_precision'])


    def test_a_missing_type_score_is_the_siblings_moved_along_the_fit_by_the_tier_difference(self):
        """User 2026-11-11: a model with no measured type score takes its same-series sibling's value, shifted along the
        fitted line of that type score on tier by the tier difference."""
        base = self.tiered(3.8, module_precision=[{'source': 'arena.agent_alignment', 'component': 'alignment', 'value': 0.68},
                                                  {'source': 'aa.terminalbench_4', 'component': 'terminal', 'value': 0.43}])
        new = self.tiered(4.2, module_precision=[{'source': 'aa.terminalbench_4', 'component': 'terminal', 'value': 0.45}])
        rows, _, fits, rejected = self.score(self.calibration_set({'claude-fable-5-1': base, 'claude-fable-5-2': new}))
        comp = rows['claude-fable-5-2']['axes']['module_precision']['components']['alignment']
        self.assertNotIn('module_precision.alignment~tier', rejected)
        self.assertEqual(comp['status'], 'estimated')
        est = comp['estimate']
        self.assertEqual((est['method'], est['base_model'], est['base_value'], est['tier_source']),
                         ('sibling_shift', 'claude-fable-5-1', 0.68, 'arena_overall'))
        self.assertEqual((est['fit'], est['tier_difference']), ('module_precision.alignment~tier', 0.4))
        self.assertAlmostEqual(est['shift'], fits['module_precision.alignment~tier']['b'] * 0.4, places=4)
        self.assertAlmostEqual(comp['value'], 0.72, places=3)                  # 0.68 + 0.1 x 0.4
        self.assertEqual(rows['claude-fable-5-2']['axes']['module_precision']['missing_components'], [])
        self.assertEqual(rows['claude-fable-5-2']['axes']['module_precision']['status'], 'estimated')

    def test_a_missing_type_score_without_a_sibling_is_the_fit_at_the_tier_kept_in_range(self):
        new = self.tiered(4.0, module_precision=[{'source': 'aa.terminalbench_4', 'component': 'terminal', 'value': 0.44}])
        far = self.tiered(5.5, module_precision=[{'source': 'aa.terminalbench_4', 'component': 'terminal', 'value': 0.5}])
        rows, _, fits, _ = self.score(self.calibration_set({'new': new, 'far': far}))
        comp = rows['new']['axes']['module_precision']['components']['alignment']
        self.assertEqual((comp['status'], comp['estimate']['method']), ('estimated', 'tier_fit'))
        self.assertAlmostEqual(comp['value'], 0.7, places=3)                   # 0.3 + 0.1 x 4.0
        self.assertNotIn('base_model', comp['estimate'])
        # a tier above every sampled model reads the fit at the top sampled tier, not beyond it
        top = rows['far']['axes']['module_precision']['components']['alignment']
        f = fits['module_precision.alignment~tier']
        self.assertEqual(top['value'], round(f['y_max'], 4))
        self.assertEqual(top['estimate']['tier_clamped_to'], f['x_max'])

    def test_a_type_score_with_no_tier_or_no_usable_fit_is_not_defaulted(self):
        """No tier (and no sibling): the type score stays missing and counts 0 in its category; a fit with no skill is
        reported, never used. Nothing stands in as a default."""
        terminal_only = [{'source': 'aa.terminalbench_4', 'component': 'terminal', 'value': 0.8}]
        meas = self.calibration_set({'no_tier': {'module_precision': terminal_only}})
        rows, tier, _, _ = self.score(meas)
        self.assertEqual(tier['no_tier']['method'], 'none')
        ax = rows['no_tier']['axes']['module_precision']
        self.assertEqual((ax['status'], ax['missing_components']), ('partial', ['alignment']))
        self.assertNotIn('alignment', ax['components'])
        self.assertAlmostEqual(ax['value'], 0.4, places=4)
        # alignment unrelated to the tier: its fit has no skill, so a tiered model still lacks it
        noisy = self.calibration_set({'new': self.tiered(4.0, module_precision=terminal_only)})
        for i, (name, fields) in enumerate(noisy['models'].items()):
            if name != 'new':
                fields['module_precision'][0]['value'] = 0.5 if i % 2 == 0 else 0.7
        rows, _, fits, rejected = self.score(noisy)
        self.assertIn('module_precision.alignment~tier', rejected)
        self.assertNotIn('module_precision.alignment~tier', fits)
        self.assertEqual(rows['new']['axes']['module_precision']['missing_components'], ['alignment'])

    def test_a_sibling_estimate_with_no_usable_fit_shifts_by_zero_and_says_so(self):
        """No skill means the tier does not predict the type score: the sibling's value stands, the shift is 0, and the
        record says why (round 058 fix round, B1)."""
        base = self.tiered(3.8, module_precision=[{'source': 'arena.agent_alignment', 'component': 'alignment', 'value': 0.68},
                                                  {'source': 'aa.terminalbench_4', 'component': 'terminal', 'value': 0.43}])
        new = self.tiered(4.2, module_precision=[{'source': 'aa.terminalbench_4', 'component': 'terminal', 'value': 0.45}])
        meas = self.calibration_set({'claude-fable-5-1': base, 'claude-fable-5-2': new})
        for i, name in enumerate(n for n in meas['models'] if n.startswith('m') or n == self.ms.ANCHOR):
            meas['models'][name]['module_precision'][0]['value'] = 0.5 if i % 2 == 0 else 0.7     # alignment unrelated to tier
        rows, _, fits, rejected = self.score(meas)
        self.assertIn('module_precision.alignment~tier', rejected)
        comp = rows['claude-fable-5-2']['axes']['module_precision']['components']['alignment']
        est = comp['estimate']
        self.assertEqual((comp['status'], comp['value'], est['method'], est['base_model']), ('estimated', 0.68, 'sibling_shift', 'claude-fable-5-1'))
        self.assertEqual((est['shift'], est['tier_difference']), (0.0, 0.4))
        self.assertTrue(est['shift_basis'].startswith('shift 0: fit rejected, out-of-sample skill'), est['shift_basis'])
        self.assertNotIn('fit', est)

    def vision_set(self):
        """`calibration_set` with a vision type score on the same line, for every calibration model."""
        meas = self.calibration_set({})
        for name, fields in meas['models'].items():
            t = fields['tier'][0]['value']
            fields['vision_tools'] = [{'source': 'arena.vision', 'component': 'vision', 'value': round(0.2 + 0.1 * t, 4)}]
        return meas

    def test_a_same_model_row_takes_its_declared_models_scores_for_the_categories_it_carries(self):
        """accounts.json `same_model` (Dot is the model behind OpenAI's dots): the declared model is the base before any
        other sibling at tier difference 0, the row has the tier of that model, and a category the row does not carry (its
        channel's own) stays missing even where a fit could estimate it."""
        meas = self.vision_set()
        meas['models']['astra'] = self.tiered(4.2, module_precision=[
            {'source': 'arena.agent_alignment', 'component': 'alignment', 'value': 0.72},
            {'source': 'aa.terminalbench_4', 'component': 'terminal', 'value': 0.5}],
            vision_tools=[{'source': 'arena.vision', 'component': 'vision', 'value': 0.62}])
        meas['models']['channel'] = {}
        acc = {'models': {m: {} for m in meas['models']},
               'same_model': {'channel': {'model': 'astra', 'carries': ['module_precision'], 'url': 'https://openai.com/index/introducing-dots/'}}}
        rows, tier, _, _ = self.fa.Scorer(meas, acc).score()
        self.assertEqual((tier['channel']['value'], tier['channel']['method'], tier['channel']['base_model']), (4.2, 'sibling', 'astra'))
        mp = rows['channel']['axes']['module_precision']
        for name, value in (('alignment', 0.72), ('terminal', 0.5)):
            est = mp['components'][name]['estimate']
            self.assertEqual((mp['components'][name]['value'], est['method'], est['base_model'], est['tier_difference'], est['shift']),
                             (value, 'sibling_shift', 'astra', 0.0, 0.0), name)
        vision = rows['channel']['axes']['vision_tools']
        self.assertEqual((vision['status'], vision.get('flag'), vision['components']), ('missing', 'missing', {}))
        # without the declaration the row has no tier and nothing to estimate from
        rows, tier, _, _ = self.fa.Scorer(meas, {'models': acc['models']}).score()
        self.assertEqual(tier['channel']['method'], 'none')
        self.assertEqual(rows['channel']['axes']['module_precision']['status'], 'missing')

    def test_a_later_measurement_replaces_the_estimate_on_refresh(self):
        """The estimate is not stored anywhere the next build reads: once a board lists the type score, the rebuild uses
        the measurement and the estimate record is gone."""
        new = self.tiered(4.0, module_precision=[{'source': 'aa.terminalbench_4', 'component': 'terminal', 'value': 0.44}])
        meas = self.calibration_set({'new': new})
        before, _, _, _ = self.score(meas)
        self.assertEqual(before['new']['axes']['module_precision']['components']['alignment']['status'], 'estimated')
        meas['models']['new']['module_precision'].append(
            {'source': 'arena.agent_alignment', 'component': 'alignment', 'value': 0.61})
        after, _, _, _ = self.score(meas)
        comp = after['new']['axes']['module_precision']['components']['alignment']
        self.assertEqual((comp['status'], comp['value']), ('measured', 0.61))
        self.assertNotIn('estimate', comp)
        self.assertEqual(after['new']['axes']['module_precision']['status'], 'measured')

    def test_partial_category_counts_the_type_score_it_lacks_as_zero(self):
        """A model backed by a single type score where the anchor has two, with no tier to estimate the other from, counts
        the missing one as 0 (pessimistic, user round 8)."""
        meas = {'models': {
            self.ms.ANCHOR: {'module_precision': [{'source': 'arena.agent_alignment', 'component': 'alignment', 'value': 0.8},
                                                  {'source': 'aa.terminalbench_4', 'component': 'terminal', 'value': 0.8}]},
            'single_board': {'module_precision': [{'source': 'arena.agent_alignment', 'component': 'alignment', 'value': 0.724}]},
        }}
        rows, _, _, _ = self.score(meas)
        self.assertEqual(rows[self.ms.ANCHOR]['axes']['module_precision']['value'], 0.8)
        ax = rows['single_board']['axes']['module_precision']
        self.assertEqual((ax['status'], ax['missing_components']), ('partial', ['terminal']))
        self.assertAlmostEqual(ax['value'], 0.724 / 2, places=4)


class SourceMergeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import five_axis
        cls.fa = five_axis

    def entries(self):
        return [{'source': 'arena.agent_sessions', 'kind': 'community', 'effort': 'high', 'value': 8.0, 'sessions': 12},
                {'source': 'arena.agent_sessions', 'kind': 'community', 'effort': 'low', 'value': 5.0, 'sessions': 12},
                {'source': 'derived.community_mix', 'kind': 'derived', 'effort': 'high', 'value': 31.0}]

    def test_usd_of_a_source_is_its_value_at_high_else_its_effortless_listing_else_the_median_of_its_efforts(self):
        arena = [e for e in self.entries() if e['source'] == 'arena.agent_sessions']
        merged = self.fa.pick_usd(arena)
        self.assertEqual((merged['value'], merged['used']), (8.0, ['arena.agent_sessions']))
        self.assertEqual(merged['sources']['arena.agent_sessions']['sessions'], 12)
        self.assertEqual(merged['sources']['arena.agent_sessions']['by_effort'], {'high': 8.0, 'low': 5.0})
        no_high = [{**arena[0], 'effort': 'max'}, arena[1]]
        self.assertEqual(self.fa.pick_usd(no_high)['value'], 6.5)          # median of the listed efforts
        self.assertEqual(self.fa.pick_usd([{'source': 's', 'kind': 'community', 'effort': None, 'value': 4.0}])['value'], 4.0)
        self.assertEqual(self.fa.pick_usd([])['status'], 'missing')

    def test_distrust_removes_a_source_from_every_field(self):
        meas = {'models': {'m': {'usd_per_hour': self.entries(), 'divergence': [
            {'source': 'eqbench.creative_writing_v3', 'component': 'creative', 'value': 0.2},
            {'source': 'lmarena.creative_writing', 'component': 'creative', 'value': 0.8}]}}}
        scorer = self.fa.Scorer(meas, {'models': {'m': {}}}, distrust=['arena.agent_sessions', 'eqbench.creative_writing_v3'])
        usd = self.fa.usd_community_mix(scorer, {'m': {'fields': meas['models']['m']}}, meas)
        self.assertEqual((usd['m']['value'], usd['m']['fallback']), (31.0, True))
        self.assertEqual(scorer.components(meas['models']['m'], 'divergence')['creative']['value'], 0.8)


class DurationTests(unittest.TestCase):
    """One basis for the time a task takes: Artificial Analysis Time/Task at the same effort, as a multiple of the anchor's at
    High; arena.ai Agent only through a cited fit; an effort with no listing only through a same-series sibling's ratio."""

    @classmethod
    def setUpClass(cls):
        import five_axis
        cls.fa = five_axis
        cls.anchor = five_axis.measurements.ANCHOR

    @staticmethod
    def tp(source, effort, minutes):
        return {'source': source, 'kind': 'benchmark', 'effort': effort, 'time_per_task_min': minutes,
                'listed_as': f'listed ({effort})', 'as_of': '2026-11-09', 'url': f'https://example.test/{source}'}

    def build(self, models, **acc_extra):
        rows = {m: {'fields': {'throughput': tps}} for m, tps in models.items()}
        acc = {'models': {m: {} for m in models}, 'aliases': {}, **acc_extra}
        scorer = self.fa.Scorer({'models': {m: r['fields'] for m, r in rows.items()}}, acc)
        tier = {m: {'value': None, 'method': 'none'} for m in rows}
        return self.fa.durations(scorer, rows, acc, {}, {}, tier)

    def test_each_listed_effort_is_its_own_aa_time_and_the_anchors_high_is_one(self):
        aa, arena = 'aa.cost_time', 'arena.agent_sessions'
        out, fit, why = self.build({self.anchor: [self.tp(aa, 'high', 10.0)],
                                    'm': [self.tp(aa, 'high', 15.0), self.tp(aa, 'max', 20.0), self.tp(arena, 'high', 3.0)]})
        self.assertEqual((out[self.anchor]['by_effort']['high'], out['m']['by_effort']), (1.0, {'high': 1.5, 'max': 2.0}))
        self.assertEqual(out['m']['minutes_by_effort'], {'high': 15.0, 'max': 20.0})
        self.assertEqual({b['method'] for b in out['m']['basis_by_effort'].values()}, {'aa'})   # the arena listing is not used
        self.assertEqual(out['m']['efforts_without_time'], ['low', 'medium', 'xhigh'])
        self.assertEqual(out['m']['basis_by_effort']['max']['url'], f'https://example.test/{aa}')

    def test_an_effortless_listing_is_the_single_configuration_keyed_high(self):
        out, _, _ = self.build({self.anchor: [self.tp('aa.cost_time', 'high', 10.0)], 'm': [self.tp('aa.cost_time', None, 5.0)],
                                'n': [self.tp('aa.cost_time', 'none', 5.0)]})
        self.assertEqual(out['m']['by_effort'], {'high': 0.5})
        self.assertEqual((out['n']['status'], out['n']['by_effort']), ('missing', {}))     # a non-reasoning variant is no effort

    def test_without_the_anchors_aa_time_at_high_the_build_stops(self):
        with self.assertRaises(ValueError):
            self.build({self.anchor: [self.tp('aa.cost_time', 'max', 10.0)], 'm': [self.tp('aa.cost_time', 'high', 5.0)]})

    def paired(self, slope, noise=()):
        """Ten models with an AA and an arena time at High (AA = slope x arena), plus the anchor, plus an arena-only model."""
        models = {self.anchor: [self.tp('aa.cost_time', 'high', 10.0), self.tp('arena.agent_sessions', 'high', 10.0 / slope)]}
        for i in range(1, 10):
            a = float(i)
            y = (20.0 if slope < 0 else 0.0) + slope * a + (noise[i % len(noise)] if noise else 0.0)
            models[f'p{i}'] = [self.tp('aa.cost_time', 'high', y), self.tp('arena.agent_sessions', 'high', a)]
        models['arena_only'] = [self.tp('arena.agent_sessions', 'high', 4.0), self.tp('arena.agent_sessions', 'max', 8.0)]
        return models

    def test_an_arena_only_model_is_moved_onto_the_aa_scale_by_a_cited_fit(self):
        out, fit, why = self.build(self.paired(2.0, noise=(0.1, -0.1, 0.2, -0.2)))
        self.assertIsNone(why)
        self.assertEqual((fit['fit'], fit['n']), ('aa_time~arena_time', 10))
        self.assertGreater(fit['skill'], 0.9)
        self.assertGreater(fit['r'], 0.99)
        row = out['arena_only']
        self.assertAlmostEqual(row['minutes_by_effort']['high'], 8.0, delta=0.5)         # 2 x 4 minutes on the AA scale
        self.assertAlmostEqual(row['minutes_by_effort']['max'], 16.0, delta=1.0)
        basis = row['basis_by_effort']['high']
        self.assertEqual((basis['method'], basis['arena_minutes'], basis['fit'], basis['n']), ('arena_fit', 4.0, 'aa_time~arena_time', 10))
        self.assertAlmostEqual(row['by_effort']['high'], row['minutes_by_effort']['high'] / 10.0, places=3)

    def test_an_arena_fit_without_skill_leaves_the_arena_only_model_without_a_time(self):
        out, fit, why = self.build(self.paired(-1.0))           # slower on arena, faster on AA: not a usable fit
        self.assertIsNone(fit)
        self.assertIn('not positive', why)
        self.assertEqual((out['arena_only']['status'], out['arena_only']['by_effort']), ('missing', {}))
        self.assertEqual(out['arena_only']['efforts_without_time'], ['low', 'medium', 'high', 'xhigh', 'max'])
        # models with their own AA time do not need the fit
        self.assertEqual(out['p3']['basis_by_effort']['high']['method'], 'aa')

    def test_an_effort_with_no_listing_takes_the_nearest_series_siblings_ratio_between_the_efforts(self):
        aa = 'aa.cost_time'
        models = {self.anchor: [self.tp(aa, 'high', 10.0)],
                  'gpt-6-luna': [self.tp(aa, 'high', 6.0)],
                  'gpt-5.6-luna': [self.tp(aa, 'high', 4.0), self.tp(aa, 'max', 12.0), self.tp(aa, 'low', 1.0)]}
        out, _, _ = self.build(models, superseded={'gpt-5.6-luna': {'by': 'gpt-6-luna'}})
        row = out['gpt-6-luna']
        self.assertEqual(row['minutes_by_effort']['max'], 18.0)                  # 6 x (12 / 4)
        self.assertEqual(row['minutes_by_effort']['low'], 1.5)                   # 6 x (1 / 4)
        basis = row['basis_by_effort']['max']
        self.assertEqual((basis['method'], basis['base_model'], basis['from_effort'], basis['ratio']),
                         ('series_effort', 'gpt-5.6-luna', 'high', 3.0))
        self.assertEqual(row['basis_by_effort']['high']['method'], 'aa')
        self.assertEqual(row['efforts_without_time'], ['medium', 'xhigh'])        # the sibling lists neither
        # alone, with no sibling to convert through, the other efforts have no time
        alone, _, _ = self.build({self.anchor: [self.tp(aa, 'high', 10.0)], 'gpt-6-luna': [self.tp(aa, 'high', 6.0)]})
        self.assertEqual(alone['gpt-6-luna']['by_effort'], {'high': 0.6})

    def test_a_same_model_row_takes_its_declared_models_times_only_when_it_carries_duration(self):
        aa = 'aa.cost_time'
        models = {self.anchor: [self.tp(aa, 'high', 10.0)], 'gpt-6-astra': [self.tp(aa, 'high', 20.0), self.tp(aa, 'max', 40.0)],
                  'dot': [], 'other': []}
        same = {'dot': {'model': 'gpt-6-astra', 'carries': ['duration'], 'url': 'https://openai.com/index/introducing-dots/',
                        'as_of': '2026-09-29'},
                'other': {'model': 'gpt-6-astra', 'carries': ['divergence']}}
        out, _, _ = self.build(models, same_model=same)
        self.assertEqual(out['dot']['by_effort'], {'high': 2.0, 'max': 4.0})
        basis = out['dot']['basis_by_effort']['high']
        self.assertEqual((basis['method'], basis['base_model'], basis['url']),
                         ('same_model', 'gpt-6-astra', 'https://openai.com/index/introducing-dots/'))
        self.assertEqual(out['other']['status'], 'missing')

    def test_a_model_with_no_time_takes_its_nearest_measured_series_siblings_times_with_provenance(self):
        aa = 'aa.cost_time'
        models = {self.anchor: [self.tp(aa, 'high', 10.0)],
                  'gpt-6-luna': [self.tp(aa, 'high', 6.0), self.tp(aa, 'max', 12.0)],
                  'gpt-5.6-luna': [], 'lone': []}
        out, _, _ = self.build(models, superseded={'gpt-5.6-luna': {'by': 'gpt-6-luna'}})
        row = out['gpt-5.6-luna']
        self.assertEqual(row['minutes_by_effort'], {'high': 6.0, 'max': 12.0})
        basis = row['basis_by_effort']['max']
        self.assertEqual((basis['method'], basis['base_model'], basis['source'], basis['base_method']),
                         ('series', 'gpt-6-luna', aa, 'aa'))
        self.assertEqual(basis['series'], ['gpt-5.6-luna', 'gpt-6-luna'])
        self.assertEqual((out['lone']['status'], out['lone']['by_effort']), ('missing', {}))          # no sibling: mode B

    def test_a_channel_row_that_does_not_carry_the_time_does_not_take_a_series_time(self):
        aa = 'aa.cost_time'
        models = {self.anchor: [self.tp(aa, 'high', 10.0)], 'gpt-6-astra': [self.tp(aa, 'high', 20.0)], 'chan': []}
        same = {'chan': {'model': 'gpt-6-astra', 'carries': ['divergence']}}
        self.assertEqual(self.build(models, same_model=same)[0]['chan']['status'], 'missing')

    def fleet(self, points, target, anchor_high=10.0):
        """Fleet models listing the (effort, minutes) `points` each, plus a 'target' listing `target`; returns the target's record."""
        aa = 'aa.cost_time'
        models = {self.anchor: [self.tp(aa, 'high', anchor_high)]}
        for i, pts in enumerate(points):
            models[f'f{i}'] = [self.tp(aa, e, t) for e, t in pts.items()]
        models['target'] = [self.tp(aa, e, t) for e, t in target.items()]
        return self.build(models)[0]['target']

    def test_an_effort_between_two_listed_ones_takes_the_fleets_median_log_position_and_honours_both_ends(self):
        import numpy as np
        highs = [3.0, 4.0, 5.0, 6.0]      # High of four models with Low 2 and Max 20: positions log(h/2)/log(10)
        row = self.fleet([{'low': 2.0, 'high': h, 'max': 20.0} for h in highs], {'low': 4.0, 'max': 40.0})
        ps = [float(np.log(h / 2.0) / np.log(10.0)) for h in highs]
        med = float(np.median(ps))
        basis = row['basis_by_effort']['high']
        self.assertEqual((basis['method'], basis['lower_effort'], basis['upper_effort'], basis['n']), ('fleet_effort_position', 'low', 'max', 4))
        self.assertAlmostEqual(basis['position'], med, places=4)
        self.assertEqual(basis['iqr'], [round(float(np.percentile(ps, 25)), 4), round(float(np.percentile(ps, 75)), 4)])
        self.assertEqual(basis['note'], f"fleet effort position high in low..max, n=4, IQR {np.percentile(ps, 25):.2f}-{np.percentile(ps, 75):.2f}")
        self.assertAlmostEqual(row['minutes_by_effort']['high'], 4.0 * 10.0 ** med, places=2)
        self.assertNotIn('bounded_by', basis)
        self.assertEqual((row['minutes_by_effort']['low'], row['minutes_by_effort']['max']), (4.0, 40.0))
        self.assertTrue(4.0 < row['minutes_by_effort']['high'] < 40.0)

    def test_the_between_estimate_is_not_flattened_to_the_upper_listed_effort(self):
        """A fleet whose High is 35x its Low must not drag a model whose own Max is only 10x its Low up to that Max: the
        estimate follows the model's own span (the earlier one-sided ratio put High at the Max)."""
        row = self.fleet([{'low': 2.0, 'high': 70.0, 'max': 140.0}] * 4, {'low': 4.0, 'max': 40.0})
        t = row['minutes_by_effort']['high']
        self.assertTrue(4.0 < t < 40.0, t)
        self.assertEqual(row['basis_by_effort']['high']['method'], 'fleet_effort_position')

    def test_a_position_outside_zero_to_one_is_held_and_marked(self):
        row = self.fleet([{'low': 2.0, 'high': 40.0, 'max': 8.0}] * 4, {'low': 4.0, 'max': 40.0})     # High slower than Max
        basis = row['basis_by_effort']['high']
        self.assertEqual(row['minutes_by_effort']['high'], 40.0)
        self.assertEqual(basis['bounded_by'], 'position held within [0, 1]')
        self.assertGreater(basis['position'], 1.0)

    def test_fewer_than_three_models_listing_all_three_efforts_leaves_the_effort_out_of_routing(self):
        row = self.fleet([{'low': 2.0, 'high': 4.0, 'max': 8.0}] * 2, {'low': 4.0, 'max': 40.0})
        self.assertIn('high', row['efforts_without_time'])
        self.assertNotIn('high', row['by_effort'])

    def test_an_effort_outside_the_listed_range_takes_the_fleets_median_ratio_from_the_nearest_listed_effort(self):
        pts = [{'low': 2.0, 'high': 2.0 * r} for r in (2.0, 3.0, 4.0, 5.0)]
        row = self.fleet(pts, {'low': 4.0})
        basis = row['basis_by_effort']['high']
        self.assertEqual((basis['method'], basis['from_effort'], basis['n'], basis['ratio']), ('fleet_effort_ratio', 'low', 4, 3.5))
        self.assertEqual(basis['iqr'], [2.75, 4.25])
        self.assertEqual(basis['note'], 'fleet effort ratio high/low, n=4, IQR 2.75-4.25')
        self.assertAlmostEqual(row['minutes_by_effort']['high'], 14.0)
        self.assertNotIn('bounded_by', basis)

    def test_an_effort_outside_the_range_is_never_faster_than_a_lower_listed_effort(self):
        row = self.fleet([{'low': 4.0, 'high': 2.0}] * 4, {'low': 4.0})           # the fleet's High is half its Low
        self.assertEqual(row['minutes_by_effort']['high'], 4.0)
        self.assertEqual(row['basis_by_effort']['high']['bounded_by'], 'a lower listed effort')

    def test_a_sibling_derived_effort_is_an_endpoint_and_estimated_efforts_never_chain(self):
        """A model that lists Max only gets its Low from the same-series sibling's ratio; Medium and High then sit between that
        Low and its Max by the fleet's position (not between Low and an estimated Medium)."""
        aa = 'aa.cost_time'
        models = {self.anchor: [self.tp(aa, 'high', 10.0)],
                  'gpt-5.6-luna': [self.tp(aa, 'low', 2.0), self.tp(aa, 'max', 20.0)],
                  'gpt-6-luna': [self.tp(aa, 'max', 40.0)]}
        for i in range(4):
            models[f'f{i}'] = [self.tp(aa, 'low', 2.0), self.tp(aa, 'medium', 4.0), self.tp(aa, 'high', 8.0), self.tp(aa, 'max', 20.0)]
        row = self.build(models, superseded={'gpt-5.6-luna': {'by': 'gpt-6-luna'}})[0]['gpt-6-luna']
        self.assertEqual((row['basis_by_effort']['low']['method'], row['minutes_by_effort']['low']), ('series_effort', 4.0))
        for e in ('medium', 'high'):
            basis = row['basis_by_effort'][e]
            self.assertEqual((basis['method'], basis['lower_effort'], basis['upper_effort']), ('fleet_effort_position', 'low', 'max'), e)
        self.assertAlmostEqual(row['minutes_by_effort']['medium'], 4.0 * 10.0 ** (np.log(2.0) / np.log(10.0)), places=2)
        self.assertEqual((row['minutes_by_effort']['low'], row['minutes_by_effort']['max']), (4.0, 40.0))
        values = [row['minutes_by_effort'][e] for e in ('low', 'medium', 'high', 'xhigh', 'max') if e in row['minutes_by_effort']]
        self.assertEqual(values, sorted(values))

    def test_the_shipped_durations_cite_a_basis_for_every_effort_and_leave_none_to_a_default(self):
        shipped = allocator.load_five_axis()
        self.assertTrue(shipped['duration_fit'] or shipped['duration_fit_rejected'])
        for name, r in shipped['models'].items():
            dur = r['duration_multiplier']
            self.assertEqual(set(dur['by_effort']), set(dur['basis_by_effort']), name)
            self.assertEqual(set(dur['efforts_without_time']) | set(dur['by_effort']), {'low', 'medium', 'high', 'xhigh', 'max'}, name)
            self.assertTrue(all(b['method'] in ('aa', 'arena_fit', 'series', 'series_effort', 'fleet_effort_ratio', 'fleet_effort_position', 'same_model')
                                for b in dur['basis_by_effort'].values()), name)
            self.assertNotIn('value', dur)


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
    """usd_per_hour has one basis: arena.agent_sessions, else the labelled option B (`derived.community_mix`), then the series
    conversion."""

    @classmethod
    def setUpClass(cls):
        import five_axis
        cls.fa = five_axis

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
        usd = self.fa.usd_community_mix(scorer, {k: {'fields': v} for k, v in builder.out.items()}, {})
        self.assertEqual(usd['m']['value'], entries[0]['value'])
        self.assertTrue(usd['m']['fallback'])
        self.assertEqual(usd['m']['kind'], 'derived')
        self.assertEqual(usd['unlisted']['status'], 'missing')
        # Round 8: when arena.agent_sessions is present, it is the primary source (kind community, fallback False).
        builder.out['m']['usd_per_hour'].append({'source': 'arena.agent_sessions', 'kind': 'community', 'effort': 'max',
                                                 'value': 6.125, 'as_of': '2026-11-08', 'url': 'https://arena.ai/leaderboard/agent',
                                                 'method': 'Cost/Task (P50) / Time/Task (P50) x 60', 'sessions': 1234})
        usd_arena = self.fa.usd_community_mix(scorer, {k: {'fields': v} for k, v in builder.out.items()}, {})
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
        usd = self.fa.usd_community_mix(scorer, {'m': {'fields': meas['models']['m']}}, meas)
        self.assertEqual((usd['m']['value'], usd['m']['fallback']), (13.688, True))
        self.assertIn('arena.agent_sessions distrusted', usd['m']['merge'])
        self.assertNotIn('not listed', usd['m']['merge'])

    def test_usd_takes_arena_then_the_labelled_option_b_and_never_a_median_across_scales(self):
        """Round 9 F4: with arena, option B and other-scale listings all present, arena alone is the value; no median is
        taken across sources, and a source outside the one basis (Artificial Analysis list-price tokens) is never consulted."""
        entries = [{'source': 'aa.cost_time', 'kind': 'benchmark', 'effort': 'high', 'value': 7.50},
                   {'source': 'arena.agent_sessions', 'kind': 'community', 'effort': 'high', 'value': 6.658},
                   {'source': 'derived.community_mix', 'kind': 'derived', 'effort': 'high', 'value': 31.901}]
        meas = {'models': {'a': {'usd_per_hour': entries}, 'b': {'usd_per_hour': [entries[0], entries[2]]}, 'c': {'usd_per_hour': [entries[0]]}}}
        scorer = self.fa.Scorer(meas, {'models': dict.fromkeys(meas['models'], {})})
        usd = self.fa.usd_community_mix(scorer, {m: {'fields': f} for m, f in meas['models'].items()}, meas)
        self.assertEqual((usd['a']['value'], usd['a']['used'], usd['a']['kind'], usd['a']['fallback']),
                         (6.658, ['arena.agent_sessions'], 'community', False))
        self.assertEqual((usd['b']['value'], usd['b']['used'], usd['b']['kind'], usd['b']['fallback']),
                         (31.901, ['derived.community_mix'], 'derived', True))
        self.assertTrue(usd['b']['merge'].startswith('fallback ('))
        self.assertEqual((usd['c']['value'], usd['c']['status']), (None, 'missing'))


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
