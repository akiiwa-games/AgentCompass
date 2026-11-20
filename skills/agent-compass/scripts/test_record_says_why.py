"""What a caller of `record`, and of a roll that names its route, reads when it does not get what it asked for: the fact
that decided it and a way out that works for that fact (refusal audit part 2, findings 8, 9, 14 and 15). Rules, never
outcomes: each text is taken from the owner of its fact, and only what is said changes."""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest.mock import patch

import account_fleet
import auto_bundles
import decision_ledger
import host_policy
import roll
import route_judge
import stage_help
import usage_reading
from router_test_support import isolated_router_state, profile, temporary_root

LS = {'allowed_accounts': ['codex', 'antigravity', 'dot', 'chatgpt-web'], 'judges': ['codex'],
      'usage_providers': ['codex', 'antigravity']}
NAMED = ['record', '--constraint', 'user_override', '--provider', 'codex', '--account', 'codex', '--model', 'gpt-6.1-sol',
         '--effort', 'high', '--basis', 'User: use codex']


def fresh(now, *accounts):
    """A usage snapshot read now, every account in `accounts` observed with an untouched window."""
    return {'status': 'fresh', 'read_ok': True, 'accounts': {
        a: {'status': 'fresh', 'fetched_at': now.isoformat(), 'available': True,
            'windows': [{'id': 'weekly', 'used_pct': 1, 'resets_at': '2099-01-01T00:00:00+00:00'}]} for a in accounts}}


class RecordCli(unittest.TestCase):
    def setUp(self):
        isolated_router_state(self.addCleanup, policy=False)
        self.directory = temporary_root(self.addCleanup)
        self.policy = self.directory / 'host-policy.json'
        self.policy.write_text(json.dumps(LS))
        p = patch.object(host_policy, 'policy_path', return_value=(self.policy, False))
        p.start()
        self.addCleanup(p.stop)
        ledger = patch.object(decision_ledger, 'ledger_path', return_value=self.directory / 'ledger.jsonl')
        ledger.start()
        self.addCleanup(ledger.stop)
        self.task = self.directory / 'task.json'
        self.task.write_text(json.dumps({'task': {'work_kind': 'code_implementation', 'difficulty': 'hard',
                                                  'profile': profile()}}))
        self.now = datetime.now(timezone.utc)

    def run_main(self, argv, usage=None):
        out = io.StringIO()
        usage = fresh(self.now, 'codex', 'claude') if usage is None else usage
        with patch.object(sys, 'argv', ['agentcompass', *argv]), \
                patch.object(decision_ledger, 'ledger_path', return_value=self.directory / 'ledger.jsonl'), \
                patch.dict(os.environ, {'PASEO_AGENT_ID': 'parent-a'}), redirect_stdout(out), \
                patch.object(usage_reading, 'usage_snapshot', return_value=usage):
            code = route_judge.main()
        return code, json.loads(out.getvalue())

    def flag_spelling(self, dest):
        return next(a.option_strings[0] for a in route_judge.build_parser()._actions if a.dest == dest)


class UnreadFlagTests(RecordCli):
    """Finding 8: one flat parser serves every stage, so a stage says which of the given flags it did not read
    (`stage_help.STAGES` lists what each stage reads); a note, not a refusal."""

    def test_a_flag_the_stage_does_not_list_is_named_in_a_note_and_the_record_still_goes(self):
        code, result = self.run_main(NAMED + ['--input', str(self.task), '--workspace', 'D:/work/x', '--title', 'T',
                                              '--new-worker'])
        self.assertEqual(code, 0, result)
        self.assertIn('decision_id', result)
        [note] = [w for w in result.get('warnings') or () if 'not read by record' in w]
        for flag in ('--workspace', '--title', '--new-worker'):
            self.assertIn(flag, note)
        for flag in ('--basis', '--model', '--input', '--constraint'):
            self.assertNotIn(flag, note)

    def test_the_note_follows_the_stage_table_for_every_stage(self):
        """Rule: a given flag is noted exactly when the stage's row of `stage_help.STAGES` does not list it."""
        parser = route_judge.build_parser()
        actions = {a.dest: a for a in parser._actions if a.option_strings and a.dest not in route_judge.ALWAYS_READ}
        for stage, (_, _, basic, extra) in stage_help.STAGES.items():
            listed = set(basic) | set(extra)
            for dest in ('hours', 'workspace', 'trigger', 'dispatch_id', 'boost'):
                with self.subTest(stage=stage, flag=dest):
                    action = actions[dest]
                    value = action.choices[0] if action.choices else 1.0 if action.type is float else 'x'
                    args = parser.parse_args([stage, action.option_strings[0], str(value)])
                    self.assertEqual(route_judge.unread_flags(args), [] if dest in listed else [action.option_strings[0]])

    def test_record_no_longer_lists_hours_which_it_never_read(self):
        self.assertNotIn('hours', stage_help.STAGES['record'][2] + stage_help.STAGES['record'][3])
        code, result = self.run_main(NAMED + ['--input', str(self.task), '--hours', '3'])
        self.assertEqual(code, 0, result)
        self.assertTrue(any('--hours' in w and 'not read by record' in w for w in result.get('warnings') or ()), result)

    def test_record_with_task_text_and_no_input_says_no_comparison_was_made(self):
        code, result = self.run_main(NAMED + ['--task', 'do something else entirely'])
        self.assertEqual(code, 0, result)
        warnings = result.get('warnings') or ()
        self.assertTrue(any('--task' in w and 'not read by record' in w for w in warnings), warnings)
        self.assertTrue(any(w.startswith(route_judge.NO_TASK_COMPARED) for w in warnings), warnings)
        code, with_task = self.run_main(NAMED + ['--input', str(self.task)])
        self.assertFalse(any(w.startswith(route_judge.NO_TASK_COMPARED) for w in with_task.get('warnings') or ()))


class QuotaNotObservedTests(RecordCli):
    """Finding 9: "no window is exhausted" and "no window was read" do not read the same. The launch goes ahead."""

    def test_no_usage_snapshot_is_said_in_the_plans_sentence(self):
        unread = {'status': 'unknown', 'read_ok': False, 'accounts': {}, 'reason': 'No readable provider usage cache.'}
        code, result = self.run_main(NAMED + ['--input', str(self.task)], usage=unread)
        self.assertEqual(code, 0, result)
        gap = route_judge.readiness_gap({}, unread)
        self.assertEqual(gap['code'], 'no_usage_data')
        self.assertTrue(any(w.endswith(gap['message']) and 'codex' in w for w in result.get('warnings') or ()), result)
        self.assertIn('no_usage_data', [a['code'] for a in result['alerts']])

    def test_an_account_without_a_fresh_reading_is_named(self):
        stale = fresh(self.now, 'claude')
        stale['accounts']['codex'] = {'status': 'stale', 'fetched_at': '2026-01-01T00:00:00+00:00', 'windows': []}
        code, result = self.run_main(NAMED + ['--input', str(self.task)], usage=stale)
        self.assertEqual(code, 0, result)
        self.assertTrue(any(w.startswith('quota not observed: codex') and 'stale' in w
                            for w in result.get('warnings') or ()), result)

    def test_a_fresh_reading_says_nothing_of_the_kind(self):
        code, result = self.run_main(NAMED + ['--input', str(self.task)])
        self.assertEqual(code, 0, result)
        self.assertFalse(any(w.startswith('quota not observed') for w in result.get('warnings') or ()), result)

    def test_a_named_roll_carries_the_records_warnings_into_its_short_answer(self):
        decision = {'selected_bundle': {'provider': 'codex'}, 'warnings': ['quota not observed: codex x']}
        self.assertEqual(roll.summary(decision)['warnings'], ['quota not observed: codex x'])


class UnknownAccountTests(RecordCli):
    """Finding 14: an account, or an account-provider pair, the fleet does not have is known before any model list."""

    def test_an_account_that_does_not_exist_is_named_as_such(self):
        code, result = self.run_main(['record', '--constraint', 'user_override', '--provider', 'codex', '--account', 'nope',
                                      '--model', 'gpt-6.1-sol', '--basis', 'q'])
        self.assertEqual((code, result['status']), (1, 'error'), result)
        self.assertTrue(result['cause'].startswith('account nope is not configured'), result['cause'])
        self.assertNotIn('named_model_unavailable', result['cause'])

    def test_a_provider_the_account_does_not_launch_through_is_named_as_such(self):
        code, result = self.run_main(['record', '--constraint', 'user_override', '--provider', 'claude', '--account', 'codex',
                                      '--model', 'gpt-6.1-sol', '--basis', 'q'])
        self.assertEqual(code, 1, result)
        self.assertTrue(result['cause'].startswith('account codex does not launch through provider claude'), result['cause'])
        served = auto_bundles.launch_providers('codex', account_fleet.load_accounts()['accounts']['codex'])
        self.assertTrue(served and 'claude' not in served and all(p in result['cause'] for p in served),
                        (served, result['cause']))


class SmallerSilencesTests(RecordCli):
    """Finding 15 (all but the cwd_regex refusal)."""

    def test_effort_beside_a_disagreeing_suffix_says_which_was_used(self):
        argv = ['record', '--constraint', 'user_override', '--provider', 'codex', '--account', 'codex',
                '--model', 'gpt-6.1-sol:low', '--effort', 'high', '--basis', 'q']
        code, result = self.run_main(argv)
        self.assertEqual(code, 0, result)
        self.assertEqual(result['selected_bundle']['effort'], 'high')
        self.assertTrue(any('--effort high' in w and ':low' in w and 'high was used' in w
                            for w in result.get('warnings') or ()), result)
        code, agreeing = self.run_main(argv[:-4] + ['--effort', 'low', '--basis', 'q'])
        self.assertFalse(any('was used' in w for w in agreeing.get('warnings') or ()), agreeing)

    def test_a_basis_over_the_limit_is_refused_with_the_limit(self):
        base = ['record', '--constraint', 'user_override', '--provider', 'codex', '--account', 'codex', '--model', 'gpt-6.1-sol']
        code, result = self.run_main(base + ['--basis', 'x' * (decision_ledger.BASIS_LIMIT + 1)])
        self.assertEqual(code, 1, result)
        self.assertIn(str(decision_ledger.BASIS_LIMIT), result['cause'])
        self.assertIn('--basis', result['cause'])
        code, result = self.run_main(base + ['--basis', 'x' * decision_ledger.BASIS_LIMIT])
        self.assertEqual((code, result['basis']), (0, 'x' * decision_ledger.BASIS_LIMIT), result)

    def test_a_workflow_trigger_on_a_constraint_the_gate_refuses_is_refused_at_record(self):
        rule = decision_ledger.WORKFLOW_RECORD
        code, result = self.run_main(['record', '--constraint', 'hard_constraint', '--provider', 'codex', '--account', 'codex',
                                      '--model', 'gpt-6.1-sol', '--basis', 'q', '--trigger', rule['trigger']])
        self.assertEqual(code, 1, result)
        self.assertIn(f"--constraint {rule['constraint']}", result['cause'])
        self.assertIn('hard_constraint', result['cause'])
        code, result = self.run_main(NAMED + ['--trigger', rule['trigger']])
        self.assertEqual((code, result.get('trigger')), (0, rule['trigger']), result)

    def test_a_name_read_as_another_row_says_both(self):
        code, result = self.run_main(['record', '--constraint', 'user_override', '--provider', 'claude', '--account', 'claude',
                                      '--model', 'claude-opus-5', '--basis', 'q'])
        self.assertEqual(code, 0, result)
        read_as = result['selected_bundle']['model']
        self.assertNotEqual(read_as, 'claude-opus-5')
        self.assertTrue(any("--model claude-opus-5 was read as" in w and read_as in w
                            for w in result.get('warnings') or ()), result)
        code, exact = self.run_main(['record', '--constraint', 'user_override', '--provider', 'claude', '--account', 'claude',
                                     '--model', read_as, '--basis', 'q'])
        self.assertFalse(any('was read as' in w for w in exact.get('warnings') or ()), exact)

    def test_a_project_rule_outside_automatic_routing_is_blocked_not_exit_zero(self):
        code, result = self.run_main(['record', '--constraint', 'hard_constraint', '--provider', 'claude', '--account', 'claude',
                                      '--model', 'claude-opus-5-5', '--basis', 'project rule'])
        violation = result['host_policy_violation']
        self.assertEqual((code, result['status']), (2, 'blocked'), result)
        self.assertIn('decision_id', result)                      # the record is written as before; the gate reads it
        self.assertIn(violation['reason'], result['next_action'])
        self.assertIn('not allowed', result['next_action'])
        self.assertEqual(decision_ledger.read_ledger()[-1]['host_policy_violation'], violation)
        code, named = self.run_main(['record', '--constraint', 'user_override', '--provider', 'claude', '--account', 'claude',
                                     '--model', 'claude-opus-5-5', '--basis', 'User: use opus'])
        self.assertEqual(code, 0, named)
        self.assertNotIn('status', named)


if __name__ == '__main__':
    unittest.main()
