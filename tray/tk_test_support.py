"""Shared Tkinter interpreter and main-thread cleanup helpers for tray UI tests."""
import atexit
import gc
import os
import sys
import threading
import time
import unittest
import importlib.util
import importlib
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


def import_theatre_without_user_diagnostics():
    """Preload only the scene module under an empty import-time diagnostic home."""
    with tempfile.TemporaryDirectory(prefix='theatre import ') as home:
        with mock.patch.dict(os.environ, {'HOME': home, 'USERPROFILE': home}):
            return importlib.import_module('decision_theatre')


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


_WINDOW_PATCHERS = []


def _keep_windows_hidden():
    """Keep production dialog helpers from mapping windows during ordinary tests."""
    if _WINDOW_PATCHERS:
        return
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
        _WINDOW_PATCHERS.append(patcher)


def restore_window_visibility():
    """Undo _keep_windows_hidden so non-tray suites in the same worker see real Tk behaviour."""
    while _WINDOW_PATCHERS:
        _WINDOW_PATCHERS.pop().stop()


atexit.register(restore_window_visibility)


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
    keep_images = set()
    for attr in ("_AgentCompass_checks", "_compass_icon"):
        val = getattr(root, attr, None)
        items = val.values() if isinstance(val, dict) else (val or ())
        for img in items:
            photo = getattr(img, "_PhotoImage__photo", img)
            name = getattr(photo, "name", None)
            if name:
                keep_images.add(name)
    image_tk_mod = sys.modules.get("PIL.ImageTk")
    pil_photo_cls = getattr(image_tk_mod, "PhotoImage", None) if image_tk_mod is not None else None
    for obj in gc.get_objects():
        if isinstance(obj, tk.Variable):
            tk_interp = getattr(obj, "_tk", None)
            if tk_interp is not None:
                for cmd in list(getattr(obj, "_tclCommands", None) or ()):
                    try:
                        tk_interp.deletecommand(cmd)
                    except Exception:
                        pass
                if getattr(obj, "_tclCommands", None):
                    obj._tclCommands.clear()
                var_name = getattr(obj, "_name", None)
                if var_name:
                    try:
                        if tk_interp.getboolean(tk_interp.call("info", "exists", var_name)):
                            tk_interp.globalunsetvar(var_name)
                    except Exception:
                        pass
                obj._tk = None
        elif pil_photo_cls is not None and isinstance(obj, pil_photo_cls):
            photo = getattr(obj, "_PhotoImage__photo", None)
            name = getattr(photo, "name", None) if photo is not None else None
            if name and name not in keep_images:
                obj.__dict__.pop("_PhotoImage__photo", None)
                photo.name = None
                try:
                    root.tk.call("image", "delete", name)
                except Exception:
                    pass
        elif isinstance(obj, tk.Image):
            name = getattr(obj, "name", None)
            if name and name not in keep_images:
                obj.name = None
                try:
                    root.tk.call("image", "delete", name)
                except Exception:
                    pass
    obj = None
    photo = None
    gc.collect()
    gc.collect()
    gc.collect()


def close_tk_root(root):
    reset_tk_root(root)
    # Variable/trace cleanup may already have removed a Tcl command through
    # the interpreter. Tkinter's Python list can still contain that name.
    commands = set(root.tk.splitlist(root.tk.call('info', 'commands')))
    if root._tclCommands:
        root._tclCommands[:] = [name for name in root._tclCommands if name in commands]
    root.destroy()


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
        tk._tray_shared_root = root
        atexit.register(close_tk_root, root)
    if not DESKTOP_TESTS:
        _keep_windows_hidden()
    reset_tk_root(root)
    if testcase is not None:
        testcase.addCleanup(reset_tk_root, root, testcase)
    return root
