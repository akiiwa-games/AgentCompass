"""Contract tests for allocator.py, override_store.py, and antigravity_telemetry.py."""
import argparse
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import allocator
import antigravity_telemetry
import override_store
import route_judge as router


EXPIRING_SUBSCRIPTION = {
    'plan': 'Grok subscription', 'billing': 'subscription', 'verified': True,
    'expires_at': '2026-10-10T23:59:00+08:00', 'expires_precision': 'date',
    'windows': {'credits': {'hours': 168}},
    'burn_pct_per_worker_hour': {'credits': 5.0},
    'burn_basis': 'Test fixture: expiring subscription account.',
    'bundles': {'grok47_high': {'model': 'grok-4.7', 'effort': 'high'}},
}
_real_load_accounts = allocator.load_accounts
_fixture_patch = None


def _load_accounts_with_expiring_subscription(path=None):
    data = _real_load_accounts(path)
    if path is None:
        data['accounts']['grok'] = json.loads(json.dumps(EXPIRING_SUBSCRIPTION))
    return data


def setUpModule():
    global _fixture_patch
    account_override = patch.dict(allocator.os.environ, {
        'ROUTER_ACCOUNTS_OVERRIDE': str(Path(tempfile.gettempdir()) / 'no-such-router-accounts-override.json')})
    account_override.start()
    unittest.addModuleCleanup(account_override.stop)
    discovery = patch.object(allocator.provider_inventory, 'current', return_value={'accounts': {}, 'launchers': {}})
    discovery.start()
    unittest.addModuleCleanup(discovery.stop)
    availability = patch.object(allocator.host_policy, 'load_policy', return_value=None)
    availability.start()
    unittest.addModuleCleanup(availability.stop)
    _fixture_patch = patch.object(allocator, 'load_accounts', _load_accounts_with_expiring_subscription)
    _fixture_patch.start()
    registry_temp = tempfile.TemporaryDirectory(prefix='router-registry-test-')
    unittest.addModuleCleanup(registry_temp.cleanup)
    registry_env = patch.dict(router.os.environ, {'ROUTER_DISPATCH_DB': str(Path(registry_temp.name) / 'dispatch.sqlite3')})
    registry_env.start()
    unittest.addModuleCleanup(registry_env.stop)
    overrides = patch.dict(allocator.os.environ, {'ROUTER_OVERRIDES': str(Path(tempfile.gettempdir()) / 'no-such-router-overrides.json')})
    overrides.start()
    unittest.addModuleCleanup(overrides.stop)


def tearDownModule():
    _fixture_patch.stop()
    unittest.case.doModuleCleanups()


class AllocatorTests(unittest.TestCase):
    DOT_ROUTES = ('dot_execution', 'web_research', 'text_in_text_out', 'self_contained_code')
    BULK = {'work_kind': 'bounded_bulk_work', 'difficulty': 'hard', 'expected_hours': 6}
    SCARCE = {'claude': [('five_hour', 90, 3), ('seven_day', 12, 100), ('weekly_fable', 100, 100)], 'codex': [('usage', 12, 100)]}
    PLENTY = {'claude': [('five_hour', 95, 3), ('seven_day', 95, 100), ('weekly_fable', 100, 100)], 'codex': [('usage', 95, 100)]}

    def setUp(self):
        self.now = datetime(2026, 10, 9, 3, 0, tzinfo=timezone.utc)
        patcher = patch.object(allocator, 'capacity_override_path', return_value=Path(tempfile.gettempdir()) / 'no-such-capacity.json')
        patcher.start()
        self.addCleanup(patcher.stop)

    def usage(self, **overrides):
        windows = {
            'claude': [('five_hour', 90, 3), ('seven_day', 60, 100), ('weekly_fable', 100, 100)],
            'codex': [('usage', 60, 100)],
            'grok': [('credits', 0, 2.2)],
            'antigravity': [('gemini-5h', 90, 3), ('gemini-weekly', 60, 100), ('3p-5h', 90, 3), ('3p-weekly', 60, 100)],
        }
        windows.update(overrides)
        windows.setdefault('claude-2', list(windows['claude']))
        accounts = {name: {'windows': [
            {'id': wid, 'remaining_pct_before_reset': rem, 'observed_remaining_pct': rem,
             'seconds_until_reset': hours * 3600, 'resets_at': (self.now + timedelta(hours=hours)).isoformat()}
            for wid, rem, hours in rows]} for name, rows in windows.items()}
        return {'read_ok': True, 'accounts': accounts}

    def no_flash(self, **accounts):
        flash = [('gemini-5h', 3, 3), ('gemini-weekly', 5, 100), ('3p-5h', 3, 3), ('3p-weekly', 5, 100)]
        return self.usage(antigravity=flash, **accounts)

    def plan(self, task, usage=None, now=None, **kwargs):
        needs = task.get('needs') if isinstance(task.get('needs'), dict) else {}
        if task.get('work_kind') not in self.DOT_ROUTES and 'local_only' not in needs:
            task = dict(task, needs=dict(needs, local_only=True))
        return allocator.plan(task, usage or self.usage(), now or self.now, **kwargs)

    def rows(self, result):
        return {c['id']: c for c in result['ranking']}

    def chain(self, bundle='sol_xhigh', coupling='high', total=6, tokens=120000, compacted=None, compactions=None, **extra):
        worker = {'bundle': bundle, 'context_tokens': tokens,
                  **({'compacted_tokens': compacted} if compacted is not None else {}),
                  **({'compactions': compactions} if compactions is not None else {})}
        return {'id': 'refactor-1', 'coupling': coupling, 'total_hours': total, 'worker': worker, **extra}

    def test_when_rule_bundle_stays_pinnable(self):
        # A `when`-limited bundle is dropped from normal ranking but a user pin (dispatch) still finds it.
        task = {'work_kind': 'debug_review', 'difficulty': 'hard', 'profile': self.profile(reasoning_depth=3)}
        self.assertNotIn('agy_opus55_low', self.rows(self.plan(task)))
        row = self.rows(self.plan(dict(task, pinned_bundle='agy_opus55_low'))).get('agy_opus55_low')
        self.assertIsNotNone(row)
        self.assertEqual((row['bundle']['account'], row['bundle']['effort']), ('antigravity', 'low'))

    def profile(self, **fields):
        base = {'reasoning_depth': 2, 'code_complexity': 2, 'context_scale': 1, 'hallucination_risk': 2, 'error_cost': 2,
                'needs_visual': False, 'needs_gui': False, 'local_only': True, 'interaction_style': 'engineering',
                'reason': 'test profile'}
        return dict(base, **fields)

    def unsubscribed(self, account):
        accounts = allocator.load_accounts()
        accounts['accounts'].pop(account)
        return accounts

    def codex_bundles(self):
        return [b for b, v in allocator.bundles(allocator.load_accounts()).items() if v['account'] == 'codex']

    def test_chain_continuity_and_handoff_rotation(self):
        # 1. Active high-coupling chain resumes owner; blocked owner asks user; low-coupling switches silently
        resumed = self.plan({'work_kind': 'general_implementation', 'difficulty': 'coupled', 'expected_hours': 2, 'chain': self.chain()})
        self.assertEqual((resumed['choice'], resumed['decision']['action'], resumed['decision']['needs_user_decision']),
                         ('sol_xhigh', 'resume', False))
        blocked_usage = self.usage(codex=[('usage', 1, 121)])
        blocked = self.plan({'work_kind': 'general_implementation', 'difficulty': 'coupled', 'expected_hours': 2, 'chain': self.chain()}, blocked_usage)
        self.assertTrue(blocked['decision']['needs_user_decision'])
        self.assertEqual(blocked['decision']['drift_risk'], 'high')
        self.assertAlmostEqual(blocked['decision']['wait_hours'], 121, delta=0.1)
        self.assertNotEqual(blocked['selected_bundle']['account'], 'codex')
        low_c = self.plan({'work_kind': 'general_implementation', 'difficulty': 'hard', 'expected_hours': 1,
                           'chain': self.chain(coupling='low')}, blocked_usage)
        self.assertFalse(low_c['decision']['needs_user_decision'])
        self.assertNotEqual(low_c['selected_bundle']['account'], 'codex')

        # 2. Whole chain must fit for high coupling, only current step for medium coupling
        tight_usage = self.usage(codex=[('usage', 8, 100)])
        task = {'work_kind': 'general_implementation', 'difficulty': 'coupled', 'expected_hours': 1, 'chain': self.chain(total=20)}
        self.assertTrue(self.plan(task, tight_usage)['decision']['needs_user_decision'])
        self.assertEqual(self.rows(self.plan(dict(task, chain=self.chain(coupling='medium', total=20)), tight_usage))['sol_xhigh']['excluded'], [])

        # 3. Ledger owner lookup and short-window delay on coupled owner
        from_ledger = self.plan({'work_kind': 'general_implementation', 'difficulty': 'coupled', 'expected_hours': 1,
                                 'chain': {'id': 'refactor-1', 'coupling': 'high'}},
                                records=[{'chain': 'refactor-1', 'bundle': {'bundle_id': 'opus55_xhigh'}}])
        self.assertEqual((from_ledger['choice'], from_ledger['decision']['action']), ('opus55_xhigh', 'resume'))
        delayed_usage = self.usage(claude=[('five_hour', 3, 2), ('seven_day', 60, 100), ('weekly_fable', 100, 100)])
        delayed_task = {'work_kind': 'general_implementation', 'difficulty': 'coupled', 'expected_hours': 1,
                        'chain': self.chain(bundle='opus55_high')}
        delayed = self.plan(delayed_task, delayed_usage)
        self.assertTrue(delayed['decision']['needs_user_decision'])
        self.assertIn('can resume in 2.0h', delayed['decision']['reason'])
        self.assertFalse(self.plan(dict(delayed_task, can_wait_hours=3), delayed_usage)['decision']['needs_user_decision'])

        # 4. Carried context (>=2M historical tokens) rotates unconditionally and unlocks re-allocation; compactions alone (<2M) still resume
        base_task = {'work_kind': 'general_implementation', 'difficulty': 'coupled', 'expected_hours': 1}
        for chain_kw, expected_action in (
            ({'tokens': 900000, 'compacted': 970000, 'compactions': 1}, 'resume'),
            ({'tokens': 120000, 'compacted': 360000, 'compactions': 2}, 'resume'),
            ({'tokens': 120000, 'compacted': 900000, 'compactions': 4}, 'resume'),
            ({'tokens': 60000, 'compacted': 8 * 244000, 'handoff_ready': True}, 'rotate'),
            ({'tokens': 60000, 'compacted': 3000000}, 'rotate'),
            ({'tokens': 900000, 'handoff_ready': True}, 'resume'),
        ):
            with self.subTest(chain_kw=chain_kw):
                res = self.plan(dict(base_task, chain=self.chain(**chain_kw)))
                self.assertEqual(res['decision']['action'], expected_action)
                if expected_action == 'rotate':
                    self.assertNotEqual(res['choice'], 'sol_xhigh')

        # 5. Unworn low-coupling worker (e.g. 195K/500K tokens, 0 compactions) resumes itself rather than rotating to a sibling harness
        low_unworn = self.plan({
            'work_kind': 'computer_use', 'difficulty': 'hard', 'expected_hours': 8,
            'needs': {'local_only': True},
            'chain': self.chain(bundle='pi_sol_high', coupling='low', total=32, tokens=195439, compacted=0, compactions=0, handoff_ready=True),
        })
        self.assertEqual((low_unworn['choice'], low_unworn['decision']['action']), ('pi_sol_high', 'resume'))
        low_rows = self.rows(low_unworn)
        self.assertEqual((low_rows['sol_high']['action'], low_rows['sol_high']['components']['continuity']), ('switch', 0.0))

    def test_session_log_compaction_telemetry(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            def agent(agent_id, provider, session):
                record = home / '.paseo' / 'agents' / 'E-PROJECTS-x' / f'{agent_id}.json'
                record.parent.mkdir(parents=True, exist_ok=True)
                record.write_text(json.dumps({'persistence': {'provider': provider, 'sessionId': session}}), encoding='utf-8')
            def log(relative, lines):
                path = home / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(''.join(json.dumps(l, separators=(',', ':')) + '\n' for l in lines), encoding='utf-8')
            reading = lambda n: {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'last_token_usage': {'input_tokens': n}}}}
            agent('a1', 'codex', 's-codex')
            log('.codex/sessions/2026/10/12/rollout-2026-10-12T08-00-00-s-codex.jsonl', [
                reading(240000), {'type': 'compacted', 'payload': {}}, reading(250000), {'type': 'compacted', 'payload': {}}, reading(120000)])
            agent('a2', 'claude', 's-claude')
            log('.claude/projects/E--PROJECTS-x/s-claude.jsonl', [
                {'type': 'system', 'subtype': 'compact_boundary', 'compactMetadata': {'preTokens': 970000}},
                {'type': 'assistant', 'message': {'usage': {'input_tokens': 10, 'cache_read_input_tokens': 500000, 'cache_creation_input_tokens': 90}}}])
            agent('a3', 'pi', 's-pi')
            log('.pi/agent/sessions/x/2026-10-12_s-pi.jsonl', [{'type': 'compaction', 'tokensBefore': 260000}] * 3 + [
                {'type': 'message', 'message': {'role': 'assistant', 'usage': {'input': 1000, 'cacheRead': 40000, 'cacheWrite': 0}}}])
            agent('a4', 'claude-2', 's-c2')
            log('.claude-2/projects/E--PROJECTS-x/s-c2.jsonl', [
                {'type': 'system', 'subtype': 'compact_boundary', 'compactMetadata': {'preTokens': 510000}},
                {'type': 'assistant', 'message': {'usage': {'input_tokens': 50, 'cache_read_input_tokens': 200000, 'cache_creation_input_tokens': 50}}}])
            self.assertEqual(allocator.session_state('a1', home), {'compactions': 2, 'compacted_tokens': 490000, 'context_tokens': 120000, 'historical_context_tokens': 610000})
            self.assertEqual(allocator.session_state('a2', home), {'compactions': 1, 'compacted_tokens': 970000, 'context_tokens': 500100, 'historical_context_tokens': 1470100})
            self.assertEqual(allocator.session_state('a3', home), {'compactions': 3, 'compacted_tokens': 780000, 'context_tokens': 41000, 'historical_context_tokens': 821000})
            self.assertEqual(allocator.session_state('a4', home), {'compactions': 1, 'compacted_tokens': 510000, 'context_tokens': 200100, 'historical_context_tokens': 710100})
            agent('a1-arch', 'codex', 's-codex-arch')
            log('.codex/archived_sessions/rollout-2026-10-20T21-21-50-s-codex-arch.jsonl', [reading(249566)])
            self.assertEqual(allocator.session_state('a1-arch', home), {'compactions': 0, 'compacted_tokens': 0, 'context_tokens': 249566, 'historical_context_tokens': 249566})
            self.assertEqual(allocator.session_state('missing', home)['telemetry_status'], 'unknown')

            # Historical 2M context cannot be masked by a small post-compaction compressed context_tokens
            agent('a5', 'claude', 's-claude-2m')
            log('.claude/projects/E--PROJECTS-x/s-claude-2m.jsonl', [
                {'type': 'system', 'subtype': 'compact_boundary', 'compactMetadata': {'preTokens': 1950000, 'postTokens': 12000, 'cumulativeDroppedTokens': 1938000}},
                {'type': 'assistant', 'message': {'usage': {'input_tokens': 100, 'cache_read_input_tokens': 80000, 'cache_creation_input_tokens': 0}}}])
            st5 = allocator.session_state('a5', home)
            self.assertEqual((st5['compactions'], st5['context_tokens'], st5['historical_context_tokens']), (1, 80100, 2030100))
            orig_log = allocator.session_log
            with patch.object(allocator, 'session_log', side_effect=lambda aid, h=None: orig_log(aid, home)):
                rot = self.plan({'work_kind': 'general_implementation', 'difficulty': 'coupled',
                                 'chain': {'id': 'c5', 'coupling': 'high', 'worker': {'agent_id': 'a5', 'bundle': 'sol_xhigh', 'context_tokens': 80100}}})
                self.assertEqual(rot['decision']['action'], 'rotate')

    def test_capability_vectors_tiers_and_effort_curves(self):
        accounts = allocator.load_accounts()
        models = allocator.models_table(accounts)
        order = ['claude-opus-5-5', 'gpt-6-astra', 'gemini-4-argon-high', 'claude-sonnet-5-5', 'gpt-6.1-sol', 'grok-4.7', 'gemini-3.8-flash-high']
        tiers = [models[m]['tier'] for m in order]
        self.assertEqual(tiers, sorted(tiers, reverse=True))
        self.assertEqual(len(set(tiers)), len(tiers))
        floor = accounts['scoring']['ability_floor']['hard']
        self.assertLess(models['gemini-3.8-flash-high']['tier'], floor)
        self.assertLessEqual(floor, models['grok-4.7']['tier'])

        for name, info in models.items():
            self.assertEqual(set(info['capabilities']), set(allocator.CAPABILITY_DIMENSIONS), name)
            for dim, value in info['capabilities'].items():
                self.assertTrue(0.0 <= value <= 1.0, (name, dim))
            legacy = info.get('visual')
            self.assertEqual(info['capabilities']['visual'], legacy if legacy is not False else 0.0, name)
        reasoning = [models[m]['capabilities']['reasoning'] for m in order]
        self.assertEqual(reasoning, sorted(reasoning, reverse=True))
        gui = {m: models[m]['capabilities']['gui'] for m in models}
        self.assertGreater(gui['gpt-6-astra'], gui['gpt-6.1-sol'])
        self.assertGreater(gui['gpt-6.1-sol'], gui['gemini-4-argon-high'])
        self.assertGreater(gui['gemini-4-argon-high'], gui['claude-opus-5-5'])
        self.assertEqual(gui['claude-opus-5-5'], gui['claude-sonnet-5-5'])
        self.assertGreater(gui['claude-sonnet-5-5'], gui['gemini-3.8-flash-high'])
        for blind in ('grok-4.7', 'dot'):
            self.assertEqual(gui[blind], 0.0)

        # Effort curve lookup & model-specific response
        policy = accounts['scoring']
        curves = {'ability': {'max': 0.9}, 'burn': {'max': 4.0}}
        self.assertEqual(allocator.effort_value(policy, curves, 'ability', 'max', 0), 0.9)
        self.assertEqual(allocator.effort_value(policy, curves, 'burn', 'max', 1.0), 4.0)
        self.assertEqual(allocator.effort_value(policy, curves, 'ability', 'low', 0), policy['effort_ability']['low'])
        self.assertEqual(allocator.effort_value(policy, None, 'burn', 'xhigh', 1.0), policy['effort_burn']['xhigh'])
        self.assertEqual(allocator.effort_value(policy, {}, 'burn', 'unheard-of', 1.0), 1.0)

        custom = allocator.load_accounts()
        custom['accounts']['claude']['bundles']['sonnet55_max'] = {'model': 'claude-sonnet-5-5', 'effort': 'max'}
        custom['accounts']['claude']['bundles']['sonnet55_xhigh'] = {'model': 'claude-sonnet-5-5', 'effort': 'xhigh'}
        rows = self.rows(self.plan({'work_kind': 'general_implementation', 'difficulty': 'hard', 'expected_hours': 10}, accounts=custom))
        ability = {k: rows[k]['components']['ability_value'] for k in rows}
        self.assertAlmostEqual(ability['sonnet55_xhigh'] - ability['sonnet55_high'], 0.5, places=2)
        self.assertAlmostEqual(ability['sonnet55_max'] - ability['sonnet55_high'], 0.9, places=2)
        self.assertAlmostEqual(ability['sol_max'] - ability['sol_high'], 0.2, places=2)
        self.assertAlmostEqual(ability['astra_xhigh'] - ability['astra_high'], 0.1, places=2)
        self.assertAlmostEqual(ability['opus55_xhigh'] - ability['opus55_high'], 0.2, places=2)
        self.assertAlmostEqual(rows['sonnet55_max']['expected_pct'] / rows['sonnet55_high']['expected_pct'], 4.34 / 0.7, places=1)
        self.assertAlmostEqual(rows['sol_max']['expected_pct'] / rows['sol_high']['expected_pct'], 1.58 / 0.7, places=1)

        # Frontier floor & steeper effort curve
        frontier = {'work_kind': 'general_implementation', 'difficulty': 'frontier'}
        eligible = {c['id'] for c in self.plan(frontier)['ranking'] if c['score'] is not None}
        self.assertEqual(eligible, {'opus55_xhigh', 'opus55_high', 'astra_high', 'astra_xhigh', 'pi_astra_high', 'pi_astra_xhigh'})
        lifted = {c['id'] for c in self.plan(frontier, accounts=custom)['ranking'] if c['score'] is not None}
        self.assertIn('sonnet55_xhigh', lifted)
        self.assertNotIn('sonnet55_high', lifted)

    def test_effort_cap_and_deep_unlocks(self):
        for usage in (self.usage(), self.usage(**self.PLENTY)):
            for kind in ('debug_review', 'tool_execution', 'research_packet', 'exploration_analysis', 'general_implementation'):
                for difficulty in ('hard', 'routine'):
                    rows = self.rows(self.plan({'work_kind': kind, 'difficulty': difficulty}, usage))
                    for prefix in ('', 'pi_'):
                        high, xhigh, top = rows[prefix + 'sol_high'], rows[prefix + 'sol_xhigh'], rows[prefix + 'sol_max']
                        self.assertGreater(high['score'], xhigh['score'])
                        self.assertGreater(high['score'], top['score'])
                        self.assertEqual(high['components']['ability'], xhigh['components']['ability'])
                        self.assertTrue(xhigh['components'].get('effort_capped'))
                        self.assertNotIn('effort_capped', high['components'])
        plenty = self.usage(**self.PLENTY)
        hard_res = self.plan({'work_kind': 'debug_review', 'difficulty': 'hard'}, plenty)
        self.assertEqual((hard_res['choice'], hard_res['decision']['task']['max_recommended_effort']), ('sol_high', 'high'))
        hard_rows = self.rows(self.plan({'work_kind': 'general_implementation', 'difficulty': 'hard', 'expected_hours': 1}, plenty))
        self.assertGreater(hard_rows['opus55_high']['score'], hard_rows['opus55_xhigh']['score'])
        self.assertGreater(hard_rows['astra_high']['score'], hard_rows['astra_xhigh']['score'])

        coupled = self.plan({'work_kind': 'general_implementation', 'difficulty': 'coupled', 'expected_hours': 1,
                             'chain': {'id': 'c', 'coupling': 'low'}}, plenty)
        c_rows = self.rows(coupled)
        self.assertGreater(c_rows['sol_xhigh']['score'], c_rows['sol_high']['score'])
        self.assertGreater(c_rows['sol_xhigh']['score'], c_rows['sol_max']['score'])
        self.assertIsNone(coupled['decision']['task']['max_recommended_effort'])
        for unlocked_task in (
            {'work_kind': 'debug_review', 'difficulty': 'hard', 'needs': {'critical': True, 'local_only': True}},
            {'work_kind': 'debug_review', 'difficulty': 'hard', 'profile': self.profile(reasoning_depth=3)},
            {'work_kind': 'debug_review', 'difficulty': 'coupled', 'profile': self.profile()},
        ):
            self.assertNotIn('effort_capped', self.rows(self.plan(unlocked_task, plenty))['sol_xhigh']['components'])

    def test_route_fit_orders_and_specialists(self):
        capabilities = allocator.load_capabilities()
        accounts = allocator.load_accounts()
        for kind, models in (
            ('web_research', ['dot', 'claude-sonnet-5-5', 'grok-4.7', 'gemini-4-argon-high', 'gpt-6.1-sol', 'claude-opus-5-5', 'gpt-6-astra', 'gemini-3.8-flash-high']),
            ('text_in_text_out', ['claude-opus-5-5', 'claude-sonnet-5-5', 'dot', 'gpt-6-astra', 'gemini-4-argon-high', 'gpt-6.1-sol', 'grok-4.7', 'gemini-3.8-flash-high']),
            ('debug_review', ['gpt-6.1-sol', 'claude-sonnet-5-5', 'gpt-6-astra', 'claude-opus-5-5', 'gemini-4-argon-high', 'grok-4.7', 'gemini-3.8-flash-high']),
            ('self_contained_code', ['dot', 'claude-opus-5-5', 'claude-sonnet-5-5', 'gpt-6-astra', 'gemini-4-argon-high', 'gpt-6.1-sol', 'grok-4.7', 'gemini-3.8-flash-high']),
            ('general_implementation', ['gemini-3.8-flash-high', 'claude-opus-5-5', 'claude-sonnet-5-5', 'gpt-6-astra', 'gemini-4-argon-high', 'gpt-6.1-sol', 'grok-4.7']),
            ('tool_execution', ['gemini-3.8-flash-high', 'gpt-6.1-sol', 'claude-sonnet-5-5', 'gemini-4-argon-high', 'claude-opus-5-5', 'gpt-6-astra', 'grok-4.7']),
            ('frontend_implementation', ['gemini-3.8-flash-high', 'claude-opus-5-5', 'gemini-4-argon-high', 'gpt-6-astra', 'claude-sonnet-5-5', 'gpt-6.1-sol', 'grok-4.7']),
        ):
            fits = [allocator.fit_score(kind, m, capabilities, accounts) for m in models]
            self.assertEqual(fits, sorted(fits, reverse=True), kind)
            self.assertEqual(len(set(fits)), len(fits), kind)

        for kind in ('complex_build_plan', 'direction_reasoning'):
            self.assertEqual(self.plan({'work_kind': kind, 'difficulty': 'frontier'})['choice'], 'pro_max', kind)
        for kind in ('general_implementation', 'tool_execution', 'bounded_bulk_work', 'frontend_implementation'):
            self.assertEqual(self.plan({'work_kind': kind, 'difficulty': 'routine'})['choice'], 'flash_high', kind)
            harder = self.plan({'work_kind': kind, 'difficulty': 'hard'})
            self.assertTrue(any('hard floor' in r for r in self.rows(harder)['flash_high']['excluded']), kind)
        for kind in ('debug_review', 'frontend_implementation', 'general_implementation'):
            res = self.plan({'work_kind': kind})
            self.assertEqual(res['decision']['task']['difficulty'], 'hard', kind)
            self.assertNotEqual(res['choice'], 'flash_high', kind)
        self.assertEqual(self.plan({'work_kind': 'exploration_analysis'})['selected_bundle']['model'], 'claude-sonnet-5-5')
        self.assertEqual(self.plan({'work_kind': 'bounded_bulk_work', 'difficulty': 'hard'})['selected_bundle']['model'], 'claude-sonnet-5-5')
        self.assertIn(self.plan({'work_kind': 'exploration_analysis', 'difficulty': 'frontier'})['selected_bundle']['model'],
                      ('claude-opus-5-5', 'gpt-6-astra'))

    def test_harness_selection_and_invariance(self):
        bundles = allocator.bundles(allocator.load_accounts())
        for bundle_id in ('pi_astra_high', 'pi_astra_xhigh', 'pi_sol_high', 'pi_sol_xhigh', 'pi_sol_max'):
            b = bundles[bundle_id]
            self.assertEqual((b['provider'], b['account'], b['launch_model']), ('pi', 'codex', 'openai-codex/' + b['model']))

        # antigravity is kept scarce so the harness order, not Argon's price, decides among the GPT siblings
        claude_low = self.no_flash(claude=[('five_hour', 90, 3), ('seven_day', 5, 100), ('weekly_fable', 100, 100)])
        explore = self.plan({'work_kind': 'exploration_analysis'}, claude_low)
        self.assertEqual(explore['selected_bundle']['provider'], 'pi')
        scores = {c['id']: c['score'] for c in explore['ranking']}
        self.assertAlmostEqual(scores[explore['choice']] - scores[explore['choice'][3:]], 0.03, places=3)
        build = self.plan({'work_kind': 'general_implementation', 'difficulty': 'hard'}, claude_low)
        gpt = [c for c in build['ranking'] if c['score'] is not None and c['bundle']['account'] == 'codex']
        self.assertEqual(gpt[0]['bundle']['provider'], 'codex')
        self.assertEqual(self.plan({'work_kind': 'research_packet'}, claude_low)['selected_bundle']['provider'], 'pi')

        for kind in ('exploration_analysis', 'general_implementation', 'debug_review'):
            with_pi = self.plan({'work_kind': kind})
            without = self.plan({'work_kind': kind}, unavailable=[b for b in bundles if b.startswith('pi_')])
            self.assertEqual((with_pi['selected_bundle']['model'], with_pi['selected_bundle']['effort']),
                             (without['selected_bundle']['model'], without['selected_bundle']['effort']), kind)
            self.assertEqual(with_pi['decision']['margin'], without['decision']['margin'], kind)

        resumed_rows = self.rows(self.plan({'work_kind': 'exploration_analysis', 'chain': self.chain(bundle='sol_xhigh', coupling='medium')}))
        self.assertNotIn('harness', resumed_rows['sol_xhigh']['components'])
        self.assertEqual(resumed_rows['astra_high']['components'].get('harness'), -0.03)

    def test_harness_factor_exchange_and_order_equivalence(self):
        policy = allocator.load_accounts()['scoring']
        self.assertEqual(allocator.harness_quality_factor(
            {'harness': 'antigravity-resilient', 'model': 'claude-opus-5-5', 'provider': 'antigravity-resilient'},
            'general_implementation', policy), 0.9)
        self.assertEqual(allocator.harness_quality_factor(
            {'harness': 'antigravity', 'model': 'claude-sonnet-5-5', 'provider': 'antigravity'},
            'debug_review', policy), 0.9)
        self.assertEqual(allocator.harness_quality_factor(
            {'harness': 'antigravity-resilient', 'model': 'gemini-4-argon-high', 'provider': 'antigravity-resilient'},
            'general_implementation', policy), 1.0)
        self.assertEqual(allocator.harness_quality_factor(
            {'harness': 'claude', 'model': 'claude-opus-5-5', 'provider': 'claude'},
            'debug_review', policy), 1.0)
        self.assertEqual(allocator.harness_quality_factor(
            {'harness': 'pi', 'model': 'gpt-6.1-sol', 'provider': 'pi'},
            'exploration_analysis', policy), 1.0)

        accounts = allocator.load_accounts()
        accounts['accounts']['claude']['bundles']['agy_opus_high'] = {
            'provider': 'antigravity-resilient', 'harness': 'antigravity-resilient',
            'model': 'claude-opus-5-5', 'effort': 'high', 'modeId': 'full-access'}
        compared = self.plan({'work_kind': 'debug_review', 'difficulty': 'hard'}, accounts=accounts)
        rows = self.rows(compared)
        native, agy = rows['opus55_high'], rows['agy_opus_high']
        self.assertEqual((native['excluded'], agy['excluded']), ([], []))
        self.assertEqual((native['components']['harness_factor'], agy['components']['harness_factor']), (1.0, 0.9))
        self.assertEqual(native['components'].get('harness_order_factor'), 1.0)
        self.assertEqual(agy['components'].get('harness_order_factor'), 1.0)
        weights = compared['decision']['system']['weights']
        base = weights['fit'] * native['components']['fit'] + weights['ability'] * native['components']['ability']
        self.assertAlmostEqual(native['score'] - agy['score'], 0.1 * base, places=3)
        self.assertGreater(native['score'], agy['score'])
        exchange = compared['decision']['system']['opportunity_exchange']
        self.assertEqual((exchange['score_per_displaced_usd'], exchange['shortage_loss_per_usd']), (0.012, 0.012))
        self.assertEqual(exchange['score_per_displaced_worker_hour'], 0.06)
        self.assertEqual(exchange['usd_per_score_point'], 83.33)
        self.assertIn('tightest', exchange['cost_rule'])
        self.assertIn('0.012 score per displaced USD', compared['decision']['reason'])
        self.assertEqual(compared['decision']['system']['harness_factors']['default'], 1.0)
        self.assertEqual(exchange['warnings'], [])
        self.assertNotIn('warnings', compared['decision'])

        claude_low = self.no_flash(claude=[('five_hour', 90, 3), ('seven_day', 5, 100), ('weekly_fable', 100, 100)])
        for kind, style in (
            ('exploration_analysis', None),
            ('research_packet', None),
            ('web_research', None),
            ('text_in_text_out', None),
            ('debug_review', None),
            ('computer_use', None),
            ('general_implementation', 'engineering'),
            ('general_implementation', 'exploration'),
        ):
            task = {'work_kind': kind, 'difficulty': 'hard'}
            if style:
                task['profile'] = self.profile(interaction_style=style)
            if kind == 'computer_use':
                task = dict(task, needs={'local_only': True, 'visual': True})
            result = self.plan(task, claude_low)
            w = result['decision']['system']['weights']
            groups = {}
            for candidate in result['ranking']:
                if candidate['score'] is None:
                    continue
                groups.setdefault(allocator.harness_group(candidate), []).append(candidate)
            for siblings in groups.values():
                if len(siblings) < 2:
                    continue
                siblings.sort(key=lambda candidate: candidate['score'], reverse=True)
                best = siblings[0]
                self.assertNotIn('harness', best['components'], kind)
                self.assertEqual(best['components'].get('harness_order_factor'), 1.0, kind)
                for other in siblings[1:]:
                    if 'harness' not in other['components']:
                        self.assertEqual(other['components'].get('harness_order_factor'), 1.0, kind)
                        self.assertAlmostEqual(best['score'], other['score'], places=3)
                        continue
                    places = round((-other['components']['harness']) / 0.03)
                    self.assertGreaterEqual(places, 1, kind)
                    self.assertAlmostEqual(best['score'] - other['score'], 0.03 * places, places=3)
                    scaled = other['components']['harness_factor'] * (
                        w['fit'] * other['components']['fit'] + w['ability'] * other['components']['ability'])
                    self.assertAlmostEqual((1 - other['components']['harness_order_factor']) * scaled, 0.03 * places, places=3)

    def test_opportunity_exchange_prefers_named_rate(self):
        legacy = {'shortage_loss_per_usd': 0.02, 'shortage_loss_per_worker_hour': 0.07}
        legacy_view = allocator.opportunity_exchange(legacy)
        self.assertEqual((legacy_view['score_per_displaced_usd'], legacy_view['score_per_displaced_worker_hour']), (0.02, 0.07))
        self.assertEqual(legacy_view['warnings'], [])
        self.assertEqual(legacy_view['source']['shortage_loss_per_usd'], 'shortage_loss_per_usd')
        self.assertEqual((allocator.exchange_rate(legacy, 'shortage_loss_per_usd'),
                          allocator.exchange_rate(legacy, 'shortage_loss_per_worker_hour')), (0.02, 0.07))

        named = {'opportunity_exchange': {'score_per_displaced_usd': 0.03, 'score_per_displaced_worker_hour': 0.08}}
        named_view = allocator.opportunity_exchange(named)
        self.assertEqual((named_view['score_per_displaced_usd'], named_view['score_per_displaced_worker_hour']), (0.03, 0.08))
        self.assertEqual(named_view['warnings'], [])
        self.assertEqual(named_view['source']['shortage_loss_per_worker_hour'], 'opportunity_exchange.score_per_displaced_worker_hour')
        self.assertNotIn('shortage_loss_per_usd', named)
        self.assertEqual(allocator.exchange_rate(named, 'shortage_loss_per_usd'), 0.03)

        conflict = {'shortage_loss_per_usd': 0.02, 'shortage_loss_per_worker_hour': 0.07,
                    'opportunity_exchange': {'score_per_displaced_usd': 0.012, 'score_per_displaced_worker_hour': 0.05}}
        conflict_view = allocator.opportunity_exchange(conflict)
        self.assertEqual((conflict_view['score_per_displaced_usd'], conflict_view['score_per_displaced_worker_hour']), (0.012, 0.05))
        self.assertEqual(allocator.exchange_rate(conflict, 'shortage_loss_per_usd'), 0.012)
        self.assertEqual(conflict['shortage_loss_per_usd'], 0.02)
        self.assertTrue(any('shortage_loss_per_usd is deprecated' in note for note in conflict_view['warnings']))
        self.assertTrue(any('shortage_loss_per_worker_hour is deprecated' in note for note in conflict_view['warnings']))

        accounts = allocator.load_accounts()
        accounts['scoring']['shortage_loss_per_usd'] = 0.02
        planned = self.plan({'work_kind': 'debug_review', 'difficulty': 'hard'}, accounts=accounts)
        warnings = planned['decision']['warnings']
        self.assertTrue(any('shortage_loss_per_usd is deprecated' in note and '0.012' in note for note in warnings))
        self.assertFalse(any('shortage_loss_per_worker_hour' in note for note in warnings))
        self.assertEqual(planned['decision']['system']['shortage_loss_per_usd'], 0.012)
        self.assertEqual(planned['decision']['system']['opportunity_exchange']['score_per_displaced_usd'], 0.012)

    def test_recent_ledger_choices_are_not_shifted_by_harness_factor(self):
        path = Path.home() / '.route-manager' / 'router-ledger.jsonl'
        if not path.is_file():
            self.skipTest('no router ledger')
        plans = []
        with path.open(encoding='utf-8') as handle:
            for line in handle:
                if '"stage": "plan"' not in line and '"stage":"plan"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get('stage') == 'plan' and row.get('choice') and row.get('choice') != 'none':
                    plans.append(row)
        bundles = allocator.bundles(allocator.load_accounts())
        policy = allocator.load_accounts()['scoring']
        shifted = []
        for row in plans[-40:]:
            bundle_id = row['choice']
            bundle = dict(bundles.get(bundle_id) or row.get('bundle') or {})
            if not bundle:
                continue
            bundle.setdefault('id', bundle_id)
            if not bundle.get('harness'):
                bundle['harness'] = allocator.bundle_harness(bundle, bundle.get('account'), bundle_id)
            factor = allocator.harness_quality_factor(bundle, 'general_implementation', policy)
            model_name = str(bundle.get('model') or '')
            agy_claude = str(bundle_id).startswith('agy_') and model_name.startswith('claude')
            expected = 0.9 if agy_claude else 1.0
            if abs(factor - expected) > 1e-9:
                shifted.append({'at': row.get('at'), 'choice': bundle_id, 'model': bundle.get('model'),
                                'harness': bundle.get('harness'), 'harness_factor': factor,
                                'expected_harness_factor': expected})
        self.assertEqual(shifted, [])

    def test_dot_eligibility_and_free_usage(self):
        capabilities = allocator.load_capabilities()
        self.assertEqual(self.plan({'work_kind': 'dot_execution'})['choice'], 'dot_text')
        self.assertEqual(self.plan({'work_kind': 'dot_execution', 'needs': {'local_only': True}})['choice'], 'none')
        self.assertFalse(allocator.dot_may_take('exploration_analysis', {'local_only': False}, capabilities))
        self.assertFalse(allocator.dot_may_take('general_implementation', {'visual': True}, capabilities))
        self.assertFalse(allocator.dot_may_take('direction_reasoning', {}, capabilities))

        for kind in ('web_research', 'text_in_text_out', 'self_contained_code'):
            res = self.plan({'work_kind': kind, 'difficulty': 'routine'}, self.usage(grok=[('credits', 100, 165)]))
            self.assertEqual(res['choice'], 'dot_text', kind)
            local = self.plan({'work_kind': kind, 'difficulty': 'routine', 'needs': {'local_only': True}})
            self.assertNotEqual(local['choice'], 'dot_text', kind)
        for difficulty in ('hard', 'coupled', 'frontier'):
            self.assertEqual(self.plan({'work_kind': 'web_research', 'difficulty': difficulty})['choice'], 'dot_text', difficulty)
        coupled = self.plan({'work_kind': 'general_implementation', 'difficulty': 'coupled', 'needs': {'local_only': False}})
        models = {c['bundle']['model'] for c in coupled['ranking'] if c['score'] is not None}
        self.assertTrue({'claude-opus-5-5', 'gpt-6-astra', 'gemini-4-argon-high', 'claude-sonnet-5-5', 'gpt-6.1-sol', 'dot'} <= models)
        self.assertFalse({'grok-4.7', 'gemini-3.8-flash-high'} & models)

    def test_visual_and_gui_routing(self):
        capabilities, accounts = allocator.load_capabilities(), allocator.load_accounts()
        visual = [allocator.fit_score('visual_analysis', m, capabilities, accounts)
                  for m in ('claude-opus-5-5', 'gpt-6-astra', 'gemini-4-argon-high', 'gpt-6.1-sol', 'claude-sonnet-5-5', 'gemini-3.8-flash-high')]
        self.assertEqual([round(v, 2) for v in visual], [1.0, 0.84, 0.68, 0.52, 0.36, 0.2])
        v_task = {'work_kind': 'visual_analysis', 'difficulty': 'routine', 'needs': {'visual': True}}
        self.assertTrue(self.plan(v_task)['choice'].startswith('opus55'))
        claude_empty = self.usage(claude=[('five_hour', 90, 3), ('seven_day', 2, 100), ('weekly_fable', 100, 100)], codex=[('usage', 95, 100)])
        self.assertTrue(self.plan(v_task, claude_empty)['choice'].startswith('astra'))
        codex_tight = self.usage(claude=[('five_hour', 90, 3), ('seven_day', 2, 100), ('weekly_fable', 100, 100)],
                                 codex=[('usage', 50, 100)],
                                 antigravity=[('gemini-5h', 90, 3), ('gemini-weekly', 80, 100), ('3p-5h', 90, 3), ('3p-weekly', 60, 100)])
        codex_tight_plan = self.plan(v_task, codex_tight)
        # agy reads local images with read_file, so Opus via the Antigravity 3P pool is eligible.
        # With Claude's own weekly pool empty and Codex tight, the same top-visual-fit model (1.0 vs Argon 0.68)
        # from a healthy pool is worth more than Argon even after the 0.9 agy-Claude harness factor.
        self.assertTrue(codex_tight_plan['choice'].startswith('agy_opus55'))
        codex_tight_rows = self.rows(codex_tight_plan)
        self.assertFalse(codex_tight_rows['agy_opus55_medium']['excluded'])
        self.assertFalse(codex_tight_rows['argon_high']['excluded'])
        for blind in ('grok47_high', 'dot_text'):
            self.assertTrue(self.rows(self.plan(v_task))[blind]['excluded'], blind)

        # Computer use continuous GUI fallback and small-task profile routing
        cu_fit = {m: allocator.fit_score('computer_use', m, capabilities, accounts)
                  for m in ('gpt-6-astra', 'gpt-6.1-sol', 'gemini-4-argon-high', 'claude-opus-5-5', 'claude-sonnet-5-5', 'gemini-3.8-flash-high', 'grok-4.7')}
        self.assertEqual((cu_fit['gpt-6-astra'], cu_fit['gpt-6.1-sol'], cu_fit['grok-4.7']), (1.0, 0.8, 0.2))
        self.assertGreater(cu_fit['gpt-6.1-sol'], cu_fit['gemini-4-argon-high'])
        self.assertGreater(cu_fit['gemini-4-argon-high'], cu_fit['claude-opus-5-5'])
        for result in (self.plan({'work_kind': 'computer_use', 'needs': {'local_only': True}}, unavailable=self.codex_bundles()),
                       self.plan({'work_kind': 'computer_use', 'needs': {'local_only': True}}, accounts=self.unsubscribed('codex'))):
            self.assertIn(result['choice'], ('argon_high', 'opus55_high', 'sonnet55_high'))
            self.assertFalse(result['decision']['needs_user_decision'])
        small_gui = {'work_kind': 'computer_use', 'objective': 'Click Save and take a screenshot',
                     'profile': self.profile(reasoning_depth=1, code_complexity=0, hallucination_risk=1, error_cost=1,
                                             needs_visual=True, needs_gui=True)}
        for usage in (self.usage(), self.usage(**self.PLENTY)):
            res = self.plan(small_gui, usage)
            self.assertEqual((res['decision']['task']['difficulty'], res['decision']['task']['difficulty_source']), ('routine', 'profile'))
            self.assertIn(res['choice'], ('sol_medium', 'sol_high', 'flash_high'))
            self.assertEqual(self.rows(res)['astra_high']['components']['overqualification'], 0.08)   # small GUI keeps the capped charge
        gui_task = dict(small_gui, profile=self.profile(reasoning_depth=2, code_complexity=0, needs_visual=True, needs_gui=True))
        gui_res = self.plan(gui_task, self.usage(**self.PLENTY))
        self.assertEqual(gui_res['choice'], 'astra_high')     # deeper GUI work: the specialist, without an overqualification charge
        self.assertNotIn('overqualification', self.rows(gui_res)['astra_high']['components'])

    def test_quota_opportunity_cost_and_expiring_subscriptions(self):
        # Middle tiers dispatch when their account has room; argon shares antigravity pool at 8x flash burn
        routine = {'work_kind': 'general_implementation', 'difficulty': 'routine', 'expected_hours': 1}
        claude_room = self.no_flash(claude=[('five_hour', 90, 3), ('seven_day', 95, 100), ('weekly_fable', 100, 100)], codex=[('usage', 30, 100)])
        codex_room = self.no_flash(claude=[('five_hour', 90, 3), ('seven_day', 30, 100), ('weekly_fable', 100, 100)], codex=[('usage', 95, 100)])
        self.assertEqual(self.plan(routine, claude_room)['selected_bundle']['account'], 'claude')
        self.assertEqual(self.plan(routine, codex_room)['selected_bundle']['account'], 'codex')
        r_rows = self.rows(self.plan(routine))
        self.assertEqual((r_rows['argon_high']['expected_hours'], r_rows['flash_high']['expected_hours']), (1.35, 1.0))
        self.assertAlmostEqual(r_rows['argon_high']['expected_usd'] / r_rows['flash_high']['expected_usd'], 8.0 * 1.35, places=2)

        # Expiring subscription (Grok) deferral, zero opportunity cost once available, and pre-finish exclusion
        scarce = self.no_flash(**self.SCARCE)     # Argon, priced in USD since 2026-10-27, would otherwise take the bulk work
        now_bulk = self.plan(self.BULK, scarce)
        self.assertEqual(now_bulk['decision']['better_if_waiting']['bundle'], 'grok47_high')
        deferred = self.plan(dict(self.BULK, can_wait_hours=3), scarce)
        self.assertEqual((deferred['choice'], deferred['decision']['action']), ('grok47_high', 'defer'))
        later = self.now + timedelta(hours=3)
        avail_usage = self.no_flash(grok=[('credits', 100, 165)], **self.SCARCE)
        for rows in avail_usage['accounts'].values():
            for w in rows['windows']:
                w['seconds_until_reset'] = max(0, w['seconds_until_reset'] - 3 * 3600)
        avail = self.plan(self.BULK, avail_usage, later)
        self.assertEqual((avail['choice'], avail['ranking'][0]['components']['opportunity_cost']), ('grok47_high', 0))
        end = datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)
        ending = self.plan({'work_kind': 'debug_review', 'difficulty': 'hard', 'expected_hours': 1.5},
                           self.usage(grok=[('credits', 100, 160)]), end)
        self.assertTrue(any('ends before' in r for r in self.rows(ending)['grok47_high']['excluded']))

        # 36h pre-reset surplus bonus: boosts expiring weekly surplus, discounts overqualification, and vanishes when scarce
        impl_local = {'work_kind': 'general_implementation', 'difficulty': 'hard', 'expected_hours': 1, 'profile': self.profile()}
        baseline_rows = self.rows(self.plan(impl_local))
        self.assertNotIn('reset_bonus', baseline_rows['sonnet55_high']['components'])
        expiring_claude = self.usage(claude=[('five_hour', 90, 3), ('seven_day', 55, 18), ('weekly_fable', 100, 100)])
        exp_res = self.plan(impl_local, expiring_claude)
        exp_rows = self.rows(exp_res)
        self.assertAlmostEqual(exp_rows['sonnet55_high']['components']['reset_bonus'], 0.1, places=2)
        self.assertLess(exp_rows['opus55_high']['components']['overqualification'], baseline_rows['opus55_high']['components']['overqualification'])
        self.assertEqual(exp_res['selected_bundle']['account'], 'claude')
        scarce_near_reset = self.usage(claude=[('five_hour', 90, 3), ('seven_day', 12, 18), ('weekly_fable', 100, 100)])
        self.assertNotIn('reset_bonus', self.rows(self.plan(impl_local, scarce_near_reset))['sonnet55_high']['components'])

    def test_short_window_deferral_and_wait_limits(self):
        usage = self.usage(claude=[('five_hour', 3, 1.0), ('seven_day', 60, 100), ('weekly_fable', 100, 100)], codex=[('usage', 1, 121)])
        task = {'work_kind': 'visual_analysis', 'difficulty': 'routine', 'expected_hours': 1, 'needs': {'visual': True}}
        # Opus via agy (3P pool, readable images) starts now instead of waiting for Claude's
        # five-hour reset: same model, healthy pool. Exclude it to keep testing the deferral mechanics.
        self.assertEqual(self.plan(dict(task, can_wait_hours=2), usage, unavailable=['argon_high'])['choice'][:10], 'agy_opus55')
        no_argon = ['argon_high'] + [b for b in allocator.bundles(allocator.load_accounts()) if b.startswith(('agy_opus', 'agy_sonnet'))]
        deferred = self.plan(dict(task, can_wait_hours=2), usage, unavailable=no_argon)
        self.assertEqual((deferred['choice'], deferred['decision']['action']), ('opus55_high', 'defer'))
        self.assertEqual(self.plan(task, usage, unavailable=no_argon)['decision']['better_if_waiting']['bundle'], 'opus55_high')

        long_wait = self.usage(claude=[('five_hour', 3, 4), ('seven_day', 60, 100), ('weekly_fable', 100, 100)], codex=[('usage', 1, 121)])
        impatient = self.plan(dict(task, can_wait_hours=1), long_wait, unavailable=['flash_high'] + no_argon)
        self.assertTrue(impatient['decision']['needs_user_decision'])
        patient = self.plan(dict(task, can_wait_hours=5), long_wait, unavailable=['flash_high'] + no_argon)
        self.assertEqual((patient['decision']['needs_user_decision'], patient['decision']['action']), (False, 'defer'))

    def test_claude_2_backup_safety_floor_and_inflight_reservation(self):
        # Ordinary times (100h to reset): claude-2 dispatches normally when its weekly pool is healthier than claude's
        normal_claude2 = self.usage(
            claude=[('five_hour', 90, 3), ('seven_day', 45, 100), ('weekly_fable', 100, 100)],
            **{'claude-2': [('five_hour', 90, 3), ('seven_day', 85, 100), ('weekly_fable', 100, 100)]},
        )
        self.assertEqual(self.plan({'work_kind': 'exploration_analysis'}, normal_claude2)['choice'], 'sonnet55_high_2')

        all_big_empty = self.usage(
            claude=[('five_hour', 90, 3), ('seven_day', 0, 100), ('weekly_fable', 100, 100)],
            codex=[('usage', 1, 100)],
            antigravity=[('gemini-5h', 90, 3), ('gemini-weekly', 2, 100), ('3p-5h', 90, 3), ('3p-weekly', 2, 100)],
            **{'claude-2': [('five_hour', 65, 3), ('seven_day', 83, 100), ('weekly_fable', 100, 100)]},
        )
        self.assertEqual(self.plan({'work_kind': 'exploration_analysis'}, all_big_empty)['choice'], 'sonnet55_high_2')

        task = {'work_kind': 'exploration_analysis', 'difficulty': 'hard', 'expected_hours': 0.7, 'needs': {'local_only': True}}
        usage_low = self.usage(**{'claude-2': [('five_hour', 23, 2.5), ('seven_day', 61, 130)], 'claude': [('five_hour', 0, 2.0), ('seven_day', 0, 100)]})
        self.assertTrue(self.rows(self.plan(task, usage_low))['sonnet55_high_2']['excluded'])

        usage_40 = self.usage(**{'claude-2': [('five_hour', 40, 2.5), ('seven_day', 61, 130)], 'claude': [('five_hour', 0, 2.0), ('seven_day', 0, 100)]})
        usage_40['accounts']['claude-2']['fetched_at'] = (self.now + timedelta(seconds=12)).isoformat()
        inflight = [{
            'at': (self.now + timedelta(seconds=10)).isoformat(), 'stage': 'dispatch', 'dispatch_id': 'fixture-dispatch',
            'state': 'started', 'lease_until': (self.now + timedelta(minutes=10)).isoformat(),
            'choice': 'sonnet55_high_2', 'hours': 0.7, 'expected_usd': 3.08, 'kind': 'exploration_analysis',
            'bundle': {'provider': 'claude-2', 'account': 'claude-2', 'model': 'claude-sonnet-5-5', 'bundle_id': 'sonnet55_high_2', 'expected_usd': 3.08},
            'usage': {'claude-2': {'windows': [{'id': 'five_hour', 'used_pct': 60.0}, {'id': 'seven_day', 'used_pct': 39.0}]}},
        }]
        s2 = self.rows(allocator.plan(task, usage_40, self.now + timedelta(minutes=2), records=inflight))['sonnet55_high_2']
        self.assertTrue(s2['excluded'])
        self.assertEqual(s2['capacity']['reserved_usd'], 3.08)

        # Unstarted 5h window (used_pct=0, resets_at=None) still enforces 5h wall against oversized or burst tasks
        unstarted_5h = copy.deepcopy(normal_claude2)
        unstarted_5h['accounts']['claude-2']['status'] = 'fresh'
        unstarted_5h['accounts']['claude-2']['windows'][0] = {
            'id': 'five_hour', 'used_pct': 0.0, 'resets_at': None, 'seconds_until_reset': None,
            'observed_remaining_pct': 100.0, 'remaining_pct_before_reset': None}
        self.assertEqual(self.plan(task, unstarted_5h)['choice'], 'sonnet55_high_2')
        burst = [dict(inflight[0], dispatch_id=f'd-{i}', expected_usd=6.6) for i in (1, 2)]
        self.assertTrue(self.rows(allocator.plan(
            {'work_kind': 'exploration_analysis', 'difficulty': 'hard', 'expected_hours': 1.5, 'needs': {'local_only': True}},
            unstarted_5h, self.now + timedelta(minutes=2), records=burst))['sonnet55_high_2']['excluded'])

        # Stale claude-2 with active 5h reset and unknown/clamped used_pct or oversized 3h task is excluded
        stale_c2 = self.usage(
            claude=[('five_hour', 75, 4.0), ('seven_day', 20, 35.0), ('weekly_fable', 100, 35.0)],
        )
        stale_c2['accounts']['claude-2'] = {
            'status': 'stale',
            'stale': True,
            'fetched_at': (self.now - timedelta(minutes=13)).isoformat(),
            'windows': [
                {'id': 'five_hour', 'used_pct': None, 'resets_at': (self.now + timedelta(hours=4)).isoformat(),
                 'seconds_until_reset': 4 * 3600, 'reset_time_passed': False,
                 'observed_remaining_pct': None, 'remaining_pct_before_reset': None},
                {'id': 'seven_day', 'used_pct': 74.0, 'resets_at': (self.now + timedelta(hours=5)).isoformat(),
                 'seconds_until_reset': 5 * 3600, 'reset_time_passed': False,
                 'observed_remaining_pct': 26.0, 'remaining_pct_before_reset': None},
            ],
        }
        res_1h = self.plan({'work_kind': 'exploration_analysis', 'difficulty': 'hard', 'expected_hours': 1.0,
                            'needs': {'local_only': True}}, stale_c2)
        self.assertTrue(self.rows(res_1h)['sonnet55_high_2']['excluded'])
        self.assertEqual(res_1h['selected_bundle']['account'], 'claude')

        # Even if five_hour telemetry is completely missing, an oversized task exceeding full 5h Pro capacity is excluded
        missing_5h = copy.deepcopy(stale_c2)
        missing_5h['accounts']['claude-2']['windows'] = [missing_5h['accounts']['claude-2']['windows'][1]]
        custom_acc = allocator.load_accounts()
        custom_acc['accounts']['claude-2']['bundles']['opus55_high_2'].pop('disabled', None)
        res_opus_3h = self.plan({'work_kind': 'frontend_implementation', 'difficulty': 'hard', 'expected_hours': 3.0,
                                 'needs': {'local_only': True}}, missing_5h, accounts=custom_acc)
        self.assertTrue(self.rows(res_opus_3h)['opus55_high_2']['excluded'])
        res_sonnet_4h = self.plan({'work_kind': 'exploration_analysis', 'difficulty': 'hard', 'expected_hours': 3.8,
                                   'needs': {'local_only': True}}, missing_5h)
        self.assertTrue(self.rows(res_sonnet_4h)['sonnet55_high_2']['excluded'])

    def test_constraints_and_validation(self):
        self.assertTrue(self.plan({'work_kind': 'general_implementation', 'difficulty': 'hard',
                                   'constraints': {'model': 'claude-opus-5'}})['choice'].startswith('opus55'))
        self.assertIn('account not verified as launchable',
                      self.rows(self.plan({'work_kind': 'general_implementation', 'difficulty': 'coupled'}))['sol_xhigh_2']['excluded'])
        with self.assertRaises(ValueError):
            self.plan({'difficulty': 'impossible'})
        with self.assertRaises(ValueError):
            self.plan({'chain': {'coupling': 'tight'}})
        accounts = allocator.load_accounts()
        models = allocator.models_table(accounts)
        for bundle in allocator.bundles(accounts).values():
            self.assertIn(allocator.canonical(bundle['model'], accounts), models, bundle['id'])

    def test_task_profile_normalization_and_vector_fit(self):
        policy = allocator.load_accounts()['scoring']
        needs = allocator.profile_requirements(self.profile(needs_visual=True), policy)
        strong = {d: 1.0 for d in allocator.CAPABILITY_DIMENSIONS}
        self.assertEqual(allocator.vector_fit(needs, strong), 1.0)
        self.assertEqual(allocator.vector_fit(needs, {d: need for d, (need, _) in needs.items()}), 1.0)
        self.assertLess(allocator.vector_fit(needs, dict(strong, visual=0.0)), 1.0)

        p = self.profile
        for prof_kw, expected_kind in (
            ({'needs_gui': True, 'needs_visual': True, 'reasoning_depth': 1}, 'computer_use'),
            ({'interaction_style': 'exploration', 'code_complexity': 0}, 'exploration_analysis'),
            ({'interaction_style': 'text_only', 'code_complexity': 0}, 'text_in_text_out'),
            ({'interaction_style': 'advisory', 'code_complexity': 0}, 'direction_reasoning'),
            ({'interaction_style': 'engineering', 'code_complexity': 0}, 'tool_execution'),
        ):
            self.assertEqual(self.plan({'profile': p(**prof_kw)})['decision']['task']['kind'], expected_kind)
        for depth, difficulty in ((1, 'routine'), (2, 'hard'), (3, 'coupled'), (4, 'frontier')):
            self.assertEqual(self.plan({'work_kind': 'general_implementation', 'profile': p(reasoning_depth=depth)})['decision']['task']['difficulty'], difficulty)

    def test_minimum_sufficient_allocation_and_medium_bundles(self):
        usage = self.usage(**self.PLENTY)
        result = self.plan({'work_kind': 'general_implementation', 'profile': self.profile()}, usage)
        self.assertTrue(result['decision']['task']['minimum_sufficient'])
        self.assertNotIn(result['selected_bundle']['model'], ('gpt-6-astra', 'claude-opus-5-5'))
        rows = self.rows(result)
        self.assertEqual(rows['sol_high']['components']['ability'], 1.0)
        # the price factor is uncapped: Astra at $22.6/h pays 2.26x the $10/h reference (0.08 * 2.26)
        self.assertEqual(rows['astra_high']['components']['overqualification'], 0.1808)
        self.assertEqual(rows['opus55_high']['components']['overqualification'], 0.06)
        self.assertNotIn('overqualification', rows['sol_high']['components'])
        for profile in (self.profile(reasoning_depth=3, code_complexity=3), self.profile(error_cost=3)):
            deep = self.plan({'work_kind': 'general_implementation', 'profile': profile}, usage)
            self.assertFalse(deep['decision']['task']['minimum_sufficient'])

        plain = {c['id'] for c in self.plan({'work_kind': 'tool_execution', 'difficulty': 'routine'})['ranking']}
        mechanical = self.rows(self.plan({'work_kind': 'tool_execution', 'profile': self.profile(reasoning_depth=1, code_complexity=1)}))
        self.assertFalse({'sol_medium', 'pi_sol_medium'} & plain)
        self.assertEqual(mechanical['sol_medium']['excluded'], [])
        self.assertLess(mechanical['sol_medium']['expected_usd'], mechanical['sol_high']['expected_usd'])

    def test_heuristic_profile_and_schema_contract(self):
        capabilities = allocator.load_capabilities()
        policy = allocator.load_accounts()['scoring']
        for kind in capabilities['routes']:
            for difficulty in (None, 'routine', 'hard', 'coupled', 'frontier'):
                task = {'work_kind': kind, **({'difficulty': difficulty} if difficulty else {})}
                prof = allocator.infer_task_profile(task, capabilities)
                self.assertEqual(prof, allocator.validate_profile(prof))
                normalized = allocator.normalize_task(task, policy, capabilities, allocator.load_accounts())
                self.assertEqual(allocator.profile_difficulty(prof, policy), normalized['difficulty'], task)
        good = self.profile()
        for bad in (dict(good, reasoning_depth=5), dict(good, code_complexity=True), dict(good, error_cost='2'),
                    dict(good, needs_gui='yes'), dict(good, interaction_style='coding'), 'deep'):
            with self.assertRaises(ValueError):
                allocator.validate_profile(bad)
        schema = allocator.TASK_PROFILE_SCHEMA
        self.assertEqual(set(schema['required']), set(schema['properties']))
        for name, prop in schema['properties'].items():
            if prop['type'] == 'integer':
                self.assertNotIn('enum', prop, name)
                self.assertEqual((prop['minimum'], prop['maximum']), allocator.PROFILE_INTEGERS[name])

    def test_subjective_model_ratings_without_route_expansion(self):
        usage = self.usage(**self.PLENTY, antigravity=[('gemini-5h', 95, 3), ('gemini-weekly', 95, 100), ('3p-5h', 95, 3), ('3p-weekly', 95, 100)])
        capabilities = allocator.load_capabilities()
        self.assertEqual(len(capabilities['routes']), 16)

        # 1. Gemini 4 Argon wins scheme formulation, divergent ideation, and creative research (divergence=3)
        for kind, depth, code, style in (
            ('exploration_analysis', 2, 1, 'exploration'),
            ('exploration_analysis', 3, 2, 'exploration'),
            ('research_packet', 2, 1, 'exploration'),
            ('research_packet', 3, 2, 'exploration'),
            ('complex_build_plan', 3, 2, 'advisory'),
            ('direction_reasoning', 3, 1, 'advisory'),
            ('general_implementation', 2, 2, 'engineering'),
            ('general_implementation', 3, 2, 'engineering'),
        ):
            prof = self.profile(reasoning_depth=depth, code_complexity=code, context_scale=2,
                                divergence=3, interaction_style=style)
            res = self.plan({'work_kind': kind, 'profile': prof}, usage)
            self.assertEqual(res['choice'], 'argon_high', (kind, depth))
            self.assertGreater(self.rows(res)['argon_high']['components']['subjective_fit'],
                               self.rows(res)['opus55_high']['components']['subjective_fit'])

        # 2. Claude Opus 5.5 wins architectural landing rigor and holistic cross-module synthesis (divergence=1, scale=3/code=3)
        for kind, code, style in (
            ('general_implementation', 3, 'engineering'),
            ('frontend_implementation', 3, 'engineering'),
            ('debug_review', 3, 'engineering'),
            ('exploration_analysis', 2, 'exploration'),
        ):
            prof = self.profile(reasoning_depth=3, code_complexity=code, context_scale=3,
                                divergence=1, interaction_style=style)
            res = self.plan({'work_kind': kind, 'profile': prof}, usage)
            self.assertEqual(res['choice'], 'opus55_high', kind)
            rows = self.rows(res)
            self.assertGreater(rows['opus55_high']['components']['fit'],
                               rows['argon_high']['components']['fit'])
            self.assertGreater(rows['opus55_high']['components']['subjective_fit'],
                               rows['sol_high']['components']['subjective_fit'])

        # 3. GPT-6.1 Sol wins concrete single-module implementation (context_scale=1, divergence=1), penalized on global scope (context_scale=3)
        for kind in ('general_implementation', 'debug_review'):
            local_mod = self.profile(reasoning_depth=2, code_complexity=2, context_scale=1,
                                     divergence=1, interaction_style='engineering')
            res_local = self.plan({'work_kind': kind, 'profile': local_mod}, usage)
            self.assertEqual(res_local['choice'], 'sol_high', kind)

            global_mod = self.profile(reasoning_depth=2, code_complexity=2, context_scale=3,
                                      divergence=1, interaction_style='engineering')
            res_global = self.plan({'work_kind': kind, 'profile': global_mod}, usage)
            self.assertNotEqual(res_global['choice'], 'sol_high', kind)
            self.assertLess(self.rows(res_global)['sol_high']['components']['subjective_fit'], 0.5)

    def test_calibration_2026_10_27(self):
        # 1. Antigravity opportunity cost is valued in USD like every other account, not in Flash worker-hours
        rows = self.rows(self.plan({'work_kind': 'general_implementation', 'profile': self.profile()}))
        argon = rows['argon_high']['capacity']
        self.assertEqual((argon['window'], argon['capacity_usd']), ('gemini-weekly', 625.0))
        self.assertIn('usd_displaced', argon)
        spec, policy = {'burn_pct_per_worker_hour': {}}, allocator.load_accounts()['scoring']
        state = {'remaining': 30.0, 'end_hours': 50.0, 'reset_hours': 50.0, 'short': False, 'stale': False}
        forecast = (40.0, 10.0, 0.8, 'observed')
        hours_only = allocator.window_cost(spec, 'w', state, 3.2, forecast, policy, {'notes': []}, self.now, unit=1.6)
        cheap_prior = allocator.window_cost({'burn_pct_per_worker_hour': {'w': 0.2}}, 'w', state, 3.2, forecast, policy,
                                            {'notes': []}, self.now, unit=1.6)
        self.assertGreater(hours_only['worker_hours_displaced'], 0)
        self.assertIn('usd_displaced', cheap_prior)     # a burn prior alone derives a USD pool; it never divides by it

        # 2. Subjective weights sum to 1 and move smoothly with scope; multi-hypothesis work blends three dimensions
        weights = lambda **kw: allocator.subjective_weights(self.profile(**{'divergence': 1, **kw}))
        for kw in ({}, {'context_scale': 2}, {'code_complexity': 0}, {'code_complexity': 0, 'context_scale': 2},
                   {'divergence': 2}, {'reasoning_depth': 3, 'context_scale': 3, 'code_complexity': 3}):
            self.assertAlmostEqual(sum(weights(**kw).values()), 1.0, places=6, msg=kw)
        self.assertEqual(weights(), {'module_precision': 1.0})
        self.assertEqual(weights(context_scale=2), {'module_precision': 0.75, 'global_rigor': 0.25})
        self.assertEqual(weights(code_complexity=0), {'module_precision': 0.35, 'global_rigor': 0.65})
        self.assertEqual(weights(code_complexity=0, context_scale=2), {'module_precision': 0.25, 'global_rigor': 0.75})
        self.assertEqual(set(weights(divergence=2, code_complexity=0)), {'divergence', 'module_precision', 'global_rigor'})
        self.assertGreaterEqual(weights(reasoning_depth=3, context_scale=3)['global_rigor'], 0.8)
        self.assertLess(weights(context_scale=2).get('global_rigor', 0), 0.8)

        # 3. Meeting the routine floor is sufficient: Flash gets full ability and takes routine tool work
        routine = {'work_kind': 'tool_execution', 'profile': self.profile(reasoning_depth=1, code_complexity=0)}
        res = self.plan(routine, self.usage(**self.PLENTY))
        self.assertEqual(self.rows(res)['flash_high']['components']['ability'], 1.0)
        self.assertEqual(res['choice'], 'flash_high')

        # 4. Astra's $22.6/h is not capped at $10/h: it loses routine and hard non-GUI work under healthy quota
        for prof in (self.profile(reasoning_depth=1, code_complexity=1), self.profile(),
                     self.profile(code_complexity=0, hallucination_risk=3, context_scale=2)):
            res = self.plan({'work_kind': 'debug_review', 'profile': prof}, self.usage(**self.PLENTY))
            self.assertNotEqual(res['selected_bundle']['model'], 'gpt-6-astra', prof)

    def healthy_quota(self, depleted=(), remaining=90):
        usage = self.usage()
        for name, account in usage['accounts'].items():
            if name == 'grok':
                continue  # The legacy expiry fixture is not an active subscription in these quota scenarios.
            for window in account['windows']:
                rem = remaining if name in depleted and window['seconds_until_reset'] > 24 * 3600 else 90
                window.update(remaining_pct_before_reset=rem, observed_remaining_pct=rem)
        return usage

    def test_cross_domain_offline_routes(self):
        # Fallback follows the requested operation, including bilingual routes and competing incidental nouns.
        for objective, kind in (
            ('Click the Save button in the desktop settings', 'computer_use'),
            ('Compare screenshots with the UI mockup', 'visual_analysis'),
            ('检查截图并列出布局差异', 'visual_analysis'),
            ('Implement the desktop UI component in React', 'frontend_implementation'),
            ('实现前端组件样式', 'frontend_implementation'),
            ('Batch rename i18n keys uniformly', 'bounded_bulk_work'),
            ('批量替换翻译键', 'bounded_bulk_work'),
            ('Search the web for public documentation', 'web_research'),
            ('搜索官网完成厂商调研', 'web_research'),
            ('Translate the deployment runbook without executing it', 'text_in_text_out'),
            ('撰写文档并翻译术语', 'text_in_text_out'),
            ('Write a multi-phase system migration spec', 'complex_build_plan'),
            ('设计系统迁移方案', 'complex_build_plan'),
            ('Prove or refute this mathematical bound', 'direction_reasoning'),
            ('开展数学推导和架构批判', 'direction_reasoning'),
            ('Run a quantitative experiment and benchmark suite, report metrics', 'research_packet'),
            ('运行仿真并报告统计检验结果', 'research_packet'),
            ('Follow the CI release runbook and health check', 'tool_execution'),
            ('执行发布流程和巡检', 'tool_execution'),
            ('Diagnose the memory leak in frontend rendering', 'debug_review'),
            ('排查内存泄漏', 'debug_review'),
            ('Explore the repository architecture', 'exploration_analysis'),
            ('梳理仓库依赖', 'exploration_analysis'),
            ('Implement a standalone algorithm', 'self_contained_code'),
            ('实现独立算法并验证', 'self_contained_code'),
            ('Implement API pagination in the backend service', 'general_implementation'),
        ):
            with self.subTest(objective=objective):
                self.assertEqual(allocator.infer_kind_from_objective(objective), kind)

    def test_frontend_quality_order_and_profiled_routine_sink(self):
        usage = self.healthy_quota()
        for kind in ('general_implementation', 'frontend_implementation', 'bounded_bulk_work', 'tool_execution'):
            prof = self.profile(reasoning_depth=1, code_complexity=2, divergence=1)
            self.assertEqual(self.plan({'work_kind': kind, 'profile': prof}, usage)['choice'], 'flash_high', kind)
        for depth, code, scale in ((2, 2, 1), (3, 3, 3)):
            for div in (1, 2):
                prof = self.profile(reasoning_depth=depth, code_complexity=code, context_scale=scale,
                                    divergence=div, needs_visual=True)
                result = self.plan({'work_kind': 'frontend_implementation', 'profile': prof, 'expected_hours': 1}, usage)
                models = list(dict.fromkeys(row['bundle']['model'] for row in result['ranking'] if not row['excluded']))
                self.assertEqual(models[:3], ['claude-opus-5-5', 'gemini-4-argon-high', 'gpt-6-astra'], (depth, div))
                self.assertEqual(result['choice'], 'opus55_high')

    def test_one_hour_hard_tasks_rotate_before_quota_wall(self):
        for kind, prof, owner, depleted, remaining in (
            ('general_implementation', self.profile(), 'sol_high', ('codex',), 30),
            ('debug_review', self.profile(code_complexity=0, hallucination_risk=3), 'argon_high', ('antigravity',), 35),
            ('debug_review', self.profile(code_complexity=0, divergence=2, hallucination_risk=3), 'opus55_xhigh', ('claude', 'claude-2'), 35),
            ('debug_review', self.profile(code_complexity=2), 'sol_high', ('codex',), 20),
            ('exploration_analysis', self.profile(code_complexity=0, context_scale=2, interaction_style='exploration'),
             'sonnet55_high', ('claude', 'claude-2'), 35),
            ('web_research', self.profile(code_complexity=0, context_scale=2, hallucination_risk=3, interaction_style='exploration'),
             'sonnet55_high', ('claude',), 35),
            ('frontend_implementation', self.profile(needs_visual=True), 'opus55_high', ('claude', 'claude-2'), 35),
        ):
            with self.subTest(kind=kind):
                task = {'work_kind': kind, 'profile': prof, 'expected_hours': 1}
                healthy = self.plan(task, self.healthy_quota())
                scarce = self.plan(task, self.healthy_quota(depleted, remaining))
                self.assertEqual(healthy['choice'], owner)
                self.assertNotIn(scarce['selected_bundle']['account'], depleted)
                row = self.rows(scarce)[owner]
                self.assertFalse(row['excluded'])  # Smooth price rotation, not a new hard gate.
                self.assertGreater(row['capacity']['scarcity_cost'], 0)
                self.assertGreater(self.rows(healthy)[owner]['score'], row['score'])

    def test_weekly_demand_pace_floor_and_scarcity_boundary(self):
        policy = allocator.load_accounts()['scoring']
        spec = {'observed': {'w': {'pct_per_hour_24h': 0.0}}}
        state = {'short': False, 'hours': 168, 'reset_hours': 96, 'end_hours': 96,
                 'remaining': 25, 'target_remaining': 100 * 96 / 168}
        forecast = allocator.demand_forecast(spec, 'w', state, policy)
        self.assertAlmostEqual(forecast[2], 0.5 * 100 / 168)
        self.assertEqual(forecast[3], 'observed')
        self.assertEqual(allocator.demand_forecast(spec, 'w', dict(state, remaining=90), policy)[2], 0)
        self.assertEqual(allocator.demand_forecast(spec, 'w', dict(state, short=True), policy)[2], 0)
        costs = []
        for remaining in (95, 60, 35, 20, 8):
            row = allocator.window_cost({}, 'w', dict(state, remaining=remaining), 0.5,
                                        (100, 10, 1, 'observed'), policy, {'notes': []}, self.now, unit=0.5)
            costs.append(row['scarcity_cost'])
        self.assertEqual(costs[:2], [0, 0])
        self.assertEqual(costs, sorted(costs))
        no_forecast = allocator.demand_forecast({}, 'w', state, policy)
        self.assertEqual(no_forecast[3], 'period average')

    def test_high_precision_effort_quality_and_quota_stepdown(self):
        # Ordinary coupled work unlocks effort but buys no precision bonus; existing High remains optimal.
        ordinary = self.plan({'work_kind': 'general_implementation', 'profile': self.profile(
            reasoning_depth=3, code_complexity=3, context_scale=3, divergence=1)}, self.healthy_quota())
        self.assertEqual(ordinary['choice'], 'opus55_high')
        self.assertNotIn('quality_bonus', self.rows(ordinary)['opus55_xhigh']['components'])
        for model, kind, prof, hours, high_effort, tight_rem in (
            ('claude-opus-5-5', 'debug_review', self.profile(code_complexity=0, divergence=2,
                hallucination_risk=3, interaction_style='exploration'), 1, 'opus55_xhigh', 35),
            ('claude-opus-5-5', 'debug_review', self.profile(reasoning_depth=3, code_complexity=3,
                context_scale=3, hallucination_risk=3), 1, 'opus55_xhigh', 35),
            ('claude-opus-5-5', 'general_implementation', self.profile(reasoning_depth=4,
                code_complexity=3, context_scale=3), 1, 'opus55_xhigh', 35),
            ('gpt-6.1-sol', 'debug_review', self.profile(code_complexity=2, divergence=1, hallucination_risk=3),
                4, 'sol_xhigh', 20),
            ('gpt-6.1-sol', 'debug_review', self.profile(code_complexity=2, divergence=1, hallucination_risk=3, error_cost=3),
                4, 'sol_max', 20),
            ('gpt-6-astra', 'computer_use', self.profile(divergence=2, hallucination_risk=3,
                needs_gui=True, needs_visual=True), 1, 'astra_xhigh', 35),
        ):
            with self.subTest(model=model, high_effort=high_effort):
                task = {'work_kind': kind, 'profile': prof, 'expected_hours': hours}
                if high_effort == 'sol_max':
                    # Explicit family constraint isolates Max vs XHigh/High on a critical Sol assignment.
                    task['constraints'] = {'model': model}
                healthy = self.plan(task, self.healthy_quota())
                # Keep the same family for the scarcity comparison; it may otherwise rotate to a healthy model.
                tight = self.plan(dict(task, constraints={'model': model}),
                                  self.healthy_quota(('codex', 'claude', 'claude-2'), tight_rem))
                self.assertEqual(healthy['choice'], high_effort)
                tight_rows = self.rows(tight)
                if model == 'claude-opus-5-5' and str(tight['choice']).startswith('agy_opus55_'):
                    # Native weekly is scarce, so the healthy 3p pool wins. USD still follows the shared burn
                    # curve. Where medium and high both saturate they tie and catalog order keeps medium;
                    # frontier does not saturate, so high outscores medium.
                    medium_row, high_row = tight_rows['agy_opus55_medium'], tight_rows['agy_opus55_high']
                    self.assertAlmostEqual(medium_row['expected_usd'] / high_row['expected_usd'], 0.52 / 0.7, places=2)
                    self.assertGreater(tight_rows['opus55_high']['score'], tight_rows['opus55_xhigh']['score'])
                    if medium_row['components']['ability'] == high_row['components']['ability']:
                        self.assertEqual(medium_row['score'], high_row['score'])
                        self.assertEqual(tight['selected_bundle']['effort'], 'medium')
                    else:
                        self.assertGreater(high_row['score'], medium_row['score'])
                        self.assertEqual(tight['selected_bundle']['effort'], 'high')
                else:
                    self.assertEqual(tight['selected_bundle']['effort'], 'high')
                self.assertGreater(self.rows(healthy)[high_effort]['components']['quality_bonus'], 0)
                self.assertNotIn('effort_capped', self.rows(healthy)[high_effort]['components'])

    def test_fast_mode_plentiful_quota_pre_reset_sprint_and_scarcity_suppression(self):
        task_base = {'work_kind': 'general_implementation',
                     'profile': self.profile(reasoning_depth=2, code_complexity=2, context_scale=1, divergence=1),
                     'expected_hours': 1.0}
        normal = self.plan(task_base, self.healthy_quota())
        self.assertEqual(normal['choice'], 'sol_high')
        self.assertFalse(normal['selected_bundle'].get('fast', False))
        base_usd = self.rows(normal)['sol_high']['capacity']['expected_usd']

        fast_req = self.plan({**task_base, 'needs': {'local_only': True, 'fast': True}}, self.healthy_quota())
        self.assertEqual(fast_req['choice'], 'sol_high')
        self.assertTrue(fast_req['selected_bundle'].get('fast'))
        self.assertTrue(fast_req['decision'].get('fast'))
        self.assertEqual(fast_req['decision'].get('fast_reason'), 'fast_requested_plentiful_quota')
        self.assertAlmostEqual(self.rows(fast_req)['sol_high']['capacity']['expected_usd'], round(base_usd * 2.0, 2), places=2)

        urgent_obj = self.plan({**task_base, 'objective': '请尽快完成单模块接口修复，比较急'}, self.healthy_quota())
        self.assertTrue(urgent_obj['decision']['task'].get('wants_fast'))
        self.assertTrue(urgent_obj['selected_bundle'].get('fast'))

        sprint_usage = self.usage(codex=[('usage', 50, 12)])
        sprint = self.plan(task_base, sprint_usage)
        self.assertEqual(sprint['choice'], 'sol_high')
        self.assertTrue(sprint['selected_bundle'].get('fast'))
        self.assertEqual(sprint['decision'].get('fast_reason'), 'pre_reset_sprint')
        self.assertAlmostEqual(self.rows(sprint)['sol_high']['capacity']['expected_usd'], round(base_usd * 2.0, 2), places=2)

        scarce_midweek = self.healthy_quota(('codex',), remaining=35)
        scarce_plan = self.plan({**task_base, 'needs': {'local_only': True, 'fast': True}, 'constraints': {'model': 'gpt-6.1-sol'}},
                                scarce_midweek)
        self.assertEqual(scarce_plan['choice'], 'sol_high')
        self.assertFalse(scarce_plan['selected_bundle'].get('fast', False))

        scarce_reset = self.usage(codex=[('usage', 10, 12)])
        scarce_reset_plan = self.plan({**task_base, 'needs': {'local_only': True, 'fast': True}, 'constraints': {'model': 'gpt-6.1-sol'}},
                                      scarce_reset)
        self.assertEqual(scarce_reset_plan['choice'], 'sol_high')
        self.assertFalse(scarce_reset_plan['selected_bundle'].get('fast', False))

    def test_account_allowed_cwd_optional_directory_scope(self):
        accounts = allocator.load_accounts()
        accounts['accounts']['codex']['allowed_cwd'] = ['/workspace/allowed_root']
        task_base = {'work_kind': 'general_implementation', 'difficulty': 'hard',
                     'expected_hours': 1.0, 'needs': {'local_only': True}}
        outside = allocator.plan({**task_base, 'cwd': '/workspace/other_repo'}, self.healthy_quota(), self.now,
                                 accounts=accounts, overrides=[])
        self.assertTrue(any('allowed_cwd' in r for r in self.rows(outside)['sol_high']['excluded']))
        inside = allocator.plan({**task_base, 'cwd': '/workspace/allowed_root/subproject'}, self.healthy_quota(), self.now,
                                accounts=accounts, overrides=[])
        self.assertEqual(self.rows(inside)['sol_high']['excluded'], [])

    def test_readonly_review_research_exploration_gui_routing_and_duration_multipliers(self):
        # 1) Read-only review tasks normalize code_complexity=0 and route to rigorous review models instead of Sol
        ro_review = {
            'work_kind': 'general_implementation',
            'objective': '# Task: Independent code and release review (read-only)\nInspect the diff and write a review report; no repo code edits.',
            'needs': {'local_only': True},
            'profile': self.profile(work_kind='general_implementation', reasoning_depth=2, code_complexity=2,
                                    context_scale=1, divergence=1, hallucination_risk=3, error_cost=2,
                                    local_only=True, interaction_style='engineering'),
        }
        ro_plan = self.plan(ro_review, self.healthy_quota())
        self.assertEqual(ro_plan['decision']['task']['kind'], 'debug_review')
        self.assertEqual(ro_plan['decision']['task']['profile']['code_complexity'], 0)
        self.assertIn(ro_plan['choice'], ('opus55_high', 'argon_high', 'sonnet55_high'))
        self.assertNotIn('agy_opus55_low', self.rows(ro_plan))

        # 2) Exploration / quantitative research packet scripts with code_complexity=2 keep global_rigor=0.65
        exp_weights = allocator.convergent_weights(depth=2, code=2, scale=2, style='exploration')
        self.assertEqual(exp_weights, {'module_precision': 0.35, 'global_rigor': 0.65})

        # 3) GUI computer_use tasks with needs_gui=True route to computer_use and only exempt GUI-capable models from overqualification
        gui_mis_kind = {
            'work_kind': 'general_implementation',
            'objective': 'Operate the desktop window, click Save, and propose a verification check',
            'needs': {'local_only': True, 'visual': True},
            'profile': self.profile(work_kind='general_implementation', reasoning_depth=2, code_complexity=1,
                                    context_scale=1, divergence=3, hallucination_risk=2, error_cost=2,
                                    needs_visual=True, needs_gui=True, local_only=True, interaction_style='engineering'),
        }
        gui_plan = self.plan(gui_mis_kind, self.healthy_quota())
        self.assertEqual(gui_plan['decision']['task']['kind'], 'computer_use')
        self.assertEqual(gui_plan['choice'], 'astra_high')
        gui_rows = self.rows(gui_plan)
        self.assertNotIn('overqualification', gui_rows['astra_high']['components'])
        self.assertGreater(gui_rows['opus55_high']['components'].get('overqualification', 0), 0)

        # 4) Offline heuristic infer_task_profile includes work_kind and calibrated default_hours / duration_multiplier
        heur_prof = allocator.infer_task_profile(
            {'objective': 'Independent review (read-only) of the authentication module'},
            allocator.load_capabilities(),
        )
        self.assertEqual(heur_prof['work_kind'], 'debug_review')
        self.assertEqual(heur_prof['code_complexity'], 0)
        scoring = allocator.load_accounts()['scoring']
        self.assertEqual(scoring['default_hours'], {'routine': 0.4, 'hard': 1.0, 'coupled': 2.0, 'frontier': 3.5})
        self.assertEqual(gui_rows['argon_high']['components'].get('duration_multiplier'), 1.35)
        self.assertEqual(gui_rows['sol_high']['components'].get('duration_multiplier'), 1.35)

    def test_existing_bundles_keep_tag_pools_and_draws_override(self):
        # Duplicated historical predicate: windows.models substring or gemini/3p group, plus the fable window.
        def historical(wid, wspec, model):
            tags = wspec.get('models') if isinstance(wspec, dict) else None
            group = 'gemini' if str(model).startswith('gemini') else '3p'
            if tags and not any(tag in model for tag in tags) and group not in tags:
                return False
            if 'fable' in wid and 'fable' not in model:
                return False
            return True

        accounts = allocator.load_accounts()
        flat = allocator.bundles(accounts)
        seen = 0
        for bundle in flat.values():
            spec = accounts['accounts'][bundle['account']]
            pooled = allocator.bundle_pools(bundle, accounts)
            via_windows = [wid for wid, _, _ in allocator.applicable_windows(
                spec, bundle['model'], {}, bundle, accounts)]
            self.assertEqual(via_windows, pooled, bundle['id'])
            if 'draws' in bundle:
                continue
            model = allocator.canonical(bundle['model'], accounts)
            expected = [wid for wid, wspec in (spec.get('windows') or {}).items() if historical(wid, wspec, model)]
            self.assertEqual(pooled, expected, bundle['id'])
            self.assertEqual([wid for wid, _, _ in allocator.applicable_windows(spec, model, {})], expected, bundle['id'])
            seen += 1
        self.assertGreater(seen, 10)

        flash = flat['flash_high']
        self.assertEqual(allocator.bundle_pools({**flash, 'draws': ['3p-weekly']}, accounts), ['3p-weekly'])
        opus = flat['agy_opus55_high']
        self.assertEqual(allocator.bundle_pools({**opus, 'draws': ['3p-weekly']}, accounts), ['3p-weekly'])
        self.assertEqual(allocator.bundle_pools({**opus, 'draws': []}, accounts), [])
        for family, canonical_name in (('opus', 'claude-opus-5-5'), ('sonnet', 'claude-sonnet-5-5')):
            for effort in ('low', 'medium', 'high'):
                bundle = flat[f'agy_{family}55_{effort}']
                self.assertEqual(allocator.bundle_pools(bundle, accounts), ['3p-5h', '3p-weekly'])
                self.assertEqual(allocator.canonical(bundle['model'], accounts), canonical_name)
                self.assertEqual(bundle['model'], f'{canonical_name}-{effort}')
                self.assertEqual(bundle['launch_model'], bundle['model'])
                self.assertEqual((bundle['provider'], bundle['modeId']), ('antigravity-resilient', 'full-access'))
                self.assertNotIn('disabled', bundle)
                self.assertEqual(allocator.harness_quality_factor(bundle, 'debug_review', accounts['scoring']), 0.9)
        low = flat['agy_opus55_low']
        self.assertEqual(low.get('when'), {'max_reasoning_depth': 1})
        self.assertTrue(allocator.bundle_applies(low, {'profile': {'reasoning_depth': 1}}))
        self.assertFalse(allocator.bundle_applies(low, {'profile': {'reasoning_depth': 2}}))
        self.assertFalse(allocator.bundle_applies(low, {}))

    def test_agy_claude_3p_reservation_launch_and_short_window(self):
        import dispatch_registry
        accounts = allocator.load_accounts()
        spec = accounts['accounts']['antigravity']
        lease = (self.now + timedelta(hours=2)).isoformat()
        record = {
            'stage': 'dispatch', 'dispatch_id': 'agy-opus-1', 'state': 'started', 'lease_until': lease,
            'bundle': {'account': 'antigravity', 'bundle_id': 'agy_opus55_high', 'model': 'claude-opus-5-5-high'},
            'expected_usd': 4.0,
        }
        for wid, cap in (('3p-5h', 17.6), ('3p-weekly', 88.0)):
            self.assertEqual(allocator.inflight_reserved_usd(
                'antigravity', spec, wid, spec['windows'][wid], {}, cap, [record], accounts, {}, self.now), 4.0)
        for wid in ('gemini-5h', 'gemini-weekly'):
            self.assertEqual(allocator.inflight_reserved_usd(
                'antigravity', spec, wid, spec['windows'][wid], {}, 100.0, [record], accounts, {}, self.now), 0.0)
        # Catalog draws win even when the record's model string would tag-match the Gemini pool.
        mismatched = {'stage': 'dispatch', 'dispatch_id': 'agy-opus-2', 'state': 'started', 'lease_until': lease,
                      'expected_usd': 4.0,
                      'bundle': {'account': 'antigravity', 'bundle_id': 'agy_opus55_high', 'model': 'gemini-3.8-flash-high'}}
        self.assertEqual(allocator.inflight_reserved_usd(
            'antigravity', spec, '3p-5h', spec['windows']['3p-5h'], {}, 17.6, [mismatched], accounts, {}, self.now), 4.0)
        self.assertEqual(allocator.inflight_reserved_usd(
            'antigravity', spec, 'gemini-5h', spec['windows']['gemini-5h'], {}, 182.0, [mismatched], accounts, {}, self.now), 0.0)

        resolved = dispatch_registry.resolve_bundle(
            {'provider': 'antigravity-resilient', 'account': 'antigravity', 'model': 'claude-opus-5-5', 'effort': 'high'},
            accounts)
        self.assertEqual(resolved['id'], 'agy_opus55_high')
        self.assertEqual(resolved['launch_model'], 'claude-opus-5-5-high')
        with self.assertRaises(ValueError):
            dispatch_registry.resolve_bundle(
                {'provider': 'antigravity-resilient', 'account': 'antigravity', 'model': 'claude-opus-5-5', 'effort': 'xhigh'},
                accounts, explicit_override=True)
        request = dispatch_registry.launch_request(
            allocator.bundles(accounts)['agy_opus55_high'], {'title': 'agy claude', 'initialPrompt': 'fixture'})
        self.assertEqual(request['provider'], 'antigravity-resilient/claude-opus-5-5-high')
        self.assertEqual(request['settings'].get('modeId'), 'full-access')
        self.assertNotIn('thinkingOptionId', request['settings'])

        sparse = allocator.load_accounts()
        allocator.accounts_override.apply(sparse, {'accounts': {'antigravity': {'bundles': {
            'flash_high': {'disabled': 'sparse test'}}}}})
        kept = sparse['accounts']['antigravity']['bundles']
        self.assertEqual(kept['flash_high'].get('disabled'), 'sparse test')
        self.assertNotIn('disabled', kept['agy_opus55_high'])
        self.assertNotIn('disabled', kept['agy_sonnet55_high'])

        frontier = {
            'work_kind': 'general_implementation', 'difficulty': 'frontier', 'expected_hours': 1.0,
            'profile': self.profile(reasoning_depth=4, code_complexity=3, context_scale=3, divergence=1),
            'needs': {'local_only': True, 'local_only_reason': 'local pytest against this worktree'},
        }
        tight_others = dict(
            claude=[('five_hour', 2, 4), ('seven_day', 2, 100), ('weekly_fable', 100, 100)],
            codex=[('usage', 1, 100)])
        chosen = self.plan(frontier, self.usage(
            **tight_others,
            antigravity=[('gemini-5h', 0, 3), ('gemini-weekly', 0, 100), ('3p-5h', 95, 4), ('3p-weekly', 90, 100)]))
        self.assertTrue(str(chosen['choice']).startswith('agy_opus55_'), chosen['choice'])
        self.assertEqual(chosen['selected_bundle']['account'], 'antigravity')
        self.assertEqual(chosen['selected_bundle'].get('draws'), ['3p-5h', '3p-weekly'])
        self.assertEqual(chosen['selected_bundle']['modeId'], 'full-access')
        rows = self.rows(chosen)
        for bid in ('flash_high', 'argon_high', 'opus55_high'):
            self.assertTrue(rows[bid]['excluded'], bid)

        blocked = self.plan(frontier, self.usage(
            **tight_others,
            antigravity=[('gemini-5h', 0, 3), ('gemini-weekly', 0, 100), ('3p-5h', 0, 2), ('3p-weekly', 90, 100)]))
        blocked_rows = self.rows(blocked)
        self.assertNotIn('agy_opus55_low', blocked_rows)
        for effort in ('medium', 'high'):
            reasons = blocked_rows[f'agy_opus55_{effort}']['excluded']
            self.assertTrue(any('3p-5h' in reason for reason in reasons), reasons)
        self.assertFalse(str(blocked['choice']).startswith('agy_'))
        shallow = {
            'work_kind': 'general_implementation', 'difficulty': 'routine', 'expected_hours': 1.0,
            'profile': self.profile(reasoning_depth=1, code_complexity=1, divergence=1,
                                    hallucination_risk=1, error_cost=1),
            'needs': {'local_only': True, 'local_only_reason': 'local pytest against this worktree'},
        }
        shallow_blocked = self.plan(shallow, self.usage(
            **tight_others,
            antigravity=[('gemini-5h', 0, 3), ('gemini-weekly', 0, 100), ('3p-5h', 0, 2), ('3p-weekly', 90, 100)]))
        low_reasons = self.rows(shallow_blocked)['agy_opus55_low']['excluded']
        self.assertTrue(any('3p-5h' in reason for reason in low_reasons), low_reasons)

        long = self.plan(dict(frontier, expected_hours=4), self.usage(
            **tight_others,
            antigravity=[('gemini-5h', 0, 3), ('gemini-weekly', 0, 100), ('3p-5h', 100, 4), ('3p-weekly', 90, 100)]))
        long_rows = self.rows(long)
        for effort in ('medium', 'high'):
            reasons = long_rows[f'agy_opus55_{effort}']['excluded']
            self.assertTrue(any('3p-5h' in reason for reason in reasons), reasons)


class OverrideContractTests(unittest.TestCase):
    """Contract tests for temporary priority overrides, reset confirmation, receipts, and atomic store."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'overrides.json'
        self.now = datetime(2026, 10, 16, 9, tzinfo=timezone.utc)
        self.baseline = self.now - timedelta(hours=1)
        self.reset = self.now + timedelta(days=6)
        self.accounts = allocator.load_accounts(allocator.REFERENCES / 'accounts.json')
        self.usage = {'accounts': {'codex': {
            'status': 'fresh', 'fetched_at': self.now.isoformat(), 'observation_id': 'new',
            'windows': [{'id': 'usage', 'used_pct': 94, 'remaining_pct_before_reset': 6,
                         'seconds_until_reset': 6 * 86400, 'resets_at': self.reset.isoformat()}]}}}
        self.override = {'id': 'priority', 'account': 'codex', 'model': 'gpt-6-astra',
                         'intent': 'user-priority', 'boost': 0.35, 'stop_on_reset': True,
                         'created_at': self.baseline.isoformat(),
                         'expected_reset_at': (self.now - timedelta(minutes=40)).isoformat(),
                         'until': (self.now + timedelta(hours=23)).isoformat(),
                         'baseline_windows': {'usage': {'account': 'codex', 'used_pct': 94,
                             'resets_at': self.reset.isoformat(), 'fetched_at': self.baseline.isoformat(),
                             'observation_id': 'old'}}}
        env = patch.dict(os.environ, {'ROUTER_OVERRIDES': str(self.path),
                                      'ROUTER_CAPACITY': str(self.root / 'capacity.json')})
        env.start()
        self.addCleanup(env.stop)

    def args(self, **changes):
        fields = dict(account='codex', model='gpt-6-astra', window='usage', intent='priority',
                      basis='user priority', boost=0.35, resets_at=self.override['expected_reset_at'],
                      resets_in=None, expires_at=None, expires_in=None, until=self.override['until'],
                      stop_on_reset=True, list=False, clear=None, usage_cache=None)
        return argparse.Namespace(**dict(fields, **changes))

    def replenished(self):
        usage = copy.deepcopy(self.usage)
        usage['accounts']['codex']['windows'][0].update(
            used_pct=0, remaining_pct_before_reset=100, resets_at=(self.now + timedelta(days=7)).isoformat())
        return usage

    def same_cycle(self, used=0):
        usage = self.replenished()
        usage['accounts']['codex']['windows'][0].update(
            used_pct=used, remaining_pct_before_reset=100 - used, resets_at=self.reset.isoformat())
        return usage

    def depleted(self, remaining=5):
        usage = copy.deepcopy(self.usage)
        usage['accounts']['codex']['windows'][0].update(used_pct=100 - remaining, remaining_pct_before_reset=remaining)
        return usage

    def test_overdue_forecast_cannot_grant_capacity_and_boost_targets_named_model(self):
        original = copy.deepcopy(self.usage)
        plan = allocator.plan({'work_kind': 'general_implementation', 'difficulty': 'hard',
                               'expected_hours': 1, 'needs': {'local_only': True}},
                              self.usage, self.now, accounts=self.accounts, overrides=[self.override])
        astra = next(r for r in plan['ranking'] if r['id'] == 'astra_high')
        sol = next(r for r in plan['ranking'] if r['id'] == 'sol_high')
        self.assertTrue(astra['excluded'])
        self.assertIsNone(astra['available_in_hours'])
        self.assertEqual(astra['components']['boost'], 0.35)
        self.assertNotIn('boost', sol['components'])
        self.assertTrue(any('overdue_uncertain' in n for n in astra['notes']))
        self.assertEqual(self.usage, original)

    def test_reset_confirmation_scheduled_and_one_off(self):
        one_off = dict(self.override, reset_confirmation='replenishment')
        self.assertTrue(allocator.override_reset_landed(self.override, self.replenished(), self.accounts, self.now))
        self.assertFalse(allocator.override_reset_landed(self.override, self.same_cycle(), self.accounts, self.now))
        self.assertTrue(allocator.override_reset_landed(one_off, self.same_cycle(), self.accounts, self.now))
        for invalid in ({'status': 'stale'}, {'stale': True}, {'available': False}, {'account': 'codex-2'},
                        {'fetched_at': self.baseline.isoformat()}, {'observation_id': 'old'},
                        {'fetched_at': (self.now - timedelta(minutes=16)).isoformat()},
                        {'fetched_at': (self.now + timedelta(seconds=1)).isoformat()}):
            with self.subTest(invalid=invalid):
                usage = self.replenished()
                usage['accounts']['codex'].update(invalid)
                self.assertFalse(allocator.override_reset_landed(self.override, usage, self.accounts, self.now))
                sc = self.same_cycle()
                sc['accounts']['codex'].update(invalid)
                self.assertFalse(allocator.override_reset_landed(one_off, sc, self.accounts, self.now))

    def test_reset_receipts_and_pruning(self):
        self.path.write_text(json.dumps({'overrides': [self.override], 'unrelated': 7}))
        allocator.observe_landed_overrides({'priority': self.override}, self.path, self.now)
        first = json.loads(self.path.read_text())
        marked = first['overrides'][0]
        self.assertEqual(marked['reset_confirmed_at'], self.now.isoformat())
        later = self.now + timedelta(hours=2)
        allocator.observe_landed_overrides({'priority': marked}, self.path, later)
        self.assertEqual(json.loads(self.path.read_text()), first)
        allocator.prune_landed_overrides({'priority': marked}, self.path, later)
        final = json.loads(self.path.read_text())
        self.assertEqual((final['overrides'], final['last_reset']['at'], final['unrelated']), ([], self.now.isoformat(), 7))

        # Confirmed marker survives stale usage; future/pre-creation marker is rejected; replaced record is untouched
        stale_usage = copy.deepcopy(self.usage)
        stale_usage['accounts']['codex']['status'] = 'stale'
        self.assertTrue(allocator.override_reset_landed(marked, stale_usage, self.accounts, later))
        for bad_when in (self.now + timedelta(seconds=1), self.baseline - timedelta(seconds=1)):
            self.assertFalse(allocator.override_reset_landed(dict(self.override, reset_confirmed_at=bad_when.isoformat()),
                                                             self.usage, self.accounts, self.now))
        newer = dict(self.override, boost=0.4)
        self.path.write_text(json.dumps({'overrides': [newer]}))
        allocator.prune_landed_overrides({'priority': self.override}, self.path, self.now)
        self.assertEqual(json.loads(self.path.read_text())['overrides'], [newer])

    def test_upsert_rebaseline_and_atomic_concurrency(self):
        with patch.object(router, 'load_allocator', return_value=allocator), \
             patch.object(router, 'usage_snapshot', return_value=self.usage):
            first = router.manage_overrides(self.args(), self.now)['active'][0]
            second = router.manage_overrides(self.args(boost=0.4), self.now + timedelta(seconds=1))['active'][0]
            self.assertEqual((second['id'], second['created_at'], second['boost']), (first['id'], first['created_at'], 0.4))

            # Repair legacy baseline with rebaseline=True
            old = copy.deepcopy(self.override)
            old['baseline_windows'] = {'usage': {'used_pct': 50, 'remaining_pct': 50, 'resets_at': self.reset.isoformat()}}
            self.path.write_text(json.dumps({'v': 1, 'overrides': [old]}))
            repaired = router.manage_overrides(self.args(intent='user-priority', window=None,
                                                         reset_confirmation='replenishment', rebaseline=True), self.now)['active'][0]
            self.assertEqual(repaired['baseline_windows']['usage']['fetched_at'], self.now.isoformat())

        # Atomic write failure retains original file
        self.path.write_text('{"overrides": []}')
        with override_store.locked(self.path), patch.object(override_store.os, 'replace', side_effect=OSError('fixture failure')):
            with self.assertRaises(OSError):
                override_store.write(self.path, {'overrides': [self.override]})
        self.assertEqual(json.loads(self.path.read_text()), {'overrides': []})

        # Concurrent process upserts do not lose intents
        cache = self.root / 'usage.json'
        cache.write_text(json.dumps({'providers': {'codex': {}}}))
        env = dict(os.environ, ROUTER_USAGE_CACHE=str(cache), ROUTER_LEDGER=str(self.root / 'ledger.jsonl'),
                   ROUTER_HOST_POLICY=str(self.root / 'unused-policy.json'), PYTHONDONTWRITEBYTECODE='1')
        script = str(Path(router.__file__))
        processes = [subprocess.Popen([sys.executable, '-B', script, 'override', '--account', 'codex',
                                       '--intent', intent, '--basis', 'fixture', '--boost', '0.1'],
                                      env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for intent in ('a', 'b', 'a', 'c', 'd')]
        try:
            for p in processes:
                out, err = p.communicate(timeout=30)
                self.assertEqual(p.returncode, 0, err or out)
        finally:
            for p in processes:
                if p.poll() is None:
                    p.kill()
                    p.communicate()
        rows = json.loads(self.path.read_text())['overrides']
        self.assertEqual(({r['intent'] for r in rows}, len(rows)), ({'a', 'b', 'c', 'd'}, 4))

    def test_depletion_retirement(self):
        self.assertTrue(allocator.override_depleted(self.override, self.depleted(5), self.accounts))
        self.assertFalse(allocator.override_depleted(self.override, self.depleted(5.5), self.accounts))
        other = dict(self.override, id='other', account='claude', model='claude-opus-5-5')
        self.path.write_text(json.dumps({'v': 1, 'overrides': [self.override, other]}))
        _, _, applied = allocator.apply_overrides([self.override], self.depleted(4), self.accounts, self.now)
        self.assertEqual(applied, [])
        self.assertEqual(json.loads(self.path.read_text()), {'v': 1, 'overrides': [other]})


class AntigravityTelemetryTests(unittest.TestCase):
    """Contract tests for Antigravity resilient transcript telemetry and handoff rotation."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.agent = self.home / '.paseo/agents/workspace/agent.json'
        self.agent.parent.mkdir(parents=True)
        self.log = self.home / '.gemini/antigravity-cli/brain/fixture/.system_generated/logs/transcript.jsonl'
        self.log.parent.mkdir(parents=True)

    def persistence(self, data=None, session=None):
        self.agent.write_text(json.dumps({'persistence': {'provider': 'antigravity-resilient',
            'sessionId': session or 'plugin:' + json.dumps({'version': 1, 'data': data or {'conversationId': 'fixture'}})}}))

    def test_resilient_bundles_and_session_key_binding(self):
        bundles = allocator.bundles(allocator.load_accounts(allocator.REFERENCES / 'accounts.json'))
        for name in ('flash_high', 'argon_high'):
            self.assertEqual((bundles[name]['provider'], bundles[name]['account'], bundles[name]['modeId']),
                             ('antigravity-resilient', 'antigravity', 'full-access'))
        session_key = 'workspace/session:fixture'
        binding = self.home / '.paseo/antigravity-resilient/conversations/session-keys' / (antigravity_telemetry.safe_key(session_key) + '.json')
        binding.parent.mkdir(parents=True)
        binding.write_text(json.dumps({'conversationId': 'fixture', 'secret': 'secret-canary'}))
        self.log.write_text(json.dumps({'type': 'PLANNER_RESPONSE', 'step_index': 1, 'usage': {'input_tokens': 123}}))
        for structured in (False, True):
            self.persistence({'sessionKey': session_key})
            if structured:
                self.agent.write_text(json.dumps({'persistence': {'provider': 'antigravity-resilient',
                    'version': 1, 'data': {'sessionKey': session_key}}}))
            self.assertEqual(allocator.session_state('agent', self.home)['context_tokens'], 123)

    def test_native_transcript_checkpoints_and_partial_evidence(self):
        self.persistence()
        steps = [
            {'type': 'PLANNER_RESPONSE', 'step_index': 1, 'content': 'secret-canary',
             'input_tokens': 20000, 'cache_read_tokens': 180000, 'output_tokens': 999999},
            {'type': 'CHECKPOINT', 'step_index': 2, 'created_at': 'old'},
            {'type': 'PLANNER_RESPONSE', 'step_index': 3, 'usage': {'input_tokens': 10000, 'cache_read_tokens': 20000}},
            {'type': 'CHECKPOINT', 'step_index': 2, 'created_at': 'replayed-later'},
            {'type': 'CHECKPOINT', 'step_index': 4},
            {'type': 'PLANNER_RESPONSE', 'step_index': 5, 'input_tokens': 12000, 'cache_read_tokens': 10000,
             'usage': {'input_tokens': 1, 'cache_read_tokens': 2}},
        ]
        self.log.write_text('\n'.join(map(json.dumps, steps)))
        result = allocator.session_state('agent', self.home)
        self.assertEqual((result['telemetry_status'], result['compactions'], result['compacted_tokens'], result['context_tokens']),
                         ('observed', 2, 230000, 22000))
        self.assertNotIn('secret-canary', json.dumps(result))

        # Partial or missing evidence
        self.log.write_text(json.dumps({'type': 'CHECKPOINT', 'step_index': 1}) + '\n{partial')
        self.assertEqual(allocator.session_state('agent', self.home)['telemetry_status'], 'partial')
        self.persistence({'conversationId': '../outside'})
        self.assertEqual(allocator.session_state('agent', self.home)['telemetry_status'], 'unknown')

    def test_carried_context_triggers_handoff_rotation(self):
        self.persistence()
        self.log.write_text('\n'.join(map(json.dumps, [
            {'type': 'PLANNER_RESPONSE', 'step_index': 1, 'usage': {'input_tokens': 2100000}},
            {'type': 'CHECKPOINT', 'step_index': 2},
            {'type': 'PLANNER_RESPONSE', 'step_index': 3, 'usage': {'input_tokens': 22000}},
        ])))
        accounts = allocator.load_accounts(allocator.REFERENCES / 'accounts.json')
        now = datetime(2026, 10, 16, 9, tzinfo=timezone.utc)
        original_session_log = allocator.session_log
        with patch.object(allocator, 'session_log', side_effect=lambda agent_id, home=None: original_session_log(agent_id, self.home)):
            plan = allocator.plan({'work_kind': 'general_implementation', 'difficulty': 'hard',
                'needs': {'local_only': True}, 'chain': {'coupling': 'high', 'handoff_ready': True,
                    'worker': {'bundle': 'argon_high', 'agent_id': 'agent'}}},
                {'accounts': {}}, now, accounts=accounts, overrides=[])
        argon = next(c for c in plan['ranking'] if c['id'] == 'argon_high')
        self.assertEqual(argon['action'], 'rotate')
        self.assertTrue(any('2.1M' in note for note in argon['notes']))


class MeasuredCapacityTests(unittest.TestCase):
    """Contract tests for capacity.json layered over accounts.json priors."""

    def setUp(self):
        self.now = datetime.now(timezone.utc)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'capacity.json'
        patcher = patch.object(allocator, 'capacity_override_path', return_value=self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, age_hours=0.5, **sections):
        document = {'v': 2, 'generated_at': (self.now - timedelta(hours=age_hours)).isoformat(),
                    'burn_pct_per_worker_hour': {}, 'evidence': {}, 'bundle_burn': {}, 'observed': {}}
        document.update(sections)
        self.path.write_text(json.dumps(document), encoding='utf-8')

    def usage(self, **windows):
        accounts = {name: {'windows': [
            {'id': wid, 'remaining_pct_before_reset': rem, 'observed_remaining_pct': rem,
             'seconds_until_reset': hours * 3600, 'resets_at': (self.now + timedelta(hours=hours)).isoformat()}
            for wid, rem, hours in rows]} for name, rows in windows.items()}
        return {'read_ok': True, 'accounts': accounts}

    def ranking(self, task, usage):
        result = allocator.plan(task, usage, self.now)
        return result, {c['id']: c for c in result['ranking']}

    def test_list_price_against_community_capacity_and_grandfathered_halving(self):
        self.write(burn_pct_per_worker_hour={'codex': {'usage': 5.0}}, evidence={'codex': {'usage': {'worker_hours': 100}}})
        accounts = allocator.load_accounts()
        self.assertEqual(accounts['accounts']['codex']['burn_pct_per_worker_hour']['usage'], 1.74)
        _, rows = self.ranking({'work_kind': 'general_implementation', 'difficulty': 'coupled', 'expected_hours': 1},
                               self.usage(codex=[('usage', 60, 100)]))
        capacity = allocator.capacity_usd(accounts['accounts']['codex'], 'usage', self.now)
        self.assertEqual((rows['sol_xhigh']['expected_hours'], rows['astra_high']['expected_hours']), (1.35, 1.0))
        self.assertAlmostEqual(rows['sol_xhigh']['expected_pct'], 100 * 2.6 * 1.35 / capacity * 0.85 / 0.7, places=1)
        self.assertAlmostEqual(rows['astra_high']['expected_pct'], 100 * 22.6 / capacity, places=1)
        spec = accounts['accounts']['codex']
        self.assertEqual(allocator.capacity_usd(spec, 'usage', datetime(2026, 10, 20, tzinfo=timezone.utc)), 650)
        self.assertEqual(allocator.capacity_usd(spec, 'usage', datetime(2026, 10, 30, 1, tzinfo=timezone.utc)), 650)

    def test_missing_or_stale_capacity_and_observed_burn(self):
        task = {'work_kind': 'general_implementation', 'difficulty': 'hard', 'expected_hours': 1}
        result, _ = self.ranking(task, self.usage(codex=[('usage', 60, 100)]))
        self.assertTrue(any('no fresh capacity.json' in note for note in result['decision']['assumptions']))
        self.write(age_hours=8, burn_pct_per_worker_hour={'codex': {'usage': 0.7}}, evidence={'codex': {'usage': {'worker_hours': 50}}})
        stale_res, _ = self.ranking(task, self.usage(codex=[('usage', 60, 100)]))
        self.assertTrue(any('8h old' in note for note in stale_res['decision']['assumptions']))

        usage = self.usage(claude=[('five_hour', 90, 3), ('seven_day', 90, 100), ('weekly_fable', 100, 100)])
        stamp = (self.now - timedelta(minutes=10)).isoformat()
        self.write(observed={'claude': {'seven_day': {'used_pct': 10.0, 'confirmed_at': stamp, 'pct_per_hour_3h': 0.2, 'pct_per_hour_24h': 0.2}}})
        calm = self.ranking(task, usage)[1]['opus55_high']
        self.assertEqual(calm['capacity']['demand_source'], 'observed')
        self.write(observed={'claude': {'seven_day': {'used_pct': 10.0, 'confirmed_at': stamp, 'pct_per_hour_3h': 5.0, 'pct_per_hour_24h': 5.0}}})
        hot = self.ranking(task, usage)[1]['opus55_high']
        self.assertAlmostEqual(hot['components']['opportunity_cost'],
                               allocator.load_accounts()['scoring']['shortage_loss_per_usd'] * 7.5, delta=0.003)


class RouterIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        patcher = patch.object(allocator, 'capacity_override_path', return_value=Path(tempfile.gettempdir()) / 'no-such-capacity.json')
        patcher.start()
        self.addCleanup(patcher.stop)
        scorer_env = patch.dict(os.environ, {'ROUTER_DISABLE_LIVE_SCORER': '1'})
        scorer_env.start()
        self.addCleanup(scorer_env.stop)

    def cache(self, directory):
        def window(window_id, used, hours):
            return {'id': window_id, 'used_pct': used, 'resets_at': (self.now + timedelta(hours=hours)).isoformat()}
        row = lambda *windows: {'fetched_at': self.now.isoformat(), 'available': True, 'stale': False, 'windows': list(windows)}
        path = Path(directory) / 'usage-cache.json'
        path.write_text(json.dumps({'providers': {
            'claude': row(window('five_hour', 10, 3), window('seven_day', 40, 100), window('weekly_fable', 0, 100)),
            'codex': row(window('usage', 40, 100)),
            'antigravity': row(window('gemini-5h', 10, 3), window('gemini-weekly', 20, 100))}}), encoding='utf-8')
        return path

    def run_main(self, argv):
        with patch('sys.argv', ['route_judge.py'] + argv), patch('builtins.print') as shown:
            code = router.main()
        return code, json.loads(shown.call_args.args[0])

    def test_plan_dry_run_no_eligible_and_record_agreement(self):
        self.addCleanup(setattr, router, 'LEDGER_WRITES', True)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(router, 'ledger_path', return_value=Path(directory) / 'ledger.jsonl'), \
                patch.object(router, 'cli_prefix', side_effect=AssertionError('Unexpected model call')):
            task = Path(directory) / 'task.json'
            cache = str(self.cache(directory))

            # 1. Chain ledgering and auto-resume
            task.write_text(json.dumps({'task': {
                'objective': 'continue refactor', 'work_kind': 'general_implementation', 'difficulty': 'coupled',
                'expected_hours': 1, 'chain': {'id': 'c1', 'coupling': 'high', 'worker': {'bundle': 'sol_xhigh'}}}}), encoding='utf-8')
            code, res = self.run_main(['plan', '--input', str(task), '--usage-cache', cache])
            self.assertEqual((code, res['choice'], res['decision']['action']), (0, 'sol_xhigh', 'resume'))
            task.write_text(json.dumps({'task': {'work_kind': 'general_implementation', 'difficulty': 'coupled',
                                                 'expected_hours': 1, 'chain': {'id': 'c1', 'coupling': 'high'}}}), encoding='utf-8')
            _, again = self.run_main(['plan', '--input', str(task), '--usage-cache', cache])
            self.assertEqual((again['choice'], again['decision']['action']), ('sol_xhigh', 'resume'))

            # 2. Dry-run leaves no extra ledger row; non-eligible exits 2
            before_count = len(router.read_ledger())
            task.write_text(json.dumps({'task': {'work_kind': 'frontend_implementation', 'difficulty': 'routine',
                                                 'expected_hours': 0.5, 'needs': {'local_only': True}}}), encoding='utf-8')
            code, dry = self.run_main(['plan', '--input', str(task), '--usage-cache', cache, '--dry-run'])
            self.assertEqual((code, dry['choice'], len(router.read_ledger())), (0, 'flash_high', before_count))

            # 3. Record agreement check
            def record(basis, provider, account, model, effort, *extra):
                return self.run_main(['record', '--input', str(task), '--usage-cache', cache,
                                      '--constraint', 'user_override', '--basis', basis, '--provider', provider,
                                      '--account', account, '--model', model, '--effort', effort, *extra])[1]
            override = record('User: use Sol max', 'codex', 'codex', 'gpt-6.1-sol', 'max', '--reason-category', 'quality_insufficient')
            agreed = record('User: Flash', 'antigravity-resilient', 'antigravity', 'gemini-3.8-flash-high', 'high')
            self.assertEqual((override['agrees'], override['router_choice'], agreed['agrees']), (False, 'flash_high', True))

            task.write_text(json.dumps({'task': {'work_kind': 'visual_analysis', 'expected_hours': 0.5, 'needs': {'visual': True},
                                                 'constraints': {'model': 'grok-4.7'}}}), encoding='utf-8')
            code, blocked = self.run_main(['plan', '--input', str(task), '--usage-cache', cache])
            self.assertEqual(code, 2)
            self.assertIn('needs_user_decision', [a['code'] for a in blocked['alerts']])


if __name__ == '__main__':
    unittest.main()
