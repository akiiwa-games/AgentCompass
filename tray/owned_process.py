"""Run one external command and own every process it starts (Windows kill-on-close Job; process group elsewhere).

Output goes to temporary files, not pipes, so a descendant that keeps a pipe open after its parent exits can never
block the caller. The timeout covers the whole tree: an updater that hands work to a helper process keeps the rest of
its time budget after the main process exits, and anything still running at the deadline is terminated and confirmed
gone.
"""
from __future__ import annotations

from i18n import translate as _tr

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import NamedTuple

OUTPUT_LIMIT = 24000
# The helper waits for one byte on stdin, so the command cannot start before the Job owns the helper.
HELPER = ("import subprocess,sys\n"
          "if sys.stdin.buffer.read(1) != b'1': raise SystemExit(125)\n"
          "raise SystemExit(subprocess.call(sys.argv[1:], stdin=subprocess.DEVNULL))")


class Result(NamedTuple):
    returncode: int
    stdout: str
    stderr: str


class OwnedProcessError(RuntimeError):
    """Ownership could not be established or termination could not be confirmed; output may be partial."""

    def __init__(self, message: str, stdout: str = "", stderr: str = ""):
        super().__init__(message)
        self.stdout, self.stderr = stdout, stderr


class OwnedTimeout(OwnedProcessError):
    """The command exceeded its timeout; the whole tree has been terminated."""


def python() -> str:
    """python.exe, not pythonw.exe: the helper needs real standard handles."""
    return str(Path(sys.executable).with_name("python.exe")) if os.name == "nt" else sys.executable


def _text(stream) -> str:
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(max(0, size - OUTPUT_LIMIT))
    data = stream.read()
    try:
        return data.decode("utf-8").strip()
    except UnicodeDecodeError:    # a tool writing the console code page
        return data.decode("mbcs" if os.name == "nt" else "utf-8", errors="replace").strip()


def run(argv: list[str], *, timeout: float, env: dict[str, str] | None = None, cwd: str | None = None) -> Result:
    if os.name == "nt":
        return _run_job(argv, timeout, env, cwd)
    return _run_group(argv, timeout, env, cwd)


def _run_job(argv, timeout, env, cwd) -> Result:
    try:
        import win32api
        import win32event
        import win32job
        job = win32job.CreateJobObject(None, "")
        limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
    except Exception as exc:
        raise OwnedProcessError(_tr("Windows 进程管理不可用；请修复 Route Manager 的 pywin32 运行环境。")) from exc

    def active() -> int:
        return win32job.QueryInformationJobObject(job, win32job.JobObjectBasicAccountingInformation)["ActiveProcesses"]

    def terminate() -> None:
        handles = []
        try:
            # Freeze growth first: a nonempty Job limited to one process cannot gain members while we enumerate.
            limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
            limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            limits["BasicLimitInformation"]["ActiveProcessLimit"] = 1
            win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
            for pid in win32job.QueryInformationJobObject(job, win32job.JobObjectBasicProcessIdList):
                try:
                    handles.append(win32api.OpenProcess(0x00100000, False, pid))    # SYNCHRONIZE
                except Exception as exc:
                    if getattr(exc, "winerror", None) != 87:    # already exited
                        raise
            win32job.TerminateJobObject(job, 1)
            deadline = time.monotonic() + 10
            while active():
                if time.monotonic() >= deadline:
                    raise TimeoutError("job still active")
                time.sleep(0.01)
            for handle in handles:
                remaining = max(0, int((deadline - time.monotonic()) * 1000))
                if win32event.WaitForSingleObject(handle, remaining) != win32event.WAIT_OBJECT_0:
                    raise TimeoutError("process still alive")
        finally:
            for handle in handles:
                handle.Close()

    started = time.monotonic()
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen([python(), "-S", "-c", HELPER, *argv], stdin=subprocess.PIPE, stdout=out,
                                    stderr=err, env=env, cwd=cwd, creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                win32job.AssignProcessToJobObject(job, int(proc._handle))
                proc.stdin.write(b"1")
                proc.stdin.flush()
                proc.stdin.close()
            except Exception as exc:
                try:
                    terminate()
                except Exception:
                    pass
                if proc.poll() is None:
                    proc.kill()
                proc.wait(10)
                raise OwnedProcessError(_tr("无法在启动前建立 Windows 进程归属，未执行命令")) from exc
            deadline = started + timeout
            timed_out = False
            try:
                proc.wait(max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
            while not timed_out and active():
                if time.monotonic() >= deadline:
                    timed_out = True
                time.sleep(0.05)
            if timed_out:
                try:
                    terminate()
                except Exception as exc:
                    raise OwnedProcessError(_tr("命令的子进程未能确认已停止，请在任务管理器中检查"), _text(out), _text(err)) from exc
                proc.wait(10)    # reap the helper so no Popen is left running
                raise OwnedTimeout(_tr("命令超时，已停止本次启动的全部子进程"), _text(out), _text(err))
            return Result(proc.returncode, _text(out), _text(err))
        finally:
            job.Close()


def _run_group(argv, timeout, env, cwd) -> Result:
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env, cwd=cwd,
                                start_new_session=True)

        def alive() -> bool:
            try:
                os.killpg(proc.pid, 0)
                return True
            except OSError:
                return False

        def terminate() -> None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait(10)
            deadline = time.monotonic() + 10
            while alive():
                if time.monotonic() >= deadline:
                    raise OwnedProcessError(_tr("命令的子进程未能确认已停止，请在任务管理器中检查"), _text(out), _text(err))
                time.sleep(0.01)

        deadline = time.monotonic() + timeout
        try:
            proc.wait(timeout)
            while alive():
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(argv, timeout)
                time.sleep(0.05)
        except subprocess.TimeoutExpired:
            terminate()
            raise OwnedTimeout(_tr("命令超时，已停止本次启动的全部子进程"), _text(out), _text(err)) from None
        return Result(proc.returncode, _text(out), _text(err))
