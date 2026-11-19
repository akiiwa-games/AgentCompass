"""Short display names for models, bundles and work kinds; unknown ids fall back to themselves.

Visible text uses these labels; raw ids stay in tooltips, plain-text mirrors and details.
"""
from __future__ import annotations

import i18n
import router_priority

_CATALOG_LABELS_CACHE: tuple[str, dict[str, str]] | None = None

WORK_KINDS = {
    "creative_ideation": "创意构想",
    "architecture_reasoning": "架构推演",
    "code_implementation": "代码实现",
    "frontend_ui": "前端界面",
    "code_audit": "代码审查",
    "exploration_forensics": "探索取证",
    "web_research": "网络调研",
    "visual_analysis": "视觉分析",
    "text_processing": "文本处理",
    "live_loop": "实时循环",
    "gui_control": "界面操控",
}
DIFFICULTIES = {"routine": "常规", "hard": "困难", "coupled": "高耦合", "frontier": "前沿", "extreme": "极难", "easy": "简单", "medium": "中等"}
FAST_SUFFIX = "+fast"      # fast_modes.ID_SUFFIX: the router's id for a bundle's fast-mode twin
# A label for every effort of the order (user_scores.EFFORTS) and every effort name off it a launch can carry (five-axis
# `effort_readings`): test_display_names states the rule.
EFFORTS = {"none": "不推理", "minimal": "最低", "low": "低", "medium": "中", "high": "高", "xhigh": "极高", "max": "最高", "ultra": "超高",
           "default": "默认"}


def _tr(text):
    return i18n.translate(text)


def catalog_labels() -> dict[str, str]:
    """{model_key: label} from the catalog (`accounts.json` `models[key].label` and `same_model` aliases) of the reference
    set the tray holds (public_data.document), derived once per set."""
    global _CATALOG_LABELS_CACHE
    try:
        router_priority.modules()
        import account_fleet
        import public_data
        reference = account_fleet.REFERENCES / "accounts.json"
        stamp = public_data.set_id(reference.parent)
        if _CATALOG_LABELS_CACHE is not None and _CATALOG_LABELS_CACHE[0] == stamp:
            return _CATALOG_LABELS_CACHE[1]
        doc = public_data.document(reference)
    except (OSError, ValueError, ImportError):
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
