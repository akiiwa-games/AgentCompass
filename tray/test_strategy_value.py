"""Strategy-page harness factors and opportunity exchange. Temporary files only."""
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_router_data_models import settings
import i18n
import router_data


EXCHANGE = {"score_per_displaced_usd": 0.012, "score_per_displaced_worker_hour": 0.06}


def catalog(tmp_path, scoring):
    path = tmp_path / "references" / "accounts.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["scoring"].update(scoring)
    path.write_text(json.dumps(document), encoding="utf-8")


def test_labels_have_english():
    assert i18n.english("（任意）") == "(any)"
    assert i18n.english("Harness 系数") == "Harness factor"
    assert i18n.english("机会成本汇率") == "Opportunity exchange"
    assert i18n.english("每美元分数") == "Score per USD"
    assert i18n.english("每工时分数") == "Score per worker-hour"
    assert i18n.english("添加一行") == "Add row"
    assert i18n.english("删除") == "Remove"
    assert "new keys" in i18n.english("汇率只写入新键，不能包含 {0}")


def test_save_without_the_new_form_stays_sparse(tmp_path, monkeypatch):
    target = settings(tmp_path, monkeypatch)
    router_data.save_strategy({"safety_margin_pct": 8}, None, {})
    assert json.loads(target.read_text())["scoring"] == {"safety_margin_pct": 8}


def test_save_writes_rules_and_named_exchange_only(tmp_path, monkeypatch):
    target = settings(tmp_path, monkeypatch)
    catalog(tmp_path, {
        "harness_factors": {
            "default": 1.0,
            "basis": "table basis",
            "by_harness": {"pi": 1.0},
            "rules": [
                {"harness": "antigravity", "model_prefix": "claude", "factor": 0.9, "basis": "agy"},
                {"harness": "antigravity-resilient", "model_prefix": "claude", "factor": 0.9},
            ],
        },
        "shortage_loss_per_usd": 0.012,
        "shortage_loss_per_worker_hour": 0.06,
        "opportunity_exchange": {**EXCHANGE, "basis": "rate basis"},
    })
    assert router_data.read_harness_factors()["rules"][0]["basis"] == "agy"
    assert router_data.read_opportunity_exchange() == EXCHANGE
    ratings = {"divergence": 0.8, "global_rigor": 0.7, "module_precision": 0.9}
    router_data.save_strategy(
        {"safety_margin_pct": 8}, "gpt-6.1-sol", ratings,
        harness_factors={"default": "1", "rules": [
            {"harness": "antigravity", "model_prefix": "claude", "factor": "0.9", "basis": "agy"},
            {"harness": "codex", "work_kind": "debug", "factor": 1.1},
        ]},
        opportunity_exchange={"score_per_displaced_usd": "0.02", "score_per_displaced_worker_hour": 0.06})
    saved = json.loads(target.read_text())
    assert saved["models"]["gpt-6.1-sol"]["subjective"] == ratings
    assert saved["scoring"]["safety_margin_pct"] == 8
    assert "shortage_loss_per_usd" not in saved["scoring"]
    assert "shortage_loss_per_worker_hour" not in saved["scoring"]
    assert set(saved["scoring"]["opportunity_exchange"]) == set(EXCHANGE)
    assert saved["scoring"]["opportunity_exchange"]["score_per_displaced_usd"] == 0.02
    rules = saved["scoring"]["harness_factors"]["rules"]
    assert rules[0]["basis"] == "agy" and rules[1] == {"harness": "codex", "work_kind": "debug", "factor": 1.1}
    assert "by_harness" not in saved["scoring"]["harness_factors"]
    merged = router_data.read_harness_factors()
    assert [row["harness"] for row in merged["rules"]] == ["antigravity", "codex"]
    assert router_data.read_opportunity_exchange()["score_per_displaced_usd"] == 0.02
    router_data.restore_tuning()
    assert "harness_factors" not in json.loads(target.read_text()).get("scoring", {})
    assert [row["harness"] for row in router_data.read_harness_factors()["rules"]] == [
        "antigravity", "antigravity-resilient"]
    assert router_data.read_opportunity_exchange() == EXCHANGE


def test_legacy_rate_is_shown_then_saved_as_named_keys(tmp_path, monkeypatch):
    target = settings(tmp_path, monkeypatch)
    catalog(tmp_path, {"shortage_loss_per_usd": 0.02, "shortage_loss_per_worker_hour": 0.07})
    assert router_data.read_opportunity_exchange() == {
        "score_per_displaced_usd": 0.02, "score_per_displaced_worker_hour": 0.07}
    target.write_text(json.dumps({"schema_version": 2, "scoring": {
        "shortage_loss_per_usd": 0.03,
        "opportunity_exchange": {"basis": "keep", "shortage_loss_per_worker_hour": 0.5}}}))
    shown = router_data.read_opportunity_exchange()
    assert shown["score_per_displaced_usd"] == 0.03
    assert shown["score_per_displaced_worker_hour"] == 0.07
    router_data.save_strategy({}, None, {}, opportunity_exchange=shown)
    saved = json.loads(target.read_text())["scoring"]
    assert "shortage_loss_per_usd" not in saved
    assert "shortage_loss_per_worker_hour" not in saved["opportunity_exchange"]
    assert saved["opportunity_exchange"]["basis"] == "keep"
    assert saved["opportunity_exchange"]["score_per_displaced_usd"] == 0.03
    assert saved["opportunity_exchange"]["score_per_displaced_worker_hour"] == 0.07


def test_invalid_rows_and_legacy_keys_do_not_write(tmp_path, monkeypatch):
    target = settings(tmp_path, monkeypatch)
    router_data.save_strategy({"safety_margin_pct": 8}, None, {})
    before = target.read_bytes()
    rejected = [
        {"harness_factors": {"default": 1, "rules": [{"factor": 0.9}]}},
        {"harness_factors": {"default": 99, "rules": []}},
        {"opportunity_exchange": {"shortage_loss_per_usd": 0.02, "score_per_displaced_usd": 0.02,
                                  "score_per_displaced_worker_hour": 0.06}},
        {"opportunity_exchange": {"score_per_displaced_usd": -1, "score_per_displaced_worker_hour": 0.06}},
        {"harness_factors": {"default": 1, "rules": [{"harness": "codex", "factor": "nope"}]}},
    ]
    for extra in rejected:
        with pytest.raises(ValueError):
            router_data.save_strategy({"safety_margin_pct": 9}, None, {}, **extra)
        assert target.read_bytes() == before


def test_choices_come_from_router_values_and_factor_is_zero_to_two(tmp_path, monkeypatch):
    settings(tmp_path, monkeypatch)
    (tmp_path / "references" / "model-capabilities.json").write_text(
        json.dumps({"routes": {"general_implementation": {}, "debug_review": {}}}), encoding="utf-8")
    path = tmp_path / "references" / "accounts.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["scoring"]["harness_factors"] = {"default": 1, "rules": [
        {"harness": "antigravity", "model_prefix": "claude", "factor": 0.9}]}
    path.write_text(json.dumps(document), encoding="utf-8")
    choices = router_data.strategy_factor_choices()
    assert choices["harness"][:4] == ["claude", "codex", "pi", "antigravity-resilient"]
    assert "antigravity" in choices["harness"]
    assert choices["work_kind"] == ["general_implementation", "debug_review"]
    assert "gpt-6.1-sol" in choices["model"] and "other" in choices["model"]
    assert router_data.HARNESS_FACTOR_RANGE == (0.0, 2.0)
    kept = router_data.validate_harness_factors({"default": 0, "rules": [
        {"harness": "（任意）", "model_prefix": "claude", "factor": 2}]})
    assert kept["rules"] == [{"model_prefix": "claude", "factor": 2.0}]
    with pytest.raises(ValueError):
        router_data.validate_harness_factors({"default": 1, "rules": [{"harness": "codex", "factor": 2.01}]})
    with pytest.raises(ValueError):
        router_data.validate_harness_factors({"default": -0.01, "rules": []})


def test_existing_by_model_map_survives_a_rules_edit(tmp_path, monkeypatch):
    target = settings(tmp_path, monkeypatch)
    target.write_text(json.dumps({"schema_version": 2, "scoring": {"harness_factors": {
        "by_model": {"gpt-6.1-sol": {"codex": 1.2}}}}}))
    router_data.save_strategy({}, None, {}, harness_factors={
        "default": 1, "rules": [{"harness": "pi", "factor": 1}]})
    table = json.loads(target.read_text())["scoring"]["harness_factors"]
    assert table["by_model"]["gpt-6.1-sol"]["codex"] == 1.2
    assert table["rules"] == [{"harness": "pi", "factor": 1.0}]
    assert table["default"] == 1.0
