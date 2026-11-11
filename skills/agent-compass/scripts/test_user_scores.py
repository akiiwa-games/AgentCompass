"""Tests for the two-tier manual capability score layer (maintainer public + local user) and Taxonomy R2 fix-round invariants."""
import copy
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

import allocator
import public_data
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


class ManualScoreLayerTests(unittest.TestCase):
    def setUp(self):
        isolated_router_state(self.addCleanup)
        self.five = copy.deepcopy(allocator.load_five_axis(REFERENCES_DIR / 'five-axis-scores.json'))
        self.accounts = allocator.load_accounts(allocator.reference_fleet_path())
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
        t1, t2, t3 = user_scores.target_triad(targets, 'divergence')
        self.assertAlmostEqual(user_scores.level_to_z(1.0, t1, t2, t3), t1, places=6)
        self.assertAlmostEqual(user_scores.level_to_z(2.0, t1, t2, t3), t2, places=6)
        self.assertAlmostEqual(user_scores.level_to_z(3.0, t1, t2, t3), t3, places=6)
        self.assertAlmostEqual(user_scores.level_to_z(1.5, t1, t2, t3), t1 + 0.5 * (t2 - t1), places=6)
        self.assertAlmostEqual(user_scores.level_to_z(2.5, t1, t2, t3), t2 + 0.5 * (t3 - t2), places=6)
        self.assertAlmostEqual(user_scores.level_to_z(3.5, t1, t2, t3), t3 + 0.5 * (t3 - t2), places=6)
        self.assertAlmostEqual(user_scores.level_to_z(0.5, t1, t2, t3), t1 - 0.5 * (t2 - t1), places=6)

        # On tool_operation.responsiveness, t3 is raised to t2; > 3 falls back to (t2 - t1) so > 3 still increases strictly.
        r1, r2, r3 = user_scores.target_triad(targets, 'tool_operation', 'responsiveness')
        self.assertEqual(r2, r3)
        self.assertAlmostEqual(
            user_scores.level_to_z(3.5, r1, r2, r3),
            r3 + 0.5 * (r2 - r1),
            places=6,
        )

    def test_maintainer_score_wins_over_measured_and_local_user_beats_maintainer(self):
        maintainer_doc = {
            'schema_version': 1,
            'scores': {
                'gpt-6.1-sol': {
                    'divergence': {'level': 3.2, 'as_of': '2026-11-11', 'kind': 'maintainer'},
                }
            },
        }
        local_doc = {
            'schema_version': 1,
            'scores': {
                'gpt-6.1-sol': {
                    'divergence': {'level': 1.2, 'as_of': '2026-11-11', 'kind': 'user'},
                }
            },
        }
        targets = self.five['anchor']['targets']
        expected_m = user_scores.mapped_z_for_target('divergence', 3.2, targets)
        expected_u = user_scores.mapped_z_for_target('divergence', 1.2, targets)

        only_m = allocator.five_axis_overlay(
            copy.deepcopy(self.accounts), data=self.five, public_scores=maintainer_doc, local_scores=None,
            read_local_state=False,
        )
        ax_m = only_m['models']['gpt-6.1-sol']['five_axis']['categories']['divergence']
        self.assertEqual(ax_m['status'], 'maintainer')
        self.assertEqual(ax_m['value'], expected_m)
        # Multi-effort curve preserves relative effort deltas rather than flattening to a constant.
        self.assertGreater(ax_m['by_effort']['high'], ax_m['by_effort']['low'])

        both = allocator.five_axis_overlay(
            copy.deepcopy(self.accounts), data=self.five, public_scores=maintainer_doc, local_scores=local_doc,
            read_local_state=False,
        )
        ax_u = both['models']['gpt-6.1-sol']['five_axis']['categories']['divergence']
        self.assertEqual(ax_u['status'], 'user')
        self.assertEqual(ax_u['value'], expected_u)

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
                'work_kind': 'tool_execution', 'reasoning_depth': 2, 'code_complexity': 0,
                'needs_visual': False, 'needs_gui': False, 'local_only': False, 'wants_fast': False,
                'read_only_review': False, 'access': 'read-only', 'expected_hours': 1.0, 'hours_confidence': 'medium',
                'urgency': 'normal', 'interaction_style': 'exploration', 'multi_task': False, 'task_count': '1',
                'axis_demand': {'divergence': 0, 'coding': 1, 'aesthetics': 0, 'tool_operation': 2},
            },
        }
        empty_pub = {'schema_version': 1, 'scores': {}}
        with patch.object(allocator, 'load_public_user_scores', return_value=empty_pub):
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
            'schema_version': 1,
            'scores': {
                'grok-bot': {
                    'tool_operation.responsiveness': {'level': 2.5, 'as_of': '2026-11-11', 'kind': 'maintainer'},
                }
            },
        }
        user_scores.set_score('grok-bot', 'tool_operation.responsiveness', 1.1, public=False, accounts=accounts)
        with patch.object(allocator, 'load_public_user_scores', return_value=pub_doc):
            fresh_plan = allocator.plan(task, usage, self.now, accounts=copy.deepcopy(accounts), read_local_state=False)
            local_plan = allocator.plan(task, usage, self.now, accounts=copy.deepcopy(accounts), read_local_state=True)
        gb_fresh = next(c for c in fresh_plan['ranking'] if c['id'] == 'grok_bot')
        gb_local = next(c for c in local_plan['ranking'] if c['id'] == 'grok_bot')
        self.assertEqual(gb_fresh['excluded'], [])
        self.assertEqual(gb_local['excluded'], [])
        self.assertGreater(gb_fresh['components']['fit'], gb_local['components']['fit'])
        self.assertTrue(any('maintainer' in str(n) for n in gb_fresh['notes']))
        self.assertTrue(any('user' in str(n) for n in gb_local['notes']))

    def test_public_data_checks_pass_with_uncited_maintainer_values_and_local_never_leaks(self):
        before_manifest = (REFERENCES_DIR / 'public-data.json').read_bytes()
        before_shipped = (REFERENCES_DIR / 'user-scores.json').read_bytes()

        # Local CLI call writes only under ROUTER_STATE_DIR and never touches shipped references.
        with patch.object(sys, 'argv', ['route_judge.py', 'score', 'set', 'gpt-6.1-sol', 'coding.faithfulness', '2.8']):
            rc = route_judge.main()
        self.assertEqual(rc, 0)
        self.assertEqual((REFERENCES_DIR / 'public-data.json').read_bytes(), before_manifest)
        self.assertEqual((REFERENCES_DIR / 'user-scores.json').read_bytes(), before_shipped)
        listed = user_scores.list_scores('gpt-6.1-sol', scope='local')
        self.assertEqual(listed['local']['gpt-6.1-sol']['coding.faithfulness']['kind'], 'user')
        self.assertEqual(listed['local']['gpt-6.1-sol']['coding.faithfulness']['level'], 2.8)
        with patch.object(sys, 'argv', ['route_judge.py', 'score', 'clear', 'gpt-6.1-sol', 'coding.faithfulness']):
            self.assertEqual(route_judge.main(), 0)
        self.assertEqual(user_scores.list_scores('gpt-6.1-sol', scope='local')['local'], {})

        # Simulated git repo checkout: `--public` writes kind='maintainer' without citations and refreshes public-data.json.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / '.git').mkdir()
            refs = repo / 'skills' / 'agent-compass' / 'references'
            refs.mkdir(parents=True)
            for name in (*public_data.FILES, public_data.MANIFEST):
                shutil.copy2(REFERENCES_DIR / name, refs / name)
            res = user_scores.set_score(
                'gpt-6.1-sol', 'divergence', 2.6, note='maintainer impression', public=True,
                public_path=refs / 'user-scores.json', accounts=self.accounts,
            )
            self.assertEqual(res['entry']['kind'], 'maintainer')
            self.assertNotIn('url', res['entry'])
            manifest = json.loads((refs / 'public-data.json').read_text(encoding='utf-8'))
            self.assertEqual(public_data.check_set(refs, manifest), ('ok', ''))

            # Writing kind='user' into the public file is rejected by public_data.check_set.
            bad = {'schema_version': 1, 'scores': {'gpt-6.1-sol': {'divergence': {'level': 2.0, 'as_of': '2026-11-11', 'kind': 'user'}}}}
            (refs / 'user-scores.json').write_text(json.dumps(bad), encoding='utf-8')
            bad_manifest = public_data.build_manifest(refs)
            status, detail = public_data.check_set(refs, bad_manifest)
            self.assertEqual(status, 'incomplete')
            self.assertIn('kind', detail)


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
                                        {'models': {m: {} for m in models}}).score()
        self.assertIsNone(rows['partial_coder']['axes']['coding']['value'])
        self.assertEqual(rows['partial_coder']['axes']['coding']['missing_facets'], ['faithfulness'])
        self.assertEqual(rows['partial_coder']['axes']['tool_operation']['by_level']['2']['by_effort']['high'], 0.2)
        self.assertEqual(rows['partial_coder']['axes']['tool_operation']['by_level']['3']['by_effort'], {})
        self.assertEqual(rows['partial_coder']['axes']['tool_operation']['missing_facets'], ['image_recognition'])

        # Named dispatch / direct fit monotonicity: missing responsiveness stands at zero (0.0) at both L2 and L3.
        targets = allocator.load_five_axis(REFERENCES_DIR / 'five-axis-scores.json')['anchor']['targets']
        vals = {'divergence': 0.0, 'coding': 0.0, 'aesthetics': 0.0,
                'tool_operation': {'responsiveness': None, 'image_recognition': 0.5}}
        d2 = {'divergence': 0, 'coding': 0, 'aesthetics': 0, 'tool_operation': 2}
        d3 = {'divergence': 0, 'coding': 0, 'aesthetics': 0, 'tool_operation': 3}
        fit_l2, _ = allocator.five_axis_fit(vals, d2, allocator.five_axis_targets(d2, targets))
        fit_l3, _ = allocator.five_axis_fit(vals, d3, allocator.five_axis_targets(d3, targets))
        self.assertEqual(fit_l2, 0.0)
        self.assertEqual(fit_l3, 0.0)
        self.assertIsNone(allocator.five_axis_shares(vals, targets)['tool_operation'])

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
        opus_div = scores['models']['claude-opus-5-5']['axes']['divergence']['by_effort']
        self.assertGreater(opus_div['high'], opus_div['low'])
        # F4: every full-panel board has sd_panel == 1.0 on High rows, and partial-panel secondary boards carry scale != 1.0
        for facet_key, link in scores['links'].items():
            for src in link['full_panel_boards']:
                self.assertEqual(link['sd_at_high'][src], 1.0, (facet_key, src))
        self.assertNotEqual(scores['links']['tool_operation.image_recognition']['scales']['aa.mmmu_pro'], 1.0)
        self.assertNotEqual(scores['links']['aesthetics.design']['scales']['designarena.svg'], 1.0)

    def test_f3_economy_cost_is_inside_net_score_without_score_rounding(self):
        accounts = allocator.load_accounts(allocator.reference_fleet_path())
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
                'work_kind': 'general_implementation', 'reasoning_depth': 3, 'code_complexity': 2,
                'needs_visual': False, 'needs_gui': False, 'local_only': True, 'wants_fast': False,
                'read_only_review': False, 'access': 'write', 'expected_hours': 0.1, 'hours_confidence': 'medium',
                'urgency': 'normal', 'interaction_style': 'engineering', 'multi_task': False, 'task_count': '1',
                'axis_demand': {'divergence': 0, 'coding': 3, 'aesthetics': 0, 'tool_operation': 1},
            },
        }
        plan = allocator.plan(task, usage, now, accounts=copy.deepcopy(accounts),
                              host_policy_config={'allowed_accounts': ['claude', 'claude-2', 'codex']}, read_local_state=False)
        c1 = next(c for c in plan['ranking'] if c['id'] == 'opus55_high')
        c2 = next(c for c in plan['ranking'] if c['id'] == 'claude2_opus55_high')
        c1_fast = next(c for c in plan['ranking'] if c['id'] == 'opus55_high+fast')
        self.assertLess(c1['components']['opportunity_cost'], c2['components']['opportunity_cost'])
        self.assertGreater(c1['score'], c2['score'])
        self.assertEqual(allocator.five_axis_deciding_key(c1, c2), 'net_score')
        # 2x burn of +fast has higher C and lower N than plain when wants_fast=False at mid-week ample (Pro and Max20 alike):
        self.assertLess(c1['components']['opportunity_cost'], c1_fast['components']['opportunity_cost'])
        self.assertGreater(c1['score'], c1_fast['score'])
        self.assertEqual(allocator.five_axis_deciding_key(c1, c1_fast), 'net_score')

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
        self.assertEqual(allocator.five_axis_deciding_key(m1, m1_fast), 'net_score')
        self.assertFalse(plan_max20['selected_bundle'].get('fast'))

        # Free unmetered dot_text beats billed astra_high on net_score when both reach the same fit:
        research_task = {
            'objective': 'Compare vendor APIs from public docs',
            'expected_hours': 0.5,
            'needs': {'local_only': False, 'outbound_ok': True},
            'profile': {
                'work_kind': 'web_research', 'reasoning_depth': 3, 'code_complexity': 0,
                'needs_visual': False, 'needs_gui': False, 'local_only': False, 'wants_fast': False,
                'read_only_review': False, 'access': 'read-only', 'expected_hours': 0.5, 'hours_confidence': 'medium',
                'urgency': 'normal', 'interaction_style': 'exploration', 'multi_task': False, 'task_count': '1',
                'axis_demand': {'divergence': 3, 'coding': 0, 'aesthetics': 0, 'tool_operation': 1},
            },
        }
        res_plan = allocator.plan(research_task, usage, now, accounts=copy.deepcopy(accounts),
                                  host_policy_config={'allowed_accounts': ['codex', 'dot']}, read_local_state=False)
        dot_row = next(c for c in res_plan['ranking'] if c['id'] == 'dot_text')
        astra_row = next(c for c in res_plan['ranking'] if c['id'] == 'astra_high')
        self.assertEqual(dot_row['components']['opportunity_cost'], 0.0)
        self.assertGreater(astra_row['components']['opportunity_cost'], 0.0)
        self.assertGreater(dot_row['score'], astra_row['score'])
        self.assertEqual(allocator.five_axis_deciding_key(dot_row, astra_row), 'net_score')

    def test_f5_auto_matched_medium_coupling_gets_continuity_and_tool_op_never_zeros_other_axes(self):
        scores = allocator.load_five_axis(REFERENCES_DIR / 'five-axis-scores.json')
        targets = scores['anchor']['targets']
        cont = allocator.five_axis_continuity_value(
            {'divergence': 0, 'coding': 2, 'aesthetics': 2, 'tool_operation': 3}, targets
        )
        self.assertGreater(cont, 1.0)

        accounts = allocator.load_accounts(allocator.reference_fleet_path())
        usage = usage_windows(self.now, {'claude': [('five_hour', 10, 3), ('seven_day', 10, 84)],
                                         'codex': [('five_hour', 10, 3), ('usage', 10, 84)]})
        base_profile = {
            'work_kind': 'general_implementation', 'reasoning_depth': 2, 'code_complexity': 2,
            'needs_visual': False, 'needs_gui': False, 'local_only': True, 'wants_fast': False,
            'read_only_review': False, 'access': 'write', 'expected_hours': 1.0, 'hours_confidence': 'medium',
            'urgency': 'normal', 'interaction_style': 'engineering', 'multi_task': False, 'task_count': '1',
            'axis_demand': {'divergence': 0, 'coding': 2, 'aesthetics': 0, 'tool_operation': 2},
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
                self.assertGreater(sum(allocator.opencode_local_spend(models_tbl, None, read_local_db=True)[0].values()), 0.0)
                self.assertEqual(allocator.opencode_local_spend(models_tbl, None, read_local_db=False)[0], {})

        proxy = {'endpoint': 'https://proxy.example.com', 'api_key': 'sk-test', 'family': 'claude'}
        with patch.object(roll.dispatch_flow, 'paseo_config', return_value={}), \
                patch.object(roll, '_router_override_proxy_spec', return_value=proxy):
            fam_fable, env_fable = roll.provider_definition('my-proxy', bundle={'model': 'fable'})
            fam_terra, env_terra = roll.provider_definition('my-proxy', bundle={'model': 'gpt-6.1-terra'})
        self.assertEqual(fam_fable, 'claude')
        self.assertIn('ANTHROPIC_BASE_URL', env_fable)
        self.assertEqual(fam_terra, 'codex')
        self.assertIn('OPENAI_BASE_URL', env_terra)

        doc = user_scores.validate_document(
            {'schema_version': 1, 'scores': {'grok-bot': {'tool_operation.responsiveness': {'level': 2.5, 'date': '2026-11-10', 'kind': 'maintainer'}}}},
            expected_kind='maintainer',
        )
        self.assertEqual(doc['scores']['grok-bot']['tool_operation.responsiveness']['as_of'], '2026-11-10')


class TaxonomyR2Round2FixTests(unittest.TestCase):
    """Regression coverage for Taxonomy R2 review-2 findings (R2-F1, R2-F6, R2-F7, R2-F8, R2-F9)."""

    def setUp(self):
        isolated_router_state(self.addCleanup)
        self.now = datetime(2026, 11, 11, 8, 0, tzinfo=timezone.utc)
        self.accounts = allocator.load_accounts(allocator.reference_fleet_path())
        self.five = copy.deepcopy(allocator.load_five_axis(REFERENCES_DIR / 'five-axis-scores.json'))

    def test_r2_f1_end_to_end_decision_snapshot_and_plan_stage_with_non_empty_user_scores(self):
        # Populate local user scores so both eligible (selected) and excluded rows carry manual_scores.
        user_scores.set_score('claude-opus-5-5', 'divergence', 3.0, public=False, accounts=self.accounts)
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
                'work_kind': 'general_implementation', 'reasoning_depth': 3, 'code_complexity': 2,
                'needs_visual': False, 'needs_gui': False, 'local_only': True, 'wants_fast': False,
                'read_only_review': False, 'access': 'write', 'expected_hours': 0.5, 'hours_confidence': 'medium',
                'urgency': 'normal', 'interaction_style': 'engineering', 'multi_task': False, 'task_count': '1',
                'axis_demand': {'divergence': 3, 'coding': 2, 'aesthetics': 0, 'tool_operation': 1},
            },
        }
        decision = allocator.plan(task, usage, self.now, accounts=accounts, read_local_state=True)
        snap = allocator.decision_snapshot(decision)
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
            with patch.object(allocator, 'load_accounts', return_value=accounts):
                result, rc = route_judge.plan_stage({'task': task}, args)
            self.assertEqual(rc, 0)
            self.assertNotEqual(result['choice'], 'none')
            self.assertTrue(any(r.get('manual_scores') for r in result['ranking']))

    def test_r2_f6_mode_b_score_set_makes_partial_and_max_only_models_routable(self):
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
                'work_kind': 'general_implementation', 'reasoning_depth': 2, 'code_complexity': 2,
                'needs_visual': False, 'needs_gui': False, 'local_only': True, 'wants_fast': False,
                'read_only_review': False, 'access': 'write', 'expected_hours': 0.5, 'hours_confidence': 'medium',
                'urgency': 'normal', 'interaction_style': 'engineering', 'multi_task': False, 'task_count': '1',
                'axis_demand': {'divergence': 0, 'coding': 2, 'aesthetics': 0, 'tool_operation': 0},
            },
        }
        empty_pub = {'schema_version': 1, 'scores': {}}
        with patch.object(allocator, 'load_public_user_scores', return_value=empty_pub):
            before = allocator.plan(code_task, usage, self.now, accounts=copy.deepcopy(accounts),
                                    host_policy_config={'allowed_accounts': ['codex']})
        rows_before = {c['id']: c for c in before['ranking']}
        for bid in ('gpt6_sol_high', 'gpt6_sol_max', 'gpt56_sol_high', 'ds_v4_high', 'ds_v4_max'):
            self.assertTrue(rows_before[bid]['excluded'], bid)
            self.assertIn('agentcompass score set', rows_before[bid]['excluded'][0])

        # Setting the missing targets per the Mode-B error lifts the capability exclusion at both High and Max:
        user_scores.set_score('gpt-6-sol', 'coding.faithfulness', 3.0, public=False, accounts=accounts)
        user_scores.set_score('gpt-6-sol', 'coding.execution', 2.4, public=False, accounts=accounts)
        user_scores.set_score('gpt-5.6-sol', 'coding.execution', 2.0, public=False, accounts=accounts)
        user_scores.set_score('deepseek-v4-pro', 'coding.faithfulness', 2.5, public=False, accounts=accounts)
        user_scores.set_score('deepseek-v4-pro', 'coding.execution', 2.2, public=False, accounts=accounts)
        with patch.object(allocator, 'load_public_user_scores', return_value=empty_pub):
            after = allocator.plan(code_task, usage, self.now, accounts=copy.deepcopy(accounts),
                                   host_policy_config={'allowed_accounts': ['codex']})
        rows_after = {c['id']: c for c in after['ranking']}
        self.assertEqual(rows_after['gpt6_sol_high']['excluded'], [])
        self.assertEqual(rows_after['gpt6_sol_max']['excluded'], [])
        self.assertEqual(rows_after['gpt56_sol_high']['excluded'], [])
        self.assertEqual(rows_after['ds_v4_max']['excluded'], [])
        for bid in ('ds_v4_high', 'ds_v4_max'):
            self.assertFalse(any('no trusted data' in e for e in rows_after[bid]['excluded']), rows_after[bid]['excluded'])

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
        empty_pub = {'schema_version': 1, 'scores': {}}
        # On aesthetics, gpt-5.6-luna has High = -3.0273 < t1 = -2.8934 (negative fit term) and no Medium score:
        # unscored Medium must be excluded rather than standing at 0.0 and beating scored High!
        task = {
            'objective': 'Named model without effort',
            'expected_hours': 0.5,
            'constraints': {'model': 'gpt-5.6-luna'},
            'needs': {'local_only': True},
            'profile': {
                'work_kind': 'frontend_implementation', 'reasoning_depth': 2, 'code_complexity': 2,
                'needs_visual': False, 'needs_gui': False, 'local_only': True, 'wants_fast': False,
                'read_only_review': False, 'access': 'write', 'expected_hours': 0.5, 'hours_confidence': 'medium',
                'urgency': 'normal', 'interaction_style': 'engineering', 'multi_task': False, 'task_count': '1',
                'axis_demand': {'divergence': 0, 'coding': 0, 'aesthetics': 2, 'tool_operation': 0},
            },
        }
        with patch.object(allocator, 'load_public_user_scores', return_value=empty_pub):
            d = allocator.plan(task, usage, self.now, accounts=copy.deepcopy(accounts),
                               host_policy_config={'allowed_accounts': ['codex']})
        self.assertEqual(d['choice'], 'luna56_high')
        rows = {c['id']: c for c in d['ranking']}
        self.assertTrue(rows['luna56_medium']['excluded'])
        self.assertIn('excluded because another effort of gpt-5.6-luna has trusted data', rows['luna56_medium']['excluded'][0])

        # Naming a model whose efforts are ALL unscored dispatches with fit=None and unscored_named_dispatch=True.
        task_unscored = {
            **task, 'constraints': {'model': 'gpt-oss-120b'},
            'profile': {**task['profile'], 'axis_demand': {'divergence': 0, 'coding': 2, 'aesthetics': 0, 'tool_operation': 0}},
        }
        with patch.object(allocator, 'load_public_user_scores', return_value=empty_pub):
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
            'schema_version': 1,
            'scores': {
                'gpt-oss-120b': {
                    'tool_operation': {'level': 3.0, 'as_of': '2026-11-11', 'kind': 'maintainer'},
                },
                'flint-40b-free': {
                    'tool_operation.image_recognition': {'level': 3.0, 'as_of': '2026-11-11', 'kind': 'maintainer'},
                },
            },
        }
        overlaid = allocator.five_axis_overlay(
            copy.deepcopy(self.accounts), data=self.five, public_scores=pub_doc, read_local_state=False
        )
        self.assertEqual(overlaid['models']['gpt-oss-120b']['five_axis']['image_input']['status'], 'no')
        self.assertNotEqual(overlaid['models']['flint-40b-free']['five_axis']['image_input']['status'], 'yes')

        # 2) Whole-category coding maps per facet through each facet's own triad.
        code_doc = {
            'schema_version': 1,
            'scores': {
                'gpt-oss-120b': {
                    'coding': {'level': 2.0, 'as_of': '2026-11-11', 'kind': 'maintainer'},
                },
            },
        }
        over_code = allocator.five_axis_overlay(
            copy.deepcopy(self.accounts), data=self.five, public_scores=code_doc, read_local_state=False
        )
        facets = over_code['models']['gpt-oss-120b']['five_axis']['categories']['coding']['facets']
        z_faith = user_scores.mapped_z_for_target('coding.faithfulness', 2.0, targets)
        z_exec = user_scores.mapped_z_for_target('coding.execution', 2.0, targets)
        self.assertNotEqual(z_faith, z_exec)
        self.assertAlmostEqual(facets['faithfulness']['value'], z_faith, places=4)
        self.assertAlmostEqual(facets['execution']['value'], z_exec, places=4)

        # 3) Top-level aliases resolve cleanly.
        res_alias = user_scores.set_score('fable', 'divergence', 2.9, public=False, accounts=self.accounts)
        self.assertEqual(res_alias['model'], 'claude-fable-5-2')
        self.assertTrue(user_scores.clear_score('fable', 'divergence', public=False, accounts=self.accounts)['removed'])

        # 4) Corrupt local JSON raises ValueError rather than silently returning empty.
        local_path = user_scores.local_scores_path()
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text('{corrupt json', encoding='utf-8')
        with self.assertRaises(ValueError):
            user_scores.load_local()
        local_path.unlink()

        # 5) z_to_level round-trips [1, 3] and documents responsiveness >= 2.
        t1, t2, t3 = user_scores.target_triad(targets, 'divergence')
        for lvl in (1.0, 1.4, 2.0, 2.6, 3.0, 3.4):
            z = user_scores.level_to_z(lvl, t1, t2, t3)
            self.assertAlmostEqual(user_scores.z_to_level(z, t1, t2, t3), lvl, places=6)
        r1, r2, r3 = user_scores.target_triad(targets, 'tool_operation', 'responsiveness')
        self.assertEqual(r2, r3)
        self.assertEqual(user_scores.z_to_level(r2, r1, r2, r3), 2.0)
        self.assertGreater(user_scores.z_to_level(r2 + 0.2, r1, r2, r3), 3.0)

        # 6) R2-F9: --public writes atomically with zero .lock files inside references/.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / '.git').mkdir()
            refs = repo / 'skills' / 'agent-compass' / 'references'
            refs.mkdir(parents=True)
            for name in (*public_data.FILES, public_data.MANIFEST):
                shutil.copy2(REFERENCES_DIR / name, refs / name)
            user_scores.set_score('grok-bot', 'divergence', 2.8, public=True,
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
            public_scores={'schema_version': 1, 'scores': {
                'm-whole': {'coding.faithfulness': {'level': 3.0, 'as_of': '2026-11-11', 'kind': 'maintainer'}},
                'm-missing': {'coding.faithfulness': {'level': 3.0, 'as_of': '2026-11-11', 'kind': 'maintainer'}},
            }},
        )
        self.assertEqual(minimal_models['m-whole']['five_axis']['categories']['coding']['value'], -0.2)
        self.assertNotIn('missing_facets', minimal_models['m-whole']['five_axis']['categories']['coding'])
        self.assertIsNone(minimal_models['m-missing']['five_axis']['categories']['coding']['value'])
        self.assertEqual(minimal_models['m-missing']['five_axis']['categories']['coding']['missing_facets'], ['execution'])


if __name__ == '__main__':
    unittest.main()
