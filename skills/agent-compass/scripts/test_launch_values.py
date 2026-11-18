"""Rule (router-internals §5, Launch values): no launch passes a value through cmd.exe. A Windows batch launcher (an npm
`.cmd` shim) is started as the program its launch line runs (launch_values.program), so a value holding cmd.exe syntax
(`&`, `|`, `^`, `%VAR%`, `>`) reaches the program as one argument and runs nothing; a launcher that cannot be read that
way refuses the launch, stated. The fake shims and the recording program live in a temporary directory."""
import argparse
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import host_herdr
import launch_values
import public_data
import roll
import scorer_client
import subscription_cli

RECORDER = """import json, os, sys
from pathlib import Path
with open(Path(__file__).with_name('argv.jsonl'), 'a', encoding='utf-8') as out:
    out.write(json.dumps({'argv': sys.argv[1:], 'shim_env': os.environ.get('SHIM_SET')}) + '\\n')
print('{}')
"""
# The shape npm's cmd-shim writes (the program in `_prog`, the script beside `%dp0%`), with a variable of its own.
NPM_SHIM = """@ECHO off\r
GOTO start\r
:find_dp0\r
SET dp0=%~dp0\r
EXIT /b\r
:start\r
SETLOCAL\r
CALL :find_dp0\r
IF EXIST "%dp0%\\node.exe" (\r
  SET "_prog=%dp0%\\node.exe"\r
) ELSE (\r
  SET "_prog={python}"\r
  SET PATHEXT=%PATHEXT:;.JS;=;%\r
)\r
\r
endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\\record.py" %*\r
"""
DESKTOP_SHIM = """@echo off\r
setlocal\r
set "SCRIPT_DIR=%~dp0"\r
set "SHIM_SET=from-the-launcher"\r
"{python}" "%SCRIPT_DIR%record.py" node-script %*\r
exit /b %errorlevel%\r
"""
# The reviewer's probe shim: it hands %* to cmd.exe itself, so it cannot be read as a program and its arguments.
UNREADABLE_SHIM = '@echo off\r\necho %* > "%~dp0args-%~n0.txt"\r\n'


class BatchLauncherTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix='launch-values-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(self.folder, ignore_errors=True))
        (self.folder / 'record.py').write_text(RECORDER, encoding='utf-8')
        self.marker = self.folder / 'INJECTED.txt'
        self.hostile = [f'm&echo.pwned>{self.marker}', f'm|echo.pwned>{self.marker}', f'm^&echo.pwned>{self.marker}',
                        'm%PWN%', f'm>{self.marker}']
        env = patch.dict(os.environ, {'PATH': str(self.folder) + os.pathsep + os.environ.get('PATH', ''),
                                      'PWN': 'EXPANDED'})
        env.start()
        self.addCleanup(env.stop)

    def shim(self, name, text):
        path = self.folder / f'{name}.CMD'
        path.write_bytes(text.replace('{python}', sys.executable).encode('ascii'))
        return path

    def recorded(self):
        path = self.folder / 'argv.jsonl'
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []

    def assert_nothing_injected(self):
        self.assertFalse(self.marker.exists(), 'a value ran as a command')
        self.assertFalse(any(p.name.startswith('pwned') for p in self.folder.iterdir()))

    def test_a_shim_is_read_as_its_program_and_script(self):
        shim = self.shim('pi', NPM_SHIM)
        argv, env = launch_values.program(shim)
        self.assertEqual([os.path.normcase(a) for a in argv],
                         [os.path.normcase(sys.executable), os.path.normcase(str(self.folder / 'record.py'))])
        self.assertEqual(env, {})                    # dp0 and _prog are consumed; PATHEXT's substring edit is not a value
        argv, env = launch_values.program(self.shim('paseo', DESKTOP_SHIM))
        self.assertEqual(argv[2], 'node-script')
        self.assertEqual(env, {'SHIM_SET': 'from-the-launcher'})
        self.assertEqual(launch_values.program(self.folder / 'tool.exe'), ([str(self.folder / 'tool.exe')], {}))

    def test_the_cli_worker_passes_every_value_as_one_argument(self):
        """roll.cli for the npm-shim CLIs (pi, opencode): the model and the prompt reach the program unchanged."""
        for family in ('pi', 'opencode'):
            self.shim(family, NPM_SHIM)
        started = []
        real = roll.subprocess.Popen

        def popen(*args, **kwargs):
            started.append(real(*args, **kwargs))
            return started[-1]
        out = {'parent_id': 'p', 'parent_host': 'paseo', 'parent_address': 'p'}
        with patch.object(roll, 'provider_definition', lambda provider, bundle=None: (provider, {})), \
                patch.object(roll.host_placement, 'place', lambda *a, **k: {'cwd': str(self.folder)}), \
                patch.object(roll, 'CLI_RUNS_DIR', self.folder / 'runs'), \
                patch.object(roll.subprocess, 'Popen', popen):
            for family in ('pi', 'opencode'):
                for value in self.hostile:
                    result, code = roll.cli(argparse.Namespace(workspace=None), out,
                                            {'provider': family, 'model': value}, f'task {value}')
                    self.assertEqual((result['launched'], code), (True, 0), result)
                    started[-1].wait(30)
        seen = [line['argv'] for line in self.recorded()]
        self.assertEqual(len(seen), 2 * len(self.hostile))
        for value in self.hostile:
            self.assertTrue(any(any(arg.endswith(value) for arg in argv) for argv in seen), value)
        self.assertNotIn('EXPANDED', json.dumps(seen))
        self.assert_nothing_injected()

    def test_the_judge_and_scorer_command_passes_values_unchanged(self):
        """subscription_cli.cli_prefix (the judge's and the scorer's command) on an npm shim."""
        self.shim('claude', NPM_SHIM)
        prefix = subscription_cli.cli_prefix('claude')
        for value in self.hostile:
            code, _out, _err = subscription_cli.run_process([*prefix, '--model', value], str(self.folder), prompt='x',
                                                            timeout=30)
            self.assertEqual(code, 0)
        self.assertEqual([line['argv'][-1] for line in self.recorded()], self.hostile)
        self.assert_nothing_injected()

    def test_herdr_core_runs_as_its_program(self):
        self.shim('hc', NPM_SHIM)
        for value in self.hostile:
            host_herdr.hc('terminal', 'spawn', '--command', value)
        self.assertEqual([line['argv'][-1] for line in self.recorded()], self.hostile)
        self.assert_nothing_injected()

    def test_a_launcher_that_cannot_be_read_refuses_the_launch_and_runs_nothing(self):
        for family in ('pi', 'opencode', 'claude'):
            self.shim(family, UNREADABLE_SHIM)
        with self.assertRaisesRegex(launch_values.UnreadableLauncher, 'goes on after %\\*'):
            subscription_cli.cli_prefix('claude')
        out = {'parent_id': 'p', 'parent_host': 'paseo', 'parent_address': 'p'}
        before = public_data._EFFECTS[0]
        with patch.object(roll, 'provider_definition', lambda provider, bundle=None: (provider, {})), \
                patch.object(roll.host_placement, 'place', lambda *a, **k: {'cwd': str(self.folder)}), \
                patch.object(roll, 'CLI_RUNS_DIR', self.folder / 'runs'), \
                patch.object(roll.subprocess, 'Popen', side_effect=AssertionError('started')):
            result, code = roll.cli(argparse.Namespace(workspace=None), out, {'provider': 'pi', 'model': self.hostile[0]},
                                    'task')
        self.assertEqual((result['launched'], code), (False, 1))
        self.assertIn('cannot be started without cmd.exe', result['next'])
        self.assertEqual(public_data._EFFECTS[0], before)                     # refused before the launch's effect
        with patch.dict(scorer_client.SCORER, {'provider': 'claude'}), \
                self.assertRaisesRegex(scorer_client.ScorerUnavailable, 'cannot be started without cmd.exe'):
            scorer_client.scorer_prefix()
        self.assertEqual(self.recorded(), [])
        self.assertFalse(list(self.folder.glob('args-*.txt')))
        self.assert_nothing_injected()


if __name__ == '__main__':
    unittest.main()
