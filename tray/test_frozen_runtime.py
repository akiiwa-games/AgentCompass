"""Frozen launch boundaries must never infer a sibling python.exe from AgentCompass.exe."""
import importlib.machinery
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import component_launch as launch
import deploy
import easytier_setup
import grok_bot_cli
import owned_process


class FrozenRuntimeTests(unittest.TestCase):
    def setUp(self):
        for patch in (mock.patch.object(sys, 'frozen', True, create=True),
                      mock.patch.object(sys, 'executable', 'C:/Apps/Agent Compass/AgentCompass.exe')):
            patch.start()
            self.addCleanup(patch.stop)

    def test_startup_registration_and_detection_use_executable_without_script_argument(self):
        with mock.patch.object(deploy, 'powershell') as ps:
            deploy.write_startup()
            script = ps.call_args_list[0].args[0]
        self.assertIn("-Execute 'C:/Apps/Agent Compass/AgentCompass.exe'", script)
        self.assertNotIn('-Argument', script)
        self.assertNotIn('python', script)
        self.assertTrue(deploy.startup_ok((sys.executable, '')))
        self.assertFalse(deploy.startup_ok((sys.executable, '--run-component router_window')))
        with mock.patch.object(deploy, 'powershell', return_value=mock.Mock(stdout=sys.executable + '\n')):
            self.assertEqual(deploy.startup_fields(), (sys.executable, ''))
        env = deploy.wanted_env()
        self.assertEqual(env['PRO_CONSULT_LAUNCHER'], sys.executable)
        self.assertEqual(env['PRO_CONSULT_LAUNCHER_ARGS'], '--run-component web_launcher')

    def test_only_main_tray_at_this_executable_is_restarted(self):
        commands = [[sys.executable], [sys.executable, '--attach-only'],
                    [sys.executable, '--run-component', 'deploy', 'restart'],
                    ['C:/Other/AgentCompass.exe'], ['pythonw.exe', str(deploy.APP)]]
        listing = [mock.Mock(info={'cmdline': cmd, 'name': Path(cmd[0]).name}) for cmd in commands]
        with mock.patch.object(deploy.psutil, 'process_iter', return_value=listing):
            self.assertEqual(deploy.tray_processes(), listing[:2])
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(deploy.services, 'STATE_DIR', Path(folder)), \
                mock.patch.object(deploy, 'tray_processes', side_effect=[listing[:1], listing[1:2]]), \
                mock.patch.object(deploy, 'close_tray') as close, \
                mock.patch.object(deploy.subprocess, 'Popen') as popen, mock.patch.object(deploy.time, 'sleep'):
            deploy.restart()
        close.assert_called_once_with(listing[0])
        self.assertEqual(popen.call_args.args[0], [sys.executable])

    def test_owned_command_preserves_arguments_and_waits_for_job_release(self):
        command = ['cmd.exe', '/c', 'echo a b & exit /b 3']
        self.assertEqual(owned_process.helper_command(command),
                         [sys.executable, '--run-component', 'owned_command', *command])
        import owned_command
        with mock.patch.object(owned_command.sys, 'stdin', mock.Mock(buffer=mock.Mock(read=mock.Mock(return_value=b'')))), \
                mock.patch.object(owned_command.subprocess, 'call') as call:
            self.assertEqual(owned_command.main(), 125)
            call.assert_not_called()
        with mock.patch.object(owned_command.sys, 'stdin', mock.Mock(buffer=mock.Mock(read=mock.Mock(return_value=b'1')))), \
                mock.patch.object(owned_command.sys, 'argv', ['owned_command', *command]), \
                mock.patch.object(owned_command.subprocess, 'call', return_value=3) as call:
            self.assertEqual(owned_command.main(), 3)
            call.assert_called_once_with(command, stdin=subprocess.DEVNULL)

    def test_easytier_helper_task_uses_component_and_keeps_quoted_arguments(self):
        with mock.patch.object(easytier_setup, 'register_task') as register:
            easytier_setup.register_helper('test', ['recover', '--log', 'C:/a b/log.txt'], 2)
        xml = register.call_args.args[1]
        self.assertIn(sys.executable, xml)
        self.assertIn('--run-component easytier_setup recover', xml)
        self.assertIn('C:/a b/log.txt', xml)
        self.assertNotIn('python', xml)

    def test_grok_external_runtime_requirement_is_explained_without_spawning(self):
        with mock.patch.object(grok_bot_cli, '_run') as run:
            self.assertEqual(grok_bot_cli.snapshot()['kind'], 'warn')
            self.assertIn('Python', grok_bot_cli.install())
            self.assertIn('Python', grok_bot_cli.check())
            run.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, 'Python'):
            grok_bot_cli.python()

    def test_menu_restart_launches_independent_frozen_helper(self):
        loader = importlib.machinery.SourceFileLoader('frozen_test_app', str(Path(__file__).with_name('app.pyw')))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        app = importlib.util.module_from_spec(spec)
        loader.exec_module(app)
        with mock.patch.object(subprocess, 'Popen') as popen:
            app.restart_tray()
        self.assertEqual(popen.call_args.args[0], [sys.executable, '--run-component', 'deploy', 'restart'])
        with mock.patch.object(sys, 'frozen', False), mock.patch.object(sys, 'executable', 'C:/Python/python.exe'), \
                mock.patch.object(subprocess, 'Popen') as popen:
            app.restart_tray()
        self.assertEqual(popen.call_args.args[0], [str(Path('C:/Python/pythonw.exe')),
                         str(Path(launch.__file__).with_name('deploy.py')), 'restart'])
        with mock.patch.object(app.i18n, 'get_language', return_value='en'):
            self.assertIn('Restart tray', [item.text for item in app.build_menu().items])


if __name__ == '__main__':
    unittest.main()
