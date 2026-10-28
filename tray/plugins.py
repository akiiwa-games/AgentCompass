"""Optional plugins and their on/off state (~/.route-manager/plugins.json).

The core (router, capacity statistics, the headless usage service) always runs. Each plugin below can be
enabled or disabled; only enabled plugins whose click opens a window appear in the tray's first menu level.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from i18n import translate

STATE_DIR = Path.home() / ".route-manager"
PATH = STATE_DIR / "plugins.json"
PLUGINS_DIR = STATE_DIR / "plugins"
ROOT = Path(__file__).resolve().parents[1]
LEGACY_UF_AUTOSTART_OFF = STATE_DIR / "usage-float-autostart-disabled"


def _sibling_repo(name: str) -> str:
    org = os.environ.get("ROUTE_MANAGER_GITHUB_ORG", "").strip()
    if not org:
        try:
            cfg = (ROOT / ".git" / "config").read_text(encoding="utf-8", errors="ignore")
            m = re.search(r"github\.com[:/]([^/\s]+)/", cfg)
            org = m.group(1) if m else "agent-compass"
        except OSError:
            org = "agent-compass"
    return f"https://github.com/{org}/{name}.git"


@dataclass(frozen=True)
class Plugin:
    id: str
    name: str
    short: str
    _description: str
    has_window_action: bool
    autostart: bool = True
    color: str = "#4F46E5"
    repo: str | None = None
    legacy_dir: str | None = None
    enabled_by_default: bool = True    # False: opt-in, nothing is downloaded until the user enables it

    @property
    def description(self) -> str:
        return translate(self._description)


# Fixed order: the tray menu and the panel list plugins in this order.
PLUGINS = (
    Plugin("usage-float", "Usage Float", "UF", "桌面额度悬浮窗", True, color="#10B981",
           repo=_sibling_repo("usage-float"), legacy_dir="usage-float"),
    Plugin("chatgpt-web", "ChatGPT Web", "Web", "ChatGPT Pro 网页桥", True, color="#0EA5E9",
           repo="https://github.com/miuuyy/codex-chatgpt-web.git", legacy_dir="codex-chatgpt-web"),
    Plugin("gemini-web", "Gemini Web", "Gemini", "Gemini / AI Studio 专用浏览器", True,
           autostart=False, color="#6366F1", repo=_sibling_repo("gemini-web"),
           legacy_dir="gemini-web"),
    Plugin("easytier", "EasyTier", "ET", "虚拟局域网节点", True, color="#14B8A6",
           repo=None, legacy_dir="easytier"),
    Plugin("dot2api", "Dot2API", "Dot", "Dot 远端任务队列与隧道", False, color="#8B5CF6",
           repo="https://github.com/Pluviobyte/dot2api.git", legacy_dir="dot2api"),
    Plugin("grok-bot", "Grok Bot CLI", "Bot", "Grok Bot 远端任务", False,
           autostart=False, color="#111827", legacy_dir="skills/route-agent-work/grok-bot"),
    Plugin("grok2api", "Grok2API", "Grok", "Grok 账号池 OpenAI/Anthropic 网关", False, color="#111827",
           repo="https://github.com/chenyme/grok2api.git", enabled_by_default=False),
    Plugin("muse2api", "Muse2API", "Muse", "muse.ai 聊天、出图、视频网关", False, color="#EC4899",
           repo="https://github.com/www222fff/muse2api.git", enabled_by_default=False),
)
BY_ID = {plugin.id: plugin for plugin in PLUGINS}


def plugin_root(plugin_id: str, plugins_dir: Path | None = None, repo_root: Path | None = None) -> Path:
    """Resolve plugin directory in ~/.route-manager/plugins/<id> (falling back to legacy checkout if present)."""
    plugin = BY_ID[plugin_id]
    base_root = repo_root or ROOT
    if plugin.repo is None:
        return base_root / (plugin.legacy_dir or plugin_id)
    target = (plugins_dir or PLUGINS_DIR) / plugin_id
    if (target / ".git").exists():
        return target
    legacy = base_root / (plugin.legacy_dir or plugin_id)
    if (legacy / ".git").exists():
        return legacy
    return target


def defaults() -> dict:
    return {plugin.id: {"enabled": plugin.enabled_by_default, "autostart": plugin.autostart} for plugin in PLUGINS}


def load(path: Path | None = None) -> dict:
    """{plugin id: {'enabled', 'autostart'}}. A missing or broken file means the defaults (opt-in plugins off)."""
    path = path or PATH
    config = defaults()
    stored = None
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
        stored = document.get("plugins") if isinstance(document, dict) else None
    except (OSError, ValueError):
        pass
    if not isinstance(stored, dict):
        stored = {}
        # Before plugins.json, Usage Float's autostart was switched off by a marker file.
        if (path.parent / LEGACY_UF_AUTOSTART_OFF.name).exists():
            config["usage-float"]["autostart"] = False
    for plugin_id, row in stored.items():
        if plugin_id in config and isinstance(row, dict):
            for key in ("enabled", "autostart"):
                if isinstance(row.get(key), bool):
                    config[plugin_id][key] = row[key]
    return config


def save(config: dict, path: Path | None = None) -> None:
    path = path or PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = {pid: {"enabled": bool(config[pid]["enabled"]), "autostart": bool(config[pid]["autostart"])}
             for pid in BY_ID if pid in config}
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps({"v": 1, "plugins": clean}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def set_flag(plugin_id: str, key: str, value: bool, path: Path | None = None) -> dict:
    if plugin_id not in BY_ID or key not in ("enabled", "autostart"):
        raise ValueError(f"unknown plugin setting {plugin_id}.{key}")
    config = load(path)
    config[plugin_id][key] = bool(value)
    save(config, path)
    return config


def enabled(plugin_id: str, config: dict | None = None) -> bool:
    return bool((config or load())[plugin_id]["enabled"])


def autostarts(plugin_id: str, config: dict | None = None) -> bool:
    row = (config or load())[plugin_id]
    return bool(row["enabled"] and row["autostart"])


def menu_plugins(config: dict) -> list[Plugin]:
    """Enabled plugins whose click opens something, in menu order."""
    return [plugin for plugin in PLUGINS if plugin.has_window_action and config[plugin.id]["enabled"]]
