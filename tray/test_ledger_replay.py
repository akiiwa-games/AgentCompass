"""The tray replays the ranking the ledger stored; it never asks the allocator again when a snapshot exists."""
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/agent-compass/scripts'))
import router_data
import router_priority as priority
from router_test_support import isolated_router_state


class LedgerReplayTests(unittest.TestCase):
    def setUp(self):
        self.allocator, self.router = priority.modules()
        isolated_router_state(self.addCleanup)
        self.now = datetime.now(timezone.utc)
        scorer = patch.dict(os.environ, {'ROUTER_DISABLE_LIVE_SCORER': '1'})
        scorer.start()
        self.addCleanup(scorer.stop)
        self.addCleanup(setattr, self.router, 'LEDGER_WRITES', True)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.ledger = Path(directory.name) / 'ledger.jsonl'
        patcher = patch.object(self.router, 'ledger_path', return_value=self.ledger)
        patcher.start()
        self.addCleanup(patcher.stop)
        window = lambda wid, used, hours: {'id': wid, 'used_pct': used, 'resets_at': (self.now + timedelta(hours=hours)).isoformat()}
        row = lambda *windows: {'fetched_at': self.now.isoformat(), 'available': True, 'stale': False, 'windows': list(windows)}
        self.cache = Path(directory.name) / 'usage-cache.json'
        self.cache.write_text(json.dumps({'providers': {
            'claude': row(window('five_hour', 10, 3), window('seven_day', 40, 100), window('weekly_fable', 0, 100)),
            'codex': row(window('usage', 40, 100)),
            'antigravity': row(window('gemini-5h', 10, 3), window('gemini-weekly', 20, 100))}}), encoding='utf-8')
        self.task = Path(directory.name) / 'task.json'
        self.task.write_text(json.dumps({'task': {
            'objective': 'Fix the pagination bug', 'work_kind': 'general_implementation', 'expected_hours': 1,
            'needs': {'local_only': True},
            'profile': {'reasoning_depth': 3, 'code_complexity': 2, 'context_scale': 2, 'hallucination_risk': 2, 'error_cost': 2,
                        'needs_visual': False, 'needs_gui': False, 'local_only': True, 'interaction_style': 'engineering',
                        'reason': 'test'}}}), encoding='utf-8')
        argv = ['route_judge.py', 'plan', '--input', str(self.task), '--usage-cache', str(self.cache)]
        with patch('sys.argv', argv), patch('builtins.print') as shown:
            self.assertEqual(self.router.main(), 0)
        self.live = json.loads(shown.call_args.args[0])
        [self.row] = [r for r in self.router.read_ledger() if r.get('stage') == 'plan']

    def test_replay_returns_the_stored_ranking_even_after_config_and_quota_change(self):
        accounts = self.allocator.load_accounts()
        accounts['models']['claude-opus-5-5']['tier'] = 1.0
        with patch.object(router_data, 'plan_task', side_effect=AssertionError('replay must not re-plan')), \
                patch.object(self.allocator, 'plan', side_effect=AssertionError('replay must not re-plan')), \
                patch.object(self.allocator, 'load_accounts', return_value=accounts):
            replayed = router_data.replay(copy.deepcopy(self.row))
        self.assertEqual(replayed['theatre_source'], 'snapshot')
        self.assertEqual(replayed['choice'], self.live['choice'])
        stored = self.row['decision_snapshot']['ranking']
        self.assertEqual([c['id'] for c in replayed['ranking']], [c['id'] for c in stored])
        winner = replayed['ranking'][0]
        self.assertEqual(winner['components'], stored[0]['components'])
        self.assertAlmostEqual(winner['score'], self.live['ranking'][0]['score'], places=3)
        self.assertEqual(replayed['decision']['system']['weights'], self.live['decision']['system']['weights'])
        rows = router_data.candidate_rows(replayed)
        self.assertEqual(rows[0]['id'], self.live['choice'])

    def test_replay_does_not_mutate_the_ledger_row(self):
        before = copy.deepcopy(self.row)
        router_data.replay(self.row)
        self.assertEqual(self.row, before)

    def test_legacy_row_without_a_snapshot_is_recomputed_and_labelled(self):
        legacy = {k: v for k, v in self.row.items() if k != 'decision_snapshot'}
        legacy.update(objective='Fix the pagination bug', hours=1, local_only=True, kind='general_implementation')
        calls = []
        sentinel = {'choice': 'x', 'ranking': [], 'task': {}}
        with patch.object(router_data, 'modules', return_value=(self.allocator, self.router)), \
                patch.object(router_data, 'plan_task', side_effect=lambda task: calls.append(task) or dict(sentinel)):
            replayed = router_data.replay(legacy)
        self.assertEqual(len(calls), 1)
        self.assertEqual(replayed['theatre_source'], 'recomputed')


if __name__ == '__main__':
    unittest.main()
