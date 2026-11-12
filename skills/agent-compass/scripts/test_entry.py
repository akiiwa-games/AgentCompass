import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

import entry

pytestmark = pytest.mark.slow




class EntryTests(unittest.TestCase):
    def test_generated_checkout_continuation_runs_the_real_public_entry(self):
        with TemporaryDirectory(prefix='continuation cwd ') as directory:
            done = subprocess.run(entry.argv('usage', '-h'), cwd=directory,
                                  capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('agentcompass usage', done.stdout)
        self.assertNotIn('usage_collector.py', done.stdout)



    def test_a_bare_skill_copy_falls_back_to_the_console_script(self):
        with TemporaryDirectory() as directory:
            scripts = Path(directory) / 'a' / 'b' / 'c' / 'scripts'
            scripts.mkdir(parents=True)
            copy = scripts / 'entry.py'
            copy.write_text(Path(entry.__file__).read_text(encoding='utf-8'), encoding='utf-8')
            code = ('import importlib.util, json\n'
                    f'spec = importlib.util.spec_from_file_location("entry", {str(copy)!r})\n'
                    'module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)\n'
                    'print(json.dumps(module.argv("launch")))\n')
            done = subprocess.run([sys.executable, '-I', '-c', code], capture_output=True, text=True, timeout=60)
            self.assertEqual(done.stdout.strip(), '["agentcompass", "launch"]', done.stderr)


if __name__ == '__main__':
    unittest.main()
