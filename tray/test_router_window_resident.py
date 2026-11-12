"""The tray keeps one router window process warm and hidden, so opening the window is only a show."""
import os
import unittest
from unittest import mock

import pytest

import tk_test_support


class OpenWindowTests(unittest.TestCase):
    def setUp(self):
        import router_window

        self.rw = router_window
        patcher = mock.patch.object(router_window, "_process", None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_opening_shows_the_warm_window_instead_of_starting_another(self):
        with mock.patch("win32gui.FindWindow", return_value=0), \
                mock.patch.object(self.rw, "signal_warm_window", return_value=True) as signal, \
                mock.patch.object(self.rw.subprocess, "Popen") as popen:
            self.rw.open_window()
        signal.assert_called_once_with()
        popen.assert_not_called()

    def test_opening_without_a_warm_window_starts_one_owned_by_the_caller(self):
        with mock.patch("win32gui.FindWindow", return_value=0), \
                mock.patch.object(self.rw, "signal_warm_window", return_value=False), \
                mock.patch.object(self.rw.subprocess, "Popen") as popen:
            self.rw.open_window()
        argv = popen.call_args.args[0]
        self.assertEqual(argv[argv.index("--parent") + 1], str(os.getpid()))
        self.assertNotIn("--hidden", argv)

    def test_a_hidden_window_is_not_restored_behind_tk_s_back(self):
        # FindWindow also finds the withdrawn warm window; showing it through Win32 would desync Tk's state.
        with mock.patch("win32gui.FindWindow", return_value=1234), \
                mock.patch("win32gui.IsWindowVisible", return_value=False), \
                mock.patch("win32gui.ShowWindow") as show, \
                mock.patch.object(self.rw, "signal_warm_window", return_value=True), \
                mock.patch.object(self.rw.subprocess, "Popen") as popen:
            self.rw.open_window()
        show.assert_not_called()
        popen.assert_not_called()

    def test_prewarm_starts_one_hidden_window(self):
        with mock.patch("win32gui.FindWindow", return_value=0), \
                mock.patch.object(self.rw, "warm_window_running", return_value=False), \
                mock.patch.object(self.rw.subprocess, "Popen") as popen:
            self.rw.prewarm()
        argv = popen.call_args.args[0]
        self.assertIn("--hidden", argv)
        self.assertEqual(argv[argv.index("--parent") + 1], str(os.getpid()))
        with mock.patch("win32gui.FindWindow", return_value=0), \
                mock.patch.object(self.rw, "warm_window_running", return_value=True), \
                mock.patch.object(self.rw.subprocess, "Popen") as popen:
            self.rw.prewarm()
        popen.assert_not_called()


@pytest.mark.slow
class ResidentPanelTests(unittest.TestCase):
    def make_panel(self):
        import router_window
        import panel_plugins
        import panel_widgets

        root = tk_test_support.shared_tk_root(self)
        root.withdraw()
        patches = [mock.patch.object(router_window.Panel, "run"),
                   mock.patch.object(router_window.Panel, "prefill"),
                   mock.patch.object(router_window.theme, "set_window_icon"),
                   mock.patch.object(panel_plugins.plugins, "load", return_value=panel_plugins.plugins.defaults()),
                   mock.patch.object(panel_widgets, "log_error")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        panel = router_window.Panel(root)
        self.addCleanup(lambda: panel._poll_id and root.after_cancel(panel._poll_id))
        # Never put a window on the desktop from a test.
        for name in ("deiconify", "lift", "focus_force"):
            patcher = mock.patch.object(root, name)
            patcher.start()
            self.addCleanup(patcher.stop)
        return router_window, root, panel

    def test_closing_a_resident_window_hides_it_and_showing_it_again_refreshes(self):
        rw, root, panel = self.make_panel()
        panel.resident = True
        with mock.patch.object(rw.os, "_exit") as exit_, mock.patch.object(panel, "trim_memory"):
            panel.close()
        exit_.assert_not_called()
        self.assertFalse(panel.closing)
        self.assertEqual(root.state(), "withdrawn")
        self.assertIn("scoring", panel.stale)
        with mock.patch.object(panel, "load_ledger") as reload:
            panel.reveal()
        reload.assert_called_once_with()
        root.deiconify.assert_called_once_with()
        self.assertNotIn("scoring", panel.stale)
        # Other pages read again when they are next raised, not all at once on show.
        panel.stale.add("quota")
        with mock.patch.object(panel, "load_quota") as quota:
            panel.show("quota")
        quota.assert_called_once_with()

    def test_closing_a_window_nobody_keeps_still_exits(self):
        rw, root, panel = self.make_panel()
        with mock.patch.object(rw.os, "_exit") as exit_:
            panel.close()
        exit_.assert_called_once_with(0)

    def test_a_hidden_window_exits_once_its_tray_is_gone(self):
        rw, root, panel = self.make_panel()
        panel.resident = True
        with mock.patch.object(panel, "trim_memory"):
            panel.hide()
        with mock.patch.object(panel, "parent_alive", return_value=False), \
                mock.patch.object(rw.os, "_exit") as exit_:
            panel.poll()
        exit_.assert_called_once_with(0)

    def test_a_hidden_window_gives_memory_back_whenever_it_goes_idle(self):
        rw, root, panel = self.make_panel()
        panel.resident = True
        panel.hide()
        with mock.patch.object(panel, "trim_memory") as trim:
            panel.busy_jobs = 1
            panel.poll()
            trim.assert_not_called()  # a read is still running
            panel.busy_jobs = 0
            panel.poll()
            panel.poll()
            self.assertEqual(trim.call_count, 1)
            # A late result fills a page and touches memory again.
            panel.busy_jobs = 1
            panel.results.put((panel._generation, None, "", None))
            panel.drain_results()
            panel.poll()
            self.assertEqual(trim.call_count, 2)

    def test_the_show_request_reveals_the_hidden_window(self):
        rw, root, panel = self.make_panel()
        panel.resident = True
        with mock.patch.object(panel, "trim_memory"):
            panel.hide()
        with mock.patch.object(panel, "show_requested", return_value=True), \
                mock.patch.object(panel, "reveal") as reveal:
            panel.poll()
        reveal.assert_called_once_with()

    def test_the_page_of_a_hidden_window_counts_as_not_shown(self):
        # Animations ask shown(); Tk still reports a withdrawn window's widgets as mapped.
        import page_stack

        rw, root, panel = self.make_panel()
        page = panel.pages["scoring"]
        inner = page.winfo_children()[0]
        panel.resident = True
        with mock.patch.object(panel, "trim_memory"), mock.patch.object(inner, "winfo_ismapped", return_value=True):
            panel.hide()
            self.assertTrue(page._page_hidden)
            self.assertFalse(page_stack.shown(inner))
            with mock.patch.object(panel, "load_ledger"):
                panel.reveal()
            self.assertFalse(page._page_hidden)
            self.assertTrue(page_stack.shown(inner))


if __name__ == "__main__":
    unittest.main()
