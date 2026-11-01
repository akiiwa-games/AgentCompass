"""Where the usage cache lives and how readers combine its copies (usage_cache.py)."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import usage_cache as cache


def stamp(seconds_ago):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def row(used, seconds_ago=None, fetched=None):
    return {'fetched_at': fetched if fetched is not None else stamp(seconds_ago), 'windows': [{'id': 'usage', 'used_pct': used}]}


class CacheBase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.env = {}

    def write(self, path, providers, **extra):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'providers': providers, **extra}), encoding='utf-8')
        return path

    def canonical(self, providers, **extra):
        return self.write(cache.canonical_path(self.home, self.env), providers, **extra)

    def legacy(self, providers, **extra):
        return self.write(cache.legacy_path(self.home), providers, **extra)

    def used(self, name, merged):
        return merged['providers'][name]['windows'][0]['used_pct']


class CacheTests(CacheBase):
    def test_default_locations_are_the_state_dir_cache_then_the_legacy_copy(self):
        self.assertEqual(cache.read_paths(None, self.home, self.env),
                         [self.home / '.agent-compass' / 'usage-cache.json', self.home / '.usage-float' / 'usage-cache.json'])
        self.assertEqual(cache.write_path(self.home, self.env), self.home / '.agent-compass' / 'usage-cache.json')

    def test_state_dir_variable_moves_the_canonical_file_only(self):
        self.env['ROUTER_STATE_DIR'] = str(self.home / 'elsewhere')
        self.assertEqual(cache.read_paths(None, self.home, self.env),
                         [self.home / 'elsewhere' / 'usage-cache.json', self.home / '.usage-float' / 'usage-cache.json'])

    def test_a_named_file_is_the_only_one_read_or_written(self):
        named = self.home / 'named.json'
        self.env['ROUTER_USAGE_CACHE'] = str(named)
        self.assertEqual(cache.read_paths(None, self.home, self.env), [named])
        self.assertEqual(cache.write_path(self.home, self.env), named)
        self.assertEqual(cache.read_paths(self.home / 'explicit.json', self.home, self.env), [self.home / 'explicit.json'])

    def test_a_missing_named_file_does_not_fall_back(self):
        self.canonical({'codex': row(1, 5)})
        self.legacy({'codex': row(2, 5)})
        self.env['ROUTER_USAGE_CACHE'] = str(self.home / 'missing.json')
        merged, origin, attempts = cache.read(None, self.home, self.env)
        self.assertIsNone(merged)
        self.assertEqual([a['path'] for a in attempts], [str(self.home / 'missing.json')])
        self.assertEqual([a['status'] for a in attempts], ['missing'])

    def test_core_cache_without_usage_float_reports_missing_legacy_not_corruption(self):
        self.canonical({'codex': row(1, 5)})
        merged, _, attempts = cache.read(None, self.home, self.env)
        self.assertEqual(self.used('codex', merged), 1)
        self.assertEqual(attempts, [{'path': str(cache.legacy_path(self.home)), 'status': 'missing'}])

    def test_the_newest_reading_per_provider_wins_and_the_canonical_file_wins_ties(self):
        stale_canonical = self.canonical({'codex': row(10, 600), 'claude': row(20, 5), 'tie': row(1, fetched=stamp(100))})
        self.legacy({'codex': row(90, 30), 'claude': row(95, 900), 'grok': row(5, 50), 'tie': row(2, fetched=json.loads(stale_canonical.read_text())['providers']['tie']['fetched_at'])})
        merged, origin, attempts = cache.read(None, self.home, self.env)
        self.assertEqual(self.used('codex', merged), 90)       # the legacy reading is newer
        self.assertEqual(self.used('claude', merged), 20)      # an older legacy reading does not replace it
        self.assertEqual(self.used('grok', merged), 5)         # only the legacy file has it
        self.assertEqual(self.used('tie', merged), 1)          # equal stamps: canonical
        self.assertEqual(origin['codex'], str(cache.legacy_path(self.home)))
        self.assertEqual(origin['claude'], str(cache.canonical_path(self.home, self.env)))
        self.assertEqual(attempts, [])

    def test_a_row_without_a_readable_stamp_never_replaces_one_with_a_stamp(self):
        self.canonical({'codex': row(10, 600)})
        self.legacy({'codex': row(90, fetched='not a time')})
        merged, _, _ = cache.read(None, self.home, self.env)
        self.assertEqual(self.used('codex', merged), 10)

    def test_poll_cadence_follows_the_file_that_supplied_the_reading(self):
        self.canonical({'codex': row(10, 600)}, poll={'codex': {'seconds': 1800, 'at': 1}})
        self.legacy({'codex': row(90, 30)}, poll={'codex': {'seconds': 300, 'at': 2}})
        merged, _, _ = cache.read(None, self.home, self.env)
        self.assertEqual(merged['poll'], {'codex': {'seconds': 300, 'at': 2}})

    def test_an_unreadable_file_is_reported_and_the_other_one_is_used(self):
        cache.canonical_path(self.home, self.env).parent.mkdir(parents=True)
        cache.canonical_path(self.home, self.env).write_text('{broken', encoding='utf-8')
        self.legacy({'codex': row(7, 5)})
        merged, _, attempts = cache.read(None, self.home, self.env)
        self.assertEqual(self.used('codex', merged), 7)
        self.assertEqual([a['status'] for a in attempts], ['unreadable_or_invalid'])

    def test_empty_and_oversized_files_are_not_caches(self):
        path = self.canonical({})
        with self.assertRaises(ValueError):
            cache.read_document(path)
        path.write_text(json.dumps({'providers': {'a': {}}}) + ' ' * cache.MAX_BYTES, encoding='utf-8')
        with self.assertRaises(ValueError):
            cache.read_document(path)

    def test_providers_helper_returns_the_merged_table_or_nothing(self):
        self.assertEqual(cache.providers(self.home, self.env), {})
        self.legacy({'codex': row(3, 5)})
        self.assertIn('codex', cache.providers(self.home, self.env))

    def test_the_home_a_cache_file_implies(self):
        self.assertEqual(cache.home_of(Path('/h/.agent-compass/usage-cache.json')), Path('/h'))
        self.assertEqual(cache.home_of(Path('/h/.usage-float/usage-cache.json')), Path('/h'))
        self.assertEqual(cache.home_of(Path('/tmp/x/usage-cache.json')), Path('/tmp/x'))


class SeedTests(CacheBase):
    def test_seeding_copies_the_legacy_file_once_and_leaves_it_untouched(self):
        legacy = self.legacy({'codex': row(4, 5)}, attempts={'codex': {'at': 1.0}})
        before = legacy.read_bytes()
        self.assertTrue(cache.seed_from_legacy(self.home, self.env))
        target = cache.canonical_path(self.home, self.env)
        self.assertEqual(json.loads(target.read_text())['attempts'], {'codex': {'at': 1.0}})
        self.assertEqual(legacy.read_bytes(), before)
        target.write_text(json.dumps({'providers': {'mine': row(1, 1)}}), encoding='utf-8')
        self.assertFalse(cache.seed_from_legacy(self.home, self.env))     # never overwrites
        self.assertIn('mine', json.loads(target.read_text())['providers'])

    def test_seeding_is_off_with_a_named_file_or_without_a_legacy_copy(self):
        self.assertFalse(cache.seed_from_legacy(self.home, self.env))
        self.legacy({'codex': row(4, 5)})
        self.env['ROUTER_USAGE_CACHE'] = str(self.home / 'named.json')
        self.assertFalse(cache.seed_from_legacy(self.home, self.env))
        self.assertFalse(cache.canonical_path(self.home, {}).exists())

    def test_an_invalid_legacy_file_is_not_copied(self):
        path = cache.legacy_path(self.home)
        path.parent.mkdir(parents=True)
        path.write_text('{"providers": {"a": {"x": NaN}}}', encoding='utf-8')
        self.assertFalse(cache.seed_from_legacy(self.home, self.env))
        self.assertFalse(cache.canonical_path(self.home, self.env).exists())
        self.assertEqual(list(cache.canonical_path(self.home, self.env).parent.glob('*.tmp')), [])


if __name__ == '__main__':
    unittest.main()
