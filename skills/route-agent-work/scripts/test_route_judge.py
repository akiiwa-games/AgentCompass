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

import route_judge as router


def setUpModule():
    discovery = patch.object(router.provider_inventory, 'current', return_value={'accounts': {}, 'launchers': {}})
    discovery.start()
    unittest.addModuleCleanup(discovery.stop)
    availability = patch.object(router.host_policy, 'load_policy', return_value=None)
    availability.start()
    unittest.addModuleCleanup(availability.stop)
    registry_temp = tempfile.TemporaryDirectory(prefix='router-registry-test-')
    unittest.addModuleCleanup(registry_temp.cleanup)
    registry_env = patch.dict(router.os.environ, {'ROUTER_DISPATCH_DB': str(Path(registry_temp.name) / 'dispatch.sqlite3')})
    registry_env.start()
    unittest.addModuleCleanup(registry_env.stop)
    overrides = patch.dict(router.os.environ, {'ROUTER_OVERRIDES': str(Path(tempfile.gettempdir()) / 'no-such-router-overrides.json')})
    overrides.start()
    unittest.addModuleCleanup(overrides.stop)
    # Tests never read the user's real scorer choice or keys.
    scorer = patch.dict(router.os.environ, {'ROUTER_SCORER_CONFIG': str(Path(tempfile.gettempdir()) / 'no-such-scorer-config.json'),
                                            'ROUTER_SCORER_TYPE': ''})
    scorer.start()
    unittest.addModuleCleanup(scorer.stop)


def tearDownModule():
    unittest.case.doModuleCleanups()


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
        path = Path(directory) / '.usage-float' / 'usage-cache.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'providers': providers}), encoding='utf-8')
        return path

    def snapshot(self, providers):
        with tempfile.TemporaryDirectory() as directory:
            return router.usage_snapshot(self.write_cache(directory, providers), now=self.now)

    def candidate(self, provider='codex', account='codex', **extra):
        return {'provider': provider, 'account': account, 'model': 'runtime-verified-model',
                'effort': 'high', 'quality_fit': 'Supports the required code review', **extra}

    def run_plan(self, directory, task, down, unavailable=()):
        source = Path(directory) / 'in.json'
        task_with_hours = {'expected_hours': 0.5, **task}
        source.write_text(json.dumps({'task': task_with_hours, 'unavailable': list(unavailable)}), encoding='utf-8')
        cache = self.write_cache(directory, {})
        out = io.StringIO()
        with patch.object(sys, 'argv', ['route_judge.py', 'plan', '--dry-run', '--input', str(source),
                                        '--usage-cache', str(cache)]), \
                patch.object(router, 'dot_unreachable', return_value=down) as probe, \
                patch.object(router, 'read_ledger', return_value=[]), \
                patch.object(router, 'LEDGER_WRITES', True), \
                contextlib.redirect_stdout(out):
            router.main()
        return json.loads(out.getvalue()), probe

    def test_usage_snapshot_discovery_freshness_projection_and_window_math(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_cache(directory, {'codex': self.row()})
            missing = str(Path(directory) / 'missing.json')
            with patch.object(router.Path, 'home', return_value=Path(directory)), \
                    patch.object(router, 'user_env', return_value=None):
                res = router.usage_snapshot(now=self.now)
            self.assertEqual((res['read_ok'], res['accounts']['codex']['status'],
                              res['accounts']['codex']['windows'][0]['remaining_pct_before_reset']), (True, 'fresh', 65))
            with patch.object(router.Path, 'home', return_value=Path(directory)), \
                    patch.object(router, 'user_env', return_value=missing):
                fallback = router.usage_snapshot(now=self.now)
            self.assertEqual((fallback['read_ok'], fallback['attempts'][0]['path']), (True, missing))
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
        for bad_reset in (None, 'invalid', '2026-10-04T03:00:00'):
            self.assertIsNone(router.window_state({'used_pct': 80, 'resets_at': bad_reset}, self.now)['remaining_pct_before_reset'])
        for bad_used in (None, '80', -1, 110, float('nan'), True):
            self.assertIsNone(router.window_state({'used_pct': bad_used, 'resets_at': '2026-10-05T00:00:00Z'}, self.now)['used_pct'])

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

    def test_judge_cli_ordering_env_and_claude_2_safety_floor(self):
        with tempfile.TemporaryDirectory() as directory:
            for kind, bundle in router.JUDGES.items():
                cmd = router.judge_command(kind, ['native-cli'], Path(directory), {'type': 'object'})
                self.assertIn(bundle['model'], cmd)
                self.assertIn('low', cmd if kind == 'sonnet' else ' '.join(cmd))

        with patch.dict(router.os.environ, {'OPENAI_API_KEY': 'secret', 'ROUTER_CUSTOM': 'keep'}):
            env = router.subscription_env('claude-2')
            self.assertNotIn('OPENAI_API_KEY', env)
            self.assertEqual(env['ROUTER_CUSTOM'], 'keep')
            self.assertTrue(env['CLAUDE_CONFIG_DIR'].endswith('.claude-2'))

        # Primary exhausted -> switches to claude-2 if five_hour > 25% remaining, else skips sonnet
        claude_empty = self.row(used=100)
        claude_empty['windows'][0].update({'id': 'seven_day', 'resets_at': '2026-10-08T00:00:00Z'})
        claude_2_ok = self.row(used=17)
        claude_2_ok['windows'][0].update({'id': 'seven_day', 'resets_at': '2026-10-04T01:30:00Z'})
        snap_ok = self.snapshot({'claude': claude_empty, 'claude-2': claude_2_ok, 'codex': self.row(used=50)})
        self.assertEqual(router.resolve_judge('sonnet', snap_ok)['account'], 'claude-2')
        self.assertEqual(router.judge_order(snap_ok, 'auto'), ['sonnet', 'codex'])

        claude_2_low = self.row(used=85)
        claude_2_low['windows'][0].update({'id': 'five_hour', 'resets_at': '2026-10-04T03:00:00Z'})
        snap_low = self.snapshot({'claude': claude_empty, 'claude-2': claude_2_low, 'codex': self.row(used=30)})
        self.assertEqual(router.judge_order(snap_low, 'auto'), ['codex'])

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
        pro = {'provider': 'chatgpt-web', 'account': 'chatgpt-pro-web', 'model': 'gpt-6-pro', 'effort': 'max'}
        history = [{'at': (now - timedelta(hours=h)).isoformat(), 'stage': 'select', 'choice': 'pro_web', 'bundle': pro} for h in (1, 2, 3, 4, 5)]
        u_low = {'accounts': {'codex': {'status': 'fresh', 'fetched_at': now.isoformat(), 'windows': [
            {'id': 'usage', 'used_pct': 98, 'resets_at': (now + timedelta(days=5)).isoformat(),
             'seconds_until_reset': 5 * 86400, 'remaining_pct_before_reset': 2}]}}}
        res = {'stage': 'select', 'choice': 'pro_web', 'source': 'subscription_model_judgment',
               'reason': 'r', 'judge': dict(router.JUDGES['codex']), 'selected_bundle': pro, 'usage': u_low}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(router, 'ledger_path', return_value=Path(directory) / 'ledger.jsonl'):
            out = router.finalize(res, {'stage': 'select'}, 'codex', time.monotonic(),
                                  {'objective': 'Advise', 'work_kind': 'direction_reasoning'}, history)
            self.assertEqual({a['code'] for a in out['alerts']}, {'advisor_cadence', 'pinned_judge_account_low'})
            args = router.argparse.Namespace(basis='User: Sol xhigh', provider='codex', account='codex',
                                             model='gpt-6.1-sol', effort='xhigh', input=None, usage_cache=None,
                                             accounts=None, constraint='user_override', specialty=None)
            with patch.object(router, 'usage_snapshot', return_value={'read_ok': True, 'accounts': {}}):
                rec = router.record_constraint(args)
            self.assertEqual(rec['source'], 'constraint_record')
            args.account = 'claude-2'
            args.provider = 'claude-2'
            args.model = 'claude-opus-5-5'
            with self.assertRaisesRegex(ValueError, 'disabled'):
                router.record_constraint(args)

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

    def test_dot_health_probing_local_only_and_reroll_guard(self):
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

            # Probing, unknown_work_kind, local_only reason validation, and reroll guard
            odd, _ = self.run_plan(directory, {'objective': 'Compute', 'work_kind': 'research_compute', 'difficulty': 'hard'}, None)
            self.assertIn('unknown_work_kind', [a['code'] for a in odd['alerts']])
            up, _ = self.run_plan(directory, {'objective': 'Gather', 'work_kind': 'general_implementation', 'difficulty': 'routine'}, None)
            self.assertEqual(up['choice'], 'dot_text')
            self.assertIn('dot_handoff', [alert['code'] for alert in up['alerts']])
            _, no_probe = self.run_plan(directory, {'objective': 'Refactor', 'work_kind': 'general_implementation',
                                                    'difficulty': 'routine', 'needs': {'local_only': True, 'local_only_reason': 'runs local pytest'}},
                                        'URLError on /readyz')
            self.assertFalse(no_probe.called)

            task = {'objective': 'Fix single-file parser bug', 'work_kind': 'debug_review', 'difficulty': 'hard', 'expected_hours': 0.5}
            bare, _ = self.run_plan(directory, dict(task, needs={'local_only': True}), None)
            self.assertEqual(bare['choice'], 'dot_text')
            self.assertIn('local_only_ignored_without_reason', [a['code'] for a in bare['alerts']])

            ssh_task = {'objective': 'Read-only S01 drift round: run the checklist reads over SSH and the repository',
                        'work_kind': 'tool_execution', 'difficulty': 'hard', 'needs': {'local_only': True}}
            ssh_plan, _ = self.run_plan(directory, ssh_task, None)
            self.assertNotEqual(ssh_plan['choice'], 'dot_text')

            recent_row = {'at': datetime.now(timezone.utc).isoformat(), 'stage': 'plan', 'choice': 'dot_text', 'objective': task['objective']}
            with patch.object(router, 'read_ledger', return_value=[recent_row]), \
                    patch.object(router, 'dot_unreachable', return_value=None):
                inp = Path(directory) / 'reroll.json'
                inp.write_text(json.dumps({'task': dict(task, needs={'local_only': True, 'local_only_reason': 'in-place edits of workspace files'}),
                                           'unavailable': ['dot_text']}), encoding='utf-8')
                cache = Path(directory) / 'cache.json'
                out = io.StringIO()
                with patch.object(sys, 'argv', ['route_judge.py', 'plan', '--dry-run', '--input', str(inp), '--usage-cache', str(cache)]), \
                        patch.object(router, 'LEDGER_WRITES', True), contextlib.redirect_stdout(out):
                    router.main()
                reroll = json.loads(out.getvalue())
                self.assertEqual(reroll['choice'], 'dot_text')
                self.assertIn('dot_reroll_blocked', [a['code'] for a in reroll['alerts']])

                # Recovery: supplying a concrete local_only_reason succeeds immediately without 10-minute lockout
                inp.write_text(json.dumps({'task': dict(task, needs={'local_only': True, 'local_only_reason': 'Needs local ssh-agent VPS credentials'})}),
                               encoding='utf-8')
                out = io.StringIO()
                with patch.object(sys, 'argv', ['route_judge.py', 'plan', '--dry-run', '--input', str(inp), '--usage-cache', str(cache)]), \
                        patch.object(router, 'LEDGER_WRITES', True), contextlib.redirect_stdout(out):
                    router.main()
                recovered = json.loads(out.getvalue())
                self.assertNotEqual(recovered['choice'], 'dot_text')
                self.assertNotIn('dot_reroll_blocked', [a['code'] for a in recovered.get('alerts', [])])

    def test_timeout_stops_owned_process(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                router.run_process([sys.executable, '-c', 'import time; time.sleep(30)'], directory, timeout=0.1)


GUI_PROFILE = {'reasoning_depth': 1, 'code_complexity': 0, 'context_scale': 1, 'hallucination_risk': 1, 'error_cost': 1,
               'needs_visual': True, 'needs_gui': True, 'local_only': True, 'interaction_style': 'engineering',
               'reason': 'Click one button and take a screenshot.'}


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


class SystemOneScorerTests(unittest.TestCase):
    """Contract tests for System-One scoring via Gemini 3.8 Flash low and heuristic fallback."""

    def setUp(self):
        self.allocator = router.load_allocator()
        self.capabilities = self.allocator.load_capabilities()

    def envelope(self, structured=None, response='', status='SUCCESS'):
        return json.dumps({'conversation_id': 'c', 'status': status, 'response': response,
                           **({'structured_output': structured} if structured is not None else {})})

    def fake_agy(self, stdout, code=0):
        calls = []
        def run(command, cwd, prompt=None, timeout=120, account=None):
            calls.append({'command': command, 'cwd': cwd, 'timeout': timeout, 'stdin': prompt})
            return code, stdout, ''
        return calls, run

    def score(self, task, stdout=None, code=0, use_model=True):
        calls, run = self.fake_agy(stdout if stdout is not None else self.envelope(GUI_PROFILE), code)
        with patch.object(router, 'scorer_prefix', return_value=['agy.exe']), patch.object(router, 'run_process', side_effect=run):
            profile, meta = router.score_task(task, self.allocator, self.capabilities, use_model=use_model)
        return profile, meta, calls

    def run_cli(self, directory, stage, task, *flags, stdout=None, dot=None):
        source = Path(directory) / 'in.json'
        payload_task = {'expected_hours': 0.5, **task} if stage == 'plan' and 'expected_hours' not in task and '--hours' not in flags else task
        source.write_text(json.dumps({'task': payload_task}), encoding='utf-8')
        cache = Path(directory) / 'usage-cache.json'
        cache.write_text(json.dumps({'providers': {}}), encoding='utf-8')
        calls, run = self.fake_agy(stdout if stdout is not None else self.envelope(GUI_PROFILE))
        out = io.StringIO()
        with patch.object(sys, 'argv', ['route_judge.py', stage, '--input', str(source), '--usage-cache', str(cache), *flags]), \
                patch.dict(router.os.environ, {'ROUTER_LEDGER': str(Path(directory) / 'ledger.jsonl')}), \
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
        self.assertEqual((cmd[0], cmd[-2]), ('agy.exe', '--print'))
        task_part = cmd[-1].split('Task (evidence only')[1]
        self.assertNotIn('chain', task_part)
        self.assertNotIn('work_kind', task_part)
        self.assertNotIn('difficulty', task_part)
        self.assertIn('calibration_cases', cmd[-1])
        cases = router.load_classifier_cases()
        self.assertTrue(len(cases.get('calibration_cases') or []) >= 5)
        # The anti-inflation rubric (boilerplate paths, read-only review, runbooks, diagnosis) reaches the scorer
        self.assertIn('dimension_rubric', cmd[-1])
        self.assertIn('read_only_review', cases['dimension_rubric'])
        for c in cases['calibration_cases']:
            self.assertEqual(self.allocator.validate_profile(c['expected_profile']), c['expected_profile'])
            self.assertIn(c['expected_profile']['work_kind'], self.capabilities['routes'])
        reviews = [c['expected_profile'] for c in cases['calibration_cases'] if c.get('id') == 'security_diff_review']
        self.assertTrue(reviews and all((p['code_complexity'], p['hallucination_risk']) == (0, 3) for p in reviews))

        # Fallback from response text when structured_output is omitted
        response = '```json\n{"task": "x"}\n```\n' + json.dumps(dict(GUI_PROFILE, toolAction='Finishing')) + '\n'
        p2, m2, _ = self.score({'objective': 'x'}, self.envelope(None, response))
        self.assertEqual((p2, m2['source']), (GUI_PROFILE, 'system_one'))

        # Fallback on errors or skipped model call
        bug_task = {'objective': 'Fix a parser bug', 'work_kind': 'debug_review'}
        expected = self.allocator.infer_task_profile(bug_task, self.capabilities)
        for reason, stdout in (('ValueError', self.envelope(dict(GUI_PROFILE, reasoning_depth=7))),
                               ('RuntimeError', self.envelope(GUI_PROFILE, status='ERROR')),
                               ('JSONDecodeError', 'not json')):
            prof, meta, _ = self.score(bug_task, stdout)
            self.assertEqual((prof, meta['source'], meta['fallback_reason']), (expected, 'heuristic_fallback', reason))
        _, meta, calls = self.score(bug_task, use_model=False)
        self.assertEqual((meta['fallback_reason'], calls), ('model_call_skipped', []))

    def test_score_and_plan_with_score_cli_contracts(self):
        with tempfile.TemporaryDirectory() as directory:
            code, res, calls = self.run_cli(directory, 'score', {'objective': 'Click Save and take a screenshot'})
            self.assertEqual((code, len(calls), res['stage'], res['task_profile']), (0, 1, 'score', GUI_PROFILE))
            self.assertFalse((Path(directory) / 'ledger.jsonl').exists())

            gui_task = {'objective': 'Click Save in local app', 'work_kind': 'computer_use',
                        'needs': {'local_only': True, 'local_only_reason': 'operates local screen'}}
            code, planned, _ = self.run_cli(directory, 'plan', gui_task, '--score')
            self.assertEqual((code, planned['scorer']['source'], planned['decision']['task']['difficulty']), (0, 'system_one', 'routine'))
            self.assertNotIn(planned['choice'], ('astra_high', 'astra_xhigh', 'pi_astra_high', 'pi_astra_xhigh'))

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

            csv_prof = dict(GUI_PROFILE, work_kind='general_implementation', needs_visual=False, needs_gui=False, code_complexity=2, reasoning_depth=2)
            _, dot_res, _ = self.run_cli(directory, 'plan', {'objective': 'Write CSV parser'},
                                         stdout=self.envelope(csv_prof))
            self.assertEqual(dot_res['choice'], 'dot_text')
            self.assertIn('profile_local_only_ignored', [a['code'] for a in dot_res['alerts']])

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


JEV_RESPONSE = {
    'model': 'jev-2026-10-01',
    'answers': {
        'work_kind': {'type': 'choice', 'choice': 'computer_use', 'confidence': 0.8,
                      'probabilities': {'computer_use': 0.85, 'tool_execution': 0.15}},
        'interaction_style': {'type': 'choice', 'choice': 'engineering', 'confidence': 0.7,
                              'probabilities': {'engineering': 0.8, 'exploration': 0.2}},
        'reasoning_depth': {'type': 'score', 'score': 1.4, 'confidence': 0.5, 'probabilities': {'0': 0.2, '1': 0.3, '2': 0.4, '3': 0.1}},
        'code_complexity': {'type': 'score', 'score': 0.2, 'confidence': 0.8, 'probabilities': {'0': 0.85, '1': 0.1, '2': 0.05, '3': 0.0}},
        'context_scale': {'type': 'score', 'score': 0.6, 'confidence': 0.4, 'probabilities': {'0': 0.5, '1': 0.4, '2': 0.1}},
        'divergence': {'type': 'score', 'score': 0.0, 'confidence': 1.0, 'probabilities': {'0': 1.0, '1': 0.0, '2': 0.0}},
        'hallucination_risk': {'type': 'score', 'score': 1.9, 'confidence': 0.9, 'probabilities': {'0': 0.0, '1': 0.1, '2': 0.9}},
        'error_cost': {'type': 'score', 'score': 1.0, 'confidence': 0.6, 'probabilities': {'0': 0.2, '1': 0.6, '2': 0.2}},
        'needs_visual': {'type': 'noul', 'noul': 0.1},
        'needs_gui': {'type': 'noul', 'noul': 0.8},
        'local_only': {'type': 'noul', 'noul': 0.7},
    },
    'usage': {'input_tokens': 900, 'output_tokens': 40},
}
JEV_PROFILE = {'work_kind': 'computer_use', 'interaction_style': 'engineering', 'reasoning_depth': 2, 'code_complexity': 0,
               'context_scale': 2, 'divergence': 1, 'hallucination_risk': 3, 'error_cost': 2,
               'needs_visual': True, 'needs_gui': True, 'local_only': True,
               'reason': 'Jev (jev-2026-10-01) classified as computer_use (D2 C0 S2 V1)'}
SECRET = 'test-key-0000000000000000'


class SelectableScorerTests(unittest.TestCase):
    """Contract tests for the user-selectable System-One scorers (config, Jev, Claude, Codex, custom API, heuristic)."""

    def setUp(self):
        self.allocator = router.load_allocator()
        self.capabilities = self.allocator.load_capabilities()
        self.temp = tempfile.TemporaryDirectory(prefix='scorer-config-test-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'scorer-config.json'
        env = patch.dict(router.os.environ, {'ROUTER_SCORER_CONFIG': str(self.path), 'ROUTER_SCORER_TYPE': '',
                                             'ROUTER_DISABLE_LIVE_SCORER': ''})
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
            profile, meta = router.score_task(self.task, self.allocator, self.capabilities, scorer_config=config)
        return profile, meta, calls

    def test_config_load_save_env_override_and_masking(self):
        self.assertEqual(router.load_scorer_config(), {'scorer_type': 'agy_flash_low', 'fallback_to_agy': True})
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
                    lambda: router.save_scorer_config('heuristic', api_key='x')):
            with self.assertRaises(ValueError):
                bad()
        with patch.dict(router.os.environ, {'ROUTER_SCORER_TYPE': 'heuristic'}):
            self.assertEqual(router.load_scorer_config()['scorer_type'], 'heuristic')
        with patch.dict(router.os.environ, {'ROUTER_SCORER_TYPE': 'nope'}):
            config = router.load_scorer_config()
            self.assertEqual((config['scorer_type'], 'nope' in config['config_error']), ('agy_flash_low', True))
        self.path.write_text('not json', encoding='utf-8')
        self.assertEqual(router.load_scorer_config()['scorer_type'], 'agy_flash_low')
        self.assertEqual((router.mask_secret(SECRET), router.mask_secret('short'), router.mask_secret(None)),
                         ('test…0000', '••••', ''))
        view = router.scorer_status({'scorer_type': 'jev', 'jev_api_key': SECRET})
        self.assertEqual((view['jev']['api_key'], view['jev']['key_source']), ('test…0000', 'file'))
        self.assertNotIn(SECRET, json.dumps(view))

    def test_jev_request_matches_typesafe_systemone_contract(self):
        request = router.build_jev_request(self.task, self.capabilities)
        self.assertEqual((request['model'], set(request['state'])), ('jev-latest', {'task', 'rubric_notes'}))
        self.assertEqual(request['state']['task'], {'objective': self.task['objective']})
        self.assertIn('read_only_review', request['state']['rubric_notes'])
        questions = request['questions']
        self.assertEqual(set(questions), {'work_kind', 'interaction_style', 'reasoning_depth', 'code_complexity', 'context_scale',
                                          'divergence', 'hallucination_risk', 'error_cost', 'needs_visual', 'needs_gui', 'local_only'})
        routes = set(self.capabilities['routes']) - {'other_or_mixed'}
        self.assertEqual((questions['work_kind']['type'], set(questions['work_kind']['criteria'])), ('choice', routes))
        self.assertTrue(all(isinstance(v, str) and v for v in questions['work_kind']['criteria'].values()))
        self.assertEqual(set(questions['interaction_style']['criteria']), set(self.allocator.INTERACTION_STYLES))
        for name, (low, high) in self.allocator.PROFILE_INTEGERS.items():
            q = questions[name]
            self.assertEqual((q['type'], len(q['criteria']), isinstance(q['criteria'], list)), ('score', high - low + 1, True), name)
            self.assertLessEqual(len(q['criteria']), 10)
        for name in ('needs_visual', 'needs_gui', 'local_only'):
            self.assertEqual(questions[name]['type'], 'noul')
            self.assertNotIn('criteria', questions[name])

    def test_jev_response_parsing_maps_every_dimension(self):
        profile = router.parse_jev_response(JEV_RESPONSE, self.allocator)
        self.assertEqual(profile, JEV_PROFILE)
        self.assertEqual(self.allocator.validate_profile(profile), profile)
        # Scores clamp into range; a bare value answer is accepted; noul below 0.5 is false and needs_gui implies visual only when true
        answers = {k: dict(v) for k, v in JEV_RESPONSE['answers'].items()}
        answers['reasoning_depth']['score'] = 9
        answers['code_complexity']['score'] = 2.6
        answers['needs_gui'] = {'type': 'noul', 'noul': 0.49}
        answers['local_only'] = 0.5
        answers['work_kind'] = 'debug_review'
        p2 = router.parse_jev_response({'answers': answers}, self.allocator)
        self.assertEqual((p2['reasoning_depth'], p2['code_complexity'], p2['needs_gui'], p2['needs_visual'], p2['local_only'], p2['work_kind']),
                         (4, 3, False, False, True, 'debug_review'))
        self.assertIn('Jev (jev-latest)', p2['reason'])
        for broken in ({'answers': {**answers, 'work_kind': {'choice': 'make_coffee'}}},
                       {'answers': {**answers, 'interaction_style': {'choice': 'vibes'}}},
                       {'answers': {k: v for k, v in answers.items() if k != 'error_cost'}},
                       {'answers': {**answers, 'divergence': {'score': float('nan')}}},
                       {'error': 'x'}):
            with self.assertRaises(ValueError):
                router.parse_jev_response(broken, self.allocator)

    def test_jev_scorer_dispatch_and_fallbacks(self):
        profile, meta, calls = self.score({'scorer_type': 'jev', 'jev_api_key': SECRET}, post=JEV_RESPONSE)
        self.assertEqual(profile, JEV_PROFILE)
        self.assertEqual((meta['source'], meta['scorer_type'], meta['provider'], meta['model']),
                         ('system_one', 'jev', 'typesafe', 'jev-2026-10-01'))
        self.assertEqual((calls['post'][0]['url'], calls['post'][0]['key'], calls['run']),
                         ('https://api.typesafe.ai/v1/systemone', SECRET, []))
        self.assertNotIn('chain', json.dumps(calls['post'][0]['body']))
        # Missing key: agy takes over by default, and the reason says why
        _, meta, calls = self.score({'scorer_type': 'jev', 'fallback_to_agy': True})
        self.assertEqual((meta['source'], meta['scorer_type'], meta['configured_scorer'], meta['fallback_reason'], calls['post']),
                         ('system_one', 'agy_flash_low', 'jev', 'ScorerError', []))
        self.assertIn('TypeSafe API key', meta['attempts'][0]['detail'])
        # fallback_to_agy off, or agy also failing: the deterministic profile
        expected = self.allocator.infer_task_profile(self.task, self.capabilities)
        profile, meta, calls = self.score({'scorer_type': 'jev', 'fallback_to_agy': False})
        self.assertEqual((profile, meta['source'], meta['configured_scorer'], calls['run']), (expected, 'heuristic_fallback', 'jev', []))
        self.assertIn('TypeSafe API key', meta['fallback_detail'])
        profile, meta, _ = self.score({'scorer_type': 'jev', 'jev_api_key': SECRET},
                                      post=router.ScorerError('Scorer API answered HTTP 401.'), run=lambda c, d: (1, '', ''))
        self.assertEqual((profile, meta['source'], [a['scorer_type'] for a in meta['attempts']]),
                         (expected, 'heuristic_fallback', ['jev', 'agy_flash_low']))
        self.assertEqual(meta['fallback_detail'], 'Scorer API answered HTTP 401.')
        self.assertNotIn(SECRET, json.dumps(meta))
        with patch.dict(router.os.environ, {'ROUTER_DISABLE_LIVE_SCORER': '1'}):
            _, meta, calls = self.score({'scorer_type': 'jev', 'jev_api_key': SECRET, 'fallback_to_agy': False}, post=JEV_RESPONSE)
        self.assertEqual((meta['source'], calls['post']), ('heuristic_fallback', []))

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
        # Claude answering with an error falls back to agy
        _, meta, _ = self.score({'scorer_type': 'claude_haiku_low'},
                                run=lambda c, d: (0, json.dumps({'is_error': True}), '') if c[0] == 'claude.exe'
                                else (0, json.dumps({'status': 'SUCCESS', 'structured_output': GUI_PROFILE}), ''), cli=('claude.exe',))
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
        _, meta, calls = self.score({**custom, 'api_key': '', 'fallback_to_agy': False}, post=reply)
        self.assertEqual((meta['source'], calls['post']), ('heuristic_fallback', []))
        _, meta, _ = self.score({**custom, 'fallback_to_agy': False}, post={'choices': []})
        self.assertEqual((meta['source'], meta['fallback_reason']), ('heuristic_fallback', 'ValueError'))

        profile, meta, calls = self.score({'scorer_type': 'heuristic'}, post=AssertionError('no call'))
        self.assertEqual((profile, meta, calls), (self.allocator.infer_task_profile(self.task, self.capabilities),
                                                  {'source': 'heuristic_configured', 'scorer_type': 'heuristic',
                                                   'provider': 'heuristic', 'model': 'heuristic'}, {'post': [], 'run': []}))

    def test_score_task_reads_the_configured_scorer_and_scorer_cli(self):
        router.save_scorer_config('heuristic')
        profile, meta = router.score_task(self.task, self.allocator, self.capabilities)
        self.assertEqual(meta['source'], 'heuristic_configured')

        def cli(*argv, stdin=None):
            out = io.StringIO()
            with patch.object(sys, 'argv', ['route_judge.py', 'scorer', *argv]), contextlib.redirect_stdout(out), \
                    patch.object(sys, 'stdin', io.StringIO(stdin or '')):
                code = router.main()
            return code, json.loads(out.getvalue()), out.getvalue()
        code, shown, _ = cli()
        self.assertEqual((code, shown['action'], shown['scorer_type'], shown['config_path']), (0, 'show', 'heuristic', str(self.path)))
        code, saved, text = cli('--action', 'set', '--type', 'jev', '--api-key', '-', stdin=SECRET + '\n')
        self.assertEqual((code, saved['scorer_type'], saved['jev']['api_key'], saved['jev']['key_source']), (0, 'jev', 'test…0000', 'file'))
        self.assertNotIn(SECRET, text)
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['jev_api_key'], SECRET)
        code, failed, _ = cli('--action', 'set')
        self.assertEqual((code, failed['detail']), (1, 'scorer set requires --type.'))
        code, tested, _ = cli('--action', 'test', '--type', 'heuristic', '--objective', 'Fix a parser bug')
        self.assertEqual((code, tested['ok'], tested['scorer']['source']), (0, True, 'heuristic_configured'))
        self.assertIn('latency_ms', tested)
        with patch.object(router, 'post_json', side_effect=router.ScorerError('Scorer API answered HTTP 401.')), \
                patch.object(router, 'scorer_prefix', side_effect=RuntimeError('agy missing')):
            code, tested, _ = cli('--action', 'test')
        self.assertEqual((code, tested['ok'], tested['scorer']['source'], tested['scorer']['fallback_detail']),
                         (1, False, 'heuristic_fallback', 'Scorer API answered HTTP 401.'))
        with patch.object(router, 'post_json', return_value=JEV_RESPONSE):
            code, tested, _ = cli('--action', 'test')
        self.assertEqual((code, tested['ok'], tested['task_profile']), (0, True, JEV_PROFILE))


@unittest.skipUnless(router.os.environ.get('ROUTER_LIVE_AGY') == '1', 'set ROUTER_LIVE_AGY=1 to call the real agy CLI')
class LiveSystemOneSmokeTests(unittest.TestCase):
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
        allocator = router.load_allocator()
        capabilities = allocator.load_capabilities()
        for name, (task, plausible) in self.TASKS.items():
            profile, meta = router.score_task(task, allocator, capabilities, timeout=self.TIMEOUT)
            self.assertEqual(meta['source'], 'system_one', (name, meta))
            self.assertTrue(plausible(profile), (name, profile))


if __name__ == '__main__':
    unittest.main()
