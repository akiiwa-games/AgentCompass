"""Account states and plan sources: fake credentials only, a new home and state directory per test."""
import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import account_state
import allocator
import plan_state_residue
import provider_inventory as inventory
import route_judge
import usage_core

from router_test_support import isolated_router_state, temporary_root


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup, discovery=False, policy=False)


TASK = {'objective': 'Implement the parser module and tests', 'work_kind': 'self_contained_code', 'difficulty': 'routine',
        'expected_hours': 1.0, 'reasoning_depth': 2, 'code_complexity': 2}


def jwt(claims):
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')
    return f"{encode({'alg': 'none'})}.{encode(claims)}.sig"


class Machine(unittest.TestCase):
    def setUp(self):
        self.home = temporary_root(self.addCleanup)
        self.state = self.home / '.agent-compass'
        self.state.mkdir()
        self.now = datetime.now(timezone.utc)
        env = patch.dict(os.environ, {
            'ROUTER_PROVIDER_HOME': str(self.home), 'ROUTER_STATE_DIR': str(self.state),
            'PASEO_HOME': str(self.home / '.paseo'), 'ROUTER_USAGE_CACHE': str(self.state / 'usage-cache.json')})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('ROUTER_ACCOUNTS_FILE', None)  # the shipped templates, not the fixture's own capacities
        inventory._LAST_PUBLISHED_INVENTORY.clear()

    def write(self, path, document):
        target = self.home / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(document), encoding='utf-8')

    def codex_login(self, claim='plus', slot='codex', account='fake-account'):
        claims = {'https://api.openai.com/auth': {'chatgpt_plan_type': claim}} if claim else {}
        self.write(f'.{slot}/auth.json', {'tokens': {'access_token': 'fake-access', 'account_id': account, 'id_token': jwt(claims)}})

    def claude_login(self, subscription='max', tier='default_claude_max_20x', slot='claude'):
        oauth = {'accessToken': f'fake-{slot}', 'refreshToken': 'fake-refresh', 'subscriptionType': subscription}
        if tier:
            oauth['rateLimitTier'] = tier
        self.write(f'.{slot}/.credentials.json', {'claudeAiOauth': oauth})

    def windows(self, used=40.0, window='usage'):
        return [{'id': window, 'label': window, 'used_pct': used, 'resets_at': (self.now + timedelta(hours=72)).isoformat()}]

    def cache(self, age_hours=0.0, **rows):
        """Rows bound to the current fake credential, the way the collector stores them."""
        bound = inventory.discover(now=self.now)['accounts']
        fetched = (self.now - timedelta(hours=age_hours)).isoformat()
        self.write('.agent-compass/usage-cache.json', {'providers': {name: {
            'provider_id': name, 'display_name': name, 'available': True, 'stale': bool(row.get('error')),
            'fetched_at': fetched, 'credential_key': (bound.get(name) or {}).get('credential_key'),
            **({'windows': self.windows()} if 'windows' not in row else {}), **row} for name, row in rows.items()}})

    def row(self, account='codex', now=None):
        return inventory.discover(now=now or self.now)['accounts'][account]

    def spec(self, account='codex'):
        return allocator.load_accounts()['accounts'][account]

    def route(self, account='codex'):
        accounts = allocator.load_accounts()
        usage = route_judge.usage_snapshot(accounts=list(accounts['accounts']), inventory=accounts.get('_discovery'))
        result = allocator.plan(TASK, usage, accounts=accounts, overrides=[], records=[])
        rows = [c for c in result['ranking'] if (c.get('bundle') or {}).get('account') == account]
        return rows, result['decision'].get('warnings') or []

    def assertExcluded(self, account, words):
        rows, warnings = self.route(account)
        self.assertTrue(rows)
        for candidate in rows:
            self.assertTrue(any(words in text for text in candidate['excluded']), candidate['excluded'])
        return warnings


class AccountIdentityTests(Machine):
    """Q1: a named account's directory comes from its configuration, not the caller's environment."""

    def two_claude_homes(self):
        self.claude_login()
        self.claude_login('pro', None, 'claude-2')

    def test_caller_claude_config_dir_does_not_move_the_claude_account(self):
        self.two_claude_homes()
        row = inventory.discover(self.home, {'PATH': '', 'CLAUDE_CONFIG_DIR': str(self.home / '.claude-2')})['accounts']['claude']
        self.assertEqual(row['plan'], 'max20')
        self.assertEqual(Path(row['home']), self.home / '.claude')

    def test_caller_codex_home_does_not_move_the_codex_account(self):
        self.codex_login('plus')
        self.codex_login('pro', 'codex-2')
        row = inventory.discover(self.home, {'PATH': '', 'CODEX_HOME': str(self.home / '.codex-2')})['accounts']['codex']
        self.assertEqual(row['plan'], 'plus')
        self.assertEqual(Path(row['home']), self.home / '.codex')

    def test_a_paseo_entry_still_names_an_accounts_directory(self):
        self.claude_login('pro', None, 'elsewhere')
        self.write('.paseo/config.json', {'agents': {'providers': {'claude': {
            'extends': 'claude', 'enabled': True, 'env': {'CLAUDE_CONFIG_DIR': str(self.home / '.elsewhere')}}}}})
        self.assertEqual(Path(inventory.discover(self.home, {'PATH': ''})['accounts']['claude']['home']), self.home / '.elsewhere')

    def test_alternating_callers_leave_the_published_inventory_stable(self):
        """The 2026-11-07 flip: `claude` read max20, None, max20... as callers with and without the variable wrote it."""
        self.two_claude_homes()
        path = self.state / 'provider-inventory.json'
        seen = []
        for rewritten in (False, True, False, True, False, True):
            variables = {'CLAUDE_CONFIG_DIR': str(self.home / '.claude-2')} if rewritten else {}
            with patch.dict(os.environ, variables):
                if not rewritten:
                    os.environ.pop('CLAUDE_CONFIG_DIR', None)
                returned = inventory.current()['accounts']
            published = json.loads(path.read_text(encoding='utf-8'))['accounts']
            seen.append((returned['claude']['plan'], published['claude']['plan'], published['claude-2']['plan'],
                         Path(published['claude']['home']).name, returned['claude'].get('quota_account')))
        self.assertEqual(set(seen), {('max20', 'max20', 'pro', '.claude', 'claude')})

    def test_a_rewritten_process_does_not_publish_the_shared_inventory(self):
        self.two_claude_homes()
        with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.home / '.claude-2')}):
            inventory.current()
        self.assertFalse((self.state / 'provider-inventory.json').exists())
        self.assertFalse(inventory.environment_rewrites_accounts(self.home, {'CODEX_HOME': str(self.home / '.codex')}))
        self.assertTrue(inventory.environment_rewrites_accounts(self.home, {'CODEX_HOME': str(self.home / '.codex-2')}))

    def merged(self, identity, owner_binding='matches'):
        accounts = {'_discovery': {'accounts': {
            'claude': {'identity': 'own', 'usage_binding': owner_binding},
            'claude-2': {'identity': identity, 'quota_account': 'claude', 'usage_binding': 'matches'}}}, 'accounts': {}}
        usage = {'accounts': {'claude': {'status': 'stale', 'windows': ['own']},
                              'claude-2': {'status': 'fresh', 'windows': ['other']}}}
        return inventory.merge_usage(usage, accounts, self.now)['accounts']['claude']

    def test_a_reading_is_not_copied_across_identities(self):
        self.assertEqual(self.merged('another')['windows'], ['own'])
        self.assertEqual(self.merged('own', 'changed')['windows'], [])
        self.assertEqual(self.merged('own')['windows'], ['other'])  # one subscription, one reading


class PlanSourceTests(Machine):
    """Q2: the usage API's plan_type outranks the id_token claim."""

    def test_usage_api_plan_beats_the_claim_and_warns(self):
        self.codex_login('pro')
        self.cache(codex={'plan_label': 'plus'})
        row = self.row()
        self.assertEqual((row['plan'], row['plan_source']), ('plus', 'usage_api'))
        self.assertEqual(row['plan_conflict'], {'online': 'plus', 'credential': 'pro'})
        self.assertEqual(self.spec()['capacity_usd']['usage'], 67)
        _, warnings = self.route()
        self.assertTrue(any('usage API reports plan plus' in text for text in warnings), warnings)

    def test_the_claim_is_the_fallback_without_an_online_plan(self):
        self.codex_login('pro')
        self.cache(codex={})
        row = self.row()
        self.assertEqual((row['plan'], row['plan_source']), ('pro', 'credential'))
        self.assertNotIn('plan_conflict', row)

    def test_a_second_login_on_the_subscription_follows_the_online_plan(self):
        self.codex_login('plus')
        self.codex_login('pro', 'codex-2')
        self.cache(codex={'plan_label': 'plus'})
        rows = inventory.discover(now=self.now)['accounts']
        self.assertEqual(rows['codex-2']['quota_account'], 'codex')
        self.assertEqual(rows['codex-2']['plan'], 'plus')


class StateTests(Machine):
    """Q3: one state per account, read by dispatch and by every list."""

    def test_a_free_plan_is_excluded_at_once(self):
        self.codex_login('free')
        self.cache(codex={'plan_label': 'free'})
        self.assertEqual(self.row()['status'], account_state.PLAN_INACTIVE)
        warnings = self.assertExcluded('codex', 'plan inactive')
        self.assertTrue(any('codex: plan_inactive' in text for text in warnings), warnings)

    def test_a_lapse_the_token_has_not_seen_is_excluded(self):
        self.codex_login('pro')
        self.cache(codex={'plan_label': 'free'})
        self.assertEqual(self.row()['status'], account_state.PLAN_INACTIVE)

    def test_a_failed_reading_is_listed_at_once_and_warned_about_while_dispatchable(self):
        self.codex_login('pro')
        self.cache(age_hours=0.5, codex={'error': '更新失败'})
        row = self.row()
        self.assertEqual((row['status'], row['usage_excluded']), (account_state.USAGE_FAILING, False))
        self.assertIn('read_failed', row['reason'])
        self.assertEqual(row['last_success_at'], (self.now - timedelta(hours=0.5)).isoformat())
        rows, warnings = self.route()
        self.assertTrue(any(not c['excluded'] for c in rows))
        self.assertTrue(any('codex: usage_failing' in text and 'still dispatchable' in text for text in warnings), warnings)

    def test_a_failed_reading_is_excluded_after_six_hours(self):
        self.codex_login('pro')
        self.cache(age_hours=6.5, codex={'error': '更新失败'})
        self.assertTrue(self.row()['usage_excluded'])
        warnings = self.assertExcluded('codex', 'usage reading failing')
        self.assertTrue(any('excluded from dispatch' in text for text in warnings), warnings)

    def test_the_six_hours_can_be_configured(self):
        self.codex_login('pro')
        self.cache(age_hours=2.5, codex={'error': '更新失败'})
        self.assertFalse(self.row()['usage_excluded'])
        with patch.dict(os.environ, {account_state.EXCLUDE_AFTER_ENV: '2'}):
            self.assertTrue(self.row()['usage_excluded'])
        self.write('.agent-compass/accounts-override.json', {'scoring': {'usage_failing_exclude_hours': 2}})
        with patch.dict(os.environ, {'ROUTER_ACCOUNTS_OVERRIDE': str(self.state / 'accounts-override.json')}):
            self.assertTrue(self.row()['usage_excluded'])

    def test_two_hours_applies_only_when_no_error_came_back(self):
        self.codex_login('pro')
        self.cache(age_hours=1.5, codex={})
        self.assertEqual(self.row()['status'], account_state.READY)
        self.cache(age_hours=2.5, codex={})
        row = self.row()
        self.assertEqual((row['status'], row['reason'], row['usage_excluded']), (account_state.USAGE_FAILING, 'reading_too_old', False))
        self.cache(age_hours=10 / 60, codex={'error': '更新失败'})
        self.assertEqual(self.row()['status'], account_state.USAGE_FAILING)

    def test_ten_days_without_a_reading_is_excluded(self):
        self.codex_login('pro')
        self.cache(age_hours=240, codex={})
        self.assertTrue(self.row()['usage_excluded'])
        self.assertExcluded('codex', 'usage reading failing')

    def test_a_good_reading_returns_to_ready_without_anyone_clearing_it(self):
        self.codex_login('pro')
        self.cache(age_hours=7, codex={'error': '更新失败'})
        self.assertTrue(self.row()['usage_excluded'])
        inventory.write_json(self.state / 'provider-inventory.json', inventory.discover(now=self.now))
        self.cache(codex={})
        row = self.row()
        self.assertEqual(row['status'], account_state.READY)
        self.assertEqual([k for k in ('reason', 'failing_since', 'usage_excluded') if k in row], [])

    def test_no_good_reading_ever_starts_the_clock_at_the_first_failure(self):
        self.codex_login('pro')
        self.cache(claude={})  # a collector has run, but not for this account
        first = self.row()
        self.assertEqual((first['status'], first['reason'], first['usage_excluded']), (account_state.USAGE_FAILING, 'no_reading', False))
        self.assertIsNone(first['last_success_at'])
        inventory.write_json(self.state / 'provider-inventory.json', inventory.discover(now=self.now))
        self.assertFalse(self.row(now=self.now + timedelta(hours=5))['usage_excluded'])
        self.assertTrue(self.row(now=self.now + timedelta(hours=7))['usage_excluded'])

    def test_a_machine_with_no_collector_is_not_each_accounts_failure(self):
        self.codex_login('pro')
        self.assertEqual(self.row()['status'], account_state.READY)

    def test_null_claude_usage_is_not_ready(self):
        self.claude_login()
        self.cache(claude={'windows': [{'id': 'seven_day', 'label': '7d', 'used_pct': None}]})
        row = self.row('claude')
        self.assertEqual(row['status'], account_state.USAGE_FAILING)
        self.assertIn('no_reading', row['reason'])

    def test_structured_refusals_decide_and_the_collector_text_is_only_for_old_rows(self):
        self.codex_login('pro')
        for fields, status in (({'error': '更新失败', 'error_status': 402}, account_state.PLAN_INACTIVE),
                               ({'error': '更新失败', 'error_status': 403, 'error_code': 'subscription_expired'}, account_state.PLAN_INACTIVE),
                               ({'error': 'x', 'error_status': 403}, account_state.AUTH_FAILED),
                               ({'error': 'x', 'error_status': 401, 'error_code': 'invalid_token'}, account_state.AUTH_FAILED),
                               # A status outranks the words: a 500 is not a refusal whatever the row says.
                               ({'error': 'Login credentials must be updated by Codex', 'error_status': 500}, account_state.USAGE_FAILING),
                               ({'error': 'Login credentials must be updated by Codex'}, account_state.AUTH_FAILED),
                               ({'error': '登录凭据需由 Codex 更新'}, account_state.AUTH_FAILED)):
            with self.subTest(fields=fields):
                self.cache(codex=fields)
                self.assertEqual(self.row()['status'], status)

    def test_collector_records_what_the_provider_said_without_its_text(self):
        account = usage_core.CodexAccount('codex', 'codex', self.home / '.codex')
        path = self.state / 'collector-cache.json'
        good = usage_core.ProviderUsage(provider_id='codex', display_name='codex', fetched_at=self.now.isoformat(),
                                        windows=[usage_core.UsageWindow('usage', '7d', 25)])
        refusal = {'error': {'code': 'subscription_expired', 'message': 'private words from the provider'}}
        with patch.object(usage_core, 'CACHE_PATH', path), patch.object(usage_core, 'provider_credential_key', return_value='k'):
            usage_core.cache_put_provider(good)
            with patch.object(usage_core, 'codex_authorized_json', return_value=(403, refusal)):
                usage_core.fetch_codex_usage(account)
            row = usage_core.load_usage_cache()['providers']['codex']
            self.assertEqual((row['error_status'], row['error_code']), (403, 'subscription_expired'))
            self.assertNotIn('private words', path.read_text(encoding='utf-8'))
            with patch.object(usage_core, 'codex_authorized_json', return_value=(0, 'network down')):
                usage_core.fetch_codex_usage(account)
            row = usage_core.load_usage_cache()['providers']['codex']
            self.assertEqual((row['error_status'], row['error_code']), (None, None))
            good.fetched_at = (self.now + timedelta(minutes=5)).isoformat()
            usage_core.cache_put_provider(good)
            self.assertNotIn('error_status', usage_core.load_usage_cache()['providers']['codex'])

    def test_collect_local_usage_preserves_error_status_code_and_records_failures_without_prior_cache(self):
        self.codex_login('plus')
        self.cache(age_hours=0.2, codex={})
        path = self.state / 'usage-cache.json'
        cacc = usage_core.CodexAccount('codex', 'codex', self.home / '.codex')
        with (patch.object(usage_core, 'CACHE_PATH', path),
              patch.object(usage_core, 'STATE_DIR', self.state),
              patch.object(usage_core, 'HOME', self.home),
              patch.object(usage_core, 'CODEX_HOME', self.home / '.codex'),
              patch.dict(usage_core.CODEX_ACCOUNT_BY_ID, {'codex': cacc})):
            with patch.object(usage_core, 'codex_authorized_json',
                              return_value=(402, {'detail': {'code': 'deactivated_workspace'}})):
                usage_core.collect_local_usage(['codex'], force=True)
            row_402 = usage_core.load_usage_cache()['providers']['codex']
            self.assertEqual((row_402.get('error_status'), row_402.get('error_code')), (402, 'deactivated_workspace'))
            self.assertEqual(self.row('codex')['status'], account_state.PLAN_INACTIVE)

            # Credential rotation with no prior cache for the new credential: 401 -> auth_failed
            self.write('.codex/auth.json', {'tokens': {
                'access_token': 'fake-access-rotated', 'account_id': 'fake-account',
                'id_token': jwt({'https://api.openai.com/auth': {'chatgpt_plan_type': 'plus'}})}})
            with patch.object(usage_core, 'codex_authorized_json',
                              return_value=(401, {'error': {'code': 'invalid_token'}})):
                usage_core.collect_local_usage(['codex'], force=True)
            self.assertEqual(self.row('codex')['status'], account_state.AUTH_FAILED)

            # Credential with 200 plan_type='free' and empty windows -> plan_inactive
            with patch.object(usage_core, 'codex_authorized_json',
                              return_value=(200, {'plan_type': 'free', 'rate_limit': {}})):
                usage_core.collect_local_usage(['codex'], force=True)
            inv_free = self.row('codex')
            self.assertEqual((inv_free['status'], inv_free['plan']), (account_state.PLAN_INACTIVE, 'free'))

    def test_antigravity_and_pi_failed_polls_mark_stale_and_exclude_after_six_hours(self):
        self.write('.pi/agent/auth.json', {'openai-codex': {'type': 'oauth', 'access': 'tok-pi', 'accountId': 'acct-pi'}})
        path = self.state / 'usage-cache.json'
        self.write('.agent-compass/usage-cache.json', {
            'fetched_at': (self.now - timedelta(minutes=10)).isoformat(),
            'providers': {
                'antigravity': {
                    'provider_id': 'antigravity', 'display_name': 'agy', 'available': True, 'stale': False,
                    'fetched_at': (self.now - timedelta(minutes=10)).isoformat(),
                    'windows': [{'id': 'claude', 'label': 'Claude', 'used_pct': 20.0,
                                 'resets_at': (self.now + timedelta(hours=5)).isoformat()}],
                },
                'pi': {
                    'provider_id': 'pi', 'display_name': 'pi', 'available': True, 'stale': False,
                    'fetched_at': (self.now - timedelta(minutes=10)).isoformat(),
                    'windows': [{'id': 'five_hour', 'label': '5h', 'used_pct': 20.0,
                                 'resets_at': (self.now + timedelta(hours=5)).isoformat()}],
                },
            },
        })
        with (patch.object(usage_core, 'CACHE_PATH', path),
              patch.object(usage_core, 'STATE_DIR', self.state),
              patch.object(usage_core, 'HOME', self.home),
              patch.object(usage_core, 'find_antigravity_cli', return_value='agy'),
              patch.object(usage_core, '_run_antigravity_cli',
                           side_effect=['1.1.12', RuntimeError('agy timeout'), RuntimeError('agy timeout')]),
              patch.object(usage_core, 'ANTIGRAVITY_USAGE_RETRY_SECONDS', 0),
              patch.object(usage_core, 'http_json', return_value=(500, None)),
              patch.object(inventory, '_which', side_effect=lambda cmd, *_: 'agy' if cmd in ('agy', 'pi') else None)):
            usage_core.collect_local_usage(['antigravity', 'pi'], force=True)
            providers = usage_core.load_usage_cache()['providers']
            self.assertEqual((providers['antigravity'].get('error'), providers['antigravity'].get('stale')), ('更新失败', True))
            self.assertEqual((providers['pi'].get('error'), providers['pi'].get('stale')), ('更新失败', True))
            discovered = inventory.discover(now=self.now)['accounts']
            self.assertEqual(discovered['antigravity']['status'], account_state.USAGE_FAILING)
            self.assertEqual(discovered['pi']['status'], account_state.USAGE_FAILING)

            # 10-day-old Antigravity error with verified override must be excluded from dispatch
            self.write('.agent-compass/accounts-override.json', {
                'accounts': {'antigravity': {'enabled': True, 'verified': True, 'plan': 'pro'}}})
            providers['antigravity']['fetched_at'] = (self.now - timedelta(days=10)).isoformat()
            self.write('.agent-compass/usage-cache.json', {'providers': providers})
            agy_row = inventory.discover(now=self.now)['accounts']['antigravity']
            self.assertEqual((agy_row['status'], agy_row['usage_excluded']), (account_state.USAGE_FAILING, True))
            self.assertIn('usage-cache', agy_row['sources'])
            agy_spec = allocator.load_accounts()['accounts']['antigravity']
            self.assertFalse(agy_spec['verified'])
            self.assertTrue(all(b.get('discovery_unavailable') for b in agy_spec['bundles'].values()))

    def test_shared_subscription_propagates_402_and_same_credential_401_without_shifting_quota_account(self):
        self.codex_login('plus', 'codex', 'acct-shared')
        self.codex_login('plus', 'codex-2', 'acct-shared')
        self.write('.pi/agent/auth.json', {'openai-codex': {
            'type': 'oauth', 'access': 'fake-access', 'accountId': 'acct-shared'}})
        self.write('.paseo/config.json', {'agents': {'providers': {
            'codex': {'type': 'codex', 'enabled': True},
            'codex-2': {'type': 'codex', 'enabled': True, 'env': {'CODEX_HOME': str(self.home / '.codex-2')}},
            'pi': {'type': 'pi', 'enabled': True},
        }}})
        self.cache(codex={'error': '用量接口返回 HTTP 402', 'error_status': 402,
                          'error_code': 'deactivated_workspace', 'windows': []})
        rows = inventory.discover(now=self.now)['accounts']
        for slot in ('codex', 'codex-2', 'pi'):
            self.assertEqual(rows[slot]['quota_account'], 'codex')
            self.assertEqual(rows[slot]['status'], account_state.PLAN_INACTIVE)
        specs = allocator.load_accounts()['accounts']
        self.assertFalse(specs['codex']['verified'])
        self.assertFalse(specs['codex-2']['verified'])

        # Same credential 401/403 propagates across slots sharing that credential
        self.cache(codex={'error': '登录凭据需由 Codex 更新', 'error_status': 401,
                          'error_code': 'invalid_token', 'windows': []})
        rows_401 = inventory.discover(now=self.now)['accounts']
        for slot in ('codex', 'codex-2', 'pi'):
            self.assertEqual(rows_401[slot]['quota_account'], 'codex')
            self.assertEqual(rows_401[slot]['status'], account_state.AUTH_FAILED)

    def test_magpie_pooled_plan_labels_and_planless_failures_are_handled_as_subscriptions(self):
        self.assertEqual(inventory.configured_plan('claude', 'max 20x ×2'), 'max20')
        self.assertEqual(inventory.configured_plan('claude', 'max5'), 'max5')
        self.assertTrue(account_state.plan_is_inactive('claude', 'free ×2'))
        self.write('.claude/settings.json', {
            'env': {'ANTHROPIC_BASE_URL': 'https://magpie.example.com', 'ANTHROPIC_AUTH_TOKEN': 'sk-magpie-1'}})
        self.write('.claude/usage-float.json', {'source': 'magpie', 'provider': 'claude', 'account': ''})
        self.cache(claude={'plan_label': 'free ×2', 'backend_kind': 'gateway',
                           'windows': self.windows(window='seven_day')})
        row_free = self.row('claude')
        self.assertEqual((row_free['status'], row_free['plan']), (account_state.PLAN_INACTIVE, 'free'))
        self.assertFalse(self.spec('claude')['verified'])

        # Failing Magpie reading without plan_label (12h old) stays billing='subscription' and is excluded
        self.cache(age_hours=12, claude={'error': 'VPS 暂未取得有效用量', 'backend_kind': 'gateway', 'windows': []})
        row_fail = self.row('claude')
        self.assertEqual((row_fail['status'], row_fail['billing'], row_fail['usage_excluded']),
                         (account_state.USAGE_FAILING, 'subscription', True))
        spec_fail = self.spec('claude')
        self.assertEqual((spec_fail['billing'], spec_fail['verified']), ('subscription', False))


class LaunchLatchTests(Machine):
    def test_only_a_subscription_failure_latches_and_structured_fields_decide(self):
        for error, code, status, expected in (
                ('Your subscription has expired', None, None, True),
                ('payment required', None, None, True),
                ('订阅已过期', None, None, True),
                ('whatever the words', 'subscription_inactive', None, True),
                ('whatever the words', None, 402, True),
                ('Your subscription has expired', 'quota', None, False),
                ('Your plan does not include this model', None, None, False),
                ('The model is not available on your plan', None, None, False),
                ('rate limit exceeded', None, None, False),
                (None, None, None, False)):
            with self.subTest(error=error, code=code, status=status):
                self.assertEqual(account_state.error_means_inactive(error, code, status), expected)

    def test_a_launch_failure_excludes_until_a_newer_good_reading(self):
        self.codex_login('pro')
        self.cache(age_hours=1, codex={})
        self.assertTrue(account_state.record_launch_error('codex', 'Your subscription has expired',
                                                          at=self.now.isoformat()))
        self.assertEqual(self.row()['status'], account_state.PLAN_INACTIVE)
        self.assertExcluded('codex', 'plan inactive')
        self.assertFalse(account_state.record_launch_error('codex', 'Your plan does not include this model'))
        # A routine reading without an online plan or credential change must not unlock the latch.
        self.cache(age_hours=-0.1, codex={})
        self.assertEqual(self.row()['status'], account_state.PLAN_INACTIVE)
        self.assertIn('codex', account_state.latched())
        # Once the online usage API confirms a paid plan newer than the latch, the latch clears.
        self.cache(age_hours=-0.1, codex={'plan_label': 'pro'})
        self.assertEqual(self.row()['status'], account_state.READY)
        self.assertEqual(account_state.latched(), {})

    def test_launch_latch_unlocks_on_credential_rotation_or_worker_start(self):
        self.claude_login()
        self.cache(age_hours=0.1, claude={'windows': self.windows(window='seven_day')})
        self.assertTrue(account_state.record_launch_error('claude', 'Your subscription has expired',
                                                          at=self.now.isoformat()))
        self.assertEqual(self.row('claude')['status'], account_state.PLAN_INACTIVE)
        # Routine 5-minute poll on the same credential (Claude usage API has no plan_type) keeps the latch.
        self.cache(age_hours=-0.1, claude={'windows': self.windows(window='seven_day')})
        self.assertEqual(self.row('claude')['status'], account_state.PLAN_INACTIVE)
        # Rotating the credential and getting a new good reading unlocks the latch.
        self.write('.claude/.credentials.json', {'claudeAiOauth': {
            'accessToken': 'fake-claude-rotated', 'refreshToken': 'fake-refresh',
            'subscriptionType': 'max', 'rateLimitTier': 'default_claude_max_20x'}})
        self.cache(age_hours=-0.2, claude={'windows': self.windows(window='seven_day')})
        self.assertEqual(self.row('claude')['status'], account_state.READY)
        self.assertEqual(account_state.latched(), {})
        # Re-latch and verify that an observed started worker clears the latch.
        self.assertTrue(account_state.record_launch_error('claude', 'Your subscription has expired',
                                                          at=(self.now + timedelta(minutes=20)).isoformat()))
        self.assertIn('claude', account_state.latched())
        import dispatch_registry
        db = self.state / 'dispatch.sqlite3'
        launched_at = (self.now + timedelta(minutes=21)).isoformat()
        updated_at = (self.now + timedelta(minutes=22)).isoformat()
        labels = {k: f'v-{k}' for k in dispatch_registry.LABELS}
        with dispatch_registry.transaction(db) as conn:
            dispatch_registry.save(conn, {
                'dispatch_id': 'd-1', 'decision_id': 'dec-1', 'parent_id': 'p-1', 'task_id': 't-1',
                'task_hash': 'th', 'request_hash': 'rh', 'state': 'launching',
                'created_at': launched_at, 'launched_at': launched_at,
                'lease_until': (self.now + timedelta(hours=1)).isoformat(),
                'hard_until': (self.now + timedelta(hours=2)).isoformat(),
                'bundle': {'account': 'claude', 'bundle_id': 'claude__opus_high'},
                'request': {'provider': 'claude/claude-opus-5-5', 'settings': {'modeId': 'bypassPermissions'},
                            'labels': labels},
                'events': [],
            })
        dispatch_registry.observe('d-1', {
            'id': 'agent-1', 'provider': 'claude', 'model': 'claude-opus-5-5',
            'status': 'running', 'currentModeId': 'bypassPermissions',
            'updatedAt': updated_at, 'labels': labels,
        }, now=self.now + timedelta(minutes=22), path=db)
        self.assertEqual(account_state.latched(), {})

    def test_the_latch_reaches_the_other_login_on_the_subscription(self):
        self.codex_login('pro')
        self.codex_login('pro', 'codex-2')
        self.write('.paseo/config.json', {'agents': {'providers': {'codex': {'enabled': True}, 'codex-2': {'extends': 'codex', 'enabled': True}}}})
        self.cache(age_hours=1, codex={})
        account_state.record_launch_error('codex', code='subscription_inactive', at=self.now.isoformat())
        self.assertEqual(self.row('codex-2')['status'], account_state.PLAN_INACTIVE)


class TemplateTests(Machine):
    """Q4: exact names, conservative pricing for the rest."""

    def test_plan_names_map_exactly(self):
        for family, label, expected in (('codex', 'prolite', 'pro100'), ('codex', 'pro', 'pro'), ('codex', 'ChatGPT Plus', 'plus'),
                                        ('codex', 'business', 'business'), ('codex', 'self_serve_business_usage_based', 'self_serve_business_usage_based'),
                                        ('claude', 'promotional', 'promotional'), ('antigravity', 'Google AI Pro', 'pro'),
                                        ('antigravity', 'Gemini Code Assist Standard', 'gemini code assist standard')):
            with self.subTest(label=label):
                self.assertEqual(inventory.configured_plan(family, label), expected)

    def test_prolite_is_priced_as_the_100_dollar_tier(self):
        self.codex_login('prolite')
        self.cache(codex={'plan_label': 'prolite'})
        self.assertEqual(self.spec()['capacity_usd'], {'usage': 175})

    def test_an_unknown_plan_is_priced_conservatively_and_warned_about(self):
        self.codex_login('business')
        self.cache(codex={})
        spec = self.spec()
        self.assertEqual(spec['capacity_usd'], {'usage': 67, 'five_hour': 10})
        self.assertEqual((spec['plan_unknown'], spec['plan_priced_as']), ('business', 'plus'))
        _, warnings = self.route()
        self.assertTrue(any("plan 'business' is not a known plan" in text for text in warnings), warnings)

    def test_claude_with_no_plan_is_priced_conservatively(self):
        self.claude_login(None, None)
        self.cache(claude={'windows': self.windows(window='seven_day')})
        spec = self.spec('claude')
        self.assertEqual((spec['plan_unknown'], spec['capacity_usd']['seven_day']), ('not reported', 156))

    def test_antigravity_free_tiers_are_inactive_not_ultra(self):
        for label in ('Free', 'Antigravity Free Tier', 'Google AI Free'):
            with self.subTest(label=label):
                self.assertTrue(account_state.plan_is_inactive('antigravity', label))
        self.assertFalse(account_state.plan_is_inactive('opencode', 'free'))
        self.assertFalse(account_state.plan_is_inactive('codex', 'plus'))

    def test_an_unknown_antigravity_tier_is_not_ultra(self):
        accounts = allocator.load_accounts()
        snapshot = {'accounts': {'antigravity': {'family': 'antigravity', 'provider': 'antigravity-resilient', 'status': 'ready',
                                                 'plan': 'Gemini Code Assist Standard', 'billing': 'subscription',
                                                 'sources': ['native', 'usage-cache']}},
                    'launchers': {'antigravity-resilient': {'family': 'antigravity-resilient', 'status': 'ready'}}}
        spec = inventory.apply(accounts, snapshot)['accounts']['antigravity']
        self.assertEqual((spec['capacity_usd']['gemini-weekly'], spec['plan_priced_as']), (31, 'pro'))

    def test_the_plus_five_hour_window_is_priced(self):
        self.codex_login('plus')
        spec = self.spec()
        self.assertEqual(spec['capacity_usd'], {'usage': 67, 'five_hour': 10})
        self.assertEqual(spec['burn_pct_per_worker_hour']['five_hour'], 100.0)
        scores = {}
        for used in (0.0, 60.0):
            self.cache(codex={'plan_label': 'plus', 'windows': self.windows(40.0) + [
                {'id': 'five_hour', 'label': '5h', 'used_pct': used, 'resets_at': (self.now + timedelta(hours=4)).isoformat()}]})
            rows, _ = self.route()
            scores[used] = max(c['score'] for c in rows if not c['excluded'])
        self.assertNotEqual(scores[0.0], scores[60.0])


class ResidueTests(Machine):
    def test_dry_run_lists_residue_and_writes_nothing(self):
        (self.state / 'provider-inventory.json').write_text(json.dumps({'accounts': {
            'codex': {'family': 'codex', 'plan': 'plus', 'home': str(self.home / '.codex'), 'credential_key': 'DIGEST'},
            'codex-2': {'family': 'codex', 'plan': 'pro', 'quota_account': 'codex'},
            'pi': {'family': 'pi', 'quota_family': 'codex', 'plan': 'pro', 'quota_account': 'codex'},
            'chatgpt-web': {'family': 'chatgpt-web', 'plan': 'pro'},
            'claude': {'family': 'claude', 'plan': None, 'home': str(self.home / '.claude-2')}}}), encoding='utf-8')
        pooled = [{'id': 'seven_day', 'used_pct': 36, 'accounts': 2}]
        own = [{'id': 'seven_day', 'used_pct': 18}]
        self.write('.agent-compass/usage-cache.json', {'providers': {
            'codex': {'plan_label': 'plus', 'fetched_at': 'new', 'credential_key': 'DIGEST'}, 'claude': {'windows': own}}})
        self.write('.usage-float/usage-cache.json', {'providers': {'codex': {'plan_label': 'pro', 'fetched_at': 'old'}}})
        self.write('.agent-compass/capacity.json', {'observed': {'codex': {'usage': {'pct_per_hour_3h': 0, 'pct_per_hour_24h': 0.4583}}}})
        (self.state / 'router-ledger.jsonl').write_text('\n'.join(json.dumps(line) for line in (
            {'at': 't1', 'usage': {'claude': {'windows': own}}}, {'at': 't2', 'usage': {'claude': {'windows': pooled}}},
            {'at': 't3', 'usage': {'claude': {'windows': pooled}}})) + '\nnot json\n', encoding='utf-8')
        before = {p: p.stat().st_mtime_ns for p in self.home.rglob('*') if p.is_file()}
        found = plan_state_residue.scan(self.home, self.state)
        self.assertEqual(before, {p: p.stat().st_mtime_ns for p in self.home.rglob('*') if p.is_file()})
        kinds = {(row['kind'], row['account']) for row in found}
        self.assertEqual(kinds, {('plan_differs_from_owner', 'codex-2'), ('plan_differs_from_owner', 'pi'),
                                 ('plan_not_verified', 'chatgpt-web'), ('home_rewritten_by_caller', 'claude'),
                                 ('cache_copies_disagree', 'codex'), ('rate_across_plan_change', 'codex.usage'),
                                 ('ledger_readings_from_another_account', 'claude')})
        self.assertNotIn('DIGEST', json.dumps(found))
        ledger = next(row for row in found if row['kind'].startswith('ledger'))
        self.assertIn('2 rows, t2 to t3', ledger['detail'])


if __name__ == '__main__':
    unittest.main()
