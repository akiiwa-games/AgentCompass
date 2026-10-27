"""EasyTier for the tray: status, start, stop and restart of the host's one EasyTier node.

The node is the SYSTEM scheduled task "Route Manager EasyTier", registered once by easytier_setup.py. Its
program, config and logs live under route_manager/easytier/. The tray itself stays unelevated: the task grants this
user only read, run and stop on that one task (not edit), so every action here is a Task Scheduler call plus,
for status, the bundled easytier-cli talking to the loopback RPC portal.
"""
from __future__ import annotations

from i18n import translate as _tr

import csv
import io
import json
import subprocess
import time
from pathlib import Path

import psutil

CREATE_NO_WINDOW = 0x08000000
ROOT = Path(__file__).resolve().parents[1]
HOME = ROOT / "easytier"
BIN = HOME / "bin"
PRIVATE = HOME / "private"
LOGS = HOME / "logs"
STATE = HOME / "state"
CORE = BIN / "easytier-core.exe"
CLI = BIN / "easytier-cli.exe"
SUPERVISOR = BIN / "easytier-supervisor.ps1"
CONFIG = PRIVATE / "node.toml"
TASK_NAME = "Route Manager EasyTier"
RPC_PORTAL = "127.0.0.1:15888"
# Task Scheduler's TASK_STATE values.
TASK_DISABLED, TASK_QUEUED, TASK_READY, TASK_RUNNING = 1, 2, 3, 4
STOP_WAIT = 20.0

MISSING, STOPPED, STARTING, RUNNING, FOREIGN, DISABLED = "missing", "stopped", "starting", "running", "foreign", "disabled"


def installed() -> bool:
    """Whether the program is in place. Cheap and local; the task itself is checked by status()."""
    return CORE.is_file() and CLI.is_file()


def _scheduler_task():
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        service = win32com.client.Dispatch("Schedule.Service")
        service.Connect()
        return service.GetFolder("\\").GetTask(TASK_NAME)
    except Exception:
        pythoncom.CoUninitialize()
        raise


def task_state() -> int | None:
    """The scheduled task's state, or None when it is not registered (or unreadable)."""
    import pythoncom

    try:
        task = _scheduler_task()
    except Exception:
        return None
    try:
        return int(task.State)
    finally:
        task = None
        pythoncom.CoUninitialize()


def _task_call(action: str) -> None:
    import pythoncom

    task = _scheduler_task()
    try:
        if action == "run":
            task.Run(None)
        else:
            task.Stop(0)
    finally:
        task = None
        pythoncom.CoUninitialize()


def core_pids() -> list[int]:
    """Every easytier-core on the host, whoever started it (the name is visible even for elevated processes)."""
    result = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq easytier-core.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, errors="replace", check=False, timeout=10, creationflags=CREATE_NO_WINDOW)
    pids = []
    for row in csv.reader(io.StringIO(result.stdout)):
        if len(row) >= 2 and row[0].lower() == "easytier-core.exe" and row[1].isdigit():
            pids.append(int(row[1]))
    return pids


def peers(timeout: float = 8.0) -> list[dict] | None:
    """The node's peer table from its loopback RPC portal, or None while the portal does not answer."""
    if not CLI.is_file():
        return None
    try:
        result = subprocess.run([str(CLI), "-p", RPC_PORTAL, "-o", "json", "peer"], capture_output=True, text=True,
                                errors="replace", check=False, timeout=timeout, creationflags=CREATE_NO_WINDOW)
        data = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return data if isinstance(data, list) else None


def snapshot() -> dict:
    """One status reading. Runs on the tray's watcher thread; the menu only ever reads the result."""
    if not installed():
        return {"state": MISSING, "pids": [], "peers": None}
    state = task_state()
    if state is None:
        return {"state": MISSING, "pids": [], "peers": None}
    pids = core_pids()
    if state == TASK_RUNNING:
        table = peers() if pids else None
        return {"state": RUNNING if table is not None else STARTING, "pids": pids, "peers": table}
    if pids:
        return {"state": FOREIGN, "pids": pids, "peers": None}
    return {"state": DISABLED if state == TASK_DISABLED else STOPPED, "pids": [], "peers": None}


def remote_peers(table: list[dict] | None) -> list[dict]:
    return [peer for peer in table or [] if peer.get("cost") != "Local"]


def label(reading: dict) -> str:
    state = reading["state"]
    if state == RUNNING:
        count = len(remote_peers(reading["peers"]))
        return _tr('运行中（{0} 个对端）').format(f'{count}') if count else _tr("运行中（无对端）")
    return {
        MISSING: _tr("未安装"), STOPPED: _tr("已停止"), STARTING: _tr("启动中"), DISABLED: _tr("任务已禁用"),
        FOREIGN: _tr("外部实例在运行"),
    }[state]


def peer_report(reading: dict) -> str:
    if reading["state"] != RUNNING:
        return f"EasyTier：{label(reading)}"
    rows = []
    for peer in remote_peers(reading["peers"]):
        address = peer.get("ipv4") or _tr("中继")
        rows.append(f"{peer.get('hostname', '?')} {address} {peer.get('cost', '?')} {peer.get('lat_ms', '-')}ms")
    return "EasyTier：" + ("；".join(rows) if rows else _tr("暂无对端"))


def start() -> str:
    reading = snapshot()
    state = reading["state"]
    if state == MISSING:
        return _tr("EasyTier 未安装，先运行 python tray/easytier_setup.py install（见 README）")
    if state in (RUNNING, STARTING):
        return _tr("EasyTier 已在运行")
    if state == FOREIGN:
        return _tr('另有 easytier-core（PID {0}）在运行，不重复启动').format(f"{', '.join(map(str, reading['pids']))}")
    if state == DISABLED:
        return _tr("EasyTier 的计划任务已被禁用")
    _task_call("run")
    return _tr("已启动 EasyTier")


def stop(wait: float = STOP_WAIT) -> str:
    reading = snapshot()
    if reading["state"] == MISSING:
        return _tr("EasyTier 未安装")
    if reading["state"] not in (RUNNING, STARTING):
        return _tr("EasyTier 未在运行") if reading["state"] != FOREIGN else _tr("运行中的 easytier-core 不是本任务启动的，未处理")
    _task_call("stop")
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline and (task_state() == TASK_RUNNING or core_pids()):
        time.sleep(0.5)
    if core_pids():
        return _tr("已请求停止 EasyTier，但进程仍在，详见 easytier/logs")
    return _tr("已停止 EasyTier")


def restart() -> str:
    reading = snapshot()
    if reading["state"] in (RUNNING, STARTING):
        message = stop()
        if _tr("仍在") in message:
            return message
    return start()


def start_if_stopped() -> str | None:
    """Login start: only a task that is ready and has no core running; stays silent otherwise."""
    reading = snapshot()
    if reading["state"] == STOPPED:
        return start()
    return None
