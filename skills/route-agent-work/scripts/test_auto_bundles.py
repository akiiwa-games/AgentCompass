import json
from pathlib import Path
import unittest

import auto_bundles as ab
import allocator

REAL = json.loads((Path(__file__).resolve().parents[1] / 'references/accounts.json').read_text(encoding='utf-8-sig'))
NOW = 2_000_000_000.0


def proxy_accounts():
    acc = {'models': REAL['models'], 'aliases': REAL.get('aliases', {}), 'accounts': {
        'llmproxy': {'billing': 'metered', 'backend_kind': 'litellm', 'family': 'codex',
                     'windows': {'spend': {'hours': 168}}, 'bundles': {}}}}
    return acc


def catalog(provider, ids, ok=True, checked=NOW):
    return {'providers': {provider: {'ok': ok, 'type': 'openai_compatible', 'checked_at': checked,
                                     'models': [{'id': i} for i in ids]}}}


class DefaultProfile(unittest.TestCase):
    def test_calibrated(self):
        p = ab.default_profile('claude-opus-5-5-high', REAL)
        self.assertEqual((p['source'], p['model']), ('calibrated', 'claude-opus-5-5'))

    def test_family_is_shrunk(self):
        p = ab.default_profile('qwen3.7-max', REAL)
        self.assertEqual(p['source'], 'family')
        self.assertLess(p['spec']['tier'], REAL['models']['qwen3.8-max']['tier'])

    def test_small_name_on_big_target(self):
        p = ab.default_profile('qwen-flash', REAL)
        self.assertEqual(p['spec']['tier'], ab.SMALL_DEFAULT['tier'])

    def test_generic(self):
        self.assertEqual(ab.default_profile('zzz-unknown-9', REAL)['source'], 'generic')

    def test_split_effort(self):
        self.assertEqual(ab.split_effort('claude-opus-5-5-xhigh'), ('claude-opus-5-5', 'xhigh'))
        self.assertEqual(ab.split_effort('gpt-6.1-sol'), ('gpt-6.1-sol', None))


class Derive(unittest.TestCase):
    def test_proxy_metered_default_off_and_non_chat_excluded(self):
        out = ab.derive(proxy_accounts(), catalog('llmproxy', ['gpt-6.1-sol', 'kimi-k3', 'gemini-embedding-001', 'qwen-flash']), NOW)
        rows = out['bundles']['llmproxy']
        self.assertTrue(all(b['disabled'] and b['billing'] == 'metered' for b in rows.values()))
        self.assertIn('auto:llmproxy:codex:gpt-6.1-sol@high', rows)
        self.assertEqual(rows['auto:llmproxy:codex:kimi-k3.1-pro@high']['compatibility'] if 'auto:llmproxy:codex:kimi-k3.1-pro@high' in rows
                         else rows['auto:llmproxy:codex:kimi-k3@high']['compatibility'], 'unverified')
        self.assertEqual([e['model'] for e in out['excluded']], ['gemini-embedding-001'])
        self.assertTrue(rows['auto:llmproxy:codex:qwen-flash@high']['cost_estimated'])
        self.assertEqual(rows['auto:llmproxy:codex:qwen-flash@high']['draws'], ['spend'])

    def test_user_override_enables(self):
        prefs = {'accounts': {'llmproxy': {'bundles': {'auto:llmproxy:codex:gpt-6.1-sol@high': {'disabled': False}}}}}
        out = ab.derive(proxy_accounts(), catalog('llmproxy', ['gpt-6.1-sol']), NOW, overrides=prefs)
        self.assertFalse(out['bundles']['llmproxy']['auto:llmproxy:codex:gpt-6.1-sol@high']['disabled'])

    def test_agy_manual_wins_and_subscription_enabled(self):
        agy = REAL['accounts']['antigravity']
        acc = {'models': REAL['models'], 'aliases': REAL.get('aliases', {}), 'accounts': {'antigravity': agy}}
        provider = next(iter(agy['bundles'].values())).get('provider', 'antigravity')
        ids = ['claude-opus-5-5-low', 'claude-opus-5-5-high', 'claude-opus-5-5-xhigh', 'gemini-4-argon-high']
        out = ab.derive(acc, catalog(provider, ids), NOW)
        rows = out['bundles'].get('antigravity', {})
        self.assertEqual(list(rows), ['auto:antigravity:antigravity-resilient:claude-opus-5-5@xhigh'])
        bundle = rows['auto:antigravity:antigravity-resilient:claude-opus-5-5@xhigh']
        self.assertTrue(bundle['disabled'])  # manual opus bundles cover the family: extra effort waits for opt-in
        self.assertEqual(bundle['draws'], agy['bundles']['agy_opus55_high']['draws'])

    def test_stale_then_expired(self):
        acc = proxy_accounts()
        bid = 'auto:llmproxy:codex:gpt-6.1-sol@high'
        failed = catalog('llmproxy', ['gpt-6.1-sol'], ok=False, checked=NOW - 3600)
        self.assertEqual(ab.derive(acc, failed, NOW, seen={bid: NOW - 3600})['bundles']['llmproxy'][bid]['availability'], 'stale')
        old = ab.derive(acc, failed, NOW, seen={bid: NOW - 8 * 86400})['bundles']['llmproxy'][bid]
        self.assertEqual(old['availability'], 'expired')
        self.assertTrue(old['disabled'])

    def test_legacy_haiku_default_off(self):
        spec = REAL['accounts']['claude']
        acc = {'models': REAL['models'], 'aliases': REAL.get('aliases', {}), 'accounts': {'claude': spec}}
        provider = next(iter(spec['bundles'].values())).get('provider', 'claude')
        rows = ab.derive(acc, catalog(provider, ['claude-haiku-4-5', 'claude-haiku-5-5']), NOW)['bundles'].get('claude', {})
        state = {b['model']: b['disabled'] for b in rows.values()}
        self.assertTrue(state.get('claude-haiku-4-5', True))
        if 'claude-haiku-5-5' in state:
            self.assertFalse(state['claude-haiku-5-5'])

    def test_merge_feeds_allocator(self):
        out = ab.derive(proxy_accounts(), catalog('llmproxy', ['zzz-unknown-9']), NOW)
        merged = ab.merge_into(proxy_accounts(), out)
        flat = allocator.bundles(merged)
        bid = 'auto:llmproxy:codex:zzz-unknown-9@high'
        self.assertIn(bid, flat)
        self.assertEqual(allocator.bundle_pools(flat[bid], merged), ['spend'])
        self.assertIn('zzz-unknown-9', merged['models'])


class Migration(unittest.TestCase):
    def test_old_choice_maps_and_explicit_new_wins(self):
        acc = proxy_accounts()
        out = ab.derive(acc, catalog('llmproxy', ['gpt-5.6-sol', 'kimi-k3']), NOW)
        aliases = ab.legacy_aliases(acc, out)
        self.assertEqual(aliases['llmproxy__gpt_5_6_sol_high'], 'auto:llmproxy:codex:gpt-5.6-sol@high')
        prefs = {'accounts': {'llmproxy': {'bundles': {
            'llmproxy__gpt_5_6_sol_high': {'disabled': False},
            'llmproxy__kimi_k3_high': {'disabled': False},
            'auto:llmproxy:codex:kimi-k3@high': {'disabled': True}}}}}
        migrated = ab.migrate_overrides(prefs, aliases)
        out2 = ab.derive(acc, catalog('llmproxy', ['gpt-5.6-sol', 'kimi-k3']), NOW, overrides=migrated)
        rows = out2['bundles']['llmproxy']
        self.assertFalse(rows['auto:llmproxy:codex:gpt-5.6-sol@high']['disabled'])
        self.assertTrue(rows['auto:llmproxy:codex:kimi-k3@high']['disabled'])
        self.assertIn('llmproxy__gpt_5_6_sol_high', prefs['accounts']['llmproxy']['bundles'])  # input untouched

    def test_ledger_continuity(self):
        aliases = {'old': 'auto:x'}
        self.assertEqual(ab.resolve_bundle_id('old', {'auto:x'}, aliases), 'auto:x')
        self.assertIsNone(ab.resolve_bundle_id('gone', {'auto:x'}, aliases))
        self.assertEqual(ab.resolve_bundle_id('auto:x', {'auto:x'}, aliases), 'auto:x')

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
    def test_accounts_override_maps_legacy_proxy_choice(self):
        import accounts_override
        acc = proxy_accounts()
        out = ab.derive(acc, catalog('llmproxy', ['gpt-5.6-sol']), NOW)
        merged = ab.merge_into(acc, out)
        merged['_auto_bundles'] = {'legacy_aliases': ab.legacy_aliases(acc, out)}
        doc = {'accounts': {'llmproxy': {'bundles': {'llmproxy__gpt_5_6_sol_high': {'disabled': False}}}}}
        accounts_override.apply(merged, doc)
        self.assertFalse(merged['accounts']['llmproxy']['bundles']['auto:llmproxy:codex:gpt-5.6-sol@high']['disabled'])
        self.assertEqual(list(doc['accounts']['llmproxy']['bundles']), ['llmproxy__gpt_5_6_sol_high'])

    def test_boolean_disabled_gives_reason_string(self):
        from datetime import datetime, timezone
        merged = allocator.load_accounts()
        owner, bid = next((o, b) for o, s in merged['accounts'].items() for b in (s.get('bundles') or {}))
        merged['accounts'][owner]['bundles'][bid].update(disabled=True, discovery_unavailable=True)
        task = {'work_kind': 'implementation', 'difficulty': 'routine', 'needs': {'local_only': True}}
        row = next(c for c in allocator.plan(task, {'read_ok': True, 'accounts': {}}, datetime.now(timezone.utc),
                                             accounts=merged)['ranking'] if c['id'] == bid)
        self.assertIn('disabled in model settings', row['excluded'])
        self.assertTrue(all(isinstance(e, str) for e in row['excluded']))

    def test_harness_visual_capability(self):
        # File-based visual work (the router's only kind) reaches multimodal models on agy via read_file (probe 2026-10-28).
        self.assertTrue(allocator.harness_accepts_visual('antigravity-resilient', 'claude-opus-5-5'))
        self.assertTrue(allocator.harness_accepts_visual('antigravity-resilient', 'gemini-4-argon-high'))
        self.assertFalse(allocator.harness_accepts_visual('antigravity', 'claude-opus-5-5', needs_attachment=True))
        self.assertTrue(allocator.harness_accepts_visual('claude', 'claude-opus-5-5', needs_attachment=True))
        # Non-multimodal models are excluded by their own model row, not the harness.
        self.assertFalse(allocator.models_table(allocator.load_accounts()).get('gpt-oss-120b', {}).get('visual'))


if __name__ == '__main__':
    unittest.main()

