"""Public/gateway launch names score consistently without changing launch IDs."""
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch

import allocator
import auto_bundles
from model_resolution import resolve_canonical_model

from router_test_support import isolated_router_state


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup)


class ModelResolutionTests(unittest.TestCase):
    def setUp(self):
        self.accounts = allocator.load_accounts(allocator.reference_fleet_path())

    def test_decorated_names_aliases_and_family_defaults(self):
        cases = {
            'anthropic/claude-haiku-4-5@20251001': 'claude-haiku-4-5',
            'anthropic/claude-opus-4-6-20260205': 'claude-opus-4-6',
            'claude-sonnet-4-6-latest': 'claude-sonnet-4-6',
            'vendor/claude-opus-future': 'claude-opus-5-5',
            'claude-sonnet-future': 'claude-sonnet-5-5',
            'claude-haiku-future': 'claude-haiku-5-5',
            'claude-fable-future': 'claude-fable-5-2',
            'fable': 'claude-fable-5-2',
            'claude-fable-5': 'claude-fable-5-2',
            'claude-fable-5-2:max': 'claude-fable-5-2',
            'claude-fable-5-1': 'claude-fable-5-1',
            'anthropic/claude-fable-5-1': 'claude-fable-5-1',
            'openai/gpt-6.1-terra:latest': 'gpt-6.1-terra',
            'gpt-6-future': 'gpt-6-astra',
            'gpt-6-future-terra': 'gpt-6.1-terra',
            'gpt-6-future-sol': 'gpt-6.1-sol',
            'gpt-6-future-luna': 'gpt-6-luna',
            'gpt-6-future-pro': 'gpt-6-pro',
            'gpt-6-sol': 'gpt-6-sol',
            'gpt-5.6-sol': 'gpt-5.6-sol',
            'gpt-5.6-terra': 'gpt-5.6-terra',
            'gpt-5.6-luna': 'gpt-5.6-luna',
            'google/gemini-4-pro-preview': 'gemini-4-argon-high',
            'google/gemini-future-pro': 'gemini-4-argon-high',
            'gemini-future-flash': 'gemini-3.8-flash-high',
            'xai/grok-4:free': 'grok-4.7',
            'grok-4.7-latest': 'grok-4.7',
            'grok-future-fast': 'grok-4.7-build-fast',
            'grok-future': 'grok-4.7',
            'deepseek-r-future': 'deepseek-v4-pro',
            'deepseek-v-future': 'deepseek-v4-pro',
            'deepseek-future-flash': 'deepseek-v4.1-flash-free',
            'opencode/deepseek-v4.1-flash-free': 'deepseek-v4.1-flash-free',
            'qwen/Qwen3-Coder-Next:free': 'qwen3-coder-next',
            'qwen-future-coder': 'qwen3-coder-next',
            'qwen-future': 'qwen3.8-max',
            'kimi-future': 'kimi-k3.1-pro',
            'moonshot-future': 'kimi-k3.1-pro',
            'glm-future': 'glm-5.3',
            'opencode/glm-5.3-flash-free': 'glm-5.3-flash-free',
            'minimax-future': 'minimax-m3',
            'minimax-future-flash': 'minimax-m3.1-flash',
            'MiniMax-M3.1-Flash': 'minimax-m3.1-flash',
            'doubao-future': 'doubao-seed-2-1-pro-260628',
            'xiaomi/mimo-future': 'mimo-v2.6-pro',
            'opencode/mimo-v2.6-flash-free': 'mimo-v2.6-flash-free',
            'nvidia/nemotron-future-ultra': 'nemotron-3-ultra-free',
            'nemotron-future-super-vl': 'nemotron-3.5-super-vl-free',
            'nemotron-future-lightning': 'nemotron-3.5-lightning-free',
            'muse-future': 'muse-wonder-1.0',
            'mistral-future': 'mistral-large-4',
            'codestral-future': 'mistral-large-4',
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(allocator.canonical(raw, self.accounts), expected)
                resolved = allocator.resolve_model(raw, self.accounts)
                row = self.accounts['models'][expected]
                spec = allocator.model_spec(self.accounts, raw)
                if resolved['source'] == 'family':
                    # Round 058 round 4: a family name takes the series conversion of its same-series row (its scores
                    # and usd_per_hour), marked estimated; a small-size name on a non-small row is not in its series.
                    self.assertTrue(spec['score_estimated'])
                    if auto_bundles.series_target(raw, self.accounts):
                        for field in ('tier', 'capabilities', 'subjective'):
                            self.assertEqual(spec.get(field), row.get(field), field)
                        if isinstance(row.get('usd_per_hour'), (int, float)):
                            self.assertEqual(spec['usd_per_hour'], row['usd_per_hour'])
                            self.assertNotIn('usd_estimated', spec)
                        else:                   # the series has no price either (gpt-6-pro is a web bridge)
                            self.assertTrue(spec['usd_estimated'])
                    else:
                        self.assertEqual(spec['capabilities'], auto_bundles.small_model_spec(self.accounts)['capabilities'])
                else:
                    self.assertEqual(spec, row)

    def test_exact_custom_models_aliases_and_existing_calibrations_win(self):
        for name, spec in self.accounts['models'].items():
            if isinstance(spec, dict):
                self.assertEqual(allocator.canonical(name, self.accounts), name)
                self.assertIs(allocator.model_spec(self.accounts, name), spec)
        for alias, target in self.accounts['aliases'].items():
            self.assertEqual(allocator.canonical(alias, self.accounts), target)
        self.accounts['models']['gpt-5-custom'] = {'tier': 4.9}
        self.accounts['aliases']['openai/gpt-5-custom@20251001'] = 'gpt-6.1-sol'
        self.assertEqual(allocator.canonical('gpt-5-custom', self.accounts), 'gpt-5-custom')
        self.assertEqual(allocator.canonical('openai/gpt-5-custom@20251001', self.accounts), 'gpt-6.1-sol')
        self.assertEqual(allocator.canonical('vendor/gpt-5-custom-latest', self.accounts), 'gpt-5-custom')
        # Alias cycles and metadata strings must not become model specifications.
        self.assertIsNone(resolve_canonical_model('loop', {}, {'loop': 'other', 'other': 'loop'}))
        self.assertIsNone(resolve_canonical_model('basis', self.accounts['models'], {}))

    def test_unknown_models_use_independent_baseline_and_can_allocate(self):
        spec = allocator.model_spec(self.accounts, 'new-vendor/unknown')
        self.assertEqual((spec['tier'], spec['usd_per_hour'], spec['visual']),
                         (self.accounts['unmeasured_models']['generic']['tier'], 1.5, False))
        self.assertLess(spec['tier'], self.accounts['scoring']['ability_floor']['hard'])
        spec['capabilities']['coding'] = 0
        self.assertGreater(allocator.model_spec(self.accounts, 'another')['capabilities']['coding'], 0)
        self.accounts['accounts'] = {'custom': {'billing': 'subscription', 'verified': True, 'unmetered': True, 'windows': {},
            'bundles': {'unknown_high': {'model': 'new-vendor/unknown', 'provider': 'custom', 'effort': 'high'}}}}
        with patch.object(allocator.host_policy, 'load_policy', return_value=None):
            result = allocator.plan({'work_kind': 'web_research', 'difficulty': 'routine', 'expected_hours': .1},
                                    {'accounts': {}}, datetime(2026, 10, 27, tzinfo=timezone.utc),
                                    accounts=self.accounts, records=[], overrides=[])
        row = result['ranking'][0]
        self.assertEqual(row['id'], 'unknown_high')
        self.assertEqual(row['excluded'], [])
        self.assertGreater(row['score'], 0)

    def test_mainstream_defaults_have_complete_bounded_vectors_and_matching_reference(self):
        defaults = allocator.load_capabilities(accounts=self.accounts)['mainstream_defaults']['models']
        scored = {k: v for k, v in self.accounts['models'].items() if isinstance(v, dict)}
        self.assertEqual(set(defaults), set(scored))
        for name, row in defaults.items():
            with self.subTest(name=name):
                self.assertEqual(row['capabilities'], self.accounts['models'][name]['capabilities'])
                self.assertEqual(row['subjective'], self.accounts['models'][name]['subjective'])
                self.assertTrue(all(0 <= x <= 1 for x in [*row['capabilities'].values(), *row['subjective'].values()]))
                self.assertEqual(set(row['capabilities']), set(allocator.CAPABILITY_DIMENSIONS))
                self.assertEqual(row['tier'], scored[name]['tier'])
                self.assertEqual(row.get('usd_per_hour'), scored[name].get('usd_per_hour'))
                if 'usd_per_hour' in row:
                    self.assertGreaterEqual(row['usd_per_hour'], 0)
        self.assertEqual(self.accounts['models']['grok-4.7']['usd_per_hour'], 1.5)

    def test_obsolete_defaults_are_removed_and_catalog_gaps_stay_explicit(self):
        obsolete = {
            'claude-3-7-sonnet', 'gpt-4.1', 'gpt-4.1-mini', 'o3', 'o3-pro',
            'o4-mini', 'gemini-2.5-pro', 'gemini-2.5-flash', 'grok-3',
            'grok-3-mini', 'deepseek-r1', 'deepseek-v3', 'qwen3-coder',
            'qwen3-235b-a22b', 'kimi-k2', 'glm-4.6', 'minimax-m2',
            'doubao-seed-1.6', 'llama-4-maverick',
        }
        for name in obsolete:
            with self.subTest(name=name):
                self.assertNotIn(name, self.accounts['models'])
                self.assertNotIn(name, self.accounts['effort_curves'])
                self.assertNotIn(name, self.accounts['aliases'])
                self.assertNotIn(name, self.accounts['aliases'].values())
                for rows in self.accounts['dispatch_efforts'].values():
                    self.assertNotIn(name, rows)
        # Catalog did not advertise these speculative launch IDs.
        for name in ['deepseek-r2', 'deepseek-v4.1', 'gemini-3.8-pro',
                     'grok-4.7-mini', 'grok-4.7-fast', 'llama-5']:
            self.assertNotIn(name, self.accounts['models'])
        for name in ['llama-future', 'gpt-4-future', 'unknown-vendor']:
            self.assertIsNone(resolve_canonical_model(name, self.accounts['models'], self.accounts['aliases']))

    def test_current_frontier_tiers_and_modality_contracts(self):
        for name, tier in {
            # Fable 5.1, Haiku 5.5, Terra and Luna are measured on LMArena and moved to High with the AA effort curves
            # (calibration/apply_rows.py); the six anchors are the user's.
            'gpt-6-pro': 5.0, 'claude-opus-5-5': 4.6, 'claude-fable-5-1': 4.0,
            'claude-sonnet-5-5': 3.9, 'claude-haiku-5-5': 2.94,
            'gpt-6-astra': 4.2, 'gpt-6.1-sol': 3.6, 'gpt-6-sol': 3.48, 'gpt-6.1-terra': 3.2,
            'gpt-6-luna': 2.61, 'gemini-4-argon-high': 4.0,
            'gemini-3.8-flash-high': 3.0, 'grok-4.7': 3.4,
            'grok-4.7-build-fast': 3.4, 'deepseek-v4-pro': 2.86,
            'deepseek-v4.1-flash-free': 2.8, 'glm-5.3': 3.08,
            'glm-5.3-flash-free': 2.85,
        }.items():
            self.assertEqual(self.accounts['models'][name]['tier'], tier, name)
        for name in ['grok-4.7-build-fast', 'deepseek-v4-pro', 'qwen3-coder-next',
                     'nemotron-3-ultra-free', 'nemotron-3.5-lightning-free']:
            self.assertFalse(self.accounts['models'][name]['visual'], name)
            self.assertEqual(self.accounts['models'][name]['capabilities']['gui'], 0)
        self.assertEqual(self.accounts['models']['gpt-6.1-terra']['context_window'], 500000)
        self.assertGreater(self.accounts['models']['nemotron-3.5-super-vl-free']['capabilities']['visual'], 0)


if __name__ == '__main__':
    unittest.main()
