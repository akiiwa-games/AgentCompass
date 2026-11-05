"""Which AgentCompass install is running, where the Windows installer's command line lives, and how hooks call it.

Three shapes run the same code:

  installer  the release ``AgentCompass-tray-setup.exe``: a frozen console ``agentcompass.exe`` in ``<app>\\cli``
             (``<app>`` is ``%LOCALAPPDATA%\\AgentCompass`` unless the user picked another folder), no Python needed
  wheel      ``uv tool install`` / ``pip install``: the ``agentcompass`` console script of a Python environment
  checkout   a git clone, run as ``python -X utf8 <clone>/agentcompass``

The installer's ``<app>\\cli`` folder is on the user PATH and keeps its path across upgrades, so hook commands and
skill links written by ``setup`` keep working when a newer installer replaces the files.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
from pathlib import Path

CLI_DIR_NAME = "cli"
CLI_EXE_NAME = "agentcompass.exe"
GATE_HOOK_COMMAND = "gate-hook"
# Inno Setup's per-user uninstall entry for AppId {A7B3E2C1-4F58-4C9A-9D2E-1B6A8C0D5E77} (packaging/agentcompass.iss).
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{A7B3E2C1-4F58-4C9A-9D2E-1B6A8C0D5E77}_is1"
# A gate hook entry written by any shape: the script path (wheel, checkout) or `<agentcompass.exe> gate-hook` (installer).
_HOOK_RE = re.compile(r"router_gate_hook\.py|agentcompass(?:\.exe)?[\"']?\s+" + GATE_HOOK_COMMAND + r"\b", re.I)


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def shape(module_file: str | Path | None = None) -> str:
    """'installer', 'wheel' or 'checkout' for the code that is running (or for ``module_file``)."""
    if module_file is None and frozen():
        return "installer"
    path = Path(module_file or __file__).resolve()
    if "site-packages" in path.parts or "dist-packages" in path.parts:
        return "wheel"
    return "checkout"


def is_gate_hook_command(command: object) -> bool:
    return bool(_HOOK_RE.search(str(command or "")))


def _registry_install_dir() -> Path | None:
    if os.name != "nt":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, "InstallLocation")
    except OSError:
        return None
    return Path(str(value).rstrip("\\/")) if value else None


def bundled_cli(environ: dict | None = None) -> Path | None:
    """The installer's ``agentcompass.exe``, if one is installed for this user (whichever shape is running)."""
    if frozen():
        exe = Path(sys.executable).resolve()
        if exe.name.lower() == CLI_EXE_NAME:
            return exe
    environ = os.environ if environ is None else environ
    candidates = []
    registered = _registry_install_dir()
    if registered is not None:
        candidates.append(registered / CLI_DIR_NAME / CLI_EXE_NAME)
    local = environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "AgentCompass" / CLI_DIR_NAME / CLI_EXE_NAME)
    return next((path for path in candidates if path.is_file()), None)


def router_command(module_file: str | Path | None = None, checkout: Path | None = None) -> str:
    """The router command agents are told to run: `agentcompass` when installed, else the checkout's package directory."""
    if shape(module_file) in ("installer", "wheel") or checkout is None:
        return "agentcompass"
    return f"python -X utf8 {(Path(checkout) / 'agentcompass').as_posix()}"


def gate_hook_argv(hook_script: str | Path) -> list[str]:
    """argv a host runs for the router gate: the installer's exe (no Python), else this Python and the hook script."""
    if frozen():
        return [Path(sys.executable).resolve().as_posix(), GATE_HOOK_COMMAND]
    return [Path(sys.executable).resolve().as_posix(), "-X", "utf8", Path(hook_script).resolve().as_posix()]


def gate_hook_command(hook_script: str | Path) -> str:
    return shlex.join(gate_hook_argv(hook_script))


def on_path(environ: dict | None = None) -> list[Path]:
    """Every `agentcompass` executable on PATH, in lookup order (the first one is what a new shell runs)."""
    environ = os.environ if environ is None else environ
    found: list[Path] = []
    seen: set[str] = set()
    exts = [""] if os.name != "nt" else [e.lower() for e in (environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD").split(";") if e]
    for folder in (environ.get("PATH") or "").split(os.pathsep):
        folder = folder.strip().strip('"')
        if not folder:
            continue
        for ext in exts:
            candidate = Path(folder) / f"agentcompass{ext}"
            try:
                if not candidate.is_file():
                    continue
                key = os.path.normcase(str(candidate.resolve()))
            except OSError:
                continue
            if key not in seen:
                seen.add(key)
                found.append(candidate)
    return found


def removal_hint(path: Path) -> str:
    """How to remove a uv/pip `agentcompass` that is not the installer's."""
    text = os.path.normcase(str(path))
    if os.path.normcase(os.path.join("uv", "tools")) in text or os.path.normcase(os.path.join(".local", "bin")) in text:
        return "uv tool uninstall agentcompass"
    python = path.parent / "python.exe"
    if path.parent.name.lower() == "scripts" and (path.parent.parent / "python.exe").is_file():
        python = path.parent.parent / "python.exe"
    if python.is_file():
        return f'"{python}" -m pip uninstall -y agentcompass'
    return "pip uninstall agentcompass (or uv tool uninstall agentcompass)"


def shadowing(environ: dict | None = None) -> dict | None:
    """When the installer's exe is installed but another `agentcompass` comes first on PATH: what wins and how to remove it."""
    bundled = bundled_cli(environ)
    if bundled is None:
        return None
    entries = on_path(environ)
    if not entries:
        return None
    first = entries[0]
    try:
        same = os.path.normcase(str(first.resolve())) == os.path.normcase(str(bundled.resolve()))
    except OSError:
        same = False
    if same:
        return None
    return {"bundled": str(bundled), "first": str(first), "remove": removal_hint(first)}


def which(name: str = "agentcompass") -> str | None:
    return shutil.which(name)
