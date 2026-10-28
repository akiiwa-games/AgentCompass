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
    def run_case(self, configured=False, installed=False, npm='npm.cmd'):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            if configured:
                (root / '.paseo').mkdir()
                (root / '.paseo/config.json').write_text('{}')
            bridge = root / 'setup/paseo-bridge'
            bridge.mkdir(parents=True)
            if installed:
                (bridge / 'node_modules').mkdir()
            output = io.StringIO()
            with patch.object(bootstrap, 'HOME', root), patch.object(bootstrap, '__file__', str(root / 'setup/sync_host_globals.py')), patch.object(bootstrap.shutil, 'which', side_effect=lambda name: npm if name == 'npm' else None), patch.object(bootstrap.subprocess, 'run') as run, redirect_stdout(output):
                bootstrap.ensure_paseo_bridge_dependencies()
                calls = run.call_args_list
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
        self.assertIn('npm --prefix setup/paseo-bridge ci', output)
        self.assertIn('Continuing other host setup', output)


if __name__ == '__main__':
    unittest.main()
