"""roll retries and relaunches a failed dispatch by itself (owner requirement 2026-11-10): isolated registry, fake host
that keeps each worker's state, fake judge that allocates with the real allocator. No live host or classifier."""
from datetime import timedelta
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import allocator
import dispatch_fallback as fallback
import dispatch_flow as flow
import dispatch_registry as registry
import provider_retry

from router_test_support import isolated_router_state
import test_dispatch


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup, policy=False)


class Host:
    """A Paseo daemon that keeps every worker it created. `refuse` maps a provider/model to the create failure it
    answers; `fail` maps one to what its worker does after it starts."""

    def __init__(self, test):
        self.test = test
        self.agents = {}          # dispatch id -> snapshot
        self.creates = []
        self.refuse = {}
        self.terminal = {}        # dispatch id -> the watcher's terminal receipt
        self.alive_at_create = []

    def alive(self):
        return [a for a in self.agents.values() if a['status'] in ('running', 'initializing')]

    def __call__(self, request):
        op = request['operation']
        if op == 'create':
            r = request['request']
            self.creates.append(r['provider'])
            # The point of the whole feature: never a second live worker on the brief.
            self.alive_at_create.append(len(self.alive()))
            if r['provider'] in self.refuse:
                return {'ok': False, 'sent': False, 'error': 'host_creation_failed', **self.refuse[r['provider']]}
            provider, _, model = r['provider'].partition('/')
            settings = r.get('settings') or {}
            self.agents[request['dispatch_id']] = {
                'id': request['dispatch_id'], 'provider': provider, 'model': model, 'status': 'running',
                'currentModeId': settings.get('modeId'), 'thinkingOptionId': settings.get('thinkingOptionId'),
                'updatedAt': registry.stamp(registry.utcnow() + timedelta(seconds=1)), 'labels': r['labels'],
                'initialPrompt': r['initialPrompt']}
            return {'ok': True, 'sent': True, 'agent': dict(self.agents[request['dispatch_id']]),
                    'completion_notification': {'status': 'watching'}}
        if op == 'observe':
            agent = self.agents.get(request['dispatch_id'])
            note = {'terminal': self.terminal[request['dispatch_id']]} if request['dispatch_id'] in self.terminal else None
            return {'ok': True, 'agent': dict(agent) if agent else None, 'creation': None,
                    'completion_notification': note}
        if op == 'workspace_agents':
            return {'ok': True, 'agents': []}
        raise AssertionError(op)

    def end(self, dispatch_id, status='error', terminal=None, **changes):
        """The worker's turn ends; `terminal` is what the bridge's watcher recorded."""
        self.agents[dispatch_id].update(status=status, updatedAt=registry.stamp(registry.utcnow() + timedelta(seconds=2)),
                                        **changes)
        if terminal is not None:
            self.terminal[dispatch_id] = {'at': registry.stamp(registry.utcnow() + timedelta(seconds=2)), **terminal}


class FallbackTests(unittest.TestCase):
    setUp_dispatch, snapshot = test_dispatch.DispatchTests.setUp, test_dispatch.DispatchTests.snapshot
    issue = test_dispatch.DispatchTests.issue

    def setUp(self):
        self.setUp_dispatch()
        self.retry_store = self.root / provider_retry.FILE
        env = patch.dict(os.environ, {'ROUTER_PROVIDER_RETRY': str(self.retry_store), 'ROUTER_STATE_DIR': str(self.root),
                                      'ROUTER_MODEL_LATCH': str(self.root / 'latch.json')})
        env.start()
        self.addCleanup(env.stop)
        accounts = patch.object(allocator, 'load_accounts', return_value=self.accounts)
        accounts.start()
        self.addCleanup(accounts.stop)
        self.scopes = []

        def classify(account, provider, model, message, failure):
            return (self.scopes.pop(0) if self.scopes else 'unknown'), {'source': 'system_one', 'scorer_type': 'fake'}
        classifier = patch.object(provider_retry, 'classify_scope', side_effect=classify)
        classifier.start()
        self.addCleanup(classifier.stop)
        self.host = Host(self)
        self.ledger, self.plans, self.slept = [], [], []
        test = self

        class Judge:
            SCORER_TIMEOUT_SECONDS = 30

            @staticmethod
            def plan_stage(document, args):
                test.plans.append(sorted(document.get('unavailable') or ()))
                planned = allocator.plan(dict(document['task']), test.usage, registry.utcnow(),
                                         unavailable=document.get('unavailable') or (), accounts=test.accounts)
                return dict(planned, **registry.issue(planned, document['task'])), 0

            @staticmethod
            def usage_snapshot(_):
                return test.usage

            @staticmethod
            def append_ledger(entry):
                test.ledger.append(entry)
        self.judge = Judge
        self.launch = {'title': 'Fixture task', 'initialPrompt': 'Only fixture work'}

    # Helpers.

    def first(self, task=None, decision=None):
        """The parent's dispatch: planned, prepared and launched as roll does."""
        task = task or self.task
        if decision is None:
            planned = allocator.plan(dict(task), self.usage, self.now, accounts=self.accounts)
            decision = dict(planned, **registry.issue(planned, task, self.now))
        row = registry.prepare(decision['decision_id'], task, self.launch, self.usage, accounts=self.accounts)
        flow.launch(row['dispatch_id'], 'parent-a', host=self.host, watch_seconds=0)
        return registry.inspect(row['dispatch_id'])

    def handle(self, dispatch_id):
        """What the bridge's watcher runs when it sees the attempt fail."""
        return fallback.handle(dispatch_id, judge=self.judge, host=self.host, sleep=self.slept.append)

    def fallback_lines(self):
        return [e for e in self.ledger if e.get('stage') == 'fallback']

    def assert_one_worker(self):
        self.assertTrue(all(n == 0 for n in self.host.alive_at_create), self.host.alive_at_create)
        self.assertLessEqual(len(self.host.alive()), 1)

    # Covered failures: the next candidate starts in the same dispatch.

    def test_create_failed_with_invalid_model_relaunches_on_the_next_candidate(self):
        import model_latch
        self.host.refuse['claude-lab/claude-sonnet-5-5'] = {'failure_class': 'unknown', 'failure_code': 'INVALID_MODEL'}
        root = self.first()
        self.assertEqual(root['state'], 'create_failed')
        out = self.handle(root['dispatch_id'])
        self.assertEqual(out['action'], 'relaunched')
        new = registry.inspect(out['dispatch_id'])
        self.assertEqual((new['attempt_of'], new['attempt'], new['previous_attempt']), (root['dispatch_id'], 2, root['dispatch_id']))
        self.assertNotEqual(new['bundle']['model'], 'claude-sonnet-5-5')
        self.assertIn('claude-lab/claude-sonnet-5-5', model_latch.active())
        self.assertIn('INVALID_MODEL', out['lines'][0])
        self.assertIn(f"switched to {new['bundle']['model']}", out['lines'][0])
        self.assert_one_worker()
        # The new worker is told what failed and to look at the workspace first.
        prompt = self.host.agents[new['dispatch_id']]['initialPrompt']
        self.assertIn('claude-sonnet-5-5', prompt.split('---')[0])
        self.assertIn('uncommitted changes', prompt)
        self.assertTrue(prompt.endswith('Only fixture work'))

    def test_a_bundle_refused_at_prepare_goes_to_the_next_candidate(self):
        """A live model list that dropped the model after the plan (any exclusion) refuses the bundle at prepare."""
        planned = allocator.plan(dict(self.task), self.usage, self.now, accounts=self.accounts)
        decision = dict(planned, **registry.issue(planned, self.task, self.now))
        dropped = planned['selected_bundle']
        real_plan = allocator.plan

        def plan(task, usage, now=None, unavailable=(), **kwargs):
            # From now on the provider no longer offers the planned model.
            return real_plan(task, usage, now, unavailable=[*unavailable, dropped['bundle_id']], **kwargs)
        with patch.object(allocator, 'plan', side_effect=plan):
            with self.assertRaises(registry.CapacityChanged):
                registry.prepare(decision['decision_id'], self.task, self.launch, self.usage, accounts=self.accounts)
            out = fallback.after_refusal(decision['decision_id'], self.launch, 'Capacity/availability changed',
                                         judge=self.judge, host=self.host, sleep=self.slept.append)
        self.assertEqual(out['action'], 'relaunched')
        row = registry.inspect(out['dispatch_id'])
        self.assertNotEqual(row['bundle']['bundle_id'], dropped['bundle_id'])
        # No row existed for the refused bundle, so the relaunch is the dispatch the parent holds.
        self.assertEqual(row['fallback']['current'], row['dispatch_id'])
        self.assertIn('refused_before_launch', row['fallback']['lines'][0])
        self.assertEqual(self.plans[-1], [dropped['bundle_id']])

    def test_a_worker_that_ended_in_error_is_relaunched_once_it_is_over(self):
        root = self.first()
        self.assertEqual(root['state'], 'started')
        self.host.end(root['dispatch_id'], 'error', terminal={'reason': 'error', 'status': 'error'})
        out = self.handle(root['dispatch_id'])
        self.assertEqual(out['action'], 'relaunched')
        self.assertEqual(registry.inspect(root['dispatch_id'])['state'], 'failed')
        self.assertIn('worker_error', out['lines'][0])
        self.assertEqual(self.plans, [[root['bundle']['bundle_id']]])
        self.assert_one_worker()

    def test_a_stuck_provider_retry_is_relaunched_only_when_the_stop_took(self):
        root = self.first()
        stuck = {'reason': 'provider retry stuck', 'failure_code': 'provider_retry_stuck', 'provider_message': 'overloaded',
                 'attempts': 5, 'partial_work': True, 'cancelled': False}
        self.host.end(root['dispatch_id'], 'running', terminal=stuck)
        refused = self.handle(root['dispatch_id'])
        self.assertEqual((refused['action'], refused['result']), ('final', 'declined'))   # it may still be running
        self.assertEqual(self.host.creates, ['claude-lab/claude-sonnet-5-5'])
        self.assertIn('stop_failed', refused['lines'][0])
        other = self.first(task=dict(self.task, task_id='stuck-2'))
        self.host.end(other['dispatch_id'], 'idle', terminal=dict(stuck, cancelled=True))
        out = self.handle(other['dispatch_id'])
        self.assertEqual(out['action'], 'relaunched')
        self.assertIn('provider_retry_stuck (classifier scope unknown)', out['lines'][0])
        self.assertIn('hold', out['attempts'][0])   # the hold provider_retry recorded, as the tray shows it

    def test_a_turn_failed_by_its_provider_moves_on_and_holds_as_the_classifier_said(self):
        self.scopes = ['account']
        root = self.first()
        self.host.end(root['dispatch_id'], 'error', terminal={
            'reason': 'error', 'failure_code': 'provider_turn_failed', 'provider_message': '429 quota exhausted'})
        out = self.handle(root['dispatch_id'])
        self.assertEqual(out['action'], 'relaunched')
        self.assertIn('claude-lab', provider_retry.active())    # the account, as the classifier scoped it
        new = registry.inspect(out['dispatch_id'])
        self.assertNotEqual(new['bundle']['account'], 'claude-lab')
        self.assertIn('classifier scope account', out['lines'][0])

    def test_the_lease_moves_to_the_new_bundle(self):
        root = self.first()
        self.assertEqual([r['dispatch_id'] for r in registry.reservations()], [root['dispatch_id']])
        self.host.end(root['dispatch_id'], 'error', terminal={'reason': 'error'})
        out = self.handle(root['dispatch_id'])
        held = registry.reservations()
        self.assertEqual([r['dispatch_id'] for r in held], [out['dispatch_id']])
        self.assertEqual(held[0]['choice'], registry.inspect(out['dispatch_id'])['bundle']['bundle_id'])
        self.assertNotEqual(held[0]['choice'], root['bundle']['bundle_id'])

    # Transient, task input, the owner's model, nothing left.

    def test_transient_retries_the_same_bundle_after_its_backoff_when_the_task_can_wait(self):
        self.scopes = ['transient']
        root = self.first(task=dict(self.task, can_wait_hours=1))
        self.host.end(root['dispatch_id'], 'error', terminal={
            'reason': 'error', 'failure_code': 'provider_turn_failed', 'provider_message': '502 bad gateway'})
        real_utcnow = registry.utcnow
        # The backoff passes while the step sleeps (provider_retry: 2 minutes for the first hold).
        with patch.object(registry, 'utcnow', side_effect=lambda: real_utcnow() + timedelta(minutes=3) if self.slept else real_utcnow()):
            out = self.handle(root['dispatch_id'])
        self.assertEqual(out['action'], 'relaunched')
        self.assertEqual(len(self.slept), 1)
        self.assertTrue(100 <= self.slept[0] <= 125, self.slept)   # its hold: 2 minutes from the failure
        new = registry.inspect(out['dispatch_id'])
        self.assertEqual(new['bundle']['bundle_id'], root['bundle']['bundle_id'])
        self.assertIn('retried the same model after its backoff', out['lines'][0])
        self.assertEqual(self.plans, [])   # no re-allocation for a retry
        self.assertEqual(self.fallback_lines()[0]['action'], 'retry_same')

    def test_transient_without_a_wait_starts_the_next_candidate_now(self):
        self.scopes = ['transient']
        root = self.first()
        self.host.end(root['dispatch_id'], 'error', terminal={
            'reason': 'error', 'failure_code': 'provider_turn_failed', 'provider_message': '502 bad gateway'})
        out = self.handle(root['dispatch_id'])
        self.assertEqual(self.slept, [])
        self.assertNotEqual(registry.inspect(out['dispatch_id'])['bundle']['bundle_id'], root['bundle']['bundle_id'])

    def test_a_task_input_failure_is_not_relaunched(self):
        self.scopes = ['task_input']
        root = self.first()
        self.host.end(root['dispatch_id'], 'error', terminal={
            'reason': 'error', 'failure_code': 'provider_turn_failed', 'provider_message': 'prompt is too long'})
        out = self.handle(root['dispatch_id'])
        self.assertEqual((out['action'], out['result'], out['covered']), ('final', 'declined', False))
        self.assertEqual(len(self.host.creates), 1)
        self.assertEqual(flow.public(registry.inspect(root['dispatch_id']))['next_action'], flow.TASK_INPUT_NEXT)

    def owner_named(self):
        decision_bundle = dict(self.bundle)
        result = {'stage': 'record', 'choice': 'constraint', 'constraint': 'user_override', 'basis': 'use sonnet',
                  'selected_bundle': decision_bundle, 'decision': {'action': 'start'}}
        return dict(result, **registry.issue(result, self.task, self.now))

    def test_an_owner_named_model_is_never_switched(self):
        self.scopes = ['model']
        root = self.first(decision=self.owner_named())
        self.host.end(root['dispatch_id'], 'error', terminal={
            'reason': 'error', 'failure_code': 'provider_turn_failed', 'provider_message': 'model overloaded'})
        out = self.handle(root['dispatch_id'])
        self.assertEqual((out['action'], out['result']), ('final', 'owner_named'))
        self.assertEqual(len(self.host.creates), 1)
        self.assertIn('roll it without --model', out['text'])
        self.assertEqual(flow.public(registry.inspect(root['dispatch_id']))['next_action'], out['text'])

    def test_an_owner_named_model_is_retried_after_a_transient_failure(self):
        self.scopes = ['transient']
        root = self.first(decision=self.owner_named())
        self.host.end(root['dispatch_id'], 'error', terminal={
            'reason': 'error', 'failure_code': 'provider_turn_failed', 'provider_message': 'timeout'})
        real_utcnow = registry.utcnow
        with patch.object(registry, 'utcnow', side_effect=lambda: real_utcnow() + timedelta(minutes=3) if self.slept else real_utcnow()):
            out = self.handle(root['dispatch_id'])
        self.assertEqual(out['action'], 'relaunched')
        self.assertEqual(self.host.creates, ['claude-lab/claude-sonnet-5-5'] * 2)

    def test_when_every_candidate_is_held_the_dispatch_ends_blocked_with_each_attempt(self):
        refusal = {'failure_class': 'unknown', 'failure_code': 'INVALID_MODEL'}
        self.host.refuse = type('Every', (dict,), {'__contains__': lambda s, k: True, '__getitem__': lambda s, k: refusal})()
        root = self.first()
        out = self.handle(root['dispatch_id'])
        self.assertEqual((out['action'], out['result']), ('final', 'blocked'))
        self.assertEqual(len(out['attempts']), len(self.host.creates))
        self.assertEqual(len(set(self.host.creates)), len(self.host.creates))   # each bundle at most once
        self.assertGreater(len(self.host.creates), 1)
        self.assertIn('every startable candidate', out['text'])
        for line in out['lines']:
            self.assertIn('INVALID_MODEL', line)
        # The parent's dispatch says so too, and nothing holds a lease.
        self.assertEqual(flow.public(registry.inspect(root['dispatch_id']))['next_action'], out['text'])
        self.assertEqual(registry.reservations(), [])
        self.assertEqual(self.fallback_lines()[-1]['action'], 'blocked')

    # One worker per brief, observation, bookkeeping.

    def test_a_second_observer_reads_the_first_ones_outcome_instead_of_relaunching(self):
        root = self.first()
        self.host.end(root['dispatch_id'], 'error', terminal={'reason': 'error'})
        out = self.handle(root['dispatch_id'])
        again = self.handle(root['dispatch_id'])
        self.assertEqual(again['dispatch_id'], out['dispatch_id'])
        self.assertEqual(len(self.host.creates), 2)

    def test_while_waiting_out_a_backoff_the_brief_counts_as_dispatched(self):
        self.scopes = ['transient']
        root = self.first(task=dict(self.task, can_wait_hours=1))
        self.host.end(root['dispatch_id'], 'error', terminal={
            'reason': 'error', 'failure_code': 'provider_turn_failed', 'provider_message': '503'})
        seen = {}

        def sleep(seconds):
            # Mid-wait, the parent rolls the same brief again: it gets this dispatch back and cannot prepare another.
            seen['pending'] = [r['dispatch_id'] for r in registry.pending()]
            seen['public'] = flow.public(registry.inspect(root['dispatch_id']))
            planned = allocator.plan(dict(self.task, can_wait_hours=1), self.usage, registry.utcnow(), accounts=self.accounts)
            decision = registry.issue(planned, dict(self.task, can_wait_hours=1))
            with self.assertRaisesRegex(ValueError, 'already has an active dispatch'):
                registry.prepare(decision['decision_id'], dict(self.task, can_wait_hours=1), self.launch, self.usage,
                                 accounts=self.accounts)
            self.slept.append(seconds)
        real_utcnow = registry.utcnow
        with patch.object(registry, 'utcnow', side_effect=lambda: real_utcnow() + timedelta(minutes=3) if self.slept else real_utcnow()):
            out = fallback.handle(root['dispatch_id'], judge=self.judge, host=self.host, sleep=sleep)
        self.assertEqual(seen['pending'], [root['dispatch_id']])
        self.assertIn('relaunching this brief itself', seen['public']['next_action'])
        self.assertEqual(out['action'], 'relaunched')

    def test_observing_the_parents_dispatch_follows_the_current_attempt(self):
        root = self.first()
        self.host.end(root['dispatch_id'], 'error', terminal={'reason': 'error'})
        out = self.handle(root['dispatch_id'])
        seen = flow.launch(root['dispatch_id'], 'parent-a', host=self.host, watch_seconds=0)
        self.assertEqual(seen['dispatch_id'], out['dispatch_id'])
        self.assertEqual((seen['root_dispatch_id'], seen['attempt']), (root['dispatch_id'], 2))
        self.assertEqual(seen['state'], 'started')
        self.assertEqual(len(seen['attempts']), 2)
        self.assertEqual(registry.current_attempt(root['dispatch_id']), out['dispatch_id'])
        # The failed row itself points at the relaunch.
        self.assertIn(out['dispatch_id'], flow.public(registry.inspect(root['dispatch_id']))['next_action'])

    def test_each_attempt_is_written_to_the_ledger_and_registry_of_the_same_dispatch(self):
        self.host.refuse['claude-lab/claude-sonnet-5-5'] = {'failure_class': 'model_unavailable'}
        root = self.first()
        out = self.handle(root['dispatch_id'])
        lines = self.fallback_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual((lines[0]['action'], lines[0]['root_dispatch_id'], lines[0]['attempt']),
                         ('switch', root['dispatch_id'], 1))
        self.assertEqual(lines[0]['failed']['failure'], 'create_failed:model_unavailable')
        self.assertEqual(lines[0]['next']['decision_id'], registry.inspect(out['dispatch_id'])['decision_id'])
        attempts = registry.inspect(root['dispatch_id'])['fallback']['attempts']
        self.assertEqual([a['dispatch_id'] for a in attempts], [root['dispatch_id'], out['dispatch_id']])
        self.assertEqual(attempts[0]['state'], 'create_failed')
        # The new attempt's watcher names the parent's dispatch and the attempts.
        note = json.loads((self.root / 'paseo-notifications' / f"{out['dispatch_id']}.json.attempts").read_bytes())
        self.assertEqual(note['root_dispatch_id'], root['dispatch_id'])
        self.assertEqual(len(note['lines']), 2)

    def test_turned_off_it_relaunches_nothing_and_the_texts_say_roll_again(self):
        root = self.first()
        self.host.end(root['dispatch_id'], 'error', terminal={'reason': 'error'})
        with patch.dict(os.environ, {'AGENTCOMPASS_ROLL_FALLBACK': 'off'}):
            self.assertEqual(self.handle(root['dispatch_id']), {'action': 'disabled'})
            flow.reconcile(root['dispatch_id'], host=self.host)
            self.assertIn('before rolling again', flow.public(registry.inspect(root['dispatch_id']))['next_action'])
            self.assertNotIn('AGENTCOMPASS_FALLBACK_ARGV', flow.bridge_env())
        self.assertIn('fallback', json.loads(flow.bridge_env()['AGENTCOMPASS_FALLBACK_ARGV']))


class RollFallbackTests(unittest.TestCase):
    """The roll process itself relaunches when the create fails while it waits for it."""
    setUp = FallbackTests.setUp
    setUp_dispatch = test_dispatch.DispatchTests.setUp

    def test_roll_answers_with_the_relaunched_worker_and_its_attempts(self):
        import argparse
        import roll
        self.host.refuse['claude-lab/claude-sonnet-5-5'] = {'failure_class': 'unknown', 'failure_code': 'INVALID_MODEL'}
        brief = self.root / 'task.md'
        brief.write_bytes(b'# Fixture brief\n\nKeep only the current rules.\n')
        args = argparse.Namespace(brief=brief, file=[], apply_to=None, local_only=None, hours=0.5, title=None,
                                  workspace=None, task_id='roll-fallback', wait=0, parent_id=None, parent_host=None,
                                  parent_address=None, provider=None, model=None, effort=None, basis=None,
                                  usage_cache=None, dry_run=False, verbose=False, task=None, new_worker=True,
                                  resume=None, fast=False, keep_whole=None, access=None, allow_concurrent=None,
                                  account=None, can_wait_hours=None)
        test = self

        def plan_stage(document, _):
            planned = allocator.plan(dict(document['task']), test.usage, registry.utcnow(),
                                     unavailable=document.get('unavailable') or (), accounts=test.accounts)
            return dict(planned, **registry.issue(planned, document['task'])), 0
        judge = type('Judge', (), {'plan_stage': staticmethod(plan_stage),
                                   'usage_snapshot': staticmethod(lambda _: test.usage),
                                   'append_ledger': staticmethod(test.ledger.append),
                                   'subscription_env': staticmethod(lambda account=None: dict(os.environ)),
                                   'cli_prefix': staticmethod(lambda name: [name])})
        with patch.dict(os.environ, {'PASEO_AGENT_ID': 'parent-a', 'ROUTER_PARENT_AGENT_ID': ''}), \
                patch.object(roll, 'WORKER_PROMPTS_DIR', self.root / 'worker-prompts'), \
                patch.object(roll, 'RESULTS_DIR', self.root / 'roll-results'), \
                patch.object(flow, 'paseo_reachable', return_value=True), \
                patch.object(flow, 'host_call', self.host):
            result, code = roll.run(args, judge)
        self.assertEqual((result['status'], code), ('launched', 0))
        self.assertNotEqual(result['worker']['model'], 'claude-sonnet-5-5')
        self.assertEqual(len(result['attempts']), 2)
        self.assertIn('INVALID_MODEL', result['attempts'][0])
        self.assertEqual(self.host.creates[0], 'claude-lab/claude-sonnet-5-5')
        self.assertEqual(len(self.host.alive()), 1)
        detail = json.loads(Path(result['details']).read_text(encoding='utf-8'))
        self.assertEqual(detail['worker']['root_dispatch_id'], registry.inspect(detail['worker']['dispatch_id'])['attempt_of'])


if __name__ == '__main__':
    unittest.main()
