"""`agentcompass <stage> -h` shows one stage's options; the script itself still accepts every option."""
import subprocess
import sys
import unittest
from pathlib import Path


ENTRY = Path(__file__).resolve().parents[3] / 'agentcompass'


def run(*argv):
    return subprocess.run([sys.executable, '-X', 'utf8', str(ENTRY), *argv], capture_output=True, text=True,
                          encoding='utf-8', timeout=60)


class StageHelpTests(unittest.TestCase):

    def test_roll_help_is_short_and_names_the_two_task_inputs_first(self):
        text = run('roll', '-h').stdout
        self.assertLess(text.index('--task'), text.index('--hours'))
        self.assertIn('--brief', text)
        self.assertIn('--can-wait-hours', text)
        self.assertNotIn('--resets-in', text)
        self.assertNotIn('--constraint', text)
        for hidden in ('--dry-run', '--access', '--title', '--task-id', '--parent-host',
                       '--parent-id', '--parent-address', '--usage-cache', '--verbose'):
            self.assertNotIn(hidden, text)
        for deleted in ('--wait', '--allow-concurrent'):
            self.assertNotIn(deleted, text)
        hours = text.split('--hours', 1)[1].split('--local-only', 1)[0]
        self.assertIn('Optional', hours)
        self.assertNotIn('required', hours.lower())
        plan_text = run('plan', '-h').stdout
        self.assertIn('--can-wait-hours', plan_text)

    def test_help_all_still_lists_every_option(self):
        text = run('roll', '--help-all').stdout
        for option in ('--resets-in', '--constraint', '--dispatch-action', '--task', '--can-wait-hours',
                       '--access', '--title', '--task-id', '--parent-host', '--parent-id',
                       '--parent-address', '--usage-cache', '--verbose', '--dry-run'):
            self.assertIn(option, text)
        for deleted in ('--wait ', '--allow-concurrent'):
            self.assertNotIn(deleted, text)



if __name__ == '__main__':
    unittest.main()
