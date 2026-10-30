"""Short display names for models, bundles and work kinds; unknown ids fall back to themselves.

Visible text uses these labels; raw ids stay in tooltips, plain-text mirrors and details.
"""
from __future__ import annotations

import re

import i18n

MODEL_LABELS = {"claude-opus-5-5": "Opus 5.5", "claude-sonnet-5-5": "Sonnet 5.5", "claude-fable-5-1": "Fable 5.1",
                "gpt-5.6-sol": "Sol 5.6", "grok-bot": "Grok Bot",
                "gpt-6-astra": "Astra 6", "gpt-6.1-sol": "Sol 6.1", "gpt-6-pro": "GPT 6 Pro",
                "gemini-4-argon-high": "Argon 4", "gemini-3.8-flash-high": "Flash 3.8", "grok-4.7": "Grok 4.7",
                "dot": "Dot", "deepseek-v4.1-flash-free": "DeepSeek Flash"}
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
EFFORTS = {"low": "低", "medium": "中", "high": "高", "xhigh": "极高", "max": "最高", "ultra": "超高"}


def _tr(text):
    return i18n.translate(text)


def model_label(model: str) -> str:
    if model in MODEL_LABELS:
        return MODEL_LABELS[model]
    claude = re.fullmatch(r"claude-(opus|sonnet|haiku|fable)-(\d+(?:-\d+)*)", model)
    if claude:
        return f"{claude[1].title()} {claude[2].replace('-', '.')}"
    gpt = re.fullmatch(r"gpt-(\d+(?:\.\d+)*?)-(sol|astra|terra|luna|pro)", model)
    if gpt:
        return f"{gpt[2].title()} {gpt[1]}"
    return model


def kind_label(value):
    return _tr(WORK_KINDS.get(value, value or "—"))


def difficulty_label(value):
    return _tr(DIFFICULTIES.get(value, value or "—"))


def effort_text(effort):
    return _tr(EFFORTS.get(effort, effort)) if effort else ""


def candidate_label(row):
    """Opus 5.5 · High for a candidate row or bundle dict; the raw id when no model is recorded."""
    row = row or {}
    model = row.get("model")
    if not model:
        return str(row.get("id") or row.get("bundle_id") or "—")
    effort = effort_text(row.get("effort"))
    return model_label(str(model)) + (" · " + effort if effort else "")


def choice_label(row):
    """The winner of a ledger plan row, from its bundle or its snapshot ranking."""
    choice = row.get("choice")
    bundle = row.get("bundle") if isinstance(row.get("bundle"), dict) else {}
    if not bundle.get("model"):
        snapshot = row.get("decision_snapshot") or row.get("snapshot") or row
        ranking = snapshot.get("ranking") if isinstance(snapshot, dict) else None
        found = next((c for c in ranking or [] if isinstance(c, dict) and c.get("id") == choice), {})
        bundle = found.get("bundle") or {}
    if bundle.get("model") and bundle.get("bundle_id", choice) == choice:
        return candidate_label(bundle)
    return str(choice or "—")


def unique_labels(rows, account_label=None):
    """Short labels for candidate rows; a duplicate adds its account, then falls back to the raw id."""
    labels = [candidate_label(r) for r in rows]
    if account_label is not None:
        labels = [f"{label} · {account_label(r.get('account'))}" if labels.count(label) > 1 and r.get("account")
                  else label for label, r in zip(labels, rows)]
    return [label if labels.count(label) == 1 else str(r.get("id") or label) for label, r in zip(labels, rows)]
