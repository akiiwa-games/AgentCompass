"""Derived state is rebuilt or skipped by the product, and every refusal that remains prints a fix that works.

AGENTS.md: caches, ledgers and other derived state are rebuilt or skipped, not migrated. An older shape of the dispatch
database or of the provider inventory is not a fault the user has to clear by deleting a file; a file that cannot be
carried is refused with a fix that works when followed and says what deleting it discards."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = Path(__file__).resolve().parent
for _entry in (str(REPO_ROOT), str(SCRIPTS_DIR), str(REPO_ROOT / 'setup'), str(REPO_ROOT / 'tray')):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import account_fleet  # noqa: E402
import accounts_override  # noqa: E402
import dispatch_registry  # noqa: E402
import host_policy  # noqa: E402
import host_rules  # noqa: E402
import provider_inventory  # noqa: E402
import quota_overrides  # noqa: E402
import state_upgrade  # noqa: E402
import user_scores  # noqa: E402
from router_test_support import isolated_router_state, profile  # noqa: E402

NOW = datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc)


class DerivedStateHealsTests(unittest.TestCase):
    def setUp(self):
        self.root = isolated_router_state(self.addCleanup, policy=False)
        self.state = Path(os.environ['ROUTER_STATE_DIR'])
        self.state.mkdir(parents=True, exist_ok=True)
        self.db = self.state / 'dispatch.sqlite3'
        self.accounts = account_fleet.load_accounts(account_fleet.reference_fleet_path())
        self.accounts['accounts']['codex']['verified'] = True
        self.bundle = dict(self.accounts['accounts']['codex']['bundles']['sol_high'], bundle_id='sol_high',
                           account='codex', provider='codex')
        self.usage = {'accounts': {'codex': {'status': 'fresh', 'fetched_at': NOW.isoformat(), 'windows': [
            {'id': 'usage', 'used_pct': 0, 'remaining_pct_before_reset': 100, 'seconds_until_reset': 86400,
             'resets_at': (NOW + timedelta(days=1)).isoformat()}]}}}

    def record(self, task_id, profile_doc):
        task = {'objective': 'Implement feature', 'task_id': task_id, 'parent_agent_id': 'p1', 'expected_hours': 0.5}
        with patch.object(account_fleet, 'load_accounts', return_value=self.accounts), \
                patch.object(dispatch_registry.provider_inventory, 'launcher_family', return_value='codex'):
            rec = dispatch_registry.issue(
                {'stage': 'plan', 'choice': 'sol_high', 'selected_bundle': self.bundle, 'decision': {'action': 'start'}},
                task, now=NOW, path=self.db, allocation_task={**task, 'profile': profile_doc})
        return task, rec['decision_id']

    def prepare(self, task, decision_id):
        with patch.object(dispatch_registry.provider_inventory, 'launcher_family', return_value='codex'):
            return dispatch_registry.prepare(
                decision_id, task, {'title': 'worker', 'initialPrompt': 'do work'}, self.usage,
                accounts=self.accounts, now=NOW, path=self.db)

    @staticmethod
    def old_profile():
        return {**profile(), 'realtime_loop': False,
                'axis_demand': {'divergence': 1, 'global_rigor': 2, 'module_precision': 2, 'vision_tools': 0}}

    def test_a_decision_of_an_older_profile_is_refused_alone_and_does_not_block_a_new_dispatch(self):
        old_task, old_id = self.record('t-old', self.old_profile())
        new_task, new_id = self.record('t-new', profile())
        with self.assertRaises(ValueError) as caught:
            self.prepare(old_task, old_id)
        message = str(caught.exception)
        self.assertIn(old_id, message)
        self.assertIn('was recorded by an earlier version of AgentCompass', message)
        self.assertNotIn(str(self.db), message)          # the database is not the fault: nothing asks to delete it
        self.assertNotIn('delete', message)
        self.assertEqual(self.prepare(new_task, new_id)['state'], 'prepared')
        with closing(sqlite3.connect(self.db, isolation_level=None)) as connection:     # the old decision is neither rewritten nor reinterpreted
            stored = json.loads(connection.execute('SELECT data FROM decisions WHERE id=?', (old_id,)).fetchone()[0])
        self.assertEqual(stored['allocation_task']['profile'], self.old_profile())

    def test_a_prepared_row_of_a_decision_with_an_older_profile_is_not_resumed(self):
        task, decision_id = self.record('t-1', profile())
        self.assertEqual(self.prepare(task, decision_id)['state'], 'prepared')
        with closing(sqlite3.connect(self.db, isolation_level=None)) as connection:
            doc = json.loads(connection.execute('SELECT data FROM decisions WHERE id=?', (decision_id,)).fetchone()[0])
            doc['allocation_task']['profile'] = self.old_profile()
            connection.execute('UPDATE decisions SET data=? WHERE id=?', (json.dumps(doc), decision_id))
        with self.assertRaisesRegex(ValueError, 'was recorded by an earlier version of AgentCompass'):
            self.prepare(task, decision_id)

    def test_a_decision_whose_stored_task_carries_a_field_agentcompass_sets_is_refused_as_an_earlier_versions(self):
        # Rule (dispatch_registry.earlier_version): v0.7.0's `record --fast` wrote force_fast into the stored task. The
        # caller-task check runs only where a caller's task enters, so prepare answers with the one earlier-version
        # refusal, naming the field, and a decision of the current shape still prepares.
        for field, value in (('force_fast', True), ('pinned_bundle', 'sol_high'), ('needs', {'force_fast': True})):
            with self.subTest(field=field):
                task = {'objective': 'Implement feature', 'task_id': f't-{field}', 'parent_agent_id': 'p1',
                        'expected_hours': 0.5}
                with patch.object(account_fleet, 'load_accounts', return_value=self.accounts), \
                        patch.object(dispatch_registry.provider_inventory, 'launcher_family', return_value='codex'):
                    old_id = dispatch_registry.issue(
                        {'stage': 'plan', 'choice': 'sol_high', 'selected_bundle': self.bundle,
                         'decision': {'action': 'start'}},
                        task, now=NOW, path=self.db,
                        allocation_task={**task, 'profile': profile(), field: value})['decision_id']
                with self.assertRaises(ValueError) as caught:
                    self.prepare(task, old_id)
                message = str(caught.exception)
                self.assertIn('was recorded by an earlier version of AgentCompass', message)
                self.assertIn('roll or record it again', message)
                self.assertIn(field if field != 'needs' else 'needs.force_fast', message)
                self.assertNotIn('invalid_task_input', message)
        new_task, new_id = self.record('t-current', profile())
        self.assertEqual(self.prepare(new_task, new_id)['state'], 'prepared')

    def test_a_database_without_tables_is_created_on_use_and_doctor_does_not_name_it(self):
        sqlite3.connect(self.db).close()                  # an empty database file: no decisions, no dispatches
        self.assertIsNone(state_upgrade.check_dispatch_db(self.db))
        task, decision_id = self.record('t-1', profile())
        self.assertEqual(self.prepare(task, decision_id)['state'], 'prepared')

    def test_a_database_that_cannot_be_read_still_fails_and_the_fix_it_names_works(self):
        self.db.write_bytes(b'this is not a sqlite database' * 40)
        issue = state_upgrade.check_dispatch_db(self.db)
        self.assertIn('corrupt', issue)
        self.assertIn('discards the recorded decisions', issue)
        with self.assertRaises(sqlite3.DatabaseError):
            self.record('t-1', profile())
        self.db.unlink()                                  # the fix the message gives
        task, decision_id = self.record('t-1', profile())
        self.assertEqual(self.prepare(task, decision_id)['state'], 'prepared')

    def test_doctor_names_no_cache_that_is_read_as_empty_and_rebuilt(self):
        for name in ('usage-activity.json', 'usage-cache.json', 'live-models.json', 'model-latch.json',
                     'provider-retry.json', 'model-catalog.json'):
            (self.state / name).write_text('{not json', encoding='utf-8')
        (self.state / 'usage-activity.json').write_text(json.dumps({'schema': 'route-manager.activity.v1'}), encoding='utf-8')
        issues = state_upgrade.collect_issues(self.root, {'ROUTER_STATE_DIR': str(self.state)})
        self.assertEqual(issues, [])


class InventoryReplacedTests(unittest.TestCase):
    def setUp(self):
        self.root = isolated_router_state(self.addCleanup, policy=False, discovery=False)
        self.state = Path(os.environ['ROUTER_STATE_DIR'])
        self.state.mkdir(parents=True, exist_ok=True)
        provider_inventory._LAST_PUBLISHED_INVENTORY.clear()
        self.addCleanup(provider_inventory._LAST_PUBLISHED_INVENTORY.clear)

    SCHEMA = provider_inventory.INVENTORY_SCHEMA

    def tombstone_inventory(self, schema):
        # A credential the user removed in the launcher: its removal mark and its paused row.
        return {'schema': schema, 'saved': [], 'accounts': {'acct': {'identity': 'id-1', 'status': 'paused'}},
                'removed': [{'entry': 'e1', 'source': 'cc-switch', 'account': 'acct', 'identity': 'id-1'}]}

    def applied(self, previous):
        """What discovery does with `previous` on a credential that is installed again: pause it for the mark."""
        result = {'schema': self.SCHEMA, 'saved': [], 'sources': {},
                  'accounts': {'acct': {'identity': 'id-1', 'status': 'bound'}}}
        provider_inventory.apply_removals(result, previous)
        return result

    def test_an_inventory_of_another_schema_without_removals_does_not_stop_discovery_and_is_replaced(self):
        target = self.state / 'provider-inventory.json'
        for old in ({'schema': 'route-manager.providers.v1', 'accounts': {'x': {}}}, {'accounts': {}},
                    {'schema': 'route-manager.providers.v1', 'removed': []}):
            with self.subTest(old=old):
                target.write_text(json.dumps(old), encoding='utf-8')
                current = provider_inventory.current()
                self.assertEqual(current['schema'], self.SCHEMA)
                self.assertEqual(json.loads(target.read_text(encoding='utf-8'))['schema'], self.SCHEMA)
                issues = state_upgrade.collect_issues(self.root, {'ROUTER_STATE_DIR': str(self.state)})
                self.assertFalse([issue for issue in issues if 'provider-inventory' in issue])
                provider_inventory._LAST_PUBLISHED_INVENTORY.clear()

    def test_the_removals_of_every_released_schema_are_carried_and_never_dropped_by_a_publish(self):
        # Rule: a credential the user removed stays paused. v0.1.0 to v0.3.1 wrote this shape under the schema name
        # `route-manager.providers.v2.local`; the current schema is read as it is.
        target = self.state / 'provider-inventory.json'
        for schema in ('route-manager.providers.v2.local', self.SCHEMA):
            with self.subTest(schema=schema):
                target.write_text(json.dumps(self.tombstone_inventory(schema)), encoding='utf-8')
                result = self.applied(provider_inventory.previous_inventory(target))
                self.assertEqual(result['accounts']['acct']['status'], 'paused')
                self.assertEqual(len(result['removed']), 1)

    def test_an_unreadable_inventory_fails_with_its_message_and_the_fix_it_names_works(self):
        target = self.state / 'provider-inventory.json'
        for bad in (json.dumps(self.tombstone_inventory(self.SCHEMA))[:40], '', '["not", "a", "dict"]'):
            with self.subTest(bad=bad):
                target.write_text(bad, encoding='utf-8')
                with self.assertRaises(ValueError) as caught:
                    provider_inventory.current()
                self.assertIn(f'Fix: delete "{target}" (this discards which credentials you removed in the launcher',
                              str(caught.exception))
                self.assertEqual(str(caught.exception), state_upgrade.check_provider_inventory(target))
                self.assertTrue(any(str(target) in issue for issue in state_upgrade.collect_issues(
                    self.root, {'ROUTER_STATE_DIR': str(self.state)})))
        target.unlink()                                   # follow it
        self.assertEqual(provider_inventory.current()['schema'], self.SCHEMA)

    def test_an_inventory_of_an_unknown_schema_that_holds_removals_is_refused_not_dropped(self):
        target = self.state / 'provider-inventory.json'
        target.write_text(json.dumps(self.tombstone_inventory('route-manager.providers.v1')), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'holds removals that cannot be read'):
            provider_inventory.current()


class OneReaderOfAccountsOverrideTests(unittest.TestCase):
    def test_the_usage_collector_reads_a_disabled_reason_string_as_disabled_like_every_other_reader(self):
        import usage_core
        root = isolated_router_state(self.addCleanup, policy=False)
        path = root / 'accounts-override.json'
        spec = {'billing': 'metered', 'endpoint': 'https://api.example.test/v1', 'api_key': 'k'}
        path.write_text(json.dumps({'schema_version': 2, 'accounts': {
            'off-by-reason': {**spec, 'disabled': 'switched off in the tray'}, 'on': {**spec, 'disabled': False}}}),
            encoding='utf-8')
        with patch.dict(os.environ, {'AGENTCOMPASS_ACCOUNTS_OVERRIDE': str(path)}):
            self.assertEqual(accounts_override.load_override(path)['accounts']['off-by-reason']['disabled'], True)
            found = usage_core.discover_metered_usd_accounts()
        self.assertNotIn('off-by-reason', found)
        self.assertIn('on', found)


class RefusalFixesWorkWhenFollowedTests(unittest.TestCase):
    def setUp(self):
        self.root = isolated_router_state(self.addCleanup, policy=False)
        self.state = Path(os.environ['ROUTER_STATE_DIR'])
        self.state.mkdir(parents=True, exist_ok=True)

    def test_accounts_override_without_an_upgrade_step_names_deleting_and_what_it_discards(self):
        path = self.state / 'accounts-override.json'
        path.write_text(json.dumps({'accounts': {'acct-a': {'verified': True}}}), encoding='utf-8')   # v0.1.0 full copy
        with self.assertRaises(ValueError) as caught:
            accounts_override.load_override(path)
        self.assertIn(f'Fix: delete "{path}" (this discards every account choice in it', str(caught.exception))
        path.unlink()                                      # follow it: the file is absent, the tray writes a new one
        self.assertIsNone(accounts_override.load_override(path))

    def test_allowed_cwd_next_to_cwd_regex_is_fixed_by_the_edit_it_names_without_losing_anything_else(self):
        path = self.state / 'accounts-override.json'
        doc = {'schema_version': 2, 'accounts': {'acct-a': {
            'verified': True, 'allowed_cwd': ['D:/work'], 'cwd_regex': 'game', 'bundles': {'b1': {'disabled': True}}}}}
        path.write_text(json.dumps(doc), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, r'Fix: edit the file: remove accounts\.acct-a\.allowed_cwd') as caught:
            accounts_override.load_override(path)
        # v0.5.0 applied both, so the edit widens the account: the text says so instead of "nothing else is lost".
        self.assertIn('widens the account', str(caught.exception))
        self.assertNotIn('nothing else is lost', str(caught.exception))
        del doc['accounts']['acct-a']['allowed_cwd']       # follow it
        path.write_text(json.dumps(doc), encoding='utf-8')
        # read through the upgrade step (accounts_override.LOCAL_UPGRADES): the schema-2 file in the current shape
        self.assertEqual(accounts_override.load_override(path), {**doc, 'schema_version': accounts_override.SCHEMA_VERSION})

    def test_an_allowed_cwd_entry_that_is_not_carried_names_the_cwd_regex_to_write_and_the_edit_works(self):
        path = self.state / 'accounts-override.json'
        doc = {'schema_version': 2, 'accounts': {'acct-a': {'verified': True, 'allowed_cwd': ['~/proj']}}}
        path.write_text(json.dumps(doc), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, r'Fix: edit the file: replace accounts\.acct-a\.allowed_cwd with accounts\.acct-a\.cwd_regex'):
            accounts_override.load_override(path)
        doc['accounts']['acct-a'] = {'verified': True, 'cwd_regex': '^D:/work/proj(/|$)'}                 # follow it
        path.write_text(json.dumps(doc), encoding='utf-8')
        # read through the upgrade step (accounts_override.LOCAL_UPGRADES): the schema-2 file in the current shape
        self.assertEqual(accounts_override.load_override(path), {**doc, 'schema_version': accounts_override.SCHEMA_VERSION})

    def test_host_policy_fix_by_editing_by_creating_or_by_deleting_works(self):
        path = self.state / 'host-policy.json'
        with patch.dict(os.environ, {'ROUTER_HOST_POLICY': str(path)}):
            path.write_text(json.dumps({'allowed_accounts': ['acct-a'], 'legacy_mode': True}), encoding='utf-8')
            with self.assertRaises(host_policy.HostPolicyError) as caught:
                host_policy.load_policy()
            self.assertEqual(str(caught.exception), state_upgrade.check_host_policy(path))
            self.assertIn('which accounts take part in automatic routing on this machine; every account does again',
                          str(caught.exception))
            self.assertNotIn('re-toggle', str(caught.exception))
            path.write_text(json.dumps({'allowed_accounts': ['acct-a']}), encoding='utf-8')          # edit
            self.assertEqual(host_policy.load_policy(), {'allowed_accounts': ['acct-a']})
            path.unlink()
            with self.assertRaisesRegex(host_policy.HostPolicyError, 'unset ROUTER_HOST_POLICY'):
                host_policy.load_policy()                  # an explicit path must exist: the fix says to create it
            path.write_text(json.dumps({'allowed_accounts': ['acct-a']}), encoding='utf-8')          # create
            self.assertEqual(host_policy.load_policy(), {'allowed_accounts': ['acct-a']})
        with patch.object(host_policy, 'policy_path', return_value=(path, False)):
            path.write_text('{"x": 1}', encoding='utf-8')
            with self.assertRaises(host_policy.HostPolicyError):
                host_policy.load_policy()
            path.unlink()                                  # delete: the default, every account allowed
            self.assertIsNone(host_policy.load_policy())

    def test_user_scores_fix_names_what_is_lost_and_deleting_then_setting_again_works(self):
        path = self.state / 'user-scores.json'
        path.write_text(json.dumps({'schema_version': 2, 'scores': {'model-a': {'no_such_category': {
            'level': 2, 'as_of': '2026-11-12', 'kind': 'user'}}}}), encoding='utf-8')
        with self.assertRaises(ValueError) as caught:
            user_scores.load_local(path)
        self.assertIn('this discards every score you set yourself', str(caught.exception))
        self.assertNotIn('re-toggle', str(caught.exception))
        path.unlink()
        user_scores.set_score('model-a', 'coding', 2.0, local_path=path, as_of='2026-11-18')
        self.assertEqual(user_scores.load_local(path)['scores']['model-a']['coding']['level'], 2.0)

    def test_overlay_fix_by_editing_or_by_deleting_works(self):
        directory = self.state / 'host-rules'
        directory.mkdir(parents=True)
        overlay = directory / 'shared.md'
        overlay.write_text('run @@ROUTE_JUDGE@@ plan\n', encoding='utf-8')
        with self.assertRaises(ValueError) as caught:
            host_rules.load('shared', self.root)
        self.assertIn('this discards your overlay', str(caught.exception))
        overlay.write_text('run @@AGENTCOMPASS@@ plan\n', encoding='utf-8')                           # edit, as the text says
        self.assertEqual(host_rules.load('shared', self.root)[1], 'overlay')
        overlay.unlink()                                                                              # delete
        self.assertEqual(host_rules.load('shared', self.root)[1], 'default')

    def test_a_corrupt_overrides_file_names_what_deleting_discards_and_deleting_works(self):
        path = self.state / 'router-overrides.json'
        path.write_text('{not json', encoding='utf-8')
        with self.assertRaises(ValueError) as caught:
            quota_overrides.load_overrides(NOW, path=path)
        self.assertIn('this discards the temporary priorities it holds', str(caught.exception))
        path.unlink()
        self.assertEqual(quota_overrides.load_overrides(NOW, path=path), [])

    def test_an_untimestamped_baseline_is_not_a_refusal(self):
        path = self.state / 'router-overrides.json'
        path.write_text(json.dumps({'overrides': [{'id': 'o1', 'stop_on_reset': True,
                                                   'baseline_windows': {'usage': {'used_pct': 50}}}]}), encoding='utf-8')
        self.assertIsNone(state_upgrade.check_router_overrides(path))

    def test_no_refusal_still_asks_for_a_tray_toggle_that_cannot_work(self):
        for name in ('accounts_override', 'host_policy', 'user_scores', 'state_upgrade'):
            source = (SCRIPTS_DIR / f'{name}.py').read_text(encoding='utf-8')
            self.assertNotIn('re-toggle in the tray', source, name)
            self.assertNotIn('FIX_TRAY', source, name)


if __name__ == '__main__':
    unittest.main()
