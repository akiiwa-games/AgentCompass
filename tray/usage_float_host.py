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
    "filter_providers",
    "provider_credential_key",
    "provider_from_dict",
    "read_collector_config",
    "write_collector_config",
    "fetch_claude_session_reset",
    "claim_claude_session_reset",
    "claim_claude_reset",
    "parse_claude_session_reset",
    "ClaudeResetClaim",
    "fetch_codex_reset_cards",
    "consume_codex_reset_card",
    "parse_codex_reset_cards",
)


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


def bind_usage(native) -> Path:
    """Bind Usage Float's public seam to AgentCompass's single collector and shared cache."""
    missing = [name for name in REQUIRED_USAGE_SEAM if not callable(getattr(native, name, None))]
    if missing:
        raise RuntimeError("Usage Float 接口变化：" + ", ".join(missing))
    cache_path = share_cache(native)
    if cache_path is None:
        raise RuntimeError("AgentCompass 用量采集器脚本未安装")
    import usage_collector
    import usage_core

    usage_core.configure_paths(Path(cache_path))
    native.PERIODIC_REFRESH = False
    native.provider_credential_key = usage_core.provider_credential_key
    native.filter_providers = usage_collector.filter_permitted_items
    native.read_collector_config = usage_core.read_usage_config
    native.write_collector_config = usage_core.write_usage_config
    native.get_usage = lambda providers: [
        native.provider_from_dict(d) for d in usage_collector.read_usage(providers)
    ]
    native.refresh_usage = lambda providers: [
        native.provider_from_dict(usage_core.provider_to_dict(p))
        for p in usage_collector.refresh_providers(providers)
    ]
    native.fetch_claude_session_reset = lambda provider_id="claude": (
        lambda block, err: (None, err) if err is not None else (native.parse_claude_session_reset(block), None)
    )(*usage_core.fetch_claude_session_reset(provider_id))
    native.claim_claude_session_reset = lambda provider_id="claude": native.ClaudeResetClaim(
        **usage_core.claim_claude_session_reset(provider_id)
    )
    native.claim_claude_reset = lambda grant_id, *, request_id, provider_id="claude": native.ClaudeResetClaim(
        **usage_core.claim_claude_reset(grant_id, request_id=request_id, provider_id=provider_id)
    )
    native.fetch_codex_reset_cards = lambda home=None, *, provider_id=None: (
        lambda credits, err: ([], err) if err is not None else (native.parse_codex_reset_cards(credits), None)
    )(*usage_core.fetch_codex_reset_cards(home, provider_id=provider_id))
    native.consume_codex_reset_card = usage_core.consume_codex_reset_card
    return cache_path


def attach(root, scope, watched_cache: Path | None = None):
    from tkinter import TclError

    required = ("state", "refresh_async", "request_open_settings", "toggle_panel_mode",
                "toggle_topmost", "do_quit", "set_wallpaper_active", "stack_text_above_plate")
    missing = [name for name in required if name not in scope]
    if missing:
        raise RuntimeError("Usage Float 接口变化：" + ", ".join(missing))
    server = local_controls.ControlServer("usage-float")
    state = scope["state"]
    if watched_cache is None:
        inferred = getattr(scope.get("refresh_async"), "__globals__", {}).get("CACHE_PATH")
        if isinstance(inferred, (str, Path)):
            watched_cache = Path(inferred)

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
    last_sig = _file_signature(watched_cache)

    def poll():
        nonlocal error_reported, cache_ticks, last_sig
        try:
            server.poll()
            cache_ticks += 1
            if cache_ticks >= 5:
                cache_ticks = 0
                if watched_cache is not None and not state.get("refreshing"):
                    sig = _file_signature(watched_cache)
                    if sig is not None and sig != last_sig:
                        last_sig = sig
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
