"""First-run onboarding for a new AgentCompass user.

    python -X utf8 setup/onboard.py [setup] [--yes] [--dry-run] [--hooks | --no-hooks] ...
    python -X utf8 setup/onboard.py uninstall [--dry-run]

`setup` detects which agent CLIs are installed and signed in (claude, codex, agy, opencode, pi, grok, paseo), writes the
accounts the router may use into ~/.route-manager/accounts-override.json, optionally fetches the usage-float plugin and
runs one usage collection, links the router skill, writes the global agent rules and (only on request) the router
gate hook. Everything it changes is backed up and recorded so `uninstall` can reverse it.

Importable: `main(argv)`, `setup_main(argv)` and `uninstall_main(argv)` return process exit codes.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SETUP = Path(__file__).resolve().parent
REPO = SETUP.parent
sys.path.insert(0, str(SETUP))
sys.path.insert(0, str(REPO / "skills" / "route-agent-work" / "scripts"))

import provider_inventory  # noqa: E402
import sync_host_globals as sg  # noqa: E402

if str(REPO) not in sys.path:
    sys.path.append(str(REPO))
from agentcompass.origin import DEFAULT_ORG  # noqa: E402
# Accounts whose sign-in cannot be read from disk: an installed CLI is taken as signed in unless the user says otherwise.
UNVERIFIABLE_FAMILIES = ("antigravity", "opencode", "grok")
CLI_ACCOUNTS = ("claude", "codex", "pi", "antigravity", "opencode", "grok")
HOOK_EXPLANATION = (
    "The router gate hook blocks every sub-agent launch (including Claude Code's built-in Explore agent) "
    "unless `route_judge.py roll` recorded a routing decision in the last 15 minutes. It enforces routing, "
    "but it will block agents until you get used to running `roll` first. You can remove it at any time with "
    "`{undo}`."
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- detection -------------------------------------------------------------------------------------------------------


def detection_environ(home: Path, environ: dict | None = None) -> dict:
    """The environment discovery may see: the real PATH, but every provider location pinned under `home`.

    Inherited PASEO_HOME / CLAUDE_CONFIG_DIR / CODEX_HOME / ROUTER_* would otherwise point discovery at another
    user's (or the author's) real accounts, so they are dropped unless `home` is the real home directory.
    """
    source = dict(os.environ if environ is None else environ)
    env = {"PATH": source.get("PATH", "")}
    for key in ("SYSTEMROOT", "PATHEXT", "TEMP", "TMP"):
        if source.get(key):
            env[key] = source[key]
    env.update(ROUTER_PROVIDER_HOME=str(home), ROUTER_STATE_DIR=str(home / ".route-manager"),
               PASEO_HOME=str(home / ".paseo"), APPDATA=str(home / "AppData" / "Roaming"),
               LOCALAPPDATA=str(home / "AppData" / "Local"), USERPROFILE=str(home), HOME=str(home))
    try:
        if home.resolve() == Path.home().resolve():
            for key in ("PASEO_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "PI_CODING_AGENT_DIR"):
                if source.get(key):
                    env[key] = source[key]
    except OSError:
        pass
    return env


def detect(home: Path, environ: dict | None = None) -> dict:
    """Report installed CLIs and signed-in accounts under `home` without any network request.

    Returns ``{"accounts": [...], "paseo": {...}, "inventory": {...}}``. Each account row has ``signed_in``: True when
    credentials were found on disk, False when the CLI or its credentials are missing, None when the CLI is installed
    but its sign-in state cannot be read (Antigravity, OpenCode, Grok).
    """
    env = detection_environ(home, environ)
    inventory = provider_inventory.discover(home=home, environ=env)
    rows = []
    for name, row in inventory["accounts"].items():
        family = row.get("family")
        if family not in CLI_ACCOUNTS:
            continue
        status = row.get("status")
        if status == "ready":
            signed_in = None if family in UNVERIFIABLE_FAMILIES else True
        else:
            signed_in = False
        rows.append({"name": name, "family": family, "status": status, "signed_in": signed_in,
                     "plan": row.get("plan"), "home": row.get("home")})
    others = {name: inventory["accounts"][name].get("status") for name in ("dot", "chatgpt-web", "grok-bot")
              if name in inventory["accounts"]}
    paseo_cfg = Path(env["PASEO_HOME"]) / "config.json"
    paseo = {"installed": bool(shutil.which("paseo", path=env["PATH"])), "config": paseo_cfg.is_file()}
    return {"accounts": rows, "paseo": paseo, "others": others, "inventory": inventory}


# --- accounts-override.json ------------------------------------------------------------------------------------------


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def override_path(home: Path) -> Path:
    return home / ".route-manager" / "accounts-override.json"


def onboarding_path(home: Path) -> Path:
    return home / ".route-manager" / "onboarding.json"


def write_override(home: Path, detection: dict, disabled: set[str], dry_run: bool = False) -> dict:
    """Make exactly the detected, not-disabled accounts routable; never overwrite the user's own override entries."""
    path = override_path(home)
    previous_state = _read_json(onboarding_path(home))
    ours_before = set(previous_state.get("disabled_by_onboarding") or [])
    existed = path.is_file()
    document = _read_json(path) if existed else {}
    document.setdefault("schema_version", 2)
    accounts = document.setdefault("accounts", {})
    detected = {row["name"]: row for row in detection["accounts"]}
    wanted_off = {name for name in disabled if name in detected and detected[name]["signed_in"] is not False}
    added, removed = [], []
    for name in sorted(wanted_off):
        entry = accounts.setdefault(name, {})
        if entry.get("verified") is not False:
            if "verified" in entry and name not in ours_before:
                continue          # the user set this deliberately; leave it
            entry["verified"] = False
            added.append(name)
    for name in sorted(ours_before - wanted_off):
        entry = accounts.get(name)
        if isinstance(entry, dict) and entry.get("verified") is False:
            entry.pop("verified")
            removed.append(name)
            if not entry:
                accounts.pop(name)
    if not accounts:
        document.pop("accounts")
    changed = bool(added or removed) or not existed
    result = {"path": str(path), "created": not existed or bool(previous_state.get("override_created")),
              "added": added, "removed": removed, "disabled": sorted(wanted_off), "backup": None}
    if changed and not dry_run:
        if existed:
            backup = home / ".route-manager" / "backups" / f"onboard-{datetime.now().strftime('%Y%m%d-%H%M%S')}" / path.name
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup)
            result["backup"] = str(backup)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return result


# --- usage-float plugin ----------------------------------------------------------------------------------------------


def usage_float_repo_url() -> str:
    from agentcompass.origin import usage_float_repo
    return usage_float_repo()


def usage_float_dir(home: Path) -> Path | None:
    """An existing usage-float checkout: the user's plugin copy first, then a sibling folder of this checkout."""
    for candidate in (home / ".route-manager" / "plugins" / "usage-float", REPO / "usage-float"):
        if (candidate / "usage_float.py").is_file():
            return candidate
    return None


def ensure_usage_float(home: Path, dry_run: bool = False, run=subprocess.run) -> dict:
    existing = usage_float_dir(home)
    target = home / ".route-manager" / "plugins" / "usage-float"
    git = shutil.which("git")
    if existing is not None:
        if (existing / ".git").exists() and git and not dry_run:
            res = run([git, "-C", str(existing), "pull", "--ff-only"], capture_output=True, text=True, timeout=120)
            return {"path": str(existing), "action": "updated" if res.returncode == 0 else "kept",
                    "detail": (res.stderr or "").strip()[:200] or None}
        return {"path": str(existing), "action": "present", "detail": None}
    if not git:
        return {"path": str(target), "action": "skipped", "detail": "git is not installed; clone the usage-float repository manually"}
    if dry_run:
        return {"path": str(target), "action": "would-clone", "detail": usage_float_repo_url()}
    target.parent.mkdir(parents=True, exist_ok=True)
    res = run([git, "clone", "--depth", "1", usage_float_repo_url(), str(target)], capture_output=True, text=True, timeout=300)
    if res.returncode != 0:
        return {"path": str(target), "action": "failed", "detail": (res.stderr or res.stdout or "").strip()[:300]}
    return {"path": str(target), "action": "cloned", "detail": usage_float_repo_url()}


def collect_usage_once(home: Path, plugin: dict, dry_run: bool = False, run=subprocess.run) -> dict:
    cache = home / ".usage-float" / "usage-cache.json"
    if plugin["action"] in ("skipped", "failed", "would-clone"):
        return {"ran": False, "cache": str(cache), "ok": cache.is_file(), "detail": "usage-float is not available"}
    script = Path(plugin["path"]) / "usage_float.py"
    if dry_run:
        return {"ran": False, "cache": str(cache), "ok": cache.is_file(), "detail": f"would run: {sys.executable} {script} once"}
    env = {**os.environ, "USERPROFILE": str(home), "HOME": str(home), "PYTHONUTF8": "1"}
    for key in ("PASEO_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        if home.resolve() != Path.home().resolve():
            env.pop(key, None)
    try:
        res = run([sys.executable, "-X", "utf8", str(script), "once"], cwd=str(script.parent), env=env,
                  capture_output=True, text=True, timeout=240)
        detail = (res.stdout.strip() or res.stderr.strip())[:400]
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ran": True, "cache": str(cache), "ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    return {"ran": True, "cache": str(cache), "ok": cache.is_file(), "detail": detail}


# --- prompts and flow ------------------------------------------------------------------------------------------------


class Asker:
    def __init__(self, assume_yes: bool):
        self.interactive = not assume_yes and sys.stdin is not None and sys.stdin.isatty()

    def ask(self, question: str, default: bool) -> bool:
        if not self.interactive:
            return default
        suffix = "[Y/n]" if default else "[y/N]"
        try:
            answer = input(f"{question} {suffix} ").strip().lower()
        except EOFError:
            self.interactive = False
            print()
            return default
        return default if not answer else answer.startswith("y")


def _packaged() -> bool:
    return "site-packages" in Path(__file__).resolve().parts


def _undo_command() -> str:
    return "agentcompass uninstall" if _packaged() else "python -X utf8 setup/onboard.py uninstall"


def _try_command() -> str:
    return "agentcompass providers" if _packaged() else "python -X utf8 skills/route-agent-work/scripts/route_judge.py providers"


def _line(text: str = "") -> None:
    print(text)


def setup_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="onboard.py setup", description="Detect accounts and set AgentCompass up safely.")
    parser.add_argument("--yes", "-y", action="store_true", help="Non-interactive: accept the safe defaults (no hook unless --hooks).")
    parser.add_argument("--dry-run", action="store_true", help="Report what would change; write nothing.")
    parser.add_argument("--hooks", dest="hooks", action="store_true", default=None, help="Install the router gate hook.")
    parser.add_argument("--no-hooks", dest="hooks", action="store_false", help="Never install the router gate hook.")
    parser.add_argument("--no-docs", action="store_true", help="Do not write global agent rules or the Paseo prompt.")
    parser.add_argument("--no-skills", action="store_true", help="Do not link the router skills into the harnesses.")
    parser.add_argument("--no-usage", action="store_true", help="Do not fetch usage-float or run a usage collection.")
    parser.add_argument("--disable", action="append", default=[], metavar="ACCOUNT",
                        help="Keep this detected account out of routing (repeatable).")
    parser.add_argument("--home", help="Operate on another home directory (testing).")
    return parser


def setup_main(argv: list[str] | None = None) -> int:
    args = setup_parser().parse_args(argv)
    home = Path(args.home).expanduser().resolve() if args.home else Path.home()
    sg.set_home(home)
    asker = Asker(args.yes)
    dry = args.dry_run
    summary: dict = {"home": str(home), "dry_run": dry, "at": now_iso()}

    _line(f"AgentCompass onboarding for {home}" + (" (dry run: nothing will be written)" if dry else ""))

    # 1. Detect
    detection = detect(home)
    summary["detected"] = [{k: v for k, v in row.items() if k != "home"} for row in detection["accounts"]]
    _line("\nDetected agent CLIs:")
    for row in detection["accounts"]:
        state = {True: "signed in", False: "not available", None: "installed (sign-in cannot be verified)"}[row["signed_in"]]
        _line(f"  {row['name']:<12} {state}" + (f"  plan={row['plan']}" if row.get("plan") else ""))
    paseo = detection["paseo"]
    _line(f"  {'paseo':<12} " + ("installed" if paseo["installed"] else "not found") + (", config present" if paseo["config"] else ""))
    usable = [r for r in detection["accounts"] if r["signed_in"] is not False]
    if not usable:
        _line("\nNo signed-in agent CLI was found. Sign in to at least one (for example `claude` or `codex`) and run this again;")
        _line("until then the router reports 'no ready account' instead of guessing.")

    # 2. Which unverifiable accounts to keep
    disabled = {name for name in args.disable}
    for row in usable:
        if row["signed_in"] is None and row["name"] not in disabled:
            if not asker.ask(f"{row['name']} is installed but its sign-in cannot be verified. Route work to it?", True):
                disabled.add(row["name"])
    override = write_override(home, detection, disabled, dry)
    summary["override"] = override
    _line(f"\nAccounts the router may use: {', '.join(r['name'] for r in usable if r['name'] not in disabled) or 'none'}")
    if disabled:
        _line(f"Kept out of routing: {', '.join(sorted(disabled))}")
    _line(f"  {'would write' if dry else 'wrote'} {override['path']}")

    # 3. usage-float plugin and one collection
    if args.no_usage:
        summary["usage"] = {"skipped": True}
    elif asker.ask("\nFetch the usage-float plugin and run one usage collection now?", True):
        plugin = ensure_usage_float(home, dry)
        collection = collect_usage_once(home, plugin, dry)
        summary["usage"] = {"plugin": plugin, "collection": collection}
        _line(f"  usage-float: {plugin['action']} ({plugin['path']})" + (f" - {plugin['detail']}" if plugin.get("detail") else ""))
        _line(f"  usage collection: {'ok' if collection['ok'] else 'no data yet'} ({collection['cache']})")
    else:
        summary["usage"] = {"declined": True}

    # 4. Global rules, skills, hooks
    hooks = args.hooks
    if hooks is None and asker.interactive:
        _line("\n" + HOOK_EXPLANATION)
        hooks = asker.ask("Install the router gate hook?", False)
    do_docs, do_skills = not args.no_docs, not args.no_skills
    if do_docs and not asker.ask("\nWrite the global agent rules (a managed block; your own text in those files is kept)?", True):
        do_docs = False
    _line("")
    lines = sg.sync(docs=do_docs, skills=do_skills, hooks=hooks, dry_run=dry, link_repo_skills=do_skills)
    summary["sync"] = lines

    # 5. Summary
    summary["hooks"] = "installed" if (hooks or (hooks is None and sg.hooks_installed())) else "not installed"
    if not dry:
        state = {**summary, "disabled_by_onboarding": override["added"] + [n for n in (_read_json(onboarding_path(home)).get("disabled_by_onboarding") or []) if n not in override["removed"]],
                 "override_created": bool(override["created"])}
        state["disabled_by_onboarding"] = sorted(set(state["disabled_by_onboarding"]))
        onboarding_path(home).parent.mkdir(parents=True, exist_ok=True)
        onboarding_path(home).write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    _line("\nSummary")
    _line(f"  accounts file : {override['path']}")
    _line(f"  global rules  : {'written' if do_docs else 'skipped'}  (overlay folder: {home / '.route-manager' / 'host-rules'})")
    _line(f"  router hook   : {summary['hooks']}")
    _line(f"  backups       : {home / '.route-manager' / 'backups'}")
    _line(f"  undo          : {_undo_command()}")
    if dry:
        _line("\nDry run complete; nothing was changed.")
    else:
        _line(f"\nTry it: {_try_command()}")
    return 0


def uninstall_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="onboard.py uninstall", description="Undo what `setup` changed.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yes", "-y", action="store_true")
    parser.add_argument("--home", help="Operate on another home directory (testing).")
    args = parser.parse_args(argv)
    home = Path(args.home).expanduser().resolve() if args.home else Path.home()
    sg.set_home(home)
    code = sg.uninstall(args.dry_run)
    state = _read_json(onboarding_path(home))
    path = override_path(home)
    if state and path.is_file():
        document = _read_json(path)
        accounts = document.get("accounts") if isinstance(document.get("accounts"), dict) else {}
        for name in state.get("disabled_by_onboarding") or []:
            entry = accounts.get(name)
            if isinstance(entry, dict) and entry.get("verified") is False:
                entry.pop("verified")
                if not entry:
                    accounts.pop(name)
        if not accounts:
            document.pop("accounts", None)
        if state.get("override_created") and set(document) <= {"schema_version"}:
            if not args.dry_run:
                path.unlink()
            print(f"[uninstall] {path}: removed (created by setup)")
        else:
            if not args.dry_run:
                path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
            print(f"[uninstall] {path}: onboarding entries removed")
    if state and not args.dry_run:
        onboarding_path(home).unlink(missing_ok=True)
    plugin = home / ".route-manager" / "plugins" / "usage-float"
    if plugin.exists():
        print(f"[uninstall] {plugin} was left in place (remove it by hand if you do not use usage-float).")
    return code


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "uninstall":
        return uninstall_main(argv[1:])
    if argv and argv[0] == "setup":
        argv = argv[1:]
    return setup_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
