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
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import plugins
import radar_chart
import router_priority
import i18n
import display_names
from display_names import MODEL_LABELS, model_label  # noqa: F401

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
# Families whose quota the Usage Float collector reads.
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
TUNING_DEFAULTS = {"safety_margin_pct": 5, "short_window_floor_pct": 10, "reset_horizon_hours": 36,
                   "reset_bonus_max": 1.2, "rotate_after_carried_tokens": 2000000,
                   "subjective.weight": 0.49, "subjective.vector_weight": 0.34, "subjective.route_weight": 0.17}
STRATEGY_PRESETS = {
    "均衡默认": dict(TUNING_DEFAULTS),
    "省额度": {"safety_margin_pct": 12, "short_window_floor_pct": 20, "reset_horizon_hours": 24,
               "reset_bonus_max": 0.8, "rotate_after_carried_tokens": 1500000,
               "subjective.weight": 0.55, "subjective.vector_weight": 0.25, "subjective.route_weight": 0.2},
    "消纳临期": {"safety_margin_pct": 3, "short_window_floor_pct": 8, "reset_horizon_hours": 72,
                 "reset_bonus_max": 1.2, "rotate_after_carried_tokens": 2000000,
                 "subjective.weight": 0.6, "subjective.vector_weight": 0.2, "subjective.route_weight": 0.2},
    "能力优先": {"safety_margin_pct": 5, "short_window_floor_pct": 10, "reset_horizon_hours": 24,
                 "reset_bonus_max": 0.8, "rotate_after_carried_tokens": 2000000,
                 "subjective.weight": 0.75, "subjective.vector_weight": 0.2, "subjective.route_weight": 0.05},
}
TUNING_LIMITS = {"safety_margin_pct": (0, 50), "short_window_floor_pct": (0, 50), "reset_horizon_hours": (1, 168),
                 "reset_bonus_max": (0, 1.2), "rotate_after_carried_tokens": (100000, 10000000),
                 "subjective.weight": (0, 1), "subjective.vector_weight": (0, 1), "subjective.route_weight": (0, 1)}
BOOST_RANGE = (0.10, 0.50)
BOOST_HOURS = router_priority.DURATIONS
PANEL_INTENT = "panel:boost"


def modules():
    """(allocator, route_judge) from the installed agent-compass skill."""
    return router_priority.modules()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------- decisions ----------

def recent_ledger(limit: int = 30, path: Path | None = None) -> list[dict]:
    """The latest plan decisions, newest first."""
    _, router = modules()
    rows = router.read_ledger(path) if path else router.read_ledger()
    plans = [r for r in rows if isinstance(r, dict) and r.get("stage") == "plan" and r.get("choice")]
    return list(reversed(plans[-limit:]))


def ledger_summary(row: dict) -> dict:
    at = row.get("at") or ""
    try:
        local = datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        local = at[:16]
    objective = " ".join(str(row.get("objective") or row.get("task_id") or "").split())
    if objective.startswith("#"):
        objective = objective.lstrip("# ")
    return {"at": local, "objective": objective[:48], "kind": row.get("kind") or "—", "choice": row.get("choice") or "—",
            "kind_label": display_names.kind_label(row.get("kind")), "choice_label": display_names.choice_label(row),
            "margin": row.get("margin")}


def build_task(objective: str, hours: float, local_only: bool = False, needs_visual: bool = False,
               work_kind: str | None = None, difficulty: str | None = None, profile: dict | None = None,
               local_only_reason: str | None = None) -> dict:
    allocator, _ = modules()
    capabilities = allocator.load_capabilities()
    needs = {"visual": bool(needs_visual), "local_only": bool(local_only)}
    if local_only:
        needs["local_only_reason"] = local_only_reason or _tr("主面板干跑：模拟需要本机资源的任务")
    task = {"objective": objective or "", "expected_hours": float(hours), "needs": needs}
    if work_kind:
        task["work_kind"] = work_kind
    if difficulty:
        task["difficulty"] = difficulty
    valid = None
    if isinstance(profile, dict):
        try:
            valid = allocator.validate_profile(profile)
        except ValueError:
            valid = None
    task["profile"] = valid or allocator.infer_task_profile(task, capabilities)
    return task


def plan_task(task: dict) -> dict:
    """allocator.plan for a task, with the same Dot reachability rule as route_judge; never writes the ledger."""
    allocator, router = modules()
    now = now_utc()
    usage = router.usage_snapshot(None, None, now)
    records = router.read_ledger()
    try:
        import dispatch_flow

        records = records + dispatch_flow.reservation_records(read_only=True)
    except Exception:
        pass
    capabilities = allocator.load_capabilities()
    policy = allocator.load_accounts()["scoring"]
    kind = allocator.task_kind(task, capabilities, policy)
    unavailable = []
    if allocator.dot_may_take(kind, task.get("needs") or {}, capabilities) and router.dot_unreachable():
        unavailable.append("dot_text")
    result = allocator.plan(task, usage, now, unavailable, records)
    # Keep the original task title/profile available to the visual replay.
    return {**result, "task": task}


def simulate(objective: str, hours: float, local_only: bool = False, needs_visual: bool = False,
             live_scorer: bool = False) -> dict:
    """Plan a typed task. live_scorer profiles it with the configured System-One scorer (as route_judge plan does)
    instead of the instant offline inference; the scorer metadata comes back under "scorer"."""
    if not live_scorer:
        return plan_task(build_task(objective, hours, local_only, needs_visual))
    allocator, router = modules()
    capabilities = allocator.load_capabilities()
    policy = allocator.load_accounts()["scoring"]
    task = build_task(objective, hours, local_only, needs_visual)
    task.pop("profile", None)
    profile, scorer = router.score_task(task, allocator, capabilities)
    task["profile"] = profile
    if scorer.get("source") == "system_one":
        task["work_kind"] = allocator.profile_kind(profile, policy)
        task["difficulty"] = allocator.profile_difficulty(profile, policy)
    return {**plan_task(task), "scorer": scorer}


def replay(row: dict) -> dict:
    """Use an immutable candidate snapshot when present; legacy records replan explicitly."""
    from decision_theatre import snapshot_result
    snapshot = snapshot_result(row)
    if snapshot is not None:
        return snapshot
    task = build_task(row.get("objective") or "", float(row.get("hours") or 1.0), bool(row.get("local_only")),
                      bool((row.get("profile") or {}).get("needs_visual")), row.get("kind"), row.get("difficulty"),
                      row.get("profile"), row.get("local_only_reason"))
    return {**plan_task(task), "theatre_source": "recomputed"}


def candidate_rows(result: dict) -> list[dict]:
    """Score breakdown for every candidate: eligible ones ranked first, then the hard-gate exclusions."""
    rows = []
    for c in result.get("ranking") or []:
        comp = c.get("components") or {}
        bundle = c.get("bundle") or {}
        rows.append({
            "id": c.get("id"), "model": bundle.get("model"), "account": bundle.get("account"),
            "effort": bundle.get("effort"), "score": c.get("score"), "fit": comp.get("fit"),
            "ability": comp.get("ability"), "opportunity_cost": comp.get("opportunity_cost"),
            "bonus": round((comp.get("reset_bonus") or 0) + (comp.get("boost") or 0) + (comp.get("free_usage") or 0), 4),
            "penalty": round((comp.get("overqualification") or 0) + (comp.get("effort_penalty") or 0), 4),
            "excluded": list(c.get("excluded") or []), "components": comp})
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
    series = [{"label": f"{display_names.candidate_label(r)} · {r['score']:.3f}", "values": radar_chart.score_dimensions(r["components"])}
              for r in pair]
    raw = [radar_chart.score_raw(r["components"]) for r in pair]
    return series, raw


def model_table() -> dict:
    allocator, _ = modules()
    accounts = allocator.load_accounts(allocator.REFERENCES / "accounts.json")
    models = {name: {**info, "usd_per_hour": allocator.model_usd_per_hour(name, info),
                     "default_subjective": dict(info.get("subjective") or {})}
              for name, info in allocator.models_table(accounts).items()}
    override = STATE_DIR / "accounts-override.json"
    if override.is_file():
        try:
            doc = json.loads(override.read_text(encoding="utf-8-sig"))
            for name, spec in (doc.get("models") or {}).items():
                if isinstance(spec, dict) and name in models and isinstance(spec.get("subjective"), dict):
                    models[name]["subjective"] = allocator.accounts_override.merge(
                        models[name].get("subjective", {}), spec["subjective"])
        except (OSError, ValueError):
            pass
    return models


def dna_series(models: dict, first: str, second: str | None) -> tuple[list[dict], list[dict]]:
    picked = [m for m in (first, second) if m and m in models]
    series = [{"label": model_label(m), "values": radar_chart.dna_dimensions(models[m])} for m in picked]
    raw = [radar_chart.dna_raw(models[m]) for m in picked]
    return series, raw


# ---------- quota and workers ----------

def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def account_badge(name: str, inventory_row: dict | None, plugin_config: dict, policy) -> tuple[str, str]:
    """(text, colour kind) of an account's status."""
    allocator, _ = modules()
    row = inventory_row or {}
    if row.get("reason") == "plugin_disabled_in_agentcompass":
        return _tr("集成已关闭"), "idle"
    if not allocator.host_policy.account_allowed(policy, name):
        if row.get('billing') in ('metered', 'unknown'):
            return _tr('仅手动使用'), 'idle'
        return _tr("本机策略禁用"), "idle"
    status = row.get("status")
    if status == 'quota_exhausted':
        return _tr('额度已耗尽，等待刷新恢复'), 'warn'
    if status == "ready":
        return _tr("就绪"), "ok"
    if status is None:
        return _tr("未发现"), "idle"
    return _tr('已暂停：{0}').format(f"{row.get('reason') or status}"), "warn"


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


def signed_out(row: dict | None) -> bool:
    """A Claude/Codex slot whose login discovery can check but found none (installed CLI, template entry)."""
    if not isinstance(row, dict) or row.get("family") not in ("claude", "codex"):
        return False
    return not (row.get("identity") or row.get("credential_key"))


def account_cards() -> list[dict]:
    allocator, router = modules()
    import provider_inventory

    now = now_utc()
    accounts = allocator.load_accounts()
    inventory = accounts.get("_discovery") if isinstance(accounts.get("_discovery"), dict) else provider_inventory.current()
    usage = router.usage_snapshot(None, None, now)
    capacity = _read_json(STATE_DIR / "capacity.json")
    observed = capacity.get("observed") or {}
    try:
        policy = allocator.host_policy.load_policy()
    except Exception:
        policy = None
    config = plugins.load()
    import venice_usage
    api_usage = _read_json(STATE_DIR / 'venice-usage.json')
    cards = []
    inv_accounts = inventory.get("accounts") or {}
    acc_table = accounts.get("accounts") or {}
    # An account drawing on another's quota (fleet `quota_sources`) shows as that pool's one card,
    # visible while any member of the pool is allowed on this host.
    sources = provider_inventory.quota_sources(accounts)
    members: dict[str, list[str]] = {}
    for member, source in sources.items():
        members.setdefault(source, []).append(member)

    def pool_allowed(name):
        return any(allocator.host_policy.account_allowed(policy, n) for n in (name, *members.get(name, ())))
    ordered = card_order([*inv_accounts.keys(), *acc_table.keys()], inv_accounts)
    names = [
        n for n in ordered
        if n in acc_table and n not in sources and (n not in members or pool_allowed(n))
        and not signed_out(inv_accounts.get(n)) and not (
            (inv_accounts.get(n) or {}).get("quota_account") not in (None, n)
            and not (acc_table.get(n) or {}).get("bundles")
        )
    ]
    reservations = pool_reservations(accounts=accounts, now=now)
    try:
        priority = router_priority.targets(allocator, accounts)
    except Exception:
        priority = []
    for name in names:
        spec = acc_table.get(name) or {}
        row = inv_accounts.get(name)
        text, kind = account_badge(name, row, config, policy)
        if name in members:
            # Quota visibility belongs to the pool, not the source account's execution switch.
            text, kind = account_badge(name, row, config, None)
        usage_row = (usage.get("accounts") or {}).get(name) or {}
        windows = []
        latched = set((row or {}).get("exhausted_windows") or ())  # Only these windows are latched out.
        for window in usage_row.get("windows") or []:
            wid = window.get("id")
            c_wid = allocator.canonical_window_id(spec, wid)
            hours = ((spec.get("windows") or {}).get(c_wid) or {}).get("hours")
            reset_passed = window.get("reset_time_passed") is True
            remaining = window.get("remaining_pct_before_reset")
            if reset_passed:
                # The reading predates the reset: the window refilled, pending a new reading.
                remaining = None  # A passed reset is not a fresh quota observation.
            elif remaining is None:
                remaining = window.get("observed_remaining_pct")
            if (row or {}).get('status') == 'quota_exhausted' or wid in latched or c_wid in latched:
                remaining = 0.0  # Explicit upstream exhaustion outranks an older positive cache.
            cap = allocator.window_capacity_usd(spec, c_wid, now) if spec else None
            acct_obs = observed.get(name) or {}
            rates = acct_obs.get(wid) or acct_obs.get(c_wid) or {}
            res = reservations.get((name, wid)) or reservations.get((name, c_wid)) or {}
            windows.append({
                "id": wid, "hours": hours, "remaining_pct": remaining,
                "remaining_usd": round(cap * remaining / 100, 1) if cap and remaining is not None else None,
                "reserved_pct": res.get("pct", 0.0), "reserved_usd": res.get("usd", 0.0),
                "reserved_workers": list(res.get("workers") or ()),
                "rate_3h": rates.get("pct_per_hour_3h"), "rate_24h": rates.get("pct_per_hour_24h"),
                "reset_in": _tr("已重置") if reset_passed else countdown(window.get("seconds_until_reset")),
                "stale": reset_passed or usage_row.get("status") != "fresh"})
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
        cards.append({"account": name, "label": spec.get("label") or account_label(name, account_family(name, row)),
                      "badge": text, "kind": kind,
                      "plan": (row or {}).get("plan") or spec.get("plan"),
                      "usage_status": 'quota_exhausted' if (row or {}).get('status') == 'quota_exhausted' else usage_row.get("status"),
                      "usage_note": venice_usage.note(api_usage) if name in ('omp', 'dsh') else
                          _tr("API 余额未提供，非无限额度") if spec.get('billing') == 'metered' and not windows else None,
                      "windows": windows, "bundles": bundle_pills,
                      "host_enabled": bool(pool_allowed(name)),
                      "verified": bool(spec.get("verified", True)),
                      "refreshable": name in ('omp', 'dsh') or bool(usage_providers([name], accounts)),
                      "priority_targets": [(t["account"], t["model"], t["label"]) for t in priority if t["account"] == name]})
    return cards


def pool_reservations(db: Path | None = None, accounts: dict | None = None,
                      now: datetime | None = None) -> dict[tuple[str, str], dict]:
    """Read-only in-flight quota reservations per (account, window_id) from active dispatch leases."""
    allocator, _ = modules()
    now = now or now_utc()
    accounts = accounts or allocator.load_accounts()
    scoring = accounts.get("scoring") or {}
    acc_table = accounts.get("accounts") or {}
    rows = _dispatch_rows(db or STATE_DIR / "dispatch.sqlite3")
    records = []
    seen = set()
    for r in rows:
        did = r.get("dispatch_id")
        if not did or did in seen or r.get("state") not in LIVE_DISPATCH:
            continue
        expiry = allocator.parse_time(r.get("lease_until"))
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
    bundle_pools_fn = getattr(allocator, "bundle_pools", None)
    for account, spec in acc_table.items():
        if not isinstance(spec, dict):
            continue
        for wid, wspec in (spec.get("windows") or {}).items():
            if not isinstance(wspec, dict):
                continue
            c_wid = allocator.canonical_window_id(spec, wid)
            cap_usd = allocator.window_capacity_usd(spec, c_wid, now)
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
                        pools = bundle_pools_fn(spec, bundle, accounts)
                    except Exception:
                        usd = allocator.inflight_reserved_usd(
                            account, spec, c_wid, wspec, {}, cap_usd, [rec], accounts, scoring, now)
                    else:
                        amt = rec.get("expected_usd")
                        if (wid in pools or c_wid in pools) and isinstance(amt, (int, float)) and not isinstance(amt, bool) and amt > 0:
                            usd = float(amt)
                else:
                    usd = allocator.inflight_reserved_usd(
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
    import provider_inventory
    if accounts is None:
        accounts = modules()[0].load_accounts()
    rows = ((accounts.get("_discovery") or {}).get("accounts") or {})
    sources = provider_inventory.quota_sources(accounts)
    result = []
    for name in names:
        source = sources.get(name, name)
        row = rows.get(source) or {}
        if row.get("family") in USAGE_FAMILIES or row.get("backend_kind") == "litellm":
            result.append(source)
    return list(dict.fromkeys(result))


def collect_usage_now(providers: list[str]) -> None:
    import sys
    uf_root = str(plugins.plugin_root("usage-float"))
    if uf_root not in sys.path:
        sys.path.insert(0, uf_root)
    import usage_float
    usage_float.fetch_all(providers, force=True)


def refresh_usage(accounts: list[str] | None = None, timeout: float = 130) -> str:
    """Ask Usage Float to re-read these accounts directly and update ~/.usage-float/usage-cache.json."""
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


def _dispatch_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return [json.loads(data) for (data,) in connection.execute("SELECT data FROM dispatches")]
    except (sqlite3.Error, ValueError):
        return []
    finally:
        connection.close()


def _paseo_agents(agents_dir: Path) -> list[dict]:
    """Unarchived Paseo agent records touched in the last three days (older files are skipped by mtime)."""
    cutoff = time.time() - AGENT_SCAN_SECONDS
    docs = []
    try:
        files = list(agents_dir.glob("*/*.json"))
    except OSError:
        return []
    for path in files:
        try:
            if path.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        doc = _read_json(path)
        if doc.get("id") and not doc.get("archivedAt"):
            docs.append(doc)
    return docs


def workers(db: Path | None = None, paseo_home: Path | None = None) -> list[dict]:
    """Live workers with their historical (uncompressed) context against the 2M rotation limit.

    A dispatch row stays 'completed' after its first turn even when the worker is resumed, so the live Paseo agent
    records are read too: running / initializing agents, and dispatched workers active in the last six hours."""
    allocator, _ = modules()
    rows = _dispatch_rows(db or STATE_DIR / "dispatch.sqlite3")
    accounts = allocator.load_accounts()
    limit = accounts["scoring"].get("rotate_after_carried_tokens", 2000000)
    now = now_utc()
    by_agent = {row["agent_id"]: row for row in rows if row.get("agent_id")}
    live = {}
    for row in rows:
        if row.get("state") not in LIVE_DISPATCH:
            continue
        hard = allocator.parse_time(row.get("hard_until"))
        if hard is not None and hard <= now:
            continue
        live[row.get("agent_id") or row.get("dispatch_id") or id(row)] = (row, {})
    if db is None or paseo_home is not None:
        home = paseo_home or Path(os.environ.get("PASEO_HOME") or Path.home() / ".paseo")
        for doc in _paseo_agents(home / "agents"):
            agent, status = doc["id"], doc.get("lastStatus")
            dispatched = (doc.get("labels") or {}).get("router.dispatch_id") or agent in by_agent
            seen = allocator.parse_time(doc.get("lastActivityAt") or doc.get("updatedAt"))
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
                session = allocator.session_state(agent)
                _WORKER_SESSION_CACHE[agent] = (stamp, mono_now, dict(session))
        else:
            session = allocator.session_state(agent)
        result.append({"agent_id": agent, "title": title, "bundle": bundle_text, "label": label, "model": model, "state": state_text,
                       "started": doc.get("lastActivityAt") or doc.get("updatedAt") or row.get("started_at"),
                       "compacted_tokens": session.get("compacted_tokens"),
                       "context_tokens": session.get("context_tokens"),
                       "historical_tokens": allocator.worker_historical_tokens(session), "limit": limit,
                       "telemetry": session.get("telemetry_status") or ("ok" if session else "unknown")})
    result.sort(key=lambda r: r["started"] or "", reverse=True)
    result.sort(key=lambda r: r["state"] == _tr("近期待命"))
    return result


# ---------- policy ----------

def overrides(now: datetime | None = None) -> list[dict]:
    allocator, router = modules()
    now = now or now_utc()
    document = router_priority.read_document(allocator.overrides_path())
    accounts = allocator.load_accounts()
    usage = router.usage_snapshot(now=now)
    rows = []
    for row in document["overrides"]:
        if not router_priority.active(row, now, allocator):
            continue
        if allocator.override_reset_landed(row, usage, accounts, now) or allocator.override_depleted(row, usage, accounts):
            continue
        rows.append({"id": row["id"], "revision": router_priority.revision(row), "account": row.get("account"),
                     "model": row.get("model"), "boost": row.get("boost"),
                     "until": router_priority.stamp(allocator.expected_reset_time(row) or row.get("until"), allocator),
                     "stop_on_reset": bool(row.get("stop_on_reset")), "intent": row.get("intent") or "",
                     **router_priority.dynamic_priority(row, allocator, accounts, usage, now),
                     "label": f"{account_label(row.get('account'))}"
                              + (f" / {model_label(row['model'])}" if row.get("model") else "")})
    return rows


def boost_targets() -> list[tuple[str, str]]:
    """(account, model) pairs with an enabled bundle."""
    allocator, _ = modules()
    accounts = allocator.load_accounts()
    seen = []
    for bundle in allocator.bundles(accounts).values():
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
    allocator, router = modules()
    now = now or now_utc()
    accounts = allocator.load_accounts()
    if account not in accounts["accounts"]:
        raise ValueError(_tr("未知账号"))
    model = allocator.canonical(model, accounts) if model else None
    path = allocator.overrides_path()
    with allocator.override_store.locked(path):
        document = router_priority.read_document(path)
        new_until = (now + timedelta(hours=hours)).isoformat()
        overlapping = [
            r for r in document["overrides"]
            if (router_priority.overlaps(r, account, model, now, allocator, accounts) if model
                else (router_priority.active(r, now, allocator) and r.get("account") == account))
        ]
        if overlapping:
            ui_rules = [r for r in overlapping if r.get("intent") in (PANEL_INTENT, router_priority.INTENT)
                        and not r.get("expires_at")]
            if not ui_rules or any(r.get("until") == new_until and (allocator.canonical(r["model"], accounts) if r.get("model") else None) == model
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
            router_priority.save(allocator, path, {**document, "v": 1})
            return _tr('已更新 {0}{1} 预期重置，{2} 小时后重置前动态优先').format(
                account_label(account), (' / ' + model_label(model) if model else ''), hours)
        row = {"id": uuid.uuid4().hex, "intent": PANEL_INTENT, "account": account,
               "created_at": now.isoformat(), "until": new_until,
               "basis": _tr('用户设置 {0}/{1} 预计 {2} 小时后重置，越近消耗优先级越高').format(account, model or _tr('全部模型'), hours)}
        if boost is not None:
            row['boost'] = round(float(boost), 2)  # Preserve legacy API data, scoring uses the reset deadline.
        if model:
            row["model"] = model
        if stop_on_reset:
            windows = [(wid, spec) for wid, spec, _ in allocator.applicable_windows(accounts["accounts"][account], model, {})
                       if spec.get("hours", 0) >= 24]
            if not windows:
                raise ValueError(_tr("该账号没有可确认重置的周额度窗口；请取消“重置后自动撤销”"))
            wid = max(windows, key=lambda w: w[1]["hours"])[0]
            usage = router.usage_snapshot(now=now)
            base = router_priority.baseline(allocator, accounts, usage, {"account": account, "window": wid}, now)
            row.update(window=wid, stop_on_reset=True, reset_confirmation="replenishment", baseline_windows={wid: base})
        document["overrides"].append(row)
        router_priority.save(allocator, path, {**document, "v": 1})
    return _tr('已添加 {0}{1} 预期重置，{2} 小时后重置前动态优先').format(account_label(account), (' / ' + model_label(model) if model else ''), hours)


def host_policy_state() -> dict:
    """{'path', 'explicit', 'allowed': set or None (all), 'accounts': [names], 'error'}."""
    allocator, _ = modules()
    path, explicit = allocator.host_policy.policy_path()
    accounts = allocator.load_accounts()
    names = list(accounts["accounts"])
    try:
        policy = allocator.host_policy.load_policy()
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
    allocator, _ = modules()
    target = path or allocator.host_policy.policy_path()[0]
    with allocator.override_store.locked(target):
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
            allocator.override_store.write(target, updated)
            return _tr('已保存本机准入：{0} 个账号').format(len(updated['allowed_accounts']))
        extra = {k: v for k, v in (existing or {}).items() if k in ("judges", "usage_providers")}
        if set(allowed) >= set(all_accounts) and not extra:
            if target.exists():
                _backup(target, "host-policy")
                target.unlink()
            return _tr("已恢复默认：本机允许全部账号")
        if not allowed:
            raise ValueError(_tr("至少保留一个账号"))
        _backup(target, "host-policy")
        _atomic_json(target, {"allowed_accounts": [a for a in all_accounts if a in allowed], **extra})
        return _tr('已保存本机准入：{0} 个账号').format(f'{len(allowed)}')


def _accounts_path() -> Path:
    allocator, _ = modules()
    return allocator.REFERENCES / "accounts.json"


def _user_accounts_path(create: bool = False) -> Path:
    user_file = STATE_DIR / "accounts-override.json"
    if create:
        modules()  # Install the router scripts path before importing the shared transaction helper.
        import override_store
        with override_store.locked(user_file):
            if user_file.is_file():
                original = json.loads(user_file.read_text(encoding="utf-8-sig"))
                if not isinstance(original, dict):
                    raise ValueError("Invalid accounts override document")
                import accounts_override
                baseline = {} if original.get("schema_version") == 2 else _read_json(_accounts_path())
                migrated = accounts_override.migrate(original, baseline)
                if migrated != original:
                    _backup(user_file, "accounts-legacy")
                    _atomic_json(user_file, migrated)
            else:
                _atomic_json(user_file, {"schema_version": 2})
        return user_file
    if user_file.is_file():
        return user_file
    return _accounts_path()


def _get(scoring: dict, key: str):
    if key.startswith("subjective."):
        return ((scoring.get("profile") or {}).get("subjective") or {}).get(key.split(".", 1)[1])
    return scoring.get(key)


def read_tuning(path: Path | None = None) -> dict:
    document = json.loads((path or _accounts_path()).read_text(encoding="utf-8-sig"))
    if path is None:
        allocator, _ = modules()
        document = allocator.accounts_override.merge(document, _read_json(STATE_DIR / "accounts-override.json"))
    scoring = document.get("scoring") or {}
    return {key: _get(scoring, key) for key in TUNING_DEFAULTS}


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
    weights = [clean.get(k, TUNING_DEFAULTS[k]) for k in ("subjective.weight", "subjective.vector_weight",
                                                          "subjective.route_weight")]
    if abs(sum(weights) - 1.0) > 0.011:
        raise ValueError(_tr("主观 / 能力 / 路由三项合成权重之和需为 1.0"))
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
                if key.startswith("subjective."):
                    scoring.setdefault("profile", {}).setdefault("subjective", {})[key.split(".", 1)[1]] = clean[key]
                else:
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
    changed = [key for key, value in clean.items() if _get(scoring, key) != value]
    if not changed:
        return _tr("参数未变化")
    for key in changed:
        start, end = _object_span(text, "scoring")
        if key.startswith("subjective."):
            start, end = _object_span(text, "profile", start, end)
            start, end = _object_span(text, "subjective", start, end)
            name = key.split(".", 1)[1]
            scoring["profile"]["subjective"][name] = clean[key]
        else:
            name = key
            scoring[key] = clean[key]
        text = _replace_number(text, name, clean[key], start, end)
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
    """A compact row for each (harness, account), with effort bundles grouped by canonical model."""
    allocator, _ = modules()
    accounts = allocator.load_accounts()
    policy = allocator.host_policy.load_policy()
    preferences = allocator.accounts_override.normalize(_read_json(STATE_DIR / "accounts-override.json"))
    inventory = (accounts.get("_discovery") or {}).get("accounts") or {}
    table = accounts.get("accounts") or {}
    groups = {}
    for owner, spec in table.items():
        for bid, bundle in (spec.get("bundles") or {}).items():
            account = allocator.accounts_override.preference_account(bundle, owner, accounts)
            if inventory and (inventory.get(account) or {}).get("status") != "ready":
                continue
            provider = bundle.get("provider") or owner
            launcher = ((accounts.get("_discovery") or {}).get("launchers") or {}).get(provider)
            if launcher and launcher.get("status") != "ready":
                continue
            harness = allocator.accounts_override.harness(bundle, owner, bid, allocator.bundle_harness)
            model = allocator.canonical(bundle.get("model") or bid, accounts)
            groups.setdefault((account, harness), {}).setdefault(model, []).append((owner, bid, bundle))
    rows = []
    for (account, harness), models in groups.items():
        if not any(not bundle.get("discovery_unavailable") for entries in models.values() for _, _, bundle in entries):
            continue
        spec = table.get(account) or {}
        host_enabled = bool(allocator.host_policy.account_allowed(policy, account))
        harness_disabled = (((preferences.get("harness_overrides") or {}).get(account) or {}).get(harness) or {}).get("disabled", False)
        badge, kind = account_badge(account, inventory.get(account), {}, policy)
        row = {"account": account, "account_label": account_label(account), "harness": harness,
               "harness_label": HARNESS_LABELS.get(harness, harness), "plan": spec.get("plan"),
               "host_enabled": host_enabled, "enabled": host_enabled and not harness_disabled,
               "verified": bool(spec.get("verified", False)), "badge": badge, "kind": kind, "models": []}
        account_preferences = (preferences.get("accounts") or {}).get(account) or {}
        row["cwd_regex"] = account_preferences.get("cwd_regex", spec.get("cwd_regex")) or ""
        row["allowed_cwd"] = spec.get("allowed_cwd") or []
        import cwd_policy
        row["cwd_pattern"] = row["cwd_regex"] or cwd_policy.from_prefixes(row["allowed_cwd"])
        row["providers"] = list(dict.fromkeys(bundle.get("provider") or owner for entries in models.values()
                                              for owner, _, bundle in entries)) or [harness if harness == "pi" else account]
        for model, bundles in models.items():
            flags = [not bool(b.get("_model_disabled", b.get("disabled"))) for _, _, b in bundles]
            group = {"model": model, "model_label": model_label(model), "enabled": all(flags),
                     "partial": any(flags) and not all(flags), "bundle_ids": [bid for _, bid, _ in bundles],
                     "bundle_refs": [(owner, bid) for owner, bid, _ in bundles]}
            group.update(_model_group_tags([b for _, _, b in bundles], flags))
            row["models"].append(group)
        rows.append(row)
    present = {row['account'] for row in rows}
    for account, spec in table.items():
        inv = inventory.get(account) or {}
        if account not in present and inv.get('models') and inv.get('status') == 'ready':
            family = inv.get('family') or account
            rows.append({'account': account, 'account_label': account_label(account), 'harness': family,
                'harness_label': HARNESS_LABELS.get(family, family), 'plan': spec.get('plan'),
                'host_enabled': bool(allocator.host_policy.account_allowed(policy, account)),
                'enabled': False, 'verified': False, 'badge': _tr('仅手动使用'), 'kind': 'warn',
                'models': [], 'providers': [account], 'cwd_regex': '', 'allowed_cwd': [], 'cwd_pattern': ''})
    order = {name: index for index, name in enumerate(HARNESS_ORDER)}
    rows = sorted(rows, key=lambda row: (order.get(row["harness"], len(order)), row["account"]))
    import model_catalog
    return decorate_model_rows(rows, accounts, model_catalog.read(STATE_DIR / "model-catalog.json"))


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
    allocator, _ = modules()
    inventory = (accounts.get("_discovery") or {}).get("accounts") or {}
    def canonical(raw):
        resolved = allocator.canonical(raw, accounts)
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
                detected[key] = model_label(key) if key in MODEL_LABELS else label
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
        allocator, _ = modules()
        import auto_bundles
        derived = (allocator.load_accounts().get("_auto_bundles") or {}).get("seen")
        if isinstance(derived, dict) and derived:
            auto_bundles.write_seen(STATE_DIR / "auto-bundles-seen.json", derived, now=time.time())
    except Exception:
        pass


def model_probe_definitions(rows: list[dict]) -> dict:
    """Private in-memory connection settings; these never enter row data or the model cache."""
    document = _read_json(STATE_DIR / "accounts-override.json")
    overrides = {name: spec for section in ("accounts", "litellm_proxies")
                 for name, spec in (document.get(section) or {}).items() if isinstance(spec, dict)}
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
    allocator, _ = modules()
    import cwd_policy
    spec = allocator.load_accounts().get("accounts", {}).get(account) or {}
    saved = ((_read_json(STATE_DIR / "accounts-override.json").get("accounts") or {}).get(account) or {})
    if "cwd_regex" in saved:
        return saved["cwd_regex"] or ""
    return spec.get("cwd_regex") or cwd_policy.from_prefixes(spec.get("allowed_cwd"))


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
        spec.pop("allowed_cwd", None)
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
        preferences = document.setdefault("harness_overrides", {}).setdefault(account, {})
        if enabled and not rows[0]["host_enabled"]:
            # Enabling a row must not incidentally activate its siblings when
            # the shared billing account was previously denied by host policy.
            for row in rows:
                if row["harness"] != harness:
                    preferences.setdefault(row["harness"], {})["disabled"] = True
        preferences.setdefault(harness, {})["disabled"] = not enabled
    _edit_preferences(change)
    if len(rows) == 1 or (enabled and not rows[0]["host_enabled"]):
        if rows[0]["host_enabled"] != enabled:
            toggle_account_host(account)
    return f"{HARNESS_LABELS.get(harness, harness)} · {account_label(account)} · {_tr('启用') if enabled else _tr('停用')}"


def normalize_weights(changed_key: str, new_val: float, current: dict[str, float]) -> dict[str, float]:
    """Keep subjective.weight + subjective.vector_weight + subjective.route_weight == 1.00 automatically."""
    keys = ("subjective.weight", "subjective.vector_weight", "subjective.route_weight")
    val = max(0.0, min(1.0, round(float(new_val), 2)))
    others = [k for k in keys if k != changed_key]
    rem = round(1.0 - val, 2)
    other_sum = sum(max(0.0, float(current.get(k, TUNING_DEFAULTS[k]))) for k in others)
    result = {changed_key: val}
    if other_sum <= 1e-6:
        first = round(rem / 2, 2)
        result[others[0]] = first
        result[others[1]] = round(rem - first, 2)
    else:
        first = round(rem * float(current.get(others[0], TUNING_DEFAULTS[others[0]])) / other_sum, 2)
        result[others[0]] = first
        result[others[1]] = round(rem - first, 2)
    return result


def save_model_subjective(model: str, divergence: float, global_rigor: float, module_precision: float,
                          path: Path | None = None, *, capability: float | None = None) -> str:
    """Save subjective DNA ratings for a model in accounts-override.json."""
    if path is None:
        if model not in model_table():
            raise ValueError(_tr('未知模型 {0}').format(model))
        ratings = dict(zip(("divergence", "global_rigor", "module_precision"),
                           [round(max(0.0, min(1.0, float(v))), 2) for v in
                            (divergence, global_rigor, module_precision)]))
        if capability is not None:
            ratings["capability"] = round(radar_chart.clamp(float(capability)), 2)
        _edit_preferences(lambda doc: doc.setdefault("models", {}).setdefault(model, {}).setdefault(
            "subjective", {}).update(ratings))
        return _tr('已保存 {0} 主观基因评分').format(model_label(model))
    target = path
    doc = json.loads(target.read_text(encoding="utf-8-sig"))
    models = doc.get("models") or {}
    if model not in models or not isinstance(models[model], dict):
        raise ValueError(_tr('未知模型 {0}').format(f'{model}'))
    models[model].setdefault("subjective", {}).update({
        "divergence": round(max(0.0, min(1.0, float(divergence))), 2),
        "global_rigor": round(max(0.0, min(1.0, float(global_rigor))), 2),
        "module_precision": round(max(0.0, min(1.0, float(module_precision))), 2),
    })
    if capability is not None:
        models[model]["subjective"]["capability"] = round(radar_chart.clamp(float(capability)), 2)
    _backup(target, "accounts-json")
    _atomic_json(target, doc)
    return _tr('已保存 {0} 主观基因评分').format(f'{model_label(model)}')


HARNESS_RULE_FIELDS = ("harness", "work_kind", "model", "model_prefix")
HARNESS_FIELD_LABELS = {"harness": "通道", "work_kind": "工作类型", "model": "模型", "model_prefix": "模型前缀"}
HARNESS_FACTOR_RANGE = (0.0, 2.0)
ANY_CHOICE = "（任意）"
ANY_CHOICE_LABELS = {"（任意）", "(any)"}
EXCHANGE_FIELDS = ("score_per_displaced_usd", "score_per_displaced_worker_hour")
EXCHANGE_LEGACY = {"score_per_displaced_usd": "shortage_loss_per_usd",
                   "score_per_displaced_worker_hour": "shortage_loss_per_worker_hour"}
EXCHANGE_RANGE = (0.0, 10.0)
EXCHANGE_DEFAULTS = {"score_per_displaced_usd": 0.012, "score_per_displaced_worker_hour": 0.06}


def _merged_scoring(path: Path | None = None) -> dict:
    document = json.loads((path or _accounts_path()).read_text(encoding="utf-8-sig"))
    if path is None:
        allocator, _ = modules()
        document = allocator.accounts_override.merge(document, _read_json(STATE_DIR / "accounts-override.json"))
    scoring = document.get("scoring") or {}
    return scoring if isinstance(scoring, dict) else {}


def _finite_number(value, label: str) -> float:
    if isinstance(value, bool) or value is None or (isinstance(value, str) and not str(value).strip()):
        raise ValueError(_tr('{0} 需为数字').format(label))
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(_tr('{0} 需为数字').format(label)) from None
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(_tr('{0} 需为数字').format(label))
    return number


def read_harness_factors(path: Path | None = None) -> dict:
    """Merged harness-factor table: default plus the rules the strategy page edits."""
    table = _merged_scoring(path).get("harness_factors")
    table = table if isinstance(table, dict) else {}
    default = table.get("default", 1.0)
    if isinstance(default, bool) or not isinstance(default, (int, float)):
        default = 1.0
    rules = []
    for rule in table.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        row = {key: "" if rule.get(key) in (None, "") else str(rule.get(key)) for key in HARNESS_RULE_FIELDS}
        factor = rule.get("factor")
        row["factor"] = factor
        basis = rule.get("basis")
        if isinstance(basis, str) and basis.strip():
            row["basis"] = basis
        rules.append(row)
    return {"default": default, "rules": rules}


def read_opportunity_exchange(path: Path | None = None) -> dict:
    """Effective rate for the form. Named keys win; legacy shortage keys only fill a gap."""
    scoring = _merged_scoring(path)
    block = scoring.get("opportunity_exchange")
    block = block if isinstance(block, dict) else {}
    found = {}
    for named, legacy in EXCHANGE_LEGACY.items():
        if named in block:
            value = block.get(named)
        elif legacy in scoring:
            value = scoring.get(legacy)
        else:
            value = EXCHANGE_DEFAULTS[named]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            value = EXCHANGE_DEFAULTS[named]
        found[named] = value
    return found


def blank_choice(text) -> str:
    """Dropdown wildcard. The displayed any-label is stored as an empty constraint."""
    text = str(text or "").strip()
    if text in ANY_CHOICE_LABELS:
        return ""
    return text


def _unique_texts(items) -> list[str]:
    found, values = set(), []
    for item in items:
        text = str(item or "").strip()
        if not text or text in found or text in ANY_CHOICE_LABELS:
            continue
        found.add(text)
        values.append(text)
    return values


def strategy_factor_choices() -> dict:
    """Harness, work-kind and model ids for the strategy dropdowns, in router order."""
    allocator, _ = modules()
    accounts = allocator.load_accounts()
    harnesses = list(HARNESS_ORDER)
    kinds = []
    models = [name for name, spec in (accounts.get("models") or {}).items() if isinstance(spec, dict)]
    try:
        routes = allocator.load_capabilities().get("routes") or {}
        if isinstance(routes, dict):
            kinds.extend(routes.keys())
    except (OSError, ValueError):
        pass
    styles = ((accounts.get("scoring") or {}).get("profile") or {}).get("kind_by_style") or {}
    if isinstance(styles, dict):
        kinds.extend(styles.values())
    for account, spec in (accounts.get("accounts") or {}).items():
        if not isinstance(spec, dict):
            continue
        for bundle_id, bundle in (spec.get("bundles") or {}).items():
            if not isinstance(bundle, dict):
                continue
            harnesses.append(allocator.bundle_harness(bundle, account, bundle_id))
            models.append(bundle.get("model"))
    for rule in read_harness_factors().get("rules") or []:
        harnesses.append(rule.get("harness"))
        kinds.append(rule.get("work_kind"))
        models.append(rule.get("model"))
    return {"harness": _unique_texts(harnesses), "work_kind": _unique_texts(kinds), "model": _unique_texts(models)}


def validate_harness_factors(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError(_tr("Harness 系数需为表格"))
    unknown = set(value) - {"default", "rules"}
    if unknown:
        raise ValueError(_tr('未知参数 {0}').format(", ".join(sorted(unknown))))
    low, high = HARNESS_FACTOR_RANGE
    default = _finite_number(value.get("default", 1.0), _tr("默认系数"))
    if not low <= default <= high:
        raise ValueError(_tr('默认系数需在 {0} ~ {1} 之间').format(low, high))
    rules_in = value.get("rules") or []
    if not isinstance(rules_in, list):
        raise ValueError(_tr("Harness 系数行需为列表"))
    rules = []
    for index, rule in enumerate(rules_in, start=1):
        if not isinstance(rule, dict):
            raise ValueError(_tr("第 {0} 行无法识别").format(index))
        extra = set(rule) - set(HARNESS_RULE_FIELDS) - {"factor", "basis"}
        if extra:
            raise ValueError(_tr('第 {0} 行含有未知字段 {1}').format(index, ", ".join(sorted(extra))))
        item = {}
        for key in HARNESS_RULE_FIELDS:
            text = blank_choice(rule.get(key))
            if not text:
                continue
            if len(text) > 120:
                raise ValueError(_tr('第 {0} 行的 {1} 过长').format(index, _tr(HARNESS_FIELD_LABELS[key])))
            item[key] = text
        if "factor" not in rule or (isinstance(rule.get("factor"), str) and not str(rule.get("factor")).strip()):
            raise ValueError(_tr("第 {0} 行缺少系数").format(index))
        factor = _finite_number(rule.get("factor"), _tr("第 {0} 行的系数").format(index))
        if not low <= factor <= high:
            raise ValueError(_tr('第 {0} 行的系数需在 {1} ~ {2} 之间').format(index, low, high))
        if not any(key in item for key in HARNESS_RULE_FIELDS):
            raise ValueError(_tr("第 {0} 行至少要填写通道、工作类型、模型或模型前缀").format(index))
        item["factor"] = round(factor, 4)
        basis = rule.get("basis")
        if isinstance(basis, str) and basis.strip():
            if len(basis.strip()) > 4000:
                raise ValueError(_tr('第 {0} 行的说明过长').format(index))
            item["basis"] = basis.strip()
        rules.append(item)
    return {"default": round(default, 4), "rules": rules}


def validate_opportunity_exchange(value: dict) -> dict:
    """Accept only the named rate. Legacy shortage keys are rejected, not written."""
    if not isinstance(value, dict):
        raise ValueError(_tr("机会成本汇率需为数字"))
    legacy = [key for key in ("shortage_loss_per_usd", "shortage_loss_per_worker_hour") if key in value]
    if legacy:
        raise ValueError(_tr('汇率只写入新键，不能包含 {0}').format(", ".join(legacy)))
    unknown = set(value) - set(EXCHANGE_FIELDS)
    if unknown:
        raise ValueError(_tr('未知参数 {0}').format(", ".join(sorted(unknown))))
    low, high = EXCHANGE_RANGE
    clean = {}
    for key in EXCHANGE_FIELDS:
        if key not in value:
            raise ValueError(_tr('机会成本汇率缺少 {0}').format(key))
        number = _finite_number(value[key], key)
        if not low <= number <= high:
            raise ValueError(_tr('{0} 需在 {1} ~ {2} 之间').format(key, low, high))
        clean[key] = round(number, 6)
    return clean


def save_strategy(values: dict, model: str | None, ratings: dict, harness_factors: dict | None = None,
                  opportunity_exchange: dict | None = None) -> str:
    """Validate the whole form before committing scoring and model ratings together.

    harness_factors and opportunity_exchange are written only when the form supplies them.
    The exchange object stores score_per_displaced_usd and score_per_displaced_worker_hour.
    A legacy shortage key already in the override is removed so it cannot contradict that rate.
    """
    clean = validate_tuning(values)
    factors = None if harness_factors is None else validate_harness_factors(harness_factors)
    exchange = None if opportunity_exchange is None else validate_opportunity_exchange(opportunity_exchange)
    if model is not None and model not in model_table():
        raise ValueError(_tr('未知模型 {0}').format(model))
    subjective = {key: round(max(0.0, min(1.0, float(ratings[key]))), 2)
                  for key in ("divergence", "global_rigor", "module_precision")} if model else {}
    if model and "capability" in ratings:
        subjective["capability"] = round(radar_chart.clamp(float(ratings["capability"])), 2)
    def change(document):
        scoring = document.setdefault("scoring", {})
        for key, value in clean.items():
            if key.startswith("subjective."):
                scoring.setdefault("profile", {}).setdefault("subjective", {})[key.split(".", 1)[1]] = value
            else:
                scoring[key] = value
        if factors is not None:
            current = scoring.get("harness_factors")
            table = dict(current) if isinstance(current, dict) else {}
            table["default"] = factors["default"]
            table["rules"] = factors["rules"]
            scoring["harness_factors"] = table
        if exchange is not None:
            for legacy in EXCHANGE_LEGACY.values():
                scoring.pop(legacy, None)
            block = scoring.get("opportunity_exchange")
            block = dict(block) if isinstance(block, dict) else {}
            for legacy in EXCHANGE_LEGACY.values():
                block.pop(legacy, None)
            block.update(exchange)
            scoring["opportunity_exchange"] = block
        if model:
            document.setdefault("models", {}).setdefault(model, {}).setdefault("subjective", {}).update(subjective)
    _edit_preferences(change)
    return _tr("已保存策略与模型画像")


def toggle_bundle(account: str, bundle_id: str, path: Path | None = None) -> str:
    """Toggle exactly one effort bundle; harnesses never change together."""
    if path is None:
        allocator, _ = modules()
        bundles = ((allocator.load_accounts().get("accounts") or {}).get(account) or {}).get("bundles") or {}
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
        allocator, _ = modules()
        live_acc = (allocator.load_accounts().get("accounts") or {}).get(account) or {}
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
    """Toggle only user policy; discovery/readiness is not a user preference."""
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
    ("agy_flash_low", "Antigravity · Gemini 3.8 Flash Low（默认）", False, False, "本机 agy CLI，无需 Key"),
    ("jev", "TypeSafe · Jev (System One API)", True, True, "TypeSafe API Key；也可设环境变量 TYPESAFE_API_KEY / JEV_API_KEY"),
    ("claude_haiku_low", "Claude · Haiku 5.5 Low", False, False, "本机 claude CLI 订阅，无需 Key"),
    ("codex_terra_low", "Codex · GPT-6.1 Terra Low", False, False, "本机 codex CLI 订阅，无需 Key"),
    ("openai_compatible", "自定义 OpenAI 兼容接口", True, True, "填写接口地址、模型名与 API Key（/v1/chat/completions）"),
    ("heuristic", "离线规则直判（无模型调用）", False, False, "不调用任何模型，按规则直接推断"),
)
SCORER_TYPES = LocalizedSequence(SCORER_TYPES)
SCORER_LABELS = {row[0]: row[1] for row in SCORER_TYPES.rows}
SCORER_SHORT = {"agy_flash_low": "Gemini Flash", "jev": "Jev", "claude_haiku_low": "Haiku",
                "codex_terra_low": "Terra", "openai_compatible": "自定义接口", "heuristic": "规则直判"}
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
        dims = f"D{profile['reasoning_depth']} C{profile['code_complexity']} S{profile['context_scale']}"
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
