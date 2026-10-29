"""Start a tray helper script as a child process, from source or from the frozen tray executable.

From a checkout a helper runs as ``pythonw tray/<name>.py``. The packaged tray has no Python
interpreter and no tray/ scripts on disk; there the same executable runs the bundled module:
``AgentCompass.exe --run-component <name> [args]``. ``dispatch`` is that entry point and must run
before the tray starts anything of its own.
"""

from __future__ import annotations

import runpy
import os
import sys
from pathlib import Path

FLAG = "--run-component"
# Every module a tray process starts as a child. The PyInstaller spec lists the same names.
COMPONENTS = (
    "capacity_stats",
    "deploy",
    "easytier_setup",
    "web_launcher",
    "network_status",
    "owned_command",
    "owned_process",
    "router_window",
    "update_status",
    "usage_float_host",
)


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def tray_command(*args: str) -> list[str]:
    if frozen():
        return [sys.executable, *args]
    return [str(Path(sys.executable).with_name("pythonw.exe")),
            str(Path(__file__).with_name("app.pyw")), *args]


def restore_stdio() -> None:
    """Windowed PyInstaller sets sys.std* to None even with inherited redirection.

    Duplicate the Windows handles so closing a Python stream cannot close the original.
    Components without redirection use the null device and never allocate a console.
    """
    if not frozen() or os.name != "nt":
        return
    import ctypes
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                                      ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    process = kernel.GetCurrentProcess()
    for name, number, mode in (("stdin", -10, "r"), ("stdout", -11, "w"), ("stderr", -12, "w")):
        if getattr(sys, name) is not None:
            continue
        handle = kernel.GetStdHandle(number & 0xffffffff)
        duplicate = wintypes.HANDLE()
        if handle and handle != wintypes.HANDLE(-1).value and kernel.DuplicateHandle(process, handle, process, ctypes.byref(duplicate), 0, False, 2):
            fd = msvcrt.open_osfhandle(duplicate.value, os.O_RDONLY if mode == "r" else os.O_WRONLY)
            stream = os.fdopen(fd, mode, encoding="utf-8", errors="replace")
        else:
            stream = open(os.devnull, mode, encoding="utf-8")
        setattr(sys, name, stream)


def command(script: str | Path, *args: str, windowed: bool = True) -> list[str]:
    """argv that runs the tray helper ``script`` (a path or a bare module name).

    ``windowed`` picks pythonw.exe over python.exe from a checkout; the frozen tray executable has no console either way.
    """
    name = Path(script).stem
    if frozen():
        return [sys.executable, FLAG, name, *args]
    python = Path(sys.executable)
    if windowed:
        python = python.with_name("pythonw.exe")
    path = Path(script) if Path(script).suffix else Path(__file__).resolve().parent / f"{name}.py"
    return [str(python), str(path), *args]


def dispatch(argv: list[str]) -> int | None:
    """Run the requested component and return its exit code; None when argv does not ask for one."""
    if len(argv) < 3 or argv[1] != FLAG:
        return None
    restore_stdio()
    name = argv[2]
    if name not in COMPONENTS:
        print(f"unknown component: {name}", file=sys.stderr)
        return 2
    sys.argv = [name, *argv[3:]]
    try:
        runpy.run_module(name, run_name="__main__", alter_sys=True)
    except SystemExit as exit_:
        code = exit_.code
        return code if isinstance(code, int) else 0 if code is None else 1
    return 0
