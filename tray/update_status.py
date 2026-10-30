"""Persistent update progress and a small, independent status window."""
from __future__ import annotations

from i18n import translate as _tr

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import component_launch

DIRECTORY = Path.home() / ".agent-compass"
TITLE = "更新进度 · AgentCompass"
_lock = threading.Lock()
_process = None
_state = {}


def record(event: dict) -> None:
    global _state
    with _lock:
        if event["kind"] == "start":
            _state = {"running": True, "started": time.time(), "results": {}, "events": []}
        if not _state:
            return
        component = event.get("component", "")
        message = event.get("message", "")
        if event["kind"] == "component":
            _state["component"] = component
        if event["kind"] == "result":
            _state["results"][component] = message
        if event["kind"] == "finish":
            _state["running"] = False
            _state["finished"] = time.time()
        _state["message"] = message
        line = f"[{time.strftime('%H:%M:%S')}] {component + ' · ' if component else ''}{message}"
        _state["events"] = (_state["events"] + [line])[-100:]
        DIRECTORY.mkdir(parents=True, exist_ok=True)
        with (DIRECTORY / "updates.log").open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
        temporary = DIRECTORY / f"update-status.{os.getpid()}.tmp"
        temporary.write_text(json.dumps(_state, ensure_ascii=False), encoding="utf-8")
        temporary.replace(DIRECTORY / "update-status.json")


def read_status() -> dict:
    try:
        return json.loads((DIRECTORY / "update-status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def open_window() -> None:
    global _process
    import win32con
    import win32gui

    window = win32gui.FindWindow(None, TITLE) or win32gui.FindWindow(None, _tr(TITLE))
    if window:
        win32gui.ShowWindow(window, win32con.SW_RESTORE)
        try:
            win32gui.SetForegroundWindow(window)
        except Exception:
            win32gui.FlashWindow(window, True)
        return
    if _process is not None and _process.poll() is None:
        return
    _process = subprocess.Popen(component_launch.command(__file__), creationflags=0x08000000)


def main() -> None:
    import tkinter as tk
    from tkinter import ttk
    from tkinter.scrolledtext import ScrolledText

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import ui_theme as theme

    theme.identify_process()
    theme.dpi_aware()
    root = tk.Tk()
    theme.apply(root, _tr(TITLE), "900x540", (720, 420))
    body = ttk.Frame(root, padding=24)
    body.pack(fill="both", expand=True)
    heading = tk.StringVar(value=_tr("等待更新开始…"))
    ttk.Label(body, textvariable=heading, style="Title.TLabel").pack(anchor="w")
    ttk.Label(body, text=_tr("Dot2API / ChatGPT Web / Token Monitor · 关闭窗口不会中断更新"),
              style="Sub.TLabel").pack(anchor="w", pady=(4, 14))
    summary = theme.card(body, padding=14, fill="x")
    results = {label: tk.StringVar(value=_tr('{0}：等待检查').format(f'{label}')) for label in ("Dot2API", "ChatGPT Web", "Token Monitor")}
    for value in results.values():
        ttk.Label(summary, textvariable=value, style="Card.TLabel", wraplength=theme.px(820)).pack(anchor="w", pady=3)
    progress = ttk.Progressbar(body, mode="indeterminate")
    progress.pack(fill="x", pady=14)
    log_card = theme.card(body, padding=1, fill="both", expand=True)
    log = ScrolledText(log_card, font=(theme.mono_font(root), 9), wrap="word", state="disabled", relief="flat",
                       bd=0, bg=theme.CARD, fg=theme.TEXT, padx=10, pady=8)
    log.pack(fill="both", expand=True)
    buttons = ttk.Frame(body)
    buttons.pack(fill="x", pady=(14, 0))
    ttk.Button(buttons, text=_tr("打开完整日志"), command=lambda: os.startfile(DIRECTORY / "updates.log")).pack(side="left")
    ttk.Button(buttons, text=_tr("关闭"), style="Accent.TButton", command=root.destroy).pack(side="right")
    previous = None
    spinning = False

    def refresh():
        nonlocal previous, spinning
        state = read_status()
        running = bool(state.get("running"))
        if running:
            elapsed = max(0, int(time.time() - state.get("started", time.time())))
            heading.set(_tr('正在更新 {0} · {1}分{2}秒').format(f"{state.get('component', '')}", f'{elapsed // 60}', f'{elapsed % 60:02d}'))
        elif state:
            failed = any(any(word in text.lower() for word in ("失败", "出错", "未更新", "稍后", "没能",
                                                              "failed", "error", "not updated", "later", "could not"))
                         for text in state.get("results", {}).values())
            heading.set(_tr("更新结束，部分项目需要处理") if failed else _tr("更新检查已完成"))
        if running != spinning:
            progress.start(15) if running else progress.stop()
            spinning = running
        for label, value in results.items():
            detail = state.get("results", {}).get(label, f"{label}：" + (_tr("处理中…") if running and state.get("component") == label else _tr("等待检查")))
            summary = detail.splitlines()[0]
            value.set(summary if len(summary) <= 180 else summary[:179] + "…")
        text = "\n".join(state.get("events", []))
        if text != previous:
            log.configure(state="normal")
            log.delete("1.0", "end")
            log.insert("end", text)
            log.see("end")
            log.configure(state="disabled")
            previous = text
        root.after(500, refresh)

    refresh()
    root.mainloop()


if __name__ == "__main__":
    main()
