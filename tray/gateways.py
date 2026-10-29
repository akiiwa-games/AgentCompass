"""Grok2API and Muse2API: optional local API gateways, cloned into ~/.route-manager/plugins/<id> when enabled.

Grok2API (github.com/chenyme/grok2api) is a Go server with a React admin console. It is built here from source
(Go builds the backend, pnpm the console) into ~/.grok2api/bin, and runs with its own config.yaml and SQLite data
in ~/.grok2api. Accounts (Grok Build / Web / Console) and client keys are added in its admin console.

Muse2API (github.com/www222fff/muse2api) is a Python FastAPI server. It gets a .venv in its clone (uv), runs with
the browser driver on the installed Chrome, and keeps its data in ~/.muse2api/data. Accounts are muse.ai cookies,
imported through its dashboard or admin API.

Both listen on loopback only and are stopped by the port they own.
"""
from __future__ import annotations

from i18n import translate as _tr

import base64
import os
import re
import secrets
import shutil
import webbrowser
from pathlib import Path

import plugins
import service_ports

GROK_PORT = service_ports.get("grok2api")
GROK_HOME = Path.home() / ".grok2api"
GROK_CONFIG = GROK_HOME / "config.yaml"
GROK_EXE = GROK_HOME / "bin" / "grok2api.exe"
GROK_LOG = GROK_HOME / "serve.log"
GROK_URL = f"http://127.0.0.1:{GROK_PORT}"

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


def grok_root() -> Path:
    return plugins.plugin_root("grok2api")


def muse_root() -> Path:
    return plugins.plugin_root("muse2api")


def _tool(*names: str) -> str:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    raise FileNotFoundError(_tr('找不到 {0}，请先安装并加入 PATH').format(f'{names[0]}'))


# ---------- Grok2API ----------

def grok_build(run) -> None:
    """Build the admin console and the server binary; `run(args, cwd, env=None, timeout=...)` runs one step."""
    root = grok_root()
    pnpm = _tool("pnpm.cmd", "pnpm")
    env = {**os.environ, "CI": "true"}    # pnpm then never prompts
    run([pnpm, "install", "--frozen-lockfile"], root / "frontend", env=env, timeout=1200)
    run([pnpm, "build"], root / "frontend", env=env, timeout=1200)
    GROK_EXE.parent.mkdir(parents=True, exist_ok=True)
    built = GROK_EXE.with_name("grok2api.new.exe")
    go_env = {**os.environ, "CGO_ENABLED": "0"}
    run([_tool("go.exe", "go"), "build", "-buildvcs=false", "-trimpath", "-ldflags=-s -w", "-o", str(built),
         "./cmd/grok2api"], root / "backend", env=go_env, timeout=1800)
    built.replace(GROK_EXE)
    grok_write_config()


def _yaml_quote(value: str) -> str:
    return '"' + value.replace("\\", "/").replace('"', '\\"') + '"'


def grok_write_config() -> bool:
    """First run only: this host's config.yaml with fresh secrets. Never rewritten: the encryption key must not change."""
    if GROK_CONFIG.is_file():
        return False
    text = (grok_root() / "config.example.yaml").read_text(encoding="utf-8")
    replacements = (
        ("listen", r'"[^"]*"', f"127.0.0.1:{GROK_PORT}"),
        ("jwtSecret", r'"[^"]*"', secrets.token_hex(32)),
        ("credentialEncryptionKey", r'"[^"]*"', base64.b64encode(secrets.token_bytes(32)).decode()),
        ("password", r'"replace-with-a-strong-password"', secrets.token_urlsafe(18)),
        ("staticPath", r'"[^"]*"', str(grok_root() / "frontend" / "dist")),
    )
    for key, old, value in replacements:
        text, count = re.subn(rf"(\n  {key}: ){old}", lambda match, v=value: match.group(1) + _yaml_quote(v), text, count=1)
        if count != 1:
            raise RuntimeError(_tr('Grok2API 配置模板已变化，找不到 {0}').format(f'{key}'))
    GROK_HOME.mkdir(parents=True, exist_ok=True)
    temporary = GROK_CONFIG.with_suffix(".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(GROK_CONFIG)
    return True


def grok_admin() -> tuple[str, str]:
    """The bootstrap administrator from this host's config.yaml ('' when there is none)."""
    try:
        text = GROK_CONFIG.read_text(encoding="utf-8")
    except OSError:
        return "", ""
    block = re.search(r"^bootstrapAdmin:\n((?:[ #].*\n?)*)", text, re.MULTILINE)
    if not block:
        return "", ""
    user = re.search(r'^  username: "([^"]*)"', block.group(1), re.MULTILINE)
    password = re.search(r'^  password: "([^"]*)"', block.group(1), re.MULTILINE)
    return (user.group(1) if user else ""), (password.group(1) if password else "")


def grok_built() -> bool:
    return GROK_EXE.is_file() and GROK_CONFIG.is_file() and (grok_root() / "frontend" / "dist" / "index.html").is_file()


def grok_ready(http_ok) -> bool:
    """The server is up. (/readyz stays 503 until some provider has a usable account.)"""
    return http_ok(f"{GROK_URL}/healthz")


def grok_serving(http_ok) -> bool:
    """At least one Grok account can take requests."""
    return http_ok(f"{GROK_URL}/readyz")


def grok_command() -> list[str]:
    return [str(GROK_EXE), "--config", str(GROK_CONFIG), "--listen", f"127.0.0.1:{GROK_PORT}"]


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


IDS = ("grok2api", "muse2api")


def built(plugin_id: str) -> bool:
    return grok_built() if plugin_id == "grok2api" else muse_built()


def open_console(url: str) -> str:
    webbrowser.open(url)
    return _tr('已在浏览器打开 {0}').format(f'{url}')
