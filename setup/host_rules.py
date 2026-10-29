"""Global agent rules written by `sync_host_globals.py`: the neutral public default and local overlays.

The repository only ships a generic English rule block. A user who wants different wording (another language, house
rules, dated notes) drops Markdown files into ``~/.route-manager/host-rules/`` and sync uses them instead:

    shared.md          ~/.claude/CLAUDE.md, ~/.pi, ~/.gemini and opencode global instructions
    codex.md           ~/.codex/AGENTS.md (and secondary Codex homes)
    daemon-append.md   the Paseo daemon ``appendSystemPrompt``

An overlay is used verbatim except for three placeholders that depend on where this checkout and the user's home are:

    @@ROUTE_JUDGE@@         absolute path of skills/route-agent-work/scripts/route_judge.py in this checkout
    @@ROUTER_HOOK_SCRIPT@@  absolute path of setup/router_gate_hook.py in this checkout
    @@HOME_POSIX@@          the user's home directory with forward slashes
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROUTE_JUDGE = (REPO / "skills" / "route-agent-work" / "scripts" / "route_judge.py").as_posix()
ROUTER_HOOK_SCRIPT = (Path(__file__).resolve().with_name("router_gate_hook.py")).as_posix()

OVERLAY_FILES = {"shared": "shared.md", "codex": "codex.md", "daemon-append": "daemon-append.md"}

LARGE_DATA_POLICY = """For large CSV, JSON, tables or logs, filter columns/rows and compute summaries in the tool invocation before returning output to this conversation. Keep complete research artifacts on disk and report their paths; do not silently truncate or discard source data or persisted user research.
"""

DEFAULT_SHARED = """# Agent routing rules (AgentCompass)

Cross-project rules for agents that can delegate work through the AgentCompass router. Project facts, strategy and business rules belong in each repository's own AGENTS.md / CLAUDE.md.

## 1. Background processes

1. A tool or command timing out only means *you* got the output back; a child process may still be running. Once you have enough output, clean up.
2. Before ending a turn, every long task you started is in exactly one state: finished, killed by you, or named in your report (PID and purpose) and handed to the user. Never leave an unclaimed background process behind.

## 2. Do it yourself, or delegate through the router

1. If you are already a delegated worker (the environment has `ROUTER_WORKER=1`, or the end of your brief carries a `Parent session agent ID:` contract), do the task yourself. Do not delegate again unless the brief appoints you coordinator.
2. In a main session, do small, local work yourself: roughly five minutes and three tool calls or fewer, such as reading a couple of known files, a tiny edit, or running one known test.
3. Delegate anything larger: multi-file exploration, read-edit-test loops, independent modules, work that can run in parallel, or work that suits another model or another account's quota.
4. To delegate, write the whole brief to a Markdown file and run exactly one command (never call Paseo `create_agent`, a host's `Agent`/`Task`, `spawn_agent` or similar directly):

   `python -X utf8 @@ROUTE_JUDGE@@ roll --brief <task.md> --hours <estimated active hours>`

   `--hours` is required. `roll` recognises the host and the current session, classifies the brief, picks the account/model/effort, records the choice and launches the worker through the best available channel.
5. Read the result before doing anything else:
   - `launched: true` (`caller_action: already_launched_do_not_respawn`): the worker is already running. Do not start another one. Wait for its message, or follow `continuation_argv` / `worker.output`.
   - `launched: false`, `dispatch_mode: self` (`caller_action: execute_self`): the router says no worker is needed; do the task yourself.
   - `launched: false`, `dispatch_mode: native` (`caller_action: launch_with_native_host_tool`): no Paseo daemon or CLI channel exists; start the worker with the host's own sub-agent tool using `route.bundle` and the full text of `worker_prompt_file`.
   - `dispatch_mode: none`: nothing fits or setup is incomplete. Read `route.reason`; if it points at onboarding, run `python -X utf8 @@REPO@@/setup/onboard.py`.
6. Do not raise the effort yourself; use what the router returned. Name a model or effort only when the user named it in this request, and then pass `--provider/--account/--model/--effort` together with `--basis "<the user's words>"`.
7. If an account becomes unavailable (expired, removed, out of quota), run the router again rather than substituting a similar-looking model.
8. Reuse an existing worker for a follow-up only while its accumulated uncompressed context is small (under about 2M tokens, counting every earlier compaction, not just the current window). Beyond that, write a handoff brief and `roll` a fresh worker.
9. Launch every worker with an explicit permission mode; never rely on a provider default (a worker waiting on a permission prompt looks "running" but does nothing). Use `selected_bundle.modeId` from the router; if absent: Codex `full-access`, Claude `bypassPermissions`, Antigravity `full-access`, OpenCode `build`. Narrow it for read-only roles. After launch, read the agent status once to confirm the mode took effect.
10. If the optional router gate hook is installed, it blocks sub-agent launches (including a host's built-in Explore) that have no router record from the last 15 minutes. Run `roll` first, or remove the hook with `python -X utf8 setup/onboard.py uninstall`.

## 3. Parent address and messaging

1. A delegated brief must carry the real parent session ID, host and message address; without them, do not launch. Never substitute `top-level`, a task ID or the worker's own ID. `roll` fills these in and appends the communication contract.
2. A worker that needs a clarification or decision must send a real message to its parent (in Paseo: `send_agent_prompt` to the actual parent ID with `background: true, notifyOnFinish: false`), then end the turn. Do not wait synchronously and do not use UI-only question tools (`AskUserQuestion`, `ask_user_question`, `request_user_input`, `request_user_input_async`); the parent never sees those.
3. When a parent adds work to, releases or resumes its own worker with `send_agent_prompt`, it must keep `notifyOnFinish` true (or omit it): the completion notification is the result channel for workers that cannot message.
4. If no messaging channel or parent address is available, or sending fails, finish with `BLOCKED / needs_input`, the exact question and the delivery problem, through the normal result channel.
5. This does not stop a main session from asking the real user a question, and it does not replace platform approvals.

## 4. Large files, tables and logs

""" + LARGE_DATA_POLICY

DEFAULT_CODEX = DEFAULT_SHARED

DEFAULT_DAEMON_APPEND = (
    "Router discipline: if you are already a delegated worker (ROUTER_WORKER=1 or the brief ends with a Parent session agent ID), do the work yourself and do not delegate again. "
    "In a main session, do small local work (about five minutes, three tool calls or fewer) yourself; delegate anything larger by writing the brief to a Markdown file and running only "
    "`python -X utf8 @@ROUTE_JUDGE@@ roll --brief <task.md> --hours <estimated hours>` instead of calling create_agent / Agent / spawn_agent / invoke_subagent directly. "
    "If roll returns launched=true the worker is already running: do not start another. Start a native sub-agent only when launched=false and dispatch_mode=native; do the work yourself when dispatch_mode=self. "
    "A worker that needs a decision must message its real parent (Paseo: send_agent_prompt with background=true, notifyOnFinish=false) and end its turn, never use AskUserQuestion-style tools; "
    "a parent that continues its own worker must keep notifyOnFinish true."
)


def overlay_dir(home: Path) -> Path:
    return home / ".route-manager" / "host-rules"


def is_legacy_generated(text: str) -> bool:
    """Recognise pre-marker generated rules even after an overlay changes the title/body."""
    stripped = text.lstrip()
    return stripped.startswith((
        "【全局强制纪律】",
        "# 全局执行纪律（",
        "# 全局执行纪律\n",
        "# Agent routing rules (AgentCompass)\n",
        "Router discipline: if you are already a delegated worker (",
    ))


def render(text: str, home: Path) -> str:
    return (
        text.replace("@@ROUTE_JUDGE@@", ROUTE_JUDGE)
        .replace("@@ROUTER_HOOK_SCRIPT@@", ROUTER_HOOK_SCRIPT)
        .replace("@@HOME_POSIX@@", home.as_posix())
        .replace("@@REPO@@", REPO.as_posix())
    )


def load(kind: str, home: Path) -> tuple[str, str]:
    """Return ``(rendered_text, source)`` where source is ``overlay`` or ``default``."""
    path = overlay_dir(home) / OVERLAY_FILES[kind]
    try:
        raw = path.read_bytes().decode("utf-8-sig")
    except OSError:
        raw = None
    if raw is not None and raw.strip():
        return render(raw, home), "overlay"
    default = {"shared": DEFAULT_SHARED, "codex": DEFAULT_CODEX, "daemon-append": DEFAULT_DAEMON_APPEND}[kind]
    return render(default, home), "default"
