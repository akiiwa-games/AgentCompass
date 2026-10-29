# -*- mode: python ; coding: utf-8 -*-
"""One-folder tray build. The router is not frozen into this executable.

Do not add codex-chatgpt-web or any node_modules tree as data. Do not run the
built AgentCompass.exe from a developer machine: it starts the tray services.
"""

from pathlib import Path

root = Path(SPECPATH).resolve().parent
tray = root / "tray"

a = Analysis(
    [str(tray / "app.pyw")],
    pathex=[str(tray), str(root)],
    binaries=[],
    datas=[],
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
