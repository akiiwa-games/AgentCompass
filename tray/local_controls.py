"""Local menu IPC. UI processes consume commands on their own event loop.

Only menu labels, checks and opaque action IDs cross this boundary. Requests and
receipts stay in the user's AgentCompass state directory; no network listener.
"""
from __future__ import annotations

from i18n import translate as _tr

import json
import os
from pathlib import Path
import time
import uuid

import psutil

DIRECTORY = Path.home() / ".agent-compass" / "controls"


def component_directory(component: str) -> Path:
    if component != "usage-float":
        raise ValueError("Unknown tray component")
    return DIRECTORY / component


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_snapshot(component: str) -> dict | None:
    try:
        path = component_directory(component) / "menu.json"
        if path.stat().st_size > 256 * 1024:
            return None
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        process = psutil.Process(snapshot["pid"])
        if not process.is_running() or process.create_time() > snapshot["started"]:
            return None
        return snapshot
    except (OSError, ValueError, KeyError, TypeError, psutil.Error):
        return None


def send_action(component: str, action: str, timeout: float = 10) -> str:
    snapshot = read_snapshot(component)
    if snapshot is None:
        raise RuntimeError(_tr('{0} 界面未运行').format(f'{component}'))
    directory = component_directory(component)
    request = directory / f"{uuid.uuid4().hex}.request.json"
    response = request.with_name(request.name.replace(".request.", ".response."))
    deadline = time.monotonic() + timeout
    write_json(request, {"instance": snapshot["instance"], "action": action,
                         "expires": time.time() + timeout})
    try:
        while time.monotonic() < deadline:
            try:
                result = json.loads(response.read_text(encoding="utf-8"))
                if not result.get("ok"):
                    raise RuntimeError(result.get("error") or _tr("操作失败"))
                return result.get("message") or _tr("操作完成")
            except FileNotFoundError:
                time.sleep(0.05)
        raise TimeoutError(_tr('{0} 未响应，请重新打开界面').format(f'{component}'))
    finally:
        request.unlink(missing_ok=True)
        response.unlink(missing_ok=True)


class ControlServer:
    def __init__(self, component: str):
        self.directory = component_directory(component)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.instance = uuid.uuid4().hex
        self.started = time.time()
        self.actions = {}
        self.key = None

    def publish(self, items: list[dict], actions: dict) -> None:
        self.actions = actions
        key = json.dumps(items, ensure_ascii=False, sort_keys=True)
        if key == self.key:
            return
        write_json(self.directory / "menu.json", {"pid": os.getpid(), "started": self.started,
                   "instance": self.instance, "items": items})
        self.key = key

    def poll(self) -> None:
        for request in list(self.directory.glob("*.request.json"))[:20]:
            response = request.with_name(request.name.replace(".request.", ".response."))
            try:
                body = json.loads(request.read_text(encoding="utf-8"))
                if body.get("expires", 0) < time.time():
                    continue
                if body.get("instance") != self.instance:
                    raise RuntimeError(_tr("界面已重启，请重试"))
                action = self.actions.get(body.get("action"))
                if action is None:
                    raise RuntimeError(_tr("该操作当前不可用"))
                message = action()
                write_json(response, {"ok": True, "message": message if isinstance(message, str) else _tr("操作完成")})
            except Exception as error:
                write_json(response, {"ok": False, "error": str(error)})
            finally:
                request.unlink(missing_ok=True)

    def close(self) -> None:
        path = self.directory / "menu.json"
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("instance") == self.instance:
                path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
