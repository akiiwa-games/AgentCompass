# -*- mode: python ; coding: utf-8 -*-
"""One-folder tray runtime plus the console command-line runtime; application code lives in ``app/current``.

dist/AgentCompass/AgentCompass.exe       the windowed tray runtime (thin loader: packaging/tray_entry.py)
dist/AgentCompass/cli/agentcompass.exe   the console `agentcompass` runtime (thin loader: packaging/cli_entry.py;
                                         legacy links under cli/_internal/agentcompass/data/skills point into app/current)
dist/AgentCompass/app/current/           unpacked code package (tray/, agentcompass/, skills/, setup/, app-manifest.json)
dist/AgentCompass/app/packages/          versioned app-<version>.zip archive
dist/AgentCompass/runtime-info.json      runtime fingerprint and build input breakdown

The command line is a separate folder because Windows file names ignore case (agentcompass.exe would be AgentCompass.exe)
and because only cli/ goes on PATH: the tray folder on PATH would make `agentcompass` start the tray.

Do not add codex-chatgpt-web or any node_modules tree as data. Do not run the
built AgentCompass.exe from a developer machine: it starts the tray services.
"""

import shutil
import sys
from pathlib import Path

root = Path(SPECPATH).resolve().parent

sys.path.insert(0, str(root / "packaging"))
import app_bundle  # noqa: E402

# The router skill, CLI, and tray modules are loaded from <app>/app/current on disk, not frozen into PYZ,
# so the frozen interpreter carries the full Windows Python standard library (see app_bundle.collect_bundled_stdlib)
# and records the bundled module set inside runtime_fingerprint.

# Tray modules the executable runs as helpers: AgentCompass.exe --run-component <name> (tray/component_launch.py).
HELPER_MODULES = [
    "capacity_stats", "deploy", "easytier_setup", "web_launcher", "network_status", "owned_command", "owned_process", "router_setup", "router_window", "update_status", "usage_collect", "usage_float_host", "updates",
]

a = Analysis(
    [str(root / "packaging" / "tray_entry.py")],
    pathex=[str(root / "packaging")],
    binaries=[],
    datas=[],
    hiddenimports=app_bundle.tray_hidden_imports(),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["agentcompass", "tray", *HELPER_MODULES, *app_bundle.NON_RUNTIME_EXCLUDES],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [("X utf8", None, "OPTION")],
    exclude_binaries=True,
    name="AgentCompass",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=True,
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
# The scripts run from disk under <app>/app/current, so the CLI frozen interpreter carries the full non-GUI standard library.

cli = Analysis(
    [str(root / "packaging" / "cli_entry.py")],
    pathex=[str(root / "packaging")],
    binaries=[],
    datas=[],
    hiddenimports=app_bundle.cli_hidden_imports(),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "PIL", "pystray", "agentcompass", "tray", *app_bundle.NON_RUNTIME_EXCLUDES],
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
dist_root = Path(DISTPATH)
dist_app = dist_root / "AgentCompass"
cli_target = dist_app / "cli"
if cli_target.exists():
    shutil.rmtree(cli_target)
shutil.copytree(dist_root / "agentcompass-cli", cli_target)
shutil.rmtree(dist_root / "agentcompass-cli")

# Stage the code package (app-<version>.zip), active app/current tree, runtime-info.json, and manifest.json.
app_bundle.stage_installed_app(root, dist_app, dist_dir=dist_root)
app_bundle.build_release_manifest(root, dist_root)
