"""Second Claude account must never borrow or refresh the first account's token."""
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import usage_core as usage


class ClaudeAccountsTests(unittest.TestCase):
    def test_missing_second_account_does_not_fall_back(self):
        with TemporaryDirectory() as folder, patch.object(usage, 'CLAUDE_HOME_2', Path(folder)):
            self.assertFalse(usage.provider_available('claude-2:7d'))



    def test_rate_limit_backoff_and_cached_usage_are_account_scoped(self):
        with TemporaryDirectory() as folder, patch.object(usage, 'CACHE_PATH', Path(folder) / 'cache.json'):
            usage.set_claude_backoff(provider_id='claude-2')
            self.assertTrue(usage.claude_in_backoff('claude-2'))
            self.assertFalse(usage.claude_in_backoff())
            usage.clear_claude_backoff()
            self.assertTrue(usage.claude_in_backoff('claude-2'))
            good = usage.ProviderUsage(provider_id='claude', display_name='claude', available=True,
                                       fetched_at=datetime.now(timezone.utc).isoformat())
            usage.cache_put_provider(good)
            self.assertFalse(usage._claude_rate_limited_usage('claude-2').available)


    def test_hud_retries_token_refreshed_by_cli(self):
        with patch.object(usage, 'read_claude_oauth', side_effect=[({'accessToken': 'old'}, None), ({'accessToken': 'new'}, None)]), patch.object(
            usage, 'http_json', side_effect=[(401, None), (200, {})]
        ) as call:
            self.assertEqual(usage.claude_authorized_json('usage', home=Path('second')), (200, {}))
            self.assertEqual(call.call_args.kwargs['headers']['Authorization'], 'Bearer new')


if __name__ == '__main__':
    unittest.main()
