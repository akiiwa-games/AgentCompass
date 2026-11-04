"""The routing simulation feeds usage through the roll pipeline, so quota scarcity moves the distribution."""
from datetime import datetime, timedelta, timezone
import unittest

import route_simulation as sim
from router_test_support import isolated_router_state
from test_fleet_parity import shipped_template, snapshot


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup, discovery=False, policy=False)


NOW = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)


def window(usage, account, wid):
    return next(w for w in usage['accounts'][account]['windows'] if w['id'] == wid)


class UsageAwareSimulationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.accounts = sim.fresh_accounts(shipped_template(), snapshot())
        cls.tasks = sim.grid_tasks(cls.accounts)
        cls.policy = {'allowed_accounts': list(cls.accounts['accounts'])}

    def wins(self, state):
        usage = sim.snapshot(sim.synthetic_document(self.accounts, NOW, sim.STATES[state]), self.accounts, NOW)
        return sim.run_state(self.accounts, usage, NOW, self.tasks, self.policy)[1]

    @staticmethod
    def share(wins, *labels):
        return sum(wins[label] for label in labels) / sum(wins.values())

    def test_scarce_windows_reach_the_allocator_as_remaining_quota(self):
        usage = sim.snapshot(sim.synthetic_document(self.accounts, NOW, sim.STATES['claude scarce']), self.accounts, NOW)
        weekly = window(usage, 'claude', 'seven_day')
        self.assertAlmostEqual(weekly['remaining_pct_before_reset'], 100 - sim.SCARCE_USED)
        self.assertGreater(weekly['seconds_until_reset'], 0)
        self.assertAlmostEqual(window(usage, 'codex', 'usage')['remaining_pct_before_reset'], 100 - sim.AMPLE_USED)

    def test_claude_scarcity_moves_work_off_opus_on_claude(self):
        ample, scarce = self.wins('ample'), self.wins('claude scarce')
        opus = 'claude-opus-5-5@claude'
        self.assertGreater(self.share(ample, opus), 0.1)
        self.assertLess(self.share(scarce, opus), self.share(ample, opus) / 2)

    def test_codex_scarcity_moves_work_off_sol_and_astra(self):
        ample, scarce = self.wins('ample'), self.wins('codex scarce')
        codex = [label for label in ample | scarce if label.endswith('@codex')]
        self.assertGreater(self.share(ample, *codex), 0.05)
        self.assertLess(self.share(scarce, *codex), self.share(ample, *codex) / 2)

    def test_stale_readings_are_not_priced_as_scarce_or_ample(self):
        usage = sim.snapshot(sim.synthetic_document(self.accounts, NOW, 'stale'), self.accounts, NOW)
        weekly = window(usage, 'claude', 'seven_day')
        self.assertIsNone(weekly['remaining_pct_before_reset'])

    def test_ledger_usage_replays_through_the_same_pipeline(self):
        at = NOW - timedelta(days=2)
        compact = {'claude': {'status': 'ok', 'fetched_at': at.isoformat(),
                              'windows': [{'id': 'seven_day', 'used_pct': 72.0,
                                           'resets_at': (at + timedelta(days=3)).isoformat()}]}}
        usage = sim.snapshot(sim.ledger_document(compact), self.accounts, at)
        weekly = window(usage, 'claude', 'seven_day')
        self.assertAlmostEqual(weekly['remaining_pct_before_reset'], 28.0)
        self.assertAlmostEqual(weekly['seconds_until_reset'], 3 * 86400, delta=1)


if __name__ == '__main__':
    unittest.main()
