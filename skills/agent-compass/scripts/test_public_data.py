"""Public reference data channel: manifest, fetch, verification, fallback, interval and precedence. No network."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import account_fleet
import axis_scores
import provider_inventory
import public_data
import scorer_client
from router_test_support import isolated_router_state

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
URL = 'https://example.invalid/refs/'


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
            folder.mkdir(parents=True)
            for name in public_data.FILES:
                shutil.copyfile(public_data.BUNDLED / name, folder / name)
        write_json(self.bundled / public_data.MANIFEST, public_data.build_manifest(self.bundled, now=T0))
        self.env = {'ROUTER_STATE_DIR': str(self.state), 'AGENTCOMPASS_PUBLIC_DATA': 'on',
                    'AGENTCOMPASS_PUBLIC_DATA_URL': URL}
        self.publish(safety_margin_pct=0.5, at=T0 + timedelta(days=1))
        self.remote = FakeRemote(self.remote_dir)

    def publish(self, safety_margin_pct, at, schema=None):
        accounts = json.loads((self.remote_dir / 'accounts.json').read_text(encoding='utf-8-sig'))
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
        path = public_data.effective(self.bundled / 'accounts.json', self.env)
        return json.loads(path.read_text(encoding='utf-8-sig'))


class ManifestTests(unittest.TestCase):
    def test_shipped_manifest_matches_the_shipped_references(self):
        current = json.loads((public_data.BUNDLED / public_data.MANIFEST).read_text(encoding='utf-8'))
        self.assertEqual(public_data.build_manifest(public_data.BUNDLED, current), current,
                         'references/public-data.json is stale: run `python -X utf8 public_data.py manifest --write`')
        self.assertEqual(public_data.check_set(public_data.BUNDLED, current), ('ok', ''))
        self.assertLessEqual(current['schema_version'], public_data.SCHEMA_VERSION)

    def test_isolated_interpreter_validates_the_shipped_user_scores(self):
        """`agentcompass data` runs public_data.py under python -I, which does not put the script directory on sys.path.
        check_set must still import the sibling user_scores module and accept the shipped set."""
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
        result = subprocess.run(
            [sys.executable, "-I", "-c", code, str(Path(public_data.__file__).resolve())],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-500:] or result.stdout[-500:])

    def test_published_at_moves_only_when_content_changes(self):
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


class FetchTests(ChannelCase):
    def test_fresh_fetch_is_verified_and_read_instead_of_the_shipped_copy(self):
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

    def test_hash_mismatch_is_rejected_and_nothing_is_applied(self):
        self.remote.overrides['accounts.json'] = b'{"scoring": {"safety_margin_pct": 9}}\n'
        outcome = self.refresh()
        self.assertEqual(outcome['status'], 'failed')
        self.assertIn('sha256', outcome['error'])
        self.assertEqual(self.chosen()['source'], 'bundled')
        self.assertNotEqual(self.effective_accounts()['scoring']['safety_margin_pct'], 9)
        self.assertEqual(list((self.state / 'public-data' / 'sets').iterdir()), [])
        self.assertIn('sha256', public_data.read_state(self.env)['last_error'])

    def test_a_fetched_file_that_does_not_match_on_disk_is_not_read(self):
        self.refresh()
        version = public_data.read_state(self.env)['version']
        (self.state / 'public-data' / 'sets' / version / 'accounts.json').write_text('{}', encoding='utf-8')
        chosen = self.chosen()
        self.assertEqual(chosen['source'], 'bundled')
        self.assertIn('not usable', chosen['note'])

    def test_newer_schema_is_ignored_and_the_last_good_set_stays(self):
        self.refresh()
        self.publish(safety_margin_pct=0.7, at=T0 + timedelta(days=3), schema=public_data.SCHEMA_VERSION + 1)
        self.remote.calls.clear()
        outcome = self.refresh(force=True, now=T0 + timedelta(days=4))
        self.assertEqual(outcome['status'], 'schema_too_new')
        self.assertEqual(self.remote.calls, [URL + public_data.MANIFEST])  # no data file was fetched
        self.assertEqual(self.effective_accounts()['scoring']['safety_margin_pct'], 0.5)
        self.assertIn('schema', public_data.status(self.env, self.bundled)['note'])

    def test_offline_falls_back_to_the_cache_then_to_the_shipped_copy(self):
        self.refresh()
        self.remote.offline = True
        outcome = self.refresh(force=True, now=T0 + timedelta(days=3))
        self.assertEqual(outcome['status'], 'failed')
        self.assertEqual(self.effective_accounts()['scoring']['safety_margin_pct'], 0.5)
        shutil.rmtree(self.state / 'public-data' / 'sets')
        self.assertEqual(self.chosen()['source'], 'bundled')
        self.assertEqual(self.effective_accounts()['scoring']['safety_margin_pct'],
                         json.loads((self.bundled / 'accounts.json').read_text(encoding='utf-8-sig'))['scoring']['safety_margin_pct'])

    def test_refresh_interval_and_retry_are_honoured(self):
        self.refresh(now=T0 + timedelta(days=2))
        calls = len(self.remote.calls)
        self.assertEqual(self.refresh(now=T0 + timedelta(days=2, hours=5))['status'], 'not_due')
        self.assertEqual(len(self.remote.calls), calls)
        self.assertEqual(self.refresh(now=T0 + timedelta(days=2, hours=7))['status'], 'unchanged')
        self.assertEqual(len(self.remote.calls), calls + 1)  # the manifest only
        self.remote.offline = True
        self.assertEqual(self.refresh(now=T0 + timedelta(days=3))['status'], 'failed')
        self.assertEqual(self.refresh(now=T0 + timedelta(days=3, minutes=10))['status'], 'not_due')
        self.assertEqual(self.refresh(now=T0 + timedelta(days=3, minutes=40))['status'], 'failed')

    def test_fetched_data_older_than_the_shipped_copy_is_not_read(self):
        self.publish(safety_margin_pct=0.5, at=T0 - timedelta(days=1))
        self.assertEqual(self.refresh()['status'], 'updated')
        chosen = self.chosen()
        self.assertEqual(chosen['source'], 'bundled')
        self.assertIn('as new', chosen['note'])

    def test_shipped_references_with_local_edits_win(self):
        self.refresh()
        accounts = json.loads((self.bundled / 'accounts.json').read_text(encoding='utf-8-sig'))
        accounts['scoring']['safety_margin_pct'] = 0.123
        write_json(self.bundled / 'accounts.json', accounts)
        chosen = self.chosen()
        self.assertEqual(chosen['source'], 'bundled')
        self.assertIn('local edits', chosen['note'])
        self.assertEqual(self.effective_accounts()['scoring']['safety_margin_pct'], 0.123)


class SwitchTests(ChannelCase):
    def test_disabled_makes_no_network_call_and_reads_the_shipped_copy(self):
        self.refresh()
        self.remote.calls.clear()
        for env in ({**self.env, 'AGENTCOMPASS_PUBLIC_DATA': 'off'},):
            self.assertEqual(public_data.refresh(force=True, environ=env, fetch=self.remote)['status'], 'disabled')
            with patch.object(public_data, 'http_get', side_effect=AssertionError('network')):
                self.assertIsNone(public_data.refresh_in_background(environ=env, bundled=self.bundled))
            public_data.unpin()
            self.assertEqual(public_data.selection(self.bundled, env)['source'], 'bundled')
        self.assertEqual(self.remote.calls, [])

    def test_config_file_switches_the_channel_off_and_env_wins_over_it(self):
        env = {k: v for k, v in self.env.items() if k != 'AGENTCOMPASS_PUBLIC_DATA'}
        self.state.mkdir(parents=True, exist_ok=True)
        write_json(self.state / public_data.CONFIG_FILE, {'enabled': False, 'interval_hours': 1})
        cfg = public_data.config(env)
        self.assertEqual((cfg['enabled'], cfg['switch'], cfg['interval_hours']), (False, 'public-data-config.json', 1.0))
        self.assertEqual(public_data.refresh(environ=env, fetch=self.remote)['status'], 'disabled')
        self.assertEqual(self.remote.calls, [])
        self.assertTrue(public_data.config({**env, 'AGENTCOMPASS_PUBLIC_DATA': '1'})['enabled'])

    def test_background_refresh_pins_the_set_this_process_started_with(self):
        with patch.object(public_data, 'http_get', self.remote):
            thread = public_data.refresh_in_background(environ=self.env, bundled=self.bundled)
            thread.join(10)
        self.assertEqual(public_data.read_state(self.env)['published_at'], '2026-11-02T00:00:00Z')
        self.assertEqual(self.chosen()['source'], 'bundled')  # pinned before the refresh landed
        public_data.unpin()
        self.assertEqual(self.chosen()['source'], 'fetched')


class LoaderPrecedenceTests(ChannelCase):
    def setUp(self):
        super().setUp()
        isolated_router_state(self.addCleanup)
        os.environ.pop('ROUTER_ACCOUNTS_FILE', None)
        os.environ.update(self.env)
        self.env = {**self.env, 'ROUTER_STATE_DIR': os.environ['ROUTER_STATE_DIR']}
        self.state = Path(self.env['ROUTER_STATE_DIR'])
        self.assertEqual(self.refresh()['status'], 'updated')
        public_data.unpin()

    def test_accounts_templates_scores_and_classifier_cases_load_from_the_fetched_set(self):
        templates = json.loads((self.remote_dir / 'plan-templates.json').read_text(encoding='utf-8-sig'))
        templates['claude']['pro']['windows']['five_hour']['hours'] = 6
        write_json(self.remote_dir / 'plan-templates.json', templates)
        cases = json.loads((self.remote_dir / 'classifier-cases.json').read_text(encoding='utf-8-sig'))
        cases['calibration_cases'] = [{'marker': 'fetched'}]
        write_json(self.remote_dir / 'classifier-cases.json', cases)
        five = json.loads((self.remote_dir / 'five-axis-scores.json').read_text(encoding='utf-8-sig'))
        five['as_of'] = '2026-11-12'
        write_json(self.remote_dir / 'five-axis-scores.json', five)
        write_json(self.remote_dir / 'user-scores.json', {
            'schema_version': 1,
            'scores': {'grok-bot': {'coding': {'level': 2.0, 'as_of': '2026-11-11', 'kind': 'maintainer'}}},
        })
        self.publish(safety_margin_pct=0.5, at=T0 + timedelta(days=2))
        self.assertEqual(self.refresh(force=True, now=T0 + timedelta(days=3))['status'], 'updated')
        with patch.object(account_fleet, 'REFERENCES', self.bundled):
            self.assertEqual(account_fleet.fleet_document_path().parent.parent.name, 'sets')
            self.assertEqual(axis_scores.load_five_axis()['as_of'], '2026-11-12')
            self.assertEqual(axis_scores.load_public_user_scores()['scores']['grok-bot']['coding']['level'], 2.0)
        with patch.object(provider_inventory, 'PLAN_TEMPLATES', self.bundled / 'plan-templates.json'), \
                patch.object(provider_inventory, '_SHIPPED_PLAN_TEMPLATES', None):
            self.assertEqual(provider_inventory.plan_defaults({}, 'claude', 'pro')['windows']['five_hour']['hours'], 6)
        with patch.object(scorer_client, 'CLASSIFIER_CASES_PATH', self.bundled / 'classifier-cases.json'):
            self.assertEqual(scorer_client.load_classifier_cases()['calibration_cases'], [{'marker': 'fetched'}])

    def test_user_override_beats_fetched_data(self):
        with patch.object(account_fleet, 'REFERENCES', self.bundled):
            self.assertEqual(account_fleet.load_accounts()['scoring']['safety_margin_pct'], 0.5)
            write_json(Path(os.environ['AGENTCOMPASS_ACCOUNTS_OVERRIDE']), {'schema_version': 2, 'scoring': {'safety_margin_pct': 0.9}})
            self.assertEqual(account_fleet.load_accounts()['scoring']['safety_margin_pct'], 0.9)
            before = (self.bundled / 'accounts.json').read_bytes()
            local = self.state / 'accounts.local.json'
            write_json(local, json.loads(before.decode('utf-8-sig')))
            self.assertNotEqual(account_fleet.fleet_document_path(), local)  # no machine-local fleet replaces public data
        self.assertEqual((self.bundled / 'accounts.json').read_bytes(), before)  # fetched data never writes user files
        self.assertEqual(json.loads(Path(os.environ['AGENTCOMPASS_ACCOUNTS_OVERRIDE']).read_text(encoding='utf-8')),
                         {'schema_version': 2, 'scoring': {'safety_margin_pct': 0.9}})


class ProvenanceTests(unittest.TestCase):
    """R2-F2: public reference files accept cited https:// URLs or dated kind='maintainer' declarations, and reject
    uncited kind='user' or undated declarations."""

    def test_accounts_provenance_accepts_dated_maintainer_and_rejects_uncited_user(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for name in public_data.FILES:
                shutil.copyfile(public_data.BUNDLED / name, folder / name)
            manifest = public_data.build_manifest(folder, now=T0)
            self.assertEqual(public_data.check_set(folder, manifest), ('ok', ''))

            # Uncited kind='user' in same_model fails check_set:
            accounts = json.loads((folder / 'accounts.json').read_text(encoding='utf-8'))
            accounts['same_model']['grok-bot']['kind'] = 'user'
            write_json(folder / 'accounts.json', accounts)
            bad_manifest = public_data.build_manifest(folder, now=T0)
            state, reason = public_data.check_set(folder, bad_manifest)
            self.assertEqual(state, 'incomplete')
            self.assertIn('grok-bot', reason)

            # Undated kind='maintainer' fails check_set:
            accounts['same_model']['grok-bot']['kind'] = 'maintainer'
            accounts['same_model']['grok-bot'].pop('as_of', None)
            write_json(folder / 'accounts.json', accounts)
            undated_manifest = public_data.build_manifest(folder, now=T0)
            state2, reason2 = public_data.check_set(folder, undated_manifest)
            self.assertEqual(state2, 'incomplete')
            self.assertIn('as_of', reason2)

            # Uncited image_input without image_input_source fails check_set:
            accounts['same_model']['grok-bot']['as_of'] = '2026-11-11'
            accounts['models']['grok-bot'] = {'image_input': True}
            write_json(folder / 'accounts.json', accounts)
            uncited_mod = public_data.build_manifest(folder, now=T0)
            state3, reason3 = public_data.check_set(folder, uncited_mod)
            self.assertEqual(state3, 'incomplete')
            self.assertIn('image_input', reason3)

            # capacity_rule containing a hardcoded plan or price literal fails check_set:
            accounts['models'].pop('grok-bot', None)
            accounts['same_model']['grok-bot']['capacity_rule']['plan'] = 'premium'
            write_json(folder / 'accounts.json', accounts)
            hardcoded_plan = public_data.build_manifest(folder, now=T0)
            state4, reason4 = public_data.check_set(folder, hardcoded_plan)
            self.assertEqual(state4, 'incomplete')
            self.assertIn('capacity_rule', reason4)

            accounts['same_model']['grok-bot']['capacity_rule'].pop('plan', None)
            accounts['same_model']['grok-bot']['capacity_rule']['price_usd_per_month'] = 30
            write_json(folder / 'accounts.json', accounts)
            hardcoded_price = public_data.build_manifest(folder, now=T0)
            state5, reason5 = public_data.check_set(folder, hardcoded_price)
            self.assertEqual(state5, 'incomplete')
            self.assertIn('capacity_rule', reason5)

            # Restore valid accounts.json, then verify plan-templates.json rejects non-https vendor price_source:
            shutil.copyfile(public_data.BUNDLED / 'accounts.json', folder / 'accounts.json')
            templates = json.loads((folder / 'plan-templates.json').read_text(encoding='utf-8'))
            templates['grok']['premium']['price_source']['url'] = 'http://x.ai/insecure'
            write_json(folder / 'plan-templates.json', templates)
            bad_tpl = public_data.build_manifest(folder, now=T0)
            state6, reason6 = public_data.check_set(folder, bad_tpl)
            self.assertEqual(state6, 'incomplete')
            self.assertIn('plan-templates.json', reason6)


if __name__ == '__main__':
    unittest.main()
