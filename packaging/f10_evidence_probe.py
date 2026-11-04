"""F10 probe: evidence-calibrated routing after 0.3.6, run inside a foreign profile against the installed skill.

    python -X utf8 f10_evidence_probe.py SCRIPTS_DIR TRAY_DIR {intact|broken} OUT.json

foreign_machine_check.verify_f10_evidence_calibrated_routing runs it twice in the same throwaway profile. `intact`
uses the installed reference data. `broken` removes, in a copy inside the profile (ROUTER_ACCOUNTS_FILE) or in the
scenario's own inputs, exactly the data each behaviour depends on, so every check is shown to fail once against the
behaviour it guards. The harness then deletes the broken copy. Each check writes {"ok": bool, "detail": ...}. Checks
compare relations (a winner's ability against the frontier floor, a pool's forecast against N x 100), never
calibrated numbers.
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
SHIPPED_FRONTIER = float(SHIPPED["scoring"]["ability_floor"]["frontier"])
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


def ability(accounts, bundle):
    model = allocator.canonical(bundle.get("model"), accounts)
    info = allocator.model_spec(accounts, model)
    curves = (accounts.get("effort_curves") or {}).get(model)
    return allocator.effort_ability(accounts["scoring"], curves, info, allocator.scoring_effort(bundle, info))


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


def reading(account, five_used, week_used, five_reset_h=3.0, week_reset_h=96.0):
    return {"provider_id": account, "plan_label": "max", "available": True, "stale": False, "fetched_at": NOW.isoformat(),
            "windows": [
                {"id": "five_hour", "used_pct": five_used, "resets_at": (NOW + dt.timedelta(hours=five_reset_h)).isoformat()},
                {"id": "seven_day", "used_pct": week_used, "resets_at": (NOW + dt.timedelta(hours=week_reset_h)).isoformat()},
            ]}


def plan(task, usage, accounts):
    return allocator.plan(task, usage, NOW, accounts=copy.deepcopy(accounts), overrides=[], records=[],
                          host_policy_config=None)


def row(result, bundle_id):
    return next((c for c in result.get("ranking") or [] if c.get("id") == bundle_id), {})


# A route every subscription model may take, so only the floor decides who is admitted.
FRONTIER_TASK = {"objective": "Build the netcode core of a new multiplayer game from first principles.",
                 "work_kind": "general_implementation", "difficulty": "frontier", "expected_hours": 1.0,
                 "needs": {"local_only": False}}

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
for name in ("claude", "codex"):
    accounts["accounts"][name]["verified"] = True
billing = "subscription" if BROKEN else None
accounts["accounts"]["metered-proxy"] = {"family": "codex", "billing": billing or "metered", "verified": True, "windows": {},
                                         "bundles": {"metered_proxy_high": {"provider": "metered-proxy", "harness": "codex",
                                                     "model": "gpt-6.1-sol", "effort": "high", "draws": []}}}
accounts["accounts"]["unbilled-proxy"] = {"family": "codex", "verified": True, "windows": {},
                                          **({"billing": billing} if billing else {}),
                                          "bundles": {"unbilled_proxy_high": {"provider": "unbilled-proxy", "harness": "codex",
                                                      "model": "gpt-6.1-sol", "effort": "high", "draws": []}}}
routine = {"objective": "Fix a bug in parser.py", "work_kind": "general_implementation", "expected_hours": 0.5}
ample = usage_from({"claude": reading("claude", 10, 10), "codex": reading("codex", 10, 10)}, accounts)
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

# --- 2. Ability floors are data: frontier routes at or above the floor, otherwise waits or asks -----------------------
if BROKEN:
    broken_document(lambda d: d["scoring"]["ability_floor"].update(frontier=d["scoring"]["ability_floor"]["coupled"]))
accounts = allocator.load_accounts()
for name in ("claude", "codex"):
    accounts["accounts"][name]["verified"] = True
ample = usage_from({"claude": reading("claude", 10, 10), "codex": reading("codex", 10, 10)}, accounts)
served = plan(FRONTIER_TASK, ample, accounts)
winner = row(served, served.get("choice"))
served_ok = served.get("choice") != "none" and ability(accounts, winner.get("bundle") or {}) >= SHIPPED_FRONTIER
# Scarcity: Claude's 5h window is spent and the user unchecked every Codex model at or above the floor, so only
# models below the frontier floor have quota now.
scarce_accounts = copy.deepcopy(accounts)
for bid, spec in scarce_accounts["accounts"]["codex"]["bundles"].items():
    if ability(scarce_accounts, dict(spec, account="codex")) >= SHIPPED_FRONTIER:
        spec["disabled"] = True
scarce = plan(FRONTIER_TASK, usage_from({"claude": reading("claude", 100, 20), "codex": reading("codex", 10, 10)},
                                        scarce_accounts), scarce_accounts)
scarce_winner = row(scarce, scarce.get("choice"))
decision = scarce.get("decision") or {}
waits = (scarce.get("choice") == "none" and decision.get("needs_user_decision") is True
         and (decision.get("next_reset") or {}).get("hours", 0) > 0)
scarce_ability = ability(scarce_accounts, scarce_winner.get("bundle") or {}) if scarce_winner else None
# One check over both scenarios: with quota the winner is at or above the floor; with only lower models having quota
# the plan waits or asks. A lower floor in the data turns the scarce case into a silent downgrade, so it fails then.
check("frontier work routes only to a bundle at or above the frontier floor; under scarcity it waits or asks "
      "(none with next_reset) instead of downgrading",
      served_ok and waits,
      {"floor": SHIPPED_FRONTIER, "ample": {"choice": served.get("choice"),
                                            "ability": winner and ability(accounts, winner.get("bundle") or {})},
       "scarce": {"choice": scarce.get("choice"), "ability": scarce_ability, "next_reset": decision.get("next_reset"),
                  "reason": decision.get("reason")}})
shipped_document()

# --- 3. Fixed effort: a model row's fixed_effort is the scored effort; the bundle (and its launch) carries none -------
if BROKEN:
    broken_document(lambda d: [m.pop("fixed_effort", None) for m in d["models"].values() if isinstance(m, dict)])
accounts = allocator.load_accounts()
fixed = {k: v["fixed_effort"] for k, v in SHIPPED["models"].items() if isinstance(v, dict) and v.get("fixed_effort")}
model_id, effort = next(iter(fixed.items()))
accounts["accounts"]["fixed-effort-bot"] = {"billing": "subscription", "verified": True, "windows": {}, "unmetered": True,
                                            "bundles": {"fixed_bot": {"provider": "fixed-effort-bot", "model": model_id}}}
text_task = {"objective": "Summarise the attached design notes.", "work_kind": "text_in_text_out", "difficulty": "routine",
             "expected_hours": 0.5, "needs": {"local_only": False}}
pinned = plan(dict(text_task, constraints={"model": model_id}), ample, accounts)
bot = row(pinned, "fixed_bot")
info = allocator.model_spec(accounts, model_id)
curves = (accounts.get("effort_curves") or {}).get(model_id)
at_fixed = round(allocator.effort_ability(accounts["scoring"], curves, SHIPPED["models"][model_id], effort), 2)
at_high = round(allocator.effort_ability(accounts["scoring"], curves, SHIPPED["models"][model_id], "high"), 2)
selected = pinned.get("selected_bundle") or {}
check("a fixed_effort model row is scored at that effort and its routed bundle carries no effort",
      at_fixed != at_high and (bot.get("components") or {}).get("ability_value") == at_fixed
      and "effort" not in (bot.get("bundle") or {}) and pinned.get("choice") == "fixed_bot" and not selected.get("effort"),
      {"model": model_id, "fixed_effort": effort, "scored": (bot.get("components") or {}).get("ability_value"),
       "at_fixed": at_fixed, "at_high": at_high, "selected": selected, "excluded": bot.get("excluded")})
shipped_document()

# --- 4. Free-usage bonus only where public data declares it ----------------------------------------------------------
if BROKEN:
    broken_document(lambda d: [a.pop("free_usage_bonus", None) for a in d["accounts"].values() if isinstance(a, dict)])
accounts = allocator.load_accounts()
declared = sorted(n for n, a in SHIPPED["accounts"].items() if isinstance(a, dict) and a.get("free_usage_bonus"))
free_other = next(n for n, a in SHIPPED["accounts"].items()
                  if isinstance(a, dict) and a.get("unlimited") and not a.get("free_usage_bonus"))
bonus_account = declared[0] if declared else "dot"
bonus_bundle, bonus_spec = next(iter(SHIPPED["accounts"][bonus_account]["bundles"].items()))
for name in (bonus_account, free_other):
    accounts["accounts"][name]["verified"] = True
# The same model on the other free account, so only the account's declaration differs.
accounts["accounts"][free_other]["bundles"]["f10_same_model"] = {"provider": free_other, "model": bonus_spec["model"]}
research = {"objective": "Collect public release notes for three libraries.", "work_kind": "web_research",
            "difficulty": "routine", "expected_hours": 0.5, "needs": {"local_only": False}}
free_plan = plan(research, ample, accounts)
declared_row, other_row = row(free_plan, bonus_bundle), row(free_plan, "f10_same_model")
check("the free-usage bonus applies on the account public data declares it for and not on another free account",
      (declared_row.get("components") or {}).get("free_usage", 0) > 0 and "free_usage" not in (other_row.get("components") or {})
      and bool(other_row.get("components")),
      {"declared": declared, "other": free_other, "declared_components": declared_row.get("components"),
       "other_components": other_row.get("components"), "other_excluded": other_row.get("excluded")})
shipped_document()

# --- 5. Pool demand: a pool of N accounts forecasts against N x 100% ------------------------------------------------
cache = json.loads((STATE / "usage-cache.json").read_text(encoding="utf-8"))
pooled = copy.deepcopy(cache["providers"]["claude"])
if BROKEN:
    for window in pooled.get("windows") or []:
        window.pop("accounts", None)   # a reading as a pre-pool collector stored it
accounts = allocator.load_accounts()
usage = usage_from({"claude": pooled}, accounts, "f10-pool-cache.json")
# The allocator's own path from a usage snapshot to a window's state and demand forecast, for a Claude bundle.
claude_spec = accounts["accounts"]["claude"]
bundle_id, bundle_spec = next(iter(claude_spec["bundles"].items()))
bundle = dict(bundle_spec, id=bundle_id, account="claude")
windows = {wid: (wspec, observed) for wid, wspec, observed in allocator.applicable_windows(
    claude_spec, allocator.canonical(bundle["model"], accounts), (usage.get("accounts") or {}).get("claude") or {}, bundle, accounts)}
week_spec, observed = windows.get("seven_day", ({"hours": 168}, None))
state = allocator.window_state("seven_day", week_spec, observed, claude_spec, NOW, accounts["scoring"]["short_window_hours"])
mean, _, rate, source = allocator.demand_forecast(claude_spec, "seven_day", state, accounts["scoring"])
count = next((w.get("accounts") for w in cache["providers"]["claude"]["windows"] if w["id"] == "seven_day"), 1)
check("a pool of N accounts forecasts demand against N x 100% (other work's draw is not hidden by remaining > 100%)",
      count > 1 and state.get("full") == 100.0 * count and state.get("remaining", 0) > 100 and mean > 0,
      {"accounts": count, "full": state.get("full"), "remaining": state.get("remaining"), "demand": mean,
       "rate": rate, "source": source})

# --- 6. Tray ability-floor chart: positions, saved overrides and an off-screen render ---------------------------------
import router_data  # noqa: E402
import router_priority  # noqa: E402

tray_allocator, _ = router_priority.modules()
accounts = tray_allocator.load_accounts()
chart_accounts = copy.deepcopy(accounts)
if BROKEN:
    for curve in (chart_accounts.get("effort_curves") or {}).values():
        if isinstance(curve, dict) and isinstance(curve.get("ability"), dict):
            curve["ability"] = {e: v + 0.5 for e, v in curve["ability"].items()}
chart = router_data.ability_floor_chart_data(accounts=chart_accounts)
positions = chart.get("bundles") or {}
bundles_now = tray_allocator.bundles(accounts)
mismatched = {bid: (pos.get("ability"), round(ability(accounts, bundles_now[bid]), 2)) for bid, pos in positions.items()
              if bid in bundles_now and pos.get("ability") != round(ability(accounts, bundles_now[bid]), 2)}
installed = Path(tray_allocator.__file__).resolve()
check("the tray chart places every bundle at the installed allocator's ability",
      positions and not mismatched and (installed.is_relative_to(Path(os.environ["CODEX_HOME"]).resolve())
                                        or "site-packages" in installed.parts),
      {"bundles": len(positions), "mismatched": dict(list(mismatched.items())[:5]), "allocator": str(installed)})

override = STATE / "accounts-override.json"
frontier_now = float(chart["floors"]["frontier"])
router_data.save_ability_floors({"frontier": round(frontier_now + 0.1, 2)})
saved = json.loads(override.read_text(encoding="utf-8")) if override.is_file() else {}
if BROKEN:
    saved.setdefault("scoring", {}).setdefault("ability_floor", {})["frontier_reason"] = "raised for a release"
scoring = saved.get("scoring") or {}
values = list((scoring.get("ability_floor") or {}).values()) + [
    v for r in (scoring.get("route_ability_floor") or {}).values() for v in (r.values() if isinstance(r, dict) else [r])]
replanned = allocator.load_accounts()["scoring"]["ability_floor"]["frontier"]
check("save_ability_floors writes only numbers to accounts-override.json and the router reads them",
      values and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values)
      and replanned == round(frontier_now + 0.1, 2), {"scoring": scoring, "router_frontier": replanned})
router_data.restore_ability_floor_defaults()
restored = json.loads(override.read_text(encoding="utf-8")) if override.is_file() else {}
if BROKEN:
    restored.setdefault("scoring", {})["route_ability_floor"] = {"web_research": {"routine": 2.0}}
check("restore_ability_floor_defaults removes the floor overrides",
      not {"ability_floor", "route_ability_floor"} & set(restored.get("scoring") or {}), restored.get("scoring"))

import ability_floor_chart  # noqa: E402

image = ability_floor_chart.AbilityFloorRenderer(width=880, theme_mode="light", dpi_scale=1.0).render(chart)
if BROKEN:
    image = image.copy()
    image.paste(image.getpixel((0, 0)), (0, 0, image.width, image.height))   # a blank image
colors = image.convert("RGB").getcolors(maxcolors=1 << 20)
check("an off-screen AbilityFloorRenderer render produces a drawn image",
      image.width > 0 and image.height > 0 and colors is not None and len(colors) > 16,
      {"size": image.size, "colors": len(colors or [])})

OUT.write_text(json.dumps(checks, indent=1, default=str), encoding="utf-8")
