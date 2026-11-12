"""Short display names for models, bundles and work kinds; unknown ids fall back to themselves.

Visible text uses these labels; raw ids stay in tooltips, plain-text mirrors and details.
"""
from __future__ import annotations

import json
from pathlib import Path

import i18n
import router_priority

_CATALOG_LABELS_CACHE: tuple[tuple, dict[str, str]] | None = None

WORK_KINDS = {
    "direction_reasoning": "方向推演", "complex_build_plan": "复杂构建规划",
    "debug_review": "调试评审", "text_in_text_out": "文本处理",
    "exploration_analysis": "探索分析", "research_packet": "研究任务",
    "bounded_bulk_work": "批量处理", "general_implementation": "通用实现",
    "self_contained_code": "独立模块实现", "tool_execution": "工具执行",
    "web_research": "网络调研", "visual_analysis": "视觉分析",
    "frontend_implementation": "前端界面实现", "research": "开放式方案调研",
}
DIFFICULTIES = {"routine": "常规", "hard": "困难", "coupled": "高耦合", "extreme": "极难", "easy": "简单", "medium": "中等"}
FAST_SUFFIX = "+fast"      # fast_modes.ID_SUFFIX: the router's id for a bundle's fast-mode twin
EFFORTS = {"low": "低", "medium": "中", "high": "高", "xhigh": "极高", "max": "最高", "ultra": "超高"}


def _tr(text):
    return i18n.translate(text)


def _catalog_path() -> Path:
    try:
        router_priority.modules()
        import account_fleet
        import public_data
        return public_data.effective(account_fleet.REFERENCES / "accounts.json")
    except Exception:
        return Path(__file__).resolve().parents[1] / "skills" / "agent-compass" / "references" / "accounts.json"


def catalog_labels() -> dict[str, str]:
    """{model_key: label} from the catalog (`accounts.json` `models[key].label` and `same_model` aliases)."""
    global _CATALOG_LABELS_CACHE
    path = _catalog_path()
    try:
        st = path.stat()
        stamp = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    if _CATALOG_LABELS_CACHE is not None and _CATALOG_LABELS_CACHE[0] == stamp:
        return _CATALOG_LABELS_CACHE[1]
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    labels = {name: str(spec["label"]) for name, spec in (doc.get("models") or {}).items()
              if isinstance(spec, dict) and isinstance(spec.get("label"), str) and spec["label"].strip()}
    for alias, entry in (doc.get("same_model") or {}).items():
        target = entry.get("target") if isinstance(entry, dict) else None
        if alias not in labels and isinstance(target, str) and target in labels:
            labels[alias] = labels[target]
    _CATALOG_LABELS_CACHE = (stamp, labels)
    return labels


def model_label(model: str) -> str:
    return catalog_labels().get(model, model)


def kind_label(value):
    return _tr(WORK_KINDS.get(value, value or "—"))


def difficulty_label(value):
    return _tr(DIFFICULTIES.get(value, value or "—"))


def effort_text(effort):
    return _tr(EFFORTS.get(effort, effort)) if effort else ""


def candidate_label(row):
    """Opus 5.5 · High for a candidate row or bundle dict (Opus 5.5 · High · Fast for its fast-mode variant); the raw id when
    no model is recorded."""
    row = row or {}
    model = row.get("model")
    if not model:
        return str(row.get("id") or row.get("bundle_id") or "—")
    effort = effort_text(row.get("effort"))
    fast = _tr("快速") if row.get("fast") is True else ""
    return model_label(str(model)) + "".join(" · " + part for part in (effort, fast) if part)


def choice_label(row):
    """The winner of a ledger plan row, from its bundle or its snapshot ranking."""
    choice = row.get("choice")
    bundle = row.get("bundle") if isinstance(row.get("bundle"), dict) else {}
    if not bundle.get("model"):
        snapshot = row.get("decision_snapshot") or row.get("snapshot") or row
        ranking = snapshot.get("ranking") if isinstance(snapshot, dict) else None
        found = next((c for c in ranking or [] if isinstance(c, dict) and c.get("id") == choice), {})
        bundle = found.get("bundle") or {}
    # A fast-mode variant's id is its bundle's id plus FAST_SUFFIX; its bundle record carries the base id and fast=True.
    base = choice[:-len(FAST_SUFFIX)] if isinstance(choice, str) and choice.endswith(FAST_SUFFIX) else choice
    if bundle.get("model") and bundle.get("bundle_id", base) == base:
        return candidate_label({**bundle, "fast": bundle.get("fast") is True or base != choice})
    return str(choice or "—")


def unique_labels(rows, account_label=None):
    """Short labels for candidate rows; a duplicate adds its account, then falls back to the raw id."""
    labels = [candidate_label(r) for r in rows]
    if account_label is not None:
        labels = [f"{label} · {account_label(r.get('account'))}" if labels.count(label) > 1 and r.get("account")
                  else label for label, r in zip(labels, rows)]
    return [label if labels.count(label) == 1 else str(r.get("id") or label) for label, r in zip(labels, rows)]
