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
                self.assertFalse(updates._mpv_thread.daemon)
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
