# -*- mode: python ; coding: utf-8 -*-
"""One-folder tray build plus the console command line the installer puts on the user PATH.

dist/AgentCompass/AgentCompass.exe       the windowed tray
dist/AgentCompass/cli/agentcompass.exe   the console `agentcompass` (its own one-folder build in cli/, with the skill and
                                         setup files as real files under cli/_internal/agentcompass/data); it is also the
                                         Python that runs those scripts, so a Windows user needs no Python at all

The command line is a separate folder because Windows file names ignore case (agentcompass.exe would be AgentCompass.exe)
and because only cli/ goes on PATH: the tray folder on PATH would make `agentcompass` start the tray.

Do not add codex-chatgpt-web or any node_modules tree as data. Do not run the
built AgentCompass.exe from a developer machine: it starts the tray services.
"""

import ast
import shutil
import sys
from pathlib import Path

root = Path(SPECPATH).resolve().parent
tray = root / "tray"
router_scripts = root / "skills" / "agent-compass" / "scripts"

# The router skill is not frozen, but the tray imports its scripts in-process (priority menu, router
# window), so the frozen interpreter must carry every standard-library module they import.
# Usage Float, run from its own checkout through usage_float_host, adds the Tk and Windows modules below.
USAGE_FLOAT_STDLIB = [
    "tkinter", "tkinter.ttk", "tkinter.font", "tkinter.messagebox", "tkinter.filedialog",
    "tkinter.colorchooser", "tkinter.simpledialog", "zoneinfo", "decimal", "html", "uuid", "struct",
    "base64", "ctypes", "dataclasses", "winreg", "msvcrt",
]


def stdlib_imports(folder, pattern="*.py"):
    found = set()
    for path in sorted(folder.glob(pattern)):
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
    "capacity_stats", "deploy", "easytier_setup", "web_launcher", "network_status", "owned_command", "owned_process", "router_setup", "router_window", "update_status", "usage_collect", "usage_float_host",
]

a = Analysis(
    [str(tray / "app.pyw")],
    pathex=[str(tray), str(root)],
    binaries=[],
    datas=[(str(tray / "easytier_supervisor.ps1"), ".")],
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

# --- the console command line ---------------------------------------------------------------------------------------
sys.path.insert(0, str(root / "packaging"))
import stage_data  # noqa: E402

from PyInstaller.utils.hooks import collect_submodules  # noqa: E402

staged = stage_data.stage(root, Path(workpath) / "cli-data")
# The scripts run from disk, so nothing analyses their imports: take every standard-library package they name whole
# (`from concurrent.futures import ...` needs concurrent.futures, not just concurrent).
cli_stdlib = []
for name in sorted(set(stdlib_imports(staged / "skills", "**/*.py")) | set(stdlib_imports(staged / "setup", "**/*.py"))):
    if name in ("tkinter", "idlelib", "test", "turtledemo", "lib2to3", "ensurepip", "venv"):
        continue
    cli_stdlib.append(name)
    cli_stdlib.extend(sub for sub in collect_submodules(name) if ".test" not in sub and "tests" not in sub)

cli = Analysis(
    [str(root / "packaging" / "cli_entry.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[(str(staged / "skills"), "agentcompass/data/skills"), (str(staged / "setup"), "agentcompass/data/setup")],
    hiddenimports=[
        "agentcompass",
        "agentcompass.cli",
        "agentcompass.doctor",
        "agentcompass.frozen_python",
        "agentcompass.install",
        "agentcompass.locations",
        "agentcompass.origin",
        "agentcompass.ports",
        "agentcompass.tools",
        # run_grok_bot.py owns its Windows CLI children through a job object
        "win32api",
        "win32con",
        "win32event",
        "win32job",
        "win32process",
        "pywintypes",
        *cli_stdlib,
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "PIL", "pystray"],
    noarchive=False,
)
cli_pyz = PYZ(cli.pure)
cli_exe = EXE(
    cli_pyz,
    cli.scripts,
    [("X utf8", None, "OPTION")],
    exclude_binaries=True,
    name="agentcompass",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
)
cli_coll = COLLECT(
    cli_exe,
    cli.binaries,
    cli.datas,
    strip=False,
    upx=False,
    name="agentcompass-cli",
)

# Put the command line inside the tray folder, where the installer takes everything from.
cli_target = Path(DISTPATH) / "AgentCompass" / "cli"
if cli_target.exists():
    shutil.rmtree(cli_target)
shutil.copytree(Path(DISTPATH) / "agentcompass-cli", cli_target)
shutil.rmtree(Path(DISTPATH) / "agentcompass-cli")
