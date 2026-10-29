"""Usage readings, running sessions and worker-hours from local session logs, for the router.

Runs as a short-lived process the tray starts every few minutes (`--once`). Each run reads only the bytes
appended to the Codex and Claude session logs since the last run, folds them into per-hour buckets of active
worker time, samples the usage-float cache, and rewrites two files in ~/.route-manager:

  capacity.json     what the router's allocator reads: each window's latest used percent with its rise over the
                    last 3 and 24 hours (the demand forecast), and the workers running right now
  usage-stats.json  per-day worker-hours, calls, tokens and list-price dollars by account and model/effort, for the
                    daily inspection and to recompute a model's `usd_per_hour` in accounts.json

There is no fit of percent against tokens any more; the router prices work at
list price against community-measured window capacity.

Codex rollouts carry the account's own rate-limit percentage on every call, so Codex history needs no other
source. Claude logs have no percentage; its windows come from the usage-float samples taken while this runs.
Pi sessions signed in to ChatGPT (provider openai-codex) spend the Codex account's allowance, so their calls are
counted as Codex worker time; Pi calls to other providers are skipped.
Antigravity (Gemini Flash and the third-party pool) keeps one SQLite database per conversation; every step has
timestamps, so each conversation is an agent and its active time is agent-hours. Windows named gemini-* are
charged only the Gemini models' hours and 3p-* windows only the others.
A worker-hour is one session actively running for one hour: the time between consecutive calls, with any gap
longer than ACTIVE_GAP_SECONDS counted as idle. Nothing here decides a route; policy lives in the allocator.

    python capacity_stats.py [--once] [--full] [--days N] [--no-write]
"""

from __future__ import annotations

import argparse
import bisect
import collections
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

UTC = timezone.utc
SCHEMA = 2
LOOKBACK_DAYS = 14
ACTIVE_GAP_SECONDS = 600        # a longer pause between calls is idle time, not work
STALE_SAMPLE_SECONDS = 2 * 3600
OBSERVE_SHORT_HOURS = 3
OBSERVE_LONG_HOURS = 24
LOCK_SECONDS = 30 * 60
SAME_RESET_SECONDS = 120        # reset times from different fetches differ by sub-second jitter
WINDOW_BY_MINUTES = {10080: "usage"}   # Codex rate-limit window length -> usage-float window id
AGY_MODEL = re.compile(rb"(gemini-\d[\d.]*-(?:flash|pro|argon)[\w\-]*|claude-[a-z]+-[\w\-]*\d[\w\-]*|gpt-[\w.\-]*\d[\w.\-]*)")
AGY_EFFORT = re.compile(r"-(low|medium|high|xhigh|max)$")
STEP_TIME_RANGE = (1_700_000_000, 2_100_000_000)   # epoch seconds a step timestamp can take (late 2023 to 2036)

HOME = Path.home()
STATE_DIR = Path(os.environ.get("ROUTER_STATE_DIR") or HOME / ".route-manager").expanduser()
STATE_FILE = Path(os.environ.get("ROUTER_CAPACITY_STATE", STATE_DIR / "capacity-state.json"))
OUTPUT = Path(os.environ.get("ROUTER_CAPACITY", STATE_DIR / "capacity.json"))
STATS_FILE = Path(os.environ.get("ROUTER_USAGE_STATS", STATE_DIR / "usage-stats.json"))
LOCK_FILE = STATE_DIR / "capacity.lock"
SOURCES_FILE = STATE_DIR / "capacity-sources.json"
KINDS = ("codex", "claude", "antigravity", "pi", "opencode")
USAGE_CACHE = Path(os.environ.get("ROUTER_USAGE_CACHE", HOME / ".usage-float" / "usage-cache.json"))

# API list prices in USD per million tokens: (input, cache write 5 min, cache write 1 h, cache read, output), checked
# 2026-10-12 on platform.claude.com/docs/en/about-claude/pricing and developers.openai.com/api/docs/pricing (standard
# tier, short context). A subscription is not billed per token, but how much of a window a call takes is assumed to
# follow these prices: an Opus 5.5 token costs twice a Sonnet 5.5 token, an Astra token five times a Sol token. Without
# them one shared per-token rate made a model that moves fewer tokens look cheaper whatever its price.
PRICES = {
    "claude-fable-5-1": (10.0, 12.5, 20.0, 0.25, 50.0),
    "claude-opus-5-5": (4.0, 5.0, 8.0, 0.20, 20.0),
    "claude-sonnet-5-5": (2.0, 2.5, 4.0, 0.10, 10.0),
    "claude-haiku-5-5": (0.10, 0.125, 0.20, 0.01, 0.50),
    "gpt-6-astra": (10.0, 12.5, 12.5, 1.00, 50.0),
    "gpt-6.1-sol": (2.0, 2.5, 2.5, 0.10, 10.0),
    "gpt-6-sol": (2.0, 2.5, 2.5, 0.20, 10.0),
    "gpt-6-luna": (0.10, 0.125, 0.125, 0.01, 0.50),
}
PRICE_VERSION = 2      # a state scanned under another price/model table is read from the logs again


# ------------------------------------------------------------------ small helpers

def parse_ts(value) -> float | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def hour_key(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H")


def key_epoch(key: str) -> float:
    return datetime.strptime(key, "%Y-%m-%dT%H").replace(tzinfo=UTC).timestamp()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def short_hash(text: str) -> str:
    return hashlib.blake2b(text.encode(), digest_size=6).hexdigest()


def same_reset(a: float, b: float) -> bool:
    return abs(a - b) < SAME_RESET_SECONDS


def list_usd(model: str, fresh: int = 0, write_5m: int = 0, write_1h: int = 0, read: int = 0, out: int = 0) -> float:
    """List-price dollars of one call, or 0.0 when the model has no price (its account then keeps the token fit)."""
    name = (model or "").split("/")[0]
    key = max((k for k in PRICES if name.startswith(k)), key=len, default=None)
    if key is None:
        return 0.0
    price = PRICES[key]
    return (fresh * price[0] + write_5m * price[1] + write_1h * price[2] + read * price[3] + out * price[4]) / 1e6


def sources() -> dict[str, dict]:
    """Accounts whose logs can be read. capacity-sources.json may add or replace entries (e.g. a second Codex home)."""
    # Primary homes follow the same overrides the CLIs and provider discovery honour.
    codex_home = Path(os.environ.get("CODEX_HOME") or HOME / ".codex").expanduser()
    claude_home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude").expanduser()
    found = {
        "codex": {"kind": "codex", "roots": [str(codex_home / "sessions"), str(codex_home / "archived_sessions")]},
        "claude": {"kind": "claude", "roots": [str(claude_home / "projects")]},
        "antigravity": {"kind": "antigravity", "roots": [str(HOME / ".gemini" / "antigravity-cli" / "conversations")]},
        "codex-pi": {"kind": "pi", "account": "codex", "roots": [str(HOME / ".pi" / "agent" / "sessions")]},
        "opencode": {"kind": "opencode", "account": "opencode", "roots": [str(HOME / ".local" / "share" / "opencode")]},
    }
    try:
        paseo_cfg = json.loads((Path(os.environ.get("PASEO_HOME") or HOME / ".paseo") / "config.json").read_text(encoding="utf-8-sig"))
        paseo_providers = ((paseo_cfg.get("agents") or {}).get("providers") or {}) if isinstance(paseo_cfg, dict) else {}
    except (OSError, ValueError):
        paseo_providers = {}
    for pid, spec in (paseo_providers.items() if isinstance(paseo_providers, dict) else []):
        if not isinstance(spec, dict) or spec.get("enabled") is False:
            continue
        extends = str(spec.get("extends") or "").strip().lower()
        env = spec.get("env") if isinstance(spec.get("env"), dict) else {}
        # A built-in provider is replaced only when Paseo points it at another home.
        if pid in found and not (pid in ("claude", "codex") and env.get("CLAUDE_CONFIG_DIR" if pid == "claude" else "CODEX_HOME")):
            continue
        extends = extends or (pid if pid in ("claude", "codex") else "")
        if extends == "claude" or (not extends and str(pid).startswith("claude-")):
            chome = Path(str(env.get("CLAUDE_CONFIG_DIR") or (HOME / f".{pid}"))).expanduser()
            found[pid] = {"kind": "claude", "roots": [str(chome / "projects")]}
        elif extends == "codex" or (not extends and str(pid).startswith("codex-")):
            chome = Path(str(env.get("CODEX_HOME") or (HOME / f".{pid}"))).expanduser()
            found[pid] = {"kind": "codex", "roots": [str(chome / "sessions"), str(chome / "archived_sessions")]}
    try:
        for entry in sorted(HOME.glob(".claude-*")):
            if entry.is_dir() and entry.name.lstrip(".") not in found:
                found[entry.name.lstrip(".")] = {"kind": "claude", "roots": [str(entry / "projects")]}
        for entry in sorted(HOME.glob(".codex-*")):
            if entry.is_dir() and not entry.name.startswith(".codex-chatgpt-web") and entry.name.lstrip(".") not in found:
                found[entry.name.lstrip(".")] = {"kind": "codex", "roots": [str(entry / "sessions"), str(entry / "archived_sessions")]}
    except OSError:
        pass
    try:
        extra = json.loads(SOURCES_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        extra = {}
    for name, spec in (extra.items() if isinstance(extra, dict) else []):
        if isinstance(spec, dict) and spec.get("kind") in KINDS and isinstance(spec.get("roots"), list):
            found[name] = {"kind": spec["kind"], "roots": [str(r) for r in spec["roots"]],
                           **({"account": str(spec["account"])} if spec.get("account") else {})}
    return found


# ------------------------------------------------------------------ readings of a window

def add_reading(series: list[list[float]], ts: float, used: float, resets: float) -> None:
    """Append [first seen, used %, reset time, last confirmed]; an unchanged reading only moves `last confirmed`."""
    if series and series[-1][1] == used and same_reset(series[-1][2], resets):
        series[-1][3] = max(series[-1][3], ts)
        return
    series.append([ts, used, resets, ts])


def merge_series(parts: list[list[list[float]]]) -> list[list[float]]:
    """Several sources' readings of one window as a single time-ordered series without repeats."""
    merged: list[list[float]] = []
    for row in sorted((r for part in parts for r in part), key=lambda r: r[0]):
        if merged and merged[-1][1] == row[1] and same_reset(merged[-1][2], row[2]):
            merged[-1][3] = max(merged[-1][3], row[3])
        else:
            merged.append(list(row))
    return merged


def reading_at(series: list[list[float]], times: list[float], moment: float):
    index = bisect.bisect_right(times, moment) - 1
    return series[index] if index >= 0 else None


# ------------------------------------------------------------------ incremental log scanning

def new_entry(path: Path, account: str) -> dict:
    return {"account": account, "path": str(path), "offset": 0, "prev": None, "total": 0, "model": None, "effort": None,
            "last_bundle": None, "last_t": None, "buckets": {}, "samples": {}, "ids": []}


def add_call(entry: dict, ts: float, bundle: str, tokens_in: int, tokens_out: int, usd: float = 0.0) -> None:
    prev = entry["prev"]
    active = 0.0 if prev is None else max(0.0, min(ts - prev, ACTIVE_GAP_SECONDS))
    entry["prev"] = entry["last_t"] = ts
    entry["last_bundle"] = bundle
    slot = entry["buckets"].setdefault(hour_key(ts), {}).setdefault(bundle, [0.0, 0, 0, 0, 0.0])
    slot[0] += active
    slot[1] += 1
    slot[2] += tokens_in
    slot[3] += tokens_out
    slot[4] += usd


def read_new_lines(path: Path, entry: dict):
    """Complete lines appended since the last run; the stored offset only moves past whole lines."""
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size < entry["offset"]:
        entry.update(new_entry(path, entry["account"]))   # rewritten or truncated: read this file again
    with open(path, "rb") as stream:
        stream.seek(entry["offset"])
        for raw in stream:
            if not raw.endswith(b"\n"):
                break
            entry["offset"] += len(raw)
            yield raw


def scan_codex(path: Path, entry: dict) -> None:
    for raw in read_new_lines(path, entry):
        if b'"turn_context"' in raw:
            try:
                payload = json.loads(raw).get("payload") or {}
            except ValueError:
                continue
            entry["model"] = payload.get("model") or entry["model"]
            entry["effort"] = payload.get("effort") or entry["effort"]
        elif b'"token_count"' in raw:
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            payload, ts = obj.get("payload") or {}, parse_ts(obj.get("timestamp"))
            if payload.get("type") != "token_count" or ts is None:
                continue
            limits = payload.get("rate_limits") or {}
            for slot_name in ("primary", "secondary"):
                limit = limits.get(slot_name) or {}
                if isinstance(limit.get("used_percent"), (int, float)) and isinstance(limit.get("resets_at"), (int, float)):
                    window = WINDOW_BY_MINUTES.get(limit.get("window_minutes"), f"usage-{limit.get('window_minutes')}m")
                    add_reading(entry["samples"].setdefault(window, []), ts, float(limit["used_percent"]), float(limit["resets_at"]))
            info = payload.get("info") or {}
            step, total = info.get("last_token_usage"), (info.get("total_token_usage") or {}).get("total_tokens")
            if step and isinstance(total, int) and total > entry["total"]:   # a repeated event does not move the total
                entry["total"] = total
                tokens_in, cached = int(step.get("input_tokens") or 0), int(step.get("cached_input_tokens") or 0)
                tokens_out = int(step.get("output_tokens") or 0)
                add_call(entry, ts, f"{entry['model'] or '?'}/{entry['effort'] or '?'}", tokens_in, tokens_out,
                         list_usd(entry["model"], fresh=max(0, tokens_in - cached), read=cached, out=tokens_out))


def scan_claude(path: Path, entry: dict, seen: set[str]) -> None:
    for raw in read_new_lines(path, entry):
        if b'"usage"' not in raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        usage, ts, model = message.get("usage"), parse_ts(obj.get("timestamp")), message.get("model")
        if not isinstance(usage, dict) or ts is None or not model or model.startswith("<"):
            continue
        identity = short_hash(str(message.get("id") or obj.get("uuid") or raw[:200]))
        if identity in seen:        # a streamed message repeats, and a resumed session copies earlier ones
            continue
        seen.add(identity)
        entry["ids"].append(identity)
        tokens_in = sum(usage.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        written = int(usage.get("cache_creation_input_tokens") or 0)
        split = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
        hour_writes = int(split.get("ephemeral_1h_input_tokens") or 0)
        add_call(entry, ts, model, int(tokens_in), int(usage.get("output_tokens") or 0),
                 list_usd(model, fresh=int(usage.get("input_tokens") or 0), write_5m=max(0, written - hour_writes),
                          write_1h=hour_writes, read=int(usage.get("cache_read_input_tokens") or 0),
                          out=int(usage.get("output_tokens") or 0)))


def scan_pi(path: Path, entry: dict, seen: set[str]) -> None:
    """Assistant messages from a Pi session; the effort is the session's thinking level."""
    only_codex = entry.get("account") == "codex"
    for raw in read_new_lines(path, entry):
        if b'"thinking_level_change"' not in raw and b'"usage"' not in raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if obj.get("type") == "thinking_level_change":
            entry["effort"] = obj.get("thinkingLevel") or entry["effort"]
            continue
        message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        usage, ts = message.get("usage"), parse_ts(obj.get("timestamp"))
        if (obj.get("type") != "message" or message.get("role") != "assistant"
                or (only_codex and message.get("provider") != "openai-codex")
                or not isinstance(usage, dict) or ts is None):
            continue
        identity = short_hash(f"{obj.get('id')}|{message.get('timestamp')}")
        if identity in seen:        # a forked session copies the entries before the fork
            continue
        seen.add(identity)
        entry["ids"].append(identity)
        tokens_in = sum(usage.get(k) or 0 for k in ("input", "cacheRead", "cacheWrite"))
        add_call(entry, ts, f"{message.get('model') or '?'}/{entry['effort'] or '?'}", int(tokens_in), int(usage.get("output") or 0),
                 list_usd(message.get("model"), fresh=int(usage.get("input") or 0), write_5m=int(usage.get("cacheWrite") or 0),
                          read=int(usage.get("cacheRead") or 0), out=int(usage.get("output") or 0)))


def scan_opencode(path: Path, entry: dict) -> None:
    """Assistant messages stored in OpenCode's opencode.db SQLite message table."""
    try:
        stamp = max((Path(p).stat().st_mtime_ns for p in (path, str(path) + "-wal") if Path(p).exists()), default=0)
    except OSError:
        return
    if stamp == entry["offset"]:
        return
    try:
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=1.0) as conn:
            rows = conn.execute(
                "SELECT time_created, data FROM message WHERE time_created > ? ORDER BY time_created ASC",
                (int(entry["total"]),),
            ).fetchall()
    except sqlite3.Error:
        return
    for time_created, raw_data in rows:
        entry["total"] = max(int(entry["total"]), int(time_created or 0))
        try:
            data = json.loads(raw_data)
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict) or data.get("role") != "assistant":
            continue
        ts = float(time_created) / 1000.0 if time_created and time_created > 1e11 else float(time_created or 0)
        if ts <= 0:
            continue
        tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
        fresh_in = int(tokens.get("input") or 0)
        cache_read = int(cache.get("read") or 0)
        cache_write = int(cache.get("write") or 0)
        tokens_in = fresh_in + cache_read + cache_write
        tokens_out = int(tokens.get("output") or 0) + int(tokens.get("reasoning") or 0)
        model = str(data.get("modelID") or data.get("model") or "?")
        cost = data.get("cost")
        usd = float(cost) if isinstance(cost, (int, float)) and cost > 0 else list_usd(
            model, fresh=fresh_in, write_5m=cache_write, read=cache_read, out=tokens_out
        )
        add_call(entry, ts, model, tokens_in, tokens_out, usd)
    entry["offset"] = stamp


def read_varint(buf: bytes, i: int):
    shift = value = 0
    while True:
        byte = buf[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, i
        shift += 7


def step_time(blob: bytes) -> int | None:
    """Earliest epoch-seconds value among the varints of a step's protobuf metadata, searched through nested messages."""
    found: list[int] = []

    def walk(buf: bytes, depth: int) -> None:
        i = 0
        try:
            while i < len(buf):
                key, i = read_varint(buf, i)
                wire = key & 7
                if wire == 0:
                    value, i = read_varint(buf, i)
                    if STEP_TIME_RANGE[0] <= value <= STEP_TIME_RANGE[1]:
                        found.append(value)
                elif wire == 1:
                    i += 8
                elif wire == 5:
                    i += 4
                elif wire == 2:
                    size, i = read_varint(buf, i)
                    if depth < 5 and size > 1:
                        walk(buf[i:i + size], depth + 1)
                    i += size
                else:
                    return
        except IndexError:
            return

    walk(blob, 0)
    return min(found) if found else None


def conversation_model(connection: sqlite3.Connection) -> str | None:
    """The model a conversation mostly ran: the commonest model id with an effort suffix matching the dominant base model."""
    counts: collections.Counter = collections.Counter()
    for table in ("gen_metadata", "executor_metadata"):
        try:
            for (data,) in connection.execute(f"select data from {table}"):
                blob = bytes(data or b"")
                if len(blob) > 4096:
                    blob = blob[:2048] + blob[-2048:]
                counts.update(m.decode() for m in AGY_MODEL.findall(blob))
        except sqlite3.Error:
            continue
    if not counts:
        return None
    base_counts: collections.Counter = collections.Counter()
    for m, n in counts.items():
        base_counts[AGY_EFFORT.sub("", m)] += n
    top_base = base_counts.most_common(1)[0][0]
    suffixed = collections.Counter({m: n for m, n in counts.items() if AGY_EFFORT.search(m) and AGY_EFFORT.sub("", m) == top_base})
    if not suffixed:
        suffixed = collections.Counter({m: n for m, n in counts.items() if AGY_EFFORT.search(m) or m == top_base})
    return (suffixed or counts).most_common(1)[0][0]


def scan_antigravity(path: Path, entry: dict) -> None:
    """New steps of one conversation database; each step is a call at its earliest timestamp (no tokens are recorded).

    The database is copied with its write-ahead log first because the CLI keeps it open. If the copy cannot be read
    (a write was in flight) nothing is recorded and the unchanged stamp makes the next run try again."""
    wal = Path(str(path) + "-wal")
    try:
        stamp = max(path.stat().st_mtime_ns, wal.stat().st_mtime_ns if wal.exists() else 0)
    except OSError:
        return
    if stamp == entry["offset"]:
        return
    with tempfile.TemporaryDirectory() as scratch:
        copy = Path(scratch) / path.name
        try:
            for suffix in ("", "-wal", "-shm"):
                source = Path(str(path) + suffix)
                if source.exists():
                    shutil.copy2(source, str(copy) + suffix)
            connection = sqlite3.connect(str(copy))
            try:
                rows = connection.execute("select idx, metadata from steps where idx >= ? order by idx", (entry["total"],)).fetchall()
                model = conversation_model(connection) if rows else None
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            return
    entry["model"] = model or entry["model"]
    for idx, blob in rows:
        entry["total"] = idx + 1
        ts = step_time(bytes(blob)) if blob else None
        if ts is not None:
            add_call(entry, float(ts), entry["model"] or "?", 0, 0)
    entry["offset"] = stamp


def discover(spec: dict, cutoff: float):
    """(identity, path, mtime) of log files touched since the cutoff. A Codex rollout keeps one identity if archived."""
    names: set[str] = set()
    suffix = ".db" if spec["kind"] in ("antigravity", "opencode") else ".jsonl"
    for root in spec["roots"]:
        base = Path(root)
        if not base.is_dir():
            continue
        for directory, _, files in os.walk(base):
            for name in files:
                if (not name.endswith(suffix)
                        or (spec["kind"] == "codex" and not name.startswith("rollout-"))
                        or (spec["kind"] == "opencode" and name != "opencode.db")):
                    continue
                path = Path(directory) / name
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if spec["kind"] in ("antigravity", "opencode"):
                    try:
                        mtime = max(mtime, Path(str(path) + "-wal").stat().st_mtime)   # recent steps sit in the write-ahead log
                    except OSError:
                        pass
                identity = name if spec["kind"] == "codex" else str(path)
                if mtime >= cutoff and identity not in names:
                    names.add(identity)
                    yield identity, path, mtime


def scan_all(state: dict, now: float, days: int) -> dict:
    cutoff = now - days * 86400
    touched: set[str] = set()
    scanned = 0
    for name, spec in sources().items():
        account = spec.get("account", name)
        seen = {i for k, e in state["files"].items() if k.startswith(name + "|") for i in e["ids"]}
        for identity, path, _ in sorted(discover(spec, cutoff), key=lambda item: item[2]):
            key = f"{name}|{identity}"
            touched.add(key)
            entry = state["files"].setdefault(key, new_entry(path, account))
            entry["path"] = str(path)
            before = entry["offset"]
            if spec["kind"] == "codex":
                scan_codex(path, entry)
            elif spec["kind"] == "antigravity":
                scan_antigravity(path, entry)
            elif spec["kind"] == "opencode":
                scan_opencode(path, entry)
            elif spec["kind"] == "pi":
                scan_pi(path, entry, seen)
            else:
                scan_claude(path, entry, seen)
            scanned += entry["offset"] != before
    for key in [k for k in state["files"] if k not in touched]:
        del state["files"][key]          # outside the lookback, or no longer present
    return {"files": len(touched), "scanned": scanned}


def sample_usage_cache(state: dict) -> None:
    """Record each account's window readings from the usage-float cache whenever its fetched_at moves on."""
    try:
        providers = json.loads(USAGE_CACHE.read_text(encoding="utf-8-sig")).get("providers") or {}
    except (OSError, ValueError, AttributeError):
        return
    for account, row in providers.items():
        fetched = parse_ts((row or {}).get("fetched_at"))
        if fetched is None:
            continue
        for window in row.get("windows") or []:
            used, resets = window.get("used_pct"), parse_ts(window.get("resets_at"))
            wid = "usage" if account.startswith("codex") and window.get("id") == "seven_day" else window.get("id")
            if isinstance(used, (int, float)) and resets is not None and wid:
                series = state["usage_samples"].setdefault(account, {}).setdefault(wid, [])
                add_reading(series, fetched, float(used), resets)


def prune(state: dict, cutoff: float) -> None:
    old = hour_key(cutoff)
    for entry in state["files"].values():
        for hour in [h for h in entry["buckets"] if h < old]:
            del entry["buckets"][hour]
        for window, series in entry["samples"].items():
            entry["samples"][window] = [s for s in series if s[3] >= cutoff]
    for windows in state["usage_samples"].values():
        for window, series in windows.items():
            windows[window] = [s for s in series if s[3] >= cutoff]


def load_state() -> dict:
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if state.get("schema") == SCHEMA:
            if state.get("price_version") != PRICE_VERSION:
                # Calls scanned before prices existed carry none: read the logs again. The usage-float samples are
                # kept, because they are Claude's only window history and cannot be read back.
                state.update(files={}, price_version=PRICE_VERSION)
            return state
    except (OSError, ValueError):
        pass
    return {"schema": SCHEMA, "price_version": PRICE_VERSION, "files": {}, "usage_samples": {}}


def atomic_write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False, separators=(",", ":"))
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


# ------------------------------------------------------------------ measurement

def account_series(state: dict, account: str) -> dict[str, list[list[float]]]:
    """Readings per window from the logs and the usage-float cache, merged in time order."""
    parts: dict[str, list] = collections.defaultdict(list)
    for entry in state["files"].values():
        if entry["account"] == account:
            for window, series in entry["samples"].items():
                parts[window].append(series)
    for window, series in (state["usage_samples"].get(account) or {}).items():
        wid = "usage" if account.startswith("codex") and window == "seven_day" else window
        parts[wid].append(series)
    merged = {window: merge_series(lists) for window, lists in parts.items()}
    return {window: series for window, series in merged.items() if series}


def usage_by_hour(state: dict, account: str) -> dict[str, dict[str, list[float]]]:
    """{hour: {bundle: [active worker-hours, input tokens, output tokens, list-price dollars]}} across every file of the account."""
    table: dict[str, dict[str, list[float]]] = collections.defaultdict(lambda: collections.defaultdict(lambda: [0.0, 0.0, 0.0, 0.0]))
    for entry in state["files"].values():
        if entry["account"] == account:
            for hour, bundles in entry["buckets"].items():
                for bundle, slot in bundles.items():
                    row = table[hour][bundle]
                    row[0] += slot[0] / 3600
                    row[1] += slot[2]
                    row[2] += slot[3]
                    row[3] += slot[4] if len(slot) > 4 else 0.0
    return table


def rate_in(series: list[list[float]], now: float, hours: float):
    """Percent per hour over the last `hours` inside the current reset period, or None when it cannot be known."""
    times = [s[0] for s in series]
    latest = reading_at(series, times, now)
    if latest is None or latest[2] <= now or now - latest[3] > STALE_SAMPLE_SECONDS:
        return None
    earlier = reading_at(series, times, now - hours * 3600)
    if earlier is None or not same_reset(earlier[2], latest[2]):
        return None
    return round(max(0.0, latest[1] - earlier[1]) / hours, 4)


def measure(state: dict, now: float, days: int = LOOKBACK_DAYS) -> dict:
    document = {"v": SCHEMA, "generated_at": iso(now), "lookback_days": days, "observed": {}, "active_workers": {},
                "worker_hours": {}}
    accounts = {e["account"] for e in state["files"].values()} | set(state["usage_samples"])
    for account in sorted(accounts):
        table = usage_by_hour(state, account)
        if table:
            spans = {span: sum(row[0] for hour, bundles in table.items() if key_epoch(hour) >= now - span * 3600
                               for row in bundles.values()) for span in (24, 168)}
            document["worker_hours"][account] = {"24h": round(spans[24], 2), "7d": round(spans[168], 2)}
        for window, series in account_series(state, account).items():
            latest = series[-1]
            document["observed"].setdefault(account, {})[window] = {
                "used_pct": latest[1], "resets_at": iso(latest[2]), "confirmed_at": iso(latest[3]),
                "pct_per_hour_3h": rate_in(series, now, OBSERVE_SHORT_HOURS),
                "pct_per_hour_24h": rate_in(series, now, OBSERVE_LONG_HOURS)}
    # Usage Float owns live activity. These log scans still measure costs and
    # worker-hours, but must not maintain a second definition of running/idle.
    activity_path = Path(os.environ.get('ROUTE_MANAGER_ACTIVITY_FILE') or STATE_DIR / 'usage-activity.json')
    document['activity_status'] = 'unknown'
    try:
        activity = json.loads(activity_path.read_text(encoding='utf-8-sig'))
        observed = parse_ts(activity.get('generated_at'))
        if (activity.get('schema') == 'route-manager.activity.v1' and observed is not None
                and 0 <= now - observed <= 180 and isinstance(activity.get('active_workers'), dict)):
            document['active_workers'] = activity['active_workers']
            document['activity_generated_at'] = activity['generated_at']
            document['activity_status'] = 'observed'
    except (OSError, ValueError, TypeError):
        pass
    return document


def daily_stats(state: dict, now: float, days: int = LOOKBACK_DAYS) -> dict:
    """Worker-hours, calls and tokens by local day, account and model/effort."""
    zone = datetime.now().astimezone().tzinfo
    first = hour_key(now - days * 86400)
    table: dict = {}
    agents: dict = collections.defaultdict(set)                          # (day, account) -> files that worked that day
    per_hour: dict = collections.defaultdict(lambda: collections.defaultdict(float))   # (day, account) -> hour -> hours
    for key, entry in state["files"].items():
        for hour, bundles in entry["buckets"].items():
            if hour < first:
                continue
            day = datetime.fromtimestamp(key_epoch(hour), UTC).astimezone(zone).date().isoformat()
            for bundle, slot in bundles.items():
                seconds, calls, tokens_in, tokens_out = slot[:4]
                row = table.setdefault(day, {}).setdefault(entry["account"], {}).setdefault(bundle, [0.0, 0, 0, 0, 0.0])
                row[0] += seconds / 3600
                row[1] += calls
                row[2] += tokens_in
                row[3] += tokens_out
                row[4] += slot[4] if len(slot) > 4 else 0.0
                if seconds > 0:
                    agents[(day, entry["account"])].add(key)
                per_hour[(day, entry["account"])][hour] += seconds / 3600
    days_out, crowd = {}, {}
    for day, accounts_ in sorted(table.items()):
        days_out[day] = {}
        for account, bundles in accounts_.items():
            hours = per_hour[(day, account)]
            days_out[day][account] = {bundle: {"worker_hours": round(v[0], 3), "calls": v[1], "input_tokens": v[2],
                                               "output_tokens": v[3], "list_usd": round(v[4], 2)} for bundle, v in bundles.items()}
            # Agents that worked, and the busiest hour's mean concurrency (active hours in one clock hour = agents running).
            crowd.setdefault(day, {})[account] = {"agents": len(agents[(day, account)]),
                                                  "peak_hour_concurrency": round(max(hours.values(), default=0.0), 2)}
    return {"v": SCHEMA, "generated_at": iso(now),
            "unit": f"worker-hours = actively running session time; gaps over {ACTIVE_GAP_SECONDS}s are idle",
            "days": days_out, "agents": crowd}


# ------------------------------------------------------------------ run

def pid_alive(pid: int) -> bool:
    if pid <= 0 or pid == os.getpid():
        return False
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def acquire_lock(now: float) -> bool:
    try:
        held = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
        if now - float(held.get("at", 0)) < LOCK_SECONDS and pid_alive(int(held.get("pid", 0))):
            return False
    except (OSError, ValueError, TypeError):
        pass
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(json.dumps({"pid": os.getpid(), "at": now}), encoding="utf-8")
    return True


def refresh(now: float | None = None, days: int = LOOKBACK_DAYS, write: bool = True, full: bool = False) -> dict:
    now = time.time() if now is None else now
    state = {"schema": SCHEMA, "price_version": PRICE_VERSION, "files": {}, "usage_samples": {}} if full else load_state()
    counts = scan_all(state, now, days)
    sample_usage_cache(state)
    prune(state, now - days * 86400)
    document = measure(state, now, days)
    document["scan"] = counts
    if write:
        atomic_write(STATE_FILE, state)
        atomic_write(STATS_FILE, daily_stats(state, now, days))
        atomic_write(OUTPUT, document)
    return document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="accepted for the tray; a run is always one pass")
    parser.add_argument("--full", action="store_true", help="discard the saved scan state and read the logs again")
    parser.add_argument("--days", type=int, default=LOOKBACK_DAYS)
    parser.add_argument("--no-write", action="store_true", help="print the result without saving anything")
    parser.add_argument("--quiet", action="store_true", help="print nothing on success (the tray logs only failures)")
    args = parser.parse_args()
    now = time.time()
    if not args.no_write and not acquire_lock(now):
        print("another run holds the lock")
        return 0
    try:
        document = refresh(now, args.days, not args.no_write, args.full)
    finally:
        if not args.no_write:
            try:
                LOCK_FILE.unlink()
            except OSError:
                pass
    if sys.stdout is not None and not args.quiet:
        print(json.dumps({k: document[k] for k in ("generated_at", "worker_hours", "observed", "active_workers", "scan")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
