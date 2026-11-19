"""Rule (router-internals §5, Launch values): the tray starts no program through cmd.exe. A batch launcher (an npm `.cmd`
shim, Paseo's `paseo.cmd`) runs as the program its launch line names, with the variables it sets (owned_process.direct);
one that cannot be read that way is refused before anything starts."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dot_reconnect
import owned_process
import updates

DESKTOP_SHIM = """@echo off\r
setlocal\r
set "SCRIPT_DIR=%~dp0"\r
set "SHIM_SET=from-the-launcher"\r
"{python}" "%SCRIPT_DIR%record.py" node-script %*\r
exit /b %errorlevel%\r
"""
UNREADABLE_SHIM = '@echo off\r\necho %* > "%~dp0args-%~n0.txt"\r\n'
HOSTILE = 'm&echo.pwned>INJECTED.txt'


class TrayBatchLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='tray launchers ')
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.desktop = self.folder / 'paseo.cmd'
        self.desktop.write_bytes(DESKTOP_SHIM.replace('{python}', sys.executable).encode('ascii'))
        self.unreadable = self.folder / 'npm.cmd'
        self.unreadable.write_bytes(UNREADABLE_SHIM.encode('ascii'))

    def test_a_launcher_runs_as_its_program_with_its_variables(self):
        argv, env = owned_process.direct([str(self.desktop), 'provider', 'models', HOSTILE], {'BASE': '1'})
        self.assertEqual([os.path.normcase(argv[0]), argv[2:]],
                         [os.path.normcase(sys.executable), ['node-script', 'provider', 'models', HOSTILE]])
        self.assertEqual((env['SHIM_SET'], env['BASE']), ('from-the-launcher', '1'))
        self.assertEqual(owned_process.direct([sys.executable, HOSTILE], None), ([sys.executable, HOSTILE], None))

    def test_a_launcher_that_cannot_be_read_starts_nothing(self):
        with patch.object(owned_process.subprocess, 'Popen', side_effect=AssertionError('started')):
            with self.assertRaisesRegex(owned_process.OwnedProcessError, 'cannot be started without cmd.exe'):
                owned_process.run([str(self.unreadable), 'install', HOSTILE], timeout=5)
        with patch.object(updates.subprocess, 'Popen', side_effect=AssertionError('started')):
            with self.assertRaisesRegex(RuntimeError, 'cannot be started without cmd.exe'):
                updates.run([str(self.unreadable), 'install'], self.folder)

    def test_dot_reconnect_starts_node_as_its_program(self):
        started = []
        with patch.object(dot_reconnect.shutil, 'which', return_value=str(self.desktop)), \
                patch.object(dot_reconnect.subprocess, 'run',
                             lambda argv, **kwargs: started.append((argv, kwargs.get('env'))) or
                             subprocess.CompletedProcess(argv, 0, '{"ok": true}', '')):
            dot_reconnect.run_node(HOSTILE)
        argv, env = started[0]
        self.assertEqual(os.path.normcase(argv[0]), os.path.normcase(sys.executable))
        self.assertIn(HOSTILE, argv)
        self.assertEqual(env['SHIM_SET'], 'from-the-launcher')
        with patch.object(dot_reconnect.shutil, 'which', return_value=str(self.unreadable)), \
                patch.object(dot_reconnect.subprocess, 'run', side_effect=AssertionError('started')):
            self.assertEqual(dot_reconnect.run_node(HOSTILE)['ok'], False)


if __name__ == '__main__':
    unittest.main()
