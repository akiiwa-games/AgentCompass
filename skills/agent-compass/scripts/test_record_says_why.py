"""What a caller of `record`, and of a roll that names its route, reads when it does not get what it asked for: the fact
that decided it and a way out that works for that fact (refusal audit part 2, findings 8, 9, 14 and 15). Rules, never
outcomes: each text names the fact of its owner (the flag, the account, the window, the provider), and only what is
said changes."""
import argparse
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import account_fleet
import auto_bundles
import decision_ledger
import dispatch_flow
import host_policy
import quota_pools
import route_judge
import scorer_client
import stage_help
import usage_reading
from router_test_support import isolated_router_state, profile, temporary_root

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'setup'))
import router_gate_hook  # noqa: E402

LS = {'allowed_accounts': ['codex', 'antigravity', 'dot', 'chatgpt-web'], 'judges': ['codex'],
      'usage_providers': ['codex', 'antigravity']}
NAMED = ['record', '--constraint', 'user_override', '--provider', 'codex', '--account', 'codex', '--model', 'gpt-6.1-sol',
         '--effort', 'high', '--basis', 'User: use codex']
PARENT = 'parent-a'


def reading(now, windows):
    """A fresh reading of `windows`, each with most of its quota left (router_test_support.usage_windows' shape)."""
    return {'status': 'fresh', 'fetched_at': now.isoformat(), 'available': True, 'windows': [
        {'id': wid, 'remaining_pct_before_reset': 90, 'observed_remaining_pct': 90, 'seconds_until_reset': 3 * 3600,
         'resets_at': (now + timedelta(hours=3)).isoformat()} for wid in windows]}


def fresh(now):
    """A usage snapshot read now: every window the reference fleet configures for codex, claude and antigravity."""
    return {'status': 'fresh', 'read_ok': True, 'accounts': {
        'codex': reading(now, ['usage']), 'claude': reading(now, ['five_hour', 'seven_day', 'weekly_fable']),
        'antigravity': reading(now, ['gemini-5h', 'gemini-weekly', '3p-5h', '3p-weekly'])}}


class RecordCli(unittest.TestCase):
    def setUp(self):
        isolated_router_state(self.addCleanup, policy=False)
        self.directory = temporary_root(self.addCleanup)
        self.policy = self.directory / 'host-policy.json'
        self.policy.write_text(json.dumps(LS))
        for p in (patch.object(host_policy, 'policy_path', return_value=(self.policy, False)),
                  patch.object(decision_ledger, 'ledger_path', return_value=self.directory / 'ledger.jsonl')):
            p.start()
            self.addCleanup(p.stop)
        self.task = self.directory / 'task.json'
        self.task.write_text(json.dumps({'task': {'work_kind': 'code_implementation', 'difficulty': 'hard',
                                                  'profile': profile()}}))
        self.now = datetime.now(timezone.utc)

    def run_main(self, argv, usage=None, env=None):
        out = io.StringIO()
        usage = fresh(self.now) if usage is None else usage
        with patch.object(sys, 'argv', ['agentcompass', *argv]), \
                patch.dict(os.environ, {'PASEO_AGENT_ID': PARENT} if env is None else env), redirect_stdout(out), \
                patch.object(usage_reading, 'usage_snapshot', return_value=usage):
            code = route_judge.main()
        return code, json.loads(out.getvalue())

    def spelled(self, dest):
        return next(a.option_strings[0] for a in route_judge.build_parser()._actions if a.dest == dest)


# --- The stage table against the code (finding 8, review findings 3, 6 and 7) ---------------------------------------

DESTS = {a.dest: a.default for a in route_judge.build_parser()._actions if a.option_strings}
# Read before the stage by the `agentcompass` entry itself (agentcompass/cli.py): `usage` without --show collects.
ENTRY_READS = {'usage': {'show'}}


class ReadLog(argparse.Namespace):
    """A parsed namespace that records which given flags (a value other than the parser's default) the stage reads,
    while `log` is set. A flag the code itself writes (roll sets --constraint for a named route) is not the caller's."""
    log = None

    def __getattribute__(self, name):
        log = type(self).log
        value = object.__getattribute__(self, name)
        if log is not None and name in DESTS and name not in log['written'] and value != DESTS[name]:
            log['read'].add(name)
        return value

    def __setattr__(self, name, value):
        if type(self).log is not None and name in DESTS:
            type(self).log['written'].add(name)
        object.__setattr__(self, name, value)


class StageTableTests(RecordCli):
    """Every flag a stage reads is in `stage_help.reads` (its row of `STAGES` and `UNLISTED`), every flag there is read
    by the stage on some path, and every stage's answer names a given flag it does not read."""

    def setUp(self):
        super().setUp()
        d = self.directory
        self.cache = str(d / 'usage-cache.json')
        self.workspace = str(d)
        multi = profile(multi_task=True, task_count=2)
        single = profile()
        self.multi_task = d / 'multi.json'
        self.multi_task.write_text(json.dumps({'task': {'objective': 'two things', 'work_kind': 'code_implementation',
                                                        'expected_hours': 1, 'profile': multi}}))
        self.prepare = d / 'prepare.json'
        self.prepare.write_text(json.dumps({'decision_id': 'd', 'task': {'parent_agent_id': PARENT}, 'launch': {}}))
        self.later = (self.now + timedelta(hours=3)).isoformat()

        def classify(task, needs, capabilities, policy, timeout, use_model=True):     # no model call in a test
            chosen = multi if 'two things' in str(task.get('objective')) else single
            return chosen, {**task, 'profile': chosen}, needs, {'source': 'fixture'}, None, []
        for p in (patch.object(route_judge, 'classify_task', side_effect=classify),
                  patch.object(scorer_client, 'score_task', return_value=(single, {'source': 'fixture'})),
                  patch.object(scorer_client, 'save_scorer_config'),
                  patch.object(scorer_client, 'scorer_test', side_effect=lambda *a, **k: {'ok': True}),
                  patch.object(dispatch_flow, 'require_actual_parent'),
                  patch.object(dispatch_flow, 'reconcile', return_value=[]),
                  patch.object(dispatch_flow, 'launch', side_effect=lambda *a, **k: {'state': 'started'}),
                  patch.object(dispatch_flow.registry, 'prepare', side_effect=lambda *a, **k: {'request': {}}),
                  patch.object(dispatch_flow, 'public', side_effect=lambda row: dict(row)),
                  patch.object(dispatch_flow.registry, 'inspect', side_effect=lambda *a, **k: {'state': 'completed'}),
                  patch.object(dispatch_flow.registry, 'release', side_effect=lambda *a, **k: {'state': 'completed'})):
            p.start()
            self.addCleanup(p.stop)

    def scenarios(self):
        """stage -> (argument vector, environment) runs that together take each path on which the stage reads a flag."""
        c, w, T, M = self.cache, self.workspace, str(self.task), str(self.multi_task)
        named = ['--provider', 'codex', '--account', 'codex', '--model', 'gpt-6.1-sol', '--effort', 'high', '--basis', 'q']
        return {
            'record': [(NAMED + ['--input', T, '--trigger', 'workflow', '--reason-category', 'other', '--specialty', 's',
                                 '--dry-run', '--parent-id', PARENT, '--usage-cache', c, '--accounts', 'codex'], None),
                       (NAMED + ['--fast'], None)],
            'roll': [(['roll', '--task', 'two things', '--dry-run', '--score', '--keep-whole', 'k', '--hours', '1',
                       '--can-wait-hours', '1', '--fast', '--task-id', 'id-1', '--score-timeout', '5', '--usage-cache', c,
                       '--file', T, '--apply-to', w, '--local-only', 'secrets', '--verbose', '--parent-id', PARENT,
                       '--parent-host', 'paseo'], None),
                     (['roll', '--task', 'x', '--dry-run', '--no-score', '--workspace', w, '--apply-to', w], None),
                     (['roll', '--task', 'x', '--resume', 'agent-1', '--new-worker'], None),
                     (['roll', '--brief', T, '--dry-run'] + named + ['--trigger', 'workflow', '--specialty', 's',
                                                                       '--reason-category', 'other', '--accounts', 'codex'],
                      None),
                     (['roll', '--task', 'one thing', '--score', '--local-only', 'secrets', '--access', 'read-only',
                       '--title', 'T', '--parent-host', 'native', '--parent-address', 'inbox-1', '--parent-id', 'p-1'],
                      {'PASEO_AGENT_ID': ''})],
            'plan': [(['plan', '--input', M, '--dry-run', '--format', 'json', '--hours', '1', '--can-wait-hours', '1',
                       '--fast', '--keep-whole', 'k', '--score', '--score-timeout', '5', '--usage-cache', c], None),
                     (['plan', '--input', T, '--dry-run', '--no-score', '--hours', '1'], None),
                     (['plan', '--demo', '--dry-run', '--format', 'json'], None)],
            'providers': [(['providers'], None)],
            'usage': [(['usage', '--show', '--accounts', 'codex', '--usage-cache', c], None)],
            'score': [(['score', '--input', T, '--dry-run', '--score-timeout', '5'], None)],
            'scorer': [(['scorer', '--action', 'set', '--type', 'openai_compatible', '--api-key', 'k', '--base-url', 'u',
                         '--model', 'm', '--fallback-to-agy', 'on'], None),
                       (['scorer', '--action', 'test', '--type', 'openai_compatible', '--objective', 'o',
                         '--score-timeout', '5'], None)],
            'override': [(['override', '--list', '--usage-cache', c], None),
                         (['override', '--clear', 'all'], None),
                         (['override', '--account', 'codex', '--model', 'gpt-6.1-sol', '--basis', 'b', '--intent', 'i',
                           '--window', 'usage', '--resets-in', '2', '--expires-in', '5', '--boost', '0.2',
                           '--until', self.later], None),
                         (['override', '--account', 'codex', '--basis', 'b', '--resets-at', self.later,
                           '--expires-at', self.later], None),
                         (['override', '--account', 'codex', '--basis', 'b', '--resets-in', '2', '--stop-on-reset'], None),
                         (['override', '--account', 'codex', '--basis', 'b', '--resets-in', '2',
                           '--reset-confirmation', 'cycle'], None),
                         (['override', '--account', 'codex', '--basis', 'b', '--resets-in', '2', '--rebaseline'], None)],
            'dispatch': [(['dispatch', '--dispatch-action', 'prepare', '--input', str(self.prepare), '--usage-cache', c],
                          None),
                         (['dispatch', '--dispatch-action', 'release', '--dispatch-id', 'x', '--parent-id', PARENT,
                           '--basis', 'b'], None)],
            'launch': [(['launch', '--dispatch-id', 'x', '--parent-id', PARENT, '--watch-seconds', '10',
                         '--poll-seconds', '2'], None)],
        }

    def reads_of(self, argv, env):
        """The given flags a run of the stage reads (route_judge._stage_result, after the checks every stage shares)."""
        real_parser, real_stage = route_judge.build_parser, route_judge._stage_result
        reads = set()

        def recording_parser():
            parser = real_parser()
            parse = parser.parse_args
            parser.parse_args = lambda args=None, namespace=None: parse(args, namespace if namespace is not None else ReadLog())
            return parser

        def recorded(args):
            ReadLog.log = {'read': reads, 'written': set()}
            try:
                return real_stage(args)
            finally:
                ReadLog.log = None
        with patch.object(route_judge, 'build_parser', side_effect=recording_parser), \
                patch.object(route_judge, '_stage_result', side_effect=recorded):
            code, answer = self.run_main(argv, env=env)
        return reads, answer

    def test_every_flag_a_stage_reads_is_listed_and_every_listed_flag_is_read(self):
        scenarios = self.scenarios()
        self.assertEqual(set(scenarios), set(stage_help.STAGES))
        for stage, runs in scenarios.items():
            with self.subTest(stage=stage):
                read, answers = set(), []
                for argv, env in runs:
                    got, answer = self.reads_of(argv, env)
                    read |= got
                    answers.append(answer)
                listed = set(stage_help.reads(stage))
                self.assertEqual(read - listed - set(route_judge.ALWAYS_READ), set(),
                                 f'{stage} reads flags its row does not list')
                self.assertEqual(listed - read - ENTRY_READS.get(stage, set()), set(),
                                 f'{stage} lists flags no run of it reads: {answers}')

    def test_every_stage_names_a_given_flag_it_does_not_read(self):
        for stage, runs in self.scenarios().items():
            argv, env = runs[0]
            dest = next(d for d in ('boost', 'hours', 'workspace') if d not in stage_help.reads(stage))
            action = next(a for a in route_judge.build_parser()._actions if a.dest == dest)
            with self.subTest(stage=stage):
                code, answer = self.run_main(argv + [action.option_strings[0], '1' if action.type is float else 'x'],
                                             env=env)
                notes = [w for w in answer.get('warnings') or () if action.option_strings[0] in w]
                self.assertTrue(notes and all(stage in w for w in notes), answer)

    def test_roll_help_hides_other_stages_options(self):
        """packaging/sandbox_check.py ("roll -h shows --task/--brief and hides other stages' options"), here."""
        shown = stage_help.stage_help(route_judge.build_parser(), 'roll')
        self.assertTrue('--task' in shown and '--brief' in shown and '--resets-in' not in shown
                        and '--constraint' not in shown, shown)


class RollPathTests(RecordCli):
    """Review finding 6: a flag only a recorded route reads, given to a roll that records no route, is said."""

    def test_a_roll_that_names_no_route_says_what_it_did_not_do(self):
        argv = ['roll', '--task', 'x', '--dry-run', '--no-score', '--trigger', 'workflow', '--specialty', 's',
                '--reason-category', 'other', '--accounts', 'codex']
        code, answer = self.run_main(argv)
        [note] = [w for w in answer.get('warnings') or () if self.spelled('trigger') in w]
        for dest in route_judge.ROUTE_RECORD_FLAGS:
            self.assertIn(self.spelled(dest), note)
        self.assertIn('Workflow', note)

    def test_a_named_roll_reads_them_and_says_nothing_of_the_kind(self):
        named = ['--provider', 'codex', '--account', 'codex', '--model', 'gpt-6.1-sol', '--basis', 'q']
        code, answer = self.run_main(['roll', '--task', 'x', '--dry-run', *named, '--specialty', 's', '--accounts', 'codex'])
        self.assertFalse(any(self.spelled('specialty') in w for w in answer.get('warnings') or ()), answer)


# --- Quota that was not looked at (finding 9, review findings 4 and 5) ----------------------------------------------

class QuotaNotObservedTests(RecordCli):
    """"No window is exhausted" and "no window was read" do not read the same. The launch goes ahead."""

    CLAUDE = ['record', '--constraint', 'user_override', '--provider', 'claude', '--account', 'claude',
              '--model', 'claude-opus-5-5', '--basis', 'User: opus']

    def binding(self, account, model):
        accounts = account_fleet.load_accounts()
        return [wid for wid, _, _ in quota_pools.applicable_windows(
            accounts['accounts'][account], model, {}, bundle={'account': account, 'model': model}, accounts=accounts)]

    def quota_notes(self, answer, account):
        return [w for w in answer.get('warnings') or () if w.startswith(f'quota not observed: {account} ')]

    def test_no_snapshot_names_every_binding_window_and_no_ranking(self):
        unread = {'status': 'unknown', 'read_ok': False, 'accounts': {}, 'reason': 'No readable provider usage cache.'}
        code, result = self.run_main(self.CLAUDE + ['--input', str(self.task)], usage=unread)
        self.assertEqual(code, 0, result)
        windows = self.binding('claude', 'claude-opus-5-5')
        [note] = self.quota_notes(result, 'claude')
        self.assertTrue(windows and all(w in note for w in windows), (windows, note))
        self.assertNotIn('weekly_fable', windows)                  # it binds fable only (its declared scope)
        self.assertNotIn('weekly_fable', note)
        self.assertNotIn(route_judge.NO_USAGE_DATA, note)           # plan's sentence: a record ranks nothing
        self.assertEqual([a['message'] for a in result['alerts'] if a['code'] == 'no_usage_data'], [note])

    def test_a_fresh_reading_without_a_binding_window_names_that_window(self):
        usage = fresh(self.now)
        usage['accounts']['claude'] = reading(self.now, ['seven_day'])
        code, result = self.run_main(self.CLAUDE, usage=usage)
        self.assertEqual(code, 0, result)
        [note] = self.quota_notes(result, 'claude')
        self.assertIn('five_hour', note)
        self.assertNotIn('seven_day', note)
        self.assertNotIn('weekly_fable', note)

    def test_an_account_without_a_fresh_reading_names_its_status(self):
        stale = fresh(self.now)
        stale['accounts']['codex'] = {'status': 'stale', 'fetched_at': '2026-01-01T00:00:00+00:00', 'windows': []}
        code, result = self.run_main(NAMED + ['--input', str(self.task)], usage=stale)
        self.assertEqual(code, 0, result)
        [note] = self.quota_notes(result, 'codex')
        self.assertIn('stale', note)
        self.assertTrue(all(w in note for w in self.binding('codex', 'gpt-6.1-sol')), note)

    def test_every_binding_window_read_says_nothing_of_the_kind(self):
        code, result = self.run_main(NAMED + ['--input', str(self.task)])
        self.assertEqual(code, 0, result)
        self.assertEqual(self.quota_notes(result, 'codex'), [])

    def test_a_route_with_no_quota_windows_gets_neither_the_warning_nor_the_alert(self):
        unread = {'status': 'unknown', 'read_ok': False, 'accounts': {}}
        dot = next(b for b in account_fleet.bundles(account_fleet.load_accounts()).values() if b['account'] == 'dot')
        code, result = self.run_main(['record', '--constraint', 'user_override', '--provider', dot.get('provider') or 'dot',
                                      '--account', 'dot', '--model', dot['model'], '--basis', 'q'], usage=unread)
        self.assertEqual(code, 0, result)
        self.assertEqual(self.quota_notes(result, 'dot'), [])
        self.assertNotIn('no_usage_data', [a['code'] for a in result.get('alerts') or ()])

    def test_a_real_named_roll_carries_the_warning_into_its_short_answer(self):
        unread = {'status': 'unknown', 'read_ok': False, 'accounts': {}}
        code, answer = self.run_main(['roll', '--task', 'x', '--dry-run', '--provider', 'codex', '--account', 'codex',
                                      '--model', 'gpt-6.1-sol', '--basis', 'User: use codex'], usage=unread)
        self.assertTrue(self.quota_notes(answer, 'codex'), answer)


# --- Unknown accounts and providers (finding 14) -------------------------------------------------------------------

class UnknownAccountTests(RecordCli):
    """An account, or an account-provider pair, the fleet does not have is known before any model list."""

    def test_an_account_that_does_not_exist_is_named_with_the_accounts_there_are(self):
        code, result = self.run_main(['record', '--constraint', 'user_override', '--provider', 'codex', '--account', 'nope',
                                      '--model', 'gpt-6.1-sol', '--basis', 'q'])
        self.assertEqual((code, result['status']), (1, 'error'), result)
        self.assertIn('account nope', result['cause'])
        self.assertTrue(all(a in result['cause'] for a in ('codex', 'claude')), result['cause'])
        self.assertNotIn('named_model_unavailable', result['cause'])

    def test_a_provider_the_account_does_not_launch_through_is_named_with_those_it_does(self):
        code, result = self.run_main(['record', '--constraint', 'user_override', '--provider', 'claude', '--account', 'codex',
                                      '--model', 'gpt-6.1-sol', '--basis', 'q'])
        self.assertEqual(code, 1, result)
        served = auto_bundles.launch_providers('codex', account_fleet.load_accounts()['accounts']['codex'])
        self.assertTrue(served and 'claude' not in served, served)
        self.assertTrue(result['cause'].startswith('account codex ') and 'provider claude' in result['cause'])
        self.assertTrue(all(p in result['cause'] for p in served), (served, result['cause']))
        self.assertNotIn('named_model_unavailable', result['cause'])


# --- Finding 15 (all but the cwd_regex refusal) ---------------------------------------------------------------------

class SmallerSilencesTests(RecordCli):

    def test_effort_beside_a_disagreeing_suffix_names_both_and_the_one_used(self):
        argv = ['record', '--constraint', 'user_override', '--provider', 'codex', '--account', 'codex',
                '--model', 'gpt-6.1-sol:low', '--effort', 'high', '--basis', 'q']
        code, result = self.run_main(argv)
        self.assertEqual((code, result['selected_bundle']['effort']), (0, 'high'), result)
        notes = [w for w in result.get('warnings') or () if '--effort' in w and 'gpt-6.1-sol:low' in w]
        self.assertEqual(len(notes), 1, result)
        self.assertIn('high', notes[0].split('gpt-6.1-sol:low', 1)[1])
        code, agreeing = self.run_main(argv[:-4] + ['--effort', 'low', '--basis', 'q'])
        self.assertFalse(any('--effort' in w for w in agreeing.get('warnings') or ()), agreeing)

    def test_a_basis_over_the_limit_is_refused_with_the_limit(self):
        base = ['record', '--constraint', 'user_override', '--provider', 'codex', '--account', 'codex', '--model', 'gpt-6.1-sol']
        code, result = self.run_main(base + ['--basis', 'x' * (decision_ledger.BASIS_LIMIT + 1)])
        self.assertEqual(code, 1, result)
        self.assertIn(str(decision_ledger.BASIS_LIMIT), result['cause'])
        self.assertIn('--basis', result['cause'])
        code, result = self.run_main(base + ['--basis', 'x' * decision_ledger.BASIS_LIMIT])
        self.assertEqual((code, result['basis']), (0, 'x' * decision_ledger.BASIS_LIMIT), result)

    def test_record_refuses_exactly_the_workflow_records_the_gate_would_not_accept(self):
        """Two holders of one rule: the spawn gate decides (check_workflow_record), `record` says it up front. Over
        constraint x trigger, record refuses a request exactly when the gate does not accept the row it writes (for a
        refused request, the row it would write: the accepted row of that trigger with the request's constraint)."""
        accepted_row, cases = {}, []
        for trigger in (None, route_judge.WORKFLOW_TRIGGER):
            for constraint in route_judge.CONSTRAINTS:
                argv = ['record', '--constraint', constraint, '--provider', 'codex', '--account', 'codex',
                        '--model', 'gpt-6.1-sol', '--basis', 'q'] + (['--trigger', trigger] if trigger else [])
                code, result = self.run_main(argv)
                row = decision_ledger.read_ledger()[-1] if code == 0 else None
                if row:
                    accepted_row[trigger] = row
                cases.append((trigger, constraint, code, row))
        for trigger, constraint, code, row in cases:
            with self.subTest(trigger=trigger, constraint=constraint):
                written = row or {**accepted_row[trigger], 'constraint': constraint}
                identities = {str(v) for v in written.get('session_ids') or ()}
                self.assertTrue(identities, written)
                gate_refuses = router_gate_hook.check_workflow_record([written], identities) is not None
                self.assertEqual(code == 1, bool(trigger) and gate_refuses, (code, written))

    def test_a_name_read_as_another_row_names_both(self):
        code, result = self.run_main(['record', '--constraint', 'user_override', '--provider', 'claude', '--account', 'claude',
                                      '--model', 'claude-opus-5', '--basis', 'q'])
        self.assertEqual(code, 0, result)
        read_as = result['selected_bundle']['model']
        self.assertNotEqual(read_as, 'claude-opus-5')
        self.assertTrue(any('claude-opus-5 ' in w and read_as in w for w in result.get('warnings') or ()), result)
        code, exact = self.run_main(['record', '--constraint', 'user_override', '--provider', 'claude', '--account', 'claude',
                                     '--model', read_as, '--basis', 'q'])
        self.assertFalse(any(f'--model {read_as}' in w for w in exact.get('warnings') or ()), exact)

    def test_a_project_rule_outside_automatic_routing_is_blocked_not_exit_zero(self):
        code, result = self.run_main(['record', '--constraint', 'hard_constraint', '--provider', 'claude', '--account', 'claude',
                                      '--model', 'claude-opus-5-5', '--basis', 'project rule'])
        violation = result['host_policy_violation']
        self.assertEqual((code, result['status']), (2, 'blocked'), result)
        self.assertIn('decision_id', result)                      # the record is written as before; the gate reads it
        self.assertIn(violation['reason'], result['next_action'])
        self.assertEqual(decision_ledger.read_ledger()[-1]['host_policy_violation'], violation)
        code, named = self.run_main(['record', '--constraint', 'user_override', '--provider', 'claude', '--account', 'claude',
                                     '--model', 'claude-opus-5-5', '--basis', 'User: use opus'])
        self.assertEqual(code, 0, named)
        self.assertNotIn('status', named)

    def test_a_record_with_no_task_says_its_route_was_not_compared(self):
        code, result = self.run_main(NAMED + ['--task', 'do something else entirely'])
        self.assertEqual(code, 0, result)
        warnings = result.get('warnings') or ()
        self.assertIn(route_judge.NO_TASK_COMPARED, warnings)
        self.assertTrue(any(self.spelled('task') in w and 'record' in w for w in warnings), warnings)
        code, with_task = self.run_main(NAMED + ['--input', str(self.task)])
        self.assertNotIn(route_judge.NO_TASK_COMPARED, with_task.get('warnings') or ())


if __name__ == '__main__':
    unittest.main()
