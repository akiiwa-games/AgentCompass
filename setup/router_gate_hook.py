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

# Match actual worker-spawning CLI invocations at the start of a command segment.
# A command segment ends at an unquoted ;, &&, ||, | or newline; quoted separators are argument text.
SHELL_SEGMENT_RE = re.compile(r"""(?:"(?:\\.|[^"\\])*"?|'[^']*'?|&(?!&)|[^;|&\r\n"'])+""")
LEADING_WRAPPERS_RE = re.compile(
    r"^\s*(?:(?:env|[A-Za-z_][A-Za-z0-9_]*=(?:\"[^\"]*\"|'[^']*'|\S+))\s+)*"
    r"(?:(?:&|call|start|nohup|bash|sh|zsh|pwsh|powershell(?:\.exe)?|cmd(?:\.exe)?\s+/c)\s+)*"
    r"(?:-\w+\s+)*",
    re.I,
)

SHELL_SPAWN_PATTERNS = (
    ("paseo agent create", re.compile(r"\bpaseo(?:\.cmd|\.exe)?\s+(?:agent\s+(?:create|start|spawn|run)|create_agent|run)\b(?!.*--help)", re.I)),
    ("run_dot_task.py", re.compile(r"\b(?:run_dot_task\.py|dot-subagent(?:\.cmd)?(?![/\\]))\b(?!.*(?:--help|-h|--check-only|\bdoctor\b|\bstatus\b|--task-id\b))", re.I)),
    ("orca worker launch", re.compile(r"(?:^|[/\\])orca(?:\.cmd|\.exe)?\s+(?:orchestration\s+worker-start|agent\s+launch)\b(?!.*--help)", re.I)),
    ("codex exec", re.compile(r"(?:^|[/\\])codex(?:\.cmd|\.exe)?\s+exec\b(?!.*--help)", re.I)),
)

READ_ONLY_CMD_PREFIX_RE = re.compile(
    r"^\s*(?:git\s+(?:commit|log|diff|show|status|grep)|rg|grep|Select-String|Get-Content|cat|head|tail|sed|awk|echo|printf|Write-Output|python(?:\.exe)?\s+(?:-\S+\s+)*-m\s+unittest)\b",
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


def extract_tool_call(payload: dict) -> tuple[str, dict, str]:
    """Return (tool_name, tool_input, harness_kind) from a hook stdin payload."""
    if "toolCall" in payload or "workspaceRoots" in payload:
        tc = payload.get("toolCall") or {}
        name = str(tc.get("name") or "")
        args = normalize_input(tc.get("args"))
        return name, args, "antigravity"
    if "toolName" in payload and "tool_name" not in payload:
        name = str(payload.get("toolName") or "")
        args = normalize_input(payload.get("input"))
        return name, args, "pi"
    name = str(payload.get("tool_name") or "")
    args = normalize_input(payload.get("tool_input"))
    return name, args, "claude_or_codex"


def extract_shell_commands(tool_input: dict) -> list[str]:
    direct = (
        tool_input.get("command")
        or tool_input.get("cmd")
        or tool_input.get("CommandLine")
    )
    if direct:
        return [str(direct)]
    raw = str(tool_input.get("_raw") or tool_input.get("code") or "")
    if not raw:
        return []
    extracted = []
    for match in CODEX_EXEC_CMD_RE.finditer(raw):
        try:
            extracted.append(json.loads(match.group(1)))
        except ValueError:
            extracted.append(match.group(1))
    return extracted or [raw]


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
PASEO_EXES = frozenset({"paseo", "paseo.cmd", "paseo.exe", "paseo.ps1"})
PASEO_VALUE_OPTIONS = frozenset({"-o", "--format", "--host", "--home"})
PASEO_GROUP_COMMANDS = frozenset({"agent", "schedule"})
# Commands that create and start an agent. A schedule launch is judged as the MCP schedule tools.
PASEO_LAUNCH_COMMANDS = frozenset({("run",), ("agent", "run"), ("agent", "create"), ("agent", "start"),
                                   ("agent", "spawn"), ("create_agent",)})
PASEO_SCHEDULE_COMMANDS = {("schedule", "create"): "create_schedule", ("schedule", "update"): "update_schedule"}
PASEO_CLI_LAUNCH = "paseo_cli_launch"   # a cli_turn_calls entry for PASEO_LAUNCH_COMMANDS; judged on the shell call
# Programs that run another command line: shells take it after -c / -Command / /c, package runners as their argv.
SHELL_PROGRAMS = frozenset({"bash", "sh", "zsh", "dash", "pwsh", "powershell", "cmd"})
SHELL_COMMAND_FLAGS = frozenset({"-c", "-command", "/c", "/k"})
PACKAGE_RUNNERS = {"npx": (), "bunx": (), "pnpm": ("dlx", "exec"), "yarn": ("dlx", "exec")}
START_PROCESS = frozenset({"start-process", "saps"})


def program_name(word: str) -> str:
    """`C:\\x\\Paseo.CMD` -> `paseo.cmd`; the program a command word runs, lower-cased."""
    return word.replace("\\", "/").rsplit("/", 1)[-1].lower()


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
    """Normalize static Paseo CLI sends and schedule launches; never execute shell or Python code.

    Only command positions and subprocess arguments count, not quoted prompt
    text. Unknown/dynamic expressions retain the existing fail-open behavior.
    """
    if tool_name not in SHELL_TOOLS | CODEMODE_TOOLS:
        return []
    targets = []

    def argv(words: list, depth: int):
        if not words or not isinstance(words[0], str) or depth > 4:
            return
        if re.search(r"\s", program_name(words[0].strip())):
            # A quoted command line left by a stripped wrapper (`bash -c "..."`), not a program path with spaces
            # (`C:\Program Files\Paseo\paseo.cmd`).
            shell(words[0], depth + 1)
            return
        exe = program_name(words[0])
        stem = exe.rsplit(".", 1)[0] if exe.endswith((".exe", ".cmd", ".ps1")) else exe
        if exe in PASEO_EXES:
            paseo(words[1:])
        elif stem in SHELL_PROGRAMS:
            flags = [i for i, w in enumerate(words) if isinstance(w, str) and w.lower() in SHELL_COMMAND_FLAGS]
            if flags:
                rest = [w for w in words[flags[0] + 1:] if isinstance(w, str)]
                shell(rest[0] if len(rest) == 1 else " ".join(rest), depth + 1)
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
            start_process(words[1:], depth)
        elif stem in ('python', 'python3', 'py') and '-c' in words:
            index = words.index('-c') + 1
            if index < len(words) and isinstance(words[index], str):
                python(words[index], depth + 1)

    def paseo(args: list):
        if '--help' in args or '-h' in args:
            return
        parsed = paseo_command(args)
        if parsed is None:
            return
        path, rest = parsed
        if path in PASEO_SCHEDULE_COMMANDS:
            # Same launch as MCP create_schedule / update_schedule: the provider settings become its arguments.
            targets.append((PASEO_SCHEDULE_COMMANDS[path], paseo_cli_options(rest)))
        elif path in PASEO_LAUNCH_COMMANDS:
            targets.append((PASEO_CLI_LAUNCH, paseo_cli_options(rest)))
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
        items = []
        for match in re.finditer(r'"([^"]*)"|\'([^\']*)\'|([^\s,]+)', " ".join(listed)):
            item = next(group for group in match.groups() if group is not None)
            items.extend(item.split() if match.group(3) is None and re.search(r'\s', item) else [item])
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
                    shell(value, depth + 1)

    def shell(command: str, depth: int = 0):
        if depth > 4:
            return
        # Also accepts a Python script supplied directly as a wrapper's code.
        python(command, depth)
        for segment in SHELL_SEGMENT_RE.findall(command):
            segment = LEADING_WRAPPERS_RE.sub('', segment.strip())
            if READ_ONLY_CMD_PREFIX_RE.match(segment):
                continue
            words = re.findall(r'''"(?:\\.|[^"\\])*"|'[^']*'|[^\s]+''', segment)
            argv([word[1:-1] if len(word) >= 2 and word[0] == word[-1]
                  and word[0] in ('"', "'") else word for word in words], depth)

    for command in extract_shell_commands(tool_input):
        shell(command)
    return targets


def detect_subagent_launch(tool_name: str, tool_input: dict) -> str | None:
    """Return a short description of the subagent launch if this tool call spawns one, else None."""
    tool_name = base_tool_name(tool_name)
    if not tool_name:
        return None
    if tool_name in DIRECT_SUBAGENT_TOOLS:
        return f"tool:{tool_name}"
    if tool_name in SCHEDULE_RETARGET_TOOLS:
        return f"tool:{tool_name}" if any(tool_input.get(k) for k in SCHEDULE_TARGET_FIELDS) else None
    if tool_name in CODEMODE_TOOLS:
        if any(name in DIRECT_SUBAGENT_TOOLS for name, _ in wrapped_agent_calls(tool_name, tool_input)):
            return f"{tool_name}:create_agent"
        return None
    if tool_name in SHELL_TOOLS:
        if any(name in DIRECT_SUBAGENT_TOOLS for name, _ in wrapped_agent_calls(tool_name, tool_input)):
            return f"{tool_name}:create_agent"
        if any(name == PASEO_CLI_LAUNCH for name, _ in cli_turn_calls(tool_name, tool_input)):
            return "shell:paseo agent create"
        for command in extract_shell_commands(tool_input):
            if not command:
                continue
            for raw_segment in SHELL_SEGMENT_RE.findall(command):
                segment = raw_segment.strip()
                if not segment or READ_ONLY_CMD_PREFIX_RE.match(segment):
                    continue
                stripped = LEADING_WRAPPERS_RE.sub("", segment)
                if READ_ONLY_CMD_PREFIX_RE.match(stripped):
                    continue
                for label, pattern in SHELL_SPAWN_PATTERNS:
                    if pattern.search(stripped):
                        return f"shell:{label}"
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


def owns_worker(doc: object, agent_id: str, caller_id: str | None) -> bool:
    """Only unambiguous persisted parent labels prove ownership."""
    labels = agent_labels(doc)
    parents = [labels.get(key) for key in ("router.parent_id", "paseo.parent-agent-id") if labels.get(key)]
    return bool(caller_id and agent_id != caller_id and parents and all(parent == caller_id for parent in parents))


def owns_running_worker(doc: object, agent_id: str, caller_id: str | None) -> bool:
    """A parent can control an existing turn; unknown state cannot authorize one."""
    return owns_worker(doc, agent_id, caller_id) and doc.get("status", doc.get("lastStatus")) == "running"


def caller_parent_ids(caller_id: str | None, paseo_agents_dir: Path | None = None) -> set[str]:
    """IDs a worker reports up to; a turn sent to one of them is never a dispatch."""
    labels = agent_labels(load_paseo_agent(caller_id, paseo_agents_dir)) if caller_id else {}
    return {v for v in (labels.get("router.parent_id"), labels.get("paseo.parent-agent-id")) if v and v != caller_id}


def is_delegated_worker(
    agent_id: str,
    paseo_agents_dir: Path | None = None,
    caller_id: str | None = None,
) -> bool:
    doc = load_paseo_agent(agent_id, paseo_agents_dir)
    if not doc:
        return False
    labels = agent_labels(doc)
    has_router = any(k.startswith("router.") for k in labels)
    target_parent = labels.get("router.parent_id") or labels.get("paseo.parent-agent-id")
    if not (has_router or target_parent):
        return False
    if not has_router and caller_id and target_parent and caller_id != target_parent:
        return False
    return True


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
    caller_id: str | None = None,
) -> str | None:
    """Block a new turn at the configured historical-token rotation threshold, preserving parent control of an active turn."""
    if not agent_id or not is_delegated_worker(agent_id, paseo_agents_dir=paseo_agents_dir, caller_id=caller_id):
        return None
    if owns_running_worker(load_paseo_agent(agent_id, paseo_agents_dir), agent_id, caller_id):
        return None
    base = paseo_agents_dir or Path(os.environ.get("ROUTER_PASEO_AGENTS_DIR", DEFAULT_PASEO_AGENTS))
    home_dir = home or (base.parents[1] if base.name == "agents" and base.parent.name == ".paseo" else base.parent)
    try:
        account_fleet = router_module("account_fleet")
        worker_sessions = router_module("worker_sessions")
    except ImportError:
        return None
    max_tokens = worker_sessions.rotation_threshold(account_fleet.load_accounts())
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


def resolve_target(target: dict, accounts: dict | None) -> tuple[str | None, list[dict]]:
    """Owning account and the bundles the target can be, by bundle_id or by model/effort/Pi harness."""
    table = (accounts or {}).get("accounts") or {}
    provider = target.get("provider")
    account = target.get("account") or (
        "antigravity" if provider == "antigravity-resilient" else provider if provider in table else None)
    candidates = []
    if target.get("bundle_id") and table:
        candidates = router_owner("account_fleet", "bundle_rows")(accounts, target["bundle_id"])
    elif not target.get("bundle_id"):
        for name, spec in table.items():
            for bundle_id, bundle in (spec.get("bundles") or {}).items():
                if bool(target.get("model")) and account in (None, name) and (
                        same_launch_value("model", target["model"], bundle.get("model"))
                        and target.get("effort") in (None, bundle.get("effort"))
                        and (not provider or (bundle.get("provider") == "pi") == (provider == "pi"))):
                    candidates.append(dict(bundle, account=name, bundle_id=bundle_id))
    if not account and len({c["account"] for c in candidates}) == 1:
        account = candidates[0]["account"]
    return account, [c for c in candidates if c["account"] == account]


DISABLED_RECOVERY = (
    "禁止启动该账号/档位的 worker 或向 idle/状态未知的旧 worker 开启新 turn；"
    "父会话仍可向自己明确运行中的 worker 发控制消息以收尾。否则请收取已有输出，必要时调用 cancel_agent，"
    f"把进展或任务写进 <task.md>，运行 {ROUTER_CMD} "
    "roll --brief <task.md> 改派当前启用的账号（确需使用该账号时，先在 AgentCompass "
    "调度设置 -> 账号模型 中开启）。"
)


def check_target_disabled(
    target: dict, account: str | None, candidates: list[dict], accounts: dict | None, policy: dict | None,
) -> str | None:
    """Block a target with leftover meter fields, disabled by host policy, or with every matching bundle disabled."""
    if not account:
        return None
    where = f" (agent_id={target['agent_id']})" if target.get("agent_id") else ""
    disabled_cause, disabled_text = router_owner("host_policy", "disabled_cause", "disabled_text")
    found = disabled_cause(account, candidates, accounts, policy)
    if not found:
        return None
    cause = disabled_text(account, found, "zh")
    return f"[Router Gate] 拦截向已禁用账号/档位启动或续派任务{where}：{cause}。" + DISABLED_RECOVERY


def load_dispatch_settings() -> tuple[dict | None, dict | None]:
    """(accounts, host policy); unreadable configuration degrades to allow-all."""
    try:
        accounts = router_module("account_fleet").load_accounts()
    except Exception:
        accounts = None
    try:
        policy = router_module("host_policy").load_policy()
    except Exception:
        policy = None
    return accounts, policy if isinstance(policy, dict) and isinstance(policy.get("allowed_accounts"), list) else None


def target_windows(candidates: list[dict], accounts: dict | None) -> list[str] | None:
    """Quota windows the resolved target bundles draw; None when unknown."""
    if not candidates or not accounts:
        return None
    try:
        pools = router_module("quota_pools").bundle_pools
        return sorted({wid for bundle in candidates for wid in pools(dict(bundle, id=bundle["bundle_id"]), accounts)})
    except Exception:
        return None


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
    quota_pools.launch_blockers on route_judge.usage_snapshot, the same rules and reading routing uses:
    the launch floor, a window used up inside its cycle, an empty short window, active in-flight
    reservations, and a scope from data only. `windows` are the quota windows the launch draws, for the
    confirmed-exhaustion latch (an account-wide latch blocks every launch). When the router modules cannot
    be loaded only that latch is checked and the gate otherwise allows: it never invents quota rules of its own.
    """
    if not account:
        return None
    now = now or dt.datetime.now(dt.timezone.utc)
    latched, without_own, window_refusal = router_owner("turn_refusal", "latched", "without_own", "window_refusal")
    # Confirmed upstream exhaustion is durable even with no/stale usage cache.
    # This applies to new launches and resumed turns, never to read-only investigation.
    if latched(account, windows, usage_cache_path, HOME):
        return (
            '[Router Gate] Account quota was confirmed exhausted; refresh quota after recovery or explicitly clear '
            'the exhaustion record. While the owned worker is running, send a control message with completion '
            f'notifications enabled; otherwise collect existing output, cancel_agent if needed, and run {ROUTER_CMD} roll --brief <task.md>.'
        )
    try:
        accounts = accounts if accounts is not None else router_module("account_fleet").load_accounts()
        if records is None:
            try:
                registry = router_module("dispatch_registry")
                records = registry.reservations(now=now, read_only=True)
            except Exception:
                records = ()
            # A watcher that died without sending still owes the parent one end notice. A failure here
            # must not become a launch denial, and a second read does not send it again.
            try:
                router_module("dispatch_flow").deliver_end_notices(now=now)
            except Exception:
                pass
        records = without_own(records, exclude_dispatch_id, exclude_agent_id)
        found = window_refusal(account, accounts, now, usage_cache_path, records, bundles, model)
    except Exception:
        return None
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


def route_spec(row: dict, accounts: dict | None = None) -> dict[str, str]:
    choice = row.get("choice")
    catalog_bundle = {}
    if isinstance(choice, str) and choice and isinstance(accounts, dict):
        acct, matches = resolve_target({"bundle_id": choice}, accounts)
        if matches:
            catalog_bundle = dict(matches[0])
            if acct and not catalog_bundle.get("account"):
                catalog_bundle["account"] = acct
    raw_bundle = row.get("bundle") or row.get("selected_bundle") or {}
    merged = {
        **catalog_bundle,
        **(raw_bundle if isinstance(raw_bundle, dict) else {}),
    }
    for key in ("provider", "account", "model", "effort"):
        val = row.get(key)
        if isinstance(val, str) and val and not merged.get(key):
            merged[key] = val
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
    """A Workflow passes only on this session's `record --constraint user_override --trigger workflow`."""
    if not identities:
        return workflow_deny_reason("hook 输入里没有会话 ID，无法确认授权记录属于本会话")
    for row in rows:
        if (row.get("stage") == "record" and row.get("constraint") == "user_override"
                and row.get("trigger") == "workflow" and written_by(row, identities)):
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
    if harness_kind == "antigravity":
        return {"decision": "allow"} if allowed else {"decision": "deny", "reason": reason}
    if harness_kind == "pi":
        return {"block": False} if allowed else {"block": True, "reason": reason}
    if allowed:
        return {}
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
    tool_name, tool_input, harness_kind = extract_tool_call(payload)
    tool_name = base_tool_name(tool_name)
    caller_id = str(payload.get("parent_agent_id") or payload.get("callerAgentId")
                    or os.environ.get("PASEO_AGENT_ID") or "").strip() or None

    calls = wrapped_agent_calls(tool_name, tool_input) or [(tool_name, tool_input)]
    calls.extend(cli_turn_calls(tool_name, tool_input))

    if os.environ.get("ROUTER_GATE_BYPASS") == "1":
        return format_response(True, harness_kind)
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
            calls.extend(("exec_command", {"cmd": command}) for command in extract_shell_commands(tool_input))
    executing = []
    for name, args in calls:
        if (name == "send_agent_prompt"
                and caller_id and args.get("notifyOnFinish") is False):
            for agent_id in detect_target_agent_ids(name, args):
                # Only persisted ownership proves direction; absent/conflicting
                # labels or caller identity leave this notification check open.
                if owns_worker(load_paseo_agent(agent_id, paseo_agents_dir), agent_id, caller_id):
                    return format_response(False, harness_kind,
                        '[Router Gate] Parent-to-worker send_agent_prompt must keep completion '
                        'notifications enabled. Set notifyOnFinish: true or omit it; completion '
                        'notifications and the final reply are the result channel for workers '
                        'without messaging tools (e.g. agy). Worker-to-parent questions and final reports may use false.')
        trigger = detect_subagent_launch(name, args)
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
    accounts, policy = load_dispatch_settings()
    upward = caller_parent_ids(caller_id, paseo_agents_dir)
    for name, args, trigger in executing:
        if trigger:
            targets = [launch_target(name, args, trigger)]
        else:
            agent_ids = [agent_id for agent_id in detect_target_agent_ids(name, args) or
                         detect_target_agent_ids(tool_name, tool_input) if agent_id not in upward]
            docs = {agent_id: load_paseo_agent(agent_id, paseo_agents_dir) for agent_id in agent_ids}
            targets = [dict(target_spec(doc, agent_labels(doc)), agent_id=agent_id,
                            dispatch_id=agent_labels(doc).get("router.dispatch_id"))
                       for agent_id, doc in docs.items() if isinstance(doc, dict)]
        dispatch_id = args.get("labels", {}).get("router.dispatch_id") if isinstance(args.get("labels"), dict) else None
        for target in targets:
            try:
                account, candidates = resolve_target(target, accounts)
                controlling = not trigger and owns_running_worker(docs[target["agent_id"]], target["agent_id"], caller_id)
                reason = None if controlling else (
                    check_target_disabled(target, account, candidates, accounts, policy)
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
            if reason:
                return format_response(False, harness_kind, reason)
        if not trigger:
            for agent_id in agent_ids:
                compaction_reason = check_worker_compaction_limit(
                    agent_id, paseo_agents_dir=paseo_agents_dir, caller_id=caller_id
                )
                if compaction_reason:
                    return format_response(False, harness_kind, compaction_reason)
            continue
        labels = args.get('labels')
        if isinstance(labels, dict) and any(k.startswith('router.') for k in labels):
            try:
                dispatch_registry = router_module('dispatch_registry')
                call_id = payload.get('tool_use_id') or payload.get('tool_call_id') or payload.get('call_id')
                dispatch_registry.claim(args, parent_id=caller_id, call_id=call_id, now=now)
            except (ValueError, OSError, ImportError, sqlite3.Error) as exc:
                return format_response(False, harness_kind, '[Router Gate] Bound dispatch rejected: ' + str(exc))
            continue
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
        if row is None:
            reason = (f"[Router Gate] 启动参数与有效期内路由不匹配 ({trigger})：{diagnostic}。"
                      "请按本次任务的 router provider/model/effort 启动，或重新运行 roll。"
                      "台账匹配仅作兼容检查，不代表完整 dispatch 授权。")
            return format_response(False, harness_kind, reason)
    return format_response(True, harness_kind)


def main() -> int:
    raw = sys.stdin.read()
    if not raw or not raw.strip():
        print("{}")
        return 0
    try:
        payload = json.loads(raw)
    except ValueError:
        print("{}")
        return 0
    if not isinstance(payload, dict):
        print("{}")
        return 0
    result = evaluate_payload(payload)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
