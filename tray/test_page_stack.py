import sys
import unittest
from pathlib import Path
from unittest import mock

import pytest

pytestmark = pytest.mark.slow

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tk_test_support import require_tk, shared_tk_root

require_tk()
import page_stack


class Watcher:
    def __init__(self, canvas):
        self.canvas = canvas
        self.events = []
        page_stack.watch(self)

    def page_shown(self):
        self.events.append("shown")

    def page_hidden(self):
        self.events.append("hidden")


class PageStackTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk

        self.tk = tk
        self.root = shared_tk_root(self)
        self.container = tk.Frame(self.root)
        self.addCleanup(self.container.destroy)
        self.a, self.b = tk.Frame(self.container), tk.Frame(self.container)
        self.canvas_a, self.canvas_b = tk.Canvas(self.a), tk.Canvas(self.b)
        self.canvas_a.pack()
        self.canvas_b.pack()
        self.stack = page_stack.PageStack(self.container)

    def test_a_covered_page_stays_placed_and_reports_hidden(self):
        self.stack.show(self.a)
        self.stack.show(self.b)
        # Covered, not unmapped: Tk never re-places and re-shows its native windows on the way back.
        self.assertEqual(self.a.winfo_manager(), "place")
        self.assertTrue(self.a._page_hidden)
        self.assertFalse(self.b._page_hidden)
        with mock.patch.object(self.canvas_a, "winfo_ismapped", return_value=True), \
                mock.patch.object(self.canvas_b, "winfo_ismapped", return_value=True):
            self.assertFalse(page_stack.shown(self.canvas_a))
            self.assertTrue(page_stack.shown(self.canvas_b))
        self.assertFalse(page_stack.shown(self.tk.Canvas(self.container)))  # never mapped

    @unittest.skipUnless(sys.platform == "win32", "native windows")
    def test_a_covered_page_is_hidden_natively_and_keeps_its_size(self):
        import ctypes

        def style_visible(page):
            return bool(ctypes.windll.user32.GetWindowLongW(int(page.winfo_id()), -16) & 0x10000000)

        self.container.pack(fill="both", expand=True)
        self.stack.show(self.a)
        self.stack.show(self.b)
        # Windows must not repaint it under the page on top; Tk keeps its geometry for the way back.
        self.assertFalse(style_visible(self.a))
        self.assertTrue(style_visible(self.b))
        self.assertEqual(self.a.place_info()["relwidth"], "0")
        self.assertEqual(self.b.place_info()["relwidth"], "1")
        self.stack.show(self.a)
        self.assertTrue(style_visible(self.a))
        self.assertFalse(style_visible(self.b))

    def test_watchers_hear_their_own_page_switch_only(self):
        watch_a, watch_b = Watcher(self.canvas_a), Watcher(self.canvas_b)
        with mock.patch.object(page_stack, "shown", return_value=True):
            self.stack.show(self.a)
            self.stack.show(self.b)
            self.stack.show(self.a)
        self.assertEqual(watch_a.events, ["shown", "hidden", "shown"])
        self.assertEqual(watch_b.events, ["shown", "hidden"])

    def test_a_watcher_on_a_covered_inner_page_is_not_started(self):
        watcher = Watcher(self.canvas_a)
        with mock.patch.object(page_stack, "shown", return_value=False):
            self.stack.show(self.a)
        self.assertEqual(watcher.events, [])

    def test_prepare_lays_a_page_out_underneath_without_switching(self):
        self.stack.show(self.a)
        self.stack.prepare(self.b)
        self.assertIs(self.stack.current, self.a)
        self.assertEqual(self.b.winfo_manager(), "place")
        self.assertTrue(self.b._page_hidden)
        self.stack.show(self.b)
        self.assertFalse(self.b._page_hidden)

    def test_focus_leaves_the_page_that_was_covered(self):
        entry = self.tk.Entry(self.a)
        entry.pack()
        self.stack.show(self.a)
        with mock.patch.object(self.container, "focus_get", return_value=entry), \
                mock.patch.object(self.b, "focus_set") as focus:
            self.stack.show(self.b)
        focus.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
