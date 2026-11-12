import sys
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import services
import updates


class MpvProvisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        state = mock.patch.dict(os.environ, ROUTER_STATE_DIR=str(self.root / "state"))
        state.start()
        self.addCleanup(state.stop)
        self.addCleanup(updates.wait_usage_float_mpv)
        self.events = []
        updates._progress.callback = self.events.append
        self.addCleanup(lambda: setattr(updates._progress, 'callback', None))

    def script(self, source):
        (self.root / 'mpv_provision.py').write_text(source, encoding='utf-8')

    def worker(self, fn):
        results = []
        def run():
            updates._progress.callback = self.events.append
            results.append(fn())
        thread = threading.Thread(target=run)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        return results[0]

    def test_success_imports_dataclass_module_and_calls_hook(self):
        self.script('from dataclasses import dataclass\nfrom pathlib import Path\n@dataclass\nclass Result:\n    value: str\ndef ensure_mpv():\n    Path(__file__).with_suffix(".done").touch()\n')
        self.assertTrue(self.worker(lambda: updates.provision_usage_float_mpv(self.root)))
        updates.wait_usage_float_mpv()
        self.assertTrue((self.root / 'mpv_provision.done').exists())
        self.assertTrue(self.events)
        self.assertFalse(any(n.startswith('_agentcompass_mpv_') for n in sys.modules))

    def test_missing_module_skipped(self):
        self.assertEqual(updates.provision_usage_float_mpv(self.root), '')
        self.assertEqual(self.events, [])

    def test_failure_is_logged_and_in_install_result(self):
        self.script('def ensure_mpv():\n    raise RuntimeError("network unavailable")\n')
        with mock.patch.object(updates, 'run'), mock.patch.object(services, 'refresh_plugin_roots'), mock.patch.object(updates, 'version', return_value='v1'):
            message, status = self.worker(lambda: (updates.ensure_plugin('usage-float', self.root), updates._progress.status))
        self.assertEqual(status, 'updated')
        self.assertIn('mpv', message)
        updates.wait_usage_float_mpv()
        self.assertIn('network unavailable', (self.root / 'state' / 'mpv-provision.log').read_text(encoding='utf-8'))
        self.assertTrue(any(e['kind'] == 'result' and 'network unavailable' in e['message'] for e in self.events))

    def test_main_thread_returns_while_download_runs(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        fake = mock.Mock()
        def download():
            self.assertIsNot(threading.current_thread(), threading.main_thread())
            started.set()
            release.wait(5)
            finished.set()
        fake.ensure_mpv = download
        with mock.patch.object(updates.importlib.util, 'module_from_spec', return_value=fake), mock.patch.object(updates.importlib.util, 'spec_from_file_location'):
            self.script('')
            message = updates.provision_usage_float_mpv(self.root)
            try:
                self.assertTrue(started.wait(2))
                self.assertFalse(finished.is_set())
                self.assertTrue(message)
            finally:
                release.set()
                self.assertTrue(finished.wait(2))
                for thread in threading.enumerate():
                    if thread.name == 'usage-float-mpv':
                        thread.join(2)

    def test_update_provisions_even_when_current_but_not_when_failed(self):
        (self.root / '.git').mkdir()
        for status, expected in [('current', 1), ('updated', 1), ('failed', 0)]:
            with self.subTest(status=status), mock.patch.object(services, 'USAGE_FLOAT_ROOT', self.root), mock.patch.object(updates, 'update_simple_plugin', side_effect=lambda *a: updates._set_status(status, 'git result')), mock.patch.object(updates, 'provision_usage_float_mpv', return_value='warning') as provision:
                result = updates.update_usage_float()
                self.assertEqual(provision.call_count, expected)
                self.assertEqual(result, 'git result' + ('\uff1bwarning' if expected else ''))
    def test_worker_releases_update_lock_and_single_flight(self):
        started, release = threading.Event(), threading.Event()
        fake = mock.Mock()
        fake.ensure_mpv.side_effect = lambda: (started.set(), release.wait(5))
        self.script('')
        with mock.patch.object(updates.importlib.util, 'module_from_spec', return_value=fake), mock.patch.object(updates.importlib.util, 'spec_from_file_location'):
            def transaction():
                with updates.LOCK:
                    return updates.provision_usage_float_mpv(self.root)
            self.assertTrue(self.worker(transaction))
            try:
                self.assertTrue(started.wait(2))
                self.assertFalse(updates.LOCK.locked())
                self.worker(lambda: updates.provision_usage_float_mpv(self.root))
                self.assertEqual(fake.ensure_mpv.call_count, 1)
                self.assertTrue(updates._mpv_thread.daemon)
            finally:
                release.set()
                updates.wait_usage_float_mpv()

    def test_cli_explicitly_waits_for_background_task(self):
        with mock.patch.object(sys, 'argv', ['updates.py', 'update']), mock.patch.object(updates, 'update_all', return_value='done'), mock.patch.object(updates, 'wait_usage_float_mpv') as wait:
            self.assertEqual(updates.main(), 0)
            wait.assert_called_once()

    def test_plugin_start_preserves_install_message(self):
        with mock.patch.object(services.plugins, 'plugin_root', return_value=self.root), mock.patch.object(updates, 'ensure_plugin', return_value='mpv pending'), mock.patch.object(services, 'refresh_plugin_roots'), mock.patch.object(services, 'start_usage_float', return_value='started') as start:
            self.assertIn('mpv pending', services.plugin_start('usage-float'))
            start.assert_called_once()
    def test_warning_status_is_structured_independent_of_language(self):
        import update_status
        with mock.patch.object(update_status, '_state', {}):
            update_status.record({'kind': 'start'}, directory=self.root / 'state')
            update_status.record({'kind': 'result', 'component': 'Usage Float mpv', 'message': 'opaque localized text', 'status': 'warning'}, directory=self.root / 'state')
            self.assertEqual(update_status._state['statuses']['Usage Float mpv'], 'warning')
    def test_failure_producers_have_explicit_status_in_both_languages(self):
        import i18n
        import cli_updates
        from types import SimpleNamespace
        from contextlib import nullcontext
        for lang in ('zh', 'en'):
            with self.subTest(lang=lang), mock.patch.object(i18n, 'get_language', return_value=lang):
                events = []
                with mock.patch.object(updates, 'ensure_plugin', side_effect=RuntimeError('offline')), mock.patch.object(updates.plugins, 'plugin_root', return_value=self.root):
                    with self.assertRaises(RuntimeError):
                        updates.deploy_gateway('muse2api', False, progress=events.append)
                self.assertEqual([e['status'] for e in events if e['kind'] == 'result'], ['failed'])
                events.clear()
                with mock.patch.object(updates, 'inspect_installs', side_effect=RuntimeError('offline')):
                    with self.assertRaises(RuntimeError):
                        updates.update_agentcompass(progress=events.append)
                self.assertEqual([e['status'] for e in events if e['kind'] == 'result'], ['failed'])
                target = SimpleNamespace(id='codex', name='Codex')
                modules = [SimpleNamespace(override_store=SimpleNamespace(locked=lambda *a, **k: nullcontext()))]
                def failed_update(ident, *args, outcome):
                    outcome.update(status='failed', message='opaque vendor error')
                    return 'opaque vendor error'
                with mock.patch('router_priority.modules', return_value=modules), mock.patch.object(cli_updates, 'targets', return_value=[target]), mock.patch.object(cli_updates, 'unsupported', return_value=False), mock.patch.object(cli_updates, 'update', side_effect=failed_update) as installer, mock.patch('psutil.process_iter', side_effect=AssertionError('must not inspect running sessions')), mock.patch.object(cli_updates, 'state_dir', return_value=self.root), mock.patch.object(cli_updates, '_change_state'), mock.patch.object(cli_updates, 'log'), mock.patch.object(cli_updates, '_record_status', side_effect=events.append):
                    events.clear()
                    result = cli_updates.update_all(home=self.root)
                installer.assert_called_once()
                self.assertEqual([e['status'] for e in events if e['kind'] == 'result'], ['failed'])
                self.assertNotIn('deferred', result)

    def test_missing_result_status_never_means_success(self):
        import update_status
        with mock.patch.object(update_status, '_state', {}):
            update_status.record({'kind': 'start'}, directory=self.root)
            update_status.record({'kind': 'result', 'message': 'opaque'}, directory=self.root)
            self.assertEqual(update_status._state['statuses']['AgentCompass'], 'warning')

    def test_abandon_logs_interruption_and_does_not_wait(self):
        import time
        release = threading.Event()
        started = threading.Event()
        fake = mock.Mock()
        fake.ensure_mpv.side_effect = lambda: (started.set(), release.wait(5))
        self.script('')
        with mock.patch.object(updates.importlib.util, 'module_from_spec', return_value=fake), mock.patch.object(updates.importlib.util, 'spec_from_file_location'):
            updates.provision_usage_float_mpv(self.root)
            try:
                self.assertTrue(started.wait(2))
                before = time.monotonic()
                updates.abandon_usage_float_mpv()
                updates.wait_usage_float_mpv()
                self.assertLess(time.monotonic() - before, .5)
                self.assertIn('interrupted', (self.root / 'state/mpv-provision.log').read_text(encoding='utf-8'))
                with mock.patch.object(services.local_controls, 'read_snapshot', return_value=None):
                    self.assertIn('mpv', services.usage_float_label())
            finally:
                release.set()
                updates._mpv_thread.join(2)
                updates._mpv_abandoned.clear()
    def test_quit_releases_mutex_before_stopping_icon(self):
        from test_app import app
        calls = []
        with mock.patch.object(app, 'log_info'), mock.patch.object(app.updates, 'abandon_usage_float_mpv', side_effect=lambda: calls.append('abandon')), mock.patch.object(app, 'release_single_instance', side_effect=lambda: calls.append('release')), mock.patch.object(app.services, 'stop_usage_collect'), mock.patch.object(app.services, 'stop_capacity'):
            item = app.build_menu().items[-1]
            icon = mock.Mock()
            icon.stop.side_effect = lambda: calls.append('stop')
            item(icon)
        self.assertEqual(calls, ['abandon', 'release', 'stop'])
    def test_panel_waits_for_long_jobs_before_logging_interruption(self):
        import router_window
        from types import SimpleNamespace
        panel = SimpleNamespace(closing=True, long_jobs=[mock.Mock()], root=mock.Mock(), _release_show_event=mock.Mock(), poll=mock.Mock())
        panel.long_jobs[0].is_alive.return_value = True
        with mock.patch.object(router_window.os, '_exit') as exit_process, mock.patch.object(updates, 'abandon_usage_float_mpv') as abandon:
            router_window.Panel.poll(panel)
            exit_process.assert_not_called()
            abandon.assert_not_called()
            panel.long_jobs[0].is_alive.return_value = False
            router_window.Panel.poll(panel)
            abandon.assert_called_once()
            exit_process.assert_called_once_with(0)
