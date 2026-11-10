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

DIRECTORY = Path(os.environ.get("ROUTER_STATE_DIR") or (Path.home() / ".agent-compass"))
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
                "owner_pid": os.getpid(),
                "title": event.get("title", ""),
                "targets": list(dict.fromkeys(event.get("targets") or ())),
                "results": {},
                "events": [],
            }
        if not _state:
            return
        if "pending_installer" in event:
            if event["pending_installer"] is None:
                _state.pop("pending_installer", None)
            else:
                _state["pending_installer"] = event["pending_installer"]
        full_reason = event.get("full_installer_reason") or event.get("reason")
        if full_reason:
            _state["full_installer_reason"] = str(full_reason)
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
            _state.setdefault("statuses", {})[component or "AgentCompass"] = event.get("status") or "warning"
        if event["kind"] == "finish":
            _state["running"] = False
            _state["finished"] = time.time()
            _state["component"] = ""
            _state.pop("pending_installer", None)
        _state["message"] = message
        line = f"[{time.strftime('%H:%M:%S')}] {component + ' · ' if component else ''}{message}"
        _state["events"] = (_state["events"] + [line])[-150:]
        target_dir.mkdir(parents=True, exist_ok=True)
        with (target_dir / "updates.log").open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
        temporary = target_dir / f"update-status.{os.getpid()}.tmp"
        temporary.write_text(json.dumps(_state, ensure_ascii=False), encoding="utf-8")
        temporary.replace(target_dir / "update-status.json")


def _pid_alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            kernel.WaitForSingleObject.restype = ctypes.c_uint32
            kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel.GetExitCodeProcess.restype = ctypes.c_int
            kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]

            handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
            if handle:
                try:
                    return kernel.WaitForSingleObject(handle, 0) == 0x00000102  # WAIT_TIMEOUT
                finally:
                    kernel.CloseHandle(handle)
            handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if handle:
                try:
                    code = ctypes.c_uint32(0)
                    if kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                        return code.value == 259  # STILL_ACTIVE
                finally:
                    kernel.CloseHandle(handle)
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _installer_process_running() -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class _PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel.Process32FirstW.restype = wintypes.BOOL
        kernel.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
        kernel.Process32NextW.restype = wintypes.BOOL
        kernel.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]

        snap = kernel.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
        invalid = ctypes.c_void_p(-1).value
        if not snap or snap == invalid:
            return False
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            ok = kernel.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                name = str(entry.szExeFile or "").lower()
                if name.startswith("agentcompass-tray-setup"):
                    return True
                ok = kernel.Process32NextW(snap, ctypes.byref(entry))
        finally:
            kernel.CloseHandle(snap)
    except Exception:
        pass
    return False


def _read_installed_version(inst_root: str | Path | None = None) -> str:
    import re

    roots: list[Path] = []
    if inst_root:
        roots.append(Path(inst_root))
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (
            (parent / "runtime-info.json").is_file()
            or (parent / "AgentCompass.exe").is_file()
            or (parent / "app" / "current" / "app-manifest.json").is_file()
            or (parent / "agentcompass" / "__init__.py").is_file()
        ):
            if parent not in roots:
                roots.append(parent)
    for root in roots:
        for rel, keys in (
            (Path("app") / "current" / "app-manifest.json", ("version",)),
            (Path("app") / "state.json", ("active_version", "current_version")),
            (Path("runtime-info.json"), ("runtime_version", "version")),
        ):
            cand = root / rel
            if cand.is_file():
                try:
                    data = json.loads(cand.read_text(encoding="utf-8-sig"))
                    if isinstance(data, dict):
                        for key in keys:
                            val = str(data.get(key) or "").strip().lstrip("vV")
                            if val and val != "0.0.0":
                                return val
                except Exception:
                    pass
        for init_rel in (Path("app") / "current" / "agentcompass" / "__init__.py", Path("agentcompass") / "__init__.py"):
            init_file = root / init_rel
            if init_file.is_file():
                try:
                    match = re.search(
                        r'^__version__\s*=\s*[\'"]([^\'"]+)[\'"]',
                        init_file.read_text(encoding="utf-8-sig"),
                        re.M,
                    )
                    if match:
                        val = match.group(1).strip().lstrip("vV")
                        if val and val != "0.0.0":
                            return val
                except Exception:
                    pass
    return ""


def reconcile_status(directory: Path | None = None, *, current_version: str | None = None) -> dict:
    """Reconcile an update-status.json left in running=True after the installer killed the previous tray."""
    target_dir = Path(directory) if directory is not None else DIRECTORY
    status_file = target_dir / "update-status.json"
    try:
        state = json.loads(status_file.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    if not isinstance(state, dict) or not state.get("running"):
        return state if isinstance(state, dict) else {}

    pending = state.get("pending_installer") if isinstance(state.get("pending_installer"), dict) else None
    owner_pid = state.get("owner_pid")

    if pending is not None:
        inst_pid = pending.get("pid")
        if _pid_alive(owner_pid) or _pid_alive(inst_pid) or _installer_process_running():
            return state
        target_ver = str(pending.get("target_version") or "").strip().lstrip("vV")
        tag_label = str(pending.get("tag_label") or (f"v{target_ver}" if target_ver else "AgentCompass-tray-setup.exe"))
        inst_root = pending.get("install_root")
        cur_ver = str(current_version or _read_installed_version(inst_root) or "").strip().lstrip("vV")
        upgraded = bool(target_ver and cur_ver and cur_ver != "0.0.0" and cur_ver == target_ver)
        if upgraded:
            msg = _tr("已完成安装包静默更新（{0}）").format(tag_label)
        else:
            msg = _tr("AgentCompass 更新失败：目标版本 v{0}，当前版本 v{1}").format(
                target_ver or "?", cur_ver or _tr("未知")
            )
        result_text = msg
    else:
        targets = state.get("targets") or ()
        is_product_update = state.get("title") == "AgentCompass" or targets == ["AgentCompass"]
        if not is_product_update:
            return state
        if _pid_alive(owner_pid) or _installer_process_running():
            return state
        msg = _tr("已结束，结果未知")
        result_text = msg

    with _lock:
        state.setdefault("results", {})["AgentCompass"] = result_text
        state["running"] = False
        state["finished"] = time.time()
        state["component"] = ""
        state["message"] = _tr("AgentCompass 更新结束")
        state.pop("pending_installer", None)
        stamp = time.strftime("%H:%M:%S")
        line1 = f"[{stamp}] AgentCompass · {msg}"
        line2 = f"[{stamp}] {_tr('AgentCompass 更新结束')}"
        events = list(state.get("events") or ())
        for line in (line1, line2):
            if not events or events[-1] != line:
                events.append(line)
        state["events"] = events[-150:]
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            with (target_dir / "updates.log").open("a", encoding="utf-8") as stream:
                stream.write(line1 + "\n" + line2 + "\n")
            temporary = target_dir / f"update-status.{os.getpid()}.tmp"
            temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            temporary.replace(status_file)
        except OSError:
            pass
    return state


def read_status(directory: Path | None = None) -> dict:
    return reconcile_status(directory)


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
            failed = any(status in ("failed", "warning", "deferred", "interrupted")
                         for status in state.get("statuses", {}).values())
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
