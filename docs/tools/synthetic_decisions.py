"""Synthetic, illustrative decision snapshots shared by the README image tools.

Values are made up to show the UI: not benchmark claims, not live allocation inputs, no account data.

The headline decision follows the quota-line economy: N = F − β·X/h, where F is the fit capped at what the
task needs (demand-weighted mean of min(score, target) per axis), X the spend that lands over the window's
recovery line ℓ(t) = C·(t − t0)/L, h the task hours and β the window's quota price. The rows are ordered by the
router's own `candidate_ranking.five_axis_order` (after R2-F4): N, then the shorter wait, then the faster completion,
then the fewer USD, then the higher level. Quota expiry is never a tie-break; it only enters N through the price.
"""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "agent-compass" / "scripts"))
import candidate_ranking  # noqa: E402

AXES = ("divergence", "coding", "aesthetics", "tool_operation")
HEADLINE = "Refactor payment retries with integration tests"
HEADLINE_KIND = "complex_build_plan"
HOURS = 2.0
ANCHOR = 0.8                                    # the score a demand of 3 asks for
DEMAND = dict(zip(AXES, (1, 3, 3, 1)))          # classifier levels 0..3 per axis
WEEK = 168.0
FAST_PRICE, FAST_SPEED = 2.0, 1.5               # fast mode: twice the spend, 1.5x the speed

# window key -> label, capacity C (USD), used U (USD), hours until reset, β (fit per over-line USD)
WINDOWS = {
    "claude-a": ("Claude A", 400.0, 240.0, 2.0, 0.0075),      # resets in 2 h with 40% left: under its line
    "claude-b": ("Claude B", 400.0, 8.0, 144.0, 0.0075),      # resets in 6 days, slightly under its line
    "codex": ("Codex", 120.0, 48.0, 120.0, 0.0080),           # ahead of its line: every dollar is over
    "gemini": ("Gemini", 250.0, 150.0, 72.0, 0.0080),
}
# The same accounts 118 hours earlier, for the same task: Claude A is ahead of its line, so it is spared and the
# task runs on Claude B. Sparing A is what leaves it under its line when the headline decision is made.
EARLIER = {
    "claude-a": ("Claude A", 400.0, 180.0, 120.0, 0.0075),     # 5 days to its reset, already 45% used
    "claude-b": ("Claude B", 400.0, 120.0, 94.0, 0.0075),      # under its line
    "codex": ("Codex", 120.0, 80.0, 70.0, 0.0080),
    "gemini": ("Gemini", 250.0, 60.0, 22.0, 0.0080),
}
EARLIER_AT, EARLIER_WINNER = "2026-10-25T11:40:00+00:00", "opus55_high_b"

# (bundle id, model, effort, account = its weekly window, fast, four axis scores, USD per hour)
# The winner is rigor/precision-heavy and the runner-up divergence/vision-heavy, so the radar polygons differ.
HEADLINE_SPECS = (
    ("opus55_high_fast", "claude-opus-5-5", "high", "claude-a", True, (.74, .86, .88, .70), 22.86),
    ("opus55_high", "claude-opus-5-5", "high", "claude-a", False, (.74, .86, .88, .70), 22.86),
    ("opus55_high_b", "claude-opus-5-5", "high", "claude-b", False, (.74, .86, .88, .70), 22.86),
    ("sol_high", "gpt-6.1-sol", "high", "codex", False, (.92, .76, .80, .93), 4.5),
    ("sonnet55_high", "claude-sonnet-5-5", "high", "claude-a", False, (.70, .68, .74, .72), 6.4),
    ("argon_high", "gemini-4-argon-high", "high", "gemini", False, (.90, .66, .70, .95), 9.0),
    ("flash_high", "gemini-3.8-flash-high", "medium", "gemini", False, (.62, .52, .58, .66), 1.5),
)

# Older rows for the "Recent decisions" list: (objective, kind, hours, winning bundle id)
HISTORY = (
    ("Fix a race condition in the upload queue", "debug_review", 1, "sol_high"),
    ("Summarize last week's release notes", "text_in_text_out", 0.5, "flash_high"),
    ("Compare three caching strategies for the API", "research_packet", 2, "argon_high"),
    ("Run the nightly lint and type checks", "tool_execution", 1, "sonnet55_high"),
    ("Migrate the report exporter to the new schema", "complex_build_plan", 3, "opus55_high"),
)


def capped_fit(scores, demand=DEMAND):
    """(F, {axis: {demand, score, target}}): ability counts only up to the task's target on each axis."""
    targets = {axis: round(ANCHOR * level / 3, 4) for axis, level in demand.items()}
    rows = {axis: {"demand": demand[axis], "score": score, "target": targets[axis]} for axis, score in zip(AXES, scores)}
    total = sum(demand.values())
    return sum(demand[a] * min(r["score"], r["target"]) for a, r in rows.items()) / total, rows


def over_line(window, spend, hours):
    """X: the part of `spend` above the recovery line when the task ends (tasks here end by the reset)."""
    _, capacity, used, reset_in, _ = window
    line_at_end = capacity * (WEEK - reset_in + hours) / WEEK
    return max(0.0, spend - max(0.0, line_at_end - used))


# How an account's usage rose over its period so far: (share of the elapsed time, share of today's usage).
# "early": a burst that ran ahead of the quota line, a quiet stretch while the line caught up, then steady work
# under it. "late": little at first, then work that overtook the line. Gemini has no recorded readings, so its
# usage line is fitted and drawn as such.
PROFILES = {"early": ((.1, .22), (.22, .44), (.35, .525), (.5, .6), (.7, .742), (.85, .867)),
            "late": ((.12, .048), (.28, .126), (.45, .2475), (.6, .468), (.75, .735), (.9, .9))}
RECORDED = ("claude-a", "claude-b", "codex")


def readings(key, capacity, used, elapsed):
    """Synthetic usage readings of one window inside its period: [[hours since its start, used USD], ...]."""
    if key not in RECORDED or elapsed <= 0:
        return None
    profile = PROFILES["late" if used > capacity * elapsed / WEEK else "early"]
    return [[round(elapsed * moment, 2), round(used * share, 2)] for moment, share in profile]


def quota_line_block(windows=WINDOWS):
    block = {}
    for key, (label, capacity, used, reset_in, beta) in windows.items():
        block[key] = {"label": label, "capacity_usd": capacity, "used_usd": used,
                      "elapsed_hours": WEEK - reset_in, "reset_in_hours": reset_in, "beta": beta}
        samples = readings(key, capacity, used, WEEK - reset_in)
        if samples:
            block[key]["samples"] = samples
    return {"windows": block}


def ranking(specs=HEADLINE_SPECS, winner=None, hours=HOURS, windows=WINDOWS):
    """Allocator-shaped ranking in the router's order; a `winner` other than the natural one is listed first."""
    rows = []
    for bundle_id, model, effort, window, fast, scores, usd_per_hour in specs:
        fit, axis_rows = capped_fit(scores)
        task_hours = hours / FAST_SPEED if fast else hours
        spend = usd_per_hour * hours * (FAST_PRICE if fast else 1.0)   # fast costs FAST_PRICE x the normal run
        _, capacity, used, reset_in, beta = windows[window]
        over = over_line(windows[window], spend, task_hours)
        cost = beta * over / hours
        components = {**dict(zip(AXES, scores)), **{f"share_{a}": s for a, s in zip(AXES, scores)}}
        level = sum(DEMAND[a] * s for a, s in zip(AXES, scores)) / sum(DEMAND.values())
        components.update(fit=round(fit, 4), opportunity_cost=round(cost, 4), five_axis_level=round(level, 4),
                          harness_place=0)
        rows.append({
            "id": bundle_id, "bundle": {"model": model, "effort": effort, "account": window, **({"fast": True} if fast else {})},
            "score": round(fit - cost, 4), "components": components, "excluded": [], "five_axis_fit": axis_rows,
            "quota_line": {"window": window, "hours": round(task_hours, 2), "spend_usd": round(spend, 2),
                           "over_line_usd": round(over, 2)},
            "capacity": {"remaining": round(100 * (1 - used / capacity), 1)},
            "expected_pct": round(100 * spend / capacity, 1),
            "duration_hours": round(task_hours, 4), "expected_hours": round(task_hours, 4), "available_in_hours": None,
            "expected_usd": round(spend, 2),
        })
    rows.sort(key=lambda row: (row["id"] != winner, candidate_ranking.five_axis_order(row)))
    for index, row in enumerate(rows):
        # The first key of the router's order on which the row above beats this one ('net_score' for the first row).
        row["quota_line"]["decided_by"] = (
            candidate_ranking.five_axis_deciding_key(rows[index - 1], row) if index else "net_score")
    return rows


def decision_row(objective, kind, hours, winner, at, specs=HEADLINE_SPECS, difficulty="hard", windows=WINDOWS):
    """A ledger `plan` row carrying an immutable decision snapshot, as the router writes it."""
    rows = ranking(specs, winner, windows=windows)
    return {
        "stage": "plan", "choice": winner, "kind": kind, "objective": objective, "at": at, "margin": 0.08,
        "decision_snapshot": {
            "task": {"objective": objective, "work_kind": kind, "difficulty": difficulty, "hours": hours},
            "ranking": rows, "choice": winner, "quota_line": quota_line_block(windows),
        },
    }
