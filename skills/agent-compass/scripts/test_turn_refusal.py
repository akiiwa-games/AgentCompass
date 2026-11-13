"""The continuation verdict follows a new turn's admission: a worker under the context threshold is "same worker" only
when its account and model could take a turn now (dispatch-rules.md section 3). Synthetic accounts, usage and holds."""
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

import account_fleet
import dispatch_registry
import model_latch
import provider_retry
import route_judge
import turn_refusal
import worker_sessions

from router_test_support import isolated_router_state

NOW = datetime.now(timezone.utc)
THRESHOLD = 2_000_000
BUNDLE = {'account': 'acme', 'provider': 'acme', 'model': 'acme-quartz-2', 'effort': 'high', 'bundle_id': 'quartz_high'}
FLEET = {'accounts': {'acme': {'windows': {'seven_day': {'hours': 168}}, 'bundles': {
    'quartz_high': {'provider': 'acme', 'model': 'acme-quartz-2', 'effort': 'high'}}}},
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


class ContinuationVerdictTests(unittest.TestCase):
    def setUp(self):
        self.fleet = {**FLEET, '_discovery': {'accounts': {'acme': {'status': 'ready'}}, 'launchers': {}}}
        self.left, self.holds, self.latches = 80, {}, {}
        for target, new in (
                (account_fleet, patch.object(account_fleet, 'load_accounts', side_effect=lambda: self.fleet)),
                (turn_refusal, patch.object(turn_refusal, 'worker_bundle', return_value=('d1', dict(BUNDLE)))),
                (route_judge, patch.object(route_judge, 'usage_snapshot', side_effect=lambda *a, **k: usage(self.left))),
                (dispatch_registry, patch.object(dispatch_registry, 'reservations', return_value=[])),
                (provider_retry, patch.object(provider_retry, 'active', side_effect=lambda **k: self.holds)),
                (model_latch, patch.object(model_latch, 'active', side_effect=lambda **k: self.latches))):
            new.start()
            self.addCleanup(new.stop)

    def verdict(self, carried=500_000):
        with patch.object(worker_sessions, 'session_state', return_value={'compacted_tokens': 0, 'context_tokens': carried}):
            return worker_sessions.continuation_verdict('worker').splitlines()[0]

    def assertRoll(self, verdict, *words):
        self.assertTrue(verdict.startswith('Continuation: roll'), verdict)
        for word in words:
            self.assertIn(word, verdict)
        self.assertNotIn('same worker', verdict.split(':', 2)[1])

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

    def test_a_hold_on_the_model_rolls(self):
        self.latches = {model_latch.key('acme', 'acme-quartz-2'): {
            'account': 'acme', 'model': 'acme-quartz-2', 'code': 'model_not_found',
            'observed_at': NOW.isoformat()}}
        self.assertRoll(self.verdict(), 'acme-quartz-2', 'is held')

    def test_an_account_that_is_not_ready_rolls(self):
        self.fleet['_discovery']['accounts']['acme'] = {'status': 'auth_failed', 'reason': 'credentials_rejected'}
        self.assertRoll(self.verdict(), 'not ready', 'credentials_rejected')

    def test_a_hold_on_another_model_does_not_refuse_this_worker(self):
        self.latches = {model_latch.key('acme', 'acme-onyx-2'): {
            'account': 'acme', 'model': 'acme-onyx-2', 'code': 'model_not_found', 'observed_at': NOW.isoformat()}}
        self.assertTrue(self.verdict().startswith('Continuation: same worker'))

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

    def test_a_worker_this_router_did_not_launch_has_nothing_to_refuse(self):
        self.left = 0
        with patch.object(turn_refusal, 'worker_bundle', return_value=None):
            self.assertTrue(self.verdict().startswith('Continuation: same worker'))

    def test_an_unreadable_account_check_is_unknown_never_same_worker(self):
        with patch.object(turn_refusal, 'worker_refusal', side_effect=OSError('state locked')):
            verdict = self.verdict()
        self.assertTrue(verdict.startswith('Continuation: unknown (turn_check_unavailable'), verdict)

    def test_the_worker_own_reservation_never_counts_against_it(self):
        mine = {'dispatch_id': 'd1', 'agent_id': 'worker'}
        other = {'dispatch_id': 'd2', 'agent_id': 'other'}
        with patch.object(dispatch_registry, 'reservations', return_value=[mine, other]), \
                patch.object(turn_refusal, 'window_refusal', return_value=None) as window:
            self.assertIsNone(turn_refusal.worker_refusal('worker', NOW))
        self.assertEqual(window.call_args.args[4], [other])


if __name__ == '__main__':
    unittest.main()
