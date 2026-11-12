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

import pytest

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
        (self.root / '.codex-2').mkdir()
        for name, value in (('HOME', self.root), ('CACHE_PATH', self.root / 'cache.json'),
                            ('CLAUDE_HOME', self.first), ('CLAUDE_HOME_2', self.second),
                            ('CODEX_HOME_2', self.root / '.codex-2')):
            p = patch.object(usage, name, value)
            p.start()
            self.addCleanup(p.stop)
        p_codex = patch.dict(
            usage.CODEX_ACCOUNT_BY_ID,
            {'codex-2': usage.CodexAccount('codex-2', 'codex-2', self.root / '.codex-2')},
        )
        p_codex.start()
        self.addCleanup(p_codex.stop)
        p = patch.dict('os.environ', {'AGENTCOMPASS_ACTIVITY_FILE': str(self.root / 'capacity.json')})
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

    def test_the_claude_plan_is_the_live_profiles_never_the_login_files(self):
        """The login file's subscriptionType is frozen at the last login: it says max while the live profile says pro."""
        (self.first / '.credentials.json').write_text(json.dumps({'claudeAiOauth': {
            'accessToken': 'fixture', 'subscriptionType': 'max', 'rateLimitTier': 'default_claude_max_20x'}}))
        week = {'seven_day': {'utilization': 12}}
        profiles = {
            'pro': (200, {'organization': {'organization_type': 'claude_pro', 'rate_limit_tier': 'default_claude_ai'}}),
            'max20': (200, {'organization': {'organization_type': 'claude_max', 'rate_limit_tier': 'default_claude_max_20x'}}),
            'max5': (200, {'organization': {'organization_type': 'claude_max', 'rate_limit_tier': 'default_claude_max_5x'}}),
            None: (403, {'error': {'type': 'permission_error'}}),
        }
        for expected, profile in profiles.items():
            with self.subTest(expected=expected):
                seen = []

                def answer(url, **kwargs):
                    seen.append(url)
                    return profile if url == usage.CLAUDE_PROFILE_URL else (200, week)
                with patch.object(usage, 'claude_authorized_json', side_effect=answer):
                    result = usage.fetch_claude_usage(force=True)
                self.assertTrue(result.available)
                self.assertIn(usage.CLAUDE_PROFILE_URL, seen)       # read with the usage refresh
                self.assertEqual((result.plan_label, result.plan_source),
                                 (expected, 'profile_api' if expected else None))
                cached = usage.load_usage_cache()['providers']['claude']
                self.assertEqual((cached.get('plan_label'), cached.get('plan_source')),
                                 (expected, 'profile_api' if expected else None))

    def test_a_profile_that_does_not_say_its_plan_gives_none(self):
        for profile in ({}, {'organization': None}, {'organization': {'organization_type': ''}},
                        {'organization': {'organization_type': 'api_individual'}}, 'not a dict'):
            with self.subTest(profile=profile):
                self.assertIsNone(usage.claude_profile_plan(profile))
        self.assertEqual(usage.claude_profile_plan({'organization': {'organization_type': 'claude_max'}}), 'max')


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
        ) as grok, patch.object(usage, 'discover_metered_usd_accounts', return_value={'usd': usage.MeteredUsdAccount(
            'usd', 'usd', 'sk-test', 'https://proxy.example.test', 'https://proxy.example.test/v1',
            'https://proxy.example.test', None, (),
        )}) as _found, patch.object(usage, 'fetch_metered_usd_usage', return_value=self.good('usd')) as proxy:
            result = usage.collect_local_usage(['codex', 'codex-2', 'grok', 'usd'])
        self.assertEqual([p.provider_id for p in result], ['codex', 'codex-2', 'grok', 'usd'])
        self.assertEqual(codex.call_count, 2)
        grok.assert_called_once()
        proxy.assert_called_once()

    def activity(self, workers, age=0):
        (self.root / 'capacity.json').write_text(json.dumps({
            'schema': 'agent-compass.activity.v1',
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

    @pytest.mark.slow
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

    def test_failed_attempts_use_exponential_retry_backoff_and_preserve_claude_429_backoff(self):
        self.activity({})
        failure = usage.ProviderUsage(provider_id='claude', display_name='claude', available=False, error='更新失败')
        now0 = 1_700_000_000.0
        clock = [now0]
        with patch.object(usage.time, 'time', side_effect=lambda: clock[0]), \
             patch.object(usage, 'fetch_claude_usage', return_value=failure) as direct:
            # First failed attempt at t=0 -> throttled for 120s (2m), not full 1800s idle poll
            usage.collect_local_usage(['claude'])
            self.assertEqual(direct.call_count, 1)

            clock[0] = now0 + 60
            usage.collect_local_usage(['claude'])
            self.assertEqual(direct.call_count, 1)

            # At t=125s (>= 120s), retries and fails a second time -> backoff doubles to 240s (4m)
            clock[0] = now0 + 125
            usage.collect_local_usage(['claude'])
            self.assertEqual(direct.call_count, 2)

            clock[0] = now0 + 125 + 180
            usage.collect_local_usage(['claude'])
            self.assertEqual(direct.call_count, 2)

            # At t=125+245s (>= 240s), retries and succeeds -> resets to full 1800s idle poll
            clock[0] = now0 + 125 + 245
            good_item = usage.ProviderUsage(
                provider_id='claude', display_name='claude', available=True,
                fetched_at=datetime.fromtimestamp(clock[0], tz=timezone.utc).isoformat(),
                windows=[usage.UsageWindow('seven_day', '7d', 20)],
            )
            direct.return_value = good_item
            usage.collect_local_usage(['claude'])
            self.assertEqual(direct.call_count, 3)

            # 300s after success (< 1800s idle poll), stays throttled on normal poll cadence
            clock[0] = now0 + 125 + 245 + 300
            usage.collect_local_usage(['claude'])
            self.assertEqual(direct.call_count, 3)

        # Claude 429 rate-limit backoff must never be bypassed by the 2-minute failure retry or force=True
        usage.CACHE_PATH.unlink(missing_ok=True)
        with patch.object(usage, 'claude_authorized_json', return_value=(429, {})) as http_call:
            res1 = usage.collect_local_usage(['claude'], force=True)[0]
            self.assertIn('限流退避', res1.error or '')
            self.assertEqual(http_call.call_count, 1)
            # Simulate 3 minutes passing (> 120s retry backoff, < 900s 429 backoff)
            usage.update_usage_cache(
                lambda c: c.get('attempts', {}).get('claude', {}).update({'at': time.time() - 180})
            )
            res2 = usage.collect_local_usage(['claude'], force=False)[0]
            res3 = usage.collect_local_usage(['claude'], force=True)[0]
            self.assertIn('限流退避', res2.error or '')
            self.assertIn('限流退避', res3.error or '')
            self.assertEqual(http_call.call_count, 1)

    def test_success_with_advancing_clock_magpie_and_v042_cache_never_triggers_false_retry(self):
        self.activity({})
        codex_dir = self.root / '.codex'
        codex_dir.mkdir(exist_ok=True)
        (codex_dir / 'auth.json').write_text(json.dumps({'tokens': {'access_token': 'codex-tok', 'account_id': 'acc-1'}}))
        real_time = time.time
        offset = [0.0]

        def advancing_time():
            offset[0] += 0.005
            return real_time() + offset[0]

        def fetch_claude(**kwargs):
            pid = kwargs.get('provider_id', 'claude')
            now_dt = datetime.fromtimestamp(advancing_time(), tz=timezone.utc)
            if pid == 'claude-2':
                # Magpie gateway readAt is minutes older than the local attempt timestamp
                read_at = (now_dt - timedelta(seconds=180)).isoformat()
                res = usage.ProviderUsage(
                    provider_id='claude-2', display_name='claude-2', available=True,
                    fetched_at=read_at, backend_kind='gateway', billing='subscription',
                    windows=[usage.UsageWindow('five_hour', '5h', 15)],
                )
                usage.cache_put_provider(res)
                return res
            return usage.ProviderUsage(
                provider_id=pid, display_name=pid, available=True,
                fetched_at=now_dt.isoformat(),
                windows=[usage.UsageWindow('seven_day', '7d', 20)],
            )

        def fetch_codex(acc):
            now_dt = datetime.fromtimestamp(advancing_time(), tz=timezone.utc)
            return usage.ProviderUsage(
                provider_id=acc.provider_id, display_name=acc.display_name, available=True,
                fetched_at=now_dt.isoformat(),
                windows=[usage.UsageWindow('usage', '7d', 30)],
            )

        import types
        codex_acc = usage.CodexAccount('codex', 'codex', codex_dir)
        magpie_src = types.SimpleNamespace(credential_key='magpie-ck-2')
        with patch.object(usage.time, 'time', side_effect=advancing_time), \
             patch.object(usage, 'CODEX_HOME', codex_dir), \
             patch.dict(usage.CODEX_ACCOUNT_BY_ID, {'codex': codex_acc}), \
             patch.object(usage, 'claude_magpie_source', side_effect=lambda pid: magpie_src if pid == 'claude-2' else None), \
             patch.object(usage, 'fetch_claude_usage', side_effect=fetch_claude) as m_claude, \
             patch.object(usage, 'fetch_codex_usage', side_effect=fetch_codex) as m_codex:
            # 1. Initial successful read across Claude, Codex, and Magpie (claude-2)
            usage.collect_local_usage(['claude', 'codex', 'claude-2'])
            self.assertEqual(m_claude.call_count, 2)  # claude + claude-2
            self.assertEqual(m_codex.call_count, 1)
            cache_doc = usage.load_usage_cache()
            for pid in ('claude', 'codex', 'claude-2'):
                att = cache_doc['attempts'][pid]
                self.assertEqual(att.get('failures'), 0, pid)
                obs = usage._iso_epoch(cache_doc['providers'][pid]['fetched_at'], 0)
                self.assertGreater(att['at'], obs, pid)

            # At +125s after success (< 300s Magpie poll and < 1800s idle poll), nothing re-fetches
            offset[0] += 125.0
            usage.collect_local_usage(['claude', 'codex', 'claude-2'])
            self.assertEqual(m_claude.call_count, 2)
            self.assertEqual(m_codex.call_count, 1)

            # 2. First real failure after a success records failures == 1 and retry_seconds == 120
            offset[0] += 2000.0
            m_claude.side_effect = lambda **kw: usage.ProviderUsage(
                provider_id=kw.get('provider_id', 'claude'), display_name='claude', available=False, error='更新失败'
            )
            m_codex.side_effect = lambda acc: usage.ProviderUsage(
                provider_id=acc.provider_id, display_name=acc.display_name, available=False, error='更新失败'
            )
            usage.collect_local_usage(['claude', 'codex'])
            cache_after_fail = usage.load_usage_cache()
            for pid in ('claude', 'codex'):
                self.assertEqual(cache_after_fail['attempts'][pid].get('failures'), 1, pid)
                self.assertEqual(cache_after_fail['attempts'][pid].get('retry_seconds'), 120, pid)

            # 3. Old v0.4.2 cache entry without `failures` key is not counted as a failure
            m_claude.side_effect = fetch_claude
            offset[0] += 2000.0
            usage.collect_local_usage(['claude'])
            calls_before_legacy = m_claude.call_count
            ckey = usage.provider_credential_key('claude')
            legacy_at = advancing_time() + 1.0
            usage.update_usage_cache(
                lambda c: c['attempts'].__setitem__('claude', {'at': legacy_at, 'credential_key': ckey})
            )
            self.assertNotIn('failures', usage.load_usage_cache()['attempts']['claude'])

            # At +125s after the legacy v0.4.2 attempt, must throttle at normal poll (not 120s)
            offset[0] += 125.0
            usage.collect_local_usage(['claude'])
            self.assertEqual(m_claude.call_count, calls_before_legacy)

            # When poll finally elapses and a failure occurs, it starts at failures == 1 (120s)
            offset[0] += 2000.0
            m_claude.side_effect = lambda **kw: usage.ProviderUsage(
                provider_id='claude', display_name='claude', available=False, error='更新失败'
            )
            usage.collect_local_usage(['claude'])
            self.assertEqual(usage.load_usage_cache()['attempts']['claude'].get('failures'), 1)
            self.assertEqual(usage.load_usage_cache()['attempts']['claude'].get('retry_seconds'), 120)



if __name__ == '__main__':
    unittest.main()
