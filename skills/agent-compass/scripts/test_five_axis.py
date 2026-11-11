"""The five-axis path (round 047, rounds 2-7): off by default; when on, the axes are the only dimension system (no tier,
vectors, route lists, harness factors or difficulty floors; round 7), quality is the continuous fit of the axis scores to
the demand with a shortfall penalty, usd merges cited sources, capacity comes down the cited ladder (community,
time-to-exhaust, vendor multiplier; an empty ladder is a data error that still prices scarcity live), every long window
charges burn x a pacing shadow price, ties go to the lower opportunity cost then the higher level, and the classifier may
answer the axis demand itself."""
import copy
from datetime import datetime, timezone
import json
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
           'needs_visual': False, 'needs_gui': False, 'local_only': True, 'wants_fast': False, 'read_only_review': False, 'interaction_style': 'engineering', 'multi_task': False,
           'task_count': '1', 'expected_hours': 1.0}
EFFORT_NAMES = ('low', 'medium', 'high', 'xhigh', 'max')


def level_targets(t1=-1.0, t2=-0.5, t3=0.0):
    """One category's panel targets as five-axis-scores.json writes them (level 1 the panel's weakest, 2 the median, 3
    the anchor; `zero` is the level-1 target). tool_operation asks per facet."""
    def lv(t, name):
        return {'target': t, 'zero': t1, 'statistic': name, 'n': 12}
    return {'levels': {'1': lv(t1, 'panel minimum'), '2': lv(t2, 'panel median'), '3': lv(t3, 'anchor')},
            'continuity': round(t3 - t2, 4)}


def tool_targets(t1=-1.0, t2=-0.5, t3=0.0):
    facet = lambda t: {'target': t, 'zero': t1, 'n': 12}
    return {'levels': {'1': {'by_facet': {}, 'reason': 'no ability requirement'},
                       '2': {'by_facet': {'responsiveness': facet(t2)}},
                       '3': {'by_facet': {'responsiveness': facet(max(t2, t3)), 'image_recognition': facet(t3)}}},
            'continuity': round(max(t2, t3) - t2, 4)}


TARGETS = {'divergence': level_targets(), 'coding': level_targets(), 'aesthetics': level_targets(),
           'tool_operation': tool_targets()}
ANCHOR = {'model': 'claude-opus-5-5', 'value': 0.0, 'scale': 'panel standard score', 'panel_n': 12, 'targets': TARGETS}


def demand(div, coding, aes, tool):
    return {'divergence': div, 'coding': coding, 'aesthetics': aes, 'tool_operation': tool}


def z_of(s):
    """A fixture score on the old 0..1 reading as a panel standard score: the anchor's 1.0 is 0."""
    return round(float(s) - 1.0, 4)


def cell(z, status='measured'):
    return {'value': z, 'by_effort': dict.fromkeys(EFFORT_NAMES, z), 'status': status}


def axes_of(scores, missing=()):
    out = {}
    for a in allocator.FIVE_AXES:
        if a in missing:
            out[a] = {'value': None, 'by_effort': {}, 'status': 'missing', 'flag': 'missing', 'facets': {},
                      'missing_facets': []}
            continue
        z = z_of(scores[a])
        out[a] = {**cell(z), 'facets': ({'responsiveness': cell(z), 'image_recognition': cell(z)}
                                        if a == 'tool_operation' else {})}
    return out


def row(scores=0.8, usd=5.0, missing=(), image_input='yes', dur=1.0, fast=None, price=True):
    scores = scores if isinstance(scores, dict) else dict.fromkeys(allocator.FIVE_AXES, scores)
    img_val = True if image_input == 'yes' else False if image_input == 'no' else None
    axes = axes_of(scores, missing)
    out = {'axes': axes, 'scores': {a: c['value'] for a, c in axes.items()}, 'missing_axes': list(missing),
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
        doc = document(self.accounts['models'], **{'gpt-6.1-sol': row(demand(0.2, 0.9, 0.9, 0.5))})
        coding = self.plan(self.accounts, doc, profile={'axis_demand': demand(0, 2, 0, 0)})
        creative = self.plan(self.accounts, doc, profile={'axis_demand': demand(3, 1, 0, 0), 'code_complexity': 0})
        sol_high = lambda d: next(c for c in self.rows_for(d, 'gpt-6.1-sol') if c['bundle'].get('effort') == 'high')
        for d in (coding, creative):
            self.assertFalse([e for c in d['ranking'] for e in c['excluded'] if 'floor' in e])
            self.assertFalse([c for c in d['ranking'] if c.get('below_floor')])
        self.assertEqual(sol_high(creative)['excluded'], [])
        self.assertGreater(sol_high(creative)['five_axis_fit']['divergence']['shortfall'], 0.5)
        self.assertGreater(sol_high(coding)['components']['fit'], sol_high(creative)['components']['fit'] + 0.3)
        self.assertEqual(sol_high(coding)['components']['five_axis_shortfall'], 0.0)

    def test_fit_is_the_capped_distance_from_the_panel_weakest_in_panel_sd_units(self):
        """Q7 (user, 2026-11-11): fit = sum w (min(z, t) - t1) / sum w; 0 is the panel's weakest, no credit above the
        target, below the weakest is negative. tool_operation level 1 asks for nothing; level 3 is min over its facets."""
        d = demand(3, 1, 0, 2)
        targets = allocator.five_axis_targets(d, TARGETS)
        self.assertEqual(targets, {'divergence': {'target': 0.0, 'zero': -1.0}, 'coding': {'target': -1.0, 'zero': -1.0},
                                   'tool_operation': {'by_facet': {'responsiveness': {'target': -0.5, 'zero': -1.0}}}})
        values = {'divergence': -0.3, 'coding': 0.4, 'aesthetics': -3.0,
                  'tool_operation': {'responsiveness': -0.8, 'image_recognition': -5.0}}
        fit, rows = allocator.five_axis_fit(values, d, targets)
        self.assertAlmostEqual(fit, (3 * 0.7 + 1 * 0.0 + 2 * 0.2) / 6, places=4)
        self.assertNotIn('aesthetics', rows)                 # an undemanded category does not count
        above = allocator.five_axis_fit({**values, 'divergence': 1.5}, d, targets)[0]
        self.assertAlmostEqual(above, (3 * 1.0 + 0 + 2 * 0.2) / 6, places=4)        # capped at the target
        below = allocator.five_axis_fit({**values, 'divergence': -2.0}, d, targets)[0]
        self.assertLess(below, 0.2)
        self.assertAlmostEqual(below, (3 * -1.0 + 0 + 2 * 0.2) / 6, places=4)       # below the weakest: negative term
        # tool_operation level 1: no requirement, so it is not part of the fit at all
        self.assertEqual(allocator.five_axis_targets(demand(0, 0, 0, 1), TARGETS), {})
        # level 3: the weaker facet decides; a level never asks less of a facet than the level below
        t3 = allocator.five_axis_targets(demand(0, 0, 0, 3), TARGETS)['tool_operation']['by_facet']
        self.assertEqual(t3['responsiveness']['target'], 0.0)
        mono = allocator.five_axis_targets(demand(0, 0, 0, 3), {'tool_operation': tool_targets(-1.7, 0.14, 0.0)})
        self.assertEqual(mono['tool_operation']['by_facet']['responsiveness']['target'], 0.14)
        fit3, rows3 = allocator.five_axis_fit(values, demand(0, 0, 0, 3), {'tool_operation': {'by_facet': t3}})
        self.assertAlmostEqual(fit3, -4.0, places=4)
        self.assertEqual(rows3['tool_operation']['facets']['image_recognition']['term'], -4.0)

    def test_continuity_is_one_demand_level_of_the_highest_demanded_categories(self):
        targets = {**TARGETS, 'coding': level_targets(-2.0, -1.4, 0.0), 'tool_operation': tool_targets(-1.7, 0.14, 0.0)}
        self.assertEqual(allocator.five_axis_continuity_value(demand(1, 3, 0, 3), targets), 1.4)
        self.assertEqual(allocator.five_axis_continuity_value(demand(0, 0, 0, 3), targets), 0.0)    # t3 - t2 = 0
        self.assertEqual(allocator.five_axis_continuity_value(demand(0, 0, 0, 0), targets), 0.0)

    def test_missing_demanded_axis_leaves_automatic_routing_but_a_named_model_runs(self):
        target = 'claude-sonnet-5-5'
        doc = document(self.accounts['models'], **{target: row(demand(0.0, 0.95, 0.95, 0.9), missing=['divergence'])})
        blind = self.plan(self.accounts, doc, profile={'axis_demand': demand(2, 2, 0, 0)})
        sighted = self.plan(self.accounts, doc, profile={'axis_demand': demand(0, 2, 0, 0)})
        named = self.plan(self.accounts, doc, profile={'axis_demand': demand(2, 2, 0, 0)}, constraints={'model': target})
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
        auto = self.plan(acc, doc, profile={'axis_demand': demand(0, 2, 0, 0)})
        self.assertTrue(all(any('usd_per_hour has no cited source on a metered account' in e for e in c['excluded'])
                            for c in self.rows_for(auto, target)))
        named = self.plan(acc, doc, profile={'axis_demand': demand(0, 2, 0, 0)}, constraints={'model': target})
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
        """Quota economy v3 + R2-F4: N = F - C is never rounded; C carries billed vs free, pool used share, and +fast burn.
        `earliest_reset_hours` is NOT in `five_axis_order` — reset timing enters only through C inside N."""
        self.assertNotIn('earliest_reset_hours', allocator.FIVE_AXIS_ORDER_KEYS)
        def cand(name, score=0.5, hours=1.0, usd=10.0, level=0.8, wait=0.0, place=0, reset=48.0):
            return {'id': name, 'score': score, 'available_in_hours': wait or None,
                    'expected_hours': hours, 'duration_hours': hours, 'expected_usd': usd,
                    'components': {'five_axis_level': level, 'harness_place': place, 'earliest_reset_hours': reset}}
        pool = [
            cand('slower_expensive', hours=0.8, usd=20.0),
            cand('faster_dear', hours=0.2, usd=20.0),
            cand('faster_cheap', hours=0.2, usd=5.0),
            cand('higher_n', score=0.50001, hours=2.0, usd=50.0, reset=160.0),
            cand('sooner_reset_same_n', score=0.5, hours=1.5, usd=30.0, reset=2.0),
            cand('waits_but_quick', hours=0.1, usd=1.0, wait=3.0, reset=1.0),
            cand('faster_cheap_higher_level', hours=0.2, usd=5.0, level=0.9),
            cand('faster_cheap_later_harness', hours=0.2, usd=5.0, level=0.9, place=1),
        ]
        self.assertEqual([c['id'] for c in sorted(pool, key=allocator.five_axis_order)],
                         ['higher_n', 'faster_cheap_higher_level',
                          'faster_cheap_later_harness', 'faster_cheap',
                          'faster_dear', 'slower_expensive', 'sooner_reset_same_n', 'waits_but_quick'])
        self.assertEqual(allocator.five_axis_deciding_key(pool[3], pool[4]), 'net_score')
        self.assertEqual(allocator.five_axis_deciding_key(pool[2], pool[4]), 'completion_hours')
        self.assertEqual(allocator.five_axis_deciding_key(pool[2], pool[1]), 'usd')
        self.assertEqual(allocator.five_axis_deciding_key(pool[1], pool[0]), 'completion_hours')
        self.assertEqual(allocator.five_axis_deciding_key(pool[6], pool[2]), 'axis_level')
        self.assertEqual(allocator.five_axis_deciding_key(pool[6], pool[7]), 'harness_place')
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
            d = allocator.plan({'objective': 't', 'hours': 1.0, 'profile': {**PROFILE, 'axis_demand': demand(2, 2, 0, 0)}},
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
                'claude-opus-5-5': row(demand(0.7333, 0.8, 0.8, 0.8000), usd=22.86, dur=1.0, fast=fast_cfg),
                'claude-sonnet-5-5': row(demand(0.6000, 0.6, 0.6, 0.7000), usd=12.83, dur=0.98),
                'claude-haiku-5-5': row(demand(0.5000, 0.45, 0.45, 0.5000), usd=2.67, dur=0.11),
                'glm-5.3-flash-free': row(demand(0.6820, 0.558, 0.558, 0.5000), usd=1.50, image_input='no'),
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
                     'profile': {**PROFILE, 'axis_demand': demand(2, 3, 0, 0), 'expected_hours': hours}},
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
        """Round 11 item 4: when tool_operation is 3 (the task reads images), models with public image_input == 'no' are
        excluded; models with unknown modality leave automatic routing under Mode B (dispatched with warning only when
        named via --model)."""
        doc = document(
            self.accounts['models'],
            **{
                'gpt-6.1-sol': row(0.85, usd=6.12, image_input='no'),
                'claude-sonnet-5-5': row(0.85, usd=12.83, image_input='unknown'),
                'claude-opus-5-5': row(0.85, usd=22.86, image_input='yes'),
            },
        )
        # Level 3 (reading images or operating a GUI through screenshots):
        for vis_level in (3,):
            d = self.plan(self.accounts, doc, profile={'axis_demand': demand(1, 2, 0, vis_level)})
            sol_rows = self.rows_for(d, 'gpt-6.1-sol')
            sonnet_rows = self.rows_for(d, 'claude-sonnet-5-5')
            opus_rows = [c for c in self.rows_for(d, 'claude-opus-5-5') if not c['excluded']]
            self.assertTrue(all(any('model does not accept image input' in e for e in c['excluded']) for c in sol_rows))
            self.assertTrue(all(any('image_input modality has no cited source' in e for e in c['excluded']) for c in sonnet_rows))
            self.assertTrue(opus_rows)
        # Naming the unknown-modality model via --model dispatches it with a Mode B warning:
        named = self.plan(self.accounts, doc, profile={'axis_demand': demand(1, 2, 0, 3)},
                          constraints={'model': 'claude-sonnet-5-5'})
        chosen = next(c for c in named['ranking'] if c['id'] == named['choice'])
        self.assertEqual(allocator.canonical(chosen['bundle']['model'], self.accounts), 'claude-sonnet-5-5')
        self.assertIn('data error: image_input modality has no cited source; dispatched because it was named',
                      ' '.join(chosen['notes']))
        # Naming a model whose public data explicitly says no image input stays blocked even when named:
        blocked = self.plan(self.accounts, doc, profile={'axis_demand': demand(1, 2, 0, 3)},
                            constraints={'model': 'gpt-6.1-sol'})
        self.assertEqual(blocked['choice'], 'none')
        # Level 0-2 (no image input needed): neither is excluded for modality.
        text_only = self.plan(self.accounts, doc, profile={'axis_demand': demand(1, 2, 0, 2)})
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
        auto = self.plan(acc, doc, profile={'axis_demand': demand(1, 2, 0, 0)})
        glm_rows = self.rows_for(auto, 'glm-5.3-flash-free')
        self.assertTrue(glm_rows)
        self.assertTrue(all(any('capacity_unknown (quota unknown, spend unpriced)' in e for e in c['excluded'])
                            for c in glm_rows))
        self.assertTrue(any('opencode: capacity_unknown — quota unknown, spend unpriced' in w
                            for w in auto['decision'].get('warnings', ())))
        # Naming --model glm-5.3-flash-free runs it with the "quota unknown, spend unpriced" warning and local spend U:
        named = self.plan(acc, doc, profile={'axis_demand': demand(1, 2, 0, 0)},
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
        rejected = shipped['rejected_fits']
        t, tb = fable52['tier']['value'], fable51['tier']['value']
        estimated = 0
        for axis, ax in fable52['axes'].items():
            for comp, row in ax['facets'].items():
                self.assertIn(row['status'], ('measured', 'measured_partial', 'estimated'), (axis, comp))
                if row['status'] != 'estimated':
                    continue
                estimated += 1
                est, base = row['estimate'], fable51['axes'][axis]['facets'][comp]
                fit_key = f'{axis}.{comp}~tier'
                self.assertEqual((est['method'], est['base_model']), ('sibling_shift', 'claude-fable-5-1'))
                if fit_key in fits:
                    f = fits[fit_key]
                    self.assertEqual({k: est[k] for k in ('r', 'skill', 'loo_rmse', 'n')},
                                     {'r': round(f['r'], 4), 'skill': round(f['skill'], 4), 'loo_rmse': round(f['loo_rmse'], 4),
                                      'n': f['n']})
                    self.assertAlmostEqual(row['value'], base['value'] + f['b'] * (t - tb), places=3)
                else:
                    # R2-F3: rejected fits (e.g. coding.faithfulness~tier with skill <= 0) never add noise slope (shift = 0)
                    self.assertIn(fit_key, rejected)
                    self.assertEqual(est['shift'], 0.0)
                    self.assertAlmostEqual(row['value'], base['value'], places=4)
        self.assertTrue(estimated)
        # Design Arena lists it: its evidence is not Artificial Analysis alone.
        self.assertFalse(fable52['evidence']['aa_only'])
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
                clean = allocator.plan({'objective': 't', 'profile': {**PROFILE, 'axis_demand': demand(2, 3, 0, 0)}},
                                       usage, NOW, accounts=copy.deepcopy(acc), overrides=[], records=[],
                                       host_policy_config=self.policy)
                poisoned = allocator.plan({'objective': 't', 'profile': {**PROFILE, 'axis_demand': demand(2, 3, 0, 0)}},
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

    def test_an_in_flight_ledger_record_is_mapped_once_onto_the_four_categories(self):
        """The one read-side mapping (user, 2026-11-11): divergence as is, coding = max(global_rigor, module_precision),
        tool_operation from vision_tools 0->0, 1->2, 2->3, 3->3 (at least 2 when it marked realtime_loop), aesthetics 0.
        A fresh classifier answer in the old shape is still invalid: only recorded profiles are mapped."""
        old = {**PROFILE, 'realtime_loop': False,
               'axis_demand': {'divergence': 2, 'global_rigor': 1, 'module_precision': 3, 'vision_tools': 1}}
        mapped = allocator.ledger_profile(old)
        self.assertEqual(mapped['axis_demand'], demand(2, 3, 0, 2))
        self.assertNotIn('realtime_loop', mapped)
        self.assertEqual(allocator.validate_profile(mapped)['axis_demand'], demand(2, 3, 0, 2))
        for vision, tool in ((0, 0), (1, 2), (2, 3), (3, 3)):
            rec = {**old, 'axis_demand': {**old['axis_demand'], 'vision_tools': vision}}
            self.assertEqual(allocator.ledger_profile(rec)['axis_demand']['tool_operation'], tool)
        live = {**old, 'realtime_loop': True, 'axis_demand': {**old['axis_demand'], 'vision_tools': 0}}
        self.assertEqual(allocator.ledger_profile(live)['axis_demand']['tool_operation'], 2)
        current = {**PROFILE, 'axis_demand': demand(1, 2, 0, 0)}
        self.assertIs(allocator.ledger_profile(current), current)
        with self.assertRaises(ValueError):
            allocator.validate_profile(old)

    def test_the_scorer_always_asks_for_axis_demand(self):
        caps = {'routes': {'general_implementation': {}, 'other_or_mixed': {}}}
        schema = route_judge.scorer_schema(allocator, caps)
        self.assertIn('axis_demand', schema['properties'])
        self.assertIn('axis_demand', schema['required'])
        self.assertEqual(schema['properties']['axis_demand'], allocator.FIVE_AXIS_DEMAND_SCHEMA)

    def test_can_wait_hours_defers_only_for_higher_net_score_without_fixed_wait_cost_constant(self):
        """Rounds 8-13b + F3: no fixed 0.15 delay penalty and no 0.06 five_axis_wait_cost constant on the five-axis path.
        Without `can_wait_hours`, `plan()` starts the best ready-now candidate immediately; when `can_wait_hours` is given,
        a task defers when and only when a waiting candidate has a strictly higher unrounded net score `N = F - C` than the
        best ready-now candidate (so a local routine task where Claude's $1,700 pool costs less per dollar than Codex's $615
        pool defers, while an outbound routine task where free `dot` has `C = 0` starts immediately)."""
        self.assertFalse(hasattr(allocator, 'five_axis_wait_cost'))
        base = {name: row(0.55, usd=5.0) for name, info in self.accounts['models'].items() if isinstance(info, dict)}
        doc = document(self.accounts['models'],
                       **{**base,
                          'claude-opus-5-5': row(demand(0.733, 0.8, 0.8, 0.800), usd=22.86),
                          'gpt-6.1-sol': row(demand(0.587, 0.787, 0.787, 0.732), usd=6.12)})
        # Without can_wait_hours: resolves to the best immediately startable candidate (never defer, never needs_user_decision).
        immediate = self.plan(self.accounts, doc, state='5h-only scarce',
                              profile={'axis_demand': demand(3, 3, 0, 0), 'code_complexity': 0})
        self.assertEqual(immediate['decision']['action'], 'start')
        self.assertFalse(immediate['decision']['needs_user_decision'])
        self.assertEqual(immediate['selected_bundle']['account'], 'codex')
        self.assertIn(immediate['decision']['better_if_waiting']['bundle'], ('opus55_xhigh', 'opus55_max'))
        # With can_wait_hours: defers iff the waiting candidate has strictly higher unrounded N = F - C.
        routine = self.plan(self.accounts, doc, state='5h-only scarce', can_wait_hours=4.0,
                            profile={'axis_demand': demand(0, 1, 0, 0)})
        routine_free = self.plan(self.accounts, doc, state='5h-only scarce', can_wait_hours=4.0,
                                 profile={'axis_demand': demand(0, 1, 0, 0), 'local_only': False, 'code_complexity': 0})
        frontier = self.plan(self.accounts, doc, state='5h-only scarce', can_wait_hours=4.0,
                             profile={'axis_demand': demand(3, 3, 0, 0), 'code_complexity': 0})
        ready_now = max((c for c in routine['ranking'] if not c.get('excluded') and not c.get('available_in_hours')),
                        key=lambda c: c['score'])
        self.assertEqual(routine['ranking'][0]['score'], max(c['score'] for c in routine['ranking'] if not c.get('excluded')))
        # Round 15 + R2-F11: with the v3.1 task-sized burst size at 10% weekly use, neither pool's quota costs above float64
        # resolution, so waiting candidates do not beat ready-now N and the routine task starts now on codex:
        self.assertEqual(round(routine['ranking'][0]['score'], 4), round(ready_now['score'], 4))
        self.assertEqual(routine['decision']['action'], 'start')
        self.assertEqual(ready_now['bundle']['account'], 'codex')
        # When codex is over its weekly line and claude's 7-day has moderate use (<100% slack so codex prices >0, while
        # claude's 7-day is still well under its line behind its 5h wait), routine defers for claude's strictly higher N = F - C:
        from unittest.mock import patch as _patch
        tight_codex_usage = self.usage(self.accounts, '5h-only scarce')
        for w in tight_codex_usage['accounts']['codex']['windows']:
            if w['id'] == 'usage':
                w['remaining_pct_before_reset'] = 8.0
                w['observed_remaining_pct'] = 8.0
                w['used_pct'] = 92.0
        for w in tight_codex_usage['accounts']['claude']['windows']:
            if w['id'] == 'seven_day':
                w['remaining_pct_before_reset'] = 45.0
                w['observed_remaining_pct'] = 45.0
                w['used_pct'] = 55.0
        with _patch.object(allocator, 'load_five_axis', return_value=doc):
            routine_tight = allocator.plan(
                {'objective': 't', 'can_wait_hours': 4.0,
                 'profile': {**PROFILE, 'axis_demand': demand(0, 1, 0, 0)}},
                tight_codex_usage, NOW, accounts=copy.deepcopy(self.accounts),
                overrides=[], records=[], host_policy_config={'allowed_accounts': ['claude', 'codex']},
            )
        ready_tight = max((c for c in routine_tight['ranking'] if not c.get('excluded') and not c.get('available_in_hours')),
                          key=lambda c: c['score'])
        self.assertGreater(routine_tight['ranking'][0]['score'], ready_tight['score'])
        self.assertEqual(routine_tight['decision']['action'], 'defer')
        self.assertEqual(routine_free['decision']['action'], 'start')
        self.assertEqual(routine_free['selected_bundle']['account'], 'dot')
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
                          'claude-opus-5-5': row(demand(0.733, 0.8, 0.8, 0.800), usd=22.86),
                          'gpt-6.1-sol': row(demand(0.587, 0.787, 0.787, 0.732), usd=6.12)})
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
    """Taxonomy R2 (2026-11-11): values are panel standard scores relative to the anchor at High, panel targets move
    nothing when a new non-panel model appears, structural/statistical checks guard raw listings, facet-on-tier fits
    report skill without gating, and local flags and samples are not sources."""

    @classmethod
    def setUpClass(cls):
        import five_axis
        import measurements
        import tiers
        cls.fa, cls.ms, cls.tiers = five_axis, measurements, tiers

    def panel_models(self):
        anchors = list(self.tiers.ANCHORS)
        extras = [f'p{i}' for i in range(max(4, self.ms.MIN_PANEL + 2 - len(anchors)))]
        return anchors + extras

    def test_panel_standard_scores_put_anchor_at_zero_and_reject_unanchored_or_thin_boards(self):
        members = self.panel_models()
        b = self.ms.Builder.__new__(self.ms.Builder)
        b.source_rows = {s: {'file': 'f'} for s in ('lmarena.webdev', 'designarena.website', 'aa.mmmu_pro',
                                                    'no_anchor', 'too_few', 'flat')}
        b.missing, b.manifest = {}, {}
        raw = {
            'lmarena.webdev': {m: 1500.0 - 25.0 * i for i, m in enumerate(members)},
            'designarena.website': {m: 1400.0 - 20.0 * i for i, m in enumerate(members)},
            # omits one anchor (not core), still has >= MIN_PANEL panel members
            'aa.mmmu_pro': {m: 0.85 - 0.02 * i for i, m in enumerate(members) if m != 'gemini-3.8-flash-high'},
            'no_anchor': {m: 0.7 - 0.01 * i for i, m in enumerate(members) if m != self.ms.ANCHOR},
            # omits gemini-3.8-flash-high (not core) and lists fewer than MIN_PANEL panel members
            'too_few': {self.ms.ANCHOR: 0.8, **{m: 0.7 - 0.05 * i for i, m in enumerate(members[-4:])}},
            'flat': dict.fromkeys(members, 0.5),
        }
        panel, core, scale = b.panel(raw)
        self.assertEqual(set(panel), set(members))
        self.assertEqual(set(core), {'lmarena.webdev', 'designarena.website', 'flat'})
        self.assertEqual(set(scale), {'lmarena.webdev', 'designarena.website', 'aa.mmmu_pro'})
        self.assertEqual(scale['lmarena.webdev']['anchor_raw'], 1500.0)
        self.assertGreater(scale['lmarena.webdev']['sd'], 0.0)
        self.assertIn('not listed', b.source_rows['no_anchor']['not_scaled'])
        self.assertIn(f'needs {self.ms.MIN_PANEL}', b.source_rows['too_few']['not_scaled'])
        self.assertIn('same value', b.source_rows['flat']['not_scaled'])

    def test_structural_and_statistical_checks_drop_bad_listings_and_flag_non_monotone_high(self):
        b = self.ms.Builder.__new__(self.ms.Builder)
        b.acc = {'models': {'m_bad_tag': {}, 'm_range': {}, 'm_dup1': {}, 'm_dup2': {}, 'm_stat': {}, 'm_ok': {}}}
        b.checks, b.missing, b.manifest = [], {}, {}
        boards = {
            'aa.terminalbench_4': {
                'listed': {'Bad (NVFP4)': 'Bad (NVFP4)', 'Range (High)': 'Range (High)', 'Shared': 'Shared',
                           'Stat (High)': 'Stat (High)', 'Ok (High)': 'Ok (High)'},
                'by_effort': {'Stat (High)': {'medium': 0.70, 'high': 0.55, 'xhigh': 0.80},
                              'Ok (High)': {'medium': 0.50, 'high': 0.60, 'xhigh': 0.70}},
            },
            'arena.agent_code': {'listed': {'A': 'A', 'B': 'B', 'C': 'C'}},
            'aa.frontiercode_2': {'listed': {'A': 'A', 'B': 'B', 'C': 'C'}},
        }
        raw = {
            'aa.terminalbench_4': {'m_bad_tag': 0.5, 'm_range': 1.5, 'm_dup1': 0.6, 'm_dup2': 0.6,
                                   'm_stat': 0.55, 'm_ok': 0.60},
            'arena.agent_code': {'m_dup1': 0.1, 'm_dup2': 0.2, 'm_ok': 0.3},
            'aa.frontiercode_2': {'m_dup1': 0.1, 'm_dup2': 0.2, 'm_ok': 0.3},  # identical vector -> duplicate_board
        }
        names = {
            'aa.terminalbench_4': {'m_bad_tag': 'Bad (NVFP4)', 'm_range': 'Range (High)',
                                   'm_dup1': 'Shared', 'm_dup2': 'Shared', 'm_stat': 'Stat (High)', 'm_ok': 'Ok (High)'},
            'arena.agent_code': {'m_dup1': 'A', 'm_dup2': 'B', 'm_ok': 'C'},
            'aa.frontiercode_2': {'m_dup1': 'A', 'm_dup2': 'B', 'm_ok': 'C'},
        }
        b.structural(boards, raw, names)
        found = {(c.get('model'), c['source'], c['check'], c['level']) for c in b.checks}
        self.assertIn(('m_bad_tag', 'aa.terminalbench_4', 'variant_not_effort', 'structural'), found)
        self.assertIn(('m_range', 'aa.terminalbench_4', 'raw_out_of_range', 'structural'), found)
        self.assertIn(('m_dup1', 'aa.terminalbench_4', 'listing_matched_twice', 'structural'), found)
        self.assertIn(('m_dup2', 'aa.terminalbench_4', 'listing_matched_twice', 'structural'), found)
        self.assertIn(('m_stat', 'aa.terminalbench_4', 'high_outside_adjacent_efforts', 'statistical'), found)
        self.assertIn((None, 'arena.agent_code', 'duplicate_board', 'structural'), found)
        self.assertEqual(set(raw['aa.terminalbench_4']), {'m_stat', 'm_ok'})
        self.assertEqual(raw['arena.agent_code'], {})

    def test_listed_shift_uses_same_facet_aa_curve_and_marks_unshifted_boards_measured_partial(self):
        meas = {'panel': {'models': [self.ms.ANCHOR, 'm1']}, 'models': {
            self.ms.ANCHOR: {'coding': [
                {'source': 'aa.terminalbench_4', 'facet': 'execution', 'value': 0.0,
                 'z_by_effort': {'low': -0.8, 'medium': -0.3, 'high': 0.0, 'xhigh': 0.2, 'max': 0.3}},
                {'source': 'arena.agent_code', 'facet': 'execution', 'value': 0.0,
                 'listed_as': 'Claude Opus 5.5 (Max)', 'listed_effort': 'max'},
                {'source': 'arena.agent_alignment', 'facet': 'faithfulness', 'value': 0.0,
                 'listed_as': 'Claude Opus 5.5 Max', 'listed_effort': 'max'},
            ]},
            'm1': {'coding': [
                {'source': 'aa.terminalbench_4', 'facet': 'execution', 'value': -0.5,
                 'z_by_effort': {'low': -1.2, 'medium': -0.8, 'high': -0.5, 'xhigh': -0.3, 'max': -0.1}},
                {'source': 'arena.agent_code', 'facet': 'execution', 'value': -0.2,
                 'listed_as': 'M1 (Max)', 'listed_effort': 'max'},
                {'source': 'arena.agent_alignment', 'facet': 'faithfulness', 'value': -0.4,
                 'listed_as': 'M1 High', 'listed_effort': 'high'},
            ]},
        }}
        rows, _, _, _, _, _ = self.fa.Scorer(meas, {'models': {self.ms.ANCHOR: {}, 'm1': {}}}).score()
        ex = rows['m1']['axes']['coding']['facets']['execution']
        self.assertEqual(ex['status'], 'measured')
        self.assertEqual(ex['sources']['arena.agent_code']['effort_shift'], 'aa')
        # faithfulness has no AA board: single-variant listing stays flat with status measured_partial
        fa = rows['m1']['axes']['coding']['facets']['faithfulness']
        self.assertEqual(fa['status'], 'measured_partial')
        self.assertEqual(fa['sources']['arena.agent_alignment']['effort_shift'], 'none')
        self.assertEqual(rows['m1']['axes']['coding']['status'], 'measured_partial')

    def test_a_stale_capability_source_is_a_surfaced_yellow_error(self):
        """Advisor taxonomy report, bug 4: staleness is checked for every capability source, not only capacity."""
        src = next(iter(self.ms.CAPABILITY_SOURCES))
        ages = {src: {'as_of': '2026-08-01', 'age_days': 100, 'stale': True},
                'aa.price': {'as_of': '2026-08-01', 'age_days': 100, 'stale': True},           # not a capability source
                'metr.x': {'missing': True, 'reason': 'not used'}}
        errors = self.fa.source_errors(ages)
        self.assertEqual([(e['source'], e['level'], e['reason'], e['field']) for e in errors],
                         [(src, 'yellow', 'stale_source', 'capability')])
        self.assertEqual(errors[0]['threshold_days'], self.fa.STALE_DAYS)

    def test_a_new_non_panel_top_model_moves_no_panel_score_and_no_target(self):
        members = self.panel_models()
        raw = {'lmarena.creative_writing': {m: 1500.0 - 20.0 * i for i, m in enumerate(members)}}
        b1 = self.ms.Builder.__new__(self.ms.Builder)
        b1.source_rows, b1.missing, b1.manifest = {'lmarena.creative_writing': {'file': 'f'}}, {}, {}
        panel1, _, scale1 = b1.panel(raw)
        grown = {'lmarena.creative_writing': {**raw['lmarena.creative_writing'], 'newtop': 1700.0}}  # not on all core boards if added to one
        # Add a second core board where newtop is absent so newtop is outside the panel
        raw2 = {**raw, 'arena.agent_code': {m: 50.0 - 2.0 * i for i, m in enumerate(members)}}
        grown2 = {'lmarena.creative_writing': {**raw2['lmarena.creative_writing'], 'newtop': 1700.0},
                  'arena.agent_code': dict(raw2['arena.agent_code'])}
        for d in (b1,):
            d.source_rows['arena.agent_code'] = {'file': 'f'}
        b_before = self.ms.Builder.__new__(self.ms.Builder)
        b_before.source_rows = {s: {'file': 'f'} for s in raw2}
        b_before.missing, b_before.manifest = {}, {}
        panel_a, _, scale_a = b_before.panel(raw2)

        b_after = self.ms.Builder.__new__(self.ms.Builder)
        b_after.source_rows = {s: {'file': 'f'} for s in grown2}
        b_after.missing, b_after.manifest = {}, {}
        panel_b, _, scale_b = b_after.panel(grown2)

        self.assertEqual(panel_a, panel_b)
        self.assertEqual(scale_a, scale_b)
        # And Scorer targets depend only on panel models: adding newtop outside the panel moves no target.
        def build_meas(extra_models):
            sc = scale_a['lmarena.creative_writing']
            models = {m: {'divergence': [{'source': 'lmarena.creative_writing', 'facet': 'creative_writing',
                                          'value': round((raw2['lmarena.creative_writing'][m] - sc['anchor_raw']) / sc['sd'], 4)}]}
                      for m in members}
            models.update(extra_models)
            return {'panel': {'models': panel_a}, 'models': models}
        _, _, _, _, _, targets_before = self.score(build_meas({}))
        _, _, _, _, _, targets_after = self.score(build_meas({
            'newtop': {'divergence': [{'source': 'lmarena.creative_writing', 'facet': 'creative_writing', 'value': 3.5}]}
        }))
        self.assertEqual(targets_before, targets_after)

    def test_board_best_scaling_would_have_drifted(self):
        """Win rate against the board best moves models relative to the anchor when a new top model appears; panel z does not."""
        old = lambda board, m: 1 / (1 + 10 ** ((max(board.values()) - board[m]) / 400))
        board = {self.ms.ANCHOR: 1500.0, 'b': 1400.0, 'c': 1450.0}
        ratio = old(board, 'b') / old(board, self.ms.ANCHOR)
        board_new = {**board, 'newtop': max(board.values()) + 100}
        self.assertLess(old(board_new, 'b') / old(board_new, self.ms.ANCHOR), ratio - 0.03)

    def test_fit_gates_on_points_and_only_enforces_skill_when_requested(self):
        fit = self.fa.Scorer.fit
        few = [(4.0 + 0.1 * i, -1.0 + 0.1 * i) for i in range(self.fa.MIN_FIT_POINTS - 1)]
        found, why = fit(few)
        self.assertIsNone(found)
        self.assertIn(f'a fit needs {self.fa.MIN_FIT_POINTS}', why)
        enough = [(4.0 + 0.1 * i, -1.0 + 0.1 * i) for i in range(self.fa.MIN_FIT_POINTS)]
        found, why = fit(enough)
        self.assertIsNone(why)
        self.assertEqual(found['n'], self.fa.MIN_FIT_POINTS)
        # Duration fit (require_skill=True) rejects negative slopes and noise-only fits; facet-on-tier (require_skill=False) keeps and reports skill.
        falling = [(4.0 + 0.1 * i, 0.9 - 0.02 * i) for i in range(self.fa.MIN_FIT_POINTS)]
        self.assertIn('not positive', fit(falling, require_skill=True)[1])
        self.assertIsNotNone(fit(falling, require_skill=False)[0])
        noise = [(float(i), 0.5 if i % 2 == 0 else 0.7) for i in range(10)]
        self.assertIn('skill', fit(noise, require_skill=True)[1])
        kept, why_kept = fit(noise, require_skill=False)
        self.assertIsNone(why_kept)
        self.assertLessEqual(kept['skill'], 0.0)
        self.assertGreater(kept['loo_rmse'], 0.0)

    def test_an_estimate_stays_inside_the_range_the_measured_points_reach(self):
        f, _ = self.fa.Scorer.fit([(float(x), -1.5 + 0.2 * x) for x in range(10)])     # measured -1.5 .. 0.3
        self.assertEqual((round(f['y_min'], 4), round(f['y_max'], 4)), (-1.5, 0.3))
        self.assertEqual(self.fa.Scorer.predict(f, 20), 0.3)        # a tier above the best sampled model is not extrapolated
        self.assertEqual(self.fa.Scorer.predict(f, -5), -1.5)
        self.assertEqual(self.fa.Scorer.predict(f, 5), -0.5)

    def test_shipped_data_has_panel_z_scores_monotonic_targets_and_no_local_sample_sources(self):
        ref = Path(__file__).resolve().parents[1] / 'references'
        meas = json.loads((ref / 'public-measurements.json').read_text(encoding='utf-8'))
        used = {e['source'] for fields in meas['models'].values() for entries in fields.values() for e in entries}
        self.assertFalse(used & {'accounts.visual', 'packaged.accounts', 'community.burn'})
        self.assertGreaterEqual(meas['panel']['n'], self.ms.MIN_PANEL)
        scores = json.loads((ref / 'five-axis-scores.json').read_text(encoding='utf-8'))
        self.assertEqual((scores['anchor']['model'], scores['anchor']['value'], scores['anchor']['scale']),
                         (self.ms.ANCHOR, 0.0, 'panel standard score'))
        self.assertNotIn('floors', scores)
        # the anchor sits at 0.0 at High on every measured facet
        for axis, row in scores['models'][self.ms.ANCHOR]['axes'].items():
            for name, facet in row['facets'].items():
                if facet['status'].startswith('measured'):
                    self.assertEqual(facet['value'], 0.0, f'{axis}.{name}')
        # targets are non-decreasing from L1 -> L2 -> L3, including tool_operation.responsiveness (raised_to_level_below)
        targets = scores['anchor']['targets']
        for axis in ('divergence', 'coding', 'aesthetics'):
            lv = targets[axis]['levels']
            self.assertLessEqual(lv['1']['target'], lv['2']['target'], axis)
            self.assertLessEqual(lv['2']['target'], lv['3']['target'], axis)
            self.assertEqual(lv['1']['zero'], lv['1']['target'], axis)
        resp2 = targets['tool_operation']['levels']['2']['by_facet']['responsiveness']['target']
        resp3 = targets['tool_operation']['levels']['3']['by_facet']['responsiveness']['target']
        self.assertGreaterEqual(resp3, resp2)

    def tiered(self, tier, **fields):
        """A model's measurements with its tier read off the arena.ai Overall board."""
        return {'tier': [{'source': 'tier.arena_overall', 'value': tier, 'as_of': '2026-11-09'}], **fields}

    def calibration_set(self, extra):
        """Ten models measured on both coding facets (faithfulness = -1.8 + 0.4 * tier, execution = -2.0 + 0.4 * tier,
        shifted so the anchor sits at 0) with their tiers, and the models in `extra`."""
        models = {}
        for i in range(10):
            t = round(3.0 + 0.2 * i, 2)
            models[f'm{i}'] = self.tiered(t, coding=[
                {'source': 'arena.agent_alignment', 'facet': 'faithfulness', 'value': round((t - 4.8) * 0.4, 4)},
                {'source': 'aa.terminalbench_4', 'facet': 'execution', 'value': round((t - 4.8) * 0.5, 4),
                 'z_by_effort': dict.fromkeys(EFFORT_NAMES, round((t - 4.8) * 0.5, 4))}])
        models[self.ms.ANCHOR] = models.pop('m9')
        panel = list(models)
        models.update(extra)
        return {'panel': {'models': panel}, 'models': models}

    def score(self, meas):
        return self.fa.Scorer(meas, {'models': {m: {} for m in meas['models']}}).score()

    def test_the_tier_is_read_only_from_the_overall_boards(self):
        measured = self.tiered(4.0)
        by_other = {'tier': [{'source': 'tier.aa_index', 'value': 3.5}]}
        stray = {'tier': [{'source': 'lmarena.webdev', 'value': 4.4}, {'source': 'lmarena.tier_fit', 'value': 4.4}]}
        meas = self.calibration_set({'claude-fable-5-1': measured, 'claude-fable-5-2': {},
                                     'by_other': by_other, 'stray': stray, 'bare': {}})
        _, tier, _, _, _, _ = self.score(meas)
        self.assertEqual((tier['claude-fable-5-1']['value'], tier['claude-fable-5-1']['method']), (4.0, 'arena_overall'))
        self.assertEqual((tier['by_other']['value'], tier['by_other']['method']), (3.5, 'other_overall'))
        self.assertEqual((tier['claude-fable-5-2']['value'], tier['claude-fable-5-2']['method'], tier['claude-fable-5-2']['base_model']),
                         (4.0, 'sibling', 'claude-fable-5-1'))
        self.assertEqual((tier['stray']['value'], tier['stray']['method']), (None, 'none'))
        self.assertEqual((tier['bare']['value'], tier['bare']['method']), (None, 'none'))

    def test_the_tier_is_never_a_score_input(self):
        same = {'coding': [{'source': 'arena.agent_alignment', 'facet': 'faithfulness', 'value': -0.3},
                           {'source': 'aa.terminalbench_4', 'facet': 'execution', 'value': -0.2,
                            'z_by_effort': dict.fromkeys(EFFORT_NAMES, -0.2)}]}
        meas = self.calibration_set({'low': self.tiered(3.0, **same), 'high': self.tiered(4.9, **same)})
        rows, _, _, _, _, _ = self.score(meas)
        self.assertEqual(rows['low']['axes']['coding']['value'], rows['high']['axes']['coding']['value'])
        self.assertEqual(rows['low']['axes']['coding']['value'], -0.3)      # min(execution -0.2, faithfulness -0.3)

    def test_every_measured_category_is_the_same_whatever_the_tier_rows_say(self):
        """Behavioural: the tier rows as read, reversed (8 - tier) and absent give identical category values for every
        model whose facets are measured. The tier only estimates a missing facet."""
        meas = self.calibration_set({'extra': self.tiered(4.4, coding=[
            {'source': 'arena.agent_alignment', 'facet': 'faithfulness', 'value': -0.25},
            {'source': 'aa.terminalbench_4', 'facet': 'execution', 'value': -0.45,
             'z_by_effort': dict.fromkeys(EFFORT_NAMES, -0.45)}])})

        def values(m):
            rows, _, _, _, _, _ = self.score(m)
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

    def test_a_missing_facet_is_the_siblings_moved_along_the_fit_by_the_tier_difference(self):
        base = self.tiered(3.8, coding=[
            {'source': 'arena.agent_alignment', 'facet': 'faithfulness', 'value': -0.40},
            {'source': 'aa.terminalbench_4', 'facet': 'execution', 'value': -0.50,
             'z_by_effort': dict.fromkeys(EFFORT_NAMES, -0.50)}])
        new = self.tiered(4.2, coding=[
            {'source': 'aa.terminalbench_4', 'facet': 'execution', 'value': -0.10,
             'z_by_effort': dict.fromkeys(EFFORT_NAMES, -0.10)}])
        rows, _, fits, rejected, _, _ = self.score(self.calibration_set({'claude-fable-5-1': base, 'claude-fable-5-2': new}))
        facet = rows['claude-fable-5-2']['axes']['coding']['facets']['faithfulness']
        self.assertNotIn('coding.faithfulness~tier', rejected)
        self.assertEqual(facet['status'], 'estimated')
        est = facet['estimate']
        self.assertEqual((est['method'], est['base_model'], est['base_value'], est['tier_source']),
                         ('sibling_shift', 'claude-fable-5-1', -0.4, 'arena_overall'))
        self.assertEqual(est['fit'], 'coding.faithfulness~tier')
        self.assertAlmostEqual(est['shift'], fits['coding.faithfulness~tier']['b'] * 0.4, places=4)
        self.assertAlmostEqual(facet['value'], -0.24, places=3)                 # -0.40 + 0.4 * 0.4
        self.assertEqual(rows['claude-fable-5-2']['axes']['coding']['missing_facets'], [])
        self.assertEqual(rows['claude-fable-5-2']['axes']['coding']['status'], 'estimated')
        self.assertAlmostEqual(rows['claude-fable-5-2']['axes']['coding']['value'], -0.24, places=3)  # min(-0.10, -0.24)

    def test_a_missing_facet_without_a_sibling_is_the_fit_at_the_tier_kept_in_range(self):
        new = self.tiered(4.0, coding=[{'source': 'aa.terminalbench_4', 'facet': 'execution', 'value': -0.1,
                                        'z_by_effort': dict.fromkeys(EFFORT_NAMES, -0.1)}])
        far = self.tiered(6.5, coding=[{'source': 'aa.terminalbench_4', 'facet': 'execution', 'value': 0.2,
                                        'z_by_effort': dict.fromkeys(EFFORT_NAMES, 0.2)}])
        rows, _, fits, _, _, _ = self.score(self.calibration_set({'new': new, 'far': far}))
        facet = rows['new']['axes']['coding']['facets']['faithfulness']
        self.assertEqual((facet['status'], facet['estimate']['method']), ('estimated', 'tier_fit'))
        self.assertAlmostEqual(facet['value'], -0.32, places=3)                 # (4.0 - 4.8) * 0.4
        self.assertNotIn('base_model', facet['estimate'])
        top = rows['far']['axes']['coding']['facets']['faithfulness']
        f = fits['coding.faithfulness~tier']
        self.assertEqual(top['value'], round(f['y_max'], 4))
        self.assertEqual(top['estimate']['tier_clamped_to'], f['x_max'])

    def test_a_facet_with_no_tier_stays_missing_and_coding_never_skips_missing_facet(self):
        """F1: coding = min(execution, faithfulness) never silently skips a missing facet; without an estimate or manual
        score on every required facet, the category stays None / partial so Mode B excludes it until filled."""
        exec_only = [{'source': 'aa.terminalbench_4', 'facet': 'execution', 'value': -0.35,
                      'z_by_effort': dict.fromkeys(EFFORT_NAMES, -0.35)}]
        meas = self.calibration_set({'no_tier': {'coding': exec_only}})
        rows, tier, _, _, _, _ = self.score(meas)
        self.assertEqual(tier['no_tier']['method'], 'none')
        ax = rows['no_tier']['axes']['coding']
        self.assertEqual((ax['status'], ax['missing_facets']), ('partial', ['faithfulness']))
        self.assertNotIn('faithfulness', ax['facets'])
        self.assertIsNone(ax['value'])
        self.assertEqual(ax['by_effort'], {})

    def test_a_sibling_estimate_with_too_few_points_for_a_fit_shifts_by_zero_and_says_so(self):
        meas = {'panel': {'models': [self.ms.ANCHOR]}, 'models': {
            self.ms.ANCHOR: self.tiered(4.8, coding=[
                {'source': 'arena.agent_alignment', 'facet': 'faithfulness', 'value': 0.0},
                {'source': 'aa.terminalbench_4', 'facet': 'execution', 'value': 0.0,
                 'z_by_effort': dict.fromkeys(EFFORT_NAMES, 0.0)}]),
            'claude-fable-5-1': self.tiered(3.8, coding=[
                {'source': 'arena.agent_alignment', 'facet': 'faithfulness', 'value': -0.45},
                {'source': 'aa.terminalbench_4', 'facet': 'execution', 'value': -0.5,
                 'z_by_effort': dict.fromkeys(EFFORT_NAMES, -0.5)}]),
            'claude-fable-5-2': self.tiered(4.2, coding=[
                {'source': 'aa.terminalbench_4', 'facet': 'execution', 'value': -0.3,
                 'z_by_effort': dict.fromkeys(EFFORT_NAMES, -0.3)}]),
        }}
        rows, _, fits, rejected, _, _ = self.score(meas)
        self.assertIn('coding.faithfulness~tier', rejected)
        self.assertNotIn('coding.faithfulness~tier', fits)
        facet = rows['claude-fable-5-2']['axes']['coding']['facets']['faithfulness']
        est = facet['estimate']
        self.assertEqual((facet['status'], facet['value'], est['method'], est['base_model']),
                         ('estimated', -0.45, 'sibling_shift', 'claude-fable-5-1'))
        self.assertEqual(est['shift'], 0.0)
        self.assertTrue(est['shift_basis'].startswith('shift 0: no fit,'), est['shift_basis'])

    def test_a_same_model_row_takes_its_declared_models_scores_for_the_categories_it_carries(self):
        meas = self.calibration_set({})
        for name, fields in meas['models'].items():
            t = fields['tier'][0]['value']
            fields['tool_operation'] = [{'source': 'lmarena.vision', 'facet': 'image_recognition',
                                         'value': round((t - 4.8) * 0.3, 4)}]
        meas['models']['astra'] = self.tiered(4.2, coding=[
            {'source': 'arena.agent_alignment', 'facet': 'faithfulness', 'value': -0.2},
            {'source': 'aa.terminalbench_4', 'facet': 'execution', 'value': -0.1,
             'z_by_effort': dict.fromkeys(EFFORT_NAMES, -0.1)}],
            tool_operation=[{'source': 'lmarena.vision', 'facet': 'image_recognition', 'value': -0.15}])
        meas['models']['channel'] = {}
        acc = {'models': {m: {} for m in meas['models']},
               'same_model': {'channel': {'model': 'astra', 'carries': ['coding'], 'url': 'https://openai.com/index/introducing-dots/'}}}
        rows, tier, _, _, _, _ = self.fa.Scorer(meas, acc).score()
        self.assertEqual((tier['channel']['value'], tier['channel']['method'], tier['channel']['base_model']), (4.2, 'sibling', 'astra'))
        cd = rows['channel']['axes']['coding']
        for name, value in (('faithfulness', -0.2), ('execution', -0.1)):
            est = cd['facets'][name]['estimate']
            self.assertEqual((cd['facets'][name]['value'], est['method'], est['base_model'], est['url']),
                             (value, 'same_model', 'astra', 'https://openai.com/index/introducing-dots/'), name)
        self.assertEqual(cd['value'], rows['astra']['axes']['coding']['value'])
        tool = rows['channel']['axes']['tool_operation']
        self.assertEqual((tool['status'], tool.get('flag'), tool['facets']), ('missing', 'missing', {}))
        # without the declaration the row has no tier and nothing to estimate from
        rows, tier, _, _, _, _ = self.fa.Scorer(meas, {'models': acc['models']}).score()
        self.assertEqual(tier['channel']['method'], 'none')
        self.assertEqual(rows['channel']['axes']['coding']['status'], 'missing')

    def test_shipped_grok_bot_category_scores_come_from_opus_5_5_through_same_model(self):
        refs = Path(self.fa.__file__).resolve().parents[1] / 'references'
        accounts_doc = json.loads((refs / 'accounts.json').read_text(encoding='utf-8'))
        declared = accounts_doc['same_model']['grok-bot']
        self.assertEqual((declared['model'], declared['kind'], declared['as_of'], declared['statement']),
                         ('claude-opus-5-5', 'maintainer', '2026-11-11', 'maintainer: grok-bot is Opus 5.5'))
        img_decl = accounts_doc['models']['grok-bot']['image_input_source']
        self.assertEqual((img_decl['kind'], img_decl['as_of']), ('maintainer', '2026-11-11'))
        models = json.loads((refs / 'five-axis-scores.json').read_text(encoding='utf-8'))['models']
        bot, opus = models['grok-bot'], models['claude-opus-5-5']
        self.assertEqual(bot['image_input']['status'], 'yes')
        self.assertEqual(bot['image_input']['value'], True)
        self.assertEqual(sorted(declared['carries']), ['aesthetics', 'coding', 'divergence', 'duration'])
        for axis in ('divergence', 'coding', 'aesthetics'):
            mine, theirs = bot['axes'][axis], opus['axes'][axis]
            self.assertEqual(mine['value'], theirs['value'], axis)
            self.assertEqual(set(mine['facets']), set(theirs['facets']), axis)
            for name, rec in mine['facets'].items():
                self.assertEqual((rec['value'], rec['estimate']['method'], rec['estimate']['base_model'],
                                  rec['estimate']['base_status']),
                                 (theirs['facets'][name]['value'], 'same_model', 'claude-opus-5-5',
                                  theirs['facets'][name]['status']), f'{axis}.{name}')
        self.assertEqual(bot['axes']['tool_operation']['status'], 'missing')
        self.assertEqual(bot['tier']['base_model'], 'claude-opus-5-5')
        self.assertEqual(bot['duration_multiplier']['by_effort'], opus['duration_multiplier']['by_effort'])
        self.assertTrue(all(b['method'] == 'same_model' and b['base_model'] == 'claude-opus-5-5'
                            for b in bot['duration_multiplier']['basis_by_effort'].values()))

    def test_a_later_measurement_replaces_the_estimate_on_refresh(self):
        new = self.tiered(4.0, coding=[{'source': 'aa.terminalbench_4', 'facet': 'execution', 'value': -0.2,
                                        'z_by_effort': dict.fromkeys(EFFORT_NAMES, -0.2)}])
        meas = self.calibration_set({'new': new})
        before, _, _, _, _, _ = self.score(meas)
        self.assertEqual(before['new']['axes']['coding']['facets']['faithfulness']['status'], 'estimated')
        meas['models']['new']['coding'].append(
            {'source': 'arena.agent_alignment', 'facet': 'faithfulness', 'value': -0.15})
        after, _, _, _, _, _ = self.score(meas)
        facet = after['new']['axes']['coding']['facets']['faithfulness']
        self.assertEqual((facet['status'], facet['value']), ('measured_partial', -0.15))
        self.assertNotIn('estimate', facet)


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
        meas = {'models': {'m': {'usd_per_hour': self.entries(), 'aesthetics': [
            {'source': 'designarena.website', 'facet': 'design', 'value': -1.2},
            {'source': 'lmarena.webdev', 'facet': 'design', 'value': 0.4}]}}}
        scorer = self.fa.Scorer(meas, {'models': {'m': {}}}, distrust=['arena.agent_sessions', 'designarena.website'])
        usd = self.fa.usd_community_mix(scorer, {'m': {'fields': meas['models']['m']}}, meas)
        self.assertEqual((usd['m']['value'], usd['m']['fallback']), (31.0, True))
        rows, _, _, _, _, _ = scorer.score()
        self.assertEqual(rows['m']['axes']['aesthetics']['facets']['design']['value'], 0.4)
        self.assertEqual(list(rows['m']['axes']['aesthetics']['facets']['design']['sources']), ['lmarena.webdev'])


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
        self.assertEqual(cap, {
            'lmarena.creative_writing': ('divergence', 'creative_writing'),
            'arena.agent_code': ('coding', 'execution'),
            'aa.terminalbench_4': ('coding', 'execution'),
            'aa.frontiercode_2': ('coding', 'execution'),
            'arena.agent_alignment': ('coding', 'faithfulness'),
            'lmarena.webdev': ('aesthetics', 'design'),
            'designarena.website': ('aesthetics', 'design'),
            'designarena.3d': ('aesthetics', 'design'),
            'designarena.svg': ('aesthetics', 'design'),
            'aa.ttfa': ('tool_operation', 'responsiveness'),
            'aa.mmmu_pro': ('tool_operation', 'image_recognition'),
            'lmarena.vision': ('tool_operation', 'image_recognition'),
        })
        self.assertNotIn('lmarena.agent', cap)         # Agent / Overall is not a capability scoring board

    def test_a_tab_that_was_never_captured_is_a_surfaced_missing_source(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            builder = self.ms.Builder(Path(tmp), {'models': {}})
            builder.boards()
        for src in self.ms.ARENA_CAPTURES:
            self.assertIn('not captured', builder.missing[src]['reason'], src)


if __name__ == '__main__':
    unittest.main()
