import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from remote_root import root_command, root_step
import shlex


class RootPreflightTests(unittest.TestCase):
    def probe(self, environment):
        command = root_command()
        # Execute the generated Python preflight without assuming a local POSIX shell.
        code = shlex.split(command.split('$(python3 -c ', 1)[1].rsplit(') &&', 1)[0])[0]
        return subprocess.run([sys.executable, '-c', code], env=environment, capture_output=True, text=True)

    def test_configured_root_is_verified_and_an_unwritable_or_missing_root_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = dict(os.environ, DOT_WORK_ROOT=directory)
            result = self.probe(environment)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(Path(result.stdout.strip()), Path(directory).resolve())
            environment['DOT_WORK_ROOT'] = str(Path(directory) / 'missing')
            result = self.probe(environment)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('No writable work root', result.stderr)

    def test_automatic_root_comes_from_current_environment(self):
        environment = dict(os.environ)
        environment.pop('DOT_WORK_ROOT', None)
        result = self.probe(environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(Path(result.stdout.strip()).is_dir())
        self.assertNotIn('/workspace/shared', root_step('fixture'))


if __name__ == '__main__':
    unittest.main()
