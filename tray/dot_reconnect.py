"""Keep the Dot connected after the Dot2API tunnel address changes.

A cloudflared quick tunnel gets a new address on every start (and after every reboot), and ChatGPT cannot edit the
URL of a plugin. So when the published address is not the one the Dot was last given, this adds a plugin for the new
address in the bridge's logged-in ChatGPT session, sends the Dot one message (it subscribes to the queue through the
plugin connected at that moment) and removes the plugins that point at dead addresses. The page work is in
`dot_plugin.mjs`; this decides when to run it and keeps the record in ~/.agent-compass/dot-plugin.json.
"""

from __future__ import annotations

from i18n import translate as _tr

import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import services

SCRIPT = Path(__file__).resolve().parent / "dot_plugin.mjs"
STATE = services.STATE_DIR / "dot-plugin.json"
DESCRIPTION = "领取本机 route 队列里的文字任务，做完后把回复交回。"
HOST = socket.gethostname()
BASE_NAME = f"Dot {HOST}"
RETRY_SECONDS = 20 * 60
MAX_TRIES = 4
RUN_SECONDS = 300


def load_state() -> dict:
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def published() -> tuple[str, str] | None:
    """(public base address, MCP URL) from connect.json, or None when there is none yet."""
    try:
        data = json.loads((services.LOG_DIR / "connect.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    base, mcp = data.get("public_base_url"), data.get("mcp_url")
    return (base, mcp) if base and mcp else None


def bridge_endpoint() -> str | None:
    """The debug endpoint of the launcher's browser, from its descriptor."""
    try:
        descriptor = json.loads((services.bridge_home() / "runtime" / "launcher-browser.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    endpoint = descriptor.get("endpoint")
    return endpoint if isinstance(endpoint, str) and endpoint.startswith("http://127.0.0.1:") else None


def turn_active(log: Path | None = None) -> bool:
    """True while a Pro consult turn runs in the same browser (its last turn event is a start)."""
    log = log or Path(os.environ.get("LOCALAPPDATA", "")) / "Codex Pro Consult" / "logs" / "launcher.jsonl"
    try:
        with log.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 400_000))
            lines = stream.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return False
    for line in reversed(lines):
        if '"browser.turn_started"' in line:
            return True
        if '"browser.turn_ended"' in line:
            return False
    return False


def wake_message(name: str) -> str:
    return (
        f"这是主机 {HOST} 的 Dot2API 连接设置。请使用刚连接的插件“{name}”。"
        f"只替换本主机“{BASE_NAME}”的旧插件连接；保留所有其他主机的插件、队列订阅和任务，不得取消或替换它们。\n\n"
        f"请立刻用“{name}”订阅这个插件的队列 dot 上的 task.available 事件，使用你自己的任务回调方式，"
        "然后立刻检查一次队列：有 queued 任务就按之前给你的说明领取、处理并回传结果。不要做其他事。"
    )


def run_node(endpoint: str, *arguments: str) -> dict:
    node = shutil.which("node")
    if node is None:
        return {"ok": False, "step": "node", "error": "node.exe was not found"}
    try:
        done = subprocess.run([node, str(SCRIPT), endpoint, *arguments], capture_output=True, text=True, encoding="utf-8",
                              timeout=RUN_SECONDS, creationflags=services.CREATE_NO_WINDOW, check=False)
    except subprocess.TimeoutExpired:
        return {"ok": False, "step": "timeout", "error": f"no result within {RUN_SECONDS} s"}
    lines = [line for line in done.stdout.splitlines() if line.startswith("{")]
    if not lines:
        return {"ok": False, "step": "node", "error": (done.stderr or "no output").strip()[-300:]}
    return json.loads(lines[-1])


def due(state: dict, base: str, now: float) -> bool:
    if state.get("url") == base and state.get("wake_pending"):
        # Sending may have succeeded even when the UI acknowledgement was lost.
        # Recover the existing subscription/conversation rather than duplicate the wake.
        return False
    if state.get("url") == base and not state.get("wake_pending"):
        return False
    if state.get("failed_url") == base:
        if state.get("tries", 0) >= MAX_TRIES or now - state.get("failed_at", 0) < RETRY_SECONDS:
            return False
    return True


def reconnect(now: float | None = None) -> str | None:
    """One attempt when the Dot has an out-of-date address and everything needed is up; a message for the user or None."""
    now = time.time() if now is None else now
    address = published()
    if address is None or not services.dot_ready() or not services.tunnel_pids():
        return None
    base, mcp = address
    state = load_state()
    if not due(state, base, now):
        return None
    endpoint = bridge_endpoint()
    if endpoint is None or not services.chatgpt_ready() or turn_active():
        return None
    if not services.http_ok(f"{base}/readyz", timeout=10):
        return None
    # One name per address, so a retry after a half-finished attempt finds the plugin it already added.
    name = f"{BASE_NAME} {hashlib.sha1(base.encode()).hexdigest()[:6]}"
    with tempfile.TemporaryDirectory() as directory:
        url_file = Path(directory) / "url.txt"
        url_file.write_text(mcp, encoding="utf-8")
        result = run_node(endpoint, "register", name, DESCRIPTION, str(url_file))
    if not result.get("ok"):
        tries = state.get("tries", 0) + 1 if state.get("failed_url") == base else 1
        save_state({**state, "failed_url": base, "failed_at": now, "tries": tries, "last_error": f"{result.get('step')}: {result.get('error')}"})
        return _tr('Dot 连接地址更新失败（{0}），稍后重试').format(f"{result.get('step')}") if tries == 1 else None
    # Only this host's recorded names are eligible. Never scan account-wide names for deletion.
    previous = list(dict.fromkeys(n for n in [state.get("name"), *state.get("old_names", [])]
                                 if isinstance(n, str) and n.startswith(BASE_NAME + " ") and n != name))
    current = {"host": HOST, "url": base, "name": name, "at": now, "old_names": previous, "wake_pending": True}
    save_state(current)
    with tempfile.TemporaryDirectory() as directory:
        message_file = Path(directory) / "message.txt"
        message_file.write_text(wake_message(name), encoding="utf-8")
        woke = run_node(endpoint, "wake", str(message_file))
    if not woke.get("ok"):
        save_state({**current, "failed_url": base, "failed_at": now, "tries": state.get("tries", 0) + 1,
                    "last_error": "wake did not complete; inspect the existing Dot conversation before retrying"})
        return _tr("Dot 本机插件已连接；但没能给 Dot 发消息，请检查现有 Dot 对话并让它订阅；其他主机连接未更改")
    current["wake_pending"] = False
    save_state(current)
    cleaned = run_node(endpoint, "remove", *previous) if previous else {"ok": True, "removed": []}
    if cleaned.get("ok"):
        save_state({**current, "old_names": []})
    return _tr('Dot 连接已自动更新到新地址（插件“{0}”）').format(f'{name}')
