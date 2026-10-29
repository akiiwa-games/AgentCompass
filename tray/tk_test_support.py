"""Shared Tkinter interpreter and main-thread cleanup helpers for tray UI tests."""
import atexit
import gc
import os
import sys
import threading
import time
import unittest
import importlib.util
try:
    import tkinter as tk
except ImportError:
    tk = None
import tempfile
from pathlib import Path
from unittest import mock

DESKTOP_TESTS = os.environ.get("AGENTCOMPASS_DESKTOP_TESTS") == "1"
desktop_test = unittest.skipUnless(
    DESKTOP_TESTS, "maps desktop windows; opt in with AGENTCOMPASS_DESKTOP_TESTS=1 on an isolated desktop")


def require_windows(*modules):
    """One import-time guard for platform integrations and their optional dependencies."""
    if sys.platform != "win32":
        raise unittest.SkipTest("Windows integration tests")
    missing = [name for name in modules if importlib.util.find_spec(name) is None]
    if missing:
        raise unittest.SkipTest("Windows integration dependencies unavailable: " + ", ".join(missing))


def require_tk(*, display=False):
    """Guard Tk-dependent imports separately from display-dependent test setup."""
    if tk is None:
        raise unittest.SkipTest("Tkinter is not installed")
    if display and sys.platform not in ("win32", "darwin") and not os.environ.get("DISPLAY"):
        raise unittest.SkipTest("Tk tests require a display (for example Xvfb)")
    return tk


def _keep_windows_hidden():
    """Keep production dialog helpers from mapping windows during ordinary tests."""
    original_init = tk.Toplevel.__init__

    def withdrawn_init(window, *args, **kwargs):
        original_init(window, *args, **kwargs)
        window.withdraw()

    for target, name, value in (
        (tk.Toplevel, "__init__", withdrawn_init),
        (tk.Wm, "deiconify", lambda window: window.withdraw()),
        (tk.Wm, "wm_deiconify", lambda window: window.withdraw()),
    ):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        atexit.register(patcher.stop)


def reset_tk_root(root, case=None):
    """Destroy child widgets, break Tcl callback/trace cycles, and collect Tk Variables on the main thread."""
    if case is not None:
        panel = case.__dict__.pop("panel", None)
        if panel is not None:
            panel.closing = True
            panel._closed = True
    for thread in list(threading.enumerate()):
        if thread is not threading.main_thread() and getattr(getattr(thread, "_target", None), "__name__", "") == "runner":
            thread.join(2)
    try:
        root.withdraw()
        root.minsize(1, 1)
        root.geometry("")
    except Exception:
        pass
    for after_id in root.tk.splitlist(root.tk.call("after", "info")):
        try:
            root.after_cancel(after_id)
        except Exception:
            pass
    for seq in ("<<PanelResult>>", "<Button-1>", "<Configure>", "<MouseWheel>"):
        try:
            root.unbind(seq)
        except Exception:
            pass
    try:
        root.protocol("WM_DELETE_WINDOW", "")
    except Exception:
        pass
    for child in list(root.winfo_children()):
        try:
            child.destroy()
        except Exception:
            pass
    for cmd in list(getattr(root, "_tclCommands", None) or ()):
        if cmd not in ("tkerror", "exit") and not cmd.endswith("destroy"):
            try:
                root.deletecommand(cmd)
            except Exception:
                pass
    for obj in gc.get_objects():
        if isinstance(obj, tk.Variable):
            for cmd in list(getattr(obj, "_tclCommands", None) or ()):
                try:
                    obj._tk.deletecommand(cmd)
                except Exception:
                    pass
            if getattr(obj, "_tclCommands", None):
                obj._tclCommands.clear()
    obj = None
    gc.collect()
    gc.collect()
    gc.collect()


def shared_tk_root(testcase=None):
    """Return the process-wide shared Tk root, initializing Tcl/Tk library paths and retries if needed."""
    require_tk(display=True)
    tcl_root = Path(sys.base_prefix) / "tcl"
    if (tcl_root / "tcl8.6").is_dir():
        os.environ.setdefault("TCL_LIBRARY", str(tcl_root / "tcl8.6"))
    if (tcl_root / "tk8.6").is_dir():
        os.environ.setdefault("TK_LIBRARY", str(tcl_root / "tk8.6"))
    root = getattr(tk, "_tray_shared_root", None)
    if root is None or not int(root.tk.call("winfo", "exists", root._w)):
        # All themed windows use a native ICO cache. Keep UI tests out of the
        # user's real profile, just as their plugin/settings fixtures do.
        import ui_theme
        icon_cache = tempfile.TemporaryDirectory()
        icon_patch = mock.patch.object(ui_theme, "ICON_CACHE_DIR", Path(icon_cache.name))
        icon_patch.start()
        atexit.register(icon_cache.cleanup)
        atexit.register(icon_patch.stop)
        for attempt in range(5):
            try:
                root = tk.Tk()
                break
            except tk.TclError:
                if attempt == 4:
                    raise unittest.SkipTest("Tk/display is unavailable")
                time.sleep(0.05 * (attempt + 1))
        root.withdraw()
        if not DESKTOP_TESTS:
            _keep_windows_hidden()
        tk._tray_shared_root = root
        atexit.register(lambda: (reset_tk_root(root), root.destroy()))
    reset_tk_root(root)
    if testcase is not None:
        testcase.addCleanup(reset_tk_root, root, testcase)
    return root
