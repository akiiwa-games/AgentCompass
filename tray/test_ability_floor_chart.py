"""Tests for ability-floor chart data, editing round-trip, ordering limits, routing parity, and off-screen rendering."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

TRAY_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TRAY_DIR.parent / "skills" / "agent-compass" / "scripts"
for _p in (str(TRAY_DIR), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import allocator
import i18n
import router_data
import ability_floor_chart


@pytest.fixture()
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect STATE_DIR and ROUTER_STATE_DIR to a throwaway directory so ~/.agent-compass is never touched."""
    monkeypatch.setenv("ROUTER_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(router_data, "STATE_DIR", tmp_path)
    router_data._invalidate_accounts_cache()
    yield tmp_path
    router_data._invalidate_accounts_cache()


def test_a_family_row_shows_the_ability_the_router_scores_it_at(isolated_state: Path) -> None:
    accounts = allocator.load_accounts(allocator.fleet_document_path())
    family, flagship = "claude-opus-4-20250514", "claude-opus-5-5"
    for spec in accounts["accounts"].values():
        if isinstance(spec, dict):
            spec["bundles"] = {}
    accounts["accounts"]["claude"]["bundles"] = {"old": {"model": family, "effort": "high", "provider": "claude"},
                                                 "top": {"model": flagship, "effort": "high", "provider": "claude"}}
    chart = router_data.ability_floor_chart_data(accounts=accounts, include_manual_billing=True, include_superseded=True)
    rows = chart["bundles"]
    policy = accounts["scoring"]
    expected = allocator.effort_ability(policy, allocator.model_curves(accounts, family), allocator.model_spec(accounts, family), "high")
    assert rows["old"]["ability"] == round(expected, 2)
    # Round 058 round 4: a family name takes its same-series row's scores (series conversion, no -0.3 shrink).
    assert rows["old"]["ability"] == rows["top"]["ability"]
    assert rows["old"]["model"] == family


def test_chart_data_matches_allocator_bundle_positions_and_fixed_effort(isolated_state: Path) -> None:
    accounts = allocator.load_accounts(allocator.fleet_document_path())
    policy = accounts["scoring"]
    curves_table = accounts.get("effort_curves") or {}
    chart = router_data.ability_floor_chart_data(accounts=accounts, include_manual_billing=True, include_superseded=True)

    # Floors and route floors come from allocator.ability_floor and allocator.critical_headroom
    for diff in router_data.ABILITY_FLOOR_KEYS:
        assert chart["floors"][diff] == round(float(allocator.ability_floor(policy, None, diff)), 2)
        expected_gate = round(chart["floors"][diff] + float(allocator.critical_headroom(policy, diff)), 2)
        assert chart["critical_gates"][diff] == expected_gate

    routes = allocator.load_capabilities(accounts=accounts)["routes"]
    assert chart["route_floors"]["tool_execution.hard"] == round(
        float(allocator.ability_floor(policy, routes["tool_execution"], "hard")), 2
    )

    # Every bundle position matches allocator.effort_ability at allocator.scoring_effort
    for bid, bspec in allocator.bundles(accounts).items():
        if bspec.get("provider") == "pi" and bspec.get("account") != "pi":
            continue
        if bspec.get("discovery_unavailable"):
            continue
        model = allocator.canonical(bspec.get("model") or bid, accounts)
        info = allocator.model_spec(accounts, model)
        curves = curves_table.get(model)
        scored_eff = allocator.scoring_effort(bspec, info)
        expected_raw = float(allocator.effort_ability(policy, curves, info, scored_eff))
        assert bid in chart["bundles"]
        b_entry = chart["bundles"][bid]
        assert b_entry["ability"] == round(expected_raw, 2)
        assert abs(b_entry["raw_ability"] - expected_raw) < 1e-9

    # grok-bot has fixed_effort="medium" and is placed as a single fixed-Medium point at 4.4
    grok_bot_models = [m for m in chart["models"] if m["model"] == "grok-bot"]
    if grok_bot_models:
        gb = grok_bot_models[0]
        assert gb["fixed_effort"] == "medium"
        assert len(gb["efforts"]) == 1
        assert gb["efforts"][0]["effort"] == "medium"
        assert gb["efforts"][0]["fixed"] is True
        assert gb["efforts"][0]["ability"] == 4.4


def test_edit_round_trip_writes_only_changed_numbers_and_restores_defaults(isolated_state: Path) -> None:
    defaults = router_data.default_ability_floors()
    initial = router_data.read_ability_floors()
    assert initial["floors"] == defaults["floors"]
    assert initial["has_overrides"] is False

    override_path = isolated_state / "accounts-override.json"
    assert not override_path.exists()

    # Change only 'hard' and 'tool_execution.hard'
    new_hard = round(defaults["floors"]["hard"] + 0.15, 2)
    new_route_hard = round(defaults["route_floors"]["tool_execution.hard"] + 0.10, 2)
    msg = router_data.save_ability_floors(
        {"hard": new_hard},
        route_floors={"tool_execution.hard": new_route_hard},
    )
    assert "2" in msg
    assert override_path.is_file()

    # Verify sparse override file contains ONLY changed numbers and no reason strings
    raw_override = json.loads(override_path.read_text(encoding="utf-8-sig"))
    assert raw_override["scoring"]["ability_floor"] == {"hard": new_hard}
    assert raw_override["scoring"]["route_ability_floor"] == {"tool_execution": {"hard": new_route_hard}}
    assert "reason" not in json.dumps(raw_override)
    assert "basis" not in json.dumps(raw_override)

    # Reading back merges with public defaults
    updated = router_data.read_ability_floors()
    assert updated["floors"]["hard"] == new_hard
    assert updated["floors"]["routine"] == defaults["floors"]["routine"]
    assert updated["floors"]["coupled"] == defaults["floors"]["coupled"]
    assert updated["floors"]["frontier"] == defaults["floors"]["frontier"]
    assert updated["route_floors"]["tool_execution.hard"] == new_route_hard
    assert updated["has_overrides"] is True

    # Restore defaults removes local override keys
    restore_msg = router_data.restore_ability_floor_defaults()
    assert restore_msg == router_data._tr("已恢复默认能力门槛")
    after_restore = router_data.read_ability_floors()
    assert after_restore["floors"] == defaults["floors"]
    assert after_restore["route_floors"] == defaults["route_floors"]
    assert after_restore["has_overrides"] is False
    raw_after = json.loads(override_path.read_text(encoding="utf-8-sig"))
    assert "scoring" not in raw_after


def test_ordering_limits_and_drag_clamping(isolated_state: Path) -> None:
    # Out-of-order floors are rejected
    with pytest.raises(ValueError, match="常规 ≤ 较难 ≤ 耦合 ≤ 前沿"):
        router_data.validate_ability_floors({
            "routine": 3.2,
            "hard": 3.1,
            "coupled": 3.6,
            "frontier": 4.2,
        })
    with pytest.raises(ValueError, match="常规 ≤ 较难 ≤ 耦合 ≤ 前沿"):
        router_data.validate_ability_floors({
            "routine": 2.3,
            "hard": 3.1,
            "coupled": 4.3,
            "frontier": 4.2,
        })

    # Out-of-bounds values are rejected
    with pytest.raises(ValueError):
        router_data.validate_ability_floors({
            "routine": 0.5,
            "hard": 3.1,
            "coupled": 3.6,
            "frontier": 4.2,
        })

    # Interactive drag clamping stops against adjacent floors
    current = {"routine": 2.3, "hard": 3.1, "coupled": 3.6, "frontier": 4.2}
    assert router_data.clamp_ability_floor_drag("hard", 1.8, current) == 2.3
    assert router_data.clamp_ability_floor_drag("hard", 3.9, current) == 3.6
    assert router_data.clamp_ability_floor_drag("hard", 3.35, current) == 3.35


def test_eligibility_preview_as_floor_moves(isolated_state: Path) -> None:
    accounts = allocator.load_accounts(allocator.reference_fleet_path())
    # Raise 'coupled' floor from 3.6 to 3.95: models at 3.85 (Sol 6.1) and 3.90 (Sonnet 5.5) lose eligibility
    preview_up = router_data.preview_ability_floor_change(
        floors_override={"routine": 2.3, "hard": 3.1, "coupled": 3.95, "frontier": 4.2},
        accounts=accounts,
    )
    assert preview_up["has_changes"] is True
    coupled_diff = preview_up["difficulties"]["coupled"]
    assert coupled_diff["changed"] is True
    assert len(coupled_diff["lost_models"]) > 0
    assert len(coupled_diff["lost_bundles"]) > 0
    assert len(coupled_diff["gained_models"]) == 0

    # Lower 'hard' floor from 3.1 to 2.9: Flash 3.8 (3.0) gains eligibility
    preview_down = router_data.preview_ability_floor_change(
        floors_override={"routine": 2.3, "hard": 2.9, "coupled": 3.6, "frontier": 4.2},
        accounts=accounts,
    )
    assert preview_down["has_changes"] is True
    hard_diff = preview_down["difficulties"]["hard"]
    assert hard_diff["changed"] is True
    assert len(hard_diff["gained_models"]) > 0
    assert len(hard_diff["gained_bundles"]) > 0


def test_routing_parity_with_allocator_plan(isolated_state: Path) -> None:
    """Changing a difficulty floor or route floor through the tray API immediately changes allocator.plan."""
    usage = {"accounts": {}}

    # 1. Baseline plan for a general_implementation hard task
    task_hard = {
        "objective": "Fix a module bug with unit tests",
        "work_kind": "general_implementation",
        "difficulty": "hard",
        "expected_hours": 0.5,
        "needs": {"local_only": False, "visual": False},
    }
    plan_before = allocator.plan(task_hard, usage)
    assert plan_before["decision"]["task"]["demand"]["floor"] == 3.1

    # Raise hard floor to 3.55 via tray API
    router_data.save_ability_floors({"hard": 3.55, "coupled": 3.60})
    chart_after = router_data.ability_floor_chart_data(include_manual_billing=True, include_superseded=True)
    plan_after = allocator.plan(task_hard, usage)
    assert plan_after["decision"]["task"]["demand"]["floor"] == 3.55

    # Every candidate's below_floor status in allocator.plan matches chart_after["bundles"][bid]["clears"]["hard"]
    for cand in plan_after["ranking"]:
        bid = cand["id"]
        if bid in chart_after["bundles"]:
            clears_hard = chart_after["bundles"][bid]["clears"]["hard"]
            below_floor = bool(cand.get("below_floor"))
            assert clears_hard == (not below_floor), f"Mismatch for {bid}: chart={clears_hard}, plan below_floor={below_floor}"

    # 2. Route-specific floor parity: tool_execution hard
    task_tool = {
        "objective": "Run the release checklist and test suite",
        "work_kind": "tool_execution",
        "difficulty": "hard",
        "expected_hours": 0.5,
        "needs": {"local_only": True, "visual": False},
    }
    plan_tool_before = allocator.plan(task_tool, usage)
    assert plan_tool_before["decision"]["task"]["demand"]["floor"] == 2.7

    router_data.save_ability_floors({}, route_floors={"tool_execution.hard": 3.05})
    plan_tool_after = allocator.plan(task_tool, usage)
    assert plan_tool_after["decision"]["task"]["demand"]["floor"] == 3.05


def test_offscreen_rendering_light_dark_and_english(isolated_state: Path) -> None:
    accounts = allocator.load_accounts(allocator.reference_fleet_path())
    data = router_data.ability_floor_chart_data(accounts=accounts)
    preview = router_data.preview_ability_floor_change(
        floors_override={"routine": 2.3, "hard": 3.3, "coupled": 3.6, "frontier": 4.2},
        accounts=accounts,
    )

    for mode in ("light", "dark"):
        renderer = ability_floor_chart.AbilityFloorRenderer(width=880, theme_mode=mode, dpi_scale=1.25)
        img = renderer.render(data, hovered_row=0, active_handle="hard", preview_diff=preview)
        assert img.size[0] == 880
        assert img.size[1] > 300
        # Ensure hitboxes were populated for all 4 difficulty floors + route floor + model rows
        handle_ids = {h["id"] for h in renderer.handle_hitboxes}
        for d in ("routine", "hard", "coupled", "frontier"):
            assert d in handle_ids
            hb = next(h for h in renderer.handle_hitboxes if h["id"] == d)
            cx = (hb["bbox"][0] + hb["bbox"][2]) / 2
            cy = (hb["bbox"][1] + hb["bbox"][3]) / 2
            hit = renderer.hit_test_handle(cx, cy)
            assert hit is not None and hit["id"] == d
            # Round-trip coordinate mapping
            mapped_val = renderer.x_to_value(hb["line_x"])
            assert abs(mapped_val - data["floors"][d]) <= 0.05
        assert len(renderer.row_hitboxes) == len(data["models"])

    # Verify English translation rendering
    prev_lang = i18n.get_language()
    try:
        i18n.set_language("en")
        renderer_en = ability_floor_chart.AbilityFloorRenderer(width=880, theme_mode="light", dpi_scale=1.0)
        img_en = renderer_en.render(data, hovered_row=1)
        assert img_en.size[0] == 880
    finally:
        i18n.set_language(prev_lang)
