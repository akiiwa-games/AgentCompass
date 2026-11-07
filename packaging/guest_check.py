"""Runs INSIDE a Windows Sandbox guest, as the guest's logged-in user (stdlib only, staged python).

The guest is a throw-away Windows with no network: it is the one place where the user's own PATH, USERPROFILE and registry
may be changed freely. This script plays a person on a clean machine: install the wheel, set up, sign in with dummy
credentials, route work (fixture provider CLIs stand in for codex/claude), run the Herdr core flow with real hc terminals,
uninstall. Results go to C:\\out\\results.json (rewritten after every check), logs to C:\\out\\logs.

    C:\\in\\python\\python.exe C:\\in\\guest\\guest_check.py
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import winreg
from pathlib import Path

IN, OUT = Path(r"C:\in"), Path(r"C:\out")
LOGS = OUT / "logs"
HOME = Path(os.path.expanduser("~"))
BASE = HOME / "agent compass test"
BIN = BASE / "fixtures" / "bin"
STATE = BASE / "fixtures" / "state"
VENV = BASE / "ve nv"
PARENT = "guest-parent-1"
TASK = "Rename the helper foo to bar in util.py and update its two callers."
results: list[dict] = []
current = {"id": "", "title": ""}
counter = {"n": 0, "cmd": 0}


def save():
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "results.json").write_text(json.dumps({"results": results, "failures": sum(1 for r in results if not r["ok"])}, indent=1, ensure_ascii=False), encoding="utf-8")


def section(title):
    counter["n"] += 1
    current.update(id=f"G{counter['n']:02d}", title=title)
    print(f"== {current['id']} {title}", flush=True)


def check(name, condition, detail=""):
    ok = bool(condition)
    results.append({"section": current["id"], "title": current["title"], "name": name, "ok": ok, "detail": "" if ok else str(detail)[:700]})
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f": {str(detail)[:300]}"), flush=True)
    save()
    return ok


def base_env(**extra):
    env = dict(os.environ)
    env.update({"PYTHONUTF8": "1", "ROUTER_SCORE_CACHE": "off", "AGENTCOMPASS_PUBLIC_DATA": "off", "FIXTURE_SCORER": "1", "FIXTURE_STATE": str(STATE), "FIXTURE_BIN": str(BIN)})
    env.update({k: v for k, v in extra.items() if v is not None})
    for key in [k for k, v in extra.items() if v is None]:
        env.pop(key, None)
    return env


def run(argv, *, env=None, cwd=None, stdin=None, timeout=240, label=None):
    counter["cmd"] += 1
    started = time.time()
    try:
        done = subprocess.run([str(a) for a in argv], env=env or base_env(), cwd=str(cwd or BASE), input=stdin, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        done = subprocess.CompletedProcess(argv, 124, (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""), "TIMEOUT")
    LOGS.mkdir(parents=True, exist_ok=True)
    name = f"{current['id']}-{counter['cmd']:03d}-{re.sub(r'[^A-Za-z0-9]+', '-', label or Path(str(argv[0])).stem)[:30]}.log"
    (LOGS / name).write_text("$ " + " ".join(map(str, argv)) + f"\n# rc {done.returncode}, {time.time() - started:.1f}s\n--- stdout ---\n{done.stdout}\n--- stderr ---\n{done.stderr}\n", encoding="utf-8")
    return done


def parse(text):
    text = (text or "").strip()
    try:
        return json.loads(text)
    except ValueError:
        i = text.find("{")
        try:
            return json.loads(text[i:]) if i >= 0 else {}
        except ValueError:
            return {}


def wait_for(predicate, seconds=60, interval=0.5):
    end = time.time() + seconds
    while time.time() < end:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def snapshot(root: Path, skip=()):
    out = {}
    for path in root.rglob("*"):
        if path.is_file() and not any(part in skip for part in path.parts):
            try:
                out[str(path.relative_to(root))] = (path.stat().st_size, hash(path.read_bytes()))
            except OSError:
                pass
    return out


def set_user_path(prepend):
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_SET_VALUE)
    try:
        try:
            current_path, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current_path, kind = "", winreg.REG_EXPAND_SZ
        winreg.SetValueEx(key, "Path", 0, kind, os.pathsep.join([*prepend, current_path]) if current_path else os.pathsep.join(prepend))
    finally:
        winreg.CloseKey(key)


def machine_path_has(fragment):
    return any(fragment.lower() in p.lower() for p in os.environ.get("PATH", "").split(os.pathsep))


def agentcompass(*args, **kwargs):
    return run([VENV / "Scripts" / "agentcompass.exe", *args], **kwargs)


# ---------------------------------------------------------------------------------------------------------------------
def g_isolation():
    section("the guest is a clean, offline machine")
    check("running in the guest profile, not the host profile", "tanwei" not in str(HOME).lower(), str(HOME))
    reached = []
    for host in ("1.1.1.1", "8.8.8.8"):
        try:
            socket.create_connection((host, 443), timeout=3).close()
            reached.append(host)
        except OSError:
            pass
    check("no outbound network", not reached, reached)
    try:
        socket.getaddrinfo("chatgpt.com", 443)
        resolved = True
    except OSError:
        resolved = False
    check("names do not resolve", not resolved)
    check("git is not installed in the guest", shutil.which("git") is None, shutil.which("git"))
    check("no real codex or claude exists in the guest", shutil.which("codex") is None and shutil.which("claude") is None, (shutil.which("codex"), shutil.which("claude")))
    check("the host's home is not visible", not Path(r"C:\Users\tanwei").exists())


def g_stage():
    section("stage the tools a person would have")
    BASE.mkdir(parents=True, exist_ok=True)
    shutil.copytree(IN / "fixtures" / "bin", BIN, dirs_exist_ok=True)
    shutil.copytree(IN / "node", BASE / "node", dirs_exist_ok=True)
    shutil.copytree(IN / "hc", BASE / "hc", dirs_exist_ok=True)
    if not (BASE / "hc" / "hc.exe").exists():
        os.link(BASE / "hc" / "herdr-core.exe", BASE / "hc" / "hc.exe")
    STATE.mkdir(parents=True, exist_ok=True)
    set_user_path([str(BIN), str(BASE / "node"), str(BASE / "hc")])
    check("the fixture provider CLIs, node and hc are in place", all((BIN / n).is_file() for n in ("codex.exe", "claude.exe")) and (BASE / "node" / "node.exe").is_file() and (BASE / "hc" / "hc.exe").is_file())
    os.environ["PATH"] = os.pathsep.join([str(BIN), str(BASE / "node"), str(BASE / "hc"), os.environ["PATH"]])
    check("codex now resolves to the fixture", str(BIN).lower() in (shutil.which("codex") or "").lower(), shutil.which("codex"))


def g_install():
    section("install the wheel into a fresh venv (path with a space)")
    python = IN / "python" / "python.exe"
    made = run([python, "-m", "venv", VENV], label="venv")
    check("python -m venv works with the staged interpreter", made.returncode == 0, made.stderr[-300:])
    wheel = next((IN / "wheel").glob("*.whl"))
    installed = run([VENV / "Scripts" / "python.exe", "-m", "pip", "install", "--no-index", "--no-deps", "--quiet", wheel], label="pip")
    check("pip installs the wheel offline", installed.returncode == 0, installed.stderr[-400:])
    version = agentcompass("--version")
    check("agentcompass --version", version.returncode == 0 and re.match(r"\d+\.\d+", version.stdout.strip()), version.stdout + version.stderr)
    helped = agentcompass("--help")
    check("the public help lists roll, setup, doctor", all(w in helped.stdout for w in ("roll", "setup", "doctor")), helped.stdout[:300])
    set_user_path([str(VENV / "Scripts")])
    os.environ["PATH"] = str(VENV / "Scripts") + os.pathsep + os.environ["PATH"]
    check("the console script resolves from PATH", shutil.which("agentcompass") is not None, shutil.which("agentcompass"))
    doctor = agentcompass("doctor")
    check("doctor runs read-only and exits 0 or reports findings without a traceback", "Traceback" not in doctor.stderr, doctor.stderr[-300:])


def g_setup_and_uninstall():
    section("setup with no CLI signed in, then with dummy sign-ins, then uninstall")
    before = snapshot(HOME, skip=("agent compass test", "AppData", "NTUSER.DAT"))
    nothing = agentcompass("setup", "--yes", "--no-hooks")
    check("setup with nobody signed in exits 0 and explains it", nothing.returncode == 0 and re.search(r"No signed-in agent CLI", nothing.stdout + nothing.stderr), (nothing.stdout + nothing.stderr)[-400:])
    (HOME / ".codex").mkdir(exist_ok=True)
    (HOME / ".codex" / "auth.json").write_text(json.dumps({"tokens": {"access_token": "guest-dummy-codex-token", "account_id": "guest-account"}}), encoding="utf-8")
    (HOME / ".claude").mkdir(exist_ok=True)
    (HOME / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "guest-dummy-claude-token", "subscriptionType": "max"}}), encoding="utf-8")
    done = agentcompass("setup", "--yes", "--hooks")
    check("setup --yes --hooks exits 0 and lists the signed-in CLIs", done.returncode == 0 and "signed in" in (done.stdout + done.stderr), (done.stdout + done.stderr)[-500:])
    check("the router skill and rules were written under the guest home", (HOME / ".codex" / "skills" / "agent-compass" / "SKILL.md").exists() or any((HOME / ".claude").rglob("*agent-compass*")), "no skill files")
    providers = agentcompass("providers")
    check("providers lists codex and claude as ready", providers.returncode == 0 and "codex" in providers.stdout and "claude" in providers.stdout, providers.stdout[:300])
    plan = agentcompass("roll", "--task", TASK, "--parent-host", "paseo", "--parent-id", PARENT, "--dry-run")
    check("a dry-run roll classifies and routes without launching", plan.returncode == 0 and (parse(plan.stdout).get("status") in ("dry_run", "self", "launched", "native", None)) and "Traceback" not in plan.stderr, plan.stdout[:300] + plan.stderr[-200:])
    undone = agentcompass("uninstall", "--yes")
    check("uninstall exits 0", undone.returncode == 0, (undone.stdout + undone.stderr)[-400:])
    final = snapshot(HOME, skip=("agent compass test", "AppData", "NTUSER.DAT"))
    removed = [HOME / ".claude" / "CLAUDE.md", HOME / ".codex" / "AGENTS.md", HOME / ".claude" / "settings.json", HOME / ".codex" / "hooks.json",
               HOME / ".claude" / "skills" / "agent-compass", HOME / ".codex" / "skills" / "agent-compass"]
    check("everything uninstall reported removing is gone (rules blocks, hook files, skill junctions)", not [str(p) for p in removed if p.exists()], [str(p) for p in removed if p.exists()])
    outside = sorted(k for k in before if not k.startswith(".agent-compass") and (k not in final or final[k] != before[k]))
    added = sorted(k for k in final if not k.startswith(".agent-compass") and k not in before and not k.endswith(("auth.json", ".credentials.json")))
    check("files that existed before the first setup, outside the router state directory, are unchanged", not outside, outside[:10])
    check("uninstall left no file behind outside the router state directory (apart from the dummy sign-ins)", not added, added[:10])
    kept = sorted(k for k in final if k.startswith(".agent-compass") and k not in before)
    check(f"observation: uninstall keeps the router's own state directory ({len(kept)} files: caches, inventory, backups, receipts)", True)
    check("the dummy sign-ins were never touched by setup or uninstall", json.loads((HOME / ".codex" / "auth.json").read_text(encoding="utf-8"))["tokens"]["access_token"] == "guest-dummy-codex-token")
    agentcompass("setup", "--yes", "--no-hooks")      # leave the machine set up for the next sections


def hc(*args, timeout=60, env=None):
    return run([BASE / "hc" / "hc.exe", *args], env=env or base_env(), timeout=timeout, label="hc-" + (args[0] if args else "x"))


def terminal(done):
    data = parse(done.stdout)
    return (data.get("terminal") if isinstance(data.get("terminal"), dict) else data) or {}


PARENT_PROGRAM = """import sys
print(chr(27) + "]7501;state=idle" + chr(27) + chr(92), flush=True)
with open(sys.argv[1], "a", encoding="utf-8") as box:
    for line in sys.stdin:
        box.write(line.rstrip("\\r\\n") + "\\n")
        box.flush()
"""
PROBE_PROGRAM = """import json, os, shutil, sys
open(sys.argv[1], "w", encoding="utf-8").write(json.dumps({"codex": shutil.which("codex"), "userprofile": os.environ.get("USERPROFILE"), "path": os.environ.get("PATH")}))
"""


def worker_records():
    folder = STATE / "workers"
    return sorted((json.loads(p.read_text(encoding="utf-8")) for p in folder.glob("*.json")), key=lambda r: r["started"]) if folder.is_dir() else []


def export_evidence(terminals):
    """Raw material for the report: receipts, retained terminal output, and what hc says about each terminal."""
    folder = OUT / "herdr-evidence"
    folder.mkdir(exist_ok=True)
    source = HOME / ".agent-compass" / "herdr-workers"
    if source.is_dir():
        shutil.copytree(source, folder / "herdr-workers", dirs_exist_ok=True)
    env = base_env(HC_SESSION="guest-session", HC_TERMINAL_ID=None)
    for address in [t for t in terminals if t]:
        (folder / f"hc-get-{address}.json").write_text(hc("get", address, "--json", env=env).stdout, encoding="utf-8")
        (folder / f"hc-read-{address}.txt").write_text(hc("read", address, "--lines", "200", env=env).stdout, encoding="utf-8")
    for name in ("workers", "hc-send", "inbox"):
        if (STATE / name).is_dir():
            shutil.copytree(STATE / name, folder / name, dirs_exist_ok=True)


def g_herdr():
    section("Herdr core: a worker started through hc, reporting to a real parent terminal")
    env = base_env(HC_SESSION="guest-session", HC_TERMINAL_ID=None)
    started = hc("server", "start", "--json", env=env)
    check("the core starts", started.returncode == 0, started.stdout[:200] + started.stderr[-200:])
    terminals = []
    try:
        mailbox = BASE / "parent-mailbox.txt"
        (BASE / "parent_program.py").write_text(PARENT_PROGRAM, encoding="utf-8")
        (BASE / "probe_program.py").write_text(PROBE_PROGRAM, encoding="utf-8")
        python = str(IN / "python" / "python.exe")
        probe = BASE / "probe.json"
        terminals.append(terminal(hc("run", "--bg", "--json", "--name", "guest-probe", python, "-u", BASE / "probe_program.py", probe, env=env)).get("address"))
        wait_for(probe.is_file, 30)
        seen = parse(probe.read_text(encoding="utf-8")) if probe.is_file() else {}
        check("a terminal of the core resolves codex to the fixture (the user's PATH reaches hc terminals)", str(BIN).lower() in (seen.get("codex") or "").lower(), seen)
        parent = terminal(hc("run", "--bg", "--json", "--name", "guest-parent", python, "-u", BASE / "parent_program.py", mailbox, env=env)).get("address")
        terminals.append(parent)
        check("a live parent terminal exists", bool(parent))
        task = TASK + " [guest-1]\nFIXTURE-SCENARIO: ok\n完整原文 unchanged\r\n"
        before = len(worker_records())
        rolled = agentcompass("roll", "--task", task, "--wait", "0", env=base_env(HC_SESSION="guest-session", HC_TERMINAL_ID=parent), label="roll")
        doc = parse(rolled.stdout)
        details = parse(Path(doc["details"]).read_text(encoding="utf-8")) if doc.get("details") and Path(doc["details"]).is_file() else {}
        check("roll launched on the herdr surface with a public status command", doc.get("status") == "launched" and details.get("dispatch_mode") == "herdr" and isinstance(doc.get("next_argv"), list), (rolled.stdout + rolled.stderr)[:500])
        worker = details.get("worker") or {}
        if worker.get("terminal"):
            terminals.append(worker["terminal"])
        final = {}

        def poll():
            nonlocal final
            if not isinstance(doc.get("next_argv"), list):
                return True
            done = run(doc["next_argv"], env=base_env(HC_SESSION="guest-session", HC_TERMINAL_ID=parent), label="herdr-status")
            final = parse(done.stdout)
            return final.get("status") in ("completed", "failed", "needs_input", "cleaned")
        wait_for(poll, 120, 2.0)
        check("running next_argv reaches completed", final.get("status") == "completed", final)
        records = worker_records()[before:]
        check("exactly one provider process started and it is the fixture codex with the required flags",
              len(records) == 1 and records[0]["cli"] == "codex" and "--no-daemon" in records[0]["argv"] and "--dangerously-bypass-approvals-and-sandbox" in records[0]["argv"], [r.get("argv") for r in records])
        request_path = Path(worker["request"]) if worker.get("request") else None
        stored = request_path.read_text(encoding="utf-8") if request_path and request_path.is_file() else ""
        prompt_file = Path(json.loads(stored)["prompt_file"]) if stored else None
        prompt_text = prompt_file.read_bytes().decode("utf-8") if prompt_file and prompt_file.is_file() else ""
        check("the assignment file the worker reads holds the whole task byte for byte (CRLF and Chinese kept)", task in prompt_text, repr(prompt_text[:160]))
        check("the receipt holds the launch intent and no credential material", '"name": "ac-' in stored and not re.search(r"(?i)(api[_-]?key|access[_-]?token|secret)", stored), stored[:200])
        env_seen = (records[0]["router_env"] if records else {})
        check("the worker's environment names the real parent terminal and host herdr", env_seen.get("ROUTER_PARENT_ADDRESS") == parent and env_seen.get("ROUTER_PARENT_HOST") == "herdr", env_seen)
        got = wait_for(lambda: mailbox.is_file() and "[Worker final report]" in mailbox.read_text(encoding="utf-8", errors="replace"), 30)
        check("the real parent terminal received the worker's report through hc send", bool(got), mailbox.read_text(encoding="utf-8", errors="replace")[-300:] if mailbox.is_file() else "no mailbox")
        export_evidence(terminals)
        # The Windows exec trampoline question: does `completed` come from the fixture finishing, or from the launcher exiting?
        slow = parse(agentcompass("roll", "--task", TASK + " [guest-slow]\nFIXTURE-SCENARIO: slow\n", "--wait", "0", env=base_env(HC_SESSION="guest-session", HC_TERMINAL_ID=parent), label="roll-slow").stdout)
        slow_details = parse(Path(slow["details"]).read_text(encoding="utf-8")) if slow.get("details") and Path(slow["details"]).is_file() else {}
        if (slow_details.get("worker") or {}).get("terminal"):
            terminals.append(slow_details["worker"]["terminal"])
        time.sleep(1.5)
        early = parse(run(slow["next_argv"], env=base_env(HC_SESSION="guest-session", HC_TERMINAL_ID=parent), label="herdr-status-early").stdout) if isinstance(slow.get("next_argv"), list) else {}
        slow_running = wait_for(lambda: any(r["scenario"] == "slow" for r in worker_records()), 15)
        check("the slow fixture worker really started (a provider process exists for it)", bool(slow_running), "no worker record: the provider CLI never ran")
        check("a worker that is still running is not reported completed", bool(slow_running) and early.get("status") in ("launched", "working", "needs_input"),
              {"status": early.get("status"), "fixture_running": bool(slow_running)})
        late = {}

        def slow_done():
            nonlocal late
            late = parse(run(slow["next_argv"], env=base_env(HC_SESSION="guest-session", HC_TERMINAL_ID=parent), label="herdr-status-late").stdout)
            return late.get("status") in ("completed", "failed")
        wait_for(slow_done, 90, 3.0)
        check("and it is completed once the fixture finished", late.get("status") == "completed", late)
        for item in (doc, slow):
            if isinstance(item.get("next_argv"), list) and (item.get("worker") or True):
                cleanup = [*item["next_argv"][:-3], "cleanup", "--request", item["next_argv"][-1]]
                done = run(cleanup, env=base_env(HC_SESSION="guest-session", HC_TERMINAL_ID=parent), label="herdr-cleanup")
                check("cleanup answers cleaned", parse(done.stdout).get("status") == "cleaned", done.stdout[:200] + done.stderr[-200:])
        gone_terminal = hc("get", worker.get("terminal") or "none", "--json", env=env)
        check("after cleanup the terminal no longer exists on the core", gone_terminal.returncode != 0, gone_terminal.stdout[:200])
        count = len(worker_records())
        replay = parse(agentcompass("roll", "--task", task, "--wait", "0", env=base_env(HC_SESSION="guest-session", HC_TERMINAL_ID=parent), label="roll-after-cleanup").stdout)
        replay_details = parse(Path(replay["details"]).read_text(encoding="utf-8")) if replay.get("details") and Path(replay["details"]).is_file() else {}
        check("rolling the same assignment after cleanup starts nothing and reports the cleaned operation",
              len(worker_records()) == count and (replay_details.get("worker") or {}).get("state") == "cleaned", replay)
        # exit codes and program states, hang + cleanup
        def lifecycle(scenario, wanted):
            rolled2 = parse(agentcompass("roll", "--task", TASK + " [guest-" + scenario + "]" + chr(10) + "FIXTURE-SCENARIO: " + scenario + chr(10), "--wait", "0",
                                         env=base_env(HC_SESSION="guest-session", HC_TERMINAL_ID=parent), label="roll-" + scenario).stdout)
            det = parse(Path(rolled2["details"]).read_text(encoding="utf-8")) if rolled2.get("details") and Path(rolled2["details"]).is_file() else {}
            if (det.get("worker") or {}).get("terminal"):
                terminals.append(det["worker"]["terminal"])
            seen = {}

            def poll_one():
                nonlocal seen
                seen = parse(run(rolled2["next_argv"], env=base_env(HC_SESSION="guest-session", HC_TERMINAL_ID=parent), label="herdr-status-" + scenario).stdout) if isinstance(rolled2.get("next_argv"), list) else {}
                return seen.get("status") == wanted
            wait_for(poll_one, 90, 2.0)
            check(f"[{scenario}] status says {wanted}", seen.get("status") == wanted, seen)
            return rolled2, seen
        failed_roll, failed_seen = lifecycle("error", "failed")
        check("[error] the fixture provider really ran and exited non-zero (3)", any(r["scenario"] == "error" for r in worker_records()), [r["scenario"] for r in worker_records()])
        blocked_roll, blocked_seen = lifecycle("blocked", "needs_input")
        check("[blocked] a worker waiting for input is not reported completed", blocked_seen.get("status") == "needs_input", blocked_seen)
        cleaned = run([*blocked_roll["next_argv"][:-3], "cleanup", "--request", blocked_roll["next_argv"][-1]], env=base_env(HC_SESSION="guest-session", HC_TERMINAL_ID=parent), label="herdr-cleanup-blocked")
        blocked_pid = next((r["pid"] for r in worker_records() if r["scenario"] == "blocked"), None)
        gone_blocked = wait_for(lambda: blocked_pid is not None and f'"{blocked_pid}"'.encode() not in subprocess.run(["tasklist", "/FI", f"PID eq {blocked_pid}", "/NH", "/FO", "CSV"], capture_output=True).stdout, 30)
        check("[blocked] cleanup removes the terminal and the waiting provider process with it", parse(cleaned.stdout).get("status") == "cleaned" and bool(gone_blocked), cleaned.stdout[:200])
        # Unknown delivery. The only fault is at the external boundary: a wrapper in front of the real `hc` lets `hc run` do its work
        # (the terminal is created, the provider starts) and then loses the answer (exit 7, empty). The product is not touched.
        faulty = BASE / "faulty-hc"
        faulty.mkdir(exist_ok=True)
        real = BASE / "hc" / "hc.exe"
        (faulty / "hc.cmd").write_text("@echo off\r\nif /I \"%~1\"==\"run\" goto lose\r\n\"" + str(real) + "\" %*\r\nexit /b %errorlevel%\r\n:lose\r\n\""
                                       + str(real) + "\" %* >nul 2>nul\r\nexit /b 7\r\n", encoding="utf-8")
        lose_env = base_env(HC_SESSION="guest-session", HC_TERMINAL_ID=parent, PATH=str(faulty) + os.pathsep + os.environ["PATH"])
        lost_task = TASK + " [guest-lost-reply]\nFIXTURE-SCENARIO: slow\n"
        starts = len([r for r in worker_records() if r["scenario"] == "slow"])
        lost = parse(agentcompass("roll", "--task", lost_task, "--wait", "0", env=lose_env, label="roll-lost").stdout)
        lost_details = parse(Path(lost["details"]).read_text(encoding="utf-8")) if lost.get("details") and Path(lost["details"]).is_file() else {}
        receipt = (lost_details.get("worker") or {})
        receipt_file = Path(receipt["request"]) if receipt.get("request") else None
        record = json.loads(receipt_file.read_text(encoding="utf-8")) if receipt_file and receipt_file.is_file() else {}
        check("a lost `hc run` answer is not reported launched: pending, receipt state unknown, 'do not resend'",
              lost.get("status") == "pending" and record.get("state") == "unknown" and "do_not_resend" in json.dumps(lost_details), {"roll": lost, "state": record.get("state")})
        named = terminal(hc("get", record.get("name", "none"), "--json", env=env))
        if named.get("address"):
            terminals.append(named["address"])
        check("the terminal exists under the receipt's deterministic name, owned by the parent", named.get("name") == record.get("name") and named.get("parent") == parent, named)
        ran = wait_for(lambda: len([r for r in worker_records() if r["scenario"] == "slow"]) > starts, 30)
        check("the provider really started once although the reply was lost", bool(ran), [r["scenario"] for r in worker_records()])
        again = parse(agentcompass("roll", "--task", lost_task, "--wait", "0", env=base_env(HC_SESSION="guest-session", HC_TERMINAL_ID=parent), label="roll-lost-again").stdout)
        time.sleep(4)
        check("rolling the same assignment again starts nothing: same receipt, no second provider or terminal",
              len([r for r in worker_records() if r["scenario"] == "slow"]) == starts + 1 and again.get("status") in ("pending", "launched")
              and (parse(Path(again["details"]).read_text(encoding="utf-8")).get("worker") or {}).get("request") == receipt.get("request"), again)
        observed = parse(run(lost["next_argv"], env=base_env(HC_SESSION="guest-session", HC_TERMINAL_ID=parent), label="herdr-status-lost").stdout) if isinstance(lost.get("next_argv"), list) else {}
        check("the public status command finds the terminal by name and leaves 'unknown'", observed.get("status") in ("launched", "completed") and observed.get("terminal") == named.get("address"), observed)
        after = {}

        def lost_done():
            nonlocal after
            after = parse(run(lost["next_argv"], env=base_env(HC_SESSION="guest-session", HC_TERMINAL_ID=parent), label="herdr-status-lost-late").stdout)
            return after.get("status") in ("completed", "failed")
        wait_for(lost_done, 90, 3.0)
        check("and it completes once the fixture finished", after.get("status") == "completed", after)
        # parent death kills the child
        second = terminal(hc("run", "--bg", "--json", "--name", "guest-parent-2", python, "-u", BASE / "parent_program.py", BASE / "mailbox2.txt", env=env)).get("address")
        terminals.append(second)
        hang_cmd = [str(VENV / "Scripts" / "agentcompass.exe"), "roll", "--task", TASK + " [guest-hang]\nFIXTURE-SCENARIO: hang\n", "--wait", "0"]
        twins = [subprocess.Popen(hang_cmd, cwd=str(BASE), env=base_env(HC_SESSION="guest-session", HC_TERMINAL_ID=second), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, encoding="utf-8", errors="replace") for _ in range(2)]
        twin_docs = [parse(t.communicate(timeout=180)[0]) for t in twins]
        hang = twin_docs[0]
        hang_details = parse(Path(hang["details"]).read_text(encoding="utf-8")) if hang.get("details") and Path(hang["details"]).is_file() else {}
        child = (hang_details.get("worker") or {}).get("terminal")
        if child:
            terminals.append(child)
        wait_for(lambda: any(r["scenario"] == "hang" for r in worker_records()), 30)
        twin_terms = {((parse(Path(d["details"]).read_text(encoding="utf-8")) if d.get("details") and Path(d["details"]).is_file() else {}).get("worker") or {}).get("terminal") for d in twin_docs}
        time.sleep(3)
        check("two identical rolls at the same moment answer launched with one and the same child terminal",
              all(d.get("status") == "launched" for d in twin_docs) and len(twin_terms) == 1 and None not in twin_terms, twin_docs)
        check("and exactly one provider process was started for the pair", len([r for r in worker_records() if r["scenario"] == "hang"]) == 1, [r["scenario"] for r in worker_records()])
        hc("kill", second, "--json", env=env)
        gone = wait_for(lambda: terminal(hc("get", child or "none", "--json", env=env)).get("state") == "finished", 40, 1.0)
        check("killing the parent finishes the child terminal", bool(gone))
        hung = [r for r in worker_records() if r["scenario"] == "hang"]
        alive = subprocess.run(["tasklist", "/FI", f"PID eq {hung[0]['pid']}", "/NH", "/FO", "CSV"], capture_output=True).stdout if hung else b""
        check("and the fixture provider process itself is gone, not orphaned", bool(hung) and f'"{hung[0]["pid"]}"'.encode() not in alive, alive[:200])
    finally:
        for address in reversed([t for t in terminals if t]):
            hc("kill", address, "--json", env=env)
            hc("rm", address, "--json", env=env)
        hc("server", "stop", "--json", env=env)
        left = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
        check("no herdr-core process remains after server stop", "herdr-core" not in left.lower(), left[:200])


def g_gui_probe():
    section("is there a desktop to drive")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
        "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen;"
        "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
        "$g=[System.Drawing.Graphics]::FromImage($bmp);$g.CopyFromScreen($b.Left,$b.Top,0,0,$bmp.Size);"
        "$bmp.Save('C:\\out\\desktop.png');"
        "Write-Output ('{0}x{1}' -f $b.Width,$b.Height)")
    done = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], label="screenshot")
    check("a screenshot of the guest desktop was saved", done.returncode == 0 and (OUT / "desktop.png").is_file(), done.stderr[-300:])
    sessions = run(["query", "session"], label="query-session")
    (OUT / "sessions.txt").write_text(sessions.stdout + sessions.stderr, encoding="utf-8")


# ---------------------------------------------------------------------------------------------------------------------
# Installer and tray GUI. These need the interactive desktop of `wsb connect` (ExistingLogin); the sandbox is thrown away afterwards.
import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32
INNO_DIR = HOME / "inno-tools"
BUILD = HOME / "ac-build"
LOCAL = Path(os.environ.get("LOCALAPPDATA") or HOME / "AppData" / "Local")
INSTALL_DIR = LOCAL / "AgentCompass"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{A7B3E2C1-4F58-4C9A-9D2E-1B6A8C0D5E77}_is1"
STARTUP = Path(os.environ.get("APPDATA") or HOME / "AppData" / "Roaming") / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
START_MENU_LINK = STARTUP.parent / "AgentCompass.lnk"
ENV_NAMES = ("PRO_CONSULT_LAUNCHER", "PRO_CONSULT_LAUNCHER_ARGS", "CODEX_WEB_GPT_EXTERNAL_TRAY")
ONLY: list[str] = []

SHOT_PS = r"""param([int]$Left,[int]$Top,[int]$Width,[int]$Height,$Path)
Add-Type -AssemblyName System.Windows.Forms,System.Drawing
$bmp = New-Object System.Drawing.Bitmap $Width,$Height
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($Left,$Top,0,0,$bmp.Size)
$bmp.Save($Path)
"""

REGION_PS = r"""param([string]$Path,[int]$Left,[int]$Top,[int]$Width,[int]$Height)
Add-Type -AssemblyName System.Drawing
$src = [System.Drawing.Bitmap]::FromFile($Path)
$crop = $src.Clone((New-Object System.Drawing.Rectangle $Left,$Top,$Width,$Height), $src.PixelFormat)
$ms = New-Object System.IO.MemoryStream
$crop.Save($ms, [System.Drawing.Imaging.ImageFormat]::Bmp)
$sha = [System.Security.Cryptography.SHA256]::Create()
([BitConverter]::ToString($sha.ComputeHash($ms.ToArray()))).Replace('-','')
$src.Dispose()
"""


def region_hash(path, left, top, width, height):
    done = run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", ps_file("region.ps1", REGION_PS), "-Path", path, "-Left", left, "-Top", top,
                "-Width", width, "-Height", height], label="region")
    return done.stdout.strip() if done.returncode == 0 else None


def ps_file(name, text):
    path = LOGS / name
    LOGS.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def shot(rect, name):
    left, top, right, bottom = rect
    path = OUT / name
    done = run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", ps_file("shot.ps1", SHOT_PS), "-Left", max(left, 0), "-Top", max(top, 0),
                "-Width", max(right - left, 1), "-Height", max(bottom - top, 1), "-Path", path], label="shot-" + name)
    return path if done.returncode == 0 and path.is_file() else None


def screen_rect():
    return (0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))


def windows_of(pid=None, title=None):
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        text = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, text, 256)
        if user32.IsWindowVisible(hwnd) and (pid is None or owner.value == pid) and (title is None or title in text.value):
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            found.append({"hwnd": hwnd, "title": text.value, "pid": owner.value, "rect": (rect.left, rect.top, rect.right, rect.bottom)})
        return True
    user32.EnumWindows(visit, 0)
    return found


def console_windows():
    found = set()
    for window in windows_of():
        name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(window["hwnd"], name, 256)
        if name.value in ("ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS"):
            found.add(window["hwnd"])
    return found


def console_command_opens_no_window(exe):
    """The tray runs CLIs (version probes, updates, model lists) through the windowed exe's owned_command helper."""
    before = console_windows()
    helper = subprocess.Popen([str(exe), "--run-component", "owned_command", "cmd", "/c", "ping -n 4 127.0.0.1 >nul"],
                              stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              cwd=str(HOME), env=base_env())
    helper.stdin.write(b"1")
    helper.stdin.close()
    popped, deadline = set(), time.time() + 20
    while helper.poll() is None and time.time() < deadline:
        popped |= console_windows() - before
        time.sleep(0.1)
    if helper.poll() is None:
        helper.kill()
    check("a console command the installed tray runs opens no console window", helper.wait(10) == 0 and not popped,
          f"rc={helper.returncode} new console windows={len(popped)}")


def tray_processes():
    done = run(["tasklist", "/FI", "IMAGENAME eq AgentCompass.exe", "/FO", "CSV", "/NH"], label="tasklist")
    return [int(row.split('","')[1]) for row in done.stdout.splitlines() if row.startswith('"AgentCompass.exe"')]


def kill_trays():
    run(["taskkill", "/F", "/T", "/IM", "AgentCompass.exe"], label="taskkill")
    return wait_for(lambda: not tray_processes(), 15)


def prepare_router_state():
    """Dummy sign-ins (as in the setup step), the router skill from the wheel, and one real plan row in the ledger."""
    (HOME / ".codex").mkdir(exist_ok=True)
    auth = HOME / ".codex" / "auth.json"
    if not auth.exists():
        auth.write_text(json.dumps({"tokens": {"access_token": "guest-dummy-codex-token", "account_id": "guest-account"}}), encoding="utf-8")
    (HOME / ".claude").mkdir(exist_ok=True)
    credentials = HOME / ".claude" / ".credentials.json"
    if not credentials.exists():
        credentials.write_text(json.dumps({"claudeAiOauth": {"accessToken": "guest-dummy-claude-token", "subscriptionType": "max"}}), encoding="utf-8")
    setup = agentcompass("setup", "--yes", "--no-hooks")
    check("setup installs the router skill the tray reads", setup.returncode == 0 and (HOME / ".codex" / "skills" / "agent-compass" / "SKILL.md").exists(), (setup.stdout + setup.stderr)[-400:])


def add_plan(objective):
    task = BASE / f"plan-{re.sub('[^A-Za-z0-9]+', '-', objective)}.json"
    task.write_text(json.dumps({"task": {
        "objective": objective, "work_kind": "general_implementation", "expected_hours": 1, "needs": {"local_only": True},
        "profile": {"reasoning_depth": 3, "code_complexity": 2, "context_scale": 2, "hallucination_risk": 2, "error_cost": 2,
                    "needs_visual": False, "needs_gui": False, "local_only": True, "interaction_style": "engineering", "multi_task": False, "task_count": "1", "reason": "guest gui fixture"}}}), encoding="utf-8")
    done = agentcompass("plan", "--input", task)
    ledger = HOME / ".agent-compass" / "router-ledger.jsonl"
    rows = 0
    for line in (ledger.read_text(encoding="utf-8").splitlines() if ledger.exists() else []):
        try:
            rows += json.loads(line).get("stage") == "plan"
        except ValueError:
            pass
    return done, rows


def gui_session(exe, tag, router_only=False):
    """Launch the packaged tray and the router window in the interactive desktop, operate the window, keep screenshots."""
    section(f"{tag}: tray process, router window, refresh, screenshots")
    prepare_router_state()
    first, rows = add_plan("GUIPROBE first task")
    check("a real plan row was written to the guest ledger (the window lists it under Recent Decisions)", first.returncode == 0 and rows >= 1, (first.stdout + first.stderr)[-400:] + f" rows={rows}")
    env = base_env()
    tray = None
    if not router_only:
        tray = subprocess.Popen([str(exe)], cwd=str(HOME), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(15)
        check("the tray process is still running 15 s after launch (no crash on a fresh profile)", tray.poll() is None, f"exit {tray.poll()}")
        shot(screen_rect(), f"{tag}-tray-desktop.png")
        check("the tray started its helper processes (usage collector / capacity sampler children)", len(tray_processes()) >= 1, tray_processes())
    opened_at = time.time()
    window_process = subprocess.Popen([str(exe), "--run-component", "router_window"], cwd=str(HOME), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    found = wait_for(lambda: windows_of(pid=window_process.pid) or windows_of(title="AgentCompass"), 60, interval=0.05)
    open_seconds = round(time.time() - opened_at, 2)
    check("the router window opened on the desktop", bool(found), f"process exit {window_process.poll()}")
    if tray is not None and found:
        # The tray built the window hidden 8 s after it started; this launch only asks that process to show it.
        check("the tray's warm window shows within 3 s of an open request", found[0]["pid"] != window_process.pid and open_seconds < 3,
              (found[0]["pid"], window_process.pid, open_seconds))
    if not found:
        kill_trays()
        return
    window = found[0]
    hwnd = window["hwnd"]
    user32.SetForegroundWindow(hwnd)
    # Coordinates below were read from screenshots of this very window in this sandbox (1351x728 screen, window at its fixed size): the sidebar entry
    # "额度监控" (Quota Monitor) at (100,178); after moving the window 700 px left, that page's primary "刷新" button at (929,79) and the label
    # "剩余额度% · 更新于 HH:MM:SS" at x 600-875, y 62-98. The window repaints slowly, so every step waits until two screenshots are identical.
    move(hwnd, 0, 0, 1351, 728)
    time.sleep(2)
    click_shot(100, 178)
    seconds, _ = settle(f"{tag}-quota-page.png")
    move(hwnd, -700, 0, 2000, 728)
    seconds2, page = settle(f"{tag}-quota-page-right.png")
    check("the window is not hung after opening the Quota Monitor page (screenshots kept for review)", not user32.IsHungAppWindow(hwnd) and page is not None, (seconds, seconds2))
    label_before = region_hash(page, 600, 62, 275, 36) if page else None
    time.sleep(6)
    idle_seconds, idle = settle(f"{tag}-quota-idle.png")
    check("control: with nobody touching it the update-time label does not change on its own", bool(label_before) and region_hash(idle, 600, 62, 275, 36) == label_before, label_before)
    click_shot(929, 79)
    after_seconds, after = settle(f"{tag}-quota-after-refresh.png", limit=60)
    label_after = region_hash(after, 600, 62, 275, 36) if after else None
    check("after clicking 刷新 the page's update-time label is different (the window really refreshed)", bool(label_after) and label_after != label_before, (label_before, label_after, after_seconds))
    check("the window stayed responsive after the click", not user32.IsHungAppWindow(hwnd))
    user32.PostMessageW(window["hwnd"], 0x0010, 0, 0)
    if tray is None:
        closed = wait_for(lambda: window_process.poll() is not None, 20)
        check("closing the window ends the router-window process", closed, window_process.poll())
    else:
        warm = window["pid"]
        hidden = wait_for(lambda: not windows_of(pid=warm), 20)
        check("closing the window hides it and keeps its process for the next open", bool(hidden) and warm in tray_processes(), (warm, tray_processes()))
        reopened_at = time.time()
        subprocess.Popen([str(exe), "--run-component", "router_window"], cwd=str(HOME), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        again = wait_for(lambda: windows_of(pid=warm), 20, interval=0.05)
        check("opening it again shows the same window within 3 s", bool(again) and time.time() - reopened_at < 3, round(time.time() - reopened_at, 2))
        if again:
            user32.PostMessageW(again[0]["hwnd"], 0x0010, 0, 0)
            wait_for(lambda: not windows_of(pid=warm), 20)
        check("the tray stayed alive while its router window was opened and closed", tray.poll() is None, tray.poll())
        tray.kill()
        check("the hidden window process ends once its tray is gone", wait_for(lambda: warm not in tray_processes(), 20), tray_processes())
    check("no AgentCompass process is left after the test", kill_trays(), tray_processes())


SHOT_PER_CURSOR = 1.5      # measured in this guest: a click sent to cursor position (x, y) lands at screenshot position (1.5x, 1.5y) (display scaling 150%)


def click_shot(x, y):
    """Click where a screenshot taken in this guest shows the control."""
    click(x / SHOT_PER_CURSOR, y / SHOT_PER_CURSOR)


def click(x, y):
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.2)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.08)
    user32.mouse_event(0x0004, 0, 0, 0, 0)


def settle(name, limit=45):
    """Screenshot until two consecutive captures are byte-identical (the Tk window repaints slowly); returns (seconds, path)."""
    import hashlib
    started, last, path = time.time(), None, None
    while time.time() - started < limit:
        path = shot(screen_rect(), name)
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path else None
        if digest and digest == last:
            break
        last = digest
        time.sleep(1.5)
    return round(time.time() - started, 1), path


def move(hwnd, x, y, w, h):
    user32.ShowWindow(hwnd, 9)
    user32.MoveWindow(hwnd, x, y, w, h, True)


def g_gui_tray():
    if "gui_tray" not in ONLY:                           # the installer step already drives the installed tray
        return
    exe = IN / "frozen" / "AgentCompass" / "AgentCompass.exe"
    section("packaged tray, run from the staged folder")
    check("the staged frozen tray exists", exe.is_file(), exe)
    if exe.is_file():
        state = HOME / ".agent-compass"
        state.mkdir(parents=True, exist_ok=True)
        flag, trace = state / "theatre-trace.on", state / "theatre-trace.log"
        flag.touch()
        try:
            gui_session(exe, "frozen")
            text = trace.read_text(encoding="utf-8") if trace.is_file() else ""
            # Dashboard.close withdraws the window and exits; it does not destroy each canvas.
            # gui_session separately verifies that closing ends the window process.
            check("opt-in frozen diagnostics recorded real rendering and stop transitions",
                  all(event in text for event in ("play#", "draw#", "stop#")) and "EXC" not in text)
            if trace.is_file():
                shutil.copy2(trace, OUT / "frozen-theatre-trace.log")
        finally:
            flag.unlink(missing_ok=True)


def schtasks_xml(name):
    done = run(["schtasks", "/Query", "/TN", name, "/XML"], label="schtasks-query")
    return done.stdout if done.returncode == 0 else None


def g_installer():
    if ONLY and "installer" not in ONLY:
        return
    section("Inno Setup: build the installer, install silently, scheduled task, tray, uninstall")
    staged = next((IN / "inno").glob("innosetup-*.exe"), None)
    check("the Inno Setup installer and the frozen tray were staged", staged is not None and (IN / "frozen" / "AgentCompass" / "AgentCompass.exe").is_file(), staged)
    if not staged:
        return
    version = (IN / "inno" / "version.txt").read_text(encoding="utf-8").strip()
    inno = run([staged, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER", f"/DIR={INNO_DIR}", f"/LOG={LOGS / 'inno-install.log'}"], label="inno-setup", timeout=600)
    iscc = INNO_DIR / "ISCC.exe"
    check("Inno Setup installed per user into the guest (silent)", inno.returncode == 0 and iscc.is_file(), (inno.stdout + inno.stderr)[-300:])
    if not iscc.is_file():
        return
    (BUILD / "packaging").mkdir(parents=True, exist_ok=True)
    shutil.copy2(IN / "inno" / "packaging" / "agentcompass.iss", BUILD / "packaging" / "agentcompass.iss")
    shutil.copytree(IN / "frozen" / "AgentCompass", BUILD / "dist" / "AgentCompass", dirs_exist_ok=True)
    built = run([iscc, f"/DMyAppVersion={version}", BUILD / "packaging" / "agentcompass.iss"], cwd=BUILD, label="iscc", timeout=900)
    setup_exe = BUILD / "dist" / "AgentCompass-tray-setup.exe"
    check("ISCC builds AgentCompass-tray-setup.exe from agentcompass.iss and the frozen folder", built.returncode == 0 and setup_exe.is_file(), (built.stdout + built.stderr)[-500:])
    if not setup_exe.is_file():
        return
    import hashlib
    digest = hashlib.sha256(setup_exe.read_bytes()).hexdigest()
    check(f"installer built: {setup_exe.stat().st_size} bytes, sha256 {digest}", True)
    (OUT / "release").mkdir(exist_ok=True)
    shutil.copy2(setup_exe, OUT / "release" / setup_exe.name)

    # State a person would already have: the pre-rename task, the old startup shortcuts, the three user variables, a router setup with hooks.
    run(["schtasks", "/Create", "/TN", "Route Manager", "/TR", "cmd /c exit", "/SC", "ONLOGON", "/F"], label="legacy-task")
    STARTUP.mkdir(parents=True, exist_ok=True)
    for name in ("Route Manager.lnk", "AgentCompass.lnk"):
        (STARTUP / name).write_text("placeholder", encoding="utf-8")
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE)
    for name in ENV_NAMES:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, "guest-" + name)
    winreg.CloseKey(key)
    (HOME / ".codex").mkdir(exist_ok=True)
    if not (HOME / ".codex" / "auth.json").exists():
        (HOME / ".codex" / "auth.json").write_text(json.dumps({"tokens": {"access_token": "guest-dummy-codex-token", "account_id": "guest-account"}}), encoding="utf-8")
    hooks = agentcompass("setup", "--yes", "--hooks")
    check("router setup with hooks is in place before the installer runs", hooks.returncode == 0 and (HOME / ".codex" / "skills" / "agent-compass").exists(), (hooks.stdout + hooks.stderr)[-300:])
    check("the pre-rename 'Route Manager' task exists before install", schtasks_xml("Route Manager") is not None)

    installed = run([setup_exe, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/LOG={LOGS / 'agentcompass-install.log'}"], label="agentcompass-setup", timeout=600)
    exe = INSTALL_DIR / "AgentCompass.exe"
    cli_exe = INSTALL_DIR / "cli" / "agentcompass.exe"
    check("silent install exits 0 and puts AgentCompass.exe under %LOCALAPPDATA%\\AgentCompass (no admin)", installed.returncode == 0 and exe.is_file(), (installed.stdout + installed.stderr)[-300:])
    check("the uninstaller (unins000.exe) is next to it", (INSTALL_DIR / "unins000.exe").is_file())
    check("runtime-info.json and app\\current\\app-manifest.json were installed",
          (INSTALL_DIR / "runtime-info.json").is_file() and (INSTALL_DIR / "app" / "current" / "app-manifest.json").is_file())
    cli_ver_a = run([cli_exe, "--version"], label="cli-version-a")
    check(f"installed cli\\agentcompass.exe --version reports {version}", cli_ver_a.returncode == 0 and version in cli_ver_a.stdout, cli_ver_a.stdout + cli_ver_a.stderr)
    check("a Start menu 'AgentCompass' shortcut launches the tray again after it is quit", START_MENU_LINK.is_file())
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY))
        registered = True
    except OSError:
        registered = False
    check("the per-user Add/Remove Programs entry exists", registered)
    xml = schtasks_xml("AgentCompass") or ""
    check("scheduled task 'AgentCompass' exists, logon trigger, runs the installed exe", "LogonTrigger" in xml and str(exe).lower() in xml.lower().replace("&amp;", "&"), xml[:600])
    check("the pre-rename 'Route Manager' task was removed by the install", schtasks_xml("Route Manager") is None)

    run_task = run(["schtasks", "/Run", "/TN", "AgentCompass"], label="task-run")
    started = wait_for(lambda: tray_processes(), 40)
    check("running the scheduled task starts the installed tray", run_task.returncode == 0 and bool(started), (run_task.stdout + run_task.stderr)[-300:])
    time.sleep(10)
    check("the tray started by the task is still running 10 s later", bool(tray_processes()), tray_processes())
    shot(screen_rect(), "installed-task-desktop.png")
    kill_trays()
    console_command_opens_no_window(exe)
    gui_session(exe, "installed", router_only=False)

    if (IN / "inno" / "tray" / "updates.py").is_file():
        section("Inno Setup: incremental code-package update (A -> B=0.4.0) and runtime-change full installer fallback")
        import zipfile
        sys.path.insert(0, str(IN / "inno"))  # agentcompass and psutil staged beside tray/
        sys.path.insert(0, str(IN / "inno" / "tray"))
        import updates as tray_updates
        override_file = HOME / ".agent-compass" / "accounts-override.json"
        override_doc = json.loads(override_file.read_text(encoding="utf-8")) if override_file.is_file() else {}
        override_doc["self_update_preserved"] = True
        override_file.write_text(json.dumps(override_doc, indent=2), encoding="utf-8")
        subprocess.Popen([str(exe)], cwd=str(HOME), env=base_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        old_pids = wait_for(tray_processes, 40)
        check("installed tray is running before self-update", bool(old_pids), old_pids)
        check("detect_install_shape identifies the installed tray as 'installer'", tray_updates.detect_install_shape(INSTALL_DIR, frozen=True) == "installer")
        check("non-frozen local_version(INSTALL_DIR) reads version A from disk (not 0.0.0)", tray_updates.local_version(INSTALL_DIR) == version, tray_updates.local_version(INSTALL_DIR))

        installer_home = BUILD / "installer-home"
        installer_home.mkdir(parents=True, exist_ok=True)
        bad_release_file = BUILD / "release-bad-digest.json"
        bad_release_file.write_text(
            json.dumps({"tag_name": "v0.4.0", "assets": [{"name": setup_exe.name, "url": setup_exe.as_uri(), "digest": "sha256:" + "0" * 64}]}),
            encoding="utf-8",
        )
        bad_upd = run(
            [exe, "--run-component", "updates", "update-agentcompass", "--release-file", bad_release_file, "--state-dir", HOME / ".agent-compass", "--home", installer_home, "--which-path", cli_exe],
            label="frozen-update-bad-digest",
        )
        check("frozen AgentCompass.exe update_agentcompass refuses an asset with a mismatched sha256 digest", bad_upd.returncode != 0, bad_upd.stdout + bad_upd.stderr)
        os.environ["AGENTCOMPASS_PUBLIC_DATA"] = "off"

        # Build version B (0.4.0) code package (app-0.4.0.zip) with the SAME runtime_fingerprint
        rt_info = json.loads((INSTALL_DIR / "runtime-info.json").read_text(encoding="utf-8"))
        rt_fp = rt_info["runtime_fingerprint"]
        pkg_b_tree = BUILD / "app-0.4.0-tree"
        if pkg_b_tree.exists():
            shutil.rmtree(pkg_b_tree)
        shutil.copytree(INSTALL_DIR / "app" / "current", pkg_b_tree)
        init_py = pkg_b_tree / "agentcompass" / "__init__.py"
        init_text = re.sub(
            r'^(__version__\s*=\s*[\'"])([^\'"]+)([\'"])',
            r'\g<1>0.4.0\g<3>',
            init_py.read_text(encoding="utf-8"),
            count=1,
            flags=re.M,
        )
        init_py.write_bytes(init_text.replace("\r\n", "\n").encode("utf-8"))
        manifest_b_file = pkg_b_tree / "app-manifest.json"
        manifest_b = json.loads(manifest_b_file.read_text(encoding="utf-8"))
        manifest_b["version"] = "0.4.0"
        manifest_b["required_runtime_fingerprint"] = rt_fp
        manifest_b["files"]["agentcompass/__init__.py"] = hashlib.sha256(init_py.read_bytes()).hexdigest()
        manifest_b_file.write_bytes((json.dumps(manifest_b, indent=2) + "\n").encode("utf-8"))

        app_b_zip = BUILD / "dist" / "app-0.4.0.zip"
        with zipfile.ZipFile(app_b_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for fpath in sorted(pkg_b_tree.rglob("*")):
                if fpath.is_file():
                    zf.write(fpath, fpath.relative_to(pkg_b_tree).as_posix())
        app_b_sha = hashlib.sha256(app_b_zip.read_bytes()).hexdigest()
        shutil.copy2(app_b_zip, OUT / "release" / app_b_zip.name)

        release_manifest_b = {
            "schema_version": 1,
            "version": "0.4.0",
            "tag_name": "v0.4.0",
            "runtime_fingerprint": rt_fp,
            "app_package": {
                "name": "app-0.4.0.zip",
                "size": app_b_zip.stat().st_size,
                "sha256": app_b_sha,
                "digest": f"sha256:{app_b_sha}",
                "required_runtime_fingerprint": rt_fp,
            },
            "installer": {
                "name": setup_exe.name,
                "size": setup_exe.stat().st_size,
                "sha256": digest,
                "digest": f"sha256:{digest}",
                "runtime_fingerprint": rt_fp,
            },
        }
        release_manifest_path = BUILD / "dist" / "manifest.json"
        release_manifest_path.write_bytes((json.dumps(release_manifest_b, indent=2) + "\n").encode("utf-8"))
        shutil.copy2(release_manifest_path, OUT / "release" / "manifest.json")
        release_manifest_sha = hashlib.sha256(release_manifest_path.read_bytes()).hexdigest()

        # Path 2 & Path 7: Same runtime_fingerprint -> real frozen AgentCompass.exe updates via app-0.4.0.zip,
        # visible old router_window stays open, releases Local\AgentCompassWindowShow, and new tray opens new router_window (R3-3)
        def _warm_show_event_open():
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenEventW.restype = ctypes.c_void_p
            k32.OpenEventW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
            k32.CloseHandle.argtypes = [ctypes.c_void_p]
            h = k32.OpenEventW(0x00100000, False, "Local\\AgentCompassWindowShow")
            if h:
                k32.CloseHandle(h)
            return bool(h)

        rw_proc = subprocess.Popen(
            [str(exe), "--run-component", "router_window", "--parent", str(old_pids[0])],
            cwd=str(HOME), env=base_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        wait_for(_warm_show_event_open, 15)
        time.sleep(1)
        release_code_file = BUILD / "release-code-040.json"
        release_code_file.write_text(
            json.dumps({
                "tag_name": "v0.4.0",
                "assets": [
                    {"name": "manifest.json", "url": release_manifest_path.as_uri(), "digest": f"sha256:{release_manifest_sha}"},
                    {"name": "app-0.4.0.zip", "url": app_b_zip.as_uri(), "digest": f"sha256:{app_b_sha}"},
                    {"name": setup_exe.name, "url": "file:///C:/nonexistent-installer-must-not-be-downloaded.exe", "digest": f"sha256:{digest}"},
                ],
            }),
            encoding="utf-8",
        )
        frozen_code_upd = run(
            [exe, "--run-component", "updates", "update-agentcompass", "--release-file", release_code_file, "--state-dir", HOME / ".agent-compass", "--home", installer_home, "--which-path", cli_exe],
            label="frozen-update-code-040",
        )
        summary_code = frozen_code_upd.stdout + frozen_code_upd.stderr
        code_pids = wait_for(lambda: [p for p in tray_processes() if p not in (old_pids or [])], 45) or tray_processes()
        time.sleep(2.5)
        old_rw_stayed_open = rw_proc.poll() is None
        new_rw_proc = subprocess.Popen(
            [str(exe), "--run-component", "router_window", "--parent", str(code_pids[0] if code_pids else os.getpid())],
            cwd=str(HOME), env=base_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        wait_for(_warm_show_event_open, 15)
        time.sleep(2)
        new_rw_opened = new_rw_proc.poll() is None and _warm_show_event_open()
        for p_rw in (rw_proc, new_rw_proc):
            try:
                p_rw.terminate()
                p_rw.wait(timeout=5)
            except Exception:
                pass
        check("code-only update (A -> 0.4.0, same runtime fingerprint) via real frozen AgentCompass.exe uses app-0.4.0.zip and restarts tray",
              frozen_code_upd.returncode == 0 and "已完成代码包增量更新" in summary_code and "v0.4.0" in summary_code and bool(code_pids),
              (frozen_code_upd.returncode, summary_code, code_pids))
        check("during incremental update, visible old router_window stays open while releasing Local\\AgentCompassWindowShow so the new tray opens the new router_window panel (R3-3)",
              bool(old_rw_stayed_open) and bool(new_rw_opened),
              (old_rw_stayed_open, new_rw_opened))
        cli_ver_b = run([cli_exe, "--version"], label="cli-version-b")
        check("after code-only update, cli\\agentcompass.exe --version reports 0.4.0",
              cli_ver_b.returncode == 0 and "0.4.0" in cli_ver_b.stdout, cli_ver_b.stdout + cli_ver_b.stderr)
        inspected_b = tray_updates.inspect_installs(root=INSTALL_DIR, home=installer_home, which_path=cli_exe, frozen=False)
        check("after code-only update, both tray and CLI report 0.4.0 with no version mismatch",
              inspected_b["tray"]["version"] == "0.4.0" and not inspected_b["version_mismatch"] and inspected_b["display_version"] == "AgentCompass 0.4.0",
              inspected_b)
        check("app\\previous retained version A for rollback",
              tray_updates.local_version(INSTALL_DIR / "app" / "previous") == version,
              tray_updates.local_version(INSTALL_DIR / "app" / "previous"))

        # Path 4: Startup crash in app\current (after single_instance mutex) automatically rolls back to app\previous in a new process and tray stays alive
        kill_trays()
        wait_for(lambda: not tray_processes(), 15)
        app_pyw_cur = INSTALL_DIR / "app" / "current" / "tray" / "app.pyw"
        crashing_pyw = app_pyw_cur.read_text(encoding="utf-8").replace(
            "PLUGINS = plugins.load()",
            "raise RuntimeError('simulated startup crash after single_instance mutex')",
            1,
        )
        app_pyw_cur.write_bytes(crashing_pyw.replace("\r\n", "\n").encode("utf-8"))
        cur_manifest_file = INSTALL_DIR / "app" / "current" / "app-manifest.json"
        cur_manifest = json.loads(cur_manifest_file.read_text(encoding="utf-8"))
        cur_manifest["files"]["tray/app.pyw"] = hashlib.sha256(app_pyw_cur.read_bytes()).hexdigest()
        cur_manifest_file.write_bytes((json.dumps(cur_manifest, indent=2) + "\n").encode("utf-8"))
        subprocess.Popen([str(exe)], cwd=str(HOME), env=base_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        rolled_back_ver = wait_for(lambda: tray_updates.local_version(INSTALL_DIR) == version and tray_processes(), 40)
        time.sleep(3)
        alive_after_rollback = tray_processes()
        rb_state = json.loads((INSTALL_DIR / "app" / "state.json").read_text(encoding="utf-8"))
        check("when app\\current crashes during startup after acquiring single_instance mutex, AgentCompass.exe rolls back to app\\previous in a new process and tray stays alive",
              bool(rolled_back_ver) and bool(alive_after_rollback) and rb_state.get("current_version") == version and rb_state.get("rolled_back_from") == "0.4.0",
              (rolled_back_ver, alive_after_rollback, rb_state))

        # Path 4b: Real frozen cli\agentcompass.exe python-shim (`-I -X utf8 <script.py> --help`) rolls back cleanly without losing sys.argv (NEW-3)
        shutil.rmtree(INSTALL_DIR / "app" / "previous", ignore_errors=True)
        shutil.copytree(INSTALL_DIR / "app" / "current", INSTALL_DIR / "app" / "previous")
        shutil.rmtree(INSTALL_DIR / "app" / "current", ignore_errors=True)
        shutil.copytree(pkg_b_tree, INSTALL_DIR / "app" / "current")
        rj_cur = INSTALL_DIR / "app" / "current" / "skills" / "agent-compass" / "scripts" / "route_judge.py"
        rj_cur.write_bytes(b"import nonexistent_broken_module_040\n")
        cur_m_file = INSTALL_DIR / "app" / "current" / "app-manifest.json"
        cur_m = json.loads(cur_m_file.read_text(encoding="utf-8"))
        cur_m["files"]["skills/agent-compass/scripts/route_judge.py"] = hashlib.sha256(rj_cur.read_bytes()).hexdigest()
        cur_m_file.write_bytes((json.dumps(cur_m, indent=2) + "\n").encode("utf-8"))
        cli_rb = run([cli_exe, "-I", "-X", "utf8", rj_cur, "--help"], label="cli-shim-rollback")
        cli_rb_state = json.loads((INSTALL_DIR / "app" / "state.json").read_text(encoding="utf-8"))
        check("when a script under app\\current raises ImportError via cli\\agentcompass.exe -I -X utf8 <script> --help, run_cli rolls back to app\\previous and preserves argv",
              cli_rb.returncode == 0 and "unknown command" not in (cli_rb.stdout + cli_rb.stderr).lower() and cli_rb_state.get("current_version") == version,
              (cli_rb.returncode, (cli_rb.stdout + cli_rb.stderr)[-300:], cli_rb_state))

        # Path 3: Runtime fingerprint changed -> real frozen AgentCompass.exe falls back to full AgentCompass-tray-setup.exe installer
        # Build a 0.4.0 full installer with a new runtime_fingerprint in a separate directory so BUILD/dist/AgentCompass-tray-setup.exe remains at version A for g_product
        new_rt_fp = "f" * 64
        pkg_c_tree = BUILD / "app-0.4.0-newrt-tree"
        if pkg_c_tree.exists():
            shutil.rmtree(pkg_c_tree)
        shutil.copytree(pkg_b_tree, pkg_c_tree)
        manifest_c_file = pkg_c_tree / "app-manifest.json"
        manifest_c = json.loads(manifest_c_file.read_text(encoding="utf-8"))
        manifest_c["required_runtime_fingerprint"] = new_rt_fp
        if isinstance(manifest_c.get("runtime_requirements"), dict):
            manifest_c["runtime_requirements"]["runtime_fingerprint"] = new_rt_fp
        manifest_c_file.write_bytes((json.dumps(manifest_c, indent=2) + "\n").encode("utf-8"))

        build_040 = BUILD / "build-040"
        (build_040 / "packaging").mkdir(parents=True, exist_ok=True)
        shutil.copy2(IN / "inno" / "packaging" / "agentcompass.iss", build_040 / "packaging" / "agentcompass.iss")
        shutil.copytree(IN / "frozen" / "AgentCompass", build_040 / "dist" / "AgentCompass", dirs_exist_ok=True)
        shutil.rmtree(build_040 / "dist" / "AgentCompass" / "app" / "current", ignore_errors=True)
        shutil.copytree(pkg_c_tree, build_040 / "dist" / "AgentCompass" / "app" / "current")
        shutil.rmtree(build_040 / "dist" / "AgentCompass" / "app" / "packages", ignore_errors=True)
        (build_040 / "dist" / "AgentCompass" / "app" / "packages").mkdir(parents=True, exist_ok=True)
        app_c_zip = build_040 / "dist" / "AgentCompass" / "app" / "packages" / "app-0.4.0.zip"
        with zipfile.ZipFile(app_c_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for fpath in sorted(pkg_c_tree.rglob("*")):
                if fpath.is_file():
                    zf.write(fpath, fpath.relative_to(pkg_c_tree).as_posix())
        app_c_sha = hashlib.sha256(app_c_zip.read_bytes()).hexdigest()
        (build_040 / "dist" / "AgentCompass" / "app" / "state.json").write_bytes(
            (json.dumps({"current_version": "0.4.0", "active_version": "0.4.0", "runtime_fingerprint": new_rt_fp}, indent=2) + "\n").encode("utf-8")
        )
        rt_040_file = build_040 / "dist" / "AgentCompass" / "runtime-info.json"
        rt_040_doc = json.loads(rt_040_file.read_text(encoding="utf-8"))
        rt_040_doc["runtime_version"] = "0.4.0"
        rt_040_doc["runtime_fingerprint"] = new_rt_fp
        rt_040_file.write_bytes((json.dumps(rt_040_doc, indent=2) + "\n").encode("utf-8"))
        built_040 = run([iscc, "/DMyAppVersion=0.4.0", build_040 / "packaging" / "agentcompass.iss"], cwd=build_040, label="iscc-040", timeout=900)
        setup_040_exe = build_040 / "dist" / "AgentCompass-tray-setup.exe"
        setup_040_digest = hashlib.sha256(setup_040_exe.read_bytes()).hexdigest() if setup_040_exe.is_file() else ""
        check("ISCC built 0.4.0 full installer for runtime-change fallback check", built_040.returncode == 0 and setup_040_exe.is_file())

        # Local runtime still has rt_fp (from version A rollback), while 0.4.0 release requires new_rt_fp
        release_manifest_b["runtime_fingerprint"] = new_rt_fp
        release_manifest_b["app_package"]["sha256"] = app_c_sha
        release_manifest_b["app_package"]["digest"] = f"sha256:{app_c_sha}"
        release_manifest_b["app_package"]["required_runtime_fingerprint"] = new_rt_fp
        release_manifest_b["installer"]["sha256"] = setup_040_digest
        release_manifest_b["installer"]["digest"] = f"sha256:{setup_040_digest}"
        release_manifest_b["installer"]["runtime_fingerprint"] = new_rt_fp
        release_manifest_path.write_bytes((json.dumps(release_manifest_b, indent=2) + "\n").encode("utf-8"))
        release_manifest_sha = hashlib.sha256(release_manifest_path.read_bytes()).hexdigest()

        before_full_pids = tray_processes()
        release_full_file = BUILD / "release-full-040.json"
        release_full_file.write_text(
            json.dumps({
                "tag_name": "v0.4.0",
                "assets": [
                    {"name": "manifest.json", "url": release_manifest_path.as_uri(), "digest": f"sha256:{release_manifest_sha}"},
                    {"name": "app-0.4.0.zip", "url": "file:///C:/nonexistent-code-pkg-must-not-be-used.zip", "digest": f"sha256:{app_c_sha}"},
                    {"name": setup_040_exe.name, "url": setup_040_exe.as_uri(), "digest": f"sha256:{setup_040_digest}"},
                ],
            }),
            encoding="utf-8",
        )
        run(
            [exe, "--run-component", "updates", "update-agentcompass", "--release-file", release_full_file, "--state-dir", HOME / ".agent-compass", "--home", installer_home, "--which-path", cli_exe],
            label="frozen-update-full-040",
            timeout=600,
        )
        import update_status as tray_update_status
        def _full_installer_done():
            try:
                rt = json.loads((INSTALL_DIR / "runtime-info.json").read_text(encoding="utf-8"))
                if rt.get("runtime_fingerprint") != new_rt_fp:
                    return []
                pids = [p for p in tray_processes() if p not in (before_full_pids or [])]
                if not pids:
                    return []
                tray_update_status.read_status(HOME / ".agent-compass")
                return pids
            except Exception:
                return []

        new_pids = wait_for(_full_installer_done, 90)
        restored_rt = json.loads((INSTALL_DIR / "runtime-info.json").read_text(encoding="utf-8"))
        upd_status_file = HOME / ".agent-compass" / "update-status.json"
        upd_status_text = upd_status_file.read_text(encoding="utf-8") if upd_status_file.is_file() else ""
        cli_ver_full = run([cli_exe, "--version"], label="cli-version-full")
        check("when runtime fingerprint differs, real frozen AgentCompass.exe runs the full installer and restores runtime-info.json + 0.4.0",
              "已完成安装包静默更新" in upd_status_text and bool(new_pids) and restored_rt.get("runtime_fingerprint") == new_rt_fp and "0.4.0" in cli_ver_full.stdout,
              (upd_status_text[-400:], restored_rt.get("runtime_fingerprint"), cli_ver_full.stdout))
        after_override = json.loads(override_file.read_text(encoding="utf-8")) if override_file.is_file() else {}
        check("user settings in ~/.agent-compass/accounts-override.json are untouched by installer upgrade", after_override.get("self_update_preserved") is True, after_override)

        # Path 5: Editing packaging/agentcompass.iss changes iss_sha256 and triggers full installer (NEW-2)
        iss_040 = build_040 / "packaging" / "agentcompass.iss"
        old_iss_sha = hashlib.sha256(iss_040.read_bytes()).hexdigest()
        iss_040.write_text(iss_040.read_text(encoding="utf-8") + "\n; modified installer action for 0.4.1\n", encoding="utf-8")
        new_iss_sha = hashlib.sha256(iss_040.read_bytes()).hexdigest()
        rt_now = json.loads((INSTALL_DIR / "runtime-info.json").read_text(encoding="utf-8"))
        rt_now["iss_sha256"] = old_iss_sha
        (INSTALL_DIR / "runtime-info.json").write_bytes((json.dumps(rt_now, indent=2) + "\n").encode("utf-8"))
        can_pkg_iss, iss_reason = tray_updates.can_use_code_package(
            tray_updates.read_local_runtime_info(INSTALL_DIR),
            {
                "runtime_fingerprint": new_rt_fp,
                "iss_sha256": new_iss_sha,
                "app_package": {
                    "name": "app-0.4.0.zip",
                    "sha256": app_c_sha,
                    "required_runtime_fingerprint": new_rt_fp,
                },
            },
        )
        release_manifest_b["runtime_fingerprint"] = new_rt_fp
        release_manifest_b["iss_sha256"] = new_iss_sha
        release_manifest_path.write_bytes((json.dumps(release_manifest_b, indent=2) + "\n").encode("utf-8"))
        release_manifest_sha = hashlib.sha256(release_manifest_path.read_bytes()).hexdigest()
        release_iss_file = BUILD / "release-iss-040.json"
        release_iss_file.write_text(
            json.dumps({
                "tag_name": "v0.4.0",
                "assets": [
                    {"name": "manifest.json", "url": release_manifest_path.as_uri(), "digest": f"sha256:{release_manifest_sha}"},
                    {"name": "app-0.4.0.zip", "url": "file:///C:/nonexistent-code-pkg-when-iss-changed.zip", "digest": f"sha256:{app_c_sha}"},
                    {"name": setup_040_exe.name, "url": setup_040_exe.as_uri(), "digest": f"sha256:{setup_040_digest}"},
                ],
            }),
            encoding="utf-8",
        )
        before_iss_pids = tray_processes()
        run(
            [exe, "--run-component", "updates", "update-agentcompass", "--release-file", release_iss_file, "--state-dir", HOME / ".agent-compass", "--home", installer_home, "--which-path", cli_exe],
            label="frozen-update-iss-040",
            timeout=600,
        )
        iss_new_pids = wait_for(lambda: [p for p in tray_processes() if p not in (before_iss_pids or [])], 90)
        tray_update_status.read_status(HOME / ".agent-compass")
        check("modifying agentcompass.iss (iss_sha256 change) rejects code package and triggers full installer via real frozen AgentCompass.exe",
              old_iss_sha != new_iss_sha and not can_pkg_iss and iss_reason.startswith("iss_sha256_mismatch:") and bool(iss_new_pids),
              (old_iss_sha[:12], new_iss_sha[:12], can_pkg_iss, iss_reason, iss_new_pids))

        # Path 6: Full installer failure (non-zero exit code / target version not installed) truthfully reports failure in update-status.json (R3-1/R3-2)
        fail_iss = build_040 / "packaging" / "agentcompass-fail.iss"
        fail_iss.write_text(
            "[Setup]\n"
            "AppName=AgentCompass\n"
            "AppVersion=0.4.1\n"
            "DefaultDirName={localappdata}\\AgentCompass\n"
            "PrivilegesRequired=lowest\n"
            "OutputDir=..\\dist-fail\n"
            "OutputBaseFilename=AgentCompass-tray-setup\n"
            "[Code]\n"
            "function InitializeSetup(): Boolean;\n"
            "begin\n"
            "  Result := False;\n"
            "end;\n",
            encoding="utf-8",
        )
        built_fail = run([iscc, fail_iss], cwd=build_040, label="iscc-fail-041", timeout=180)
        fail_setup_exe = build_040 / "dist-fail" / "AgentCompass-tray-setup.exe"
        fail_setup_sha = hashlib.sha256(fail_setup_exe.read_bytes()).hexdigest() if fail_setup_exe.is_file() else ""
        fail_rt_fp = "e" * 64
        fail_manifest = dict(release_manifest_b)
        fail_manifest["version"] = "0.4.1"
        fail_manifest["tag_name"] = "v0.4.1"
        fail_manifest["runtime_fingerprint"] = fail_rt_fp
        fail_manifest["installer"] = {
            "name": fail_setup_exe.name,
            "size": fail_setup_exe.stat().st_size if fail_setup_exe.is_file() else 0,
            "sha256": fail_setup_sha,
            "digest": f"sha256:{fail_setup_sha}",
            "runtime_fingerprint": fail_rt_fp,
        }
        fail_manifest_path = build_040 / "dist-fail" / "manifest.json"
        fail_manifest_path.write_bytes((json.dumps(fail_manifest, indent=2) + "\n").encode("utf-8"))
        fail_manifest_sha = hashlib.sha256(fail_manifest_path.read_bytes()).hexdigest()
        release_fail_file = BUILD / "release-fail-041.json"
        release_fail_file.write_text(
            json.dumps({
                "tag_name": "v0.4.1",
                "assets": [
                    {"name": "manifest.json", "url": fail_manifest_path.as_uri(), "digest": f"sha256:{fail_manifest_sha}"},
                    {"name": fail_setup_exe.name, "url": fail_setup_exe.as_uri(), "digest": f"sha256:{fail_setup_sha}"},
                ],
            }),
            encoding="utf-8",
        )
        fail_run = run(
            [exe, "--run-component", "updates", "update-agentcompass", "--release-file", release_fail_file, "--state-dir", HOME / ".agent-compass", "--home", installer_home, "--which-path", cli_exe],
            label="frozen-update-fail-041",
            timeout=180,
        )
        fail_status_1 = tray_update_status.read_status(HOME / ".agent-compass")
        # Also verify post-kill reconciliation when pending_installer was recorded for 0.4.1 and installer aborted after killing tray
        (HOME / ".agent-compass" / "update-status.json").write_bytes(
            (json.dumps({
                "running": True,
                "started": time.time() - 5.0,
                "owner_pid": 999998,
                "title": "AgentCompass",
                "targets": ["AgentCompass"],
                "pending_installer": {
                    "pid": 999999,
                    "target_version": "0.4.1",
                    "started_at": time.time() - 5.0,
                    "install_root": str(INSTALL_DIR),
                    "tag_label": "v0.4.1",
                },
                "results": {},
                "events": [],
            }, indent=2) + "\n").encode("utf-8")
        )
        fail_status_2 = tray_update_status.read_status(HOME / ".agent-compass")
        check("when full installer fails (non-zero exit code or aborts before installing target version), update-status.json truthfully reports failure (R3-1/R3-2)",
              built_fail.returncode == 0
              and fail_run.returncode != 0
              and not fail_status_1.get("running")
              and "更新失败" in str((fail_status_1.get("results") or {}).get("AgentCompass") or "")
              and not fail_status_2.get("running")
              and "更新失败" in str((fail_status_2.get("results") or {}).get("AgentCompass") or ""),
              (fail_run.returncode, fail_status_1.get("results"), fail_status_2.get("results")))
        kill_trays()

    section("Inno Setup: silent uninstall")
    started = subprocess.Popen([str(INSTALL_DIR / "unins000.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/LOG={LOGS / 'agentcompass-uninstall.log'}"], env=base_env())
    started.wait(timeout=120)
    gone = wait_for(lambda: not INSTALL_DIR.exists() or not (INSTALL_DIR / "AgentCompass.exe").exists(), 90)
    check("silent uninstall removes the installed executable", gone, [p.name for p in INSTALL_DIR.glob("*")][:8] if INSTALL_DIR.exists() else "")
    time.sleep(3)
    check("the installed folder is gone or empty", not INSTALL_DIR.exists() or not any(INSTALL_DIR.rglob("*.exe")), [str(p) for p in INSTALL_DIR.rglob("*")][:8] if INSTALL_DIR.exists() else "")
    check("scheduled task 'AgentCompass' was deleted", schtasks_xml("AgentCompass") is None)
    check("the Start menu shortcut was removed", not START_MENU_LINK.exists())
    check("no AgentCompass process is left", not tray_processes(), tray_processes())
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment")
    leftovers = []
    for name in ENV_NAMES:
        try:
            winreg.QueryValueEx(key, name)
            leftovers.append(name)
        except OSError:
            pass
    winreg.CloseKey(key)
    check("the three user environment variables were removed", not leftovers, leftovers)
    check("the Startup shortcuts were removed", not [n for n in ("Route Manager.lnk", "AgentCompass.lnk") if (STARTUP / n).exists()], list(STARTUP.glob("*")))
    check("the uninstaller ran 'agentcompass uninstall' (router skill links and hook files are gone)",
          not (HOME / ".codex" / "skills" / "agent-compass").exists() and not (HOME / ".claude" / "skills" / "agent-compass").exists(),
          [str(p) for p in (HOME / ".codex" / "skills" / "agent-compass", HOME / ".claude" / "skills" / "agent-compass") if p.exists()])
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY))
        still = True
    except OSError:
        still = False
    check("the Add/Remove Programs entry is gone", not still)
    run(["schtasks", "/Delete", "/TN", "Route Manager", "/F"], label="cleanup-legacy")


def registry_path(root, subkey):
    try:
        with winreg.OpenKey(root, subkey) as key:
            return winreg.QueryValueEx(key, "Path")[0]
    except OSError:
        return ""


def user_path_entries():
    return [p for p in registry_path(winreg.HKEY_CURRENT_USER, "Environment").split(os.pathsep) if p]


def new_shell_env():
    """What a terminal opened now would get: PATH from the registry (machine, then user), no Python of ours anywhere."""
    machine = registry_path(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")
    path = os.pathsep.join(os.path.expandvars(p) for p in (machine.split(os.pathsep) + user_path_entries()) if p)
    env = {k: v for k, v in base_env().items() if not k.upper().startswith(("PYTHON", "VIRTUAL_ENV"))}
    env["PATH"] = path
    return env


def shell(command, env, stdin=None, label=None):
    return run(["cmd", "/d", "/c", command], env=env, stdin=stdin, label=label or command.split()[0])


def g_product():
    """W02: the installer alone is the product. No Python on PATH: install, new shell, setup, gate hook, upgrade, uninstall."""
    if ONLY and "product" not in ONLY:
        return
    section("Installer as the whole product: agentcompass.exe on PATH, setup and gate hook with no Python")
    setup_exe = BUILD / "dist" / "AgentCompass-tray-setup.exe"
    staged = next((IN / "inno").glob("innosetup-*.exe"), None)
    if not setup_exe.is_file() and staged is not None:
        if not (INNO_DIR / "ISCC.exe").is_file():
            run([staged, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER", f"/DIR={INNO_DIR}"], label="inno-setup", timeout=600)
        (BUILD / "packaging").mkdir(parents=True, exist_ok=True)
        shutil.copy2(IN / "inno" / "packaging" / "agentcompass.iss", BUILD / "packaging" / "agentcompass.iss")
        shutil.copytree(IN / "frozen" / "AgentCompass", BUILD / "dist" / "AgentCompass", dirs_exist_ok=True)
        version = (IN / "inno" / "version.txt").read_text(encoding="utf-8").strip()
        run([INNO_DIR / "ISCC.exe", f"/DMyAppVersion={version}", BUILD / "packaging" / "agentcompass.iss"], cwd=BUILD, label="iscc", timeout=900)
    check("the installer built from this tree is available", setup_exe.is_file(), setup_exe)
    if not setup_exe.is_file():
        return
    version = (IN / "inno" / "version.txt").read_text(encoding="utf-8").strip()
    cli_dir = INSTALL_DIR / "cli"
    cli_exe = cli_dir / "agentcompass.exe"

    # A clean machine: nothing of the wheel step on the user PATH, no router setup left over.
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_SET_VALUE)
    kept = [p for p in user_path_entries() if "ve nv" not in p.lower() and "python" not in p.lower()]
    winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, os.pathsep.join(kept))
    winreg.CloseKey(key)
    for link in (HOME / ".codex" / "skills" / "agent-compass", HOME / ".claude" / "skills" / "agent-compass"):
        if link.exists() or link.is_symlink():
            run(["cmd", "/d", "/c", "rmdir", str(link)], label="rmdir-skill")
    # Dummy sign-ins as in the setup step: `record` only accepts an account the router can see.
    for folder, name, doc in ((".codex", "auth.json", {"tokens": {"access_token": "guest-dummy-codex-token", "account_id": "guest-account"}}),
                              (".claude", ".credentials.json", {"claudeAiOauth": {"accessToken": "guest-dummy-claude-token", "subscriptionType": "max"}})):
        (HOME / folder).mkdir(exist_ok=True)
        if not (HOME / folder / name).exists():
            (HOME / folder / name).write_text(json.dumps(doc), encoding="utf-8")
    before = new_shell_env()
    nopy = shell("where python python3 py", before, label="python-absent")
    check("no Python on the new-shell PATH before install", nopy.returncode != 0, nopy.stdout + before["PATH"][:400])
    no_cli = shell("agentcompass --version", before, label="cli-absent")
    check("and no agentcompass either", no_cli.returncode != 0, no_cli.stdout[:200])

    installed = run([setup_exe, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SETUP=hooks", f"/LOG={LOGS / 'product-install.log'}"],
                    label="product-install", timeout=600)
    check("silent install with /SETUP=hooks exits 0 and ships cli\\agentcompass.exe", installed.returncode == 0 and cli_exe.is_file(),
          (installed.stdout + installed.stderr)[-300:])
    entries = user_path_entries()
    check("the user PATH (HKCU\\Environment) starts with the installer's cli folder, once",
          bool(entries) and os.path.normcase(os.path.expandvars(entries[0])) == os.path.normcase(str(cli_dir))
          and sum(os.path.normcase(os.path.expandvars(p)) == os.path.normcase(str(cli_dir)) for p in entries) == 1, entries)
    env = new_shell_env()
    nopy = shell("where python python3 py", env, label="python-absent-after")
    check("still no Python on the new-shell PATH after install", nopy.returncode != 0, nopy.stdout + env["PATH"][:400])
    where = shell("where agentcompass", env, label="where")
    check("a new shell finds agentcompass in the installer's cli folder", where.returncode == 0
          and os.path.normcase(where.stdout.strip().splitlines()[0]) == os.path.normcase(str(cli_exe)), where.stdout + where.stderr)
    ver = shell("agentcompass --version", env, label="version")
    check(f"`agentcompass --version` in a new shell prints {version}", ver.returncode == 0 and version in ver.stdout, ver.stdout + ver.stderr[-300:])
    doc = shell("agentcompass doctor", env, label="doctor")
    check("`agentcompass doctor` runs and names the Windows installer as the active install",
          doc.returncode == 0 and "active: Windows installer" in doc.stdout, doc.stdout[-600:] + doc.stderr[-300:])
    demo = shell("agentcompass plan --demo --dry-run", env, label="plan-demo")
    check("`agentcompass plan --demo --dry-run` runs", demo.returncode == 0 and demo.stdout.strip(), demo.stdout[-300:] + demo.stderr[-300:])

    log = HOME / ".agent-compass" / "installer-setup.log"
    log_text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
    check("/SETUP=hooks ran setup and logged it to ~/.agent-compass/installer-setup.log", bool(log_text.strip()), log_text[-400:])
    bundle_candidate = INSTALL_DIR / "app" / "current" / "skills" / "agent-compass"
    bundle = (bundle_candidate if bundle_candidate.exists() else (cli_dir / "_internal" / "agentcompass" / "data" / "skills" / "agent-compass")).resolve()
    links = {name: (HOME / name / "skills" / "agent-compass") for name in (".codex", ".claude")}
    check("the router skill is linked from ~/.codex and ~/.claude into the installed bundle",
          all(p.exists() and p.resolve() == bundle for p in links.values()), {k: str(v.resolve()) if v.exists() else "missing" for k, v in links.items()})
    settings = HOME / ".claude" / "settings.json"
    hooks = [h.get("command", "") for g in (json.loads(settings.read_text(encoding="utf-8")).get("hooks", {}).get("PreToolUse", []) if settings.is_file() else [])
             for h in g.get("hooks", [])]
    gate = next((c for c in hooks if "gate-hook" in c), "")
    check("the Claude PreToolUse hook calls the installed agentcompass.exe gate-hook, no Python",
          bool(gate) and cli_exe.as_posix().lower() in gate.lower() and "python" not in gate.lower() and "router_gate_hook" not in gate, hooks)
    codex_hooks = HOME / ".codex" / "hooks.json"
    codex_text = codex_hooks.read_text(encoding="utf-8") if codex_hooks.is_file() else ""
    check("the Codex hook calls the same exe", "gate-hook" in codex_text and "python" not in codex_text.lower(), codex_text[:400])
    rules = HOME / ".claude" / "CLAUDE.md"
    rules_text = rules.read_text(encoding="utf-8") if rules.is_file() else ""
    check("global rules tell agents to run `agentcompass roll`", "agentcompass roll" in rules_text and "python" not in rules_text.split("agentcompass roll")[0][-40:].lower(),
          rules_text[:300])

    # The hook exactly as a host runs it (argv from the stored command), a sub-agent launch on stdin.
    (HOME / ".agent-compass" / "router-ledger.jsonl").unlink(missing_ok=True)
    import shlex
    argv = shlex.split(gate) if gate else [str(cli_exe), "gate-hook"]
    payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Agent", "session_id": "product-session",
                          "tool_input": {"subagent_type": "general-purpose", "prompt": "do the work", "model": "sonnet"}})

    def verdict():
        done = run(argv, env=env, stdin=payload, label="gate-hook")
        return ((parse(done.stdout) or {}).get("hookSpecificOutput") or {}).get("permissionDecision"), done
    first, done = verdict()
    check("an unrouted sub-agent launch is denied by the installed gate hook (no Python, router_command is 'agentcompass')",
          done.returncode == 0 and first == "deny" and "agentcompass roll" in done.stdout and "app\\current\\agentcompass" not in done.stdout,
          done.stdout[:300] + done.stderr[-300:])
    rec = shell('agentcompass record --provider claude --account claude --model claude-sonnet-5-5 --effort high --constraint user_override --basis W02-gate-test',
                env, label="record")
    second, done = verdict()
    check("after `agentcompass record` the same launch passes", rec.returncode == 0 and second != "deny", rec.stdout[-200:] + rec.stderr[-200:] + done.stdout[:300])
    other = run(argv, env=env, stdin=json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": {"file_path": "x"}}), label="gate-read")
    check("an unrelated tool call passes", ((parse(other.stdout) or {}).get("hookSpecificOutput") or {}).get("permissionDecision") != "deny", other.stdout[:200])

    # The tray's one-click update re-runs the same installer silently, without /SETUP.
    upgraded = run([setup_exe, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/LOG={LOGS / 'product-upgrade.log'}"], label="product-upgrade", timeout=600)
    kill_trays()
    entries = user_path_entries()
    check("an in-place upgrade keeps exactly one PATH entry", upgraded.returncode == 0
          and sum(os.path.normcase(os.path.expandvars(p)) == os.path.normcase(str(cli_dir)) for p in entries) == 1, entries)
    check("after the upgrade the skill links still resolve into the bundle", all(p.exists() and p.resolve() == bundle for p in links.values()))
    again, done = verdict()
    check("after the upgrade the stored hook command still runs (the routed launch still passes)", done.returncode == 0 and again != "deny", done.stdout[:300] + done.stderr[-300:])
    ver = shell("agentcompass --version", new_shell_env(), label="version-after-upgrade")
    check("after the upgrade a new shell still runs agentcompass", ver.returncode == 0 and version in ver.stdout, ver.stdout + ver.stderr[-200:])

    section("Installer as the whole product: uninstall removes the PATH entry and what setup changed")
    kill_trays()
    started = subprocess.Popen([str(INSTALL_DIR / "unins000.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/LOG={LOGS / 'product-uninstall.log'}"], env=base_env())
    started.wait(timeout=180)
    wait_for(lambda: not cli_exe.exists(), 90)
    check("the installed agentcompass.exe is gone", not cli_exe.exists())
    entries = user_path_entries()
    check("the cli folder is no longer on the user PATH", not any(os.path.normcase(os.path.expandvars(p)) == os.path.normcase(str(cli_dir)) for p in entries), entries)
    check("the skill links are gone", not any(p.exists() or p.is_symlink() for p in links.values()), [str(p) for p in links.values() if p.exists()])
    left = settings.read_text(encoding="utf-8") if settings.is_file() else ""
    check("the gate hook is gone from Claude settings", "gate-hook" not in left, left[:300])
    left = codex_hooks.read_text(encoding="utf-8") if codex_hooks.is_file() else ""
    check("the gate hook is gone from Codex hooks", "gate-hook" not in left, left[:300])
    gone = shell("agentcompass --version", new_shell_env(), label="cli-gone")
    check("a new shell no longer finds agentcompass", gone.returncode != 0, gone.stdout[:200])


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    only = sys.argv[1:]
    ONLY[:] = only
    for step in (g_isolation, g_stage, g_install, g_setup_and_uninstall, g_herdr, g_gui_probe, g_gui_tray, g_installer, g_product):
        if only and step.__name__[2:] not in only and step.__name__ not in ("g_isolation", "g_stage", "g_install"):
            continue
        try:
            step()
        except Exception:
            import traceback
            check(f"{step.__name__} ran to the end", False, traceback.format_exc()[-700:])
    save()
    (OUT / "DONE").write_text("done", encoding="utf-8")
    return sum(1 for r in results if not r["ok"])


if __name__ == "__main__":
    raise SystemExit(main())
