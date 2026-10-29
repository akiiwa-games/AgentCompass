"""Deploy the Route Manager tray: the single tray for Dot2API, ChatGPT Web and the usage statistics.

    python deploy.py check      report what is in place and what is not; changes nothing
    python deploy.py install    patch and build ChatGPT Web (it runs from codex-chatgpt-web/), fix the startup entries,
                                point the route at the launcher, link the skills in skills/ into ~/.codex and ~/.claude
    python deploy.py restart    close the running tray (services keep running) and start it again, detached
    python deploy.py all        install, then restart

Every step is safe to repeat. A replaced startup shortcut is moved to ~/.route-manager/backups/startup, not deleted.

The skills live in skills/ and reach a host with git pull: ~/.codex/skills/<name> is a junction to skills/<name>, and
~/.claude/skills/<name> a junction to the ~/.codex one. A real directory in their place is moved to
~/.route-manager/backups/skills first.

Login start is the per-user scheduled task "Route Manager", not a Startup-folder shortcut: Explorer runs the Run keys
and the Startup folder one after another, and a blocking login startup item can stall the Startup
folder (the tray, Snipaste) started at all.
"""

from __future__ import annotations

from i18n import translate as _tr

import argparse
import ctypes
import os
import shutil
import subprocess
import sys
import time
import winreg
from pathlib import Path

import psutil

import chatgpt_web_patch as web_patch
import easytier
import services
import updates

TRAY_DIR = Path(__file__).resolve().parent
APP = TRAY_DIR / "app.pyw"
LAUNCHER = TRAY_DIR / "launch_chatgpt_web.pyw"
PYTHONW = Path(sys.executable).with_name("pythonw.exe")
STARTUP = Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
SHORTCUT = STARTUP / "Route Manager.lnk"      # the old login entry, replaced by TASK_NAME
SUPERSEDED = (
    STARTUP / "Pro Consult.lnk",      # started the ChatGPT Web launcher (with a second tray icon) at login
    SHORTCUT,
)
TASK_NAME = "Route Manager"
TASK_DELAY = "PT20S"      # let the taskbar come up before pystray adds its icon
BACKUPS = Path.home() / ".route-manager" / "backups" / "startup"
SKILLS = TRAY_DIR.parent / "skills"
CODEX_SKILLS = Path.home() / ".codex" / "skills"
CLAUDE_SKILLS = Path.home() / ".claude" / "skills"
SKILL_BACKUPS = Path.home() / ".route-manager" / "backups" / "skills"
INSTALLED = Path.home() / "AppData" / "Local" / "Programs" / "Codex Web GPT"      # the packaged release this tree replaces
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def user_env(name: str) -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return str(winreg.QueryValueEx(key, name)[0])
    except OSError:
        return None


def set_user_env(name: str, value: str) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)


def wanted_env() -> dict[str, str]:
    """User environment: how the route reaches the launcher, and the switch that keeps ChatGPT Web from adding its own tray icon."""
    return {
        "PRO_CONSULT_LAUNCHER": str(PYTHONW),
        "PRO_CONSULT_LAUNCHER_ARGS": str(LAUNCHER),
        "CODEX_WEB_GPT_EXTERNAL_TRAY": "1",
    }


def tray_icon_owners(matches=None) -> list[int]:
    """Launcher processes that own a tray icon of their own (Electron makes one top-level NotifyIconHostWindow per Tray)."""
    import win32gui
    import win32process

    matches = matches or (lambda proc: services.launcher_process(proc.name(), proc.cmdline()))
    owners = set()

    def visit(hwnd, _):
        if "NotifyIcon" in win32gui.GetClassName(hwnd):
            pid = win32process.GetWindowThreadProcessId(hwnd)[1]
            try:
                if matches(psutil.Process(pid)):
                    owners.add(pid)
            except psutil.Error:
                pass
        return True

    win32gui.EnumWindows(visit, None)
    return sorted(owners)


def broadcast_environment() -> None:
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 2, 5000, None)


def powershell(script: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True,
                          creationflags=services.CREATE_NO_WINDOW, check=check)


def startup_fields() -> tuple[str, str] | None:
    """(program, arguments) of the login task, or None when it is not registered."""
    script = (f"$t = Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue; "
              "if ($t) { $t.Actions[0].Execute; $t.Actions[0].Arguments }")
    out = powershell(script).stdout.splitlines()
    return (out[0].strip(), out[1].strip()) if len(out) >= 2 else None


def startup_ok(fields: tuple[str, str] | None) -> bool:
    return fields is not None and Path(fields[0]) == PYTHONW and fields[1].strip('"') == str(APP)


def startup_enabled() -> bool:
    return startup_fields() is not None


def write_startup() -> None:
    """Register the login task: normal priority (tasks default to below normal, which the services would inherit),
    no time limit (the default stops a task after 72 hours), and on battery too."""
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ['USERNAME']}"
    script = (
        f"$a = New-ScheduledTaskAction -Execute '{PYTHONW}' -Argument '\"{APP}\"' -WorkingDirectory '{services.ROOT}'; "
        f"$t = New-ScheduledTaskTrigger -AtLogOn -User '{user}'; $t.Delay = '{TASK_DELAY}'; "
        f"$p = New-ScheduledTaskPrincipal -UserId '{user}' -LogonType Interactive -RunLevel Limited; "
        "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -Priority 4; "
        f"Register-ScheduledTask -TaskName '{TASK_NAME}' -Description 'Route Manager tray' "
        "-Action $a -Trigger $t -Principal $p -Settings $s -Force | Out-Null"
    )
    powershell(script, check=True)


def remove_startup() -> None:
    powershell(f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false -ErrorAction SilentlyContinue")


def adopt_usage_float_startup() -> list[str]:
    """The unified tray owns HUD startup; remove only this project's old Run entry."""
    location = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, location, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
            command, _ = winreg.QueryValueEx(key, "UsageFloat")
            old_root = str(services.ROOT.parent / "usage-float").lower()
            new_root = str(services.USAGE_FLOAT_ROOT).lower()
            if not any(root in str(command).replace("/", "\\").lower() for root in (old_root, new_root)):
                return []
            backup = services.STATE_DIR / "backups" / "usage-float-autostart.json"
            if not backup.exists():
                backup.parent.mkdir(parents=True, exist_ok=True)
                import json
                backup.write_text(json.dumps({"UsageFloat": command}), encoding="utf-8")
            winreg.DeleteValue(key, "UsageFloat")
            return [_tr("已将 Usage Float 开机启动交给 Route Manager")]
    except FileNotFoundError:
        return []


def skill_links() -> list[tuple[Path, Path]]:
    """(link, target) pairs: ~/.codex/skills/<name> -> skills/<name>, ~/.claude/skills/<name> -> the ~/.codex link."""
    pairs = []
    for skill in sorted(path for path in SKILLS.iterdir() if (path / "SKILL.md").is_file()):
        pairs.append((CODEX_SKILLS / skill.name, skill))
        pairs.append((CLAUDE_SKILLS / skill.name, CODEX_SKILLS / skill.name))
    return pairs


def is_link(path: Path) -> bool:
    try:
        return bool(os.lstat(path).st_file_attributes & 0x400)      # a junction or a symlink
    except OSError:
        return False


def skill_linked(link: Path, target: Path) -> bool:
    return is_link(link) and link.resolve() == target.resolve()


def link_skills() -> list[str]:
    done = []
    for link, target in skill_links():
        if skill_linked(link, target):
            continue
        if is_link(link):
            os.rmdir(link)
        elif link.exists():
            SKILL_BACKUPS.mkdir(parents=True, exist_ok=True)
            backup = SKILL_BACKUPS / f"{link.parent.parent.name.lstrip('.')}-{link.name}-{time.strftime('%Y%m%d-%H%M%S')}"
            shutil.move(str(link), str(backup))
            done.append(_tr('已移走 {0}（备份在 {1}）').format(f'{link}', f'{backup}'))
        link.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True,
                                encoding="mbcs", errors="replace")
        if result.returncode != 0:
            raise RuntimeError(_tr('无法链接 skill {0} -> {1}：{2}').format(f'{link}', f'{target}', f'{(result.stderr or result.stdout).strip()}'))
        done.append(_tr('已链接 skill {0} -> {1}').format(f'{link}', f'{target}'))
    return done


def tray_processes() -> list[psutil.Process]:
    found = []
    for proc in psutil.process_iter(["name", "cmdline"]):
        arguments = proc.info["cmdline"] or []
        if (proc.info["name"] or "").lower() in ("pythonw.exe", "python.exe") and any(Path(part) == APP for part in arguments[1:] if part.endswith(".pyw")):
            found.append(proc)
    return found


def check() -> list[tuple[str, bool, str]]:
    rows = []
    for module in ("pystray", "PIL", "psutil"):
        try:
            __import__(module)
            rows.append((_tr('python 依赖 {0}').format(f'{module}'), True, ""))
        except ImportError:
            rows.append((_tr('python 依赖 {0}').format(f'{module}'), False, f"pip install {module}"))
    rows.append(("pythonw", PYTHONW.is_file(), str(PYTHONW)))
    rows.append((_tr("ChatGPT Web 源码（工程内的 git 克隆）"), (services.CHATGPT_ROOT / ".git").exists(), str(services.CHATGPT_ROOT)))
    bun = services.bun_exe()
    rows.append(("Bun", bun is not None, str(bun or services.BUN)))
    built = services.chatgpt_built()
    rows.append((_tr("ChatGPT Web 已构建"), built, "" if built else "python tray\\updates.py build"))
    state = web_patch.status()
    rows.append((_tr("ChatGPT Web 本地补丁（无自带托盘图标、中文停止按钮等）"), state == "patched", state))
    icons = tray_icon_owners()
    rows.append((_tr("ChatGPT Web 没有自己的托盘图标"), not icons, _tr('进程 {0} 仍有托盘图标').format(f'{icons}') if icons else ""))
    installed = (INSTALLED / "Codex Web GPT.exe").exists()
    rows.append((_tr("旧的安装版 Codex Web GPT 已卸载"), not installed, str(INSTALLED) if installed else ""))
    task = startup_fields()
    rows.append((_tr('开机启动 Route Manager（计划任务 {0}）').format(f'{TASK_NAME}'), startup_ok(task),
                 _tr("未创建") if task is None else f"{task[0]} {task[1]}"))
    for old in SUPERSEDED:
        rows.append((_tr('旧开机项 {0} 已移除').format(f'{old.name}'), not old.exists(), _tr("仍在启动文件夹") if old.exists() else ""))
    for name, value in wanted_env().items():
        rows.append((_tr('环境变量 {0}').format(f'{name}'), user_env(name) == value, user_env(name) or _tr("未设置")))
    missing = [str(link) for link, target in skill_links() if not skill_linked(link, target)]
    rows.append((_tr('skill 链接（{0} → ~/.codex/skills → ~/.claude/skills）').format(f'{SKILLS}'), not missing, "、".join(missing)))
    state = easytier.snapshot()
    rows.append((_tr("EasyTier 后台任务（easytier/ 下的 SYSTEM 计划任务）"), state["state"] != easytier.MISSING,
                 easytier.label(state) if state["state"] != easytier.MISSING else "python tray/easytier_setup.py install"))
    running = tray_processes()
    rows.append((_tr("托盘进程"), len(running) == 1, _tr('{0} 个').format(f'{len(running)}')))
    return rows


def install() -> list[str]:
    done = []
    state = web_patch.status()
    patched_now = False
    if state == "original":
        if services.chatgpt_main_pid() is not None:
            stopped = services.stop_chatgpt_web()
            done.append(stopped)
            if services.chatgpt_main_pid() is not None:
                raise RuntimeError(_tr('无法给 ChatGPT Web 打补丁：{0}').format(f'{stopped}'))
        web_patch.apply()
        patched_now = True
        done.append(_tr("已给 ChatGPT Web 源码打补丁"))
    elif state != "patched":
        raise RuntimeError(_tr('无法给 ChatGPT Web 打补丁：{0}').format(f'{state}'))
    if patched_now or not services.chatgpt_built():
        updates.build()
        done.append(_tr("已构建 ChatGPT Web"))
    if not startup_ok(startup_fields()):
        write_startup()
        done.append(_tr('已注册开机启动计划任务 {0}').format(f'{TASK_NAME}'))
    for old in SUPERSEDED:
        if old.exists():
            BACKUPS.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old), str(BACKUPS / old.name))
            done.append(_tr('已移走旧开机项 {0}（备份在 {1}）').format(f'{old.name}', f'{BACKUPS}'))
    changed = False
    for name, value in wanted_env().items():
        if user_env(name) != value:
            set_user_env(name, value)
            changed = True
            done.append(_tr('已设置环境变量 {0}').format(f'{name}'))
    if changed:
        broadcast_environment()
    done.extend(adopt_usage_float_startup())
    done.extend(link_skills())
    return done or [_tr("无需改动")]


def close_tray(process: psutil.Process, wait: float = 8.0) -> str:
    """Ask the tray's own window to stop its icon loop, so Windows drops the icon; end the process if it does not go."""
    import win32gui
    import win32process
    from pystray._util import win32 as pystray_win32

    def post(hwnd, _):
        if win32process.GetWindowThreadProcessId(hwnd)[1] == process.pid and win32gui.GetClassName(hwnd).endswith("SystemTrayIcon"):
            win32gui.PostMessage(hwnd, pystray_win32.WM_STOP, 0, 0)
        return True

    win32gui.EnumWindows(post, None)
    try:
        process.wait(wait)
        return "closed"
    except psutil.TimeoutExpired:
        process.kill()
        return "killed"


def restart() -> list[str]:
    done = []
    for proc in tray_processes():
        done.append(_tr('托盘 {0} {1}').format(f'{proc.pid}', f'{close_tray(proc)}'))
    services.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (services.STATE_DIR / "tray-start.log").open("ab", buffering=0) as log:
        subprocess.Popen(
            [str(PYTHONW), "-X", "faulthandler", str(APP)], cwd=str(services.ROOT),
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        )
    time.sleep(2)
    now = tray_processes()
    done.append(_tr('托盘已启动（{0} 个进程）').format(f'{len(now)}') if len(now) == 1 else _tr('托盘进程数异常：{0}').format(f'{len(now)}'))
    return done


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("check", "install", "restart", "all"))
    action = parser.parse_args().action
    status = 0
    if action in ("install", "all"):
        for line in install():
            print(line)
    if action in ("restart", "all"):
        for line in restart():
            print(line)
    if action in ("check", "all"):
        for name, ok, detail in check():
            print(f"[{'OK' if ok else '!!'}] {name}" + (f"  {detail}" if detail else ""))
            status |= 0 if ok else 1
    return status


if __name__ == "__main__":
    raise SystemExit(main())
