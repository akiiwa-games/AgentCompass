"""The continuation verdict follows a new turn's admission: a worker under the context threshold is "same worker" only
when its account and model could take a turn now (dispatch-rules.md section 3), it never writes or waits on a writer, and
it does not depend on the hold a failure records later. Synthetic accounts, usage, holds and registry rows."""
import copy
from datetime import datetime, timedelta, timezone
import sqlite3
import time
import unittest
from unittest.mock import patch

import account_fleet
import dispatch_registry as registry
import host_policy
import model_latch
import provider_retry
import quota_exhaustion
import route_judge
import turn_refusal
import worker_sessions

from router_test_support import isolated_router_state

NOW = datetime.now(timezone.utc)
THRESHOLD = 2_000_000
BUNDLE = {'account': 'acme', 'provider': 'acme', 'model': 'acme-quartz-2', 'effort': 'high', 'bundle_id': 'quartz_high'}
FLEET = {'accounts': {'acme': {'windows': {'seven_day': {'hours': 168}}, 'bundles': {
    'quartz_high': {'provider': 'acme', 'model': 'acme-quartz-2', 'effort': 'high'},
    'onyx_high': {'provider': 'acme', 'model': 'acme-onyx-2', 'effort': 'high'}}}},
    'scoring': {'rotate_after_carried_tokens': THRESHOLD},
    '_discovery': {'accounts': {'acme': {'status': 'ready'}}, 'launchers': {}}}


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup)


def usage(left):
    """A fresh reading of the account's one window with `left` percent remaining, resetting in 40 hours."""
    window = route_judge.window_state({'id': 'seven_day', 'used_pct': 100 - left,
                                       'resets_at': (NOW + timedelta(hours=40)).isoformat()}, NOW)
    return {'accounts': {'acme': {'status': 'fresh', 'fetched_at': NOW.isoformat(), 'available': True,
                                  'windows': [window]}}}


def dispatch_row(dispatch_id='d1', agent_id='worker', **extra):
    now = NOW.isoformat()
    return {'dispatch_id': dispatch_id, 'decision_id': 'dec-' + dispatch_id, 'agent_id': agent_id, 'state': 'started',
            'created_at': now, 'lease_until': (NOW + timedelta(hours=1)).isoformat(), 'expected_usd': 0.0, 'usage': {},
            'bundle': dict(BUNDLE), **extra}


class VerdictTestCase(unittest.TestCase):
    def setUp(self):
        registry.db_path().unlink(missing_ok=True)
        self.fleet = copy.deepcopy(FLEET)
        self.left, self.holds, self.latches, self.policy, self.receipt = 80, {}, {}, None, None
        for new in (patch.object(account_fleet, 'load_accounts', side_effect=lambda: self.fleet),
                    patch.object(route_judge, 'usage_snapshot', side_effect=lambda *a, **k: usage(self.left)),
                    patch.object(host_policy, 'load_policy', side_effect=lambda: self.policy),
                    patch.object(provider_retry, 'active', side_effect=lambda **k: self.holds),
                    patch.object(model_latch, 'active', side_effect=lambda **k: self.latches),
                    patch.object(registry, 'watcher_receipt', side_effect=lambda dispatch_id: self.receipt),
                    patch.object(registry, 'paseo_agent_record', return_value=None)):
            new.start()
            self.addCleanup(new.stop)
        self.store(dispatch_row())

    def store(self, *rows):
        with registry.transaction() as connection:
            for row in rows:
                registry.save(connection, row)

    def verdict(self, carried=500_000):
        with patch.object(worker_sessions, 'session_state', return_value={'compacted_tokens': 0, 'context_tokens': carried}):
            return worker_sessions.continuation_verdict('worker').splitlines()[0]

    def assertRoll(self, verdict, *words):
        self.assertTrue(verdict.startswith('Continuation: roll'), verdict)
        for word in words:
            self.assertIn(word, verdict)

    def assertSame(self, verdict):
        self.assertTrue(verdict.startswith('Continuation: same worker'), verdict)


class ContinuationVerdictTests(VerdictTestCase):
    def test_under_the_threshold_with_a_ready_account_is_the_same_worker(self):
        self.assertTrue(self.verdict().startswith('Continuation: same worker (carried 500,000 of 2,000,000'))

    def test_an_exhausted_window_names_the_window_and_rolls(self):
        self.left = 0
        self.assertRoll(self.verdict(), 'seven_day', 'acme')

    def test_a_provider_hold_on_the_account_rolls(self):
        self.holds = {provider_retry.key('acme'): {
            'account': 'acme', 'scope': 'account', 'failure_code': provider_retry.TURN_FAILED_CODE,
            'message': 'Individual quota reached', 'retry_at': None}}
        self.assertRoll(self.verdict(), 'held after a provider failure', 'Individual quota reached')

    def test_a_hold_on_a_quota_pool_of_the_account_refuses_only_the_bundles_drawing_it(self):
        pool = {'id': '3p-5h+3p-weekly', 'models': ['acme-quartz-2']}
        self.holds = {provider_retry.pool_key('acme', pool['id']): {
            'account': 'acme', 'model': None, 'scope': 'account', 'pool': pool,
            'failure_code': provider_retry.TURN_FAILED_CODE, 'message': 'Individual quota reached. Resets in 122h48m2s.'}}
        self.assertRoll(self.verdict(), 'quota pool 3p-5h+3p-weekly', 'Individual quota reached')
        self.store(dispatch_row('d2', 'worker', created_at=(NOW + timedelta(seconds=5)).isoformat(),
                                bundle=dict(BUNDLE, model='acme-onyx-2', bundle_id='onyx_high')))
        self.assertSame(self.verdict())

    def test_a_hold_on_the_model_rolls(self):
        self.latches = {model_latch.key('acme', 'acme-quartz-2'): {
            'account': 'acme', 'model': 'acme-quartz-2', 'code': 'model_not_found', 'observed_at': NOW.isoformat()}}
        self.assertRoll(self.verdict(), 'acme-quartz-2', 'is held')

    def test_a_hold_on_another_model_does_not_refuse_this_worker(self):
        self.latches = {model_latch.key('acme', 'acme-onyx-2'): {
            'account': 'acme', 'model': 'acme-onyx-2', 'code': 'model_not_found', 'observed_at': NOW.isoformat()}}
        self.assertSame(self.verdict())

    def test_an_account_that_is_not_ready_rolls(self):
        self.fleet['_discovery']['accounts']['acme'] = {'status': 'auth_failed', 'reason': 'credentials_rejected'}
        self.assertRoll(self.verdict(), 'not ready', 'credentials_rejected')

    def test_a_disabled_account_or_unchecked_bundle_rolls_and_leaves_re_enabling_to_the_user(self):
        for name, change in (
                ('host_policy', lambda: setattr(self, 'policy', {'allowed_accounts': ['other']})),
                ('unchecked', lambda: self.fleet['accounts']['acme']['bundles']['quartz_high'].update(disabled=True)),
                ('legacy', lambda: self.fleet['accounts']['acme'].update(legacy_meter='retired meter'))):
            with self.subTest(name=name):
                self.setUp()
                change()
                self.assertRoll(self.verdict(), 'roll the follow-up', "user's choice", 'AgentCompass settings')

    def test_one_unchecked_bundle_does_not_switch_off_its_siblings(self):
        self.fleet['accounts']['acme']['bundles']['onyx_high']['disabled'] = True
        self.assertSame(self.verdict())

    def test_over_the_threshold_stays_a_rotate_verdict_whatever_the_account_state(self):
        for state in ('ready', 'exhausted', 'held', 'not_ready'):
            with self.subTest(state=state):
                self.left = 0 if state == 'exhausted' else 80
                self.holds = ({provider_retry.key('acme'): {'account': 'acme', 'scope': 'account', 'retry_at': None}}
                              if state == 'held' else {})
                self.fleet['_discovery']['accounts']['acme'] = (
                    {'status': 'auth_failed', 'reason': 'x'} if state == 'not_ready' else {'status': 'ready'})
                with patch.object(turn_refusal, 'worker_refusal', side_effect=AssertionError('not asked')):
                    self.assertTrue(self.verdict(THRESHOLD).startswith('Continuation: rotate'), state)

    def test_an_unreadable_account_check_is_unknown_never_same_worker(self):
        with patch.object(turn_refusal, 'worker_refusal', side_effect=OSError('state locked')):
            verdict = self.verdict()
        self.assertTrue(verdict.startswith('Continuation: unknown (turn_check_unavailable'), verdict)


class OwnFailureTests(VerdictTestCase):
    """The failed turn the notification reports is read from the receipt (or the settled row), never from the hold that
    a later settlement records: the first verdict after the failure already says roll."""

    FAILED = {'terminal': {'reason': 'error', 'failure_code': provider_retry.TURN_FAILED_CODE,
                           'provider_message': 'Individual quota reached. Resets in 122h48m2s.'}}

    def test_a_provider_failure_in_the_receipt_rolls_before_any_hold_exists(self):
        self.receipt, self.holds = self.FAILED, {}
        self.assertRoll(self.verdict(), 'last turn ended in a provider failure', 'Individual quota reached')

    def test_the_same_failure_settled_on_the_row_rolls(self):
        self.store(dispatch_row(state='failed', failure_code=provider_retry.TURN_FAILED_CODE))
        self.assertRoll(self.verdict(), 'last turn ended in a provider failure')

    def test_a_failure_the_classifier_read_as_the_workers_own_does_not_refuse(self):
        self.store(dispatch_row(state='failed', failure_code=provider_retry.WORKER_TURN_FAILED_CODE))
        self.receipt = self.FAILED
        self.assertSame(self.verdict())

    def test_a_turn_that_ended_without_a_provider_failure_does_not_refuse(self):
        self.receipt = {'terminal': {'reason': 'finished'}}
        self.assertSame(self.verdict())

    def test_the_verdict_never_records_the_hold_itself(self):
        self.receipt = self.FAILED
        with patch.object(provider_retry, 'record', side_effect=AssertionError('verdict wrote a hold')):
            self.assertRoll(self.verdict())


class UnresolvedWorkerTests(VerdictTestCase):
    def test_a_worker_with_no_dispatch_row_is_unknown_never_same_worker(self):
        registry.db_path().unlink()
        verdict = self.verdict()
        self.assertTrue(verdict.startswith('Continuation: unknown (turn_check_unavailable: worker_not_resolvable'), verdict)

    def test_a_row_of_another_worker_does_not_resolve_this_one(self):
        registry.db_path().unlink()
        self.store(dispatch_row('d9', 'someone-else'))
        self.assertIn('worker_not_resolvable', self.verdict())

    def test_the_dispatch_label_resolves_a_worker_whose_row_has_no_agent_id_yet(self):
        registry.db_path().unlink()
        self.store(dispatch_row('d1', None))
        with patch.object(registry, 'paseo_agent_record', return_value={'labels': {'router.dispatch_id': 'd1'}}):
            self.assertSame(self.verdict())

    def test_the_row_of_the_current_attempt_names_the_account(self):
        later = (NOW + timedelta(seconds=5)).isoformat()
        self.store(dispatch_row('d2', 'worker', created_at=later, bundle=dict(BUNDLE, account='other', provider='other', bundle_id='x')))
        self.fleet['accounts']['other'] = {'bundles': {}}
        self.fleet['_discovery']['accounts']['other'] = {'status': 'auth_failed', 'reason': 'x'}
        self.assertRoll(self.verdict(), 'account other is not ready')


class OwnReservationTests(unittest.TestCase):
    """One function drops the worker's own reservation for the gate and the verdict: one naming its agent id, or naming
    no agent id and carrying its dispatch label. One naming another agent id is never its own."""

    def kept(self, records, dispatch_id=None, agent_id=None):
        return [r['dispatch_id'] for r in turn_refusal.without_own(records, dispatch_id, agent_id) if isinstance(r, dict)]

    def test_own_is_its_agent_id_or_an_unbound_reservation_with_its_dispatch_label(self):
        records = [{'dispatch_id': 'mine', 'agent_id': None}, {'dispatch_id': 'bound', 'agent_id': 'worker'},
                   {'dispatch_id': 'other', 'agent_id': 'someone'}]
        self.assertEqual(self.kept(records, 'mine', 'worker'), ['other'])
        self.assertEqual(self.kept(records, 'mine'), ['bound', 'other'])
        self.assertEqual(self.kept(records, None, 'worker'), ['mine', 'other'])
        self.assertEqual(self.kept(records), ['mine', 'bound', 'other'])
        self.assertEqual(turn_refusal.without_own(['not-a-dict']), ['not-a-dict'])

    def test_a_reservation_naming_another_agent_is_never_excluded_by_the_dispatch_label(self):
        # A worker labelled with another dispatch's id, and a second worker carrying the same dispatch id.
        records = [{'dispatch_id': 'shared', 'agent_id': 'second-worker'}, {'dispatch_id': 'shared', 'agent_id': 'worker'}]
        self.assertEqual([r['agent_id'] for r in turn_refusal.without_own(records, 'shared', 'worker')], ['second-worker'])
        self.assertEqual([r['agent_id'] for r in turn_refusal.without_own(records, 'shared', None)],
                         ['second-worker', 'worker'])

    def test_the_verdict_leaves_out_the_row_of_its_own_worker_and_counts_the_others(self):
        class Quiet(VerdictTestCase):
            def runTest(self):
                pass
        case = Quiet()
        case.setUp()
        try:
            case.store(dispatch_row('d2', 'other-worker'))
            with patch.object(turn_refusal, 'window_refusal', return_value=None) as window:
                self.assertIsNone(turn_refusal.worker_refusal('worker', NOW))
            self.assertEqual([r['dispatch_id'] for r in window.call_args.args[4]], ['d2'])
        finally:
            case.doCleanups()


class ReadOnlyTests(VerdictTestCase):
    def test_the_verdict_writes_nothing_and_calls_no_writer_or_classifier(self):
        self.receipt = OwnFailureTests.FAILED
        before = registry.db_path().read_bytes()
        forbidden = {name: AssertionError(f'verdict called {name}') for name in ('reservations', 'inspect', 'transaction')}
        with patch.object(registry, 'reservations', side_effect=forbidden['reservations']), \
                patch.object(registry, 'inspect', side_effect=forbidden['inspect']), \
                patch.object(registry, 'transaction', side_effect=forbidden['transaction']), \
                patch.object(provider_retry, 'record', side_effect=AssertionError('record')), \
                patch.object(provider_retry, 'classify_scope', side_effect=AssertionError('classifier')):
            self.assertRoll(self.verdict())
            self.receipt = None
            self.assertSame(self.verdict())
        self.assertEqual(registry.db_path().read_bytes(), before)

    def test_a_writer_holding_the_registry_lock_does_not_make_the_verdict_wait(self):
        writer = sqlite3.connect(registry.db_path(), isolation_level=None)
        self.addCleanup(writer.close)
        writer.execute('BEGIN IMMEDIATE')       # a writer mid-transaction: readers still read
        started = time.monotonic()
        self.assertSame(self.verdict())
        self.assertLess(time.monotonic() - started, 3)
        writer.execute('ROLLBACK')

    def test_a_registry_that_stays_locked_is_unknown_within_the_short_wait(self):
        writer = sqlite3.connect(registry.db_path(), isolation_level=None)
        self.addCleanup(writer.close)
        writer.execute('BEGIN EXCLUSIVE')
        started = time.monotonic()
        verdict = self.verdict()
        self.assertLess(time.monotonic() - started, 3)
        self.assertTrue(verdict.startswith('Continuation: unknown (turn_check_unavailable'), verdict)
        writer.execute('ROLLBACK')

    def test_reading_the_exhaustion_latch_for_the_verdict_does_not_clear_it(self):
        observed = (NOW - timedelta(hours=2)).isoformat()
        quota_exhaustion.record('acme', 'Individual quota reached', windows=['seven_day'], at=observed)
        path = quota_exhaustion.store_path()
        before = path.read_text(encoding='utf-8')
        recovered = {'available': True, 'fetched_at': NOW.isoformat(), 'windows': [
            {'id': 'seven_day', 'used_pct': 10, 'resets_at': (NOW + timedelta(hours=40)).isoformat()}]}
        self.assertEqual(quota_exhaustion.exhausted_windows('acme', recovered, now=NOW, read_only=True), set())
        self.assertEqual(path.read_text(encoding='utf-8'), before)
        self.assertEqual(quota_exhaustion.exhausted_windows('acme', recovered, now=NOW), set())     # the gate's read clears
        self.assertNotEqual(path.read_text(encoding='utf-8'), before)


if __name__ == '__main__':
    unittest.main()
