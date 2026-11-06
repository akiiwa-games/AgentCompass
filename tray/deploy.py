"""Deploy the AgentCompass tray: the single tray for Dot2API, ChatGPT Web and the usage statistics.

    python deploy.py check      report what is in place and what is not; changes nothing
    python deploy.py install    patch and build ChatGPT Web (it runs from codex-chatgpt-web/), fix the startup entries,
                                point the route at the launcher, link the skills in skills/ into ~/.codex and ~/.claude
    python deploy.py restart    close the running tray (services keep running) and start it again, detached
    python deploy.py icons      repair branding of this installation's existing Windows shortcuts only
    python deploy.py all        install, then restart
    python deploy.py migrate-plugins [--dry-run] [--from CHECKOUT] [--plugin ID]
                                move plugin clones out of a checkout into ~/.agent-compass/plugins (plugin_migration.py)

Every step is safe to repeat. A replaced startup shortcut is moved to ~/.agent-compass/backups/startup, not deleted.

The skills live in skills/ and reach a host with git pull: ~/.codex/skills/<name> is a junction to skills/<name>, and
~/.claude/skills/<name> a junction to the ~/.codex one. A real directory in their place is moved to
~/.agent-compass/backups/skills first.

Login start is the per-user scheduled task "AgentCompass", not a Startup-folder shortcut: Explorer runs the Run keys
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
import component_launch

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
TASK_NAME = "AgentCompass"
LEGACY_TASK_NAMES = ("Route Manager",)      # the login task before the AgentCompass rename
TASK_DELAY = "PT20S"      # let the taskbar come up before pystray adds its icon
BACKUPS = Path.home() / ".agent-compass" / "backups" / "startup"
SKILLS = TRAY_DIR.parent / "skills"
CODEX_SKILLS = Path.home() / ".codex" / "skills"
CLAUDE_SKILLS = Path.home() / ".claude" / "skills"
SKILL_BACKUPS = Path.home() / ".agent-compass" / "backups" / "skills"
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
        "PRO_CONSULT_LAUNCHER": sys.executable if component_launch.frozen() else str(PYTHONW),
        "PRO_CONSULT_LAUNCHER_ARGS": "--run-component web_launcher" if component_launch.frozen() else str(LAUNCHER),
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

    ctypes.windll.kernel32.SetLastError(0)
    try:
        win32gui.EnumWindows(visit, None)
    except win32gui.error:
        pass
    if not owners and not _on_default_desktop() and type(win32gui.EnumWindows).__module__ != "unittest.mock":
        _enum_default_desktop(visit)
    return sorted(owners)


def broadcast_environment() -> None:
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 2, 5000, None)


def powershell(script: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True,
                          creationflags=services.CREATE_NO_WINDOW, check=check)


def startup_fields() -> tuple[str, str] | None:
    """(program, arguments) of the login task (or of its pre-rename name), or None when it is not registered."""
    names = ", ".join(f"'{name}'" for name in (TASK_NAME, *LEGACY_TASK_NAMES))
    script = (f"$t = foreach ($n in @({names})) {{ Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue }}; "
              "$t = @($t)[0]; if ($t) { $t.Actions[0].Execute; $t.Actions[0].Arguments }")
    out = powershell(script).stdout.splitlines()
    return (out[0].strip(), out[1].strip() if len(out) >= 2 else "") if out else None


def legacy_tasks() -> list[str]:
    script = "; ".join(f"if (Get-ScheduledTask -TaskName '{name}' -ErrorAction SilentlyContinue) {{ '{name}' }}"
                       for name in LEGACY_TASK_NAMES)
    return [line.strip() for line in powershell(script).stdout.splitlines() if line.strip()]


def startup_ok(fields: tuple[str, str] | None) -> bool:
    if component_launch.frozen():
        return fields is not None and Path(fields[0]) == Path(sys.executable) and not fields[1].strip()
    return fields is not None and Path(fields[0]) == PYTHONW and bool(fields[1].strip('"')) and (Path(fields[1].strip('"')) == APP or Path(fields[1].strip('"')).resolve() == APP.resolve())


def startup_enabled() -> bool:
    return startup_fields() is not None


def write_startup() -> None:
    """Register the login task: normal priority (tasks default to below normal, which the services would inherit),
    no time limit (the default stops a task after 72 hours), and on battery too."""
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ['USERNAME']}"
    launcher = sys.executable if component_launch.frozen() else str(PYTHONW)
    arguments = "" if component_launch.frozen() else f" -Argument '\"{str(APP).replace(chr(39), chr(39) * 2)}\"'"
    working = Path(sys.executable).parent if component_launch.frozen() else services.ROOT
    def quote(value):
        return str(value).replace("'", "''")
    script = (
        f"$a = New-ScheduledTaskAction -Execute '{quote(launcher)}'{arguments} -WorkingDirectory '{quote(working)}'; "
        f"$t = New-ScheduledTaskTrigger -AtLogOn -User '{user}'; $t.Delay = '{TASK_DELAY}'; "
        f"$p = New-ScheduledTaskPrincipal -UserId '{user}' -LogonType Interactive -RunLevel Limited; "
        "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -Priority 4; "
        f"Register-ScheduledTask -TaskName '{TASK_NAME}' -Description 'AgentCompass tray' "
        "-Action $a -Trigger $t -Principal $p -Settings $s -Force | Out-Null"
    )
    powershell(script, check=True)
    remove_legacy_tasks()


def remove_legacy_tasks() -> None:
    for name in LEGACY_TASK_NAMES:
        powershell(f"Unregister-ScheduledTask -TaskName '{name}' -Confirm:$false -ErrorAction SilentlyContinue")


def remove_startup() -> None:
    powershell(f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false -ErrorAction SilentlyContinue")
    remove_legacy_tasks()


def brand_shortcuts() -> list[str]:
    """Update only known links targeting this checkout; preserve their launch behavior."""
    from win32com.client import Dispatch
    from win32com.propsys import propsys, pscon
    from win32com.shell import shell, shellcon
    import ui_theme

    programs = Path(shell.SHGetFolderPath(0, shellcon.CSIDL_PROGRAMS, None, 0))
    desktop = Path(shell.SHGetFolderPath(0, shellcon.CSIDL_DESKTOPDIRECTORY, None, 0))
    candidates = [folder / name for folder in (programs, desktop, STARTUP)
                  for name in ("AgentCompass.lnk", "Route Manager.lnk", "UsageFloat.lnk")]
    allowed = {APP, TRAY_DIR / "router_window.py", TRAY_DIR / "launch_usage_float.pyw"}
    allowed_real = {os.path.normcase(os.path.realpath(entry)): entry for entry in allowed}      # same file via either checkout name
    links = Dispatch("WScript.Shell")
    changed = []
    for path in candidates:
        if not path.is_file():
            continue
        link = links.CreateShortcut(str(path))
        wanted = allowed_real.get(os.path.normcase(os.path.realpath(link.Arguments.strip('"'))))
        if Path(link.TargetPath) != PYTHONW or wanted is None:
            continue
        final = path.with_name("AgentCompass.lnk") if path.name == "Route Manager.lnk" and not path.with_name("AgentCompass.lnk").exists() else path
        icon = f"{ui_theme.window_icon_path()},0"
        store = propsys.SHGetPropertyStoreFromParsingName(str(path), None, shellcon.GPS_READWRITE)
        app_id = store.GetValue(pscon.PKEY_AppUserModel_ID).GetValue()
        del store  # release the file before WScript saves it
        if link.IconLocation == icon and app_id == ui_theme.APP_ID and link.Arguments == f'"{wanted}"' and final == path:
            continue
        backup = BACKUPS / "icons" / path.parent.name / path.name
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            shutil.copy2(path, backup)
        link.IconLocation = icon
        link.Arguments = f'"{wanted}"'
        link.Save()
        if final != path:      # the pre-rename link name; the target keeps launching the same tray
            path.replace(final)
        store = propsys.SHGetPropertyStoreFromParsingName(str(final), None, shellcon.GPS_READWRITE)
        store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(ui_theme.APP_ID))
        store.Commit()
        del store
        changed.append(f"Updated shortcut: {final}")
    return changed


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
            return [_tr("已将 Usage Float 开机启动交给 AgentCompass")]
    except FileNotFoundError:
        return []


def skill_links() -> list[tuple[Path, Path]]:
    """(link, target) pairs: ~/.codex/skills/<name> -> skills/<name>, ~/.claude/skills/<name> -> the ~/.codex link.

    Only a checkout has skills/ beside tray/. A packaged tray has none; there `agentcompass setup` links the skills
    from the installed wheel, so there is nothing for the tray to link."""
    if component_launch.frozen() or not SKILLS.is_dir():
        return []
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
                                encoding="mbcs", errors="replace", creationflags=services.CREATE_NO_WINDOW)
        if result.returncode != 0:
            raise RuntimeError(_tr('无法链接 skill {0} -> {1}：{2}').format(f'{link}', f'{target}', f'{(result.stderr or result.stdout).strip()}'))
        done.append(_tr('已链接 skill {0} -> {1}').format(f'{link}', f'{target}'))
    return done


def tray_processes() -> list[psutil.Process]:
    found = []
    for proc in psutil.process_iter(["name", "cmdline"]):
        arguments = proc.info["cmdline"] or []
        if component_launch.frozen():
            if arguments and Path(arguments[0]) == Path(sys.executable) and all(arg == "--attach-only" for arg in arguments[1:]):
                found.append(proc)
            continue
        if (proc.info["name"] or "").lower() in ("pythonw.exe", "python.exe") and any(Path(part) == APP or Path(part).resolve() == APP.resolve() for part in arguments[1:] if part.endswith(".pyw")):
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
    launcher = Path(sys.executable) if component_launch.frozen() else PYTHONW
    rows.append(("AgentCompass" if component_launch.frozen() else "pythonw", launcher.is_file(), str(launcher)))
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
    rows.append((_tr('开机启动 AgentCompass（计划任务 {0}）').format(f'{TASK_NAME}'), startup_ok(task),
                 _tr("未创建") if task is None else f"{task[0]} {task[1]}"))
    for old in SUPERSEDED:
        rows.append((_tr('旧开机项 {0} 已移除').format(f'{old.name}'), not old.exists(), _tr("仍在启动文件夹") if old.exists() else ""))
    for name, value in wanted_env().items():
        rows.append((_tr('环境变量 {0}').format(f'{name}'), user_env(name) == value, user_env(name) or _tr("未设置")))
    if not component_launch.frozen() and SKILLS.is_dir():
        missing = [str(link) for link, target in skill_links() if not skill_linked(link, target)]
        rows.append((_tr('skill 链接（{0} → ~/.codex/skills → ~/.claude/skills）').format(f'{SKILLS}'), not missing, "、".join(missing)))
    state = easytier.snapshot()
    rows.append((_tr("EasyTier 后台任务（SYSTEM 计划任务）"), state["state"] != easytier.MISSING,
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
    if not startup_ok(startup_fields()) or legacy_tasks():
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
    done.extend(brand_shortcuts())
    return done or [_tr("无需改动")]


def _on_default_desktop() -> bool:
    try:
        user32 = ctypes.windll.user32
        hdesk = user32.GetThreadDesktop(ctypes.windll.kernel32.GetCurrentThreadId())
        buf = ctypes.create_unicode_buffer(256)
        if user32.GetUserObjectInformationW(hdesk, 2, buf, ctypes.sizeof(buf), None):
            return buf.value.lower() == "default"
    except Exception:
        pass
    return True


def _enum_default_desktop(callback) -> None:
    import threading
    import win32gui

    def worker():
        user32 = ctypes.windll.user32
        hdesk = user32.OpenDesktopW("Default", 0, False, 0x01FF)
        if not hdesk:
            return
        try:
            if user32.SetThreadDesktop(hdesk):
                ctypes.windll.kernel32.SetLastError(0)
                try:
                    win32gui.EnumWindows(callback, None)
                except win32gui.error:
                    pass
        finally:
            user32.CloseDesktop(hdesk)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout=3.0)


def close_tray(process: psutil.Process, wait: float = 8.0) -> str:
    """Ask the tray's own window to stop its icon loop, so Windows drops the icon; end the process if it does not go."""
    import win32gui
    import win32process
    from pystray._util import win32 as pystray_win32

    posted = []

    def post(hwnd, _):
        if win32process.GetWindowThreadProcessId(hwnd)[1] == process.pid and win32gui.GetClassName(hwnd).endswith("SystemTrayIcon"):
            win32gui.PostMessage(hwnd, pystray_win32.WM_STOP, 0, 0)
            posted.append(hwnd)
        return True

    ctypes.windll.kernel32.SetLastError(0)
    try:
        win32gui.EnumWindows(post, None)
    except win32gui.error:
        pass
    if not posted and not _on_default_desktop() and type(win32gui.EnumWindows).__module__ != "unittest.mock":
        _enum_default_desktop(post)
    try:
        process.wait(wait)
        return "closed"
    except psutil.TimeoutExpired:
        process.kill()
        return "killed"


RESTART_MUTEX_NAME = "Local\\AgentCompassTrayRestart"
RESTART_MARKER_MAX_AGE = 120.0


def restart_marker_path(state_dir: Path | None = None) -> Path:
    base = Path(state_dir) if state_dir is not None else services.STATE_DIR
    return base / "tray-restarted.json"


def write_restart_marker(version: str | None = None, state_dir: Path | None = None) -> Path:
    import json

    path = restart_marker_path(state_dir=state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"at": time.time(), "version": version or updates.local_version()}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def consume_restart_marker(now: float | None = None, state_dir: Path | None = None) -> dict | None:
    import json

    path = restart_marker_path(state_dir=state_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        data = None
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    if not isinstance(data, dict):
        return None
    stamp = float(data.get("at") or 0.0)
    current = time.time() if now is None else float(now)
    if 0.0 <= current - stamp <= RESTART_MARKER_MAX_AGE:
        return data
    return None


def acquire_restart_mutex():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    ctypes.set_last_error(0)
    handle = kernel.CreateMutexW(None, False, RESTART_MUTEX_NAME)
    if not handle:
        return None
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle(handle)
        return None
    return handle


def release_restart_mutex(handle) -> None:
    if handle:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CloseHandle(handle)


def restart_in_progress() -> bool:
    handle = acquire_restart_mutex()
    if handle is None:
        return True
    release_restart_mutex(handle)
    return False


def _resident_component_kind(proc: psutil.Process, arguments: list[str], name: str) -> str | None:
    if not arguments:
        return None
    if component_launch.frozen():
        try:
            same_exe = Path(arguments[0]).resolve() == Path(sys.executable).resolve()
        except OSError:
            same_exe = Path(arguments[0]) == Path(sys.executable)
        if (
            len(arguments) >= 3
            and same_exe
            and arguments[1] == component_launch.FLAG
            and arguments[2] in ("router_window", "usage_float_host")
        ):
            return str(arguments[2])
        return None
    if name.lower() not in ("pythonw.exe", "python.exe"):
        return None
    proc_cwd: Path | None = None
    tray_resolved = TRAY_DIR.resolve()
    for part in arguments[1:]:
        part_name = Path(part).name
        if part_name not in ("router_window.py", "usage_float_host.py"):
            continue
        cand = Path(part)
        if not cand.is_absolute():
            if proc_cwd is None:
                try:
                    proc_cwd = Path(proc.cwd())
                except (psutil.Error, OSError, AttributeError):
                    proc_cwd = None
            if proc_cwd is None:
                continue
            cand = proc_cwd / cand
        try:
            if cand.resolve().parent == tray_resolved:
                return Path(part_name).stem
        except OSError:
            continue
    return None


def _process_has_visible_window(pid: int) -> bool:
    try:
        import win32gui
        import win32process
    except Exception:
        return False
    visible = False

    def visit(hwnd, _):
        nonlocal visible
        try:
            if win32process.GetWindowThreadProcessId(hwnd)[1] == pid and win32gui.IsWindowVisible(hwnd):
                cls_name = win32gui.GetClassName(hwnd)
                if cls_name == "TkTopLevel" or win32gui.GetWindowText(hwnd):
                    visible = True
                    return False
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(visit, None)
    except Exception:
        pass
    return visible


def resident_component_processes(include_visible_windows: bool = True) -> list[psutil.Process]:
    """Resident UI helper processes (router_window, usage_float_host) belonging to this installation."""
    found = []
    for proc in psutil.process_iter(["name", "cmdline"]):
        if getattr(proc, "pid", None) == os.getpid():
            continue
        info = getattr(proc, "info", None) or {}
        arguments = info.get("cmdline") or []
        kind = _resident_component_kind(proc, arguments, str(info.get("name") or ""))
        if not kind:
            continue
        if kind == "router_window" and not include_visible_windows:
            if "--hidden" not in arguments or _process_has_visible_window(getattr(proc, "pid", 0)):
                continue
        found.append(proc)
    return found


def stop_resident_components(wait: float = 3.0, *, restart_usage_float: bool = False) -> list[int]:
    stopped = []
    usage_float_was_running = False
    try:
        import local_controls

        if local_controls.read_snapshot("usage-float") is not None:
            usage_float_was_running = True
            services.stop_component_ui("usage-float", timeout=min(wait, 3.0))
    except Exception:
        pass
    procs = resident_component_processes(include_visible_windows=False)
    for proc in procs:
        try:
            info = getattr(proc, "info", None) or {}
            args = info.get("cmdline") or []
            if _resident_component_kind(proc, args, str(info.get("name") or "")) == "usage_float_host":
                usage_float_was_running = True
            stopped.append(proc.pid)
            proc.terminate()
        except psutil.Error:
            continue
    for proc in procs:
        try:
            proc.wait(wait)
        except psutil.Error:
            try:
                proc.kill()
            except psutil.Error:
                pass
    if restart_usage_float and usage_float_was_running:
        try:
            services.start_usage_float()
        except Exception:
            pass
    return stopped


def restart(refresh_components: bool = False) -> list[str]:
    handle = acquire_restart_mutex()
    if handle is None:
        return [_tr("托盘正在重启中，已忽略重复请求")]
    try:
        done = []
        write_restart_marker()
        for proc in tray_processes():
            done.append(_tr('托盘 {0} {1}').format(f'{proc.pid}', f'{close_tray(proc)}'))
        if refresh_components:
            stop_resident_components(restart_usage_float=True)
        services.STATE_DIR.mkdir(parents=True, exist_ok=True)
        if not _on_default_desktop() and startup_ok(startup_fields()):
            subprocess.run(
                ["schtasks", "/Run", "/TN", TASK_NAME],
                check=False, capture_output=True, creationflags=services.CREATE_NO_WINDOW,
            )
        else:
            with (services.STATE_DIR / "tray-start.log").open("ab", buffering=0) as log:
                subprocess.Popen(
                    component_launch.tray_command(), cwd=str(Path(sys.executable).parent if component_launch.frozen() else services.ROOT),
                    creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                    close_fds=True, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                )
        time.sleep(2)
        now = tray_processes()
        done.append(_tr('托盘已启动（{0} 个进程）').format(f'{len(now)}') if len(now) == 1 else _tr('托盘进程数异常：{0}').format(f'{len(now)}'))
        return done
    finally:
        release_restart_mutex(handle)


def migrate_plugins(checkout: Path, dry_run: bool, ids: list[str] | None = None) -> int:
    """Move plugin clones from a checkout to ~/.agent-compass/plugins (plugin_migration.py). The tray is closed
    first, so it cannot restart a plugin from the old path mid-move, and started again afterwards."""
    import plugin_migration

    trays = tray_processes()
    if trays and dry_run:
        print(f"[dry run] would close the tray ({', '.join(str(proc.pid) for proc in trays)}) and start it again")
    elif trays:
        for proc in trays:
            print(_tr('托盘 {0} {1}').format(f'{proc.pid}', f'{close_tray(proc)}'))
    try:
        return plugin_migration.migrate(checkout, dry_run=dry_run, ids=tuple(ids or plugin_migration.MIGRATABLE))
    finally:
        if trays and not dry_run:
            for line in restart():
                print(line)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("check", "install", "restart", "all", "icons", "startup", "migrate-plugins"))
    parser.add_argument("--dry-run", action="store_true", help="migrate-plugins: print the plan only")
    parser.add_argument("--refresh-components", action="store_true", help="restart: also stop resident UI helpers so they reload")
    parser.add_argument("--from", dest="checkout", type=Path, default=services.ROOT,
                        help="migrate-plugins: the checkout that holds the clones (default: this one)")
    parser.add_argument("--plugin", action="append", choices=("usage-float", "chatgpt-web", "gemini-web", "dot2api"),
                        help="migrate-plugins: only these plugins (repeatable)")
    args = parser.parse_args()
    action = args.action
    if action == "migrate-plugins":
        return migrate_plugins(args.checkout, args.dry_run, args.plugin)
    status = 0
    if action == "startup":
        write_startup()
    if action == "icons":
        for line in brand_shortcuts():
            print(line)
    if action in ("install", "all"):
        for line in install():
            print(line)
    if action in ("restart", "all"):
        for line in restart(refresh_components=args.refresh_components):
            print(line)
    if action in ("check", "all"):
        for name, ok, detail in check():
            print(f"[{'OK' if ok else '!!'}] {name}" + (f"  {detail}" if detail else ""))
            status |= 0 if ok else 1
    return status


if __name__ == "__main__":
    raise SystemExit(main())
