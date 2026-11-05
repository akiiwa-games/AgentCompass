"""F13 probe: a checkout-era profile moves to the product layout; packaged shapes own every component.

    python -X utf8 f13_product_layout_probe.py TRAY_DIR SCRIPTS_DIR SETUP_DIR WORK OUT.json

Runs inside a foreign profile (HOME, ProgramData and the state directory are the sandbox's). TRAY_DIR is the
tray source, SCRIPTS_DIR and SETUP_DIR the skill scripts and setup files as installed from the wheel. Nothing here
elevates, registers a task, or starts a plugin: EasyTier's Task Scheduler queries are replaced, and the plugin
clones are synthetic git repositories with no processes.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

tray, scripts, setup_dir, work, out = (Path(arg) for arg in sys.argv[1:6])
sys.path[:0] = [str(tray), str(scripts)]
home = Path.home()
results: dict[str, dict] = {}


def check(name: str, ok, detail=None) -> None:
    results[name] = {"ok": bool(ok), "detail": detail}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=f13", "-c", "user.email=f13@example.invalid", *args],
                          capture_output=True, text=True, check=True).stdout


def clone(path: Path, with_venv: bool = False) -> None:
    path.mkdir(parents=True)
    git(path, "init", "-q")
    (path / "app.py").write_text("print('upstream')\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "upstream")
    (path / "app.py").write_text("print('local patch')\n")
    (path / "notes.local").write_text("untracked\n")


import easytier_paths  # noqa: E402
import plugin_migration  # noqa: E402
import plugins  # noqa: E402

# ---- the checkout-era layout: clones inside the checkout, a junction from the plugins directory to one of them
checkout = work / "checkout"
folders = {plugin_id: plugins.BY_ID[plugin_id].legacy_dir for plugin_id in plugin_migration.MIGRATABLE}
for folder in folders.values():
    clone(checkout / folder)
plugins_dir = plugins.PLUGINS_DIR
check("the plugins directory is the foreign profile's", plugins_dir.is_relative_to(home), str(plugins_dir))
plugins_dir.mkdir(parents=True, exist_ok=True)
if os.name == "nt":
    plugin_migration.make_link(plugins_dir / "usage-float", checkout / "usage-float", True)
before = {plugin_id: plugins.plugin_root(plugin_id, repo_root=checkout) for plugin_id in folders}
check("before: plugin roots resolve into the checkout (directly or through the junction)",
      all((root / "notes.local").is_file() for root in before.values()), {k: str(v) for k, v in before.items()})

lines: list[str] = []
code = plugin_migration.migrate(checkout, dry_run=True, log=lines.append)
check("dry run plans a move for every clone and changes nothing",
      code == 0 and all(f"{plugin_id}: move" in "\n".join(lines) for plugin_id in folders)
      and all((checkout / folder / ".git").is_dir() for folder in folders.values()), lines)

lines = []
code = plugin_migration.migrate(checkout, rebuild=lambda target: "no venv", log=lines.append)
moved = {plugin_id: plugins_dir / plugin_id for plugin_id in folders}
check("migration exits 0", code == 0, lines)
check("every clone now lives in ~/.agent-compass/plugins/<id> and the checkout copy is gone",
      all((path / ".git").is_dir() and not plugin_migration.exists_link(path) for path in moved.values())
      and not any((checkout / folder).exists() for folder in folders.values()), lines)
check("local changes and untracked files moved with the clones",
      all(git(path, "status", "--porcelain").split() == ["M", "app.py", "??", "notes.local"] for path in moved.values()),
      {k: git(v, "status", "--porcelain") for k, v in moved.items()})
check("after: plugin roots resolve to the product location even with the checkout still present",
      all(plugins.plugin_root(plugin_id, repo_root=checkout) == moved[plugin_id] for plugin_id in folders))
lines = []
code = plugin_migration.migrate(checkout, log=lines.append)
check("a second migration is a no-op", code == 0 and all(f"{plugin_id}: done" in "\n".join(lines) for plugin_id in folders), lines)

# ---- EasyTier: one per-machine home, found from frozen and wheel shapes; the checkout node until migrated
program_data = Path(os.environ["ProgramData"])
product = program_data / "AgentCompass" / "easytier"
check("ProgramData is the sandbox's", program_data.is_relative_to(work.parent), str(program_data))
frozen_root = work / "frozen-app"
(frozen_root / "_internal").mkdir(parents=True)
(frozen_root / "AgentCompass.exe").write_bytes(b"")
wheel_root = work / "venv" / "Lib" / "site-packages"
(wheel_root / "agentcompass-0.0.0.dist-info").mkdir(parents=True)
check("EasyTier home from a frozen-shaped install is %ProgramData%\\AgentCompass\\easytier",
      easytier_paths.home(root=frozen_root) == product, str(easytier_paths.home(root=frozen_root)))
check("EasyTier home from a wheel-shaped install is %ProgramData%\\AgentCompass\\easytier",
      easytier_paths.home(root=wheel_root) == product, str(easytier_paths.home(root=wheel_root)))
legacy = checkout / "easytier"
for relative in ("bin/easytier-core.exe", "bin/easytier-cli.exe", "private/node.toml", "state/legacy.json"):
    (legacy / relative).parent.mkdir(parents=True, exist_ok=True)
    (legacy / relative).write_bytes(b"fixture")
check("a checkout-era node is still found until it is migrated", easytier_paths.home(root=checkout) == legacy)
check("the tray's plugin root for EasyTier follows it", plugins.plugin_root("easytier", repo_root=checkout) == legacy)

import easytier_setup  # noqa: E402

easytier_setup.task_exists = lambda name: False      # never ask the real Task Scheduler
easytier_setup.relaunch_elevated = lambda argv: (_ for _ in ()).throw(AssertionError("dry run tried to elevate"))
buffer = io.StringIO()
with redirect_stdout(buffer):
    code = easytier_setup.main(["install", "--migrate-from", str(legacy), "--dry-run"])
check("easytier_setup install --migrate-from --dry-run plans the move to ProgramData without elevating",
      code == 0 and str(product) in buffer.getvalue() and not product.exists(), buffer.getvalue())
(product / "bin").mkdir(parents=True)
(product / "bin" / "easytier-core.exe").write_bytes(b"fixture")
check("once the node is at the product home it wins over the checkout copy", easytier_paths.home(root=checkout) == product)

# ---- wheel-installed skill and setup: per-user homes for grok-bot's CLI and the Paseo bridge's node_modules
import grok_bot_paths  # noqa: E402

check("grok-bot CLI installs under the state directory, not the wheel's skill",
      grok_bot_paths.install_dir() == home / ".agent-compass" / "grok-bot" / "runtime"
      and not grok_bot_paths.runtime_dir().is_relative_to(scripts.parents[2]), str(grok_bot_paths.runtime_dir()))
import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("installed_sync_host_globals", setup_dir / "sync_host_globals.py")
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)
check("the Paseo bridge's dependencies go under the state directory for a wheel install",
      sync.paseo_bridge_dir() == home / ".agent-compass" / "paseo-bridge", str(sync.paseo_bridge_dir()))

out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
