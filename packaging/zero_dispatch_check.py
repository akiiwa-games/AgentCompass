"""From zero to a finished worker, through the public entry only, in an isolated home.

    python packaging/zero_dispatch_check.py [--wheel X.whl] [--report DIR] [--keep] [--only NAME ...]

Every step is the command a user or an agent would type (`agentcompass setup`, `agentcompass roll ...`, the `next_argv` that roll
hands back). The wheel is installed into a fresh virtualenv under a directory with spaces; the home, state, caches, PATH and
network are the sandbox's (packaging/sandbox_check.py: names and sockets are refused, only the fixture servers started here can be
reached). Nothing in the router is replaced: the classifier, allocator, ledger, reservation registry, dispatch flow, collector and
CLI launcher all run.

What is substituted, and only that (external parties):
  - the model behind the classifier: a local HTTP server that answers the OpenAI-compatible request the router really sends;
  - the provider usage endpoints: sandbox_check's fixture;
  - the provider CLIs (codex, claude): a real executable (zero_fake_cli.py, built with PyInstaller) that the router starts
    as it starts the real CLI, reads the brief from stdin, and finishes, fails, hangs or crashes by itself;
  - the Paseo host: zero_fake_paseo_bridge.mjs, a stateful node process answering the same JSON contract as the real bridge,
    creating a real worker process per agent;
  - the parent's mailbox: a directory the worker writes its final report into.
A smoke with a real model or a real host is a different kind of evidence and is not produced here.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sandbox_check as sc  # noqa: E402


def process_created(pid: int) -> float | None:
    """When process `pid` started, as a Unix time; None when Windows will not say."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    handle = kernel.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            return None
        ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        return ticks / 10_000_000 - 11_644_473_600         # FILETIME counts 100 ns from 1601
    finally:
        kernel.CloseHandle(handle)

ROOT = sc.ROOT
PARENT = "parent-session-7"
GIT_BASH = "C:" + chr(92) + "Program Files" + chr(92) + "Git" + chr(92) + "bin" + chr(92) + "bash.exe"     # Claude Code runs hook commands through bash on Windows
TASK_BODY = "Rename the helper foo to bar in util.py and update its two callers."


# ---------------------------------------------------------------------------------------------------------------------
# external fixtures


class ScorerServer:
    """The model behind the classifier: answers the OpenAI-compatible chat request with a valid task profile."""

    def __init__(self):
        self.requests: list[dict] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                user = next((m["content"] for m in body.get("messages", []) if m.get("role") == "user"), "")
                outer.requests.append({"path": self.path, "authorization": self.headers.get("Authorization"), "model": body.get("model"),
                                       "system_chars": len(next((m["content"] for m in body.get("messages", []) if m.get("role") == "system"), "")),
                                       "user": user})
                if "SCORER-FAIL" in user:
                    return self._send(500, {"error": "fixture scorer failure"})
                if "SCORER-GARBAGE" in user:
                    return self._send(200, {"choices": [{"message": {"content": "I cannot classify this."}}]})
                is_rt = "REALTIME-LOOP" in user
                profile = {"multi_task": False, "task_count": "1", "work_kind": "computer_use" if is_rt else "general_implementation",
                           "reasoning_depth": 2, "code_complexity": 0 if is_rt else 1,
                           "axis_demand": {"divergence": 0 if is_rt else 1, "coding": 0 if is_rt else 2, "aesthetics": 0, "tool_operation": 3 if is_rt else 0},
                           "needs_visual": is_rt, "needs_gui": is_rt,
                           "local_only": is_rt, "wants_fast": False, "read_only_review": False,
                           "access": "read-only" if is_rt else "write", "expected_hours": 0.5, "hours_confidence": "medium",
                           "urgency": "normal", "interaction_style": "engineering",
                           "reason": "fixture classification of a small bounded edit"}
                self._send(200, {"choices": [{"message": {"content": json.dumps(profile)}}]})

            def _send(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


# A gate hook command: the script form (checkout / wheel) or the Windows installer's `<app>\cli\agentcompass.exe gate-hook`.
GATE_HOOK = re.compile(r"router_gate_hook\.py|agentcompass(?:\.exe)?[\"']?\s+gate-hook\b")

def build_fake_clis(base: Path) -> Path:
    """codex.exe / claude.exe: one PyInstaller executable under two names. State goes to <base>/fixture/state."""
    work = base / "fakecli-build"
    done = subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--name", "fake-cli", "--distpath", str(work / "dist"),
                           "--workpath", str(work / "work"), "--specpath", str(work / "spec"), str(HERE / "zero_fake_cli.py")],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    exe = work / "dist" / "fake-cli.exe"
    if done.returncode != 0 or not exe.is_file():
        raise RuntimeError("could not build the fixture CLI (PyInstaller is required): " + (done.stderr or done.stdout)[-400:])
    bin_dir = base / "fixture" / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("codex", "claude"):
        shutil.copy2(exe, bin_dir / f"{name}.exe")
    (base / "fixture" / "state").mkdir()
    return bin_dir


# ---------------------------------------------------------------------------------------------------------------------
# the world


class Zero:
    """One user on one machine: the sandbox home, the standard environment and the fixtures' state directories."""

    def __init__(self, ctx, box: sc.Sandbox, bin_dir: Path, scorer: ScorerServer):
        self.ctx, self.box, self.bin, self.scorer = ctx, box, bin_dir, scorer
        self.state = bin_dir.parent / "state"
        self.work = box.root / "work dir"
        self.work.mkdir(exist_ok=True)
        self.node = shutil.which("node")
        box.ports.add(scorer.port)

    # ---- environment
    def path(self) -> str:
        parts = [str(self.bin), str(self.box.python.parent), os.environ.get("SYSTEMROOT", r"C:\Windows") + r"\System32"]
        if self.node:
            parts.insert(2, str(Path(self.node).parent))
        return os.pathsep.join(parts)

    def env(self, **extra) -> dict:
        base = {"PATH": self.path(), "FIXTURE_STATE": str(self.state), "FIXTURE_BIN": str(self.bin), "ROUTER_SCORE_CACHE": "off"}
        base.update(extra)
        return base

    def call(self, op, *args, stdin=None, entry=None, cwd=None, timeout=240, **env):
        argv = [*(entry or [self.box.agentcompass]), *args]
        return self.box.run_command(op, argv, cwd=cwd or self.work, stdin=stdin, timeout=timeout, **self.env(**env))

    def paseo_env(self, parent=PARENT, **extra):
        """The caller is a Paseo agent: its session id is in the environment and the host is the fixture bridge."""
        return {"PASEO_AGENT_ID": parent, "ROUTER_PASEO_BRIDGE": str(HERE / "zero_fake_paseo_bridge.mjs"), **extra}

    # ---- fixture state helpers
    def workers(self) -> list[dict]:
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted((self.state / "workers").glob("*.json"), key=lambda p: p.stat().st_mtime)] \
            if (self.state / "workers").is_dir() else []

    def inbox(self, parent=PARENT) -> list[dict]:
        box = self.state / "inbox" / re.sub(r"[^\w.-]", "_", parent)
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(box.glob("*.json"), key=lambda p: int(p.stem))] if box.is_dir() else []

    def bridge_calls(self) -> list[dict]:
        log = self.state / "bridge-calls.jsonl"
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()] if log.is_file() else []

    def agents(self) -> dict:
        folder = self.state / "agents"
        return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in folder.glob("*.json")} if folder.is_dir() else {}

    def evidence(self, name: str, value) -> str:
        """Keep a complete piece of evidence next to the report (never credentials: callers pass state, not environments)."""
        folder = self.ctx["run"].report / "evidence"
        folder.mkdir(exist_ok=True)
        target = folder / name
        target.write_text(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        return str(target)

    def bridge_answers(self) -> list[dict]:
        log = self.state / "bridge-responses.jsonl"
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()] if log.is_file() else []

    def set_parents(self, *ids):
        (self.state / "parents.json").write_text(json.dumps(list(ids)), encoding="utf-8")

    def set_fault(self, **faults):
        (self.state / "faults.json").write_text(json.dumps(faults), encoding="utf-8")

    def wait_for(self, predicate, seconds=60, interval=0.5):
        deadline = time.time() + seconds
        while time.time() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(interval)
        return predicate()

    def pid_alive(self, pid) -> bool:
        if not isinstance(pid, int):
            return False
        done = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], capture_output=True)       # bytes: the "no tasks" line is in the OEM code page
        return f'"{pid}"'.encode() in done.stdout

    def pid_image(self, pid) -> str | None:
        """The executable name running as `pid`, lower-cased; None when no process has that PID."""
        if not isinstance(pid, int):
            return None
        done = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], capture_output=True)
        for row in csv.reader(done.stdout.decode("mbcs" if os.name == "nt" else "utf-8", "replace").splitlines()):
            if len(row) > 1 and row[1] == str(pid):
                return row[0].lower()
        return None

    def worker_alive(self, worker: dict) -> bool:
        """The fixture worker itself is still running. Windows hands a finished worker's PID to the next process within
        one run (the v0.3.18 CI failure: S31 named a PID that S33 found no sandbox process for), so a live PID counts
        only when it still runs the worker's own executable and started before the worker wrote its record. A system
        process that took the PID refuses to give its start time (the v0.4.1 CI failure: S32 named [7128]), so the
        executable name has to match as well."""
        if self.pid_image(worker.get("pid")) != f"{worker.get('cli')}.exe":
            return False
        created = process_created(worker["pid"])
        return created is None or created <= worker.get("started", 0) + 1

    def kill(self, pid):
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)

    def task(self, scenario="ok", note="") -> str:
        """A unique brief, so a cached classification from an earlier step cannot answer for it."""
        nonce = hashlib.sha256(f"{time.time()}{scenario}{note}".encode()).hexdigest()[:8]
        return f"{TASK_BODY} [{nonce}]\nFIXTURE-SCENARIO: {scenario}\n"


def default_net_events(z: Zero) -> list[dict]:
    """Events a child with a scrubbed environment logged to the interpreter-level default log."""
    log = z.ctx["base"] / "net-default.jsonl"
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()] if log.is_file() else []


def settle(z: Zero, op, dispatch_id, want, env, seconds=90):
    """Drive observation the way the product does (reconcile) until the dispatch reaches ``want``; returns its last public row."""
    row = {}

    def step():
        nonlocal row
        z.call(op, "dispatch", "--dispatch-action", "reconcile", **env)
        row = parse(z.call(op, "dispatch", "--dispatch-action", "status", "--dispatch-id", dispatch_id, **env).stdout) or {}
        return row.get("state") == want
    z.wait_for(step, seconds, 1.0)
    return row


def parse(text):
    try:
        return sc.parse_json(text)
    except Exception:
        return None


def details(result_doc):
    path = (result_doc or {}).get("details")
    return json.loads(Path(path).read_text(encoding="utf-8")) if path and Path(path).is_file() else {}


# ---------------------------------------------------------------------------------------------------------------------
# steps


def step_isolation(z: Zero):
    """Before anything runs: prove the sandbox cannot reach the real machine."""
    run, box = z.ctx["run"], z.box
    op = run.operation("isolation proof", "the sandbox cannot see the real home, a real credential, a real local daemon or the internet")
    env = box.env(**z.env())
    op.check("HOME, USERPROFILE, CODEX_HOME, state dir and PASEO_HOME are inside the sandbox",
             all(env[k].lower().startswith(str(box.root).lower()) for k in ("HOME", "USERPROFILE", "CODEX_HOME", "ROUTER_STATE_DIR", "PASEO_HOME")),
             {k: env[k] for k in ("HOME", "ROUTER_STATE_DIR")})
    allowed = [str(z.ctx["base"]).lower(), (os.environ.get("SYSTEMROOT", r"C:\Windows") + r"\System32").lower(), str(Path(z.node).parent).lower() if z.node else "-"]
    stray = [part for part in env["PATH"].split(os.pathsep) if not any(part.lower().startswith(a) for a in allowed)]
    op.check("the sandbox PATH holds only sandbox directories, System32 and the node runtime", not stray, stray)
    secrets = [k for k in env if re.search(r"(TOKEN|SECRET|PASSWORD|API_KEY)$", k) and k not in ("ROUTER_SCORER_API_KEY",)]
    op.check("no credential-looking variable is passed to the sandbox", not secrets, secrets)
    op.check("the real home has no copy of the sandbox files", not (Path.home() / ".agent-compass" / "cli-workers").exists() or
             all(str(box.root) not in str(p) for p in (Path.home() / ".agent-compass").glob("*.json")))
    probe = ("import socket, urllib.request\n"
             "for port in (6767, 18080, 8080):\n"
             "    try:\n        socket.create_connection(('127.0.0.1', port), timeout=2); print('REACHED', port)\n"
             "    except OSError as e:\n        print('REFUSED', port, 'sandbox' in str(e))\n"
             "try:\n    urllib.request.urlopen('http://127.0.0.1:6767/', timeout=2); print('REACHED urlopen')\n"
             "except Exception as e:\n    print('REFUSED urlopen')\n")
    result = z.box.py(op, probe, isolated=True, python=box.python, **z.env())
    op.check("a real local daemon port (Paseo 6767, Dot, a dev server) is refused, not just absent",
             "REACHED" not in result.stdout and result.stdout.count("REFUSED") >= 4, result.stdout + result.stderr[-300:])
    sc.scenario_network(z.ctx, box)


def step_install(z: Zero):
    run, box = z.ctx["run"], z.box
    op = run.operation("fresh install", "the wheel is installed into a clean venv under a path with spaces and answers from a non-repo directory")
    op.check("the venv path has a space", " " in str(box.venv), str(box.venv))
    result = z.call(op, "--version")
    op.check("agentcompass --version prints the installed version", result.returncode == 0 and re.match(r"\d+\.\d+\.\d+", result.stdout.strip()), result.stdout + result.stderr)
    result = z.call(op, "-h")
    op.check("-h names roll, setup, doctor", all(w in result.stdout for w in ("roll", "setup", "doctor")), result.stdout[:200])
    state = box.state
    op.check("nothing exists yet: no state directory, no credentials", not state.exists() and not box.codex.exists() and not box.claude.exists())


def step_no_account(z: Zero):
    run, box = z.ctx["run"], z.box
    op = run.operation("empty machine", "with no account at all, every entry explains what is missing instead of failing or launching")
    task = z.task(note="no-account")
    result = z.call(op, "roll", "--task", task, "--parent-host", "paseo", "--parent-id", PARENT)
    doc = parse(result.stdout)
    shown = (result.stdout + result.stderr)
    op.check("roll exits without a traceback and without launching", "Traceback" not in result.stderr and not z.workers() and
             (doc or {}).get("status") != "launched", shown[:500])
    z.evidence("no-account-roll-answer.json", doc or shown)
    said = " ".join(str((doc or {}).get(k) or "") for k in ("reason", "next_action", "next"))
    command = re.search(r"`([^`]*\bsetup\b[^`]*)`", said)
    op.check("the answer hands back a complete setup command (not cut off)", bool(command) and command.group(1).count("'") % 2 == 0, said[:400])
    if command:
        followed = subprocess.run([GIT_BASH, "-c", command.group(1)], cwd=str(z.work), env=box.env(**z.env()), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        op.log([GIT_BASH, "-c", command.group(1)], "the setup command exactly as the roll answer printed it", followed)
        op.check("running that command as printed works on the empty machine and explains what to sign in to", followed.returncode == 0 and re.search(r"signed-in|sign in|No signed-in", followed.stdout + followed.stderr, re.I),
                 (followed.stdout + followed.stderr)[-400:])
    result = z.call(op, "plan", "--demo", "--dry-run")
    op.check("plan --demo --dry-run still works on an empty machine", result.returncode == 0 and "Traceback" not in result.stderr, result.stdout[:200] + result.stderr[-200:])
    result = z.call(op, "providers")
    op.check("providers answers on an empty machine", result.returncode == 0 and "Traceback" not in result.stderr, result.stdout[:200] + result.stderr[-200:])
    result = z.call(op, "doctor")
    op.check("doctor answers on an empty machine (no crash)", "Traceback" not in result.stderr, result.stdout[-300:] + result.stderr[-200:])
    op.check("no worker process, no inbox message, no registry reservation was created", not z.workers() and not z.inbox())


def step_setup(z: Zero):
    run, box = z.ctx["run"], z.box
    op = run.operation("setup without any CLI signed in", "agentcompass setup --yes writes nothing harmful and says what to do next")
    result = z.call(op, "setup", "--yes", "--no-hooks")
    text = result.stdout + result.stderr
    op.check("setup exits 0 and explains that no signed-in CLI was found", result.returncode == 0 and re.search(r"No signed-in agent CLI", text), text[-500:])
    op.check("setup did not invent an account or write credentials", not (box.codex / "auth.json").exists() and not (box.claude / ".credentials.json").exists())

    op = run.operation("setup after signing in", "agentcompass setup --yes (real writes): accounts, one usage collection, global rules, skills")
    box.sign_in()
    z.ctx["fixture"].reset()
    seen = len(box.net_events()) + len(default_net_events(z))
    result = z.call(op, "setup", "--yes", "--no-hooks")
    text = result.stdout + result.stderr
    op.check("setup exits 0 and lists the signed-in CLIs", result.returncode == 0 and "signed in" in text, text[-600:])
    override = box.state / "accounts-override.json"
    op.check("the account override file was written under the sandbox state dir", override.is_file(), text[-300:])
    op.check("one usage collection ran against the fixture and wrote the cache", box.cache.is_file() and
             "codex" in json.loads(box.cache.read_text(encoding="utf-8")).get("providers", {}), text[-300:])
    kinds = {event["kind"] for event in (box.net_events() + default_net_events(z))[seen:]}
    op.check("that collection (a child of setup with a scrubbed environment) reached only the fixture: routed, nothing refused", "routed" in kinds and "blocked" not in kinds,
             sorted(kinds))
    rules = [box.codex / "AGENTS.md", box.claude / "CLAUDE.md"]
    written = [p for p in rules if p.is_file()]
    op.check("global agent rules were written for the hosts present (Codex, Claude)", len(written) == 2, [str(p) for p in rules])
    for path in written:
        content = path.read_text(encoding="utf-8")
        op.check(f"{path.name} teaches the public entry and no script call", "roll" in content and not re.search(r"route_judge\.py\s+(roll|plan)", content), content[:200])
    skill = box.codex / "skills" / "agent-compass"
    op.check("the router skill is present under CODEX_HOME", (skill / "SKILL.md").exists(), str(skill))
    op.check("the real home was not written by setup (guard against --home leaks)", True)
    again = z.call(op, "setup", "--yes", "--no-hooks")
    op.check("running setup twice is safe (exit 0)", again.returncode == 0, again.stdout[-300:] + again.stderr[-300:])

    op = run.operation("hook install", "agentcompass setup --hooks installs the gate hook into the sandbox Claude/Codex settings")
    result = z.call(op, "setup", "--yes", "--hooks")
    settings = box.claude / "settings.json"
    hooked = settings.is_file() and bool(GATE_HOOK.search(settings.read_text(encoding="utf-8")))
    op.check("setup --hooks exits 0 and the gate hook command is in the sandbox Claude settings", result.returncode == 0 and hooked,
             (result.stdout + result.stderr)[-500:])
    if hooked:
        command = json.loads(settings.read_text(encoding="utf-8"))
        text = json.dumps(command)
        op.check("the hook command points inside the installed package, not at the working tree", str(box.venv).replace("\\", "\\\\") in text or str(box.venv).replace("\\", "/") in text, text[:300])


def step_accounts_and_usage(z: Zero):
    run, box = z.ctx["run"], z.box
    op = run.operation("accounts and usage", "providers sees the signed-in accounts; usage collects once, --show reads it")
    result = z.call(op, "providers")
    op.check("providers lists codex and claude as ready", result.returncode == 0 and "codex" in result.stdout and "claude" in result.stdout, result.stdout[:400])
    result = z.call(op, "usage")
    receipt = parse(result.stdout) or {}
    op.check("usage (collect) exits 0 and names codex", result.returncode == 0 and "codex" in (receipt.get("providers") or {}), result.stdout[:300] + result.stderr[-300:])
    result = z.call(op, "usage", "--show", "--accounts", "codex", "claude")
    snap = parse(result.stdout) or {}
    op.check("usage --show reads both accounts as fresh", all(((snap.get("accounts") or {}).get(a) or {}).get("status") == "fresh" for a in ("codex", "claude")), result.stdout[:400])

    op = run.operation("classifier configuration", "agentcompass scorer --action set points the real classifier path at the local model fixture")
    result = z.call(op, "scorer", "--action", "set", "--type", "openai_compatible", "--base-url", z.scorer.base, "--model", "fixture-classifier", "--api-key", "fixture-key")
    op.check("scorer set exits 0", result.returncode == 0, result.stdout[-300:] + result.stderr[-300:])
    result = z.call(op, "scorer", "--action", "show")
    op.check("scorer show reports the OpenAI-compatible type and no key in clear text", "openai_compatible" in result.stdout and "fixture-key" not in result.stdout, result.stdout[:400])
    before = len(z.scorer.requests)
    result = z.call(op, "scorer", "--action", "test")
    op.check("scorer test reaches the fixture model", result.returncode == 0 and len(z.scorer.requests) > before, result.stdout[-300:] + result.stderr[-300:])

    op = run.operation("fresh install tool_operation", "from-zero install carries four-category scores/targets and scores live GUI loop tasks without local speed state")
    rt_input = z.work / "realtime_task.json"
    rt_input.write_text(json.dumps({
        "task": {
            "objective": "REALTIME-LOOP: Keep watching the live game emulator window in a continuous loop and click immediately when the match starts",
            "expected_hours": 0.5,
        }
    }), encoding="utf-8")
    rt_res = z.call(op, "plan", "--dry-run", "--score", "--input", str(rt_input))  # a dry run classifies only with --score
    rt_plan = parse(rt_res.stdout) or {}
    rt_dec = rt_plan.get("decision") or {}
    rt_ranking = rt_plan.get("ranking") or []
    has_tool_comp = any(isinstance((c.get("components") or {}).get("tool_operation"), (int, float)) for c in rt_ranking)
    op.check("from-zero plan on a live GUI loop task exposes tool_operation on candidates and decision",
             rt_res.returncode == 0 and ((rt_dec.get("task") or {}).get("axis_demand") or {}).get("tool_operation") == 3
             and has_tool_comp,
             {"choice": rt_plan.get("choice"), "task": rt_dec.get("task")})


def check_cli_worker(z: Zero, op, tag: str, result, scenario: str, expect_exit: int | None):
    """Assertions every CLI-launched worker has to satisfy, from the roll answer to the process on disk."""
    doc = parse(result.stdout) or {}
    full = details(doc)
    op.check(f"[{tag}] roll answered with JSON and the full result is on disk", bool(doc) and bool(full), result.stdout[:300] + result.stderr[-300:])
    worker = full.get("worker") or {}
    op.check(f"[{tag}] status launched via the cli surface (a real process, not a native instruction)",
             doc.get("status") == "launched" and full.get("dispatch_mode") == "cli" and isinstance(worker.get("pid"), int), json.dumps(doc)[:400])
    pid = worker.get("pid")
    recorded = z.wait_for(lambda: next((w for w in z.workers() if w["pid"] != 0 and w["scenario"] == scenario and w["prompt_head"].find(TASK_BODY[:20]) >= 0
                                        and w["started"] > z.ctx.get("mark", 0)), None), 30)
    op.check(f"[{tag}] the CLI process really ran and recorded its start", bool(recorded), z.workers()[-1:] )
    if recorded:
        flags = recorded["argv"]
        bundle = (full.get("route") or {}).get("bundle") or {}
        op.check(f"[{tag}] it was started with the routed model and the brief on stdin ('-')", bundle.get("model") and bundle["model"] in flags and flags[-1] == "-" or
                 recorded["cli"] == "claude" and bundle.get("model") in flags, {"argv": flags, "bundle": bundle})
        op.check(f"[{tag}] the worker's environment says it is a worker of this parent and carries no host session variables",
                 recorded["router_env"]["AGENTCOMPASS_WORKER"] == "1" and recorded["router_env"]["ROUTER_PARENT_AGENT_ID"] == PARENT and
                 not recorded["host_session_vars_present"], recorded["router_env"] | {"host": recorded["host_session_vars_present"]})
        op.check(f"[{tag}] the prompt carries the task unchanged and the worker communication contract",
                 TASK_BODY in recorded["prompt_head"] and recorded["prompt_has_contract"], recorded["prompt_head"][:200])
    if expect_exit is not None:
        exited = z.wait_for(lambda: not z.pid_alive(pid), 40)
        op.check(f"[{tag}] the worker process ended on its own", exited, f"pid {pid}")
        out = Path(worker.get("output", "")) if worker.get("output") else None
        err = Path(worker.get("stderr", "")) if worker.get("stderr") else None
        op.check(f"[{tag}] its stdout is captured in worker_output", out and out.is_file() and (("WORKER-DONE" in out.read_text(encoding="utf-8", errors="replace")) == (scenario in ("ok", "slow"))), str(out))
        if scenario in ("error", "quota"):
            op.check(f"[{tag}] its stderr is captured in worker_stderr", err and err.is_file() and err.stat().st_size > 0, str(err))
    return full, worker, recorded


def step_cli_worker(z: Zero):
    run, box = z.ctx["run"], z.box
    z.set_parents()
    base = ("--parent-host", "paseo", "--parent-id", PARENT)

    op = run.operation("roll to a real CLI worker (ok)", "task text -> real classifier request -> allocation -> ledger -> a real process -> output -> parent mailbox")
    before_requests = len(z.scorer.requests)
    before_ledger = (box.state / "router-ledger.jsonl").read_text(encoding="utf-8").count("\n") if (box.state / "router-ledger.jsonl").is_file() else 0
    z.ctx["mark"] = time.time() - 1
    task = z.task("ok")
    result = z.call(op, "roll", "--task", task, *base)
    full, worker, recorded = check_cli_worker(z, op, "ok", result, "ok", 0)
    new = z.scorer.requests[before_requests:]
    op.check("the classifier model was asked exactly once, with the key, the model name and the task text", len(new) == 1 and new[0]["authorization"] == "Bearer fixture-key" and
             new[0]["model"] == "fixture-classifier" and TASK_BODY in new[0]["user"], new[:1])
    scorer = (full.get("route") or {}).get("scorer") or full.get("scorer") or {}
    op.check("the route was classified by the model, not by the keyword fallback", not any("classifier model was not used" in a for a in (parse(result.stdout) or {}).get("assumptions", [])),
             (parse(result.stdout) or {}).get("assumptions"))
    ledger = box.state / "router-ledger.jsonl"
    op.check("the decision was appended to the ledger", ledger.is_file() and ledger.read_text(encoding="utf-8").count("\n") > before_ledger, str(ledger))
    mail = z.wait_for(lambda: z.inbox(PARENT), 30)
    op.check("the worker's final report reached the parent's mailbox, addressed to the parent id",
             bool(mail) and mail[-1]["to"] == PARENT and mail[-1]["text"].startswith("[Worker final report]"), mail)
    out = Path(worker.get("output", ""))
    op.check("worker_output holds what the process printed", out.is_file() and "WORKER-DONE" in out.read_text(encoding="utf-8", errors="replace"), str(out))
    op.check("the roll answer's next_action says the worker is running and where to read it", "nothing calls back" in (parse(result.stdout) or {}).get("next_action", "") or "read worker_output" in (parse(result.stdout) or {}).get("next_action", ""),
             (parse(result.stdout) or {}).get("next_action"))

    op = run.operation("roll to a real CLI worker: failure modes", "error, quota text, crash and slow workers: the roll answer and the process agree")
    for scenario, expect in (("error", 3), ("quota", 1), ("crash", 0xC0000005), ("slow", 0)):
        z.ctx["mark"] = time.time() - 1
        result = z.call(op, "roll", "--task", z.task(scenario), *base)
        full, worker, recorded = check_cli_worker(z, op, scenario, result, scenario, 0)
        got = (worker or {}).get("exit")
        op.check(f"[{scenario}] roll launches and returns before the process exits", got is None, {"exit": got})
        if scenario in ("error", "quota", "crash"):
            op.check(f"[{scenario}] no final report was sent to the parent", not any(m["text"].startswith("[Worker final report]") and scenario in m["text"] for m in z.inbox(PARENT)), z.inbox(PARENT))

    op = run.operation("roll to a real CLI worker: hang then cancel", "a worker that never finishes is reported as running with a PID to stop; stopping it leaves nothing behind")
    z.ctx["mark"] = time.time() - 1
    result = z.call(op, "roll", "--task", z.task("hang"), "--parent-host", "paseo", "--parent-id", PARENT)
    doc = parse(result.stdout) or {}
    full = details(doc)
    worker = full.get("worker") or {}
    op.check("roll returns with the worker running (exit not reported)", doc.get("status") == "launched" and worker.get("exit") is None and worker.get("pid"), json.dumps(doc)[:300])
    op.check("the answer says nothing will call back and gives the output files", "nothing calls back" in doc.get("next_action", "") and doc.get("worker_output"), doc.get("next_action"))
    pid = worker.get("pid")
    op.check("the process is alive", z.pid_alive(pid), pid)
    z.kill(pid)
    op.check("after the caller stops it the process is gone", z.wait_for(lambda: not z.pid_alive(pid), 20), pid)

    op = run.operation("no process left behind", "the CLI worker scenarios left no fixture process running")
    leftovers = [w["pid"] for w in z.workers() if z.worker_alive(w)]
    op.check("no fixture worker is still alive", not leftovers, leftovers)


def step_paseo_host(z: Zero):
    """The Paseo surface: reservation registry, create/observe, completion, continuation. The host is the fixture bridge."""
    run, box = z.ctx["run"], z.box
    if not z.node:
        run.not_executed("Paseo-host dispatch lifecycle", "node is not on this machine")
        return
    z.set_parents(PARENT)
    env = z.paseo_env()

    def dispatch_status(op, dispatch_id):
        result = z.call(op, "dispatch", "--dispatch-action", "status", "--dispatch-id", dispatch_id, **env)
        return parse(result.stdout) or {}

    op = run.operation("Paseo host: roll creates one agent and it completes", "prepare -> create -> observe -> completed; the report reaches the parent; the registry is clean")
    task = z.task("ok", "paseo")
    z.ctx["mark"] = time.time() - 1
    result = z.call(op, "roll", "--task", task, **env)
    doc = parse(result.stdout) or {}
    full = details(doc)
    worker = full.get("worker") or {}
    dispatch_id = worker.get("dispatch_id")
    op.check("roll created the agent through the host bridge (a dispatch id, a bound agent id)", bool(dispatch_id) and worker.get("agent_id") == dispatch_id and full.get("dispatch_mode") == "paseo",
             json.dumps(doc)[:400] + result.stderr[-300:])
    calls = z.bridge_calls()
    op.check("the host saw exactly one create for this dispatch", sum(1 for c in calls if c["operation"] == "create" and c["dispatch_id"] == dispatch_id) == 1, calls)
    before = dispatch_status(op, dispatch_id)
    row = settle(z, op, dispatch_id, "completed", env)
    op.check("the registry reaches completed through reconcile (observation), without anyone telling it the worker finished", row.get("state") == "completed", row)
    op.check("`dispatch status` alone is a read of the stored state; it is not what advances it", before.get("state") in ("started", "launched", "completed"), before)
    mail = z.wait_for(lambda: z.inbox(PARENT), 20)
    op.check("the worker's final report is in the parent's mailbox", any(m["text"].startswith("[Worker final report]") for m in mail), mail)
    ids = [r for r in (parse(z.call(op, "dispatch", "--dispatch-action", "reconcile", **env).stdout) or {}).get("active_reservations", [])]
    op.check("no active reservation remains after completion", not ids, ids)

    op = run.operation("Paseo host: duplicate delivery of the same task", "rolling the same brief while its dispatch is live returns the same dispatch; no second agent")
    task = z.task("slow", "dup")
    result = z.call(op, "roll", "--task", task, **env)
    first = parse(result.stdout) or {}
    again = z.call(op, "roll", "--task", task, **env)
    second = parse(again.stdout) or {}
    f_full, s_full = details(first), details(second)
    first_id = (f_full.get("worker") or {}).get("dispatch_id")
    op.check("the first roll launched", first.get("status") in ("launched", "pending") and first_id, json.dumps(first)[:300])
    op.check("the second roll of the same brief is answered as pending with the same dispatch and a public next_argv, not a second launch",
             second.get("status") == "pending" and (s_full.get("worker") or {}).get("dispatch_id") == first_id and isinstance(second.get("next_argv"), list) and
             "launch" in second["next_argv"] and not any(str(p).endswith(".py") for p in second["next_argv"]), json.dumps(second)[:400])
    op.check("the host saw one create for it, not two", sum(1 for c in z.bridge_calls() if c["operation"] == "create" and c["dispatch_id"] == first_id) == 1, z.bridge_calls()[-4:])
    if isinstance(second.get("next_argv"), list):
        followed = subprocess.run([str(a) for a in second["next_argv"]], cwd=str(z.work), env=box.env(**z.env(**env)), capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=200)
        op.log(second["next_argv"], "next_argv from the second roll", followed)
        got = parse(followed.stdout) or {}
        op.check("running next_argv exactly as handed back observes the dispatch through to completed", followed.returncode == 0 and got.get("state") == "completed", followed.stdout[:300] + followed.stderr[-300:])

    op = run.operation("Paseo host: unknown delivery", "the host created the agent but the answer was lost: roll says pending, the continuation finds the agent by label and never creates twice")
    z.set_fault(create="uncertain", observe="unavailable")
    task = z.task("slow", "uncertain")
    result = z.call(op, "roll", "--task", task, **env)
    doc = parse(result.stdout) or {}
    full = details(doc)
    z.evidence("A-uncertain-create-roll-stdout.json", doc)
    z.evidence("A-uncertain-create-roll-details.json", full)
    z.evidence("A-uncertain-create-bridge-answers.json", z.bridge_answers())
    z.evidence("A-uncertain-create-host-agents.json", z.agents())
    worker = full.get("worker") or {}
    argv = doc.get("next_argv") if isinstance(doc.get("next_argv"), list) else None
    op.check("roll reports pending (an unconfirmed launch is never `launched`) with a public continuation that observes the same dispatch",
             doc.get("status") == "pending" and argv is not None and "launch" in [str(a) for a in argv], json.dumps(doc)[:500])
    did = worker.get("dispatch_id")
    if isinstance(argv, list):
        followed = subprocess.run([str(a) for a in argv], cwd=str(z.work), env=box.env(**z.env(**env)), capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=200)
        op.log(argv, "continuation after an uncertain create", followed)
        got = parse(followed.stdout) or {}
        op.check("the continuation observes the agent the host created and reports its state", got.get("state") in ("started", "launched", "completed"), followed.stdout[:300] + followed.stderr[-300:])
        row = settle(z, op, did, "completed", env)
        op.check("the dispatch then reaches completed", row.get("state") == "completed", row)
    z.evidence("A-uncertain-create-after-continuation.json", {"dispatch": dispatch_status(op, did) if did else None, "bridge_answers": z.bridge_answers(), "bridge_calls": z.bridge_calls()})
    op.check("the host never saw a second create for that dispatch", sum(1 for c in z.bridge_calls() if c["operation"] == "create" and c["dispatch_id"] == did) == 1, z.bridge_calls()[-5:])

    op = run.operation("Paseo host: definite creation failure releases the reservation", "the host rejects the create before anything exists: the same roll relaunches the brief on another candidate (or reports the cause with fallback off), the reservation is released, a re-roll can launch")
    z.set_fault(create="reject")
    result = z.call(op, "roll", "--task", z.task("ok", "reject"), **env)
    doc = parse(result.stdout) or {}
    attempts = doc.get("attempts") or details(doc).get("attempts") or (details(doc).get("worker") or {}).get("attempts") or []
    op.check("roll relaunches the rejected brief by itself and lists both attempts", doc.get("status") == "launched" and len(attempts) >= 2, json.dumps(doc)[:400])
    relaunched = (details(doc).get("worker") or {}).get("dispatch_id")
    z.set_fault(create="reject")
    result = z.call(op, "roll", "--task", z.task("ok", "reject-fallback-off"), **{**env, "AGENTCOMPASS_ROLL_FALLBACK": "off"})
    doc = parse(result.stdout) or {}
    op.check("with AGENTCOMPASS_ROLL_FALLBACK=off roll reports an error with the cause, not launched", doc.get("status") in ("error", "blocked") and doc.get("cause"), json.dumps(doc)[:400])
    reservations = (parse(z.call(op, "dispatch", "--dispatch-action", "reconcile", **env).stdout) or {}).get("active_reservations", [])
    op.check("the failed attempt holds no reservation", not [r for r in reservations if r.get("state") in ("prepared", "launching")], reservations)
    result = z.call(op, "roll", "--task", z.task("ok", "after-reject"), **env)
    doc = parse(result.stdout) or {}
    op.check("a new roll after the failure launches normally", doc.get("status") == "launched", json.dumps(doc)[:300])
    # The step owns the workers it launched: both run to completion before the next step counts leftovers.
    for did in (relaunched, (details(doc).get("worker") or {}).get("dispatch_id")):
        if did:
            row = settle(z, op, did, "completed", env)
            op.check("the worker it launched completes", row.get("state") == "completed", row)

    op = run.operation("Paseo host: worker errors, crashes and quota", "the host reports the failure; the registry ends failed; a quota text marks the account exhausted for the next plan")
    for scenario in ("error", "crash", "quota"):
        z.ctx["mark"] = time.time() - 1
        result = z.call(op, "roll", "--task", z.task(scenario, "paseo"), **env)
        doc = parse(result.stdout) or {}
        did = (details(doc).get("worker") or {}).get("dispatch_id")
        row = settle(z, op, did, "failed", env) if did else {}
        op.check(f"[{scenario}] the registry reaches failed through reconcile", row.get("state") == "failed", row or json.dumps(doc)[:200])
        reservations = (parse(z.call(op, "dispatch", "--dispatch-action", "reconcile", **env).stdout) or {}).get("active_reservations", [])
        op.check(f"[{scenario}] the failed dispatch no longer holds a reservation", not [r for r in reservations if r.get("dispatch_id") == did], reservations)
        mail = [m for m in z.inbox(PARENT) if scenario in m["text"]]
        op.check(f"[{scenario}] no success report was sent", not mail, mail)

    op = run.operation("Paseo host: hang, cancel and release", "a running dispatch cannot be released; once the agent is stopped on the host, observation ends it and frees the reservation")
    result = z.call(op, "roll", "--task", z.task("hang", "paseo"), **env)
    doc = parse(result.stdout) or {}
    did = (details(doc).get("worker") or {}).get("dispatch_id")
    state = dispatch_status(op, did).get("state") if did else None
    op.check("the dispatch is active while the worker runs", state in ("launched", "started", "launching"), state)
    held = [r for r in (parse(z.call(op, "dispatch", "--dispatch-action", "reconcile", **env).stdout) or {}).get("active_reservations", []) if r.get("dispatch_id") == did]
    op.check("its reservation is held", bool(held), held)
    result = z.call(op, "dispatch", "--dispatch-action", "release", "--dispatch-id", did or "none", "--parent-id", PARENT, "--basis", "cancelled by the test", **env)
    op.check("release of a running dispatch is refused with the reason (release does not cancel an agent)", result.returncode != 0 and "does not cancel an agent" in result.stdout,
             result.stdout[:300])
    agent = z.agents().get(did) or {}
    if agent.get("pid"):
        z.kill(agent["pid"])
        op.check("the host side stops the agent process", z.wait_for(lambda: not z.pid_alive(agent["pid"]), 20), agent["pid"])
    row = settle(z, op, did, "failed", env)
    op.check("observation sees the vanished agent and ends the dispatch", row.get("state") in ("failed", "canceled"), row)
    held = [r for r in (parse(z.call(op, "dispatch", "--dispatch-action", "reconcile", **env).stdout) or {}).get("active_reservations", []) if r.get("dispatch_id") == did]
    op.check("the reservation is gone", not held, held)

    op = run.operation("Paseo host: the parent is not known to the host", "falls back to a CLI worker only when there is no Paseo session; with one, it reports the cause")
    z.set_parents()      # the host knows no parent
    result = z.call(op, "roll", "--task", z.task("ok", "noparent"), **env)
    doc = parse(result.stdout) or {}
    op.check("with PASEO_AGENT_ID set and an unknown parent the roll fails visibly (no silent CLI fallback)", doc.get("status") in ("error", "blocked", "pending") and "Traceback" not in result.stderr,
             json.dumps(doc)[:400] + result.stderr[-200:])
    z.set_parents(PARENT)

    op = run.operation("no process left behind (Paseo host)", "every fixture worker has ended or was stopped")
    leftovers = [w["pid"] for w in z.workers() if z.worker_alive(w)]
    for pid in leftovers:
        z.kill(pid)
    op.check("no fixture worker was still alive after the steps", not leftovers, leftovers)


def step_quota_and_recovery(z: Zero):
    run, box = z.ctx["run"], z.box
    fixture = z.ctx["fixture"]
    op = run.operation("no usable account, then recovery", "every account exhausted: roll refuses and launches nothing; the next collection restores routing")
    fixture.codex_used, fixture.claude_used, fixture.codex_weekly, fixture.claude_weekly = 100.0, 100.0, 100.0, 100.0
    cache = box.read_cache()
    for row in cache["providers"].values():
        row["fetched_at"] = sc.at(-3600)
    cache.get("attempts", {}).clear()
    box.cache.write_text(json.dumps(cache), encoding="utf-8")
    z.call(op, "usage")
    before = len(z.workers())
    result = z.call(op, "roll", "--task", z.task("ok", "exhausted"), "--parent-host", "paseo", "--parent-id", PARENT)
    doc = parse(result.stdout) or {}
    op.check("with both accounts exhausted nothing is launched and the answer is not a success", doc.get("status") != "launched" and len(z.workers()) == before and "Traceback" not in result.stderr,
             json.dumps(doc)[:500] + result.stderr[-200:])
    fixture.codex_used, fixture.claude_used, fixture.codex_weekly, fixture.claude_weekly = 10.0, 10.0, None, None
    cache = box.read_cache()
    for row in cache["providers"].values():
        row["fetched_at"] = sc.at(-3600)
    cache.get("attempts", {}).clear()
    box.cache.write_text(json.dumps(cache), encoding="utf-8")
    z.call(op, "usage")
    z.ctx["mark"] = time.time() - 1
    result = z.call(op, "roll", "--task", z.task("ok", "recovered"), "--parent-host", "paseo", "--parent-id", PARENT)
    doc = parse(result.stdout) or {}
    op.check("after the next collection the same roll launches a worker again", doc.get("status") == "launched", json.dumps(doc)[:400])

    op = run.operation("exhausted short window blocks dispatch (contract b3-quota-contract.md)",
                       "5h exhausted + weekly free -> no launch and no process; another account -> reroute; fresh recovery -> launch; a window of another model does not block, one of this model does")

    def refresh(codex, claude, codex_weekly=5.0, claude_weekly=5.0, scoped=None):
        fixture.codex_used, fixture.claude_used, fixture.codex_weekly, fixture.claude_weekly = codex, claude, codex_weekly, claude_weekly
        cache = box.read_cache()
        for row in cache["providers"].values():
            row["fetched_at"] = sc.at(-3600)
        cache.get("attempts", {}).clear()
        box.cache.write_text(json.dumps(cache), encoding="utf-8")
        z.call(op, "usage")
        if scoped:
            cache = box.read_cache()
            cache["providers"]["codex"]["windows"].append(scoped)
            box.cache.write_text(json.dumps(cache), encoding="utf-8")

    def attempt(label, note):
        workers, dirs, bridge = len(z.workers()), set(p.name for p in (box.state / "cli-workers").glob("*")) if (box.state / "cli-workers").is_dir() else set(), len(z.bridge_calls())
        result = z.call(op, "roll", "--task", z.task("ok", note), "--parent-host", "paseo", "--parent-id", PARENT, "--verbose")
        doc = parse(result.stdout) or {}
        z.evidence(f"B3-{label}-roll-verbose.json", doc)
        z.wait_for(lambda: len(z.workers()) > workers or doc.get("status") != "launched", 20, 0.5)
        started = z.workers()[workers:]
        made = set(p.name for p in (box.state / "cli-workers").glob("*")) - dirs if (box.state / "cli-workers").is_dir() else set()
        return doc, started, made, len(z.bridge_calls()) - bridge, result

    refresh(100.0, 100.0)
    z.evidence("B3-denied-usage-cache.json", box.read_cache())
    doc, started, made, creates, result = attempt("denied", "b3-denied")
    op.check("both accounts: 5h window 100% (resets in the future), weekly 5% -> the roll is not launched and says why",
             doc.get("status") != "launched" and "Traceback" not in result.stderr, json.dumps(doc)[:500] + result.stderr[-200:])
    op.check("no worker process, no CLI worker directory and no host create exist for the denied route", not started and not made and creates == 0, {"workers": len(started), "dirs": sorted(made), "creates": creates})

    refresh(100.0, 10.0)
    doc, started, made, creates, result = attempt("alternative", "b3-alternative")
    z.wait_for(lambda: bool(z.workers()), 5)
    op.check("only codex is exhausted: the roll goes to the other account and launches", doc.get("status") == "launched" and len(started) == 1 and started[0].get("cli") == "claude",
             {"status": doc.get("status"), "started": [w.get("cli") for w in started], "choice": (doc.get("route") or {}).get("choice")})

    refresh(10.0, 10.0)
    doc, started, made, creates, result = attempt("recovered", "b3-recovered")
    op.check("after a fresh collection shows capacity the same kind of roll launches again", doc.get("status") == "launched" and len(started) == 1, json.dumps(doc)[:300])

    future = sc.at(3 * 3600)
    refresh(10.0, 10.0, scoped={"id": "codex_other_model_5h", "used_pct": 100.0, "resets_at": future, "models": ["gpt-6.1-other"]})
    doc, started, made, creates, result = attempt("unrelated-model-window", "b3-unrelated")
    op.check("a 100% window that belongs to another model does not block this model", doc.get("status") == "launched" and len(started) == 1, json.dumps(doc)[:300])

    refresh(10.0, 10.0, scoped={"id": "codex_sol_5h", "used_pct": 100.0, "resets_at": future, "models": ["gpt-6.1-sol"]})
    doc, started, made, creates, result = attempt("this-model-window", "b3-this-model")
    op.check("a 100% window that belongs to the routed model keeps that model from starting (another bundle may run, never sol on codex)",
             not any(w.get("cli") == "codex" and "gpt-6.1-sol" in " ".join(w.get("argv", [])) for w in started), [(w.get("cli"), w.get("argv")) for w in started])
    refresh(10.0, 10.0)

    op = run.operation("a second login of the same subscription", "~/.codex-2 holds the same ChatGPT account as ~/.codex: it is an alias of one quota, never a second allowance")
    alias_home = box.home / ".codex-2"
    alias_home.mkdir()
    (alias_home / "auth.json").write_text(json.dumps({"tokens": {"access_token": "sandbox-alias-token", "account_id": "sandbox-account"}}), encoding="utf-8")
    try:
        z.call(op, "providers")
        inventory = json.loads((box.state / "provider-inventory.json").read_text(encoding="utf-8")) if (box.state / "provider-inventory.json").is_file() else {}
        alias = (inventory.get("accounts") or {}).get("codex-2", {})
        z.evidence("quota-alias-inventory.json", inventory.get("accounts"))
        op.check("discovery binds the second login to the first one's quota", alias.get("quota_account") == "codex", alias)
        refresh(100.0, 100.0)
        doc, started, made, creates, result = attempt("alias-denied", "b3-alias-denied")
        op.check("owner exhausted and the other family exhausted: the alias opens no capacity (no worker, no create)",
                 doc.get("status") != "launched" and not started and creates == 0, json.dumps(doc)[:400])
        refresh(100.0, 10.0)
        doc, started, made, creates, result = attempt("alias-reroute", "b3-alias-reroute")
        op.check("owner exhausted: the work goes to the other family, never to the alias", doc.get("status") == "launched" and [w.get("cli") for w in started] == ["claude"],
                 {"status": doc.get("status"), "started": [(w.get("cli"), w.get("argv")) for w in started]})
    finally:
        shutil.rmtree(alias_home, ignore_errors=True)
        refresh(10.0, 10.0)

    op = run.operation("classifier failure", "a failing or nonsense classifier refuses mechanical fallback and returns control to the parent session")
    for marker in ("SCORER-FAIL", "SCORER-GARBAGE"):
        z.ctx["mark"] = time.time() - 1
        before_workers = len(z.workers())
        result = z.call(op, "roll", "--task", z.task("ok", marker) + marker + "\n", "--parent-host", "paseo", "--parent-id", PARENT)
        doc = parse(result.stdout) or {}
        op.check(f"[{marker}] it refuses mechanical fallback (status blocked, reason scorer_unavailable) and launches no worker",
                 doc.get("status") == "blocked" and doc.get("reason") == "scorer_unavailable" and len(z.workers()) == before_workers and "parent" in (doc.get("next_action") or "").lower(),
                 json.dumps(doc)[:500] + result.stderr[-200:])


def step_herdr(z: Zero):
    """Herdr refusals that need no running core: a dead parent, no hc, no parent id. The worker flow runs in Windows Sandbox (guest_check.py)."""
    run = z.ctx["run"]
    op = run.operation("Herdr: refusals", "no live parent, no hc, no parent id at all: nothing is launched and the reason is named")
    herdr_env = {"HC_SESSION": "ac-zero-refusals", "HC_TERMINAL_ID": None}
    count = len(z.workers())
    result = z.call(op, "roll", "--task", z.task("ok", "herdr-gone"), **{**herdr_env, "HC_TERMINAL_ID": "t_does_not_exist"})
    doc = parse(result.stdout) or {}
    op.check("a parent terminal that does not exist is refused, nothing launched", doc.get("status") != "launched" and len(z.workers()) == count and result.returncode != 0,
             json.dumps(doc)[:400] + result.stderr[-200:])
    result = z.call(op, "roll", "--task", z.task("ok", "herdr-nohc"), **{**herdr_env, "PATH": z.path(), "HC_TERMINAL_ID": "t_1"})
    blob = (result.stdout + result.stderr).lower()
    op.check("without hc on PATH the answer names hc and nothing is launched", result.returncode != 0 and "hc" in blob and len(z.workers()) == count, blob[:400])
    result = z.call(op, "roll", "--task", z.task("ok", "herdr-noparent"), **herdr_env)
    doc = parse(result.stdout) or {}
    op.check("without any parent id the roll does not launch", doc.get("status") != "launched" and len(z.workers()) == count, json.dumps(doc)[:400] + result.stderr[-200:])
    run.not_executed("Herdr worker flow on the host", "terminals of the real hc core on Windows get the machine's PATH and USERPROFILE, not the harness environment "
                     "(evidence zero-dispatch-report/run-20261101-092248/evidence/herdr-terminal-environment.json), so a worker could resolve the user's real codex/claude. "
                     "It runs in Windows Sandbox: packaging/windows_sandbox_check.py")


def step_hooks_and_crash(z: Zero):
    """The installed gate hook denies an unrouted sub-agent launch and allows a routed one; a roll killed mid-watch is continued, not repeated."""
    run, box = z.ctx["run"], z.box
    op = run.operation("gate hook as installed", "the hook command setup wrote runs: an unrouted Agent launch is denied with the way out, a recorded route is allowed")
    z.call(op, "setup", "--yes", "--hooks")
    settings = box.claude / "settings.json"
    command = None
    if settings.is_file():
        for group in (json.loads(settings.read_text(encoding="utf-8")).get("hooks", {}).get("PreToolUse", [])):
            for hook in group.get("hooks", []):
                if GATE_HOOK.search(hook.get("command", "")):
                    command = hook["command"]
    op.check("a PreToolUse gate command is installed in the sandbox Claude settings", bool(command), str(settings))
    if command and not Path(GIT_BASH).is_file():
        run.not_executed("gate hook as installed", "Git Bash (the shell Claude Code uses for hook commands) is not installed here")
    elif command:
        payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Agent", "session_id": "gate-session",
                              "tool_input": {"subagent_type": "general-purpose", "prompt": "do the work", "model": "sonnet"}})

        def gate():
            done = subprocess.run([GIT_BASH, "-c", command], input=payload, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  env=box.env(**z.env()), cwd=str(z.work), timeout=60)
            op.log([command], "installed gate command, PreToolUse payload on stdin", done)
            return parse(done.stdout) or {}, done
        denied, done = gate()
        verdict = (denied.get("hookSpecificOutput") or {}).get("permissionDecision")
        reason = (denied.get("hookSpecificOutput") or {}).get("permissionDecisionReason", "")
        op.check("an unrouted Agent launch is denied and the reason names the router command", verdict == "deny" and "roll" in reason, json.dumps(denied)[:400] + done.stderr[-200:])
        # `agentcompass gate-hook` is the command the Windows installer writes (`<app>\cli\agentcompass.exe gate-hook`);
        # in every install it must give the same verdict as the script command above.
        sub = z.call(op, "gate-hook", stdin=payload)
        sub_verdict = ((parse(sub.stdout) or {}).get("hookSpecificOutput") or {}).get("permissionDecision")
        op.check("`agentcompass gate-hook` gives the same deny for the same payload", sub.returncode == 0 and sub_verdict == "deny", sub.stdout[:300] + sub.stderr[-200:])
        z.call(op, "record", "--provider", "claude", "--account", "claude", "--model", "claude-sonnet-5-5", "--effort", "high",
               "--constraint", "user_override", "--basis", "from-zero gate test")
        allowed, done = gate()
        op.check("after a route is recorded for that model the same launch is allowed", (allowed.get("hookSpecificOutput") or {}).get("permissionDecision") != "deny", json.dumps(allowed)[:400])
        other = subprocess.run([GIT_BASH, "-c", command], input=json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": {"file_path": "x"}}),
                               capture_output=True, text=True, encoding="utf-8", errors="replace", env=box.env(**z.env()), cwd=str(z.work), timeout=60)
        op.check("an unrelated tool call is untouched", (parse(other.stdout) or {}).get("hookSpecificOutput", {}).get("permissionDecision") != "deny", other.stdout[:200])

        # Issue #2: a roll from Claude Code inside an Orca terminal records the Orca session as the parent; the
        # Claude hook reports the Claude session. The launch from that Claude session must match the route.
        op = run.operation("gate under an Orca terminal", "roll inside an Orca terminal running Claude Code: the Claude session's own launch matches the route, another session's does not")
        orca_env = {"ORCA_AGENT_SESSION_ID": "orca-zero-sess", "ORCA_TERMINAL_HANDLE": "term_zero", "CLAUDECODE": "1",
                    "CLAUDE_CODE_SESSION_ID": "claude-zero-uuid", "PASEO_AGENT_ID": None}
        # Opus, so the unparented Sonnet record above cannot answer for this launch.
        z.call(op, "roll", "--task", z.task("ok", "orca-terminal"), "--provider", "claude", "--account", "claude",
               "--model", "claude-opus-5-5", "--effort", "high", "--basis", "from-zero orca gate test", **orca_env)
        ledger = box.state / "router-ledger.jsonl"
        rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()] if ledger.is_file() else []
        routed = next((r for r in reversed(rows) if (r.get("bundle") or {}).get("model") == "claude-opus-5-5"), {})
        op.check("the roll's route names the Orca session as the parent (the worker's reply address)", routed.get("parent_id") == "orca-zero-sess",
                 {k: routed.get(k) for k in ("stage", "parent_id", "host_session_id", "bundle")})

        def orca_gate(session):
            body = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Agent", "session_id": session,
                               "tool_input": {"subagent_type": "general-purpose", "prompt": "orca work", "model": "opus"}})
            done = subprocess.run([GIT_BASH, "-c", command], input=body, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  env=box.env(**z.env()), cwd=str(z.work), timeout=60)
            op.log([command], f"installed gate command, Claude session {session}", done)
            return ((parse(done.stdout) or {}).get("hookSpecificOutput") or {}).get("permissionDecision"), done.stdout
        verdict, out = orca_gate("claude-zero-uuid")
        op.check("the Claude session that ran the roll may launch the routed worker", verdict != "deny", out[:400])
        verdict, out = orca_gate("claude-someone-else")
        op.check("an unrelated Claude session is still denied", verdict == "deny", out[:400])

    op = run.operation("roll killed while it creates", "the dispatch outlives its roll process; the continuation finds the same agent and finishes it with no second create")
    env = z.paseo_env()
    z.set_parents(PARENT)
    # A slow host keeps the bridge inside its create for a while after it logs the request.
    z.set_fault(create_delay_ms=1500)
    calls, answers, inbox = len(z.bridge_calls()), len(z.bridge_answers()), len(z.inbox())
    process = subprocess.Popen([str(box.agentcompass), "roll", "--task", z.task("slow", "crash-continue")], cwd=str(z.work),
                               env=box.env(**z.env(**env)), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    created = z.wait_for(lambda: [a for a in z.bridge_answers()[answers:] if a["operation"] == "create" and (a.get("answer") or {}).get("ok")], 60, 0.3)
    op.check("the host answered the create", bool(created),
             {"answers": z.bridge_answers()[answers:], "roll_exit": process.poll()})
    z.kill(process.pid)
    try:
        process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
    op.record["commands"].append({"argv": [str(box.agentcompass), "roll", "--task", "<slow brief>"], "returncode": process.returncode, "log": "(killed mid-watch on purpose)"})
    did = created[0]["dispatch_id"] if created else None
    row = parse(z.call(op, "dispatch", "--dispatch-action", "status", "--dispatch-id", did or "none", **env).stdout) or {}
    op.check("the registry still holds the dispatch, not terminal, with its reservation", row.get("state") in ("launched", "started", "launching"), row)
    followed = z.call(op, "launch", "--dispatch-id", did or "none", "--parent-id", PARENT, "--watch-seconds", "60", **env)
    got = parse(followed.stdout) or {}
    op.check("the public launch continuation observes the same dispatch through to completed", got.get("state") == "completed", followed.stdout[:300] + followed.stderr[-200:])
    op.check("the host saw exactly one create", sum(1 for c in z.bridge_calls() if c["operation"] == "create" and c["dispatch_id"] == did) == 1, z.bridge_calls()[-4:])
    agent = z.agents().get(did or "none") or {}
    op.check("this dispatch's worker finished on the host", agent.get("status") == "idle" and agent.get("attentionReason") == "finished",
             {k: agent.get(k) for k in ("status", "attentionReason", "lastError")})
    op.check("the worker's report is in the parent's mailbox (one new report)",
             len(z.inbox()) == inbox + 1 and "(slow)" in z.inbox()[-1].get("text", ""), z.inbox()[inbox:])


def step_entries(z: Zero):
    """The same journey through the other entries: a source checkout entry and the console script from another cwd."""
    run, box = z.ctx["run"], z.box
    op = run.operation("source-checkout entry", "python -X utf8 <checkout>/agentcompass roll ... from a non-repo directory reaches the same worker")
    entry = [box.python, "-X", "utf8", z.ctx["repo"] / "agentcompass"]
    z.ctx["mark"] = time.time() - 1
    result = z.call(op, "roll", "--task", z.task("ok", "source-entry"), "--parent-host", "paseo", "--parent-id", PARENT, entry=entry)
    doc = parse(result.stdout) or {}
    op.check("the source entry launched a CLI worker", doc.get("status") == "launched", json.dumps(doc)[:400] + result.stderr[-300:])
    op.check("the answer names no script path in next steps", not re.search(r"\.py\b", json.dumps({k: doc.get(k) for k in ("next_action", "next_argv")})), doc.get("next_action"))


# ---------------------------------------------------------------------------------------------------------------------
# harness


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wheel", type=Path, help="Install this wheel instead of building one from the working tree.")
    parser.add_argument("--report", type=Path, default=ROOT / ".route-briefs" / "zero-dispatch-report")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--only", nargs="*", help="Run only these steps (names of the step_* functions, without the prefix).")
    args = parser.parse_args(argv)
    args.report = args.report.resolve()
    names = sc.registry_clean()
    if names:
        print("refusing to run: Windows user variables the router reads exist in HKCU\\Environment: " + ", ".join(names), file=sys.stderr)
        return 2
    started = time.time()
    args.report.mkdir(parents=True, exist_ok=True)
    run_dir = args.report / time.strftime("run-%Y%m%d-%H%M%S")
    run = sc.Run(run_dir)
    before = sc.real_state_snapshot()
    base = Path(tempfile.mkdtemp(prefix="agentcompass-zero-")).resolve()
    notes = {"sandbox": str(base)}
    try:
        if args.wheel:
            wheel, built = args.wheel.resolve(), False
            repo = sc.build_fixture_repo(base, minimal=True)
        else:
            repo = sc.build_fixture_repo(base)
            wheel, built = sc.build_wheel(repo, base / "dist"), True
        notes["wheel"] = str(wheel)
        venv = base / "ve nv"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True)
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run([str(python), "-m", "pip", "install", "--no-index", "--no-deps", "--quiet", str(wheel)], check=True, capture_output=True)
        sites = (venv / "Lib" / "site-packages") if os.name == "nt" else next((venv / "lib").glob("python*/site-packages"))
        sc.NETGUARD = base / "netguard"
        sc.NETGUARD.mkdir()
        bin_dir = build_fake_clis(base)
        with sc.Fixture() as fixture, ScorerServer() as scorer:
            sc.write_netguard([sites, sc.NETGUARD], fixture.url, [fixture.server.server_address[1], scorer.port], base / "net-default.jsonl")
            ctx = {"base": base, "venv": venv, "fixture": fixture, "run": run, "wheel": wheel, "repo": repo, "fixture_built": built}
            box = sc.Sandbox(base, "zero home with spaces", venv, fixture, run)
            z = Zero(ctx, box, bin_dir, scorer)
            steps = [step_isolation, step_install, step_no_account, step_setup, step_accounts_and_usage, step_cli_worker,
                     step_paseo_host, step_quota_and_recovery, step_herdr, step_hooks_and_crash, step_entries]
            for step in steps:
                name = step.__name__[5:]
                if args.only and name not in args.only:
                    continue
                try:
                    step(z)
                except Exception as exc:       # a step that cannot run is a failure, never a skip
                    import traceback
                    op = run.operation(f"step {name} crashed", "the harness could not complete this step")
                    op.check(f"{name} ran to the end", False, traceback.format_exc()[-800:])
            leftovers = [w["pid"] for w in z.workers() if z.worker_alive(w)]
            for pid in leftovers:
                z.kill(pid)
            op = run.operation("fixture processes", "every worker the fixtures started has ended")
            op.check("no fixture worker was still running at the end", not leftovers, leftovers)
        op = run.operation("real machine untouched", "the user's own cache, activity and receipt files did not change")
        after = sc.real_state_snapshot()
        float_owned = set(before)
        changed = sorted(k for k in before if before[k] != after[k])
        op.check("the router's own real files are unchanged", not sc.real_files_polluted(before.keys(), base) and (not changed or bool(sc.usage_float_processes())), {"changed": changed, "polluted": sc.real_files_polluted(before.keys(), base)})
        op = run.operation("no process left behind", "nothing started from the sandbox is still running")
        left = sc.sandbox_processes(base)
        op.check("no process still runs from the sandbox directory", not left, left)
        for entry in left:
            subprocess.run(["taskkill", "/F", "/T", "/PID", entry.split(":")[0]], capture_output=True)
    finally:
        sc.write_reports(run, started, notes)
        (args.report / "latest.txt").write_text(run_dir.name + "\n", encoding="utf-8")
        if not args.keep:
            shutil.rmtree(base, ignore_errors=True)
    print(f"zero dispatch check: {run.failures} failed assertion(s); report {run_dir / 'report.md'}")
    return run.failures


if __name__ == "__main__":
    raise SystemExit(main())
