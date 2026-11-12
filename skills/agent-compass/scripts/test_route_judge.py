"""Contract tests for route_judge.py: usage snapshots, judge CLI & ordering, Dot probes, and System-One scoring."""
import contextlib
import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import pytest

import account_fleet
import route_judge as router
import task_intake
from router_test_support import isolated_router_state, profile, profile_for, temporary_root


_real_scorer_accounts = router.scorer_accounts


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup)
    # Scorer contract tests run every family's CLI as if its own account were signed in; ScorerAccountTests
    # covers the per-host choice and the silent skip.
    accounts = patch.object(router, 'scorer_accounts',
                            side_effect=lambda scorer_type, **_: [router.SCORER_FAMILIES.get(scorer_type)])
    accounts.start()
    unittest.addModuleCleanup(accounts.stop)


def reference_capabilities():
    return account_fleet.load_capabilities(accounts=account_fleet.load_accounts(account_fleet.reference_fleet_path()))


def live_providers():
    """A fresh, healthy Codex reading, so a plan run on the real clock has one account it can price."""
    live = datetime.now(timezone.utc)
    return {'codex': {'fetched_at': live.isoformat(), 'available': True, 'stale': False,
                      'windows': [{'id': 'usage', 'used_pct': 20, 'resets_at': (live + timedelta(days=3)).isoformat()}]}}


def dot_admitted(result):
    """Whether the plan left dot_text eligible: another account may still be the better pick."""
    row = next((c for c in result['ranking'] if c['id'] == 'dot_text'), None)
    return row is not None and not row['excluded'] and row['score'] is not None


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 4, 1, 0, tzinfo=timezone.utc)

    def row(self, used=35, age=30, reset=None, **extra):
        return {'fetched_at': (self.now - timedelta(seconds=age)).isoformat(),
                'available': True, 'stale': False,
                'windows': [{'id': 'usage', 'used_pct': used,
                             'resets_at': reset or (self.now + timedelta(hours=2)).isoformat()}],
                **extra}

    def write_cache(self, directory, providers):
        path = Path(directory) / '.agent-compass' / 'usage-cache.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'providers': providers}), encoding='utf-8')
        return path

    def snapshot(self, providers):
        with tempfile.TemporaryDirectory() as directory:
            return router.usage_snapshot(self.write_cache(directory, providers), now=self.now)

    def candidate(self, provider='codex', account='codex', **extra):
        return {'provider': provider, 'account': account, 'model': 'runtime-verified-model',
                'effort': 'high', 'quality_fit': 'Supports the required code review', **extra}

    def run_plan(self, directory, task, down=None, unavailable=(), records=(), extra=()):
        source = Path(directory) / 'in.json'
        task_with_hours = {'expected_hours': 0.5, **task}
        task_with_hours.setdefault('profile', profile_for(task_with_hours))
        source.write_text(json.dumps({'task': task_with_hours, 'unavailable': list(unavailable)}), encoding='utf-8')
        cache = self.write_cache(directory, live_providers())
        out = io.StringIO()
        with patch.object(sys, 'argv', ['route_judge.py', 'plan', '--dry-run', '--input', str(source),
                                        '--usage-cache', str(cache), *extra]), \
                patch.object(router, 'dot_unreachable', return_value=down) as probe, \
                patch.object(router, 'read_ledger', return_value=list(records)), \
                patch.object(router, 'LEDGER_WRITES', True), \
                contextlib.redirect_stdout(out):
            router.main()
        return json.loads(out.getvalue()), probe

    def test_a_multi_task_brief_returns_to_the_parent_unless_kept_whole(self):
        profile = dict(GUI_PROFILE, needs_gui=False, needs_visual=False, local_only=False, multi_task=True, task_count='3')
        task = {'objective': 'Phase 1: collect references, compare engines, design the loop', 'auto_score': True,
                'profile': profile}
        with tempfile.TemporaryDirectory() as directory:
            split, _ = self.run_plan(directory, task)
            whole, _ = self.run_plan(directory, task, extra=('--keep-whole', 'appointed coordinator of the whole phase'))
            single, _ = self.run_plan(directory, dict(task, profile=dict(profile, multi_task=False, task_count='1')))
        self.assertEqual((split['choice'], split['task_count']), ('split', '3'))
        self.assertIn('--keep-whole', split['next'])
        self.assertNotIn('selected_bundle', split)
        self.assertNotEqual(whole['choice'], 'split')
        self.assertIn('kept_whole', [a['code'] for a in whole['alerts']])
        self.assertNotEqual(single['choice'], 'split')

    def test_multi_task_fields_are_required_and_checked(self):
        self.assertEqual({k: v for k, v in task_intake.validate_profile(dict(GUI_PROFILE, multi_task=True, task_count='>3')).items()
                          if k in ('multi_task', 'task_count')}, {'multi_task': True, 'task_count': '>3'})
        self.assertEqual(task_intake.validate_profile(dict(GUI_PROFILE, multi_task=True, task_count=5))['task_count'], '>3')
        for bad in (
            dict(GUI_PROFILE, multi_task='yes', task_count='5'),
            dict(GUI_PROFILE, multi_task='yes', task_count='2'),
            dict(GUI_PROFILE, multi_task=True, task_count='5'),
            dict(GUI_PROFILE, multi_task=False, task_count='2'),
            dict(GUI_PROFILE, multi_task=True, task_count='1'),
            {k: v for k, v in GUI_PROFILE.items() if k != 'multi_task'},
            {k: v for k, v in GUI_PROFILE.items() if k != 'task_count'},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                task_intake.validate_profile(bad)
        for missing_flag in ('wants_fast', 'read_only_review'):
            with self.subTest(missing_flag=missing_flag), self.assertRaises(task_intake.InvalidCallerProfile):
                task_intake.validate_caller_profile({k: v for k, v in GUI_PROFILE.items() if k != missing_flag})
        self.assertFalse(hasattr(task_intake, 'multi_task_marks'))
        self.assertFalse(hasattr(task_intake, 'MULTI_TASK_MARK_RE'))
        checked = task_intake.validate_profile(dict(GUI_PROFILE, wants_fast=True, read_only_review=False))
        self.assertEqual((checked['wants_fast'], checked['read_only_review']), (True, False))

    def test_usage_snapshot_discovery_freshness_projection_and_window_math(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_cache(directory, {'codex': self.row()})
            missing = str(Path(directory) / 'missing.json')
            with patch.dict(router.os.environ, {'ROUTER_STATE_DIR': '', 'ROUTER_USAGE_CACHE': ''}), \
                    patch.object(router.Path, 'home', return_value=Path(directory)), \
                    patch.object(router, 'user_env', return_value=None):
                res = router.usage_snapshot(now=self.now)
            self.assertEqual((res['read_ok'], res['accounts']['codex']['status'],
                              res['accounts']['codex']['windows'][0]['remaining_pct_before_reset']), (True, 'fresh', 65))
            with patch.dict(router.os.environ, {'ROUTER_STATE_DIR': '', 'ROUTER_USAGE_CACHE': ''}), \
                    patch.object(router.Path, 'home', return_value=Path(directory)), \
                    patch.object(router, 'user_env', return_value=missing):
                fallback = router.usage_snapshot(now=self.now)
            # A named cache file is the only one read: it must not quietly fall back to another copy of the data.
            self.assertEqual((fallback['read_ok'], [a['path'] for a in fallback['attempts']]), (False, [missing]))
            self.assertFalse(router.usage_snapshot(Path(missing), now=self.now)['read_ok'])

            # Stale & idle cadence
            reset = (self.now + timedelta(days=3)).isoformat()
            stale_path = self.write_cache(directory, {
                'old': self.row(age=901), 'future': self.row(age=-30),
                'reset': self.row(reset='2026-10-04T00:30:00Z'), 'flagged': self.row(stale=True),
                'idle': self.row(age=2000, reset=reset), 'late': self.row(age=2500, reset=reset),
                'quiet': self.row(age=2000, reset=reset),
            })
            doc = json.loads(stale_path.read_text(encoding='utf-8'))
            decided = self.now.timestamp()
            doc['poll'] = {'idle': {'seconds': 1800, 'at': decided - 200},
                           'late': {'seconds': 1800, 'at': decided - 200},
                           'quiet': {'seconds': 1800, 'at': decided - 1000}}
            stale_path.write_text(json.dumps(doc), encoding='utf-8')
            accts = router.usage_snapshot(stale_path, now=self.now)['accounts']
            for k in ('old', 'future', 'reset', 'flagged', 'late', 'quiet'):
                self.assertEqual(accts[k]['status'], 'stale', k)
            self.assertEqual(accts['idle']['status'], 'fresh')

            # Secret scrubbing & malformed tolerance
            row = self.row(token='secret-canary', summary='secret-canary', reset_block={'credential': 'secret-canary'})
            row['windows'][0]['credential'] = 'secret-canary'
            sec_path = self.write_cache(directory, {'one': row, 'other': self.row()})
            sec_res = router.usage_snapshot(sec_path, {'one', 'missing'}, self.now)
            self.assertNotIn('secret-canary', json.dumps(sec_res))
            self.assertEqual(sec_res['missing_accounts'], ['missing'])
            sec_path.write_text('{invalid', encoding='utf-8')
            self.assertFalse(router.usage_snapshot(sec_path, now=self.now)['read_ok'])

        # Window math
        w = router.window_state({'used_pct': 35, 'resets_at': '2026-10-04T11:00:00+08:00'}, self.now)
        self.assertEqual((w['seconds_until_reset'], w['remaining_pct_before_reset']), (7200, 65))
        pool = router.window_state({'used_pct': 25, 'accounts': 2, 'resets_at': '2026-10-04T11:00:00+08:00'}, self.now)
        self.assertEqual((pool['used_pct'], pool['remaining_pct_before_reset'], pool['accounts']), (25, 150, 2))
        for odd in (1, 0, True, '2'):
            self.assertEqual(router.window_state({'used_pct': 0, 'accounts': odd, 'resets_at': '2026-10-04T11:00:00+08:00'},
                                                 self.now)['remaining_pct_before_reset'], 100)
        for over_used in (100.5, 101.0, 110):
            over = router.window_state({'used_pct': over_used, 'resets_at': '2026-10-04T03:00:00Z'}, self.now)
            self.assertEqual((over['used_pct'], over['observed_remaining_pct'], over['remaining_pct_before_reset']),
                             (100.0, 0.0, 0.0))
        for bad_reset in (None, 'invalid', '2026-10-04T03:00:00'):
            self.assertIsNone(router.window_state({'used_pct': 80, 'resets_at': bad_reset}, self.now)['remaining_pct_before_reset'])
        for bad_used in (None, '80', -1, float('nan'), float('inf'), True):
            self.assertIsNone(router.window_state({'used_pct': bad_used, 'resets_at': '2026-10-05T00:00:00Z'}, self.now)['used_pct'])

        # 429 session-limit log detection and rate-limits.json write-back
        with tempfile.TemporaryDirectory() as directory:
            hit_now = datetime(2026, 10, 28, 9, 55, tzinfo=timezone.utc)
            cache = self.write_cache(directory, {
                'claude-lab': {
                    'fetched_at': '2026-10-28T09:38:12Z',
                    'available': True,
                    'stale': True,
                    'windows': [
                        {'id': 'five_hour', 'used_pct': 101.0, 'resets_at': '2026-10-28T13:50:00Z'},
                        {'id': 'seven_day', 'used_pct': 74.0, 'resets_at': '2026-10-28T15:00:00Z'},
                    ],
                }
            })
            log_path = Path(directory) / '.claude-lab' / 'projects' / 'E--PROJECTS-route-manager' / 'sess1.jsonl'
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(json.dumps({
                'type': 'assistant',
                'timestamp': '2026-10-28T09:52:07.000Z',
                'isApiErrorMessage': True,
                'error': 'rate_limit',
                'message': {'content': [{'type': 'text', 'text': "API Error: Request rejected (429) · Claude Code: You've hit your session limit · resets 1:50pm (UTC)"}]},
            }) + '\n', encoding='utf-8')
            snap_429 = router.usage_snapshot(cache, now=hit_now)
            c2 = snap_429['accounts']['claude-lab']
            self.assertEqual(c2.get('rate_limited_until'), '2026-10-28T13:50:00+00:00')
            w5 = next(w for w in c2['windows'] if w['id'] == 'five_hour')
            self.assertEqual((w5['used_pct'], w5['observed_remaining_pct'], w5['remaining_pct_before_reset'], w5.get('rate_limited')),
                             (100.0, 0.0, 0.0, True))
            persisted = json.loads((Path(directory) / '.agent-compass' / 'rate-limits.json').read_text(encoding='utf-8'))
            self.assertEqual(persisted['claude-lab']['resets_at'], '2026-10-28T13:50:00+00:00')

    def test_request_building_validation_and_metered_fallback(self):
        candidate = self.candidate(account='distinct-account')
        with patch.object(router, 'usage_snapshot', return_value={'read_ok': True}):
            delegate = router.build_request('delegate', {'task': {'objective': 'Review'}})
            select = router.build_request('select', {'task': {'objective': 'Review'}, 'candidates': {'reviewer': candidate}})
        self.assertEqual(router.validate_response(select, {'choice': 'reviewer', 'reason': 'Fits'}, router.JUDGES['codex'])['selected_bundle'], candidate)
        good = {'choice': 'self', 'specialty': 'debug_review', 'reason': 'Parent can review directly'}
        self.assertEqual(router.validate_response(delegate, good, router.JUDGES['codex'])['choice'], 'self')
        for bad in ({**good, 'choice': 'invented'}, {**good, 'specialty': 'invented'},
                    {**good, 'reason': ''}, {**good, 'reason': True}, {'choice': 'self'}):
            with self.assertRaises(ValueError):
                router.validate_response(delegate, bad, router.JUDGES['codex'])

        sub = self.candidate()
        metered = self.candidate('proxy', 'proxy', billing_source='metered')
        for candidates, status, expected in (
            ({'sub': sub, 'proxy': metered}, 'unknown', {'sub', 'none'}),
            ({'proxy': metered}, 'unknown', {'none'}),
            ({'sub': {**sub, 'available': False}, 'proxy': metered}, 'unavailable', {'proxy', 'none'}),
        ):
            with patch.object(router, 'usage_snapshot', return_value={'read_ok': True}), \
                    patch.object(router, 'user_env', return_value=None):
                req = router.build_request('select', {'task': {'objective': 'Review'},
                                                      'candidates': candidates, 'subscription_status': status})
            self.assertEqual(set(req['response_schema']['properties']['choice']['enum']), expected)

    def test_judge_cli_ordering_and_env(self):
        with tempfile.TemporaryDirectory() as directory:
            for kind, bundle in router.JUDGES.items():
                cmd = router.judge_command(kind, ['native-cli'], Path(directory), {'type': 'object'})
                self.assertIn(bundle['model'], cmd)
                self.assertIn('low', cmd if kind == 'sonnet' else ' '.join(cmd))

        with patch.dict(router.os.environ, {'OPENAI_API_KEY': 'secret', 'ROUTER_CUSTOM': 'keep'}), \
                patch.object(router.provider_inventory, 'current', return_value={'accounts': {
                    'claude-lab': {'family': 'claude', 'home': str(Path.home() / '.claude-lab')}}}):
            env = router.subscription_env('claude-lab')
            self.assertNotIn('OPENAI_API_KEY', env)
            self.assertEqual(env['ROUTER_CUSTOM'], 'keep')
            self.assertTrue(env['CLAUDE_CONFIG_DIR'].endswith('.claude-lab'))

        # Primary exhausted -> the Sonnet judge switches to claude-lab
        claude_empty = self.row(used=100)
        claude_empty['windows'][0].update({'id': 'seven_day', 'resets_at': '2026-10-08T00:00:00Z'})
        claude_2_ok = self.row(used=17)
        claude_2_ok['windows'][0].update({'id': 'seven_day', 'resets_at': '2026-10-04T01:30:00Z'})
        snap_ok = self.snapshot({'claude': claude_empty, 'claude-lab': claude_2_ok, 'codex': self.row(used=50)})
        self.assertEqual(router.resolve_judge('sonnet', snap_ok)['account'], 'claude-lab')
        self.assertEqual(router.judge_order(snap_ok, 'auto'), ['sonnet', 'codex'])

    def test_capacity_alerts_ledger_and_constraint_recording(self):
        reset = (self.now + timedelta(days=3)).isoformat()
        proxy_row = self.row(used=0, reset=reset)
        proxy_row['billing'] = 'metered'
        usage = self.snapshot({'codex': self.row(used=90, reset=reset), 'claude': self.row(used=5, reset=reset),
                               'team-proxy': proxy_row, 'old': self.row(age=15 * 86400)})
        self.assertTrue(usage['accounts']['old']['flag_conflict'])
        norm_reset = usage['accounts']['codex']['windows'][0]['resets_at']
        earlier = [{'usage': {'codex': {'fetched_at': (self.now - timedelta(hours=h)).isoformat(),
                                        'windows': [{'id': 'usage', 'used_pct': used, 'resets_at': norm_reset}]}}}
                   for h, used in ((5, 40), (2, 70))]
        alerts = router.capacity_alerts(usage, ['codex'], earlier, self.now)
        self.assertEqual({a['code'] for a in alerts}, {'low_remaining', 'exhaustion_before_reset', 'idle_alternative'})
        self.assertEqual([a['account'] for a in alerts if a['code'] == 'idle_alternative'], ['claude'])

        # Finalize & record_constraint
        now = datetime.now(timezone.utc)
        pro = {'provider': 'chatgpt-web', 'account': 'fixture-advisor', 'model': 'gpt-6-pro', 'effort': 'max'}
        accounts = account_fleet.load_accounts(account_fleet.reference_fleet_path())
        accounts['accounts']['fixture-advisor'] = {'cadence': {'window_seconds': 6 * 3600, 'limit': 5}}
        history = [{'at': (now - timedelta(hours=h)).isoformat(), 'stage': 'select', 'choice': 'pro_web', 'bundle': pro} for h in (1, 2, 3, 4, 5)]
        u_low = {'accounts': {'codex': {'status': 'fresh', 'fetched_at': now.isoformat(), 'windows': [
            {'id': 'usage', 'used_pct': 98, 'resets_at': (now + timedelta(days=5)).isoformat(),
             'seconds_until_reset': 5 * 86400, 'remaining_pct_before_reset': 2}]}}}
        res = {'stage': 'select', 'choice': 'pro_web', 'source': 'subscription_model_judgment',
               'reason': 'r', 'judge': dict(router.JUDGES['codex']), 'selected_bundle': pro, 'usage': u_low}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(router, 'ledger_path', return_value=Path(directory) / 'ledger.jsonl'), \
                patch.object(account_fleet, 'load_accounts', return_value=accounts):
            out = router.finalize(res, {'stage': 'select'}, 'codex', time.monotonic(),
                                  {'objective': 'Advise', 'work_kind': 'direction_reasoning'}, history)
            self.assertEqual({a['code'] for a in out['alerts']},
                             {'advisor_cadence', 'pinned_judge_account_low', 'telemetry_missing'})
            args = router.argparse.Namespace(basis='User: Sol xhigh', provider='codex', account='codex',
                                             model='gpt-6.1-sol', effort='xhigh', input=None, usage_cache=None,
                                             accounts=None, constraint='user_override', specialty=None)
            with patch.object(router, 'usage_snapshot', return_value={'read_ok': True, 'accounts': {}}):
                rec = router.record_constraint(args)
            self.assertEqual(rec['source'], 'constraint_record')
            self.assertNotIn('trigger', rec)
            # A record that authorizes a Workflow says so in the ledger row the spawn gate reads.
            args.trigger = 'workflow'
            with patch.object(router, 'usage_snapshot', return_value={'read_ok': True, 'accounts': {}}):
                rec = router.record_constraint(args)
            self.assertEqual(rec['trigger'], 'workflow')
            router.finalize(rec, {'stage': 'record'}, None, time.monotonic(), {'parent_agent_id': 'session-1'}, [])
            row = json.loads((Path(directory) / 'ledger.jsonl').read_text(encoding='utf-8').splitlines()[-1])
            self.assertEqual((row['stage'], row['trigger'], row['constraint']), ('record', 'workflow', 'user_override'))
            del args.trigger
            args.account = 'claude-lab'
            args.provider = 'claude-lab'
            args.model = 'claude-opus-5-5'
            with self.assertRaisesRegex(ValueError, 'disabled'):
                router.record_constraint(args)

    def test_ledger_rows_name_the_writing_session_not_its_parent(self):
        """The spawn gate binds Workflow records and schedule plans to `session_ids`; parent_id stays the parent link."""
        import roll
        import worker_context
        session_vars = [*worker_context.HOST_SESSION_ID_VARS, 'PASEO_AGENT_ID']
        cases = (
            # A roll-launched CLI worker: roll hands it the parent link, its own host sets its session.
            ({'AGENTCOMPASS_WORKER': '1', 'ROUTER_PARENT_AGENT_ID': 'P', 'CLAUDE_CODE_SESSION_ID': 'W'}, ['W']),
            # A main Claude session under Paseo: its host session and its own Paseo agent.
            ({'CLAUDE_CODE_SESSION_ID': 'S1', 'PASEO_AGENT_ID': 'A1'}, ['S1', 'A1']),
            ({'ROUTER_PARENT_AGENT_ID': 'P'}, None),     # only a parent link: no writer identity at all
        )
        for env, expected in cases:
            with self.subTest(env=env), tempfile.TemporaryDirectory() as directory, \
                    patch.dict('os.environ', {var: '' for var in session_vars}), \
                    patch.object(router, 'ledger_path', return_value=Path(directory) / 'ledger.jsonl'):
                import os
                for var in session_vars:
                    os.environ.pop(var, None)
                os.environ.update(env)
                self.assertEqual(worker_context.own_session_ids(), expected or [])
                for stage in ('plan', 'record'):
                    router.finalize({'stage': stage, 'choice': 'x', 'source': 's', 'selected_bundle': {}},
                                    {'stage': stage}, None, time.monotonic(), {'parent_agent_id': 'P'}, [])
                rows = [json.loads(line) for line in
                        (Path(directory) / 'ledger.jsonl').read_text(encoding='utf-8').splitlines()]
                self.assertEqual([row['stage'] for row in rows], ['plan', 'record'])
                for row in rows:
                    self.assertEqual(row.get('session_ids'), expected)
        # roll never passes the parent's own session identity on to a CLI worker.
        for var in worker_context.OWN_SESSION_ID_VARS + ('PASEO_AGENT_ID',):
            self.assertTrue(var.startswith(roll.HOST_SESSION_ENV), var)

    def test_constraint_record_cannot_bypass_local_account_pause_with_an_unknown_model(self):
        args = router.argparse.Namespace(basis='Synthetic explicit model request', provider='codex', account='codex',
            model='new-model', effort='high', input=None, usage_cache=None,
            accounts=None, constraint='user_override', specialty=None)
        snapshot = {'accounts': {'codex': {'family': 'codex', 'status': 'paused',
                    'reason': 'unhealthy_in_cc_switch', 'plan': 'pro'}}, 'launchers': {}}
        with patch.object(router.provider_inventory, 'current', return_value=snapshot), \
                patch.object(router, 'usage_snapshot', return_value={'read_ok': True, 'accounts': {}}):
            with self.assertRaisesRegex(ValueError, 'unhealthy_in_cc_switch'):
                router.record_constraint(args)

    def test_dot_probe_and_caller_local_dependency_admission(self):
        class Ready:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *exc): return False

        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / '.dot2api' / 'client.json'
            config.parent.mkdir()
            config.write_text(json.dumps({'base_url': 'http://127.0.0.1:8788/', 'api_key': 'secret-key'}), encoding='utf-8')
            with patch.object(router.Path, 'home', return_value=Path(directory)), \
                    patch.dict(router.os.environ, {'DOT2API_BASE_URL': ''}), \
                    patch.object(router.urllib.request, 'urlopen', return_value=Ready()):
                self.assertIsNone(router.dot_unreachable())

            cases = [
                ('remote', {}, None, True, True, 'dot_handoff'),
                ('local runtime', {'local_only': True, 'local_only_reason': 'runs local pytest'}, None, False, False, None),
                ('missing reason respected', {'local_only': True}, None, False, False, None),
                ('edit-only reason respected', {'local_only': True, 'local_only_reason': 'in-place edits of workspace files'},
                 None, False, False, None),
                ('explicit false', {'local_only': False}, None, True, True, 'dot_handoff'),
                ('credentials', {'local_only': True, 'local_only_reason': 'Needs local ssh-agent VPS credentials'},
                 None, False, False, None),
                ('unreachable', {}, 'URLError on /readyz', False, True, 'dot_unreachable'),
            ]
            for label, needs, down, dot, probed, alert in cases:
                with self.subTest(case=label):
                    result, probe = self.run_plan(directory, {'objective': 'Fix single-file parser bug',
                        'work_kind': 'debug_review', 'difficulty': 'hard', 'needs': needs}, down)
                    self.assertEqual(dot_admitted(result), dot)
                    self.assertNotEqual(result['choice'], 'none')
                    self.assertEqual(probe.called, probed)
                    codes = {a['code'] for a in result['alerts']}
                    if alert and (alert != 'dot_handoff' or result['choice'] == 'dot_text'):
                        self.assertIn(alert, codes)
                    self.assertNotIn('local_only_ignored_without_reason', codes)
            ssh, _ = self.run_plan(directory, {'objective': 'Read-only checklist over SSH and the repository',
                'work_kind': 'tool_execution', 'difficulty': 'hard', 'needs': {'local_only': True}})
            self.assertNotIn(ssh['choice'], ('dot_text', 'none'))
            unknown, _ = self.run_plan(directory, {'objective': 'Compute', 'work_kind': 'research_compute', 'difficulty': 'hard',
                                                   'profile': profile(work_kind='other_or_mixed')})
            self.assertIn('unknown_work_kind', {a['code'] for a in unknown['alerts']})

    def test_recent_dot_reroll_requires_local_evidence_or_failed_health(self):
        capabilities = reference_capabilities()
        task = {'task_id': 't-dot-1', 'objective': 'Fix single-file parser bug', 'work_kind': 'debug_review', 'difficulty': 'hard'}
        recent = {'at': datetime.now(timezone.utc).isoformat(), 'stage': 'plan',
                  'choice': 'dot_text', 'task_id': 't-dot-1', 'request_hash': router._task_request_hash(task)}
        for local_only, down, enabled, dot, blocked in (
            (False, None, True, True, True),
            (True, None, True, False, False),
            (False, 'endpoint down', True, False, False),
            (False, None, False, False, False),
        ):
            with self.subTest(local_only=local_only, down=down, enabled=enabled), \
                    tempfile.TemporaryDirectory() as directory, \
                    patch.object(account_fleet, 'load_capabilities', return_value={**capabilities,
                        'remote_reroute_guard': {'enabled': enabled, 'bundle_id': 'dot_text', 'window_seconds': 600}}):
                result, _ = self.run_plan(directory, dict(task, needs={'local_only': local_only}),
                                           down, unavailable=['dot_text'], records=[recent])
                self.assertEqual(dot_admitted(result), dot)
                self.assertNotEqual(result['choice'], 'none')
                self.assertEqual('dot_reroll_blocked' in {a['code'] for a in result['alerts']}, blocked)
        # m1: sharing an objective prefix (>180 chars) without matching task_id/chain_id/request_hash never locks
        prefix = 'Shared preamble ' * 15
        rec_prefix = {'at': datetime.now(timezone.utc).isoformat(), 'stage': 'plan', 'choice': 'dot_text',
                      'objective': prefix + 'task one',
                      'request_hash': router._task_request_hash({'objective': prefix + 'task one'})}
        self.assertIsNone(router.recent_route_lock(
            {'objective': prefix + 'task two'}, [rec_prefix],
            datetime.now(timezone.utc), 'dot_text', 600))
        self.assertIsNotNone(router.recent_route_lock(
            {'objective': prefix + 'task one'}, [rec_prefix],
            datetime.now(timezone.utc), 'dot_text', 600))

    @pytest.mark.slow
    def test_timeout_stops_owned_process(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                router.run_process([sys.executable, '-c', 'import time; time.sleep(30)'], directory, timeout=0.1)


def as_agy_stream(stdout):
    """agy answers on its stream-json channel: a scripted single result envelope arrives as a `result` event."""
    try:
        envelope = json.loads(stdout)
    except (TypeError, ValueError):
        return stdout
    if not isinstance(envelope, dict) or 'event' in envelope:
        return stdout
    return json.dumps({'event': 'init'}) + chr(10) + json.dumps({'event': 'result', 'result': envelope}) + chr(10)


GUI_PROFILE = {'reasoning_depth': 1, 'code_complexity': 0, 'needs_visual': True, 'needs_gui': True, 'local_only': True,
               'wants_fast': False, 'read_only_review': False, 'multi_task': False, 'task_count': '1',
               'interaction_style': 'engineering',
               'axis_demand': {'divergence': 0, 'coding': 1, 'aesthetics': 0, 'tool_operation': 3}}


class ReadinessGapTests(unittest.TestCase):
    """A router with nothing to route to, or no quota data at all, must say so instead of returning a confident pick."""

    @staticmethod
    def accounts(*statuses):
        return {'_discovery': {'accounts': {f'a{i}': {'status': s, 'family': 'claude'} for i, s in enumerate(statuses)}}}

    def test_readiness_gap_severity_and_effect_on_choice(self):
        for statuses, read_ok, code, fatal in (
            (('missing_credentials', 'missing_client'), True, 'no_ready_account', True),
            (('paused',), True, 'no_ready_account', True),
            (('ready',), False, 'no_usage_data', False),
            (('ready',), True, None, False),
            (None, True, None, False),
        ):
            with self.subTest(statuses=statuses, read_ok=read_ok):
                accounts = self.accounts(*statuses) if statuses is not None else {}
                usage = {'read_ok': read_ok}
                gap = router.readiness_gap(accounts, usage)
                planned = {'choice': 'sonnet55_high', 'selected_bundle': {'account': 'a0'}, 'decision': {}}
                router.apply_readiness_gap(planned, accounts, usage)
                if code is None:
                    self.assertIsNone(gap)
                else:
                    self.assertEqual((gap['code'], gap['fatal']), (code, fatal))
                if fatal:
                    self.assertIn(' setup` to detect the CLIs', gap['message'])
                    self.assertNotIn('onboard.py', gap['message'])
                    self.assertEqual((planned['choice'], planned['error']), ('none', code))
                    self.assertNotIn('selected_bundle', planned)
                    self.assertEqual(planned['alerts'][0]['severity'], 'warn')
                    self.assertIn(' setup`', planned['decision']['reason'])
                else:
                    self.assertEqual(planned['choice'], 'sonnet55_high')
                    self.assertEqual(planned['selected_bundle'], {'account': 'a0'})

    def test_plan_with_ready_account_but_no_cache_warns(self):
        inventory = {'accounts': {'antigravity': {'family': 'antigravity', 'status': 'ready'}}, 'launchers': {}}
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'in.json'
            source.write_text(json.dumps({'task': {'objective': 'x', 'expected_hours': 0.5, 'work_kind': 'general_implementation',
                                                   'difficulty': 'routine', 'profile': profile()}}), encoding='utf-8')
            out = io.StringIO()
            missing = str(Path(directory) / '.usage-float' / 'usage-cache.json')
            with patch.object(sys, 'argv', ['route_judge.py', 'plan', '--dry-run', '--no-score', '--input', str(source),
                                            '--usage-cache', missing]), \
                    patch.object(router.provider_inventory, 'current', return_value=inventory), \
                    patch.object(router, 'dot_unreachable', return_value='down'), \
                    patch.object(router, 'read_ledger', return_value=[]), contextlib.redirect_stdout(out):
                router.main()
        parsed_out = json.loads(out.getvalue())
        alerts = {a['code']: a['severity'] for a in parsed_out['alerts']}
        self.assertEqual(alerts.get('no_usage_data'), 'warn')


class CliPrefixTests(unittest.TestCase):
    def test_grok_installed_outside_path_resolves_to_its_home_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            installed = Path(tmp) / '.grok' / 'bin' / ('grok.exe' if router.os.name == 'nt' else 'grok')
            with patch.object(router.shutil, 'which', return_value=None), patch.object(router.Path, 'home', return_value=Path(tmp)):
                with self.assertRaisesRegex(RuntimeError, 'not installed'):
                    router.cli_prefix('grok')
                installed.parent.mkdir(parents=True)
                installed.write_bytes(b'')
                self.assertEqual(router.cli_prefix('grok'), [str(installed)])
                with self.assertRaisesRegex(RuntimeError, 'not installed'):
                    router.cli_prefix('codex')
            with patch.object(router.shutil, 'which', return_value='C:/tools/grok.exe'):
                self.assertEqual(router.cli_prefix('grok'), [str(Path('C:/tools/grok.exe'))])


class ScoreCacheTests(unittest.TestCase):
    """Classification cache: same task and scorer identity skips the scorer call; anything else misses."""

    def setUp(self):
        self.capabilities = reference_capabilities()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = patch.dict(router.os.environ, {'ROUTER_SCORE_CACHE': str(Path(self.dir.name) / 'score-cache.json')})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = 0

    def score(self, objective='Fix the parser bug', config=None, stdout=None):
        def run(command, cwd, prompt=None, timeout=120, account=None):
            self.calls += 1
            answer = stdout or json.dumps({'status': 'SUCCESS', 'structured_output': GUI_PROFILE})
            return 0, (as_agy_stream(answer) if command[0] == 'agy.exe' else answer), ''
        cfg = config if config is not None else {'scorer_type': 'agy_flash_low', 'fallback_to_agy': False}
        with patch.object(router, 'scorer_prefix', return_value=['agy.exe']), patch.object(router, 'run_process', side_effect=run):
            return router.score_task({'objective': objective}, self.capabilities, scorer_config=cfg)

    def test_repeat_is_served_from_cache_and_changed_task_or_scorer_is_not(self):
        first, meta = self.score()
        again, cached = self.score()
        self.assertEqual((self.calls, again, cached.get('cached'), meta.get('cached')), (1, first, True, None))
        self.score('Fix the lexer bug')
        self.assertEqual(self.calls, 2)
        self.score(config={'scorer_type': 'jev', 'jev_api_key': 'k1', 'fallback_to_agy': True})
        self.assertEqual(self.calls, 3)  # different scorer identity (the jev call fails, cascade answers)

    def test_corrupt_stale_and_fallback_results_never_serve_from_cache(self):
        self.score()
        path = router.score_cache_path()
        path.write_text('{not json', encoding='utf-8')
        self.score()
        self.assertEqual(self.calls, 2)
        data = json.loads(path.read_text(encoding='utf-8'))
        for entry in data['entries'].values():
            entry['saved_at'] -= router.SCORE_CACHE_TTL_SECONDS + 1
        path.write_text(json.dumps(data), encoding='utf-8')
        self.score()
        self.assertEqual(self.calls, 3)
        path.unlink()
        prof, meta = self.score('Other task', stdout='garbage')
        self.assertIsNone(prof)
        self.assertEqual(meta['source'], 'scorer_unavailable')
        self.score('Other task', stdout='garbage')
        self.assertEqual(self.calls, 5)

    def test_prefetch_classifies_tasks_concurrently_and_every_result_reaches_the_cache(self):
        import threading
        running, peak, lock = [0], [0], threading.Lock()

        def run(command, cwd, prompt=None, timeout=120, account=None):
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            time.sleep(0.05)
            with lock:
                running[0] -= 1
                self.calls += 1
            return 0, as_agy_stream(json.dumps({'status': 'SUCCESS', 'structured_output': GUI_PROFILE})), ''
        tasks = [{'objective': f'Fix bug {n}'} for n in range(4)]
        with patch.object(router, 'scorer_prefix', return_value=['agy.exe']), patch.object(router, 'run_process', side_effect=run), \
                patch.object(router, 'load_scorer_config', return_value={'scorer_type': 'agy_flash_low', 'fallback_to_agy': False}), \
                patch.object(account_fleet, 'load_capabilities', return_value=self.capabilities):
            router.prefetch_scores(tasks)
        self.assertEqual(self.calls, 4)
        self.assertGreater(peak[0], 1)
        for n in range(4):
            self.assertTrue(self.score(f'Fix bug {n}')[1].get('cached'))
        self.assertEqual(self.calls, 4)

    def test_cache_holds_only_the_classification_never_routing_state_or_keys(self):
        self.score(config={'scorer_type': 'jev', 'jev_api_key': 'secret-key-123', 'fallback_to_agy': True})
        self.score()
        raw = router.score_cache_path().read_text(encoding='utf-8')
        self.assertNotIn('secret-key-123', raw)
        for entry in json.loads(raw)['entries'].values():
            self.assertEqual(set(entry), {'saved_at', 'profile', 'scorer'})

    def test_cache_is_bounded(self):
        for index in range(router.SCORE_CACHE_MAX + 5):
            router.score_cache_put(f'k{index}', GUI_PROFILE, {'source': 'system_one'})
        self.assertEqual(len(router._score_cache_read()), router.SCORE_CACHE_MAX)


class FullTaskTextTests(unittest.TestCase):
    """The classifier gets the whole task text on every transport; a text no transport can carry stops instead of being cut."""

    def setUp(self):
        self.capabilities = reference_capabilities()
        patcher = patch.dict(router.os.environ, {'ROUTER_SCORE_CACHE': 'off', 'ROUTER_DISABLE_LIVE_SCORER': ''})
        patcher.start()
        self.addCleanup(patcher.stop)
        crlf = chr(13) + chr(10)
        filler = ('普通背景说明，请忽略。' + crlf) * 2000  # ~24k characters, far past the agy command line
        self.text = filler + '中间的硬性验收条件：必须在周五前完成。' + crlf + filler
        self.config = {'scorer_type': 'agy_flash_low', 'fallback_to_agy': True}

    def test_evidence_is_never_cut_and_the_middle_is_part_of_the_prompt(self):
        prompt = router.scorer_prompt({'objective': self.text, 'context': '上下文' * 5000})
        self.assertIn(json.dumps(self.text, ensure_ascii=False), prompt)
        self.assertIn('中间的硬性验收条件', prompt)
        self.assertNotEqual(router.score_cache_key({'objective': self.text}, self.capabilities, ['agy_flash_low'], {}),
                            router.score_cache_key({'objective': self.text.replace('周五', '周四')},
                                                   self.capabilities, ['agy_flash_low'], {}))

    def test_agy_sends_every_task_on_stdin_and_nothing_long_on_the_command_line(self):
        runs = []

        def fake_run(command, cwd, prompt=None, timeout=120, account=None):
            runs.append((command, prompt))
            result = {'conversation_id': 'c', 'status': 'SUCCESS', 'structured_output': GUI_PROFILE}
            return 0, json.dumps({'event': 'init'}) + chr(10) + json.dumps({'event': 'result', 'result': result}) + chr(10), ''
        with patch.object(router, 'scorer_prefix', return_value=['agy.exe']), patch.object(router, 'run_process', side_effect=fake_run):
            router.score_task({'objective': self.text}, self.capabilities, scorer_config=self.config)
            router.score_task({'objective': 'short task'}, self.capabilities, scorer_config=self.config)
        for (command, stdin), expected in zip(runs, (self.text, 'short task')):
            self.assertEqual(command[-2:], ['--print', ''])
            self.assertIn('stream-json', command)
            message = json.loads(stdin)
            self.assertEqual(message['event'], 'user')
            self.assertIn(json.dumps(expected, ensure_ascii=False), message['message']['content'])
            self.assertIn('calibration_cases', message['message']['content'])
            self.assertLess(max(len(part) for part in command), 8000)  # only the compact schema rides the command line

    def test_a_failed_stdin_channel_gives_scorer_unavailable_for_any_task_length(self):
        with patch.object(router, 'scorer_prefix', return_value=['agy.exe']), \
                patch.object(router, 'run_process', return_value=(2, '', 'unsupported')):
            for objective in (self.text, 'short task'):
                prof, meta = router.score_task({'objective': objective}, self.capabilities, scorer_config=self.config)
                self.assertIsNone(prof)
                self.assertEqual(meta['source'], 'scorer_unavailable')

    def test_api_scorer_refusing_the_size_falls_back_to_agy_stdin_not_to_heuristic(self):
        ok = {'conversation_id': 'c', 'status': 'SUCCESS', 'structured_output': GUI_PROFILE}
        error = router.urllib.error.HTTPError('https://api.typesafe.ai', 413, 'too large', {}, None)
        with patch.object(router.urllib.request, 'urlopen', side_effect=error), patch.object(router, 'user_env', return_value=None),                 patch.object(router, 'scorer_prefix', return_value=['agy.exe']),                 patch.object(router, 'run_process', return_value=(0, json.dumps({'event': 'result', 'result': ok}), '')):
            _, meta = router.score_task({'objective': self.text}, self.capabilities,
                                        scorer_config={'scorer_type': 'jev', 'jev_api_key': 'k', 'fallback_to_agy': True})
            self.assertEqual((meta['source'], meta['attempts'][0]['error_type']), ('system_one', 'ScorerInputError'))
            with self.assertRaises(router.ScorerInputError):
                router.score_task({'objective': self.text}, self.capabilities,
                                  scorer_config={'scorer_type': 'jev', 'jev_api_key': 'k', 'fallback_to_agy': False})


class SystemOneScorerTests(unittest.TestCase):
    """Contract tests for System-One scoring via Gemini 3.8 Flash low and heuristic fallback."""

    def setUp(self):
        self.capabilities = reference_capabilities()
        # Tests rescore one objective with different fake answers; a cache hit would replay the first.
        patcher = patch.dict(router.os.environ, {'ROUTER_SCORE_CACHE': 'off'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def envelope(self, structured=None, response='', status='SUCCESS'):
        return json.dumps({'conversation_id': 'c', 'status': status, 'response': response,
                           **({'structured_output': structured} if structured is not None else {})})

    def fake_agy(self, stdout, code=0):
        calls = []
        def run(command, cwd, prompt=None, timeout=120, account=None):
            calls.append({'command': command, 'cwd': cwd, 'timeout': timeout, 'stdin': prompt})
            return code, as_agy_stream(stdout), ''
        return calls, run

    def score(self, task, stdout=None, code=0, use_model=True):
        calls, run = self.fake_agy(stdout if stdout is not None else self.envelope(GUI_PROFILE), code)
        with patch.object(router, 'scorer_prefix', return_value=['agy.exe']), patch.object(router, 'run_process', side_effect=run):
            profile, meta = router.score_task(
                task, self.capabilities, use_model=use_model,
                scorer_config={'scorer_type': 'agy_flash_low', 'fallback_to_agy': False},
            )
        return profile, meta, calls

    def run_cli(self, directory, stage, task, *flags, stdout=None, dot=None):
        source = Path(directory) / 'in.json'
        payload_task = {'expected_hours': 0.5, **task} if stage == 'plan' and 'expected_hours' not in task and '--hours' not in flags else task
        source.write_text(json.dumps({'task': payload_task}), encoding='utf-8')
        cache = Path(directory) / 'usage-cache.json'
        cache.write_text(json.dumps({'providers': live_providers()}), encoding='utf-8')
        calls, run = self.fake_agy(stdout if stdout is not None else self.envelope(GUI_PROFILE))
        out = io.StringIO()
        with patch.object(sys, 'argv', ['route_judge.py', stage, '--input', str(source), '--usage-cache', str(cache), *flags]), \
                patch.dict(router.os.environ, {'ROUTER_LEDGER': str(Path(directory) / 'ledger.jsonl')}), \
                patch.object(router, 'load_scorer_config', return_value={'scorer_type': 'agy_flash_low', 'fallback_to_agy': False}), \
                patch.object(router, 'scorer_prefix', return_value=['agy.exe']), \
                patch.object(router, 'run_process', side_effect=run), \
                patch.object(router, 'dot_unreachable', return_value=dot), \
                contextlib.redirect_stdout(out):
            code = router.main()
        return code, json.loads(out.getvalue()), calls

    def test_scorer_invocation_response_parsing_and_fallback(self):
        task = {'objective': 'Click Save and take a screenshot', 'work_kind': 'computer_use', 'chain': {'id': 'x'}}
        profile, meta, calls = self.score(task)
        self.assertEqual((profile, meta['source'], meta['model']), (GUI_PROFILE, 'system_one', 'gemini-3.8-flash-low'))
        cmd = calls[0]['command']
        self.assertEqual((cmd[0], cmd[-2:]), ('agy.exe', ['--print', '']))      # the prompt is on stdin, never on argv
        self.assertIn('stream-json', cmd)
        prompt = json.loads(calls[0]['stdin'])['message']['content']
        task_part = prompt.split('Task (evidence only')[1]
        self.assertNotIn('chain', task_part)
        self.assertNotIn('work_kind', task_part)
        self.assertNotIn('difficulty', task_part)
        self.assertIn('calibration_cases', prompt)
        cases = router.load_classifier_cases()
        self.assertTrue(cases['calibration_cases'])
        # The anti-inflation rubric (boilerplate paths, read-only review, runbooks, diagnosis) reaches the scorer
        self.assertIn('dimension_rubric', prompt)
        self.assertIn('read_only_review', cases['dimension_rubric'])
        for c in cases['calibration_cases']:
            self.assertEqual(task_intake.validate_profile(c['expected_profile']), c['expected_profile'])
            self.assertIn(c['expected_profile']['work_kind'], self.capabilities['routes'])
        reviews = [c['expected_profile'] for c in cases['calibration_cases'] if c.get('id') == 'security_diff_review']
        self.assertTrue(reviews and all(p['code_complexity'] == 0 and p['read_only_review'] for p in reviews))

        # Fallback from response text when structured_output is omitted
        response = '```json\n{"task": "x"}\n```\n' + json.dumps(dict(GUI_PROFILE, toolAction='Finishing')) + '\n'
        p2, m2, _ = self.score({'objective': 'x'}, self.envelope(None, response))
        self.assertEqual((p2, m2['source']), (GUI_PROFILE, 'system_one'))

        # No mechanical fallback on errors or skipped model call: returns scorer_unavailable so parent session chooses
        bug_task = {'objective': 'Fix a parser bug', 'work_kind': 'debug_review'}
        for reason, stdout in (('ValueError', self.envelope(dict(GUI_PROFILE, reasoning_depth=7))),
                               ('RuntimeError', self.envelope(GUI_PROFILE, status='ERROR')),
                               ('ValueError', 'not json')):
            prof, meta, _ = self.score(bug_task, stdout)
            self.assertEqual((prof, meta['source'], meta['fallback_reason']), (None, 'scorer_unavailable', reason))
        prof_skipped, meta, calls = self.score(bug_task, use_model=False)
        self.assertEqual((prof_skipped, meta['source'], meta['fallback_reason'], calls), (None, 'scorer_unavailable', 'model_call_skipped', []))

    def test_legacy_or_corrupted_score_cache_is_treated_as_cache_miss(self):
        old_entry = {k: v for k, v in GUI_PROFILE.items() if k not in ('wants_fast', 'read_only_review')}
        contradictory_entry = dict(GUI_PROFILE, code_complexity=2, read_only_review=True)
        config = {'scorer_type': 'agy_flash_low', 'fallback_to_agy': False}
        _, order = router.scorer_attempt_order(config)
        for bad_cached in (old_entry, contradictory_entry):
            with self.subTest(bad_cached=bad_cached), tempfile.TemporaryDirectory() as directory:
                cache_file = Path(directory) / 'score-cache.json'
                task = {'objective': 'Click Save and take a screenshot', 'expected_hours': 0.5}
                with patch.dict(router.os.environ, {'ROUTER_SCORE_CACHE': str(cache_file)}):
                    key = router.score_cache_key(task, self.capabilities, order, config)
                    cache_file.write_text(
                        json.dumps({'entries': {key: {'saved_at': time.time(), 'profile': bad_cached,
                                                      'scorer': {'source': 'system_one', 'model': 'old'}}}}),
                        encoding='utf-8',
                    )
                    profile, meta, calls = self.score(task)
                self.assertEqual(len(calls), 1)
                self.assertEqual(profile, GUI_PROFILE)
                self.assertFalse(meta.get('cached', False))

    def test_score_and_plan_with_score_cli_contracts(self):
        with tempfile.TemporaryDirectory() as directory:
            code, res, calls = self.run_cli(directory, 'score', {'objective': 'Click Save and take a screenshot'})
            self.assertEqual((code, len(calls), res['stage'], res['task_profile']), (0, 1, 'score', GUI_PROFILE))
            self.assertFalse((Path(directory) / 'ledger.jsonl').exists())

            gui_task = {'objective': 'Click Save in local app', 'work_kind': 'computer_use',
                        'needs': {'local_only': True, 'local_only_reason': 'operates local screen'}}
            code, planned, _ = self.run_cli(directory, 'plan', gui_task, '--score')
            self.assertEqual((code, planned['scorer']['source'], planned['decision']['task']['difficulty']), (0, 'system_one', 'routine'))

            # Default plan without --score and without caller work_kind/difficulty invokes classifier automatically
            bare_gui = {'objective': 'Click Save in local app',
                        'needs': {'local_only': True, 'local_only_reason': 'operates local screen'}}
            code, auto_planned, auto_calls = self.run_cli(
                directory, 'plan', bare_gui, stdout=self.envelope(dict(GUI_PROFILE, work_kind='computer_use')))
            self.assertEqual((code, len(auto_calls), auto_planned['scorer']['source'],
                              auto_planned['decision']['task']['kind'], auto_planned['decision']['task']['difficulty']),
                             (0, 1, 'system_one', 'computer_use', 'routine'))

            # Inflated caller difficulty / manual work_kind is overridden by the System-One classifier
            _, inflated, _ = self.run_cli(
                directory, 'plan', {'objective': 'Click Save', 'work_kind': 'complex_build_plan', 'difficulty': 'coupled'})
            alert_codes = [a['code'] for a in inflated['alerts']]
            self.assertIn('difficulty_above_profile', alert_codes)
            self.assertIn('caller_classification_ignored', alert_codes)
            self.assertEqual((inflated['decision']['task']['kind'], inflated['decision']['task']['difficulty']),
                             ('computer_use', 'routine'))

            # Missing or non-positive expected_hours is refused on plan unless --hours is passed
            for bad in ({'objective': 'Click Save'}, {'objective': 'Click Save', 'expected_hours': 0},
                        {'objective': 'Click Save', 'expected_hours': -1}):
                source = Path(directory) / 'bad_hours.json'
                source.write_text(json.dumps({'task': bad}), encoding='utf-8')
                out = io.StringIO()
                with patch.object(sys, 'argv', ['route_judge.py', 'plan', '--dry-run', '--input', str(source)]), \
                        contextlib.redirect_stdout(out):
                    bad_code = router.main()
                self.assertEqual(bad_code, 1)
                self.assertIn('expected_hours', json.loads(out.getvalue()).get('detail', ''))
            code_flag, res_flag, _ = self.run_cli(directory, 'plan', {'objective': 'Click Save'}, '--hours', '0.5')
            self.assertEqual((code_flag, res_flag['decision']['task']['hours']), (0, 0.5))

    def test_hours_come_from_the_same_classification_and_explicit_hours_win(self):
        base = dict(GUI_PROFILE, work_kind='general_implementation', needs_visual=False, needs_gui=False, local_only=False)
        estimate = dict(base, expected_hours=3.0, hours_confidence='medium', urgency='unknown')
        task = {'objective': 'Implement the parser module with tests', 'hours_source': 'classifier_estimate', 'expected_hours': None}
        with tempfile.TemporaryDirectory() as directory:
            code, result, calls = self.run_cli(directory, 'plan', task, stdout=self.envelope(estimate))
            self.assertEqual((code, len(calls)), (0, 1))  # one scorer call carries class, hours and urgency
            self.assertEqual(result['hours_estimate'], {'hours': 3.0, 'source': 'classifier_estimate', 'confidence': 'medium'})
            self.assertEqual(result['decision']['task']['hours'], 3.0)
            self.assertEqual(result['task_profile']['urgency'], 'unknown')
            code, unestimated, _ = self.run_cli(directory, 'plan', task, stdout=self.envelope(base))
            # No default stands in for an hours estimate the classifier did not give.
            self.assertEqual(code, 1)
            self.assertIn('expected_hours is required', unestimated['detail'])
            _, explicit, _ = self.run_cli(directory, 'plan', {'objective': task['objective'], 'expected_hours': 0.25},
                                          stdout=self.envelope(estimate))
            self.assertNotIn('hours_estimate', explicit)
            self.assertEqual(explicit['decision']['task']['hours'], 0.25)
        for bad in ({'expected_hours': 'x'}, {'urgency': 'now'}, {'hours_confidence': 'sure'}, {'expected_hours': True}):
            with self.assertRaises(ValueError):
                task_intake.validate_profile(dict(base, **bad))
        self.assertEqual(task_intake.validate_profile(dict(base, expected_hours=999))['expected_hours'], 24.0)
        self.assertNotIn('expected_hours', task_intake.validate_profile(base))

    def test_profile_local_only_is_trusted_and_caller_values_win(self):
        profile = dict(GUI_PROFILE, work_kind='general_implementation', needs_visual=False,
                       needs_gui=False, code_complexity=2, reasoning_depth=2,
                       axis_demand={'divergence': 1, 'coding': 2, 'aesthetics': 0, 'tool_operation': 0})
        with tempfile.TemporaryDirectory() as directory:
            unavail_code, unavail_res, _ = self.run_cli(directory, 'plan', {'objective': 'Explore local logs'}, stdout='not json')
            self.assertEqual((unavail_code, unavail_res['choice'], unavail_res['error'], unavail_res['caller_action']),
                             (2, 'none', 'scorer_unavailable', 'parent_autonomous_choice'))
        # Short/edit-only reasons intentionally fail the caller validator but remain trusted classifier output.
        cases = [
            ({}, 'Uses the local pytest environment.', True, 'Uses the local pytest environment.'),
            ({'local_only': None}, 'local', True, 'local'),
            ({}, 'in-place edit', True, 'in-place edit'),
            ({}, '', True, 'Task profile requires execution on this machine.'),
            ({'local_only': False}, 'Uses the local pytest environment.', False, None),
            ({'local_only': True, 'local_only_reason': 'Needs local ssh-agent credentials'},
             'Classifier reason', True, 'Needs local ssh-agent credentials'),
        ]
        for needs, reason, expected_local, expected_reason in cases:
            with self.subTest(needs=needs, reason=reason), tempfile.TemporaryDirectory() as directory:
                task = {'objective': 'Write CSV parser', 'needs': needs}
                code, result, _ = self.run_cli(directory, 'plan', task,
                                              stdout=self.envelope(dict(profile, reason=reason)))
                self.assertEqual(code, 0)
                self.assertEqual(result['scorer']['source'], 'system_one')
                self.assertEqual(result['decision']['task']['difficulty_source'], 'profile')
                self.assertNotEqual(result['choice'], 'none')
                self.assertEqual(dot_admitted(result), not expected_local)
                if expected_local:
                    self.assertNotEqual(result['choice'], 'dot_text')
                applied = [a for a in result['alerts'] if a['code'] == 'profile_local_only_applied']
                self.assertEqual(bool(applied), needs.get('local_only') is None)
                if applied:
                    self.assertEqual((applied[0]['severity'], applied[0]['reason']), ('info', expected_reason))
                self.assertNotIn('local_only_ignored_without_reason', [a['code'] for a in result['alerts']])
                ledger = json.loads((Path(directory) / 'ledger.jsonl').read_text(encoding='utf-8').splitlines()[-1])
                self.assertEqual(ledger.get('local_only_reason'), expected_reason)
                self.assertEqual(bool(ledger['local_only']), expected_local)
                with router.dispatch_registry.transaction() as connection:
                    decision = router.dispatch_registry.get(connection, 'decisions', result['decision_id'])
                self.assertEqual(decision['allocation_task']['needs']['local_only'], expected_local)
                self.assertEqual(decision['allocation_task']['needs'].get('local_only_reason'), expected_reason)
                self.assertEqual(decision['task']['needs'], needs)


JEV_RESPONSE = {
    'model': 'jev-2026-10-01',
    'answers': {
        'work_kind': {'type': 'choice', 'choice': 'computer_use', 'confidence': 0.8,
                      'probabilities': {'computer_use': 0.85, 'tool_execution': 0.15}},
        'interaction_style': {'type': 'choice', 'choice': 'engineering', 'confidence': 0.7,
                              'probabilities': {'engineering': 0.8, 'exploration': 0.2}},
        'reasoning_depth': {'type': 'score', 'score': 1.4, 'confidence': 0.5, 'probabilities': {'0': 0.2, '1': 0.3, '2': 0.4, '3': 0.1}},
        'code_complexity': {'type': 'score', 'score': 0.2, 'confidence': 0.8, 'probabilities': {'0': 0.85, '1': 0.1, '2': 0.05, '3': 0.0}},
        'demand_divergence': {'type': 'score', 'score': 0.1, 'confidence': 0.7, 'probabilities': {'0': 0.9, '1': 0.1, '2': 0.0, '3': 0.0}},
        'demand_coding': {'type': 'score', 'score': 1.2, 'confidence': 0.6, 'probabilities': {'0': 0.1, '1': 0.7, '2': 0.2, '3': 0.0}},
        'demand_aesthetics': {'type': 'score', 'score': 0.3, 'confidence': 0.8, 'probabilities': {'0': 0.8, '1': 0.2, '2': 0.0, '3': 0.0}},
        'demand_tool_operation': {'type': 'score', 'score': 2.8, 'confidence': 0.8, 'probabilities': {'0': 0.0, '1': 0.0, '2': 0.2, '3': 0.8}},
        'needs_visual': {'type': 'noul', 'noul': 0.1},
        'needs_gui': {'type': 'noul', 'noul': 0.8},
        'local_only': {'type': 'noul', 'noul': 0.7},
        'wants_fast': {'type': 'noul', 'noul': 0.05},
        'read_only_review': {'type': 'noul', 'noul': 0.05},
        'multi_task': {'type': 'noul', 'noul': 0.1},
        'task_count': {'type': 'choice', 'choice': '1'},
    },
    'usage': {'input_tokens': 900, 'output_tokens': 40},
}
JEV_PROFILE = {'work_kind': 'computer_use', 'interaction_style': 'engineering', 'multi_task': False, 'task_count': '1',
               'reasoning_depth': 2, 'code_complexity': 0,
               'needs_visual': True, 'needs_gui': True, 'local_only': True, 'wants_fast': False, 'read_only_review': False,
               'axis_demand': {'divergence': 0, 'coding': 1, 'aesthetics': 0, 'tool_operation': 3}}
SECRET = 'test-key-0000000000000000'


class SelectableScorerTests(unittest.TestCase):
    """Contract tests for the user-selectable System-One scorers (config, Jev, Claude, Codex, custom API, heuristic)."""

    def setUp(self):
        self.capabilities = reference_capabilities()
        self.path = temporary_root(self.addCleanup) / 'scorer-config.json'
        env = patch.dict(router.os.environ, {'ROUTER_SCORER_CONFIG': str(self.path), 'ROUTER_SCORER_TYPE': '',
                                             'ROUTER_DISABLE_LIVE_SCORER': '', 'ROUTER_SCORE_CACHE': 'off'})
        env.start()
        self.addCleanup(env.stop)
        no_user_env = patch.object(router, 'user_env', return_value=None)
        no_user_env.start()
        self.addCleanup(no_user_env.stop)
        self.task = {'objective': 'Click Save in the local app and take a screenshot', 'work_kind': 'tool_execution',
                     'chain': {'id': 'x'}}

    def score(self, config, post=None, run=None, prefix=('agy.exe',), cli=('cli.exe',)):
        calls = {'post': [], 'run': []}

        def fake_post(url, body, api_key, timeout):
            calls['post'].append({'url': url, 'body': body, 'key': api_key, 'timeout': timeout})
            if isinstance(post, Exception):
                raise post
            return post

        def fake_run(command, cwd, prompt=None, timeout=120, account=None):
            calls['run'].append({'command': command, 'cwd': cwd, 'stdin': prompt})
            return run(command, cwd) if callable(run) else (0, json.dumps({'status': 'SUCCESS', 'structured_output': GUI_PROFILE}), '')

        with patch.object(router, 'post_json', side_effect=fake_post), \
                patch.object(router, 'run_process', side_effect=fake_run), \
                patch.object(router, 'scorer_prefix', return_value=list(prefix)), \
                patch.object(router, 'cli_prefix', return_value=list(cli)):
            profile, meta = router.score_task(self.task, self.capabilities, scorer_config=config)
        return profile, meta, calls

    def test_config_load_save_env_override_and_masking(self):
        self.assertEqual(router.load_scorer_config(), {'scorer_type': 'claude_haiku_low', 'fallback_to_agy': True})
        doc = router.save_scorer_config('jev', api_key=f'  {SECRET} ')
        self.assertEqual(doc, {'scorer_type': 'jev', 'jev_api_key': SECRET})
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['jev_api_key'], SECRET)
        # None keeps the stored key, '' clears it; base_url/model are stored per scorer type
        router.save_scorer_config('jev', api_key=None, base_url='https://api.typesafe.ai/', model='jev-latest')
        config = router.load_scorer_config()
        self.assertEqual((config['scorer_type'], config['jev_api_key'], config['jev_base_url']), ('jev', SECRET, 'https://api.typesafe.ai'))
        router.save_scorer_config('openai_compatible', api_key='test-custom-key', base_url='http://127.0.0.1:8080/v1',
                                  model='qwen3-4b', fallback_to_agy=False)
        config = router.load_scorer_config()
        self.assertEqual((config['jev_api_key'], config['api_key'], config['fallback_to_agy']), (SECRET, 'test-custom-key', False))
        # A Jev key is never resolved from a custom endpoint's api_key, and the custom key never from Jev's
        self.assertEqual(router.scorer_api_settings('jev', {'api_key': 'sk-custom', 'base_url': 'https://x.example'})[0], None)
        self.assertEqual(router.scorer_api_settings('jev', {'api_key': 'sk-bare'})[:2], ('sk-bare', 'file'))
        with patch.object(router, 'user_env', side_effect=lambda name: 'env-key' if name == 'JEV_API_KEY' else None):
            self.assertEqual(router.scorer_api_settings('jev', {})[:4], ('env-key', 'env:JEV_API_KEY', 'https://api.typesafe.ai', 'jev-latest'))
        router.save_scorer_config('jev', api_key='')
        self.assertNotIn('jev_api_key', router.load_scorer_config())
        for bad in (lambda: router.save_scorer_config('gpt-9'),
                    lambda: router.save_scorer_config('openai_compatible', base_url='', model=''),
                    lambda: router.save_scorer_config('jev', base_url='http://api.typesafe.ai'),
                    lambda: router.save_scorer_config('heuristic')):
            with self.assertRaises(ValueError):
                bad()
        with patch.dict(router.os.environ, {'ROUTER_SCORER_TYPE': 'heuristic'}):
            config = router.load_scorer_config()
            self.assertEqual((config['scorer_type'], 'heuristic' in config['config_error']), ('claude_haiku_low', True))
        with patch.dict(router.os.environ, {'ROUTER_SCORER_TYPE': 'nope'}):
            config = router.load_scorer_config()
            self.assertEqual((config['scorer_type'], 'nope' in config['config_error']), ('claude_haiku_low', True))
        self.path.write_text('not json', encoding='utf-8')
        self.assertEqual(router.load_scorer_config()['scorer_type'], 'claude_haiku_low')
        self.assertEqual((router.mask_secret(SECRET), router.mask_secret('short'), router.mask_secret(None)),
                         ('test…0000', '••••', ''))
        view = router.scorer_status({'scorer_type': 'jev', 'jev_api_key': SECRET})
        self.assertEqual((view['jev']['api_key'], view['jev']['key_source']), ('test…0000', 'file'))
        self.assertNotIn(SECRET, json.dumps(view))

    def test_jev_reads_the_whole_brief_and_answers_multi_task_and_task_count(self):
        objective = 'Two things: (1) fix the parser crash; (2) translate the README into Japanese.'
        request = router.build_jev_request({'objective': objective}, self.capabilities)
        self.assertEqual(request['state']['task'], {'objective': objective})
        questions = request['questions']
        self.assertEqual((questions['multi_task']['type'], questions['task_count']['type']), ('noul', 'choice'))
        self.assertEqual(list(questions['task_count']['criteria']), ['1', '2', '3', '>3'])
        answers = {**JEV_RESPONSE['answers'], 'multi_task': {'noul': 0.91}, 'task_count': {'choice': '2'}}
        profile = router.parse_jev_response({'answers': answers})
        self.assertEqual((profile['multi_task'], profile['task_count']), (True, '2'))

    def test_jev_request_matches_typesafe_systemone_contract(self):
        request = router.build_jev_request(self.task, self.capabilities)
        self.assertEqual((request['model'], set(request['state'])), ('jev-latest', {'task', 'rubric_notes'}))
        self.assertEqual(request['state']['task'], {'objective': self.task['objective']})
        self.assertIn('read_only_review', request['state']['rubric_notes'])
        questions = request['questions']
        self.assertEqual(set(questions), {'work_kind', 'interaction_style', 'reasoning_depth', 'code_complexity', 'needs_visual',
                                          'needs_gui', 'local_only', 'wants_fast', 'read_only_review', 'access',
                                          'effort_band', 'urgency', 'multi_task', 'task_count',
                                          'demand_divergence', 'demand_coding', 'demand_aesthetics', 'demand_tool_operation'})
        routes = set(self.capabilities['routes']) - {'other_or_mixed'}
        self.assertEqual((questions['work_kind']['type'], set(questions['work_kind']['criteria'])), ('choice', routes))
        self.assertTrue(all(isinstance(v, str) and v for v in questions['work_kind']['criteria'].values()))
        self.assertEqual(set(questions['interaction_style']['criteria']), set(task_intake.INTERACTION_STYLES))
        self.assertEqual((questions['access']['type'], set(questions['access']['criteria'])), ('choice', {'read-only', 'write'}))
        for name, (low, high) in task_intake.PROFILE_INTEGERS.items():
            q = questions[name]
            self.assertEqual((q['type'], len(q['criteria']), isinstance(q['criteria'], list)), ('score', high - low + 1, True), name)
            self.assertLessEqual(len(q['criteria']), 10)
        for name in ('needs_visual', 'needs_gui', 'local_only', 'wants_fast', 'read_only_review'):
            self.assertEqual(questions[name]['type'], 'noul')
            self.assertNotIn('criteria', questions[name])

    def test_jev_response_parsing_maps_every_dimension(self):
        profile = router.parse_jev_response(JEV_RESPONSE)
        self.assertEqual(profile, JEV_PROFILE)
        self.assertEqual(task_intake.validate_profile(profile), profile)
        # Scores clamp into range; a bare value answer is accepted; noul below 0.5 is false and needs_gui implies visual only when true
        answers = {k: dict(v) for k, v in JEV_RESPONSE['answers'].items()}
        answers['reasoning_depth']['score'] = 9
        answers['code_complexity']['score'] = 2.6
        answers['needs_gui'] = {'type': 'noul', 'noul': 0.49}
        answers['local_only'] = 0.5
        answers['work_kind'] = 'debug_review'
        answers['access'] = {'choice': 'write'}
        p2 = router.parse_jev_response({'answers': answers})
        self.assertEqual((p2['reasoning_depth'], p2['code_complexity'], p2['needs_gui'], p2['needs_visual'], p2['local_only'],
                          p2['work_kind'], p2['access']),
                         (4, 3, False, False, True, 'debug_review', 'write'))
        self.assertNotIn('reason', p2)
        for broken in ({'answers': {**answers, 'work_kind': {'choice': 'make_coffee'}}},
                       {'answers': {**answers, 'interaction_style': {'choice': 'vibes'}}},
                       {'answers': {**answers, 'access': {'choice': 'sudo'}}},
                       {'answers': {**answers, 'access': {'choice': 'read-only'}, 'code_complexity': {'score': 2.0}}},
                       {'answers': {k: v for k, v in answers.items() if k != 'demand_coding'}},
                       {'answers': {k: v for k, v in answers.items() if k != 'demand_tool_operation'}},
                       {'answers': {k: v for k, v in answers.items() if k != 'wants_fast'}},
                       {'answers': {k: v for k, v in answers.items() if k != 'read_only_review'}},
                       {'answers': {**answers, 'read_only_review': {'noul': 0.9}, 'code_complexity': {'score': 2.0}}},
                       {'answers': {**answers, 'demand_divergence': {'score': float('nan')}}},
                       {'error': 'x'}):
            with self.assertRaises(ValueError):
                router.parse_jev_response(broken)
        for bad_prof in (
            {k: v for k, v in GUI_PROFILE.items() if k != 'wants_fast'},
            {k: v for k, v in GUI_PROFILE.items() if k != 'read_only_review'},
            {**GUI_PROFILE, 'read_only_review': True, 'code_complexity': 1},
        ):
            with self.assertRaises(ValueError):
                task_intake.validate_profile(bad_prof)

    def test_jev_answers_effort_band_and_urgency_in_the_same_request(self):
        answers = {**JEV_RESPONSE['answers'], 'effort_band': {'choice': 'multi_file'}, 'urgency': {'choice': 'high'}}
        profile = router.parse_jev_response({'answers': answers})
        self.assertEqual((profile['expected_hours'], profile['urgency'], 'hours_confidence' in profile), (2.0, 'high', False))
        for value in ('minutes', None):  # unknown or missing answers leave the allocator's difficulty default in force
            plain = router.parse_jev_response({'answers': {**answers, 'effort_band': {'choice': value}, 'urgency': {'choice': value}}})
            self.assertNotIn('expected_hours', plain)
            self.assertNotIn('urgency', plain)

    def test_jev_scorer_dispatch_and_fallbacks(self):
        profile, meta, calls = self.score({'scorer_type': 'jev', 'jev_api_key': SECRET}, post=JEV_RESPONSE)
        self.assertEqual(profile, JEV_PROFILE)
        self.assertEqual((meta['source'], meta['scorer_type'], meta['provider'], meta['model']),
                         ('system_one', 'jev', 'typesafe', 'jev-2026-10-01'))
        self.assertEqual((calls['post'][0]['url'], calls['post'][0]['key'], calls['run']),
                         ('https://api.typesafe.ai/v1/systemone', SECRET, []))
        self.assertNotIn('chain', json.dumps(calls['post'][0]['body']))
        # Missing key: jev is absent on this host, so the next scorer in order answers and nothing counts as a failure
        _, meta, calls = self.score({'scorer_type': 'jev', 'fallback_to_agy': True})
        self.assertEqual((meta['source'], meta['scorer_type'], calls['post']), ('system_one', 'claude_haiku_low', []))
        self.assertNotIn('attempts', meta)
        # fallback_to_agy off, or all scorers failing: returns scorer_unavailable (None profile) so parent session chooses
        profile, meta, calls = self.score({'scorer_type': 'jev', 'fallback_to_agy': False})
        self.assertEqual((profile, meta['source'], meta['configured_scorer'], calls['run']), (None, 'scorer_unavailable', 'jev', []))
        self.assertNotIn('attempts', meta)
        profile, meta, _ = self.score({'scorer_type': 'jev', 'jev_api_key': SECRET},
                                      post=router.ScorerError('Scorer API answered HTTP 401.'), run=lambda c, d: (1, '', ''))
        self.assertEqual((profile, meta['source'], [a['scorer_type'] for a in meta['attempts']]),
                         (None, 'scorer_unavailable', ['jev', 'claude_haiku_low', 'codex_terra_low', 'agy_flash_low']))
        self.assertEqual(meta['fallback_detail'], 'Scorer API answered HTTP 401.')
        self.assertNotIn(SECRET, json.dumps(meta))
        with patch.dict(router.os.environ, {'ROUTER_DISABLE_LIVE_SCORER': '1'}):
            prof_dis, meta, calls = self.score({'scorer_type': 'jev', 'jev_api_key': SECRET, 'fallback_to_agy': False}, post=JEV_RESPONSE)
        self.assertEqual((prof_dis, meta['source'], calls['post']), (None, 'scorer_unavailable', []))

    def test_post_json_reports_http_failures_without_bodies(self):
        error = router.urllib.error.HTTPError('https://api.typesafe.ai/v1/systemone', 401, 'Unauthorized', {},
                                             io.BytesIO(b'{"detail":"bad key ts-live-0123"}'))
        with patch.object(router.urllib.request, 'urlopen', side_effect=error):
            with self.assertRaises(router.ScorerError) as caught:
                router.post_json('https://api.typesafe.ai/v1/systemone', {}, SECRET, 5)
        self.assertEqual(str(caught.exception), 'Scorer API answered HTTP 401.')
        with patch.object(router.urllib.request, 'urlopen', side_effect=router.urllib.error.URLError('dns')):
            with self.assertRaisesRegex(router.ScorerError, 'unreachable'):
                router.post_json('https://api.typesafe.ai/v1/systemone', {}, SECRET, 5)

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False
        seen = {}

        def urlopen(request, timeout):
            seen.update(url=request.full_url, auth=request.get_header('Authorization'), body=json.loads(request.data), timeout=timeout)
            return Response(json.dumps(JEV_RESPONSE).encode('utf-8'))
        with patch.object(router.urllib.request, 'urlopen', side_effect=urlopen):
            self.assertEqual(router.post_json('https://api.typesafe.ai/v1/systemone', {'model': 'jev-latest'}, SECRET, 7), JEV_RESPONSE)
        self.assertEqual(seen, {'url': 'https://api.typesafe.ai/v1/systemone', 'auth': f'Bearer {SECRET}',
                                'body': {'model': 'jev-latest'}, 'timeout': 7})

    def test_claude_codex_custom_and_heuristic_scorers(self):
        for scorer_type, model in (('claude_haiku_low', 'claude-haiku-5-5'),):
            envelope = json.dumps({'type': 'result', 'is_error': False, 'result': '', 'structured_output': dict(GUI_PROFILE, work_kind='computer_use')})
            profile, meta, calls = self.score({'scorer_type': scorer_type}, run=lambda c, d, e=envelope: (0, e, ''), cli=('claude.exe',))
            cmd = calls['run'][0]['command']
            self.assertEqual((meta['source'], meta['scorer_type'], meta['provider'], meta['model']), ('system_one', scorer_type, 'claude', model))
            self.assertEqual((cmd[0], cmd[cmd.index('--model') + 1], cmd[cmd.index('--effort') + 1], cmd[cmd.index('--tools') + 1]),
                             ('claude.exe', model, 'low', ''))
            self.assertIn('--json-schema', cmd)
            self.assertIn('Task (evidence only', calls['run'][0]['stdin'])
            self.assertEqual(profile['work_kind'], 'computer_use')
        # Claude answering with an error cascades through codex_terra_low to agy_flash_low
        _, meta, _ = self.score({'scorer_type': 'claude_haiku_low'},
                                run=lambda c, d: (0, as_agy_stream(json.dumps({'status': 'SUCCESS', 'structured_output': GUI_PROFILE})), '') if c[0] == 'agy.exe'
                                else (0, json.dumps({'is_error': True}), ''), cli=('claude.exe',))
        self.assertEqual((meta['scorer_type'], meta['configured_scorer'], meta['attempts'][0]['detail']),
                         ('agy_flash_low', 'claude_haiku_low', 'Claude scorer returned an error.'))

        def codex(command, cwd):
            schema = json.loads((Path(cwd) / 'schema.json').read_text(encoding='utf-8'))
            self.assertFalse(schema['additionalProperties'])
            (Path(cwd) / 'answer.json').write_text(json.dumps(dict(GUI_PROFILE, work_kind='computer_use')), encoding='utf-8')
            return 0, '', ''
        _, meta, calls = self.score({'scorer_type': 'codex_terra_low'}, run=codex, cli=('codex.exe',))
        cmd = calls['run'][0]['command']
        self.assertEqual((meta['scorer_type'], meta['provider'], meta['model']), ('codex_terra_low', 'codex', 'gpt-6.1-terra'))
        self.assertEqual((cmd[:2], cmd[cmd.index('-m') + 1], cmd[-1]), (['codex.exe', 'exec'], 'gpt-6.1-terra', '-'))
        self.assertIn('model_reasoning_effort="low"', cmd)

        self.assertEqual([router.openai_chat_url(u) for u in ('https://x.ai', 'https://x.ai/v1/', 'https://x.ai/api/chat/completions')],
                         ['https://x.ai/v1/chat/completions', 'https://x.ai/v1/chat/completions', 'https://x.ai/api/chat/completions'])
        content = 'Here you go:\n' + json.dumps(dict(GUI_PROFILE, work_kind='computer_use'))
        reply = {'choices': [{'message': {'role': 'assistant', 'content': content}}]}
        custom = {'scorer_type': 'openai_compatible', 'api_key': 'sk-custom', 'base_url': 'https://llm.example.com/v1', 'model': 'qwen3-4b'}
        profile, meta, calls = self.score(custom, post=reply)
        body = calls['post'][0]['body']
        self.assertEqual((meta['scorer_type'], meta['provider'], meta['model'], profile['work_kind']),
                         ('openai_compatible', 'openai_compatible', 'qwen3-4b', 'computer_use'))
        self.assertEqual((calls['post'][0]['url'], calls['post'][0]['key'], body['model'], body['response_format']),
                         ('https://llm.example.com/v1/chat/completions', 'sk-custom', 'qwen3-4b', {'type': 'json_object'}))
        self.assertTrue(body['messages'][0]['content'].startswith(router.SCORER_PROMPT))
        prof_no_key, meta, calls = self.score({**custom, 'api_key': '', 'fallback_to_agy': False}, post=reply)
        self.assertEqual((prof_no_key, meta['source'], calls['post']), (None, 'scorer_unavailable', []))
        prof_empty, meta, _ = self.score({**custom, 'fallback_to_agy': False}, post={'choices': []})
        self.assertEqual((prof_empty, meta['source'], meta['fallback_reason']), (None, 'scorer_unavailable', 'ValueError'))

        # Priority cascade order: jev (when key present) -> claude_haiku_low -> codex_terra_low -> agy_flash_low
        self.assertEqual(router.scorer_attempt_order({'scorer_type': 'claude_haiku_low', 'fallback_to_agy': True}),
                         ('claude_haiku_low', ['claude_haiku_low', 'codex_terra_low', 'agy_flash_low']))
        self.assertEqual(router.scorer_attempt_order({'scorer_type': 'claude_haiku_low', 'jev_api_key': SECRET, 'fallback_to_agy': True}),
                         ('claude_haiku_low', ['jev', 'claude_haiku_low', 'codex_terra_low', 'agy_flash_low']))

    def test_score_task_reads_the_configured_scorer_and_scorer_cli(self):
        router.save_scorer_config('claude_haiku_low')

        def cli(*argv, stdin=None):
            out = io.StringIO()
            with patch.object(sys, 'argv', ['route_judge.py', 'scorer', *argv]), contextlib.redirect_stdout(out), \
                    patch.object(sys, 'stdin', io.StringIO(stdin or '')):
                code = router.main()
            return code, json.loads(out.getvalue()), out.getvalue()
        code, shown, _ = cli()
        self.assertEqual((code, shown['action'], shown['scorer_type'], shown['config_path']), (0, 'show', 'claude_haiku_low', str(self.path)))
        code, saved, text = cli('--action', 'set', '--type', 'jev', '--api-key', '-', stdin=SECRET + '\n')
        self.assertEqual((code, saved['scorer_type'], saved['jev']['api_key'], saved['jev']['key_source']), (0, 'jev', 'test…0000', 'file'))
        self.assertNotIn(SECRET, text)
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['jev_api_key'], SECRET)
        code, failed, _ = cli('--action', 'set')
        self.assertEqual((code, failed['detail']), (1, 'scorer set requires --type.'))
        with patch.object(router, 'post_json', side_effect=router.ScorerError('Scorer API answered HTTP 401.')), \
                patch.object(router, 'cli_prefix', side_effect=RuntimeError('cli missing')), \
                patch.object(router, 'scorer_prefix', side_effect=RuntimeError('agy missing')):
            code, tested, _ = cli('--action', 'test')
        self.assertEqual((code, tested['ok'], tested['scorer']['source'], tested['scorer']['fallback_detail']),
                         (1, False, 'scorer_unavailable', 'Scorer API answered HTTP 401.'))
        with patch.object(router, 'post_json', return_value=JEV_RESPONSE):
            code, tested, _ = cli('--action', 'test')
        self.assertEqual((code, tested['ok'], tested['task_profile']), (0, True, JEV_PROFILE))


class ScorerAccountTests(unittest.TestCase):
    """The cascade keeps its order but runs each CLI scorer on an account this host has; absent ones are skipped."""

    INVENTORY = {'accounts': {
        'claude': {'family': 'claude', 'status': 'disabled', 'home': 'C:/u/.claude'},
        'claude-2': {'family': 'claude', 'status': 'ready', 'home': 'C:/u/.claude-2', 'billing': 'subscription'},
        'llmproxy': {'family': 'codex', 'status': 'ready', 'billing': 'metered'},
        'antigravity': {'family': 'antigravity', 'status': 'ready'},
    }}

    def setUp(self):
        self.capabilities = reference_capabilities()
        env = patch.dict(router.os.environ, {'ROUTER_SCORER_CONFIG': str(temporary_root(self.addCleanup) / 'scorer.json'),
                                             'ROUTER_SCORER_TYPE': '', 'ROUTER_DISABLE_LIVE_SCORER': '',
                                             'ROUTER_SCORE_CACHE': 'off'})
        env.start()
        self.addCleanup(env.stop)
        self.task = {'objective': 'Click Save in the local app and take a screenshot', 'work_kind': 'tool_execution'}
        real = patch.object(router, 'scorer_accounts', side_effect=_real_scorer_accounts)
        real.start()
        self.addCleanup(real.stop)

    def test_lists_every_ready_allowed_subscription_account_of_the_family(self):
        rows = self.INVENTORY
        # LITTLESPRING: ~/.claude signed out, ~/.claude-2 signed in.
        self.assertEqual(router.scorer_accounts('claude_haiku_low', rows, None), ['claude-2'])
        both = {'accounts': {**rows['accounts'], 'claude': {**rows['accounts']['claude'], 'status': 'ready'}}}
        self.assertEqual(router.scorer_accounts('claude_haiku_low', both, None), ['claude', 'claude-2'])
        self.assertEqual(router.scorer_accounts('agy_flash_low', rows, None), ['antigravity'])
        self.assertEqual(router.scorer_accounts('codex_terra_low', rows, None), [])     # a metered proxy is not one
        self.assertEqual(router.scorer_accounts('claude_haiku_low', both, {'allowed_accounts': ['claude-2']}), ['claude-2'])
        self.assertEqual(router.scorer_accounts('jev', rows, None), [None])

    def test_absent_scorers_are_skipped_in_order_without_errors(self):
        used = []

        def fake_run(command, cwd, prompt=None, timeout=120, account=None):
            used.append((command[0], account))
            if command[0] == 'agy.exe':
                return 0, as_agy_stream(json.dumps({'status': 'SUCCESS', 'structured_output': GUI_PROFILE})), ''
            return 0, json.dumps({'structured_output': GUI_PROFILE}), ''

        with patch.object(router.provider_inventory, 'current', return_value=self.INVENTORY), \
                patch.object(router.host_policy, 'load_policy', return_value=None), \
                patch.object(router, 'run_process', side_effect=fake_run), \
                patch.object(router, 'scorer_prefix', return_value=['agy.exe']), \
                patch.object(router, 'cli_prefix', return_value=['cli.exe']):
            _, meta = router.score_task(self.task, self.capabilities, scorer_config={})
        self.assertEqual(used, [('cli.exe', 'claude-2')])
        self.assertEqual((meta['source'], meta['scorer_type'], meta['account']), ('system_one', 'claude_haiku_low', 'claude-2'))
        self.assertNotIn('attempts', meta)
        # Both Claude accounts signed in and the first fails: the next Claude account answers before Codex is tried.
        both = {'accounts': {**self.INVENTORY['accounts'],
                             'claude': {**self.INVENTORY['accounts']['claude'], 'status': 'ready'}}}
        used.clear()
        homes = iter([(1, '', ''), (0, json.dumps({'structured_output': GUI_PROFILE}), '')])
        with patch.object(router.provider_inventory, 'current', return_value=both),                 patch.object(router.host_policy, 'load_policy', return_value=None),                 patch.object(router, 'run_process', side_effect=lambda c, d, p=None, t=120, account=None:
                             (used.append((c[0], account)), next(homes))[1]),                 patch.object(router, 'cli_prefix', return_value=['cli.exe']):
            _, meta = router.score_task(self.task, self.capabilities, scorer_config={})
        self.assertEqual(used, [('cli.exe', 'claude'), ('cli.exe', 'claude-2')])
        self.assertEqual((meta['account'], [(a['scorer_type'], a['account']) for a in meta['attempts']]),
                         ('claude-2', [('claude_haiku_low', 'claude')]))
        no_claude = {'accounts': {k: v for k, v in self.INVENTORY['accounts'].items() if not k.startswith('claude')}}
        used.clear()
        with patch.object(router.provider_inventory, 'current', return_value=no_claude), \
                patch.object(router.host_policy, 'load_policy', return_value=None), \
                patch.object(router, 'run_process', side_effect=fake_run), \
                patch.object(router, 'scorer_prefix', return_value=['agy.exe']), \
                patch.object(router, 'cli_prefix', return_value=['cli.exe']):
            _, meta = router.score_task(self.task, self.capabilities, scorer_config={})
        self.assertEqual([c for c, _ in used], ['agy.exe'])        # claude and codex absent: agy, no error recorded
        self.assertEqual(meta['scorer_type'], 'agy_flash_low')
        self.assertNotIn('attempts', meta)

    def test_subscription_env_points_codex_and_claude_clis_at_the_chosen_home(self):
        rows = {'accounts': {'codex-2': {'family': 'codex', 'home': 'C:/u/.codex-2'},
                             'claude-2': {'family': 'claude', 'home': 'C:/u/.claude-2'}}}
        with patch.object(router.provider_inventory, 'current', return_value=rows):
            self.assertEqual(router.subscription_env('codex-2')['CODEX_HOME'], 'C:/u/.codex-2')
            self.assertEqual(router.subscription_env('claude-2')['CLAUDE_CONFIG_DIR'], 'C:/u/.claude-2')

    def test_compact_rubric_column_union_and_prompt_stays_off_the_command_line(self):
        import subprocess

        rubric_het = {
            'calibration_cases': [
                {'id': 'a', 'objective': 'o1', 'expected_profile': {'work_kind': 'general_implementation', 'reasoning_depth': 2}},
                {'id': 'b', 'objective': 'o2', 'expected_profile': {'work_kind': 'computer_use', 'reasoning_depth': 1, 'needs_gui': True}},
            ]
        }
        compact = router._compact_rubric_for_prompt(rubric_het)
        self.assertEqual(compact['calibration_columns'][2], ['work_kind', 'reasoning_depth', 'needs_gui'])
        self.assertEqual(compact['calibration_cases'][0][2], ['general_implementation', 2, None])
        self.assertEqual(compact['calibration_cases'][1][2], ['computer_use', 1, True])

        capabilities = reference_capabilities()
        schema = router.scorer_schema(capabilities)
        prompt = router.scorer_prompt({
            'objective': 'Implement a live order-book dashboard and run unit tests',
            'context': 'Frontend React + WebSocket module',
            'environment': 'Windows 11, Python 3.13',
        })
        # The limit that remains is the scorer model's context window, not a command line. The smallest window of any
        # System-One scorer is taken as 200k tokens (accounts.json cites 500k for the Codex scorer; the others are not
        # cited, so the floor is a stated assumption); the rubric and calibration cases may use a quarter of it, at the
        # conservative 2 characters per token that CJK text needs.
        context_floor_tokens, share, chars_per_token = 200_000, 0.25, 2
        budget_chars = int(context_floor_tokens * share * chars_per_token)
        self.assertLessEqual(len(prompt), budget_chars,
                             f'the scorer prompt is {len(prompt)} chars; the budget is {budget_chars} '
                             f'({share:.0%} of a {context_floor_tokens}-token context at {chars_per_token} chars/token)')
        # Nothing of the prompt rides the command line: it is the same size whether the rubric holds 1 or 1000 cases.
        small = router.scorer_stream_command(['agy.cmd'], schema)
        with patch.object(router, 'load_classifier_cases', return_value={'calibration_cases': [
                {'id': f'c{i}', 'task': 'x' * 500, 'expected_profile': {'work_kind': 'debug_review'}} for i in range(1000)]}):
            big_prompt = router.scorer_prompt({'objective': 'x'})
        self.assertGreater(len(big_prompt), 500_000)
        self.assertEqual(router.scorer_stream_command(['agy.cmd'], schema), small)
        self.assertLess(len(subprocess.list2cmdline(small)), 30000)
        self.assertFalse(hasattr(router, 'ARGV_LIMIT_CHARS'))


@unittest.skipUnless(router.os.environ.get('ROUTER_LIVE_AGY') == '1', 'set ROUTER_LIVE_AGY=1 to call the real agy CLI')
class LiveSystemOneSmokeTests(unittest.TestCase):
    # Capture only for the explicit opt-in; ordinary tests never restore host state.
    LIVE_ENV = dict(router.os.environ) if router.os.environ.get('ROUTER_LIVE_AGY') == '1' else {}
    LIVE_HOME = Path.home() if LIVE_ENV else None
    TASKS = {
        'gui_click': ({'objective': 'Click the Save button in the Settings dialog of the local desktop app and take a screenshot to confirm.',
                       'work_kind': 'computer_use', 'needs': {'local_only': True, 'local_only_reason': 'local desktop app screen'}},
                      lambda p: p['reasoning_depth'] <= 2 and p['needs_gui']),
        'one_file_edit': ({'objective': 'In tray/deploy.py change the default retry count constant from 3 to 5 and update the one unit test that asserts it.',
                           'work_kind': 'general_implementation', 'needs': {'local_only': True, 'local_only_reason': 'local pytest suite'}},
                          lambda p: p['reasoning_depth'] <= 2 and not p['needs_gui']),
        'core_refactor': ({'objective': 'Refactor the order-matching engine across the core library, the risk module and the gateway so all three '
                                        'share one lock-free event queue, preserving latency and ordering invariants, and migrate every caller.',
                           'needs': {'local_only': True, 'local_only_reason': 'local repository and benchmarks'}},
                          lambda p: p['reasoning_depth'] >= 3 and p['code_complexity'] >= 2),
    }
    TIMEOUT = 60

    def test_real_scores_and_plans(self):
        capabilities = reference_capabilities()
        for name, (task, plausible) in self.TASKS.items():
            with self.subTest(task=name), patch.dict(router.os.environ, self.LIVE_ENV, clear=True), \
                    patch.object(Path, 'home', return_value=self.LIVE_HOME):
                profile, meta = router.score_task(task, capabilities, timeout=self.TIMEOUT,
                                                   scorer_config={'scorer_type': 'agy_flash_low'})
            self.assertEqual(meta['source'], 'system_one', (name, meta))
            self.assertTrue(plausible(profile), (name, profile))


if __name__ == '__main__':
    unittest.main()
