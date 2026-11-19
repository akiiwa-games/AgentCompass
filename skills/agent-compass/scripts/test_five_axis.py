"""The five-axis path (round 047, rounds 2-7): off by default; when on, the axes are the only dimension system (no tier,
vectors, route lists, harness factors or difficulty floors; round 7), quality is the continuous fit of the axis scores to
the demand with a shortfall penalty, usd merges cited sources, capacity comes down the cited ladder (community,
time-to-exhaust, vendor multiplier; an empty ladder is a data error that still prices scarcity live), every long window
charges burn x a pacing shadow price, ties go to the lower opportunity cost then the higher level, and the classifier may
answer the axis demand itself."""
import copy
from datetime import datetime, timedelta, timezone
import json
import math
import re
import shutil
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace

import numpy as np

import allocator
import data_age
import account_fleet
import axis_scores
import candidate_ranking
import ledger_snapshot
import quota_economy
import task_intake
import route_judge
import scorer_client
import usage_reading
import route_simulation as sim
from router_test_support import isolated_router_state
from test_fleet_parity import shipped_template, snapshot

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'calibration'))
import five_axis  # noqa: E402

# accounts.json's declarations the build reads (sources.effort_readings, five_axis.estimation_order)
# declared family rows (series.identity) for the gpt-6 / gpt-5.6 luna and astra rows and the claude rows of the fixtures
GPT_FAMILIES = {'rows': [{'pattern': '^gpt-(?:6|5\\.6).*luna', 'models': [], 'maker': 'openai', 'product': 'openai', 'line': 'luna', 'size': 'full'},
                         {'pattern': '^gpt', 'models': [], 'maker': 'openai', 'product': 'openai', 'line': 'astra', 'size': 'full'},
                         {'pattern': '^claude', 'models': [], 'maker': 'anthropic', 'product': 'anthropic', 'line': 'claude', 'size': 'full'}]}
READINGS = {'effort_readings': {'labels': {'non-reasoning': 'none', 'reasoning': 'default'}, 'reads': {'default': 'high'}},
            'estimation_order': {'order': list(five_axis.ESTIMATION_ORDER), 'sibling_first_up_to_distance': 1,
                                 'speed_cross_line_before_fit': True, 'evidence': 'synthetic'}}


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup, discovery=False, policy=False)


NOW = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)
PROFILE = {'work_kind': 'code_implementation', 'primary_level': 2,
           'secondary_kind': 'none', 'secondary_level': 0, 'access': 'write',
           'needs_visual': False, 'local_only': True, 'wants_fast': False,
           'task_count': '1', 'expected_hours': 1.0}
EFFORT_NAMES = ('low', 'medium', 'high', 'xhigh', 'max')


def ceiling_row(value):
    return {'value': value, 'model': 'best', 'status': 'measured', 'n': 12}


def level_targets(t1=-1.0, t2=-0.5, t3=0.0, ceiling=None):
    """One category's panel targets as five-axis-scores.json writes them (level 1 the panel's weakest, 2 the median, 3
    the panel's best; `zero` is the level-1 target; the level-3 row carries the `ceiling`, the best measured model, t3 unless
    given). tool_operation asks per facet."""
    def lv(t, name):
        return {'target': t, 'zero': t1, 'statistic': name, 'n': 12}
    return {'levels': {'1': lv(t1, 'panel minimum'), '2': lv(t2, 'panel median'),
                       '3': {**lv(t3, 'panel maximum'), 'ceiling': ceiling_row(t3 if ceiling is None else ceiling)}},
            'continuity': round(t3 - t2, 4)}


def aesthetics_targets(t1=-1.0, t2=-0.5, t3=0.0, ceiling=None):
    facet = lambda t: {'target': t, 'zero': t1, 'n': 12}
    top = lambda t: {**facet(t), 'ceiling': ceiling_row(t if ceiling is None else max(t, ceiling))}
    return {'levels': {'1': {'by_facet': {'design': facet(t1)}},
                       '2': {'by_facet': {'design': facet(t2), 'image_recognition': facet(t2)}},
                       '3': {'by_facet': {'design': top(t3), 'image_recognition': top(t3)}}},
            'continuity': round(t3 - t2, 4)}


def tool_targets(t1=-1.0, t2=-0.5, t3=0.0, ceiling=None):
    facet = lambda t: {'target': t, 'zero': t1, 'n': 12}
    top = lambda t: {**facet(t), 'ceiling': ceiling_row(t3 if ceiling is None else max(t3, ceiling))}
    return {'levels': {'1': {'by_facet': {}, 'reason': 'no ability requirement'},
                       '2': {'by_facet': {'responsiveness': facet(t2)}},
                       '3': {'by_facet': {'responsiveness': top(t2), 'image_recognition': top(t3)}}},
            'continuity': round(max(t3 - t2, 0.0), 4)}


TARGETS = {'creativity': level_targets(), 'coding': level_targets(), 'aesthetics': aesthetics_targets(),
           'tool_operation': tool_targets()}
ANCHOR = {'model': 'claude-opus-5-5', 'value': 0.0, 'scale': 'panel standard score', 'panel_n': 12, 'targets': TARGETS}


def demand(cre, coding, aes, tool):
    return {'creativity': cre, 'coding': coding, 'aesthetics': aes, 'tool_operation': tool}


def z_of(s):
    """A fixture score on the old 0..1 reading as a panel standard score: the anchor's 1.0 is 0."""
    return round(float(s) - 1.0, 4)


def cell(z, status='measured'):
    return {'value': z, 'by_effort': dict.fromkeys(EFFORT_NAMES, z), 'status': status}


# The facets the fit reads per category, as the shipped five-axis-scores.json carries them.
FIXTURE_FACETS = {'aesthetics': ('design',), 'tool_operation': ('responsiveness', 'image_recognition')}


def axes_of(scores, missing=()):
    out = {}
    for a in axis_scores.FIVE_AXES:
        if a in missing:
            out[a] = {'value': None, 'by_effort': {}, 'status': 'missing', 'flag': 'missing', 'facets': {},
                      'missing_facets': []}
            continue
        z = z_of(scores[a])
        out[a] = {**cell(z), 'facets': {f: cell(z) for f in FIXTURE_FACETS.get(a, ())}}
    return out


def row(scores=0.8, usd=5.0, missing=(), image_input='yes', dur=1.0, fast=None, price=True):
    scores = scores if isinstance(scores, dict) else dict.fromkeys(axis_scores.FIVE_AXES, scores)
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
    return {'schema': axis_scores.FIVE_AXIS_SCHEMA, 'models': rows, 'anchor': ANCHOR, 'capacity': CAPACITY,
            'effort_readings': {'default': 'high'}}


def window(usd, as_of='2026-11-10', ref=None):
    return {'unit': 'usd', 'capacity_usd': usd, 'status': 'ok', 'as_of': as_of, 'reference_model': ref}


# Community capacity per family / plan / window, as five_axis.capacity writes it (Max 20x weekly_fable left out).
CAPACITY = {'claude': {'max20': {'seven_day': window(1700.0, ref='claude-opus-5-5'),
                                 'five_hour': window(270.0, ref='claude-opus-5-5')}},
            'codex': {'pro': {'usage': window(615.0)}}}


def _synthetic_doc_ctx(doc):
    import contextlib
    from unittest.mock import patch

    @contextlib.contextmanager
    def _ctx():
        with patch.object(axis_scores, 'load_five_axis', lambda path=None: doc), \
                patch.object(axis_scores.user_scores, 'load_public', lambda path=None: {'schema_version': 2, 'scores': {}}):
            yield
    return _ctx()


class FiveAxisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.accounts = sim.fresh_accounts(shipped_template(), snapshot())
        cls.policy = {'allowed_accounts': list(cls.accounts['accounts'])}

    def usage(self, accounts, state='ample'):
        return sim.snapshot(sim.synthetic_document(accounts, NOW, sim.STATES[state]), accounts, NOW)

    def plan(self, accounts, doc, state='ample', **task):
        acc = copy.deepcopy(accounts)
        with _synthetic_doc_ctx(doc):
            profile = {**PROFILE, **task.pop('profile', {})}
            return allocator.plan({'objective': 't', 'profile': profile, **task}, self.usage(acc, state), NOW,
                                  accounts=acc, overrides=[], records=[], host_policy_config=self.policy)

    def rows_for(self, decision, model):
        return [c for c in decision['ranking'] if account_fleet.canonical(c['bundle']['model'], self.accounts) == model]

    def test_the_fit_follows_the_demanded_axes_continuously_and_no_floor_excludes(self):
        # Sol: precise coder, weak creativity. Round 7: nothing excludes it on a creative task; its fit pays for the
        # shortfall on the demanded axis instead, continuously.
        doc = document(self.accounts['models'], **{'gpt-6.1-sol': row(demand(0.2, 0.9, 0.9, 0.5))})
        coding = self.plan(self.accounts, doc, profile={'axis_demand': demand(0, 2, 0, 0)})
        creative = self.plan(self.accounts, doc, profile={'work_kind': 'creative_ideation', 'axis_demand': demand(3, 1, 0, 0), 'access': 'read-only'})
        sol_high = lambda d: next(c for c in self.rows_for(d, 'gpt-6.1-sol') if c['bundle'].get('effort') == 'high')
        for d in (coding, creative):
            self.assertFalse([e for c in d['ranking'] for e in c['excluded'] if 'floor' in e])
            self.assertFalse([c for c in d['ranking'] if c.get('below_floor')])
        self.assertEqual(sol_high(creative)['excluded'], [])
        self.assertGreater(sol_high(creative)['five_axis_fit']['creativity']['shortfall'], 0.5)
        self.assertGreater(sol_high(coding)['components']['fit'], sol_high(creative)['components']['fit'] + 0.3)
        self.assertEqual(sol_high(coding)['components']['five_axis_shortfall'], 0.0)

    def test_fit_is_the_capped_distance_from_the_panel_weakest_in_panel_sd_units(self):
        """Q7 (user, 2026-11-11): fit = sum w (min(z, c) - t1) / sum w; 0 is the panel's weakest, below the weakest is
        negative; c is the target at levels 1 and 2 and the ceiling at level 3 (owner, 2026-11-13). tool_operation level 1
        asks for nothing; level 3 is min over its facets."""
        d = demand(3, 1, 0, 2)
        targets = axis_scores.five_axis_targets(d, TARGETS)
        self.assertEqual(targets, {'creativity': {'target': 0.0, 'zero': -1.0, 'cap': 0.0},
                                   'coding': {'target': -1.0, 'zero': -1.0, 'cap': -1.0},
                                   'tool_operation': {'by_facet': {'responsiveness': {'target': -0.5, 'zero': -1.0,
                                                                                      'cap': -0.5}}}})
        values = {'creativity': -0.3, 'coding': 0.4,
                  'aesthetics': {'design': -3.0, 'image_recognition': -3.0},
                  'tool_operation': {'responsiveness': -0.8, 'image_recognition': -5.0}}
        fit, rows = axis_scores.five_axis_fit(values, d, targets)
        self.assertAlmostEqual(fit, (3 * 0.7 + 1 * 0.0 + 2 * 0.2) / 6, places=4)
        self.assertNotIn('aesthetics', rows)                 # an undemanded category does not count
        below = axis_scores.five_axis_fit({**values, 'creativity': -2.0}, d, targets)[0]
        self.assertLess(below, 0.2)
        self.assertAlmostEqual(below, (3 * -1.0 + 0 + 2 * 0.2) / 6, places=4)       # below the weakest: negative term
        # tool_operation level 1: no requirement, so it is not part of the fit at all
        self.assertEqual(axis_scores.five_axis_targets(demand(0, 0, 0, 1), TARGETS), {})
        # level 3: the weaker facet decides; a level-3 task asks of responsiveness what a level-2 task asks (declared rule)
        t3 = axis_scores.five_axis_targets(demand(0, 0, 0, 3), TARGETS)['tool_operation']['by_facet']
        t2 = axis_scores.five_axis_targets(demand(0, 0, 0, 2), TARGETS)['tool_operation']['by_facet']
        self.assertEqual(t3['responsiveness']['target'], t2['responsiveness']['target'])
        mono = axis_scores.five_axis_targets(demand(0, 0, 0, 3), {'tool_operation': tool_targets(-1.7, 0.14, 0.0)})
        self.assertEqual(mono['tool_operation']['by_facet']['responsiveness']['target'], 0.14)
        fit3, rows3 = axis_scores.five_axis_fit(values, demand(0, 0, 0, 3), {'tool_operation': {'by_facet': t3}})
        self.assertAlmostEqual(fit3, -4.0, places=4)
        self.assertEqual(rows3['tool_operation']['facets']['image_recognition']['term'], -4.0)

    def test_levels_one_and_two_stop_at_the_target_and_level_three_climbs_to_the_ceiling(self):
        """Owner 2026-11-13: routine levels pay nothing for surplus ability; a level-3 demand is capped at the category's
        ceiling (the best measured model): its fit is non-decreasing in z, strictly increasing between t3 and the
        ceiling, and the same min cuts anything above the ceiling."""
        targets = {'creativity': level_targets(-1.0, -0.5, 0.0, ceiling=0.6), 'coding': level_targets(),
                   'aesthetics': aesthetics_targets(-1.0, -0.5, 0.0, ceiling=0.6),
                   'tool_operation': tool_targets(-1.0, -0.5, 0.0, ceiling=0.6)}
        zs = [x / 10 for x in range(-15, 16)]

        def fit_at(d, t, z):
            return axis_scores.five_axis_fit(
                {'creativity': z,
                 'aesthetics': {'design': z, 'image_recognition': z},
                 'tool_operation': {'responsiveness': z, 'image_recognition': z}}, d, t)[0]
        for level in (1, 2):
            for d, key in ((demand(level, 0, 0, 0), 'creativity'),
                           (demand(0, 0, level, 0), 'aesthetics'),
                           (demand(0, 0, 0, level), 'tool_operation')):
                t = axis_scores.five_axis_targets(d, targets)
                rows = [t[key]] if key == 'creativity' else list((t.get(key) or {}).get('by_facet', {}).values())
                if not rows:
                    continue                                     # tool_operation level 1 asks for nothing
                target = max(r['target'] for r in rows)
                self.assertEqual({fit_at(d, t, z) for z in zs if z >= target}, {fit_at(d, t, target)})
        for d, key in ((demand(3, 0, 0, 0), 'creativity'),
                       (demand(0, 0, 3, 0), 'aesthetics'),
                       (demand(0, 0, 0, 3), 'tool_operation')):
            t = axis_scores.five_axis_targets(d, targets)
            fits = [fit_at(d, t, z) for z in zs]
            self.assertTrue(all(b >= a for a, b in zip(fits, fits[1:])))
            row3 = targets[key]['levels']['3']
            rows = [row3] if key == 'creativity' else list(row3['by_facet'].values())
            t3, ceiling = max(r['target'] for r in rows), min(r['ceiling']['value'] for r in rows)
            between = [z for z in zs if t3 <= z <= ceiling]
            self.assertGreater(len(between), 2)
            self.assertTrue(all(fit_at(d, t, b) > fit_at(d, t, a) for a, b in zip(between, between[1:])))
            self.assertEqual(fit_at(d, t, ceiling + 1.0), fit_at(d, t, ceiling))

    def test_a_candidate_at_or_below_t3_scores_as_before_the_ceiling(self):
        """Owner 2026-11-13: the ceiling changes only what lies above t3; any candidate with every demanded z <= t3 has
        the fit it had when the level-3 cap was t3 (a ceiling equal to t3)."""
        import random
        raised = {
            a: (tool_targets(ceiling=0.8) if a == 'tool_operation'
                else aesthetics_targets(ceiling=0.8) if a == 'aesthetics'
                else level_targets(ceiling=0.8))
            for a in TARGETS
        }
        rng = random.Random(7)
        for _ in range(200):
            d = demand(*(rng.randint(0, 3) for _ in range(4)))
            values = {a: rng.uniform(-3.0, 0.0) for a in ('creativity', 'coding')}
            img_z = rng.uniform(-3.0, 0.0)
            values['aesthetics'] = {'design': rng.uniform(-3.0, 0.0), 'image_recognition': img_z}
            values['tool_operation'] = {'responsiveness': rng.uniform(-3.0, 0.0), 'image_recognition': img_z}
            before = axis_scores.five_axis_fit(values, d, axis_scores.five_axis_targets(d, TARGETS))[0]
            after = axis_scores.five_axis_fit(values, d, axis_scores.five_axis_targets(d, raised))[0]
            self.assertEqual(before, after)

    def test_aesthetics_shares_image_recognition_at_levels_two_and_three_and_keeps_level_one_design_only(self):
        """Rule (2026-11-17): aesthetics level 1 reads design only (never excluding a model that lacks image_recognition);
        aesthetics levels 2 and 3 read min(term_design, term_image_recognition) sharing tool_operation.image_recognition,
        and exclude missing image_recognition under Mode B naming tool_operation.image_recognition."""
        shipped = axis_scores.load_five_axis()['anchor']['targets']
        t_l1 = axis_scores.five_axis_targets(demand(0, 0, 1, 0), shipped)['aesthetics']['by_facet']
        t_l2 = axis_scores.five_axis_targets(demand(0, 0, 2, 0), shipped)['aesthetics']['by_facet']
        t_l3 = axis_scores.five_axis_targets(demand(0, 0, 3, 0), shipped)['aesthetics']['by_facet']
        self.assertEqual(set(t_l1), {'design'})
        self.assertEqual(set(t_l2), {'design', 'image_recognition'})
        self.assertEqual(set(t_l3), {'design', 'image_recognition'})

        # Weak image_recognition bottlenecks aesthetics at L2 and L3, while L1 depends only on design.
        strong_design_weak_vision = {
            'creativity': 0.0, 'coding': 0.0,
            'aesthetics': {'design': 0.0, 'image_recognition': -2.0},
            'tool_operation': {'responsiveness': 0.5, 'image_recognition': -2.0},
        }
        strong_both = {
            'creativity': 0.0, 'coding': 0.0,
            'aesthetics': {'design': 0.0, 'image_recognition': 0.0},
            'tool_operation': {'responsiveness': 0.5, 'image_recognition': 0.0},
        }
        for lvl, expected_equal in ((1, True), (2, False), (3, False)):
            d = demand(0, 0, lvl, 0)
            tgts = axis_scores.five_axis_targets(d, shipped)
            f_weak = axis_scores.five_axis_fit(strong_design_weak_vision, d, tgts)[0]
            f_strong = axis_scores.five_axis_fit(strong_both, d, tgts)[0]
            if expected_equal:
                self.assertEqual(f_weak, f_strong)
            else:
                self.assertLess(f_weak, f_strong)

        # Mode B: missing image_recognition passes at aesthetics=1 and leaves automatic routing at aesthetics>=2.
        target = 'claude-sonnet-5-5'
        no_vision = row(demand(0.95, 0.95, 0.95, 0.9))
        no_vision['axes']['tool_operation']['facets'].pop('image_recognition', None)
        no_vision['axes']['tool_operation']['missing_facets'] = ['image_recognition']
        doc = document(self.accounts['models'], **{target: no_vision})
        at_l1 = self.plan(self.accounts, doc, profile={'axis_demand': demand(0, 1, 1, 0)})
        at_l2 = self.plan(self.accounts, doc, profile={'axis_demand': demand(0, 1, 2, 0)})
        self.assertFalse(any('no trusted data' in e for c in self.rows_for(at_l1, target) for e in c['excluded']))
        self.assertTrue(all(any('missing tool_operation.image_recognition' in e for e in c['excluded'])
                            for c in self.rows_for(at_l2, target)))

    def test_the_radar_stops_at_the_anchor_and_the_hover_names_the_level_three_headroom(self):
        """The radar spoke is (min(z, t3) - t1) / (t3 - t1) whatever the ceiling; a level-3 demand's credit above the
        anchor and its cap (the ceiling with the model that sets it) are display facts for the hover, computed from the
        fit rows, and only for categories demanded at level 3."""
        targets = {**TARGETS, 'creativity': level_targets(-1.0, -0.5, 0.0, ceiling=1.0),
                   'tool_operation': tool_targets(-1.0, -0.5, 0.0, ceiling=0.6)}
        values = {'creativity': 0.5, 'coding': -0.2,
                  'aesthetics': {'design': None, 'image_recognition': 0.1},
                  'tool_operation': {'responsiveness': 0.3, 'image_recognition': 0.1}}
        shares = axis_scores.five_axis_shares(values, targets)
        self.assertEqual((shares['creativity'], shares['tool_operation']), (1.0, 1.0))
        self.assertEqual(shares, axis_scores.five_axis_shares(values, TARGETS))     # the ceiling never moves a spoke
        d = demand(3, 2, 0, 3)
        rows = axis_scores.five_axis_fit(values, d, axis_scores.five_axis_targets(d, targets))[1]
        headroom = axis_scores.five_axis_headroom(rows, targets)
        self.assertEqual(set(headroom), {'creativity', 'tool_operation'})            # coding is demanded at level 2
        self.assertEqual(headroom['creativity'], {'above_anchor': 0.5, 'ceiling': 1.0, 'model': 'best'})
        name, deciding = min(rows['tool_operation']['facets'].items(), key=lambda kv: kv[1]['term'])
        tool = headroom['tool_operation']
        self.assertEqual(tool['facet'], name)
        self.assertEqual(tool['above_anchor'], round(max(0.0, min(deciding['score'], tool['ceiling'])), 4))
        t = axis_scores.five_axis_targets(d, targets)
        for z, shown in ((-0.4, 0.0), (1.4, 1.0)):       # nothing at or below the anchor; never more than the ceiling
            rows_z = axis_scores.five_axis_fit({**values, 'creativity': z}, d, t)[1]
            self.assertEqual(axis_scores.five_axis_headroom(rows_z, targets)['creativity']['above_anchor'], shown)

    def test_a_five_axis_file_of_another_schema_is_refused_with_what_to_run(self):
        """Schema /6 changed what level 3 means (the panel maximum, not the anchor): a file of any other schema fails fast with
        the schema it has and the command to run, never read with the wrong scale."""
        for schema in ('agent-compass.five-axis-scores/5', 'agent-compass.five-axis-scores/2', None):
            with self.assertRaises(ValueError) as caught:
                axis_scores.require_current_schema({'schema': schema, 'anchor': ANCHOR})
            text = str(caught.exception)
            for part in (repr(schema), axis_scores.FIVE_AXIS_SCHEMA, 'agentcompass data'):
                self.assertIn(part, text)
        axis_scores.require_current_schema({'schema': axis_scores.FIVE_AXIS_SCHEMA})        # the current one passes
        axis_scores.require_current_schema(axis_scores.load_five_axis())                    # and so does the shipped file
        import five_axis
        self.assertEqual(five_axis.SCHEMA, axis_scores.FIVE_AXIS_SCHEMA)                    # the builder writes what the reader reads

    def test_continuity_is_one_demand_level_of_the_highest_demanded_categories(self):
        targets = {**TARGETS, 'coding': level_targets(-2.0, -1.4, 0.0), 'tool_operation': tool_targets(-1.7, 0.14, 0.0)}
        self.assertEqual(axis_scores.five_axis_continuity_value(demand(1, 3, 0, 3), targets), 1.4)
        self.assertEqual(axis_scores.five_axis_continuity_value(demand(0, 0, 0, 3), targets), 0.0)    # t3 - t2 = 0
        self.assertEqual(axis_scores.five_axis_continuity_value(demand(0, 0, 0, 0), targets), 0.0)

    def test_missing_demanded_axis_leaves_automatic_routing_but_a_named_model_runs(self):
        target = 'claude-sonnet-5-5'
        doc = document(self.accounts['models'], **{target: row(demand(0.0, 0.95, 0.95, 0.9), missing=['creativity'])})
        blind = self.plan(self.accounts, doc, profile={'axis_demand': demand(2, 2, 0, 0)})
        sighted = self.plan(self.accounts, doc, profile={'axis_demand': demand(0, 2, 0, 0)})
        named = self.plan(self.accounts, doc, profile={'axis_demand': demand(2, 2, 0, 0)}, constraints={'model': target})
        self.assertTrue(all(any('no trusted data for the demanded category creativity' in e for e in c['excluded'])
                            for c in self.rows_for(blind, target)))
        self.assertFalse(any('no trusted data' in e for c in self.rows_for(sighted, target) for e in c['excluded']))
        chosen = next(c for c in named['ranking'] if c['id'] == named['choice'])
        self.assertEqual(account_fleet.canonical(chosen['bundle']['model'], self.accounts), target)
        self.assertIn('no trusted data for the demanded category creativity', ' '.join(chosen['notes']))

    def test_missing_usd_is_removed_not_defaulted(self):
        acc = copy.deepcopy(self.accounts)
        out = axis_scores.five_axis_overlay(acc, document(acc['models'], **{'claude-sonnet-5-5': row(0.8, None)}))
        info = account_fleet.model_spec(out, 'claude-sonnet-5-5')
        self.assertEqual(info['five_axis']['usd_status'], 'missing')
        self.assertNotIn('usd_per_hour', info)

    def test_unsourced_usd_is_never_priced_at_the_default(self):
        acc = copy.deepcopy(self.accounts)
        target = 'claude-sonnet-5-5'
        doc = document(acc['models'], **{target: row(0.8, None)})
        out = axis_scores.five_axis_overlay(acc, doc)
        info = account_fleet.model_spec(out, target)
        self.assertEqual(quota_economy.model_usd_per_hour(target, info), 0.0)
        self.assertEqual(quota_economy.unit_rate({'windows': {'w': {}}}, 'w', target, info), 0.0)
        auto = self.plan(acc, doc, profile={'axis_demand': demand(0, 2, 0, 0)})
        self.assertTrue(all(any('usd_per_hour has no cited source on a metered account' in e for e in c['excluded'])
                            for c in self.rows_for(auto, target)))
        named = self.plan(acc, doc, profile={'axis_demand': demand(0, 2, 0, 0)}, constraints={'model': target})
        chosen = next(c for c in named['ranking'] if c['id'] == named['choice'])
        self.assertEqual(account_fleet.canonical(chosen['bundle']['model'], acc), target)
        self.assertIn('its cost is not priced', ' '.join(chosen['notes']))

    def test_capacity_is_the_community_figure_of_the_account_plan(self):
        acc = copy.deepcopy(self.accounts)
        acc['accounts']['pi']['family'] = 'pi'      # discovery is off here; on a host it names the family
        out = axis_scores.five_axis_overlay(acc, document(acc['models']))
        spec = out['accounts']['claude']
        self.assertEqual(spec['capacity_usd'], {'seven_day': 1700.0, 'five_hour': 270.0})
        # Max 20x weekly_fable has no community value in CAPACITY here: unknown, with neither the template figure nor the burn prior.
        self.assertTrue(spec['capacity_unknown'])
        self.assertEqual(spec['capacity_source']['weekly_fable']['status'], 'missing')
        self.assertIsNone(quota_economy.capacity_usd(spec, 'weekly_fable'))
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
        self.assertNotIn('earliest_reset_hours', candidate_ranking.FIVE_AXIS_ORDER_KEYS)
        def cand(name, score=0.5, hours=1.0, usd=10.0, level=0.8, wait=0.0, place=0, reset=48.0):
            return {'id': name, 'score': score, 'available_in_hours': wait or None,
                    'expected_hours': hours, 'duration_hours': hours, 'expected_usd': usd,
                    'components': {'five_axis_level': level, 'harness_place': place, 'earliest_reset_hours': reset}}
        # Pairwise properties of the docstring's tuple order: change one key in the better direction while every
        # later key moves the other way; the changed key alone decides, and the better candidate sorts first.
        base = dict(score=0.5, hours=1.0, usd=10.0, level=0.8, wait=0.0, place=0)
        worse_later = {
            'net_score': dict(wait=3.0, hours=5.0, usd=50.0, level=0.1, place=9),
            'wait_hours': dict(hours=5.0, usd=50.0, level=0.1, place=9),
            'completion_hours': dict(usd=50.0, level=0.1, place=9),
            'usd': dict(level=0.1, place=9),
            'axis_level': dict(place=9),
            'harness_place': {},
        }
        better = {'net_score': dict(score=0.50001), 'wait_hours': dict(wait=0.0), 'completion_hours': dict(hours=0.2),
                  'usd': dict(usd=5.0), 'axis_level': dict(level=0.9), 'harness_place': dict(place=0)}
        start = {'wait_hours': dict(wait=1.0), 'harness_place': dict(place=1)}
        self.assertEqual(tuple(better), candidate_ranking.FIVE_AXIS_ORDER_KEYS)
        for key in candidate_ranking.FIVE_AXIS_ORDER_KEYS:
            with self.subTest(key=key):
                loser = cand('loser', **{**base, **start.get(key, {})})
                winner = cand('winner', **{**base, **worse_later[key], **better[key]})
                self.assertEqual(candidate_ranking.five_axis_deciding_key(winner, loser), key)
                self.assertEqual([c['id'] for c in sorted([loser, winner], key=candidate_ranking.five_axis_order)],
                                 ['winner', 'loser'])
        # Reset timing alone never reorders: it enters only through C inside N.
        self.assertEqual(candidate_ranking.five_axis_deciding_key(cand('a', reset=1.0), cand('b', reset=160.0)), 'tie')
        self.assertEqual(axis_scores.FIVE_AXIS_DEFAULTS['weights']['fit'], 1.0)
        for gone in ('overqualification_weight', 'strong_level', 'pace', 'pace_smoothing'):
            self.assertNotIn(gone, axis_scores.FIVE_AXIS_DEFAULTS, gone)
        for gone in ('five_axis_overqualification', 'five_axis_gate', 'five_axis_gate_level',
                     'five_axis_pacing', 'five_axis_wait_cost', 'five_axis_beta', 'five_axis_beta_fallback',
                     'five_axis_over_line_usd', 'five_axis_non_expiring_under_line_usd'):
            self.assertFalse(hasattr(allocator, gone), gone)

    def test_window_clock_across_reset_unstarted_and_infinite_reset(self):
        """Quota economy v3: each window is read on its own clock. `five_axis_window_recovery` gives the elapsed fraction e of
        its period (the recovery line is C e), a rolling window that starts on first use is at e = 0, and a starter
        allowance (T = inf) never advances."""
        rec = quota_economy.five_axis_window_recovery(
            {'known': True, 'full': 100.0, 'hours': 168.0, 'remaining': 70.0, 'reset_hours': 84.0}, 1700.0)
        self.assertAlmostEqual(rec['capacity_usd'], 1700.0)
        self.assertAlmostEqual(rec['used_usd'], 510.0)
        self.assertAlmostEqual(rec['elapsed_frac'], 0.5)
        self.assertAlmostEqual(rec['line_now_usd'], 850.0)
        cross = quota_economy.five_axis_window_recovery(
            {'known': True, 'full': 100.0, 'hours': 168.0, 'remaining': 40.0, 'reset_hours': 2.0}, 1704.5)
        self.assertAlmostEqual(cross['elapsed_frac'], 1 - 2 / 168)
        self.assertAlmostEqual(cross['reset_hours'], 2.0)
        unstarted = quota_economy.five_axis_window_recovery(
            {'known': True, 'full': 100.0, 'hours': 168.0, 'remaining': 100.0, 'reset_hours': 168.0, 'unstarted': True}, 2580.0)
        self.assertTrue(unstarted['unstarted'])
        self.assertEqual((unstarted['elapsed_frac'], unstarted['used_usd']), (0.0, 0.0))
        starter = quota_economy.five_axis_window_recovery(
            {'known': True, 'full': 100.0, 'hours': None, 'remaining': 80.0, 'reset_hours': float('inf')}, 50.0)
        self.assertEqual((starter['elapsed_frac'], starter['line_now_usd'], starter['reset_hours']), (0.0, 0.0, float('inf')))
        self.assertAlmostEqual(starter['used_usd'], 10.0)
        self.assertIsNone(quota_economy.five_axis_window_recovery({'known': False}, 100.0))
        # The starter allowance's clock never runs: its need probability depends on use only.
        self.assertGreater(quota_economy.five_axis_need_probability({**starter, 'lump_usd': 5.0}, starter['used_usd']), 0.0)

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
        acc = axis_scores.five_axis_overlay(acc, doc)
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
        with _synthetic_doc_ctx(doc):
            d = allocator.plan({'objective': 't', 'hours': 1.0, 'profile': {**PROFILE, 'axis_demand': demand(2, 2, 0, 0)}},
                               usage, NOW, accounts=acc, overrides=[], records=[], host_policy_config={'allowed_accounts': ['claude']})
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
            with _synthetic_doc_ctx(doc):
                return allocator.plan(
                    {'objective': 'frontier rewrite', 'expected_hours': hours,
                     'profile': {**PROFILE, 'axis_demand': demand(2, 3, 0, 0), 'expected_hours': hours}},
                    copy.deepcopy(usage), NOW, accounts=copy.deepcopy(acc),
                    overrides=[], records=[], host_policy_config={'allowed_accounts': ['claude', 'claude-2']},
                )

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
        """Round 11 item 4: when the task reads images (tool_operation 3, or needs_visual), models with public image_input == 'no' are
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
        self.assertEqual(account_fleet.canonical(chosen['bundle']['model'], self.accounts), 'claude-sonnet-5-5')
        self.assertIn('data error: image_input modality has no cited source; dispatched because it was named',
                      ' '.join(chosen['notes']))
        # Naming a model whose public data explicitly says no image input stays blocked even when named:
        blocked = self.plan(self.accounts, doc, profile={'axis_demand': demand(1, 2, 0, 3)},
                            constraints={'model': 'gpt-6.1-sol'})
        self.assertEqual(blocked['choice'], 'none')
        # Level 0-2 and no visual flag (no image input needed): neither is excluded for modality.
        text_only = self.plan(self.accounts, doc, profile={'axis_demand': demand(1, 2, 0, 2)})
        self.assertFalse(any('image input' in e or 'image_input' in e
                             for c in text_only['ranking'] for e in c['excluded']))
        # needs_visual is a required modality at any tool_operation level: the same two gates apply.
        for level in (0, 2):
            d = self.plan(self.accounts, doc, profile={'axis_demand': demand(1, 2, 0, level), 'needs_visual': True})
            self.assertTrue(all(any('model does not accept image input' in e for e in c['excluded'])
                                for c in self.rows_for(d, 'gpt-6.1-sol')))
            self.assertTrue(all(any('image_input modality has no cited source' in e for e in c['excluded'])
                                for c in self.rows_for(d, 'claude-sonnet-5-5')))
            self.assertTrue([c for c in self.rows_for(d, 'claude-opus-5-5') if not c['excluded']])

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
        self.assertEqual(account_fleet.canonical(chosen['bundle']['model'], acc), 'glm-5.3-flash-free')
        self.assertIn('capacity_unknown (quota unknown, spend unpriced); dispatched because it was named',
                      ' '.join(chosen['notes']))
        self.assertIn('local opencode.db lower-bound spend', ' '.join(chosen['notes']))
        # Tray / account_state warnings surface opencode when five_axis is enabled:
        overlaid = copy.deepcopy(acc)
        overlaid = axis_scores.five_axis_overlay(overlaid, doc)
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
        self.assertTrue(quota_economy.usd_unpriced('newcomer-9', acc['models']['newcomer-9']))
        self.assertEqual(quota_economy.model_usd_per_hour('newcomer-9', acc['models']['newcomer-9']), 0.0)
        for state in ('ample', 'claude scarce'):
            decision = allocator.plan({'objective': 't', 'profile': {**PROFILE, 'primary_level': 3}},
                                      self.usage(acc, state), NOW, accounts=copy.deepcopy(acc), overrides=[], records=[],
                                      host_policy_config=self.policy)
            rows = [c for c in decision['ranking'] if c['bundle']['model'] == 'newcomer-9']
            self.assertTrue(rows)
            self.assertTrue(all(any('usd_per_hour has no cited source' in e for e in c['excluded']) for c in rows))
            self.assertNotEqual(decision['choice'], rows[0]['id'])

    def test_every_shipped_model_carries_a_sourced_price_or_says_it_has_none(self):
        shipped = axis_scores.load_five_axis()
        for name, row in shipped['models'].items():
            usd = row['usd_per_hour']
            if usd.get('status') == 'ok':
                self.assertTrue(usd.get('sources'), name)
            else:
                self.assertIsNone(usd.get('value'), name)
                self.assertIn('no_cited_source', {e['reason'] for e in row['data_errors']}, name)

    def test_fable_5_2_reads_its_own_sessions_figure_and_converts_the_rest_from_fable_5_1(self):
        """User 2026-11-10 ("后续同系列新出，缺少实测数据的情况下，先按照同系列的全分数进行折算，不要留下任何一个默认值或者
        fallback"): every type score claude-fable-5-2 does not measure is claude-fable-5-1's (its same-series sibling) moved along
        the type score's fit on tier by the two tiers' difference. Its usd_per_hour is the figure of arena.ai's Agent sessions
        when that board lists it, else 5.1's times the declared API list price ratio over the community token mix. No
        pessimistic 0, no option-B fallback. Fast-mode pricing ($16/$80 = 2x, 2.5x speed) and its maintainer image-input
        declaration (2026-11-12) stay its own."""
        shipped = axis_scores.load_five_axis()
        fable52, fable51 = shipped['models']['claude-fable-5-2'], shipped['models']['claude-fable-5-1']
        usd = fable52['usd_per_hour']
        # Rule: a model arena.ai's Agent sessions list has its own community figure; one they do not list is converted from
        # its sibling at the price ratio; neither is ever the option-B fallback.
        self.assertFalse(usd['fallback'])
        self.assertIn(usd['kind'], ('community', 'converted'))
        if usd['kind'] == 'converted':
            self.assertEqual(usd['sources']['base']['model'], 'claude-fable-5-1')
            self.assertAlmostEqual(usd['value'], round(fable51['usd_per_hour']['value'] * usd['sources']['ratio'], 3), places=2)
            self.assertAlmostEqual(usd['sources']['ratio'], 0.8, places=4)      # $8/$40/$0.20 against $10/$50/$0.25
        else:
            self.assertEqual(usd['used'], ['arena.agent_sessions'])
            self.assertEqual(usd['value'], usd['sources']['arena.agent_sessions']['value'])      # the cited reading, nothing else
        self.assertEqual(fable52['fast_mode']['status'], 'ok')
        self.assertEqual((fable52['fast_mode']['price_multiplier'], fable52['fast_mode']['speed_multiplier']), (2.0, 2.5))
        self.assertEqual((fable52['image_input']['status'], fable52['image_input']['source']), ('yes', 'maintainer.image_input'))
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
        with _synthetic_doc_ctx(doc):
            clean = allocator.plan({'objective': 't', 'profile': {**PROFILE, 'axis_demand': demand(2, 3, 0, 0)}},
                                   usage, NOW, accounts=copy.deepcopy(acc), overrides=[], records=[],
                                   host_policy_config=self.policy)
            poisoned = allocator.plan({'objective': 't', 'profile': {**PROFILE, 'axis_demand': demand(2, 3, 0, 0)}},
                                      usage_poisoned, NOW, accounts=copy.deepcopy(acc), overrides=[],
                                      records=completed_history, host_policy_config=self.policy)
        self.assertEqual(clean['choice'], poisoned['choice'])
        self.assertEqual({c['id']: c['score'] for c in clean['ranking']},
                         {c['id']: c['score'] for c in poisoned['ranking']})

    def test_classifier_axis_demand_is_projected_deterministically_and_caller_demand_is_validated(self):
        # Classifier profiles omit axis_demand and project it deterministically from (work_kind, primary_level, secondary_kind, secondary_level).
        projected = task_intake.validate_profile(dict(PROFILE))
        self.assertEqual(projected['axis_demand'], task_intake.project_axis_demand('code_implementation', 2))
        self.assertEqual(projected['axis_demand'], demand(1, 2, 0, 1))
        # Tropical max envelope properties across all 11 atomic work kinds: monotonicity in depth, identity on 'none'/0, idempotence, symmetry.
        for kind in task_intake.WORK_KINDS:
            d1, d2, d3 = (task_intake.project_axis_demand(kind, s) for s in (1, 2, 3))
            self.assertTrue(all(d1[a] <= d2[a] <= d3[a] for a in axis_scores.FIVE_AXES), kind)
            self.assertEqual(task_intake.project_axis_demand(kind, 2, 'none', 0), d2)
            self.assertEqual(task_intake.project_axis_demand(kind, 2, kind, 2), d2)
        for k1 in ('creative_ideation', 'architecture_reasoning', 'code_implementation'):
            for k2 in ('frontend_ui', 'visual_analysis', 'live_loop'):
                self.assertEqual(task_intake.project_axis_demand(k1, 2, k2, 3),
                                 task_intake.project_axis_demand(k2, 3, k1, 2))
        # Cognitive role invariants: creative_ideation is P on creativity and 0 on coding; architecture_reasoning is P on coding and S on creativity (caps at L2);
        # live_loop holds tool_operation at 2 across depths; gui_control holds tool_operation at 3 across depths.
        self.assertEqual(task_intake.project_axis_demand('creative_ideation', 3), demand(3, 0, 1, 0))
        self.assertEqual(task_intake.project_axis_demand('architecture_reasoning', 3), demand(2, 3, 0, 0))
        self.assertEqual([task_intake.project_axis_demand('live_loop', s)['tool_operation'] for s in (1, 2, 3)], [2, 2, 2])
        self.assertEqual([task_intake.project_axis_demand('gui_control', s)['tool_operation'] for s in (1, 2, 3)], [3, 3, 3])
        # Explicit caller axis_demand (e.g. grid sweeps) is validated strictly.
        prof = task_intake.validate_profile({**PROFILE, 'axis_demand': demand(1, 2, 3, 0)})
        self.assertEqual(prof['axis_demand'], demand(1, 2, 3, 0))
        self.assertEqual(axis_scores.five_axis_demand({'profile': prof}), demand(1, 2, 3, 0))
        for bad in (demand(1, 2, 4, 0), {**demand(1, 2, 3, 0), 'economy': 1}, {'creativity': 1}, demand(True, 2, 3, 0)):
            with self.assertRaises(ValueError):
                task_intake.validate_profile({**PROFILE, 'axis_demand': bad})
        self.assertEqual(axis_scores.five_axis_demand({'profile': {**PROFILE, 'axis_demand': demand(0, 0, 0, 0)}}), demand(0, 0, 0, 0))

    def test_the_scorer_schema_asks_for_ten_observable_fields_and_never_raw_axis_demand(self):
        caps = {'routes': {'code_implementation': {}, 'other_or_mixed': {}}}
        schema = scorer_client.scorer_schema(caps)
        expected = {'work_kind', 'primary_level', 'secondary_kind', 'secondary_level', 'access',
                    'local_only', 'needs_visual', 'wants_fast', 'expected_hours', 'task_count'}
        self.assertEqual(set(schema['properties']), expected)
        self.assertEqual(set(schema['required']), expected)
        self.assertNotIn('axis_demand', schema['properties'])

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
                              profile={'work_kind': 'architecture_reasoning', 'axis_demand': demand(3, 3, 0, 0), 'access': 'read-only'})
        self.assertEqual(immediate['decision']['action'], 'start')
        self.assertFalse(immediate['decision']['needs_user_decision'])
        self.assertEqual(immediate['selected_bundle']['account'], 'codex')
        self.assertIn(immediate['decision']['better_if_waiting']['bundle'], ('opus55_xhigh', 'opus55_max'))
        # With can_wait_hours: defers iff the waiting candidate has strictly higher unrounded N = F - C.
        routine = self.plan(self.accounts, doc, state='5h-only scarce', can_wait_hours=4.0,
                            profile={'axis_demand': demand(0, 1, 0, 0)})
        routine_free = self.plan(self.accounts, doc, state='5h-only scarce', can_wait_hours=4.0,
                                 profile={'work_kind': 'architecture_reasoning', 'axis_demand': demand(0, 1, 0, 0), 'local_only': False, 'access': 'read-only'})
        frontier = self.plan(self.accounts, doc, state='5h-only scarce', can_wait_hours=4.0,
                             profile={'work_kind': 'architecture_reasoning', 'axis_demand': demand(3, 3, 0, 0), 'access': 'read-only'})
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
        with _synthetic_doc_ctx(doc):
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
            profile = {**PROFILE, 'work_kind': 'architecture_reasoning', 'axis_demand': demand(3, 3, 0, 0), 'access': 'read-only'}

            def fake_plan_stage(payload, cli_args):
                t = dict(payload['task'], profile=profile)
                if getattr(cli_args, 'can_wait_hours', None) is not None and t.get('can_wait_hours') is None:
                    t['can_wait_hours'] = cli_args.can_wait_hours
                with _synthetic_doc_ctx(doc):
                    d = allocator.plan(t, usage, NOW, accounts=acc,
                                       overrides=[], records=[], host_policy_config=self.policy)
                return d, 0

            argv = ['route_judge.py', 'roll', '--task', 'frontier proof', '--task-id', 't-f1', '--hours', '2.0',
                    '--parent-id', 'parent-1', '--new-worker', *extra_argv]
            buf = io.StringIO()
            with patch.object(sys, 'argv', argv), \
                    patch.object(route_judge, 'plan_stage', side_effect=fake_plan_stage), \
                    patch.object(usage_reading, 'usage_snapshot', return_value=usage), \
                    patch.object(route_judge.antigravity_plan, 'refresh_in_background'), \
                    patch.object(roll, 'resolve_parent', return_value=('parent-1', 'cli', 'parent-1')), \
                    patch.object(roll.registry, 'pending', return_value=[]), \
                    patch.object(roll.registry, 'reusable_workers', return_value=[]), \
                    patch.object(roll, 'can_launch_via_paseo', return_value=False), \
                    patch.object(roll, 'cli', side_effect=lambda _a, out, b, _txt: (
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


class HighFillTests(unittest.TestCase):
    """High from one-sided effort data (user 2026-11-12, five_axis.fill_high)."""

    @staticmethod
    def scorer(bundle_efforts=()):
        import five_axis
        bundles = {f'b{i}': {'model': 'm', 'effort': e} for i, e in enumerate(bundle_efforts)}
        return five_axis.Scorer({'models': {}}, {**READINGS, 'models': {}, 'accounts': {'acct': {'bundles': bundles}}})

    @staticmethod
    def facet(by, status='measured'):
        return {'value': by.get('high'), 'by_effort': dict(by), 'status': status, 'sources': {}}

    @staticmethod
    def fill(scorer, facets, panel):
        scorer.steps = scorer.effort_steps(facets, panel)
        return scorer.fill_high(facets)

    def test_bracketed_high_interpolates_in_the_models_own_effort_rank(self):
        key = ('creativity', 'creative_writing')
        facets = {'m': {key: self.facet({'low': -2.0, 'max': -1.0}, 'measured_partial')}}
        self.assertEqual(self.fill(self.scorer(), facets, []), [])
        rec = facets['m'][key]
        self.assertEqual((rec['status'], rec['by_effort']['high']), ('interpolated', -1.5))
        self.assertEqual((rec['interpolation']['from'], rec['interpolation']['to']), ({'low': -2.0}, {'max': -1.0}))
        # On a ladder low < high < xhigh < max, High sits one third of the way from Low to Max.
        facets = {'m': {key: self.facet({'low': 0.0, 'max': 3.0})}}
        self.fill(self.scorer(('low', 'high', 'xhigh', 'max')), facets, [])
        self.assertEqual(facets['m'][key]['by_effort']['high'], 1.0)
        # A ladder lacking an endpoint falls back to low < medium < high < xhigh < max.
        facets = {'m': {key: self.facet({'low': 0.0, 'max': 4.0})}}
        self.fill(self.scorer(('high', 'max')), facets, [])
        self.assertEqual(facets['m'][key]['by_effort']['high'], 2.0)

    def test_one_sided_high_takes_the_panel_median_own_step(self):
        ability, resp = ('coding', 'execution'), ('tool_operation', 'responsiveness')
        facets = {'p1': {ability: self.facet({'high': 0.0, 'max': 0.2}), resp: self.facet({'high': 1.0, 'max': -1.0})},
                  'p2': {ability: self.facet({'high': 0.0, 'max': 0.4}), resp: self.facet({'high': 2.0, 'max': -1.0})},
                  'p3': {ability: self.facet({'high': 0.0, 'max': 0.3}), resp: self.facet({'high': 0.5, 'max': 0.0})},
                  'm': {ability: self.facet({'max': -1.0}), resp: self.facet({'max': -2.0})}}
        self.assertEqual(self.fill(self.scorer(), facets, ['p1', 'p2', 'p3']), [])
        rec = facets['m'][ability]
        self.assertEqual((rec['status'], rec['by_effort']), ('estimated', {'high': -1.3, 'max': -1.0}))
        self.assertEqual(rec['estimate'], {'method': 'panel_effort_step', 'from_effort': 'max', 'from_value': -1.0,
                                           'step': -0.3, 'n': 3})
        self.assertEqual(facets['m'][resp]['by_effort']['high'], 0.0)        # responsiveness: faster than Max (step 2.0)
        for p in ('p1', 'p2', 'p3'):                                          # measured High is never overwritten
            self.assertEqual(facets[p][ability]['status'], 'measured')

    def test_one_sided_high_is_bounded_by_the_measured_effort_and_refused_under_two_panel_models(self):
        """Rule (owner 2026-11-18, `user_scores.bound_along_effort`): a filled High never inverts the facet's measured
        efforts and never moves one; a panel step with the wrong sign leaves High at the measured neighbour's value."""
        key = ('coding', 'execution')
        facets = {'p1': {key: self.facet({'high': 0.5, 'max': 0.2})}, 'p2': {key: self.facet({'high': 0.4, 'max': 0.3})},
                  'm': {key: self.facet({'max': -1.0}, 'measured_partial')}}
        self.assertEqual(self.fill(self.scorer(), facets, ['p1', 'p2']), [])
        rec = facets['m'][key]
        self.assertEqual(rec['by_effort'], {'high': -1.0, 'max': -1.0})
        self.assertEqual(rec['estimate']['bounded_from'], -0.8)
        refused = self.fill(self.scorer(), {'p1': {key: self.facet({'high': 0.0, 'max': 0.2})},
                                            'm': {key: self.facet({'max': -1.0})}}, ['p1'])
        self.assertEqual([(r['model'], r['effort'], r['from']) for r in refused], [('m', 'high', 'max')])

    def test_targets_read_measured_and_interpolated_high_only(self):
        """User 2026-11-12: panel targets read measured High rows; own-curve interpolation counts, an estimate never does."""
        import five_axis
        import measurements
        key = ('creativity', 'creative_writing')

        def row(value, status):
            facet = {'value': value, 'by_effort': {'high': value}, 'status': status}
            axes = {a: {} for a in five_axis.AXES}
            axes['creativity'] = {'value': value, 'status': status, 'facets': {'creative_writing': facet}}
            return {'axes': axes}
        rows = {measurements.ANCHOR: row(0.0, 'measured'), 'p1': row(-1.0, 'measured_partial'),
                'p2': row(-2.0, 'interpolated'), 'p3': row(-9.0, 'estimated')}
        targets = self.scorer().targets(rows, list(rows))['creativity']
        self.assertEqual((targets['levels']['1']['target'], targets['levels']['2']['target'], targets['levels']['2']['n']),
                         (-2.0, -1.0, 3))
        self.assertEqual(targets['facets'][key[1]]['levels']['1']['n'], 3)

    def test_the_level_three_ceiling_is_the_best_measured_high_value_of_any_catalogued_model(self):
        """Owner 2026-11-13: the ceiling is computed from the data: the highest measured or interpolated High value among
        all catalogued models (a model outside the panel included), never an estimate; t3 is the panel's own maximum."""
        import five_axis
        import measurements

        def row(value, status, tool=None):
            facet = {'value': value, 'by_effort': {'high': value}, 'status': status}
            axes = {a: {} for a in five_axis.AXES}
            axes['creativity'] = {'value': value, 'status': status, 'facets': {'creative_writing': facet}}
            tool_facet = {'value': tool if tool is not None else value, 'status': status}
            axes['tool_operation'] = {'facets': {f: dict(tool_facet) for f in five_axis.AXES['tool_operation']}}
            return {'axes': axes}
        rng = np.random.default_rng(3)
        panel_vals = sorted(rng.uniform(-2.0, 0.5, 4).round(4))
        rows = {measurements.ANCHOR: row(0.0, 'measured'),
                **{f'p{i}': row(float(v), 'measured') for i, v in enumerate(panel_vals)},
                'outside': row(float(max(panel_vals)) + 0.7, 'interpolated'),
                'guess': row(float(max(panel_vals)) + 5.0, 'estimated')}
        panel = [measurements.ANCHOR, *(f'p{i}' for i in range(len(panel_vals)))]
        targets = self.scorer().targets(rows, panel)
        best = max((r['axes']['creativity']['value'], m) for m, r in rows.items()
                   if r['axes']['creativity']['status'] in five_axis.TARGET_STATUSES)
        for lv in (targets['creativity']['levels']['3'], targets['creativity']['facets']['creative_writing']['levels']['3'],
                   *targets['aesthetics']['levels']['3']['by_facet'].values(),
                   *targets['tool_operation']['levels']['3']['by_facet'].values()):
            self.assertEqual((lv['ceiling']['value'], lv['ceiling']['model']), (round(best[0], 4), best[1]))
            self.assertEqual(lv['ceiling']['status'], 'interpolated')
            self.assertGreaterEqual(lv['ceiling']['value'], lv['target'])
        panel_best = max(float(v) for v in (0.0, *panel_vals))          # the panel's own maximum, not the outside model's
        self.assertEqual(targets['creativity']['levels']['3']['target'], round(panel_best, 4))
        self.assertLess(targets['creativity']['levels']['3']['target'], targets['creativity']['levels']['3']['ceiling']['value'])
        for level in ('1', '2'):
            self.assertNotIn('ceiling', targets['creativity']['levels'][level])

    @staticmethod
    def _level_rows(values):
        """{model: row} of creativity and tool_operation High values, each facet of a model at the model's value."""
        import five_axis

        def row(value, status='measured'):
            facet = {'value': value, 'by_effort': {'high': value}, 'status': status}
            axes = {a: {} for a in five_axis.AXES}
            axes['creativity'] = {'value': value, 'status': status, 'facets': {'creative_writing': dict(facet)}}
            axes['tool_operation'] = {'facets': {f: dict(facet) for f in five_axis.AXES['tool_operation']}}
            return {'axes': axes}
        return {m: row(v) for m, v in values.items()}

    def test_level_3_is_the_panel_maximum_not_the_anchor(self):
        """Owner 2026-11-18 (five_axis.targets): a facet's level scale is the panel's minimum, median and maximum of the measured
        High values; the anchor is the origin of z and is level 3 only where it is the panel's best. Computed here from the
        panel the test builds, so no output is pinned."""
        import measurements
        import user_scores
        rng = np.random.default_rng(11)
        values = {f'p{i}': float(v) for i, v in enumerate(rng.uniform(-2.0, 1.5, 7).round(4))}
        values[measurements.ANCHOR] = -0.3                    # below the panel's best on purpose
        values['outside'] = max(values.values()) + 1.0        # catalogued, not in the panel: raises the ceiling only
        panel = [m for m in values if m != 'outside']
        rows = self._level_rows(values)
        targets = self.scorer().targets(rows, panel)
        in_panel = [values[m] for m in panel]
        best_model = max(panel, key=lambda m: (values[m], m))
        for name, levels in (('creativity', targets['creativity']['levels']),
                             ('creative_writing', targets['creativity']['facets']['creative_writing']['levels']),
                             ('image_recognition', targets['tool_operation']['facets']['image_recognition']['levels'])):
            self.assertEqual((levels['1']['target'], levels['2']['target'], levels['3']['target']),
                             (round(min(in_panel), 4), round(float(np.median(in_panel)), 4), round(max(in_panel), 4)), name)
            self.assertEqual(levels['3']['statistic'], 'panel maximum')
            self.assertEqual(levels['3']['model'], best_model)
            self.assertGreater(levels['3']['ceiling']['value'], levels['3']['target'])   # the outside model raises the cap only
            triad = tuple(levels[k]['target'] for k in '123')
            self.assertLess(user_scores.z_to_level(-0.3, *triad), 3.0)       # the anchor is not level 3 here
            self.assertGreater(user_scores.z_to_level(values['outside'], *triad), 3.0)   # a level above 3, not clamped
        # continuity is t3 - t2 of the category
        self.assertEqual(targets['creativity']['continuity'],
                         round(targets['creativity']['levels']['3']['target'] - targets['creativity']['levels']['2']['target'], 4))

    def test_responsiveness_level_3_task_asks_what_level_2_asks_but_its_scale_ends_at_the_panel_maximum(self):
        """Owner 2026-11-18 (LEVEL3_ASKS_LEVEL2): a tool_operation level-3 task adds image recognition to the responsiveness a
        level-2 task asks; the facet's own scale is (min, median, maximum) like every other."""
        import measurements
        rng = np.random.default_rng(5)
        values = {f'p{i}': float(v) for i, v in enumerate(rng.uniform(-2.0, 1.5, 6).round(4))}
        values[measurements.ANCHOR] = 0.0
        targets = self.scorer().targets(self._level_rows(values), list(values))['tool_operation']
        asks = lambda level: targets['levels'][level]['by_facet']
        self.assertEqual(asks('3')['responsiveness']['target'], asks('2')['responsiveness']['target'])
        scale = targets['facets']['responsiveness']['levels']
        self.assertGreater(scale['3']['target'], scale['2']['target'])
        self.assertEqual(scale['3']['target'], round(max(values.values()), 4))
        self.assertEqual(asks('3')['image_recognition']['target'], targets['facets']['image_recognition']['levels']['3']['target'])
        # continuity reads what the level-3 task asks of each facet: image recognition only
        image = targets['facets']['image_recognition']['levels']
        self.assertEqual(targets['continuity'], round(image['3']['target'] - image['2']['target'], 4))

    def test_a_panel_without_three_different_level_targets_is_a_build_error_naming_the_facet(self):
        """Owner 2026-11-18: levels 2 and 3 coincide only when the panel's median is its maximum; nothing is written then
        and there is nothing to accept (five_axis.check_scales)."""
        import five_axis
        import measurements
        flat = {f'p{i}': 0.5 for i in range(5)}
        flat[measurements.ANCHOR] = 0.5
        targets = self.scorer().targets(self._level_rows(flat), list(flat))
        with self.assertRaises(ValueError) as caught:
            five_axis.check_scales(targets)
        self.assertIn('degenerate level scale on creativity', str(caught.exception))
        # median equal to the maximum: more than half of the panel at the top
        top = {f'p{i}': 1.0 for i in range(4)}
        top.update({'low1': -1.0, measurements.ANCHOR: 1.0, 'low2': -2.0})
        targets = self.scorer().targets(self._level_rows(top), list(top))
        with self.assertRaises(ValueError) as caught:
            five_axis.check_scales(targets)
        self.assertIn('degenerate level scale', str(caught.exception))
        spread = {f'p{i}': float(i) for i in range(6)}
        spread[measurements.ANCHOR] = 2.5
        five_axis.check_scales(self.scorer().targets(self._level_rows(spread), list(spread)))      # a proper panel passes

    def test_shipped_targets_ignore_the_estimated_grok_4_7_image_recognition(self):
        shipped = axis_scores.load_five_axis()
        grok = shipped['models']['grok-4.7']['axes']['tool_operation']['facets']['image_recognition']
        self.assertEqual(grok['estimate']['method'], 'panel_effort_step')
        level2 = shipped['anchor']['targets']['tool_operation']['facets']['image_recognition']['levels']['2']
        # Rule (five_axis.Scorer.targets): the level-2 target is the median of the panel's measured High values, and an
        # estimated panel member (grok-4.7 here) is not counted, so n is the panel's size less its estimated rows.
        import statistics
        import five_axis
        cells = {m: shipped['models'][m]['axes']['tool_operation']['facets']['image_recognition']
                 for m in shipped['anchor']['panel']}
        counted = [c['value'] for c in cells.values() if c['status'] in five_axis.TARGET_STATUSES]
        self.assertLess(len(counted), len(cells))
        self.assertEqual((level2['target'], level2['n']), (round(statistics.median(counted), 4), len(counted)))

    def test_shipped_glm_5_3_and_deepseek_v4_pro_have_high(self):
        models = axis_scores.load_five_axis()['models']
        glm = models['glm-5.3']['axes']
        self.assertEqual(glm['creativity']['facets']['creative_writing']['status'], 'interpolated')
        # Rule (fill_high): High between the bracketing efforts is linear in effort rank on the model's ladder (an ordered
        # subset of EFFORTS that holds both efforts and High, else EFFORTS), so the weight is the rank distance of High
        # from the lower effort over that of the upper effort, computed here, not read back from the data.
        import five_axis
        fill = glm['creativity']['facets']['creative_writing']['interpolation']
        (low_effort, low), = fill['from'].items()
        (high_effort, high), = fill['to'].items()
        ladder = fill['ladder']
        self.assertEqual(ladder, [e for e in five_axis.EFFORTS if e in ladder])
        self.assertTrue({low_effort, 'high', high_effort} <= set(ladder) or ladder == list(five_axis.EFFORTS))
        weight = (ladder.index('high') - ladder.index(low_effort)) / (ladder.index(high_effort) - ladder.index(low_effort))
        self.assertEqual(fill['weight'], round(weight, 4))
        self.assertEqual(glm['creativity']['by_effort']['high'], round(low + weight * (high - low), 4))
        ds = models['deepseek-v4-pro']['axes']['tool_operation']['facets']['responsiveness']
        self.assertIn('high', ds['by_effort'])
        if ds['status'] == 'estimated' and ds['measured_efforts']:   # one-sided readings: High from the panel step (fill_high)
            self.assertEqual(ds['estimate']['method'], 'panel_effort_step')
        self.assertGreaterEqual(ds['by_effort']['high'], ds['by_effort']['max'])    # responsiveness never rises
        ex = models['deepseek-v4-pro']['axes']['coding']['facets']['execution']
        self.assertLess(ex['by_effort']['high'], ex['by_effort']['max'])


class AnchorRelativeTests(unittest.TestCase):
    """Taxonomy R2 (2026-11-11): values are panel standard scores relative to the anchor at High, panel targets move
    nothing when a new non-panel model appears, structural/statistical checks guard raw listings, facet-on-tier fits
    report skill without gating, and local flags and samples are not sources."""

    @classmethod
    def setUpClass(cls):
        import five_axis
        import measurements
        import sources
        import tiers
        cls.fa, cls.ms, cls.tiers = five_axis, measurements, tiers
        cls.anchors = tiers.panel(sources.accounts())[1]

    def panel_models(self):
        anchors = list(self.anchors)
        extras = [f'p{i}' for i in range(max(4, self.ms.MIN_PANEL + 2 - len(anchors)))]
        return anchors + extras

    def test_panel_standard_scores_put_anchor_at_zero_and_reject_unanchored_or_thin_boards(self):
        members = self.panel_models()
        b = self.ms.Builder.__new__(self.ms.Builder)
        b.panel_ids, b.members = self.anchors, members
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

    def test_a_declared_member_missing_from_a_core_board_is_a_named_error(self):
        """Rule (measurements.py, owner 2026-11-18): the panel is accounts.json calibration_panel.members; a member a core
        board does not list is an error naming both, never a smaller panel; the shipped declaration holds on the boards."""
        import sources
        members = self.panel_models()
        b = self.ms.Builder.__new__(self.ms.Builder)
        b.panel_ids, b.members = self.anchors, members
        b.source_rows = {'lmarena.webdev': {'file': 'f'}}
        b.missing, b.manifest = {}, {}
        board = {m: 1500.0 - 25.0 * i for i, m in enumerate(members)}
        self.assertEqual(b.panel({'lmarena.webdev': board})[0], sorted(members))
        lost = members[-1]
        with self.assertRaisesRegex(ValueError, f'{lost}.*lmarena.webdev'):
            b.panel({'lmarena.webdev': {m: v for m, v in board.items() if m != lost}})
        declared = sources.accounts()['calibration_panel']
        self.assertTrue(set(declared['models']) <= set(declared['members']))
        refs = Path(__file__).resolve().parents[1] / 'references'
        shipped = json.loads((refs / 'five-axis-scores.json').read_text(encoding='utf-8'))
        self.assertEqual(sorted(shipped['anchor']['panel']), sorted(declared['members']))

    def test_structural_and_statistical_checks_drop_bad_listings_and_flag_non_monotone_high(self):
        b = self.ms.Builder.__new__(self.ms.Builder)
        b.acc = {'models': {'m_bad_tag': {}, 'm_range': {}, 'm_dup1': {}, 'm_dup2': {}, 'm_stat': {}, 'm_ok': {}}, 'families': {'rows': []}}
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
        rows, _, _, _, _, _ = self.fa.Scorer(meas, {**READINGS, 'models': {self.ms.ANCHOR: {}, 'm1': {}}, 'families': {'rows': []}}).score()
        ex = rows['m1']['axes']['coding']['facets']['execution']
        self.assertEqual(ex['status'], 'measured')
        self.assertEqual(ex['sources']['arena.agent_code']['effort_shift'], 'aa')
        # faithfulness has no AA board: single-variant listing stays flat with status measured_partial
        fa = rows['m1']['axes']['coding']['facets']['faithfulness']
        self.assertEqual(fa['status'], 'measured_partial')
        self.assertEqual(fa['sources']['arena.agent_alignment']['effort_shift'], 'none')
        self.assertEqual(rows['m1']['axes']['coding']['status'], 'measured_partial')

    def test_a_stale_capability_source_is_a_surfaced_yellow_error(self):
        """Advisor taxonomy report, bug 4: staleness is checked for every capability source, not only capacity. The scores
        file holds the date; the refresh's report ages it on the day it runs (data_age)."""
        import refresh
        src = next(iter(self.ms.CAPABILITY_SOURCES))
        dated = {src: {'kind': 'benchmark', 'as_of': '2026-08-01'},
                 'aa.price': {'kind': 'vendor', 'as_of': '2026-08-01'},                       # not a capability source
                 'metr.x': {'missing': True, 'reason': 'not used'}}
        now = datetime(2026, 11, 9, tzinfo=timezone.utc)
        rows, worst = refresh.staleness({'source_dates': dated}, now)
        by = {r['what']: r for r in rows}
        self.assertEqual((by[src]['status'], by[src]['age_days']), ('stale', 100))
        self.assertIn('stale capability source', by[src]['note'])
        self.assertEqual(by['aa.price']['status'], 'stale')
        self.assertNotIn('capability', by['aa.price']['note'])
        self.assertEqual((by['metr.x']['status'], worst), ('unused', 'stale'))
        self.assertEqual(refresh.staleness({'source_dates': dated}, datetime(2026, 8, 10, tzinfo=timezone.utc))[1], 'ok')

    def test_a_new_non_panel_top_model_moves_no_panel_score_and_no_target(self):
        members = self.panel_models()
        raw = {'lmarena.creative_writing': {m: 1500.0 - 20.0 * i for i, m in enumerate(members)}}
        b1 = self.ms.Builder.__new__(self.ms.Builder)
        b1.source_rows, b1.missing, b1.manifest = {'lmarena.creative_writing': {'file': 'f'}}, {}, {}
        b1.panel_ids, b1.members = self.anchors, members
        panel1, _, scale1 = b1.panel(raw)
        grown = {'lmarena.creative_writing': {**raw['lmarena.creative_writing'], 'newtop': 1700.0}}  # not on all core boards if added to one
        # Add a second core board where newtop is absent so newtop is outside the panel
        raw2 = {**raw, 'arena.agent_code': {m: 50.0 - 2.0 * i for i, m in enumerate(members)}}
        # newtop is listed on every core board: the declared panel (owner 2026-11-18) still does not take it in
        grown2 = {'lmarena.creative_writing': {**raw2['lmarena.creative_writing'], 'newtop': 1700.0},
                  'arena.agent_code': {**raw2['arena.agent_code'], 'newtop': 60.0}}
        for d in (b1,):
            d.source_rows['arena.agent_code'] = {'file': 'f'}
        b_before = self.ms.Builder.__new__(self.ms.Builder)
        b_before.source_rows = {s: {'file': 'f'} for s in raw2}
        b_before.missing, b_before.manifest = {}, {}
        b_before.panel_ids, b_before.members = self.anchors, members
        panel_a, _, scale_a = b_before.panel(raw2)

        b_after = self.ms.Builder.__new__(self.ms.Builder)
        b_after.source_rows = {s: {'file': 'f'} for s in grown2}
        b_after.missing, b_after.manifest = {}, {}
        b_after.panel_ids, b_after.members = self.anchors, members
        panel_b, _, scale_b = b_after.panel(grown2)

        self.assertEqual(panel_a, panel_b)
        self.assertEqual(scale_a, scale_b)
        # And Scorer targets depend only on panel models: adding newtop outside the panel moves no target.
        def build_meas(extra_models):
            sc = scale_a['lmarena.creative_writing']
            models = {m: {'creativity': [{'source': 'lmarena.creative_writing', 'facet': 'creative_writing',
                                          'value': round((raw2['lmarena.creative_writing'][m] - sc['anchor_raw']) / sc['sd'], 4)}]}
                      for m in members}
            models.update(extra_models)
            return {'panel': {'models': panel_a}, 'models': models}
        _, _, _, _, _, targets_before = self.score(build_meas({}))
        _, _, _, _, _, targets_after = self.score(build_meas({
            'newtop': {'creativity': [{'source': 'lmarena.creative_writing', 'facet': 'creative_writing', 'value': 3.5}]}
        }))

        def without_ceiling(node):
            if isinstance(node, dict):
                return {k: without_ceiling(v) for k, v in node.items() if k != 'ceiling'}
            return node
        self.assertEqual(without_ceiling(targets_before), without_ceiling(targets_after))
        # The level-3 ceiling is not a panel statistic: a measured model above every other raises it (owner, 2026-11-13).
        before_c = targets_before['creativity']['levels']['3']['ceiling']
        after_c = targets_after['creativity']['levels']['3']['ceiling']
        self.assertEqual(after_c['model'], 'newtop')
        self.assertGreater(after_c['value'], before_c['value'])

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
        # targets are non-decreasing from L1 -> L2 -> L3, including tool_operation.responsiveness (a level-3 task asks what level 2 asks)
        targets = scores['anchor']['targets']
        for axis in ('creativity', 'coding'):
            lv = targets[axis]['levels']
            self.assertLessEqual(lv['1']['target'], lv['2']['target'], axis)
            self.assertLessEqual(lv['2']['target'], lv['3']['target'], axis)
            self.assertEqual(lv['1']['zero'], lv['1']['target'], axis)
        des = targets['aesthetics']['levels']
        self.assertLessEqual(des['1']['by_facet']['design']['target'], des['2']['by_facet']['design']['target'])
        self.assertLessEqual(des['2']['by_facet']['design']['target'], des['3']['by_facet']['design']['target'])
        self.assertLessEqual(des['2']['by_facet']['image_recognition']['target'],
                             des['3']['by_facet']['image_recognition']['target'])
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
        # The fable rows are relatives through this catalog's declared family row (series.identity: maker, line, size).
        families = {'rows': [{'pattern': '^claude-fable', 'models': ['claude-fable-5-2'], 'maker': 'anthropic', 'product': 'anthropic',
                              'line': 'fable', 'size': 'full'}]}
        # their declared versions (accounts.json series_versions): one version apart, a sibling first (estimation_order)
        versions = {'series_versions': {'versions': {'claude-fable-5-1': 5.1, 'claude-fable-5-2': 5.2}}}
        return self.fa.Scorer(meas, {**READINGS, **versions, 'models': {m: {} for m in meas['models']},
                                     'families': families}).score()

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

    def test_responsiveness_without_its_own_line_takes_the_median_of_the_makers_other_lines(self):
        """Rule (Scorer.cross_line, owner 2026-11-19): a missing ability facet is estimated from a relative of the same maker
        and size on another line (up to the declared distance); a missing responsiveness with no near relative of its own
        line takes the maker's other lines: here no line pair shares two same-version readings, so the median of the
        other lines' relatives (method cross_line_median)."""
        resp_entry = lambda z: [{'source': 'aa.ttfa', 'facet': 'responsiveness', 'value': z,  # noqa: E731
                                 'z_by_effort': dict.fromkeys(EFFORT_NAMES, z)}]
        meas = self.calibration_set({
            'acme-a-1': self.tiered(4.2, coding=[{'source': 'arena.agent_alignment', 'facet': 'faithfulness', 'value': -0.2}],
                                    tool_operation=resp_entry(0.5)),
            'acme-b-1': self.tiered(4.0)})
        fams = {'rows': [{'pattern': f'^acme-{x}', 'models': [], 'maker': 'acme', 'product': 'acme', 'line': x, 'size': 'full'} for x in 'ab']}
        versions = {'series_versions': {'versions': {'acme-a-1': 1, 'acme-b-1': 1}}}
        rows, _, _, _, _, _ = self.fa.Scorer(meas, {**READINGS, **versions, 'models': {m: {} for m in meas['models']},
                                                    'families': fams}).score()
        faith = rows['acme-b-1']['axes']['coding']['facets']['faithfulness']['estimate']
        self.assertEqual((faith['method'], faith['base_model']), ('sibling_shift', 'acme-a-1'))
        resp = rows['acme-b-1']['axes']['tool_operation']['facets']['responsiveness']
        self.assertEqual((resp['status'], resp['estimate']['method'], resp['estimate']['base_model']),
                         ('estimated', 'cross_line_median', 'acme-a-1'))
        self.assertEqual(resp['estimate']['lines']['acme-a-1']['n'], 0)

    def test_responsiveness_moves_by_the_lines_median_step_where_two_same_version_pairs_read_both(self):
        """Rule (Scorer.cross_line, owner 2026-11-19): where the relative's line and the row's own line are both measured on
        at least two same-version pairs, the relative's value plus the median line-to-line difference (method
        cross_line_extrapolation, n and spread shown); never the relative's value copied. acme-b-3, unread, puts the own
        line's nearest reading (acme-b-2) two versions away, past the declared distance of 1."""
        resp_entry = lambda z: [{'source': 'aa.ttfa', 'facet': 'responsiveness', 'value': z,  # noqa: E731
                                 'z_by_effort': dict.fromkeys(EFFORT_NAMES, z)}]
        meas = self.calibration_set({
            'acme-a-1': self.tiered(4.0, tool_operation=resp_entry(0.5)), 'acme-b-1': self.tiered(4.0, tool_operation=resp_entry(0.2)),
            'acme-a-2': self.tiered(4.0, tool_operation=resp_entry(0.7)), 'acme-b-2': self.tiered(4.0, tool_operation=resp_entry(0.3)),
            'acme-a-3': self.tiered(4.0, tool_operation=resp_entry(0.6)), 'acme-b-3': self.tiered(4.0),
            'acme-a-4': self.tiered(4.0, tool_operation=resp_entry(0.9)), 'acme-b-4': self.tiered(4.0)})
        fams = {'rows': [{'pattern': f'^acme-{x}', 'models': [], 'maker': 'acme', 'product': 'acme', 'line': x, 'size': 'full'} for x in 'ab']}
        versions = {'series_versions': {'versions': {m: int(m[-1]) for m in meas['models'] if m.startswith('acme')}}}
        rows, _, _, _, _, _ = self.fa.Scorer(meas, {**READINGS, **versions, 'models': {m: {} for m in meas['models']},
                                                    'families': fams}).score()
        resp = rows['acme-b-4']['axes']['tool_operation']['facets']['responsiveness']
        line = resp['estimate']['lines']['acme-a-4']
        self.assertEqual((resp['estimate']['method'], resp['estimate']['base_model'], line['n'], line['spread']),
                         ('cross_line_extrapolation', ['acme-a-4'], 2, [-0.4, -0.3]))
        self.assertAlmostEqual(resp['value'], 0.9 + float(np.median([-0.3, -0.4])), places=4)

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
        # astra's declared family row; the channel takes astra's identity through the declaration (series.identity)
        astra = {'rows': [{'pattern': '^astra', 'models': [], 'maker': 'openai', 'product': 'openai', 'line': 'astra', 'size': 'full'}]}
        acc = {**READINGS, 'models': {m: {} for m in meas['models']}, 'families': astra,
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
        rows, tier, _, _, _, _ = self.fa.Scorer(meas, {**READINGS, 'models': acc['models'], 'families': acc['families']}).score()
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
        self.assertEqual(sorted(declared['carries']), ['aesthetics', 'coding', 'creativity', 'duration', 'usd_per_hour'])
        for axis in ('creativity', 'coding', 'aesthetics'):
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

    def test_a_measured_none_stays_and_the_estimate_moves_to_it(self):
        """Rule (Scorer.score, round 6 item 5): a facet measured only at `none` is estimated at the other efforts and keeps
        its measured `none`; where the two invert, the estimate moves, never the measurement."""
        exe = lambda z: [{'source': 'aa.terminalbench_4', 'facet': 'execution', 'value': z,       # noqa: E731
                          'z_by_effort': dict.fromkeys(EFFORT_NAMES, z)}]
        off = [{'source': 'aa.terminalbench_4', 'facet': 'execution', 'value': 0.9, 'z_by_effort': {'none': 0.9}}]
        meas = self.calibration_set({'claude-fable-5-1': self.tiered(4.0, coding=exe(-0.3)),
                                     'claude-fable-5-2': self.tiered(4.0, coding=off)})
        rows, _, _, _, _, _ = self.score(meas)
        facet = rows['claude-fable-5-2']['axes']['coding']['facets']['execution']
        self.assertEqual((facet['status'], facet['estimate']['base_model'], facet['measured_efforts'], facet['by_effort']['none']),
                         ('estimated', 'claude-fable-5-1', ['none'], 0.9))
        self.assertTrue(all(v >= 0.9 for v in facet['by_effort'].values()), facet['by_effort'])

    def test_a_reading_off_the_measured_ladder_is_bounded_on_the_facet_not_on_one_board(self):
        """Rule (Scorer.facet_value, round 6 item 5): a board's `none` reading enters the facet as listed (its distance
        from that board's High); one board's bound does not move it, the facet's bound against the facet's own measured
        readings does (here to the facet's Low, not to the one board's)."""
        sc = self.fa.Scorer.__new__(self.fa.Scorer)
        key = ('coding', 'execution')
        recs = {'aa.terminalbench_4': {'by_effort': {'none': -1.0, 'low': -1.0, 'high': 0.0}, 'off_ladder': {'none': -0.5},
                                       'bound_moved': ['none']},
                'arena.agent_code': {'by_effort': {'low': -0.2, 'high': 0.0}}}
        rec = sc.facet_value(key, recs)
        self.assertEqual((rec['by_effort'], rec['measured_efforts']), ({'none': -0.5, 'low': -0.6, 'high': 0.0},
                                                                       ['none', 'low', 'high']))
        self.assertEqual(self.fa.bound_curve(rec['by_effort'], True), {'none': -0.6, 'low': -0.6, 'high': 0.0})


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
        scorer = self.fa.Scorer(meas, {**READINGS, 'models': {'m': {}}, 'families': {'rows': []}}, distrust=['arena.agent_sessions', 'designarena.website'])
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
        acc = {**READINGS, 'models': {m: {} for m in models}, 'aliases': {}, 'families': GPT_FAMILIES, **acc_extra}
        scorer = self.fa.Scorer({'models': {m: r['fields'] for m, r in rows.items()}}, acc)
        tier = {m: {'value': None, 'method': 'none'} for m in rows}
        return self.fa.durations(scorer, rows, acc)

    def test_each_listed_effort_is_its_own_aa_time_and_the_anchors_high_is_one(self):
        aa, arena = 'aa.cost_time', 'arena.agent_sessions'
        out, fit, why = self.build({self.anchor: [self.tp(aa, 'high', 10.0)],
                                    'm': [self.tp(aa, 'high', 15.0), self.tp(aa, 'max', 20.0), self.tp(arena, 'high', 3.0)]})
        self.assertEqual((out[self.anchor]['by_effort']['high'], out['m']['by_effort']), (1.0, {'high': 1.5, 'max': 2.0}))
        self.assertEqual(out['m']['minutes_by_effort'], {'high': 15.0, 'max': 20.0})
        self.assertEqual({b['method'] for b in out['m']['basis_by_effort'].values()}, {'aa'})   # the arena listing is not used
        self.assertEqual(out['m']['efforts_without_time'], [e for e in self.fa.LADDER if e not in ('high', 'max')])
        self.assertEqual(out['m']['basis_by_effort']['max']['url'], f'https://example.test/{aa}')

    def test_an_effortless_listing_is_the_single_configuration_keyed_high(self):
        out, _, _ = self.build({self.anchor: [self.tp('aa.cost_time', 'high', 10.0)], 'm': [self.tp('aa.cost_time', None, 5.0)],
                                'n': [self.tp('aa.cost_time', 'none', 5.0)]})
        self.assertEqual(out['m']['by_effort'], {'high': 0.5})
        # a non-reasoning listing is the measured reading at none (round 5 review B), used as measured
        self.assertEqual((out['n']['status'], out['n']['by_effort'], out['n']['basis_by_effort']['none']['method']),
                         ('ok', {'none': 0.5}, 'aa'))

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

    def test_an_edge_effort_takes_the_fleet_ratio_next_to_it_and_never_passes_its_neighbour(self):
        """Rule (durations, `EDGES`): `ultra` is Max times the fleet's median time(max) / time(xhigh) over the Artificial
        Analysis listings with both (n >= MIN_FLEET_RATIO_N), held at Max where that ratio would make it shorter."""
        aa = 'aa.cost_time'
        for ratios, ultra, held in (((1.5, 2.0, 2.5), 24.0, False), ((0.5, 0.6, 0.7), 12.0, True)):
            fleet = {f'f{i}': [self.tp(aa, 'xhigh', 10.0), self.tp(aa, 'max', 10.0 * r)] for i, r in enumerate(ratios)}
            out, _, _ = self.build({self.anchor: [self.tp(aa, 'high', 10.0)], **fleet, 'm': [self.tp(aa, 'max', 12.0)]})
            basis = out['m']['basis_by_effort']['ultra']
            self.assertEqual((out['m']['minutes_by_effort']['ultra'], basis['method'], basis['from_effort']),
                             (ultra, 'fleet_highest_step', 'max'), ratios)
            self.assertEqual(basis.get('bounded_by') == 'max', held, ratios)

    def test_an_extrapolated_time_never_leaves_the_fleets_measured_range(self):
        """Rule (durations, declared clip): an edge effort's time from the fleet ratio is held inside the range of Time/Task
        the board measures for the fleet (marked `clipped_to_fleet_range`), and never below its neighbour."""
        aa = 'aa.cost_time'
        fleet = {f'f{i}': [self.tp(aa, 'xhigh', 10.0), self.tp(aa, 'max', 30.0)] for i in range(3)}
        out, _, _ = self.build({self.anchor: [self.tp(aa, 'high', 10.0)], **fleet, 'm': [self.tp(aa, 'max', 12.0)]})
        basis = out['m']['basis_by_effort']['ultra']
        self.assertEqual((out['m']['minutes_by_effort']['ultra'], basis['clipped_to_fleet_range']), (30.0, [10.0, 30.0]))
        out, _, _ = self.build({self.anchor: [self.tp(aa, 'high', 10.0)], **fleet, 'm': [self.tp(aa, 'max', 8.0)]})
        self.assertEqual(out['m']['minutes_by_effort']['ultra'], 24.0)                 # inside the range: not clipped
        self.assertNotIn('clipped_to_fleet_range', out['m']['basis_by_effort']['ultra'])

    def test_none_time_is_the_lowest_measured_effort_times_the_fleets_own_off_ratio_when_the_pairs_agree(self):
        """Rule (durations, `fleet_off_step`; round 5 review B): `none` is the model's lowest measured reasoning effort times
        the fleet's (none / lowest measured effort) ratio, read by pair_agreement on the log ratios: the geometric median
        when n >= 2 and every pair points one way, else 1.0 with the reason; with no fleet pair there is no `none`."""
        aa = 'aa.cost_time'
        base = {self.anchor: [self.tp(aa, 'high', 10.0)], 'm': [self.tp(aa, 'low', 8.0), self.tp(aa, 'high', 10.0)]}
        agree = {'p': [self.tp(aa, 'none', 2.0), self.tp(aa, 'low', 4.0)], 'q': [self.tp(aa, 'none', 3.0), self.tp(aa, 'medium', 5.0)]}
        out, _, _ = self.build({**base, **agree})
        basis = out['m']['basis_by_effort']['none']
        ratio = round(float((0.5 * 0.6) ** 0.5), 4)
        self.assertEqual((basis['method'], basis['from_effort'], basis['ratio'], basis['n']), ('fleet_off_step', 'low', ratio, 2))
        self.assertEqual(out['m']['minutes_by_effort']['none'], round(8.0 * ratio, 3))
        disagree = {**agree, 'q': [self.tp(aa, 'none', 6.0), self.tp(aa, 'medium', 5.0)]}
        out, _, _ = self.build({**base, **disagree})
        self.assertEqual((out['m']['basis_by_effort']['none']['ratio'], out['m']['minutes_by_effort']['none']), (1.0, 8.0))
        out, _, _ = self.build(base)
        self.assertNotIn('none', out['m']['minutes_by_effort'])
        self.assertIn('none', out['m']['efforts_without_time'])

    def test_an_arena_fit_without_skill_leaves_the_arena_only_model_without_a_time(self):
        out, fit, why = self.build(self.paired(-1.0))           # slower on arena, faster on AA: not a usable fit
        self.assertIsNone(fit)
        self.assertIn('not positive', why)
        self.assertEqual((out['arena_only']['status'], out['arena_only']['by_effort']), ('missing', {}))
        self.assertEqual(out['arena_only']['efforts_without_time'], list(self.fa.LADDER))
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
        self.assertEqual(row['efforts_without_time'], [e for e in self.fa.LADDER if e not in ('low', 'high', 'max')])   # the sibling lists neither
        # alone, with no sibling to convert through, the other efforts have no time
        alone, _, _ = self.build({self.anchor: [self.tp(aa, 'high', 10.0)], 'gpt-6-luna': [self.tp(aa, 'high', 6.0)]})
        self.assertEqual(alone['gpt-6-luna']['by_effort'], {'high': 0.6})

    def test_a_same_model_row_takes_its_declared_models_times_only_when_it_carries_duration(self):
        aa = 'aa.cost_time'
        models = {self.anchor: [self.tp(aa, 'high', 10.0)], 'gpt-6-astra': [self.tp(aa, 'high', 20.0), self.tp(aa, 'max', 40.0)],
                  'dot': [], 'other': []}
        same = {'dot': {'model': 'gpt-6-astra', 'carries': ['duration'], 'url': 'https://openai.com/index/introducing-dots/',
                        'as_of': '2026-09-29'},
                'other': {'model': 'gpt-6-astra', 'carries': ['creativity']}}
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
        # Time/Task reads the own line only (series.groups 'line', round 6 item 4): another line of the maker gives nothing
        models = {self.anchor: [self.tp(aa, 'high', 10.0)], 'gpt-6-astra': [self.tp(aa, 'high', 20.0)], 'gpt-5.6-luna': []}
        self.assertEqual(self.build(models)[0]['gpt-5.6-luna']['status'], 'missing')

    def test_a_channel_row_that_does_not_carry_the_time_does_not_take_a_series_time(self):
        aa = 'aa.cost_time'
        models = {self.anchor: [self.tp(aa, 'high', 10.0)], 'gpt-6-astra': [self.tp(aa, 'high', 20.0)], 'chan': []}
        same = {'chan': {'model': 'gpt-6-astra', 'carries': ['creativity']}}
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
        shipped = axis_scores.load_five_axis()
        self.assertTrue(shipped['duration_fit'] or shipped['duration_fit_rejected'])
        for name, r in shipped['models'].items():
            dur = r['duration_multiplier']
            self.assertEqual(set(dur['by_effort']), set(dur['basis_by_effort']), name)
            self.assertEqual(set(dur['efforts_without_time']) | set(dur['by_effort']),
                             set(self.fa.LADDER), name)
            self.assertTrue(all(b['method'] in ('aa', 'arena_fit', 'series', 'series_effort', 'fleet_effort_ratio',
                                                'fleet_effort_position', 'same_model', 'effort_bound', 'size_step',
                                                'fleet_lowest_step', 'fleet_highest_step', 'fleet_off_step',
                                                'operating_point', 'speed_ratio', 'speed_lower_bound', 'declared_response_floor',
                                                'cross_line_extrapolation', 'cross_line_median')
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

    # The shape of https://wokey.ai/claude-usage-limits fetched 2026-11-18, where the plan the page used to call Max 20x is
    # named `Claude Ultra (Max 20x)` (the page's own words: "Claude Ultra (formerly Max 20x)"); figures are illustrative.
    WOKEY = ('<html><body><p>Data updated 2026-11-02</p><p>Claude Pro ($20/mo)</p><p>: ≈ $155 of API use a week '
             '(IQR $146–173) · 24.2M uncached tokens · ≈ 13 full 5-hour windows</p>'
             '<p>Claude Ultra (Max 20x) ($200/mo)</p><p>: ≈ $1,549 of API use a week (≈ $6,711/month) · 180M uncached tokens '
             '· ≈ 5.7 full 5-hour windows · 9.7× Claude Pro, not 20×</p>'
             '<p>Fable 5.1 can use only half the weekly limit.</p>'
             '<p>value every plan at one fixed traffic split: Claude Code traffic on Wokey in the week to 2026-11-02, about '
             '92% of tokens cache reads.</p><p>Claude Ultra (Max 20x) $200/mo $1,549 ($1,216–$1,697), ≈ $6,711/mo 34× 180M 9.9M '
             '$272 (≈ 5.7 a week) 18 2026 11/02 – 11/09</p><p>Claude Ultra (Max 20x): 92% cache reads, 1.98B tokens a week</p>'
             '</body></html>')

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
            target, files = self.pages(folder, self.WOKEY.replace('Claude Ultra (Max 20x)', 'Claude Ultra (Max 25x)'))
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

    def test_capacity_merges_dates_and_derives_fable(self):
        now = datetime(2026, 11, 9, tzinfo=timezone.utc)
        caps, errors = self.fa.capacity(self.meas(), self.PLANS, {'claude-opus-5-5': {'value': 20.0}})
        week = caps['claude']['max20']['seven_day']
        self.assertEqual(week['capacity_usd'], 1700.0)
        self.assertEqual((week['status'], week['as_of']), ('ok', '2026-11-02'))        # the newest date it rests on
        self.assertEqual(week['ref_hours'], 85.0)
        # The file holds dates, never ages: staleness is the reader's, on the day it reads.
        self.assertFalse([e for e in week['sources'] if 'stale' in e or 'age_days' in e])
        self.assertEqual({e['source']: data_age.is_stale(e['as_of'], now) for e in week['sources']},
                         {'community.a': False, 'community.b': True})
        fable = caps['claude']['max20']['weekly_fable']
        self.assertEqual(fable['capacity_usd'], 850.0)
        self.assertEqual(fable['sources'][0]['kind'], 'derived')
        self.assertEqual(len(fable['sources'][0]['derived_from']), 2)
        self.assertEqual(fable['sources'][0]['rests_on_as_of'], '2026-11-02')
        self.assertFalse([e for e in errors if e['reason'] == 'stale_source'])
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
                                          'factor': 0.25, 'windows': ['five_hour', 'seven_day'],
                                          'statement': 'a quarter', 'source': 'vendor.x', 'kind': 'vendor',
                                          'url': 'https://example.org/vendor', 'as_of': '2026-11-08'})
        meas['capacity']['exhaust'] = [{'family': 'claude', 'plan': 'max5', 'window': 'five_hour', 'model': 'claude-opus-5-5',
                                        'hours': 4.0, 'source': 'community.c', 'kind': 'community',
                                        'url': 'https://example.org/c', 'as_of': '2026-11-05', 'quote': 'ran out after 4h'}]
        caps, errors = self.fa.capacity(meas, self.PLANS, {'claude-opus-5-5': {'value': 20.0}})
        five = caps['claude']['max5']['five_hour']
        self.assertEqual((five['rung'], five['capacity_usd'], five['sources'][0]['kind']), (2, 80.0, 'derived'))
        self.assertEqual(five['cross_check']['value'], round(0.25 * 270.0, 2))
        week = caps['claude']['max5']['seven_day']
        self.assertEqual((week['rung'], week['capacity_usd']), (3, 425.0))
        self.assertEqual(week['sources'][0]['derived_from'][0]['source'], 'community.a')
        self.assertNotIn('cross_check', week)
        self.assertFalse([e for e in errors if e['reason'] == 'capacity_ladder_empty'])
        # An exhaust report of a model with no usd gives no value: the vendor rung takes over.
        caps, _ = self.fa.capacity(meas, self.PLANS, {})
        self.assertEqual(caps['claude']['max5']['five_hour']['rung'], 3)
        # A multiplier derives only the windows it names: the vendor statement covers no other quota track.
        meas['capacity']['rules'][-1]['windows'] = ['five_hour']
        caps, errors = self.fa.capacity(meas, self.PLANS, {})
        self.assertEqual(caps['claude']['max5']['five_hour']['rung'], 3)
        self.assertEqual((caps['claude']['max5']['seven_day']['capacity_usd'], caps['claude']['max5']['seven_day']['status']),
                         (None, 'error'))
        def gap(errs, reason):
            return [(e.get('plan'), e.get('window'), e['level']) for e in errs if e['reason'] == reason]
        self.assertIn(('max5', 'seven_day', 'red'), gap(errors, 'capacity_ladder_empty'))
        # A window the maintainers declared unmeasured is a listed gap, not a failed refresh; its capacity stays unknown.
        meas['capacity']['unmeasured'] = [{'family': 'claude', 'plan': 'max5', 'window': 'seven_day', 'kind': 'maintainer',
                                           'as_of': '2026-11-13', 'statement': 'no public measurement'}]
        caps, errors = self.fa.capacity(meas, self.PLANS, {})
        self.assertIsNone(caps['claude']['max5']['seven_day']['capacity_usd'])
        self.assertIn(('max5', 'seven_day', 'yellow'), gap(errors, 'capacity_unmeasured'))
        self.assertNotIn(('max5', 'seven_day', 'red'), gap(errors, 'capacity_ladder_empty'))

    def test_all_sources_stale_makes_the_window_stale_on_reading_and_refresh_exits_yellow_or_red(self):
        now = datetime(2026, 12, 15, tzinfo=timezone.utc)
        caps, errors = self.fa.capacity(self.meas(), self.PLANS, {})
        week = caps['claude']['max20']['seven_day']
        self.assertEqual(week['status'], 'ok')                                         # the file holds no calendar
        self.assertTrue(data_age.is_stale(week['as_of'], now))
        self.assertFalse(data_age.is_stale(week['as_of'], datetime(2026, 11, 9, tzinfo=timezone.utc)))
        meas = self.meas()
        meas['sources'] = {'community.a': {'kind': 'community', 'as_of': '2026-11-02'}}
        doc = {'source_dates': self.fa.source_dates(meas), 'capacity_errors': []}
        self.assertEqual(self.rf.staleness(doc, datetime(2026, 11, 9, tzinfo=timezone.utc))[1], 'ok')
        self.assertEqual(self.rf.staleness(doc, now)[1], 'stale')
        doc['capacity_errors'] = errors
        rows, worst = self.rf.staleness(doc, now)
        self.assertEqual(worst, 'missing')
        self.assertIn('claude/max5/seven_day', {r['what'] for r in rows})
        meas['missing_sources'] = {'metr.time_horizon_p50': {'reason': 'not used: the anchor is not listed'}}
        unused = self.rf.staleness({'source_dates': self.fa.source_dates(meas)}, datetime(2026, 11, 9, tzinfo=timezone.utc))
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
        scorer = self.fa.Scorer({'models': builder.out}, {**READINGS, 'models': {'m': {}, 'unlisted': {}}})
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

    def test_arena_matcher_reads_variant_labels_never_a_dated_snapshot_and_reports_true_miss_reason(self):
        """Rule (measurements.Matcher, round 5 review A): a variant label (`Muse Wonder (Max)`) matches its model; a dated
        listing (`DeepSeek-V4.1-Flash-20261017`) is another snapshot and is read only under its declared `listed_as`, never
        found for an undated row; a miss gives the true reason (without Cost/Task, distrusted, or not captured), not
        'not listed'."""
        import measurements
        matcher = measurements.Matcher(['DeepSeek-V4.1-Flash-20261017 (Max)', 'DeepSeek-V4.1-Flash-20260910 (Max)',
                                        'Muse Wonder (Max)', 'Beam 1.0 501B A23B'])
        self.assertIsNone(matcher.find('deepseek-v4.1-flash-free'))
        self.assertIsNone(matcher.find('deepseek-v4.1-flash'))
        self.assertEqual(matcher.find('deepseek-v4.1-flash-free', 'DeepSeek-V4.1-Flash-20260910'),
                         'DeepSeek-V4.1-Flash-20260910 (Max)')
        self.assertEqual(matcher.find('muse-wonder-1.0'), 'Muse Wonder (Max)')
        shipped = axis_scores.load_five_axis()
        mw = shipped['models']['muse-wonder-1.0']['usd_per_hour']
        # Rule: it resolves to its listing on the Agent sessions board, and the value is that board's figure.
        for row in (mw,):
            self.assertEqual((row['kind'], row['fallback'], row['used']), ('community', False, ['arena.agent_sessions']))
            self.assertEqual(row['value'], row['sources']['arena.agent_sessions']['value'])
        # A model listed on arena.ai Agent / Overall without Cost/Task says so instead of 'not listed'.
        # (synthetic: the fallback's merge text carries the true reason; round 5 converts the shipped example from a sibling)
        listed = {'models': {'n': {'usd_per_hour': [{'source': 'derived.community_mix', 'kind': 'derived', 'effort': 'high',
                                                      'value': 10.0}]}},
                  'sources': {'arena.agent_sessions': {'unpriced': {'n': {'reason': 'listed on arena.ai Agent / Overall as '
                                                                                    'N without Cost/Task'}}}}}
        nem = self.fa.usd_community_mix(self.fa.Scorer(listed, {**READINGS, 'models': {'n': {}}, 'families': {'rows': []}}),
                                        {'n': {'fields': listed['models']['n']}}, listed)['n']
        self.assertIn('without Cost/Task', nem['merge'])
        self.assertNotIn('not listed', nem['merge'])
        beam = shipped['models']['beam-free']['usd_per_hour']
        self.assertIn('without Cost/Task', beam['reason'])
        # Distrusting arena.agent_sessions records 'arena.agent_sessions distrusted', not 'not listed'.
        meas = {'models': {'m': {'usd_per_hour': [
            {'source': 'arena.agent_sessions', 'kind': 'community', 'effort': 'max', 'value': 6.125},
            {'source': 'derived.community_mix', 'kind': 'derived', 'effort': 'high', 'value': 13.688},
        ]}}}
        scorer = self.fa.Scorer(meas, {**READINGS, 'models': {'m': {}}, 'families': {'rows': []}}, distrust=('arena.agent_sessions',))
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
        scorer = self.fa.Scorer(meas, {**READINGS, 'models': dict.fromkeys(meas['models'], {})})
        usd = self.fa.usd_community_mix(scorer, {m: {'fields': f} for m, f in meas['models'].items()}, meas)
        self.assertEqual((usd['a']['value'], usd['a']['used'], usd['a']['kind'], usd['a']['fallback']),
                         (6.658, ['arena.agent_sessions'], 'community', False))
        self.assertEqual((usd['b']['value'], usd['b']['used'], usd['b']['kind'], usd['b']['fallback']),
                         (31.901, ['derived.community_mix'], 'derived', True))
        self.assertTrue(usd['b']['merge'].startswith('fallback ('))
        self.assertEqual((usd['c']['value'], usd['c']['status']), (None, 'missing'))

    def test_a_value_on_the_fallback_source_alone_is_put_on_the_primary_scale_by_the_fleets_median_ratio(self):
        """Owner 2026-11-19 (scale_fallback_usd): option B / arena over the rows with both (here 2, 3, 4 and 10: median 3.5),
        a row with option B alone divided by it, the ratio, n and spread stated; a row with both keeps arena; fewer than
        MIN_FLEET_RATIO_N rows with both leaves option B as it is."""
        def row(arena, mix):
            return {'usd_per_hour': ([{'source': 'arena.agent_sessions', 'kind': 'community', 'effort': 'high', 'value': arena}]
                                     if arena else []) + [{'source': 'derived.community_mix', 'kind': 'derived',
                                                           'effort': 'high', 'value': mix}]}
        meas = {'models': {'a': row(1.0, 2.0), 'b': row(2.0, 6.0), 'c': row(1.0, 4.0), 'd': row(1.0, 10.0),
                           'only-b': row(None, 7.0)}}
        def run(models):
            sub = {'models': {m: meas['models'][m] for m in models}}
            scorer = self.fa.Scorer(sub, {**READINGS, 'models': dict.fromkeys(models, {})})
            rows = {m: {'fields': f} for m, f in sub['models'].items()}
            usd = self.fa.usd_community_mix(scorer, rows, sub)
            self.fa.scale_fallback_usd(usd, rows, scorer)
            return usd
        usd = run(meas['models'])
        cell = usd['only-b']
        self.assertEqual((cell['value'], cell['estimated']), (round(7.0 / 3.5, 3), True))
        self.assertEqual({k: cell['on_primary_scale'][k] for k in ('ratio', 'n', 'rows', 'fallback_value')},
                         {'ratio': 3.5, 'n': 4, 'rows': ['a', 'b', 'c', 'd'], 'fallback_value': 7.0})
        self.assertEqual((cell['on_primary_scale']['spread']['min'], cell['on_primary_scale']['spread']['max']), (2.0, 10.0))
        self.assertEqual(usd['b']['value'], 2.0)
        self.assertNotIn('on_primary_scale', usd['b'])
        self.assertEqual(run(['a', 'b', 'only-b'])['only-b']['value'], 7.0)


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

    def test_alignment_rows_of_the_2026_11_15_page_shape_split_with_lab_prefixes_and_unlisted_case(self):
        """Row shapes of the page as of 2026-11-15: some rows carry the lab's text in front of the name, the lab at the end
        is written in the page's own case, and a row whose lab is unknown is an error, never a partial board."""
        rates = '66.6±2.31.00%0.50–1.50%2.00%1.00–3.00%3.00%2.00–4.00%'
        # (rank interval, the cell as the page runs it together, expected name, expected lab); the rank precedes the interval
        shapes = [('921', 'AnthropicClaude Opus 5.5Anthropic', 'Claude Opus 5.5', 'Anthropic'),
                  ('747', 'Reflection AIBeam 1.0 501B A23BReflection', 'Beam 1.0 501B A23B', 'Reflection'),
                  ('850', 'Nemotron 3 UltraNvidia', 'Nemotron 3 Ultra', 'Nvidia'),
                  ('249', 'NVIDIA-Nemotron-3.5-Super-VL-120B-A12B (NVFP4)Nvidia',
                   'NVIDIA-Nemotron-3.5-Super-VL-120B-A12B (NVFP4)', 'Nvidia'),
                  ('264', 'Thinking MachinesInkling 2 SmallThinky', 'Inkling 2 Small', 'Thinky'),
                  ('164', 'StepfunStep 5 PreviewStepFun', 'Step 5 Preview', 'StepFun'),
                  ('555', 'Mistral Large 4Mistral', 'Mistral Large 4', 'Mistral'),
                  ('250', 'SynlanSpark-X2.6XHToken', 'Spark-X2.6', 'XHToken'),     # the Agent page: Spark-X2.6, lab XHToken
                  ('155', 'Solar Pro 4Upstage', 'Solar Pro 4', 'Upstage')]
        head = ('Agent Arena | Alignment IndexNovember 15, 2026184,539 sessions{n} modelsRankModelAlignment Index'
                'Unauthorized actionFalse attributionDeceptive completion')
        text = head.format(n=len(shapes)) + ''.join(f'{rank}{interval}{cell}{rates}' for rank, (interval, cell, _, _)
                                                    in enumerate(shapes, 1)) + 'Footer'
        page = self.ac.parse_alignment(text)
        self.assertEqual([(r['rank'], r['name'], r['lab']) for r in page['rows']],
                         [(rank, name, lab) for rank, (_, _, name, lab) in enumerate(shapes, 1)])
        with self.assertRaises(ValueError) as caught:       # a lab the parser does not know is an error, not a partial board
            self.ac.parse_alignment(head.format(n=1) + '1' + '14' + 'Some ModelUnlistedLab' + rates)
        self.assertIn('lab not recognised', str(caught.exception))

    def test_the_users_mapping_of_the_agent_boards_onto_the_axes(self):
        cap = self.ms.CAPABILITY_SOURCES
        self.assertEqual(cap, {
            'lmarena.creative_writing': ('creativity', 'creative_writing'),
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
            (Path(tmp) / 'fetch-manifest.json').write_text('{"files": {}}', encoding='utf-8')
            anchor = self.ms.ANCHOR
            cite = {'kind': 'vendor', 'url': 'https://example.test/a', 'as_of': '2026-11-19', 'statement': 'A is the anchor'}
            builder = self.ms.Builder(Path(tmp), {**READINGS, 'models': {anchor: {'listed_as': {'arena': 'A', 'aa': 'A'},
                                                                      'listed_as_source': {'arena': cite, 'aa': cite}}},
                                                  'calibration_panel': {'anchor': anchor, 'models': [anchor],
                                                                        'members': [anchor]}})
            builder.boards()
        for src in self.ms.ARENA_CAPTURES:
            self.assertIn('not captured', builder.missing[src]['reason'], src)


class GrokPlanCapacityRuleTests(unittest.TestCase):
    """Task 7 redo (user 2026-11-11): Grok tier -> plan & cited price via plan-templates.json, runtime Claude-price
    capacity rule, Opus 5.5 usd_per_hour via same_model, and zero plan/price literals in Python code paths."""

    def test_detected_tiers_map_to_plan_and_cited_vendor_price_via_data(self):
        import provider_inventory
        accounts = account_fleet.load_accounts(account_fleet.reference_fleet_path())
        grok_table = provider_inventory.plan_table(accounts, 'grok')
        claude_table = provider_inventory.plan_table(accounts, 'claude')
        self.assertNotIn('supergrok', grok_table)
        expected = [
            ('SuperGrok', 'premium', 30),
            ('GrokPro', 'premium', 30),
            ('Grok Pro', 'premium', 30),
            ('Premium', 'premium', 30),
            ('The X Subscription Premium', 'premium', 30),
            ('Super', 'super', 100),
            ('The X Subscription Super', 'super', 100),
            ('Ultra', 'ultra', 200),
            ('Plus', 'plus', 8),
            ('Free', 'free', 0),
        ]
        for tier_label, plan_id, monthly_usd in expected:
            with self.subTest(tier=tier_label):
                resolved = provider_inventory.configured_plan('grok', tier_label, accounts=accounts)
                self.assertEqual(resolved, plan_id)
                self.assertEqual(grok_table[resolved]['price_usd_per_month'], monthly_usd)
                src = grok_table[resolved]['price_source']
                self.assertEqual(src['kind'], 'vendor')
                self.assertTrue(str(src['url']).startswith('https://'))
        for claude_plan, monthly_usd in (('pro', 20), ('max5', 100), ('max20', 200)):
            self.assertEqual(claude_table[claude_plan]['price_usd_per_month'], monthly_usd)
            self.assertEqual(claude_table[claude_plan]['price_source']['kind'], 'vendor')
            self.assertTrue(str(claude_table[claude_plan]['price_source']['url']).startswith('https://'))
        for bad in ('UnrecognizedFutureTier', 'subscription'):
            self.assertIsNone(provider_inventory.plan_defaults(
                accounts, 'grok', provider_inventory.configured_plan('grok', bad, accounts=accounts)))

    def test_runtime_capacity_matches_same_price_or_scales_nearest_claude_plan_and_yields_to_measurements(self):
        import provider_inventory
        scores = axis_scores.load_five_axis()
        claude_cap = {
            p: float(scores['capacity']['claude'][p]['seven_day']['capacity_usd'])
            for p in ('pro', 'max5', 'max20')
        }

        def overlay_for_tier(tier_label, extra_scores=None):
            snap = {
                'accounts': {
                    'grok': {
                        'family': 'grok', 'status': 'ready', 'billing': 'subscription',
                        'plan': provider_inventory.configured_plan('grok', tier_label) if tier_label else None,
                        'plan_source': 'settings_api' if tier_label else None,
                        'usage_windows': ['credits'],
                    },
                    'grok-bot': {'family': 'grok-bot', 'status': 'ready', 'billing': 'unknown'},
                },
                'launchers': {},
            }
            fleet = provider_inventory.apply(account_fleet.load_accounts(account_fleet.reference_fleet_path()), snap)
            return axis_scores.five_axis_overlay(fleet, extra_scores or scores)

        # Exact-price matches: Super ($100) == claude/max5 ($100), Ultra ($200) == claude/max20 ($200).
        for tier_label, ref_plan in (('Super', 'max5'), ('Ultra', 'max20')):
            over = overlay_for_tier(tier_label)
            for acct in ('grok', 'grok-bot'):
                self.assertFalse(over['accounts'][acct].get('capacity_unknown'))
                self.assertAlmostEqual(over['accounts'][acct]['capacity_usd']['credits'], claude_cap[ref_plan], places=2)
                src = over['accounts'][acct]['capacity_source']['credits']
                self.assertEqual((src['match'], src['price_ratio'], src['reference_plan']), ('exact_price', 1.0, ref_plan))
                self.assertIn(f'same-price claude subscription: claude/{ref_plan}', src['evidence'])

        # Nearest-price ratio scaling: SuperGrok -> premium ($30) -> 30/20 * claude/pro ($20); Plus ($8) -> 8/20 * claude/pro ($20).
        for tier_label, expected_plan, ratio in (('SuperGrok', 'premium', 30.0 / 20.0), ('Plus', 'plus', 8.0 / 20.0)):
            over = overlay_for_tier(tier_label)
            expected = round(claude_cap['pro'] * ratio, 2)
            for acct in ('grok', 'grok-bot'):
                self.assertFalse(over['accounts'][acct].get('capacity_unknown'))
                self.assertAlmostEqual(over['accounts'][acct]['capacity_usd']['credits'], expected, places=2)
                src = over['accounts'][acct]['capacity_source']['credits']
                self.assertEqual((src['plan'], src['match'], src['reference_plan']), (expected_plan, 'nearest_price_scaled', 'pro'))
                self.assertAlmostEqual(src['price_ratio'], ratio, places=4)
                self.assertIn('nearest plan by price is claude/pro', src['evidence'])

        # Free ($0) and missing tier stay capacity_unknown (never defaulted).
        for unpriced in ('Free', None):
            over = overlay_for_tier(unpriced)
            self.assertTrue(over['accounts']['grok-bot'].get('capacity_unknown'))
            self.assertEqual(over['accounts']['grok-bot']['capacity_usd'], {})

        # Direct measurement on capacity['grok']['premium']['credits'] overrides the maintainer rule automatically.
        measured_scores = json.loads(json.dumps(scores))
        measured_scores.setdefault('capacity', {}).setdefault('grok', {}).setdefault('premium', {})['credits'] = {
            'capacity_usd': 410.0, 'rung': 1, 'status': 'ok', 'sources': [{'source': 'community.test', 'value': 410.0}],
        }
        over_measured = overlay_for_tier('SuperGrok', extra_scores=measured_scores)
        self.assertEqual(over_measured['accounts']['grok-bot']['capacity_usd']['credits'], 410.0)
        self.assertNotIn('rule', over_measured['accounts']['grok-bot']['capacity_source']['credits'])

    def test_grok_bot_usd_per_hour_matches_opus_5_5_and_yields_to_direct_measurement(self):
        import five_axis
        shipped = axis_scores.load_five_axis()['models']
        self.assertEqual(shipped['grok-bot']['usd_per_hour']['value'], shipped['claude-opus-5-5']['usd_per_hour']['value'])
        self.assertIn('same_model', shipped['grok-bot']['usd_per_hour']['merge'])
        self.assertIn('claude-opus-5-5', shipped['grok-bot']['usd_per_hour']['merge'])
        accounts = account_fleet.load_accounts(account_fleet.reference_fleet_path())
        overlaid = axis_scores.five_axis_overlay(accounts)
        self.assertEqual(overlaid['models']['grok-bot']['usd_per_hour'],
                         overlaid['models']['claude-opus-5-5']['usd_per_hour'])
        # Direct measurement overrides same_model inheritance.
        meas = {'models': {
            'claude-opus-5-5': {'usd_per_hour': [{'source': 'arena.agent_sessions', 'kind': 'community', 'effort': 'high', 'value': 22.86}]},
            'grok-bot': {'usd_per_hour': [{'source': 'arena.agent_sessions', 'kind': 'community', 'effort': 'high', 'value': 19.5}]},
        }}
        scorer = five_axis.Scorer(meas, accounts)
        rows = {m: {'fields': f} for m, f in meas['models'].items()}
        usd = five_axis.usd_community_mix(scorer, rows, meas)
        five_axis.convert_usd(usd, rows, scorer, meas, accounts)
        self.assertEqual(usd['grok-bot']['value'], 19.5)
        self.assertNotIn('same_model', usd['grok-bot']['merge'])

    def test_no_grok_plan_or_price_literals_appear_in_python_code_or_maintainer_rule(self):
        import ast
        accounts = json.loads((Path(__file__).resolve().parent.parent / 'references' / 'accounts.json').read_text(encoding='utf-8'))
        rule = accounts['same_model']['grok-bot']['capacity_rule']
        # Rule declaration contains no plan names and no numeric literals.
        for key, val in rule.items():
            self.assertNotIsInstance(val, (int, float), f'numeric literal in capacity_rule.{key}')
            if isinstance(val, str):
                self.assertNotIn(val.lower(), {'supergrok', 'grokpro', 'premium', 'super', 'ultra', 'plus', 'pro', 'max5', 'max20'})

        scripts_dir = Path(__file__).resolve().parent
        cal_dir = scripts_dir.parent / 'calibration'
        forbidden_plan_strings = {
            'supergrok', 'grokpro', 'grok pro', 'supergrok plus', 'supergrokplus', 'supergrok heavy',
            'x subscription plus', 'x subscription premium', 'x subscription super', 'x subscription ultra',
        }
        target_files = [
            scripts_dir / 'axis_scores.py',
            scripts_dir / 'candidate_eval.py',
            scripts_dir / 'provider_inventory.py',
            scripts_dir / 'reference_provenance.py',
            cal_dir / 'five_axis.py',
        ]
        for path in target_files:
            tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    self.assertNotIn(node.value.strip().lower(), forbidden_plan_strings, f'{path.name}:{node.lineno}')

        # Also check the Grok tier extraction / resolution functions specifically for zero plan or monthly price literals.
        usage_tree = ast.parse((scripts_dir / 'usage_core.py').read_text(encoding='utf-8'))
        for node in ast.walk(usage_tree):
            if isinstance(node, ast.FunctionDef) and 'grok' in node.name.lower():
                for child in ast.walk(node):
                    if isinstance(child, ast.Constant) and isinstance(child.value, str):
                        self.assertNotIn(child.value.strip().lower(), forbidden_plan_strings,
                                         f'usage_core.py:{node.name}:{child.lineno}')

        forbidden_monthly_prices = {8, 8.0, 20, 20.0, 30, 30.0, 100, 100.0, 200, 200.0, 300, 300.0}
        axis_tree = ast.parse((scripts_dir / 'axis_scores.py').read_text(encoding='utf-8'))
        for node in ast.walk(axis_tree):
            if isinstance(node, ast.FunctionDef) and node.name in ('_capacity_rules', '_resolve_rule_capacity', '_plan_unknown_text'):
                for child in ast.walk(node):
                    if isinstance(child, ast.Constant) and isinstance(child.value, (int, float)) and not isinstance(child.value, bool):
                        self.assertNotIn(child.value, forbidden_monthly_prices,
                                         f'subscription price literal {child.value} in axis_scores.{node.name}:{child.lineno}')

    def test_antigravity_tier_mapping_lives_only_in_plan_templates_with_no_default_plan(self):
        import ast
        refs_dir = Path(__file__).resolve().parent.parent / 'references'
        scripts_dir = Path(__file__).resolve().parent
        plan_templates = json.loads((refs_dir / 'plan-templates.json').read_text(encoding='utf-8'))
        for family, plans in plan_templates.items():
            self.assertNotIn('default', plans, f'{family} must not declare a default plan')
        agy_plans = plan_templates['antigravity']
        self.assertEqual(set(agy_plans), {'pro', 'ultra5', 'ultra20'})
        for plan_key, tpl in agy_plans.items():
            self.assertEqual(tpl['price_source']['kind'], 'vendor', plan_key)
            self.assertTrue(tpl['price_source']['url'].startswith('https://'), plan_key)
            self.assertTrue(tpl.get('tiers'), plan_key)

        forbidden_agy_tiers = {
            'g1-pro-tier', 'g1-ultra-lite-tier', 'g1-ultra-tier',
            'google ai pro', 'google ai ultra', 'google ai ultra (5x)', 'google ai ultra (20x)',
        }
        for fname in ('antigravity_plan.py', 'provider_inventory.py', 'axis_scores.py', 'candidate_eval.py'):
            path = scripts_dir / fname
            tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    self.assertNotIn(node.value.strip().lower(), forbidden_agy_tiers, f'{fname}:{node.lineno}')
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'plan_defaults':
                    for arg in node.args:
                        if isinstance(arg, ast.Constant) and arg.value == 'default':
                            self.fail(f'{fname}:{node.lineno} calls plan_defaults(..., "default")')


class SpeedVariantTests(unittest.TestCase):
    """Rule (calibration/README.md, Speed variants; owner 2026-11-19): a `same_model` row with `speed` takes its model's
    abilities; its responsiveness and Time/Task are its own reading, else the model's moved by the stated ratio r
    (responsiveness + ln(r) / sd_panel of aa.ttfa, Time / r; a range read as its geometric mean), else the model's as a stated
    lower bound; never the ordinary estimate from the model, and never the model's cost per hour."""

    def test_speed_declaration_reads_a_range_as_its_geometric_mean_and_refuses_a_malformed_one(self):
        cite = {'kind': 'vendor', 'url': 'https://example.com/models', 'as_of': '2026-11-19', 'statement': 'x'}
        self.assertEqual(five_axis.speed_declaration({'speed': {**cite, 'ratio': [2, 8]}})['ratio'], 4.0)
        self.assertEqual(five_axis.speed_declaration({'speed': {**cite, 'ratio': 3}})['ratio'], 3)
        self.assertIsNone(five_axis.speed_declaration({'speed': {**cite, 'ratio': None}})['ratio'])
        self.assertIsNone(five_axis.speed_declaration({'model': 'm'}))
        for bad in ({**cite, 'ratio': 1}, {**cite, 'ratio': [3, 2]}, {**cite, 'ratio': 'fast'}, {'ratio': 2, 'statement': 'x'},
                    {**cite, 'ratio': 2, 'statement': ''}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                five_axis.speed_declaration({'speed': bad})

    def test_a_stated_ratio_moves_the_bases_responsiveness_by_ln_r_over_sd_and_no_ratio_keeps_it_as_a_bound(self):
        """Rule (`Scorer.speed_variants`): a variant with no responsiveness of its own is its base's curve plus
        ln(r) / sd_panel of aa.ttfa (method `speed_ratio`); without a stated ratio the base's curve, a lower bound."""
        sc = five_axis.Scorer.__new__(five_axis.Scorer)
        sc.meas = {'sources': {'aa.ttfa': {'scale': {'sd_panel': 0.5}}}}
        cite = {'kind': 'vendor', 'url': 'https://example.com/models', 'as_of': '2026-11-19', 'statement': 'x'}
        sc.speed = {'fast': {**cite, 'ratio': 4.0, 'stated': [2, 8]}, 'bound': {**cite, 'ratio': None, 'stated': None}}
        sc.same = {'fast': {'model': 'base'}, 'bound': {'model': 'base'}}
        key = five_axis.SPEED_KEY
        facets = {'base': {key: {'value': 1.0, 'by_effort': {'high': 1.0, 'max': 0.5}, 'status': 'measured'}},
                  'fast': {}, 'bound': {}}
        sc.speed_variants(facets)
        shift = round(math.log(4.0) / 0.5, 4)
        self.assertEqual(facets['fast'][key]['by_effort'], {'high': round(1.0 + shift, 4), 'max': round(0.5 + shift, 4)})
        self.assertEqual((facets['fast'][key]['estimate']['method'], facets['fast'][key]['estimate']['shift']),
                         ('speed_ratio', shift))
        self.assertEqual(facets['bound'][key]['by_effort'], {'high': 1.0, 'max': 0.5})
        self.assertEqual(facets['bound'][key]['estimate']['method'], 'speed_lower_bound')

    def test_every_shipped_speed_variant_follows_the_rule(self):
        refs = account_fleet.REFERENCES
        acc = json.loads((refs / 'accounts.json').read_text(encoding='utf-8-sig'))
        meas = json.loads((refs / 'public-measurements.json').read_text(encoding='utf-8-sig'))
        models = json.loads((refs / 'five-axis-scores.json').read_text(encoding='utf-8-sig'))['models']
        sd = meas['sources']['aa.ttfa']['scale']['sd_panel']
        variants = {m: e for m, e in acc['same_model'].items() if 'speed' in e}
        self.assertTrue(variants)
        for m, entry in variants.items():
            with self.subTest(model=m):
                r = five_axis.speed_declaration(entry)['ratio']
                base, own = models[entry['model']], models[m]
                resp = own['axes']['tool_operation']['facets']['responsiveness']
                if not resp['status'].startswith('measured'):
                    self.assertEqual(resp['estimate']['method'], 'speed_ratio' if r else 'speed_lower_bound')
                    shift = math.log(r) / sd if r else 0.0
                    want = base['axes']['tool_operation']['facets']['responsiveness']['by_effort']
                    for e, v in resp['by_effort'].items():
                        self.assertAlmostEqual(v, want[e] + shift, delta=2e-4, msg=e)
                dur = own['duration_multiplier']
                if dur['basis_by_effort'].get('high', {}).get('method') in ('speed_ratio', 'speed_lower_bound'):
                    self.assertAlmostEqual(dur['minutes_by_effort']['high'],
                                           base['duration_multiplier']['minutes_by_effort']['high'] / (r or 1.0), delta=2e-3)
                for cat in ('creativity', 'coding'):
                    self.assertEqual(own['axes'][cat]['value'], base['axes'][cat]['value'], cat)
                usd = own.get('usd_per_hour') or {}
                if usd.get('value') is not None:
                    self.assertNotIn(usd.get('method'), ('same_model',))
                    self.assertEqual(usd.get('kind'), 'converted')        # at its own list price


class ResponseFloorTests(unittest.TestCase):
    """Rule (calibration/README.md, Speed without a reading; owner 2026-11-19): a declared floor of the response time
    (`response_time_source` `min_seconds`) on a row with no reading of its own bounds, it does not replace: Time/Task is
    the longer of the floor (min_seconds / 60 minutes, a task is at least one response) and what the next method of the
    order gives at that effort, responsiveness the slower of the floor read through the time-to-first-answer transform and
    the method's value; the floor alone where no method gives one; the cell keeps both inputs and which one binds; no
    inversion along effort."""
    FLOOR = {'kind': 'maintainer', 'as_of': '2026-11-19', 'min_seconds': 600, 'statement': 'synthetic'}

    @classmethod
    def setUpClass(cls):
        refs = account_fleet.REFERENCES
        cls.acc = json.loads((refs / 'accounts.json').read_text(encoding='utf-8-sig'))
        cls.meas = json.loads((refs / 'public-measurements.json').read_text(encoding='utf-8-sig'))
        cls.models = json.loads((refs / 'five-axis-scores.json').read_text(encoding='utf-8-sig'))['models']

    def times(self, relative_minutes):
        """durations() for a floored astra-line row `gpt-6-pro` beside its read same-line relative `gpt-6-astra`
        ({effort: AA minutes}) and a floored row `solo` with no relative."""
        def tp(e, t):
            return {'source': 'aa.cost_time', 'kind': 'benchmark', 'effort': e, 'time_per_task_min': t,
                    'listed_as': f'x ({e})', 'as_of': '2026-11-19', 'url': 'https://example.test/aa'}
        fields = {five_axis.measurements.ANCHOR: [tp('high', 10.0)],
                  'gpt-6-astra': [tp(e, t) for e, t in relative_minutes.items()], 'gpt-6-pro': [], 'solo': []}
        rows = {m: {'fields': {'throughput': f}} for m, f in fields.items()}
        acc = {**READINGS, 'models': {m: ({'response_time_source': self.FLOOR} if m in ('gpt-6-pro', 'solo') else {})
                                      for m in fields}, 'aliases': {}, 'families': GPT_FAMILIES}
        out, _, _ = five_axis.durations(five_axis.Scorer({'models': fields}, acc), rows, acc)
        return out

    def test_time_is_the_longer_of_the_floor_and_the_estimate_at_each_effort(self):
        out = self.times({'high': 4.0, 'max': 30.0})
        pro, basis = out['gpt-6-pro']['minutes_by_effort'], out['gpt-6-pro']['basis_by_effort']
        self.assertEqual((pro['high'], basis['high']['binds'], basis['high']['estimate']['minutes']), (10.0, 'floor', 4.0))
        self.assertEqual((pro['max'], basis['max']['binds'], basis['max']['estimate']['method']), (30.0, 'estimate', 'series'))
        efforts = [e for e in five_axis.LADDER if e in pro]
        self.assertEqual([pro[e] for e in efforts], sorted(pro[e] for e in efforts))     # no inversion along effort
        solo = out['solo']
        self.assertEqual(set(solo['minutes_by_effort'].values()), {10.0})
        self.assertEqual({b['binds'] for b in solo['basis_by_effort'].values()}, {'floor'})
        self.assertIsNone(solo['basis_by_effort']['high']['estimate'])

    def responsiveness(self, relative_z):
        def resp(z):
            return [{'source': 'aa.ttfa', 'facet': 'responsiveness', 'value': z, 'z_by_effort': dict.fromkeys(EFFORT_NAMES, z)}]
        rel = AnchorRelativeTests('test_responsiveness_without_its_own_line_takes_the_median_of_the_makers_other_lines')
        rel.ms = five_axis.measurements
        meas = {**rel.calibration_set({'acme-a-1': rel.tiered(4.2, tool_operation=resp(relative_z)),
                                       'acme-b-1': rel.tiered(4.0)}),
                'sources': {'aa.ttfa': {'scale': {'anchor_raw': -3.0, 'sd_panel': 1.0}}}}
        fams = {'rows': [{'pattern': f'^acme-{x}', 'models': [], 'maker': 'acme', 'product': 'acme', 'line': x, 'size': 'full'} for x in 'ab']}
        acc = {**READINGS, 'series_versions': {'versions': {'acme-a-1': 1, 'acme-b-1': 1}}, 'families': fams,
               'models': {m: ({'response_time_source': self.FLOOR} if m == 'acme-b-1' else {}) for m in meas['models']}}
        rows, _, _, _, _, _ = five_axis.Scorer(meas, acc).score()
        return rows['acme-b-1']['axes']['tool_operation']['facets']['responsiveness']

    def test_responsiveness_is_the_slower_of_the_floor_and_the_estimate(self):
        z = round(-math.log(600) + 3.0, 4)
        floor = self.responsiveness(0.5)            # the other line's relative is faster than the floor: the floor binds
        self.assertEqual((floor['value'], floor['estimate']['binds_by_effort']['high'],
                          floor['estimate']['estimate_input']['estimate']['method']), (z, 'floor', 'cross_line_median'))
        slow = self.responsiveness(-5.0)            # slower than the floor: the estimate binds
        self.assertEqual((slow['estimate']['binds_by_effort']['high'], slow['estimate']['floor_value']), ('estimate', z))
        self.assertAlmostEqual(slow['value'], slow['estimate']['estimate_input']['by_effort']['high'], places=4)
        self.assertLess(slow['value'], z)

    def test_the_shipped_floors_bound_the_shipped_estimates(self):
        floors = {m: r['response_time_source'] for m, r in self.acc['models'].items() if 'response_time_source' in r}
        self.assertTrue(floors)
        scale = self.meas['sources']['aa.ttfa']['scale']
        for m, decl in floors.items():
            with self.subTest(model=m):
                dur = self.models[m]['duration_multiplier']
                floor = round(decl['min_seconds'] / 60.0, 3)
                for e, b in dur['basis_by_effort'].items():
                    if b['method'] == 'declared_response_floor' and 'bounded_by' not in b:
                        est = (b['estimate'] or {}).get('minutes')
                        self.assertEqual(dur['minutes_by_effort'][e], max(floor, est) if est is not None else floor)
                resp = self.models[m]['axes']['tool_operation']['facets']['responsiveness']
                if resp.get('estimate', {}).get('method') == 'declared_response_floor':
                    z = (-math.log(decl['min_seconds']) - scale['anchor_raw']) / scale['sd_panel']
                    self.assertTrue(all(v <= z + 2e-4 for v in resp['by_effort'].values()), resp['by_effort'])


class SameModelPointTests(unittest.TestCase):
    """Rule (accounts.json same_model `at_effort`, owner 2026-11-19, round 7): a row declared the same weights as its model
    at one operating point reads the model's cell at that effort at every effort (facets and Time/Task), then its declared
    response floor: Time at least the floor, responsiveness at most the floor's. Never below the model at max on an
    ability facet, never faster than the model at max."""
    FLOOR = {'kind': 'maintainer', 'as_of': '2026-11-19', 'min_seconds': 600, 'statement': 'synthetic'}
    POINT = {'effort': 'ultra', 'kind': 'maintainer', 'as_of': '2026-11-19', 'statement': 'synthetic'}

    def scorer(self, floor=True):
        s = five_axis.Scorer.__new__(five_axis.Scorer)
        s.same = {'pro': {'model': 'base', 'carries': ['coding', 'tool_operation'], 'kind': 'vendor',
                          'url': 'https://example.test/help', 'statement': 's', 'at_effort': self.POINT}}
        s.response_floor = {'pro': self.FLOOR} if floor else {}
        s.fill_refused = []
        s.meas = {'sources': {'aa.ttfa': {'scale': {'anchor_raw': -3.0, 'sd_panel': 1.0}}}}
        return s

    @staticmethod
    def curve(values):
        by = dict(zip(five_axis.LADDER, values))
        return {'value': by['high'], 'by_effort': by, 'status': 'measured', 'sources': {}, 'measured_efforts': list(by)}

    def facets(self, resp):
        return {'base': {('coding', 'execution'): self.curve([-1, -0.8, -0.5, -0.2, 0.0, 0.3, 0.5, 0.7]),
                         five_axis.SPEED_KEY: self.curve(resp)}, 'pro': {}}

    def test_every_effort_reads_the_models_point_and_never_falls_below_the_model_at_max(self):
        s, f = self.scorer(floor=False), self.facets([2, 1.8, 1.5, 1.0, 0.5, 0.0, -0.5, -1.0])
        s.copy_same_model(f, ['pro'])
        ex, resp = f['pro'][('coding', 'execution')], f['pro'][five_axis.SPEED_KEY]
        self.assertEqual(set(ex['by_effort'].values()), {0.7})
        self.assertEqual((ex['estimate']['method'], ex['estimate']['at_effort']), ('same_model', 'ultra'))
        self.assertGreaterEqual(min(ex['by_effort'].values()), f['base'][('coding', 'execution')]['by_effort']['max'])
        self.assertLessEqual(max(resp['by_effort'].values()), f['base'][five_axis.SPEED_KEY]['by_effort']['max'])

    def test_the_floor_bounds_the_point_both_ways(self):
        z = round(-math.log(600) + 3.0, 4)
        for point, binds in ((-1.0, 'floor'), (-5.0, 'estimate')):
            with self.subTest(point=point):
                s, f = self.scorer(), self.facets([2, 1.8, 1.5, 1.0, 0.5, 0.0, -0.5, point])
                s.copy_same_model(f, ['pro'])
                s.response_floors(f)
                resp = f['pro'][five_axis.SPEED_KEY]
                self.assertEqual(set(resp['estimate']['binds_by_effort'].values()), {binds})
                self.assertEqual(resp['value'], min(z, point))
                self.assertEqual(resp['estimate']['estimate_input']['estimate']['at_effort'], 'ultra')

    def test_time_reads_the_models_point_then_at_least_the_floor(self):
        def tp(e, t):
            return {'source': 'aa.cost_time', 'kind': 'benchmark', 'effort': e, 'time_per_task_min': t,
                    'listed_as': f'x ({e})', 'as_of': '2026-11-19', 'url': 'https://example.test/aa'}
        point = {**self.POINT, 'effort': 'max'}        # a measured effort: the board lists no ultra
        for top, binds in ((6.0, 'floor'), (30.0, 'estimate')):
            with self.subTest(top=top):
                fields = {five_axis.measurements.ANCHOR: [tp('high', 10.0)],
                          'base': [tp('high', 4.0), tp('xhigh', 5.0), tp('max', top)], 'pro': []}
                acc = {**READINGS, 'models': {'pro': {'response_time_source': self.FLOOR}, 'base': {},
                                              five_axis.measurements.ANCHOR: {}}, 'aliases': {}, 'families': {'rows': []},
                       'same_model': {'pro': {'model': 'base', 'carries': ['duration'], 'kind': 'vendor',
                                              'url': 'https://example.test/help', 'statement': 's', 'at_effort': point}}}
                rows = {m: {'fields': {'throughput': v}} for m, v in fields.items()}
                out, _, _ = five_axis.durations(five_axis.Scorer({'models': fields}, acc), rows, acc)
                pro = out['pro']
                self.assertEqual(set(pro['minutes_by_effort'].values()), {max(10.0, top)})
                self.assertEqual({b['binds'] for b in pro['basis_by_effort'].values()}, {binds})
                self.assertEqual(pro['basis_by_effort']['high']['estimate']['at_effort'], 'max')
                self.assertGreaterEqual(min(pro['minutes_by_effort'].values()), out['base']['minutes_by_effort']['max'])


class FloorScoreProvenanceTests(unittest.TestCase):
    """Rule (reference_provenance.validate_scores_against_floors, owner 2026-11-19, round 7): a shipped maintainer score on
    responsiveness that reads faster than the same row's declared response floor refuses the references, naming both; a
    score at or slower than the floor passes."""

    def test_a_maintainer_score_faster_than_the_floor_is_refused_and_one_slower_passes(self):
        import reference_provenance
        acc = {'models': {'pro': {'response_time_source': {'kind': 'maintainer', 'as_of': '2026-11-19', 'min_seconds': 600,
                                                            'statement': 's'}}}}
        meas = {'sources': {'aa.ttfa': {'scale': {'anchor_raw': -3.0, 'sd_panel': 1.0}}}}
        levels = {str(k): {'target': t} for k, t in ((1, -5.0), (2, -1.0), (3, 1.0))}
        five = {'anchor': {'targets': {'tool_operation': {'facets': {'responsiveness': {'levels': levels}}}}}}

        def scores(level):
            return {'scores': {'pro': {'tool_operation.responsiveness': {'level': level, 'kind': 'maintainer',
                                                                          'as_of': '2026-11-11'}}}}
        z_floor = -math.log(600) + 3.0                    # -3.397: level 1.4 on this scale
        with self.assertRaisesRegex(ValueError, 'pro tool_operation.responsiveness level 2.*response_time_source'):
            reference_provenance.validate_scores_against_floors(scores(2.0), acc, five, meas)
        reference_provenance.validate_scores_against_floors(scores(1.0), acc, five, meas)
        self.assertLess(-5.0, z_floor)

    def test_a_score_under_another_spelling_of_the_row_is_checked_too(self):
        """Round 8 item 2: the score's key is resolved to its row first (an alias, a provider-qualified name), as the
        overlay reads it, so writing the score under another spelling does not pass the check."""
        import reference_provenance
        acc = {'models': {'pro': {'response_time_source': {'kind': 'maintainer', 'as_of': '2026-11-19', 'min_seconds': 600,
                                                            'statement': 's'}}},
               'aliases': {'Pro via consult': 'pro'}}
        meas = {'sources': {'aa.ttfa': {'scale': {'anchor_raw': -3.0, 'sd_panel': 1.0}}}}
        levels = {str(k): {'target': t} for k, t in ((1, -5.0), (2, -1.0), (3, 1.0))}
        five = {'anchor': {'targets': {'tool_operation': {'facets': {'responsiveness': {'levels': levels}}}}}}
        for key in ('Pro via consult', 'vendor/pro', 'vendor/Pro via consult'):
            for target in reference_provenance.FLOOR_TARGETS:
                doc = {'scores': {key: {target: {'level': 2.0, 'kind': 'maintainer', 'as_of': '2026-11-11'}}}}
                with self.subTest(key=key, target=target), \
                        self.assertRaisesRegex(ValueError, f'{re.escape(key)} \\(row pro\\) {re.escape(target)} level 2'):
                    reference_provenance.validate_scores_against_floors(doc, acc, five, meas)


class CrossLineTimeTests(unittest.TestCase):
    """Rule (five_axis.cross_line_times; calibration/README.md, Speed without a reading; owner 2026-11-19): a row with no
    Time/Task of its own or of its line takes the maker's other lines of the same size, nearest version up to the declared
    distance, among relatives read on a board at some effort: the relatives' times times the median own/other ratio at High
    where both lines are read on at least two same-version pairs (cross_line_extrapolation), else the median of the
    relatives' times (cross_line_median). An estimated time is never a relative's basis."""

    def run_step(self, read, estimated=(), versions=None):
        """minutes/basis after the step, for acme rows `read` ({row: High minutes}, method aa) and `estimated`
        ({row: High minutes}, method series)."""
        models = {**read, **dict(estimated)}
        acc = {**READINGS, 'models': {m: {} for m in [*models, 'acme-b-4']},
               'families': {'rows': [{'pattern': f'^acme-{x}', 'models': [], 'maker': 'acme', 'product': 'acme', 'line': x, 'size': 'full'}
                                     for x in 'ab']},
               'series_versions': {'versions': versions or {m: int(m[-1]) for m in [*models, 'acme-b-4']}}}
        rows = dict.fromkeys(acc['models'])
        minutes = {m: {'high': t, 'xhigh': 2 * t} for m, t in models.items()} | {'acme-b-4': {}}
        basis = {m: {e: {'method': 'aa' if m in read else 'series'} for e in ('high', 'xhigh')} for m in models}
        basis['acme-b-4'] = {}
        five_axis.cross_line_times(SimpleNamespace(entries=set()), rows, acc, {}, minutes, basis, set())
        return minutes['acme-b-4'], basis['acme-b-4']

    def test_durations_give_a_row_with_no_time_of_its_line_the_other_lines_time(self):
        """Rule (`durations` runs `cross_line_times`): a row with no Time/Task of its own or of its line, beside a read
        relative of another line of the same maker and size at the same version, gets that relative's time
        (cross_line_median) at each effort the relative has, end to end through the build of the times."""
        def tp(e, t):
            return {'source': 'aa.cost_time', 'kind': 'benchmark', 'effort': e, 'time_per_task_min': t,
                    'listed_as': f'x ({e})', 'as_of': '2026-11-19', 'url': 'https://example.test/aa'}
        fields = {five_axis.measurements.ANCHOR: [tp('high', 10.0)], 'acme-a-2': [tp('high', 6.0), tp('max', 9.0)],
                  'acme-b-2': []}
        rows = {m: {'fields': {'throughput': f}} for m, f in fields.items()}
        fams = {'rows': [{'pattern': f'^acme-{x}', 'models': [], 'maker': 'acme', 'product': 'acme', 'line': x,
                          'size': 'full'} for x in 'ab']}
        acc = {**READINGS, 'models': {m: {} for m in fields}, 'aliases': {}, 'families': fams,
               'series_versions': {'versions': {'acme-a-2': 2, 'acme-b-2': 2}}}
        out, _, _ = five_axis.durations(five_axis.Scorer({'models': fields}, acc), rows, acc)
        got, basis = out['acme-b-2']['minutes_by_effort'], out['acme-b-2']['basis_by_effort']
        self.assertEqual((got['high'], got['max']), (6.0, 9.0))
        self.assertEqual((basis['high']['method'], basis['high']['base_model']), ('cross_line_median', ['acme-a-2']))

    def test_without_two_same_version_pairs_the_median_of_the_nearest_other_line_relatives(self):
        got, basis = self.run_step({'acme-a-4': 6.0, 'acme-a-3': 9.0, 'acme-b-1': 2.0})
        self.assertEqual(got, {'high': 6.0, 'xhigh': 12.0})
        self.assertEqual((basis['high']['method'], basis['high']['base_model'], basis['high']['version_distance']),
                         ('cross_line_median', ['acme-a-4'], 0))

    def test_with_two_same_version_pairs_the_relative_times_the_lines_median_ratio(self):
        got, basis = self.run_step({'acme-a-1': 4.0, 'acme-b-1': 2.0, 'acme-a-2': 5.0, 'acme-b-2': 4.0, 'acme-a-4': 10.0},
                                   versions={'acme-a-1': 1, 'acme-b-1': 1, 'acme-a-2': 2, 'acme-b-2': 2, 'acme-a-4': 3,
                                             'acme-b-4': 3})
        ratio = float(np.median([2.0 / 4.0, 4.0 / 5.0]))
        self.assertEqual(basis['high']['method'], 'cross_line_extrapolation')
        self.assertEqual(basis['high']['lines']['acme-a-4']['n'], 2)
        self.assertAlmostEqual(got['high'], round(10.0 * ratio, 3))

    def test_a_relative_whose_time_is_an_estimate_is_never_a_basis(self):
        got, basis = self.run_step({'acme-b-1': 2.0}, estimated={'acme-a-4': 6.0})
        self.assertEqual((got, basis), ({}, {}))

    def test_a_relative_past_the_declared_version_distance_is_not_used(self):
        got, _ = self.run_step({'acme-a-1': 6.0}, estimated={'acme-a-2': 7.0},      # acme-a-1: two versions away
                               versions={'acme-a-1': 1, 'acme-a-2': 2, 'acme-b-4': 4})
        self.assertEqual(got, {})


class ShippedScoresMatchTheirInputsTests(unittest.TestCase):
    """references/five-axis-scores.json is generated (calibration/refresh.py step 3): it must be what five_axis.build makes
    from the shipped public-measurements.json, accounts.json and plan-templates.json. A hand edit, or an input changed
    without the refresh, turns this red. `measurements_sha256` is part of the comparison, so a public-measurements.json
    changed after the build is caught too."""

    def test_the_shipped_five_axis_scores_are_rebuilt_from_the_shipped_inputs(self):
        import five_axis
        refs = account_fleet.REFERENCES
        raw = (refs / 'public-measurements.json').read_bytes()
        shipped = json.loads((refs / 'five-axis-scores.json').read_text(encoding='utf-8-sig'))
        # The run's own options, as the document records them: its time (the stamp of an accepted panel change), the
        # sources it was told to leave out, and the document it replaces (`refresh` passes it; with the same panel members it
        # carries `anchor.panel_change`, the record of an accepted change, forward).
        built = five_axis.build(json.loads(raw.decode('utf-8-sig')),
                                json.loads((refs / 'accounts.json').read_text(encoding='utf-8-sig')),
                                json.loads((refs / 'plan-templates.json').read_text(encoding='utf-8-sig')),
                                distrust=shipped['distrusted_sources'], meas_bytes=raw,
                                now=datetime.fromisoformat(shipped['generated_at']), previous=shipped)
        built = json.loads(json.dumps(built, ensure_ascii=False))

        def differences(a, b, at=''):
            if isinstance(a, dict) and isinstance(b, dict):
                return [d for k in sorted(set(a) | set(b), key=str) if (at or k != 'generated_at')
                        for d in differences(a.get(k), b.get(k), f'{at}.{k}')]
            if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
                return [d for i, (x, y) in enumerate(zip(a, b)) for d in differences(x, y, f'{at}[{i}]')]
            return [] if a == b else [f'{at}: built {str(a)[:80]} != shipped {str(b)[:80]}']

        found = differences(built, shipped)
        self.assertEqual(found[:10], [], f'five-axis-scores.json does not match its inputs ({len(found)} difference(s)): '
                                         'run `agentcompass data refresh --no-fetch`')

    def test_a_rebuild_on_another_day_writes_the_same_file(self):
        """Rule (owner, 2026-11-19): the shipped file holds facts with their dates only, so a rebuild 40 days later from the
        same inputs writes the same bytes, and a published set does not change its sha256 with the calendar. Age and
        staleness are the reader's (data_age); `generated_at` is the measurements' date, the latest date of a reading
        (measurements.inputs_date), never the clock."""
        from unittest.mock import patch
        import five_axis
        refs = account_fleet.REFERENCES
        raw = (refs / 'public-measurements.json').read_bytes()
        shipped = json.loads((refs / 'five-axis-scores.json').read_text(encoding='utf-8-sig'))
        day = datetime.fromisoformat(shipped['generated_at'])

        def written(now):
            doc = five_axis.build(json.loads(raw.decode('utf-8-sig')),
                                  json.loads((refs / 'accounts.json').read_text(encoding='utf-8-sig')),
                                  json.loads((refs / 'plan-templates.json').read_text(encoding='utf-8-sig')),
                                  distrust=shipped['distrusted_sources'], meas_bytes=raw, now=now, previous=shipped)
            return json.dumps(doc, indent=1, ensure_ascii=False) + '\n'     # as refresh.rebuild writes it
        folder = Path(tempfile.mkdtemp(prefix='five-axis-day-'))
        self.addCleanup(shutil.rmtree, folder, True)
        first = folder / 'five-axis-scores.json'
        first.write_text(written(day), encoding='utf-8', newline='\n')

        class Later(datetime):                      # the clock itself 40 days ahead, for any reader of today's date
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + timedelta(days=40)
        with patch.object(five_axis, 'datetime', Later), patch.object(data_age, 'datetime', Later):
            later = written(day + timedelta(days=40))
        self.assertEqual(later, first.read_text(encoding='utf-8'))


class EffortCoverageRuleTests(unittest.TestCase):
    """Owner 2026-11-18: every facet has a value at every effort; no inversion along effort; the downward relation from a
    small row's larger model; size in `families`. Properties of the shipped data and of the rules, never a winner."""

    @classmethod
    def setUpClass(cls):
        import five_axis
        import series
        cls.fa, cls.series = five_axis, series
        cls.doc = axis_scores.load_five_axis()
        cls.acc = json.loads((Path(__file__).resolve().parents[1] / 'references' / 'accounts.json').read_text(encoding='utf-8'))

    def facets(self):
        for m, r in self.doc['models'].items():
            for a, ax in r['axes'].items():
                for f, c in (ax.get('facets') or {}).items():
                    yield m, (a, f), c

    def test_no_inversion_along_effort_for_every_model_facet_and_time(self):
        """Rule (`user_scores.bound_along_effort`, direction `rises_with_effort`): an ability facet never decreases as effort
        rises, responsiveness never rises, Time/Task never shortens."""
        efforts = self.fa.LADDER
        for m, key, c in self.facets():
            sign = 1.0 if self.fa.rises(key) else -1.0
            vals = [sign * c['by_effort'][e] for e in efforts if e in c['by_effort']]
            self.assertEqual(vals, sorted(vals), (m, key))
        for m, r in self.doc['models'].items():
            t = r['duration_multiplier'].get('by_effort') or {}
            vals = [t[e] for e in efforts if e in t]
            self.assertEqual(vals, sorted(vals), (m, 'time'))

    def test_every_facet_with_a_value_has_every_effort_or_a_declared_refusal(self):
        """Rule (`fill_efforts`, `fill_minimal`): a facet that has a value at some effort has one at every effort of the
        ladder, `minimal` included, unless no step reaches it, which `effort_fill_refused` lists; so has Time/Task."""
        refused = {(r['model'], r['facet'], r['effort']) for r in self.doc['effort_fill_refused']}
        for m, r in self.doc['models'].items():
            t = r['duration_multiplier']
            if t.get('by_effort'):
                self.assertEqual(t['efforts_without_time'], [], m)
        for m, key, c in self.facets():
            for e in self.fa.LADDER:
                if c['by_effort'] and e not in c['by_effort']:
                    self.assertIn((m, '.'.join(key), e), refused)

    def test_measured_efforts_hold_board_values_and_filled_ones_are_declared(self):
        """A cell the bound moved is not listed as measured; every effort is either measured or listed with its method."""
        for m, key, c in self.facets():
            measured = set(c.get('measured_efforts', ()))
            self.assertTrue(measured.isdisjoint(c.get('bound_moved') or ()), (m, key))
            if c['status'] in self.fa.TARGET_STATUSES:
                filled = set(c.get('filled_efforts') or ())
                own = measured | filled | ({'high'} if c['status'] == 'interpolated' else set())
                self.assertEqual(own | set(c.get('bound_moved') or ()), set(c['by_effort']), (m, key))

    @staticmethod
    def before_size(cell, e):
        """The cell's value at `e` before the bound across size moved it (`size_order`, Scorer.bound_across_size)."""
        return next((d[e]['from'] for d in (cell.get('size_order') or {}).values() if e in d), cell['by_effort'][e])

    def test_filled_values_follow_the_declared_methods(self):
        """Rule (`fill_one`, `fill_edge`): one-sided, own value plus the published panel step (then the bound); an edge effort
        its neighbour plus the fleet's edge step; bracketed, linear in rank."""
        for m, key, c in self.facets():
            for e, how in (c.get('filled_efforts') or {}).items():
                if how['method'] == 'operating_point':
                    expected = c['by_effort'][how['from_effort']]     # its one value at every effort
                elif how['method'] in ('panel_effort_step', 'fleet_lowest_step', 'fleet_highest_step', 'fleet_off_step',
                                       'other_facets_median'):
                    anchor = self.fa.EDGES[e][0] if e in self.fa.EDGES else how['from_effort']
                    step = self.doc['effort_steps']['.'.join(key)][anchor][e]
                    self.assertEqual((how['step'], how['n']), (step['step'], step['n']), (m, key, e))
                    self.assertEqual(how['method'] != 'panel_effort_step', e in self.fa.EDGES, (m, key, e))
                    if e in self.fa.EDGES:
                        self.assertEqual(step.get('edge_method', how['method']), self.fa.edge_method(e), (m, key, e))
                        if anchor != self.fa.user_scores.LOWEST_REASONING:
                            self.assertEqual(how['from_effort'], anchor, (m, key, e))
                    expected = round(how['from_value'] + how['step'], 4)
                else:
                    (lo, vlo), (up, vup) = next(iter(how['from'].items())), next(iter(how['to'].items()))
                    expected = round(vlo + how['weight'] * (vup - vlo), 4)
                self.assertEqual(how.get('bounded_from', self.before_size(c, e)), expected, (m, key, e))

    def test_a_derived_value_follows_its_formula_and_never_exceeds_its_larger_model(self):
        """Rule (`tier_down`): value(e) = large(e) + min(gap, 0), held by the bound and never above large(e); gap the mean of
        (small - large) at High over the facets the estimate names. Only ability facets, only small rows with `tier_up`."""
        ups = self.series.tier_up(self.acc)
        seen = 0
        for m, key, c in self.facets():
            est = c.get('estimate') or {}
            if est.get('method') != 'tier_down':
                continue
            seen += 1
            big = est['base_model']
            self.assertEqual(ups.get(m), {'model': big, 'version': 'same'})
            self.assertTrue(self.fa.rises(key))
            gap_keys = [tuple(k.split('.')) for k in est['gap_facets']]
            own = self.doc['models'][m]['axes']
            theirs = self.doc['models'][big]['axes']
            gap = np.mean([self.before_size(own[a]['facets'][f], 'high') - self.before_size(theirs[a]['facets'][f], 'high')
                           for a, f in gap_keys])
            self.assertAlmostEqual(est['gap'], round(float(gap), 4), places=3)
            self.assertEqual(est['drop'], round(min(est['gap'], 0.0), 4))
            large = theirs[key[0]]['facets'][key[1]]['by_effort']
            for e, v in c['by_effort'].items():
                self.assertLessEqual(v, large[e] + 1e-9, (m, key, e))
        self.assertGreater(seen, 0)

    def size_relations(self):
        rows = self.doc['models']
        return {m: rel for m, rel in self.series.tier_up(self.acc).items()
                if m in rows and rel['model'] in rows and m not in (self.acc.get('same_model') or {})}

    def test_the_size_step_table_follows_pair_agreement(self):
        """Rule (`size_step_table`, `pair_agreement`, calibration/README.md): one value per declared same-version pair both
        measured at High (an ability facet short of pairs: per pair, the mean over the ability facets it measured); `n`
        counts pairs; the step is their median when n >= MIN_SIZE_PAIRS and all point one way, else 0 with the reason;
        Time/Task the same on the log ratio at High (ratio 1 where the pairs do not agree)."""
        same = sorted((m, r['model']) for m, r in self.size_relations().items() if r['version'] == 'same')
        axes = {m: r['axes'] for m, r in self.doc['models'].items()}

        def high(m, key):
            c = (axes[m][key[0]].get('facets') or {}).get(key[1]) or {}
            return c['by_effort']['high'] if 'high' in (c.get('measured_efforts') or ()) else None

        def per_pair(ks):
            out = []
            for s, l in same:
                ds = [high(s, k) - high(l, k) for k in ks if high(s, k) is not None and high(l, k) is not None]
                if ds:
                    out.append(float(np.mean(ds)))
            return out
        keys = [(a, f) for a, fs in self.fa.AXES.items() for f in fs]
        for k in keys:
            got = self.doc['size_steps']['.'.join(k)]
            vals, basis = per_pair([k]), 'facet'
            if len(vals) < self.fa.MIN_SIZE_PAIRS and self.fa.rises(k):
                vals, basis = per_pair([x for x in keys if self.fa.rises(x)]), 'ability_pooled'
            agree = len(vals) >= self.fa.MIN_SIZE_PAIRS and (max(vals) <= 0 or min(vals) >= 0)
            self.assertEqual((got['n'], got['basis'], got['agree']), (len(vals), basis, agree), k)
            self.assertEqual(got['step'], round(float(np.median(vals)), 4) if agree else 0.0, k)
            self.assertEqual(len(got['pairs']), got['n'], k)
            if not agree:
                self.assertIn(got['reason'], ('directions disagree', f'fewer than {self.fa.MIN_SIZE_PAIRS} pairs'), k)

        def listed(m):
            t = self.doc['models'][m]['duration_multiplier']
            b = (t.get('basis_by_effort') or {}).get('high') or {}
            return t['minutes_by_effort']['high'] if b.get('method') == 'aa' else None
        logs = [float(np.log(listed(s) / listed(l))) for s, l in same if listed(s) and listed(l)]
        got = self.doc['size_steps']['time_per_task']
        agree = len(logs) >= self.fa.MIN_SIZE_PAIRS and (max(logs) <= 0 or min(logs) >= 0)
        self.assertEqual((got['n'], got['agree']), (len(logs), agree))
        self.assertEqual(got['ratio'], round(float(np.exp(np.median(logs))), 4) if agree else 1.0)

    def test_the_edge_steps_are_the_fleet_medians_next_to_them(self):
        """Rule (`edge_step_table`, `EDGES`, round 6 item 5): per facet and edge effort, the pairs of every listing a board of
        the facet holds at both efforts (public-measurements `edge_pairs`, catalog row or not), each times the board's
        link scale: `minimal` and `ultra` their median (n >= 2), `none` `pair_agreement`; an ability facet without a pair
        the median of the other ability facets' board steps. Time/Task the neighbour times the fleet ratio at that end,
        never past the neighbour against its direction, and clipped to the fleet's measured Time/Task range where it would
        leave it (`clipped_to_fleet_range`, round 5 review B)."""
        L = self.fa.user_scores.LOWEST_REASONING
        self.assertEqual(self.fa.EDGES, {'minimal': ('low', ('low', 'medium')), 'none': (L, ('none', L)),
                                         'ultra': ('max', ('max', 'xhigh'))})
        meas = json.loads((Path(__file__).resolve().parents[1] / 'references' / 'public-measurements.json')
                          .read_text(encoding='utf-8'))
        board_steps = {}
        for fid, link in self.doc['links'].items():
            key = tuple(fid.split('.'))
            for edge, (near, _) in self.fa.EDGES.items():
                xs = [pr['difference'] * link['scales'].get(src, 1.0) for src in link['boards']
                      if src not in self.doc['distrusted_sources'] and self.fa.measurements.CAPABILITY_SOURCES.get(src) == key
                      for pr in ((meas['sources'].get(src) or {}).get('edge_pairs') or {}).get(edge, ())]
                got = (self.doc['effort_steps'].get(fid) or {}).get(near, {}).get(edge)
                if near == L and xs:
                    self.assertEqual({k: got[k] for k in ('step', 'n', 'agree')},
                                     {k: v for k, v in self.fa.pair_agreement(xs).items() if k in ('step', 'n', 'agree')},
                                     (fid, edge))
                elif near != L and len(xs) >= 2:
                    self.assertEqual((got['step'], got['n'], got['method']),
                                     (round(float(np.median(xs)), 4), len(xs), self.fa.edge_method(edge)), (fid, edge))
                else:
                    continue
                board_steps.setdefault(edge, {})[fid] = got['step']
        seen = 0
        for fid, by in self.doc['effort_steps'].items():
            for near, to in by.items():
                for edge, got in to.items():
                    if got.get('method') != 'other_facets_median':
                        continue
                    seen += 1
                    self.assertTrue(self.fa.rises(tuple(fid.split('.'))), fid)
                    others = {k: v for k, v in board_steps[edge].items() if k != fid and self.fa.rises(tuple(k.split('.')))}
                    self.assertEqual((got['facets'], got['step']), (others, round(float(np.median(list(others.values()))), 4)))
        self.assertGreater(seen, 0)
        for edge, (near, (end, nxt)) in self.fa.EDGES.items():
            if near == L:
                continue
            step = self.doc['effort_steps']['time_per_task'][near][edge]
            below = self.fa.LADDER.index(edge) < self.fa.LADDER.index(near)
            for m, r in self.doc['models'].items():
                t = r['duration_multiplier']
                b = (t.get('basis_by_effort') or {}).get(edge) or {}
                if b.get('method') == self.fa.edge_method(edge):
                    mins = t['minutes_by_effort']
                    v = mins[near] * step['ratio']
                    # never past an effort on either side (no inversion: the efforts below it, `none` included, and above)
                    lower = [mins[e] for e in self.fa.LADDER[:self.fa.LADDER.index(edge)] if e in mins]
                    upper = [mins[e] for e in self.fa.LADDER[self.fa.LADDER.index(edge) + 1:] if e in mins]
                    clamp = (lambda x: min(max(x, max(lower, default=-np.inf)), min(upper, default=np.inf)))     # noqa: E731
                    bound = clamp(v)
                    if 'clipped_to_fleet_range' in b:
                        lo, hi = b['clipped_to_fleet_range']
                        near_held = min(v, mins[near]) if below else max(v, mins[near])
                        self.assertTrue(near_held < lo or near_held > hi, (m, edge))
                        bound = clamp(min(max(near_held, lo), hi))
                    self.assertEqual(mins[edge], round(bound, 3), (m, edge))
                    if mins[edge] != round(v, 3) and 'clipped_to_fleet_range' not in b:
                        self.assertEqual(mins[b['bounded_by']], mins[edge], (m, edge))

    def test_a_size_step_cell_follows_its_formula_and_an_ability_value_never_crosses_its_larger_model(self):
        """Rule (`size_down`, `bound_across_size`, order of methods): only a facet the small row lacks after measurement,
        series, fit and (same version only) its own gap; value(high) = large(high) + move, move = min(step, 0) for an
        ability facet, the step itself for responsiveness (zero where the pairs disagree); an ability value is never above
        the larger model's at any effort; Time/Task is the larger model's times the step's ratio."""
        rel = self.size_relations()
        seen = set()
        for m, key, c in self.facets():
            est = c.get('estimate') or {}
            if est.get('method') not in ('size_step', 'tier_down'):
                continue
            big, up = est['base_model'], self.fa.rises(key)
            self.assertEqual(rel[m]['model'], big, m)
            large = self.doc['models'][big]['axes'][key[0]]['facets'][key[1]]['by_effort']
            if up:
                for e, v in c['by_effort'].items():
                    self.assertLessEqual(v, large[e] + 1e-9, (m, key, e, v, large[e]))
            if est['method'] == 'tier_down':
                continue
            seen.add(m)
            self.assertEqual(c['measured_efforts'], [], (m, key))
            step = self.doc['size_steps']['.'.join(key)]
            self.assertEqual((est['step'], est['n'], est['agree'], est['version']),
                             (step['step'], step['n'], step['agree'], rel[m]['version']))
            move = min(step['step'], 0.0) if up else step['step']
            self.assertEqual(est['move'], round(move, 4))
            if 'high' not in (c.get('size_order') or {}).get('held', {}):
                self.assertAlmostEqual(c['by_effort']['high'], large['high'] + move, places=3, msg=(m, key))
            others = {(a, f): x for a, ax in self.doc['models'][m]['axes'].items() for f, x in (ax.get('facets') or {}).items()}
            if rel[m]['version'] == 'nearest' or not up:
                self.assertNotIn('tier_down', {(x.get('estimate') or {}).get('method') for k2, x in others.items()
                                               if rel[m]['version'] == 'nearest'}, m)
            else:   # same version, ability: only when the row's own gap could not be read (no tier_down on the row)
                self.assertNotIn('tier_down', {(x.get('estimate') or {}).get('method') for x in others.values()}, m)
        self.assertTrue(seen)
        tseen = 0
        for m, r in self.doc['models'].items():
            t = r['duration_multiplier']
            for e, b in (t.get('basis_by_effort') or {}).items():
                if b.get('method') != 'size_step':
                    continue
                tseen += 1
                large = self.doc['models'][b['base_model']]['duration_multiplier']['minutes_by_effort']
                self.assertEqual(b['ratio'], self.doc['size_steps']['time_per_task']['ratio'])
                self.assertAlmostEqual(t['minutes_by_effort'][e], large[e] * b['ratio'], places=2)
        self.assertGreater(tseen, 0)

    def test_an_own_ability_median_cell_is_the_median_of_the_models_other_ability_facets(self):
        """Rule (`own_ability_median`, last in the order): at each effort, the median of the ability facets the estimate
        names (at least MIN_OWN_FACETS), bounded along effort, never used for responsiveness and never where another method
        gave a value."""
        seen = 0
        for m, key, c in self.facets():
            est = c.get('estimate') or {}
            if est.get('method') != 'own_ability_median':
                continue
            seen += 1
            self.assertTrue(self.fa.rises(key))
            self.assertGreaterEqual(len(est['facets']), self.fa.MIN_OWN_FACETS)
            axes = self.doc['models'][m]['axes']
            used = [axes[a]['facets'][f]['by_effort'] for a, f in (k.split('.') for k in est['facets'])]
            self.assertNotIn('.'.join(key), est['facets'])
            # the medians, then the no-inversion bound along effort (bound_curve), as for every curve
            by = self.fa.bound_curve({e: float(np.median([u[e] for u in used if e in u])) for e in c['by_effort']}, True)
            for e, v in c['by_effort'].items():
                self.assertAlmostEqual(v, by[e], places=3, msg=(m, key, e))
        self.assertGreater(seen, 0)

    def test_relatives_are_the_declared_maker_and_size_and_a_line_for_speed(self):
        """Rule (series.groups, owner 2026-11-19 round 6 item 4): ability relatives are the rows of the same declared maker
        and size, every line; speed relatives also the same line; a `small` row is never a relative of a `full` one; no
        name, alias, preview or superseded pair joins two rows."""
        for scope in ('ability', 'line'):
            groups, ident = self.series.groups(self.acc, scope), self.series.identity(self.acc)
            for k, sibs in groups.items():
                for j in sibs:
                    self.assertEqual((ident[k]['maker'], ident[k]['size']), (ident[j]['maker'], ident[j]['size']), (k, j))
                    if scope == 'line':
                        self.assertEqual(ident[k]['line'], ident[j]['line'], (k, j))
        row = lambda pattern, line, size: {'pattern': pattern, 'models': [], 'maker': 'acme', 'product': 'acme', 'line': line, 'size': size}  # noqa: E731
        synthetic = {'models': {m: {} for m in ('fam-1', 'fam-1-preview', 'fam-1-mini', 'fam-pro-1', 'zed-1', 'zed-2')},
                     'aliases': {'zed-2': 'fam-1'}, 'superseded': {'zed-1': {'by': 'zed-2'}},
                     'families': {'rows': [row('^fam.*mini', 'fam', 'small'),
                                           row('^fam-pro', 'pro', 'full'), row('^fam', 'fam', 'full'),
                                           {**row('^zed-1', 'zed', 'full'), 'maker': 'other', 'product': 'other'},
                                           {**row('^zed', 'zed', 'full'), 'maker': 'third', 'product': 'third'}]}}
        ability, line = self.series.groups(synthetic, 'ability'), self.series.groups(synthetic, 'line')
        self.assertEqual(ability['fam-1'], ['fam-1-preview', 'fam-pro-1'])      # same maker and size, every line
        self.assertEqual(line['fam-1'], ['fam-1-preview'])                      # the bare name decides the row
        self.assertNotIn('zed-2', ability['fam-1'])                             # an alias joins nothing
        self.assertEqual((ability['fam-1-mini'], line['fam-pro-1']), ([], []))  # small never joins full; pro its own line
        self.assertEqual((ability['zed-1'], ability['zed-2']), ([], []))        # a superseded pair across makers: no join
        synthetic['families']['rows'][0]['size'] = 'full'
        self.assertEqual(self.series.groups(synthetic, 'line')['fam-1'], ['fam-1-mini', 'fam-1-preview'])
        with self.assertRaises(ValueError):
            self.series.groups({**synthetic, 'families': {'rows': [{'pattern': '^fam', 'models': [], 'size': 'full'}]}})

    def test_lines_of_one_product_only_are_relatives_and_version_distance_is_counted_within_it(self):
        """Rule (series.groups, owner 2026-11-19 round 7): relatives of another line are rows of the same maker and the
        same product (an open-weights product of the same maker is another product); version distance counts the
        product's own versions only; a families row without a product stops the build."""
        row = lambda pattern, product, line: {'pattern': pattern, 'models': [], 'maker': 'acme', 'product': product,  # noqa: E731
                                              'line': line, 'size': 'small'}
        acc = {'models': {m: {} for m in ('main-a-3', 'main-b-2', 'open-1', 'open-2')},
               'series_versions': {'versions': {'main-a-3': 3, 'main-b-2': 2, 'open-1': 1, 'open-2': 2}},
               'families': {'rows': [row('^main-a', 'Main', 'a'), row('^main-b', 'Main', 'b'), row('^open', 'Open', 'open')]}}
        ability = self.series.groups(acc, 'ability')
        self.assertEqual(ability['main-a-3'], ['main-b-2'])        # the other line of its product, not the open product
        self.assertEqual(ability['open-2'], ['open-1'])
        self.assertEqual(self.series.distance('main-a-3', 'main-b-2', ability['main-a-3'], self.series.versions(acc)), 1)
        with self.assertRaisesRegex(ValueError, 'product'):
            self.series.groups({**acc, 'families': {'rows': [{**row('^main', 'Main', 'a'), 'product': ''}]}})

    def test_another_lines_relative_is_used_only_up_to_the_declared_distance_and_after_the_own_line(self):
        """Rule (series.nearest `line_of`, `other_line_up_to`): at one version distance the own line comes first; a
        relative of another line beyond the cap, or without a declared version, is no candidate."""
        versions = {'t': 3.0, 'own': 2.0, 'other': 2.0, 'far': 1.0}
        line_of = {'t': 'a', 'own': 'a', 'other': 'b', 'far': 'b', 'blank': 'b'}
        rows = ['own', 'other', 'far', 'blank']
        got = self.series.nearest('t', rows, rows, versions, set(), line_of=line_of, other_line_up_to=1)
        self.assertEqual(got, [(1, ['own']), (1, ['other'])])
        got = self.series.nearest('t', rows, rows, versions, set(), line_of=line_of, other_line_up_to=2)
        self.assertEqual(got, [(1, ['own']), (1, ['other']), (2, ['far'])])



class CrossSizeAndEdgeRuleTests(unittest.TestCase):
    """The size, edge and last-method rules on made-up models and values (owner 2026-11-18, round 3): each test breaks when
    the line of code that states its rule is broken, whatever the shipped data happens to hold."""

    @classmethod
    def setUpClass(cls):
        import five_axis
        cls.fa = five_axis

    def scorer(self, size_of=None, size_steps=None, steps=None, acc=None):
        s = self.fa.Scorer.__new__(self.fa.Scorer)
        s.acc = acc or {'accounts': {}, 'dispatch_efforts': {}}
        s.size_of, s.size_steps, s.steps = size_of or {}, size_steps or {}, steps or {}
        s.same, s.speed, s.fill_refused, s.points, s.response_floor = {}, {}, [], {}, {}
        s.meas, s.links, s.distrust, s.entries = {'sources': {}}, {}, set(), frozenset()
        return s

    def board(self, sc, key, src, pairs, scale=1.0):
        """Board `src` of facet `key` with its listings' edge pairs ({edge: [difference]}) and its link scale."""
        sc.meas['sources'][src] = {'edge_pairs': {e: [{'listed': f'l{i}', 'efforts': [], 'difference': d}
                                                      for i, d in enumerate(v)] for e, v in pairs.items()}}
        link = sc.links.setdefault('.'.join(key), {'boards': [], 'scales': {}})
        link['boards'].append(src)
        link['scales'][src] = scale

    @staticmethod
    def rec(by, status='measured', measured=None, estimate=None):
        out = {'value': by.get('high'), 'by_effort': dict(by), 'status': status, 'sources': {},
               'measured_efforts': list(by) if measured is None else list(measured)}
        if estimate:
            out['estimate'] = estimate
        return out

    EXE, RESP = ('coding', 'execution'), ('tool_operation', 'responsiveness')

    def step(self, value):
        return {'step': value, 'median': value, 'n': 2, 'agree': True, 'basis': 'facet', 'pairs': []}

    def test_pair_agreement_uses_the_median_only_when_every_pair_points_one_way(self):
        pa = self.fa.pair_agreement
        self.assertEqual((pa([-0.2, -0.4])['step'], pa([-0.2, -0.4])['agree']), (-0.3, True))
        self.assertEqual(pa([0.2, 0.6])['step'], 0.4)
        self.assertEqual((pa([-0.5, 0.1])['step'], pa([-0.5, 0.1])['reason']), (0.0, 'directions disagree'))
        self.assertEqual((pa([-0.5])['step'], pa([-0.5])['reason']), (0.0, f'fewer than {self.fa.MIN_SIZE_PAIRS} pairs'))
        self.assertEqual((pa([])['step'], pa([])['n']), (0.0, 0))

    def test_the_size_step_table_counts_pairs_and_pools_ability_facets_per_pair(self):
        """Two same-version pairs: execution measured by both (they disagree: step 0); faithfulness by none, so pooled per
        pair over the ability facets each pair measured (n = 2 pairs, 3 readings); responsiveness by one pair (step 0)."""
        cw = ('creativity', 'creative_writing')
        facets = {
            's1': {self.EXE: self.rec({'high': -1.0}), cw: self.rec({'high': -2.0}), self.RESP: self.rec({'high': 0.5})},
            'l1': {self.EXE: self.rec({'high': 0.0}), cw: self.rec({'high': 0.0}), self.RESP: self.rec({'high': 0.0})},
            's2': {self.EXE: self.rec({'high': 0.4})}, 'l2': {self.EXE: self.rec({'high': 0.0})}}
        sc = self.scorer(size_of={'s1': {'model': 'l1', 'version': 'same'}, 's2': {'model': 'l2', 'version': 'same'}})
        keys = [self.EXE, ('coding', 'faithfulness'), cw, self.RESP]
        got = sc.size_step_table(facets, keys)
        self.assertEqual((got['coding.execution']['step'], got['coding.execution']['reason'], got['coding.execution']['n']),
                         (0.0, 'directions disagree', 2))
        pooled = got['coding.faithfulness']
        self.assertEqual((pooled['basis'], pooled['n'], pooled['differences']), ('ability_pooled', 2, 3))
        self.assertEqual([p['difference'] for p in pooled['pairs']], [-1.5, 0.4])
        self.assertEqual((pooled['step'], pooled['agree']), (0.0, False))
        self.assertEqual((got['tool_operation.responsiveness']['step'], got['tool_operation.responsiveness']['n']), (0.0, 1))
        facets['s2'][self.EXE]['by_effort']['high'] = -0.4                     # now both pairs point down
        got = sc.size_step_table(facets, keys)
        self.assertEqual((got['coding.execution']['step'], got['coding.execution']['agree']), (-0.7, True))

    def test_size_down_moves_ability_never_up_and_responsiveness_by_the_step_in_either_direction(self):
        for step, ability_move in ((0.3, 0.0), (-0.3, -0.3)):
            for key, move in ((self.EXE, ability_move), (self.RESP, step)):
                facets = {'small': {}, 'big': {key: self.rec({'low': -1.0, 'high': 0.0, 'max': 0.5} if key == self.EXE
                                                             else {'low': 1.0, 'high': 0.0, 'max': -0.5})}}
                sc = self.scorer(size_of={'small': {'model': 'big', 'version': 'nearest'}},
                                 size_steps={'.'.join(key): self.step(step)})
                sc.size_down(facets, [key], 'nearest')
                got = facets['small'][key]
                self.assertEqual(got['estimate']['move'], move, (key, step))
                for e, v in facets['big'][key]['by_effort'].items():
                    self.assertAlmostEqual(got['by_effort'][e], v + move, places=4, msg=(key, step, e))

    def test_tier_down_drops_by_a_negative_gap_and_never_raises_by_a_positive_one(self):
        cw = ('creativity', 'creative_writing')
        for gap, drop in ((0.4, 0.0), (-0.4, -0.4)):
            facets = {'small': {cw: self.rec({'high': gap})},
                      'big': {cw: self.rec({'high': 0.0}), self.EXE: self.rec({'low': -1.0, 'high': 0.0, 'max': 0.5})}}
            sc = self.scorer(size_of={'small': {'model': 'big', 'version': 'same'}})
            sc.tier_down(facets, [cw, self.EXE])
            got = facets['small'][self.EXE]
            self.assertEqual((got['estimate']['method'], got['estimate']['drop']), ('tier_down', drop), gap)
            for e, v in facets['big'][self.EXE]['by_effort'].items():
                self.assertAlmostEqual(got['by_effort'][e], v + drop, places=4, msg=(gap, e))

    FAMILIES = {'rows': [
        {'pattern': '^big', 'models': [], 'maker': 'acme', 'product': 'P', 'line': 'a', 'size': 'full'},
        {'pattern': '^twin', 'models': [], 'maker': 'acme', 'product': 'P', 'line': 'b', 'size': 'full'},
        {'pattern': '^small', 'models': [], 'maker': 'acme', 'product': 'P', 'line': 'a', 'size': 'small'},
        {'pattern': '^other', 'models': [], 'maker': 'acme', 'product': 'Q', 'line': 'q', 'size': 'small'}]}

    def dom_scorer(self, models, versions, entries=(), up=None):
        """A scorer over made-up rows of maker acme: `big*` and `twin*` full (product P, lines a and b), `small*` small (P,
        line a), `other*` small of product Q; `up` {small row: (its declared larger model, version)} (series.tier_up)."""
        fams = {'rows': [dict(r) for r in self.FAMILIES['rows']]}
        for small, (big, version) in (up or {}).items():
            fams['rows'].insert(0, {'pattern': f'^{small}$', 'models': [small], 'maker': 'acme', 'product': 'P',
                                    'line': 'a', 'size': 'small', 'tier_up': f'^{big}$', 'tier_up_version': version})
            fams['rows'].insert(0, {'pattern': f'^{big}$', 'models': [big], 'maker': 'acme', 'product': 'P', 'line': 'a',
                                    'size': 'full'})
        sc = self.scorer(acc={'accounts': {}, 'dispatch_efforts': {}, 'models': {m: {} for m in models},
                              'families': fams, 'series_versions': {'versions': versions}})
        sc.versions, sc.entries = dict(versions), frozenset(entries)
        return sc

    def test_only_a_strictly_smaller_row_of_the_same_maker_and_product_not_newer_is_dominated(self):
        """Rule (`dominance`, owner 2026-11-19 round 7 as corrected): A is dominated by B when they are the same maker and
        product, A's size is strictly smaller and A's version not newer (both declared, or B is A's declared larger model
        of the same version). Equal size never dominates, version alone never does, nor another product."""
        rows = ['big-2', 'big-3', 'twin-1', 'small-2', 'small-3', 'other-1', 'smallx', 'smally', 'bigx', 'bigy']
        sc = self.dom_scorer(rows, {'big-2': 2, 'big-3': 3, 'twin-1': 1, 'small-2': 2, 'small-3': 3, 'other-1': 1},
                             up={'smallx': ('bigx', 'same'), 'smally': ('bigy', 'nearest')})
        got = sc.dominance(rows)
        self.assertEqual(got['small-2'], ['big-2', 'big-3'])           # small, not newer than either full row of P
        self.assertEqual(got['small-3'], ['big-3'])                    # newer than big-2: not dominated by it
        self.assertEqual(got['big-2'], [])                             # version alone (big-3) never dominates
        self.assertEqual(got['twin-1'], [])                            # equal size, any line
        self.assertEqual(got['other-1'], [])                           # another product of the same maker
        self.assertEqual(got['smallx'], ['bigx'])                      # no versions: the declared same-version larger model
        self.assertEqual(got['smally'], [])                            # `nearest` does not order the versions

    def test_a_dominated_estimate_is_held_and_a_measurement_stays_along_effort(self):
        """Rule (`bound_across_size`): a dominated estimate is bounded by the dominating value (`size_order.held`); the
        record is then bounded along effort with its measured efforts kept."""
        facets = {'small-2': {self.EXE: self.rec({'low': -0.5, 'high': 0.4, 'max': 0.9}, 'estimated', ['low'])},
                  'big-2': {self.EXE: self.rec({'low': -1.0, 'high': 0.0, 'max': 0.5})}}
        sc = self.dom_scorer(facets, {'small-2': 2, 'big-2': 2})
        sc.bound_across_size(facets)
        got = facets['small-2'][self.EXE]
        self.assertEqual(got['by_effort'], {'low': -0.5, 'high': 0.0, 'max': 0.5})
        self.assertEqual(got['size_order']['held']['high'], {'model': 'big-2', 'from': 0.4})
        self.assertEqual(sc.measured_inversions, [{'model': 'small-2', 'dominated_by': 'big-2', 'facet': 'coding.execution',
                                                   'effort': 'low', 'value': -0.5, 'dominating_value': -1.0,
                                                   'measured': True, 'dominating_measured': True}])

    def test_a_dominating_estimate_is_raised_to_a_dominated_measurement(self):
        facets = {'small-2': {self.EXE: self.rec({'high': 0.4})},
                  'big-2': {self.EXE: self.rec({'high': 0.0, 'max': 0.2}, 'estimated', [])}}
        sc = self.dom_scorer(facets, {'small-2': 2, 'big-2': 2})
        sc.bound_across_size(facets)
        self.assertEqual(facets['big-2'][self.EXE]['by_effort'], {'high': 0.4, 'max': 0.4})
        self.assertEqual(facets['big-2'][self.EXE]['size_order']['raised']['high'], {'model': 'small-2', 'from': 0.0})
        self.assertEqual(facets['small-2'][self.EXE]['by_effort'], {'high': 0.4})

    def test_two_measurements_out_of_the_size_order_are_kept_and_listed(self):
        facets = {'small-2': {self.EXE: self.rec({'high': 0.4})}, 'big-2': {self.EXE: self.rec({'high': 0.0})}}
        sc = self.dom_scorer(facets, {'small-2': 2, 'big-2': 2})
        sc.bound_across_size(facets)
        self.assertEqual((facets['small-2'][self.EXE]['by_effort'], facets['big-2'][self.EXE]['by_effort']),
                         ({'high': 0.4}, {'high': 0.0}))
        self.assertEqual(sc.measured_inversions, [{'model': 'small-2', 'dominated_by': 'big-2', 'facet': 'coding.execution',
                                                   'effort': 'high', 'value': 0.4, 'dominating_value': 0.0,
                                                   'measured': True, 'dominating_measured': True}])

    def test_an_estimate_moves_within_what_its_own_measurements_allow_and_the_rest_is_listed_or_refused(self):
        """Rule (`bound_across_size` with `effort_floor`): a dominating estimate is raised to the floor a dominated row's
        measured efforts give it (not only to a measurement at that effort); a dominated estimate is held no lower than
        its own floor; what two measurements keep out of order is listed, and an estimate its own measurements keep out
        of order stops the build, naming the pair (round 8)."""
        facets = {'small-2': {self.EXE: self.rec({'low': 0.3, 'high': 0.5, 'xhigh': 0.6}, 'measured', ['low', 'high'])},
                  'big-2': {self.EXE: self.rec({'low': -0.1, 'high': 0.0, 'xhigh': 0.2}, 'measured', ['low', 'high'])}}
        sc = self.dom_scorer(facets, {'small-2': 2, 'big-2': 2})
        sc.bound_across_size(facets)
        small, big = facets['small-2'][self.EXE]['by_effort'], facets['big-2'][self.EXE]['by_effort']
        self.assertEqual(big, {'low': -0.1, 'high': 0.0, 'xhigh': 0.5})         # xhigh raised to the floor small's high gives
        self.assertEqual(small, {'low': 0.3, 'high': 0.5, 'xhigh': 0.5})        # held, never below its measured high
        self.assertEqual([(i['effort'], i['measured'], i['dominating_measured']) for i in sc.measured_inversions],
                         [('low', True, True), ('high', True, True)])
        facets = {'small-2': {self.EXE: self.rec({'low': 0.3, 'medium': 0.6}, 'measured', ['low'])},
                  'big-2': {self.EXE: self.rec({'low': 0.0, 'medium': 0.1})}}
        sc = self.dom_scorer(facets, {'small-2': 2, 'big-2': 2})
        with self.assertRaisesRegex(ValueError, r'small-2 coding\.execution at medium 0\.3 \(estimated\) above big-2 0\.1 '
                                                r'\(measured\)'):
            sc.bound_across_size(facets)

    def test_a_newer_smaller_row_and_an_older_larger_one_are_left_as_they_are(self):
        facets = {'small-3': {self.EXE: self.rec({'high': 0.4}, 'estimated', [])},
                  'big-2': {self.EXE: self.rec({'high': 0.0}, 'estimated', [])}}
        sc = self.dom_scorer(facets, {'small-3': 3, 'big-2': 2})
        sc.bound_across_size(facets)
        self.assertEqual((facets['small-3'][self.EXE]['by_effort'], facets['big-2'][self.EXE]['by_effort']),
                         ({'high': 0.4}, {'high': 0.0}))

    def test_a_model_entry_row_is_bounded_by_release_rows_and_never_moves_one(self):
        """Rule (frozen fits, round 6; round 8): an entry is held by a release row, and an entry dominating a release row
        is raised to its value, measured or estimated; an entry never moves a release row, so where only that would
        restore the order (the entry's measurement above a release estimate, or a release estimate above an entry's
        measurement) the build stops, naming the pair."""
        def run(entries, small, big):
            facets = {'small-2': {self.EXE: self.rec({'high': 0.4}, *small)},
                      'big-2': {self.EXE: self.rec({'high': 0.0}, *big)}}
            self.dom_scorer(facets, {'small-2': 2, 'big-2': 2}, entries).bound_across_size(facets)
            return facets['small-2'][self.EXE], facets['big-2'][self.EXE]
        small, _ = run(['small-2'], ('estimated', []), ())                           # the entry is held
        self.assertEqual(small['by_effort']['high'], 0.0)
        for entries, est in ((['big-2'], ('estimated', [])), ([], ('estimated', []))):
            small, big = run(entries, est, est)
            with self.subTest(entries=entries):
                if entries:                                  # an entry over a release estimate: raised to its value
                    self.assertEqual((small['by_effort']['high'], big['by_effort']['high']), (0.4, 0.4))
                    self.assertEqual(big['size_order']['raised']['high'], {'model': 'small-2', 'from': 0.0})
                else:                                        # two release estimates: the dominated one is held
                    self.assertEqual((small['by_effort']['high'], big['by_effort']['high']), (0.0, 0.0))
        for entries, small_how, big_how in ((['small-2'], (), ('estimated', [])), (['big-2'], ('estimated', []), ())):
            with self.subTest(entries=entries), self.assertRaisesRegex(ValueError, 'small-2 coding.execution at high 0.4'):
                run(entries, small_how, big_how)

    def test_the_bound_across_size_reads_no_order_of_the_rows_and_leaves_speed_alone(self):
        """The raise reads measurements only and a dominating row is never dominated, so the result is one in any order
        of the rows; responsiveness has no bound across size."""
        def build(order):
            base = {'small-2': {self.EXE: self.rec({'high': 0.6}, 'estimated', []),
                                self.RESP: self.rec({'high': 0.4}, 'estimated', [])},
                    'small-1': {self.EXE: self.rec({'high': 0.3})},
                    'big-2': {self.EXE: self.rec({'high': 0.0}, 'estimated', []), self.RESP: self.rec({'high': 0.0})},
                    'big-3': {self.EXE: self.rec({'high': 0.5})}}
            facets = {m: base[m] for m in order}
            sc = self.dom_scorer(facets, {'small-1': 1, 'small-2': 2, 'big-2': 2, 'big-3': 3})
            sc.bound_across_size(facets)
            return {m: {k: r['by_effort'] for k, r in fs.items()} for m, fs in facets.items()}
        one = build(['small-2', 'small-1', 'big-2', 'big-3'])
        self.assertEqual(one, build(['big-3', 'big-2', 'small-1', 'small-2']))
        self.assertEqual(one['big-2'][self.EXE], {'high': 0.3})          # raised to small-1's measurement
        self.assertEqual(one['small-2'][self.EXE], {'high': 0.3})        # then held by big-2 (below big-3's 0.5)
        self.assertEqual(one['small-2'][self.RESP], {'high': 0.4})       # faster than the larger: kept

    def test_along_effort_an_estimate_moves_and_a_measurement_does_not(self):
        """Rule (`bound_pinned`, round 7 item 5, the effort direction): the measured efforts keep their values when they
        are in order and the estimates are bounded around them; two measurements out of order fall back to the anchor
        rule."""
        self.assertEqual(self.fa.bound_pinned({'none': -1.5, 'low': -1.8, 'high': 0.0}, ['none', 'high']),
                         {'none': -1.5, 'low': -1.5, 'high': 0.0})
        self.assertEqual(self.fa.bound_pinned({'none': -1.5, 'low': -1.8, 'high': 0.0}, ['none', 'low', 'high']),
                         self.fa.bound_curve({'none': -1.5, 'low': -1.8, 'high': 0.0}))

    def test_complete_bounds_a_filled_small_value_at_its_larger_models(self):
        """End to end: the larger model runs on a ladder of its own (low, xhigh, max), so the fill interpolates its xhigh at
        the midpoint, the small model's at three quarters; without the bound after the fill the small one ends above."""
        facets = {'small': {}, 'big': {self.EXE: self.rec({'low': -1.0, 'max': 1.0})}}
        # the same-version size step comes before the fill (estimation_order); the nearest-version one after it
        sc = self.dom_scorer(facets, {'small': 2, 'big': 2})
        sc.size_of = {'small': {'model': 'big', 'version': 'same'}}
        sc.acc['dispatch_efforts'] = {'h': {'big': ['low', 'xhigh', 'max']}}
        sc.size_step_table = lambda f, k: {'coding.execution': self.step(-0.1)}
        sc.complete(facets, [self.EXE], ['small', 'big'])
        small, big = facets['small'][self.EXE]['by_effort'], facets['big'][self.EXE]['by_effort']
        self.assertEqual(big['xhigh'], 0.0)
        for e, v in small.items():
            self.assertLessEqual(v, big[e] + 1e-9, e)
        self.assertEqual(list(facets['small'][self.EXE]['size_order']['held']), ['xhigh'])

    def test_an_edge_effort_is_its_neighbour_plus_the_fleet_step_held_by_no_inversion(self):
        """Rule (`edge_step_table`, round 6 item 5): the step is the median over every listing a board of the facet holds
        at both efforts of the pair (catalog row or not), each on the facet's linked scale (the board's link scale)."""
        sc = self.scorer()
        # three listings with low - medium -0.5 and max - xhigh below zero: a falling step for an ability
        self.board(sc, self.EXE, 'aa.terminalbench_4', {'minimal': [-0.5, -0.5, -0.5], 'ultra': [-0.1, -0.2, -0.3]})
        table = sc.edge_step_table([self.EXE])
        self.assertEqual(table['coding.execution']['minimal']['step'], -0.5)
        self.assertEqual(table['coding.execution']['ultra'], {'step': -0.2, 'n': 3, 'sample': 'board listings',
                                                               'boards': {'aa.terminalbench_4': 3},
                                                               'method': 'fleet_highest_step'})
        self.board(sc, self.EXE, 'arena.agent_code', {'ultra': [-0.4, -0.4, -0.4]}, scale=2.0)   # on the facet's scale: -0.8
        self.assertEqual(sc.edge_step_table([self.EXE])['coding.execution']['ultra']['step'], -0.55)
        sc.distrust = {'arena.agent_code'}                                                # a distrusted board gives none
        self.assertEqual(sc.edge_step_table([self.EXE])['coding.execution']['ultra']['n'], 3)
        for key, by in table.items():
            for edge, st in by.items():
                sc.steps.setdefault(key, {}).setdefault(self.fa.EDGES[edge][0], {})[edge] = st
        rec = self.rec({'low': -1.0, 'high': 0.0, 'max': 0.6})
        for edge in ('minimal', 'ultra'):
            self.assertEqual(sc.fill_edge('x', self.EXE, rec, edge), [])
        self.assertEqual(rec['by_effort']['minimal'], -1.5)
        self.assertEqual((rec['by_effort']['ultra'], rec['filled_efforts']['ultra']['bounded_from']), (0.6, 0.4))
        # no fleet model measured at none and a reasoning effort: none is refused, not guessed
        self.assertEqual(sc.fill_edge('x', self.EXE, rec, 'none')[0]['reason'],
                         'no board listing at none and at a reasoning effort')
        self.assertEqual(list(rec['by_effort']), ['minimal', 'low', 'high', 'max', 'ultra'])
        refused = self.scorer().fill_edge('x', self.EXE, self.rec({'max': 0.0}), 'ultra')
        self.assertEqual(refused[0]['reason'], 'fewer than 2 board listings at max and xhigh')

    def test_an_ability_facet_no_board_gives_a_pair_takes_the_other_ability_facets_median(self):
        """Rule (`edge_step_table` last resort, round 6 item 5): an ability facet without a pair on any board takes the
        median of the other ability facets' board steps for that edge, marked and naming them; responsiveness never."""
        faith, design = ('coding', 'faithfulness'), ('aesthetics', 'design')
        sc = self.scorer()
        self.board(sc, self.EXE, 'aa.terminalbench_4', {'minimal': [-0.4, -0.4]})
        self.board(sc, design, 'designarena.svg', {'minimal': [-0.2, -0.2]})
        table = sc.edge_step_table([self.EXE, faith, design, self.RESP])
        self.assertEqual(table['coding.faithfulness']['minimal'],
                         {'step': -0.3, 'n': 2, 'sample': 'other ability facets', 'method': 'other_facets_median',
                          'facets': {'coding.execution': -0.4, 'aesthetics.design': -0.2}, 'edge_method': 'fleet_lowest_step'})
        self.assertNotIn('tool_operation.responsiveness', table)

    def test_a_same_model_copy_carries_the_efforts_its_model_was_refused(self):
        """Rule (`copy_same_model`, round 5): a copied facet is the model's facet as it stands, so an effort refused on the
        model is refused on the copy too (listed in `fill_refused` with `same_model`); an effort the copy has is not."""
        sc = self.scorer()
        sc.same = {'copy': {'model': 'base', 'carries': ['coding']}}
        sc.fill_refused = [{'model': 'base', 'facet': 'coding.execution', 'effort': 'none', 'reason': 'r'},
                           {'model': 'base', 'facet': 'aesthetics.design', 'effort': 'none', 'reason': 'r'}]
        facets = {'base': {self.EXE: self.rec({'low': -1.0, 'high': 0.0}), ('aesthetics', 'design'): self.rec({'high': 0.0})},
                  'copy': {}}
        sc.copy_same_model(facets, ['copy'])
        self.assertEqual(facets['copy'][self.EXE]['estimate']['method'], 'same_model')
        self.assertNotIn(('aesthetics', 'design'), facets['copy'])                  # not carried: no copy, no refusal
        self.assertEqual([r for r in sc.fill_refused if r['model'] == 'copy'],
                         [{'model': 'copy', 'facet': 'coding.execution', 'effort': 'none', 'reason': 'r', 'same_model': 'base'}])

    def test_none_is_the_lowest_measured_reasoning_effort_plus_the_fleets_own_off_step(self):
        """Rule (`edge_step_table`, `fill_edge`, user_scores.edge_anchor; round 5 review B): `none` is anchored at the
        model's lowest measured reasoning effort (High when it measured none below it) and moved by the fleet's own
        (none - lowest measured reasoning effort) pairs under pair_agreement: the median when n >= 2 and every pair points
        one way, else zero with the reason; a measured none stays as measured."""
        L = self.fa.user_scores.LOWEST_REASONING
        self.assertEqual(self.fa.EDGES['none'], (L, ('none', L)))
        sc = self.scorer()
        # two listings: none - low -1.1 and none - medium -1.3 (each against its lowest listed reasoning effort)
        self.board(sc, self.EXE, 'aa.terminalbench_4', {'none': [-1.1, -1.3]})
        got = sc.edge_step_table([self.EXE])['coding.execution']['none']
        self.assertEqual((got['step'], got['n'], got['agree'], got['method']), (-1.2, 2, True, 'fleet_off_step'))
        sc.steps = {'coding.execution': {L: {'none': got}}}
        low_first = self.rec({'low': -1.0, 'high': 0.0})
        self.assertEqual(sc.fill_edge('x', self.EXE, low_first, 'none'), [])
        self.assertEqual((low_first['by_effort']['none'], low_first['filled_efforts']['none']['from_effort']), (-2.2, 'low'))
        high_only = self.rec({'high': 0.0, 'max': 0.6})
        sc.fill_edge('y', self.EXE, high_only, 'none')
        self.assertEqual((high_only['by_effort']['none'], high_only['filled_efforts']['none']['from_effort']), (-1.2, 'high'))
        measured = self.rec({'none': -0.5, 'high': 0.0})
        self.assertEqual(sc.fill_edge('z', self.EXE, measured, 'none'), [])
        self.assertEqual(measured['by_effort']['none'], -0.5)
        sc.meas['sources']['aa.terminalbench_4']['edge_pairs']['none'][1]['difference'] = 0.5     # they disagree: no step
        got = sc.edge_step_table([self.EXE])['coding.execution']['none']
        self.assertEqual((got['step'], got['agree'], got['reason']), (0.0, False, 'directions disagree'))

    def test_an_artificial_analysis_entry_keeps_every_effort_of_the_one_order(self):
        """Rule (Scorer.aa_rows, round 6 item 5): none, minimal and ultra readings are kept; a label-less listing reads
        at the effort `default` names."""
        sc = self.scorer()
        sc.readings = {'default': 'high'}
        got = sc.aa_rows({'z_by_effort': {'none': -2.0, 'minimal': -1.0, 'default': 0.0, 'ultra': 1.0, 'other': 9.0}})
        self.assertEqual(got, {'none': -2.0, 'minimal': -1.0, 'high': 0.0, 'ultra': 1.0})

    def test_an_operating_point_that_cannot_reason_reads_an_estimates_none_point(self):
        """Rule (`flatten_point`, round 6 item 5): an operating point row that cannot reason and has no reading of its own
        reads its estimated curve at `none` (the reasoning relative's off point); a measured one reads its own reading."""
        sc = self.scorer()
        sc.readings, sc.points = {'default': 'high'}, {'p': {'reasoning': False}, 'r': {'reasoning': True}}
        est = self.rec({'none': -2.0, 'low': -1.0, 'high': 0.0}, status='estimated', measured=[])
        sc.flatten_point('p', est)
        self.assertEqual(set(est['by_effort'].values()), {-2.0})
        own = self.rec({'none': -2.0, 'high': 0.0}, measured=['high'])
        sc.flatten_point('p', own)
        self.assertEqual(set(own['by_effort'].values()), {0.0})
        reasons = self.rec({'none': -2.0, 'high': 0.0}, status='estimated', measured=[])
        sc.flatten_point('r', reasons)
        self.assertEqual(set(reasons['by_effort'].values()), {0.0})

    def test_the_own_ability_median_is_last_needs_two_facets_and_never_fills_responsiveness(self):
        keys = [('creativity', 'creative_writing'), self.EXE, ('coding', 'faithfulness'), ('aesthetics', 'design'),
                ('tool_operation', 'image_recognition'), self.RESP]
        own = {keys[0]: self.rec({'high': -1.0, 'max': -0.5}), self.EXE: self.rec({'high': 0.0, 'max': 0.2}),
               keys[3]: self.rec({'high': -3.0, 'max': -2.0})}
        self.scorer().own_ability_median(own, keys)
        for k in (('coding', 'faithfulness'), ('tool_operation', 'image_recognition')):
            self.assertEqual(own[k]['by_effort'], {'high': -1.0, 'max': -0.5}, k)
            self.assertEqual(own[k]['estimate'], {'method': 'own_ability_median', 'n': 3,
                                                  'facets': ['creativity.creative_writing', 'coding.execution',
                                                             'aesthetics.design']})
        self.assertNotIn(self.RESP, own)
        lone = {keys[0]: self.rec({'high': -1.0})}
        self.scorer().own_ability_median(lone, keys)
        self.assertEqual(list(lone), [keys[0]])                                 # one facet is no median

    def test_the_time_size_step_follows_pair_agreement_with_no_bound_across_size(self):
        rel = {'s1': {'model': 'l1', 'version': 'same'}, 's2': {'model': 'l2', 'version': 'same'},
               's3': {'model': 'l1', 'version': 'nearest'}}
        for listed, ratio in (({'s1': 2.0, 'l1': 4.0, 's2': 1.0, 'l2': 4.0}, round(float(np.exp(np.log(0.5 * 0.25) / 2)), 4)),
                              ({'s1': 6.0, 'l1': 4.0, 's2': 8.0, 'l2': 4.0}, round(float(np.exp(np.log(1.5 * 2.0) / 2)), 4)),
                              ({'s1': 2.0, 'l1': 4.0, 's2': 8.0, 'l2': 4.0}, 1.0),
                              ({'s1': 2.0, 'l1': 4.0}, 1.0)):
            step = self.fa.time_size_step(listed, rel)
            self.assertEqual(step['ratio'], ratio, listed)
            self.assertEqual(step['n'], sum(1 for s in ('s1', 's2') if s in listed))
            minutes = {'s3': {}, 'l1': {'high': 4.0, 'max': 6.0}}
            basis = {'s3': {}, 'l1': {}}
            self.fa.apply_time_size_step(minutes, basis, {'s3': rel['s3']}, step)
            self.assertEqual(minutes['s3'], {'high': round(4.0 * ratio, 3), 'max': round(6.0 * ratio, 3)}, listed)
            self.assertEqual(basis['s3']['high']['agree'], ratio != 1.0)

    def test_an_effortless_listing_is_stored_and_read_at_the_declared_effort(self):
        """Rule (`effort_readings`): the declaration names where `default` is stored and read; without it the build stops."""
        sc = self.scorer()
        for reads in ('high', 'medium'):
            sc.readings = {'default': reads}
            self.assertEqual(sc.aa_rows({'z_by_effort': {'default': 0.3, 'low': -0.2}}), {reads: 0.3, 'low': -0.2})
            rows = self.fa.time_rows([{'source': 'aa.cost_time', 'effort': None, 'time_per_task_min': 5.0}], 'aa.cost_time',
                                     {'default': reads})
            self.assertEqual(list(rows), [reads])
        sc.readings = {'default': 'high'}
        self.assertEqual(sc.aa_rows({'z_by_effort': {'default': 0.3, 'high': 0.1}}), {'high': 0.1})   # a listed High stands
        for bad in ({}, {'effort_readings': {'reads': {}}}, {'effort_readings': {'reads': {'default': 'ultra'}}},
                    {'effort_readings': {'reads': {'max': 'high'}}}):
            with self.assertRaises(ValueError):
                self.fa.effort_readings(bad)


if __name__ == '__main__':
    unittest.main()
