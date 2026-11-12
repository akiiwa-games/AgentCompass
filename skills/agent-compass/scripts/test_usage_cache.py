"""Where the usage cache lives and how readers read it (usage_cache.py)."""
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

    def used(self, name, merged):
        return merged['providers'][name]['windows'][0]['used_pct']


class CacheTests(CacheBase):
    def test_default_location_is_the_state_dir_cache_only(self):
        self.assertEqual(cache.read_paths(None, self.home, self.env),
                         [self.home / '.agent-compass' / 'usage-cache.json'])
        self.assertEqual(cache.write_path(self.home, self.env), self.home / '.agent-compass' / 'usage-cache.json')

    def test_state_dir_variable_moves_the_canonical_file(self):
        self.env['ROUTER_STATE_DIR'] = str(self.home / 'elsewhere')
        self.assertEqual(cache.read_paths(None, self.home, self.env),
                         [self.home / 'elsewhere' / 'usage-cache.json'])

    def test_a_named_file_is_the_only_one_read_or_written(self):
        named = self.home / 'named.json'
        self.env['ROUTER_USAGE_CACHE'] = str(named)
        self.assertEqual(cache.read_paths(None, self.home, self.env), [named])
        self.assertEqual(cache.write_path(self.home, self.env), named)
        self.assertEqual(cache.read_paths(self.home / 'explicit.json', self.home, self.env), [self.home / 'explicit.json'])

    def test_a_missing_named_file_does_not_fall_back(self):
        self.canonical({'codex': row(1, 5)})
        self.env['ROUTER_USAGE_CACHE'] = str(self.home / 'missing.json')
        merged, origin, attempts = cache.read(None, self.home, self.env)
        self.assertIsNone(merged)
        self.assertEqual([a['path'] for a in attempts], [str(self.home / 'missing.json')])
        self.assertEqual([a['status'] for a in attempts], ['missing'])

    def test_usage_float_cache_directory_is_ignored(self):
        old_uf = self.home / '.usage-float' / 'usage-cache.json'
        self.write(old_uf, {'codex': row(90, 5)})
        merged, origin, attempts = cache.read(None, self.home, self.env)
        self.assertIsNone(merged)
        self.assertEqual(origin, {})
        self.assertEqual(attempts, [{'path': str(cache.canonical_path(self.home, self.env)), 'status': 'missing'}])

    def test_canonical_cache_returns_readings_and_poll_cadence(self):
        self.canonical({'codex': row(10, 30), 'claude': row(20, 5)}, poll={'codex': {'seconds': 300, 'at': 2}})
        merged, origin, attempts = cache.read(None, self.home, self.env)
        self.assertEqual(self.used('codex', merged), 10)
        self.assertEqual(self.used('claude', merged), 20)
        self.assertEqual(merged['poll'], {'codex': {'seconds': 300, 'at': 2}})
        self.assertEqual(origin['codex'], str(cache.canonical_path(self.home, self.env)))
        self.assertEqual(attempts, [])

    def test_an_unreadable_file_is_reported(self):
        cache.canonical_path(self.home, self.env).parent.mkdir(parents=True)
        cache.canonical_path(self.home, self.env).write_text('{broken', encoding='utf-8')
        merged, _, attempts = cache.read(None, self.home, self.env)
        self.assertIsNone(merged)
        self.assertEqual([a['status'] for a in attempts], ['unreadable_or_invalid'])

    def test_empty_and_oversized_files_are_not_caches(self):
        path = self.canonical({})
        with self.assertRaises(ValueError):
            cache.read_document(path)
        path.write_text(json.dumps({'providers': {'a': {}}}) + ' ' * cache.MAX_BYTES, encoding='utf-8')
        with self.assertRaises(ValueError):
            cache.read_document(path)

    def test_providers_helper_returns_the_table_or_nothing(self):
        self.assertEqual(cache.providers(self.home, self.env), {})
        self.canonical({'codex': row(3, 5)})
        self.assertIn('codex', cache.providers(self.home, self.env))

    def test_the_home_a_cache_file_implies(self):
        self.assertEqual(cache.home_of(Path('/h/.agent-compass/usage-cache.json')), Path('/h'))
        self.assertEqual(cache.home_of(Path('/tmp/x/usage-cache.json')), Path('/tmp/x'))


if __name__ == '__main__':
    unittest.main()
