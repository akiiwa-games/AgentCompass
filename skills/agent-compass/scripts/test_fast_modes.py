"""Fast contracts across model, harness, billing and launch surfaces; no live calls."""
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from datetime import datetime, timezone, timedelta

import allocator
import dispatch_registry as registry
import fast_modes
import roll
import route_judge
from router_test_support import isolated_router_state, usage_windows


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup)


class FastModesTests(unittest.TestCase):
    def test_effective_fast_policy_false_wins_but_missing_is_not_disabled(self):
        now = datetime.now(timezone.utc)
        accounts = allocator.load_accounts(allocator.reference_fleet_path())
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
                        row = next(r for r in allocator.plan(dict(task, force_fast=forced), usage, now,
                                   accounts=accounts)['ranking'] if r['id'] == bid)
                        self.assertEqual(bool(row['bundle'].get('fast')),
                                         policy is not False and (provider == 'codex' or forced))
                    bundle = dict(allocator.bundles(accounts)[bid], fast=True)
                    args = SimpleNamespace(basis='Diagnostic fixture', provider=provider, account=provider,
                        model=model, effort='high', input=None, usage_cache=None, accounts=None,
                        constraint='user_override', specialty=None, fast=True)
                    with patch.object(allocator, 'load_accounts', return_value=accounts), \
                            patch.object(route_judge, 'usage_snapshot', return_value=usage):
                        if policy is False:
                            with self.assertRaisesRegex(ValueError, 'fast_capable'):
                                route_judge.record_constraint(args)
                            with self.assertRaisesRegex(ValueError, 'fast_capable'):
                                registry.resolve_bundle(bundle, accounts)
                            with patch.object(roll, 'provider_definition', return_value=(provider, {})), \
                                    patch.object(roll.subprocess, 'Popen') as spawn:
                                with self.assertRaisesRegex(ValueError, 'fast_capable'):
                                    roll.cli(SimpleNamespace(wait=0), {}, bundle, 'fixture', Mock())
                                spawn.assert_not_called()
                        else:
                            self.assertTrue(route_judge.record_constraint(args)['selected_bundle']['fast'])
                            self.assertTrue(registry.resolve_bundle(bundle, accounts)['fast'])
                    self.assertIsNotNone(registry.resolve_bundle(dict(bundle, fast=False), accounts))

    def test_only_verified_model_and_harness_pairs(self):
        for harness, models in fast_modes.MODELS.items():
            for model in models:
                fast_modes.validate(harness, model)
        for harness, model in [('pi', 'gpt-6-astra'), ('codex', 'gpt-6-pro'),
                               ('claude', 'claude-sonnet-5-5'), ('claude', 'claude-opus-4-7'),
                               ('claude', 'claude-haiku-5-5'), ('claude', 'claude-opus-future')]:
            with self.subTest(harness=harness, model=model), self.assertRaises(ValueError):
                fast_modes.validate(harness, model)

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

    def test_codex_public_subscription_rate_and_explicit_calibration(self):
        self.assertEqual(fast_modes.codex_burn_multiplier({}, {'billing': 'subscription'}), 2.5)
        self.assertEqual(fast_modes.codex_burn_multiplier({'fast_burn_multiplier': 2}, {'billing': 'subscription'}), 2)
        self.assertEqual(fast_modes.codex_burn_multiplier({'fast_burn_multiplier': 2.5}, {'billing': 'credits'}), 2)
        self.assertEqual(fast_modes.claude_billing({'billing': 'subscription'}), 'usage_credits')
        self.assertEqual(fast_modes.claude_billing({'billing': 'metered'}), 'api')
        self.assertEqual(fast_modes.claude_billing({}), 'unknown_paid')

    def test_claude_paid_fast_is_explicit_and_never_a_subscription_reservation(self):
        now = datetime.now(timezone.utc)
        accounts = allocator.load_accounts(allocator.reference_fleet_path())
        usage = usage_windows(now, {'claude': [('five_hour', 95, 3), ('seven_day', 95, 1)]})
        task = dict(work_kind='debug_review', difficulty='routine', expected_hours=0.01,
                    needs={'local_only': True, 'fast': True})
        def opus(t):
            return next(r for r in allocator.plan(t, usage, now, accounts=accounts)['ranking'] if r['id'] == 'opus55_high')
        normal = opus(task)
        self.assertFalse(normal['bundle'].get('fast'))  # --fast preference/reset never buys Claude credits
        paid = opus(dict(task, force_fast=True))
        self.assertTrue(paid['bundle']['fast'])
        self.assertEqual(paid['bundle']['fast_billing'], 'usage_credits')
        self.assertIsNone(paid['expected_usd'])
        self.assertIsNone(paid['expected_pct'])
        self.assertIsNone(paid['capacity'])
        self.assertNotIn('reset_bonus', paid['components'])
        self.assertNotIn('fast_bonus', paid['components'])
        disabled = opus(dict(task, force_fast=True, disable_fast=True))
        self.assertFalse(disabled['bundle'].get('fast'))
        accounts['accounts']['claude']['bundles']['opus55_high']['disabled'] = True
        self.assertTrue(opus(dict(task, force_fast=True))['excluded'])
        accounts['accounts']['claude']['bundles']['opus55_high']['disabled'] = False
        usage = usage_windows(now, {'claude': [('five_hour', 0, 3), ('seven_day', 0, 100)]})
        self.assertTrue(opus(dict(task, force_fast=True))['excluded'])
        record = dict(stage='dispatch', dispatch_id='paid', state='started',
                      lease_until=(now + timedelta(minutes=5)).isoformat(), bundle=paid['bundle'], expected_usd=None)
        self.assertEqual(allocator.inflight_reserved_usd('claude', accounts['accounts']['claude'], 'seven_day',
                         {}, {}, 100, [record], accounts, accounts['scoring'], now), 0)


if __name__ == '__main__':
    unittest.main()
