"""roll retries and relaunches a failed dispatch by itself (owner requirement 2026-11-10): isolated registry, fake host
that keeps each worker's state, fake judge that allocates with the real allocator. No live host or classifier."""
from datetime import timedelta
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import allocator
import account_fleet
import dispatch_fallback as fallback
import dispatch_flow as flow
import dispatch_registry as registry
import provider_retry

from router_test_support import isolated_router_state
import test_dispatch

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'audit'))
import router_audit  # noqa: E402


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
        # A second account that can take the work when the first attempt's model is refused or held.
        self.usage['accounts']['codex'] = {'status': 'fresh', 'fetched_at': self.now.isoformat(), 'windows': [
            {'id': 'usage', 'used_pct': 0, 'remaining_pct_before_reset': 100,
             'resets_at': (self.now + timedelta(hours=100)).isoformat(), 'seconds_until_reset': 100 * 3600}]}
        self.retry_store = self.root / provider_retry.FILE
        env = patch.dict(os.environ, {'ROUTER_PROVIDER_RETRY': str(self.retry_store), 'ROUTER_STATE_DIR': str(self.root),
                                      'ROUTER_MODEL_LATCH': str(self.root / 'latch.json')})
        env.start()
        self.addCleanup(env.stop)
        accounts = patch.object(account_fleet, 'load_accounts', return_value=self.accounts)
        accounts.start()
        self.addCleanup(accounts.stop)
        self.scopes = []

        def classify(account, provider, model, message, failure):
            return (self.scopes.pop(0) if self.scopes else 'unknown'), {'source': 'system_one', 'scorer_type': 'fake'}
        classifier = patch.object(provider_retry, 'classify_scope', side_effect=classify)
        classifier.start()
        self.addCleanup(classifier.stop)
        self.host = Host(self)
        self.ledger, self.plans = [], []
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
            planned = allocator.plan(dict(task, pinned_bundle='lab_sonnet55_high'), self.usage, self.now, accounts=self.accounts)
            decision = dict(planned, **registry.issue(planned, task, self.now))
        row = registry.prepare(decision['decision_id'], task, self.launch, self.usage, accounts=self.accounts)
        flow.launch(row['dispatch_id'], 'parent-a', host=self.host, watch_seconds=0)
        return registry.inspect(row['dispatch_id'])

    def handle(self, dispatch_id):
        """What the bridge's watcher runs when it sees the attempt fail."""
        return fallback.handle(dispatch_id, judge=self.judge, host=self.host)

    def at(self, **delta):
        """Run the block as if this much time had passed."""
        real = registry.utcnow
        return patch.object(registry, 'utcnow', side_effect=lambda: real() + timedelta(**delta))

    def transient_root(self, task=None, decision=None, minutes=None):
        """A first dispatch whose turn the provider failed transiently; `minutes`: the provider's own retry time."""
        self.scopes = ['transient']
        root = self.first(task=task, decision=decision)
        terminal = {'reason': 'error', 'failure_code': 'provider_turn_failed', 'provider_message': '429 retry later'}
        if minutes:
            terminal['next'] = registry.stamp(registry.utcnow() + timedelta(minutes=minutes))
        self.host.end(root['dispatch_id'], 'error', terminal=terminal)
        return root

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
                                         judge=self.judge, host=self.host)
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
        other_task = dict(self.task, task_id='stuck-2')
        sol = dict(account_fleet.bundles(self.accounts)['sol_high'], bundle_id='sol_high')
        other = self.first(task=other_task, decision=self.issue(task=other_task, choice='sol_high', selected_bundle=sol))
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
        root = self.transient_root(task=dict(self.task, can_wait_hours=1))
        out = self.handle(root['dispatch_id'])
        # Nothing waits in the step: it records the retry time and returns (provider_retry: 2 minutes for a first hold).
        self.assertEqual(out['action'], 'scheduled')
        wait = (registry.parse(out['retry_at']) - registry.utcnow()).total_seconds()
        self.assertTrue(100 <= wait <= 125, wait)
        self.assertEqual(len(self.host.creates), 1)
        fb = registry.inspect(root['dispatch_id'])['fallback']
        self.assertEqual((fb['state'], fb['retry_of'], fb['retry_at']), ('retrying', root['dispatch_id'], out['retry_at']))
        with self.at(minutes=3):
            out = self.handle(root['dispatch_id'])
        self.assertEqual(out['action'], 'relaunched')
        new = registry.inspect(out['dispatch_id'])
        self.assertEqual(new['bundle']['bundle_id'], root['bundle']['bundle_id'])
        self.assertIn('retried the same model after its backoff', out['lines'][0])
        self.assertEqual(self.plans, [])   # no re-allocation for a retry
        self.assertEqual([e['action'] for e in self.fallback_lines()], ['scheduled', 'retry_same'])
        fb = registry.inspect(root['dispatch_id'])['fallback']
        self.assertEqual((fb['state'], fb['current']), ('relaunched', new['dispatch_id']))
        self.assertNotIn('retry_at', fb)
        self.assert_one_worker()

    def test_transient_without_a_wait_starts_the_next_candidate_now(self):
        root = self.transient_root()
        out = self.handle(root['dispatch_id'])
        self.assertEqual(out['action'], 'relaunched')
        self.assertNotEqual(registry.inspect(out['dispatch_id'])['bundle']['bundle_id'], root['bundle']['bundle_id'])

    def test_a_backoff_longer_than_the_run_lease_keeps_its_claim_until_the_retry(self):
        """Review F1: a second observer while the backoff runs past RUN_LEASE must not relaunch the brief, and the
        first one must not then rewrite the chain to final/declined."""
        root = self.transient_root(task=dict(self.task, can_wait_hours=1), minutes=16)
        first = self.handle(root['dispatch_id'])
        self.assertEqual(first['action'], 'scheduled')
        self.assertGreater((registry.parse(first['retry_at']) - registry.utcnow()).total_seconds(), registry.RUN_LEASE)
        # Past RUN_LEASE, before the retry time: the watcher looks again, the parent observes, the parent rolls again.
        with self.at(seconds=registry.RUN_LEASE + 60):
            again = self.handle(root['dispatch_id'])
            seen = flow.launch(root['dispatch_id'], 'parent-a', host=self.host, watch_seconds=0)
            self.assertEqual([r['dispatch_id'] for r in registry.pending()], [root['dispatch_id']])
        self.assertEqual((again['action'], again['retry_at']), ('scheduled', first['retry_at']))
        self.assertEqual(seen['retry_at'], first['retry_at'])
        self.assertIn('retries the same model by itself', seen['next_action'])
        self.assertEqual(len(self.host.creates), 1)
        # Due: one observer starts the retry; the other reads what it did.
        with self.at(minutes=17):
            started = self.handle(root['dispatch_id'])
            late = self.handle(root['dispatch_id'])
        self.assertEqual(started['action'], 'relaunched')
        self.assertEqual((late['action'], late['dispatch_id']), ('relaunched', started['dispatch_id']))
        self.assertEqual(len(self.host.creates), 2)
        self.assert_one_worker()
        fb = registry.inspect(root['dispatch_id'])['fallback']
        self.assertEqual((fb['state'], fb['current']), ('relaunched', started['dispatch_id']))
        self.assertEqual(registry.current_attempt(root['dispatch_id']), started['dispatch_id'])

    def test_an_observation_starts_a_due_retry_without_the_watcher(self):
        root = self.transient_root(task=dict(self.task, can_wait_hours=1))
        self.assertEqual(self.handle(root['dispatch_id'])['action'], 'scheduled')
        real_handle = fallback.handle
        with self.at(minutes=3), patch.object(fallback, 'handle', side_effect=lambda dispatch_id, **kw: real_handle(
                dispatch_id, **dict(kw, judge=self.judge))):
            seen = flow.launch(root['dispatch_id'], 'parent-a', host=self.host, watch_seconds=0)
        self.assertEqual(len(self.host.creates), 2)
        self.assertEqual((seen['root_dispatch_id'], seen['attempt'], seen['state']), (root['dispatch_id'], 2, 'started'))
        self.assertEqual(seen['dispatch_id'], registry.current_attempt(root['dispatch_id']))

    # Review round 2 (round 055): G1 the parent cancels a scheduled retry; G2 the guard lives as long as the retry may run.

    def scheduled_root(self, minutes, can_wait_hours=1):
        task = dict(self.task, can_wait_hours=can_wait_hours)
        root = self.transient_root(task=task, minutes=minutes)
        out = self.handle(root['dispatch_id'])
        self.assertEqual(out['action'], 'scheduled')
        return root, task

    def observe_old(self, dispatch_id):
        """The parent observes its dispatch (dispatch launch / next_argv), which starts a due retry (resume_due)."""
        real_handle = fallback.handle
        with patch.object(fallback, 'handle', side_effect=lambda d, **kw: real_handle(d, **dict(kw, judge=self.judge))):
            return flow.launch(dispatch_id, 'parent-a', host=self.host, watch_seconds=0)

    def roll_again(self, task):
        planned = allocator.plan(dict(task), self.usage, registry.utcnow(), accounts=self.accounts)
        decision = dict(planned, **registry.issue(planned, task))
        return registry.prepare(decision['decision_id'], task, self.launch, self.usage, accounts=self.accounts)

    def test_R3_the_parents_release_cancels_a_scheduled_retry(self):
        """Review G1 (probe R3): release on the failed first dispatch cancels the retry in one transaction; the
        watcher, an observation and resume_due that run at the retry time start nothing."""
        root, _ = self.scheduled_root(minutes=16)
        released = registry.release(root['dispatch_id'], 'parent-a', 'parent cancels this brief')
        self.assertEqual(released['canceled_retry'], root['dispatch_id'])
        fb = registry.inspect(root['dispatch_id'])['fallback']
        self.assertEqual((fb['state'], fb['outcome']['result']), ('final', 'canceled'))
        self.assertEqual(fb['handled'][root['dispatch_id']]['result'], 'canceled')
        self.assertFalse({'until', 'retry_at', 'retry_of'} & set(fb))
        self.assertEqual(registry.pending(), [])
        self.assertEqual(flow.public(registry.inspect(root['dispatch_id']))['next_action'], registry.RETRY_CANCELED_NEXT)
        with self.at(minutes=17):
            watcher = self.handle(root['dispatch_id'])
            seen = self.observe_old(root['dispatch_id'])
            self.assertIsNone(fallback.resume_due(root['dispatch_id'], host=self.host))
        self.assertEqual((watcher['action'], watcher['result']), ('final', 'canceled'))
        self.assertEqual(seen['dispatch_id'], root['dispatch_id'])
        self.assertEqual(self.host.creates, ['claude-lab/claude-sonnet-5-5'])

    def test_the_release_command_stops_the_watcher_of_the_canceled_retry(self):
        import argparse
        root, _ = self.scheduled_root(minutes=16)
        notes = self.root / 'paseo-notifications'
        notes.mkdir(exist_ok=True)
        (notes / f"{root['dispatch_id']}.json").write_bytes(b'{}')
        args = argparse.Namespace(stage='dispatch', dispatch_action='release', dispatch_id=root['dispatch_id'],
                                  parent_id='parent-a', basis='the owner dropped this brief')
        with patch.object(flow, 'require_actual_parent'), patch.object(flow, 'reconcile', return_value=[]):
            out = flow.cli(args)
        self.assertEqual(out['canceled_retry'], root['dispatch_id'])
        self.assertTrue((notes / f"{root['dispatch_id']}.json.creation-failed").exists())

    def test_R4_the_brief_cannot_be_dispatched_again_while_its_retry_may_run(self):
        """Review G2 (probe R4): past retry_at + RUN_LEASE with no watcher and no observer, the guard still holds, so
        rolling the brief again is refused; the next observation then runs the one retry."""
        root, task = self.scheduled_root(minutes=3)
        with self.at(minutes=3 + 11):
            self.assertEqual([r['dispatch_id'] for r in registry.pending()], [root['dispatch_id']])
            self.assertIn('retries the same model by itself', flow.public(registry.inspect(root['dispatch_id']))['next_action'])
            with self.assertRaisesRegex(ValueError, 'already has an active dispatch'):
                self.roll_again(task)
        with self.at(minutes=3 + 40):
            seen = self.observe_old(root['dispatch_id'])
        self.assertEqual((seen['attempt'], seen['state']), (2, 'started'))
        self.assertEqual(len(self.host.creates), 2)
        self.assert_one_worker()

    def test_R4_a_retry_nobody_started_within_the_wait_is_retired_not_run_beside_a_new_dispatch(self):
        """Review G2: once the wait the task allows is over the guard lapses and the brief may be rolled again; the old
        retry is then retired by whatever touches it, so the brief never runs twice."""
        root, task = self.scheduled_root(minutes=3)
        with self.at(hours=1, minutes=1):
            self.assertEqual(registry.pending(), [])
            row = self.roll_again(task)
            flow.launch(row['dispatch_id'], 'parent-a', host=self.host, watch_seconds=0)
            self.host.end(row['dispatch_id'], 'idle', terminal={'reason': 'finished'})
            registry.observe(row['dispatch_id'], self.host.agents[row['dispatch_id']])
        self.assertEqual(len(self.host.creates), 2)
        with self.at(hours=1, minutes=30):
            seen = self.observe_old(root['dispatch_id'])
            late_watcher = self.handle(root['dispatch_id'])
        self.assertEqual(len(self.host.creates), 2)
        self.assertEqual(seen['dispatch_id'], root['dispatch_id'])
        self.assertEqual(seen['next_action'], fallback.RETRY_EXPIRED_NEXT)
        self.assertEqual((late_watcher['action'], late_watcher['result']), ('final', 'retry_expired'))
        fb = registry.inspect(root['dispatch_id'])['fallback']
        self.assertEqual((fb['state'], fb['outcome']['result']), ('final', 'retry_expired'))

    def test_the_guard_of_a_scheduled_retry_lasts_the_wait_the_task_allows(self):
        root, _ = self.scheduled_root(minutes=3, can_wait_hours=2)
        fb = registry.inspect(root['dispatch_id'])['fallback']
        hours = (registry.parse(fb['until']) - registry.parse(fb['handled'][root['dispatch_id']]['retry_at'])).total_seconds()
        self.assertGreater(hours, 3600 * 1.9)   # can_wait_hours from the failure, well past retry_at + RUN_LEASE

    def test_a_stale_step_on_a_live_chain_never_rewrites_it_to_final(self):
        """A step whose prepare is refused because another attempt of the brief is live leaves the chain as it is."""
        root = self.first()
        self.host.end(root['dispatch_id'], 'error', terminal={'reason': 'error'})
        out = self.handle(root['dispatch_id'])
        chain = fallback.Chain.of(registry.inspect(root['dispatch_id']), judge=self.judge, host=self.host)
        failed = registry.inspect(root['dispatch_id'])
        stale = chain.run(fallback.failure_of(failed), failed)
        self.assertEqual((stale['action'], stale['dispatch_id']), ('relaunched', out['dispatch_id']))
        fb = registry.inspect(root['dispatch_id'])['fallback']
        self.assertEqual((fb['state'], fb['current']), ('relaunched', out['dispatch_id']))
        self.assert_one_worker()

    def test_the_same_bundle_retry_is_a_routed_start_for_the_audit(self):
        """Review F3: the retry writes a plan line with its choice, so router_audit.find_unrouted_spawns accepts it."""
        root = self.transient_root(task=dict(self.task, can_wait_hours=1), minutes=16)
        self.handle(root['dispatch_id'])
        with self.at(minutes=17):
            out = self.handle(root['dispatch_id'])
            spawned = registry.utcnow()
        self.assertEqual(out['action'], 'relaunched')
        routed = [e for e in self.ledger if e.get('stage') in ('plan', 'select', 'record')]
        self.assertEqual(len(routed), 1)
        self.assertEqual((routed[0]['choice'], routed[0]['decision_id'], routed[0]['root_dispatch_id']),
                         (root['bundle']['bundle_id'], registry.inspect(out['dispatch_id'])['decision_id'],
                          root['dispatch_id']))
        # Through the audit's own ledger reader.
        ledger = self.root / 'audit-ledger.jsonl'
        ledger.write_bytes(''.join(json.dumps(e) + '\n' for e in self.ledger).encode('utf-8'))
        rows = router_audit.from_ledger(ledger)
        spawns = [{'at': spawned, 'label': 'same-bundle retry'}]
        self.assertEqual(router_audit.find_unrouted_spawns(spawns, rows), [])
        self.assertEqual(len(router_audit.find_unrouted_spawns(spawns, [r for r in rows if r['stage'] == 'fallback'])), 1)

    def test_a_task_input_failure_is_not_relaunched(self):
        self.scopes = ['task_input']
        root = self.first()
        self.host.end(root['dispatch_id'], 'error', terminal={
            'reason': 'error', 'failure_code': 'provider_turn_failed', 'provider_message': 'prompt is too long'})
        out = self.handle(root['dispatch_id'])
        self.assertEqual((out['action'], out['result'], out['covered']), ('final', 'declined', False))
        self.assertEqual(len(self.host.creates), 1)
        self.assertEqual(flow.public(registry.inspect(root['dispatch_id']))['next_action'], flow.TASK_INPUT_NEXT)

    def owner_named(self, task=None):
        decision_bundle = dict(self.bundle)
        result = {'stage': 'record', 'choice': 'constraint', 'constraint': 'user_override', 'basis': 'use sonnet',
                  'selected_bundle': decision_bundle, 'decision': {'action': 'start'}}
        return dict(result, **registry.issue(result, task or self.task, self.now))

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

    def test_an_owner_named_model_is_retried_after_a_transient_failure_when_the_task_can_wait(self):
        task = dict(self.task, can_wait_hours=1)
        root = self.transient_root(task=task, decision=self.owner_named(task))
        self.assertEqual(self.handle(root['dispatch_id'])['action'], 'scheduled')
        with self.at(minutes=3):
            out = self.handle(root['dispatch_id'])
        self.assertEqual(out['action'], 'relaunched')
        self.assertEqual(self.host.creates, ['claude-lab/claude-sonnet-5-5'] * 2)
        routed = [e for e in self.ledger if e.get('stage') in ('plan', 'select', 'record')]
        self.assertEqual([(e['stage'], e['constraint']) for e in routed], [('record', 'user_override')])

    def test_an_owner_named_model_waits_only_what_the_task_allows(self):
        """Review F2: the owner's model follows can_wait_hours too; a backoff past it reports to the parent."""
        for name, task, minutes in (('no wait given', dict(self.task, task_id='owner-a'), None),
                                    ('backoff past the wait', dict(self.task, task_id='owner-b', can_wait_hours=1), 120)):
            with self.subTest(name):
                creates = len(self.host.creates)
                root = self.transient_root(task=task, decision=self.owner_named(task), minutes=minutes)
                out = self.handle(root['dispatch_id'])
                self.assertEqual((out['action'], out['result']), ('final', 'owner_named'))
                self.assertEqual(len(self.host.creates), creates + 1)
                fb = registry.inspect(root['dispatch_id'])['fallback']
                self.assertEqual(fb['state'], 'final')
                self.assertNotIn('until', fb)
                self.assertIn('roll it without --model', out['text'])

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
        task = dict(self.task, can_wait_hours=1)
        root = self.transient_root(task=task)
        self.assertEqual(self.handle(root['dispatch_id'])['action'], 'scheduled')
        # Mid-wait, the parent rolls the same brief again: it gets this dispatch back and cannot prepare another.
        self.assertEqual([r['dispatch_id'] for r in registry.pending()], [root['dispatch_id']])
        public = flow.public(registry.inspect(root['dispatch_id']))
        self.assertIn('retries the same model by itself', public['next_action'])
        planned = allocator.plan(dict(task), self.usage, registry.utcnow(), accounts=self.accounts)
        decision = registry.issue(planned, task)
        with self.assertRaisesRegex(ValueError, 'already has an active dispatch'):
            registry.prepare(decision['decision_id'], task, self.launch, self.usage, accounts=self.accounts)
        with self.at(minutes=3):
            self.assertEqual(self.handle(root['dispatch_id'])['action'], 'relaunched')

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

    def test_parent_followup_retires_scheduled_retry_and_due_calls_start_nothing(self):
        root, _ = self.scheduled_root(minutes=16)
        self.assertEqual([r['dispatch_id'] for r in registry.pending()], [root['dispatch_id']])
        # The parent sends a follow-up prompt to the failed worker; it runs again before the backoff is over.
        self.host.end(root['dispatch_id'], 'running', attentionReason='error')   # Paseo keeps the stale flag
        flow.reconcile(host=self.host)
        fb = registry.inspect(root['dispatch_id'])['fallback']
        self.assertEqual((fb['state'], fb['outcome']['result'], fb['current']),
                         ('final', 'resumed', root['dispatch_id']))
        self.assertEqual(fb['handled'][root['dispatch_id']]['result'], 'resumed')
        self.assertFalse({'until', 'retry_at', 'retry_of'} & set(fb))
        self.assertEqual(registry.pending(), [])
        self.assertEqual(registry.current_attempt(root['dispatch_id']), root['dispatch_id'])
        self.assertEqual(flow.public(registry.inspect(root['dispatch_id']))['next_action'], registry.RESUMED_NEXT)
        # Even once the resumed worker finishes and the scheduled retry time passes, nothing starts.
        self.host.end(root['dispatch_id'], 'idle', attentionReason='finished')
        with self.at(minutes=17):
            self.assertIsNone(fallback.resume_due(root['dispatch_id'], host=self.host))
            watcher = self.handle(root['dispatch_id'])
            seen = self.observe_old(root['dispatch_id'])
        self.assertEqual((watcher['action'], watcher['result']), ('final', 'resumed'))
        self.assertEqual(seen['dispatch_id'], root['dispatch_id'])
        self.assertEqual(len(self.host.creates), 1)
        self.assertFalse(any('switched to' in line for line in watcher.get('lines') or ()))

    def test_parent_followup_retires_pending_switch_and_restores_current_attempt(self):
        task = dict(self.task, can_wait_hours=1)
        root = self.first(task=task)
        self.host.end(root['dispatch_id'], 'error', terminal={'reason': 'error'})
        flow.reconcile(root['dispatch_id'], host=self.host)
        second = self.handle(root['dispatch_id'])
        self.assertEqual(second['action'], 'relaunched')
        self.assertEqual(registry.current_attempt(root['dispatch_id']), second['dispatch_id'])
        # Attempt 2 fails and its switch is claimed/pending while the parent resumes attempt 1.
        self.host.end(second['dispatch_id'], 'error', terminal={'reason': 'error'})
        flow.reconcile(second['dispatch_id'], host=self.host)
        chain = fallback.Chain.of(registry.inspect(root['dispatch_id']), judge=self.judge, host=self.host)
        chain.claim(second['dispatch_id'])
        self.assertEqual([r['dispatch_id'] for r in registry.pending()], [root['dispatch_id']])
        self.host.end(root['dispatch_id'], 'running', attentionReason='error')   # Paseo keeps the stale flag
        flow.reconcile(host=self.host)
        fb = registry.inspect(root['dispatch_id'])['fallback']
        self.assertEqual((fb['state'], fb['outcome']['result'], fb['current']),
                         ('final', 'resumed', root['dispatch_id']))
        self.assertEqual(fb['handled'][second['dispatch_id']]['result'], 'resumed')
        self.assertEqual(registry.current_attempt(root['dispatch_id']), root['dispatch_id'])
        self.assertEqual(self.handle(second['dispatch_id'])['result'], 'resumed')
        self.assertEqual(len(self.host.creates), 2)

    def test_failed_attempt_running_again_when_handle_runs_does_not_relaunch(self):
        # 2026-11-11 incident: worker ends in error (failure_code=None -> worker_error/switch), parent resumes it,
        # then fallback handle runs. Neither before replan nor right before launch may a second worker start.
        for mode in ('already_running', 'resumed_during_replan'):
            with self.subTest(mode=mode):
                creates_before = len(self.host.creates)
                root = self.first(task=dict(self.task, task_id=f'resumed-{mode}'))
                self.host.end(root['dispatch_id'], 'error', terminal={'reason': 'error'})
                flow.reconcile(root['dispatch_id'], host=self.host)
                self.assertEqual(registry.inspect(root['dispatch_id'])['state'], 'failed')
                if mode == 'already_running':
                    self.host.end(root['dispatch_id'], 'running', attentionReason='error')   # Paseo keeps the stale flag
                    out = self.handle(root['dispatch_id'])
                else:
                    real_plan = self.judge.plan_stage

                    def resume_then_plan(document, args):
                        self.host.end(root['dispatch_id'], 'running', attentionReason='error')   # Paseo keeps the stale flag
                        return real_plan(document, args)
                    with patch.object(self.judge, 'plan_stage', side_effect=resume_then_plan):
                        out = self.handle(root['dispatch_id'])
                self.assertEqual((out['action'], out['result']), ('final', 'resumed'))
                self.assertEqual(len(self.host.creates), creates_before + 1)
                self.assertEqual(registry.current_attempt(root['dispatch_id']), root['dispatch_id'])
                self.assertFalse(any('switched to' in line for line in out['lines']), out['lines'])
                notes = self.root / 'paseo-notifications'
                self.assertFalse(list(notes.glob('*.attempts')) if notes.exists() else [])
                fb = registry.inspect(root['dispatch_id'])['fallback']
                self.assertEqual((fb['state'], fb['outcome']['result'], fb['current']),
                                 ('final', 'resumed', root['dispatch_id']))
                self.assertFalse(any('switched to' in line for line in fb['lines']), fb['lines'])
                self.assertEqual(self.fallback_lines()[-1]['action'], 'resumed')


class RollFallbackTests(unittest.TestCase):
    """The roll process itself relaunches when the create fails while it waits for it."""
    setUp = FallbackTests.setUp
    setUp_dispatch = test_dispatch.DispatchTests.setUp
    snapshot = test_dispatch.DispatchTests.snapshot
    issue = test_dispatch.DispatchTests.issue
    first = FallbackTests.first
    handle = FallbackTests.handle

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
            first = not document.get('unavailable')
            planned = allocator.plan(dict(document['task'], **({'pinned_bundle': 'lab_sonnet55_high'} if first else {})),
                                     test.usage, registry.utcnow(),
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

    def test_roll_answers_a_scheduled_retry_as_pending_with_its_time(self):
        import roll
        retry_at = registry.stamp(registry.utcnow() + timedelta(minutes=2))
        out, code = roll.fallback_result({}, {'action': 'scheduled', 'retry_at': retry_at, 'root_dispatch_id': 'root-1',
                                              'lines': ['attempt 1: m on a failed: provider_turn_failed']},
                                         {}, 'parent-a', None)
        self.assertEqual((roll.status_of(out, code), code), ('pending', 0))
        answer = roll.compact(out, 'pending', None)
        self.assertEqual(answer['retry_at'], retry_at)
        self.assertIn(retry_at, answer['next_action'])
        self.assertIn('root-1', answer['next_argv'])

    def test_unclassified_or_request_invalid_create_refusal_stops_with_no_cooldown_and_no_fallback(self):
        import argparse
        import launch_failures
        import roll
        cooldown_store = self.root / 'launch-failures.json'
        for idx, refusal in enumerate((
            {'failure_class': 'unknown'},
            {'failure_class': 'request_invalid'},
        )):
            with self.subTest(refusal=refusal):
                self.host.creates.clear()
                self.host.refuse = {'claude-lab/claude-sonnet-5-5': refusal}
                brief = self.root / f'task-{idx}.md'
                brief.write_bytes(b'# Fixture brief\n\nKeep only the current rules.\n')
                args = argparse.Namespace(brief=brief, file=[], apply_to=None, local_only=None, hours=0.5, title=None,
                                          workspace=None, task_id=f'no-walk-{idx}', wait=0, parent_id=None, parent_host=None,
                                          parent_address=None, provider=None, model=None, effort=None, basis=None,
                                          usage_cache=None, dry_run=False, verbose=False, task=None, new_worker=True,
                                          resume=None, fast=False, keep_whole=None, access=None, allow_concurrent=None,
                                          account=None, can_wait_hours=None)
                test = self

                def plan_stage(document, _):
                    planned = allocator.plan(dict(document['task'], pinned_bundle='lab_sonnet55_high'),
                                             test.usage, registry.utcnow(),
                                             unavailable=document.get('unavailable') or (), accounts=test.accounts)
                    return dict(planned, **registry.issue(planned, document['task'])), 0
                judge = type('Judge', (), {'plan_stage': staticmethod(plan_stage),
                                           'usage_snapshot': staticmethod(lambda _: test.usage),
                                           'append_ledger': staticmethod(test.ledger.append),
                                           'subscription_env': staticmethod(lambda account=None: dict(os.environ)),
                                           'cli_prefix': staticmethod(lambda name: [name])})
                with patch.dict(os.environ, {'PASEO_AGENT_ID': 'parent-a', 'ROUTER_PARENT_AGENT_ID': '',
                                             'ROUTER_LAUNCH_FAILURES': str(cooldown_store)}), \
                        patch.object(roll, 'WORKER_PROMPTS_DIR', self.root / 'worker-prompts'), \
                        patch.object(roll, 'RESULTS_DIR', self.root / 'roll-results'), \
                        patch.object(flow, 'paseo_reachable', return_value=True), \
                        patch.object(flow, 'host_call', self.host):
                    result, code = roll.run(args, judge)
                self.assertEqual((result['status'], code), ('error', 1))
                self.assertEqual(len(self.host.creates), 1)
                self.assertEqual(launch_failures.active(path=cooldown_store), {})
                self.assertFalse(cooldown_store.exists())

        # Contrast property: a candidate-scoped refusal DOES record cooldown and fall back to the next candidate.
        self.host.creates.clear()
        self.host.refuse = {'claude-lab/claude-sonnet-5-5': {'failure_class': 'model_unavailable'}}
        with patch.dict(os.environ, {'ROUTER_LAUNCH_FAILURES': str(cooldown_store)}):
            root = self.first(task=dict(self.task, task_id='cand-walk'))
            out = self.handle(root['dispatch_id'])
            self.assertEqual(out['action'], 'relaunched')
            self.assertEqual(len(self.host.creates), 2)
            self.assertTrue(launch_failures.active(path=cooldown_store))


if __name__ == '__main__':
    unittest.main()
