"""The headless collector against the real collector core: only the provider HTTP transport is replaced."""
import io
import json
import sys
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import host_policy
import router_test_support as support
import usage_cache
import usage_collector as collector
import usage_core
import venice_usage


class Response:
    def __init__(self, payload, status=200):
        self.status, self.headers, self._raw = status, {}, json.dumps(payload).encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class CollectorBase(unittest.TestCase):
    def setUp(self):
        self.root = support.isolated_router_state(self.addCleanup, discovery=False, policy=False)
        patcher = patch.dict(usage_cache.os.environ)     # the fixture pins ROUTER_USAGE_CACHE; a real default install has none
        patcher.start()
        self.addCleanup(patcher.stop)
        usage_cache.os.environ.pop('ROUTER_USAGE_CACHE', None)
        self.home = self.root
        self.state = self.root / '.agent-compass'
        self.cache = self.state / 'usage-cache.json'
        self.profile = self.root / 'host-policy.json'
        self.requests = []
        self.codex_used = 25.0
        stack = ExitStack()
        self.addCleanup(stack.close)
        codex_home, codex_home_2 = self.root / '.codex', self.root / '.codex-2'
        accounts = {'codex': usage_core.CodexAccount('codex', 'codex', codex_home)}
        for name, value in (('HOME', self.root), ('STATE_DIR', self.state), ('CACHE_PATH', self.cache),
                            ('CONFIG_PATH', self.state / 'usage-config.json'), ('LEGACY_CONFIG_PATH', self.root / '.usage-float' / 'config.json'),
                            ('CODEX_HOME', codex_home), ('CODEX_HOME_2', codex_home_2), ('CODEX_ACCOUNT_BY_ID', accounts),
                            ('CLAUDE_HOME', self.root / '.claude'), ('CLAUDE_HOME_2', self.root / '.claude-2'),
                            ('GROK_HOME', self.root / '.grok')):
            stack.enter_context(patch.object(usage_core, name, value))
        stack.enter_context(patch.object(collector, 'STATE', self.state))
        stack.enter_context(patch.object(venice_usage, 'key', return_value=None))
        stack.enter_context(patch.object(usage_core.urllib.request, 'urlopen', side_effect=self.transport))
        stack.enter_context(patch.object(host_policy, 'policy_path', return_value=(self.profile, False)))
        # No agy CLI, no Pi: only the credentials a test writes exist.
        stack.enter_context(patch.object(usage_core, 'find_antigravity_cli', return_value=None))

    def transport(self, request, timeout=None):
        url = request.full_url
        self.requests.append(url)
        now = datetime.now(timezone.utc)
        if 'chatgpt.com' in url:
            return Response({'plan_type': 'plus', 'rate_limit': {'primary_window': {
                'used_percent': self.codex_used, 'limit_window_seconds': 18000, 'reset_at': (now + timedelta(hours=3)).timestamp()}}})
        if 'api.anthropic.com/api/oauth/usage' in url:
            return Response({'five_hour': {'utilization': 10, 'resets_at': (now + timedelta(hours=2)).isoformat()}})
        raise AssertionError(f'unexpected request {url}')

    def sign_in(self, *names):
        for name in names:
            if name == 'codex':
                (self.root / '.codex').mkdir(exist_ok=True)
                (self.root / '.codex' / 'auth.json').write_text(json.dumps({'tokens': {'access_token': 'codex-token'}}))
            if name == 'claude':
                (self.root / '.claude').mkdir(exist_ok=True)
                (self.root / '.claude' / '.credentials.json').write_text(json.dumps({'claudeAiOauth': {'accessToken': 'claude-token'}}))

    def hosts(self):
        return sorted({url.split('/')[2] for url in self.requests})


class CollectorTests(CollectorBase):
    def test_the_collector_modules_do_not_import_the_optional_plugin(self):
        import ast
        forbidden = {'usage_float', 'usage_lock', 'magpie_usage', 'plugins'}
        for module in (usage_core, collector, usage_cache, venice_usage, sys.modules['usage_magpie']):
            tree = ast.parse(Path(module.__file__).read_text(encoding='utf-8'))
            imported = {alias.name.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
            imported |= {node.module.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
            self.assertEqual(imported & forbidden, set(), module.__name__)
        self.assertNotIn('usage_float', sys.modules)

    def test_collects_every_signed_in_account_and_writes_the_canonical_cache(self):
        self.sign_in('codex', 'claude')
        result = collector.collect()
        self.assertEqual(result['collection_mode'], 'direct')
        self.assertEqual(result['cache_path'], str(self.cache))
        self.assertEqual(sorted(result['providers']), ['claude', 'codex'])
        self.assertEqual(result['not_detected'], ['grok', 'antigravity'])
        self.assertEqual(self.hosts(), ['api.anthropic.com', 'chatgpt.com'])
        document = json.loads(self.cache.read_text(encoding='utf-8'))
        self.assertEqual(document['providers']['codex']['windows'][0]['used_pct'], 25.0)
        self.assertTrue((self.state / 'usage-collector.json').is_file())
        self.assertFalse((self.root / '.usage-float').exists())

    def test_host_policy_subset_collects_only_the_allowed_providers(self):
        self.sign_in('codex', 'claude')
        self.profile.write_text(json.dumps({'allowed_accounts': ['codex', 'antigravity', 'dot', 'chatgpt-web'],
                                            'judges': ['codex'], 'usage_providers': ['codex', 'antigravity']}))
        result = collector.collect()
        self.assertEqual(result['requested_providers'], ['codex', 'antigravity'])
        self.assertEqual(sorted(result['providers']), ['codex'])
        self.assertEqual(self.hosts(), ['chatgpt.com'])        # claude is signed in but not allowed on this host

    def test_empty_policy_fetches_nothing(self):
        self.sign_in('codex', 'claude')
        self.profile.write_text('{"allowed_accounts":[]}')
        result = collector.collect()
        self.assertEqual((result['requested_providers'], result['providers'], self.requests), ([], {}, []))

    def test_invalid_policy_fetches_nothing_and_raises(self):
        self.sign_in('codex')
        self.profile.write_text('{"allow_accounts":["codex"]}')
        with self.assertRaises(host_policy.HostPolicyError):
            collector.collect()
        self.assertEqual(self.requests, [])
        self.assertFalse(self.cache.exists())

    def test_an_account_without_credentials_is_reported_not_marked_stale(self):
        self.sign_in('codex')
        result = collector.collect()
        self.assertEqual(sorted(result['providers']), ['codex'])
        self.assertEqual(result['not_detected'], ['claude', 'grok', 'antigravity'])
        self.assertEqual(sorted(json.loads(self.cache.read_text())['providers']), ['codex'])

    def test_discovery_includes_named_claude_and_codex_homes(self):
        (self.root / '.claude-work').mkdir()
        (self.root / '.codex-lab').mkdir()
        self.assertEqual(collector.discovered_subscriptions(),
                         ['claude', 'claude-work', 'codex', 'codex-lab', 'grok', 'antigravity'])

    def test_a_pooled_account_collects_its_source_meter_when_only_it_is_allowed(self):
        self.sign_in('codex')
        self.profile.write_text(json.dumps({'allowed_accounts': ['codex-alias']}))
        with patch.object(collector.allocator, 'load_accounts', return_value={'quota_sources': {'codex-alias': 'codex'}}):
            result = collector.collect()
        self.assertEqual(result['requested_providers'], ['codex'])
        self.assertEqual(sorted(result['providers']), ['codex'])

    def test_second_pass_inside_the_poll_interval_does_not_ask_the_provider_again(self):
        self.sign_in('codex')
        collector.collect()
        first = len(self.requests)
        self.codex_used = 90.0
        collector.collect()
        self.assertEqual(len(self.requests), first)
        self.assertEqual(json.loads(self.cache.read_text())['providers']['codex']['windows'][0]['used_pct'], 25.0)

    def test_forced_refresh_reads_again_past_the_interval(self):
        self.sign_in('codex')
        collector.collect()
        self.codex_used = 90.0
        usage_core.fetch_all(['codex'], force=True)
        self.assertEqual(json.loads(self.cache.read_text())['providers']['codex']['windows'][0]['used_pct'], 90.0)

    def test_a_failing_provider_keeps_the_old_stamp_and_marks_the_row_stale(self):
        self.sign_in('codex')
        collector.collect()
        document = json.loads(self.cache.read_text())
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        document['providers']['codex']['fetched_at'] = old
        document.pop('attempts', None)
        self.cache.write_text(json.dumps(document))
        self.transport = lambda request, timeout=None: Response({'error': 'down'}, 500)
        with patch.object(usage_core.urllib.request, 'urlopen', side_effect=self.transport):
            collector.collect()
        row = json.loads(self.cache.read_text())['providers']['codex']
        self.assertEqual((row['fetched_at'], row['stale']), (old, True))

    def test_main_prints_one_receipt_and_logs_only_the_class_of_a_failure(self):
        self.sign_in('codex')
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(collector.main(['--once']), 0)
        self.assertEqual(sorted(json.loads(out.getvalue())['providers']), ['codex'])
        self.profile.write_text('{"nope": 1}')
        with redirect_stdout(io.StringIO()):
            self.assertEqual(collector.main(['--once']), 1)
        self.assertIn('requires allowed_accounts', (self.state / 'usage-collector.log').read_text())
        with patch.object(collector, 'collect', side_effect=RuntimeError('secret remote text')), redirect_stdout(io.StringIO()):
            self.assertEqual(collector.main(['--once']), 1)
        self.assertNotIn('secret remote text', (self.state / 'usage-collector.log').read_text())


class MigrationTests(CollectorBase):
    """The collector next to a Usage Float that wrote the legacy cache."""

    def legacy_cache(self, **providers):
        path = self.root / '.usage-float' / 'usage-cache.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'providers': providers, 'attempts': {}}))
        return path

    def test_first_write_seeds_the_canonical_cache_from_legacy_and_never_writes_legacy(self):
        self.sign_in('codex')
        fetched = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        legacy = self.legacy_cache(grok={'provider_id': 'grok', 'available': True, 'stale': False, 'fetched_at': fetched,
                                         'windows': [{'id': 'credits', 'label': 'c', 'used_pct': 5.0}]})
        before = legacy.read_bytes()
        collector.collect()
        providers = json.loads(self.cache.read_text())['providers']
        self.assertEqual(sorted(providers), ['codex', 'grok'])
        self.assertEqual(providers['grok']['fetched_at'], fetched)
        self.assertEqual(legacy.read_bytes(), before)

    def test_a_corrupt_cache_is_set_aside_not_overwritten_and_not_blocking(self):
        self.sign_in('codex')
        self.state.mkdir(parents=True)
        self.cache.write_text('{not json')
        collector.collect()
        [aside] = self.state.glob('usage-cache.json.corrupt-*')
        self.assertEqual(aside.read_text(), '{not json')
        self.assertEqual(sorted(json.loads(self.cache.read_text())['providers']), ['codex'])

    def test_a_named_cache_is_used_without_seeding(self):
        self.sign_in('codex')
        self.legacy_cache(grok={'provider_id': 'grok', 'available': True, 'stale': False,
                                'fetched_at': datetime.now(timezone.utc).isoformat(), 'windows': []})
        named = self.root / 'named.json'
        with patch.object(usage_core, 'CACHE_PATH', named):
            collector.collect()
        self.assertEqual(sorted(json.loads(named.read_text())['providers']), ['codex'])
        self.assertFalse(self.cache.exists())


if __name__ == '__main__':
    unittest.main()
