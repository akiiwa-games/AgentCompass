"""Read-only report of the local AgentCompass install. This module does not write."""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import sys
import time
from pathlib import Path

from agentcompass import __version__
from agentcompass import locations
from agentcompass.ports import FEATURES, conflicts, listeners, load
from agentcompass.tools import HINTS

CLI_NAMES = ("claude", "codex", "agy", "grok", "npm", "git", "paseo", "python")
_HOOK_MARKER = "router_gate_hook.py"


def _which(names: tuple[str, ...], path: str | None = None) -> str | None:
    for name in names:
        found = shutil.which(name, path=path)
        if found:
            return found
    return None


def pinned_environ(home: Path) -> dict[str, str]:
    """PATH stays so CLIs can be found. Account locations are pinned under ``home``."""
    env = {"PATH": os.environ.get("PATH", "")}
    for key in ("SYSTEMROOT", "PATHEXT", "TEMP", "TMP", "WINDIR"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    env.update(
        USERPROFILE=str(home),
        HOME=str(home),
        APPDATA=str(home / "AppData" / "Roaming"),
        LOCALAPPDATA=str(home / "AppData" / "Local"),
        ROUTER_PROVIDER_HOME=str(home),
        ROUTER_STATE_DIR=str(home / ".route-manager"),
        ROUTER_USAGE_CACHE=str(home / ".usage-float" / "usage-cache.json"),
        PASEO_HOME=str(home / ".paseo"),
    )
    return env


def _real_home(home: Path) -> bool:
    try:
        return home.resolve() == Path.home().resolve()
    except OSError:
        return False


def account_rows(home: Path, environ: dict | None) -> list | str:
    """Statuses only. Credential material from the inventory is not copied out."""
    try:
        scripts = locations.skill_root() / "route-agent-work" / "scripts"
        script = scripts / "provider_inventory.py"
        if not script.is_file():
            return "unavailable (route-agent-work is not installed)"
        if str(scripts) not in sys.path:
            sys.path.append(str(scripts))
        spec = importlib.util.spec_from_file_location("agentcompass_provider_inventory", script)
        if spec is None or spec.loader is None:
            return "unavailable (provider inventory could not be loaded)"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        found = module.discover(home=home, environ=environ)
    except Exception as error:
        return f"unavailable ({error.__class__.__name__}: {error})"
    accounts = found.get("accounts") if isinstance(found, dict) else None
    if not isinstance(accounts, dict):
        return "unavailable (inventory returned no accounts)"
    rows = []
    for name in sorted(accounts):
        row = accounts[name]
        if not isinstance(row, dict):
            continue
        rows.append({
            "name": name,
            "family": row.get("family"),
            "status": row.get("status"),
            "plan": row.get("plan"),
            "home": row.get("home"),
        })
    return rows


def _tool_lines(checkout: Path | None) -> list[str]:
    if checkout is not None and (checkout / "tray" / "runtime_paths.py").is_file():
        tray = str(checkout / "tray")
        if tray not in sys.path:
            sys.path.append(tray)
        import runtime_paths

        lines = []
        for label, fn in (
            ("uv", runtime_paths.uv_path),
            ("bun", runtime_paths.bun_path),
            ("cloudflared", runtime_paths.cloudflared_path),
            ("node", runtime_paths.node_path),
        ):
            try:
                lines.append(f"  {label}: {fn()}")
            except runtime_paths.ToolMissing as error:
                lines.append(f"  {label}: missing — {error}")
            except OSError as error:
                lines.append(f"  {label}: missing — {error}")
        return lines
    lines = []
    for file_name, item in HINTS.items():
        found = _which((item["tool"], file_name))
        if found:
            lines.append(f"  {item['tool']}: {found}")
        else:
            lines.append(f"  {item['tool']}: missing — {item['feature']} needs it. {item['install']}")
    return lines


def _hook_paths(home: Path) -> list[Path]:
    paths = [
        home / ".claude" / "settings.json",
        home / ".codex" / "hooks.json",
        home / ".gemini" / "config" / "hooks.json",
        home / ".pi" / "agent" / "extensions" / "router-gate.ts",
    ]
    for pattern, filename in ((".claude-*", "settings.json"), (".codex-*", "hooks.json")):
        try:
            children = list(home.glob(pattern))
        except OSError:
            children = []
        for directory in children:
            if directory.is_dir():
                paths.append(directory / filename)
    return paths


def _hook_installed(path: Path) -> str:
    try:
        if not path.is_file():
            return "absent"
        if path.stat().st_size > 2_000_000:
            return "unreadable"
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "unreadable"
    return "installed" if _HOOK_MARKER in text else "not installed"


def _cache_line(home: Path, environ: dict | None) -> str:
    override = ""
    if environ is not None:
        override = (environ.get("ROUTER_USAGE_CACHE") or "").strip()
    elif os.environ.get("ROUTER_USAGE_CACHE"):
        override = os.environ["ROUTER_USAGE_CACHE"].strip()
    path = Path(override) if override else home / ".usage-float" / "usage-cache.json"
    try:
        stat = path.stat()
    except OSError:
        return f"missing ({path})"
    age_min = max(0, int((time.time() - stat.st_mtime) // 60))
    return f"{path} ({age_min} min old, {stat.st_size} bytes)"


def _plugin_lines(home: Path) -> list[str]:
    path = home / ".route-manager" / "plugins.json"
    lines = []
    try:
        import json
        document = json.loads(path.read_text(encoding="utf-8-sig"))
        rows = document.get("plugins") if isinstance(document, dict) else None
        if not isinstance(rows, dict):
            lines.append(f"  config: {path} (no plugin table)")
        else:
            lines.append(f"  config: {path}")
            for name in sorted(rows):
                row = rows[name] if isinstance(rows[name], dict) else {}
                enabled = row.get("enabled")
                autostart = row.get("autostart")
                lines.append(f"  {name}: enabled={enabled} autostart={autostart}")
    except OSError:
        lines.append("  config: missing")
    except ValueError:
        lines.append(f"  config: unreadable ({path})")
    directory = home / ".route-manager" / "plugins"
    try:
        names = sorted(item.name for item in directory.iterdir() if item.is_dir()) if directory.is_dir() else []
    except OSError:
        names = []
    lines.append("  directories: " + (", ".join(names) if names else "none"))
    return lines


def collect(home: Path | None = None) -> dict:
    home = Path.home() if home is None else Path(home)
    environ = None if _real_home(home) else pinned_environ(home)
    checkout = locations.checkout_root()
    try:
        skill = str(locations.skill_root() / "route-agent-work")
    except FileNotFoundError as error:
        skill = f"missing ({error})"
    port_state = load(home, environ if environ is not None else os.environ)
    return {
        "version": __version__,
        "python": f"{sys.version.split()[0]} ({sys.executable})",
        "install": f"checkout {checkout}" if checkout else f"package {locations.package_dir()}",
        "skill": skill,
        "clis": [(name, _which((name, f"{name}.exe"), None if environ is None else environ.get("PATH")) or "not found")
                 for name in CLI_NAMES],
        "tools": _tool_lines(checkout),
        "accounts": account_rows(home, environ),
        "cache": _cache_line(home, environ),
        "plugins": _plugin_lines(home),
        "ports": port_state,
        "listeners": listeners(list(port_state["ports"].values())),
        "conflicts": conflicts(home, environ if environ is not None else os.environ),
        "hooks": [(str(path), _hook_installed(path)) for path in _hook_paths(home)],
    }


def render(report: dict) -> str:
    lines = [
        f"AgentCompass {report.get('version', __version__)} doctor",
        f"Python: {report.get('python', '')}",
        f"Install: {report.get('install', '')}",
        f"Skill: {report.get('skill', '')}",
        "CLIs:",
    ]
    for name, found in report.get("clis") or []:
        lines.append(f"  {name}: {found}")
    lines.append("Tools:")
    lines.extend(report.get("tools") or ["  (none)"])
    lines.append("Accounts:")
    accounts = report.get("accounts")
    if isinstance(accounts, str):
        lines.append(f"  {accounts}")
    elif not accounts:
        lines.append("  none detected")
    else:
        for row in accounts:
            plan = f" plan={row.get('plan')}" if row.get("plan") else ""
            lines.append(
                f"  {row.get('name')}: {row.get('status')} family={row.get('family')}{plan} home={row.get('home')}"
            )
    lines.append(f"Usage cache: {report.get('cache', '')}")
    lines.append("Plugins:")
    lines.extend(report.get("plugins") or ["  none"])
    lines.append("Ports:")
    ports = (report.get("ports") or {}).get("ports") or {}
    listening = report.get("listeners") or {}
    for name in ports:
        port = ports[name]
        rows = listening.get(port) or []
        feature = FEATURES.get(name, name)
        if not rows:
            lines.append(f"  {name}: {port} free ({feature})")
        else:
            detail = ", ".join(f"pid {pid} on {host}" for host, pid in rows)
            exposed = [host for host, _pid in rows if host not in {"127.0.0.1", "[::1]"}]
            suffix = " not-loopback-only" if exposed else ""
            lines.append(f"  {name}: {port} in use ({feature}) {detail}{suffix}")
    conflict_rows = report.get("conflicts") or []
    lines.append("Port conflicts: " + ("; ".join(conflict_rows) if conflict_rows else "none"))
    lines.append("Hooks:")
    for path, state in report.get("hooks") or []:
        lines.append(f"  {path}: {state}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentcompass doctor", description="Read-only local AgentCompass report.")
    parser.add_argument("--home", help="Read this home directory instead of the current user (testing).")
    args = parser.parse_args(argv)
    home = Path(args.home).expanduser() if args.home else None
    sys.stdout.write(render(collect(home)))
    return 0
