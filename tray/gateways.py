"""Muse2API: optional local API gateway, cloned into ~/.route-manager/plugins/<id> when enabled.

Muse2API (github.com/www222fff/muse2api) is a Python FastAPI server. It gets a .venv in its clone (uv), runs with
the browser driver on the installed Chrome, and keeps its data in ~/.muse2api/data. Accounts are muse.ai cookies,
imported through its dashboard or admin API.

It listens on loopback only and is stopped by the port it owns.
"""
from __future__ import annotations

from i18n import translate as _tr

import os
import webbrowser
from pathlib import Path

import plugins
import service_ports

MUSE_PORT = service_ports.get("muse2api")
MUSE_HOME = Path.home() / ".muse2api"
MUSE_LOG = MUSE_HOME / "serve.log"
MUSE_URL = f"http://127.0.0.1:{MUSE_PORT}"
def chrome_path() -> Path:
    from runtime_paths import executable
    return executable("chrome", "MUSE2API_CHROMIUM_PATH", (
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "Google/Chrome/Application/chrome.exe",
    ))


def muse_root() -> Path:
    return plugins.plugin_root("muse2api")


# ---------- Muse2API ----------

def muse_python() -> Path:
    return muse_root() / ".venv" / "Scripts" / "python.exe"


def muse_sync(run, uv: Path) -> None:
    """A .venv in the clone with muse2api installed editable (the project has no lock file for `uv sync`)."""
    root = muse_root()
    if not muse_python().is_file():
        run([str(uv), "venv", str(root / ".venv")], root, timeout=600)
    run([str(uv), "pip", "install", "--python", str(muse_python()), "-e", str(root)], root, timeout=1200)


def muse_built() -> bool:
    return muse_python().is_file() and (muse_root() / "src" / "muse2api" / "__main__.py").is_file()


def muse_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "MUSE2API_HOST": "127.0.0.1",
        "MUSE2API_PORT": str(MUSE_PORT),
        "MUSE2API_DATA_DIR": str(MUSE_HOME / "data"),
        "MUSE2API_DRIVER": os.environ.get("MUSE2API_DRIVER", "browser"),
        "MUSE2API_HEADLESS": "true",
        "PYTHONIOENCODING": "utf-8",
    })
    if env["MUSE2API_DRIVER"] == "browser":
        env["MUSE2API_CHROMIUM_PATH"] = str(chrome_path())
    return env


def muse_command() -> list[str]:
    return [str(muse_python()), "-m", "muse2api"]


def muse_ready(http_ok) -> bool:
    return http_ok(f"{MUSE_URL}/healthz")


def muse_api_key() -> str:
    try:
        return (MUSE_HOME / "data" / "api_key").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


IDS = ("muse2api",)


def built(plugin_id: str) -> bool:
    return plugin_id in IDS and muse_built()


def open_console(url: str) -> str:
    webbrowser.open(url)
    return _tr('已在浏览器打开 {0}').format(f'{url}')
