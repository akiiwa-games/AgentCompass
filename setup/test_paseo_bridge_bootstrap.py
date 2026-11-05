"""Bootstrap checks run in temporary homes; never change host configuration."""
import importlib.util
import io
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('host_globals_bootstrap', Path(__file__).with_name('sync_host_globals.py'))
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class PaseoBridgeBootstrapTests(unittest.TestCase):
    def run_case(self, configured=False, installed=False, npm='npm.cmd', checkout=True, inspect=None):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            if checkout:
                (root / '.git').mkdir()
            if configured:
                (root / '.paseo').mkdir()
                (root / '.paseo/config.json').write_text('{}')
            bridge = root / 'setup/paseo-bridge'
            bridge.mkdir(parents=True)
            (bridge / 'package.json').write_text('{"name": "bridge"}')
            (bridge / 'package-lock.json').write_text('{"lockfileVersion": 3}')
            if installed:
                (bridge / 'node_modules').mkdir()
            output = io.StringIO()
            with patch.object(bootstrap, 'HOME', root), patch.object(bootstrap, '__file__', str(root / 'setup/sync_host_globals.py')), patch.object(bootstrap.shutil, 'which', side_effect=lambda name: npm if name == 'npm' else None), patch.object(bootstrap.subprocess, 'run') as run, redirect_stdout(output):
                bootstrap.ensure_paseo_bridge_dependencies()
                calls = run.call_args_list
                if inspect:
                    inspect(root)
            return calls, output.getvalue(), bridge

    def test_non_paseo_host_does_not_require_node(self):
        self.assertEqual(self.run_case()[0], [])

    def test_existing_dependencies_are_kept(self):
        self.assertEqual(self.run_case(configured=True, installed=True)[0], [])

    def test_missing_dependencies_use_locked_install(self):
        calls, _, bridge = self.run_case(configured=True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args, (['npm.cmd', 'ci'],))
        self.assertEqual(calls[0].kwargs, {'cwd': bridge, 'check': True})

    def test_missing_npm_prints_manual_step_and_continues(self):
        calls, output, _ = self.run_case(configured=True, npm=None)
        self.assertEqual(calls, [])
        self.assertIn('agentcompass setup', output)
        self.assertIn('Continuing other host setup', output)

    def test_an_installed_wheel_installs_under_the_state_directory_not_site_packages(self):
        seen = {}

        def inspect(root):
            target = root / '.agent-compass/paseo-bridge'
            seen['lock'] = (target / 'package-lock.json').read_text()
            seen['target'] = target

        calls, _, bridge = self.run_case(configured=True, checkout=False, inspect=inspect)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].kwargs['cwd'], seen['target'])
        self.assertEqual(seen['lock'], '{"lockfileVersion": 3}')

    def test_an_upgraded_wheel_with_a_new_lock_reinstalls(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / '.paseo').mkdir()
            (root / '.paseo/config.json').write_text('{}')
            source = root / 'setup/paseo-bridge'
            source.mkdir(parents=True)
            target = root / '.agent-compass/paseo-bridge'
            (target / 'node_modules').mkdir(parents=True)
            for name, old, new in (('package.json', '{}', '{}'), ('package-lock.json', '{"v": 1}', '{"v": 2}')):
                (target / name).write_text(old)
                (source / name).write_text(new)
            with patch.object(bootstrap, 'HOME', root), patch.object(bootstrap, '__file__', str(root / 'setup/sync_host_globals.py')),                     patch.object(bootstrap.shutil, 'which', side_effect=lambda name: 'npm.cmd' if name == 'npm' else None),                     patch.object(bootstrap.subprocess, 'run') as run, redirect_stdout(io.StringIO()):
                bootstrap.ensure_paseo_bridge_dependencies()
                self.assertEqual(run.call_args.kwargs['cwd'], target)
                self.assertEqual((target / 'package-lock.json').read_text(), '{"v": 2}')
                run.reset_mock()
                bootstrap.ensure_paseo_bridge_dependencies()
                run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
