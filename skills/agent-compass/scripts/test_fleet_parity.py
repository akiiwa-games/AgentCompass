"""A clean install routes like a calibrated fleet: shipped template + discovery, no accounts.local.json.

The expected values are the ones a hand-written machine fleet used to set (2026-11-04). Only the
shipped references and a synthetic discovery snapshot are read; the user home is never touched.
"""
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch

import accounts_override
import allocator
import account_fleet
import axis_scores
import provider_inventory as inventory

from router_test_support import isolated_router_state, temporary_root


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup, discovery=False, policy=False)


def shipped_template():
    return accounts_override.normalize(json.loads((account_fleet.REFERENCES / 'accounts.json').read_text(encoding='utf-8-sig')))


def snapshot():
    """Claude Max 20x, a second Claude Pro login, Codex Pro, Antigravity with a quota reading but no
    reported tier, a Pro ChatGPT web bridge, OpenCode free, Dot, Grok and the Grok Bot."""
    rows = {
        'claude': {'family': 'claude', 'status': 'ready', 'plan': 'max20', 'billing': 'subscription',
                   'sources': ['native'], 'identity': 'id-claude', 'quota_account': 'claude'},
        'claude-2': {'family': 'claude', 'status': 'ready', 'plan': 'pro', 'billing': 'subscription',
                     'sources': ['native', 'paseo'], 'identity': 'id-claude-2', 'quota_account': 'claude-2'},
        'codex': {'family': 'codex', 'status': 'ready', 'plan': 'pro', 'billing': 'subscription',
                  'sources': ['native'], 'identity': 'id-codex', 'quota_account': 'codex'},
        'antigravity': {'family': 'antigravity', 'provider': 'antigravity-resilient', 'status': 'ready',
                        'plan': None, 'billing': 'subscription', 'sources': ['native', 'usage-cache']},
        'opencode': {'family': 'opencode', 'status': 'ready', 'plan': 'free', 'billing': 'subscription', 'sources': ['native']},
        'grok': {'family': 'grok', 'status': 'ready', 'plan': 'subscription', 'billing': 'subscription',
                 'sources': ['native', 'usage-cache'], 'usage_windows': ['credits']},
        'dot': {'family': 'dot', 'status': 'ready', 'sources': ['native']},
        'chatgpt-web': {'family': 'chatgpt-web', 'status': 'ready', 'plan': 'pro', 'sources': ['native']},
        'grok-bot': {'family': 'grok-bot', 'status': 'ready', 'billing': 'unknown', 'sources': ['native']},
    }
    launchers = {name: {'family': row['family'], 'status': 'ready'} for name, row in rows.items()}
    launchers['antigravity-resilient'] = {'family': 'antigravity-resilient', 'status': 'ready'}
    return {'accounts': rows, 'launchers': launchers}


def priced(accounts):
    """The accounts as the router prices them: capacity from the community data by plan."""
    return axis_scores.five_axis_overlay(accounts, axis_scores.load_five_axis())['accounts']


def community(family, plan, window):
    return axis_scores.load_five_axis()['capacity'][family][plan][window]['capacity_usd']


class CleanInstallParityTests(unittest.TestCase):
    def setUp(self):
        self.accounts = inventory.apply(shipped_template(), snapshot())
        self.specs = self.accounts['accounts']

    def test_subscription_capacity_comes_from_the_discovered_plan(self):
        claude, codex = self.specs['claude'], self.specs['codex']
        priced_specs = priced(self.accounts)
        self.assertEqual(priced_specs['claude']['capacity_usd'],
                         {w: community('claude', 'max20', w) for w in ('five_hour', 'seven_day', 'weekly_fable')})
        self.assertEqual(priced_specs['codex']['capacity_usd'], {'usage': community('codex', 'pro', 'usage')})
        self.assertTrue(claude['verified'] and codex['verified'])

    def test_antigravity_without_a_reported_tier_enters_mode_b_with_reason(self):
        agy = self.specs['antigravity']
        self.assertEqual(agy['plan'], 'unknown')
        self.assertNotIn('plan_priced_as', agy)
        self.assertIn('loadCodeAssist', agy['plan_unknown'])
        self.assertEqual(set(agy['windows']), {'gemini-weekly', 'gemini-5h', '3p-weekly', '3p-5h'})
        self.assertTrue(agy['verified'])  # a quota reading is the sign-in proof
        over = priced(self.accounts)['antigravity']
        self.assertEqual(over['capacity_usd'], {})
        self.assertTrue(over['capacity_unknown'])
        self.assertIn('loadCodeAssist', over['plan_unknown_reason'])
        now = datetime(2026, 11, 11, tzinfo=timezone.utc)
        task_doc = {
            'objective': 'Draft module notes', 'hours': 0.5,
            'profile': {'work_kind': 'general_implementation', 'reasoning_depth': 1, 'code_complexity': 1,
                        'needs_visual': False, 'needs_gui': False, 'local_only': False, 'wants_fast': False,
                        'read_only_review': False, 'access': 'write', 'expected_hours': 0.5,
                        'hours_confidence': 'medium', 'urgency': 'normal', 'interaction_style': 'engineering',
                        'multi_task': False, 'task_count': '1',
                        'axis_demand': {'divergence': 0, 'coding': 1, 'aesthetics': 0, 'tool_operation': 0}},
        }
        decision = allocator.plan(task_doc, {'accounts': {}}, now, accounts=self.accounts, overrides=[],
                                  host_policy_config=None, read_local_state=False)
        agy_rows = [r for r in decision['ranking'] if r['bundle']['account'] == 'antigravity']
        self.assertTrue(agy_rows)
        for row in agy_rows:
            self.assertTrue(any('capacity_unknown' in ex and 'plan unknown' in ex for ex in row['excluded']), row['excluded'])
        # Explicit --model / bundle pin still allows running with a capacity_unknown note.
        pinned_bundle = agy_rows[0]['id']
        named = allocator.plan({**task_doc, 'constraints': {'bundle': pinned_bundle}}, {'accounts': {}}, now,
                               accounts=self.accounts, overrides=[], host_policy_config=None, read_local_state=False)
        self.assertEqual(named['choice'], pinned_bundle)

    def test_an_exact_antigravity_tier_resolves_its_plan_capacity(self):
        for raw_tier, expected_plan in (
                ('g1-pro-tier', 'pro'),
                ('g1-ultra-lite-tier', 'ultra5'),
                ('g1-ultra-tier', 'ultra20'),
                ('Google AI Ultra (5x)', 'ultra5'),
                ('Google AI Ultra (20x)', 'ultra20')):
            with self.subTest(raw_tier=raw_tier):
                snap = snapshot()
                snap['accounts']['antigravity']['plan'] = raw_tier
                applied = inventory.apply(shipped_template(), snap)
                over = priced(applied)['antigravity']
                self.assertEqual(over['capacity_usd']['gemini-weekly'], community('antigravity', expected_plan, 'gemini-weekly'))
                self.assertFalse(over.get('capacity_unknown'))
                self.assertNotIn('plan_priced_as', applied['accounts']['antigravity'])
                self.assertNotIn('plan_unknown', applied['accounts']['antigravity'])

    def test_intrinsic_unmetered_accounts_and_the_pro_web_cadence(self):
        for name in ('opencode', 'dot'):
            self.assertTrue(self.specs[name]['unmetered'] and self.specs[name]['unlimited'], name)
            self.assertTrue(self.specs[name]['verified'], name)
        web = self.specs['chatgpt-web']
        self.assertEqual(web['cadence'], {'window_seconds': 43200, 'limit': 5})
        self.assertTrue(web['unmetered'])

    def test_second_account_comes_from_discovery(self):
        second = self.specs['claude-2']
        self.assertEqual(priced(self.accounts)['claude-2']['capacity_usd'],
                         {w: community('claude', 'pro', w) for w in ('five_hour', 'seven_day')})
        self.assertTrue(second['verified'])
        self.assertEqual(self.accounts['dispatch_efforts']['claude-2'], self.accounts['dispatch_efforts']['claude'])
        self.assertTrue(second['bundles'])
        self.assertTrue(all(b['provider'] == 'claude-2' for b in second['bundles'].values()))

    def test_grok_bot_draws_on_the_grok_meter_and_mode_b_excludes_unmeasured_demands(self):
        inventory.apply_shared_quota(self.accounts)
        self.assertEqual(self.specs['grok-bot']['quota_source'], 'grok')
        self.assertIsNone(accounts_override.manual_only_billing('grok-bot', self.accounts))
        now = datetime(2026, 11, 11, tzinfo=timezone.utc)
        task_doc = {
            'objective': 'Interactive multi-step tool loop', 'hours': 0.5,
            'profile': {'work_kind': 'tool_execution', 'reasoning_depth': 1, 'code_complexity': 0,
                        'needs_visual': False, 'needs_gui': False, 'local_only': False, 'wants_fast': False,
                        'read_only_review': False, 'access': 'read-only', 'expected_hours': 0.5,
                        'hours_confidence': 'medium', 'urgency': 'normal', 'interaction_style': 'exploration',
                        'multi_task': False, 'task_count': '1',
                        'axis_demand': {'divergence': 0, 'coding': 0, 'aesthetics': 0, 'tool_operation': 2}},
        }
        # Without manual scores, unmeasured tool_operation.responsiveness enters Mode B with actionable CLI hint:
        with unittest.mock.patch.object(axis_scores, 'load_public_user_scores',
                                        return_value={'schema_version': 1, 'scores': {}}):
            unscored = allocator.plan(task_doc, {'accounts': {}}, now, accounts=self.accounts, overrides=[],
                                      host_policy_config=None, read_local_state=False)
        for row in [r for r in unscored['ranking'] if r['bundle']['account'] == 'grok-bot']:
            self.assertTrue(any('tool_operation.responsiveness' in ex and 'agentcompass score set ' in ex
                                for ex in row['excluded']), row['excluded'])
        # With shipped maintainer scores in references/user-scores.json, responsiveness is filled while grok meter remains Mode B:
        decision = allocator.plan(task_doc, {'accounts': {}}, now, accounts=self.accounts, overrides=[],
                                  host_policy_config=None, read_local_state=False)
        grok_bot_rows = [r for r in decision['ranking'] if r['bundle']['account'] == 'grok-bot']
        self.assertTrue(grok_bot_rows)
        for row in grok_bot_rows:
            self.assertFalse(any('tool_operation.responsiveness' in ex for ex in row['excluded']), row['excluded'])
            self.assertTrue(any('capacity_unknown' in ex for ex in row['excluded']), row['excluded'])

    def test_scoring_and_route_policy_are_the_public_defaults(self):
        scoring = self.accounts['scoring']
        self.assertEqual(scoring['harness_order'], ['codex', 'pi'])
        self.assertEqual(scoring['profile']['harness_by_style'], {'engineering': ['codex', 'pi'], 'exploration': ['pi', 'codex']})
        policy = account_fleet.load_capabilities(accounts=self.accounts)
        self.assertEqual(policy['remote_reroute_guard'], {'enabled': True, 'bundle_id': 'dot_text', 'window_seconds': 600})
        self.assertEqual([t['label'] for t in self.accounts['priority_targets']], ['Astra', 'Argon', 'Opus 5.5'])

    def test_a_sparse_scoring_override_keeps_the_defaults(self):
        merged = accounts_override.merge(self.accounts['scoring'], {'safety_margin_pct': 7})
        self.assertEqual((merged['safety_margin_pct'], merged['harness_order']), (7, ['codex', 'pi']))

    def test_discovered_models_default_from_public_data_alone(self):
        snap = snapshot()
        advertised = {'claude': ['claude-opus-5-5', 'claude-opus-4-8', 'claude-haiku-4-5', 'claude-haiku-5-5', 'claude-opus-9-1'],
                      'codex': ['gpt-6.1-sol', 'gpt-6.1-terra', 'gpt-6-luna', 'gpt-6-sol', 'gpt-5.6-terra'],
                      'grok': ['grok-4.7', 'grok-4.7-build-fast', 'grok-4.6']}
        for name, models in advertised.items():
            snap['accounts'][name]['models'] = models
        accounts = inventory.apply(shipped_template(), snap)
        superseded = shipped_template()['superseded']
        auto = {(b['provider'], b['launch_model']): b['disabled'] for spec in accounts['accounts'].values()
                for b in spec['bundles'].values() if b.get('auto')}
        self.assertTrue(auto)
        for (provider, model), disabled in auto.items():
            with self.subTest(provider=provider, model=model):
                self.assertEqual(disabled, model in superseded)
        self.assertEqual({m for (_, m), off in auto.items() if off},
                         {'claude-opus-4-8', 'claude-haiku-4-5', 'gpt-6-sol', 'gpt-5.6-terra', 'grok-4.6'})
        self.assertFalse(auto['claude', 'claude-opus-9-1'])  # newer than the data: checked

    def test_template_stays_free_of_personal_choices(self):
        document = shipped_template()
        bundles = [b for spec in document['accounts'].values() for b in spec.get('bundles', {}).values()]
        self.assertFalse([b for b in bundles if b.get('disabled')])
        self.assertFalse([name for name, spec in document['accounts'].items() if spec.get('model_defaults')])
        self.assertFalse([name for name, spec in document['accounts'].items() if spec.get('verified')])


class DiscoveredTierTests(unittest.TestCase):
    def setUp(self):
        self.home = temporary_root(self.addCleanup)
        self.now = datetime(2026, 11, 4, tzinfo=timezone.utc)

    def write(self, path, value):
        target = self.home / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value), encoding='utf-8')

    def discover(self):
        with patch.object(inventory, '_which', lambda name, path=None: '/bin/' + name if name in ('agy', 'opencode') else None):
            return inventory.discover(self.home, {'PATH': ''}, self.now)['accounts']

    def test_antigravity_quota_reading_proves_sign_in_and_installed_cli_alone_does_not(self):
        self.assertNotIn('usage-cache', self.discover()['antigravity']['sources'])
        self.assertFalse(inventory.launch_verified(self.discover()['antigravity'], False))
        self.write('.usage-float/usage-cache.json', {'providers': {'antigravity': {'windows': [
            {'id': 'gemini-weekly', 'used_pct': 25}]}}})
        row = self.discover()['antigravity']
        self.assertIn('usage-cache', row['sources'])
        self.assertTrue(inventory.launch_verified(row, False))

    def test_opencode_free_needs_no_sign_in(self):
        row = self.discover()['opencode']
        self.assertTrue(inventory.launch_verified(row, False))
        self.assertEqual(inventory.account_state_label(row), 'ready')

    def test_ready_chatgpt_web_bridge_reports_a_pro_seat(self):
        self.write('.codex-chatgpt-web-consult/config.json', {'mode': 'browser-only', 'browserInteractionMode': 'automatic',
                                                             'host': '127.0.0.1', 'port': 8790, 'proAvailable': False})
        self.assertNotIn('plan', self.discover()['chatgpt-web'])
        self.write('.codex-chatgpt-web-consult/config.json', {'mode': 'browser-only', 'browserInteractionMode': 'automatic',
                                                             'host': '127.0.0.1', 'port': 8790, 'proAvailable': True})
        self.assertEqual(self.discover()['chatgpt-web']['plan'], 'pro')


if __name__ == '__main__':
    unittest.main()
