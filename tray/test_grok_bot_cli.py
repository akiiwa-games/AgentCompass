import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import grok_bot_cli as cli
import plugins
import router_window
import updates


class GrokBotIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.package = Path(self.directory.name)
        self.binary = self.package / 'node_modules/grok-bot-cli/dist/bin/gbot.mjs'
        self.binary.parent.mkdir(parents=True)
        self.binary.write_text('synthetic', encoding='utf-8')
        for patch in (mock.patch.object(cli, 'PACKAGE', self.package),
                      mock.patch.object(cli, '_connection', None),
                      mock.patch.object(cli.shutil, 'which', side_effect=lambda name: name)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_local_status_does_not_call_gateway_and_distinguishes_repair(self):
        with mock.patch.object(cli, '_run', return_value=mock.Mock(returncode=0)) as run:
            self.assertEqual(cli.snapshot()['label'], '已安装 · 连接未检查')
            self.assertEqual(run.call_args.args[0], ['node', str(self.package / 'patch-descriptor-v3.mjs'), '--check'])
            run.return_value.returncode = 1
            self.assertEqual(cli.snapshot()['label'], 'CLI 需要修复')
            self.binary.unlink()
            run.reset_mock()
            self.assertEqual(cli.snapshot()['label'], 'CLI 未安装')
            run.assert_not_called()

    def test_install_uses_lockfile_then_patch_and_stops_after_failure(self):
        with mock.patch.object(updates, 'run') as run:
            cli.install()
            self.assertEqual(run.call_args_list, [
                mock.call(['npm', 'ci', '--ignore-scripts', '--no-audit', '--no-fund'], self.package),
                mock.call(['npm', 'run', 'patch:descriptor-v3'], self.package)])
            run.reset_mock()
            run.side_effect = RuntimeError('fixture install failure')
            with self.assertRaises(RuntimeError):
                cli.install()
            self.assertEqual(run.call_count, 1)

    def test_connection_checks_only_doctor_and_retain_results_without_exposing_output(self):
        for result, available in [
            (mock.Mock(returncode=0, stdout=json.dumps({'status': 'ready', 'bots': [{'id': 'synthetic-private-id'}]})), True),
            (mock.Mock(returncode=1, stdout=json.dumps({'error': 'synthetic-private-id',
                'diagnostic': {'category': 'http_error', 'operation': 'listAgents', 'status': 417}})), False),
            (mock.Mock(returncode=0, stdout='[]'), False),
            (subprocess.TimeoutExpired('fixture', 90), False),
        ]:
            with self.subTest(available=available), \
                    mock.patch.object(cli, '_run', side_effect=[mock.Mock(returncode=0), result, mock.Mock(returncode=0), mock.Mock(returncode=0)]) as run:
                message = cli.check()
                self.assertNotIn('synthetic-private-id', message)
                self.assertEqual(run.call_args.args[0][-2:], [str(cli.RUNNER), 'doctor'])
                reading = cli.snapshot()
                self.assertEqual(reading['kind'], 'ok' if available else 'warn')
                self.assertIn('已安装', reading['label'])
                with mock.patch.object(cli.time, 'monotonic', return_value=cli._connection[0] + 61):
                    refreshed = cli.snapshot()
                    self.assertEqual(refreshed['label'], reading['label'])
                    self.assertTrue(refreshed['due'])

    def test_probe_deduplicates_and_preserves_result_during_recheck(self):
        cli._connection = (0, False, 'HTTP 417')
        with mock.patch.object(cli, 'snapshot', return_value={'installed': True, 'due': True}):
            def transport(*args):
                self.assertEqual(cli._connection, (0, False, 'HTTP 417'))
                self.assertIn('HTTP 417', cli.check())  # overlapping call joins last result, no new subprocess
                return mock.Mock(returncode=0, stdout='{"status":"ready"}')
            with mock.patch.object(cli, '_run', side_effect=transport) as run:
                cli.check()
                self.assertEqual(run.call_count, 1)
        with mock.patch.object(cli, 'snapshot', return_value={'installed': True, 'due': False}), \
                mock.patch.object(cli, '_run') as run:
            cli.check(force=False)
            run.assert_not_called()
        with mock.patch.object(cli, 'snapshot', return_value={'label': 'CLI 未安装'}), \
                mock.patch.object(cli, '_run') as run:
            self.assertEqual(cli.check(), 'CLI 未安装')
            run.assert_not_called()

    def test_ui_uses_only_allowlisted_diagnostic_fields(self):
        for diagnostic, expected in [
            ({'category': 'http_error', 'operation': 'listAgents', 'status': 417, 'body': 'synthetic-secret'}, 'HTTP 417'),
            ({'category': 'http_error', 'operation': 'synthetic-secret', 'status': 417}, '原因尚未确定'),
            ({'category': 'http_error', 'operation': 'listAgents', 'status': '417 synthetic-secret'}, '原因尚未确定'),
        ]:
            with mock.patch.object(cli, 'snapshot', return_value={'installed': True}), \
                    mock.patch.object(cli, '_run', return_value=mock.Mock(returncode=1, stdout=json.dumps({'diagnostic': diagnostic}))):
                message = cli.check()
                self.assertIn(expected, message)
                self.assertNotIn('synthetic-secret', message)

    def test_real_integration_page_card_actions_and_failed_connection_render(self):
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        with mock.patch.object(router_window.Panel, 'run'), \
                mock.patch.object(router_window.Panel, 'prefill'), \
                mock.patch.object(router_window.theme, 'set_window_icon'), \
                mock.patch.object(plugins, 'load', return_value=plugins.defaults()):
            panel = router_window.Panel(root, 'plugins')
            self.addCleanup(lambda: root.after_cancel(panel._poll_id))
            self.assertEqual(panel.current, 'plugins')
            self.assertIn('grok-bot', panel.plugin_widgets)
            self.assertEqual(panel.plugin_actions('grok-bot'), (
                ('安装/修复', cli.install), ('检查连接', cli.check), ('使用说明', cli.guide)))
            card = panel.plugin_widgets['grok-bot']['status'].master.master
            def widgets(parent):
                for child in parent.winfo_children():
                    yield child
                    yield from widgets(child)
            controls = list(widgets(card))
            self.assertIn('Grok Bot CLI', [w.cget('text') for w in controls if 'text' in w.keys()])
            self.assertFalse(any(w.winfo_class() == 'TCheckbutton' for w in controls))
            # Render an unavailable gateway through the actual page callback, independent of plugin toggles.
            config = plugins.defaults()
            config['grok-bot']['enabled'] = False
            with mock.patch.object(plugins, 'PLUGINS', (plugins.BY_ID['grok-bot'],)):
                with mock.patch.object(root, 'after', return_value='fixture-timer') as timer:
                    panel.plugins_loaded((config, {'grok-bot': '已安装 · 连接不可用'},
                                          {'grok-bot': {'kind': 'warn', 'installed': True, 'retry_in': 60}}))
            status = panel.plugin_widgets['grok-bot']['status']
            self.assertIn('已安装 · 连接不可用', status.cget('text'))
            self.assertEqual(status.cget('fg'), router_window.theme.STATUS_COLORS['warn'][0])
            timer.assert_called_once()
            with mock.patch.object(root, 'after_cancel'), mock.patch.object(cli, 'check', return_value='fixture') as probe:
                panel.run.reset_mock()
                timer.call_args.args[1]()
                panel.check_grok_bot(force=True)
                self.assertEqual(panel.run.call_count, 1)
                self.assertIn('连接不可用', status.cget('text'))
                work, done = panel.run.call_args.args[:2]
                self.assertEqual(work(), 'fixture')
                probe.assert_called_once_with(force=False)
                with mock.patch.object(panel, 'load_plugins') as refresh:
                    done('fixture')
                    refresh.assert_called_once()

    def test_initial_page_probe_missing_installation_and_close_timer(self):
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        with mock.patch.object(router_window.Panel, 'run'), \
                mock.patch.object(router_window.Panel, 'prefill'), \
                mock.patch.object(router_window.theme, 'set_window_icon'), \
                mock.patch.object(plugins, 'load', return_value=plugins.defaults()):
            panel = router_window.Panel(root, 'plugins')
            config = plugins.defaults()
            with mock.patch.object(plugins, 'PLUGINS', (plugins.BY_ID['grok-bot'],)), \
                    mock.patch.object(panel, 'check_grok_bot') as check:
                panel.plugins_loaded((config, {'grok-bot': '已安装'},
                                      {'grok-bot': {'kind': 'idle', 'installed': True, 'due': True}}))
                check.assert_called_once_with()
                check.reset_mock()
                panel.plugins_loaded((config, {'grok-bot': 'CLI 未安装'}, {'grok-bot': {'kind': 'idle'}}))
                check.assert_not_called()
            with mock.patch.object(panel, 'load_plugins') as refresh:
                panel.plugin_act('grok-bot', cli.install)
                work, done = panel.run.call_args.args[:2]
                self.assertIs(work, cli.install)
                done('installed fixture')
                refresh.assert_called_once()
            timer = root.after(60000, lambda: None)
            panel._grok_status_timer = timer
            panel.show('scoring')
            self.assertIsNone(panel._grok_status_timer)
            panel.run.reset_mock()
            panel.check_grok_bot()
            panel.run.assert_not_called()
            panel._grok_status_timer = root.after(60000, lambda: None)
            with mock.patch.object(router_window.os, '_exit'):
                panel.close()
            self.assertIsNone(panel._grok_status_timer)
            panel.check_grok_bot()
            panel.run.assert_not_called()

    def test_delayed_older_load_cannot_overwrite_new_connection_result(self):
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        with mock.patch.object(router_window.Panel, 'run'), \
                mock.patch.object(router_window.Panel, 'prefill'), \
                mock.patch.object(router_window.theme, 'set_window_icon'), \
                mock.patch.object(plugins, 'load', return_value=plugins.defaults()):
            panel = router_window.Panel(root, 'plugins')
            self.addCleanup(lambda: root.after_cancel(panel._poll_id))
            panel.load_plugins()
            old_done = panel.run.call_args.args[1]
            panel.load_plugins()
            new_done = panel.run.call_args.args[1]
            config = plugins.defaults()
            cli._connection = (0, True, '')
            with mock.patch.object(plugins, 'PLUGINS', (plugins.BY_ID['grok-bot'],)), \
                    mock.patch.object(panel, 'check_grok_bot') as check:
                new_done((config, {'grok-bot': '已安装 · 连接可用'},
                          {'grok-bot': {'kind': 'ok', 'installed': True, 'retry_in': 60}}))
                timer = panel._grok_status_timer
                old_done((config, {'grok-bot': '已安装 · 连接不可用'},
                          {'grok-bot': {'kind': 'warn', 'installed': True, 'due': True}}))
                status = panel.plugin_widgets['grok-bot']['status']
                self.assertIn('连接可用', status.cget('text'))
                self.assertEqual(status.cget('fg'), router_window.theme.STATUS_COLORS['ok'][0])
                self.assertEqual(panel._grok_status_timer, timer)
                check.assert_not_called()
                panel.stop_grok_check_timer()


if __name__ == '__main__':
    unittest.main()
