"""Published reference data channel: publication, fetch, verification, schema guard, fallback, interval and precedence.

Each test names the rule it holds (router-internals.md, Reference data). No request leaves the machine: the unit tests
fetch through FakeRemote, the end-to-end class through a local HTTP fixture on 127.0.0.1.
"""
from datetime import datetime, timedelta, timezone
import copy
import functools
import http.server
import importlib.util
import json
import os
from pathlib import Path
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

import pytest

import account_fleet
import auto_bundles
import axis_scores
import provider_inventory
import public_data
import scorer_client
import user_scores
from router_test_support import isolated_router_state

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
URL = 'https://example.invalid/refs/'
REPO = Path(public_data.__file__).resolve().parents[3]


class FakeRemote:
    """The published references, served by name; records every URL asked for."""

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


class ChannelCase(unittest.TestCase):
    def setUp(self):
        public_data.unpin()
        self.addCleanup(public_data.unpin)
        root = Path(tempfile.mkdtemp(prefix='public-data-'))
        self.addCleanup(shutil.rmtree, root, True)
        self.state = root / 'state'
        self.bundled = root / 'install' / 'references'
        self.remote_dir = root / 'remote'
        for folder in (self.bundled, self.remote_dir):
            copy_set(folder)
        write_json(self.bundled / public_data.MANIFEST, public_data.build_manifest(self.bundled, now=T0))
        self.env = {'ROUTER_STATE_DIR': str(self.state), 'AGENTCOMPASS_PUBLIC_DATA': 'on',
                    'AGENTCOMPASS_PUBLIC_DATA_URL': URL}
        self.publish(safety_margin_pct=0.5, at=T0 + timedelta(days=1))
        self.remote = FakeRemote(self.remote_dir)

    def publish(self, safety_margin_pct=None, at=None, schema=None):
        if safety_margin_pct is not None:
            accounts = read_json(self.remote_dir / 'accounts.json')
            accounts['scoring']['safety_margin_pct'] = safety_margin_pct
            write_json(self.remote_dir / 'accounts.json', accounts)
        manifest = public_data.build_manifest(self.remote_dir, now=at)
        if schema is not None:
            manifest['schema_version'] = schema
        write_json(self.remote_dir / public_data.MANIFEST, manifest)
        return manifest

    def refresh(self, **kwargs):
        kwargs.setdefault('now', T0 + timedelta(days=2))
        return public_data.refresh(environ=self.env, fetch=self.remote, bundled=self.bundled, **kwargs)

    def chosen(self):
        return public_data.selection(self.bundled, self.env)

    def effective_accounts(self):
        return read_json(public_data.effective(self.bundled / 'accounts.json', self.env))


class PublicationTests(unittest.TestCase):
    """Rule 2: publication is one act (`data publish`) and one check (`data publish --check`, the public-data workflow)."""

    def test_the_shipped_manifest_publishes_the_shipped_files(self):
        self.assertEqual(public_data.check_published(), '',
                         'references/public-data.json does not publish the files beside it: run `agentcompass data publish`')

    def test_an_isolated_interpreter_accepts_the_shipped_set(self):
        """`agentcompass data` runs public_data.py under python -I and doctor loads it by path; neither puts the script
        directory on sys.path, yet check_set must import its sibling readers and accept the shipped set."""
        code = (
            "import importlib.util, json, sys\n"
            "from pathlib import Path\n"
            "script = Path(sys.argv[1])\n"
            "sys.path = [p for p in sys.path if Path(p or '.').resolve() != script.parent.resolve()]\n"
            "spec = importlib.util.spec_from_file_location('public_data_isolated', script)\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "bundled = script.parent.parent / 'references'\n"
            "manifest = json.loads((bundled / 'public-data.json').read_text(encoding='utf-8'))\n"
            "status, detail = module.check_set(bundled, manifest)\n"
            "if status != 'ok':\n"
            "    raise SystemExit(f'{status}: {detail}')\n"
        )
        result = subprocess.run([sys.executable, '-I', '-c', code, str(Path(public_data.__file__).resolve())],
                                capture_output=True, text=True, check=False, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr[-800:] or result.stdout[-800:])

    def test_published_at_moves_only_when_the_content_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for name in public_data.FILES:
                write_json(folder / name, {'name': name})
            first = public_data.build_manifest(folder, now=T0)
            self.assertEqual(public_data.build_manifest(folder, first, now=T0 + timedelta(days=3)), first)
            write_json(folder / 'accounts.json', {'name': 'changed'})
            second = public_data.build_manifest(folder, first, now=T0 + timedelta(days=3))
            self.assertNotEqual(second['version'], first['version'])
            self.assertEqual(second['published_at'], '2026-11-04T00:00:00Z')

    def test_an_edit_without_publication_fails_the_check_and_publishing_fixes_it(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            copy_set(folder)
            public_data.publish(folder, now=T0)
            self.assertEqual(public_data.check_published(folder), '')
            accounts = read_json(folder / 'accounts.json')
            accounts['scoring']['safety_margin_pct'] = 0.41
            write_json(folder / 'accounts.json', accounts)
            problem = public_data.check_published(folder)
            self.assertIn('accounts.json changed', problem)
            self.assertIn('agentcompass data publish', problem)
            fresh = public_data.publish(folder, now=T0 + timedelta(days=1))
            self.assertEqual((public_data.check_published(folder), fresh['published_at']), ('', '2026-11-02T00:00:00Z'))

    def test_publish_refuses_a_set_this_code_cannot_read_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            copy_set(folder)
            five = read_json(folder / 'five-axis-scores.json')
            five['schema'] = 'agent-compass.five-axis-scores/5'
            write_json(folder / 'five-axis-scores.json', five)
            with self.assertRaisesRegex(ValueError, 'five-axis-scores'):
                public_data.publish(folder, now=T0)
            self.assertFalse((folder / public_data.MANIFEST).exists())


class FetchTests(ChannelCase):
    def test_a_verified_set_published_after_the_shipped_copy_is_read_instead_of_it(self):
        """Rule 3 (complete): every file verifies, every schema is read, published after the shipped copy."""
        self.assertEqual(self.chosen()['source'], 'bundled')
        outcome = self.refresh()
        self.assertEqual(outcome['status'], 'updated', outcome)
        self.assertEqual(outcome['downloaded'], ['accounts.json'])  # unchanged files are copied from the install
        chosen = self.chosen()
        self.assertEqual((chosen['source'], chosen['published_at']), ('fetched', '2026-11-02T00:00:00Z'))
        self.assertEqual(self.effective_accounts()['scoring']['safety_margin_pct'], 0.5)
        state = public_data.read_state(self.env)
        self.assertEqual((state['fetched_at'], state['last_error']), ('2026-11-03T00:00:00Z', None))
        self.assertTrue(all(call.startswith(URL) for call in self.remote.calls))

    def test_a_hash_mismatch_applies_nothing(self):
        """Rule 3 (complete or absent)."""
        self.remote.overrides['accounts.json'] = b'{"scoring": {"safety_margin_pct": 9}}\n'
        outcome = self.refresh()
        self.assertEqual(outcome['status'], 'failed')
        self.assertIn('sha256', outcome['error'])
        self.assertEqual(self.chosen()['source'], 'bundled')
        self.assertNotEqual(self.effective_accounts()['scoring']['safety_margin_pct'], 9)
        self.assertEqual(list((self.state / 'public-data' / 'sets').iterdir()), [])
        self.assertIn('sha256', public_data.read_state(self.env)['last_error'])

    def test_a_fetched_file_that_no_longer_matches_on_disk_is_not_read(self):
        self.refresh()
        version = public_data.read_state(self.env)['version']
        (self.state / 'public-data' / 'sets' / version / 'accounts.json').write_text('{}', encoding='utf-8')
        chosen = self.chosen()
        self.assertEqual(chosen['source'], 'bundled')
        self.assertIn('not usable', chosen['note'])

    def test_a_newer_data_schema_is_noted_and_ignored_after_one_request(self):
        """Rule 3 (schema guard): a newer schema is noted and ignored; only the manifest is requested."""
        self.refresh()
        self.publish(safety_margin_pct=0.7, at=T0 + timedelta(days=3), schema=public_data.SCHEMA_VERSION + 1)
        self.remote.calls.clear()
        outcome = self.refresh(force=True, now=T0 + timedelta(days=4))
        self.assertEqual(outcome['status'], 'schema_too_new')
        self.assertEqual(self.remote.calls, [URL + public_data.MANIFEST])
        self.assertEqual(self.effective_accounts()['scoring']['safety_margin_pct'], 0.5)
        self.assertIn(public_data.UPDATE_APP, public_data.status(self.env, self.bundled)['note'])

    def test_an_older_data_schema_is_noted_without_downloading(self):
        self.refresh()
        self.publish(safety_margin_pct=0.7, at=T0 + timedelta(days=3), schema=public_data.MIN_SCHEMA_VERSION - 1)
        self.remote.calls.clear()
        outcome = self.refresh(force=True, now=T0 + timedelta(days=4))
        self.assertEqual(outcome['status'], 'schema_too_old')
        self.assertEqual(self.remote.calls, [URL + public_data.MANIFEST])
        self.assertEqual(self.effective_accounts()['scoring']['safety_margin_pct'], 0.5)
        state = public_data.read_state(self.env)
        self.assertIn('nothing to run', state['note'])
        self.assertIsNone(state['last_error'])

    def test_a_file_of_a_schema_this_code_does_not_read_rejects_the_whole_set(self):
        """Rule 3 (every schema is one this code reads): the data schema matches, but one file is of another schema."""
        for name, change in (('five-axis-scores.json', lambda d: {**d, 'schema': 'agent-compass.five-axis-scores/5'}),
                             ('user-scores.json', lambda d: {**d, 'schema_version': 2})):
            with self.subTest(name=name):
                public_data.unpin()
                copy_set(self.remote_dir)
                write_json(self.remote_dir / name, change(read_json(self.remote_dir / name)))
                self.publish(safety_margin_pct=0.6, at=T0 + timedelta(days=3))
                outcome = self.refresh(force=True, now=T0 + timedelta(days=4))
                self.assertEqual(outcome['status'], 'rejected', outcome)
                self.assertIn(name.split('.')[0], outcome['detail'])
                self.assertNotEqual(self.effective_accounts()['scoring']['safety_margin_pct'], 0.6)

    def test_a_set_this_install_rejects_is_named_once_and_never_downloaded_again(self):
        """Rule 3: a rejected set is noted once per published version; an updated reader looks again."""
        self.refresh()
        accounts = read_json(self.remote_dir / 'accounts.json')
        del accounts['families']
        write_json(self.remote_dir / 'accounts.json', accounts)
        self.publish(at=T0 + timedelta(days=3))
        outcome = self.refresh(force=True, now=T0 + timedelta(days=4))
        self.assertEqual(outcome['status'], 'rejected', outcome)
        self.assertIn('families', outcome['detail'])
        self.assertEqual(self.effective_accounts()['scoring']['safety_margin_pct'], 0.5)
        self.assertIn(public_data.UPDATE_APP, public_data.status(self.env, self.bundled)['note'])
        self.remote.calls.clear()
        again = self.refresh(force=True, now=T0 + timedelta(days=5))
        self.assertEqual(again['status'], 'rejected')
        self.assertEqual(self.remote.calls, [URL + public_data.MANIFEST])
        self.remote.calls.clear()
        with patch.object(public_data, 'reader_id', return_value='updated-reader'):
            self.refresh(force=True, now=T0 + timedelta(days=6))
        self.assertIn(URL + 'accounts.json', self.remote.calls)

    def test_offline_the_last_accepted_set_then_the_shipped_copy_is_read(self):
        """Rule 5: offline installs run on what they have."""
        self.refresh()
        self.remote.offline = True
        outcome = self.refresh(force=True, now=T0 + timedelta(days=3))
        self.assertEqual(outcome['status'], 'failed')
        self.assertEqual(self.effective_accounts()['scoring']['safety_margin_pct'], 0.5)
        shutil.rmtree(self.state / 'public-data' / 'sets')
        self.assertEqual(self.chosen()['source'], 'bundled')
        self.assertEqual(self.effective_accounts(), read_json(self.bundled / 'accounts.json'))

    def test_the_check_interval_and_retry_hold_and_an_unchanged_manifest_costs_one_request(self):
        """Rule 5: at most one check per interval (30 minutes after a failure); an unchanged set costs one request."""
        self.refresh(now=T0 + timedelta(days=2))
        calls = len(self.remote.calls)
        self.assertEqual(self.refresh(now=T0 + timedelta(days=2, hours=5))['status'], 'not_due')
        self.assertEqual(len(self.remote.calls), calls)
        self.assertEqual(self.refresh(now=T0 + timedelta(days=2, hours=7))['status'], 'unchanged')
        self.assertEqual(self.remote.calls[calls:], [URL + public_data.MANIFEST])
        self.remote.offline = True
        self.assertEqual(self.refresh(now=T0 + timedelta(days=3))['status'], 'failed')
        self.assertEqual(self.refresh(now=T0 + timedelta(days=3, minutes=10))['status'], 'not_due')
        self.assertEqual(self.refresh(now=T0 + timedelta(days=3, minutes=40))['status'], 'failed')

    def test_a_fetched_set_not_newer_than_the_shipped_copy_is_not_read(self):
        """Rule 3: after a product update, a fetched set older than the newly shipped copy is not used."""
        self.assertEqual(self.refresh()['status'], 'updated')
        self.assertEqual(self.chosen()['source'], 'fetched')
        shipped = read_json(self.bundled / public_data.MANIFEST)          # the update ships a set published later
        write_json(self.bundled / public_data.MANIFEST,
                   {**shipped, 'published_at': (T0 + timedelta(days=5)).strftime('%Y-%m-%dT%H:%M:%SZ')})
        chosen = self.chosen()
        self.assertEqual(chosen['source'], 'bundled')
        self.assertIn('as new', chosen['note'])

    def test_shipped_references_with_local_edits_are_used_as_they_are(self):
        """Rule 5: a checkout whose shipped files differ from their manifest uses its own files."""
        self.refresh()
        accounts = read_json(self.bundled / 'accounts.json')
        accounts['scoring']['safety_margin_pct'] = 0.123
        write_json(self.bundled / 'accounts.json', accounts)
        chosen = self.chosen()
        self.assertEqual(chosen['source'], 'bundled')
        self.assertIn('local edits', chosen['note'])
        self.assertEqual(self.effective_accounts()['scoring']['safety_margin_pct'], 0.123)

    def test_a_set_an_older_release_fetched_is_ignored_and_kept(self):
        """Rule 7: a set v0.6.x fetched (data schema 6) is ignored by the schema guard, not deleted."""
        old = self.state / 'public-data' / 'sets' / '0123456789ab'
        copy_set(old)
        manifest = {**public_data.build_manifest(old, now=T0 + timedelta(days=9)), 'schema_version': 6}
        manifest['version'] = public_data.set_version(manifest)
        write_json(old / public_data.MANIFEST, manifest)
        write_json(self.state / 'public-data' / 'state.json', {'version': manifest['version']})
        old.rename(old.with_name(manifest['version']))
        chosen = self.chosen()
        self.assertEqual(chosen['source'], 'bundled')
        self.assertIn('schema 6', chosen['note'])
        self.assertTrue((self.state / 'public-data' / 'sets' / manifest['version'] / 'accounts.json').is_file())


class SwitchAndBoundTests(ChannelCase):
    def test_off_makes_no_network_call_and_reads_the_shipped_copy(self):
        """Rule 5: the off switch (environment)."""
        self.refresh()
        self.remote.calls.clear()
        env = {**self.env, 'AGENTCOMPASS_PUBLIC_DATA': 'off'}
        self.assertEqual(public_data.refresh(force=True, environ=env, fetch=self.remote)['status'], 'disabled')
        with patch.object(public_data, 'http_get', side_effect=AssertionError('network')):
            self.assertIsNone(public_data.refresh_in_background(environ=env, bundled=self.bundled))
        public_data.unpin()
        self.assertEqual(public_data.selection(self.bundled, env)['source'], 'bundled')
        self.assertEqual(self.remote.calls, [])

    def test_the_state_file_switches_the_channel_off_and_the_environment_wins(self):
        """Rule 5: the off switch (state file)."""
        env = {k: v for k, v in self.env.items() if k != 'AGENTCOMPASS_PUBLIC_DATA'}
        self.state.mkdir(parents=True, exist_ok=True)
        write_json(self.state / public_data.CONFIG_FILE, {'enabled': False, 'interval_hours': 1})
        cfg = public_data.config(env)
        self.assertEqual((cfg['enabled'], cfg['switch'], cfg['interval_hours']), (False, 'public-data-config.json', 1.0))
        self.assertEqual(public_data.refresh(environ=env, fetch=self.remote)['status'], 'disabled')
        self.assertEqual(self.remote.calls, [])
        self.assertTrue(public_data.config({**env, 'AGENTCOMPASS_PUBLIC_DATA': '1'})['enabled'])

    def test_a_background_check_pins_the_set_this_process_started_with(self):
        with patch.object(public_data, 'http_get', self.remote):
            thread = public_data.refresh_in_background(environ=self.env, bundled=self.bundled)
            thread.join(10)
        self.assertEqual(public_data.read_state(self.env)['published_at'], '2026-11-02T00:00:00Z')
        self.assertEqual(self.chosen()['source'], 'bundled')  # pinned before the check landed
        public_data.unpin()
        self.assertEqual(self.chosen()['source'], 'fetched')

    def test_a_hanging_network_never_delays_the_caller(self):
        """Rule 5: the routing thread never waits on the check."""
        release = threading.Event()

        def hang(url, timeout):
            release.wait(30)
            raise urllib.error.URLError('released')

        self.addCleanup(release.set)
        with patch.object(public_data, 'http_get', hang):
            started = time.monotonic()
            thread = public_data.refresh_in_background(environ=self.env, bundled=self.bundled)
            elapsed = time.monotonic() - started
        self.assertIsNotNone(thread)
        self.assertLess(elapsed, 1.0)
        release.set()
        thread.join(10)

    def test_a_process_whose_check_hangs_exits_within_the_stated_bound(self):
        """Rule 5: process exit waits at most EXIT_GRACE_SECONDS for a background check (a server that never answers)."""
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(8)
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        # The request itself would wait far longer than the grace, so only the grace can end the process in time.
        code = ('import sys, time; sys.path.insert(0, sys.argv[1]); import public_data; '
                'slow = public_data.http_get; public_data.http_get = lambda url, timeout=None: slow(url, 60); '
                'started = time.monotonic(); public_data.refresh_in_background(); '
                'print(round(time.monotonic() - started, 3))')
        env = {**os.environ, **self.env, 'AGENTCOMPASS_PUBLIC_DATA_URL': f'http://127.0.0.1:{port}/',
               'NO_PROXY': '*', 'HTTP_PROXY': '', 'HTTPS_PROXY': ''}
        started = time.monotonic()
        done = subprocess.run([sys.executable, '-c', code, str(Path(public_data.__file__).parent)], env=env,
                              capture_output=True, text=True, timeout=60)
        wall = time.monotonic() - started
        self.assertEqual(done.returncode, 0, done.stderr[-800:])
        self.assertLess(float(done.stdout.strip()), 1.0)                      # the caller went on at once
        self.assertLess(wall, public_data.EXIT_GRACE_SECONDS + 8)              # interpreter start + import + the grace

    def test_the_published_location_comes_from_origin_and_a_request_carries_no_credentials(self):
        """Rule 6: coordinates from agentcompass/origin.py; plain GET with no credential or personal header."""
        spec = importlib.util.spec_from_file_location('origin_under_test', REPO / 'agentcompass' / 'origin.py')
        origin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(origin)
        env = {k: v for k, v in self.env.items() if k != 'AGENTCOMPASS_PUBLIC_DATA_URL'}
        self.assertEqual(public_data.config(env)['url'], origin.PUBLIC_DATA_URL)
        self.assertTrue(origin.PUBLIC_DATA_URL.startswith('https://raw.githubusercontent.com/'))
        seen = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, size):
                return b'{}'

        def urlopen(request, timeout):
            seen.append(request)
            return Response()

        with patch.object(public_data.urllib.request, 'urlopen', urlopen):
            public_data.http_get(origin.PUBLIC_DATA_URL + public_data.MANIFEST)
        self.assertEqual({k.lower() for k in seen[0].headers}, {'user-agent', 'cache-control'})
        self.assertEqual(seen[0].get_method(), 'GET')


class LoaderPrecedenceTests(ChannelCase):
    """Rule 4 and rule 7: every run-time reader reads through the one accessor; shipped < fetched < the user's own."""

    def setUp(self):
        super().setUp()
        isolated_router_state(self.addCleanup)
        os.environ.pop('ROUTER_ACCOUNTS_FILE', None)
        os.environ.update(self.env)
        self.env = {**self.env, 'ROUTER_STATE_DIR': os.environ['ROUTER_STATE_DIR']}
        self.state = Path(self.env['ROUTER_STATE_DIR'])
        self.assertEqual(self.refresh()['status'], 'updated')
        public_data.unpin()

    def test_every_run_time_reader_reads_the_fetched_set(self):
        templates = read_json(self.remote_dir / 'plan-templates.json')
        templates['claude']['pro']['windows']['five_hour']['hours'] = 6
        write_json(self.remote_dir / 'plan-templates.json', templates)
        cases = read_json(self.remote_dir / 'classifier-cases.json')
        cases['calibration_cases'] = [{'marker': 'fetched'}]
        write_json(self.remote_dir / 'classifier-cases.json', cases)
        five = read_json(self.remote_dir / 'five-axis-scores.json')
        five['generated_at'] = '2026-11-12T00:00:00Z'
        write_json(self.remote_dir / 'five-axis-scores.json', five)
        scores = read_json(self.remote_dir / 'user-scores.json')
        scores['scores'].setdefault('grok-bot', {})['coding'] = {'level': 2.0, 'as_of': '2026-11-11', 'kind': 'maintainer'}
        write_json(self.remote_dir / 'user-scores.json', scores)
        accounts = read_json(self.remote_dir / 'accounts.json')
        accounts['models']['fixture-model-high'] = {'label': 'Fixture'}
        write_json(self.remote_dir / 'accounts.json', accounts)
        self.publish(at=T0 + timedelta(days=2))
        self.assertEqual(self.refresh(force=True, now=T0 + timedelta(days=3))['status'], 'updated')
        with patch.object(account_fleet, 'REFERENCES', self.bundled):
            self.assertEqual(account_fleet.fleet_document_path().parent.parent.name, 'sets')
            self.assertEqual(axis_scores.load_five_axis()['generated_at'], '2026-11-12T00:00:00Z')
        with patch.object(user_scores, 'PUBLIC_SCORES_FILE', self.bundled / 'user-scores.json'):
            self.assertEqual(user_scores.load_public()['scores']['grok-bot']['coding']['level'], 2.0)
            self.assertEqual(user_scores.public_scores_path(), self.bundled / 'user-scores.json')   # `--public` writes the shipped file
        with patch.object(provider_inventory, 'PLAN_TEMPLATES', self.bundled / 'plan-templates.json'), \
                patch.object(provider_inventory, '_SHIPPED_PLAN_TEMPLATES', None):
            self.assertEqual(provider_inventory.plan_defaults({}, 'claude', 'pro')['windows']['five_hour']['hours'], 6)
        with patch.object(scorer_client, 'CLASSIFIER_CASES_PATH', self.bundled / 'classifier-cases.json'):
            self.assertEqual(scorer_client.load_classifier_cases()['calibration_cases'], [{'marker': 'fetched'}])
        with patch.object(auto_bundles, '__file__', str(self.bundled.parent / 'scripts' / 'auto_bundles.py')), \
                patch.object(auto_bundles, '_PUBLIC_CATALOG_CACHE', None):
            self.assertIn('fixture-model-high', auto_bundles._public_catalog()['models'])

    def test_the_users_own_settings_beat_the_fetched_set_and_are_never_written(self):
        override = Path(os.environ['AGENTCOMPASS_ACCOUNTS_OVERRIDE'])
        write_json(override, {'schema_version': 2, 'scoring': {'safety_margin_pct': 0.9}})
        local_scores = self.state / 'user-scores.json'
        write_json(local_scores, {'schema_version': 3, 'scores': {
            'grok-bot': {'coding': {'level': 1.5, 'as_of': '2026-11-18', 'kind': 'user'}}}})
        scores = read_json(self.remote_dir / 'user-scores.json')
        scores['scores'].setdefault('grok-bot', {})['coding'] = {'level': 2.5, 'as_of': '2026-11-11', 'kind': 'maintainer'}
        write_json(self.remote_dir / 'user-scores.json', scores)
        self.publish(at=T0 + timedelta(days=2))
        self.assertEqual(self.refresh(force=True, now=T0 + timedelta(days=3))['status'], 'updated')
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
    """A local HTTP fixture serving a published set from a directory; records the paths asked for."""

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
    model's bundle and `doctor` names the set. A wrong hash is rejected and a newer schema ignored: shipped data stays."""

    MODEL, BUNDLE, LIKE = 'fixture-model-high', 'fixture_high', 'gpt-6.2-sol'

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='public-data-e2e-'))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.served = self.root / 'served'
        copy_set(self.served)
        accounts = read_json(self.served / 'accounts.json')
        accounts['models'][self.MODEL] = {**{k: v for k, v in copy.deepcopy(accounts['models'][self.LIKE]).items()
                                              if k != 'listed_as'}, 'label': 'Fixture model'}
        accounts['dispatch_efforts']['codex'][self.MODEL] = list(accounts['dispatch_efforts']['codex'][self.LIKE])
        accounts['accounts']['codex']['bundles'][self.BUNDLE] = {'model': self.MODEL, 'effort': 'high'}
        write_json(self.served / 'accounts.json', accounts)
        five = read_json(self.served / 'five-axis-scores.json')
        five['models'][self.MODEL] = copy.deepcopy(five['models'][self.LIKE])
        write_json(self.served / 'five-axis-scores.json', five)
        public_data.publish(self.served, now=datetime.now(timezone.utc) + timedelta(minutes=1))
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
                   AGENTCOMPASS_LIVE_MODELS='off', ROUTER_SCORE_CACHE='off', NO_PROXY='*', PYTHONUTF8='1')
        done = subprocess.run([sys.executable, '-X', 'utf8', str(REPO / 'agentcompass'), *map(str, args)], cwd=self.home,
                              env=env, capture_output=True, text=True, encoding='utf-8', timeout=180)
        print(f'$ agentcompass {" ".join(map(str, args))} -> {done.returncode}', file=sys.stderr)
        return done

    def ranking_ids(self, channel='on'):
        done = self.run_cli('plan', '--input', self.task, '--dry-run', channel=channel)
        return [c['id'] for c in json.loads(done.stdout).get('ranking') or []]

    def doctor_line(self):
        done = self.run_cli('doctor')
        self.assertEqual(done.returncode, 0, done.stderr[-800:])
        return next(line for line in done.stdout.splitlines() if line.startswith('Reference data:'))

    def test_a_published_model_reaches_an_install_and_is_routed(self):
        self.assertNotIn(self.BUNDLE, self.ranking_ids(channel='off'))  # the shipped set has no such model
        self.fixture.requested.clear()
        refreshed = self.run_cli('data', '--refresh')
        outcome = json.loads(refreshed.stdout)
        print(json.dumps({k: outcome[k] for k in ('refresh', 'source', 'version', 'published_at')}), file=sys.stderr)
        self.assertEqual((refreshed.returncode, outcome['refresh']['status'], outcome['source']), (0, 'updated', 'fetched'))
        self.assertEqual(sorted(self.fixture.requested),
                         sorted(['/' + public_data.MANIFEST, '/accounts.json', '/five-axis-scores.json']))
        self.assertIn(self.BUNDLE, self.ranking_ids())
        line = self.doctor_line()
        print(line, file=sys.stderr)
        self.assertIn(f"fetched {read_json(self.served / public_data.MANIFEST)['version']}", line)

    def test_a_wrong_hash_is_rejected_and_the_shipped_data_stays_in_effect(self):
        manifest = read_json(self.served / public_data.MANIFEST)
        manifest['files']['accounts.json']['sha256'] = '0' * 64
        write_json(self.served / public_data.MANIFEST, manifest)
        refreshed = self.run_cli('data', '--refresh')
        outcome = json.loads(refreshed.stdout)
        print(json.dumps({k: outcome[k] for k in ('refresh', 'source', 'version')}), file=sys.stderr)
        self.assertEqual((refreshed.returncode, outcome['refresh']['status'], outcome['source']), (0, 'failed', 'bundled'))
        self.assertIn('sha256', outcome['refresh']['error'])
        self.assertNotIn(self.BUNDLE, self.ranking_ids())
        line = self.doctor_line()
        print(line, file=sys.stderr)
        self.assertTrue(line.startswith('Reference data: bundled'), line)

    def test_a_newer_schema_is_ignored_and_the_shipped_data_stays_in_effect(self):
        manifest = read_json(self.served / public_data.MANIFEST)
        write_json(self.served / public_data.MANIFEST, {**manifest, 'schema_version': public_data.SCHEMA_VERSION + 1})
        self.fixture.requested.clear()
        refreshed = self.run_cli('data', '--refresh')
        outcome = json.loads(refreshed.stdout)
        print(json.dumps({k: outcome[k] for k in ('refresh', 'source', 'note')}), file=sys.stderr)
        self.assertEqual((refreshed.returncode, outcome['refresh']['status'], outcome['source']),
                         (0, 'schema_too_new', 'bundled'))
        self.assertEqual(self.fixture.requested, ['/' + public_data.MANIFEST])
        self.assertNotIn(self.BUNDLE, self.ranking_ids())
        print(self.doctor_line(), file=sys.stderr)


if __name__ == '__main__':
    unittest.main()
