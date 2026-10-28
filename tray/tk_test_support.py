"""Shared Tkinter interpreter and main-thread cleanup helpers for tray UI tests."""
import atexit
import gc
import os
import sys
import threading
import time
import tkinter as tk
from pathlib import Path


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
    tcl_root = Path(sys.base_prefix) / "tcl"
    if (tcl_root / "tcl8.6").is_dir():
        os.environ.setdefault("TCL_LIBRARY", str(tcl_root / "tcl8.6"))
    if (tcl_root / "tk8.6").is_dir():
        os.environ.setdefault("TK_LIBRARY", str(tcl_root / "tk8.6"))
    root = getattr(tk, "_tray_shared_root", None)
    if root is None or not int(root.tk.call("winfo", "exists", root._w)):
        for attempt in range(5):
            try:
                root = tk.Tk()
                break
            except tk.TclError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
        root.withdraw()
        tk._tray_shared_root = root
        atexit.register(lambda: (reset_tk_root(root), root.destroy()))
    reset_tk_root(root)
    if testcase is not None:
        testcase.addCleanup(reset_tk_root, root, testcase)
    return root
