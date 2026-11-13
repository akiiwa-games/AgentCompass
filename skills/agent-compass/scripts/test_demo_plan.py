"""Cold-start contract: demo works offline, cannot launch, and leaves the user home untouched."""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import pytest

import demo_plan
import host_policy
import provider_inventory
import quota_overrides
import decision_ledger
import usage_reading
import worker_sessions

pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[3]
ENTRY = ROOT / 'agentcompass'
EXAMPLE = ROOT / 'examples' / 'task.json'


class DemoTests(unittest.TestCase):
    def test_empty_home_cli_ranks_without_writing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {k: v for k, v in os.environ.items() if k.upper() in ('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP')}
            env.update(HOME=directory, USERPROFILE=directory, PATH='')
            command = [sys.executable, '-X', 'utf8', str(ENTRY), 'plan', '--dry-run', '--demo']
            table = subprocess.run(command, cwd=directory, env=env, capture_output=True, text=True, encoding='utf-8', timeout=30)
            self.assertEqual(table.returncode, 0, table.stderr + table.stdout)
            self.assertIn('Rank', table.stdout)
            self.assertIn('winner', table.stdout)
            self.assertIn('DEMO', table.stdout)
            full = subprocess.run(command + ['--format', 'json'], cwd=directory, env=env, capture_output=True, text=True, encoding='utf-8', timeout=30)
            self.assertEqual(full.returncode, 0, full.stderr + full.stdout)
            result = json.loads(full.stdout)
            self.assertGreater(len([c for c in result['ranking'] if not c['excluded']]), 2)
            self.assertTrue(result['ranking'][0]['capacity'])
            self.assertGreater(result['ranking'][0]['components']['opportunity_cost'], 0)
            self.assertTrue(result['demo'])
            self.assertNotIn('decision_id', result)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_demo_never_reads_live_state_even_with_a_continuation(self):
        document = json.loads(EXAMPLE.read_text(encoding='utf-8'))
        document['task']['chain'] = {'worker': {'agent_id': 'must-not-read-live-session'}}
        with ExitStack() as stack:
            for obj, name in ((provider_inventory, 'current'), (host_policy, 'load_policy'),
                              (quota_overrides, 'load_overrides'), (worker_sessions, 'session_state'),
                              (usage_reading, 'usage_snapshot'), (decision_ledger, 'read_ledger'),
                              (Path, 'home')):
                stack.enter_context(patch.object(obj, name, side_effect=AssertionError('live state read')))
            result = demo_plan.plan(document)
        self.assertNotEqual(result['choice'], 'none')

    def test_demo_is_only_a_read_only_plan(self):
        for flags in (['plan'], ['roll', '--dry-run'], ['launch'], ['record', '--dry-run']):
            with self.subTest(flags=flags):
                result = subprocess.run([sys.executable, '-I', str(ENTRY), *flags, '--demo'], capture_output=True,
                                        text=True, encoding='utf-8', timeout=30)
                self.assertEqual(result.returncode, 1)
                self.assertIn('--demo requires plan --dry-run', result.stdout)


if __name__ == '__main__':
    unittest.main()
