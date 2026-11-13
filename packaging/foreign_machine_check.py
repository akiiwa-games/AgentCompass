"""Verify that a clean foreign-machine install of AgentCompass and usage-float works end-to-end.

    python -X utf8 packaging/foreign_machine_check.py [--wheel X.whl] [--report DIR] [--keep]

Designed the pstack way (behavior over implementation, prove it works from packaged artifacts):
  - Builds a wheel from an isolated git copy of the working tree (or takes --wheel) and installs it into a fresh venv.
  - Clones usage-float from an isolated git copy into the foreign profile's ~/.agent-compass/plugins/usage-float
    via `agentcompass setup --yes --usage-float` (with AGENTCOMPASS_USAGE_FLOAT_REPO).
  - Never touches the real user profile, real credentials, or external network (all HTTP goes through sandbox_check's
    sitecustomize netguard to local fixture servers; all CLIs are fixture executables in a throwaway PATH).
  - Covers every failure hit when syncing to a second host (LITTLESPRING / LS) and every 0.3.6 behavior:
      F01  Stale install & public reference data channel (shipped-schema fetch, sha256 & corrupt JSON rejection, schema-1 reader sees the shipped schema, opt-out, doctor line)
      F02  Fleet parity on a clean install with no accounts.local.json
      F03  Classifier cascade across families and multi-home accounts (jev absent, ~/.claude signed out, ~/.claude-2 signed in)
      F04  Quota, window scope, gate parity, control-message exemption, discovered model management, no twin bundles, and agy 3p pool-prior capacity
      F05  Antigravity plan tier read from Google loadCodeAssist with keyring token + cache + opt-out
      F06  Magpie pools (largest plan ×N, stale readings re-read via POST /v1/magpie/quotas/refresh, N×(100-mean) router & gate capacity, masked hover members)
      F07  Stranger account topologies + setup/discovery/uninstall home parity (only Claude; only Codex; Magpie Claude; agy installed not signed in; no Paseo; Paseo provider with extends chain & custom CLAUDE_CONFIG_DIR)
      F08  Tray account cards & hover + usage-float standalone & hosted rows, ×N pool left_pct, week-only titles, and masked member hover
      F09  Real machine untouched & no background process left behind
      F10  Evidence-calibrated routing after 0.3.6 (default checkbox from public data, metered and never-billed
           accounts as a last resort, pool reset); each check is also shown to fail once against broken data in the
           throwaway profile
      F11  Oh My Pi OAuth subscription logins (agent.db): shared quota with the native account, omp-only accounts,
           metered API keys, disabled/corrupt/locked credentials; each check is also shown to fail once
      F12  Tray version display, one-click self-update (old wheel upgraded to locally built wheel in a foreign profile,
           sha256 digest verification, dirty git refusal, data --refresh), and single-flight tray restart
      F13  Product layout without the dev checkout: a checkout-era profile (plugin clones in the checkout, a junction
           from ~/.agent-compass/plugins) migrates to ~/.agent-compass/plugins with local changes kept; EasyTier's home
           is %ProgramData%/AgentCompass/easytier from frozen- and wheel-shaped installs (checkout node until migrated);
           grok-bot's CLI and the Paseo bridge's node_modules live under the state directory for a wheel install
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sandbox_check as sc  # noqa: E402
import zero_dispatch_check as zd  # noqa: E402

ROOT = sc.ROOT
USAGE_FLOAT_SRC = ROOT / "usage-float"
# The data schema this checkout ships (router-internals, Public reference data); checks derive from it, never a literal.
SHIPPED_SCHEMA = json.loads((ROOT / "skills" / "agent-compass" / "references" / "public-data.json")
                            .read_text(encoding="utf-8"))["schema_version"]

FOREIGN_ALLOWED_HOSTS = (
    "chatgpt.com",
    "api.anthropic.com",
    "raw.githubusercontent.com",
    "magpie.fixture.local",
    "cloudcode-pa.googleapis.com",
    "daily-cloudcode-pa.googleapis.com",
)


class ForeignFixtureServer:
    """Local HTTP fixture serving provider quotas, GitHub raw public-data, Magpie gateway pools, and Google Cloud Code."""

    def __init__(self, shipped_references: Path):
        self.shipped_references = shipped_references
        self.requests: list[dict] = []
        self.codex_used_5h = 15.0
        self.codex_used_7d = 20.0
        self.claude_used_5h = 25.0
        self.claude_used_7d = 30.0
        # Public reference data fixture state
        self.public_data_mode = "valid_update"  # valid_update | bad_sha | bad_json | future_schema | error
        # Bearer token -> OAuth profile organization. The Claude plan is read only from this live profile.
        self.claude_profiles: dict[str, dict] = {}
        # plan-templates.json codex.pro has only the weekly `usage` window. A five-hour reading belongs to Plus.
        self.codex_plan = "pro"
        self.updated_accounts_bytes = b""
        self.updated_manifest_bytes = b""
        self.updated_version = ""
        self._prepare_public_data()
        # Magpie gateway pool state: two pro accounts (one fresh, one stale 5h old) + one smaller free account
        now = dt.datetime.now(dt.timezone.utc)
        self.now_iso = now.isoformat()
        self.stale_iso = (now - dt.timedelta(hours=5)).isoformat()
        self.reset_5h = (now + dt.timedelta(hours=3)).isoformat()
        self.reset_7d = (now + dt.timedelta(days=4)).isoformat()
        self.magpie_refreshed_users: list[tuple[str, str]] = []
        self.magpie_bob_fresh = False
        self.magpie_bob_reset_7d = None   # F10 sets a later weekly reset on one pool member
        # Google loadCodeAssist state
        self.google_tier_id = "g1-ultra-tier"
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status: int, body, headers: dict | None = None, raw_bytes: bytes | None = None):
                raw = raw_bytes if raw_bytes is not None else json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                host, _, rest = self.path.lstrip("/").partition("/")
                path, _, query = rest.partition("?")
                path = "/" + path
                outer.requests.append({
                    "method": "GET", "host": host, "path": path, "query": query,
                    "authorization": self.headers.get("Authorization"),
                })
                now = dt.datetime.now(dt.timezone.utc)
                if host == "chatgpt.com" and path == "/backend-api/wham/usage":
                    return self._reply(200, outer.codex_usage_body(now))
                if host == "api.anthropic.com" and path == "/api/oauth/profile":
                    token = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
                    org = outer.claude_profiles.get(token)
                    if org is None:
                        return self._reply(404, {"error": "unknown claude token"})
                    return self._reply(200, {"organization": org})
                if host == "api.anthropic.com" and path == "/api/oauth/usage":
                    return self._reply(200, {
                        "five_hour": {"utilization": outer.claude_used_5h, "resets_at": (now + dt.timedelta(hours=2)).isoformat()},
                        "seven_day": {"utilization": outer.claude_used_7d, "resets_at": (now + dt.timedelta(days=3)).isoformat()},
                    })
                if host == "raw.githubusercontent.com":
                    return outer._handle_github_raw(self, path)
                if host == "magpie.fixture.local" and path == "/v1/magpie/quotas":
                    if self.headers.get("Authorization") != "Bearer magpie-gateway-token":
                        return self._reply(401, {"error": "unauthorized"})
                    return self._reply(200, {"data": outer._magpie_rows()})
                return self._reply(404, {"error": f"unknown GET {host}{path}"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                _ = self.rfile.read(length) if length else b""
                host, _, rest = self.path.lstrip("/").partition("/")
                path, _, query = rest.partition("?")
                path = "/" + path
                outer.requests.append({
                    "method": "POST", "host": host, "path": path, "query": query,
                    "authorization": self.headers.get("Authorization"),
                })
                if host == "magpie.fixture.local" and path == "/v1/magpie/quotas/refresh":
                    if self.headers.get("Authorization") != "Bearer magpie-gateway-token":
                        return self._reply(401, {"error": "unauthorized"})
                    params = urllib.parse.parse_qs(query)
                    provider = (params.get("provider") or [""])[0]
                    user = (params.get("user") or [""])[0]
                    outer.magpie_refreshed_users.append((provider, user))
                    if user == "bob@example.com":
                        outer.magpie_bob_fresh = True
                    return self._reply(200, {"ok": True})
                if host in ("cloudcode-pa.googleapis.com", "daily-cloudcode-pa.googleapis.com") and path == "/v1internal:loadCodeAssist":
                    return self._reply(200, {
                        "currentTier": {"id": outer.google_tier_id, "name": "Google AI Ultra"},
                        "paidTier": {"id": outer.google_tier_id, "name": "Google AI Ultra"},
                    })
                if self.path.endswith("/chat/completions"):
                    profile = {
                        "multi_task": False, "task_count": "1", "wants_fast": False, "read_only_review": False,
                        "work_kind": "general_implementation", "reasoning_depth": 2, "code_complexity": 1,
                        "axis_demand": {"divergence": 1, "coding": 2, "aesthetics": 0, "tool_operation": 0}, "needs_visual": False, "needs_gui": False, "local_only": True, "expected_hours": 0.5,
                        "hours_confidence": "medium", "urgency": "normal", "interaction_style": "engineering",
                        "reason": "foreign machine fixture classification",
                    }
                    return self._reply(200, {"choices": [{"message": {"content": json.dumps(profile)}}]})
                return self._reply(404, {"error": f"unknown POST {host}{path}"})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def codex_usage_body(self, now) -> dict:
        """Codex usage payload for the signed-in plan.

        Pro's template window is the weekly `usage` window (plan-templates.json). Reporting a
        five-hour window on that plan is an observation the template does not price, and the
        account then stays out of automatic routing (capacity_unknown). Plus and the other
        plans that declare `five_hour` get the primary window as well.
        """
        rate = {
            "secondary_window": {
                "used_percent": self.codex_used_7d,
                "limit_window_seconds": 604800,
                "reset_at": int((now + dt.timedelta(days=4)).timestamp()),
            }
        }
        if self.codex_plan != "pro":
            rate["primary_window"] = {
                "used_percent": self.codex_used_5h,
                "limit_window_seconds": 18000,
                "reset_at": int((now + dt.timedelta(hours=3)).timestamp()),
            }
        return {"plan_type": self.codex_plan, "rate_limit": rate}

    def _prepare_public_data(self):
        accounts_doc = json.loads((self.shipped_references / "accounts.json").read_text(encoding="utf-8"))
        # Add a benign public-data note so we can prove the fetched copy is active and distinct from bundled bytes
        accounts_doc["public_channel_verified"] = "0.3.6-fetched"
        self.updated_accounts_bytes = (json.dumps(accounts_doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        manifest = json.loads((self.shipped_references / "public-data.json").read_text(encoding="utf-8"))
        files = dict(manifest.get("files") or {})
        files["accounts.json"] = {
            "sha256": hashlib.sha256(self.updated_accounts_bytes).hexdigest(),
            "bytes": len(self.updated_accounts_bytes),
        }
        schema_ver = manifest["schema_version"]
        public_files_order = tuple(files.keys())
        lines = [f"schema {schema_ver}"] + [f"{name} {files[name]['sha256']}" for name in public_files_order]
        self.updated_version = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:12]
        manifest = {
            "schema_version": schema_ver,
            "version": self.updated_version,
            "published_at": "2026-11-30T12:00:00Z",
            "files": files,
        }
        self.updated_manifest_bytes = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    def _handle_github_raw(self, handler, path: str):
        if self.public_data_mode == "error":
            return handler._reply(503, {"error": "github unavailable"})
        if path.endswith("/references/public-data.json"):
            if self.public_data_mode == "future_schema":
                manifest = json.loads(self.updated_manifest_bytes.decode("utf-8"))
                manifest["schema_version"] = 99
                return handler._reply(200, None, raw_bytes=json.dumps(manifest).encode("utf-8"))
            if self.public_data_mode == "bad_json":
                bad = b'{"not-valid-json"\n'
                manifest = json.loads(self.updated_manifest_bytes.decode("utf-8"))
                manifest["files"]["accounts.json"] = {
                    "sha256": hashlib.sha256(bad).hexdigest(),
                    "bytes": len(bad),
                }
                manifest["version"] = "badjson00001"
                manifest["published_at"] = "2026-12-01T00:00:00Z"
                return handler._reply(200, None, raw_bytes=json.dumps(manifest).encode("utf-8"))
            if self.public_data_mode == "bad_sha":
                manifest = json.loads(self.updated_manifest_bytes.decode("utf-8"))
                manifest["files"]["accounts.json"]["sha256"] = "0" * 64
                manifest["version"] = "badsha000001"
                manifest["published_at"] = "2026-12-01T00:00:00Z"
                return handler._reply(200, None, raw_bytes=json.dumps(manifest).encode("utf-8"))
            return handler._reply(200, None, raw_bytes=self.updated_manifest_bytes)
        name = path.rsplit("/references/", 1)[-1] if "/references/" in path else ""
        if name == "accounts.json":
            if self.public_data_mode == "bad_sha":
                return handler._reply(200, None, raw_bytes=self.updated_accounts_bytes + b" ")
            if self.public_data_mode == "bad_json":
                return handler._reply(200, None, raw_bytes=b'{"not-valid-json"\n')
            return handler._reply(200, None, raw_bytes=self.updated_accounts_bytes)
        shipped_file = self.shipped_references / name
        if shipped_file.is_file():
            return handler._reply(200, None, raw_bytes=shipped_file.read_bytes())
        return handler._reply(404, {"error": f"no such reference {name}"})

    def _magpie_rows(self) -> list[dict]:
        bob_read_at = self.now_iso if self.magpie_bob_fresh else self.stale_iso
        bob_5h = 0.0 if self.magpie_bob_fresh else 50.0
        bob_7d = 0.0 if self.magpie_bob_fresh else 50.0
        return [
            {
                "provider": "claude", "kind": "subscription", "user": "alice@example.com",
                "plan": "pro", "readAt": self.now_iso,
                "windows": [
                    {"name": "5 hours", "used": 100.0, "resetsAt": self.reset_5h},
                    {"name": "7 days", "used": 50.0, "resetsAt": self.reset_7d},
                ],
            },
            {
                "provider": "claude", "kind": "subscription", "user": "bob@example.com",
                "plan": "pro", "readAt": bob_read_at,
                "windows": [
                    {"name": "5 hours", "used": bob_5h, "resetsAt": self.reset_5h},
                    {"name": "7 days", "used": bob_7d, "resetsAt": self.magpie_bob_reset_7d or self.reset_7d},
                ],
            },
            {
                "provider": "claude", "kind": "subscription", "user": "charlie@example.com",
                "plan": "free", "readAt": self.now_iso,
                "windows": [
                    {"name": "5 hours", "used": 10.0, "resetsAt": self.reset_5h},
                    {"name": "7 days", "used": 10.0, "resetsAt": self.reset_7d},
                ],
            },
        ]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def write_foreign_netguard(targets, fixture_url: str, ports: list[int], log: Path) -> None:
    config = {
        "proxy": fixture_url,
        "hosts": list(FOREIGN_ALLOWED_HOSTS),
        "ports": sorted(set(ports)),
        "log": str(log),
    }
    for target in targets:
        (Path(target) / "sitecustomize.py").write_text(sc.SITECUSTOMIZE, encoding="utf-8")
        (Path(target) / "sandbox_net.json").write_text(json.dumps(config), encoding="utf-8")


def build_usage_float_fixture_repo(base: Path) -> Path:
    """Create an isolated git repository of usage-float so setup/tray can clone it offline."""
    dest = base / "usage-float-origin"
    listing = subprocess.run(
        ["git", "-C", str(USAGE_FLOAT_SRC), "ls-files", "-c", "-z"],
        capture_output=True, check=True,
    ).stdout
    for raw in listing.split(b"\0"):
        if not raw:
            continue
        rel = Path(raw.decode("utf-8"))
        src = USAGE_FLOAT_SRC / rel
        if src.is_file():
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
    subprocess.run(["git", "init", "-q", str(dest)], check=True)
    subprocess.run(["git", "-C", str(dest), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(dest), "-c", "user.name=fixture", "-c", "user.email=fixture@local",
         "commit", "-q", "-m", "fixture"],
        check=True,
    )
    return dest


def make_profile(ctx: dict, name: str) -> sc.Sandbox:
    return sc.Sandbox(ctx["base"], name, ctx["venv"], ctx["fixture"], ctx["run"])


def sign_in_codex(box: sc.Sandbox, account_id: str = "sandbox-account") -> None:
    box.codex.mkdir(parents=True, exist_ok=True)
    (box.codex / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": sc.FIXTURE_TOKENS["codex"], "account_id": account_id}}),
        encoding="utf-8",
    )


def claude_organization(plan: str) -> dict:
    """Organization block of the live OAuth profile for a signed-in plan.

    usage_core.claude_profile_plan reads organization_type and rate_limit_tier. The login
    file's subscriptionType is not the plan (router-internals: a discovered plan supplies
    the window structure; Claude's plan comes from the profile API).
    """
    key = (plan or "max").strip().lower()
    if key in {"pro", "claude_pro"}:
        return {"organization_type": "claude_pro"}
    if key in {"max20", "20x"}:
        return {"organization_type": "claude_max", "rate_limit_tier": "default_claude_max_20x"}
    if key in {"max5", "5x"}:
        return {"organization_type": "claude_max", "rate_limit_tier": "default_claude_max_5x"}
    return {"organization_type": "claude_max"}


def remember_claude_plan(box: sc.Sandbox, token: str, plan: str) -> None:
    box.fixture.claude_profiles[token] = claude_organization(plan)


def sign_in_claude(box: sc.Sandbox, plan: str = "max", token: str | None = None) -> None:
    token = token or sc.FIXTURE_TOKENS["claude"]
    box.claude.mkdir(parents=True, exist_ok=True)
    (box.claude / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": token, "subscriptionType": plan}}),
        encoding="utf-8",
    )
    remember_claude_plan(box, token, plan)


def run_in_box(box: sc.Sandbox, op: sc.Operation, argv: list, env: dict) -> subprocess.CompletedProcess:
    return box.run_command(op, argv, **env)


def base_cli_env(box: sc.Sandbox, ctx: dict, *, include_clis=("codex", "claude"), extra: dict | None = None) -> dict:
    """Construct a clean environment with only the requested fixture CLIs on PATH and no ambient keys."""
    cli_dir = box.root / "bin"
    cli_dir.mkdir(parents=True, exist_ok=True)
    for item in cli_dir.iterdir():
        if item.is_file():
            item.unlink()
    for runtime in (p for p in ctx["bin_dir"].iterdir() if p.is_dir()):    # the one-folder runtime the executables need
        if not (cli_dir / runtime.name).exists():
            shutil.copytree(runtime, cli_dir / runtime.name)
    for cli_name in include_clis:
        ext = ".exe" if os.name == "nt" else ""
        src = ctx["bin_dir"] / f"{cli_name}{ext}"
        if not src.is_file():
            src = ctx["bin_dir"] / f"codex{ext}"
        if src.is_file():
            shutil.copy2(src, cli_dir / f"{cli_name}{ext}")
    path_parts = [str(box.python.parent), str(cli_dir)]
    git_exe = shutil.which("git")
    if git_exe:
        path_parts.append(str(Path(git_exe).parent))
    if os.name == "nt":
        path_parts.append(os.environ.get("SYSTEMROOT", r"C:\Windows") + r"\System32")
    else:
        path_parts.extend(["/usr/bin", "/bin"])
    state_dir = box.root / "fixture-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    env: dict = {
        "PATH": os.pathsep.join(path_parts),
        "ROUTER_SCORE_CACHE": "off",
        "AGENTCOMPASS_PUBLIC_DATA": "off",
        "AGENTCOMPASS_ANTIGRAVITY_PLAN": "off",
        "FIXTURE_SCORER": "1",
        "FIXTURE_STATE": str(state_dir),
        "FIXTURE_BIN": str(cli_dir),
        "AGENTCOMPASS_USAGE_FLOAT_REPO": str(ctx["uf_repo"]),
        "AC_SANDBOX_PROXY": ctx["fixture"].url,
        "AC_SANDBOX_HOSTS": ",".join(FOREIGN_ALLOWED_HOSTS),
        "AC_SANDBOX_PORTS": str(ctx["fixture"].port),
    }
    if extra:
        for k, v in extra.items():
            env[k] = None if v is None else str(v)
    return env


def parse_json(text: str) -> dict:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except ValueError:
        idx = text.find("{")
        if idx >= 0:
            try:
                return json.loads(text[idx:])
            except ValueError:
                pass
        return {}


# ---------------------------------------------------------------------------------------------------------------------
# F01: Stale install & Public reference data channel (shipped schema; a schema-1 reader still refuses a newer set)


def verify_f01_public_data_and_installed_copy(ctx: dict):
    run: sc.Run = ctx["run"]
    fx: ForeignFixtureServer = ctx["fixture"]
    box = make_profile(ctx, "f01-public-data")
    op = run.operation(
        "F01 stale-install & public reference data channel",
        "Installed wheel runs from site-packages (not checkout), fetches sha256+schema verified public reference data, rejects tampered/corrupt sets, preserves bundled set when a schema-1 reader sees the shipped schema, reports in doctor, and honours AGENTCOMPASS_PUBLIC_DATA=off",
    )
    env = base_cli_env(box, ctx, extra={"AGENTCOMPASS_PUBLIC_DATA": "1"})

    # 1. Verify installed copy location (not repo checkout) and default bundled status.
    # The shipped schema is the only one this install reads (router-internals, Public reference data).
    res_status = run_in_box(box, op, [box.agentcompass, "data"], env)
    doc_status = parse_json(res_status.stdout)
    op.check("agentcompass data exits 0", res_status.returncode == 0, res_status.stderr)
    op.check(f"initial reference data source is bundled schema {SHIPPED_SCHEMA}",
             doc_status.get("source") == "bundled" and doc_status.get("schema_version") == SHIPPED_SCHEMA,
             doc_status)
    active_dir = Path(doc_status.get("dir") or "")
    op.check("active bundled dir is inside the venv's site-packages, never the repo checkout",
             "site-packages" in active_dir.parts and ROOT not in active_dir.parents, str(active_dir))

    # 2. Refresh from GitHub main fixture (valid sha256 + the shipped schema)
    fx.public_data_mode = "valid_update"
    res_refresh = run_in_box(box, op, [box.agentcompass, "data", "--refresh"], env)
    doc_refresh = parse_json(res_refresh.stdout)
    refresh_outcome = doc_refresh.get("refresh") or {}
    op.check("data --refresh succeeds against GitHub raw fixture",
             res_refresh.returncode == 0 and refresh_outcome.get("status") == "updated",
             doc_refresh)
    op.check("active source becomes fetched after refresh",
             doc_refresh.get("source") == "fetched" and doc_refresh.get("version") == fx.updated_version,
             doc_refresh)
    cached_accounts_path = box.state / "public-data" / "sets" / fx.updated_version / "accounts.json"
    op.check("accounts.json was fetched and cached under ROUTER_STATE_DIR/public-data/sets/<version>",
             cached_accounts_path.is_file(), str(cached_accounts_path))
    cached_accounts = json.loads(cached_accounts_path.read_text(encoding="utf-8")) if cached_accounts_path.is_file() else {}
    op.check("fetched accounts.json carries the updated reference content",
             cached_accounts.get("public_channel_verified") == "0.3.6-fetched",
             cached_accounts.get("public_channel_verified"))

    # 3. Doctor reports Reference data: fetched ... schema <shipped> (public_data.summary_line).
    res_doc = run_in_box(box, op, [box.agentcompass, "doctor"], env)
    op.check(f"doctor reports Reference data: fetched <version> (..., schema {SHIPPED_SCHEMA}, ...)",
             res_doc.returncode == 0
             and f"Reference data: fetched {fx.updated_version}" in res_doc.stdout
             and f"schema {SHIPPED_SCHEMA}" in res_doc.stdout,
             res_doc.stdout)

    # 4. Tampered sha256 is rejected and does not overwrite active fetched version
    fx.public_data_mode = "bad_sha"
    res_bad_sha = run_in_box(box, op, [box.agentcompass, "data", "--refresh", "--force"], env)
    doc_bad_sha = parse_json(res_bad_sha.stdout)
    op.check("tampered sha256 is rejected by data --refresh",
             (doc_bad_sha.get("refresh") or {}).get("status") == "failed"
             and "sha256" in str((doc_bad_sha.get("refresh") or {}).get("error") or ""),
             doc_bad_sha)
    op.check("previously cached valid set remains active after sha256 rejection",
             doc_bad_sha.get("source") == "fetched" and doc_bad_sha.get("version") == fx.updated_version,
             doc_bad_sha)

    # 5. Corrupt JSON (even when sha256 matches) is rejected and keeps previous valid set
    fx.public_data_mode = "bad_json"
    res_bad_json = run_in_box(box, op, [box.agentcompass, "data", "--refresh", "--force"], env)
    doc_bad_json = parse_json(res_bad_json.stdout)
    op.check("corrupt JSON with matching sha256 is rejected by data --refresh",
             (doc_bad_json.get("refresh") or {}).get("status") == "failed"
             and doc_bad_json.get("version") == fx.updated_version,
             doc_bad_json)

    # 6. A schema-1 reader fetching the published set of the shipped schema keeps its bundled set without error
    fx.public_data_mode = "valid_update"
    scripts_dir = box.data / "skills" / "agent-compass" / "scripts"
    compat_dir = box.root / "v035-bundled-refs"
    compat_dir.mkdir(parents=True, exist_ok=True)
    for f in (box.data / "skills" / "agent-compass" / "references").iterdir():
        if f.is_file():
            shutil.copy2(f, compat_dir / f.name)
    v035_manifest = json.loads((compat_dir / "public-data.json").read_text(encoding="utf-8"))
    v035_manifest["schema_version"] = 1
    v035_manifest["version"] = "v035bundled1"
    (compat_dir / "public-data.json").write_text(json.dumps(v035_manifest, indent=2), encoding="utf-8")
    v035_state = box.root / "v035-state"
    v035_probe = (
        "import json, os, sys; from pathlib import Path; "
        f"sys.path.insert(0, {str(scripts_dir)!r}); "
        "import public_data as pd; "
        "pd.SCHEMA_VERSION = 1; pd.MIN_SCHEMA_VERSION = 1; "
        f"bdir = Path({str(compat_dir)!r}); sdir = Path({str(v035_state)!r}); "
        "env_v = dict(os.environ, ROUTER_STATE_DIR=str(sdir)); "
        "outcome = pd.refresh(force=True, environ=env_v, bundled=bdir); "
        "st = pd.status(bundled=bdir, environ=env_v); "
        "print(json.dumps({'outcome': outcome, 'status': st}))"
    )
    res_v035 = run_in_box(box, op, [box.python, "-X", "utf8", "-c", v035_probe], env)
    doc_v035 = parse_json(res_v035.stdout)
    op.check("a 0.3.5 install fetching newer-schema public data returns schema_too_new and keeps its bundled set without error",
             res_v035.returncode == 0
             and (doc_v035.get("outcome") or {}).get("status") == "schema_too_new"
             and (doc_v035.get("outcome") or {}).get("remote_schema_version") == SHIPPED_SCHEMA
             and (doc_v035.get("status") or {}).get("source") == "bundled"
             and (doc_v035.get("status") or {}).get("version") == "v035bundled1"
             and (doc_v035.get("status") or {}).get("last_error") is None,
             doc_v035)

    # 7. Opt-out via AGENTCOMPASS_PUBLIC_DATA=off
    env_off = {**env, "AGENTCOMPASS_PUBLIC_DATA": "off"}
    res_off = run_in_box(box, op, [box.agentcompass, "data"], env_off)
    doc_off = parse_json(res_off.stdout)
    op.check("AGENTCOMPASS_PUBLIC_DATA=off pins source to bundled and disables fetch",
             doc_off.get("source") == "bundled" and doc_off.get("enabled") is False, doc_off)
    res_doc_off = run_in_box(box, op, [box.agentcompass, "doctor"], env_off)
    op.check("doctor with AGENTCOMPASS_PUBLIC_DATA=off reports Reference data: bundled ... - channel off",
             "Reference data: bundled" in res_doc_off.stdout and "- channel off" in res_doc_off.stdout,
             res_doc_off.stdout)


# ---------------------------------------------------------------------------------------------------------------------
# F02: Fleet parity on a clean foreign install with no accounts.local.json


def verify_f02_fleet_parity(ctx: dict):
    run: sc.Run = ctx["run"]
    box = make_profile(ctx, "f02-fleet-parity")
    op = run.operation(
        "F02 fleet parity without accounts.local.json",
        "A clean foreign install with no accounts.local.json and no accounts-override.json routes across task archetypes using shipped public scoring defaults",
    )
    sign_in_codex(box)
    sign_in_claude(box, plan="max")
    env = base_cli_env(box, ctx)
    run_in_box(box, op, [box.agentcompass, "usage"], env)

    # Confirm no accounts.local.json exists anywhere in the installed wheel or user profile
    local_files = list(box.site_packages().rglob("*local*.json")) + list(box.home.rglob("*local*.json"))
    op.check("no accounts.local.json exists in the installed package or foreign profile", not local_files, local_files)

    tasks = [
        ("bounded_coding", {
            "objective": "Fix off-by-one slice in parser.py and add a unit test.",
            "work_kind": "general_implementation", "expected_hours": 0.5,
            "profile": {"reasoning_depth": 2, "code_complexity": 2, "axis_demand": {"divergence": 1, "coding": 2, "aesthetics": 0, "tool_operation": 0}, "needs_visual": False, "needs_gui": False,
                        "local_only": True, "interaction_style": "engineering", "multi_task": False, "task_count": "1", "wants_fast": False, "read_only_review": False,
                        "reason": "bounded coding"},
        }),
        ("deep_debugging", {
            "objective": "Diagnose race condition across worker pool shutdown and lock handoff.",
            "work_kind": "debug_review", "expected_hours": 1.5,
            "profile": {"reasoning_depth": 4, "code_complexity": 3, "axis_demand": {"divergence": 2, "coding": 2, "aesthetics": 0, "tool_operation": 0}, "needs_visual": False, "needs_gui": False,
                        "local_only": True, "interaction_style": "engineering", "multi_task": False, "task_count": "1", "wants_fast": False, "read_only_review": False,
                        "reason": "deep debugging"},
        }),
        ("frontend_visual", {
            "objective": "Design and implement responsive quota cards with hover breakdown.",
            "work_kind": "frontend_implementation", "expected_hours": 1.0,
            "profile": {"reasoning_depth": 3, "code_complexity": 2, "axis_demand": {"divergence": 3, "coding": 2, "aesthetics": 0, "tool_operation": 3}, "needs_visual": True, "needs_gui": False,
                        "local_only": True, "interaction_style": "engineering", "multi_task": False, "task_count": "1", "wants_fast": False, "read_only_review": False,
                        "reason": "frontend UI"},
        }),
    ]
    for label, payload in tasks:
        task_file = box.root / f"task-{label}.json"
        task_file.write_text(json.dumps({"task": payload}), encoding="utf-8")
        res = run_in_box(box, op, [box.agentcompass, "plan", "--input", task_file, "--dry-run"], env)
        doc = parse_json(res.stdout)
        selected = doc.get("selected_bundle") or {}
        ranking = doc.get("ranking") or []
        # Mode B (router-internals): a window without a cited capacity, or an account with no current
        # reading, stays out of automatic routing. Both signed-in accounts must clear that, and a plan
        # that still has an eligible candidate must select one. Do not pin the winner or the score.
        def capacity_blocked(candidate: dict) -> bool:
            text = " ".join(str(item) for item in (candidate.get("excluded") or []))
            return "capacity_unknown" in text or "quota reading unavailable" in text
        by_account: dict[str, list] = {}
        for candidate in ranking:
            by_account.setdefault((candidate.get("bundle") or {}).get("account"), []).append(candidate)
        priced = all(
            any(not capacity_blocked(candidate) for candidate in by_account.get(account, []))
            for account in ("claude", "codex")
        )
        open_candidates = [candidate for candidate in ranking if not candidate.get("excluded")]
        selected_when_open = (not open_candidates) or (res.returncode == 0 and bool(selected.get("account")))
        op.check(f"clean foreign fleet keeps claude and codex out of mode B for {label}",
                 priced and selected_when_open,
                 {"rc": res.returncode, "choice": doc.get("choice"), "selected_account": selected.get("account"),
                  "stderr": res.stderr[-200:],
                  "blocked": {
                      account: [candidate.get("excluded") for candidate in rows[:2]]
                      for account, rows in by_account.items() if account in ("claude", "codex")
                  }})


# ---------------------------------------------------------------------------------------------------------------------
# F03: Classifier cascade across multi-home accounts and families


def verify_f03_classifier_cascade(ctx: dict):
    run: sc.Run = ctx["run"]
    box = make_profile(ctx, "f03-classifier-cascade")
    op = run.operation(
        "F03 classifier cascade across usable accounts",
        "When JEV_API_KEY is absent and ~/.claude is signed out while ~/.claude-2 is signed in, score_task skips jev and ~/.claude and classifies with claude_haiku_low on account claude-2; falls back to codex_terra_low on codex-2 when all Claude homes are signed out",
    )
    # Topology A: ~/.claude exists (signed out, no .credentials.json), ~/.claude-2 is signed in!
    (box.home / ".claude").mkdir(parents=True, exist_ok=True)
    claude2 = box.home / ".claude-2"
    claude2.mkdir(parents=True, exist_ok=True)
    (claude2 / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "foreign-claude2-token", "subscriptionType": "pro"}}),
        encoding="utf-8",
    )
    remember_claude_plan(box, "foreign-claude2-token", "pro")
    codex2 = box.home / ".codex-2"
    paseo_dir = box.home / ".paseo"
    paseo_dir.mkdir(parents=True, exist_ok=True)
    (paseo_dir / "config.json").write_text(
        json.dumps({
            "agents": {
                "providers": {
                    "claude-2": {"extends": "claude", "env": {"CLAUDE_CONFIG_DIR": str(claude2)}},
                    "codex-2": {"extends": "codex", "env": {"CODEX_HOME": str(codex2)}},
                }
            }
        }, indent=2),
        encoding="utf-8",
    )
    score_input = box.root / "score-task.json"
    score_input.write_text(json.dumps({
        "task": {"objective": "Update timeout constant in client.py and run unit tests."}
    }), encoding="utf-8")

    env = base_cli_env(box, ctx, include_clis=("claude", "codex"), extra={"ROUTER_SCORER_TYPE": "jev"})
    res_a = run_in_box(box, op, [box.agentcompass, "score", "--input", score_input], env)
    doc_a = parse_json(res_a.stdout)
    scorer_a = doc_a.get("scorer") or {}
    op.check("score succeeds when ~/.claude is signed out and ~/.claude-2 is signed in",
             res_a.returncode == 0
             and scorer_a.get("scorer_type") == "claude_haiku_low"
             and scorer_a.get("account") == "claude-2",
             {"rc": res_a.returncode, "scorer": scorer_a, "stderr": res_a.stderr[-300:]})
    skipped_a = scorer_a.get("skipped") or []
    op.check("skipped list records jev (missing_api_key) and does not fail on signed-out ~/.claude",
             any(s.get("scorer_type") == "jev" for s in skipped_a if isinstance(s, dict)),
             skipped_a)

    # Topology B: remove ~/.claude-2 credentials, sign in ~/.codex-2 (while ~/.codex is signed out)
    (claude2 / ".credentials.json").unlink()
    (box.home / ".codex").mkdir(parents=True, exist_ok=True)
    codex2.mkdir(parents=True, exist_ok=True)
    (codex2 / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "foreign-codex2-token", "account_id": "acct-codex2"}}),
        encoding="utf-8",
    )
    res_b = run_in_box(box, op, [box.agentcompass, "score", "--input", score_input], env)
    doc_b = parse_json(res_b.stdout)
    scorer_b = doc_b.get("scorer") or {}
    skipped_b = scorer_b.get("skipped") or []
    op.check("when all Claude homes are signed out, cascade skips claude_haiku_low and uses codex_terra_low on codex-2",
             res_b.returncode == 0
             and scorer_b.get("scorer_type") == "codex_terra_low"
             and scorer_b.get("account") == "codex-2",
             {"rc": res_b.returncode, "scorer": scorer_b, "stderr": res_b.stderr[-300:]})
    op.check("skipped list records both jev and claude_haiku_low",
             {s.get("scorer_type") for s in skipped_b if isinstance(s, dict)} >= {"jev", "claude_haiku_low"},
             skipped_b)


# ---------------------------------------------------------------------------------------------------------------------
# F04: Quota, window scope, gate parity, control messages, discovered bundles, no twins, and agy 3p capacity


def verify_f04_quota_gate_scope_and_twins(ctx: dict):
    run: sc.Run = ctx["run"]
    box = make_profile(ctx, "f04-quota-gate-scope")
    op = run.operation(
        "F04 quota, window scope, gate parity, control messages, model management & twin suppression",
        "Verify from installed artifacts: (1) no twin Pi bundles on shared Codex subscription + agy 3p pool-prior capacity; "
        "(2) agy with 3p-weekly used up allows Gemini launch and blocks Claude-model launch; "
        "(3) 5h window at its floor blocks new launch for claude, codex, and claude-2; "
        "(4) Magpie pool with accounts: 2 is judged on N×(100-mean); "
        "(5) new data-declared scoped window works with zero code change; "
        "(6) owning parent's control message to its running worker on an exhausted window passes while idle is blocked; "
        "(7) discovered bundles show up in model management for the user to check.",
    )
    # Sign in codex, claude, claude-2, and pi (sharing codex's subscription account_id)
    sign_in_codex(box)
    sign_in_claude(box, plan="pro")
    claude2 = box.home / ".claude-2"
    claude2.mkdir(parents=True, exist_ok=True)
    (claude2 / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "foreign-claude2-token", "subscriptionType": "pro"}}),
        encoding="utf-8",
    )
    remember_claude_plan(box, "foreign-claude2-token", "pro")
    codex_auth = json.loads((box.codex / "auth.json").read_text(encoding="utf-8"))
    shared_account_id = codex_auth["tokens"]["account_id"]
    pi_dir = box.home / ".pi" / "agent"
    pi_dir.mkdir(parents=True, exist_ok=True)
    (pi_dir / "auth.json").write_text(
        json.dumps({"openai-codex": {"type": "oauth", "access": "foreign-pi-token", "refresh": "r", "accountId": shared_account_id}}),
        encoding="utf-8",
    )
    paseo_dir = box.home / ".paseo"
    paseo_dir.mkdir(parents=True, exist_ok=True)
    (paseo_dir / "config.json").write_text(
        json.dumps({
            "agents": {
                "providers": {
                    "claude-2": {"extends": "claude", "env": {"CLAUDE_CONFIG_DIR": str(claude2)}},
                }
            }
        }, indent=2),
        encoding="utf-8",
    )
    env = base_cli_env(box, ctx, include_clis=("codex", "claude", "agy", "pi"))
    run_in_box(box, op, [box.agentcompass, "setup", "--yes", "--no-hooks"], env)

    # 1. Collect codex/claude usage with matching credential_key, then inject antigravity usage with canonical window IDs
    run_in_box(box, op, [box.agentcompass, "usage"], env)
    cache_path = box.state / "usage-cache.json"
    now = dt.datetime.now(dt.timezone.utc)
    now_iso = now.isoformat()
    reset_5h = (now + dt.timedelta(hours=4)).isoformat()
    reset_7d = (now + dt.timedelta(days=5)).isoformat()
    cache_doc = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.is_file() else {"providers": {}}
    cache_doc.setdefault("providers", {})["antigravity"] = {
        "provider_id": "antigravity", "display_name": "antigravity", "plan_label": "ultra20",
        "available": True, "stale": False, "fetched_at": now_iso,
        "windows": [
            {"id": "gemini-weekly", "label": "Gemini 7d", "used_pct": 20.0, "resets_at": reset_7d},
            {"id": "gemini-5h", "label": "Gemini 5h", "used_pct": 10.0, "resets_at": reset_5h},
            {"id": "3p-weekly", "label": "Claude/GPT-OSS 7d", "used_pct": 20.0, "resets_at": reset_7d},
            {"id": "3p-5h", "label": "Claude/GPT-OSS 5h", "used_pct": 10.0, "resets_at": reset_5h},
        ],
    }
    cache_path.write_text(json.dumps(cache_doc, indent=2), encoding="utf-8")
    ov_path = box.state / "accounts-override.json"
    ov_doc = json.loads(ov_path.read_text(encoding="utf-8")) if ov_path.is_file() else {"schema_version": 2, "accounts": {}}
    ov_doc.setdefault("accounts", {})["antigravity"] = {"verified": True}
    ov_doc["accounts"]["pi"] = {"verified": True}
    ov_doc["accounts"]["claude-2"] = {"verified": True}
    ov_path.write_text(json.dumps(ov_doc, indent=2), encoding="utf-8")

    task_file = box.root / "task-twins-agy.json"
    task_file.write_text(json.dumps({"task": {
        "objective": "Implement retry backoff in transport.py and verify edge cases.",
        "work_kind": "general_implementation", "expected_hours": 1.0,
        "profile": {"reasoning_depth": 3, "code_complexity": 3, "axis_demand": {"divergence": 1, "coding": 2, "aesthetics": 0, "tool_operation": 0}, "needs_visual": False, "needs_gui": False,
                    "local_only": True, "interaction_style": "engineering", "multi_task": False, "task_count": "1", "wants_fast": False, "read_only_review": False,
                    "reason": "verify no twin pi bundles and agy 3p capacity"},
    }}), encoding="utf-8")
    res_plan = run_in_box(box, op, [box.agentcompass, "plan", "--input", task_file, "--dry-run"], env)
    doc_plan = parse_json(res_plan.stdout)
    ranking = doc_plan.get("ranking") or []
    eligible = [c for c in ranking if not c.get("excluded") and c.get("score") is not None]
    eligible_accounts = {(c.get("bundle") or {}).get("account") for c in eligible}
    op.check("codex is in eligible candidates while quota-alias twin pi is excluded",
             "codex" in eligible_accounts and "pi" not in eligible_accounts,
             sorted(a for a in eligible_accounts if a))
    agy_3p = [
        c for c in eligible
        if (c.get("bundle") or {}).get("account") == "antigravity"
        and "claude" in str((c.get("bundle") or {}).get("model") or "")
    ]
    op.check("antigravity 3p bundles are eligible and have positive capacity priced from the pool prior",
             bool(agy_3p)
             and all(((c.get("capacity") or {}).get("remaining") or 0) > 0
                     and ((c.get("capacity") or {}).get("capacity_usd") or 0) > 0 for c in agy_3p),
             [{"id": c.get("id"), "cap": c.get("capacity")} for c in agy_3p[:2]])

    # 2-7. Run a single comprehensive probe against the installed wheel's scripts, setup/router_gate_hook.py, and tray/router_data.py
    scripts_dir = box.data / "skills" / "agent-compass" / "scripts"
    setup_dir = box.data / "setup"
    tray_dir = ctx["repo"] / "tray"
    quota_gate_probe = f"""
import copy, datetime as dt, json, os, sys
from pathlib import Path
sys.path.insert(0, {str(scripts_dir)!r})
sys.path.insert(0, {str(setup_dir)!r})
sys.path.insert(0, {str(tray_dir)!r})

import account_fleet
import allocator
import axis_scores
import dispatch_registry
import provider_inventory
import route_judge
import router_gate_hook as gate
import router_data

now = dt.datetime.now(dt.timezone.utc)
reset_5h = (now + dt.timedelta(hours=3)).isoformat()
reset_7d = (now + dt.timedelta(days=4)).isoformat()
state_dir = Path(os.environ["ROUTER_STATE_DIR"])
cache_path = state_dir / "usage-cache.json"
ledger_path = state_dir / "router-ledger.jsonl"
paseo_dir = state_dir / "paseo-agents"
paseo_ws_dir = paseo_dir / "ws-default"
paseo_ws_dir.mkdir(parents=True, exist_ok=True)

inv_initial = provider_inventory.discover()
cred_keys = {{
    name: row.get("credential_key")
    for name, row in (inv_initial.get("accounts") or {{}}).items()
    if isinstance(row, dict) and row.get("credential_key")
}}

def write_cache(providers):
    for acc, row in providers.items():
        if isinstance(row, dict) and acc in cred_keys:
            row.setdefault("credential_key", cred_keys[acc])
    cache_path.write_text(json.dumps({{"providers": providers}}, indent=2), encoding="utf-8")

def write_ledger(choice, bundle, parent_id="parent-1", extra_rows=()):
    row = {{
        "at": now.isoformat(),
        "stage": "record",
        "choice": choice,
        "parent_agent_id": parent_id,
        "bundle": bundle,
        "selected_bundle": bundle,
    }}
    lines = [json.dumps(r) for r in extra_rows] + [json.dumps(row)]
    ledger_path.write_text("\\n".join(lines) + "\\n", encoding="utf-8")

# --- Check A: agy with 3p-weekly used up (100%): Gemini launch allowed, Claude-model launch blocked ---
write_cache({{
    "antigravity": {{
        "provider_id": "antigravity", "plan_label": "ultra20", "available": True, "stale": False, "fetched_at": now.isoformat(),
        "windows": [
            {{"id": "gemini-weekly", "used_pct": 28.4, "resets_at": reset_7d}},
            {{"id": "gemini-5h", "used_pct": 7.3, "resets_at": reset_5h}},
            {{"id": "3p-weekly", "used_pct": 100.0, "resets_at": reset_7d}},
            {{"id": "3p-5h", "used_pct": 0.0, "resets_at": reset_5h}},
        ],
    }}
}})
accounts = account_fleet.load_accounts()
agy_gemini_reason = gate.check_account_quota_exhausted(
    "antigravity", usage_cache_path=cache_path, now=now, accounts=accounts, model="gemini-4-argon-high"
)
agy_claude_reason = gate.check_account_quota_exhausted(
    "antigravity", usage_cache_path=cache_path, now=now, accounts=accounts, model="claude-sonnet-5-5"
)
write_ledger("argon_high", {{"account": "antigravity", "provider": "antigravity-resilient", "model": "gemini-4-argon-high", "effort": "high", "modeId": "full-access"}})
agy_gemini_eval = gate.evaluate_payload(
    {{"parent_agent_id": "parent-1", "tool_name": "invoke_subagent", "tool_input": {{"TypeName": "self", "Role": "Worker", "Prompt": "check", "Model": "gemini-4-argon-high"}}}},
    ledger_path=ledger_path, now=now, usage_cache_path=cache_path, paseo_agents_dir=paseo_dir
)
write_ledger("agy_sonnet55_high", {{"account": "antigravity", "provider": "antigravity-resilient", "model": "claude-sonnet-5-5", "effort": "high", "modeId": "full-access"}})
agy_claude_eval = gate.evaluate_payload(
    {{"parent_agent_id": "parent-1", "tool_name": "mcp__paseo__create_agent", "tool_input": {{"provider": "antigravity-resilient", "model": "claude-sonnet-5-5", "effort": "high", "modeId": "full-access"}}}},
    ledger_path=ledger_path, now=now, usage_cache_path=cache_path, paseo_agents_dir=paseo_dir
)

# --- Check B: 5h window at its floor blocks a new launch for claude, codex, and claude-2;
#              in-flight reservations count against quota; secondary Claude account not named claude-2 is resolved from bundle/labels ---
write_cache({{
    "claude": {{
        "provider_id": "claude", "plan_label": "pro", "available": True, "stale": False, "fetched_at": now.isoformat(),
        "windows": [
            {{"id": "five_hour", "used_pct": 100.0, "resets_at": reset_5h}},
            {{"id": "seven_day", "used_pct": 20.0, "resets_at": reset_7d}},
        ],
    }},
    "codex": {{
        "provider_id": "codex", "plan_label": "pro", "available": True, "stale": False, "fetched_at": now.isoformat(),
        "windows": [
            {{"id": "five_hour", "used_pct": 100.0, "resets_at": reset_5h}},
            {{"id": "seven_day", "used_pct": 20.0, "resets_at": reset_7d}},
        ],
    }},
    "claude-2": {{
        "provider_id": "claude-2", "plan_label": "pro", "available": True, "stale": False, "fetched_at": now.isoformat(),
        "windows": [
            {{"id": "five_hour", "used_pct": 100.0, "resets_at": reset_5h}},
            {{"id": "seven_day", "used_pct": 20.0, "resets_at": reset_7d}},
        ],
    }},
}})
accounts = account_fleet.load_accounts()
floor_blocks_100 = {{
    acc: gate.check_account_quota_exhausted(acc, usage_cache_path=cache_path, now=now, accounts=accounts)
    for acc in ("claude", "codex", "claude-2")
}}
write_cache({{
    "claude": {{
        "provider_id": "claude", "plan_label": "pro", "available": True, "stale": False, "fetched_at": now.isoformat(),
        "windows": [
            {{"id": "five_hour", "used_pct": 75.0, "resets_at": reset_5h}},
            {{"id": "seven_day", "used_pct": 20.0, "resets_at": reset_7d}},
        ],
    }},
    "codex": {{
        "provider_id": "codex", "plan_label": "pro", "available": True, "stale": False, "fetched_at": now.isoformat(),
        "windows": [
            {{"id": "five_hour", "used_pct": 75.0, "resets_at": reset_5h}},
            {{"id": "seven_day", "used_pct": 20.0, "resets_at": reset_7d}},
        ],
    }},
    "claude-2": {{
        "provider_id": "claude-2", "plan_label": "pro", "available": True, "stale": False, "fetched_at": now.isoformat(),
        "windows": [
            {{"id": "five_hour", "used_pct": 75.0, "resets_at": reset_5h}},
            {{"id": "seven_day", "used_pct": 20.0, "resets_at": reset_7d}},
        ],
    }},
}})
floor_blocks_75 = {{
    acc: gate.check_account_quota_exhausted(acc, usage_cache_path=cache_path, now=now, accounts=accounts)
    for acc in ("claude", "codex", "claude-2")
}}

# Keep the Claude subscription spec: the reservation cache replaces providers and drops Claude,
# and a second home is priced as that spec (router-internals: plan capacity comes from the discovered plan).
claude_subscription = copy.deepcopy(accounts["accounts"]["claude"])
# 3p-weekly at 93% used leaves 7%, above the 5% safety margin. A reservation of 3% of the
# priced capacity (public ultra20 figure from five-axis-scores, not a fixed dollar amount)
# brings remaining to 4% and blocks. An unrecognized tier such as "ultra" is Mode B.
write_cache({{
    "antigravity": {{
        "provider_id": "antigravity", "plan_label": "ultra20", "available": True, "stale": False, "fetched_at": now.isoformat(),
        "windows": [
            {{"id": "gemini-weekly", "used_pct": 10.0, "resets_at": reset_7d}},
            {{"id": "gemini-5h", "used_pct": 10.0, "resets_at": reset_5h}},
            {{"id": "3p-weekly", "used_pct": 93.0, "resets_at": reset_7d}},
            {{"id": "3p-5h", "used_pct": 10.0, "resets_at": reset_5h}},
        ],
    }},
    "team-anthropic": {{
        "provider_id": "team-anthropic", "plan_label": "pro", "available": True, "stale": False, "fetched_at": now.isoformat(),
        "windows": [
            {{"id": "five_hour", "used_pct": 80.0, "resets_at": reset_5h}},
            {{"id": "seven_day", "used_pct": 20.0, "resets_at": reset_7d}},
        ],
    }},
}})
accounts = account_fleet.load_accounts()
priced_agy = axis_scores.five_axis_overlay(copy.deepcopy(accounts))
weekly_cap = (((priced_agy.get("accounts") or {{}}).get("antigravity") or {{}}).get("capacity_usd") or {{}}).get("3p-weekly")
if not isinstance(weekly_cap, (int, float)) or isinstance(weekly_cap, bool) or weekly_cap <= 0:
    raise SystemExit("antigravity ultra20 3p-weekly has no priced capacity")
reserve_usd = round(float(weekly_cap) * 0.03, 2)
_, agy_opus_candidates = gate.resolve_target({{"bundle_id": "agy_opus55_high"}}, accounts)
no_res_reason = gate.check_account_quota_exhausted(
    "antigravity", usage_cache_path=cache_path, now=now, accounts=accounts, bundles=agy_opus_candidates, records=()
)
inflight_records = [{{
    "stage": "dispatch",
    "dispatch_id": "disp-inflight-1",
    "state": "started",
    "at": now.isoformat(),
    "lease_until": (now + dt.timedelta(minutes=10)).isoformat(),
    "choice": "agy_opus55_high",
    "bundle": {{"account": "antigravity", "provider": "antigravity-resilient", "model": "claude-opus-5-5", "effort": "high"}},
    "expected_usd": reserve_usd,
}}]
with_res_reason = gate.check_account_quota_exhausted(
    "antigravity", usage_cache_path=cache_path, now=now, accounts=accounts, bundles=agy_opus_candidates, records=inflight_records
)

# Secondary Claude account not named claude-2 ('team-anthropic') resolved from bundle/labels without name inference.
# The spec is the one loaded with the Claude plan, not the reloaded cache that no longer carries Claude.
accounts_team = copy.deepcopy(accounts)
accounts_team.setdefault("accounts", {{}})["team-anthropic"] = copy.deepcopy(claude_subscription)
accounts_team["accounts"]["team-anthropic"]["bundles"] = {{
    "team_sonnet55_high": {{"provider": "team-anthropic", "harness": "claude", "model": "claude-sonnet-5-5", "effort": "high", "modeId": "bypassPermissions"}}
}}
team_reason = gate.check_account_quota_exhausted(
    "team-anthropic", usage_cache_path=cache_path, now=now, accounts=accounts_team, model="claude-sonnet-5-5"
)

# --- Check C: Magpie pool with accounts: 2 is judged on N × (100 - mean) ---
write_cache({{
    "claude": {{
        "provider_id": "claude", "plan_label": "pro", "available": True, "stale": False, "fetched_at": now.isoformat(),
        "windows": [
            {{"id": "five_hour", "used_pct": 80.0, "accounts": 2, "resets_at": reset_5h}},
            {{"id": "seven_day", "used_pct": 30.0, "accounts": 2, "resets_at": reset_7d}},
        ],
    }},
    "claude-2": {{
        "provider_id": "claude-2", "plan_label": "pro", "available": True, "stale": False, "fetched_at": now.isoformat(),
        "windows": [
            {{"id": "five_hour", "used_pct": 80.0, "accounts": 1, "resets_at": reset_5h}},
            {{"id": "seven_day", "used_pct": 30.0, "accounts": 1, "resets_at": reset_7d}},
        ],
    }},
}})
accounts = account_fleet.load_accounts()
pool2_reason = gate.check_account_quota_exhausted("claude", usage_cache_path=cache_path, now=now, accounts=accounts)
pool1_reason = gate.check_account_quota_exhausted("claude-2", usage_cache_path=cache_path, now=now, accounts=accounts)

# --- Check D: New data-declared scoped window works with zero code change ---
write_cache({{
    "claude": {{
        "provider_id": "claude", "plan_label": "pro", "available": True, "stale": False, "fetched_at": now.isoformat(),
        "windows": [
            {{"id": "five_hour", "used_pct": 10.0, "resets_at": reset_5h}},
            {{"id": "seven_day", "used_pct": 20.0, "resets_at": reset_7d}},
            {{"id": "weekly_sonnet", "models": ["sonnet"], "used_pct": 100.0, "resets_at": reset_7d}},
        ],
    }},
}})
accounts_scoped = account_fleet.load_accounts()
accounts_scoped.setdefault("accounts", {{}}).setdefault("claude", {{}}).setdefault("windows", {{}})["weekly_sonnet"] = {{
    "hours": 168,
    "models": ["sonnet"],
}}
scoped_sonnet_reason = gate.check_account_quota_exhausted(
    "claude", usage_cache_path=cache_path, now=now, accounts=accounts_scoped, model="claude-sonnet-5-5"
)
scoped_opus_reason = gate.check_account_quota_exhausted(
    "claude", usage_cache_path=cache_path, now=now, accounts=accounts_scoped, model="claude-opus-5-5"
)

# --- Check E: Owning parent's control message to its running worker on an exhausted window passes;
#              idle, unknown-status, and non-owner running workers are blocked ---
write_cache({{
    "claude": {{
        "provider_id": "claude", "plan_label": "pro", "available": True, "stale": False, "fetched_at": now.isoformat(),
        "windows": [
            {{"id": "five_hour", "used_pct": 100.0, "resets_at": reset_5h}},
            {{"id": "seven_day", "used_pct": 50.0, "resets_at": reset_7d}},
        ],
    }},
}})
running_id = "11111111-2222-3333-4444-555555555555"
idle_id = "66666666-7777-8888-9999-aaaaaaaaaaaa"
unknown_id = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
parent_id = "parent-owner-0001"
for wid, st in ((running_id, "running"), (idle_id, "idle"), (unknown_id, "")):
    (paseo_ws_dir / f"{{wid}}.json").write_text(json.dumps({{
        "id": wid,
        "provider": "claude",
        "model": "claude-sonnet-5-5",
        **({{"status": st, "lastStatus": st}} if st else {{}}),
        "labels": {{
            "router.parent_id": parent_id,
            "router.account": "claude",
            "router.bundle_id": "sonnet55_high",
            "router.model": "claude-sonnet-5-5",
        }},
    }}), encoding="utf-8")
ctrl_running_eval = gate.evaluate_payload(
    {{
        "parent_agent_id": parent_id,
        "tool_name": "mcp__paseo__send_agent_prompt",
        "tool_input": {{"agentId": running_id, "prompt": "Please wrap up at a consistent state and report back."}},
    }},
    ledger_path=ledger_path, now=now, usage_cache_path=cache_path, paseo_agents_dir=paseo_dir
)
ctrl_idle_eval = gate.evaluate_payload(
    {{
        "parent_agent_id": parent_id,
        "tool_name": "mcp__paseo__send_agent_prompt",
        "tool_input": {{"agentId": idle_id, "prompt": "Start a new turn on this task."}},
    }},
    ledger_path=ledger_path, now=now, usage_cache_path=cache_path, paseo_agents_dir=paseo_dir
)
ctrl_unknown_eval = gate.evaluate_payload(
    {{
        "parent_agent_id": parent_id,
        "tool_name": "mcp__paseo__send_agent_prompt",
        "tool_input": {{"agentId": unknown_id, "prompt": "Please wrap up."}},
    }},
    ledger_path=ledger_path, now=now, usage_cache_path=cache_path, paseo_agents_dir=paseo_dir
)
ctrl_nonowner_eval = gate.evaluate_payload(
    {{
        "parent_agent_id": "different-parent-9999",
        "tool_name": "mcp__paseo__send_agent_prompt",
        "tool_input": {{"agentId": running_id, "prompt": "Please wrap up."}},
    }},
    ledger_path=ledger_path, now=now, usage_cache_path=cache_path, paseo_agents_dir=paseo_dir
)

# --- Check F: Discovered models default-checked rule (superseded unchecked, latest & unknown checked),
#              user checkbox overrides both ways, tray agrees with routing, and metered stays last resort ---
catalog_path = state_dir / "model-catalog.json"
catalog_path.write_text(json.dumps({{
    "schema_version": 1,
    "providers": {{
        "claude": {{
            "ok": True,
            "type": "cli",
            "checked_at": now.isoformat(),
            "models": [
                {{"id": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6 (superseded)"}},
                {{"id": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5 (latest)"}},
                {{"id": "claude-opus-6-0", "label": "Claude Opus 6.0 (new unknown)"}},
            ],
        }}
    }}
}}, indent=2), encoding="utf-8")
router_data._invalidate_accounts_cache()
hm_rows = router_data.harness_model_rows()
claude_hm = next((r for r in hm_rows if r["account"] == "claude" and r["harness"] == "claude"), {{}})
superseded_group = next((m for m in claude_hm.get("models", []) if m.get("model") == "claude-sonnet-4-6"), None)
latest_group = next((m for m in claude_hm.get("models", []) if m.get("model") == "claude-sonnet-5-5"), None)
discovered_group = next((m for m in claude_hm.get("models", []) if m.get("model") == "claude-opus-6-0"), None)

# Check agreement between tray harness_model_rows and account_fleet.bundles(account_fleet.load_accounts()) before toggle
alloc_bundles_initial = account_fleet.bundles(account_fleet.load_accounts())
superseded_bid = "auto:claude:claude:claude-sonnet-4-6@high"
discovered_bid = "auto:claude:claude:claude-opus-6-0@high"
initial_alloc_superseded_disabled = (alloc_bundles_initial.get(superseded_bid) or {{}}).get("disabled")
initial_alloc_discovered_disabled = (alloc_bundles_initial.get(discovered_bid) or {{}}).get("disabled")

# User checkbox overrides both ways: check the superseded model ON, uncheck the new unknown model OFF
superseded_toggled_on = router_data.toggle_harness_model("claude", "claude", "claude-sonnet-4-6", True)
toggled_off = router_data.toggle_harness_model("claude", "claude", "claude-opus-6-0", False)
alloc_bundles_after = account_fleet.bundles(account_fleet.load_accounts())
after_alloc_superseded_disabled = (alloc_bundles_after.get(superseded_bid) or {{}}).get("disabled")
after_alloc_discovered_disabled = (alloc_bundles_after.get(discovered_bid) or {{}}).get("disabled")
toggled_on = router_data.toggle_harness_model("claude", "claude", "claude-opus-6-0", True)

# Metered last resort (router-internals.md "Checked metered bundles stay a last resort";
# docs/technical-reference.md Metered USD). Community capacity measures subscription plans
# only, so a prepaid metered account states its own weekly window and capacity_usd
# (axis_scores.five_axis_overlay). apply() names a Claude plan only from a cache row whose
# plan_source is profile_api, and a Codex plan only from plan_label, both with this
# credential. Write that cache before load_accounts(). The second snapshot is only the
# windows those applied plans draw, at 10% used: claude pro is five_hour + seven_day
# (plan-templates.json); codex pro is the weekly usage window. A window the plan does not
# draw leaves the subscription unpriced, plan() lifts the last resort, and this row is
# judged as a data error instead.
write_cache({{
    "claude": {{
        "provider_id": "claude", "plan_label": "pro", "plan_source": "profile_api",
        "available": True, "stale": False, "fetched_at": now.isoformat(),
        "windows": [
            {{"id": "five_hour", "used_pct": 10.0, "resets_at": reset_5h}},
            {{"id": "seven_day", "used_pct": 10.0, "resets_at": reset_7d}},
        ],
    }},
    "claude-2": {{
        "provider_id": "claude-2", "plan_label": "pro", "plan_source": "profile_api",
        "available": True, "stale": False, "fetched_at": now.isoformat(),
        "windows": [
            {{"id": "five_hour", "used_pct": 10.0, "resets_at": reset_5h}},
            {{"id": "seven_day", "used_pct": 10.0, "resets_at": reset_7d}},
        ],
    }},
    "codex": {{
        "provider_id": "codex", "plan_label": "pro", "available": True, "stale": False, "fetched_at": now.isoformat(),
        "windows": [
            {{"id": "usage", "used_pct": 10.0, "resets_at": reset_7d}},
        ],
    }},
}})
accounts_metered = account_fleet.load_accounts()
accounts_metered["accounts"]["litellm-test"] = {{
    "billing": "metered",
    "verified": True,
    "windows": {{"weekly": {{"hours": 168}}}},
    "capacity_usd": {{"weekly": 200.0}},
    "bundles": {{
        "metered_sol_high": {{
            "provider": "litellm-test",
            "harness": "codex",
            "model": "gpt-6.1-sol",
            "effort": "high",
            "modeId": "full-access",
            "disabled": False,
            "billing": "metered",
            "draws": ["weekly"],
        }}
    }},
}}
fresh_readings = {{}}
for _name, _meta in (
    ("claude", {{"provider_id": "claude", "plan_label": "pro", "plan_source": "profile_api"}}),
    ("claude-2", {{"provider_id": "claude-2", "plan_label": "pro", "plan_source": "profile_api"}}),
    ("codex", {{"provider_id": "codex", "plan_label": "pro"}}),
):
    _spec = (accounts_metered.get("accounts") or {{}}).get(_name) or {{}}
    _rows = []
    for _wid, _wspec in (_spec.get("windows") or {{}}).items():
        _hours = _wspec.get("hours") if isinstance(_wspec, dict) else None
        _rows.append({{
            "id": _wid, "used_pct": 10.0,
            "resets_at": reset_5h if isinstance(_hours, (int, float)) and _hours < 24 else reset_7d,
        }})
    if _rows:
        fresh_readings[_name] = {{
            **_meta, "available": True, "stale": False, "fetched_at": now.isoformat(), "windows": _rows,
        }}
fresh_readings["litellm-test"] = {{
    "provider_id": "litellm-test", "available": True, "stale": False, "fetched_at": now.isoformat(),
    "windows": [{{"id": "weekly", "used_pct": 10.0, "resets_at": reset_7d}}],
}}
write_cache(fresh_readings)
usage_fresh = route_judge.usage_snapshot(
    cache_path, list((accounts_metered.get("accounts") or {{}}).keys()), now, inventory=accounts_metered.get("_discovery") or {{}}
)
usage_fresh = provider_inventory.merge_usage(usage_fresh, accounts_metered, now)
# Caller profile is required (task_intake.normalize_task). Host policy must name every in-memory
# account, including litellm-test, which setup never enrolled (host_policy.account_allowed).
caller_profile = {{
    "reasoning_depth": 2,
    "code_complexity": 2,
    "axis_demand": {{"divergence": 1, "coding": 2, "aesthetics": 0, "tool_operation": 0}},
    "needs_visual": False,
    "needs_gui": False,
    "local_only": True,
    "wants_fast": False,
    "read_only_review": False,
    "interaction_style": "engineering",
    "multi_task": False,
    "task_count": "1",
}}
metered_task = {{
    "objective": "Fix a bug in parser.py",
    "work_kind": "general_implementation",
    "expected_hours": 0.5,
    "profile": caller_profile,
}}
policy_names = sorted(accounts_metered.get("accounts") or {{}})
metered_policy = {{"allowed_accounts": policy_names, "known_accounts": policy_names}}
plan_with_sub = allocator.plan(
    metered_task,
    usage_fresh, now=now, accounts=accounts_metered, overrides=[], host_policy_config=metered_policy
)
metered_cand_with_sub = next((c for c in plan_with_sub.get("ranking", []) if c.get("id") == "metered_sol_high"), {{}})
# Subscriptions are unlaunchable by launch evidence. verified is not an on/off switch.
for acc_name, acc_spec in list(accounts_metered.get("accounts", {{}}).items()):
    if acc_name != "litellm-test" and isinstance(acc_spec, dict):
        acc_spec["verified"] = False
plan_no_sub = allocator.plan(
    metered_task,
    usage_fresh, now=now, accounts=accounts_metered, overrides=[], host_policy_config=metered_policy
)

print(json.dumps({{
    "agy_gemini_reason": agy_gemini_reason,
    "agy_claude_reason": agy_claude_reason,
    "agy_gemini_eval": agy_gemini_eval,
    "agy_claude_eval": agy_claude_eval,
    "floor_blocks_100": floor_blocks_100,
    "floor_blocks_75": floor_blocks_75,
    "no_res_reason": no_res_reason,
    "with_res_reason": with_res_reason,
    "team_reason": team_reason,
    "pool2_reason": pool2_reason,
    "pool1_reason": pool1_reason,
    "scoped_sonnet_reason": scoped_sonnet_reason,
    "scoped_opus_reason": scoped_opus_reason,
    "ctrl_running_eval": ctrl_running_eval,
    "ctrl_idle_eval": ctrl_idle_eval,
    "ctrl_unknown_eval": ctrl_unknown_eval,
    "ctrl_nonowner_eval": ctrl_nonowner_eval,
    "superseded_group": superseded_group,
    "latest_group": latest_group,
    "discovered_group": discovered_group,
    "initial_alloc_superseded_disabled": initial_alloc_superseded_disabled,
    "initial_alloc_discovered_disabled": initial_alloc_discovered_disabled,
    "superseded_toggled_on": superseded_toggled_on,
    "after_alloc_superseded_disabled": after_alloc_superseded_disabled,
    "after_alloc_discovered_disabled": after_alloc_discovered_disabled,
    "toggled_on": toggled_on,
    "toggled_off": toggled_off,
    "metered_excluded_with_sub": metered_cand_with_sub.get("excluded"),
    "metered_selected_when_no_sub": plan_no_sub.get("choice"),
    "metered_fallback_flag": ((plan_no_sub.get("decision") or {{}}).get("task") or {{}}).get("metered_fallback"),
}}, ensure_ascii=False))
"""
    res_probe = run_in_box(box, op, [sc.TRAY_PYTHON, "-X", "utf8", "-c", quota_gate_probe], env)
    doc_p = parse_json(res_probe.stdout)
    op.check("quota/gate/scope probe exits 0", res_probe.returncode == 0, res_probe.stderr[-400:])
    op.check("on agy with 3p-weekly used up, a Gemini launch is allowed and a Claude-model launch is blocked",
             doc_p.get("agy_gemini_reason") is None
             and bool(doc_p.get("agy_claude_reason"))
             and "3p-weekly" in str(doc_p.get("agy_claude_reason"))
             and doc_p.get("agy_gemini_eval") == {}
             and (doc_p.get("agy_claude_eval") or {}).get("decision") == "block",
             {k: doc_p.get(k) for k in ("agy_gemini_reason", "agy_claude_reason", "agy_gemini_eval", "agy_claude_eval")})
    fb100 = doc_p.get("floor_blocks_100") or {}
    fb75 = doc_p.get("floor_blocks_75") or {}
    op.check("a 5h window at its floor blocks a new launch for claude, codex, and claude-2 (100% blocks all; 75% blocks claude/claude-2 Pro 25% floor)",
             all(bool(fb100.get(acc)) and "five_hour" in str(fb100.get(acc)) for acc in ("claude", "codex", "claude-2"))
             and bool(fb75.get("claude")) and bool(fb75.get("claude-2")) and fb75.get("codex") is None,
             {"at_100": fb100, "at_75": fb75})
    op.check("in-flight ledger reservations count against quota in the gate and secondary Claude accounts not named claude-2 resolve from bundle/labels",
             doc_p.get("no_res_reason") is None
             and bool(doc_p.get("with_res_reason"))
             and bool(doc_p.get("team_reason")),
             {k: doc_p.get(k) for k in ("no_res_reason", "with_res_reason", "team_reason")})
    op.check("a Magpie pool with accounts: 2 is judged on N×(100-mean) (2×20%=40% passes 25% floor, 1×20%=20% blocks)",
             doc_p.get("pool2_reason") is None and bool(doc_p.get("pool1_reason")),
             {"pool2_reason": doc_p.get("pool2_reason"), "pool1_reason": doc_p.get("pool1_reason")})
    op.check("a new data-declared scoped window (weekly_sonnet) blocks sonnet and allows opus with no code change",
             bool(doc_p.get("scoped_sonnet_reason"))
             and "weekly_sonnet" in str(doc_p.get("scoped_sonnet_reason"))
             and doc_p.get("scoped_opus_reason") is None,
             {"sonnet": doc_p.get("scoped_sonnet_reason"), "opus": doc_p.get("scoped_opus_reason")})
    op.check("the owning parent's control message to its running worker on an exhausted window passes, while idle, unknown-status, and non-owner messages are blocked",
             doc_p.get("ctrl_running_eval") == {}
             and (doc_p.get("ctrl_idle_eval") or {}).get("decision") == "block"
             and (doc_p.get("ctrl_unknown_eval") or {}).get("decision") == "block"
             and (doc_p.get("ctrl_nonowner_eval") or {}).get("decision") == "block",
             {k: doc_p.get(k) for k in ("ctrl_running_eval", "ctrl_idle_eval", "ctrl_unknown_eval", "ctrl_nonowner_eval")})
    sup = doc_p.get("superseded_group") or {}
    lat = doc_p.get("latest_group") or {}
    disc = doc_p.get("discovered_group") or {}
    op.check("superseded discovered models default to unchecked, latest & unknown new models default to checked, user checkbox overrides both ways, and tray agrees with routing",
             bool(sup.get("routed")) and sup.get("enabled") is False and doc_p.get("initial_alloc_superseded_disabled") is True
             and bool(lat.get("routed")) and lat.get("enabled") is True
             and bool(disc.get("routed")) and disc.get("enabled") is True and doc_p.get("initial_alloc_discovered_disabled") is False
             and doc_p.get("superseded_toggled_on") is True and doc_p.get("after_alloc_superseded_disabled") is False
             and doc_p.get("toggled_off") is False and doc_p.get("after_alloc_discovered_disabled") is True
             and doc_p.get("toggled_on") is True,
             {
                 "superseded": sup, "latest": lat, "discovered": disc,
                 "initial_sup_dis": doc_p.get("initial_alloc_superseded_disabled"),
                 "initial_disc_dis": doc_p.get("initial_alloc_discovered_disabled"),
                 "after_sup_dis": doc_p.get("after_alloc_superseded_disabled"),
                 "after_disc_dis": doc_p.get("after_alloc_discovered_disabled"),
             })
    op.check("a checked metered model is excluded when a subscription can take the work and chosen as fallback when no subscription is eligible",
             doc_p.get("metered_excluded_with_sub") == ["metered access is a last resort"]
             and doc_p.get("metered_selected_when_no_sub") == "metered_sol_high"
             and doc_p.get("metered_fallback_flag") is True,
             {k: doc_p.get(k) for k in ("metered_excluded_with_sub", "metered_selected_when_no_sub", "metered_fallback_flag")})


# ---------------------------------------------------------------------------------------------------------------------
# F05: Antigravity plan tier read from Google loadCodeAssist


def verify_f05_antigravity_plan_tier(ctx: dict):
    run: sc.Run = ctx["run"]
    fx: ForeignFixtureServer = ctx["fixture"]
    box = make_profile(ctx, "f05-agy-plan")
    op = run.operation(
        "F05 Antigravity plan tier from Google loadCodeAssist",
        "antigravity_plan reads Google loadCodeAssist using agy's keyring token, caches antigravity-plan.json under ROUTER_STATE_DIR, and honours AGENTCOMPASS_ANTIGRAVITY_PLAN=off",
    )
    scripts_dir = box.data / "skills" / "agent-compass" / "scripts"
    probe_code = (
        "import json, os, sys; from pathlib import Path; "
        f"sys.path.insert(0, {str(scripts_dir)!r}); "
        "import antigravity_plan as ap, provider_inventory as pi; "
        "sdir = Path(os.environ['ROUTER_STATE_DIR']); "
        "blob = lambda: json.dumps({'token': {'access_token': 'ya29.fixture-secret-token'}}); "
        "doc = ap.refresh(sdir, read_blob=blob); "
        "tier = pi.configured_plan('antigravity', ap.cached_plan(sdir)); "
        "off_thread = ap.refresh_in_background(sdir, environ={**os.environ, 'AGENTCOMPASS_ANTIGRAVITY_PLAN': 'off'}); "
        "print(json.dumps({'tier': tier, 'off_tier': off_thread, 'cache': ap.cache_path(sdir).as_posix(), 'doc': doc}))"
    )
    env = base_cli_env(box, ctx, extra={"AGENTCOMPASS_ANTIGRAVITY_PLAN": "1"})
    for tier_id, expected_plan in (("g1-pro-tier", "pro"), ("g1-ultra-lite-tier", "ultra5"), ("g1-ultra-tier", "ultra20")):
        fx.google_tier_id = tier_id
        res = run_in_box(box, op, [box.python, "-X", "utf8", "-c", probe_code], env)
        doc = parse_json(res.stdout)
        op.check(f"antigravity_plan resolves {expected_plan!r} from loadCodeAssist {tier_id}",
                 res.returncode == 0 and doc.get("tier") == expected_plan,
                 {"rc": res.returncode, "doc": doc, "stderr": res.stderr[-300:]})
    op.check("AGENTCOMPASS_ANTIGRAVITY_PLAN=off disables plan tier resolution",
             doc.get("off_tier") is None, doc)
    cache_file = box.state / "antigravity-plan.json"
    op.check("antigravity-plan.json is written under ROUTER_STATE_DIR and never leaks the OAuth token",
             cache_file.is_file() and "ya29.fixture-secret-token" not in cache_file.read_text(encoding="utf-8"),
             cache_file.read_text(encoding="utf-8") if cache_file.is_file() else "missing")


# ---------------------------------------------------------------------------------------------------------------------
# F06: Magpie pools, stale reading refresh (POST /v1/magpie/quotas/refresh), and router ×N capacity


def verify_f06_magpie_pools_and_refresh(ctx: dict) -> sc.Sandbox:
    run: sc.Run = ctx["run"]
    fx: ForeignFixtureServer = ctx["fixture"]
    box = make_profile(ctx, "f06-magpie-pool")
    op = run.operation(
        "F06 Magpie pool ×N, stale reading POST refresh, and router N×(100-mean) capacity",
        "Auto-discovered Magpie gateway refreshes hours-old readings via POST /v1/magpie/quotas/refresh, aggregates the largest plan's N accounts (pro ×2, 7d×2, 5h×2), masks member emails for hover, and gives the router N×(100-mean) remaining capacity",
    )
    box.claude.mkdir(parents=True, exist_ok=True)
    (box.claude / "settings.json").write_text(json.dumps({
        "env": {
            "ANTHROPIC_BASE_URL": "https://magpie.fixture.local",
            "ANTHROPIC_AUTH_TOKEN": "magpie-gateway-token",
        }
    }), encoding="utf-8")
    sign_in_codex(box)

    fx.magpie_refreshed_users.clear()
    fx.magpie_bob_fresh = False
    env = base_cli_env(box, ctx, include_clis=("claude", "codex"))
    run_in_box(box, op, [box.agentcompass, "setup", "--yes", "--no-hooks"], env)
    res_usage = run_in_box(box, op, [box.agentcompass, "usage"], env)
    op.check("agentcompass usage exits 0 against Magpie pool fixture", res_usage.returncode == 0, res_usage.stderr[-300:])
    op.check("collector automatically called POST /v1/magpie/quotas/refresh for the stale account (bob@example.com)",
             ("claude", "bob@example.com") in fx.magpie_refreshed_users, fx.magpie_refreshed_users)

    cache_doc = json.loads((box.state / "usage-cache.json").read_text(encoding="utf-8"))
    claude_row = (cache_doc.get("providers") or {}).get("claude") or {}
    op.check("refreshed Magpie pool reading is not stale",
             claude_row.get("available") is True and claude_row.get("stale") is False, claude_row)
    op.check("largest plan wins and shows pool multiplier 'pro ×2' (ignoring smaller 'free' plan)",
             claude_row.get("plan_label") == "pro ×2", claude_row.get("plan_label"))
    windows = {w["id"]: w for w in (claude_row.get("windows") or [])}
    op.check("windows carry labels '7d×2' (used_pct=25.0, accounts=2) and '5h×2' (used_pct=50.0, accounts=2)",
             windows.get("seven_day", {}).get("label") == "7d×2"
             and windows.get("seven_day", {}).get("used_pct") == 25.0
             and windows.get("seven_day", {}).get("accounts") == 2
             and windows.get("five_hour", {}).get("label") == "5h×2"
             and windows.get("five_hour", {}).get("used_pct") == 50.0
             and windows.get("five_hour", {}).get("accounts") == 2,
             windows)
    details = claude_row.get("usage_details") or {}
    members = details.get("members") or []
    op.check("usage_details.members stores masked emails (a***@..., b***@..., c***@...) and never raw emails or tokens",
             [m.get("label") for m in members] == ["a***@example.com", "b***@example.com", "c***@example.com"]
             and "alice@example.com" not in json.dumps(claude_row)
             and "magpie-gateway-token" not in json.dumps(claude_row),
             members)

    # Verify router counts N × (100 - mean) remaining capacity via `agentcompass usage --show`
    res_show = run_in_box(box, op, [box.agentcompass, "usage", "--show", "--accounts", "claude"], env)
    doc_show = parse_json(res_show.stdout)
    claude_quota = ((doc_show.get("accounts") or {}).get("claude") or {})
    op.check("router usage --show reports pool remaining correctly and fresh status",
             res_show.returncode == 0
             and claude_quota.get("status") == "fresh"
             and claude_quota.get("available") is True,
             claude_quota)
    return box


# ---------------------------------------------------------------------------------------------------------------------
# F07: Stranger account topologies + setup/discovery/uninstall home parity


def _home_files_snapshot(home: Path) -> dict[str, str]:
    """Snapshot files outside .agent-compass and AppData under `home` (normalized JSON or text)."""
    out: dict[str, str] = {}
    for path in sorted(home.rglob("*")):
        try:
            rel = path.relative_to(home)
        except ValueError:
            continue
        if any(part in (".agent-compass", "AppData") for part in rel.parts):
            continue
        if path.is_file() and not path.is_symlink():
            try:
                raw = path.read_text(encoding="utf-8")
                try:
                    norm = json.dumps(json.loads(raw), sort_keys=True)
                except ValueError:
                    norm = raw.replace("\r\n", "\n")
                out[rel.as_posix()] = hashlib.sha256(norm.encode("utf-8")).hexdigest()
            except OSError:
                pass
    return out


def _canonical_target_home(target_path: Path, box_home: Path) -> str:
    """Map a doc/hook/skill target path in setup-manifest.json to its harness account home."""
    p = target_path.resolve()
    bh = box_home.resolve()
    for special in (bh / ".gemini", bh / ".pi", bh / ".config" / "opencode"):
        spec_res = special.resolve()
        if p == spec_res or spec_res in p.parents:
            return spec_res.as_posix()
    if p.name in ("CLAUDE.md", "AGENTS.md", "settings.json", "hooks.json", "skills"):
        return p.parent.resolve().as_posix()
    if p.parent.name == "skills":
        return p.parent.parent.resolve().as_posix()
    return p.parent.resolve().as_posix()


def _verify_setup_discovery_uninstall_cycle(
    box: sc.Sandbox,
    op: sc.Operation,
    env: dict,
    label: str,
    expected_homes: set[Path],
    *,
    setup_extra_args: tuple[str, ...] = (),
) -> None:
    """Prove on a stranger profile that:
    1) setup --yes --hooks writes rules/skills/hooks into exactly the homes discovered by provider_inventory.discover();
    2) uninstall --yes removes every doc/hook/skill link setup wrote and restores pre-existing files unchanged;
    3) re-running setup leaves the profile ready for subsequent roll checks.
    """
    before = _home_files_snapshot(box.home)
    # Query provider_inventory.discover() + sync_host_globals.discovered_homes() inside the installed wheel environment BEFORE setup runs
    scripts_dir = box.data / "skills" / "agent-compass" / "scripts"
    setup_dir = box.data / "setup"
    disc_probe = (
        "import json, os, sys; from pathlib import Path; "
        f"sys.path.insert(0, {str(scripts_dir)!r}); sys.path.insert(0, {str(setup_dir)!r}); "
        "import onboard, provider_inventory, sync_host_globals as shg; "
        "home = Path(os.environ['HOME']); "
        "shg.set_home(home); "
        "inv = provider_inventory.discover(home=home, environ=onboard.detection_environ(home, os.environ)); "
        "homes = [h.resolve().as_posix() for _, h in shg.discovered_homes(inv, home)]; "
        "print(json.dumps({'homes': homes}))"
    )
    d_res = run_in_box(box, op, [box.python, "-X", "utf8", "-c", disc_probe], env)
    disc_doc = parse_json(d_res.stdout)
    discovered_homes = set(disc_doc.get("homes") or [])

    s_res = run_in_box(box, op, [box.agentcompass, "setup", "--yes", "--hooks", *setup_extra_args], env)
    op.check(f"[{label}] setup --yes --hooks exits 0", s_res.returncode == 0, (s_res.stdout + s_res.stderr)[-300:])

    manifest_path = box.state / "setup-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    doc_homes = {_canonical_target_home(Path(k), box.home) for k in (manifest.get("docs") or {})}
    hook_homes = {_canonical_target_home(Path(k), box.home) for k in (manifest.get("hooks") or {})}
    hub_link = (box.codex / "skills" / "agent-compass").resolve()
    skill_homes = {
        _canonical_target_home(Path(k), box.home)
        for k in list(manifest.get("junctions") or {}) + list(manifest.get("materialized") or {})
        if Path(k).resolve() != hub_link
    }
    expected_norm = {p.resolve().as_posix() for p in expected_homes}

    op.check(
        f"[{label}] router discovery homes match expected stranger homes",
        discovered_homes == expected_norm,
        {"discovered": sorted(discovered_homes), "expected": sorted(expected_norm)},
    )
    op.check(
        f"[{label}] setup writes global rule docs, hooks, and skill links into the exact set of homes the router discovers",
        doc_homes == discovered_homes
        and hook_homes == discovered_homes
        and skill_homes <= discovered_homes,
        {
            "doc_homes": sorted(doc_homes),
            "hook_homes": sorted(hook_homes),
            "skill_homes": sorted(skill_homes),
            "discovered_homes": sorted(discovered_homes),
        },
    )

    # Verify uninstall removes everything setup wrote and restores pre-existing files
    u_res = run_in_box(box, op, [box.agentcompass, "uninstall", "--yes"], env)
    op.check(f"[{label}] uninstall --yes exits 0", u_res.returncode == 0, (u_res.stdout + u_res.stderr)[-300:])
    after = _home_files_snapshot(box.home)
    op.check(
        f"[{label}] uninstall removes all setup-created files and restores pre-existing files byte/JSON-identical",
        after == before,
        {"before": before, "after": after},
    )

    # Re-run setup (--no-hooks) so subsequent roll checks can use the profile
    run_in_box(box, op, [box.agentcompass, "setup", "--yes", "--no-hooks", *setup_extra_args], env)


def verify_f07_stranger_topologies(ctx: dict):
    run: sc.Run = ctx["run"]
    op = run.operation(
        "F07 stranger account topologies & setup/discovery/uninstall parity",
        "Verify only-Claude, only-Codex, Magpie-Claude, agy-not-signed-in, no-Paseo, and Paseo custom CLAUDE_CONFIG_DIR + extends chain profiles: setup writes rules/skills/hooks to the exact homes discovery finds, uninstall removes what setup wrote, and roll works",
    )

    # 1. Only Claude (no Codex CLI or credentials, no Paseo, no API keys)
    box_claude = make_profile(ctx, "f07-only-claude")
    sign_in_claude(box_claude, plan="pro")
    env_claude = base_cli_env(box_claude, ctx, include_clis=("claude",))
    _verify_setup_discovery_uninstall_cycle(
        box_claude, op, env_claude, "only-Claude", {box_claude.claude}
    )
    # A dry run does not call the classifier unless --score is set (route_judge.plan_stage).
    # usage first so a missing reading is not what the plan reports.
    u_claude = run_in_box(box_claude, op, [box_claude.agentcompass, "usage"], env_claude)
    op.check("only-Claude usage reading is collected before planning", u_claude.returncode == 0, (u_claude.stdout + u_claude.stderr)[-300:])
    r_claude = run_in_box(box_claude, op, [
        box_claude.agentcompass, "roll",
        "--task", "Add a docstring to parse_header in http_utils.py and run its unit test across modules.",
        "--parent-host", "native", "--parent-id", "stranger-claude-parent", "--parent-address", "stranger-claude-inbox",
        "--dry-run", "--score",
    ], env_claude)
    doc_claude = parse_json(r_claude.stdout)
    op.check("only-Claude machine classifies the task and selects the signed-in claude account",
             r_claude.returncode == 0
             and doc_claude.get("status") == "dry_run"
             and (doc_claude.get("worker") or {}).get("account") == "claude"
             and (doc_claude.get("worker") or {}).get("provider") == "claude",
             {"rc": r_claude.returncode, "doc": doc_claude, "stderr": r_claude.stderr[-200:]})

    # 2. Only Codex (no Claude CLI or credentials, no Paseo, no API keys)
    box_codex = make_profile(ctx, "f07-only-codex")
    sign_in_codex(box_codex)
    env_codex = base_cli_env(box_codex, ctx, include_clis=("codex",))
    _verify_setup_discovery_uninstall_cycle(
        box_codex, op, env_codex, "only-Codex", {box_codex.codex}
    )
    u_codex = run_in_box(box_codex, op, [box_codex.agentcompass, "usage"], env_codex)
    op.check("only-Codex usage reading is collected before planning", u_codex.returncode == 0, (u_codex.stdout + u_codex.stderr)[-300:])
    r_codex = run_in_box(box_codex, op, [
        box_codex.agentcompass, "roll",
        "--task", "Add a docstring to parse_header in http_utils.py and run its unit test across modules.",
        "--parent-host", "native", "--parent-id", "stranger-codex-parent", "--parent-address", "stranger-codex-inbox",
        "--dry-run", "--score",
    ], env_codex)
    doc_codex = parse_json(r_codex.stdout)
    op.check("only-Codex machine classifies the task and selects the signed-in codex account",
             r_codex.returncode == 0
             and doc_codex.get("status") == "dry_run"
             and (doc_codex.get("worker") or {}).get("account") == "codex"
             and (doc_codex.get("worker") or {}).get("provider") == "codex",
             {"rc": r_codex.returncode, "doc": doc_codex, "stderr": r_codex.stderr[-200:]})

    # 3. Magpie Claude (no .credentials.json; ~/.claude/settings.json has Magpie gateway URL + token; no Paseo)
    box_magpie = make_profile(ctx, "f07-magpie-claude")
    box_magpie.claude.mkdir(parents=True, exist_ok=True)
    (box_magpie.claude / "settings.json").write_text(json.dumps({
        "env": {
            "ANTHROPIC_BASE_URL": "https://magpie.fixture.local",
            "ANTHROPIC_AUTH_TOKEN": "magpie-gateway-token",
        }
    }), encoding="utf-8")
    env_magpie = base_cli_env(box_magpie, ctx, include_clis=("claude",))
    _verify_setup_discovery_uninstall_cycle(
        box_magpie, op, env_magpie, "Magpie-Claude", {box_magpie.claude}
    )

    # 4. agy installed on PATH, but NOT signed in (+ Codex signed in, no Paseo)
    box_agy = make_profile(ctx, "f07-agy-unsigned")
    sign_in_codex(box_agy)
    env_agy = base_cli_env(box_agy, ctx, include_clis=("codex",))
    bin_dir = Path(env_agy["FIXTURE_BIN"])
    if os.name == "nt":
        shutil.copy2(bin_dir / "codex.exe", bin_dir / "agy.exe")
    else:
        shutil.copy2(bin_dir / "codex", bin_dir / "agy")
    # Before setup (or when user declines unverifiable agy via --disable antigravity), sign_in_proven is False
    _verify_setup_discovery_uninstall_cycle(
        box_agy, op, env_agy, "agy-not-signed-in",
        {box_agy.codex, box_agy.home / ".gemini"},
        setup_extra_args=("--disable", "antigravity"),
    )
    ov_agy = json.loads((box_agy.state / "accounts-override.json").read_text(encoding="utf-8"))
    policy_path = box_agy.state / "host-policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8")) if policy_path.is_file() else {}
    allowed = policy.get("allowed_accounts") or []
    agy_override = (ov_agy.get("accounts") or {}).get("antigravity") or {}
    # router-internals: on/off lives only in host policy allowed_accounts. verified is launch
    # evidence; setup/onboard.py strips a legacy verified:false and records the choice in host policy.
    op.check(
        "when agy is not signed in and kept out of routing, host policy omits antigravity and the override does not store verified=false",
        "antigravity" not in allowed and agy_override.get("verified") is not False,
        {"allowed": allowed, "override": ov_agy},
    )

    # 5. Paseo provider with a custom CLAUDE_CONFIG_DIR and 2-hop extends chain (team-anthropic -> claude-base -> claude)
    #    Also give primary ~/.claude and secondary custom_claude_dir distinct model/effortLevel/env in settings.json
    #    to verify that setup adds the gate hook without clobbering the second home's settings, and uninstall restores both.
    box_paseo = make_profile(ctx, "f07-paseo-extends")
    sign_in_codex(box_paseo)
    box_paseo.claude.mkdir(parents=True, exist_ok=True)
    (box_paseo.claude / "settings.json").write_text(
        json.dumps({"model": "opus[1m]", "effortLevel": "high", "env": {"A": "1"}}, indent=2),
        encoding="utf-8",
    )
    custom_claude_dir = box_paseo.home / ".custom-claude-team"
    custom_claude_dir.mkdir(parents=True, exist_ok=True)
    (custom_claude_dir / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "foreign-team-claude-token", "subscriptionType": "max"}}),
        encoding="utf-8",
    )
    remember_claude_plan(box_paseo, "foreign-team-claude-token", "max")
    (custom_claude_dir / "settings.json").write_text(
        json.dumps({"model": "opus", "effortLevel": "medium", "env": {"B": "2"}}, indent=2),
        encoding="utf-8",
    )
    paseo_dir = box_paseo.home / ".paseo"
    paseo_dir.mkdir(parents=True, exist_ok=True)
    (paseo_dir / "config.json").write_text(json.dumps({
        "daemon": {},
        "agents": {
            "providers": {
                "claude-base": {
                    "extends": "claude",
                },
                "team-anthropic": {
                    "extends": "claude-base",
                    "env": {
                        "CLAUDE_CONFIG_DIR": str(custom_claude_dir),
                    },
                },
            }
        }
    }, indent=2), encoding="utf-8")
    env_paseo = base_cli_env(box_paseo, ctx, include_clis=("codex", "claude"))
    _verify_setup_discovery_uninstall_cycle(
        box_paseo, op, env_paseo, "Paseo-custom-CLAUDE_CONFIG_DIR-extends-chain",
        {box_paseo.codex, box_paseo.claude, custom_claude_dir},
    )

    # 6. Secondary Claude home settings survival + Old-manifest upgrade:
    #    - Verify that setup --yes --hooks preserves custom_claude_dir's own model/effortLevel/env alongside the gate hook
    #    - When custom_claude_dir is retired from .paseo/config.json and removed, re-running setup --yes --hooks cleans up
    #      its managed doc, hook, and skill link and drops it from setup-manifest.json
    run_in_box(box_paseo, op, [box_paseo.agentcompass, "setup", "--yes", "--hooks"], env_paseo)
    primary_settings = json.loads((box_paseo.claude / "settings.json").read_text(encoding="utf-8"))
    second_settings = json.loads((custom_claude_dir / "settings.json").read_text(encoding="utf-8"))
    op.check(
        "[secondary-settings] a second Claude home's model/effortLevel/env survive setup --yes --hooks alongside the gate hook",
        (primary_settings.get("model"), primary_settings.get("effortLevel"), primary_settings.get("env")) == ("opus[1m]", "high", {"A": "1"})
        and (second_settings.get("model"), second_settings.get("effortLevel"), second_settings.get("env")) == ("opus", "medium", {"B": "2"})
        and zd.GATE_HOOK.search(json.dumps(primary_settings.get("hooks")))
        and zd.GATE_HOOK.search(json.dumps(second_settings.get("hooks"))),
        {"primary": primary_settings, "second": second_settings},
    )
    (paseo_dir / "config.json").write_text(json.dumps({"daemon": {}, "agents": {"providers": {}}}, indent=2), encoding="utf-8")
    r_upgrade = run_in_box(box_paseo, op, [box_paseo.agentcompass, "setup", "--yes", "--hooks"], env_paseo)
    manifest_up = json.loads((box_paseo.state / "setup-manifest.json").read_text(encoding="utf-8"))
    up_doc_homes = {_canonical_target_home(Path(k), box_paseo.home) for k in (manifest_up.get("docs") or {})}
    up_hook_homes = {_canonical_target_home(Path(k), box_paseo.home) for k in (manifest_up.get("hooks") or {})}
    retired_pos = custom_claude_dir.resolve().as_posix()
    retired_settings_after = json.loads((custom_claude_dir / "settings.json").read_text(encoding="utf-8"))
    op.check(
        "[manifest-upgrade] retiring a custom home from discovery cleans up its managed doc/hook/skill, restores its settings.json, and removes it from setup-manifest.json",
        r_upgrade.returncode == 0
        and retired_pos not in up_doc_homes
        and retired_pos not in up_hook_homes
        and not (custom_claude_dir / "CLAUDE.md").exists()
        and retired_settings_after == {"model": "opus", "effortLevel": "medium", "env": {"B": "2"}}
        and not (custom_claude_dir / "skills").exists(),
        {"up_doc_homes": sorted(up_doc_homes), "up_hook_homes": sorted(up_hook_homes), "retired_settings_after": retired_settings_after},
    )


# ---------------------------------------------------------------------------------------------------------------------
# F08: Tray account cards & hover + usage-float standalone & hosted rows/hover


def verify_f08_tray_cards_and_usage_float(ctx: dict, magpie_box: sc.Sandbox):
    run: sc.Run = ctx["run"]
    op = run.operation(
        "F08 tray account cards & usage-float rows and hover",
        "Clone usage-float as another user would, verify tray account cards (remaining %, ×N pool, no window tag on week-only accounts, slim masked hover) and usage-float rows (left_pct 150%/100% on ×2 pool, week-only titles, masked pool_detail_text hover, shared cache)",
    )
    env = base_cli_env(magpie_box, ctx, include_clis=("claude", "codex"))

    # Add a week-only provider reading (grok) to magpie_box's usage-cache.json to verify week-only display in both tray and usage-float
    cache_path = magpie_box.state / "usage-cache.json"
    cache_doc = json.loads(cache_path.read_text(encoding="utf-8"))
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    reset_7d = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=5)).isoformat()
    cache_doc.setdefault("providers", {})["grok"] = {
        "provider_id": "grok",
        "display_name": "grok",
        "plan_label": "SuperGrok",
        "available": True,
        "stale": False,
        "fetched_at": now_iso,
        "windows": [{"id": "seven_day", "label": "7d", "used_pct": 18.0, "resets_at": reset_7d}],
    }
    cache_path.write_text(json.dumps(cache_doc, indent=2), encoding="utf-8")

    # 1. Install usage-float into ~/.agent-compass/plugins/usage-float via `agentcompass setup --yes --no-hooks --usage-float`
    res_setup_uf = run_in_box(magpie_box, op, [magpie_box.agentcompass, "setup", "--yes", "--no-hooks", "--usage-float"], env)
    uf_plugin_dir = magpie_box.state / "plugins" / "usage-float"
    op.check("setup --usage-float clones usage-float into ~/.agent-compass/plugins/usage-float",
             res_setup_uf.returncode == 0 and (uf_plugin_dir / "usage_float.py").is_file() and (uf_plugin_dir / ".git").exists(),
             res_setup_uf.stdout + res_setup_uf.stderr)

    # 2. Verify Tray account cards (router_data.account_cards) and hover (panel_quota.quota_tooltip_text) in an isolated subprocess
    tray_dir = ctx["repo"] / "tray"
    tray_probe = f"""
import json, os, sys
sys.path.insert(0, {str(tray_dir)!r})
import router_data, panel_quota
cards = {{c['account']: c for c in router_data.account_cards()}}
claude_card = cards.get('claude') or {{}}
grok_card = cards.get('grok') or {{}}
claude_tags = {{w['id']: panel_quota.card_window_name(claude_card, w) for w in claude_card.get('windows', [])}}
grok_tags = [panel_quota.card_window_name(grok_card, w) for w in grok_card.get('windows', [])]
hover = panel_quota.quota_tooltip_text(claude_card)
print(json.dumps({{
    'claude': claude_card,
    'grok': grok_card,
    'claude_tags': claude_tags,
    'grok_tags': grok_tags,
    'hover': hover,
}}, ensure_ascii=False))
"""
    res_tray = run_in_box(magpie_box, op, [sc.TRAY_PYTHON, "-X", "utf8", "-c", tray_probe], env)
    doc_tray = parse_json(res_tray.stdout)
    claude_card = doc_tray.get("claude") or {}
    grok_card = doc_tray.get("grok") or {}
    claude_tags = doc_tray.get("claude_tags") or {}
    grok_tags = doc_tray.get("grok_tags") or []
    hover_text = doc_tray.get("hover") or ""
    op.check("tray router_data.account_cards() succeeds", res_tray.returncode == 0 and bool(claude_card), res_tray.stderr[-300:])
    claude_wins = {w["id"]: w for w in (claude_card.get("windows") or [])}
    op.check("tray card shows remaining_pct (150% on 7d×2, 100% on 5h×2) and pool_members for 'pro ×2'",
             claude_card.get("plan") == "pro"
             and len(claude_card.get("pool_members") or []) == 3
             and claude_wins.get("seven_day", {}).get("remaining_pct") == 150.0
             and claude_tags.get("seven_day") == "7d×2"
             and claude_wins.get("five_hour", {}).get("remaining_pct") == 100.0
             and claude_tags.get("five_hour") == "5h×2",
             {"claude_card": claude_card, "claude_tags": claude_tags})
    grok_wins = grok_card.get("windows") or []
    op.check("tray card has no window tag (tag=='') on a week-only account (grok) and shows 82% remaining_pct",
             len(grok_wins) == 1 and grok_tags == [""] and grok_wins[0].get("remaining_pct") == 82.0,
             {"grok_wins": grok_wins, "grok_tags": grok_tags})
    op.check("tray hover tip renders masked pool member lines with remaining % and no raw emails",
             "a***" in hover_text and "b***" in hover_text
             and "@example.com" not in hover_text and "alice" not in hover_text,
             hover_text)

    # 3. Verify usage-float standalone & hosted behavior against the cloned plugin in ~/.agent-compass/plugins/usage-float
    scripts_dir = magpie_box.data / "skills" / "agent-compass" / "scripts"
    uf_probe = f"""
import json, os, sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, {str(uf_plugin_dir)!r})
sys.path.insert(0, {str(tray_dir)!r})
sys.path.insert(0, {str(scripts_dir)!r})
import usage_float as uf
import usage_float_host as host
import window_scope

shared = host.share_cache(uf)
providers = uf.collect_local_usage(['claude', 'codex'], force=False)
grok = uf.cache_get_provider('grok')
if grok:
    providers.append(grok)
rows = [
    {{
        'title': r.title,
        'used_pct': r.used_pct,
        'left_pct': r.left_pct,
        'accounts': r.accounts,
        'provider_id': r.provider_id,
        'window_id': r.window_id,
        'detail': r.detail,
    }}
    for r in uf.iter_display_rows(providers)
]
scoped_payload = {{
    'limits': [
        {{
            'kind': 'weekly_scoped',
            'scope': {{'model': {{'display_name': 'Sonnet'}}}},
            'percent': 85.0,
            'resets_at': '2026-11-09T00:00:00Z',
        }}
    ],
    'five_hour': {{'utilization': 10.0, 'resets_at': '2026-11-05T00:00:00Z'}},
}}
with patch.object(uf, 'read_claude_oauth', return_value=({{'accessToken': 't'}}, None)), \\
     patch.object(uf, 'claude_authorized_json', return_value=(200, scoped_payload)):
    parsed_scoped = uf.fetch_claude_usage(force=True, provider_id='claude-scoped-test')
serialized_scoped = uf._provider_to_dict(parsed_scoped)
ws_win = next((w for w in serialized_scoped.get('windows', []) if w.get('id') == 'weekly_sonnet'), {{}})
ws_binds_sonnet = window_scope.binds('claude-sonnet-5-5', ws_win) if ws_win else False
ws_binds_opus = window_scope.binds('claude-opus-5-5', ws_win) if ws_win else True
print(json.dumps({{
    'shared_cache': str(shared),
    'rows': rows,
    'weekly_sonnet_window': ws_win,
    'ws_binds_sonnet': ws_binds_sonnet,
    'ws_binds_opus': ws_binds_opus,
}}, ensure_ascii=False))
"""
    res_uf = run_in_box(magpie_box, op, [sc.TRAY_PYTHON, "-X", "utf8", "-c", uf_probe], env)
    doc_uf = parse_json(res_uf.stdout)
    rows = {r["title"]: r for r in (doc_uf.get("rows") or [])}
    op.check("hosted usage-float shares ~/.agent-compass/usage-cache.json",
             res_uf.returncode == 0 and Path(doc_uf.get("shared_cache") or "") == cache_path,
             {"rc": res_uf.returncode, "doc": doc_uf, "stderr": res_uf.stderr[-300:]})
    op.check("usage-float fetch_claude_usage sets models=['Sonnet'] on weekly_sonnet and window_scope.binds scopes it to Sonnet only",
             (doc_uf.get("weekly_sonnet_window") or {}).get("models") == ["Sonnet"]
             and doc_uf.get("ws_binds_sonnet") is True
             and doc_uf.get("ws_binds_opus") is False,
             {k: doc_uf.get(k) for k in ("weekly_sonnet_window", "ws_binds_sonnet", "ws_binds_opus")})
    op.check("usage-float row 'claude 7d×2' shows left_pct=150 (2 × 75%) and 'claude 5h×2' shows left_pct=100 (2 × 50%)",
             rows.get("claude 7d×2", {}).get("left_pct") == 150
             and rows.get("claude 5h×2", {}).get("left_pct") == 100,
             rows)
    detail = rows.get("claude 7d×2", {}).get("detail") or ""
    op.check("usage-float hover detail (pool_detail_text) lists masked pool members and remaining %",
             "a***" in detail and "b***" in detail and "@example.com" not in detail and "alice" not in detail,
             detail)
    op.check("usage-float titles week-only account 'grok' without a '7d' suffix and shows left_pct=82",
             "grok" in rows and "grok 7d" not in rows and rows["grok"].get("left_pct") == 82,
             list(rows.keys()))

    # 4. Also run `python usage_float.py once` CLI mode directly from the cloned plugin directory
    uf_cfg_dir = magpie_box.home / ".usage-float"
    uf_cfg_dir.mkdir(parents=True, exist_ok=True)
    (uf_cfg_dir / "config.json").write_text(
        json.dumps({"items": [{"id": "claude", "type": "claude"}, {"id": "codex", "type": "codex"}]}, indent=2),
        encoding="utf-8",
    )
    res_once = run_in_box(magpie_box, op, [magpie_box.python, "-X", "utf8", str(uf_plugin_dir / "usage_float.py"), "once"], env)
    once_out = res_once.stdout or ""
    # Codex pro declares only the weekly usage window (plan-templates.json). A week-only account
    # has no window tag, the same display rule as the grok card above. A five-hour row would mean
    # the fixture reported a window this plan does not price.
    op.check("usage_float.py once CLI runs cleanly and shows the Magpie pool plus a week-only Codex pro row",
             res_once.returncode == 0
             and "claude 7d×2" in once_out and "150%" in once_out
             and "claude 5h×2" in once_out and "100%" in once_out
             and any(line.startswith("codex ") and "80%" in line and "5h" not in line for line in once_out.splitlines())
             and not any(line.startswith("codex 5h") for line in once_out.splitlines()),
             {"rc": res_once.returncode, "stdout": once_out, "stderr": res_once.stderr[-200:]})


# ---------------------------------------------------------------------------------------------------------------------
# F10: Evidence-calibrated routing after 0.3.6


def verify_f10_evidence_calibrated_routing(ctx: dict):
    run: sc.Run = ctx["run"]
    fx: ForeignFixtureServer = ctx["fixture"]
    box = make_profile(ctx, "f10-evidence-routing")
    op = run.operation(
        "F10 evidence-calibrated routing after 0.3.6",
        "From the installed wheel in a foreign profile (Magpie Claude pool of two + Codex): first-scan checkboxes follow "
        "public data superseded; metered accounts are a last resort and never-billed accounts explicit selection only; the "
        "pool's reset is its members' later reset. Every check is also run once against broken data in the profile and "
        "must fail there.",
    )
    box.claude.mkdir(parents=True, exist_ok=True)
    (box.claude / "settings.json").write_text(json.dumps({"env": {
        "ANTHROPIC_BASE_URL": "https://magpie.fixture.local", "ANTHROPIC_AUTH_TOKEN": "magpie-gateway-token"}}), encoding="utf-8")
    sign_in_codex(box)
    fx.magpie_refreshed_users.clear()
    fx.magpie_bob_fresh = False
    later = (dt.datetime.fromisoformat(fx.reset_7d) + dt.timedelta(hours=20)).isoformat()
    fx.magpie_bob_reset_7d = later
    try:
        env = base_cli_env(box, ctx, include_clis=("claude", "codex"))
        run_in_box(box, op, [box.agentcompass, "setup", "--yes", "--no-hooks"], env)
        res_usage = run_in_box(box, op, [box.agentcompass, "usage"], env)
    finally:
        fx.magpie_bob_reset_7d = None
    op.check("agentcompass usage reads the Magpie pool", res_usage.returncode == 0, res_usage.stderr[-300:])

    # 5b. The pool's weekly reset is its members' later reset (every account refilled), from the installed collector.
    cache = json.loads((box.state / "usage-cache.json").read_text(encoding="utf-8"))
    claude_windows = ((cache.get("providers") or {}).get("claude") or {}).get("windows") or []
    week = next((w for w in claude_windows if w.get("id") == "seven_day"), {})

    def later_reset(window):
        stamp = window.get("resets_at")
        return (window.get("accounts") == 2 and bool(stamp)
                and dt.datetime.fromisoformat(stamp) == dt.datetime.fromisoformat(later))

    op.check("a pool's reset is its members' later reset", later_reset(week), week)
    op.check("(failure shown) the reset check fails on a reading carrying the earlier member reset",
             not later_reset(dict(week, resets_at=fx.reset_7d)), week)

    # The installed modules, intact and against broken data in this profile.
    probe = HERE / "f10_evidence_probe.py"
    scripts_dir = box.data / "skills" / "agent-compass" / "scripts"
    tray_dir = ctx["repo"] / "tray"
    results: dict[str, dict] = {}
    for mode in ("intact", "broken"):
        out = box.root / f"f10-{mode}.json"
        res = run_in_box(box, op, [sc.TRAY_PYTHON, "-X", "utf8", probe, scripts_dir, tray_dir, mode, out], env)
        op.check(f"F10 probe ({mode}) exits 0", res.returncode == 0 and out.is_file(), res.stderr[-600:])
        results[mode] = json.loads(out.read_text(encoding="utf-8")) if out.is_file() else {}
    for leftover in ("f10-broken-accounts.json", "f10-scenario-cache.json"):
        (box.state / leftover).unlink(missing_ok=True)
    for name, outcome in results["intact"].items():
        op.check(name, outcome.get("ok"), outcome.get("detail"))
        broken = results["broken"].get(name) or {}
        op.check(f"(failure shown) {name}: fails with its data broken", bool(broken) and not broken.get("ok"),
                 broken.get("detail"))
    op.check("the probe ran every F10 check",
             len(results["intact"]) >= 2 and set(results["intact"]) == set(results["broken"]), sorted(results["intact"]))


# ---------------------------------------------------------------------------------------------------------------------
# F11: Oh My Pi OAuth subscription logins


def verify_f11_omp_oauth_subscriptions(ctx: dict):
    run: sc.Run = ctx["run"]
    box = make_profile(ctx, "f11-omp-oauth")
    op = run.operation(
        "F11 Oh My Pi OAuth subscription logins",
        "From the installed skill in a foreign profile, with synthetic ~/.omp/agent/agent.db files (omp's table shape, no "
        "real credential; omp is never run): an OAuth login on the native Codex account is a subscription sharing that "
        "account's quota, its bundles sit on the Codex account with omp launch ids and no twin, and the plan treats them "
        "like Pi's; an omp-only login is its own subscription account; stored API keys keep the metered side; disabled "
        "credentials are ignored; two logins are two accounts; a corrupt or locked database is missing_credentials. Every "
        "check is also run once against broken input in the profile and must fail there.",
    )
    probe = HERE / "f11_omp_probe.py"
    scripts_dir = box.data / "skills" / "agent-compass" / "scripts"
    env = base_cli_env(box, ctx, include_clis=())
    results: dict[str, dict] = {}
    for mode in ("intact", "broken"):
        out = box.root / f"f11-{mode}.json"
        res = run_in_box(box, op, [box.python, "-I", "-X", "utf8", probe, scripts_dir, mode, out], env)
        op.check(f"F11 probe ({mode}) exits 0", res.returncode == 0 and out.is_file(), res.stderr[-600:])
        results[mode] = json.loads(out.read_text(encoding="utf-8")) if out.is_file() else {}
    for name, outcome in results["intact"].items():
        op.check(name, outcome.get("ok"), outcome.get("detail"))
        broken = results["broken"].get(name) or {}
        op.check(f"(failure shown) {name}: fails with its input broken", bool(broken) and not broken.get("ok"),
                 broken.get("detail"))
    op.check("the probe ran every F11 check",
             len(results["intact"]) >= 2 and set(results["intact"]) == set(results["broken"]), sorted(results["intact"]))


# ---------------------------------------------------------------------------------------------------------------------
# F12: Tray version display, one-click self-update & single-flight tray restart


def verify_f12_self_update_and_version_display(ctx: dict):
    import re
    run: sc.Run = ctx["run"]
    box = make_profile(ctx, "f12-self-update")
    op = run.operation(
        "F12 Tray version display, one-click self-update & single-flight tray restart",
        "In a foreign profile with an older 0.3.7 router wheel installed alongside the newer tray: inspect_installs "
        "flags the version mismatch across PATH, CODEX_HOME/skills, and gate hooks; update_agentcompass refuses a "
        "mismatched sha256 digest and refuses git pull on a dirty checkout while upgrading the installed 0.3.7 wheel "
        "to the locally built wheel via its owning Python interpreter, running agentcompass data --refresh against the "
        "fixture server, and recording every step; single-flight restart mutex and restart marker work end-to-end.",
    )
    old_ver = "0.3.7"
    old_repo = box.root / "repo-0.3.7"
    shutil.copytree(ctx["repo"], old_repo)
    init_py = old_repo / "agentcompass" / "__init__.py"
    init_py.write_text(
        re.sub(r'__version__\s*=\s*["\'][^"\']+["\']', f'__version__ = "{old_ver}"', init_py.read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    pyproject = old_repo / "pyproject.toml"
    pyproject.write_text(
        re.sub(r'(?m)^version\s*=\s*"[^"]+"', f'version = "{old_ver}"', pyproject.read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    old_wheel = sc.build_wheel(old_repo, box.root / "dist-0.3.7")
    op.check("built 0.3.7 fixture wheel for upgrade test", old_wheel.is_file() and "0.3.7" in old_wheel.name, old_wheel.name)

    old_venv = box.root / "venv-self-update"
    subprocess.run([sys.executable, "-m", "venv", str(old_venv)], check=True, capture_output=True)
    old_py = old_venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    old_cli = old_venv / ("Scripts/agentcompass.exe" if os.name == "nt" else "bin/agentcompass")
    subprocess.run(
        [str(old_py), "-m", "pip", "install", "--no-index", "--no-deps", "--quiet", str(old_wheel)],
        check=True,
        capture_output=True,
    )
    old_sites = (old_venv / "Lib" / "site-packages") if os.name == "nt" else next((old_venv / "lib").glob("python*/site-packages"))
    write_foreign_netguard([old_sites], ctx["fixture"].url, [ctx["fixture"].port], ctx["base"] / "net-foreign.jsonl")

    ctx["fixture"].public_data_mode = "valid_update"
    env = base_cli_env(box, ctx, include_clis=(), extra={"AGENTCOMPASS_PUBLIC_DATA": "1"})
    env["PATH"] = str(old_cli.parent) + os.pathsep + env.get("PATH", "")
    env["PIP_NO_INDEX"] = "1"

    sign_in_codex(box)
    sign_in_claude(box)
    setup_res = run_in_box(box, op, [old_cli, "setup", "--yes", "--hooks"], env)
    op.check("setup with 0.3.7 wheel succeeds in foreign profile", setup_res.returncode == 0, setup_res.stderr[-400:])

    ver_before = run_in_box(box, op, [old_cli, "--version"], env)
    op.check("installed router CLI starts at version 0.3.7", ver_before.returncode == 0 and ver_before.stdout.strip() == "0.3.7", ver_before.stdout)

    dirty_repo = box.root / "dirty-repo"
    shutil.copytree(ctx["repo"], dirty_repo)
    (dirty_repo / "README.md").write_text("dirty local change\n", encoding="utf-8")

    new_wheel: Path = ctx["wheel"]
    new_wheel_sha = hashlib.sha256(new_wheel.read_bytes()).hexdigest()
    new_ver_match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', (ctx["repo"] / "agentcompass" / "__init__.py").read_text(encoding="utf-8"))
    new_ver = new_ver_match.group(1) if new_ver_match else "0.3.8"

    probe = box.root / "f12_probe.py"
    out_json = box.root / "f12_result.json"
    probe.write_text(
        """import json, os, sys
from pathlib import Path

tray_dir = Path(sys.argv[1])
dirty_repo = Path(sys.argv[2])
old_sites = Path(sys.argv[3])
old_cli = Path(sys.argv[4])
new_wheel = Path(sys.argv[5])
new_wheel_sha = sys.argv[6]
new_ver = sys.argv[7]
out_path = Path(sys.argv[8])

sys.path.insert(0, str(tray_dir))
import deploy
import update_status
import updates

home = Path(os.environ["USERPROFILE"])
state_dir = Path(os.environ["ROUTER_STATE_DIR"])

shapes = {
    "git": updates.detect_install_shape(dirty_repo, frozen=False),
    "wheel": updates.detect_install_shape(old_sites, frozen=False),
}
before_info = updates.inspect_installs(root=dirty_repo, home=home, which_path=old_cli, frozen=False)

bad_digest_refused = False
bad_digest_err = ""
try:
    updates.update_agentcompass(
        root=dirty_repo,
        home=home,
        state_dir=state_dir,
        which_path=old_cli,
        frozen=False,
        release={
            "tag_name": f"v{new_ver}",
            "assets": [{"name": new_wheel.name, "url": new_wheel.as_uri(), "digest": "sha256:" + "0" * 64}],
        },
        restart_fn=lambda: "restarted",
    )
except RuntimeError as exc:
    bad_digest_refused = True
    bad_digest_err = str(exc)

restarted = []
summary = updates.update_agentcompass(
    root=dirty_repo,
    home=home,
    state_dir=state_dir,
    which_path=old_cli,
    frozen=False,
    release={
        "tag_name": f"v{new_ver}",
        "assets": [{"name": new_wheel.name, "url": new_wheel.as_uri(), "digest": f"sha256:{new_wheel_sha}"}],
    },
    restart_fn=lambda: restarted.append(True) or "托盘已重启",
)

after_info = updates.inspect_installs(root=dirty_repo, home=home, which_path=old_cli, frozen=False)
status_doc = update_status.read_status(directory=state_dir)

# Single-flight restart mutex + restart marker check
deploy.RESTART_MUTEX_NAME = "Local\\\\AgentCompassF12TestMutex"
deploy.services.STATE_DIR = state_dir
h1 = deploy.acquire_restart_mutex()
in_progress_while_held = deploy.restart_in_progress()
h2 = deploy.acquire_restart_mutex()
deploy.release_restart_mutex(h1)
in_progress_after_release = deploy.restart_in_progress()

deploy.write_restart_marker(version=new_ver)
consumed1 = deploy.consume_restart_marker()
consumed2 = deploy.consume_restart_marker()

out_path.write_text(json.dumps({
    "shapes": shapes,
    "before_mismatch": before_info["version_mismatch"],
    "before_display": before_info["display_version"],
    "before_routers": {k: v["version"] for k, v in before_info["routers"].items()},
    "bad_digest_refused": bad_digest_refused,
    "bad_digest_err": bad_digest_err,
    "summary": summary,
    "restarted_count": len(restarted),
    "after_mismatch": after_info["version_mismatch"],
    "after_display": after_info["display_version"],
    "after_routers": {k: v["version"] for k, v in after_info["routers"].items()},
    "status_running": status_doc.get("running"),
    "status_results": status_doc.get("results"),
    "mutex_ok": bool(h1) and in_progress_while_held and (h2 is None) and (not in_progress_after_release),
    "marker_ok": isinstance(consumed1, dict) and consumed1.get("version") == new_ver and consumed2 is None,
}, ensure_ascii=False, indent=2), encoding="utf-8")
""",
        encoding="utf-8",
    )

    res = run_in_box(
        box,
        op,
        [
            sc.TRAY_PYTHON,
            "-X",
            "utf8",
            probe,
            ctx["repo"] / "tray",
            dirty_repo,
            old_sites,
            old_cli,
            new_wheel,
            new_wheel_sha,
            new_ver,
            out_json,
        ],
        env,
    )
    op.check("F12 self-update probe exits 0", res.returncode == 0 and out_json.is_file(), res.stderr[-600:])
    data = json.loads(out_json.read_text(encoding="utf-8")) if out_json.is_file() else {}
    op.check("detect_install_shape distinguishes git and wheel installs", data.get("shapes") == {"git": "git", "wheel": "wheel"}, data.get("shapes"))
    op.check(
        "inspect_installs detects version mismatch across PATH, skills, and gate hooks before update",
        bool(data.get("before_mismatch"))
        and f"AgentCompass {new_ver} · 命令行 0.3.7（版本不一致）" == data.get("before_display")
        and data.get("before_routers", {}).get("path") == "0.3.7"
        and data.get("before_routers", {}).get("hooks") == "0.3.7",
        data,
    )
    op.check(
        "update_agentcompass refuses a wheel with a mismatched sha256 digest",
        bool(data.get("bad_digest_refused")) and "校验不匹配" in str(data.get("bad_digest_err")),
        data.get("bad_digest_err"),
    )
    op.check(
        "update_agentcompass refuses git pull on dirty checkout while upgrading the 0.3.7 wheel via pip and refreshing public data",
        "有本地改动" in str(data.get("summary"))
        and "pip" in str(data.get("summary"))
        and "data --refresh" in str(data.get("summary"))
        and data.get("restarted_count") == 1
        and data.get("status_running") is False
        and bool(data.get("status_results")),
        data,
    )
    ver_after = run_in_box(box, op, [old_cli, "--version"], env)
    op.check(
        f"installed router CLI is upgraded to {new_ver} after update_agentcompass",
        ver_after.returncode == 0 and ver_after.stdout.strip() == new_ver,
        ver_after.stdout,
    )
    op.check(
        "inspect_installs shows matching versions after self-update",
        not data.get("after_mismatch")
        and data.get("after_display") == f"AgentCompass {new_ver} · 命令行 {new_ver}"
        and data.get("after_routers", {}).get("path") == new_ver,
        data,
    )
    cached_accounts_path = box.state / "public-data" / "sets" / ctx["fixture"].updated_version / "accounts.json"
    op.check(
        "agentcompass data --refresh ran during update_agentcompass and populated public-data cache from fixture",
        cached_accounts_path.is_file(),
        str(cached_accounts_path),
    )
    op.check("single-flight restart mutex and one-shot restart marker work", bool(data.get("mutex_ok")) and bool(data.get("marker_ok")), data)


# ---------------------------------------------------------------------------------------------------------------------
# F13: Product layout without the dev checkout


def verify_f13_product_layout(ctx: dict):
    run: sc.Run = ctx["run"]
    box = make_profile(ctx, "f13-product-layout")
    op = run.operation(
        "F13 product layout without the dev checkout",
        "In a foreign profile, git-cloned plugin roots resolve directly to ~/.agent-compass/plugins/<id>, EasyTier "
        "resolves to %ProgramData%/AgentCompass/easytier, and the wheel's grok-bot CLI and Paseo bridge dependencies "
        "go under the state directory, not site-packages.",
    )
    probe = HERE / "f13_product_layout_probe.py"
    work = box.root / "work"
    work.mkdir(parents=True, exist_ok=True)
    out = box.root / "f13.json"
    env = base_cli_env(box, ctx, include_clis=(), extra={
        "ProgramData": box.root / "ProgramData", "ALLUSERSPROFILE": box.root / "ProgramData",
        "AGENTCOMPASS_EASYTIER_HOME": None, "AGENTCOMPASS_GROK_BOT_RUNTIME": None,
    })
    res = run_in_box(box, op, [sc.TRAY_PYTHON, "-X", "utf8", probe, ctx["repo"] / "tray", box.scripts,
                               box.data / "setup", work, out], env)
    op.check("F13 probe exits 0", res.returncode == 0 and out.is_file(), res.stderr[-1200:])
    results = json.loads(out.read_text(encoding="utf-8")) if out.is_file() else {}
    for name, outcome in results.items():
        op.check(name, outcome.get("ok"), outcome.get("detail"))
    op.check("the probe ran every F13 check", len(results) >= 6, sorted(results))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wheel", type=Path, help="Install this wheel instead of building one from the working tree.")
    parser.add_argument("--report", type=Path, default=ROOT / "foreign-machine-report")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--only", nargs="+", metavar="FXX", help="Run only these features (F09 always runs), e.g. --only F10.")
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
    base = Path(tempfile.mkdtemp(prefix="agentcompass-foreign-")).resolve()
    notes = {"sandbox": str(base)}
    try:
        if args.wheel:
            wheel, built = args.wheel.resolve(), False
            repo = sc.build_fixture_repo(base, minimal=True)
        else:
            repo = sc.build_fixture_repo(base)
            wheel, built = sc.build_wheel(repo, base / "dist"), True
        uf_repo = build_usage_float_fixture_repo(base)
        notes["wheel"] = str(wheel)
        notes["usage_float_repo"] = str(uf_repo)
        venv = base / "venv"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True)
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run([str(python), "-m", "pip", "install", "--no-index", "--no-deps", "--quiet", str(wheel)], check=True, capture_output=True)
        sites = (venv / "Lib" / "site-packages") if os.name == "nt" else next((venv / "lib").glob("python*/site-packages"))
        sc.NETGUARD = base / "netguard"
        sc.NETGUARD.mkdir()
        bin_dir = zd.build_fake_clis(base)
        shipped_refs = sites / "agentcompass" / "data" / "skills" / "agent-compass" / "references"
        with ForeignFixtureServer(shipped_refs) as fixture:
            write_foreign_netguard([sites, sc.NETGUARD], fixture.url, [fixture.port], base / "net-foreign.jsonl")
            ctx = {
                "base": base, "venv": venv, "fixture": fixture, "run": run,
                "wheel": wheel, "repo": repo, "uf_repo": uf_repo,
                "bin_dir": bin_dir, "fixture_built": built,
            }
            wanted = lambda fid: not args.only or fid in args.only
            if wanted("F01"):
                verify_f01_public_data_and_installed_copy(ctx)
            if wanted("F02"):
                verify_f02_fleet_parity(ctx)
            if wanted("F03"):
                verify_f03_classifier_cascade(ctx)
            if wanted("F04"):
                verify_f04_quota_gate_scope_and_twins(ctx)
            if wanted("F05"):
                verify_f05_antigravity_plan_tier(ctx)
            if wanted("F06") or wanted("F08"):
                magpie_box = verify_f06_magpie_pools_and_refresh(ctx)
            if wanted("F07"):
                verify_f07_stranger_topologies(ctx)
            if wanted("F08"):
                verify_f08_tray_cards_and_usage_float(ctx, magpie_box)
            if wanted("F10"):
                verify_f10_evidence_calibrated_routing(ctx)
            if wanted("F11"):
                verify_f11_omp_oauth_subscriptions(ctx)
            if wanted("F12"):
                verify_f12_self_update_and_version_display(ctx)
            if wanted("F13"):
                verify_f13_product_layout(ctx)

        op = run.operation("F09 real machine untouched", "the user's own cache, activity and receipt files are unpolluted by the foreign-machine harness")
        after = sc.real_state_snapshot()
        changed = sorted(k for k in before if before[k] != after[k])
        polluted = sc.real_files_polluted(before.keys(), base)
        op.check("the router's own real files are unpolluted and unchanged unless a real tray/Usage Float refreshed them",
                 not polluted and (not changed or bool(sc.usage_float_processes())),
                 {"changed": changed, "polluted": polluted})
        op_proc = run.operation("no process left behind", "nothing started from the foreign-machine sandbox is still running")
        left = sc.sandbox_processes(base)
        op_proc.check("no process still runs from the foreign-machine sandbox directory", not left, left)
        for entry in left:
            subprocess.run(["taskkill", "/F", "/T", "/PID", entry.split(":")[0]], capture_output=True)
    finally:
        sc.write_reports(run, started, notes)
        (args.report / "latest.txt").write_text(run_dir.name + "\n", encoding="utf-8")
        if not args.keep:
            shutil.rmtree(base, ignore_errors=True)
    print(f"foreign machine check: {run.failures} failed assertion(s); report {run_dir / 'report.md'}")
    return run.failures


if __name__ == "__main__":
    raise SystemExit(main())
