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

    def test_a_row_the_count_cannot_place_is_no_launch_and_raises_nothing(self):
        # The registry is derived state: a damaged row (a time that is not a time, a bundle that is not an object, no
        # dispatch id) is read as no launch, without a default and without an exception, beside real launches.
        import json
        import sqlite3
        self.claim(0, launched=True)
        self.claim(1, launched=True)
        stamp = registry.stamp(NOW - timedelta(hours=1))
        damaged = {
            'launched_at is a number': dict(dispatch_id='n1', decision_id='n1', state='completed', bundle=BUNDLE,
                                            launched_at=1763700000, created_at=stamp),
            'launched_at is not a time': dict(dispatch_id='n2', decision_id='n2', state='completed', bundle=BUNDLE,
                                              launched_at='yesterday', created_at=stamp),
            'created_at is not a time': dict(dispatch_id='n3', decision_id='n3', state='launched', bundle=BUNDLE,
                                             created_at='soon', lease_until=registry.stamp(NOW + timedelta(hours=1))),
            'lease_until is a number': dict(dispatch_id='n4', decision_id='n4', state='launched', bundle=BUNDLE,
                                            created_at=stamp, lease_until=5),
            'bundle is a string': dict(dispatch_id='n5', decision_id='n5', state='completed', bundle='pro_max',
                                       launched_at=stamp, created_at=stamp),
            'dispatch_id missing': dict(decision_id='n6', state='completed', bundle=BUNDLE, launched_at=stamp,
                                        created_at=stamp),
            'not an object': ['n7'],
        }
        with registry.transaction(self.path) as connection:
            stored = registry.rows(connection, 'dispatches')
        for label, row in damaged.items():
            with self.subTest(label=label):
                read = registry.launches([*stored, row], self.accounts, NOW)
                self.assertEqual(used(read), 2)
                self.assertTrue(all(r['at'] is None for r in read[len(stored):]), read[len(stored):])
        # Stored in the file itself, all at once: what every plan, record and roll reads.
        connection = sqlite3.connect(self.path)
        try:
            for i, row in enumerate(damaged.values()):
                key = row.get('dispatch_id', f'x{i}') if isinstance(row, dict) else f'x{i}'
                decision = row.get('decision_id', f'x{i}') if isinstance(row, dict) else f'x{i}'
                connection.execute('INSERT INTO dispatches(id,decision_id,data) VALUES (?,?,?)',
                                   (key, decision, json.dumps(row)))
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(self.counted(), 2)

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


class NamedRoutesReachPastTheCadence(unittest.TestCase):
    """Rule (router-internals.md, Pins): a cadence is a pace declared for automatic routing. Automatic routing is held
    to it; a route the user names reaches past it and launches, with the advisor_cadence sentence once in its warnings,
    on every surface: `record`, `roll` named to a ChatGPT Web consult, a named Paseo route through `prepare`, and a
    named `--resume`."""

    def setUp(self):
        import json
        import route_judge
        from router_test_support import isolated_router_state, profile
        self.json, self.router, self.profile = json, route_judge, profile
        self.root = isolated_router_state(self.addCleanup)
        self.now = datetime.now(timezone.utc)
        self.fleet = account_fleet.load_accounts()
        # A Paseo account held to the same cadence as the fixture's ChatGPT Web seat.
        self.fleet['accounts']['claude']['cadence'] = dict(SPEC['cadence'])
        from router_test_support import usage_windows
        self.usage = usage_windows(self.now, {'claude': [('five_hour', 90, 3), ('seven_day', 90, 100)]})
        patcher = patch.object(account_fleet, 'load_accounts', return_value=self.fleet)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fill(self, account, n=LIMIT, *, age_h=1.0):
        """`n` named launches outside roll on `account` (ledger record rows): the count reads each."""
        import decision_ledger as ledger
        bundle = {'account': account, 'provider': 'chatgpt-web' if account == 'chatgpt-web' else 'claude',
                  'model': 'gpt-6-pro' if account == 'chatgpt-web' else 'claude-opus-5-5'}
        with open(ledger.ledger_path(), 'a', encoding='utf-8', newline='\n') as handle:
            for i in range(n):
                handle.write(self.json.dumps({'v': 1, 'stage': 'record', 'choice': 'constraint', 'bundle': bundle,
                                              'decision_id': f'outside-{account}-{i}',
                                              'at': (self.now - timedelta(hours=age_h + i * 0.01)).isoformat()}) + '\n')

    def task(self, task_id):
        return {'objective': 'Advise on the fixture plan', 'task_id': task_id, 'parent_agent_id': 'parent-a',
                'work_kind': 'architecture_reasoning', 'expected_hours': 0.5,
                'profile': self.profile(work_kind='architecture_reasoning', expected_hours=0.5)}

    def args(self, account, **flags):
        import argparse
        web = account == 'chatgpt-web'
        values = dict(basis='User: send this to Pro', provider='chatgpt-web' if web else 'claude', account=account,
                      model='gpt-6-pro' if web else 'claude-opus-5-5', effort='max' if web else 'high',
                      usage_cache=None, accounts=None, constraint='user_override', specialty=None)
        values.update(flags)
        return argparse.Namespace(**values)

    def record(self, account, task_id='named', constraint='user_override'):
        import usage_reading
        task = self.task(task_id)
        path = self.root / f'{task_id}.json'
        path.write_text(self.json.dumps({'task': task}), encoding='utf-8')
        with patch.object(usage_reading, 'usage_snapshot', return_value=self.usage):
            return self.router.record_constraint(self.args(account, input=path, constraint=constraint)), task

    def cadence_warnings(self, answer):
        return [w for w in answer.get('warnings') or () if 'cadence window' in w]

    def test_record_alone_launches_with_the_warning_once(self):
        result, _ = self.record('chatgpt-web', 'under')
        self.assertEqual(self.cadence_warnings(result), [])             # under the limit: nothing to reach past
        self.fill('chatgpt-web')
        result, _ = self.record('chatgpt-web', 'at-the-limit')
        self.assertEqual(result['choice'], 'constraint')
        self.assertNotIn('status', result)
        said = self.cadence_warnings(result)
        self.assertEqual(len(said), 1, result.get('warnings'))
        self.assertIn(f'of {LIMIT} dispatches', said[0])
        self.assertTrue(said[0].endswith('launched because the user named it in this request'), said[0])
        self.assertTrue(registry.decision_record(result['decision_id'])['dispatchable'])
        # The router by itself would not choose it: the automatic plan of the same task excludes the account.
        import candidate_eval
        self.assertIn(candidate_eval.OVER_CADENCE, result['override_cost']['excluded'])
        # Two channels, one source: the warning the caller passes on is the alert's own sentence.
        alert = [a['message'] for a in result['alerts'] if a['code'] == 'advisor_cadence']
        self.assertEqual([f'{alert[0]}; launched because the user named it in this request'], said)

    def test_the_ledger_marks_a_named_launch_past_the_cadence(self):
        # Rule (decision_ledger.automatic_routing_marker): `outside_automatic_routing` names the cadence for a launch row
        # at the limit, from the count the alert reads; below the limit there is nothing to reach past.
        import decision_ledger as ledger
        self.fill('chatgpt-web', LIMIT - 1)
        result, _ = self.record('chatgpt-web', 'below')
        row = ledger.read_ledger()[-1]
        self.assertEqual((row['decision_id'], row.get('outside_automatic_routing')), (result['decision_id'], None))
        # That record row is itself a launch outside roll: the next named one is at the limit.
        result, _ = self.record('chatgpt-web', 'at-the-limit')
        row = ledger.read_ledger()[-1]
        self.assertEqual((row['decision_id'], row.get('outside_automatic_routing')), (result['decision_id'], ['cadence']))

    def test_a_named_paseo_route_is_prepared_with_the_warning_and_an_automatic_one_is_not(self):
        import candidate_eval
        launch = {'title': 'Fixture', 'initialPrompt': 'Read the brief', 'cwd': str(self.root)}
        for age_h, held in ((1.0, 'excluded'), (11.9, 'deferred')):     # a seat frees in ~11 h, or in ~0.1 h
            with self.subTest(held=held):
                (self.root / 'ledger.jsonl').unlink(missing_ok=True)
                self.fill('claude', age_h=age_h)
                result, task = self.record('claude', f'named-{held}')
                self.assertEqual(len(self.cadence_warnings(result)), 1, result.get('warnings'))
                row = registry.prepare(result['decision_id'], task, launch, self.usage, self.fleet)
                self.assertEqual((row['state'], row['bundle']['account']), ('prepared', 'claude'))
                registry.release(row['dispatch_id'], 'parent-a', 'fixture: never launched')
                automatic = self.task(f'automatic-{held}')
                planned = registry.issue({'stage': 'plan', 'choice': row['bundle']['bundle_id'],
                                          'selected_bundle': dict(row['bundle'])}, automatic, self.now)
                with self.assertRaises(registry.CapacityChanged) as refused:
                    registry.prepare(planned['decision_id'], automatic, launch, self.usage, self.fleet)
                self.assertRegex(str(refused.exception),
                                 candidate_eval.OVER_CADENCE if held == 'excluded' else 'reset not yet observed')

    def test_a_named_resume_takes_the_turn_with_the_warning(self):
        import usage_reading
        self.fill('claude')
        task = self.task('resume')
        bundle = dict(self.fleet['accounts']['claude']['bundles']['opus55_high'],
                      account='claude', bundle_id='opus55_high', id='opus55_high')
        classified = {'task': task, 'scorer': None, 'alerts': []}
        with patch.object(usage_reading, 'usage_snapshot', return_value=self.usage):
            result = self.router.record_resume(self.args('claude'), task, classified, bundle, 'worker-a')
        said = self.cadence_warnings(result)
        self.assertEqual(len(said), 1, result.get('warnings'))
        self.assertTrue(said[0].endswith("the turn goes to worker 'worker-a' because --resume named it"), said[0])
        import decision_ledger as ledger
        self.assertEqual(ledger.read_ledger()[-1].get('outside_automatic_routing'), ['cadence'])
        launch = {'title': 'Fixture', 'initialPrompt': 'Read the brief', 'cwd': str(self.root)}
        row = registry.prepare(result['decision_id'], task, launch, self.usage, self.fleet,
                               resume_agent_id='worker-a', resume_bundle=bundle, explicit_resume=True)
        self.assertEqual(row['bundle']['account'], 'claude')

    def test_a_project_rule_is_refused_by_record_at_the_limit_and_recorded_below_it(self):
        # Rule (router-internals.md, Pins): a project rule does not reach past a pace of automatic routing. At the limit
        # `record` refuses it, as it refuses one on an unchecked model, and writes nothing; below the limit it records.
        import decision_ledger as ledger
        for account in ('chatgpt-web', 'claude'):
            for constraint in ('hard_constraint', 'dot_closed_loop'):
                with self.subTest(account=account, constraint=constraint):
                    (self.root / 'ledger.jsonl').unlink(missing_ok=True)
                    self.fill(account, LIMIT - 1)
                    result, _ = self.record(account, f'{account}-{constraint}-below', constraint)
                    self.assertEqual(result['choice'], 'constraint')
                    self.assertEqual(self.cadence_warnings(result), [])
                    self.assertTrue(registry.decision_record(result['decision_id'])['dispatchable'])
                    rows = ledger.read_ledger()               # the record row above is the fifth launch
                    with self.assertRaises(ValueError) as refused:
                        self.record(account, f'{account}-{constraint}-at', constraint)
                    said = str(refused.exception)
                    self.assertIn(f'{account}: {LIMIT} of {LIMIT} dispatches', said)
                    self.assertIn('a seat frees in', said)
                    self.assertIn(f'a {constraint} record does not reach past a pace of automatic routing', said)
                    self.assertIn('--constraint user_override', said)
                    self.assertEqual(ledger.read_ledger(), rows)      # no row written

    def between(self, n):
        """record at `n` of the limit with a launch written after its reading of the cadence, before finalize (in
        `_compare_with_router`): the answer and the ledger row of that record."""
        import decision_ledger as ledger
        self.fill('chatgpt-web', n)
        compare = self.router._compare_with_router

        def compare_then_launch(*a, **k):
            out = compare(*a, **k)
            registry.record_external_launch('chatgpt-web', 'between', 'between', dict(BUNDLE, account='chatgpt-web'),
                                            parent_id='parent-a', now=registry.utcnow())
            return out
        with patch.object(self.router, '_compare_with_router', side_effect=compare_then_launch):
            result, _ = self.record('chatgpt-web', f'between-{n}')
        return result, ledger.read_ledger()[-1]

    def test_the_warning_and_the_alert_are_one_reading(self):
        # Rule (route_judge.cadence_reading): record reads the cadence once and finalize alerts from that reading, so a
        # launch written in between changes neither: the warning is the alert's sentence plus the named-route ending.
        result, _ = self.between(LIMIT)
        alert = [a['message'] for a in result['alerts'] if a['code'] == 'advisor_cadence']
        self.assertEqual(len(alert), 1, result['alerts'])
        self.assertIn(f'{LIMIT} of {LIMIT} dispatches', alert[0])
        self.assertEqual(self.cadence_warnings(result),
                         [f'{alert[0]}; launched because the user named it in this request'])

    def test_the_marker_is_the_answers_alert_not_a_second_reading(self):
        # Rule (decision_ledger.automatic_routing_marker): `cadence` comes from the advisor_cadence alert of the row's
        # own answer. Below the limit at the reading, a launch written before the row is not a reach-past.
        result, row = self.between(LIMIT - 1)
        self.assertEqual(self.cadence_warnings(result), [])
        self.assertNotIn('advisor_cadence', {a['code'] for a in result['alerts']})
        self.assertEqual((row['decision_id'], row.get('outside_automatic_routing')), (result['decision_id'], None))

    def test_an_automatic_plan_is_never_marked_cadence(self):
        # Rule (decision_ledger.automatic_routing_marker): only a row the user named carries `cadence`. An automatic plan
        # on a cadence account at its limit, deferred until a seat frees, launches nothing past the cadence.
        import contextlib
        import io
        import caller_input
        import decision_ledger as ledger
        import host_policy
        import scorer_client
        import usage_reading
        self.fill('claude', age_h=11.95)                     # 5 of 5, a seat frees in ~0.05 h
        path = self.root / 'automatic.json'
        path.write_text(self.json.dumps({'task': self.task('automatic')}), encoding='utf-8')
        args = self.router.build_parser().parse_args(['plan', '--input', str(path)])
        scored = (self.profile(work_kind='architecture_reasoning', expected_hours=0.5), {'source': 'system_one', 'model': 'm'})
        out = io.StringIO()
        with patch.object(host_policy, 'load_policy', return_value={'allowed_accounts': ['claude']}), \
                patch.object(scorer_client, 'score_task', return_value=scored), \
                patch.object(usage_reading, 'usage_snapshot', return_value=self.usage), contextlib.redirect_stdout(out):
            self.router._run_stage(caller_input.attach(args, caller_input.capture(args)))
        answer = self.json.loads(out.getvalue()[out.getvalue().index('{'):])
        row = ledger.read_ledger()[-1]
        self.assertEqual((answer['decision']['action'], (answer.get('selected_bundle') or {}).get('account')),
                         ('defer', 'claude'))
        self.assertEqual((row['stage'], row.get('outside_automatic_routing')), ('plan', None))

    def test_a_route_named_to_roll_is_the_users_naming_and_nothing_else(self):
        # Rule (roll.named_route_refusal): beside route flags, --constraint can only say what the named route already is
        # (user_override); a project rule is recorded with `record`, which holds it to this machine's switches. Without
        # route flags --constraint is a flag roll does not read (route_judge.unread_flags), not a refusal.
        import argparse
        import roll
        flags = vars(self.args('chatgpt-web'))
        for constraint in ('hard_constraint', 'dot_closed_loop', 'user_override', None):
            named = argparse.Namespace(**dict(flags, constraint=constraint))
            unnamed = argparse.Namespace(**dict(flags, constraint=constraint, provider=None, account=None, model=None,
                                                effort=None))
            with self.subTest(constraint=constraint):
                refusal = roll.named_route_refusal(named)
                self.assertIsNone(roll.named_route_refusal(unnamed))
                if constraint in (None, 'user_override'):
                    self.assertIsNone(refusal)
                else:
                    self.assertIn(f'--constraint {constraint}', refusal)
                    self.assertIn("the user's own naming", refusal)
                    self.assertIn(f'record --constraint {constraint}', refusal)
                # `record` itself reads the project rule (it is held to the switches there, not refused here).
                self.assertIsNone(roll.named_route_refusal(named, require=True))
        parsed = self.router.build_parser().parse_args(['roll', '--task', 'Advise', '--constraint', 'hard_constraint'])
        parsed.stage = 'roll'
        self.assertEqual(self.router.unread_flags(parsed), ['--constraint'])

    def test_roll_named_to_a_consult_hands_it_over_with_the_warning_once(self):
        import argparse
        import roll
        from router_test_support import profile_for
        self.fill('chatgpt-web')
        brief = self.root / 'brief.md'
        brief.write_text('# Fixture\n\nAdvise on the plan.\n', encoding='utf-8', newline='\n')
        args = argparse.Namespace(brief=brief, file=[], apply_to=None, local_only=None, hours=0.5, title=None,
                                  workspace=None, task_id='consult', parent_id=None, parent_host=None,
                                  parent_address=None, dry_run=False, verbose=True, task=None, new_worker=False,
                                  resume=None, fast=False, keep_whole=None, access=None,
                                  **vars(self.args('chatgpt-web')))

        def classified(task, _args):
            return {'task': {**task, 'work_kind': 'architecture_reasoning', 'profile': profile_for(
                {'work_kind': 'architecture_reasoning', 'expected_hours': 0.5})}, 'scorer': None,
                'hours_estimate': None, 'alerts': []}
        import usage_reading
        with patch.dict(registry.os.environ, {'PASEO_AGENT_ID': 'parent-a'}), \
                patch.object(self.router, 'classify_pinned', side_effect=classified), \
                patch.object(usage_reading, 'usage_snapshot', return_value=self.usage), \
                patch.object(roll, 'WORKER_PROMPTS_DIR', self.root / 'worker-prompts'), \
                patch.object(roll, 'RESULTS_DIR', self.root / 'roll-results'):
            result, code = roll.run(args, self.router)
        self.assertEqual((code, result['dispatch_mode']), (0, 'chatgpt-web'), result)
        self.assertEqual(len(self.cadence_warnings(result['route'])), 1, result['route'].get('warnings'))
        self.assertEqual(len([w for w in result.get('warnings') or () if 'cadence window' in w]), 0)


if __name__ == '__main__':
    unittest.main()
