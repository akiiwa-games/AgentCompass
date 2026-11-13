"""Data for the main panel: router decisions, quota, workers, policy and settings. No tkinter here.

Normal page reads use the router's modules and local files. Explicit background model refreshes
query configured providers through model_catalog; they never launch work or create routing bundles.
"""
from __future__ import annotations

from i18n import translate as _tr, LocalizedSequence

import codecs
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import plugins
import radar_chart
import router_priority
import i18n
import display_names
from display_names import model_label  # noqa: F401

STATE_DIR = Path(os.environ.get("ROUTER_STATE_DIR") or Path.home() / ".agent-compass").expanduser()


def get_ui_language() -> str:
    return i18n.get_language()


def save_ui_language(lang: str) -> str:
    return i18n.set_language(lang)


def strategy_preset_key(name: str) -> str:
    """Accept either display language without changing the persisted Chinese preset keys."""
    return next((key for key in STRATEGY_PRESETS if name in (key, i18n.english(key))), name)


# Cards are ordered and named by product family; account names come only from config and discovery.
FAMILY_ORDER = ("claude", "codex", "antigravity", "opencode", "dot", "chatgpt-web", "pi", "grok", "grok-bot", "omp", "dsh")
FAMILY_LABELS = {"claude": "Claude", "codex": "Codex", "antigravity": "Antigravity", "opencode": "OpenCode",
                 "dot": "Dot", "chatgpt-web": "ChatGPT Web", "pi": "Pi", "grok": "Grok", "grok-bot": "Grok Bot",
                 "api-proxy": "API Proxy", "llmproxy": "LLM Proxy", "omp": "Oh My Pi", "dsh": "DeepSeek Harness"}
# Families whose quota the usage collector reads.
USAGE_FAMILIES = ("claude", "codex", "pi", "antigravity", "grok")


def account_family(name: str, row: dict | None = None) -> str:
    family = (row or {}).get("family")
    if family:
        return family
    return next((f for f in FAMILY_LABELS if name == f or name.startswith(f + "-")), name)


def account_label(name: str | None, family: str | None = None) -> str:
    """A configured label wins; otherwise the family name plus the account suffix (claude-work -> Claude Work)."""
    if not name:
        return "—"
    if name in FAMILY_LABELS:
        return FAMILY_LABELS[name]
    family = family or account_family(name)
    if family in FAMILY_LABELS and name.startswith(family + "-"):
        return f"{FAMILY_LABELS[family]} {name[len(family) + 1:].replace('-', ' ').title()}"
    return str(name).replace("-", " ").title()


def card_order(names, inventory_rows: dict) -> list[str]:
    """Family order, the family's primary account first, then discovery/config order."""
    names = list(dict.fromkeys(names))
    rank = {family: index for index, family in enumerate(FAMILY_ORDER)}

    def key(item):
        index, name = item
        family = account_family(name, inventory_rows.get(name))
        return (rank.get(family, len(rank)), name != family, index)
    return [name for _, name in sorted(enumerate(names), key=key)]
PRESETS = (
    ("跨模块架构重构", "重构支付服务的插件架构与后台菜单，跨多个模块保持一致，需要全局严密的设计", 4.0, False, False),
    ("单模块缺陷修复", "修复 app/upload_queue.py 里上传队列停止时的竞态 bug 并补单测", 1.0, True, False),
    ("前端界面实现", "按设计稿实现 React 前端组件与样式，像素级还原", 2.0, False, True),
    ("开放式方案调研", "调研并提出三种创新的多代理调度方案，比较优劣并给出建议", 2.0, False, False),
    ("回测研究包", "运行因子回测，计算样本外夏普并整理研究报告", 3.0, False, False),
)
PRESETS = LocalizedSequence(PRESETS)
def _shipped_tuning_defaults() -> dict[str, int]:
    scripts = Path(__file__).resolve().parents[1] / "skills" / "agent-compass" / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import worker_sessions
    scoring = worker_sessions.shipped_scoring()
    return {
        "safety_margin_pct": int(scoring["safety_margin_pct"]),
        "short_window_floor_pct": int(scoring["short_window_floor_pct"]),
        "rotate_after_carried_tokens": worker_sessions.rotation_threshold(scoring),
    }


TUNING_DEFAULTS = _shipped_tuning_defaults()
STRATEGY_PRESETS = {
    "均衡": dict(TUNING_DEFAULTS),
    "省额度": {"safety_margin_pct": 12, "short_window_floor_pct": 20, "rotate_after_carried_tokens": 1500000},
    "重置前蹬完": {
        "safety_margin_pct": 3,
        "short_window_floor_pct": 8,
        "rotate_after_carried_tokens": TUNING_DEFAULTS["rotate_after_carried_tokens"],
    },
}
TUNING_LIMITS = {"safety_margin_pct": (0, 50), "short_window_floor_pct": (0, 50),
                 "rotate_after_carried_tokens": (100000, 10000000)}
BOOST_RANGE = (0.10, 0.50)
BOOST_HOURS = router_priority.DURATIONS
PANEL_INTENT = "panel:boost"


def modules():
    """(allocator, route_judge) from the installed agent-compass skill."""
    return router_priority.modules()


_ACCOUNTS_CACHE_LOCK = threading.Lock()
_ACCOUNTS_CACHE: tuple[tuple, float, dict] | None = None


def _invalidate_accounts_cache() -> None:
    global _ACCOUNTS_CACHE
    with _ACCOUNTS_CACHE_LOCK:
        _ACCOUNTS_CACHE = None


def _accounts_fingerprint() -> tuple | None:
    import account_fleet
    import host_policy
    if hasattr(getattr(account_fleet, "load_accounts", None), "_mock_name"):
        return None
    if getattr(account_fleet.load_accounts, "__module__", "") != "account_fleet":
        return None
    if hasattr(getattr(host_policy, "load_policy", None), "_mock_name"):
        return None
    try:
        import provider_inventory
        for fn_name in ("apply", "current", "discover"):
            fn = getattr(provider_inventory, fn_name, None)
            if hasattr(fn, "_mock_name") or getattr(fn, "__module__", "") != "provider_inventory":
                return None
    except Exception:
        return None
    refs = getattr(account_fleet, "REFERENCES", None)
    if not isinstance(refs, Path):
        return None
    paseo_home = Path(os.environ.get("PASEO_HOME") or Path.home() / ".paseo")
    files = (
        _public_accounts(refs),
        STATE_DIR / "accounts-override.json",
        STATE_DIR / "host-policy.json",
        STATE_DIR / "plugins.json",
        STATE_DIR / "model-catalog.json",
        STATE_DIR / "usage-cache.json",
        STATE_DIR / "usage-collector.json",
        STATE_DIR / "quota-exhaustion.json",
        STATE_DIR / "plan-inactive.json",
        STATE_DIR / "venice-usage.json",
        paseo_home / "config.json",
    )
    stamps = []
    for path in files:
        try:
            st = path.stat()
            stamps.append((str(path), st.st_mtime_ns, st.st_size))
        except OSError:
            stamps.append((str(path), None, None))
    return (str(STATE_DIR), os.environ.get("ROUTER_HOST_POLICY"), *stamps)


def _cached_accounts() -> dict:
    global _ACCOUNTS_CACHE
    modules()
    import account_fleet
    fp = _accounts_fingerprint()
    if fp is None:
        return account_fleet.load_accounts()
    now = time.monotonic()
    with _ACCOUNTS_CACHE_LOCK:
        cached = _ACCOUNTS_CACHE
        if cached is not None and cached[0] == fp and now - cached[1] < 2.0:
            return cached[2]
    accounts = account_fleet.load_accounts()
    fp_after = _accounts_fingerprint() or fp
    with _ACCOUNTS_CACHE_LOCK:
        _ACCOUNTS_CACHE = (fp_after, time.monotonic(), accounts)
    return accounts


def _load_capabilities(accounts: dict | None = None) -> dict:
    import account_fleet
    if accounts is not None:
        try:
            return account_fleet.load_capabilities(accounts=accounts)
        except TypeError:
            pass
    return account_fleet.load_capabilities()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------- decisions ----------

UNREPLAYABLE_NOTE = "旧记录缺字段，无法重放"


class UnreplayableProfile(ValueError):
    """A recorded profile the classifier's current answer cannot be rebuilt from: it is neither validated nor reclassified."""


def profile_invalid(profile) -> bool:
    """True when a profile fails the classifier's current schema (a missing or invalid field of any kind)."""
    modules()
    import task_intake
    try:
        task_intake.validate_profile(profile)
        return False
    except ValueError:
        return True


def unreplayable(row: dict) -> bool:
    from decision_theatre import snapshot_result
    return snapshot_result(row) is None and profile_invalid(row.get("profile"))


def recent_ledger_with_skipped(limit: int = 30, path: Path | None = None) -> tuple[list[dict], int]:
    """The latest valid plan decisions (newest first) and the count of skipped older-shape rows."""
    _, router = modules()
    import state_upgrade
    rows = router.read_ledger(path) if path else router.read_ledger()
    plans = [r for r in rows if isinstance(r, dict) and r.get("stage") == "plan" and r.get("choice")]
    valid = []
    skipped = 0
    for r in plans:
        if state_upgrade.row_has_old_profile(r) or unreplayable(r):
            skipped += 1
        else:
            valid.append(r)
    return list(reversed(valid[-limit:])), skipped


def recent_ledger(limit: int = 30, path: Path | None = None) -> list[dict]:
    """The latest plan decisions, newest first (skipping older-shape rows)."""
    return recent_ledger_with_skipped(limit, path)[0]


def ledger_summary(row: dict) -> dict:
    at = row.get("at") or ""
    try:
        local = datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        local = at[:16]
    objective = " ".join(str(row.get("objective") or row.get("task_id") or "").split())
    if objective.startswith("#"):
        objective = objective.lstrip("# ")
    stale = unreplayable(row)
    return {"at": local, "objective": (f"{_tr(UNREPLAYABLE_NOTE)} · {objective}" if stale else objective)[:48],
            "kind": row.get("kind") or "—", "choice": row.get("choice") or "—",
            "kind_label": display_names.kind_label(row.get("kind")), "choice_label": display_names.choice_label(row),
            "margin": row.get("margin"), "unreplayable": stale}


def build_task(objective: str, hours: float, local_only: bool = False, needs_visual: bool = False,
               work_kind: str | None = None, difficulty: str | None = None, profile: dict | None = None,
               local_only_reason: str | None = None) -> dict:
    modules()
    import task_intake
    needs = {}
    if needs_visual:
        needs["visual"] = True
    if local_only:
        needs["local_only"] = True
        needs["local_only_reason"] = local_only_reason or _tr("主面板干跑：模拟需要本机资源的任务")
    task = {"objective": objective or "", "expected_hours": float(hours), "needs": needs}
    if work_kind:
        task["work_kind"] = work_kind
    if difficulty:
        task["difficulty"] = difficulty
    if profile is not None:
        if profile_invalid(profile):
            raise UnreplayableProfile(_tr(UNREPLAYABLE_NOTE))
        task["profile"] = task_intake.validate_profile(profile)
    return task


def plan_task(task: dict) -> dict:
    """allocator.plan for a task, with the same Dot reachability rule as route_judge; never writes the ledger."""
    allocator, router = modules()
    import task_intake
    now = now_utc()
    usage = router.usage_snapshot(None, None, now)
    records = router.read_ledger()
    try:
        import dispatch_flow

        records = records + dispatch_flow.reservation_records(read_only=True)
    except Exception:
        pass
    accounts = _cached_accounts()
    capabilities = _load_capabilities(accounts)
    policy = accounts["scoring"]
    kind = task_intake.task_kind(task, capabilities, policy)
    unavailable = []
    if task_intake.dot_may_take(kind, task.get("needs") or {}, capabilities) and router.dot_unreachable():
        unavailable.append("dot_text")
    result = allocator.plan(task, usage, now, unavailable, records)
    import ledger_snapshot
    # Keep the original task title/profile available to the visual replay; the quota page reads the same block a ledger
    # snapshot records.
    return ledger_snapshot.with_quota_line({**result, "task": task})


def simulate(objective: str, hours: float, local_only: bool = False, needs_visual: bool = False,
             live_scorer: bool = True) -> dict:
    """Plan a typed task. Profiles it with the configured System-One scorer; when the classifier is unavailable,
    returns blocked / scorer_unavailable rather than guessing from keywords."""
    _, router = modules()
    import task_intake
    accounts = _cached_accounts()
    capabilities = _load_capabilities(accounts)
    policy = accounts["scoring"]
    task = build_task(objective, hours, local_only, needs_visual)
    profile, scorer = router.score_task(task, capabilities, use_model=bool(live_scorer))
    if profile is None:
        return {
            "stage": "plan",
            "status": "blocked",
            "choice": "none",
            "error": "scorer_unavailable",
            "ranking": [],
            "task": task,
            "scorer": scorer,
        }
    task["profile"] = profile
    if scorer.get("source") == "system_one":
        task["work_kind"] = task_intake.profile_kind(profile, policy)
        task["difficulty"] = task_intake.profile_difficulty(profile, policy)
    return {**plan_task(task), "scorer": scorer}


def replay(row: dict) -> dict:
    """Use an immutable candidate snapshot when present; a row without one replans from its recorded profile."""
    from decision_theatre import snapshot_result
    snapshot = snapshot_result(row)
    if snapshot is not None:
        if "quota_line" not in snapshot:
            try:
                modules()
                import ledger_snapshot
                snapshot = ledger_snapshot.with_quota_line(snapshot)
            except (router_priority.RouterMissing, ImportError):
                pass        # without the router the snapshot replays as recorded
        return with_usage_readings(snapshot, row.get("at"))
    profile = row.get("profile")
    if profile_invalid(profile):
        return {"choice": None, "ranking": [], "theatre_source": "unreplayable", "replay_note": _tr(UNREPLAYABLE_NOTE)}
    task = build_task(row.get("objective") or "", float(row.get("hours") or 1.0), bool(row.get("local_only")),
                      bool((profile or {}).get("needs_visual")), row.get("kind"), row.get("difficulty"),
                      profile, row.get("local_only_reason"))
    return {**plan_task(task), "theatre_source": "recomputed"}


_READINGS = {"stamp": None, "state": None}


def usage_readings_state():
    """The usage collector's recorded readings (capacity-state.json), read-only and cached until the file changes."""
    import capacity_stats
    path = capacity_stats.STATE_FILE
    stamp = (str(path), path.stat().st_mtime_ns, path.stat().st_size)
    if _READINGS["stamp"] != stamp:
        _READINGS.update(stamp=stamp, state=capacity_stats.load_state())
    return capacity_stats, _READINGS["state"]


def with_usage_readings(result: dict, decided_at) -> dict:
    """Display only: give a snapshot's quota-line windows the usage readings recorded inside their period.

    A window takes part when it names the collector's `account` and `window_id` and carries no `samples` of its
    own. The readings come from the usage collector; the router's economics never read them (they use the
    current period's consumption only). The snapshot itself is not changed: a copy is returned.
    """
    block = result.get("quota_line") if isinstance(result, dict) else None
    windows = block.get("windows") if isinstance(block, dict) else None
    try:
        at = datetime.fromisoformat(str(decided_at).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return result
    number = lambda value: isinstance(value, (int, float)) and not isinstance(value, bool)
    wanted = {key: window for key, window in (windows.items() if isinstance(windows, dict) else ())
              if isinstance(window, dict) and "samples" not in window and window.get("account") and window.get("window_id")
              and all(number(window.get(field)) for field in ("reset_in_hours", "elapsed_hours", "capacity_usd"))}
    if not wanted:
        return result
    try:
        import quota_line
        stats, state = usage_readings_state()
        filled = {}
        for key, window in wanted.items():
            series = stats.account_series(state, str(window["account"])).get(str(window["window_id"]))
            samples = quota_line.period_samples(series, at + window["reset_in_hours"] * 3600, at,
                                                window["elapsed_hours"], window["capacity_usd"])
            if samples:
                filled[key] = {**window, "samples": samples}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return result
    return {**result, "quota_line": {**block, "windows": {**windows, **filled}}} if filled else result


def candidate_rows(result: dict) -> list[dict]:
    """Score breakdown for every candidate: eligible ones ranked first, then the hard-gate exclusions."""
    rows = []
    for c in result.get("ranking") or []:
        comp = c.get("components") or {}
        bundle = c.get("bundle") or {}
        rows.append({
            "id": c.get("id"), "model": bundle.get("model"), "account": bundle.get("account"),
            "effort": bundle.get("effort"), "fast": bool(bundle.get("fast")), "score": c.get("score"), "fit": comp.get("fit"),
            "opportunity_cost": comp.get("opportunity_cost"), "boost": comp.get("boost") or 0,
            "excluded": list(c.get("excluded") or []), "components": comp,
            "headroom": c.get("five_axis_headroom") or {}})
    return rows


def decision_pair(result: dict, first: str | None = None, second: str | None = None) -> list[dict]:
    """Winner and runner-up (best candidate of another model) unless two ids are picked."""
    eligible = [r for r in candidate_rows(result) if not r["excluded"] and r["score"] is not None]
    by_id = {r["id"]: r for r in eligible}
    winner = by_id.get(first) or by_id.get(result.get("choice")) or (eligible[0] if eligible else None)
    if winner is None:
        return []
    rival = by_id.get(second) if second and second != winner["id"] else None
    if rival is None:
        rival = next((r for r in eligible if r is not winner and r["model"] != winner["model"]), None)
    return [r for r in (winner, rival) if r is not None]


def decision_series(pair: list[dict]) -> tuple[list[dict], list[dict]]:
    series = [{"label": f"{display_names.candidate_label(r)} · {r['score']:.3f}", "values": radar_chart.score_dimensions(r["components"]),
               "estimated": _estimated_axes(r)} for r in pair]
    raw = [radar_chart.score_raw(r["components"], r.get("headroom")) for r in pair]
    return series, raw


def _estimated_axes(candidate: dict) -> dict:
    """{axis: provenance} for a decision candidate's categories with type scores estimated through the tier."""
    note = "; ".join(n for n in candidate.get("notes") or [] if "estimated through the tier" in str(n))
    return {axis: note for axis in candidate.get("estimated_axes") or []}


def model_table() -> dict:
    """The router's model rows with their category scores (`five_axis`), tier and cited usd per hour (absent when unpriced)."""
    modules()
    import account_fleet
    import axis_scores
    accounts = axis_scores.five_axis_overlay(account_fleet.load_accounts(_public_accounts(account_fleet.REFERENCES)))
    return account_fleet.models_table(accounts)


def dna_series(models: dict, first: str, second: str | None) -> tuple[list[dict], list[dict]]:
    picked = [m for m in (first, second) if m and m in models]
    prices = [float(row["usd_per_hour"]) for row in models.values() if row.get("usd_per_hour") is not None]
    ceiling = max(prices, default=None)
    series = [{"label": model_label(m), "values": radar_chart.dna_dimensions(models[m], ceiling),
               "estimated": radar_chart.dna_estimated(models[m])} for m in picked]
    raw = [radar_chart.dna_raw(models[m]) for m in picked]
    return series, raw


def live_economy_windows() -> list[dict]:
    """Current live priced long-window economy numbers (`FiveAxisEconomy.report()` at 1h), read-only."""
    try:
        allocator, router = modules()
        now = now_utc()
        usage = router.usage_snapshot(None, None, now)
        records = router.read_ledger()
        try:
            import dispatch_flow
            records = records + dispatch_flow.reservation_records(read_only=True)
        except Exception:
            pass
        probe = {
            "objective": "economy overview",
            "expected_hours": 1.0,
            "needs": {},
            "profile": {
                "reasoning_depth": 2,
                "code_complexity": 1,
                "needs_visual": False,
                "needs_gui": False,
                "local_only": False,
                "wants_fast": False,
                "read_only_review": False,
                "access": "write",
                "expected_hours": 1.0,
                "hours_confidence": "medium",
                "urgency": "normal",
                "interaction_style": "engineering",
                "multi_task": False,
                "task_count": "1",
                "axis_demand": {"divergence": 0, "coding": 0, "aesthetics": 0, "tool_operation": 0},
            },
        }
        result = allocator.plan(probe, usage, now, records=records)
        windows = (((result.get("decision") or {}).get("system") or {}).get("quota_windows"))
        return [w for w in windows if isinstance(w, dict)] if isinstance(windows, list) else []
    except Exception:
        return []


def score_overview() -> dict:
    """The scoring page's data: the four categories (facets, boards, data dates, panel targets, coverage, continuity) and
    every model's category scores, read the way the router reads them (local manual scores included)."""
    modules()
    import account_fleet
    import axis_scores
    import score_overview as overview
    accounts = axis_scores.five_axis_overlay(account_fleet.load_accounts(_public_accounts(account_fleet.REFERENCES)))
    data = axis_scores.load_five_axis(axis_scores.five_axis_config(accounts.get("scoring")).get("path"))
    models = account_fleet.models_table(accounts)
    meta = accounts.get("_five_axis") or {}
    targets = meta.get("targets") or {}
    return {"generated_at": meta.get("generated_at"), "anchor": meta.get("anchor"), "panel_n": meta.get("panel_n"),
            "demand_grid_n": overview.demand_grid_n(), "economy_windows": live_economy_windows(),
            "targets": targets, "categories": overview.category_overview(data, models),
            "models": overview.model_rows(models, targets, model_label)}


def set_manual_score(model: str, target: str, level: float) -> str:
    """A local manual score, through the same writer as `agentcompass score set` (never the public file)."""
    modules()
    import user_scores
    result = user_scores.set_score(model, target, level)
    return _tr("已为 {0} 的 {1} 写入本机评分 {2}").format(result["model"], result["target"], result["entry"]["level"])


def clear_manual_score(model: str, target: str) -> str:
    """Clear a local manual score, through the same writer as `agentcompass score clear` (never the public file)."""
    modules()
    import user_scores
    result = user_scores.clear_score(model, target)
    if "." not in target and result["removed"] == 0:
        local = user_scores.list_scores(model, scope="local").get("local") or {}
        remaining = sorted({k for entries in local.values() if isinstance(entries, dict)
                            for k in entries if k.startswith(f"{target}.")})
        if remaining:
            return _tr("未设置整个类别的本机评分（已清除 0 条）；子维度本机评分仍保留：{0}").format(", ".join(remaining))
    return _tr("已清除 {0} 条本机评分").format(result["removed"])


# ---------- quota and workers ----------

def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def account_badge(name: str, inventory_row: dict | None, plugin_config: dict, policy) -> tuple[str, str]:
    """(text, colour kind) of an account's status."""
    modules()
    import host_policy
    import state_upgrade
    row = inventory_row or {}
    if row.get("status") == "legacy_meter":
        return _tr(state_upgrade.LEGACY_METER_REASON), "warn"
    plugin_id = {"dot": "dot2api", "chatgpt-web": "chatgpt-web"}.get(name)
    plugin_entry = (plugin_config or {}).get(plugin_id) if plugin_id and isinstance(plugin_config, dict) else None
    if isinstance(plugin_entry, dict) and plugin_entry.get("enabled") is False:
        return _tr("集成已关闭"), "idle"
    if row.get("reason") == "plugin_disabled_in_agentcompass":
        if isinstance(plugin_entry, dict) and plugin_entry.get("enabled") is True:
            return _tr("已启用，等待刷新"), "idle"
        return _tr("集成已关闭"), "idle"
    if not host_policy.account_allowed(policy, name):
        if row.get('billing') in ('metered', 'unknown'):
            return _tr('仅手动使用'), 'idle'
        return _tr("本机策略禁用"), "idle"
    status = row.get("status")
    if status == 'quota_exhausted':
        return _tr('额度已耗尽，等待刷新恢复'), 'warn'
    if status == 'plan_inactive':
        reason = str(row.get('reason') or status)
        retry_at = row.get('plan_inactive_retry_at')
        if retry_at and 'auto-retry at' not in reason:
            reason = f'{reason} (auto-retry at {retry_at})'
        return _tr('套餐失效，已排除派发：{0}').format(reason), 'warn'
    if status == 'auth_failed':
        return _tr('登录被拒绝，已排除派发：{0}').format(row.get('reason') or status), 'warn'
    if status == 'agy_account_flapping':
        return _tr('账号来回切换，已排除派发：{0}（关闭仍登录另一个 Google 账号的 agy 会话，再用目标账号登录一次）').format(row.get('reason') or status), 'warn'
    if status == 'usage_failing':
        last = read_time_text(row.get('last_success_at'))
        text = _tr('读数失败，已排除派发：{0}（上次成功 {1}）') if row.get('usage_excluded') else _tr('读数失败：{0}（上次成功 {1}）')
        return text.format(row.get('reason') or status, last), 'warn'
    if status == "ready":
        return _tr("就绪"), "ok"
    if status is None:
        return _tr("未发现"), "idle"
    return _tr('已暂停：{0}').format(f"{row.get('reason') or status}"), "warn"



def account_exclusion_recovery(row: dict | None) -> str | None:
    """Concrete next steps for account-level health exclusions."""
    status = (row or {}).get('status')
    if status == 'auth_failed':
        return _tr('请重新登录 CLI 并刷新额度')
    if status == 'plan_inactive':
        return _tr('请检查订阅状态并刷新额度')
    if status in ('usage_failing', 'quota_exhausted'):
        return _tr('请刷新额度并检查提供方状态')
    return None

def read_time_text(value) -> str:
    """A reading's time in local time; '从未' when there has been no good reading."""
    moment = _iso_datetime(value)
    return moment.astimezone().strftime('%m-%d %H:%M') if moment else _tr('从未')


def metered_spent_text(usage_row: dict) -> str:
    """Spent-first dollar label for a metered USD reading. Empty for any other account."""
    if not isinstance(usage_row, dict) or usage_row.get("backend_kind") != "metered_usd":
        return ""
    details = usage_row.get("usage_details") if isinstance(usage_row.get("usage_details"), dict) else {}
    if details.get("query") == "no_permission":
        return ""
    spent = details.get("spent_usd")
    if isinstance(spent, (int, float)) and not isinstance(spent, bool):
        import usage_core
        spent_text = usage_core.format_usd_amount(float(spent))
        budget = details.get("budget_usd")
        if isinstance(budget, (int, float)) and not isinstance(budget, bool) and budget > 0:
            return _tr("已用 {0} / {1}").format(spent_text, usage_core.format_usd_amount(float(budget)))
        return _tr("已用 {0}").format(spent_text)
    summary = usage_row.get("summary")
    return summary if isinstance(summary, str) else ""


def metered_usage_hover(usage_row: dict) -> str:
    """Hover for a metered USD miss. A spend number has no extra hover."""
    if not isinstance(usage_row, dict):
        return ""
    details = usage_row.get("usage_details") if isinstance(usage_row.get("usage_details"), dict) else {}
    if usage_row.get("backend_kind") != "metered_usd" and details.get("kind") != "metered_usd":
        return ""
    if details.get("query") == "no_permission":
        return _tr("无查询权限")
    if details.get("query") == "failed":
        reason = details.get("read_error")
        if isinstance(reason, str) and reason.strip():
            return reason.strip()
    return ""


def countdown(seconds) -> str:
    if not isinstance(seconds, (int, float)):
        return "—"
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return _tr('{0}天{1}小时').format(f'{days}', f'{hours}')
    if hours:
        return _tr('{0}小时{1}分').format(f'{hours}', f'{minutes:02d}')
    return _tr('{0}分').format(f'{minutes}')


def _iso_datetime(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else None


# An account this little spent gives nothing back when it resets, so its reset is not the pool's next refill.
POOL_REFILL_MIN_USED_PCT = 5.0


def pool_member_rows(members, now: datetime) -> list[dict]:
    """A Magpie pool's accounts as the hover reads them: per window what is left and when it resets.

    A reading taken before a reset that has since passed counts as refilled (left 100%), as the pooled bar does."""
    rows = []
    for member in members if isinstance(members, list) else ():
        if not isinstance(member, dict):
            continue
        read = _iso_datetime(member.get("read_at"))
        windows = {}
        for wid, window in (member.get("windows") or {}).items() if isinstance(member.get("windows"), dict) else ():
            if not isinstance(window, dict) or not isinstance(window.get("used_pct"), (int, float)):
                continue
            reset = _iso_datetime(window.get("resets_at"))
            refilled = bool(read and reset and read < reset <= now)
            used = 0.0 if refilled else max(0.0, min(100.0, float(window["used_pct"])))
            windows[wid] = {"used_pct": used, "refilled": refilled,
                            "reset_seconds": (reset - now).total_seconds() if reset and reset > now else None}
        rows.append({"label": member.get("label") or "—", "plan": member.get("plan"),
                     "counted": member.get("counted", True) is not False,
                     "age_seconds": (now - read).total_seconds() if read else None, "windows": windows})
    return rows


def pool_next_refill_seconds(rows: list[dict], wid: str) -> float | None:
    """Seconds until a pool window next gets quota back: the soonest reset of a counted account that spent some."""
    counted = [w for r in rows if r["counted"] and isinstance(w := r["windows"].get(wid), dict)]
    pending = [w["reset_seconds"] for w in counted
               if w["reset_seconds"] is not None and w["used_pct"] >= POOL_REFILL_MIN_USED_PCT]
    return min(pending) if len(counted) > 1 and pending else None


def signed_out(row: dict | None) -> bool:
    """A Claude/Codex slot whose login discovery can check but found none (installed CLI, template entry)."""
    if not isinstance(row, dict) or row.get("family") not in ("claude", "codex"):
        return False
    return not (row.get("identity") or row.get("credential_key"))


def account_cards() -> list[dict]:
    _, router = modules()
    import axis_scores
    import host_policy
    import quota_economy
    import quota_pools
    import provider_inventory

    now = now_utc()
    accounts = _cached_accounts()
    if callable(getattr(axis_scores, "five_axis_overlay", None)):
        try:
            overlaid = axis_scores.five_axis_overlay(accounts)
            if isinstance(overlaid, dict):
                accounts = overlaid
        except Exception:
            pass
    inventory = accounts.get("_discovery") if isinstance(accounts.get("_discovery"), dict) else provider_inventory.current()
    sources = provider_inventory.quota_sources(accounts)
    if getattr(router.usage_snapshot, "__module__", "") == "route_judge" and not hasattr(router.usage_snapshot, "_mock_name"):
        usage = router.usage_snapshot(None, None, now, quota_sources=sources, inventory=inventory)
    else:
        usage = router.usage_snapshot(None, None, now)
    capacity = _read_json(STATE_DIR / "capacity.json")
    observed = capacity.get("observed") or {}
    try:
        policy = host_policy.load_policy()
    except Exception:
        policy = None
    config = plugins.load()
    import venice_usage
    api_usage = _read_json(STATE_DIR / 'venice-usage.json')
    cards = []
    inv_accounts = inventory.get("accounts") or {}
    acc_table = accounts.get("accounts") or {}
    # An account drawing on another's quota (fleet `quota_sources`) shows as that pool's one card;
    # dispatch exclusions (`allowed_accounts`) mark the card as not dispatched rather than hiding its usage.
    members: dict[str, list[str]] = {}
    for member, source in sources.items():
        members.setdefault(source, []).append(member)

    def pool_allowed(name):
        return any(host_policy.account_allowed(policy, n) for n in (name, *members.get(name, ())))
    ordered = card_order([*inv_accounts.keys(), *acc_table.keys()], inv_accounts)
    names = [
        n for n in ordered
        if n in acc_table and n not in sources
        and not signed_out(inv_accounts.get(n)) and not (
            (inv_accounts.get(n) or {}).get("quota_account") not in (None, n)
            and not (acc_table.get(n) or {}).get("bundles")
        )
    ]
    reservations = pool_reservations(accounts=accounts, now=now)
    retry_holds = provider_retry_holds()
    live_doc = live_model_state(accounts)
    try:
        priority = router_priority.targets(accounts)
    except Exception:
        priority = []
    for name in names:
        spec = acc_table.get(name) or {}
        row = inv_accounts.get(name)
        usage_row = (usage.get("accounts") or {}).get(name) or {}
        metered_usd = usage_row.get("backend_kind") == "metered_usd" or spec.get("backend_kind") == "metered_usd"
        usage_details = usage_row.get("usage_details") if isinstance(usage_row.get("usage_details"), dict) else {}
        no_permission = metered_usd and usage_details.get("query") == "no_permission"
        if (
            isinstance(row, dict)
            and row.get("status") == "ready"
            and usage_row.get("available") is False
            and not no_permission
        ):
            row = {
                **row,
                "status": "usage_failing",
                "reason": usage_row.get("error") or row.get("reason") or _tr("额度接口暂不可用"),
                "last_success_at": row.get("last_success_at") or usage_row.get("fetched_at"),
            }
        # Quota visibility belongs to the pool; mark blocked only when every pool member is excluded from dispatch.
        text, kind = account_badge(name, row, config, None if pool_allowed(name) else policy)
        held = retry_hold_text(retry_holds, name)
        if held and kind != "idle":   # an idle badge already says why the account takes no work
            text, kind = (held if kind == "ok" else f"{text}；{held}"), "warn"
        else:
            held = None
        live_note = live_model_hold_text(live_doc, name, spec)
        if live_note and kind != "idle":
            text, kind = (live_note if kind == "ok" else f"{text}；{live_note}"), "warn"
        status = (row or {}).get("status")
        reading_failed = status in ("usage_failing", "plan_inactive", "auth_failed") or bool((row or {}).get("usage_read_failed"))
        windows = []
        latched = set((row or {}).get("exhausted_windows") or ())  # Only these windows are latched out.
        member_rows = pool_member_rows(usage_row.get("pool_members"), now)
        spent_text = "" if no_permission else (metered_spent_text(usage_row) if metered_usd else "")
        usage_hover = metered_usage_hover(usage_row) if metered_usd else ""
        for window in usage_row.get("windows") or []:
            wid = window.get("id")
            c_wid = quota_pools.canonical_window_id(spec, wid)
            hours = ((spec.get("windows") or {}).get(c_wid) or {}).get("hours")
            reset_passed = window.get("reset_time_passed") is True
            remaining = window.get("remaining_pct_before_reset")
            if reset_passed:
                # The reading predates the reset: the window refilled, pending a new reading.
                remaining = None  # A passed reset is not a fresh quota observation.
            elif remaining is None:
                remaining = window.get("observed_remaining_pct")
            if status == 'quota_exhausted' or wid in latched or c_wid in latched:
                remaining = 0.0  # Explicit upstream exhaustion outranks an older positive cache.
            cap = quota_economy.capacity_usd(spec, c_wid) if spec else None
            acct_obs = observed.get(name) or {}
            rates = acct_obs.get(wid) or acct_obs.get(c_wid) or {}
            res = reservations.get((name, wid)) or reservations.get((name, c_wid)) or {}
            # A pool counts down to its next partial refill; each account's own reset is on the hover.
            refill = None if reset_passed else pool_next_refill_seconds(member_rows, wid)
            windows.append({
                "id": wid, "hours": None if metered_usd else hours, "remaining_pct": remaining,
                "used_pct": window.get("used_pct"),
                "accounts": window.get("accounts") or 1,
                "remaining_usd": None if metered_usd else (round(cap * remaining / 100, 1) if cap and remaining is not None else None),
                "reserved_pct": res.get("pct", 0.0), "reserved_usd": res.get("usd", 0.0),
                "reserved_workers": list(res.get("workers") or ()),
                "rate_3h": rates.get("pct_per_hour_3h"), "rate_24h": rates.get("pct_per_hour_24h"),
                "reset_in": "" if metered_usd else (_tr("已重置") if reset_passed else countdown(
                    refill if refill is not None else window.get("seconds_until_reset"))),
                **({"metered_usd": True, "spent_text": spent_text} if metered_usd else {}),
                "stale": reset_passed or usage_row.get("status") != "fresh" or reading_failed})
        bundle_pills = []
        for bid, bspec in (spec.get("bundles") or {}).items():
            b_prov = bspec.get("provider")
            if b_prov == "pi" and name != "pi":
                continue
            if b_prov and b_prov not in (name, "antigravity-resilient") and bspec.get("discovery_unavailable"):
                continue
            m_label = model_label(bspec.get("model") or bid)
            eff = bspec.get("effort")
            b_label = f"{m_label}·{eff}" if eff and eff not in ("high", "default") else m_label
            bundle_pills.append({"id": bid, "label": b_label, "enabled": not bool(bspec.get("disabled"))})
        fetched_at = usage_row.get("fetched_at") or (row or {}).get("last_success_at")
        usage_status = (
            'quota_exhausted' if status == 'quota_exhausted'
            else 'stale' if reading_failed and usage_row.get("status") == "fresh"
            else usage_row.get("status")
        )
        free_unknown = bool(spec.get("capacity_unknown") and spec.get("five_axis_free_pool"))
        plan_unknown_reason = (
            str(spec["plan_unknown_reason"]).strip()
            if isinstance(spec.get("plan_unknown_reason"), str) and spec["plan_unknown_reason"].strip()
            else str(spec["plan_unknown"]).strip()
            if not spec.get("plan_priced_as") and isinstance(spec.get("plan_unknown"), str) and spec["plan_unknown"].strip()
            else None
        )
        plan_mode_b = bool(spec.get("capacity_unknown") and plan_unknown_reason and not free_unknown)
        raw_local = spec.get("local_spend_usd")
        local_spend = (raw_local if isinstance(raw_local, dict)
                       else {"used_usd": float(raw_local), "tokens": int(spec.get("local_spend_tokens") or 0),
                             "source": spec.get("local_spend_source") or "opencode.db"}
                       if isinstance(raw_local, (int, float)) and not isinstance(raw_local, bool) else None)
        if free_unknown and kind == "ok":
            text, kind = _tr("额度上限未知（不参与自动路由）"), "warn"
        elif plan_mode_b and kind != "idle":
            mode_b_text = _tr("套餐未知（Mode B），不参与自动路由：{0}").format(plan_unknown_reason)
            text, kind = (mode_b_text if kind == "ok" else f"{text}；{mode_b_text}"), "warn"
        free_note = (
            _tr("额度上限未知（capacity_unknown），不参与自动路由；本地累计消耗下界 U ≥ ${0:.2f}（{1:,} tokens，{2}）").format(
                float(local_spend["used_usd"]), int(local_spend.get("tokens") or 0), local_spend.get("source") or "opencode.db")
            if free_unknown and local_spend and local_spend.get("used_usd") is not None else
            _tr("额度上限未知（capacity_unknown），不参与自动路由")
            if free_unknown else
            _tr("套餐未知（capacity_unknown：{0}），不参与自动路由").format(plan_unknown_reason)
            if plan_mode_b else None
        )
        cards.append({"account": name, "label": spec.get("label") or account_label(name, account_family(name, row)),
                      "badge": text, "kind": kind,
                      "status": status, "status_reason": (row or {}).get("reason"), "retry_hold": bool(held),
                      "retry_clear_pool": retry_clear_pool(retry_holds, name) if held else None,
                      "last_success_at": (row or {}).get("last_success_at"),
                      "fetched_at": fetched_at,
                      "plan": (row or {}).get("plan") or spec.get("plan"),
                      "usage_status": usage_status,
                      "usage_age_seconds": usage_row.get("observation_age_seconds"),
                      "pool_members": member_rows,
                      **({"capacity_unknown": True} if spec.get("capacity_unknown") else {}),
                      **({"plan_unknown_reason": plan_unknown_reason} if plan_mode_b else {}),
                      **({"local_spend_usd": local_spend} if local_spend else {}),
                      "usage_note": venice_usage.note(api_usage) if name in ('omp', 'dsh') else
                          free_note if free_note is not None else
                          None if no_permission else
                          spent_text if metered_usd and not windows else
                          _tr("API 余额未提供，非无限额度") if spec.get('billing') == 'metered' and not windows else None,
                      "usage_hover": usage_hover,
                      "windows": windows, "bundles": bundle_pills,
                      "host_enabled": bool(pool_allowed(name)),
                      "verified": bool(spec.get("verified", True)),
                      "refreshable": name in ('omp', 'dsh') or bool(usage_providers([name], accounts)),
                      "priority_targets": [(t["account"], t["model"], t["label"]) for t in priority if t["account"] == name]})
    return cards


def pool_reservations(db: Path | None = None, accounts: dict | None = None,
                      now: datetime | None = None) -> dict[tuple[str, str], dict]:
    """Read-only in-flight quota reservations per (account, window_id) from active dispatch leases."""
    modules()
    import quota_economy
    import quota_pools
    now = now or now_utc()
    accounts = accounts or _cached_accounts()
    scoring = accounts.get("scoring") or {}
    acc_table = accounts.get("accounts") or {}
    rows = _dispatch_rows(db or STATE_DIR / "dispatch.sqlite3")
    records = []
    seen = set()
    for r in rows:
        did = r.get("dispatch_id")
        if not did or did in seen or r.get("state") not in LIVE_DISPATCH:
            continue
        expiry = quota_pools.parse_time(r.get("lease_until"))
        if expiry is None or expiry <= now:
            continue
        seen.add(did)
        bundle = dict(r.get("bundle") or {})
        if not bundle.get("bundle_id") and bundle.get("id"):
            bundle["bundle_id"] = bundle["id"]
        records.append({
            "stage": "dispatch",
            "dispatch_id": did,
            "state": r.get("state"),
            "at": r.get("created_at"),
            "lease_until": r.get("lease_until"),
            "choice": bundle.get("bundle_id") or bundle.get("id"),
            "bundle": bundle,
            "expected_usd": r.get("expected_usd"),
            "agent_id": r.get("agent_id"),
            "title": (r.get("request") or {}).get("title") or r.get("task_id") or "",
        })
    result: dict[tuple[str, str], dict] = {}
    if not records:
        return result
    bundle_pools_fn = getattr(quota_pools, "bundle_pools", None)
    for account, spec in acc_table.items():
        if not isinstance(spec, dict):
            continue
        for wid, wspec in (spec.get("windows") or {}).items():
            if not isinstance(wspec, dict):
                continue
            c_wid = quota_pools.canonical_window_id(spec, wid)
            cap_usd = quota_economy.capacity_usd(spec, c_wid)
            if not cap_usd or cap_usd <= 0:
                continue
            pool_workers = []
            total_usd = 0.0
            for rec in records:
                bundle = rec["bundle"]
                if ('grok' if bundle.get('account') == 'grok-bot' else bundle.get('account')) != ('grok' if account == 'grok-bot' else account):
                    continue
                usd = 0.0
                if callable(bundle_pools_fn):
                    try:
                        pools = bundle_pools_fn(dict(bundle, account=account), accounts)
                    except Exception:
                        usd = quota_pools.inflight_reserved_usd(
                            account, spec, c_wid, wspec, {}, cap_usd, [rec], accounts, scoring, now)
                    else:
                        amt = rec.get("expected_usd")
                        if (wid in pools or c_wid in pools) and isinstance(amt, (int, float)) and not isinstance(amt, bool) and amt > 0:
                            usd = float(amt)
                else:
                    usd = quota_pools.inflight_reserved_usd(
                        account, spec, c_wid, wspec, {}, cap_usd, [rec], accounts, scoring, now)
                if usd > 0:
                    total_usd += usd
                    pct = round(100.0 * usd / cap_usd, 2)
                    short_id = (rec.get("agent_id") or rec.get("dispatch_id") or "")[:8]
                    b_name = bundle.get("bundle_id") or bundle.get("id") or model_label(bundle.get("model") or "—")
                    pool_workers.append({
                        "dispatch_id": rec["dispatch_id"],
                        "agent_id": short_id,
                        "bundle": b_name,
                        "usd": round(usd, 2),
                        "pct": pct,
                        "title": rec.get("title") or "",
                    })
            if total_usd > 0:
                entry = {
                    "pct": round(100.0 * total_usd / cap_usd, 2),
                    "usd": round(total_usd, 2),
                    "workers": pool_workers,
                }
                result[(account, wid)] = entry
                result[(account, c_wid)] = entry
    return result


def usage_providers(names, accounts: dict | None = None) -> list[str]:
    """Collector provider IDs for these accounts: a pooled account reads its source, by discovered family."""
    modules()   # installs the skill scripts path before provider_inventory is imported
    import host_policy
    import provider_inventory
    if accounts is None:
        accounts = _cached_accounts()
    rows = ((accounts.get("_discovery") or {}).get("accounts") or {})
    sources = provider_inventory.quota_sources(accounts)
    result = []
    for name in names:
        source = sources.get(name, name)
        row = rows.get(source) or {}
        if row.get("family") in USAGE_FAMILIES or row.get("backend_kind") == "metered_usd":
            result.append(source)
    result = list(dict.fromkeys(result))
    try:
        policy = host_policy.load_policy()
    except Exception:
        policy = None
    if isinstance(policy, dict) and "usage_providers" in policy:
        allowed = {sources.get(n, n) for n in host_policy.usage_providers(policy, [*names, *result])}
        result = [p for p in result if p in allowed]
    return result


def collect_usage_now(providers: list[str]) -> None:
    """Read these accounts now with the router's own collector (usage_core); Usage Float is not involved."""
    modules()   # installs the skill scripts path
    import usage_core
    usage_core.fetch_all(providers, force=True)


def refresh_usage(accounts: list[str] | None = None, timeout: float = 130) -> str:
    """Re-read these accounts directly and update the usage cache (~/.agent-compass/usage-cache.json)."""
    if accounts is None:
        accounts = [c["account"] for c in account_cards() if c.get("refreshable")]
    providers = usage_providers(accounts)
    api_requested = any(name in ('omp', 'dsh') for name in accounts)
    if api_requested:
        import venice_usage
        api_result = venice_usage.refresh(STATE_DIR)
    if not providers:
        if api_requested:
            return _tr('已刷新 Venice API 余额') if (api_result or {}).get('available') else venice_usage.note(api_result or {})
        return _tr("该账号没有可刷新的额度来源")
    try:
        collect_usage_now(providers)
    except Exception as exc:
        return _tr('刷新额度失败：{0}').format(f'{exc}')
    names = "、".join(account_label(a) for a in providers)
    return _tr('已刷新额度：{0}').format(f'{names}')


LIVE_DISPATCH = ("prepared", "launching", "launched", "started")
RECENT_WORKER_SECONDS = 6 * 3600
AGENT_SCAN_SECONDS = 3 * 86400
_WORKER_SESSION_CACHE: dict[str, tuple[tuple, float, dict]] = {}
_DISPATCH_ROWS_CACHE: dict[str, tuple[tuple, list[dict]]] = {}


def _dispatch_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    key = str(path)
    stamp = None
    try:
        st = path.stat()
        wal = path.with_name(path.name + "-wal")
        wal_st = wal.stat() if wal.is_file() else None
        stamp = (st.st_mtime_ns, st.st_size, wal_st.st_mtime_ns if wal_st else 0, wal_st.st_size if wal_st else 0)
    except OSError:
        stamp = None
    if stamp is not None:
        cached = _DISPATCH_ROWS_CACHE.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1]
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = [json.loads(data) for (data,) in connection.execute("SELECT data FROM dispatches")]
    except (sqlite3.Error, ValueError):
        return []
    finally:
        connection.close()
    if stamp is not None:
        _DISPATCH_ROWS_CACHE[key] = (stamp, rows)
    return rows


def _paseo_agents(agents_dir: Path) -> list[dict]:
    """Unarchived Paseo agent records touched in the last three days (older files are skipped by mtime)."""
    cutoff = time.time() - AGENT_SCAN_SECONDS
    docs = []
    try:
        with os.scandir(agents_dir) as top_entries:
            ws_dirs = [entry.path for entry in top_entries if entry.is_dir()]
    except OSError:
        return []
    for ws_dir in ws_dirs:
        try:
            with os.scandir(ws_dir) as entries:
                for entry in entries:
                    if not entry.name.endswith(".json") or not entry.is_file():
                        continue
                    if entry.stat().st_mtime < cutoff:
                        continue
                    doc = _read_json(Path(entry.path))
                    if doc.get("id") and not doc.get("archivedAt"):
                        docs.append(doc)
        except OSError:
            continue
    return docs


def rotation_limit(accounts: dict | None = None) -> int:
    """Configured historical-context rotation threshold (`scoring.rotate_after_carried_tokens`)."""
    modules()
    import account_fleet
    import worker_sessions
    if accounts is None:
        if (hasattr(getattr(account_fleet, "load_accounts", None), "_mock_name")
                or getattr(account_fleet.load_accounts, "__module__", "") != "account_fleet"):
            accounts = account_fleet.load_accounts()
        else:
            accounts = read_tuning()
    return worker_sessions.rotation_threshold(accounts)


def workers(db: Path | None = None, paseo_home: Path | None = None) -> list[dict]:
    """Live workers with their historical (uncompressed) context against the configured rotation limit.

    A dispatch row stays 'completed' after its first turn even when the worker is resumed, so the live Paseo agent
    records are read too: running / initializing agents, and dispatched workers active in the last six hours."""
    modules()
    import quota_pools
    import worker_sessions
    rows = _dispatch_rows(db or STATE_DIR / "dispatch.sqlite3")
    accounts = _cached_accounts()
    limit = rotation_limit(accounts)
    now = now_utc()
    by_agent = {row["agent_id"]: row for row in rows if row.get("agent_id")}
    live = {}
    for row in rows:
        if row.get("state") not in LIVE_DISPATCH:
            continue
        hard = quota_pools.parse_time(row.get("hard_until"))
        if hard is not None and hard <= now:
            continue
        live[row.get("agent_id") or row.get("dispatch_id") or id(row)] = (row, {})
    if db is None or paseo_home is not None:
        home = paseo_home or Path(os.environ.get("PASEO_HOME") or Path.home() / ".paseo")
        for doc in _paseo_agents(home / "agents"):
            agent, status = doc["id"], doc.get("lastStatus")
            dispatched = (doc.get("labels") or {}).get("router.dispatch_id") or agent in by_agent
            seen = quota_pools.parse_time(doc.get("lastActivityAt") or doc.get("updatedAt"))
            recent = seen is not None and (now - seen).total_seconds() <= RECENT_WORKER_SECONDS
            if status in ("running", "initializing") or (dispatched and recent and status != "closed"):
                live[agent] = (live.get(agent, (by_agent.get(agent) or {}, None))[0], doc)
    result = []
    for key, (row, doc) in live.items():
        agent = row.get("agent_id") or doc.get("id")
        status, dispatch_state = doc.get("lastStatus"), row.get("state")
        if status == "running" or dispatch_state == "started":
            state_text = _tr("运行中")
        elif status == "initializing" or dispatch_state in ("prepared", "launching", "launched"):
            state_text = _tr("启动中")
        else:
            state_text = _tr("近期待命")
        labels = doc.get("labels") or {}
        bundle = row.get("bundle") or {}
        model = bundle.get("model") or (doc.get("config") or {}).get("model") or (doc.get("runtimeInfo") or {}).get("model")
        owner = labels.get("router.account") or bundle.get("account") or doc.get("provider") or ""
        bundle_text = (labels.get("router.bundle_id") or bundle.get("bundle_id") or bundle.get("id")
                       or f"{account_label(owner)} · {model_label(model or '—')}")
        label = display_names.candidate_label({"model": bundle.get("model"), "effort": bundle.get("effort"), "id": bundle_text})
        title = (doc.get("title") or (row.get("request") or {}).get("title") or row.get("task_id") or "")[:60]
        if not agent:
            session = {}
        elif db is None and paseo_home is None:
            stamp = (doc.get("updatedAt"), doc.get("lastActivityAt"), status, dispatch_state)
            cached = _WORKER_SESSION_CACHE.get(agent)
            mono_now = time.monotonic()
            ttl = 2.0 if status in ("running", "initializing") or dispatch_state in LIVE_DISPATCH else 120.0
            if cached and cached[0] == stamp and mono_now - cached[1] < ttl:
                session = dict(cached[2])
            else:
                session = worker_sessions.session_state(agent)
                _WORKER_SESSION_CACHE[agent] = (stamp, mono_now, dict(session))
        else:
            session = worker_sessions.session_state(agent)
        result.append({"agent_id": agent, "title": title, "bundle": bundle_text, "label": label, "model": model, "state": state_text,
                       "started": doc.get("lastActivityAt") or doc.get("updatedAt") or row.get("started_at"),
                       "compacted_tokens": session.get("compacted_tokens"),
                       "context_tokens": session.get("context_tokens"),
                       "historical_tokens": worker_sessions.worker_historical_tokens(session), "limit": limit,
                       "telemetry": session.get("telemetry_status") or ("ok" if session else "unknown")})
    result.sort(key=lambda r: r["started"] or "", reverse=True)
    result.sort(key=lambda r: r["state"] == _tr("近期待命"))
    return result


# ---------- policy ----------

def overrides(now: datetime | None = None) -> list[dict]:
    _, router = modules()
    import quota_overrides
    now = now or now_utc()
    document = router_priority.read_document(quota_overrides.overrides_path())
    active_rows = [row for row in document["overrides"] if router_priority.active(row, now)]
    if not active_rows:
        return []
    accounts = _cached_accounts()
    usage = router.usage_snapshot(now=now)
    rows = []
    for row in active_rows:
        if quota_overrides.override_reset_landed(row, usage, accounts, now) or quota_overrides.override_depleted(row, usage, accounts):
            continue
        rows.append({"id": row["id"], "revision": router_priority.revision(row), "account": row.get("account"),
                     "model": row.get("model"), "boost": row.get("boost"),
                     "until": router_priority.stamp(quota_overrides.expected_reset_time(row) or row.get("until")),
                     "stop_on_reset": bool(row.get("stop_on_reset")), "intent": row.get("intent") or "",
                     **router_priority.countdown(row, now),
                     "label": f"{account_label(row.get('account'))}"
                              + (f" / {model_label(row['model'])}" if row.get("model") else "")})
    return rows


def boost_targets() -> list[tuple[str, str]]:
    """(account, model) pairs with an enabled bundle."""
    modules()
    import account_fleet
    accounts = _cached_accounts()
    seen = []
    for bundle in account_fleet.bundles(accounts).values():
        pair = (bundle["account"], bundle["model"])
        if not bundle.get("disabled") and pair not in seen:
            seen.append(pair)
    return seen


def add_boost(account: str, model: str | None, boost: float | None = None, hours: int = 6, stop_on_reset: bool = True,
              now: datetime | None = None) -> str:
    """A temporary priority rule in router-overrides.json; never stacks on an existing one for the same model."""
    if boost is not None and not BOOST_RANGE[0] - 1e-9 <= boost <= BOOST_RANGE[1] + 1e-9:
        raise ValueError(_tr('加成需在 +{0} ~ +{1} 之间').format(f'{BOOST_RANGE[0]:.2f}', f'{BOOST_RANGE[1]:.2f}'))
    if hours not in BOOST_HOURS:
        raise ValueError(_tr("预期重置时间只能是 1h / 3h / 6h / 12h / 24h"))
    _, router = modules()
    import account_fleet
    import override_store
    import quota_overrides
    import quota_pools
    now = now or now_utc()
    accounts = account_fleet.load_accounts()
    if account not in accounts["accounts"]:
        raise ValueError(_tr("未知账号"))
    model = account_fleet.canonical(model, accounts) if model else None
    path = quota_overrides.overrides_path()
    with override_store.locked(path):
        document = router_priority.read_document(path)
        new_until = (now + timedelta(hours=hours)).isoformat()
        overlapping = [
            r for r in document["overrides"]
            if (router_priority.overlaps(r, account, model, now, accounts) if model
                else (router_priority.active(r, now) and r.get("account") == account))
        ]
        if overlapping:
            ui_rules = [r for r in overlapping if r.get("intent") in (PANEL_INTENT, router_priority.INTENT)
                        and not r.get("expires_at")]
            if not ui_rules or any(r.get("until") == new_until and (account_fleet.canonical(r["model"], accounts) if r.get("model") else None) == model
                                   for r in ui_rules):
                return _tr("该账号/模型已有临时规则；未叠加权重")
            existing = ui_rules[0]
            existing["until"] = new_until
            existing["basis"] = _tr('用户设置 {0}/{1} 预计 {2} 小时后重置，越近消耗优先级越高').format(account, model or _tr('全部模型'), hours)
            if model:
                existing["model"] = model
            else:
                existing.pop("model", None)
            if boost is not None:
                existing["boost"] = round(float(boost), 2)
            document["overrides"] = [r for r in document["overrides"] if r is existing or r not in ui_rules]
            router_priority.save(path, {**document, "v": 1})
            return _tr('已更新 {0}{1} 预期重置，{2} 小时后重置前动态优先').format(
                account_label(account), (' / ' + model_label(model) if model else ''), hours)
        row = {"id": uuid.uuid4().hex, "intent": PANEL_INTENT, "account": account,
               "created_at": now.isoformat(), "until": new_until,
               "basis": _tr('用户设置 {0}/{1} 预计 {2} 小时后重置，越近消耗优先级越高').format(account, model or _tr('全部模型'), hours)}
        if boost is not None:
            row['boost'] = round(float(boost), 2)
        if model:
            row["model"] = model
        if stop_on_reset:
            windows = [(wid, spec) for wid, spec, _ in quota_pools.applicable_windows(accounts["accounts"][account], model, {})
                       if spec.get("hours", 0) >= 24]
            if not windows:
                raise ValueError(_tr("该账号没有可确认重置的周额度窗口；请取消“重置后自动撤销”"))
            wid = max(windows, key=lambda w: w[1]["hours"])[0]
            usage = router.usage_snapshot(now=now)
            base = router_priority.baseline(accounts, usage, {"account": account, "window": wid}, now)
            row.update(window=wid, stop_on_reset=True, reset_confirmation="replenishment", baseline_windows={wid: base})
        document["overrides"].append(row)
        router_priority.save(path, {**document, "v": 1})
    return _tr('已添加 {0}{1} 预期重置，{2} 小时后重置前动态优先').format(account_label(account), (' / ' + model_label(model) if model else ''), hours)


def host_policy_state() -> dict:
    """{'path', 'explicit', 'allowed': set or None (all), 'accounts': [names], 'error'}."""
    modules()
    import host_policy
    path, explicit = host_policy.policy_path()
    accounts = _cached_accounts()
    names = list(accounts["accounts"])
    try:
        policy = host_policy.load_policy()
        error = None
    except Exception as exc:
        policy, error = None, str(exc)
    names = list(dict.fromkeys([*names, *((policy or {}).get("known_accounts") or []),
                               *((policy or {}).get("allowed_accounts") or [])]))
    return {"path": path, "explicit": explicit, "allowed": None if policy is None else set(policy["allowed_accounts"]),
            "policy": policy, "accounts": names, "error": error}


def save_host_policy(allowed: list[str], all_accounts: list[str], path: Path | None = None,
                     existing: dict | None = None) -> str:
    """Write allowed_accounts atomically; every account allowed and nothing else set means no file (the default)."""
    modules()
    import host_policy
    import override_store
    target = path or host_policy.policy_path()[0]
    with override_store.locked(target):
        current = json.loads(target.read_text(encoding='utf-8-sig')) if target.exists() else (existing or {})
        if 'known_accounts' in current:
            # A known omission is a manual disable. Keep it even while the account is absent.
            if not allowed:
                raise ValueError(_tr('至少保留一个账号'))
            baseline = set((existing if existing is not None else current).get('allowed_accounts', []))
            requested = set(allowed)
            changed = [a for a in all_accounts if (a in baseline) != (a in requested)]
            # Apply only the user's changes to the latest policy. Merely displaying
            # an unready account is not a deny; a concurrent admission is not undone.
            selected = [a for a in current['allowed_accounts'] if a not in changed or a in requested]
            selected += [a for a in changed if a in requested]
            updated = {**current, 'allowed_accounts': list(dict.fromkeys(selected)),
                       'known_accounts': list(dict.fromkeys([*current.get('known_accounts', []), *changed]))}
            _backup(target, 'host-policy')
            override_store.write(target, updated)
            _invalidate_accounts_cache()
            return _tr('已保存本机准入：{0} 个账号').format(len(updated['allowed_accounts']))
        extra = {k: v for k, v in (existing or {}).items() if k in ("judges", "usage_providers")}
        if set(allowed) >= set(all_accounts) and not extra:
            if target.exists():
                _backup(target, "host-policy")
                target.unlink()
            _invalidate_accounts_cache()
            return _tr("已恢复默认：本机允许全部账号")
        if not allowed:
            raise ValueError(_tr("至少保留一个账号"))
        _backup(target, "host-policy")
        _atomic_json(target, {"allowed_accounts": [a for a in all_accounts if a in allowed], **extra})
        _invalidate_accounts_cache()
        return _tr('已保存本机准入：{0} 个账号').format(f'{len(allowed)}')


def _public_accounts(refs: Path) -> Path:
    """The public accounts.json routing reads: the fetched copy when it is newer than the shipped one."""
    import public_data
    shipped = refs / "accounts.json"
    return public_data.effective(shipped)


def _accounts_path() -> Path:
    modules()
    import account_fleet
    return _public_accounts(account_fleet.REFERENCES)


def _user_accounts_path(create: bool = False) -> Path:
    user_file = STATE_DIR / "accounts-override.json"
    if create:
        modules()  # Install the router scripts path before importing the shared transaction helper.
        import accounts_override
        import override_store
        with override_store.locked(user_file):
            if user_file.is_file():
                accounts_override.load_override(user_file)
            else:
                _atomic_json(user_file, {"schema_version": 2})
        return user_file
    if user_file.is_file():
        modules()
        import accounts_override
        accounts_override.load_override(user_file)
        return user_file
    return _accounts_path()


def read_tuning(path: Path | None = None) -> dict:
    document = json.loads((path or _accounts_path()).read_text(encoding="utf-8-sig"))
    if path is None:
        modules()
        import accounts_override
        override = accounts_override.load_override(STATE_DIR / "accounts-override.json")
        if override is not None:
            document = accounts_override.merge(document, override)
    scoring = document.get("scoring") or {}
    return {key: scoring.get(key) for key in TUNING_DEFAULTS}


def validate_tuning(values: dict) -> dict:
    clean = {}
    for key, value in values.items():
        if key not in TUNING_DEFAULTS:
            raise ValueError(_tr('未知参数 {0}').format(f'{key}'))
        low, high = TUNING_LIMITS[key]
        number = float(value)
        if not low <= number <= high:
            raise ValueError(_tr('{0} 需在 {1} ~ {2} 之间').format(f'{key}', f'{low}', f'{high}'))
        clean[key] = int(number) if isinstance(TUNING_DEFAULTS[key], int) else round(number, 4)
    return clean


def _object_span(text: str, key: str, start: int = 0, end: int | None = None) -> tuple[int, int]:
    """(open brace, close brace + 1) of the object value of the first "key": { ... } between start and end."""
    end = len(text) if end is None else end
    at = text.find(f'"{key}": {{', start, end)
    if at < 0:
        raise ValueError(_tr('accounts.json 中未找到 {0}').format(f'{key}'))
    opening = text.index("{", at)
    depth, index, quoted = 0, opening, False
    while index < end:
        char = text[index]
        if quoted:
            if char == "\\":
                index += 1
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return opening, index + 1
        index += 1
    raise ValueError(_tr('accounts.json 中 {0} 不完整').format(f'{key}'))


def _replace_number(text: str, key: str, value, start: int, end: int) -> str:
    """Replace the number of "key": <number> at the object's own level (depth 1) within [start, end)."""
    import re

    pattern = re.compile(r'"%s":\s*-?[0-9.eE+-]+' % re.escape(key))
    depth = 0
    for match in pattern.finditer(text, start, end):
        depth = text.count("{", start, match.start()) - text.count("}", start, match.start())
        if depth == 1:
            literal = json.dumps(value)
            return text[:match.start()] + f'"{key}": {literal}' + text[match.end():]
    raise ValueError(_tr('accounts.json 中未找到 {0}').format(f'{key}'))


def save_tuning(values: dict | str, path: Path | None = None) -> str:
    """Change only the named scoring thresholds in accounts-override.json (or path), in place, after a backup."""
    if isinstance(values, str):
        values = STRATEGY_PRESETS[strategy_preset_key(values)]
    if path is None:
        clean = validate_tuning(values)
        current = read_tuning()
        changed = [key for key, value in clean.items() if current.get(key) != value]
        if not changed:
            return _tr("参数未变化")
        def change(doc):
            scoring = doc.setdefault("scoring", {})
            for key in changed:
                scoring[key] = clean[key]
        _edit_preferences(change)
        return _tr('已保存 {0} 项路由参数（已备份原文件）').format(len(changed))
    target = path or _user_accounts_path(create=True)
    clean = validate_tuning(values)
    raw = target.read_bytes()
    bom = raw.startswith(codecs.BOM_UTF8)
    text = raw.decode("utf-8-sig")  # bytes, not text mode: keep the file's own line endings
    expected = json.loads(text)
    scoring = expected["scoring"]
    changed = [key for key, value in clean.items() if scoring.get(key) != value]
    if not changed:
        return _tr("参数未变化")
    for key in changed:
        start, end = _object_span(text, "scoring")
        scoring[key] = clean[key]
        text = _replace_number(text, key, clean[key], start, end)
    if json.loads(text) != expected:
        raise ValueError(_tr("accounts.json 定位修改后内容不符，未保存"))
    _backup(target, "accounts-json")
    modules()
    import accounts_override
    normalized = accounts_override.normalize(json.loads(text))
    if normalized != expected:
        _atomic_json(target, normalized)
    else:
        temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        temporary.write_bytes((codecs.BOM_UTF8 if bom else b"") + text.encode("utf-8"))
        temporary.replace(target)
    return _tr('已保存 {0} 项路由参数（已备份原文件）').format(f'{len(changed)}')


def restore_tuning(path: Path | None = None) -> str:
    if path is None:
        user_file = STATE_DIR / "accounts-override.json"
        if user_file.is_file():
            _edit_preferences(lambda doc: doc.pop("scoring", None))
            return _tr("已恢复默认路由参数")
        return _tr("参数未变化")
    return save_tuning(dict(TUNING_DEFAULTS), path)


def _backup(path: Path, tag: str) -> Path | None:
    if not path.exists():
        return None
    folder = STATE_DIR / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{tag}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}{path.suffix}"
    shutil.copy2(path, target)
    return target


def _atomic_json(path: Path, value: dict, indent: int | None = 2) -> None:
    modules()  # Make the shared settings normalizer importable.
    import accounts_override
    value = accounts_override.normalize(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=indent) + "\n", encoding="utf-8")
    temporary.replace(path)
    _invalidate_accounts_cache()


def _edit_preferences(change) -> None:
    target = _user_accounts_path(create=True)
    import override_store
    with override_store.locked(target):
        document = json.loads(target.read_text(encoding="utf-8-sig"))
        change(document)
        _backup(target, "accounts-json")
        _atomic_json(target, document)


HARNESS_ORDER = ("claude", "codex", "pi", "antigravity-resilient", "opencode", "grok", "dot", "chatgpt-web", "grok-bot")
HARNESS_LABELS = {"claude": "Claude Code", "codex": "Codex CLI", "pi": "Pi", "antigravity-resilient": "Antigravity",
                  "opencode": "OpenCode", "grok": "Grok CLI", "dot": "Dot", "chatgpt-web": "ChatGPT Web",
                  "grok-bot": "Grok Bot CLI"}


def harness_model_rows() -> list[dict]:
    """A compact row for each (harness, account), with effort bundles grouped by row_key.

    `canonical` folds a family guess onto its series target for scoring. The checkbox is the
    row's own model (`account_fleet.row_key`): unchecking one effort of a discovered family
    name must not remove the target model. See router-internals, "Model names, efforts and pins".
    """
    modules()
    import account_fleet
    import accounts_override
    import host_policy
    import live_models
    import provider_inventory
    accounts = _cached_accounts()
    policy = host_policy.load_policy()
    preferences = accounts_override.load_override(STATE_DIR / "accounts-override.json") or {"schema_version": 2}
    inventory = (accounts.get("_discovery") or {}).get("accounts") or {}
    table = accounts.get("accounts") or {}
    groups = {}
    for owner, spec in table.items():
        for bid, bundle in (spec.get("bundles") or {}).items():
            account = accounts_override.preference_account(bundle, owner, accounts)
            if inventory and not provider_inventory.account_state.dispatchable(inventory.get(account)):
                continue
            provider = bundle.get("provider") or owner
            launcher = ((accounts.get("_discovery") or {}).get("launchers") or {}).get(provider)
            if launcher and launcher.get("status") != "ready":
                continue
            harness = accounts_override.harness(bundle, owner, bid, account_fleet.bundle_harness)
            model = account_fleet.row_key(bundle.get("model") or bid, accounts)
            groups.setdefault((account, harness), {}).setdefault(model, []).append((owner, bid, bundle))
    rows = []
    import model_catalog
    import model_latch
    latch = model_latch
    latches = latch.active(path=None if os.environ.get("ROUTER_MODEL_LATCH") else STATE_DIR / latch.FILE,
                           catalog=model_catalog.read(STATE_DIR / "model-catalog.json"))
    retry_holds = provider_retry_holds()
    live_doc = live_model_state(accounts)
    for (account, harness), models in groups.items():
        if not any(not bundle.get("discovery_unavailable") for entries in models.values() for _, _, bundle in entries):
            continue
        spec = table.get(account) or {}
        host_enabled = bool(host_policy.account_allowed(policy, account))
        harness_disabled = (((preferences.get("harness_overrides") or {}).get(account) or {}).get(harness) or {}).get("disabled", False)
        badge, kind = account_badge(account, inventory.get(account), {}, policy)
        if spec.get("_legacy_disabled_flag"):
            badge, kind = _tr("旧的关闭标记残留，已排除派发；请在托盘切换此账号，或重新运行 setup"), "warn"
        elif host_enabled and spec.get("verified") is False and kind != "warn":
            badge, kind = _tr("无法确认账号可启动，已排除派发；请登录 CLI 并刷新额度，或重新运行 setup 确认登录"), "warn"
        recovery = account_exclusion_recovery(inventory.get(account))
        if host_enabled and kind == "warn" and recovery:
            badge = f"{badge}；{recovery}"
        row = {"account": account, "account_label": account_label(account), "harness": harness,
               "harness_label": HARNESS_LABELS.get(harness, harness), "plan": spec.get("plan"),
               "host_enabled": host_enabled, "enabled": host_enabled and not harness_disabled,
               "verified": bool(spec.get("verified", False)), "legacy_disabled_flag": bool(spec.get("_legacy_disabled_flag")), "badge": badge, "kind": kind, "models": []}
        account_preferences = (preferences.get("accounts") or {}).get(account) or {}
        row["cwd_regex"] = account_preferences.get("cwd_regex", spec.get("cwd_regex")) or ""
        row["cwd_pattern"] = row["cwd_regex"]
        row["providers"] = list(dict.fromkeys(bundle.get("provider") or owner for entries in models.values()
                                              for owner, _, bundle in entries)) or [harness if harness == "pi" else account]
        for model, bundles in models.items():
            flags = [not bool(b.get("_model_disabled", b.get("disabled"))) for _, _, b in bundles]
            group = {"model": model, "model_label": model_label(model), "enabled": all(flags),
                     "partial": any(flags) and not all(flags), "bundle_ids": [bid for _, bid, _ in bundles],
                     "bundle_refs": [(owner, bid) for owner, bid, _ in bundles]}
            group.update(_model_group_tags([b for _, _, b in bundles], flags))
            unavailable = (launch_unavailable_text(latches, account, bundles)
                           or retry_model_text(retry_holds, account, bundles)
                           or live_model_group_text(live_doc, account, spec, bundles))
            if unavailable:
                group["launch_unavailable"] = unavailable
            row["models"].append(group)
        failing = sum(1 for group in row["models"] if group.get("launch_unavailable"))
        account_hold = retry_holds.get(account)
        pool_holds = {k: v for k, v in retry_holds.items() if v.get("account") == account and retry_hold_pool(v)}
        unavail_provs = [
            live_models.unavailable_detail((live_doc.get("providers") or {}).get(p))
            for p in row["providers"]
        ]
        unavail_provs = [d for d in unavail_provs if d]
        if account_hold:
            note = retry_hold_text({account: account_hold}, account)
        elif pool_holds:
            note = retry_hold_text(pool_holds, account)
        elif failing:
            note = _tr("{0} 个模型启动失败，已排除派发").format(failing)
        elif unavail_provs:
            note = _tr("模型列表暂不可用（{0}）").format(unavail_provs[0])
        else:
            note = None
        # Account cards append a hold onto an existing warning and leave an idle badge alone.
        # An unverified account is already warn (launch evidence); that must not hide a model failure or a hold.
        if note and kind != "idle":
            row["badge"] = note if kind == "ok" else f"{row['badge']}；{note}"
            row["kind"] = "warn"
        rows.append(row)
    present = {row['account'] for row in rows}
    for account, spec in table.items():
        inv = inventory.get(account) or {}
        if account in present or inv.get('status') not in provider_inventory.account_state.LISTED_STATES:
            continue
        family = inv.get('family') or account
        host_enabled = bool(host_policy.account_allowed(policy, account))
        badge, kind = account_badge(account, inv, {}, policy)
        if spec.get('_legacy_disabled_flag'):
            badge, kind = _tr("旧的关闭标记残留，已排除派发；请在托盘切换此账号，或重新运行 setup"), 'warn'
        elif host_enabled and spec.get('verified') is False and kind != 'warn':
            badge, kind = _tr("无法确认账号可启动，已排除派发；请登录 CLI 并刷新额度，或重新运行 setup 确认登录"), 'warn'
        elif kind == 'ok':
            badge, kind = _tr('没有可派发的模型；请刷新模型列表并检查模型设置'), 'warn'
        recovery = account_exclusion_recovery(inv)
        if host_enabled and kind == 'warn' and recovery:
            badge = f'{badge}；{recovery}'
        rows.append({'account': account, 'account_label': account_label(account), 'harness': family,
            'harness_label': HARNESS_LABELS.get(family, family), 'plan': spec.get('plan'),
            'host_enabled': host_enabled, 'enabled': host_enabled, 'verified': bool(spec.get('verified', True)),
            'legacy_disabled_flag': bool(spec.get('_legacy_disabled_flag')), 'badge': badge, 'kind': kind, 'models': [], 'providers': [account], 'cwd_regex': '', 'cwd_pattern': ''})
    order = {name: index for index, name in enumerate(HARNESS_ORDER)}
    rows = sorted(rows, key=lambda row: (order.get(row["harness"], len(order)), row["account"]))
    return decorate_model_rows(rows, accounts, model_catalog.read(STATE_DIR / "model-catalog.json"))


def launch_unavailable_text(latches: dict, account: str, bundles: list) -> str | None:
    """The model list's note for a model a launch reported as not existing; None when no bundle of it is latched.
    The latch lives in its own state file, never in the user's model settings."""
    import model_latch
    for owner, _, bundle in bundles:
        entry = (model_latch.entry_for({**bundle, "account": owner}, latches)
                 or model_latch.entry_for({**bundle, "account": account}, latches))
        if entry:
            retry = model_latch.retry_at(entry)
            return _tr("模型启动失败：不可用，下次重试 {0}").format(read_time_text(retry.isoformat() if retry else None))
    return None


def provider_retry_holds() -> dict:
    """Accounts and models held out of dispatch because their provider kept retrying without progress."""
    import provider_retry
    holds = provider_retry
    found = holds.active(path=None if os.environ.get("ROUTER_PROVIDER_RETRY") else STATE_DIR / holds.FILE)
    return found if isinstance(found, dict) else {}


_LIVE_REFRESH_LOCK = threading.Lock()
_LIVE_REFRESH: dict = {"thread": None}


def live_model_state(accounts: dict | None = None) -> dict:
    """Live Paseo provider model lists as roll/plan cached them. Never calls the bridge: this runs on the Tk thread
    (model page prefill) and a hung Paseo would freeze the window. A stale or missing reading starts one background
    refresh; its answer shows on the next redraw."""
    import live_models as live_mod
    if not isinstance(getattr(live_mod, "FILE", None), str) or not live_mod.enabled():
        return {"providers": {}}
    cached = live_mod.read_cached(base_dir=STATE_DIR, allow_stale=True)
    if not isinstance(cached, dict):
        return {"providers": {}}
    if isinstance(accounts, dict):
        fresh = live_mod.read_cached(base_dir=STATE_DIR).get("providers") or {}
        if any(p not in fresh for p in live_mod.candidate_providers(accounts)):
            _refresh_live_models_in_background(live_mod, accounts)
    return cached


def _refresh_live_models_in_background(live_mod, accounts: dict) -> threading.Thread | None:
    """At most one refresh at a time, on a daemon thread; a failed read is cached too, so this runs once per TTL."""
    with _LIVE_REFRESH_LOCK:
        running = _LIVE_REFRESH["thread"]
        if running is not None and running.is_alive():
            return None

        def work():
            try:
                live_mod.load_or_refresh(accounts=accounts, base_dir=STATE_DIR)
            except Exception:
                pass

        thread = threading.Thread(target=work, name="live-model-refresh", daemon=True)
        _LIVE_REFRESH["thread"] = thread
        thread.start()
        return thread


def _resolve_live_models_module():
    try:
        import live_models
        return live_models
    except Exception:
        return None


def live_model_hold_text(live_doc: dict, account: str, spec: dict) -> str | None:
    """Account card note when live Paseo model list omits configured models or is unavailable."""
    if not isinstance(live_doc, dict) or not isinstance(spec, dict):
        return None
    providers = live_doc.get("providers")
    if not isinstance(providers, dict) or not providers:
        return None
    live_mod = _resolve_live_models_module()
    if live_mod is None:
        return None
    unoffered_models: list[str] = []
    unavail_details: list[str] = []
    for bid, bspec in (spec.get("bundles") or {}).items():
        if not isinstance(bspec, dict) or bspec.get("disabled") or bspec.get("discovery_unavailable"):
            continue
        b_prov = bspec.get("provider") or account
        if b_prov == "pi" and account != "pi":
            continue
        if not live_mod.is_paseo_bundle(bspec, account, spec):
            continue
        entry = providers.get(b_prov)
        if not isinstance(entry, dict):
            continue
        if entry.get("ok") is False:
            detail = live_mod.unavailable_detail(entry)  # None for a provider Paseo does not know
            if detail and detail not in unavail_details:
                unavail_details.append(detail)
        elif entry.get("ok") is True:
            mod = live_mod.launch_model(bspec)
            if mod and not live_mod.offers_model(b_prov, mod, entry) and mod not in unoffered_models:
                unoffered_models.append(mod)
    notes: list[str] = []
    for mod in unoffered_models:
        notes.append(_tr("{0} 已排除派发（provider no longer offers {1}）").format(model_label(mod), mod))
    for detail in unavail_details:
        notes.append(_tr("模型列表暂不可用（{0}）").format(detail))
    return "；".join(notes) or None


def live_model_group_text(live_doc: dict, account: str, spec: dict, bundles: list) -> str | None:
    """Model row note when the provider's live model list no longer offers a bundle's launch model."""
    if not isinstance(live_doc, dict) or not isinstance(live_doc.get("providers"), dict) or not live_doc["providers"]:
        return None
    live_mod = _resolve_live_models_module()
    if live_mod is None:
        return None
    for owner, _, bundle in bundles:
        unoffered = live_mod.reason(bundle, live_doc, owner, spec)
        if unoffered:
            return _tr("提供方不再提供该模型（{0}），已排除派发").format(unoffered)
    return None


def _retry_what(entry: dict) -> str:
    return _tr("提供方报错") if entry.get("failure_code") == "provider_turn_failed" else _tr("提供方持续重试")


def retry_hold_pool(entry: dict) -> str | None:
    """The quota pool an account-level hold covers (provider_retry.hold_pool), or None for the whole account."""
    pool = entry.get("pool") if entry.get("model") is None else None
    return pool["id"] if isinstance(pool, dict) and isinstance(pool.get("id"), str) and pool["id"] else None


def retry_clear_pool(holds: dict, account: str) -> str | None:
    """The pool the card's clear link clears: the first held pool, one at a time, unless the whole account is held."""
    entries = [v for v in holds.values() if v.get("account") == account]
    if any(v.get("model") is None and not retry_hold_pool(v) for v in entries):
        return None
    return min((p for p in map(retry_hold_pool, entries) if p), default=None)


def retry_hold_text(holds: dict, account: str) -> str | None:
    """The account list's note for an account (or a quota pool or model of it) held for a provider failure: the
    provider's own message and when the router tries again, or how to clear an account or pool hold. None when nothing
    of the account is held."""
    notes = []
    for entry in sorted((v for v in holds.values() if v.get("account") == account),
                        key=lambda v: (v.get("model") is not None, retry_hold_pool(v) is not None,
                                       retry_hold_pool(v) or v.get("model") or "")):
        message = entry.get("message") or _tr("无提供方消息")
        pool = retry_hold_pool(entry)
        if pool and entry.get("retry_at"):
            notes.append(_tr("{0}，额度池 {1} 已排除派发：{2}（下次重试 {3}，或点卡片上的“清除 {1}”）").format(
                _retry_what(entry), pool, message, read_time_text(entry.get("retry_at"))))
        elif pool:
            notes.append(_tr("{0}，额度池 {1} 已排除派发：{2}（点卡片上的“清除 {1}”或运行 agentcompass hold --clear {3} 后再派发）").format(
                _retry_what(entry), pool, message, f"{account}#{pool}"))
        elif entry.get("model") is None and entry.get("retry_at"):
            # Widened from guesses, not an `account` answer: it backs off like a model hold.
            notes.append(_tr("{0}，整个账号已排除派发：{1}（下次重试 {2}，或点卡片上的“清除”）").format(
                _retry_what(entry), message, read_time_text(entry.get("retry_at"))))
        elif entry.get("model") is None:
            notes.append(_tr("{0}，整个账号已排除派发：{1}（点卡片上的“清除”或运行 agentcompass hold --clear {2} 后再派发）").format(
                _retry_what(entry), message, account))
        else:
            notes.append(_tr("{0} {1}，已排除派发：{2}（下次重试 {3}）").format(
                model_label(entry["model"]), _retry_what(entry), message, read_time_text(entry.get("retry_at"))))
    return "；".join(notes) or None


def clear_retry_holds(account: str, pool: str | None = None) -> bool:
    """The account card's clear button: drop every provider-failure hold of the account (its own and its models'), or
    with `pool` that quota pool's hold and its models' only."""
    modules()
    import provider_retry
    holds = provider_retry
    return holds.clear(account, path=None if os.environ.get("ROUTER_PROVIDER_RETRY") else STATE_DIR / holds.FILE,
                       **({"pool": pool} if pool else {}))


def retry_model_text(holds: dict, account: str, bundles: list) -> str | None:
    """The model list's note for a model held for a provider failure: its own hold or its quota pool's (a hold of the
    whole account badges the row instead)."""
    import provider_retry
    retry = provider_retry
    for owner, _, bundle in bundles:
        for name in (owner, account):
            entry = retry.entry_for({**bundle, "account": name}, {k: v for k, v in holds.items() if k != name})
            if not entry:
                continue
            pool = retry_hold_pool(entry)
            if pool:
                when = (_tr("下次重试 {0}").format(read_time_text(entry.get("retry_at"))) if entry.get("retry_at")
                        else _tr("点卡片上的“清除 {0}”后再派发").format(pool))
                return _tr("额度池 {0} {1}，已排除派发，{2}").format(pool, _retry_what(entry), when)
            return _tr("{0}，已排除派发，下次重试 {1}").format(_retry_what(entry), read_time_text(entry.get("retry_at")))
    return None


def _model_group_tags(bundles: list[dict], flags: list[bool]) -> dict:
    """Dropdown markers for one model group: default score, estimated price, unverified pairing, stale discovery."""
    tags = []
    if any(b.get("auto") and b.get("score_source", "calibrated") != "calibrated" for b in bundles):
        tags.append("默认分")
    if any(b.get("cost_estimated") for b in bundles):
        tags.append("估价")
    if any(b.get("compatibility") == "unverified" for b in bundles):
        tags.append("未验证")
    stale = bool(bundles) and all(b.get("auto") and b.get("availability") in ("stale", "expired") for b in bundles)
    efforts = [(b.get("effort") or "default", flag) for b, flag in zip(bundles, flags)]
    return {"tags": tags, "auto": any(b.get("auto") for b in bundles), "auto_stale": stale,
            "efforts_on": [e for e, on in efforts if on], "efforts_off": [e for e, on in efforts if not on]}


def decorate_model_rows(rows: list[dict], accounts: dict, catalog: dict) -> list[dict]:
    """Union configured bundles with advertised models, without creating launchable bundles."""
    modules()
    import account_fleet
    inventory = (accounts.get("_discovery") or {}).get("accounts") or {}
    def canonical(raw):
        resolved = account_fleet.canonical(raw, accounts)
        return re.sub(r"-(low|medium|high|xhigh|max|minimal|default|standard|extended)$", "", resolved)
    for row in rows:
        row['models'] = [group for group in row['models'] if group.get('routed', True)]
        detected, known = {}, False
        exact_catalog = row.get('harness') in ('omp', 'dsh')
        checked, failures = [], []
        for provider in row.get("providers", []):
            entry = (catalog.get("providers") or {}).get(provider)
            advertised = (inventory.get(provider) or {}).get("models")
            if entry is not None:
                models = entry.get("models", []) if entry.get("ok") and entry.get("type") != "static" else []
                known = known or bool(entry.get("ok") and entry.get("type") != "static")
                if entry.get("checked_at"):
                    checked.append(entry["checked_at"])
                failures.append(entry.get("ok") is False)
            else:
                models = advertised if isinstance(advertised, list) else []
                known = known or isinstance(advertised, list)
            for model in models:
                raw = model.get("id") if isinstance(model, dict) else model
                if not isinstance(raw, str):
                    continue
                key = raw if exact_catalog else canonical(raw)
                label = (model.get("label") or model.get("model") or raw) if isinstance(model, dict) else raw
                label = re.sub(r"\s*\((Low|Medium|High|XHigh|Max)\)$", "", label, flags=re.I)
                detected[key] = display_names.catalog_labels().get(key, label)
        row["models_checked_at"] = max(checked, default=None)
        row["models_probe_failed"] = any(failures)
        configured = {}
        consumed = set()
        for group in row["models"]:
            key = canonical(group["model"])
            configured[key] = group
            matched = next((item for item in detected if canonical(item) == key and item not in consumed), None)
            if matched is not None:
                consumed.add(matched)
            group["routed"] = True
            group["availability"] = ("missing" if group.get("auto_stale") else
                                     "detected" if matched is not None else "missing" if known else "unknown")
        # Discovery is a catalog, not an instruction to enable paid routing.
        for key, label in detected.items():
            if key not in consumed:
                row['models'].append({'model': key, 'model_label': label, 'enabled': False,
                    'partial': False, 'bundle_ids': [], 'bundle_refs': [], 'tags': ['仅目录'],
                    'auto': False, 'auto_stale': False, 'efforts_on': [], 'efforts_off': [],
                    'routed': False, 'availability': 'detected'})
    return rows


def refresh_model_catalog(force=False) -> list[dict]:
    """Background-only provider queries; the normal row read uses local cache alone."""
    import model_catalog
    rows = harness_model_rows()
    providers = list(dict.fromkeys(provider for row in rows for provider in row.get("providers", [])))
    model_catalog.refresh(providers, STATE_DIR / "model-catalog.json", definitions=model_probe_definitions(rows), force=force)
    _persist_auto_seen()
    return harness_model_rows()


def _persist_auto_seen() -> None:
    """Record when each auto bundle's model was last advertised, so stale/expired survives cache failures."""
    try:
        modules()
        import auto_bundles
        derived = (_cached_accounts().get("_auto_bundles") or {}).get("seen")
        if isinstance(derived, dict) and derived:
            auto_bundles.write_seen(STATE_DIR / "auto-bundles-seen.json", derived, now=time.time())
    except Exception:
        pass


def model_probe_definitions(rows: list[dict]) -> dict:
    """Private in-memory connection settings; these never enter row data or the model cache."""
    modules()
    import accounts_override
    document = accounts_override.load_override(STATE_DIR / "accounts-override.json") or {}
    accounts = document.get("accounts") if isinstance(document.get("accounts"), dict) else {}
    overrides = {name: spec for name, spec in accounts.items() if isinstance(spec, dict)}
    baseline = (_read_json(_accounts_path()).get("accounts") or {})
    paseo = _read_json(Path(os.environ.get("PASEO_HOME") or Path.home() / ".paseo") / "config.json")
    provider_settings = (paseo.get("agents") or {}).get("providers") or {}
    definitions = {}
    for row in rows:
        for provider in row.get("providers", []):
            provider_definition = provider_settings.get(provider) or {}
            provider_definition = provider_definition if isinstance(provider_definition, dict) else {}
            private_fields = {key: provider_definition[key] for key in
                              ("model_probe", "endpoint", "base_url", "api_key_env", "api_key") if key in provider_definition}
            definitions[provider] = {"harness": row["harness"], **baseline.get(provider, {}), **private_fields, **overrides.get(provider, {}),
                                     "configured_models": [{"id": model["model"], "label": model["model_label"]}
                                                           for model in row["models"] if model.get("routed", True)]}
    return definitions


def set_harness_models(account: str, harness: str, action: str) -> str:
    if action not in ("all", "invert", "clear"):
        raise ValueError("Unknown model selection action")
    row = next((row for row in harness_model_rows() if (row["account"], row["harness"]) == (account, harness)), None)
    if row is None:
        raise ValueError("Unknown harness/account")
    def change(document):
        for group in row["models"]:
            if not group.get("routed", True):
                continue
            enabled = action == "all" or (action == "invert" and not group["enabled"])
            for owner, bid in group["bundle_refs"]:
                document.setdefault("accounts", {}).setdefault(owner, {}).setdefault("bundles", {}).setdefault(
                    bid, {})["disabled"] = not enabled
    _edit_preferences(change)
    return _tr("模型选择已保存")


def read_account_cwd(account: str) -> str:
    modules()
    import accounts_override
    spec = _cached_accounts().get("accounts", {}).get(account) or {}
    override_doc = accounts_override.load_override(STATE_DIR / "accounts-override.json") or {}
    saved = ((override_doc.get("accounts") or {}).get(account) or {})
    if "cwd_regex" in saved:
        return saved["cwd_regex"] or ""
    return spec.get("cwd_regex") or ""


def preview_account_cwd(pattern: str, directories: list[str]) -> list[tuple[str, bool]]:
    modules()
    import cwd_policy
    value = cwd_policy.validate(pattern)
    return [(directory, cwd_policy.matches(value, directory)) for directory in directories]


def save_account_cwd(account: str, pattern: str) -> str:
    modules()
    import cwd_policy
    value = cwd_policy.validate(pattern)
    def change(document):
        spec = document.setdefault("accounts", {}).setdefault(account, {})
        spec["cwd_regex"] = value
    _edit_preferences(change)
    return _tr("账号目录限制已保存")


def toggle_harness_model(account: str, harness: str, model: str, enabled: bool | None = None) -> bool:
    row = next((row for row in harness_model_rows() if (row["account"], row["harness"]) == (account, harness)), None)
    group = next((group for group in (row or {}).get("models", []) if group["model"] == model), None)
    if group is None or not group.get("routed", True):
        raise ValueError(f"Unknown harness/model: {account}/{harness}/{model}")
    enable = not group["enabled"] if enabled is None else bool(enabled)
    def change(document):
        for owner, bid in group.get("bundle_refs", [(account, bid) for bid in group["bundle_ids"]]):
            bundles = document.setdefault("accounts", {}).setdefault(owner, {}).setdefault("bundles", {})
            bundles.setdefault(bid, {})["disabled"] = not enable
    _edit_preferences(change)
    return enable


def toggle_harness(harness: str, account: str, enabled: bool) -> str:
    """Suspend a harness row while remembering every model checkbox."""
    rows = [row for row in harness_model_rows() if row["account"] == account]
    if not any(row["harness"] == harness for row in rows):
        raise ValueError(f"Unknown harness/account: {harness}/{account}")
    def change(document):
        entry = (document.get("accounts") or {}).get(account) or {}
        if entry.get("verified") is False:
            del entry["verified"]
        preferences = document.setdefault("harness_overrides", {}).setdefault(account, {})
        if enabled and not rows[0]["host_enabled"]:
            # Enabling a row must not incidentally activate its siblings when
            # the shared billing account was previously denied by host policy.
            for row in rows:
                if row["harness"] != harness:
                    preferences.setdefault(row["harness"], {})["disabled"] = True
        preferences.setdefault(harness, {})["disabled"] = not enabled
    _edit_preferences(change)
    # Clearing a legacy flag above is cleanup only; the click keeps its row/account meaning, so a
    # multi-harness account changes host policy only when a row is enabled on a denied account.
    if len(rows) == 1 or (enabled and not rows[0]["host_enabled"]):
        if rows[0]["host_enabled"] != enabled:
            toggle_account_host(account)
    return f"{HARNESS_LABELS.get(harness, harness)} · {account_label(account)} · {_tr('启用') if enabled else _tr('停用')}"


def toggle_bundle(account: str, bundle_id: str, path: Path | None = None) -> str:
    """Toggle exactly one effort bundle; harnesses never change together."""
    if path is None:
        modules()
        import account_fleet
        bundles = ((account_fleet.load_accounts().get("accounts") or {}).get(account) or {}).get("bundles") or {}
        if bundle_id not in bundles:
            raise ValueError(_tr('未知档位 {0}').format(bundle_id))
        enable_now = bool(bundles[bundle_id].get("_model_disabled", bundles[bundle_id].get("disabled")))
        _edit_preferences(lambda doc: doc.setdefault("accounts", {}).setdefault(account, {}).setdefault(
            "bundles", {}).setdefault(bundle_id, {}).update(disabled=not enable_now))
        return _tr('{0} · {1} 已{2}').format(account_label(account), bundle_id, _tr('启用') if enable_now else _tr('停用'))
    target = path or _user_accounts_path(create=True)
    modules()
    import accounts_override
    doc = accounts_override.normalize(json.loads(target.read_text(encoding="utf-8-sig")))
    bundles = ((doc.get("accounts") or {}).get(account) or {}).get("bundles") or {}
    if bundle_id not in bundles:
        modules()
        live_acc = (account_fleet.load_accounts().get("accounts") or {}).get(account) or {}
        live_bundles = live_acc.get("bundles") or {}
        if bundle_id not in live_bundles:
            raise ValueError(_tr('未知档位 {0}').format(f'{bundle_id}'))
        acc_entry = doc.setdefault("accounts", {}).setdefault(account, {})
        bundles = acc_entry.setdefault("bundles", {})
        for bid, bspec in live_bundles.items():
            if bid not in bundles and isinstance(bspec, dict):
                bundles[bid] = dict(bspec)
    enable_now = bool(bundles[bundle_id].get("disabled"))
    bundles[bundle_id]["disabled"] = not enable_now
    _backup(target, "accounts-json")
    _atomic_json(target, doc)
    return _tr('{0} · {1} 已{2}').format(f'{account_label(account)}', f'{bundle_id}', f"{(_tr('启用') if enable_now else _tr('停用'))}")


def toggle_account_host(account: str) -> str:
    """Toggle only user policy; discovery/readiness is not a user preference.

    A leftover `verified: false` is cleared as cleanup. Leftover meter fields are not part of this
    click: `agentcompass setup` removes them and leaves the account switch as it is.
    """
    def clear_legacy(document):
        entry = (document.get("accounts") or {}).get(account) or {}
        if entry.get("verified") is False:
            del entry["verified"]
    target = STATE_DIR / "accounts-override.json"
    if target.exists():
        import accounts_override
        document = accounts_override.load_override(target)
        if ((document.get("accounts") or {}).get(account) or {}).get("verified") is False:
            _edit_preferences(clear_legacy)
    state = host_policy_state()
    all_accounts = state["accounts"]
    allowed = set(all_accounts if state["allowed"] is None else state["allowed"])
    currently_on = account in allowed
    if currently_on:
        allowed.discard(account)
        return save_host_policy(sorted(allowed), all_accounts, state["path"], state["policy"])
    allowed.add(account)
    return save_host_policy(sorted(allowed), all_accounts, state["path"], state["policy"])


# ---------- System-One scorer ----------

# (type id, display label, needs an API key, takes base_url/model, hint); ids match route_judge.SCORER_TYPES.
SCORER_TYPES = (
    ("jev", "TypeSafe · Jev (System One API)", True, True, "TypeSafe API Key；也可设环境变量 TYPESAFE_API_KEY / JEV_API_KEY"),
    ("claude_haiku_low", "Claude · Haiku 5.5 Low（默认）", False, False, "本机 claude CLI 订阅，无需 Key"),
    ("codex_terra_low", "Codex · GPT-6.1 Terra Low", False, False, "本机 codex CLI 订阅，无需 Key"),
    ("agy_flash_low", "Antigravity · Gemini 3.8 Flash Low", False, False, "本机 agy CLI，无需 Key"),
    ("openai_compatible", "自定义 OpenAI 兼容接口", True, True, "填写接口地址、模型名与 API Key（/v1/chat/completions）"),
)
SCORER_TYPES = LocalizedSequence(SCORER_TYPES)
SCORER_LABELS = {row[0]: row[1] for row in SCORER_TYPES.rows}
SCORER_SHORT = {"jev": "Jev", "claude_haiku_low": "Haiku", "codex_terra_low": "Terra",
                "agy_flash_low": "Gemini Flash", "openai_compatible": "自定义接口"}
SCORER_API_DEFAULTS = {"jev": ("https://api.typesafe.ai", "jev-latest"), "openai_compatible": ("", "")}


def _scorer_file_key(router, scorer_type: str, path: Path | None) -> str:
    """The key stored in the file for this API scorer (never shown; only compared with its masked placeholder)."""
    fields = router.SCORER_API_FIELDS.get(scorer_type)
    if not fields:
        return ""
    raw = router._read_scorer_file(path or router.scorer_config_path())
    value = raw.get(fields[0])
    return value.strip() if isinstance(value, str) else ""


def read_scorer_config(path: Path | None = None) -> dict:
    """The saved scorer choice for the settings card. Keys are masked; key_source says file / env:<NAME> / None."""
    _, router = modules()
    config = router.load_scorer_config(path)
    view = router.scorer_status(config, path)
    fields = {}
    for scorer_type in ("jev", "openai_compatible"):
        entry = view[scorer_type]
        source = entry["key_source"]
        stored = _scorer_file_key(router, scorer_type, path)
        hint = (_tr('已保存 Key（{0}）').format(f"{entry['api_key']}") if source == "file"
                else _tr('使用环境变量 {0}').format(f'{source[4:]}') if source else _tr("尚未配置 API Key"))
        fields[scorer_type] = {"api_key_masked": router.mask_secret(stored), "key_source": source, "key_hint": hint,
                               "env_key": bool(source and source.startswith("env:")),
                               "base_url": entry["base_url"], "model": entry["model"]}
    return {"scorer_type": config["scorer_type"], "label": _tr(SCORER_LABELS.get(config["scorer_type"], config["scorer_type"])),
            "fallback_to_agy": config.get("fallback_to_agy", True), "path": view["config_path"],
            "type_from_env": view.get("type_from_env"), "config_error": view.get("config_error"), "fields": fields}


def _key_update(router, scorer_type: str, api_key: str | None, path: Path | None) -> str | None:
    """None keeps the stored key (field untouched or still showing its masked placeholder), '' clears, else the new key."""
    if api_key is None:
        return None
    clean = api_key.strip()
    stored = _scorer_file_key(router, scorer_type, path)
    if stored and clean == router.mask_secret(stored):
        return None
    return clean


def save_scorer_config(scorer_type: str, api_key: str | None = None, base_url: str = "", model: str = "",
                       path: Path | None = None) -> str:
    """Validate and atomically write ~/.agent-compass/scorer-config.json. api_key: None or the masked placeholder keeps
    the saved key, '' clears it. Empty base_url/model fall back to the scorer's defaults (Jev) or are required (custom)."""
    _, router = modules()
    if scorer_type not in SCORER_LABELS:
        raise ValueError(_tr('未知小模型类型 {0}').format(f'{scorer_type}'))
    if scorer_type in SCORER_API_DEFAULTS:
        default_url, default_model = SCORER_API_DEFAULTS[scorer_type]
        url, name = (base_url or "").strip(), (model or "").strip()
        if scorer_type == "openai_compatible" and not (url and name):
            raise ValueError(_tr("自定义接口需要填写接口地址和模型名"))
        router.save_scorer_config(scorer_type, _key_update(router, scorer_type, api_key, path),
                                  "" if url == default_url else url, "" if name == default_model else name, path=path)
    else:
        router.save_scorer_config(scorer_type, path=path)
    return _tr('已保存路由分类小模型：{0}').format(_tr(SCORER_LABELS[scorer_type]))


def test_scorer_config(scorer_type: str | None = None, api_key: str | None = None, base_url: str | None = None,
                       model: str | None = None, objective: str | None = None) -> str:
    """Score one sample task with the saved config, overlaid with any unsaved form values; no agy fallback, so a
    failure names the chosen scorer's own problem."""
    _, router = modules()
    config = router.load_scorer_config()
    chosen = scorer_type or config["scorer_type"]
    if chosen not in SCORER_LABELS:
        raise ValueError(_tr('未知小模型类型 {0}').format(f'{chosen}'))
    config = {**config, "scorer_type": chosen, "fallback_to_agy": False}
    fields = router.SCORER_API_FIELDS.get(chosen)
    if fields:
        key = _key_update(router, chosen, api_key, None)
        if key is not None:
            config[fields[0]] = key
            if chosen == "jev":
                config.pop("api_key", None)
        for field, value in ((fields[1], base_url), (fields[2], model)):
            if value is not None and value.strip():
                config[field] = value.strip()
    result = router.scorer_test(config, objective)
    label, meta, profile = _tr(SCORER_SHORT.get(chosen, chosen)), result["scorer"], result["task_profile"]
    if result["ok"]:
        dims = f"D{profile['reasoning_depth']} C{profile['code_complexity']}"
        return _tr('测试成功（{0} · {1}ms）：{2} · {3}').format(f'{label}', f"{result['latency_ms']}", f"{result['derived_kind']}", f'{dims}')
    why = meta.get("fallback_detail") or meta.get("fallback_reason") or _tr("未知原因")
    return _tr('测试失败（{0} · {1}ms）：{2}；路由时将回退').format(f'{label}', f"{result['latency_ms']}", f'{why}')


# ---------- plugins ----------

def dot_details() -> dict:
    connect = _read_json(Path.home() / ".dot2api" / "connect.json")
    dot_cfg = _read_json(STATE_DIR / "dot-plugin.json")
    return {"tunnel_url": connect.get("public_base_url") or "", "transfer_repo": dot_cfg.get("transfer_repo") or ""}


def save_dot_repo(repo: str) -> str:
    clean = repo.strip()
    path = STATE_DIR / "dot-plugin.json"
    data = _read_json(path)
    data["transfer_repo"] = clean
    _atomic_json(path, data)
    return _tr('已保存 Dot 中转库：{0}').format(f"{clean or _tr('（已清空）')}")


def subscription_text() -> str:
    try:
        import services

        expires = services._subscription_expires()
    except Exception:
        return _tr("订阅状态未知")
    if expires is None:
        return _tr("未发现有效订阅")
    remaining = expires - time.time()
    return _tr("订阅已失效") if remaining <= 0 else _tr('订阅剩余 {0}').format(f'{countdown(remaining)}')
