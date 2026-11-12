"""Metered USD is spent dollars and an optional budget. It does not reset."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import account_fleet
import allocator
import candidate_eval
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
        self.assertIn('re-toggle in the tray', issue)
        proxies = {'schema_version': 2, 'litellm_proxies': {'old': {}}, 'accounts': {}}
        path.write_text(json.dumps(proxies), encoding='utf-8', newline='\n')
        proxy_issue = state_upgrade.check_accounts_override(path)
        self.assertIn('litellm_proxies', proxy_issue)
        self.assertEqual(path.read_text(encoding='utf-8'), json.dumps(proxies))

        omp = {'schema_version': 2, 'accounts': {
            'omp': {'billing': 'metered', 'windows': {'week': {'hours': 168}}},
            'claude': {'billing': 'subscription', 'windows': {'seven_day': {'hours': 168}}},
        }}
        state_upgrade.validate_accounts_override_doc(omp, path)

        kind = {'schema_version': 2, 'accounts': {
            'old': {'backend_kind': 'litellm', 'endpoint': 'https://proxy.example.test', 'api_key': 'sk-team-proxy'},
        }}
        with self.assertRaises(ValueError) as raised:
            state_upgrade.validate_accounts_override_doc(kind, path)
        self.assertIn('backend_kind=litellm', str(raised.exception))


class SpendRead(unittest.TestCase):
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

    def test_one_declared_model_completion_then_one_flush_retry(self):
        self._account(['declared-model'])
        calls = []

        def fake(url, method='GET', headers=None, body=None, timeout=20):
            calls.append((method, url, (body or {}).get('model')))
            if url.endswith('/key/info'):
                return 403, {}, {}
            spend = '0.0000002' if len(calls) == 2 else '12.3'
            return 200, {}, {
                'x-litellm-key-spend': spend,
                'x-litellm-key-max-budget': '80',
                'x-litellm-response-cost': '0.0000002',
            }

        with patch.object(usage_core, 'http_json_response', side_effect=fake):
            row = usage_core.fetch_metered_usd_usage('usd')
        self.assertEqual([call[2] for call in calls if call[0] == 'POST'], ['declared-model', 'declared-model'])
        self.assertEqual(len(calls), 3)
        self.assertNotIn('gemini-embedding', json.dumps(calls))
        self.assertEqual(row.summary, '已用 $12.30 / $200')
        self.assertIsNone(row.windows[0].resets_at)

    def test_no_declared_model_does_not_guess_one(self):
        self._account([])
        calls = []

        def fake(url, method='GET', headers=None, body=None, timeout=20):
            calls.append(method)
            return 403, {}, {}

        with patch.object(usage_core, 'http_json_response', side_effect=fake):
            row = usage_core.fetch_metered_usd_usage('usd')
        self.assertEqual(calls, ['GET'])
        self.assertFalse(row.available)
        self.assertEqual(row.error, '更新失败')

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


if __name__ == '__main__':
    unittest.main()
