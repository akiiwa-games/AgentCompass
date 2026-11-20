"""A rate-limit window of the ChatGPT usage endpoint is named by its duration, whichever reader read it (Codex's or
Pi's), so a configured window a fresh reading reports is observed. Rules, never outcomes: the expected id of a
configured window is the configuration's own (its `hours`), and the two readers are held to each other."""
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import patch

import account_fleet
import quota_pools
import route_judge
import usage_core
import usage_reading
from router_test_support import temporary_root

HOUR = 3600


def payload(now, primary_seconds, secondary_seconds, used=(10.0, 40.0)):
    """The endpoint's answer: two slots, each window saying how long it is."""
    def window(seconds, pct, hours_left):
        return {'used_percent': pct, 'limit_window_seconds': seconds,
                'reset_at': int((now + timedelta(hours=hours_left)).timestamp())}
    return {'plan_type': 'plus', 'rate_limit': {'primary_window': window(primary_seconds, used[0], 3),
                                                'secondary_window': window(secondary_seconds, used[1], 100)}}


class Readers(unittest.TestCase):
    def setUp(self):
        self.root = temporary_root(self.addCleanup)
        self.now = datetime.now(timezone.utc)
        for name, value in (('CACHE_PATH', self.root / 'usage-cache.json'), ('HOME', self.root)):
            p = patch.object(usage_core, name, value)
            p.start()
            self.addCleanup(p.stop)

    def codex(self, answer, cache=False):
        account = usage_core.CodexAccount('codex', 'codex', self.root / '.codex')
        with self.network(cache, patch.object(usage_core, 'codex_authorized_json', return_value=(200, answer))):
            return usage_core.fetch_codex_usage(account)

    def pi(self, answer, cache=False):
        auth = {'tokens': {'access_token': 't', 'account_id': 'a'}}
        with self.network(cache, patch.object(usage_core, 'read_pi_codex_auth', return_value=(auth, None)),
                          patch.object(usage_core, '_pi_agent_home', return_value=self.root / '.pi'),
                          patch.object(usage_core, 'http_json', return_value=(200, answer))):
            return usage_core.fetch_pi_usage('pi')

    @staticmethod
    def network(cache, *patches):
        """The endpoint's answer in place of the network; the reading reaches the usage cache only with `cache`."""
        stack = ExitStack()
        for p in (*patches, patch.object(usage_core, 'provider_credential_key', return_value='k'),
                  *(() if cache else (patch.object(usage_core, 'cache_put_provider'),))):
            stack.enter_context(p)
        return stack

    @staticmethod
    def windows(reading):
        return sorted((w.id, w.label, w.used_pct, w.resets_at) for w in reading.windows)


class SameNameTests(Readers):
    SHAPES = {'5h and 7d': (5 * HOUR, 168 * HOUR), '7d in the first slot': (168 * HOUR, 5 * HOUR),
              'other lengths': (2 * HOUR, 720 * HOUR)}

    def test_both_readers_name_a_window_alike_whatever_its_length_or_slot(self):
        for label, (first, second) in self.SHAPES.items():
            with self.subTest(shape=label):
                answer = payload(self.now, first, second)
                pi, codex = self.windows(self.pi(answer)), self.windows(self.codex(answer))
                self.assertEqual(pi, codex)
                self.assertEqual(len({wid for wid, *_ in pi}), 2, pi)       # two lengths, two names

    def test_a_configured_window_carries_its_configured_id_on_pi(self):
        spec = account_fleet.load_accounts()['accounts']['pi']
        configured = {w['hours']: wid for wid, w in spec['windows'].items() if w.get('hours')}
        self.assertTrue(configured, spec)
        for label, (first, second) in self.SHAPES.items():
            with self.subTest(shape=label):
                answer = payload(self.now, first, second)
                reading = {w.resets_at: w.id for w in self.pi(answer).windows}
                for slot, seconds in (('primary_window', first), ('secondary_window', second)):
                    wid = reading[usage_core._iso_timestamp(answer['rate_limit'][slot]['reset_at'])]
                    if seconds // HOUR in configured:
                        self.assertEqual(wid, configured[seconds // HOUR], (label, slot))
                    else:
                        self.assertNotIn(wid, configured.values(), (label, slot))


class ObservedTests(Readers):
    def snapshot(self, answer):
        self.pi(answer, cache=True)
        return usage_reading.usage_snapshot(str(usage_core.CACHE_PATH), ['pi'])

    def bundle(self, accounts):
        """A pi bundle of a model pi launches (its rows of `dispatch_efforts`): the reference fleet declares none."""
        model = next(iter(accounts['dispatch_efforts']['pi']))
        return {'id': 'pi_probe', 'account': 'pi', 'provider': 'pi', 'model': model}

    def test_a_configured_window_a_fresh_pi_reading_reports_is_observed(self):
        accounts = account_fleet.load_accounts()
        usage = self.snapshot(payload(self.now, 5 * HOUR, 168 * HOUR))
        row = usage['accounts']['pi']
        self.assertEqual(row.get('status'), 'fresh', row)
        bundle = self.bundle(accounts)
        windows = quota_pools.applicable_windows(accounts['accounts']['pi'], bundle['model'], row, bundle=bundle,
                                                 accounts=accounts)
        self.assertTrue(windows)
        self.assertEqual([wid for wid, _, observed in windows if observed is None], [], windows)
        self.assertIsNone(route_judge.quota_unobserved(accounts, usage, bundle))

    def test_the_launch_check_reads_the_configured_window_and_blocks_only_when_it_is_spent(self):
        accounts = account_fleet.load_accounts()
        bundle = self.bundle(accounts)
        configured = set(accounts['accounts']['pi']['windows'])
        for used, blocked in ((100.0, True), (60.0, False)):
            with self.subTest(used=used):
                usage = self.snapshot(payload(self.now, 5 * HOUR, 168 * HOUR, used=(10.0, used)))
                stops = {wid for wid, _ in quota_pools.launch_blockers(bundle, accounts, usage, self.now)}
                self.assertEqual(bool(stops & configured), blocked, stops)


if __name__ == '__main__':
    unittest.main()
