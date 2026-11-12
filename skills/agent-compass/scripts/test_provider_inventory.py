"""Provider-discovery contract: actual bindings, isolated sources and no secrets/writes."""
import base64
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import socket
import unittest
from unittest.mock import patch

import allocator
import account_fleet
import accounts_override
import axis_scores
import candidate_eval
import dispatch_registry
import host_policy
import provider_inventory as inventory

from router_test_support import isolated_router_state, temporary_root, usage_windows


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup, discovery=False, policy=False)



def community_capacity(family, plan, window):
    """The cited community capacity of one plan window: the only capacity a subscription account has."""
    return axis_scores.load_five_axis()['capacity'][family][plan][window]['capacity_usd']


def overlaid_capacity(accounts, name):
    """The capacity the router prices an account's windows at (community data by plan)."""
    return axis_scores.five_axis_overlay(accounts, axis_scores.load_five_axis())['accounts'][name].get('capacity_usd')

class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.home = temporary_root(self.addCleanup)
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
            inventory.write_json(self.home / '.agent-compass/provider-inventory.json', result)
        return result

    def apply(self, snapshot):
        accounts = account_fleet.load_accounts(account_fleet.reference_fleet_path())
        return inventory.apply(accounts, snapshot)

    def live_plan(self, **plans):
        """A live reading's plan per account, bound to its current credential the way the collector caches it: the
        usage API's plan_type (Codex) or the OAuth profile (Claude, plan_source profile_api). A login file never sets
        the plan."""
        bound = self.discover()['accounts']
        window = lambda wid: [{'id': wid, 'label': wid, 'used_pct': 10.0, 'resets_at': '2026-10-28T00:00:00+00:00'}]
        self.write('.agent-compass/usage-cache.json', {'providers': {name: {
            'provider_id': name, 'display_name': name, 'available': True, 'stale': False,
            'fetched_at': self.now.isoformat(), 'credential_key': bound[name]['credential_key'], 'plan_label': label,
            **({'plan_source': 'profile_api'} if bound[name]['family'] == 'claude' else {}),
            'windows': window('seven_day' if bound[name]['family'] == 'claude' else 'usage')}
            for name, label in plans.items()}})

    def test_new_launcher_later_signin_enrolls_via_real_account_loading(self):
        policy = self.write('host-policy.json', {'allowed_accounts': ['codex'], 'known_accounts': ['codex', 'claude']})
        env = {'ROUTER_PROVIDER_HOME': str(self.home), 'ROUTER_STATE_DIR': str(self.home / '.agent-compass'),
               'PASEO_HOME': str(self.home / '.paseo'), 'ROUTER_HOST_POLICY': str(policy),
               'ROUTER_CAPACITY': str(self.home / 'capacity.json'), 'PATH': ''}
        with patch.dict(inventory.os.environ, env):
            first = account_fleet.load_accounts()
            policy.write_text(json.dumps({
                'allowed_accounts': ['codex'],
                'known_accounts': sorted(first['_discovery']['accounts']),
            }))
            host_policy.sync_discovered(account_fleet.load_accounts())
            self.paseo({'codex': {'enabled': True}, 'codex-work': {'extends': 'codex', 'enabled': True, 'env': {'CODEX_HOME': str(self.home / '.codex-work')}}})
            waiting = account_fleet.load_accounts()
            self.assertNotIn('codex-work', host_policy.load_policy()['known_accounts'])
            self.assertNotEqual(waiting['_discovery']['accounts']['codex-work']['status'], 'ready')
            self.native_codex('codex-work', 'account-B')
            ready = account_fleet.load_accounts()
            self.assertEqual(ready['_host_policy_sync']['added'], ['codex-work'])
            before = policy.read_bytes()
            self.assertEqual(account_fleet.load_accounts()['_host_policy_sync']['added'], [])
            self.assertEqual(before, policy.read_bytes())
            document = host_policy.load_policy()
            document['allowed_accounts'].remove('codex-work')
            policy.write_text(json.dumps(document))
            self.paseo({'codex': {'enabled': True}})
            account_fleet.load_accounts()
            self.paseo({'codex': {'enabled': True}, 'codex-work': {'extends': 'codex', 'enabled': True, 'env': {'CODEX_HOME': str(self.home / '.codex-work')}}})
            account_fleet.load_accounts()
            self.assertFalse(host_policy.account_allowed(host_policy.load_policy(), 'codex-work'))

    def test_grok_requires_live_client_evidence_and_honors_launcher_switch(self):
        parent = self.home
        for label, cached, native, enabled, executable, status in (
            ('cache only', True, False, None, False, 'missing_client'),
            ('native and cache', True, True, None, False, 'ready'),
            ('disabled launcher', True, True, False, False, 'disabled'),
            ('enabled launcher', True, True, True, False, 'ready'),
            ('native executable', False, False, None, True, 'ready'),
        ):
            with self.subTest(case=label):
                self.home = parent / label
                self.home.mkdir()
                if cached:
                    self.write('.agent-compass/usage-cache.json', {'providers': {'grok': {'plan_label': 'SuperGrok'}}})
                if native:
                    (self.home / '.grok').mkdir()
                if enabled is not None:
                    self.paseo({'grok': {'extends': 'acp', 'enabled': enabled}})
                with patch.object(inventory.shutil, 'which', side_effect=lambda name, **kwargs:
                                  'grok.exe' if executable and name == 'grok' else None):
                    snapshot = self.discover()
                row = snapshot['accounts']['grok']
                self.assertEqual(row['status'], status)
                if label == 'native and cache':
                    self.assertEqual((row['family'], row['billing'], row['plan']), ('grok', 'subscription', 'SuperGrok'))
                    self.assertIn('native', row['sources'])
                if enabled:
                    self.assertEqual(snapshot['launchers']['grok'], {'family': 'grok', 'status': 'ready'})

    def _grok_bot_discover(self):
        fake_cli = self.home / 'gbot.mjs'
        fake_cli.write_text('', encoding='utf-8')
        with patch.object(inventory, 'GROK_BOT_CLI', fake_cli), \
                patch.object(inventory.shutil, 'which', side_effect=lambda name, **kw: 'node' if name == 'node' else None):
            return self.discover()

    def test_grok_401_marks_auth_failed_and_excludes_grok_and_grok_bot_while_keeping_other_providers(self):
        import account_state
        (self.home / '.grok').mkdir(exist_ok=True)
        self.write('.agent-compass/grok-bot/config.json', {'bot_id': 'bot-1'})
        key_a = hashlib.sha256((str((self.home / '.grok').resolve()) + '\0token-A').encode()).hexdigest()
        self.write('.grok/auth.json', {'https://auth.x.ai::client': {'key': 'token-A', 'user_id': 'u1'}})
        codex_key = inventory.auth_facts('codex', self.codex_auth('account-A'), self.home / '.codex')['credential_key']
        self.write('.agent-compass/usage-cache.json', {'providers': {
            'codex': {
                'available': True, 'stale': False, 'fetched_at': self.now.isoformat(),
                'credential_key': codex_key,
                'windows': [{'id': 'usage', 'used_pct': 10.0, 'resets_at': '2026-10-30T00:00:00+00:00'}],
            },
            'grok': {
                'available': False, 'stale': True, 'error': '登录凭据需由 Grok 更新',
                'error_status': 401, 'error_at': self.now.isoformat(), 'credential_key': key_a, 'windows': [],
            },
        }})
        snap = self._grok_bot_discover()
        self.assertEqual(snap['accounts']['grok']['status'], 'auth_failed')
        self.assertEqual(snap['accounts']['grok-bot']['status'], 'auth_failed')
        self.assertTrue(any('grok: auth_failed' in w for w in account_state.warnings(snap)))
        applied = self.apply(snap)
        self.assertIn('credentials rejected', applied['accounts']['grok']['bundles']['grok47_high']['discovery_unavailable'])
        self.assertIn('credentials rejected', applied['accounts']['grok-bot']['bundles']['grok_bot']['discovery_unavailable'])
        self.assertEqual(snap['accounts']['codex']['status'], 'ready')

    def test_grok_402_marks_plan_inactive_and_excludes_grok_and_grok_bot(self):
        (self.home / '.grok').mkdir(exist_ok=True)
        self.write('.agent-compass/grok-bot/config.json', {'bot_id': 'bot-1'})
        key_a = hashlib.sha256((str((self.home / '.grok').resolve()) + '\0token-A').encode()).hexdigest()
        self.write('.grok/auth.json', {'https://auth.x.ai::client': {'key': 'token-A', 'user_id': 'u1'}})
        self.write('.agent-compass/usage-cache.json', {'providers': {'grok': {
            'available': False, 'stale': True, 'error': '更新失败',
            'error_status': 402, 'error_code': 'payment_required', 'error_at': self.now.isoformat(),
            'credential_key': key_a, 'windows': [],
        }}})
        snap = self._grok_bot_discover()
        self.assertEqual(snap['accounts']['grok']['status'], 'plan_inactive')
        self.assertEqual(snap['accounts']['grok-bot']['status'], 'plan_inactive')
        applied = self.apply(snap)
        self.assertIn('plan inactive', applied['accounts']['grok']['bundles']['grok47_high']['discovery_unavailable'])
        self.assertIn('plan inactive', applied['accounts']['grok-bot']['bundles']['grok_bot']['discovery_unavailable'])

    def test_grok_500_or_offline_marks_usage_failing_warns_then_excludes_after_6h_and_recovers(self):
        import account_state
        (self.home / '.grok').mkdir(exist_ok=True)
        self.write('.agent-compass/grok-bot/config.json', {'bot_id': 'bot-1'})
        key_a = hashlib.sha256((str((self.home / '.grok').resolve()) + '\0token-A').encode()).hexdigest()
        self.write('.grok/auth.json', {'https://auth.x.ai::client': {'key': 'token-A', 'user_id': 'u1'}})
        # Without prior windows: offline / 500 still marks usage_failing immediately.
        self.write('.agent-compass/usage-cache.json', {'providers': {'grok': {
            'available': False, 'stale': True, 'error': '更新失败',
            'error_status': None, 'error_at': self.now.isoformat(), 'credential_key': key_a, 'windows': [],
        }}})
        snap_no_win = self._grok_bot_discover()
        self.assertEqual(snap_no_win['accounts']['grok']['status'], 'usage_failing')
        self.assertFalse(snap_no_win['accounts']['grok']['usage_excluded'])

        # With prior windows < 6h old: warns, stays dispatchable.
        self.write('.agent-compass/usage-cache.json', {'providers': {'grok': {
            'available': False, 'stale': True, 'error': '更新失败',
            'error_status': 500, 'fetched_at': '2026-10-26T05:00:00+00:00', 'error_at': self.now.isoformat(),
            'credential_key': key_a,
            'windows': [{'id': 'credits', 'used_pct': 30.0, 'resets_at': '2026-10-30T00:00:00+00:00'}],
        }}})
        snap_grace = self._grok_bot_discover()
        self.assertEqual(snap_grace['accounts']['grok']['status'], 'usage_failing')
        self.assertFalse(snap_grace['accounts']['grok']['usage_excluded'])
        self.assertTrue(any('grok: usage_failing' in w and 'still dispatchable' in w for w in account_state.warnings(snap_grace)))
        applied_grace = self.apply(snap_grace)
        self.assertNotIn('discovery_unavailable', applied_grace['accounts']['grok']['bundles']['grok47_high'])
        self.assertTrue(applied_grace['accounts']['grok']['verified'])

        # Over 6h old: excluded from dispatch for both grok and grok-bot.
        self.write('.agent-compass/usage-cache.json', {'providers': {'grok': {
            'available': False, 'stale': True, 'error': '更新失败',
            'error_status': 500, 'fetched_at': '2026-10-25T23:00:00+00:00', 'error_at': self.now.isoformat(),
            'credential_key': key_a,
            'windows': [{'id': 'credits', 'used_pct': 30.0, 'resets_at': '2026-10-30T00:00:00+00:00'}],
        }}})
        snap_excluded = self._grok_bot_discover()
        self.assertEqual(snap_excluded['accounts']['grok']['status'], 'usage_failing')
        self.assertTrue(snap_excluded['accounts']['grok']['usage_excluded'])
        self.assertEqual(snap_excluded['accounts']['grok-bot']['status'], 'usage_failing')
        self.assertTrue(snap_excluded['accounts']['grok-bot']['usage_excluded'])
        applied_excluded = self.apply(snap_excluded)
        self.assertIn('usage reading failing', applied_excluded['accounts']['grok']['bundles']['grok47_high']['discovery_unavailable'])
        self.assertIn('usage reading failing', applied_excluded['accounts']['grok-bot']['bundles']['grok_bot']['discovery_unavailable'])

        # Recovery returns both to ready.
        self.write('.agent-compass/usage-cache.json', {'providers': {'grok': {
            'available': True, 'stale': False, 'error': None,
            'fetched_at': self.now.isoformat(), 'credential_key': key_a,
            'windows': [{'id': 'credits', 'used_pct': 20.0, 'resets_at': '2026-10-30T00:00:00+00:00'}],
        }}})
        snap_recovered = self._grok_bot_discover()
        self.assertEqual(snap_recovered['accounts']['grok']['status'], 'ready')
        self.assertEqual(snap_recovered['accounts']['grok-bot']['status'], 'ready')
        self.assertTrue(self.apply(snap_recovered)['accounts']['grok']['verified'])

    def test_grok_credential_rotation_and_missing_credentials_do_not_inherit_stale_failure(self):
        (self.home / '.grok').mkdir(exist_ok=True)
        self.write('.agent-compass/grok-bot/config.json', {'bot_id': 'bot-1'})
        key_a = hashlib.sha256((str((self.home / '.grok').resolve()) + '\0token-A').encode()).hexdigest()
        self.write('.grok/auth.json', {'https://auth.x.ai::client': {'key': 'token-B', 'user_id': 'u1'}})
        codex_key = inventory.auth_facts('codex', self.codex_auth('account-A'), self.home / '.codex')['credential_key']
        self.write('.agent-compass/usage-cache.json', {'providers': {
            'codex': {
                'available': True, 'stale': False, 'fetched_at': self.now.isoformat(),
                'credential_key': codex_key,
                'windows': [{'id': 'usage', 'used_pct': 10.0, 'resets_at': '2026-10-30T00:00:00+00:00'}],
            },
            'grok': {
                'available': False, 'stale': True, 'error': '登录凭据需由 Grok 更新',
                'error_status': 401, 'error_at': self.now.isoformat(), 'credential_key': key_a, 'windows': [],
            },
        }})
        snap_rotated = self._grok_bot_discover()
        self.assertEqual(snap_rotated['accounts']['grok']['usage_binding'], 'changed')
        self.assertEqual(snap_rotated['accounts']['grok']['status'], 'ready')
        self.assertEqual(snap_rotated['accounts']['grok-bot']['status'], 'ready')

        # When ~/.grok has no login and no GROK_API_KEY, even a legacy un-keyed failure row in cache must not make it usage_failing.
        self.write('.grok/auth.json', {})
        self.write('.agent-compass/usage-cache.json', {'providers': {'grok': {
            'available': False, 'stale': True, 'error': '更新失败',
            'error_at': self.now.isoformat(), 'credential_key': None, 'windows': [],
        }}})
        snap_no_creds = self._grok_bot_discover()
        self.assertEqual(snap_no_creds['accounts']['grok']['status'], 'ready')
        self.assertFalse(inventory.sign_in_proven(snap_no_creds['accounts']['grok']))
        self.assertFalse(self.apply(snap_no_creds)['accounts']['grok']['verified'])

    def test_grok_auth_json_ignores_paseo_def_env_and_shell_env_keeping_windows_and_401_auth_failed(self):
        from datetime import timedelta
        import io
        import usage_core as usage
        (self.home / '.grok').mkdir(exist_ok=True)
        self.write('.agent-compass/grok-bot/config.json', {'bot_id': 'bot-1'})
        exp = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat().replace('+00:00', 'Z')
        self.write('.grok/auth.json', {'https://auth.x.ai::client': {'key': 'tok-A', 'user_id': 'u1', 'expires_at': exp}})
        cache_file = self.home / '.agent-compass/usage-cache.json'
        payload = {'config': {'currentPeriod': {'end': '2026-11-03T08:26:58.149+00:00'}, 'creditUsagePercent': 40.0}}

        class _Resp(io.BytesIO):
            status = 200
            headers = {}
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        # Collector runs without GROK_API_KEY in its environment and writes a fresh reading
        with patch.object(usage, 'GROK_HOME', self.home / '.grok'), \
                patch.object(usage, 'HOME', self.home), \
                patch.object(usage, 'CACHE_PATH', cache_file), \
                patch.dict(usage.os.environ, {'GROK_API_KEY': '', 'GROK_TOKEN': ''}, clear=False), \
                patch.object(usage.urllib.request, 'urlopen', return_value=_Resp(json.dumps(payload).encode())):
            usage.collect_local_usage(['grok'], force=True)

        # Case 1: Paseo grok definition has env GROK_API_KEY
        self.paseo({'codex': {'enabled': True}, 'grok': {'enabled': True, 'env': {'GROK_API_KEY': 'def-K'}}})
        snap_paseo_env = self._grok_bot_discover()
        self.assertEqual(snap_paseo_env['accounts']['grok'].get('usage_binding'), 'matches')
        self.assertEqual(snap_paseo_env['accounts']['grok'].get('usage_windows'), ['credits'])
        merged_paseo = inventory.merge_usage(
            {'accounts': {'grok': {'status': 'fresh', 'windows': [{'id': 'credits', 'used_pct': 40.0}]}}},
            {'_discovery': snap_paseo_env, 'accounts': {}},
            self.now,
        )
        self.assertEqual(len(merged_paseo['accounts']['grok']['windows']), 1)

        # Case 2: Caller shell has GROK_API_KEY while collector did not
        self.paseo({'codex': {'enabled': True}, 'grok': {'enabled': True}})
        self.env = {'PATH': '', 'GROK_API_KEY': 'shell-K'}
        snap_shell_env = self._grok_bot_discover()
        self.assertEqual(snap_shell_env['accounts']['grok'].get('usage_binding'), 'matches')
        self.assertEqual(snap_shell_env['accounts']['grok'].get('usage_windows'), ['credits'])
        merged_shell = inventory.merge_usage(
            {'accounts': {'grok': {'status': 'fresh', 'windows': [{'id': 'credits', 'used_pct': 40.0}]}}},
            {'_discovery': snap_shell_env, 'accounts': {}},
            self.now,
        )
        self.assertEqual(len(merged_shell['accounts']['grok']['windows']), 1)

        # Collector gets 401: both Paseo definition env and caller shell env still settle to auth_failed (not ready/changed)
        http_401 = usage.urllib.error.HTTPError(usage.GROK_CLI_BILLING_URL, 401, 'Unauthorized', {}, io.BytesIO(b'{}'))
        with patch.object(usage, 'GROK_HOME', self.home / '.grok'), \
                patch.object(usage, 'HOME', self.home), \
                patch.object(usage, 'CACHE_PATH', cache_file), \
                patch.dict(usage.os.environ, {'GROK_API_KEY': '', 'GROK_TOKEN': ''}, clear=False), \
                patch.object(usage.urllib.request, 'urlopen', side_effect=[http_401, http_401]):
            usage.collect_local_usage(['grok'], force=True)

        self.paseo({'codex': {'enabled': True}, 'grok': {'enabled': True, 'env': {'GROK_API_KEY': 'def-K'}}})
        self.env = {'PATH': ''}
        snap_401_paseo = self._grok_bot_discover()
        self.assertEqual(snap_401_paseo['accounts']['grok'].get('usage_binding'), 'matches')
        self.assertEqual(snap_401_paseo['accounts']['grok']['status'], 'auth_failed')
        self.assertEqual(snap_401_paseo['accounts']['grok-bot']['status'], 'auth_failed')

        self.paseo({'codex': {'enabled': True}, 'grok': {'enabled': True}})
        self.env = {'PATH': '', 'GROK_API_KEY': 'shell-K'}
        snap_401_shell = self._grok_bot_discover()
        self.assertEqual(snap_401_shell['accounts']['grok'].get('usage_binding'), 'matches')
        self.assertEqual(snap_401_shell['accounts']['grok']['status'], 'auth_failed')
        self.assertEqual(snap_401_shell['accounts']['grok-bot']['status'], 'auth_failed')

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

    def test_antigravity_tier_read_from_google_sets_the_plan_and_proves_sign_in(self):
        snapshot = self.discover()
        self.assertFalse(inventory.sign_in_proven(snapshot['accounts']['antigravity']))
        read_at = self.now.isoformat()
        for tier_id, paid_name, expected_plan in (
                ('g1-pro-tier', 'Google AI Pro', 'pro'),
                ('g1-ultra-lite-tier', 'Google AI Ultra', 'ultra5'),
                ('g1-ultra-tier', 'Google AI Ultra', 'ultra20')):
            with self.subTest(tier_id=tier_id):
                self.write('.agent-compass/antigravity-plan.json',
                           {'plan': paid_name, 'tier_id': tier_id, 'read_at': read_at})
                row = self.discover()['accounts']['antigravity']
                self.assertEqual(row['plan'], expected_plan)
                self.assertIn('account-tier', row['sources'])
                self.assertTrue(inventory.sign_in_proven(row))
                applied = self.apply(self.discover())
                self.assertEqual(overlaid_capacity(applied, 'antigravity')['gemini-weekly'],
                                 community_capacity('antigravity', expected_plan, 'gemini-weekly'))
        # Ambiguous display label without tier_id ("Google AI Ultra" covers both 5x and 20x) and unrecognized
        # tier IDs enter Mode B with a stated reason instead of assuming pro or ultra20.
        for bad_doc, expected_substr in (
                ({'plan': 'Google AI Ultra', 'read_at': read_at}, 'Google AI Ultra'),
                ({'plan': 'Gemini Code Assist Standard', 'tier_id': 'standard-tier', 'read_at': read_at}, 'standard-tier')):
            with self.subTest(bad_doc=bad_doc):
                self.write('.agent-compass/antigravity-plan.json', bad_doc)
                applied = self.apply(self.discover())
                agy = applied['accounts']['antigravity']
                self.assertEqual(agy['plan'], 'unknown')
                self.assertNotIn('plan_priced_as', agy)
                self.assertIn(expected_substr, agy['plan_unknown'])
                over = axis_scores.five_axis_overlay(applied, axis_scores.load_five_axis())['accounts']['antigravity']
                self.assertTrue(over['capacity_unknown'])
                self.assertEqual(over['capacity_usd'], {})
                self.assertIn(expected_substr, over['plan_unknown_reason'])

    def test_grok_no_usage_is_eligible_without_unlimited_bonus_and_launchable(self):
        self.paseo({'grok': {'extends': 'acp', 'enabled': True}})
        snapshot = self.discover()
        accounts = account_fleet.load_accounts(account_fleet.reference_fleet_path())
        accounts['accounts'].pop('grok')  # custom catalog without a Grok template
        inventory.apply(accounts, snapshot)
        spec = accounts['accounts']['grok']
        self.assertFalse(spec['verified'])  # installed, with no usage-cache, is not a confident pick
        spec['verified'] = True
        self.assertTrue(spec['unmetered'])
        self.assertFalse(spec.get('unlimited', False))
        self.assertEqual(spec['windows'], {})
        with patch.object(host_policy, 'load_policy', return_value=None):
            result = allocator.plan({'work_kind': 'web_research', 'difficulty': 'routine', 'expected_hours': .1},
                                    {'accounts': {}}, self.now, accounts=accounts, overrides=[], records=[])
        candidate = next(c for c in result['ranking'] if c['id'] == 'grok47_high')
        self.assertEqual(candidate['excluded'], [])
        self.assertNotIn('free_usage', candidate['components'])
        with patch.object(inventory, 'current', return_value=snapshot):
            bundle = account_fleet.bundles(accounts)['grok47_high']
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
        self.write('.agent-compass/usage-cache.json', {'providers': {'grok': {
            'windows': [{'id': 'credits', 'used_pct': 99}], 'available': True}}})
        accounts = self.apply(self.discover())
        spec = accounts['accounts']['grok']
        self.assertFalse(spec['unmetered'])
        self.assertIn('credits', spec['windows'])
        with patch.object(host_policy, 'load_policy', return_value=None):
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
        # A model with no public data has no row and no score; it stays out of automatic routing until the data has it.
        self.assertEqual(account_fleet.model_spec(accounts, 'some-new-model'), {})

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

    def test_AgentCompass_plugin_switch_pauses_dot_and_chatgpt_web_until_reenabled(self):
        self.write('.dot2api/client.json', {'base_url': 'http://127.0.0.1:8788', 'api_key': 'SECRET'})
        self.write('.codex-chatgpt-web-consult/config.json', {'mode': 'browser-only', 'browserInteractionMode': 'automatic',
                                                             'host': '127.0.0.1', 'port': 8790, 'proAvailable': True})
        accounts = self.discover()['accounts']
        self.assertEqual((accounts['dot']['status'], accounts['chatgpt-web']['status']), ('ready', 'ready'))
        for plugin_id, account in (('dot2api', 'dot'), ('chatgpt-web', 'chatgpt-web')):
            with self.subTest(plugin_id):
                self.write('.agent-compass/plugins.json', {'v': 1, 'plugins': {plugin_id: {'enabled': False, 'autostart': True}}})
                accounts = self.discover()['accounts']
                self.assertEqual(accounts[account]['status'], 'paused')
                self.assertEqual(accounts[account]['reason'], 'plugin_disabled_in_agentcompass')
                other = 'chatgpt-web' if account == 'dot' else 'dot'
                self.assertEqual(accounts[other]['status'], 'ready')
                bundles = self.apply(self.discover())['accounts'][account]['bundles'].values()
                self.assertTrue(all('plugin_disabled_in_agentcompass' in b.get('discovery_unavailable', '') for b in bundles))
        self.write('.agent-compass/plugins.json', {'v': 1, 'plugins': {'dot2api': {'enabled': True}}})
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
        self.live_plan(**{'claude-work': 'pro'})
        snapshot = self.discover()
        accounts = self.apply(snapshot)
        spec = accounts['accounts']['claude-work']
        self.assertTrue(spec['verified'])
        self.assertEqual(overlaid_capacity(accounts, 'claude-work')['seven_day'], community_capacity('claude', 'pro', 'seven_day'))
        bundle = next(b for b in account_fleet.bundles(accounts).values() if b['account'] == 'claude-work' and not b.get('disabled'))
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
        self.native_codex('codex-work', 'account-A')
        self.paseo({'codex-work': {'extends': 'codex', 'enabled': True,
                    'env': {'CODEX_HOME': str(self.home / '.codex-work')}}})
        snapshot = self.discover()
        self.assertEqual(snapshot['accounts']['codex-work']['quota_account'], 'codex')
        accounts = self.apply(snapshot)
        second = account_fleet.bundles(accounts)['work_sol_xhigh']
        self.assertEqual((second['account'], second['provider']), ('codex', 'codex-work'))
        self.assertFalse(accounts['accounts']['codex-work']['bundles'])
        self.write('.agent-compass/usage-cache.json', {'providers': {'codex': {
            'credential_key': snapshot['accounts']['codex']['credential_key']}}})
        self.paseo({'codex': {'enabled': False}, 'codex-work': {'extends': 'codex', 'enabled': True,
                    'env': {'CODEX_HOME': str(self.home / '.codex-work')}}})
        accounts = self.apply(self.discover())
        usage = inventory.merge_usage({'accounts': {'codex': {'status': 'fresh', 'windows': [{'used_pct': 98}]}}}, accounts, self.now)
        self.assertEqual(usage['accounts']['codex-work']['windows'][0]['used_pct'], 98)

    def test_changed_credentials_clear_old_quota_and_unknown_plan_capacity(self):
        old = self.discover()['accounts']['codex']['credential_key']
        self.write('.agent-compass/usage-cache.json', {'providers': {'codex': {'credential_key': old}}})
        self.native_codex('codex', 'account-B', plan='unknown-plan')
        snapshot = self.discover()
        accounts = self.apply(snapshot)
        merged = inventory.merge_usage({'accounts': {'codex': {'status': 'fresh', 'windows': [1]}}}, accounts, self.now)
        self.assertEqual(merged['accounts']['codex']['windows'], [])
        # An unknown plan has no cited capacity: unknown, not what the old credential's plan allowed. The new
        # login's id_token claim is not a plan, so with no reading of its own the plan is not reported.
        self.assertEqual(overlaid_capacity(accounts, 'codex'), {})
        self.assertEqual(accounts['accounts']['codex']['plan_unknown'], 'not reported')
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
        self.native_claude('claude-lab')
        self.paseo({'claude-lab': {'extends': 'claude', 'enabled': False,
                    'env': {'CLAUDE_CONFIG_DIR': str(self.home / '.claude-lab')}}})
        accounts = self.apply(self.discover())
        bundles = account_fleet.bundles(accounts)
        self.assertIn('disabled', bundles['lab_sonnet55_high']['discovery_unavailable'])
        self.assertIn('disabled', bundles['lab_opus55_high'])
        task = {'objective': 'Fixture', 'expected_hours': 0.1, 'needs': {'local_only': True}, 'include_unverified': True}
        with patch.object(host_policy, 'load_policy', return_value=None):
            plan = allocator.plan(task, {'accounts': {}}, self.now, accounts=accounts, overrides=[])
        row = next(r for r in plan['ranking'] if r['id'] == 'lab_sonnet55_high')
        self.assertTrue(any('provider discovery: disabled' in reason for reason in row['excluded']))

    def test_launcher_uses_its_effective_credentials_not_its_display_identity(self):
        self.native_claude('claude', 'max')
        self.native_claude('claude-lab', 'pro')
        self.paseo({'claude': {'enabled': False}, 'claude-lab': {'extends': 'claude', 'enabled': True,
                    'env': {'CLAUDE_CONFIG_DIR': str(self.home / '.claude-lab'),
                            'CLAUDE_SECURESTORAGE_CONFIG_DIR': str(self.home / '.claude')}}})
        snapshot = self.discover()
        self.assertEqual(snapshot['accounts']['claude-lab']['status'], 'ready')
        self.assertIsNone(snapshot['accounts']['claude-lab']['plan'])       # the login file names a plan; that is no source
        self.live_plan(**{'claude-lab': 'max20'})      # bound to the effective (.claude) credential
        snapshot = self.discover()
        self.assertEqual(snapshot['accounts']['claude-lab']['plan'], 'max20')
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
        self.write('.agent-compass/usage-cache.json', {'providers': {'claude': {
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
        text = (self.home / '.agent-compass/provider-inventory.json').read_text()
        self.assertNotIn('SECRET-', text)
        self.assertNotIn('private@example.test', text)
        self.assertNotIn('id_token', text)

    def test_magpie_backup_account_off_is_distinct_from_current_account(self):
        self.native_codex('codex-work', 'account-B')
        self.paseo({'codex-work': {'extends': 'codex', 'enabled': True,
                    'env': {'CODEX_HOME': str(self.home / '.codex-work')}}})
        login = {'id': 'saved-B', 'agent': 'codex', 'auth': self.codex_auth('account-B'), 'on': False}
        self.write('.config/magpie/logins.json', [login])
        first = self.discover(publish=True)
        self.assertEqual(first['accounts']['codex-work']['status'], 'paused')
        self.assertEqual(first['accounts']['codex']['status'], 'ready')
        self.write('.config/magpie/logins.json', [{**login, 'on': True}])
        self.assertEqual(self.discover()['accounts']['codex-work']['status'], 'ready')

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
        self.write('.agent-compass/vps-magpie/connection.json', {'base_url': 'https://never-contact.test', 'api_key': 'SECRET-unused'})
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

    def test_collector_auth_refusal_excludes_only_matching_credential_and_recovers(self):
        bound = self.discover()['accounts']['codex']['credential_key']
        reading = {'fetched_at': self.now.isoformat(), 'windows': [{'id': 'usage', 'used_pct': 10}]}
        for error, status in (('登录凭据需由 Codex 更新', 'auth_failed'), ('更新失败', 'usage_failing'), (None, 'ready')):
            self.write('.agent-compass/usage-cache.json', {'providers': {'codex': {
                'credential_key': bound, 'error': error, 'stale': bool(error), **reading}}})
            self.assertEqual(self.discover(publish=True)['accounts']['codex']['status'], status)
        self.write('.agent-compass/usage-cache.json', {'providers': {'codex': {
            'credential_key': bound, 'error': '登录凭据需由 Codex 更新', 'stale': True}}})
        self.native_codex('codex', 'account-B')
        row = self.discover(publish=True)['accounts']['codex']
        self.assertEqual((row['status'], row['reason']), ('usage_failing', 'credential_changed'))  # not the old refusal

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
        self.write('.agent-compass/usage-cache.json', {'providers': {'claude-magpie': {
            'credential_key': fingerprint, 'plan_label': 'pro', 'backend_kind': 'gateway'}}})
        snapshot = self.discover()
        self.assertNotIn('claude-2', snapshot['accounts'])
        self.assertNotIn('codex-2', snapshot['accounts'])
        self.assertNotIn('claude-lab', snapshot['accounts'])
        self.assertNotIn('codex-work', snapshot['accounts'])
        self.assertEqual(snapshot['accounts']['claude-magpie']['usage_binding'], 'matches')
        self.assertEqual(snapshot['accounts']['claude-magpie']['billing'], 'subscription')
        self.assertEqual(snapshot['accounts']['claude-magpie']['plan'], 'pro')
        applied = self.apply(snapshot)
        accounts = applied['accounts']
        self.assertIn('codex-team', applied['dispatch_efforts'])
        self.assertNotIn('claude-2', accounts)
        self.assertNotIn('codex-2', accounts)
        self.assertNotIn('claude-lab', accounts)
        self.assertNotIn('codex-work', accounts)
        magpie_spec = accounts['claude-magpie']
        self.assertTrue(magpie_spec['verified'])
        self.assertEqual(overlaid_capacity(applied, 'claude-magpie')['seven_day'], community_capacity('claude', 'pro', 'seven_day'))
        self.assertEqual(magpie_spec['windows']['five_hour']['launch_min_remaining_pct'], 25)
        # No implicit model reservation between plans; bundle switches are the user's.
        self.assertNotIn('disabled', magpie_spec['bundles']['claude-magpie__opus55_high'])
        self.assertNotIn('disabled', magpie_spec['bundles']['claude-magpie__sonnet55_high'])
        override_path = self.write('.agent-compass/accounts-override.json', {
            'schema_version': 2,
            'accounts': {
                'claude-magpie': {
                    'bundles': {
                        'claude-magpie__sonnet55_high': {'disabled': True}
                    }
                }
            }
        })
        with patch.object(account_fleet, 'accounts_override_path', return_value=override_path), \
                patch.object(inventory, 'current', return_value=snapshot):
            merged = account_fleet.load_accounts()
            self.assertEqual(
                merged['accounts']['claude-magpie']['bundles']['claude-magpie__sonnet55_high'].get('disabled'),
                True,
            )

    # --- Oh My Pi OAuth subscription logins (agent.db) ---------------------------------------------------------------
    def omp_installed(self):
        target = self.home / '.bun' / 'bin' / ('omp.exe' if inventory.os.name == 'nt' else 'omp')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'')
        self.paseo({'codex': {'enabled': True}, 'omp': {'enabled': True}})

    def omp_db(self, rows, models=None):
        """A synthetic ~/.omp/agent/agent.db (and models.db) with omp's table shapes; no real credential."""
        folder = self.home / '.omp' / 'agent'
        folder.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(folder / 'agent.db')) as db:
            db.execute('CREATE TABLE auth_credentials (id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, '
                       'credential_type TEXT NOT NULL, data TEXT NOT NULL, disabled_cause TEXT DEFAULT NULL, '
                       'identity_key TEXT DEFAULT NULL, created_at INTEGER, updated_at INTEGER)')
            db.executemany('INSERT INTO auth_credentials (provider, credential_type, data, disabled_cause, identity_key) '
                           'VALUES (?,?,?,?,?)', [(p, t, json.dumps(d), cause, 'email:user@example.test')
                                                   for p, t, d, cause in rows])
            db.commit()
        if models is not None:
            with closing(sqlite3.connect(folder / 'models.db')) as db:
                db.execute('CREATE TABLE model_cache (provider_id TEXT PRIMARY KEY, version INTEGER, models TEXT)')
                db.executemany('INSERT INTO model_cache VALUES (?,?,?)',
                               [(pid, 1, json.dumps([{'id': m} for m in ids])) for pid, ids in models.items()])
                db.commit()
        return folder / 'agent.db'

    @staticmethod
    def omp_codex_login(account):
        return {'access': 'SECRET-omp-' + account, 'refresh': 'r', 'expires': 0, 'accountId': account,
                'email': 'user@example.test'}

    def test_omp_oauth_login_on_a_native_codex_account_shares_its_quota_as_a_subscription(self):
        self.native_codex('codex', 'shared-acct')
        self.omp_installed()
        self.omp_db([('openai-codex', 'oauth', self.omp_codex_login('shared-acct'), None)],
                    models={'openai-codex:0.159.0': ['gpt-6.1-sol', 'gpt-6-astra']})
        snapshot = self.discover()
        row = snapshot['accounts']['omp-openai-codex']
        self.assertEqual((row['family'], row['quota_family'], row['billing'], row['quota_account']),
                         ('omp', 'codex', 'subscription', 'codex'))
        self.assertEqual(row['identity'], snapshot['accounts']['codex']['identity'])
        # Only subscription logins: no API-key side is reported as a metered `omp` account.
        self.assertNotIn('omp', snapshot['accounts'])
        self.assertNotIn('SECRET-omp', json.dumps(snapshot))
        self.assertNotIn('user@example.test', json.dumps(snapshot))
        accounts = self.apply(snapshot)
        self.assertNotIn('omp', accounts['accounts'])
        self.assertEqual(accounts['accounts']['omp-openai-codex']['bundles'], {})
        omp = {bid: b for bid, b in account_fleet.bundles(accounts).items() if b.get('harness') == 'omp'}
        self.assertTrue(omp)
        for bid, b in omp.items():
            # On the Codex account itself: its windows, its subscription billing, omp's `<provider>/<model>` id.
            self.assertEqual((b['account'], b['provider']), ('codex', 'omp'), bid)
            self.assertTrue(b['launch_model'].startswith('openai-codex/'), b)
            self.assertIsNone(accounts_override.manual_only_billing(b['account'], accounts))
        # No twin: each (harness, model, effort) once on the shared account.
        keys = [(account_fleet.bundle_harness(b, 'codex', bid), account_fleet.canonical(b['model'], accounts), b.get('effort'))
                for bid, b in account_fleet.bundles(accounts).items() if b['account'] == 'codex']
        self.assertEqual(len(keys), len(set(keys)))

    def test_omp_only_oauth_login_is_its_own_subscription_account_on_the_family_plan(self):
        self.omp_installed()
        self.omp_db([('openai-codex', 'oauth', self.omp_codex_login('omp-only-acct'), None)])
        snapshot = self.discover()
        row = snapshot['accounts']['omp-openai-codex']
        self.assertIn(row.get('quota_account'), (None, 'omp-openai-codex'))   # owns its quota
        accounts = self.apply(snapshot)
        spec = accounts['accounts']['omp-openai-codex']
        self.assertEqual(spec['billing'], 'subscription')
        self.assertTrue(spec['verified'])
        self.assertTrue(spec['windows'])   # the Codex plan's windows
        self.assertTrue(spec['bundles'])
        self.assertTrue(all(b['provider'] == 'omp' and b['harness'] == 'omp' for b in spec['bundles'].values()))

    def test_omp_with_only_api_keys_stays_metered(self):
        self.omp_installed()
        self.omp_db([('openai', 'api_key', {'key': 'SECRET-sk'}, None)])
        snapshot = self.discover()
        self.assertEqual((snapshot['accounts']['omp']['billing'], snapshot['accounts']['omp']['status']), ('metered', 'ready'))
        self.assertFalse([n for n in snapshot['accounts'] if n.startswith('omp-')])

    def test_omp_disabled_oauth_credential_is_ignored(self):
        self.native_codex('codex', 'shared-acct')
        self.omp_installed()
        self.omp_db([('openai-codex', 'oauth', self.omp_codex_login('shared-acct'), 'invalid_grant')])
        snapshot = self.discover()
        self.assertFalse([n for n in snapshot['accounts'] if n.startswith('omp-')])

    def test_omp_several_oauth_logins_are_told_apart(self):
        self.native_codex('codex', 'shared-acct')
        self.omp_installed()
        self.omp_db([('openai-codex', 'oauth', self.omp_codex_login('shared-acct'), None),
                     ('openai-codex', 'oauth', self.omp_codex_login('second-acct'), None)])
        snapshot = self.discover()
        first, second = snapshot['accounts']['omp-openai-codex'], snapshot['accounts']['omp-openai-codex-2']
        self.assertEqual(first.get('quota_account'), 'codex')
        self.assertIn(second.get('quota_account'), (None, 'omp-openai-codex-2'))
        self.assertNotEqual(first['identity'], second['identity'])

    def test_omp_corrupt_or_locked_database_is_missing_credentials_not_a_crash(self):
        self.omp_installed()
        path = self.omp_db([('openai-codex', 'oauth', self.omp_codex_login('acct'), None)])
        with closing(sqlite3.connect(path)) as holder:
            holder.execute('BEGIN EXCLUSIVE')
            locked = self.discover()
            holder.rollback()
        path.write_bytes(b'not a sqlite database' * 64)
        corrupt = self.discover()
        for snapshot in (locked, corrupt):
            self.assertEqual(snapshot['accounts']['omp']['status'], 'missing_credentials')
            self.assertFalse([n for n in snapshot['accounts'] if n.startswith('omp-')])

    def test_omp_subscription_bundle_is_eligible_like_pi_and_draws_the_shared_quota(self):
        self.native_codex('codex', 'shared-acct')
        self.omp_installed()
        self.omp_db([('openai-codex', 'oauth', self.omp_codex_login('shared-acct'), None)])
        self.live_plan(codex='pro')
        accounts = self.apply(self.discover())
        accounts['accounts']['codex']['verified'] = True
        omp_id = next(bid for bid, b in account_fleet.bundles(accounts).items() if b.get('harness') == 'omp')
        task = {'objective': 'Fix a bug in parser.py', 'work_kind': 'general_implementation', 'expected_hours': 1,
                'constraints': {'model': account_fleet.bundles(accounts)[omp_id]['model']}}
        usage = usage_windows(self.now, {'codex': [('usage', 80, 100)]})
        plan = allocator.plan(task, usage, self.now, accounts=accounts, overrides=[], records=[], host_policy_config=None)
        row = next(c for c in plan['ranking'] if c['id'] == omp_id)
        self.assertEqual(row['excluded'], [], row)
        self.assertNotIn(candidate_eval.METERED_LAST_RESORT, row['excluded'])
        # It draws the Codex account's window, as Pi's rows do.
        self.assertEqual(row['bundle']['account'], 'codex')
        self.assertEqual(row['capacity']['window'], 'usage')
        scarce = allocator.plan(task, usage_windows(self.now, {'codex': [('usage', 0, 100)]}), self.now,
                                accounts=accounts, overrides=[], records=[], host_policy_config=None)
        self.assertTrue(next(c for c in scarce['ranking'] if c['id'] == omp_id)['excluded'])

    def test_pi_on_the_codex_subscription_adds_no_twin_of_a_codex_pi_bundle(self):
        # LITTLESPRING 2026-11-03: Pi signed in as the Codex account re-derived every pi-harness model under `pi`,
        # enabled, next to Codex's own (curated, partly disabled) rows; launch then hit the disabled twin.
        self.native_codex('codex', 'shared-acct')
        self.write('.pi/agent/auth.json', {'openai-codex': {
            'type': 'oauth', 'access': 'SECRET-shared-acct', 'refresh': 'r', 'accountId': 'shared-acct'}})
        snapshot = self.discover()
        self.assertEqual(snapshot['accounts']['pi'].get('quota_account'), 'codex')
        # Pi's catalog, as the tray's model-catalog.json lists it on that host.
        snapshot['accounts']['pi']['models'] = ['openai-codex/gpt-6.1-sol', 'openai-codex/gpt-6-luna']
        fleet = account_fleet.load_accounts(account_fleet.reference_fleet_path())
        # The shipped template's bundle-less `pi` account, which derives its bundles per model.
        fleet['accounts']['pi'] = {'plan': 'Pi', 'billing': 'subscription', 'verified': False,
                                   'windows': {'usage': {'hours': 168}}}
        accounts = inventory.apply(fleet, snapshot)
        self.assertNotIn('auto:pi:pi:gpt-6.1-sol@high', account_fleet.bundles(accounts))
        seen = {}
        for bid, b in account_fleet.bundles(accounts).items():
            if b['account'] == 'codex':
                key = (account_fleet.bundle_harness(b, 'codex', bid), account_fleet.canonical(b['model'], accounts), b.get('effort'))
                self.assertNotIn(key, seen, f'{bid} duplicates {seen.get(key)}')
                seen[key] = bid
        self.assertTrue(any(k[0] == 'pi' for k in seen))

    def test_pi_and_litellm_proxy_auto_enrolled_into_router(self):
        self.write('.pi/agent/auth.json', {
            'openai-codex': {
                'type': 'oauth',
                'access': 'pi-standalone-token',
                'refresh': 'pi-refresh',
                'accountId': 'standalone-pi-acct',
            }
        })
        self.write('.agent-compass/accounts-override.json', {
            'schema_version': 2,
            'accounts': {
                'team-proxy': {
                    'backend_kind': 'litellm',
                    'endpoint': 'https://proxy.example.test',
                    'api_key': 'sk-team-proxy',
                    'models': ['gpt-6.1-sol', 'claude-sonnet-5-5'],
                }
            }
        })
        pi_key = self.discover()['accounts']['pi']['credential_key']
        self.write('.agent-compass/usage-cache.json', {
            'providers': {
                'team-proxy': {
                    'plan_label': '$300',
                    'backend_kind': 'litellm',
                    'billing': 'metered',
                },
                'pi': {'credential_key': pi_key, 'fetched_at': self.now.isoformat(), 'windows': [{'id': 'usage', 'used_pct': 10}]},
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
        self.assertIn('auto:team-proxy:codex:gpt-6.1-sol@high', accounts['team-proxy']['bundles'])
        self.assertIn('auto:team-proxy:claude:claude-sonnet-5-5@high', accounts['team-proxy']['bundles'])
        self.assertEqual(accounts['team-proxy']['bundles']['auto:team-proxy:codex:gpt-6.1-sol@high']['harness'], 'codex')
        self.assertEqual(accounts['team-proxy']['bundles']['auto:team-proxy:codex:gpt-6.1-sol@high']['modeId'], 'full-access')
        self.assertEqual(accounts['team-proxy']['bundles']['auto:team-proxy:claude:claude-sonnet-5-5@high']['harness'], 'claude')
        self.assertEqual(accounts['team-proxy']['bundles']['auto:team-proxy:claude:claude-sonnet-5-5@high']['modeId'], 'bypassPermissions')
        override_file = self.home / 'custom-override.json'
        self.assertEqual(account_fleet.accounts_override_path({'AGENTCOMPASS_ACCOUNTS_OVERRIDE': str(override_file)}), override_file)
        self.assertNotEqual(account_fleet.accounts_override_path({'ROUTER_ACCOUNTS_OVERRIDE': str(override_file)}), override_file)


class FreshPathTests(unittest.TestCase):
    def test_a_cli_installed_after_the_process_started_is_found_on_the_fresh_path(self):
        # The tray keeps the PATH it started with; the installer added the CLI's folder to the user PATH since.
        root = temporary_root(self.addCleanup)
        (root / 'omp.exe').write_bytes(b'')
        (root / 'omp').write_bytes(b'')
        (root / 'omp').chmod(0o755)
        inventory._WHICH_CACHE.clear()
        self.addCleanup(inventory._WHICH_CACHE.clear)
        with patch.object(inventory, '_fresh_windows_path', return_value=str(root)), \
                patch.dict('os.environ', {'PATH': str(root.parent / 'stale-bin')}):
            import os
            self.assertTrue(inventory._which('omp', os.environ['PATH']))
            # A PATH the caller supplied (a test fleet, another profile) is taken as given.
            self.assertIsNone(inventory._which('omp', str(root.parent / 'other-bin')))

    @unittest.skipUnless(os.name == 'nt', 'the registry PATH exists on Windows only')
    def test_a_process_run_as_another_profile_does_not_take_the_signed_in_users_registry_path(self):
        # A sandbox or foreign-profile check points USERPROFILE at a throwaway home; the real user's PATH names CLIs
        # that profile does not have.
        owner = temporary_root(self.addCleanup)
        with patch.object(inventory, '_registry_profile', return_value=str(owner)):
            with patch.dict('os.environ', {'USERPROFILE': str(owner / 'sandbox-home')}):
                self.assertEqual(inventory._fresh_windows_path(), '')
            with patch.dict('os.environ', {'USERPROFILE': str(owner)}):
                self.assertTrue(inventory._fresh_windows_path())   # the machine PATH at least


class IsolationRecipeTests(unittest.TestCase):
    """Discovery under another home: which inherited environment variables still reach the real machine.

    Pointing HOME/USERPROFILE at an empty directory is not enough. `discover()` honours an explicit PASEO_HOME (the
    Paseo daemon sets it for every agent it starts). An isolated run must pass an explicit `environ` (or clear it) as
    `detection_environ` does. CLAUDE_CONFIG_DIR / CODEX_HOME never move a named account: they only describe the process.
    """

    def setUp(self):
        outer = temporary_root(self.addCleanup)
        self.real = outer / 'real-home'
        self.sandbox = outer / 'new-user'
        self.sandbox.mkdir(parents=True)
        (self.real / '.paseo').mkdir(parents=True)
        (self.real / '.paseo/config.json').write_text(json.dumps({'agents': {'providers': {
            'codex-work': {'extends': 'codex', 'enabled': True, 'env': {'CODEX_HOME': str(self.real / '.codex-work')}}}}}), encoding='utf-8')
        (self.real / '.codex-work').mkdir()
        (self.real / '.codex-work/auth.json').write_text(json.dumps({'tokens': {'account_id': 'X', 'access_token': 'SECRET'}}), encoding='utf-8')

    def test_discovery_uses_only_the_explicit_environment(self):
        for variables, expected_account in (
            ({'PASEO_HOME': str(self.real / '.paseo')}, 'codex-work'),
            ({'CODEX_HOME': str(self.real / '.codex-work')}, None),
            ({}, None),
        ):
            with self.subTest(variables=variables):
                snapshot = inventory.discover(self.sandbox, {'PATH': '', **variables})
                if expected_account:
                    self.assertEqual(snapshot['accounts'][expected_account]['status'], 'ready')
                    self.assertEqual(Path(snapshot['accounts'][expected_account]['home']), self.real / '.codex-work')
                else:
                    self.assertNotIn('codex-work', snapshot['accounts'])
                    self.assertFalse([n for n, row in snapshot['accounts'].items() if row['status'] == 'ready'])

    def test_shipped_fleet_gives_a_new_user_no_candidates(self):
        """The shipped accounts.json is an unverified template; discovery or a saved fleet makes an account routable."""
        env = {'ROUTER_PROVIDER_HOME': str(self.sandbox), 'ROUTER_STATE_DIR': str(self.sandbox / '.agent-compass'),
               'PASEO_HOME': str(self.sandbox / '.paseo'), 'USERPROFILE': str(self.sandbox), 'HOME': str(self.sandbox),
               'ROUTER_CAPACITY': str(self.sandbox / 'capacity.json'), 'PATH': ''}
        with patch.dict(inventory.os.environ, env, clear=True):
            accounts = account_fleet.load_accounts()
        self.assertTrue(accounts['accounts'])
        self.assertEqual([n for n, spec in accounts['accounts'].items() if spec.get('verified')], [])
        self.assertFalse([n for n in accounts['accounts'] if n in
                          ('claude-2', 'codex-2', 'claude-lab', 'codex-work')])


if __name__ == '__main__':
    unittest.main()


class DiscoverySnapshotTests(unittest.TestCase):
    def test_snapshot_scans_once_returns_independent_copies_and_ends_with_the_block(self):
        calls = []
        def scan(*args, **kwargs):
            calls.append(args)
            return {'accounts': {'a': 1}}
        with patch.object(inventory, '_discover', scan):
            with inventory.snapshot():
                first = inventory.discover()
                first['accounts']['a'] = 2
                with inventory.snapshot():
                    self.assertEqual(inventory.discover(), {'accounts': {'a': 1}})
                inventory.discover(home='/explicit')  # explicit arguments always rescan
            self.assertEqual(len(calls), 2)
            inventory.discover()
            self.assertEqual(len(calls), 3)


class PlanTemplateTests(unittest.TestCase):
    def apply(self, spec, family='antigravity', name='antigravity', plan=None, accounts_extra=None):
        accounts = {'accounts': {name: spec}, **(accounts_extra or {})}
        row = {'family': family, 'status': 'ready', 'billing': 'subscription'}
        if plan:
            row['plan'] = plan
        inv = {'accounts': {name: row}, 'launchers': {name: {'family': family, 'status': 'ready'}}}
        return inventory.apply(accounts, inv)['accounts'][name]

    def test_shipped_templates_are_plain_family_plan_dicts(self):
        shipped, _ = inventory.read_json(inventory.PLAN_TEMPLATES)
        self.assertEqual(set(shipped), {'claude', 'codex', 'antigravity', 'chatgpt-web', 'grok'})
        for family, plans in shipped.items():
            for plan, template in plans.items():
                self.assertIsInstance(template, dict, (family, plan))
                if 'alias' in template:
                    self.assertIn(template['alias'], plans)
                    continue
                self.assertIn('windows', template, (family, plan))
                self.assertNotIn('capacity_usd', template, (family, plan))      # capacity comes from community data by plan
                self.assertNotIn('burn_pct_per_worker_hour', template, (family, plan))

    def test_plan_labels_resolve_to_templates(self):
        for family, label, expected in (
                ('antigravity', 'g1-ultra-tier', 'ultra20'), ('antigravity', 'g1-ultra-lite-tier', 'ultra5'),
                ('antigravity', 'g1-pro-tier', 'pro'),
                ('antigravity', 'Google AI Ultra (20x)', 'ultra20'), ('antigravity', 'Google AI Ultra (5x)', 'ultra5'),
                ('antigravity', 'ultra5', 'ultra5'), ('antigravity', 'ultra20', 'ultra20'),
                ('antigravity', 'Google AI Pro', 'pro'), ('codex', 'ChatGPT Pro 100', 'pro100'),
                ('codex', 'pro100', 'pro100'), ('codex', 'Team', 'team'), ('claude', 'Claude Max 5x', 'max5')):
            with self.subTest(label=label):
                plan = inventory.configured_plan(family, label)
                self.assertEqual(plan, expected)
                self.assertTrue(inventory.plan_defaults({}, family, plan))
        self.assertEqual(set(inventory.plan_defaults({}, 'codex', 'team')['windows']), {'usage', 'five_hour'})
        self.assertEqual(inventory.plan_defaults({}, 'claude', 'max'), inventory.plan_defaults({}, 'claude', 'max5'))
        # Antigravity has no default plan and no unsplit 'ultra' -> 'ultra20' assumption.
        self.assertNotIn('default', inventory.plan_table({}, 'antigravity'))
        self.assertNotIn('ultra', inventory.plan_table({}, 'antigravity'))
        self.assertNotIn('antigravity', inventory.UNKNOWN_PLAN_PRICED_AS)
        self.assertEqual(inventory.configured_plan('antigravity', 'Google AI Ultra'), 'google ai ultra')
        self.assertIsNone(inventory.plan_defaults({}, 'antigravity', 'google ai ultra'))

    def test_claude_max_plans_ship_a_five_hour_window(self):
        for plan in ('max5', 'max20'):
            self.assertIn('five_hour', inventory.plan_defaults({}, 'claude', plan)['windows'])

    def test_configured_plan_seeds_windows_when_discovery_reports_none(self):
        spec = self.apply({'plan': 'Google AI Ultra (5x)', 'billing': 'subscription', 'windows': {}, 'bundles': {}})
        self.assertEqual(spec['plan'], 'Google AI Ultra (5x)')
        self.assertIn('gemini-5h', spec['windows'])
        self.assertNotIn('capacity_usd', spec)

    def test_same_plan_fills_gaps_but_keeps_account_values(self):
        spec = self.apply({'plan': 'pro', 'billing': 'subscription', 'windows': {'usage': {'hours': 168, 'launch_min_remaining_pct': 9}},
                           'capacity_usd': {'usage': 999}, 'bundles': {}}, family='codex', name='codex', plan='pro')
        self.assertEqual(spec['capacity_usd'], {'usage': 999})
        self.assertEqual(spec['windows']['usage']['launch_min_remaining_pct'], 9)

    def test_account_plan_templates_override_shipped(self):
        spec = self.apply({'plan': 'x', 'billing': 'subscription', 'windows': {}, 'bundles': {}}, family='codex', name='codex',
                          plan='plus', accounts_extra={'plan_templates': {'codex': {'plus': {
                              'windows': {'custom': {'hours': 24}}}}}})
        self.assertEqual(list(spec['windows']), ['custom'])
