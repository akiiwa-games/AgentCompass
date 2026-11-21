"""Periodic tray pass that recovers dead Paseo dispatch watchers and due end notices."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import psutil

import hermetic_run
import services
from tk_test_support import require_windows

require_windows("psutil")

import dispatch_registry as registry


FAKE_SDK_CLIENT = r"""
const fs = require('node:fs');
const path = require('node:path');

function statePath() {
  return path.join(process.env.ROUTER_STATE_DIR, 'fake-paseo-host.json');
}

function readState() {
  try {
    return JSON.parse(fs.readFileSync(statePath(), 'utf8'));
  } catch {
    return {};
  }
}

function appendJsonl(name, entry) {
  const target = path.join(process.env.ROUTER_STATE_DIR, name);
  fs.appendFileSync(target, JSON.stringify(entry) + '\n');
}

function deliveredIds() {
  const target = path.join(process.env.ROUTER_STATE_DIR, 'messages.jsonl');
  if (!fs.existsSync(target)) return new Set();
  const lines = fs.readFileSync(target, 'utf8').trim().split('\n').filter(Boolean);
  const ids = new Set();
  for (const line of lines) {
    try {
      const parsed = JSON.parse(line);
      if (parsed?.options?.messageId) ids.add(parsed.options.messageId);
    } catch {}
  }
  return ids;
}

exports.DaemonClient = class {
  isConnected = false;
  async connect() {
    this.isConnected = true;
    appendJsonl('connections.jsonl', {pid: process.pid, at: Date.now()});
  }
  async close() {
    this.isConnected = false;
  }
  async fetchAgent(id) {
    const state = readState();
    if (id === (state.parent_id || 'parent-1')) {
      return {agent: state.parent ?? {id, status: 'running'}};
    }
    const agents = state.agents || {};
    return {agent: Object.prototype.hasOwnProperty.call(agents, id) ? agents[id] : null};
  }
  async fetchAgents(query) {
    const state = readState();
    const wanted = query?.filter?.labels?.['router.dispatch_id'];
    const entries = [];
    for (const agent of Object.values(state.agents || {})) {
      if (!agent) continue;
      if (!wanted || agent?.labels?.['router.dispatch_id'] === wanted) {
        entries.push({id: agent.id, agent});
      }
    }
    return {entries};
  }
  async fetchAgentTimeline(agentId) {
    const state = readState();
    const entries = (state.timelines && state.timelines[agentId]) || [];
    return {entries, hasOlder: false};
  }
  async sendCorrelatedSessionRequest(request) {
    const state = readState();
    const key = request?.message?.idempotencyKey;
    if (request?.message?.type === 'creation.subscribe.request' && key) {
      const creations = state.creations || {};
      const entry = creations[key] ?? null;
      if (entry && typeof entry.error === 'string') return {error: entry.error};
      return {snapshot: entry};
    }
    return {snapshot: null};
  }
  async sendMessage(agent, text, options) {
    appendJsonl('raw_sends.jsonl', {pid: process.pid, agent, text, options, at: Date.now()});
    const msgId = options?.messageId;
    if (msgId && deliveredIds().has(msgId)) return;
    appendJsonl('messages.jsonl', {pid: process.pid, agent, text, options, at: Date.now()});
  }
};
"""


def write_fake_sdk(root: Path) -> Path:
    sdk = root / "fake_sdk" / "node_modules"
    client_dir = sdk / "@getpaseo" / "client"
    ws_dir = sdk / "ws"
    client_dir.mkdir(parents=True, exist_ok=True)
    ws_dir.mkdir(parents=True, exist_ok=True)
    (client_dir / "package.json").write_text(
        json.dumps({"name": "@getpaseo/client", "exports": {"./internal/daemon-client": "./client.cjs"}}),
        encoding="utf-8",
    )
    (client_dir / "client.cjs").write_text(FAKE_SDK_CLIENT, encoding="utf-8")
    (ws_dir / "index.js").write_text("exports.WebSocket = class {};\n", encoding="utf-8")
    return sdk


def seed_dispatch_row(db_path: Path, row: dict) -> None:
    with registry.transaction(db_path, upkeep=True) as conn:
        registry.save(conn, row)


def base_paseo_row(
    dispatch_id: str,
    *,
    state: str = "started",
    agent_id: str | None = "worker-1",
    parent_id: str = "parent-1",
    now: datetime | None = None,
    launched: bool = True,
    end_notice: dict | None = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    created = (now - timedelta(seconds=30)).isoformat()
    lease_until = (now + timedelta(seconds=600)).isoformat()
    hard_until = (now + timedelta(hours=24)).isoformat()
    labels = {
        "router.decision_id": f"dec-{dispatch_id}",
        "router.dispatch_id": dispatch_id,
        "router.parent_id": parent_id,
        "router.task_id": f"task-{dispatch_id}",
        "router.account": "codex",
        "router.bundle_id": "codex:gpt-6.1-sol:medium",
        "router.request_hash": "hash-1",
    }
    request = {
        "title": f"Task {dispatch_id}",
        "provider": "codex/gpt-6.1-sol",
        "initialPrompt": "Run task",
        "settings": {"modeId": "full-access", "thinkingOptionId": "medium"},
        "labels": labels,
        "notifyOnFinish": True,
    }
    row = {
        "dispatch_id": dispatch_id,
        "decision_id": f"dec-{dispatch_id}",
        "dispatch_mode": "paseo",
        "parent_id": parent_id,
        "task_id": f"task-{dispatch_id}",
        "task_hash": "thash",
        "request_hash": "hash-1",
        "request": request,
        "bundle": {
            "id": "codex:gpt-6.1-sol:medium",
            "bundle_id": "codex:gpt-6.1-sol:medium",
            "provider": "codex",
            "account": "codex",
            "model": "gpt-6.1-sol",
            "effort": "medium",
            "modeId": "full-access",
        },
        "expected_usd": 0.25,
        "usage": {},
        "created_at": created,
        "updated_at": created,
        "lease_until": lease_until,
        "hard_until": hard_until,
        "operation": "create",
        "agent_id": agent_id,
        "state": state,
        "events": [{"state": state, "at": created}],
    }
    if launched:
        row["launched_at"] = created
        if state == "started":
            row["started_at"] = created
    if state in ("completed", "failed", "canceled", "create_failed"):
        row["released_at"] = now.isoformat()
        row["lease_until"] = now.isoformat()
    if end_notice is not None:
        row["end_notice"] = end_notice
    return row


class DispatchNoticeUnitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_dir = self.root / ".agent-compass"
        self.paseo_home = self.root / ".paseo"
        self.db_path = self.state_dir / "dispatch.sqlite3"
        env_patch = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.root),
                "USERPROFILE": str(self.root),
                "ROUTER_STATE_DIR": str(self.state_dir),
                "ROUTER_DISPATCH_DB": str(self.db_path),
                "PASEO_HOME": str(self.paseo_home),
            },
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)
        state_patch = mock.patch.object(services, "STATE_DIR", self.state_dir)
        state_patch.start()
        self.addCleanup(state_patch.stop)
        self.addCleanup(services._dispatch_notice.update, proc=None, started=None)
        services._dispatch_notice.update(proc=None, started=None)

    def child(self, running: bool):
        proc = mock.Mock(pid=5150)
        proc.poll.return_value = None if running else 0
        return proc

    def configure_paseo_daemon(self) -> None:
        self.paseo_home.mkdir(parents=True, exist_ok=True)
        (self.paseo_home / "config.json").write_text(
            json.dumps({"daemon": {"listen": "127.0.0.1:6767"}}),
            encoding="utf-8",
        )

    def write_receipt(self, dispatch_id: str, status: str = "watching", pid: int = 999999) -> Path:
        notif_dir = self.state_dir / "paseo-notifications"
        notif_dir.mkdir(parents=True, exist_ok=True)
        path = notif_dir / f"{dispatch_id}.json"
        now_ms = int(time.time() * 1000)
        path.write_text(
            json.dumps(
                {
                    "dispatch_id": dispatch_id,
                    "parent_id": "parent-1",
                    "agent_id": "worker-1",
                    "pid": pid,
                    "status": status,
                    "delivered": ["terminal"] if status == "delivered" else [],
                    "pending": None,
                    "created_at": now_ms - 5000,
                    "deadline": now_ms + 3600_000,
                    "creation_deadline": now_ms + 240_000,
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_idle_registry_starts_nothing(self):
        self.assertLessEqual(services.DISPATCH_NOTICE_INTERVAL, 60)
        self.assertFalse(services.dispatch_notice_needed())
        with mock.patch.object(services, "hidden_popen") as popen:
            self.assertFalse(services.dispatch_notice_tick(force=True))
            popen.assert_not_called()

        now = datetime.now(timezone.utc)
        # Settled rows, unlaunched prepared row, expired-past-hard-limit row, non-Paseo claim,
        # and due notice without daemon config or without receipt file must all stay idle.
        seed_dispatch_row(
            self.db_path,
            base_paseo_row(
                "d-done",
                state="completed",
                now=now,
                end_notice={"state": "sent", "source": "watcher", "message_id": "m1"},
            ),
        )
        seed_dispatch_row(self.db_path, base_paseo_row("d-prep", state="prepared", now=now, launched=False))
        expired_old = base_paseo_row("d-old", state="expired", now=now)
        expired_old["hard_until"] = (now - timedelta(seconds=5)).isoformat()
        seed_dispatch_row(self.db_path, expired_old)
        herdr_claim = base_paseo_row("d-herdr", state="launched", now=now)
        herdr_claim["dispatch_mode"] = "herdr"
        seed_dispatch_row(self.db_path, herdr_claim)
        seed_dispatch_row(
            self.db_path,
            base_paseo_row(
                "d-due-no-daemon",
                state="completed",
                now=now,
                end_notice={"state": "due", "message_id": "m2"},
            ),
        )
        self.write_receipt("d-due-no-daemon", status="delivery_failed")

        self.assertFalse(services.dispatch_notice_needed())
        with mock.patch.object(services, "hidden_popen") as popen:
            self.assertFalse(services.dispatch_notice_tick(force=True))
            popen.assert_not_called()

        # Even with daemon configured, a due row whose receipt file is missing cannot be sent by notify_end.
        (self.state_dir / "paseo-notifications" / "d-due-no-daemon.json").unlink()
        self.configure_paseo_daemon()
        self.assertFalse(services.dispatch_notice_needed())

    def test_open_and_due_rows_each_require_a_pass(self):
        now = datetime.now(timezone.utc)
        for state in ("launching", "launched", "started"):
            with self.subTest(state=state):
                db = self.root / f"open-{state}.sqlite3"
                with mock.patch.dict(os.environ, {"ROUTER_DISPATCH_DB": str(db)}):
                    seed_dispatch_row(db, base_paseo_row(f"d-{state}", state=state, now=now))
                    self.assertTrue(services.dispatch_notice_needed())

        db_exp = self.root / "expired-live.sqlite3"
        with mock.patch.dict(os.environ, {"ROUTER_DISPATCH_DB": str(db_exp)}):
            seed_dispatch_row(db_exp, base_paseo_row("d-exp", state="expired", now=now))
            self.assertTrue(services.dispatch_notice_needed())

        db_fb = self.root / "fallback.sqlite3"
        with mock.patch.dict(os.environ, {"ROUTER_DISPATCH_DB": str(db_fb)}):
            fb_row = base_paseo_row("d-fb", state="failed", now=now)
            fb_row["fallback"] = {"state": "retrying", "until": (now + timedelta(seconds=60)).isoformat()}
            seed_dispatch_row(db_fb, fb_row)
            self.assertTrue(services.dispatch_notice_needed())

        db_due = self.root / "due.sqlite3"
        self.configure_paseo_daemon()
        self.write_receipt("d-due", status="delivery_failed")
        with mock.patch.dict(os.environ, {"ROUTER_DISPATCH_DB": str(db_due)}):
            seed_dispatch_row(
                db_due,
                base_paseo_row(
                    "d-due",
                    state="completed",
                    now=now,
                    end_notice={"state": "due", "message_id": "m-due"},
                ),
            )
            self.assertTrue(services.dispatch_notice_needed())

    def test_frozen_tray_invokes_bundled_cli_executable_never_windowed_tray(self):
        install_root = self.root / "install"
        tray_exe = install_root / "AgentCompass.exe"
        cli_exe = install_root / "cli" / "agentcompass.exe"
        cli_exe.parent.mkdir(parents=True, exist_ok=True)
        tray_exe.write_bytes(b"tray")
        cli_exe.write_bytes(b"cli")
        seed_dispatch_row(self.db_path, base_paseo_row("d-frozen", state="started"))
        with mock.patch.object(services.component_launch, "frozen", return_value=True), \
                mock.patch.object(services.sys, "executable", str(tray_exe)), \
                mock.patch.object(services, "hidden_popen", return_value=self.child(True)) as popen:
            self.assertTrue(services.dispatch_notice_tick())
        cmd, _env, _cwd, log, flags = popen.call_args.args
        self.assertEqual(cmd, [str(cli_exe.resolve()), "dispatch", "--dispatch-action", "reconcile"])
        self.assertNotEqual(Path(cmd[0]).resolve(), tray_exe.resolve())
        self.assertEqual(log.name, "dispatch-notice.log")
        self.assertEqual(flags, services.BELOW_NORMAL_PRIORITY_CLASS)

    def test_interval_single_flight_and_non_tree_stop(self):
        seed_dispatch_row(self.db_path, base_paseo_row("d-flight", state="started"))
        with mock.patch.object(services, "hidden_popen", return_value=self.child(True)) as popen:
            self.assertTrue(services.dispatch_notice_tick())
            self.assertFalse(services.dispatch_notice_tick(force=True))
            self.assertEqual(popen.call_count, 1)

        services._dispatch_notice.update(proc=None, started=None)
        with mock.patch.object(services, "hidden_popen", return_value=self.child(False)) as popen:
            self.assertTrue(services.dispatch_notice_tick())
            self.assertFalse(services.dispatch_notice_tick())
            self.assertTrue(services.dispatch_notice_tick(force=True))
            services._dispatch_notice["started"] -= services.DISPATCH_NOTICE_INTERVAL + 1
            self.assertTrue(services.dispatch_notice_tick())
            self.assertEqual(popen.call_count, 3)

        # Stopping the pass terminates only the pass process, never taskkill /T (which would kill a watcher it spawned).
        with mock.patch.object(services, "kill_pid") as kill:
            services._dispatch_notice["proc"] = None
            services.stop_dispatch_notice()
            done_proc = self.child(False)
            services._dispatch_notice["proc"] = done_proc
            services.stop_dispatch_notice()
            done_proc.terminate.assert_not_called()

            live_proc = self.child(True)
            services._dispatch_notice["proc"] = live_proc
            services.stop_dispatch_notice()
            live_proc.terminate.assert_called_once_with()
            kill.assert_not_called()


class DispatchNoticeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_dir = self.root / ".agent-compass"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.paseo_home = self.root / ".paseo"
        self.paseo_home.mkdir(parents=True, exist_ok=True)
        (self.paseo_home / "config.json").write_text(
            json.dumps({"daemon": {"listen": "127.0.0.1:6767"}}),
            encoding="utf-8",
        )
        (self.paseo_home / "local-credential").write_text("fixture-token\n", encoding="utf-8")
        self.db_path = self.state_dir / "dispatch.sqlite3"
        self.sdk_root = write_fake_sdk(self.root)
        env_patch = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.root),
                "USERPROFILE": str(self.root),
                "ROUTER_STATE_DIR": str(self.state_dir),
                "ROUTER_DISPATCH_DB": str(self.db_path),
                "PASEO_HOME": str(self.paseo_home),
                "ROUTER_PASEO_SDK_ROOT": str(self.sdk_root),
                "AGENTCOMPASS_ROLL_FALLBACK": "off",
                "ROUTER_PROVIDER_RETRY": "off",
            },
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)
        state_patch = mock.patch.object(services, "STATE_DIR", self.state_dir)
        state_patch.start()
        self.addCleanup(state_patch.stop)
        self.addCleanup(self._cleanup_processes)
        services._dispatch_notice.update(proc=None, started=None)

    def _cleanup_processes(self) -> None:
        proc = services._dispatch_notice.get("proc")
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        services._dispatch_notice.update(proc=None, started=None)
        notif_dir = self.state_dir / "paseo-notifications"
        if notif_dir.is_dir():
            for item in notif_dir.glob("*.json"):
                try:
                    pid = json.loads(item.read_text(encoding="utf-8")).get("pid")
                except (OSError, ValueError):
                    continue
                if isinstance(pid, int) and pid > 0 and psutil.pid_exists(pid):
                    try:
                        cmdline = " ".join(psutil.Process(pid).cmdline())
                    except psutil.Error:
                        continue
                    if "paseo_dispatch_bridge.mjs" in cmdline and "--watch" in cmdline:
                        hermetic_run._started.add(pid)
                        try:
                            psutil.Process(pid).terminate()
                            psutil.Process(pid).wait(timeout=5)
                        except psutil.Error:
                            pass

    def write_host_state(self, state: dict) -> None:
        target = self.state_dir / "fake-paseo-host.json"
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(target)

    def write_dead_watcher_receipt(
        self,
        dispatch_id: str,
        *,
        agent_id: str | None = "worker-1",
        parent_id: str = "parent-1",
        status: str = "watching",
    ) -> Path:
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait(timeout=5)
        dead_pid = dead.pid
        notif_dir = self.state_dir / "paseo-notifications"
        notif_dir.mkdir(parents=True, exist_ok=True)
        receipt_path = notif_dir / f"{dispatch_id}.json"
        lock_path = notif_dir / f"{dispatch_id}.json.lock"
        now_ms = int(time.time() * 1000)
        record = {
            "dispatch_id": dispatch_id,
            "parent_id": parent_id,
            **({"agent_id": agent_id} if agent_id else {}),
            "pid": dead_pid,
            "status": status,
            "delivered": [],
            "pending": None,
            "created_at": now_ms - 10_000,
            "deadline": now_ms + 3600_000,
            "creation_deadline": now_ms + 240_000,
        }
        receipt_path.write_text(json.dumps(record), encoding="utf-8")
        # Leave the stale lock from the killed watcher to verify recovery reclaims it cleanly.
        if status == "watching":
            lock_path.write_text(str(dead_pid), encoding="utf-8")
        return receipt_path

    def read_jsonl(self, name: str) -> list[dict]:
        path = self.state_dir / name
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def wait_until(self, predicate, timeout: float = 20.0, step: float = 0.1):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(step)
        self.fail("condition was not met before deadline")

    def run_pass_and_wait(self, *, force: bool = False) -> subprocess.Popen:
        started = services.dispatch_notice_tick(force=force)
        self.assertTrue(started, "expected dispatch_notice_tick to start a reconcile pass")
        proc = services._dispatch_notice["proc"]
        self.assertIsNotNone(proc)
        code = proc.wait(timeout=30)
        self.assertEqual(
            code,
            0,
            f"reconcile pass exited with {code}: "
            f"{(self.state_dir / 'dispatch-notice.log').read_text(encoding='utf-8', errors='replace') if (self.state_dir / 'dispatch-notice.log').is_file() else ''}",
        )
        return proc

    def test_idle_pass_spawns_no_bridge_and_makes_no_daemon_connection(self):
        seed_dispatch_row(
            self.db_path,
            base_paseo_row(
                "d-settled",
                state="completed",
                end_notice={"state": "sent", "source": "watcher", "message_id": "m-settled"},
            ),
        )
        self.assertFalse(services.dispatch_notice_tick(force=True))
        self.assertEqual(self.read_jsonl("connections.jsonl"), [])
        self.assertFalse((self.state_dir / "dispatch-notice.log").exists())

    def test_dead_watcher_is_restarted_outlives_pass_delivers_permission_then_finish(self):
        now = datetime.now(timezone.utc)
        row = base_paseo_row("d-live", state="started", agent_id="w-live", now=now)
        seed_dispatch_row(self.db_path, row)
        receipt_path = self.write_dead_watcher_receipt("d-live", agent_id="w-live")
        dead_pid = json.loads(receipt_path.read_text(encoding="utf-8"))["pid"]

        agent_doc = {
            "id": "w-live",
            "status": "running",
            "provider": "codex",
            "model": "gpt-6.1-sol",
            "currentModeId": "full-access",
            "effectiveThinkingOptionId": "medium",
            "labels": row["request"]["labels"],
            "updatedAt": now.isoformat(),
        }
        self.write_host_state({"parent_id": "parent-1", "agents": {"w-live": agent_doc}})

        # 1. Pass runs reconcile, restarts the dead watcher, and exits; the watcher must outlive the pass command.
        self.run_pass_and_wait()
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        watcher_pid = receipt["pid"]
        self.assertNotEqual(watcher_pid, dead_pid)
        self.assertEqual(receipt["status"], "watching")
        time.sleep(0.3)
        self.assertTrue(psutil.pid_exists(watcher_pid), "restarted watcher died when the pass command exited")

        # 2. While the watcher is alive, another pass keeps the same watcher PID without starting a duplicate.
        self.run_pass_and_wait(force=True)
        receipt_after = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt_after["pid"], watcher_pid)
        self.assertTrue(psutil.pid_exists(watcher_pid))

        # 3. Worker pauses on a permission request -> restarted watcher delivers the permission notice and keeps watching.
        perm_agent = dict(
            agent_doc,
            status="idle",
            attentionReason="permission",
            pendingPermissions=[{"id": "perm-42"}],
            updatedAt=datetime.now(timezone.utc).isoformat(),
        )
        self.write_host_state({"parent_id": "parent-1", "agents": {"w-live": perm_agent}})
        perm_msgs = self.wait_until(lambda: [m for m in self.read_jsonl("messages.jsonl") if "State: needs permission" in m["text"]])
        self.assertEqual(len(perm_msgs), 1)
        self.assertTrue(psutil.pid_exists(watcher_pid))

        # 4. Worker finishes -> watcher sends the single completion notice with final reply, settles registry, and exits.
        done_at = datetime.now(timezone.utc).isoformat()
        done_agent = dict(
            agent_doc,
            status="idle",
            attentionReason="finished",
            pendingPermissions=[],
            updatedAt=done_at,
        )
        self.write_host_state(
            {
                "parent_id": "parent-1",
                "agents": {"w-live": done_agent},
                "timelines": {
                    "w-live": [
                        {"timestamp": now.isoformat(), "item": {"type": "user_message", "text": "Run task"}},
                        {"timestamp": done_at, "item": {"type": "assistant_message", "text": "All checks green."}},
                    ]
                },
            }
        )
        finish_msgs = self.wait_until(lambda: [m for m in self.read_jsonl("messages.jsonl") if "State: finished" in m["text"]])
        self.assertEqual(len(finish_msgs), 1)
        self.assertIn("All checks green.", finish_msgs[0]["text"])
        self.wait_until(lambda: registry.inspect("d-live", self.db_path)["state"] == "completed")
        settled = registry.inspect("d-live", self.db_path)
        self.assertEqual((settled.get("end_notice") or {}).get("state"), "sent")
        self.wait_until(lambda: not psutil.pid_exists(watcher_pid))
        self.assertFalse(services.dispatch_notice_needed())

    def test_worker_already_failed_or_removed_while_watcher_was_dead_notifies_once(self):
        now = datetime.now(timezone.utc)
        # Case A: worker already failed while watcher was dead.
        fail_row = base_paseo_row("d-fail", state="started", agent_id="w-fail", now=now)
        seed_dispatch_row(self.db_path, fail_row)
        self.write_dead_watcher_receipt("d-fail", agent_id="w-fail")
        failed_agent = {
            "id": "w-fail",
            "status": "error",
            "attentionReason": "error",
            "lastError": "provider exploded",
            "provider": "codex",
            "model": "gpt-6.1-sol",
            "labels": fail_row["request"]["labels"],
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }
        # Case B: worker created then removed before observation while watcher was dead.
        rem_row = base_paseo_row("d-rem", state="launching", agent_id=None, now=now)
        seed_dispatch_row(self.db_path, rem_row)
        self.write_dead_watcher_receipt("d-rem", agent_id=None)
        # Case C: started worker removed while watcher was dead.
        gone_row = base_paseo_row("d-gone", state="started", agent_id="w-gone", now=now)
        seed_dispatch_row(self.db_path, gone_row)
        self.write_dead_watcher_receipt("d-gone", agent_id="w-gone")

        self.write_host_state(
            {
                "parent_id": "parent-1",
                "agents": {"w-fail": failed_agent},
                "creations": {"d-rem": {"error": "Previously created agent no longer exists"}},
                "timelines": {},
            }
        )

        self.run_pass_and_wait(force=True)
        self.wait_until(
            lambda: (
                any("Dispatch: d-fail" in m["text"] and "State: error" in m["text"] for m in self.read_jsonl("messages.jsonl"))
                and any(
                    "Dispatch: d-rem" in m["text"] and "removed before" in m["text"]
                    for m in self.read_jsonl("messages.jsonl")
                )
                and any(
                    "Dispatch: d-gone" in m["text"] and "State: worker unavailable" in m["text"]
                    for m in self.read_jsonl("messages.jsonl")
                )
            )
        )
        # Wait for any restarted short-lived watchers to finish settling and exit.
        self.wait_until(
            lambda: (
                registry.inspect("d-fail", self.db_path)["state"] == "failed"
                and (registry.inspect("d-fail", self.db_path).get("end_notice") or {}).get("state") == "sent"
                and registry.inspect("d-rem", self.db_path)["state"] == "canceled"
                and (registry.inspect("d-rem", self.db_path).get("end_notice") or {}).get("state") == "sent"
                and registry.inspect("d-gone", self.db_path)["state"] == "canceled"
                and (registry.inspect("d-gone", self.db_path).get("end_notice") or {}).get("state") == "sent"
                and all(
                    not psutil.pid_exists(
                        json.loads((self.state_dir / "paseo-notifications" / f"{did}.json").read_text(encoding="utf-8")).get("pid", -1)
                    )
                    for did in ("d-fail", "d-rem", "d-gone")
                )
            )
        )
        for did in ("d-fail", "d-rem", "d-gone"):
            msgs = [m for m in self.read_jsonl("messages.jsonl") if f"Dispatch: {did}" in m["text"]]
            self.assertEqual(len(msgs), 1)
            expected_id = hashlib.sha256(f"router-finish:{did}:terminal".encode()).hexdigest()
            raw = [m["options"]["messageId"] for m in self.read_jsonl("raw_sends.jsonl") if f"Dispatch: {did}" in m["text"]]
            self.assertTrue(raw)
            self.assertEqual(set(raw), {expected_id})

        self.assertFalse(services.dispatch_notice_needed())
        raw_before = len(self.read_jsonl("raw_sends.jsonl"))
        self.assertFalse(services.dispatch_notice_tick(force=True))
        self.assertEqual(len(self.read_jsonl("raw_sends.jsonl")), raw_before)

    def test_due_end_notice_with_dead_watcher_is_delivered_once_and_matches_watcher_message_id(self):
        now = datetime.now(timezone.utc)
        expected_id = hashlib.sha256(b"router-finish:d-due:terminal").hexdigest()
        due_row = base_paseo_row(
            "d-due",
            state="completed",
            agent_id="w-due",
            now=now,
            end_notice={"state": "due", "message_id": expected_id},
        )
        seed_dispatch_row(self.db_path, due_row)
        self.write_dead_watcher_receipt("d-due", agent_id="w-due", status="delivery_failed")
        self.write_host_state(
            {
                "parent_id": "parent-1",
                "agents": {
                    "w-due": {
                        "id": "w-due",
                        "status": "idle",
                        "attentionReason": "finished",
                        "provider": "codex",
                        "model": "gpt-6.1-sol",
                        "labels": due_row["request"]["labels"],
                    }
                },
                "timelines": {
                    "w-due": [
                        {"timestamp": now.isoformat(), "item": {"type": "assistant_message", "text": "Recovered catch-up report."}}
                    ]
                },
            }
        )

        self.run_pass_and_wait(force=True)
        msgs = self.read_jsonl("messages.jsonl")
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["options"]["messageId"], expected_id)
        self.assertIn("Recovered catch-up report.", msgs[0]["text"])
        settled = registry.inspect("d-due", self.db_path)
        self.assertEqual((settled.get("end_notice") or {}).get("state"), "sent")
        self.assertFalse(services.dispatch_notice_needed())
        self.assertFalse(services.dispatch_notice_tick(force=True))
        self.assertEqual(len(self.read_jsonl("raw_sends.jsonl")), 1)

    def test_race_with_live_watcher_shares_deterministic_message_id_and_delivers_once(self):
        now = datetime.now(timezone.utc)
        expected_id = hashlib.sha256(b"router-finish:d-race:terminal").hexdigest()
        row = base_paseo_row("d-race", state="started", agent_id="w-race", now=now)
        seed_dispatch_row(self.db_path, row)
        receipt_path = self.write_dead_watcher_receipt("d-race", agent_id="w-race")
        running_agent = {
            "id": "w-race",
            "status": "running",
            "provider": "codex",
            "model": "gpt-6.1-sol",
            "currentModeId": "full-access",
            "effectiveThinkingOptionId": "medium",
            "labels": row["request"]["labels"],
            "updatedAt": now.isoformat(),
        }
        self.write_host_state({"parent_id": "parent-1", "agents": {"w-race": running_agent}})

        # Start a live watcher via the tray pass and confirm it stays alive.
        self.run_pass_and_wait(force=True)
        watcher_pid = json.loads(receipt_path.read_text(encoding="utf-8"))["pid"]
        self.assertTrue(psutil.pid_exists(watcher_pid))

        # Flip worker to finished while immediately forcing a reconcile pass to race the live watcher.
        done_at = datetime.now(timezone.utc).isoformat()
        self.write_host_state(
            {
                "parent_id": "parent-1",
                "agents": {
                    "w-race": dict(
                        running_agent,
                        status="idle",
                        attentionReason="finished",
                        updatedAt=done_at,
                    )
                },
                "timelines": {
                    "w-race": [
                        {"timestamp": now.isoformat(), "item": {"type": "user_message", "text": "Run task"}},
                        {"timestamp": done_at, "item": {"type": "assistant_message", "text": "Race reply."}},
                    ]
                },
            }
        )
        self.run_pass_and_wait(force=True)
        self.wait_until(
            lambda: (
                registry.inspect("d-race", self.db_path)["state"] == "completed"
                and (registry.inspect("d-race", self.db_path).get("end_notice") or {}).get("state") == "sent"
                and not psutil.pid_exists(watcher_pid)
            )
        )

        msgs = [m for m in self.read_jsonl("messages.jsonl") if "Dispatch: d-race" in m["text"]]
        self.assertEqual(len(msgs), 1)
        self.assertIn("Race reply.", msgs[0]["text"])
        raw = [m["options"]["messageId"] for m in self.read_jsonl("raw_sends.jsonl") if "Dispatch: d-race" in m["text"]]
        self.assertTrue(raw)
        self.assertEqual(set(raw), {expected_id})
        raw_count = len(raw)
        self.assertFalse(services.dispatch_notice_tick(force=True))
        self.assertEqual(
            len([m for m in self.read_jsonl("raw_sends.jsonl") if "Dispatch: d-race" in m["text"]]),
            raw_count,
        )


if __name__ == "__main__":
    unittest.main()
