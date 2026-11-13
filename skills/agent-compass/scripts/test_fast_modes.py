"""Fast contracts across model, harness, billing and launch surfaces; no live calls."""
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from datetime import datetime, timezone

import allocator
import account_fleet
import axis_scores
import candidate_ranking
import dispatch_registry as registry
import fast_modes
import roll
import route_judge
import usage_reading
from router_test_support import isolated_router_state, usage_windows


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup)


class FastModesTests(unittest.TestCase):
    def test_only_an_effective_fast_capable_true_enables_fast(self):
        now = datetime.now(timezone.utc)
        accounts = account_fleet.load_accounts(account_fleet.reference_fleet_path())
        usage = usage_windows(now, {'codex': [('five_hour', 95, 3), ('seven_day', 95, 100)],
                                    'claude': [('five_hour', 95, 3), ('seven_day', 95, 100)]})
        task = dict(work_kind='debug_review', difficulty='routine', expected_hours=0.01,
                    needs={'local_only': True, 'fast': True})
        for provider, model, bid in [('codex', 'gpt-6.1-sol', 'sol_high'),
                                     ('claude', 'claude-opus-5-5', 'opus55_high')]:
            for policy in (False, True, None):
                with self.subTest(provider=provider, fast_capable=policy):
                    info = accounts['models'][model]
                    if policy is None:
                        info.pop('fast_capable', None)
                    else:
                        info['fast_capable'] = policy
                    for forced in (False, True):
                        ranking = allocator.plan(dict(task, force_fast=forced), usage, now, accounts=accounts)['ranking']
                        plain = candidate_ranking.candidate_for_bundle(ranking, bid, False)
                        self.assertFalse(plain['bundle'].get('fast') if plain else False)
                        if forced:
                            # Only Claude Opus has cited fast-mode pricing and speed; Codex models have none, so forcing
                            # fast on them is a data error, never a guessed variant.
                            fast = candidate_ranking.candidate_for_bundle(ranking, bid, True)
                            self.assertEqual(bool(fast and fast['bundle'].get('fast')), provider == 'claude' and policy is True)
                    bundle = dict(account_fleet.bundles(accounts)[bid], fast=True)
                    args = SimpleNamespace(basis='Diagnostic fixture', provider=provider, account=provider,
                        model=model, effort='high', input=None, usage_cache=None, accounts=None,
                        constraint='user_override', specialty=None, fast=True)
                    with patch.object(account_fleet, 'load_accounts', return_value=accounts), \
                            patch.object(usage_reading, 'usage_snapshot', return_value=usage):
                        if policy is not True:
                            with self.assertRaisesRegex(ValueError, 'fast_capable'):
                                route_judge.record_constraint(args)
                            with self.assertRaisesRegex(ValueError, 'fast_capable'):
                                registry.resolve_bundle(bundle, accounts)
                            with patch.object(roll, 'provider_definition', return_value=(provider, {})), \
                                    patch.object(roll.subprocess, 'Popen') as spawn:
                                with self.assertRaisesRegex(ValueError, 'fast_capable'):
                                    roll.cli(SimpleNamespace(wait=0), {}, bundle, 'fixture')
                                spawn.assert_not_called()
                        else:
                            self.assertTrue(route_judge.record_constraint(args)['selected_bundle']['fast'])
                            self.assertTrue(registry.resolve_bundle(bundle, accounts)['fast'])
                    self.assertIsNotNone(registry.resolve_bundle(dict(bundle, fast=False), accounts))

    def test_fast_needs_a_declared_row_on_the_harness_of_its_api_family(self):
        # Rule (fast-modes.md): fast runs only where the row declares fast_capable: true, on its api_family's harness.
        for harness in fast_modes.HARNESSES:
            fast_modes.validate(harness, 'model-a', {'fast_capable': True, 'api_family': harness})
        other = {'codex': 'claude', 'claude': 'codex'}
        for harness, info in [('pi', {'fast_capable': True, 'api_family': 'codex'}),
                              ('antigravity-resilient', {'fast_capable': True, 'api_family': 'claude'}),
                              *((h, {'fast_capable': True, 'api_family': other[h]}) for h in fast_modes.HARNESSES),
                              *((h, {'api_family': h}) for h in fast_modes.HARNESSES),
                              *((h, {'fast_capable': False, 'api_family': h}) for h in fast_modes.HARNESSES),
                              ('claude', {})]:
            with self.subTest(harness=harness, info=info), self.assertRaises(ValueError):
                fast_modes.validate(harness, 'model-a', info)
            self.assertFalse(fast_modes.supported(harness, 'model-a', info))

    def test_a_missing_catalog_row_is_a_named_error(self):
        for check in (fast_modes.supported, fast_modes.validate):
            with self.subTest(check=check.__name__), self.assertRaisesRegex(ValueError, 'needs its catalog row'):
                check('codex', 'model-a', None)

    def test_fast_and_normal_requests_preserve_model_effort_and_no_pi_feature(self):
        for provider, model in [('codex', 'gpt-6-astra'), ('claude', 'claude-opus-5-5')]:
            for fast in (False, True):
                b = dict(provider=provider, model=model, effort='medium', fast=fast)
                before = copy.deepcopy(b)
                request = registry.launch_request(b, dict(title='fixture', initialPrompt='fixture'))
                self.assertEqual(b, before)
                self.assertEqual(request['provider'], provider + '/' + model)
                self.assertEqual(request['settings']['thinkingOptionId'], 'medium')
                expected = {'service_tier': 'priority' if fast else 'default'} if provider == 'codex' else {'fast_mode': fast}
                self.assertEqual(request['settings']['features'], expected)
        pi = registry.launch_request(dict(provider='pi', model='gpt-6-astra', effort='high'),
                                     dict(title='pi', initialPrompt='fixture'))
        self.assertNotIn('features', pi['settings'])
        self.assertNotIn('modeId', pi['settings'])
        with self.assertRaises(ValueError):
            registry.launch_request(dict(provider='pi', model='gpt-6-astra', fast=True),
                                    dict(title='pi', initialPrompt='fixture'))

    def test_cli_session_settings_explicitly_enable_and_disable_without_model_switch(self):
        for fast in (False, True):
            args = roll.CLI_RUNS['claude'][1]('claude-opus-5-5', 'medium', 'C:/fixture', fast)
            self.assertEqual(args[args.index('--model') + 1], 'claude-opus-5-5')
            self.assertEqual(args[args.index('--effort') + 1], 'medium')
            self.assertEqual(json.loads(args[args.index('--settings') + 1]), {'fastMode': fast})
            args = roll.CLI_RUNS['codex'][1]('gpt-6-astra', 'medium', 'C:/fixture', fast)
            self.assertIn('service_tier="priority"' if fast else 'service_tier="default"', args)
            self.assertIn('features.fast_mode=' + str(fast).lower(), args)

    def test_claude_fast_billing_follows_the_account(self):
        self.assertEqual(fast_modes.claude_billing({'billing': 'subscription'}), 'usage_credits')
        self.assertEqual(fast_modes.claude_billing({'billing': 'metered'}), 'api')
        self.assertEqual(fast_modes.claude_billing({}), 'unknown_paid')

    def test_claude_fast_is_its_own_candidate_priced_at_the_cited_multiples(self):
        now = datetime.now(timezone.utc)
        accounts = account_fleet.load_accounts(account_fleet.reference_fleet_path())
        usage = usage_windows(now, {'claude': [('five_hour', 95, 3), ('seven_day', 95, 1)]})
        task = dict(work_kind='debug_review', difficulty='routine', expected_hours=0.01,
                    needs={'local_only': True, 'fast': True})
        shipped = axis_scores.load_five_axis()['models']['claude-opus-5-5']['fast_mode']
        def rows(t, load=accounts):
            return allocator.plan(t, usage, now, accounts=load)['ranking']
        plain = candidate_ranking.candidate_for_bundle(rows(task), 'opus55_high', False)
        self.assertFalse(plain['bundle'].get('fast'))
        paid = candidate_ranking.candidate_for_bundle(rows(dict(task, force_fast=True)), 'opus55_high', True)
        self.assertEqual((paid['id'], paid['bundle']['fast']), ('opus55_high+fast', True))
        self.assertEqual(paid['base_id'], 'opus55_high')
        self.assertGreater(paid['expected_usd'], 0)
        self.assertTrue(any(f"{shipped['price_multiplier']:g}x burn" in n for n in paid['notes']))
        self.assertIsNone(candidate_ranking.candidate_for_bundle(rows(dict(task, force_fast=True, disable_fast=True)), 'opus55_high', True))
        accounts['accounts']['claude']['bundles']['opus55_high']['disabled'] = True
        self.assertTrue(candidate_ranking.candidate_for_bundle(rows(dict(task, force_fast=True)), 'opus55_high', False)['excluded'])
        accounts['accounts']['claude']['bundles']['opus55_high']['disabled'] = False
        usage = usage_windows(now, {'claude': [('five_hour', 0, 3), ('seven_day', 0, 100)]})
        self.assertTrue(candidate_ranking.candidate_for_bundle(rows(dict(task, force_fast=True)), 'opus55_high', False)['excluded'])


if __name__ == '__main__':
    unittest.main()
