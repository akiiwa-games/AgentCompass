"""What the scoring page shows: the four categories, the models' category scores and the manual scores, as the router reads them.

Plain Python over the router's own data (five-axis-scores.json and the model rows of axis_scores.five_axis_overlay), so it is
unit-tested without a display. Levels are on the classifier's demand scale through user_scores.z_to_level (1 = the panel's
weakest, 2 = the panel median, 3 = the anchor); the manual-score writer is user_scores, the same code path as
`agentcompass score set|clear` (local file only).
"""
from __future__ import annotations

from i18n import translate as _tr, LocalizedSequence

# (identifier, label): the four categories in the router's order (axis_scores.FIVE_AXES).
CATEGORIES = LocalizedSequence((("divergence", "创意"), ("coding", "编码能力"), ("aesthetics", "审美能力"),
                                ("tool_operation", "工具操作")))
FACET_LABELS = {"creative_writing": "创意写作", "execution": "执行", "faithfulness": "忠实度", "design": "设计",
                "responsiveness": "响应速度", "image_recognition": "图像识别"}
STATUS_LABELS = {"measured": "实测", "measured_partial": "部分实测", "partial": "部分", "estimated": "估算",
                 "interpolated": "换算", "missing": "缺失", "user": "本机覆盖", "maintainer": "维护者"}
# The source a status comes from, as the manual-score UI names it.
SOURCE_OF = {"user": "本机覆盖", "maintainer": "维护者", "measured": "实测", "measured_partial": "实测", "partial": "实测",
             "estimated": "估算", "interpolated": "估算"}
EFFORT = "high"     # the effort the overview reads; the hover lists every effort the data has


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def level_text(level) -> str:
    """A level on the 1..3 scale for display: clamped at 3 and badged "3.0+" above it (extrapolation there is narrow)."""
    if not _number(level):
        return "—"
    return "3.0+" if level > 3.0 else f"{max(level, 0.0):.1f}"


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


def _levels(targets: dict, category: str, facet: str) -> dict:
    return ((((targets.get(category) or {}).get("facets") or {}).get(facet) or {}).get("levels")) or {}


def continuity_derivation(targets: dict, category: str) -> tuple[float | None, str]:
    """(the category's continuity value, how it is derived): the largest t3 − t2 over its facets, as the data computed it."""
    value = (targets.get(category) or {}).get("continuity")
    gaps = []
    for facet in facets_of(category):
        lv = _levels(targets, category, facet)
        t2, t3 = (lv.get("2") or {}).get("target"), (lv.get("3") or {}).get("target")
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
        for facet in facets_of(key):
            link = links.get(f"{key}.{facet}") or {}
            boards = list(link.get("boards") or ())
            dates = sorted(str((ages.get(b) or {}).get("as_of")) for b in boards if (ages.get(b) or {}).get("as_of"))
            levels = _levels(targets, key, facet)
            facets.append({"key": facet, "label": facet_label(facet), "boards": boards,
                           "panel_n": link.get("panel_n"), "dates": (dates[0], dates[-1]) if dates else None,
                           "targets": {lv: ((levels.get(lv) or {}).get("target"), (levels.get(lv) or {}).get("statistic"))
                                       for lv in ("1", "2", "3")}})
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


def targets_tip(facet: dict) -> str:
    return "\n".join(_tr("等级 {0}：{1}（{2}）").format(lv, f"{value:+.4f}" if _number(value) else "—", statistic or "—")
                     for lv, (value, statistic) in facet["targets"].items())


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
    return {"z": z, "level": _level(targets, f"{category}.{facet}", z), "status": status,
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
    for board, src in (cell.get("sources") or {}).items():
        listed = (src or {}).get("listed_as") if isinstance(src, dict) else None
        lines.append(f"{board}" + (f" — {listed}" if listed else ""))
    estimate = cell.get("estimate")
    if isinstance(estimate, dict) and estimate:
        basis = estimate.get("base_model") or estimate.get("tier_source") or ""
        lines.append(_tr("估算：{0}").format(" · ".join(str(v) for v in (estimate.get("method"), basis) if v)))
    return lines


def model_rows(models: dict, targets: dict, label=lambda name: name) -> list[dict]:
    """Per model: every category's level, status and source, with its facets; the category level is the router's reading
    (coding is min over its facets in the data; tool_operation the weakest of its level-3 facets)."""
    rows = []
    for name, info in sorted((models or {}).items(), key=lambda kv: str(label(kv[0])).lower()):
        five = (info or {}).get("five_axis") if isinstance(info, dict) else None
        if not isinstance(five, dict):
            continue
        cats = five.get("categories") or {}
        cells = {}
        for key, _ in CATEGORIES:
            cat_cell = cats.get(key) if isinstance(cats.get(key), dict) else {}
            facets = {f: facet_cell(targets, key, f, (cat_cell.get("facets") or {}).get(f)) for f in facets_of(key)}
            status = (five.get("axis_status") or {}).get(key, "missing")
            if key == "tool_operation":
                levels = [c["level"] for c in facets.values()]
                level = min(levels) if levels and all(lv is not None for lv in levels) else None
            else:
                level = _level(targets, key, _at(cat_cell))
            manual = cat_cell.get("manual") if isinstance(cat_cell.get("manual"), dict) else None
            cells[key] = {"level": level, "status": status, "manual": manual, "facets": facets,
                          "evidence": evidence_lines(cat_cell)}
        rows.append({"model": name, "label": label(name), "cells": cells})
    return rows


def cell_text(cell: dict) -> str:
    mark = {"user": " ✎", "maintainer": " ◆", "estimated": " ~", "interpolated": " ~", "missing": ""}.get(cell["status"], "")
    return level_text(cell["level"]) + mark


def cell_tip(model_label: str, category: str, cell: dict) -> str:
    lines = [f"{model_label} · {category_label(category)}",
             _tr("等级 {0} · {1} · 来源：{2}").format(level_text(cell["level"]), status_label(cell["status"]),
                                               source_label(cell["status"]))]
    for facet, fc in cell["facets"].items():
        lines.append(f"  {facet_label(facet)}: {level_text(fc['level'])} ({status_label(fc['status'])})")
        lines += [f"    {line}" for line in fc["evidence"]]
    if category == "tool_operation":
        lines.append(_tr("响应速度的等级 ≥ 2 表示「至少达到面板中位数」：t2 与 t3 相同。"))
    lines.append(_tr("高于 3 的等级按 t3 − t2 外推，区间很窄，显示为 3.0+。"))
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
