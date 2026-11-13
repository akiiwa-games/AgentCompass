"""Findings of the adversarial review of the routing contract work: the record and prepare paths, family rows, the meaning
of "best level", and the remaining corners of profile validation."""
import argparse
import copy
from datetime import datetime
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
from unittest.mock import patch
import unittest

import pytest

import account_fleet
import candidate_eval
import task_intake
import dispatch_registry
import launch_failures
import roll
import route_judge
import decision_ledger
import scorer_client
import usage_reading
from router_test_support import usage_windows
from test_routing_parity import FULL_QUOTA, LIVE, NOW, PROFILE, RouterCase


FAMILY = 'claude-opus-4-20250514'
FLAGSHIP = 'claude-opus-5-5'
FAMILY_9 = 'claude-opus-9'


class RecordCase(RouterCase):
    def record(self, task=None, accounts=None, **fields):
        args = argparse.Namespace(**{
            'basis': 'User asked', 'account': 'claude', 'provider': 'claude', 'model': FLAGSHIP, 'effort': None,
            'input': None, 'usage_cache': None, 'accounts': None, 'constraint': 'user_override', 'specialty': None,
            'reason_category': None, 'fast': False, 'parent_id': None, **fields})
        with patch.object(usage_reading, 'usage_snapshot', return_value=copy.deepcopy(self.usage)), \
                patch.object(account_fleet, 'load_accounts', return_value=copy.deepcopy(accounts or self.accounts)), \
                patch.object(route_judge, 'datetime', wraps=datetime) as clock, \
                patch.object(decision_ledger, 'datetime', wraps=datetime) as ledger_clock:
            clock.now.return_value = ledger_clock.now.return_value = NOW
            return route_judge.record_constraint(args, task)

    def launched(self, accounts=None, **fields):
        """record, then the real prepare: what would actually start."""
        task = self.task(parent_agent_id='parent-1', task_id='t-' + os.urandom(4).hex())
        fleet = accounts or self.accounts
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory, \
                patch.dict(os.environ, {'ROUTER_DISPATCH_DB': os.path.join(directory, 'db.sqlite3')}):
            result = self.record(task, fleet, **fields)
            with patch.object(account_fleet, 'load_accounts', return_value=copy.deepcopy(fleet)):
                row = dispatch_registry.prepare(result['decision_id'], task, {'title': 'x', 'initialPrompt': 'go'},
                                                copy.deepcopy(self.usage), accounts=copy.deepcopy(fleet), now=NOW)
        return result, row


@pytest.mark.slow
class CallerProfileEntryTests(RecordCase):
    """M1: the CLI read multi_task from an unchecked profile and offered to split on it."""

    def cli(self, profile, *extra):
        path = Path(self.root) / 'in.json'
        path.write_text(json.dumps({'task': {'objective': 'x', 'work_kind': 'general_implementation', 'expected_hours': 1,
                                             'needs': {'local_only': True}, 'profile': profile}}), encoding='utf-8')
        script = Path(route_judge.__file__)
        done = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(script), 'plan', '--input', str(path), '--dry-run', *extra],
                              capture_output=True, text=True, encoding='utf-8', env=dict(os.environ), timeout=120)
        return done.returncode, json.loads(done.stdout)

    def test_an_invalid_split_answer_is_reported_not_split_on(self):
        cases = [dict(PROFILE, multi_task=True, task_count=count) for count in ('1', 0, -1, 1.5, 'many', None)]
        cases.append({k: v for k, v in dict(PROFILE, multi_task=True).items() if k != 'task_count'})
        for profile in cases:
            with self.subTest(task_count=profile.get('task_count', 'missing')):
                code, output = self.cli(profile)
                self.assertEqual(code, 1, output)
                self.assertNotEqual(output.get('stage'), 'split')
                self.assertEqual(output.get('error_type'), 'InvalidCallerProfile')
                self.assertIn('invalid_caller_profile', output.get('detail', ''))

    def test_a_valid_split_answer_splits_on_its_band(self):
        code, output = self.cli(dict(PROFILE, multi_task=True, task_count=9))
        self.assertEqual((code, output.get('stage'), output.get('task_count')), (2, 'split', '>3'))


class FamilyRowTests(RecordCase):
    """M3, M6: a family name is its own model everywhere a row or a launch is chosen."""

    def test_an_unconfigured_family_name_is_blocked_on_record(self):
        spelled = copy.deepcopy(self.accounts)
        for row in spelled['accounts']['claude']['bundles'].values():
            if row.get('model') == FLAGSHIP:
                row['launch_model'] = FLAGSHIP
        for fleet in (self.accounts, spelled):
            with self.assertRaisesRegex(ValueError, 'named_model_unavailable'):
                self.record(accounts=fleet, model=FAMILY, effort='high')
        with self.assertRaisesRegex(ValueError, 'named_model_unavailable'):
            self.record(model='gemini-2.5-pro', account='antigravity', provider='antigravity-resilient', effort='high')

    def test_a_configured_family_name_launches_itself_on_both_sides_of_the_ledger(self):
        fleet = self.only('claude', own={'model': FAMILY, 'effort': 'high', 'provider': 'claude'},
                          top={'model': FLAGSHIP, 'effort': 'high', 'provider': 'claude', 'launch_model': FLAGSHIP})
        result, row = self.launched(fleet, model=FAMILY, effort='high')
        self.assertEqual(row['request']['provider'], f'claude/{FAMILY}')
        self.assertEqual(row['bundle']['model'], FAMILY)
        self.assertEqual(row['bundle']['bundle_id'], 'own')
        self.assertEqual(result['selected_bundle']['model'], FAMILY)

    def test_a_family_level_with_no_row_and_no_table_entry_is_blocked_on_record(self):
        fleet = self.only('claude', own={'model': FAMILY, 'effort': 'high', 'provider': 'claude'},
                          top={'model': FLAGSHIP, 'effort': 'medium', 'provider': 'claude'})
        with self.assertRaisesRegex(ValueError, 'named_model_unavailable'):
            self.record(accounts=fleet, model=FAMILY, effort='medium')

    def test_naming_the_flagship_never_takes_a_family_row(self):
        fleet = self.only('claude', opus_high={'model': FLAGSHIP, 'effort': 'high', 'provider': 'claude'},
                          opus_medium={'model': FLAGSHIP, 'effort': 'medium', 'provider': 'claude'},
                          old_high={'model': FAMILY, 'effort': 'high', 'provider': 'claude'})
        for profile in (PROFILE, LIVE):
            for constraints in ({'model': FLAGSHIP}, {'model': FLAGSHIP, 'effort': 'high'}):
                with self.subTest(realtime=profile is LIVE, constraints=constraints):
                    plan = self.plan(self.task(profile, constraints=constraints), fleet)
                    self.assertEqual(plan['selected_bundle']['model'], FLAGSHIP)
                    self.assertIn('explicit model constraint', self.row(plan, 'old_high')['excluded'])

    def test_a_family_row_leaves_the_flagship_step_down_alone(self):
        flagship = {'opus_high': {'model': FLAGSHIP, 'effort': 'high', 'provider': 'claude'}}
        control = self.row(self.plan(self.task(LIVE), self.only('claude', **flagship)), 'opus_high')['bundle']['effort']
        for label, family_row in (('unchecked', {'model': FAMILY, 'effort': control, 'provider': 'claude', 'disabled': True}),
                                  ('enabled and lower', {'model': FAMILY, 'effort': 'low', 'provider': 'claude'})):
            with self.subTest(label):
                fleet = self.only('claude', **flagship, old=family_row)
                self.assertEqual(self.row(self.plan(self.task(LIVE), fleet), 'opus_high')['bundle']['effort'], control)

    def test_a_family_level_unchecked_does_not_reach_the_flagship_in_prepare(self):
        fleet = self.only('claude', opus_high={'model': FLAGSHIP, 'effort': 'high', 'provider': 'claude'},
                          old={'model': FAMILY, 'effort': 'medium', 'provider': 'claude', 'disabled': True})
        pinned = dict(account_fleet.bundles(fleet)['opus_high'], id='opus_high@medium', bundle_id='opus_high@medium',
                      effort='medium', pin_carrier='opus_high')
        self.assertEqual(dispatch_registry.resolve_bundle(pinned, fleet)['effort'], 'medium')

    def test_a_worker_of_one_model_is_not_reused_for_the_other(self):
        same = roll.model_comparison(self.accounts)
        row = {'provider': 'claude', 'account': 'claude', 'effort': 'high'}
        self.assertFalse(roll._bundle_matches_worker({**row, 'model': FAMILY}, {'bundle': {**row, 'model': FLAGSHIP}}, same))
        self.assertFalse(roll._bundle_matches_worker({**row, 'model': FLAGSHIP}, {'bundle': {**row, 'model': FAMILY}}, same))
        self.assertTrue(roll._bundle_matches_worker({**row, 'model': 'claude-opus-5'}, {'bundle': {**row, 'model': FLAGSHIP}}, same))

    def test_a_family_cooldown_stays_on_the_family(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / 'failures.json'
            launch_failures.record({'account': 'claude', 'model': FAMILY}, 'model_unavailable', now=NOW, path=path)
            entries = launch_failures.active(now=NOW, path=path)
        self.assertIsNotNone(candidate_eval.cooling_reason({'account': 'claude', 'model': FAMILY}, entries, self.accounts, now=NOW))
        self.assertIsNone(candidate_eval.cooling_reason({'account': 'claude', 'model': FLAGSHIP}, entries, self.accounts, now=NOW))
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / 'failures.json'
            launch_failures.record({'account': 'claude', 'model': FLAGSHIP}, 'model_unavailable', now=NOW, path=path)
            entries = launch_failures.active(now=NOW, path=path)
        self.assertIsNone(candidate_eval.cooling_reason({'account': 'claude', 'model': FAMILY}, entries, self.accounts, now=NOW))


class RecordPathTests(RecordCase):
    """M4, M5: record agrees with plan and prepare."""

    def test_a_dash_effort_is_an_effort_on_record(self):
        for model in ('gpt-6-astra-medium', 'openai/gpt-6-astra-medium', 'gpt-6-astra:medium'):
            with self.subTest(model=model):
                result = self.record(model=model, account='codex', provider='codex')
                self.assertEqual(result['selected_bundle']['effort'], 'medium')
                self.assertEqual(result['selected_bundle']['model'], 'gpt-6-astra')

    def test_a_dash_effort_survives_prepare(self):
        _, row = self.launched(model='gpt-6-astra-medium', account='codex', provider='codex')
        self.assertEqual((row['request']['provider'], row['bundle']['effort']), ('codex/gpt-6-astra', 'medium'))

    def test_every_level_unchecked_refuses_a_level_with_no_row(self):
        fleet = copy.deepcopy(self.accounts)
        for row in fleet['accounts']['claude']['bundles'].values():
            if row.get('model') == FLAGSHIP:
                row['disabled'] = True
        for effort in ('high', 'low', None):
            with self.subTest(effort=effort), self.assertRaisesRegex(ValueError, 'is disabled'):
                self.record(accounts=fleet, effort=effort)

    def test_one_level_unchecked_leaves_the_others(self):
        fleet = copy.deepcopy(self.accounts)
        fleet['accounts']['claude']['bundles']['opus55_medium']['disabled'] = True
        self.assertEqual(self.record(accounts=fleet, effort='low')['selected_bundle']['effort'], 'low')
        with self.assertRaisesRegex(ValueError, 'is disabled'):
            self.record(accounts=fleet, effort='medium')

    def test_off_by_default_rows_stay_reachable_at_any_level(self):
        fleet = copy.deepcopy(self.accounts)
        for row in fleet['accounts']['claude']['bundles'].values():
            if row.get('model') == FLAGSHIP:
                row.update(disabled=True, disabled_by_default=True)
        bundle = self.record(accounts=fleet, effort='low')['selected_bundle']
        self.assertEqual(bundle['effort'], 'low')
        self.assertEqual(bundle['modeId'], 'bypassPermissions')


class BestLevelTests(RecordCase):
    """M7: a named effort carrier reaches prepare and resolves only within the launch contract."""

    def levels(self, model, *efforts, provider='codex', account='codex'):
        return self.only(account, **{e: {'model': model, 'effort': e, 'provider': provider} for e in efforts})

    def test_prepare_accepts_any_contract_level_via_pin_carrier_and_no_other(self):
        fleet = self.accounts
        row = account_fleet.bundles(fleet)['sol_high']
        for effort, accepted in (('low', True), ('medium', True), ('bogus', False)):
            with self.subTest(effort=effort):
                pinned = dict(row, id=f'sol_high@{effort}', bundle_id=f'sol_high@{effort}', effort=effort, pin_carrier='sol_high')
                resolved = dispatch_registry.resolve_bundle(pinned, fleet)
                self.assertEqual(resolved is not None, accepted)

    def test_the_named_level_reaches_prepare(self):
        task = self.task(LIVE, constraints={'model': 'gpt-6-astra', 'effort': 'medium'}, parent_agent_id='p', task_id='t')
        fleet = self.levels('gpt-6-astra', 'high')
        plan = self.plan(task, fleet)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory, \
                patch.object(account_fleet, 'load_accounts', return_value=copy.deepcopy(fleet)):
            db = os.path.join(directory, 'reg.sqlite')
            issued = dispatch_registry.issue(plan, task, now=NOW, path=db, allocation_task=task)
            prepared = dispatch_registry.prepare(issued['decision_id'], task, {'title': 'x', 'initialPrompt': 'go'},
                                                 self.usage, accounts=copy.deepcopy(fleet), now=NOW, path=db)
        self.assertEqual(prepared['bundle']['effort'], 'medium')


class MinorFindingTests(RecordCase):
    def test_a_row_listing_its_efforts_passes_prepare_only_for_listed_efforts(self):
        """m1: prepare resolves a pin carrier only for efforts the row offers when unlisted in dispatch_efforts."""
        fleet = self.only('codex', astra={'model': 'gpt-6-astra', 'effort': 'high', 'provider': 'codex', 'efforts': ['low', 'high']})
        fleet['dispatch_efforts']['codex'].pop('gpt-6-astra', None)
        plan = self.plan(self.task(LIVE, constraints={'model': 'gpt-6-astra', 'effort': 'low'}), fleet)
        self.assertEqual(plan['selected_bundle']['effort'], 'low')
        self.assertEqual(dispatch_registry.resolve_bundle(dict(plan['selected_bundle']), fleet)['effort'], 'low')
        unlisted = dict(account_fleet.bundles(fleet)['astra'], id='astra@medium', bundle_id='astra@medium',
                        effort='medium', pin_carrier='astra')
        self.assertIsNone(dispatch_registry.resolve_bundle(unlisted, fleet))

    def test_validation_is_idempotent_for_any_reason(self):
        """m3: a cut at 300 characters could leave a trailing space, which the next pass removed."""
        rng = random.Random(7)
        reasons = ['y' * 299 + ' z', ' ' + 'y' * 299 + ' z', 'y' * 299 + '  ']
        reasons += [''.join(rng.choice('ab  \t\n') for _ in range(rng.randint(1, 340))) for _ in range(500)]
        for reason in reasons:
            once = task_intake.validate_profile(dict(PROFILE, reason=reason))
            self.assertEqual(task_intake.validate_profile(once), once, repr(reason))

    def test_a_huge_whole_number_is_a_band_and_huge_hours_are_invalid(self):
        """m4: math.isfinite raised OverflowError out of validation."""
        self.assertEqual(task_intake.validate_profile(dict(PROFILE, multi_task=True, task_count=10 ** 400))['task_count'], '>3')
        self.assertEqual(self.plan(self.task(dict(PROFILE, multi_task=True, task_count=10 ** 400)))['choice'] is not None, True)
        with self.assertRaisesRegex(ValueError, 'expected_hours'):
            task_intake.validate_profile(dict(PROFILE, expected_hours=10 ** 400))
        with self.assertRaisesRegex(ValueError, 'task_count'):
            task_intake.validate_profile(dict(PROFILE, multi_task=True, task_count=-10 ** 400))

    def test_a_cached_huge_number_is_only_a_miss(self):
        caps = account_fleet.load_capabilities()
        config = {'scorer_type': 'claude_haiku_low', 'fallback_to_agy': False}
        task = {'objective': 'hello world'}
        key = scorer_client.score_cache_key(task, caps, scorer_client.scorer_attempt_order(config)[1], config)
        good = task_intake.validate_profile(dict(PROFILE))
        with patch.dict(os.environ, {'ROUTER_SCORE_CACHE': str(Path(self.root) / 'cache.json')}):
            scorer_client.score_cache_put(key, dict(PROFILE, expected_hours=10 ** 400), {'source': 'system_one', 'scorer_type': 'claude_haiku_low'})
            with patch.object(scorer_client, 'scorer_accounts', return_value=[None]), \
                    patch.object(scorer_client, 'run_scorer', return_value=(good, 'p', 'm')) as scorer:
                self.assertEqual(scorer_client.score_task(task, caps, scorer_config=config)[0], good)
            scorer.assert_called_once()

    def test_a_cooldown_recorded_under_any_spelling_cools_the_model(self):
        """m5: a failure on an alias row left the exact row launching."""
        for recorded, row in (('claude-opus-5', FLAGSHIP), (FLAGSHIP, 'claude-opus-5'), ('Anthropic/Claude-Opus-5-5', FLAGSHIP)):
            with self.subTest(recorded=recorded), tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
                path = Path(directory) / 'failures.json'
                launch_failures.record({'account': 'claude', 'model': recorded}, 'model_unavailable', now=NOW, path=path)
                entries = launch_failures.active(now=NOW, path=path)
                self.assertIsNotNone(candidate_eval.cooling_reason({'account': 'claude', 'model': row}, entries, self.accounts, now=NOW))


class PinIdentityTests(RecordCase):
    """X1 and R4-r2, the cross-repo reading of contract R1: a family row is another model to a pin and to a checkbox."""

    @staticmethod
    def claude(model, **fields):
        return {'model': model, 'provider': 'claude', **fields}

    def test_a_pin_on_either_name_runs_that_name(self):
        """Magpie's parity grid: 4436 rows where this side let the family row answer to a pin on the flagship."""
        for pinned, other in ((FLAGSHIP, FAMILY_9), (FAMILY_9, FLAGSHIP)):
            for efforts in (['high'], ['low', 'high'], ['medium', 'high', 'xhigh']):
                rows = {f'{model}:{effort}': self.claude(model, effort=effort) for model in (pinned, other) for effort in efforts}
                fleet = self.only('claude', **rows)
                for constraints in ({'model': pinned}, {'model': pinned, 'effort': efforts[0]}, {'model': f'{pinned}:{efforts[0]}'}):
                    for profile in (PROFILE, LIVE):
                        with self.subTest(pinned=pinned, efforts=efforts, constraints=constraints, realtime=profile is LIVE):
                            plan = self.plan(self.task(profile, constraints=constraints), fleet)
                            self.assertEqual(plan['selected_bundle']['model'], pinned)

    def test_a_flagship_that_is_down_is_refused_with_a_family_row_beside_it(self):
        fleet = self.only('claude', a=self.claude(FLAGSHIP, effort='high'), b=self.claude(FAMILY_9, effort='high'))
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / 'failures.json'
            launch_failures.record({'account': 'claude', 'model': FLAGSHIP}, 'model_unavailable', path=path)     # the plan reads the real clock
            with patch.dict(os.environ, {'ROUTER_LAUNCH_FAILURES': str(path)}):
                named = self.plan(self.task(PROFILE, constraints={'model': FLAGSHIP}), fleet)
                unnamed = self.plan(self.task(PROFILE), fleet)
        self.assertEqual(named['choice'], 'none')
        self.assertEqual(named['decision']['blocked'], 'named_model_unavailable')
        # The family row has no data of its own (mode B), so the unnamed plan does not take it either; the flagship stays refused.
        self.assertEqual(unnamed['choice'], 'none')

    def test_unchecking_a_family_level_leaves_the_flagship_at_that_level(self):
        for label, own in (('listed', {'efforts': ['low', 'high']}), ('written', {'effort': 'high'}), ('no row', {'effort': 'medium'})):
            with self.subTest(label):
                fleet = self.only('claude', a=self.claude(FLAGSHIP, **own), b=self.claude(FAMILY_9, effort='high', disabled=True))
                plan = self.plan(self.task(PROFILE, constraints={'model': f'{FLAGSHIP}:high'}), fleet)
                self.assertEqual((plan['selected_bundle']['model'], plan['selected_bundle']['effort']), (FLAGSHIP, 'high'))
                _, row = self.launched(fleet, model=FLAGSHIP, effort='high')
                self.assertEqual((row['bundle']['model'], row['bundle']['effort']), (FLAGSHIP, 'high'))

    def test_unchecking_a_flagship_level_leaves_the_family_row_at_that_level(self):
        for label, own in (('listed', {'efforts': ['low', 'high']}), ('written', {'effort': 'high'})):
            with self.subTest(label):
                fleet = self.only('claude', a=self.claude(FAMILY_9, **own), b=self.claude(FLAGSHIP, effort='high', disabled=True))
                plan = self.plan(self.task(PROFILE, constraints={'model': f'{FAMILY_9}:high'}), fleet)
                self.assertEqual((plan['selected_bundle']['model'], plan['selected_bundle']['effort']), (FAMILY_9, 'high'))
                _, row = self.launched(fleet, model=FAMILY_9, effort='high')
                self.assertEqual((row['bundle']['model'], row['bundle']['effort']), (FAMILY_9, 'high'))

    def test_a_level_unchecked_on_the_model_itself_is_still_refused(self):
        fleet = self.only('claude', a=self.claude(FLAGSHIP, efforts=['low', 'high']), b=self.claude(FLAGSHIP, effort='high', disabled=True))
        self.assertEqual(self.plan(self.task(PROFILE, constraints={'model': f'{FLAGSHIP}:high'}), fleet)['choice'], 'none')
        with self.assertRaisesRegex(ValueError, 'is disabled'):
            self.record(accounts=fleet, model=FLAGSHIP, effort='high')

    def test_a_family_row_offers_only_the_levels_it_lists(self):
        fleet = self.only('claude', a=self.claude(FAMILY_9, efforts=['low', 'high']))
        self.assertEqual(self.plan(self.task(PROFILE, constraints={'model': f'{FAMILY_9}:medium'}), fleet)['choice'], 'none')
        with self.assertRaisesRegex(ValueError, 'named_model_unavailable'):
            self.record(accounts=fleet, model=FAMILY_9, effort='medium')


class ListedLevelTests(RecordCase):
    """m7: a row listing its levels does not block a sibling from carrying a named effort."""

    X = {'model': 'gpt-6-astra', 'effort': 'high', 'efforts': ['high'], 'provider': 'codex'}
    Y = {'model': 'gpt-6-astra', 'effort': 'xhigh', 'provider': 'codex'}

    def test_a_row_with_a_lower_level_answers_for_that_named_level(self):
        fleet = self.only('codex', x={**self.X, 'efforts': ['medium', 'high']}, y=self.Y)
        plan = self.plan(self.task(LIVE, constraints={'model': 'gpt-6-astra', 'effort': 'medium'}), fleet)
        self.assertEqual((plan['choice'], plan['selected_bundle']['effort']), ('x@medium', 'medium'))
        self.assertEqual(dispatch_registry.resolve_bundle(plan['selected_bundle'], fleet)['effort'], 'medium')

    def test_a_named_level_does_not_depend_on_the_order_of_the_rows(self):
        for order in (('x', 'y'), ('y', 'x')):
            with self.subTest(order=order):
                fleet = self.only('codex', **{name: {'x': self.X, 'y': self.Y}[name] for name in order})
                plan = self.plan(self.task(LIVE, constraints={'model': 'gpt-6-astra', 'effort': 'medium'}), fleet)
                self.assertEqual(plan['choice'], 'y@medium')
                self.assertEqual(dispatch_registry.resolve_bundle(plan['selected_bundle'], fleet)['effort'], 'medium')


if __name__ == '__main__':
    unittest.main()
