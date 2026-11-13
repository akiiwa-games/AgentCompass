"""Maintainer calibration report over recorded dispatch outcomes. Read-only; spends no model tokens.

Reads `dispatch.sqlite3` (the rows `dispatch_ground_truth.py` completes) and `router-ledger.jsonl` from the state
directory and compares what the classifier and the allocator expected with what the dispatches did: classifier hours
against active duration, estimated spend against recorded consumption, and how dispatches ended. Every cell carries its
sample size, and a cell under `MIN_CELL` samples is marked too small to read.

    python audit/calibration_report.py [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--state-dir DIR] [--out DIR]

The report holds only counts, medians and group names: no task text, no file paths, no agent or dispatch ids, no
credentials. It writes Markdown and JSON under `<state dir>/calibration/<run date>/` and never writes a routing input.
Recorded outcomes never reach routing at runtime; the maintainer reads the report and edits shipped reference data by hand.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
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


def parse_ts(value):
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


# ---------------------------------------------------------------- reading

def read_dispatches(db: Path):
    """(dispatch rows, {decision_id: constraint}) read-only. Only the constraint of a decision is kept, never its task."""
    if not db.is_file():
        return [], {}
    connection = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        rows = [json.loads(r[0]) for r in connection.execute("SELECT data FROM dispatches")]
        constraints = {}
        for (data,) in connection.execute("SELECT data FROM decisions"):
            decision = json.loads(data)
            constraints[decision.get("decision_id")] = decision.get("constraint")
    finally:
        connection.close()
    return [r for r in rows if isinstance(r, dict)], constraints


def read_ledger(path: Path):
    """Decision lines of the ledger, whole file, newest wins per decision id: {decision_id: {...}} and the line list."""
    lines = []
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return {}, lines
    for raw in text.splitlines():
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict):
            lines.append(row)
    by_decision = {}
    for row in lines:
        if row.get("decision_id") and row.get("stage") in ("plan", "select", "record"):
            by_decision[row["decision_id"]] = row
    return by_decision, lines


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
    """(fresh tokens, cache-read tokens) of a recorded `token_usage`, None when nothing was recorded. Fresh = input + output
    (+ cache writes where the harness reports them)."""
    if not isinstance(usage, dict):
        return None, None
    fresh = [number(usage.get(k)) for k in ("input_tokens", "output_tokens", "cache_write_tokens")]
    fresh = [p for p in fresh if p is not None]
    return (sum(fresh) if fresh else None), number(usage.get("cache_read_tokens"))


def list_price_usd(usage, price):
    """Recorded tokens at the model's public list price (USD per million tokens: input, output, cache_read). Cache-write
    tokens have no listed price and are left out; None when nothing was recorded or the model has no listed price."""
    if not isinstance(usage, dict) or not isinstance(price, dict):
        return None
    parts = (("input_tokens", "input"), ("output_tokens", "output"), ("cache_read_tokens", "cache_read"))
    if any(number(usage.get(tok)) is not None and number(price.get(rate)) is None for tok, rate in parts):
        return None
    total = sum(number(usage.get(tok)) * number(price.get(rate)) for tok, rate in parts
                if number(usage.get(tok)) is not None)
    return total / 1_000_000 if any(number(usage.get(tok)) is not None for tok, _ in parts) else None


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


def normalize(rows, constraints, ledger, pricing, since, until):
    """One flat record per dispatch in the date range: grouping keys and measurements, nothing that identifies a task."""
    successors = {r.get("previous_attempt") for r in rows if r.get("previous_attempt")}
    records = []
    for row in rows:
        created = parse_ts(row.get("created_at"))
        if created is None or not since <= created.date() <= until:
            continue
        decision_id = row.get("decision_id")
        line = ledger.get(decision_id) or {}
        profile = row.get("profile") if isinstance(row.get("profile"), dict) else {}
        bundle = row.get("bundle") if isinstance(row.get("bundle"), dict) else {}
        pinned = USER_PIN in (constraints.get(decision_id), line.get("constraint"))
        kept = bool(line.get("keep_whole"))
        active = number(row.get("active_seconds"))
        price = pricing(bundle) if pricing else None
        capacity = number((price or {}).get("capacity_usd"))
        actual_usd = list_price_usd(row.get("token_usage"), (price or {}).get("list_price"))
        fresh, cached = token_parts(row.get("token_usage"))
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
            "followups": row.get("followup_turns"),
            "recorded": "ground_truth_recorded_at" in row,
            "expected_hours": number(profile.get("expected_hours")),
            "active_hours": active / 3600 if active is not None else None,
            "active_reason": row.get("active_seconds_reason"),
            "fresh_tokens": fresh,
            "cache_read_tokens": cached,
            "token_reason": row.get("token_usage_reason"),
            "expected_usd": number(row.get("expected_usd")),
            "actual_usd": actual_usd,
            "window": (price or {}).get("window_id"),
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
    paired = [r for r in records if r["expected_hours"] and r["active_hours"] is not None]
    ratios = [ratio(r["active_hours"], r["expected_hours"]) for r in paired]
    low, high = quartiles(ratios)
    return {"n": len(paired), "small": len(paired) < MIN_CELL,
            "expected_hours_median": median(r["expected_hours"] for r in paired),
            "active_hours_median": median(r["active_hours"] for r in paired),
            "ratio_median": median(ratios), "ratio_q1": low, "ratio_q3": high,
            "with_followups": sum(1 for r in paired if r["followups"])}


def spend_cell(records):
    priced = [r for r in records if r["expected_usd"] and r["actual_usd"] is not None]
    ratios = [ratio(r["actual_usd"], r["expected_usd"]) for r in priced]
    low, high = quartiles(ratios)
    tokens = [r for r in records if r["fresh_tokens"] is not None]
    windows = collections.Counter(r["window"] for r in records if r["window"] and r["window_share"] is not None)
    return {"n": len(priced), "small": len(priced) < MIN_CELL,
            "expected_usd_median": median(r["expected_usd"] for r in priced),
            "actual_usd_median": median(r["actual_usd"] for r in priced),
            "ratio_median": median(ratios), "ratio_q1": low, "ratio_q3": high,
            "window": windows.most_common(1)[0][0] if windows else None,
            "window_share_pct_median": median(r["window_share"] for r in priced),
            "tokens_n": len(tokens), "tokens_small": len(tokens) < MIN_CELL,
            "fresh_tokens_median": median(r["fresh_tokens"] for r in tokens),
            "cache_read_tokens_median": median(r["cache_read_tokens"] for r in tokens)}


def ending_cell(records):
    counts = collections.Counter(r["ending"] for r in records)
    return {"n": len(records), "small": len(records) < MIN_CELL,
            **{name: counts.get(name, 0) for name in ENDINGS},
            "started_by_rotation": sum(1 for r in records if r["rotated"])}


def model_key(r):
    return f"{r['model']}@{r['effort']}"


def section(records, key, builder):
    return {name: builder(rows) for name, rows in cell(records, key).items()}


def coverage(records, ledger_lines, rows_in_range, since, until):
    """Where a dispatch left no measurement, by dispatch path: the recording gaps the other sections cannot show."""
    done = [r for r in records if r["ending"] == "completed"]
    by_path = collections.Counter(f"{r['mode']}/{r['harness'] or 'no telemetry harness'}" for r in done)
    decisions = {l["decision_id"] for l in ledger_lines
                 if l.get("decision_id") and l.get("stage") in ("plan", "select", "record")
                 and (t := parse_ts(l.get("at"))) is not None and since <= t.date() <= until}
    launched = {r.get("decision_id") for r in rows_in_range}
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
        "ledger_decisions": len(decisions),
        "ledger_decisions_without_dispatch": len(decisions - launched),
    }


def build(db, ledger_path, since, until, pricing):
    all_rows, constraints = read_dispatches(db)
    ledger, lines = read_ledger(ledger_path)
    records = normalize(all_rows, constraints, ledger, pricing, since, until)
    in_range = [r for r in all_rows if (t := parse_ts(r.get("created_at"))) is not None and since <= t.date() <= until]
    routed = [r for r in records if r["group"] == ROUTED]
    return {
        "range": {"since": since.isoformat(), "until": until.isoformat()},
        "min_cell": MIN_CELL,
        "coverage": coverage(records, lines, in_range, since, until),
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

def accounts_pricing():
    """bundle -> {list_price, window_id, capacity_usd} from the shipped reference data and discovery, read-only.

    `list_price` is the model's public price in USD per million tokens (`five_axis.list_price`). `expected_usd` is estimated
    on the model's `usd_per_hour` rate, so a recorded cost at list price against it tests that estimate on an independent
    basis. The window is the first long window the bundle draws that has a cited capacity."""
    sys.path.insert(0, str(SCRIPTS))
    import account_fleet
    import axis_scores
    import quota_economy
    import quota_pools
    accounts = axis_scores.five_axis_overlay(account_fleet.load_accounts())
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
        return {"list_price": list_price, "window_id": window, "capacity_usd": capacity}
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


def hours_rows(block):
    return [[k, mark(c), fmt(c["expected_hours_median"]), fmt(c["active_hours_median"]), fmt(c["ratio_median"]),
             f"{fmt(c['ratio_q1'])}-{fmt(c['ratio_q3'])}" if c["ratio_q1"] is not None else "-", c["with_followups"]]
            for k, c in block.items() if c["n"]]


def render(report):
    cov, rng = report["coverage"], report["range"]
    out = [f"# Calibration report {rng['since']} to {rng['until']}", "",
           f"Cells under {report['min_cell']} samples are marked too small to read. Medians only; ratio = actual / expected. "
           "Tables cover routed dispatches; pinned and keep-whole dispatches are their own groups below.", "",
           "## Coverage", "",
           f"- dispatches in range: {cov['dispatches']}; completed: {cov['completed']}; groups: "
           + ", ".join(f"{k} {v}" for k, v in report["groups"].items()),
           f"- completed without ground truth: {cov['completed_without_ground_truth']}; without active time: "
           f"{cov['completed_without_active_time']}; without tokens: {cov['completed_without_tokens']}",
           f"- failed, relaunched or canceled: {cov['failed_or_canceled']}, of which with any measurement: "
           f"{cov['failed_or_canceled_with_measurement']}",
           f"- ledger decisions in range: {cov['ledger_decisions']}, without a dispatch row: {cov['ledger_decisions_without_dispatch']}",
           "- completed by path: " + (", ".join(f"{k} {v}" for k, v in cov["completed_by_path"].items()) or "none"),
           "- missing active time reasons: " + (", ".join(f"{k} {v}" for k, v in cov["missing_active_time_reasons"].items()) or "none"),
           ""]
    head = ["cell", "n", "expected h", "actual h", "ratio", "ratio q1-q3", "with follow-ups"]
    for title, block in (("Hours by work kind", report["hours"]["by_work_kind"]),
                         ("Hours by demand profile (dominant category)", report["hours"]["by_demand"])):
        out += [f"## {title}", ""] + table(head, hours_rows(block)) + [""]
    out += ["## Spend by model and effort", "",
            "Actual USD = recorded input, output and cache-read tokens at the model's public list price (cache-write tokens are not priced); "
            "share = actual USD over the window's cited capacity. Fresh tokens = input + output + cache writes.", ""]
    out += table(["cell", "n", "expected USD", "actual USD", "ratio", "window", "window share %", "tokens n", "fresh tokens", "cache-read tokens"],
                 [[k, mark(c), fmt(c["expected_usd_median"]), fmt(c["actual_usd_median"]), fmt(c["ratio_median"]), c["window"] or "-",
                   fmt(c["window_share_pct_median"]), f"{c['tokens_n']}{' (too small)' if c['tokens_small'] else ''}",
                   fmt(c["fresh_tokens_median"], 0), fmt(c["cache_read_tokens_median"], 0)]
                  for k, c in report["spend"]["by_model_effort"].items() if c["n"] or c["tokens_n"]]) + [""]
    end_head = ["cell", "n"] + list(ENDINGS) + ["started by rotation"]
    for title, block in (("Endings by model and effort", report["endings"]["by_model_effort"]),
                         ("Endings by work kind", report["endings"]["by_work_kind"])):
        out += [f"## {title}", ""] + table(end_head, [[k, mark(c)] + [c[e] for e in ENDINGS] + [c["started_by_rotation"]]
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
    cells = [(k, c) for k, c in report["hours"]["by_work_kind"].items() if not c["small"] and c["ratio_median"] is not None]
    far = max(cells, key=lambda kc: abs(kc[1]["ratio_median"] - 1), default=None)
    line = (f"{cov['dispatches']} dispatches ({cov['completed']} completed, {cov['completed_without_active_time']} without active time); "
            f"groups " + ", ".join(f"{k} {v}" for k, v in report["groups"].items()) + "; ")
    line += (f"largest hours deviation among readable work kinds: {far[0]} x{fmt(far[1]['ratio_median'])} (n {far[1]['n']})"
             if far else "no work kind has a readable hours sample")
    return line


# ---------------------------------------------------------------- cli

def state_dir(arg):
    return Path(arg or os.environ.get("ROUTER_STATE_DIR") or Path.home() / ".agent-compass")


def main(argv=None, pricing=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--since", type=dt.date.fromisoformat)
    parser.add_argument("--until", type=dt.date.fromisoformat)
    parser.add_argument("--state-dir")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)
    today = dt.datetime.now(UTC).date()
    until = args.until or today
    since = args.since or until - dt.timedelta(days=DEFAULT_DAYS)
    root = state_dir(args.state_dir)
    report = build(root / "dispatch.sqlite3", root / "router-ledger.jsonl", since, until, pricing if pricing is not None else accounts_pricing())
    if not args.no_write:
        out = args.out or root / "calibration" / today.isoformat()
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.md").write_text(render(report), encoding="utf-8", newline="\n")
        (out / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                                         encoding="utf-8", newline="\n")
        print(f"wrote {out}")
    print(summary(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
