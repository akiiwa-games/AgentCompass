"""What the `dispatch` actions answer when they did nothing: each says which fact decided it, so "nothing to do",
"nothing found" and "could not look" never share one shape. No state, reservation or lease moves for these answers."""
import json
from datetime import timedelta
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import dispatch_fallback
import dispatch_flow as flow
import dispatch_registry as registry
from router_test_support import isolated_router_state
import test_dispatch as td


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup, policy=False)


def action(name, dispatch_id=None, **changes):
    values = dict(stage='dispatch', dispatch_action=name, dispatch_id=dispatch_id, parent_id='parent-a', basis='give up',
                  input=None, usage_cache=None)
    values.update(changes)
    return SimpleNamespace(**values)


class DispatchAnswerTests(unittest.TestCase):
    snapshot, issue, prepare = td.DispatchTests.snapshot, td.DispatchTests.issue, td.DispatchTests.prepare
    claimed, claim_row = td.CreateLifecycleTests.claimed, td.CreateLifecycleTests.claim_row

    def setUp(self):
        td.DispatchTests.setUp(self)
        state = patch.dict(os.environ, {'ROUTER_STATE_DIR': str(self.root / 'state')})
        state.start()
        self.addCleanup(state.stop)

    def run_action(self, args, host=None):
        def silent(request):
            raise AssertionError(f'no host call expected: {request["operation"]}')
        with patch.object(flow, 'host_call', host or silent):
            return flow.cli(args)

    def fresh(self, task_id):
        task = dict(self.task, task_id=task_id)
        return self.prepare(self.issue(task), task)

    def completed(self, task_id='done'):
        row = self.claim_row(self.fresh(task_id))
        registry.observe(row['dispatch_id'], self.snapshot(row, 'running'), now=self.now)
        registry.observe(row['dispatch_id'], self.snapshot(row, 'idle', attentionReason='finished'), now=self.now)
        self.assertEqual(registry.inspect(row['dispatch_id'])['state'], 'completed')
        return row

    def stored(self, dispatch_id, **changes):
        with registry.transaction() as connection:
            row = registry.get(connection, 'dispatches', dispatch_id)
            row.update(changes)
            registry.save(connection, row)

    def started(self, task_id):
        row = self.claim_row(self.fresh(task_id))
        registry.observe(row['dispatch_id'], self.snapshot(row, 'running'), now=self.now)
        return row

    def clears(self):
        """The four clears that follow a completion, recorded instead of run."""
        import account_state
        import launch_failures
        import model_latch
        import provider_retry
        called = []
        for owner, name in ((provider_retry, 'clear'), (launch_failures, 'clear'), (account_state, 'clear_latch'),
                            (model_latch, 'clear')):
            label = f'{owner.__name__}.{name}'
            self.enterContext(patch.object(owner, name, side_effect=lambda *a, _label=label, **k: called.append(_label)))
        return called

    def test_a_collection_that_raises_on_observe_still_completes_records_and_clears(self):
        import dispatch_ground_truth as gt
        for raised, recorded in ((TypeError('host free text'), True), (PermissionError(13, 'locked free text'), False),
                                 (None, True)):
            with self.subTest(raised=type(raised).__name__):
                row = self.started(f'observe-{type(raised).__name__}')
                called = self.clears()
                at = (registry.parse(registry.inspect(row['dispatch_id'])['launched_at']) + timedelta(minutes=1)).isoformat()

                def host(request):
                    return {'ok': True, 'agent': self.snapshot(row, 'idle', attentionReason='finished'),
                            'completion_notification': {'terminal': {'reason': 'finished', 'replied': True, 'at': at}}}
                collect = patch.object(gt, 'collect_ground_truth', side_effect=raised) if raised else patch.object(
                    gt, 'collect_ground_truth', wraps=gt.collect_ground_truth)
                with collect:
                    answer = self.run_action(action('start', row['dispatch_id']), host)
                self.assertEqual([o['state'] for o in answer['observations']], ['completed'])
                self.assertNotIn('free text', json.dumps(answer))
                self.assertEqual(sorted(called), ['account_state.clear_latch', 'launch_failures.clear',
                                                  'model_latch.clear', 'provider_retry.clear'])
                stored = registry.inspect(row['dispatch_id'])
                self.assertEqual(stored['state'], 'completed')
                if raised is None:
                    self.assertNotIn('ground_truth_reason', stored)
                    self.assertIn('ground_truth_recorded_at', stored)
                elif recorded:
                    self.assertEqual(stored['ground_truth_reason'], 'collection_failed:TypeError')
                    self.assertNotIn('ground_truth_pending', stored)
                else:   # an OSError: pending, retried once by a later read
                    self.assertEqual((stored['ground_truth_pending'], stored['ground_truth_retry']['error']),
                                     (True, 'PermissionError'))
                    self.assertNotIn('ground_truth_recorded_at', stored)

    def test_a_collection_that_raises_on_an_external_runner_still_completes(self):
        import dispatch_ground_truth as gt
        for raised in (TypeError('free text'), PermissionError(13, 'locked')):
            with self.subTest(raised=type(raised).__name__):
                external = f'ext-{type(raised).__name__}'
                row = registry.record_external_launch('grok-bot', external, f'decision-{external}', dict(self.bundle),
                                                      parent_id='parent-a', task_id=external, cwd=str(self.root))
                with patch.object(gt, 'collect_ground_truth', side_effect=raised) as collect:
                    done = registry.complete_external('grok-bot', external)
                    again = registry.complete_external('grok-bot', external)   # the retry is a later read's, not this
                self.assertEqual(collect.call_count, 1)
                self.assertEqual((done['state'], again['state']), ('completed', 'completed'))
                if isinstance(raised, OSError):
                    self.assertEqual(done['ground_truth_retry']['error'], 'PermissionError')
                else:
                    self.assertEqual(done['ground_truth_reason'], 'collection_failed:TypeError')
                self.assertEqual(registry.inspect(row['dispatch_id'])['state'], 'completed')

    def test_nothing_open_names_what_reconcile_looked_at_and_the_open_claims_it_does_not_observe(self):
        task = dict(self.task, task_id='herdr-claim')
        claim = registry.record_host_claim('herdr', {'decision_id': 'd-herdr'}, task, dict(self.bundle),
                                           cwd=str(self.root), launched=True, now=registry.utcnow())
        answer = self.run_action(action('reconcile'))
        self.assertEqual((answer['observations'], answer['not_observed']), ([], []))
        self.assertEqual(answer['note'], flow.NOTHING_OPEN + ' ' + flow.OPEN_ELSEWHERE.format(
            claims=f"{claim['dispatch_id']} (herdr)"))

    def test_reconcile_and_start_name_each_dispatch_they_did_not_observe_and_why(self):
        never = self.prepare()
        done = self.completed()
        other = self.fresh('other-mode')
        self.stored(other['dispatch_id'], dispatch_mode='cli')
        for name in ('reconcile', 'start'):
            for row, state, why in ((never, 'prepared', 'prepared, never launched'),
                                    (done, 'completed', 'already completed'),
                                    (other, 'prepared', 'not a Paseo dispatch (cli)')):
                with self.subTest(action=name, why=why):
                    before = registry.inspect(row['dispatch_id'])
                    answer = self.run_action(action(name, row['dispatch_id']))
                    self.assertEqual(answer['observations'], [])
                    self.assertEqual(answer['not_observed'], [{'dispatch_id': row['dispatch_id'], 'state': state, 'why': why}])
                    self.assertNotIn('note', answer)
                    self.assertEqual(registry.inspect(row['dispatch_id']), before)   # nothing moved

    def test_reconcile_with_nothing_open_says_nothing_is_open(self):
        self.completed()   # an ended dispatch is not open: registry.pending does not list it
        answer = self.run_action(action('reconcile'))
        self.assertEqual((answer['observations'], answer['not_observed']), ([], []))
        self.assertEqual(answer['note'], flow.NOTHING_OPEN)
        never = self.fresh('held')   # a prepared one holds its reservation, so it is open and named
        answer = self.run_action(action('reconcile'))
        self.assertEqual(answer['not_observed'], [{'dispatch_id': never['dispatch_id'], 'state': 'prepared',
                                                   'why': 'prepared, never launched'}])
        self.assertNotIn('note', answer)

    def test_an_observation_that_was_not_applied_leaves_its_error_class(self):
        row = self.claimed()
        registry.observe(row['dispatch_id'], self.snapshot(row, 'running'), now=self.now)
        self.stored(row['dispatch_id'], state='failed')

        def host(request):
            return {'ok': True, 'agent': self.snapshot(row, 'error'), 'completion_notification': None}
        for raised in (registry.NotBound('Paseo host text that must not leave'), ValueError('free text')):
            with self.subTest(raised=type(raised).__name__), \
                    patch.object(registry, 'observe', side_effect=raised):
                answer = self.run_action(action('reconcile', row['dispatch_id']), host)
                self.assertEqual(answer['observations'], [])
                self.assertEqual(answer['not_observed'], [{
                    'dispatch_id': row['dispatch_id'], 'state': 'failed', 'why': 'observation not applied',
                    'error': type(raised).__name__}])
                self.assertNotIn('must not leave', json.dumps(answer)); self.assertNotIn('free text', json.dumps(answer))

        def down(request):
            return {'ok': False, 'error': 'host_connection_failed'}
        answer = self.run_action(action('reconcile', row['dispatch_id']), down)
        self.assertEqual(answer['not_observed'], [{'dispatch_id': row['dispatch_id'], 'state': 'failed',
                                                   'why': 'could not be observed', 'error': 'host_connection_failed'}])

    def test_launch_reads_the_same_observations_as_before(self):
        # launch's `queried_host = bool(observations)` must not count a dispatch that was not observed.
        done = self.completed()
        self.assertEqual(flow.reconcile(done['dispatch_id'], host=lambda _: self.fail('no host call')), [])

    def test_an_end_notice_that_failed_leaves_its_error_class(self):
        done = self.completed()
        with patch.object(flow, 'deliver_end_notices', side_effect=RuntimeError('host free text')):
            for args in (action('reconcile'), action('start', done['dispatch_id']), action('settle', done['dispatch_id'])):
                with self.subTest(action=args.dispatch_action):
                    answer = self.run_action(args)
                    self.assertEqual(answer['end_notices_error'], 'RuntimeError')
                    self.assertNotIn('free text', json.dumps(answer))
        self.assertNotIn('end_notices_error', self.run_action(action('reconcile')))

    def test_release_of_a_dispatch_that_is_already_over_says_nothing_was_released(self):
        done = self.completed()
        answer = self.run_action(action('release', done['dispatch_id']))
        self.assertEqual((answer['state'], answer['released']), ('completed', False))
        self.assertEqual(answer['note'], 'Dispatch is already completed; nothing was released.')
        never = self.fresh('released')
        answer = self.run_action(action('release', never['dispatch_id']))
        self.assertEqual((answer['state'], answer['released']), ('canceled', True))
        self.assertNotIn('note', answer)

    def test_settle_without_an_ending_receipt_says_so(self):
        never = self.prepare()
        answer = self.run_action(action('settle', never['dispatch_id']))
        self.assertEqual(answer['state'], 'prepared')
        self.assertEqual(answer['note'], flow.SETTLE_NO_RECEIPT['missing'])
        receipt = flow.notification_file(never['dispatch_id'])
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps({'status': 'watching'}), encoding='utf-8')
        self.assertEqual(self.run_action(action('settle', never['dispatch_id']))['note'], flow.SETTLE_NO_RECEIPT['no_ending'])
        receipt.write_text('{not json', encoding='utf-8')
        self.assertEqual(self.run_action(action('settle', never['dispatch_id']))['note'], flow.SETTLE_NO_RECEIPT['unreadable'])
        receipt.write_text('[]', encoding='utf-8')   # read, and not a receipt
        self.assertEqual(self.run_action(action('settle', never['dispatch_id']))['note'], flow.SETTLE_NO_RECEIPT['not_a_receipt'])
        self.assertEqual(registry.inspect(never['dispatch_id'])['state'], 'prepared')
        row = self.started('ending-that-does-not-count')
        launched = registry.parse(registry.inspect(row['dispatch_id'])['launched_at'])
        receipt = flow.notification_file(row['dispatch_id'])
        for terminal in ({'reason': 'finished', 'at': (launched - timedelta(hours=1)).isoformat()},
                         {'reason': 'a reason the registry does not know'}):
            with self.subTest(terminal=terminal):
                receipt.write_text(json.dumps({'status': 'finished', 'terminal': terminal}), encoding='utf-8')
                answer = self.run_action(action('settle', row['dispatch_id']))
                self.assertEqual((answer['state'], answer['note']), ('started', flow.SETTLE_NO_RECEIPT['ending_not_counted']))

    def test_fallback_unsettled_and_disabled_say_what_they_mean(self):
        never = self.prepare()
        answer = self.run_action(action('fallback', never['dispatch_id']))
        self.assertEqual((answer['action'], answer['state']), ('unsettled', 'prepared'))
        self.assertEqual(answer['meaning'], dispatch_fallback.UNSETTLED_MEANING.format(state='prepared'))
        with patch.dict(os.environ, {'AGENTCOMPASS_ROLL_FALLBACK': 'off'}):
            answer = self.run_action(action('fallback', never['dispatch_id']))
        self.assertEqual(answer, {'action': 'disabled', 'meaning': dispatch_fallback.DISABLED_MEANING})
        self.assertIn('AGENTCOMPASS_ROLL_FALLBACK=off', answer['meaning'])


if __name__ == '__main__':
    unittest.main()
