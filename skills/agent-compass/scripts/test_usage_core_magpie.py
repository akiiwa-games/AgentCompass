"""Remote Claude quota reads stay on the selected gateway and account."""
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import usage_magpie as magpie_usage
import usage_core as usage


class MagpieUsageTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.home = Path(self.folder.name)
        self.settings = {
            'env': {'ANTHROPIC_BASE_URL': 'https://192.0.2.1', 'ANTHROPIC_AUTH_TOKEN': 'gateway-key'},
            'usageFloat': {'source': 'magpie', 'account': 'second@example.com'},
        }
        self.save()
        self.now = datetime.now(timezone.utc).isoformat()
        self.quota = {'provider': 'claude', 'kind': 'subscription', 'user': 'second@example.com',
                      'plan': 'pro', 'readAt': self.now,
                      'windows': [{'name': '5 hours', 'used': 7, 'resetsAt': self.now},
                                  {'name': '7 days', 'used': 54, 'resetsAt': self.now}]}
        for name, value in [('CLAUDE_HOME_2', self.home), ('CACHE_PATH', self.home / 'cache.json')]:
            p = patch.object(usage, name, value)
            p.start()
            self.addCleanup(p.stop)

    def save(self):
        (self.home / 'settings.json').write_text(json.dumps({'env': self.settings['env']}), encoding='utf-8')
        (self.home / 'usage-float.json').write_text(json.dumps(self.settings.get('usageFloat', {})), encoding='utf-8')


    def test_gateway_failure_never_falls_back_to_local_oauth(self):
        with patch.object(usage, 'http_json', return_value=(401, {})), \
                patch.object(usage, 'claude_authorized_json', side_effect=AssertionError('local API call')):
            result = usage.fetch_claude_usage(provider_id='claude-2')
        self.assertFalse(result.available)
        self.assertTrue(result.stale)

    def test_account_change_invalidates_history_and_mismatch_is_rejected(self):
        old = usage.provider_credential_key('claude-2')
        self.settings['usageFloat']['account'] = 'different@example.com'
        self.save()
        self.assertNotEqual(old, usage.provider_credential_key('claude-2'))
        with patch.object(usage, 'http_json', return_value=(200, {'data': [self.quota]})):
            self.assertFalse(usage.fetch_claude_usage(provider_id='claude-2').available)

    def test_missing_or_invalid_windows_are_not_zero_usage(self):
        source = magpie_usage.read_source(self.home)
        for value in (None, float('nan'), True):
            self.quota['windows'][0]['used'] = value
            with self.assertRaises(ValueError):
                magpie_usage.parse_quota({'data': [self.quota]}, source)

    def test_old_gateway_snapshot_stays_stale(self):
        self.quota['readAt'] = '2020-01-01T00:00:00Z'
        with patch.object(usage, 'http_json', return_value=(200, {'data': [self.quota]})):
            result = usage.fetch_claude_usage(provider_id='claude-2')
        self.assertTrue(result.stale)
        self.assertEqual(result.fetched_at, '2020-01-01T00:00:00Z')

    def test_late_gateway_reading_replaces_older_cache_instead_of_reverting(self):
        old_at = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
        with patch.object(usage, 'http_json', return_value=(200, {'data': [dict(self.quota, readAt=old_at)]})):
            seeded = usage.fetch_claude_usage(provider_id='claude-2')
        self.assertEqual(usage.load_usage_cache()['providers']['claude-2']['fetched_at'], seeded.fetched_at)
        read_at = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        self.quota.update(readAt=read_at, windows=[{'name': '5 hours', 'used': 100, 'resetsAt': self.now},
                                                   {'name': '7 days', 'used': 64, 'resetsAt': self.now}])
        with patch.object(usage, 'http_json', return_value=(200, {'data': [self.quota]})):
            [result] = usage.collect_local_usage(['claude-2'], force=True)
        self.assertIsNone(result.error)
        self.assertTrue(result.stale)
        self.assertEqual(result.fetched_at, read_at)
        self.assertEqual(sorted(w.used_pct for w in result.windows), [64, 100])
        row = usage.load_usage_cache()['providers']['claude-2']
        self.assertEqual(row['fetched_at'], read_at)
        self.assertTrue(row['stale'])
        self.assertIsNone(row['error'])
        self.assertEqual(sorted(w['used_pct'] for w in row['windows']), [64, 100])

    def test_auto_discovers_gateway_without_usage_float_json_and_respects_opt_out(self):
        del self.settings['usageFloat']
        self.save()
        source = magpie_usage.read_source(self.home)
        self.assertIsNotNone(source)
        self.assertEqual(source.url, 'https://192.0.2.1')
        self.assertEqual(source.token, 'gateway-key')
        self.assertEqual(source.account, '')
        self.assertTrue(usage.provider_available('claude-2'))
        quota, windows = magpie_usage.parse_quota({'data': [self.quota]}, source)
        self.assertEqual(quota['user'], 'second@example.com')
        self.assertEqual(len(windows), 2)
        (self.home / 'usage-float.json').write_text(json.dumps({'source': 'api'}), encoding='utf-8')
        self.assertIsNone(magpie_usage.read_source(self.home))
        self.assertFalse(usage.provider_available('claude-2'))


if __name__ == '__main__':
    unittest.main()
