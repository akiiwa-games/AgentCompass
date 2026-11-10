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
from router_test_support import isolated_router_state, profile, profile_for, temporary_root, usage_windows


EXPIRING_SUBSCRIPTION = {
    'plan': 'Grok subscription', 'billing': 'subscription', 'verified': True,
    'expires_at': '2026-10-10T23:59:00+08:00', 'expires_precision': 'date',
    'windows': {'credits': {'hours': 168}},
    'bundles': {'grok47_high': {'model': 'grok-4.7', 'effort': 'high'}},
}
_real_load_accounts = allocator.load_accounts


def reference_capabilities():
    return allocator.load_capabilities(accounts=allocator.load_accounts(allocator.reference_fleet_path()))


def _load_accounts_with_expiring_subscription(path=None):
    data = _real_load_accounts(path)
    if path is None:
        data['accounts']['grok'] = json.loads(json.dumps(EXPIRING_SUBSCRIPTION))
    return data


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup)
    fixture = patch.object(allocator, 'load_accounts', _load_accounts_with_expiring_subscription)
    fixture.start()
    unittest.addModuleCleanup(fixture.stop)


class AllocatorTests(unittest.TestCase):
    DOT_ROUTES = ('dot_execution', 'web_research', 'text_in_text_out', 'self_contained_code')
    BULK = {'work_kind': 'bounded_bulk_work', 'difficulty': 'hard', 'expected_hours': 6}
    SCARCE = {'claude': [('five_hour', 90, 3), ('seven_day', 12, 100), ('weekly_fable', 100, 100)], 'codex': [('usage', 12, 100)]}
    PLENTY = {'claude': [('five_hour', 95, 3), ('seven_day', 95, 100), ('weekly_fable', 100, 100)], 'codex': [('usage', 95, 100)]}

    def setUp(self):
        self.now = datetime(2026, 10, 9, 3, 0, tzinfo=timezone.utc)

    def usage(self, **overrides):
        windows = {
            'claude': [('five_hour', 90, 3), ('seven_day', 60, 100), ('weekly_fable', 100, 100)],
            'codex': [('usage', 60, 100)],
            'grok': [('credits', 0, 2.2)],
            'antigravity': [('gemini-5h', 90, 3), ('gemini-weekly', 60, 100), ('3p-5h', 90, 3), ('3p-weekly', 60, 100)],
        }
        windows.update(overrides)
        windows.setdefault('claude-lab', list(windows['claude']))
        return usage_windows(self.now, windows)

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

    def profile(self, **fields):
        access = {'access': 'read-only'} if fields.get('read_only_review') else {}
        return profile(**{'local_only': True, **access, **fields})

    def unsubscribed(self, account):
        accounts = allocator.load_accounts()
        accounts['accounts'].pop(account)
        return accounts

    def codex_bundles(self):
        return [b for b, v in allocator.bundles(allocator.load_accounts()).items() if v['account'] == 'codex']

    def test_chain_continuity_and_handoff_rotation(self):
        # 1. Active high-coupling chain resumes owner; blocked owner asks user; low-coupling switches silently
        # the whole 6 h chain at xhigh is ~59% of the Codex window at the Artificial Analysis time (0.96x the anchor's High), so the
        # owner resumes with 80% left
        roomy = self.usage(codex=[('usage', 80, 100)])
        resumed = self.plan({'work_kind': 'general_implementation', 'difficulty': 'coupled', 'expected_hours': 2, 'chain': self.chain()}, roomy)
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

        # 4. Carried context (>=2M historical tokens) rotates unconditionally and unlocks re-allocation; compactions alone (<2M) still resume
        base_task = {'work_kind': 'general_implementation', 'difficulty': 'coupled', 'expected_hours': 1}
        for chain_kw, expected_action in (
            ({'tokens': 120000, 'compacted': 360000, 'compactions': 2}, 'resume'),
            ({'tokens': 60000, 'compacted': 8 * 244000, 'handoff_ready': True}, 'rotate'),
            ({'tokens': 60000, 'compacted': 3000000}, 'rotate'),
        ):
            with self.subTest(chain_kw=chain_kw):
                res = self.plan(dict(base_task, chain=self.chain(**chain_kw)), roomy)
                self.assertEqual(res['decision']['action'], expected_action)
                if expected_action == 'rotate':
                    self.assertNotEqual(res['choice'], 'sol_xhigh')


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
            agent('a4', 'claude-lab', 's-c2')
            log('.claude-lab/projects/E--PROJECTS-x/s-c2.jsonl', [
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
            agent('a5', 'claude', 's-claude-long-context')
            log('.claude/projects/E--PROJECTS-x/s-claude-long-context.jsonl', [
                {'type': 'system', 'subtype': 'compact_boundary', 'compactMetadata': {'preTokens': 1950000, 'postTokens': 12000, 'cumulativeDroppedTokens': 1938000}},
                {'type': 'assistant', 'message': {'usage': {'input_tokens': 100, 'cache_read_input_tokens': 80000, 'cache_creation_input_tokens': 0}}}])
            st5 = allocator.session_state('a5', home)
            self.assertEqual((st5['compactions'], st5['context_tokens'], st5['historical_context_tokens']), (1, 80100, 2030100))
            orig_log = allocator.session_log
            with patch.object(allocator, 'session_log', side_effect=lambda aid, h=None: orig_log(aid, home)):
                rot = self.plan({'work_kind': 'general_implementation', 'difficulty': 'coupled',
                                 'chain': {'id': 'c5', 'coupling': 'high', 'worker': {'agent_id': 'a5', 'bundle': 'sol_xhigh', 'context_tokens': 80100}}})
                self.assertEqual(rot['decision']['action'], 'rotate')

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
        # The harness order never touches the score: the Pi bundle and its Codex twin score alike and differ in place only.
        self.assertAlmostEqual(scores[explore['choice']], scores[explore['choice'][3:]], places=6)
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

    def test_dot_eligibility_respects_route_modality_and_local_needs(self):
        capabilities = reference_capabilities()
        self.assertEqual(self.plan({'work_kind': 'dot_execution'})['choice'], 'dot_text')
        # Caller false stands. Route implies fill local_only only when the caller and the profile left it unset.
        self.assertTrue(allocator.dot_may_take('exploration_analysis', {'local_only': False}, capabilities))
        self.assertTrue(allocator.dot_may_take('exploration_analysis', {}, capabilities, {'local_only': False}))
        self.assertTrue(allocator.dot_may_take('research_packet', {}, capabilities, {'local_only': False}))
        self.assertFalse(allocator.dot_may_take('exploration_analysis', {}, capabilities, {'local_only': True}))
        self.assertTrue(allocator.dot_may_take('exploration_analysis', {'local_only': False}, capabilities, {'local_only': True}))
        self.assertFalse(allocator.dot_may_take('exploration_analysis', {}, capabilities))
        self.assertFalse(allocator.dot_may_take('general_implementation', {'visual': True}, capabilities))
        for kind in ('research_packet', 'exploration_analysis'):
            opened = self.plan({'work_kind': kind, 'difficulty': 'hard', 'needs': {'local_only': False},
                                'profile': self.profile(local_only=False)})
            admitted = {c['bundle']['model'] for c in opened['ranking'] if c['score'] is not None and not c['excluded']}
            self.assertIn('dot', admitted, kind)

        for kind in ('web_research', 'text_in_text_out', 'self_contained_code'):
            res = self.plan({'work_kind': kind, 'difficulty': 'routine'}, self.usage(grok=[('credits', 100, 165)]))
            self.assertEqual(res['choice'], 'dot_text', kind)
            local = self.plan({'work_kind': kind, 'difficulty': 'routine', 'needs': {'local_only': True}})
            self.assertNotEqual(local['choice'], 'dot_text', kind)
        for difficulty in ('hard', 'coupled'):
            self.assertEqual(self.plan({'work_kind': 'web_research', 'difficulty': difficulty})['choice'], 'dot_text', difficulty)

    def test_short_window_deferral_and_wait_limits(self):
        usage = self.usage(claude=[('five_hour', 3, 1.0), ('seven_day', 60, 100), ('weekly_fable', 100, 100)], codex=[('usage', 1, 121)])
        task = {'work_kind': 'visual_analysis', 'difficulty': 'routine', 'expected_hours': 1, 'needs': {'visual': True}}
        no_argon = ['argon_high'] + [b for b in allocator.bundles(allocator.load_accounts()) if b.startswith(('agy_opus', 'agy_sonnet'))]
        # The cheapest sufficient candidate sits behind Claude's short five-hour window: a task that can wait waits for the reset.
        deferred = self.plan(dict(task, can_wait_hours=2), usage, unavailable=no_argon)
        self.assertEqual(deferred['decision']['action'], 'defer')

        long_wait = self.usage(claude=[('five_hour', 3, 4), ('seven_day', 60, 100), ('weekly_fable', 100, 100)], codex=[('usage', 1, 121)])
        impatient = self.plan(dict(task, can_wait_hours=1), long_wait, unavailable=['flash_high'] + no_argon)
        self.assertTrue(impatient['decision']['needs_user_decision'])
        patient = self.plan(dict(task, can_wait_hours=5), long_wait, unavailable=['flash_high'] + no_argon)
        self.assertEqual((patient['decision']['needs_user_decision'], patient['decision']['action']), (False, 'defer'))

    def test_claude_2_backup_safety_floor_and_inflight_reservation(self):
        normal_claude2 = self.usage(
            claude=[('five_hour', 90, 3), ('seven_day', 45, 100), ('weekly_fable', 100, 100)],
            **{'claude-lab': [('five_hour', 90, 3), ('seven_day', 85, 100), ('weekly_fable', 100, 100)]},
        )
        # With every big pool empty claude-lab is what takes the work, at the effort that fits its remaining window (in ordinary
        # times Argon's shorter Artificial Analysis time takes this task instead, so no claude-lab choice is pinned there)
        all_big_empty = self.usage(
            claude=[('five_hour', 90, 3), ('seven_day', 0, 100), ('weekly_fable', 100, 100)],
            codex=[('usage', 1, 100)],
            antigravity=[('gemini-5h', 90, 3), ('gemini-weekly', 2, 100), ('3p-5h', 90, 3), ('3p-weekly', 2, 100)],
            **{'claude-lab': [('five_hour', 65, 3), ('seven_day', 83, 100), ('weekly_fable', 100, 100)]},
        )
        self.assertTrue(self.plan({'work_kind': 'exploration_analysis'}, all_big_empty)['choice'].startswith('lab_sonnet55_'))

        task = {'work_kind': 'exploration_analysis', 'difficulty': 'hard', 'expected_hours': 0.7, 'needs': {'local_only': True}}
        usage_low = self.usage(**{'claude-lab': [('five_hour', 23, 2.5), ('seven_day', 61, 130)], 'claude': [('five_hour', 0, 2.0), ('seven_day', 0, 100)]})
        self.assertTrue(self.rows(self.plan(task, usage_low))['lab_sonnet55_high']['excluded'])

        usage_40 = self.usage(**{'claude-lab': [('five_hour', 40, 2.5), ('seven_day', 61, 130)], 'claude': [('five_hour', 0, 2.0), ('seven_day', 0, 100)]})
        usage_40['accounts']['claude-lab']['fetched_at'] = (self.now + timedelta(seconds=12)).isoformat()
        inflight = [{
            'at': (self.now + timedelta(seconds=10)).isoformat(), 'stage': 'dispatch', 'dispatch_id': 'fixture-dispatch',
            'state': 'started', 'lease_until': (self.now + timedelta(minutes=10)).isoformat(),
            'choice': 'lab_sonnet55_high', 'hours': 0.7, 'expected_usd': 3.08, 'kind': 'exploration_analysis',
            'bundle': {'provider': 'claude-lab', 'account': 'claude-lab', 'model': 'claude-sonnet-5-5', 'bundle_id': 'lab_sonnet55_high', 'expected_usd': 3.08},
            'usage': {'claude-lab': {'windows': [{'id': 'five_hour', 'used_pct': 60.0}, {'id': 'seven_day', 'used_pct': 39.0}]}},
        }]
        s2 = self.rows(allocator.plan(task, usage_40, self.now + timedelta(minutes=2), records=inflight))['lab_sonnet55_high']
        self.assertTrue(s2['excluded'])
        self.assertTrue(any('$3.08' in n and 'reserved by in-flight' in n for n in s2['notes']), s2['notes'])

        # Unstarted 5h window (used_pct=0, resets_at=None) still enforces 5h wall against oversized or burst tasks
        unstarted_5h = copy.deepcopy(normal_claude2)
        unstarted_5h['accounts']['claude-lab']['status'] = 'fresh'
        unstarted_5h['accounts']['claude-lab']['windows'][0] = {
            'id': 'five_hour', 'used_pct': 0.0, 'resets_at': None, 'seconds_until_reset': None,
            'observed_remaining_pct': 100.0, 'remaining_pct_before_reset': None}
        self.assertEqual(self.plan(task, unstarted_5h)['choice'], 'lab_sonnet55_high')
        burst = [dict(inflight[0], dispatch_id=f'd-{i}', expected_usd=6.6) for i in (1, 2)]
        self.assertTrue(self.rows(allocator.plan(
            {'work_kind': 'exploration_analysis', 'difficulty': 'hard', 'expected_hours': 1.5, 'needs': {'local_only': True}},
            unstarted_5h, self.now + timedelta(minutes=2), records=burst))['lab_sonnet55_high']['excluded'])

        # Stale claude-lab with active 5h reset and unknown/clamped used_pct or oversized 3h task is excluded
        stale_c2 = self.usage(
            claude=[('five_hour', 75, 4.0), ('seven_day', 20, 35.0), ('weekly_fable', 100, 35.0)],
        )
        stale_c2['accounts']['claude-lab'] = {
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
        self.assertTrue(self.rows(res_1h)['lab_sonnet55_high']['excluded'])
        self.assertNotEqual(res_1h['selected_bundle']['account'], 'claude-lab')

        # Even if five_hour telemetry is completely missing, an oversized task exceeding full 5h Pro capacity is excluded
        missing_5h = copy.deepcopy(stale_c2)
        missing_5h['accounts']['claude-lab']['windows'] = [missing_5h['accounts']['claude-lab']['windows'][1]]
        custom_acc = allocator.load_accounts()
        custom_acc['accounts']['claude-lab']['bundles']['lab_opus55_high'].pop('disabled', None)
        res_opus_3h = self.plan({'work_kind': 'frontend_implementation', 'difficulty': 'hard', 'expected_hours': 3.0,
                                 'needs': {'local_only': True}}, missing_5h, accounts=custom_acc)
        self.assertTrue(self.rows(res_opus_3h)['lab_opus55_high']['excluded'])
        res_sonnet_4h = self.plan({'work_kind': 'exploration_analysis', 'difficulty': 'hard', 'expected_hours': 3.8,
                                   'needs': {'local_only': True}}, missing_5h)
        self.assertTrue(self.rows(res_sonnet_4h)['lab_sonnet55_high']['excluded'])

    def test_constraints_and_validation(self):
        self.assertTrue(self.plan({'work_kind': 'general_implementation', 'difficulty': 'hard',
                                   'constraints': {'model': 'claude-opus-5'}})['selected_bundle']['model'].startswith('claude-opus-5-5'))
        self.assertIn('account not verified as launchable',
                      self.rows(self.plan({'work_kind': 'general_implementation', 'difficulty': 'coupled'}))['work_sol_xhigh']['excluded'])
        with self.assertRaises(ValueError):
            self.plan({'difficulty': 'impossible'})
        with self.assertRaises(ValueError):
            self.plan({'chain': {'coupling': 'tight'}})
        accounts = allocator.load_accounts()
        models = allocator.models_table(accounts)
        for bundle in allocator.bundles(accounts).values():
            self.assertIn(allocator.canonical(bundle['model'], accounts), models, bundle['id'])

    def test_heuristic_profile_and_schema_contract(self):
        capabilities = reference_capabilities()
        policy = allocator.load_accounts()['scoring']
        accounts = allocator.load_accounts()
        for banned in (
            'infer_task_profile', 'infer_kind_from_objective', 'infer_fast_preference',
            'is_readonly_review_task', 'infer_divergence', 'multi_task_marks',
            'DIVERGENT_WORDS', 'FAST_REQUEST_WORDS', 'READONLY_REVIEW_PHRASES',
            'GUI_WORDS', 'COMPUTER_USE_PHRASES', 'CODE_FILE_RE',
            'OBJECTIVE_LOCAL_RUNTIME_RE', 'LOCAL_MACHINE_RE', 'MULTI_TASK_MARK_RE',
        ):
            self.assertFalse(hasattr(allocator, banned), f'{banned} must be removed')
        for kind in capabilities['routes']:
            for difficulty in (None, 'routine', 'hard', 'coupled', 'frontier'):
                task = {'work_kind': kind, **({'difficulty': difficulty} if difficulty else {})}
                normalized = allocator.normalize_task(task, policy, capabilities, accounts)
                # No default table: the caller's difficulty, else the one the profile derives.
                self.assertEqual(normalized['difficulty'], difficulty or normalized['profile_difficulty'], task)
        good = self.profile()
        for bad in (dict(good, reasoning_depth=5), dict(good, code_complexity=True),
                    {k: v for k, v in good.items() if k != 'axis_demand'}, {k: v for k, v in good.items() if k != 'realtime_loop'},
                    dict(good, needs_gui='yes'), dict(good, wants_fast='yes'), dict(good, read_only_review=1),
                    dict(good, interaction_style='coding'), 'deep'):
            with self.assertRaises(ValueError):
                allocator.validate_profile(bad)
        schema = allocator.TASK_PROFILE_SCHEMA
        self.assertEqual(set(schema['required']), set(schema['properties']))
        self.assertIn('wants_fast', schema['properties'])
        self.assertIn('read_only_review', schema['properties'])
        for name, prop in schema['properties'].items():
            if prop['type'] == 'integer':
                self.assertNotIn('enum', prop, name)
                self.assertEqual((prop['minimum'], prop['maximum']), allocator.PROFILE_INTEGERS[name])
        # local_only is resolved strictly from caller needs -> profile.local_only -> route implies -> False, never from objective/cwd text.
        prof_local = allocator.normalize_task(
            {'objective': 'Pure text', 'profile': self.profile(local_only=True)},
            policy, capabilities, accounts,
        )
        self.assertTrue(prof_local['local_only'])
        caller_false = allocator.normalize_task(
            {'objective': 'Run pytest', 'cwd': r'E:\PROJECTS\AgentCompass', 'needs': {'local_only': False},
             'profile': self.profile(local_only=True)},
            policy, capabilities, accounts,
        )
        self.assertFalse(caller_false['local_only'])

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
        # Route selection follows the classified profile work_kind regardless of incidental keywords in objective.
        capabilities = reference_capabilities()
        policy = allocator.load_accounts()['scoring']
        for objective, kind in (
            ('Click the Save button in the desktop settings', 'debug_review'),
            ('Compare screenshots with the UI mockup', 'general_implementation'),
            ('检查截图并列出布局差异', 'visual_analysis'),
            ('Implement the desktop UI component in React', 'frontend_implementation'),
            ('Batch rename i18n keys uniformly', 'bounded_bulk_work'),
            ('Search the web for public documentation', 'web_research'),
            ('Translate the deployment runbook without executing it', 'text_in_text_out'),
            ('Write a multi-phase system migration spec', 'complex_build_plan'),
            ('Prove or refute this mathematical bound', 'direction_reasoning'),
            ('Run a quantitative experiment and benchmark suite, report metrics', 'research_packet'),
            ('Follow the CI release runbook and health check', 'tool_execution'),
            ('Diagnose the memory leak in frontend rendering', 'debug_review'),
            ('Explore the repository architecture', 'exploration_analysis'),
            ('Implement a standalone algorithm', 'self_contained_code'),
            ('Fix the keyboard shortcuts in editor.py.', 'debug_review'),
            ('用鼠标点击设置里的保存', 'computer_use'),
        ):
            with self.subTest(objective=objective, kind=kind):
                prof = self.profile(work_kind=kind)
                self.assertEqual(allocator.task_kind({'objective': objective, 'profile': prof}, capabilities, policy), kind)

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

    def test_classified_read_only_review_profile_and_its_own_hours(self):
        ro_prof = self.profile(work_kind='debug_review', code_complexity=0, read_only_review=True)
        ro_norm = allocator.normalize_task(
            {'objective': 'Independent review (read-only) of the authentication module', 'profile': ro_prof},
            allocator.load_accounts()['scoring'],
            reference_capabilities(),
            allocator.load_accounts(),
        )
        self.assertEqual(ro_norm['kind'], 'debug_review')
        self.assertEqual(ro_norm['profile']['code_complexity'], 0)
        self.assertTrue(ro_norm['read_only_review'])
        self.assertEqual(ro_norm['hours'], ro_prof['expected_hours'])      # the classifier's own estimate; no per-difficulty default

    def test_existing_bundles_keep_tag_pools_and_draws_override(self):
        accounts = allocator.load_accounts()
        flat = allocator.bundles(accounts)
        # Expected pools are contract examples, not a copy of the production tag predicate.
        for bid, expected in (
            ('opus55_high', ['five_hour', 'seven_day']),
            ('sonnet55_high', ['five_hour', 'seven_day']),
            ('fable51_high', ['five_hour', 'seven_day', 'weekly_fable']),
            ('fable52_high', ['five_hour', 'seven_day', 'weekly_fable']),
            ('sol_high', ['usage']),
            ('pi_sol_high', ['usage']),
            ('flash_high', ['gemini-5h', 'gemini-weekly']),
            ('argon_high', ['gemini-5h', 'gemini-weekly']),
            ('agy_opus55_high', ['3p-5h', '3p-weekly']),
            ('dot_text', []),
        ):
            with self.subTest(bundle=bid):
                bundle = flat[bid]
                spec = accounts['accounts'][bundle['account']]
                self.assertEqual(allocator.bundle_pools(bundle, accounts), expected)
                self.assertEqual([wid for wid, _, _ in allocator.applicable_windows(
                    spec, bundle['model'], {}, bundle, accounts)], expected)
                if 'draws' not in bundle:
                    self.assertEqual([wid for wid, _, _ in allocator.applicable_windows(
                        spec, allocator.canonical(bundle['model'], accounts), {})], expected)
        for bid, draws in (('flash_high', ['3p-weekly']), ('agy_opus55_high', ['3p-weekly']),
                           ('agy_opus55_high', [])):
            with self.subTest(bundle=bid, draws=draws):
                self.assertEqual(allocator.bundle_pools({**flat[bid], 'draws': draws}, accounts), draws)
        for family, canonical_name in (('opus', 'claude-opus-5-5'), ('sonnet', 'claude-sonnet-5-5')):
            for effort in ('low', 'medium', 'high'):
                bundle = flat[f'agy_{family}55_{effort}']
                self.assertEqual(allocator.bundle_pools(bundle, accounts), ['3p-5h', '3p-weekly'])
                self.assertEqual(allocator.canonical(bundle['model'], accounts), canonical_name)
                self.assertEqual(bundle['model'], f'{canonical_name}-{effort}')
                self.assertEqual(bundle['launch_model'], bundle['model'])
                self.assertEqual((bundle['provider'], bundle['modeId']), ('antigravity-resilient', 'full-access'))
                self.assertNotIn('disabled', bundle)
        self.assertNotIn('when', flat['agy_opus55_low'])

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
        self.assertIs(kept['flash_high'].get('disabled'), True)
        self.assertNotIn('disabled', kept['agy_opus55_high'])
        self.assertNotIn('disabled', kept['agy_sonnet55_high'])


class OverrideContractTests(unittest.TestCase):
    """Contract tests for temporary priority overrides, reset confirmation, receipts, and atomic store."""

    def setUp(self):
        self.root = temporary_root(self.addCleanup)
        self.path = self.root / 'overrides.json'
        self.now = datetime(2026, 10, 16, 9, tzinfo=timezone.utc)
        self.baseline = self.now - timedelta(hours=1)
        self.reset = self.now + timedelta(days=6)
        self.accounts = allocator.load_accounts(allocator.reference_fleet_path())
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
        for invalid in ({'status': 'stale'}, {'stale': True}, {'available': False}, {'account': 'codex-work'},
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
            override_rows = [row for row in router.read_ledger() if row.get('stage') == 'override']
            self.assertEqual((override_rows[-1]['action'], override_rows[-1]['account'], override_rows[-1]['removed']),
                             ('upsert', 'codex', 0))
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
        self.home = temporary_root(self.addCleanup)
        self.agent = self.home / '.paseo/agents/workspace/agent.json'
        self.agent.parent.mkdir(parents=True)
        self.log = self.home / '.gemini/antigravity-cli/brain/fixture/.system_generated/logs/transcript.jsonl'
        self.log.parent.mkdir(parents=True)

    def persistence(self, data=None, session=None):
        self.agent.write_text(json.dumps({'persistence': {'provider': 'antigravity-resilient',
            'sessionId': session or 'plugin:' + json.dumps({'version': 1, 'data': data or {'conversationId': 'fixture'}})}}))

    def test_resilient_bundles_and_session_key_binding(self):
        bundles = allocator.bundles(allocator.load_accounts(allocator.reference_fleet_path()))
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
        accounts = allocator.load_accounts(allocator.reference_fleet_path())
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


class RouterIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
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

    @staticmethod
    def dumps_task(document):
        """The CLI loads its own allocator module, so a test task carries the profile a classifier would give it."""
        task = document['task']
        return json.dumps({**document, 'task': {**task, 'profile': task.get('profile') or profile_for(task)}})

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
            task.write_text(self.dumps_task({'task': {
                'objective': 'continue refactor', 'work_kind': 'general_implementation', 'difficulty': 'coupled',
                'expected_hours': 1, 'chain': {'id': 'c1', 'coupling': 'high', 'worker': {'bundle': 'sol_xhigh'}}}}), encoding='utf-8')
            code, res = self.run_main(['plan', '--input', str(task), '--usage-cache', cache])
            self.assertEqual((code, res['choice'], res['decision']['action']), (0, 'sol_xhigh', 'resume'))
            task.write_text(self.dumps_task({'task': {'work_kind': 'general_implementation', 'difficulty': 'coupled',
                                                 'expected_hours': 1, 'chain': {'id': 'c1', 'coupling': 'high'}}}), encoding='utf-8')
            _, again = self.run_main(['plan', '--input', str(task), '--usage-cache', cache])
            self.assertEqual((again['choice'], again['decision']['action']), ('sol_xhigh', 'resume'))

            # 2. Dry-run leaves no extra ledger row; non-eligible exits 2
            before_count = len(router.read_ledger())
            task.write_text(self.dumps_task({'task': {'work_kind': 'frontend_implementation', 'difficulty': 'routine',
                                                 'expected_hours': 0.5, 'needs': {'local_only': True}}}), encoding='utf-8')
            code, dry = self.run_main(['plan', '--input', str(task), '--usage-cache', cache, '--dry-run'])
            self.assertEqual((code, len(router.read_ledger())), (0, before_count))
            planned = dry['choice']

            # 3. Record agreement check
            task.write_text(json.dumps({'task': {'work_kind': 'frontend_implementation', 'difficulty': 'routine',
                                                 'expected_hours': 0.5, 'needs': {'local_only': True}}}), encoding='utf-8')
            def record(basis, provider, account, model, effort, *extra):
                return self.run_main(['record', '--input', str(task), '--usage-cache', cache,
                                      '--constraint', 'user_override', '--basis', basis, '--provider', provider,
                                      '--account', account, '--model', model, '--effort', effort, *extra])[1]
            override = record('User: use Sol max', 'codex', 'codex', 'gpt-6.1-sol', 'max', '--reason-category', 'quality_insufficient')
            agreed = record('User: Flash', 'antigravity-resilient', 'antigravity', 'gemini-3.8-flash-high', 'high')
            # record skips the classifier. Without a caller profile, agrees is a marker, not a comparison.
            self.assertEqual((override['agrees'], override['classifier_skipped'], override['router_choice']),
                             (None, True, None))
            self.assertEqual((agreed['agrees'], agreed['classifier_skipped'], agreed['router_choice']),
                             (None, True, None))
            skipped_row = router.read_ledger()[-1]
            self.assertTrue(skipped_row['classifier_skipped'])
            self.assertIsNone(skipped_row['agrees'])
            task.write_text(json.dumps({'task': {
                'work_kind': 'frontend_implementation', 'expected_hours': 0.5, 'needs': {'local_only': True},
                'profile': profile(work_kind='frontend_implementation', reasoning_depth=1, local_only=True,
                                   reason='caller profile')}}), encoding='utf-8')
            profiled = record('User: Flash', 'antigravity-resilient', 'antigravity', 'gemini-3.8-flash-high', 'high')
            self.assertEqual((profiled['classifier_skipped'], profiled['agrees'], profiled['router_choice']),
                             (False, False, planned))

            task.write_text(self.dumps_task({'task': {'work_kind': 'visual_analysis', 'expected_hours': 0.5, 'needs': {'visual': True},
                                                 'constraints': {'model': 'grok-4.7'}}}), encoding='utf-8')
            code, blocked = self.run_main(['plan', '--input', str(task), '--usage-cache', cache])
            self.assertEqual(code, 2)
            self.assertIn('needs_user_decision', [a['code'] for a in blocked['alerts']])


class RealtimeLoopAndResponsivenessTests(unittest.TestCase):
    """Tests for `realtime_loop` task-profile flag, heuristic fallback, `responsiveness` scoring/floor,
    fresh-install zero-local-data behavior, conservative default on unmeasured models, and `refresh_speed_data.py`."""

    def setUp(self):
        self.now = datetime(2026, 10, 9, 3, 0, tzinfo=timezone.utc)

    def healthy_usage(self):
        windows = {
            'claude': [('five_hour', 90, 3), ('seven_day', 80, 100), ('weekly_fable', 100, 100)],
            'claude-lab': [('five_hour', 90, 3), ('seven_day', 80, 100), ('weekly_fable', 100, 100)],
            'codex': [('usage', 80, 100)],
            'grok': [('credits', 80, 100)],
            'antigravity': [('gemini-5h', 90, 3), ('gemini-weekly', 80, 100), ('3p-5h', 90, 3), ('3p-weekly', 80, 100)],
        }
        return usage_windows(self.now, windows)

    SPEED_FIXTURE = {
        'schema_version': 1,
        'fetched_at': '2026-11-06T09:16:00Z',
        'sources': [
            {
                'id': 'artificial_analysis',
                'url': 'https://artificialanalysis.ai/leaderboards/models',
                'accessed_at': '2026-11-06',
                'access': 'scrape',
            },
            {
                'id': 'openrouter',
                'url': 'https://openrouter.ai/api/v1/models/<author>/<slug>/endpoints',
                'accessed_at': '2026-11-06',
                'access': 'api',
            },
        ],
        'models': {
            'gemini-4-argon-high': {
                'status': 'ok',
                'entries': [
                    {
                        'router_model': 'gemini-4-argon-high',
                        'effort': 'high',
                        'source_id': 'artificial_analysis',
                        'source_name': 'Gemini 4 Argon (High)',
                        'source_slug': 'gemini-4-argon',
                        'match_kind': 'exact',
                        'output_tps': 78.66,
                        'ttft_s': 41.09,
                        'reasoning_tokens_per_turn': 2888116,
                    },
                ],
            },
            'gemini-3.8-flash-high': {
                'status': 'ok',
                'entries': [
                    {
                        'router_model': 'gemini-3.8-flash-high',
                        'effort': 'low',
                        'source_id': 'artificial_analysis',
                        'source_name': 'Gemini 3.8 Flash (Low)',
                        'source_slug': 'gemini-3-8-flash',
                        'match_kind': 'exact',
                        'output_tps': 68.54,
                        'ttft_s': 1.80,
                        'reasoning_tokens_per_turn': 570993,
                    },
                    {
                        'router_model': 'gemini-3.8-flash-high',
                        'effort': 'medium',
                        'source_id': 'artificial_analysis',
                        'source_name': 'Gemini 3.8 Flash (Medium)',
                        'source_slug': 'gemini-3-8-flash',
                        'match_kind': 'exact',
                        'output_tps': 67.35,
                        'ttft_s': 5.74,
                        'reasoning_tokens_per_turn': 1707038,
                    },
                    {
                        'router_model': 'gemini-3.8-flash-high',
                        'effort': 'high',
                        'source_id': 'artificial_analysis',
                        'source_name': 'Gemini 3.8 Flash (High)',
                        'source_slug': 'gemini-3-8-flash',
                        'match_kind': 'exact',
                        'output_tps': 73.12,
                        'ttft_s': 19.62,
                        'reasoning_tokens_per_turn': 5668404,
                    },
                ],
            },
            'gpt-6.1-sol': {
                'status': 'ok',
                'entries': [
                    {
                        'router_model': 'gpt-6.1-sol',
                        'effort': 'low',
                        'source_id': 'artificial_analysis',
                        'source_name': 'GPT-6.1 Sol (Low)',
                        'source_slug': 'gpt-6-1-sol',
                        'match_kind': 'exact',
                        'output_tps': 53.06,
                        'ttft_s': 2.76,
                        'reasoning_tokens_per_turn': 300000,
                    },
                    {
                        'router_model': 'gpt-6.1-sol',
                        'effort': 'medium',
                        'source_id': 'artificial_analysis',
                        'source_name': 'GPT-6.1 Sol (Medium)',
                        'source_slug': 'gpt-6-1-sol',
                        'match_kind': 'exact',
                        'output_tps': 53.44,
                        'ttft_s': 6.60,
                        'reasoning_tokens_per_turn': 1000000,
                    },
                    {
                        'router_model': 'gpt-6.1-sol',
                        'effort': 'high',
                        'source_id': 'artificial_analysis',
                        'source_name': 'GPT-6.1 Sol (High)',
                        'source_slug': 'gpt-6-1-sol',
                        'match_kind': 'exact',
                        'output_tps': 53.16,
                        'ttft_s': 45.89,
                        'reasoning_tokens_per_turn': 5000000,
                    },
                ],
            },
            'gpt-6-astra': {
                'status': 'ok',
                'entries': [
                    {
                        'router_model': 'gpt-6-astra',
                        'effort': 'low',
                        'source_id': 'artificial_analysis',
                        'source_name': 'GPT-6 Astra (Low)',
                        'source_slug': 'gpt-6-astra',
                        'match_kind': 'exact',
                        'output_tps': 43.58,
                        'ttft_s': 3.97,
                        'reasoning_tokens_per_turn': 400000,
                    },
                    {
                        'router_model': 'gpt-6-astra',
                        'effort': 'medium',
                        'source_id': 'artificial_analysis',
                        'source_name': 'GPT-6 Astra (Medium)',
                        'source_slug': 'gpt-6-astra',
                        'match_kind': 'exact',
                        'output_tps': 46.64,
                        'ttft_s': 6.95,
                        'reasoning_tokens_per_turn': 1100000,
                    },
                    {
                        'router_model': 'gpt-6-astra',
                        'effort': 'high',
                        'source_id': 'artificial_analysis',
                        'source_name': 'GPT-6 Astra (High)',
                        'source_slug': 'gpt-6-astra',
                        'match_kind': 'exact',
                        'output_tps': 43.85,
                        'ttft_s': 55.88,
                        'reasoning_tokens_per_turn': 6000000,
                    },
                ],
            },
            'claude-sonnet-5-5': {
                'status': 'ok',
                'entries': [
                    {
                        'router_model': 'claude-sonnet-5-5',
                        'effort': 'low',
                        'source_id': 'artificial_analysis',
                        'source_name': 'Claude Sonnet 5.5 (Low)',
                        'source_slug': 'claude-sonnet-5-5',
                        'match_kind': 'exact',
                        'output_tps': 96.48,
                        'ttft_s': 2.43,
                    },
                    {
                        'router_model': 'claude-sonnet-5-5',
                        'effort': 'medium',
                        'source_id': 'artificial_analysis',
                        'source_name': 'Claude Sonnet 5.5 (Medium)',
                        'source_slug': 'claude-sonnet-5-5',
                        'match_kind': 'exact',
                        'output_tps': 98.18,
                        'ttft_s': 4.13,
                    },
                    {
                        'router_model': 'claude-sonnet-5-5',
                        'effort': 'high',
                        'source_id': 'artificial_analysis',
                        'source_name': 'Claude Sonnet 5.5 (High)',
                        'source_slug': 'claude-sonnet-5-5',
                        'match_kind': 'exact',
                        'output_tps': 97.68,
                        'ttft_s': 12.16,
                    },
                ],
            },
            'claude-opus-5-5': {
                'status': 'ok',
                'entries': [
                    {
                        'router_model': 'claude-opus-5-5',
                        'effort': 'low',
                        'source_id': 'artificial_analysis',
                        'source_name': 'Claude Opus 5.5 (Low)',
                        'source_slug': 'claude-opus-5-5',
                        'match_kind': 'exact',
                        'output_tps': 105.86,
                        'ttft_s': 6.34,
                    },
                    {
                        'router_model': 'claude-opus-5-5',
                        'effort': 'medium',
                        'source_id': 'artificial_analysis',
                        'source_name': 'Claude Opus 5.5 (Medium)',
                        'source_slug': 'claude-opus-5-5',
                        'match_kind': 'exact',
                        'output_tps': 107.68,
                        'ttft_s': 19.55,
                    },
                    {
                        'router_model': 'claude-opus-5-5',
                        'effort': 'high',
                        'source_id': 'artificial_analysis',
                        'source_name': 'Claude Opus 5.5 (High)',
                        'source_slug': 'claude-opus-5-5',
                        'match_kind': 'exact',
                        'output_tps': 107.41,
                        'ttft_s': 36.89,
                    },
                ],
            },
            'gemini-3.7-flash': {
                'status': 'ok',
                'entries': [
                    {
                        'router_model': 'gemini-3.7-flash',
                        'effort': 'default',
                        'source_id': 'openrouter',
                        'source_name': 'Google | google/gemini-3.7-flash-20260813',
                        'source_slug': 'google/gemini-3.7-flash-20260813',
                        'match_kind': 'exact',
                        'output_tps': 90.0,
                        'ttft_s': 2.236,
                    },
                ],
            },
            'gpt-6-pro': {
                'status': 'missing',
                'entries': [],
                'note': 'ChatGPT Pro web bridge; no public speed entry',
            },
            'grok-bot': {
                'status': 'not_applicable',
                'entries': [],
                'note': 'Web session channel; not applicable for realtime_loop',
            },
            'dot': {
                'status': 'not_applicable',
                'entries': [],
                'note': 'Async packet worker; not applicable for realtime_loop',
            },
        },
    }

    def test_schema_and_validation_require_boolean_realtime_loop(self):
        self.assertIn('realtime_loop', allocator.PROFILE_FLAGS)
        self.assertIn('realtime_loop', allocator.TASK_PROFILE_SCHEMA['properties'])
        self.assertIn('realtime_loop', allocator.TASK_PROFILE_SCHEMA['required'])
        desc = allocator.TASK_PROFILE_SCHEMA['properties']['realtime_loop']['description']
        self.assertIn('observe-act loop', desc)
        self.assertIn('False for ALL software engineering', desc)

        good = {
            'work_kind': 'computer_use', 'reasoning_depth': 1, 'code_complexity': 0, 'expected_hours': 0.5, 
            
            'needs_visual': True, 'needs_gui': True, 'local_only': True, 'realtime_loop': True,
            'wants_fast': False, 'read_only_review': False, 'multi_task': False, 'task_count': '1',
            'interaction_style': 'engineering', 'axis_demand': {'divergence': 1, 'global_rigor': 1, 'module_precision': 0, 'vision_tools': 3},
        }
        validated = allocator.validate_profile(good)
        self.assertIs(validated['realtime_loop'], True)

        for bad_val in ('yes', 1, 0, ['true']):
            with self.assertRaises(ValueError) as ctx:
                allocator.validate_profile({**good, 'realtime_loop': bad_val})
            self.assertIn('task.profile.realtime_loop must be true or false', str(ctx.exception))

        # Classifier calibration cases include >= 8 positive and >= 8 negative realtime_loop examples
        cases_path = Path(__file__).resolve().parents[1] / 'references' / 'classifier-cases.json'
        doc = json.loads(cases_path.read_text(encoding='utf-8-sig'))
        self.assertIn('realtime_loop', doc.get('dimension_rubric') or {})
        positives = [c for c in doc['calibration_cases'] if c['expected_profile'].get('realtime_loop') is True]
        negatives = [c for c in doc['calibration_cases'] if c['expected_profile'].get('realtime_loop') is False]
        self.assertGreaterEqual(len(positives), 8)
        self.assertGreaterEqual(len(negatives), 8)
        for case in doc['calibration_cases']:
            self.assertIsInstance(allocator.validate_profile(case['expected_profile'])['realtime_loop'], bool)

    def test_realtime_loop_is_classifier_and_caller_only_with_zero_keyword_heuristics(self):
        # 1. No keyword heuristics or word lists exist on allocator
        for banned_attr in (
            'infer_realtime_loop',
            'infer_realtime_gui',
            'realtime_loop_needs_gui',
            'REALTIME_LOOP_CONTINUOUS_WORDS',
            'REALTIME_LOOP_TARGET_WORDS',
            'REALTIME_LOOP_PRESSURE_WORDS',
            'REALTIME_DIRECT_EXECUTION_PHRASES',
            'REALTIME_ENGINEERING_ACTION_WORDS',
            'REALTIME_ENGINEERING_ARTIFACT_WORDS',
            'REALTIME_GUI_TARGET_WORDS',
        ):
            self.assertFalse(hasattr(allocator, banned_attr), f'Banned heuristic symbol still present: {banned_attr}')

        base_accounts = copy.deepcopy(allocator.load_accounts())
        capabilities = allocator.load_capabilities(accounts=base_accounts)
        policy = base_accounts['scoring']

        # 2. All 11 review-report false-positive prompts + 4 brief engineering prompts default to realtime_loop=False
        # when neither the classifier nor the caller sets realtime_loop=True.
        engineering_prompts = [
            '实现一个实时刷新页面的前端组件',
            '修复客户端页面跳转时的卡死问题',
            '给后台管理系统加上实时数据轮询',
            '排查浏览器页面每隔几秒自动刷新的 bug',
            '写一个盯盘看板前端页面，展示盘口深度',
            '修复行情终端里的毫秒级时间戳格式化问题',
            '打开浏览器页面立刻点击保存按钮',
            '在客户端窗口里马上切换一下夜间模式',
            '抓取网页上的实时票房数据并保存为 CSV',
            '测试选课系统的并发抢课接口性能',
            '优化直播弹幕渲染组件的内存占用',
            '实现实时刷新页面',
            '修复页面轮询逻辑',
            '设计盯盘看板前端',
            '排查客户端循环等待',
        ]
        for text in engineering_prompts:
            with self.subTest(prompt=text):
                norm = allocator.normalize_task({'objective': text}, policy, capabilities, base_accounts)
                self.assertIs(norm['realtime_loop'], False, f'normalize_task guessed realtime_loop for: {text}')

        # 3. Explicit caller / classifier values pass through; omitted defaults to False
        self.assertIs(
            allocator.normalize_task({'objective': 'task', 'realtime_loop': True}, policy, capabilities, base_accounts)['realtime_loop'],
            True,
        )
        self.assertIs(
            allocator.normalize_task({'objective': 'task', 'needs': {'realtime_loop': True}}, policy, capabilities, base_accounts)['realtime_loop'],
            True,
        )
        self.assertIs(
            allocator.normalize_task(
                {'objective': 'task', 'profile': {
                    'work_kind': 'computer_use', 'reasoning_depth': 1, 'code_complexity': 0, 'expected_hours': 0.5, 
                    
                    'needs_visual': True, 'needs_gui': True, 'local_only': True, 'realtime_loop': True,
                    'wants_fast': False, 'read_only_review': False, 'multi_task': False, 'task_count': '1',
                    'interaction_style': 'engineering',
                }},
                policy, capabilities, base_accounts,
            )['realtime_loop'],
            True,
        )

        # 4. The classifier always answers realtime_loop: a profile without it is refused, and every non-realtime
        # classifier case plans with it set to False.
        cases_path = Path(__file__).resolve().parents[1] / 'references' / 'classifier-cases.json'
        doc = json.loads(cases_path.read_text(encoding='utf-8-sig'))
        usage = self.healthy_usage()
        for case in doc['calibration_cases']:
            prof = dict(case['expected_profile'])
            if prof.get('realtime_loop') is True:
                continue
            prof_without = {k: v for k, v in prof.items() if k != 'realtime_loop'}
            task_obj = case.get('task') if isinstance(case.get('task'), dict) else case
            task_base = {
                'objective': task_obj.get('objective', ''),
                'context': task_obj.get('context', ''),
                'expected_hours': 0.5,
            }
            plan_false = allocator.plan(
                {**task_base, 'profile': {**prof_without, 'realtime_loop': False}},
                usage, now=self.now, accounts=base_accounts, capabilities=capabilities, overrides=[],
            )
            self.assertIn('choice', plan_false, case['id'])
            with self.assertRaises(ValueError):
                allocator.plan({**task_base, 'profile': prof_without}, usage, now=self.now, accounts=base_accounts,
                               capabilities=capabilities, overrides=[])

    def test_parse_aa_rsc_html_offline_fixture(self):
        import refresh_speed_data

        # Offline Next.js RSC stream fixture testing both initialModels array and $hex dereferenced model objects
        rsc_html = (
            '<html><body>'
            '<script>self.__next_f.push([1,"1a:{\\"outputSpeed\\":78.66,\\"timeToFirstAnswerToken\\":41.09,\\"outputTokens\\":2888116}\\n"])</script>'
            '<script>self.__next_f.push([1,"1b:{\\"name\\":\\"Gemini 4 Argon (High)\\",\\"slug\\":\\"gemini-4-argon\\",\\"effort\\":{\\"slug\\":\\"high\\"},\\"headline\\":\\"$1a\\"}\\n"])</script>'
            '<script>self.__next_f.push([1,"1c:{\\"initialModels\\":[{\\"name\\":\\"Claude Sonnet 5.5 (Low, Default Fallback)\\",\\"slug\\":\\"claude-sonnet-5-5\\",\\"outputSpeed\\":96.48,\\"timeToFirstAnswerToken\\":2.43,\\"reasoning_output_tokens\\":119402}]}\\n"])</script>'
            '</body></html>'
        )
        rows = refresh_speed_data.parse_aa_rsc_html(rsc_html)
        self.assertIn('gemini-4-argon', rows)
        argon = rows['gemini-4-argon']
        self.assertEqual(argon['effort'], 'high')
        self.assertAlmostEqual(argon['output_tps'], 78.66)
        self.assertAlmostEqual(argon['ttft_s'], 41.09)
        self.assertEqual(argon['reasoning_tokens_per_turn'], 2888116)

        self.assertIn('claude-sonnet-5-5', rows)
        sonnet_low = rows['claude-sonnet-5-5']
        self.assertEqual(sonnet_low['effort'], 'low')
        self.assertAlmostEqual(sonnet_low['output_tps'], 96.48)
        self.assertAlmostEqual(sonnet_low['ttft_s'], 2.43)
        self.assertEqual(sonnet_low['reasoning_tokens_per_turn'], 119402)

    def test_refresh_speed_data_and_floor_excludes_slow_argon_while_keeping_fast_models(self):
        import dispatch_registry
        import refresh_speed_data

        base_accounts = copy.deepcopy(allocator.load_accounts())
        capabilities = allocator.load_capabilities(accounts=base_accounts)
        usage = self.healthy_usage()

        # 1. Refresh script computes T_turn = ttft_s + 400 / output_tps (never adding reasoning_tokens_per_turn)
        updated_accounts, report = refresh_speed_data.apply_speed_data(base_accounts, self.SPEED_FIXTURE)
        self.assertEqual(report['updated_count'], 7)
        self.assertIn('gpt-6-pro', report['default_models'])
        self.assertEqual(sorted(report['not_applicable_models']), ['dot', 'grok-bot'])

        # Argon High: T_turn = 41.09 + 400 / 78.66 = 46.18s -> responsiveness 0.000 (< floor 0.50)
        argon_spec = updated_accounts['models']['gemini-4-argon-high']
        self.assertEqual(argon_spec['responsiveness'], 0.0)
        self.assertIn('46.18s->0.000', argon_spec['responsiveness_basis'])
        # OpenRouter fallback explicitly notes metric difference in basis
        g37_spec = updated_accounts['models']['gemini-3.7-flash']
        self.assertIn('openrouter fallback', g37_spec['responsiveness_basis'])
        self.assertIn('p50_latency', g37_spec['responsiveness_basis'])

        # Per-effort curves on multi-effort models (Sol: low 0.846 / medium 0.754 / high 0.0; Astra: low 0.777 / medium 0.719 / high 0.0)
        sol_curve = updated_accounts['effort_curves']['gpt-6.1-sol']['responsiveness']
        self.assertAlmostEqual(sol_curve['low'], 0.846, places=2)
        self.assertAlmostEqual(sol_curve['medium'], 0.754, places=2)
        self.assertEqual(sol_curve['high'], 0.0)

        # 2. Test on shipped references/accounts.json (which carries full speed-data.json numbers):
        # On a realtime_loop GUI game task (classified as computer_use + needs_gui=True, realtime_loop=True),
        # Argon High (only high) and grok-bot/dot (not_applicable) are excluded, multi-effort bundles whose configured
        # effort is high (e.g. astra_high, flash_high, grok47_high) step down to a faster supported effort that clears
        # floor=0.50, and a fast GUI-capable model wins.
        lajishouyou_profile = {
            'work_kind': 'computer_use',
            'reasoning_depth': 2, 'code_complexity': 0, 'expected_hours': 0.5, 
            'needs_visual': True, 'needs_gui': True, 'local_only': True, 'realtime_loop': True,
            'wants_fast': False, 'read_only_review': False, 'multi_task': False, 'task_count': '1',
            'interaction_style': 'engineering',
        }
        rt_plan = allocator.plan(
            {
                'objective': '你是该游戏的唯一执行子 Agent，持续在模拟器窗口里操作游戏完成日常挂机刷本',
                'expected_hours': 1.0,
                'profile': lajishouyou_profile,
            },
            usage, now=self.now, accounts=base_accounts, capabilities=capabilities, overrides=[],
        )
        by_id = {c['id']: c for c in rt_plan['ranking']}
        self.assertTrue(rt_plan['decision']['task']['realtime_loop'])
        self.assertTrue(rt_plan['decision']['task']['profile']['needs_gui'])
        self.assertTrue(rt_plan['decision']['task']['profile']['needs_visual'])

        # Argon High only offers `high` (46.18s -> 0.000) -> excluded
        self.assertTrue(any('too slow per turn for a live loop' in r for r in by_id['argon_high']['excluded']),
                        by_id['argon_high']['excluded'])
        self.assertTrue(by_id['argon_high'].get('below_floor'))

        # Multi-effort bundles step down to an effort clearing both floor=0.50 and the hard ability gate (3.1):
        # - astra_high steps down from high (0.0) to medium (0.719, ability 4.05 >= 3.1)
        self.assertEqual(by_id['astra_high']['excluded'], [])
        self.assertEqual(by_id['astra_high']['bundle']['effort'], 'medium')
        self.assertEqual(by_id['astra_high']['components']['effort_stepped_down_from'], 'high')
        # - sol_medium (0.754, ability 3.40 >= 3.1), sonnet55_medium (0.898), sonnet55_high (0.701), opus55_medium (0.530) pass directly
        self.assertEqual(by_id['sol_medium']['excluded'], [])
        self.assertEqual(by_id['sonnet55_medium']['excluded'], [])
        self.assertEqual(by_id['sonnet55_high']['excluded'], [])
        self.assertEqual(by_id['opus55_medium']['excluded'], [])
        self.assertNotEqual(rt_plan['choice'], 'argon_high')

        # A stepped-down bundle resolves cleanly in dispatch_registry when allow_realtime_stepdown=True
        stepped_bundle = dispatch_registry.resolve_bundle(
            dict(by_id['astra_high']['bundle'], bundle_id='astra_high'),
            accounts=base_accounts,
            allow_realtime_stepdown=True,
        )
        self.assertEqual(stepped_bundle['effort'], 'medium')

        # On a routine non-GUI realtime_loop task:
        # - flash38_medium (0.813 >= 0.50) passes directly, while flash_high (antigravity-resilient, effort fixed in
        #   model ID `gemini-3.8-flash-high`, responsiveness 0.49 < 0.50) is NOT allowed to step down and is excluded;
        # - grok47_high (CLI harness accepts effort) steps down to low (0.744);
        # - grok-bot / dot (not_applicable) are excluded.
        routine_rt = allocator.plan(
            {
                'objective': '持续轮询命令行行情接口，价差触及阈值时立刻调用下单命令',
                'expected_hours': 0.5,
                'needs': {'local_only': False, 'outbound_ok': True},
                'profile': {
                    'work_kind': 'tool_execution',
                    'reasoning_depth': 1, 'code_complexity': 0, 'expected_hours': 0.5, 
                    'needs_visual': False, 'needs_gui': False, 'local_only': False, 'realtime_loop': True,
                    'wants_fast': False, 'read_only_review': False, 'multi_task': False, 'task_count': '1',
                    'interaction_style': 'engineering',
                },
            },
            usage, now=self.now, accounts=base_accounts, capabilities=capabilities, overrides=[],
        )
        routine_by_id = {c['id']: c for c in routine_rt['ranking']}
        self.assertTrue(
            any('below the realtime_loop floor' in r for r in routine_by_id['flash_high']['excluded']),
            routine_by_id['flash_high']['excluded'],
        )
        # Grok's credit window has no cited capacity, so it is out of automatic routing for that data error, not for speed.
        self.assertFalse(any('too slow' in r or 'realtime_loop floor' in r for r in routine_by_id['grok47_high']['excluded']))
        self.assertEqual(routine_by_id['grok47_high']['bundle']['effort'], 'low')
        for na_id in ('grok_bot', 'dot_text'):
            if na_id in routine_by_id:
                self.assertTrue(
                    any('not_applicable' in r and 'too slow per turn for a live loop' in r
                        for r in routine_by_id[na_id]['excluded']),
                    routine_by_id[na_id]['excluded'],
                )

    def test_non_realtime_tasks_are_completely_unaffected_by_responsiveness(self):
        base_accounts = copy.deepcopy(allocator.load_accounts())
        stripped_accounts = copy.deepcopy(base_accounts)
        for spec in stripped_accounts['models'].values():
            if isinstance(spec, dict):
                spec.pop('responsiveness', None)
                spec.pop('responsiveness_basis', None)
                spec.pop('responsiveness_status', None)
        for curve in stripped_accounts.get('effort_curves', {}).values():
            if isinstance(curve, dict):
                curve.pop('responsiveness', None)
        capabilities = allocator.load_capabilities(accounts=base_accounts)
        usage = self.healthy_usage()

        non_rt_tasks = [
            {
                'objective': 'Implement the retry backoff module and unit tests',
                'expected_hours': 0.5,
                'profile': {
                    'work_kind': 'general_implementation',
                    'reasoning_depth': 2, 'code_complexity': 2, 'expected_hours': 0.5, 
                    'needs_visual': False, 'needs_gui': False, 'local_only': True, 'realtime_loop': False,
                    'wants_fast': False, 'read_only_review': False, 'multi_task': False, 'task_count': '1',
                    'interaction_style': 'engineering',
                },
            },
            {
                'objective': '修复游戏挂机脚本 `auto_farm.py` 里轮询窗口卡死的 bug',
                'expected_hours': 0.5,
                'profile': {
                    'work_kind': 'debug_review',
                    'reasoning_depth': 2, 'code_complexity': 2, 'expected_hours': 0.5, 
                    'needs_visual': False, 'needs_gui': False, 'local_only': True, 'realtime_loop': False,
                    'wants_fast': False, 'read_only_review': False, 'multi_task': False, 'task_count': '1',
                    'interaction_style': 'engineering',
                },
            },
        ]
        for task in non_rt_tasks:
            before = allocator.plan(task, usage, now=self.now, accounts=stripped_accounts, capabilities=capabilities, overrides=[])
            after = allocator.plan(task, usage, now=self.now, accounts=base_accounts, capabilities=capabilities, overrides=[])
            self.assertEqual(before['choice'], after['choice'])
            before_scores = {c['id']: (c['score'], c['excluded'], c['components'].get('fit')) for c in before['ranking']}
            after_scores = {c['id']: (c['score'], c['excluded'], c['components'].get('fit')) for c in after['ranking']}
            self.assertEqual(before_scores, after_scores)
            for c in after['ranking']:
                self.assertNotIn('responsiveness', c['components'])

    def test_zero_local_data_fresh_install_scores_realtime_with_the_default_responsiveness(self):
        with tempfile.TemporaryDirectory() as empty_state:
            with patch.dict(os.environ, {'ROUTER_STATE_DIR': empty_state}):
                fresh_accounts = copy.deepcopy(allocator.load_accounts())
                # Simulate an unmeasured model on codex (`astra_high` without speed data) to test conservative default
                del fresh_accounts['models']['gpt-6-astra']['responsiveness']
                del fresh_accounts['effort_curves']['gpt-6-astra']['responsiveness']
                capabilities = allocator.load_capabilities(accounts=fresh_accounts)
                usage = self.healthy_usage()

                # `gpt-6-astra` without speed data gets conservative default (0.52 >= floor 0.50):
                # not excluded, marked `responsiveness: default`, and ranked behind measured fast models.
                rt_task = {
                    'objective': '守着票务网页等放票立即点击抢票',
                    'expected_hours': 0.5,
                    'profile': {
                        'work_kind': 'computer_use',
                        'reasoning_depth': 1, 'code_complexity': 0, 'expected_hours': 0.5, 
                        'needs_visual': True, 'needs_gui': True, 'local_only': True, 'realtime_loop': True,
                        'wants_fast': False, 'read_only_review': False, 'multi_task': False, 'task_count': '1',
                        'interaction_style': 'engineering',
                    },
                }
                res = allocator.plan(rt_task, usage, now=self.now, accounts=fresh_accounts, capabilities=capabilities, overrides=[])
                by_id = {c['id']: c for c in res['ranking']}
                self.assertEqual(by_id['astra_high']['excluded'], [])
                self.assertEqual(by_id['astra_high']['components']['responsiveness_source'], 'default')
                self.assertAlmostEqual(by_id['astra_high']['components']['responsiveness'],
                                       fresh_accounts['scoring']['responsiveness']['default'])
                self.assertIn('responsiveness: default', by_id['astra_high']['notes'])

                # When an unmeasured model is selected (e.g. only eligible candidate), decision marks `responsiveness: default`
                other_ids = [cid for cid in by_id if cid != 'astra_high']
                pinned = allocator.plan(
                    rt_task, usage, now=self.now, accounts=fresh_accounts, capabilities=capabilities,
                    unavailable=other_ids, overrides=[],
                )
                self.assertEqual(pinned['choice'], 'astra_high')
                self.assertEqual(pinned['decision'].get('responsiveness'), 'default')
                self.assertIn('responsiveness: default', pinned['decision'].get('reason', ''))

                # Optional local speed telemetry shrinkage only activates at >= min_samples (20) and is bounded
                telem_path = Path(empty_state) / 'speed-telemetry.json'
                telem_path.write_text(json.dumps({
                    'models': {'gpt-6.1-sol': {'samples': 5, 'median_turn_seconds': 35.0}}
                }), encoding='utf-8')
                low_sample_val, low_src = allocator.bundle_responsiveness(
                    {'id': 'sol_medium', 'account': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'medium'},
                    fresh_accounts['models']['gpt-6.1-sol'],
                    fresh_accounts['scoring'],
                    fresh_accounts['effort_curves']['gpt-6.1-sol'],
                    state_dir=empty_state,
                )
                self.assertEqual(low_src, 'measured')

                telem_path.write_text(json.dumps({
                    'models': {'gpt-6.1-sol': {'samples': 40, 'median_turn_seconds': 35.0}}
                }), encoding='utf-8')
                blended_val, blended_src = allocator.bundle_responsiveness(
                    {'id': 'sol_medium', 'account': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'medium'},
                    fresh_accounts['models']['gpt-6.1-sol'],
                    fresh_accounts['scoring'],
                    fresh_accounts['effort_curves']['gpt-6.1-sol'],
                    state_dir=empty_state,
                )
                self.assertEqual(blended_src, 'local_blended')
                self.assertLess(blended_val, low_sample_val)
                self.assertGreaterEqual(low_sample_val - blended_val, 0.01)
                self.assertLessEqual(low_sample_val - blended_val,
                                     fresh_accounts['scoring']['responsiveness']['local_telemetry']['max_shift'] + 1e-6)

    def test_realtime_effort_step_down_respects_dispatch_efforts_and_fixed_effort_harnesses(self):
        import dispatch_registry

        accounts = copy.deepcopy(allocator.load_accounts())
        policy = accounts['scoring']
        bundles_map = allocator.bundles(accounts)

        # 1. antigravity-resilient bundle (`flash_high`, model `gemini-3.8-flash-high` with effort in model ID,
        # responsiveness 0.49 < floor 0.50) does not accept an effort flag at launch and must NEVER step down.
        b_flash_high = dict(bundles_map['flash_high'], id='flash_high')
        eff, val, src, stepped_from = allocator.resolve_realtime_effort(
            b_flash_high, accounts['models']['gemini-3.8-flash-high'], policy, accounts,
            all_bundles=bundles_map,
        )
        self.assertEqual((eff, stepped_from), ('high', None))
        self.assertAlmostEqual(val, 0.486, places=2)

        # 2. Step-down only considers efforts present in dispatch_efforts[provider][model] and effort_curves[model]['ability']
        accounts_restricted = copy.deepcopy(accounts)
        accounts_restricted['dispatch_efforts']['codex']['gpt-6-astra'] = ['high']
        b_astra = dict(bundles_map['astra_high'], id='astra_high')
        eff_r, _, _, stepped_r = allocator.resolve_realtime_effort(
            b_astra, accounts_restricted['models']['gpt-6-astra'], policy, accounts_restricted,
            all_bundles={'astra_high': b_astra},
        )
        self.assertEqual((eff_r, stepped_r), ('high', None))

        # 3. dispatch_registry.resolve_bundle rejects tampered non-realtime effort changes, rejects unsupported
        # dispatch_efforts, rejects antigravity-resilient step-down, and permits valid realtime step-down.
        tampered = dict(bundles_map['astra_high'], bundle_id='astra_high', effort='medium')
        self.assertIsNone(dispatch_registry.resolve_bundle(tampered, accounts=accounts, explicit_override=False))
        self.assertIsNone(dispatch_registry.resolve_bundle(
            dict(bundles_map['astra_high'], bundle_id='astra_high', effort='minimal', realtime_stepped_from='high'),
            accounts=accounts, explicit_override=False, allow_realtime_stepdown=True,
        ))
        self.assertIsNone(dispatch_registry.resolve_bundle(
            dict(bundles_map['flash_high'], bundle_id='flash_high', effort='medium', realtime_stepped_from='high'),
            accounts=accounts, explicit_override=False, allow_realtime_stepdown=True,
        ))
        valid_stepdown = dispatch_registry.resolve_bundle(
            dict(bundles_map['astra_high'], bundle_id='astra_high', effort='medium', realtime_stepped_from='high'),
            accounts=accounts, explicit_override=False, allow_realtime_stepdown=True,
        )
        self.assertIsNotNone(valid_stepdown)
        self.assertEqual(valid_stepdown['effort'], 'medium')

    def test_realtime_sibling_deference_requires_sibling_to_clear_the_responsiveness_floor(self):
        accounts = copy.deepcopy(allocator.load_accounts())
        bundles_map = allocator.bundles(accounts)
        non_astra_ids = [bid for bid in bundles_map if not bid.startswith('astra_')]
        capabilities = allocator.load_capabilities(accounts=accounts)
        usage = self.healthy_usage()

        # On a coupled + critical realtime_loop task (gate = 3.6 + 0.3 = 3.9):
        # - `astra_low` has responsiveness 0.777 >= 0.50, but ability 3.61 < 3.9 (fails ability gate)
        # - `astra_high` has high responsiveness 0.0 < 0.50, and must NOT defer to `astra_low`;
        #   instead it steps down to `medium` (ability 4.05 >= 3.9, responsiveness 0.719 >= 0.50).
        res = allocator.plan(
            {
                'objective': '实时循环任务',
                'difficulty': 'coupled',
                'needs': {'critical': True},
                'realtime_loop': True,
            },
            usage, now=self.now, unavailable=non_astra_ids, accounts=accounts, capabilities=capabilities, overrides=[],
        )
        self.assertEqual(res['choice'], 'astra_high')
        by_id = {c['id']: c for c in res['ranking']}
        self.assertEqual(by_id['astra_high']['excluded'], [])
        self.assertEqual(by_id['astra_high']['bundle']['effort'], 'medium')
        self.assertEqual(by_id['astra_high']['components']['effort_stepped_down_from'], 'high')

    def test_realtime_loop_does_not_auto_promote_gui_visual_or_local_only(self):
        accounts = copy.deepcopy(allocator.load_accounts())
        capabilities = allocator.load_capabilities(accounts=accounts)
        policy = accounts['scoring']

        norm_top = allocator.normalize_task(
            {
                'objective': '连接本地交易所 WebSocket 行情流持续盯盘，价差触发时用 REST API 下单',
                'work_kind': 'tool_execution',
                'realtime_loop': True,
                'needs_gui': False,
                'needs_visual': False,
                'local_only': False,
            },
            policy, capabilities, accounts,
        )
        self.assertTrue(norm_top['realtime_loop'])
        self.assertFalse(norm_top['needs_gui'])
        self.assertFalse(norm_top['needs_visual'])
        self.assertFalse(norm_top['local_only'])

        norm_prof = allocator.normalize_task(
            {
                'objective': '连接交易所 WebSocket 行情流持续盯盘，价差触发时用 REST API 下单',
                'profile': {
                    'work_kind': 'tool_execution',
                    'reasoning_depth': 2, 'code_complexity': 0, 'expected_hours': 0.5, 
                    'needs_visual': False, 'needs_gui': False, 'local_only': False, 'realtime_loop': True,
                    'wants_fast': False, 'read_only_review': False, 'multi_task': False, 'task_count': '1',
                    'interaction_style': 'engineering',
                },
            },
            policy, capabilities, accounts,
        )
        self.assertTrue(norm_prof['realtime_loop'])
        self.assertFalse(norm_prof['needs_gui'])
        self.assertFalse(norm_prof['needs_visual'])
        self.assertFalse(norm_prof['local_only'])

    def test_discovered_family_and_date_variants_do_not_inherit_single_effort_measured_responsiveness(self):
        import auto_bundles

        accounts = copy.deepcopy(allocator.load_accounts())
        policy = accounts['scoring']
        # A family guess has no measured speed of its own.
        for variant_id in ('claude-opus-5-6', 'gpt-6-experimental', 'gemini-2.5-pro'):
            with self.subTest(variant=variant_id):
                prof = auto_bundles.default_profile(variant_id, accounts)
                self.assertEqual(prof['source'], 'family')    # no row of its own, so no measured speed
                b = {'id': f'auto_{variant_id}', 'account': 'claude', 'model': variant_id, 'effort': 'medium'}
                val, src = allocator.bundle_responsiveness(b, prof, accounts)
                self.assertEqual(src, 'default')
                self.assertAlmostEqual(val, policy['responsiveness']['default'])
                self.assertGreaterEqual(val, policy['responsiveness']['floor'])
        # A date or -latest/-preview spelling is the model itself, so its measured curve answers (R1 `alias`).
        for variant_id, exact_id in (('claude-opus-5-5-20260901', 'claude-opus-5-5'), ('gpt-6.1-sol-20260901', 'gpt-6.1-sol'),
                                     ('gpt-6-astra-preview', 'gpt-6-astra'), ('grok-4.7-latest', 'grok-4.7')):
            with self.subTest(variant=variant_id):
                prof = auto_bundles.default_profile(variant_id, accounts)
                self.assertEqual(prof['source'], 'calibrated')
                self.assertEqual(
                    allocator.bundle_responsiveness({'model': variant_id, 'effort': 'medium'}, prof, accounts),
                    allocator.bundle_responsiveness({'model': exact_id, 'effort': 'medium'}, prof, accounts))

    def test_issue1_resolve_bundle_requires_realtime_flag_a_qualifying_effort_and_survives_disabled_sibling(self):
        import dispatch_registry
        import router_test_support

        accounts = copy.deepcopy(allocator.load_accounts())
        bundles_map = allocator.bundles(accounts)

        # 1a. Forged `realtime_stepped_from` on a non-realtime call (`allow_realtime_stepdown=False`) is rejected.
        forged_non_rt = dict(bundles_map['astra_high'], id='astra_high', bundle_id='astra_high',
                             effort='medium', realtime_stepped_from='high')
        self.assertIsNone(dispatch_registry.resolve_bundle(
            forged_non_rt, accounts, explicit_override=False, allow_realtime_stepdown=False,
        ))

        # 1b. The plan chooses among qualifying efforts by score, so a lower qualifying one stands; one below the floor does not.
        lower_qualifying = dict(bundles_map['sol_high'], id='sol_high', bundle_id='sol_high',
                                effort='low', realtime_stepped_from='high')
        self.assertEqual(dispatch_registry.resolve_bundle(
            lower_qualifying, accounts, explicit_override=False, allow_realtime_stepdown=True,
        )['effort'], 'low')
        too_slow = copy.deepcopy(accounts)
        too_slow['effort_curves'][bundles_map['sol_high']['model']]['responsiveness']['low'] = 0.1
        self.assertIsNone(dispatch_registry.resolve_bundle(
            lower_qualifying, too_slow, explicit_override=False, allow_realtime_stepdown=True,
        ))
        valid_sol = dict(bundles_map['sol_high'], id='sol_high', bundle_id='sol_high',
                         effort='medium', realtime_stepped_from='high')
        resolved_sol = dispatch_registry.resolve_bundle(
            valid_sol, accounts, explicit_override=False, allow_realtime_stepdown=True,
        )
        self.assertIsNotNone(resolved_sol)
        self.assertEqual(resolved_sol['bundle_id'], 'sol_high')
        self.assertEqual(resolved_sol['effort'], 'medium')

        # 1c. Explicit caller `realtime_loop=False` overrides `profile['realtime_loop']=True` in `normalize_task`.
        prof_rt = {
            'work_kind': 'computer_use', 'reasoning_depth': 2, 'code_complexity': 0, 'expected_hours': 0.5, 'needs_visual': True, 'needs_gui': True, 'local_only': True,
            'realtime_loop': True, 'wants_fast': False, 'read_only_review': False,
            'multi_task': False, 'task_count': '1', 'interaction_style': 'engineering',
        }
        norm = allocator.normalize_task(
            {'objective': 'implement polling', 'realtime_loop': False, 'profile': prof_rt},
            accounts['scoring'], allocator.load_capabilities(accounts=accounts), accounts,
        )
        self.assertFalse(norm['realtime_loop'])
        self.assertFalse(norm['profile']['realtime_loop'])

        # 1d. When `opus55_medium` is disabled in model management, `opus55_high` steps past it to `low`, and
        # `resolve_bundle`, `issue()`, and `prepare()` keep that effort on `bundle_id='opus55_high'`.
        acc_disabled_sib = copy.deepcopy(accounts)
        for k, v in acc_disabled_sib['accounts'].items():
            if isinstance(v, dict):
                v['verified'] = (k == 'claude')
        for bid, b in acc_disabled_sib['accounts']['claude']['bundles'].items():
            if bid != 'opus55_high':
                b['disabled'] = True
        caps = allocator.load_capabilities(accounts=acc_disabled_sib)
        usage = router_test_support.usage_windows(
            self.now, {'claude': [('five_hour', 90, 3), ('seven_day', 80, 100), ('weekly_fable', 100, 100)]}
        )
        task = {
            'objective': 'Play game live', 'parent_agent_id': 'p1', 'task_id': 't_opus_stepdown',
            'difficulty': 'hard', 'realtime_loop': True,
        }
        orig_hp = allocator.host_policy.load_policy
        orig_la = allocator.load_accounts
        try:
            allocator.host_policy.load_policy = lambda *a, **kw: None
            allocator.load_accounts = lambda *a, **kw: acc_disabled_sib
            plan_res = allocator.plan({**task, 'disable_fast': True}, usage, now=self.now, accounts=acc_disabled_sib, capabilities=caps, overrides=[])
            self.assertEqual(plan_res['choice'], 'opus55_high')
            self.assertEqual(plan_res['selected_bundle']['effort'], 'low')
            with tempfile.TemporaryDirectory() as td:
                db = os.path.join(td, 'reg.sqlite')
                issued = dispatch_registry.issue(plan_res, task, now=self.now, path=db, allocation_task=task)
                prepared = dispatch_registry.prepare(
                    issued['decision_id'], task, {'title': 'rt', 'initialPrompt': 'go'},
                    usage, accounts=acc_disabled_sib, now=self.now, path=db,
                )
                self.assertEqual(prepared['bundle']['bundle_id'], 'opus55_high')
                self.assertEqual(prepared['bundle']['effort'], 'low')
                self.assertFalse(prepared['bundle'].get('disabled'))
        finally:
            allocator.host_policy.load_policy = orig_hp
            allocator.load_accounts = orig_la

    def test_issue2_pinned_and_user_override_preserve_chosen_model_and_effort_with_warning(self):
        import dispatch_registry
        import router_test_support

        accounts = copy.deepcopy(allocator.load_accounts())
        for k, v in accounts['accounts'].items():
            if isinstance(v, dict):
                v['verified'] = True
        caps = allocator.load_capabilities(accounts=accounts)
        bundles_map = allocator.bundles(accounts)
        usage = self.healthy_usage()

        # 2a. `pinned_bundle='opus55_high'` on a `realtime_loop=True` task keeps `effort='high'` and records a warning.
        pinned_plan = allocator.plan(
            {'objective': 'Play game live', 'difficulty': 'routine', 'realtime_loop': True, 'pinned_bundle': 'opus55_high'},
            usage, now=self.now, accounts=accounts, capabilities=caps, overrides=[],
        )
        self.assertEqual(pinned_plan['choice'], 'opus55_high')
        self.assertEqual(pinned_plan['selected_bundle']['effort'], 'high')
        self.assertTrue(any('responsiveness' in w for w in pinned_plan['decision'].get('warnings', [])),
                        pinned_plan['decision'].get('warnings'))

        # 2b. Explicit model constraint on a slow-only model (`gemini-4-argon-high`) is not excluded by responsiveness floor.
        constrained_plan = allocator.plan(
            {'objective': 'Play game live', 'difficulty': 'routine', 'realtime_loop': True,
             'constraints': {'model': 'gemini-4-argon-high'}},
            usage, now=self.now, accounts=accounts, capabilities=caps, overrides=[],
        )
        self.assertEqual(constrained_plan['choice'], 'argon_high')
        self.assertEqual(constrained_plan['selected_bundle']['effort'], 'high')
        self.assertTrue(any('responsiveness' in w for w in constrained_plan['decision'].get('warnings', [])),
                        constrained_plan['decision'].get('warnings'))

        # 2c. `record --constraint user_override` with `astra_high` (`effort='high'`) or `argon_high` (`effort='high'`)
        # succeeds through `dispatch_registry.issue()` + `prepare()` without crashing.
        usage_prep = router_test_support.usage_windows(self.now, {
            'codex': [('usage', 80, 100)],
            'antigravity': [('gemini-5h', 90, 3), ('gemini-weekly', 80, 100), ('3p-5h', 90, 3), ('3p-weekly', 80, 100)],
        })
        orig_hp = allocator.host_policy.load_policy
        orig_la = allocator.load_accounts
        try:
            allocator.host_policy.load_policy = lambda *a, **kw: None
            allocator.load_accounts = lambda *a, **kw: accounts
            for bid in ('astra_high', 'argon_high'):
                with self.subTest(bundle_id=bid), tempfile.TemporaryDirectory() as td:
                    db = os.path.join(td, 'reg.sqlite')
                    task = {'objective': 'Play game live', 'parent_agent_id': 'p1', 'task_id': f't_{bid}', 'realtime_loop': True}
                    rec = {
                        'stage': 'record', 'choice': 'constraint', 'constraint': 'user_override',
                        'basis': f'User explicitly requested {bid}',
                        'selected_bundle': dict(bundles_map[bid], id=bid, bundle_id=bid, effort='high'),
                        'decision': {'action': 'dispatch', 'needs_user_decision': False, 'task': {'realtime_loop': True}},
                    }
                    issued = dispatch_registry.issue(rec, task, now=self.now, path=db, allocation_task=task)
                    prepared = dispatch_registry.prepare(
                        issued['decision_id'], task, {'title': 'rt', 'initialPrompt': 'go'},
                        usage_prep, accounts=accounts, now=self.now, path=db,
                    )
                    self.assertEqual(prepared['bundle']['bundle_id'], bid)
                    self.assertEqual(prepared['bundle']['effort'], 'high')
        finally:
            allocator.host_policy.load_policy = orig_hp
            allocator.load_accounts = orig_la

    def test_issue3_realtime_sibling_deference_ignores_unavailable_and_cooling_down_siblings(self):
        accounts = copy.deepcopy(allocator.load_accounts())
        for v in accounts['accounts'].values():
            if isinstance(v, dict):
                v['verified'] = True
        caps = allocator.load_capabilities(accounts=accounts)
        bundles_map = allocator.bundles(accounts)
        usage = self.healthy_usage()

        orig_hp = allocator.host_policy.load_policy
        try:
            allocator.host_policy.load_policy = lambda *a, **kw: None
            # When all lower-effort siblings are in `unavailable`, the high/xhigh bundle steps down instead of deferring.
            for target_id in ('sol_high', 'opus55_high', 'opus55_xhigh', 'astra_xhigh'):
                with self.subTest(target_id=target_id):
                    unavail = [bid for bid in bundles_map if bid != target_id]
                    res = allocator.plan(
                        {'objective': 'Play game live', 'difficulty': 'hard', 'realtime_loop': True},
                        usage, now=self.now, unavailable=unavail, accounts=accounts, capabilities=caps, overrides=[],
                    )
                    cand = next(c for c in res['ranking'] if c['id'] == target_id)
                    self.assertEqual(res['choice'], target_id)
                    self.assertEqual(cand['excluded'], [])
                    self.assertEqual(cand['bundle']['effort'], 'medium')

            # When a lower-effort sibling is in `launch_failures` cooldown, `resolve_realtime_effort` ignores it and steps down.
            custom_bundles = {
                'opus55_medium': dict(bundles_map['opus55_medium'], id='opus55_medium', model='claude-opus-5-5-medium'),
                'opus55_high': dict(bundles_map['opus55_high'], id='opus55_high', model='claude-opus-5-5'),
            }
            accounts_alias = copy.deepcopy(accounts)
            accounts_alias.setdefault('aliases', {})['claude-opus-5-5-medium'] = 'claude-opus-5-5'
            rt_ctx = {
                'accounts': accounts_alias,
                'policy': accounts_alias['scoring'],
                'bundles': custom_bundles,
                'unavailable': set(),
                'now': self.now,
                'launch_failures': {
                    'claude/claude-opus-5-5-medium': {
                        'failure_class': 'host_creation_failed',
                        'streak': 1,
                        'until': '2099-01-01T00:00:00Z',
                    }
                },
            }
            eff, resp_val, resp_src, stepped_from = allocator.resolve_realtime_effort(
                custom_bundles['opus55_high'],
                {'realtime_loop': True},
                rt_ctx,
                'claude-opus-5-5',
                info=accounts_alias['models']['claude-opus-5-5'],
                policy=accounts_alias['scoring'],
                curves=accounts_alias['effort_curves']['claude-opus-5-5'],
            )
            self.assertEqual((eff, stepped_from), ('medium', 'high'))
            self.assertGreaterEqual(resp_val, 0.5)
        finally:
            allocator.host_policy.load_policy = orig_hp

    def test_issue4_infer_task_profile_local_only_matches_origin_main_without_gui_expansion(self):
        caps = allocator.load_capabilities()
        accounts = allocator.load_accounts()
        policy = accounts['scoring']
        for task in (
            {'objective': 'Click the Save button in the dialog'},
            {'work_kind': 'computer_use', 'objective': 'Operate the application'},
            {'work_kind': 'other_or_mixed', 'objective': 'Take a screenshot of the result'},
            {'objective': 'Design a browser dashboard UI layout in React'},
        ):
            with self.subTest(task=task):
                norm = allocator.normalize_task(task, policy, caps, accounts)
                self.assertFalse(norm['local_only'])


if __name__ == '__main__':
    unittest.main()
