"""Maintainer calibration report over recorded dispatch outcomes. Read-only; spends no model tokens.

Reads `dispatch.sqlite3` (the rows `dispatch_ground_truth.py` completes) and `router-ledger.jsonl` from the state
directory and compares what the classifier and the allocator expected with what the dispatches did: classifier hours
against active duration, estimated spend against recorded consumption, and how dispatches ended. Every cell carries its
sample size, and a cell under `MIN_CELL` samples is marked too small to read.

    python audit/calibration_report.py [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--state-dir DIR] [--out DIR]

Read-only means the whole run: the report writes only its own two output files and nothing else, no discovery, no
cache, no state. Prices and capacities come from the shipped reference data plus the plan facts already recorded in the
state directory's `provider-inventory.json` (read, never rewritten). The output goes to
`<state dir>/calibration/<run date>/` unless `--out` names another directory (the one place a caller may point it
elsewhere). A missing state directory or an unreadable database is an error, never created or guessed; unreadable
rows and ledger lines are counted in the Coverage block.

The report holds only counts, medians and group names: no task text, no file paths, no agent or dispatch ids, no
credentials. It never writes a routing input. Recorded outcomes never reach routing at runtime; the maintainer reads the
report and edits shipped reference data by hand.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import math
import os
import sqlite3
import statistics
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "agent-compass" / "scripts"
UTC = dt.timezone.utc

MIN_CELL = 5                   # samples a cell needs before its median is read; below it the cell is marked
DEFAULT_DAYS = 30
AXES = ("divergence", "coding", "aesthetics", "tool_operation")
ROUTED, PINNED, KEPT_WHOLE = "routed", "pinned", "keep_whole"
GROUPS = (ROUTED, PINNED, KEPT_WHOLE)
OPEN_STATES = ("prepared", "launching", "launched", "started")
ENDINGS = ("completed", "failed", "relaunched", "canceled", "other_terminal", "open")
USER_PIN = "user_override"
NON_DISPATCH_CHOICES = (None, "none", "self", "split")   # a decision that deliberately starts no worker
DECISION_STAGES = ("plan", "select", "record")


class ReportError(Exception):
    """A named input problem the maintainer must fix: the report stops and says which file."""


def parse_ts(value):
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def day_of(value):
    parsed = parse_ts(value)
    return parsed.date() if parsed else None


def number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


# ---------------------------------------------------------------- reading

def read_dispatches(db: Path):
    """(dispatch rows, {decision_id: {constraint, worker}}, skipped counts), read-only.

    Only the constraint of a decision and the id of the worker it continued are kept, never its task. A row that is not
    a JSON object is skipped and counted."""
    if not db.is_file():
        raise ReportError(f"{db}: dispatch database not found")
    skipped = collections.Counter()
    try:
        connection = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            try:
                raw_rows = [r[0] for r in connection.execute("SELECT data FROM dispatches")]
            except sqlite3.OperationalError as exc:
                raise ReportError(f"{db}: no readable dispatches table ({exc})") from exc
            try:
                raw_decisions = [r[0] for r in connection.execute("SELECT data FROM decisions")]
            except sqlite3.OperationalError:
                raw_decisions = []
                skipped["decisions_table_missing"] = 1
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        raise ReportError(f"{db}: not a readable SQLite database ({exc})") from exc

    def objects(raw, label):
        for text in raw:
            try:
                value = json.loads(text)
            except (ValueError, TypeError):
                value = None
            if isinstance(value, dict):
                yield value
            else:
                skipped[label] += 1

    rows = list(objects(raw_rows, "dispatch_rows_unreadable"))
    decisions = {}
    for decision in objects(raw_decisions, "decision_rows_unreadable"):
        task = decision.get("allocation_task")
        chain = task.get("chain") if isinstance(task, dict) else None
        worker = chain.get("worker") if isinstance(chain, dict) else None
        decisions[decision.get("decision_id")] = {
            "constraint": decision.get("constraint"),
            "worker": worker.get("agent_id") if isinstance(worker, dict) else None}
    return rows, decisions, skipped


def read_ledger(path: Path):
    """(decision lines by id, all lines, skipped count). A line that is not valid UTF-8 JSON is skipped and counted;
    the skipped count is None when the ledger file cannot be read at all."""
    try:
        data = path.read_bytes()
    except OSError:
        return {}, [], None
    lines, skipped = [], 0
    for raw in data.splitlines():
        if not raw.strip():
            continue
        try:
            row = json.loads(raw.decode("utf-8-sig"))
        except (ValueError, UnicodeDecodeError):
            skipped += 1
            continue
        if isinstance(row, dict):
            lines.append(row)
        else:
            skipped += 1
    by_decision = {row["decision_id"]: row for row in lines
                   if row.get("decision_id") and row.get("stage") in DECISION_STAGES}
    return by_decision, lines, skipped


# ---------------------------------------------------------------- normalizing

def demand_label(profile):
    """The category (or categories, on a tie) the classifier demanded most; `none` when every demand is zero."""
    demand = (profile or {}).get("axis_demand")
    if not isinstance(demand, dict):
        return "unclassified"
    peak = max((number(demand.get(a)) or 0 for a in AXES), default=0)
    if peak <= 0:
        return "none"
    return "+".join(a for a in AXES if (number(demand.get(a)) or 0) == peak)


def token_parts(usage):
    """(fresh tokens, cache-read tokens, cache-write tokens) of a recorded `token_usage`, None where not recorded.
    Fresh = input + output."""
    if not isinstance(usage, dict):
        return None, None, None
    fresh = [number(usage.get(k)) for k in ("input_tokens", "output_tokens")]
    fresh = [p for p in fresh if p is not None]
    return (sum(fresh) if fresh else None), number(usage.get("cache_read_tokens")), number(usage.get("cache_write_tokens"))


def list_price_usd(usage, price):
    """(USD, lower_bound) for recorded tokens at the model's public list price (USD per million tokens: input, output,
    cache_read, and cache_write when the data lists one). Cache-write tokens the data prices are included; where it
    lists no cache-write price they are left out and the result is marked a lower bound, never given an invented price.
    (None, False) when nothing was recorded or a recorded kind of token has no listed price."""
    if not isinstance(usage, dict) or not isinstance(price, dict):
        return None, False
    total, seen, lower = 0.0, False, False
    for tokens, rate in (("input_tokens", "input"), ("output_tokens", "output"),
                         ("cache_read_tokens", "cache_read"), ("cache_write_tokens", "cache_write")):
        count, per_million = number(usage.get(tokens)), number(price.get(rate))
        if count is None or (count == 0 and rate == "cache_write"):
            continue
        if per_million is None:
            if rate == "cache_write":
                lower = True
                continue
            return None, False
        total += count * per_million
        seen = True
    return (total / 1_000_000, lower) if seen else (None, False)


def ending(row, successors):
    state = row.get("state")
    if state == "completed":
        return "completed"
    if state == "failed":
        return "relaunched" if row.get("dispatch_id") in successors else "failed"
    if state == "canceled":
        return "canceled"
    if state in OPEN_STATES:
        return "open"
    return "other_terminal"


def rotated_away(rows, decisions, ledger):
    """Dispatch ids of the workers a context rotation replaced: the newest earlier dispatch of the worker the rotating
    decision continued. Returns (ids, rotations whose replaced worker could not be found)."""
    away, unlinked = set(), 0
    for row in rows:
        if (ledger.get(row.get("decision_id")) or {}).get("action") != "rotate":
            continue
        worker = (decisions.get(row.get("decision_id")) or {}).get("worker")
        created = parse_ts(row.get("created_at"))
        earlier = [r for r in rows if worker and r.get("agent_id") == worker and r is not row and created is not None
                   and (t := parse_ts(r.get("created_at"))) is not None and t < created]
        if earlier:
            away.add(max(earlier, key=lambda r: parse_ts(r["created_at"])).get("dispatch_id"))
        else:
            unlinked += 1
    return away, unlinked


def normalize(rows, decisions, ledger, pricing, since, until, away):
    """One flat record per dispatch in the date range: grouping keys and measurements, nothing that identifies a task."""
    successors = {r.get("previous_attempt") for r in rows if r.get("previous_attempt")}
    records = []
    for row in rows:
        day = day_of(row.get("created_at"))
        if day is None or not since <= day <= until:
            continue
        decision_id = row.get("decision_id")
        line = ledger.get(decision_id) or {}
        profile = row.get("profile") if isinstance(row.get("profile"), dict) else {}
        bundle = row.get("bundle") if isinstance(row.get("bundle"), dict) else {}
        pinned = USER_PIN in ((decisions.get(decision_id) or {}).get("constraint"), line.get("constraint"))
        kept = bool(line.get("keep_whole"))
        active = number(row.get("active_seconds"))
        price = (pricing(bundle) if pricing else None) or {}
        capacity = number(price.get("capacity_usd"))
        actual_usd, lower_bound = list_price_usd(row.get("token_usage"), price.get("list_price"))
        fresh, cached, written = token_parts(row.get("token_usage"))
        active_hours = active / 3600 if active is not None else None
        records.append({
            "group": PINNED if pinned else KEPT_WHOLE if kept else ROUTED,
            "pinned_and_kept": pinned and kept,
            "ending": ending(row, successors),
            "mode": row.get("dispatch_mode", "paseo"),
            "harness": row.get("telemetry_harness"),
            "work_kind": profile.get("work_kind") or "unclassified",
            "demand": demand_label(profile),
            "model": bundle.get("model") or "unknown",
            "effort": bundle.get("effort") or "none",
            "rotated": line.get("action") == "rotate",
            "rotated_away": row.get("dispatch_id") in away,
            "chained": bool(row.get("attempt_of")),
            "followups": row.get("followup_turns"),
            "recorded": "ground_truth_recorded_at" in row,
            "expected_hours": number(profile.get("expected_hours")),
            "active_hours": active_hours,
            "active_reason": row.get("active_seconds_reason"),
            "fresh_tokens": fresh,
            "cache_read_tokens": cached,
            "cache_write_tokens": written,
            "expected_usd": number(row.get("expected_usd")),
            "actual_usd": actual_usd,
            "lower_bound": lower_bound,
            "usd_per_hour_rate": number(price.get("usd_per_hour")),
            "actual_usd_per_hour": actual_usd / active_hours if actual_usd is not None and active_hours else None,
            "window": price.get("window_id"),
            "window_share": 100 * actual_usd / capacity if actual_usd is not None and capacity else None,
        })
    return records


# ---------------------------------------------------------------- aggregating

def median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def quartiles(values):
    values = sorted(v for v in values if v is not None)
    if len(values) < MIN_CELL:
        return None, None
    cut = statistics.quantiles(values, n=4, method="inclusive")
    return cut[0], cut[2]


def ratio(a, b):
    return a / b if a is not None and b else None


def cell(records, key):
    return {name: [r for r in records if key(r) == name] for name in sorted({key(r) for r in records}, key=str)}


def hours_cell(records):
    """Expected against actual hours. `ratio_median` is the median of the per-dispatch actual / expected, not the ratio
    of the two medians. A retried task's later attempt is left out: only the completed attempt carries active time, so
    its hours would read low against the whole task's estimate."""
    paired = [r for r in records if r["expected_hours"] and r["active_hours"] is not None and not r["chained"]]
    ratios = [ratio(r["active_hours"], r["expected_hours"]) for r in paired]
    low, high = quartiles(ratios)
    return {"n": len(paired), "small": len(paired) < MIN_CELL,
            "expected_hours_median": median(r["expected_hours"] for r in paired),
            "active_hours_median": median(r["active_hours"] for r in paired),
            "ratio_median": median(ratios), "ratio_q1": low, "ratio_q3": high,
            "with_followups": sum(1 for r in paired if r["followups"]),
            "retried_left_out": sum(1 for r in records if r["chained"] and r["active_hours"] is not None)}


def spend_cell(records):
    """Two separate errors. `ratio_median` (actual / expected USD) mixes the hours error and the rate error; the rate
    columns do not depend on the hours: recorded list-price USD per active hour against the `usd_per_hour` the estimate
    used. USD that omits unpriced cache-write tokens is a lower bound, and the cell says how many rows are."""
    priced = [r for r in records if r["expected_usd"] and r["actual_usd"] is not None and not r["chained"]]
    ratios = [ratio(r["actual_usd"], r["expected_usd"]) for r in priced]
    low, high = quartiles(ratios)
    rated = [r for r in priced if r["actual_usd_per_hour"] is not None and r["usd_per_hour_rate"]]
    rate_ratios = [ratio(r["actual_usd_per_hour"], r["usd_per_hour_rate"]) for r in rated]
    rate_low, rate_high = quartiles(rate_ratios)
    tokens = [r for r in records if r["fresh_tokens"] is not None]
    windows = collections.Counter(r["window"] for r in priced if r["window"] and r["window_share"] is not None)
    return {"n": len(priced), "small": len(priced) < MIN_CELL,
            "expected_usd_median": median(r["expected_usd"] for r in priced),
            "actual_usd_median": median(r["actual_usd"] for r in priced),
            "lower_bound_rows": sum(1 for r in priced if r["lower_bound"]),
            "ratio_median": median(ratios), "ratio_q1": low, "ratio_q3": high,
            "rate_n": len(rated), "rate_small": len(rated) < MIN_CELL,
            "usd_per_hour_rate": median(r["usd_per_hour_rate"] for r in rated),
            "actual_usd_per_active_hour_median": median(r["actual_usd_per_hour"] for r in rated),
            "rate_ratio_median": median(rate_ratios), "rate_ratio_q1": rate_low, "rate_ratio_q3": rate_high,
            "window": windows.most_common(1)[0][0] if windows else None,
            "window_share_pct_median": median(r["window_share"] for r in priced),
            "tokens_n": len(tokens), "tokens_small": len(tokens) < MIN_CELL,
            "fresh_tokens_median": median(r["fresh_tokens"] for r in tokens),
            "cache_read_tokens_median": median(r["cache_read_tokens"] for r in tokens),
            "cache_write_tokens_median": median(r["cache_write_tokens"] for r in tokens),
            "retried_left_out": sum(1 for r in records if r["chained"] and r["actual_usd"] is not None)}


def ending_cell(records):
    counts = collections.Counter(r["ending"] for r in records)
    return {"n": len(records), "small": len(records) < MIN_CELL,
            **{name: counts.get(name, 0) for name in ENDINGS},
            "started_by_rotation": sum(1 for r in records if r["rotated"]),
            "rotated_away": sum(1 for r in records if r["rotated_away"])}


def model_key(r):
    return f"{r['model']}@{r['effort']}"


def section(records, key, builder):
    return {name: builder(rows) for name, rows in cell(records, key).items()}


def coverage(records, ledger_lines, all_rows, since, until, skipped, ledger_skipped, unlinked_rotations):
    """Where a dispatch left no measurement, by dispatch path, and which inputs could not be read."""
    done = [r for r in records if r["ending"] == "completed"]
    by_path = collections.Counter(f"{r['mode']}/{r['harness'] or 'no telemetry harness'}" for r in done)
    launched = {r.get("decision_id") for r in all_rows}
    planned = {}
    for line in ledger_lines:
        if line.get("decision_id") and line.get("stage") in DECISION_STAGES:
            day = day_of(line.get("at"))
            if day is not None and since <= day <= until:
                planned[line["decision_id"]] = line
    deliberate = {d for d, line in planned.items() if line.get("choice") in NON_DISPATCH_CHOICES}
    unreadable = dict(skipped)
    if ledger_skipped:
        unreadable["ledger_lines_unreadable"] = ledger_skipped
    if ledger_skipped is None:
        unreadable["ledger_missing"] = 1
    return {
        "dispatches": len(records),
        "completed": len(done),
        "completed_without_ground_truth": sum(1 for r in done if not r["recorded"]),
        "completed_without_active_time": sum(1 for r in done if r["active_hours"] is None),
        "completed_without_tokens": sum(1 for r in done if r["fresh_tokens"] is None),
        "completed_by_path": dict(sorted(by_path.items())),
        "missing_active_time_reasons": dict(collections.Counter(r["active_reason"] for r in done if r["active_hours"] is None)),
        "failed_or_canceled_with_measurement": sum(1 for r in records if r["ending"] in ("failed", "relaunched", "canceled")
                                                  and (r["active_hours"] is not None or r["fresh_tokens"] is not None)),
        "failed_or_canceled": sum(1 for r in records if r["ending"] in ("failed", "relaunched", "canceled")),
        "completed_without_window_share": sum(1 for r in done if r["actual_usd"] is not None and r["window_share"] is None),
        "ledger_decisions": len(planned) - len(deliberate),
        "ledger_decisions_without_dispatch": len(set(planned) - deliberate - launched),
        "ledger_deliberate_non_dispatches": len(deliberate),
        "rotations_without_predecessor": unlinked_rotations,
        "skipped": unreadable,
        "rows_without_date": sum(1 for r in all_rows if day_of(r.get("created_at")) is None),
    }


def build(db, ledger_path, since, until, pricing):
    all_rows, decisions, skipped = read_dispatches(db)
    ledger, lines, ledger_skipped = read_ledger(ledger_path)
    away, unlinked = rotated_away(all_rows, decisions, ledger)
    records = normalize(all_rows, decisions, ledger, pricing, since, until, away)
    routed = [r for r in records if r["group"] == ROUTED]
    return {
        "range": {"since": since.isoformat(), "until": until.isoformat()},
        "min_cell": MIN_CELL,
        "coverage": coverage(records, lines, all_rows, since, until, skipped, ledger_skipped, unlinked),
        "groups": {g: sum(1 for r in records if r["group"] == g) for g in GROUPS}
                  | {"pinned_and_kept_whole": sum(1 for r in records if r["pinned_and_kept"])},
        "hours": {"by_work_kind": section(routed, lambda r: r["work_kind"], hours_cell),
                  "by_demand": section(routed, lambda r: r["demand"], hours_cell)},
        "spend": {"by_model_effort": section(routed, model_key, spend_cell)},
        "endings": {"by_model_effort": section(routed, model_key, ending_cell),
                    "by_work_kind": section(routed, lambda r: r["work_kind"], ending_cell)},
        "own_groups": {g: {"hours": hours_cell(rows), "spend": spend_cell(rows), "endings": ending_cell(rows)}
                       for g in (PINNED, KEPT_WHOLE) if (rows := [r for r in records if r["group"] == g])},
    }


# ---------------------------------------------------------------- pricing

def accounts_pricing(state: Path):
    """bundle -> {list_price, usd_per_hour, window_id, capacity_usd}, with no write anywhere.

    Prices come from the shipped `accounts.json` and `five-axis-scores.json` (`references/`), not from fleet discovery,
    which rewrites `provider-inventory.json`. Plan facts (which plan an account is on, hence its window capacity) come from
    the state directory's existing `provider-inventory.json`, read and never rewritten; without it a window's capacity is
    unknown and its share is left blank. `list_price` is the model's public price in USD per million tokens and
    `usd_per_hour` the rate `expected_usd` is estimated on. The window is the first long window the bundle draws that
    has a cited capacity."""
    sys.path.insert(0, str(SCRIPTS))
    import account_fleet
    import axis_scores
    import provider_inventory
    import quota_economy
    import quota_pools
    accounts = account_fleet.load_accounts(path=account_fleet.REFERENCES / "accounts.json")
    try:
        inventory = json.loads((state / "provider-inventory.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        inventory = None
    if isinstance(inventory, dict):
        provider_inventory.apply(accounts, inventory)
    accounts = axis_scores.five_axis_overlay(accounts, read_local_state=False)
    short = (accounts.get("scoring") or {}).get("short_window_hours")
    specs = accounts.get("accounts") or {}

    def price(bundle):
        if not bundle.get("model"):
            return None
        spec = specs.get(bundle.get("account")) or {}
        info = account_fleet.model_spec(accounts, bundle["model"])
        window = capacity = None
        for wid in quota_pools.bundle_pools(bundle, accounts):
            wspec = (spec.get("windows") or {}).get(wid) or {}
            cap = quota_economy.capacity_usd(spec, wid)
            if cap and (short is None or wspec.get("hours", 168) >= short):
                window, capacity = wid, cap
                break
        list_price = ((info.get("five_axis") or {}).get("list_price") or {}).get("value")
        return {"list_price": list_price, "usd_per_hour": info.get("usd_per_hour"), "window_id": window,
                "capacity_usd": capacity}
    return price


# ---------------------------------------------------------------- rendering

def fmt(value, digits=2):
    return "-" if value is None else f"{value:.{digits}f}"


def mark(cell_):
    return f"{cell_['n']}{' (too small)' if cell_['small'] else ''}"


def table(header, body):
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in body]
    return out


def quarts(low, high):
    return f"{fmt(low)}-{fmt(high)}" if low is not None else "-"


def hours_rows(block):
    return [[k, mark(c), fmt(c["expected_hours_median"]), fmt(c["active_hours_median"]), fmt(c["ratio_median"]),
             quarts(c["ratio_q1"], c["ratio_q3"]), c["with_followups"], c["retried_left_out"]]
            for k, c in block.items() if c["n"] or c["retried_left_out"]]


def usd(c):
    value = fmt(c["actual_usd_median"])
    return f">={value} ({c['lower_bound_rows']}/{c['n']} rows)" if c["lower_bound_rows"] and value != "-" else value


def spend_rows(block):
    return [[k, mark(c), fmt(c["expected_usd_median"]), usd(c), fmt(c["ratio_median"]),
             f"{c['rate_n']}{' (too small)' if c['rate_small'] else ''}", fmt(c["usd_per_hour_rate"]),
             fmt(c["actual_usd_per_active_hour_median"]), fmt(c["rate_ratio_median"]),
             quarts(c["rate_ratio_q1"], c["rate_ratio_q3"]), c["window"] or "-", fmt(c["window_share_pct_median"]),
             f"{c['tokens_n']}{' (too small)' if c['tokens_small'] else ''}", fmt(c["fresh_tokens_median"], 0),
             fmt(c["cache_read_tokens_median"], 0), fmt(c["cache_write_tokens_median"], 0), c["retried_left_out"]]
            for k, c in block.items() if c["n"] or c["tokens_n"] or c["retried_left_out"]]


def render(report):
    cov, rng = report["coverage"], report["range"]
    skipped = ", ".join(f"{k} {v}" for k, v in cov["skipped"].items()) or "none"
    out = [f"# Calibration report {rng['since']} to {rng['until']}", "",
           f"Cells under {report['min_cell']} samples are marked too small to read. Ratios are actual / expected, each cell "
           "the median of the per-dispatch ratios (not the ratio of the two medians). A retried task's later attempts are left "
           "out of the hours and spend cells, because only the completed attempt carries active time. Tables cover routed "
           "dispatches; pinned and keep-whole dispatches are their own groups below.", "",
           "## Coverage", "",
           f"- dispatches in range: {cov['dispatches']}; completed: {cov['completed']}; groups: "
           + ", ".join(f"{k} {v}" for k, v in report["groups"].items()),
           f"- completed without ground truth: {cov['completed_without_ground_truth']}; without active time: "
           f"{cov['completed_without_active_time']}; without tokens: {cov['completed_without_tokens']}; with tokens but no "
           f"window share: {cov['completed_without_window_share']}",
           f"- failed, relaunched or canceled: {cov['failed_or_canceled']}, of which with any measurement: "
           f"{cov['failed_or_canceled_with_measurement']}",
           f"- ledger decisions in range that start a worker: {cov['ledger_decisions']}, without a dispatch row: "
           f"{cov['ledger_decisions_without_dispatch']}; deliberate non-dispatches (self, none, split): "
           f"{cov['ledger_deliberate_non_dispatches']}",
           f"- rotations whose replaced worker could not be found: {cov['rotations_without_predecessor']}",
           f"- unreadable inputs skipped: {skipped}; rows without a creation date: {cov['rows_without_date']}",
           "- completed by path: " + (", ".join(f"{k} {v}" for k, v in cov["completed_by_path"].items()) or "none"),
           "- missing active time reasons: " + (", ".join(f"{k} {v}" for k, v in cov["missing_active_time_reasons"].items()) or "none"),
           ""]
    head = ["cell", "n", "expected h", "actual h", "ratio", "ratio q1-q3", "with follow-ups", "retried left out"]
    for title, block in (("Hours by work kind", report["hours"]["by_work_kind"]),
                         ("Hours by demand profile (dominant category)", report["hours"]["by_demand"])):
        out += [f"## {title}", ""] + table(head, hours_rows(block)) + [""]
    out += ["## Spend by model and effort", "",
            "Actual USD = recorded input, output and cache-read tokens at the model's public list price, plus cache-write tokens "
            "where the data lists a cache-write price; otherwise the USD is a lower bound (`>=`) and the cache-write tokens are "
            "shown beside it. The spend ratio mixes the hours error and the rate error. The rate columns separate them: actual "
            "list-price USD per active hour against the `usd_per_hour` the estimate used. Window share = actual USD over the "
            "window's cited capacity.", ""]
    out += table(["cell", "n", "expected USD", "actual USD", "spend ratio", "rate n", "usd_per_hour", "actual USD / active h",
                  "rate ratio", "rate q1-q3", "window", "window share %", "tokens n", "fresh tokens", "cache-read tokens",
                  "cache-write tokens", "retried left out"], spend_rows(report["spend"]["by_model_effort"])) + [""]
    end_head = ["cell", "n"] + list(ENDINGS) + ["started by rotation", "rotated away"]
    out += ["Rotation is not an ending: the worker a rotation replaced shows as completed and is counted in *rotated away*; "
            "the fresh worker that continued the work is counted in *started by rotation*.", ""]
    for title, block in (("Endings by model and effort", report["endings"]["by_model_effort"]),
                         ("Endings by work kind", report["endings"]["by_work_kind"])):
        out += [f"## {title}", ""] + table(end_head, [[k, mark(c)] + [c[e] for e in ENDINGS]
                                                       + [c["started_by_rotation"], c["rotated_away"]]
                                                       for k, c in block.items()]) + [""]
    out += ["## Pinned and keep-whole dispatches", ""]
    if not report["own_groups"]:
        out += ["none in range", ""]
    for group, c in report["own_groups"].items():
        h, s, e = c["hours"], c["spend"], c["endings"]
        out += [f"- {group}: {mark(e)} dispatches; hours ratio {fmt(h['ratio_median'])} (n {mark(h)}); spend ratio "
                f"{fmt(s['ratio_median'])} (n {mark(s)}); " + ", ".join(f"{n} {e[n]}" for n in ENDINGS)]
    return "\n".join(out) + "\n"


def summary(report):
    cov = report["coverage"]
    cells = [(k, c) for k, c in report["hours"]["by_work_kind"].items()
             if not c["small"] and c["ratio_median"] is not None and c["ratio_median"] > 0]
    far = max(cells, key=lambda kc: abs(math.log(kc[1]["ratio_median"])), default=None)
    line = (f"{cov['dispatches']} dispatches ({cov['completed']} completed, {cov['completed_without_active_time']} without active time); "
            "groups " + ", ".join(f"{k} {v}" for k, v in report["groups"].items()) + "; ")
    line += (f"largest hours deviation (by log ratio) among readable work kinds: {far[0]} x{fmt(far[1]['ratio_median'])} (n {far[1]['n']})"
             if far else "no work kind has a readable hours sample")
    return line


# ---------------------------------------------------------------- cli

def main(argv=None, pricing=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--since", type=dt.date.fromisoformat)
    parser.add_argument("--until", type=dt.date.fromisoformat)
    parser.add_argument("--state-dir", type=Path, help="default: ROUTER_STATE_DIR, else ~/.agent-compass")
    parser.add_argument("--out", type=Path, help="write the report here instead of <state dir>/calibration/<run date>/")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)
    root = (args.state_dir or Path(os.environ.get("ROUTER_STATE_DIR") or Path.home() / ".agent-compass")).expanduser()
    today = dt.datetime.now(UTC).date()
    until = args.until or today
    since = args.since or until - dt.timedelta(days=DEFAULT_DAYS)
    try:
        if not root.is_dir():
            raise ReportError(f"{root}: state directory not found (pass --state-dir)")
        report = build(root / "dispatch.sqlite3", root / "router-ledger.jsonl", since, until,
                       pricing if pricing is not None else accounts_pricing(root))
        if not args.no_write:
            out = args.out or root / "calibration" / today.isoformat()
            out.mkdir(parents=True, exist_ok=True)
            (out / "report.md").write_text(render(report), encoding="utf-8", newline="\n")
            (out / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                                             encoding="utf-8", newline="\n")
            print(f"wrote {out}")
    except ReportError as exc:
        print(f"calibration report: {exc}", file=sys.stderr)
        return 2
    print(summary(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
