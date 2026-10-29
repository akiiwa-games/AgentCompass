"""Shell branding regressions, with no writes to the user's icon cache."""
import ctypes
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from PIL import Image

import ui_theme as theme
from tk_test_support import desktop_test, require_windows, require_tk


class IdentityTests(unittest.TestCase):
    def test_windows_process_identity_and_non_windows_noop(self):
        shell = mock.Mock()
        shell.SetCurrentProcessExplicitAppUserModelID.return_value = 0
        with mock.patch.object(ctypes, "windll", mock.Mock(shell32=shell), create=True):
            with mock.patch.object(sys, "platform", "linux"):
                theme.identify_process()
            shell.SetCurrentProcessExplicitAppUserModelID.assert_not_called()
            with mock.patch.object(sys, "platform", "win32"):
                theme.identify_process()
            shell.SetCurrentProcessExplicitAppUserModelID.assert_called_once_with(theme.APP_ID)

    def test_cache_contains_all_shell_sizes_and_is_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = theme.window_icon_path(Path(directory))
            with Image.open(path) as icon:
                self.assertEqual(icon.ico.sizes(), {(n, n) for n in (16, 24, 32, 48, 64, 128, 256)})
            with mock.patch.object(theme, "compass_image", side_effect=AssertionError("cache was redrawn")):
                self.assertEqual(theme.window_icon_path(Path(directory)), path)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_readonly_cache_keeps_photo_icon(self):
        require_tk()
        root = mock.Mock()
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch("PIL.ImageTk.PhotoImage"), \
                mock.patch.object(theme, "window_icon_path", side_effect=PermissionError("readonly")), \
                self.assertLogs(theme.__name__, level="WARNING"):
            theme.set_window_icon(root)
        root.iconphoto.assert_called_once()
        root.iconbitmap.assert_not_called()


@unittest.skipUnless(sys.platform == "win32", "native Windows shell icons")
@desktop_test
class NativeIconTests(unittest.TestCase):
    def test_theme_sets_large_and_small_icons_and_dialogs_inherit(self):
        require_windows("win32gui")
        import tkinter as tk
        import win32gui
        from tk_test_support import shared_tk_root

        root = shared_tk_root(self)
        theme.apply(root, "AgentCompass")
        child = tk.Toplevel(root)
        child.withdraw()
        child.geometry("+-3000+-3000")
        child.title("AgentCompass dialog")
        root.geometry("+-3000+-3000")
        root.deiconify()
        child.deiconify()
        root.update()
        for window in (root, child):
            hwnd = int(window.tk.call("wm", "frame", window._w), 0)
            for kind in (0, 1):  # ICON_SMALL, ICON_BIG
                # Windows falls back to the window class icon for an inherited
                # default, just as the shell does when drawing a taskbar button.
                icon = win32gui.SendMessage(hwnd, 0x7F, kind, 0)
                if not icon:
                    icon = win32gui.GetClassLong(hwnd, -34 if kind == 0 else -14)
                if not icon:
                    icon = win32gui.GetClassLong(hwnd, -14)
                self.assertNotEqual(icon, 0)


if __name__ == "__main__":
    unittest.main()
