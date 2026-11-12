"""F13 probe: packaged shapes resolve plugins, EasyTier, grok-bot and Paseo bridge to their product locations.

    python -X utf8 f13_product_layout_probe.py TRAY_DIR SCRIPTS_DIR SETUP_DIR WORK OUT.json

Runs inside a foreign profile (HOME, ProgramData and the state directory are the sandbox's). TRAY_DIR is the
tray source, SCRIPTS_DIR and SETUP_DIR the skill scripts and setup files as installed from the wheel.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

tray, scripts, setup_dir, work, out = (Path(arg) for arg in sys.argv[1:6])
sys.path[:0] = [str(tray), str(scripts)]
home = Path.home()
results: dict[str, dict] = {}


def check(name: str, ok, detail=None) -> None:
    results[name] = {"ok": bool(ok), "detail": detail}


import easytier_paths  # noqa: E402
import grok_bot_paths  # noqa: E402
import plugins  # noqa: E402

checkout = work / "checkout"
for folder in ("usage-float", "codex-chatgpt-web", "gemini-web", "dot2api"):
    (checkout / folder / ".git").mkdir(parents=True, exist_ok=True)
plugins_dir = plugins.PLUGINS_DIR
check("the plugins directory is the foreign profile's", plugins_dir.is_relative_to(home), str(plugins_dir))
check("git-cloned plugin roots always resolve to ~/.agent-compass/plugins/<id> (never a checkout copy)",
      all(plugins.plugin_root(pid, repo_root=checkout) == plugins_dir / pid
          for pid in ("usage-float", "chatgpt-web", "gemini-web", "dot2api")))

program_data = Path(os.environ["ProgramData"])
product = program_data / "AgentCompass" / "easytier"
check("ProgramData is the sandbox's", program_data.is_relative_to(work.parent), str(program_data))
(checkout / "easytier" / "bin").mkdir(parents=True, exist_ok=True)
(checkout / "easytier" / "bin" / "easytier-core.exe").write_bytes(b"fixture")
check("EasyTier home is always %ProgramData%\\AgentCompass\\easytier",
      easytier_paths.home() == product and plugins.plugin_root("easytier", repo_root=checkout) == product,
      str(easytier_paths.home()))

check("grok-bot CLI installs under the state directory, not the wheel's skill",
      grok_bot_paths.install_dir() == home / ".agent-compass" / "grok-bot" / "runtime"
      and not grok_bot_paths.runtime_dir().is_relative_to(scripts.parents[2]), str(grok_bot_paths.runtime_dir()))

spec = importlib.util.spec_from_file_location("installed_sync_host_globals", setup_dir / "sync_host_globals.py")
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)
check("the Paseo bridge's dependencies go under the state directory for a wheel install",
      sync.paseo_bridge_dir() == home / ".agent-compass" / "paseo-bridge", str(sync.paseo_bridge_dir()))

out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
