"""What a caller of `agentcompass roll` reads when it does not get what it asked for: each answer names the fact that decided it
and a way out that works for that fact (`roll-reference.md`, "Every answer names the fact").

The tests assert rules: the text carries the facts the owning function computed (a window, the hours, a code, the caller's own
path), and offers no way out that cannot work for them. They never assert which route wins.
"""
import argparse
import contextlib
import io
import json
import os
from datetime import timedelta
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

import dispatch_flow as flow
import dispatch_registry as registry
import route_judge
import test_dispatch as td
from router_test_support import isolated_router_state


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup, policy=False)


def with_profile(task):
    task['profile'] = td.profile_for(task)
    return task


def full(res):
    return json.loads(Path(res['details']).read_text(encoding='utf-8')) if res.get('details') else res


class Plain(unittest.TestCase):
    """A roll with a fake judge (the real allocator and the real roll), a fake Paseo host, and isolated state."""
    setUp = td.DispatchTests.setUp
    issue = td.DispatchTests.issue
    prepare = td.DispatchTests.prepare
    snapshot = td.DispatchTests.snapshot
    _setup_paseo_env = td.ContinuationRollTests._setup_paseo_env

    def args(self, brief, **over):
        base = dict(task=None, file=[], apply_to=None, local_only=None, keep_whole=None, hours=0.5, title=None, task_id=None,
                    wait=0, parent_id=None, parent_host=None, parent_address=None, provider=None, account=None, model=None,
                    effort=None, basis=None, usage_cache=None, dry_run=False, verbose=False, fast=False, resume=None,
                    new_worker=False, workspace=None, can_wait_hours=None, access=None)
        base.update(over)
        return argparse.Namespace(brief=brief, **base)

    def brief_file(self, name='b.md', text='# Task\n'):
        path = self.root / name
        path.write_text(text, encoding='utf-8')
        return path

    def run_roll(self, brief=None, host=None, judge=None, env=None, **over):
        import roll
        original = roll.build_task
        _, standard_host, standard_judge, _ = self._setup_paseo_env()
        standard_judge.record_constraint = staticmethod(route_judge.record_constraint)
        self.enterContext(td.fake_owners(self))
        with patch.dict(os.environ, {'PASEO_AGENT_ID': 'parent-a', **(env or {})}), \
                patch.object(roll, 'WORKER_PROMPTS_DIR', self.root / 'wp'), patch.object(roll, 'RESULTS_DIR', self.root / 'rr'), \
                patch.object(flow, 'host_call', host or standard_host), \
                patch.object(roll, 'build_task', lambda *a, **k: with_profile(original(*a, **k))):
            return roll.run(self.args(brief or self.brief_file(), **over), judge or standard_judge)

    def set_remaining(self, five, seven, five_hours=3.0, seven_hours=100.0, account='claude-lab'):
        for window in self.usage['accounts'][account]['windows']:
            remaining, hours = (five, five_hours) if window['id'] == 'five_hour' else (seven, seven_hours)
            window.update(used_pct=100 - remaining, remaining_pct_before_reset=remaining, seconds_until_reset=hours * 3600,
                          resets_at=(self.now + timedelta(hours=hours)).isoformat())

    def make_worker(self, name, doc=None, finish=True, final_status='idle'):
        agents = self.root / '.paseo' / 'agents' / 'workspace'
        agents.mkdir(parents=True, exist_ok=True)
        row = self.prepare(self.issue())
        registry.claim(row['request'], now=self.now)
        registry.observe(row['dispatch_id'], self.snapshot(row, id=name), now=self.now)
        if finish:
            registry.observe(row['dispatch_id'], self.snapshot(row, 'idle', attentionReason='finished', id=name), now=self.now)
        document = {'id': name, 'status': final_status, 'labels': {'router.parent_id': 'parent-a'}}
        document.update(doc or {})
        (agents / f'{name}.json').write_text(json.dumps(document), encoding='utf-8')
        return row


class HandBuilt(unittest.TestCase):
    """A roll whose plan is written out by the test: what roll says about the facts a plan carries."""
    setUp, snapshot = td.DispatchTests.setUp, td.DispatchTests.snapshot
    roll = td.RollTests.roll
    brief = td.RollTests.brief


class BlockedAnswers(Plain):
    """Finding 1: `none`, `ask_user` and `defer` say the fact that decided them, first, and a way out that works for it."""

    def test_none_held_by_quota_names_the_candidate_the_window_and_the_hours_before_the_cut(self):
        # Rule (allocator.plan, quota_pools.quota_wait): a candidate excluded only by its own quota windows returns when the
        # last of them resets; the answer names that candidate, window and wait, and says wait.
        for label, five, seven, window, hours in (('five-hour-empty', 1, 100, 'five_hour', 3.0), ('both-empty', 1, 1, 'seven_day', 100.0)):
            with self.subTest(label):
                self.set_remaining(five, seven)
                res, code = self.run_roll(task_id='t-' + label)
                self.assertEqual((code, res['status']), (2, 'blocked'))
                self.assertIn(window, res['reason'][:160])
                self.assertIn(f'{hours:.1f}h', res['reason'][:160])
                for text in (res['next_action'],):
                    self.assertIn(window, text)
                    self.assertIn(f'{hours:.1f}h', text)
                    self.assertIn('claude-lab', text)
                    self.assertIn('Wait', text)
                self.setUp()



class BlockedHandBuilt(HandBuilt):
    """Finding 1, from plans the test writes out: what roll says about the facts a plan carries."""

    def test_none_without_a_quota_hold_points_at_the_reason(self):
        decision = dict(stage='plan', choice='none', selected_bundle={}, decision={'action': 'none', 'reason': 'No eligible bundle.'})
        (res, code), _ = HandBuilt.roll(self, decision, task='x')
        self.assertEqual(code, 2)
        self.assertIn('route.reason', res['next_action'])

    def test_ask_user_for_a_blocked_chain_owner_names_what_blocks_it_and_for_how_long(self):
        plan = {'action': 'resume', 'needs_user_decision': True, 'resume_blocked_by': ['five_hour has 1% left'], 'wait_hours': 4.2,
                'reason': 'The chain owner x cannot continue now (five_hour has 1% left); it can continue in 4.2h. Waiting keeps one owner.'}
        decision = dict(stage='plan', choice='lab_sonnet55_high', selected_bundle=self.bundle, decision=plan)
        (res, code), _ = HandBuilt.roll(self, decision, task='x')
        self.assertEqual((code, res['status']), (2, 'blocked'))
        for fact in ('five_hour has 1% left', '4.2h'):
            self.assertIn(fact, res['next_action'])
        self.assertIn('--new-worker', res['next_action'])       # the way to switch: the chain is not carried

    def test_ask_user_for_a_wait_longer_than_allowed_names_the_wait_and_the_flag(self):
        plan = {'action': 'start', 'needs_user_decision': True, 'wait_hours': 7.5,
                'reason': 'Nothing eligible can start within 2h; the best, x, can start in 7.5h (claude-lab\'s five_hour window resets then).'}
        decision = dict(stage='plan', choice='lab_sonnet55_high', selected_bundle=self.bundle, decision=plan)
        (res, _), _ = HandBuilt.roll(self, decision, task='x')
        self.assertIn('five_hour', res['next_action'])
        self.assertIn('--can-wait-hours 7.5', res['next_action'])

    def test_defer_names_the_window_that_sets_the_wait_and_the_ways_out(self):
        plan = {'action': 'defer', 'start_after_hours': 2.5, 'wait_for': {'bundle': 'lab_sonnet55_high', 'account': 'claude-lab',
                                                                           'window': 'five_hour', 'hours': 2.5}}
        decision = dict(stage='plan', choice='lab_sonnet55_high', selected_bundle=self.bundle, decision=plan)
        (res, code), _ = HandBuilt.roll(self, decision, task='x')
        self.assertEqual(code, 2)
        for fact in ("claude-lab's five_hour window", '2.5h', 'without --can-wait-hours', '--basis', 'yourself'):
            self.assertIn(fact, res['next_action'])


class FlagsAreNeverDropped(Plain):
    """Finding 3."""

    def test_account_names_a_route_and_needs_the_basis(self):
        with self.assertRaisesRegex(ValueError, '--account.*--basis'):
            self.run_roll(account='codex')
        with self.assertRaisesRegex(ValueError, '--provider'):       # the named route needs its provider and model too
            self.run_roll(account='codex', basis='the user said so', task_id='t-account')

    def test_basis_alone_is_said_to_name_nothing(self):
        res, _ = self.run_roll(basis='because', task_id='t-basis')
        self.assertTrue(any('--basis' in w and 'ignored' in w for w in res.get('warnings', [])), res)

    def test_fast_that_was_not_applied_says_so_and_why(self):
        res, _ = self.run_roll(fast=True, task_id='t-fast')
        bundle = full(res)['route']['bundle']
        if not bundle.get('fast'):
            self.assertTrue(any(w.startswith('--fast was not applied') for w in res.get('warnings', [])), res)
        else:
            self.assertFalse(any('--fast was not applied' in w for w in res.get('warnings', [])), res)

    def test_files_given_to_a_route_that_is_not_dot_are_said_not_to_be_sent(self):
        (self.root / 'x.txt').write_text('x', encoding='utf-8')
        res, _ = self.run_roll(file=[self.root / 'x.txt'], task_id='t-file')
        self.assertEqual(full(res)['route']['bundle']['provider'], 'claude-lab')
        self.assertTrue(any('files were not sent' in w and 'claude-lab' in w for w in res.get('warnings', [])), res)


class PaseoReplacedByCli(Plain):
    """Finding 4."""

    def test_the_condition_that_ended_paseo_is_named(self):
        import roll
        popen = MagicMock(return_value=MagicMock(pid=4242))
        with patch.object(roll.subprocess, 'Popen', popen), patch.object(flow, 'paseo_reachable', return_value=False):
            res, _ = self.run_roll(env={'PASEO_AGENT_ID': ''}, parent_id='parent-a', task_id='t-cli')
        self.assertEqual(res['status'], 'launched')
        self.assertTrue(any('Paseo was not used' in w and 'not reachable' in w for w in res.get('warnings', [])), res)

    def test_paseo_choice_names_the_first_condition_that_says_no(self):
        import roll
        args = argparse.Namespace(parent_id=None)
        with patch.dict(os.environ, {'PASEO_AGENT_ID': ''}):
            self.assertEqual(roll.paseo_choice('dot', 'cli', args), (False, None))      # not a launcher question
            self.assertIn('herdr', roll.paseo_choice('claude-lab', 'herdr', args)[1])
            with patch.object(roll, 'can_launch_via_paseo', return_value=False):
                self.assertIn("'claude-lab'", roll.paseo_choice('claude-lab', 'cli', args)[1])
            with patch.object(roll, 'can_launch_via_paseo', return_value=True):
                self.assertIn('no Paseo parent', roll.paseo_choice('claude-lab', 'cli', args)[1])
                with patch.object(flow, 'paseo_reachable', return_value=False):
                    self.assertIn('not reachable', roll.paseo_choice('claude-lab', 'cli', argparse.Namespace(parent_id='p'))[1])
                with patch.object(flow, 'paseo_reachable', return_value=True):
                    self.assertEqual(roll.paseo_choice('claude-lab', 'cli', argparse.Namespace(parent_id='p')), (True, None))


class LaunchRefusedBeforeAnyWorker(Plain):
    """Finding 2: no worker exists, so nothing is collected; each failure says what failed and what to do."""
    CODES = ('sdk_setup_failed', 'host_connection_failed', 'host_request_failed', 'bridge_unavailable',
             'create_send_window_elapsed', 'parent_agent_not_found')
    NAMES = {'sdk_setup_failed': 'SDK', 'host_connection_failed': 'daemon could not be reached',
             'host_request_failed': 'answered the request with an error', 'bridge_unavailable': 'Node.js',
             'create_send_window_elapsed': 'send window', 'parent_agent_not_found': 'parent agent'}

    def host_answering(self, response):
        def host(request):
            if request['operation'] == 'workspace_agents':
                return {'ok': True, 'placeable': True, 'agents': []}
            if request['operation'] in ('create', 'resume'):
                return dict(response)
            return {'ok': False, 'error': 'agent_not_observed'}
        return host

    def test_each_code_says_what_failed_and_never_sends_the_caller_to_collect(self):
        for fallback in ('on', 'off'):
            for code in self.CODES:
                with self.subTest(code=code, fallback=fallback):
                    res, _ = self.run_roll(host=self.host_answering({'ok': False, 'sent': False, 'error': code}),
                                           env={'AGENTCOMPASS_ROLL_FALLBACK': fallback}, task_id=f't-{code}-{fallback}')
                    self.assertEqual((res['status'], res['cause']), ('error', code))
                    self.assertNotIn('continuation_argv', res['next_action'])
                    self.assertNotIn('collect the result', res['next_action'])
                    self.assertIn('roll', res['next_action'].lower())
                    self.assertIn(self.NAMES[code], res['next_action'])       # what failed, in this code's own words
                    self.setUp()

    def test_setup_hint_and_condition_of_the_bridge_reach_the_caller(self):
        hint = 'Paseo bridge dependencies are missing. Run: agentcompass setup, or set ROUTER_PASEO_SDK_ROOT.'
        res, _ = self.run_roll(host=self.host_answering({'ok': False, 'sent': False, 'error': 'sdk_setup_failed', 'setupHint': hint}),
                               task_id='t-hint')
        self.assertIn(hint, res['next_action'])
        res, _ = self.run_roll(host=self.host_answering({'ok': False, 'sent': False, 'error': 'host_connection_failed',
                                                         'condition': 'connection_refused'}), task_id='t-cond')
        self.assertIn('connection_refused', res['next_action'])
        res, _ = self.run_roll(host=self.host_answering({'ok': False, 'sent': False, 'error': 'host_connection_failed',
                                                         'condition': 'Free text, with a secret=abc'}), task_id='t-cond2')
        self.assertNotIn('secret=abc', json.dumps(res))


class PendingNamesWhatIsKnown(Plain):
    """Finding 8: `pending` stays the status while a create may exist; the text and the cause follow the error."""

    def host_answering(self, response):
        return LaunchRefusedBeforeAnyWorker.host_answering(self, response)

    def test_unconfirmed_creates_name_the_error_and_do_not_say_still_creating(self):
        for code in ('host_timeout', 'invalid_host_response', 'create_outcome_uncertain_reconcile_by_label'):
            with self.subTest(code):
                res, code_ = self.run_roll(host=self.host_answering({'ok': False, 'sent': True, 'error': code}), task_id='t-' + code)
                self.assertEqual((res['status'], res['cause'], code_), ('pending', code, 0))
                self.assertNotIn('still creating', res['next_action'])
                self.assertIn('Do not roll again', res['next_action'])
                self.assertIn('next_argv', res['next_action'])
                self.assertIn('next_argv', res)
                self.setUp()

    def test_a_create_the_host_is_carrying_out_is_still_creating(self):
        res, _ = self.run_roll(host=self.host_answering({'ok': True, 'sent': True}), task_id='t-creating')
        self.assertEqual(res['status'], 'pending')
        self.assertIn('still creating', res['next_action'])
        self.assertNotIn('cause', res)


class FallbackKeepsClass(Plain):
    """Finding 12."""

    def test_a_request_scoped_refusal_keeps_its_class_with_the_fallback_on_and_off(self):
        host = LaunchRefusedBeforeAnyWorker.host_answering(self, {'ok': False, 'sent': False, 'error': 'host_creation_failed',
                                                                  'failure_class': 'request_invalid'})
        causes = {}
        for fallback in ('on', 'off'):
            res, _ = self.run_roll(host=host, env={'AGENTCOMPASS_ROLL_FALLBACK': fallback}, task_id='t-' + fallback)
            causes[fallback] = res['cause']
            self.setUp()
        self.assertEqual(causes['on'], causes['off'])
        self.assertIn('request_invalid', causes['on'])


class RefusedBeforeLaunchDetail(Plain):
    """Finding 6."""

    def test_the_refusal_text_of_a_first_launch_is_in_the_attempt_line(self):
        text = 'Account claude-lab five_hour is below its existing launch protection (5% remaining floor); refresh quota or replan'
        with patch.object(registry, 'check_launch_quota', side_effect=ValueError(text)):
            res, _ = self.run_roll(task_id='t-cap')
        self.assertEqual(res['status'], 'blocked')
        self.assertIn('5% remaining floor', res['next_action'])

    def test_line_prints_the_detail_of_any_attempt_that_has_one(self):
        import dispatch_fallback
        line = dispatch_fallback.line({'attempt': 2, 'model': 'm', 'account': 'a', 'failure': 'not_prepared', 'detail': 'the reason'})
        self.assertIn('the reason', line)


class ResumeRefusals(Plain):
    """Finding 7."""

    def resume(self, name, **over):
        return self.run_roll(self.brief_file(f'{name}.md'), resume='agent-' + name, task_id='t-' + name, **over)

    def test_the_way_out_follows_the_reason(self):
        cases = {   # label: (worker options, what the way out says, what it must not say)
            'permission': (dict(doc={'attentionReason': 'permission'}), 'respond_to_permission'),
            'running': (dict(final_status='running'), 'Wait until its turn ends'),
            'status-error': (dict(final_status='error'), 'get_agent_activity'),
            'provider-unavailable': (dict(doc={'providerUnavailable': True}), 'provider answers again'),
        }
        for label, (options, says) in cases.items():
            with self.subTest(label):
                self.make_worker('agent-' + label, **options)
                res, _ = self.resume(label)
                self.assertEqual(res.get('cause'), 'resume_worker_unavailable', res)
                self.assertIn(says, res['next_action'])
                if label in ('permission', 'running', 'provider-unavailable'):
                    self.assertNotIn('start a fresh worker', res['next_action'])      # a fresh worker answers none of these
                self.setUp()

    def test_the_registry_gives_the_remedy_with_the_reason(self):
        refusal = registry.reuse_refusal('parent-a', 'agent-nobody')
        self.assertTrue(refusal.reason and refusal.remedy)
        self.make_worker('agent-open', finish=False, final_status='running')       # a dispatch to it is still open: its way out is to wait, not a fresh worker
        refusal = registry.reuse_refusal('parent-a', 'agent-open')
        self.assertIn('still open', refusal.reason)
        self.assertIn('Wait for that dispatch', refusal.remedy)
        self.assertNotIn('fresh worker', refusal.remedy)

    def test_a_named_worker_the_host_refuses_says_the_condition(self):
        for code, condition, says in (('worker_not_idle', 'permission_pending', 'respond_to_permission'),
                                      ('worker_not_idle', 'running', 'wait until it ends'),
                                      ('worker_not_found', 'archived', 'unarchive'),
                                      ('host_connection_failed', None, 'daemon'),
                                      ('host_resume_failed', 'resume_failed', 'once')):
            with self.subTest(code=code, condition=condition):
                self.make_worker('agent-n')
                host_answer = {'ok': False, 'sent': False, 'error': code, **({'condition': condition} if condition else {})}

                def host(request, answer=host_answer):
                    if request['operation'] == 'workspace_agents':
                        return {'ok': True, 'placeable': True, 'agents': []}
                    if request['operation'] == 'resume':
                        return dict(answer)
                    return {'ok': False, 'error': 'agent_not_observed'}
                res, _ = self.run_roll(self.brief_file('n.md'), host=host, resume='agent-n', task_id='t-n')
                self.assertEqual(res['cause'], 'resume_worker_unavailable')
                self.assertIn(code, res['next_action'])
                self.assertIn(says, res['next_action'])
                if condition:
                    self.assertIn(condition, res['next_action'])
                if code == 'host_connection_failed':
                    self.assertNotIn('without --resume to let the router choose', res['next_action'])
                self.setUp()

    def test_a_fault_of_the_input_or_the_data_is_not_said_to_be_the_workers(self):
        import roll
        self.make_worker('agent-v')
        with patch.object(registry, 'prepare', side_effect=ValueError('task.profile is required: the classifier always produces it')):
            res, _ = self.resume('v')
        self.assertIn('task.profile is required', res['next_action'])
        self.assertNotIn('cannot take a new turn', res['next_action'])
        self.assertIn('not a state of the worker', res['next_action'])
        import turn_refusal
        with patch.object(turn_refusal, 'gate_refusal', side_effect=ValueError('data row for X is malformed')):
            reason = roll._worker_gate_refusal({'agent_id': 'a', 'bundle': {'account': 'claude-lab', 'bundle_id': 'lab_sonnet55_high'}},
                                               accounts=self.accounts)
        text = roll.gate_refusal_text('a', reason)
        self.assertIn('data row for X is malformed', text)
        self.assertNotIn('cannot take a new turn', text)
        self.assertIn('cannot take a new turn', roll.gate_refusal_text('a', 'account claude-lab is disabled'))


class FailureOutsideTheResultPath(unittest.TestCase):
    """Finding 5."""

    def run_main(self, *argv, raising=None):
        import route_judge
        out = io.StringIO()
        stage = (patch.object(route_judge, '_run_stage', side_effect=raising) if raising else contextlib.nullcontext())
        with patch.object(sys, 'argv', ['agentcompass', *argv]), contextlib.redirect_stdout(out), stage, \
                patch.object(route_judge.public_data, 'refresh_in_background'), \
                patch.object(route_judge.antigravity_plan, 'refresh_in_background'):
            code = route_judge.main()
        return code, json.loads(out.getvalue())

    def test_a_missing_brief_is_named_with_status_cause_and_a_next_action_that_points_at_what_is_there(self):
        missing = str(Path('D:/work/nowhere/brief.md'))
        code, answer = self.run_main('roll', '--brief', missing)
        self.assertEqual(code, 1)
        self.assertEqual(answer['status'], 'error')
        self.assertIn('brief.md', answer['cause'])
        self.assertIn('FileNotFoundError', answer['cause'])
        for field in ('cause', 'next_action'):
            self.assertIn(field, answer)
        self.assertNotIn('detail', answer['next_action'])      # it points at no field that is not there

    def test_every_error_class_gets_a_cause_and_no_free_text_of_a_host(self):
        for error, must in ((OSError(13, 'Permission denied', 'D:/x/y'), 'D:/x/y'), (KeyError('model'), 'model'),
                            (ValueError('fixed text'), 'fixed text'), (RuntimeError('{"reply": "secret-body"}'), 'RuntimeError')):
            with self.subTest(type(error).__name__):
                code, answer = self.run_main('roll', '--task', 'x', raising=error)
                self.assertEqual((code, answer['status']), (1, 'error'))
                self.assertIn(must, answer['cause'])
                self.assertNotIn('secret-body', json.dumps(answer))
                self.assertIn('next_action', answer)


class DotFailures(HandBuilt):
    """Finding 9."""

    def roll_dot(self, returncode, stderr):
        import roll

        def fake_run(command, **_):
            return subprocess.CompletedProcess(command, returncode, '', stderr)
        decision = dict(stage='plan', choice='dot_text', selected_bundle={'provider': 'dot', 'account': 'dot', 'model': 'dot'},
                        decision={'action': 'start'})
        with patch.object(roll.subprocess, 'run', fake_run):
            (result, code), _ = HandBuilt.roll(self, decision, task='x')
        return result, code

    def test_a_runner_that_launched_nothing_says_why_and_never_exits_zero(self):
        for returncode in (1, 2, 3):
            with self.subTest(returncode):
                result, code = self.roll_dot(returncode, 'dot2api is unreachable\n')
                self.assertEqual(code, 1)
                self.assertEqual(result['status'], 'error')
                self.assertEqual(result['cause'], f'dot_runner_exit_{returncode}')
                self.assertIn(f'exited {returncode}', result['next_action'])
                self.assertIn('dot2api is unreachable', result['next_action'])

    def test_what_looks_like_a_credential_is_withheld(self):
        result, _ = self.roll_dot(1, 'Authorization: Bearer ' + 'a' * 40 + '\nplain failure\nDOT2API_API_KEY=abc\n')
        self.assertIn('plain failure', result['next_action'])
        self.assertNotIn('a' * 40, json.dumps(result))
        self.assertNotIn('DOT2API_API_KEY', result['next_action'])

    def test_a_launched_task_keeps_its_exit_code(self):
        result, code = self.roll_dot(3, 'task_id=dot-task-9\n')
        self.assertEqual((code, result['status']), (0, 'launched'))


if __name__ == '__main__':
    unittest.main()
