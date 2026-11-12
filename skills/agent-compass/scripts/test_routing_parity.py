"""The three-repository routing contract (R1-R4): model-name resolution, step-down range, user pins, profile validation."""
import argparse
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import allocator
import account_fleet
import candidate_eval
import launch_failures
import quota_pools
import task_intake
import auto_bundles
import dispatch_registry
import roll
import route_judge
from router_test_support import isolated_router_state, profile, usage_windows
import window_scope


NOW = datetime(2026, 10, 29, 3, 0, tzinfo=timezone.utc)
PROFILE = profile(code_complexity=1, local_only=True, reason='test profile')
LIVE = profile(code_complexity=1, local_only=True,
               axis_demand={'divergence': 0, 'coding': 1, 'aesthetics': 0, 'tool_operation': 2}, reason='test profile')
FULL_QUOTA = {
    'claude': [('five_hour', 90, 3), ('seven_day', 80, 100), ('weekly_fable', 100, 100)],
    'claude-lab': [('five_hour', 90, 3), ('seven_day', 80, 100), ('weekly_fable', 100, 100)],
    'codex': [('usage', 80, 100)],
    'antigravity': [('gemini-5h', 90, 3), ('gemini-weekly', 80, 100), ('3p-5h', 90, 3), ('3p-weekly', 80, 100)],
}


class RouterCase(unittest.TestCase):
    def setUp(self):
        self.root = isolated_router_state(self.addCleanup)
        self.accounts = account_fleet.load_accounts()
        self.usage = usage_windows(NOW, FULL_QUOTA)

    def task(self, profile=PROFILE, **extra):
        return {'objective': 'routing contract', 'profile': profile, 'expected_hours': 1, 'disable_fast': True,
                'include_unverified': True, **extra}

    def plan(self, task, accounts=None, usage=None, **kw):
        return allocator.plan(task, self.usage if usage is None else usage, NOW,
                              accounts=copy.deepcopy(accounts or self.accounts), overrides=[], **kw)

    def only(self, account, **rows):
        """The reference fleet with `rows` as the only bundles anywhere."""
        accounts = copy.deepcopy(self.accounts)
        for spec in accounts['accounts'].values():
            if isinstance(spec, dict):
                spec['bundles'] = {}
        accounts['accounts'][account]['bundles'] = rows
        return accounts

    @staticmethod
    def row(plan, bundle_id):
        return next(c for c in plan['ranking'] if c['id'] == bundle_id)


class ModelResolverTests(RouterCase):
    """R1: one resolver, a result with its source, and every table read through it."""

    def test_one_resolver_reports_where_a_name_came_from(self):
        cases = {
            'claude-opus-5-5': ('claude-opus-5-5', 'exact', None),
            'claude-opus-5-5:medium': ('claude-opus-5-5', 'exact', 'medium'),
            'anthropic/claude-opus-5-5': ('claude-opus-5-5', 'alias', None),
            'CLAUDE-OPUS-5-5': ('claude-opus-5-5', 'alias', None),
            'CLAUDE-SONNET-4-6': ('claude-sonnet-4-6', 'alias', None),
            'claude-opus-5-5-20261001': ('claude-opus-5-5', 'alias', None),
            'claude-opus-5-5@20261001': ('claude-opus-5-5', 'alias', None),
            'gpt-6.1-sol-latest': ('gpt-6.1-sol', 'alias', None),
            'gpt-6-astra-preview': ('gpt-6-astra', 'alias', None),
            'claude-sonnet-4-6[1m]': ('claude-sonnet-4-6', 'alias', None),
            'openrouter/anthropic/claude-opus-4-6:max': ('claude-opus-4-6', 'alias', 'max'),
            'claude-opus-5': ('claude-opus-5-5', 'alias', None),
            'qwen3-flash-2025-07-28': ('qwen3-flash-2025-07-28', 'exact', None),
            'gemini-2.5-pro': ('gemini-4-argon-high', 'family', None),
            'deepseek-r1': ('deepseek-v4-pro', 'family', None),
            'claude-opus-4-20250514': ('claude-opus-5-5', 'family', None),
            'gpt-6-experimental': ('gpt-6-astra', 'family', None),
            'totally-unknown-model': (None, 'unknown', None),
            'grok-bot': ('grok-bot', 'exact', None) if 'grok-bot' in self.accounts['models'] else (None, 'unknown', None),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                found = account_fleet.resolve_model(raw, self.accounts)
                self.assertEqual((found['key'], found['source'], found['effort']), expected)

    def test_a_vendor_tag_is_not_an_effort(self):
        found = account_fleet.resolve_model('qwen/Qwen3-Coder-Next:free', self.accounts)
        self.assertEqual((found['key'], found['effort']), ('qwen3-coder-next', None))

    def test_family_names_have_no_scores_of_their_own_and_leave_automatic_routing(self):
        """1.1: an unmeasured family name does not borrow the flagship's scores or invent a default speed: rule B."""
        for raw in ('gemini-2.5-pro', 'deepseek-r1', 'claude-opus-4-20250514'):
            with self.subTest(raw=raw):
                accounts = self.only('claude', x={'model': raw, 'effort': 'high', 'provider': 'claude'})
                row = self.plan(self.task(LIVE), accounts)['ranking'][0]
                self.assertEqual(row['bundle']['effort'], 'high')
                self.assertTrue(any('no trusted data for the demanded category' in r for r in row['excluded']), row['excluded'])

    def test_a_date_spelling_keeps_the_measured_scores(self):
        accounts = self.only('claude', x={'model': 'claude-opus-5-5-20261001', 'effort': 'high', 'provider': 'claude'})
        exact = self.only('claude', x={'model': 'claude-opus-5-5', 'effort': 'high', 'provider': 'claude'})
        dated, plain = self.plan(self.task(LIVE), accounts)['ranking'][0], self.plan(self.task(LIVE), exact)['ranking'][0]
        self.assertEqual(dated['components']['tool_operation'], plain['components']['tool_operation'])
        self.assertEqual(dated['components']['fit'], plain['components']['fit'])
        self.assertEqual(dated['bundle']['effort'], plain['bundle']['effort'])

    def test_one_model_under_two_spellings_is_no_rival_of_itself(self):
        """1.2: a second spelling counted as the best other model, so the margin collapsed and asked for a judge."""
        accounts = self.only('claude',
                             plain={'model': 'claude-opus-5-5', 'effort': 'high', 'provider': 'claude'},
                             spelled={'model': 'claude-opus-5', 'effort': 'high', 'provider': 'claude'})
        plan = self.plan(self.task(), accounts)
        self.assertEqual(len([c for c in plan['ranking'] if not c['excluded']]), 2)
        self.assertIsNone(plan['decision']['margin'])
        self.assertFalse(plan['decision']['needs_judgment'])

    def test_superseded_survives_any_spelling(self):
        """1.3: the data's old id must be found through a prefix, a date and case alike."""
        for raw in ('deepseek-v4-flash', 'opencode/deepseek-v4-flash', 'OpenCode/DeepSeek-V4-Flash', 'opencode/deepseek-v4-flash-20261001'):
            with self.subTest(raw=raw):
                self.assertEqual(auto_bundles.superseded_by(raw, self.accounts), 'deepseek-v4.1-flash-free')

    def test_quota_window_scope_matches_the_key_or_the_raw_name(self):
        self.assertTrue(window_scope.binds_any(('gemini-4-argon-high', 'gemini-2.5-pro'), {'models': ['2.5-pro']}))
        self.assertTrue(window_scope.binds_any(('gemini-4-argon-high', 'argon'), {'models': ['gemini']}))
        self.assertFalse(window_scope.binds_any(('gemini-4-argon-high', 'argon'), {'models_except': ['gemini']}))
        accounts = self.only('claude', x={'model': 'anthropic/claude-fable-5-1', 'effort': 'high', 'provider': 'claude'})
        self.assertIn('weekly_fable', quota_pools.bundle_pools(account_fleet.bundles(accounts)['x'], accounts))

    def test_a_cooldown_on_the_key_stops_every_spelling(self):
        entries = {'claude/claude-opus-5-5': {'until': '2099-01-01T00:00:00+00:00', 'failure_class': 'launch failed'}}
        found = candidate_eval.cooling_reason({'account': 'claude', 'model': 'claude-opus-5'}, entries, self.accounts, now=NOW)
        self.assertIn('cooling down', found)

    def test_worker_continuity_compares_the_key(self):
        same = lambda a, b: account_fleet.canonical(a, self.accounts) == account_fleet.canonical(b, self.accounts)
        row = {'provider': 'claude', 'account': 'claude', 'effort': 'high'}
        worker = {'bundle': {**row, 'model': 'claude-opus-5-5'}}
        self.assertTrue(roll._bundle_matches_worker({**row, 'model': 'claude-opus-5'}, worker, same))
        self.assertFalse(roll._bundle_matches_worker({**row, 'model': 'claude-sonnet-5-5'}, worker, same))


class StepDownRangeTests(RouterCase):
    """R2: dispatch_efforts(provider) intersected with what is offered, minus what the user unchecked."""

    def test_an_unchecked_effort_is_excluded_from_named_efforts(self):
        accounts = self.only('codex',
                             astra_medium={'model': 'gpt-6-astra', 'effort': 'medium', 'provider': 'codex', 'disabled': True},
                             astra_high={'model': 'gpt-6-astra', 'effort': 'high', 'provider': 'codex'})
        plan = self.plan(self.task(LIVE, constraints={'model': 'gpt-6-astra', 'effort': 'medium'}), accounts)
        self.assertEqual(plan['choice'], 'none')

    def test_a_row_listing_its_efforts_offers_only_them_when_unlisted_in_table(self):
        accounts = self.only('codex', astra={'model': 'gpt-6-astra', 'effort': 'high', 'provider': 'codex', 'efforts': ['high']})
        accounts['dispatch_efforts']['codex'].pop('gpt-6-astra', None)
        self.assertEqual(account_fleet.dispatch_allowed_efforts(accounts['accounts']['codex']['bundles']['astra'], accounts), ('high',))
        plan = self.plan(self.task(LIVE, constraints={'model': 'gpt-6-astra', 'effort': 'medium'}), accounts)
        self.assertEqual(plan['choice'], 'none')

    def test_a_second_account_reads_its_family_dispatch_table(self):
        """2.2: claude-2 and opencode-2 read their family's dispatch_efforts table."""
        cases = (('claude-haiku-5-5', 'claude', 'claude-2'), ('claude-haiku-5-5', 'claude', 'claude_2'),
                 ('deepseek-v4.1-flash-free', 'opencode', 'opencode-2'))
        for model, account, provider in cases:
            with self.subTest(provider=provider):
                main = self.only(account, x={'model': model, 'effort': 'high', 'provider': account})
                second = self.only(account, x={'model': model, 'effort': 'high', 'provider': provider})
                self.assertEqual(self.row(self.plan(self.task(LIVE), second), 'x')['bundle']['effort'],
                                 self.row(self.plan(self.task(LIVE), main), 'x')['bundle']['effort'])
                self.assertEqual(self.row(self.plan(self.task(LIVE), second), 'x')['bundle']['effort'], 'high')

    def test_discovery_family_names_the_table(self):
        accounts = self.only('claude', x={'model': 'claude-haiku-5-5', 'effort': 'high', 'provider': 'work'})
        accounts['_discovery'] = {'launchers': {'work': {'family': 'claude', 'status': 'ready'}}, 'accounts': {}}
        self.assertEqual(account_fleet.dispatch_table('work', accounts), accounts['dispatch_efforts']['claude'])
        self.assertEqual(account_fleet.dispatch_allowed_efforts({'provider': 'work', 'model': 'claude-haiku-5-5'}, accounts), ('high',))

    def test_pi_has_its_own_table(self):
        self.assertIn('pi', self.accounts['dispatch_efforts'])
        row = {'model': 'gpt-6-astra', 'effort': 'high', 'provider': 'pi'}
        self.assertEqual(account_fleet.dispatch_allowed_efforts(row, self.accounts),
                         tuple(self.accounts['dispatch_efforts']['pi']['gpt-6-astra']))
        unlisted = self.only('codex', x={'model': 'deepseek-v4.1-flash-free', 'effort': 'high', 'provider': 'pi'})
        self.assertEqual(self.row(self.plan(self.task(LIVE), unlisted), 'x')['bundle']['effort'], 'high')

    def test_a_table_that_does_not_list_the_model_offers_no_unlisted_effort(self):
        accounts = self.only('codex', x={'model': 'gpt-6-astra', 'effort': 'high', 'provider': 'codex'})
        del accounts['dispatch_efforts']['codex']['gpt-6-astra']
        self.assertEqual(account_fleet.dispatch_allowed_efforts(accounts['accounts']['codex']['bundles']['x'], accounts), ())
        self.assertEqual(self.plan(self.task(LIVE, constraints={'model': 'gpt-6-astra', 'effort': 'medium'}), accounts)['choice'], 'none')

    def test_model_colon_effort_is_a_fixed_level(self):
        accounts = self.only('codex', x={'model': 'gpt-6-astra:high', 'provider': 'codex'})
        row = self.row(self.plan(self.task(LIVE), accounts), 'x')
        self.assertEqual((row['bundle']['model'], row['bundle']['effort']), ('gpt-6-astra', 'high'))

    def test_antigravity_does_not_step_down(self):
        accounts = self.only('antigravity', x={'model': 'claude-opus-5-5', 'effort': 'high', 'provider': 'antigravity-resilient'})
        self.assertEqual(self.row(self.plan(self.task(LIVE), accounts), 'x')['bundle']['effort'], 'high')


class UserPinTests(RouterCase):
    """R3: what the user named holds in every mode and through every hop."""

    def test_model_and_effort_hold_outside_realtime(self):
        """3.1: the plan chose opus High although the user named Medium."""
        for effort in ('low', 'medium', 'high', 'xhigh'):
            with self.subTest(effort=effort):
                plan = self.plan(self.task(constraints={'model': 'claude-opus-5-5', 'effort': effort}))
                bundle = plan['selected_bundle']
                # The effort is the user's; Antigravity carries it in the model id (claude-opus-5-5-low), a Claude account in the effort field.
                self.assertIn(bundle['model'], ('claude-opus-5-5', f'claude-opus-5-5-{effort}'))
                self.assertEqual(bundle['effort'], effort)

    def test_colon_effort_is_an_effort_not_a_vendor_tag(self):
        """3.2: `:medium` was stripped as a vendor tag and the effort was lost."""
        norm = task_intake.normalize_task({'objective': 'x', 'profile': PROFILE, 'expected_hours': 1,
                                         'constraints': {'model': 'claude-opus-5-5:medium'}},
                                        self.accounts['scoring'], account_fleet.load_capabilities(accounts=self.accounts), self.accounts)
        self.assertEqual((norm['constraint_model'], norm['constraint_effort']), ('claude-opus-5-5', 'medium'))
        plan = self.plan(self.task(constraints={'model': 'claude-opus-5-5:medium'}))
        self.assertEqual(plan['selected_bundle']['effort'], 'medium')

    def test_pinned_fields_in_the_request_body_count(self):
        plan = self.plan(self.task(pinned_model='claude-opus-5-5', pinned_effort='medium'))
        bundle = plan['selected_bundle']
        self.assertIn(bundle['model'], ('claude-opus-5-5', 'claude-opus-5-5-medium'))
        self.assertEqual(bundle['effort'], 'medium')

    def test_a_named_effort_with_no_row_is_carried_with_a_distinct_id(self):
        plan = self.plan(self.task(constraints={'model': 'gpt-6-astra', 'effort': 'medium'}))
        selected = plan['selected_bundle']
        self.assertEqual((selected['model'], selected['effort'], selected['bundle_id']),
                         ('gpt-6-astra', 'medium', f"{selected['pin_carrier']}@medium"))
        self.assertNotEqual(selected['bundle_id'], selected['pin_carrier'])

    def test_a_named_effort_with_no_row_reaches_prepare(self):
        task = self.task(constraints={'model': 'gpt-6-astra', 'effort': 'medium'}, parent_agent_id='p', task_id='t')
        plan = self.plan(task)
        with tempfile.TemporaryDirectory() as directory, patch.object(account_fleet, 'load_accounts', return_value=copy.deepcopy(self.accounts)):
            db = os.path.join(directory, 'reg.sqlite')
            issued = dispatch_registry.issue(plan, task, now=NOW, path=db, allocation_task=task)
            prepared = dispatch_registry.prepare(issued['decision_id'], task, {'title': 'x', 'initialPrompt': 'go'},
                                                 self.usage, accounts=copy.deepcopy(self.accounts), now=NOW, path=db)
        self.assertEqual((prepared['bundle']['effort'], prepared['bundle']['model']), ('medium', 'gpt-6-astra'))

    def test_a_named_effort_outside_the_contract_is_blocked_with_the_reason(self):
        plan = self.plan(self.task(constraints={'model': 'claude-haiku-5-5', 'effort': 'medium'}))
        self.assertEqual(plan['choice'], 'none')
        self.assertEqual(plan['decision']['blocked'], 'named_model_unavailable')
        self.assertTrue(plan['decision']['warnings'])

    def test_a_named_model_alone_scores_its_written_rows_by_tool_operation(self):
        plan = self.plan(self.task(LIVE, constraints={'model': 'gpt-6-astra'}))
        high, xhigh = self.row(plan, 'astra_high'), self.row(plan, 'astra_xhigh')
        self.assertGreater(high['components']['fit'], xhigh['components']['fit'])
        self.assertEqual(plan['selected_bundle']['effort'], 'high')

    def test_model_and_effort_named_keep_their_level(self):
        plan = self.plan(self.task(LIVE, constraints={'model': 'gpt-6-astra', 'effort': 'high'}))
        self.assertEqual(plan['selected_bundle']['effort'], 'high')

    def test_a_family_name_never_launches_the_flagship(self):
        plan = self.plan(self.task(constraints={'model': 'gemini-2.5-pro'}))
        self.assertEqual(plan['choice'], 'none')
        self.assertIn('gemini-2.5-pro', plan['decision']['reason'])

    def test_named_model_blocked_by_quota_or_cooldown_never_falls_back(self):
        exhausted = usage_windows(NOW, {**FULL_QUOTA, 'codex': [('usage', 0, 100)]})
        plan = self.plan(self.task(constraints={'model': 'gpt-6-astra'}), usage=exhausted)
        self.assertEqual(plan['choice'], 'none')
        self.assertEqual(plan['decision']['blocked'], 'named_model_unavailable')
        self.assertTrue(any('usage' in w for w in plan['decision']['warnings']), plan['decision']['warnings'])
        self.assertIn('do not substitute', plan['next'])

        with patch.object(launch_failures, 'active', return_value={'x': 1}), \
                patch.object(launch_failures, 'reason', side_effect=lambda b, e, now=None: 'cooling down' if b.get('account') == 'codex' else None):
            cooled = self.plan(self.task(constraints={'model': 'gpt-6-astra'}))
        self.assertEqual(cooled['choice'], 'none')
        self.assertTrue(any('cooling down' in w for w in cooled['decision']['warnings']))

    def test_a_new_pin_breaks_the_hold_of_a_coupled_chain(self):
        chain = {'id': 'c', 'coupling': 'high', 'worker': {'bundle': 'opus55_high', 'compacted_tokens': 0, 'compactions': 0}}
        plan = self.plan(self.task(chain=chain, constraints={'model': 'gpt-6-astra'}))
        self.assertEqual(plan['selected_bundle']['model'], 'gpt-6-astra')
        self.assertFalse(plan['decision']['needs_user_decision'])
        self.assertIsNone(plan['decision'].get('drift_risk'))
        kept = self.plan(self.task(chain=chain, constraints={'model': 'claude-opus-5-5', 'effort': 'high'}))
        self.assertEqual(kept['choice'], 'opus55_high')

    def test_off_by_default_stays_out_of_routing_until_named(self):
        accounts = self.only('codex',
                             old={'model': 'gpt-6.1-terra', 'effort': 'high', 'provider': 'codex', 'disabled': True, 'disabled_by_default': True},
                             new={'model': 'gpt-6.1-sol', 'effort': 'high', 'provider': 'codex'})
        self.assertEqual(self.plan(self.task(), accounts)['choice'], 'new')
        self.assertEqual(self.plan(self.task(constraints={'model': 'gpt-6.1-terra'}), accounts)['choice'], 'old')
        unchecked = copy.deepcopy(accounts)
        del unchecked['accounts']['codex']['bundles']['old']['disabled_by_default']
        self.assertEqual(self.plan(self.task(constraints={'model': 'gpt-6.1-terra'}), unchecked)['choice'], 'none')

    def test_discovery_marks_only_the_default_as_off_by_default(self):
        catalog = {'providers': {'codex': {'ok': True, 'models': [{'id': 'claude-opus-4-6'}, {'id': 'gpt-5.6-terra'}], 'checked_at': 1000.0}}}
        accounts = copy.deepcopy(self.accounts)
        accounts['superseded'] = {'gpt-5.6-terra': {'by': 'gpt-6.1-terra', 'basis': 'test'}}
        accounts['accounts'] = {'codex': {**accounts['accounts']['codex'], 'bundles': {'seed': {'model': 'gpt-6.1-sol', 'effort': 'high', 'provider': 'codex'}}}}
        row = next(b for b in auto_bundles.derive(accounts, catalog, 1000.0)['bundles']['codex'].values() if b['launch_model'] == 'gpt-5.6-terra')
        self.assertTrue(row['disabled'] and row['disabled_by_default'])
        bid = auto_bundles.auto_bundle_id('codex', 'codex', 'gpt-5.6-terra', 'high')
        chosen = {'accounts': {'codex': {'bundles': {bid: {'disabled': True}}}}}
        row = auto_bundles.derive(accounts, catalog, 1000.0, overrides=chosen)['bundles']['codex'][bid]
        self.assertTrue(row['disabled'])
        self.assertNotIn('disabled_by_default', row)


class RecordConstraintTests(RouterCase):
    """R3 through `roll --provider ... --model ...`: 3.4."""

    def record(self, task=None, usage=None, **fields):
        args = argparse.Namespace(**{
            'basis': 'User asked', 'account': 'claude', 'provider': 'claude', 'model': 'claude-opus-5-5', 'effort': None,
            'input': None, 'usage_cache': None, 'accounts': None, 'constraint': 'user_override', 'specialty': None,
            'reason_category': None, 'fast': False, 'parent_id': None, **fields})
        with patch.object(route_judge, 'usage_snapshot', return_value=copy.deepcopy(usage or self.usage)), \
                patch.object(route_judge, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = NOW
            return route_judge.record_constraint(args, task)

    def test_omitting_effort_keeps_the_row_level(self):
        """3C-1: the matched row's effort was overwritten with None."""
        bundle = self.record()['selected_bundle']
        self.assertEqual(bundle['effort'], 'high')
        self.assertEqual(bundle['bundle_id'], 'opus55_high')

    def test_colon_effort_on_the_command_line(self):
        self.assertEqual(self.record(model='claude-opus-5-5:medium')['selected_bundle']['effort'], 'medium')

    def test_an_effort_with_no_row_inherits_mode_and_draws(self):
        """3C-2: the launch fields were lost for a level the account has no row for."""
        accounts = copy.deepcopy(self.accounts)
        accounts['accounts']['claude']['bundles']['opus55_high'].update(modeId='bypassPermissions', draws=['five_hour', 'seven_day'])
        with patch.object(account_fleet, 'load_accounts', return_value=accounts):
            bundle = self.record(effort='low')['selected_bundle']
        self.assertEqual((bundle['effort'], bundle['modeId'], bundle['draws']), ('low', 'bypassPermissions', ['five_hour', 'seven_day']))
        self.assertEqual(bundle['harness'], 'claude')

    def test_an_effort_with_no_row_cannot_slip_past_an_exhausted_window(self):
        """3C-4: quota was not checked for a level without a static row."""
        spent = usage_windows(NOW, {**FULL_QUOTA, 'claude': [('five_hour', 0, 3), ('seven_day', 0, 100), ('weekly_fable', 100, 100)]})
        spent['accounts']['claude']['status'] = 'fresh'
        accounts = copy.deepcopy(self.accounts)
        rows = accounts['accounts']['claude']['bundles']
        for bundle_id in [k for k, b in rows.items() if b.get('model') == 'claude-opus-5-5' and b.get('effort') == 'low']:
            del rows[bundle_id]
        for effort in ('high', 'low'):
            with self.subTest(effort=effort), patch.object(account_fleet, 'load_accounts', return_value=accounts), \
                    self.assertRaisesRegex(ValueError, 'five_hour|seven_day'):
                self.record(usage=spent, effort=effort)

    def test_a_cooling_route_is_refused(self):
        with patch.object(launch_failures, 'active', return_value={'x': 1}), \
                patch.object(launch_failures, 'reason', side_effect=lambda b, e, now=None: 'cooling down' if b.get('model') == 'claude-opus-5-5' else None), \
                self.assertRaisesRegex(ValueError, 'cooling down'):
            self.record(effort='high')

    def test_a_named_route_on_live_work_keeps_its_level(self):
        result = self.record(task=self.task(LIVE), effort='xhigh')
        self.assertEqual(result['selected_bundle']['effort'], 'xhigh')
        result_default = self.record(task=self.task(LIVE), model='gpt-6-astra', account='codex', provider='codex')
        self.assertEqual(result_default['selected_bundle']['effort'], 'high')

    def test_the_router_comparison_stays_a_free_choice(self):
        result = self.record(task=self.task(), model='gpt-6-astra', account='codex', provider='codex', effort='high')
        self.assertIn('router_choice', result)
        self.assertIsNotNone(result['router_choice'])

    def test_off_by_default_is_reachable_and_unchecked_is_not(self):
        accounts = copy.deepcopy(self.accounts)
        rows = accounts['accounts']['codex']['bundles']
        rows['old'] = {'model': 'gpt-5.6-terra', 'effort': 'high', 'disabled': True, 'disabled_by_default': True}
        rows['off'] = {'model': 'gpt-5.6-luna', 'effort': 'high', 'disabled': True}
        with patch.object(account_fleet, 'load_accounts', return_value=accounts):
            named = self.record(model='gpt-5.6-terra', account='codex', provider='codex', effort='high')
            self.assertEqual(named['selected_bundle']['bundle_id'], 'old')
            with self.assertRaisesRegex(ValueError, 'is disabled'):
                self.record(model='gpt-5.6-luna', account='codex', provider='codex', effort='high')

    def test_a_family_name_keeps_its_own_launch_id(self):
        accounts = self.only('claude', own={'model': 'claude-opus-4-20250514', 'effort': 'high', 'provider': 'claude'})
        with patch.object(account_fleet, 'load_accounts', return_value=accounts):
            bundle = self.record(model='claude-opus-4-20250514', effort='high')['selected_bundle']
        self.assertEqual((bundle['model'], bundle['launch_model'], bundle['bundle_id']),
                         ('claude-opus-4-20250514', 'claude-opus-4-20250514', 'own'))


BAD_COUNTS = (0, -1, 1.5, float('nan'), float('inf'), 'many', None, '4', True)


class ProfileValidationTests(RouterCase):
    """R4."""

    def test_task_count_bands(self):
        for raw, multi, expected in ((1, False, '1'), (2, True, '2'), (3, True, '3'), (4, True, '>3'), (9, True, '>3'), (2.0, True, '2'),
                                     ('1', False, '1'), ('2', True, '2'), ('3', True, '3'), ('>3', True, '>3')):
            with self.subTest(raw=raw):
                profile = task_intake.validate_profile(dict(PROFILE, multi_task=multi, task_count=raw))
                self.assertEqual(profile['task_count'], expected)
                self.assertEqual(task_intake.validate_profile(profile), profile)

    def test_anything_else_is_invalid(self):
        for raw in BAD_COUNTS:
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, 'task_count'):
                task_intake.validate_profile(dict(PROFILE, multi_task=True, task_count=raw))
        missing = {k: v for k, v in PROFILE.items() if k != 'task_count'}
        with self.assertRaisesRegex(ValueError, 'task_count'):
            task_intake.validate_profile(missing)
        with self.assertRaisesRegex(ValueError, 'multi_task'):
            task_intake.validate_profile({k: v for k, v in PROFILE.items() if k != 'multi_task'})

    def test_contradictions_are_invalid_not_repaired(self):
        """4.1: `multi_task true, task_count 1` passed through and printed "1 separate tasks"."""
        for multi, count in ((True, '1'), (True, 1), (False, '3'), (False, 2), (False, '>3')):
            with self.subTest(multi=multi, count=count), self.assertRaisesRegex(ValueError, 'contradicts'):
                task_intake.validate_profile(dict(PROFILE, multi_task=multi, task_count=count))

    def test_caller_profile_failure_has_its_code(self):
        caps = account_fleet.load_capabilities()
        bad = dict(PROFILE, multi_task=True, task_count='1')
        with self.assertRaisesRegex(ValueError, 'invalid_caller_profile'):
            route_judge.score_task({'objective': 'x', 'profile': bad}, caps)
        with self.assertRaisesRegex(ValueError, 'invalid_caller_profile'):
            self.plan(self.task(bad))

    def test_invalid_classifier_output_moves_on_and_finally_blocks(self):
        caps = account_fleet.load_capabilities()

        def answer(*_args, **_kw):
            return task_intake.validate_profile(dict(PROFILE, multi_task=True, task_count='1')), 'x', 'y'
        with patch.dict(os.environ, {'ROUTER_SCORE_CACHE': 'off'}), \
                patch.object(route_judge, 'scorer_accounts', return_value=[None]), \
                patch.object(route_judge, 'run_scorer', side_effect=answer) as scorer:
            profile, meta = route_judge.score_task({'objective': 'x'}, caps, scorer_config={'scorer_type': 'claude_haiku_low', 'fallback_to_agy': True})
        self.assertIsNone(profile)
        self.assertEqual(meta['source'], 'scorer_unavailable')
        self.assertEqual(meta['fallback_reason'], 'ValueError')
        self.assertGreater(scorer.call_count, 1)

    def test_a_cached_profile_is_revalidated_and_a_bad_one_is_a_miss(self):
        """4.2: a stale cache entry raised through score_task."""
        caps = account_fleet.load_capabilities()
        config = {'scorer_type': 'claude_haiku_low', 'fallback_to_agy': False}
        task = {'objective': 'hello world'}
        order = route_judge.scorer_attempt_order(config)[1]
        key = route_judge.score_cache_key(task, caps, order, config)
        good = task_intake.validate_profile(dict(PROFILE))
        for stale in ({'work_kind': 'removed_legacy_kind'}, dict(PROFILE, multi_task=True, task_count='1'), {k: v for k, v in PROFILE.items() if k != 'task_count'}):
            with self.subTest(stale=stale), patch.dict(os.environ, {'ROUTER_SCORE_CACHE': str(Path(self.root) / 'cache.json')}):
                route_judge.score_cache_put(key, stale, {'source': 'system_one', 'scorer_type': 'claude_haiku_low'})
                with patch.object(route_judge, 'scorer_accounts', return_value=[None]), \
                        patch.object(route_judge, 'run_scorer', return_value=(good, 'p', 'm')) as scorer:
                    profile, meta = route_judge.score_task(task, caps, scorer_config=config)
                self.assertEqual(profile, good)
                self.assertFalse(meta.get('cached'))
                scorer.assert_called_once()

    def test_cache_key_covers_every_structured_input_and_copies_are_isolated(self):
        caps = account_fleet.load_capabilities()
        config = {'scorer_type': 'claude_haiku_low', 'fallback_to_agy': False}
        order = route_judge.scorer_attempt_order(config)[1]
        base = {'objective': 'o', 'context': 'c', 'environment': 'e', 'expected_hours': 1, 'needs': {'gui': False}}
        keys = {route_judge.score_cache_key(base, caps, order, config)}
        for name, value in (('objective', 'o2'), ('context', 'c2'), ('environment', 'e2'), ('expected_hours', 2), ('needs', {'gui': True})):
            keys.add(route_judge.score_cache_key({**base, name: value}, caps, order, config))
        self.assertEqual(len(keys), 6)
        self.assertEqual(set(route_judge.SCORER_TASK_FIELDS), {'objective', 'context', 'environment', 'expected_hours', 'needs'})
        with patch.dict(os.environ, {'ROUTER_SCORE_CACHE': str(Path(self.root) / 'cache2.json')}):
            profile, scorer = dict(PROFILE), {'source': 'system_one', 'attempts': []}
            route_judge.score_cache_put('k', profile, scorer)
            profile['reasoning_depth'] = 4
            scorer['attempts'].append('later')
            first = route_judge.score_cache_get('k')
            self.assertEqual(first['profile']['reasoning_depth'], 2)
            self.assertEqual(first['scorer']['attempts'], [])
            first['profile']['reasoning_depth'] = 3
            self.assertEqual(route_judge.score_cache_get('k')['profile']['reasoning_depth'], 2)


if __name__ == '__main__':
    unittest.main()
