"""Provider-discovery contract: actual bindings, isolated sources and no secrets/writes."""
import base64
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import socket
import unittest
from unittest.mock import patch

import allocator
import dispatch_registry
import host_policy
import provider_inventory as inventory


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='provider-discovery-test-')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.now = datetime(2026, 10, 26, 7, tzinfo=timezone.utc)
        self.env = {'PATH': ''}
        self.paseo({'codex': {'enabled': True}})
        self.native_codex('codex', 'account-A')

    def write(self, path, value):
        target = self.home / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value), encoding='utf-8')
        return target

    def paseo(self, providers):
        self.write('.paseo/config.json', {'agents': {'providers': providers}})

    def codex_auth(self, account, plan='pro'):
        claim = {'https://api.openai.com/auth': {'chatgpt_plan_type': plan,
                  'chatgpt_subscription_active_until': '2026-10-27T00:00:00Z'}}
        jwt = 'header.' + base64.urlsafe_b64encode(json.dumps(claim).encode()).decode().rstrip('=') + '.signature'
        return {'tokens': {'account_id': account, 'access_token': 'SECRET-' + account, 'id_token': jwt}}

    def native_codex(self, slot, account, plan='pro'):
        return self.write('.' + slot + '/auth.json', self.codex_auth(account, plan))

    def native_claude(self, slot, plan='pro'):
        return self.write('.' + slot + '/.credentials.json', {'claudeAiOauth': {
            'accessToken': 'SECRET-' + slot, 'subscriptionType': plan, 'rateLimitTier': 'default_claude_max_20x'}})

    def cc(self, rows):
        path = self.home / '.cc-switch/cc-switch.db'
        path.parent.mkdir(exist_ok=True)
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE providers (id TEXT, app_type TEXT, name TEXT, settings_config TEXT, category TEXT, is_current INTEGER)')
            db.executemany('INSERT INTO providers VALUES (?,?,?,?,?,?)', [
                (ident, family, name, json.dumps(config), 'official', current)
                for ident, family, name, config, current in rows])
            db.commit()
        return path



    def discover(self, publish=False):
        result = inventory.discover(self.home, self.env, self.now)
        if publish:
            inventory.write_json(self.home / '.route-manager/provider-inventory.json', result)
        return result

    def apply(self, snapshot):
        accounts = allocator.load_accounts(allocator.REFERENCES / 'accounts.json')
        return inventory.apply(accounts, snapshot)

    def test_new_launcher_later_signin_enrolls_via_real_account_loading(self):
        policy = self.write('host-policy.json', {'allowed_accounts': ['codex']})
        env = {'ROUTER_PROVIDER_HOME': str(self.home), 'ROUTER_STATE_DIR': str(self.home / '.route-manager'),
               'PASEO_HOME': str(self.home / '.paseo'), 'ROUTER_HOST_POLICY': str(policy),
               'ROUTER_CAPACITY': str(self.home / 'capacity.json'), 'PATH': ''}
        with patch.dict(inventory.os.environ, env):
            host_policy.sync_discovered(allocator.load_accounts(), enable=True)
            self.paseo({'codex': {'enabled': True}, 'codex-work': {'extends': 'codex', 'enabled': True, 'env': {'CODEX_HOME': str(self.home / '.codex-work')}}})
            waiting = allocator.load_accounts()
            self.assertNotIn('codex-work', host_policy.load_policy()['known_accounts'])
            self.assertNotEqual(waiting['_discovery']['accounts']['codex-work']['status'], 'ready')
            self.native_codex('codex-work', 'account-B')
            ready = allocator.load_accounts()
            self.assertEqual(ready['_host_policy_sync']['added'], ['codex-work'])
            before = policy.read_bytes()
            self.assertEqual(allocator.load_accounts()['_host_policy_sync']['added'], [])
            self.assertEqual(before, policy.read_bytes())
            document = host_policy.load_policy()
            document['allowed_accounts'].remove('codex-work')
            policy.write_text(json.dumps(document))
            self.paseo({'codex': {'enabled': True}})
            allocator.load_accounts()
            self.paseo({'codex': {'enabled': True}, 'codex-work': {'extends': 'codex', 'enabled': True, 'env': {'CODEX_HOME': str(self.home / '.codex-work')}}})
            allocator.load_accounts()
            self.assertFalse(host_policy.account_allowed(host_policy.load_policy(), 'codex-work'))

    def test_grok_local_sources_and_cache_only_availability(self):
        cached = {'providers': {'grok': {'plan_label': 'SuperGrok'}}}
        self.write('.usage-float/usage-cache.json', cached)
        self.assertEqual(self.discover()['accounts']['grok']['status'], 'missing_client')
        (self.home / '.grok').mkdir()
        row = self.discover()['accounts']['grok']
        self.assertEqual((row['status'], row['family'], row['billing'], row['plan']),
                         ('ready', 'grok', 'subscription', 'SuperGrok'))
        self.assertIn('native', row['sources'])
        self.paseo({'grok': {'extends': 'acp', 'enabled': False}})
        self.assertEqual(self.discover()['accounts']['grok']['status'], 'disabled')
        self.paseo({'grok': {'extends': 'acp', 'enabled': True}})
        self.assertEqual(self.discover()['launchers']['grok'], {'family': 'grok', 'status': 'ready'})

    def test_grok_native_cli_without_configuration_or_cache(self):
        with patch.object(inventory.shutil, 'which', side_effect=lambda name, **kwargs: 'grok.exe' if name == 'grok' else None):
            self.assertEqual(self.discover()['accounts']['grok']['status'], 'ready')

    def test_active_grokbuild_official_binds_health_and_disable(self):
        dbpath = self.cc([('official', 'grokbuild', 'Grok Official', {}, 1)])
        before = dbpath.read_bytes()
        row = self.discover()
        self.assertEqual(row['accounts']['grok']['status'], 'ready')
        self.assertEqual(row['saved'][0]['bound_to'], ['grok'])
        self.assertEqual(dbpath.read_bytes(), before)
        self.paseo({'grok': {'extends': 'acp', 'enabled': False}})
        self.assertEqual(self.discover()['accounts']['grok']['status'], 'disabled')
        self.paseo({'grok': {'extends': 'acp', 'enabled': True}})
        with closing(sqlite3.connect(dbpath)) as db:
            db.execute('CREATE TABLE provider_health (provider_id TEXT, app_type TEXT, is_healthy INTEGER)')
            db.execute("INSERT INTO provider_health VALUES ('official', 'grokbuild', 0)")
            db.commit()
        self.assertEqual(self.discover()['accounts']['grok']['status'], 'paused')

    def test_saved_grok_profile_does_not_prove_client_availability(self):
        self.cc([('saved', 'grok', 'Grok Official', {}, 0)])
        snapshot = self.discover()
        self.assertEqual(snapshot['accounts']['grok']['status'], 'missing_client')
        self.assertEqual(snapshot['saved'][0]['bound_to'], [])

    def test_grok_no_usage_is_eligible_without_unlimited_bonus_and_launchable(self):
        self.paseo({'grok': {'extends': 'acp', 'enabled': True}})
        snapshot = self.discover()
        accounts = allocator.load_accounts(allocator.REFERENCES / 'accounts.json')
        accounts['accounts'].pop('grok')  # custom catalog without a Grok template
        inventory.apply(accounts, snapshot)
        spec = accounts['accounts']['grok']
        self.assertTrue(spec['unmetered'])
        self.assertFalse(spec.get('unlimited', False))
        self.assertEqual(spec['windows'], {})
        with patch.object(allocator.host_policy, 'load_policy', return_value=None):
            result = allocator.plan({'work_kind': 'web_research', 'difficulty': 'routine', 'expected_hours': .1},
                                    {'accounts': {}}, self.now, accounts=accounts, overrides=[], records=[])
        candidate = next(c for c in result['ranking'] if c['id'] == 'grok47_high')
        self.assertEqual(candidate['excluded'], [])
        self.assertNotIn('free_usage', candidate['components'])
        with patch.object(inventory, 'current', return_value=snapshot):
            bundle = allocator.bundles(accounts)['grok47_high']
            request = dispatch_registry.launch_request(bundle, {'title': 'Grok fixture', 'initialPrompt': 'fixture'})
        self.assertEqual(request['provider'], 'grok/grok-4.7')
        self.assertNotIn('modeId', request['settings'])
        snapshot['accounts']['grok']['status'] = snapshot['launchers']['grok']['status'] = 'disabled'
        inventory.apply(accounts, snapshot)
        self.assertFalse(accounts['accounts']['grok']['verified'])
        self.assertIn('disabled', accounts['accounts']['grok']['bundles']['grok47_high']['discovery_unavailable'])
        with patch.object(inventory, 'current', return_value=snapshot):
            with self.assertRaisesRegex(ValueError, 'verified Paseo providers'):
                dispatch_registry.launch_request(bundle, {'title': 'Grok fixture', 'initialPrompt': 'fixture'})

    def test_reported_grok_quota_is_used_instead_of_unmetered(self):
        self.paseo({'grok': {'extends': 'acp', 'enabled': True}})
        self.write('.usage-float/usage-cache.json', {'providers': {'grok': {
            'windows': [{'id': 'credits', 'used_pct': 99}], 'available': True}}})
        accounts = self.apply(self.discover())
        spec = accounts['accounts']['grok']
        self.assertFalse(spec['unmetered'])
        self.assertIn('credits', spec['windows'])
        with patch.object(allocator.host_policy, 'load_policy', return_value=None):
            result = allocator.plan({'work_kind': 'web_research', 'difficulty': 'routine', 'expected_hours': .1},
                                    {'accounts': {'grok': {'windows': [{'id': 'credits', 'used_pct': 99,
                                        'remaining_pct_before_reset': 1, 'seconds_until_reset': 86400}]}}},
                                    self.now, accounts=accounts, overrides=[], records=[])
        rows = result['ranking'] + result.get('excluded', [])
        self.assertTrue(next(c for c in rows if c['id'] == 'grok47_high')['excluded'])

    def test_custom_launcher_and_proxy_advertised_models_get_scored_bundles(self):
        self.paseo({'research': {'extends': 'acp', 'enabled': True, 'models': ['moonshot/kimi-k3.1-pro-20261023']}})
        snapshot = self.discover()
        accounts = self.apply(snapshot)
        bundle = next(iter(accounts['accounts']['research']['bundles'].values()))
        self.assertEqual((bundle['model'], bundle['launch_model'], bundle['harness']),
                         ('kimi-k3.1-pro', 'moonshot/kimi-k3.1-pro-20261023', 'acp'))
        raw_models = ['vendor/claude-haiku-4-5@20251001', 'claude-opus-4-6', 'claude-sonnet-4-6',
                      'gateway/qwen3-coder-next:free', 'some-new-model', 'another-new-model',
                      'vendor-a/gpt-6.1-terra', 'vendor-b/gpt-6.1-terra']
        snapshot['accounts']['gateway'] = {'family': 'codex', 'provider': 'gateway', 'status': 'ready',
            'backend_kind': 'litellm', 'billing': 'metered', 'models': raw_models}
        snapshot['launchers']['gateway'] = {'family': 'codex', 'status': 'ready'}
        accounts = self.apply(snapshot)
        bundles = list(accounts['accounts']['gateway']['bundles'].values())
        self.assertEqual({b['launch_model'] for b in bundles}, set(raw_models))
        self.assertEqual({b['model'] for b in bundles}, {'claude-haiku-4-5', 'claude-opus-4-6',
            'claude-sonnet-4-6', 'qwen3-coder-next', 'some-new-model', 'another-new-model', 'gpt-6.1-terra'})
        self.assertEqual({b['model'] for b in bundles if b['model'].startswith('gpt-')}, {'gpt-6.1-terra'})
        self.assertTrue(all(allocator.model_spec(accounts, b['model'])['tier'] >= 3 for b in bundles))

    def test_bound_cc_switch_api_models_get_native_bundles_without_changing_signin(self):
        config = {'env': {'ANTHROPIC_BASE_URL': 'https://example.invalid',
                         'ANTHROPIC_AUTH_TOKEN': 'SECRET-gateway',
                         'ANTHROPIC_DEFAULT_HAIKU_MODEL': 'claude-haiku-4-5@20251001',
                         'ANTHROPIC_DEFAULT_OPUS_MODEL': 'claude-opus-4-6',
                         'ANTHROPIC_DEFAULT_SONNET_MODEL': 'claude-sonnet-4-6'}}
        settings = self.write('.claude/settings.json', config)
        self.cc([('default', 'claude', 'Gateway', config, 1)])
        before = settings.read_bytes()
        accounts = self.apply(self.discover())
        bundles = [b for b in accounts['accounts']['claude']['bundles'].values() if not b.get('discovery_unavailable')]
        self.assertEqual({b['model'] for b in bundles}, {'claude-haiku-4-5', 'claude-opus-4-6', 'claude-sonnet-4-6'})
        self.assertEqual({b.get('launch_model') for b in bundles}, set(config['env'].values()) - {'https://example.invalid', 'SECRET-gateway'})
        self.assertEqual(settings.read_bytes(), before)

    def test_route_manager_plugin_switch_pauses_dot_and_chatgpt_web_until_reenabled(self):
        self.write('.dot2api/client.json', {'base_url': 'http://127.0.0.1:8788', 'api_key': 'SECRET'})
        self.write('.codex-chatgpt-web-consult/config.json', {'mode': 'browser-only', 'host': '127.0.0.1'})
        accounts = self.discover()['accounts']
        self.assertEqual((accounts['dot']['status'], accounts['chatgpt-web']['status']), ('ready', 'ready'))
        for plugin_id, account in (('dot2api', 'dot'), ('chatgpt-web', 'chatgpt-web')):
            with self.subTest(plugin_id):
                self.write('.route-manager/plugins.json', {'v': 1, 'plugins': {plugin_id: {'enabled': False, 'autostart': True}}})
                accounts = self.discover()['accounts']
                self.assertEqual(accounts[account]['status'], 'paused')
                self.assertEqual(accounts[account]['reason'], 'plugin_disabled_in_route_manager')
                other = 'chatgpt-web' if account == 'dot' else 'dot'
                self.assertEqual(accounts[other]['status'], 'ready')
                bundles = self.apply(self.discover())['accounts'][account]['bundles'].values()
                self.assertTrue(all('plugin_disabled_in_route_manager' in b.get('discovery_unavailable', '') for b in bundles))
        self.write('.route-manager/plugins.json', {'v': 1, 'plugins': {'dot2api': {'enabled': True}}})
        self.assertEqual(self.discover()['accounts']['dot']['status'], 'ready')

    def test_cc_switch_identity_overrides_stale_current_flag_without_writing_products(self):
        db = self.cc([('a', 'codex', 'A', {'auth': self.codex_auth('account-A')}, 1),
                      ('b', 'codex', 'B', {'auth': self.codex_auth('account-B')}, 0)])
        before = hashlib.sha256(db.read_bytes()).hexdigest()
        first = self.discover()
        self.assertEqual([r['bound_to'] for r in first['saved']], [['codex'], []])
        self.native_codex('codex', 'account-B')
        second = self.discover()
        self.assertEqual([r['bound_to'] for r in second['saved']], [[], ['codex']])
        self.assertEqual(hashlib.sha256(db.read_bytes()).hexdigest(), before)
        serialized = json.dumps(second)
        self.assertNotIn('SECRET-', serialized)
        self.assertNotIn('id_token', serialized)

    def test_new_bound_launcher_is_routable_then_signout_and_disable_remove_it(self):
        self.paseo({'claude-work': {'extends': 'claude', 'enabled': True,
                    'env': {'CLAUDE_CONFIG_DIR': str(self.home / '.claude-work')}}})
        auth = self.native_claude('claude-work')
        snapshot = self.discover()
        accounts = self.apply(snapshot)
        spec = accounts['accounts']['claude-work']
        self.assertTrue(spec['verified'])
        self.assertEqual(spec['capacity_usd']['seven_day'], 156)
        bundle = next(b for b in allocator.bundles(accounts).values() if b['account'] == 'claude-work' and not b.get('disabled'))
        self.assertEqual(bundle['provider'], 'claude-work')
        self.assertEqual(bundle['modeId'], 'bypassPermissions')
        with patch.object(inventory, 'current', return_value=snapshot):
            request = dispatch_registry.launch_request(bundle, {'title': 'Fixture', 'initialPrompt': 'Fixture'})
        self.assertTrue(request['provider'].startswith('claude-work/'))
        auth.unlink()
        gone = self.discover()
        self.assertEqual(gone['accounts']['claude-work']['status'], 'missing_credentials')
        self.assertNotIn('claude-work', self.apply(gone)['accounts'])
        self.native_claude('claude-work')
        self.paseo({'claude-work': {'extends': 'claude', 'enabled': False,
                    'env': {'CLAUDE_CONFIG_DIR': str(self.home / '.claude-work')}}})
        self.assertEqual(self.discover()['accounts']['claude-work']['status'], 'disabled')




    def test_same_subscription_has_one_quota_owner_across_live_launchers(self):
        self.native_codex('codex-2', 'account-A')
        self.paseo({'codex-2': {'extends': 'codex', 'enabled': True,
                    'env': {'CODEX_HOME': str(self.home / '.codex-2')}}})
        snapshot = self.discover()
        self.assertEqual(snapshot['accounts']['codex-2']['quota_account'], 'codex')
        accounts = self.apply(snapshot)
        second = allocator.bundles(accounts)['sol_xhigh_2']
        self.assertEqual((second['account'], second['provider']), ('codex', 'codex-2'))
        self.assertFalse(accounts['accounts']['codex-2']['bundles'])
        self.write('.usage-float/usage-cache.json', {'providers': {'codex': {
            'credential_key': snapshot['accounts']['codex']['credential_key']}}})
        self.paseo({'codex': {'enabled': False}, 'codex-2': {'extends': 'codex', 'enabled': True,
                    'env': {'CODEX_HOME': str(self.home / '.codex-2')}}})
        accounts = self.apply(self.discover())
        usage = inventory.merge_usage({'accounts': {'codex': {'status': 'fresh', 'windows': [{'used_pct': 98}]}}}, accounts, self.now)
        self.assertEqual(usage['accounts']['codex-2']['windows'][0]['used_pct'], 98)

    def test_changed_credentials_clear_old_quota_and_unknown_plan_capacity(self):
        old = self.discover()['accounts']['codex']['credential_key']
        self.write('.usage-float/usage-cache.json', {'providers': {'codex': {'credential_key': old}}})
        self.native_codex('codex', 'account-B', plan='unknown-plan')
        snapshot = self.discover()
        accounts = self.apply(snapshot)
        merged = inventory.merge_usage({'accounts': {'codex': {'status': 'fresh', 'windows': [1]}}}, accounts, self.now)
        self.assertEqual(merged['accounts']['codex']['windows'], [])
        self.assertNotIn('capacity_usd', accounts['accounts']['codex'])
        self.assertIsNone(accounts['accounts']['codex']['expires_at'])

    def test_corrupt_cc_db_and_magpie_config_do_not_remove_native_account(self):
        path = self.home / '.cc-switch/cc-switch.db'
        path.parent.mkdir()
        path.write_bytes(b'not sqlite')
        other = self.home / '.config/magpie/providers.json'
        other.parent.mkdir(parents=True)
        other.write_text('{broken')
        snapshot = self.discover()
        self.assertEqual(snapshot['sources']['cc-switch'], 'unreadable')
        self.assertEqual(snapshot['sources']['magpie_config'], 'unreadable')
        self.assertEqual(snapshot['accounts']['codex']['status'], 'ready')
        for path, document in (('.claude/.credentials.json', {'claudeAiOauth': ['SECRET-bad-shape']}),
                               ('.claude/settings.json', {'usageFloat': True, 'env': []}),
                               ('.config/magpie/providers.json', {'providers': 123})):
            self.write(path, document)
        self.assertEqual(self.discover()['accounts']['codex']['status'], 'ready')

    def test_magpie_product_config_binds_by_endpoint_and_key_and_observes_off(self):
        self.write('.claude/settings.json', {'env': {'ANTHROPIC_BASE_URL': 'https://api.example.test',
                                                    'ANTHROPIC_AUTH_TOKEN': 'SECRET-api'}})
        path = self.write('.config/magpie/providers.json', {'providers': [{
            'id': 'custom', 'name': 'Custom', 'anthropic': 'https://api.example.test/v1/messages',
            'key': 'SECRET-api', 'models': ['claude-sonnet-5-5'], 'off': True}]})
        before = path.read_bytes()
        snapshot = self.discover()
        self.assertEqual(snapshot['accounts']['claude']['status'], 'paused')
        self.assertEqual(snapshot['saved'][0]['bound_to'], ['claude'])
        self.assertEqual(path.read_bytes(), before)
        provider = json.loads(path.read_text())['providers'][0]
        provider.update(off=False, key='SECRET-other', keys=[{'key': 'SECRET-api', 'off': True}])
        self.write('.config/magpie/providers.json', {'providers': [provider]})
        self.assertEqual(self.discover(publish=True)['accounts']['claude']['status'], 'paused')
        provider['keys'][0]['off'] = False
        self.write('.config/magpie/providers.json', {'providers': [provider]})
        self.assertEqual(self.discover(publish=True)['accounts']['claude']['status'], 'ready')

    def test_explicit_disable_and_model_policy_win_over_stale_verified_flag(self):
        self.native_claude('claude-2')
        self.paseo({'claude-2': {'extends': 'claude', 'enabled': False,
                    'env': {'CLAUDE_CONFIG_DIR': str(self.home / '.claude-2')}}})
        accounts = self.apply(self.discover())
        bundles = allocator.bundles(accounts)
        self.assertIn('disabled', bundles['sonnet55_high_2']['discovery_unavailable'])
        self.assertIn('disabled', bundles['opus55_high_2'])
        task = {'objective': 'Fixture', 'expected_hours': 0.1, 'needs': {'local_only': True}, 'include_unverified': True}
        with patch.object(allocator.host_policy, 'load_policy', return_value=None):
            plan = allocator.plan(task, {'accounts': {}}, self.now, accounts=accounts, overrides=[])
        row = next(r for r in plan['ranking'] if r['id'] == 'sonnet55_high_2')
        self.assertTrue(any('provider discovery: disabled' in reason for reason in row['excluded']))

    def test_launcher_uses_its_effective_credentials_not_its_display_identity(self):
        self.native_claude('claude', 'max')
        self.native_claude('claude-2', 'pro')
        self.paseo({'claude': {'enabled': False}, 'claude-2': {'extends': 'claude', 'enabled': True,
                    'env': {'CLAUDE_CONFIG_DIR': str(self.home / '.claude-2'),
                            'CLAUDE_SECURESTORAGE_CONFIG_DIR': str(self.home / '.claude')}}})
        snapshot = self.discover()
        self.assertEqual(snapshot['accounts']['claude-2']['status'], 'ready')
        self.assertEqual(snapshot['accounts']['claude-2']['plan'], 'max20')
        self.assertEqual(snapshot['accounts']['claude']['status'], 'disabled')
        # A custom API key is usable configuration, but does not inherit OAuth quota.
        self.write('.claude/settings.json', {'env': {'ANTHROPIC_API_KEY': 'SECRET-payg'}})
        self.paseo({'claude': {'enabled': True}})
        row = self.discover()['accounts']['claude']
        self.assertEqual((row['status'], row['billing'], row['plan']), ('ready', 'metered', None))
        self.write('.claude/settings.json', {'env': {'ANTHROPIC_BASE_URL': 'https://gateway.example.test/v1/',
                                                    'ANTHROPIC_AUTH_TOKEN': 'SECRET-gateway'}})
        self.write('.claude/usage-float.json', {'source': 'magpie', 'account': 'private@example.test'})
        fingerprint = inventory.digest('magpie', 'https://gateway.example.test/v1', 'SECRET-gateway', 'claude', 'private@example.test')
        self.write('.usage-float/usage-cache.json', {'providers': {'claude': {
            'credential_key': fingerprint, 'plan_label': 'pro'}}})
        row = self.discover()['accounts']['claude']
        self.assertEqual((row['usage_binding'], row['billing'], row['plan']), ('matches', 'subscription', 'pro'))




    def test_local_magpie_pause_revoke_restore_and_secret_free_snapshot(self):
        login = {'id': 'saved-A', 'agent': 'codex', 'user': 'private@example.test',
                 'auth': self.codex_auth('account-A'), 'plan': 'pro', 'on': False}
        self.write('.config/magpie/logins.json', [login])
        first = self.discover(publish=True)
        # The active native account is usable even without the backup 'on' flag.
        self.assertEqual(first['accounts']['codex']['status'], 'ready')
        self.assertIn('magpie-logins', first['accounts']['codex']['sources'])
        for flag in ('paused', 'lapsed', 'hidden'):
            self.write('.config/magpie/logins.json', [{**login, flag: True}])
            stopped = self.discover(publish=True)
            self.assertEqual(stopped['accounts']['codex']['status'], 'paused')
            spec = self.apply(stopped)['accounts']['codex']
            self.assertFalse(spec['verified'])
            self.assertTrue(all(b.get('discovery_unavailable') for b in spec['bundles'].values()))
            self.write('.config/magpie/logins.json', [login])
            self.assertEqual(self.discover(publish=True)['accounts']['codex']['status'], 'ready')
        text = (self.home / '.route-manager/provider-inventory.json').read_text()
        self.assertNotIn('SECRET-', text)
        self.assertNotIn('private@example.test', text)
        self.assertNotIn('id_token', text)

    def test_magpie_backup_account_off_is_distinct_from_current_account(self):
        self.native_codex('codex-2', 'account-B')
        self.paseo({'codex-2': {'extends': 'codex', 'enabled': True,
                    'env': {'CODEX_HOME': str(self.home / '.codex-2')}}})
        login = {'id': 'saved-B', 'agent': 'codex', 'auth': self.codex_auth('account-B'), 'on': False}
        self.write('.config/magpie/logins.json', [login])
        first = self.discover(publish=True)
        self.assertEqual(first['accounts']['codex-2']['status'], 'paused')
        self.assertEqual(first['accounts']['codex']['status'], 'ready')
        self.write('.config/magpie/logins.json', [{**login, 'on': True}])
        self.assertEqual(self.discover()['accounts']['codex-2']['status'], 'ready')

    def test_magpie_builtin_subscription_flags_and_default_model_picks(self):
        provider = {'id': 'codex', 'models': []}
        for flags, status in (({'off': True}, 'paused'), ({'hidden': True}, 'paused'), ({}, 'ready')):
            self.write('.config/magpie/providers.json', {'providers': [{**provider, **flags}]})
            result = self.discover(publish=True)
            self.assertEqual(result['accounts']['codex']['status'], status)
            self.assertNotIn('models', result['accounts']['codex'])
        self.write('.config/magpie/providers.json', {'providers': [{**provider, 'noModels': True}]})
        accounts = self.apply(self.discover(publish=True))
        self.assertTrue(all(b.get('discovery_unavailable') for b in accounts['accounts']['codex']['bundles'].values()))
        # Built-in entries store custom picks: removing the picks restores defaults.
        self.write('.config/magpie/providers.json', {'providers': []})
        self.assertEqual(self.discover(publish=True)['accounts']['codex']['status'], 'ready')

    def test_cc_health_pauses_only_bound_account_and_recovers(self):
        db = self.cc([('a', 'codex', 'A', {'auth': self.codex_auth('account-A')}, 0)])
        with closing(sqlite3.connect(db)) as con:
            con.execute('CREATE TABLE provider_health (provider_id TEXT, app_type TEXT, is_healthy INTEGER)')
            con.execute('INSERT INTO provider_health VALUES (?,?,?)', ('a', 'codex', 0))
            con.commit()
        self.assertEqual(self.discover(publish=True)['accounts']['codex']['reason'], 'unhealthy_in_cc_switch')
        with closing(sqlite3.connect(db)) as con:
            con.execute('UPDATE provider_health SET is_healthy=1')
            con.commit()
        # The stored current flag stays false: recovery follows health/binding.
        self.assertEqual(self.discover(publish=True)['accounts']['codex']['status'], 'ready')

    def test_product_removal_stays_paused_until_restored_or_identity_changes(self):
        config = {'auth': self.codex_auth('account-A')}
        db = self.cc([('a', 'codex', 'A', config, 1)])
        self.discover(publish=True)
        with closing(sqlite3.connect(db)) as con:
            con.execute('DELETE FROM providers')
            con.commit()
        for _ in range(3):
            row = self.discover(publish=True)['accounts']['codex']
            self.assertEqual((row['status'], row['reason']), ('paused', 'removed_from_cc-switch'))
        self.native_codex('codex', 'account-B')
        self.assertEqual(self.discover(publish=True)['accounts']['codex']['status'], 'ready')
        self.native_codex('codex', 'account-A')
        with closing(sqlite3.connect(db)) as con:
            con.execute('INSERT INTO providers VALUES (?,?,?,?,?,?)', ('a', 'codex', 'A', json.dumps(config), 'official', 1))
            con.commit()
        self.assertEqual(self.discover(publish=True)['accounts']['codex']['status'], 'ready')

    def test_confirmed_pause_survives_unreadable_source_then_recovers(self):
        login = {'id': 'a', 'agent': 'codex', 'auth': self.codex_auth('account-A'), 'paused': True}
        path = self.write('.config/magpie/logins.json', [login])
        self.discover(publish=True)
        path.write_text('{broken')
        for _ in range(2):
            self.assertEqual(self.discover(publish=True)['accounts']['codex']['status'], 'paused')
        self.write('.config/magpie/logins.json', [{**login, 'paused': False}])
        self.assertEqual(self.discover(publish=True)['accounts']['codex']['status'], 'ready')

    def test_portable_magpie_and_relocated_cc_switch_use_product_data_paths(self):
        exe = self.home / 'apps/magpie/magpie.exe'
        exe.parent.mkdir(parents=True)
        exe.touch()
        (exe.parent / '.portable').touch()
        self.env['MAGPIE_EXE'] = str(exe)
        self.write('apps/magpie/data/logins.json', [{'agent': 'codex', 'id': 'a',
                   'auth': self.codex_auth('account-A'), 'paused': True}])
        db = self.cc([('a', 'codex', 'A', {'auth': self.codex_auth('account-A')}, 1)])
        moved = self.home / 'relocated-cc'
        moved.mkdir()
        (moved / 'cc-switch.db').write_bytes(db.read_bytes())
        self.write('AppData/Roaming/com.ccswitch.desktop/app_paths.json', {'app_config_dir_override': str(moved)})
        snapshot = self.discover()
        self.assertEqual(Path(snapshot['source_paths']['magpie']), exe.parent / 'data')
        self.assertEqual(Path(snapshot['source_paths']['cc-switch']), moved / 'cc-switch.db')
        self.assertEqual(snapshot['accounts']['codex']['status'], 'paused')

    def test_discovery_is_local_only_and_does_not_read_vps_connection(self):
        self.write('.route-manager/vps-magpie/connection.json', {'base_url': 'https://never-contact.test', 'api_key': 'SECRET-unused'})
        reader = inventory.read_json
        def local_read(path, *args):
            self.assertNotIn('vps-magpie', path.parts)
            return reader(path, *args)
        with patch.object(inventory, 'read_json', side_effect=local_read), \
                patch.object(socket, 'create_connection', side_effect=AssertionError('network forbidden')):
            result = self.discover()
        self.assertNotIn('magpie_connection', result['sources'])
        self.assertEqual(result['sources']['magpie_logins'], 'missing')

    def test_claude_pause_uses_account_identity_across_token_refresh(self):
        auth_path = self.native_claude('claude', 'max')
        profile = {'accountUuid': 'stable-account', 'organizationUuid': 'stable-org'}
        self.write('.claude.json', {'oauthAccount': profile})
        auth = json.loads(auth_path.read_text())
        login = {'agent': 'claude', 'id': 'primary', 'auth': auth, 'profile': profile, 'paused': True}
        self.write('.config/magpie/logins.json', [login])
        first = self.discover(publish=True)
        auth['claudeAiOauth']['accessToken'] = 'SECRET-refreshed'
        self.write('.claude/.credentials.json', auth)
        after = self.discover()
        self.assertEqual(after['accounts']['claude']['identity'], first['accounts']['claude']['identity'])
        self.assertEqual(after['accounts']['claude']['status'], 'paused')
        # Refusal belongs to one credential version; an explicit pause belongs to the account.
        login.update(paused=False, lapsed='sign in again', refused=inventory.digest('SECRET-claude', ''))
        self.write('.config/magpie/logins.json', [login])
        self.assertEqual(self.discover()['accounts']['claude']['status'], 'ready')
        login['refused'] = inventory.digest('SECRET-refreshed', '')
        self.write('.config/magpie/logins.json', [login])
        self.assertEqual(self.discover()['accounts']['claude']['status'], 'paused')

    def test_collector_auth_refusal_pauses_only_matching_credential_and_recovers(self):
        bound = self.discover()['accounts']['codex']['credential_key']
        for error, status in (('登录凭据需由 Codex 更新', 'paused'), ('更新失败', 'ready'), (None, 'ready')):
            self.write('.usage-float/usage-cache.json', {'providers': {'codex': {
                'credential_key': bound, 'error': error, 'stale': bool(error)}}})
            self.assertEqual(self.discover(publish=True)['accounts']['codex']['status'], status)
        self.write('.usage-float/usage-cache.json', {'providers': {'codex': {
            'credential_key': bound, 'error': '登录凭据需由 Codex 更新', 'stale': True}}})
        self.native_codex('codex', 'account-B')
        self.assertEqual(self.discover(publish=True)['accounts']['codex']['status'], 'ready')

    def test_unconfigured_fixed_slots_are_absent_and_custom_magpie_provider_is_synthesized(self):
        self.native_claude('claude', 'max')
        self.native_codex('codex-team', 'account-team')
        self.paseo({
            'claude': {'enabled': True},
            'codex-team': {
                'extends': 'codex',
                'enabled': True,
                'env': {'CODEX_HOME': str(self.home / '.codex-team')},
            },
            'claude-magpie': {
                'extends': 'claude',
                'enabled': True,
                'env': {
                    'CLAUDE_CONFIG_DIR': str(self.home / '.claude-magpie'),
                    'ANTHROPIC_BASE_URL': 'https://gateway.example.test/v1',
                    'ANTHROPIC_AUTH_TOKEN': 'SECRET-magpie-gw',
                },
            },
        })
        fingerprint = inventory.digest('magpie', 'https://gateway.example.test/v1', 'SECRET-magpie-gw', 'claude', '')
        self.write('.usage-float/usage-cache.json', {'providers': {'claude-magpie': {
            'credential_key': fingerprint, 'plan_label': 'pro', 'backend_kind': 'gateway'}}})
        snapshot = self.discover()
        self.assertNotIn('claude-2', snapshot['accounts'])
        self.assertNotIn('codex-2', snapshot['accounts'])
        self.assertEqual(snapshot['accounts']['claude-magpie']['usage_binding'], 'matches')
        self.assertEqual(snapshot['accounts']['claude-magpie']['billing'], 'subscription')
        self.assertEqual(snapshot['accounts']['claude-magpie']['plan'], 'pro')
        applied = self.apply(snapshot)
        accounts = applied['accounts']
        self.assertIn('codex-team', applied['dispatch_efforts'])
        self.assertNotIn('claude-2', accounts)
        self.assertNotIn('codex-2', accounts)
        magpie_spec = accounts['claude-magpie']
        self.assertTrue(magpie_spec['verified'])
        self.assertEqual(magpie_spec['capacity_usd']['seven_day'], 156)
        self.assertEqual(magpie_spec['windows']['five_hour']['launch_min_remaining_pct'], 25)
        self.assertIn('disabled', magpie_spec['bundles']['claude-magpie__opus55_high'])
        self.assertNotIn('disabled', magpie_spec['bundles']['claude-magpie__sonnet55_high'])
        override_path = self.write('.route-manager/accounts-override.json', {
            'accounts': {
                'claude-magpie': {
                    'bundles': {
                        'claude-magpie__sonnet55_high': {'disabled': 'user disabled'}
                    }
                }
            }
        })
        with patch.object(allocator, 'accounts_override_path', return_value=override_path), \
                patch.object(inventory, 'current', return_value=snapshot):
            merged = allocator.load_accounts()
            self.assertEqual(
                merged['accounts']['claude-magpie']['bundles']['claude-magpie__sonnet55_high'].get('disabled'),
                'user disabled',
            )

    def test_pi_and_litellm_proxy_auto_enrolled_into_router(self):
        self.write('.pi/agent/auth.json', {
            'openai-codex': {
                'type': 'oauth',
                'access': 'pi-standalone-token',
                'refresh': 'pi-refresh',
                'accountId': 'standalone-pi-acct',
            }
        })
        self.write('.route-manager/accounts-override.json', {
            'accounts': {
                'team-proxy': {
                    'backend_kind': 'litellm',
                    'endpoint': 'https://proxy.example.test',
                    'api_key': 'sk-team-proxy',
                    'models': ['gpt-6.1-sol', 'claude-sonnet-5-5'],
                }
            }
        })
        self.write('.usage-float/usage-cache.json', {
            'providers': {
                'team-proxy': {
                    'plan_label': '$300',
                    'backend_kind': 'litellm',
                    'billing': 'metered',
                }
            }
        })
        snapshot = self.discover()
        self.assertEqual(snapshot['accounts']['pi']['status'], 'ready')
        self.assertEqual(snapshot['accounts']['team-proxy']['status'], 'ready')
        self.assertEqual(snapshot['accounts']['team-proxy']['budget_usd'], 300.0)
        applied = self.apply(snapshot)
        accounts = applied['accounts']
        self.assertIn('pi', accounts)
        self.assertTrue(accounts['pi']['verified'])
        self.assertTrue(accounts['pi']['bundles'])
        self.assertIn('team-proxy', accounts)
        self.assertEqual(accounts['team-proxy']['billing'], 'metered')
        self.assertEqual(accounts['team-proxy']['capacity_usd']['spend'], 300.0)
        self.assertIn('team-proxy__gpt_6_1_sol_high', accounts['team-proxy']['bundles'])
        self.assertIn('team-proxy__claude_sonnet_5_5_high', accounts['team-proxy']['bundles'])
        self.assertEqual(accounts['team-proxy']['bundles']['team-proxy__gpt_6_1_sol_high']['harness'], 'codex')
        self.assertEqual(accounts['team-proxy']['bundles']['team-proxy__gpt_6_1_sol_high']['modeId'], 'full-access')
        self.assertEqual(accounts['team-proxy']['bundles']['team-proxy__claude_sonnet_5_5_high']['harness'], 'claude')
        self.assertEqual(accounts['team-proxy']['bundles']['team-proxy__claude_sonnet_5_5_high']['modeId'], 'bypassPermissions')
        override_file = self.home / '.route-manager/accounts-override.json'
        self.assertEqual(allocator.accounts_override_path({'ROUTE_MANAGER_ACCOUNTS_OVERRIDE': str(override_file)}), override_file)
        self.assertEqual(allocator.accounts_override_path({'ROUTER_ACCOUNTS_OVERRIDE': str(override_file)}), override_file)


if __name__ == '__main__':
    unittest.main()
