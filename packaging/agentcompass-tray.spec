# -*- mode: python ; coding: utf-8 -*-
"""One-folder tray build. The router is not frozen into this executable.

Do not add codex-chatgpt-web or any node_modules tree as data. Do not run the
built AgentCompass.exe from a developer machine: it starts the tray services.
"""

import ast
import sys
from pathlib import Path

root = Path(SPECPATH).resolve().parent
tray = root / "tray"
router_scripts = root / "skills" / "route-agent-work" / "scripts"

# The router skill is not frozen, but the tray imports its scripts in-process (priority menu, router
# window), so the frozen interpreter must carry every standard-library module they import.
# Usage Float, run from its own checkout through usage_float_host, adds the Tk and Windows modules below.
USAGE_FLOAT_STDLIB = [
    "tkinter", "tkinter.ttk", "tkinter.font", "tkinter.messagebox", "tkinter.filedialog",
    "tkinter.colorchooser", "tkinter.simpledialog", "zoneinfo", "decimal", "html", "uuid", "struct",
    "base64", "ctypes", "dataclasses", "winreg", "msvcrt",
]


def stdlib_imports(folder):
    found = set()
    for path in sorted(folder.glob("*.py")):
        if path.name.startswith("test_"):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            if isinstance(node, ast.Import):
                found.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    return sorted(name for name in found if name in sys.stdlib_module_names)


# Tray modules the executable runs as helpers: AgentCompass.exe --run-component <name> (tray/component_launch.py).
HELPER_MODULES = [
    "capacity_stats", "deploy", "easytier_setup", "web_launcher", "network_status", "owned_command", "owned_process", "router_window", "update_status", "usage_float_host",
]

a = Analysis(
    [str(tray / "app.pyw")],
    pathex=[str(tray), str(root)],
    binaries=[],
    datas=[(str(tray / "easytier_supervisor.ps1"), "tray")],
    hiddenimports=[
        "pystray",
        "pystray._win32",
        "PIL",
        "PIL.Image",
        "PIL.ImageDraw",
        "win32api",
        "win32con",
        "win32gui",
        "win32process",
        "win32event",
        "pywintypes",
        "pythoncom",
        "psutil",
        "agentcompass",
        "agentcompass.ports",
        "agentcompass.origin",
        "agentcompass.tools",
        *HELPER_MODULES,
        *stdlib_imports(router_scripts),
        *USAGE_FLOAT_STDLIB,
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AgentCompass",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="AgentCompass",
)
