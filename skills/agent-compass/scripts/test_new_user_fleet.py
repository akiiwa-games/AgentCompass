"""Shipped account template and unverified-account routing. Does not read the user home."""
import json
import unittest

import account_fleet
import provider_inventory
import route_judge


class ShippedFleetTests(unittest.TestCase):
    def test_shipped_accounts_are_neutral_and_unverified(self):
        document = json.loads((account_fleet.REFERENCES / 'accounts.json').read_text(encoding='utf-8-sig'))
        names = set(document['accounts'])
        self.assertTrue({'claude', 'codex', 'antigravity', 'opencode', 'pi', 'grok', 'dot', 'chatgpt-web'} <= names)
        for fixture_only in ('claude-2', 'codex-2', 'claude-lab', 'codex-work'):
            self.assertNotIn(fixture_only, names)
        text = json.dumps(document['accounts'])
        for phrase in ('Max 20x', 'second account', 'User 2026', 'Claude Pro', 'ChatGPT Pro', 'own OpenAI', 'api_key'):
            self.assertNotIn(phrase, text)
        for spec in document['accounts'].values():
            self.assertIs(spec.get('verified'), False)
        self.assertIn('argon_high', document['accounts']['antigravity']['bundles'])
        self.assertIn('pi', names)
        self.assertIn('pi_sol_high', document['accounts']['codex']['bundles'])
        self.assertEqual(document['accounts']['pi']['bundles'], {})




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
