"""Public/gateway launch names score consistently without changing launch IDs."""
import unittest

import account_fleet
import auto_bundles
import axis_scores
from model_resolution import family_patterns, resolve_canonical_model

from router_test_support import isolated_router_state


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup)


class ModelResolutionTests(unittest.TestCase):
    def setUp(self):
        self.accounts = account_fleet.load_accounts(account_fleet.reference_fleet_path())

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
                self.assertEqual(account_fleet.canonical(raw, self.accounts), expected)
                resolved = account_fleet.resolve_model(raw, self.accounts)
                spec = account_fleet.model_spec(self.accounts, raw)
                if resolved['source'] == 'family':
                    # A family name carries no row of its own: its scores come from the series conversion in the shipped
                    # five-axis data, never from a copied or default spec.
                    self.assertEqual(spec, {})
                else:
                    self.assertEqual(spec, self.accounts['models'][expected])

    def test_exact_custom_models_aliases_and_existing_calibrations_win(self):
        for name, spec in self.accounts['models'].items():
            if isinstance(spec, dict):
                self.assertEqual(account_fleet.canonical(name, self.accounts), name)
                self.assertIs(account_fleet.model_spec(self.accounts, name), spec)
        for alias, target in self.accounts['aliases'].items():
            self.assertEqual(account_fleet.canonical(alias, self.accounts), target)
        self.accounts['models']['gpt-5-custom'] = {'fast_capable': False}
        self.accounts['aliases']['openai/gpt-5-custom@20251001'] = 'gpt-6.1-sol'
        self.assertEqual(account_fleet.canonical('gpt-5-custom', self.accounts), 'gpt-5-custom')
        self.assertEqual(account_fleet.canonical('openai/gpt-5-custom@20251001', self.accounts), 'gpt-6.1-sol')
        self.assertEqual(account_fleet.canonical('vendor/gpt-5-custom-latest', self.accounts), 'gpt-5-custom')
        # Alias cycles and metadata strings must not become model specifications.
        self.assertIsNone(resolve_canonical_model('loop', {}, {'loop': 'other', 'other': 'loop'}, []))
        self.assertIsNone(resolve_canonical_model('basis', self.accounts['models'], {}, family_patterns(self.accounts)))

    def test_a_document_without_a_families_table_is_a_named_error_not_an_empty_table(self):
        doc = {key: value for key, value in self.accounts.items() if key != 'families'}
        for call in (lambda: account_fleet.resolve_model('claude-opus-6-0', doc),
                     lambda: account_fleet.canonical('claude-opus-6-0', doc),
                     lambda: auto_bundles.series_target('claude-opus-6-0', doc)):
            with self.subTest(call=call), self.assertRaisesRegex(ValueError, 'no families table'):
                call()

    def test_unknown_models_have_no_row_score_or_price(self):
        self.assertEqual(account_fleet.resolve_model('new-vendor/unknown', self.accounts)['source'], 'unknown')
        self.assertEqual(account_fleet.model_spec(self.accounts, 'new-vendor/unknown'), {})
        self.assertNotIn('unmeasured_models', self.accounts)

    def test_obsolete_defaults_are_removed_and_catalog_gaps_stay_explicit(self):
        obsolete = {
            'claude-3-7-sonnet', 'gpt-4.1', 'gpt-4.1-mini', 'o3', 'o3-pro',
            'o4-mini', 'gemini-2.5-pro', 'gemini-2.5-flash', 'grok-3',
            'grok-3-mini', 'deepseek-r1', 'deepseek-v3', 'qwen3-coder',
            'qwen3-235b-a22b', 'kimi-k2', 'glm-4.6', 'minimax-m2',
            'doubao-seed-1.6', 'llama-4-maverick',
        }
        self.assertNotIn('effort_curves', self.accounts)
        for name in obsolete:
            with self.subTest(name=name):
                self.assertNotIn(name, self.accounts['models'])
                self.assertNotIn(name, self.accounts['aliases'])
                self.assertNotIn(name, self.accounts['aliases'].values())
                for rows in self.accounts['dispatch_efforts'].values():
                    self.assertNotIn(name, rows)
        # Catalog did not advertise these speculative launch IDs.
        for name in ['deepseek-r2', 'deepseek-v4.1', 'gemini-3.8-pro',
                     'grok-4.7-mini', 'grok-4.7-fast', 'llama-5']:
            self.assertNotIn(name, self.accounts['models'])
        for name in ['llama-future', 'gpt-4-future', 'unknown-vendor']:
            self.assertIsNone(resolve_canonical_model(name, self.accounts['models'], self.accounts['aliases'],
                                                      family_patterns(self.accounts)))

    def test_current_frontier_tiers_and_context_windows(self):
        shipped = axis_scores.load_five_axis()['models']
        self.assertEqual(shipped['claude-opus-5-5']['tier']['value'], 0.0)
        sentinels = {
            'claude-opus-5-5': 'arena_overall',
            'gemini-3.8-flash-high': 'arena_overall',
            # Listed only at xHigh/Max on arena.ai Overall, so R2-F2 @High-only tier reads aa.index at High:
            'claude-sonnet-5-5': 'other_overall',
            'gpt-6-astra': 'other_overall',
            'gpt-6.1-sol': 'other_overall',
            'grok-4.7': 'other_overall',
        }
        for name, expected_method in sentinels.items():
            self.assertEqual(shipped[name]['tier']['method'], expected_method, name)
            self.assertIsInstance(shipped[name]['tier']['value'], (int, float), name)
        # Distinct Arena Overall @High models never collapse to the old 3.675 PAV anchor plateau or 'anchor' method
        self.assertEqual(len({shipped[n]['tier']['value'] for n in sentinels}), len(sentinels))
        for name, row in shipped.items():
            self.assertNotEqual((row.get('tier') or {}).get('method'), 'anchor', name)
            self.assertNotEqual((row.get('tier') or {}).get('value'), 3.675, name)
        for name in self.accounts['models']:
            if isinstance(self.accounts['models'][name], dict):
                self.assertIn(name, shipped, name)
        self.assertEqual(self.accounts['models']['gpt-6.1-terra']['context_window'], 500000)


if __name__ == '__main__':
    unittest.main()
