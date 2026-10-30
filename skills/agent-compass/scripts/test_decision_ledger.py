"""Every plan decision is written to the ledger with the ranking and component scores it was made with."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import allocator
import route_judge as router
from router_test_support import isolated_router_state


class DecisionLedgerTests(unittest.TestCase):
    def setUp(self):
        isolated_router_state(self.addCleanup)
        self.now = datetime.now(timezone.utc)
        scorer_env = patch.dict(os.environ, {'ROUTER_DISABLE_LIVE_SCORER': '1'})
        scorer_env.start()
        self.addCleanup(scorer_env.stop)
        self.addCleanup(setattr, router, 'LEDGER_WRITES', True)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.ledger = Path(self.directory.name) / 'ledger.jsonl'
        patcher = patch.object(router, 'ledger_path', return_value=self.ledger)
        patcher.start()
        self.addCleanup(patcher.stop)
        window = lambda wid, used, hours: {'id': wid, 'used_pct': used, 'resets_at': (self.now + timedelta(hours=hours)).isoformat()}
        row = lambda *windows: {'fetched_at': self.now.isoformat(), 'available': True, 'stale': False, 'windows': list(windows)}
        self.cache = Path(self.directory.name) / 'usage-cache.json'
        self.cache.write_text(json.dumps({'providers': {
            'claude': row(window('five_hour', 10, 3), window('seven_day', 40, 100), window('weekly_fable', 0, 100)),
            'codex': row(window('usage', 40, 100)),
            'antigravity': row(window('gemini-5h', 10, 3), window('gemini-weekly', 20, 100))}}), encoding='utf-8')
        self.task = Path(self.directory.name) / 'task.json'
        self.task.write_text(json.dumps({'task': {
            'objective': 'Fix the pagination bug', 'work_kind': 'general_implementation', 'expected_hours': 1,
            'needs': {'local_only': True},
            'profile': {'reasoning_depth': 3, 'code_complexity': 2, 'context_scale': 2, 'hallucination_risk': 2, 'error_cost': 2,
                        'needs_visual': False, 'needs_gui': False, 'local_only': True, 'interaction_style': 'engineering',
                        'reason': 'private reasoning'}}}), encoding='utf-8')

    def run_plan(self, *extra):
        argv = ['route_judge.py', 'plan', '--input', str(self.task), '--usage-cache', str(self.cache), *extra]
        with patch('sys.argv', argv), patch('builtins.print') as shown:
            code = router.main()
        return code, json.loads(shown.call_args.args[0])

    def plan_rows(self):
        return [row for row in router.read_ledger() if row.get('stage') == 'plan']

    def test_plan_line_carries_ranking_components_and_scoring_system(self):
        code, result = self.run_plan()
        self.assertEqual(code, 0)
        [line] = self.plan_rows()
        snapshot = line['decision_snapshot']
        self.assertEqual(snapshot['choice'], result['choice'])
        self.assertEqual(snapshot['v'], 1)
        ranking = snapshot['ranking']
        self.assertEqual(ranking[0]['id'], result['choice'])
        self.assertEqual(snapshot['ranking_total'], len(result['ranking']))
        self.assertEqual(snapshot['ranking_eligible'], sum(1 for c in result['ranking'] if not c['excluded']))
        live_eligible = [c for c in result['ranking'] if not c['excluded']]
        recorded_eligible = [row for row in ranking if not row['excluded']]
        self.assertEqual(len(recorded_eligible), min(len(live_eligible), allocator.SNAPSHOT_FULL + allocator.SNAPSHOT_COMPACT))
        for row, live in zip(recorded_eligible, live_eligible):
            self.assertEqual((row['id'], row['score']), (live['id'], live['score']))
        full = recorded_eligible[:allocator.SNAPSHOT_FULL]
        # Full rows hold every component, the binding window and the money numbers the comparison table reads.
        winner, live = ranking[0], result['ranking'][0]
        self.assertEqual(winner['components'], {k: (round(v, 4) if isinstance(v, float) else v) for k, v in live['components'].items()})
        self.assertEqual(winner['capacity']['window'], live['capacity']['window'])
        self.assertEqual((winner['expected_pct'], winner['expected_usd']), (round(live['expected_pct'], 4), round(live['expected_usd'], 4)))
        self.assertEqual(winner['action'], live['action'])
        self.assertIn('required_ability', winner['components'])
        self.assertTrue(all('capacity' in row or row['expected_pct'] is None for row in full if 'expected_pct' in row))
        # Blocked candidates keep their reasons; the scoring system and the task demand are recorded with the decision.
        blocked = [row for row in ranking if row['excluded']]
        self.assertTrue(blocked)
        self.assertTrue(all(row['score'] is None or row['excluded'] for row in blocked))
        decision = snapshot['decision']
        self.assertEqual(decision['system']['weights'], result['decision']['system']['weights'])
        self.assertEqual(decision['system']['shortage_loss_per_usd'], result['decision']['system']['shortage_loss_per_usd'])
        self.assertEqual(decision['task']['demand']['required_ability'], result['decision']['task']['demand']['required_ability'])
        self.assertEqual(decision['action'], result['decision']['action'])
        self.assertNotIn('reason', decision['task']['profile'])                 # the classifier's free text is not copied
        self.assertNotIn('private reasoning', json.dumps(snapshot))

    def test_snapshot_is_bounded_so_the_ledger_stays_cheap_to_tail(self):
        self.run_plan()
        [line] = self.plan_rows()
        text = json.dumps(line)
        self.assertLess(len(text), 24000, len(text))
        ranking = line['decision_snapshot']['ranking']
        eligible = [row for row in ranking if not row['excluded']]
        self.assertLessEqual(len(eligible), allocator.SNAPSHOT_FULL + allocator.SNAPSHOT_COMPACT)
        self.assertLessEqual(len(ranking) - len(eligible), allocator.SNAPSHOT_EXCLUDED)
        omitted = line['decision_snapshot']['ranking_omitted']
        self.assertEqual(len(ranking) + omitted['eligible'] + omitted['excluded'], line['decision_snapshot']['ranking_total'])
        compact = eligible[allocator.SNAPSHOT_FULL:]
        self.assertTrue(all('capacity' not in row and 'notes' not in row for row in compact))
        self.assertTrue(all({'fit', 'ability', 'opportunity_cost'} <= set(row['components']) for row in compact))

    def test_dry_run_and_non_ranking_stages_write_no_snapshot(self):
        before = len(router.read_ledger())
        self.run_plan('--dry-run')
        self.assertEqual(len(router.read_ledger()), before)
        self.assertIsNone(router.decision_snapshot({'stage': 'record'}))
        self.assertIsNone(allocator.decision_snapshot({'stage': 'record'}))

    def test_snapshot_survives_a_later_change_of_configuration_and_quota(self):
        self.run_plan()
        [before] = self.plan_rows()
        accounts = allocator.load_accounts()
        accounts['models']['claude-opus-5-5']['tier'] = 1.0           # the fleet is recalibrated afterwards
        accounts['effort_curves']['claude-opus-5-5']['ability']['max'] = -5
        later = allocator.plan({'work_kind': 'general_implementation', 'difficulty': 'hard', 'needs': {'local_only': True}},
                               {'read_ok': True, 'accounts': {}}, self.now + timedelta(days=3), accounts=accounts, overrides=[])
        self.assertNotEqual(later['choice'], before['choice'])
        [after] = self.plan_rows()
        self.assertEqual(after, before)

    def test_tail_read_keeps_a_ledger_over_the_cap_usable(self):
        small = 4096
        with patch.object(router, 'LEDGER_TAIL_BYTES', small):
            lines = [json.dumps({'v': 1, 'stage': 'plan', 'choice': f'bundle{i}', 'pad': 'x' * 200}) for i in range(100)]
            self.ledger.write_text('\n'.join(lines) + '\n', encoding='utf-8')
            rows = router.read_ledger()
        self.assertTrue(rows)
        self.assertLess(len(rows), 100)
        self.assertEqual(rows[-1]['choice'], 'bundle99')
        self.assertTrue(all(row['choice'].startswith('bundle') for row in rows))     # the cut first line is dropped, not parsed


if __name__ == '__main__':
    unittest.main()
