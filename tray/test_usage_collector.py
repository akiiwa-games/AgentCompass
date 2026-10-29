"""No network or live cache access: verify the collector's policy boundary."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import usage_collector as collector


class CollectorPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile = self.root/'host-policy.json'
        # Collectors without a discovery API read the router's inventory; names are arbitrary.
        self.inventory = {'accounts': {'claude': {'family': 'claude'}, 'codex': {'family': 'codex'},
                                       'codex-work': {'family': 'codex'},
                                       'codex-alias': {'family': 'codex', 'quota_account': 'codex-work'},
                                       'antigravity': {'family': 'antigravity'}, 'dot': {'family': 'dot'}}}
        self.fallback = ['claude', 'codex', 'codex-work', 'antigravity']
        self.usage = SimpleNamespace(CACHE_PATH=self.root/'cache.json',
            active_providers=Mock(side_effect=lambda names: names), fetch_all=Mock(return_value=[]),
            mark_provider_stale=Mock())
        for patcher in (patch.dict(sys.modules, {'usage_float': self.usage}),
                        patch.object(collector.allocator, 'load_accounts', return_value={}),
                        patch.object(collector.allocator.provider_inventory, 'current', return_value=self.inventory),
                        patch.object(collector, 'STATE', self.root),
                        patch.object(collector.host_policy, 'policy_path', return_value=(self.profile, False))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_host_policy_subset_collects_only_codex_and_antigravity(self):
        self.profile.write_text(json.dumps({'allowed_accounts': ['codex', 'antigravity', 'dot', 'chatgpt-web'],
            'judges': ['codex'], 'usage_providers': ['codex', 'antigravity']}))
        result = collector.collect()
        self.usage.active_providers.assert_called_once_with(['codex', 'antigravity'])
        self.usage.fetch_all.assert_called_once_with(['codex', 'antigravity'], force=False)
        self.assertEqual(result['requested_providers'], ['codex', 'antigravity'])

    def test_collector_without_discovery_api_uses_router_inventory(self):
        collector.collect()
        self.usage.active_providers.assert_called_once_with(self.fallback)

    def test_empty_policy_does_not_fetch_default_accounts(self):
        self.profile.write_text('{"allowed_accounts":[]}')
        collector.collect()
        self.usage.active_providers.assert_called_once_with([])
        self.usage.fetch_all.assert_called_once_with([], force=False)

    def test_client_with_missing_credentials_does_not_mutate_shared_snapshot(self):
        self.usage.active_providers.return_value = []
        self.usage.active_providers.side_effect = None
        result = collector.collect()
        self.assertEqual(result['not_detected'], self.fallback)
        self.usage.mark_provider_stale.assert_not_called()

    def test_invalid_policy_does_not_fetch(self):
        self.profile.write_text('{"allow_accounts":["codex"]}')
        with self.assertRaises(collector.host_policy.HostPolicyError):
            collector.collect()
        self.usage.active_providers.assert_not_called()
        self.usage.fetch_all.assert_not_called()

    def test_dynamic_discovery_uses_usage_float_discovered_accounts(self):
        self.usage.discover_claude_homes = Mock(return_value={'claude': self.root / '.claude', 'claude-magpie': self.root / '.claude-magpie'})
        self.usage.discover_codex_accounts = Mock(return_value={'codex': object(), 'codex-work': object()})
        result = collector.collect()
        expected = ['claude', 'claude-magpie', 'codex', 'codex-work', 'grok', 'antigravity']
        self.usage.active_providers.assert_called_once_with(expected)
        self.assertEqual(result['requested_providers'], expected)
        self.assertEqual(result['collection_mode'], 'direct')

    def test_pooled_account_collects_its_source_meter_when_only_it_is_allowed(self):
        self.profile.write_text(json.dumps({'allowed_accounts': ['grok-bot']}))
        collector.allocator.load_accounts.return_value = {'quota_sources': {'grok-bot': 'grok'}}
        self.usage.active_providers.side_effect = lambda names: [p for p in names if p == 'grok']
        result = collector.collect()
        self.assertEqual(result['requested_providers'], ['grok'])
        self.usage.fetch_all.assert_called_once_with(['grok'], force=False)


if __name__ == '__main__':
    unittest.main()
