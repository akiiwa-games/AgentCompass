"""One-time and recovery operations for the EasyTier node, run as administrator (stdlib only).

    python easytier_setup.py check
    python easytier_setup.py install --binaries DIR --config FILE [--hook FILE] [--legacy LEGACY.json] [--switch]
    python easytier_setup.py install --migrate-from OLD_HOME [--expect-peer HOSTNAME] [--keep-old] [--dry-run]
    python easytier_setup.py switch   --expect-peer HOSTNAME   stop the legacy runtime, start the task, roll back on failure
    python easytier_setup.py recover                           roll back unless the last switch finished ok
    python easytier_setup.py rollback                          put the legacy runtime back
    python easytier_setup.py cleanup                           delete the helper tasks left by detach-switch / arm-recovery
    python easytier_setup.py uninstall                         remove the task (files stay)

`install` copies the official v2.6.4 binaries and this host's own config (never another host's) into
%ProgramData%/AgentCompass/easytier (easytier_paths.py says why), locks it, its AgentCompass parent and
bin/private/logs/state down to SYSTEM and Administrators (read for others where there is no secret), and registers the SYSTEM task "AgentCompass EasyTier" with a start trigger at boot. The only right the
installing user gets on the task is read, run and stop, so the unelevated tray can control it but not rewrite it.
It relaunches itself elevated (UAC, hidden) when needed. The legacy spec (JSON) names how the previous runtime is
stopped and brought back: {"task": "...", "command": [...], "cwd": "...", "kill_cmdline_contains": [...]}.

`install --migrate-from` moves a node installed before that home existed (the checkout-era <root>/easytier) to
it: it takes bin, private (config and hook), state and backup from the old home, stops the old node, points the
task at the new supervisor, and checks the node comes up from the new bin (and sees every --expect-peer). On
success the old home is deleted (--keep-old keeps it); on failure the task goes back to the old supervisor and the
new copy is removed. `--dry-run` prints the plan without elevating or changing anything.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from xml.sax.saxutils import escape

import component_launch
import easytier_paths

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

ROOT = Path(__file__).resolve().parents[1]
HOME = easytier_paths.home()      # the installed node; install itself always targets easytier_paths.product_home()
BIN, PRIVATE, LOGS, STATE, BACKUP = (HOME / name for name in ("bin", "private", "logs", "state", "backup"))
TASK_NAME = "AgentCompass EasyTier"
RECOVER_TASK = "AgentCompass EasyTier Recover"
SWITCH_TASK = "AgentCompass EasyTier Switch"
# Names before the AgentCompass rename. install deletes them after registering TASK_NAME; until then they still run the node.
LEGACY_TASKS = ("Route Manager EasyTier", "Route Manager EasyTier Recover", "Route Manager EasyTier Switch")
USER_LOG_DIR = Path.home() / ".agent-compass" / "easytier-setup"
PINNED = {"easytier-core.exe": "da7eb2d24b5416f3d3407636949e964a0750e3f9dc53a828cb6799a57ead445d"}
REQUIRED = ("easytier-core.exe", "easytier-cli.exe", "Packet.dll", "wintun.dll")
OPTIONAL = ("WinDivert64.sys",)
SUPERVISOR_SOURCE = Path(__file__).with_name("easytier_supervisor.ps1")
SYSTEM_SID, ADMINS_SID, USERS_SID = "S-1-5-18", "S-1-5-32-544", "S-1-5-32-545"
POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
LEGACY_STATE = STATE / "legacy.json"
SWITCH_STATE = STATE / "switch.json"
_log_file: Path | None = None


def use_home(home: Path) -> None:
    """Point every path in this module at another EasyTier home."""
    global HOME, BIN, PRIVATE, LOGS, STATE, BACKUP, LEGACY_STATE, SWITCH_STATE
    HOME = Path(home)
    BIN, PRIVATE, LOGS, STATE, BACKUP = (HOME / name for name in ("bin", "private", "logs", "state", "backup"))
    LEGACY_STATE, SWITCH_STATE = STATE / "legacy.json", STATE / "switch.json"


def log(message: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    print(line, flush=True)
    if _log_file is not None:
        _log_file.parent.mkdir(parents=True, exist_ok=True)
        with _log_file.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_admin() -> bool:
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def run(args: list[str], check: bool = True, timeout: float = 120) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=timeout,
                            creationflags=CREATE_NO_WINDOW, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(f"{Path(args[0]).name} 失败（{result.returncode}）：{(result.stderr or result.stdout).strip()[:400]}")
    return result


def powershell(script: str, check: bool = True, timeout: float = 120) -> subprocess.CompletedProcess:
    encoded = base64.b64encode(("$ProgressPreference='SilentlyContinue'\n" + script).encode("utf-16-le")).decode()
    return run([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
               check=check, timeout=timeout)


def current_user_sid() -> str:
    output = run(["whoami", "/user", "/fo", "csv", "/nh"]).stdout.strip()
    return output.rsplit(",", 1)[1].strip().strip('"')


# ---------- files and ACLs ----------

def lock_down(path: Path, others_read: bool) -> None:
    """SYSTEM and Administrators full control; Users read/execute only where nothing secret lives."""
    grants = [f"*{SYSTEM_SID}:(OI)(CI)F", f"*{ADMINS_SID}:(OI)(CI)F"]
    if others_read:
        grants.append(f"*{USERS_SID}:(OI)(CI)RX")
    run(["icacls", str(path), "/grant:r", *grants])
    run(["icacls", str(path), "/inheritance:r"])
    run(["icacls", str(path), "/setowner", f"*{ADMINS_SID}"])


def is_reparse_point(path: Path) -> bool:
    try:
        return bool(getattr(os.lstat(path), "st_file_attributes", 0) & 0x400)
    except OSError:
        return False


def prepare_home() -> None:
    """Create the home and lock it before anything is copied in. A pre-made junction or symlink is refused: an
    unprivileged user may create folders under ProgramData, and must not steer where SYSTEM's program lands."""
    parent = easytier_paths.product_parent()
    locked_parent = parent if parent is not None and HOME == easytier_paths.product_home() else None
    for directory in (locked_parent, HOME):
        if directory is not None and is_reparse_point(directory):
            raise RuntimeError(f"{directory} 是联接或符号链接，拒绝安装到这里")
    if locked_parent is not None:
        locked_parent.mkdir(parents=True, exist_ok=True)
        lock_down(locked_parent, True)
    for directory in (HOME, BIN, PRIVATE, LOGS, STATE, BACKUP):
        directory.mkdir(parents=True, exist_ok=True)
        if is_reparse_point(directory):
            raise RuntimeError(f"{directory} 是联接或符号链接，拒绝安装到这里")
    # Lock the parent first, then the children that differ from it.
    lock_down(HOME, True)
    lock_down(BIN, True)
    lock_down(LOGS, True)
    lock_down(STATE, True)
    lock_down(PRIVATE, False)
    lock_down(BACKUP, False)


def stage_files(binaries: Path, config: Path, hook: Path | None) -> list[str]:
    done = []
    for name in REQUIRED:
        if not (binaries / name).is_file():
            raise FileNotFoundError(f"{binaries} 里缺少 {name}")
    for name, expected in PINNED.items():
        if sha256(binaries / name) != expected:
            raise RuntimeError(f"{name} 的 SHA-256 与固定的 v2.6.4 官方版本不符")
    if not SUPERVISOR_SOURCE.is_file():
        raise FileNotFoundError(str(SUPERVISOR_SOURCE))
    prepare_home()
    manifest = {}
    sources = [(binaries / name, BIN / name) for name in (*REQUIRED, *OPTIONAL) if (binaries / name).is_file()]
    sources.append((SUPERVISOR_SOURCE, BIN / "easytier-supervisor.ps1"))
    for source, target in sources:
        digest = sha256(source)
        if not target.is_file() or sha256(target) != digest:
            shutil.copy2(source, target)
            done.append(f"已复制 {target.name}")
        manifest[target.name] = digest
    (BIN / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    target = PRIVATE / "node.toml"
    if config.resolve() != target.resolve():
        shutil.copy2(config, target)
        done.append("已放入本机的 node.toml")
    if hook is not None:
        shutil.copy2(hook, PRIVATE / "pre-start.ps1")
        done.append("已放入本机的 pre-start.ps1")
    return done


def check_config() -> None:
    result = run([str(BIN / "easytier-core.exe"), "--config-file", str(PRIVATE / "node.toml"), "--check-config",
                  "--disable-env-parsing"], check=False, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"easytier-core --check-config 未通过（详情见 {LOGS}，不在此输出配置内容）")


# ---------- scheduled tasks ----------

def task_xml(command: str, arguments: str, trigger: str, restart: bool, start_boundary: str | None = None) -> str:
    triggers = {
        "boot": "<BootTrigger><Enabled>true</Enabled></BootTrigger>",
        "none": "",
        "once": f"<TimeTrigger><StartBoundary>{start_boundary}</StartBoundary><Enabled>true</Enabled></TimeTrigger>",
    }[trigger]
    restart_xml = "<RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>" if restart else ""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>{triggers}</Triggers>
  <Principals><Principal id="A"><UserId>{SYSTEM_SID}</UserId><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Hidden>true</Hidden>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    {restart_xml}
  </Settings>
  <Actions Context="A"><Exec><Command>{escape(command)}</Command><Arguments>{escape(arguments)}</Arguments></Exec></Actions>
</Task>"""


def register_task(name: str, xml: str, user_sid: str | None) -> None:
    sddl = f"D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;GRGX;;;{user_sid})" if user_sid else "D:P(A;;FA;;;SY)(A;;FA;;;BA)"
    encoded_xml = base64.b64encode(xml.encode("utf-8")).decode()
    powershell(f"""
$ErrorActionPreference = 'Stop'
$xml = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_xml}'))
$service = New-Object -ComObject Schedule.Service
$service.Connect()
$null = $service.GetFolder('\\').RegisterTask('{name}', $xml, 6, $null, $null, 5, '{sddl}')
""")


def task_exists(name: str) -> bool:
    return run(["schtasks", "/Query", "/TN", name], check=False).returncode == 0


def task_running(name: str) -> bool:
    return powershell(f"if ((Get-ScheduledTask -TaskName '{name}' -ErrorAction SilentlyContinue).State -eq 'Running') {{ 'yes' }}",
                      check=False).stdout.strip() == "yes"


def delete_task(name: str, end: bool = True) -> None:
    """Delete a task. A task deleting itself must not End itself first, or it dies before the delete."""
    if task_exists(name):
        if end:
            run(["schtasks", "/End", "/TN", name], check=False)
        run(["schtasks", "/Delete", "/TN", name, "/F"], check=False)


def register_main_task(user_sid: str, bin_dir: Path | None = None) -> None:
    supervisor = (bin_dir or BIN) / "easytier-supervisor.ps1"
    arguments = f'-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{supervisor}"'
    register_task(TASK_NAME, task_xml(str(POWERSHELL), arguments, "boot", True), user_sid)


def register_helper(name: str, subcommand: list[str], minutes_from_now: int | None) -> None:
    launcher, *args = component_launch.command(__file__, *subcommand, windowed=False)
    arguments = subprocess.list2cmdline(args)
    boundary = None
    if minutes_from_now is not None:
        boundary = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() + minutes_from_now * 60))
    register_task(name, task_xml(launcher, arguments, "once" if boundary else "none", False, boundary), None)


# ---------- processes and health ----------

def processes() -> list[dict]:
    output = powershell("""
Get-CimInstance Win32_Process | Where-Object { $_.Name -in 'easytier-core.exe','powershell.exe','pwsh.exe','cmd.exe' } |
  Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,CommandLine | ConvertTo-Json -Compress
""").stdout.strip()
    if not output:
        return []
    data = json.loads(output)
    return data if isinstance(data, list) else [data]


def cores() -> list[dict]:
    return [proc for proc in processes() if proc["Name"].lower() == "easytier-core.exe"]


def own_core(proc: dict) -> bool:
    return (proc.get("ExecutablePath") or "").lower() == str(BIN / "easytier-core.exe").lower()


def peer_names() -> list[str] | None:
    try:
        import service_ports
        portal = f"127.0.0.1:{service_ports.get('easytier_rpc')}"
    except Exception:
        portal = "127.0.0.1:15888"
    result = run([str(BIN / "easytier-cli.exe"), "-p", portal, "-o", "json", "peer"], check=False, timeout=15)
    if result.returncode != 0:
        return None
    try:
        return [str(peer.get("hostname")) for peer in json.loads(result.stdout) if peer.get("cost") != "Local"]
    except ValueError:
        return None


def wait_until(predicate, timeout: float, interval: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def healthy(expect: list[str]) -> bool:
    found = cores()
    if len(found) != 1 or not own_core(found[0]):
        return False
    names = peer_names()
    return names is not None and all(expected in names for expected in expect)


# ---------- legacy runtime ----------

def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def stop_legacy(spec: dict) -> list[str]:
    """Stop the previous runtime: its scheduled task and every wrapper/core whose command line is its own."""
    stopped = []
    if spec.get("task") and task_exists(spec["task"]):
        run(["schtasks", "/End", "/TN", spec["task"]], check=False)
        stopped.append(f"任务 {spec['task']}")
    needles = [text.lower() for text in spec.get("kill_cmdline_contains", [])]
    me = os.getpid()
    for proc in processes():
        command = (proc.get("CommandLine") or "").lower()
        if proc["ProcessId"] == me or own_core(proc) or not needles or not any(needle in command for needle in needles):
            continue
        run(["taskkill", "/PID", str(proc["ProcessId"]), "/T", "/F"], check=False)
        stopped.append(f"{proc['Name']} {proc['ProcessId']}")
    return stopped


def start_legacy(spec: dict) -> str:
    if spec.get("task") and task_exists(spec["task"]):
        run(["schtasks", "/Change", "/TN", spec["task"], "/ENABLE"], check=False)
        run(["schtasks", "/Run", "/TN", spec["task"]], check=False)
        return f"任务 {spec['task']}"
    if spec.get("command"):
        subprocess.Popen(spec["command"], cwd=spec.get("cwd"), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True,
                         creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB)
        return "命令 " + " ".join(spec["command"])[:200]
    raise RuntimeError("legacy 说明里既没有任务也没有命令，无法恢复")


def rollback(expect: list[str], reason: str) -> bool:
    spec = read_json(LEGACY_STATE)
    log(f"回滚：{reason}")
    run(["schtasks", "/End", "/TN", TASK_NAME], check=False)
    wait_until(lambda: not any(own_core(proc) for proc in cores()), 20)
    for proc in cores():
        if own_core(proc):
            run(["taskkill", "/PID", str(proc["ProcessId"]), "/T", "/F"], check=False)
    log("已恢复旧运行方式：" + start_legacy(spec))
    ok = wait_until(lambda: len(cores()) == 1 and (peer_names() is not None) and all(n in peer_names() for n in expect), 90)
    write_json(SWITCH_STATE, {"status": "rolled_back" if ok else "rollback_unverified", "reason": reason,
                              "at": time.strftime("%Y-%m-%d %H:%M:%S")})
    return ok


def switch(expect: list[str]) -> int:
    spec = read_json(LEGACY_STATE)
    write_json(SWITCH_STATE, {"status": "switching", "at": time.strftime("%Y-%m-%d %H:%M:%S")})
    try:
        check_config()
        if not task_exists(TASK_NAME):
            raise RuntimeError(f"计划任务 {TASK_NAME} 未注册")
        log("停止旧运行方式：" + (", ".join(stop_legacy(spec)) or "没有运行中的"))
        if not wait_until(lambda: not cores(), 25):
            raise RuntimeError("旧 easytier-core 没有退出")
        run(["schtasks", "/Run", "/TN", TASK_NAME])
        if not wait_until(lambda: healthy(expect), 120, 3):
            raise RuntimeError(f"新任务 120 秒内没有达到：单实例、自带二进制、对端 {expect}")
    except Exception as exc:
        log(f"切换失败：{exc}")
        rollback(expect, str(exc))
        return 1
    if spec.get("task") and task_exists(spec["task"]):
        run(["schtasks", "/Change", "/TN", spec["task"], "/DISABLE"], check=False)
        log(f"已禁用旧计划任务 {spec['task']}（保留作回滚）")
    write_json(SWITCH_STATE, {"status": "ok", "at": time.strftime("%Y-%m-%d %H:%M:%S"), "expect": expect})
    log("切换完成")
    return 0


def recover(expect: list[str]) -> int:
    """The independent safety net: roll back if the switch never reported ok, or the node is not healthy now."""
    status = read_json(SWITCH_STATE).get("status")
    if status == "switching":
        return 0 if rollback(expect, "切换进程没有报告结果") else 1
    if status == "ok" and not healthy(expect):
        return 0 if rollback(expect, "切换已报告成功，但节点现在不健康") else 1
    log(f"恢复任务无需动作（切换状态：{status}）")
    delete_task(RECOVER_TASK, end=False)
    return 0


# ---------- commands ----------

def without_option(argv: list[str], option: str) -> list[str]:
    result, skip = [], False
    for part in argv:
        if skip:
            skip = False
        elif part == option:
            skip = True
        else:
            result.append(part)
    return result


def relaunch_elevated(argv: list[str]) -> int:
    USER_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = USER_LOG_DIR / f"setup-{time.strftime('%Y%m%d-%H%M%S')}.log"
    launcher, *launcher_args = component_launch.command(__file__, *argv, "--log", str(log_path))
    parameters = subprocess.list2cmdline(launcher_args)

    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong), ("fMask", ctypes.c_ulong), ("hwnd", ctypes.c_void_p),
                    ("lpVerb", ctypes.c_wchar_p), ("lpFile", ctypes.c_wchar_p), ("lpParameters", ctypes.c_wchar_p),
                    ("lpDirectory", ctypes.c_wchar_p), ("nShow", ctypes.c_int), ("hInstApp", ctypes.c_void_p),
                    ("lpIDList", ctypes.c_void_p), ("lpClass", ctypes.c_wchar_p), ("hkeyClass", ctypes.c_void_p),
                    ("dwHotKey", ctypes.c_ulong), ("hIcon", ctypes.c_void_p), ("hProcess", ctypes.c_void_p)]

    info = ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x40  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb, info.lpFile, info.lpParameters, info.nShow = "runas", launcher, parameters, 0
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        print("没有获得管理员权限（UAC 被取消或不可用）")
        return 1
    ctypes.windll.kernel32.WaitForSingleObject(info.hProcess, 0xFFFFFFFF)
    code = ctypes.c_ulong()
    ctypes.windll.kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(info.hProcess)
    print(log_path.read_text(encoding="utf-8") if log_path.is_file() else "(没有日志)")
    return int(code.value)


def command_check() -> int:
    rows = [(f"{name} 在 {BIN}", (BIN / name).is_file()) for name in REQUIRED]
    core = BIN / "easytier-core.exe"
    rows.append(("easytier-core.exe 为固定的 v2.6.4 官方版本", core.is_file() and sha256(core) == PINNED[core.name]))
    installed = BIN / "easytier-supervisor.ps1"
    rows.append(("监督脚本与源码一致", installed.is_file() and sha256(installed) == sha256(SUPERVISOR_SOURCE)))
    rows.append((f"计划任务 {TASK_NAME} 已注册", task_exists(TASK_NAME)))
    found = cores()
    rows.append(("只有一个 easytier-core", len(found) == 1))
    # An unelevated reader cannot see a SYSTEM process's path; the task running is then the evidence.
    from_bin = own_core(found[0]) if len(found) == 1 and is_admin() else len(found) == 1 and task_running(TASK_NAME)
    rows.append((f"该 core 来自 {BIN}（未提权时以任务在运行为准）", from_bin))
    for text, ok in rows:
        print(("通过  " if ok else "未通过  ") + text)
    return 0 if all(ok for _, ok in rows) else 1


# ---------- migration from the checkout-era home ----------

def path_state(path: Path) -> str:
    """'present', 'missing', or 'locked' (exists, but this unelevated user may not read it)."""
    try:
        os.stat(path)
        return "present"
    except PermissionError:
        return "locked"
    except OSError:
        return "missing"


def migration_sources(old: Path, hook: str | None) -> tuple[Path, Path, Path | None]:
    """bin, node.toml and the optional pre-start hook of the node at `old`."""
    if old.resolve() == HOME.resolve():
        raise RuntimeError(f"{old} 已经是 EasyTier 的安装位置，无需迁移")
    binaries, config = old / "bin", old / "private" / "node.toml"
    if not (binaries / "easytier-core.exe").is_file():
        raise FileNotFoundError(f"{binaries} 里没有 easytier-core.exe，{old} 不是一个已安装的节点")
    if not config.is_file():
        raise FileNotFoundError(f"缺少 {config}")
    default_hook = old / "private" / "pre-start.ps1"
    return binaries, config, Path(hook) if hook else (default_hook if default_hook.is_file() else None)


def carry_state(old: Path) -> list[str]:
    """Copy the old home's state/ and backup/ (rollback spec, switch result, legacy backups) into the new home."""
    done = []
    for name in ("state", "backup"):
        source = old / name
        if not source.is_dir():
            continue
        for path in sorted(source.rglob("*")):
            target = HOME / name / path.relative_to(source)
            if path.is_file() and not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
                done.append(f"已带上 {name}/{path.relative_to(source).as_posix()}")
    return done


def cores_from(bin_dir: Path) -> list[dict]:
    exe = str(bin_dir / "easytier-core.exe").lower()
    return [proc for proc in cores() if (proc.get("ExecutablePath") or "").lower() == exe]


def stop_node_at(bin_dir: Path) -> list[str]:
    """End the node's task (either name) and any core still running from bin_dir."""
    stopped = []
    for name in (TASK_NAME, *LEGACY_TASKS):
        if task_exists(name):
            run(["schtasks", "/End", "/TN", name], check=False)
            stopped.append(f"任务 {name}")
    wait_until(lambda: not cores_from(bin_dir), 20)
    for proc in cores_from(bin_dir):
        run(["taskkill", "/PID", str(proc["ProcessId"]), "/T", "/F"], check=False)
        stopped.append(f"easytier-core {proc['ProcessId']}")
    return stopped


def remove_tree(path: Path) -> None:
    def retry(function, target, _info):
        os.chmod(target, 0o700)
        function(target)
    if is_reparse_point(path):
        os.rmdir(path)      # a link: remove the link only, never what it points at
    elif path.exists():
        shutil.rmtree(path, onerror=retry)


def task_supervisor(name: str) -> str:
    """The arguments of a registered task's action, lowercased, or '' when they cannot be read."""
    output = powershell(f"(Get-ScheduledTask -TaskName '{name}' -ErrorAction SilentlyContinue).Actions.Arguments",
                        check=False).stdout
    return output.strip().lower()


def migration_plan(old: Path, keep_old: bool) -> list[str]:
    lines = [f"迁移 EasyTier：{old} → {HOME}"]
    for relative in ("bin/easytier-core.exe", "bin/easytier-cli.exe", "private/node.toml", "private/pre-start.ps1",
                     "state", "backup"):
        note = {"present": "有", "missing": "无", "locked": "有（受 ACL 保护，提权后读取）"}[path_state(old / relative)]
        lines.append(f"  {relative}: {note}")
    if easytier_paths.installed_at(HOME):
        lines.append(f"  注意：{HOME} 已有节点；会检查计划任务是否已指向这里")
    tasks = [name for name in (TASK_NAME, *LEGACY_TASKS) if task_exists(name)]
    lines.append("  将停止的计划任务：" + ("、".join(tasks) if tasks else "（未查到；可能需要提权才看得见）"))
    lines.append(f"  将重新注册 {TASK_NAME} 指向 {BIN / 'easytier-supervisor.ps1'}，并删除改名前的任务 "
                 + "、".join(LEGACY_TASKS))
    lines.append("  成功后：" + ("保留旧目录" if keep_old else f"删除 {old}") + "；失败则任务改回旧目录、删除新副本")
    lines.append("  需要管理员权限（会弹出 UAC）")
    return lines


def command_migrate(args) -> int:
    old = Path(args.migrate_from).expanduser().resolve()
    if args.dry_run:
        for line in migration_plan(old, args.keep_old):
            print(line)
        return 0
    user_sid = args.user_sid or current_user_sid()
    if easytier_paths.installed_at(HOME):
        if str(BIN / "easytier-supervisor.ps1").lower() not in task_supervisor(TASK_NAME):
            raise RuntimeError(f"{HOME} 已有节点，但计划任务 {TASK_NAME} 没有指向它；请先确认哪一份是在用的节点")
        log(f"节点已在 {HOME}，计划任务也已指向这里")
        if old.exists() and not args.keep_old:
            remove_tree(old)
            log(f"已删除旧目录 {old}")
        return 0
    binaries, config, hook = migration_sources(old, args.hook)
    log(f"迁移 EasyTier：{old} → {HOME}")
    for line in stage_files(binaries, config, hook):
        log(line)
    for line in carry_state(old):
        log(line)
    check_config()
    log("配置检查通过")
    log("停止旧节点：" + (", ".join(stop_node_at(binaries)) or "没有运行中的"))
    try:
        register_main_task(user_sid)
        for name in LEGACY_TASKS:
            delete_task(name)
        run(["schtasks", "/Run", "/TN", TASK_NAME])
        if not wait_until(lambda: healthy(args.expect_peer), 120, 3):
            raise RuntimeError(f"新位置的节点 120 秒内没有达到：单实例、来自 {BIN}、对端 {args.expect_peer}")
    except Exception as exc:
        log(f"迁移失败：{exc}；改回旧目录")
        run(["schtasks", "/End", "/TN", TASK_NAME], check=False)
        wait_until(lambda: not cores_from(BIN), 20)
        for proc in cores_from(BIN):
            run(["taskkill", "/PID", str(proc["ProcessId"]), "/T", "/F"], check=False)
        register_main_task(user_sid, binaries)
        run(["schtasks", "/Run", "/TN", TASK_NAME], check=False)
        remove_tree(HOME)
        log(f"已让 {TASK_NAME} 重新运行 {binaries / 'easytier-supervisor.ps1'}，并删除了 {HOME} 的副本")
        return 1
    log(f"已注册计划任务 {TASK_NAME} 指向 {BIN}，节点运行正常")
    if not args.keep_old:
        remove_tree(old)
        log(f"已删除旧目录 {old}")
    return 0


def command_install(args) -> int:
    if args.migrate_from:
        return command_migrate(args)
    log(f"安装 EasyTier 到 {HOME}")
    for line in stage_files(Path(args.binaries), Path(args.config), Path(args.hook) if args.hook else None):
        log(line)
    check_config()
    log("配置检查通过")
    if args.legacy:
        write_json(LEGACY_STATE, json.loads(Path(args.legacy).read_text(encoding="utf-8-sig")))
        log("已记录旧运行方式（供回滚）")
    register_main_task(args.user_sid or current_user_sid())
    log(f"已注册计划任务 {TASK_NAME}（SYSTEM，开机启动，当前用户只可读/运行/停止）")
    replaced = [name for name in LEGACY_TASKS if task_exists(name)]
    for name in replaced:
        delete_task(name)      # ends the old task's node first, so the new task never runs a second core beside it
    if replaced:
        log("已删除改名前的计划任务：" + "、".join(replaced))
        if not args.switch:
            run(["schtasks", "/Run", "/TN", TASK_NAME], check=False)
    if args.switch:
        return switch(args.expect_peer)
    return 0


def command_uninstall() -> int:
    for name in (TASK_NAME, RECOVER_TASK, SWITCH_TASK, *LEGACY_TASKS):
        delete_task(name)
    log(f"已删除计划任务（{HOME} 里的文件保留）")
    return 0


def main(argv: list[str] | None = None) -> int:
    global _log_file
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("check", "install", "switch", "recover", "rollback", "uninstall",
                                            "arm-recovery", "detach-switch", "cleanup"))
    parser.add_argument("--binaries")
    parser.add_argument("--config")
    parser.add_argument("--hook")
    parser.add_argument("--legacy")
    parser.add_argument("--switch", action="store_true")
    parser.add_argument("--expect-peer", action="append", default=[])
    parser.add_argument("--user-sid")
    parser.add_argument("--minutes", type=int, default=10)
    parser.add_argument("--log")
    parser.add_argument("--migrate-from", help="install: move the node from this older home (e.g. <checkout>\\easytier)")
    parser.add_argument("--keep-old", action="store_true", help="install --migrate-from: keep the old home")
    parser.add_argument("--dry-run", action="store_true", help="install --migrate-from: print the plan only")
    args = parser.parse_args(argv)
    raw = list(sys.argv[1:] if argv is None else argv)
    if args.command == "check":
        return command_check()
    if args.command == "install":
        use_home(easytier_paths.product_home())
        if args.migrate_from and args.switch:
            parser.error("--migrate-from moves an installed node; it does not combine with --switch")
        if not args.migrate_from and not (args.binaries and args.config):
            parser.error("install needs --binaries and --config, or --migrate-from")
        if args.migrate_from and args.dry_run:
            return command_migrate(args)
    if args.log:
        _log_file = Path(args.log)
    if not is_admin():
        if args.command == "install" and not args.user_sid:
            raw += ["--user-sid", current_user_sid()]
        return relaunch_elevated(without_option(raw, "--log"))
    try:
        if args.command == "install":
            return command_install(args)
        if args.command == "switch":
            return switch(args.expect_peer)
        if args.command == "recover":
            return recover(args.expect_peer)
        if args.command == "rollback":
            return 0 if rollback(args.expect_peer, "手动回滚") else 1
        if args.command == "uninstall":
            return command_uninstall()
        if args.command == "cleanup":
            for name in (RECOVER_TASK, SWITCH_TASK):
                delete_task(name)
            log("已删除迁移用的辅助计划任务")
            return 0
        if args.command == "arm-recovery":
            register_helper(RECOVER_TASK, ["recover", *[x for p in args.expect_peer for x in ("--expect-peer", p)],
                                          "--log", str(STATE / "recover.log")], args.minutes)
            log(f"已登记 {args.minutes} 分钟后的独立恢复任务 {RECOVER_TASK}")
            return 0
        if args.command == "detach-switch":
            register_helper(SWITCH_TASK, ["switch", *[x for p in args.expect_peer for x in ("--expect-peer", p)],
                                         "--log", str(STATE / "switch.log")], None)
            run(["schtasks", "/Run", "/TN", SWITCH_TASK])
            log(f"已让 SYSTEM 任务 {SWITCH_TASK} 独立执行切换（SSH 断开也会继续）")
            return 0
    except Exception as exc:
        log(f"失败：{exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
