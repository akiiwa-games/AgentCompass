"""Read subscription windows from a user's Magpie gateway, without local OAuth."""
from __future__ import annotations

import hashlib
import json
import math
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


def parse_quota(payload, source: Source):
    """Keep the selected account and vendor timestamps; never sum subscriptions."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("Magpie 用量响应无效")
    matches = [q for q in payload["data"] if isinstance(q, dict)
               and q.get("provider") == source.provider
               and q.get("kind") == "subscription"
               and (not source.account or q.get("user") == source.account or q.get("id") == source.account)]
    if len(matches) != 1:
        raise ValueError("Magpie 中未找到唯一匹配的订阅账户")
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
