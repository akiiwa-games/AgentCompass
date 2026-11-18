"""What the scoring page shows: the four categories, the models' category scores and the manual scores, as the router reads them.

Plain Python over the router's own data (five-axis-scores.json and the model rows of axis_scores.five_axis_overlay), so it is
unit-tested without a display. Levels are on the classifier's demand scale through user_scores.z_to_level (1 = the panel's
weakest, 2 = the panel median, 3 = the panel's best; above 3 is shown as the number it is); the manual-score writer is user_scores, the same code path as
`agentcompass score set|clear` (local file only).
"""
from __future__ import annotations

from i18n import translate as _tr, LocalizedSequence

# (identifier, label): the four categories in the router's order (axis_scores.FIVE_AXES).
CATEGORIES = LocalizedSequence((("creativity", "创意"), ("coding", "编码能力"), ("aesthetics", "审美能力"),
                                ("tool_operation", "工具操作")))
FACET_LABELS = {"creative_writing": "创意写作", "execution": "执行", "faithfulness": "忠实度", "design": "设计",
                "responsiveness": "响应速度", "image_recognition": "图像识别"}
STATUS_LABELS = {"measured": "实测", "measured_partial": "部分实测", "partial": "部分", "estimated": "估算",
                 "interpolated": "换算", "missing": "缺失", "user": "本机覆盖", "maintainer": "维护者"}
# The source a status comes from, as the manual-score UI names it.
SOURCE_OF = {"user": "本机覆盖", "maintainer": "维护者", "measured": "实测", "measured_partial": "实测", "partial": "部分",
             "estimated": "估算", "interpolated": "估算"}
EFFORT = "high"     # the effort the overview reads; the hover lists every effort the data has


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _usd(value) -> str:
    if not _number(value):
        return "—"
    return f"${value:,.0f}" if abs(value) >= 10 else f"${value:.1f}"


def demand_grid_n() -> int:
    import quota_economy
    return len(quota_economy.FIVE_AXIS_DEMAND_GRID)


def level_text(level) -> str:
    """A level on the demand scale for display: one decimal, never below 0; above 3 it is the extrapolated number."""
    if not _number(level):
        return "—"
    return f"{max(level, 0.0):.1f}"


def category_label(key: str) -> str:
    return next((label for k, label in CATEGORIES if k == key), key)


def facet_label(key: str) -> str:
    return _tr(FACET_LABELS.get(key, key))


def status_label(status) -> str:
    return _tr(STATUS_LABELS.get(status, str(status or "missing")))


def source_label(status) -> str:
    return _tr(SOURCE_OF.get(status, "缺失"))


def facets_of(category: str) -> tuple:
    import user_scores
    return tuple(user_scores.CATEGORY_FACETS[category])


def display_facets_of(category: str) -> tuple:
    import axis_scores
    if category in axis_scores.LEVEL_FACETS:
        return tuple(axis_scores.LEVEL_FACETS[category][3])
    return facets_of(category)


def _facet_owner(category: str, facet: str) -> str:
    import axis_scores
    return axis_scores.FACET_CATEGORY.get(facet, category)


def _levels(targets: dict, category: str, facet: str) -> dict:
    own = ((((targets.get(category) or {}).get("facets") or {}).get(facet) or {}).get("levels")) or {}
    if own:
        return own
    owner = _facet_owner(category, facet)
    if owner != category:
        return ((((targets.get(owner) or {}).get("facets") or {}).get(facet) or {}).get("levels")) or {}
    return {}


def level3_cap(targets: dict, category: str, facet: str) -> dict | None:
    """The level-3 cap the fit reads for this facet's category (axis_scores.five_axis_targets): the category's ceiling,
    and for per-facet categories (`aesthetics`, `tool_operation`) the ceiling of this facet when level 3 asks for it."""
    level3 = (((targets.get(category) or {}).get("levels") or {}).get("3")) or {}
    if "by_facet" in level3:
        return ((level3.get("by_facet") or {}).get(facet) or {}).get("ceiling")
    return level3.get("ceiling")


def continuity_derivation(targets: dict, category: str) -> tuple[float | None, str]:
    """(the category's continuity value, how it is derived): the largest t3 − t2 over its facets, as the data computed it
    (a per-facet category reads the level-3 task's target of each facet)."""
    value = (targets.get(category) or {}).get("continuity")
    by_facet = ((((targets.get(category) or {}).get("levels") or {}).get("3")) or {}).get("by_facet") or {}
    gaps = []
    for facet in display_facets_of(category):
        lv = _levels(targets, category, facet)
        t2, t3 = (lv.get("2") or {}).get("target"), ((by_facet.get(facet) or lv.get("3")) or {}).get("target")
        if _number(t2) and _number(t3):
            gaps.append((t3 - t2, facet, t2, t3))
    if not gaps:
        return value if _number(value) else None, ""
    gap, facet, t2, t3 = max(gaps)
    text = _tr("max(t3 − t2)，取 {0}：{1:.3f} − ({2:.3f}) = {3:.3f}").format(facet_label(facet), t3, t2, gap)
    return (value if _number(value) else None), text


def category_overview(data: dict, models: dict) -> list[dict]:
    """One block per category: facets with their boards, data dates and panel targets per level, coverage and continuity."""
    anchor = data.get("anchor") or {}
    targets = anchor.get("targets") or {}
    links = data.get("links") or {}
    ages = data.get("source_ages") or {}
    out = []
    for key, label in CATEGORIES:
        facets = []
        level3_block = (((targets.get(key) or {}).get("levels") or {}).get("3")) or {}
        per_facet = "by_facet" in level3_block
        for facet in display_facets_of(key):
            owner = _facet_owner(key, facet)
            link = links.get(f"{key}.{facet}") or links.get(f"{owner}.{facet}") or {}
            boards = list(link.get("boards") or ())
            dates = sorted(str((ages.get(b) or {}).get("as_of")) for b in boards if (ages.get(b) or {}).get("as_of"))
            levels = _levels(targets, key, facet)
            facets.append({"key": facet, "label": facet_label(facet), "boards": boards,
                           "panel_n": link.get("panel_n"), "dates": (dates[0], dates[-1]) if dates else None,
                           "targets": {lv: ((levels.get(lv) or {}).get("target"), (levels.get(lv) or {}).get("statistic"))
                                       for lv in ("1", "2", "3")},
                           "cap": level3_cap(targets, key, facet),
                           "facet_best": None if per_facet else (levels.get("3") or {}).get("ceiling")})
        coverage = {}
        for row in models.values():
            status = (((row or {}).get("five_axis") or {}).get("axis_status") or {}).get(key, "missing")
            coverage[status] = coverage.get(status, 0) + 1
        continuity, derivation = continuity_derivation(targets, key)
        out.append({"key": key, "label": label, "facets": facets, "coverage": coverage,
                    "continuity": continuity, "derivation": derivation})
    return out


def coverage_text(coverage: dict) -> str:
    order = list(STATUS_LABELS)
    items = sorted(coverage.items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else len(order))
    return " · ".join(f"{status_label(status)} {count}" for status, count in items)


def facet_dates_text(facet: dict) -> str:
    if not facet["dates"]:
        return "—"
    first, last = facet["dates"]
    return first if first == last else f"{first} – {last}"


def targets_text(facet: dict) -> str:
    return "  ".join(f"L{lv} {value:+.3f}" if _number(value) else f"L{lv} —"
                     for lv, (value, _) in facet["targets"].items())


def z_targets_note() -> str:
    return _tr("面板标准分 z：以锚点模型 High 档为 0.000，1 个单位为 1 个面板标准差（L1 面板最弱、L2 面板中位数、L3 锚点）。")


def targets_tip(facet: dict) -> str:
    rows = [_tr("等级 {0}：{1}（{2}）").format(lv, f"{value:+.4f}" if _number(value) else "—", statistic or "—")
            for lv, (value, statistic) in facet["targets"].items()]
    cap = facet.get("cap") or {}
    if _number(cap.get("value")):
        rows.append(_tr("等级 3 上限（契合度读取）：{0}（{1}）；等级 1、2 封顶在目标").format(
            f"{cap['value']:+.4f}", cap.get("model") or "—"))
    best = facet.get("facet_best") or {}
    if _number(best.get("value")):
        rows.append(_tr("本分项最高实测：{0}（{1}）").format(f"{best['value']:+.4f}", best.get("model") or "—"))
    return "\n".join([z_targets_note(), *rows])


def economy_text(windows: list[dict] | None, grid_n: int | None = None) -> str:
    rows = [w for w in (windows or ()) if isinstance(w, dict) and w.get("label")]
    if not rows:
        return _tr("额度经济：暂无已计价的长周期窗口读数")
    shown = [_tr("{0} {1}/{2}（线 {3} · λ {4:.4f}）").format(
        w["label"], _usd(w.get("used_usd")), _usd(w.get("capacity_usd")),
        _usd(w.get("recovery_line_usd")), float(w.get("quota_price") or 0.0)) for w in rows[:4]]
    tail = _tr(" 等 {0} 个窗口").format(len(rows)) if len(rows) > 4 else ""
    return _tr("额度经济（实时窗口）：{0}").format(" · ".join(shown)) + tail


def economy_tip(windows: list[dict] | None, grid_n: int | None = None) -> str:
    n = grid_n if isinstance(grid_n, int) and grid_n > 0 else demand_grid_n()
    lines = [_tr("{0}：已用 {1} / {2} · 恢复线 {3} · π {4:.3f} · λ {5:.5f}").format(
        w["label"], _usd(w.get("used_usd")), _usd(w.get("capacity_usd")),
        _usd(w.get("recovery_line_usd")), float(w.get("need_probability") or 0.0), float(w.get("quota_price") or 0.0))
        for w in (windows or ()) if isinstance(w, dict) and w.get("label")]
    lines.append(_tr(
        "候选成本 = 在约束长周期窗口上对本单花费 y 积分额度价格 λ(y)；λ 来自该账号价值阶梯（在 {0} 点需求网格上平均）"
        "与本周期需求概率 π。5 小时窗口只判断能否放下，不计价。净分 N = 契合 + 连续性 + 临时加成 − 成本。").format(n))
    return "\n".join(lines)


def _at(cell: dict | None, effort: str = EFFORT):
    import axis_scores
    return axis_scores._at_effort(cell, effort) if isinstance(cell, dict) else None


def _level(targets: dict, target: str, z):
    import user_scores
    if z is None:
        return None
    try:
        return user_scores.level_for_target_z(target, z, targets)
    except ValueError:
        return None


def facet_cell(targets: dict, category: str, facet: str, cell: dict | None) -> dict:
    """One facet of one model: its z at the overview effort, the level, the status and the evidence for the hover."""
    cell = cell if isinstance(cell, dict) else {}
    z = _at(cell)
    status = cell.get("status", "missing") if cell else "missing"
    owner = _facet_owner(category, facet)
    return {"z": z, "level": _level(targets, f"{owner}.{facet}", z), "status": status,
            "manual": cell.get("manual"), "evidence": evidence_lines(cell)}


def evidence_lines(cell: dict) -> list[str]:
    """Where a score cell comes from: its boards with the listed name, its estimate, or the manual entry."""
    lines = []
    by_effort = cell.get("by_effort") if isinstance(cell.get("by_effort"), dict) else {}
    if by_effort:
        lines.append(" · ".join(f"{e} {v:+.3f}" for e, v in by_effort.items() if _number(v)))
    manual = cell.get("manual")
    if isinstance(manual, dict):
        lines.append(_tr("手动 {0}：等级 {1} · {2}").format(source_label(manual.get("kind")), manual.get("level"),
                                                     manual.get("as_of") or "—")
                     + (f" · {manual['note']}" if manual.get("note") else ""))
        ref = manual.get("data_reference")
        if isinstance(ref, dict) and _number(ref.get("level")) and ref.get("status") not in ("missing", None):
            srcs = ", ".join(ref.get("sources") or ())
            delta = ref.get("delta_level")
            delta_txt = f" ({delta:+.1f})" if _number(delta) else ""
            lines.append(_tr("公开数据对照：{0}{1} · {2}").format(
                level_text(ref["level"]), delta_txt, status_label(ref.get("status"))
            ) + (f" ({srcs})" if srcs else ""))
    for board, src in (cell.get("sources") or {}).items():
        listed = (src or {}).get("listed_as") if isinstance(src, dict) else None
        lines.append(f"{board}" + (f" — {listed}" if listed else ""))
    estimate = cell.get("estimate")
    if isinstance(estimate, dict) and estimate:
        basis = estimate.get("base_model") or estimate.get("tier_source") or ""
        lines.append(_tr("估算：{0}").format(" · ".join(str(v) for v in (estimate.get("method"), basis) if v)))
    return lines


def model_rows(models: dict, targets: dict, label=lambda name: name) -> list[dict]:
    """Per model: every category's level, deciding facet and source, with its facets; `coding` uses its category z scale,
    while per-facet categories (`aesthetics`, `tool_operation`) take the weakest of their existing facets."""
    rows = []
    for name, info in sorted((models or {}).items(), key=lambda kv: str(label(kv[0])).lower()):
        five = (info or {}).get("five_axis") if isinstance(info, dict) else None
        if not isinstance(five, dict):
            continue
        cats = five.get("categories") or {}
        cells = {}
        for key, _ in CATEGORIES:
            cat_cell = cats.get(key) if isinstance(cats.get(key), dict) else {}
            facets = {}
            for f in display_facets_of(key):
                owner = _facet_owner(key, f)
                own_cat = cats.get(owner) if isinstance(cats.get(owner), dict) else {}
                raw_fc = (own_cat.get("facets") or {}).get(f)
                facets[f] = facet_cell(targets, key, f, raw_fc)
            present = [(f, fc) for f, fc in facets.items() if fc["z"] is not None and fc["level"] is not None]
            missing_facets = [f for f, fc in facets.items() if fc["level"] is None]
            if present:
                if key in ("aesthetics", "tool_operation"):
                    deciding_facet, deciding_fc = min(present, key=lambda kv: kv[1]["level"])
                    level = deciding_fc["level"]
                else:
                    deciding_facet, deciding_fc = min(present, key=lambda kv: kv[1]["z"])
                    z_val = _at(cat_cell)
                    level = _level(targets, key, z_val if z_val is not None else deciding_fc["z"])
                deciding_level = deciding_fc["level"]
                status = deciding_fc["status"]
            elif _at(cat_cell) is not None and key not in ("aesthetics", "tool_operation"):
                deciding_facet, deciding_level = None, None
                level = _level(targets, key, _at(cat_cell))
                status = cat_cell.get("status") or (five.get("axis_status") or {}).get(key, "missing")
            else:
                deciding_facet, deciding_level, level, status = None, None, None, "missing"
            manual = cat_cell.get("manual") if isinstance(cat_cell.get("manual"), dict) else None
            cells[key] = {"level": level, "status": status, "deciding_facet": deciding_facet,
                          "deciding_level": deciding_level, "missing_facets": missing_facets,
                          "manual": manual, "facets": facets, "evidence": evidence_lines(cat_cell)}
        rows.append({"model": name, "label": label(name), "cells": cells})
    return rows


def cell_text(cell: dict) -> str:
    return level_text(cell["level"])


def model_tip(row: dict) -> str:
    label, key = row.get("label") or "", row.get("model") or ""
    return f"{label} ({key})" if label and label != key else str(key or label)


def cell_tip(model_label: str, category: str, cell: dict, model_key: str | None = None) -> str:
    head = f"{model_label} ({model_key})" if model_key and model_key != model_label else model_label
    deciding = cell.get("deciding_facet")
    multi = len(cell.get("facets") or {}) > 1
    if multi and deciding and cell.get("deciding_level") is not None:
        summary = _tr("等级 {0}（决定项 {1} {2}）· {3} · 来源：{4}").format(
            level_text(cell["level"]), facet_label(deciding), level_text(cell["deciding_level"]),
            status_label(cell["status"]), source_label(cell["status"]))
    else:
        summary = _tr("等级 {0} · {1} · 来源：{2}").format(
            level_text(cell["level"]), status_label(cell["status"]), source_label(cell["status"]))
    lines = [f"{head} · {category_label(category)}", summary]
    for facet, fc in cell["facets"].items():
        lines.append(f"  {facet_label(facet)}: {level_text(fc['level'])} ({status_label(fc['status'])})")
        lines += [f"    {line}" for line in fc["evidence"]]
    missing = cell.get("missing_facets") or ()
    if missing and cell["level"] is not None:
        lines.append(_tr("缺失子维度：{0}").format("、".join(facet_label(f) for f in missing)))
    if multi:
        lines.append(_tr("类别等级按路由器的类别目标换算。"))
    if category == "aesthetics":
        lines.append(_tr("等级 1 仅看设计；等级 2、3 取设计与图像识别（与工具操作共用）中较弱者。"))
    if category == "tool_operation":
        lines.append(_tr("工具操作等级 3 的任务对响应速度的要求与等级 2 相同（面板中位数），另加图像识别。"))
    lines.append(_tr("等级 3 = 面板最强；高于 3 的等级按 t3 − t2 外推，照实显示。"))
    return "\n".join(lines)


def targets_choices(category: str) -> list[tuple[str, str]]:
    """(user_scores target, label) for the manual-score editor: the whole category, then each facet the CLI accepts."""
    out = [(category, _tr("整个类别"))]
    out += [(f"{category}.{facet}", facet_label(facet)) for facet in facets_of(category)]
    return out


def current_manual(row: dict, target: str) -> tuple[float | None, str]:
    """(the current level of a model on a target, its source) for the editor."""
    category, _, facet = target.partition(".")
    cell = row["cells"][category]
    if facet:
        fc = cell["facets"][facet]
        return fc["level"], fc["status"]
    return cell["level"], cell["status"]


def editor_current_text(row: dict, target: str) -> str:
    category, _, facet = target.partition(".")
    cell = row["cells"][category]
    if facet:
        fc = cell["facets"][facet]
        return _tr("当前 {0}（来源：{1}）").format(level_text(fc["level"]), source_label(fc["status"]))
    multi = len(cell.get("facets") or {}) > 1
    deciding = cell.get("deciding_facet")
    parts = []
    if multi and deciding and cell.get("deciding_level") is not None:
        parts.append(_tr("决定项 {0} {1}").format(facet_label(deciding), level_text(cell["deciding_level"])))
    parts.append(_tr("来源：{0}").format(source_label(cell["status"])))
    if multi:
        parts.append(_tr("类别等级按路由器的类别目标换算"))
    missing = cell.get("missing_facets") or ()
    if missing and cell["level"] is not None:
        parts.append(_tr("缺失：{0}").format("、".join(facet_label(f) for f in missing)))
    return _tr("当前 {0}（{1}）").format(level_text(cell["level"]), " · ".join(parts))


def prefill_level(row: dict, target: str) -> float | None:
    """Exact level to prefill in the editor, or None when prefilling would lose information (multi-facet aggregation,
    or a level below 1)."""
    category, _, facet = target.partition(".")
    cell = row["cells"][category]
    if facet:
        fc = cell["facets"][facet]
        manual = fc.get("manual")
        if isinstance(manual, dict) and manual.get("target") == target and _number(manual.get("level")):
            return float(manual["level"])
        level = fc.get("level")
        if _number(level) and level >= 1.0:
            return round(float(level), 1)
        return None
    manual = cell.get("manual")
    if isinstance(manual, dict) and manual.get("target") == target and _number(manual.get("level")):
        return float(manual["level"])
    if len(cell.get("facets") or {}) == 1:
        only = next(iter(cell["facets"].values()))
        level = only.get("level")
        if _number(level) and level >= 1.0:
            return round(float(level), 1)
    return None
