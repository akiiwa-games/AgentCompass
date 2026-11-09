"""AgentCompass tray: opens the main panel, sets the reset priority, and gives one click to each enabled plugin window."""


from i18n import translate as _tr, tr
import i18n
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pystray
from PIL import Image, ImageDraw
from pystray import Menu
from pystray import MenuItem

import dot_reconnect
import cli_updates
import easytier
import network_status
import plugins
import router_priority
import router_setup
import router_window
import services
import ui_theme
import updates
import update_status

REFRESH_SECONDS = 10
RUNNING = "运行中"
DISABLED = "未启用"
TITLE_LIMIT = 127

# What the menu and icon show. Built by the watcher threads so that opening the menu never waits on a service.
STATE = {"dot": "检测中", "web": "检测中", "gemini": "检测中",
         "float": "检测中", "easytier": "检测中", "muse": "检测中"}
EASYTIER = {"state": easytier.MISSING, "pids": [], "peers": None}
PRIORITY = {"label": "重置前优先", "entries": [], "targets": []}
PLUGINS = plugins.defaults()
_LOCAL_VER = updates.local_version()
VERSION = {
    "tray_version": _LOCAL_VER,
    "display_version": f"AgentCompass {_LOCAL_VER}",
    "menu_label": f"AgentCompass {_LOCAL_VER}",
    "update_available": False,
    "remote_tag": "",
    "version_mismatch": False,
    "status_line": f"AgentCompass {_LOCAL_VER}",
}
_NOTIFIED_RELEASE_TAG = ""

# plugin id -> STATE key of its label
PLUGIN_STATE = {"usage-float": "float", "chatgpt-web": "web", "gemini-web": "gemini",
                "easytier": "easytier", "dot2api": "dot", "muse2api": "muse"}
# Plugins that are expected to keep running once started (Gemini Web is a visible browser, started on demand).
SERVICE_PLUGINS = ("dot2api", "chatgpt-web", "easytier", "usage-float", "muse2api")


_SINGLE_INSTANCE_MUTEX = None


def single_instance() -> bool:
    import ctypes

    global _SINGLE_INSTANCE_MUTEX
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel.CreateMutexW(None, False, "Local\\AgentCompassTray")
    if ctypes.get_last_error() == 183:
        if handle:
            kernel.CloseHandle(ctypes.c_void_p(handle))
        return False
    _SINGLE_INSTANCE_MUTEX = handle
    sys._agentcompass_tray_mutex = handle
    return True


def release_single_instance() -> None:
    global _SINGLE_INSTANCE_MUTEX
    handle = _SINGLE_INSTANCE_MUTEX or getattr(sys, "_agentcompass_tray_mutex", None)
    if not handle:
        return
    try:
        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CloseHandle(ctypes.c_void_p(handle))
    except Exception:
        pass
    _SINGLE_INSTANCE_MUTEX = None
    sys._agentcompass_tray_mutex = None


STATUS_COLOURS = {"ok": (16, 185, 129), "attention": (245, 158, 11), "error": (239, 68, 68)}


def health() -> str:
    """error: an enabled always-on plugin is down; attention: a reset priority is active."""
    for plugin_id in SERVICE_PLUGINS:
        if PLUGINS[plugin_id]["enabled"] and PLUGINS[plugin_id]["autostart"]:
            if STATE.get(PLUGIN_STATE[plugin_id]) in ("已停止", "Stopped"):
                return "error"
    return "attention" if PRIORITY["entries"] else "ok"


def draw_icon() -> Image.Image:
    """The AgentCompass compass tile, with a status dot in the lower left."""
    image = ui_theme.compass_image(64)
    draw = ImageDraw.Draw(image)
    draw.ellipse((4, 42, 24, 62), fill=(15, 23, 42))
    draw.ellipse((7, 45, 21, 59), fill=STATUS_COLOURS[health()])
    return image


def title() -> str:
    parts = ["AgentCompass", {"ok": _tr("正常"), "attention": PRIORITY["label"], "error": _tr("有服务停止")}[health()]]
    for plugin in plugins.PLUGINS:
        if PLUGINS[plugin.id]["enabled"] and plugin.id in PLUGIN_STATE:
            parts.append(f"{plugin.short} {_tr(STATE[PLUGIN_STATE[plugin.id]])}")
    text = " · ".join(parts)
    return text if len(text) <= TITLE_LIMIT else text[:TITLE_LIMIT - 1] + "…"


MAX_NOTIFY_LENGTH = 240


def log_error(exc: BaseException) -> None:
    path = services.STATE_DIR / "tray.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        traceback.print_exception(exc, file=stream)


def log_info(message: str) -> None:
    path = services.STATE_DIR / "tray.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with path.open("a", encoding="utf-8") as stream:
        stream.write(f"[{timestamp}] {message}\n")


def notify(icon: pystray.Icon, message: str) -> None:
    text = message if len(message) <= MAX_NOTIFY_LENGTH else f"{message[:MAX_NOTIFY_LENGTH - 1]}…"
    icon.notify(text, "AgentCompass")


def redraw(icon) -> None:
    icon.icon = draw_icon()
    icon.title = title()
    icon.update_menu()


def refresh(icon: pystray.Icon) -> None:
    """Re-read the states of the enabled plugins; redraw only when something changed."""
    config = PLUGINS
    probes = {"dot": ("dot2api", services.dot_label), "web": ("chatgpt-web", services.chatgpt_label),
              "gemini": ("gemini-web", services.gemini_label),
              "float": ("usage-float", services.usage_float_label),
              "muse": ("muse2api", services.muse_label)}
    latest = {}
    for key, (plugin_id, probe) in probes.items():
        latest[key] = probe() if config[plugin_id]["enabled"] else _tr(DISABLED)
    if config["easytier"]["enabled"]:
        try:
            EASYTIER.update(easytier.snapshot())
        except Exception as exc:
            log_error(exc)
        latest["easytier"] = easytier.label(EASYTIER)
    else:
        latest["easytier"] = _tr(DISABLED)
    if latest == STATE:
        return
    STATE.update(latest)
    redraw(icon)


def act(icon: pystray.Icon, work) -> None:
    is_update_job = work in (updates.update_all, updates.update_agentcompass)
    if is_update_job:
        try:
            update_status.open_window()
        except Exception as exc:
            log_error(exc)

    def runner() -> None:
        if work is updates.update_all:
            try:
                notify(icon, _tr("正在检查并更新插件组件…"))
            except Exception:
                pass
        elif work is updates.update_agentcompass:
            try:
                notify(icon, _tr("正在检查并更新 AgentCompass…"))
            except Exception:
                pass
        try:
            message = work(progress=update_status.record) if is_update_job else work()
            if _tr("已启动隧道") in message or _tr("已启动 Dot2API") in message:
                url = services.capture_tunnel_url()
                if url:
                    message = _tr('{0}；隧道 {1}').format(f'{message}', f'{url}')
            if work is updates.update_all:
                log_info(f"update_all: {message}")
            elif work is updates.update_agentcompass:
                log_info(f"update_agentcompass: {message}")
            else:
                log_info(f"action: {message}")
        except Exception as exc:
            log_error(exc)
            message = _tr("操作失败，详见日志")
        try:
            refresh(icon)
            refresh_priority(icon)
            if work is updates.update_agentcompass:
                refresh_version(icon, force=False, network=False)
        except Exception as exc:
            log_error(exc)
        try:
            notify(icon, message)
        except Exception:
            pass  # Quitting the tray may close its icon while an update finishes.

    # An update owns child installers and a recovery transaction. Finish it even
    # if the user closes the icon, so those children are never abandoned.
    threading.Thread(target=runner, daemon=not is_update_job).start()


RECONNECTING = threading.Lock()


def reconnect_dot(icon: pystray.Icon) -> None:
    """Give the Dot the new tunnel address when it changed; takes a minute or two, so it runs off the watcher thread."""
    if not RECONNECTING.acquire(blocking=False):
        return
    try:
        message = dot_reconnect.reconnect()
        if message:
            notify(icon, message)
    except Exception as exc:
        log_error(exc)
    finally:
        RECONNECTING.release()


def watch(icon: pystray.Icon) -> None:
    checked = 0.0
    while True:
        try:
            services.usage_collect_tick()
            services.capacity_tick()
            refresh(icon)
            now = time.monotonic()
            if now - checked >= 60:
                checked = now
                cli_updates.maybe_trigger_auto_update(notify_fn=lambda message: notify(icon, message))
                # A disabled Dot2API is neither renewed nor reconnected, and reports nothing.
                if PLUGINS["dot2api"]["enabled"]:
                    message = services.maintain_subscription()
                    if message:
                        notify(icon, message)
                    threading.Thread(target=reconnect_dot, args=(icon,), daemon=True).start()
        except Exception as exc:
            log_error(exc)
        threading.Event().wait(REFRESH_SECONDS)


def refresh_priority(icon) -> None:
    global PRIORITY
    try:
        latest = router_priority.snapshot()
    except router_priority.RouterMissing:
        latest = {"label": _tr("Router 未设置"), "entries": [], "targets": [], "missing": True}
    except Exception as exc:
        log_error(exc)
        latest = {"label": _tr("优先状态异常"), "entries": [], "targets": []}
    if latest != PRIORITY:
        PRIORITY = latest
        icon.menu = build_menu()
        icon.icon = draw_icon()


def refresh_plugins(icon) -> None:
    global PLUGINS
    latest = plugins.load()
    if latest != PLUGINS:
        PLUGINS = latest
        icon.menu = build_menu()
        icon.icon = draw_icon()
        icon.title = title()


def refresh_version(icon, force: bool = False, network: bool = False) -> dict:
    global VERSION, _NOTIFIED_RELEASE_TAG
    status = updates.version_status(force=force, network=network)
    latest = {
        "tray_version": status["tray_version"],
        "display_version": status["display_version"],
        "menu_label": status["menu_label"],
        "update_available": status["update_available"],
        "remote_tag": status["remote_tag"],
        "version_mismatch": status["version_mismatch"],
        "status_line": status["status_line"],
    }
    if latest != VERSION:
        VERSION = latest
        icon.menu = build_menu()
    if status.get("update_available") and status.get("remote_tag") and status["remote_tag"] != _NOTIFIED_RELEASE_TAG:
        _NOTIFIED_RELEASE_TAG = status["remote_tag"]
        if not force:
            try:
                notify(icon, f"AgentCompass · {status['update_label']}")
            except Exception:
                pass
    return status


def check_product_update(icon=None, item=None) -> None:
    def runner() -> None:
        try:
            status = refresh_version(icon, force=True, network=True)
            if icon is not None:
                notify(icon, status.get("status_line") or status.get("menu_label") or VERSION["menu_label"])
        except Exception as exc:
            log_error(exc)

    threading.Thread(target=runner, daemon=True).start()


def open_router_setup(icon=None, item=None) -> None:
    try:
        router_setup.open_window()
    except Exception as exc:
        log_error(exc)


def offer_router_setup() -> None:
    """First launch without a router setup: open the consent window once (it records that it was offered)."""
    try:
        if router_setup.should_offer():
            router_setup.open_window()
    except Exception as exc:
        log_error(exc)


PREWARM_DELAY = 8.0


def prewarm_window() -> None:
    """Build the window hidden once login work has settled, so the first open is only a show."""
    time.sleep(PREWARM_DELAY)
    try:
        router_window.prewarm()
    except Exception as exc:
        log_error(exc)


def open_panel(icon=None, item=None) -> None:
    try:
        router_window.open_window()
    except Exception as exc:
        log_error(exc)


def notify_if_restarted(icon: pystray.Icon) -> bool:
    import deploy

    marker = deploy.consume_restart_marker()
    if not marker:
        return False
    ver = VERSION.get("tray_version") or marker.get("version") or updates.local_version()
    try:
        notify(icon, _tr("托盘已重启 · AgentCompass {0}").format(ver))
    except Exception as exc:
        log_error(exc)
    return True


PLUGIN_ACTIONS = {
    "usage-float": lambda: services.component_action("usage-float", "show"),
    "chatgpt-web": lambda: services.start_chatgpt_web(True),
    "gemini-web": lambda: services.start_gemini_web(True),
}


def build_menu() -> Menu:
    def run(work):
        return lambda icon, item: act(icon, work)

    def quit_tray(icon, item):
        log_info("tray quit requested from menu")
        services.stop_usage_collect()
        services.stop_capacity()
        icon.stop()

    def priority_menu():
        # A model's submenu selects the expected reset deadline, not a fixed score.
        items = []
        for target in PRIORITY["targets"]:
            durations = []
            for hours in router_priority.DURATIONS:
                action = {"kind": "enable", "hours": hours, **{k: target[k] for k in ("account", "model", "window")}}
                durations.append(MenuItem(_tr('预计 {0} 小时后重置').format(hours),
                                          run(lambda a=action: router_priority.perform(a))))
            items.append(MenuItem(target["label"], Menu(*durations),
                                  checked=lambda item, selected=target.get("selected", False): selected))
        if PRIORITY["entries"]:
            items.append(Menu.SEPARATOR)
        for entry in PRIORITY["entries"]:
            action = {"kind": "cancel", "id": entry["id"], "revision": entry["revision"]}
            label = _tr("取消优先") if len(PRIORITY["entries"]) == 1 else _tr('取消 {0}').format(f"{entry['label']}")
            items.append(MenuItem(label, run(lambda a=action: router_priority.perform(a))))
        return Menu(*items)

    def plugin_item(plugin):
        key = PLUGIN_STATE[plugin.id]
        text = lambda item, name=plugin.name, key=key: f"{name}{tr('：', ': ')}{_tr(STATE[key])}"
        if plugin.id == "easytier":
            # The window is a separate process that reads EasyTier itself; opening it costs no probe.
            return MenuItem(text, lambda icon, item: network_status.open_window())
        return MenuItem(text, run(PLUGIN_ACTIONS[plugin.id]))

    has_priority = bool(PRIORITY["targets"] or PRIORITY["entries"])
    items = [
        MenuItem(_tr("打开 AgentCompass"), open_panel, default=True),
        MenuItem(_tr(PRIORITY["label"]), priority_menu() if has_priority else None, enabled=has_priority),
    ]
    if PRIORITY.get("missing"):
        items.append(MenuItem(_tr("设置 Router…"), open_router_setup))
    shown = plugins.menu_plugins(PLUGINS)
    if shown:
        items.append(Menu.SEPARATOR)
        items += [plugin_item(plugin) for plugin in shown]
    # One product line: it updates when a newer release is known, otherwise it checks for one.
    def product_text(item):
        current = VERSION.get("display_version") or f"AgentCompass {VERSION.get('tray_version', '0.0.0')}"
        current = current.removeprefix("AgentCompass ")
        if VERSION.get("update_available") and VERSION.get("remote_tag"):
            return _tr("更新到 {0}（当前 {1}）").format(VERSION["remote_tag"], current)
        return _tr("检查更新（当前 {0}）").format(current)

    update_now = run(updates.update_agentcompass)

    def product_action(icon, item):
        if VERSION.get("update_available") and VERSION.get("remote_tag"):
            update_now(icon, item)
        else:
            check_product_update(icon, item)

    items += [
        Menu.SEPARATOR,
        MenuItem(product_text, product_action),
        MenuItem(_tr("退出"), quit_tray),
    ]
    return Menu(*items)


def watch_component_menus(icon) -> None:
    # Read the small local snapshots (priority, plugin switches, version/release cache) off the UI thread.
    language = i18n.get_language()
    release_checked_at = 0.0
    while True:
        try:
            refresh_plugins(icon)
            refresh_priority(icon)
            now = time.monotonic()
            allow_net = (now - release_checked_at) >= 60.0
            if allow_net:
                release_checked_at = now
            refresh_version(icon, force=False, network=allow_net)
            latest_language = i18n.get_language()
            if latest_language != language:
                language = latest_language
                refresh_version(icon, force=False, network=False)
                icon.menu = build_menu()
                redraw(icon)
        except Exception as exc:
            log_error(exc)
        threading.Event().wait(REFRESH_SECONDS)


def main() -> None:
    import os

    ui_theme.identify_process()
    global PLUGINS
    log_info(f"tray starting pid={os.getpid()} attach_only={'--attach-only' in sys.argv}")
    if not single_instance():
        log_info("tray startup skipped: another instance holds the mutex")
        if "--attach-only" not in sys.argv:
            # Started again from the Start menu or desktop shortcut while the tray runs: show its window, not nothing.
            router_window.open_window()
        return
    PLUGINS = plugins.load()
    icon = pystray.Icon("agentcompass", draw_icon(), "AgentCompass")
    icon.menu = build_menu()

    def setup(icon: pystray.Icon) -> None:
        icon.visible = True
        log_info("tray icon visible")
        try:
            st = update_status.reconcile_status()
            if isinstance(st, dict) and st.get("running"):
                def _reconcile_after_installer() -> None:
                    for _ in range(60):
                        time.sleep(0.5)
                        try:
                            cur = update_status.reconcile_status()
                            if not isinstance(cur, dict) or not cur.get("running"):
                                break
                        except Exception:
                            break

                threading.Thread(target=_reconcile_after_installer, daemon=True).start()
        except Exception as exc:
            log_error(exc)
        notify_if_restarted(icon)
        # Login starts services; an explicit UI-only maintenance reload must not
        # start a service that the user intentionally left stopped.
        if "--attach-only" not in sys.argv:
            act(icon, services.start_all)
        threading.Thread(target=watch, args=(icon,), daemon=True).start()
        threading.Thread(target=watch_component_menus, args=(icon,), daemon=True).start()
        if "--attach-only" not in sys.argv:
            threading.Thread(target=offer_router_setup, daemon=True).start()
        threading.Thread(target=prewarm_window, daemon=True).start()

    try:
        icon.run(setup)
    finally:
        log_info("tray event loop ended")


if __name__ == "__main__":
    import component_launch

    helper_exit = component_launch.dispatch(sys.argv)   # AgentCompass.exe --run-component <name>: a helper, not a second tray
    if helper_exit is not None:
        raise SystemExit(helper_exit)
    try:
        main()
    except BaseException as exc:
        release_single_instance()
        log_error(exc)
        raise
