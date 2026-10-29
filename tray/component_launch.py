"""Start a tray helper script as a child process, from source or from the frozen tray executable.

From a checkout a helper runs as ``pythonw tray/<name>.py``. The packaged tray has no Python
interpreter and no tray/ scripts on disk; there the same executable runs the bundled module:
``AgentCompass.exe --run-component <name> [args]``. ``dispatch`` is that entry point and must run
before the tray starts anything of its own.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

FLAG = "--run-component"
# Every module a tray process starts as a child. The PyInstaller spec lists the same names.
COMPONENTS = (
    "capacity_stats",
    "easytier_setup",
    "network_status",
    "router_window",
    "update_status",
    "usage_float_host",
)


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


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
