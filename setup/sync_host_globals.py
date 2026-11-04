"""Unify global agent instructions, link shared skills and (optionally) install the router gate across local harnesses.

What it touches, and how it stays reversible:

* Which harness homes are touched comes from a single scan: ``provider_inventory.discover(home=HOME, environ=...)``,
  paired with ``FAMILY_SPECS`` below (one per-family table of global instruction files, rule kinds, skills layout and
  hook locations).
* Global rule files (~/.claude/CLAUDE.md, ~/.codex/AGENTS.md, ...) get a managed block between
  ``<!-- agentcompass:begin -->`` and ``<!-- agentcompass:end -->``; everything outside it is preserved. A file that
  already equals the generated text is treated as wholly managed (the pre-marker behaviour), as is one recorded as such.
* The rule text is the neutral default in ``host_rules.py`` unless an overlay exists in ``~/.agent-compass/host-rules/``.
* Every file that is about to change is first copied into a per-run backup directory
  ``~/.agent-compass/backups/host-setup-<timestamp>/``; nothing is skipped because an older backup exists.
* Every change is recorded in ``~/.agent-compass/setup-manifest.json`` so ``uninstall()`` can remove the managed blocks,
  hook entries, junctions and Paseo config changes again (restoring backups where a whole file was replaced).
* The router gate hook is installed only when asked for (``--hooks``) or when it is already installed.
"""
from __future__ import annotations

import argparse
import filecmp
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SETUP_DIR = Path(__file__).resolve().parent
REPO_ROOT = SETUP_DIR.parent
sys.path.insert(0, str(SETUP_DIR))
for _scripts_dir in (
    REPO_ROOT / "skills" / "agent-compass" / "scripts",
    Path(os.environ["ROUTER_SKILL_ROOT"]) / "scripts" if os.environ.get("ROUTER_SKILL_ROOT") else None,
):
    if _scripts_dir is not None and _scripts_dir.is_dir() and str(_scripts_dir) not in sys.path:
        sys.path.insert(0, str(_scripts_dir))

import host_rules  # noqa: E402
import provider_inventory  # noqa: E402

CRLF, LF = "\r\n", "\n"
MARKER_BEGIN = "<!-- agentcompass:begin -->"
MARKER_END = "<!-- agentcompass:end -->"

PYTHON_EXE = Path(sys.executable).resolve().as_posix()
ROUTER_HOOK_SCRIPT = Path(__file__).resolve().with_name("router_gate_hook.py").as_posix()
ROUTER_HOOK_CMD = shlex.join([PYTHON_EXE, "-X", "utf8", ROUTER_HOOK_SCRIPT])

ROUTER_GATE_MATCHER = (
    ".*request_user_input_async|.*request_user_input|.*AskUserQuestion|.*ask_user_question|"
    "Agent|Task|dispatch_agent|spawn_agent|invoke_subagent|create_agent|mcp__paseo__create_agent|"
    "send_agent_prompt|mcp__paseo__send_agent_prompt|run_turn|mcp__paseo__run_turn|send_message|mcp__paseo__send_message|"
    "codemode|execute_code|mcp__paseo__execute_code|Bash|bash|PowerShell|powershell|exec|exec_command|run_command|shell"
)

PI_ROUTER_GATE_TS = """import { spawnSync } from "node:child_process";

const PYTHON_EXE = __PYTHON_EXE__;
const HOOK_SCRIPT = __HOOK_SCRIPT__;
const SUBAGENT_TOOLS = new Set([
  "request_user_input_async",
  "request_user_input",
  "AskUserQuestion",
  "ask_user_question",
  "Agent",
  "Task",
  "dispatch_agent",
  "spawn_agent",
  "invoke_subagent",
  "create_agent",
  "mcp__paseo__create_agent",
  "send_agent_prompt",
  "mcp__paseo__send_agent_prompt",
  "run_turn",
  "mcp__paseo__run_turn",
  "send_message",
  "mcp__paseo__send_message",
  "codemode",
  "execute_code",
  "mcp__paseo__execute_code",
  "Bash",
  "bash",
  "PowerShell",
  "powershell",
  "exec",
  "exec_command",
  "run_command",
  "shell",
]);

export default function (pi: any) {
  pi.on("tool_call", async (event: any) => {
    const toolName = String(event?.toolName || "");
    if (!SUBAGENT_TOOLS.has(toolName) && !/request_user_input|AskUserQuestion|ask_user_question/i.test(toolName)) {
      return undefined;
    }
    try {
      const payload = JSON.stringify({
        toolName,
        input: event?.input || {},
        cwd: process.cwd(),
      });
      const res = spawnSync(PYTHON_EXE, ["-X", "utf8", HOOK_SCRIPT], {
        input: payload,
        encoding: "utf-8",
        timeout: 8000,
        windowsHide: true,
      });
      if (res.status === 0 && res.stdout) {
        const parsed = JSON.parse(res.stdout.trim());
        if (parsed && parsed.block) {
          return { block: true, reason: parsed.reason || "Blocked by Router Gate" };
        }
      }
    } catch {
      // Fail-open if python is unexpectedly unavailable
    }
    return undefined;
  });
}
""".replace("__PYTHON_EXE__", json.dumps(PYTHON_EXE)).replace("__HOOK_SCRIPT__", json.dumps(ROUTER_HOOK_SCRIPT))

# Per-family table of global instruction files, rule kinds, skills layout and router-gate hook locations.
# Checked against each harness's own documentation and source:
# - grok (Grok CLI 1.0.70): `~/.grok/docs/user-guide/12-project-rules.md` and `grok inspect` show Grok enables
#   `[compat.claude] agents = true`, `skills = true`, `hooks = true` by default and already loads `~/.claude/CLAUDE.md`,
#   `~/.claude/skills/` and `~/.claude/settings.json`; writing `~/.grok/AGENTS.md` as well would load rules twice.
# - omp (Oh My Pi): Paseo's `@getpaseo/server` (`providers/omp/runtime.js`) passes `daemon.appendSystemPrompt` via
#   `--append-system-prompt`, while `~/.omp/agent/SYSTEM.md` replaces the base system prompt template.
# - copilot (GitHub Copilot CLI): Paseo's `copilot --acp` slot is `PASEO_RECOGNIZED_ONLY` without local discovery.
FAMILY_SPECS: dict[str, dict] = {
    "codex": {
        "default_home": ".codex",
        "rule_kind": "codex",
        "docs": (("AGENTS.md", "{home_tag}-AGENTS.md"),),
        "skills_dir": "skills",
        "skills_mode": "codex",
        "hook": ("hooks.json", "{home_tag}-hooks.json", "codex"),
    },
    "claude": {
        "default_home": ".claude",
        "rule_kind": "shared",
        "docs": (("CLAUDE.md", "{home_tag}-CLAUDE.md"),),
        "skills_dir": "skills",
        "skills_mode": "claude",
        "hook": ("settings.json", "{home_tag}-settings.json", "claude"),
    },
    "pi": {
        "default_home": ".pi/agent",
        "presence_dir": ".pi",
        "rule_kind": "shared",
        "docs": (("AGENTS.md", "pi-agent-AGENTS.md"),),
        "skills_dir": "skills",
        "skills_mode": "pi",
        "hook": ("extensions/router-gate.ts", "pi-router-gate.ts", "pi"),
    },
    "antigravity": {
        "default_home": ".gemini",
        "rule_kind": "shared",
        "docs": (
            ("GEMINI.md", "gemini-GEMINI.md"),
            ("config/GEMINI.md", "gemini-config-GEMINI.md"),
        ),
        "skills_dir": "skills",
        "skills_mode": "gemini",
        "hook": ("config/hooks.json", "gemini-hooks.json", "gemini"),
    },
    "opencode": {
        "default_home": ".config/opencode",
        "rule_kind": "shared",
        "docs": (("AGENTS.md", "opencode-AGENTS.md"),),
        "skills_dir": None,
        "skills_mode": None,
        "hook": None,
    },
}

# --- run state -------------------------------------------------------------------------------------------------------

HOME = Path.home()
HOME_POSIX = HOME.as_posix()
RUN_ID = datetime.now().strftime("%Y%m%d-%H%M%S")
DRY_RUN = False
MANIFEST: dict = {}
ACTIONS: list[str] = []


def set_home(path: Path | str | None = None) -> None:
    """Point every derived location at another home directory (tests, onboarding into a sandbox)."""
    global HOME, HOME_POSIX
    HOME = Path(path) if path is not None else Path.home()
    HOME_POSIX = HOME.as_posix()


def state_dir() -> Path:
    return HOME / ".agent-compass"


def manifest_path() -> Path:
    return state_dir() / "setup-manifest.json"


def backup_root() -> Path:
    return state_dir() / "backups" / f"host-setup-{RUN_ID}"


def report(message: str) -> None:
    ACTIONS.append(message)
    print(message)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_manifest() -> dict:
    try:
        data = json.loads(manifest_path().read_text(encoding="utf-8-sig"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def new_run_id() -> str:
    """A timestamp id whose backup directory does not exist yet, so no run ever shares or skips another run's backups."""
    base = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate, n = base, 1
    while (state_dir() / "backups" / f"host-setup-{candidate}").exists():
        n += 1
        candidate = f"{base}-{n}"
    return candidate


def begin_run(dry_run: bool = False) -> None:
    global DRY_RUN, MANIFEST, RUN_ID
    DRY_RUN = dry_run
    RUN_ID = new_run_id()
    ACTIONS.clear()
    MANIFEST = load_manifest()
    MANIFEST.setdefault("schema", 1)
    for key in ("docs", "junctions", "hooks", "files", "materialized"):
        MANIFEST.setdefault(key, {})
    MANIFEST.setdefault("paseo", {})
    MANIFEST.setdefault("runs", [])


def save_manifest() -> None:
    if DRY_RUN:
        return
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(MANIFEST, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


# --- filesystem helpers ----------------------------------------------------------------------------------------------


def is_reparse_point(path: Path) -> bool:
    try:
        return bool(os.lstat(path).st_file_attributes & 0x400)
    except (OSError, AttributeError):
        return path.is_symlink()


def _within_home(path: Path, home: Path | None = None) -> bool:
    """True when `path` resolves inside `home` (strictly enforced whenever `home` is not the real user home)."""
    base = Path(home) if home is not None else HOME
    try:
        base_res = base.resolve()
        if base_res == Path.home().resolve():
            return True
        lexical = Path(os.path.abspath(path))
        base_abs = Path(os.path.abspath(base))
        if not (lexical == base_abs or base_abs in lexical.parents):
            return False
        check_path = path.parent if is_reparse_point(path) else path
        resolved = check_path.resolve()
        return resolved == base_res or base_res in resolved.parents
    except OSError:
        return False


def _require_within_home(path: Path) -> None:
    if not _within_home(path):
        raise ValueError(f"Refusing to modify target outside --home ({HOME}): {path}")


def backup_path(src: Path, rel_name: str) -> str | None:
    """Copy src into this run's backup directory (once per run) and return the backup path, or None."""
    if not src.exists() and not src.is_symlink():
        return None
    dst = backup_root() / rel_name
    if DRY_RUN:
        return str(dst)
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir() and not is_reparse_point(src):
            shutil.copytree(src, dst)
        elif src.is_file():
            shutil.copy2(src, dst)
        else:
            return None
    return str(dst)


def write_text(path: Path, text: str) -> None:
    _require_within_home(path)
    if DRY_RUN:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def read_text(path: Path) -> str | None:
    try:
        return path.read_bytes().decode("utf-8-sig")
    except OSError:
        return None


def junction_target(path: Path) -> str | None:
    try:
        if is_reparse_point(path):
            return str(path.resolve())
    except OSError:
        pass
    return None


def remove_dir_or_junction(path: Path) -> None:
    _require_within_home(path)
    if DRY_RUN or (not path.exists() and not is_reparse_point(path)):
        return
    if is_reparse_point(path):
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "rmdir", str(path)], check=True, capture_output=True)
        else:
            path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def make_link(link: Path, target: Path) -> None:
    _require_within_home(link)
    link.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        res = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            encoding="mbcs",
            errors="replace",
        )
        if res.returncode != 0:
            raise RuntimeError(f"mklink /J failed for {link} -> {target}: {res.stderr or res.stdout}")
    else:
        os.symlink(target, link, target_is_directory=True)


def create_junction(link: Path, target: Path) -> None:
    """Point link at target, remembering what was there so uninstall can put it back."""
    _require_within_home(link)
    key = str(link)
    existing_target = junction_target(link)
    if existing_target is not None and Path(existing_target) == target.resolve():
        MANIFEST["junctions"].setdefault(key, {"target": str(target), "previous_target": None, "backup": None})
        return
    entry = MANIFEST["junctions"].get(key) or {"target": str(target), "previous_target": None, "backup": None}
    if key not in MANIFEST["junctions"]:
        if existing_target is not None:
            entry["previous_target"] = existing_target
        elif link.exists():
            entry["backup"] = backup_path(link, f"junction-replaced/{link.parent.name}-{link.name}")
    entry["target"] = str(target)
    MANIFEST["junctions"][key] = entry
    if DRY_RUN:
        return
    if existing_target is not None or link.exists():
        remove_dir_or_junction(link)
    make_link(link, target)


# --- managed blocks --------------------------------------------------------------------------------------------------


def make_block(content: str) -> str:
    return f"{MARKER_BEGIN}\n{content.rstrip(chr(10))}\n{MARKER_END}\n"


def _block_span(text: str) -> tuple[int, int] | None:
    start = text.find(MARKER_BEGIN)
    if start < 0:
        return None
    end = text.find(MARKER_END, start)
    if end < 0:
        raise ValueError("begin marker without a matching end marker")
    end += len(MARKER_END)
    if text[end : end + 2] == CRLF:
        end += 2
    elif text[end : end + 1] == LF:
        end += 1
    return start, end


def merge_block(existing: str | None, content: str, recorded_whole_sha: str | None = None) -> tuple[str, str]:
    """Return ``(new_text, mode)``; mode is ``whole`` (the file is the generated text) or ``block``.

    Raises ValueError when the file has an unbalanced begin marker (the caller leaves such a file alone).
    """
    if existing is None or not existing.strip():
        return make_block(content), "block"
    if existing.replace(CRLF, LF) == content or (recorded_whole_sha is not None and sha256(existing) == recorded_whole_sha):
        return content, "whole"
    span = _block_span(existing)
    if span is None and existing.lstrip().splitlines()[0] == content.splitlines()[0]:
        # An earlier generation of this very document, written before markers existed: replace it as a whole.
        return content, "whole"
    if span is None and host_rules.is_legacy_generated(existing.replace(CRLF, LF)):
        # A changed one-line daemon prompt has no stable first line. Migrate the
        # known generated text to one block instead of preserving a stale copy.
        return make_block(content), "block"
    block = make_block(content)
    if span is not None:
        return existing[: span[0]] + block + existing[span[1] :], "block"
    return existing.rstrip("\n") + "\n\n" + block, "block"


def strip_block(existing: str) -> str | None:
    """Remove the managed block (and the blank line that separated it); None when no block is present."""
    span = _block_span(existing)
    if span is None:
        return None
    before, after = existing[: span[0]], existing[span[1] :]
    if not after.strip():
        if before.endswith(CRLF + CRLF):
            before = before[:-2]
        elif before.endswith(LF + LF):
            before = before[:-1]
    return before + after


# --- unified discovery & global docs ---------------------------------------------------------------------------------


def paseo_config_path() -> Path:
    env = os.environ.get("PASEO_HOME")
    if env and HOME.resolve() == Path.home().resolve():
        return Path(env).expanduser() / "config.json"
    return HOME / ".paseo" / "config.json"


def orca_user_data_dirs(home: Path | None = None) -> list[Path]:
    """Re-export from `provider_inventory` so `onboard.py` and callers share one implementation."""
    return provider_inventory.orca_user_data_dirs(home or HOME)


def orca_codex_homes(home: Path | None = None) -> list[Path]:
    """Re-export from `provider_inventory` so `onboard.py` and callers share one implementation."""
    return provider_inventory.orca_codex_homes(home or HOME)


def _sync_environ(home: Path | None = None) -> dict:
    """Environment passed to `provider_inventory.discover()` when scanning `home`."""
    target = Path(home) if home is not None else HOME
    source = dict(os.environ)
    env = {"PATH": source.get("PATH", "")}
    for key in ("SYSTEMROOT", "PATHEXT", "TEMP", "TMP"):
        if source.get(key):
            env[key] = source[key]
    env.update(
        ROUTER_PROVIDER_HOME=str(target),
        ROUTER_STATE_DIR=str(target / ".agent-compass"),
        PASEO_HOME=str(target / ".paseo"),
        APPDATA=str(target / "AppData" / "Roaming"),
        LOCALAPPDATA=str(target / "AppData" / "Local"),
        USERPROFILE=str(target),
        HOME=str(target),
    )
    try:
        if target.resolve() == Path.home().resolve():
            for key in ("PASEO_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "PI_CODING_AGENT_DIR", "PRO_CONSULT_HOME"):
                if source.get(key):
                    env[key] = source[key]
    except OSError:
        pass
    return env


def _is_skill_hub_only(home_dir: Path, base: Path) -> bool:
    """True when `~/.codex` contains only the skill-hub junctions created by `link_repo_skills_into_hub`."""
    try:
        if home_dir.resolve() != (base / ".codex").resolve() or not home_dir.is_dir():
            return False
        if [p.name for p in home_dir.iterdir()] != ["skills"]:
            return False
        skills_dir = home_dir / "skills"
        if not skills_dir.is_dir() or is_reparse_point(skills_dir):
            return False
        children = list(skills_dir.iterdir())
        return bool(children) and all(p.name in SKILLS_FROM_REPO and is_reparse_point(p) for p in children)
    except OSError:
        return False


def account_home_eligible(row: dict, home: Path | None = None) -> bool:
    """Decide from a `provider_inventory.discover()` account row whether its home gets rules, skills and hooks.

    Rule (derived strictly from the account row, never from the account name):
    1. `row['family']` must be in `FAMILY_SPECS` and `row['home']` must be a non-empty directory path
       (virtual/remote accounts such as LiteLLM proxies, Dot, ChatGPT Web and Grok Bot carry no local harness home).
    2. When operating on a custom `--home`, `row['home']` must resolve inside that home.
    3. The home gets rules, skills and hooks if:
       - its directory already exists on disk (including disabled, paused, saved or signed-out homes that the user
         may launch by hand), OR
       - the account's discovery status is `ready`, OR
       - the harness CLI is installed (`row.get('cli_installed')`) and `row['home']` is the family's default native
         home (so a newly installed CLI gets its default home initialized before first sign-in, without creating
         non-existent directories for disabled/unconfigured secondary Paseo definitions).
    """
    if not isinstance(row, dict):
        return False
    base = Path(home) if home is not None else HOME
    family = row.get("family")
    spec = FAMILY_SPECS.get(family)
    raw_home = row.get("home")
    if not spec or not isinstance(raw_home, str) or not raw_home.strip():
        return False
    home_dir = Path(raw_home)
    if not _within_home(home_dir, base):
        return False
    presence_rel = spec.get("presence_dir")
    exists = (home_dir.is_dir() and not _is_skill_hub_only(home_dir, base)) or bool(
        presence_rel and (base / presence_rel).is_dir()
    )
    if exists or row.get("status") == "ready":
        return True
    if row.get("cli_installed"):
        default_dir = (base / spec["default_home"]).resolve()
        return home_dir.resolve() == default_dir
    return False


def discovered_homes(inventory: dict | None = None, home: Path | None = None) -> list[tuple[str, Path]]:
    """Ordered `(family, home_dir)` pairs discovered via `provider_inventory.discover()`."""
    base = Path(home) if home is not None else HOME
    inv = inventory if isinstance(inventory, dict) else provider_inventory.discover(home=base, environ=_sync_environ(base))
    accounts = inv.get("accounts") if isinstance(inv.get("accounts"), dict) else {}
    by_family: dict[str, list[Path]] = {fam: [] for fam in FAMILY_SPECS}
    seen: set[tuple[str, str]] = set()
    for row in accounts.values():
        if not account_home_eligible(row, base):
            continue
        family = row["family"]
        home_dir = Path(row["home"])
        key = (family, str(home_dir.resolve()))
        if key in seen:
            continue
        seen.add(key)
        by_family[family].append(home_dir)
    result: list[tuple[str, Path]] = []
    for family, spec in FAMILY_SPECS.items():
        default_res = (base / spec["default_home"]).resolve()
        homes = sorted(by_family[family], key=lambda p: (p.resolve() != default_res, str(p.resolve())))
        for h in homes:
            result.append((family, h))
    return result


def _home_tag(home_dir: Path) -> str:
    tag = home_dir.name.lstrip(".") or "home"
    if tag == "home" and home_dir.parent.name:
        return f"{home_dir.parent.name}-home"
    return tag


def doc_targets(homes: list[tuple[str, Path]] | None = None) -> list[tuple[Path, str, str]]:
    """(path, backup name, rule kind) for every global instruction file of a discovered harness home."""
    targets: list[tuple[Path, str, str]] = []
    for family, home_dir in (discovered_homes() if homes is None else homes):
        spec = FAMILY_SPECS[family]
        tag = _home_tag(home_dir)
        for rel_doc, backup_tpl in spec["docs"]:
            targets.append((home_dir / rel_doc, backup_tpl.format(home_tag=tag), spec["rule_kind"]))
    return targets


def unify_global_docs(homes: list[tuple[str, Path]] | None = None) -> None:
    targets = doc_targets(homes)
    active_keys: set[str] = set()
    for path, backup_name, kind in targets:
        content, source = host_rules.load(kind, HOME)
        key = str(path)
        active_keys.add(key)
        entry = MANIFEST["docs"].get(key)
        existing = read_text(path)
        try:
            new_text, mode = merge_block(existing, content, entry.get("sha256") if entry and entry.get("mode") == "whole" else None)
        except ValueError as exc:
            report(f"[docs] Skipped {path}: {exc}; fix or remove the marker by hand.")
            continue
        if entry is None:
            entry = {"created": existing is None, "backup": None}
        if existing == new_text:
            report(f"[docs] Up to date {path} ({mode}, {source} rules)")
        else:
            backup = backup_path(path, f"docs/{backup_name}")
            if entry.get("backup") is None and existing is not None and existing.strip():
                entry["backup"] = backup
            write_text(path, new_text)
            report(f"[docs] Updated {path} ({mode}, {source} rules)")
        entry.update(mode=mode, sha256=sha256(new_text))
        MANIFEST["docs"][key] = entry
    for stale_key in sorted(set(MANIFEST["docs"]) - active_keys):
        stale_path = Path(stale_key)
        if _within_home(stale_path):
            _uninstall_one_doc(stale_path, MANIFEST["docs"][stale_key])
        if not DRY_RUN:
            MANIFEST["docs"].pop(stale_key, None)


# --- Paseo daemon config ---------------------------------------------------------------------------------------------


def _clean_agy_path(raw_path: str) -> str:
    cleaned, seen = [], set()
    for entry in raw_path.split(";"):
        item = entry.strip()
        if not item:
            continue
        low = item.lower().rstrip("\\/")
        if "codex-arg0" in low or "grok-subagent" in low or low in seen:
            continue
        seen.add(low)
        cleaned.append(item)
    return ";".join(cleaned)


def reload_paseo_daemon(paseo_home: Path) -> None:
    """The daemon reads config.json once at start; `paseo reload` re-reads it without touching running agents."""
    if HOME.resolve() != Path.home().resolve():
        return      # a rehearsal home must never poke the real daemon
    local = os.environ.get("LOCALAPPDATA")
    cli = shutil.which("paseo") or next((str(c) for c in (Path(local) / "Programs" / "Paseo" / "resources" / "bin" / "paseo.cmd",) if local and c.is_file()), None)
    if not cli:
        return
    try:
        done = subprocess.run([cli, "reload", "--home", str(paseo_home)], capture_output=True, text=True, timeout=60)
        report(f"[paseo] reload: {(done.stdout or done.stderr).strip() or done.returncode}")
    except (OSError, subprocess.SubprocessError) as exc:
        report(f"[paseo] reload skipped ({exc}); run `paseo reload` to apply the new appendSystemPrompt")


def update_paseo_config() -> None:
    cfg_path = paseo_config_path()
    if not cfg_path.is_file():
        return
    data = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    daemon = data.setdefault("daemon", {})
    state = MANIFEST["paseo"]
    content, source = host_rules.load("daemon-append", HOME)
    previous = daemon.get("appendSystemPrompt")
    whole_sha = (state.get("append") or {}).get("sha256") if (state.get("append") or {}).get("mode") == "whole" else None
    try:
        new_prompt, mode = merge_block(previous if isinstance(previous, str) else None, content, whole_sha)
    except ValueError as exc:
        report(f"[paseo] Left appendSystemPrompt alone: {exc}")
        new_prompt, mode = previous, "skipped"
    changed = False
    if mode != "skipped":
        if "append" not in state:
            state["append"] = {"had_key": "appendSystemPrompt" in daemon, "previous": previous,
                               "leave": isinstance(previous, str) and previous == content}
        state["append"].update(mode=mode, sha256=sha256(new_prompt))
        if previous != new_prompt:
            daemon["appendSystemPrompt"] = new_prompt
            changed = True

    agy_env = data.get("agents", {}).get("providers", {}).get("antigravity", {}).get("env", {})
    raw_path = agy_env.get("PATH")
    if isinstance(raw_path, str) and raw_path:
        cleaned = _clean_agy_path(raw_path)
        if cleaned != raw_path:
            state.setdefault("agy_path", {"previous": raw_path, "cleaned": cleaned})
            agy_env["PATH"] = cleaned
            changed = True

    if changed:
        backup = backup_path(cfg_path, "configs/paseo-config.json")
        state.setdefault("config_backup", backup)
        state["config"] = str(cfg_path)
        if not DRY_RUN:
            cfg_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        report(f"[paseo] Updated {cfg_path} (appendSystemPrompt {mode}, {source} rules)")
        if not DRY_RUN:
            reload_paseo_daemon(cfg_path.parent)
    else:
        report(f"[paseo] Up to date {cfg_path}")
    state["config"] = str(cfg_path)


# --- skills ----------------------------------------------------------------------------------------------------------

SKILLS_TO_CODEX = ["agent-compass", "dot-subagent", "chatgpt-pro-consult", "gemini-web", "gate-design-reflection"]
SKILLS_TO_AGENTS = ["paseo", "paseo-advisor", "paseo-committee", "paseo-handoff", "paseo-help", "paseo-plugin"]


def _link_skill(dst: Path, src: Path, backup_name: str) -> None:
    create_junction(dst, src)
    report(f"[skills] Linked {dst} -> {src}")


def deduplicate_and_link_skills(homes: list[tuple[str, Path]] | None = None) -> None:
    active_homes = discovered_homes() if homes is None else homes
    codex_skills = HOME / ".codex" / "skills"
    agents_skills = HOME / ".agents" / "skills"
    claude_skills = HOME / ".claude" / "skills"

    # 1-2. Claude homes (primary links individual shared skills; secondary homes link skills -> ~/.claude/skills)
    for family, home_dir in active_homes:
        if family != "claude":
            continue
        spec = FAMILY_SPECS[family]
        skills_path = home_dir / spec["skills_dir"]
        if home_dir.resolve() == (HOME / spec["default_home"]).resolve():
            for name in SKILLS_TO_CODEX:
                if (codex_skills / name).is_dir():
                    _link_skill(skills_path / name, codex_skills / name, f"skills/claude-{name}")
            for name in SKILLS_TO_AGENTS:
                if (agents_skills / name).is_dir():
                    _link_skill(skills_path / name, agents_skills / name, f"skills/claude-{name}")
        elif claude_skills.is_dir():
            _link_skill(skills_path, claude_skills, "")
        else:
            tag = _home_tag(home_dir)
            for name in SKILLS_TO_CODEX:
                if (codex_skills / name).is_dir():
                    _link_skill(skills_path / name, codex_skills / name, f"skills/{tag}-{name}")
            for name in SKILLS_TO_AGENTS:
                if (agents_skills / name).is_dir():
                    _link_skill(skills_path / name, agents_skills / name, f"skills/{tag}-{name}")

    # 3. Secondary & Orca Codex homes get the router skill linked into <home>/skills/<name> (keeping their own skills dir)
    for family, home_dir in active_homes:
        if family != "codex":
            continue
        spec = FAMILY_SPECS[family]
        if home_dir.resolve() == (HOME / spec["default_home"]).resolve():
            continue
        skills_path = home_dir / spec["skills_dir"]
        if (skills_path.exists() or is_reparse_point(skills_path)) and skills_path.resolve() == codex_skills.resolve():
            continue
        for name in SKILLS_FROM_REPO:
            if (codex_skills / name).is_dir():
                _link_skill(skills_path / name, codex_skills / name, "")

    # 4. Pi: populate <pi_home>/skills with junctions to all shared skills
    for family, home_dir in active_homes:
        if family != "pi":
            continue
        spec = FAMILY_SPECS[family]
        pi_skills = home_dir / spec["skills_dir"]
        if not DRY_RUN:
            pi_skills.mkdir(parents=True, exist_ok=True)
        skill_sources: dict[str, Path] = {}
        for root in (codex_skills, agents_skills):
            if not root.is_dir():
                continue
            for item in sorted(root.iterdir()):
                if item.name.startswith(".") or not item.is_dir():
                    continue
                if (item / "SKILL.md").is_file():
                    skill_sources[item.name] = item
        if claude_skills.is_dir():
            for item in sorted(claude_skills.iterdir()):
                if item.name.startswith(".") or not item.is_dir() or is_reparse_point(item):
                    continue
                if (item / "SKILL.md").is_file() and item.name not in skill_sources:
                    skill_sources[item.name] = item
        for name, target in sorted(skill_sources.items()):
            create_junction(pi_skills / name, target)
        report(f"[skills] Populated {pi_skills} with {len(skill_sources)} skill junctions")

    # 5. Antigravity: skills.json plus real top-level skill directories (its scanner skips junction roots)
    for family, home_dir in active_homes:
        if family != "antigravity":
            continue
        _sync_gemini_skills(codex_skills, agents_skills, claude_skills, gemini_home=home_dir)

    # Retire skill links/files belonging to homes that are no longer discovered
    _retire_stale_skill_links(active_homes)


def _materialized_children(path: Path) -> dict[str, Path]:
    return {p.name: p for p in path.iterdir() if not p.name.startswith(".") and p.name != "__pycache__"}


def materialized_skill_matches(dst: Path, src: Path) -> bool:
    """Gemini needs a real root, byte-identical top-level files and live directory links."""
    if not dst.is_dir() or is_reparse_point(dst):
        return False
    expected, actual = _materialized_children(src), _materialized_children(dst)
    if expected.keys() != actual.keys():
        return False
    for name, child in expected.items():
        target = actual[name]
        if child.is_dir():
            linked = junction_target(target)
            if linked is None or Path(linked).resolve() != child.resolve():
                return False
        elif child.is_file():
            if not target.is_file() or is_reparse_point(target) or not filecmp.cmp(child, target, shallow=False):
                return False
    return True


def backup_materialized_skill(src: Path, rel_name: str) -> str | None:
    """Keep live directory links as links; never copy the repositories behind them."""
    if not src.is_dir() or is_reparse_point(src):
        return backup_path(src, rel_name)
    dst = backup_root() / rel_name
    if not DRY_RUN and not dst.exists():
        dst.mkdir(parents=True)
        for child in src.iterdir():
            target = junction_target(child)
            if target is not None and child.is_dir():
                make_link(dst / child.name, Path(target))
            elif child.is_dir():
                shutil.copytree(child, dst / child.name)
            elif child.is_file():
                shutil.copy2(child, dst / child.name)
    return str(dst)


def _sync_gemini_skills(codex_skills: Path, agents_skills: Path, claude_skills: Path,
                        gemini_home: Path | None = None) -> None:
    gemini_root = gemini_home or (HOME / ".gemini")
    gemini_skills_json = gemini_root / "config" / "skills.json"
    gemini_skills_cfg = {
        "entries": [
            {"path": (HOME / ".codex" / "skills").as_posix(), "exclude": ["\\..*"]},
            {"path": (HOME / ".agents" / "skills").as_posix(), "exclude": ["\\..*"]},
            {"path": (HOME / ".claude" / "skills").as_posix(), "exclude": ["\\..*"]},
        ]
    }
    text = json.dumps(gemini_skills_cfg, indent=2, ensure_ascii=False) + "\n"
    existing = read_text(gemini_skills_json)
    if not same_config(existing, text):
        entry = MANIFEST["files"].get(str(gemini_skills_json)) or {"created": existing is None, "backup": None}
        backup = backup_path(gemini_skills_json, "configs/gemini-skills.json")
        if entry.get("backup") is None and existing is not None:
            entry["backup"] = backup
        MANIFEST["files"][str(gemini_skills_json)] = entry
        write_text(gemini_skills_json, text)
        report(f"[skills] Wrote {gemini_skills_json}")

    gemini_skills_dir = gemini_root / "skills"
    if not DRY_RUN:
        gemini_skills_dir.mkdir(parents=True, exist_ok=True)
    for name in SKILLS_TO_CODEX:
        src = codex_skills / name
        if not src.is_dir() or not is_reparse_point(src):
            continue
        real_src = src.resolve()
        dst = gemini_skills_dir / name
        if materialized_skill_matches(dst, real_src):
            report(f"[skills] Up to date {dst}")
            continue
        if dst.exists() or is_reparse_point(dst):
            backup = backup_materialized_skill(dst, f"skills/gemini-{name}")
            if str(dst) not in MANIFEST["materialized"]:
                # Not ours: keep the original before replacing it.
                MANIFEST["materialized"][str(dst)] = {"backup": backup}
            remove_dir_or_junction(dst)
        else:
            MANIFEST["materialized"].setdefault(str(dst), {"backup": None})
        report(f"[skills] Materialized {dst} -> {real_src} (real dir + live subdir junctions for Antigravity)")
        if DRY_RUN:
            continue
        dst.mkdir(parents=True, exist_ok=True)
        for child in sorted(real_src.iterdir()):
            if child.name.startswith(".") or child.name == "__pycache__":
                continue
            dst_child = dst / child.name
            if child.is_dir():
                make_link(dst_child, child)
            elif child.is_file():
                shutil.copy2(child, dst_child)


REPO_SKILLS = Path(__file__).resolve().parents[1] / "skills"
SKILLS_FROM_REPO = ["agent-compass"]


def link_repo_skills_into_hub() -> None:
    """New-user bootstrap: expose this checkout's router skill as ~/.codex/skills/<name>, the hub the other hosts link to.

    Refresh links still matching our manifest; preserve user-owned or retargeted paths.
    """
    hub = HOME / ".codex" / "skills"
    for name in SKILLS_FROM_REPO:
        source = REPO_SKILLS / name
        link = hub / name
        if not (source / "SKILL.md").is_file():
            continue
        if link.exists() or is_reparse_point(link):
            recorded = MANIFEST["junctions"].get(str(link), {}).get("target")
            current = junction_target(link)
            if not recorded or current is None or Path(current) != Path(recorded).resolve():
                report(f"[skills] {link} is not a matching managed link; left as is")
                continue
        create_junction(link, source)
        report(f"[skills] Linked {link} -> {source}")


def _retire_stale_skill_links(active_homes: list[tuple[str, Path]]) -> None:
    """Clean up manifest skill entries belonging to homes that are no longer discovered."""
    active_roots = {h.resolve() for _, h in active_homes} | {(HOME / ".codex" / "skills").resolve()}

    def _under_active(p: Path) -> bool:
        try:
            parent_res = p.parent.resolve()
            return any(r == parent_res or r in parent_res.parents for r in active_roots)
        except OSError:
            return False

    for key in sorted(list(MANIFEST["materialized"])):
        path = Path(key)
        if _under_active(path):
            continue
        if _within_home(path):
            _uninstall_one_materialized(path, MANIFEST["materialized"][key])
        if not DRY_RUN:
            MANIFEST["materialized"].pop(key, None)

    for key in sorted(list(MANIFEST["junctions"])):
        link = Path(key)
        if _under_active(link):
            continue
        if _within_home(link):
            _uninstall_one_junction(link, MANIFEST["junctions"][key])
        if not DRY_RUN:
            MANIFEST["junctions"].pop(key, None)

    for key in sorted(list(MANIFEST["files"])):
        path = Path(key)
        if _under_active(path):
            continue
        if _within_home(path):
            entry = MANIFEST["files"][key]
            if path.exists() or entry.get("backup"):
                report(f"[uninstall] {path}: {_restore_file(path, entry.get('backup'), bool(entry.get('created')))}")
        if not DRY_RUN:
            MANIFEST["files"].pop(key, None)


def clean_dead_files() -> None:
    """Maintenance for long-running installs; removes only empty leftover directories and stale Codex temp files."""
    for empty_dir in (
        HOME / ".codex" / "skills" / ".git",
        HOME / ".codex" / "skills" / ".agents",
        HOME / ".codex" / ".agents",
    ):
        if empty_dir.is_dir() and not any(empty_dir.iterdir()):
            if not DRY_RUN:
                empty_dir.rmdir()
            report(f"[clean] Removed empty directory {empty_dir}")

    codex_dir = HOME / ".codex"
    removed_tmp = 0
    for tmp_file in codex_dir.glob("..codex-global-state.json*.tmp-*"):
        if tmp_file.is_file():
            backup_path(tmp_file, f"codex-tmp/{tmp_file.name}")
            if not DRY_RUN:
                tmp_file.unlink()
            removed_tmp += 1
    if removed_tmp:
        report(f"[clean] Removed {removed_tmp} stale ..codex-global-state.json*.tmp-* files")


# --- router gate hooks -----------------------------------------------------------------------------------------------


def ensure_hook_in_list(hook_entries: list, command_str: str, matcher: str | None = ROUTER_GATE_MATCHER) -> list:
    """Ensure a command hook entry with command_str exists in hook_entries (updating its matcher) without duplicating."""
    filtered = []
    for group in hook_entries:
        other_hooks = [
            h for h in group.get("hooks", [])
            if h.get("command") != command_str and "router_gate_hook.py" not in str(h.get("command", ""))
        ]
        if other_hooks:
            new_group = dict(group)
            new_group["hooks"] = other_hooks
            filtered.append(new_group)
    entry: dict = {"hooks": [{"type": "command", "command": command_str, "timeout": 10}]}
    if matcher is not None:
        entry["matcher"] = matcher
    # Put router gate first so blocked tool calls don't even proceed
    return [entry] + filtered


def remove_hook_from_list(hook_entries: list) -> list:
    result = []
    for group in hook_entries:
        others = [h for h in group.get("hooks", []) if "router_gate_hook.py" not in str(h.get("command", ""))]
        if others:
            new_group = dict(group)
            new_group["hooks"] = others
            result.append(new_group)
    return result


def _read_json_file(path: Path) -> dict:
    text = read_text(path)
    if text is None or not text.strip():
        return {}
    data = json.loads(text)
    return data if isinstance(data, dict) else {}


def _json_text(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def same_config(existing: str | None, new_text: str) -> bool:
    """Equal up to line endings, or equal as JSON (a rewrite that changes nothing is not a change)."""
    if existing is None:
        return False
    if existing.replace(CRLF, LF) == new_text:
        return True
    try:
        return json.loads(existing) == json.loads(new_text)
    except ValueError:
        return False


def _write_hook_file(path: Path, new_text: str, backup_name: str, kind: str) -> None:
    existing = read_text(path)
    entry = MANIFEST["hooks"].get(str(path)) or {"kind": kind, "created": existing is None, "backup": None}
    if not same_config(existing, new_text):
        backup = backup_path(path, f"configs/{backup_name}")
        if entry.get("backup") is None and existing is not None:
            entry["backup"] = backup
        write_text(path, new_text)
        report(f"[hooks] Updated {path}")
    MANIFEST["hooks"][str(path)] = entry


def hook_targets(homes: list[tuple[str, Path]] | None = None) -> list[tuple[str, Path, Path, str, str]]:
    """(family, home_dir, hook_path, backup_name, hook_kind) for every discovered home that uses a router gate hook."""
    targets: list[tuple[str, Path, Path, str, str]] = []
    for family, home_dir in (discovered_homes() if homes is None else homes):
        spec = FAMILY_SPECS[family]
        hook = spec.get("hook")
        if not hook:
            continue
        rel_path, backup_tpl, hook_kind = hook
        tag = _home_tag(home_dir)
        targets.append((family, home_dir, home_dir / rel_path, backup_tpl.format(home_tag=tag), hook_kind))
    return targets


def hooks_installed(homes: list[tuple[str, Path]] | None = None) -> bool:
    """True when any discovered harness home already routes tool calls through the router gate."""
    for _, _, path, _, _ in hook_targets(homes):
        text = read_text(path)
        if text and "router_gate_hook.py" in text:
            return True
    return False


def configure_hooks(homes: list[tuple[str, Path]] | None = None) -> None:
    targets = hook_targets(homes)
    active_keys: set[str] = set()
    claude_base_text: str | None = None
    codex_base_text: str | None = None

    for family, home_dir, hook_path, backup_name, hook_kind in targets:
        active_keys.add(str(hook_path))
        spec = FAMILY_SPECS[family]
        is_default_home = home_dir.resolve() == (HOME / spec["default_home"]).resolve()

        if hook_kind == "claude":
            if is_default_home or claude_base_text is None:
                claude_settings = _read_json_file(hook_path)
                hooks = claude_settings.setdefault("hooks", {})
                hooks["PreToolUse"] = ensure_hook_in_list(hooks.get("PreToolUse", []), ROUTER_HOOK_CMD, matcher=ROUTER_GATE_MATCHER)
                text = _json_text(claude_settings)
                if is_default_home:
                    claude_base_text = text
                _write_hook_file(hook_path, text, backup_name, "claude")
            else:
                existing_alt = _read_json_file(hook_path)
                alt_settings = json.loads(claude_base_text)
                alt_env = dict(alt_settings.get("env") or {})
                for k, v in (existing_alt.get("env") or {}).items():
                    if v is not None and v != "":
                        alt_env[k] = v
                if alt_env or "env" in alt_settings or "env" in existing_alt:
                    alt_settings["env"] = alt_env
                _write_hook_file(hook_path, _json_text(alt_settings), backup_name, "claude")

        elif hook_kind == "codex":
            if is_default_home or hook_path.is_file() or codex_base_text is None:
                codex_data = _read_json_file(hook_path)
                chooks = codex_data.setdefault("hooks", {})
                chooks["PreToolUse"] = ensure_hook_in_list(chooks.get("PreToolUse", []), ROUTER_HOOK_CMD, matcher=None)
                text = _json_text(codex_data)
                if is_default_home:
                    codex_base_text = text
                _write_hook_file(hook_path, text, backup_name, "codex")
            else:
                _write_hook_file(hook_path, codex_base_text, backup_name, "codex")

        elif hook_kind == "gemini":
            gemini_data = _read_json_file(hook_path)
            new_gemini_data = {
                "router-gate": {
                    "PreToolUse": [
                        {"matcher": ROUTER_GATE_MATCHER, "hooks": [{"type": "command", "command": ROUTER_HOOK_CMD, "timeout": 10}]}
                    ]
                }
            }
            for k, v in gemini_data.items():
                if k != "router-gate":
                    new_gemini_data[k] = v
            _write_hook_file(hook_path, _json_text(new_gemini_data), backup_name, "gemini")

        elif hook_kind == "pi":
            _write_hook_file(hook_path, PI_ROUTER_GATE_TS, backup_name, "pi")

    for stale_key in sorted(set(MANIFEST["hooks"]) - active_keys):
        stale_path = Path(stale_key)
        if _within_home(stale_path):
            _uninstall_one_hook(stale_path, MANIFEST["hooks"][stale_key])
        if not DRY_RUN:
            MANIFEST["hooks"].pop(stale_key, None)


# --- uninstall -------------------------------------------------------------------------------------------------------


def _restore_file(path: Path, backup: str | None, created: bool) -> str:
    if backup and Path(backup).exists():
        if not DRY_RUN:
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, path)
        return f"restored from {backup}"
    if created:
        if not DRY_RUN and path.exists():
            path.unlink()
        return "removed (created by setup)"
    return "left in place (no backup; it already matched)"


def _uninstall_one_doc(path: Path, entry: dict) -> None:
    existing = read_text(path)
    if existing is None:
        return
    if entry.get("mode") == "whole":
        if sha256(existing) != entry.get("sha256"):
            report(f"[uninstall] {path}: edited since setup; left untouched")
        else:
            report(f"[uninstall] {path}: {_restore_file(path, entry.get('backup'), bool(entry.get('created')))}")
        return
    try:
        stripped = strip_block(existing)
    except ValueError:
        stripped = None
    if stripped is None:
        report(f"[uninstall] {path}: no managed block found")
        return
    if not stripped.strip():
        if not DRY_RUN:
            path.unlink()
        report(f"[uninstall] {path}: removed (only the managed block was left)")
    else:
        write_text(path, stripped)
        report(f"[uninstall] {path}: managed block removed")


def _uninstall_docs() -> None:
    for key, entry in sorted(MANIFEST["docs"].items()):
        _uninstall_one_doc(Path(key), entry)


def _uninstall_one_hook(path: Path, entry: dict) -> None:
    kind = entry.get("kind")
    existing = read_text(path)
    if existing is None:
        return
    if kind == "pi":
        if "router_gate_hook.py" not in existing:
            return
        report(f"[uninstall] {path}: {_restore_file(path, entry.get('backup'), bool(entry.get('created')))}")
        return
    try:
        data = json.loads(existing)
    except ValueError:
        report(f"[uninstall] {path}: not valid JSON; left untouched")
        return
    if kind == "gemini":
        data.pop("router-gate", None)
    else:
        hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
        if "PreToolUse" in hooks:
            remaining = remove_hook_from_list(hooks["PreToolUse"])
            if remaining:
                hooks["PreToolUse"] = remaining
            else:
                hooks.pop("PreToolUse")
        if not hooks:
            data.pop("hooks", None)
    backup = entry.get("backup")
    original = read_text(Path(backup)) if backup else None
    if not data and entry.get("created"):
        if not DRY_RUN:
            path.unlink()
        report(f"[uninstall] {path}: removed (created by setup)")
    elif original is not None and same_config(original, _json_text(data)):
        if not DRY_RUN:
            shutil.copy2(backup, path)       # back to the exact bytes the user had
        report(f"[uninstall] {path}: router gate hook entry removed (original file restored)")
    else:
        write_text(path, _json_text(data))
        report(f"[uninstall] {path}: router gate hook entry removed")


def _uninstall_hooks() -> None:
    for key, entry in sorted(MANIFEST["hooks"].items()):
        _uninstall_one_hook(Path(key), entry)


def _uninstall_paseo() -> None:
    state = MANIFEST.get("paseo") or {}
    cfg_path = Path(state["config"]) if state.get("config") else paseo_config_path()
    if not cfg_path.is_file() or not state:
        return
    data = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    daemon = data.setdefault("daemon", {})
    changed = False
    append = state.get("append")
    if append and isinstance(daemon.get("appendSystemPrompt"), str):
        current = daemon["appendSystemPrompt"]
        if append.get("mode") == "whole":
            if sha256(current) != append.get("sha256"):
                report("[uninstall] Paseo appendSystemPrompt edited since setup; left untouched")
            elif append.get("leave"):
                report("[uninstall] Paseo appendSystemPrompt left in place (it already matched before setup)")
            elif append.get("had_key"):
                daemon["appendSystemPrompt"] = append.get("previous")
                changed = True
            else:
                daemon.pop("appendSystemPrompt")
                changed = True
        else:
            stripped = strip_block(current)
            if stripped is not None:
                if stripped.strip():
                    original_prompt = append.get("previous")
                    # The separator added around the block is not information: give back the exact original text.
                    same = isinstance(original_prompt, str) and original_prompt.strip() == stripped.strip()
                    daemon["appendSystemPrompt"] = original_prompt if same else stripped
                elif append.get("had_key"):
                    daemon["appendSystemPrompt"] = append.get("previous") or ""
                else:
                    daemon.pop("appendSystemPrompt")
                changed = True
    agy = state.get("agy_path")
    agy_env = data.get("agents", {}).get("providers", {}).get("antigravity", {}).get("env", {})
    if agy and agy_env.get("PATH") == agy.get("cleaned"):
        agy_env["PATH"] = agy["previous"]
        changed = True
    if changed:
        text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        backup = state.get("config_backup")
        original = read_text(Path(backup)) if backup else None
        if not DRY_RUN:
            if original is not None and same_config(original, text):
                shutil.copy2(backup, cfg_path)        # back to the exact bytes the user had
            else:
                cfg_path.write_text(text, encoding="utf-8", newline="\n")
        report(f"[uninstall] {cfg_path}: Paseo changes reverted")


def _uninstall_one_materialized(path: Path, entry: dict) -> None:
    if path.exists() or is_reparse_point(path):
        for child in sorted(path.iterdir()) if path.is_dir() else []:
            if is_reparse_point(child):
                remove_dir_or_junction(child)
        remove_dir_or_junction(path)
    if entry.get("backup") and Path(entry["backup"]).exists() and not DRY_RUN:
        shutil.copytree(entry["backup"], path)
    report(f"[uninstall] {path}: materialized skill directory removed")


def _uninstall_one_junction(link: Path, entry: dict) -> None:
    if is_reparse_point(link):
        remove_dir_or_junction(link)
    elif link.exists():
        report(f"[uninstall] {link}: no longer a junction; left untouched")
        return
    if entry.get("backup") and Path(entry["backup"]).exists():
        if not DRY_RUN:
            shutil.copytree(entry["backup"], link)
        report(f"[uninstall] {link}: junction removed, original directory restored")
    elif entry.get("previous_target"):
        if not DRY_RUN:
            make_link(link, Path(entry["previous_target"]))
        report(f"[uninstall] {link}: junction restored to {entry['previous_target']}")
    else:
        report(f"[uninstall] {link}: junction removed")


def _uninstall_links() -> None:
    for key, entry in sorted(MANIFEST["materialized"].items()):
        _uninstall_one_materialized(Path(key), entry)
    for key, entry in sorted(MANIFEST["junctions"].items()):
        _uninstall_one_junction(Path(key), entry)
    for key in sorted(MANIFEST["junctions"]):
        skills_dir = Path(key).parent
        try:
            if not DRY_RUN and skills_dir.name == "skills" and skills_dir.is_dir() and not is_reparse_point(skills_dir) and not any(skills_dir.iterdir()):
                skills_dir.rmdir()
        except OSError:
            pass
    for key, entry in sorted(MANIFEST["files"].items()):
        path = Path(key)
        if path.exists() or entry.get("backup"):
            report(f"[uninstall] {path}: {_restore_file(path, entry.get('backup'), bool(entry.get('created')))}")
    for leftover in (HOME / ".claude" / "skills", HOME / ".codex" / "skills", HOME / ".gemini" / "skills",
                     HOME / ".gemini" / "config", HOME / ".pi" / "agent" / "skills", HOME / ".pi" / "agent" / "extensions",
                     HOME / ".gemini", HOME / ".pi" / "agent", HOME / ".pi", HOME / ".config" / "opencode", HOME / ".config"):
        try:
            if not DRY_RUN and leftover.is_dir() and not is_reparse_point(leftover) and not any(leftover.iterdir()):
                leftover.rmdir()
        except OSError:
            pass


def uninstall(dry_run: bool = False) -> int:
    """Undo everything recorded in the setup manifest. Backups stay on disk."""
    begin_run(dry_run)
    if not MANIFEST.get("runs"):
        print(f"Nothing to uninstall: no setup manifest at {manifest_path()}")
        return 0
    _uninstall_docs()
    _uninstall_hooks()
    _uninstall_paseo()
    _uninstall_links()
    if not dry_run:
        manifest_path().unlink(missing_ok=True)
    print("Uninstall complete. Backups are kept under " + str(state_dir() / "backups") + ".")
    return 0


# --- entry points ----------------------------------------------------------------------------------------------------


def ensure_paseo_bridge_dependencies() -> None:
    """Prepare the optional Paseo dispatcher without requiring Node for other hosts."""
    if not paseo_config_path().is_file() and not shutil.which("paseo"):
        return
    bridge = Path(__file__).resolve().parent / "paseo-bridge"
    if (bridge / "node_modules").is_dir():
        return
    hint = "Install Node.js with npm, then run npm --prefix setup/paseo-bridge ci from the repository root (only needed for Paseo worker dispatch via roll)."
    npm = shutil.which("npm")
    if not npm:
        print(f"[paseo-bridge] {hint} Continuing other host setup.")
        return
    if DRY_RUN:
        print("[paseo-bridge] Would install optional Paseo worker dispatch dependencies.")
        return
    print("[paseo-bridge] Installing optional Paseo worker dispatch dependencies...")
    subprocess.run([npm, "ci"], cwd=bridge, check=True)


def is_existing_install(homes: list[tuple[str, Path]] | None = None) -> bool:
    return hooks_installed(homes) or host_rules.overlay_dir(HOME).is_dir()


def sync(*, docs: bool = True, skills: bool = True, hooks: bool | None = None, dry_run: bool = False,
         clean: bool | None = None, link_repo_skills: bool = False) -> list[str]:
    """Run the host synchronisation and return the report lines.

    hooks=None keeps hooks that are already installed up to date and installs none for a new user.
    """
    begin_run(dry_run)
    homes = discovered_homes()
    install_hooks = hooks_installed(homes) if hooks is None else hooks
    existing = is_existing_install(homes)
    ensure_paseo_bridge_dependencies()
    if not dry_run:
        print(f"Backups (only if something changes): {backup_root()}")
    if docs:
        unify_global_docs(homes)
        update_paseo_config()
    if skills:
        if link_repo_skills or str(HOME / ".codex/skills/agent-compass") in MANIFEST["junctions"]:
            link_repo_skills_into_hub()
        deduplicate_and_link_skills(homes)
    if clean if clean is not None else existing:
        clean_dead_files()
    if install_hooks:
        configure_hooks(homes)
    else:
        report("[hooks] Router gate hook not installed (pass --hooks to install it; it blocks sub-agent launches without a recent router record).")
    if not dry_run:
        MANIFEST["runs"].append({"id": RUN_ID, "backup_dir": str(backup_root()), "hooks": install_hooks})
        MANIFEST["runs"] = MANIFEST["runs"][-20:]
        save_manifest()
    return list(ACTIONS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hooks", dest="hooks", action="store_true", default=None, help="Install the router gate hook (blocks sub-agent launches without a recent router record).")
    parser.add_argument("--no-hooks", dest="hooks", action="store_false", help="Do not install or refresh the router gate hook.")
    parser.add_argument("--skip-docs", action="store_true", help="Leave global instruction files and the Paseo prompt alone.")
    parser.add_argument("--skip-skills", action="store_true", help="Leave skill junctions alone.")
    parser.add_argument("--dry-run", action="store_true", help="Report what would change without touching anything.")
    parser.add_argument("--uninstall", action="store_true", help="Undo everything this setup recorded in its manifest.")
    parser.add_argument("--home", help="Operate on another home directory (testing).")
    args = parser.parse_args(argv)
    if args.home:
        set_home(args.home)
    if args.uninstall:
        return uninstall(args.dry_run)
    sync(docs=not args.skip_docs, skills=not args.skip_skills, hooks=args.hooks, dry_run=args.dry_run)
    print("Host globals synchronized." if not args.dry_run else "Dry run complete; nothing was changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
