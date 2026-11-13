"""Window scope is data: a new scoped allowance works with no code change, an unscoped one binds every model."""
from datetime import datetime, timedelta, timezone
import copy
import unittest

import account_fleet
import quota_pools
import usage_reading
import window_scope

NOW = datetime(2026, 11, 4, 4, 0, tzinfo=timezone.utc)


def reading(wid, used, hours=3, **extra):
    return usage_reading.window_state({'id': wid, 'used_pct': used,
                                     'resets_at': (NOW + timedelta(hours=hours)).isoformat(), **extra}, NOW)


def usage(account, *windows):
    return {'accounts': {account: {'status': 'fresh', 'fetched_at': NOW.isoformat(), 'available': True,
                                   'windows': list(windows)}}}


class WindowScopeTests(unittest.TestCase):
    def test_tags_match_whole_tokens_only(self):
        self.assertTrue(window_scope.binds('claude-fable-5-1', {'models': ['fable']}))
        self.assertTrue(window_scope.binds('claude-fable-5-1', {'models': ['Claude Fable 5.1']}))
        # Claude's usage API scopes the weekly limit to the family ("Fable"), so it binds every Fable release.
        self.assertTrue(window_scope.binds('claude-fable-5-2', {'models': ['Fable']}))
        self.assertFalse(window_scope.binds('claude-fable-5-2', {'models': ['Claude Fable 5.1']}))
        self.assertFalse(window_scope.binds('claude-opus-5-5', {'models': ['fable']}))
        self.assertFalse(window_scope.binds('gemini-4-argon-high', {'models': ['gem']}))
        self.assertTrue(window_scope.binds('claude-opus-5-5-low', {'models_except': ['gemini']}))
        self.assertFalse(window_scope.binds('gemini-3.8-flash-high', {'models_except': ['gemini']}))

    def test_unscoped_window_and_unknown_model_bind(self):
        self.assertTrue(window_scope.binds('claude-opus-5-5', {}, None, {'hours': 5}))
        self.assertTrue(window_scope.binds('', {'models': ['fable']}))
        # Any source that declares a scope and binds is enough; sources without a scope do not veto.
        self.assertTrue(window_scope.binds('claude-fable-5-1', {'models': ['opus']}, {'models': ['fable']}))
        self.assertTrue(window_scope.binds('claude-fable-5-1', {}, {'models': ['fable']}))
        self.assertFalse(window_scope.binds('claude-opus-5-5', {}, {'models': ['fable']}))

    def test_a_new_scoped_week_works_by_configuration_alone(self):
        accounts = {'accounts': {'acme': {'windows': {
            'five_hour': {'hours': 5}, 'seven_day': {'hours': 168},
            'weekly_quartz': {'hours': 168, 'models': ['quartz']}}, 'bundles': {
            'quartz_high': {'model': 'acme-quartz-2', 'effort': 'high'},
            'onyx_high': {'model': 'acme-onyx-2', 'effort': 'high'}}}}, 'scoring': {}}
        observed = usage('acme', reading('weekly_quartz', 100, 40), reading('seven_day', 20, 40))
        for bid, blocked in (('quartz_high', True), ('onyx_high', False)):
            bundle = dict(accounts['accounts']['acme']['bundles'][bid], account='acme', id=bid)
            with self.subTest(bundle=bid):
                self.assertEqual(bool(quota_pools.launch_blockers(bundle, accounts, observed, NOW)), blocked)
                self.assertEqual('weekly_quartz' in quota_pools.bundle_pools(bundle, accounts), blocked)

    def test_a_new_scoped_week_works_from_the_provider_reading_alone(self):
        accounts = {'accounts': {'acme': {'windows': {'seven_day': {'hours': 168}}}}, 'scoring': {}}
        observed = usage('acme', reading('weekly_quartz', 100, 40, models=['Quartz']), reading('seven_day', 20, 40))
        for model, blocked in (('acme-quartz-2', True), ('acme-onyx-2', False)):
            with self.subTest(model=model):
                self.assertEqual(bool(quota_pools.launch_blockers({'account': 'acme', 'model': model}, accounts,
                                                                observed, NOW)), blocked)
                row = observed['accounts']['acme']
                self.assertEqual([w['id'] for w in usage_reading.applicable_windows(row, model)],
                                 ['weekly_quartz', 'seven_day'] if blocked else ['seven_day'])

    def test_an_unscoped_observed_window_binds_every_model(self):
        accounts = {'accounts': {'acme': {'windows': {'seven_day': {'hours': 168}}}}, 'scoring': {}}
        observed = usage('acme', reading('mystery', 100, 40), reading('seven_day', 20, 40))
        self.assertTrue(quota_pools.launch_blockers({'account': 'acme', 'model': 'acme-onyx-2'}, accounts, observed, NOW))

    def test_shipped_references_scope_antigravity_pools_without_code(self):
        accounts = account_fleet.load_accounts(account_fleet.REFERENCES / 'accounts.json')
        agy = copy.deepcopy(accounts['accounts']['antigravity'])
        flat = {bid: dict(b, account='antigravity', id=bid) for bid, b in agy['bundles'].items()}
        self.assertEqual(quota_pools.bundle_pools(flat['argon_high'], accounts), ['gemini-5h', 'gemini-weekly'])
        # Without the bundles' explicit draws, the windows' declared scope alone puts a Claude model on the 3p pool.
        for bid, bundle in flat.items():
            probe = {'account': 'antigravity', 'model': bundle['model']}
            with self.subTest(bundle=bid):
                self.assertEqual(quota_pools.bundle_pools(probe, accounts),
                                 bundle.get('draws') or ['gemini-5h', 'gemini-weekly'])


if __name__ == '__main__':
    unittest.main()
