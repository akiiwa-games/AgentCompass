"""A stand-in for a provider CLI (codex / claude), built into an executable by zero_dispatch_check.py.

It is a real process that the router starts exactly as it starts the real CLI: same executable name, same arguments, the
brief on stdin. What it does is decided by the executable's name (which CLI it plays) and by a line in the brief,
``FIXTURE-SCENARIO: <name>``. It never talks to a network. State lives in ``<exe dir>/../state``:

    workers/<pid>.json     what it was started with (argv, cwd, the router's environment variables - names, never secrets)
    inbox/<parent>/<n>.json the final report it sends to its parent (the parent's mailbox is a directory here)
    agents/<id>.json       when started by the Paseo host fixture (FIXTURE_AGENT_ID): status of that agent

Started by the Herdr adapter (the last argument tells it to read an assignment file), it reads that file and reports to
the parent with the real ``hc send``.

Scenarios: ok (default) | error | quota | hang | crash | slow | blocked (Herdr: announces "needs input" and waits)
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

EXE = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve()
STATE = EXE.parent.parent / "state"
ROUTER_VARS = ("ROUTER_WORKER", "ROUTER_PARENT_AGENT_ID", "ROUTER_PARENT_HOST", "ROUTER_PARENT_ADDRESS")


def write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    if "--json-schema" in sys.argv or "--output-schema" in sys.argv:
        if os.environ.get("FIXTURE_SCORER") == "1":
            profile = {
                "work_kind": "general_implementation", "reasoning_depth": 2, "code_complexity": 1,
                "axis_demand": {"divergence": 1, "coding": 2, "aesthetics": 0, "tool_operation": 0},
                "needs_visual": False, "needs_gui": False, "local_only": False, "wants_fast": False, "read_only_review": False, "expected_hours": 0.5,
                "hours_confidence": "medium", "urgency": "normal", "interaction_style": "engineering",
                "multi_task": False, "task_count": "1",
                "reason": "fixture scorer classification",
            }
            if "--output-last-message" in sys.argv:
                idx = sys.argv.index("--output-last-message")
                if idx + 1 < len(sys.argv):
                    Path(sys.argv[idx + 1]).write_text(json.dumps(profile), encoding="utf-8")
            print(json.dumps({"structured_output": profile}))
            return 0
        sys.stderr.write("fixture CLI invoked as read-only scorer; use ScorerServer in zero_dispatch_check\n")
        return 1
    name = EXE.stem.lower()
    prompt = ""
    if not sys.stdin.isatty():
        prompt = sys.stdin.buffer.read().decode("utf-8", "replace")
    if not prompt and sys.argv[-1] not in ("-", "--print"):
        prompt = sys.argv[-1]
    bootstrap = re.search(r'assignment file ("(?:[^"\\]|\\.)*")', sys.argv[-1]) if sys.argv[1:] else None
    if bootstrap:
        prompt = Path(json.loads(bootstrap.group(1))).read_text(encoding="utf-8", errors="replace")
    scenario = (re.search(r"FIXTURE-SCENARIO:\s*(\w+)", prompt) or [None, "ok"])[1]
    agent_id = os.environ.get("FIXTURE_AGENT_ID")
    record = {
        "cli": name, "pid": os.getpid(), "argv": sys.argv[1:], "cwd": os.getcwd(), "scenario": scenario,
        "started": time.time(), "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(), "prompt_chars": len(prompt),
        "prompt_has_contract": "Worker final report" in prompt or "worker-communication" in prompt.lower(),
        "prompt_head": prompt[:400],
        "router_env": {key: os.environ.get(key) for key in ROUTER_VARS},
        # names only: a real credential must never end up in a fixture log
        "host_session_vars_present": sorted(k for k in os.environ if k.startswith(("CLAUDE", "CODEX_", "PASEO_AGENT"))),
    }
    # Windows reuses PIDs within one run; a PID-only name let a new worker overwrite an earlier record, so the count never grew
    write(STATE / "workers" / f"{os.getpid()}-{time.time_ns()}.json", record)

    def agent(status, **extra):
        if agent_id:
            write(STATE / "agents" / f"{agent_id}.json", {**json.loads((STATE / "agents" / f"{agent_id}.json").read_text(encoding="utf-8")),
                                                         "status": status, "updatedAt": datetime.now(timezone.utc).isoformat(), **extra})

    def report(text):
        parent = os.environ.get("ROUTER_PARENT_ADDRESS") or os.environ.get("ROUTER_PARENT_AGENT_ID") or "nobody"
        box = STATE / "inbox" / re.sub(r"[^\w.-]", "_", parent)
        box.mkdir(parents=True, exist_ok=True)
        write(box / f"{len(list(box.glob('*.json'))) + 1}.json",
              {"to": parent, "from_pid": os.getpid(), "host": os.environ.get("ROUTER_PARENT_HOST"), "text": text})
        if bootstrap:
            sent = subprocess.run(["hc", "send", parent, text], capture_output=True, text=True, timeout=60)
            write(STATE / "hc-send" / f"{os.getpid()}.json", {"exit": sent.returncode, "stdout": sent.stdout[-400:], "stderr": sent.stderr[-400:]})

    if scenario == "crash":
        os._exit(0xC0000005 if os.name == "nt" else 139)
    if scenario == "blocked":
        sys.stdout.write(chr(27) + "]7501;state=blocked" + chr(27) + chr(92))
        sys.stdout.flush()
        while True:
            time.sleep(1)
    if scenario == "hang":
        agent("running")
        while True:
            time.sleep(1)
    if scenario == "slow":
        agent("running")
        time.sleep(6)
    if scenario == "error":
        sys.stderr.write("fixture worker failure: the task could not be completed\n")
        agent("error", lastError="fixture worker failure")
        return 3
    if scenario == "quota":
        sys.stderr.write("You've hit your usage limit. Try again later.\n")
        agent("error", lastError="You've hit your usage limit. Try again later.")
        return 1
    print(f"WORKER-DONE {name} scenario={scenario}")
    sys.stdout.flush()
    report(f"[Worker final report] {name} finished the fixture task ({scenario}); output sha {record['prompt_sha256'][:12]}")
    agent("idle", attentionReason="finished")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
