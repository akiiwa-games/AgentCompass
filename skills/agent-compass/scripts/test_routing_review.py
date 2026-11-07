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

import allocator
import dispatch_registry
import launch_failures
import roll
import route_judge
from router_test_support import usage_windows
from test_routing_parity import FULL_QUOTA, LIVE, NOW, PROFILE, RouterCase


FAMILY = 'claude-opus-4-20250514'
FLAGSHIP = 'claude-opus-5-5'
FAMILY_9 = 'claude-opus-9'


class RecordCase(RouterCase):
    def setUp(self):
        super().setUp()
        # route_judge loads its own copy of the module; share ours so the fleet can be patched.
        patcher = patch.object(route_judge, 'load_allocator', return_value=allocator)
        patcher.start()
        self.addCleanup(patcher.stop)

    def record(self, task=None, accounts=None, **fields):
        args = argparse.Namespace(**{
            'basis': 'User asked', 'account': 'claude', 'provider': 'claude', 'model': FLAGSHIP, 'effort': None,
            'input': None, 'usage_cache': None, 'accounts': None, 'constraint': 'user_override', 'specialty': None,
            'reason_category': None, 'fast': False, 'parent_id': None, **fields})
        with patch.object(route_judge, 'usage_snapshot', return_value=copy.deepcopy(self.usage)), \
                patch.object(allocator, 'load_accounts', return_value=copy.deepcopy(accounts or self.accounts)), \
                patch.object(route_judge, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = NOW
            return route_judge.record_constraint(args, task)

    def launched(self, accounts=None, **fields):
        """record, then the real prepare: what would actually start."""
        task = self.task(parent_agent_id='parent-1', task_id='t-' + os.urandom(4).hex())
        fleet = accounts or self.accounts
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory, \
                patch.dict(os.environ, {'ROUTER_DISPATCH_DB': os.path.join(directory, 'db.sqlite3')}):
            result = self.record(task, fleet, **fields)
            with patch.object(allocator, 'load_accounts', return_value=copy.deepcopy(fleet)):
                row = dispatch_registry.prepare(result['decision_id'], task, {'title': 'x', 'initialPrompt': 'go'},
                                                copy.deepcopy(self.usage), accounts=copy.deepcopy(fleet), now=NOW)
        return result, row


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
        stepped = dict(allocator.bundles(fleet)['opus_high'], bundle_id='opus_high', effort='medium', realtime_stepped_from='high')
        self.assertEqual(dispatch_registry.resolve_bundle(stepped, fleet, False, True)['effort'], 'medium')

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
        self.assertIsNotNone(allocator.cooling_reason({'account': 'claude', 'model': FAMILY}, entries, self.accounts, now=NOW))
        self.assertIsNone(allocator.cooling_reason({'account': 'claude', 'model': FLAGSHIP}, entries, self.accounts, now=NOW))
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / 'failures.json'
            launch_failures.record({'account': 'claude', 'model': FLAGSHIP}, 'model_unavailable', now=NOW, path=path)
            entries = launch_failures.active(now=NOW, path=path)
        self.assertIsNone(allocator.cooling_reason({'account': 'claude', 'model': FAMILY}, entries, self.accounts, now=NOW))


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
    """M7, by the parent's ruling: the best level is the highest-scoring one that clears the floor, however the rows are written."""

    def levels(self, model, *efforts, provider='codex', account='codex'):
        return self.only(account, **{e: {'model': model, 'effort': e, 'provider': provider} for e in efforts})

    def test_the_same_model_gets_the_same_level_however_many_rows_it_has(self):
        alone = self.plan(self.task(LIVE, constraints={'model': 'gpt-6-astra'}), self.levels('gpt-6-astra', 'high'))
        rows = self.plan(self.task(LIVE, constraints={'model': 'gpt-6-astra'}), self.levels('gpt-6-astra', 'low', 'medium', 'high'))
        self.assertEqual(alone['selected_bundle']['effort'], rows['selected_bundle']['effort'])

    def test_the_winner_outscores_every_other_level_that_clears(self):
        plan = self.plan(self.task(LIVE, constraints={'model': 'gpt-6-astra'}), self.levels('gpt-6-astra', 'high'))
        eligible = [c for c in plan['ranking'] if not c['excluded']]
        self.assertGreater(len(eligible), 1)
        self.assertEqual(plan['choice'], max(eligible, key=lambda c: c['score'])['id'])
        floor = self.accounts['scoring']['responsiveness']['floor']
        self.assertTrue(all(c['components']['responsiveness'] >= floor for c in eligible))

    def test_prepare_accepts_any_level_that_clears_and_no_other(self):
        fleet = self.accounts
        row = allocator.bundles(fleet)['sol_high']
        for effort, accepted in (('low', True), ('medium', True)):
            with self.subTest(effort=effort):
                stepped = dict(row, bundle_id='sol_high', effort=effort, realtime_stepped_from='high')
                resolved = dispatch_registry.resolve_bundle(stepped, fleet, False, True)
                self.assertEqual(resolved is not None, accepted)
        slow = copy.deepcopy(fleet)
        slow['effort_curves'][row['model']]['responsiveness']['medium'] = 0.1
        stepped = dict(row, bundle_id='sol_high', effort='medium', realtime_stepped_from='high')
        self.assertIsNone(dispatch_registry.resolve_bundle(stepped, slow, False, True))

    def test_the_named_level_reaches_prepare(self):
        task = self.task(LIVE, constraints={'model': 'gpt-6-astra'}, parent_agent_id='p', task_id='t')
        fleet = self.levels('gpt-6-astra', 'high')
        plan = self.plan(task, fleet)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory, \
                patch.object(allocator, 'load_accounts', return_value=copy.deepcopy(fleet)):
            db = os.path.join(directory, 'reg.sqlite')
            issued = dispatch_registry.issue(plan, task, now=NOW, path=db, allocation_task=task)
            prepared = dispatch_registry.prepare(issued['decision_id'], task, {'title': 'x', 'initialPrompt': 'go'},
                                                 self.usage, accounts=copy.deepcopy(fleet), now=NOW, path=db)
        self.assertEqual(prepared['bundle']['effort'], plan['selected_bundle']['effort'])

    def test_an_unnamed_row_still_takes_the_nearest_level_that_clears(self):
        plan = self.plan(self.task(LIVE), self.levels('gpt-6-astra', 'high'))
        self.assertEqual(self.row(plan, 'high')['bundle']['effort'], 'medium')


class MinorFindingTests(RecordCase):
    def test_a_row_listing_its_efforts_passes_prepare(self):
        """m1: the plan stepped to a listed level and prepare asked for a level the row does not offer."""
        fleet = self.only('codex', astra={'model': 'gpt-6-astra', 'effort': 'high', 'provider': 'codex', 'efforts': ['low', 'high']})
        plan = self.plan(self.task(LIVE), fleet)
        self.assertEqual(plan['selected_bundle']['effort'], 'low')
        self.assertEqual(dispatch_registry.resolve_bundle(dict(plan['selected_bundle']), fleet, False, True)['effort'], 'low')
        stepped = dict(allocator.bundles(fleet)['astra'], bundle_id='astra', effort='medium', realtime_stepped_from='high')
        self.assertIsNone(dispatch_registry.resolve_bundle(stepped, fleet, False, True))

    def test_a_fixed_level_does_not_hold_back_its_siblings(self):
        """m2: a row that cannot step sat below the other row and stopped it from stepping."""
        fleet = self.only('codex', x={'model': 'gpt-6-astra:high', 'provider': 'codex'},
                          y={'model': 'gpt-6-astra', 'effort': 'xhigh', 'provider': 'codex'})
        plan = self.plan(self.task(LIVE), fleet)
        self.assertEqual(plan['choice'], 'y')
        self.assertEqual(plan['selected_bundle']['effort'], 'medium')

    def test_validation_is_idempotent_for_any_reason(self):
        """m3: a cut at 300 characters could leave a trailing space, which the next pass removed."""
        rng = random.Random(7)
        reasons = ['y' * 299 + ' z', ' ' + 'y' * 299 + ' z', 'y' * 299 + '  ']
        reasons += [''.join(rng.choice('ab  \t\n') for _ in range(rng.randint(1, 340))) for _ in range(500)]
        for reason in reasons:
            once = allocator.validate_profile(dict(PROFILE, reason=reason))
            self.assertEqual(allocator.validate_profile(once), once, repr(reason))

    def test_a_huge_whole_number_is_a_band_and_huge_hours_are_invalid(self):
        """m4: math.isfinite raised OverflowError out of validation."""
        self.assertEqual(allocator.validate_profile(dict(PROFILE, multi_task=True, task_count=10 ** 400))['task_count'], '>3')
        self.assertEqual(self.plan(self.task(dict(PROFILE, multi_task=True, task_count=10 ** 400)))['choice'] is not None, True)
        with self.assertRaisesRegex(ValueError, 'expected_hours'):
            allocator.validate_profile(dict(PROFILE, expected_hours=10 ** 400))
        with self.assertRaisesRegex(ValueError, 'task_count'):
            allocator.validate_profile(dict(PROFILE, multi_task=True, task_count=-10 ** 400))

    def test_a_cached_huge_number_is_only_a_miss(self):
        caps = allocator.load_capabilities()
        config = {'scorer_type': 'claude_haiku_low', 'fallback_to_agy': False}
        task = {'objective': 'hello world'}
        key = route_judge.score_cache_key(task, allocator, caps, route_judge.scorer_attempt_order(config)[1], config)
        good = allocator.validate_profile(dict(PROFILE))
        with patch.dict(os.environ, {'ROUTER_SCORE_CACHE': str(Path(self.root) / 'cache.json')}):
            route_judge.score_cache_put(key, dict(PROFILE, expected_hours=10 ** 400), {'source': 'system_one', 'scorer_type': 'claude_haiku_low'})
            with patch.object(route_judge, 'scorer_accounts', return_value=[None]), \
                    patch.object(route_judge, 'run_scorer', return_value=(good, 'p', 'm')) as scorer:
                self.assertEqual(route_judge.score_task(task, allocator, caps, scorer_config=config)[0], good)
            scorer.assert_called_once()

    def test_a_cooldown_recorded_under_any_spelling_cools_the_model(self):
        """m5: a failure on an alias row left the exact row launching."""
        for recorded, row in (('claude-opus-5', FLAGSHIP), (FLAGSHIP, 'claude-opus-5'), ('Anthropic/Claude-Opus-5-5', FLAGSHIP)):
            with self.subTest(recorded=recorded), tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
                path = Path(directory) / 'failures.json'
                launch_failures.record({'account': 'claude', 'model': recorded}, 'model_unavailable', now=NOW, path=path)
                entries = launch_failures.active(now=NOW, path=path)
                self.assertIsNotNone(allocator.cooling_reason({'account': 'claude', 'model': row}, entries, self.accounts, now=NOW))


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
        self.assertEqual(unnamed['selected_bundle']['model'], FAMILY_9)

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
    """m7: a row listing its levels held back a sibling that could step, as a fixed level once did."""

    X = {'model': 'gpt-6-astra', 'effort': 'high', 'efforts': ['high'], 'provider': 'codex'}
    Y = {'model': 'gpt-6-astra', 'effort': 'xhigh', 'provider': 'codex'}

    def test_a_row_with_no_lower_level_does_not_hold_back_its_siblings(self):
        for label, own in (('only its level', ['high']), ('one above', ['high', 'xhigh'])):
            with self.subTest(label):
                fleet = self.only('codex', x={**self.X, 'efforts': own}, y=self.Y)
                plan = self.plan(self.task(LIVE), fleet)
                self.assertEqual((plan['choice'], plan['selected_bundle']['effort']), ('y', 'medium'))
                self.assertEqual(dispatch_registry.resolve_bundle(plan['selected_bundle'], fleet, False, True)['effort'], 'medium')

    def test_a_row_with_a_lower_level_still_answers_for_the_model(self):
        fleet = self.only('codex', x={**self.X, 'efforts': ['medium', 'high']}, y=self.Y)
        plan = self.plan(self.task(LIVE), fleet)
        self.assertEqual((plan['choice'], plan['selected_bundle']['effort']), ('x', 'medium'))

    def test_a_named_level_does_not_depend_on_the_order_of_the_rows(self):
        for constraints, level in (({'model': 'gpt-6-astra'}, 'low'), ({'model': 'gpt-6-astra', 'effort': 'medium'}, 'medium')):
            for order in (('x', 'y'), ('y', 'x')):
                with self.subTest(constraints=constraints, order=order):
                    fleet = self.only('codex', **{name: {'x': self.X, 'y': self.Y}[name] for name in order})
                    plan = self.plan(self.task(LIVE, constraints=constraints), fleet)
                    self.assertEqual(plan['choice'], f'y@{level}')
                    self.assertEqual(dispatch_registry.resolve_bundle(plan['selected_bundle'], fleet, False, True)['effort'], level)


if __name__ == '__main__':
    unittest.main()
