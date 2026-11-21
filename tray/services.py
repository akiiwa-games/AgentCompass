"""Start and stop Dot2API and ChatGPT Web without a console window."""

from __future__ import annotations

from i18n import translate as _tr

import csv
from datetime import datetime, timedelta, timezone
import importlib
import io
import json
import os
import re
import shutil
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
import component_launch
import plugins
import runtime_paths

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))
_SKILL_SCRIPTS = ROOT / "skills" / "agent-compass" / "scripts"
if str(_SKILL_SCRIPTS) not in sys.path:
    sys.path.append(str(_SKILL_SCRIPTS))
DOT_ROOT = plugins.plugin_root("dot2api")
CHATGPT_ROOT = plugins.plugin_root("chatgpt-web")
CHATGPT_LAUNCHER = CHATGPT_ROOT / "launcher"
USAGE_FLOAT_ROOT = plugins.plugin_root("usage-float")
USAGE_FLOAT_SCRIPT = USAGE_FLOAT_ROOT / "usage_float.py"
_component_lock = threading.Lock()
_component_children = {}
UPDATING_COMPONENTS = set()
BUN = ROOT / "tools" / "bun" / "bun.exe"   # workspace copy only; use bun_exe() for the one that runs
STATE_DIR = Path.home() / ".agent-compass"
LOG_DIR = Path.home() / ".dot2api"
TUNNEL_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def safe_cwd(cwd: Path | str | None = None) -> Path:
    """Return a working directory that never sits inside ``<install_root>/app``."""
    default_dir = Path(sys.executable).resolve().parent if component_launch.frozen() else ROOT
    try:
        target = Path(cwd or default_dir).resolve()
    except OSError:
        target = default_dir
    for parent in (target, *target.parents):
        if parent.name.lower() in ("current", "previous") and parent.parent.name.lower() == "app":
            inst_root = parent.parent.parent
            return inst_root if inst_root.is_dir() else Path.home()
        if parent.name.lower() == "app" and (parent.parent / "runtime-info.json").is_file():
            return parent.parent
    return target if target.is_dir() else default_dir


def hidden_popen(args: list[str], env: dict[str, str], cwd: Path, log_path: Path, extra_flags: int = 0) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    startup = subprocess.STARTUPINFO()
    exe_name = Path(args[0]).name.lower() if args else ""
    if "--hidden" in args or "--quit" in args or not exe_name.startswith(("electron", "pythonw", "agentcompass")):
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
    with open(log_path, "ab", buffering=0) as log:
        return subprocess.Popen(
            args,
            cwd=str(safe_cwd(cwd)),
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
            "DOT2API_PORT": str(dot_port()),
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


def dot_port() -> int:
    from agentcompass import ports
    return ports.get("dot2api")


def uv_path() -> Path:
    return runtime_paths.uv_path()


def cloudflared_path() -> Path:
    return runtime_paths.cloudflared_path()


def dot_ready() -> bool:
    return http_ok(f"http://127.0.0.1:{dot_port()}/readyz")


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
                dot_urls = {f"http://127.0.0.1:{dot_port()}", f"http://localhost:{dot_port()}"}
                if ("tunnel" in arguments and any(
                        arguments[index + 1] in dot_urls
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
        try:
            uv = uv_path()
        except runtime_paths.ToolMissing as error:
            return str(error)
        hidden_popen(
            [str(uv), "run", "--no-sync", "dot2api", "--data-dir", str(LOG_DIR / "data"), "serve"],
            dot_env(),
            DOT_ROOT,
            LOG_DIR / "serve.log",
        )
        messages.append(_tr("已启动 Dot2API"))
    if tunnel_pids():
        messages.append(_tr("隧道已在运行"))
    else:
        try:
            cloudflared = cloudflared_path()
        except runtime_paths.ToolMissing as error:
            messages.append(str(error))
            return "；".join(messages)
        log = LOG_DIR / "tunnel.log"
        log.write_bytes(b"")
        tunnel = hidden_popen(
            [
                str(cloudflared),
                "tunnel",
                "--url",
                f"http://127.0.0.1:{dot_port()}",
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
    pid = listener_pid(dot_port())
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


def bun_exe() -> Path | None:
    """The Bun that will run: the workspace copy, AGENTCOMPASS_BUN, PATH or ~/.bun. None when there is none."""
    try:
        return runtime_paths.bun_path()
    except runtime_paths.ToolMissing:
        return None


def chatgpt_built() -> bool:
    """Everything the launcher needs before it can start: Electron, Bun, the built window and the browser helper."""
    return bun_exe() is not None and all(path.is_file() for path in (
        chatgpt_exe(), CHATGPT_LAUNCHER / "dist" / "index.html", CHATGPT_ROOT / ".launcher-runtime" / "browser-helper.cjs"))


def launcher_data_dir() -> Path:
    """The launcher's data folder: the user's choice, else under %LOCALAPPDATA% (which may be redirected)."""
    configured = os.environ.get("CODEX_WEB_GPT_LAUNCHER_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) if base else Path.home() / "AppData" / "Local") / "Codex Pro Consult"


def chatgpt_env() -> dict[str, str]:
    env = os.environ.copy()
    # The bridge keeps its own Codex profile so it never touches the user's real ~/.codex.
    env["CODEX_HOME"] = os.environ.get("AGENTCOMPASS_CHATGPT_CODEX_HOME") or str(Path.home() / ".codex-pro-consult")
    env["CODEX_CHATGPT_WEB_HOME"] = str(bridge_home())
    env["CODEX_WEB_GPT_LAUNCHER_DATA_DIR"] = str(launcher_data_dir())
    bun = bun_exe()
    if bun is not None:
        env["CODEX_WEB_GPT_BUN"] = env["CODEX_CHATGPT_WEB_BUN"] = str(bun)
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
        message = _tr("ChatGPT Web 还没有构建，先运行 deploy.py install")
        if bun_exe() is None:
            try:
                runtime_paths.bun_path()
            except runtime_paths.ToolMissing as error:
                return f"{message}；{error}"
        return message
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
    try:
        directory = Path(os.environ.get("ROUTER_STATE_DIR") or STATE_DIR)
        status = json.loads((directory / "mpv-provision-status.json").read_text(encoding="utf-8"))
        if status.get("status") in ("warning", "interrupted"):
            return status["message"]
    except (OSError, ValueError, KeyError):
        pass
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
        env["AGENTCOMPASS_HOSTED"] = "1"
        _component_children["usage-float"] = hidden_popen(
            component_launch.command(ROOT / "tray" / "usage_float_host.py"), env, USAGE_FLOAT_ROOT,
            STATE_DIR / "usage-float.log")
    return _tr("已启动 Usage Float")


def component_action(component: str, action: str) -> str:
    if component in UPDATING_COMPONENTS:
        return _tr('{0} 更新中，请稍后操作').format(f'{component}')
    if local_controls.read_snapshot(component) is None:
        if component == "usage-float":
            if not USAGE_FLOAT_SCRIPT.is_file():
                plugin_start("usage-float")
            else:
                start_usage_float()
            if not USAGE_FLOAT_SCRIPT.is_file():
                return _tr("Usage Float 未安装")
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
    if plugins.autostarts("muse2api", config):
        messages.append(start_muse2api())
    if plugins.autostarts("usage-float", config) and USAGE_FLOAT_SCRIPT.is_file():
        messages.append(start_usage_float())
    if plugins.autostarts("easytier", config):
        started = easytier.start_if_stopped()
        if started:
            messages.append(started)
    return "；".join(messages) if messages else _tr("核心服务已就绪")


def restart_services(config: dict | None = None) -> str:
    """Restart the enabled bridges but keep the tunnel: a new tunnel means a new public address."""
    config = config or plugins.load()
    dot, web = plugins.enabled("dot2api", config), plugins.enabled("chatgpt-web", config)
    muse = plugins.enabled("muse2api", config)
    messages = []
    if dot:
        messages.append(stop_dot2api(tunnel=False))
    if web:
        messages.append(stop_chatgpt_web())
    if muse:
        messages.append(stop_muse2api())
    time.sleep(1)
    if dot:
        messages.append(start_dot2api())
    if web:
        messages.append(start_chatgpt_web(False))
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
    if plugins.autostarts("muse2api", config):
        ready = ready and muse_ready()
    return ready


def refresh_plugin_roots() -> None:
    """Refresh module-level root paths after a plugin is cloned into ~/.agent-compass/plugins/."""
    global DOT_ROOT, CHATGPT_ROOT, CHATGPT_LAUNCHER, USAGE_FLOAT_ROOT, USAGE_FLOAT_SCRIPT
    DOT_ROOT = plugins.plugin_root("dot2api")
    CHATGPT_ROOT = plugins.plugin_root("chatgpt-web")
    CHATGPT_LAUNCHER = CHATGPT_ROOT / "launcher"
    USAGE_FLOAT_ROOT = plugins.plugin_root("usage-float")
    USAGE_FLOAT_SCRIPT = USAGE_FLOAT_ROOT / "usage_float.py"


# Start / stop per plugin, used by the main panel.
def plugin_start(plugin_id: str) -> str:
    if plugin_id not in plugins.BY_ID:
        raise ValueError(f"unknown plugin {plugin_id}")
    spec = plugins.BY_ID[plugin_id]
    root = plugins.plugin_root(plugin_id)
    install_message = ""
    if spec.repo and not (root / ".git").exists():
        import updates

        install_message = updates.ensure_plugin(plugin_id)
        refresh_plugin_roots()
    elif plugin_id in gateways.IDS and not gateways.built(plugin_id):
        import updates

        updates.prepare_gateway(plugin_id)    # cloned earlier but the build did not finish
    if plugin_id == "dot2api":
        return start_dot2api()
    if plugin_id == "chatgpt-web":
        return start_chatgpt_web(False)
    if plugin_id == "muse2api":
        return start_muse2api()
    if plugin_id == "easytier":
        return easytier.start()
    if plugin_id == "usage-float":
        message = start_usage_float()
        return message + ("；" + install_message if install_message else "")
    raise ValueError(f"unknown plugin {plugin_id}")


def plugin_stop(plugin_id: str) -> str:
    if plugin_id == "dot2api":
        return stop_dot2api()
    if plugin_id == "chatgpt-web":
        return stop_chatgpt_web()
    if plugin_id == "muse2api":
        return stop_muse2api()
    if plugin_id == "easytier":
        return easytier.stop()
    if plugin_id == "usage-float":
        return stop_component_ui(plugin_id)
    raise ValueError(f"unknown plugin {plugin_id}")


def plugin_label(plugin_id: str) -> str:
    if plugin_id == 'grok-bot':
        import grok_bot_cli
        return grok_bot_cli.snapshot()['label']
    return {"dot2api": dot_label, "chatgpt-web": chatgpt_label, "muse2api": muse_label,
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
    child_cwd = Path(sys.executable).resolve().parent if component_launch.frozen() else ROOT
    _capacity["proc"] = hidden_popen(
        component_launch.command(CAPACITY_SCRIPT, "--once", "--quiet", windowed=False),
        os.environ.copy(),
        child_cwd,
        STATE_DIR / "capacity.log",
        BELOW_NORMAL_PRIORITY_CLASS,
    )
    return True


def stop_capacity() -> None:
    proc = _capacity["proc"]
    if proc is not None and proc.poll() is None:
        kill_pid(proc.pid)


# Usage collection: the router's own quota collector (skill script usage_collector.py), independent of Usage Float.
USAGE_COLLECT_SCRIPT = ROOT / "tray" / "usage_collect.py"
USAGE_COLLECT_INTERVAL = 300
_usage_collect: dict = {"proc": None, "started": None}


def usage_collect_tick(force: bool = False) -> bool:
    """Start one collection pass in a low-priority child unless one is running or the last was under 5 minutes ago."""
    proc = _usage_collect["proc"]
    if proc is not None and proc.poll() is None:
        return False
    started = _usage_collect["started"]
    if not force and started is not None and time.monotonic() - started < USAGE_COLLECT_INTERVAL:
        return False
    _usage_collect["started"] = time.monotonic()
    child_cwd = Path(sys.executable).resolve().parent if component_launch.frozen() else ROOT
    _usage_collect["proc"] = hidden_popen(
        component_launch.command(USAGE_COLLECT_SCRIPT, windowed=False),
        os.environ.copy(),
        child_cwd,
        STATE_DIR / "usage-collect.log",
        BELOW_NORMAL_PRIORITY_CLASS,
    )
    return True


def stop_usage_collect() -> None:
    proc = _usage_collect["proc"]
    if proc is not None and proc.poll() is None:
        kill_pid(proc.pid)


# Dispatch notice recovery: restarts killed Paseo watchers (e.g. Antigravity's Windows job object) and sends due end notices.
DISPATCH_NOTICE_INTERVAL = 30
DISPATCH_NOTICE_MAX_INTERVAL = 600
DISPATCH_NOTICE_LOG_MAX_BYTES = 1_000_000
PASEO_REACHABLE_TIMEOUT = 0.2
_DISPATCH_NOTICE_ACTIVE = ("launching", "launched", "started")
_WATCHER_DONE_STATUSES = frozenset(
    {
        "delivered",
        "parent_unavailable",
        "delivery_failed",
        "creation_failed",
        "relaunched",
        "canceled",
        "resumed",
        "continued",
        "undelivered",
    }
)
_dispatch_notice: dict = {
    "proc": None,
    "started": None,
    "last_reasons": None,
    "interval": DISPATCH_NOTICE_INTERVAL,
    "doubled": False,
    "evaluated_reasons": None,
}


def _reset_dispatch_notice() -> None:
    _dispatch_notice.update(
        proc=None,
        started=None,
        last_reasons=None,
        interval=DISPATCH_NOTICE_INTERVAL,
        doubled=False,
        evaluated_reasons=None,
    )


def _reset_dispatch_notice_backoff() -> None:
    _dispatch_notice.update(
        last_reasons=None,
        interval=DISPATCH_NOTICE_INTERVAL,
        doubled=False,
    )


def _dispatch_state_dir() -> Path:
    return Path(os.environ["ROUTER_STATE_DIR"]) if os.environ.get("ROUTER_STATE_DIR") else STATE_DIR


def _dispatch_db_path() -> Path:
    return Path(os.environ["ROUTER_DISPATCH_DB"]) if os.environ.get("ROUTER_DISPATCH_DB") else _dispatch_state_dir() / "dispatch.sqlite3"


def _parse_iso(stamp: object) -> datetime | None:
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def _router(name: str):
    """A router module, imported on first use: every tray helper imports this module and only the notice gate reads the router."""
    return importlib.import_module(name)


def _in_create_send_window(row: dict | None, now: datetime) -> bool:
    if not isinstance(row, dict) or row.get("state") != "launching" or row.get("observed_at"):
        return False
    launched_at = _parse_iso(row.get("launched_at"))
    return launched_at is not None and now < launched_at + timedelta(seconds=_router("dispatch_registry").CREATE_SEND_WINDOW)


def _sqlite_locked(exc: sqlite3.Error) -> bool:
    name = getattr(exc, "sqlite_errorname", "") or ""
    if name in ("SQLITE_BUSY", "SQLITE_LOCKED") or name.startswith(("SQLITE_BUSY_", "SQLITE_LOCKED_")):
        return True
    msg = str(exc).lower()
    return isinstance(exc, sqlite3.OperationalError) and ("locked" in msg or "busy" in msg)


def _paseo_daemon_configured() -> bool:
    return "daemon" in _router("dispatch_flow").paseo_config()


def _safe_dispatch_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 128
        and value[0].isalnum()
        and all(ch.isalnum() or ch in "-_" for ch in value)
    )


def _is_bridge_watcher_cmdline(cmdline: list[str], dispatch_id: str | None = None) -> bool:
    for idx, arg in enumerate(cmdline):
        if arg != "--watch":
            continue
        if not any(Path(prev).name == "paseo_dispatch_bridge.mjs" for prev in cmdline[:idx]):
            continue
        if dispatch_id is not None:
            return idx + 1 < len(cmdline) and cmdline[idx + 1] == dispatch_id
        return True
    return False


def _watcher_process_alive(pid: object, dispatch_id: str) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        proc = psutil.Process(pid)
        if not proc.is_running():
            return False
        cmdline = proc.cmdline()
    except (psutil.Error, OSError, ValueError):
        return False
    return _is_bridge_watcher_cmdline(cmdline, dispatch_id)


def _readable_receipt(dispatch_id: object, state_dir: Path, *, parent_id: object = None) -> dict | None:
    """Readable watcher receipt dict of `dispatch_id` (and matching `parent_id` when given), or None."""
    if not _safe_dispatch_id(dispatch_id):
        return None
    receipt_path = state_dir / "paseo-notifications" / f"{dispatch_id}.json"
    try:
        record = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("dispatch_id") != dispatch_id:
        return None
    rec_parent = record.get("parent_id")
    if not isinstance(rec_parent, str) or not rec_parent:
        return None
    if isinstance(parent_id, str) and parent_id and rec_parent != parent_id:
        return None
    return record


def _watcher_restartable(dispatch_id: object, state_dir: Path) -> bool:
    """True only where `observe` -> `ensureNotification` in `paseo_dispatch_bridge.mjs` would restart `--watch <id>`."""
    record = _readable_receipt(dispatch_id, state_dir)
    if record is None:
        return False
    if record.get("status") in _WATCHER_DONE_STATUSES:
        return False
    if (state_dir / "paseo-notifications" / f"{dispatch_id}.json.creation-failed").exists():
        return False
    return not _watcher_process_alive(record.get("pid"), str(dispatch_id))


def _receipt_proves_ending(row: dict, dispatch_id: object, state_dir: Path) -> bool:
    """True when `row` is not a parent follow-up and its readable receipt already proves an ending to the registry."""
    dispatch_registry = _router("dispatch_registry")
    if dispatch_registry.followed_up(row):
        return False
    record = _readable_receipt(dispatch_id, state_dir)
    if record is None:
        return False
    return dispatch_registry.state_from_receipt(row, record)[0] is not None


def _local_dispatch_notice_reasons(now: datetime | None = None) -> frozenset[tuple[str, str]]:
    """Dispatches and reasons for which a reconcile pass can change something now, before probing the local daemon."""
    db = _dispatch_db_path()
    if not db.is_file():
        return frozenset()
    dispatch_registry = _router("dispatch_registry")
    current = now or dispatch_registry.utcnow()
    try:
        stored = dispatch_registry.read_dispatches(path=db, timeout=0.5)
    except sqlite3.Error as exc:
        return frozenset({("*", "sqlite_locked")}) if _sqlite_locked(exc) else frozenset()
    except (OSError, ValueError):
        return frozenset()

    state_dir = _dispatch_state_dir()
    by_id = {
        r["dispatch_id"]: r
        for r in stored
        if isinstance(r, dict) and isinstance(r.get("dispatch_id"), str)
    }
    reasons: set[tuple[str, str]] = set()
    for row in stored:
        if not isinstance(row, dict) or row.get("dispatch_mode", "paseo") != "paseo":
            continue
        state = row.get("state")
        dispatch_id = row.get("dispatch_id")
        if state in _DISPATCH_NOTICE_ACTIVE:
            if not row.get("launched_at") or _in_create_send_window(row, current):
                continue
            if _watcher_restartable(dispatch_id, state_dir):
                reasons.add((str(dispatch_id), f"watcher:{state}"))
            elif _receipt_proves_ending(row, dispatch_id, state_dir):
                reasons.add((str(dispatch_id), "settle_receipt"))
        elif state == "expired" and row.get("launched_at") and dispatch_registry.later(row.get("hard_until"), current):
            if _watcher_restartable(dispatch_id, state_dir):
                reasons.add((str(dispatch_id), "watcher:expired"))
            elif _receipt_proves_ending(row, dispatch_id, state_dir):
                reasons.add((str(dispatch_id), "settle_receipt"))
        elif dispatch_registry.fallback_guard(row, current):
            if not _safe_dispatch_id(dispatch_id):
                continue
            try:
                watch_id = dispatch_registry.current_attempt(dispatch_id, path=db)
            except sqlite3.Error as exc:
                if _sqlite_locked(exc):
                    reasons.add(("*", "sqlite_locked"))
                continue
            except (OSError, ValueError):
                continue
            cur = by_id.get(watch_id)
            if isinstance(cur, dict) and cur.get("launched_at"):
                if _in_create_send_window(cur, current):
                    continue
                cur_state = cur.get("state")
                if cur_state in _DISPATCH_NOTICE_ACTIVE or (
                    cur_state == "expired" and dispatch_registry.later(cur.get("hard_until"), current)
                ):
                    if _watcher_restartable(watch_id, state_dir):
                        reasons.add((str(watch_id), f"watcher:{cur_state}"))
                elif cur_state == "failed" and cur.get("agent_id") and _watcher_restartable(watch_id, state_dir):
                    reasons.add((str(watch_id), "chain_watcher"))
            else:
                for cand in stored:
                    if (
                        isinstance(cand, dict)
                        and dispatch_registry.chain_root(cand) == dispatch_id
                        and cand.get("state") == "failed"
                        and cand.get("launched_at")
                        and cand.get("agent_id")
                        and _watcher_restartable(cand.get("dispatch_id"), state_dir)
                    ):
                        reasons.add((str(cand["dispatch_id"]), "chain_watcher"))

    try:
        needs = dispatch_registry.needs_reader(path=db, timeout=0.5, now=current)
    except sqlite3.Error as exc:
        if _sqlite_locked(exc):
            reasons.add(("*", "sqlite_locked"))
        return frozenset(reasons)
    except (OSError, ValueError):
        return frozenset(reasons)

    if needs["needed"]:
        for did in needs["observe"]:
            if _watcher_restartable(did, state_dir):
                reasons.add((str(did), "awaiting_fallback"))
        if needs["due"] and _paseo_daemon_configured():
            for did in needs["due"]:
                parent_id = (by_id.get(did) or {}).get("parent_id")
                if _readable_receipt(did, state_dir, parent_id=parent_id) is not None:
                    reasons.add((str(did), "due_notice"))

    return frozenset(reasons)


def dispatch_notice_reasons(now: datetime | None = None) -> frozenset[tuple[str, str]]:
    """Dispatches and reasons for which a reconcile pass can make progress right now."""
    local = _local_dispatch_notice_reasons(now=now)
    if not local:
        return frozenset()
    if not _router("dispatch_flow").paseo_reachable(timeout=PASEO_REACHABLE_TIMEOUT):
        return frozenset()
    return local


def dispatch_notice_needed(now: datetime | None = None) -> bool:
    """True only when a reconcile pass can change something now and the local Paseo daemon answers."""
    reasons = dispatch_notice_reasons(now=now)
    _dispatch_notice["evaluated_reasons"] = reasons
    if not reasons:
        _reset_dispatch_notice_backoff()
        return False
    return True


def dispatch_reconcile_command() -> list[str] | None:
    """argv for `agentcompass dispatch --dispatch-action reconcile` using the console CLI, never the windowed tray or `route_judge.py`."""
    if component_launch.frozen():
        cli = Path(sys.executable).resolve().parent / "cli" / ("agentcompass.exe" if os.name == "nt" else "agentcompass")
        if cli.is_file():
            return [str(cli), "dispatch", "--dispatch-action", "reconcile"]
        found = shutil.which("agentcompass")
        return [found, "dispatch", "--dispatch-action", "reconcile"] if found else None
    scripts = component_launch.skill_scripts()
    if scripts is None:
        return None
    try:
        scripts = scripts.resolve()
    except OSError:
        pass
    python = Path(sys.executable)
    if os.name == "nt" and python.name.lower() == "pythonw.exe":
        console = python.with_name("python.exe")
        if console.is_file():
            python = console
    checkout_pkg = scripts.parents[2] / "agentcompass" if len(scripts.parents) > 2 else None
    if checkout_pkg is not None and (checkout_pkg / "__main__.py").is_file() and (scripts.parents[2] / "pyproject.toml").is_file():
        return [str(python), "-X", "utf8", str(checkout_pkg), "dispatch", "--dispatch-action", "reconcile"]
    installed_pkg = scripts.parents[3] if len(scripts.parents) > 3 else None
    if installed_pkg is not None and installed_pkg.name == "agentcompass" and (installed_pkg / "__main__.py").is_file():
        return [str(python), "-X", "utf8", str(installed_pkg), "dispatch", "--dispatch-action", "reconcile"]
    root_pkg = ROOT / "agentcompass"
    if (root_pkg / "__main__.py").is_file() and (ROOT / "pyproject.toml").is_file():
        return [str(python), "-X", "utf8", str(root_pkg), "dispatch", "--dispatch-action", "reconcile"]
    found = shutil.which("agentcompass")
    return [found, "dispatch", "--dispatch-action", "reconcile"] if found else None


def _bound_dispatch_notice_log(log_path: Path) -> None:
    try:
        if log_path.stat().st_size > DISPATCH_NOTICE_LOG_MAX_BYTES:
            os.replace(log_path, log_path.with_name(log_path.name + ".1"))
    except OSError:
        pass


def dispatch_notice_tick(force: bool = False) -> bool:
    """Run one low-priority reconcile pass only when a pass can change something now."""
    proc = _dispatch_notice["proc"]
    if proc is not None and proc.poll() is None:
        return False
    started = _dispatch_notice["started"]
    if started is None:
        _reset_dispatch_notice_backoff()
    now_mono = time.monotonic()
    if not force and started is not None and now_mono - started < DISPATCH_NOTICE_INTERVAL:
        return False
    _dispatch_notice["evaluated_reasons"] = None
    if not dispatch_notice_needed():
        _reset_dispatch_notice_backoff()
        return False
    reasons = _dispatch_notice["evaluated_reasons"] or frozenset({("*", "needed")})
    last_reasons = _dispatch_notice["last_reasons"]
    if last_reasons is not None:
        if reasons == last_reasons:
            if not _dispatch_notice["doubled"]:
                _dispatch_notice["interval"] = min(
                    _dispatch_notice["interval"] * 2,
                    DISPATCH_NOTICE_MAX_INTERVAL,
                )
                _dispatch_notice["doubled"] = True
            if not force and started is not None and now_mono - started < _dispatch_notice["interval"]:
                return False
        else:
            _dispatch_notice["interval"] = DISPATCH_NOTICE_INTERVAL
            _dispatch_notice["doubled"] = False
    _dispatch_notice["started"] = now_mono
    cmd = dispatch_reconcile_command()
    if cmd is None:
        return False
    state_dir = _dispatch_state_dir()
    env = os.environ.copy()
    default_state = Path.home() / ".agent-compass"
    if state_dir != default_state:
        env.setdefault("ROUTER_STATE_DIR", str(state_dir))
        env.setdefault("ROUTER_DISPATCH_DB", str(_dispatch_db_path()))
    child_cwd = Path(sys.executable).resolve().parent if component_launch.frozen() else ROOT
    log_path = state_dir / "dispatch-notice.log"
    _bound_dispatch_notice_log(log_path)
    try:
        _dispatch_notice["proc"] = hidden_popen(
            cmd,
            env,
            child_cwd,
            log_path,
            BELOW_NORMAL_PRIORITY_CLASS,
        )
    except OSError:
        _dispatch_notice["proc"] = None
        return False
    _dispatch_notice["last_reasons"] = reasons
    _dispatch_notice["doubled"] = False
    return True


def _non_watcher_descendants(pid: int) -> list[psutil.Process]:
    to_stop: list[psutil.Process] = []
    try:
        queue = list(psutil.Process(pid).children(recursive=False))
    except psutil.Error:
        return to_stop
    while queue:
        child = queue.pop(0)
        try:
            cmdline = child.cmdline()
        except psutil.Error:
            continue
        if _is_bridge_watcher_cmdline(cmdline):
            continue
        to_stop.append(child)
        try:
            queue.extend(child.children(recursive=False))
        except psutil.Error:
            continue
    return to_stop


def stop_dispatch_notice() -> None:
    """Stop an in-flight reconcile pass and its non-watcher bridge children while preserving `--watch` processes."""
    proc = _dispatch_notice["proc"]
    _dispatch_notice["proc"] = None
    if proc is None or proc.poll() is not None:
        return
    children = _non_watcher_descendants(proc.pid) if isinstance(proc, subprocess.Popen) else []
    for child in reversed(children):
        try:
            if child.is_running():
                child.terminate()
        except psutil.Error:
            pass
    try:
        proc.terminate()
    except OSError:
        pass
    if children:
        _gone, alive = psutil.wait_procs(children, timeout=3.0)
        for child in reversed(alive):
            try:
                child.kill()
            except psutil.Error:
                pass
    if isinstance(proc, subprocess.Popen):
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=1.0)
            except OSError:
                pass
