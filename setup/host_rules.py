"""Global agent rules written by `sync_host_globals.py`: the neutral public default and local overlays.

The repository only ships a generic English rule block. A user who wants different wording (another language, house
rules, dated notes) drops Markdown files into ``~/.agent-compass/host-rules/`` and sync uses them instead:

    shared.md          ~/.claude/CLAUDE.md, ~/.pi, ~/.gemini and opencode global instructions
    codex.md           ~/.codex/AGENTS.md (and secondary Codex homes)
    daemon-append.md   the Paseo daemon ``appendSystemPrompt``
    dispatch.md        not written anywhere: the user's own dispatch workflow (worktrees, review habits). The default
                       rules name it when it exists; an overlay names it with @@PERSONAL_DISPATCH@@

An overlay is used verbatim except for placeholders that depend on where this checkout and the user's home are:

    @@AGENTCOMPASS@@        the router command: `agentcompass` for the Windows installer or an installed package, otherwise
                            `python -X utf8 <checkout>/agentcompass` (same sub-commands, any working directory)
    @@ROUTE_JUDGE@@         legacy; `python -X utf8 @@ROUTE_JUDGE@@` is rewritten to @@AGENTCOMPASS@@, a bare path stays the script path
    @@ROUTER_HOOK_SCRIPT@@  what the gate hook runs: `<app>/cli/agentcompass.exe gate-hook` for the Windows installer,
                            otherwise the absolute path of setup/router_gate_hook.py
    @@HOME_POSIX@@          the user's home directory with forward slashes
    @@DISPATCH_RULES@@      absolute path of this install's skills/agent-compass/references/dispatch-rules.md
    @@SKILL_DIR@@           absolute path of this install's skills/agent-compass
    @@PERSONAL_DISPATCH@@   absolute path of ~/.agent-compass/host-rules/dispatch.md
    @@SHARED@@              codex.md / daemon-append.md only: the whole shared rule text (overlay, else the default),
                            so the common rules live in one file and the others hold only what differs
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.append(str(REPO))
try:
    from agentcompass import install as ac_install  # noqa: E402
except ImportError:  # a bare copy of setup/ without the package
    ac_install = None

ROUTE_JUDGE = (REPO / "skills" / "agent-compass" / "scripts" / "route_judge.py").as_posix()
LEGACY_ENTRY = "python -X utf8 @@ROUTE_JUDGE@@"


def agentcompass_command() -> str:
    """The one way agents call the router: `agentcompass` when installed (Windows installer or package), else the checkout's package directory."""
    if ac_install is not None and (ac_install.frozen() or ac_install.shape(__file__) == "wheel"):
        return "agentcompass"
    if ac_install is None and "site-packages" in Path(__file__).resolve().parts:
        return "agentcompass"
    return f"python -X utf8 {(REPO / 'agentcompass').as_posix()}"


def router_hook_reference() -> str:
    """@@ROUTER_HOOK_SCRIPT@@: the installer's exe-based hook command, else the hook script's path."""
    script = Path(__file__).resolve().with_name("router_gate_hook.py")
    if ac_install is not None and ac_install.frozen():
        return ac_install.gate_hook_command(script)
    return script.as_posix()


ROUTER_HOOK_SCRIPT = router_hook_reference()

OVERLAY_FILES = {"shared": "shared.md", "codex": "codex.md", "daemon-append": "daemon-append.md"}

# Keep the installed skill and generated host prompts on the same ownership policy.
EXECUTION_POLICY = (REPO / "skills/agent-compass/SKILL.md").read_text(encoding="utf-8").split(
    "## Execution ownership\n", 1
)[1].split("\n## ", 1)[0].strip()

LARGE_DATA_POLICY = """For large CSV, JSON, tables or logs, filter columns/rows and compute summaries in the tool invocation before returning output to this conversation. Keep complete research artifacts on disk and report their paths; do not silently truncate or discard source data or persisted user research.
"""

SKILL_DIR = (REPO / "skills" / "agent-compass").as_posix()
DISPATCH_RULES = f"{SKILL_DIR}/references/dispatch-rules.md"
# Optional personal dispatch workflow; never copied into a host file, only named by the rules that point to it.
PERSONAL_DISPATCH_FILE = "dispatch.md"

# The installed block is a pointer: the dispatch procedure lives in the skill's docs, read only when dispatching. The
# docs path names this install's own copy of the skill, so the pointer and the docs it names always ship together.
DEFAULT_SHARED = """# Agent routing rules (AgentCompass)

Cross-project rules for agents that can delegate work through the AgentCompass router. Project facts, strategy and business rules belong in each repository's own AGENTS.md / CLAUDE.md.

## 1. Background processes

1. A tool or command timing out only means *you* got the output back; a child process may still be running. Once you have enough output, clean up.
2. Before ending a turn, every long task you started is in exactly one state: finished, killed by you, or named in your report (PID and purpose) and handed to the user. Never leave an unclaimed background process behind.

## 2. Delegation goes through AgentCompass

1. Do serial work yourself. Delegate only independent work that gains from parallelism, an isolated large context, a specialist capability or quota offloading. A delegated worker (`ROUTER_WORKER=1` or a `Parent session agent ID:` contract) does its assignment without delegating again.
2. Dispatch only with `@@AGENTCOMPASS@@ roll --task "<assignment>"` (or `--brief <file>`, one brief per independent task). Never call `create_agent`, `Agent`/`Task`, `spawn_agent` or `invoke_subagent` directly.
3. Read the result's `status` and do exactly `next_action`.
4. Never pick a model or effort yourself; the router does.
5. Before dispatching, load the `agent-compass` skill and read its dispatch rules (`@@DISPATCH_RULES@@`): named models, re-rolls, worker continuation, permission modes, the gate, parent messaging.
6. A worker asks its parent and reports through a real message, never a UI question tool; the contract at the end of its brief says how.
@@PERSONAL_DISPATCH_LINE@@

## 3. Large files, tables and logs

""" + LARGE_DATA_POLICY

DEFAULT_CODEX = DEFAULT_SHARED

DEFAULT_DAEMON_APPEND = (
    "AgentCompass: do serial work yourself; a delegated worker (ROUTER_WORKER=1 or a Parent session agent ID contract) does not delegate again. "
    "Dispatch only with `@@AGENTCOMPASS@@ roll --task \"<assignment>\"`, never create_agent / Agent / spawn_agent / invoke_subagent directly; "
    "read the result's status and do exactly next_action; never pick a model or effort yourself. "
    "Before dispatching, load the agent-compass skill and read @@DISPATCH_RULES@@.@@PERSONAL_DISPATCH_SENTENCE@@ "
    "A worker asks its parent and reports through a real message, never a UI question tool."
)


def personal_dispatch_path(home: Path) -> Path:
    return overlay_dir(home) / PERSONAL_DISPATCH_FILE


def _personal_dispatch_lines(home: Path) -> tuple[str, str]:
    """(numbered list line, sentence) naming the user's dispatch workflow file, or empty strings when there is none."""
    path = personal_dispatch_path(home)
    try:
        present = path.is_file() and bool(path.read_bytes().strip())
    except OSError:
        present = False
    if not present:
        return "", ""
    return (f"7. Before dispatching, also read `{path.as_posix()}` (your own dispatch workflow).\n",
            f" Also read {path.as_posix()} before dispatching.")


def overlay_dir(home: Path) -> Path:
    return home / ".agent-compass" / "host-rules"


def is_legacy_generated(text: str) -> bool:
    """Recognise pre-marker generated rules even after an overlay changes the title/body."""
    stripped = text.lstrip()
    return stripped.startswith((
        "【全局强制纪律】",
        "# 全局执行纪律（",
        "# 全局执行纪律\n",
        "# Agent routing rules (AgentCompass)\n",
        "Router discipline: if you are already a delegated worker (",
        "Router discipline: " + EXECUTION_POLICY,
        "Router discipline: The main session executes serial work directly by default",
        "AgentCompass: do serial work yourself;",
    ))


def render(text: str, home: Path) -> str:
    return (
        text.replace(LEGACY_ENTRY, "@@AGENTCOMPASS@@")
        .replace("@@AGENTCOMPASS@@", agentcompass_command())
        .replace("@@ROUTE_JUDGE@@", ROUTE_JUDGE)
        .replace("@@ROUTER_HOOK_SCRIPT@@", ROUTER_HOOK_SCRIPT)
        .replace("@@HOME_POSIX@@", home.as_posix())
        .replace("@@REPO@@", REPO.as_posix())
        .replace("@@DISPATCH_RULES@@", DISPATCH_RULES)
        .replace("@@SKILL_DIR@@", SKILL_DIR)
        .replace("@@PERSONAL_DISPATCH@@", personal_dispatch_path(home).as_posix())
    )


def load(kind: str, home: Path) -> tuple[str, str]:
    """Return ``(rendered_text, source)`` where source is ``overlay`` or ``default``."""
    path = overlay_dir(home) / OVERLAY_FILES[kind]
    try:
        raw = path.read_bytes().decode("utf-8-sig")
    except OSError:
        raw = None
    if raw is not None and raw.strip():
        if kind != "shared" and "@@SHARED@@" in raw:
            shared = load("shared", home)[0]
            raw = raw.replace("@@SHARED@@", shared.strip("\n"))
        return render(raw, home), "overlay"
    default = {"shared": DEFAULT_SHARED, "codex": DEFAULT_CODEX, "daemon-append": DEFAULT_DAEMON_APPEND}[kind]
    line, sentence = _personal_dispatch_lines(home)
    default = default.replace("@@PERSONAL_DISPATCH_LINE@@\n", line).replace("@@PERSONAL_DISPATCH_SENTENCE@@", sentence)
    return render(default, home), "default"
