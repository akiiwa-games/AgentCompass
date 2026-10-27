"""Native PreToolUse hook that enforces calling route_judge.py before spawning subagents.

Supports Claude Code, Codex CLI, Antigravity IDE/CLI, and Pi hook payloads on stdin.

Dispatch-labelled Paseo launches consume a task-bound, one-use durable claim.
Unlabelled legacy calls retain ledger compatibility checks only: an unbound
ledger entry is never represented as dispatch authorization or a reservation.
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
from pathlib import Path

HOME = Path.home()
DEFAULT_LEDGER = HOME / ".route-manager" / "router-ledger.jsonl"
DEFAULT_USAGE_CACHE = HOME / ".usage-float" / "usage-cache.json"
DEFAULT_PASEO_AGENTS = HOME / ".paseo" / "agents"
DEFAULT_WINDOW_SECONDS = 900  # 15 minutes
QUOTA_MAX_AGE_SECONDS = 900

DIRECT_SUBAGENT_TOOLS = frozenset({
    "Agent",
    "Task",
    "dispatch_agent",
    "spawn_agent",
    "invoke_subagent",
    "create_agent",
    "mcp__paseo__create_agent",
})

PASEO_TURN_TOOLS = frozenset({
    "send_agent_prompt",
    "mcp__paseo__send_agent_prompt",
    "run_turn",
    "mcp__paseo__run_turn",
    "send_message",
    "mcp__paseo__send_message",
})

CODEMODE_TOOLS = frozenset({
    "codemode",
    "execute_code",
    "mcp__paseo__execute_code",
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

CODEMODE_TURN_AGENT_RE = re.compile(r"\b(?:mcp__paseo__)?(?:run_turn|send_message|send_agent_prompt)\s*\(")
WRAPPED_AGENT_CALL_RE = re.compile(
    r"\b((?:mcp__paseo__)?(?:create_agent|spawn_agent|run_turn|send_message|send_agent_prompt))\s*\("
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
    ("codex exec", re.compile(r"(?:^|[/\\])codex(?:\.cmd|\.exe)?\s+exec\b(?!.*--help)", re.I)),
)

READ_ONLY_CMD_PREFIX_RE = re.compile(
    r"^\s*(?:git\s+(?:commit|log|diff|show|status|grep)|rg|grep|Select-String|Get-Content|cat|head|tail|sed|awk|echo|printf|Write-Output|python(?:\.exe)?\s+(?:-\S+\s+)*-m\s+unittest)\b",
    re.I,
)


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
        (token, literal_call_args(tokens, i + 2))
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


def detect_subagent_launch(tool_name: str, tool_input: dict) -> str | None:
    """Return a short description of the subagent launch if this tool call spawns one, else None."""
    if not tool_name:
        return None
    if tool_name in DIRECT_SUBAGENT_TOOLS:
        return f"tool:{tool_name}"
    if tool_name in CODEMODE_TOOLS:
        if any(name in DIRECT_SUBAGENT_TOOLS for name, _ in wrapped_agent_calls(tool_name, tool_input)):
            return f"{tool_name}:create_agent"
        return None
    if tool_name in SHELL_TOOLS:
        if any(name in DIRECT_SUBAGENT_TOOLS for name, _ in wrapped_agent_calls(tool_name, tool_input)):
            return f"{tool_name}:create_agent"
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


def router_module(name: str):
    skill = Path(os.environ.get("ROUTER_SKILL_ROOT") or HOME / ".codex/skills/route-agent-work")
    if str(skill / "scripts") not in sys.path:
        sys.path.insert(0, str(skill / "scripts"))
    return importlib.import_module(name)


def check_worker_compaction_limit(
    agent_id: str | None,
    paseo_agents_dir: Path | None = None,
    home: Path | None = None,
    caller_id: str | None = None,
) -> str | None:
    """Return a block reason if the target Paseo worker has reached the carried-token rotation threshold (2M tokens)."""
    if not agent_id or not is_delegated_worker(agent_id, paseo_agents_dir=paseo_agents_dir, caller_id=caller_id):
        return None
    base = paseo_agents_dir or Path(os.environ.get("ROUTER_PASEO_AGENTS_DIR", DEFAULT_PASEO_AGENTS))
    home_dir = home or (base.parents[1] if base.name == "agents" and base.parent.name == ".paseo" else base.parent)
    try:
        allocator = router_module("allocator")
        policy = allocator.load_accounts().get("scoring") or {}
        max_tokens = int(policy.get("rotate_after_carried_tokens", 2_000_000))
        state = allocator.session_state(agent_id, home=home_dir)
    except Exception:
        return None
    compactions = state.get("compactions") or 0
    compacted_tok = state.get("compacted_tokens") or 0
    current_tok = state.get("context_tokens") or 0
    carried = allocator.worker_historical_tokens(state)
    if not (isinstance(carried, (int, float)) and carried >= max_tokens):
        return None
    return (
        f"[Router Gate] 拦截向已达历史累计上下文上限的旧 worker 续派任务 (agent_id={agent_id}, "
        f"historical_context_tokens={carried:,}/{max_tokens:,}, compactions={compactions})："
        f"该 worker 历史累计未压缩上下文达 {carried:,} tokens"
        f"（历次压缩前累计 {compacted_tok:,} + 当前窗口 {current_tok:,}，非压缩后当前窗口），继续追加 prompt 会导致上下文漂移与质量退化。"
        "禁止继续向该旧 worker 调用 run_turn / send_agent_prompt；请把当前进展与剩余工作写成交接任务书 <handoff.md>，"
        "运行 route_judge.py roll --brief <handoff.md> --hours <n> 由新 worker 接手。"
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
    for name, spec in table.items():
        for bundle_id, bundle in (spec.get("bundles") or {}).items():
            if target.get("bundle_id"):
                matched = bundle_id == target["bundle_id"]
            else:
                matched = bool(target.get("model")) and account in (None, name) and (
                    same_launch_value("model", target["model"], bundle.get("model"))
                    and target.get("effort") in (None, bundle.get("effort"))
                    and (not provider or (bundle.get("provider") == "pi") == (provider == "pi")))
            if matched:
                candidates.append(dict(bundle, account=name, bundle_id=bundle_id))
    if not account and len({c["account"] for c in candidates}) == 1:
        account = candidates[0]["account"]
    return account, [c for c in candidates if c["account"] == account]


DISABLED_RECOVERY = (
    "禁止启动该账号/档位的 worker，也禁止向它的旧 worker 继续调用 send_agent_prompt / run_turn；"
    "请把进展或任务写进 <task.md>，运行 python -X utf8 ~/.codex/skills/route-agent-work/scripts/route_judge.py "
    "roll --brief <task.md> --hours <n> 改派当前启用的账号（确需使用该账号时，先在 AgentCompass "
    "调度设置 -> 账号模型 中开启）。"
)


def check_target_disabled(
    target: dict, account: str | None, candidates: list[dict], accounts: dict | None, policy: dict | None,
) -> str | None:
    """Block a target the user turned off: host policy, account verified=false, or every matching bundle disabled."""
    if not account:
        return None
    where = f" (agent_id={target['agent_id']})" if target.get("agent_id") else ""
    if policy is not None and account not in (policy.get("allowed_accounts") or []):
        cause = f"账号 {account} 已在本机禁用（不在 host-policy.json 的 allowed_accounts 中）"
    elif ((accounts or {}).get("accounts") or {}).get(account, {}).get("verified") is False:
        cause = f"账号 {account} 已标记 verified: false（accounts-override.json / accounts.json）"
    elif candidates and all(c.get("disabled") for c in candidates):
        cause = "档位 " + ", ".join(f"{c['bundle_id']}（{c['disabled']}）" for c in candidates) + " 已停用"
    else:
        return None
    return f"[Router Gate] 拦截向已禁用账号/档位启动或续派任务{where}：{cause}。" + DISABLED_RECOVERY


def load_dispatch_settings() -> tuple[dict | None, dict | None]:
    """(accounts, host policy); unreadable configuration degrades to allow-all."""
    try:
        accounts = router_module("allocator").load_accounts()
    except Exception:
        accounts = None
    try:
        policy = router_module("host_policy").load_policy()
    except Exception:
        policy = None
    return accounts, policy if isinstance(policy, dict) and isinstance(policy.get("allowed_accounts"), list) else None


def quota_window_seconds(wid: str) -> int:
    return 5 * 3600 if wid in ("five_hour", "5h") else 7 * 86400


def check_account_quota_exhausted(
    account: str | None,
    usage_cache_path: Path | None = None,
    now: dt.datetime | None = None,
    accounts: dict | None = None,
) -> str | None:
    """Return a block reason if account's active quota window is exhausted in usage-cache.json."""
    if not account:
        return None
    cache = usage_cache_path or Path(os.environ.get("ROUTER_USAGE_CACHE", DEFAULT_USAGE_CACHE))
    if not cache.is_file():
        return None
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        doc = json.loads(cache.read_text(encoding="utf-8-sig"))
        row = ((doc.get("providers") or {}).get(account)) if isinstance(doc, dict) else None
    except (OSError, ValueError):
        return None
    if not isinstance(row, dict) or row.get("available") is False:
        return None
    fetched_at = parse_ts(row.get("fetched_at"))
    if fetched_at is None or (fetched_at - now).total_seconds() > 60:
        return None
    acc_spec = ((accounts or {}).get("accounts") or {}).get(account) or {}
    raw_windows = acc_spec.get("windows")
    if isinstance(raw_windows, dict):
        win_specs = {str(k): v for k, v in raw_windows.items() if isinstance(v, dict)}
    else:
        win_specs = {
            str(ws.get("id") or ""): ws
            for ws in (raw_windows or [])
            if isinstance(ws, dict) and ws.get("id")
        }
    for w in row.get("windows") or []:
        if not isinstance(w, dict):
            continue
        wid = str(w.get("id") or "")
        if wid in ("weekly_fable", "seven_day_fable", "weekly_opus", "seven_day_opus"):
            continue
        resets_at = parse_ts(w.get("resets_at"))
        if resets_at is None or resets_at <= now:
            continue
        used = w.get("used_pct")
        if not isinstance(used, (int, float)) or isinstance(used, bool):
            continue
        # Usage only grows until resets_at: a stale or old reading taken inside the
        # current cycle still holds; only a reading from a previous cycle is void.
        cycle_start = resets_at - dt.timedelta(seconds=quota_window_seconds(wid))
        if fetched_at < cycle_start and (now - fetched_at).total_seconds() > QUOTA_MAX_AGE_SECONDS:
            continue
        rem = round(100.0 - float(used), 1)
        c_wid = "five_hour" if wid in ("five_hour", "5h", "usage-300m") else ("usage" if wid == "seven_day" and "usage" in win_specs else wid)
        w_spec = win_specs.get(wid) or win_specs.get(c_wid) or {}
        min_rem = w_spec.get("launch_min_remaining_pct")
        if min_rem is None and account.startswith("claude-") and c_wid == "five_hour":
            min_rem = 25.0
        if isinstance(min_rem, (int, float)) and used >= round(100.0 - float(min_rem), 4):
            max_used = int(round(100.0 - float(min_rem)))
            return (
                f"[Router Gate] 拦截向已满额/低余量账号 {account} 派发或续跑任务："
                f"{account} 当前 {wid} 已用 {used}%（仅剩 {rem}%，重置时间 {w.get('resets_at')}），"
                f"窗口已触顶或进入保护线（>={max_used}%）。"
                f"禁止继续创建或调用 {account} worker，请把进展或任务写进 <task.md>，运行 route_judge.py roll --brief <task.md> --hours <n> 改派其他可用账号（如 codex / claude / dot / pi）。"
            )
        threshold = 95.0 if account.startswith("claude-") else 98.0
        if used >= threshold:
            return (
                f"[Router Gate] 拦截向已满额账号 {account} 派发或续跑任务："
                f"{account} 当前 {wid} 已用 {used}%（仅剩 {rem}%，重置时间 {w.get('resets_at')}）。"
                f"禁止继续调用 {account}，请把进展或任务写进 <task.md>，运行 route_judge.py roll --brief <task.md> --hours <n> 改派其他可用账号。"
            )
    return None


def is_dot_route(row: dict | None) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get("choice") == "dot_text":
        return True
    bundle = row.get("bundle")
    return isinstance(bundle, dict) and bundle.get("provider") == "dot"


def secondary_claude_provider(row: dict | None) -> str | None:
    if not isinstance(row, dict):
        return None
    for key in ("bundle", "selected_bundle"):
        b = row.get(key)
        if isinstance(b, dict):
            for field in ("account", "provider"):
                val = str(b.get(field) or "")
                if val.startswith("claude-"):
                    return val
    choice = str(row.get("choice") or "")
    if choice in ("sonnet55_high_2", "opus55_high_2", "opus55_xhigh_2"):
        return "claude-2"
    m = re.match(r"^(?:sonnet55|opus55)_(?:high|xhigh)_(.+)$", choice)
    if m:
        suf = m.group(1)
        return "claude-2" if suf == "2" else (suf if suf.startswith("claude-") else f"claude-{suf}")
    return None


def recent_routes(
    ledger_path: Path,
    now: dt.datetime | None = None,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
) -> list[dict]:
    """Read recent decisions newest first, bounded to the ledger's last 64 KiB."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if not ledger_path.is_file():
        return []
    try:
        size = ledger_path.stat().st_size
        with ledger_path.open("rb") as fh:
            if size > 65536:
                fh.seek(size - 65536)
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


def route_spec(row: dict) -> dict[str, str]:
    bundle = row.get("bundle") or row.get("selected_bundle") or {}
    spec = normalize_launch(bundle if isinstance(bundle, dict) else {})
    if not spec.get("provider"):
        sec_claude = secondary_claude_provider(row)
        if is_dot_route(row):
            spec["provider"] = "dot"
        elif sec_claude:
            spec["provider"] = sec_claude
    return spec


# Claude's Agent/Task tool takes only a family alias and runs the host's current
# model of that family; the router routes one model per Claude family.
CLAUDE_MODEL_FAMILIES = frozenset({"sonnet", "opus", "haiku", "fable"})


def same_launch_value(key: str, requested: str, routed: str | None) -> bool:
    """Compare what will run: a bare Claude family alias is that family's model."""
    if key == "model" and requested in CLAUDE_MODEL_FAMILIES:
        return bool(routed) and routed.startswith(f"claude-{requested}-")
    return requested == routed


def match_route(rows: list[dict], requested: dict, context: dict, trigger: str) -> tuple[dict | None, str]:
    """Use available correlation, then compare launch fields, without inventing IDs.

    A newer decision for the *same identified request* supersedes its old one.
    Without identity, search all compatible rows in the TTL (legacy callers).
    Missing identity is not proof of ownership: a dispatch_id lifecycle still
    needs caller/ledger support, launch acknowledgement and terminal release.
    """
    candidates = []
    for row in rows:
        identity = correlation_fields(row, row.get("task") if isinstance(row.get("task"), dict) else {})
        shared = context.keys() & identity.keys()
        if any(context[k] != identity[k] for k in shared):
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
        chosen = route_spec(row)
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
        f"最近 {minutes} 分钟内 ~/.route-manager/router-ledger.jsonl 中无有效的 plan/select/record 记录。"
        "把任务书写进 <task.md>，一条命令完成分类、选路与启动：\n"
        "  python -X utf8 ~/.codex/skills/route-agent-work/scripts/route_judge.py roll --brief <task.md> --hours <n>\n"
        "（用户在本次请求中点名了 model/effort 时追加 --provider <p> --account <a> --model <m> --effort <e> "
        "--basis \"<用户原话>\"；宿主没有自动启动器时 roll 会返回 route.bundle，按它原生启动。）"
    )


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
    if os.environ.get("ROUTER_GATE_BYPASS") == "1":
        return format_response(True, harness_kind)

    caller_id = str(payload.get("parent_agent_id") or payload.get("callerAgentId")
                    or os.environ.get("PASEO_AGENT_ID") or "").strip() or None

    calls = wrapped_agent_calls(tool_name, tool_input) or [(tool_name, tool_input)]
    if any(re.split(r'__|\.', name)[-1] in WORKER_QUESTION_TOOLS for name, _ in calls):
        worker = os.environ.get('ROUTER_WORKER') == '1' or (
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
            targets = [dict(target_spec(doc, agent_labels(doc)), agent_id=agent_id)
                       for agent_id, doc in docs.items() if isinstance(doc, dict)]
        for target in targets:
            account, candidates = resolve_target(target, accounts)
            reason = (check_target_disabled(target, account, candidates, accounts, policy) or
                      check_account_quota_exhausted(account or target.get("provider"),
                                                    usage_cache_path=usage_cache_path, now=now, accounts=accounts))
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
        if (name in ('create_agent', 'mcp__paseo__create_agent') or trigger == 'shell:paseo agent create') and os.environ.get('ROUTER_PASEO_LEGACY_NO_LABELS') != '1':
            return format_response(False, harness_kind,
                '[Router Gate] This Paseo host supports labels. Write the task brief to <task.md> and run '
                'route_judge.py roll --brief <task.md> --hours <n>; it routes and launches in one step. Or pass the returned '
                'create_agent object with router.* labels (title/initialPrompt/workspaceId may be edited; '
                'provider/settings/labels may not). An unbound legacy ledger is not launch authorization.')
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
        row, diagnostic = match_route(rows, requested, context, trigger)
        if row is None:
            reason = (f"[Router Gate] 启动参数与有效期内路由不匹配 ({trigger})：{diagnostic}。"
                      "请按本次任务的 router provider/model/effort 启动，或重新运行 route_judge.py plan。"
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
