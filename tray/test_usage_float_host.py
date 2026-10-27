import threading
import time
import unittest
from unittest import mock

import deploy
import usage_float_host as host


class HostTests(unittest.TestCase):
    def setUp(self):
        with host._autostart_lock:
            self._saved_autostart = dict(host._autostart_state)
            host._autostart_state.update(
                value=None, checked_at=0.0, refreshing=False, generation=0
            )

    def tearDown(self):
        with host._autostart_lock:
            host._autostart_state.clear()
            host._autostart_state.update(self._saved_autostart)

    def scope(self):
        return {"state": {"refreshing": False, "always_on_top": True, "display_mode": "panel", "panel_active": True},
                **{name: mock.Mock() for name in ("refresh_async", "request_open_settings", "toggle_panel_mode",
                   "toggle_topmost", "do_quit", "set_wallpaper_active", "stack_text_above_plate")},
                "bg_layer": mock.Mock()}

    def test_menu_uses_native_callbacks_and_preserves_panel_background_behavior(self):
        root, scope = mock.Mock(), self.scope()
        with mock.patch.object(host.local_controls, "ControlServer") as server:
            host.attach(root, scope)
        actions = server.return_value.publish.call_args.args[1]
        self.assertIs(actions["settings"], scope["request_open_settings"])
        self.assertIs(actions["panel"], scope["toggle_panel_mode"])
        self.assertIs(actions["quit"], scope["do_quit"])
        actions["refresh"]()
        scope["refresh_async"].assert_called_once_with(force=True)
        actions["show"]()
        scope["bg_layer"].deiconify.assert_not_called()
        root.deiconify.assert_called_once()

    def test_interface_drift_is_reported_before_registering_commands(self):
        with mock.patch.object(host.local_controls, "ControlServer") as server:
            with self.assertRaisesRegex(RuntimeError, "接口变化"):
                host.attach(mock.Mock(), {})
        server.assert_not_called()

    def test_is_autostart_enabled_never_blocks_ui_thread_and_updates_cache_async(self):
        gate = threading.Event()
        done = threading.Event()

        def slow_startup_enabled():
            gate.wait(timeout=1.0)
            done.set()
            return False

        with mock.patch.object(deploy, "startup_enabled", side_effect=slow_startup_enabled):
            t0 = time.perf_counter()
            initial = host.is_autostart_enabled()
            elapsed = time.perf_counter() - t0
            self.assertTrue(initial)
            self.assertLess(elapsed, 0.05)
            gate.set()
            self.assertTrue(done.wait(timeout=1.0))
            for _ in range(20):
                if host._autostart_state["value"] is False:
                    break
                time.sleep(0.01)
            self.assertFalse(host.is_autostart_enabled())

    def test_set_autostart_updates_cached_state_immediately_and_runs_deploy_async(self):
        applied = threading.Event()

        def fake_remove():
            applied.set()

        with mock.patch.object(deploy, "remove_startup", side_effect=fake_remove) as remove_mock, \
                mock.patch.object(deploy, "startup_enabled", return_value=False):
            ok, msg = host.set_autostart(False)
            self.assertTrue(ok)
            self.assertIn("开机启动", msg)
            self.assertFalse(host.is_autostart_enabled())
            self.assertTrue(applied.wait(timeout=1.0))
            remove_mock.assert_called_once()
