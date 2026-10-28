"""Start and stop Dot2API and ChatGPT Web without a console window."""

from __future__ import annotations

from i18n import translate as _tr

import csv
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import psutil
import easytier
import gateways
import local_controls
import plugins
import runtime_paths

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
ROOT = Path(__file__).resolve().parents[1]
DOT_ROOT = plugins.plugin_root("dot2api")
CHATGPT_ROOT = plugins.plugin_root("chatgpt-web")
CHATGPT_LAUNCHER = CHATGPT_ROOT / "launcher"
USAGE_FLOAT_ROOT = plugins.plugin_root("usage-float")
USAGE_FLOAT_SCRIPT = USAGE_FLOAT_ROOT / "usage_float.py"
_component_lock = threading.Lock()
_component_children = {}
UPDATING_COMPONENTS = set()
BUN = ROOT / "tools" / "bun" / "bun.exe"
STATE_DIR = Path.home() / ".route-manager"
LOG_DIR = Path.home() / ".dot2api"
TUNNEL_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def hidden_popen(args: list[str], env: dict[str, str], cwd: Path, log_path: Path, extra_flags: int = 0) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "ab", buffering=0)
    startup = subprocess.STARTUPINFO()
    exe_name = Path(args[0]).name.lower() if args else ""
    if "--hidden" in args or "--quit" in args or not exe_name.startswith(("electron", "pythonw")):
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
    return subprocess.Popen(
        args,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        startupinfo=startup,
        creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | extra_flags,
        close_fds=True,
    )


def run_hidden(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        text=True,
        errors="replace",
        capture_output=True,
        creationflags=CREATE_NO_WINDOW,
        check=False,
    )


def listener_pid(port: int) -> int | None:
    """The process listening on a local TCP port, if any."""
    try:
        connections = psutil.net_connections(kind="tcp")
    except psutil.Error:
        return None
    for connection in connections:
        if (connection.status == psutil.CONN_LISTEN and connection.pid and connection.laddr
                and connection.laddr.port == port and connection.laddr.ip in ("127.0.0.1", "0.0.0.0")):
            return connection.pid
    return None


def http_ok(url: str, timeout: float = 3) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def http_json(url: str, timeout: float = 3) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            data = json.loads(response.read())
    except (OSError, urllib.error.URLError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def kill_pid(pid: int) -> None:
    run_hidden(["taskkill", "/PID", str(pid), "/T", "/F"])


def dot_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "DOT2API_DATA_DIR": str(LOG_DIR / "data"),
            "DOT2API_HOST": "127.0.0.1",
            "DOT2API_PORT": "8788",
            "DOT2API_CAPABILITY_URLS": "1",
            "DOT2API_DOH": "1",
            "DOT2API_QUEUE": "dot",
            "DOT2API_COMPLETION_TIMEOUT": "900",
            "DOT2API_TASK_TTL": "7200",
        }
    )
    uv_dir = str(uv_path().parent)
    env["PATH"] = uv_dir + os.pathsep + env.get("PATH", "")
    return env


def uv_path() -> Path:
    return runtime_paths.uv_path()


def cloudflared_path() -> Path:
    return runtime_paths.cloudflared_path()


def dot_ready() -> bool:
    return http_ok("http://127.0.0.1:8788/readyz")


def named_processes(name: str) -> list[psutil.Process]:
    """Filter in Windows before inspecting candidates; never read every process's command line."""
    result = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, errors="replace", check=True, timeout=10,
        creationflags=CREATE_NO_WINDOW,
    )
    found = []
    for row in csv.reader(io.StringIO(result.stdout)):
        if len(row) < 2 or row[0].lower() != name.lower() or not row[1].isdigit():
            continue
        try:
            proc = psutil.Process(int(row[1]))
            # The PID may have exited or been reused since tasklist took its snapshot.
            if proc.name().lower() == name.lower():
                found.append(proc)
        except psutil.Error:
            continue
    return found


_tunnel_lock = threading.Lock()
_tunnel_processes: list[psutil.Process] | None = None
_tunnel_checked = 0.0
TUNNEL_RETRY_SECONDS = 10


def tunnel_pids() -> list[int]:
    """Reuse known tunnel processes until they exit; share discovery across worker threads."""
    global _tunnel_processes, _tunnel_checked
    with _tunnel_lock:
        alive = []
        for proc in _tunnel_processes or []:
            try:
                # Process.is_running also checks creation time, protecting against PID reuse.
                if proc.is_running():
                    alive.append(proc)
            except psutil.Error:
                pass
        if alive:
            _tunnel_processes = alive
            return [proc.pid for proc in alive]
        if _tunnel_processes == [] and time.monotonic() - _tunnel_checked < TUNNEL_RETRY_SECONDS:
            return []
        found = []
        for proc in named_processes("cloudflared.exe"):
            try:
                arguments = proc.cmdline()
                if ("tunnel" in arguments and any(
                        arguments[index + 1] in ("http://127.0.0.1:8788", "http://localhost:8788")
                        for index, part in enumerate(arguments[:-1]) if part == "--url")):
                    found.append(proc)
            except psutil.Error:
                continue
        _tunnel_processes = found
        _tunnel_checked = time.monotonic()
        return [proc.pid for proc in found]


def start_dot2api() -> str:
    global _tunnel_processes
    messages = []
    if dot_ready():
        messages.append(_tr("Dot2API 已在运行"))
    else:
        hidden_popen(
            [str(uv_path()), "run", "--no-sync", "dot2api", "--data-dir", str(LOG_DIR / "data"), "serve"],
            dot_env(),
            DOT_ROOT,
            LOG_DIR / "serve.log",
        )
        messages.append(_tr("已启动 Dot2API"))
    if tunnel_pids():
        messages.append(_tr("隧道已在运行"))
    else:
        log = LOG_DIR / "tunnel.log"
        log.write_bytes(b"")
        tunnel = hidden_popen(
            [
                str(cloudflared_path()),
                "tunnel",
                "--url",
                "http://127.0.0.1:8788",
                "--protocol",
                "http2",
                "--no-autoupdate",
            ],
            os.environ.copy(),
            ROOT,
            log,
        )
        with _tunnel_lock:
            _tunnel_processes = [psutil.Process(tunnel.pid)]
        messages.append(_tr("已启动隧道"))
    return "；".join(messages)


def stop_dot2api(tunnel: bool = True) -> str:
    """Stop the server and, unless told to keep it, the tunnel (a new tunnel gets a new public address)."""
    pid = listener_pid(8788)
    if pid:
        kill_pid(pid)
    if tunnel:
        for tunnel_pid in tunnel_pids():
            kill_pid(tunnel_pid)
    return _tr("已停止 Dot2API") if tunnel else _tr("已停止 Dot2API 服务（隧道保留）")


def publish_tunnel(url: str) -> bool:
    path = LOG_DIR / "connect.json"
    if not path.is_file():
        return False
    data = json.loads(path.read_text(encoding="utf-8"))
    token = data.get("dot_token", "")
    if not token or data.get("public_base_url") == url:
        data["public_base_url"] = url
        if token and not data.get("mcp_url"):
            data["mcp_url"] = f"{url}/mcp/{token}"
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return False
    data["public_base_url"] = url
    data["mcp_url"] = f"{url}/mcp/{token}"
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


def capture_tunnel_url(timeout: float = 30) -> str:
    log = LOG_DIR / "tunnel.log"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log.is_file():
            found = TUNNEL_URL.findall(log.read_text(encoding="utf-8", errors="replace"))
            if found:
                publish_tunnel(found[-1])
                return found[-1]
        time.sleep(1)
    return ""


def bridge_home() -> Path:
    return Path.home() / ".codex-chatgpt-web-consult"


def bridge_port() -> int:
    config = json.loads((bridge_home() / "config.json").read_text(encoding="utf-8"))
    if config.get("mode") != "browser-only" or config.get("host") != "127.0.0.1":
        raise RuntimeError(_tr("ChatGPT Web 配置不是本机 Browser-only"))
    return int(config["port"])


def chatgpt_ready() -> bool:
    try:
        port = bridge_port()
    except (OSError, json.JSONDecodeError, RuntimeError, ValueError):
        return False
    return http_ok(f"http://127.0.0.1:{port}/healthz")


def chatgpt_exe() -> Path:
    """The Electron binary that runs the launcher straight from its source tree."""
    return CHATGPT_LAUNCHER / "node_modules" / "electron" / "dist" / "electron.exe"


def chatgpt_command(*flags: str) -> list[str]:
    return [str(chatgpt_exe()), str(CHATGPT_LAUNCHER), *flags]


def chatgpt_built() -> bool:
    """Everything the launcher needs before it can start: Electron, Bun, the built window and the browser helper."""
    return all(path.is_file() for path in (
        chatgpt_exe(), BUN, CHATGPT_LAUNCHER / "dist" / "index.html", CHATGPT_ROOT / ".launcher-runtime" / "browser-helper.cjs"))


def chatgpt_env() -> dict[str, str]:
    env = os.environ.copy()
    env["CODEX_HOME"] = str(Path.home() / ".codex-pro-consult")
    env["CODEX_CHATGPT_WEB_HOME"] = str(bridge_home())
    env["CODEX_WEB_GPT_LAUNCHER_DATA_DIR"] = str(
        Path.home() / "AppData" / "Local" / "Codex Pro Consult"
    )
    env["CODEX_WEB_GPT_BUN"] = env["CODEX_CHATGPT_WEB_BUN"] = str(BUN)
    # The patched launcher then shows no tray icon of its own and hides, rather than quits, when its window closes.
    env["CODEX_WEB_GPT_EXTERNAL_TRAY"] = "1"
    return env


def launcher_process(name: str | None, arguments: list[str] | None) -> bool:
    """A process of the launcher's Electron binary: the main one, or one of its Chromium helpers."""
    return (name or "").lower() == "electron.exe" and bool(arguments) and Path(arguments[0]) == chatgpt_exe()


def chatgpt_main_pid() -> int | None:
    """The launcher's main process: parent of the bridge runtime that owns the port, else the one that is no helper."""
    try:
        runtime = listener_pid(bridge_port())
    except (OSError, json.JSONDecodeError, RuntimeError, ValueError):
        runtime = None
    if runtime:
        try:
            parent = psutil.Process(runtime).parent()
            if parent is not None and launcher_process(parent.name(), parent.cmdline()):
                return parent.pid
        except psutil.Error:
            pass
    for proc in named_processes("electron.exe"):
        try:
            arguments = proc.cmdline()
            if not launcher_process(proc.name(), arguments):
                continue
            if not any(part.startswith("--type=") or part.endswith("browser-helper.cjs") or part == "--quit" for part in arguments[1:]):
                return proc.pid
        except psutil.Error:
            continue
    return None


def chatgpt_busy() -> bool:
    try:
        health = http_json(f"http://127.0.0.1:{bridge_port()}/healthz")
    except (OSError, json.JSONDecodeError, RuntimeError, ValueError):
        return False
    return bool(health and (health.get("active_http_turns") or health.get("active_browser_turns")))


def start_chatgpt_web(show: bool = False) -> str:
    if not show and chatgpt_main_pid() is not None:
        # Already running, or still starting: a second `--hidden` launch would raise its window.
        return _tr("ChatGPT Web 已在运行") if chatgpt_ready() else _tr("ChatGPT Web 正在启动")
    if not chatgpt_built():
        return _tr("ChatGPT Web 还没有构建，先运行 deploy.py install")
    hidden_popen(chatgpt_command() if show else chatgpt_command("--hidden"), chatgpt_env(), CHATGPT_LAUNCHER,
                 STATE_DIR / "chatgpt-web.log")
    return _tr("已打开 ChatGPT Web 窗口") if show else _tr("已启动 ChatGPT Web")


def stop_chatgpt_web(wait: float = 20.0) -> str:
    """Ask the launcher to quit through its second-instance hook; end it if it ignores that (an unpatched launcher)."""
    main = chatgpt_main_pid()
    if main is None:
        return _tr("ChatGPT Web 未在运行")
    if chatgpt_busy():
        return _tr("ChatGPT Web 正在处理请求，稍后再停")
    hidden_popen(chatgpt_command("--quit"), chatgpt_env(), CHATGPT_LAUNCHER, STATE_DIR / "chatgpt-web.log")
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline and psutil.pid_exists(main):
        time.sleep(0.5)
    forced = psutil.pid_exists(main)
    if forced:
        kill_pid(main)
    try:
        leftover = listener_pid(bridge_port())
    except (OSError, json.JSONDecodeError, RuntimeError, ValueError):
        leftover = None
    if leftover:
        kill_pid(leftover)
    return _tr("已停止 ChatGPT Web（强制结束）") if forced else _tr("已停止 ChatGPT Web")


# Gemini Web: route_manager/gemini-web, the installed Chrome on a dedicated, signed-in profile for
# Gemini / AI Studio / Flow tasks. It is a visible browser, so it starts on demand (tray click or a
# client's first task), not with the tray.
GEMINI_ROOT = plugins.plugin_root("gemini-web")
_gemini_module = None


def gemini_web():
    global _gemini_module
    if _gemini_module is None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("gemini_web", GEMINI_ROOT / "gemini_web.py")
        _gemini_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_gemini_module)
    return _gemini_module


def gemini_built() -> bool:
    return (GEMINI_ROOT / "gemini_web.py").is_file() and gemini_web().CHROME.is_file()


def start_gemini_web(show: bool = False) -> str:
    if not gemini_built():
        return _tr("Gemini Web 未安装（缺 gemini-web/gemini_web.py 或 Chrome）")
    return gemini_web().start(show=show)


def stop_gemini_web() -> str:
    if not gemini_built():
        return _tr("Gemini Web 未安装")
    return gemini_web().stop()


def gemini_label() -> str:
    if not gemini_built():
        return _tr("未安装")
    module = gemini_web()
    if module.health():
        return _tr("运行中")
    return _tr("启动中") if module.main_pid() is not None else _tr("已停止")


# Grok2API and Muse2API: local OpenAI/Anthropic-compatible gateways (tray/gateways.py), stopped by the port they own.
def grok_ready() -> bool:
    return gateways.grok_ready(http_ok)


def start_grok2api() -> str:
    if grok_ready():
        return _tr("Grok2API 已在运行")
    if not gateways.grok_built():
        return _tr("Grok2API 还没有构建，先在面板里检查更新")
    hidden_popen(gateways.grok_command(), os.environ.copy(), gateways.GROK_HOME, gateways.GROK_LOG)
    return _tr("已启动 Grok2API")


def stop_grok2api() -> str:
    pid = listener_pid(gateways.GROK_PORT)
    if not pid:
        return _tr("Grok2API 未在运行")
    kill_pid(pid)
    return _tr("已停止 Grok2API")


def grok_label() -> str:
    if "grok2api" in UPDATING_COMPONENTS:
        return _tr("更新中")
    if not gateways.grok_built():
        return _tr("未安装")
    if grok_ready():
        return _tr("运行中") if gateways.grok_serving(http_ok) else _tr("运行中·待添加账号")
    return _tr("启动中") if listener_pid(gateways.GROK_PORT) is not None else _tr("已停止")


def muse_ready() -> bool:
    return gateways.muse_ready(http_ok)


def start_muse2api() -> str:
    if muse_ready():
        return _tr("Muse2API 已在运行")
    if not gateways.muse_built():
        return _tr("Muse2API 还没有安装依赖，先在面板里检查更新")
    gateways.MUSE_HOME.mkdir(parents=True, exist_ok=True)
    hidden_popen(gateways.muse_command(), gateways.muse_env(), gateways.MUSE_HOME, gateways.MUSE_LOG)
    return _tr("已启动 Muse2API")


def stop_muse2api() -> str:
    pid = listener_pid(gateways.MUSE_PORT)
    if not pid:
        return _tr("Muse2API 未在运行")
    kill_pid(pid)    # /T also ends the headless Chrome the browser driver started
    return _tr("已停止 Muse2API")


def muse_label() -> str:
    if "muse2api" in UPDATING_COMPONENTS:
        return _tr("更新中")
    if not gateways.muse_built():
        return _tr("未安装")
    if muse_ready():
        return _tr("运行中")
    return _tr("启动中") if listener_pid(gateways.MUSE_PORT) is not None else _tr("已停止")


def dot_label() -> str:
    if not dot_ready():
        return _tr("已停止")
    if tunnel_pids():
        return _tr("运行中")
    return _tr("服务已开，隧道未开")


def chatgpt_label() -> str:
    if chatgpt_ready():
        return _tr("运行中")
    return _tr("启动中") if chatgpt_main_pid() is not None else _tr("已停止")


def node_path() -> Path:
    return runtime_paths.node_path()


def usage_float_label() -> str:
    if local_controls.read_snapshot("usage-float"):
        return _tr("运行中")
    child = _component_children.get("usage-float")
    return _tr("启动中") if child and child.poll() is None else _tr("已停止")


def stop_component_ui(component: str, timeout: float = 15) -> str:
    snapshot = local_controls.read_snapshot(component)
    if not snapshot:
        return _tr('{0} 界面未运行').format(f'{component}')
    process = psutil.Process(snapshot["pid"])
    local_controls.send_action(component, "quit")
    deadline = time.monotonic() + timeout
    while process.is_running() and time.monotonic() < deadline:
        time.sleep(0.05)
    if process.is_running():
        raise TimeoutError(_tr('{0} 界面尚未退出，保持当前版本').format(f'{component}'))
    return _tr('已关闭 {0} 界面').format(f'{component}')


def start_usage_float() -> str:
    if not USAGE_FLOAT_SCRIPT.is_file():
        return _tr("Usage Float 未安装")
    with _component_lock:
        child = _component_children.get("usage-float")
        if local_controls.read_snapshot("usage-float") or (child and child.poll() is None):
            return _tr("Usage Float 已在运行")
        env = os.environ.copy()
        env["ROUTE_MANAGER_HOSTED"] = "1"
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        _component_children["usage-float"] = hidden_popen(
            [str(pythonw), str(ROOT / "tray" / "usage_float_host.py")], env, USAGE_FLOAT_ROOT,
            STATE_DIR / "usage-float.log")
    return _tr("已启动 Usage Float")


def component_action(component: str, action: str) -> str:
    if component in UPDATING_COMPONENTS:
        return _tr('{0} 更新中，请稍后操作').format(f'{component}')
    if local_controls.read_snapshot(component) is None:
        if component == "usage-float":
            start_usage_float()
        else:
            raise ValueError("Unknown component")
    deadline = time.monotonic() + 20
    while local_controls.read_snapshot(component) is None:
        if time.monotonic() >= deadline:
            raise TimeoutError(_tr('{0} 界面未就绪，详见日志').format(f'{component}'))
        time.sleep(0.1)
    return local_controls.send_action(component, action)


def start_all(config: dict | None = None) -> str:
    """Login start: each plugin when enabled and set to start with the tray."""
    config = config or plugins.load()
    messages = []
    if plugins.autostarts("dot2api", config):
        messages.append(start_dot2api())
    if plugins.autostarts("chatgpt-web", config):
        messages.append(start_chatgpt_web(False))
    if plugins.autostarts("grok2api", config):
        messages.append(start_grok2api())
    if plugins.autostarts("muse2api", config):
        messages.append(start_muse2api())
    if plugins.autostarts("usage-float", config) and USAGE_FLOAT_SCRIPT.is_file():
        messages.append(start_usage_float())
    if plugins.autostarts("gemini-web", config):
        messages.append(start_gemini_web(False))
    if plugins.autostarts("easytier", config):
        started = easytier.start_if_stopped()
        if started:
            messages.append(started)
    return "；".join(messages) if messages else _tr("核心服务已就绪")


def restart_services(config: dict | None = None) -> str:
    """Restart the enabled bridges but keep the tunnel: a new tunnel means a new public address."""
    config = config or plugins.load()
    dot, web = plugins.enabled("dot2api", config), plugins.enabled("chatgpt-web", config)
    grok, muse = plugins.enabled("grok2api", config), plugins.enabled("muse2api", config)
    messages = []
    if dot:
        messages.append(stop_dot2api(tunnel=False))
    if web:
        messages.append(stop_chatgpt_web())
    if grok:
        messages.append(stop_grok2api())
    if muse:
        messages.append(stop_muse2api())
    time.sleep(1)
    if dot:
        messages.append(start_dot2api())
    if web:
        messages.append(start_chatgpt_web(False))
    if grok:
        messages.append(start_grok2api())
    if muse:
        messages.append(start_muse2api())
    return "；".join(messages) if messages else _tr("没有需要重启的服务")


def services_up(config: dict | None = None) -> bool:
    config = config or plugins.load()
    ready = True
    if plugins.enabled("dot2api", config):
        ready = ready and dot_ready()
    if plugins.enabled("chatgpt-web", config):
        ready = ready and chatgpt_ready()
    if plugins.autostarts("grok2api", config):
        ready = ready and grok_ready()
    if plugins.autostarts("muse2api", config):
        ready = ready and muse_ready()
    return ready


def refresh_plugin_roots() -> None:
    """Refresh module-level root paths after a plugin is cloned into ~/.route-manager/plugins/."""
    global DOT_ROOT, CHATGPT_ROOT, CHATGPT_LAUNCHER, USAGE_FLOAT_ROOT, USAGE_FLOAT_SCRIPT, GEMINI_ROOT, _gemini_module
    DOT_ROOT = plugins.plugin_root("dot2api")
    CHATGPT_ROOT = plugins.plugin_root("chatgpt-web")
    CHATGPT_LAUNCHER = CHATGPT_ROOT / "launcher"
    USAGE_FLOAT_ROOT = plugins.plugin_root("usage-float")
    USAGE_FLOAT_SCRIPT = USAGE_FLOAT_ROOT / "usage_float.py"
    GEMINI_ROOT = plugins.plugin_root("gemini-web")
    _gemini_module = None


# Start / stop per plugin, used by the main panel.
def plugin_start(plugin_id: str) -> str:
    if plugin_id not in plugins.BY_ID:
        raise ValueError(f"unknown plugin {plugin_id}")
    spec = plugins.BY_ID[plugin_id]
    root = plugins.plugin_root(plugin_id)
    if spec.repo and not (root / ".git").exists():
        import updates

        updates.ensure_plugin(plugin_id)
        refresh_plugin_roots()
    elif plugin_id in gateways.IDS and not gateways.built(plugin_id):
        import updates

        updates.prepare_gateway(plugin_id)    # cloned earlier but the build did not finish
    if plugin_id == "dot2api":
        return start_dot2api()
    if plugin_id == "chatgpt-web":
        return start_chatgpt_web(False)
    if plugin_id == "grok2api":
        return start_grok2api()
    if plugin_id == "muse2api":
        return start_muse2api()
    if plugin_id == "gemini-web":
        return start_gemini_web(False)
    if plugin_id == "easytier":
        return easytier.start()
    if plugin_id == "usage-float":
        return start_usage_float()
    raise ValueError(f"unknown plugin {plugin_id}")


def plugin_stop(plugin_id: str) -> str:
    if plugin_id == "dot2api":
        return stop_dot2api()
    if plugin_id == "chatgpt-web":
        return stop_chatgpt_web()
    if plugin_id == "grok2api":
        return stop_grok2api()
    if plugin_id == "muse2api":
        return stop_muse2api()
    if plugin_id == "gemini-web":
        return stop_gemini_web()
    if plugin_id == "easytier":
        return easytier.stop()
    if plugin_id == "usage-float":
        return stop_component_ui(plugin_id)
    raise ValueError(f"unknown plugin {plugin_id}")


def plugin_label(plugin_id: str) -> str:
    if plugin_id == 'grok-bot':
        import grok_bot_cli
        return grok_bot_cli.snapshot()['label']
    return {"dot2api": dot_label, "chatgpt-web": chatgpt_label, "gemini-web": gemini_label,
            "grok2api": grok_label, "muse2api": muse_label,
            "easytier": lambda: easytier.label(easytier.snapshot()), "usage-float": usage_float_label}[plugin_id]()


RENEW_WINDOW = 2 * 60 * 60
RENEW_STATE = STATE_DIR / "renewal.json"


def renewal_decision(
    remaining: int | None,
    task_status: str | None,
    told_missing: bool,
    told_stuck: bool,
) -> str | None:
    """Decide whether the tray should wake the Dot to renew, or tell the user."""
    if remaining is None or remaining <= 0:
        return None if told_missing else "missing"
    if remaining > RENEW_WINDOW or task_status in ("queued", "running"):
        return None
    if task_status in ("completed", "failed", "cancelled", "expired"):
        return None if told_stuck else "stuck"
    return "submit"


def _subscription_expires() -> float | None:
    database = LOG_DIR / "data" / "dot2api.sqlite3"
    if not database.is_file():
        return None
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT expires FROM subscriptions WHERE queue='dot' AND name='task.available' AND active=1"
        ).fetchone()
    finally:
        connection.close()
    return None if row is None else float(row[0])


def _renew_status(expires: float) -> str | None:
    database = LOG_DIR / "data" / "dot2api.sqlite3"
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT status FROM tasks WHERE idempotency_key=? ORDER BY created DESC LIMIT 1",
            (f"renew-{int(expires)}",),
        ).fetchone()
    finally:
        connection.close()
    return None if row is None else str(row[0])


def _renew_state() -> dict:
    if not RENEW_STATE.is_file():
        return {}
    try:
        data = json.loads(RENEW_STATE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _save_renew_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    RENEW_STATE.write_text(json.dumps(state), encoding="utf-8")


def _submit_renewal(expires: float) -> None:
    client = json.loads((LOG_DIR / "client.json").read_text(encoding="utf-8"))
    text = (
        "这是订阅续期。先再次订阅队列 dot 的 task.available，回调方式和上次相同。"
        "然后完成这条任务，result 为 {\"content\":\"续期已提交\"}。不要做其他事。"
    )
    body = json.dumps(
        {
            "queue": "dot",
            "instructions": text,
            "payload": {"messages": [{"role": "user", "content": text}], "marker": "dot-subscription-renew"},
            "idempotency_key": f"renew-{int(expires)}",
            "ttl_seconds": 3600,
        }
    ).encode()
    request = urllib.request.Request(
        str(client["base_url"]).rstrip("/") + "/v1/tasks",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {client['api_key']}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()


def maintain_subscription() -> str | None:
    """Renew through the normal task queue during the last two hours, or say when that cannot work."""
    expires = _subscription_expires()
    remaining = None if expires is None else int(expires - time.time())
    status = None if expires is None else _renew_status(expires)
    state = _renew_state()
    if remaining is not None and remaining > RENEW_WINDOW:
        if state:
            _save_renew_state({})
        return None
    action = renewal_decision(
        remaining,
        status,
        bool(state.get("told_missing")),
        state.get("told_stuck") == (int(expires) if expires is not None else None),
    )
    if action == "missing":
        state["told_missing"] = True
        _save_renew_state(state)
        return _tr("Dot 订阅已失效。打开 ChatGPT，让 Dot 再订阅一次队列 dot 的 task.available。")
    if action == "stuck":
        state["told_stuck"] = int(expires)
        _save_renew_state(state)
        return _tr("续期任务没有延长订阅。打开 ChatGPT，让 Dot 再订阅一次。")
    if action == "submit":
        _submit_renewal(expires)
        return _tr("已请 Dot 续订。它会在队列里收到这条任务。")
    return None


# Capacity statistics: worker-hours and burn rates from the session logs, for the router's allocator.
CAPACITY_SCRIPT = ROOT / "tray" / "capacity_stats.py"
CAPACITY_OUTPUT = STATE_DIR / "capacity.json"
CAPACITY_INTERVAL = 300
_capacity: dict = {"proc": None, "started": None}


def capacity_tick(force: bool = False) -> bool:
    """Start one statistics pass in a low-priority child unless one is running or the last was under 5 minutes ago."""
    proc = _capacity["proc"]
    if proc is not None and proc.poll() is None:
        return False
    started = _capacity["started"]
    if not force and started is not None and time.monotonic() - started < CAPACITY_INTERVAL:
        return False
    _capacity["started"] = time.monotonic()
    _capacity["proc"] = hidden_popen(
        [sys.executable, str(CAPACITY_SCRIPT), "--once", "--quiet"],
        os.environ.copy(),
        ROOT,
        STATE_DIR / "capacity.log",
        BELOW_NORMAL_PRIORITY_CLASS,
    )
    return True


def stop_capacity() -> None:
    proc = _capacity["proc"]
    if proc is not None and proc.poll() is None:
        kill_pid(proc.pid)
