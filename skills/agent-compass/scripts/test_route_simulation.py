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
        cls._wins_cache = {}

    @classmethod
    def wins(cls, state):
        if state not in cls._wins_cache:
            usage = sim.snapshot(sim.synthetic_document(cls.accounts, NOW, sim.STATES[state]), cls.accounts, NOW)
            cls._wins_cache[state] = sim.run_state(cls.accounts, usage, NOW, cls.tasks, cls.policy)[1]
        return cls._wins_cache[state]

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

    def test_work_follows_the_account_with_quota_to_spare(self):
        ample, claude_scarce, codex_scarce, both = (self.wins(s) for s in (
            'ample', 'claude scarce', 'codex scarce', 'claude+codex scarce'))
        codex = [label for label in ample | claude_scarce | codex_scarce | both if label.endswith('@codex')]
        self.assertGreater(self.share(claude_scarce, *codex), self.share(ample, *codex))
        self.assertLess(self.share(codex_scarce, *codex), self.share(claude_scarce, *codex))
        self.assertEqual(self.share(both, *codex), 0)

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


    def test_a_pooled_window_keeps_its_account_count_through_the_ledger(self):
        import quota_pools
        import route_judge
        at = NOW - timedelta(days=2)
        reading = {'id': 'seven_day', 'used_pct': 40.0, 'resets_at': (at + timedelta(days=3)).isoformat(), 'accounts': 3}
        digest = route_judge.usage_digest({'accounts': {'claude': {'status': 'ok', 'fetched_at': at.isoformat(),
                                                                   'windows': [reading]}}})
        self.assertEqual(digest['claude']['windows'][0]['accounts'], 3)
        old = {'claude': {'status': 'ok', 'fetched_at': at.isoformat(), 'windows': [dict(reading, accounts=None)]}}
        old['claude']['windows'][0].pop('accounts')
        for compact, pools in ((digest, None), (old, {'claude': {'seven_day': 3}})):
            usage = sim.snapshot(sim.ledger_document(compact, pools), self.accounts, at)
            self.assertEqual(quota_pools.pool_size(window(usage, 'claude', 'seven_day')), 3)
        usage = sim.snapshot(sim.ledger_document(old), self.accounts, at)    # old record, no live pool: one account
        self.assertEqual(quota_pools.pool_size(window(usage, 'claude', 'seven_day')), 1)

    def test_old_shape_ledger_rows_are_skipped_with_a_warning_count(self):
        import json
        from pathlib import Path
        import tempfile
        from router_test_support import profile
        current_profile = profile(local_only=True, expected_hours=0.5)
        missing_axis = {k: v for k, v in current_profile.items() if k != 'axis_demand'}
        pre_four_category = {
            **current_profile,
            'realtime_loop': True,
            'axis_demand': {'divergence': 1, 'global_rigor': 2, 'module_precision': 3, 'vision_tools': 1},
        }
        base_row = {
            'stage': 'plan', 'at': NOW.isoformat(), 'choice': 'sol_high',
            'kind': 'general_implementation', 'difficulty': 'hard', 'hours': 1.0,
            'usage': {'accounts': {}},
        }
        warnings = []
        with tempfile.TemporaryDirectory() as d:
            ledger_file = Path(d) / 'ledger.jsonl'
            rows = [
                {**base_row, 'decision_snapshot': {'task': {'objective': 'missing axis', 'expected_hours': 1.0, 'profile': missing_axis}}},
                {**base_row, 'decision_snapshot': {'task': {'objective': 'pre four category', 'expected_hours': 1.0, 'profile': pre_four_category}}},
                {**base_row, 'decision_snapshot': {'task': {'objective': 'current row', 'expected_hours': 1.0, 'profile': current_profile}}},
            ]
            ledger_file.write_text('\n'.join(json.dumps(r) for r in rows) + '\n', encoding='utf-8')
            out = sim.replay(path=ledger_file, accounts=self.accounts, warnings=warnings)
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0].get('unreplayable', False))
        self.assertIsNotNone(out[0]['choice'])
        self.assertTrue(any('skipped 2 older-shape ledger row(s)' in w for w in warnings), warnings)


if __name__ == '__main__':
    unittest.main()
