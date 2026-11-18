"""Published reference data channel: publication, acceptance, fetch, the set a process holds, fall-back, interval, launch
values and precedence.

Each test names the rule it holds (router-internals.md, Reference data). No request leaves the machine: the unit tests
fetch through FakeRemote or a file:// location, the end-to-end class through a local HTTP fixture on 127.0.0.1.
"""
from datetime import datetime, timedelta, timezone
import argparse
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
import sqlite3
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
    # review round 2, r2_sets: Infinity passed publish and the install, then every real plan failed on its JSON output
    'infinity': ('five-axis-scores.json', lambda d: d['models'][next(iter(d['models']))].__setitem__('probe', float('inf'))),
    'nan': ('accounts.json', lambda d: d['scoring'].__setitem__('safety_margin_pct', float('nan'))),
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
                write_json(self.target / public_data.MANIFEST, unchecked_manifest(self.target))
                self.assertRegex(public_data.check_published(self.root, self.references), 'does not accept|is not readable')
                shutil.rmtree(self.target)


def unchecked_manifest(directory, at=T0):
    """A manifest over the files in ``directory`` as they are (build_manifest refuses a file it cannot parse)."""
    files = {n: {'sha256': public_data.sha256_bytes(b), 'bytes': len(b)} for n in public_data.FILES
             for b in [(Path(directory) / n).read_bytes()]}
    manifest = {'schema_version': public_data.SCHEMA_VERSION, 'files': files}
    return {**manifest, 'version': public_data.set_version(manifest), 'published_at': public_data._stamp(at.replace(microsecond=0))}


class PublicationHistoryTests(unittest.TestCase):
    """Rule (finding 7): the check against the previous publication runs or fails, never passes by default, and a published
    set of an older schema is frozen."""

    def test_an_unknown_since_revision_fails_the_check_saying_why(self):
        with self.assertRaises(ValueError) as refused:
            public_data.manifest_at('no-such-revision-0123')
        self.assertIn('not a commit of this repository', str(refused.exception))
        import io
        err = io.StringIO()
        with patch.object(sys, 'stderr', err):
            self.assertEqual(public_data._publish_main(['--check', '--since', 'no-such-revision-0123']), 1)
        self.assertIn('not a commit of this repository', err.getvalue())

    def test_a_base_commit_missing_from_the_checkout_fails_the_check_naming_it_and_what_to_run(self):
        """Note (round 5): after a force push to main the workflow's base (github.event.before) is not in the checkout;
        the check still fails, and says that it is the base commit that is missing, not bad data, and what to run."""
        gone = 'f' * 40
        with self.assertRaises(ValueError) as refused:
            public_data.manifest_at(gone)
        message = str(refused.exception)
        for part in ('base commit is not in this checkout', 'force push', 'says nothing about the published data',
                     f'git fetch origin {gone}', 'agentcompass data publish --check --since'):
            self.assertIn(part, message)
        import io
        err = io.StringIO()
        with patch.object(sys, 'stderr', err):
            self.assertEqual(public_data._publish_main(['--check', '--since', gone]), 1)       # still fails closed
        self.assertIn('base commit is not in this checkout', err.getvalue())

    def test_a_published_set_of_an_older_schema_is_frozen(self):
        root = Path(tempfile.mkdtemp(prefix='public-data-frozen-'))
        self.addCleanup(shutil.rmtree, root, True)
        old = root / 'published' / str(public_data.SCHEMA_VERSION - 1)
        old.mkdir(parents=True)
        (old / 'accounts.json').write_text('{"accounts": {}}\n', encoding='utf-8', newline='\n')
        write_json(old / public_data.MANIFEST, unchecked_manifest_of(old, ['accounts.json']))
        self.assertEqual(public_data.check_frozen(root / 'published'), '')
        git = ['git', '-c', 'user.name=probe', '-c', 'user.email=probe@example.invalid', '-c', 'core.autocrlf=false']
        for command in (['init', '-q'], ['add', '-A'], ['commit', '-q', '-m', 'base']):
            subprocess.run(git + command, cwd=root, check=True, capture_output=True, timeout=60)
        self.assertEqual(public_data.check_frozen(root / 'published', since='HEAD'), '')
        (old / 'accounts.json').write_text('{"accounts": {"x": {}}}\n', encoding='utf-8', newline='\n')
        self.assertIn('does not match its manifest', public_data.check_frozen(root / 'published'))
        write_json(old / public_data.MANIFEST, unchecked_manifest_of(old, ['accounts.json']))   # edited consistently
        self.assertEqual(public_data.check_frozen(root / 'published'), '')
        self.assertIn('changed since HEAD', public_data.check_frozen(root / 'published', since='HEAD'))


def unchecked_manifest_of(directory, names):
    return {'files': {n: {'sha256': public_data.sha256_bytes((directory / n).read_bytes())} for n in names}}


class AcceptanceTests(unittest.TestCase):
    """Rule: a set is accepted only when this code's own readers load it and an offline plan completes with it; each
    refusal names the step. The candidate is read, never the set the process holds."""

    def test_each_shape_a_reader_cannot_read_is_refused_by_name(self):
        launch = 'accounts.json launch values'
        steps = {'scoring_missing': ('scoring', 'rotate_after_carried_tokens'), 'bundle_str': (launch, 'not an object'),
                 'drop_targets': ('plan on the reference fleet', ''), 'scoring_type': ('plan on the fleet of the set', ''),
                 'new_family': (launch, "family 'newcli'"), 'new_harness': (launch, "harness 'newcli'"),
                 'unknown_effort': (launch, "effort 'turbo'"), 'injected_model': (launch, 'does not match'),
                 'infinity': ('', 'Infinity, which is not a finite number'),
                 'nan': ('', 'NaN, which is not a finite number')}
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

    def test_every_reference_file_is_parsed_strictly_wherever_it_is_read(self):
        """Rule (finding 3): a non-finite number is a named error, not a value, in a shipped or a fetched file."""
        for text in ('{"a": NaN}', '{"a": -Infinity}', '{"a": 1e999}'):
            with self.subTest(text), self.assertRaises(public_data.ReferenceDataError) as refused:
                public_data.parse_document(text.encode(), 'accounts.json')
            self.assertIn('accounts.json', str(refused.exception))
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            copy_set(folder)
            data = (folder / 'accounts.json').read_text(encoding='utf-8').replace('"safety_margin_pct": 5', '"safety_margin_pct": 1e999', 1)
            self.assertIn('1e999', data)
            (folder / 'accounts.json').write_text(data, encoding='utf-8', newline='\n')
            with self.assertRaises(public_data.ReferenceDataError):
                public_data.document(folder / 'accounts.json', {'AGENTCOMPASS_PUBLIC_DATA': 'off'})

    def test_the_plans_are_serialized_as_the_cli_prints_them(self):
        """Rule (finding 3): the acceptance serializes each offline plan exactly as the CLI would (route_judge.render)."""
        import demo_plan
        real = demo_plan.plan
        with patch.object(demo_plan, 'plan', lambda *a, **k: {**real(*a, **k), 'probe': float('inf')}), \
                self.assertRaises(ValueError) as refused:
            public_data.accept(public_data.BUNDLED)
        self.assertTrue(str(refused.exception).startswith('plan on the reference fleet'), refused.exception)

    def test_the_plan_on_the_set_fleet_must_have_a_selectable_candidate(self):
        """Rule (finding 3): the second plan proves something only when it can select; a set none of whose bundles can be
        selected on its own priced fleet is refused at that step."""
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            copy_set(folder)
            edit(folder, 'accounts.json', lambda d: [b.__setitem__('disabled', True) for s in d['accounts'].values()
                                                     for b in (s.get('bundles') or {}).values() if isinstance(b, dict)])
            with self.assertRaises(ValueError) as refused:
                public_data.accept(folder)
        self.assertTrue(str(refused.exception).startswith('plan on the fleet of the set'), refused.exception)
        self.assertIn('no selectable candidate', str(refused.exception))

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
        manifest = unchecked_manifest(self.remote_dir, at or datetime.now(timezone.utc))
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

    def test_a_held_set_of_another_schema_is_removed_where_it_is_read(self):
        """Rule (derived state is rebuilt or skipped): a fetched set an earlier version's channel left (v0.6.8 wrote
        schema 6 under the same names) is removed where it is read, state and files; doctor says the shipped set is in
        effect, and the next check is due at once."""
        for recorded in (6, None):                       # as state.json records it, or only as its manifest declares
            with self.subTest(recorded=recorded):
                sets = self.state / 'public-data' / 'sets'
                for version in ('a0680001', 'a0670001'):
                    copy_set(sets / version)
                    write_json(sets / version / public_data.MANIFEST,
                               {**unchecked_manifest(sets / version, T0 + timedelta(days=5)), 'schema_version': 6})
                state = {'version': 'a0680001', 'published_at': public_data._stamp(T0 + timedelta(days=5)),
                         'fetched_at': public_data._stamp(T0 + timedelta(days=1)), 'checked_at': public_data._stamp(T0 + timedelta(days=2)),
                         'last_error': None, 'note': None, 'url': f'{URL}6/'}
                if recorded is not None:
                    state['schema_version'] = recorded
                write_json(self.state / 'public-data' / 'state.json', state)
                chosen = self.held()
                self.assertEqual(chosen['source'], 'bundled')
                self.assertIn('data schema 6', chosen['note'])
                self.assertIn('was removed', public_data.status(self.env, self.bundled)['note'])
                self.assertEqual(list(sets.iterdir()), [])
                self.assertFalse(set(public_data.read_state(self.env)) & {'version', 'schema_version', 'checked_at', 'url'})
                self.assertEqual(self.held()['note'], 'nothing fetched yet')              # the next process: no stale note
                self.assertEqual(self.refresh()['status'], 'updated')                      # not waiting out the old check
                self.assertEqual(self.held()['source'], 'fetched')
                shutil.rmtree(self.state)

    def test_a_fetched_set_changed_on_disk_after_its_acceptance_is_not_held(self):
        """Rule: a process takes a fetched set only when every file matches its manifest (review round 2, M5n)."""
        self.assertEqual(self.refresh()['status'], 'updated')
        version = public_data.read_state(self.env)['version']
        edit(self.state / 'public-data' / 'sets' / version, 'accounts.json',
             lambda d: d['scoring'].__setitem__('safety_margin_pct', 0.7))
        chosen = self.held()
        self.assertEqual(chosen['source'], 'bundled')
        self.assertIn('not complete on disk', chosen['note'])

    def test_a_published_set_with_a_non_finite_number_is_rejected_by_version_not_retried(self):
        """Rule (finding 3): the sha256 proves the content is that version's, so a number no reader can take is a defect
        of the version: rejected once (status rejected, no last_error), never downloaded again."""
        self.publish(change=('five-axis-scores.json', lambda d: d['models'][next(iter(d['models']))].__setitem__(
            'probe', float('inf'))), at=T0 + timedelta(days=1))
        result = self.refresh()
        self.assertEqual(result['status'], 'rejected')
        self.assertIn('not a finite number', result['detail'])
        state = public_data.read_state(self.env)
        self.assertIsNone(state.get('last_error'))
        self.remote.calls.clear()
        self.assertEqual(self.refresh(force=True, now=T0 + timedelta(days=3))['status'], 'rejected')
        self.assertEqual(len(self.remote.calls), 1)                         # the manifest only

    def test_nothing_published_yet_is_a_completed_check(self):
        """Rule (finding 9): a schema with no publication yet (404) is the normal state before its first one: no failure,
        no last_error, the normal check interval."""
        self.remote.overrides[public_data.MANIFEST] = None
        missing = urllib.error.HTTPError(URL, 404, 'Not Found', {}, None)

        def fetch(url, timeout):
            if url.endswith(public_data.MANIFEST):
                raise missing
            return self.remote(url, timeout)
        result = public_data.refresh(environ=self.env, fetch=fetch, bundled=self.bundled, now=T0 + timedelta(days=2))
        self.assertEqual(result['status'], 'unpublished')
        state = public_data.read_state(self.env)
        self.assertIsNone(state.get('last_error'))
        cfg = public_data.config(self.env)
        self.assertFalse(public_data.due(state, cfg, T0 + timedelta(days=2, hours=1)))
        self.assertTrue(public_data.due(state, cfg, T0 + timedelta(days=2, hours=cfg['interval_hours'] + 1)))

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

    def fail_on_fetched(self, error=KeyError, every=False, before=None, fresh=True):
        """with_fallback over a run that raises ``error`` on the fetched set (on every set with ``every``); returns the
        sources it ran on. ``fresh``: a new set is published and fetched first."""
        if fresh:
            self.assertEqual(self.refresh()['status'], 'updated')
        self.held()
        seen = []

        def run():
            chosen = public_data.held(self.bundled, self.env)
            seen.append(chosen['source'])
            if before:
                before()
            if every or chosen['source'] == 'fetched':
                raise error('raised by the run')
            return 'routed'
        with patch.object(public_data, 'BUNDLED', self.bundled), patch.dict(os.environ, self.env):
            try:
                public_data.with_fallback(run)
            except Exception:
                pass
        return seen

    def test_a_data_error_on_the_fetched_set_reruns_on_the_shipped_set_and_sets_it_aside(self):
        """Rule (finding 1): a blame that can be wrong is not permanent: the set is set aside for SET_ASIDE_HOURS."""
        self.assertEqual(self.fail_on_fetched(KeyError), ['fetched', 'bundled'])
        state = public_data.read_state(self.env)
        self.assertEqual((state['set_aside_version'], state['set_aside_by']), (state['version'], public_data.reader_id()))
        self.assertNotIn('rejected_version', state)
        self.assertIn('KeyError', state['set_aside_error'])
        self.assertEqual(self.held()['source'], 'bundled')
        self.assertIn('set aside until', public_data.status(self.env, self.bundled)['note'])
        self.assertEqual(public_data.reference_alert(self.bundled, self.env)['code'], 'reference_data_refused')

    def test_a_set_aside_set_is_taken_again_after_its_period_or_at_once_for_a_newer_set_or_other_code(self):
        self.fail_on_fetched(KeyError)
        state = public_data.read_state(self.env)
        until = public_data._parse(state['set_aside_until'])
        self.assertAlmostEqual((until - public_data._parse(state['set_aside_at'])).total_seconds() / 3600,
                               public_data.SET_ASIDE_HOURS, delta=0.01)
        with patch.object(public_data, '_now', lambda: until - timedelta(minutes=1)):
            self.assertEqual(self.held()['source'], 'bundled')
            self.assertEqual(public_data.status(self.env, self.bundled)['set_aside']['version'], state['version'])
        self.assertEqual(self.refresh(force=True, now=T0 + timedelta(days=3))['status'], 'unchanged')
        self.assertEqual(self.held()['source'], 'bundled')                       # a check alone does not end it
        with patch.object(public_data, '_now', lambda: until + timedelta(minutes=1)):
            self.assertEqual(self.held()['source'], 'fetched')                   # the period is over
            report = public_data.status(self.env, self.bundled)
            self.assertIsNone(report['set_aside'])                               # finding 4: doctor says what is in effect
            self.assertNotIn('set aside', report['note'] or '')
            self.assertIsNone(public_data.reference_alert(self.bundled, self.env))
        self.assertNotIn('set_aside_version', public_data.read_state(self.env))  # cleared where it was read
        public_data.release()
        self.assertEqual(self.fail_on_fetched(KeyError, fresh=False), ['fetched', 'bundled'])
        with patch.object(public_data, 'reader_id', lambda: 'other-code'):
            self.assertEqual(self.refresh(force=True, now=T0 + timedelta(days=3))['status'], 'accepted')
            self.assertEqual(self.held()['source'], 'fetched')                   # this install's code changed
        self.publish(safety_margin_pct=0.6, at=T0 + timedelta(days=2, hours=1))
        self.fail_on_fetched(KeyError)
        self.publish(safety_margin_pct=0.7, at=T0 + timedelta(days=2, hours=2))
        self.assertEqual(self.refresh(force=True, now=T0 + timedelta(days=3))['status'], 'updated')
        self.assertEqual(self.held()['source'], 'fetched')                       # a newer set is tried at once
        self.assertNotIn('set_aside_version', public_data.read_state(self.env))

    def stand_set_aside(self, **fields):
        if not public_data.read_state(self.env).get('version'):
            self.assertEqual(self.refresh()['status'], 'updated')
        now = datetime.now(timezone.utc)
        state = public_data.read_state(self.env)
        values = {'set_aside_version': state['version'], 'set_aside_by': public_data.reader_id(),
                  'set_aside_at': public_data._stamp(now), 'set_aside_until': public_data._stamp(now + timedelta(hours=1)),
                  'set_aside_error': 'KeyError: x', 'set_aside_note': 'set aside until later', **fields}
        public_data._update_state(self.env, lambda s: s.update(values))
        public_data.release()

    def test_a_set_aside_begun_in_the_future_or_longer_than_its_period_is_not_one(self):
        """Rule (finding 4): a start after now is a clock that ran ahead, as for a check time in the future; a period
        longer than SET_ASIDE_HOURS is not one settle wrote (review round 3, r3_aside: until +400 days)."""
        now, later = datetime.now(timezone.utc), datetime.now(timezone.utc) + timedelta(days=400)
        for label, fields in (('begun in the future', {'set_aside_at': public_data._stamp(later),
                                                       'set_aside_until': public_data._stamp(later + timedelta(hours=24))}),
                              ('longer than its period', {'set_aside_at': public_data._stamp(now - timedelta(minutes=1)),
                                                          'set_aside_until': public_data._stamp(later)})):
            with self.subTest(label):
                self.stand_set_aside(**fields)
                self.assertEqual(self.held()['source'], 'fetched')
                self.assertIsNone(public_data.status(self.env, self.bundled)['set_aside'])
                self.assertIsNone(public_data.reference_alert(self.bundled, self.env))
                self.assertNotIn('set_aside_version', public_data.read_state(self.env))      # cleared where it was read
                public_data.release()

    def test_a_set_aside_recorded_by_other_code_does_not_hold_for_this_code(self):
        """Rule (review round 3, A1): an older install's code sharing this state set the set aside; this code accepted
        the set itself and takes it."""
        self.stand_set_aside(set_aside_by='an-older-reader')
        self.assertEqual(self.held()['source'], 'fetched')
        self.assertIsNone(public_data.status(self.env, self.bundled)['set_aside'])
        self.assertIsNone(public_data.reference_alert(self.bundled, self.env))
        public_data._update_state(self.env, lambda s: s.update(set_aside_by=public_data.reader_id()))
        public_data.release()
        self.assertEqual(self.held()['source'], 'bundled')                       # the same record by this code holds
        self.assertEqual(public_data.reference_alert(self.bundled, self.env)['code'], 'reference_data_refused')

    def test_only_a_data_error_is_re_run_and_blamed(self):
        """Rule (finding 1): a refusal (a ValueError, such as a dispatch_registry state refusal) and an environment
        failure are answers: never re-run, never blamed, whatever set is held (review round 2, R3)."""
        import dispatch_registry
        refusals = (ValueError, functools.partial(dispatch_registry.WorkspaceBusy, holders=[]), OSError,
                    sqlite3.OperationalError, RuntimeError)
        for error in refusals:
            with self.subTest(getattr(error, '__name__', 'WorkspaceBusy')):
                public_data.release()
                shutil.rmtree(self.state, ignore_errors=True)
                self.assertEqual(self.fail_on_fetched(error), ['fetched'])
                self.assertNotIn('set_aside_version', public_data.read_state(self.env))
        for error in (public_data.ReferenceDataError, KeyError, TypeError, AttributeError, ZeroDivisionError):
            with self.subTest(error.__name__):
                public_data.release()
                shutil.rmtree(self.state, ignore_errors=True)
                self.assertEqual(self.fail_on_fetched(error), ['fetched', 'bundled'])
                self.assertIn('set_aside_version', public_data.read_state(self.env))

    def test_a_re_run_never_repeats_a_classifier_call(self):
        """Rule (round 4): a paid classifier call is carried into the re-run (public_data.carried), not made again, even
        with the score cache off and a prompt the shipped set builds differently."""
        import scorer_client
        calls = []

        def run_scorer(scorer_type, task, capabilities, timeout, config, account=None):
            calls.append(public_data.held()['source'])
            return {'work_kind': 'code_implementation'}, 'claude', 'haiku'

        def run_structured(scorer_type, instructions, text, schema, timeout, config, account=None):
            calls.append(public_data.held()['source'])
            return {'scope': 'model'}, 'claude', 'haiku'

        def classify():
            scorer_client.score_task({'objective': 'Fix the parser.'}, {}, 5, scorer_config={})
            scorer_client.classify_structured('Which scope?', 'text', {'type': 'object'}, lambda a: a, 5, scorer_config={})
        with patch.dict(os.environ, {'ROUTER_SCORE_CACHE': 'off'}), \
                patch.object(scorer_client, 'run_scorer', run_scorer), \
                patch.object(scorer_client, 'run_structured', run_structured), \
                patch.object(scorer_client, 'scorer_accounts', lambda *a, **k: [None]):
            self.assertEqual(self.fail_on_fetched(KeyError, before=classify), ['fetched', 'bundled'])
        self.assertEqual(calls, ['fetched', 'fetched'])                          # one scorer, one structured call

    def test_the_judge_call_is_an_effect_and_never_repeated(self):
        """Rule (round 4): a select/delegate judge call is an effect; what follows it is only the ledger row."""
        import subscription_cli
        import subscription_judge
        judged = []

        def run_process(command, directory, prompt=None, timeout=None, account=None):
            if prompt is None:
                return 0, 'Logged in using ChatGPT', ''
            judged.append(public_data._EFFECTS[0])
            (Path(directory) / 'answer.json').write_text('{"choice": "none", "reason": "x"}', encoding='utf-8')
            return 0, '', ''
        payload = {'stage': 'delegate', 'state': {'usage': {}, 'now': 'now'}, 'response_schema': {'type': 'object'}}
        before = []
        with patch.object(subscription_judge, 'judge_order', lambda usage, requested: ['codex']), \
                patch.object(subscription_judge, 'resolve_judge', lambda kind, usage, availability: {'account': None}), \
                patch.object(subscription_judge.host_policy, 'load_policy', lambda: None), \
                patch.object(subscription_cli, 'cli_prefix', lambda provider: ['codex']), \
                patch.object(subscription_cli, 'run_process', run_process):
            seen = self.fail_on_fetched(KeyError, before=lambda: before.append(public_data._EFFECTS[0]) or
                                        subscription_judge.evaluate(payload, 'codex', 30))
        self.assertEqual(seen, ['fetched'])
        self.assertEqual(len(judged), 1)
        self.assertEqual(judged[0], before[0] + 1)                               # marked before the judge ran

    def test_an_error_after_an_effect_propagates_and_nothing_is_re_run(self):
        """Rule (finding 1): a re-run never repeats an effect."""
        self.assertEqual(self.fail_on_fetched(KeyError, before=lambda: public_data.effect('ledger row')), ['fetched'])
        self.assertNotIn('set_aside_version', public_data.read_state(self.env))

    def test_a_registry_change_is_an_effect_and_upkeep_is_not(self):
        """Rule (finding 2): a write this call decided is an effect; bringing recorded state in step with what happened
        on its own (an expired lease, an ended worker) is not."""
        import dispatch_registry
        path = self.state / 'dispatch.sqlite3'
        before = public_data._EFFECTS[0]
        with dispatch_registry.transaction(path, upkeep=True) as connection:
            connection.execute('INSERT INTO decisions VALUES (?,?)', ('upkeep', '{}'))
        self.assertEqual(public_data._EFFECTS[0], before)
        with dispatch_registry.transaction(path) as connection:
            connection.execute('INSERT INTO decisions VALUES (?,?)', ('decided', '{}'))
        self.assertEqual(public_data._EFFECTS[0], before + 1)
        with dispatch_registry.transaction(path) as connection:
            connection.execute('SELECT 1')                                       # nothing changed: nothing marked
        self.assertEqual(public_data._EFFECTS[0], before + 1)

    def test_a_failure_the_shipped_set_produces_as_well_blames_nothing(self):
        self.assertEqual(self.fail_on_fetched(KeyError, every=True), ['fetched', 'bundled'])
        self.assertNotIn('set_aside_version', public_data.read_state(self.env))

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
    """Rule: a fetched set is never on the failure line. A reader failing on it with a data error the acceptance cannot
    see (here one that fails only for a fetched set), before the run had an effect, makes `plan` run again on the shipped
    set; the set is set aside. After the ledger row, or for a refusal, the error is the run's answer."""

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

    def fetched_set(self):
        isolated_router_state(self.addCleanup)
        public_data.release()
        self.addCleanup(public_data.release)
        public_data.release()
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
                                   'expected_hours': 0.5, 'profile': {
                                       'work_kind': 'code_implementation', 'primary_level': 2, 'secondary_kind': 'none',
                                       'secondary_level': 0, 'access': 'write', 'local_only': True, 'needs_visual': False,
                                       'wants_fast': False, 'expected_hours': 0.5, 'task_count': '1'}}})
        return env, task

    def test_a_plan_error_after_its_ledger_row_is_its_answer_and_nothing_is_re_run(self):
        """Review round 2, r2_inject: a failure after the ledger row was written ran the stage twice (two rows) and
        rejected a good set for good."""
        import decision_ledger
        import io
        import route_judge
        for error in (KeyError, ValueError):
            with self.subTest(error.__name__):
                env, task = self.fetched_set()
                real, seen = decision_ledger.append_ledger, []

                def append(entry):
                    written = real(entry)
                    seen.append(public_data.held()['source'])
                    raise error('a local failure after the row was written')
                out = io.StringIO()
                with patch.dict(os.environ, env), patch.object(decision_ledger, 'append_ledger', append), \
                        patch.object(sys, 'argv', ['route_judge.py', 'plan', '--input', str(task)]), \
                        patch.object(sys, 'stdout', out):
                    code = route_judge.main()                                   # finding 2: never a traceback
                self.assertEqual(seen, ['fetched'])
                rows = decision_ledger.ledger_path().read_text(encoding='utf-8').splitlines()
                self.assertEqual(len(rows), 1)
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(out.getvalue())['error_type'], error.__name__)
                self.assertNotIn('set_aside_version', public_data.read_state(env))

    def test_a_refusal_on_the_fetched_set_is_the_answer_and_blames_nothing(self):
        """A refusal is not a data error, even one that depends on the set (named_model_unavailable)."""
        import allocator
        import io
        import route_judge
        env, task = self.fetched_set()
        seen = []

        def plan(*args, **kwargs):
            seen.append(public_data.held()['source'])
            raise ValueError('named_model_unavailable: gpt-6.2-sol has no configured route')
        out = io.StringIO()
        with patch.dict(os.environ, env), patch.object(allocator, 'plan', plan), \
                patch.object(sys, 'argv', ['route_judge.py', 'plan', '--input', str(task), '--dry-run']), \
                patch.object(sys, 'stdout', out):
            self.assertEqual(route_judge.main(), 1)
        self.assertEqual(seen, ['fetched'])
        self.assertIn('named_model_unavailable', out.getvalue())
        self.assertNotIn('set_aside_version', public_data.read_state(env))

    def test_expiring_a_lease_on_the_way_is_not_an_effect(self):
        """Rule (finding 2, review round 3 `housekeeping`): plan's reservation read expires a lease that ran out; that
        commit does not stop a later data error from re-running on the shipped set."""
        import dispatch_registry
        import io
        import route_judge
        env, task = self.fetched_set()
        with patch.dict(os.environ, env):
            with dispatch_registry.transaction() as connection:
                dispatch_registry.save(connection, {'dispatch_id': 'd-old', 'decision_id': 'dec-old', 'state': 'launched',
                                                    'dispatch_mode': 'cli', 'lease_until': '2026-11-18T00:00:00Z',
                                                    'events': [], 'parent_id': 'p'})
        real, seen = route_judge._add_plan_alerts, []

        def late(*args, **kwargs):
            seen.append(public_data.held()['source'])
            if len(seen) == 1:
                raise KeyError('a field only the live fleet reaches')
            return real(*args, **kwargs)
        out = io.StringIO()
        with patch.dict(os.environ, env), patch.object(route_judge, '_add_plan_alerts', late), \
                patch.object(sys, 'argv', ['route_judge.py', 'plan', '--input', str(task)]), \
                patch.object(sys, 'stdout', out):
            code = route_judge.main()
            self.assertEqual(dispatch_registry.inspect('d-old')['state'], 'expired')   # the upkeep did happen
        self.assertEqual(seen, ['fetched', 'bundled'])
        self.assertIn(code, (0, 2))
        self.assertIn('set_aside_version', public_data.read_state(env))

    def test_the_caller_input_is_read_once_and_a_re_run_receives_it(self):
        """Rule (finding 1): stdin and the --input / --brief files are read once, before the part that may run again."""
        import io
        import roll
        import route_judge
        text = 'Fix an off-by-one slice in parser.py and add a unit test.'
        for how in ('stdin', 'brief', 'input'):
            with self.subTest(how):
                env, task = self.fetched_set()
                brief = task.with_name('brief.md')
                brief.write_text(text, encoding='utf-8')
                real_plan, real_read, runs, texts = route_judge.plan_stage, roll.read_task_text, [], []

                def plan_stage(*args, **kwargs):
                    runs.append(public_data.held()['source'])
                    if len(runs) == 1:
                        for named in (brief, task):
                            named.unlink()                                       # a re-run that read again would fail
                        raise KeyError('a field only the live fleet reaches')
                    return real_plan(*args, **kwargs)
                argv = {'stdin': ['roll', '--task', '-'], 'brief': ['roll', '--brief', str(brief)],
                        'input': ['plan', '--input', str(task)]}[how]
                out = io.StringIO()
                stdin = io.TextIOWrapper(io.BytesIO(text.encode('utf-8')), encoding='utf-8')
                with patch.dict(os.environ, env), patch.object(route_judge, 'plan_stage', plan_stage), \
                        patch.object(roll, 'read_task_text', lambda args: texts.append(real_read(args)) or texts[-1]), \
                        patch.object(sys, 'argv', ['route_judge.py', *argv, '--dry-run']), \
                        patch.object(sys, 'stdin', stdin), patch.object(sys, 'stdout', out):
                    route_judge.main()
                self.assertEqual(runs, ['fetched', 'bundled'], out.getvalue()[-400:])
                self.assertNotIn('The task is empty', out.getvalue())
                self.assertNotIn('Routing input or local runtime failed', out.getvalue())
                if how != 'input':
                    self.assertEqual(texts, [text, text])

    def test_record_reads_its_input_once_and_reports_a_data_error_by_name(self):
        """Rules (finding 1 and 2) on main's named-route entry `record --input`: the re-run receives the input as first
        read, and a data error on the shipped set too is the stage's named error, never a traceback."""
        import io
        import route_judge
        env, task = self.fetched_set()
        real, tasks = route_judge._record_task, []

        def read_then_fail(args, given):
            tasks.append(real(args, given))
            public_data.held()                                                   # a reader took the set
            if len(tasks) == 1:
                task.unlink()                                                    # a re-run that read again would fail
                raise KeyError('a field only the live fleet reaches')
            raise KeyError('the shipped set fails too')
        out = io.StringIO()
        with patch.dict(os.environ, env), patch.object(route_judge, '_record_task', read_then_fail), \
                patch.object(sys, 'stdout', out), patch.object(sys, 'argv', [
                    'route_judge.py', 'record', '--input', str(task), '--constraint', 'user_override',
                    '--account', 'codex', '--provider', 'codex', '--model', 'gpt-6.2-sol', '--basis', 'the user named it']):
            code = route_judge.main()
        self.assertEqual([t.get('objective', '')[:10] for t in tasks], ['Fix an off'] * 2)
        self.assertEqual(code, 1)
        report = json.loads(out.getvalue())
        self.assertEqual((report['error_type'], report['error']), ('KeyError', 'Reference or local data could not be read'))

    def test_marking_a_ledger_row_on_a_broken_fetched_set_runs_again_on_the_shipped_set(self):
        """Rule (merge of main): the automatic-routing marker main derives in append_ledger reads the fleet; a data error
        there on a fetched set comes before the row, so the stage runs again on the shipped set and writes one row."""
        import decision_ledger
        env, _task = self.fetched_set()
        with patch.dict(os.environ, env):
            os.environ.pop('ROUTER_ACCOUNTS_FILE', None)                         # the fleet of the held set, as installed
            held = public_data.held()
            self.assertEqual(held['source'], 'fetched')
            broken = json.loads(held['files']['accounts.json'])
            del broken['accounts']
            public_data._HELD[public_data._key(None, None)] = {
                **held, 'files': {**held['files'], 'accounts.json': json.dumps(broken).encode()}}
            entry = {'stage': 'record', 'choice': 'constraint', 'bundle': {'account': 'codex', 'model': 'gpt-6.2-sol'}}
            sources = []

            def write():
                sources.append(public_data.held()['source'])
                return decision_ledger.append_ledger(dict(entry))
            self.assertTrue(public_data.with_fallback(write))
            rows = decision_ledger.ledger_path().read_text(encoding='utf-8').splitlines()
        self.assertEqual(sources, ['fetched', 'bundled'])
        self.assertEqual(len(rows), 1)
        self.assertIn('set_aside_version', public_data.read_state(env))

    def test_a_plan_says_when_the_published_set_is_set_aside(self):
        """Rule (review round 3, A2): while the published set is set aside, every plan carries reference_data_refused."""
        import io
        import route_judge
        env, task = self.fetched_set()
        now = datetime.now(timezone.utc)
        public_data._update_state(env, lambda s: s.update(
            set_aside_version=s['version'], set_aside_by=public_data.reader_id(), set_aside_at=public_data._stamp(now),
            set_aside_until=public_data._stamp(now + timedelta(hours=1)), set_aside_error='KeyError: x',
            set_aside_note='set aside until later'))
        public_data.release()
        out = io.StringIO()
        with patch.dict(os.environ, env), patch.object(sys, 'stdout', out), \
                patch.object(sys, 'argv', ['route_judge.py', 'plan', '--input', str(task), '--dry-run']):
            route_judge.main()
        alerts = json.loads(out.getvalue()).get('alerts') or []
        self.assertIn('reference_data_refused', [a.get('code') for a in alerts])

    def plan_falls_back(self, error, entry):
        import io
        env, task = self.fetched_set()
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
        self.assertEqual(state['set_aside_version'], state['version'])
        public_data.release()
        self.assertEqual(public_data.held(None, env)['source'], 'bundled')
        self.assertIn('set aside until', public_data.status(env)['note'])


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
    """Rule (finding 4): reference data must match the declared patterns, which accept everything shipped and every id
    this code generates; values from the user's machine are never dropped, only made safe where they are used, and a
    launch that cannot be expressed safely is refused there, by name."""

    def test_the_reference_document_is_refused_by_name_and_the_patterns_accept_what_this_code_ships_and_generates(self):
        doc = read_json(public_data.BUNDLED / 'accounts.json')
        self.assertEqual(account_fleet.launch_problems(doc), [])
        bad = copy.deepcopy(doc)
        bad['accounts']['codex']['bundles']['evil'] = {'model': 'gpt-6.2-sol $(calc.exe)', 'effort': 'high'}
        with self.assertRaises(public_data.ReferenceDataError) as refused:
            account_fleet.load_accounts(document=bad)
        self.assertIn('codex.bundles.evil.model', str(refused.exception))
        listed = {'models': [{'id': 'gpt-6.2-sol-2026-11-01'}, {'id': 'some-new-model-x1'}, {'id': 'gpt-6.2-sol:high'}]}
        catalog = {'providers': {p: listed for p in ('codex', 'claude', 'pi', 'opencode', 'antigravity')}}
        derived = auto_bundles.derive(doc, catalog, 1795000000)
        generated = [(account, bid, row) for account, rows in (derived.get('bundles') or {}).items() for bid, row in rows.items()]
        self.assertTrue(any('#' in bid for _, bid, _ in generated))         # a collision id is among them
        for account, bid, row in generated + [('codex', 'auto:codex:codex:gpt-6.2-sol@high#a1b2c3d4', {'model': 'gpt-6.2-sol'})]:
            self.assertEqual(launch_values.bundle_problems(account, bid, row), [], bid)

    def test_a_local_state_shaped_by_v0_7_0_keeps_every_bundle_it_had(self):
        """Account names the user chose, model ids a provider lists and the ids derived from them are never dropped."""
        import provider_inventory
        isolated_router_state(self.addCleanup)
        discovered = {
            'codex@work': {'gpt-6.2-sol@high': {'model': 'gpt-6.2-sol', 'effort': 'high', 'provider': 'codex@work'},
                           'auto:codex@work:codex:gpt-6.2-sol@high#a1b2c3d4': {'model': 'gpt-6.2-sol', 'effort': 'high',
                                                                               'provider': 'codex@work'}},
            'my claude': {'auto:my claude:claude:claude-opus-5-5[1m]@high': {'model': 'claude-opus-5-5[1m]', 'effort': 'high',
                                                                             'provider': 'my claude'}},
            'claude:alt': {'grok fast': {'model': 'grok-4.7 fast', 'provider': 'claude:alt'}},
        }
        real = provider_inventory.apply

        def apply(accounts, *args, **kwargs):
            result = real(accounts, *args, **kwargs)
            for name, rows in discovered.items():
                accounts['accounts'][name] = {'plan': 'unknown', 'billing': 'subscription', 'verified': True,
                                              'windows': {}, 'bundles': copy.deepcopy(rows)}
            return result
        override = {'schema_version': 2, 'accounts': {name: {'bundles': {bid: {'disabled': False} for bid in rows}}
                                                      for name, rows in discovered.items()}}
        path = account_fleet.accounts_override_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, override)
        with patch.object(provider_inventory, 'apply', apply), patch.dict(os.environ, {'AGENTCOMPASS_PUBLIC_DATA': 'off'}):
            accounts = account_fleet.load_accounts()
        for name, rows in discovered.items():
            self.assertEqual(set(rows) - set((accounts['accounts'].get(name) or {}).get('bundles') or {}), set(), name)

    def test_a_launch_that_cannot_be_expressed_safely_is_refused_there_by_name(self):
        import roll
        launched = []

        class Process:
            pid = 4242
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(roll, 'provider_definition', lambda provider, bundle=None: ('codex', {})), \
                patch.object(roll, 'cli_command', lambda family, provider: ['codex']), \
                patch.object(roll.host_placement, 'place', lambda *a, **k: {'cwd': folder}), \
                patch.object(roll, 'CLI_RUNS_DIR', Path(folder) / 'runs'), \
                patch.object(roll.subprocess, 'Popen', lambda argv, **k: launched.append(argv) or Process()):
            args = type('Args', (), {'workspace': None})()
            out = {'parent_id': 'p', 'parent_host': 'paseo', 'parent_address': 'p'}
            with self.assertRaises(launch_values.LaunchValueError) as refused:
                roll.cli(args, out, {'provider': 'codex@work', 'model': '-c', 'effort': 'high'}, 'task')
            self.assertIn("model '-c'", str(refused.exception))
            self.assertEqual(launched, [])
            result, code = roll.cli(args, out, {'provider': 'my claude:alt', 'model': 'gpt 6.2 sol[1m]', 'effort': 'high'}, 'task')
            self.assertTrue(result['launched'])
            self.assertIn('gpt 6.2 sol[1m]', launched[0])                      # its own argument, as it is
            self.assertEqual([p.name.split('-', 2)[-1].rsplit('-', 1)[0] for p in (Path(folder) / 'runs').iterdir()],
                             ['my_claude_alt'])

    def test_every_launch_is_marked_an_effect_before_it_starts(self):
        """Rule (round 4): a launch is an effect also when no ledger row came before it: the CLI Popen, Dot, grok-bot,
        Herdr and the Paseo bridge mark before they start anything."""
        import dispatch_flow
        import host_herdr
        import roll
        import run_grok_bot
        at = {}

        class Stop(Exception):
            pass

        def starting(name):
            def start(*args, **kwargs):
                at[name] = public_data._EFFECTS[0]
                raise Stop(name)
            return start

        def marked(name, call):
            before = public_data._EFFECTS[0]
            try:
                call()
            except (Stop, OSError):
                pass
            self.assertEqual(at.get(name), before + 1, name)
        out = {'parent_id': 'p', 'parent_host': 'paseo', 'parent_address': 'p'}
        with tempfile.TemporaryDirectory() as folder:
            prompt = Path(folder) / 'prompt.md'
            prompt.write_text('task', encoding='utf-8')
            args = argparse.Namespace(file=[], apply_to=None, local_only=None, workspace=None)
            with patch.object(roll.subprocess, 'run', starting('dot')):
                marked('dot', lambda: roll.dot(args, out, prompt))
            with patch.object(run_grok_bot, 'submit', starting('grok-bot')):
                marked('grok-bot', lambda: roll.grok_bot(args, out, prompt, {'task_id': 't'},
                                                         {'model': 'grok-bot', 'account': 'grok-bot'}))
            with patch.object(roll, 'provider_definition', lambda provider, bundle=None: ('codex', {})), \
                    patch.object(roll, 'cli_command', lambda family, provider: ['codex']), \
                    patch.object(roll.host_placement, 'place', lambda *a, **k: {'cwd': folder}), \
                    patch.object(roll, 'CLI_RUNS_DIR', Path(folder) / 'runs'), \
                    patch.object(roll.subprocess, 'Popen', starting('cli')):
                marked('cli', lambda: roll.cli(type('Args', (), {'workspace': None})(), out,
                                               {'provider': 'codex', 'model': 'gpt-6.2-sol', 'effort': 'high'}, 'task'))
            with patch.object(host_herdr, 'interactive', lambda bundle: None), \
                    patch.object(host_herdr, 'hc', lambda *a: '{}'), \
                    patch.object(host_herdr, 'terminal_from', lambda value: {'state': 'live'}), \
                    patch.object(host_herdr, 'runs_dir', starting('herdr')):
                marked('herdr', lambda: host_herdr._launch(out, {'provider': 'codex'}, prompt, 't'))
            for operation, launches in (('create', True), ('resume', True), ('observe', False), ('notify_end', False)):
                with patch.object(dispatch_flow.subprocess, 'Popen', starting(operation)):
                    before = public_data._EFFECTS[0]
                    with self.assertRaises(Stop):
                        dispatch_flow.host_call({'operation': operation, 'timeout_seconds': 1})
                self.assertEqual(at[operation], before + (1 if launches else 0), operation)

    def test_herdr_passes_the_model_as_its_own_argument_or_refuses_the_launch(self):
        """Rule (finding 4, review round 3 L2)."""
        import host_herdr
        import roll
        with patch.object(roll, 'provider_definition', lambda provider, bundle=None: ('claude', {})), \
                patch.object(roll, 'cli_command', lambda family, provider: ['claude']):
            with self.assertRaises(launch_values.LaunchValueError):
                host_herdr.interactive({'provider': 'claude', 'model': '-c', 'effort': 'high'})
            self.assertIn('gpt 6 [1m]', host_herdr.interactive({'provider': 'claude', 'model': 'gpt 6 [1m]'})[0])

    def test_values_are_quoted_for_the_syntax_they_land_in(self):
        self.assertEqual(json.loads(launch_values.toml_string('high"; x = "')), 'high"; x = "')
        self.assertEqual(launch_values.shell_word('gpt-6.2-sol'), 'gpt-6.2-sol')
        self.assertEqual(launch_values.shell_word('a $(b)'), "'a $(b)'")
        with self.assertRaises(launch_values.LaunchValueError):
            launch_values.shell_word("it's")
        with self.assertRaises(launch_values.LaunchValueError):
            launch_values.shell_word('-x')
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
        """Rule (finding 6): the hermetic run refuses a run-time module opening a file of the set other than through
        public_data.document, whatever shape the read takes (hermetic_run.reference_reader); the shapes a static scan
        misses (review round 3, r3_scan) are refused here as real code under tray/."""
        import hermetic_run
        self.assertEqual(hermetic_run.REFERENCE_FILES, public_data.FILES)
        head = ("from pathlib import Path\nimport json\nimport public_data\n"
                "ROOT = Path(public_data.BUNDLED)\n")
        shapes = {
            'two lines': "REF = ROOT / 'accounts.json'\nDIRECT = json.loads(REF.read_text(encoding='utf-8'))\n",
            'a loop variable': "for name in ('accounts.json',):\n    DIRECT = (ROOT / name).read_text(encoding='utf-8')\n",
            'a function returns the path': "def ref():\n    return ROOT / 'accounts.json'\nDIRECT = ref().read_text()\n",
            'a built name': "KIND = 'accounts'\nDIRECT = (ROOT / (KIND + '.json')).read_text(encoding='utf-8')\n",
            "the accessor's private helper": "DIRECT = public_data._read_bytes(ROOT / 'accounts.json')\n",
            # finding 3 (review round 4, S2): an owner's own readers serve its commands, they are not an accessor
            "an owner's public generator": "import capability_catalog\nDIRECT = capability_catalog.markdown({'accounts': {}})\n",
            "an owner's public checker": "import capability_catalog\nDIRECT = capability_catalog.check_files()\n",
            "another function of the accessor's module": "DIRECT = public_data.validate_files(ROOT)\n",
            'open()': "with open(ROOT / 'five-axis-scores.json', 'rb') as f:\n    DIRECT = f.read()\n",
            'a default argument': "def load(path=ROOT / 'accounts.json'):\n    return Path(path).read_text()\nDIRECT = load()\n",
        }
        for label, code in shapes.items():
            with self.subTest(label):
                before = len(hermetic_run.VIOLATIONS)
                with self.assertRaises(hermetic_run.HermeticViolation):
                    exec(compile(head + code, str(REPO / 'tray' / 'planted_reader.py'), 'exec'), {'__name__': 'planted'})
                found = hermetic_run.VIOLATIONS[before:]
                del hermetic_run.VIOLATIONS[before:]                             # this test's own, expected refusal
                self.assertEqual(len(found), 1)
                self.assertIn('planted_reader.py', found[0][1])
        before = len(hermetic_run.VIOLATIONS)
        exec(compile(head + "DIRECT = public_data.document(ROOT / 'accounts.json')\n",
                     str(REPO / 'tray' / 'planted_reader.py'), 'exec'), {'__name__': 'planted'})
        self.assertEqual(hermetic_run.VIOLATIONS[before:], [])                   # through the accessor
        planted = {'__name__': 'planted'}
        exec(compile(head + "def load(path):\n    return Path(path).read_text(encoding='utf-8')\n",
                     str(REPO / 'tray' / 'planted_reader.py'), 'exec'), planted)
        planted['load'](public_data.BUNDLED / 'accounts.json')                   # a file its caller (this test) named
        self.assertEqual(hermetic_run.VIOLATIONS[before:], [])

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
