"""Daily router inspection. Read-only; spends no model tokens.

Rebuilds every routing decision (ledger first, then session transcripts for history), joins it with the
usage-float cache, Codex/Claude token logs and the dot2api task table, applies fixed rules, and writes a
Markdown report. Thresholds are the named constants below, not measured limits.

    python audit/router_audit.py [--days 7] [--detail-days 2] [--commentary FILE] [--no-write]
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import glob
import importlib.util
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

HOME = Path.home()
SKILL_SCRIPTS = Path(os.environ.get("ROUTER_SKILL_DIR", HOME / ".codex" / "skills" / "agent-compass" / "scripts"))
LEDGER = Path(os.environ.get("ROUTER_LEDGER", HOME / ".agent-compass" / "router-ledger.jsonl"))
DOT_DB = HOME / ".dot2api" / "data" / "dot2api.sqlite3"
REPORT_DIR = Path(__file__).resolve().parent / "reports"
UTC = dt.timezone.utc

GATE_MIN_SAMPLES = 20      # delegate verdicts needed before the gate's discrimination is judged
GATE_YES_SHARE = 0.95      # a gate that says yes this often filters nothing
PLAN_SHARE = 0.99          # target share of worker dispatches chosen by `plan` rather than pinned by hand
PLAN_MIN_SAMPLES = 10      # dispatches needed before that share is judged
FIXED_SHARE = 0.80         # selects whose route was fixed by instruction or by the specialist table
LOW_REMAINING_PCT = 20
STALE_FLAG_SECONDS = 24 * 3600
HEAVY_SESSION_CALLS = 500  # calls in one local day for a session to count as heavy
HEAVY_SESSIONS_PER_DAY = 4
BLOAT_CONTEXT_TOKENS = 100_000
LONG_SESSION_CALLS = 1000
POLL_OUTPUT_PER_CALL = 300
PRO_BURST = 5              # Pro web dispatches inside PRO_WINDOW_HOURS
PRO_WINDOW_HOURS = 12
PAIR_GAP_SECONDS = 15 * 60
MARKERS = ("subscription_model_judgment", "judge_unavailable", "local_candidate_filter", "constraint_record")
FIXED_WORDS = re.compile(r"explicit|required|retained|sole.?owner|authorized|honors|user-?named|指定|明确|授权", re.I)
METERED = {"api-proxy"}
CAPACITY = Path(os.environ.get("ROUTER_CAPACITY", HOME / ".agent-compass" / "capacity.json"))
USAGE_STATS = Path(os.environ.get("ROUTER_USAGE_STATS", HOME / ".agent-compass" / "usage-stats.json"))
CAPACITY_STALE_HOURS = 2   # the tray service rewrites capacity.json every five minutes
UNROUTED_WINDOW_SECONDS = 15 * 60
ROUTER_RULE_SINCE = dt.datetime(2026, 10, 10, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))


def parse_ts(value):
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def local(ts: dt.datetime) -> dt.datetime:
    return ts.astimezone()


def load_router():
    spec = importlib.util.spec_from_file_location("route_judge", SKILL_SCRIPTS / "route_judge.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- decisions

def iter_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from iter_strings(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from iter_strings(value)


def find_results(text: str):
    decoder = json.JSONDecoder()
    for match in re.finditer(r'\{\s*"stage"\s*:', text):
        try:
            obj, _ = decoder.raw_decode(text[match.start():])
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("stage") in ("delegate", "select") and "source" in obj:
            yield obj


def usage_points(usage: dict) -> dict:
    points = {}
    for account, row in (usage.get("accounts") or {}).items():
        points[account] = {"fetched_at": row.get("fetched_at"), "status": row.get("status"),
                           "windows": [{"id": w.get("id"), "used_pct": w.get("used_pct"), "resets_at": w.get("resets_at")}
                                       for w in row.get("windows") or []]}
    return points


def from_transcripts(files):
    found = {}
    for path in files:
        try:
            stream = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with stream:
            for line in stream:
                if not any(marker in line for marker in MARKERS):
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                for text in iter_strings(record):
                    if not any(marker in text for marker in MARKERS):
                        continue
                    for obj in find_results(text):
                        key = (obj.get("observed_at"), obj.get("stage"))
                        bundle = obj.get("selected_bundle") or {}
                        found.setdefault(key, {
                            "at": parse_ts(obj.get("observed_at")), "stage": obj["stage"], "choice": obj.get("choice"),
                            "specialty": obj.get("specialty"), "source": obj.get("source"),
                            "judge": (obj.get("judge") or {}).get("model"), "pinned": None,
                            "bundle": {k: bundle.get(k) for k in ("provider", "account", "model", "effort")} if bundle else None,
                            "reason": (obj.get("reason") or obj.get("error") or "")[:600],
                            "usage": usage_points(obj.get("usage") or {}), "origin": "transcript"})
    return [row for row in found.values() if row["at"]]


def from_ledger(path: Path):
    rows = []
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        at = parse_ts(r.get("at"))
        if at:
            rows.append({"at": at, "stage": r.get("stage"), "choice": r.get("choice"), "specialty": r.get("specialty"),
                         "source": r.get("source"), "judge": r.get("judge"), "pinned": r.get("judge_pinned"),
                         "constraint": r.get("constraint"), "bundle": r.get("bundle"), "reason": r.get("reason") or "",
                         "usage": r.get("usage") or {}, "alerts": r.get("alerts") or [], "origin": "ledger",
                         "payload_bytes": r.get("payload_bytes"), "duration_ms": r.get("duration_ms"),
                         "chain": r.get("chain"), "action": r.get("action"), "margin": r.get("margin"),
                         "hours": r.get("hours"), "agrees": r.get("agrees"), "router_choice": r.get("router_choice"),
                         "needs_user_decision": r.get("needs_user_decision"), "reason_category": r.get("reason_category")})
    return rows


def merge(ledger_rows, transcript_rows):
    """A ledger record supersedes the transcript copy of the same decision (clocks differ by minutes)."""
    kept = list(ledger_rows)
    for row in transcript_rows:
        twin = any(l["stage"] == row["stage"] and l["choice"] == row["choice"]
                   and abs((l["at"] - row["at"]).total_seconds()) <= 180 for l in ledger_rows)
        if not twin:
            kept.append(row)
    return sorted(kept, key=lambda r: r["at"])


def classify(row: dict) -> str:
    """Why this select landed where it did: fixed by instruction, fixed by the specialist table, or open."""
    if row.get("source") == "deterministic_allocator":
        return "planned"
    if row.get("source") == "constraint_record":
        return f"fixed:{row.get('constraint')}"
    bundle = row.get("bundle") or {}
    model = str(bundle.get("model") or "")
    if bundle.get("provider") in ("chatgpt-web", "pro-consult") or "pro" in str(row.get("choice") or "").lower():
        return "specialist:pro"
    if "astra" in model:
        return "specialist:astra"
    if FIXED_WORDS.search(row.get("reason") or "") or re.search(r"retained|same_", str(row.get("choice") or "")):
        return "fixed:instruction"
    return "open"


def pair_decisions(rows):
    """Join each delegate verdict with the select that follows it."""
    pairs, pending = [], None
    for row in rows:
        if row["stage"] == "delegate":
            if pending:
                pairs.append((pending, None))
            pending = row
        elif row["stage"] in ("select", "record", "plan"):
            if pending and (row["at"] - pending["at"]).total_seconds() <= PAIR_GAP_SECONDS:
                pairs.append((pending, row))
            else:
                if pending:
                    pairs.append((pending, None))
                pairs.append((None, row))
            pending = None
    if pending:
        pairs.append((pending, None))
    return pairs


def count_pinned(files):
    """Distinct explicit route_judge delegate/select command lines by --judge value (approximate)."""
    pinned = collections.Counter()
    pattern = re.compile(r"route_judge\.py[^\n]{0,12}?\s+(delegate|select)\b(.{0,300})")
    for path in files:
        try:
            stream = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        seen = set()
        with stream:
            for line in stream:
                if "route_judge.py" not in line or not ('"function_call"' in line or '"custom_tool_call"' in line or '"tool_use"' in line):
                    continue
                for stage, rest in pattern.findall(line):
                    if "--help" in rest[:200] or "--dry-run" in rest[:200]:
                        continue
                    key = (stage, rest[:140])
                    if key in seen:
                        continue
                    seen.add(key)
                    flag = re.search(r"--judge\s+(\w+)", rest)
                    pinned[flag.group(1) if flag else "auto"] += 1
    return pinned


# ---------------------------------------------------------------- token logs

def scan_codex(files):
    sessions, per_day = {}, collections.defaultdict(collections.Counter)
    for path in files:
        name = os.path.basename(path)
        if name in sessions:
            continue
        model = effort = None
        calls = 0
        prev_total = None
        total = last = None
        first_ts = last_ts = None
        peak = 0
        calls_by_day = collections.Counter()
        meta = {}
        with open(path, encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if "token_count" not in line and "turn_context" not in line and '"session_meta"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                payload = obj.get("payload") or {}
                kind = obj.get("type")
                if kind == "session_meta":
                    meta = payload
                elif kind == "turn_context":
                    model = payload.get("model") or model
                    effort = payload.get("effort") or effort
                elif kind == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info") or {}
                    usage, step = info.get("total_token_usage"), info.get("last_token_usage")
                    ts = parse_ts(obj.get("timestamp"))
                    if not usage or not ts:
                        continue
                    first_ts = first_ts or ts
                    last_ts = ts
                    total = usage
                    if prev_total is None or usage["total_tokens"] >= prev_total:
                        day = local(ts).date().isoformat()
                        per_day[day][(model, effort)] += usage["total_tokens"] - (prev_total or 0)
                        prev_total = usage["total_tokens"]
                    if step:
                        calls += 1
                        calls_by_day[local(ts).date().isoformat()] += 1
                        peak = max(peak, step.get("input_tokens", 0))
                        last = step
        if total and calls:
            subagent = isinstance(meta.get("source"), dict) and "subagent" in meta["source"]
            sessions[name] = {"name": name, "model": model, "effort": effort, "calls": calls, "total": total["total_tokens"],
                              "cached": total.get("cached_input_tokens", 0), "input": total.get("input_tokens", 0),
                              "output": total.get("output_tokens", 0), "reasoning": total.get("reasoning_output_tokens", 0),
                              "peak_ctx": peak, "first": first_ts, "last": last_ts, "calls_by_day": calls_by_day,
                              "cwd": meta.get("cwd") or "", "subagent": subagent}
    return list(sessions.values()), per_day


def scan_claude(files, since_day: str):
    seen, rows = set(), collections.defaultdict(collections.Counter)
    for path in files:
        with open(path, encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if '"usage"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                message = obj.get("message") or {}
                usage, ts = message.get("usage"), parse_ts(obj.get("timestamp"))
                key = message.get("id") or obj.get("uuid")
                if not usage or not ts or key in seen:
                    continue
                seen.add(key)
                day = local(ts).date().isoformat()
                if day < since_day:
                    continue
                bucket = rows[(day, message.get("model"))]
                bucket["calls"] += 1
                bucket["cache_read"] += usage.get("cache_read_input_tokens", 0)
                bucket["output"] += usage.get("output_tokens", 0)
    return rows


def dot_summary():
    if not DOT_DB.is_file():
        return None
    connection = sqlite3.connect(f"file:{DOT_DB.as_posix()}?mode=ro", uri=True)
    try:
        tasks = connection.execute("SELECT idempotency_key, status, created FROM tasks").fetchall()
        audit = connection.execute("SELECT action, timestamp FROM audit ORDER BY seq").fetchall()
    finally:
        connection.close()
    real = [t for t in tasks if not str(t[0]).startswith("renew-")]
    return {"tasks": len(tasks), "non_renewal": len(real), "statuses": collections.Counter(t[1] for t in tasks),
            "first_created": min((t[2] for t in tasks), default=None), "audit_actions": collections.Counter(a[0] for a in audit)}


def scan_subagent_spawns(paseo_files, claude_subagent_files, codex_sessions, since_dt: dt.datetime, pi_files=()) -> list[dict]:
    """Collect child-agent spawn events across Paseo creations, Pi sessions, Claude subagents, and Codex subagents."""
    spawns = []
    cutoff = max(since_dt, ROUTER_RULE_SINCE)
    for path in paseo_files:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        snap = data.get("snapshot") if isinstance(data.get("snapshot"), dict) else {}
        agent = snap.get("agent") if isinstance(snap.get("agent"), dict) else data
        idem = str(snap.get("idempotencyKey") or data.get("idempotencyKey") or "")
        is_child = bool(
            data.get("parentAgentId")
            or agent.get("parentAgentId")
            or (idem and not idem.startswith("draft_msg_"))
        )
        if not is_child:
            continue
        at = parse_ts(agent.get("createdAt") or agent.get("updatedAt") or data.get("createdAt"))
        if not at or at < cutoff:
            continue
        spawns.append({
            "at": at,
            "kind": "paseo",
            "label": f"paseo:{agent.get('provider') or '?'}/{agent.get('model') or '?'}",
            "cwd": agent.get("cwd") or "",
        })
    for path in pi_files:
        try:
            with open(path, encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    if "create_agent" not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if obj.get("type") != "message":
                        continue
                    msg = obj.get("message") or {}
                    if msg.get("role") != "assistant":
                        continue
                    ts = parse_ts(obj.get("timestamp"))
                    if not ts or ts < cutoff:
                        continue
                    for part in msg.get("content") or []:
                        if not isinstance(part, dict) or part.get("type") != "toolCall":
                            continue
                        code = str((part.get("arguments") or {}).get("code") or "")
                        if "create_agent(" in code:
                            m_prov = re.search(r'provider\s*:\s*["\']([^"\']+)["\']', code)
                            m_mod = re.search(r'model\s*:\s*["\']([^"\']+)["\']', code)
                            spawns.append({
                                "at": ts,
                                "kind": "pi->paseo",
                                "label": f"pi->paseo:{m_prov.group(1) if m_prov else '?'}/{m_mod.group(1) if m_mod else '?'}",
                                "cwd": Path(path).parent.name,
                            })
        except OSError:
            continue
    for path in claude_subagent_files:
        first_ts, model = None, None
        try:
            with open(path, encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    ts = parse_ts(obj.get("timestamp"))
                    if ts and (first_ts is None or ts < first_ts):
                        first_ts = ts
                    msg = obj.get("message") or {}
                    if isinstance(msg, dict) and msg.get("model"):
                        model = msg.get("model")
                    if first_ts and model:
                        break
        except OSError:
            continue
        if first_ts and first_ts >= cutoff:
            spawns.append({
                "at": first_ts,
                "kind": "claude-subagent",
                "label": f"claude-subagent/{model or '?'}",
                "cwd": Path(path).parent.parent.name,
            })
    for s in codex_sessions:
        if s.get("subagent") and s.get("first") and s["first"] >= cutoff:
            spawns.append({
                "at": s["first"],
                "kind": "codex-subagent",
                "label": f"codex-subagent/{s.get('model') or '?'}/{s.get('effort') or '?'}",
                "cwd": s.get("cwd") or "",
            })
    return sorted(spawns, key=lambda x: x["at"])


def find_unrouted_spawns(spawns: list[dict], rows: list[dict], window_seconds: int = UNROUTED_WINDOW_SECONDS) -> list[dict]:
    """Return child-agent spawns that had no plan/select/record decision within window_seconds prior."""
    valid_routes = [
        r["at"] for r in rows
        if r.get("stage") in ("plan", "select", "record")
        and r.get("choice") not in (None, "", "none", "self")
        and r.get("at") is not None
    ]
    unrouted = []
    for sp in spawns:
        t = sp["at"]
        matched = any(-60 <= (t - r_at).total_seconds() <= window_seconds for r_at in valid_routes)
        if not matched:
            unrouted.append(sp)
    return unrouted


# ---------------------------------------------------------------- findings

def sliding_max(times, hours):
    best, left = 0, 0
    for right, moment in enumerate(times):
        while (moment - times[left]).total_seconds() > hours * 3600:
            left += 1
        best = max(best, right - left + 1)
    return best


REMAIN = re.compile(r"(\d{1,3}(?:\.\d+)?)%\s+(?:remain|left|remaining)", re.I)


def quota_series(rows, account="codex", window="usage"):
    """(time, used_pct, derived) points of the latest stretch with no reset.

    Some callers stripped the usage block from the saved verdict; for those the percentage the judge
    quoted in its reason is used and marked derived."""
    points = {}
    ids = (window, "seven_day") if account.startswith("codex") and window == "usage" else (window,)
    for row in rows:
        entry = (row.get("usage") or {}).get(account) or {}
        fetched = parse_ts(entry.get("fetched_at"))
        used = next((w["used_pct"] for w in entry.get("windows") or []
                     if w.get("id") in ids and isinstance(w.get("used_pct"), (int, float))), None)
        if used is not None and fetched:
            points[fetched] = (used, False)
        elif (row.get("bundle") or {}).get("account") == account and row["stage"] == "select":
            match = REMAIN.search(row.get("reason") or "")
            if match:
                points.setdefault(row["at"], (100 - float(match.group(1)), True))
    ordered = sorted(points.items())
    start = 0
    for i in range(1, len(ordered)):
        if ordered[i][1][0] < ordered[i - 1][1][0] - 10:   # usage fell: the window was reset
            start = i
    return [(t, u, d) for t, (u, d) in ordered[start:]]


def peak_used(rows):
    """Highest used_pct seen per (account, window) in the period, from structured usage only."""
    peaks = {}
    for row in rows:
        for account, entry in (row.get("usage") or {}).items():
            for w in entry.get("windows") or []:
                if isinstance(w.get("used_pct"), (int, float)):
                    wid = "usage" if account.startswith("codex") and w.get("id") == "seven_day" else w.get("id")
                    key = (account, wid)
                    peaks[key] = max(peaks.get(key, 0), w["used_pct"])
    return peaks


def load_capacity(path: Path = CAPACITY):
    """The tray capacity service's output, or None when it is missing or unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def load_agent_days(path: Path = USAGE_STATS) -> dict:
    """{day: {account: {agents, peak_hour_concurrency}}} from the capacity service, or {} when absent."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    agents = data.get("agents") if isinstance(data, dict) else None
    return agents if isinstance(agents, dict) else {}


def capacity_findings(capacity, now: dt.datetime, agent_days: dict | None = None) -> list[tuple]:
    if capacity is None:
        return [("warn", "capacity", "托盘用量统计服务没有产出 capacity.json",
                 f"{CAPACITY} 不存在或无法解析;router 没有实时用量读数,其他工作的需求只能按直线进度估计。",
                 "确认托盘在运行,或手动运行 `python tray/capacity_stats.py --once`;失败日志在 ~/.agent-compass/capacity.log。")]
    out = []
    made = parse_ts(capacity.get("generated_at"))
    age = (now - made).total_seconds() / 3600 if made else None
    if age is None or age > CAPACITY_STALE_HOURS:
        out.append(("warn", "capacity", "用量统计已过期",
                    f"capacity.json 生成于 {capacity.get('generated_at')}(约 {'?' if age is None else f'{age:.0f}'} 小时前);服务应每 5 分钟刷新。",
                    "托盘可能没在运行或统计失败;router 忽略过期的用量读数,按直线进度估计需求,并在 assumptions 里提示。"))
    worked = capacity.get("worker_hours") or {}
    if worked:
        active = {a: v.get("count", 0) for a, v in (capacity.get("active_workers") or {}).items()}
        out.append(("info", "workhours", "各账户工时(日志统计,单位=活跃会话小时)",
                    "; ".join(f"{a} {v.get('24h', 0):.1f}h/24h、{v.get('7d', 0):.1f}h/7d" for a, v in sorted(worked.items()))
                    + (f"。当前在跑:{', '.join(f'{a} {n}' for a, n in sorted(active.items()))}。" if active else "。"),
                    "工时按相邻调用间隔(上限 10 分钟)累计;并行会话各自计时。"))
    recent = [(day, row["antigravity"]) for day, row in sorted((agent_days or {}).items()) if isinstance(row, dict) and "antigravity" in row][-3:]
    if recent:
        out.append(("info", "flash-agents", "Flash 代理数",
                    "近日:" + "; ".join(f"{day[5:]} {r['agents']} 个代理、峰值并发 {r['peak_hour_concurrency']}" for day, r in recent) + "。",
                    "代理小时来自 Antigravity CLI 对话库(每步时间戳,相邻间隔上限 10 分钟)。"))
    return out


def build_findings(ctx: dict) -> list[dict]:
    out = []

    def add(level, key, title, evidence, action):
        out.append({"level": level, "key": key, "title": title, "evidence": evidence, "action": action})

    rows, selects = ctx["rows"], [r for r in ctx["rows"] if r["stage"] in ("select", "record", "plan") and r.get("choice") != "none"]
    delegates = [r for r in rows if r["stage"] == "delegate" and r.get("choice")]
    if len(delegates) >= GATE_MIN_SAMPLES:
        yes = sum(1 for r in delegates if r["choice"] == "delegate")
        if yes / len(delegates) >= GATE_YES_SHARE:
            add("warn", "gate", "delegate 闸门几乎恒为 delegate",
                f"{yes}/{len(delegates)} 次判 delegate(阈值 {GATE_YES_SHARE:.0%});仅 {len(delegates) - yes} 次 self。",
                "闸门没有过滤作用:调用方只在已决定委派时才调用它。选 worker 直接用 `route_judge.py plan`(无模型调用)。")
    kinds = collections.Counter(classify(r) for r in selects)
    planned = [r for r in selects if r.get("source") == "deterministic_allocator"]
    overrides = [r for r in selects if r.get("source") == "constraint_record" and r.get("origin") == "ledger"]
    dispatches = len(planned) + len(overrides)
    if dispatches >= PLAN_MIN_SAMPLES and len(planned) / dispatches < PLAN_SHARE:
        off = [r for r in overrides if r.get("agrees") is False]
        add("warn", "override", f"router 选择只占 {len(planned) / dispatches:.0%},低于 {PLAN_SHARE:.0%} 目标",
            f"{len(planned)} 次 plan,{len(overrides)} 次人工钉死(其中 {len(off)} 次与 router 不一致)。"
            + ("不一致的原因分类:" + ", ".join(f"{k} {v}" for k, v in collections.Counter(
                r.get("reason_category") or "未分类" for r in off).most_common()) + "。" if off else "")
            + ("最近的不一致:" + "; ".join(f"{local(r['at']):%m-%d %H:%M} 钉死 {(r.get('bundle') or {}).get('bundle_id') or (r.get('bundle') or {}).get('model')}"
                                       f" vs router {r.get('router_choice')}" for r in off[-3:]) if off else ""),
            "逐条看 `override_cost`:router 排除的原因若站不住(能力表/消耗/账户状态错了),改 `accounts.json`;若钉死合理,补进表而不是继续钉。")
    if planned:
        by_account = collections.defaultdict(float)
        for r in planned:
            by_account[(r.get("bundle") or {}).get("account") or "?"] += float(r.get("hours") or 0)
        waits = sum(1 for r in planned if r.get("needs_user_decision"))
        add("info", "balance", "plan 派发的预计工时按账户分布",
            "; ".join(f"{a} {h:.1f}h" for a, h in sorted(by_account.items(), key=lambda kv: -kv[1]))
            + f"。其中 {waits} 次需要用户决定(owner 无法继续,或要等的时间超过 can_wait)。",
            "对照各账户的短缺概率(plan 结果 `capacity.shortage_probability`):长期偏向某个本会短缺的账户,说明 `shortage_loss_per_worker_hour` 或消耗估计需要校准(见 capacity 相关发现)。")
    fixed = sum(v for k, v in kinds.items() if k not in ("open", "planned"))
    if selects and fixed / len(selects) >= FIXED_SHARE:
        add("warn", "fixed", "绝大多数分配由指令/路由表钉死,judge 不改变结果",
            f"{fixed}/{len(selects)} 次 select 的路由已固定({', '.join(f'{k} {v}' for k, v in kinds.most_common())});"
            f"真正做额度/质量权衡的只有 {kinds.get('open', 0)} 次。",
            "路由本该由 `plan` 综合能力/适配/余量/消耗/连贯性来定;只有用户对本任务的明确指令才 `record --constraint user_override`。")
    now_usage = ctx["usage_now"]
    accounts = now_usage.get("accounts", {})
    judge_calls = collections.Counter(r["judge"] for r in rows if r.get("judge"))
    if judge_calls:
        top, n = judge_calls.most_common(1)[0]
        account = "codex" if top and "sol" in top else "claude"
        window = ctx["router"].tightest_window(accounts.get(account, {}), top or "")
        if window and window["remaining_pct_before_reset"] <= LOW_REMAINING_PCT:
            pins = ctx["pinned"]
            add("warn", "judge-account", f"judge 调用集中在额度见底的账户({account})",
                f"{n}/{sum(judge_calls.values())} 次 judge 是 {top};该账户当前仅剩 {window['remaining_pct_before_reset']:.0f}%。"
                f"显式调用里 `--judge` 固定情况:{dict(pins)}。",
                "auto 已加健康门槛(≤10% 排后);调用方固定 `--judge codex` 时会给 `pinned_judge_account_low` 告警。")
    for name, row in accounts.items():
        if name.lower() in METERED:
            continue
        window = ctx["router"].tightest_window(row)
        if window and window["remaining_pct_before_reset"] <= LOW_REMAINING_PCT and window["seconds_until_reset"] > 48 * 3600:
            idle = [f"{a}({ctx['router'].tightest_window(r)['remaining_pct_before_reset']:.0f}% 余,近期峰值已用 "
                    f"{max([v for (acc, _), v in ctx['peaks'].items() if acc == a] or [0]):.0f}%)" for a, r in accounts.items()
                    if a != name and a.lower() not in METERED and r.get("status") == "fresh" and ctx["router"].tightest_window(r)
                    and ctx["router"].tightest_window(r)["remaining_pct_before_reset"] >= 80]
            add("crit", f"cliff-{name}", f"{name} 额度见底且重置还很远",
                f"{name}/{window['id']} 仅剩 {window['remaining_pct_before_reset']:.0f}%,{window['seconds_until_reset'] / 86400:.1f} 天后重置。"
                + (f" 同期几乎闲置:{', '.join(idle)}。" if idle else ""),
                "余量取决于刚重置的时点 —— 峰值说明同样的负载也会烧光它们;换账户不创造容量,要降强度(并行度/档位/上下文)。被钉死的路由不会自己让位,需用户决定。")
    for name, row in accounts.items():
        if row.get("flag_conflict"):
            add("warn", f"stale-{name}", f"{name} 的用量观测已过期却标 stale=false",
                f"最后观测 {row['fetched_at']}(约 {row['observation_age_seconds'] / 86400:.0f} 天前)。",
                "路由按 fetched_at 判断因而没被误导,但该账户容量实际未知;确认它是否仍是可用订阅。")
    series = quota_series(rows + [ctx["now_row"]])
    if len(series) >= 2:
        (t0, u0, _), (t1, u1, _) = series[0], series[-1]
        hours = (t1 - t0).total_seconds() / 3600
        derived = sum(1 for p in series if p[2])
        if hours >= 1 and u1 > u0:
            add("info", "burn", "codex 当前额度窗口的燃烧速度",
                f"{local(t0):%m-%d %H:%M} 已用 {u0:.0f}% → {local(t1):%m-%d %H:%M} 已用 {u1:.0f}%,{hours:.1f}h 内 {(u1 - u0) / hours:.1f}%/h"
                f"({len(series)} 个观测点,其中 {derived} 个取自 judge 理由文字)。",
                "判决里每次都写“其他账户需求未知”—— 路由没有累计视角;账本 + `exhaustion_before_reset` 告警补这一块。")
    heavy = {d: c for d, c in ctx["heavy_by_day"].items() if c >= HEAVY_SESSIONS_PER_DAY}
    if heavy:
        days = ", ".join(f"{d} ×{c}" for d, c in sorted(heavy.items()))
        worst = max(heavy, key=heavy.get)
        add("warn", "concurrent", "多个重型 Codex 会话同日并行消耗同一额度池",
            f"每日各 ≥{HEAVY_SESSION_CALLS} 次调用的会话数:{days}。峰值日 {worst}:{ctx['heavy_detail'][worst]}。",
            "单次路由判断看不到彼此;并行度与档位(xhigh/max)需要由调用方控制,路由只能靠烧速告警提示。")
    long_runs = [x for x in ctx["sessions"] if x["calls"] >= LONG_SESSION_CALLS]
    bloated = sorted((x for x in long_runs if x["input"] / x["calls"] >= BLOAT_CONTEXT_TOKENS), key=lambda x: -x["total"])
    if bloated:
        top = "; ".join(f"{x['name'][8:30]} {x['model']}/{x['effort']} {x['calls']}次×{x['input'] / x['calls'] / 1000:.0f}k"
                        for x in bloated[:4])
        add("warn", "bloat", f"{len(bloated)} 个长期保留会话每次调用重读 ≥{BLOAT_CONTEXT_TOKENS // 1000}k 上下文",
            f"{top}。缓存命中均 ≥94%(便宜但不免费),峰值上下文 ~{max(x['peak_ctx'] for x in bloated) / 1000:.0f}k,接近窗口上限。",
            "“复用持有者省交接成本”的判断没有计入这一项;达到阈值应轮换(新包交接)而不是继续保留。")
    for x in long_runs:
        if 0 < x["output"] / x["calls"] < POLL_OUTPUT_PER_CALL:
            add("warn", f"poll-{x['name'][8:24]}", "疑似轮询型工作占用模型会话",
                f"{x['name'][8:30]} {x['model']}/{x['effort']}:{x['calls']} 次调用平均仅输出 {x['output'] / x['calls']:.0f} tokens,"
                f"上下文 ~{x['input'] / x['calls'] / 1000:.0f}k/次(合计 {x['total'] / 1e6:.0f}M tokens)。",
                "监看类循环应由脚本/定时器承担,模型只在需要判读时短上下文介入。")
    pro_times = sorted(r["at"] for r in selects if classify(r) == "specialist:pro")
    if sliding_max(pro_times, PRO_WINDOW_HOURS) >= PRO_BURST:
        add("info", "pro", f"Pro 顾问节奏:{PRO_WINDOW_HOURS}h 内最多 {sliding_max(pro_times, PRO_WINDOW_HOURS)} 次",
            f"窗口内累计 {len(pro_times)} 次 Pro 派发。Pro 网页用量不进入用量快照,路由无法衡量其成本。",
            "账本现记录每次 Pro 派发并在 ≥5 次/12h 时给 `advisor_cadence` 提示;采纳率仍需人工评估。")
    dot = ctx["dot"]
    if dot is not None and dot["non_renewal"] <= 1:
        add("info", "dot", "Dot 路由尚无真实派发",
            f"dot2api 任务表共 {dot['tasks']} 条,非续期 {dot['non_renewal']} 条(均为接通测试);服务与隧道持续运行,每日需续订阅。",
            "闭环判据很窄属预期;用 `record --constraint dot_closed_loop` 记录后才能统计真实使用。")
    flash = sum(1 for r in rows if str((r.get("bundle") or {}).get("model") or "").startswith("gemini"))
    if flash == 0:
        add("info", "flash", "Gemini Flash 的简单任务分流尚无派发记录",
            "规则于 2026-10-09 生效:Flash 不是首选编码者,只因额度很大承接 `routine` 的编码+工具调用任务,`hard` 及以上不进入;账本/会话中暂无 gemini 派发;Paseo 无 antigravity 代理。", "样本不足,明日复查。")
    if "capacity" in ctx:
        for finding in capacity_findings(ctx["capacity"], ctx["now"], ctx.get("agent_days")):
            add(*finding)
    unrouted = find_unrouted_spawns(ctx.get("spawns") or [], rows)
    if unrouted:
        samples = "; ".join(f"{local(u['at']):%m-%d %H:%M} {u['label']}" for u in unrouted[-5:])
        add("warn", "unrouted", f"检测到 {len(unrouted)} 次未过 router 的子 agent 启动",
            f"启动前 15 分钟内无 plan/select/record 账本记录。最近样本:{samples}。",
            "检查对应宿主是否已挂载 PreToolUse router_gate_hook.py 与全局 AGENTS.md/CLAUDE.md/GEMINI.md。")
    if not ctx["ledger_rows"]:
        add("info", "ledger", "账本今日起才开始记录", "历史分配由会话回溯得到,可能漏计无法解析的输出。", "之后每次路由自动写账本。")
    order = {"crit": 0, "warn": 1, "info": 2}
    return sorted(out, key=lambda f: order[f["level"]])


# ---------------------------------------------------------------- report

def fmt_pct(value):
    return "—" if value is None else f"{value:.0f}%"


def render(ctx: dict, days: int, detail_days: int, commentary: str) -> str:
    rows, router = ctx["rows"], ctx["router"]
    now = ctx["now"]
    lines = [f"# Router 每日巡检 — {local(now):%Y-%m-%d %H:%M}(本地时间)", ""]
    if commentary:
        lines += [commentary.strip(), ""]
    lines += ["## 1. 发现(规则判定)", ""]
    labels = {"crit": "严重", "warn": "注意", "info": "信息"}
    for f in ctx["findings"]:
        lines += [f"- **[{labels[f['level']]}] {f['title']}** — {f['evidence']}  ", f"  → {f['action']}"]
    lines += ["", "## 2. 分配汇总(按本地日)", "",
              "| 日期 | delegate | self | select | plan | 钉死 | Pro 顾问 | open(真权衡) | judge 构成 |", "|---|---|---|---|---|---|---|---|---|"]
    by_day = collections.defaultdict(list)
    for r in rows:
        by_day[local(r["at"]).date().isoformat()].append(r)
    for day in sorted(by_day):
        items = by_day[day]
        sel = [r for r in items if r["stage"] in ("select", "record", "plan") and r.get("choice") != "none"]
        kinds = collections.Counter(classify(r) for r in sel)
        judges = collections.Counter((r.get("judge") or "").replace("claude-", "").replace("gpt-6.1-", "") or "-" for r in items if r.get("judge"))
        lines.append(f"| {day} | {sum(1 for r in items if r['stage'] == 'delegate' and r.get('choice') == 'delegate')} | "
                     f"{sum(1 for r in items if r['stage'] == 'delegate' and r.get('choice') == 'self')} | {len(sel)} | {kinds.get('planned', 0)} | "
                     f"{sum(v for k, v in kinds.items() if k.startswith('fixed'))} | {kinds.get('specialist:pro', 0)} | "
                     f"{kinds.get('open', 0)} | {', '.join(f'{k}×{v}' for k, v in judges.most_common()) or '-'} |")
    lines += ["", f"## 3. 逐次分配(最近 {detail_days} 天;delegate→select 成对)", "",
              "| 时间 | 类型 | 分配 | 分类 | judge | 账户用量(调用时) | 理由 |", "|---|---|---|---|---|---|---|"]
    cutoff = now - dt.timedelta(days=detail_days)
    for d, s in pair_decisions(rows):
        anchor = (s or d)["at"]
        if anchor < cutoff:
            continue
        bundle = (s or {}).get("bundle") or {}
        label = f"{bundle.get('account') or '-'}/{bundle.get('model') or '-'}/{bundle.get('effort') or '-'}" if s else "(无 select)"
        account = bundle.get("account")
        entry = ((s or d).get("usage") or {}).get(account) or {}
        windows = [w for w in entry.get("windows") or [] if isinstance(w.get("used_pct"), (int, float))]
        used = "; ".join(f"{w['id']} 余{100 - w['used_pct']:.0f}%" for w in windows[:2])
        if not used:
            quoted = REMAIN.search(((s or d).get("reason") or ""))
            used = f"余{quoted.group(1)}%(理由)" if quoted and account == "codex" else "—"
        reason = ((s or d).get("reason") or "").replace("|", "/").replace("\n", " ")[:70]
        kind = classify(s) if s else ("self" if d and d.get("choice") == "self" else "-")
        lines.append(f"| {local(anchor):%m-%d %H:%M} | {(d or {}).get('specialty') or '-'} | {label} | {kind} | "
                     f"{((s or d).get('judge') or '-').replace('claude-', '').replace('gpt-6.1-', '')} | {used} | {reason} |")
    lines += ["", "## 4. 当前用量快照", "", "| 账户 | 状态 | 最紧窗口 | 余量 | 重置 | 观测时间 |", "|---|---|---|---|---|---|"]
    for name, row in sorted(ctx["usage_now"].get("accounts", {}).items()):
        window = router.tightest_window(row)
        flag = " ⚠标志冲突" if row.get("flag_conflict") else ""
        lines.append(f"| {name}{' (按量)' if name.lower() in METERED else ''} | {row.get('status')}{flag} | {(window or {}).get('id', '—')} | "
                     f"{fmt_pct((window or {}).get('remaining_pct_before_reset'))} | "
                     f"{'—' if not window else f'{window['seconds_until_reset'] / 86400:.1f}d'} | {(row.get('fetched_at') or '—')[:16]} |")
    series = quota_series(rows + [ctx["now_row"]])
    if series:
        lines += ["", "### codex 额度曲线(同一重置窗口内;“理由”= 取自 judge 理由文字,调用方裁掉了用量块)", "",
                  "| 观测时间 | 已用 | 来源 |", "|---|---|---|"]
        step = max(1, len(series) // 16)
        picked = series[::step] + ([series[-1]] if (len(series) - 1) % step else [])
        for t, used, derived in picked:
            lines.append(f"| {local(t):%m-%d %H:%M} | {used:.0f}% | {'理由' if derived else '快照'} |")
    lines += ["", f"## 5. Codex 实际消耗(rollout 日志,近 {days} 天,token 含缓存读取)", "",
              "| 日期 | 合计(M) | 构成(M) |", "|---|---|---|"]
    for day in sorted(ctx["codex_days"]):
        parts = ctx["codex_days"][day]
        lines.append(f"| {day} | {sum(parts.values()) / 1e6:.0f} | " + ", ".join(
            f"{m}/{e} {v / 1e6:.0f}" for (m, e), v in parts.most_common(4)) + " |")
    lines += ["", "| 会话 | 模型/档位 | 调用数 | 均上下文(k) | 缓存% | 输出(M) | 合计(M) | 起止 | 类型 |", "|---|---|---|---|---|---|---|---|---|"]
    for s in sorted(ctx["sessions"], key=lambda s: -s["total"])[:10]:
        lines.append(f"| {s['name'][8:30]} | {s['model']}/{s['effort']} | {s['calls']} | {s['input'] / s['calls'] / 1000:.0f} | "
                     f"{100 * s['cached'] / max(s['input'], 1):.0f} | {s['output'] / 1e6:.2f} | {s['total'] / 1e6:.0f} | "
                     f"{local(s['first']):%m-%d %H:%M}→{local(s['last']):%m-%d %H:%M} | {'子代理' if s['subagent'] else '主会话'} |")
    lines += ["", f"## 6. Claude 实际消耗(近 {days} 天)", "", "| 日期 | 模型 | 调用 | 输出(k) | 缓存读(M) |", "|---|---|---|---|---|"]
    for (day, model), v in sorted(ctx["claude_days"].items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
        if v["calls"] >= 10:
            lines.append(f"| {day} | {model} | {v['calls']} | {v['output'] / 1e3:.0f} | {v['cache_read'] / 1e6:.0f} |")
    dot = ctx["dot"]
    lines += ["", "## 7. Dot / 数据来源与局限", ""]
    if dot:
        lines.append(f"- dot2api:任务 {dot['tasks']} 条(非续期 {dot['non_renewal']}),状态 {dict(dot['statuses'])},审计动作 {dict(dot['audit_actions'])}。")
    lines += [f"- 决策来源:账本 {len(ctx['ledger_rows'])} 条 + 会话回溯 {len(ctx['transcript_rows'])} 条,合并后 {len(rows)} 条。",
              f"- 显式 `route_judge.py delegate/select` 调用的 `--judge` 取值:{dict(ctx['pinned'])}(仅统计能在日志里看到命令行的调用)。",
              "- token 数含缓存读取,不等于额度消耗;额度只能看用量快照的百分比。Pro 网页用量与 Dot 的 ChatGPT 轮次不在任何快照里。",
              "- 本报告判定“规则是否触发”,不判定研究成果好坏;价值是否合理需结合上面的证据由人判断。", ""]
    return "\n".join(lines)


def log_roots() -> dict[str, list[str]]:
    """Session-log roots of every discovered Claude/Codex account, plus capacity-sources.json entries."""
    codex_home = Path(os.environ.get("CODEX_HOME") or HOME / ".codex").expanduser()
    claude_home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude").expanduser()
    roots = {"codex": [codex_home / "sessions", codex_home / "archived_sessions"], "claude": [claude_home / "projects"]}
    try:
        if str(SKILL_SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SKILL_SCRIPTS))
        import provider_inventory
        accounts = provider_inventory.discover().get("accounts") or {}
    except Exception:
        accounts = {}
    for row in accounts.values():
        home = Path(row["home"]) if isinstance(row, dict) and row.get("home") else None
        if home and row.get("family") == "codex":
            roots["codex"] += [home / "sessions", home / "archived_sessions"]
        elif home and row.get("family") == "claude":
            roots["claude"].append(home / "projects")
    state = Path(os.environ.get("ROUTER_STATE_DIR") or HOME / ".agent-compass")
    try:
        extra = json.loads((state / "capacity-sources.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        extra = {}
    for spec in (extra.values() if isinstance(extra, dict) else ()):
        if isinstance(spec, dict) and spec.get("kind") in roots and isinstance(spec.get("roots"), list):
            roots[spec["kind"]] += [Path(r) for r in spec["roots"]]
    # A session file is read once even when two accounts resolve to the same home.
    return {kind: list(dict.fromkeys(str(Path(r).resolve()) for r in paths)) for kind, paths in roots.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--detail-days", type=int, default=2)
    parser.add_argument("--commentary", type=Path)
    parser.add_argument("--out", type=Path, default=REPORT_DIR)
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args()
    router = load_router()
    now = dt.datetime.now(UTC)
    since_dt = now - dt.timedelta(days=args.days)
    since = since_dt.timestamp()
    pick = lambda pattern: [f for f in glob.glob(str(HOME / pattern), recursive=True) if os.path.getmtime(f) >= since]
    under = lambda roots, pattern: list(dict.fromkeys(
        f for root in roots for f in glob.glob(str(Path(root) / pattern), recursive=True) if os.path.getmtime(f) >= since))
    homes = log_roots()
    codex_files = under(homes["codex"], "**/*.jsonl")
    claude_subagent_files = under(homes["claude"], "*/*/subagents/*.jsonl")
    claude_files = under(homes["claude"], "*/*.jsonl") + claude_subagent_files
    pi_files = pick(".pi/agent/sessions/**/*.jsonl")
    paseo_files = pick(".paseo/creations/*.json")
    ledger_rows = from_ledger(LEDGER)
    transcript_rows = from_transcripts(codex_files + claude_files + pi_files)
    rows = [r for r in merge(ledger_rows, transcript_rows) if r["at"] >= since_dt]
    sessions, codex_days = scan_codex(codex_files)
    spawns = scan_subagent_spawns(paseo_files, claude_subagent_files, sessions, since_dt, pi_files=pi_files)
    since_day = local(since_dt).date().isoformat()
    heavy_by_day, heavy_detail = collections.Counter(), collections.defaultdict(list)
    for s in sessions:
        for day, n in s["calls_by_day"].items():
            if n >= HEAVY_SESSION_CALLS:
                heavy_by_day[day] += 1
                heavy_detail[day].append(f"{s['model']}/{s['effort']}×{n}")
    usage_now = router.usage_snapshot()
    now_row = {"usage": usage_points(usage_now)}
    ctx = {"now": now, "rows": rows, "ledger_rows": ledger_rows, "transcript_rows": transcript_rows, "router": router,
           "usage_now": usage_now, "now_row": now_row, "sessions": sessions, "spawns": spawns,
           "pinned": count_pinned(codex_files + claude_files),
           "codex_days": {d: v for d, v in codex_days.items() if d >= since_day}, "claude_days": scan_claude(claude_files, since_day),
           "peaks": peak_used(rows), "heavy_by_day": heavy_by_day, "heavy_detail": {d: ", ".join(v[:6]) for d, v in heavy_detail.items()}, "dot": dot_summary(),
           "capacity": load_capacity(), "agent_days": load_agent_days()}
    ctx["findings"] = build_findings(ctx)
    commentary = args.commentary.read_text(encoding="utf-8") if args.commentary else ""
    report = render(ctx, args.days, args.detail_days, commentary)
    if not args.no_write:
        args.out.mkdir(parents=True, exist_ok=True)
        path = args.out / f"{local(now):%Y-%m-%d}.md"
        path.write_text(report, encoding="utf-8")
        print(f"report: {path}")
    print(report if args.no_write else "\n".join(l for l in report.splitlines() if l.startswith("- **")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
