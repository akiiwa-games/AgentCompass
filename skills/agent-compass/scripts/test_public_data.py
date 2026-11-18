"""Published reference data channel: publication, acceptance, fetch, the set a process holds, fall-back, interval, launch
values and precedence.

Each test names the rule it holds (router-internals.md, Reference data). No request leaves the machine: the unit tests
fetch through FakeRemote or a file:// location, the end-to-end class through a local HTTP fixture on 127.0.0.1.
"""
from datetime import datetime, timedelta, timezone
import copy
import functools
import http.server
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

import pytest

import account_fleet
import auto_bundles
import axis_scores
import host_placement
import launch_values
import provider_inventory
import public_data
import scorer_client
import user_scores
import worker_sessions
from router_test_support import isolated_router_state

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
URL = 'https://example.invalid/published/'
REPO = Path(public_data.__file__).resolve().parents[3]
SCHEMA = str(public_data.SCHEMA_VERSION)


class FakeRemote:
    """The published set of this schema, served by name; records every URL asked for."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.calls = []
        self.offline = False
        self.overrides = {}

    def __call__(self, url, timeout):
        self.calls.append(url)
        if self.offline:
            raise urllib.error.URLError('offline')
        name = url.rsplit('/', 1)[-1]
        if name in self.overrides:
            return self.overrides[name]
        return (self.directory / name).read_bytes()


def write_json(path, document):
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def copy_set(folder):
    folder.mkdir(parents=True, exist_ok=True)
    for name in public_data.FILES:
        shutil.copyfile(public_data.BUNDLED / name, folder / name)


def edit(folder, name, change):
    document = read_json(folder / name)
    change(document)
    write_json(folder / name, document)


# Shapes this code cannot route with; each one passed the checks of b2cb79d (review p2, p4).
UNREADABLE = {
    'scoring_missing': ('accounts.json', lambda d: d['scoring'].pop('rotate_after_carried_tokens')),
    'bundle_str': ('accounts.json', lambda d: d['accounts']['codex']['bundles'].__setitem__('sol62_high', 'gpt-6.2-sol')),
    'drop_targets': ('five-axis-scores.json', lambda d: d['anchor'].pop('targets')),
    'scoring_type': ('accounts.json', lambda d: d['scoring'].__setitem__('safety_margin_pct', '5')),
    'new_family': ('accounts.json', lambda d: d['accounts'].__setitem__('newcli', {
        'plan': 'New', 'billing': 'subscription', 'windows': {'usage': {'hours': 168}},
        'bundles': {'nb_high': {'model': 'gpt-6.2-sol', 'effort': 'high'}}})),
    'new_harness': ('accounts.json', lambda d: d['accounts']['codex']['bundles'].__setitem__(
        'sol62_newcli', {'model': 'gpt-6.2-sol', 'effort': 'high', 'harness': 'newcli'})),
    'unknown_effort': ('accounts.json', lambda d: d['accounts']['codex']['bundles'].__setitem__(
        'sol62_turbo', {'model': 'gpt-6.2-sol', 'effort': 'turbo'})),
    'injected_model': ('accounts.json', lambda d: d['accounts']['codex']['bundles'].__setitem__(
        'evil', {'model': 'gpt-6.2-sol $(calc.exe)', 'effort': 'high'})),
}


class PublicationTests(unittest.TestCase):
    """Rule: publication is one act (`data publish`) that writes published/<schema>/; editing references/ publishes
    nothing; the check fails a published set this code does not accept, an inconsistent one, or one whose publication
    time is not later than the set it replaces."""

    def setUp(self):
        root = Path(tempfile.mkdtemp(prefix='public-data-publish-'))
        self.addCleanup(shutil.rmtree, root, True)
        self.references = root / 'references'
        copy_set(self.references)
        self.root = root / 'published'
        self.target = self.root / SCHEMA

    def test_the_repository_publishes_what_it_ships(self):
        self.assertEqual(public_data.check_published(release=True), '')

    def test_publish_writes_the_set_to_its_schema_and_editing_references_publishes_nothing(self):
        manifest = public_data.publish(self.references, self.root, now=T0)
        self.assertEqual(read_json(self.target / public_data.MANIFEST), manifest)
        self.assertEqual((self.references / public_data.MANIFEST).read_bytes(), (self.target / public_data.MANIFEST).read_bytes())
        for name in public_data.FILES:
            self.assertEqual((self.target / name).read_bytes(), (self.references / name).read_bytes())
        edit(self.references, 'accounts.json', lambda d: d['scoring'].__setitem__('safety_margin_pct', 0.6))
        self.assertEqual(read_json(self.target / public_data.MANIFEST), manifest)          # nothing published
        self.assertEqual(public_data.check_published(self.root, self.references), '')
        self.assertIn('accounts.json', public_data.check_published(self.root, self.references, release=True))

    def test_published_at_moves_only_with_the_content_and_always_past_the_previous_publication(self):
        first = public_data.publish(self.references, self.root, now=T0)
        self.assertEqual(public_data.publish(self.references, self.root, now=T0 + timedelta(days=3)), first)
        edit(self.references, 'accounts.json', lambda d: d['scoring'].__setitem__('safety_margin_pct', 0.6))
        second = public_data.publish(self.references, self.root, now=T0 - timedelta(days=1))   # a clock behind
        self.assertGreater(public_data._parse(second['published_at']), public_data._parse(first['published_at']))

    def test_the_check_fails_a_published_set_that_does_not_match_its_manifest(self):
        public_data.publish(self.references, self.root, now=T0)
        edit(self.target, 'accounts.json', lambda d: d['scoring'].__setitem__('safety_margin_pct', 0.6))
        self.assertIn('not consistent', public_data.check_published(self.root, self.references))

    def test_the_check_fails_a_publication_not_later_than_the_one_it_replaces(self):
        previous = public_data.publish(self.references, self.root, now=T0)
        edit(self.references, 'accounts.json', lambda d: d['scoring'].__setitem__('safety_margin_pct', 0.6))
        current = public_data.publish(self.references, self.root, now=T0 + timedelta(days=1))
        self.assertEqual(public_data.check_published(self.root, self.references, previous=previous), '')
        write_json(self.target / public_data.MANIFEST, {**current, 'published_at': previous['published_at']})
        self.assertIn('not later than', public_data.check_published(self.root, self.references, previous=previous))
        ahead = public_data._stamp(T0 + timedelta(days=4))         # beyond a day of clock skew: installs would hold it
        write_json(self.target / public_data.MANIFEST, {**current, 'published_at': ahead})
        self.assertIn('in the future', public_data.check_published(self.root, self.references, now=T0 + timedelta(days=1)))

    def test_publish_and_the_check_refuse_a_set_this_code_cannot_route_with(self):
        for label, (name, change) in UNREADABLE.items():
            with self.subTest(label):
                shutil.rmtree(self.references)
                copy_set(self.references)
                edit(self.references, name, change)
                with self.assertRaises(ValueError):
                    public_data.publish(self.references, self.root, now=T0)
                self.assertFalse((self.target / public_data.MANIFEST).exists())
                self.target.mkdir(parents=True, exist_ok=True)
                for file in public_data.FILES:
                    shutil.copyfile(self.references / file, self.target / file)
                write_json(self.target / public_data.MANIFEST, public_data.build_manifest(self.target, now=T0))
                self.assertIn('does not accept', public_data.check_published(self.root, self.references))
                shutil.rmtree(self.target)


class AcceptanceTests(unittest.TestCase):
    """Rule: a set is accepted only when this code's own readers load it and an offline plan completes with it; each
    refusal names the step. The candidate is read, never the set the process holds."""

    def test_each_shape_a_reader_cannot_read_is_refused_by_name(self):
        launch = 'accounts.json launch values'
        steps = {'scoring_missing': ('scoring', 'rotate_after_carried_tokens'), 'bundle_str': (launch, 'not an object'),
                 'drop_targets': ('plan on the reference fleet', ''), 'scoring_type': ('plan on the fleet of the set', ''),
                 'new_family': (launch, "family 'newcli'"), 'new_harness': (launch, "harness 'newcli'"),
                 'unknown_effort': (launch, "effort 'turbo'"), 'injected_model': (launch, 'does not match')}
        public_data.accept(public_data.BUNDLED)
        for label, (name, change) in UNREADABLE.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as folder:
                folder = Path(folder)
                copy_set(folder)
                edit(folder, name, change)
                with self.assertRaises(ValueError) as refused:
                    public_data.accept(folder)
                step, cause = steps[label]
                self.assertTrue(str(refused.exception).startswith(step), refused.exception)
                self.assertIn(cause, str(refused.exception))

    def test_the_acceptance_runs_every_run_time_reader_of_the_set(self):
        """Each reader is a step: replacing one with a no-op makes its broken shape pass (mutation guard)."""
        labels = [label for label, _ in public_data._acceptance_steps()]
        for reader in ('accounts.json launch values', 'accounts.json through load_accounts', 'scoring',
                       'five-axis-scores.json', 'user-scores.json', 'classifier-cases.json', 'plan-templates.json',
                       'plan on the reference fleet', 'plan on the fleet of the set'):
            self.assertIn(reader, labels)


class ChannelCase(unittest.TestCase):
    def setUp(self):
        public_data.release()
        self.addCleanup(public_data.release)
        root = Path(tempfile.mkdtemp(prefix='public-data-'))
        self.addCleanup(shutil.rmtree, root, True)
        self.state = root / 'state'
        self.bundled = root / 'install' / 'references'
        self.remote_dir = root / 'remote' / SCHEMA
        for folder in (self.bundled, self.remote_dir):
            copy_set(folder)
        write_json(self.bundled / public_data.MANIFEST, public_data.build_manifest(self.bundled, now=T0))
        self.env = {'ROUTER_STATE_DIR': str(self.state), 'AGENTCOMPASS_PUBLIC_DATA': 'on',
                    'AGENTCOMPASS_PUBLIC_DATA_URL': URL}
        self.publish(safety_margin_pct=0.5, at=T0 + timedelta(days=1))
        self.remote = FakeRemote(self.remote_dir)

    def publish(self, safety_margin_pct=None, at=None, schema=None, change=None):
        """The served set; written without the acceptance so a test can serve a set this code refuses."""
        if safety_margin_pct is not None:
            edit(self.remote_dir, 'accounts.json', lambda d: d['scoring'].__setitem__('safety_margin_pct', safety_margin_pct))
        if change is not None:
            edit(self.remote_dir, *change)
        manifest = public_data.build_manifest(self.remote_dir, now=at)
        if schema is not None:
            manifest['schema_version'] = schema
        write_json(self.remote_dir / public_data.MANIFEST, manifest)
        return manifest

    def refresh(self, **kwargs):
        kwargs.setdefault('now', T0 + timedelta(days=2))
        return public_data.refresh(environ=self.env, fetch=self.remote, bundled=self.bundled, **kwargs)

    def held(self):
        public_data.release()
        return public_data.held(self.bundled, self.env)

    def held_accounts(self):
        return public_data.document(self.bundled / 'accounts.json', self.env)


class FetchTests(ChannelCase):
    def test_a_set_accepted_and_published_after_the_shipped_copy_is_held_instead_of_it(self):
        """Rule: complete, accepted by this code, published after the shipped copy."""
        self.assertEqual(self.refresh()['status'], 'updated')
        chosen = self.held()
        self.assertEqual(chosen['source'], 'fetched')
        self.assertEqual(self.held_accounts()['scoring']['safety_margin_pct'], 0.5)
        self.assertEqual(public_data.read_state(self.env)['accepted_by'], public_data.reader_id())

    def test_the_request_goes_to_the_published_set_of_this_schema(self):
        """Rule: an install fetches only the location of the schema it reads."""
        self.refresh()
        self.assertTrue(all(url.startswith(f'{URL}{SCHEMA}/') for url in self.remote.calls), self.remote.calls)

    def test_a_hash_mismatch_applies_nothing(self):
        self.remote.overrides['accounts.json'] = b'{"tampered": true}\n'
        outcome = self.refresh()
        self.assertEqual(outcome['status'], 'failed')
        self.assertIn('sha256', outcome['error'])
        self.assertEqual(self.held()['source'], 'bundled')
        self.assertFalse(list((self.state / 'public-data' / 'sets').glob('[0-9a-f]*')))

    def test_a_newer_data_schema_is_noted_and_ignored_after_one_request(self):
        self.publish(schema=public_data.SCHEMA_VERSION + 1)
        outcome = self.refresh()
        self.assertEqual(outcome['status'], 'schema_too_new')
        self.assertEqual(self.remote.calls, [f'{URL}{SCHEMA}/{public_data.MANIFEST}'])
        self.assertEqual(self.held()['source'], 'bundled')

    def test_a_set_this_code_does_not_accept_is_named_once_and_never_downloaded_again(self):
        self.publish(change=UNREADABLE['drop_targets'], at=T0 + timedelta(days=1, hours=1))
        outcome = self.refresh()
        self.assertEqual(outcome['status'], 'rejected')
        self.assertIn('plan on the reference fleet', outcome['detail'])
        self.assertEqual(self.held()['source'], 'bundled')
        self.assertIn('not usable by this install', public_data.status(self.env, self.bundled)['note'])
        self.remote.calls.clear()
        self.assertEqual(self.refresh(force=True, now=T0 + timedelta(days=3))['status'], 'rejected')
        self.assertEqual(self.remote.calls, [f'{URL}{SCHEMA}/{public_data.MANIFEST}'])

    def test_a_fetched_set_is_held_only_after_this_code_accepted_it(self):
        """Rule: a set recorded by another reader (an updated install) is not read until this code accepts it; the
        next check accepts it without downloading."""
        self.assertEqual(self.refresh()['status'], 'updated')
        public_data._update_state(self.env, lambda s: s.update(accepted_by='another-reader'))
        self.assertEqual(self.held()['source'], 'bundled')
        self.assertIn('not yet accepted', self.held()['note'])
        self.remote.calls.clear()
        self.assertEqual(self.refresh(force=True, now=T0 + timedelta(days=3))['status'], 'accepted')
        self.assertEqual(self.remote.calls, [f'{URL}{SCHEMA}/{public_data.MANIFEST}'])
        self.assertEqual(self.held()['source'], 'fetched')

    def test_offline_the_last_accepted_set_then_the_shipped_copy_is_read(self):
        self.assertEqual(self.refresh()['status'], 'updated')
        self.remote.offline = True
        self.assertEqual(self.refresh(force=True, now=T0 + timedelta(days=3))['status'], 'failed')
        self.assertEqual(self.held()['source'], 'fetched')
        shutil.rmtree(self.state / 'public-data' / 'sets')
        self.assertEqual(self.held()['source'], 'bundled')

    def test_the_check_interval_and_retry_hold_and_an_unchanged_manifest_costs_one_request(self):
        self.assertEqual(self.refresh()['status'], 'updated')
        self.remote.calls.clear()
        self.assertEqual(self.refresh(now=T0 + timedelta(days=2, hours=5))['status'], 'not_due')
        self.assertEqual(self.remote.calls, [])
        self.assertEqual(self.refresh(now=T0 + timedelta(days=2, hours=7))['status'], 'unchanged')
        self.assertEqual(len(self.remote.calls), 1)
        self.remote.offline = True
        self.assertEqual(self.refresh(now=T0 + timedelta(days=2, hours=14))['status'], 'failed')
        self.assertEqual(self.refresh(now=T0 + timedelta(days=2, hours=14, minutes=20))['status'], 'not_due')
        self.assertEqual(self.refresh(now=T0 + timedelta(days=2, hours=14, minutes=31))['status'], 'failed')

    def test_a_check_recorded_in_the_future_is_no_check(self):
        """Rule: a clock that ran ahead does not freeze the check until that date."""
        self.assertEqual(self.refresh(now=T0 + timedelta(days=2000))['status'], 'updated')
        self.assertTrue(public_data.due(public_data.read_state(self.env), public_data.config(self.env), T0 + timedelta(days=3)))
        public_data._update_state(self.env, lambda s: s.update(checked_at=None, attempt_at='2031-01-01T00:00:00Z'))
        self.assertTrue(public_data.due(public_data.read_state(self.env), public_data.config(self.env), T0 + timedelta(days=3)))

    def test_a_fetched_set_not_newer_than_the_shipped_copy_is_not_read(self):
        self.publish(at=T0 - timedelta(days=1))
        self.assertEqual(self.refresh()['status'], 'updated')
        self.assertEqual(self.held()['source'], 'bundled')

    def test_shipped_references_with_local_edits_are_used_as_they_are(self):
        self.assertEqual(self.refresh()['status'], 'updated')
        edit(self.bundled, 'accounts.json', lambda d: d['scoring'].__setitem__('safety_margin_pct', 0.7))
        self.assertEqual(self.held()['source'], 'bundled')
        self.assertEqual(self.held_accounts()['scoring']['safety_margin_pct'], 0.7)

    def test_the_current_and_the_previous_set_are_kept_and_older_ones_removed(self):
        versions = []
        for day, margin in ((1, 0.5), (2, 0.6), (3, 0.7)):
            versions.append(self.publish(safety_margin_pct=margin, at=T0 + timedelta(days=day, hours=1))['version'])
            self.assertEqual(self.refresh(force=True, now=T0 + timedelta(days=day + 1))['status'], 'updated')
        kept = sorted(p.name for p in (self.state / 'public-data' / 'sets').iterdir())
        self.assertEqual(kept, sorted(versions[1:]))

    def test_a_set_an_older_release_fetched_is_ignored_and_kept(self):
        older = self.state / 'public-data' / 'sets' / '0123456789ab'
        older.mkdir(parents=True)
        write_json(older / public_data.MANIFEST, {'schema_version': 6})
        public_data._update_state(self.env, lambda s: s.update(version='0123456789ab'))
        self.assertEqual(self.held()['source'], 'bundled')
        self.assertTrue(older.is_dir())


class HeldSetTests(ChannelCase):
    """Rule: the unit is the set, for every process: taken whole, kept, moved only as a whole; every fall-back is to the
    shipped set as a whole."""

    def test_a_process_keeps_the_whole_set_it_took_whatever_happens_on_disk(self):
        self.assertEqual(self.refresh()['status'], 'updated')
        chosen = self.held()
        self.assertEqual(chosen['source'], 'fetched')
        self.publish(safety_margin_pct=0.6, at=T0 + timedelta(days=2, hours=1))
        self.assertEqual(self.refresh(force=True, now=T0 + timedelta(days=3))['status'], 'updated')
        shutil.rmtree(Path(chosen['dir']))                                        # pruned, or deleted by hand
        for name in public_data.FILES:                                            # every file from the set it took
            self.assertEqual(public_data.document(self.bundled / name, self.env),
                             json.loads(chosen['files'][name].decode('utf-8-sig')))
        self.assertEqual(public_data.retake(self.bundled, self.env)['version'],
                         public_data.read_state(self.env)['version'])             # moves as a whole, when asked

    def test_a_reader_failing_on_the_fetched_set_reruns_on_the_shipped_set_and_rejects_it(self):
        self.assertEqual(self.refresh()['status'], 'updated')
        self.held()
        seen = []

        def run():
            chosen = public_data.held(self.bundled, self.env)
            seen.append(chosen['source'])
            if chosen['source'] == 'fetched':
                raise KeyError('a field the acceptance did not exercise')
            return 'routed'
        with patch.object(public_data, 'BUNDLED', self.bundled), patch.dict(os.environ, self.env):
            self.assertEqual(public_data.with_fallback(run), 'routed')
        self.assertEqual(seen, ['fetched', 'bundled'])
        state = public_data.read_state(self.env)
        self.assertEqual((state['rejected_version'], state['rejected_by']), (state['version'], public_data.reader_id()))
        self.assertEqual(self.held()['source'], 'bundled')
        self.assertIn('failed this install', public_data.status(self.env, self.bundled)['note'])
        self.remote.calls.clear()
        self.assertEqual(self.refresh(force=True, now=T0 + timedelta(days=3))['status'], 'rejected')
        self.assertEqual(len(self.remote.calls), 1)

    def test_a_failure_the_shipped_set_produces_as_well_rejects_nothing(self):
        self.assertEqual(self.refresh()['status'], 'updated')
        self.held()

        def run():
            public_data.held(self.bundled, self.env)
            raise ValueError('a caller error that is not about the data')
        with patch.object(public_data, 'BUNDLED', self.bundled), patch.dict(os.environ, self.env), \
                self.assertRaises(ValueError):
            public_data.with_fallback(run)
        self.assertNotIn('rejected_version', public_data.read_state(self.env))

    def test_concurrent_checks_never_fail_on_the_state_file(self):
        """Rule: contention is no error: the state is written under the process-safe lock (override_store)."""
        remote_root = self.remote_dir.parent
        env = {**os.environ, **self.env, 'AGENTCOMPASS_PUBLIC_DATA_URL': remote_root.as_uri() + '/'}
        code = ('import sys, json; sys.path.insert(0, sys.argv[1]); import public_data; '
                'print(json.dumps([public_data.refresh(force=True)["status"] for _ in range(8)]))')
        procs = [subprocess.Popen([sys.executable, '-c', code, str(Path(public_data.__file__).parent)], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(4)]
        outcomes = []
        for proc in procs:
            out, err = proc.communicate(timeout=180)
            self.assertEqual(proc.returncode, 0, err[-800:])
            outcomes += json.loads(out)
        self.assertNotIn('state_not_saved', outcomes)
        self.assertNotIn('failed', outcomes, outcomes)


    def test_a_check_while_another_process_holds_the_state_is_busy_and_exits_0(self):
        """Rule: contention is no error: a check that cannot take the state lock reports busy and `--refresh` exits 0."""
        import io
        import override_store
        path = public_data.cache_dir(self.env) / 'state.json'
        with patch.object(public_data, 'LOCK_SECONDS', 0.2), override_store.locked(path, timeout=5):
            self.assertEqual(self.refresh(force=True)['status'], 'busy')
            with patch.dict(os.environ, self.env), patch.object(sys, 'stdout', io.StringIO()) as out:
                self.assertEqual(public_data.main(['--refresh']), 0)
            self.assertEqual(json.loads(out.getvalue())['refresh']['status'], 'busy')


class RouteFallbackTests(unittest.TestCase):
    """Rule: a fetched set is never on the failure line. A reader failing on it in a way the acceptance cannot see (here
    one that fails only for a fetched set) makes `plan` run again on the shipped set; the set is recorded as rejected."""

    def test_plan_reruns_on_the_shipped_set_through_the_stage_handler(self):
        import route_judge
        self.plan_falls_back(TypeError, route_judge.main)

    def test_plan_reruns_on_the_shipped_set_through_the_cli_entry_point(self):
        import runpy
        import route_judge

        def entry():
            try:
                runpy.run_path(route_judge.__file__, run_name='__main__')
            except SystemExit as done:
                return done.code
        self.plan_falls_back(KeyError, entry)       # not one the stage handler catches: the entry point's fall-back

    def plan_falls_back(self, error, entry):
        import io
        isolated_router_state(self.addCleanup)
        public_data.release()
        self.addCleanup(public_data.release)
        root = Path(tempfile.mkdtemp(prefix='public-data-route-'))
        self.addCleanup(shutil.rmtree, root, True)
        remote = root / 'remote'
        copy_set(remote)
        edit(remote, 'accounts.json', lambda d: d['scoring'].__setitem__('safety_margin_pct', 0.5))
        write_json(remote / public_data.MANIFEST, public_data.build_manifest(
            remote, now=datetime.now(timezone.utc) + timedelta(minutes=1)))
        env = {'ROUTER_STATE_DIR': os.environ['ROUTER_STATE_DIR'], 'AGENTCOMPASS_PUBLIC_DATA': 'on',
               'AGENTCOMPASS_PUBLIC_DATA_URL': URL}
        self.assertEqual(public_data.refresh(environ=env, fetch=FakeRemote(remote))['status'], 'updated')
        task = root / 'task.json'
        write_json(task, {'task': {'objective': 'Fix an off-by-one slice in parser.py and add a unit test.',
                                   'expected_hours': 0.5}})
        real = account_fleet.load_accounts
        seen = []

        def load_accounts(*args, **kwargs):
            seen.append(public_data.held()['source'])
            if seen[-1] == 'fetched':
                raise error('a field only the live fleet reaches')
            return real(*args, **kwargs)
        out = io.StringIO()
        with patch.dict(os.environ, env), patch.object(account_fleet, 'load_accounts', load_accounts), \
                patch.object(sys, 'argv', ['route_judge.py', 'plan', '--input', str(task), '--dry-run']), \
                patch.object(sys, 'stdout', out):
            code = entry()
        self.assertEqual(seen[0], 'fetched')
        self.assertEqual(set(seen[1:]), {'bundled'})
        self.assertNotIn('Routing input or local runtime failed', out.getvalue())
        self.assertIn(code, (0, 2))                                       # a plan, as with the channel off
        state = public_data.read_state(env)
        self.assertEqual(state['rejected_version'], state['version'])
        public_data.release()
        self.assertEqual(public_data.held(None, env)['source'], 'bundled')
        self.assertIn('failed this install', public_data.status(env)['note'])


class SwitchAndBoundTests(ChannelCase):
    def test_off_makes_no_network_call_and_reads_the_shipped_copy(self):
        self.env['AGENTCOMPASS_PUBLIC_DATA'] = 'off'
        self.assertEqual(self.refresh(force=True)['status'], 'disabled')
        self.assertEqual(self.remote.calls, [])
        self.assertEqual(public_data.refresh_in_background(self.env), None)
        self.assertEqual(self.held()['source'], 'bundled')

    def test_the_state_file_switches_the_channel_off_and_the_environment_wins(self):
        self.env.pop('AGENTCOMPASS_PUBLIC_DATA')
        self.state.mkdir(parents=True, exist_ok=True)
        write_json(self.state / public_data.CONFIG_FILE, {'enabled': False})
        self.assertEqual(public_data.config(self.env)['enabled'], False)
        self.assertEqual(public_data.config({**self.env, 'AGENTCOMPASS_PUBLIC_DATA': '1'})['enabled'], True)

    def test_the_environment_location_beats_the_state_file_location(self):
        self.state.mkdir(parents=True, exist_ok=True)
        write_json(self.state / public_data.CONFIG_FILE, {'url': 'https://config.invalid/p/'})
        self.assertEqual(public_data.config(self.env)['url'], f'{URL}{SCHEMA}/')
        self.assertEqual(public_data.config({k: v for k, v in self.env.items() if k != 'AGENTCOMPASS_PUBLIC_DATA_URL'})['url'],
                         f'https://config.invalid/p/{SCHEMA}/')

    def test_the_published_location_comes_from_origin_and_a_request_carries_exactly_two_headers(self):
        spec = importlib.util.spec_from_file_location('origin_for_test', REPO / 'agentcompass' / 'origin.py')
        origin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(origin)
        self.assertEqual(public_data.published_url(), origin.PUBLIC_DATA_URL)
        # Rule: an install fetches from the directory `data publish` writes, on main (never references/).
        self.assertEqual(public_data.PUBLISHED, REPO / 'published')
        self.assertTrue(origin.PUBLIC_DATA_URL.endswith(f'/main/{public_data.PUBLISHED.name}/'), origin.PUBLIC_DATA_URL)
        self.assertEqual(public_data.config({'ROUTER_STATE_DIR': str(self.state)})['url'], f'{origin.PUBLIC_DATA_URL}{SCHEMA}/')
        sent = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size):
                return b'{}'
        with patch.object(urllib.request, 'urlopen', lambda request, timeout: sent.append(request) or Response()):
            public_data.http_get('https://example.invalid/x')
        self.assertEqual(dict(sent[0].header_items()), {'User-agent': 'AgentCompass-public-data', 'Cache-control': 'no-cache'})

    def test_a_background_check_runs_in_its_own_process_and_the_caller_never_waits(self):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(8)
        self.addCleanup(listener.close)
        env = {**os.environ, **self.env, 'AGENTCOMPASS_PUBLIC_DATA_URL': f'http://127.0.0.1:{listener.getsockname()[1]}/',
               'NO_PROXY': '*', 'HTTP_PROXY': '', 'HTTPS_PROXY': ''}
        started = time.monotonic()
        child = public_data.refresh_in_background(env)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertIsNotNone(child)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        self.assertIsNone(public_data.refresh_in_background(env))               # claimed: no second process
        self.assertEqual(child.wait(timeout=60), 0)                              # bounded by the request timeout
        self.assertTrue(public_data.read_state(self.env).get('last_error'))     # the child recorded its outcome


class LaunchValueTests(unittest.TestCase):
    """Rule: a value from reference data or discovery reaches a command only when it matches the declared pattern, and
    every place that builds one quotes it for its syntax."""

    def test_the_reference_document_is_refused_and_a_discovered_row_dropped_by_name(self):
        doc = read_json(public_data.BUNDLED / 'accounts.json')
        self.assertEqual(account_fleet.launch_problems(doc), [])
        bad = copy.deepcopy(doc)
        bad['accounts']['codex']['bundles']['evil'] = {'model': 'gpt-6.2-sol $(calc.exe)', 'effort': 'high'}
        with self.assertRaises(ValueError) as refused:
            account_fleet.load_accounts(document=bad)
        self.assertIn('codex.bundles.evil.model', str(refused.exception))
        fleet = {'accounts': {'codex': {'bundles': {'ok': {'model': 'gpt-6.2-sol', 'effort': 'high'},
                                                    'evil': {'model': 'm', 'effort': 'high"; touch x; echo "'}}}}}
        dropped = account_fleet.drop_unlaunchable_rows(fleet)
        self.assertEqual(list(fleet['accounts']['codex']['bundles']), ['ok'])
        self.assertTrue(dropped and 'codex.bundles.evil.effort' in dropped[0])

    def test_values_are_quoted_for_the_syntax_they_land_in(self):
        self.assertEqual(json.loads(launch_values.toml_string('high"; x = "')), 'high"; x = "')
        self.assertEqual(launch_values.shell_word('gpt-6.2-sol'), 'gpt-6.2-sol')
        self.assertEqual(launch_values.shell_word('a $(b)'), "'a $(b)'")
        with self.assertRaises(ValueError):
            launch_values.shell_word("it's")
        text = host_placement.native_next('orca', {'provider': 'codex', 'model': 'gpt $(calc)', 'effort': 'high'}, 'addr')
        self.assertIn("--model 'gpt $(calc)'", text)
        import roll
        argv = roll.CLI_RUNS['codex'][1]('m', 'high"; y = "', 'C:/w')
        self.assertIn('model_reasoning_effort=' + json.dumps('high"; y = "'), argv)


class LoaderPrecedenceTests(ChannelCase):
    """Rule: every run-time reader reads the held set through the one accessor; shipped < fetched < the user's own."""

    def setUp(self):
        super().setUp()
        isolated_router_state(self.addCleanup)
        os.environ.pop('ROUTER_ACCOUNTS_FILE', None)
        os.environ.update(self.env)
        self.env = {**self.env, 'ROUTER_STATE_DIR': os.environ['ROUTER_STATE_DIR']}
        self.state = Path(self.env['ROUTER_STATE_DIR'])
        self.state.mkdir(parents=True, exist_ok=True)

    def test_every_run_time_reader_reads_the_held_set(self):
        sys.path.insert(0, str(REPO / 'tray'))
        import router_data                  # imported before the fetched set exists: nothing may freeze at import
        router_data.TUNING_DEFAULTS
        edit(self.remote_dir, 'plan-templates.json', lambda d: d['claude']['pro']['windows']['five_hour'].__setitem__('hours', 6))
        edit(self.remote_dir, 'five-axis-scores.json', lambda d: d.__setitem__('generated_at', '2026-11-12T00:00:00Z'))
        edit(self.remote_dir, 'user-scores.json', lambda d: d['scores'].setdefault('grok-bot', {}).__setitem__(
            'coding', {'level': 2.0, 'as_of': '2026-11-11', 'kind': 'maintainer'}))
        edit(self.remote_dir, 'accounts.json', lambda d: d['models']['gpt-6.2-sol'].__setitem__('label', 'Fetched label'))
        edit(self.remote_dir, 'accounts.json', lambda d: d['scoring'].__setitem__('short_window_floor_pct', 11))
        edit(self.remote_dir, 'classifier-cases.json', lambda d: d['route_definitions'].__setitem__(
            'code_implementation', 'fetched definition'))
        self.publish(at=T0 + timedelta(days=2))
        self.assertEqual(self.refresh(force=True, now=T0 + timedelta(days=3))['status'], 'updated')
        public_data.release()
        with patch.object(account_fleet, 'REFERENCES', self.bundled):
            self.assertEqual(account_fleet.fleet_document()['models']['gpt-6.2-sol']['label'], 'Fetched label')
            self.assertEqual(axis_scores.load_five_axis()['generated_at'], '2026-11-12T00:00:00Z')
            self.assertEqual(account_fleet.reference_fleet_document()['families'],
                             read_json(self.remote_dir / 'accounts.json')['families'])
        with patch.object(user_scores, 'PUBLIC_SCORES_FILE', self.bundled / 'user-scores.json'):
            self.assertEqual(user_scores.load_public()['scores']['grok-bot']['coding']['level'], 2.0)
            self.assertEqual(user_scores.public_scores_path(), self.bundled / 'user-scores.json')   # `--public` writes the shipped file
        with patch.object(provider_inventory, 'PLAN_TEMPLATES', self.bundled / 'plan-templates.json'), \
                patch.object(provider_inventory, '_SHIPPED_PLAN_TEMPLATES', None):
            self.assertEqual(provider_inventory.plan_defaults({}, 'claude', 'pro')['windows']['five_hour']['hours'], 6)
        with patch.object(scorer_client, 'CLASSIFIER_CASES_PATH', self.bundled / 'classifier-cases.json'):
            self.assertEqual(scorer_client.load_classifier_cases()['route_definitions']['code_implementation'],
                             'fetched definition')
        with patch.object(auto_bundles, '__file__', str(self.bundled.parent / 'scripts' / 'auto_bundles.py')), \
                patch.object(auto_bundles, '_PUBLIC_CATALOG_CACHE', None):
            self.assertEqual(auto_bundles._public_catalog()['models']['gpt-6.2-sol']['label'], 'Fetched label')
        with patch.object(worker_sessions, 'SHIPPED_ACCOUNTS', self.bundled / 'accounts.json'):
            self.assertEqual(worker_sessions.shipped_scoring()['short_window_floor_pct'], 11)
            self.assertEqual(router_data.TUNING_DEFAULTS['short_window_floor_pct'], 11)

    def test_no_reader_opens_a_reference_file_except_through_the_accessor(self):
        """Construction: a reader gets reference content only from public_data.document (the held set), so it cannot mix
        sets. A line that opens a file of the set elsewhere at run time fails here."""
        allowed = {'public_data.py', 'reference_provenance.py', 'capability_catalog.py'}   # the accessor; maintainer checks
        names = '|'.join(re.escape(n) for n in public_data.FILES)
        opening = re.compile(rf"(?:{names}).*(?:read_text|read_bytes|open\(|json\.load\()|"
                             rf"(?:read_text|read_bytes|open\(|json\.load\().*(?:{names})")
        offenders = []
        for folder in (REPO / 'skills' / 'agent-compass' / 'scripts', REPO / 'tray', REPO / 'setup', REPO / 'agentcompass'):
            for path in folder.glob('*.py'):
                if path.name.startswith(('test_', 'conftest')) or path.name in allowed or path.name.endswith('_test_support.py'):
                    continue
                for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
                    if opening.search(line) and 'accounts-override' not in line and 'reference-accounts' not in line:
                        offenders.append(f'{path.name}:{number}: {line.strip()}')
        self.assertEqual(offenders, [])

    def test_the_users_own_settings_beat_the_fetched_set_and_are_never_written(self):
        override = Path(os.environ['AGENTCOMPASS_ACCOUNTS_OVERRIDE'])
        write_json(override, {'schema_version': 2, 'scoring': {'safety_margin_pct': 0.9}})
        local_scores = self.state / 'user-scores.json'
        write_json(local_scores, {'schema_version': 3, 'scores': {
            'grok-bot': {'coding': {'level': 1.5, 'as_of': '2026-11-18', 'kind': 'user'}}}})
        edit(self.remote_dir, 'user-scores.json', lambda d: d['scores'].setdefault('grok-bot', {}).__setitem__(
            'coding', {'level': 2.5, 'as_of': '2026-11-11', 'kind': 'maintainer'}))
        self.publish(at=T0 + timedelta(days=2))
        self.assertEqual(self.refresh(force=True, now=T0 + timedelta(days=3))['status'], 'updated')
        public_data.release()
        owned = {path: path.read_bytes() for path in (override, local_scores)}
        with patch.object(account_fleet, 'REFERENCES', self.bundled):
            self.assertEqual(account_fleet.load_accounts()['scoring']['safety_margin_pct'], 0.9)
        with patch.object(user_scores, 'PUBLIC_SCORES_FILE', self.bundled / 'user-scores.json'):
            listed = user_scores.list_scores(local_path=local_scores)
        self.assertEqual(listed['public']['grok-bot']['coding']['level'], 2.5)
        self.assertEqual(listed['effective']['grok-bot']['coding']['level'], 1.5)       # a local hand-written score wins
        self.assertEqual({path: path.read_bytes() for path in owned}, owned)
        written = {p.relative_to(self.state).parts[0] for p in self.state.rglob('*') if p.is_file()}
        self.assertEqual(written - {'accounts-override.json', 'user-scores.json'}, {'public-data'})


class FixtureServer:
    """A local HTTP fixture serving published sets from a directory; records the paths asked for."""

    def __init__(self, directory):
        requested = self.requested = []

        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                requested.append(self.path)
                super().do_GET()

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(directory)))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}/'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(10)


@pytest.mark.slow
class EndToEndTests(unittest.TestCase):
    """An install in an isolated home fetches a published set that adds one model from a local fixture; `plan` lists the
    model's bundle and `doctor` names the set. A wrong hash, a newer schema and sets this code cannot read are refused;
    the shipped data stays and `plan` behaves as with the channel off."""

    MODEL, BUNDLE, LIKE = 'fixture-model-high', 'fixture_high', 'gpt-6.2-sol'

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='public-data-e2e-'))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.references = self.root / 'references'
        copy_set(self.references)
        self.served = self.root / 'served'
        self.target = self.served / SCHEMA

        def add_model(d):
            d['models'][self.MODEL] = {**{k: v for k, v in copy.deepcopy(d['models'][self.LIKE]).items()
                                          if k != 'listed_as'}, 'label': 'Fixture model'}
            d['dispatch_efforts']['codex'][self.MODEL] = list(d['dispatch_efforts']['codex'][self.LIKE])
            d['accounts']['codex']['bundles'][self.BUNDLE] = {'model': self.MODEL, 'effort': 'high'}
        edit(self.references, 'accounts.json', add_model)
        edit(self.references, 'five-axis-scores.json',
             lambda d: d['models'].__setitem__(self.MODEL, copy.deepcopy(d['models'][self.LIKE])))
        public_data.publish(self.references, self.served, now=datetime.now(timezone.utc) + timedelta(minutes=1))
        self.fixture = FixtureServer(self.served)
        self.addCleanup(self.fixture.close)
        self.task = self.root / 'task.json'
        write_json(self.task, {'task': {
            'objective': 'Fix an off-by-one slice in parser.py and add a unit test.', 'work_kind': 'code_implementation',
            'expected_hours': 0.5, 'profile': {
                'work_kind': 'code_implementation', 'primary_level': 2, 'secondary_kind': 'none', 'secondary_level': 0,
                'access': 'write', 'local_only': True, 'needs_visual': False, 'wants_fast': False, 'expected_hours': 0.5,
                'task_count': '1', 'reason': 'bounded coding'}}})

    def run_cli(self, *args, channel='on'):
        env = {k: v for k, v in os.environ.items() if k.upper() in ('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'PATH')}
        env.update(HOME=str(self.home), USERPROFILE=str(self.home), ROUTER_STATE_DIR=str(self.home / '.agent-compass'),
                   LOCALAPPDATA=str(self.home / 'AppData' / 'Local'), APPDATA=str(self.home / 'AppData' / 'Roaming'),
                   ROUTER_PROVIDER_HOME=str(self.home), AGENTCOMPASS_PUBLIC_DATA=channel,
                   AGENTCOMPASS_PUBLIC_DATA_URL=self.fixture.url, AGENTCOMPASS_ANTIGRAVITY_PLAN='off',
                   AGENTCOMPASS_LIVE_MODELS='off', ROUTER_SCORE_CACHE='off', NO_PROXY='*', PYTHONUTF8='1',
                   AGENTCOMPASS_PUBLIC_DATA_INTERVAL_HOURS='1000')
        done = subprocess.run([sys.executable, '-X', 'utf8', str(REPO / 'agentcompass'), *map(str, args)], cwd=self.home,
                              env=env, capture_output=True, text=True, encoding='utf-8', timeout=180)
        print(f'$ agentcompass {" ".join(map(str, args))} -> {done.returncode}', file=sys.stderr)
        return done

    def plan(self, channel='on'):
        done = self.run_cli('plan', '--input', self.task, '--dry-run', channel=channel)
        return done.returncode, [c['id'] for c in json.loads(done.stdout).get('ranking') or []]

    def doctor_line(self):
        done = self.run_cli('doctor')
        self.assertEqual(done.returncode, 0, done.stderr[-800:])
        return next(line for line in done.stdout.splitlines() if line.startswith('Reference data:'))

    def refresh(self):
        done = self.run_cli('data', '--refresh')
        outcome = json.loads(done.stdout)
        print(json.dumps({k: outcome.get(k) for k in ('refresh', 'source', 'version', 'note')}), file=sys.stderr)
        self.assertEqual(done.returncode, 0, done.stderr[-800:])
        return outcome

    def test_a_published_model_reaches_an_install_and_is_routed(self):
        self.assertNotIn(self.BUNDLE, self.plan(channel='off')[1])               # the shipped set has no such model
        self.fixture.requested.clear()
        outcome = self.refresh()
        self.assertEqual((outcome['refresh']['status'], outcome['source']), ('updated', 'fetched'))
        self.assertEqual(sorted(self.fixture.requested), sorted(
            f'/{SCHEMA}/{name}' for name in (public_data.MANIFEST, 'accounts.json', 'five-axis-scores.json')))
        self.assertIn(self.BUNDLE, self.plan()[1])
        line = self.doctor_line()
        print(line, file=sys.stderr)
        self.assertIn(f"fetched {read_json(self.target / public_data.MANIFEST)['version']}", line)

    def test_a_wrong_hash_is_rejected_and_the_shipped_data_stays_in_effect(self):
        edit(self.target, public_data.MANIFEST, lambda d: d['files']['accounts.json'].__setitem__('sha256', '0' * 64))
        outcome = self.refresh()
        self.assertEqual((outcome['refresh']['status'], outcome['source']), ('failed', 'bundled'))
        self.assertIn('sha256', outcome['refresh']['error'])
        self.assertNotIn(self.BUNDLE, self.plan()[1])
        line = self.doctor_line()
        print(line, file=sys.stderr)
        self.assertTrue(line.startswith('Reference data: bundled'), line)

    def test_a_newer_schema_is_ignored_and_the_shipped_data_stays_in_effect(self):
        edit(self.target, public_data.MANIFEST, lambda d: d.__setitem__('schema_version', public_data.SCHEMA_VERSION + 1))
        self.fixture.requested.clear()
        outcome = self.refresh()
        self.assertEqual((outcome['refresh']['status'], outcome['source']), ('schema_too_new', 'bundled'))
        self.assertEqual(self.fixture.requested, [f'/{SCHEMA}/{public_data.MANIFEST}'])
        self.assertNotIn(self.BUNDLE, self.plan()[1])
        print(self.doctor_line(), file=sys.stderr)

    def test_sets_this_code_cannot_read_never_reach_plan(self):
        """The review's p2 sets, served as if published past the check: refused by the install's acceptance; plan as with
        the channel off."""
        baseline = self.plan(channel='off')
        for label in ('scoring_missing', 'bundle_str', 'drop_targets'):
            with self.subTest(label):
                shutil.rmtree(self.home / '.agent-compass', ignore_errors=True)
                shutil.rmtree(self.target)
                shutil.copytree(self.references, self.target)
                (self.target / public_data.MANIFEST).unlink()
                edit(self.target, *UNREADABLE[label])
                write_json(self.target / public_data.MANIFEST, public_data.build_manifest(
                    self.target, now=datetime.now(timezone.utc) + timedelta(minutes=1)))
                outcome = self.refresh()
                self.assertEqual((outcome['refresh']['status'], outcome['source']), ('rejected', 'bundled'))
                self.assertEqual(self.plan(), baseline)
                self.assertIn('not usable by this install', self.doctor_line())


if __name__ == '__main__':
    unittest.main()
