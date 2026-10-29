"""Default GUI tests stay hidden; unavailable integrations have explicit skips."""
import unittest
from unittest import mock

import tk_test_support as support


class GuardTests(unittest.TestCase):
    def test_windows_guard_skips_before_importing_platform_modules(self):
        with mock.patch.object(support.sys, 'platform', 'linux'), \
                mock.patch.object(support.importlib.util, 'find_spec') as lookup:
            with self.assertRaises(unittest.SkipTest):
                support.require_windows('win32gui')
            lookup.assert_not_called()

    def test_missing_tk_and_headless_display_have_clear_skip_guards(self):
        with mock.patch.object(support, 'tk', None):
            with self.assertRaisesRegex(unittest.SkipTest, 'Tkinter'):
                support.shared_tk_root()
        with mock.patch.object(support.sys, 'platform', 'linux'), \
                mock.patch.dict(support.os.environ, {'DISPLAY': ''}):
            with self.assertRaises(unittest.SkipTest):
                support.shared_tk_root()

    @unittest.skipIf(support.DESKTOP_TESTS, 'checks the default hidden-window mode')
    def test_root_and_child_stay_unmapped_after_deiconify(self):
        root = support.shared_tk_root(self)
        child = support.tk.Toplevel(root)
        root.deiconify()
        child.deiconify()
        root.update()
        self.assertFalse(root.winfo_ismapped())
        self.assertFalse(child.winfo_ismapped())
