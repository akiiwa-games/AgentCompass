"""Shipped account template and unverified-account routing. Does not read the user home."""
import json
import unittest

import allocator
import provider_inventory
import route_judge


class ShippedFleetTests(unittest.TestCase):
    def test_shipped_accounts_are_neutral_and_unverified(self):
        document = json.loads((allocator.REFERENCES / 'accounts.json').read_text(encoding='utf-8-sig'))
        names = set(document['accounts'])
        self.assertTrue({'claude', 'codex', 'antigravity', 'opencode', 'pi', 'grok', 'dot', 'chatgpt-web'} <= names)
        self.assertNotIn('claude-2', names)
        self.assertNotIn('codex-2', names)
        text = json.dumps(document['accounts'])
        for phrase in ('Max 20x', 'second account', 'User 2026', 'Claude Pro', 'ChatGPT Pro', 'own OpenAI', 'api_key'):
            self.assertNotIn(phrase, text)
        for spec in document['accounts'].values():
            self.assertIs(spec.get('verified'), False)
        self.assertIn('argon_high', document['accounts']['antigravity']['bundles'])
        self.assertIn('pi', names)
        self.assertIn('pi_sol_high', document['accounts']['codex']['bundles'])
        self.assertEqual(document['accounts']['pi']['bundles'], {})

    def test_reference_fixture_is_inside_the_repository(self):
        fixture = allocator.reference_fleet_path()
        self.assertTrue(fixture.is_file())
        self.assertEqual(fixture.parent.name, 'fixtures')
        self.assertIn('claude-2', json.loads(fixture.read_text(encoding='utf-8-sig'))['accounts'])


class UnverifiedPickTests(unittest.TestCase):
    def test_ready_but_unverified_is_not_a_confident_pick(self):
        accounts = {
            '_discovery': {'accounts': {'opencode': {'status': 'ready', 'family': 'opencode'}}},
            'accounts': {'opencode': {'verified': False}},
        }
        gap = route_judge.readiness_gap(accounts, {'read_ok': True})
        self.assertEqual((gap['code'], gap['fatal']), ('no_ready_account', True))
        self.assertIn('agentcompass setup', gap['message'])

    def test_ready_without_a_spec_stays_launchable(self):
        accounts = {'_discovery': {'accounts': {'claude': {'status': 'ready', 'family': 'claude'}}}}
        self.assertIsNone(route_judge.readiness_gap(accounts, {'read_ok': True}))

    def test_installed_cli_without_proof_stays_unverified(self):
        row = {'family': 'antigravity', 'status': 'ready', 'sources': ['native']}
        self.assertFalse(provider_inventory.launch_verified(row, False))
        self.assertFalse(provider_inventory.sign_in_proven(row))
        self.assertEqual(provider_inventory.account_state_label(row), 'installed, sign-in unverified')

    def test_saved_fleet_and_usage_cache_stay_launchable(self):
        installed = {'family': 'antigravity', 'status': 'ready', 'sources': ['native']}
        self.assertTrue(provider_inventory.launch_verified(installed, True))
        proven = {'family': 'grok', 'status': 'ready', 'sources': ['native', 'usage-cache']}
        self.assertTrue(provider_inventory.launch_verified(proven, False))
        self.assertTrue(provider_inventory.sign_in_proven(proven))
        # Doctor and setup still say the sign-in cannot be read from the CLI itself.
        self.assertEqual(provider_inventory.account_state_label(proven), 'installed, sign-in unverified')

    def test_claude_sign_in_is_still_taken_from_discovery(self):
        row = {'family': 'claude', 'status': 'ready', 'sources': ['native']}
        self.assertTrue(provider_inventory.launch_verified(row, False))
        self.assertEqual(provider_inventory.account_state_label(row), 'ready')


class DemoWithoutInputTests(unittest.TestCase):
    def test_demo_plan_uses_the_built_in_task_when_no_input_is_given(self):
        import contextlib
        import io
        from unittest import mock

        out = io.StringIO()
        argv = ['route_judge.py', 'plan', '--demo', '--dry-run']
        with mock.patch.object(route_judge.sys, 'argv', argv), contextlib.redirect_stdout(out):
            code = route_judge.main()
        self.assertEqual(code, 0)
        self.assertIn('DEMO:', out.getvalue())
        self.assertIn('winner', out.getvalue())

    def test_plan_without_input_or_demo_is_still_refused(self):
        import contextlib
        import io
        from unittest import mock

        out = io.StringIO()
        argv = ['route_judge.py', 'plan', '--dry-run']
        with mock.patch.object(route_judge.sys, 'argv', argv), contextlib.redirect_stdout(out):
            code = route_judge.main()
        self.assertNotEqual(code, 0)
        self.assertIn('plan requires --input', out.getvalue())
