"""Synthetic, illustrative decision snapshots shared by the README image tools.

Values are made up to show the UI: not benchmark claims, not live allocation inputs, no account data.
Score = mean(four capabilities) - opportunity cost + reset bonus - effort penalty.
"""
from __future__ import annotations

AXES = ("divergence", "global_rigor", "module_precision", "capability")
HEADLINE = "Refactor payment retries with integration tests"

# (bundle id, model, effort, four abilities, opportunity cost, reset bonus, effort penalty)
# The winner is rigor/precision-heavy and the runner-up divergence/vision-heavy, so the radar polygons differ.
HEADLINE_SPECS = (
    ("opus55_high", "claude-opus-5-5", "high", (.74, .96, .97, .70), .025, .04, .02),
    ("sol_high", "gpt-6.1-sol", "high", (.96, .66, .70, .93), .045, .02, .02),
    ("argon_high", "gemini-4-argon-high", "high", (.90, .58, .64, .97), .075, .01, .03),
    ("sonnet55_medium", "claude-sonnet-5-5", "medium", (.72, .78, .84, .72), .035, .02, .01),
    ("flash_high", "gemini-3.8-flash-high", "medium", (.65, .64, .68, .70), .020, .01, .01),
)

HEADLINE_KIND = "complex_build_plan"

# Older rows for the "Recent decisions" list: (objective, kind, hours, winning bundle id)
HISTORY = (
    ("Fix a race condition in the upload queue", "debug_review", 1, "sol_high"),
    ("Summarize last week's release notes", "text_in_text_out", 0.5, "flash_high"),
    ("Compare three caching strategies for the API", "research_packet", 2, "argon_high"),
    ("Run the nightly lint and type checks", "tool_execution", 1, "sonnet55_medium"),
    ("Migrate the report exporter to the new schema", "complex_build_plan", 3, "opus55_high"),
)


def ranking(specs=HEADLINE_SPECS, winner=None):
    """Allocator-shaped ranking; `winner` (a bundle id) is moved to the top by a small bonus when given."""
    rows = []
    for index, (bundle_id, model, effort, abilities, cost, bonus, penalty) in enumerate(specs):
        excluded = ["below task ability floor"] if bundle_id == "flash_high" and winner != "flash_high" else []
        boost = .12 if bundle_id == winner and winner != specs[0][0] else 0.0
        components = dict(zip(AXES, abilities))
        components.update(opportunity_cost=cost, reset_bonus=bonus + boost, effort_penalty=penalty)
        rows.append({
            "id": bundle_id,
            "bundle": {"model": model, "effort": effort},
            "score": None if excluded else round(sum(abilities) / 4 - cost + bonus + boost - penalty, 4),
            "components": components,
            "excluded": excluded,
            "capacity": {"remaining": 72 - index * 7},
            "expected_pct": 6,
        })
    return rows


def decision_row(objective, kind, hours, winner, at, specs=HEADLINE_SPECS, difficulty="hard"):
    """A ledger `plan` row carrying an immutable decision snapshot, as the router writes it."""
    rows = ranking(specs, winner)
    return {
        "stage": "plan", "choice": winner, "kind": kind, "objective": objective, "at": at, "margin": 0.08,
        "decision_snapshot": {
            "task": {"objective": objective, "work_kind": kind, "difficulty": difficulty, "hours": hours},
            "ranking": rows, "choice": winner,
        },
    }
