"""First-run router setup from the tray, with the user's consent: preview, choose the gate hook, confirm, see the result.

The installer puts ``agentcompass.exe`` in ``<app>\\cli`` but never rewrites host configs on its own. When the router is not
set up yet (no ``~/.codex/skills/agent-compass``), the tray opens this window once and keeps a "设置 Router…" menu item:

1. the window shows ``agentcompass setup --dry-run`` (which files would change);
2. the user turns the router gate hook on or off (off by default, as on the command line);
3. ``agentcompass setup --yes --hooks|--no-hooks`` runs only after "确认设置";
4. the window shows what setup printed.

``~/.agent-compass/router-setup.json`` records only that the window was offered and how it ended.
Scripted installs skip the window: ``AgentCompass-tray-setup.exe /VERYSILENT /SETUP=hooks`` (or ``/SETUP=nohooks``), or
``agentcompass setup --yes --hooks`` after install.

    AgentCompass.exe --run-component router_setup
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import component_launch
from i18n import translate as _tr

TITLE = "AgentCompass Router 设置"
STATE_NAME = "router-setup.json"
CREATE_NO_WINDOW = 0x08000000
_process = None


def state_path(home: Path | None = None) -> Path:
    return (Path(home) if home is not None else Path.home()) / ".agent-compass" / STATE_NAME


def load_state(home: Path | None = None) -> dict:
    try:
        data = json.loads(state_path(home).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record(outcome: str, home: Path | None = None) -> None:
    path = state_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {"offered": True, "outcome": outcome, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def router_ready(home: Path | None = None, environ: dict | None = None) -> bool:
    """The router skill the tray and hosts read is in place (`setup` links it into the Codex skills hub)."""
    environ = os.environ if environ is None else environ
    if home is None and environ.get("CODEX_HOME"):
        codex = Path(environ["CODEX_HOME"])
    else:
        codex = (Path(home) if home is not None else Path.home()) / ".codex"
    return (codex / "skills" / "agent-compass" / "scripts" / "route_judge.py").is_file()


def should_offer(home: Path | None = None, environ: dict | None = None) -> bool:
    """Open the window by itself only once, and only while the router is not set up."""
    return not router_ready(home, environ) and not load_state(home).get("offered")


def cli_command(frozen: bool | None = None, executable: str | None = None) -> list[str] | None:
    """How the tray runs `agentcompass`: the installer's exe beside the tray, else one on PATH, else this checkout."""
    frozen = component_launch.frozen() if frozen is None else frozen
    executable = executable or sys.executable
    if frozen:
        bundled = Path(executable).resolve().parent / "cli" / "agentcompass.exe"
        if bundled.is_file():
            return [str(bundled)]
    found = shutil.which("agentcompass")
    if found:
        return [found]
    checkout = Path(__file__).resolve().parents[1]
    if (checkout / "agentcompass" / "__main__.py").is_file() and not frozen:
        python = Path(executable)
        if python.name.lower() == "pythonw.exe":
            python = python.with_name("python.exe")
        return [str(python), "-X", "utf8", str(checkout / "agentcompass")]
    return None


def setup_argv(base: list[str], hooks: bool, dry_run: bool) -> list[str]:
    return [*base, "setup", "--yes", "--hooks" if hooks else "--no-hooks", *(["--dry-run"] if dry_run else [])]


def run_setup(hooks: bool, dry_run: bool, base: list[str] | None = None, runner=subprocess.run) -> tuple[int, str]:
    base = base if base is not None else cli_command()
    if not base:
        return 2, _tr("未找到 agentcompass 命令行；请重新运行 AgentCompass 安装包。")
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    try:
        done = runner(setup_argv(base, hooks, dry_run), capture_output=True, text=True, encoding="utf-8", errors="replace",
                      env=env, stdin=subprocess.DEVNULL, timeout=600, creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as error:
        return 2, f"{type(error).__name__}: {error}"
    return done.returncode, ((done.stdout or "") + (done.stderr or "")).strip()


def open_window() -> None:
    """Raise the window if it is open, else start it in its own process; never blocks the tray."""
    global _process
    try:
        import win32con
        import win32gui
        window = win32gui.FindWindow("TkTopLevel", TITLE)
        if window:
            win32gui.ShowWindow(window, win32con.SW_RESTORE)
            try:
                win32gui.SetForegroundWindow(window)
            except Exception:
                win32gui.FlashWindow(window, True)
            return
    except ImportError:
        pass
    if _process is not None and _process.poll() is None:
        return
    _process = subprocess.Popen(component_launch.command(__file__), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, close_fds=True, creationflags=CREATE_NO_WINDOW)


def main() -> int:
    import threading
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title(TITLE)
    root.geometry("760x560")
    hooks = tk.BooleanVar(value=False)
    finished = {"outcome": None}

    ttk.Label(root, text=_tr("AgentCompass 将把 router 技能链接到各 agent、写入全局 agent 规则（受管块，你自己的内容保留）。"
                            "下面是将要改动的文件，确认后才会写入；所有改动都有备份，可用 agentcompass uninstall 撤销。"),
              wraplength=720, justify="left").pack(fill="x", padx=12, pady=(12, 6))
    ttk.Checkbutton(root, variable=hooks, text=_tr("安装 router 门禁 hook（拦截 15 分钟内没有 roll 记录的子 agent 启动；可随时撤销）")).pack(
        anchor="w", padx=12)
    text = tk.Text(root, wrap="word", height=22)
    text.pack(fill="both", expand=True, padx=12, pady=6)
    buttons = ttk.Frame(root)
    buttons.pack(fill="x", padx=12, pady=(0, 12))
    confirm = ttk.Button(buttons, text=_tr("确认设置"))
    later = ttk.Button(buttons, text=_tr("以后再说"))
    confirm.pack(side="right")
    later.pack(side="right", padx=6)

    def show(content: str) -> None:
        text.configure(state="normal")
        text.delete("1.0", "end")
        text.insert("1.0", content)
        text.configure(state="disabled")

    def in_background(work, done) -> None:
        threading.Thread(target=lambda: (lambda result: root.after(0, done, result))(work()), daemon=True).start()

    def preview(*_args) -> None:
        confirm.state(["disabled"])
        show(_tr("正在预览（setup --dry-run）…"))
        in_background(lambda: run_setup(hooks.get(), True), lambda result: (show(result[1]), confirm.state(["!disabled"])))

    def apply() -> None:
        confirm.state(["disabled"])
        later.state(["disabled"])
        show(_tr("正在设置…"))

        def done(result) -> None:
            code, output = result
            finished["outcome"] = "applied" if code == 0 else "failed"
            record(finished["outcome"])
            show((_tr("设置完成。") if code == 0 else _tr("设置失败（退出码 {0}）。").format(code)) + "\n\n" + output)
            later.configure(text=_tr("关闭"))
            later.state(["!disabled"])

        in_background(lambda: run_setup(hooks.get(), False), done)

    def close() -> None:
        if finished["outcome"] is None:
            record("declined")
        root.destroy()

    hooks.trace_add("write", preview)
    confirm.configure(command=apply)
    later.configure(command=close)
    root.protocol("WM_DELETE_WINDOW", close)
    preview()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
