"""The dispatch cadence count (accounts.json `cadence`: at most `limit` dispatches on the account per `window_seconds`).

Rules, each from that statement: a dispatch counts from the moment it was launched until the window has passed over that
moment, whoever chose the route and whether or not it has ended; one dispatch counts once whatever rows it has; a plan is
not a dispatch; the window state and the ledger's alert read the same count."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import account_fleet
import decision_ledger
import dispatch_registry as registry
import quota_pools

NOW = datetime(2026, 11, 21, 12, tzinfo=timezone.utc)
WINDOW = 43200
LIMIT = 5
SPEC = {'cadence': {'window_seconds': WINDOW, 'limit': LIMIT}}
ACCOUNT = 'web-seat'
BUNDLE = {'provider': 'chatgpt-web', 'account': ACCOUNT, 'model': 'gpt-6-pro', 'bundle_id': 'pro_max'}


def launch(i, *, age_h=1.0, decision=None, account=ACCOUNT):
    """A dispatch the registry knows was launched, as dispatch_registry.launch_record gives it."""
    return {'stage': 'launch', 'dispatch_id': f'dispatch-{i}', 'decision_id': decision or f'decision-{i}',
            'bundle': dict(BUNDLE, account=account), 'at': (NOW - timedelta(hours=age_h)).isoformat()}


def ledger_row(stage, i, *, age_h=1.0, decision=None):
    return {'stage': stage, 'choice': 'pro_max', 'bundle': dict(BUNDLE), 'decision_id': decision or f'decision-{i}',
            'at': (NOW - timedelta(hours=age_h)).isoformat()}


def used(records, spec=SPEC, now=NOW):
    """Dispatches the window state counts, from the declared formula remaining = 100 * (limit - used) / limit."""
    _, state, cap = quota_pools.cadence_window_state(ACCOUNT, spec, records, now)
    return round(cap * (100.0 - state['remaining']) / 100.0, 9)


class CadenceCountRules(unittest.TestCase):

    def test_the_count_is_non_decreasing_in_launched_dispatches_inside_the_window(self):
        counts = [used([launch(i, age_h=1 + i) for i in range(n)]) for n in range(LIMIT + 2)]
        self.assertTrue(all(b >= a for a, b in zip(counts, counts[1:])), counts)
        self.assertEqual(counts, [min(n, LIMIT) for n in range(LIMIT + 2)])  # remaining is clamped at 0 of limit

    def test_a_dispatch_older_than_the_window_does_not_count(self):
        hours = WINDOW / 3600
        self.assertEqual(used([launch(0, age_h=hours - 0.01)]), 1)
        self.assertEqual(used([launch(0, age_h=hours)]), 0)
        self.assertEqual(used([launch(0, age_h=hours + 0.01)]), 0)
        self.assertEqual(used([ledger_row('record', 0, age_h=hours + 0.01)]), 0)

    def test_one_dispatch_counts_once_whatever_rows_it_has(self):
        # The plan or record that authorized it, its registry row, and a live reservation row of the same dispatch.
        rows = [ledger_row('plan', 0), ledger_row('record', 0), launch(0),
                {'stage': 'dispatch', 'dispatch_id': 'dispatch-0', 'state': 'launched', 'bundle': dict(BUNDLE),
                 'at': (NOW - timedelta(hours=1)).isoformat(),
                 'lease_until': (NOW + timedelta(minutes=5)).isoformat()}]
        self.assertEqual(used(rows), 1)
        self.assertEqual(used([launch(0), launch(0)]), 1)

    def test_a_plan_that_was_not_launched_does_not_count(self):
        self.assertEqual(used([ledger_row('plan', i) for i in range(LIMIT)]), 0)
        # A record whose dispatch the registry holds but never launched (refused create, canceled before launch).
        never = dict(launch(0), at=None)
        self.assertEqual(used([ledger_row('record', 0), never]), 0)

    def test_a_named_route_with_no_dispatch_row_counts_from_its_record(self):
        # `record` followed by a launch outside roll: the record is the only row of that dispatch.
        self.assertEqual(used([ledger_row('record', i) for i in range(3)]), 3)
        self.assertEqual(used([dict(ledger_row('record', 0), choice='none')]), 0)

    def test_each_turn_sent_to_one_worker_counts(self):
        # A --resume turn is a new prompt to the vendor: its own registry row, its own count.
        turns = [dict(launch(i, age_h=1 + i), agent_id='worker-a') for i in range(3)]
        self.assertEqual(used(turns), 3)

    def test_other_accounts_and_accounts_without_a_cadence(self):
        self.assertEqual(used([launch(0, account='other')]), 0)
        self.assertIsNone(quota_pools.cadence_window_state(ACCOUNT, {}, [launch(0)], NOW))
        self.assertIsNone(decision_ledger.account_cadence_alert([launch(0)], NOW, ACCOUNT, {}))

    def test_the_alert_and_the_window_state_read_one_count(self):
        for launched in range(LIMIT + 2):
            for named in range(LIMIT + 2):
                rows = ([launch(i, age_h=1 + i * 0.5) for i in range(launched)]
                        + [ledger_row('record', 100 + i, age_h=1 + i * 0.5) for i in range(named)]
                        + [ledger_row('plan', 200 + i) for i in range(3)])
                with self.subTest(launched=launched, named=named):
                    _, state, _ = quota_pools.cadence_window_state(ACCOUNT, SPEC, rows, NOW)
                    alert = decision_ledger.account_cadence_alert(rows, NOW, ACCOUNT, SPEC)
                    self.assertEqual(alert is not None, state['remaining'] <= 0.0)


class RollingWindowState(unittest.TestCase):
    """A cadence is a rolling limit: the seat a dispatch takes comes back one window after its launch, whatever the
    others did, and a free seat never expires."""

    def state(self, ages_h):
        return quota_pools.cadence_window_state(ACCOUNT, SPEC, [launch(i, age_h=a) for i, a in enumerate(ages_h)], NOW)

    def test_while_a_seat_is_free_nothing_of_the_allowance_expires(self):
        period = WINDOW / 3600
        for ages in ([], [1.0], [11.9], [11.9, 0.1], [11.99, 11.0, 6.0, 0.5]):
            _, state, _ = self.state(ages)
            with self.subTest(ages=ages):
                self.assertEqual((state['reset_hours'], state['end_hours'], state['target_remaining']),
                                 (period, period, 100.0))

    def test_with_no_seat_free_the_wait_is_until_enough_dispatches_leave(self):
        ages = [11.5, 9.5, 7.5, 5.5, 3.5]
        _, state, _ = self.state(ages)
        self.assertEqual(state['remaining'], 0.0)
        self.assertAlmostEqual(state['reset_hours'], 0.5)
        # One over the limit (a named route may pass it): two must leave before one seat is free.
        _, over, _ = self.state([11.8, *ages])
        self.assertAlmostEqual(over['reset_hours'], 0.5)
        _, over, _ = self.state([11.8, 11.6, *ages])
        self.assertAlmostEqual(over['reset_hours'], 0.5)
        _, over, _ = self.state([11.5, 11.5, 9.5, 7.5, 5.5, 3.5])
        self.assertAlmostEqual(over['reset_hours'], 0.5)

    def test_what_comes_back_is_the_limit_less_the_dispatches_still_inside(self):
        for ages, back in (([11.5, 9.5, 7.5, 5.5, 3.5], 1), ([11.5, 11.5, 9.5, 7.5, 5.5], 2),
                           ([11.8, 11.5, 9.5, 7.5, 5.5, 3.5], 1)):
            wspec, state, cap = self.state(ages)
            renewed = quota_pools.after_reset(state, wspec['hours'])
            with self.subTest(ages=ages):
                self.assertAlmostEqual(renewed['remaining'], 100.0 * back / cap)
                self.assertEqual(renewed['deferred_hours'], state['reset_hours'])


class RegistryLaunchRecords(unittest.TestCase):
    """What the registry gives the count: every stored dispatch of the window, ended or not, with its launch time."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='cadence-')
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'dispatch.sqlite3'
        self.accounts = {'accounts': {ACCOUNT: dict(SPEC)}}

    def claim(self, i, *, launched, at=NOW):
        task = {'parent_agent_id': 'parent-a', 'task_id': f'task-{i}'}
        row = registry.record_host_claim('cli', {'decision_id': f'decision-{i}'}, task, BUNDLE,
                                         now=at - timedelta(hours=1), path=self.path)
        if launched:
            row = registry.host_claim_launched(row['dispatch_id'], now=at - timedelta(hours=1), path=self.path)
        return row

    def counted(self):
        return used(registry.launch_records(self.accounts, NOW, path=self.path))

    def test_an_ended_dispatch_counts_as_a_live_one_does(self):
        live = self.claim(0, launched=True, at=NOW + timedelta(hours=1) - timedelta(minutes=1))
        self.assertEqual(self.counted(), 1)
        registry.settle_host_claim(live['dispatch_id'], 'completed', now=NOW, path=self.path)
        self.assertEqual(self.counted(), 1)

    def test_a_dispatch_that_never_launched_does_not_count(self):
        row = self.claim(0, launched=False)
        registry.settle_host_claim(row['dispatch_id'], 'canceled', now=NOW, path=self.path)
        self.assertEqual(self.counted(), 0)
        refused = self.claim(1, launched=True)
        with registry.transaction(self.path) as connection:
            stored = registry.get(connection, 'dispatches', refused['dispatch_id'])
            registry.event(stored, 'create_failed', NOW)
            registry.save(connection, stored)
        self.assertEqual(self.counted(), 0)

    def test_a_dispatch_older_than_the_window_is_not_read(self):
        self.claim(0, launched=True, at=NOW - timedelta(seconds=WINDOW) - timedelta(seconds=1))
        self.assertEqual(registry.launch_records(self.accounts, NOW, path=self.path), [])
        self.claim(1, launched=True, at=NOW - timedelta(seconds=WINDOW) + timedelta(hours=1, seconds=1))
        self.assertEqual([r['decision_id'] for r in registry.launch_records(self.accounts, NOW, path=self.path)],
                         ['decision-1'])

    def test_a_row_with_no_launch_or_creation_time_is_no_launch_in_any_window(self):
        # A hand-made or damaged row: no `launched_at`, no `created_at`, no bundle. It is read as no launch (no default
        # time), and the good rows beside it still count.
        self.claim(0, launched=True)
        self.claim(1, launched=True)
        with registry.transaction(self.path) as connection:
            stored = registry.rows(connection, 'dispatches')
        timeless = {'dispatch_id': 'd-old', 'decision_id': 'dec-old', 'state': 'launched', 'dispatch_mode': 'cli',
                    'lease_until': '2026-11-18T00:00:00Z', 'events': [], 'parent_id': 'p'}
        read = registry.launches([*stored, timeless, {'dispatch_id': 'd-bare', 'state': 'prepared'}],
                                 self.accounts, NOW)
        self.assertEqual({r['dispatch_id']: r['at'] for r in read if r['dispatch_id'] in ('d-old', 'd-bare')},
                         {'d-old': None, 'd-bare': None})
        self.assertEqual(used(read), 2)

    def test_no_cadence_reads_nothing(self):
        self.claim(0, launched=True)
        self.assertEqual(registry.launch_records({'accounts': {ACCOUNT: {}}}, NOW, path=self.path), [])

    def test_the_alert_reads_the_registry(self):
        now = datetime.now(timezone.utc)    # finalize reads the clock
        for i in range(LIMIT):
            row = self.claim(i, launched=True, at=now)
            registry.settle_host_claim(row['dispatch_id'], 'completed', now=now - timedelta(minutes=30), path=self.path)
        result = {'stage': 'plan', 'choice': 'pro_max', 'selected_bundle': dict(BUNDLE), 'usage': {'accounts': {}}}
        with patch.object(decision_ledger, 'LEDGER_WRITES', False), \
                patch.object(decision_ledger, 'read_ledger', return_value=[]), \
                patch.object(decision_ledger, 'append_ledger'), \
                patch.object(registry, 'db_path', return_value=self.path), \
                patch.object(registry, 'utcnow', return_value=NOW), \
                patch.object(account_fleet, 'load_accounts', return_value=self.accounts):
            out = decision_ledger.finalize(result, time.monotonic(), {'objective': 'Advise'})
        self.assertIn('advisor_cadence', {a['code'] for a in out['alerts']})


if __name__ == '__main__':
    unittest.main()
