"""Console script for the AgentCompass router.

``setup`` and ``uninstall`` call ``setup/onboard.py``. The routing stages (roll, plan, providers, record, ...) run
``route_judge.py`` with ``python -I`` so the skill scripts stay real files on disk; ``usage`` runs the collector.

The installer's frozen ``agentcompass.exe`` is its own Python for those scripts (``agentcompass.frozen_python``), and
``gate-hook`` is the router gate hook command hosts run (``setup/router_gate_hook.py`` in this process).
"""

from __future__ import annotations

import importlib.util
import re
import runpy
import subprocess
import sys

from agentcompass import __version__
from agentcompass import locations

STAGES = ("roll", "plan", "providers", "score", "scorer", "delegate", "select", "record", "override", "dispatch", "launch")

RUNNERS = {"dot": ("dot-subagent", "run_dot_task.py"), "grok-bot": ("agent-compass", "run_grok_bot.py"),
           "herdr": ("agent-compass", "host_herdr.py"),
           "chatgpt-web": ("chatgpt-pro-consult", "consult_bridge.py")}

HELP = """usage: agentcompass <command> [arguments]

delegate work:
  roll        hand a task to the router: agentcompass roll --task "<what the worker should do>"   (--task - reads stdin, --brief <file> reads a file)
  plan        rank the bundles for a task file without launching: plan --input <task.json> [--dry-run]  (offline demo: plan --demo --dry-run)
  providers   refresh and print which accounts and CLIs this machine can launch
  usage       collect provider usage once into the router's cache (no Usage Float needed); `usage --show` prints what routing reads
  record      record a route the user forced (user-named model or effort)
  override    add, list or clear a temporary routing boost
  hold        list accounts/models held after a provider failed a worker; `hold --clear ACCOUNT[/MODEL]` lifts one

install and health:
  setup       first-run onboarding (--dry-run previews)
  uninstall   undo what setup changed
  doctor      read-only report of Python, CLIs, accounts, cache, reference data, plugins, ports and hooks
  data        which public reference data routing reads (shipped or fetched); `data --refresh` checks for newer data now

other stages: score, scorer, delegate, select, dispatch, launch
follow-up of a roll result (its next_argv already spells these): dot status, grok-bot status, chatgpt-web, herdr status
Options of one command: agentcompass <command> -h   (every option: --help-all)
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


def gate_hook(argv: list[str]) -> int:
    """The router gate hook, as hosts call it from the installer (`agentcompass.exe gate-hook`): JSON on stdin, verdict on stdout."""
    path = locations.setup_script("router_gate_hook.py")
    if path is None:
        sys.stderr.write("router_gate_hook.py is not part of this install\n")
        return 0  # fail open, as the hook does when its own checks cannot run
    sys.argv = [str(path), *argv]
    folder = str(path.parent)
    if folder not in sys.path:
        sys.path.insert(0, folder)
    try:
        runpy.run_path(str(path), run_name="__main__")
    except SystemExit as exc:
        code = exc.code
        return 0 if code is None else code if isinstance(code, int) else 1
    return 0


def route_judge_command(stage: str, args: list[str]) -> list[str]:
    script = locations.skill_root() / "agent-compass" / "scripts" / "route_judge.py"
    if not script.is_file():
        raise FileNotFoundError(script)
    return [sys.executable, "-I", "-X", "utf8", str(script), stage, *args]


USAGE_HELP = """usage: agentcompass usage [--force] [--show [--accounts KEY ...] [--usage-cache FILE]]

Without --show: collect provider usage once into the router's cache (~/.agent-compass/usage-cache.json, or under
ROUTER_STATE_DIR) for the accounts this machine's host policy allows, and print a receipt. Needs no Usage Float and no tray.

--force             skip poll/retry throttling (Claude 429 rate-limit backoff still applies)
--show              print what routing reads from the cache (read only, nothing is collected)
--accounts KEY ...  with --show: only these accounts
--usage-cache FILE  with --show: read only this file (also: ROUTER_USAGE_CACHE)
"""


def usage_command(args: list[str]) -> int:
    """Run the headless usage collector once. It is the core's own collector; the Usage Float plugin is not involved."""
    script = locations.skill_root() / "agent-compass" / "scripts" / "usage_collector.py"
    if not script.is_file():
        sys.stderr.write(f"usage_collector.py was not found ({script})\n")
        return 2
    if _has_option(args, "-h") or _has_option(args, "--help"):
        sys.stdout.write(USAGE_HELP)
        return 0
    rest = [arg for arg in args if arg != "--once"]
    return subprocess.call([sys.executable, "-I", "-X", "utf8", str(script), "--once", *rest])


def data_command(args: list[str]) -> int:
    """Show or refresh the public reference data channel (skills/agent-compass/scripts/public_data.py)."""
    script = locations.skill_root() / "agent-compass" / "scripts" / "public_data.py"
    if not script.is_file():
        sys.stderr.write(f"public_data.py was not found ({script})\n")
        return 2
    return subprocess.call([sys.executable, "-I", "-X", "utf8", str(script), *args])


def hold_command(args: list[str]) -> int:
    """List or clear provider-retry holds (skills/agent-compass/scripts/provider_retry.py)."""
    script = locations.skill_root() / "agent-compass" / "scripts" / "provider_retry.py"
    if not script.is_file():
        sys.stderr.write(f"provider_retry.py was not found ({script})\n")
        return 2
    return subprocess.call([sys.executable, "-I", "-X", "utf8", str(script), *args])


def runner_command(name: str, args: list[str]) -> int:
    """Follow-up of an external runner (Dot, Grok Bot, ChatGPT web) that roll handed back; the caller never sees the script."""
    folder, script = RUNNERS[name]
    path = locations.skill_root() / folder / "scripts" / script
    if not path.is_file():
        sys.stderr.write(f"{name} is not part of this install ({path})\n")
        return 2
    return subprocess.call([sys.executable, "-I", "-X", "utf8", str(path), *args])


def passthrough(stage: str, args: list[str]) -> int:
    try:
        command = route_judge_command(stage, args)
    except FileNotFoundError as error:
        sys.stderr.write(f"route_judge.py was not found ({error})\n")
        return 2
    return subprocess.call(command)


PLAN_NEEDS_TASK = """\
plan needs a task. Pass one of:
  agentcompass plan --input <task.json> [--dry-run]
  agentcompass plan --demo --dry-run
To hand a task to a worker, use roll instead: agentcompass roll --task "<text>"
"""


def _has_option(args: list[str], name: str) -> bool:
    return any(arg == name or arg.startswith(name + "=") for arg in args)


def packaged_wording(message: str) -> str:
    """The skill script spells the checkout's own command; the console script is simply `agentcompass`."""
    return re.sub(r"`python -X utf8 [^`]*? setup`", "`agentcompass setup`", message)


def readiness_notice() -> str | None:
    """The existing no_ready_account text, when this machine has nothing launchable.

    This reads discovery and the account files. It does not refresh provider-inventory.json and it
    does not enroll accounts into host policy; a missing task is not a plan.
    """
    try:
        scripts = locations.skill_root() / "agent-compass" / "scripts"
        if str(scripts) not in sys.path:
            sys.path.append(str(scripts))
        import json

        import accounts_override
        import allocator
        import route_judge

        accounts = json.loads(allocator.fleet_document_path().read_text(encoding="utf-8-sig"))
        user_file = allocator.accounts_override_path()
        user_doc = None
        if user_file.is_file():
            loaded = json.loads(user_file.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                user_doc = accounts_override.migrate(loaded, accounts)
        allocator.provider_inventory.apply(accounts, allocator.provider_inventory.discover())
        if user_doc is not None:
            accounts_override.apply(accounts, user_doc)
        gap = route_judge.readiness_gap(accounts, route_judge.usage_snapshot())
    except Exception:
        return None
    if isinstance(gap, dict) and gap.get("code") == "no_ready_account":
        message = gap.get("message")
        if not isinstance(message, str) or not message.strip():
            return None
        return packaged_wording(message)
    return None


def plan_passthrough(args: list[str]) -> int:
    """Explain a missing task before route_judge turns it into a bare ValueError."""
    if any(_has_option(args, name) for name in ("-h", "--help", "--help-all")):
        return passthrough("plan", args)
    if not _has_option(args, "--input") and not _has_option(args, "--demo"):
        sys.stdout.write(PLAN_NEEDS_TASK)
        notice = readiness_notice()
        if notice:
            sys.stdout.write("\n" + notice + "\n")
        return 1
    return passthrough("plan", args)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if getattr(sys, "frozen", False):
        from agentcompass import frozen_python
        if frozen_python.looks_like_python(argv):
            frozen_python.environment_ready()
            return frozen_python.run(argv)
    if not argv or argv[0] in ("-h", "--help"):
        sys.stdout.write(HELP)
        return 0
    if argv[0] in ("-V", "--version"):
        print(__version__)
        return 0
    command, rest = argv[0], argv[1:]
    try:
        if command == "gate-hook":
            return gate_hook(rest)
        if command == "doctor":
            from agentcompass.doctor import main as doctor_main
            return doctor_main(rest)
        if command in ("setup", "uninstall"):
            return call_onboard(command, rest)
        if command == "plan":
            return plan_passthrough(rest)
        if command == "data":
            return data_command(rest)
        if command == "hold":
            return hold_command(rest)
        if command == "usage" and "--show" not in rest:
            return usage_command(rest)
        if command in RUNNERS:
            return runner_command(command, rest)
        if command in STAGES or command == "usage":
            return passthrough(command, rest)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 1
    sys.stderr.write(f"unknown command {command!r}\n{HELP}")
    return 2
