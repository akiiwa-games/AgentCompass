"""A Claude/Codex slot without a login gets no account card; a fresh install shows none."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

import router_data

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills' / 'agent-compass' / 'scripts'))
import provider_inventory as real_inventory


def cards_for(discovered: dict) -> list[str]:
    allocator, router = Mock(), Mock()
    inventory = Mock(quota_sources=real_inventory.quota_sources)
    # Every name has a template entry with bundles, as the shipped accounts.json does.
    allocator.load_accounts.return_value = {'accounts': {
        name: {'windows': {}, 'bundles': {'b': {'provider': name, 'model': 'm'}}} for name in discovered},
        '_discovery': {'accounts': discovered}}
    allocator.host_policy.load_policy.return_value = None
    allocator.host_policy.account_allowed.return_value = True
    router.usage_snapshot.return_value = {'accounts': {}}
    with patch.object(router_data, 'modules', return_value=(allocator, router)), \
         patch.dict(sys.modules, {'provider_inventory': inventory}), \
         patch.object(router_data, '_read_json', return_value={}), \
         patch.object(router_data, 'pool_reservations', return_value={}), \
         patch.object(router_data.router_priority, 'targets', return_value=[]), \
         patch.object(router_data.plugins, 'load', return_value={}):
        return [card['account'] for card in router_data.account_cards()]


class SignedOutCardTests(unittest.TestCase):
    def test_clean_install_without_logins_shows_no_claude_or_codex_card(self):
        # Shape of `agentcompass providers` in a clean Windows Sandbox.
        discovered = {name: {'family': name, 'provider': name, 'sources': ['native'],
                             'status': 'missing_credentials', 'credential_key': None, 'identity': None}
                      for name in ('claude', 'codex')}
        self.assertEqual(cards_for(discovered), [])

    def test_disabled_unsigned_claude_hides_while_gateway_and_signed_slots_stay(self):
        discovered = {
            # Installed CLI and a disabled Paseo entry, never signed in.
            'claude': {'family': 'claude', 'sources': ['native', 'paseo'], 'status': 'disabled',
                       'credential_key': None, 'identity': None},
            # Second account through a token gateway: discovery binds an identity.
            'claude-2': {'family': 'claude', 'sources': ['native', 'paseo'], 'status': 'ready',
                         'credential_key': 'k2', 'identity': 'i2'},
            'codex': {'family': 'codex', 'sources': ['native'], 'status': 'ready',
                      'credential_key': 'k', 'identity': 'i'},
            # Families whose sign-in discovery cannot check keep their card.
            'antigravity': {'family': 'antigravity', 'sources': ['native'], 'status': 'ready'},
        }
        self.assertEqual(sorted(cards_for(discovered)), ['antigravity', 'claude-2', 'codex'])


if __name__ == '__main__':
    unittest.main()
