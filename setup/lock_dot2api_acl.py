"""Limit the Dot credential directory to the current user."""

import subprocess
import sys
from pathlib import Path

target = Path.home() / ".dot2api"
who = subprocess.run(["whoami"], check=True, text=True, capture_output=True).stdout.strip()
command = [
    "icacls",
    str(target),
    "/inheritance:r",
    "/grant:r",
    f"{who}:(OI)(CI)F",
]
result = subprocess.run(command, text=True, capture_output=True)
sys.stdout.write(result.stdout)
sys.stderr.write(result.stderr)
raise SystemExit(result.returncode)
