"""Job-owned command helper; stdin release happens only after assignment to the Job.

In the installed product this helper is the windowed AgentCompass.exe, which has no console, so a console command it
starts without CREATE_NO_WINDOW opens a console window of its own (Windows Terminal, where that is the default).
"""
import os
import subprocess
import sys

NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def main() -> int:
    if sys.stdin.buffer.read(1) != b"1":
        return 125
    return subprocess.call(sys.argv[1:], stdin=subprocess.DEVNULL, **NO_WINDOW)


if __name__ == "__main__":
    raise SystemExit(main())
