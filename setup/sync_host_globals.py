"""Unify global agent instructions, link shared skills and (optionally) install the router gate across local harnesses.

What it touches, and how it stays reversible:

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
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import host_rules  # noqa: E402

CRLF, LF = "\r\n", "\n"
MARKER_BEGIN = "<!-- agentcompass:begin -->"
MARKER_END = "<!-- agentcompass:end -->"

PYTHON_EXE = Path(sys.executable).resolve().as_posix()
ROUTER_HOOK_SCRIPT = Path(__file__).resolve().with_name("router_gate_hook.py").as_posix()
ROUTER_HOOK_CMD = f"{PYTHON_EXE} -X utf8 {ROUTER_HOOK_SCRIPT}"

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


def host_present(*names: str, dirs: tuple[Path, ...] = ()) -> bool:
    """A harness counts as installed when its config directory exists or its CLI is on PATH."""
    return any(d.exists() for d in dirs) or any(shutil.which(n) for n in names)


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


# --- global docs -----------------------------------------------------------------------------------------------------


def paseo_config_path() -> Path:
    env = os.environ.get("PASEO_HOME")
    if env and HOME.resolve() == Path.home().resolve():
        return Path(env).expanduser() / "config.json"
    return HOME / ".paseo" / "config.json"


def _paseo_providers() -> dict:
    try:
        cfg = json.loads(paseo_config_path().read_text(encoding="utf-8-sig"))
        providers = (cfg.get("agents") or {}).get("providers") or {}
        return providers if isinstance(providers, dict) else {}
    except (OSError, ValueError, AttributeError):
        return {}


def _secondary_claude_dirs() -> list[Path]:
    found: dict[str, Path] = {}
    primary = (HOME / ".claude").resolve()
    for p in sorted(HOME.glob(".claude-*")):
        if p.is_dir() and p.resolve() != primary:
            found[str(p.resolve())] = p
    for name, row in _paseo_providers().items():
        if not isinstance(row, dict) or not (name == "claude" or name.startswith("claude-") or row.get("extends") == "claude"):
            continue
        cfg_dir = (row.get("env") or {}).get("CLAUDE_CONFIG_DIR")
        if isinstance(cfg_dir, str) and cfg_dir.strip():
            p = Path(cfg_dir.strip()).expanduser()
            if p.is_dir() and p.resolve() != primary:
                found[str(p.resolve())] = p
    return list(found.values())


def orca_user_data_dirs(home: Path | None = None) -> list[Path]:
    """Orca's Electron userData directories under `home` (Windows roaming, macOS Application Support, Linux ~/.config).

    ORCA_USER_DATA_PATH is deliberately not read: an inherited value would point a test or sandbox home at the real install.
    """
    home = home or HOME
    found: dict[str, Path] = {}
    for p in (home / "AppData" / "Roaming" / "orca", home / "Library" / "Application Support" / "orca", home / ".config" / "orca"):
        if p.is_dir():
            found.setdefault(str(p.resolve()), p)
    return list(found.values())


def orca_codex_homes(home: Path | None = None) -> list[Path]:
    """Codex homes Orca manages per signed-in account. Orca mirrors only config.toml into them, so AGENTS.md and skills never arrive."""
    return [h for ud in orca_user_data_dirs(home) for h in sorted((ud / "codex-accounts").glob("*/home")) if h.is_dir()]


def _secondary_codex_dirs() -> list[Path]:
    found: dict[str, Path] = {}
    primary = (HOME / ".codex").resolve()
    for p in sorted(HOME.glob(".codex-*")):
        if not p.is_dir() or p.resolve() == primary:
            continue
        if p.name.startswith((".codex-chatgpt-web", ".codex-pro-consult")):
            continue
        found[str(p.resolve())] = p
    for name, row in _paseo_providers().items():
        if not isinstance(row, dict) or not (name == "codex" or name.startswith("codex-") or row.get("extends") == "codex"):
            continue
        cfg_dir = (row.get("env") or {}).get("CODEX_HOME")
        if isinstance(cfg_dir, str) and cfg_dir.strip():
            p = Path(cfg_dir.strip()).expanduser()
            if p.is_dir() and p.resolve() != primary and not p.name.startswith((".codex-chatgpt-web", ".codex-pro-consult")):
                found[str(p.resolve())] = p
    for p in orca_codex_homes():
        if p.resolve() != primary:
            found[str(p.resolve())] = p
    return list(found.values())


def doc_targets() -> list[tuple[Path, str, str]]:
    """(path, backup name, rule kind) for every global instruction file of a harness that is present."""
    targets: list[tuple[Path, str, str]] = []
    if host_present("codex", dirs=(HOME / ".codex",)):
        targets.append((HOME / ".codex" / "AGENTS.md", "codex-AGENTS.md", "codex"))
    targets += [(d / "AGENTS.md", f"{d.name.lstrip('.')}-AGENTS.md", "codex") for d in _secondary_codex_dirs()]
    if host_present("claude", dirs=(HOME / ".claude",)):
        targets.append((HOME / ".claude" / "CLAUDE.md", "claude-CLAUDE.md", "shared"))
    targets += [(d / "CLAUDE.md", f"{d.name.lstrip('.')}-CLAUDE.md", "shared") for d in _secondary_claude_dirs()]
    if host_present("pi", dirs=(HOME / ".pi",)):
        targets.append((HOME / ".pi" / "agent" / "AGENTS.md", "pi-agent-AGENTS.md", "shared"))
    if host_present("agy", "gemini", dirs=(HOME / ".gemini",)):
        targets.append((HOME / ".gemini" / "GEMINI.md", "gemini-GEMINI.md", "shared"))
        targets.append((HOME / ".gemini" / "config" / "GEMINI.md", "gemini-config-GEMINI.md", "shared"))
    if host_present("opencode", dirs=(HOME / ".config" / "opencode",)):
        targets.append((HOME / ".config" / "opencode" / "AGENTS.md", "opencode-AGENTS.md", "shared"))
    return targets


def unify_global_docs() -> None:
    for path, backup_name, kind in doc_targets():
        content, source = host_rules.load(kind, HOME)
        key = str(path)
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


def deduplicate_and_link_skills() -> None:
    codex_skills = HOME / ".codex" / "skills"
    agents_skills = HOME / ".agents" / "skills"
    claude_skills = HOME / ".claude" / "skills"

    claude_present = host_present("claude", dirs=(HOME / ".claude",))

    # 1. Replace duplicate/pointer skills in ~/.claude/skills with junctions to ~/.codex/skills
    for name in SKILLS_TO_CODEX:
        if claude_present and (codex_skills / name).is_dir():
            _link_skill(claude_skills / name, codex_skills / name, f"skills/claude-{name}")

    # 2. Replace duplicate paseo* skills in ~/.claude/skills with junctions to ~/.agents/skills
    for name in SKILLS_TO_AGENTS:
        if claude_present and (agents_skills / name).is_dir():
            _link_skill(claude_skills / name, agents_skills / name, f"skills/claude-{name}")

    # 3. Link <secondary-claude>/skills -> ~/.claude/skills for all discovered secondary Claude homes
    if claude_skills.is_dir():
        for alt_dir in _secondary_claude_dirs():
            _link_skill(alt_dir / "skills", claude_skills, "")

    # 3b. Orca's managed Codex account homes get the router skill only; their own skills directory is kept.
    for codex_home in orca_codex_homes():
        for name in SKILLS_FROM_REPO:
            if (codex_skills / name).is_dir():
                _link_skill(codex_home / "skills" / name, codex_skills / name, "")

    # 4. Populate ~/.pi/agent/skills with junctions to all shared skills (only when Pi is installed)
    if host_present("pi", dirs=(HOME / ".pi",)):
        pi_skills = HOME / ".pi" / "agent" / "skills"
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

    # 5/6. Antigravity: skills.json plus real top-level skill directories (its scanner skips junction roots)
    if host_present("agy", "gemini", dirs=(HOME / ".gemini",)):
        _sync_gemini_skills(codex_skills, agents_skills, claude_skills)


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


def _sync_gemini_skills(codex_skills: Path, agents_skills: Path, claude_skills: Path) -> None:
    gemini_skills_json = HOME / ".gemini" / "config" / "skills.json"
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

    gemini_skills_dir = HOME / ".gemini" / "skills"
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


def hooks_installed() -> bool:
    """True when any harness already routes tool calls through the router gate."""
    candidates = [HOME / ".claude" / "settings.json", HOME / ".codex" / "hooks.json", HOME / ".gemini" / "config" / "hooks.json",
                  HOME / ".pi" / "agent" / "extensions" / "router-gate.ts"]
    candidates += [d / "settings.json" for d in _secondary_claude_dirs()] + [d / "hooks.json" for d in _secondary_codex_dirs()]
    for path in candidates:
        text = read_text(path)
        if text and "router_gate_hook.py" in text:
            return True
    return False


def configure_hooks() -> None:
    # 1. Claude (& secondary Claude homes) settings.json
    if host_present("claude", dirs=(HOME / ".claude",)):
        claude_settings_path = HOME / ".claude" / "settings.json"
        claude_settings = _read_json_file(claude_settings_path)
        hooks = claude_settings.setdefault("hooks", {})
        hooks["PreToolUse"] = ensure_hook_in_list(hooks.get("PreToolUse", []), ROUTER_HOOK_CMD, matcher=ROUTER_GATE_MATCHER)
        text = _json_text(claude_settings)
        _write_hook_file(claude_settings_path, text, "claude-settings.json", "claude")

        for alt_dir in _secondary_claude_dirs():
            alt_settings_path = alt_dir / "settings.json"
            existing_alt = _read_json_file(alt_settings_path)
            alt_settings = json.loads(text)
            alt_env = dict(alt_settings.get("env") or {})
            for k, v in (existing_alt.get("env") or {}).items():
                if v is not None and v != "":
                    alt_env[k] = v
            alt_settings["env"] = alt_env
            _write_hook_file(alt_settings_path, _json_text(alt_settings), f"{alt_dir.name.lstrip('.')}-settings.json", "claude")

    # 2. Codex (& any secondary Codex homes) hooks.json
    if host_present("codex", dirs=(HOME / ".codex",)):
        codex_hooks_path = HOME / ".codex" / "hooks.json"
        codex_data = _read_json_file(codex_hooks_path)
        chooks = codex_data.setdefault("hooks", {})
        chooks["PreToolUse"] = ensure_hook_in_list(chooks.get("PreToolUse", []), ROUTER_HOOK_CMD, matcher=None)
        codex_text = _json_text(codex_data)
        _write_hook_file(codex_hooks_path, codex_text, "codex-hooks.json", "codex")
        for alt_codex in _secondary_codex_dirs():
            _write_hook_file(alt_codex / "hooks.json", codex_text, f"{alt_codex.name.lstrip('.')}-hooks.json", "codex")

    # 3. Antigravity ~/.gemini/config/hooks.json
    if host_present("agy", "gemini", dirs=(HOME / ".gemini",)):
        gemini_hooks_path = HOME / ".gemini" / "config" / "hooks.json"
        gemini_data = _read_json_file(gemini_hooks_path)
        # Place router-gate first in PreToolUse evaluation order
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
        _write_hook_file(gemini_hooks_path, _json_text(new_gemini_data), "gemini-hooks.json", "gemini")

    # 4. Pi ~/.pi/agent/extensions/router-gate.ts
    if host_present("pi", dirs=(HOME / ".pi",)):
        pi_ext_path = HOME / ".pi" / "agent" / "extensions" / "router-gate.ts"
        _write_hook_file(pi_ext_path, PI_ROUTER_GATE_TS, "pi-router-gate.ts", "pi")


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


def _uninstall_docs() -> None:
    for key, entry in sorted(MANIFEST["docs"].items()):
        path = Path(key)
        existing = read_text(path)
        if existing is None:
            continue
        if entry.get("mode") == "whole":
            if sha256(existing) != entry.get("sha256"):
                report(f"[uninstall] {path}: edited since setup; left untouched")
            else:
                report(f"[uninstall] {path}: {_restore_file(path, entry.get('backup'), bool(entry.get('created')))}")
            continue
        try:
            stripped = strip_block(existing)
        except ValueError:
            stripped = None
        if stripped is None:
            report(f"[uninstall] {path}: no managed block found")
            continue
        if not stripped.strip():
            if not DRY_RUN:
                path.unlink()
            report(f"[uninstall] {path}: removed (only the managed block was left)")
        else:
            write_text(path, stripped)
            report(f"[uninstall] {path}: managed block removed")


def _uninstall_hooks() -> None:
    for key, entry in sorted(MANIFEST["hooks"].items()):
        path = Path(key)
        kind = entry.get("kind")
        existing = read_text(path)
        if existing is None:
            continue
        if kind == "pi":
            if "router_gate_hook.py" not in existing:
                continue
            report(f"[uninstall] {path}: {_restore_file(path, entry.get('backup'), bool(entry.get('created')))}")
            continue
        try:
            data = json.loads(existing)
        except ValueError:
            report(f"[uninstall] {path}: not valid JSON; left untouched")
            continue
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


def _uninstall_links() -> None:
    for key, entry in sorted(MANIFEST["materialized"].items()):
        path = Path(key)
        if path.exists() or is_reparse_point(path):
            for child in sorted(path.iterdir()) if path.is_dir() else []:
                if is_reparse_point(child):
                    remove_dir_or_junction(child)
            remove_dir_or_junction(path)
        if entry.get("backup") and Path(entry["backup"]).exists() and not DRY_RUN:
            shutil.copytree(entry["backup"], path)
        report(f"[uninstall] {path}: materialized skill directory removed")
    for key, entry in sorted(MANIFEST["junctions"].items()):
        link = Path(key)
        if is_reparse_point(link):
            remove_dir_or_junction(link)
        elif link.exists():
            report(f"[uninstall] {link}: no longer a junction; left untouched")
            continue
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
    for key in sorted(MANIFEST["junctions"]):
        skills_dir = Path(key).parent
        try:
            if not DRY_RUN and skills_dir.name == "skills" and skills_dir.parent.name == "home"                     and skills_dir.parent.parent.parent.name == "codex-accounts"                     and skills_dir.is_dir() and not any(skills_dir.iterdir()):
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


def is_existing_install() -> bool:
    return hooks_installed() or host_rules.overlay_dir(HOME).is_dir()


def sync(*, docs: bool = True, skills: bool = True, hooks: bool | None = None, dry_run: bool = False,
         clean: bool | None = None, link_repo_skills: bool = False) -> list[str]:
    """Run the host synchronisation and return the report lines.

    hooks=None keeps hooks that are already installed up to date and installs none for a new user.
    """
    begin_run(dry_run)
    install_hooks = hooks_installed() if hooks is None else hooks
    existing = is_existing_install()
    ensure_paseo_bridge_dependencies()
    if not dry_run:
        print(f"Backups (only if something changes): {backup_root()}")
    if docs:
        unify_global_docs()
        update_paseo_config()
    if skills:
        if link_repo_skills or str(HOME / ".codex/skills/agent-compass") in MANIFEST["junctions"]:
            link_repo_skills_into_hub()
        deduplicate_and_link_skills()
    if clean if clean is not None else existing:
        clean_dead_files()
    if install_hooks:
        configure_hooks()
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
