"""Periodic tray pass that recovers dead Paseo dispatch watchers and due end notices."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
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

import allocator
import account_fleet
import dispatch_fallback
import dispatch_flow
import dispatch_registry as registry
import provider_inventory
import provider_retry
import usage_reading


def start_loopback_listener() -> tuple[socket.socket, int, threading.Event]:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    srv.settimeout(0.2)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def _accept_loop() -> None:
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
                conn.close()
            except OSError:
                if stop.is_set():
                    break

    threading.Thread(target=_accept_loop, daemon=True).start()
    return srv, port, stop


def bridge_done_statuses() -> set[str]:
    text = (services.ROOT / "setup" / "paseo_dispatch_bridge.mjs").read_text(encoding="utf-8")
    match = re.search(r"const DONE = new Set\(\[([^\]]+)\]\);", text)
    if not match:
        raise AssertionError("DONE set not found in paseo_dispatch_bridge.mjs")
    return set(re.findall(r"'([^']+)'", match.group(1)))


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

function writeState(state) {
  const target = statePath();
  const tmp = `${target}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(state));
  fs.renameSync(tmp, target);
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
    appendJsonl('connections.jsonl', {pid: process.pid, argv: process.argv, at: Date.now()});
  }
  async close() {
    this.isConnected = false;
  }
  async fetchAgent(id) {
    const state = readState();
    if (Array.isArray(state.delay_fetch_ids) && state.delay_fetch_ids.includes(id) && !process.argv.includes('--watch')) {
      appendJsonl('slow_bridge.jsonl', {pid: process.pid, id, at: Date.now()});
      await new Promise(resolve => setTimeout(resolve, state.delay_fetch_ms || 6000));
    }
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
  async listWorkspaces() {
    return {workspaces: []};
  }
  async createAgent(options) {
    const state = readState();
    const agents = state.agents || {};
    const nowIso = new Date().toISOString();
    const agent = {
      id: options.agentId,
      provider: options.provider,
      model: options.model,
      status: state.created_agent_status || 'running',
      currentModeId: options.modeId,
      effectiveThinkingOptionId: options.thinkingOptionId,
      labels: options.labels,
      updatedAt: nowIso,
      lastUserMessageAt: nowIso,
    };
    agents[agent.id] = agent;
    state.agents = agents;
    writeState(state);
    appendJsonl('created_agents.jsonl', {
      pid: process.pid,
      id: agent.id,
      provider: agent.provider,
      model: agent.model,
      at: Date.now(),
    });
    return agent;
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
        self._daemon_sock, self.daemon_port, self._daemon_stop = start_loopback_listener()
        self.addCleanup(self._stop_daemon_listener)
        self.configure_paseo_daemon()
        self.addCleanup(self.reset_notice_state)
        self.reset_notice_state()

    def _stop_daemon_listener(self) -> None:
        self._daemon_stop.set()
        try:
            self._daemon_sock.close()
        except OSError:
            pass

    def reset_notice_state(self) -> None:
        if hasattr(services, "_reset_dispatch_notice"):
            services._reset_dispatch_notice()
        else:
            services._dispatch_notice.update(proc=None, started=None)

    def child(self, running: bool):
        proc = mock.Mock(pid=5150)
        proc.poll.return_value = None if running else 0
        return proc

    def configure_paseo_daemon(self, port: int | None = None) -> None:
        listen_port = self.daemon_port if port is None else port
        self.paseo_home.mkdir(parents=True, exist_ok=True)
        (self.paseo_home / "config.json").write_text(
            json.dumps({"daemon": {"listen": f"127.0.0.1:{listen_port}"}}),
            encoding="utf-8",
        )

    def write_receipt(
        self,
        dispatch_id: str,
        status: str = "watching",
        pid: int = 999999,
        *,
        record_dispatch_id: str | None = None,
        parent_id: object = "parent-1",
        fallback: dict | None = None,
        pending: dict | None = None,
        mtime_age_s: float = 0.0,
    ) -> Path:
        notif_dir = self.state_dir / "paseo-notifications"
        notif_dir.mkdir(parents=True, exist_ok=True)
        path = notif_dir / f"{dispatch_id}.json"
        now_ms = int(time.time() * 1000)
        record = {
            "dispatch_id": dispatch_id if record_dispatch_id is None else record_dispatch_id,
            "parent_id": parent_id,
            "agent_id": "worker-1",
            "pid": pid,
            "status": status,
            "delivered": ["terminal"] if status == "delivered" else [],
            "pending": pending,
            "created_at": now_ms - 5000,
            "deadline": now_ms + 3600_000,
            "creation_deadline": now_ms + 240_000,
            **({"fallback": fallback} if fallback is not None else {}),
        }
        path.write_text(json.dumps(record), encoding="utf-8")
        if mtime_age_s:
            ts = time.time() - mtime_age_s
            os.utime(path, (ts, ts))
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
        (self.paseo_home / "config.json").unlink(missing_ok=True)
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
                did = f"d-{state}"
                row = base_paseo_row(did, state=state, now=now)
                if state == "launching":
                    row["launched_at"] = (now - timedelta(seconds=registry.CREATE_SEND_WINDOW + 5)).isoformat()
                self.write_receipt(did, status="watching", pid=999999)
                with mock.patch.dict(os.environ, {"ROUTER_DISPATCH_DB": str(db)}):
                    seed_dispatch_row(db, row)
                    self.assertTrue(services.dispatch_notice_needed(now=now))

        db_exp = self.root / "expired-live.sqlite3"
        self.write_receipt("d-exp", status="watching", pid=999999)
        with mock.patch.dict(os.environ, {"ROUTER_DISPATCH_DB": str(db_exp)}):
            seed_dispatch_row(db_exp, base_paseo_row("d-exp", state="expired", now=now))
            self.assertTrue(services.dispatch_notice_needed(now=now))

        db_fb = self.root / "fallback.sqlite3"
        self.write_receipt("d-fb", status="watching", pid=999999)
        with mock.patch.dict(os.environ, {"ROUTER_DISPATCH_DB": str(db_fb)}):
            fb_row = base_paseo_row("d-fb", state="failed", now=now)
            fb_row["observed_at"] = fb_row["launched_at"]
            seed_dispatch_row(db_fb, fb_row)
            self.assertEqual(registry.needs_reader(path=db_fb, now=now)["observe"], ["d-fb"])
            self.assertTrue(services.dispatch_notice_needed(now=now))

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
            self.assertTrue(services.dispatch_notice_needed(now=now))

    def test_three_waiting_resend_notices_start_zero_processes_across_ticks_until_next_at(self):
        now = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)
        self.configure_paseo_daemon()
        # Three due notices waiting on `RESEND_AFTER_SECONDS`: two at attempt 0 (15 min), one at attempt 1 (60 min).
        specs = (
            ("d-wait-1", now, 0),
            ("d-wait-2", now + timedelta(seconds=45), 0),
            ("d-wait-3", now, 1),
        )
        for did, sent_at, attempt in specs:
            seed_dispatch_row(
                self.db_path,
                base_paseo_row(
                    did,
                    state="completed",
                    agent_id=f"w-{did}",
                    now=now,
                    end_notice={"state": "due", "message_id": registry.end_notice_message_id(did)},
                ),
            )
            self.write_receipt(
                did,
                status="delivery_failed",
                pending={
                    "key": "terminal",
                    "text": f"Notice for {did}",
                    "saved_at": sent_at.isoformat(),
                    "sent_at": sent_at.isoformat(),
                    "unknown": True,
                    **({"attempt": attempt} if attempt else {}),
                },
            )

        initial = registry.needs_reader(path=self.db_path, now=now)
        self.assertFalse(initial["needed"])
        self.assertEqual(initial["due"], [])
        self.assertEqual(initial["observe"], [])
        next_at = registry.parse(initial["next_at"])
        self.assertEqual(next_at, now + timedelta(seconds=registry.RESEND_AFTER_SECONDS[0]))

        mono = [1000.0]
        wall = [now]
        with mock.patch.object(services.time, "monotonic", side_effect=lambda: mono[0]), \
                mock.patch.object(registry, "utcnow", side_effect=lambda: wall[0]), \
                mock.patch.object(services, "hidden_popen", return_value=self.child(False)) as popen:
            for offset_s in (0, 35, 120, 600, registry.RESEND_AFTER_SECONDS[0] - 1):
                wall[0] = now + timedelta(seconds=offset_s)
                mono[0] += services.DISPATCH_NOTICE_INTERVAL + 5
                self.assertFalse(services.dispatch_notice_needed(now=wall[0]))
                self.assertFalse(services.dispatch_notice_tick())
            popen.assert_not_called()

            # Once `next_at` arrives, the earliest waiting notice can move and the gate starts a pass.
            wall[0] = next_at
            mono[0] += services.DISPATCH_NOTICE_INTERVAL + 5
            self.assertTrue(services.dispatch_notice_needed(now=wall[0]))
            self.assertTrue(services.dispatch_notice_tick())
            popen.assert_called_once()

    def test_gate_delegates_fallback_chains_and_later_turns_to_needs_reader(self):
        now = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)
        self.configure_paseo_daemon()

        # 1. A chain waiting on a `scheduled` retry under an active `fallback_guard`: `needs_reader` holds the due
        #    end notice until `until`, while the tray gate checks liveness on `registry.current_attempt` (starting no
        #    pass when its `--watch` process is healthy, and starting a pass when that watcher died during the wait).
        bridge = str(services.ROOT / "setup" / "paseo_dispatch_bridge.mjs")
        db_sched = self.root / "sched.sqlite3"
        retry_at = (now + timedelta(seconds=180)).isoformat()
        guard_until = (now + timedelta(seconds=1800)).isoformat()
        root_row = base_paseo_row(
            "d-sched-root",
            state="failed",
            agent_id="w-sched-root",
            now=now - timedelta(seconds=60),
            end_notice={"state": "sent", "source": "relaunched", "message_id": registry.end_notice_message_id("d-sched-root")},
        )
        root_row["observed_at"] = root_row["launched_at"]
        root_row["fallback"] = {
            "state": "retrying",
            "current": "d-sched-2",
            "retry_of": "d-sched-2",
            "retry_at": retry_at,
            "until": guard_until,
            "attempts": [
                {"attempt": 1, "dispatch_id": "d-sched-root"},
                {"attempt": 2, "dispatch_id": "d-sched-2"},
            ],
            "handled": {
                "d-sched-root": {"action": "relaunched", "dispatch_id": "d-sched-2"},
                "d-sched-2": {"action": "scheduled", "retry_at": retry_at},
            },
        }
        sched_row = base_paseo_row(
            "d-sched-2",
            state="failed",
            agent_id="w-sched-2",
            now=now,
            end_notice={"state": "due", "message_id": registry.end_notice_message_id("d-sched-2")},
        )
        sched_row["attempt_of"] = "d-sched-root"
        sched_row["attempt"] = 2
        sched_row["observed_at"] = sched_row["launched_at"]
        seed_dispatch_row(db_sched, root_row)
        seed_dispatch_row(db_sched, sched_row)
        # Attempt 1's watcher already exited (`relaunched`); Attempt 2's receipt holds the scheduled retry.
        self.write_receipt("d-sched-root", status="relaunched", pid=1111)
        self.write_receipt(
            "d-sched-2",
            status="watching",
            pid=2222,
            fallback={"status": "scheduled", "at": (now - timedelta(seconds=90)).isoformat(), "retry_at": retry_at},
            mtime_age_s=90.0,
        )
        healthy_sched_proc = mock.Mock()
        healthy_sched_proc.is_running.return_value = True
        healthy_sched_proc.cmdline.return_value = ["node", bridge, "--watch", "d-sched-2", "parent-1", "w-sched-2", ""]
        dead_sched_proc = mock.Mock()
        dead_sched_proc.is_running.return_value = False
        with mock.patch.dict(os.environ, {"ROUTER_DISPATCH_DB": str(db_sched)}):
            self.assertFalse(registry.needs_reader(path=db_sched, now=now)["needed"])
            with mock.patch.object(registry, "current_attempt", wraps=registry.current_attempt) as cur_attempt, \
                    mock.patch.object(services.psutil, "Process", return_value=healthy_sched_proc):
                self.assertFalse(services.dispatch_notice_needed(now=now))
                cur_attempt.assert_called_once_with("d-sched-root", path=db_sched)
            # When the scheduled retry's watcher dies (e.g. tray restart during backoff), `needs_reader` still waits,
            # while the gate's liveness check on `current_attempt` triggers a pass.
            with mock.patch.object(services.psutil, "Process", return_value=dead_sched_proc):
                self.assertFalse(registry.needs_reader(path=db_sched, now=now)["needed"])
                self.assertTrue(services.dispatch_notice_needed(now=now))
            # A finished (`delivery_failed`) receipt cannot be restarted by `ensureNotification`, and `needs_reader`
            # holds the due notice until `guard_until`; past `guard_until`, `needs_reader` releases it for `notify_end`.
            self.write_receipt("d-sched-2", status="delivery_failed", pid=2222)
            self.assertFalse(services.dispatch_notice_needed(now=now))
            self.assertTrue(services.dispatch_notice_needed(now=registry.parse(guard_until)))

        # 2. A completed attempt whose worker has an in-flight `resume` of the same parent waits (`_later_turn` pending),
        #    while the new resume itself is inside `CREATE_SEND_WINDOW`.
        db_later = self.root / "later.sqlite3"
        first_row = base_paseo_row(
            "d-first",
            state="completed",
            agent_id="w-shared",
            now=now - timedelta(seconds=60),
            end_notice={"state": "due", "message_id": registry.end_notice_message_id("d-first")},
        )
        later_row = base_paseo_row("d-later", state="launching", agent_id="w-shared", now=now)
        later_row["operation"] = "resume"
        later_row["launched_at"] = (now - timedelta(seconds=5)).isoformat()
        seed_dispatch_row(db_later, first_row)
        seed_dispatch_row(db_later, later_row)
        self.write_receipt("d-first", status="delivery_failed")
        with mock.patch.dict(os.environ, {"ROUTER_DISPATCH_DB": str(db_later)}):
            self.assertFalse(registry.needs_reader(path=db_later, now=now)["needed"])
            self.assertFalse(services.dispatch_notice_needed(now=now))

        # 3. An unhandled covered failure settled by a non-reconcile reader (`_awaits_fallback` -> `needs_reader['observe']`)
        #    starts a pass even without `end_notice` when its watcher receipt is restartable, and does not when its
        #    receipt is missing or its `--watch` process is already healthy.
        db_await = self.root / "await.sqlite3"
        await_row = base_paseo_row("d-await", state="failed", agent_id="w-await", now=now)
        await_row["observed_at"] = await_row["launched_at"]
        seed_dispatch_row(db_await, await_row)
        bridge = str(services.ROOT / "setup" / "paseo_dispatch_bridge.mjs")
        live_proc = mock.Mock()
        live_proc.is_running.return_value = True
        live_proc.cmdline.return_value = ["node", bridge, "--watch", "d-await", "parent-1", "w-await", ""]
        dead_proc = mock.Mock()
        dead_proc.is_running.return_value = False
        with mock.patch.dict(os.environ, {"ROUTER_DISPATCH_DB": str(db_await)}):
            self.assertEqual(registry.needs_reader(path=db_await, now=now)["observe"], ["d-await"])
            self.assertFalse(services.dispatch_notice_needed(now=now))
            self.write_receipt("d-await", status="watching", pid=4242)
            with mock.patch.object(services.psutil, "Process", return_value=dead_proc):
                self.assertTrue(services.dispatch_notice_needed(now=now))
            with mock.patch.object(services.psutil, "Process", return_value=live_proc):
                self.assertFalse(services.dispatch_notice_needed(now=now))

    def test_due_notice_from_needs_reader_without_paseo_daemon_starts_no_pass_across_ticks(self):
        now = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)
        seed_dispatch_row(
            self.db_path,
            base_paseo_row(
                "d-nodaemon",
                state="completed",
                now=now,
                end_notice={"state": "due", "message_id": registry.end_notice_message_id("d-nodaemon")},
            ),
        )
        self.write_receipt("d-nodaemon", status="delivery_failed")
        self.assertEqual(registry.needs_reader(path=self.db_path, now=now)["due"], ["d-nodaemon"])

        mono = [1000.0]
        for cfg in (None, {}):
            if cfg is None:
                (self.paseo_home / "config.json").unlink(missing_ok=True)
            else:
                self.paseo_home.mkdir(parents=True, exist_ok=True)
                (self.paseo_home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
            with mock.patch.object(services.time, "monotonic", side_effect=lambda: mono[0]), \
                    mock.patch.object(services, "hidden_popen", return_value=self.child(False)) as popen:
                for _ in range(4):
                    mono[0] += services.DISPATCH_NOTICE_INTERVAL + 5
                    self.assertFalse(services.dispatch_notice_needed(now=now))
                    self.assertFalse(services.dispatch_notice_tick())
                popen.assert_not_called()

        self.configure_paseo_daemon()
        with mock.patch.object(services, "hidden_popen", return_value=self.child(False)) as popen:
            self.assertTrue(services.dispatch_notice_needed(now=now))
            self.assertTrue(services.dispatch_notice_tick(force=True))
            popen.assert_called_once()

    def test_launching_row_inside_create_send_window_starts_no_pass_until_window_elapses(self):
        now = datetime.now(timezone.utc)
        for op, aid in (("create", None), ("resume", "w-vanished")):
            with self.subTest(operation=op):
                db = self.root / f"launching-{op}.sqlite3"
                row = base_paseo_row(f"d-launch-{op}", state="launching", agent_id=aid, now=now)
                row["operation"] = op
                row["launched_at"] = (now - timedelta(seconds=5)).isoformat()
                seed_dispatch_row(db, row)
                # Even with a dead watcher receipt, a launching row inside CREATE_SEND_WINDOW belongs to `roll`.
                self.write_receipt(f"d-launch-{op}", status="watching", pid=999999)
                with mock.patch.dict(os.environ, {"ROUTER_DISPATCH_DB": str(db)}), \
                        mock.patch.object(services, "hidden_popen") as popen:
                    self.assertFalse(services.dispatch_notice_needed(now=now))
                    self.assertFalse(services.dispatch_notice_tick(force=True))
                    popen.assert_not_called()
                    # Prove the gate reads registry.CREATE_SEND_WINDOW directly rather than a copied constant.
                    with mock.patch.object(registry, "CREATE_SEND_WINDOW", 120):
                        self.assertFalse(
                            services.dispatch_notice_needed(
                                now=datetime.fromisoformat(row["launched_at"]) + timedelta(seconds=90)
                            )
                        )
                        self.assertTrue(
                            services.dispatch_notice_needed(
                                now=datetime.fromisoformat(row["launched_at"]) + timedelta(seconds=120)
                            )
                        )
                    after_window = datetime.fromisoformat(row["launched_at"]) + timedelta(seconds=registry.CREATE_SEND_WINDOW)
                    self.assertTrue(services.dispatch_notice_needed(now=after_window))

    def assert_zero_processes_over_ticks(self, now: datetime, ticks: int = 4) -> None:
        mono = [1000.0]
        with mock.patch.object(services.time, "monotonic", side_effect=lambda: mono[0]), \
                mock.patch.object(registry, "utcnow", return_value=now), \
                mock.patch.object(services, "hidden_popen", return_value=self.child(False)) as popen:
            for _ in range(ticks):
                mono[0] += services.DISPATCH_NOTICE_INTERVAL + 5.0
                self.assertFalse(services.dispatch_notice_needed(now=now))
                self.assertFalse(services.dispatch_notice_tick())
            popen.assert_not_called()

    def test_rule1_watcher_restart_only_where_bridge_restarts_one(self):
        now = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)
        done_statuses = bridge_done_statuses()

        # Stuck state 1: Parent closed while worker still runs (`started` row with `status="parent_unavailable"`),
        # plus every other finished status in the bridge's `DONE` set.
        for done_status in sorted(done_statuses):
            with self.subTest(done_status=done_status):
                db_done = self.root / f"rule1-done-{done_status}.sqlite3"
                did = f"d-done-{done_status}"
                seed_dispatch_row(db_done, base_paseo_row(did, state="started", now=now))
                self.write_receipt(did, status=done_status, pid=999999)
                with mock.patch.dict(os.environ, {"ROUTER_DISPATCH_DB": str(db_done)}):
                    self.assert_zero_processes_over_ticks(now)
        self.assertEqual(getattr(services, "_WATCHER_DONE_STATUSES", None), done_statuses)

        # Stuck state 2: Expired row after a refused resume (`status="creation_failed"` + `.creation-failed` marker),
        # and an open row carrying `.creation-failed` even while its receipt still says `watching`.
        db_exp = self.root / "rule1-expired-refused-resume.sqlite3"
        exp_row = base_paseo_row("d-exp-nb", state="expired", agent_id="w-prev", now=now)
        exp_row["operation"] = "resume"
        exp_row["launched_at"] = (now - timedelta(minutes=15)).isoformat()
        exp_row["lease_until"] = (now - timedelta(minutes=5)).isoformat()
        exp_row["hard_until"] = (now + timedelta(hours=23)).isoformat()
        seed_dispatch_row(db_exp, exp_row)
        self.write_receipt("d-exp-nb", status="creation_failed", pid=999999)
        marker_exp = self.state_dir / "paseo-notifications" / "d-exp-nb.json.creation-failed"
        marker_exp.write_text("host_resume_failed", encoding="utf-8")
        with mock.patch.dict(os.environ, {"ROUTER_DISPATCH_DB": str(db_exp)}):
            self.assert_zero_processes_over_ticks(now)

        db_marker = self.root / "rule1-watching-marker.sqlite3"
        seed_dispatch_row(db_marker, base_paseo_row("d-watch-marker", state="started", now=now))
        self.write_receipt("d-watch-marker", status="watching", pid=999999)
        (self.state_dir / "paseo-notifications" / "d-watch-marker.json.creation-failed").write_text(
            "released", encoding="utf-8"
        )
        with mock.patch.dict(os.environ, {"ROUTER_DISPATCH_DB": str(db_marker)}):
            self.assert_zero_processes_over_ticks(now)

        # Stuck state 3: Open `started` row with missing, unreadable, non-dict, or another dispatch's receipt.
        notif_dir = self.state_dir / "paseo-notifications"
        notif_dir.mkdir(parents=True, exist_ok=True)
        for case, raw in (
            ("missing", None),
            ("corrupt", "{not valid json"),
            ("list", "[1, 2, 3]"),
            ("other_dispatch", json.dumps({"dispatch_id": "d-other", "parent_id": "parent-1", "status": "watching", "pid": 999999})),
            ("missing_parent", json.dumps({"dispatch_id": "d-open-bad", "parent_id": "", "status": "watching", "pid": 999999})),
        ):
            with self.subTest(receipt_case=case):
                db_bad = self.root / f"rule1-{case}.sqlite3"
                seed_dispatch_row(db_bad, base_paseo_row("d-open-bad", state="started", now=now))
                r_file = notif_dir / "d-open-bad.json"
                if raw is None:
                    r_file.unlink(missing_ok=True)
                else:
                    r_file.write_text(raw, encoding="utf-8")
                with mock.patch.dict(os.environ, {"ROUTER_DISPATCH_DB": str(db_bad)}):
                    self.assert_zero_processes_over_ticks(now)

        # Positive control: readable `watching` receipt of this dispatch with no marker and dead PID asks for a pass.
        db_ok = self.root / "rule1-ok.sqlite3"
        seed_dispatch_row(db_ok, base_paseo_row("d-open-ok", state="started", now=now))
        self.write_receipt("d-open-ok", status="watching", pid=999999)
        with mock.patch.dict(os.environ, {"ROUTER_DISPATCH_DB": str(db_ok)}):
            self.assertTrue(services.dispatch_notice_needed(now=now))

    def test_rule2_live_watcher_is_healthy_regardless_of_receipt_age_including_resend_wait(self):
        now = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)
        bridge = str(services.ROOT / "setup" / "paseo_dispatch_bridge.mjs")

        def fake_proc(cmdline, running=True):
            proc = mock.Mock()
            proc.is_running.return_value = running
            proc.cmdline.return_value = cmdline
            return proc

        # Stuck state: Live watcher waiting on a 15-minute `agent_request_outcome_unknown` resend interval for a permission notice.
        db_perm = self.root / "rule2-perm-wait.sqlite3"
        seed_dispatch_row(db_perm, base_paseo_row("d-perm-wait", state="started", now=now))
        self.write_receipt(
            "d-perm-wait",
            status="watching",
            pid=7777,
            pending={
                "key": "permission:perm-1",
                "text": "Permission waiting",
                "saved_at": (now - timedelta(minutes=10)).isoformat(),
                "sent_at": (now - timedelta(minutes=10)).isoformat(),
                "unknown": True,
            },
            mtime_age_s=600.0,
        )
        with mock.patch.dict(os.environ, {"ROUTER_DISPATCH_DB": str(db_perm)}), \
                mock.patch.object(services.psutil, "Process", return_value=fake_proc(["node", bridge, "--watch", "d-perm-wait", "parent-1", "worker-1", ""])):
            self.assert_zero_processes_over_ticks(now)
        self.assertFalse(hasattr(services, "WATCHER_RECEIPT_STALE_SECONDS"))

        # Live watcher with an aged/stalled receipt (no pending notice, or past `retry_at`) still starts zero passes.
        db_aged = self.root / "rule2-aged.sqlite3"
        seed_dispatch_row(db_aged, base_paseo_row("d-aged", state="started", now=now))
        self.write_receipt("d-aged", status="watching", pid=7777, mtime_age_s=900.0)
        with mock.patch.dict(os.environ, {"ROUTER_DISPATCH_DB": str(db_aged)}), \
                mock.patch.object(services.psutil, "Process", return_value=fake_proc(["node", bridge, "--watch", "d-aged", "parent-1", "worker-1", ""])):
            self.assert_zero_processes_over_ticks(now)

        # PID reuse check is preserved: PID must actually be `paseo_dispatch_bridge.mjs --watch <dispatch_id>`.
        for bad_cmd in (
            ["python.exe", "-c", "print(1)"],
            ["node", bridge],
            ["node", bridge, "--watch", "d-other", "parent-1", "worker-1", ""],
        ):
            with self.subTest(cmdline=bad_cmd), \
                    mock.patch.dict(os.environ, {"ROUTER_DISPATCH_DB": str(db_aged)}), \
                    mock.patch.object(services.psutil, "Process", return_value=fake_proc(bad_cmd)):
                self.assertTrue(services.dispatch_notice_needed(now=now))

        # Even when every open dispatch's watcher is healthy, a terminal due end notice still starts a pass.
        seed_dispatch_row(
            db_aged,
            base_paseo_row("d-due-still", state="completed", now=now, end_notice={"state": "due", "message_id": "m-due"}),
        )
        self.write_receipt("d-due-still", status="delivery_failed", pid=8888)
        with mock.patch.dict(os.environ, {"ROUTER_DISPATCH_DB": str(db_aged)}), \
                mock.patch.object(services.psutil, "Process", return_value=fake_proc(["node", bridge, "--watch", "d-aged", "parent-1", "worker-1", ""])):
            self.assertTrue(services.dispatch_notice_needed(now=now))

    def test_rule3_due_notice_requires_readable_matching_receipt_for_notify_end(self):
        now = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)
        seed_dispatch_row(
            self.db_path,
            base_paseo_row(
                "d-due-bad",
                state="completed",
                parent_id="parent-1",
                now=now,
                end_notice={"state": "due", "message_id": registry.end_notice_message_id("d-due-bad")},
            ),
        )
        self.assertEqual(registry.needs_reader(path=self.db_path, now=now)["due"], ["d-due-bad"])
        notif_file = self.state_dir / "paseo-notifications" / "d-due-bad.json"
        notif_file.parent.mkdir(parents=True, exist_ok=True)

        # Stuck state: corrupt, non-dict, mismatched-dispatch, or mismatched-parent receipt on a due notice.
        for case, raw in (
            ("corrupt_json", "{corrupt json"),
            ("json_array", "[1, 2]"),
            ("wrong_dispatch", json.dumps({"dispatch_id": "d-other", "parent_id": "parent-1", "status": "delivery_failed"})),
            ("wrong_parent", json.dumps({"dispatch_id": "d-due-bad", "parent_id": "parent-other", "status": "delivery_failed"})),
            ("empty_parent", json.dumps({"dispatch_id": "d-due-bad", "parent_id": "", "status": "delivery_failed"})),
        ):
            with self.subTest(case=case):
                notif_file.write_text(raw, encoding="utf-8")
                self.assert_zero_processes_over_ticks(now)

        # Once the receipt is a readable record of `d-due-bad` matching `parent-1`, `notify_end` can act.
        self.write_receipt("d-due-bad", status="delivery_failed", parent_id="parent-1")
        self.assertTrue(services.dispatch_notice_needed(now=now))

    def test_rule4_guard_active_chain_checks_launched_attempt_never_prepared_attempt(self):
        now = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)
        bridge = str(services.ROOT / "setup" / "paseo_dispatch_bridge.mjs")
        guard_until = (now + timedelta(seconds=600)).isoformat()
        root_row = base_paseo_row(
            "d-chain-root",
            state="failed",
            agent_id="w-chain-root",
            now=now - timedelta(seconds=20),
            end_notice={"state": "due", "message_id": registry.end_notice_message_id("d-chain-root")},
        )
        root_row["observed_at"] = root_row["launched_at"]
        root_row["fallback"] = {
            "state": "relaunching",
            "current": "d-chain-2",
            "until": guard_until,
            "attempts": [
                {"attempt": 1, "dispatch_id": "d-chain-root", "bundle_id": "codex:gpt-6.1-sol:medium"},
                {"attempt": 2, "dispatch_id": "d-chain-2", "bundle_id": "codex:gpt-6.1-sol:medium"},
            ],
            "handled": {
                "d-chain-root": {"action": "pending", "at": now.isoformat(), "until": guard_until},
            },
        }
        attempt2 = base_paseo_row("d-chain-2", state="prepared", agent_id=None, now=now, launched=False)
        attempt2["attempt_of"] = "d-chain-root"
        attempt2["attempt"] = 2
        seed_dispatch_row(self.db_path, root_row)
        seed_dispatch_row(self.db_path, attempt2)

        live_root_proc = mock.Mock()
        live_root_proc.is_running.return_value = True
        live_root_proc.cmdline.return_value = ["node", bridge, "--watch", "d-chain-root", "parent-1", "w-chain-root", ""]
        dead_proc = mock.Mock()
        dead_proc.is_running.return_value = False

        # Stuck state: `d-chain-2` is `prepared` (missing receipt `d-chain-2.json`), while `d-chain-root` has a live watcher
        # (or no receipt at all). Zero processes start across ticks.
        self.assert_zero_processes_over_ticks(now)
        self.write_receipt("d-chain-root", status="watching", pid=3131)
        with mock.patch.object(services.psutil, "Process", return_value=live_root_proc):
            self.assert_zero_processes_over_ticks(now)

        # Even if a stray dead `watching` receipt exists for the unlaunched `prepared` attempt `d-chain-2`,
        # a `prepared` current attempt has no watcher for `reconcile` to restart (`never_launched`).
        self.write_receipt("d-chain-2", status="watching", pid=999999)
        with mock.patch.object(
            services.psutil,
            "Process",
            side_effect=lambda pid: live_root_proc if pid == 3131 else dead_proc,
        ):
            self.assert_zero_processes_over_ticks(now)

        # When the launched failed attempt `d-chain-root`'s watcher died, the chain check catches `d-chain-root`.
        (self.state_dir / "paseo-notifications" / "d-chain-2.json").unlink(missing_ok=True)
        with mock.patch.object(services.psutil, "Process", return_value=dead_proc):
            self.assertTrue(services.dispatch_notice_needed(now=now))

    def test_rule5_stopped_paseo_daemon_starts_zero_passes_for_due_notices_and_watchers(self):
        now = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)
        bridge = str(services.ROOT / "setup" / "paseo_dispatch_bridge.mjs")

        # When nothing locally needs a pass, the gate does not even probe `dispatch_flow.paseo_reachable`.
        with mock.patch.object(dispatch_flow, "paseo_reachable") as probe:
            self.assertFalse(services.dispatch_notice_needed(now=now))
            probe.assert_not_called()

        closed_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        closed_sock.bind(("127.0.0.1", 0))
        closed_port = closed_sock.getsockname()[1]
        closed_sock.close()
        self.configure_paseo_daemon(port=closed_port)
        self.assertFalse(dispatch_flow.paseo_reachable(timeout=0.2))

        # Stuck state 1: Daemon down with a due notice.
        db_due = self.root / "rule5-due.sqlite3"
        seed_dispatch_row(
            db_due,
            base_paseo_row(
                "d-down-due",
                state="completed",
                now=now,
                end_notice={"state": "due", "message_id": registry.end_notice_message_id("d-down-due")},
            ),
        )
        self.write_receipt("d-down-due", status="delivery_failed")
        with mock.patch.dict(os.environ, {"ROUTER_DISPATCH_DB": str(db_due)}):
            self.assert_zero_processes_over_ticks(now)

        # Stuck state 2: Daemon down with a live watcher (aged receipt) AND with a dead watcher.
        db_open = self.root / "rule5-open.sqlite3"
        seed_dispatch_row(db_open, base_paseo_row("d-down-open", state="started", now=now))
        self.write_receipt("d-down-open", status="watching", pid=6161, mtime_age_s=120.0)
        live_proc = mock.Mock()
        live_proc.is_running.return_value = True
        live_proc.cmdline.return_value = ["node", bridge, "--watch", "d-down-open", "parent-1", "worker-1", ""]
        dead_proc = mock.Mock()
        dead_proc.is_running.return_value = False
        with mock.patch.dict(os.environ, {"ROUTER_DISPATCH_DB": str(db_open)}):
            with mock.patch.object(services.psutil, "Process", return_value=live_proc):
                self.assert_zero_processes_over_ticks(now)
            with mock.patch.object(services.psutil, "Process", return_value=dead_proc):
                self.assert_zero_processes_over_ticks(now)

        # Once the local Paseo daemon answers again, both the due notice and the dead watcher start a pass.
        self.configure_paseo_daemon(port=self.daemon_port)
        self.assertTrue(dispatch_flow.paseo_reachable(timeout=0.2))
        with mock.patch.dict(os.environ, {"ROUTER_DISPATCH_DB": str(db_due)}):
            self.assertTrue(services.dispatch_notice_needed(now=now))
        with mock.patch.dict(os.environ, {"ROUTER_DISPATCH_DB": str(db_open)}), \
                mock.patch.object(services.psutil, "Process", return_value=dead_proc):
            self.assertTrue(services.dispatch_notice_needed(now=now))

    def test_rule6_unchanged_reason_set_doubles_wait_up_to_ten_minutes_and_resets_on_change_or_restart(self):
        now = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)
        # Unseen no-op state not covered by Rules 1-5: a valid due notice with a readable receipt and a reachable daemon,
        # where the pass exits 0 without modifying the registry or receipts (e.g. `timeline_unavailable` in `composeNotice`).
        seed_dispatch_row(
            self.db_path,
            base_paseo_row(
                "d-noop",
                state="completed",
                now=now,
                end_notice={"state": "due", "message_id": registry.end_notice_message_id("d-noop")},
            ),
        )
        self.write_receipt("d-noop", status="delivery_failed")

        mono = [1000.0]
        started_times: list[float] = []

        def spawn_noop(*_args, **_kwargs):
            started_times.append(mono[0] - 1000.0)
            return self.child(False)

        with mock.patch.object(services.time, "monotonic", side_effect=lambda: mono[0]), \
                mock.patch.object(registry, "utcnow", return_value=now), \
                mock.patch.object(services, "hidden_popen", side_effect=spawn_noop):
            # Pass 1 at t=0.
            self.assertTrue(services.dispatch_notice_tick())
            self.assertEqual(started_times, [0.0])

            # Unchanged set: waits double 30 -> 60 -> 120 -> 240 -> 480 -> 600 -> 600 (10 min cap), never repeating at 30 s.
            expected_waits = (60, 120, 240, 480, 600, 600)
            cursor = 0.0
            for wait_s in expected_waits:
                for probe_offset in (10, 30, wait_s - 1):
                    mono[0] = 1000.0 + cursor + probe_offset
                    self.assertFalse(
                        services.dispatch_notice_tick(),
                        f"unexpected pass at +{probe_offset}s during {wait_s}s backoff",
                    )
                cursor += wait_s
                mono[0] = 1000.0 + cursor
                self.assertTrue(services.dispatch_notice_tick(), f"expected pass at +{wait_s}s")
            self.assertEqual(started_times, [0.0, 60.0, 180.0, 420.0, 900.0, 1500.0, 2100.0])

            # Any change of the set (a second dispatch `d-new` needing watcher restart) returns the wait to 30 s immediately.
            seed_dispatch_row(self.db_path, base_paseo_row("d-new", state="started", now=now))
            self.write_receipt("d-new", status="watching", pid=999999)
            mono[0] = 1000.0 + cursor + 35.0
            self.assertTrue(services.dispatch_notice_tick())
            self.assertEqual(started_times[-1], 2135.0)

            # With the new 2-dispatch set also unchanged after Pass 8 finishes, +30 s is blocked again (doubled to 60 s).
            mono[0] = 1000.0 + 2135.0 + 30.0
            self.assertFalse(services.dispatch_notice_tick())

            # A tray restart (fresh in-memory gate state) returns to 30 s / immediate start.
            self.reset_notice_state()
            mono[0] = 1000.0 + 2166.0
            self.assertTrue(services.dispatch_notice_tick())
            self.assertEqual(started_times[-1], 2166.0)

    def test_dispatch_notice_log_rotates_above_size_bound(self):
        seed_dispatch_row(self.db_path, base_paseo_row("d-log", state="started"))
        self.write_receipt("d-log", status="watching", pid=999999)
        log_path = self.state_dir / "dispatch-notice.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        oversized = b"L" * (services.DISPATCH_NOTICE_LOG_MAX_BYTES + 64)
        log_path.write_bytes(oversized)
        with mock.patch.object(services, "hidden_popen", return_value=self.child(True)) as popen:
            self.assertTrue(services.dispatch_notice_tick(force=True))
        popen.assert_called_once()
        rotated = self.state_dir / "dispatch-notice.log.1"
        self.assertTrue(rotated.is_file())
        self.assertEqual(rotated.read_bytes(), oversized)
        self.assertFalse(log_path.exists())

    def test_junctioned_codex_skill_resolves_to_checkout_agentcompass_never_route_judge(self):
        codex_skills = self.root / ".codex" / "skills"
        codex_skills.mkdir(parents=True, exist_ok=True)
        junc = codex_skills / "agent-compass"
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junc), str(services.ROOT / "skills" / "agent-compass")],
            capture_output=True,
            check=True,
            creationflags=services.CREATE_NO_WINDOW,
        )
        self.addCleanup(
            subprocess.run,
            ["cmd", "/c", "rmdir", str(junc)],
            capture_output=True,
            creationflags=services.CREATE_NO_WINDOW,
        )
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.root / ".codex")}):
            cmd = services.dispatch_reconcile_command()
        self.assertIsNotNone(cmd)
        self.assertNotIn("-I", cmd)
        self.assertFalse(any(part.endswith("route_judge.py") for part in cmd), cmd)
        self.assertEqual(Path(cmd[3]).resolve(), (services.ROOT / "agentcompass").resolve())
        self.assertEqual(cmd[4:], ["dispatch", "--dispatch-action", "reconcile"])

    def test_missing_cli_and_popen_oserror_are_quiet_and_throttled_to_interval(self):
        seed_dispatch_row(self.db_path, base_paseo_row("d-nocli", state="started"))
        self.write_receipt("d-nocli", status="watching", pid=999999)
        needed_calls = 0
        orig_needed = services.dispatch_notice_needed

        def counting_needed(*args, **kwargs):
            nonlocal needed_calls
            needed_calls += 1
            return orig_needed(*args, **kwargs)

        clock = [1000.0]
        with mock.patch.object(services, "dispatch_notice_needed", side_effect=counting_needed), \
                mock.patch.object(services, "dispatch_reconcile_command", return_value=None), \
                mock.patch.object(services.time, "monotonic", side_effect=lambda: clock[0]):
            for offset in (0, 10, 20, 30):
                clock[0] = 1000.0 + offset
                self.assertFalse(services.dispatch_notice_tick(force=False))
        self.assertIsNotNone(services._dispatch_notice["started"])
        self.assertEqual(needed_calls, 2)

        services._dispatch_notice.update(proc=None, started=None)
        needed_calls = 0
        with mock.patch.object(services, "dispatch_notice_needed", side_effect=counting_needed), \
                mock.patch.object(services, "hidden_popen", side_effect=OSError("spawn failed")) as popen, \
                mock.patch.object(services.time, "monotonic", side_effect=lambda: clock[0]):
            for offset in (0, 10, 20, 30):
                clock[0] = 2000.0 + offset
                self.assertFalse(services.dispatch_notice_tick(force=False))
        self.assertEqual(needed_calls, 2)
        self.assertEqual(popen.call_count, 2)

    def test_corrupt_non_database_registry_starts_no_pass_while_locked_database_still_may(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        for name, payload in (
            ("garbage", b"not a sqlite database"),
            ("malformed_header", b"SQLite format 3\x00" + b"\xff" * 512),
            ("empty_file", b""),
        ):
            with self.subTest(case=name):
                bad_db = self.root / f"{name}.sqlite3"
                bad_db.write_bytes(payload)
                with mock.patch.dict(os.environ, {"ROUTER_DISPATCH_DB": str(bad_db)}), \
                        mock.patch.object(services, "hidden_popen") as popen:
                    self.assertFalse(services.dispatch_notice_needed())
                    self.assertFalse(services.dispatch_notice_tick(force=True))
                    popen.assert_not_called()

        locked_db = self.root / "locked.sqlite3"
        seed_dispatch_row(locked_db, base_paseo_row("d-locked", state="started"))
        holder = sqlite3.connect(locked_db, isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute("BEGIN EXCLUSIVE")
        try:
            with mock.patch.dict(os.environ, {"ROUTER_DISPATCH_DB": str(locked_db)}):
                self.assertTrue(services.dispatch_notice_needed())
        finally:
            holder.rollback()

    def test_frozen_tray_invokes_bundled_cli_executable_never_windowed_tray(self):
        install_root = self.root / "install"
        tray_exe = install_root / "AgentCompass.exe"
        cli_exe = install_root / "cli" / "agentcompass.exe"
        cli_exe.parent.mkdir(parents=True, exist_ok=True)
        tray_exe.write_bytes(b"tray")
        cli_exe.write_bytes(b"cli")
        seed_dispatch_row(self.db_path, base_paseo_row("d-frozen", state="started"))
        self.write_receipt("d-frozen", status="watching", pid=999999)
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
        self.write_receipt("d-flight", status="watching", pid=999999)
        with mock.patch.object(services, "hidden_popen", return_value=self.child(True)) as popen:
            self.assertTrue(services.dispatch_notice_tick())
            self.assertFalse(services.dispatch_notice_tick(force=True))
            self.assertEqual(popen.call_count, 1)

        self.reset_notice_state()
        with mock.patch.object(services, "hidden_popen", return_value=self.child(False)) as popen:
            self.assertTrue(services.dispatch_notice_tick())
            self.assertFalse(services.dispatch_notice_tick())
            self.assertTrue(services.dispatch_notice_tick(force=True))
            # A changed reason set returns the interval to 30 s.
            seed_dispatch_row(self.db_path, base_paseo_row("d-flight-2", state="started"))
            self.write_receipt("d-flight-2", status="watching", pid=999999)
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

        # When `observe` is still alive with a newly spawned `--watch` child beneath it, stopping terminates
        # `route_judge` and `observe` (leaves-first) while sparing `--watch` and anything beneath `--watch`.
        bridge = str(services.ROOT / "setup" / "paseo_dispatch_bridge.mjs")
        watch_sub = mock.Mock(pid=9004)
        watch_sub.cmdline.return_value = ["python.exe", "-m", "agentcompass", "continuation-verdict", "w-1"]
        watch_child = mock.Mock(pid=9003)
        watch_child.cmdline.return_value = ["node", bridge, "--watch", "d-1", "parent-1", "w-1", ""]
        watch_child.children.return_value = [watch_sub]
        observe_child = mock.Mock(pid=9002)
        observe_child.cmdline.return_value = ["node", bridge]
        observe_child.children.return_value = [watch_child]
        observe_child.is_running.return_value = True
        judge_child = mock.Mock(pid=9001)
        judge_child.cmdline.return_value = ["python.exe", "-I", "-X", "utf8", "route_judge.py", "dispatch"]
        judge_child.children.return_value = [observe_child]
        judge_child.is_running.return_value = True
        root_ps = mock.Mock(pid=9000)
        root_ps.children.return_value = [judge_child]

        fake_popen = mock.Mock(spec=subprocess.Popen, pid=9000)
        fake_popen.poll.return_value = None
        services._dispatch_notice["proc"] = fake_popen
        with mock.patch.object(services.psutil, "Process", return_value=root_ps), \
                mock.patch.object(services.psutil, "wait_procs", return_value=([observe_child, judge_child], [])):
            services.stop_dispatch_notice()
        observe_child.terminate.assert_called_once_with()
        judge_child.terminate.assert_called_once_with()
        fake_popen.terminate.assert_called_once_with()
        watch_child.terminate.assert_not_called()
        watch_sub.terminate.assert_not_called()


class DispatchNoticeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_dir = self.root / ".agent-compass"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.paseo_home = self.root / ".paseo"
        self.paseo_home.mkdir(parents=True, exist_ok=True)
        self._daemon_sock, self.daemon_port, self._daemon_stop = start_loopback_listener()
        self.addCleanup(self._stop_daemon_listener)
        (self.paseo_home / "config.json").write_text(
            json.dumps({"daemon": {"listen": f"127.0.0.1:{self.daemon_port}"}}),
            encoding="utf-8",
        )
        (self.paseo_home / "local-credential").write_text("fixture-token\n", encoding="utf-8")
        self.db_path = self.state_dir / "dispatch.sqlite3"
        self.sdk_root = write_fake_sdk(self.root)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        (self.bin_dir / "claude.cmd").write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")
        self.claude_home = self.root / ".claude"
        self.claude_home.mkdir(parents=True, exist_ok=True)
        auth_doc = {"claudeAiOauth": {"accessToken": "fixture-claude-token", "subscriptionType": "max"}}
        (self.claude_home / ".credentials.json").write_text(json.dumps(auth_doc), encoding="utf-8")
        cred_key = provider_inventory.auth_facts("claude", auth_doc, self.claude_home)["credential_key"]
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        resets_5h = (now_dt + timedelta(hours=4)).isoformat()
        resets_7d = (now_dt + timedelta(days=5)).isoformat()
        (self.state_dir / "usage-cache.json").write_text(
            json.dumps(
                {
                    "providers": {
                        "claude": {
                            "provider_id": "claude",
                            "display_name": "claude",
                            "available": True,
                            "stale": False,
                            "plan_label": "max5",
                            "plan_source": "profile_api",
                            "credential_key": cred_key,
                            "fetched_at": now_iso,
                            "windows": [
                                {"id": "five_hour", "label": "5h", "used_pct": 10.0, "resets_at": resets_5h},
                                {"id": "seven_day", "label": "7d", "used_pct": 10.0, "resets_at": resets_7d},
                                {"id": "weekly_fable", "label": "7d-fable", "used_pct": 10.0, "resets_at": resets_7d},
                            ],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        env_patch = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.root),
                "USERPROFILE": str(self.root),
                "CLAUDE_CONFIG_DIR": str(self.claude_home),
                "ROUTER_PROVIDER_HOME": str(self.root),
                "ROUTER_STATE_DIR": str(self.state_dir),
                "ROUTER_DISPATCH_DB": str(self.db_path),
                "PASEO_HOME": str(self.paseo_home),
                "ROUTER_PASEO_SDK_ROOT": str(self.sdk_root),
                "ROUTER_PROVIDER_RETRY": "off",
                "AGENTCOMPASS_PUBLIC_DATA": "off",
                "AGENTCOMPASS_ANTIGRAVITY_PLAN": "off",
                "AGENTCOMPASS_LIVE_MODELS": "off",
                "PATH": str(self.bin_dir) + os.pathsep + os.environ.get("PATH", ""),
            },
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)
        os.environ.pop("AGENTCOMPASS_ROLL_FALLBACK", None)
        state_patch = mock.patch.object(services, "STATE_DIR", self.state_dir)
        state_patch.start()
        self.addCleanup(state_patch.stop)
        self.addCleanup(self._cleanup_processes)
        if hasattr(services, "_reset_dispatch_notice"):
            services._reset_dispatch_notice()
        else:
            services._dispatch_notice.update(proc=None, started=None)

    def _stop_daemon_listener(self) -> None:
        self._daemon_stop.set()
        try:
            self._daemon_sock.close()
        except OSError:
            pass

    def _cleanup_processes(self) -> None:
        proc = services._dispatch_notice.get("proc")
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if hasattr(services, "_reset_dispatch_notice"):
            services._reset_dispatch_notice()
        else:
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
        state_dir: Path | None = None,
    ) -> Path:
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait(timeout=5)
        dead_pid = dead.pid
        notif_dir = (state_dir or self.state_dir) / "paseo-notifications"
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

        # 2. While the watcher is alive and writing, another tick starts nothing at all.
        conns_before_idle_tick = len(self.read_jsonl("connections.jsonl"))
        self.assertFalse(services.dispatch_notice_needed())
        self.assertFalse(services.dispatch_notice_tick(force=True))
        self.assertEqual(len(self.read_jsonl("connections.jsonl")), conns_before_idle_tick)
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

    def seed_routed_started_dispatch(
        self,
        task_id: str,
        agent_id: str,
        *,
        parent_id: str = "parent-1",
        can_wait_hours: float | None = None,
        now: datetime | None = None,
    ) -> dict:
        now = now or registry.utcnow()
        task = {
            "task_id": task_id,
            "parent_agent_id": parent_id,
            "expected_hours": 0.5,
            **({"can_wait_hours": can_wait_hours} if can_wait_hours is not None else {}),
            "profile": {
                "work_kind": "code_implementation",
                "primary_level": 2,
                "secondary_kind": "none",
                "secondary_level": 0,
                "needs_visual": False,
                "local_only": False,
                "wants_fast": False,
                "access": "write",
                "expected_hours": 0.5,
                "task_count": "1",
                "reason": "integration fixture task",
            },
        }
        accounts = account_fleet.load_accounts()
        usage = usage_reading.usage_snapshot(None)
        planned = allocator.plan(task, usage, now, accounts=accounts)
        decision = {**planned, **registry.issue(planned, task, now=now, path=self.db_path)}
        prepared = registry.prepare(
            decision["decision_id"],
            task,
            {"title": f"Task {task_id}", "initialPrompt": "Run fixture task"},
            usage,
            accounts=accounts,
            now=now,
            path=self.db_path,
        )
        claimed = registry.claim(prepared["request"], now=now, path=self.db_path)
        provider, _, model = claimed["request"]["provider"].partition("/")
        settings = claimed["request"].get("settings") or {}
        snap = {
            "id": agent_id,
            "provider": provider,
            "model": model,
            "status": "running",
            "currentModeId": settings.get("modeId"),
            "effectiveThinkingOptionId": settings.get("thinkingOptionId"),
            "labels": claimed["request"]["labels"],
            "updatedAt": (now + timedelta(seconds=1)).isoformat(),
        }
        return registry.observe(claimed["dispatch_id"], snap, now=now + timedelta(seconds=1), path=self.db_path)

    def test_worker_failed_while_watcher_was_dead_relaunches_by_default_and_sends_one_notice_for_the_brief(self):
        self.assertTrue(dispatch_fallback.enabled())
        first_row = self.seed_routed_started_dispatch("t-fb-brief", "w-fb-1")
        first_id = first_row["dispatch_id"]
        self.write_dead_watcher_receipt(first_id, agent_id="w-fb-1")
        provider_1, _, model_1 = first_row["request"]["provider"].partition("/")
        failed_agent = {
            "id": "w-fb-1",
            "status": "error",
            "attentionReason": "error",
            "provider": provider_1,
            "model": model_1,
            "labels": first_row["request"]["labels"],
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }
        self.write_host_state({"parent_id": "parent-1", "agents": {"w-fb-1": failed_agent}, "timelines": {}})

        # 1. Pass runs reconcile: settles attempt 1 as failed, sends NO error notice (fallback holds it), and restarts
        #    attempt 1's watcher, which runs fallback and creates attempt 2 (`created_agents.jsonl`).
        self.run_pass_and_wait(force=True)

        def created_without_early_error():
            early = self.read_jsonl("messages.jsonl")
            if early:
                self.fail(f"expected no notice before fallback finished, got: {early}")
            return self.read_jsonl("created_agents.jsonl")

        created = self.wait_until(created_without_early_error, timeout=20.0)
        self.assertEqual(len(created), 1)
        second_id = created[0]["id"]
        self.assertNotEqual(second_id, first_id)

        # Wait for attempt 1's watcher to record `relaunched`, settle, and exit — still zero messages to `parent-1`.
        self.wait_until(
            lambda: (
                (registry.inspect(first_id, self.db_path).get("end_notice") or {}).get("source") == "relaunched"
                and not psutil.pid_exists(
                    json.loads((self.state_dir / "paseo-notifications" / f"{first_id}.json").read_text(encoding="utf-8")).get("pid", -1)
                )
            ),
            timeout=20.0,
        )
        second_watcher_pid = json.loads(
            (self.state_dir / "paseo-notifications" / f"{second_id}.json").read_text(encoding="utf-8")
        )["pid"]
        hermetic_run._started.add(second_watcher_pid)
        self.assertEqual(self.read_jsonl("messages.jsonl"), [])

        # 2. The relaunched attempt finishes; its watcher sends the single notice for the brief.
        host_state = json.loads((self.state_dir / "fake-paseo-host.json").read_text(encoding="utf-8"))
        done_at = datetime.now(timezone.utc).isoformat()
        host_state["agents"][second_id].update(status="idle", attentionReason="finished", updatedAt=done_at)
        host_state.setdefault("timelines", {})[second_id] = [
            {"timestamp": first_row["created_at"], "item": {"type": "user_message", "text": "Run fixture task"}},
            {"timestamp": done_at, "item": {"type": "assistant_message", "text": "Recovered on fallback candidate."}},
        ]
        self.write_host_state(host_state)

        msgs = self.wait_until(lambda: self.read_jsonl("messages.jsonl"), timeout=20.0)
        self.wait_until(
            lambda: (
                registry.inspect(second_id, self.db_path)["state"] == "completed"
                and (registry.inspect(second_id, self.db_path).get("end_notice") or {}).get("state") == "sent"
                and not psutil.pid_exists(second_watcher_pid)
            ),
            timeout=20.0,
        )
        self.assertEqual(len(msgs), 1)
        self.assertIn("State: finished", msgs[0]["text"])
        self.assertNotIn("State: error", msgs[0]["text"])
        self.assertIn("Recovered on fallback candidate.", msgs[0]["text"])
        self.assertIn(f"Dispatch: {first_id}", msgs[0]["text"])
        expected_second_msg_id = registry.end_notice_message_id(second_id)
        raw = [m["options"]["messageId"] for m in self.read_jsonl("raw_sends.jsonl")]
        self.assertEqual(raw, [expected_second_msg_id])
        self.assertFalse(services.dispatch_notice_needed())
        self.assertFalse(services.dispatch_notice_tick(force=True))

    def test_dead_watcher_during_scheduled_retry_restarts_launches_retry_at_retry_at_and_notifies_once(self):
        self.assertTrue(dispatch_fallback.enabled())
        retry_store = self.state_dir / provider_retry.FILE
        with mock.patch.dict(os.environ, {"ROUTER_PROVIDER_RETRY": str(retry_store)}):
            now_utc = datetime.now(timezone.utc).replace(microsecond=0)
            backoff_s = int(provider_retry.retry_seconds(1))
            launched_dt = now_utc - timedelta(seconds=backoff_s + 15)
            failed_dt = now_utc - timedelta(seconds=backoff_s - 4)  # retry_at ~4 s in the future
            first_row = self.seed_routed_started_dispatch(
                "t-sched-retry",
                "w-sched-1",
                can_wait_hours=1.0,
                now=launched_dt,
            )
            first_id = first_row["dispatch_id"]
            provider_1, _, model_1 = first_row["request"]["provider"].partition("/")
            terminal = {
                "reason": "error",
                "status": "error",
                "attentionReason": "error",
                "at": failed_dt.isoformat(),
                "failure_code": "provider_turn_failed",
                "provider_message": "503 upstream gateway timeout",
            }
            provider_retry.record(
                first_row["bundle"],
                terminal,
                dispatch_id=first_id,
                at=failed_dt.isoformat(),
                classify=lambda *_: ("transient", {"source": "system_one", "scorer_type": "fixture"}),
            )
            failed_agent = {
                "id": "w-sched-1",
                "status": "error",
                "attentionReason": "error",
                "lastError": "503 upstream gateway timeout",
                "provider": provider_1,
                "model": model_1,
                "labels": first_row["request"]["labels"],
                "updatedAt": failed_dt.isoformat(),
            }
            self.write_host_state({"parent_id": "parent-1", "agents": {"w-sched-1": failed_agent}, "timelines": {}})
            # Settle attempt 1 (`provider_turn_failed`) and schedule its transient retry; `handle` starts `--watch`,
            # which we then terminate mid-wait to simulate a tray restart killing the watcher during backoff.
            receipt_path = self.write_dead_watcher_receipt(first_id, agent_id="w-sched-1")
            registry.observe(first_id, failed_agent, now=failed_dt, path=self.db_path, terminal=terminal)
            scheduled = dispatch_fallback.handle(first_id)
            self.assertEqual(scheduled["action"], "scheduled")
            retry_at_iso = scheduled["retry_at"]
            saved_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            initial_watcher_pid = saved_receipt["pid"]
            hermetic_run._started.add(initial_watcher_pid)
            try:
                proc_obj = psutil.Process(initial_watcher_pid)
                proc_obj.terminate()
                proc_obj.wait(timeout=5)
            except psutil.Error:
                pass
            self.assertFalse(psutil.pid_exists(initial_watcher_pid))
            saved_receipt.update(
                seen=True,
                started=True,
                status="watching",
                terminal=terminal,
                fallback={"status": "scheduled", "at": datetime.now(timezone.utc).isoformat(), "retry_at": retry_at_iso},
            )
            receipt_path.write_text(json.dumps(saved_receipt), encoding="utf-8")

            # `needs_reader` holds the notice side (`needed: False`), while the tray gate catches the dead watcher on the
            # guard-active chain (`registry.fallback_guard` + `registry.current_attempt`).
            needs = registry.needs_reader(path=self.db_path)
            self.assertFalse(needs["needed"], needs)
            self.assertEqual((needs["due"], needs["observe"]), ([], []))
            self.assertTrue(registry.fallback_guard(registry.inspect(first_id, self.db_path), registry.utcnow()))
            self.assertTrue(services.dispatch_notice_needed())

            # 1. Pass runs `reconcile`, observes `first_id` via `failed_attempts`, and restarts `--watch <first_id>`.
            self.run_pass_and_wait(force=True)
            log_text = (self.state_dir / "dispatch-notice.log").read_text(encoding="utf-8")
            reconcile_out = json.loads(log_text)
            self.assertEqual([obs["dispatch_id"] for obs in reconcile_out["observations"]], [first_id])
            restarted_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            restarted_pid = restarted_receipt["pid"]
            hermetic_run._started.add(restarted_pid)
            self.assertNotEqual(restarted_pid, initial_watcher_pid)
            self.assertEqual(restarted_receipt["status"], "watching")
            self.assertEqual((restarted_receipt.get("fallback") or {}).get("status"), "scheduled")
            self.assertTrue(psutil.pid_exists(restarted_pid))
            self.assertEqual(self.read_jsonl("messages.jsonl"), [])
            if datetime.now(timezone.utc) < registry.parse(retry_at_iso) - timedelta(seconds=1):
                self.assertEqual(self.read_jsonl("created_agents.jsonl"), [])
                self.assertFalse(services.dispatch_notice_needed())

            # 2. At `retry_at`, the restarted watcher runs `fallback`, launches the same-bundle retry, and exits quietly.
            def created_without_early_error():
                early = self.read_jsonl("messages.jsonl")
                if early:
                    self.fail(f"expected no notice before scheduled retry finished, got: {early}")
                return self.read_jsonl("created_agents.jsonl")

            created = self.wait_until(created_without_early_error, timeout=25.0)
            self.assertEqual(len(created), 1)
            second_id = created[0]["id"]
            self.assertNotEqual(second_id, first_id)
            self.assertEqual((created[0]["provider"], created[0]["model"]), (provider_1, model_1))

            self.wait_until(
                lambda: (
                    (registry.inspect(first_id, self.db_path).get("end_notice") or {}).get("source") == "relaunched"
                    and not psutil.pid_exists(restarted_pid)
                ),
                timeout=20.0,
            )
            second_watcher_pid = json.loads(
                (self.state_dir / "paseo-notifications" / f"{second_id}.json").read_text(encoding="utf-8")
            )["pid"]
            hermetic_run._started.add(second_watcher_pid)
            self.assertEqual(self.read_jsonl("messages.jsonl"), [])

            # 3. Attempt 2 finishes; its watcher sends the single notice for the brief.
            host_state = json.loads((self.state_dir / "fake-paseo-host.json").read_text(encoding="utf-8"))
            done_at = datetime.now(timezone.utc).isoformat()
            host_state["agents"][second_id].update(status="idle", attentionReason="finished", updatedAt=done_at)
            host_state.setdefault("timelines", {})[second_id] = [
                {"timestamp": first_row["created_at"], "item": {"type": "user_message", "text": "Run fixture task"}},
                {"timestamp": done_at, "item": {"type": "assistant_message", "text": "Completed on scheduled retry."}},
            ]
            self.write_host_state(host_state)

            msgs = self.wait_until(lambda: self.read_jsonl("messages.jsonl"), timeout=20.0)
            self.wait_until(
                lambda: (
                    registry.inspect(second_id, self.db_path)["state"] == "completed"
                    and (registry.inspect(second_id, self.db_path).get("end_notice") or {}).get("state") == "sent"
                    and not psutil.pid_exists(second_watcher_pid)
                ),
                timeout=20.0,
            )
            self.assertEqual(len(msgs), 1)
            self.assertIn("State: finished", msgs[0]["text"])
            self.assertIn("retried the same model after its backoff", msgs[0]["text"])
            self.assertIn("Completed on scheduled retry.", msgs[0]["text"])
            self.assertIn(f"Dispatch: {first_id}", msgs[0]["text"])
            expected_second_msg_id = registry.end_notice_message_id(second_id)
            raw = [m["options"]["messageId"] for m in self.read_jsonl("raw_sends.jsonl")]
            self.assertEqual(raw, [expected_second_msg_id])
            self.assertFalse(services.dispatch_notice_needed())
            self.assertFalse(services.dispatch_notice_tick(force=True))

    def test_worker_removed_or_failed_with_fallback_off_notifies_once(self):
        now = datetime.now(timezone.utc)
        # With default fallback ON, non-covered removals (before observation and after start) notify once immediately.
        rem_row = base_paseo_row("d-rem", state="launching", agent_id=None, now=now)
        rem_row["launched_at"] = (now - timedelta(seconds=registry.CREATE_SEND_WINDOW + 5)).isoformat()
        seed_dispatch_row(self.db_path, rem_row)
        self.write_dead_watcher_receipt("d-rem", agent_id=None)
        gone_row = base_paseo_row("d-gone", state="started", agent_id="w-gone", now=now)
        gone_row["observed_at"] = gone_row["launched_at"]
        seed_dispatch_row(self.db_path, gone_row)
        self.write_dead_watcher_receipt("d-gone", agent_id="w-gone")
        self.write_host_state(
            {
                "parent_id": "parent-1",
                "agents": {},
                "creations": {"d-rem": {"error": "Previously created agent no longer exists"}},
                "timelines": {},
            }
        )
        self.run_pass_and_wait(force=True)
        self.wait_until(
            lambda: (
                registry.inspect("d-rem", self.db_path)["state"] == "canceled"
                and (registry.inspect("d-rem", self.db_path).get("end_notice") or {}).get("state") == "sent"
                and registry.inspect("d-gone", self.db_path)["state"] == "canceled"
                and (registry.inspect("d-gone", self.db_path).get("end_notice") or {}).get("state") == "sent"
                and all(
                    not psutil.pid_exists(
                        json.loads((self.state_dir / "paseo-notifications" / f"{did}.json").read_text(encoding="utf-8")).get("pid", -1)
                    )
                    for did in ("d-rem", "d-gone")
                )
            )
        )
        self.assertTrue(
            any("Dispatch: d-rem" in m["text"] and "removed before" in m["text"] for m in self.read_jsonl("messages.jsonl"))
        )
        self.assertTrue(
            any("Dispatch: d-gone" in m["text"] and "State: worker unavailable" in m["text"] for m in self.read_jsonl("messages.jsonl"))
        )

        # With fallback explicitly off (`AGENTCOMPASS_ROLL_FALLBACK=off`), a failed worker sends one plain error notice.
        fail_row = base_paseo_row("d-fail", state="started", agent_id="w-fail", now=now)
        fail_row["observed_at"] = fail_row["launched_at"]
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
        self.write_host_state({"parent_id": "parent-1", "agents": {"w-fail": failed_agent}, "timelines": {}})
        with mock.patch.dict(os.environ, {"AGENTCOMPASS_ROLL_FALLBACK": "off"}):
            self.run_pass_and_wait(force=True)
            self.wait_until(
                lambda: (
                    registry.inspect("d-fail", self.db_path)["state"] == "failed"
                    and (registry.inspect("d-fail", self.db_path).get("end_notice") or {}).get("state") == "sent"
                    and not psutil.pid_exists(
                        json.loads((self.state_dir / "paseo-notifications" / "d-fail.json").read_text(encoding="utf-8")).get("pid", -1)
                    )
                )
            )
            self.assertFalse(services.dispatch_notice_needed())
            raw_before = len(self.read_jsonl("raw_sends.jsonl"))
            self.assertFalse(services.dispatch_notice_tick(force=True))
            self.assertEqual(len(self.read_jsonl("raw_sends.jsonl")), raw_before)

        for did in ("d-rem", "d-gone", "d-fail"):
            msgs = [m for m in self.read_jsonl("messages.jsonl") if f"Dispatch: {did}" in m["text"]]
            self.assertEqual(len(msgs), 1)
            expected_id = registry.end_notice_message_id(did)
            raw = [m["options"]["messageId"] for m in self.read_jsonl("raw_sends.jsonl") if f"Dispatch: {did}" in m["text"]]
            self.assertTrue(raw)
            self.assertEqual(set(raw), {expected_id})
        fail_msg = [m for m in self.read_jsonl("messages.jsonl") if "Dispatch: d-fail" in m["text"]][0]
        self.assertIn("State: error", fail_msg["text"])

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

        # Without `"daemon"` in `$PASEO_HOME/config.json`, `needs_reader` reports `due`, yet `deliver_end_notices`
        # would skip it: the gate must not start a pass every 30 s.
        cfg_path = self.paseo_home / "config.json"
        saved_cfg = cfg_path.read_text(encoding="utf-8")
        cfg_path.write_text("{}", encoding="utf-8")
        self.assertEqual(registry.needs_reader(path=self.db_path, now=now)["due"], ["d-due"])
        self.assertFalse(services.dispatch_notice_needed(now=now))
        self.assertFalse(services.dispatch_notice_tick(force=True))
        cfg_path.write_text(saved_cfg, encoding="utf-8")

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

        # Flip worker to finished while another dispatch (`d-other-due`) legitimately starts a reconcile pass,
        # racing `d-race`'s live watcher.
        done_at = datetime.now(timezone.utc).isoformat()
        trigger_row = base_paseo_row(
            "d-other-due",
            state="completed",
            agent_id="w-trigger",
            now=now,
            end_notice={"state": "due", "message_id": registry.end_notice_message_id("d-other-due")},
        )
        seed_dispatch_row(self.db_path, trigger_row)
        self.write_dead_watcher_receipt("d-other-due", agent_id="w-trigger", status="delivery_failed")
        self.write_host_state(
            {
                "parent_id": "parent-1",
                "agents": {
                    "w-race": dict(
                        running_agent,
                        status="idle",
                        attentionReason="finished",
                        updatedAt=done_at,
                    ),
                    "w-trigger": {
                        "id": "w-trigger",
                        "status": "idle",
                        "attentionReason": "finished",
                        "provider": "codex",
                        "model": "gpt-6.1-sol",
                        "labels": trigger_row["request"]["labels"],
                    },
                },
                "timelines": {
                    "w-race": [
                        {"timestamp": now.isoformat(), "item": {"type": "user_message", "text": "Run task"}},
                        {"timestamp": done_at, "item": {"type": "assistant_message", "text": "Race reply."}},
                    ],
                    "w-trigger": [
                        {"timestamp": done_at, "item": {"type": "assistant_message", "text": "Trigger reply."}}
                    ],
                },
            }
        )
        self.run_pass_and_wait(force=True)
        self.wait_until(
            lambda: (
                registry.inspect("d-race", self.db_path)["state"] == "completed"
                and (registry.inspect("d-race", self.db_path).get("end_notice") or {}).get("state") == "sent"
                and (registry.inspect("d-other-due", self.db_path).get("end_notice") or {}).get("state") == "sent"
                and not psutil.pid_exists(watcher_pid)
            )
        )

        msgs = [m for m in self.read_jsonl("messages.jsonl") if "Dispatch: d-race\n" in m["text"]]
        self.assertEqual(len(msgs), 1)
        self.assertIn("Race reply.", msgs[0]["text"])
        raw = [m["options"]["messageId"] for m in self.read_jsonl("raw_sends.jsonl") if "Dispatch: d-race\n" in m["text"]]
        self.assertTrue(raw)
        self.assertEqual(set(raw), {expected_id})
        raw_count = len(raw)
        self.assertFalse(services.dispatch_notice_needed())
        self.assertFalse(services.dispatch_notice_tick(force=True))
        self.assertEqual(
            len([m for m in self.read_jsonl("raw_sends.jsonl") if "Dispatch: d-race\n" in m["text"]]),
            raw_count,
        )

    def test_open_dispatches_with_live_watchers_spawn_zero_processes_and_zero_connections(self):
        for count in (1, 3):
            with self.subTest(open_dispatches=count):
                sub_dir = self.root / f"live-{count}"
                state_dir = sub_dir / ".agent-compass"
                state_dir.mkdir(parents=True, exist_ok=True)
                db_path = state_dir / "dispatch.sqlite3"
                now = datetime.now(timezone.utc)
                agents = {}
                with mock.patch.dict(os.environ, {"ROUTER_STATE_DIR": str(state_dir), "ROUTER_DISPATCH_DB": str(db_path)}), \
                        mock.patch.object(services, "STATE_DIR", state_dir):
                    for idx in range(1, count + 1):
                        did = f"d-live-{count}-{idx}"
                        aid = f"w-live-{count}-{idx}"
                        row = base_paseo_row(did, state="started", agent_id=aid, now=now)
                        seed_dispatch_row(db_path, row)
                        self.write_dead_watcher_receipt(did, agent_id=aid, state_dir=state_dir)
                        agents[aid] = {
                            "id": aid,
                            "status": "running",
                            "provider": "codex",
                            "model": "gpt-6.1-sol",
                            "currentModeId": "full-access",
                            "effectiveThinkingOptionId": "medium",
                            "labels": row["request"]["labels"],
                            "updatedAt": now.isoformat(),
                        }
                    (state_dir / "fake-paseo-host.json").write_text(
                        json.dumps({"parent_id": "parent-1", "agents": agents}), encoding="utf-8"
                    )
                    # First pass starts the `count` live watchers.
                    self.assertTrue(services.dispatch_notice_tick(force=True))
                    rc = services._dispatch_notice["proc"].wait(timeout=30)
                    self.assertEqual(rc, 0)
                    watcher_pids = [
                        json.loads((state_dir / "paseo-notifications" / f"d-live-{count}-{i}.json").read_text(encoding="utf-8"))["pid"]
                        for i in range(1, count + 1)
                    ]
                    for pid in watcher_pids:
                        hermetic_run._started.add(pid)
                        self.assertTrue(psutil.pid_exists(pid))

                    try:
                        conn_path = state_dir / "connections.jsonl"
                        conns_before = conn_path.read_text(encoding="utf-8").splitlines() if conn_path.is_file() else []
                        log_path = state_dir / "dispatch-notice.log"
                        log_before = log_path.stat().st_size if log_path.is_file() else 0

                        with mock.patch.object(services, "hidden_popen") as popen:
                            self.assertFalse(services.dispatch_notice_needed())
                            self.assertFalse(services.dispatch_notice_tick(force=True))
                            popen.assert_not_called()

                        conns_after = conn_path.read_text(encoding="utf-8").splitlines() if conn_path.is_file() else []
                        non_watch_new = [
                            json.loads(line)
                            for line in conns_after[len(conns_before):]
                            if line.strip() and "--watch" not in json.loads(line).get("argv", [])
                        ]
                        self.assertEqual(non_watch_new, [])
                        self.assertEqual((log_path.stat().st_size if log_path.is_file() else 0) - log_before, 0)
                    finally:
                        for pid in watcher_pids:
                            if psutil.pid_exists(pid):
                                try:
                                    psutil.Process(pid).terminate()
                                    psutil.Process(pid).wait(timeout=5)
                                except psutil.Error:
                                    pass

    def test_stop_dispatch_notice_stops_inflight_bridge_child_without_stopping_watcher(self):
        now = datetime.now(timezone.utc)
        # First row (`d-keep-watch`) observed quickly -> starts a detached `--watch` child.
        keep_row = base_paseo_row("d-keep-watch", state="started", agent_id="w-keep", now=now)
        seed_dispatch_row(self.db_path, keep_row)
        keep_receipt = self.write_dead_watcher_receipt("d-keep-watch", agent_id="w-keep")
        # Second row (`d-slow-obs`) blocks inside `observe` (`node paseo_dispatch_bridge.mjs` without `--watch`).
        slow_row = base_paseo_row("d-slow-obs", state="started", agent_id="w-slow", now=now)
        seed_dispatch_row(self.db_path, slow_row)
        self.write_dead_watcher_receipt("d-slow-obs", agent_id="w-slow")

        self.write_host_state(
            {
                "parent_id": "parent-1",
                "delay_fetch_ids": ["w-slow"],
                "delay_fetch_ms": 15000,
                "agents": {
                    "w-keep": {
                        "id": "w-keep",
                        "status": "running",
                        "provider": "codex",
                        "model": "gpt-6.1-sol",
                        "currentModeId": "full-access",
                        "effectiveThinkingOptionId": "medium",
                        "labels": keep_row["request"]["labels"],
                        "updatedAt": now.isoformat(),
                    },
                    "w-slow": {
                        "id": "w-slow",
                        "status": "running",
                        "provider": "codex",
                        "model": "gpt-6.1-sol",
                        "currentModeId": "full-access",
                        "effectiveThinkingOptionId": "medium",
                        "labels": slow_row["request"]["labels"],
                        "updatedAt": now.isoformat(),
                    },
                },
            }
        )

        self.assertTrue(services.dispatch_notice_tick(force=True))
        pass_proc = services._dispatch_notice["proc"]
        slow_entries = self.wait_until(lambda: self.read_jsonl("slow_bridge.jsonl"), timeout=15.0)
        bridge_pid = slow_entries[0]["pid"]
        hermetic_run._started.add(bridge_pid)
        for desc in psutil.Process(pass_proc.pid).children(recursive=True):
            hermetic_run._started.add(desc.pid)
        watcher_pid = json.loads(keep_receipt.read_text(encoding="utf-8"))["pid"]
        hermetic_run._started.add(watcher_pid)
        self.assertTrue(psutil.pid_exists(bridge_pid))
        self.assertTrue(psutil.pid_exists(watcher_pid))

        try:
            services.stop_dispatch_notice()
            pass_proc.wait(timeout=5)
            self.wait_until(lambda: not psutil.pid_exists(bridge_pid), timeout=3.0)
            time.sleep(0.3)
            self.assertTrue(psutil.pid_exists(watcher_pid), "stopping an in-flight pass must not kill a `--watch` child")
        finally:
            for pid in (bridge_pid, watcher_pid):
                if psutil.pid_exists(pid):
                    try:
                        psutil.Process(pid).terminate()
                        psutil.Process(pid).wait(timeout=5)
                    except psutil.Error:
                        pass


if __name__ == "__main__":
    unittest.main()
