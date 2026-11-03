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


DEFAULT_TARGETS = ("Dot2API", "ChatGPT Web", "Usage Float", "Gemini Web")


def record(event: dict | str, *, directory: Path | None = None) -> None:
    global _state
    target_dir = directory or DIRECTORY
    with _lock:
        if isinstance(event, str):
            if not _state:
                _state = {"running": True, "started": time.time(), "targets": [], "results": {}, "events": []}
            event = {"kind": "progress", "component": _state.get("component", ""), "message": event}
        if event["kind"] == "start":
            _state = {
                "running": True,
                "started": time.time(),
                "title": event.get("title", ""),
                "targets": list(dict.fromkeys(event.get("targets") or ())),
                "results": {},
                "events": [],
            }
        if not _state:
            return
        component = event.get("component", "")
        message = event.get("message", "")
        targets = _state.setdefault("targets", [])
        for name in event.get("targets") or ():
            if name and name not in targets:
                targets.append(name)
        if component and component not in targets:
            targets.append(component)
        if event["kind"] == "component":
            _state["component"] = component
        if event["kind"] == "result":
            _state["results"][component or "AgentCompass"] = message
        if event["kind"] == "finish":
            _state["running"] = False
            _state["finished"] = time.time()
            _state["component"] = ""
        _state["message"] = message
        line = f"[{time.strftime('%H:%M:%S')}] {component + ' · ' if component else ''}{message}"
        _state["events"] = (_state["events"] + [line])[-150:]
        target_dir.mkdir(parents=True, exist_ok=True)
        with (target_dir / "updates.log").open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
        temporary = target_dir / f"update-status.{os.getpid()}.tmp"
        temporary.write_text(json.dumps(_state, ensure_ascii=False), encoding="utf-8")
        temporary.replace(target_dir / "update-status.json")


def read_status(directory: Path | None = None) -> dict:
    target_dir = directory or DIRECTORY
    try:
        return json.loads((target_dir / "update-status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def open_log(directory: Path | None = None) -> str:
    target_dir = directory or DIRECTORY
    target_dir.mkdir(parents=True, exist_ok=True)
    log_path = target_dir / "updates.log"
    if not log_path.exists():
        cli_log = target_dir / "cli-updates.log"
        if cli_log.exists():
            log_path = cli_log
        else:
            log_path.write_text("", encoding="utf-8")
    os.startfile(log_path)
    return _tr("已打开日志 {0}").format(log_path.name)


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


def active_targets(state: dict) -> list[str]:
    names = list(dict.fromkeys([*(state.get("targets") or ()), *((state.get("results") or {}).keys())]))
    return names or list(DEFAULT_TARGETS)


def main() -> None:
    import tkinter as tk
    from tkinter import ttk
    from tkinter.scrolledtext import ScrolledText

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import ui_theme as theme

    theme.identify_process()
    theme.dpi_aware()
    root = tk.Tk()
    theme.apply(root, _tr(TITLE), "900x560", (720, 420))
    body = ttk.Frame(root, padding=24)
    body.pack(fill="both", expand=True)
    heading = tk.StringVar(value=_tr("等待更新开始…"))
    ttk.Label(body, textvariable=heading, style="Title.TLabel").pack(anchor="w")
    subtitle = tk.StringVar(value=f"{' / '.join(DEFAULT_TARGETS)} · {_tr('关闭窗口不会中断更新')}")
    ttk.Label(body, textvariable=subtitle, style="Sub.TLabel", wraplength=theme.px(840)).pack(anchor="w", pady=(4, 14))
    summary = theme.card(body, padding=14, fill="x")
    results: dict[str, tk.StringVar] = {}
    rendered_targets: tuple[str, ...] = ()
    progress = ttk.Progressbar(body, mode="indeterminate")
    progress.pack(fill="x", pady=14)
    log_card = theme.card(body, padding=1, fill="both", expand=True)
    log = ScrolledText(log_card, font=(theme.mono_font(root), 9), wrap="word", state="disabled", relief="flat",
                       bd=0, bg=theme.CARD, fg=theme.TEXT, padx=10, pady=8)
    log.pack(fill="both", expand=True)
    buttons = ttk.Frame(body)
    buttons.pack(fill="x", pady=(14, 0))
    ttk.Button(buttons, text=_tr("打开完整日志"), command=open_log).pack(side="left")
    ttk.Button(buttons, text=_tr("关闭"), style="Accent.TButton", command=root.destroy).pack(side="right")
    previous = None
    spinning = False

    def rebuild_summary(targets: tuple[str, ...]) -> None:
        nonlocal rendered_targets, results
        if targets == rendered_targets:
            return
        for child in summary.winfo_children():
            child.destroy()
        results = {
            label: tk.StringVar(value=_tr("{0}：等待检查").format(label))
            for label in targets
        }
        for value in results.values():
            ttk.Label(summary, textvariable=value, style="Card.TLabel", wraplength=theme.px(820)).pack(anchor="w", pady=2)
        rendered_targets = targets
        subtitle.set(f"{' / '.join(targets)} · {_tr('关闭窗口不会中断更新')}")

    def refresh():
        nonlocal previous, spinning
        state = read_status()
        targets = tuple(active_targets(state))
        rebuild_summary(targets)
        running = bool(state.get("running"))
        if running:
            elapsed = max(0, int(time.time() - state.get("started", time.time())))
            current = state.get("component") or state.get("title") or _tr("组件")
            heading.set(_tr("正在更新 {0} · {1}分{2}秒").format(current, f"{elapsed // 60}", f"{elapsed % 60:02d}"))
        elif state:
            failed = any(any(word in text.lower() for word in ("失败", "出错", "未更新", "稍后", "没能", "延后",
                                                              "failed", "error", "not updated", "later", "could not", "deferred"))
                         for text in state.get("results", {}).values())
            heading.set(_tr("更新结束，部分项目需要处理") if failed else _tr("更新检查已完成"))
        if running != spinning:
            progress.start(15) if running else progress.stop()
            spinning = running
        for label, value in results.items():
            detail = state.get("results", {}).get(
                label,
                f"{label}：" + (_tr("处理中…") if running and state.get("component") == label else _tr("等待检查")),
            )
            line = detail.splitlines()[0] if detail else f"{label}：{_tr('等待检查')}"
            value.set(line if len(line) <= 180 else line[:179] + "…")
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
