"""Loopback ports for the local bridges.

Environment variables win over ``~/.route-manager/ports.json``, which wins over the defaults.
A bad value is reported and the default is kept so a tray process still starts.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

DEFAULTS = {
    "dot2api": 8788,
    "chatgpt_web": 17843,
    "gemini_web": 17849,
    "account_login": 17851,
    "easytier_rpc": 15888,
    "grok2api": 8790,
    "muse2api": 18610,
}

FEATURES = {
    "dot2api": "Dot2API local server",
    "chatgpt_web": "ChatGPT Web bridge",
    "gemini_web": "Gemini Web Chrome CDP",
    "account_login": "account-login Chrome CDP",
    "easytier_rpc": "EasyTier RPC portal",
    "grok2api": "Grok2API",
    "muse2api": "Muse2API",
}

ENV_KEYS = {
    "dot2api": ("AGENTCOMPASS_PORT_DOT2API", "DOT2API_PORT"),
    "chatgpt_web": ("AGENTCOMPASS_PORT_CHATGPT_WEB",),
    "gemini_web": ("AGENTCOMPASS_PORT_GEMINI_WEB", "GEMINI_WEB_CDP_PORT"),
    "account_login": ("AGENTCOMPASS_PORT_ACCOUNT_LOGIN",),
    "easytier_rpc": ("AGENTCOMPASS_PORT_EASYTIER",),
    "grok2api": ("AGENTCOMPASS_PORT_GROK2API",),
    "muse2api": ("AGENTCOMPASS_PORT_MUSE2API", "MUSE2API_PORT"),
}


def config_path(home: Path | None = None, environ: dict | None = None) -> Path:
    environ = os.environ if environ is None else environ
    override = (environ.get("AGENTCOMPASS_PORTS_FILE") or "").strip()
    if override:
        return Path(override).expanduser()
    if home is None:
        raw = environ.get("USERPROFILE") or environ.get("HOME")
        home = Path(raw) if raw else Path.home()
    return Path(home) / ".route-manager" / "ports.json"


def _parse_port(raw: object) -> int | None:
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        port = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if 1 <= port <= 65535:
        return port
    return None


def load(home: Path | None = None, environ: dict | None = None) -> dict:
    """Return ``{"ports": {name: int}, "problems": [str]}``. Defaults fill any gap."""
    environ = os.environ if environ is None else environ
    problems: list[str] = []
    ports = dict(DEFAULTS)
    path = config_path(home, environ)
    document: dict = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
            document = loaded if isinstance(loaded, dict) else {}
            if not isinstance(loaded, dict):
                problems.append(f"{path} is not a JSON object; defaults were kept")
        except (OSError, ValueError) as error:
            problems.append(f"{path} could not be read ({error}); defaults were kept")
    for name, default in DEFAULTS.items():
        if name in document:
            parsed = _parse_port(document[name])
            if parsed is None:
                problems.append(f"{name}: {document[name]!r} in {path} is not a TCP port; using {default}")
            else:
                ports[name] = parsed
        for key in ENV_KEYS[name]:
            raw = (environ.get(key) or "").strip()
            if not raw:
                continue
            parsed = _parse_port(raw)
            if parsed is None:
                problems.append(f"{name}: {key}={raw!r} is not a TCP port; using {ports[name]}")
            else:
                ports[name] = parsed
            break
    by_port: dict[int, list[str]] = {}
    for name, port in ports.items():
        by_port.setdefault(port, []).append(name)
    for port, names in sorted(by_port.items()):
        if len(names) > 1:
            problems.append(f"port {port} is assigned to {', '.join(names)}")
    return {"ports": ports, "problems": problems}


def get(name: str, home: Path | None = None, environ: dict | None = None) -> int:
    ports = load(home, environ)["ports"]
    if name not in ports:
        raise KeyError(name)
    return ports[name]


def problems(home: Path | None = None, environ: dict | None = None) -> list[str]:
    return load(home, environ)["problems"]


def conflicts(home: Path | None = None, environ: dict | None = None) -> list[str]:
    """Invalid assignments and ports given to more than one feature."""
    return problems(home, environ)


def parse_listeners(text: str) -> dict[int, list[tuple[str, int]]]:
    """Map a port to ``(local address, pid)`` rows from ``netstat -ano`` text."""
    found: dict[int, list[tuple[str, int]]] = {}
    for line in text.splitlines():
        parts = line.split()
        if "LISTENING" not in parts:
            continue
        state = parts.index("LISTENING")
        if state < 2 or state + 1 >= len(parts):
            continue
        local = parts[state - 2]
        host, _, port_text = local.rpartition(":")
        try:
            port = int(port_text)
            pid = int(parts[state + 1])
        except ValueError:
            continue
        found.setdefault(port, []).append((host, pid))
    return found


def _netstat() -> str:
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000
    try:
        done = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True, text=True, errors="replace", timeout=8, check=False, **kwargs,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"netstat failed: {error}"
    return done.stdout or ""


def listeners(ports: list[int], text: str | None = None) -> dict[int, list[tuple[str, int]]]:
    parsed = parse_listeners(text if text is not None else _netstat())
    return {port: parsed.get(port, []) for port in ports}
