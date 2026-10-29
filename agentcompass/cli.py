"""Console script for the AgentCompass router.

``setup`` and ``uninstall`` call ``setup/onboard.py``. ``plan`` and ``roll`` run
``route_judge.py`` with ``python -I`` so the skill scripts stay real files on disk.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys

from agentcompass import __version__
from agentcompass import locations

HELP = """\
usage: agentcompass <command> [arguments]

commands:
  setup       first-run onboarding (setup/onboard.py setup_main)
  uninstall   undo what setup changed (setup/onboard.py uninstall_main)
  doctor      read-only report of Python, CLIs, accounts, cache, plugins, ports, and hooks
  plan        pass the remaining arguments to route_judge.py plan
  roll        pass the remaining arguments to route_judge.py roll

The router is not frozen into an executable. Skills stay real directories.
"""


def call_onboard(command: str, argv: list[str]) -> int:
    path = locations.setup_script("onboard.py")
    if path is None:
        sys.stderr.write(
            "agentcompass is wired to setup/onboard.py (main, setup_main, uninstall_main), "
            "but that file is not in this install.\n"
        )
        return 2
    spec = importlib.util.spec_from_file_location("agentcompass_onboard", path)
    if spec is None or spec.loader is None:
        sys.stderr.write(f"could not load {path}\n")
        return 2
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if command == "setup" and hasattr(module, "setup_main"):
        return int(module.setup_main(argv) or 0)
    if command == "uninstall" and hasattr(module, "uninstall_main"):
        return int(module.uninstall_main(argv) or 0)
    if hasattr(module, "main"):
        return int(module.main([command, *argv]) or 0)
    sys.stderr.write(f"{path} has no main(argv)\n")
    return 2


def route_judge_command(stage: str, args: list[str]) -> list[str]:
    script = locations.skill_root() / "route-agent-work" / "scripts" / "route_judge.py"
    if not script.is_file():
        raise FileNotFoundError(script)
    return [sys.executable, "-I", "-X", "utf8", str(script), stage, *args]


def passthrough(stage: str, args: list[str]) -> int:
    try:
        command = route_judge_command(stage, args)
    except FileNotFoundError as error:
        sys.stderr.write(f"route_judge.py was not found ({error})\n")
        return 2
    return subprocess.call(command)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        sys.stdout.write(HELP)
        return 0
    if argv[0] in ("-V", "--version"):
        print(__version__)
        return 0
    command, rest = argv[0], argv[1:]
    try:
        if command == "doctor":
            from agentcompass.doctor import main as doctor_main
            return doctor_main(rest)
        if command in ("setup", "uninstall"):
            return call_onboard(command, rest)
        if command in ("plan", "roll"):
            return passthrough(command, rest)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 1
    sys.stderr.write(f"unknown command {command!r}\n{HELP}")
    return 2
