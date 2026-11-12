"""Metered USD is spent dollars and an optional budget. It does not reset."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import unittest
import urllib.request
from unittest.mock import patch

import account_fleet
import accounts_override
import allocator
import candidate_eval
import provider_inventory
import quota_economy
import quota_pools
import route_judge
import state_upgrade
import usage_core

from router_test_support import isolated_router_state, profile, usage_windows


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup)


NOW = datetime(2026, 11, 12, 8, 0, tzinfo=timezone.utc)
PROBE = 'probe-model'
_PROXY_KEYS = (
    'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy',
    'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
)


@contextmanager
def _proxy_env(updates):
    saved = {key: os.environ.get(key) for key in _PROXY_KEYS}
    try:
        for key in _PROXY_KEYS:
            os.environ.pop(key, None)
        for key, value in updates.items():
            os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _accounts(budget):
    spec = {
        'billing': 'metered',
        'backend_kind': 'metered_usd',
        'verified': True,
        'windows': {},
        'bundles': {
            'usd_probe': {
                'provider': 'usd', 'harness': 'codex', 'model': PROBE, 'effort': 'high',
                'draws': [], 'billing': 'metered',
            },
        },
    }
    if budget is not None:
        spec['windows'] = {'balance': {}}
        spec['capacity_usd'] = {'balance': float(budget)}
        spec['bundles']['usd_probe']['draws'] = ['balance']
    return {
        'models': {PROBE: {'usd_per_hour': 10, 'api_family': 'codex'}},
        'aliases': {},
        'accounts': {'usd': spec},
        'scoring': {'safety_margin_pct': 5, 'short_window_hours': 24},
        # Already priced. launch_blockers would otherwise overlay public scores and drop this rate.
        '_five_axis': True,
    }


def _balance_usage(remaining):
    return {'read_ok': True, 'accounts': {'usd': {
        'status': 'fresh', 'fetched_at': NOW.isoformat(), 'available': True,
        'windows': [{
            'id': 'balance', 'used_pct': 100 - remaining, 'resets_at': None,
            'observed_remaining_pct': remaining, 'remaining_pct_before_reset': None,
            'seconds_until_reset': None,
        }],
    }}}


def _bundle(accounts):
    return {'account': 'usd', 'model': PROBE, 'id': 'usd_probe', 'draws': ['balance']}


class BalanceGate(unittest.TestCase):
    def test_spent_budget_excludes_and_names_no_reset(self):
        accounts = _accounts(100)
        blockers = quota_pools.launch_blockers(_bundle(accounts), accounts, _balance_usage(0), NOW)
        reasons = [reason for _wid, reason in blockers]
        self.assertTrue(reasons)
        text = ' '.join(reasons)
        self.assertIn('does not reset', text)
        self.assertNotIn('resets in', text)
        self.assertNotIn('for inf', text)
        self.assertNotRegex(text, r'\d{4}-\d{2}-\d{2}')

    def test_room_above_the_margin_is_not_an_exhausted_balance(self):
        accounts = _accounts(100)
        reasons = [reason for _wid, reason in quota_pools.launch_blockers(
            _bundle(accounts), accounts, _balance_usage(80), NOW)]
        self.assertFalse([reason for reason in reasons if 'balance' in reason])

    def test_a_finite_window_still_names_its_wait(self):
        accounts = {
            'models': {PROBE: {'usd_per_hour': 10}},
            'aliases': {},
            'accounts': {'acme': {
                'windows': {'seven_day': {'hours': 168}},
                'capacity_usd': {'seven_day': 100},
            }},
            'scoring': {'safety_margin_pct': 5, 'short_window_hours': 24},
            '_five_axis': True,
        }
        # Inside the margin, with a reset still ahead, so this is the wait wording and not "used up".
        usage = usage_windows(NOW, {'acme': [('seven_day', 4, 40)]})
        reasons = [reason for _wid, reason in quota_pools.launch_blockers(
            {'account': 'acme', 'model': PROBE, 'id': 'acme_probe'}, accounts, usage, NOW)]
        self.assertTrue(any('for ' in reason and 'h' in reason for reason in reasons))
        self.assertFalse(any('does not reset' in reason for reason in reasons))


class BalanceEconomy(unittest.TestCase):
    def _recovery(self, remaining):
        state = quota_pools.window_state(
            'balance', {}, _balance_usage(remaining)['accounts']['usd']['windows'][0],
            _accounts(100)['accounts']['usd'], NOW, 24)
        rec = quota_economy.five_axis_window_recovery(state, 100)
        self.assertIsNotNone(rec)
        rec['lump_usd'] = 10
        return state, rec

    def test_a_balance_does_not_move_a_recovery_clock(self):
        state, rec = self._recovery(20)
        self.assertEqual(state['reset_hours'], float('inf'))
        self.assertIsNone(state['reset_at'])
        self.assertEqual(rec['elapsed_frac'], 0.0)
        self.assertEqual(rec['reset_hours'], float('inf'))
        self.assertIsNone(None if rec['reset_hours'] == float('inf') else rec['reset_hours'])

    def test_need_rises_as_remaining_room_shrinks(self):
        _state, tight = self._recovery(20)
        _state, open_ = self._recovery(80)
        tight_p = quota_economy.five_axis_need_probability(tight, tight['used_usd'])
        open_p = quota_economy.five_axis_need_probability(open_, open_['used_usd'])
        self.assertGreaterEqual(tight_p, open_p)


class UncappedRouting(unittest.TestCase):
    def test_no_budget_is_not_a_missing_long_window(self):
        self.assertTrue(quota_pools.metered_usd_uncapped(_accounts(None)['accounts']['usd']))
        self.assertFalse(quota_pools.metered_usd_uncapped(_accounts(100)['accounts']['usd']))
        # omp is metered and has windows, and it is not this account.
        self.assertFalse(quota_pools.metered_usd_uncapped(
            {'billing': 'metered', 'windows': {'week': {'hours': 168}}}))

    def test_plan_keeps_uncapped_as_last_resort_and_excludes_a_spent_balance(self):
        base = account_fleet.load_accounts()
        fleet = {**base, 'accounts': {'claude': base['accounts']['claude']}}
        task = {'objective': 'Fix a bug in parser.py', 'expected_hours': 1, 'profile': profile()}
        claude_usage = usage_windows(NOW, {'claude': [('five_hour', 80, 4), ('seven_day', 80, 100)]})

        # A scored public model, so a missing score is not what excludes the dollar account.
        uncapped = _accounts(None)
        uncapped['accounts']['usd']['bundles']['usd_probe']['model'] = 'gpt-6.1-sol'
        fleet['accounts']['usd'] = uncapped['accounts']['usd']
        usage = {'read_ok': True, 'accounts': {
            **claude_usage['accounts'],
            'usd': {'status': 'fresh', 'fetched_at': NOW.isoformat(), 'available': True, 'windows': [],
                    'summary': '已用 $12.30', 'backend_kind': 'metered_usd',
                    'usage_details': {'kind': 'metered_usd', 'spent_usd': 12.3, 'budget_usd': None}},
        }}
        plan = allocator.plan(task, usage, NOW, accounts=fleet, overrides=[], records=[],
                              host_policy_config=None, read_local_state=False)
        usd = next(c for c in plan['ranking'] if c['id'] == 'usd_probe')
        self.assertNotIn(candidate_eval.QUOTA_NO_LONG_WINDOW, ' '.join(map(str, usd['excluded'])))
        subscription = [c for c in plan['ranking'] if c['bundle']['account'] == 'claude' and not c['excluded']]
        if subscription:
            self.assertEqual(usd['excluded'], [candidate_eval.METERED_LAST_RESORT])
        self.assertNotIn('usd_probe', str(plan['decision'].get('next_reset') or ''))

        spent = _accounts(100)
        spent['accounts']['usd']['bundles']['usd_probe']['model'] = 'gpt-6.1-sol'
        fleet['accounts']['usd'] = spent['accounts']['usd']
        usage['accounts']['usd'] = _balance_usage(0)['accounts']['usd']
        spent_plan = allocator.plan(task, usage, NOW, accounts=fleet, overrides=[], records=[],
                                    host_policy_config=None, read_local_state=False)
        row = next(c for c in spent_plan['ranking'] if c['id'] == 'usd_probe')
        text = ' '.join(map(str, row['excluded']))
        self.assertIn('does not reset', text)
        self.assertNotIn('resets in', text)
        self.assertNotRegex(text, r'\d{4}-\d{2}-\d{2}')
        for window in (spent_plan['decision']['system'].get('quota_windows') or []):
            if window.get('window') == 'balance':
                self.assertIsNone(window.get('reset_in_hours'))


class Doctor(unittest.TestCase):
    def test_old_meter_fields_are_named_and_left_in_place(self):
        path = Path(os.environ['AGENTCOMPASS_ACCOUNTS_OVERRIDE'])
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            'schema_version': 2,
            'litellm_proxies': {'old': {}},
            'accounts': {
                'llmproxy': {
                    'billing': 'metered',
                    'endpoint': 'https://proxy.example.test',
                    'api_key': 'sk-team-proxy',
                    'windows': {'spend': {'hours': 168}},
                    'capacity_usd': {'spend': 200},
                    'burn_pct_per_worker_hour': {'spend': 1},
                    'burn_basis': 'old',
                    'budget_duration': '7d',
                    'timezone': 'UTC+8',
                },
            },
        }
        path.write_text(json.dumps(doc), encoding='utf-8', newline='\n')
        before = path.read_bytes()
        issue = state_upgrade.check_accounts_override(path)
        self.assertEqual(path.read_bytes(), before)
        for field in ('windows', 'capacity_usd', 'burn_pct_per_worker_hour', 'burn_basis',
                      'budget_duration', 'timezone'):
            self.assertIn(field, issue)
        self.assertIn(state_upgrade.LEGACY_METER_REASON, issue)
        self.assertIn(state_upgrade.FIX_SETUP, issue)
        self.assertNotIn('re-toggle in the tray', issue)
        self.assertNotIn('Remove those fields', issue)
        proxies = {'schema_version': 2, 'litellm_proxies': {'old': {}}, 'accounts': {}}
        path.write_text(json.dumps(proxies), encoding='utf-8', newline='\n')
        proxy_issue = state_upgrade.check_accounts_override(path)
        self.assertIn('litellm_proxies', proxy_issue)
        self.assertIn(state_upgrade.FIX_SETUP, proxy_issue)
        self.assertEqual(path.read_text(encoding='utf-8'), json.dumps(proxies))

        omp = {'schema_version': 2, 'accounts': {
            'omp': {'billing': 'metered', 'windows': {'week': {'hours': 168}}},
            'claude': {'billing': 'subscription', 'windows': {'seven_day': {'hours': 168}}},
        }}
        state_upgrade.validate_accounts_override_doc(omp, path)
        self.assertEqual(state_upgrade.legacy_meter_field_names(omp['accounts']['omp']), [])
        self.assertEqual(state_upgrade.legacy_meter_field_names(omp['accounts']['claude']), [])

        kind = {'schema_version': 2, 'accounts': {
            'old': {'backend_kind': 'litellm', 'endpoint': 'https://proxy.example.test', 'api_key': 'sk-team-proxy'},
        }}
        state_upgrade.validate_accounts_override_doc(kind, path)
        path.write_text(json.dumps(kind), encoding='utf-8', newline='\n')
        kind_issue = state_upgrade.check_accounts_override(path)
        self.assertIn('backend_kind=litellm', kind_issue)
        self.assertIn(state_upgrade.LEGACY_METER_REASON, kind_issue)
        self.assertIn(state_upgrade.FIX_SETUP, kind_issue)


class SpendRead(unittest.TestCase):
    def _clear_usd_cache(self):
        def update(cache):
            providers = cache.get('providers')
            if isinstance(providers, dict):
                providers.pop('usd', None)
            snaps = cache.get('display_snapshots')
            if isinstance(snaps, dict):
                snaps.pop('usd', None)
        usage_core.update_usage_cache(update)

    def _account(self, models):
        path = Path(os.environ['AGENTCOMPASS_ACCOUNTS_OVERRIDE'])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            'schema_version': 2,
            'accounts': {'usd': {
                'billing': 'metered', 'label': 'USD',
                'endpoint': 'https://proxy.example.test/v1',
                'api_key': 'sk-team-proxy',
                'budget_usd': 200,
                'models': models,
            }},
        }), encoding='utf-8', newline='\n')

    def test_key_info_is_enough(self):
        self._account(['declared-model'])
        calls = []

        def fake(url, method='GET', headers=None, body=None, timeout=20):
            calls.append((method, url, (body or {}).get('model')))
            self.assertNotIn('sk-team-proxy', url)
            return 200, {'info': {'spend': 2.65, 'max_budget': 50}}, {}

        with patch.object(usage_core, 'http_json_response', side_effect=fake):
            row = usage_core.fetch_metered_usd_usage('usd')
        self.assertEqual(calls, [('GET', 'https://proxy.example.test/key/info', None)])
        self.assertEqual(row.summary, '已用 $2.65 / $200')
        self.assertEqual(row.windows[0].resets_at, None)
        self.assertAlmostEqual(row.windows[0].used_pct, 1.325)
        self.assertEqual(row.usage_details, {'kind': 'metered_usd', 'spent_usd': 2.65, 'budget_usd': 200})
        self.assertEqual(row.backend_kind, 'metered_usd')

    def test_permission_denial_is_one_get_and_stores_no_spend(self):
        for status in (401, 403):
            with self.subTest(status=status):
                self._clear_usd_cache()
                self._account(['declared-model'])
                calls = []

                def fake(url, method='GET', headers=None, body=None, timeout=20, status=status):
                    calls.append((method, url, body))
                    self.assertNotIn('sk-team-proxy', url)
                    return status, {}, {}

                with patch.object(usage_core, 'http_json_response', side_effect=fake):
                    row = usage_core.fetch_metered_usd_usage('usd')
                self.assertEqual([call[0] for call in calls], ['GET'])
                self.assertTrue(calls[0][1].endswith('/key/info'))
                self.assertIsNone(calls[0][2])
                self.assertFalse(row.available)
                self.assertIsNone(row.error)
                self.assertIsNone(row.summary)
                self.assertEqual(row.windows, [])
                self.assertEqual(row.usage_details, {'kind': 'metered_usd', 'query': 'no_permission'})
                blob = json.dumps(usage_core._provider_to_dict(row), ensure_ascii=False)
                self.assertNotIn('已用', blob)
                self.assertNotIn('$', blob)
                self.assertNotIn('读不到', blob)
                if status == 403:
                    cached = usage_core.read_cached_usage(['usd'])
                    self.assertEqual(cached[0]['usage_details']['query'], 'no_permission')
                    self.assertEqual(cached[0]['windows'], [])
                    self.assertIsNone(cached[0]['summary'])

    def test_other_misses_stay_read_failures(self):
        cases = (
            (500, {'error': 'nope'}, '/key/info 返回 500'),
            (0, 'timed out', 'timed out'),
            (0, '', '网络错误'),
            (0, 'reset sk-team-proxy Bearer secret', '读取失败'),
            (200, {'info': {}}, '/key/info 没有已用金额'),
        )
        for status, payload, reason in cases:
            with self.subTest(status=status, reason=reason):
                self._clear_usd_cache()
                self._account([])
                calls = []

                def fake(url, method='GET', headers=None, body=None, timeout=20, status=status, payload=payload):
                    calls.append(method)
                    return status, payload, {}

                with patch.object(usage_core, 'http_json_response', side_effect=fake):
                    row = usage_core.fetch_metered_usd_usage('usd')
                self.assertEqual(calls, ['GET'])
                self.assertEqual(row.usage_details['query'], 'failed')
                self.assertEqual(row.usage_details['read_error'], reason)
                self.assertNotIn('无查询权限', reason)
                self.assertNotIn('sk-', row.usage_details['read_error'])
                self.assertNotIn('Bearer', row.usage_details['read_error'])
                self.assertEqual(row.windows, [])
                self.assertIsNone(row.summary)

    def test_spent_first_text_keeps_cents_and_has_no_countdown(self):
        self.assertEqual(usage_core.format_metered_usd(2.65, 200), '已用 $2.65 / $200')
        self.assertEqual(usage_core.format_metered_usd(12.3, None), '已用 $12.30')
        self.assertEqual(usage_core.format_usd_amount(200), '$200')
        self.assertNotIn('reset', usage_core.format_metered_usd(2.65, 200).lower())


class Snapshot(unittest.TestCase):
    def test_null_reset_is_still_a_current_balance(self):
        path = Path(os.environ['ROUTER_USAGE_CACHE'])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'providers': {'usd': {
            'provider_id': 'usd', 'backend_kind': 'metered_usd', 'billing': 'metered',
            'available': True, 'stale': False, 'fetched_at': NOW.isoformat(),
            'summary': '已用 $12.34 / $200', 'plan_label': '$200',
            'windows': [{'id': 'balance', 'label': '已用', 'used_pct': 6.17, 'resets_at': None}],
            'usage_details': {'kind': 'metered_usd', 'spent_usd': 12.34, 'budget_usd': 200,
                              'api_key': 'sk-team-proxy'},
        }}}), encoding='utf-8', newline='\n')
        snap = route_judge.usage_snapshot(path, accounts=['usd'], now=NOW)
        row = snap['accounts']['usd']
        self.assertEqual(row['status'], 'fresh')
        self.assertIsNone(row['windows'][0]['resets_at'])
        self.assertIsNone(row['windows'][0]['remaining_pct_before_reset'])
        self.assertEqual(row['summary'], '已用 $12.34 / $200')
        self.assertEqual(row['usage_details'], {'kind': 'metered_usd', 'spent_usd': 12.34, 'budget_usd': 200})
        self.assertNotIn('sk-team-proxy', json.dumps(row))

    def test_a_permission_miss_adds_no_window_and_no_spend(self):
        path = Path(os.environ['ROUTER_USAGE_CACHE'])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'providers': {'usd': {
            'provider_id': 'usd', 'backend_kind': 'metered_usd', 'billing': 'metered',
            'available': False, 'stale': False, 'fetched_at': NOW.isoformat(),
            'windows': [],
            'usage_details': {'kind': 'metered_usd', 'query': 'no_permission'},
        }}}), encoding='utf-8', newline='\n')
        snap = route_judge.usage_snapshot(path, accounts=['usd'], now=NOW)
        row = snap['accounts']['usd']
        self.assertFalse(row['available'])
        self.assertEqual(row['status'], 'stale')
        self.assertEqual(row['windows'], [])
        self.assertEqual(row['usage_details'], {'kind': 'metered_usd', 'query': 'no_permission'})
        self.assertNotIn('spent_usd', row['usage_details'])
        self.assertNotIn('summary', row)
        self.assertNotIn('无查询权限', json.dumps(row, ensure_ascii=False))


class LegacyMeterFleet(unittest.TestCase):
    def _write_dirty(self):
        path = Path(os.environ['AGENTCOMPASS_ACCOUNTS_OVERRIDE'])
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            'schema_version': 2,
            'litellm_proxies': {'old': {}},
            'accounts': {
                'llmproxy': {
                    'billing': 'metered',
                    'endpoint': 'https://example.test/v1',
                    'api_key': 'sk-team-proxy',
                    'budget_usd': 200,
                    'windows': {'spend': {'hours': 168}},
                    'capacity_usd': {'spend': 200},
                    'burn_pct_per_worker_hour': {'spend': 1},
                    'burn_basis': 'old',
                    'budget_duration': '7d',
                    'timezone': 'UTC+8',
                    'backend_kind': 'litellm',
                },
                'usd': {
                    'billing': 'metered',
                    'endpoint': 'https://example.test/v1',
                    'api_key': 'sk-team-proxy',
                    'budget_usd': 40,
                },
                'omp': {'billing': 'metered', 'windows': {'week': {'hours': 168}}},
            },
        }
        path.write_text(json.dumps(doc), encoding='utf-8', newline='\n')
        return path

    def test_one_dirty_account_does_not_block_the_fleet_and_stays_out_when_pinned(self):
        path = self._write_dirty()
        before = path.read_bytes()
        loaded = accounts_override.load_override(path)
        self.assertIn('llmproxy', loaded['accounts'])
        self.assertEqual(path.read_bytes(), before)
        found = provider_inventory.discover()
        dirty = found['accounts']['llmproxy']
        self.assertEqual(dirty['status'], 'legacy_meter')
        self.assertIn('旧的额度字段残留', dirty['reason'])
        self.assertEqual(found['accounts']['usd']['status'], 'ready')
        # A native omp login is not this dollar account. Override windows on it are not a leftover meter.
        omp = found['accounts'].get('omp') or {}
        self.assertNotEqual(omp.get('status'), 'legacy_meter')
        self.assertNotIn('旧的额度字段残留', str(omp.get('reason') or ''))
        with patch.object(provider_inventory, 'current', return_value=found):
            fleet = account_fleet.load_accounts()
        self.assertEqual(path.read_bytes(), before)
        self.assertIn('claude', fleet['accounts'])
        spec = fleet['accounts']['llmproxy']
        self.assertIn('旧的额度字段残留', spec['legacy_meter'])
        self.assertEqual(spec['windows'], {})
        self.assertNotIn('capacity_usd', spec)
        self.assertNotIn('api_key', spec)
        clean = fleet['accounts']['usd']
        self.assertNotIn('legacy_meter', clean)
        self.assertIn('balance', clean['windows'])
        self.assertEqual(clean['capacity_usd']['balance'], 40)
        spec['bundles'] = {'llmproxy_pin': {
            'provider': 'llmproxy', 'harness': 'codex', 'model': 'gpt-6.1-sol',
            'effort': 'high', 'draws': [], 'billing': 'metered',
        }}
        task = {
            'objective': 'Fix a bug in parser.py', 'expected_hours': 1, 'profile': profile(),
            'constraints': {'model': 'gpt-6.1-sol'},
        }
        plan = allocator.plan(
            task, {'read_ok': True, 'accounts': {}}, NOW, accounts=fleet, overrides=[], records=[],
            host_policy_config=None, read_local_state=False)
        row = next(c for c in plan['ranking'] if c['id'] == 'llmproxy_pin')
        self.assertTrue(any('旧的额度字段残留' in str(reason) for reason in row['excluded']))
        self.assertNotEqual(plan.get('choice'), 'llmproxy_pin')
        if plan.get('choice') not in (None, 'none'):
            chosen = next(c for c in plan['ranking'] if c['id'] == plan['choice'])
            self.assertNotEqual((chosen.get('bundle') or {}).get('account'), 'llmproxy')

    def test_the_gate_names_that_account_and_does_not_allow_every_account(self):
        import sys
        setup = Path(__file__).resolve().parents[3] / 'setup'
        if str(setup) not in sys.path:
            sys.path.insert(0, str(setup))
        import router_gate_hook
        path = self._write_dirty()
        before = path.read_bytes()
        found = provider_inventory.discover()
        with patch.object(provider_inventory, 'current', return_value=found):
            accounts, _policy = router_gate_hook.load_dispatch_settings()
        self.assertIsNotNone(accounts)
        self.assertIn('claude', accounts['accounts'])
        self.assertIn('llmproxy', accounts['accounts'])
        self.assertEqual(path.read_bytes(), before)
        cause = router_gate_hook.check_target_disabled(
            {'agent_id': 'worker-1'}, 'llmproxy', [], accounts, None)
        self.assertIn('旧的额度字段残留', cause)


class SetupCleanup(unittest.TestCase):
    def test_setup_strips_meter_fields_and_old_usage_keys_without_a_dry_run_write(self):
        import sys
        import tempfile
        setup = Path(__file__).resolve().parents[3] / 'setup'
        if str(setup) not in sys.path:
            sys.path.insert(0, str(setup))
        import onboard
        home = Path(tempfile.mkdtemp(prefix='metered-setup-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(home, ignore_errors=True))
        override = home / '.agent-compass' / 'accounts-override.json'
        override.parent.mkdir(parents=True)
        doc = {
            'schema_version': 2,
            'litellm_proxies': {'old': {}},
            'accounts': {'llmproxy': {
                'billing': 'metered',
                'endpoint': 'https://example.test/v1',
                'api_key': 'sk-team-proxy',
                'budget_usd': 200,
                'windows': {'spend': {'hours': 168}},
                'capacity_usd': {'spend': 200},
                'timezone': 'UTC+8',
                'backend_kind': 'proxy',
            }},
        }
        override.write_text(json.dumps(doc), encoding='utf-8', newline='\n')
        before = override.read_bytes()
        dry = onboard.write_override(home, {'accounts': []}, set(), dry_run=True)
        self.assertEqual(override.read_bytes(), before)
        self.assertTrue(dry['stripped'])
        written = onboard.write_override(home, {'accounts': []}, set(), dry_run=False)
        self.assertTrue(written['stripped'])
        saved = json.loads(override.read_text(encoding='utf-8'))
        spec = saved['accounts']['llmproxy']
        self.assertEqual(spec['endpoint'], 'https://example.test/v1')
        self.assertEqual(spec['api_key'], 'sk-team-proxy')
        self.assertEqual(spec['budget_usd'], 200)
        self.assertEqual(spec['billing'], 'metered')
        for field in ('windows', 'capacity_usd', 'timezone', 'backend_kind'):
            self.assertNotIn(field, spec)
        self.assertNotIn('litellm_proxies', saved)
        collector = home / '.agent-compass' / 'usage-config.json'
        plugin = home / '.usage-float' / 'config.json'
        plugin.parent.mkdir(parents=True)
        collector.write_text(
            json.dumps({'language': 'zh', 'llm_proxy_timezone': 'UTC+8'}),
            encoding='utf-8', newline='\n')
        plugin.write_text(
            json.dumps({'language': 'en', 'llm_proxy_api_key': 'sk-test-key', 'card_width': 320}),
            encoding='utf-8', newline='\n')
        preview = onboard.clear_obsolete_usage_configs(home, dry_run=True)
        self.assertIn('llm_proxy_timezone', preview[str(collector)])
        self.assertIn('llm_proxy_api_key', preview[str(plugin)])
        self.assertIn('llm_proxy_timezone', collector.read_text(encoding='utf-8'))
        cleared = onboard.clear_obsolete_usage_configs(home, dry_run=False)
        self.assertEqual(cleared[str(collector)], ['llm_proxy_timezone'])
        self.assertEqual(cleared[str(plugin)], ['llm_proxy_api_key'])
        self.assertEqual(json.loads(collector.read_text(encoding='utf-8'))['language'], 'zh')
        plugin_saved = json.loads(plugin.read_text(encoding='utf-8'))
        self.assertEqual(plugin_saved['language'], 'en')
        self.assertEqual(plugin_saved['card_width'], 320)
        self.assertNotIn('llm_proxy_api_key', plugin_saved)
        self.assertNotIn('sk-test-key', plugin.read_text(encoding='utf-8'))

    def test_setup_clears_old_fields_and_leaves_the_account_switch(self):
        import sys
        import tempfile
        setup = Path(__file__).resolve().parents[3] / 'setup'
        if str(setup) not in sys.path:
            sys.path.insert(0, str(setup))
        import onboard
        home = Path(tempfile.mkdtemp(prefix='metered-policy-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(home, ignore_errors=True))
        state = home / '.agent-compass'
        state.mkdir(parents=True)
        override = state / 'accounts-override.json'
        override.write_text(json.dumps({
            'schema_version': 2,
            'accounts': {
                'llmproxy': {
                    'billing': 'metered',
                    'endpoint': 'https://example.test/v1',
                    'api_key': 'sk-team-proxy',
                    'budget_usd': 200,
                    'windows': {'spend': {'hours': 168}},
                    'capacity_usd': {'spend': 200},
                },
                'claude': {'verified': True},
            },
        }), encoding='utf-8', newline='\n')
        policy = state / 'host-policy.json'
        policy.write_text(
            json.dumps({'allowed_accounts': ['llmproxy', 'claude']}) + '\n',
            encoding='utf-8', newline='\n')
        before_policy = policy.read_bytes()
        onboard.write_override(home, {'accounts': []}, set(), dry_run=False)
        self.assertEqual(policy.read_bytes(), before_policy)
        saved = json.loads(override.read_text(encoding='utf-8'))
        spec = saved['accounts']['llmproxy']
        self.assertNotIn('windows', spec)
        self.assertNotIn('capacity_usd', spec)
        self.assertEqual(spec['endpoint'], 'https://example.test/v1')
        self.assertEqual(spec['api_key'], 'sk-team-proxy')
        self.assertEqual(spec['budget_usd'], 200)
        self.assertEqual(saved['accounts']['claude'], {'verified': True})
        bare = Path(tempfile.mkdtemp(prefix='metered-policy-bare-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(bare, ignore_errors=True))
        bare_state = bare / '.agent-compass'
        bare_state.mkdir(parents=True)
        (bare_state / 'accounts-override.json').write_text(json.dumps({
            'schema_version': 2,
            'accounts': {'llmproxy': {
                'billing': 'metered',
                'endpoint': 'https://example.test/v1',
                'api_key': 'sk-team-proxy',
                'windows': {'spend': {'hours': 168}},
            }},
        }), encoding='utf-8', newline='\n')
        onboard.write_override(bare, {'accounts': []}, set(), dry_run=False)
        self.assertFalse((bare_state / 'host-policy.json').exists())
        bare_spec = json.loads((bare_state / 'accounts-override.json').read_text(encoding='utf-8'))['accounts']['llmproxy']
        self.assertNotIn('windows', bare_spec)
        self.assertIn('运行 agentcompass setup 清掉旧字段', state_upgrade.LEGACY_METER_REASON)
        self.assertNotIn('托盘', state_upgrade.LEGACY_METER_REASON)


class HarnessCandidates(unittest.TestCase):
    """A self-built harness key is read live. Display follows /key/info and the key stays off disk."""

    SECRET = 'fixture-harness-key'
    CODEX_SECRET = 'fixture-codex-harness-key'

    def setUp(self):
        self._saved = (
            usage_core.HOME, usage_core.CLAUDE_HOME, usage_core.CLAUDE_HOME_2,
            usage_core.CODEX_HOME, usage_core.CODEX_HOME_2, usage_core.GROK_HOME,
            usage_core.STATE_DIR, usage_core.CONFIG_PATH, usage_core.CACHE_PATH,
            usage_core.CLAUDE_CLI_PACKAGE_PATHS, usage_core.CODEX_ACCOUNTS,
            dict(usage_core.CODEX_ACCOUNT_BY_ID),
        )
        usage_core.configure_paths(
            home=Path(os.environ['ROUTER_PROVIDER_HOME']),
            state_dir=Path(os.environ['ROUTER_STATE_DIR']),
        )
        home = Path(os.environ['ROUTER_PROVIDER_HOME'])
        for relative in (
            '.claude/settings.json', '.claude/usage-float.json',
            '.codex/config.toml', '.codex/auth.json',
        ):
            path = home / relative
            if path.is_file():
                path.unlink()

    def tearDown(self):
        (usage_core.HOME, usage_core.CLAUDE_HOME, usage_core.CLAUDE_HOME_2,
         usage_core.CODEX_HOME, usage_core.CODEX_HOME_2, usage_core.GROK_HOME,
         usage_core.STATE_DIR, usage_core.CONFIG_PATH, usage_core.CACHE_PATH,
         usage_core.CLAUDE_CLI_PACKAGE_PATHS, usage_core.CODEX_ACCOUNTS,
         usage_core.CODEX_ACCOUNT_BY_ID) = self._saved

    def _home(self):
        return Path(os.environ['ROUTER_PROVIDER_HOME'])

    def _write_claude(self, base, secret=None):
        home = self._home() / '.claude'
        home.mkdir(parents=True, exist_ok=True)
        (home / 'settings.json').write_text(json.dumps({'env': {
            'ANTHROPIC_BASE_URL': base,
            'ANTHROPIC_API_KEY': secret or self.SECRET,
        }}), encoding='utf-8')

    def _write_codex(self, base, secret=None):
        home = self._home() / '.codex'
        home.mkdir(parents=True, exist_ok=True)
        (home / 'config.toml').write_text(
            'model_provider = "proxy"\nmodel = "gpt-test"\n\n'
            '[model_providers.proxy]\n'
            f'base_url = "{base}"\n'
            f'experimental_bearer_token = "{secret or self.CODEX_SECRET}"\n',
            encoding='utf-8')

    def _override(self, accounts):
        path = Path(os.environ['AGENTCOMPASS_ACCOUNTS_OVERRIDE'])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'schema_version': 2, 'accounts': accounts}), encoding='utf-8')

    def _clear_override(self):
        path = Path(os.environ['AGENTCOMPASS_ACCOUNTS_OVERRIDE'])
        if path.is_file():
            path.unlink()

    def _assert_secret_absent(self, *secrets):
        blobs = [json.dumps(provider_inventory.discover(), ensure_ascii=False)]
        cache_path = Path(os.environ['ROUTER_USAGE_CACHE'])
        if cache_path.is_file():
            blobs.append(cache_path.read_text(encoding='utf-8'))
        override = Path(os.environ['AGENTCOMPASS_ACCOUNTS_OVERRIDE'])
        if override.is_file():
            blobs.append(override.read_text(encoding='utf-8'))
        inventory_path = Path(os.environ['ROUTER_STATE_DIR']) / 'provider-inventory.json'
        if inventory_path.is_file():
            blobs.append(inventory_path.read_text(encoding='utf-8'))
        joined = '\n'.join(blobs)
        for secret in secrets:
            self.assertNotIn(secret, joined)

    def _clear_provider_cache(self, provider='claude'):
        def update(cache):
            providers = cache.get('providers')
            if isinstance(providers, dict):
                providers.pop(provider, None)
            snaps = cache.get('display_snapshots')
            if isinstance(snaps, dict):
                snaps.pop(provider, None)
        usage_core.update_usage_cache(update)

    def _fetch(self, status, payload, provider='claude'):
        calls = []

        def fake(url, method='GET', headers=None, body=None, timeout=20):
            calls.append((method, url, body, headers or {}))
            return status, payload, {}

        with patch.object(usage_core, 'http_json_response', side_effect=fake):
            row = usage_core.fetch_metered_usd_usage(provider)
        return row, calls

    def test_permission_denial_stores_no_number_and_leaves_billing_unknown(self):
        self._clear_override()
        self._clear_provider_cache()
        self._write_claude('https://proxy.example.test/v1')
        for status in (401, 403):
            with self.subTest(status=status):
                row, calls = self._fetch(status, {})
                self.assertEqual([call[0] for call in calls], ['GET'])
                self.assertTrue(calls[0][1].endswith('/key/info'))
                self.assertIsNone(calls[0][2])
                self.assertEqual(row.usage_details, {'kind': 'metered_usd', 'query': 'no_permission'})
                self.assertIsNone(row.billing)
                self.assertIsNone(row.summary)
                cached = usage_core.load_usage_cache()['providers']['claude']
                blob = json.dumps(cached, ensure_ascii=False)
                self.assertNotIn('$', blob)
                self.assertNotIn('spent_usd', blob)
                self.assertNotIn('已用', blob)
                self.assertNotIn('billing', cached)
                self.assertEqual(cached['backend_kind'], 'metered_usd')
                discovered = provider_inventory.discover()['accounts']['claude']
                self.assertEqual(discovered['billing'], 'unknown')
                self.assertNotEqual(discovered.get('backend_kind'), 'metered_usd')
                self._assert_secret_absent(self.SECRET)

    def test_first_miss_does_not_prove_billing(self):
        self._clear_override()
        self._clear_provider_cache()
        self._write_claude('https://proxy.example.test/v1')
        for status, payload in ((404, {'error': 'missing'}), (0, 'timed out'), (200, {'info': {}})):
            with self.subTest(status=status):
                self._clear_provider_cache()
                row, calls = self._fetch(status, payload)
                self.assertEqual(calls[0][0], 'GET')
                self.assertEqual(row.usage_details, {'kind': 'metered_usd', 'query': 'not_metered'})
                self.assertTrue(row.available)
                self.assertIsNone(row.error)
                self.assertIsNone(row.summary)
                cached = usage_core.load_usage_cache()['providers']['claude']
                self.assertNotEqual(cached.get('backend_kind'), 'metered_usd')
                self.assertNotEqual(cached.get('usage_details', {}).get('query'), 'failed')
                self.assertNotIn('billing', cached)
                self.assertNotIn('spent_usd', cached.get('usage_details') or {})
                discovered = provider_inventory.discover()['accounts']['claude']
                self.assertEqual(discovered['billing'], 'unknown')
                self.assertTrue(discovered.get('usd_candidate'))
                self._assert_secret_absent(self.SECRET)

    def test_cross_origin_redirect_is_not_followed(self):
        other_seen = []

        class Other(BaseHTTPRequestHandler):
            def do_GET(self):
                other_seen.append((self.path, self.headers.get('Authorization')))
                body = b'{"info":{"spend":1}}'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                return

        other = ThreadingHTTPServer(('127.0.0.1', 0), Other)
        other_port = other.server_address[1]

        class Source(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header('Location', f'http://127.0.0.1:{other_port}/elsewhere')
                self.end_headers()

            def log_message(self, fmt, *args):
                return

        source = ThreadingHTTPServer(('127.0.0.1', 0), Source)
        threads = []
        for server in (other, source):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            threads.append(thread)
        try:
            with _proxy_env({'no_proxy': '127.0.0.1,localhost'}):
                status, _payload, _headers = usage_core.http_json_response(
                    f'http://127.0.0.1:{source.server_address[1]}/key/info',
                    headers={'Authorization': 'Bearer fixture-secret'},
                )
        finally:
            for server in (source, other):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=5)
        self.assertEqual(status, 302)
        self.assertEqual(other_seen, [])

    def test_usage_request_uses_the_environment_proxy(self):
        seen = []

        class Destination(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append('destination')
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                return

        class Proxy(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append('proxy')
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                return

        destination = ThreadingHTTPServer(('127.0.0.1', 0), Destination)
        proxy = ThreadingHTTPServer(('127.0.0.1', 0), Proxy)
        threads = []
        for server in (destination, proxy):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            threads.append(thread)
        dest_url = f'http://127.0.0.1:{destination.server_address[1]}/key/info'
        proxy_url = f'http://127.0.0.1:{proxy.server_address[1]}'
        try:
            with _proxy_env({'http_proxy': proxy_url, 'HTTP_PROXY': proxy_url}):
                with urllib.request.urlopen(dest_url, timeout=5) as response:
                    response.read()
                status, _payload, _headers = usage_core.http_json_response(dest_url)
        finally:
            for server in (destination, proxy):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=5)
        self.assertEqual(status, 200)
        self.assertEqual(seen, ['proxy', 'proxy'])

    def test_reading_is_discarded_when_the_key_changes_before_it_is_stored(self):
        self._clear_override()
        self._clear_provider_cache()
        self._write_claude('https://proxy.example.test/v1', secret='fixture-old-key')
        old = usage_core.discover_metered_usd_accounts()['claude'].credential_key

        def fake(url, method='GET', headers=None, body=None, timeout=20):
            self._write_claude('https://proxy.example.test/v1', secret='fixture-new-key')
            return 200, {'info': {'spend': 7}}, {}

        with patch.object(usage_core, 'http_json_response', side_effect=fake):
            usage_core.fetch_metered_usd_usage('claude')
        new = usage_core.discover_metered_usd_accounts()['claude'].credential_key
        self.assertNotEqual(old, new)
        cached = (usage_core.load_usage_cache().get('providers') or {}).get('claude')
        self.assertIsNone(provider_inventory._metered_spend_evidence(cached, new))
        if isinstance(cached, dict) and cached.get('credential_key') == new:
            self.assertNotEqual((cached.get('usage_details') or {}).get('spent_usd'), 7)

    def test_a_later_miss_keeps_the_category_and_follows_the_latest_read(self):
        self._clear_override()
        self._clear_provider_cache()
        self._write_claude('https://proxy.example.test/v1')
        proved, _calls = self._fetch(200, {'info': {'spend': 7}})
        self.assertEqual(proved.usage_details['spent_usd'], 7)
        key = provider_inventory.discover()['accounts']['claude']['credential_key']
        cases = (
            (401, {}, 'permission'),
            (403, {}, 'permission'),
            (0, 'timed out', 'failure'),
            (0, '', 'failure'),
            (302, '', 'failure'),
            (404, {'error': 'missing'}, 'failure'),
        )
        for status, payload, kind in cases:
            with self.subTest(status=status, payload=payload):
                self._fetch(200, {'info': {'spend': 7}})
                row, _calls = self._fetch(status, payload)
                cached = usage_core.load_usage_cache()['providers']['claude']
                details = cached['usage_details']
                self.assertTrue(provider_inventory._metered_category_evidence(cached, key))
                self.assertIsNone(provider_inventory._metered_spend_evidence(cached, key))
                self.assertEqual(details.get('category'), 'metered')
                self.assertNotIn('spent_usd', details)
                self.assertNotIn('spent_usd', row.usage_details or {})
                self.assertIsNone(row.summary)
                self.assertFalse(row.available)
                self.assertFalse(row.available and not row.error)
                self.assertEqual(provider_inventory.discover()['accounts']['claude']['billing'], 'metered')
                if kind == 'permission':
                    self.assertEqual(row.usage_details, {'kind': 'metered_usd', 'query': 'no_permission'})
                    self.assertIsNone(row.error)
                else:
                    self.assertEqual(row.usage_details.get('query'), 'failed')
                    self.assertTrue(row.error)
                    self.assertNotEqual(details.get('query'), 'not_metered')

    def test_a_timeout_after_a_proved_spend_does_not_reset_failures(self):
        self._clear_override()
        self._clear_provider_cache()
        self._write_claude('https://proxy.example.test/v1')
        proved, _calls = self._fetch(200, {'info': {'spend': 7}})
        self.assertEqual(proved.usage_details['spent_usd'], 7)
        key = usage_core.provider_credential_key('claude')
        usage_core.record_usage_attempt('claude', key, failures=2, retry_seconds=120)

        def fake(url, method='GET', headers=None, body=None, timeout=20):
            return 0, 'timed out', {}

        with patch.object(usage_core, 'http_json_response', side_effect=fake):
            usage_core.collect_local_usage(['claude'], force=True)
        attempt = (usage_core.load_usage_cache().get('attempts') or {}).get('claude') or {}
        cached = usage_core.load_usage_cache()['providers']['claude']
        self.assertEqual(attempt.get('failures'), 3)
        self.assertNotIn('spent_usd', cached.get('usage_details') or {})
        self.assertEqual(cached['usage_details'].get('category'), 'metered')
        self.assertTrue(cached.get('error') or cached.get('stale') or cached.get('available') is False)
        self.assertFalse(cached.get('available') is True and not cached.get('error'))
        self.assertEqual(provider_inventory.discover()['accounts']['claude']['billing'], 'metered')

    def test_handwritten_builtin_id_stores_the_reading(self):
        self._clear_override()
        for provider in ('claude', 'claude-2', 'codex'):
            with self.subTest(provider=provider):
                self._clear_provider_cache(provider)
                if provider == 'codex':
                    secret_path = self._home() / '.codex' / 'auth.json'
                    secret_path.parent.mkdir(parents=True, exist_ok=True)
                    secret_path.write_text(json.dumps({
                        'tokens': {'access_token': 'fixture-old-key', 'account_id': 'acct'},
                    }), encoding='utf-8')
                else:
                    secret_path = usage_core.claude_account_home(provider) / '.credentials.json'
                    secret_path.parent.mkdir(parents=True, exist_ok=True)
                    secret_path.write_text(json.dumps({
                        'claudeAiOauth': {'accessToken': 'fixture-old-key'},
                    }), encoding='utf-8')
                try:
                    self._override({provider: {
                        'billing': 'metered',
                        'endpoint': f'https://{provider}.example.test/v1',
                        'api_key': 'fixture-handwritten-key',
                        'label': provider,
                    }})
                    account = usage_core.discover_metered_usd_accounts()[provider]
                    sent = usage_core._metered_sent_digest(account)
                    self.assertEqual(usage_core.provider_credential_key(provider), sent)
                    row, calls = self._fetch(200, {'info': {'spend': 7}}, provider=provider)
                    self.assertEqual([call[0] for call in calls], ['GET'])
                    self.assertTrue(row.available)
                    self.assertIsNone(row.error)
                    self.assertEqual((row.usage_details or {}).get('spent_usd'), 7)
                    cached = usage_core.load_usage_cache()['providers'][provider]
                    self.assertEqual(cached['usage_details']['spent_usd'], 7)
                    self.assertEqual(cached.get('credential_key'), sent)
                finally:
                    secret_path.unlink(missing_ok=True)

    def test_one_non_utf8_codex_config_skips_only_that_harness(self):
        self._clear_override()
        codex = self._home() / '.codex'
        codex.mkdir(parents=True, exist_ok=True)
        (codex / 'config.toml').write_bytes(b'\xff')
        self._write_claude('https://proxy.example.test/v1')
        self._override({'handwritten': {
            'billing': 'metered',
            'endpoint': 'https://handwritten.example.test/v1',
            'api_key': 'fixture-handwritten-key',
            'label': 'handwritten',
        }})
        found = provider_inventory.harness_usd_candidates(self._home(), os.environ)
        ids = {item['provider_id'] for item in found}
        self.assertIn('claude', ids)
        self.assertNotIn('codex', ids)
        accounts = usage_core.discover_metered_usd_accounts()
        self.assertIn('claude', accounts)
        self.assertIn('handwritten', accounts)
        self.assertNotIn('codex', accounts)

    def test_spend_promotes_billing_and_shows_budget_when_present(self):
        self._clear_override()
        self._write_claude('https://proxy.example.test/v1')
        row, calls = self._fetch(200, {'info': {'spend': 2.65, 'max_budget': 10}})
        self.assertEqual([call[0] for call in calls], ['GET'])
        self.assertTrue(calls[0][1].endswith('/key/info'))
        self.assertIsNone(calls[0][2])
        self.assertEqual(row.summary, '已用 $2.65 / $10')
        self.assertEqual(row.billing, 'metered')
        self.assertEqual(row.usage_details['spent_usd'], 2.65)
        self.assertEqual(row.usage_details['budget_usd'], 10)
        cached = usage_core.load_usage_cache()['providers']['claude']
        discovered = provider_inventory.discover()['accounts']['claude']
        self.assertEqual(cached.get('credential_key'), discovered['credential_key'])
        self.assertEqual(discovered['billing'], 'metered')
        self.assertEqual(discovered['backend_kind'], 'metered_usd')
        self.assertEqual(discovered['budget_usd'], 10.0)
        fleet = account_fleet.load_accounts(account_fleet.reference_fleet_path())
        spec = provider_inventory.apply(fleet, provider_inventory.discover())['accounts']['claude']
        self.assertEqual(spec['plan'], 'metered USD')
        self.assertEqual(spec['windows'], {'balance': {}})
        self.assertEqual(spec['capacity_usd']['balance'], 10.0)
        self._assert_secret_absent(self.SECRET)
        plain, _calls = self._fetch(200, {'info': {'spend': 12.3}})
        self.assertEqual(plain.summary, '已用 $12.30')
        self.assertIsNone(plain.usage_details['budget_usd'])
        uncapped = provider_inventory.discover()['accounts']['claude']
        self.assertEqual(uncapped['billing'], 'metered')
        self.assertNotIn('budget_usd', uncapped)
        uncapped_spec = provider_inventory.apply(fleet, provider_inventory.discover())['accounts']['claude']
        self.assertEqual(uncapped_spec['windows'], {})
        self.assertNotIn('balance', uncapped_spec.get('capacity_usd') or {})

    def test_codex_harness_is_read_with_one_get(self):
        self._clear_override()
        self._write_codex('https://codex-proxy.example.test/v1')
        row, calls = self._fetch(200, {'info': {'spend': 4}}, provider='codex')
        self.assertEqual([call[0] for call in calls], ['GET'])
        self.assertEqual(calls[0][1], 'https://codex-proxy.example.test/key/info')
        self.assertEqual(row.summary, '已用 $4')
        self.assertEqual(provider_inventory.discover()['accounts']['codex']['billing'], 'metered')
        self._assert_secret_absent(self.CODEX_SECRET)

    def test_handwritten_account_wins_by_id(self):
        self._write_claude('https://proxy.example.test/v1')
        self._override({'claude': {
            'billing': 'metered',
            'endpoint': 'https://handwritten.example.test/v1',
            'api_key': 'fixture-handwritten-key',
            'label': 'handwritten',
        }})
        found = usage_core.discover_metered_usd_accounts()
        self.assertEqual(set(found), {'claude'})
        self.assertEqual(found['claude'].source, 'override')
        self.assertEqual(found['claude'].origin, 'https://handwritten.example.test')
        self._assert_secret_absent(self.SECRET)

    def test_same_origin_and_key_keeps_the_handwritten_account(self):
        self._write_claude('https://proxy.example.test/v1', secret='fixture-handwritten-key')
        self._override({'handwritten': {
            'billing': 'metered',
            'endpoint': 'https://proxy.example.test/v1',
            'api_key': 'fixture-handwritten-key',
            'label': 'handwritten',
        }})
        found = usage_core.discover_metered_usd_accounts()
        self.assertEqual(set(found), {'handwritten'})
        self.assertNotIn('claude', found)
        self.assertEqual(found['handwritten'].source, 'override')

    def test_same_origin_different_key_shows_its_own_spend(self):
        self._clear_provider_cache()
        self._clear_provider_cache('handwritten')
        self._write_claude('https://proxy.example.test/v1')
        self._override({'handwritten': {
            'billing': 'metered',
            'endpoint': 'https://proxy.example.test/v1',
            'api_key': 'fixture-handwritten-key',
            'label': 'handwritten',
        }})
        found = usage_core.discover_metered_usd_accounts()
        self.assertEqual(set(found), {'claude', 'handwritten'})
        self.assertNotEqual(found['claude'].api_key, found['handwritten'].api_key)

        def fake(url, method='GET', headers=None, body=None, timeout=20):
            auth = (headers or {}).get('Authorization') or ''
            if auth.endswith('fixture-handwritten-key'):
                return 200, {'info': {'spend': 3}}, {}
            if auth.endswith(self.SECRET):
                return 200, {'info': {'spend': 9}}, {}
            return 500, {}, {}

        with patch.object(usage_core, 'http_json_response', side_effect=fake):
            handwritten = usage_core.fetch_metered_usd_usage('handwritten')
            claude = usage_core.fetch_metered_usd_usage('claude')
        self.assertEqual(handwritten.usage_details['spent_usd'], 3)
        self.assertEqual(claude.usage_details['spent_usd'], 9)
        cached = usage_core.load_usage_cache()['providers']
        self.assertEqual(cached['handwritten']['usage_details']['spent_usd'], 3)
        self.assertEqual(cached['claude']['usage_details']['spent_usd'], 9)
        self.assertNotEqual(
            cached['handwritten']['usage_details']['spent_usd'],
            cached['claude']['usage_details']['spent_usd'],
        )
        stored = json.dumps(provider_inventory.discover(), ensure_ascii=False) + json.dumps(cached, ensure_ascii=False)
        self.assertNotIn(self.SECRET, stored)
        self.assertNotIn('fixture-handwritten-key', stored)

    def test_official_hosts_are_not_probed(self):
        self._clear_override()
        self._write_claude('https://api.anthropic.com')
        self.assertNotIn('claude', usage_core.discover_metered_usd_accounts())
        with patch.object(usage_core, 'http_json_response', side_effect=AssertionError('probed')) as probe, \
                patch.object(usage_core, 'fetch_claude_usage', return_value=usage_core.ProviderUsage('claude', 'claude')):
            usage_core.collect_local_usage(['claude'])
        probe.assert_not_called()
        claude_settings = self._home() / '.claude' / 'settings.json'
        claude_settings.unlink()
        self._write_codex('https://api.openai.com')
        self.assertNotIn('codex', usage_core.discover_metered_usd_accounts())
        with patch.object(usage_core, 'http_json_response', side_effect=AssertionError('probed')) as probe, \
                patch.object(usage_core, 'fetch_codex_usage', return_value=usage_core.ProviderUsage('codex', 'codex')):
            usage_core.collect_local_usage(['codex'])
        probe.assert_not_called()

    def test_magpie_gateway_is_not_a_candidate(self):
        self._clear_override()
        self._write_claude('https://gateway.example.test/v1')
        home = self._home() / '.claude'
        (home / 'usage-float.json').write_text(json.dumps({
            'source': 'magpie', 'account': 'private@example.test',
        }), encoding='utf-8')
        self.assertNotIn('claude', usage_core.discover_metered_usd_accounts())
        row = provider_inventory.discover()['accounts']['claude']
        self.assertEqual(row['billing'], 'subscription')
        self.assertNotIn('usd_candidate', row)


if __name__ == '__main__':
    unittest.main()
