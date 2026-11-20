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


# What the bridge really answers (setup/paseo_dispatch_bridge.mjs: `main`, `hostFailure`, `execute`; dispatch_flow.host_call for
# the two it makes itself): its `sent`, and the `condition` tokens it merges under a code. A create or a resume that failed
# after the daemon was connected is `sent: true`; one that never reached the daemon is `sent: false`.
CONNECT_CONDITIONS = ('refused', 'timeout', 'authentication', 'credential_unreadable', 'rejected', 'unreachable')
REQUEST_CONDITIONS = ('timeout', 'connection_lost', 'daemon_error', 'invalid_response', 'authentication', 'bridge_error')
SENT_CODES = ('host_request_failed', 'host_timeout', 'invalid_host_response', 'create_outcome_uncertain_reconcile_by_label',
              'resume_outcome_uncertain_reconcile_by_agent')


def bridge_answer(error, condition=None, **fields):
    """The bridge's answer for `error`, with the `sent` its source gives that code."""
    return {'ok': False, 'sent': error in SENT_CODES, 'error': error, **({'condition': condition} if condition else {}), **fields}


def starts_the_daemon(text):
    return 'start the daemon' in text.lower()


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
        # Rule (allocator.plan, quota_pools.quota_wait): an account whose windows are exhausted returns when the last of the
        # exhausted windows resets; the answer names that account, that window and that wait (the fixture sets all three),
        # and says to wait, before the cut that `reason` gets.
        for label, five, seven in (('five-hour-empty', 1, 100), ('both-empty', 1, 1)):
            with self.subTest(label):
                five_hours, seven_hours = 3.0, 100.0
                self.set_remaining(five, seven, five_hours=five_hours, seven_hours=seven_hours)
                res, code = self.run_roll(task_id='t-' + label)
                self.assertEqual((code, res['status']), (2, 'blocked'))
                exhausted = {'five_hour': five_hours} | ({'seven_day': seven_hours} if seven < 5 else {})
                window = max(exhausted, key=exhausted.get)
                for text in (res['reason'][:160], res['next_action']):
                    for fact in ('claude-lab', window, f'{exhausted[window]:.1f}h'):
                        self.assertIn(fact, text)
                self.assertIn('wait', res['next_action'].lower())
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
        # Rule (roll.files_not_sent): only Dot receives --file; any other route's answer says the files were not sent and
        # names the route that did not get them.
        (self.root / 'x.txt').write_text('x', encoding='utf-8')
        res, _ = self.run_roll(file=[self.root / 'x.txt'], task_id='t-file')
        provider = full(res)['route']['bundle']['provider']
        self.assertNotEqual(provider, 'dot')
        self.assertTrue(any('not sent' in w and provider in w for w in res.get('warnings', [])), res)

    def test_a_named_route_that_is_not_dot_is_not_told_it_runs_on_this_machine(self):
        import roll
        out = roll.files_not_sent({}, argparse.Namespace(file=['x'], apply_to=None), 'chatgpt-web')
        self.assertIn('chatgpt-web', out['warnings'][0])
        self.assertNotIn('this machine', out['warnings'][0])


class ExitCodes(Plain):
    """Finding 8: what `roll-reference.md` says about the exit code of each kind of answer is what the code does."""

    def reference_bullet(self):
        text = (Path(__file__).resolve().parents[1] / 'references' / 'roll-reference.md').read_text(encoding='utf-8')
        start = text.index('- A Dot runner that launched nothing')
        return text[start:text.index('\n', start)]

    def test_each_kind_of_answer_exits_as_the_reference_says(self):
        bullet = self.reference_bullet()
        host = LaunchRefusedBeforeAnyWorker.host_answering
        res, code = self.run_roll(host=host(self, bridge_answer('host_connection_failed', 'refused')), task_id='t-x1')
        self.assertEqual((code, res['status']), (1, 'error'))            # refused before any worker existed
        self.assertIn('exits 1', bullet)
        self.setUp()
        text = 'Account claude-lab five_hour is below its existing launch protection (5% remaining floor); refresh quota or replan'
        with patch.object(registry, 'check_launch_quota', side_effect=ValueError(text)):
            res, code = self.run_roll(task_id='t-x2')
        self.assertEqual((code, res['status']), (2, 'blocked'))          # refused before any launch
        self.setUp()
        self.make_worker('agent-x')
        res, code = self.run_roll(self.brief_file('x.md'), resume='agent-x', task_id='t-x3',
                                  host=host(self, bridge_answer('worker_not_idle', 'running')))
        self.assertEqual((code, res['status']), (2, 'blocked'))          # a resume the host refused
        self.assertIn('`blocked` exits 2', bullet)
        self.setUp()
        res, code = self.run_roll(host=host(self, bridge_answer('host_timeout')), task_id='t-x4')
        self.assertEqual((code, res['status']), (0, 'pending'))
        self.assertIn('`pending` result exits 0', bullet)

    def test_the_documents_say_the_plan_fields_are_rounded_up(self):
        base = Path(__file__).resolve().parents[1]
        for name in ('roll-reference.md', 'router-internals.md'):
            text = (base / 'references' / name).read_text(encoding='utf-8')
            at = text.index('decision.start_after_hours')
            sentence = text[at:at + 300]
            self.assertIn('rounded up', sentence, name)


class NamedRouteRefusals(Plain):
    """Finding 4: flags that name a route without the rest are refused in one answer, in `roll`'s words."""
    REQUIRED = ('provider', 'account', 'model')

    def test_each_refusal_names_what_was_given_what_is_missing_and_the_other_way_out(self):
        values = dict(provider='claude-lab', account='claude-lab', model='sonnet', effort='high')
        for given in (('account',), ('provider',), ('model',), ('effort',), ('provider', 'account'), ('account', 'model'),
                      ('provider', 'model'), ('provider', 'account', 'effort')):
            for basis in (None, 'the user said so'):
                with self.subTest(given=given, basis=bool(basis)):
                    with self.assertRaises(ValueError) as caught:
                        self.run_roll(basis=basis, task_id='t-named', **{flag: values[flag] for flag in given})
                    text = str(caught.exception)
                    missing = [flag for flag in self.REQUIRED if flag not in given] + ([] if basis else ['basis'])
                    self.assertTrue(missing)
                    gave_part, missing_part = text.split('This request gave ', 1)[1].split(' and is missing ', 1)
                    for flag in given:
                        self.assertIn(f'--{flag}', gave_part)
                    self.assertEqual('--basis' in gave_part, bool(basis))
                    for flag in missing:
                        self.assertIn(f'--{flag}', missing_part.split('.', 1)[0])
                    self.assertNotIn('--effort', missing_part.split('.', 1)[0])        # optional: never asked for
                    self.assertIn('drop', text)                                        # the other way out: no named route
                    self.assertNotIn('record', text)                                   # `record` is a stage the caller never ran
                    self.setUp()

    def test_record_reads_the_rule_roll_owns(self):
        import roll
        for given in ({}, {'account': 'a'}, {'provider': 'p', 'account': 'a', 'model': 'm'}, {'effort': 'high', 'basis': 'u'}):
            with self.subTest(given=given):
                args = argparse.Namespace(**{'provider': None, 'account': None, 'model': None, 'effort': None, 'basis': None, **given})
                with self.assertRaises(ValueError) as caught:
                    route_judge.record_constraint(args)
                self.assertEqual(str(caught.exception), roll.named_route_refusal(args, require=True))
        self.assertIsNone(roll.named_route_refusal(argparse.Namespace(provider=None, account=None, model=None, effort=None, basis=None)))

    def test_a_complete_named_route_is_not_refused_for_its_flags(self):
        import roll
        args = argparse.Namespace(provider='p', account='a', model='m', effort=None, basis='the user said so')
        self.assertIsNone(roll.named_route_refusal(args))
        self.assertIsNone(roll.named_route_refusal(argparse.Namespace(provider=None, account=None, model=None, effort=None, basis=None)))

    def test_an_input_error_is_not_sent_to_files_or_the_installation(self):
        text = route_judge.failure_answer(ValueError('A named route needs --provider'), 'roll')['next_action']
        system = route_judge.failure_answer(OSError(13, 'Permission denied', 'D:/x'), 'roll')['next_action']
        self.assertNotEqual(text, system)
        self.assertNotIn('installation', text)
        self.assertNotIn('unreadable', text)
        self.assertIn('installation', system)


class PaseoReplacedByCli(Plain):
    """Finding 4 (CLI): the condition that ended Paseo is said."""

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
    CODES = ('sdk_setup_failed', 'host_connection_failed', 'bridge_unavailable', 'create_send_window_elapsed',
             'parent_agent_not_found')
    STEPS = {   # a step the caller can take for that failure (commands and variables that exist)
        'sdk_setup_failed': ('agentcompass setup',), 'host_connection_failed': ('paseo ls',),
        'bridge_unavailable': ('agentcompass setup', 'Node.js'), 'create_send_window_elapsed': ('Roll the brief again',),
        'parent_agent_not_found': ('PASEO_AGENT_ID', '--parent-id')}

    def host_answering(self, response):
        def host(request):
            if request['operation'] == 'workspace_agents':
                return {'ok': True, 'placeable': True, 'agents': []}
            if request['operation'] in ('create', 'resume'):
                return dict(response)
            return {'ok': False, 'error': 'agent_not_observed'}
        return host

    def test_each_code_says_what_failed_and_never_sends_the_caller_to_collect(self):
        texts = {}
        for fallback in ('on', 'off'):
            for code in self.CODES:
                with self.subTest(code=code, fallback=fallback):
                    answer = bridge_answer(code, 'refused' if code == 'host_connection_failed' else None)
                    res, _ = self.run_roll(host=self.host_answering(answer), env={'AGENTCOMPASS_ROLL_FALLBACK': fallback},
                                           task_id=f't-{code}-{fallback}')
                    self.assertEqual((res['status'], res['cause']), ('error', code))
                    self.assertNotIn('continuation_argv', res['next_action'])
                    self.assertNotIn('collect the result', res['next_action'])
                    self.assertTrue(any(step in res['next_action'] for step in self.STEPS[code]), res['next_action'])
                    texts.setdefault(fallback, {})[code] = res['next_action']
                    self.setUp()
        for by_code in texts.values():       # a code-specific fact, not one text for every failure
            self.assertEqual(len(set(by_code.values())), len(by_code))

    def test_the_setup_hint_of_the_bridge_reaches_the_caller_as_a_sentence(self):
        for hint in ('Paseo bridge dependencies are missing. Run: agentcompass setup, or set ROUTER_PASEO_SDK_ROOT.',
                     'Paseo bridge dependencies are missing. Run: agentcompass setup'):
            with self.subTest(hint):
                res, _ = self.run_roll(host=self.host_answering(bridge_answer('sdk_setup_failed', setupHint=hint)), task_id='t-hint')
                self.assertIn(hint.rstrip('.') + '.', res['next_action'])
                self.setUp()


class ConditionAdvice(Plain):
    """Finding 2 of round 2: the advice follows the bridge's `condition`, from one table that create and resume both read."""

    def resume_refused_by(self, answer):
        self.make_worker('agent-n')

        def host(request):
            if request['operation'] == 'workspace_agents':
                return {'ok': True, 'placeable': True, 'agents': []}
            if request['operation'] == 'resume':
                return dict(answer)
            return {'ok': False, 'error': 'agent_not_observed'}
        return self.run_roll(self.brief_file('n.md'), host=host, resume='agent-n', task_id='t-n')[0]

    def test_every_token_of_the_bridges_vocabulary_has_its_facts(self):
        tokens = {*CONNECT_CONDITIONS, *REQUEST_CONDITIONS, *registry.WORKER_STATES}
        facts = {token: flow.condition_facts(token) for token in tokens}
        for token, (what, fix) in facts.items():
            with self.subTest(token):
                self.assertTrue(what.strip() and fix.strip())
        self.assertEqual(len({what for what, _ in facts.values()}), len(tokens))       # each token its own cause

    def test_a_create_that_never_connected_follows_its_condition(self):
        # Rule: starting the daemon is advised where nothing listens (`refused`) and nowhere else; a daemon that is up and
        # refuses the credential, or does not answer, is not helped by being started again.
        for token in CONNECT_CONDITIONS:
            with self.subTest(token):
                res, _ = self.run_roll(host=LaunchRefusedBeforeAnyWorker.host_answering(
                    self, bridge_answer('host_connection_failed', token)), task_id='t-' + token)
                self.assertEqual((res['status'], res['cause']), ('error', 'host_connection_failed'))
                what, fix = flow.HOST_CONDITIONS[token]
                self.assertIn(what.lower(), res['next_action'].lower())
                self.assertIn(fix.lower(), res['next_action'].lower())
                self.assertEqual(starts_the_daemon(res['next_action']), token == 'refused')
                self.setUp()

    def test_a_daemon_that_closed_the_handshake_is_running_and_is_not_started_again(self):
        # Rule: `rejected` is a daemon that accepted the connection, so it runs; the way out is its log and its version.
        what, fix = flow.HOST_CONDITIONS['rejected']
        self.assertIn('closed the handshake', what)
        self.assertFalse(starts_the_daemon(fix))
        self.assertIn('log', fix)
        self.assertIn('version', fix)

    def test_what_did_not_reach_the_daemon_is_not_said_to_have_reached_it(self):
        # Rule (the bridge's `bridge_error`): its own code threw, nothing reached the daemon; every other request class is a
        # daemon or connection failure after the bridge had connected. A text says each one as it is.
        for token in REQUEST_CONDITIONS:
            with self.subTest(token):
                res, _ = self.run_roll(host=LaunchRefusedBeforeAnyWorker.host_answering(
                    self, bridge_answer('host_request_failed', token)), task_id='t-reach-' + token)
                says_reached = 'reached the daemon' in res['next_action'] and 'nothing of it reached' not in res['next_action']
                self.assertFalse(says_reached)
                self.assertEqual('nothing of it reached the daemon' in res['next_action'], token == 'bridge_error')
                self.setUp()

    def test_the_composed_texts_join_their_sentences(self):
        # Rule: a fix is one clause with no semicolon, and the step that follows it starts a new sentence.
        for token, (what, fix) in flow.HOST_CONDITIONS.items():
            self.assertNotIn(';', fix, token)
        for token in (*CONNECT_CONDITIONS, 'some_new_class'):
            text = flow.not_sent_next('host_connection_failed', token)
            self.assertNotRegex(text, r'[,;] then\b', token)
            self.assertRegex(text, r'\. Then roll the brief again\.$', token)
        for token in (*REQUEST_CONDITIONS, 'some_new_class'):
            text = flow.resume_refusal_next('host_resume_failed', token)
            self.assertNotRegex(text, r'[,;] then\b', token)
            self.assertRegex(text, r'\. Then roll --resume again with the same id\.$', token)

    def test_a_named_resume_that_never_connected_reads_the_same_table(self):
        for token in CONNECT_CONDITIONS:
            with self.subTest(token):
                res = self.resume_refused_by(bridge_answer('host_connection_failed', token))
                self.assertEqual(res['cause'], 'resume_worker_unavailable')
                what, fix = flow.HOST_CONDITIONS[token]
                self.assertIn(what.lower(), res['next_action'].lower())
                self.assertIn(fix.lower(), res['next_action'].lower())
                self.assertEqual(starts_the_daemon(res['next_action']), token == 'refused')
                self.setUp()

    def test_a_named_resume_that_failed_before_it_was_sent_reads_the_request_classes(self):
        for token in REQUEST_CONDITIONS:
            with self.subTest(token):
                res = self.resume_refused_by(bridge_answer('host_resume_failed', token))
                what, fix = flow.HOST_CONDITIONS[token]
                self.assertIn(what.lower(), res['next_action'].lower())
                self.assertIn(fix.lower(), res['next_action'].lower())
                self.assertIn('host_resume_failed', res['next_action'])
                self.setUp()

    def test_a_token_the_table_does_not_hold_is_printed_and_presumes_no_cause(self):
        for answer in (bridge_answer('host_connection_failed', 'some_new_class'),):
            res, _ = self.run_roll(host=LaunchRefusedBeforeAnyWorker.host_answering(self, answer), task_id='t-new')
            self.assertIn('some_new_class', res['next_action'])
            self.assertFalse(starts_the_daemon(res['next_action']))
            self.setUp()
            res = self.resume_refused_by(answer)
            self.assertIn('some_new_class', res['next_action'])
            self.assertFalse(starts_the_daemon(res['next_action']))
            self.setUp()

    def test_a_condition_that_is_free_text_is_never_printed(self):
        res, _ = self.run_roll(host=LaunchRefusedBeforeAnyWorker.host_answering(
            self, bridge_answer('host_connection_failed', 'Free text, with a secret=abc')), task_id='t-free')
        self.assertNotIn('secret=abc', json.dumps(res))
        self.assertFalse(starts_the_daemon(res['next_action']))


class PendingNamesWhatIsKnown(Plain):
    """Finding 8: `pending` stays the status while a create may exist; the text and the cause follow the error."""

    def host_answering(self, response):
        return LaunchRefusedBeforeAnyWorker.host_answering(self, response)

    def test_unconfirmed_creates_name_the_error_and_do_not_say_still_creating(self):
        for code in ('host_timeout', 'invalid_host_response', 'create_outcome_uncertain_reconcile_by_label'):
            with self.subTest(code):
                res, code_ = self.run_roll(host=self.host_answering(bridge_answer(code)), task_id='t-' + code)
                self.assertEqual((res['status'], res['cause'], code_), ('pending', code, 0))
                self.assertNotIn('still creating', res['next_action'])
                self.assertIn('Do not roll again', res['next_action'])
                self.assertIn('next_argv', res['next_action'])
                self.assertIn('next_argv', res)
                self.setUp()

    def test_a_request_that_failed_after_it_reached_the_daemon_is_pending_with_its_condition(self):
        # The bridge answers host_request_failed with `sent: true` on a create: a worker may exist.
        for token in REQUEST_CONDITIONS:
            with self.subTest(token):
                answer = bridge_answer('host_request_failed', token)
                self.assertTrue(answer['sent'])
                res, code = self.run_roll(host=self.host_answering(answer), task_id='t-req-' + token)
                self.assertEqual((res['status'], res['cause'], code), ('pending', f'host_request_failed:{token}', 0))
                self.assertIn(flow.HOST_CONDITIONS[token][0].lower(), res['next_action'].lower())
                self.assertNotIn('still creating', res['next_action'])
                self.assertIn('Do not roll again', res['next_action'])
                self.assertIn('next_argv', res)
                self.setUp()

    def test_a_resume_the_daemon_may_have_received_is_pending_with_a_cause_and_its_own_text(self):
        generic = 'Run next_argv to observe the same operation; do not roll again.'
        for answer in (bridge_answer('host_request_failed', 'connection_lost'),
                       bridge_answer('resume_outcome_uncertain_reconcile_by_agent')):
            with self.subTest(answer['error']):
                self.make_worker('agent-n')

                def host(request, answer=answer):
                    if request['operation'] == 'workspace_agents':
                        return {'ok': True, 'placeable': True, 'agents': []}
                    if request['operation'] == 'resume':
                        return dict(answer)
                    return {'ok': False, 'error': 'agent_not_observed'}
                res, _ = self.run_roll(self.brief_file('n.md'), host=host, resume='agent-n', task_id='t-n')
                self.assertEqual(res['status'], 'pending')
                self.assertTrue(res['cause'].startswith(answer['error']), res)
                self.assertNotEqual(res['next_action'], generic)
                self.assertIn('Do not roll again', res['next_action'])
                self.assertIn('next_argv', res)
                if answer.get('condition'):
                    self.assertIn(flow.HOST_CONDITIONS[answer['condition']][0].lower(), res['next_action'].lower())
                self.setUp()

    def test_a_create_the_host_is_carrying_out_is_still_creating(self):
        res, _ = self.run_roll(host=self.host_answering({'ok': True, 'sent': True}), task_id='t-creating')
        self.assertEqual(res['status'], 'pending')
        self.assertIn('still creating', res['next_action'])
        self.assertNotIn('cause', res)


class FallbackKeepsClass(Plain):
    """Finding 12."""

    def test_a_request_scoped_refusal_keeps_its_class_with_the_fallback_on_and_off(self):
        host = LaunchRefusedBeforeAnyWorker.host_answering(self, bridge_answer('host_creation_failed', failure_class='request_invalid'))
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
        self.assertIn(text, res['next_action'])

    def test_line_prints_the_detail_of_any_attempt_that_has_one(self):
        import dispatch_fallback
        line = dispatch_fallback.line({'attempt': 2, 'model': 'm', 'account': 'a', 'failure': 'not_prepared', 'detail': 'the reason'})
        self.assertIn('the reason', line)


class ResumeRefusals(Plain):
    """Finding 7."""

    def resume(self, name, **over):
        return self.run_roll(self.brief_file(f'{name}.md'), resume='agent-' + name, task_id='t-' + name, **over)

    def test_the_way_out_follows_the_reason(self):
        # Rule (dispatch_registry.WORKER_STATES): the text carries the state's own fix, and offers a fresh worker only for
        # a state where one helps.
        cases = {
            'permission': (dict(doc={'attentionReason': 'permission'}), 'permission_pending'),
            'running': (dict(final_status='running'), 'running'),
            'status-error': (dict(final_status='error'), 'error'),
            'provider-unavailable': (dict(doc={'providerUnavailable': True}), 'provider_unavailable'),
            'provider-and-permission': (dict(doc={'providerUnavailable': True, 'attentionReason': 'permission'}), 'provider_unavailable'),
        }
        for label, (options, state) in cases.items():
            with self.subTest(label):
                self.make_worker('agent-' + label, **options)
                res, _ = self.resume(label)
                known = registry.WORKER_STATES[state]
                self.assertEqual(res.get('cause'), 'resume_worker_unavailable', res)
                self.assertIn(known.fix.lower(), res['next_action'].lower())
                self.assertEqual('fresh worker' in res['next_action'], known.fresh)
                self.setUp()

    def test_a_fresh_worker_is_offered_only_where_it_helps(self):
        # Rule (the worker's context is not carried): a fresh worker answers no permission, ends no running turn and does
        # not make a provider answer, so none of those offers one; a worker that is gone, closed or in error can be replaced.
        for state in ('permission_pending', 'running', 'initializing', 'provider_unavailable'):
            self.assertFalse(registry.WORKER_STATES[state].fresh, state)
            self.assertNotIn('fresh worker', registry.resume_way_out(state))
        for state in ('error', 'closed', 'archived'):
            self.assertTrue(registry.WORKER_STATES[state].fresh, state)
            self.assertIn('fresh worker', registry.resume_way_out(state))

    def test_the_registry_gives_the_remedy_with_the_reason(self):
        refusal = registry.reuse_refusal('parent-a', 'agent-nobody')
        self.assertTrue(refusal.reason and refusal.remedy)
        self.make_worker('agent-open', finish=False, final_status='running')       # a dispatch to it is still open: its way out is to wait, not a fresh worker
        refusal = registry.reuse_refusal('parent-a', 'agent-open')
        self.assertIn('still open', refusal.reason)
        self.assertIn('next_argv', refusal.remedy)
        self.assertNotIn('fresh worker', refusal.remedy)

    def test_the_registry_and_the_host_say_the_same_thing_about_a_state(self):
        # One owner (dispatch_registry.WORKER_STATES): the host's condition and the registry's reading give one remedy.
        for state in registry.WORKER_STATES:
            with self.subTest(state):
                self.assertEqual(flow.resume_refusal_next('worker_not_idle', state), registry.resume_way_out(state))
                self.assertEqual(registry.worker_state_refusal(state).remedy, registry.resume_way_out(state))

    def test_a_named_worker_the_host_refuses_says_the_condition(self):
        cases = (bridge_answer('worker_not_idle', 'permission_pending'), bridge_answer('worker_not_idle', 'running'),
                 bridge_answer('worker_not_idle', 'provider_unavailable'), bridge_answer('worker_not_found', 'archived'),
                 bridge_answer('worker_not_found', 'absent'), bridge_answer('host_connection_failed', 'refused'),
                 bridge_answer('worker_not_idle'))
        for answer in cases:
            with self.subTest(answer['error'], condition=answer.get('condition')):
                self.make_worker('agent-n')

                def host(request, answer=answer):
                    if request['operation'] == 'workspace_agents':
                        return {'ok': True, 'placeable': True, 'agents': []}
                    if request['operation'] == 'resume':
                        return dict(answer)
                    return {'ok': False, 'error': 'agent_not_observed'}
                res, _ = self.run_roll(self.brief_file('n.md'), host=host, resume='agent-n', task_id='t-n')
                self.assertEqual(res['cause'], 'resume_worker_unavailable')
                self.assertIn(answer['error'], res['next_action'])
                condition = answer.get('condition')
                if condition:
                    self.assertIn(condition, res['next_action'])
                if condition in registry.WORKER_STATES:
                    self.assertIn(registry.resume_way_out(condition), res['next_action'])
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

    def test_a_flag_naming_another_account_is_an_error_not_a_silent_launch(self):
        # Documented rule (roll-reference.md, `--resume`): flags naming another route are an error; the worker's own route is
        # its own. `--account` is one of them (named_route_conflict).
        self.make_worker('agent-r')
        with self.assertRaises(ValueError) as caught:
            self.run_roll(self.brief_file('r.md'), resume='agent-r', account='another-account', task_id='t-r')
        self.assertIn('--account', str(caught.exception))
        self.assertIn('--resume', str(caught.exception))
        text = (Path(__file__).resolve().parents[1] / 'references' / 'roll-reference.md').read_text(encoding='utf-8')
        sentence = text[text.index('`--resume` names the worker'):].split('\n', 1)[0]
        self.assertIn('`--account`', sentence.split('are an error', 1)[0])


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
    """Finding 9: the runner's exit code and its documented meaning are in the answer; its output is not."""
    OUTPUT = ('Invalid API key: sk-live-0123456789abcdef\n'
              'POST https://alice:hunter2pass@dot.example.net/v1/tasks failed\n'
              'Cookie: sid=Fe26.2**abc123def456**zz\n'
              'signed in as alice.smith@example.com\n'
              '{"error":{"message":"Your organization org-AbCdEf0123 must be verified"}}\n')
    PLANTED = ('sk-live-0123456789abcdef', 'hunter2pass', 'Fe26.2', 'alice.smith@example.com', 'org-AbCdEf0123', 'Invalid API key')

    def roll_dot(self, returncode, stderr):
        import roll

        def fake_run(command, **_):
            return subprocess.CompletedProcess(command, returncode, '', stderr)
        decision = dict(stage='plan', choice='dot_text', selected_bundle={'provider': 'dot', 'account': 'dot', 'model': 'dot'},
                        decision={'action': 'start'})
        with patch.object(roll.subprocess, 'run', fake_run):
            (result, code), _ = HandBuilt.roll(self, decision, task='x')
        return result, code

    def test_a_runner_that_launched_nothing_says_its_exit_code_and_what_it_means_and_never_exits_zero(self):
        import roll
        for returncode in (1, 2, 3, 9):
            with self.subTest(returncode):
                result, code = self.roll_dot(returncode, 'dot2api is unreachable\n')
                self.assertEqual(code, 1)
                self.assertEqual(result['status'], 'error')
                self.assertEqual(result['cause'], f'dot_runner_exit_{returncode}')
                self.assertIn(f'exited {returncode}', result['next_action'])
                self.assertIn(roll.DOT_EXIT_MEANING.get(returncode, 'ended abnormally'), result['next_action'])
                self.assertIn('details', result['next_action'])

    def test_the_meanings_are_the_runners_own(self):
        # Rule: roll states what the runner documents (SKILL.md, Outcomes), by the runner's EXIT_* constants.
        import roll
        runner = (Path(__file__).resolve().parents[2] / 'dot-subagent' / 'scripts' / 'run_dot_task.py').read_text(encoding='utf-8')
        for name, value in (('EXIT_FAILED', 1), ('EXIT_USAGE', 2), ('EXIT_PENDING', 3)):
            self.assertIn(f'{name} = {value}', runner)
            self.assertIn(value, roll.DOT_EXIT_MEANING)

    def test_nothing_of_the_runners_output_is_in_the_answer_but_it_is_kept_in_the_details_file(self):
        for returncode in (1, 2, 3):
            with self.subTest(returncode):
                result, _ = self.roll_dot(returncode, self.OUTPUT)
                for planted in self.PLANTED:
                    self.assertNotIn(planted, json.dumps(result))
                    self.assertNotIn(planted, result['next_action'])
                details = Path(result['details'])
                self.assertTrue(details.exists())
                self.assertIn('hunter2pass', details.read_text(encoding='utf-8'))      # kept there, as the answer says

    def test_a_launched_task_keeps_its_exit_code(self):
        result, code = self.roll_dot(3, 'task_id=dot-task-9\n')
        self.assertEqual((code, result['status']), (0, 'launched'))


if __name__ == '__main__':
    unittest.main()
