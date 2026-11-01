"""Offline regressions for account attribution, OAuth ownership and shared usage."""
import concurrent.futures
import json
import multiprocessing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

import usage_core as usage
from usage_core import file_lock


def write_account(cache, name):
    import usage_core as module
    module.CACHE_PATH = Path(cache)
    for i in range(8):
        module.cache_put_provider(module.ProviderUsage(
            provider_id=name, display_name=name,
            windows=[module.UsageWindow('usage', 'usage', i)],
            fetched_at=datetime.now(timezone.utc).isoformat()))


class UsageSafetyTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.first = self.root / 'first'
        self.second = self.root / 'second'
        for home, token in ((self.first, 'first-token'), (self.second, 'second-token')):
            home.mkdir()
            (home / '.credentials.json').write_text(json.dumps({
                'claudeAiOauth': {'accessToken': token, 'refreshToken': 'not-for-monitor'}}))
        for name, value in (('CACHE_PATH', self.root / 'cache.json'),
                            ('CLAUDE_HOME', self.first), ('CLAUDE_HOME_2', self.second)):
            p = patch.object(usage, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict('os.environ', {'ROUTE_MANAGER_ACTIVITY_FILE': str(self.root / 'capacity.json')})
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(usage, 'http_json', side_effect=AssertionError('Unexpected network'))
        p.start()
        self.addCleanup(p.stop)

    def good(self, pid='claude', age=0):
        return usage.ProviderUsage(provider_id=pid, display_name=pid,
            windows=[usage.UsageWindow('seven_day', '7d', 25)],
            fetched_at=(datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat())


    def test_reset_post_is_not_replayed_when_cli_changes_login(self):
        with patch.object(usage, 'read_claude_oauth', side_effect=[
            ({'accessToken': 'a'}, None), ({'accessToken': 'b'}, None)
        ]) as read, patch.object(usage, 'http_json', return_value=(401, None)) as request:
            usage.claude_authorized_json('claim', method='POST')
            self.assertEqual(read.call_count, 1)
            self.assertEqual(request.call_count, 1)


    def test_unknown_percentage_survives_cache_roundtrip(self):
        item = self.good()
        item.windows[0].used_pct = None
        usage.cache_put_provider(item)
        self.assertIsNone(usage.cache_get_provider('claude').windows[0].used_pct)

    def test_stale_observation_cannot_be_published_as_fresh(self):
        item = self.good()
        usage.cache_put_provider(item)
        old = usage.load_usage_cache()['providers']['claude']['fetched_at']
        usage.mark_provider_stale('claude', 'auth expired')
        item.stale = True
        usage.cache_put_provider(item)
        row = usage.load_usage_cache()['providers']['claude']
        self.assertTrue(row['stale'])
        self.assertEqual(row['fetched_at'], old)

    def test_account_change_invalidates_cache(self):
        usage.cache_put_provider(self.good())
        self.assertIsNotNone(usage.cache_get_provider('claude'))
        (self.first / '.credentials.json').write_text(json.dumps({
            'claudeAiOauth': {'accessToken': 'different-login'}}))
        self.assertIsNone(usage.cache_get_provider('claude'))

    def test_force_cannot_bypass_429_backoff(self):
        usage.cache_put_provider(self.good())
        usage.set_claude_backoff()
        with patch.object(usage, 'claude_authorized_json') as request:
            result = usage.fetch_claude_usage(force=True)
            request.assert_not_called()
        self.assertTrue(result.stale)
        self.assertTrue(usage.load_usage_cache()['providers']['claude']['stale'])

    def test_login_change_does_not_inherit_previous_account_backoff(self):
        usage.set_claude_backoff()
        (self.first / '.credentials.json').write_text(json.dumps({
            'claudeAiOauth': {'accessToken': 'different-login'}}))
        self.assertFalse(usage.claude_in_backoff())

    def test_login_change_during_request_does_not_cache_wrong_usage(self):
        def response(*args, **kwargs):
            (self.first / '.credentials.json').write_text(json.dumps({
                'claudeAiOauth': {'accessToken': 'different-login'}}))
            return 200, {'seven_day': {'utilization': 99}}
        usage.cache_put_provider(self.good())
        with patch.object(usage, 'claude_authorized_json', side_effect=response):
            result = usage.fetch_claude_usage()
        self.assertFalse(result.available)
        self.assertNotIn('claude', usage.load_usage_cache()['providers'])

    def test_empty_claude_payload_is_not_a_success(self):
        with patch.object(usage, 'claude_authorized_json', return_value=(200, {})):
            self.assertFalse(usage.fetch_claude_usage().available)


    def test_hub_name_is_not_an_account_binding(self):
        with patch.object(usage, 'fetch_all', return_value={'claude': self.good()}) as hub, patch.object(
            usage, 'fetch_claude_usage', return_value=self.good()
        ) as direct:
            usage.collect_local_usage(['claude:5h', 'claude:7d'])
            hub.assert_not_called()
            direct.assert_called_once()

    def test_single_row_providers_are_fetched_not_confused_with_display_groups(self):
        with patch.object(usage, 'fetch_codex_usage', side_effect=lambda account: self.good(account.provider_id)) as codex, patch.object(
            usage, 'fetch_grok_usage', return_value=self.good('grok')
        ) as grok, patch.object(usage, 'fetch_api_proxy_usage', return_value=self.good('api-proxy')) as proxy:
            result = usage.collect_local_usage(['codex', 'codex-2', 'grok', 'api-proxy'])
        self.assertEqual([p.provider_id for p in result], ['codex', 'codex-2', 'grok', 'api-proxy'])
        self.assertEqual(codex.call_count, 2)
        grok.assert_called_once()
        proxy.assert_called_once()

    def activity(self, workers, age=0):
        (self.root / 'capacity.json').write_text(json.dumps({
            'schema': 'route-manager.activity.v1',
            'accounts': {pid: {'state': 'active' if workers.get(pid, 0) else 'idle'}
                         for pid in ('claude', 'claude-2', 'codex', 'codex-2', 'grok', 'antigravity')},
            'generated_at': (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat(),
            'active_workers': {pid: {'count': count} for pid, count in workers.items()}}))


    def test_activity_resumes_normal_refresh_for_only_the_used_account(self):
        self.activity({'claude-2': 1})
        usage.cache_put_provider(self.good('claude-2', age=600))
        with patch.object(usage, 'fetch_claude_usage', return_value=self.good('claude-2')) as direct:
            usage.collect_local_usage(['claude-2'])
        direct.assert_called_once()
        self.assertEqual(usage.provider_poll_seconds('claude-2'), 300)
        self.assertEqual(usage.provider_poll_seconds('claude'), 1800)

    def test_idle_account_eventually_checks_and_manual_refresh_is_explicit(self):
        self.activity({})
        for age, force in ((1900, False), (600, True)):
            with self.subTest(age=age, force=force):
                usage.CACHE_PATH.unlink(missing_ok=True)
                usage.cache_put_provider(self.good(age=age))
                with patch.object(usage, 'fetch_claude_usage', return_value=self.good()) as direct:
                    usage.collect_local_usage(['claude'], force=force)
                direct.assert_called_once()

    def test_magpie_account_keeps_normal_cadence_while_local_sessions_are_idle(self):
        self.activity({})
        with patch.object(usage, 'claude_magpie_source', side_effect=lambda pid: object() if pid == 'claude-2' else None):
            self.assertEqual(usage.provider_poll_seconds('claude-2'), 300)
            self.assertEqual(usage.provider_poll_seconds('claude'), 1800)

    def reset_item(self, read_age, reset_age):
        now = datetime.now(timezone.utc)
        return usage.ProviderUsage(provider_id='claude', display_name='claude',
            windows=[usage.UsageWindow('five_hour', '5h', 100,
                                       (now - timedelta(seconds=reset_age)).isoformat())],
            fetched_at=(now - timedelta(seconds=read_age)).isoformat())

    def test_idle_account_rereads_once_a_cached_window_has_reset(self):
        self.activity({})
        usage.cache_put_provider(self.reset_item(read_age=600, reset_age=60))
        with patch.object(usage, 'fetch_claude_usage', return_value=self.reset_item(600, 60)) as direct:
            usage.collect_local_usage(['claude'])
            usage.collect_local_usage(['claude'])   # attempted after the reset: wait REFRESH_SECONDS
        direct.assert_called_once()


    def test_failures_without_snapshot_do_not_trigger_every_ui_tick(self):
        self.activity({})
        failure = usage.ProviderUsage(provider_id='claude', display_name='claude', available=False)
        with patch.object(usage, 'fetch_claude_usage', return_value=failure) as direct:
            usage.collect_local_usage(['claude'])
            usage.collect_local_usage(['claude'])
        direct.assert_called_once()

    def test_stale_or_missing_activity_is_unknown_not_an_active_account(self):
        self.assertEqual(usage.provider_poll_seconds('claude'), 900)
        self.activity({'claude': 1}, age=400)
        self.assertEqual(usage.provider_poll_seconds('claude'), 900)

    def test_unbound_activity_does_not_prove_a_slot_is_idle(self):
        self.activity({})
        path = self.root / 'capacity.json'
        data = json.loads(path.read_text())
        data['accounts'].pop('claude-2')
        path.write_text(json.dumps(data))
        self.assertEqual(usage.provider_poll_seconds('claude-2'), 900)
        data.pop('schema')
        path.write_text(json.dumps(data))
        self.assertEqual(usage.provider_poll_seconds('claude'), 900)


    def test_display_history_is_not_promoted_to_router_cache(self):
        item = self.good()
        item.credential_key = usage.provider_credential_key('claude')
        usage.update_usage_cache(lambda c: c.update({'display_snapshots': {'claude': usage._provider_to_dict(item)}}))
        result = usage.stale_provider_usage('claude', '限流退避中')
        self.assertTrue(result.available)
        self.assertTrue(result.stale)
        self.assertNotIn('claude', usage.load_usage_cache().get('providers', {}))

    def test_ui_and_collector_share_fresh_snapshot(self):
        with patch.object(usage, 'fetch_claude_usage', return_value=self.good()) as direct:
            first = usage.collect_local_usage(['claude'])
            second = usage.collect_local_usage(['claude'])
            self.assertEqual(direct.call_count, 1)
            self.assertEqual(first[0].fetched_at, second[0].fetched_at)
            self.assertFalse(second[0].stale)

    def test_simultaneous_fetches_coalesce(self):
        def slow(**kwargs):
            time.sleep(.1)
            return self.good()
        with patch.object(usage, 'fetch_claude_usage', side_effect=slow) as direct:
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                results = list(pool.map(lambda _: usage.collect_local_usage(['claude']), range(2)))
            self.assertEqual(direct.call_count, 1)
            self.assertTrue(all(r[0].available for r in results))

    def test_two_process_cache_writers_do_not_lose_accounts(self):
        ctx = multiprocessing.get_context('spawn')
        children = [ctx.Process(target=write_account, args=(str(usage.CACHE_PATH), name))
                    for name in ('writer-a', 'writer-b')]
        try:
            for child in children:
                child.start()
            for child in children:
                child.join(10)
                self.assertEqual(child.exitcode, 0)
            self.assertEqual(set(usage.load_usage_cache()['providers']), {'writer-a', 'writer-b'})
        finally:
            for child in children:
                if child.is_alive():
                    child.terminate()
                    child.join(5)

    def test_lock_timeout_does_not_start_duplicate_fetch(self):
        with file_lock(usage.CACHE_PATH.with_name('cache.json.claude.fetch.lock')), patch.object(
            usage, 'fetch_claude_usage'
        ) as direct:
            self.assertFalse(usage.collect_local_usage(['claude'])[0].available)
            direct.assert_not_called()



if __name__ == '__main__':
    unittest.main()
