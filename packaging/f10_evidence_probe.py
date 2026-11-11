"""F10 probe: evidence-calibrated routing after 0.3.6, run inside a foreign profile against the installed skill.

    python -X utf8 f10_evidence_probe.py SCRIPTS_DIR TRAY_DIR {intact|broken} OUT.json

foreign_machine_check.verify_f10_evidence_calibrated_routing runs it twice in the same throwaway profile. `intact`
uses the installed reference data. `broken` removes, in a copy inside the profile (ROUTER_ACCOUNTS_FILE) or in the
scenario's own inputs, exactly the data each behaviour depends on, so every check is shown to fail once against the
behaviour it guards. The harness then deletes the broken copy. Each check writes {"ok": bool, "detail": ...}.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
import sys
from pathlib import Path

SCRIPTS, TRAY, MODE, OUT = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4])
BROKEN = MODE == "broken"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(1, str(TRAY))

import allocator  # noqa: E402
import provider_inventory  # noqa: E402
import route_judge  # noqa: E402

STATE = Path(os.environ["ROUTER_STATE_DIR"])
NOW = dt.datetime.now(dt.timezone.utc)
SHIPPED = json.loads((SCRIPTS.parent / "references" / "accounts.json").read_text(encoding="utf-8-sig"))
OLDER, NEWER = "claude-haiku-4-5", "claude-haiku-5-5"   # fixture names: an older generation and its successor
checks: dict[str, dict] = {}


def check(name, ok, detail=None):
    checks[name] = {"ok": bool(ok), "detail": detail}


def broken_document(edit):
    """A copy of the installed accounts.json inside the profile with `edit` applied; ROUTER_ACCOUNTS_FILE pins it."""
    doc = copy.deepcopy(SHIPPED)
    edit(doc)
    path = STATE / "f10-broken-accounts.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    os.environ["ROUTER_ACCOUNTS_FILE"] = str(path)


def shipped_document():
    os.environ.pop("ROUTER_ACCOUNTS_FILE", None)


def usage_from(providers, accounts, name="f10-scenario-cache.json"):
    """A usage snapshot from a scenario cache written next to the real one (the real cache stays as collected)."""
    keys = {n: r.get("credential_key") for n, r in ((accounts.get("_discovery") or {}).get("accounts") or {}).items()
            if isinstance(r, dict) and r.get("credential_key")}
    for acc, row in providers.items():
        if acc in keys:
            row.setdefault("credential_key", keys[acc])
    path = STATE / name
    path.write_text(json.dumps({"providers": providers}), encoding="utf-8")
    usage = route_judge.usage_snapshot(path, list(accounts.get("accounts") or {}), NOW,
                                       inventory=accounts.get("_discovery") or {})
    return provider_inventory.merge_usage(usage, accounts, NOW)


def reading(account, accounts, used=10, reset_h=96.0):
    """One reading per window the account declares: a window the account does not declare cannot be priced."""
    return {"provider_id": account, "available": True, "stale": False, "fetched_at": NOW.isoformat(),
            "windows": [{"id": wid, "used_pct": used, "resets_at": (NOW + dt.timedelta(hours=reset_h)).isoformat()}
                        for wid in accounts["accounts"][account]["windows"]]}


def plan(task, usage, accounts):
    return allocator.plan(task, usage, NOW, accounts=copy.deepcopy(accounts), overrides=[], records=[],
                          host_policy_config=None)


def row(result, bundle_id):
    return next((c for c in result.get("ranking") or [] if c.get("id") == bundle_id), {})


# The classifier's answer for a small, convergent coding task: it asks little of the four categories.
ROUTINE_PROFILE = {"work_kind": "general_implementation", "reasoning_depth": 1, "code_complexity": 1, "needs_visual": False,
                   "needs_gui": False, "local_only": False, "wants_fast": False,
                   "read_only_review": False, "access": "write", "expected_hours": 0.5, "hours_confidence": "medium",
                   "urgency": "normal", "interaction_style": "engineering", "multi_task": False, "task_count": "1",
                   "axis_demand": {"divergence": 0, "coding": 1, "aesthetics": 0, "tool_operation": 0}}

# --- 1. Default checkbox from public data; never-billed and metered accounts are a last resort -----------------------
if BROKEN:
    broken_document(lambda d: d["superseded"].pop(OLDER, None))
(STATE / "model-catalog.json").write_text(json.dumps({"schema_version": 1, "providers": {"claude": {
    "ok": True, "type": "cli", "checked_at": NOW.isoformat(),
    "models": [{"id": OLDER, "label": "older generation"}, {"id": NEWER, "label": "newest generation"}]}}}), encoding="utf-8")
first_scan = allocator.bundles(allocator.load_accounts())
older = {b: s.get("disabled") for b, s in first_scan.items() if s.get("auto") and s.get("model") == OLDER}
newer = {b: s.get("disabled") for b, s in first_scan.items() if s.get("auto") and s.get("model") == NEWER}
check("first scan: a model public data lists as superseded is discovered unchecked, its successor checked",
      older and all(older.values()) and newer and not any(newer.values()), {"older": older, "newer": newer})
shipped_document()

accounts = allocator.load_accounts()
for name, plan_name in (("claude", "max5"), ("codex", "pro")):   # the plans' community capacities are public data
    accounts["accounts"][name].update(verified=True, plan=plan_name)
billing = "subscription" if BROKEN else None
# A metered account states its own prepaid budget: community capacity data measures subscription plans only.
BUDGET = {"windows": {"weekly": {"hours": 168}}, "capacity_usd": {"weekly": 200.0}}
accounts["accounts"]["metered-proxy"] = {"family": "codex", "billing": billing or "metered", "verified": True, **BUDGET,
                                         "bundles": {"metered_proxy_high": {"provider": "metered-proxy", "harness": "codex",
                                                     "model": "gpt-6.1-sol", "effort": "high"}}}
accounts["accounts"]["unbilled-proxy"] = {"family": "codex", "verified": True, **BUDGET,
                                          **({"billing": billing} if billing else {}),
                                          "bundles": {"unbilled_proxy_high": {"provider": "unbilled-proxy", "harness": "codex",
                                                      "model": "gpt-6.1-sol", "effort": "high"}}}
routine = {"objective": "Fix a bug in parser.py", "expected_hours": 0.5, "needs": {"local_only": False}, "profile": ROUTINE_PROFILE}
ample = usage_from({name: reading(name, accounts) for name in ("claude", "codex", "metered-proxy")}, accounts)
with_subscription = plan(routine, ample, accounts)
without = copy.deepcopy(accounts)
for name, spec in without["accounts"].items():
    if name not in ("metered-proxy", "unbilled-proxy"):
        spec["verified"] = False
fallback = plan(routine, ample, without)
check("metered and never-billed accounts are a last resort: excluded while a subscription can take the work, "
      "metered chosen only when none can, never-billed never automatically",
      allocator.METERED_LAST_RESORT in (row(with_subscription, "metered_proxy_high").get("excluded") or [])
      and any("explicit selection only" in e for e in row(with_subscription, "unbilled_proxy_high").get("excluded") or [])
      and fallback.get("choice") == "metered_proxy_high",
      {"metered": row(with_subscription, "metered_proxy_high").get("excluded"),
       "unbilled": row(with_subscription, "unbilled_proxy_high").get("excluded"), "fallback": fallback.get("choice")})

OUT.write_text(json.dumps(checks, indent=1, default=str), encoding="utf-8")
