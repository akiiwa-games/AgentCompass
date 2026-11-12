import json
from pathlib import Path
import unittest

import auto_bundles as ab
import allocator
import account_fleet
import quota_pools

from router_test_support import isolated_router_state


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup)

REAL = json.loads(account_fleet.reference_fleet_path().read_text(encoding='utf-8-sig'))
NOW = 2_000_000_000.0


def public(accounts):
    return {'models': REAL['models'], 'aliases': REAL.get('aliases', {}), 'superseded': REAL.get('superseded', {}),
            'accounts': accounts}


def proxy_accounts():
    acc = public({
        'llmproxy': {'billing': 'metered', 'backend_kind': 'litellm', 'family': 'codex',
                     'windows': {'spend': {'hours': 168}}, 'bundles': {}}})
    return acc


def catalog(provider, ids, ok=True, checked=NOW):
    return {'providers': {provider: {'ok': ok, 'type': 'openai_compatible', 'checked_at': checked,
                                     'models': [{'id': i} for i in ids]}}}


class DefaultProfile(unittest.TestCase):
    def test_a_name_resolves_to_a_calibrated_row_a_series_or_nothing(self):
        for model, source, canonical, target in (
            ('claude-opus-5-5-high', 'calibrated', 'claude-opus-5-5', 'claude-opus-5-5'),
            ('qwen3.7-max', 'calibrated', 'qwen3.7-max', 'qwen3.7-max'),
            ('qwen3.5-max', 'family', 'qwen3.5-max', 'qwen3.8-max'),
            # Alibaba documents `qwen-flash` as the Qwen3 Flash 2025-07-28 snapshot: an alias to its row
            ('qwen-flash', 'calibrated', 'qwen3-flash-2025-07-28', 'qwen3-flash-2025-07-28'),
            ('deepseek-flash', 'calibrated', 'deepseek-v4.1-flash-free', 'deepseek-v4.1-flash-free'),
            ('zzz-unknown-9', 'unmeasured', 'zzz-unknown-9', None),
        ):
            with self.subTest(model=model):
                profile = ab.default_profile(model, REAL)
                self.assertEqual((profile['source'], profile['model'], profile['target']), (source, canonical, target))
                self.assertNotIn('spec', profile)       # no score row is supplied for a name the data does not carry

    def test_a_name_without_a_row_or_a_series_has_no_scores(self):
        for name in ('opencode/big-pickle', 'pulse-alpha-free'):
            profile = ab.default_profile(name, REAL)
            self.assertEqual((profile['source'], profile['target']), ('unmeasured', None), name)

    def test_split_effort(self):
        self.assertEqual(ab.split_effort('claude-opus-5-5-xhigh'), ('claude-opus-5-5', 'xhigh'))
        self.assertEqual(ab.split_effort('gpt-6.1-sol'), ('gpt-6.1-sol', None))


class Derive(unittest.TestCase):
    def test_proxy_metered_latest_checked_and_non_chat_excluded(self):
        # Checked is not routed: metered stays a last resort in candidate_eval.evaluate_bundle.
        out = ab.derive(proxy_accounts(), catalog('llmproxy', ['gpt-6.1-sol', 'kimi-k3', 'gemini-embedding-001', 'qwen-flash', 'zzz-proxy-model']), NOW)
        rows = out['bundles']['llmproxy']
        self.assertTrue(all(not b['disabled'] and b['billing'] == 'metered' for b in rows.values()))
        self.assertIn('auto:llmproxy:codex:gpt-6.1-sol@high', rows)
        self.assertEqual(rows['auto:llmproxy:codex:kimi-k3.1-pro@high']['compatibility'] if 'auto:llmproxy:codex:kimi-k3.1-pro@high' in rows
                         else rows['auto:llmproxy:codex:kimi-k3@high']['compatibility'], 'unverified')
        self.assertEqual([e['model'] for e in out['excluded']], ['gemini-embedding-001'])
        # `qwen-flash` resolves (alias) to the Qwen3 Flash snapshot row it serves
        self.assertEqual(rows['auto:llmproxy:codex:qwen3-flash-2025-07-28@high']['draws'], ['spend'])
        self.assertEqual(rows['auto:llmproxy:codex:zzz-proxy-model@high']['score_source'], 'unmeasured')

    def test_user_checkbox_overrides_default_both_ways(self):
        latest, old = 'auto:llmproxy:codex:gpt-6.1-sol@high', 'auto:llmproxy:codex:gpt-5.6-sol@high'
        ids = ['gpt-6.1-sol', 'gpt-5.6-sol']
        rows = ab.derive(proxy_accounts(), catalog('llmproxy', ids), NOW)['bundles']['llmproxy']
        self.assertEqual((rows[latest]['disabled'], rows[old]['disabled']), (False, True))
        prefs = {'accounts': {'llmproxy': {'bundles': {latest: {'disabled': True}, old: {'disabled': False}}}}}
        rows = ab.derive(proxy_accounts(), catalog('llmproxy', ids), NOW, overrides=prefs)['bundles']['llmproxy']
        self.assertEqual((rows[latest]['disabled'], rows[old]['disabled']), (True, False))

    def test_extra_effort_of_latest_model_is_checked(self):
        agy = REAL['accounts']['antigravity']
        acc = public({'antigravity': agy})
        provider = next(iter(agy['bundles'].values())).get('provider', 'antigravity')
        ids = ['claude-opus-5-5-low', 'claude-opus-5-5-high', 'claude-opus-5-5-xhigh', 'gemini-4-argon-high']
        out = ab.derive(acc, catalog(provider, ids), NOW)
        rows = out['bundles'].get('antigravity', {})
        self.assertEqual(list(rows), ['auto:antigravity:antigravity-resilient:claude-opus-5-5@xhigh'])
        bundle = rows['auto:antigravity:antigravity-resilient:claude-opus-5-5@xhigh']
        self.assertFalse(bundle['disabled'])  # another effort of the latest model is the same model
        self.assertEqual(bundle['draws'], agy['bundles']['agy_opus55_high']['draws'])

    def test_stale_then_expired(self):
        acc = proxy_accounts()
        bid = 'auto:llmproxy:codex:gpt-6.1-sol@high'
        failed = catalog('llmproxy', ['gpt-6.1-sol'], ok=False, checked=NOW - 3600)
        self.assertEqual(ab.derive(acc, failed, NOW, seen={bid: NOW - 3600})['bundles']['llmproxy'][bid]['availability'], 'stale')
        old = ab.derive(acc, failed, NOW, seen={bid: NOW - 8 * 86400})['bundles']['llmproxy'][bid]
        self.assertEqual(old['availability'], 'expired')
        self.assertTrue(old['disabled'])

    def test_superseded_unchecked_latest_and_unknown_checked(self):
        spec = REAL['accounts']['claude']
        provider = next(iter(spec['bundles'].values())).get('provider', 'claude')
        ids = ['claude-haiku-4-5', 'claude-haiku-5-5', 'claude-opus-4-8', 'claude-opus-9-1', 'claude-sonnet-5-5-xhigh']
        rows = ab.derive(public({'claude': spec}), catalog(provider, ids), NOW)['bundles']['claude']
        state = {b['launch_model']: b['disabled'] for b in rows.values()}
        self.assertEqual(state, {'claude-haiku-4-5': True, 'claude-opus-4-8': True,          # superseded in public data
                                 'claude-haiku-5-5': False, 'claude-sonnet-5-5-xhigh': False,  # latest
                                 'claude-opus-9-1': False})                                  # newer than the data
        self.assertFalse([key for b in rows.values() for key in b if 'reason' in key and 'compatibility' not in key])

    def test_default_comes_from_data_not_names(self):
        spec = REAL['accounts']['claude']
        provider = next(iter(spec['bundles'].values())).get('provider', 'claude')
        without = {**public({'claude': spec}), 'superseded': {}}
        rows = ab.derive(without, catalog(provider, ['claude-haiku-4-5']), NOW)['bundles']['claude']
        self.assertFalse(rows['auto:claude:claude:claude-haiku-4-5@high']['disabled'])
        marked = {**without, 'superseded': {'claude-haiku-5-5': {'by': 'x', 'basis': 'test'}}}
        rows = ab.derive(marked, catalog(provider, ['claude-haiku-5-5']), NOW)['bundles']['claude']
        self.assertTrue(rows['auto:claude:claude:claude-haiku-5-5@high']['disabled'])

    def test_superseded_data_points_at_latest_known_models(self):
        table = json.loads((account_fleet.REFERENCES / 'accounts.json').read_text(encoding='utf-8'))
        known = set(table['models']) | set(table['aliases'])
        for old, entry in table['superseded'].items():
            with self.subTest(old=old):
                self.assertEqual(set(entry), {'by', 'basis'})
                self.assertIn(entry['by'], known)
                self.assertNotIn(entry['by'], table['superseded'])
                self.assertTrue(entry['basis'].strip())
        self.assertEqual(table['superseded'], REAL['superseded'])

    def test_merge_feeds_allocator(self):
        out = ab.derive(proxy_accounts(), catalog('llmproxy', ['zzz-unknown-9']), NOW)
        merged = ab.merge_into(proxy_accounts(), out)
        flat = account_fleet.bundles(merged)
        bid = 'auto:llmproxy:codex:zzz-unknown-9@high'
        self.assertIn(bid, flat)
        self.assertEqual(quota_pools.bundle_pools(flat[bid], merged), ['spend'])
        self.assertNotIn('zzz-unknown-9', merged['models'])     # no row is invented for an unmeasured name


class SeenPersistence(unittest.TestCase):
    def test_seen_roundtrip_and_prune(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'sub/seen.json'
            self.assertEqual(ab.read_seen(path), {})
            ab.write_seen(path, {'a': NOW, 'old': NOW - 40 * 86400}, now=NOW)
            self.assertEqual(ab.read_seen(path), {'a': NOW})
            path.write_text('not json')
            self.assertEqual(ab.read_seen(path), {})


class SharedHooks(unittest.TestCase):
    def test_legacy_disabled_gives_neutral_exclusion(self):
        from datetime import datetime, timezone
        merged = json.loads(json.dumps(REAL))
        owner, bid = next((o, b) for o, s in merged['accounts'].items() for b in (s.get('bundles') or {}))
        merged['accounts'][owner]['bundles'][bid].update(disabled='Legacy explanation', discovery_unavailable=True)
        task = {'work_kind': 'general_implementation', 'difficulty': 'routine', 'needs': {'local_only': True}}
        row = next(c for c in allocator.plan(task, {'read_ok': True, 'accounts': {}}, datetime.now(timezone.utc),
                                             accounts=merged)['ranking'] if c['id'] == bid)
        self.assertIn('unchecked in model management', row['excluded'])
        self.assertIs(merged['accounts'][owner]['bundles'][bid]['disabled'], True)
        self.assertNotIn('Legacy explanation', row['excluded'])
        self.assertTrue(all(isinstance(e, str) for e in row['excluded']))

    def test_harness_visual_capability(self):
        # File-based visual work (the router's only kind) reaches multimodal models on agy via read_file (probe 2026-10-28).
        self.assertTrue(account_fleet.harness_accepts_visual('antigravity-resilient', 'claude-opus-5-5'))
        self.assertTrue(account_fleet.harness_accepts_visual('antigravity-resilient', 'gemini-4-argon-high'))
        self.assertFalse(account_fleet.harness_accepts_visual('antigravity', 'claude-opus-5-5', needs_attachment=True))
        self.assertTrue(account_fleet.harness_accepts_visual('claude', 'claude-opus-5-5', needs_attachment=True))
        # Non-multimodal models are excluded by their own model row, not the harness.
        self.assertFalse(account_fleet.models_table(account_fleet.load_accounts()).get('gpt-oss-120b', {}).get('visual'))


if __name__ == '__main__':
    unittest.main()

