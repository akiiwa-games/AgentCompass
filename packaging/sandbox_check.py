"""Run an INSTALLED AgentCompass through the operations a user performs, in an isolated sandbox.

    python packaging/sandbox_check.py [--wheel X.whl] [--report DIR] [--keep]

Without --wheel the working tree (tracked and untracked, not ignored, files) is copied into a throw-away git repo and a
wheel is built from that copy, so uncommitted work is covered exactly as a release build would carry it.

What is real: the wheel installed into a fresh virtualenv, the console script, the collector process, the cache files,
the router CLIs, the tray helper scripts, the gate hook and (when a checkout is present) Usage Float's own code.
What is substituted: the provider HTTP endpoints. A sitecustomize in the sandbox venv sends the two provider hosts to a
local fixture server and refuses every other network name, so nothing leaves the machine. Credentials are dummy files.

The tray executable is built with PyInstaller from an isolated source copy and its `--run-component` entries are run for
real (collector tick against a local LiteLLM-style fixture, capacity sampling). --skip-frozen leaves that out and says so.
With --wheel the product comes from the given wheel; the tray, helper and source-entry scenarios use a minimal isolated copy
of the working tree (never the checkout itself), so a real usage-float next to the checkout cannot be started by accident.

Not covered (listed in the report): the Inno Setup installer, Windows startup entries / scheduled tasks, any GUI (Tk window,
tray menu, router window), real provider authentication and real account quotas, the Antigravity CLI, Usage Float's own
upstream code when no usage-float checkout exists, and the Windows user-registry variables (the check refuses to run when
router variables are set there). A missing tray dependency or PyInstaller is a failure, not a skip.

Exit code = number of failed assertions. Every run writes its own directory under --report (earlier runs are kept);
`latest.txt` names the newest. A run directory holds report.json, report.md and one log per command.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import http.server
import json
import os
import shutil
import site
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_TOKENS = {"codex": "sandbox-codex-access", "claude": "sandbox-claude-access"}
ALLOWED_HOSTS = ("chatgpt.com", "api.anthropic.com")
NETGUARD: Path | None = None        # directory holding sitecustomize.py for interpreters that are not the sandbox venv
USER_SITE = site.getusersitepackages() if site.ENABLE_USER_SITE else ""
TRAY_PYTHON = Path(sys.executable)   # the tray runs under a normal interpreter with its extras (psutil, Pillow, pystray)
SITECUSTOMIZE = '''\
import json, os, socket, urllib.error, urllib.request
from pathlib import Path
from urllib.parse import urlsplit
# The settings come from sandbox_net.json next to this file (and from the environment when it is set). A child process that a
# product command starts with a scrubbed environment (setup's usage collection does) therefore still refuses the network.
try:
    _cfg = json.loads(Path(__file__).with_name("sandbox_net.json").read_text(encoding="utf-8"))
except Exception:
    _cfg = {}
_base = os.environ.get("AC_SANDBOX_PROXY") or _cfg.get("proxy")
if _base or _cfg:
    _base = _base or ""
    _allowed = set(filter(None, os.environ.get("AC_SANDBOX_HOSTS", "").split(","))) | set(_cfg.get("hosts", []))
    _log = os.environ.get("AC_SANDBOX_NETLOG") or _cfg.get("log")
    _opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    _loopback = ("127.0.0.1", "::1", "localhost")
    # Only the fixture servers the test started may be reached on this machine; a real local daemon (Paseo, Dot, ...) may not.
    _ports = {int(p) for p in os.environ.get("AC_SANDBOX_PORTS", "").split(",") if p.strip().isdigit()} | set(_cfg.get("ports", []))
    _getaddrinfo = socket.getaddrinfo

    def _note(kind, target):
        if _log:
            with open(_log, "a", encoding="utf-8") as stream:
                stream.write(json.dumps({"kind": kind, "target": target, "pid": os.getpid()}) + "\\n")

    def _dns(host, *args, **kwargs):
        if host not in ("127.0.0.1", "localhost", "::1", None, ""):
            _note("blocked-dns", str(host))
            raise socket.gaierror("sandbox: name resolution is blocked")
        return _getaddrinfo(host, *args, **kwargs)

    def _urlopen(url, data=None, timeout=30, **kwargs):
        request = url if isinstance(url, urllib.request.Request) else urllib.request.Request(url, data=data)
        parts = urlsplit(request.full_url)
        if parts.hostname in _loopback:
            if parts.port in _ports:
                _note("loopback", request.full_url)
                return _opener.open(request, timeout=timeout)
            _note("blocked-loopback", request.full_url)
            raise urllib.error.URLError("sandbox: this local port is not a fixture (%s)" % parts.port)
        if parts.hostname not in _allowed or not _base:
            _note("blocked", request.full_url)
            raise urllib.error.URLError("sandbox: real network is blocked (%s)" % parts.hostname)
        _note("routed", request.full_url)
        target = _base + "/" + parts.netloc + parts.path + ("?" + parts.query if parts.query else "")
        routed = urllib.request.Request(target, data=request.data, headers=dict(request.header_items()),
                                        method=request.get_method())
        return _opener.open(routed, timeout=timeout)

    _connect = socket.socket.connect

    def _guard(self, address):
        host = address[0] if isinstance(address, tuple) else None
        if host is not None and host not in _loopback:
            _note("blocked-connect", str(host))
            raise OSError("sandbox: outbound connections are blocked (%s)" % host)
        if host is not None and address[1] not in _ports:
            _note("blocked-loopback", "%s:%s" % (host, address[1]))
            raise OSError("sandbox: this local port is not a fixture (%s)" % address[1])
        return _connect(self, address)

    socket.getaddrinfo = _dns
    socket.socket.connect = _guard
    urllib.request.urlopen = _urlopen
'''


def write_netguard(targets, fixture_url: str, ports, log: Path) -> None:
    """sitecustomize.py plus its sandbox_net.json into each directory in ``targets`` (the venv's site-packages, the NETGUARD dir)."""
    config = {"proxy": fixture_url, "hosts": list(ALLOWED_HOSTS), "ports": sorted(set(ports)), "log": str(log)}
    for target in targets:
        (Path(target) / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
        (Path(target) / "sandbox_net.json").write_text(json.dumps(config), encoding="utf-8")


# ---------------------------------------------------------------------------------------------------------------------
# provider fixture server


class Fixture:
    """Serves the two provider usage endpoints. ``mode[host]``: ok | error | unauthorized."""

    def __init__(self):
        self.mode = {"chatgpt.com": "ok", "api.anthropic.com": "ok"}
        self.codex_used = 12.0
        self.claude_used = 20.0
        self.proxy_spend = 2.5
        self.codex_weekly = None     # None: half of the five-hour figure
        self.claude_weekly = None
        self.requests: list[dict] = []
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status, body, headers=None):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                host, _, rest = self.path.lstrip("/").partition("/")
                path = "/" + rest.split("?", 1)[0]
                fixture.requests.append({"host": host, "path": path, "authorization": self.headers.get("Authorization")})
                mode = fixture.mode.get(host, "ok")
                if mode == "error":
                    return self._reply(500, {"error": "fixture failure"})
                if mode == "unauthorized":
                    return self._reply(401, {"error": "fixture unauthorized"})
                now = dt.datetime.now(dt.timezone.utc)
                if host == "chatgpt.com" and path == "/backend-api/wham/usage":
                    return self._reply(200, {"plan_type": "plus", "rate_limit": {
                        "primary_window": {"used_percent": fixture.codex_used, "limit_window_seconds": 18000,
                                           "reset_at": int((now + dt.timedelta(hours=3)).timestamp())},
                        "secondary_window": {"used_percent": fixture.codex_weekly if fixture.codex_weekly is not None else fixture.codex_used / 2, "limit_window_seconds": 604800,
                                             "reset_at": int((now + dt.timedelta(days=4)).timestamp())}}})
                if host == "api.anthropic.com" and path == "/api/oauth/usage":
                    return self._reply(200, {
                        "five_hour": {"utilization": fixture.claude_used, "resets_at": (now + dt.timedelta(hours=2)).isoformat()},
                        "seven_day": {"utilization": fixture.claude_weekly if fixture.claude_weekly is not None else fixture.claude_used / 2, "resets_at": (now + dt.timedelta(days=3)).isoformat()}})
                return self._reply(404, {"error": "unknown fixture path"})

            def do_POST(self):
                """A LiteLLM-style proxy account (host name ``litellm``): spend and budget come back as response headers."""
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                host, _, rest = self.path.lstrip("/").partition("/")
                path = "/" + rest.split("?", 1)[0]
                fixture.requests.append({"host": host, "path": path, "authorization": self.headers.get("Authorization")})
                if host == "litellm" and path.endswith("/embeddings"):
                    return self._reply(200, {"data": []}, {"x-litellm-key-spend": str(fixture.proxy_spend),
                                                           "x-litellm-key-max-budget": "10"})
                if self.path.endswith("/chat/completions"):
                    profile = {"work_kind": "general_implementation", "reasoning_depth": 2, "code_complexity": 1,
                               "context_scale": 1, "divergence": 1, "hallucination_risk": 2, "error_cost": 2,
                               "needs_visual": False, "needs_gui": False, "local_only": True, "expected_hours": 0.5,
                               "hours_confidence": "medium", "urgency": "normal", "interaction_style": "engineering",
                               "multi_task": False, "task_count": "1",
                               "reason": "sandbox fixture classification of a local util.py edit"}
                    return self._reply(200, {"choices": [{"message": {"content": json.dumps(profile)}}]})
                return self._reply(404, {"error": "unknown fixture path"})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def reset(self):
        self.mode = {"chatgpt.com": "ok", "api.anthropic.com": "ok"}
        self.codex_used, self.claude_used = 12.0, 20.0
        self.codex_weekly = self.claude_weekly = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def hits(self, host: str | None = None) -> list[dict]:
        return [r for r in self.requests if host is None or r["host"] == host]


# ---------------------------------------------------------------------------------------------------------------------
# recording


class Run:
    def __init__(self, report: Path):
        self.report = report
        self.operations: list[dict] = []
        self.counter = 0
        self.logs = report / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)

    def operation(self, covers: str, title: str) -> "Operation":
        op = Operation(self, covers, title)
        self.operations.append(op.record)
        return op

    def not_executed(self, covers: str, reason: str):
        self.operations.append({"id": f"X{len(self.operations) + 1}", "covers": covers, "title": "not executed",
                                "status": "not-executed", "reason": reason, "commands": [], "assertions": []})

    @property
    def failures(self) -> int:
        return sum(1 for op in self.operations for a in op["assertions"] if not a["ok"])


class Operation:
    def __init__(self, run: Run, covers: str, title: str):
        run.counter += 1
        self.run = run
        self.record = {"id": f"S{run.counter:02d}", "covers": covers, "title": title, "status": "executed",
                       "commands": [], "assertions": []}

    def check(self, name: str, condition, detail: str = ""):
        ok = bool(condition)
        self.record["assertions"].append({"name": name, "ok": ok, "detail": "" if ok else str(detail)[:600]})
        if not ok:
            print(f"  FAIL {self.record['id']} {name}: {str(detail)[:300]}")
        return ok

    def log(self, argv, env_note, result: subprocess.CompletedProcess):
        index = len(self.record["commands"]) + 1
        name = f"{self.record['id']}-{index}.log"
        text = ("$ " + " ".join(map(str, argv)) + f"\n# env: {env_note}\n# returncode: {result.returncode}\n"
                f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}\n")
        (self.run.logs / name).write_text(text, encoding="utf-8", errors="replace")
        self.record["commands"].append({"argv": [str(a) for a in argv], "returncode": result.returncode, "log": f"logs/{name}"})


# ---------------------------------------------------------------------------------------------------------------------
# sandbox


class Sandbox:
    """One user: a home directory, its provider credentials, and a way to run installed commands as that user."""

    def __init__(self, base: Path, name: str, venv: Path, fixture: Fixture, run: Run):
        self.root = base / name
        self.home = self.root / "home"
        self.state = self.home / ".agent-compass"
        self.codex = self.home / ".codex"
        self.claude = self.home / ".claude"
        self.temp = self.root / "tmp"
        self.venv, self.fixture, self.run = venv, fixture, run
        self.python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        self.agentcompass = venv / ("Scripts/agentcompass.exe" if os.name == "nt" else "bin/agentcompass")
        self.netlog = self.root / "net.jsonl"
        self.ports: set[int] = set()        # extra local fixture ports the sandbox may reach (the provider fixture is always allowed)
        for folder in (self.home, self.temp):
            folder.mkdir(parents=True, exist_ok=True)

    @property
    def data(self) -> Path:
        return self.site_packages() / "agentcompass" / "data"

    def site_packages(self) -> Path:
        if os.name == "nt":
            return self.venv / "Lib" / "site-packages"
        return next((self.venv / "lib").glob("python*/site-packages"))

    @property
    def scripts(self) -> Path:
        return self.data / "skills" / "agent-compass" / "scripts"

    @property
    def cache(self) -> Path:
        return self.state / "usage-cache.json"

    @property
    def legacy(self) -> Path:
        return self.home / ".usage-float" / "usage-cache.json"

    def sign_in(self):
        """Dummy credentials in the formats the CLIs write; they never leave the machine."""
        self.codex.mkdir(parents=True, exist_ok=True)
        (self.codex / "auth.json").write_text(json.dumps({"tokens": {
            "access_token": FIXTURE_TOKENS["codex"], "account_id": "sandbox-account"}}), encoding="utf-8")
        self.claude.mkdir(parents=True, exist_ok=True)
        (self.claude / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
            "accessToken": FIXTURE_TOKENS["claude"], "subscriptionType": "max"}}), encoding="utf-8")

    def env(self, **extra) -> dict:
        env = {"PATH": str(self.python.parent) + os.pathsep + os.environ.get("SYSTEMROOT", r"C:\Windows") + r"\System32"
               if os.name == "nt" else str(self.python.parent) + os.pathsep + "/usr/bin:/bin",
               "HOME": str(self.home), "USERPROFILE": str(self.home), "APPDATA": str(self.home / "AppData/Roaming"),
               "LOCALAPPDATA": str(self.home / "AppData/Local"), "TEMP": str(self.temp), "TMP": str(self.temp),
               "CODEX_HOME": str(self.codex), "ROUTER_PROVIDER_HOME": str(self.home),
               "ROUTER_STATE_DIR": str(self.state), "PASEO_HOME": str(self.home / ".paseo"),
               "PYTHONUTF8": "1", "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(NETGUARD),
               "AC_SANDBOX_PROXY": self.fixture.url, "AC_SANDBOX_HOSTS": ",".join(ALLOWED_HOSTS),
               "AC_SANDBOX_NETLOG": str(self.netlog),
               "AC_SANDBOX_PORTS": ",".join(str(p) for p in sorted({self.fixture.server.server_address[1], *self.ports}))}
        for key in ("SYSTEMROOT", "PATHEXT", "WINDIR", "COMSPEC"):
            if os.environ.get(key):
                env[key] = os.environ[key]
        env.update({k: v for k, v in extra.items() if v is not None})
        for key in [k for k, v in extra.items() if v is None]:
            env.pop(key, None)
        return env

    def run_command(self, op: Operation, argv, *, cwd=None, timeout=240, stdin=None, **extra_env):
        env = self.env(**extra_env)
        if Path(str(argv[0])) == TRAY_PYTHON and USER_SITE:
            # The tray interpreter's extras (pystray, Pillow, ...) often sit in the real user site; the sandbox HOME/APPDATA hide it.
            env["PYTHONPATH"] = os.pathsep.join(filter(None, [env.get("PYTHONPATH"), USER_SITE]))
        note = ", ".join(f"{k}={env[k]}" for k in ("HOME", "ROUTER_STATE_DIR", "CODEX_HOME", "ROUTER_USAGE_CACHE") if k in env)
        result = subprocess.run([str(a) for a in argv], cwd=str(cwd or self.root), env=env, input=stdin, text=True,
                                capture_output=True, timeout=timeout, encoding="utf-8", errors="replace")
        op.log(argv, note, result)
        return result

    def py(self, op: Operation, code: str, *, isolated=True, cwd=None, python=None, **extra_env):
        return self.run_command(op, [python or self.python, *(["-I"] if isolated else []), "-X", "utf8", "-c", code], cwd=cwd, **extra_env)

    @staticmethod
    def pythonpath(*paths) -> str:
        return os.pathsep.join([*map(str, paths), str(NETGUARD)])

    def script(self, op: Operation, name: str, *args, **extra_env):
        return self.run_command(op, [self.python, "-I", "-X", "utf8", self.scripts / name, *args], **extra_env)

    def net_events(self) -> list[dict]:
        if not self.netlog.is_file():
            return []
        return [json.loads(line) for line in self.netlog.read_text(encoding="utf-8").splitlines() if line.strip()]

    def read_cache(self, path: Path | None = None) -> dict:
        return json.loads((path or self.cache).read_text(encoding="utf-8"))


def sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def parse_json(text: str):
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        start = text.find("{")
        return json.loads(text[start:]) if start >= 0 else None


def usage_stage(box: Sandbox, op: Operation, *accounts, **env):
    """The router's own read of the cache: ``route_judge.py usage``."""
    argv = ["usage", *(["--accounts", *accounts] if accounts else [])]
    result = box.script(op, "route_judge.py", *argv, **env)
    return result, (parse_json(result.stdout) if result.returncode == 0 else None)


def at(delta_seconds: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=delta_seconds)).isoformat()


def codex_row(used: float, fetched: str, **extra) -> dict:
    return {"provider_id": "codex", "display_name": "codex", "available": True, "stale": False, "fetched_at": fetched,
            "windows": [{"id": "five_hour", "label": "5h", "used_pct": used, "resets_at": at(3 * 3600)}], **extra}


# ---------------------------------------------------------------------------------------------------------------------
# scenarios


def scenario_clean_install(ctx):
    """Headless install with no Usage Float anywhere: collect, read, route, diagnose."""
    box = Sandbox(ctx["base"], "clean", ctx["venv"], ctx["fixture"], ctx["run"])
    box.sign_in()
    fixture = ctx["fixture"]
    fixture.reset()

    op = ctx["run"].operation("clean headless install has no Usage Float", "installed environment cannot import Usage Float")
    op.check("no plugin or Usage Float directory exists in the sandbox home",
             not (box.state / "plugins").exists() and not (box.home / ".usage-float").exists())
    op = ctx["run"].operation("doctor before any collection", "agentcompass doctor on a fresh home")
    result = box.run_command(op, [box.agentcompass, "doctor"])
    op.check("doctor exits 0", result.returncode == 0, result.stderr)
    op.check("doctor reports the usage cache as missing at the canonical path",
             "Usage cache: missing" in result.stdout and str(box.cache) in result.stdout, result.stdout[-400:])

    op = ctx["run"].operation("collector --once through the console script", "agentcompass usage (first collection)")
    result = box.run_command(op, [box.agentcompass, "usage"])
    op.check("collector exits 0", result.returncode == 0, result.stderr + result.stdout)
    receipt = parse_json(result.stdout) or {}
    providers = receipt.get("providers", {})
    op.check("receipt lists codex and claude with windows", all(providers.get(n, {}).get("window_count", 0) > 0 for n in ("codex", "claude")), receipt)
    op.check("canonical cache written", box.cache.is_file())
    op.check("legacy ~/.usage-float was not created", not (box.home / ".usage-float").exists())
    tokens = {r["host"]: r["authorization"] for r in fixture.hits() if r["authorization"]}
    op.check("fixture saw only the sandbox tokens", tokens.get("chatgpt.com") == "Bearer " + FIXTURE_TOKENS["codex"]
             and tokens.get("api.anthropic.com") == "Bearer " + FIXTURE_TOKENS["claude"], tokens)
    op.check("no request tried to leave the sandbox", not [e for e in box.net_events() if e["kind"].startswith("blocked")], box.net_events())

    op = ctx["run"].operation("router reads the collector cache", "route_judge usage / providers / plan against the canonical cache")
    result, snapshot = usage_stage(box, op)
    accounts = (snapshot or {}).get("accounts", {})
    op.check("router read ok from the canonical cache", snapshot and snapshot.get("read_ok") and
             Path(snapshot["cache_path"]).resolve() == box.cache.resolve(), result.stdout[:300] + result.stderr[:300])
    op.check("codex and claude are fresh", all(accounts.get(n, {}).get("status") == "fresh" for n in ("codex", "claude")), accounts)
    result = box.run_command(op, [box.agentcompass, "providers"])
    op.check("providers inventory exits 0 and sees both accounts", result.returncode == 0 and "codex" in result.stdout and "claude" in result.stdout, result.stderr)
    task = box.root / "task.json"
    task.write_text(json.dumps({"task": {
        "objective": "Fix the pagination bug", "work_kind": "general_implementation", "expected_hours": 1,
        "needs": {"local_only": True},
        "profile": {"reasoning_depth": 3, "code_complexity": 2, "context_scale": 2, "hallucination_risk": 2, "error_cost": 2,
                    "needs_visual": False, "needs_gui": False, "local_only": True, "interaction_style": "engineering",
                    "reason": "sandbox fixture"}}}), encoding="utf-8")
    result = box.run_command(op, [box.agentcompass, "plan", "--input", task, "--dry-run"], ROUTER_LEDGER=str(box.state / "ledger.jsonl"))
    plan = parse_json(result.stdout) or {}
    bundle = plan.get("selected_bundle") or {}
    usage = plan.get("usage") or {}
    op.check("plan --dry-run (no cache argument) routes to a collected account", result.returncode == 0 and
             bundle.get("account") in ("codex", "claude"), result.stdout[:400] + result.stderr[:400])
    op.check("the plan read the canonical cache and found that account fresh",
             usage.get("cache_path") and Path(usage["cache_path"]).resolve() == box.cache.resolve() and
             (usage.get("accounts") or {}).get(bundle.get("account"), {}).get("status") == "fresh", usage and {k: usage[k] for k in ("cache_path", "read_ok")})
    return box


# Commands the documentation tells an agent to run, each with the document that has to contain it verbatim. The sandbox runs
# them as written (placeholders filled with fixture values) from a directory that is not the repository.
TASK_TEXT = "Rename foo to bar in util.py"
NATIVE = ["--parent-host", "native", "--parent-id", "sandbox-parent", "--parent-address", "sandbox-inbox"]
DOC_RUNS = [
    ("README.md", "agentcompass plan --demo --dry-run", []),
    ("README.md", "agentcompass doctor", []),
    ("README.md", "agentcompass setup --dry-run", ["--yes", "--no-hooks"]),
    ("README.md", 'agentcompass roll --task "<what the worker should do>"', NATIVE),
    ("skills/agent-compass/SKILL.md", 'agentcompass roll --task "<what the worker should do>"', NATIVE),
    ("skills/agent-compass/references/roll-reference.md", 'agentcompass roll --task "<text>"', NATIVE),
    ("skills/agent-compass/references/roll-reference.md", "agentcompass roll --task - < task.md", NATIVE),
    ("skills/agent-compass/references/roll-reference.md", "agentcompass roll --brief task.md", NATIVE),
    ("skills/agent-compass/references/router-internals.md", "agentcompass plan --demo --dry-run", []),
    ("skills/agent-compass/references/router-internals.md", "agentcompass plan --input <task.json>", []),
    ("skills/agent-compass/references/router-internals.md", "agentcompass scorer --action show", []),
    ("skills/agent-compass/references/router-internals.md", "agentcompass providers", []),
    ("skills/agent-compass/references/provider-usage.md", "agentcompass providers", []),
    ("skills/agent-compass/references/provider-usage.md", "agentcompass usage --show", []),
    ("skills/agent-compass/references/dispatch-lifecycle.md", "agentcompass dispatch --dispatch-action reconcile", []),
    ("skills/agent-compass/references/dispatch-lifecycle.md", "agentcompass dispatch --dispatch-action status --dispatch-id <dispatch_id>", []),
]
PLACEHOLDERS = {"<task.json>": "task.json", "<task.md>": "task.md", "<dispatch_id>": "no-such-dispatch",
                "<what the worker should do>": TASK_TEXT, "<text>": TASK_TEXT}


OPERATIONAL = (r"python[^\n`]*route_judge\.py|(?<![\w/\[`.])route_judge\.py\s+"
               r"(?:roll|plan|providers|record|score|scorer|delegate|select|override|dispatch|launch|usage)\b|dispatch-state")


def scenario_entry_and_docs(ctx, box: Sandbox):
    """The entry an agent is told to use, in the installed package and in a source checkout, from a directory that is not the repo."""
    import re
    import shlex
    repo = ctx["repo"]
    elsewhere = box.root / "elsewhere"
    elsewhere.mkdir(exist_ok=True)
    (elsewhere / "task.md").write_text(TASK_TEXT + "\n", encoding="utf-8")
    (elsewhere / "task.json").write_text(json.dumps({"task": {"objective": TASK_TEXT, "expected_hours": 0.5, "needs": {"local_only": True}}}),
                                         encoding="utf-8")
    scorer_cfg = box.state / "scorer-config.json"
    box.state.mkdir(parents=True, exist_ok=True)
    scorer_cfg.write_text(json.dumps({"scorer_type": "openai_compatible", "base_url": f"{box.fixture.url}/v1",
                                      "model": "sandbox-fixture-classifier", "api_key": "sandbox-scorer-key"}), encoding="utf-8")
    offline = {"ROUTER_SCORER_CONFIG": str(scorer_cfg)}          # the classifier stand-in: local fixture HTTP server
    installed = [box.agentcompass]
    checkout = [box.python, "-I", "-X", "utf8", repo / "agentcompass"]
    modes = {"installed package": installed, "source checkout": checkout}

    def call(op, entry, *args, stdin=None, **env):
        return box.run_command(op, [*entry, *args], cwd=elsewhere, stdin=stdin, **{**offline, **env})

    op = ctx["run"].operation("entry discovery and help", "agentcompass -h / <command> -h in both install modes, from a non-repo directory")
    for label, entry in modes.items():
        result = call(op, entry, "-h")
        op.check(f"[{label}] -h lists roll, plan, usage, doctor",
                 result.returncode == 0 and all(w in result.stdout for w in ("roll", "plan", "usage", "doctor")), result.stderr)
        result = call(op, entry, "roll", "-h")
        op.check(f"[{label}] roll -h shows --task/--brief and hides other stages' options",
                 result.returncode == 0 and "--task" in result.stdout and "--brief" in result.stdout and "--resets-in" not in result.stdout
                 and "--constraint" not in result.stdout, result.stdout[:300])

    op = ctx["run"].operation("roll task entries", "--task text, --task - on stdin, --brief file: same short result; --verbose; repeated delivery")
    shapes = {}
    for label, entry in modes.items():
        for name, args, stdin in (("--task text", ["--task", TASK_TEXT], None), ("--task - (stdin)", ["--task", "-"], TASK_TEXT + "\n"),
                                  ("--brief file", ["--brief", elsewhere / "task.md"], None)):
            dry = call(op, entry, "roll", *args, "--dry-run", stdin=stdin)
            document = parse_json(dry.stdout) or {}
            op.check(f"[{label}] roll {name} --dry-run: status dry_run, no hours needed", dry.returncode == 0 and document.get("status") == "dry_run"
                     and document.get("worker") and "assumptions" in document, dry.stdout[:300] + dry.stderr[:300])
            live = call(op, entry, "roll", *args, *NATIVE, stdin=stdin)
            document = parse_json(live.stdout) or {}
            shapes[(label, name)] = sorted(document)
            details = Path(document["details"]) if document.get("details") else None
            op.check(f"[{label}] roll {name}: short result with status, next_action and the full result on disk",
                     live.returncode == 0 and document.get("status") == "native" and document.get("next_action") and details and details.is_file()
                     and len(live.stdout) < 3000 and "dispatch_mode" not in document, live.stdout[:400] + live.stderr[:300])
            full = json.loads(details.read_text(encoding="utf-8")) if details and details.is_file() else {}
            op.check(f"[{label}] roll {name}: the saved result is the full one and its worker prompt file exists",
                     full.get("dispatch_mode") == "native" and Path(full.get("worker_prompt_file", "")).is_file(), sorted(full)[:12])
    op.check("--task and --brief produce the same result shape", len({tuple(v) for v in shapes.values()}) == 1, shapes)
    verbose = call(op, installed, "roll", "--task", TASK_TEXT, *NATIVE, "--verbose")
    full = parse_json(verbose.stdout) or {}
    op.check("--verbose prints the full result", verbose.returncode == 0 and full.get("dispatch_mode") == "native" and "route" in full, verbose.stdout[:200])
    again = call(op, installed, "roll", "--task", TASK_TEXT, *NATIVE)
    op.check("delivering the same task again gives the same short answer (nothing launches in this sandbox)",
             again.returncode == 0 and (parse_json(again.stdout) or {}).get("status") == "native", again.stdout[:300])
    unaddressed = call(op, installed, "roll", "--task", TASK_TEXT, PASEO_AGENT_ID=None)
    op.check("without a parent session roll refuses and says what to pass", unaddressed.returncode == 1 and "--parent-id" in unaddressed.stdout,
             unaddressed.stdout[:300])

    op = ctx["run"].operation("follow-up entries", "usage --show, dispatch status of an unknown id, usage from a non-repo directory")
    for label, entry in modes.items():
        result = call(op, entry, "usage", "--show", "--accounts", "codex")
        snapshot = parse_json(result.stdout) or {}
        op.check(f"[{label}] usage --show reads the collector cache", result.returncode == 0 and snapshot.get("read_ok") and
                 snapshot.get("accounts", {}).get("codex", {}).get("status") == "fresh", result.stdout[:300] + result.stderr[:200])
        result = call(op, entry, "dispatch", "--dispatch-action", "status", "--dispatch-id", "no-such-dispatch")
        op.check(f"[{label}] dispatch status of an unknown id is a clean one-line JSON error",
                 result.returncode == 1 and "Unknown dispatch id" in result.stdout and "Traceback" not in result.stderr,
                 result.stdout[:200] + result.stderr[:200])
    result = call(op, installed, "usage")
    op.check("usage (collect once) works from a non-repo directory", result.returncode == 0 and (parse_json(result.stdout) or {}).get("providers"),
             result.stderr[:300])

    op = ctx["run"].operation("documented commands", "the commands the docs print, run as written (placeholders filled) in both install modes")
    for doc, text, extra in DOC_RUNS:
        content = (repo / doc).read_text(encoding="utf-8")
        op.check(f"{doc} still contains: {text}", text in content)
        stdin, command = None, text
        if " < task.md" in command:
            command, stdin = command.replace(" < task.md", ""), TASK_TEXT + "\n"
        command = command.replace("--brief task.md", "--brief <task.md>")
        for placeholder, value in PLACEHOLDERS.items():
            command = command.replace(placeholder, (elsewhere / value).as_posix() if value.endswith((".md", ".json")) else value)
        argv = shlex.split(command, posix=True)[1:]
        for label, entry in modes.items():
            result = call(op, entry, *argv, *extra, stdin=stdin)
            if "no-such-dispatch" in command:
                ok = result.returncode == 1 and "Unknown dispatch id" in result.stdout
            else:
                ok = result.returncode == 0
            op.check(f"[{label}] {text}", ok, result.stdout[:250] + result.stderr[:250])

    op = ctx["run"].operation("installed documents", "the documents inside the wheel teach no direct script call")
    pattern = re.compile(OPERATIONAL)
    skill = box.data / "skills" / "agent-compass"
    shipped = sorted(skill.rglob("*.md")) + [skill / "agents" / "openai.yaml"]
    bad = [f"{path.name}:{n}" for path in shipped for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1) if pattern.search(line)]
    op.check(f"{len(shipped)} shipped documents contain no operational script call", not bad, bad[:5])


def scenario_freshness(ctx, box: Sandbox):
    """Stale data is never read as new: aged cache, failing provider, recovery."""
    fixture = ctx["fixture"]
    op = ctx["run"].operation("stale or failed data", "aged reading is stale; a failing provider keeps the old stamp; recovery is fresh")
    document = box.read_cache()
    old = at(-2 * 3600)
    document["providers"]["codex"]["fetched_at"] = old
    box.cache.write_text(json.dumps(document), encoding="utf-8")
    _, snapshot = usage_stage(box, op, "codex")
    op.check("a 2-hour-old reading is stale", snapshot["accounts"]["codex"]["status"] == "stale", snapshot)
    fixture.mode["chatgpt.com"] = "error"
    document = box.read_cache()
    document.get("attempts", {}).pop("codex", None)     # as if the last attempt were long ago
    box.cache.write_text(json.dumps(document), encoding="utf-8")
    before = len(fixture.hits("chatgpt.com"))
    result = box.run_command(op, [box.agentcompass, "usage"])
    op.check("collector survives a failing provider", result.returncode == 0, result.stderr)
    op.check("it asked the provider (the reading was due)", len(fixture.hits("chatgpt.com")) > before)
    row = box.read_cache()["providers"]["codex"]
    op.check("failure keeps the old fetched_at and marks the row stale", row["fetched_at"] == old and row.get("stale") is True, row)
    _, snapshot = usage_stage(box, op, "codex")
    op.check("router still reports stale", snapshot["accounts"]["codex"]["status"] == "stale", snapshot)
    fixture.mode["chatgpt.com"] = "ok"
    fixture.codex_used = 33.0
    document = box.read_cache()
    document.get("attempts", {}).pop("codex", None)
    box.cache.write_text(json.dumps(document), encoding="utf-8")
    result = box.run_command(op, [box.agentcompass, "usage"])
    _, snapshot = usage_stage(box, op, "codex")
    op.check("after recovery the reading is fresh with the new value", result.returncode == 0 and
             snapshot["accounts"]["codex"]["status"] == "fresh" and
             snapshot["accounts"]["codex"]["windows"][0]["used_pct"] == 33.0, snapshot["accounts"]["codex"])

    op = ctx["run"].operation("corrupt cache", "an unreadable canonical file is never overwritten")
    box.cache.write_text("{not json", encoding="utf-8")
    digest = sha(box.cache)
    result, snapshot = usage_stage(box, op)
    op.check("router reports no readable cache while it is corrupt (no invented data)", snapshot is None or not snapshot.get("read_ok"), result.stdout[:300])
    result = box.run_command(op, [box.agentcompass, "usage"])
    set_aside = sorted(box.state.glob("usage-cache.json.corrupt-*"))
    op.check("collector exits 0 and sets the corrupt file aside instead of overwriting it", result.returncode == 0 and len(set_aside) == 1 and
             sha(set_aside[0]) == digest, result.stderr[-300:])
    _, snapshot = usage_stage(box, op, "codex")
    op.check("a new valid cache replaces it", snapshot and snapshot["read_ok"], snapshot)


def scenario_legacy(ctx):
    """An existing Usage Float user: legacy cache is read, migrated by the collector, never written, and loses to newer data."""
    box = Sandbox(ctx["base"], "legacy", ctx["venv"], ctx["fixture"], ctx["run"])
    box.sign_in()
    fixture = ctx["fixture"]
    fixture.reset()
    seed = ctx["run"].operation("legacy fixture", "write a Usage Float style cache with real credential fingerprints")
    box.legacy.parent.mkdir(parents=True)
    # The collector, pointed at the legacy path, writes the same format Usage Float does (credential_key included).
    result = box.run_command(seed, [box.agentcompass, "usage"], ROUTER_USAGE_CACHE=str(box.legacy))
    seed.check("fixture cache produced at the legacy path", result.returncode == 0 and box.legacy.is_file(), result.stderr)
    legacy_doc = box.read_cache(box.legacy)
    base = legacy_doc["providers"]["codex"]
    legacy_doc["providers"]["codex"] = {**base, "fetched_at": at(-300)}
    legacy_doc["providers"]["grok"] = {"provider_id": "grok", "display_name": "grok", "available": True, "stale": False,
                                       "fetched_at": at(-200), "windows": [{"id": "credits", "label": "credits", "used_pct": 5.0, "resets_at": at(86400)}]}
    box.legacy.write_text(json.dumps(legacy_doc), encoding="utf-8")
    legacy_digest = sha(box.legacy)
    requests_before = len(fixture.hits("chatgpt.com"))

    def with_reading(used, fetched):
        row = json.loads(json.dumps(base))
        row["fetched_at"] = fetched
        row["windows"][0]["used_pct"] = used
        return row

    op = ctx["run"].operation("legacy cache read before any migration", "router reads ~/.usage-float/usage-cache.json when nothing else exists")
    _, snapshot = usage_stage(box, op, "codex", "grok")
    op.check("legacy rows are read and fresh", snapshot and snapshot["accounts"]["codex"]["status"] == "fresh" and
             snapshot["accounts"]["grok"]["status"] == "fresh", snapshot and snapshot["accounts"])
    op.check("source is the legacy file", Path(snapshot["cache_path"]).resolve() == box.legacy.resolve(), snapshot.get("cache_path"))

    op = ctx["run"].operation("first collection migrates", "collector seeds the canonical cache from legacy, then writes only canonical")
    result = box.run_command(op, [box.agentcompass, "usage"])
    op.check("collector exits 0", result.returncode == 0, result.stderr + result.stdout)
    document = box.read_cache()
    op.check("canonical exists and carries the legacy codex reading", document["providers"]["codex"]["fetched_at"] == legacy_doc["providers"]["codex"]["fetched_at"],
             document["providers"].get("codex"))
    op.check("carried-over attempt times stopped an immediate duplicate request", len(fixture.hits("chatgpt.com")) == requests_before)
    op.check("legacy-only provider (grok) was carried over", document["providers"].get("grok", {}).get("fetched_at") == legacy_doc["providers"]["grok"]["fetched_at"])
    op.check("the legacy file is byte-identical (never written)", sha(box.legacy) == legacy_digest)
    _, snapshot = usage_stage(box, op, "codex", "grok")
    op.check("router now reads the canonical file", Path(snapshot["cache_path"]).resolve() == box.cache.resolve(), snapshot.get("cache_path"))

    op = ctx["run"].operation("a second writer (standalone Usage Float) on the legacy path", "newest observation wins per provider, both ways")
    legacy_doc["providers"]["codex"] = with_reading(88.0, at(-10))
    box.legacy.write_text(json.dumps(legacy_doc), encoding="utf-8")
    document["providers"]["codex"] = with_reading(12.0, at(-60))
    box.cache.write_text(json.dumps(document), encoding="utf-8")
    _, snapshot = usage_stage(box, op, "codex")
    op.check("a newer legacy reading beats an older canonical one", snapshot["accounts"]["codex"]["windows"][0:1] and
             snapshot["accounts"]["codex"]["windows"][0]["observed_remaining_pct"] == 12.0, snapshot["accounts"]["codex"])
    op.check("the response says it also read the legacy file", str(box.legacy.resolve()) in snapshot.get("also_read_from", []), snapshot.get("also_read_from"))
    document["providers"]["codex"] = with_reading(30.0, at(-5))
    box.cache.write_text(json.dumps(document), encoding="utf-8")
    _, snapshot = usage_stage(box, op, "codex")
    op.check("a newer canonical reading beats an older legacy one", snapshot["accounts"]["codex"]["windows"][0]["observed_remaining_pct"] == 70.0, snapshot["accounts"]["codex"])
    legacy_doc["providers"]["codex"] = with_reading(99.0, at(-5 * 3600))
    box.legacy.write_text(json.dumps(legacy_doc), encoding="utf-8")
    _, snapshot = usage_stage(box, op, "codex")
    op.check("an old legacy reading can neither replace nor stale the canonical one", snapshot["accounts"]["codex"]["status"] == "fresh" and
             snapshot["accounts"]["codex"]["windows"][0]["observed_remaining_pct"] == 70.0, snapshot["accounts"]["codex"])

    op = ctx["run"].operation("explicit override", "ROUTER_USAGE_CACHE names the only file read or written")
    custom = box.root / "custom-cache.json"
    canonical_digest, legacy_digest = sha(box.cache), sha(box.legacy)
    result = box.run_command(op, [box.agentcompass, "usage"], ROUTER_USAGE_CACHE=str(custom))
    op.check("collector writes the override file", result.returncode == 0 and custom.is_file(), result.stderr)
    op.check("canonical and legacy files were not touched", sha(box.cache) == canonical_digest and sha(box.legacy) == legacy_digest)
    _, snapshot = usage_stage(box, op, ROUTER_USAGE_CACHE=str(custom))
    op.check("router reads only the override", Path(snapshot["cache_path"]).resolve() == custom.resolve() and not snapshot.get("also_read_from"), snapshot.get("cache_path"))
    result, snapshot = usage_stage(box, op, ROUTER_USAGE_CACHE=str(box.root / "missing.json"))
    op.check("a missing override does not fall back to other caches", snapshot is None or not snapshot.get("read_ok"), result.stdout[:300])
    cli = box.script(op, "route_judge.py", "usage", "--usage-cache", str(box.legacy))
    op.check("--usage-cache wins over everything", parse_json(cli.stdout)["cache_path"] == str(box.legacy.resolve()), cli.stdout[:200])
    return box


def tray_needs(op: Operation, result: subprocess.CompletedProcess, what: str) -> bool:
    """The tray scenarios are required operations: a tray dependency missing from the interpreter is a failure, not a skip."""
    if result.returncode != 0 and "ModuleNotFoundError" in result.stderr:
        op.check(f"{what}: the tray dependencies are installed in the sandbox interpreter", False, result.stderr.strip().splitlines()[-1])
        return False
    return True


def scenario_tray(ctx):
    """Tray install: helper scripts from the isolated source copy, router skill installed under CODEX_HOME, plugin absent/disabled."""
    box = Sandbox(ctx["base"], "tray", ctx["venv"], ctx["fixture"], ctx["run"])
    box.sign_in()
    fixture = ctx["fixture"]
    fixture.reset()
    skill = box.codex / "skills" / "agent-compass"
    shutil.copytree(box.data / "skills" / "agent-compass", skill)
    repo_tray = ctx["repo"] / "tray"
    state = box.state
    state.mkdir(parents=True, exist_ok=True)
    (state / "plugins.json").write_text(json.dumps({"v": 1, "plugins": {"usage-float": {"enabled": False, "autostart": False}}}), encoding="utf-8")

    op = ctx["run"].operation("tray source copy", "the tray scenarios run from an isolated copy that holds no Usage Float and is not the real checkout")
    op.check("the copy is not the repository", ctx["repo"].resolve() != ROOT.resolve())
    op.check("the copy has no usage-float directory", not (ctx["repo"] / "usage-float").exists())

    op = ctx["run"].operation("tray collector tick (helper script)", "tray/usage_collect.py with the installed skill, Usage Float disabled and absent")
    result = box.run_command(op, [TRAY_PYTHON, "-X", "utf8", repo_tray / "usage_collect.py"], cwd=repo_tray)
    op.check("helper exits 0 and prints the receipt", result.returncode == 0 and "codex" in result.stdout, result.stderr[-400:])
    op.check("cache written at the canonical path", box.cache.is_file())
    op.check("Usage Float plugin state did not matter (disabled, no directory)", not (state / "plugins" / "usage-float").exists())

    op = ctx["run"].operation("tray tick through services", "services.usage_collect_tick starts a real child; the interval gates the next")
    fixture.codex_used = 41.0
    document = box.read_cache()
    document.get("attempts", {}).pop("codex", None)
    document["providers"]["codex"]["fetched_at"] = at(-3600)      # due again, as after a quiet hour
    box.cache.write_text(json.dumps(document), encoding="utf-8")
    code = ("import sys,time,json; sys.path.insert(0,'.'); import services; "
            "print(services.usage_collect_tick(force=True)); p=services._usage_collect['proc']; p.wait(timeout=120); "
            "print(p.returncode); print(services.usage_collect_tick())")
    result = box.py(op, code, isolated=False, cwd=repo_tray, python=TRAY_PYTHON, PYTHONPATH=box.pythonpath(repo_tray))
    if tray_needs(op, result, "services tick"):
        lines = result.stdout.strip().splitlines()
        op.check("a tick starts a child, the child succeeds, an immediate second tick is refused",
                 lines[-3:] == ["True", "0", "False"], result.stdout + result.stderr[-400:])
        _, snapshot = usage_stage(box, op, "codex")
        op.check("reading came from that child (codex 41%)", snapshot and snapshot["accounts"]["codex"]["windows"][0]["used_pct"] == 41.0,
                 snapshot and snapshot["accounts"]["codex"])

    op = ctx["run"].operation("active refresh from the tray", "router_data.refresh_usage forces a re-read past the poll interval")
    fixture.codex_used = 77.0
    code = ("import sys; sys.path.insert(0,'.'); import router_data; "
            "print(router_data.refresh_usage(['codex'], timeout=60))")
    result = box.py(op, code, isolated=False, cwd=repo_tray, python=TRAY_PYTHON, PYTHONPATH=box.pythonpath(repo_tray))
    if tray_needs(op, result, "active refresh"):
        op.check("refresh reports success", result.returncode == 0 and "已刷新额度" in result.stdout, result.stdout + result.stderr[-400:])
        _, snapshot = usage_stage(box, op, "codex")
        op.check("the new value is in the cache (77%) although the last read was seconds ago",
                 snapshot and snapshot["accounts"]["codex"]["windows"][0]["used_pct"] == 77.0, snapshot and snapshot["accounts"]["codex"])

    op = ctx["run"].operation("starting a missing Usage Float", "services.start_usage_float reports 'not installed' instead of failing")
    before = set(usage_float_processes())
    code = "import sys; sys.path.insert(0,'.'); import services; print(services.start_usage_float())"
    result = box.py(op, code, isolated=False, cwd=repo_tray, python=TRAY_PYTHON, PYTHONPATH=box.pythonpath(repo_tray))
    if tray_needs(op, result, "starting a missing Usage Float"):
        op.check("reports not installed", result.returncode == 0 and "未安装" in result.stdout, result.stdout + result.stderr[-300:])
        op.check("no Usage Float process was started", set(usage_float_processes()) <= before, sorted(set(usage_float_processes()) - before))

    op = ctx["run"].operation("capacity statistics sampling", "capacity_stats --once samples the collector cache")
    result = box.run_command(op, [TRAY_PYTHON, "-X", "utf8", repo_tray / "capacity_stats.py", "--once", "--quiet"], cwd=repo_tray)
    capacity_state = state / "capacity-state.json"
    sampled = capacity_state.is_file() and "codex" in json.loads(capacity_state.read_text(encoding="utf-8")).get("usage_samples", {})
    op.check("capacity_stats exits 0 and sampled codex windows from the canonical cache", result.returncode == 0 and sampled, result.stderr[-400:])
    return box


def scenario_network(ctx, box: Sandbox):
    """The isolation the other scenarios rely on: names and sockets are refused, provider hosts reach the fixture, children inherit it."""
    fixture = ctx["fixture"]
    fixture.reset()
    box.netlog.unlink(missing_ok=True)
    op = ctx["run"].operation("network isolation", "sitecustomize refuses real hosts and routes the two provider hosts to the fixture, in the venv and in a normal interpreter")
    blocked = ("import urllib.request, urllib.error, socket\n"
               "try:\n    urllib.request.urlopen('https://example.com/', timeout=5); print('REACHED')\n"
               "except urllib.error.URLError as exc:\n    print('BLOCKED urlopen', 'sandbox' in str(exc.reason))\n"
               "try:\n    socket.getaddrinfo('example.com', 80); print('REACHED')\n"
               "except OSError as exc:\n    print('BLOCKED dns', 'sandbox' in str(exc))\n"
               "try:\n    socket.socket(socket.AF_INET).connect(('93.184.216.34', 80)); print('REACHED')\n"
               "except OSError as exc:\n    print('BLOCKED connect', 'sandbox' in str(exc))\n")
    routed = ("import urllib.request, json\n"
              "print('ROUTED', json.load(urllib.request.urlopen('https://chatgpt.com/backend-api/wham/usage', timeout=10)).get('plan_type'))\n")
    for label, python, isolated in (("sandbox venv interpreter", box.python, True), ("normal interpreter on PYTHONPATH", TRAY_PYTHON, False)):
        result = box.py(op, blocked, isolated=isolated, python=python, PYTHONPATH=box.pythonpath())
        op.check(f"[{label}] a real host (urlopen, name lookup) and a raw outbound socket are all refused", "REACHED" not in result.stdout and
                 all(f"BLOCKED {kind} True" in result.stdout for kind in ("urlopen", "dns", "connect")), result.stdout + result.stderr[-300:])
        result = box.py(op, routed, isolated=isolated, python=python, PYTHONPATH=box.pythonpath())
        op.check(f"[{label}] a provider host is answered by the fixture", "ROUTED plus" in result.stdout, result.stdout + result.stderr[-300:])
    nested = ("import subprocess, sys\n"
              "child = subprocess.run([sys.executable, '-X', 'utf8', '-c', sys.argv[1]], capture_output=True, text=True)\n"
              "print(child.stdout, child.stderr[-200:])\n")
    result = box.run_command(op, [TRAY_PYTHON, "-X", "utf8", "-c", nested, blocked], PYTHONPATH=box.pythonpath())
    op.check("a child process started by a normal interpreter inherits the isolation", "REACHED" not in result.stdout and
             "BLOCKED urlopen True" in result.stdout, result.stdout + result.stderr[-300:])


def scenario_usage_float(ctx, tray_box: Sandbox):
    """Real Usage Float code, when a checkout exists: standalone writer, hosted redirect, uninstall."""
    source = ROOT / "usage-float"
    if not (source / "usage_float.py").is_file():
        ctx["run"].not_executed("Usage Float installed / hosted / uninstalled", "no usage-float checkout next to this repository")
        return
    box = Sandbox(ctx["base"], "withuf", ctx["venv"], ctx["fixture"], ctx["run"])
    box.sign_in()
    fixture = ctx["fixture"]
    fixture.reset()
    plugin = box.state / "plugins" / "usage-float"
    for path in source.glob("*.py"):
        if not path.name.startswith("test_"):
            plugin.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, plugin / path.name)
    (plugin / ".git").mkdir()
    repo_tray = ctx["repo"] / "tray"
    skill = box.codex / "skills" / "agent-compass"
    shutil.copytree(box.data / "skills" / "agent-compass", skill)

    op = ctx["run"].operation("Usage Float standalone (old behaviour)", "usage_float.py once writes the legacy cache; the router still reads it")
    result = box.run_command(op, [box.python, "-X", "utf8", plugin / "usage_float.py", "once"], cwd=plugin)
    op.check("Usage Float 'once' runs its own collection against the fixture", result.returncode == 0 and box.legacy.is_file(), result.stdout[-300:] + result.stderr[-400:])
    legacy_digest = sha(box.legacy)
    _, snapshot = usage_stage(box, op, "codex")
    op.check("router reads the legacy cache Usage Float wrote", snapshot and snapshot["accounts"]["codex"]["status"] == "fresh" and
             Path(snapshot["cache_path"]).resolve() == box.legacy.resolve(), snapshot)

    op = ctx["run"].operation("Usage Float hosted by the tray", "share_cache redirects its CACHE_PATH; its writes land in the shared cache")
    # Negative control: the same fetch WITHOUT share_cache lands in Usage Float's own legacy file and leaves the shared cache alone.
    # That is the difference the assertions below look for; if the control did not show it, they would prove nothing.
    fixture.codex_used = 33.0
    canonical_before = sha(box.cache)
    control = box.py(op, "import usage_float as native; native.fetch_all(['codex'], force=True)", isolated=False, cwd=plugin,
                     PYTHONPATH=box.pythonpath(plugin))
    legacy_codex = [w["used_pct"] for w in box.read_cache(box.legacy)["providers"]["codex"]["windows"]] if box.legacy.is_file() else []
    op.check("control: an un-hosted Usage Float writes its own legacy file (33%) and not the shared cache",
             control.returncode == 0 and sha(box.legacy) != legacy_digest and 33.0 in legacy_codex and sha(box.cache) == canonical_before,
             control.stderr[-300:] + str(legacy_codex))
    legacy_digest = sha(box.legacy)
    fixture.codex_used = 55.0
    code = ("import sys; sys.path.insert(0,'.'); import usage_float_host as host, usage_float as native; "
            "print(host.share_cache(native)); native.fetch_all(['codex'], force=True)")
    result = box.py(op, code, isolated=False, cwd=repo_tray, python=TRAY_PYTHON, PYTHONPATH=box.pythonpath(repo_tray, plugin))
    if tray_needs(op, result, "hosted Usage Float"):
        op.check("share_cache returns the canonical path", result.returncode == 0 and str(box.cache) in result.stdout, result.stdout + result.stderr[-400:])
        hosted = [w["used_pct"] for w in box.read_cache()["providers"]["codex"]["windows"]]
        op.check("hosted Usage Float wrote the shared cache (55%) and left the legacy file byte-identical",
                 sha(box.legacy) == legacy_digest and 55.0 in hosted, {"legacy_unchanged": sha(box.legacy) == legacy_digest, "canonical_windows": hosted})
        _, snapshot = usage_stage(box, op, "codex")
        op.check("router sees the hosted write as fresh", snapshot and snapshot["accounts"]["codex"]["status"] == "fresh" and
                 snapshot["accounts"]["codex"]["windows"][0]["used_pct"] in (55.0, 27.5), snapshot and snapshot["accounts"]["codex"])

    op = ctx["run"].operation("uninstalling Usage Float", "removing the plugin and ~/.usage-float leaves collection, reading and routing working")
    shutil.rmtree(plugin.parent)
    shutil.rmtree(box.home / ".usage-float")
    fixture.codex_used = 60.0
    document = box.read_cache()
    document.get("attempts", {}).pop("codex", None)
    document["providers"]["codex"]["fetched_at"] = at(-3600)      # due again
    box.cache.write_text(json.dumps(document), encoding="utf-8")
    result = box.run_command(op, [box.agentcompass, "usage"])
    _, snapshot = usage_stage(box, op, "codex", "claude")
    op.check("collector and reader work with Usage Float and its data gone", result.returncode == 0 and
             snapshot["accounts"]["codex"]["status"] == "fresh" and snapshot["accounts"]["codex"]["windows"][0]["used_pct"] == 60.0, snapshot)
    op.check("nothing recreated ~/.usage-float", not (box.home / ".usage-float").exists())


def scenario_hook_and_setup(ctx, box: Sandbox):
    """Gate hook decision from the collector cache; onboarding's collection step; dry-run setup."""
    fixture = ctx["fixture"]
    skill_root = box.data / "skills" / "agent-compass"
    hook = box.data / "setup" / "router_gate_hook.py"

    op = ctx["run"].operation("gate hook quota check", "installed router_gate_hook blocks an exhausted account read from the collector cache")
    document = box.read_cache()
    row = document["providers"]["codex"]
    row["windows"] = [{"id": "five_hour", "label": "5h", "used_pct": 100.0, "resets_at": at(3600)}]
    row["fetched_at"] = at(-5)
    row["stale"] = False
    box.cache.write_text(json.dumps(document), encoding="utf-8")
    code = ("import importlib.util as u, sys; s=u.spec_from_file_location('gate', sys.argv[1]); m=u.module_from_spec(s); "
            "s.loader.exec_module(m); print(m.check_account_quota_exhausted('codex')); print(m.check_account_quota_exhausted('claude'))")
    result = box.run_command(op, [box.python, "-I", "-X", "utf8", "-c", code, hook], ROUTER_SKILL_ROOT=str(skill_root))
    lines = result.stdout.strip().splitlines()
    op.check("exhausted codex is blocked, claude is not", result.returncode == 0 and "[Router Gate]" in (lines[0] if lines else "") and
             lines[-1] == "None", result.stdout + result.stderr[-300:])

    op = ctx["run"].operation("agentcompass setup", "setup --dry-run on the sandbox home")
    result = box.run_command(op, [box.agentcompass, "setup", "--yes", "--dry-run", "--home", box.home, "--no-hooks"], stdin="")
    op.check("dry-run setup exits 0 and writes nothing", result.returncode == 0 and not (box.state / "accounts-override.json").exists(),
             result.stdout[-300:] + result.stderr[-300:])

    op = ctx["run"].operation("onboarding collection step", "setup's 'run one usage collection' with the router's collector")
    fresh_home = ctx["base"] / "onboard-home"
    fresh = Sandbox(ctx["base"], "onboard-home", ctx["venv"], fixture, ctx["run"])
    fresh.sign_in()
    code = ("import importlib.util as u, sys, json; s=u.spec_from_file_location('onboard', sys.argv[1]); m=u.module_from_spec(s); "
            "sys.modules['onboard']=m; s.loader.exec_module(m); from pathlib import Path; print(json.dumps(m.collect_usage_once(Path(sys.argv[2]))))")
    result = fresh.run_command(op, [fresh.python, "-X", "utf8", "-c", code, box.data / "setup" / "onboard.py", fresh.home])
    outcome = parse_json(result.stdout) or {}
    op.check("collection ran and produced the canonical cache", outcome.get("ok") is True and fresh.cache.is_file(), result.stdout[-300:] + result.stderr[-400:])
    del fresh_home


def scenario_frozen(ctx, skip: bool):
    """The packaged tray executable: built from the isolated copy, then its helper entry is run like the tray runs it."""
    if skip:
        ctx["run"].not_executed("PyInstaller tray executable (--run-component entries)", "skipped by --skip-frozen")
        return
    repo, base = ctx["repo"], ctx["base"]
    op = ctx["run"].operation("PyInstaller tray build", "packaging/agentcompass-tray.spec built in the isolated source copy")
    dist, work = base / "frozen-dist", base / "frozen-work"
    build = subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--distpath", str(dist), "--workpath", str(work),
                            str(repo / "packaging" / "agentcompass-tray.spec")], cwd=str(repo), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=1500)
    op.log([sys.executable, "-m", "PyInstaller", "...", "packaging/agentcompass-tray.spec"], "isolated source copy", build)
    exe = dist / "AgentCompass" / ("AgentCompass.exe" if os.name == "nt" else "AgentCompass")
    op.check("PyInstaller builds the one-folder tray (PyInstaller and the tray extra are installed)", build.returncode == 0 and exe.is_file(),
             (build.stderr or build.stdout)[-500:])
    if not exe.is_file():
        return
    box = Sandbox(base, "frozen", ctx["venv"], ctx["fixture"], ctx["run"])
    fixture = ctx["fixture"]
    fixture.reset()
    fixture.proxy_spend = 2.5
    asked_before = len(fixture.requests)
    shutil.copytree(ctx["wheel_skill"], box.codex / "skills" / "agent-compass")       # the router comes from the wheel, never frozen
    box.state.mkdir(parents=True, exist_ok=True)
    # No provider credentials in this home: the only account that can be read is a LiteLLM-style proxy on the local fixture, so the
    # executable (which has no network guard) has nothing to send anywhere else. A dead proxy address backs that up.
    (box.state / "usage-config.json").write_text(json.dumps({"litellm_proxies": {"sandbox-proxy": {
        "endpoint": fixture.url + "/litellm", "api_key": "sandbox-key", "label": "sandbox proxy"}}}), encoding="utf-8")
    dead = {"HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9", "ALL_PROXY": "http://127.0.0.1:9", "NO_PROXY": "127.0.0.1"}

    op = ctx["run"].operation("frozen usage_collect", "AgentCompass.exe --run-component usage_collect: the tray's collector child, packaged")
    result = box.run_command(op, [exe, "--run-component", "usage_collect"], cwd=box.root, **dead)
    receipt = parse_json(result.stdout) if result.stdout.strip() else None
    op.check("the packaged component exits 0 and prints the collector receipt", result.returncode == 0 and receipt and
             "sandbox-proxy" in receipt.get("providers", {}), result.stdout[-300:] + result.stderr[-400:])
    document = box.read_cache() if box.cache.is_file() else {"providers": {}}
    row = document["providers"].get("sandbox-proxy", {})
    op.check("it wrote the canonical cache with a fresh sandbox-proxy reading", row.get("available") is True and bool(row.get("fetched_at")), row)
    op.check("it asked only the local fixture (one embeddings probe, nothing for codex or claude)",
             [hit["host"] for hit in fixture.requests[asked_before:]] == ["litellm"] and
             fixture.requests[asked_before]["path"].endswith("/embeddings"), fixture.requests[asked_before:])
    op.check("the legacy Usage Float location was not created", not box.legacy.exists())
    fixture.proxy_spend = 6.0
    document["providers"]["sandbox-proxy"]["fetched_at"] = at(-3600)
    document.get("attempts", {}).pop("sandbox-proxy", None)
    box.cache.write_text(json.dumps(document), encoding="utf-8")
    again = box.run_command(op, [exe, "--run-component", "usage_collect"], cwd=box.root, **dead)
    _, snapshot = usage_stage(box, op, "sandbox-proxy")
    op.check("a second packaged run refreshes the reading and the router reads it",
             again.returncode == 0 and snapshot and snapshot["accounts"]["sandbox-proxy"]["status"] == "fresh", again.stderr[-300:])

    op = ctx["run"].operation("frozen capacity_stats", "AgentCompass.exe --run-component capacity_stats --once: sampling the packaged collector's cache")
    result = box.run_command(op, [exe, "--run-component", "capacity_stats", "--once", "--quiet"], cwd=box.root, **dead)
    capacity_state = box.state / "capacity-state.json"
    samples = json.loads(capacity_state.read_text(encoding="utf-8")).get("usage_samples", {}) if capacity_state.is_file() else {}
    op.check("the packaged sampler exits 0 and sampled the proxy account", result.returncode == 0 and "sandbox-proxy" in samples,
             result.stderr[-400:] + str(sorted(samples)))

    op = ctx["run"].operation("frozen component dispatch", "unknown names are refused, and the dispatch runs before the tray starts anything")
    result = box.run_command(op, [exe, "--run-component", "no_such_component"], cwd=box.root, **dead)
    op.check("an unknown component exits 2 without starting the tray", result.returncode == 2 and "unknown component" in (result.stderr + result.stdout),
             result.stderr[-200:])


def scenario_payload(ctx):
    op = ctx["run"].operation("release payload", "the wheel carries the collector modules and nothing from Usage Float")
    import zipfile
    names = zipfile.ZipFile(ctx["wheel"]).namelist()
    scripts = "agentcompass/data/skills/agent-compass/scripts/"
    missing = [m for m in ("usage_core.py", "usage_cache.py", "usage_collector.py", "usage_magpie.py", "venice_usage.py") if scripts + m not in names]
    op.check("wheel carries every collector module", not missing, missing)
    op.check("wheel contains no Usage Float file", not [n for n in names if "usage_float" in n or "usage-float/" in n], [n for n in names if "usage_float" in n][:5])
    if ctx.get("fixture_built"):
        check = subprocess.run([sys.executable, str(ctx["repo"] / "packaging" / "check_payload.py"), "--wheel", str(ctx["wheel"])],
                               cwd=str(ctx["repo"]), capture_output=True, text=True)
        op.log([sys.executable, "packaging/check_payload.py", "--wheel", ctx["wheel"]], "fixture repo", check)
        op.check("check_payload: every wheel file is tracked in the fixture repo", check.returncode == 0, check.stdout + check.stderr)
    else:
        ctx["run"].not_executed("check_payload on the wheel", "the wheel was given, not built here, so there is no matching git copy to compare it with")
    ctx["run"].not_executed("Inno Setup installer, startup entry / scheduled task, Tk windows", "not run here: they write the registry and Task Scheduler of the real user and need a desktop; packaging/windows_sandbox_check.py executes them in a Windows Sandbox")


# ---------------------------------------------------------------------------------------------------------------------
# harness


MINIMAL_SOURCE = ("tray/", "agentcompass/", "skills/", "setup/", "packaging/", "pyproject.toml", "README.md", "llms.txt")


def build_fixture_repo(base: Path, minimal: bool = False) -> Path:
    """Copy the working tree (tracked + untracked, not ignored) into a throw-away git repo so a build sees uncommitted work.

    ``minimal`` is the copy used when the product comes from a given wheel: only what the tray, helper and source-entry scenarios
    need. Either way the copy never holds a usage-float directory and never aliases the real checkout.
    """
    listing = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-co", "--exclude-standard", "-z"], capture_output=True, check=True).stdout
    repo = base / ("source" if minimal else "repo")
    for raw in filter(None, listing.split(b"\0")):
        rel = raw.decode("utf-8")
        source = ROOT / rel
        if not source.is_file() or rel.startswith(("usage-float/", "dist/", "scratch/", "pip/", "tools/")):
            continue
        if minimal and (not rel.startswith(MINIMAL_SOURCE) or Path(rel).name.startswith("test_")):
            continue
        destination = repo / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    git = ["git", "-C", str(repo), "-c", "user.name=sandbox", "-c", "user.email=sandbox@example.invalid"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "fixture"], check=True)
    return repo


def build_wheel(repo: Path, out: Path) -> Path:
    subprocess.run([sys.executable, "-m", "build", "--wheel", "--no-isolation", "-o", str(out)], cwd=str(repo), check=True,
                   capture_output=True)
    return next(out.glob("*.whl"))


def registry_clean() -> list[str]:
    if os.name != "nt":
        return []
    import winreg
    names = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            index = 0
            while True:
                name = winreg.EnumValue(key, index)[0]
                if name.upper().startswith(("ROUTER_", "AGENTCOMPASS_")):
                    names.append(name)
                index += 1
    except OSError:
        pass
    return names


def real_state_snapshot() -> dict:
    home = Path.home()
    files = [home / ".agent-compass" / n for n in ("usage-cache.json", "usage-activity.json", "usage-collector.json", "usage-collector.log")]
    files.append(home / ".usage-float" / "usage-cache.json")
    return {str(p): (p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None for p in files}


def usage_float_processes() -> list[int]:
    """Process ids of a running Usage Float on this machine (Windows); empty when none or when it cannot be told."""
    if os.name != "nt":
        return []
    command = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'usage_float' -and $_.ProcessId -ne $PID "
               "-and $_.Name -notmatch 'powershell|pwsh' } | ForEach-Object { $_.ProcessId }")
    try:
        done = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(line) for line in done.stdout.split() if line.isdigit()]


def sandbox_processes(base: Path) -> list[str]:
    """Process ids whose command line mentions the sandbox directory (Windows); empty elsewhere or when it cannot be told."""
    if os.name != "nt":
        return []
    needle = str(base).replace("'", "''")
    command = (f"Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like '*{needle}*' -and $_.ProcessId -ne {os.getpid()} "
               "-and $_.ProcessId -ne $PID } | ForEach-Object { \"$($_.ProcessId):$($_.Name)\" }")
    try:
        done = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return []
    return done.stdout.split()


def write_reports(run: Run, started: float, notes: dict):
    summary = {"finished": dt.datetime.now().isoformat(timespec="seconds"), "seconds": round(time.time() - started, 1),
               "failures": run.failures, "python": sys.version.split()[0], "notes": notes, "operations": run.operations}
    (run.report / "report.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    lines = ["# Sandbox check", "", f"failures: **{run.failures}**, {summary['seconds']} s", "",
             "| id | covers | operation | status | assertions | commands |", "|---|---|---|---|---|---|"]
    for op in run.operations:
        passed = sum(1 for a in op["assertions"] if a["ok"])
        status = op["status"] if op["status"] != "executed" else ("PASS" if passed == len(op["assertions"]) else "FAIL")
        detail = f" ({op['reason']})" if op.get("reason") else ""
        lines.append(f"| {op['id']} | {op['covers']} | {op['title']} | {status}{detail} | {passed}/{len(op['assertions'])} | {len(op['commands'])} |")
    lines += ["", "## Assertions", ""]
    for op in run.operations:
        for a in op["assertions"]:
            lines.append(f"- {'ok  ' if a['ok'] else 'FAIL'} {op['id']} {a['name']}" + ("" if a["ok"] else f" — {a['detail']}"))
    (run.report / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wheel", type=Path, help="Install this wheel instead of building one from the working tree.")
    parser.add_argument("--report", type=Path, default=ROOT / ".route-briefs" / "sandbox-report",
                        help="Parent directory of the per-run report directories (nothing in it is deleted).")
    parser.add_argument("--keep", action="store_true", help="Keep the sandbox directory for inspection.")
    parser.add_argument("--skip-frozen", action="store_true", help="Do not build and run the PyInstaller tray (recorded as not executed).")
    args = parser.parse_args(argv)
    names = registry_clean()
    if names:
        print("refusing to run: Windows user variables the router reads exist in HKCU\\Environment: " + ", ".join(names), file=sys.stderr)
        return 2
    started = time.time()
    args.report.mkdir(parents=True, exist_ok=True)
    run_dir = args.report / time.strftime("run-%Y%m%d-%H%M%S")
    run = Run(run_dir)
    before = real_state_snapshot()
    base = Path(tempfile.mkdtemp(prefix="agentcompass-sandbox-"))
    notes = {"sandbox": str(base)}
    try:
        if args.wheel:
            wheel, built = args.wheel.resolve(), False
            repo = build_fixture_repo(base, minimal=True)
        else:
            repo = build_fixture_repo(base)
            wheel, built = build_wheel(repo, base / "dist"), True
        notes["wheel"] = str(wheel)
        venv = base / "venv"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True)
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run([str(python), "-m", "pip", "install", "--no-index", "--no-deps", "--quiet", str(wheel)], check=True, capture_output=True)
        sites = (venv / "Lib" / "site-packages") if os.name == "nt" else next((venv / "lib").glob("python*/site-packages"))
        global NETGUARD
        NETGUARD = base / "netguard"
        NETGUARD.mkdir()
        with Fixture() as fixture:
            write_netguard([sites, NETGUARD], fixture.url, [fixture.server.server_address[1]], base / "net-default.jsonl")
            ctx = {"base": base, "venv": venv, "fixture": fixture, "run": run, "wheel": wheel, "repo": repo, "fixture_built": built}
            clean = scenario_clean_install(ctx)
            ctx["wheel_skill"] = clean.data / "skills" / "agent-compass"
            scenario_entry_and_docs(ctx, clean)
            scenario_freshness(ctx, clean)
            scenario_legacy(ctx)
            tray = scenario_tray(ctx)
            scenario_network(ctx, tray)
            scenario_usage_float(ctx, tray)
            scenario_hook_and_setup(ctx, tray)
            scenario_frozen(ctx, args.skip_frozen)
            scenario_payload(ctx)
        op = run.operation("real machine untouched", "the user's own cache, activity and receipt files did not change")
        after = real_state_snapshot()
        # Usage Float, when it runs on this machine, rewrites its own cache and the activity file every few minutes; those two
        # are only held to "unchanged" when no Usage Float process is running. The router's own files never may change.
        float_owned = {str(Path.home() / ".usage-float" / "usage-cache.json"), str(Path.home() / ".agent-compass" / "usage-activity.json")}
        running = usage_float_processes()
        changed = sorted(k for k in before if before[k] != after[k])
        op.check("the router's own real files (~/.agent-compass cache, receipt, log) are unchanged",
                 not [k for k in changed if k not in float_owned], {"changed": changed})
        op.check("Usage Float's files changed only while a real Usage Float was running",
                 not [k for k in changed if k in float_owned] or bool(running), {"changed": changed, "usage_float_processes": running})
        op = run.operation("no process left behind", "nothing started from the sandbox is still running")
        left = sandbox_processes(base)
        op.check("no process still runs from the sandbox directory", not left, left)
        for entry in left:
            subprocess.run(["taskkill", "/F", "/T", "/PID", entry.split(":")[0]], capture_output=True)
    finally:
        write_reports(run, started, notes)
        (args.report / "latest.txt").write_text(run_dir.name + "\n", encoding="utf-8")
        if not args.keep:
            shutil.rmtree(base, ignore_errors=True)
    print(f"sandbox check: {run.failures} failed assertion(s); report {run_dir / 'report.md'}")
    return run.failures


if __name__ == "__main__":
    raise SystemExit(main())
