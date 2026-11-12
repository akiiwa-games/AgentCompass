"""Direct subscription usage collection: provider discovery, per-provider fetchers and the usage cache.

This is the headless collector core of AgentCompass. It has no UI and no dependency on the optional Usage Float
plugin; the router, the tray's collector tick and ``agentcompass usage`` all run it. Usage Float is a display plugin
that reads the same cache file (see usage_cache.py).

Provenance: the fetchers, discovery and cache code were extracted from Usage Float's ``usage_float.py`` (v1.3.5,
https://github.com/akiiwa-games/usage-float, MIT License, Copyright (c) 2026 usage-float contributors), together with
its ``magpie_usage.py`` and ``usage_lock.py``. The MIT notice is reproduced in THIRD_PARTY_NOTICES.md at the
repository root. Usage Float keeps its own copy for its UI; the two are maintained independently and share only the
cache file format.

Differences from the upstream copy: no Tk/Windows UI, wallpaper or HUD code; the cache file location comes from
usage_cache.py; proxy settings are read from ``~/.agent-compass/usage-config.json`` first and the legacy
``~/.usage-float/config.json`` second (read-only).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import usage_cache
import usage_magpie as magpie_usage


# ---------------------------------------------------------------------------
# Cross-process lock (extracted from usage_lock.py; never lock or write CLI credentials)
# ---------------------------------------------------------------------------

_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


@contextlib.contextmanager
def file_lock(path: Path, timeout: float = 5.0):
    key = str(path.resolve())
    with _guard:
        local = _locks.setdefault(key, threading.Lock())
    deadline = time.monotonic() + timeout
    if not local.acquire(timeout=timeout):
        raise TimeoutError('Telemetry is already being updated')
    stream = None
    acquired = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = path.open('a+b')
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b'\0')
            stream.flush()
        while True:
            try:
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Telemetry is already being updated') from None
                time.sleep(0.025)
        yield
    finally:
        if stream is not None:
            if acquired:
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()
        local.release()


# ---------------------------------------------------------------------------
# Paths / constants / discovery
# ---------------------------------------------------------------------------


SUPPORTED_LANGUAGES = ("zh", "en")
_ACTIVE_LANGUAGE = "zh"


def _lang(cfg_or_state: Any = None) -> str:
    """Normalize a persisted language; a missing/unknown value always means zh."""
    value = cfg_or_state.get("language") if isinstance(cfg_or_state, dict) else cfg_or_state
    value = str(value or "zh").strip().lower()
    value = {"zh-cn": "zh", "en-us": "en"}.get(value, value)
    return value if value in SUPPORTED_LANGUAGES else "zh"


def _tr(zh: str, en: str, lang: str | None = None) -> str:
    """Translate UI text using the active language unless explicitly supplied."""
    return en if _lang(_ACTIVE_LANGUAGE if lang is None else lang) == "en" else zh


CLAUDE_OAUTH_BETA = "oauth-2025-04-20"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# The account's live plan: organization.organization_type and rate_limit_tier (e.g. claude_max +
# default_claude_max_20x). Read with each usage refresh, with the same token and headers.
CLAUDE_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"


# "cedar-ember" is Claude Code's internal name for the weekly /limit-reset
# entitlement. Its status rides along on the usage endpoint as a query flag,
# so reading it costs no extra call against an API that answers 429 easily.
CLAUDE_USAGE_RESET_URL = f"{CLAUDE_USAGE_URL}?cedar_ember=1&skip_spend=1"
CLAUDE_USAGE_AT_WALL_URL = f"{CLAUDE_USAGE_URL}?at_wall=1&skip_spend=1"
CLAUDE_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
CLAUDE_RESET_CLAIM_URL = "https://api.anthropic.com/api/organizations/{org}/reset_rate_limits"
CLAUDE_RESET_PROGRAM = "cedar_ember"
CLAUDE_SESSION_RESET_PROGRAM = "juniper_tide"
CLAUDE_RESET_GRANT_ID_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
CLAUDE_RESET_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


# The server only hands that block to the CLI surface: a plain request comes
# back with ineligible_reason="surface". Report the version actually installed
# so the gate reflects this machine instead of a number frozen into the file.
CLAUDE_CLI_VERSION_FALLBACK = "2.1.278"


CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_RESET_CARDS_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"
CODEX_RESET_CONSUME_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume"


@dataclass(frozen=True)
class CodexAccount:
    """One signed-in Codex account, identified by its own CODEX_HOME.

    Codex stores exactly one credential per home directory, so a second account
    is simply a second home. Every Codex call takes the account it belongs to
    rather than reading the process-wide default.
    """

    provider_id: str
    display_name: str
    home: Path

    @property
    def auth_path(self) -> Path:
        return self.home / "auth.json"


HOME: Path
CLAUDE_HOME: Path
CLAUDE_HOME_2: Path
CODEX_HOME: Path
CODEX_HOME_2: Path
GROK_HOME: Path
STATE_DIR: Path
CONFIG_PATH: Path
LEGACY_CONFIG_PATH: Path
CACHE_PATH: Path
CLAUDE_CLI_PACKAGE_PATHS: tuple[Path, ...]
CODEX_ACCOUNTS: tuple[CodexAccount, ...]
CODEX_ACCOUNT_BY_ID: dict[str, CodexAccount]


def configure_paths(
    cache_path: Path | None = None,
    *,
    home: Path | None = None,
    state_dir: Path | None = None,
) -> Path:
    """Configure local provider, config, and cache paths from one source of truth."""
    global HOME, CLAUDE_HOME, CLAUDE_HOME_2, CODEX_HOME, CODEX_HOME_2, GROK_HOME
    global STATE_DIR, CONFIG_PATH, LEGACY_CONFIG_PATH, CACHE_PATH
    global CLAUDE_CLI_PACKAGE_PATHS, CODEX_ACCOUNTS, CODEX_ACCOUNT_BY_ID

    resolved_home = Path(home) if home is not None else Path.home()
    claude_home = Path(os.environ.get("CLAUDE_HOME", resolved_home / ".claude"))
    claude_home_2 = Path(os.environ.get("CLAUDE_HOME_2", resolved_home / ".claude-2"))
    # Not the CODEX_HOME variable: a worker running on another account has it set, and `codex` must stay `codex` there.
    # An account's directory comes from its configuration (the Paseo provider's env, read in discover_codex_accounts).
    codex_home = resolved_home / ".codex"
    codex_home_2 = Path(os.environ.get("CODEX_HOME_2", resolved_home / ".codex-2"))
    grok_home = Path(os.environ.get("GROK_HOME", resolved_home / ".grok"))
    resolved_state = Path(state_dir) if state_dir is not None else usage_cache.state_dir(resolved_home)
    resolved_cache = Path(cache_path) if cache_path is not None else usage_cache.write_path(resolved_home)

    HOME = resolved_home
    CLAUDE_HOME = claude_home
    CLAUDE_HOME_2 = claude_home_2
    CODEX_HOME = codex_home
    CODEX_HOME_2 = codex_home_2
    GROK_HOME = grok_home
    STATE_DIR = resolved_state
    # Proxy settings: the core's own file wins, the legacy Usage Float config only fills what it leaves out (read-only).
    CONFIG_PATH = Path(os.environ.get("AGENTCOMPASS_USAGE_CONFIG") or resolved_state / "usage-config.json")
    LEGACY_CONFIG_PATH = resolved_home / ".usage-float" / "config.json"
    CACHE_PATH = resolved_cache
    CLAUDE_CLI_PACKAGE_PATHS = (
        Path(os.environ.get("APPDATA", str(resolved_home)))
        / "npm"
        / "node_modules"
        / "@anthropic-ai"
        / "claude-code"
        / "package.json",
        claude_home / "local" / "node_modules" / "@anthropic-ai" / "claude-code" / "package.json",
        resolved_home / ".npm-global" / "lib" / "node_modules" / "@anthropic-ai" / "claude-code" / "package.json",
    )
    CODEX_ACCOUNTS = (
        CodexAccount("codex", "codex", codex_home),
        CodexAccount("codex-2", "codex-2", codex_home_2),
    )
    CODEX_ACCOUNT_BY_ID = {account.provider_id: account for account in CODEX_ACCOUNTS}
    return CACHE_PATH


configure_paths()


# Claude and Antigravity share a login across several independently configurable rows.
CLAUDE_DISPLAY_ITEMS: tuple[str, ...] = ("claude:fable", "claude:7d", "claude:5h")


ANTIGRAVITY_WINDOW_ITEMS: dict[str, str] = {
    "gemini-weekly": "antigravity:gemini-weekly",
    "gemini-5h": "antigravity:gemini-5h",
    "3p-weekly": "antigravity:3p-weekly",
    "3p-5h": "antigravity:3p-5h",
}


ANTIGRAVITY_DISPLAY_ITEMS: tuple[str, ...] = tuple(ANTIGRAVITY_WINDOW_ITEMS.values())


PROVIDER_DISPLAY_ITEMS: dict[str, tuple[str, ...]] = {
    "claude": CLAUDE_DISPLAY_ITEMS,
    "claude-2": ("claude-2:fable", "claude-2:7d", "claude-2:5h"),
    "antigravity": ANTIGRAVITY_DISPLAY_ITEMS,
}


# Display order. A provider needs local credentials (Antigravity needs its CLI),
# so a machine with one Codex account shows one Codex row.
DEFAULT_PROVIDERS: tuple[str, ...] = (
    "codex",
    "codex-2",
    "grok",
    *CLAUDE_DISPLAY_ITEMS,
    *ANTIGRAVITY_DISPLAY_ITEMS,
)


PROVIDER_LABELS: dict[str, str] = {
    "codex": "Codex",
    "codex-2": "Codex 2",
    "grok": "Grok",
    "claude": "Claude",
    "claude:fable": "fable 7d",
    "claude:7d": "claude 7d",
    "claude:5h": "claude 5h",
    "claude-2": "Claude 2",
    "claude-2:fable": "Claude 2 fable 7d",
    "claude-2:7d": "Claude 2 7d",
    "claude-2:5h": "Claude 2 5h",
    "pi": "Pi",
    "opencode": "OpenCode",
    "antigravity": "Antigravity",
    "antigravity:gemini-weekly": "agy gemini 7d",
    "antigravity:gemini-5h": "agy gemini 5h",
    "antigravity:3p-weekly": "agy claude 7d",
    "antigravity:3p-5h": "agy claude 5h",
}


def _read_paseo_providers() -> dict[str, dict[str, Any]]:
    paseo_path = Path(os.environ.get("PASEO_CONFIG") or HOME / ".paseo" / "config.json")
    try:
        if not paseo_path.is_file():
            return {}
        doc = json.loads(paseo_path.read_text(encoding="utf-8-sig"))
        providers = ((doc.get("agents") or {}).get("providers") or {}) if isinstance(doc, dict) else {}
        return {str(k): v for k, v in providers.items() if isinstance(k, str) and isinstance(v, dict)}
    except Exception:
        return {}


def _register_claude_display_items(provider_id: str, label: str | None = None) -> None:
    if provider_id not in PROVIDER_DISPLAY_ITEMS:
        PROVIDER_DISPLAY_ITEMS[provider_id] = (
            f"{provider_id}:fable",
            f"{provider_id}:7d",
            f"{provider_id}:5h",
        )
    if provider_id != "claude" and provider_id not in PROVIDER_LABELS:
        base = (label or provider_id.replace("-", " ").title()).strip()
        PROVIDER_LABELS[provider_id] = base
        PROVIDER_LABELS[f"{provider_id}:fable"] = f"{base} fable 7d"
        PROVIDER_LABELS[f"{provider_id}:7d"] = f"{base} 7d"
        PROVIDER_LABELS[f"{provider_id}:5h"] = f"{base} 5h"


def discover_claude_homes() -> dict[str, Path]:
    """Return {provider_id: config_dir} for all configured or discovered Claude accounts."""
    homes: dict[str, Path] = {"claude": CLAUDE_HOME}
    if CLAUDE_HOME_2 != HOME / ".claude-2" or CLAUDE_HOME_2.is_dir():
        homes["claude-2"] = CLAUDE_HOME_2
    for pid, spec in _read_paseo_providers().items():
        if spec.get("enabled") is False:
            continue
        extends = str(spec.get("extends") or "").strip().lower()
        if pid == "claude" or extends == "claude" or (not extends and pid.startswith("claude-")):
            env = spec.get("env") if isinstance(spec.get("env"), dict) else {}
            cfg = str(env.get("CLAUDE_CONFIG_DIR") or "").strip()
            if pid == "claude" and CLAUDE_HOME != HOME / ".claude":
                homes[pid] = CLAUDE_HOME
            elif pid == "claude-2" and CLAUDE_HOME_2 != HOME / ".claude-2":
                homes[pid] = CLAUDE_HOME_2
            elif cfg:
                homes[pid] = Path(cfg).expanduser()
            elif pid not in homes:
                homes[pid] = HOME / f".{pid}"
            _register_claude_display_items(pid, str(spec.get("label") or "").strip() or None)
    try:
        for entry in sorted(HOME.glob(".claude-*")):
            if entry.is_dir():
                pid = entry.name.lstrip(".")
                if pid and pid not in homes:
                    homes[pid] = entry
                    _register_claude_display_items(pid)
    except OSError:
        pass
    for pid in homes:
        _register_claude_display_items(pid)
    return homes


def claude_account_home(provider_id: str = "claude") -> Path:
    if provider_id == "claude":
        return CLAUDE_HOME
    if provider_id == "claude-2" and CLAUDE_HOME_2 != HOME / ".claude-2":
        return CLAUDE_HOME_2
    homes = discover_claude_homes()
    if provider_id not in homes and provider_id.startswith("claude-"):
        _register_claude_display_items(provider_id)
    return homes.get(provider_id, HOME / f".{provider_id}")


def claude_account_paseo_env(provider_id: str) -> dict[str, str]:
    spec = _read_paseo_providers().get(provider_id) or {}
    env = spec.get("env") if isinstance(spec.get("env"), dict) else {}
    return {str(k): str(v) for k, v in env.items() if isinstance(k, str) and isinstance(v, str) and v.strip()}


def is_claude_provider(provider_id: str) -> bool:
    return provider_id == "claude" or provider_id.startswith("claude-") or provider_id in discover_claude_homes()


def discover_codex_accounts() -> dict[str, CodexAccount]:
    """Return {provider_id: CodexAccount} for all configured or discovered Codex accounts."""
    accounts: dict[str, CodexAccount] = {
        pid: acc for pid, acc in CODEX_ACCOUNT_BY_ID.items()
        if pid == "codex" or acc.home != HOME / f".{pid}" or acc.home.is_dir()
    }
    for pid, spec in _read_paseo_providers().items():
        env = spec.get("env") if isinstance(spec.get("env"), dict) else {}
        cfg = str(env.get("CODEX_HOME") or "").strip()
        if spec.get("enabled") is False or (pid in accounts and not (pid == "codex" and cfg)):
            continue
        extends = str(spec.get("extends") or "").strip().lower()
        if pid == "codex" or extends == "codex" or (not extends and pid.startswith("codex-")):
            home = Path(cfg).expanduser() if cfg else (HOME / (".codex" if pid == "codex" else f".{pid}"))
            accounts[pid] = CodexAccount(pid, pid, home)
            PROVIDER_LABELS.setdefault(pid, str(spec.get("label") or pid.replace("-", " ").title()).strip())
    try:
        for entry in sorted(HOME.glob(".codex-*")):
            if entry.is_dir() and not entry.name.startswith((".codex-chatgpt-web", ".codex-pro-consult")):
                pid = entry.name.lstrip(".")
                if pid and pid not in accounts:
                    accounts[pid] = CodexAccount(pid, pid, entry)
                    PROVIDER_LABELS.setdefault(pid, pid.replace("-", " ").title())
    except OSError:
        pass
    CODEX_ACCOUNT_BY_ID.update(accounts)
    return accounts


def is_codex_account(provider_id: str) -> bool:
    """A known Codex account, discovering Paseo-configured ones on first sight."""
    if provider_id in CODEX_ACCOUNT_BY_ID:
        return True
    return (provider_id == "codex" or provider_id.startswith("codex-")) and provider_id in discover_codex_accounts()


def provider_fetch_id(item: str) -> str:
    """The provider a display item is fetched from ("claude:5h" -> "claude", "codex:7d" -> "codex")."""
    if item == 'grok-bot':
        return 'grok'  # Saved runner selection uses the ordinary Grok meter.
    provider, sep, _ = item.partition(":")
    if sep and is_codex_account(provider):
        return provider
    if sep and (provider in PROVIDER_DISPLAY_ITEMS or is_claude_provider(provider)):
        _register_claude_display_items(provider)
        return provider
    return item


# SuperGrok / Grok Build credit window (same endpoint CC Switch / CodexBar use).
GROK_BILLING_URL = "https://grok.com/grok_api_v2.GrokBuildBilling/GetGrokCreditsConfig"


# Many LiteLLM virtual keys cannot call /key/info. Spend and budget come back
# on actual LLM responses as x-litellm-key-spend / x-litellm-key-max-budget.
# Probe models are overridable; leave empty unless env or config supplies an endpoint.
LLM_PROXY_DEFAULT_ENDPOINT = ""
LLM_PROXY_EMBED_MODEL = "gemini-embedding-2"
LLM_PROXY_CHAT_FALLBACK_MODEL = "glm-5.3-flash"


# Virtual keys often cannot call /key/info. Weekly (7d) budgets reset Monday
# midnight in the proxy timezone (UTC+8 unless configured).
LLM_PROXY_BUDGET_DURATION_DEFAULT = "7d"
LLM_PROXY_TIMEZONE_DEFAULT = "UTC+8"


LLM_PROXY_RESET_HEADERS = (
    "x-litellm-key-budget-reset-at",
    "x-litellm-budget-reset-at",
    "x-litellm-key-reset-at",
)


LLM_PROXY_DURATION_HEADERS = (
    "x-litellm-key-budget-duration",
    "x-litellm-budget-duration",
)


REFRESH_SECONDS = 300
IDLE_REFRESH_SECONDS = 1800
UNKNOWN_ACTIVITY_REFRESH_SECONDS = 900
CLAUDE_429_BACKOFF_SECONDS = 900


COLLECTOR_CONFIG_KEYS: tuple[str, ...] = (
    "language",
    "llm_proxy_timezone",
    "llm_proxy_budget_duration",
    "llm_proxy_api_key",
    "llm_proxy_endpoint",
    "llm_proxy_provider_id",
    "litellm_proxies",
)


def read_usage_config() -> dict[str, Any]:
    """Collector settings (LLM proxy keys and budget); {} when neither file exists or parses."""
    merged: dict[str, Any] = {}
    for path in (LEGACY_CONFIG_PATH, CONFIG_PATH):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            merged.update(data)
    return merged


def write_usage_config(cfg: dict[str, Any]) -> None:
    """Persist collector-relevant settings to CONFIG_PATH atomically."""
    global _ACTIVE_LANGUAGE
    if not isinstance(cfg, dict):
        return
    updates = {k: cfg[k] for k in COLLECTOR_CONFIG_KEYS if k in cfg}
    if "language" in updates:
        _ACTIVE_LANGUAGE = _lang(updates["language"])
    if not updates:
        return
    with file_lock(CONFIG_PATH.with_name(f"{CONFIG_PATH.name}.lock"), timeout=2):
        existing: dict[str, Any] = {}
        try:
            if CONFIG_PATH.exists():
                loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
                if isinstance(loaded, dict):
                    existing = loaded
        except (OSError, ValueError):
            pass
        existing.update(updates)
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = CONFIG_PATH.with_name(f"{CONFIG_PATH.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(existing, indent=2), encoding="utf-8", newline="\n")
            temporary.replace(CONFIG_PATH)
        finally:
            temporary.unlink(missing_ok=True)


def load_usage_cache(*, strict: bool = False) -> dict[str, Any]:
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        raise ValueError("Invalid usage cache")
    except FileNotFoundError:
        pass
    except Exception:
        if strict:
            raise
        pass
    return {"providers": {}, "claude_backoff_until": None}


def save_usage_cache(cache: dict[str, Any]) -> None:
    """Atomic publication; read/modify/write callers hold the cache lock."""
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = CACHE_PATH.with_name(f"{CACHE_PATH.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(cache, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(CACHE_PATH)
    finally:
        temporary.unlink(missing_ok=True)


def update_usage_cache(update: Callable[[dict[str, Any]], None]) -> None:
    with file_lock(CACHE_PATH.with_suffix('.lock')):
        if CACHE_PATH == usage_cache.canonical_path(HOME):
            usage_cache.seed_from_legacy(HOME)
        # A transient read failure (a locked or unreadable file) must not turn a read/modify/write into an
        # empty-cache publication that destroys all display history. A file that is readable but is not valid JSON
        # will not heal by waiting, so it is set aside under a new name and the collector starts a new cache.
        try:
            cache = load_usage_cache(strict=True)
        except ValueError:
            quarantine = CACHE_PATH.with_name(f"{CACHE_PATH.name}.corrupt-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}")
            CACHE_PATH.replace(quarantine)
            if CACHE_PATH == usage_cache.canonical_path(HOME):
                usage_cache.seed_from_legacy(HOME)
            cache = load_usage_cache(strict=True)
        update(cache)
        save_usage_cache(cache)


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        t = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(t)
    except Exception:
        return None


@dataclass
class UsageWindow:
    id: str
    label: str
    used_pct: float | None
    resets_at: str | None = None
    # A pool of N same-plan subscriptions: used_pct is their mean, so the window holds N accounts' worth.
    accounts: int = 1
    # Models this allowance is scoped to, as the provider names them; None binds every model (window_scope.py).
    models: list[str] | None = None


@dataclass
class ProviderUsage:
    provider_id: str
    display_name: str
    plan_label: str | None = None
    # Where plan_label was read: "profile_api" for Claude's live OAuth profile; None when not stated.
    plan_source: str | None = None
    windows: list[UsageWindow] = field(default_factory=list)
    error: str | None = None
    available: bool = True
    stale: bool = False
    fetched_at: str | None = None
    # Free-form one-line summary override (e.g. grok credits without %)
    summary: str | None = None
    # Raw provider-specific reset entitlement block (Claude's cedar_ember).
    # Kept as the untouched payload so a field added upstream survives the
    # cache round-trip without a schema change here.
    reset_block: dict[str, Any] | None = None
    # Local provenance, not an account identifier or a credential. Rotation
    # invalidates reuse too: only the exact credential may reuse this snapshot.
    credential_key: str | None = None
    backend_kind: str | None = None
    billing: str | None = None
    home: str | None = None
    # Sanitized native quota provenance/units, never credentials or account IDs.
    usage_details: dict[str, Any] | None = None
    error_status: int | None = None
    error_code: str | None = None


def claude_magpie_source(provider_id: str):
    if not is_claude_provider(provider_id):
        return None
    home = claude_account_home(provider_id)
    extra_env = claude_account_paseo_env(provider_id) if home.parent == HOME else None
    source = magpie_usage.read_source(home, extra_env=extra_env)
    if source is not None and not source.explicit:
        non_magpie = (load_usage_cache().get("non_magpie_gateways") or {}).get(provider_id)
        if non_magpie and non_magpie == source.credential_key:
            return None
    return source


def provider_credential_key(provider_id: str) -> str | None:
    remote = claude_magpie_source(provider_id)
    if remote is not None:
        return remote.credential_key
    if is_claude_provider(provider_id):
        home = claude_account_home(provider_id)
        oauth, _ = read_claude_oauth(home)
        token = (oauth or {}).get("accessToken")
    elif provider_id == "pi":
        home = _pi_agent_home()
        auth, _ = read_pi_codex_auth(home)
        tokens = (auth or {}).get("tokens") or {}
        token = str(tokens.get("access_token") or '') + ':' + str(tokens.get("account_id") or '')
        if not tokens.get("access_token"):
            token = None
    elif provider_id == "grok":
        home = GROK_HOME
        token = _grok_credential_material()
    else:
        codex_accounts = discover_codex_accounts()
        if provider_id in codex_accounts:
            home = codex_accounts[provider_id].home
            auth, _ = read_codex_auth(home)
            tokens = (auth or {}).get("tokens") or {}
            token = str(tokens.get("access_token") or '') + ':' + str(tokens.get("account_id") or '')
            if not tokens.get("access_token"):
                token = None
        else:
            return None
    if not token:
        return None
    return hashlib.sha256((str(home.resolve()) + '\0' + token).encode()).hexdigest()


def _provider_to_dict(p: ProviderUsage) -> dict[str, Any]:
    out = {
        "provider_id": p.provider_id,
        "display_name": p.display_name,
        "plan_label": p.plan_label,
        **({"plan_source": p.plan_source} if p.plan_source else {}),
        "windows": [
            {
                "id": w.id,
                "label": w.label,
                "used_pct": w.used_pct,
                "resets_at": w.resets_at,
                **({"accounts": w.accounts} if w.accounts > 1 else {}),
                **({"models": list(w.models)} if w.models else {}),
            }
            for w in p.windows
        ],
        "error": p.error,
        "available": p.available,
        "stale": p.stale,
        "fetched_at": p.fetched_at,
        "summary": p.summary,
        "reset_block": p.reset_block,
        "credential_key": p.credential_key,
        "usage_details": p.usage_details,
    }
    if p.backend_kind:
        out["backend_kind"] = p.backend_kind
    if p.billing:
        out["billing"] = p.billing
    if p.home:
        out["home"] = p.home
    if p.error_status is not None:
        out["error_status"] = p.error_status
    if p.error_code is not None:
        out["error_code"] = p.error_code
    return out


provider_to_dict = _provider_to_dict


def window_models(window) -> list[str] | None:
    models = window.get("models") if isinstance(window, dict) else None
    named = [m for m in models if isinstance(m, str) and m.strip()] if isinstance(models, list) else []
    return named or None


def pool_accounts(window) -> int:
    count = window.get("accounts") if isinstance(window, dict) else None
    return count if isinstance(count, int) and not isinstance(count, bool) and count > 1 else 1


def _provider_from_dict(d: dict[str, Any]) -> ProviderUsage:
    windows = [
        UsageWindow(
            id=str(w.get("id") or ""),
            label=str(w.get("label") or ""),
            used_pct=float(w["used_pct"]) if w.get("used_pct") is not None else None,
            resets_at=w.get("resets_at"),
            accounts=pool_accounts(w),
            models=window_models(w),
        )
        for w in (d.get("windows") or [])
        if isinstance(w, dict)
    ]
    return ProviderUsage(
        provider_id=str(d.get("provider_id") or ""),
        display_name=str(d.get("display_name") or "?"),
        plan_label=d.get("plan_label"),
        plan_source=d.get("plan_source"),
        windows=windows,
        error=d.get("error"),
        available=bool(d.get("available", True)),
        stale=bool(d.get("stale", False)),
        fetched_at=d.get("fetched_at"),
        summary=d.get("summary"),
        reset_block=d.get("reset_block") if isinstance(d.get("reset_block"), dict) else None,
        credential_key=d.get("credential_key"),
        backend_kind=d.get("backend_kind"),
        billing=d.get("billing"),
        home=d.get("home"),
        usage_details=d.get("usage_details") if isinstance(d.get("usage_details"), dict) else None,
        error_status=d.get("error_status") if isinstance(d.get("error_status"), int) and not isinstance(d.get("error_status"), bool) else None,
        error_code=d.get("error_code") if isinstance(d.get("error_code"), str) else None,
    )


def cache_get_provider(provider_id: str, *, cache: dict[str, Any] | None = None) -> ProviderUsage | None:
    doc = load_usage_cache() if cache is None else cache
    raw = (doc.get("providers") or {}).get(provider_id)
    if not isinstance(raw, dict):
        return None
    try:
        p = _provider_from_dict(raw)
    except (TypeError, ValueError):
        return None
    if p.provider_id != provider_id or p.credential_key != provider_credential_key(provider_id):
        return None
    if not p.windows and not p.summary:
        return None
    p.stale = True
    return p


def cache_put_provider(p: ProviderUsage) -> None:
    if not p.available or p.error or not p.fetched_at:
        return
    if not p.windows and not p.summary:
        return
    key = provider_credential_key(p.provider_id)
    if p.credential_key is not None and p.credential_key != key:
        return
    p.credential_key = key
    snap = _provider_to_dict(p)
    def update(cache):
        providers = cache.setdefault("providers", {})
        old = providers.get(p.provider_id) or {}
        # A different pool membership or reading rule is a new observation, whatever its vendor time.
        pool = lambda details: tuple((details or {}).get(k) for k in ('pool_key', 'aggregation'))
        same_pool = pool(old.get('usage_details')) == pool(p.usage_details)
        if old.get('credential_key') == key and same_pool:
            new_at, old_at = _iso_epoch(p.fetched_at, 0), _iso_epoch(old.get('fetched_at'), 0)
            # A stale observation (e.g. an old gateway readAt) is kept, still
            # flagged stale, only when it is strictly newer than what we hold.
            if old_at > new_at or (p.stale and old_at >= new_at):
                return
        providers[p.provider_id] = snap
        cache.setdefault('display_snapshots', {})[p.provider_id] = snap
    update_usage_cache(update)


def display_snapshot(provider_id: str, *, cache: dict[str, Any] | None = None) -> ProviderUsage | None:
    """Presentation history is never promoted into the router's quota cache."""
    doc = load_usage_cache() if cache is None else cache
    raw = (doc.get('display_snapshots') or {}).get(provider_id)
    if not isinstance(raw, dict) or raw.get('credential_key') != provider_credential_key(provider_id):
        return None
    try:
        item = _provider_from_dict(raw)
    except (TypeError, ValueError):
        return None
    if item.provider_id != provider_id or not (item.windows or item.summary):
        return None
    item.stale = True
    return item


def read_cached_usage(providers: Iterable[str], *, now: float | None = None) -> list[dict[str, Any]]:
    """Read cached provider observations and display snapshots with product freshness semantics."""
    cache = load_usage_cache()
    raw_providers = cache.get("providers") if isinstance(cache.get("providers"), dict) else {}
    polls = cache.get("poll") if isinstance(cache.get("poll"), dict) else {}
    now_ts = time.time() if now is None else float(now)
    out: list[dict[str, Any]] = []
    for pid in dict.fromkeys(provider_fetch_id(item) for item in providers):
        raw = raw_providers.get(pid)
        item = cache_get_provider(pid, cache=cache)
        if item is not None:
            observed = _iso_epoch(item.fetched_at, -1.0)
            age = now_ts - observed if observed > 0 else float("inf")
            allowed = usage_cache.poll_max_age(polls.get(pid), now_ts, REFRESH_SECONDS)
            item.stale = (
                bool(raw.get("stale", False) if isinstance(raw, dict) else False)
                or bool(item.error)
                or not (0.0 <= age <= allowed)
            )
            out.append(_provider_to_dict(item))
            continue
        expected_key = provider_credential_key(pid)
        snap = display_snapshot(pid, cache=cache)
        if snap is not None:
            if isinstance(raw, dict) and raw.get("credential_key") in (None, expected_key) and raw.get("error"):
                snap.error = str(raw["error"])
            out.append(_provider_to_dict(snap))
            continue
        if isinstance(raw, dict) and raw.get("credential_key") in (None, expected_key) and raw.get("error"):
            try:
                err_item = _provider_from_dict(raw)
                err_item.provider_id = pid
                err_item.stale = True
                err_item.credential_key = expected_key
                out.append(_provider_to_dict(err_item))
            except (TypeError, ValueError):
                pass
    return out


def provider_error_code(payload: Any) -> str | None:
    """A provider's machine-readable error code. Its free-form text is never kept."""
    if not isinstance(payload, dict):
        return None
    candidates = [payload.get('error'), payload.get('code')]
    for block in (payload.get('error'), payload.get('detail')):
        if isinstance(block, dict):
            candidates[:0] = [block.get('code'), block.get('type')]
    return next((value.lower() for value in candidates
                 if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', value)), None)


def mark_provider_stale(provider_id: str, error: str, status: int | None = None, code: str | None = None,
                        plan: str | None = None) -> None:
    key = provider_credential_key(provider_id)
    now_iso = datetime.now(timezone.utc).isoformat()
    def update(cache):
        providers = cache.setdefault('providers', {})
        row = providers.get(provider_id)
        if isinstance(row, dict) and row.get('credential_key') != key:
            providers.pop(provider_id, None)
            row = None
        disp = (cache.get('display_snapshots') or {}).get(provider_id)
        if isinstance(disp, dict) and disp.get('credential_key') != key:
            cache['display_snapshots'].pop(provider_id, None)
        if not isinstance(row, dict):
            if status is None and code is None and plan is None and (key is not None or provider_id == 'grok'):
                return
            row = {
                'provider_id': provider_id,
                'display_name': provider_id,
                'plan_label': plan,
                'windows': [],
                'error': error,
                'available': False,
                'stale': True,
                'fetched_at': None,
                'error_at': now_iso,
                'credential_key': key,
            }
            providers[provider_id] = row
        if not row.get('error_at') or not row.get('stale') or status in (401, 402, 403) or code:
            row['error_at'] = now_iso
        row['stale'] = True
        row['error'] = error
        # What the provider itself said, for provider_inventory; a good reading replaces the whole row.
        row['error_status'], row['error_code'] = (status if status and status != 200 else None), code
        if plan:
            row['plan_label'] = plan
    update_usage_cache(update)


def claude_in_backoff(provider_id: str = "claude") -> bool:
    cache = load_usage_cache()
    if cache.get(f'{provider_id}_backoff_credential_key') != provider_credential_key(provider_id):
        return False
    until = cache.get(f"{provider_id}_backoff_until")
    dt = parse_iso(str(until) if until else None)
    if not dt:
        return False
    return datetime.now(timezone.utc) < dt


def set_claude_backoff(seconds: int = CLAUDE_429_BACKOFF_SECONDS, *, provider_id: str = "claude") -> None:
    until = datetime.now(timezone.utc).timestamp() + seconds
    update_usage_cache(lambda cache: cache.update({
        f"{provider_id}_backoff_until": datetime.fromtimestamp(until, tz=timezone.utc).isoformat(),
        f'{provider_id}_backoff_credential_key': provider_credential_key(provider_id)
    }))


def clear_claude_backoff(provider_id: str = "claude") -> None:
    update_usage_cache(lambda cache: cache.update({f"{provider_id}_backoff_until": None}))


def _parse_json_body(raw: str) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return raw


def http_json_response(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 20.0,
) -> tuple[int, Any, dict[str, str]]:
    data = None
    req_headers = {"Accept": "application/json", "User-Agent": "usage-float/1.2"}
    if headers:
        req_headers.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return (
                resp.status,
                _parse_json_body(raw),
                {str(k).lower(): str(v) for k, v in resp.headers.items()},
            )
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        return (
            e.code,
            _parse_json_body(raw),
            {str(k).lower(): str(v) for k, v in e.headers.items()},
        )
    except Exception as e:
        return 0, str(e), {}


def http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 20.0,
) -> tuple[int, Any]:
    status, payload, _headers = http_json_response(
        url, method=method, headers=headers, body=body, timeout=timeout
    )
    return status, payload


def read_claude_oauth(home: Path | None = None) -> tuple[dict[str, Any] | None, Path | None]:
    cred_path = (home if home is not None else CLAUDE_HOME) / ".credentials.json"
    if not cred_path.exists():
        return None, None
    try:
        data = json.loads(cred_path.read_text(encoding="utf-8"))
        oauth = data.get("claudeAiOauth") or {}
        if oauth.get("accessToken"):
            return oauth, cred_path
    except Exception:
        return None, None
    return None, None


def claude_cli_version() -> str:
    """Version of the Claude Code CLI installed on this machine.

    The weekly-reset block is gated on the caller looking like a recent CLI, so
    a stale number would silently turn the field off. Read it fresh on every
    call — the CLI self-updates under a long-running HUD, and re-reading a 1 KB
    manifest once per refresh is cheaper than serving a version that has since
    dropped below the gate.
    """
    for path in CLAUDE_CLI_PACKAGE_PATHS:
        try:
            if not path.exists():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            found = str((data or {}).get("version") or "").strip()
            if found:
                return found
        except Exception:
            continue
    return CLAUDE_CLI_VERSION_FALLBACK


def claude_request_headers(token: str) -> dict[str, str]:
    version = claude_cli_version()
    return {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": CLAUDE_OAUTH_BETA,
        # Identify as the CLI surface; without this the response reports
        # ineligible_reason="surface" and the reset block is always empty.
        "User-Agent": f"claude-cli/{version} (external, cli)",
        "anthropic-client-platform": "cli",
        "anthropic-client-version": version,
    }


def claude_authorized_json(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    timeout: float = 20.0,
    home: Path | None = None,
) -> tuple[int, Any]:
    """Use CLI-owned credentials read-only; re-read once after a GET auth failure.

    Returns status 0 when there are no local credentials at all.
    """
    oauth, cred_path = read_claude_oauth(home) if home is not None else read_claude_oauth()
    if not oauth:
        return 0, None

    def call(token: str) -> tuple[int, Any]:
        return http_json(
            url,
            method=method,
            headers=claude_request_headers(token),
            body=body,
            timeout=timeout,
        )

    status, payload = call(oauth["accessToken"])
    if status not in (401, 403):
        return status, payload
    # Both accounts belong to Claude Code. Never compete for rotating refresh
    # tokens, and never replay a reset claim under a newly signed-in account.
    if method.upper() == 'GET':
        latest, _ = read_claude_oauth(home) if home is not None else read_claude_oauth()
        if latest and latest.get("accessToken") != oauth["accessToken"]:
            return call(latest["accessToken"])
    return status, payload


def display_fallback(
    provider_id: str, error: str, previous: Iterable[ProviderUsage] = (),
) -> ProviderUsage:
    """Recover presentation history without writing it into the quota cache."""
    candidates = [p for p in previous if isinstance(p, ProviderUsage)
                  and p.provider_id == provider_id and (p.windows or p.summary)]
    try:
        key = provider_credential_key(provider_id)
        # Do not attribute a known previous login's quota to a new login.
        if key is not None:
            candidates = [p for p in candidates if p.credential_key in (None, key)]
    except Exception:
        pass  # Discovery/read failures must not erase the UI's last reading.
    for reader in (cache_get_provider, display_snapshot):
        try:
            item = reader(provider_id)
            if item is not None:
                candidates.append(item)
        except Exception:
            continue
    if candidates:
        cached = max(candidates, key=lambda p: _iso_epoch(p.fetched_at, 0))
        # Copy before marking stale: the UI may still be rendering this object.
        cached = _provider_from_dict(_provider_to_dict(cached))
        cached.stale = True
        cached.error = error
        return cached
    return ProviderUsage(provider_id=provider_id, display_name=provider_id,
                         available=False, error=error, stale=True)


def stale_provider_usage(
    provider_id: str, error: str, previous: Iterable[ProviderUsage] = (),
    status: int | None = None, code: str | None = None, plan: str | None = None,
) -> ProviderUsage:
    cached = display_fallback(provider_id, error, previous)
    cached.error_status = status or None
    cached.error_code = code
    if plan:
        cached.plan_label = plan
    try:
        mark_provider_stale(provider_id, error, status, code, plan)
    except Exception:
        pass  # Cache contention must not defeat presentation fallback.
    return cached


def _claude_rate_limited_usage(provider_id: str = "claude") -> ProviderUsage:
    return stale_provider_usage(provider_id, _tr("限流退避中", 'Rate limit cooldown'))


def fetch_magpie_claude_usage(source, provider_id: str, *, probe_only: bool = False,
                              force: bool = False) -> ProviderUsage | None:
    if not source.valid:
        return None if probe_only else stale_provider_usage(provider_id, _tr("Magpie HTTPS 用量来源配置不完整", 'Magpie HTTPS usage source is not fully configured'))
    auth = {"Authorization": "Bearer " + source.token}
    status, payload = http_json(source.url + "/v1/magpie/quotas", headers=auth)
    # Magpie reads a Claude account only when asked, so its readings age by hours; have it read the old ones
    # (or all, on a manual refresh) once more, then take the list again.
    targets = magpie_usage.reread_targets(payload, source, time.time(), REFRESH_SECONDS * 2, force) if status == 200 else []
    for vendor, user in targets:
        http_json(source.url + "/v1/magpie/quotas/refresh?" + urllib.parse.urlencode({"provider": vendor, "user": user}),
                  method="POST", headers=auth, timeout=60.0)
    if targets:
        status, payload = http_json(source.url + "/v1/magpie/quotas", headers=auth)
    if status != 200:
        if probe_only and status in (0, 400, 404, 405, 501):
            update_usage_cache(lambda c: c.setdefault("non_magpie_gateways", {})
                               .update({provider_id: source.credential_key}))
            return None
        return stale_provider_usage(provider_id, _tr(f"VPS 用量读取失败（HTTP {status}）", f'VPS usage request failed (HTTP {status})'))
    if source.credential_key != provider_credential_key(provider_id):
        return stale_provider_usage(provider_id, _tr("用量来源已变化，等待下次更新", 'Usage source changed; waiting for the next update'))
    try:
        quota, windows = magpie_usage.parse_quota(payload, source)
    except ValueError as error:
        if probe_only and (not isinstance(payload, dict) or not isinstance(payload.get("data"), list)):
            update_usage_cache(lambda c: c.setdefault("non_magpie_gateways", {})
                               .update({provider_id: source.credential_key}))
            return None
        return stale_provider_usage(provider_id, str(error))
    age = time.time() - _iso_epoch(quota["readAt"], 0)
    result = ProviderUsage(
        provider_id=provider_id, display_name=provider_id,
        plan_label=quota.get("plan"), windows=[UsageWindow(**w) for w in windows],
        available=True, fetched_at=quota["readAt"],
        stale=age > REFRESH_SECONDS * 2 or age < -60,
        credential_key=source.credential_key,
        backend_kind="gateway",
        billing="subscription",
        home=str(claude_account_home(provider_id)),
        usage_details=quota.get("usage_details"),
    )
    cache_put_provider(result)
    return result


def claude_profile_plan(profile: Any) -> str | None:
    """The plan label of a live OAuth profile, or None when it does not say. organization_type claude_max with
    rate_limit_tier ..._20x / ..._5x is "max20" / "max5" (the plan-template names); another claude_<plan> is "<plan>"."""
    org = profile.get("organization") if isinstance(profile, dict) else None
    if not isinstance(org, dict):
        return None
    kind = str(org.get("organization_type") or "").strip().lower()
    if not kind.startswith("claude_") or kind == "claude_":
        return None
    plan = kind.removeprefix("claude_")
    if plan == "max":
        tier = str(org.get("rate_limit_tier") or "").lower()
        plan = "max20" if "20x" in tier else "max5" if "5x" in tier else "max"
    return plan


def fetch_claude_usage(*, force: bool = False, provider_id: str = "claude") -> ProviderUsage:
    remote = claude_magpie_source(provider_id)
    if remote is not None:
        if remote.explicit:
            return fetch_magpie_claude_usage(remote, provider_id, force=force)
        probed = fetch_magpie_claude_usage(remote, provider_id, probe_only=True, force=force)
        if probed is not None:
            return probed
    home = None if provider_id == "claude" else claude_account_home(provider_id)
    if claude_in_backoff(provider_id):
        return _claude_rate_limited_usage(provider_id)

    oauth, _cred_path = read_claude_oauth(home) if home is not None else read_claude_oauth()
    if not oauth:
        return ProviderUsage(
            provider_id=provider_id,
            display_name=provider_id,
            available=False,
            error="更新失败",
        )

    source_key = provider_credential_key(provider_id)
    # Not oauth["subscriptionType"]: the login file's plan is frozen at the CLI's last login and goes stale when the
    # subscription changes. The plan is the live profile's, read below with the usage reading.
    status, payload = (claude_authorized_json(CLAUDE_USAGE_RESET_URL, home=home)
                       if home is not None else claude_authorized_json(CLAUDE_USAGE_RESET_URL))

    if source_key != provider_credential_key(provider_id):
        return stale_provider_usage(provider_id, _tr('登录凭据已变化，等待下次更新', 'Login credentials changed; waiting for the next update'))
    if status == 429:
        set_claude_backoff(provider_id=provider_id)
        return _claude_rate_limited_usage(provider_id)

    if status != 200 or not isinstance(payload, dict):
        error = _tr("登录凭据需由 Claude Code 更新", 'Login credentials must be updated by Claude Code') if status in (401, 403) else "更新失败"
        return stale_provider_usage(provider_id, error, status=status, code=provider_error_code(payload))

    windows: list[UsageWindow] = []
    five = payload.get("five_hour") or {}
    # Rows read "fable 7d → claude 7d → claude 5h"; Settings decides which of
    # them show and in what order.
    #
    # Model-scoped weekly limits (e.g. Fable)
    for entry in payload.get("limits") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("kind") != "weekly_scoped":
            continue
        scope = entry.get("scope") or {}
        model = ((scope.get("model") or {}).get("display_name") or "").strip()
        if not model:
            continue
        # Prefer Fable; still show other scoped models if present
        pct = entry.get("percent")
        if pct is None:
            continue
        try:
            pct_f = float(pct)
        except Exception:
            continue
        model_key = model.lower().replace(" ", "")
        windows.append(
            UsageWindow(
                id=f"weekly_{model_key}",
                label=f"{model_key} 7d",
                used_pct=pct_f,
                resets_at=entry.get("resets_at"),
                models=[model],
            )
        )

    week = payload.get("seven_day") or {}
    if isinstance(week, dict) and week.get("utilization") is not None:
        windows.append(
            UsageWindow(
                id="seven_day",
                label="7d",
                used_pct=float(week["utilization"]),
                resets_at=week.get("resets_at"),
            )
        )

    if isinstance(five, dict) and five.get("utilization") is not None:
        windows.append(
            UsageWindow(
                id="five_hour",
                label="5h",
                used_pct=float(five["utilization"]),
                resets_at=five.get("resets_at"),
            )
        )

    if not windows or any(w.used_pct is None or not math.isfinite(w.used_pct) for w in windows):
        return stale_provider_usage(provider_id, _tr('用量响应缺少有效窗口', 'Usage response has no valid windows'))
    reset_block = payload.get("cedar_ember")
    profile_status, profile = (claude_authorized_json(CLAUDE_PROFILE_URL, home=home)
                               if home is not None else claude_authorized_json(CLAUDE_PROFILE_URL))
    plan = claude_profile_plan(profile) if profile_status == 200 else None
    result = ProviderUsage(
        provider_id=provider_id,
        display_name=provider_id,
        plan_label=plan,
        plan_source="profile_api" if plan else None,
        windows=windows,
        available=True,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        reset_block=reset_block if isinstance(reset_block, dict) else None,
        credential_key=source_key,
        backend_kind="subscription",
        billing="subscription",
        home=str(claude_account_home(provider_id)),
    )
    clear_claude_backoff(provider_id)
    cache_put_provider(result)
    return result


CLAUDE_RESET_WINDOW_LABELS: dict[str, tuple[str, str]] = {
    "five_hour": ("5 小时会话", "5-hour session"),
    "seven_day": ("7 天周额度", "7-day weekly limit"),
    "seven_day_overage_included": ("7 天周额度（含超额）", "7-day weekly limit (with overage)"),
    "seven_day_opus": ("Opus 周额度", "Opus weekly limit"),
    "seven_day_sonnet": ("Sonnet 周额度", "Sonnet weekly limit"),
}

CLAUDE_RESET_RESULT_LABELS: dict[str, tuple[str, str]] = {
    "already_used": ("这次重置已经用过了。", "This reset has already been used."),
    "not_limited": ("还没到用量上限，暂时不能使用，额度未消耗。", "Usage limit not reached yet; reset credit was not consumed."),
    "cooldown": ("刚用过一次重置，冷却中，稍后再试。", "Cooldown active after a recent reset. Please try again later."),
    "ineligible": ("当前账号不符合使用条件。", "This account is not eligible."),
    "unavailable": ("重置暂不可用，稍后再试。", "Reset is currently unavailable. Please try again later."),
}

CLAUDE_SESSION_RESULT_LABELS: dict[str, tuple[str, str]] = {
    "already_used": ("本周的 5 小时重置已经用过了。", "This week's 5-hour reset has already been used."),
    "not_limited": ("还没到 5 小时上限，暂时不能使用，额度未消耗。", "5-hour limit not reached yet; reset was not consumed."),
    "ineligible": ("当前账号不符合使用条件。", "This account is not eligible."),
    "unavailable": ("重置暂不可用，稍后再试。", "Reset is currently unavailable. Please try again later."),
}


def codex_error_text(status: int, payload: Any) -> str:
    detail: str | None = None
    if isinstance(payload, dict):
        for key in ("detail", "message", "error_description"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                detail = value.strip()
                break
        if detail is None:
            error = payload.get("error")
            if isinstance(error, str) and error.strip():
                detail = error.strip()
            elif isinstance(error, dict):
                value = error.get("message") or error.get("detail")
                if isinstance(value, str) and value.strip():
                    detail = value.strip()
    elif isinstance(payload, str) and payload.strip():
        detail = payload.strip()
    if detail:
        detail = detail[:160]
        return f"HTTP {status} · {detail}" if status else detail
    return f"HTTP {status}" if status else _tr("网络不可用", "Network unavailable")


def claude_organization_uuid(provider_id: str = "claude") -> tuple[str | None, int]:
    """Resolve the OAuth organization UUID for a Claude account, respecting 429 backoff."""
    is_primary = provider_id == "claude"
    home = None if is_primary else claude_account_home(provider_id)
    paths = (CLAUDE_HOME / ".claude.json", HOME / ".claude.json") if is_primary else (home / ".claude.json",)
    for path in paths:
        try:
            if not path.exists():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            found = str(((data or {}).get("oauthAccount") or {}).get("organizationUuid") or "")
            if found.strip():
                return found.strip(), 200
        except Exception:
            continue
    if claude_in_backoff(provider_id):
        return None, 429
    status, payload = (
        claude_authorized_json(CLAUDE_PROFILE_URL, home=home)
        if home is not None
        else claude_authorized_json(CLAUDE_PROFILE_URL)
    )
    if status == 429:
        set_claude_backoff(provider_id=provider_id)
        return None, 429
    if status == 200 and isinstance(payload, dict):
        found = str((payload.get("organization") or {}).get("uuid") or "").strip()
        if found:
            return found, 200
    return None, status


def fetch_claude_session_reset(provider_id: str = "claude") -> tuple[dict[str, Any] | None, str | None]:
    """Read Claude's 5h session reset block while enforcing 429 rate-limit backoff."""
    if claude_in_backoff(provider_id):
        return None, _tr("限流退避中，稍后再试。", "Rate limit cooldown. Please try again later.")
    home = None if provider_id == "claude" else claude_account_home(provider_id)
    status, payload = (
        claude_authorized_json(CLAUDE_USAGE_AT_WALL_URL, home=home)
        if home is not None
        else claude_authorized_json(CLAUDE_USAGE_AT_WALL_URL)
    )
    if status == 0 and payload is None:
        return None, _tr("未找到可用的 Claude 登录凭证", "No usable Claude login credentials found")
    if status == 429:
        set_claude_backoff(provider_id=provider_id)
        return None, _tr("请求太频繁，稍后再试。", "Too many requests. Please try again later.")
    if status != 200 or not isinstance(payload, dict):
        return None, _tr(
            f"读取失败：{codex_error_text(status, payload)}",
            f"Could not load: {codex_error_text(status, payload)}",
        )
    clear_claude_backoff(provider_id)
    block = payload.get("juniper_tide")
    return (block if isinstance(block, dict) else {}), None


def claim_claude_reset(
    grant_id: str, *, request_id: str, provider_id: str = "claude"
) -> dict[str, Any]:
    """Spend one /limit-reset grant while enforcing 429 rate-limit backoff."""
    if not CLAUDE_RESET_GRANT_ID_RE.match(grant_id or "") or not CLAUDE_RESET_REQUEST_ID_RE.match(request_id or ""):
        return {
            "ok": False,
            "message": _tr("重置数据格式异常，未发送请求。", "Invalid reset data. No request was sent."),
            "settled": True,
        }
    if claude_in_backoff(provider_id):
        return {
            "ok": False,
            "message": _tr("限流退避中，稍后再试。", "Rate limit cooldown. Please try again later."),
            "settled": False,
        }
    home = None if provider_id == "claude" else claude_account_home(provider_id)
    org, org_status = claude_organization_uuid(provider_id)
    if org_status == 429:
        return {
            "ok": False,
            "message": _tr("请求太频繁，稍后再试。", "Too many requests. Please try again later."),
            "settled": False,
        }
    if not org:
        return {
            "ok": False,
            "message": _tr("找不到 Claude 组织信息，请在 Claude Code 里重新登录。", "Claude organization not found. Please sign in again in Claude Code."),
            "settled": True,
        }
    url = CLAUDE_RESET_CLAIM_URL.format(org=urllib.parse.quote(org, safe=""))
    body = {
        "program": CLAUDE_RESET_PROGRAM,
        "grant_id": grant_id,
        "request_id": request_id,
    }
    status, payload = (
        claude_authorized_json(url, method="POST", body=body, timeout=25.0, home=home)
        if home is not None
        else claude_authorized_json(url, method="POST", body=body, timeout=25.0)
    )
    if status == 0 and payload is None:
        return {
            "ok": False,
            "message": _tr("未找到可用的 Claude 登录凭证", "No usable Claude login credentials found"),
            "settled": True,
        }
    if status == 200 and isinstance(payload, dict):
        result = str(payload.get("result") or "unavailable")
        if result == "reset":
            cleared = _tr("、", ", ").join(
                _tr(*CLAUDE_RESET_WINDOW_LABELS.get(str(w), (str(w), str(w))))
                for w in payload.get("cleared") or []
            )
            message = _tr(f"已重置{cleared}。", f"Reset {cleared}.") if cleared else _tr("用量已重置。", "Usage reset.")
            left = payload.get("resets_left")
            if isinstance(left, int) and not isinstance(left, bool):
                message += _tr(f" 剩余 {left} 次。", f" {left} remaining.")
            return {"ok": True, "message": message, "settled": True}
        zh, en = CLAUDE_RESET_RESULT_LABELS.get(result, CLAUDE_RESET_RESULT_LABELS["unavailable"])
        return {"ok": False, "message": _tr(zh, en), "settled": True}
    if status == 429:
        set_claude_backoff(provider_id=provider_id)
        return {
            "ok": False,
            "message": _tr("请求太频繁，稍后再试。", "Too many requests. Please try again later."),
            "settled": False,
        }
    if status in (401, 403):
        return {
            "ok": False,
            "message": _tr("Claude 登录已失效，请在 Claude Code 里重新登录。", "Claude login expired. Please sign in again in Claude Code."),
            "settled": True,
        }
    return {
        "ok": False,
        "message": _tr(f"使用失败：{codex_error_text(status, payload)}", f"Redemption failed: {codex_error_text(status, payload)}"),
        "settled": False,
    }


def claim_claude_session_reset(provider_id: str = "claude") -> dict[str, Any]:
    """Spend this week's 5h session reset while enforcing 429 rate-limit backoff."""
    if claude_in_backoff(provider_id):
        return {
            "ok": False,
            "message": _tr("限流退避中，稍后再试。", "Rate limit cooldown. Please try again later."),
            "settled": False,
        }
    home = None if provider_id == "claude" else claude_account_home(provider_id)
    org, org_status = claude_organization_uuid(provider_id)
    if org_status == 429:
        return {
            "ok": False,
            "message": _tr("请求太频繁，稍后再试。", "Too many requests. Please try again later."),
            "settled": False,
        }
    if not org:
        return {
            "ok": False,
            "message": _tr("找不到 Claude 组织信息，请在 Claude Code 里重新登录。", "Claude organization not found. Please sign in again in Claude Code."),
            "settled": True,
        }
    url = CLAUDE_RESET_CLAIM_URL.format(org=urllib.parse.quote(org, safe=""))
    status, payload = (
        claude_authorized_json(url, method="POST", body={"program": CLAUDE_SESSION_RESET_PROGRAM}, timeout=25.0, home=home)
        if home is not None
        else claude_authorized_json(url, method="POST", body={"program": CLAUDE_SESSION_RESET_PROGRAM}, timeout=25.0)
    )
    if status == 0 and payload is None:
        return {
            "ok": False,
            "message": _tr("未找到可用的 Claude 登录凭证", "No usable Claude login credentials found"),
            "settled": True,
        }
    if status == 200 and isinstance(payload, dict):
        result = str(payload.get("result") or "unavailable")
        if result == "reset":
            return {"ok": True, "message": _tr("5 小时会话已重置。", "5-hour session reset."), "settled": True}
        zh, en = CLAUDE_SESSION_RESULT_LABELS.get(result, CLAUDE_SESSION_RESULT_LABELS["unavailable"])
        return {"ok": False, "message": _tr(zh, en), "settled": True}
    if status == 429:
        set_claude_backoff(provider_id=provider_id)
        return {
            "ok": False,
            "message": _tr("请求太频繁，稍后再试。", "Too many requests. Please try again later."),
            "settled": False,
        }
    if status in (401, 403):
        return {
            "ok": False,
            "message": _tr("Claude 登录已失效，请在 Claude Code 里重新登录。", "Claude login expired. Please sign in again in Claude Code."),
            "settled": True,
        }
    return {
        "ok": False,
        "message": _tr(f"使用失败：{codex_error_text(status, payload)}", f"Redemption failed: {codex_error_text(status, payload)}"),
        "settled": False,
    }


def read_codex_auth(home: Path | None = None) -> tuple[dict[str, Any] | None, Path | None]:
    auth_path = (CODEX_HOME if home is None else home) / "auth.json"
    if not auth_path.exists():
        return None, None
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
        tokens = (data.get("tokens") or {}) if isinstance(data, dict) else {}
        if tokens.get("access_token"):
            return data, auth_path
    except Exception:
        return None, None
    return None, None


def refresh_codex_token(refresh_token: str) -> dict[str, Any] | None:
    body = (
        f"grant_type=refresh_token"
        f"&refresh_token={urllib.parse.quote(refresh_token)}"
        f"&client_id={urllib.parse.quote(CODEX_CLIENT_ID)}"
    ).encode("utf-8")
    req = urllib.request.Request(
        CODEX_TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "usage-float/1.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def codex_authorized_json(
    url: str,
    *,
    home: Path | None = None,
    method: str = "GET",
    body: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    """Call a ChatGPT backend endpoint with the local Codex OAuth token.

    Returns the same (status, payload) pair as http_json(); status 0 means the
    local credential is missing. Only the Codex CLI owns token rotation.
    """
    resolved_home = CODEX_HOME if home is None else home
    auth, auth_path = read_codex_auth(resolved_home)
    if not auth:
        return 0, None

    tokens = auth.get("tokens") or {}
    access = tokens.get("access_token")
    account_id = tokens.get("account_id")
    if not access:
        return 0, None

    def call(token: str, account: str | None) -> tuple[int, Any]:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if account:
            headers["ChatGPT-Account-ID"] = str(account)
        return http_json(url, method=method, headers=headers, body=body)

    status, payload = call(access, account_id)
    if status in (401, 403) and method.upper() == 'GET':
        latest, _ = read_codex_auth(resolved_home)
        new = (latest or {}).get('tokens') or {}
        if new.get('access_token') and new['access_token'] != access:
            return call(new['access_token'], new.get('account_id'))
    return status, payload


def fetch_codex_usage(account: CodexAccount) -> ProviderUsage:
    source_key = provider_credential_key(account.provider_id)
    status, payload = codex_authorized_json(CODEX_USAGE_URL, home=account.home)
    if source_key != provider_credential_key(account.provider_id):
        return stale_provider_usage(account.provider_id, _tr('登录凭据已变化，等待下次更新', 'Login credentials changed; waiting for the next update'))
    if status != 200 or not isinstance(payload, dict):
        error = _tr("登录凭据需由 Codex 更新", 'Login credentials must be updated by Codex') if status in (401, 403) else "更新失败"
        return stale_provider_usage(account.provider_id, error, status=status, code=provider_error_code(payload))

    rate = payload.get("rate_limit") or {}
    # Preserve both constraints for the router; primary is not always weekly.
    windows = []
    for name, default_seconds in (("primary_window", 18000), ("secondary_window", 604800)):
        window = rate.get(name) or {}
        used = window.get('used_percent')
        if used is None:
            continue
        seconds = window.get('limit_window_seconds') or default_seconds
        wid = 'five_hour' if seconds == 18000 else 'usage' if seconds == 604800 else f'usage-{int(seconds / 60)}m'
        windows.append(UsageWindow(
            id=wid, label='5h' if seconds == 18000 else '7d' if seconds == 604800 else wid,
            used_pct=float(used), resets_at=_iso_timestamp(window.get('reset_at')),
        ))
    if not windows:
        return stale_provider_usage(account.provider_id, "更新失败", plan=str(payload.get("plan_type") or "") or None)
    result = ProviderUsage(
        provider_id=account.provider_id,
        display_name=account.display_name,
        plan_label=str(payload.get("plan_type") or "") or None,
        windows=windows,
        available=True,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        credential_key=source_key,
        backend_kind="subscription",
        billing="subscription",
        home=str(account.home),
    )
    cache_put_provider(result)
    return result


def _resolve_codex_home(home: Path | None, provider_id: str | None) -> Path | None:
    if provider_id is not None:
        account = discover_codex_accounts().get(provider_id)
        return account.home if account is not None else None
    return CODEX_HOME if home is None else home


def fetch_codex_reset_cards(
    home: Path | None = None,
    *,
    provider_id: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Read raw Codex rate-limit reset credits for a resolved Codex account home."""
    target_home = _resolve_codex_home(home, provider_id)
    if target_home is None:
        return [], _tr("未找到可用的 Codex 登录凭证", "No usable Codex login credentials found")
    status, payload = codex_authorized_json(CODEX_RESET_CARDS_URL, home=target_home)
    if status == 0:
        return [], _tr("未找到可用的 Codex 登录凭证", "No usable Codex login credentials found")
    if status != 200 or not isinstance(payload, dict):
        return [], _tr(
            f"读取重置卡失败：{codex_error_text(status, payload)}",
            f"Could not load reset credits: {codex_error_text(status, payload)}",
        )
    credits = [item for item in (payload.get("credits") or []) if isinstance(item, dict)]
    return credits, None


def consume_codex_reset_card(
    card_id: str,
    *,
    home: Path | None = None,
    request_id: str | None = None,
    provider_id: str | None = None,
) -> tuple[bool, str]:
    """Redeem one Codex reset credit for a resolved Codex account home."""
    card_id = str(card_id).strip()
    if not card_id:
        return False, _tr("没有选中任何重置卡", "No reset credit selected")
    target_home = _resolve_codex_home(home, provider_id)
    if target_home is None:
        return False, _tr("未找到可用的 Codex 登录凭证", "No usable Codex login credentials found")
    body = {
        "credit_id": card_id,
        "redeem_request_id": request_id or uuid.uuid4().hex,
    }
    status, payload = codex_authorized_json(
        CODEX_RESET_CONSUME_URL,
        home=target_home,
        method="POST",
        body=body,
    )
    if status == 0:
        return False, _tr("未找到可用的 Codex 登录凭证", "No usable Codex login credentials found")
    if status in (200, 201, 202, 204):
        return True, _tr("重置卡已使用，用量窗口已重置。", "Reset redeemed. Usage windows have been reset.")
    return False, _tr(
        f"使用失败：{codex_error_text(status, payload)}",
        f"Redemption failed: {codex_error_text(status, payload)}",
    )


def _iso_timestamp(value: Any) -> str | None:
    """Normalise a card timestamp to ISO-8601 so parse_iso() can read it.

    The endpoint has shipped both epoch seconds and RFC 3339 strings, so accept
    either rather than dropping a card because of its timestamp encoding.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if parse_iso(text) is not None:
        return text
    try:
        return datetime.fromtimestamp(float(text), tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _iso_epoch(ts: str | None, default: float) -> float:
    dt = parse_iso(ts)
    if dt is None:
        return default
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def extract_grok_token(auth: Any) -> str | None:
    """Prefer SuperGrok OIDC entry (https://auth.x.ai::…), then legacy session."""
    if auth is None or not isinstance(auth, dict):
        return None
    top = auth.get("access_token")
    if isinstance(top, str) and top:
        return top

    oidc = None
    legacy = None
    for scope, value in auth.items():
        if not isinstance(value, dict):
            continue
        key = value.get("key")
        if not isinstance(key, str) or not key:
            continue
        s = str(scope)
        if s.startswith("https://auth.x.ai::"):
            oidc = key
        elif s == "https://accounts.x.ai/sign-in" or "/sign-in" in s:
            legacy = key
    return oidc or legacy


def read_grok_token() -> str | None:
    path = GROK_HOME / "auth.json"
    if path.exists():
        try:
            file_tok = extract_grok_token(json.loads(path.read_text(encoding="utf-8")))
            if file_tok:
                return file_tok
        except Exception:
            pass
    env = (os.environ.get("GROK_API_KEY") or os.environ.get("GROK_TOKEN") or "").strip()
    return env or None


def _grok_credential_material() -> str | None:
    return read_grok_token()


def _read_varint(buf: bytes, index: int) -> tuple[int | None, int]:
    value = 0
    shift = 0
    i = index
    while i < len(buf) and shift < 64:
        byte = buf[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            return value, i
        shift += 7
    return None, i


def _scan_protobuf(
    buf: bytes,
    depth: int,
    path: list[int],
    order: int,
    fixed32: list[tuple[list[int], float, int]],
    varints: list[tuple[list[int], int]],
) -> int:
    """Heuristic protobuf walker (ported from CC Switch / CodexBar)."""
    import struct as _struct

    i = 0
    next_order = order
    n = len(buf)
    while i < n:
        start = i
        key, i = _read_varint(buf, i)
        if key is None or key == 0:
            i = start + 1
            continue
        field_number = key >> 3
        wire_type = key & 0x07
        field_path = path + [field_number]

        if wire_type == 0:
            val, i = _read_varint(buf, i)
            if val is None:
                i = start + 1
                continue
            varints.append((field_path, val))
        elif wire_type == 1:
            if i + 8 > n:
                return next_order
            i += 8
        elif wire_type == 2:
            length, i = _read_varint(buf, i)
            if length is None or i + length > n:
                i = start + 1
                continue
            end = i + length
            if depth < 4:
                next_order = _scan_protobuf(
                    buf[i:end], depth + 1, field_path, next_order, fixed32, varints
                )
            i = end
        elif wire_type == 5:
            if i + 4 > n:
                return next_order
            bits = _struct.unpack_from("<I", buf, i)[0]
            value = _struct.unpack("<f", _struct.pack("<I", bits))[0]
            fixed32.append((field_path, value, next_order))
            next_order += 1
            i += 4
        else:
            i = start + 1
    return next_order


def _grpc_web_data_frames(data: bytes) -> list[bytes]:
    frames: list[bytes] = []
    i = 0
    while i < len(data):
        if i + 5 > len(data):
            return []
        flags = data[i]
        length = int.from_bytes(data[i + 1 : i + 5], "big")
        start = i + 5
        end = start + length
        if end > len(data):
            return []
        if flags & 0x80 == 0:
            frames.append(data[start:end])
        i = end
    return frames


def _parse_grok_billing_payload(data: bytes, now_secs: int) -> tuple[float, str | None]:
    """Return (used_percent, resets_at_iso). Raises ValueError on failure."""
    payloads = _grpc_web_data_frames(data)
    if not payloads and data:
        # bare protobuf fallback
        first = data[0]
        if (first >> 3) > 0 and (first & 0x07) in (0, 1, 2, 5):
            payloads = [data]
    if not payloads:
        raise ValueError("empty billing payload")

    fixed32: list[tuple[list[int], float, int]] = []
    varints: list[tuple[list[int], int]] = []
    for payload in payloads:
        _scan_protobuf(payload, 0, [], 0, fixed32, varints)

    percent_cands = [
        (path, value, order)
        for path, value, order in fixed32
        if path
        and path[-1] == 1
        and value == value  # not NaN
        and 0.0 <= value <= 100.0
    ]
    percent_cands.sort(key=lambda x: (len(x[0]), x[2]))
    parsed_percent = percent_cands[0][1] if percent_cands else None

    reset_cands = [
        (path, value)
        for path, value in varints
        if 1_700_000_000 <= value <= 2_100_000_000 and value > now_secs
    ]
    reset_ts = None
    preferred = [v for path, v in reset_cands if path == [1, 5, 1]]
    if preferred:
        reset_ts = min(preferred)
    elif reset_cands:
        reset_ts = min(v for _, v in reset_cands)

    has_usage_period = any(
        path[:2] == [1, 6] or (path == [1, 8, 1] and value in (1, 2))
        for path, value in varints
    )
    no_usage_yet = (
        parsed_percent is None
        and not fixed32
        and reset_ts is not None
        and has_usage_period
    )
    if parsed_percent is None and no_usage_yet:
        parsed_percent = 0.0
    if parsed_percent is None:
        raise ValueError("could not locate usage percent")

    resets_at = None
    if reset_ts is not None:
        resets_at = datetime.fromtimestamp(reset_ts, tz=timezone.utc).isoformat()
    return float(parsed_percent), resets_at


GROK_CLI_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
GROK_CLI_SETTINGS_URL = "https://cli-chat-proxy.grok.com/v1/settings"
GROK_CLI_USER_SUBSCRIPTION_URL = "https://cli-chat-proxy.grok.com/v1/user?include=subscription"
_GROK_TIER_SAFE_RE = re.compile(r"[A-Za-z0-9 _+-]{1,64}")


def _extract_grok_tier(payload: Any) -> str | None:
    """Extract a safe subscription tier identifier from a Grok API JSON payload; never return emails or tokens."""
    if not isinstance(payload, dict):
        return None
    blocks: list[dict[str, Any]] = [payload]
    if isinstance(payload.get("config"), dict):
        blocks.append(payload["config"])
    for block in blocks:
        for key in ("subscription_tier_display", "subscription_tier", "subscriptionTier"):
            val = block.get(key)
            if isinstance(val, str):
                cleaned = val.strip()
                if cleaned and "@" not in cleaned and _GROK_TIER_SAFE_RE.fullmatch(cleaned):
                    return cleaned
    return None


def _fetch_grok_subscription_tier(token: str, user_id: str | None = None) -> tuple[str | None, str | None, str | None]:
    """Read the signed-in Grok subscription tier from the account's own CLI proxy endpoints.

    Returns (tier_label, plan_source, error_reason). Never logs or returns tokens or PII.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "X-XAI-Token-Auth": "xai-grok-cli",
        "Accept": "application/json",
        "User-Agent": "usage-float",
    }
    if user_id:
        headers["x-userid"] = str(user_id)
    reasons: list[str] = []
    for url, source, label in (
        (GROK_CLI_SETTINGS_URL, "settings_api", "settings"),
        (GROK_CLI_USER_SUBSCRIPTION_URL, "user_api", "user"),
    ):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
                payload = _parse_grok_error_body(raw)
                if resp.status != 200:
                    reasons.append(f"{label} HTTP {int(resp.status)}")
                    continue
                tier = _extract_grok_tier(payload)
                if tier:
                    return tier, source, None
                reasons.append(f"{label} response omitted tier")
        except urllib.error.HTTPError as e:
            try:
                e.read()
            except Exception:
                pass
            reasons.append(f"{label} HTTP {int(e.code)}")
        except Exception:
            reasons.append(f"{label} request failed")
    return None, None, "; ".join(reasons) if reasons else "subscription tier unavailable"


def read_grok_cli_login() -> dict[str, Any] | None:
    """The Grok CLI's own OIDC login, read only while it is unexpired.

    Never refresh or rewrite ~/.grok/auth.json here: a refresh token is single-use,
    so refreshing alongside the CLI could sign it out. The CLI refreshes on its own use.
    """
    try:
        doc = json.loads((GROK_HOME / "auth.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    for scope, entry in doc.items():
        if str(scope).startswith("https://auth.x.ai::") and isinstance(entry, dict)                 and isinstance(entry.get("key"), str) and entry["key"]:
            try:
                expires = datetime.fromisoformat(str(entry.get("expires_at") or "").replace("Z", "+00:00"))
            except ValueError:
                return entry
            return entry if expires.tzinfo is None or expires > datetime.now(timezone.utc) else None
    return None


def parse_grok_cli_billing(payload: Any) -> tuple[float, str | None]:
    """Return (used_percent, resets_at_iso) from the CLI billing JSON. Raises ValueError."""
    config = payload.get("config") if isinstance(payload, dict) else None
    if not isinstance(config, dict):
        raise ValueError("missing Grok billing config")
    period = config.get("currentPeriod") if isinstance(config.get("currentPeriod"), dict) else {}
    percent = config.get("creditUsagePercent")
    if percent is None:
        if period.get("end"):
            percent = 0.0  # proto3 omits a zero float; trust it only on the current-period shape
        else:
            limit = (config.get("monthlyLimit") or {}).get("val")
            used = (config.get("used") or {}).get("val", 0)
            if not isinstance(limit, (int, float)) or limit <= 0 or not isinstance(used, (int, float)):
                raise ValueError("unknown Grok allowance")
            percent = 100.0 * used / limit
    if isinstance(percent, bool) or not isinstance(percent, (int, float)) or percent != percent or percent < 0:
        raise ValueError("invalid Grok usage percent")
    reset = period.get("end") or config.get("billingPeriodEnd")
    try:
        resets_at = datetime.fromisoformat(str(reset).replace("Z", "+00:00")).isoformat() if reset else None
    except ValueError:
        resets_at = None
    return min(100.0, float(percent)), resets_at


def _parse_grok_error_body(raw: bytes | str | None) -> Any:
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    return _parse_json_body(text)


def _fetch_grok_cli_billing_with_payload() -> tuple[tuple[float, str | None] | None, Any, dict[str, Any] | None, int | None, str | None]:
    """Return (parsed_result, raw_payload, login_entry, error_status, error_code)."""
    login = read_grok_cli_login()
    if login is None:
        return None, None, None, None, None
    req = urllib.request.Request(
        GROK_CLI_BILLING_URL,
        headers={
            "Authorization": f"Bearer {login['key']}",
            "X-XAI-Token-Auth": "xai-grok-cli",
            "x-userid": str(login.get("user_id") or ""),
            "Accept": "application/json",
            "User-Agent": "usage-float",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            payload = _parse_grok_error_body(raw)
            if resp.status != 200:
                return None, None, login, int(resp.status), provider_error_code(payload)
            return parse_grok_cli_billing(payload), payload, login, None, None
    except urllib.error.HTTPError as e:
        raw = None
        try:
            raw = e.read()
        except Exception:
            pass
        return None, None, login, int(e.code), provider_error_code(_parse_grok_error_body(raw))
    except Exception:
        return None, None, login, 0, None


def _fetch_grok_cli_billing_detailed() -> tuple[tuple[float, str | None] | None, int | None, str | None]:
    """Return (parsed_result, error_status, error_code). (None, None, None) when no unexpired CLI login exists."""
    parsed, _, _, status, code = _fetch_grok_cli_billing_with_payload()
    return parsed, status, code


def fetch_grok_cli_billing() -> tuple[float, str | None] | None:
    """Usage from the Grok CLI's billing endpoint, or None so the caller falls back."""
    parsed, _, _ = _fetch_grok_cli_billing_detailed()
    return parsed


def _grok_failure_rank(status: int | None, code: str | None) -> tuple[int, int, int, int]:
    return (
        1 if status in (401, 402, 403) else 0,
        1 if (isinstance(status, int) and status > 0 and status != 200) else 0,
        1 if code else 0,
        1 if status is not None else 0,
    )


def fetch_grok_usage() -> ProviderUsage:
    """Grok credit window and subscription tier: the Grok CLI endpoints, else grok.com gRPC-web."""
    cli, cli_payload, cli_login, cli_status, cli_code = _fetch_grok_cli_billing_with_payload()
    if cli is not None:
        pct, resets_at = cli
        inline_tier = _extract_grok_tier(cli_payload)
        if inline_tier:
            tier, plan_source, tier_err = inline_tier, "usage_api", None
        elif cli_login and cli_login.get("key"):
            tier, plan_source, tier_err = _fetch_grok_subscription_tier(
                str(cli_login["key"]), str(cli_login.get("user_id") or "") or None
            )
        else:
            tier, plan_source, tier_err = None, None, "missing Grok CLI token for tier lookup"
        result = ProviderUsage(
            provider_id="grok",
            display_name="grok",
            plan_label=tier,
            plan_source=plan_source,
            windows=[UsageWindow(id="credits", label=_tr("额度", 'Quota'), used_pct=pct, resets_at=resets_at)],
            available=True,
            fetched_at=datetime.now(timezone.utc).isoformat(),
            usage_details={"plan_tier_error": tier_err} if tier is None and tier_err else None,
        )
        cache_put_provider(result)
        return result
    token = read_grok_token()
    if not token:
        if cli_status is None:
            return ProviderUsage(
                provider_id="grok",
                display_name="grok",
                available=False,
                error="更新失败",
            )
        err = (
            _tr("登录凭据需由 Grok 更新", "Login credentials must be updated by Grok")
            if cli_status in (401, 403)
            else "更新失败"
        )
        return stale_provider_usage("grok", err, status=cli_status, code=cli_code)

    # Empty gRPC-web frame: flags(1) + big-endian length(4) = 5 zero bytes
    body = bytes(5)
    req = urllib.request.Request(
        GROK_BILLING_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Origin": "https://grok.com",
            "Referer": "https://grok.com/?_s=usage",
            "Accept": "*/*",
            "Content-Type": "application/grpc-web+proto",
            "x-grpc-web": "1",
            "x-user-agent": "connect-es/2.1.1",
            "User-Agent": "usage-float",
        },
    )
    web_status: int | None = None
    web_code: str | None = None
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            status = resp.status
            raw = resp.read()
            grpc_status = resp.headers.get("grpc-status")
            if status != 200:
                web_status = int(status)
                web_code = provider_error_code(_parse_grok_error_body(raw))
            elif grpc_status and grpc_status not in ("0", "00"):
                web_status = 401 if grpc_status == "16" else (403 if grpc_status == "7" else 0)
                web_code = provider_error_code(_parse_grok_error_body(raw))
            else:
                pct, resets_at = _parse_grok_billing_payload(raw, int(time.time()))
                tier, plan_source, tier_err = _fetch_grok_subscription_tier(token)
                result = ProviderUsage(
                    provider_id="grok",
                    display_name="grok",
                    plan_label=tier,
                    plan_source=plan_source,
                    windows=[
                        UsageWindow(
                            id="credits",
                            label=_tr("额度", 'Quota'),
                            used_pct=max(0.0, min(100.0, pct)),
                            resets_at=resets_at,
                        )
                    ],
                    available=True,
                    fetched_at=datetime.now(timezone.utc).isoformat(),
                    usage_details={"plan_tier_error": tier_err} if tier is None and tier_err else None,
                )
                cache_put_provider(result)
                return result
    except urllib.error.HTTPError as e:
        raw = None
        try:
            raw = e.read()
        except Exception:
            pass
        web_status = int(e.code)
        web_code = provider_error_code(_parse_grok_error_body(raw))
    except Exception:
        web_status = 0
        web_code = None

    if _grok_failure_rank(cli_status, cli_code) >= _grok_failure_rank(web_status, web_code):
        final_status, final_code = cli_status, cli_code
    else:
        final_status, final_code = web_status, web_code
    err = (
        _tr("登录凭据需由 Grok 更新", "Login credentials must be updated by Grok")
        if final_status in (401, 403)
        else "更新失败"
    )
    return stale_provider_usage("grok", err, status=final_status, code=final_code)


ANTIGRAVITY_MAX_OUTPUT = 1024 * 1024


# agy refreshes its own OAuth token a few seconds after expiry. A /usage call
# in that gap is a 401, so the retry waits long enough for the new token.
ANTIGRAVITY_USAGE_RETRY_SECONDS = 8


ANTIGRAVITY_BUCKETS = {
    "gemini-weekly": "gemini 7d",
    "gemini-5h": "gemini 5h",
    "3p-weekly": "claude 7d",
    "3p-5h": "claude 5h",
}


def find_antigravity_cli() -> Path | None:
    """Also find the Windows installer location when agy is absent from PATH."""
    override = os.environ.get("ANTIGRAVITY_CLI_PATH")
    if override is not None:
        candidate = Path(override) if override.strip() else None
        return candidate.resolve() if candidate is not None and candidate.is_file() else None
    binary = shutil.which("agy")
    if binary:
        return Path(binary)
    local_app_data = Path(os.environ.get("LOCALAPPDATA", HOME / "AppData" / "Local"))
    for candidate in (local_app_data / "agy" / "bin" / "agy.exe", HOME / ".local" / "bin" / "agy"):
        if candidate.is_file():
            return candidate
    return None


def _antigravity_windows_job() -> tuple[Any, Any]:
    """Own CLI descendants, including plugin helpers, until this query ends."""
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
            ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
            ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
            ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
            ("scheduling", wintypes.DWORD),
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("basic", BasicLimits), ("io_counters", ctypes.c_ulonglong * 6),
            ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
            ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateJobObject.restype = wintypes.BOOL
    kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    kernel.QueryInformationJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        error = ctypes.WinError(ctypes.get_last_error())
        kernel.CloseHandle(handle)
        raise error
    return kernel, handle


def _close_antigravity_windows_job(kernel: Any, job: Any) -> None:
    import ctypes
    from ctypes import wintypes

    class Accounting(ctypes.Structure):
        _fields_ = [
            ("times", ctypes.c_longlong * 4), ("page_faults", wintypes.DWORD),
            ("total_processes", wintypes.DWORD), ("active_processes", wintypes.DWORD),
            ("terminated_processes", wintypes.DWORD),
        ]

    try:
        kernel.TerminateJobObject(job, 1)
        # Termination is asynchronous. Wait before removing their working directory.
        accounting = Accounting()
        deadline = time.monotonic() + 5
        while kernel.QueryInformationJobObject(job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
            if accounting.active_processes == 0 or time.monotonic() >= deadline:
                break
            time.sleep(0.02)
    finally:
        kernel.CloseHandle(job)


def _antigravity_env() -> dict[str, str]:
    """agy (Go) ignores the Windows system proxy; pass it on when no proxy variable is set."""
    env = os.environ.copy()
    if any(env.get(name) for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")):
        return env
    proxies = urllib.request.getproxies()
    proxy = proxies.get("https") or proxies.get("http")
    if proxy:
        env["HTTPS_PROXY"] = proxy
        env.setdefault("HTTP_PROXY", proxies.get("http") or proxy)
    return env


def _run_antigravity_cli(binary: Path, arguments: list[str], *, timeout: float) -> str:
    """Bound runtime/output and close this query's process tree on every exit."""
    process = None
    kernel = job = None
    resources = ExitStack()
    try:
        if os.name == "nt":
            kernel, job = _antigravity_windows_job()
        # An empty directory avoids loading any project's instructions or MCP config.
        directory_resource = tempfile.TemporaryDirectory(prefix="usage-float-agy-")
        resources.callback(_cleanup_antigravity_directory, directory_resource)
        directory = directory_resource.name
        output = resources.enter_context(tempfile.TemporaryFile())
        process = subprocess.Popen(
            [str(binary), *arguments], cwd=directory, env=_antigravity_env(),
            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
        if kernel is not None and not kernel.AssignProcessToJobObject(job, int(process._handle)):
            import ctypes
            raise ctypes.WinError(ctypes.get_last_error())
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if os.fstat(output.fileno()).st_size > ANTIGRAVITY_MAX_OUTPUT:
                raise ValueError("Antigravity usage response exceeds output limit")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(str(binary), timeout)
            try:
                process.wait(timeout=min(0.1, remaining))
            except subprocess.TimeoutExpired:
                pass
        if process.returncode != 0:
            raise ValueError("Antigravity CLI query failed")
        output.seek(0)
        raw = output.read(ANTIGRAVITY_MAX_OUTPUT + 1)
        if len(raw) > ANTIGRAVITY_MAX_OUTPUT:
            raise ValueError("Antigravity usage response exceeds output limit")
        return raw.decode("utf-8-sig")
    finally:
        if kernel is not None and job is not None:
            _close_antigravity_windows_job(kernel, job)
        elif process is not None and os.name != "nt":
            import signal
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
        resources.close()


def _cleanup_antigravity_directory(directory: tempfile.TemporaryDirectory) -> None:
    # Windows can release a terminated child's cwd handle just after the job's
    # active-process count reaches zero. Retry that short filesystem delay.
    for attempt in range(50):
        try:
            directory.cleanup()
            return
        except PermissionError:
            if attempt == 49:
                raise
            time.sleep(0.02)


def _parse_antigravity_usage(payload: Any) -> list[UsageWindow]:
    """Read quota buckets, never the prose response or inference token counts."""
    if not isinstance(payload, dict) or payload.get("status") != "SUCCESS":
        raise ValueError("Antigravity usage query did not succeed")
    command = payload.get("command")
    if not isinstance(command, dict) or command.get("name") != "usage":
        raise ValueError("Antigravity did not return the usage command")
    data = command.get("data")
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, list):
        raise ValueError("Antigravity quota groups are missing")
    buckets: dict[str, UsageWindow] = {}
    for group in groups:
        group_buckets = group.get("buckets") if isinstance(group, dict) else None
        if not isinstance(group_buckets, list):
            continue
        for bucket in group_buckets:
            if not isinstance(bucket, dict) or bucket.get("disabled") is True:
                continue
            bucket_id = bucket.get("id")
            if not isinstance(bucket_id, str) or bucket_id not in ANTIGRAVITY_BUCKETS:
                continue
            fraction = bucket.get("remaining_fraction")
            if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
                continue
            if not math.isfinite(fraction) or not 0 <= fraction <= 1:
                continue
            reset = bucket.get("reset_time")
            reset = reset if isinstance(reset, str) and parse_iso(reset) is not None else None
            window = UsageWindow(bucket_id, ANTIGRAVITY_BUCKETS[bucket_id], (1 - fraction) * 100, reset)
            if bucket_id not in buckets or window.used_pct > buckets[bucket_id].used_pct:
                buckets[bucket_id] = window
    if not buckets:
        raise ValueError("Antigravity returned no supported quota buckets")
    return [buckets[bucket_id] for bucket_id in ANTIGRAVITY_BUCKETS if bucket_id in buckets]


def fetch_antigravity_usage() -> ProviderUsage:
    try:
        binary = find_antigravity_cli()
        if binary is None:
            raise ValueError("Antigravity CLI is not installed")
        version = _run_antigravity_cli(binary, ["--version"], timeout=5)
        match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", version)
        # Earlier CLIs can treat /usage as a model prompt instead of a command.
        if match is None or tuple(map(int, match.groups())) < (1, 1, 11):
            raise ValueError("Antigravity CLI 1.1.11 or later is required")
    except Exception:
        return stale_provider_usage("antigravity", "更新失败")

    usage_args = ["-p", "/usage", "--output-format", "json"]
    for attempt in range(2):
        try:
            raw = _run_antigravity_cli(binary, usage_args, timeout=60)
            windows = _parse_antigravity_usage(json.loads(raw))
            break
        except Exception:
            if attempt == 0:
                time.sleep(ANTIGRAVITY_USAGE_RETRY_SECONDS)
                continue
            return stale_provider_usage("antigravity", "更新失败")
    result = ProviderUsage(
        "antigravity", "agy", windows=windows,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )
    cache_put_provider(result)
    return result


@dataclass(frozen=True)
class LiteLLMProxyConfig:
    provider_id: str
    display_name: str
    api_key: str
    openai_base: str
    embed_model: str = LLM_PROXY_EMBED_MODEL
    chat_model: str = LLM_PROXY_CHAT_FALLBACK_MODEL
    budget_duration: str | None = None
    timezone_name: str | None = None


def _llm_proxy_openai_base(endpoint: str) -> str:
    base = (endpoint or LLM_PROXY_DEFAULT_ENDPOINT).strip().rstrip("/")
    if not base:
        return ""
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return base


def _extract_litellm_proxy_entry(pid: str, spec: dict[str, Any]) -> LiteLLMProxyConfig | None:
    if not isinstance(spec, dict) or spec.get("enabled") is False:
        return None
    sub = spec.get("litellm") if isinstance(spec.get("litellm"), dict) else spec
    kind = str(spec.get("backend_kind") or sub.get("backend_kind") or spec.get("probe") or "").strip().lower()
    endpoint = str(
        sub.get("endpoint")
        or sub.get("base_url")
        or spec.get("endpoint")
        or spec.get("base_url")
        or ""
    ).strip()
    key = str(sub.get("api_key") or spec.get("api_key") or "").strip()
    key_env = str(sub.get("api_key_env") or spec.get("api_key_env") or "").strip()
    if not key and key_env:
        key = str(os.environ.get(key_env) or "").strip()
    endpoint_env = str(sub.get("endpoint_env") or spec.get("endpoint_env") or "").strip()
    if not endpoint and endpoint_env:
        endpoint = str(os.environ.get(endpoint_env) or "").strip()
    if not endpoint and kind not in {"litellm", "proxy", "api-proxy"} and not isinstance(spec.get("litellm"), dict):
        return None
    base = _llm_proxy_openai_base(endpoint)
    if not key or not base:
        return None
    label = str(spec.get("label") or sub.get("label") or pid).strip() or pid
    PROVIDER_LABELS.setdefault(pid, label)
    return LiteLLMProxyConfig(
        provider_id=pid,
        display_name=pid,
        api_key=key,
        openai_base=base,
        embed_model=str(sub.get("embed_model") or spec.get("embed_model") or LLM_PROXY_EMBED_MODEL).strip() or LLM_PROXY_EMBED_MODEL,
        chat_model=str(sub.get("chat_model") or sub.get("probe_model") or spec.get("chat_model") or spec.get("probe_model") or LLM_PROXY_CHAT_FALLBACK_MODEL).strip() or LLM_PROXY_CHAT_FALLBACK_MODEL,
        budget_duration=str(sub.get("budget_duration") or spec.get("budget_duration") or "").strip() or None,
        timezone_name=str(sub.get("timezone") or spec.get("timezone") or "").strip() or None,
    )


def discover_litellm_proxies() -> dict[str, LiteLLMProxyConfig]:
    """Discover LiteLLM/OpenAI-compatible proxy accounts configured in the router files, Paseo or the usage config."""
    proxies: dict[str, LiteLLMProxyConfig] = {}
    router_files = [
        Path(os.environ.get("ROUTE_MANAGER_ACCOUNTS_OVERRIDE") or STATE_DIR / "accounts-override.json"),
        STATE_DIR / "provider-inventory.json",
        Path(__file__).resolve().parent.parent / "references" / "accounts.json",
    ]
    for path in router_files:
        try:
            if not path.is_file():
                continue
            doc = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(doc, dict):
                continue
            for section in ("litellm_proxies", "accounts"):
                block = doc.get(section)
                if not isinstance(block, dict):
                    continue
                for pid, spec in block.items():
                    if pid in proxies or not isinstance(pid, str):
                        continue
                    entry = _extract_litellm_proxy_entry(pid, spec)
                    if entry is not None:
                        proxies[pid] = entry
        except Exception:
            continue

    for pid, spec in _read_paseo_providers().items():
        if pid in proxies or spec.get("enabled") is False:
            continue
        if is_claude_provider(pid) and claude_magpie_source(pid) is not None:
            continue
        env = spec.get("env") if isinstance(spec.get("env"), dict) else {}
        endpoint = str(
            env.get("LLM_PROXY_ENDPOINT")
            or env.get("LITELLM_BASE_URL")
            or env.get("OPENAI_BASE_URL")
            or ""
        ).strip()
        key = str(
            env.get("LLM_PROXY_API_KEY")
            or env.get("LITELLM_API_KEY")
            or env.get("OPENAI_API_KEY")
            or ""
        ).strip()
        if endpoint and key and "api.openai.com" not in endpoint.lower():
            entry = _extract_litellm_proxy_entry(
                pid,
                {
                    "backend_kind": "litellm",
                    "endpoint": endpoint,
                    "api_key": key,
                    "label": spec.get("label") or pid,
                },
            )
            if entry is not None:
                proxies[pid] = entry

    env_key = str(os.environ.get("LLM_PROXY_API_KEY") or "").strip()
    env_endpoint = str(os.environ.get("LLM_PROXY_ENDPOINT") or "").strip()
    cfg_key = ""
    cfg_endpoint = ""
    cfg_pid = str(os.environ.get("LLM_PROXY_PROVIDER_ID") or "").strip()
    try:
        cfg = read_usage_config()
        cfg_key = str(cfg.get("llm_proxy_api_key") or "").strip()
        cfg_endpoint = str(cfg.get("llm_proxy_endpoint") or "").strip()
        cfg_pid = cfg_pid or str(cfg.get("llm_proxy_provider_id") or "").strip()
        extra_proxies = cfg.get("litellm_proxies")
        if isinstance(extra_proxies, dict):
            for pid, spec in extra_proxies.items():
                if isinstance(pid, str) and pid not in proxies and isinstance(spec, dict):
                    entry = _extract_litellm_proxy_entry(pid, spec)
                    if entry is not None:
                        proxies[pid] = entry
    except Exception:
        pass
    legacy_key = env_key or cfg_key
    legacy_endpoint = env_endpoint or cfg_endpoint
    if legacy_key and legacy_endpoint:
        target_pid = cfg_pid or "api-proxy"
        if target_pid not in proxies:
            base = _llm_proxy_openai_base(legacy_endpoint)
            if base:
                PROVIDER_LABELS.setdefault(target_pid, "LLM Proxy" if target_pid == "api-proxy" else target_pid)
                proxies[target_pid] = LiteLLMProxyConfig(
                    provider_id=target_pid,
                    display_name=target_pid,
                    api_key=legacy_key,
                    openai_base=base,
                )
    return proxies


def read_llm_proxy_credentials(provider_id: str = "api-proxy") -> tuple[str, str] | tuple[None, None]:
    """Return (api_key, openai_base) from router/Paseo discovery, env, or the usage config."""
    discovered = discover_litellm_proxies()
    if provider_id in discovered:
        proxy = discovered[provider_id]
        return proxy.api_key, proxy.openai_base
    key = str(os.environ.get("LLM_PROXY_API_KEY") or "").strip()
    endpoint = str(os.environ.get("LLM_PROXY_ENDPOINT") or "").strip()
    if not key or not endpoint:
        cfg = read_usage_config()
        key = key or str(cfg.get("llm_proxy_api_key") or "").strip()
        endpoint = endpoint or str(cfg.get("llm_proxy_endpoint") or "").strip()
    if not key:
        return None, None
    base = _llm_proxy_openai_base(endpoint)
    if not base:
        return None, None
    return key, base


def _header_float(headers: dict[str, str], name: str) -> float | None:
    raw = headers.get(name)
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _spend_looks_like_this_request(spend: float | None, cost: float | None) -> bool:
    """True when LiteLLM has not flushed cumulative key spend yet."""
    if spend is None:
        return False
    if cost is None:
        return spend <= 1e-5
    return spend <= max(cost * 2.0, 1e-5)


def parse_llm_proxy_spend_headers(
    headers: dict[str, str],
) -> tuple[float | None, float | None, float | None]:
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    spend = _header_float(lowered, "x-litellm-key-spend")
    budget = _header_float(lowered, "x-litellm-key-max-budget")
    cost = _header_float(lowered, "x-litellm-response-cost")
    return spend, budget, cost


def _parse_utc_offset(text: str) -> timedelta | None:
    raw = text.strip().upper().replace(" ", "")
    if raw in {"UTC", "GMT", "Z"}:
        return timedelta(0)
    if raw.startswith("UTC"):
        raw = raw[3:]
    if not raw or raw[0] not in "+-":
        return None
    sign = 1 if raw[0] == "+" else -1
    rest = raw[1:]
    hours = 0
    minutes = 0
    if ":" in rest:
        hour_text, minute_text = rest.split(":", 1)
        if not hour_text.isdigit() or not minute_text.isdigit():
            return None
        hours, minutes = int(hour_text), int(minute_text)
    elif rest.isdigit():
        if len(rest) <= 2:
            hours = int(rest)
        elif len(rest) == 4:
            hours, minutes = int(rest[:2]), int(rest[2:])
        else:
            return None
    else:
        return None
    if hours > 14 or minutes > 59:
        return None
    return timedelta(hours=sign * hours, minutes=sign * minutes)


def _normalize_llm_proxy_timezone(value: Any) -> str:
    text = str(value or "").strip() or LLM_PROXY_TIMEZONE_DEFAULT
    aliases = {
        "cst": "UTC+8",
        "prc": "UTC+8",
        "asia/shanghai": "UTC+8",
        "asia/hong_kong": "UTC+8",
        "asia/taipei": "UTC+8",
        "asia/singapore": "UTC+8",
        "asia/tokyo": "UTC+9",
        "asia/seoul": "UTC+9",
        "us/eastern": "UTC-5",
        "us/pacific": "UTC-8",
        "europe/london": "UTC",
    }
    mapped = aliases.get(text.lower())
    if mapped:
        return mapped
    offset = _parse_utc_offset(text)
    if offset is not None:
        total_minutes = int(offset.total_seconds() // 60)
        if total_minutes == 0:
            return "UTC"
        sign = "+" if total_minutes > 0 else "-"
        hours, minutes = divmod(abs(total_minutes), 60)
        if minutes:
            return f"UTC{sign}{hours:02d}:{minutes:02d}"
        return f"UTC{sign}{hours}"
    return text


def _llm_proxy_zoneinfo(name: str | None) -> timezone | ZoneInfo:
    text = _normalize_llm_proxy_timezone(name)
    offset = _parse_utc_offset(text)
    if offset is not None:
        return timezone(offset)
    try:
        return ZoneInfo(text)
    except Exception:
        return timezone(timedelta(hours=8))


def _normalize_llm_proxy_budget_duration(raw: str | None) -> str:
    text = (raw or "").strip().lower()
    aliases = {
        "hourly": "1h",
        "daily": "1d",
        "weekly": "7d",
        "monthly": "30d",
        "24h": "1d",
        "1w": "7d",
        "1mo": "30d",
    }
    return aliases.get(text, text)


def _parse_llm_proxy_duration(raw: str | None) -> tuple[int, str] | None:
    text = _normalize_llm_proxy_budget_duration(raw)
    if not text:
        return None
    index = 0
    while index < len(text) and text[index].isdigit():
        index += 1
    if index == 0 or index == len(text):
        return None
    try:
        value = int(text[:index])
    except ValueError:
        return None
    unit = text[index:]
    if unit not in {"s", "m", "h", "d", "w", "mo"}:
        return None
    return value, unit


def next_llm_proxy_budget_reset(
    duration: str | None,
    *,
    now: datetime | None = None,
    timezone_name: str | None = None,
) -> datetime | None:
    """Next LiteLLM-style budget reset (1d midnight, 7d Monday, 30d 1st of month)."""
    parsed = _parse_llm_proxy_duration(duration)
    if parsed is None:
        return None
    value, unit = parsed
    tzinfo = _llm_proxy_zoneinfo(timezone_name)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(tzinfo)
    midnight = current.replace(hour=0, minute=0, second=0, microsecond=0)

    def at_midnight(moment: datetime) -> datetime:
        return moment.replace(hour=0, minute=0, second=0, microsecond=0)

    if unit == "d" and value == 1:
        candidate = midnight
        return candidate + timedelta(days=1) if candidate <= current else candidate
    if (unit == "d" and value == 7) or (unit == "w" and value == 1):
        days_until_monday = (7 - current.weekday()) % 7
        candidate = midnight + timedelta(days=days_until_monday)
        return candidate + timedelta(days=7) if candidate <= current else candidate
    if (unit == "d" and value == 30) or (unit == "mo" and value == 1):
        candidate = midnight.replace(day=1)
        if candidate <= current:
            if candidate.month == 12:
                candidate = candidate.replace(year=candidate.year + 1, month=1)
            else:
                candidate = candidate.replace(month=candidate.month + 1)
        return candidate
    if unit == "h":
        return current.replace(minute=0, second=0, microsecond=0) + timedelta(hours=value)
    if unit == "m":
        return current.replace(second=0, microsecond=0) + timedelta(minutes=value)
    if unit == "s":
        return current.replace(microsecond=0) + timedelta(seconds=value)
    if unit == "d":
        return at_midnight(midnight + timedelta(days=value))
    if unit == "w":
        return at_midnight(midnight + timedelta(weeks=value))
    return None


def _parse_llm_proxy_reset_timestamp(raw: str | None) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        stamp = float(text)
        if stamp > 1e12:
            stamp /= 1000.0
        if stamp > 1e9:
            return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat()
    except ValueError:
        pass
    parsed = parse_iso(text)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def _llm_proxy_budget_settings() -> tuple[str, str]:
    duration = str(os.environ.get("LLM_PROXY_BUDGET_DURATION") or "").strip()
    timezone_name = str(os.environ.get("LLM_PROXY_TIMEZONE") or "").strip()
    data = read_usage_config()
    duration = duration or str(data.get("llm_proxy_budget_duration") or "").strip()
    timezone_name = timezone_name or str(data.get("llm_proxy_timezone") or "").strip()
    return (
        duration or LLM_PROXY_BUDGET_DURATION_DEFAULT,
        _normalize_llm_proxy_timezone(timezone_name or LLM_PROXY_TIMEZONE_DEFAULT),
    )


def resolve_llm_proxy_resets_at(
    headers: dict[str, str] | None = None,
    *,
    now: datetime | None = None,
    duration: str | None = None,
    timezone_name: str | None = None,
) -> str | None:
    lowered = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    for key in LLM_PROXY_RESET_HEADERS:
        parsed = _parse_llm_proxy_reset_timestamp(lowered.get(key))
        if parsed:
            return parsed
    header_duration = None
    for key in LLM_PROXY_DURATION_HEADERS:
        raw = lowered.get(key)
        if raw:
            header_duration = raw
            break
    reset = next_llm_proxy_budget_reset(
        header_duration or duration or LLM_PROXY_BUDGET_DURATION_DEFAULT,
        now=now,
        timezone_name=timezone_name,
    )
    return reset.isoformat() if reset is not None else None


def _round_dollars(value: float) -> int:
    """Half-up dollar rounding (2.5 → 3), not banker's rounding or truncation."""
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_llm_proxy_spend(spend: float, budget: float | None = None) -> str:
    """Compact dollar readout of what is left, e.g. $198/200 after $2 of a $200 budget.

    Every other row reads what is left, so a budgeted key does too; with no budget
    nothing is known to be left and the spend alone is shown ($12)."""
    if budget is None or budget <= 0:
        return f"${_round_dollars(spend)}"
    return f"${_round_dollars(max(0.0, budget - spend))}/{_round_dollars(budget)}"


def _llm_proxy_probe(
    base: str, key: str, *, model: str, kind: str
) -> tuple[int, dict[str, str]]:
    headers = {"Authorization": f"Bearer {key}"}
    if kind == "embeddings":
        status, _payload, resp_headers = http_json_response(
            f"{base}/embeddings",
            method="POST",
            headers=headers,
            body={"model": model, "input": "usage"},
        )
        return status, resp_headers
    status, _payload, resp_headers = http_json_response(
        f"{base}/chat/completions",
        method="POST",
        headers=headers,
        body={
            "model": model,
            "messages": [{"role": "user", "content": "."}],
            "max_tokens": 1,
        },
    )
    return status, resp_headers


def fetch_api_proxy_usage(provider_id: str = "api-proxy") -> ProviderUsage:
    proxy = discover_litellm_proxies().get(provider_id)
    creds = read_llm_proxy_credentials(provider_id) if proxy is None else (proxy.api_key, proxy.openai_base)
    display_name = proxy.display_name if proxy is not None else provider_id
    if creds[0] is None:
        return ProviderUsage(
            provider_id=provider_id,
            display_name=display_name,
            available=False,
            error="更新失败",
        )
    key, base = creds
    embed_model = proxy.embed_model if proxy is not None else LLM_PROXY_EMBED_MODEL
    chat_model = proxy.chat_model if proxy is not None else LLM_PROXY_CHAT_FALLBACK_MODEL
    probes = (
        ("embeddings", embed_model),
        ("chat", chat_model),
    )
    spend: float | None = None
    budget: float | None = None
    headers: dict[str, str] = {}
    for kind, model in probes:
        status, headers = _llm_proxy_probe(base, key, model=model, kind=kind)
        if status != 200:
            continue
        spend, budget, cost = parse_llm_proxy_spend_headers(headers)
        if _spend_looks_like_this_request(spend, cost):
            status, headers = _llm_proxy_probe(base, key, model=model, kind=kind)
            if status == 200:
                spend, budget, cost = parse_llm_proxy_spend_headers(headers)
        if spend is not None:
            break
    if spend is None:
        return ProviderUsage(
            provider_id=provider_id,
            display_name=display_name,
            available=False,
            error="更新失败",
        )

    duration, timezone_name = _llm_proxy_budget_settings()
    if proxy is not None:
        if proxy.budget_duration:
            duration = proxy.budget_duration
        if proxy.timezone_name:
            timezone_name = _normalize_llm_proxy_timezone(proxy.timezone_name)
    resets_at = resolve_llm_proxy_resets_at(
        headers,
        duration=duration,
        timezone_name=timezone_name,
    )

    if budget is None or budget <= 0:
        result = ProviderUsage(
            provider_id=provider_id,
            display_name=display_name,
            summary=format_llm_proxy_spend(spend),
            windows=[
                UsageWindow(
                    id="spend",
                    label=_tr("额度", 'Quota'),
                    used_pct=0.0,
                    resets_at=resets_at,
                )
            ]
            if resets_at
            else [],
            available=True,
            fetched_at=datetime.now(timezone.utc).isoformat(),
            backend_kind="litellm",
            billing="metered",
        )
        cache_put_provider(result)
        return result

    used_pct = max(0.0, min(100.0, (spend / budget) * 100.0))
    result = ProviderUsage(
        provider_id=provider_id,
        display_name=display_name,
        plan_label=f"${budget:.0f}",
        summary=format_llm_proxy_spend(spend, budget),
        windows=[
            UsageWindow(
                id="spend",
                label=_tr("额度", 'Quota'),
                used_pct=used_pct,
                resets_at=resets_at,
            )
        ],
        available=True,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        backend_kind="litellm",
        billing="metered",
    )
    cache_put_provider(result)
    return result


def _pi_agent_home() -> Path:
    return Path(claude_account_paseo_env("pi").get("PI_CODING_AGENT_DIR") or HOME / ".pi" / "agent")


def read_pi_codex_auth(pi_home: Path | None = None) -> tuple[dict[str, Any] | None, Path | None]:
    """Read openai-codex OAuth credentials from ~/.pi/agent/auth.json."""
    home = pi_home if pi_home is not None else _pi_agent_home()
    auth_path = home / "auth.json"
    if not auth_path.is_file():
        return None, None
    try:
        doc = json.loads(auth_path.read_text(encoding="utf-8-sig"))
        if not isinstance(doc, dict):
            return None, None
        codex_oauth = doc.get("openai-codex")
        if isinstance(codex_oauth, dict) and str(codex_oauth.get("access") or "").strip():
            return {
                "tokens": {
                    "access_token": str(codex_oauth.get("access")).strip(),
                    "refresh_token": str(codex_oauth.get("refresh") or "").strip(),
                    "account_id": str(codex_oauth.get("accountId") or "").strip(),
                }
            }, auth_path
    except Exception:
        return None, None
    return None, None


def pi_linked_codex_provider(pi_home: Path | None = None) -> str | None:
    """Return the codex provider_id sharing the same OpenAI accountId as pi, if any."""
    pi_auth, _ = read_pi_codex_auth(pi_home)
    if not pi_auth:
        return None
    pi_account_id = str((pi_auth.get("tokens") or {}).get("account_id") or "").strip()
    if not pi_account_id:
        return None
    for pid, acc in discover_codex_accounts().items():
        cauth, _ = read_codex_auth(acc.home)
        if not cauth:
            continue
        cid = str((cauth.get("tokens") or {}).get("account_id") or "").strip()
        if cid and cid == pi_account_id:
            return pid
    return None


def fetch_pi_usage(provider_id: str = "pi") -> ProviderUsage:
    """Fetch quota usage for pi when signed in via openai-codex OAuth."""
    home = _pi_agent_home()
    pi_auth, auth_path = read_pi_codex_auth(home)
    if not pi_auth:
        return stale_provider_usage(provider_id, "更新失败")
    tokens = pi_auth.get("tokens") or {}
    access_token = str(tokens.get("access_token") or "").strip()
    account_id = str(tokens.get("account_id") or "").strip()
    if not access_token:
        return stale_provider_usage(provider_id, "更新失败")
    headers = {"Authorization": f"Bearer {access_token}"}
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    status, data = http_json(CODEX_USAGE_URL, headers=headers)
    if status == 401 and tokens.get("refresh_token") and auth_path is not None:
        refreshed = refresh_codex_token(str(tokens["refresh_token"]))
        if refreshed and refreshed.get("access_token"):
            access_token = str(refreshed["access_token"])
            try:
                raw_doc = json.loads(auth_path.read_text(encoding="utf-8-sig"))
                if isinstance(raw_doc, dict) and isinstance(raw_doc.get("openai-codex"), dict):
                    raw_doc["openai-codex"]["access"] = access_token
                    if refreshed.get("refresh_token"):
                        raw_doc["openai-codex"]["refresh"] = refreshed["refresh_token"]
                    auth_path.write_text(json.dumps(raw_doc, indent=2), encoding="utf-8")
            except Exception:
                pass
            headers["Authorization"] = f"Bearer {access_token}"
            status, data = http_json(CODEX_USAGE_URL, headers=headers)
    if status != 200 or not isinstance(data, dict):
        error = _tr("登录凭据需由 Pi 更新", "Login credentials must be updated by Pi") if status in (401, 403) else "更新失败"
        return stale_provider_usage(provider_id, error, status=status, code=provider_error_code(data))
    rl = data.get("rate_limit") or {}
    windows: list[UsageWindow] = []
    if isinstance(rl.get("secondary_window"), dict):
        w = rl["secondary_window"]
        windows.append(
            UsageWindow(
                id="secondary",
                label="7d",
                used_pct=float(w.get("used_percent", 0)),
                resets_at=_iso_timestamp(w.get("reset_at")),
            )
        )
    if isinstance(rl.get("primary_window"), dict):
        w = rl["primary_window"]
        windows.append(
            UsageWindow(
                id="primary",
                label="5h",
                used_pct=float(w.get("used_percent", 0)),
                resets_at=_iso_timestamp(w.get("reset_at")),
            )
        )
    if not windows:
        return stale_provider_usage(provider_id, "更新失败", plan=str(data.get("plan_type") or "") or None)
    result = ProviderUsage(
        provider_id=provider_id,
        display_name=provider_id,
        plan_label=data.get("plan_type"),
        windows=windows,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        backend_kind="subscription",
        billing="subscription",
        home=str(home),
    )
    cache_put_provider(result)
    return result


def read_opencode_auth() -> dict[str, Any]:
    """Read configured credentials from ~/.local/share/opencode/auth.json."""
    auth_path = HOME / ".local" / "share" / "opencode" / "auth.json"
    try:
        if auth_path.is_file():
            doc = json.loads(auth_path.read_text(encoding="utf-8-sig"))
            if isinstance(doc, dict):
                return doc
    except Exception:
        pass
    return {}


def provider_available(provider_id: str) -> bool:
    """True when local credentials (or the Antigravity-managed CLI) are present.

    Providers without local credentials are hidden. Antigravity manages its
    secure login internally, so its installed CLI is checked by /usage instead.
    """
    provider_id = provider_fetch_id(provider_id)
    remote = claude_magpie_source(provider_id)
    if remote is not None:
        return bool(remote.valid)
    codex_accounts = discover_codex_accounts()
    account = codex_accounts.get(provider_id)
    if account is not None:
        return read_codex_auth(account.home)[0] is not None
    if provider_id == "claude":
        return read_claude_oauth()[0] is not None
    if is_claude_provider(provider_id):
        return read_claude_oauth(claude_account_home(provider_id))[0] is not None
    if provider_id == "grok":
        return read_grok_token() is not None
    if provider_id in discover_litellm_proxies() or provider_id == "api-proxy":
        return read_llm_proxy_credentials(provider_id)[0] is not None
    if provider_id == "pi":
        return read_pi_codex_auth()[0] is not None
    if provider_id == "opencode":
        return bool(read_opencode_auth())
    if provider_id == "antigravity":
        return find_antigravity_cli() is not None
    return False


def discovered_default_providers() -> list[str]:
    items = list(DEFAULT_PROVIDERS)
    for pid in discover_codex_accounts():
        if pid not in items:
            items.append(pid)
    for pid in discover_claude_homes():
        if pid == "claude":
            continue
        for sub in PROVIDER_DISPLAY_ITEMS.get(pid, (f"{pid}:fable", f"{pid}:7d", f"{pid}:5h")):
            if sub not in items:
                items.append(sub)
    for pid in discover_litellm_proxies():
        if pid not in items:
            items.append(pid)
    if read_pi_codex_auth()[0] is not None and pi_linked_codex_provider() is None:
        if "pi" not in items:
            items.append("pi")
    return items


def active_providers(
    configured: list[str] | None = None,
    *,
    include_discovered: bool = False,
    disabled: list[str] | None = None,
) -> list[str]:
    """Configured providers with local credentials or an installed agy CLI."""
    if configured is None:
        selected = discovered_default_providers()
    else:
        selected = list(configured)
        if include_discovered:
            dis = set(disabled or [])
            for extra in discovered_default_providers():
                if extra not in DEFAULT_PROVIDERS and extra not in dis and extra not in selected:
                    selected.append(extra)
    selected = list(dict.fromkeys('grok' if pid == 'grok-bot' else pid for pid in selected))
    return [pid for pid in selected if provider_available(pid)]


def refresh_local_activity(output: Path | None = None, now: float | None = None) -> dict[str, Any]:
    """Scan local session files and Paseo agents to publish route-manager.activity.v1."""
    clock = time.time() if now is None else float(now)
    cutoff = clock - 300.0
    accounts: dict[str, dict[str, Any]] = {}
    active_workers: dict[str, dict[str, int]] = {}
    iso_now = datetime.fromtimestamp(clock, tz=timezone.utc).isoformat()

    paseo_counts: dict[str, int] = {}
    paseo_agents_dir = Path(os.environ.get("PASEO_HOME") or HOME / ".paseo") / "agents"
    try:
        for agent_file in paseo_agents_dir.glob("*/*.json"):
            try:
                if agent_file.stat().st_mtime < cutoff:
                    continue
                doc = json.loads(agent_file.read_text(encoding="utf-8-sig"))
                if isinstance(doc, dict) and not doc.get("archivedAt") and doc.get("lastStatus") == "running":
                    prov = str(doc.get("provider") or "").strip()
                    if prov:
                        paseo_counts[prov] = paseo_counts.get(prov, 0) + 1
            except Exception:
                continue
    except OSError:
        pass

    def _recent_files(root: Path, suffix: str = ".jsonl") -> int:
        if not root.is_dir():
            return 0
        count = 0
        try:
            for dirpath, _, filenames in os.walk(root):
                for fname in filenames:
                    if not fname.endswith(suffix):
                        continue
                    fpath = Path(dirpath) / fname
                    try:
                        mtime = fpath.stat().st_mtime
                        if suffix == ".db":
                            wal = Path(str(fpath) + "-wal")
                            if wal.exists():
                                mtime = max(mtime, wal.stat().st_mtime)
                        if mtime >= cutoff:
                            count += 1
                    except OSError:
                        continue
        except OSError:
            pass
        return count

    for pid, chome in discover_claude_homes().items():
        if not chome.is_dir() and pid != "claude":
            continue
        count = max(paseo_counts.get(pid, 0), _recent_files(chome / "projects") + _recent_files(chome / "sessions"))
        state = "active" if count > 0 else ("idle" if chome.is_dir() else "unknown")
        accounts[pid] = {"state": state, "source": "collector", "observed_at": iso_now}
        if count > 0:
            active_workers[pid] = {"count": count}

    for pid, cacc in discover_codex_accounts().items():
        if not cacc.home.is_dir() and pid != "codex":
            continue
        count = max(paseo_counts.get(pid, 0), _recent_files(cacc.home / "sessions"))
        state = "active" if count > 0 else ("idle" if cacc.home.is_dir() else "unknown")
        accounts[pid] = {"state": state, "source": "collector", "observed_at": iso_now}
        if count > 0:
            active_workers[pid] = {"count": count}

    agy_dir = HOME / ".gemini" / "antigravity-cli" / "conversations"
    agy_count = _recent_files(agy_dir, ".db")
    accounts["antigravity"] = {
        "state": "active" if agy_count > 0 else ("idle" if agy_dir.is_dir() else "unknown"),
        "source": "collector",
        "observed_at": iso_now,
    }
    if agy_count > 0:
        active_workers["antigravity"] = {"count": agy_count}

    grok_count = paseo_counts.get("grok", 0)
    accounts["grok"] = {
        "state": "active" if grok_count > 0 else "idle",
        "source": "collector",
        "observed_at": iso_now,
    }
    if grok_count > 0:
        active_workers["grok"] = {"count": grok_count}

    pi_home = _pi_agent_home()
    if pi_home.is_dir() or paseo_counts.get("pi", 0) > 0:
        pi_count = max(paseo_counts.get("pi", 0), _recent_files(pi_home / "sessions", ".jsonl"))
        accounts["pi"] = {
            "state": "active" if pi_count > 0 else ("idle" if pi_home.is_dir() else "unknown"),
            "source": "collector",
            "observed_at": iso_now,
        }
        if pi_count > 0:
            active_workers["pi"] = {"count": pi_count}

    oc_dir = HOME / ".local" / "share" / "opencode"
    if oc_dir.is_dir() or paseo_counts.get("opencode", 0) > 0:
        oc_count = max(paseo_counts.get("opencode", 0), _recent_files(oc_dir, ".db"))
        accounts["opencode"] = {
            "state": "active" if oc_count > 0 else ("idle" if oc_dir.is_dir() else "unknown"),
            "source": "collector",
            "observed_at": iso_now,
        }
        if oc_count > 0:
            active_workers["opencode"] = {"count": oc_count}

    for pid in discover_litellm_proxies():
        if pid in accounts:
            continue
        pcount = paseo_counts.get(pid, 0)
        accounts[pid] = {
            "state": "active" if pcount > 0 else "idle",
            "source": "collector",
            "observed_at": iso_now,
        }
        if pcount > 0:
            active_workers[pid] = {"count": pcount}

    payload = {
        "schema": "route-manager.activity.v1",
        "generated_at": iso_now,
        "active_workers": active_workers,
        "accounts": accounts,
    }
    if output is None:
        output = activity_path()
    if output is not None:
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            tmp = output.with_name(f"{output.name}.{uuid.uuid4().hex}.tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(output)
        except OSError:
            pass
    return payload


def activity_path() -> Path:
    """The running/idle activity file the collector publishes and capacity_stats reads."""
    configured = os.environ.get("AGENTCOMPASS_ACTIVITY_FILE") or os.environ.get("ROUTE_MANAGER_ACTIVITY_FILE")
    return Path(configured) if configured else STATE_DIR / "usage-activity.json"


def provider_poll_seconds(provider_id: str) -> int:
    """Local session activity controls quota polling, not the panel layout."""
    if claude_magpie_source(provider_id) is not None:
        # Read from the VPS gateway: its usage moves without local sessions.
        return REFRESH_SECONDS
    path = activity_path()
    try:
        if path.stat().st_size > 1024 * 1024:
            return UNKNOWN_ACTIVITY_REFRESH_SECONDS
        activity = json.loads(path.read_text(encoding='utf-8-sig'))
        age = time.time() - _iso_epoch(activity.get('generated_at'), 0)
        workers = activity.get('active_workers')
        account = (activity.get('accounts') or {}).get(provider_id) or {}
        if (activity.get('schema') != 'route-manager.activity.v1'
                or not 0 <= age <= 180 or not isinstance(workers, dict)):
            return UNKNOWN_ACTIVITY_REFRESH_SECONDS
        tracked = {
            *discover_claude_homes(),
            *discover_codex_accounts(),
            *discover_litellm_proxies(),
            'pi',
            'opencode',
            'grok',
            'antigravity',
        }
        state = account.get('state')
        if provider_id not in tracked or state not in ('active', 'idle'):
            return UNKNOWN_ACTIVITY_REFRESH_SECONDS
        count = (workers.get(provider_id) or {}).get('count', 0)
        if state == 'active' and count > 0:
            return REFRESH_SECONDS
        if state == 'idle' and count == 0:
            return IDLE_REFRESH_SECONDS
        return UNKNOWN_ACTIVITY_REFRESH_SECONDS
    except (OSError, ValueError, TypeError, AttributeError):
        return UNKNOWN_ACTIVITY_REFRESH_SECONDS


FAILURE_RETRY_BASE_SECONDS = 120


def failure_retry_seconds(failures: int, poll: int) -> int:
    """Short exponential retry cadence after failed attempts (2m, 4m, 8m...), capped at poll."""
    if failures <= 0:
        return poll
    return min(poll, FAILURE_RETRY_BASE_SECONDS * (2 ** min(failures - 1, 10)))


def record_usage_attempt(
    provider_id: str,
    key: str | None,
    *,
    failures: int = 0,
    retry_seconds: int | None = None,
) -> None:
    def update(cache):
        entry: dict[str, Any] = {
            'at': time.time(),
            'credential_key': key,
            'failures': max(0, int(failures)),
        }
        if retry_seconds is not None and retry_seconds > 0:
            entry['retry_seconds'] = int(retry_seconds)
        cache.setdefault('attempts', {})[provider_id] = entry
    update_usage_cache(update)


def fetch_all(providers: list[str], *, force: bool = False) -> list[ProviderUsage]:
    """Collect and cache usage directly across the requested providers."""
    try:
        refresh_local_activity()
    except Exception:
        pass
    return collect_local_usage(providers, force=force)


def collect_local_usage(providers: list[str], *, force: bool = False) -> list[ProviderUsage]:
    """One account-scoped collection/cache lane."""
    out = []
    polled = {}
    claude_homes = discover_claude_homes()
    codex_accounts = discover_codex_accounts()
    litellm_proxies = discover_litellm_proxies()
    for pid in dict.fromkeys(provider_fetch_id(item) for item in providers):
        if pid not in {*PROVIDER_DISPLAY_ITEMS, *claude_homes, *codex_accounts, *litellm_proxies, 'pi', 'grok', 'api-proxy'}:
            continue
        polled[pid] = poll = provider_poll_seconds(pid)
        cached = item = None
        try:
            with file_lock(CACHE_PATH.with_name(f'{CACHE_PATH.name}.{pid}.fetch.lock'), timeout=1):
                key = provider_credential_key(pid)
                cached = cache_get_provider(pid) or display_snapshot(pid)
                cache = load_usage_cache()
                raw = (cache.get('providers') or {}).get(pid) or {}
                attempt = (cache.get('attempts') or {}).get(pid) or {}
                same_attempt = attempt.get('credential_key') == key
                last_attempt = attempt.get('at', 0) if same_attempt else 0
                observed = _iso_epoch(cached.fetched_at, 0) if cached and (not raw or raw.get('credential_key') == key) else 0
                stored_failures = attempt.get('failures') if same_attempt else None
                if (
                    isinstance(stored_failures, int)
                    and not isinstance(stored_failures, bool)
                    and stored_failures > 0
                    and not (observed > last_attempt and not raw.get('error') and raw.get('available') is not False)
                ):
                    prior_failures = stored_failures
                else:
                    prior_failures = 0
                throttle = failure_retry_seconds(prior_failures, poll) if prior_failures > 0 else poll
                now_ts = time.time()
                age = now_ts - max(last_attempt, observed)
                # A window that reset after the reading is due now, not after the idle cadence.
                resets = [_iso_epoch(w.resets_at, 0) for w in (cached.windows if cached else []) if w.resets_at]
                passed = [r for r in resets if observed < r <= now_ts]
                reset_due = bool(passed) and (last_attempt < max(passed) or now_ts - last_attempt >= REFRESH_SECONDS)
                if not force and not reset_due and 0 <= age < throttle:
                    if cached:
                        # Keep exactly the same numbers/titles. Staleness is
                        # metadata for routing, not an extra panel label.
                        cached.stale = bool(raw.get('stale', True)) or time.time() - observed >= REFRESH_SECONDS
                        out.append(cached)
                    else:
                        out.append(ProviderUsage(provider_id=pid, display_name=pid,
                                                 available=False, stale=True, error='更新失败'))
                    continue
                is_claude = pid == 'claude' or is_claude_provider(pid)
                if not (is_claude and claude_magpie_source(pid) is None and claude_in_backoff(pid)):
                    next_failures = prior_failures + 1
                    record_usage_attempt(
                        pid, key,
                        failures=next_failures,
                        retry_seconds=failure_retry_seconds(next_failures, poll),
                    )
                if pid == 'claude':
                    item = fetch_claude_usage(force=force)
                elif is_claude_provider(pid):
                    item = fetch_claude_usage(force=force, provider_id=pid)
                elif pid in codex_accounts:
                    item = fetch_codex_usage(codex_accounts[pid])
                elif pid == 'pi':
                    item = fetch_pi_usage('pi')
                elif pid == 'grok':
                    item = fetch_grok_usage()
                elif pid == 'api-proxy':
                    item = fetch_api_proxy_usage()
                elif pid in litellm_proxies:
                    item = fetch_api_proxy_usage(pid)
                elif pid == 'antigravity':
                    item = fetch_antigravity_usage()
                else:
                    continue
                if key != provider_credential_key(pid):
                    item = stale_provider_usage(pid, _tr('登录凭据已变化，等待下次更新', 'Login credentials changed; waiting for the next update'))
                elif item.available and not item.error:
                    # May still be stale (gateway readAt older than the
                    # freshness bound); that is a real reading, not a failure.
                    item.credential_key = key
                    cache_put_provider(item)
                    record_usage_attempt(pid, key, failures=0, retry_seconds=poll)
                elif item.error or item.stale or not item.available:
                    item = stale_provider_usage(
                        pid, item.error or '更新失败', [item, cached],
                        status=item.error_status, code=item.error_code, plan=item.plan_label,
                    )
                out.append(item)
        except TimeoutError:
            # The other process owns this fetch; don't overwrite its result.
            out.append(display_fallback(pid, _tr('正在由其他采集器更新', 'Another collector is updating usage'),
                                        [item, cached]))
        except Exception:
            out.append(stale_provider_usage(pid, '更新失败', [item, cached]))
    if polled:
        # Publish the cadence chosen this tick: an idle account is read every
        # IDLE_REFRESH_SECONDS, so its reading stays current that long. The router
        # widens freshness only while these decisions keep arriving.
        decided = time.time()
        def record(cache):
            cache.setdefault('poll', {}).update(
                {pid: {'seconds': seconds, 'at': decided} for pid, seconds in polled.items()})
        try:
            update_usage_cache(record)
        except Exception:
            pass
    return out
