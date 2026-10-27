"""Host the original Usage Float event loop; leave its Git working tree untouched."""
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import plugins

sys.path.insert(0, str(plugins.plugin_root("usage-float")))

import local_controls

AUTOSTART_TTL_SECONDS = 30.0
_autostart_lock = threading.Lock()
_autostart_state = {"value": None, "checked_at": 0.0, "refreshing": False, "generation": 0}


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
    return True, "已更新 Route Manager 开机启动"


def attach(root, scope):
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

    def poll():
        server.poll()
        try:
            if root.winfo_exists():
                publish()
                root.after(200, poll)
        except Exception:
            server.close()

    publish()
    root.after(200, poll)
    return server


def main():
    import tkinter as tk
    import usage_float as native

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
