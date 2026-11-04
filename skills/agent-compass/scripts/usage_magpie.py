"""Read subscription windows from a user's Magpie gateway, without local OAuth."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Source:
    url: str
    token: str
    account: str
    provider: str = "claude"
    explicit: bool = True

    @property
    def valid(self):
        address = urlsplit(self.url)
        return (address.scheme == "https" and bool(address.hostname)
                and not address.username and not address.password
                and not address.query and not address.fragment
                and bool(self.token))

    @property
    def credential_key(self):
        return hashlib.sha256("\0".join(
            ("magpie", self.url, self.token, self.provider, self.account)
        ).encode()).hexdigest()


def read_source(home: Path, extra_env: dict[str, str] | None = None) -> Source | None:
    """Discover a Magpie gateway from usage-float.json, settings.json, or Paseo provider env."""
    try:
        config = json.loads((home / "usage-float.json").read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            config = {}
    except (OSError, ValueError):
        config = {}
    try:
        settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
        if not isinstance(settings, dict):
            settings = {}
    except (OSError, ValueError):
        settings = {}
    if not config and isinstance(settings.get("usageFloat"), dict):
        config = settings["usageFloat"]
    source_kind = str(config.get("source") or "").strip().lower()
    if source_kind and source_kind != "magpie":
        return None
    env: dict[str, str] = {}
    if isinstance(extra_env, dict):
        for k, v in extra_env.items():
            if isinstance(v, str) and v.strip():
                env[k] = v.strip()
    if isinstance(settings.get("env"), dict):
        for k, v in settings["env"].items():
            if isinstance(v, str) and v.strip():
                env[k] = v.strip()
    url = str(env.get("ANTHROPIC_BASE_URL") or "").strip().rstrip("/")
    token = str(env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY") or "").strip()
    if not source_kind:
        address = urlsplit(url)
        host = (address.hostname or "").lower()
        if (address.scheme != "https" or not host
                or host in ("api.anthropic.com", "claude.ai", "platform.claude.com")
                or not token):
            return None
    return Source(
        url,
        token,
        str(config.get("account") or "").strip(),
        str(config.get("provider") or "claude").strip() or "claude",
        explicit=(source_kind == "magpie"),
    )


def _matches(payload, source: Source):
    pooled = source.account in ("", "*")
    return [q for q in payload["data"] if isinstance(q, dict)
            and q.get("provider") == source.provider
            and q.get("kind") == "subscription"
            and (pooled or q.get("user") == source.account or q.get("id") == source.account)]


def reread_targets(payload, source: Source, now: float, max_age: float, force: bool = False):
    """Accounts whose reading Magpie should take again (POST /v1/magpie/quotas/refresh?provider=&user=).

    Magpie reads a Claude account only when asked (a saved account only on its card's refresh), so a reading
    can be hours old; one older than max_age seconds, or every account when forced, is asked for again."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return []
    targets = []
    for quota in _matches(payload, source):
        user = quota.get("user") or quota.get("id")
        try:
            age = now - _timestamp(quota.get("readAt"))
        except ValueError:
            age = float("inf")
        if user and (force or age > max_age):
            targets.append((quota["provider"], user))
    return targets


def parse_quota(payload, source: Source):
    """Read the configured account; with none (or '*'), every subscription account of the provider."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("Magpie 用量响应无效")
    pooled = source.account in ("", "*")
    matches = _matches(payload, source)
    if pooled and len(matches) > 1:
        return _parse_pool(matches, source)
    if len(matches) != 1:
        raise ValueError("Magpie 中未找到唯一匹配的订阅账户" if matches or not pooled else "Magpie 中未找到订阅账户")
    quota = matches[0]
    if quota.get("error") or not quota.get("readAt"):
        raise ValueError("VPS 暂未取得有效用量")
    names = {"5 hours": ("five_hour", "5h"), "7 days": ("seven_day", "7d")}
    windows = []
    for window in quota.get("windows") or []:
        if not isinstance(window, dict) or window.get("name") not in names:
            continue
        value = window.get("used")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("Magpie 用量窗口无效")
        window_id, label = names[window["name"]]
        windows.append(dict(id=window_id, label=label, used_pct=float(value),
                            resets_at=window.get("resetsAt")))
    if not windows:
        raise ValueError("Magpie 未返回订阅用量窗口")
    windows.sort(key=lambda w: w["id"] != "seven_day")
    return quota, windows


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.timestamp()
    except (AttributeError, TypeError, ValueError):
        raise ValueError("Magpie 用量时间无效") from None


# Larger subscriptions first; the codex and claude ladders share the words they use (plus < pro < max 5x < max 20x).
PLAN_RANKS = (("20x", 5), ("max", 4), ("team", 3), ("enterprise", 3), ("business", 3), ("pro", 2), ("plus", 1))


def plan_rank(plan):
    label = str(plan or "").lower()
    return next((rank for word, rank in PLAN_RANKS if word in label), 0)


def _masked(identity):
    """An account as its hover line names it: 'k***@gmail.com', never the whole address."""
    name, at, domain = str(identity).partition("@")
    return f"{name[:1]}***{at}{domain}" if at else f"{name[:2]}***"


def _parse_pool(matches, source):
    """The largest plan's N accounts as one window set: mean usage, N accounts' worth (two fresh ones = 200%)."""
    members = []
    seen = set()
    for account in matches:
        identity = account.get("id") or account.get("user")
        if not identity or identity in seen:
            raise ValueError("Magpie 订阅账户标识缺失或重复")
        seen.add(identity)
        selected = Source(source.url, source.token, identity, source.provider)
        try:
            quota, windows = parse_quota({"data": [account]}, selected)
        except ValueError as error:
            if "暂未取得有效用量" in str(error):
                continue  # an unread account is left out; it is not counted as free capacity
            raise
        _timestamp(quota["readAt"])
        by_id = {w["id"]: w for w in windows}
        if len(by_id) != len(windows):
            raise ValueError("Magpie 用量窗口重复")
        if any(not 0 <= w["used_pct"] <= 100 for w in windows):
            raise ValueError("Magpie 用量窗口无效")
        for window in windows:
            if window["resets_at"]:
                _timestamp(window["resets_at"])
        members.append({"label": _masked(identity), "plan": quota.get("plan"), "read_at": quota["readAt"],
                        "windows": by_id})
    if not members:
        raise ValueError("VPS 暂未取得有效用量")
    top = max(plan_rank(m["plan"]) for m in members)
    largest = [m for m in members if plan_rank(m["plan"]) == top]
    count = len(largest)
    for member in members:
        member["counted"] = plan_rank(member["plan"]) == top  # smaller plans are listed, not added up
    ids = set(largest[0]["windows"])
    if any(set(m["windows"]) != ids for m in largest):
        raise ValueError("Magpie 账户池用量窗口不完整")
    suffix = f"×{count}" if count > 1 else ""
    combined = []
    for window_id in sorted(ids, key=lambda i: i != "seven_day"):
        values = [m["windows"][window_id] for m in largest]
        # The latest reset is when every account has refilled; an earlier one refills only part of the pool.
        resets = [w["resets_at"] for w in values if w["resets_at"]]
        combined.append(dict(id=window_id, label=values[0]["label"] + suffix,
                             used_pct=sum(w["used_pct"] for w in values) / count,
                             resets_at=max(resets, key=_timestamp) if resets else None, accounts=count))
    fingerprint = hashlib.sha256("\0".join(sorted(seen)).encode()).hexdigest()
    plan = largest[0]["plan"] or "subscription"
    return {"readAt": min((m["read_at"] for m in largest), key=_timestamp),
            "plan": f"{plan} ×{count}" if count > 1 else plan,
            "usage_details": {"kind": "magpie_pool", "aggregation": "plan_sum",
                              "account_count": len(members), "plan_count": count, "pool_key": fingerprint,
                              "members": members}}, combined
