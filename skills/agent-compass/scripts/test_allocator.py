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

import pytest

import allocator
import account_fleet
import accounts_override
import axis_scores
import ledger_snapshot
import quota_overrides
import quota_pools
import task_intake
import worker_sessions
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
_real_load_accounts = account_fleet.load_accounts


def reference_capabilities():
    return account_fleet.load_capabilities(accounts=account_fleet.load_accounts(account_fleet.reference_fleet_path()))


def _load_accounts_with_expiring_subscription(path=None):
    data = _real_load_accounts(path)
    if path is None:
        data['accounts']['grok'] = json.loads(json.dumps(EXPIRING_SUBSCRIPTION))
    return data


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup)
    fixture = patch.object(account_fleet, 'load_accounts', _load_accounts_with_expiring_subscription)
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

    def test_legacy_disabled_flag_excludes_only_affected_account(self):
        accounts = account_fleet.load_accounts(account_fleet.reference_fleet_path())
        accounts_override.apply(accounts, {'schema_version': 2, 'accounts': {'codex': {'verified': False}}})
        result = self.plan({'work_kind': 'general_implementation', 'difficulty': 'hard', 'expected_hours': 0.5}, accounts=accounts, overrides=[])
        rows = result['ranking']
        affected = [row for row in rows if row['bundle']['account'] == 'codex']
        others = [row for row in rows if row['bundle']['account'] != 'codex']
        assert affected and others
        assert all(any('legacy disabled flag remains' in reason and 'tray' in reason for reason in row['excluded']) for row in affected)
        assert all(not any('legacy disabled flag remains' in reason for reason in row['excluded']) for row in others)

    def chain(self, bundle='sol_xhigh', coupling='high', total=6, tokens=120000, compacted=None, compactions=None, **extra):
        worker = {'bundle': bundle, 'context_tokens': tokens,
                  **({'compacted_tokens': compacted} if compacted is not None else {}),
                  **({'compactions': compactions} if compactions is not None else {})}
        return {'id': 'refactor-1', 'coupling': coupling, 'total_hours': total, 'worker': worker, **extra}

    def profile(self, **fields):
        access = {'access': 'read-only'} if fields.get('read_only_review') else {}
        return profile(**{'local_only': True, **access, **fields})

    def unsubscribed(self, account):
        accounts = account_fleet.load_accounts()
        accounts['accounts'].pop(account)
        return accounts

    def codex_bundles(self):
        return [b for b, v in account_fleet.bundles(account_fleet.load_accounts()).items() if v['account'] == 'codex']

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
            self.assertEqual(worker_sessions.session_state('a1', home), {'compactions': 2, 'compacted_tokens': 490000, 'context_tokens': 120000, 'historical_context_tokens': 610000})
            self.assertEqual(worker_sessions.session_state('a2', home), {'compactions': 1, 'compacted_tokens': 970000, 'context_tokens': 500100, 'historical_context_tokens': 1470100})
            self.assertEqual(worker_sessions.session_state('a3', home), {'compactions': 3, 'compacted_tokens': 780000, 'context_tokens': 41000, 'historical_context_tokens': 821000})
            self.assertEqual(worker_sessions.session_state('a4', home), {'compactions': 1, 'compacted_tokens': 510000, 'context_tokens': 200100, 'historical_context_tokens': 710100})
            agent('a1-arch', 'codex', 's-codex-arch')
            log('.codex/archived_sessions/rollout-2026-10-20T21-21-50-s-codex-arch.jsonl', [reading(249566)])
            self.assertEqual(worker_sessions.session_state('a1-arch', home), {'compactions': 0, 'compacted_tokens': 0, 'context_tokens': 249566, 'historical_context_tokens': 249566})
            self.assertEqual(worker_sessions.session_state('missing', home)['telemetry_status'], 'unknown')

            # Historical 2M context cannot be masked by a small post-compaction compressed context_tokens
            agent('a5', 'claude', 's-claude-long-context')
            log('.claude/projects/E--PROJECTS-x/s-claude-long-context.jsonl', [
                {'type': 'system', 'subtype': 'compact_boundary', 'compactMetadata': {'preTokens': 1950000, 'postTokens': 12000, 'cumulativeDroppedTokens': 1938000}},
                {'type': 'assistant', 'message': {'usage': {'input_tokens': 100, 'cache_read_input_tokens': 80000, 'cache_creation_input_tokens': 0}}}])
            st5 = worker_sessions.session_state('a5', home)
            self.assertEqual((st5['compactions'], st5['context_tokens'], st5['historical_context_tokens']), (1, 80100, 2030100))
            orig_log = worker_sessions.session_log
            with patch.object(worker_sessions, 'session_log', side_effect=lambda aid, h=None: orig_log(aid, home)):
                rot = self.plan({'work_kind': 'general_implementation', 'difficulty': 'coupled',
                                 'chain': {'id': 'c5', 'coupling': 'high', 'worker': {'agent_id': 'a5', 'bundle': 'sol_xhigh', 'context_tokens': 80100}}})
                self.assertEqual(rot['decision']['action'], 'rotate')

    def test_harness_selection_and_invariance(self):
        bundles = account_fleet.bundles(account_fleet.load_accounts())
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
        self.assertTrue(task_intake.dot_may_take('exploration_analysis', {'local_only': False}, capabilities))
        self.assertTrue(task_intake.dot_may_take('exploration_analysis', {}, capabilities, {'local_only': False}))
        self.assertTrue(task_intake.dot_may_take('research_packet', {}, capabilities, {'local_only': False}))
        self.assertFalse(task_intake.dot_may_take('exploration_analysis', {}, capabilities, {'local_only': True}))
        self.assertTrue(task_intake.dot_may_take('exploration_analysis', {'local_only': False}, capabilities, {'local_only': True}))
        self.assertFalse(task_intake.dot_may_take('exploration_analysis', {}, capabilities))
        self.assertFalse(task_intake.dot_may_take('general_implementation', {'visual': True}, capabilities))
        for kind in ('research_packet', 'exploration_analysis'):
            opened = self.plan({'work_kind': kind, 'difficulty': 'hard', 'needs': {'local_only': False},
                                'profile': self.profile(local_only=False)})
            admitted = {c['bundle']['model'] for c in opened['ranking'] if c['score'] is not None and not c['excluded']}
            self.assertIn('dot', admitted, kind)

        # Eligibility only: Dot is admitted for these kinds; which candidate wins is not a rule (#9, deleted).
        for kind in ('web_research', 'text_in_text_out', 'self_contained_code'):
            res = self.plan({'work_kind': kind, 'difficulty': 'routine'}, self.usage(grok=[('credits', 100, 165)]))
            self.assertFalse(self.rows(res)['dot_text']['excluded'], kind)
            local = self.plan({'work_kind': kind, 'difficulty': 'routine', 'needs': {'local_only': True}})
            self.assertNotEqual(local['choice'], 'dot_text', kind)
        for difficulty in ('hard', 'coupled'):
            self.assertFalse(self.rows(self.plan({'work_kind': 'web_research', 'difficulty': difficulty}))['dot_text']['excluded'], difficulty)

    def test_short_window_deferral_and_wait_limits(self):
        usage = self.usage(claude=[('five_hour', 3, 1.0), ('seven_day', 60, 100), ('weekly_fable', 100, 100)], codex=[('usage', 1, 121)])
        task = {'work_kind': 'visual_analysis', 'difficulty': 'routine', 'expected_hours': 1, 'needs': {'visual': True}}
        no_argon = ['argon_high'] + [b for b in account_fleet.bundles(account_fleet.load_accounts()) if b.startswith(('agy_opus', 'agy_sonnet'))]
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
        # Rule: an exhausted window excludes its candidates. With every big pool spent, only claude-lab rows stay eligible
        # and the plan still routes; which claude-lab effort wins is not pinned.
        empty_plan = self.plan({'work_kind': 'exploration_analysis'}, all_big_empty)
        open_rows = [c for c in empty_plan['ranking'] if c['score'] is not None and not c['excluded']]
        self.assertTrue(open_rows)
        self.assertEqual({c['bundle']['account'] for c in open_rows}, {'claude-lab'}, [c['id'] for c in open_rows])
        self.assertNotIn(empty_plan['choice'], (None, 'none'))

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
        unstarted_5h = copy.deepcopy(all_big_empty)
        unstarted_5h['accounts']['claude-lab']['status'] = 'fresh'
        unstarted_5h['accounts']['claude-lab']['windows'][0] = {
            'id': 'five_hour', 'used_pct': 0.0, 'resets_at': None, 'seconds_until_reset': None,
            'observed_remaining_pct': 100.0, 'remaining_pct_before_reset': None}
        self.assertFalse(self.rows(self.plan(task, unstarted_5h))['lab_sonnet55_high']['excluded'])
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
        custom_acc = account_fleet.load_accounts()
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
        accounts = account_fleet.load_accounts()
        models = account_fleet.models_table(accounts)
        for bundle in account_fleet.bundles(accounts).values():
            self.assertIn(account_fleet.canonical(bundle['model'], accounts), models, bundle['id'])

    def test_heuristic_profile_and_schema_contract(self):
        capabilities = reference_capabilities()
        policy = account_fleet.load_accounts()['scoring']
        accounts = account_fleet.load_accounts()
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
                normalized = task_intake.normalize_task(task, policy, capabilities, accounts)
                # No default table: the caller's difficulty, else the one the profile derives.
                self.assertEqual(normalized['difficulty'], difficulty or normalized['profile_difficulty'], task)
        good = self.profile()
        for bad in (dict(good, reasoning_depth=5), dict(good, code_complexity=True),
                    {k: v for k, v in good.items() if k != 'axis_demand'}, {k: v for k, v in good.items() if k != 'wants_fast'},
                    dict(good, axis_demand={'divergence': 1, 'global_rigor': 1, 'module_precision': 1, 'vision_tools': 0}),
                    dict(good, needs_gui='yes'), dict(good, wants_fast='yes'), dict(good, read_only_review=1),
                    dict(good, interaction_style='coding'), 'deep'):
            with self.assertRaises(ValueError):
                task_intake.validate_profile(bad)
        schema = task_intake.TASK_PROFILE_SCHEMA
        self.assertEqual(set(schema['required']), set(schema['properties']))
        self.assertIn('wants_fast', schema['properties'])
        self.assertIn('read_only_review', schema['properties'])
        for name, prop in schema['properties'].items():
            if prop['type'] == 'integer':
                self.assertNotIn('enum', prop, name)
                self.assertEqual((prop['minimum'], prop['maximum']), task_intake.PROFILE_INTEGERS[name])
        # local_only is resolved strictly from caller needs -> profile.local_only -> route implies -> False, never from objective/cwd text.
        prof_local = task_intake.normalize_task(
            {'objective': 'Pure text', 'profile': self.profile(local_only=True)},
            policy, capabilities, accounts,
        )
        self.assertTrue(prof_local['local_only'])
        caller_false = task_intake.normalize_task(
            {'objective': 'Run pytest', 'cwd': r'D:\work\AgentCompass', 'needs': {'local_only': False},
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
        policy = account_fleet.load_accounts()['scoring']
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
                self.assertEqual(task_intake.task_kind({'objective': objective, 'profile': prof}, capabilities, policy), kind)

    def test_classified_read_only_review_profile_and_its_own_hours(self):
        ro_prof = self.profile(work_kind='debug_review', code_complexity=0, read_only_review=True)
        ro_norm = task_intake.normalize_task(
            {'objective': 'Independent review (read-only) of the authentication module', 'profile': ro_prof},
            account_fleet.load_accounts()['scoring'],
            reference_capabilities(),
            account_fleet.load_accounts(),
        )
        self.assertEqual(ro_norm['kind'], 'debug_review')
        self.assertEqual(ro_norm['profile']['code_complexity'], 0)
        self.assertTrue(ro_norm['read_only_review'])
        self.assertEqual(ro_norm['hours'], ro_prof['expected_hours'])      # the classifier's own estimate; no per-difficulty default

    def test_existing_bundles_keep_tag_pools_and_draws_override(self):
        accounts = account_fleet.load_accounts()
        flat = account_fleet.bundles(accounts)
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
                self.assertEqual(quota_pools.bundle_pools(bundle, accounts), expected)
                self.assertEqual([wid for wid, _, _ in quota_pools.applicable_windows(
                    spec, bundle['model'], {}, bundle, accounts)], expected)
                if 'draws' not in bundle:
                    self.assertEqual([wid for wid, _, _ in quota_pools.applicable_windows(
                        spec, account_fleet.canonical(bundle['model'], accounts), {})], expected)
        for bid, draws in (('flash_high', ['3p-weekly']), ('agy_opus55_high', ['3p-weekly']),
                           ('agy_opus55_high', [])):
            with self.subTest(bundle=bid, draws=draws):
                self.assertEqual(quota_pools.bundle_pools({**flat[bid], 'draws': draws}, accounts), draws)
        for family, canonical_name in (('opus', 'claude-opus-5-5'), ('sonnet', 'claude-sonnet-5-5')):
            for effort in ('low', 'medium', 'high'):
                bundle = flat[f'agy_{family}55_{effort}']
                self.assertEqual(quota_pools.bundle_pools(bundle, accounts), ['3p-5h', '3p-weekly'])
                self.assertEqual(account_fleet.canonical(bundle['model'], accounts), canonical_name)
                self.assertEqual(bundle['model'], f'{canonical_name}-{effort}')
                self.assertEqual(bundle['launch_model'], bundle['model'])
                self.assertEqual((bundle['provider'], bundle['modeId']), ('antigravity-resilient', 'full-access'))
                self.assertNotIn('disabled', bundle)
        self.assertNotIn('when', flat['agy_opus55_low'])

    def test_agy_claude_3p_reservation_launch_and_short_window(self):
        import dispatch_registry
        accounts = account_fleet.load_accounts()
        spec = accounts['accounts']['antigravity']
        lease = (self.now + timedelta(hours=2)).isoformat()
        record = {
            'stage': 'dispatch', 'dispatch_id': 'agy-opus-1', 'state': 'started', 'lease_until': lease,
            'bundle': {'account': 'antigravity', 'bundle_id': 'agy_opus55_high', 'model': 'claude-opus-5-5-high'},
            'expected_usd': 4.0,
        }
        for wid, cap in (('3p-5h', 17.6), ('3p-weekly', 88.0)):
            self.assertEqual(quota_pools.inflight_reserved_usd(
                'antigravity', spec, wid, spec['windows'][wid], {}, cap, [record], accounts, {}, self.now), 4.0)
        for wid in ('gemini-5h', 'gemini-weekly'):
            self.assertEqual(quota_pools.inflight_reserved_usd(
                'antigravity', spec, wid, spec['windows'][wid], {}, 100.0, [record], accounts, {}, self.now), 0.0)
        # Catalog draws win even when the record's model string would tag-match the Gemini pool.
        mismatched = {'stage': 'dispatch', 'dispatch_id': 'agy-opus-2', 'state': 'started', 'lease_until': lease,
                      'expected_usd': 4.0,
                      'bundle': {'account': 'antigravity', 'bundle_id': 'agy_opus55_high', 'model': 'gemini-3.8-flash-high'}}
        self.assertEqual(quota_pools.inflight_reserved_usd(
            'antigravity', spec, '3p-5h', spec['windows']['3p-5h'], {}, 17.6, [mismatched], accounts, {}, self.now), 4.0)
        self.assertEqual(quota_pools.inflight_reserved_usd(
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
            account_fleet.bundles(accounts)['agy_opus55_high'], {'title': 'agy claude', 'initialPrompt': 'fixture'})
        self.assertEqual(request['provider'], 'antigravity-resilient/claude-opus-5-5-high')
        self.assertEqual(request['settings'].get('modeId'), 'full-access')
        self.assertNotIn('thinkingOptionId', request['settings'])

        sparse = account_fleet.load_accounts()
        accounts_override.apply(sparse, {'accounts': {'antigravity': {'bundles': {
            'flash_high': {'disabled': True}}}}})
        kept = sparse['accounts']['antigravity']['bundles']
        self.assertIs(kept['flash_high'].get('disabled'), True)
        self.assertNotIn('disabled', kept['agy_opus55_high'])
        self.assertNotIn('disabled', kept['agy_sonnet55_high'])

    def test_configured_rotation_threshold_and_missing_key_errors(self):
        import dispatch_registry as registry

        base = account_fleet.load_accounts(account_fleet.reference_fleet_path())
        caps = account_fleet.load_capabilities(accounts=base)
        roomy = self.usage(codex=[('usage', 80, 100)])
        step_task = {'work_kind': 'general_implementation', 'difficulty': 'coupled', 'expected_hours': 1}

        # 1. Thresholds below 250k (permitted by TUNING_LIMITS 100k..10M) scale context burn and rotate without a 250k floor.
        accounts_150k = copy.deepcopy(base)
        accounts_150k['scoring']['rotate_after_carried_tokens'] = 150_000
        fresh = self.plan(step_task, roomy, accounts=accounts_150k, capabilities=caps)
        fresh_usd = self.rows(fresh)['sol_xhigh']['expected_usd']
        at_120k = self.plan(
            dict(step_task, chain=self.chain(total=2, tokens=120_000, compacted=0, compactions=0)),
            roomy,
            accounts=accounts_150k,
            capabilities=caps,
        )
        self.assertEqual(at_120k['decision']['action'], 'resume')
        extra = accounts_150k['scoring']['context_burn_extra_at_250k'] * (120_000 / 150_000)
        self.assertAlmostEqual(self.rows(at_120k)['sol_xhigh']['expected_usd'], fresh_usd * (1 + extra), delta=0.02)
        accounts_250k = copy.deepcopy(base)
        accounts_250k['scoring']['rotate_after_carried_tokens'] = 250_000
        at_120k_250k = self.plan(
            dict(step_task, chain=self.chain(total=2, tokens=120_000, compacted=0, compactions=0)),
            roomy,
            accounts=accounts_250k,
            capabilities=caps,
        )
        self.assertGreater(self.rows(at_120k)['sol_xhigh']['expected_usd'],
                           self.rows(at_120k_250k)['sol_xhigh']['expected_usd'] + 0.5)
        at_150k = self.plan(
            dict(step_task, chain=self.chain(total=2, tokens=90_000, compacted=60_000, compactions=1)),
            roomy,
            accounts=accounts_150k,
            capabilities=caps,
        )
        self.assertEqual(at_150k['decision']['action'], 'rotate')

        # 2. Non-default threshold flows through dispatch_registry.reusable_workers; missing/invalid values raise surfaced errors.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / 'dispatch.sqlite3'
            paseo_home = root / '.paseo'
            agents = paseo_home / 'agents' / 'ws'
            agents.mkdir(parents=True)
            (agents / 'w-reusable.json').write_text(
                json.dumps({'id': 'w-reusable', 'status': 'idle', 'labels': {'router.parent_id': 'parent-1'}}),
                encoding='utf-8',
            )
            with registry.transaction(db) as conn:
                registry.save(conn, {
                    'dispatch_id': 'disp-1', 'decision_id': 'dec-1', 'state': 'completed',
                    'agent_id': 'w-reusable', 'parent_id': 'parent-1',
                    'created_at': self.now.isoformat(), 'updated_at': self.now.isoformat(),
                    # prepare() writes this bound. reusable_workers expires first, and that read
                    # requires it on every completed Paseo row.
                    'hard_until': (self.now + timedelta(seconds=registry.MAX_LIFETIME)).isoformat(),
                    'bundle': {'bundle_id': 'sol_xhigh', 'account': 'codex', 'provider': 'codex', 'model': 'gpt-6.1-sol'},
                    'task': {'task_id': 't-1'},
                    'request': {'title': 'worker task'},
                })

            accounts_750k = copy.deepcopy(base)
            accounts_750k['scoring']['rotate_after_carried_tokens'] = 750_000
            with patch.object(account_fleet, 'load_accounts', return_value=accounts_750k):
                with patch.object(worker_sessions, 'session_state', return_value={'historical_context_tokens': 700_000, 'context_tokens': 100_000, 'compacted_tokens': 600_000}):
                    under = registry.reusable_workers('parent-1', now=self.now + timedelta(minutes=1), path=db, paseo_home=paseo_home)
                    self.assertEqual([w['worn'] for w in under], [False])
                with patch.object(worker_sessions, 'session_state', return_value={'historical_context_tokens': 800_000, 'context_tokens': 100_000, 'compacted_tokens': 700_000}):
                    over = registry.reusable_workers('parent-1', now=self.now + timedelta(minutes=1), path=db, paseo_home=paseo_home)
                    self.assertEqual([w['worn'] for w in over], [True])

            missing = copy.deepcopy(base)
            missing['scoring'].pop('rotate_after_carried_tokens', None)
            with self.assertRaises(KeyError):
                worker_sessions.rotation_threshold(missing)
            with self.assertRaises(KeyError):
                self.plan(step_task, roomy, accounts=missing, capabilities=caps)
            with patch.object(account_fleet, 'load_accounts', return_value=missing), \
                    patch.object(worker_sessions, 'session_state', return_value={'historical_context_tokens': 100_000}):
                with self.assertRaises(KeyError):
                    registry.reusable_workers('parent-1', now=self.now + timedelta(minutes=1), path=db, paseo_home=paseo_home)
            for bad_val in (0, -100, True, '750000'):
                with self.assertRaises(ValueError):
                    worker_sessions.rotation_threshold({'rotate_after_carried_tokens': bad_val})


class OverrideContractTests(unittest.TestCase):
    """Contract tests for temporary priority overrides, reset confirmation, receipts, and atomic store."""

    def setUp(self):
        self.root = temporary_root(self.addCleanup)
        self.path = self.root / 'overrides.json'
        self.now = datetime(2026, 10, 16, 9, tzinfo=timezone.utc)
        self.baseline = self.now - timedelta(hours=1)
        self.reset = self.now + timedelta(days=6)
        self.accounts = account_fleet.load_accounts(account_fleet.reference_fleet_path())
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
        self.assertTrue(quota_overrides.override_reset_landed(self.override, self.replenished(), self.accounts, self.now))
        self.assertFalse(quota_overrides.override_reset_landed(self.override, self.same_cycle(), self.accounts, self.now))
        self.assertTrue(quota_overrides.override_reset_landed(one_off, self.same_cycle(), self.accounts, self.now))
        for invalid in ({'status': 'stale'}, {'stale': True}, {'available': False}, {'account': 'codex-work'},
                        {'fetched_at': self.baseline.isoformat()}, {'observation_id': 'old'},
                        {'fetched_at': (self.now - timedelta(minutes=16)).isoformat()},
                        {'fetched_at': (self.now + timedelta(seconds=1)).isoformat()}):
            with self.subTest(invalid=invalid):
                usage = self.replenished()
                usage['accounts']['codex'].update(invalid)
                self.assertFalse(quota_overrides.override_reset_landed(self.override, usage, self.accounts, self.now))
                sc = self.same_cycle()
                sc['accounts']['codex'].update(invalid)
                self.assertFalse(quota_overrides.override_reset_landed(one_off, sc, self.accounts, self.now))

    def test_reset_receipts_and_pruning(self):
        self.path.write_text(json.dumps({'overrides': [self.override], 'unrelated': 7}))
        quota_overrides.observe_landed_overrides({'priority': self.override}, self.path, self.now)
        first = json.loads(self.path.read_text())
        marked = first['overrides'][0]
        self.assertEqual(marked['reset_confirmed_at'], self.now.isoformat())
        later = self.now + timedelta(hours=2)
        quota_overrides.observe_landed_overrides({'priority': marked}, self.path, later)
        self.assertEqual(json.loads(self.path.read_text()), first)
        quota_overrides.prune_landed_overrides({'priority': marked}, self.path, later)
        final = json.loads(self.path.read_text())
        self.assertEqual((final['overrides'], final['last_reset']['at'], final['unrelated']), ([], self.now.isoformat(), 7))

        # Confirmed marker survives stale usage; future/pre-creation marker is rejected; replaced record is untouched
        stale_usage = copy.deepcopy(self.usage)
        stale_usage['accounts']['codex']['status'] = 'stale'
        self.assertTrue(quota_overrides.override_reset_landed(marked, stale_usage, self.accounts, later))
        for bad_when in (self.now + timedelta(seconds=1), self.baseline - timedelta(seconds=1)):
            self.assertFalse(quota_overrides.override_reset_landed(dict(self.override, reset_confirmed_at=bad_when.isoformat()),
                                                             self.usage, self.accounts, self.now))
        newer = dict(self.override, boost=0.4)
        self.path.write_text(json.dumps({'overrides': [newer]}))
        quota_overrides.prune_landed_overrides({'priority': self.override}, self.path, self.now)
        self.assertEqual(json.loads(self.path.read_text())['overrides'], [newer])

    @pytest.mark.slow
    def test_upsert_rebaseline_and_atomic_concurrency(self):
        with patch.object(router, 'usage_snapshot', return_value=self.usage):
            first = router.manage_overrides(self.args(), self.now)['active'][0]
            override_rows = [row for row in router.read_ledger() if row.get('stage') == 'override']
            self.assertEqual((override_rows[-1]['action'], override_rows[-1]['account'], override_rows[-1]['removed']),
                             ('upsert', 'codex', 0))
            second = router.manage_overrides(self.args(boost=0.4), self.now + timedelta(seconds=1))['active'][0]
            self.assertEqual((second['id'], second['created_at'], second['boost']), (first['id'], first['created_at'], 0.4))

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
        self.assertTrue(quota_overrides.override_depleted(self.override, self.depleted(5), self.accounts))
        self.assertFalse(quota_overrides.override_depleted(self.override, self.depleted(5.5), self.accounts))
        other = dict(self.override, id='other', account='claude', model='claude-opus-5-5')
        self.path.write_text(json.dumps({'v': 1, 'overrides': [self.override, other]}))
        _, _, applied = quota_overrides.apply_overrides([self.override], self.depleted(4), self.accounts, self.now)
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
        bundles = account_fleet.bundles(account_fleet.load_accounts(account_fleet.reference_fleet_path()))
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
            self.assertEqual(worker_sessions.session_state('agent', self.home)['context_tokens'], 123)

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
        result = worker_sessions.session_state('agent', self.home)
        self.assertEqual((result['telemetry_status'], result['compactions'], result['compacted_tokens'], result['context_tokens']),
                         ('observed', 2, 230000, 22000))
        self.assertNotIn('secret-canary', json.dumps(result))

        # Partial or missing evidence
        self.log.write_text(json.dumps({'type': 'CHECKPOINT', 'step_index': 1}) + '\n{partial')
        self.assertEqual(worker_sessions.session_state('agent', self.home)['telemetry_status'], 'partial')
        self.persistence({'conversationId': '../outside'})
        self.assertEqual(worker_sessions.session_state('agent', self.home)['telemetry_status'], 'unknown')

    def test_carried_context_triggers_handoff_rotation(self):
        self.persistence()
        self.log.write_text('\n'.join(map(json.dumps, [
            {'type': 'PLANNER_RESPONSE', 'step_index': 1, 'usage': {'input_tokens': 2100000}},
            {'type': 'CHECKPOINT', 'step_index': 2},
            {'type': 'PLANNER_RESPONSE', 'step_index': 3, 'usage': {'input_tokens': 22000}},
        ])))
        accounts = account_fleet.load_accounts(account_fleet.reference_fleet_path())
        now = datetime(2026, 10, 16, 9, tzinfo=timezone.utc)
        original_session_log = worker_sessions.session_log
        with patch.object(worker_sessions, 'session_log', side_effect=lambda agent_id, home=None: original_session_log(agent_id, self.home)):
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


class ToolOperationAndTaxonomyR2Tests(unittest.TestCase):
    """Contract tests for Taxonomy R2 `tool_operation` levels, clean deletion of the legacy responsiveness /
    realtime_loop path, continuity switching threshold, family vs alias resolution, and ledger read-side mapping."""

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

    def test_legacy_realtime_and_responsiveness_defaults_are_deleted(self):
        for banned_attr in (
            'RESPONSIVENESS_DEFAULTS',
            'bundle_responsiveness',
            'resolve_realtime_effort',
            'clearing_levels',
            'can_step_down',
            'five_axis_effort_ratio',
            'REALTIME_LOOP_QUESTION',
            'infer_realtime_loop',
        ):
            self.assertFalse(hasattr(allocator, banned_attr), f'{banned_attr} must be deleted')
        self.assertNotIn('realtime_loop', task_intake.PROFILE_FLAGS)
        self.assertNotIn('realtime_loop', task_intake.TASK_PROFILE_SCHEMA['properties'])
        self.assertNotIn('realtime_loop', task_intake.TASK_PROFILE_SCHEMA['required'])
        self.assertFalse((Path(__file__).resolve().parent / 'refresh_speed_data.py').exists())

    def test_all_calibrated_classifier_cases_validate_and_plan(self):
        base_accounts = copy.deepcopy(account_fleet.load_accounts())
        capabilities = account_fleet.load_capabilities(accounts=base_accounts)
        usage = self.healthy_usage()
        cases_path = Path(__file__).resolve().parents[1] / 'references' / 'classifier-cases.json'
        doc = json.loads(cases_path.read_text(encoding='utf-8-sig'))
        rubric_axis = (doc.get('dimension_rubric') or {}).get('axis_demand', '')
        for cat in axis_scores.FIVE_AXES:
            self.assertIn(cat, rubric_axis)
        for case in doc['calibration_cases']:
            prof = task_intake.validate_profile(dict(case['expected_profile']))
            self.assertEqual(set(prof['axis_demand']), set(axis_scores.FIVE_AXES), case['id'])
            self.assertNotIn('realtime_loop', prof, case['id'])
            task_obj = case.get('task') if isinstance(case.get('task'), dict) else case
            res = allocator.plan(
                {
                    'objective': task_obj.get('objective', ''),
                    'context': task_obj.get('context', ''),
                    'expected_hours': prof['expected_hours'],
                    'profile': prof,
                },
                usage, now=self.now, accounts=base_accounts, capabilities=capabilities, overrides=[],
            )
            self.assertIn('choice', res, case['id'])

    def test_tool_operation_levels_0_and_1_ask_for_no_ability_target(self):
        base_accounts = axis_scores.five_axis_overlay(copy.deepcopy(account_fleet.load_accounts()))
        targets = base_accounts['_five_axis']['targets']
        self.assertEqual(axis_scores.five_axis_targets({'divergence': 0, 'coding': 2, 'aesthetics': 0, 'tool_operation': 0}, targets).get('tool_operation'), None)
        self.assertEqual(axis_scores.five_axis_targets({'divergence': 0, 'coding': 2, 'aesthetics': 0, 'tool_operation': 1}, targets).get('tool_operation'), None)

        # Stripping tool_operation facet scores completely does not change choice, fit, or net score at tool_operation <= 1
        stripped = copy.deepcopy(base_accounts)
        for spec in stripped['models'].values():
            if isinstance(spec, dict) and isinstance(spec.get('five_axis'), dict):
                cats = spec['five_axis'].get('categories') or {}
                if 'tool_operation' in cats:
                    cats['tool_operation'] = {'value': None, 'by_effort': {}, 'by_level': {'2': {}, '3': {}},
                                              'status': 'missing', 'facets': {}, 'missing_facets': ['responsiveness', 'image_recognition']}
        capabilities = account_fleet.load_capabilities(accounts=base_accounts)
        usage = self.healthy_usage()
        for tool_level in (0, 1):
            task = {
                'objective': 'Implement the retry backoff module and unit tests',
                'expected_hours': 0.5,
                'profile': profile(work_kind='general_implementation', local_only=True, expected_hours=0.5,
                                   axis_demand={'divergence': 1, 'coding': 2, 'aesthetics': 0, 'tool_operation': tool_level}),
            }
            before = allocator.plan(task, usage, now=self.now, accounts=stripped, capabilities=capabilities, overrides=[])
            after = allocator.plan(task, usage, now=self.now, accounts=base_accounts, capabilities=capabilities, overrides=[])
            self.assertEqual(before['choice'], after['choice'])
            before_scores = {c['id']: (c['score'], c['excluded'], c['components'].get('fit')) for c in before['ranking']}
            after_scores = {c['id']: (c['score'], c['excluded'], c['components'].get('fit')) for c in after['ranking']}
            self.assertEqual(before_scores, after_scores)

        # Outbound models whose tool_operation is missing (dot_text, grok_bot) are NOT excluded by tool_operation at level 0 or 1
        for tool_level in (0, 1):
            outbound_task = {
                'objective': 'Gather release dates from three public SDK changelogs',
                'expected_hours': 0.3,
                'needs': {'local_only': False, 'outbound_ok': True},
                'profile': profile(work_kind='web_research', reasoning_depth=1, code_complexity=0,
                                   local_only=False, expected_hours=0.3,
                                   axis_demand={'divergence': 1, 'coding': 0, 'aesthetics': 0, 'tool_operation': tool_level}),
            }
            planned_out = allocator.plan(outbound_task, usage, now=self.now, accounts=base_accounts,
                                         capabilities=capabilities, overrides=[])
            by_id = {c['id']: c for c in planned_out['ranking']}
            self.assertEqual(by_id['dot_text']['excluded'], [])
            self.assertIsNotNone(by_id['dot_text']['score'])
            if 'grok_bot' in by_id:
                self.assertFalse(any('tool_operation' in r for r in by_id['grok_bot']['excluded']), by_id['grok_bot']['excluded'])

    def test_tool_operation_level_2_scores_per_effort_responsiveness_and_excludes_unmeasured(self):
        base_accounts = copy.deepcopy(account_fleet.load_accounts())
        capabilities = account_fleet.load_capabilities(accounts=base_accounts)
        usage = self.healthy_usage()

        live_cli_task = {
            'objective': '持续轮询命令行行情接口，价差触及阈值时立刻调用下单命令',
            'expected_hours': 0.5,
            'needs': {'local_only': False, 'outbound_ok': True},
            'profile': profile(
                work_kind='tool_execution', reasoning_depth=1, code_complexity=0, expected_hours=0.5,
                needs_visual=False, needs_gui=False, local_only=False,
                axis_demand={'divergence': 0, 'coding': 1, 'aesthetics': 0, 'tool_operation': 2},
            ),
        }
        res = allocator.plan(live_cli_task, usage, now=self.now, accounts=base_accounts, capabilities=capabilities, overrides=[])
        by_id = {c['id']: c for c in res['ranking']}

        # Lower/medium effort rows on multi-effort models have higher responsiveness (and level-2 fit) than high
        self.assertGreater(
            by_id['sol_medium']['five_axis_fit']['tool_operation']['facets']['responsiveness']['score'],
            by_id['sol_high']['five_axis_fit']['tool_operation']['facets']['responsiveness']['score'],
        )
        self.assertGreater(
            by_id['opus55_medium']['five_axis_fit']['tool_operation']['facets']['responsiveness']['score'],
            by_id['opus55_high']['five_axis_fit']['tool_operation']['facets']['responsiveness']['score'],
        )
        # Slow-but-measured models (e.g. argon_high) are scored continuously, never hard-floor excluded
        self.assertEqual(by_id['argon_high']['excluded'], [])
        self.assertIsNotNone(by_id['argon_high']['score'])
        # With shipped maintainer scores in references/user-scores.json, dot_text carries maintainer responsiveness;
        # when public manual scores are empty, channels with no responsiveness measurement leave automatic routing via Mode B.
        self.assertEqual(by_id['dot_text']['excluded'], [])
        with patch.object(axis_scores, 'load_public_user_scores', return_value={'schema_version': 1, 'scores': {}}):
            res_unscored = allocator.plan(live_cli_task, usage, now=self.now, accounts=copy.deepcopy(base_accounts),
                                          capabilities=capabilities, overrides=[])
        by_id_unscored = {c['id']: c for c in res_unscored['ranking']}
        for unmeasured_id in ('dot_text', 'grok_bot'):
            if unmeasured_id in by_id_unscored:
                self.assertTrue(
                    any('no trusted data for the demanded category tool_operation' in r
                        for r in by_id_unscored[unmeasured_id]['excluded']),
                    by_id_unscored[unmeasured_id]['excluded'],
                )

    def test_tool_operation_level_3_requires_image_input_and_scores_min_of_responsiveness_and_vision(self):
        base_accounts = copy.deepcopy(account_fleet.load_accounts())
        capabilities = account_fleet.load_capabilities(accounts=base_accounts)
        usage = self.healthy_usage()

        gui_task = {
            'objective': '持续在模拟器窗口里根据画面点击完成日常任务',
            'expected_hours': 0.5,
            'profile': profile(
                work_kind='computer_use', reasoning_depth=2, code_complexity=0, expected_hours=0.5,
                needs_visual=True, needs_gui=True, local_only=False,
                axis_demand={'divergence': 1, 'coding': 1, 'aesthetics': 0, 'tool_operation': 3},
            ),
        }
        base_accounts['accounts']['codex']['bundles']['deepseek_test'] = {
            'model': 'deepseek-v4-pro', 'effort': 'high', 'provider': 'codex',
        }
        base_accounts['accounts']['codex']['bundles']['unknown_image_test'] = {
            'model': 'grok-4.7-build-fast', 'effort': 'high', 'provider': 'codex',
        }
        res = allocator.plan(gui_task, usage, now=self.now, accounts=base_accounts, capabilities=capabilities, overrides=[])
        by_id = {c['id']: c for c in res['ranking']}
        # Models with image_input == 'no' (deepseek-v4-pro) and 'unknown' (grok-4.7-build-fast) are excluded when
        # tool_operation >= 3; dot carries a dated maintainer image-input declaration (2026-11-12).
        self.assertTrue(any('does not accept image input' in r for r in by_id['deepseek_test']['excluded']),
                        by_id['deepseek_test']['excluded'])
        self.assertTrue(any('image_input modality has no cited source' in r for r in by_id['unknown_image_test']['excluded']),
                        by_id['unknown_image_test']['excluded'])
        self.assertFalse(any('image input' in r or 'image_input' in r for r in by_id['dot_text']['excluded']),
                         by_id['dot_text']['excluded'])
        # Named constraint overrides the exclusion with a surfaced note/decision alert
        pinned = allocator.plan(
            {**gui_task, 'constraints': {'model': 'claude-opus-5-5'}},
            usage, now=self.now, accounts=base_accounts, capabilities=capabilities, overrides=[],
        )
        self.assertTrue(pinned['selected_bundle']['model'].startswith('claude-opus-5-5'))

    def test_family_guesses_have_no_scores_while_date_aliases_resolve_to_calibrated_model(self):
        import auto_bundles
        accounts = copy.deepcopy(account_fleet.load_accounts())
        for variant_id in ('claude-opus-5-6', 'gpt-6-experimental', 'gemini-2.5-pro'):
            with self.subTest(variant=variant_id):
                prof = auto_bundles.default_profile(variant_id, accounts)
                self.assertEqual(prof['source'], 'family')
                self.assertEqual(account_fleet.model_spec(accounts, variant_id), {})
        for variant_id, exact_id in (('claude-opus-5-5-20260901', 'claude-opus-5-5'), ('gpt-6.1-sol-20260901', 'gpt-6.1-sol'),
                                     ('gpt-6-astra-preview', 'gpt-6-astra'), ('grok-4.7-latest', 'grok-4.7')):
            with self.subTest(variant=variant_id):
                prof = auto_bundles.default_profile(variant_id, accounts)
                self.assertEqual(prof['source'], 'calibrated')
                self.assertEqual(account_fleet.model_spec(accounts, variant_id), account_fleet.model_spec(accounts, exact_id))

    def test_high_coupling_continuity_keeps_close_worker_and_switches_past_one_demand_level(self):
        accounts = copy.deepcopy(account_fleet.load_accounts())
        accounts['accounts']['claude']['bundles']['fable52_high']['disabled'] = False
        capabilities = account_fleet.load_capabilities(accounts=accounts)
        usage = self.healthy_usage()
        # High-divergence task (divergence=3, continuity = t3 - t2 = 1.19 SD):
        # 1. `fable52_high` (divergence -0.93, within 1.19 SD of Argon +0.25 / Opus 0.0) stays when continuing (`coupling='high'`).
        close_chain = {'id': 'c-close', 'coupling': 'high', 'total_hours': 2,
                       'worker': {'bundle': 'fable52_high', 'context_tokens': 100000}}
        div_task = {
            'objective': 'Continue deep research synthesis',
            'expected_hours': 0.5,
            'profile': profile(work_kind='exploration_analysis', reasoning_depth=3, code_complexity=0,
                               access='read-only', interaction_style='exploration', local_only=True, expected_hours=0.5,
                               axis_demand={'divergence': 3, 'coding': 0, 'aesthetics': 0, 'tool_operation': 0}),
        }
        kept = allocator.plan({**div_task, 'chain': close_chain}, usage, now=self.now,
                              accounts=accounts, capabilities=capabilities, overrides=[])
        self.assertEqual((kept['choice'], kept['decision']['action']), ('fable52_high', 'resume'))

        # 2. A worker whose deficit exceeds continuity + rival cost (e.g. `sol_medium` with divergence ~ -2.1 vs `argon_high` +0.25)
        # switches to the stronger candidate instead of forcing a bad continuation.
        far_chain = {'id': 'c-far', 'coupling': 'high', 'total_hours': 2,
                     'worker': {'bundle': 'sol_medium', 'context_tokens': 100000}}
        switched = allocator.plan({**div_task, 'chain': far_chain}, usage, now=self.now,
                                  accounts=accounts, capabilities=capabilities, overrides=[])
        self.assertNotEqual(switched['choice'], 'sol_medium')
        self.assertGreater(
            next(c for c in switched['ranking'] if c['id'] == switched['choice'])['score'],
            next(c for c in switched['ranking'] if c['id'] == 'sol_medium')['score'],
        )

    def test_normalize_task_local_only_matches_origin_main_without_gui_expansion(self):
        caps = account_fleet.load_capabilities()
        accounts = account_fleet.load_accounts()
        policy = accounts['scoring']
        for task in (
            {'objective': 'Click the Save button in the dialog'},
            {'work_kind': 'computer_use', 'objective': 'Operate the application'},
            {'work_kind': 'other_or_mixed', 'objective': 'Take a screenshot of the result'},
            {'objective': 'Design a browser dashboard UI layout in React'},
        ):
            with self.subTest(task=task):
                norm = task_intake.normalize_task(task, policy, caps, accounts)
                self.assertFalse(norm['local_only'])


class ExplainPoolWordingTests(unittest.TestCase):
    """A pooled window's percentage is of one account, so a pool of N holds N x 100 when full."""

    def top(self, remaining, full):
        return {'id': 'claude-2__opus55_medium', 'action': 'start', 'score': 0.15, 'expected_pct': 10.3, 'chain_pct': 10.3,
                'bundle': {}, 'components': {'opportunity_cost': 0.01},
                'capacity': {'window': 'seven_day', 'remaining': remaining, 'full': full, 'priced': False,
                             'end_hours': 50.0, 'expires_first': False}}

    def test_pool_names_its_size(self):
        text = allocator.explain(self.top(152.0, 200.0), {})
        self.assertIn('seven_day: 152% of one account left in a pool of 2 (full = 200%)', text)

    def test_single_account_stays_plain(self):
        text = allocator.explain(self.top(27.0, 100.0), {})
        self.assertIn('seven_day: 27% left,', text)
        self.assertNotIn('pool', text)


if __name__ == '__main__':
    unittest.main()
