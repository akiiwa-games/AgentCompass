"""Native PreToolUse hook that enforces routing (`agentcompass roll`) before spawning subagents.

Supports Claude Code, Codex CLI, Antigravity IDE/CLI, and Pi hook payloads on stdin.

Dispatch-labelled Paseo launches consume a task-bound, one-use durable claim.
Non-Paseo harness launches without label support use ledger matching checks:
an unbound ledger entry is never represented as dispatch authorization or a reservation.
"""
from __future__ import annotations

import ast
import datetime as dt
import importlib
import json
import os
import re
import shlex
import sys
import sqlite3
import warnings
from pathlib import Path

HOME = Path.home()
DEFAULT_LEDGER = HOME / ".agent-compass" / "router-ledger.jsonl"
LEDGER_TAIL_BYTES = 1024 * 1024
def router_command() -> str:
    """What the deny messages tell the agent to run: the console script when installed, else this checkout's package directory."""
    if getattr(sys, "frozen", False):
        return "agentcompass"
    here = Path(__file__).resolve()
    root = here.parents[1]
    if (
        "site-packages" in here.parts
        or not (root / "pyproject.toml").is_file()
        or not (root / "agentcompass" / "__main__.py").is_file()
    ):
        return "agentcompass"
    return f"python -X utf8 {(root / 'agentcompass').as_posix()}"


ROUTER_CMD = router_command()

# What this run could not check, one "what (detail)" each, in the order it happened. The gate allows when a check cannot
# be made (its documented degrade: it guards against accidental launches and is not a security boundary); the answer
# carries this list (`format_response`) so that allow is not silent. The detail is an error's type, never its text,
# which may hold a path, a credential or the task.
UNEVALUATED: list[str] = []


def note_unevaluated(what: str, detail: object) -> None:
    entry = f"{what} ({detail if isinstance(detail, str) else type(detail).__name__})"
    if entry not in UNEVALUATED:
        UNEVALUATED.append(entry)


def attempt(check: str, call, default):
    """The one place a failing check is caught: `default` stands in for its answer and the check is noted."""
    try:
        return call()
    except Exception as exc:
        note_unevaluated(check, exc)
        return default


def unevaluated_notice() -> str:
    return ("[Router Gate] Allowed without checking: " + "; ".join(UNEVALUATED)
            + ". The call was not judged on these; nothing was blocked because of them.") if UNEVALUATED else ""
DEFAULT_PASEO_AGENTS = HOME / ".paseo" / "agents"
DEFAULT_WINDOW_SECONDS = 900  # 15 minutes

# Every tool set below holds base names. A host exposes an MCP tool as `mcp__<server>__<tool>`, and a proxy that
# re-exports another host's tools nests the prefix (`mcp__magpie__mcp__paseo__send_agent_prompt`); base_tool_name
# strips exactly that structure before any comparison, so a prefix never changes what a call is.
DIRECT_SUBAGENT_TOOLS = frozenset({
    "Agent",
    "Task",
    "dispatch_agent",
    "spawn_agent",
    "invoke_subagent",
    "create_agent",
    "delegate_task",  # T3 Code's orchestrator MCP server (`t3-code`)
    # Claude Code's Workflow runs a script whose agent() calls start sub-agents inside the runtime; those launches
    # never pass through a PreToolUse hook, so the Workflow call itself is the launch.
    "Workflow",
    "create_schedule",  # Paseo: "Create a recurring schedule that starts a new agent on a cron cadence."
})

# Claude Code built-in agents that are a host tool or command in agent form. Calling one hands no work to a worker,
# so that call is not a launch:
#   web-fetch          stands in for the WebFetch tool in a session that has none of its own; WebFetch is its only
#                      tool, and the host presents calling it as ordinary tool use (the session's own agent list)
#   statusline-setup   what the `/statusline` command runs to write the status line setting
#   claude-code-guide  answers questions about Claude Code itself
# (code.claude.com/docs/en/sub-agents, "Built-in subagents"). Explore, Plan, general-purpose, claude and fork do the
# session's own work in another context: they stay launches. Codex `spawn_agent` roles (default, explorer, worker)
# and Antigravity `invoke_subagent` types (self, DeepCoder, DeepInvestigator, teamwork_*) are all delegated work, so
# those hosts have no entry here. A project or user definition of one of these names replaces the built-in and
# carries tools of its own (same page, "Choose the subagent scope"); that one is a launch.
CLAUDE_AGENT_TOOLS = frozenset({"Agent", "Task"})
HOST_TOOL_AGENTS = frozenset({"web-fetch", "statusline-setup", "claude-code-guide"})

# Paseo update_schedule re-targets an existing schedule's new-agent launch when it names a provider or model.
SCHEDULE_RETARGET_TOOLS = frozenset({"update_schedule"})
SCHEDULE_TARGET_FIELDS = ("provider", "model")

PASEO_TURN_TOOLS = frozenset({
    "send_agent_prompt",
    "run_turn",
    "send_message",
})

CODEMODE_TOOLS = frozenset({
    "codemode",
    "execute_code",
})

WORKER_QUESTION_TOOLS = frozenset({
    'AskUserQuestion', 'ask_user_question', 'request_user_input', 'request_user_input_async',
})

SHELL_TOOLS = frozenset({
    "Bash",
    "bash",
    "PowerShell",
    "powershell",
    "exec",
    "exec_command",
    "run_command",
    "shell",
})

# `mcp__<server>__`, nested when a proxy re-exports another server's tools (`mcp__magpie__mcp__paseo__x`). Server
# names are the host's normalized identifiers (letters, digits, `_`, `-`), so one may itself hold `__` or end in `_`.
# Because the class holds `_`, nested prefixes are also one prefix whose server name is `magpie__mcp__paseo`: one
# optional prefix accepts exactly the names a repeated one does, and its single greedy run keeps a match linear in
# the name's length, where a repeated `(?:mcp__[...]+?__)*` backtracks exponentially on a name that fails.
MCP_PREFIX = r"mcp__[A-Za-z0-9_-]+__"
CODEMODE_TURN_AGENT_RE = re.compile(r"\b(?:mcp__\w+__)?(?:run_turn|send_message|send_agent_prompt)\s*\(")
WRAPPED_AGENT_CALL_RE = re.compile(
    r"\b((?:mcp__\w+__)?(?:create_agent|spawn_agent|run_turn|send_message|send_agent_prompt))\s*\("
)
# Literal-only extraction: never evaluate wrapper code. Dynamic JS remains a
# legacy presence check; it cannot supply a trustworthy structured launch spec.
JS_TOKEN_RE = re.compile(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|//[^\n]*|/\*[\s\S]*?\*/|[A-Za-z_$][\w$]*|[^\s]''')
CODEX_EXEC_CMD_RE = re.compile(r'exec_command\s*\(\s*\{\s*(?:"cmd"|cmd)\s*:\s*("(?:\\.|[^"\\])*")', re.S)
LAUNCH_PROVIDER_RE = re.compile(
    r"""(?:(?:\\?["']provider\\?["']|\bprovider)\s*:\s*\\?["']([\w.-]+)|--provider(?:=|\s+)(?:\\?["'])?([\w.-]+)|CLAUDE_CONFIG_DIR\s*=\s*(?:\\?["'])?[^\s"']*\.(claude(?:-[\w.-]+)?)\b)""",
    re.I,
)
UUID_RE = re.compile(r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b", re.I)
TURN_TARGET_ARG_RE = re.compile(
    r"\b(?:agentId|agent_id|id|target)\s*:\s*(?:\\?[\"']([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\\?[\"']|([A-Za-z_$][\w$]*))",
    re.I,
)


# What the installed hook matchers must route here: every base name evaluate_payload acts on.
HOOKED_TOOLS = DIRECT_SUBAGENT_TOOLS | SCHEDULE_RETARGET_TOOLS | PASEO_TURN_TOOLS | CODEMODE_TOOLS | SHELL_TOOLS

# One pattern classifies a name here and, anchored, is the installed matcher (and the Pi extension's test), so the
# hook and the gate cannot disagree: a whole name is a hooked base tool behind any number of MCP server prefixes
# (see MCP_PREFIX), wherever each server name ends (`mcp__paseo___create_agent`, `mcp__my__srv__Agent`).
# The syntax is the subset Python and JavaScript share (no named groups, no lookbehind).
TOOL_NAME_PATTERN = (f"(?:{MCP_PREFIX})?("
                     + "|".join(sorted(HOOKED_TOOLS, key=lambda n: (n.lower(), n))) + ")")
TOOL_NAME_RE = re.compile(TOOL_NAME_PATTERN)


def base_tool_name(name: str) -> str:
    """The hooked tool a call names, prefixes removed (`mcp__magpie__Agent` is `Agent`); any other name unchanged."""
    name = str(name or "")
    match = TOOL_NAME_RE.fullmatch(name)
    return match.group(1) if match else name


def hook_matcher() -> str:
    """The PreToolUse matcher: TOOL_NAME_PATTERN anchored, as Claude Code and Gemini test a matcher unanchored.

    `ListAgents` does not reach the hook. Worker question tools keep their substring reach (`x.ask_user_question`).
    """
    questions = "|".join(sorted(WORKER_QUESTION_TOOLS, key=lambda n: (-len(n), n)))
    return f"^{TOOL_NAME_PATTERN}$|{questions}"


def parse_ts(value: object) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return parsed.astimezone(dt.timezone.utc) if parsed.tzinfo else None


def normalize_input(raw_input: object) -> dict:
    if isinstance(raw_input, dict):
        return raw_input
    if isinstance(raw_input, str):
        return {"_raw": raw_input}
    return {}


def harness_kind_of(payload: dict) -> str:
    if "toolCall" in payload or "workspaceRoots" in payload:
        return "antigravity"
    return "pi" if "toolName" in payload and "tool_name" not in payload else "claude_or_codex"


def extract_tool_call(payload: dict) -> tuple[str, dict, str]:
    """Return (tool_name, tool_input, harness_kind) from a hook stdin payload."""
    kind = harness_kind_of(payload)
    if kind == "antigravity":
        tc = payload.get("toolCall") or {}
        name = str(tc.get("name") or "")
        args = normalize_input(tc.get("args"))
        return name, args, kind
    if kind == "pi":
        name = str(payload.get("toolName") or "")
        args = normalize_input(payload.get("input"))
        return name, args, kind
    name = str(payload.get("tool_name") or "")
    args = normalize_input(payload.get("tool_input"))
    return name, args, "claude_or_codex"


def extract_shell_commands(tool_input: dict) -> list[tuple[str, bool]]:
    """The texts of a call the gate reads for launches, each with whether the tool's own contract calls it a shell command
    line: a `command` / `cmd` / `CommandLine` argument and the literal `cmd` of an `exec_command({cmd: ...})` inside code
    are; the code itself (`code`, `_raw`: a JavaScript or Python program of a code-mode tool) is not, and is read as a
    command line too only because a launcher may be spelled in it."""
    direct = (
        tool_input.get("command")
        or tool_input.get("cmd")
        or tool_input.get("CommandLine")
    )
    if direct:
        return [(str(direct), True)]
    raw = str(tool_input.get("_raw") or tool_input.get("code") or "")
    if not raw:
        return []
    extracted = []
    for match in CODEX_EXEC_CMD_RE.finditer(raw):
        try:
            extracted.append(json.loads(match.group(1)))
        except ValueError:
            extracted.append(match.group(1))
    return [(command, True) for command in extracted] or [(raw, False)]


def literal_call_args(tokens: list[str], start: int) -> dict:
    """Read a complete static object; never authorize a partial dynamic expression."""
    if start >= len(tokens) or tokens[start] != "{":
        return {}

    def string(token: str) -> str | None:
        if token[:1] not in ("'", '"'):
            return None
        try:
            return ast.literal_eval(token)
        except (ValueError, SyntaxError):
            return None

    def object_at(i: int, depth: int = 0) -> tuple[dict, int]:
        if depth > 30 or tokens[i] != '{':
            raise ValueError('Not a static object')
        result = {}
        i += 1
        while tokens[i] != '}':
            key = string(tokens[i])
            if key is None:
                if not re.fullmatch(r'[A-Za-z_$][\w$]*', tokens[i]):
                    raise ValueError('Dynamic key/spread')
                key = tokens[i]
            if key in result or tokens[i + 1] != ':':
                raise ValueError('Duplicate/dynamic property')
            i += 2
            value = tokens[i]
            if value == '{':
                result[key], i = object_at(i, depth + 1)
            elif string(value) is not None:
                result[key] = string(value)
                i += 1
            elif value in ('true', 'false', 'null'):
                result[key] = {'true': True, 'false': False, 'null': None}[value]
                i += 1
            else:
                raise ValueError('Dynamic value')
            if tokens[i] == ',':
                i += 1
            elif tokens[i] != '}':
                raise ValueError('Dynamic expression')
        return result, i + 1

    try:
        result, end = object_at(start)
        return result if tokens[end] == ')' else {}
    except (ValueError, IndexError):
        return {}


def wrapped_agent_calls(tool_name: str, tool_input: dict) -> list[tuple[str, dict]]:
    if tool_name not in CODEMODE_TOOLS | SHELL_TOOLS:
        return []
    code = str(tool_input.get("code") or tool_input.get("script") or tool_input.get("_raw") or "")
    tokens = [token for token in JS_TOKEN_RE.findall(code) if not token.startswith(("//", "/*"))]
    return [
        (base_tool_name(token), literal_call_args(tokens, i + 2))
        for i, token in enumerate(tokens[:-1])
        if (WRAPPED_AGENT_CALL_RE.fullmatch(token + "(") or
            re.split(r'__|\.', token)[-1] in WORKER_QUESTION_TOOLS) and tokens[i + 1] == "("
    ]


def normalize_launch(spec: dict) -> dict[str, str]:
    # Paseo agent records keep the launch settings under "config".
    settings = next((v for v in (spec.get("settings"), spec.get("config")) if isinstance(v, dict)), {})
    provider = str(spec.get("provider") or "").strip()
    provider, sep, paired_model = provider.partition("/")
    model = str((paired_model if sep else "") or spec.get("model") or settings.get("model") or "").strip()
    # Pi's ChatGPT OAuth launch namespace is absent from the canonical ledger
    # model. Strip only that known namespace, retaining the Pi execution owner.
    if provider == "pi" and model.startswith("openai-codex/"):
        model = model.removeprefix("openai-codex/")
    elif provider == "opencode" and model.startswith("opencode/"):
        model = model.removeprefix("opencode/")
    effort = str(spec.get("effort") or spec.get("reasoning_effort") or
                 spec.get("thinkingOptionId") or settings.get("thinkingOptionId") or "").strip()
    account = str(spec.get("account") or "").strip()
    return {key: value for key, value in {
        "provider": provider, "model": model, "effort": effort, "account": account,
    }.items() if value}


# `paseo schedule create|update` options that name the launch, as the MCP tools' argument keys normalize_launch reads.
PASEO_SCHEDULE_OPTIONS = {"--provider": "provider", "--model": "model", "--thinking": "thinkingOptionId", "--mode": "mode"}


UNKNOWN_CLI_VALUE = "<not a literal>"

# The Paseo CLI (commander): global options are accepted before and between command words, on the root and on
# `schedule`/`agent` alike (`paseo --json schedule create`, `paseo schedule --json create`).
PASEO_VALUE_OPTIONS = frozenset({"-o", "--format", "--host", "--home"})
PASEO_GROUP_COMMANDS = frozenset({"agent", "schedule"})
# Commands that create and start an agent. A schedule launch is judged as the MCP schedule tools.
PASEO_LAUNCH_COMMANDS = frozenset({("run",), ("agent", "run"), ("agent", "create"), ("agent", "start"),
                                   ("agent", "spawn"), ("create_agent",)})
PASEO_SCHEDULE_COMMANDS = {("schedule", "create"): "create_schedule", ("schedule", "update"): "update_schedule"}
# A cli_turn_calls entry for a worker launcher a shell command runs, args {"trigger": "shell:<launcher>"}; the launch
# itself is judged on the shell call.
SHELL_LAUNCH = "shell_launch"
# Words of the Dot runner that only inspect: help, a dry check, diagnostics, or an existing task's status or result.
# A CLI asked for its help prints it and starts nothing, whichever spelling asks.
HELP_FLAGS = frozenset({"--help", "-h"})
DOT_INSPECT_WORDS = HELP_FLAGS | {"--check-only", "doctor", "status", "--task-id"}
ORCA_LAUNCH_COMMANDS = frozenset({("orchestration", "worker-start"), ("agent", "launch")})
# Programs that run another command line: shells take it after -c / -Command / /c, package runners as their argv.
SHELL_PROGRAMS = frozenset({"bash", "sh", "zsh", "dash", "pwsh", "powershell", "cmd"})
POSIX_SHELL_COMMAND_FLAG_RE = re.compile(r"-[A-Za-z]*c[A-Za-z]*")   # `-c`, `-lc`, `-ec`
# `//c` is `/c` written through Git Bash, which would otherwise convert `/c` into a path.
WINDOWS_SHELL_COMMAND_FLAGS = frozenset({"-c", "-command", "/c", "/k", "//c", "//k"})
# Options of a POSIX shell that take a value; its first other non-option word is a script file.
POSIX_SHELL_VALUE_OPTIONS = frozenset({"-o", "+o", "-O", "+O", "--rcfile", "--init-file"})
PACKAGE_RUNNERS = {"npx": (), "bunx": (), "pnpm": ("dlx", "exec"), "yarn": ("dlx", "exec")}
START_PROCESS = frozenset({"start-process", "saps"})
PYTHON_STEM_RE = re.compile(r"py|python(?:\d+(?:\.\d+)*)?w?")
PYTHON_VALUE_FLAGS = frozenset({"-X", "-W"})
NODE_VALUE_FLAGS = frozenset({"-r", "--require", "--import", "--loader", "--experimental-loader", "-C", "--conditions"})
NODE_CODE_FLAGS = frozenset({"-e", "--eval", "-p", "--print"})
# Words before a program that run it rather than being it: shell keywords and prefix commands without options.
COMMAND_PREFIX_WORDS = frozenset({"if", "then", "else", "elif", "do", "while", "until", "!", "time", "exec",
                                  "command", "call"})
ENV_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.S)
# Programs that run the command after their own options: name -> (subcommands of which one must follow, options that
# take a value, positional arguments before the command). `timeout 600 <command>`, `uv tool run <command>`.
UV_VALUE_OPTIONS = frozenset({"--with", "--with-requirements", "--python", "-p", "--project", "--directory",
                              "--package", "--extra", "--group", "--env-file", "--index", "--from"})
PASS_THROUGH_WRAPPERS = {
    "env": ((), frozenset({"-u", "--unset", "-C", "--chdir"}), 0),   # -S / --split-string: command_words
    "timeout": ((), frozenset({"-s", "--signal", "-k", "--kill-after"}), 1),
    "sudo": ((), frozenset({"-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt", "-C", "-D",
                            "--chdir", "-r", "--role", "-t", "--type", "-U", "--other-user"}), 0),
    "nice": ((), frozenset({"-n", "--adjustment"}), 0),
    "nohup": ((), frozenset(), 0),
    "setsid": ((), frozenset(), 0),
    "stdbuf": ((), frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}), 0),
    "winpty": ((), frozenset(), 0),
    "wsl": ((), frozenset({"-d", "--distribution", "-u", "--user", "--cd"}), 0),
    "uv": ((("run",), ("tool", "run")), UV_VALUE_OPTIONS, 0),
    "uvx": ((), UV_VALUE_OPTIONS, 0),
    "pipx": ((("run",),), frozenset({"--spec", "--python", "--pip-args"}), 0),
    "poetry": ((("run",),), frozenset({"-C", "--directory", "-P", "--project"}), 0),
}
ENV_SPLIT_OPTIONS = ("-S", "--split-string")
LAUNCHER_SUFFIXES = (".exe", ".cmd", ".bat", ".ps1", ".js", ".mjs", ".cjs")


def program_name(word: str) -> str:
    """`C:\\x\\Paseo.CMD` -> `paseo.cmd`; the program a command word runs, lower-cased."""
    return word.replace("\\", "/").rsplit("/", 1)[-1].lower()


def program_stem(word: str) -> str:
    """`C:\\x\\Paseo.CMD` -> `paseo`: the program name without a Windows or Node launcher suffix."""
    name = program_name(word)
    return next((name[:-len(s)] for s in LAUNCHER_SUFFIXES if name.endswith(s)), name)


class ShellParseError(ValueError):
    """A command line no shell would run: an unterminated quote, substitution, comment or here-string."""


# How a command line is read: by the shell of the tool that sent it, or the shell a wrapper starts.
POSIX, POWERSHELL, CMD = "posix", "powershell", "cmd"
SHELL_TOOL_DIALECTS = {"Bash": POSIX, "bash": POSIX, "PowerShell": POWERSHELL, "powershell": POWERSHELL}
HOST_SHELL_DIALECT = POWERSHELL if os.name == "nt" else POSIX
# A line the chosen dialect cannot parse is read in this one before it counts as undecidable.
FALLBACK_DIALECT = {POSIX: POWERSHELL, POWERSHELL: POSIX}
SHELL_ESCAPE = {POSIX: "\\", POWERSHELL: "`", CMD: "^"}
# Characters a POSIX backslash outside quotes escapes. Before any other character it is kept, as a Windows path
# separator in a command a host sends through bash.
POSIX_ESCAPED = frozenset(" \t\"'\\;&|()<>{}$`#")
HEREDOC_RE = re.compile(r"""<<(-?)[ \t]*(['"]?)([^\s'"<>;|&()]+)\2""")
# cmd keeps the quotes of `cmd /c "<program>" ...` only for a quoted executable name (`cmd /?`).
CMD_SPECIAL = frozenset('&<>()@^|')
CMD_EXECUTABLE_SUFFIXES = (".exe", ".cmd", ".bat", ".com")


def program_dialect(program: object) -> str | None:
    """The dialect of a shell named as a program (`powershell`, `bash.exe`, `/bin/sh`, `cmd`), else None."""
    if not isinstance(program, str):
        return None
    stem = program_stem(program.strip())
    return (POWERSHELL if stem in ("pwsh", "powershell") else CMD if stem == "cmd"
            else POSIX if stem in SHELL_PROGRAMS else None)


def shell_dialect(tool_name: str, tool_input: dict) -> str:
    """The shell a call's command line is written for, decided from the call.

    A tool that names its shell keeps it (`Bash`, `PowerShell`). Any other shell tool (Codex `exec_command` and
    `exec`, Antigravity `run_command`, `shell`) runs the shell its `shell` argument names, else the host's default
    shell: PowerShell on Windows, a POSIX shell elsewhere.
    """
    named = SHELL_TOOL_DIALECTS.get(base_tool_name(tool_name))
    return named or program_dialect(tool_input.get("shell")) or HOST_SHELL_DIALECT


def read_heredoc(line: str, i: int, delimiter: str, strip_tabs: bool) -> tuple[str, int]:
    """The body of a here-document that starts at `i`, and the index after its delimiter line (or the end)."""
    lines = []
    while i < len(line):
        end = line.find("\n", i)
        end = len(line) if end < 0 else end
        text = line[i:end].rstrip("\r")
        i = end + 1
        if (text.lstrip("\t") if strip_tabs else text) == delimiter:
            break
        lines.append(text)
    return "\n".join(lines), min(i, len(line))


def arithmetic_end(line: str, start: int) -> int:
    """Index of the `)` that balances the `(` at `start` of a POSIX `((...))` arithmetic text."""
    depth = 0
    for i in range(start, len(line)):
        depth += {"(": 1, ")": -1}.get(line[i], 0)
        if not depth:
            return i
    raise ShellParseError("unterminated ((")


def simple_commands(line: str, dialect: str = POSIX) -> list[tuple[list[str], list[str]]]:
    """Each simple command a line runs, as (words, here-document bodies fed to its stdin), read in `dialect`.

    Commands end at an unquoted `;` `&&` `||` `|` `&` newline or bracket; the commands inside `$(...)` are included.
    Quotes are removed from words; quoted text, here-document bodies and PowerShell here-strings and `@(...)`
    arrays are argument text. Raises ShellParseError when the line does not parse.
    """
    return parse_commands(line, 0, dialect, nested=False)[0]


def parse_commands(line: str, i: int, dialect: str, nested: bool) -> tuple[list, int]:
    """simple_commands from `i`; when `nested`, up to the `)` that closes a `$(` and that index."""
    posix, powershell, cmd = dialect == POSIX, dialect == POWERSHELL, dialect == CMD
    escape = SHELL_ESCAPE[dialect]
    commands, words, stdin, heredocs = [], [], [], []
    word, in_word, depth, n = [], False, 0, len(line)

    def end_word():
        nonlocal word, in_word
        if in_word:
            words.append("".join(word))
        word, in_word = [], False

    def end_command():
        nonlocal stdin
        end_word()
        if words:
            commands.append((list(words), stdin))
            words.clear()
            stdin = []

    def substitution(start: int) -> int:
        """Collect the commands of the `$(` whose `(` is at `start`; the index of its `)`."""
        if posix and line[start + 1:start + 2] == "(":     # `$(( 1 << 2 ))` is arithmetic, not commands
            return arithmetic_end(line, start)
        inner, end = parse_commands(line, start + 1, dialect, nested=True)
        commands.extend(inner)
        return end

    def standalone(at: int) -> bool:
        return line[at + 1:at + 2] in ("", " ", "\t", "\r", "\n", ";", "|", "&", ")")

    while i < n:
        c, nxt = line[i], line[i + 1:i + 2]
        if c == escape and line.startswith(("\n", "\r\n"), i + 1):    # line continuation
            i = line.index("\n", i) + 1
        elif c == escape and nxt and (not posix or nxt in POSIX_ESCAPED):
            word.append(nxt)
            in_word, i = True, i + 2
        elif c in " \t\r":
            end_word()
            i += 1
        elif c == "\n":
            end_command()
            i += 1
            for delimiter, strip_tabs, bodies in heredocs:
                body, i = read_heredoc(line, i, delimiter, strip_tabs)
                bodies.append(body)
            heredocs.clear()
        elif c == "#" and not in_word and not cmd:
            end = line.find("\n", i)
            i = n if end < 0 else end
        elif powershell and c == "<" and nxt == "#" and not in_word:
            end = line.find("#>", i + 2)
            if end < 0:
                raise ShellParseError("unterminated <#")
            i = end + 2
        elif powershell and c == "@" and not in_word and nxt in ("'", '"') and line.startswith(("\n", "\r\n"), i + 2):
            body = line.index("\n", i) + 1
            end = line.find("\n" + nxt + "@", body - 1)
            if end < 0:
                raise ShellParseError("unterminated here-string")
            word.append(line[body:end].rstrip("\r"))
            in_word, i = True, end + 3
        elif powershell and c == "@" and not in_word and nxt == "(":
            # An array `@('a','b')`: its items are argument values, kept with their quotes for Start-Process.
            end = parse_commands(line, i + 2, dialect, nested=True)[1]
            word.append(line[i:end + 1])
            in_word, i = True, end + 1
        elif c == "'" and not cmd:
            end = line.find("'", i + 1)
            if end < 0:
                raise ShellParseError("unterminated '")
            word.append(line[i + 1:end])
            in_word, i = True, end + 1
        elif c == '"':
            in_word, i = True, i + 1
            while i < n and line[i] != '"':
                ch, after = line[i], line[i + 1:i + 2]
                if after and ((posix and ch == "\\" and after in '"\\$`\n') or (powershell and ch == "`")):
                    word.append("" if after == "\n" else after)
                    i += 2
                elif ch == "$" and after == "(" and not cmd:
                    end = substitution(i + 1)
                    word.append(line[i:end + 1])
                    i = end + 1
                else:
                    word.append(ch)
                    i += 1
            if i >= n:
                raise ShellParseError('unterminated "')
            i += 1
        elif c == "$" and nxt == "(" and not cmd:
            end = substitution(i + 1)
            word.append(line[i:end + 1])
            in_word, i = True, end + 1
        elif c == "$" and nxt == "{" and not cmd:
            end = line.find("}", i)
            if end < 0:
                raise ShellParseError("unterminated ${")
            word.append(line[i:end + 1])
            in_word, i = True, end + 1
        elif posix and c == "<" and nxt == "<" and line[i + 2:i + 3] != "<" and HEREDOC_RE.match(line, i):
            match = HEREDOC_RE.match(line, i)
            heredocs.append((match.group(3), bool(match.group(1)), stdin))
            end_word()
            i = match.end()
        elif c == ")" and nested and not depth:
            end_command()
            return commands, i
        elif posix and c == "(" and nxt == "(" and not in_word and not words:
            end_command()       # `(( x = 1 << 2 ))`: an arithmetic command, whose `<<` is a shift
            i = arithmetic_end(line, i) + 1
        elif c in "()":
            depth += 1 if c == "(" else -1
            end_command()
            i += 1
        elif c in "{}" and (powershell or (posix and not in_word and standalone(i))):
            # PowerShell script blocks; a POSIX `{ ...; }` group, not brace expansion inside a word (`{a,b}`).
            end_command()
            i += 1
        elif c in ";|&":
            end_command()
            i += 1
        else:
            word.append(c)
            in_word, i = True, i + 1
    if nested:
        raise ShellParseError("unterminated $(")
    end_command()
    return commands, n


def cmd_command_line(args: list[str]) -> str:
    """The line `cmd /c` runs: its arguments as Windows quotes them, then cmd's outer-quote rule (`cmd /?`).

    The quotes stay only around a quoted executable name with whitespace (`"C:\\Program Files\\x\\paseo.cmd" run`);
    otherwise the first and last quote go (`"cd x & paseo run"` runs `cd x & paseo run`).
    """
    line = " ".join(f'"{arg}"' if not arg or re.search(r"\s", arg) else arg for arg in args)
    if not line.startswith('"'):
        return line
    quoted = line[1:].partition('"')[0]
    if (line.count('"') == 2 and not CMD_SPECIAL & set(quoted) and re.search(r"\s", quoted)
            and program_name(quoted).endswith(CMD_EXECUTABLE_SUFFIXES)):
        return line
    last = line.rfind('"')
    return line[1:last] + line[last + 1:] if last > 0 else line[1:]


def posix_shell_script(args: list) -> bool:
    """Whether a POSIX shell's arguments name a script file: a non-option word, unless `-s` reads stdin."""
    i = 0
    while i < len(args) and isinstance(args[i], str):
        word = args[i]
        if word in ("-", "--"):     # end of options: the next word, if any, is the script
            return i + 1 < len(args)
        if word in POSIX_SHELL_VALUE_OPTIONS:
            i += 2
        elif word.startswith(("-", "+")):
            if word.startswith("-") and not word.startswith("--") and "s" in word[1:]:
                return False
            i += 1
        else:
            return True
    return False


def command_words(words: list) -> list:
    """A simple command from its program on: leading assignments, shell keywords and wrappers removed."""
    i = 0
    while i < len(words) and isinstance(words[i], str):
        word = words[i]
        stem = program_stem(word)
        if ENV_ASSIGNMENT_RE.fullmatch(word) or word.lower() in COMMAND_PREFIX_WORDS:
            i += 1
        elif word.startswith("$") and i + 1 < len(words) and words[i + 1] == "=":   # PowerShell `$out = <command>`
            i += 2
        elif stem in PASS_THROUGH_WRAPPERS:
            subcommands, value_options, positionals = PASS_THROUGH_WRAPPERS[stem]
            subcommand = next((s for s in subcommands if tuple(words[i + 1:i + 1 + len(s)]) == s), None)
            if subcommands and subcommand is None:
                break   # another subcommand of the wrapper (`uv pip install`): the wrapper is the program
            i += 1 + len(subcommand or ())
            while i < len(words) and isinstance(words[i], str) and words[i].startswith("-") and words[i] != "-":
                option, sep, value = words[i].partition("=")
                if stem == "env" and option in ENV_SPLIT_OPTIONS:
                    # `env -S 'prog args'` splits its value into the command, before the words that follow.
                    value = value if sep else words[i + 1] if i + 1 < len(words) else None
                    try:
                        split = shlex.split(value) if isinstance(value, str) else []
                    except ValueError:
                        split = []
                    return command_words([*split, *words[i + (1 if sep else 2):]]) if split else []
                i += 2 if words[i] in value_options else 1
                if words[i - 1] == "--":
                    break
            i += positionals
        elif word.lower() == "start":
            # cmd `start ["title"] [/b ...] <program>`; in PowerShell `start` is Start-Process.
            i += 1
            while i < len(words) and isinstance(words[i], str) and (not words[i] or words[i].startswith("/")):
                i += 1
            return ["Start-Process", *words[i:]]
        else:
            break
    return words[i:]


def paseo_command(args: list) -> tuple[tuple[str, ...], list] | None:
    """(command words, the words after them) of a `paseo` argv, skipping global options as commander does.

    None when a command word is not a literal: what it runs is unknown.
    """
    path, i = [], 0
    while i < len(args) and len(path) < 2:
        word = args[i]
        if not isinstance(word, str):
            return None
        if word.startswith("-"):
            i += 2 if word in PASEO_VALUE_OPTIONS else 1
            continue
        path.append(word)
        i += 1
        if path[0] not in PASEO_GROUP_COMMANDS:
            break
    return tuple(path), args[i:]


def paseo_cli_options(words: list) -> dict[str, str]:
    """The launch options of a parsed `paseo schedule` argv (`--opt value` or `--opt=value`); other words are ignored.

    A value that is not a literal (None from the Python reader) stays present as UNKNOWN_CLI_VALUE, which no route
    matches, so a dynamic `--provider` is still a launch and is never taken for a routed one.
    """
    options = {}
    for i, word in enumerate(words):
        if not isinstance(word, str):
            continue
        flag, sep, value = word.partition("=")
        if flag in PASEO_SCHEDULE_OPTIONS:
            if not sep:
                value = words[i + 1] if i + 1 < len(words) else None
            options[PASEO_SCHEDULE_OPTIONS[flag]] = value if isinstance(value, str) and value else UNKNOWN_CLI_VALUE
    return options


def cli_turn_calls(tool_name: str, tool_input: dict) -> list[tuple[str, dict]]:
    """Normalize the static Paseo CLI sends, schedule launches and worker launchers a call runs; never execute it.

    A shell line is parsed, in the dialect of the call (shell_dialect), into simple commands, and each is judged by the
    program it runs, seen through interpreters, shells and wrappers (`python <script>`, `bash -c`, `bash <<EOF`,
    `cmd /c`, `eval`, `iex`, `Start-Process`, `npx`, PASS_THROUGH_WRAPPERS). A launcher name that
    is an argument, a path, a search pattern or quoted text given to another program is not a launch. A model CLI
    asked for one answer (`claude -p`, `codex exec`) is a quick call that starts no worker and is not read. A line that
    does not parse, and a program or argument that is not a literal, is undecidable and is not a launch: the gate's
    fail-open degrade, since it guards against accidental launches and is not a security boundary.
    """
    if tool_name not in SHELL_TOOLS | CODEMODE_TOOLS:
        return []
    targets = []

    def launch(label: str):
        targets.append((SHELL_LAUNCH, {"trigger": f"shell:{label}"}))

    def argv(words: list, depth: int, stdin: list = ()):
        words = command_words(words)
        if not words or not isinstance(words[0], str) or depth > 4:
            return
        exe = program_name(words[0])
        stem = program_stem(words[0])
        args = words[1:]
        literal_args = [w for w in args if isinstance(w, str)]
        if stem == "paseo":
            paseo(args)
        elif exe == "run_dot_task.py" or stem == "dot-subagent":
            if not any(isinstance(w, str) and (w in DOT_INSPECT_WORDS or w.startswith("--task-id=")) for w in args):
                launch("run_dot_task.py")
        elif stem == "orca":
            if tuple(args[:2]) in ORCA_LAUNCH_COMMANDS and not HELP_FLAGS.intersection(literal_args):
                launch("orca worker launch")
        elif stem in SHELL_PROGRAMS:
            dialect = CMD if stem == "cmd" else POWERSHELL if stem in ("pwsh", "powershell") else POSIX
            flags = [i for i, w in enumerate(words) if isinstance(w, str) and (
                POSIX_SHELL_COMMAND_FLAG_RE.fullmatch(w) if dialect == POSIX
                else w.lower() in WINDOWS_SHELL_COMMAND_FLAGS)]
            rest = [w for w in words[flags[0] + 1:] if isinstance(w, str)] if flags else []
            if rest:
                # A POSIX shell runs the one string after -c; PowerShell joins what follows; cmd as cmd_command_line.
                shell(rest[0] if dialect == POSIX else " ".join(rest) if dialect == POWERSHELL
                      else cmd_command_line(rest), depth + 1, dialect)
            elif not flags and dialect == POSIX and not posix_shell_script(args):
                for body in stdin:      # `bash <<'EOF'`: the here-document is the script, unless a script file is
                    shell(body, depth + 1, POSIX)   # given (`bash run.sh <<EOF`: it is that script's stdin)
        elif stem == "eval":
            shell(" ".join(literal_args), depth + 1, POSIX)
        elif stem in ("invoke-expression", "iex"):
            shell(" ".join(w for w in literal_args if w.lower() != "-command"), depth + 1, POWERSHELL)
        elif stem in PACKAGE_RUNNERS:
            rest = words[1:]
            if PACKAGE_RUNNERS[stem]:
                if not rest or rest[0] not in PACKAGE_RUNNERS[stem]:
                    return
                rest = rest[1:]
            while rest and isinstance(rest[0], str) and rest[0].startswith("-"):
                rest = rest[2:] if rest[0] in ("-p", "--package") else rest[1:]
            argv(rest, depth + 1)
        elif exe in START_PROCESS:
            start_process(args, depth)
        elif PYTHON_STEM_RE.fullmatch(stem):
            python_interpreter(args, depth)
        elif stem == "node":
            node_interpreter(args, depth)

    def python_interpreter(args: list, depth: int):
        """`python [options] <script> ...` runs the script; `-c` runs code; `-m` and stdin run no script by path."""
        i = 0
        while i < len(args) and isinstance(args[i], str):
            word = args[i]
            if word == "-c":
                if i + 1 < len(args) and isinstance(args[i + 1], str):
                    python(args[i + 1], depth + 1)
                return
            if word in ("-m", "-"):
                return
            if word.startswith("-"):
                i += 2 if word in PYTHON_VALUE_FLAGS else 1
                continue
            argv(args[i:], depth + 1)
            return

    def node_interpreter(args: list, depth: int):
        """`node [options] <script> ...` runs the script; inline `-e` code is not read."""
        i = 0
        while i < len(args) and isinstance(args[i], str):
            word = args[i]
            if word in NODE_CODE_FLAGS:
                return
            if word.startswith("-"):
                i += 2 if word in NODE_VALUE_FLAGS else 1
                continue
            argv(args[i:], depth + 1)
            return

    def paseo(args: list):
        if HELP_FLAGS.intersection(w for w in args if isinstance(w, str)):
            return
        parsed = paseo_command(args)
        if parsed is None:
            return
        path, rest = parsed
        if path in PASEO_SCHEDULE_COMMANDS:
            # Same launch as MCP create_schedule / update_schedule: the provider settings become its arguments.
            targets.append((PASEO_SCHEDULE_COMMANDS[path], paseo_cli_options(rest)))
        elif path in PASEO_LAUNCH_COMMANDS:
            launch("paseo agent create")
        elif path in (('send',), ('agent', 'send')):
            ids = [w for w in rest if not (isinstance(w, str) and w.startswith('-'))]
            if ids and isinstance(ids[0], str) and UUID_RE.fullmatch(ids[0]):
                targets.append(('run_turn', {'agentId': ids[0]}))

    def start_process(args: list, depth: int):
        """PowerShell `Start-Process <file> [-ArgumentList] a,b,...`: the program and its literal arguments."""
        program, listed, i = None, [], 0
        while i < len(args):
            word = args[i] if isinstance(args[i], str) else ''
            lower = word.lower()
            if lower == '-filepath' and i + 1 < len(args):
                program, i = args[i + 1], i + 2
            elif lower in ('-argumentlist', '-args'):
                i += 1
                while i < len(args) and not (isinstance(args[i], str) and re.fullmatch(r'-[A-Za-z]+', args[i])
                                              and args[i].lower() not in ('-c', '-q', '-o', '-h', '-y', '-p')):
                    listed.append(args[i] if isinstance(args[i], str) else '')
                    i += 1
            elif re.fullmatch(r'-[A-Za-z]+', word):
                i += 2 if lower in ('-workingdirectory', '-verb', '-windowstyle', '-redirectstandardoutput',
                                    '-redirectstandarderror', '-redirectstandardinput', '-credential') else 1
            else:
                if program is None:
                    program = word
                else:
                    listed.append(word)
                i += 1
        if not isinstance(program, str) or not program:
            return
        elements = []
        for word in listed:
            if word.startswith("@(") and word.endswith(")"):    # an array literal, kept raw by the tokenizer
                for match in re.finditer(r"""'((?:[^']|'')*)'|"((?:[^"`]|`.)*)"|([^\s,]+)""", word[2:-1]):
                    single, double, bare = match.groups()
                    elements.append(single.replace("''", "'") if single is not None
                                    else re.sub(r"`(.)", r"\1", double) if double is not None else bare)
            else:
                elements.extend(part for part in word.split(",") if part)
        # Start-Process joins the elements with spaces into one Windows command line and adds no quotes, so only
        # double quotes written into an element keep a spaced path together (its documented `"`"C:\x y\a.py`""`).
        items = [item.replace('"', '') for item in re.findall(r'(?:"[^"]*"|[^\s"])+', " ".join(elements))]
        argv([program, *items], depth + 1)

    def python(code: str, depth: int):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')   # shell text read as Python: its escapes are not ours to judge
                tree = ast.parse(code)
        except (SyntaxError, ValueError):
            return
        bindings = {}

        def literal(node):
            if isinstance(node, ast.Name):
                return bindings.get(node.id)
            if isinstance(node, (ast.List, ast.Tuple)):
                return [literal(item) for item in node.elts]
            return node.value if isinstance(node, ast.Constant) else None

        def in_source_order(node):
            yield node
            for child in ast.iter_child_nodes(node):
                yield from in_source_order(child)

        for node in in_source_order(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        bindings[target.id] = literal(node.value)
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == 'subprocess'
                    and node.func.attr in ('run', 'Popen', 'call', 'check_call', 'check_output')):
                argument = node.args[0] if node.args else next(
                    (kw.value for kw in node.keywords if kw.arg == 'args'), None)
                value = literal(argument)
                if isinstance(value, list):
                    argv(value, depth)
                elif isinstance(value, str):
                    shell(value, depth + 1, POSIX)

    def shell(command: str, depth: int, dialect: str, program: bool = False):
        """Reads `command` for launches. `program`: it is a code-mode tool's code, not a shell command line."""
        if depth > 4:
            return
        # Also accepts a Python script supplied directly as a wrapper's code.
        python(command, depth)
        commands = None
        failure = None
        for reading in (dialect, FALLBACK_DIALECT.get(dialect)):
            try:
                commands = simple_commands(command, reading) if reading else None
            except ShellParseError as exc:
                failure = exc
                continue    # read it in the other dialect before it counts as undecidable
            break
        if commands is None:
            # Undecidable in both readings: not a launch (see the docstring). The answer says so for a shell command
            # line; a program that does not parse as one never was a shell command.
            if not program:
                note_unevaluated("reading a shell command for launches", failure)
            return
        if not commands:
            return
        for words, stdin in commands:
            argv(words, depth, stdin)

    for command, line in extract_shell_commands(tool_input):
        shell(command, 0, shell_dialect(tool_name, tool_input), program=not line)
    return targets


def defined_agent_names(directory: Path) -> set[str]:
    """Names the agent files under one `agents` directory define: the frontmatter `name`, else the file's own name.
    An unreadable file or directory defines nothing here: refusing to read a page costs more than the rare miss."""
    names = set()
    try:
        files = sorted(directory.rglob("*.md"))
    except OSError:
        return names
    for file in files:
        name = file.stem
        try:
            lines = file.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError:
            continue
        if lines and lines[0].strip() == "---":
            for line in lines[1:]:
                if line.strip() == "---":
                    break
                key, _, value = line.partition(":")
                if key.strip() == "name" and value.strip():
                    name = value.strip().strip("'\"")
                    break
        names.add(name.casefold())
    return names


def custom_agent_names(cwd: object) -> set[str]:
    """Agent names defined where Claude Code reads project and user definitions: every `.claude/agents` from the
    working directory upwards, and the user's Claude home. Definitions passed on the command line, by managed
    settings or by a plugin are not visible to a hook."""
    start = Path(str(cwd)) if cwd else Path.cwd()
    directories = [folder / ".claude" / "agents" for folder in (start, *start.parents)]
    directories.append(Path(os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude") / "agents")
    return set().union(*(defined_agent_names(directory) for directory in directories))


def host_tool_agent(tool_name: str, tool_input: dict, cwd: object = None) -> bool:
    """Whether this Agent call runs a built-in agent that only stands in for a host tool (see HOST_TOOL_AGENTS)."""
    agent = str(tool_input.get("subagent_type") or "")
    return (tool_name in CLAUDE_AGENT_TOOLS and agent in HOST_TOOL_AGENTS
            and agent.casefold() not in custom_agent_names(cwd))


def detect_subagent_launch(tool_name: str, tool_input: dict, cwd: object = None) -> str | None:
    """Return a short description of the subagent launch if this tool call spawns one, else None."""
    tool_name = base_tool_name(tool_name)
    if not tool_name:
        return None
    if tool_name in DIRECT_SUBAGENT_TOOLS:
        return None if host_tool_agent(tool_name, tool_input, cwd) else f"tool:{tool_name}"
    if tool_name in SCHEDULE_RETARGET_TOOLS:
        return f"tool:{tool_name}" if any(tool_input.get(k) for k in SCHEDULE_TARGET_FIELDS) else None
    if tool_name in CODEMODE_TOOLS:
        if any(name in DIRECT_SUBAGENT_TOOLS for name, _ in wrapped_agent_calls(tool_name, tool_input)):
            return f"{tool_name}:create_agent"
        return None
    if tool_name in SHELL_TOOLS:
        if any(name in DIRECT_SUBAGENT_TOOLS for name, _ in wrapped_agent_calls(tool_name, tool_input)):
            return f"{tool_name}:create_agent"
        return next((args["trigger"] for name, args in cli_turn_calls(tool_name, tool_input)
                     if name == SHELL_LAUNCH), None)
    return None


def load_paseo_agent(agent_id: str, paseo_agents_dir: Path | None = None) -> dict | None:
    base = paseo_agents_dir or Path(os.environ.get("ROUTER_PASEO_AGENTS_DIR", DEFAULT_PASEO_AGENTS))
    if not agent_id or not re.fullmatch(r"[\w-]+", agent_id) or not base.is_dir():
        return None
    try:
        for candidate in base.glob(f"*/{agent_id}.json"):
            doc = json.loads(candidate.read_text(encoding="utf-8", errors="replace"))
            if isinstance(doc, dict):
                return doc
    except (OSError, ValueError):
        return None
    return None


def agent_labels(doc: object) -> dict:
    return doc["labels"] if isinstance(doc, dict) and isinstance(doc.get("labels"), dict) else {}


def recorded_sessions(doc: object) -> set[str]:
    """The provider session a Paseo agent record says the agent runs (`persistence.sessionId`, `runtimeInfo.sessionId`):
    the ID that agent's harness reports to its hooks as `session_id`."""
    if not isinstance(doc, dict):
        return set()
    return {str(part.get("sessionId")) for part in (doc.get("persistence"), doc.get("runtimeInfo"))
            if isinstance(part, dict) and part.get("sessionId")}


def is_caller(agent_id: str, identities: set[str], paseo_agents_dir: Path | None = None) -> bool:
    """The Paseo agent `agent_id` is the calling session: its ID is one of the caller's identities, or its record's
    provider session is (a Paseo-hosted caller whose hook carries only its host session ID)."""
    return agent_id in identities or bool(recorded_sessions(load_paseo_agent(agent_id, paseo_agents_dir)) & identities)


def owns_worker(doc: object, agent_id: str, identities: set[str], paseo_agents_dir: Path | None = None) -> bool:
    """Only unambiguous persisted parent labels prove ownership: every parent label names the caller (`is_caller`), and
    the target is not the caller itself. `identities` are the caller's IDs (`caller_identities`)."""
    labels = agent_labels(doc)
    parents = [labels.get(key) for key in ("router.parent_id", "paseo.parent-agent-id") if labels.get(key)]
    return bool(identities and parents and agent_id not in identities and not (recorded_sessions(doc) & identities)
                and all(is_caller(parent, identities, paseo_agents_dir) for parent in parents))


def owns_running_worker(doc: object, agent_id: str, identities: set[str], paseo_agents_dir: Path | None = None) -> bool:
    """A parent can control an existing turn; unknown state cannot authorize one."""
    return (owns_worker(doc, agent_id, identities, paseo_agents_dir)
            and doc.get("status", doc.get("lastStatus")) == "running")


def caller_parent_ids(caller_id: str | None, paseo_agents_dir: Path | None = None) -> set[str]:
    """IDs a worker reports up to; a turn sent to one of them is never a dispatch."""
    labels = agent_labels(load_paseo_agent(caller_id, paseo_agents_dir)) if caller_id else {}
    return {v for v in (labels.get("router.parent_id"), labels.get("paseo.parent-agent-id")) if v and v != caller_id}


def is_delegated_worker(agent_id: str, paseo_agents_dir: Path | None = None) -> bool:
    """The agent was started by a parent: router labels or a persisted parent label."""
    labels = agent_labels(load_paseo_agent(agent_id, paseo_agents_dir))
    return bool(any(k.startswith("router.") for k in labels)
                or labels.get("router.parent_id") or labels.get("paseo.parent-agent-id"))


def detect_target_agent_ids(tool_name: str, tool_input: dict) -> list[str]:
    """Extract target Paseo agent ID(s) from a turn tool or codemode/shell wrapper."""
    tool_name = base_tool_name(tool_name)
    if tool_name in PASEO_TURN_TOOLS:
        direct = str(tool_input.get("agentId") or tool_input.get("agent_id") or tool_input.get("id") or tool_input.get("target") or "").strip()
        return [direct] if direct and re.fullmatch(r"[\w-]+", direct) else []
    if tool_name in CODEMODE_TOOLS | SHELL_TOOLS:
        code = str(tool_input.get("code") or tool_input.get("script") or tool_input.get("_raw") or "")
        if not CODEMODE_TURN_AGENT_RE.search(code):
            return []
        ids = []
        for match in TURN_TARGET_ARG_RE.finditer(code):
            uid, var = match.group(1), match.group(2)
            if uid:
                ids.append(uid)
            elif var:
                bound = re.search(rf"\b{re.escape(var)}\s*=\s*\\?[\"']({UUID_RE.pattern})\\?[\"']", code, re.I)
                if bound:
                    ids.append(bound.group(1))
        return list(dict.fromkeys(ids))
    return []


class RouterTreeMismatch(Exception):
    """The skill tree the hook loads lacks a module or function this hook version calls (hook and skill from different installs)."""


def router_owner(module: str, *names: str):
    """The named functions of a router module (one function: itself). A missing module or function is one explicit
    outcome, RouterTreeMismatch, which evaluate_payload turns into a denial that names the action."""
    try:
        loaded = router_module(module)
        found = tuple(getattr(loaded, name) for name in names)
    except (ImportError, AttributeError) as exc:
        raise RouterTreeMismatch(f"{module}.{'/'.join(names)}: {exc}") from exc
    return found[0] if len(found) == 1 else found


def router_module(name: str):
    checkout = Path(__file__).resolve().parents[1] / "skills/agent-compass"
    skill = Path(os.environ.get("ROUTER_SKILL_ROOT") or (
        checkout if (checkout / "scripts").is_dir() else HOME / ".codex/skills/agent-compass"))
    if str(skill / "scripts") not in sys.path:
        sys.path.insert(0, str(skill / "scripts"))
    return importlib.import_module(name)


def check_worker_compaction_limit(
    agent_id: str | None,
    paseo_agents_dir: Path | None = None,
    home: Path | None = None,
    identities: set[str] | None = None,
) -> str | None:
    """Block a parent's new turn to its own idle worker at the configured historical-token rotation threshold. A message
    to an agent the caller does not own is communication, and control of the caller's own running turn passes.
    `identities` are the caller's IDs (`caller_identities`)."""
    ids = identities or set()
    doc = load_paseo_agent(agent_id, paseo_agents_dir) if agent_id else None
    if not owns_worker(doc, agent_id, ids, paseo_agents_dir) or owns_running_worker(doc, agent_id, ids, paseo_agents_dir):
        return None
    base = paseo_agents_dir or Path(os.environ.get("ROUTER_PASEO_AGENTS_DIR", DEFAULT_PASEO_AGENTS))
    home_dir = home or (base.parents[1] if base.name == "agents" and base.parent.name == ".paseo" else base.parent)
    try:
        account_fleet = router_module("account_fleet")
        worker_sessions = router_module("worker_sessions")
    except ImportError:
        return None
    max_tokens = read_reference(lambda: worker_sessions.rotation_threshold(account_fleet.load_accounts()))
    state = worker_sessions.session_state(agent_id, home=home_dir)
    compactions = state.get("compactions") or 0
    compacted_tok = state.get("compacted_tokens") or 0
    current_tok = state.get("context_tokens") or 0
    carried = worker_sessions.worker_historical_tokens(state)
    if not (isinstance(carried, (int, float)) and carried >= max_tokens):
        return None
    return (
        f"[Router Gate] 拦截向已达历史累计上下文上限的旧 worker 续派任务 (agent_id={agent_id}, "
        f"historical_context_tokens={carried:,}/{max_tokens:,}, compactions={compactions})："
        f"该 worker 历史累计未压缩上下文达 {carried:,} tokens"
        f"（已达配置的上下文轮换阈值 rotate_after_carried_tokens={max_tokens:,}；"
        f"历次压缩前累计 {compacted_tok:,} + 当前窗口 {current_tok:,}，非压缩后当前窗口），继续追加 prompt 会导致上下文漂移与质量退化。"
        "禁止向该旧 worker 开启新 turn；父会话仍可向自己明确运行中的 worker 发控制消息以收尾交接。"
        "若已 idle 或状态未知，请收取已有输出，必要时调用 cancel_agent，再把当前进展与剩余工作写成交接任务书 <handoff.md>，"
        f"运行 {ROUTER_CMD} roll --brief <handoff.md> 由新 worker 接手。"
    )


def target_spec(spec: dict, labels: dict) -> dict[str, str]:
    """Launch spec of a new or existing worker; router labels name its account and bundle exactly."""
    target = normalize_launch(spec)
    for key, label in (("account", "router.account"), ("bundle_id", "router.bundle_id")):
        if isinstance(labels.get(label), str) and labels[label]:
            target[key] = labels[label]
    return target


def launch_target(name: str, args: dict, trigger: str) -> dict[str, str]:
    target = target_spec(args, args.get("labels") if isinstance(args.get("labels"), dict) else {})
    if name in CODEMODE_TOOLS | SHELL_TOOLS and not target.get("provider"):
        match = LAUNCH_PROVIDER_RE.search(json.dumps(args, ensure_ascii=False))
        if match:
            target["provider"] = next(group for group in match.groups() if group)
    if trigger == "shell:run_dot_task.py":
        target["provider"] = "dot"
    return target


def bundle_launch_model(bundle: dict, fallback_provider: str | None = None) -> str | None:
    """Normalized wire model spelling a catalog bundle launches as (`launch_model`, else `model`)."""
    raw = bundle.get("launch_model") or bundle.get("model")
    if not isinstance(raw, str) or not raw.strip():
        return None
    prov = str(bundle.get("provider") or fallback_provider or "").strip()
    return normalize_launch({"provider": prov, "model": raw}).get("model")


def launch_model_problem(spec: dict) -> str | None:
    """Why a launch call's provider/model carries a malformed `/` pin (empty prefix or model segment), else None."""
    settings = next((v for v in (spec.get("settings"), spec.get("config")) if isinstance(v, dict)), {})
    raw_provider = str(spec.get("provider") or "").strip()
    if raw_provider and raw_provider != UNKNOWN_CLI_VALUE and "/" in raw_provider:
        head, _, tail = raw_provider.partition("/")
        if not head.strip() or not tail.strip():
            return (f"invalid_model_pin: provider {raw_provider!r} is malformed "
                    "(empty provider or model segment around '/'); write <provider> or <provider>/<model>.")
        problem = router_owner("model_resolution", "model_pin_problem")(tail, "provider")
        if problem:
            return problem
    for field, raw_model in (("model", spec.get("model")), ("settings.model", settings.get("model"))):
        if raw_model in (None, "", UNKNOWN_CLI_VALUE):
            continue
        problem = router_owner("model_resolution", "model_pin_problem")(raw_model, field)
        if problem:
            return problem
    return None


def resolve_target(target: dict, accounts: dict | None) -> tuple[str | None, list[dict]]:
    """Owning account and the bundles the target can be, by bundle_id or by model/effort/Pi harness."""
    table = (accounts or {}).get("accounts") or {}
    provider = target.get("provider")
    account = target.get("account") or (
        "antigravity" if provider == "antigravity-resilient" else provider if provider in table else None)
    candidates = []
    if target.get("bundle_id") and table:
        candidates = router_owner("account_fleet", "bundle_rows")(accounts, target["bundle_id"])
    elif not target.get("bundle_id") and bool(target.get("model")):
        if target["model"] != UNKNOWN_CLI_VALUE:
            router_owner("model_resolution", "check_model_pin")(target["model"], "model")
        pin_pool, pin_matching_bundles = router_owner("account_fleet", "pin_pool", "pin_matching_bundles")
        pool = [
            dict(bundle, bundle_id=bundle["id"], launch_model=bundle_launch_model(bundle, bundle["account"]) or "")
            for bundle in pin_pool(accounts, account=account, provider=provider)
        ]
        if target["model"] in CLAUDE_MODEL_FAMILIES:
            matched = [c for c in pool if same_launch_value("model", target["model"], c.get("launch_model"))]
        else:
            matched = pin_matching_bundles(target["model"], pool, accounts)
        candidates = [c for c in matched if target.get("effort") in (None, c.get("effort"))]
    if not account and len({c["account"] for c in candidates}) == 1:
        account = candidates[0]["account"]
    return account, [c for c in candidates if c["account"] == account]


def native_own_account(name: str, target: dict, accounts: dict | None) -> str | None:
    """The calling session's own account for a native sub-agent launch (Claude Code `Agent`/`Task`, Codex
    `spawn_agent`) that names no provider or account, when discovery records it (provider_inventory.session_account);
    None otherwise (parent-address-gate.md states that limit). Only the unchecked-model rule reads it, and only for a
    launch that names a model (resolve_target matches bundles by model): every quota, window, floor and cooldown check
    on a native launch resolves the launch as it is written."""
    family = "claude" if name in CLAUDE_AGENT_TOOLS else "codex" if name == "spawn_agent" else None
    if not family or any(target.get(k) for k in ("provider", "account", "bundle_id")):
        return None
    return router_owner("provider_inventory", "session_account")(family, (accounts or {}).get("_discovery") or {})


def native_unchecked_text(account: str, model: str, row: dict) -> str:
    """The unchecked-model refusal of a native launch: the model the launch asked for, the account it runs on, and the
    route record it was matched against."""
    bundle = row.get("bundle") if isinstance(row.get("bundle"), dict) else {}
    # The shape decision_ledger writes: the bundle carries `bundle_id`; a row for a model without a data row has none.
    matched = bundle.get("bundle_id") or bundle.get("model")
    kind = row.get("constraint") or row.get("stage")
    return (f"[Router Gate] 拦截：原生子 agent 请求的模型 {model} 在本会话账号 {account} 上的档位都在模型管理中未勾选"
            f"（不参与自动派发）；匹配到的路由记录是 {matched}（{kind}），不是用户点名的路由。"
            "只有用户在本次请求中点名的路由（user_override 记录）可以启动它。" + DISABLED_RECOVERY)


DISABLED_RECOVERY = (
    f"请把任务写进 <task.md>，运行 {ROUTER_CMD} roll --brief <task.md> 由路由在参与自动派发的模型中选择；"
    "用户在本次请求中点名了该模型时，用 --provider/--account/--model --basis \"<用户原话>\" 记录后启动"
    f"（账号残留旧计量字段时运行 {ROUTER_CMD} setup 清除）。"
)


def check_target_disabled(
    target: dict, account: str | None, candidates: list[dict], accounts: dict | None,
) -> tuple[str, str] | None:
    """(kind, refusal) for a launch on an account with leftover meter fields (`legacy`, a fact: refused whatever the
    route) or with every matching bundle unchecked in model management (`bundles`: out of automatic routing, so the
    launch passes only on a route the user named, which the caller decides from the launch's authority). Host policy
    is not read here: it governs automatic routing, and a launch carries the authority of the route it matches."""
    if not account:
        return None
    disabled_cause, disabled_text = router_owner("host_policy", "disabled_cause", "disabled_text")
    found = disabled_cause(account, candidates, accounts)
    if not found:
        return None
    if found[0] == "legacy":
        return "legacy", f"[Router Gate] 拦截启动残留旧计量字段的账号：{disabled_text(found)}。" + DISABLED_RECOVERY
    return "bundles", (f"[Router Gate] 拦截：{disabled_text(found)}，只有用户在本次请求中点名的路由"
                       "（user_override 记录）可以启动它。" + DISABLED_RECOVERY)


def read_reference(read):
    """``read()``, a read of the reference set in effect with no effect of its own. When it fails on a fetched set, it
    reads again from the shipped set as a whole (public_data.with_fallback), and the fetched set is set aside; only when
    the shipped set fails too does the error reach the caller, whose degrade then applies. No network, no thread."""
    try:
        public_data = router_module("public_data")
    except ImportError:
        return read()
    return public_data.with_fallback(read)


def check_own_worker_legacy(target: dict, account: str | None, accounts: dict | None) -> str | None:
    """Block a parent's new turn to its own idle worker on an account with leftover meter fields: a fact, like an
    exhausted window. The preference switches (host policy, an unchecked model) do not refuse continuing a worker."""
    found = account and router_owner("host_policy", "legacy_cause")(account, accounts)
    if not found:
        return None
    return (f"[Router Gate] 拦截向残留旧计量字段的账号上的 worker 续派任务 (agent_id={target['agent_id']})：{found[1]}。"
            "父会话仍可向自己明确运行中的 worker 发控制消息以收尾。否则请收取已有输出，把进展写进 <task.md>，"
            f"运行 {ROUTER_CMD} roll --brief <task.md> 改派；{ROUTER_CMD} setup 会清除旧计量字段。")


def check_route_authority(row: dict, trigger: str) -> str | None:
    """A native launch matched to a record that is not the user's own naming (`hard_constraint`, `dot_closed_loop`) on
    an account outside this host's automatic routing (`host_policy_violation`). Decided on the row's constraint: a
    `user_override` row is launch authority whatever else it carries (v0.7.0 wrote the field on those rows too)."""
    violation = row.get("host_policy_violation")
    if row.get("constraint") == "user_override" or not isinstance(violation, dict):
        return None
    return (f"[Router Gate] 拦截 ({trigger})：{violation.get('reason')}。"
            f"这条 {row.get('constraint')} 记录不是启动授权：项目规则不覆盖本机的自动派发开关。"
            "用户在本次请求中点名了该路由时，改用 "
            f"{ROUTER_CMD} record --constraint user_override ... --basis \"<用户原话>\" 记录后启动；"
            f"否则运行 {ROUTER_CMD} roll --brief <task.md>，由路由在参与自动派发的账号中选择。")


def load_dispatch_accounts() -> dict | None:
    """The fleet, read from the reference set in effect (read_reference); configuration unreadable also from the
    shipped set degrades to None (no account or bundle resolves, nothing is refused)."""
    return attempt("the account fleet", lambda: read_reference(router_module("account_fleet").load_accounts), None)


def target_windows(candidates: list[dict], accounts: dict | None) -> list[str] | None:
    """Quota windows the resolved target bundles draw; None when unknown."""
    if not candidates or not accounts:
        return None
    def windows() -> list[str]:
        pools = router_module("quota_pools").bundle_pools
        return sorted({wid for bundle in candidates for wid in pools(dict(bundle, id=bundle["bundle_id"]), accounts)})
    return attempt("the quota windows of the target", windows, None)


def check_account_quota_exhausted(
    account: str | None,
    usage_cache_path: Path | None = None,
    now: dt.datetime | None = None,
    accounts: dict | None = None,
    windows: list[str] | None = None,
    bundles: list[dict] | None = None,
    model: str | None = None,
    records: list[dict] | tuple[dict, ...] | None = None,
    exclude_dispatch_id: str | None = None,
    exclude_agent_id: str | None = None,
) -> str | None:
    """Block reason when a window the launch draws refuses it, decided by the router itself.

    `bundles` are the target's resolved bundles; without them the launch is probed as `model` on `account`
    (an unknown model draws every window). Which windows bind and when they refuse come from
    quota_pools.launch_blockers on usage_reading.usage_snapshot, the same rules and reading routing uses:
    the launch floor, a window used up inside its cycle, an empty short window, active in-flight
    reservations, and a scope from data only. `windows` are the quota windows the launch draws, for the
    confirmed-exhaustion latch (an account-wide latch blocks every launch). When the router modules cannot
    be loaded only that latch is checked and the gate otherwise allows: it never invents quota rules of its own.
    """
    if not account:
        return None
    now = now or dt.datetime.now(dt.timezone.utc)
    latched_windows, without_own, window_refusal = router_owner(
        "turn_refusal", "latched_windows", "without_own", "window_refusal")
    # Confirmed upstream exhaustion is durable even with no/stale usage cache.
    # This applies to new launches and resumed turns, never to read-only investigation.
    held = latched_windows(account, windows, usage_cache_path, HOME)
    if held:
        every, script = router_owner("quota_exhaustion", "ALL", "__file__")
        drawn = f"every window of {account}" if every in held else f"window {', '.join(sorted(held))} of {account}"
        clear = (f'{"agentcompass" if getattr(sys, "frozen", False) else "python"} -X utf8 '
                 f'"{Path(script).resolve().as_posix()}" --clear {account}')
        return (
            f'[Router Gate] A confirmed quota failure is recorded for {drawn}, which this launch draws. The record '
            'lifts when a newer successful usage reading shows the window under 100% used or past its reset time; the passing '
            f'of time alone does not lift it. To clear it by hand run: {clear}. While the owned worker is running, send a control message with completion '
            f'notifications enabled; otherwise collect existing output, cancel_agent if needed, and run {ROUTER_CMD} roll --brief <task.md>.'
        )
    if records is None:
        records = attempt("the in-flight quota reservations",
                          lambda: router_module("dispatch_registry").reservations(now=now, read_only=True), ())
        # A watcher that died without sending still owes the parent one end notice. A failure here
        # must not become a launch denial, and a second read does not send it again.
        try:
            router_module("dispatch_flow").deliver_end_notices(now=now)
        except Exception:
            pass
    # The reference read (accounts, the window verdict on them) falls back to the shipped set as a whole; the
    # end notices above are an effect and stay outside it.
    found = attempt("quota window verdict", lambda: read_reference(lambda: window_refusal(
        account, accounts if accounts is not None else router_module("account_fleet").load_accounts(), now,
        usage_cache_path, without_own(records, exclude_dispatch_id, exclude_agent_id), bundles, model)), None)
    if not found:
        return None
    what, wid, why = found
    return (
        f"[Router Gate] 拦截向额度不足的窗口派发或续跑任务：{account} 的 {what} 所用窗口 {wid} 不可用（{why}）。"
        "运行中的受控子会话仍可由父会话直接发送控制消息收尾；idle 或运行状态未知的 worker 请先收取已有输出，"
        f"必要时 cancel_agent，再把进展写进 <task.md> 运行 {ROUTER_CMD} roll --brief <task.md> 改派其他可用账号/档位。"
    )


def is_dot_route(row: dict | None) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get("choice") == "dot_text":
        return True
    bundle = row.get("bundle")
    return isinstance(bundle, dict) and bundle.get("provider") == "dot"


def recent_routes(
    ledger_path: Path,
    now: dt.datetime | None = None,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
) -> list[dict]:
    """Read recent decisions newest first, bounded to the ledger's last 1 MiB (plan lines carry a decision snapshot)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if not ledger_path.is_file():
        return []
    try:
        size = ledger_path.stat().st_size
        with ledger_path.open("rb") as fh:
            if size > LEDGER_TAIL_BYTES:
                fh.seek(size - LEDGER_TAIL_BYTES)
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []

    lines = [line.strip() for line in tail.splitlines() if line.strip()]
    rows = []
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        stage = row.get("stage")
        choice = row.get("choice")
        if stage not in ("plan", "select", "record"):
            continue
        if choice in (None, "", "none", "self"):
            continue
        at = parse_ts(row.get("at"))
        if at is None:
            continue
        age = (now - at).total_seconds()
        if -60 <= age <= window_seconds:
            rows.append(row)
    return rows


CORRELATION_KEYS = {
    "parent": ("parent_agent_id", "parentAgentId", "parent_id", "parentId", "session_id", "sessionId"),
    "task": ("task_id", "taskId"),
    "task_name": ("task_name", "taskName"),
    "target": ("target_agent_id", "targetAgentId", "agent_id", "agentId", "target"),
    "chain": ("chain_id", "chainId", "chain"),
}


def correlation_fields(*sources: dict) -> dict[str, str]:
    result = {}
    for source in sources:
        for key, aliases in CORRELATION_KEYS.items():
            for alias in aliases:
                value = source.get(alias)
                if key == "chain" and isinstance(value, dict):
                    value = value.get("id")
                if isinstance(value, str) and value:
                    result[key] = value
                    break
    return result


def _merged_route_bundle(row: dict, accounts: dict | None = None) -> dict:
    raw_bundle = row.get("bundle") or row.get("selected_bundle") or {}
    raw_dict = raw_bundle if isinstance(raw_bundle, dict) else {}
    choice = raw_dict.get("bundle_id") or row.get("choice")
    catalog_bundle = {}
    if isinstance(choice, str) and choice and isinstance(accounts, dict):
        acct, matches = resolve_target({"bundle_id": choice}, accounts)
        if matches:
            catalog_bundle = dict(matches[0])
            if acct and not catalog_bundle.get("account"):
                catalog_bundle["account"] = acct
    merged = {**catalog_bundle, **raw_dict}
    for key in ("provider", "account", "model", "effort"):
        val = row.get(key)
        if isinstance(val, str) and val and not merged.get(key):
            merged[key] = val
    return merged


def route_spec(row: dict, accounts: dict | None = None) -> dict[str, str]:
    merged = _merged_route_bundle(row, accounts)
    if isinstance(merged.get("launch_model"), str) and merged["launch_model"].strip():
        merged = {**merged, "model": merged["launch_model"]}
    spec = normalize_launch(merged)
    if not spec.get("provider"):
        if is_dot_route(row):
            spec["provider"] = "dot"
        elif isinstance(merged.get("account"), str) and merged["account"]:
            spec["provider"] = merged["account"]
    return spec


# Claude's Agent/Task tool takes only a family alias and runs the host's current
# model of that family; the router routes one model per Claude family.
CLAUDE_MODEL_FAMILIES = frozenset({"sonnet", "opus", "haiku", "fable"})


def same_launch_value(key: str, requested: str, routed: str | None) -> bool:
    """Compare what will run: a bare Claude family alias is that family's model."""
    if key == "model" and requested in CLAUDE_MODEL_FAMILIES:
        return bool(routed) and routed.startswith(f"claude-{requested}-")
    return requested == routed


def match_route(
    rows: list[dict],
    requested: dict,
    context: dict,
    trigger: str,
    accounts: dict | None = None,
) -> tuple[dict | None, str]:
    """Use available correlation, then compare launch fields, without inventing IDs.

    A newer decision for the *same identified request* supersedes its old one.
    Without identity, search all compatible rows in the TTL (legacy callers).
    Missing identity is not proof of ownership: a dispatch_id lifecycle still
    needs caller/ledger support, launch acknowledgement and terminal release.
    """
    candidates = []
    for row in rows:
        task = row.get("task") if isinstance(row.get("task"), dict) else {}
        identity = correlation_fields(row, task)
        shared = context.keys() & identity.keys()
        # A parent recorded from a host terminal (Orca) is the worker's reply address; the harness running in it
        # (Claude Code) reports its own session, which roll records alongside as host_session_id.
        host_session = row.get("host_session_id") or task.get("host_session_id")
        if any(context[k] != identity[k] and not (k == "parent" and host_session and context[k] == host_session)
               for k in shared):
            continue
        candidates.append((len(shared), row))
    if not candidates:
        return None, "parent/task/target/chain 不匹配；没有本次请求的兼容记录"
    strongest = max(score for score, _ in candidates)
    if strongest:
        candidates = [item for item in candidates if item[0] == strongest]
        latest = candidates[0][1]
        latest_identity = correlation_fields(latest, latest.get("task") if isinstance(latest.get("task"), dict) else {})
        if (context.keys() & latest_identity.keys()) - {"parent"}:
            # Parent alone may own several concurrent tasks. A task/target/chain
            # identifies a request closely enough to honor its latest decision.
            candidates = candidates[:1]
    mismatches = []
    for _, row in candidates:
        chosen = route_spec(row, accounts)
        differences = [f"{key}: requested={value!r}, routed={chosen.get(key)!r}"
                       for key, value in requested.items() if not same_launch_value(key, value, chosen.get(key))]
        if is_dot_route(row) and trigger != "shell:run_dot_task.py":
            differences.append("dot_text requires run_dot_task.py")
        if not differences:
            return row, ""
        mismatches.append(f"choice={row.get('choice')!r}: " + "; ".join(differences))
    return None, " | ".join(mismatches[:3])


def build_deny_reason(trigger: str, window_seconds: int) -> str:
    minutes = max(1, round(window_seconds / 60))
    return (
        f"[Router Gate] 拦截未路由的子 agent 启动 ({trigger})："
        f"最近 {minutes} 分钟内 ~/.agent-compass/router-ledger.jsonl 中无有效的 plan/select/record 记录。"
        "把任务书写进 <task.md>，一条命令完成分类、选路与启动：\n"
        f"  {ROUTER_CMD} roll --brief <task.md>\n"
        "（用户在本次请求中点名了 model/effort 时追加 --provider <p> --account <a> --model <m> --effort <e> "
        "--basis \"<用户原话>\"；宿主没有自动启动器时 roll 会返回 route.bundle，按它原生启动。）"
    )


WORKFLOW_TRIGGER = "tool:Workflow"
SCHEDULE_TRIGGERS = frozenset({"tool:create_schedule", "tool:update_schedule"})


def workflow_deny_reason(cause: str) -> str:
    return (
        f"[Router Gate] 拦截未授权的 Workflow 启动 (tool:Workflow)：{cause}。"
        "Workflow 的脚本会在运行时内部启动多个子 agent，roll 不适用（roll 会立即启动一个普通 worker，替换掉 workflow）。"
        "只有用户在本次请求中明确要求用 workflow 时才可调用：在本会话中先运行\n"
        f"  {ROUTER_CMD} record --constraint user_override --trigger workflow --provider <p> --account <a> "
        "--model <本会话模型> --effort <e> --basis \"<用户要求 workflow 的原话>\"\n"
        "再重试（15 分钟内有效，只授权本会话的 Workflow）。用户没有要求 workflow 时不要调用：自己做，或用 roll 派发单个 worker。"
    )


def schedule_deny_reason(trigger: str, cause: str) -> str:
    return (
        f"[Router Gate] 拦截未路由的定时 agent 启动 ({trigger})：{cause}。"
        "schedule 每次触发都会启动新 agent；roll 不适用（roll 会立即启动 worker，不会建 schedule）。"
        "先把任务写进 <task.json>（objective 写定时要做的事）并运行\n"
        f"  {ROUTER_CMD} plan --input <task.json>\n"
        "再用返回 bundle 的 provider 与 model 创建：create_schedule 传 provider=\"<provider>/<model>\"，"
        "或 `paseo schedule create --provider <provider>/<model> ...`（update_schedule 同时给出 provider 与 model）。"
        "gate 只接受本会话最近 15 分钟内 provider/model 一致的 plan 记录（plan 须在本会话中运行）。"
    )


def caller_identities(payload: dict, caller_id: str | None) -> set[str]:
    """IDs that name the calling session: the host's session ID and, under Paseo, the calling agent."""
    return {str(v) for v in (payload.get("session_id"), payload.get("sessionId"), payload.get("parent_agent_id"),
                             payload.get("callerAgentId"), caller_id) if v}


def written_by(row: dict, identities: set[str]) -> bool:
    """The row was written by the calling session: its `session_ids` (the writer's own IDs, never the parent link
    in parent_id/host_session_id, which a worker's rows carry for its parent) name one of the caller's IDs."""
    ids = row.get("session_ids")
    return isinstance(ids, list) and bool(identities & {str(v) for v in ids if v})


def check_workflow_record(rows: list[dict], identities: set[str]) -> str | None:
    """A Workflow passes only on this session's `record --constraint user_override --trigger workflow`: the row
    `decision_ledger.WORKFLOW_RECORD` names, which `record` also reads to refuse any other constraint."""
    if not identities:
        return workflow_deny_reason("hook 输入里没有会话 ID，无法确认授权记录属于本会话")
    try:
        rule = router_module("decision_ledger").WORKFLOW_RECORD
    except (ImportError, AttributeError) as exc:
        return workflow_deny_reason(f"无法读取 Workflow 授权规则（decision_ledger.WORKFLOW_RECORD：{type(exc).__name__}）")
    for row in rows:
        if all(row.get(key) == value for key, value in rule.items()) and written_by(row, identities):
            return None
    return workflow_deny_reason("最近 15 分钟内本会话没有 `--trigger workflow` 的 user_override 记录")


def check_schedule_plan(rows: list[dict], args: dict, context: dict, trigger: str,
                        accounts: dict | None, identities: set[str]) -> str | None:
    """A schedule passes only on this session's recent plan with its provider and model (records do not count)."""
    requested = normalize_launch(args)
    missing = [key for key in ("provider", "model") if not requested.get(key)]
    if missing:
        return schedule_deny_reason(trigger, f"未指定 {'/'.join(missing)}，无法与路由比对")
    if not identities:
        return schedule_deny_reason(trigger, "hook 输入里没有会话 ID，无法确认 plan 属于本会话")
    plans = [row for row in rows if row.get("stage") in ("plan", "select") and written_by(row, identities)]
    if not plans:
        return schedule_deny_reason(trigger, "最近 15 分钟内本会话没有 plan 记录")
    row, diagnostic = match_route(plans, requested, context, trigger, accounts=accounts)
    return None if row else schedule_deny_reason(trigger, f"与最近的 plan 不一致：{diagnostic}")


def format_response(allowed: bool, harness_kind: str, reason: str = "") -> dict:
    """The answer in the shape of the host. An allow carries what this run could not check (`UNEVALUATED`) where the host
    reads a message beside an allow: `systemMessage`, a field of Claude Code's and Codex's hook output for every hook,
    shown to the user and no decision; Antigravity's optional `reason` ("the explanation shown to the agent or to you for
    the decision", antigravity.google/docs/hooks, PreToolUse). Pi's `tool_call` return only controls blocking, so the
    notice goes to stderr, which the generated extension shows (`PI_ROUTER_GATE_TS`)."""
    notice = unevaluated_notice() if allowed else ""
    if notice and harness_kind == "pi":
        print(notice, file=sys.stderr)
    if harness_kind == "antigravity":
        if allowed:
            return {"decision": "allow", "reason": notice} if notice else {"decision": "allow"}
        return {"decision": "deny", "reason": reason}
    if harness_kind == "pi":
        return {"block": False} if allowed else {"block": True, "reason": reason}
    if allowed:
        return {"systemMessage": notice} if notice else {}
    return {
        "decision": "block",
        "reason": reason,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }


def evaluate_payload(
    payload: dict,
    ledger_path: Path | None = None,
    now: dt.datetime | None = None,
    window_seconds: int | None = None,
    usage_cache_path: Path | None = None,
    paseo_agents_dir: Path | None = None,
) -> dict:
    """The hook's answer to one payload (`decide`). The one place a failure inside the decision is caught: the call is
    allowed, as every unmade check is, and the answer names the failure's type (`format_response`)."""
    UNEVALUATED.clear()
    try:
        return decide(payload, ledger_path, now, window_seconds, usage_cache_path, paseo_agents_dir)
    except Exception as exc:
        note_unevaluated("the gate's decision", exc)
        return format_response(True, harness_kind_of(payload))


def decide(
    payload: dict,
    ledger_path: Path | None,
    now: dt.datetime | None,
    window_seconds: int | None,
    usage_cache_path: Path | None,
    paseo_agents_dir: Path | None,
) -> dict:
    tool_name, tool_input, harness_kind = extract_tool_call(payload)
    tool_name = base_tool_name(tool_name)
    if os.environ.get("ROUTER_GATE_BYPASS") == "1":
        return format_response(True, harness_kind)
    caller_id = str(payload.get("parent_agent_id") or payload.get("callerAgentId")
                    or os.environ.get("PASEO_AGENT_ID") or "").strip() or None
    # Who the caller is, for ownership: its Paseo agent ID and the host session ID its hook payload carries.
    identities = caller_identities(payload, caller_id)

    calls = wrapped_agent_calls(tool_name, tool_input) or [(tool_name, tool_input)]
    calls.extend(cli_turn_calls(tool_name, tool_input))

    if any(re.split(r'__|\.', name)[-1] in WORKER_QUESTION_TOOLS for name, _ in calls):
        worker = os.environ.get('AGENTCOMPASS_WORKER') == '1' or (
            caller_id and is_delegated_worker(caller_id, paseo_agents_dir))
        if worker:
            return format_response(False, harness_kind,
                '[Router Gate] Delegated workers cannot use request_user_input_async, request_user_input '
                'or other user-question dialogs. Send the question to the actual parent ID using the '
                'host messaging channel (e.g. Paseo send_agent_prompt or host SendMessage). If delivery '
                'fails, return BLOCKED / needs_input through the result channel and end the turn.')
    # A native wrapper may both resume an agent and invoke a spawning CLI.
    if calls[0][0] != tool_name:
        raw = str(tool_input.get("_raw") or tool_input.get("code") or "")
        if CODEX_EXEC_CMD_RE.search(raw):
            calls.extend(("exec_command", {"cmd": command}) for command, _ in extract_shell_commands(tool_input))
    executing = []
    for name, args in calls:
        if (name == "send_agent_prompt"
                and identities and args.get("notifyOnFinish") is False):
            for agent_id in detect_target_agent_ids(name, args):
                # Only persisted ownership proves direction; absent/conflicting
                # labels or caller identity leave this notification check open.
                if owns_worker(load_paseo_agent(agent_id, paseo_agents_dir), agent_id, identities, paseo_agents_dir):
                    return format_response(False, harness_kind,
                        '[Router Gate] Parent-to-worker send_agent_prompt must keep completion '
                        'notifications enabled. Set notifyOnFinish: true or omit it; completion '
                        'notifications and the final reply are the result channel for workers '
                        'without messaging tools (e.g. agy). Worker-to-parent questions and final reports may use false.')
        trigger = detect_subagent_launch(name, args, payload.get("cwd"))
        if trigger or name in PASEO_TURN_TOOLS or (
            name in CODEMODE_TOOLS | SHELL_TOOLS and CODEMODE_TURN_AGENT_RE.search(json.dumps(args, ensure_ascii=False))
        ):
            executing.append((name, args, trigger))
    if not executing:
        return format_response(True, harness_kind)

    ledger = ledger_path or Path(os.environ.get("ROUTER_LEDGER", DEFAULT_LEDGER))
    win = window_seconds if window_seconds is not None else int(
        os.environ.get("ROUTER_GATE_WINDOW_SECONDS", DEFAULT_WINDOW_SECONDS)
    )
    rows = None
    accounts = load_dispatch_accounts()
    upward = caller_parent_ids(caller_id, paseo_agents_dir)
    for name, args, trigger in executing:
        if trigger:
            try:
                malformed = launch_model_problem(args)
            except RouterTreeMismatch as exc:
                return format_response(False, harness_kind,
                    f"[Router Gate] 拦截：本钩子与已加载的 agent-compass 技能目录版本不一致（{exc}）。"
                    "请运行 `agentcompass setup` 使钩子与技能目录来自同一次安装后重试。")
            if malformed:
                return format_response(False, harness_kind,
                    f"[Router Gate] 拦截格式错误的模型参数 ({trigger})：{malformed}")
            targets = [launch_target(name, args, trigger)]
        else:
            # The gate governs dispatch, not communication. A turn is dispatch only when it hands more work to the
            # caller's own idle worker (persisted parent labels name the caller, `owns_worker`); a message to any other agent,
            # an ancestor, or an agent whose ownership cannot be proven is communication and passes unchecked, and
            # control of the caller's own running turn passes too.
            docs = {agent_id: load_paseo_agent(agent_id, paseo_agents_dir)
                    for agent_id in detect_target_agent_ids(name, args) or detect_target_agent_ids(tool_name, tool_input)
                    if agent_id not in upward}
            agent_ids = [agent_id for agent_id, doc in docs.items()
                         if owns_worker(doc, agent_id, identities, paseo_agents_dir)
                         and not owns_running_worker(doc, agent_id, identities, paseo_agents_dir)]
            targets = [dict(target_spec(docs[agent_id], agent_labels(docs[agent_id])), agent_id=agent_id,
                            dispatch_id=agent_labels(docs[agent_id]).get("router.dispatch_id")) for agent_id in agent_ids]
        dispatch_id = args.get("labels", {}).get("router.dispatch_id") if isinstance(args.get("labels"), dict) else None
        unchecked = None    # an unchecked model: refused below unless the launch's authority is the user's naming
        native_unchecked = None     # (account, model) of a native launch's unchecked model, named in its refusal
        for target in targets:
            try:
                account, candidates = resolve_target(target, accounts)
                disabled = check_target_disabled(target, account, candidates, accounts) if trigger else None
                if disabled and disabled[0] == "bundles":
                    unchecked, disabled = disabled[1], None
                    if (name in CLAUDE_AGENT_TOOLS or name == "spawn_agent") and target.get("model"):
                        native_unchecked = (account, target["model"])
                own = native_own_account(name, target, accounts) if trigger else None
                if own and own != account:
                    own_target = dict(target, account=own)
                    found = check_target_disabled(own_target, own, resolve_target(own_target, accounts)[1], accounts)
                    if found and found[0] == "bundles":
                        unchecked, native_unchecked = found[1], (own, target["model"])
                reason = (
                    (disabled[1] if disabled else None if trigger
                     else check_own_worker_legacy(target, account, accounts))
                    or check_account_quota_exhausted(
                        account or target.get("provider"),
                        usage_cache_path=usage_cache_path,
                        now=now,
                        accounts=accounts,
                        windows=target_windows(candidates, accounts),
                        bundles=candidates,
                        model=target.get("model"),
                        exclude_dispatch_id=dispatch_id or target.get("dispatch_id"),
                        exclude_agent_id=target.get("agent_id"),
                    )
                )
            except RouterTreeMismatch as exc:
                return format_response(False, harness_kind,
                    f"[Router Gate] 拦截：本钩子与已加载的 agent-compass 技能目录版本不一致（{exc}）。"
                    "请运行 `agentcompass setup` 使钩子与技能目录来自同一次安装后重试。")
            except ValueError as exc:
                if str(exc).startswith("invalid_model_pin:"):
                    return format_response(False, harness_kind,
                        f"[Router Gate] 拦截格式错误的模型参数 ({trigger or name})：{exc}")
                raise
            if reason:
                return format_response(False, harness_kind, reason)
        if not trigger:
            for agent_id in agent_ids:
                compaction_reason = check_worker_compaction_limit(
                    agent_id, paseo_agents_dir=paseo_agents_dir, identities=identities
                )
                if compaction_reason:
                    return format_response(False, harness_kind, compaction_reason)
            continue
        labels = args.get('labels')
        if isinstance(labels, dict) and any(k.startswith('router.') for k in labels):
            try:
                dispatch_registry = router_module('dispatch_registry')
                if unchecked and dispatch_registry.dispatch_constraint(labels.get('router.dispatch_id')) != 'user_override':
                    return format_response(False, harness_kind, unchecked)
                call_id = payload.get('tool_use_id') or payload.get('tool_call_id') or payload.get('call_id')
                dispatch_registry.claim(args, parent_id=caller_id, call_id=call_id, now=now)
            except (ValueError, OSError, ImportError, sqlite3.Error) as exc:
                return format_response(False, harness_kind, '[Router Gate] Bound dispatch rejected: ' + str(exc))
            continue
        if unchecked and (trigger == WORKFLOW_TRIGGER or trigger in SCHEDULE_TRIGGERS
                          or name == 'create_agent' or trigger == 'shell:paseo agent create'):
            return format_response(False, harness_kind, unchecked)
        # Workflow and schedules are not worker launches roll can make: each has its own authorization.
        if trigger == WORKFLOW_TRIGGER or trigger in SCHEDULE_TRIGGERS:
            if rows is None:
                rows = recent_routes(ledger, now=now, window_seconds=win)
            reason = (check_workflow_record(rows, caller_identities(payload, caller_id))
                      if trigger == WORKFLOW_TRIGGER else
                      check_schedule_plan(rows, args, correlation_fields(payload, tool_input, args), trigger, accounts,
                                          caller_identities(payload, caller_id)))
            if reason:
                return format_response(False, harness_kind, reason)
            continue
        if name == 'create_agent' or trigger == 'shell:paseo agent create':
            return format_response(False, harness_kind,
                f'[Router Gate] This Paseo host supports labels ({trigger}). Write the task brief to <task.md> and run '
                + ROUTER_CMD + ' roll --brief <task.md>; it routes and launches in one step. Or pass the returned '
                'create_agent object with router.* labels (title/initialPrompt/workspaceId may be edited; '
                'provider/settings/labels may not). An unbound ledger entry is not launch authorization.')
        if rows is None:
            rows = recent_routes(ledger, now=now, window_seconds=win)
        if not rows:
            return format_response(False, harness_kind, build_deny_reason(trigger, win))
        requested = normalize_launch(args)
        # Only Claude accounts and Dot are filled from shell text; other CLIs name a harness, not the routed provider.
        t_prov = str(targets[0].get("provider") or "")
        if not requested.get("provider") and (t_prov in ("claude", "dot") or t_prov.startswith("claude-")):
            requested["provider"] = t_prov
        context = correlation_fields(payload, tool_input, args)
        row, diagnostic = match_route(rows, requested, context, trigger, accounts=accounts)
        if row is not None and (reason := check_route_authority(row, trigger)):
            return format_response(False, harness_kind, reason)
        if row is not None and unchecked and row.get("constraint") != "user_override":
            return format_response(False, harness_kind,
                                   native_unchecked_text(*native_unchecked, row) if native_unchecked else unchecked)
        if row is None:
            reason = (f"[Router Gate] 启动参数与有效期内路由不匹配 ({trigger})：{diagnostic}。"
                      "请按本次任务的 router provider/model/effort 启动，或重新运行 roll。"
                      "台账匹配仅作兼容检查，不代表完整 dispatch 授权。")
            return format_response(False, harness_kind, reason)
    return format_response(True, harness_kind)


def read_payload(raw: str) -> tuple[dict | None, str]:
    """(payload, "") for a JSON object, else (None, what was wrong with the input)."""
    if not raw.strip():
        return None, "empty"
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        return None, type(exc).__name__
    return (payload, "") if isinstance(payload, dict) else (None, f"JSON {type(payload).__name__}, not an object")


def main() -> int:
    UNEVALUATED.clear()
    payload, unreadable = read_payload(sys.stdin.read())
    if payload is None:
        # No payload names a host and nothing to judge: allowed, as the gate always did, but the answer says the gate
        # got nothing it could read (a host whose payload changed would otherwise switch the gate off silently), in the
        # shape every host's allow defaults to.
        note_unevaluated("reading the hook's stdin", unreadable)
        result = format_response(True, "claude_or_codex")
    else:
        result = evaluate_payload(payload)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
