"""Host the original Usage Float event loop; leave its Git working tree untouched."""
from pathlib import Path
import logging
import os
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import component_launch
import plugins

sys.path.insert(0, str(plugins.plugin_root("usage-float")))

import local_controls

AUTOSTART_TTL_SECONDS = 30.0
_autostart_lock = threading.Lock()
_autostart_state = {"value": None, "checked_at": 0.0, "refreshing": False, "generation": 0}

REQUIRED_USAGE_SEAM = (
    "get_usage",
    "refresh_usage",
    "provider_credential_key",
    "provider_fetch_id",
    "load_usage_cache",
    "_provider_from_dict",
    "_iso_epoch",
)
_hosted_cache_lock = threading.Lock()
_hosted_cache_state: dict[str, object] = {"path": None, "signature": None}


def refresh_autostart_async(force: bool = False) -> None:
    import deploy

    with _autostart_lock:
        now = time.monotonic()
        if _autostart_state["refreshing"]:
            return
        if (
            not force
            and _autostart_state["value"] is not None
            and now - _autostart_state["checked_at"] < AUTOSTART_TTL_SECONDS
        ):
            return
        _autostart_state["refreshing"] = True
        gen = _autostart_state["generation"]

    def worker() -> None:
        try:
            enabled = bool(deploy.startup_enabled())
        except Exception:
            enabled = False
        with _autostart_lock:
            if _autostart_state["generation"] == gen:
                _autostart_state["value"] = enabled
                _autostart_state["checked_at"] = time.monotonic()
            _autostart_state["refreshing"] = False

    threading.Thread(target=worker, daemon=True, name="uf-autostart-probe").start()


def is_autostart_enabled() -> bool:
    with _autostart_lock:
        cached = _autostart_state["value"]
        stale = (
            cached is None
            or time.monotonic() - _autostart_state["checked_at"] >= AUTOSTART_TTL_SECONDS
        )
    if stale:
        refresh_autostart_async(force=cached is None)
    return True if cached is None else bool(cached)


def set_autostart(enabled: bool) -> tuple[bool, str]:
    import deploy

    target = bool(enabled)
    with _autostart_lock:
        _autostart_state["generation"] += 1
        gen = _autostart_state["generation"]
        _autostart_state["value"] = target
        _autostart_state["checked_at"] = time.monotonic()
        _autostart_state["refreshing"] = True

    def worker() -> None:
        try:
            if target:
                deploy.write_startup()
            else:
                deploy.remove_startup()
        except Exception:
            pass
        try:
            actual = bool(deploy.startup_enabled())
        except Exception:
            actual = target
        with _autostart_lock:
            if _autostart_state["generation"] == gen:
                _autostart_state["value"] = actual
                _autostart_state["checked_at"] = time.monotonic()
                _autostart_state["refreshing"] = False

    threading.Thread(target=worker, daemon=True, name="uf-autostart-apply").start()
    return True, "已更新 AgentCompass 开机启动"


def share_cache(native) -> Path | None:
    """Point the hosted Usage Float at the router's usage cache (~/.agent-compass/usage-cache.json).

    The router's collector owns ~/.agent-compass/usage-cache.json (ROUTER_USAGE_CACHE when set). Usage Float's own
    default is the legacy ~/.usage-float copy. The legacy cache seeds the shared one first so its history carries over.
    Returns the path, or None when the router's scripts are not installed.
    """
    scripts = component_launch.skill_scripts()
    if scripts is None:
        return None
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import usage_cache
    usage_cache.seed_from_legacy()
    native.CACHE_PATH = usage_cache.write_path()
    # Usage Float publishes the activity file only next to its own default cache; keep that path aligned with the shared state dir.
    os.environ.setdefault("ROUTE_MANAGER_ACTIVITY_FILE", str(usage_cache.state_dir() / "usage-activity.json"))
    return native.CACHE_PATH


def _file_signature(path: Path | None) -> tuple[int, int] | None:
    if path is None:
        return None
    try:
        st = Path(path).stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _record_cache_signature(path: Path | None) -> None:
    sig = _file_signature(path)
    with _hosted_cache_lock:
        _hosted_cache_state["path"] = Path(path) if path is not None else None
        _hosted_cache_state["signature"] = sig


def _sync_usage_core_paths(usage_core, cache_path: Path) -> None:
    home = Path.home()
    claude_home = Path(os.environ.get("CLAUDE_HOME", home / ".claude"))
    claude_home_2 = Path(os.environ.get("CLAUDE_HOME_2", home / ".claude-2"))
    codex_home = home / ".codex"
    codex_home_2 = Path(os.environ.get("CODEX_HOME_2", home / ".codex-2"))
    grok_home = Path(os.environ.get("GROK_HOME", home / ".grok"))
    state_dir = usage_core.usage_cache.state_dir(home)
    usage_core.HOME = home
    usage_core.CLAUDE_HOME = claude_home
    usage_core.CLAUDE_HOME_2 = claude_home_2
    usage_core.CODEX_HOME = codex_home
    usage_core.CODEX_HOME_2 = codex_home_2
    usage_core.GROK_HOME = grok_home
    usage_core.STATE_DIR = state_dir
    usage_core.CONFIG_PATH = Path(os.environ.get("AGENTCOMPASS_USAGE_CONFIG") or state_dir / "usage-config.json")
    usage_core.LEGACY_CONFIG_PATH = home / ".usage-float" / "config.json"
    usage_core.CACHE_PATH = cache_path
    codex_accounts = (
        usage_core.CodexAccount("codex", "codex", codex_home),
        usage_core.CodexAccount("codex-2", "codex-2", codex_home_2),
    )
    usage_core.CODEX_ACCOUNTS = codex_accounts
    usage_core.CODEX_ACCOUNT_BY_ID = {acc.provider_id: acc for acc in codex_accounts}


def bind_usage(native) -> Path:
    """Bind Usage Float's usage seam to AgentCompass's single collector and shared cache.

    Hosted Usage Float never runs its own fetchers or parsers and never writes providers[*]:
    - get_usage reads what the AgentCompass collector wrote to ~/.agent-compass/usage-cache.json.
    - refresh_usage triggers the AgentCompass collector (usage_core.fetch_all(..., force=True)).
    """
    missing = [name for name in REQUIRED_USAGE_SEAM if not callable(getattr(native, name, None))]
    if missing:
        raise RuntimeError("Usage Float 接口变化：" + ", ".join(missing))
    cache_path = share_cache(native)
    if cache_path is None:
        raise RuntimeError("AgentCompass 用量采集器脚本未安装")
    import host_policy
    import usage_core

    _sync_usage_core_paths(usage_core, Path(cache_path))

    def hosted_credential_key(provider_id: str) -> str | None:
        _sync_usage_core_paths(usage_core, Path(native.CACHE_PATH))
        return usage_core.provider_credential_key(provider_id)

    def hosted_get_usage(providers: list[str]):
        current_cache = Path(native.CACHE_PATH)
        _record_cache_signature(current_cache)
        cache = native.load_usage_cache()
        raw_providers = cache.get("providers") if isinstance(cache.get("providers"), dict) else {}
        snapshots = cache.get("display_snapshots") if isinstance(cache.get("display_snapshots"), dict) else {}
        polls = cache.get("poll") if isinstance(cache.get("poll"), dict) else {}
        now_ts = time.time()
        out = []
        for pid in dict.fromkeys(native.provider_fetch_id(item) for item in providers):
            expected_key = hosted_credential_key(pid)
            raw = raw_providers.get(pid)
            if isinstance(raw, dict) and raw.get("provider_id") == pid and raw.get("credential_key") == expected_key:
                try:
                    item = native._provider_from_dict(raw)
                except (TypeError, ValueError):
                    item = None
                if item is not None and (item.windows or item.summary):
                    poll_entry = polls.get(pid)
                    poll_seconds = (
                        float(poll_entry["seconds"])
                        if isinstance(poll_entry, dict)
                        and isinstance(poll_entry.get("seconds"), (int, float))
                        and not isinstance(poll_entry.get("seconds"), bool)
                        else 300.0
                    )
                    observed = native._iso_epoch(item.fetched_at, 0)
                    item.stale = (
                        bool(raw.get("stale", False))
                        or bool(item.error)
                        or observed <= 0
                        or (now_ts - observed >= max(300.0, poll_seconds))
                    )
                    out.append(item)
                    continue
            snap = snapshots.get(pid)
            if isinstance(snap, dict) and snap.get("provider_id") == pid and snap.get("credential_key") == expected_key:
                try:
                    snap_item = native._provider_from_dict(snap)
                except (TypeError, ValueError):
                    snap_item = None
                if snap_item is not None and (snap_item.windows or snap_item.summary):
                    snap_item.stale = True
                    if isinstance(raw, dict) and raw.get("credential_key") in (None, expected_key) and raw.get("error"):
                        snap_item.error = str(raw["error"])
                    out.append(snap_item)
                    continue
            if isinstance(raw, dict) and raw.get("credential_key") in (None, expected_key) and raw.get("error"):
                try:
                    err_item = native._provider_from_dict(raw)
                    err_item.provider_id = pid
                    err_item.stale = True
                    out.append(err_item)
                except (TypeError, ValueError):
                    pass
        return out

    def hosted_refresh_usage(providers: list[str]):
        current_cache = Path(native.CACHE_PATH)
        _sync_usage_core_paths(usage_core, current_cache)
        requested = list(dict.fromkeys(native.provider_fetch_id(item) for item in providers))
        allowed = host_policy.usage_providers(host_policy.load_policy(), requested)
        observations = usage_core.fetch_all(allowed, force=True) if allowed else []
        _record_cache_signature(current_cache)
        return [native._provider_from_dict(usage_core._provider_to_dict(p)) for p in observations]

    native.provider_credential_key = hosted_credential_key
    native.get_usage = hosted_get_usage
    native.refresh_usage = hosted_refresh_usage
    _record_cache_signature(cache_path)
    return cache_path


def attach(root, scope):
    from tkinter import TclError

    required = ("state", "refresh_async", "request_open_settings", "toggle_panel_mode",
                "toggle_topmost", "do_quit", "set_wallpaper_active", "stack_text_above_plate")
    missing = [name for name in required if name not in scope]
    if missing:
        raise RuntimeError("Usage Float 接口变化：" + ", ".join(missing))
    server = local_controls.ControlServer("usage-float")
    state = scope["state"]

    def show():
        scope["set_wallpaper_active"](False)
        background = scope.get("bg_layer")
        if background is not None and not state.get("panel_active"):
            background.deiconify()
        root.deiconify()
        scope["stack_text_above_plate"]()
        root.lift()

    def publish():
        server.publish([
            {"label": "显示用量面板", "id": "show"},
            {"label": "刷新用量", "id": "refresh", "enabled": not state["refreshing"]},
            {"label": "设置…", "id": "settings"},
            {"type": "separator"},
            {"label": "专用副屏模式", "id": "panel", "type": "checkbox",
             "checked": state.get("display_mode") == "panel"},
            {"label": "动态壁纸", "id": "wallpaper", "type": "checkbox",
             "checked": bool(state.get("wallpaper_active")), "enabled": bool(state.get("panel_active"))},
            {"label": "置顶", "id": "topmost", "type": "checkbox", "checked": state["always_on_top"]},
            {"type": "separator"},
            {"label": "关闭 Usage Float", "id": "quit"},
        ], {"show": show, "refresh": lambda: scope["refresh_async"](force=True),
            "settings": scope["request_open_settings"], "panel": scope["toggle_panel_mode"],
            "wallpaper": lambda: scope["set_wallpaper_active"](not state.get("wallpaper_active")),
            "topmost": scope["toggle_topmost"], "quit": scope["do_quit"]})

    error_reported = False
    cache_ticks = 0

    def poll():
        nonlocal error_reported, cache_ticks
        try:
            server.poll()
            cache_ticks += 1
            if cache_ticks >= 5:
                cache_ticks = 0
                if not state.get("refreshing"):
                    with _hosted_cache_lock:
                        watched_path = _hosted_cache_state["path"]
                        last_sig = _hosted_cache_state["signature"]
                    if watched_path is not None:
                        sig = _file_signature(watched_path)
                        if sig is not None and sig != last_sig:
                            with _hosted_cache_lock:
                                _hosted_cache_state["signature"] = sig
                            scope["refresh_async"](force=False)
            if root.winfo_exists():
                publish()
        except Exception:
            # A Windows reader can briefly prevent replacing menu.json. Keep the
            # existing snapshot and retry on the UI loop instead of disconnecting
            # a healthy window. Log once per failure streak, including traceback.
            if not error_reported:
                logging.getLogger(__name__).exception("Usage Float control update failed; retrying")
                error_reported = True
        else:
            error_reported = False
        finally:
            try:
                if root.winfo_exists():
                    root.after(200, poll)
                else:
                    server.close()
            except TclError:
                # The quit action can destroy Tk while this callback is running.
                server.close()

    poll()
    return server


def main():
    import tkinter as tk
    import ui_theme as theme

    theme.identify_process()
    import usage_float as native

    bind_usage(native)
    # Warm the scheduled-task autostart cache off the UI thread so right-clicking
    # the HUD never blocks on PowerShell Get-ScheduledTask.
    refresh_autostart_async(force=True)
    native.is_autostart_enabled = is_autostart_enabled
    native.set_autostart = set_autostart
    original_mainloop = tk.Tk.mainloop
    def hosted_mainloop(root, *args, **kwargs):
        frame = sys._getframe(1)
        if frame.f_code.co_name != "run_ui" or frame.f_globals.get("__name__") != native.__name__:
            return original_mainloop(root, *args, **kwargs)
        theme.set_window_icon(root)
        server = attach(root, frame.f_locals)
        try:
            return original_mainloop(root, *args, **kwargs)
        finally:
            server.close()
    tk.Tk.mainloop = hosted_mainloop
    try:
        return native.main()
    finally:
        tk.Tk.mainloop = original_mainloop


if __name__ == "__main__":
    raise SystemExit(main())
