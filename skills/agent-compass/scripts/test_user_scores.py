"""Tests for the two-tier manual capability score layer (maintainer public + local user) and Taxonomy R2 fix-round invariants."""
import copy
from datetime import datetime, timezone
import importlib.util
import json
import random
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

import allocator
import account_fleet
import axis_scores
import candidate_ranking
import ledger_snapshot
import reference_provenance
import roll
import route_judge
import user_scores
from router_test_support import isolated_router_state, usage_windows


SCRIPTS_DIR = Path(__file__).resolve().parent
CALIBRATION_DIR = SCRIPTS_DIR.parent / 'calibration'
REFERENCES_DIR = SCRIPTS_DIR.parent / 'references'


def _load_calibration_modules():
    out = {}
    sys.path.insert(0, str(CALIBRATION_DIR))
    try:
        for name in ('sources', 'measurements', 'five_axis'):
            spec = importlib.util.spec_from_file_location(f'_cal_{name}', CALIBRATION_DIR / f'{name}.py')
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            out[name] = mod
    finally:
        if sys.path and sys.path[0] == str(CALIBRATION_DIR):
            sys.path.pop(0)
    return out


def drop_facet(five, model, category, facet):
    """`five` (a five-axis-scores document) with `model` having no reading at all on `category.facet`: the category cell is the
    partial one the builder writes for a missing facet."""
    cell = five['models'][model]['axes'][category]
    cell['facets'].pop(facet, None)             # the shipped data may have no reading of it already
    five['models'][model]['axes'][category] = {
        'facets': cell['facets'], 'missing_facets': sorted({*cell.get('missing_facets', []), facet}), 'value': None, 'by_effort': {},
        'status': 'partial', 'flag': 'partial'}
    return five


def keep_efforts(five, model, category, facet, keep):
    """`five` with `model`'s `category.facet` and its category holding only the efforts in `keep`."""
    axis = five['models'][model]['axes'][category]
    for cell in (axis['facets'][facet], axis):
        cell['by_effort'] = {e: v for e, v in cell['by_effort'].items() if e in keep}
    return five


class ManualScoreLayerTests(unittest.TestCase):
    def setUp(self):
        isolated_router_state(self.addCleanup)
        self.five = copy.deepcopy(axis_scores.load_five_axis(REFERENCES_DIR / 'five-axis-scores.json'))
        self.accounts = account_fleet.load_accounts(account_fleet.reference_fleet_path())
        self.now = datetime(2026, 11, 11, 8, 0, tzinfo=timezone.utc)

    def test_shipped_public_user_scores_file_is_valid_maintainer_only(self):
        doc = json.loads((REFERENCES_DIR / 'user-scores.json').read_text(encoding='utf-8'))
        validated = user_scores.validate_document(doc, expected_kind='maintainer')
        self.assertGreater(len(validated['scores']), 0)
        for model, targets in validated['scores'].items():
            for target, entry in targets.items():
                self.assertEqual(entry['kind'], 'maintainer', (model, target))
                self.assertNotIn('url', entry)

    def test_level_to_z_mapping_exact_interpolation_and_extrapolation(self):
        targets = self.five['anchor']['targets']
        t1, t2, t3 = user_scores.target_triad(targets, 'creativity')
        self.assertAlmostEqual(user_scores.level_to_z(1.0, t1, t2, t3), t1, places=6)
        self.assertAlmostEqual(user_scores.level_to_z(2.0, t1, t2, t3), t2, places=6)
        self.assertAlmostEqual(user_scores.level_to_z(3.0, t1, t2, t3), t3, places=6)
        self.assertAlmostEqual(user_scores.level_to_z(1.5, t1, t2, t3), t1 + 0.5 * (t2 - t1), places=6)
        self.assertAlmostEqual(user_scores.level_to_z(2.5, t1, t2, t3), t2 + 0.5 * (t3 - t2), places=6)
        self.assertAlmostEqual(user_scores.level_to_z(3.5, t1, t2, t3), t3 + 0.5 * (t3 - t2), places=6)
        self.assertAlmostEqual(user_scores.level_to_z(0.5, t1, t2, t3), t1 - 0.5 * (t2 - t1), places=6)

        # Every shipped scale (facets and the two category triads) has t1 < t2 < t3, so the two maps are strictly increasing
        # and inverse to each other on the whole line, above 3 and below 1 included (extrapolated, no clamp).
        for category, facets in user_scores.CATEGORY_FACETS.items():
            for facet in (None, *facets):
                if facet is None and category in ('tool_operation',):
                    continue
                triad = user_scores.target_triad(targets, category, facet)
                self.assertLess(triad[0], triad[1], (category, facet))
                self.assertLess(triad[1], triad[2], (category, facet))
                previous = None
                for lvl in (-1.0, 0.5, 1.0, 1.4, 2.0, 2.6, 3.0, 3.5, 4.2, 6.0):
                    z = user_scores.level_to_z(lvl, *triad)
                    self.assertAlmostEqual(user_scores.z_to_level(z, *triad), lvl, places=3, msg=(category, facet, lvl))
                    self.assertTrue(previous is None or z > previous)
                    previous = z
                self.assertAlmostEqual(user_scores.level_to_z(4.0, *triad), triad[2] + (triad[2] - triad[1]), places=6)
        with self.assertRaisesRegex(ValueError, 't1 < t2 < t3'):
            user_scores.level_to_z(2.5, -1.0, 0.0, 0.0)           # a scale whose levels 2 and 3 coincide is refused
        with self.assertRaisesRegex(ValueError, 't1 < t2 < t3'):
            user_scores.z_to_level(0.1, -1.0, 0.0, 0.0)

    def test_maintainer_score_wins_over_measured_and_local_user_beats_maintainer(self):
        maintainer_doc = {
            'schema_version': user_scores.SCHEMA_VERSION,
            'scores': {
                'gpt-6.1-sol': {
                    'creativity': {'level': 3.2, 'as_of': '2026-11-11', 'kind': 'maintainer'},
                }
            },
        }
        local_doc = {
            'schema_version': user_scores.SCHEMA_VERSION,
            'scores': {
                'gpt-6.1-sol': {
                    'creativity': {'level': 1.2, 'as_of': '2026-11-11', 'kind': 'user'},
                }
            },
        }
        targets = self.five['anchor']['targets']
        expected_m = user_scores.mapped_z_for_target('creativity', 3.2, targets)
        expected_u = user_scores.mapped_z_for_target('creativity', 1.2, targets)

        only_m = axis_scores.five_axis_overlay(
            copy.deepcopy(self.accounts), data=self.five, public_scores=maintainer_doc, local_scores=None,
            read_local_state=False,
        )
        ax_m = only_m['models']['gpt-6.1-sol']['five_axis']['categories']['creativity']
        self.assertEqual(ax_m['status'], 'maintainer')
        self.assertEqual(ax_m['value'], expected_m)
        # Multi-effort curve preserves relative effort deltas rather than flattening to a constant.
        self.assertGreater(ax_m['by_effort']['high'], ax_m['by_effort']['low'])

        both = axis_scores.five_axis_overlay(
            copy.deepcopy(self.accounts), data=self.five, public_scores=maintainer_doc, local_scores=local_doc,
            read_local_state=False,
        )
        ax_u = both['models']['gpt-6.1-sol']['five_axis']['categories']['creativity']
        self.assertEqual(ax_u['status'], 'user')
        self.assertEqual(ax_u['value'], expected_u)
        # Underlying public data_reference stays anchored to the measured board across both layers without altering routing z.
        ref_m = ax_m['manual']['data_reference']
        ref_u = ax_u['manual']['data_reference']
        self.assertEqual(ref_m['status'], 'measured')
        self.assertEqual(ref_u['status'], 'measured')
        self.assertEqual(ref_m['z'], ref_u['z'])
        self.assertEqual(ref_m['level'], ref_u['level'])
        self.assertIn('lmarena.creative_writing', ref_u['sources'])

    def test_suggest_scores_is_read_only_and_flags_only_measured_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            pub_file = Path(tmp) / 'public-scores.json'
            loc_file = Path(tmp) / 'local-scores.json'
            pub_doc = {
                'schema_version': user_scores.SCHEMA_VERSION,
                'scores': {
                    # gpt-6.1-sol creativity is measured near level ~2.00; manual 3.2 drifts by > 0.4
                    'gpt-6.1-sol': {'creativity': {'level': 3.2, 'as_of': '2026-11-11', 'kind': 'maintainer'}},
                    # claude-opus-5-5 creativity is measured at 3.0; manual 2.9 is inside the threshold whatever its note says
                    'claude-opus-5-5': {'creativity': {'level': 2.9, 'as_of': '2026-11-11', 'kind': 'maintainer', 'note': 'maintainer impression'}},
                    # flint-40b-free is missing on public boards -> never flagged
                    'flint-40b-free': {'creativity': {'level': 1.0, 'as_of': '2026-11-11', 'kind': 'maintainer', 'note': 'maintainer impression'}},
                },
            }
            pub_bytes = (json.dumps(pub_doc, indent=2) + '\n').encode('utf-8')
            pub_file.write_bytes(pub_bytes)
            res = user_scores.suggest_scores(
                scope='public', threshold=0.4, accounts=self.accounts, five_data=self.five, public_path=pub_file,
            )
            self.assertEqual(pub_file.read_bytes(), pub_bytes)
            by_model = {r['model']: r for r in res['suggestions']}
            self.assertEqual(set(by_model), {'gpt-6.1-sol'})
            self.assertEqual(by_model['gpt-6.1-sol']['reason'], 'measured_drift')
            self.assertFalse(loc_file.exists())

    def test_manual_score_fills_missing_facet_lifts_mode_b_and_surfaces_actionable_hint_when_absent(self):
        accounts = copy.deepcopy(self.accounts)
        accounts['accounts']['grok-bot'] = {
            'plan': 'subscription', 'billing': 'subscription', 'verified': True, 'unmetered': True,
            'windows': {}, 'bundles': {'grok_bot': {'provider': 'grok-bot', 'harness': 'grok-bot', 'model': 'grok-bot'}},
        }
        usage = usage_windows(self.now, {'claude': [('five_hour', 10, 3), ('seven_day', 10, 84)],
                                         'codex': [('five_hour', 10, 3), ('usage', 10, 84)]})
        task = {
            'objective': 'Interactive multi-turn tool loop',
            'expected_hours': 1.0,
            'needs': {'local_only': False, 'outbound_ok': True},
            'profile': {
                'work_kind': 'live_loop', 'primary_level': 2, 'secondary_kind': 'none', 'secondary_level': 0,
                'needs_visual': False, 'local_only': False, 'wants_fast': False,
                'access': 'read-only', 'expected_hours': 1.0, 'task_count': '1',
                'axis_demand': {'creativity': 0, 'coding': 1, 'aesthetics': 0, 'tool_operation': 2},
            },
        }
        empty_pub = {'schema_version': user_scores.SCHEMA_VERSION, 'scores': {}}
        with patch.object(user_scores, 'load_public', return_value=empty_pub):
            before = allocator.plan(task, usage, self.now, accounts=copy.deepcopy(accounts))
        gb_before = next(c for c in before['ranking'] if c['id'] == 'grok_bot')
        self.assertTrue(gb_before['excluded'])
        msg = gb_before['excluded'][0]
        self.assertTrue(msg.startswith('data error: no trusted data for the demanded category'), msg)
        self.assertIn('grok-bot', msg)
        self.assertIn('tool_operation.responsiveness', msg)
        self.assertIn('agentcompass score set grok-bot tool_operation.responsiveness', msg)

        # Filling a maintainer score lifts Mode B; read_local_state=False honors maintainer scores while ignoring local user scores.
        pub_doc = {
            'schema_version': user_scores.SCHEMA_VERSION,
            'scores': {
                'grok-bot': {
                    'tool_operation.responsiveness': {'level': 2.5, 'as_of': '2026-11-11', 'kind': 'maintainer'},
                }
            },
        }
        user_scores.set_score('grok-bot', 'tool_operation.responsiveness', 1.1, public=False, accounts=accounts)
        with patch.object(user_scores, 'load_public', return_value=pub_doc):
            fresh_plan = allocator.plan(task, usage, self.now, accounts=copy.deepcopy(accounts), read_local_state=False)
            local_plan = allocator.plan(task, usage, self.now, accounts=copy.deepcopy(accounts), read_local_state=True)
        gb_fresh = next(c for c in fresh_plan['ranking'] if c['id'] == 'grok_bot')
        gb_local = next(c for c in local_plan['ranking'] if c['id'] == 'grok_bot')
        self.assertEqual(gb_fresh['excluded'], [])
        self.assertEqual(gb_local['excluded'], [])
        self.assertGreater(gb_fresh['components']['fit'], gb_local['components']['fit'])
        self.assertTrue(any('maintainer' in str(n) for n in gb_fresh['notes']))
        self.assertTrue(any('user' in str(n) for n in gb_local['notes']))

    def test_provenance_checks_pass_with_uncited_maintainer_values_and_local_never_leaks(self):
        before_shipped = (REFERENCES_DIR / 'user-scores.json').read_bytes()

        # Local CLI call writes only under ROUTER_STATE_DIR and never touches shipped references.
        with patch.object(sys, 'argv', ['route_judge.py', 'score', 'set', 'gpt-6.1-sol', 'coding.faithfulness', '2.8']):
            rc = route_judge.main()
        self.assertEqual(rc, 0)
        self.assertEqual((REFERENCES_DIR / 'user-scores.json').read_bytes(), before_shipped)
        listed = user_scores.list_scores('gpt-6.1-sol', scope='local')
        self.assertEqual(listed['local']['gpt-6.1-sol']['coding.faithfulness']['kind'], 'user')
        self.assertEqual(listed['local']['gpt-6.1-sol']['coding.faithfulness']['level'], 2.8)
        with patch.object(sys, 'argv', ['route_judge.py', 'score', 'clear', 'gpt-6.1-sol', 'coding.faithfulness']):
            self.assertEqual(route_judge.main(), 0)
        self.assertEqual(user_scores.list_scores('gpt-6.1-sol', scope='local')['local'], {})

        # Simulated git repo checkout: `--public` writes kind='maintainer' without citations.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / '.git').mkdir()
            refs = repo / 'skills' / 'agent-compass' / 'references'
            refs.mkdir(parents=True)
            for name in ('accounts.json', 'plan-templates.json', 'user-scores.json'):
                shutil.copy2(REFERENCES_DIR / name, refs / name)
            res = user_scores.set_score(
                'gpt-6.1-sol', 'creativity', 2.6, note='maintainer impression', public=True,
                public_path=refs / 'user-scores.json', accounts=self.accounts,
            )
            self.assertEqual(res['entry']['kind'], 'maintainer')
            self.assertNotIn('url', res['entry'])
            reference_provenance.validate_references(refs)

            # Writing kind='user' into the public file is rejected by the provenance checks.
            bad = {'schema_version': user_scores.SCHEMA_VERSION, 'scores': {'gpt-6.1-sol': {'creativity': {'level': 2.0, 'as_of': '2026-11-11', 'kind': 'user'}}}}
            (refs / 'user-scores.json').write_text(json.dumps(bad), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'kind'):
                reference_provenance.validate_references(refs)


class FixRoundRegressionTests(unittest.TestCase):
    def setUp(self):
        isolated_router_state(self.addCleanup)
        self.now = datetime(2026, 11, 11, 8, 0, tzinfo=timezone.utc)

    def test_f1_missing_facet_never_makes_higher_demand_level_easier(self):
        cal = _load_calibration_modules()
        fa, ms = cal['five_axis'], cal['measurements']
        panel = [ms.ANCHOR] + [f'p{i}' for i in range(9)]
        models = {}
        for i, m in enumerate(panel):
            z = 0.0 if m == ms.ANCHOR else -0.2 * (i + 1)
            models[m] = {
                'tier': [{'source': 'tier.arena_overall', 'value': 4.8 - 0.1 * i}],
                'coding': [
                    {'source': 'arena.agent_alignment', 'facet': 'faithfulness', 'value': z},
                    {'source': 'aa.terminalbench_4', 'facet': 'execution', 'value': z,
                     'z_by_effort': {'low': z - 0.4, 'medium': z - 0.2, 'high': z, 'xhigh': z + 0.1, 'max': z + 0.1}},
                ],
                'tool_operation': [
                    {'source': 'aa.ttfa', 'facet': 'responsiveness', 'value': z,
                     'z_by_effort': {'low': z + 0.3, 'medium': z + 0.1, 'high': z, 'xhigh': z - 0.2, 'max': z - 0.4}},
                    {'source': 'aa.mmmu_pro', 'facet': 'image_recognition', 'value': z,
                     'z_by_effort': {'low': z - 0.2, 'medium': z - 0.1, 'high': z, 'xhigh': z + 0.1, 'max': z + 0.1}},
                ],
            }
        models['partial_coder'] = {
            'coding': [{'source': 'aa.terminalbench_4', 'facet': 'execution', 'value': 0.1,
                        'z_by_effort': {'high': 0.1}}],
            'tool_operation': [{'source': 'aa.ttfa', 'facet': 'responsiveness', 'value': 0.2,
                                'z_by_effort': {'high': 0.2}}],
        }
        rows, _, _, _, _, _ = fa.Scorer({'panel': {'models': panel}, 'models': models},
                                        {'models': {m: {} for m in models}, 'families': {'rows': []}}).score()
        self.assertIsNone(rows['partial_coder']['axes']['coding']['value'])
        self.assertEqual(rows['partial_coder']['axes']['coding']['missing_facets'], ['faithfulness'])
        self.assertEqual(rows['partial_coder']['axes']['tool_operation']['by_level']['2']['by_effort']['high'], 0.2)
        self.assertEqual(rows['partial_coder']['axes']['tool_operation']['by_level']['3']['by_effort'], {})
        self.assertEqual(rows['partial_coder']['axes']['tool_operation']['missing_facets'], ['image_recognition'])

        # Named dispatch / direct fit monotonicity: missing responsiveness stands at zero (0.0) at both L2 and L3.
        targets = axis_scores.load_five_axis(REFERENCES_DIR / 'five-axis-scores.json')['anchor']['targets']
        vals = {'creativity': 0.0, 'coding': 0.0,
                'aesthetics': {'design': 0.0, 'image_recognition': 0.5},
                'tool_operation': {'responsiveness': None, 'image_recognition': 0.5}}
        d2 = {'creativity': 0, 'coding': 0, 'aesthetics': 0, 'tool_operation': 2}
        d3 = {'creativity': 0, 'coding': 0, 'aesthetics': 0, 'tool_operation': 3}
        fit_l2, _ = axis_scores.five_axis_fit(vals, d2, axis_scores.five_axis_targets(d2, targets))
        fit_l3, _ = axis_scores.five_axis_fit(vals, d3, axis_scores.five_axis_targets(d3, targets))
        self.assertEqual(fit_l2, 0.0)
        self.assertEqual(fit_l3, 0.0)
        self.assertIsNone(axis_scores.five_axis_shares(vals, targets)['tool_operation'])

        # Shared image_recognition: a manual score on tool_operation.image_recognition moves aesthetics L2/L3 too.
        over = axis_scores.five_axis_overlay(
            copy.deepcopy(account_fleet.load_accounts(account_fleet.reference_fleet_path())),
            read_local_state=False,
            local_scores={'scores': {'claude-opus-5-5': {
                'tool_operation.image_recognition': {'level': 1.2, 'as_of': '2026-11-17', 'kind': 'user'},
            }}},
        )
        opus_vals = axis_scores.five_axis_values(over['models']['claude-opus-5-5']['five_axis'], 'high')
        self.assertEqual(opus_vals['aesthetics']['design'], 0.0)
        self.assertEqual(opus_vals['aesthetics']['image_recognition'],
                         opus_vals['tool_operation']['image_recognition'])
        self.assertLess(opus_vals['aesthetics']['image_recognition'], -1.5)

    def test_f2_select_variants_picks_own_high_row_and_correlates_aa_effort_curve(self):
        cal = _load_calibration_modules()
        ms = cal['measurements']
        items = [
            ('GPT-6 Astra (xHigh)', 59.0, {}),
            ('GPT-6 Astra (High)', 55.0, {}),
            ('GPT-6 Astra (Medium)', 50.0, {}),
            ('GPT-6 Astra (Low)', 42.0, {}),
            ('Claude Opus 5.5 (High)', 56.0, {}),
        ]
        high, names, _, variants = ms.Builder.select_variants(items)
        self.assertEqual(high['GPT-6 Astra'], 55.0)
        self.assertEqual(names['GPT-6 Astra'], 'GPT-6 Astra (High)')
        self.assertEqual(variants['GPT-6 Astra']['xhigh'], 59.0)
        self.assertEqual(variants['GPT-6 Astra']['low'], 42.0)

        scores = json.loads((REFERENCES_DIR / 'five-axis-scores.json').read_text(encoding='utf-8'))
        corrs = scores['effort_correlations']
        self.assertIn('lmarena.creative_writing', corrs)
        self.assertGreater(corrs['lmarena.creative_writing']['r'], 0.5)
        opus_div = scores['models']['claude-opus-5-5']['axes']['creativity']['by_effort']
        self.assertGreater(opus_div['high'], opus_div['low'])
        # F4: every full-panel board has sd_panel == 1.0 on High rows, and partial-panel secondary boards carry scale != 1.0
        for facet_key, link in scores['links'].items():
            for src in link['full_panel_boards']:
                self.assertEqual(link['sd_at_high'][src], 1.0, (facet_key, src))
        self.assertNotEqual(scores['links']['tool_operation.image_recognition']['scales']['aa.mmmu_pro'], 1.0)
        self.assertNotEqual(scores['links']['aesthetics.design']['scales']['designarena.svg'], 1.0)

    def test_f3_economy_cost_is_inside_net_score_without_score_rounding(self):
        accounts = account_fleet.load_accounts(account_fleet.reference_fleet_path())
        now = self.now
        accounts['accounts']['claude']['plan'] = 'pro'
        accounts['accounts']['claude']['verified'] = True
        accounts['accounts']['claude']['windows'] = {'five_hour': {'hours': 5}, 'seven_day': {'hours': 168}}
        accounts['accounts']['codex']['plan'] = 'pro'
        accounts['accounts']['codex']['verified'] = True
        accounts['accounts']['codex']['windows'] = {'usage': {'hours': 168}}
        accounts['accounts']['claude-2'] = copy.deepcopy(accounts['accounts']['claude'])
        accounts['accounts']['claude-2']['bundles'] = {
            'claude2_opus55_high': {
                **accounts['accounts']['claude']['bundles']['opus55_high'],
                'provider': 'claude-2', 'account': 'claude-2',
            }
        }
        # usage_windows takes (id, remaining_pct, reset_hours): claude has 75% remaining (25% used at 84h, half of pace -> C ~ 1e-8 < 1e-4);
        # claude-2 has 30% remaining (70% used, 83h reset — earlier reset must NOT let the 70%-used pool win).
        usage = usage_windows(now, {
            'claude': [('five_hour', 90.0, 4.0), ('seven_day', 75.0, 84.0)],
            'claude-2': [('five_hour', 90.0, 4.0), ('seven_day', 30.0, 83.0)],
            'codex': [('usage', 75.0, 84.0)],
        })
        task = {
            'objective': 'Small coding fix',
            'expected_hours': 0.1,
            'needs': {'local_only': True},
            'profile': {
                'work_kind': 'code_implementation', 'primary_level': 3, 'secondary_kind': 'none', 'secondary_level': 0,
                'needs_visual': False, 'local_only': True, 'wants_fast': False,
                'access': 'write', 'expected_hours': 0.1, 'task_count': '1',
                'axis_demand': {'creativity': 0, 'coding': 3, 'aesthetics': 0, 'tool_operation': 1},
            },
        }
        plan = allocator.plan(task, usage, now, accounts=copy.deepcopy(accounts),
                              host_policy_config={'allowed_accounts': ['claude', 'claude-2', 'codex']}, read_local_state=False)
        c1 = next(c for c in plan['ranking'] if c['id'] == 'opus55_high')
        c2 = next(c for c in plan['ranking'] if c['id'] == 'claude2_opus55_high')
        c1_fast = next(c for c in plan['ranking'] if c['id'] == 'opus55_high+fast')
        self.assertLess(c1['components']['opportunity_cost'], c2['components']['opportunity_cost'])
        self.assertGreater(c1['score'], c2['score'])
        self.assertEqual(candidate_ranking.five_axis_deciding_key(c1, c2), 'net_score')
        # 2x burn of +fast has higher C and lower N than plain when wants_fast=False at mid-week ample (Pro and Max20 alike):
        self.assertLess(c1['components']['opportunity_cost'], c1_fast['components']['opportunity_cost'])
        self.assertGreater(c1['score'], c1_fast['score'])
        self.assertEqual(candidate_ranking.five_axis_deciding_key(c1, c1_fast), 'net_score')

        acc_max20 = copy.deepcopy(accounts)
        acc_max20['accounts']['claude']['plan'] = 'max20'
        acc_max20['accounts']['claude']['windows'] = {'five_hour': {'hours': 5}, 'seven_day': {'hours': 168},
                                                      'weekly_fable': {'hours': 168, 'models': ['fable']}}
        usage_max20 = usage_windows(now, {
            'claude': [('five_hour', 90.0, 4.0), ('seven_day', 50.0, 84.0), ('weekly_fable', 50.0, 84.0)],
            'codex': [('usage', 50.0, 84.0)],
        })
        plan_max20 = allocator.plan(task, usage_max20, now, accounts=acc_max20,
                                    host_policy_config={'allowed_accounts': ['claude', 'codex']}, read_local_state=False)
        m1 = next(c for c in plan_max20['ranking'] if c['id'] == 'opus55_high')
        m1_fast = next(c for c in plan_max20['ranking'] if c['id'] == 'opus55_high+fast')
        self.assertGreater(m1['score'], m1_fast['score'])
        self.assertEqual(candidate_ranking.five_axis_deciding_key(m1, m1_fast), 'net_score')
        self.assertFalse(plan_max20['selected_bundle'].get('fast'))

        # Free unmetered dot_text beats billed astra_high on net_score when both reach the same fit:
        research_task = {
            'objective': 'Compare vendor APIs from public docs',
            'expected_hours': 0.5,
            'needs': {'local_only': False, 'outbound_ok': True},
            'profile': {
                'work_kind': 'web_research', 'primary_level': 3, 'secondary_kind': 'none', 'secondary_level': 0,
                'needs_visual': False, 'local_only': False, 'wants_fast': False,
                'access': 'read-only', 'expected_hours': 0.5, 'task_count': '1',
                'axis_demand': {'creativity': 3, 'coding': 0, 'aesthetics': 0, 'tool_operation': 1},
            },
        }
        res_plan = allocator.plan(research_task, usage, now, accounts=copy.deepcopy(accounts),
                                  host_policy_config={'allowed_accounts': ['codex', 'dot']}, read_local_state=False)
        dot_row = next(c for c in res_plan['ranking'] if c['id'] == 'dot_text')
        astra_row = next(c for c in res_plan['ranking'] if c['id'] == 'astra_high')
        self.assertEqual(dot_row['components']['opportunity_cost'], 0.0)
        self.assertGreater(astra_row['components']['opportunity_cost'], 0.0)
        self.assertGreater(dot_row['score'], astra_row['score'])
        self.assertEqual(candidate_ranking.five_axis_deciding_key(dot_row, astra_row), 'net_score')

    def test_f5_auto_matched_medium_coupling_gets_continuity_and_tool_op_never_zeros_other_axes(self):
        scores = axis_scores.load_five_axis(REFERENCES_DIR / 'five-axis-scores.json')
        targets = scores['anchor']['targets']
        cont = axis_scores.five_axis_continuity_value(
            {'creativity': 0, 'coding': 2, 'aesthetics': 2, 'tool_operation': 3}, targets
        )
        self.assertGreater(cont, 1.0)

        accounts = account_fleet.load_accounts(account_fleet.reference_fleet_path())
        usage = usage_windows(self.now, {'claude': [('five_hour', 10, 3), ('seven_day', 10, 84)],
                                         'codex': [('five_hour', 10, 3), ('usage', 10, 84)]})
        base_profile = {
            'work_kind': 'code_implementation', 'primary_level': 2, 'secondary_kind': 'none', 'secondary_level': 0,
            'needs_visual': False, 'local_only': True, 'wants_fast': False,
            'access': 'write', 'expected_hours': 1.0, 'task_count': '1',
            'axis_demand': {'creativity': 0, 'coding': 2, 'aesthetics': 0, 'tool_operation': 2},
        }
        auto_resume = allocator.plan(
            {'objective': 'Continue chain',
             'chain': {'id': 'chain-1', 'coupling': 'medium', 'total_hours': 2,
                       'worker': {'bundle': 'sol_high', 'context_tokens': 80000}},
             'needs': {'local_only': True}, 'profile': base_profile},
            usage, self.now, accounts=copy.deepcopy(accounts), read_local_state=False,
        )
        unrelated = allocator.plan(
            {'objective': 'Unrelated task',
             'chain': {'id': 'chain-1', 'coupling': 'low', 'total_hours': 2,
                       'worker': {'bundle': 'sol_high', 'context_tokens': 80000}},
             'needs': {'local_only': True}, 'profile': base_profile},
            usage, self.now, accounts=copy.deepcopy(accounts), read_local_state=False,
        )
        sol_resume = next(c for c in auto_resume['ranking'] if c['id'] == 'sol_high')
        sol_unrelated = next(c for c in unrelated['ranking'] if c['id'] == 'sol_high')
        self.assertGreater(sol_resume['components']['continuity'], 1.0)
        self.assertEqual(sol_unrelated['components']['continuity'], 0.0)

    def test_f7_f8_f9_hygiene_invariants(self):
        cal = _load_calibration_modules()
        self.assertFalse(hasattr(cal['sources'], '_aa_companion_html'))
        self.assertFalse(hasattr(cal['five_axis'], 'FAST_CAPABLE_MODELS'))

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / 'opencode.db'
            con = sqlite3.connect(db_path)
            con.execute('CREATE TABLE message (time_created INTEGER, data TEXT)')
            now_ms = int(self.now.timestamp() * 1000)
            msg = {'role': 'assistant', 'providerID': 'opencode', 'modelID': 'mimo-v2.6-flash-free',
                   'tokens': {'input': 100_000, 'output': 50_000, 'reasoning': 0, 'cache': {'read': 0}}}
            con.execute('INSERT INTO message VALUES (?, ?)', (now_ms, json.dumps(msg)))
            con.commit()
            con.close()
            models_tbl = {
                'mimo-v2.6-flash-free': {
                    'five_axis': {'list_price': {'value': {'input': 1.0, 'output': 2.0, 'cache_read': 0.1}}}
                }
            }
            with patch.dict(os.environ, {'OPENCODE_DB_PATH': str(db_path)}):
                self.assertGreater(sum(axis_scores.opencode_local_spend(models_tbl, None, read_local_db=True)[0].values()), 0.0)
                self.assertEqual(axis_scores.opencode_local_spend(models_tbl, None, read_local_db=False)[0], {})

        proxy = {'endpoint': 'https://proxy.example.com', 'api_key': 'sk-test', 'family': 'claude'}
        with patch.object(roll.dispatch_flow, 'paseo_config', return_value={}), \
                patch.object(roll, '_router_override_proxy_spec', return_value=proxy):
            fam_fable, env_fable = roll.provider_definition('my-proxy', bundle={'model': 'fable'})
            fam_terra, env_terra = roll.provider_definition('my-proxy', bundle={'model': 'gpt-6.1-terra'})
        self.assertEqual(fam_fable, 'claude')
        self.assertIn('ANTHROPIC_BASE_URL', env_fable)
        self.assertEqual(fam_terra, 'codex')
        self.assertIn('OPENAI_BASE_URL', env_terra)

        with self.assertRaisesRegex(ValueError, 'as_of'):
            user_scores.validate_document(
                {'schema_version': user_scores.SCHEMA_VERSION, 'scores': {'grok-bot': {'tool_operation.responsiveness': {'level': 2.5, 'date': '2026-11-10', 'kind': 'maintainer'}}}},
                expected_kind='maintainer',
            )
        with self.assertRaisesRegex(ValueError, 'schema_version'):
            user_scores.validate_document(
                {'schema_version': 1, 'scores': {'grok-bot': {'coding': {'level': 2.5, 'as_of': '2026-11-10', 'kind': 'maintainer'}}}},
                expected_kind='maintainer',
            )
        with self.assertRaisesRegex(ValueError, 'Unknown score target'):
            user_scores.validate_document(
                {'schema_version': user_scores.SCHEMA_VERSION, 'scores': {'grok-bot': {'divergence': {'level': 2.5, 'as_of': '2026-11-10', 'kind': 'maintainer'}}}},
                expected_kind='maintainer',
            )


class LocalScoresKeptAcrossUpdatesTests(unittest.TestCase):
    """The user's own scores survive an update: a local file an earlier release wrote is carried into the current schema."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.local = Path(self.tmp.name) / 'user-scores.json'

    def write(self, doc):
        with self.local.open('w', encoding='utf-8', newline='\n') as handle:
            json.dump(doc, handle)

    @staticmethod
    def conversion():
        return json.loads(user_scores.LEVEL3_CONVERSION_FILE.read_text(encoding='utf-8'))['conversions']['2']

    def assert_position_kept(self, old_level, new_level, triad, msg=None, *, plateau=False):
        """Owner 2026-11-18: the definition change by itself moves no hand-written value. The value the old level had under the
        old definition on this panel (t3 = the anchor, z = 0: levels 1 and 2 as before, above 2 the same slope towards 0 and
        beyond; on `tool_operation.responsiveness` t3 = t2, a plateau: the median up to level 3, above it `t2 + (L - 3) * (t2 - t1)`)
        equals the value the new level has under the new definition, to the rounding of the written level."""
        t1, t2, t3 = triad
        if old_level <= 2.0:
            old_value = t1 + (old_level - 1.0) * (t2 - t1)
        elif plateau:
            old_value = t2 + max(old_level - 3.0, 0.0) * (t2 - t1)
        else:
            old_value = t2 + (old_level - 2.0) * (0.0 - t2)
        self.assertAlmostEqual(user_scores.level_to_z(new_level, t1, t2, t3), old_value, delta=0.0051 * (t3 - t2), msg=msg)

    def test_levels_1_and_2_stay_and_the_position_above_2_is_kept_on_any_panel(self):
        """Property of `convert_level`: on the shipped panels (anchor level read from the shipped data, the declared slope on the
        plateau facet) and on made-up ones (any t1 < t2 < 0 <= t3; a made-up plateau panel t1 < t2 = t3's old scale), for levels
        below 1, inside the scale and above 3, grid 0.5..4 and beyond."""
        targets = json.loads((REFERENCES_DIR / 'five-axis-scores.json').read_text(encoding='utf-8'))['anchor']['targets']
        triads = {f'{c}.{f}': user_scores.target_triad(targets, c, f) for c, fs in user_scores.CATEGORY_FACETS.items() for f in fs}
        rng = random.Random(7)
        for i in range(40):
            t1 = -rng.uniform(1.0, 3.5)
            t2 = rng.uniform(t1 + 0.2, -0.05)
            triads[f'made-up-{i}'] = (t1, t2, rng.choice([0.0, rng.uniform(0.02, 1.8)]))
        for i in range(10):
            t1 = -rng.uniform(1.0, 3.5)
            t2 = rng.uniform(t1 + 0.2, 0.3)
            triads[f'plateau-made-up-{i}'] = (t1, t2, t2 + rng.uniform(0.2, 2.0))
        grid = [0.5 + 0.1 * n for n in range(36)] + [4.2, 6.0]
        for name, triad in triads.items():
            plateau = name == 'tool_operation.responsiveness' or name.startswith('plateau-')
            t1, t2, t3 = triad
            anchor_level = 2.0 if plateau else user_scores.z_to_level(0.0, *triad)
            slope = (t2 - t1) / (t3 - t2) if plateau else None
            for level in grid:
                new = user_scores.convert_level(level, anchor_level, above_3_slope=slope)
                if level <= 2.0:
                    self.assertEqual(new, round(level, 2), (name, level))
                self.assert_position_kept(level, new, triad, (name, level), plateau=plateau)
                self.assertEqual(new, round(new, 2))

    def test_the_declared_conversion_data_follows_from_its_declared_scale(self):
        conversion = self.conversion()
        self.assertEqual(set(conversion['anchor_levels']), set(conversion['scale']))
        self.assertEqual(set(conversion['above_3_slopes']), {'tool_operation.responsiveness'})
        for target, level in conversion['anchor_levels'].items():
            if target in conversion['above_3_slopes']:
                # the old scale had t2 = t3: every level in (2, 3] meant the panel median, which is level 2 on any scale
                self.assertEqual(level, 2.0)
                t1, t2, t3 = conversion['scale'][target]
                slope = conversion['above_3_slopes'][target]
                self.assertAlmostEqual(slope, (t2 - t1) / (t3 - t2), places=3, msg=target)
                for written in (2.5, 3.0):
                    self.assertEqual(user_scores.convert_level(written, level, above_3_slope=slope), 2.0)
                # above 3 the old scale went above the median by (L - 3) * (t2 - t1)
                self.assertAlmostEqual(user_scores.convert_level(4.0, level, above_3_slope=slope), 2.0 + slope, places=2)
                continue
            self.assertAlmostEqual(level, user_scores.z_to_level(0.0, *conversion['scale'][target]), places=4, msg=target)
            self.assertTrue(2.0 <= level <= 3.0, target)       # the anchor lies between the panel median and its best

    def test_convert_scores_splits_a_category_entry_only_above_2_and_a_facet_entry_wins(self):
        anchor = {'coding.execution': 3.0, 'coding.faithfulness': 2.25, 'creativity.creative_writing': 3.0,
                  'aesthetics.design': 2.9, 'tool_operation.responsiveness': 2.0, 'tool_operation.image_recognition': 2.9}
        entry = lambda level, note=None: {'level': level, 'as_of': '2026-11-11', 'kind': 'maintainer', **({'note': note} if note else {})}
        out = user_scores.convert_scores({
            'a': {'coding': entry(2.0), 'tool_operation': entry(1.5), 'aesthetics': entry(2.6)},
            'b': {'coding': entry(3.0, 'n'), 'coding.faithfulness': entry(2.5)},
            'c': {'tool_operation': entry(3.0)},
        }, anchor, {})
        self.assertEqual(out['a'], {'aesthetics': entry(2.54), 'coding': entry(2.0), 'tool_operation': entry(1.5)})
        self.assertEqual(out['b'], {'coding.execution': entry(3.0, 'n'), 'coding.faithfulness': entry(2.12)})
        self.assertEqual(out['c'], {'tool_operation.image_recognition': entry(2.9), 'tool_operation.responsiveness': entry(2.0)})

    def assert_converted(self, old_scores, new_scores):
        """Each old entry is in `new_scores` at its position on the declared v0.7.0 scale (an entry of a category of several
        facets above level 2 per facet); everything but the level is kept."""
        conversion = self.conversion()
        for model, entries in old_scores.items():
            for target, entry in entries.items():
                cat, facet = user_scores.parse_target(target)
                multi = facet is None and len(user_scores.CATEGORY_FACETS[cat]) > 1
                if multi and entry['level'] > 2.0:
                    keys = [(f'{cat}.{f}', f) for f in user_scores.CATEGORY_FACETS[cat] if f'{cat}.{f}' not in entries]
                    self.assertNotIn(target, new_scores[model])
                else:
                    keys = [(target, facet or user_scores.CATEGORY_FACETS[cat][0])]
                for key, f in keys:
                    new = new_scores[model][key]
                    self.assertEqual({k: v for k, v in new.items() if k != 'level'}, {k: v for k, v in entry.items() if k != 'level'})
                    self.assert_position_kept(entry['level'], new['level'], conversion['scale'][f'{cat}.{f}'], (model, key),
                                              plateau=f == 'responsiveness')

    def test_a_schema_1_file_is_read_with_its_entries_under_the_renamed_category_on_the_current_scale(self):
        entry = {'level': 2.6, 'as_of': '2026-11-12', 'kind': 'user', 'note': 'mine'}
        facet = {'level': 1.5, 'as_of': '2026-11-13', 'kind': 'user'}
        coding = {'level': 3.0, 'as_of': '2026-11-14', 'kind': 'user'}
        aesthetics = {'level': 2.6, 'as_of': '2026-11-14', 'kind': 'user'}
        self.write({'schema_version': 1, 'scores': {
            'model-a': {'divergence': entry, 'coding': coding, 'aesthetics': aesthetics}, 'model-b': {'divergence.creative_writing': facet}}})
        before = self.local.read_bytes()
        loaded = user_scores.load_local(self.local)
        self.assertEqual(loaded['schema_version'], user_scores.SCHEMA_VERSION)
        self.assert_converted({'model-a': {'creativity': entry, 'coding': coding, 'aesthetics': aesthetics},
                               'model-b': {'creativity.creative_writing': facet}}, loaded['scores'])
        self.assertEqual(self.local.read_bytes(), before)  # a read never writes

    def test_a_schema_2_file_keeps_the_position_of_every_entry_and_the_next_save_writes_schema_3(self):
        """Owner 2026-11-18: level 3 moved from the anchor to the panel maximum; levels at or below 2 stay, levels above 2 keep
        their position (`LOCAL_UPGRADES[2]`, `level-3-conversion.json`), a facet entry beats a category entry, and a read writes nothing."""
        old = {
            'model-a': {'creativity': {'level': 3.1, 'as_of': '2026-11-12', 'kind': 'user', 'note': 'mine'},
                        'coding': {'level': 2.4, 'as_of': '2026-11-12', 'kind': 'user'},
                        'tool_operation.responsiveness': {'level': 2.5, 'as_of': '2026-11-12', 'kind': 'user'}},
            'model-e': {'tool_operation.responsiveness': {'level': 3.5, 'as_of': '2026-11-12', 'kind': 'user'},
                        'tool_operation': {'level': 4.0, 'as_of': '2026-11-12', 'kind': 'user'}},
            'model-b': {'aesthetics': {'level': 0.5, 'as_of': '2026-11-12', 'kind': 'user'},
                        'coding': {'level': 3.0, 'as_of': '2026-11-12', 'kind': 'user'},
                        'coding.faithfulness': {'level': 1.7, 'as_of': '2026-11-13', 'kind': 'user'}},
            'model-c': {'coding': {'level': 1.8, 'as_of': '2026-11-12', 'kind': 'user'}},
        }
        self.write({'schema_version': 2, 'scores': old})
        before = self.local.read_bytes()
        loaded = user_scores.load_local(self.local)
        self.assertEqual(loaded['schema_version'], 3)
        self.assert_converted(old, loaded['scores'])
        self.assertIn('coding', loaded['scores']['model-c'])                 # at or below 2 a category entry stays one entry
        self.assertEqual(self.local.read_bytes(), before)
        user_scores.set_score('model-d', 'coding', 2.0, local_path=self.local, as_of='2026-11-18')
        saved = json.loads(self.local.read_text(encoding='utf-8'))
        self.assertEqual(saved['schema_version'], 3)
        self.assertEqual(saved['scores']['model-a'], loaded['scores']['model-a'])          # converted once, kept as written
        self.assertEqual(user_scores.load_local(self.local)['scores']['model-a'], saved['scores']['model-a'])   # no second conversion

    def test_the_next_save_writes_the_current_schema_and_keeps_the_earlier_entries(self):
        entry = {'level': 2.6, 'as_of': '2026-11-12', 'kind': 'user'}
        self.write({'schema_version': 1, 'scores': {'model-a': {'divergence': entry}}})
        user_scores.set_score('model-b', 'coding', 2.0, local_path=self.local, as_of='2026-11-18')
        saved = json.loads(self.local.read_text(encoding='utf-8'))
        self.assertEqual(saved['schema_version'], user_scores.SCHEMA_VERSION)
        self.assert_converted({'model-a': {'creativity': entry}}, saved['scores'])
        self.assertEqual(saved['scores']['model-b']['coding']['level'], 2.0)

    def test_every_schema_below_the_current_one_has_an_upgrade_step(self):
        self.assertEqual(sorted(user_scores.LOCAL_UPGRADES), list(range(1, user_scores.SCHEMA_VERSION)))

    def test_an_unknown_schema_or_shape_is_refused_not_guessed(self):
        unknown_target = {'no_such_category': {'level': 2, 'as_of': '2026-11-12', 'kind': 'user'}}
        for doc in ({'schema_version': user_scores.SCHEMA_VERSION + 1, 'scores': {}}, {'scores': {}},
                    {'schema_version': 1, 'scores': {'model-a': unknown_target}}):
            with self.subTest(doc=doc):
                self.write(doc)
                with self.assertRaisesRegex(ValueError, 'invalid local user-scores file'):
                    user_scores.load_local(self.local)

    def test_the_shipped_maintainer_file_has_no_upgrade_path(self):
        # It ships with the version, so it is always in the current schema; an older one is a packaging error.
        self.write({'schema_version': 1, 'scores': {}})
        with self.assertRaisesRegex(ValueError, 'schema_version'):
            user_scores.load_public(self.local)


class TaxonomyR2Round2FixTests(unittest.TestCase):
    """Regression coverage for Taxonomy R2 review-2 findings (R2-F1, R2-F6, R2-F7, R2-F8, R2-F9)."""

    def setUp(self):
        isolated_router_state(self.addCleanup)
        self.now = datetime(2026, 11, 11, 8, 0, tzinfo=timezone.utc)
        self.accounts = account_fleet.load_accounts(account_fleet.reference_fleet_path())
        self.five = copy.deepcopy(axis_scores.load_five_axis(REFERENCES_DIR / 'five-axis-scores.json'))

    def test_r2_f1_end_to_end_decision_snapshot_and_plan_stage_with_non_empty_user_scores(self):
        # Populate local user scores so both eligible (selected) and excluded rows carry manual_scores.
        user_scores.set_score('claude-opus-5-5', 'creativity', 3.0, public=False, accounts=self.accounts)
        user_scores.set_score('dot', 'tool_operation.responsiveness', 2.5, public=False, accounts=self.accounts)
        usage = usage_windows(self.now, {'claude': [('five_hour', 10, 3), ('seven_day', 10, 84), ('weekly_fable', 10, 84)],
                                         'codex': [('five_hour', 10, 3), ('usage', 10, 84)]})
        accounts = copy.deepcopy(self.accounts)
        accounts['accounts']['claude']['plan'] = 'max_20x'
        accounts['accounts']['claude']['verified'] = True
        accounts['accounts']['codex']['plan'] = 'pro'
        accounts['accounts']['codex']['verified'] = True
        task = {
            'objective': 'Local implementation step',
            'expected_hours': 0.5,
            'needs': {'local_only': True},
            'profile': {
                'work_kind': 'code_implementation', 'primary_level': 3, 'secondary_kind': 'none', 'secondary_level': 0,
                'needs_visual': False, 'local_only': True, 'wants_fast': False,
                'access': 'write', 'expected_hours': 0.5, 'task_count': '1',
                'axis_demand': {'creativity': 3, 'coding': 2, 'aesthetics': 0, 'tool_operation': 1},
            },
        }
        decision = allocator.plan(task, usage, self.now, accounts=accounts, read_local_state=True)
        snap = ledger_snapshot.decision_snapshot(decision)
        json.dumps(snap, allow_nan=False)
        selected_rows = [r for r in snap['ranking'] if not r['excluded'] and r.get('manual_scores')]
        excluded_rows = [r for r in snap['ranking'] if r['excluded'] and r.get('manual_scores')]
        self.assertTrue(selected_rows)
        self.assertTrue(excluded_rows)

        # End-to-end route_judge.plan_stage with non-empty shipped + local user-scores.
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / 'usage.json'
            now_live = datetime.now(timezone.utc)
            live_usage = usage_windows(now_live, {
                'claude': [('five_hour', 80, 3), ('seven_day', 80, 84), ('weekly_fable', 80, 84)],
                'codex': [('five_hour', 80, 3), ('usage', 80, 84)],
            })
            import argparse
            import route_simulation as sim
            accounts['accounts']['claude']['plan'] = 'max20'
            accounts['accounts']['claude']['windows'] = {
                'five_hour': {'hours': 5}, 'seven_day': {'hours': 168}, 'weekly_fable': {'hours': 168},
            }
            accounts['accounts']['codex']['windows'] = {'five_hour': {'hours': 5}, 'usage': {'hours': 168}}
            cache_doc = sim.synthetic_document(accounts, now_live, sim.STATES['ample'])
            cache_path.write_text(json.dumps(cache_doc), encoding='utf-8')
            args = argparse.Namespace(
                stage='plan', dry_run=True, demo=False, score=False, no_score=True,
                hours=0.5, can_wait_hours=None, fast=False, usage_cache=cache_path,
                keep_whole=None, score_timeout=30,
            )
            with patch.object(account_fleet, 'load_accounts', return_value=accounts):
                result, rc = route_judge.plan_stage({'task': task}, args)
            self.assertEqual(rc, 0)
            self.assertNotEqual(result['choice'], 'none')
            self.assertTrue(any(r.get('manual_scores') for r in result['ranking']))

    def test_r2_f6_manual_scores_set_high_and_leave_unmeasured_efforts_missing(self):
        accounts = copy.deepcopy(self.accounts)
        accounts['accounts']['codex']['plan'] = 'pro'
        accounts['accounts']['codex']['verified'] = True
        accounts['accounts']['codex']['windows'] = {'usage': {'hours': 168}}
        cx = accounts['accounts']['codex']['bundles']
        cx['gpt6_sol_high'] = {**cx['sol_high'], 'model': 'gpt-6-sol', 'effort': 'high'}
        cx['gpt6_sol_max'] = {**cx['sol_high'], 'model': 'gpt-6-sol', 'effort': 'max'}
        cx['gpt56_sol_high'] = {**cx['sol_high'], 'model': 'gpt-5.6-sol', 'effort': 'high'}
        cx['ds_v4_high'] = {**cx['sol_high'], 'model': 'deepseek-v4-pro', 'effort': 'high'}
        cx['ds_v4_max'] = {**cx['sol_high'], 'model': 'deepseek-v4-pro', 'effort': 'max'}
        usage = usage_windows(self.now, {'codex': [('usage', 90, 84)]})

        code_task = {
            'objective': 'Code refactor',
            'expected_hours': 0.5,
            'needs': {'local_only': True},
            'profile': {
                'work_kind': 'code_implementation', 'primary_level': 2, 'secondary_kind': 'none', 'secondary_level': 0,
                'needs_visual': False, 'local_only': True, 'wants_fast': False,
                'access': 'write', 'expected_hours': 0.5, 'task_count': '1',
                'axis_demand': {'creativity': 0, 'coding': 2, 'aesthetics': 0, 'tool_operation': 0},
            },
        }
        empty_pub = {'schema_version': user_scores.SCHEMA_VERSION, 'scores': {}}

        # The precondition is set here, not read from the shipped coverage: gpt-6-sol has no faithfulness at Max and
        # deepseek-v4-pro none at all.
        five = keep_efforts(copy.deepcopy(axis_scores.load_five_axis()), 'gpt-6-sol', 'coding', 'faithfulness',
                            ('low', 'medium', 'high', 'xhigh'))
        five = drop_facet(five, 'deepseek-v4-pro', 'coding', 'faithfulness')

        def excluded():
            with patch.object(user_scores, 'load_public', return_value=empty_pub), \
                    patch.object(axis_scores, 'load_five_axis', return_value=copy.deepcopy(five)):
                d = allocator.plan(code_task, usage, self.now, accounts=copy.deepcopy(accounts),
                                   host_policy_config={'allowed_accounts': ['codex']})
            return {c['id']: c['excluded'] for c in d['ranking']}

        # High filled from one-sided effort data (five_axis.fill_high) routes gpt-6-sol and gpt-5.6-sol at High; Max of
        # gpt-6-sol (no faithfulness at Max) and deepseek-v4-pro (no faithfulness at all) stay in Mode B with the hint.
        before = excluded()
        self.assertEqual((before['gpt6_sol_high'], before['gpt56_sol_high']), ([], []))
        for bid in ('gpt6_sol_max', 'ds_v4_high', 'ds_v4_max'):
            self.assertIn('agentcompass score set', before[bid][0], bid)

        user_scores.set_score('gpt-6-sol', 'coding.faithfulness', 3.0, public=False, accounts=accounts)
        user_scores.set_score('gpt-6-sol', 'coding.execution', 2.4, public=False, accounts=accounts)
        user_scores.set_score('deepseek-v4-pro', 'coding.faithfulness', 2.5, public=False, accounts=accounts)
        user_scores.set_score('deepseek-v4-pro', 'coding.execution', 2.2, public=False, accounts=accounts)
        after = excluded()
        self.assertEqual((after['gpt6_sol_high'], after['ds_v4_high']), ([], []))
        # Rule (owner 2026-11-18, `_manual_by_effort`): an effort the facet has no value at takes the manual High plus the
        # panel's median own step from High to it (`effort_steps`), so Max routes; never another model's curve.
        step = five['effort_steps']['coding.faithfulness']['high']['max']['step']
        for bid in ('gpt6_sol_max', 'ds_v4_max'):
            self.assertFalse(any('no trusted data' in e for e in after[bid]), after[bid])
        with patch.object(axis_scores, 'load_five_axis', return_value=copy.deepcopy(five)):
            ov = axis_scores.five_axis_overlay(copy.deepcopy(accounts), read_local_state=True, public_scores=empty_pub)
        for model in ('gpt-6-sol', 'deepseek-v4-pro'):
            faith = ov['models'][model]['five_axis']['categories']['coding']['facets']['faithfulness']
            # bounded by the effort below it (`bound_along_effort`): never under Xhigh
            self.assertAlmostEqual(faith['by_effort']['max'],
                                   max(faith['value'] + step, faith['by_effort'].get('xhigh', faith['value'])), places=4)

        overlay = axis_scores.five_axis_overlay(copy.deepcopy(accounts), read_local_state=True, public_scores=empty_pub)
        shipped = axis_scores.load_five_axis()['models']['gpt-6-sol']['axes']['coding']['facets']['execution']
        execution = overlay['models']['gpt-6-sol']['five_axis']['categories']['coding']['facets']['execution']
        # The measured Max stands unless it is below the manual High, which floors it (and it is no longer measured).
        self.assertIn('max', shipped['measured_efforts'])
        self.assertEqual(execution['by_effort']['max'], max(shipped['by_effort']['max'], execution['value']))
        self.assertEqual('max' in execution['measured_efforts'], shipped['by_effort']['max'] >= execution['value'])
        self.assertEqual(execution['by_effort']['high'], execution['value'])

    def test_manual_by_effort_keeps_measured_efforts_follows_own_curve_and_never_borrows(self):
        """User 2026-11-12: a manual score sets High; measured efforts stay; missing efforts follow the facet's own curve,
        or stay missing; ability facets stay non-decreasing and responsiveness non-increasing with High held."""
        measured_partial = {'by_effort': {'low': -2.0, 'max': -1.0}, 'status': 'measured_partial'}
        self.assertEqual(user_scores._manual_by_effort(-1.5, measured_partial, True),
                         ({'low': -2.0, 'high': -1.5, 'max': -1.0}, ['low', 'max']))
        # A measured effort contradicting the manual High is bounded by it and no longer listed as measured.
        self.assertEqual(user_scores._manual_by_effort(-0.5, measured_partial, True),
                         ({'low': -2.0, 'high': -0.5, 'max': -0.5}, ['low']))
        # Responsiveness falls with effort: Low (measured -2.0) cannot be slower than the set High, Max (measured -1.0) stays below it.
        self.assertEqual(user_scores._manual_by_effort(-0.5, measured_partial, False),
                         ({'low': -0.5, 'high': -0.5, 'max': -1.0}, ['max']))
        # An estimated curve is the facet's own curve: it moves to the manual High.
        estimated = {'by_effort': {'low': -1.0, 'high': 0.0, 'max': 0.5}, 'status': 'estimated'}
        self.assertEqual(user_scores._manual_by_effort(1.0, estimated, True), ({'low': 0.0, 'high': 1.0, 'max': 1.5}, []))
        # A filled High (five_axis.fill_high) is replaced; the measured efforts it was filled from stay.
        filled = {'by_effort': {'high': -1.3, 'max': -1.0}, 'status': 'estimated', 'measured_efforts': ['max']}
        self.assertEqual(user_scores._manual_by_effort(-1.2, filled, True), ({'high': -1.2, 'max': -1.0}, ['max']))
        # No own curve: only High, never another facet's curve; a declared fixed effort takes the score too.
        self.assertEqual(user_scores._manual_by_effort(0.3, None, True), ({'high': 0.3}, []))
        self.assertEqual(user_scores._manual_by_effort(0.3, None, False, 'medium'), ({'medium': 0.3, 'high': 0.3}, []))

    def test_manual_by_effort_bounds_each_facet_in_its_direction_with_the_set_efforts_held(self):
        """Rule (`user_scores._manual_by_effort`, direction from `rises_with_effort`): after a manual score sets High (and a fixed
        effort), a facet that rises with effort is non-decreasing and one that falls is non-increasing; the set efforts hold the
        score, efforts between two set ones equal it, efforts below/above are isotonic and on the allowed side of it. A group
        that already satisfies this is left as it is, and only a measured effort the bound did not move is listed as measured."""
        curves = {
            'falls': {'low': 3.0, 'medium': 1.5, 'high': 0.5, 'xhigh': -0.5, 'max': -1.5},
            'rises': {'low': -1.0, 'medium': 0.0, 'high': 0.5, 'xhigh': 1.0, 'max': 2.0},
            'zigzag': {'low': 0.2, 'medium': 1.4, 'high': 0.5, 'xhigh': 0.9, 'max': -0.3},
            'missing medium and xhigh': {'low': 2.0, 'high': 0.5, 'max': -1.0},
        }
        efforts = list(user_scores.EFFORTS)
        for rises in (True, False):
            sign = 1.0 if rises else -1.0
            for name, by in curves.items():
                cell = {'by_effort': dict(by), 'status': 'measured'}
                for fixed in (None, 'medium'):
                    for z in (-2.0, -1.0, -0.5, 0.0, 0.3, 0.5, 1.0, 1.7, 3.0):
                        label = (rises, name, fixed, z)
                        out, measured = user_scores._manual_by_effort(z, cell, rises, fixed)
                        pinned = [e for e in efforts if e in ('high', fixed)]
                        self.assertEqual(list(out), [e for e in efforts if e in out], label)
                        signed = [sign * out[e] for e in out]
                        self.assertEqual(signed, sorted(signed), label)                 # monotone in the facet's direction
                        lo, hi = efforts.index(pinned[0]), efforts.index(pinned[-1])
                        for e in out:
                            i = efforts.index(e)
                            if e in pinned or lo < i < hi:
                                self.assertEqual(out[e], z, (label, e))
                            elif i < lo:
                                self.assertLessEqual(sign * out[e], sign * z + 1e-9, (label, e))
                            else:
                                self.assertGreaterEqual(sign * out[e], sign * z - 1e-9, (label, e))
                        # the curve before the bound: measured efforts as measured, the set ones at z
                        base = {e: z if e in pinned else v for e, v in by.items()}
                        base = {e: base[e] for e in efforts if e in base}
                        for group in ([e for e in base if efforts.index(e) < lo], [e for e in base if efforts.index(e) > hi]):
                            vals = [sign * base[e] for e in group]
                            allowed = all(v <= sign * z for v in vals) if group and efforts.index(group[0]) < lo else all(v >= sign * z for v in vals)
                            if vals == sorted(vals) and allowed:
                                self.assertEqual({e: out[e] for e in group}, {e: base[e] for e in group}, (label, 'moved without need'))
                        self.assertEqual(measured, [e for e in by if e not in pinned and out[e] == by[e]], label)

    def test_r2_f7_named_model_without_effort_never_lets_unscored_effort_beat_scored_effort(self):
        accounts = copy.deepcopy(self.accounts)
        accounts['accounts']['codex']['plan'] = 'pro'
        accounts['accounts']['codex']['verified'] = True
        accounts['accounts']['codex']['windows'] = {'usage': {'hours': 168}}
        cx = accounts['accounts']['codex']['bundles']
        cx['luna56_medium'] = {**cx['sol_medium'], 'model': 'gpt-5.6-luna', 'effort': 'medium'}
        cx['luna56_high'] = {**cx['sol_high'], 'model': 'gpt-5.6-luna', 'effort': 'high'}
        cx['unscored_high'] = {**cx['sol_high'], 'model': 'gpt-oss-120b', 'effort': 'high'}
        usage = usage_windows(self.now, {'codex': [('usage', 90, 84)]})
        empty_pub = {'schema_version': user_scores.SCHEMA_VERSION, 'scores': {}}
        # On aesthetics, gpt-5.6-luna has a High reading below t1 (negative fit term) and no Medium score:
        # unscored Medium must be excluded rather than standing at 0.0 and beating scored High!
        task = {
            'objective': 'Named model without effort',
            'expected_hours': 0.5,
            'constraints': {'model': 'gpt-5.6-luna'},
            'needs': {'local_only': True},
            'profile': {
                'work_kind': 'frontend_ui', 'primary_level': 2, 'secondary_kind': 'none', 'secondary_level': 0,
                'needs_visual': False, 'local_only': True, 'wants_fast': False,
                'access': 'write', 'expected_hours': 0.5, 'task_count': '1',
                'axis_demand': {'creativity': 0, 'coding': 0, 'aesthetics': 2, 'tool_operation': 0},
            },
        }
        # The precondition is set here: gpt-5.6-luna has a design reading at High and none at Medium.
        five = keep_efforts(copy.deepcopy(axis_scores.load_five_axis()), 'gpt-5.6-luna', 'aesthetics', 'design', ('high',))
        with patch.object(user_scores, 'load_public', return_value=empty_pub), \
                patch.object(axis_scores, 'load_five_axis', return_value=five):
            d = allocator.plan(task, usage, self.now, accounts=copy.deepcopy(accounts),
                               host_policy_config={'allowed_accounts': ['codex']})
        self.assertEqual(d['choice'], 'luna56_high')
        rows = {c['id']: c for c in d['ranking']}
        self.assertTrue(rows['luna56_medium']['excluded'])
        self.assertIn('excluded because another effort of gpt-5.6-luna has trusted data', rows['luna56_medium']['excluded'][0])

        # Naming a model whose efforts are ALL unscored dispatches with fit=None and unscored_named_dispatch=True.
        task_unscored = {
            **task, 'constraints': {'model': 'gpt-oss-120b'},
            'profile': {**task['profile'], 'axis_demand': {'creativity': 0, 'coding': 2, 'aesthetics': 0, 'tool_operation': 0}},
        }
        # gpt-oss-120b has coding readings in the shipped data; the precondition (no reading at any effort) is set here.
        five_un = drop_facet(drop_facet(copy.deepcopy(axis_scores.load_five_axis()), 'gpt-oss-120b', 'coding', 'execution'),
                             'gpt-oss-120b', 'coding', 'faithfulness')
        with patch.object(user_scores, 'load_public', return_value=empty_pub),                 patch.object(axis_scores, 'load_five_axis', return_value=five_un):
            d_un = allocator.plan(task_unscored, usage, self.now, accounts=copy.deepcopy(accounts),
                                  host_policy_config={'allowed_accounts': ['codex']})
        self.assertEqual(d_un['choice'], 'unscored_high')
        un_row = next(c for c in d_un['ranking'] if c['id'] == 'unscored_high')
        self.assertIsNone(un_row['components']['fit'])
        self.assertTrue(un_row['unscored_named_dispatch'])

    def test_r2_f8_and_r2_f9_manual_score_invariants(self):
        targets = self.five['anchor']['targets']
        # 1) Manual tool_operation score never overwrites text_only / missing image_input modality to 'yes'.
        pub_doc = {
            'schema_version': user_scores.SCHEMA_VERSION,
            'scores': {
                'gpt-oss-120b': {
                    'tool_operation': {'level': 3.0, 'as_of': '2026-11-11', 'kind': 'maintainer'},
                },
                'flint-40b-free': {
                    'tool_operation.image_recognition': {'level': 3.0, 'as_of': '2026-11-11', 'kind': 'maintainer'},
                },
            },
        }
        overlaid = axis_scores.five_axis_overlay(
            copy.deepcopy(self.accounts), data=self.five, public_scores=pub_doc, read_local_state=False
        )
        self.assertEqual(overlaid['models']['gpt-oss-120b']['five_axis']['image_input']['status'], 'no')
        self.assertNotEqual(overlaid['models']['flint-40b-free']['five_axis']['image_input']['status'], 'yes')

        # 2) Whole-category coding maps per facet through each facet's own triad.
        code_doc = {
            'schema_version': user_scores.SCHEMA_VERSION,
            'scores': {
                'gpt-oss-120b': {
                    'coding': {'level': 2.0, 'as_of': '2026-11-11', 'kind': 'maintainer'},
                },
            },
        }
        over_code = axis_scores.five_axis_overlay(
            copy.deepcopy(self.accounts), data=self.five, public_scores=code_doc, read_local_state=False
        )
        facets = over_code['models']['gpt-oss-120b']['five_axis']['categories']['coding']['facets']
        z_faith = user_scores.mapped_z_for_target('coding.faithfulness', 2.0, targets)
        z_exec = user_scores.mapped_z_for_target('coding.execution', 2.0, targets)
        self.assertNotEqual(z_faith, z_exec)
        self.assertAlmostEqual(facets['faithfulness']['value'], z_faith, places=4)
        self.assertAlmostEqual(facets['execution']['value'], z_exec, places=4)

        # 3) Top-level aliases resolve cleanly.
        res_alias = user_scores.set_score('fable', 'creativity', 2.9, public=False, accounts=self.accounts)
        self.assertEqual(res_alias['model'], 'claude-fable-5-2')
        self.assertTrue(user_scores.clear_score('fable', 'creativity', public=False, accounts=self.accounts)['removed'])

        # 4) Corrupt local JSON raises ValueError rather than silently returning empty.
        local_path = user_scores.local_scores_path()
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text('{corrupt json', encoding='utf-8')
        with self.assertRaises(ValueError):
            user_scores.load_local()
        local_path.unlink()

        # 5) z_to_level round-trips [1, 3] and documents responsiveness >= 2.
        t1, t2, t3 = user_scores.target_triad(targets, 'creativity')
        for lvl in (1.0, 1.4, 2.0, 2.6, 3.0, 3.4):
            z = user_scores.level_to_z(lvl, t1, t2, t3)
            self.assertAlmostEqual(user_scores.z_to_level(z, t1, t2, t3), lvl, places=6)
        r1, r2, r3 = user_scores.target_triad(targets, 'tool_operation', 'responsiveness')
        self.assertLess(r2, r3)                                  # responsiveness has a scale like every facet
        self.assertEqual(user_scores.z_to_level(r2, r1, r2, r3), 2.0)
        self.assertAlmostEqual(user_scores.z_to_level(r3 + (r3 - r2), r1, r2, r3), 4.0, places=3)

        # 6) R2-F9: --public writes atomically with zero .lock files inside references/.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / '.git').mkdir()
            refs = repo / 'skills' / 'agent-compass' / 'references'
            refs.mkdir(parents=True)
            shutil.copy2(REFERENCES_DIR / 'user-scores.json', refs / 'user-scores.json')
            user_scores.set_score('grok-bot', 'creativity', 2.8, public=True,
                                  public_path=refs / 'user-scores.json', accounts=self.accounts)
            locks = list(refs.glob('*.lock'))
            self.assertEqual(locks, [])

        # 7) Facet overlay preserves sibling scores on minimal whole-category cells while keeping partial cells Mode-B.
        minimal_models = {
            'm-whole': {'five_axis': {'categories': {
                'coding': {'value': -0.2, 'by_effort': dict.fromkeys(user_scores.EFFORTS, -0.2), 'status': 'measured', 'facets': {}},
            }}},
            'm-missing': {'five_axis': {'categories': {
                'coding': {'value': None, 'by_effort': {}, 'status': 'missing', 'flag': 'missing',
                           'facets': {}, 'missing_facets': ['execution', 'faithfulness']},
            }}},
        }
        user_scores.apply_overlay(
            minimal_models, targets,
            public_scores={'schema_version': user_scores.SCHEMA_VERSION, 'scores': {
                'm-whole': {'coding.faithfulness': {'level': 3.0, 'as_of': '2026-11-11', 'kind': 'maintainer'}},
                'm-missing': {'coding.faithfulness': {'level': 3.0, 'as_of': '2026-11-11', 'kind': 'maintainer'}},
            }},
        )
        self.assertEqual(minimal_models['m-whole']['five_axis']['categories']['coding']['value'], -0.2)
        self.assertNotIn('missing_facets', minimal_models['m-whole']['five_axis']['categories']['coding'])
        self.assertIsNone(minimal_models['m-missing']['five_axis']['categories']['coding']['value'])
        self.assertEqual(minimal_models['m-missing']['five_axis']['categories']['coding']['missing_facets'], ['execution'])


class IsolatedEntryTests(unittest.TestCase):
    def test_the_score_stage_starts_under_python_isolated_mode(self):
        # Rule: `agentcompass score list|set|clear` runs user_scores.py with `python -I` (agentcompass/cli.py), where the
        # script directory is not on sys.path, so the file must reach its sibling modules by itself.
        import subprocess
        script = Path(__file__).resolve().parent / 'user_scores.py'
        for subcmd in (['list'], ['suggest', '--public']):
            done = subprocess.run([sys.executable, '-I', '-X', 'utf8', str(script), *subcmd], capture_output=True, text=True,
                                  encoding='utf-8', timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            json.loads(done.stdout)


class EffortBoundAndManualFillTests(unittest.TestCase):
    """Owner 2026-11-18: one rule against inversion along effort (`bound_along_effort`) for every value, and every effort
    filled after a hand-written score; a hand-written score wins over any estimate, the downward one included."""

    def test_bound_never_inverts_keeps_the_pinned_efforts_and_leaves_an_ordered_curve_alone(self):
        curves = [{'low': 0.2, 'medium': 1.4, 'high': 0.5, 'xhigh': 0.9, 'max': -0.3},
                  {'low': -1.0, 'medium': 0.0, 'high': 0.5, 'xhigh': 1.0, 'max': 2.0},
                  {'medium': 3.0, 'high': 0.5, 'max': 1.0}, {'low': 1.0, 'max': 0.0}]
        efforts = list(user_scores.EFFORTS)
        for by in curves:
            for rises in (True, False):
                sign = 1.0 if rises else -1.0
                out = user_scores.bound_along_effort(by, rises)
                anchor = user_scores.anchor_effort(by)
                self.assertEqual(out[anchor], by[anchor])                       # High (else nearest) never moves
                vals = [sign * out[e] for e in efforts if e in out]
                self.assertEqual(vals, sorted(vals), (by, rises))
                self.assertEqual(user_scores.bound_along_effort(out, rises), out)     # an ordered curve stays as it is
                at = efforts.index(anchor)
                for e in out:          # a moved effort takes its neighbour's value toward the anchor
                    if out[e] != by[e]:
                        i = efforts.index(e)
                        toward = [k for k in out if (efforts.index(k) > i if i < at else efforts.index(k) < i)]
                        near = min(toward, key=lambda k: abs(efforts.index(k) - i))
                        self.assertEqual(out[e], out[near], (by, rises, e))
        with self.assertRaises(ValueError):
            user_scores.bound_along_effort({'low': 1.0, 'high': 0.0}, True, ('low', 'high'))

    def test_a_manual_score_without_a_curve_fills_every_effort_from_the_panel_steps(self):
        steps = {'high': {'low': {'step': -1.0, 'n': 3}, 'medium': {'step': -0.4, 'n': 3}, 'xhigh': {'step': 0.3, 'n': 2},
                          'max': {'step': 0.2, 'n': 2}}}
        out, measured = user_scores._manual_by_effort(0.5, None, True, None, steps)
        self.assertEqual(out, {'low': -0.5, 'medium': 0.1, 'high': 0.5, 'xhigh': 0.8, 'max': 0.8})    # max held at xhigh
        self.assertEqual(measured, [])
        # an effort the panel gives no step for stays empty; nothing is borrowed
        out, _ = user_scores._manual_by_effort(0.5, None, True, None, {'high': {'low': {'step': -1.0, 'n': 2}}})
        self.assertEqual(out, {'low': -0.5, 'high': 0.5})

    def test_a_hand_written_score_wins_over_a_derived_value(self):
        five = axis_scores.load_five_axis()
        derived = [(m, a, f) for m, r in five['models'].items() for a, ax in r['axes'].items()
                   for f, c in (ax.get('facets') or {}).items() if (c.get('estimate') or {}).get('method') == 'tier_down']
        self.assertTrue(derived)
        accounts = account_fleet.load_accounts(account_fleet.reference_fleet_path())
        tested = [(m, a, f) for m, a, f in derived if m in accounts['models']]
        self.assertTrue(tested)
        for m, a, f in tested:
            pub = {'schema_version': user_scores.SCHEMA_VERSION,
                   'scores': {m: {f'{a}.{f}': {'level': 1.5, 'as_of': '2026-11-18', 'kind': 'maintainer'}}}}
            ov = axis_scores.five_axis_overlay(copy.deepcopy(accounts), data=copy.deepcopy(five), read_local_state=False,
                                               public_scores=pub)
            cell = ov['models'][m]['five_axis']['categories'][a]['facets'][f]
            z = round(user_scores.level_to_z(1.5, *user_scores.target_triad(five['anchor']['targets'], a, f)), 4)
            self.assertEqual((cell['status'], cell['value'], cell['by_effort']['high']), ('maintainer', z, z), (m, a, f))
            self.assertNotIn(f'{a}.{f}', ov['models'][m]['five_axis']['estimates'])


if __name__ == '__main__':
    unittest.main()
