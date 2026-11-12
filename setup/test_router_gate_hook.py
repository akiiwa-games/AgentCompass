import datetime as dt
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import router_gate_hook as gate

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 14, 6, 0, tzinfo=UTC)

SKILL_ROOT = Path(__file__).resolve().parents[1] / "skills/agent-compass"


def setUpModule():
    """Run against this checkout with isolated state, even after another copy was imported."""
    global _state, _environment, _saved_modules, _saved_path, _gate_paths
    _state = tempfile.TemporaryDirectory()
    _saved_path = sys.path[:]
    names = {path.stem for path in (SKILL_ROOT / "scripts").glob("*.py")}
    _saved_modules = {name: sys.modules.pop(name) for name in names if name in sys.modules}
    _environment = mock.patch.dict(os.environ, {
        "ROUTER_SKILL_ROOT": str(SKILL_ROOT), "ROUTER_STATE_DIR": _state.name,
        "HOME": _state.name, "USERPROFILE": _state.name,
    })
    _environment.start()
    for name in ("CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR", "CODEX_HOME"):
        os.environ.pop(name, None)   # restored by _environment.stop(); discovery must not see the host's config homes
    home = Path(_state.name)
    _gate_paths = mock.patch.multiple(gate, HOME=home,
        DEFAULT_LEDGER=home / '.agent-compass/router-ledger.jsonl',
        DEFAULT_USAGE_CACHE=home / '.agent-compass/usage-cache.json',
        DEFAULT_PASEO_AGENTS=home / '.paseo/agents')
    _gate_paths.start()
    sys.path.insert(0, str(SKILL_ROOT / "scripts"))


def tearDownModule():
    for path in (SKILL_ROOT / "scripts").glob("*.py"):
        sys.modules.pop(path.stem, None)
    sys.modules.update(_saved_modules)
    sys.path[:] = _saved_path
    _gate_paths.stop()
    _environment.stop()
    _state.cleanup()


class RouterGateHookTests(unittest.TestCase):
    def test_confirmed_exhaustion_blocks_without_cache_and_recovers(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {'ROUTER_STATE_DIR': directory}):
            exhaustion = gate.router_module('quota_exhaustion')
            exhaustion.record('worker', 'Individual quota reached')
            missing = Path(directory) / 'missing.json'
            self.assertIsNotNone(gate.check_account_quota_exhausted('worker', missing, T0))
            exhaustion.clear('worker')
            self.assertIsNone(gate.check_account_quota_exhausted('worker', missing, T0))

    def setUp(self):
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        self.state = Path(state.name)
        env = mock.patch.dict(os.environ, {
            "ROUTER_GATE_BYPASS": "0",
            "ROUTER_LEDGER": str(self.state / "ledger.jsonl"),
            "ROUTER_USAGE_CACHE": str(self.state / "usage.json"),
            "ROUTER_PASEO_AGENTS_DIR": str(self.state / "agents"),
            "ROUTER_GATE_WINDOW_SECONDS": "900",
            "ROUTER_DISPATCH_DB": str(self.state / "dispatch.sqlite3"),
            "ROUTER_HOST_POLICY": str(self.state / "host-policy.json"),
            "AGENTCOMPASS_ACCOUNTS_OVERRIDE": str(self.state / "accounts-override.json"),
            "ROUTER_ACCOUNTS_FILE": str(Path(__file__).resolve().parents[1] / "skills" / "agent-compass" / "scripts" / "fixtures" / "reference-accounts.json"),
        })
        env.start()
        self.addCleanup(env.stop)
        # The machine's installed-provider discovery would otherwise rewrite each account's verified flag.
        inventory = mock.patch.object(gate.router_module("provider_inventory"), "current", return_value={"accounts": {}})
        inventory.start()
        self.addCleanup(inventory.stop)

    def route(self, provider="codex", model="gpt-6-astra", effort="high", **identity):
        return {"at": (T0 - dt.timedelta(minutes=1)).isoformat(), "stage": "plan",
                "choice": provider + "_choice", "bundle": {
                    "provider": provider, "model": model, "effort": effort}, **identity}

    def test_worker_questions_are_blocked_but_main_questions_and_messages_pass(self):
        worker_id = self.existing_agent()
        agent_file = self.state / 'agents' / 'project' / (worker_id + '.json')
        for labels in ({'paseo.parent-agent-id': 'actual-parent'}, {'router.parent_id': 'actual-parent'}):
            agent_file.write_text(json.dumps({'labels': labels}), encoding='utf-8')
            payloads = (
                {'tool_name': 'functions.request_user_input_async', 'tool_input': {}},
                {'tool_name': 'AskUserQuestion', 'tool_input': {}},
                {'toolName': 'request_user_input_async', 'input': {}},
                {'toolCall': {'name': 'request_user_input', 'args': {}}},
                {'tool_name': 'exec', 'tool_input': 'await tools.request_user_input_async({questions: []})'},
            )
            for payload in payloads if 'paseo.parent-agent-id' in labels else payloads[:1]:
                with self.subTest(labels=labels, payload=payload):
                    with mock.patch.dict(os.environ, {'PASEO_AGENT_ID': worker_id, 'AGENTCOMPASS_WORKER': ''}):
                        denied = gate.evaluate_payload(payload)
                        self.assertIn('actual parent ID', denied['reason'])
                    with mock.patch.dict(os.environ, {'PASEO_AGENT_ID': '', 'AGENTCOMPASS_WORKER': ''}):
                        allowed = gate.evaluate_payload(payload)
                        self.assertNotIn('reason', allowed)
            with mock.patch.dict(os.environ, {'PASEO_AGENT_ID': worker_id}):
                self.assertEqual(gate.evaluate_payload({'tool_name': 'exec', 'tool_input':
                    'text("request_user_input_async({questions: []})")'}), {})
                self.assertEqual(gate.evaluate_payload({'tool_name': 'send_agent_prompt', 'tool_input':
                    {'agentId': 'actual-parent', 'prompt': 'question', 'background': True, 'notifyOnFinish': False}}), {})
        with mock.patch.dict(os.environ, {'PASEO_AGENT_ID': '', 'AGENTCOMPASS_WORKER': '1'}):
            self.assertEqual(gate.evaluate_payload({'tool_name': 'request_user_input_async'})['decision'], 'block')

    def test_parent_continuation_keeps_completion_notifications(self):
        worker_id = self.existing_agent(provider='')
        agent_file = self.state / 'agents' / 'project' / (worker_id + '.json')
        cases = (
            ('router owner', 'parent', {'router.parent_id': 'parent'}, False, True),
            ('paseo owner', 'parent', {'paseo.parent-agent-id': 'parent'}, False, True),
            ('both owners', 'parent', {'router.parent_id': 'parent', 'paseo.parent-agent-id': 'parent'}, False, True),
            ('enabled', 'parent', {'router.parent_id': 'parent'}, True, False),
            ('default', 'parent', {'router.parent_id': 'parent'}, None, False),
            ('other caller', 'peer', {'router.parent_id': 'parent'}, False, False),
            ('unknown caller', '', {'router.parent_id': 'parent'}, False, False),
            ('unknown owner', 'parent', {}, False, False),
            ('conflicting owners', 'parent', {'router.parent_id': 'parent', 'paseo.parent-agent-id': 'other'}, False, False),
            ('self-conflicting owner', 'parent', {'router.parent_id': 'parent', 'paseo.parent-agent-id': worker_id}, False, False),
            ('self', worker_id, {'router.parent_id': worker_id}, False, False),
            ('missing record', 'parent', None, False, False),
            ('unreadable record', 'parent', 'broken json', False, False),
        )
        for label, caller, labels, notify, blocked in cases:
            if labels is None:
                agent_file.unlink(missing_ok=True)
            else:
                agent_file.write_text(json.dumps({'labels': labels}) if isinstance(labels, dict) else labels,
                                      encoding='utf-8', newline='\n')
            args = {'agentId': worker_id, 'prompt': 'continue', 'background': True}
            if notify is not None:
                args['notifyOnFinish'] = notify
            with self.subTest(case=label), \
                    mock.patch.dict(os.environ, {'PASEO_AGENT_ID': caller}), \
                    mock.patch.object(gate, 'load_dispatch_settings', return_value=({}, None)):
                result = gate.evaluate_payload({'tool_name': 'send_agent_prompt', 'tool_input': args})
                self.assertEqual('reason' in result, blocked)
                if blocked:
                    self.assertIn('notifyOnFinish: true or omit it', result['reason'])

        # Formats carry the same semantics; do not multiply the ownership table
        # by each transport representation.
        agent_file.write_text(json.dumps({'labels': {'router.parent_id': 'parent'}}), encoding='utf-8')
        args = {'agentId': worker_id, 'prompt': 'continue', 'notifyOnFinish': False}
        for payload in (
            {'tool_name': 'mcp__paseo__send_agent_prompt', 'tool_input': args},
            {'toolName': 'send_agent_prompt', 'input': args},
            {'toolCall': {'name': 'send_agent_prompt', 'args': args}},
            {'tool_name': 'exec', 'tool_input': 'await tools.mcp__paseo__send_agent_prompt(' + json.dumps(args) + ')'},
        ):
            with self.subTest(format=payload), mock.patch.dict(os.environ, {'PASEO_AGENT_ID': 'parent'}):
                self.assertIn('notifyOnFinish', gate.evaluate_payload(payload)['reason'])


    def exhausted_cache(self, **changes):
        row = {"available": True, "stale": False, "fetched_at": T0.isoformat(),
               "windows": [{"id": "five_hour", "used_pct": 100,
                            "resets_at": (T0 + dt.timedelta(hours=2)).isoformat()}]}
        row.update(changes)
        path = self.state / "usage.json"
        path.write_text(json.dumps({"providers": {"claude-lab": row, "claude": row}}), encoding="utf-8")
        return path

    def existing_agent(self, provider="claude-lab/claude-sonnet-5-5"):
        directory = self.state / "agents" / "project"
        directory.mkdir(parents=True, exist_ok=True)
        agent_id = "11111111-1111-4111-8111-111111111111"
        (directory / (agent_id + ".json")).write_text(json.dumps({"provider": provider}), encoding="utf-8")
        return agent_id

    def write_ledger(self, directory: str, rows: list[dict]) -> Path:
        path = Path(directory) / "router-ledger.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        return path

    def test_non_subagent_tools_always_pass_without_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_ledger = Path(tmp) / "missing.jsonl"
            for payload in (
                {"tool_name": "Read", "tool_input": {"file_path": "a.py"}},
                {"tool_name": "Bash", "tool_input": {"command": 'git commit -m "fix spawn_agent and create_agent()"'}},
                {"tool_name": "exec_command", "tool_input": {"cmd": "rg -n create_agent src/"}},
                {"toolCall": {"name": "view_file", "args": {"AbsolutePath": "E:/a.py"}}},
            ):
                res = gate.evaluate_payload(payload, ledger_path=missing_ledger, now=T0)
                self.assertNotIn("deny", json.dumps(res))
                self.assertNotIn("block", [ k for k, v in res.items() if v is True or v == "block"])

    def test_blocks_unrouted_subagent_tools_across_harnesses(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.jsonl"
            # Claude Agent tool
            claude_res = gate.evaluate_payload(
                {"tool_name": "Agent", "tool_input": {"subagent_type": "Explore", "prompt": "find files"}},
                ledger_path=missing,
                now=T0,
            )
            self.assertEqual(claude_res["decision"], "block")
            self.assertEqual(claude_res["hookSpecificOutput"]["permissionDecision"], "deny")

            # Codex spawn_agent tool
            codex_res = gate.evaluate_payload(
                {"tool_name": "spawn_agent", "tool_input": {"message": "do work"}},
                ledger_path=missing,
                now=T0,
            )
            self.assertEqual(codex_res["decision"], "block")

            # Antigravity invoke_subagent tool
            agy_res = gate.evaluate_payload(
                {"toolCall": {"name": "invoke_subagent", "args": {"Subagents": [{"TypeName": "research"}]}}},
                ledger_path=missing,
                now=T0,
            )
            self.assertEqual(agy_res["decision"], "deny")
            self.assertIn("Router Gate", agy_res["reason"])

            # Paseo codemode create_agent (Claude & Pi 'codemode' tool name)
            paseo_res = gate.evaluate_payload(
                {"tool_name": "mcp__paseo__execute_code", "tool_input": {"code": "await create_agent({provider: 'codex'})"}},
                ledger_path=missing,
                now=T0,
            )
            self.assertEqual(paseo_res["decision"], "block")

            pi_codemode_res = gate.evaluate_payload(
                {"toolName": "codemode", "input": {"code": "await tools.mcp__paseo__create_agent({provider: 'codex'})"}},
                ledger_path=missing,
                now=T0,
            )
            self.assertTrue(pi_codemode_res["block"])

            # Codex unified exec string input
            codex_exec_res = gate.evaluate_payload(
                {"tool_name": "exec", "tool_input": 'text(await tools.exec_command({cmd:"codex exec -m gpt-6-astra \'task\'"}))'},
                ledger_path=missing,
                now=T0,
            )
            self.assertEqual(codex_exec_res["decision"], "block")

            # Shell worker spawns (paseo agent create, codex exec, run_dot_task.py)
            for cmd in (
                "paseo agent create --provider codex/gpt-6-astra",
                "codex exec -m gpt-6-astra 'task'",
                "python ~/.codex/skills/dot-subagent/scripts/run_dot_task.py --prompt 'hi'",
            ):
                shell_res = gate.evaluate_payload(
                    {"tool_name": "Bash", "tool_input": {"command": cmd}},
                    ledger_path=missing,
                    now=T0,
                )
                self.assertEqual(shell_res["decision"], "block", msg=cmd)

    def test_orca_and_t3_worker_launches_are_gated_but_their_other_commands_are_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing-ledger.jsonl"

            def decide(tool, tool_input):
                return gate.evaluate_payload({"tool_name": tool, "tool_input": tool_input}, ledger_path=missing, now=T0).get("decision", "allow")

            for cmd in (
                'orca orchestration worker-start --spec "task" --worktree current --agent codex --json',
                "orca.cmd agent launch --agent claude --prompt hi",
                r"C:\Users\me\.orca\bin\orca.exe orchestration worker-start --task t1 --agent claude",
            ):
                self.assertEqual(decide("Bash", {"command": cmd}), "block", msg=cmd)
            for cmd in (
                "orca status --json",
                "orca worktree list --json",
                "orca orchestration check --wait --json",
                "orca orchestration worker-start --help",
            ):
                self.assertNotEqual(decide("Bash", {"command": cmd}), "block", msg=cmd)
            for tool in ("delegate_task", "mcp__t3-code__delegate_task"):
                self.assertEqual(decide(tool, {"task": "do it"}), "block", msg=tool)
            self.assertNotEqual(decide("mcp__t3-code__task_status", {"taskId": "t"}), "block")

    def test_recent_plan_is_found_behind_snapshot_sized_lines(self):
        """Plan lines carry a decision snapshot (up to ~24 KB); the tail read must still reach the route behind them."""
        with tempfile.TemporaryDirectory() as tmp:
            heavy = {"at": (T0 - dt.timedelta(minutes=1)).isoformat(), "stage": "snapshot-only", "pad": "x" * 24000}
            route = {"at": (T0 - dt.timedelta(minutes=4)).isoformat(), "stage": "plan", "choice": "sol_high"}
            ledger = self.write_ledger(tmp, [route] + [dict(heavy, n=i) for i in range(30)])
            self.assertGreater(ledger.stat().st_size, 64 * 1024 * 8)
            routes = gate.recent_routes(ledger, now=T0)
            self.assertTrue(any(r.get("choice") == route["choice"] for r in routes))

    def test_allows_subagent_when_recent_plan_select_or_record_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self.write_ledger(
                tmp,
                [
                    {
                        "at": (T0 - dt.timedelta(minutes=4)).isoformat(),
                        "stage": "plan",
                        "choice": "sol_high",
                        "source": "deterministic_allocator",
                    }
                ],
            )
            res = gate.evaluate_payload(
                {"tool_name": "Agent", "tool_input": {"subagent_type": "Explore", "prompt": "find files"}},
                ledger_path=ledger,
                now=T0,
            )
            self.assertEqual(res, {})

            agy_res = gate.evaluate_payload(
                {"toolCall": {"name": "invoke_subagent", "args": {"Subagents": []}}},
                ledger_path=ledger,
                now=T0,
            )
            self.assertEqual(agy_res, {"decision": "allow"})

    def test_expired_or_self_only_ledger_entry_still_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self.write_ledger(
                tmp,
                [
                    {
                        "at": (T0 - dt.timedelta(minutes=30)).isoformat(),
                        "stage": "plan",
                        "choice": "sol_high",
                    },
                    {
                        "at": (T0 - dt.timedelta(minutes=1)).isoformat(),
                        "stage": "delegate",
                        "choice": "self",
                    },
                ],
            )
            res = gate.evaluate_payload(
                {"tool_name": "spawn_agent", "tool_input": {"message": "hi"}},
                ledger_path=ledger,
                now=T0,
            )
            self.assertEqual(res["decision"], "block")

    def test_dot_status_and_doctor_pass_without_ledger_and_dot_route_blocks_local_subagent(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.jsonl"
            for cmd in (
                "python ~/.codex/skills/dot-subagent/scripts/run_dot_task.py doctor",
                "python ~/.codex/skills/dot-subagent/scripts/run_dot_task.py status --task-id task_123",
                "python ~/.codex/skills/dot-subagent/scripts/run_dot_task.py run --task-id task_123 --wait 60",
            ):
                res = gate.evaluate_payload(
                    {"tool_name": "Bash", "tool_input": {"command": cmd}},
                    ledger_path=missing,
                    now=T0,
                )
                self.assertEqual(res, {}, msg=cmd)

            dot_ledger = self.write_ledger(
                tmp,
                [
                    {
                        "at": (T0 - dt.timedelta(minutes=2)).isoformat(),
                        "stage": "plan",
                        "choice": "dot_text",
                        "bundle": {"provider": "dot", "account": "dot", "model": "gpt-6-astra", "bundle_id": "dot_text"},
                    }
                ],
            )
            # Allowing run_dot_task.py when dot_text is selected
            ok_dot = gate.evaluate_payload(
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "python ~/.codex/skills/dot-subagent/scripts/run_dot_task.py run --prompt-file task.md"},
                },
                ledger_path=dot_ledger,
                now=T0,
            )
            self.assertEqual(ok_dot, {})

            # Blocking local subagent bypass when dot_text is selected
            bypass_local = gate.evaluate_payload(
                {"tool_name": "Agent", "tool_input": {"subagent_type": "general-purpose", "prompt": "do it locally"}},
                ledger_path=dot_ledger,
                now=T0,
            )
            self.assertEqual(bypass_local["decision"], "block")
            self.assertIn("dot_text", bypass_local["reason"])

    def test_claude_2_quota_exhaustion_and_route_mismatch_are_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "usage-cache.json"
            paseo_dir = Path(tmp) / "paseo_agents"
            proj_dir = paseo_dir / "E-PROJECTS-demo"
            proj_dir.mkdir(parents=True)
            # claude-lab's five_hour declares a 25% launch floor; claude-magpie is not configured, so only a
            # used-up window refuses it (no account name implies a floor).
            for account_name, choice_name, agent_uuid, used in (
                ("claude-lab", "lab_sonnet55_high", "11111111-1111-4111-8111-111111111111", 85.0),
                ("claude-magpie", "sonnet55_high_claude_magpie", "22222222-2222-4222-8222-222222222222", 100.0),
            ):
                cache.write_text(
                    json.dumps(
                        {
                            "providers": {
                                account_name: {
                                    "available": True,
                                    "stale": False,
                                    "fetched_at": (T0 - dt.timedelta(minutes=1)).isoformat(),
                                    "windows": [
                                        {
                                            "id": "five_hour",
                                            "used_pct": used,
                                            "resets_at": (T0 + dt.timedelta(hours=2)).isoformat(),
                                        }
                                    ],
                                }
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                (proj_dir / f"{agent_uuid}.json").write_text(
                    json.dumps({"id": agent_uuid, "provider": account_name}),
                    encoding="utf-8",
                )
                claude_ledger = self.write_ledger(
                    tmp,
                    [
                        {
                            "at": (T0 - dt.timedelta(minutes=1)).isoformat(),
                            "stage": "plan",
                            "choice": choice_name,
                            "bundle": {"provider": account_name, "account": account_name, "model": "claude-sonnet-5-5"},
                        }
                    ],
                )
                # 1. Blocks new secondary Claude create_agent / CLI when five_hour is below its floor even if ledger matches
                res_create = gate.evaluate_payload(
                    {"tool_name": "mcp__paseo__create_agent", "tool_input": {"provider": account_name, "prompt": "work"}},
                    ledger_path=claude_ledger,
                    usage_cache_path=cache,
                    paseo_agents_dir=paseo_dir,
                    now=T0,
                )
                self.assertEqual(res_create["decision"], "block")
                self.assertIn("five_hour", res_create["reason"])

                res_cli = gate.evaluate_payload(
                    {"tool_name": "Bash", "tool_input": {"command": f"CLAUDE_CONFIG_DIR=~/.{account_name} paseo agent create --prompt 'work'"}},
                    ledger_path=claude_ledger,
                    usage_cache_path=cache,
                    paseo_agents_dir=paseo_dir,
                    now=T0,
                )
                self.assertEqual(res_cli["decision"], "block")
                self.assertIn(account_name, res_cli["reason"])

                # 2. Blocks run_turn / send_message to an existing secondary Claude Paseo agent the same way
                res_turn = gate.evaluate_payload(
                    {
                        "toolName": "codemode",
                        "input": {"code": f'await paseo.run_turn({{agentId: "{agent_uuid}", prompt: "continue"}})'},
                    },
                    ledger_path=claude_ledger,
                    usage_cache_path=cache,
                    paseo_agents_dir=paseo_dir,
                    now=T0,
                )
                self.assertTrue(res_turn["block"])
                self.assertIn("five_hour", res_turn["reason"])

                # 3. Blocks secondary Claude create_agent when quota is healthy (20% used) but recent ledger picked pi_astra_high
                cache.write_text(
                    json.dumps(
                        {
                            "providers": {
                                account_name: {
                                    "available": True,
                                    "stale": False,
                                    "fetched_at": (T0 - dt.timedelta(minutes=1)).isoformat(),
                                    "windows": [
                                        {
                                            "id": "five_hour",
                                            "used_pct": 20.0,
                                            "resets_at": (T0 + dt.timedelta(hours=2)).isoformat(),
                                        }
                                    ],
                                }
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                other_ledger = self.write_ledger(
                    tmp,
                    [
                        {
                            "at": (T0 - dt.timedelta(minutes=1)).isoformat(),
                            "stage": "plan",
                            "choice": "pi_astra_high",
                            "bundle": {"provider": "pi", "account": "codex", "model": "gpt-6-astra"},
                        }
                    ],
                )
                res_mismatch = gate.evaluate_payload(
                    {
                        "tool_name": "Agent",
                        "tool_input": {"subagent_type": "general-purpose", "prompt": "work", "model": "sonnet"},
                    },
                    ledger_path=other_ledger,
                    usage_cache_path=cache,
                    paseo_agents_dir=paseo_dir,
                    now=T0,
                )
                self.assertEqual(res_mismatch["decision"], "block")
            self.assertIn("pi_astra_high", res_mismatch["reason"])

    def test_current_paseo_create_contract_checks_quota_and_normalizes_pair(self):
        self.exhausted_cache()
        for provider in ("claude-lab", "claude-lab/claude-sonnet-5-5"):
            payload = {"tool_name": "mcp__paseo__create_agent", "tool_input": {
                "provider": provider, "initialPrompt": "work", "settings": {"thinkingOptionId": "high"}}}
            result = gate.evaluate_payload(payload, now=T0)
            self.assertIn("five_hour", result["reason"])
        self.assertEqual(gate.normalize_launch({"provider": "codex/gpt-6-astra",
                                               "settings": {"thinkingOptionId": "high"}}),
                         {"provider": "codex", "model": "gpt-6-astra", "effort": "high"})

    def test_current_send_agent_prompt_direct_and_native_wrappers(self):
        self.exhausted_cache()
        agent_id = self.existing_agent()
        for name in ("send_agent_prompt", "mcp__paseo__send_agent_prompt", "run_turn", "send_message"):
            with self.subTest(name=name):
                result = gate.evaluate_payload({"tool_name": name, "tool_input": {
                    "agentId": agent_id, "prompt": "continue"}}, now=T0)
                self.assertIn("five_hour", result["reason"])
        code = f'await tools.mcp__paseo__send_agent_prompt({{agentId: "{agent_id}", prompt: "continue"}})'
        for payload in (
            {"tool_name": "exec", "tool_input": code},
            {"toolName": "codemode", "input": {"code": code}},
            {"toolCall": {"name": "execute_code", "args": {"code": code}}},
            {"tool_name": "exec_command", "tool_input": {"cmd": f'paseo agent send {agent_id} continue'}},
        ):
            self.assertIn("five_hour", gate.evaluate_payload(payload, now=T0)["reason"])
        self.exhausted_cache(stale=True)
        self.assertIn("five_hour", gate.evaluate_payload({"tool_name": "send_agent_prompt", "tool_input": {
            "agentId": agent_id, "prompt": "continue"}}, now=T0)["reason"])

    def test_pi_launch_namespace_matches_canonical_ledger_on_schedule_and_host_launch(self):
        # Actual ledger records retain canonical model, not bundle.launch_model.
        for prov, canonical_model, launch_model in (
            ("pi", "gpt-6-astra", "openai-codex/gpt-6-astra"),
            ("opencode", "deepseek-v4.1-flash-free", "opencode/deepseek-v4.1-flash-free"),
        ):
            ledger = self.write_ledger(str(self.state), [self.route(prov, canonical_model, "high", session_ids=["s1"])])
            for args in (
                {"provider": f"{prov}/{launch_model}"},
                {"provider": prov, "model": launch_model},
            ):
                args["settings"] = {"thinkingOptionId": "high"}
                host_launch = {"tool_name": "delegate_task", "tool_input": args}
                schedule = {"tool_name": "mcp__paseo__create_schedule", "session_id": "s1",
                            "tool_input": dict(args, prompt="work", cron="7 * * * *")}
                self.assertEqual(gate.evaluate_payload(host_launch, ledger_path=ledger, now=T0), {})
                self.assertEqual(gate.evaluate_payload(schedule, ledger_path=ledger, now=T0), {})

    def test_pi_namespace_normalization_preserves_owner_model_and_effort_checks(self):
        ledger = self.write_ledger(str(self.state), [self.route("pi")])
        for provider, effort, diagnostic in (
            ("pi/openai-codex/gpt-6.1-sol", "high", "model"),
            ("pi/openai-codex/gpt-6-astra", "max", "effort"),
            ("codex/openai-codex/gpt-6-astra", "high", "provider"),
            ("pi/other-backend/gpt-6-astra", "high", "model"),
        ):
            args = {"provider": provider, "settings": {"thinkingOptionId": effort}}
            result = gate.evaluate_payload({"tool_name": "delegate_task", "tool_input": args}, ledger_path=ledger, now=T0)
            self.assertIn(diagnostic, result["reason"])

    def test_antigravity_high_model_accepts_omitted_thinking_option(self):
        for provider in ("antigravity", "antigravity-resilient"):
            for model in ("gemini-4-argon-high", "gemini-3.8-flash-high"):
                ledger = self.write_ledger(str(self.state), [self.route(provider, model, "high", session_ids=["s1"])])
                args = {"provider": provider + "/" + model, "settings": {"modeId": "full-access"}}
                self.assertEqual(gate.evaluate_payload({"tool_name": "delegate_task", "tool_input": args},
                                                      ledger_path=ledger, now=T0), {})
                self.assertEqual(gate.evaluate_payload({"tool_name": "mcp__paseo__create_schedule", "session_id": "s1",
                    "tool_input": dict(args, prompt="work", cron="7 * * * *")},
                    ledger_path=ledger, now=T0), {})

    def test_quota_never_blocks_inspection_even_with_provider_or_agent_id(self):
        self.exhausted_cache()
        agent_id = self.existing_agent()
        for name in ("list_models", "inspect_provider", "get_agent_status", "mcp__paseo__list_models", "update_agent"):
            self.assertEqual(gate.evaluate_payload({"tool_name": name, "tool_input": {
                "provider": "claude-lab/claude-sonnet-5-5", "agentId": agent_id}}, now=T0), {})
        code = f'await paseo.get_agent_status({{agentId:"{agent_id}"}}); await paseo.list_models({{provider:"claude-lab"}})'
        self.assertEqual(gate.evaluate_payload({"toolName": "codemode", "input": {"code": code}}, now=T0), {"block": False})

    def test_full_window_blocks_until_reset_unless_reading_is_from_a_previous_cycle(self):
        # The fixture's five_hour window resets at T0+2h, so its current cycle began at T0-3h.
        week = {"id": "seven_day", "used_pct": 100, "resets_at": (T0 + dt.timedelta(days=1)).isoformat()}
        for changes, blocked in (
                ({}, True),
                ({"stale": True}, True),
                ({"fetched_at": (T0 - dt.timedelta(hours=1)).isoformat()}, True),
                ({"stale": True, "fetched_at": (T0 - dt.timedelta(hours=1)).isoformat()}, True),
                ({"windows": [week], "fetched_at": (T0 - dt.timedelta(days=5)).isoformat()}, True),
                ({"fetched_at": (T0 - dt.timedelta(hours=4)).isoformat()}, False),
                ({"windows": [week], "fetched_at": (T0 - dt.timedelta(days=7)).isoformat()}, False),
                ({"windows": [{"id": "five_hour", "used_pct": 100, "resets_at": T0.isoformat()}]}, False),
                ({"fetched_at": None}, False), ({"fetched_at": "bad"}, False),
                ({"fetched_at": (T0 + dt.timedelta(hours=1)).isoformat()}, False),
                ({"available": False}, False)):
            with self.subTest(changes=changes):
                cache = self.exhausted_cache(**changes)
                self.assertEqual(gate.check_account_quota_exhausted("claude-lab", cache, T0) is not None, blocked)
        # Dict-shaped window specs with custom launch_min_remaining_pct apply across non-Claude accounts too.
        custom_accounts = {"accounts": {"team-proxy": {"windows": {"spend": {"hours": 168, "launch_min_remaining_pct": 15}}}}}
        cache = self.state / "usage.json"
        for used_pct, blocked in ((86.0, True), (80.0, False)):
            cache.write_text(json.dumps({"providers": {"team-proxy": {
                "available": True, "stale": False, "fetched_at": T0.isoformat(),
                "windows": [{"id": "spend", "used_pct": used_pct, "resets_at": (T0 + dt.timedelta(days=2)).isoformat()}],
            }}}), encoding="utf-8")
            self.assertEqual(gate.check_account_quota_exhausted("team-proxy", cache, T0, accounts=custom_accounts) is not None, blocked)

    def write_settings(self, allowed=None, accounts=None):
        policy = self.state / "host-policy.json"
        if allowed is None:
            policy.unlink(missing_ok=True)
        else:
            policy.write_text(json.dumps({"allowed_accounts": allowed}), encoding="utf-8")
        (self.state / "accounts-override.json").write_text(
            json.dumps({"schema_version": 2, "accounts": accounts or {}}), encoding="utf-8"
        )

    def test_disabled_account_or_bundle_blocks_launch_and_resume_but_not_reports(self):
        names = ("sonnet", "opus", "bare", "codex", "child")
        ids = {name: f"{i:08d}-0000-4000-8000-000000000000" for i, name in enumerate(names, 1)}
        directory = self.state / "agents" / "project"
        directory.mkdir(parents=True, exist_ok=True)
        for name, doc in zip(names, (
                {"provider": "claude-lab", "config": {"model": "claude-sonnet-5-5", "thinkingOptionId": "high"},
                 "labels": {"router.account": "claude-lab", "router.bundle_id": "lab_sonnet55_high",
                            "router.parent_id": "actual-parent"}},
                {"provider": "claude-lab", "labels": {"router.account": "claude-lab", "router.bundle_id": "lab_opus55_high"}},
                {"provider": "claude-lab/claude-sonnet-5-5"},
                {"provider": "codex", "config": {"model": "gpt-6-astra"}},
                {"provider": "claude-lab", "labels": {"paseo.parent-agent-id": ids["sonnet"]}})):
            (directory / (ids[name] + ".json")).write_text(json.dumps(doc), encoding="utf-8")
        ledger = self.write_ledger(str(self.state), [self.route("claude-lab", "claude-sonnet-5-5", "high"),
                                                     self.route("claude-lab", "claude-opus-5-5", "high")])

        def turn(agent, tool="send_agent_prompt", **extra):
            return {"tool_name": tool, "tool_input": {"agentId": ids[agent], "prompt": "continue"}, **extra}

        def launch(model):
            return {"tool_name": "create_agent", "tool_input": {
                "provider": f"claude-lab/claude-{model}-5-5", "settings": {"thinkingOptionId": "high"},
                "labels": {"router.account": "claude-lab", "router.bundle_id": f"lab_{model}55_high",
                           "router.dispatch_id": f"d-{model}"}}}

        resume_sonnet = (
            turn("sonnet"), turn("sonnet", "mcp__paseo__run_turn"), turn("sonnet", "send_message"),
            {"toolName": "codemode", "input": {"code":
                f'await tools.mcp__paseo__send_agent_prompt({{agentId: "{ids["sonnet"]}", prompt: "continue"}})'}})
        bound_sonnet = {"tool_name": "mcp__paseo__create_agent", "tool_input": {"provider": "claude-lab/claude-sonnet-5-5",
            "labels": {"router.account": "claude-lab", "router.bundle_id": "lab_sonnet55_high"}}}
        shell_launch = {"tool_name": "Bash", "tool_input": {"command": "paseo agent create --provider claude-lab/claude-sonnet-5-5"}}
        everyone = ["claude", "claude-lab", "codex", "antigravity", "dot"]
        with mock.patch.object(gate.router_module("dispatch_registry"), "claim", return_value=None):
            # (settings, cause in the block reason, blocked calls, calls differing only in the judged condition)
            for settings, cause, blocked, passed in (
                    ({"allowed": [a for a in everyone if a != "claude-lab"]}, "host-policy.json",
                     resume_sonnet + (turn("bare"), launch("sonnet"), bound_sonnet, shell_launch),
                     (turn("codex"), turn("sonnet", parent_agent_id=ids["child"]))),
                    ({"accounts": {"claude-lab": {"bundles": {"lab_sonnet55_high": {"disabled": True},
                                                            "lab_sonnet55_medium": {"disabled": True},
                                                            "lab_opus55_high": {"disabled": False}}}}}, "档位 lab_sonnet55_high",
                     resume_sonnet + (turn("bare"), launch("sonnet"), bound_sonnet), (turn("opus"), launch("opus"))),
                    ({"accounts": {"claude-lab": {"verified": False}}}, "verified: false",
                     (turn("bare"), launch("sonnet")), (turn("codex"),))):
                self.write_settings(**settings)
                for payload in blocked:
                    with self.subTest(cause=cause, blocked=payload):
                        result = gate.evaluate_payload(payload, ledger_path=ledger, now=T0)
                        self.assertIn(cause, result["reason"])
                        self.assertIn("roll --brief", result["reason"])
                for payload in passed:
                    with self.subTest(cause=cause, passed=payload):
                        self.assertNotIn("reason", gate.evaluate_payload(payload, ledger_path=ledger, now=T0))
            # Turning the account back on, or having no host policy, lets the same calls through.
            for settings in ({"allowed": everyone}, {}):
                self.write_settings(**settings)
                for payload in resume_sonnet + (turn("bare"), launch("sonnet")):
                    with self.subTest(settings=settings, payload=payload):
                        self.assertNotIn("reason", gate.evaluate_payload(payload, ledger_path=ledger, now=T0))
        # A bound launch then reaches its dispatch claim instead of the disabled-target block.
        self.assertIn("Bound dispatch rejected", gate.evaluate_payload(bound_sonnet, now=T0)["reason"])
        # Degrade: an unreadable host policy allows rather than blocking normal work.
        (self.state / "host-policy.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(gate.evaluate_payload(turn("sonnet"), now=T0), {})

    def test_running_owner_control_exempts_disabled_and_context_rotation_gates(self):
        wid = self.existing_agent()
        agent_file = self.state / 'agents' / 'project' / (wid + '.json')
        owner = {'router.parent_id': 'parent'}
        cases = (
            ('router owner', 'parent', owner, {'lastStatus': 'running'}, False),
            ('paseo owner', 'parent', {'paseo.parent-agent-id': 'parent'}, {'lastStatus': 'running'}, False),
            ('both parents', 'parent', {**owner, 'paseo.parent-agent-id': 'parent'}, {'lastStatus': 'running'}, False),
            ('current status', 'parent', owner, {'status': 'running'}, False),
            ('idle', 'parent', owner, {'lastStatus': 'idle'}, True),
            ('permission wait', 'parent', owner, {'lastStatus': 'waiting'}, True),
            ('unknown status', 'parent', owner, {}, True),
            ('current idle wins', 'parent', owner, {'lastStatus': 'running', 'status': 'idle'}, True),
            ('current unknown wins', 'parent', owner, {'lastStatus': 'running', 'status': None}, True),
            ('activeTurn alone', 'parent', owner, {'activeTurn': {'turnId': 't'}}, True),
            ('idle activeTurn', 'parent', owner, {'status': 'idle', 'activeTurn': {'turnId': 't'}}, True),
            ('nonowner', 'peer', owner, {'lastStatus': 'running'}, True),
            ('unknown caller', '', owner, {'lastStatus': 'running'}, True),
            ('unknown owner', 'parent', {}, {'lastStatus': 'running'}, True),
            ('conflicting parents', 'parent', {**owner, 'paseo.parent-agent-id': 'peer'}, {'lastStatus': 'running'}, True),
            ('self', wid, {'router.parent_id': wid}, {'lastStatus': 'running'}, True),
        )
        worker_sessions = gate.router_module('worker_sessions')
        for settings, tokens, cause in (
                ({'allowed': []}, 0, 'host-policy.json'),
                ({'accounts': {'claude-lab': {'verified': False}}}, 0, 'verified: false'),
                ({'accounts': {'claude-lab': {'bundles': {'lab_sonnet55_high': {'disabled': True}}}}}, 0, '档位 lab_sonnet55_high'),
                ({}, 2_000_000, 'historical_context_tokens=2,000,000/2,000,000'),
                ({}, 0, 'five_hour')):
            self.write_settings(**settings)
            if cause == 'five_hour':
                self.exhausted_cache()
            else:
                (self.state / 'usage.json').unlink(missing_ok=True)
            with mock.patch.object(worker_sessions, 'session_state', return_value={'context_tokens': tokens}):
                for label, caller, labels, status, blocked in cases:
                    doc = {'provider': 'claude-lab/claude-sonnet-5-5',
                           'labels': {**labels, 'router.account': 'claude-lab', 'router.bundle_id': 'lab_sonnet55_high'},
                           **status}
                    agent_file.write_text(json.dumps(doc), encoding='utf-8')
                    # Wrap-up text is deliberately identical for idle and running targets.
                    args = {'agentId': wid, 'prompt': 'commit, write handoff, report', 'notifyOnFinish': True}
                    payloads = [
                        {'tool_name': tool, 'tool_input': args}
                        for tool in ('send_agent_prompt', 'mcp__paseo__run_turn', 'send_message')]
                    payloads.extend((
                        {'toolName': 'send_agent_prompt', 'input': args},
                        {'toolCall': {'name': 'send_agent_prompt', 'args': args}},
                        {'tool_name': 'exec', 'tool_input': 'await tools.mcp__paseo__send_agent_prompt(' + json.dumps(args) + ')'},
                        {'toolName': 'codemode', 'input': {'code':
                            f'const id = "{wid}"; await paseo.send_agent_prompt({{agentId: id, prompt: "wrap up"}})'}},
                    ))
                    script = ("import subprocess; paseo = r'C:\\Program Files\\Paseo\\paseo.cmd'; "
                              f"target = '{wid}'; args = [paseo, 'agent', 'send', target, '--prompt', 'wrap up']; "
                              "subprocess.run(args)")
                    commands = (
                        f'paseo agent send {wid} --prompt "wrap up"',
                        f'paseo send {wid} "wrap up"',
                        f'& "C:\\Program Files\\Paseo\\paseo.cmd" agent send {wid} --prompt "wrap up"',
                        f'python -X utf8 -c "{script}"',
                        script,
                        f"python -c \"import subprocess; subprocess.run(['paseo', 'send', '{wid}', 'wrap up'])\"",
                    )
                    payloads.extend({'toolCall': {'name': 'run_command', 'args': {'CommandLine': command}}}
                                    for command in commands)
                    payloads.append({'tool_name': 'exec', 'tool_input':
                        'await tools.exec_command(' + json.dumps({'cmd': commands[0]}) + ')'})
                    for payload in payloads:
                        with self.subTest(gate=cause, case=label, payload=payload), \
                                mock.patch.dict(os.environ, {'PASEO_AGENT_ID': caller}):
                            result = gate.evaluate_payload(payload, now=T0)
                            self.assertEqual('reason' in result, blocked)
                            if blocked:
                                self.assertIn(cause, result['reason'])
                                self.assertIn('cancel_agent', result['reason'])
                                self.assertIn('roll --brief', result['reason'])

    def test_cli_inspection_cancel_and_quoted_examples_are_not_continuations(self):
        wid = self.existing_agent()
        self.write_settings(allowed=[])
        commands = [f'paseo agent {verb} {wid}' for verb in ('status', 'activity', 'list', 'cancel', 'stop')]
        commands += [f'paseo agent send {wid} --help', f'echo "paseo agent send {wid}"',
                     f'paseo agent status {wid} --label "paseo send {wid}"',
                     f"python -c \"print('paseo agent send {wid}')\"",
                     f"python -c \"import subprocess; subprocess.run(['paseo', 'agent', 'status', '{wid}'])\""]
        for command in commands:
            with self.subTest(command=command):
                self.assertNotIn('reason', gate.evaluate_payload({
                    'tool_name': 'exec_command', 'tool_input': {'cmd': command}}, now=T0))

    def test_running_control_keeps_launch_quota_and_notification_checks(self):
        wid = self.existing_agent()
        labels = {'router.parent_id': 'parent', 'router.account': 'claude-lab', 'router.bundle_id': 'lab_sonnet55_high'}
        (self.state / 'agents' / 'project' / (wid + '.json')).write_text(json.dumps({
            'provider': 'claude-lab/claude-sonnet-5-5', 'labels': labels, 'lastStatus': 'running'}), encoding='utf-8')
        self.write_settings(allowed=[])
        with mock.patch.dict(os.environ, {'PASEO_AGENT_ID': 'parent'}):
            # Spoofing running state in a launch request cannot bypass account disablement or quota.
            result = gate.evaluate_payload({'tool_name': 'create_agent', 'tool_input': {
                'provider': 'claude-lab/claude-sonnet-5-5', 'labels': labels, 'lastStatus': 'running'}}, now=T0)
            self.assertIn('host-policy.json', result['reason'])
            self.write_settings()
            self.exhausted_cache()
            result = gate.evaluate_payload({'tool_name': 'create_agent', 'tool_input': {
                'provider': 'claude-lab/claude-sonnet-5-5', 'labels': labels, 'lastStatus': 'running'}}, now=T0)
            self.assertIn('five_hour', result['reason'])
            # notifyOnFinish: false is still blocked even on a running owned worker.
            result = gate.evaluate_payload({'tool_name': 'send_agent_prompt', 'tool_input': {
                'agentId': wid, 'prompt': 'wrap up', 'notifyOnFinish': False}}, now=T0)
            self.assertIn('notifyOnFinish: true or omit it', result['reason'])
            # Control message with completion notifications enabled skips quota on a running owned worker,
            # including when the account's confirmed-exhaustion latch is set.
            exhaustion = gate.router_module('quota_exhaustion')
            exhaustion.record('claude-lab', 'Individual quota reached', windows=['five_hour'])
            self.addCleanup(exhaustion.clear, 'claude-lab')
            result = gate.evaluate_payload({'tool_name': 'send_agent_prompt', 'tool_input': {
                'agentId': wid, 'prompt': 'wrap up', 'notifyOnFinish': True}}, now=T0)
            self.assertNotIn('reason', result)
            # Idle, unknown-state, or non-owned workers remain blocked by quota on the same message.
            for caller, state in (
                ('parent', {'lastStatus': 'idle'}),
                ('parent', {}),
                ('other', {'lastStatus': 'running'}),
            ):
                (self.state / 'agents' / 'project' / (wid + '.json')).write_text(json.dumps({
                    'provider': 'claude-lab/claude-sonnet-5-5', 'labels': labels, **state}), encoding='utf-8')
                with mock.patch.dict(os.environ, {'PASEO_AGENT_ID': caller}):
                    result = gate.evaluate_payload({'tool_name': 'send_agent_prompt', 'tool_input': {
                        'agentId': wid, 'prompt': 'wrap up', 'notifyOnFinish': True}}, now=T0)
                self.assertIn('reason', result)
                self.assertIn('cancel_agent', result['reason'])
                self.assertIn('roll --brief', result['reason'])

    def quota_cache(self, rows):
        path = self.state / "usage.json"
        path.write_text(json.dumps({"providers": {name: {"available": True, "stale": False, "fetched_at": T0.isoformat(),
                                                         **row} for name, row in rows.items()}}), encoding="utf-8")
        return path

    @staticmethod
    def window(wid, used, hours=2.0, **extra):
        return {"id": wid, "used_pct": used, "resets_at": (T0 + dt.timedelta(hours=hours)).isoformat(), **extra}

    def owned_worker(self, account, bundle_id, provider, status="running"):
        directory = self.state / "agents" / "project"
        directory.mkdir(parents=True, exist_ok=True)
        agent_id = "33333333-3333-4333-8333-333333333333"
        doc = {
            "provider": provider,
            "labels": {
                "router.parent_id": "parent", "paseo.parent-agent-id": "parent",
                "router.account": account, "router.bundle_id": bundle_id,
            },
        }
        if status is not None:
            doc["lastStatus"] = status
        (directory / (agent_id + ".json")).write_text(json.dumps(doc), encoding="utf-8")
        return agent_id

    def test_antigravity_third_party_exhaustion_blocks_only_models_that_draw_it(self):
        # The 2026-11-04 reading: 3p-weekly used up, the Gemini pools have room.
        self.quota_cache({"antigravity": {"windows": [
            self.window("gemini-weekly", 28.4, 67), self.window("gemini-5h", 7.3, 4),
            self.window("3p-weekly", 99.26, 7), self.window("3p-5h", 0, 5)]}})
        for bundle_id, blocked in (("argon_high", False), ("flash_high", False),
                                   ("agy_opus55_low", True), ("agy_sonnet55_high", True)):
            agent_id = self.owned_worker("antigravity", bundle_id, "antigravity-resilient")
            for caller, status, is_control in (
                ("parent", "running", True),
                ("parent", "idle", False),
                ("parent", None, False),
                ("other", "running", False),
            ):
                self.owned_worker("antigravity", bundle_id, "antigravity-resilient", status)
                expect_blocked = False if is_control else blocked
                with self.subTest(bundle=bundle_id, caller=caller, status=status), \
                        mock.patch.dict(os.environ, {"PASEO_AGENT_ID": caller}):
                    result = gate.evaluate_payload({"tool_name": "send_agent_prompt", "tool_input": {
                        "agentId": agent_id, "prompt": "wrap up"}}, now=T0)
                    self.assertEqual("reason" in result, expect_blocked, result)
                    if expect_blocked:
                        self.assertIn("3p-weekly", result["reason"])
            ledger = self.write_ledger(str(self.state), [self.route("antigravity-resilient", "gemini-4-argon-high")])
            launch = {"tool_name": "create_agent", "tool_input": {
                "provider": "antigravity-resilient/gemini-4-argon-high", "settings": {"modeId": "full-access"},
                "labels": {"router.account": "antigravity", "router.bundle_id": bundle_id}}}
            with self.subTest(launch=bundle_id),                     mock.patch.object(gate.router_module("dispatch_registry"), "claim", return_value=None):
                self.assertEqual("reason" in gate.evaluate_payload(launch, ledger_path=ledger, now=T0), blocked)

    def test_used_up_five_hour_window_blocks_every_account_alike(self):
        five = self.window("five_hour", 100)
        week = self.window("seven_day", 20, 100)
        for account, model in (("claude", "claude-opus-5-5"), ("claude-2", "claude-opus-5-5"),
                               ("claude-lab", "claude-sonnet-5-5"), ("codex", "gpt-6.1-sol")):
            cache = self.quota_cache({account: {"windows": [five, week]}})
            with self.subTest(account=account):
                self.assertIn("five_hour", gate.check_account_quota_exhausted(account, cache, T0, model=model))
                for fresh_row in ({"windows": [self.window("five_hour", 30), week]},
                                  {"windows": [{"id": "five_hour", "used_pct": 0, "resets_at": None}, week]}):
                    cache = self.quota_cache({account: fresh_row})   # partly used, or unstarted (no reset yet)
                    self.assertIsNone(gate.check_account_quota_exhausted(account, cache, T0, model=model))

    def test_scoped_week_blocks_only_its_own_model(self):
        cache = self.quota_cache({"claude": {"windows": [
            self.window("weekly_fable", 100, 40), self.window("seven_day", 30, 40), self.window("five_hour", 10)]}})
        self.assertIn("weekly_fable", gate.check_account_quota_exhausted("claude", cache, T0, model="claude-fable-5-1"))
        self.assertIsNone(gate.check_account_quota_exhausted("claude", cache, T0, model="claude-opus-5-5"))
        # A scope the provider reports on the reading needs no configuration or code (a new Sonnet-only week).
        cache = self.quota_cache({"claude": {"windows": [
            self.window("weekly_sonnet", 100, 40, models=["Sonnet"]), self.window("seven_day", 30, 40)]}})
        self.assertIn("weekly_sonnet", gate.check_account_quota_exhausted("claude", cache, T0, model="claude-sonnet-5-5"))
        self.assertIsNone(gate.check_account_quota_exhausted("claude", cache, T0, model="claude-opus-5-5"))
        # Without any declared scope the window binds every model.
        cache = self.quota_cache({"claude": {"windows": [self.window("weekly_mystery", 100, 40)]}})
        self.assertIsNotNone(gate.check_account_quota_exhausted("claude", cache, T0, model="claude-opus-5-5"))

    def test_magpie_pool_counts_every_account_it_holds(self):
        # claude-lab's five_hour keeps a 25% launch floor: 20% of one account is under it, 2 x 20% is not.
        for used, accounts, blocked in ((60, 2, False), (80, 2, False), (80, 1, True)):
            cache = self.quota_cache({"claude-lab": {"windows": [
                self.window("five_hour", used, accounts=accounts), self.window("seven_day", 30, 100, accounts=accounts)]}})
            with self.subTest(used=used, accounts=accounts):
                self.assertEqual(gate.check_account_quota_exhausted(
                    "claude-lab", cache, T0, model="claude-sonnet-5-5") is not None, blocked)

    def test_stale_reading_holds_inside_its_cycle_only(self):
        week = self.window("seven_day", 100, 24)
        for fetched, blocked in ((T0 - dt.timedelta(days=2), True), (T0 - dt.timedelta(days=8), False)):
            cache = self.quota_cache({"codex": {"fetched_at": fetched.isoformat(), "windows": [
                dict(week, id="usage")]}})
            with self.subTest(fetched=fetched):
                self.assertEqual(gate.check_account_quota_exhausted(
                    "codex", cache, T0, model="gpt-6.1-sol") is not None, blocked)

    def test_route_spec_resolves_account_from_ledger_or_bundle_catalog_without_name_inference(self):
        self.assertFalse(hasattr(gate, "secondary_claude_provider"))
        accounts, _ = gate.load_dispatch_settings()
        # Choice resolved from catalog when ledger row carries only choice
        spec = gate.route_spec({"choice": "lab_sonnet55_high"}, accounts)
        self.assertEqual(spec["provider"], "claude-lab")
        self.assertEqual(spec["model"], "claude-sonnet-5-5")
        self.assertEqual(spec["effort"], "high")
        # Arbitrary account resolved from ledger bundle without claude-2 suffix guessing
        custom = gate.route_spec(
            {"choice": "custom_worker", "bundle": {"account": "team-east", "model": "claude-sonnet-5-5", "effort": "high"}},
            accounts,
        )
        self.assertEqual(custom["provider"], "team-east")
        # Unrecognized choice with no bundle or catalog match does not invent claude-2
        unknown = gate.route_spec({"choice": "sonnet55_high_2"}, accounts)
        self.assertNotIn("provider", unknown)

    def test_quota_gate_counts_other_inflight_reservations_like_routing(self):
        # Antigravity 3p-weekly (the public community capacity of the Ultra 20x plan) at 93% used has 7% left (> 5% safety
        # margin). An active reservation of 3% of that capacity from another dispatch brings remaining to 4% (<= 5% margin).
        cache = self.quota_cache({"antigravity": {"windows": [
            self.window("gemini-weekly", 10, 100),
            self.window("gemini-5h", 10, 4),
            self.window("3p-weekly", 93.0, 100),
            self.window("3p-5h", 10, 4),
        ]}})
        accounts, _ = gate.load_dispatch_settings()
        _, candidates = gate.resolve_target({"bundle_id": "agy_opus55_high"}, accounts)
        axis_scores = gate.router_module("axis_scores")
        capacity = axis_scores.five_axis_overlay(accounts)["accounts"]["antigravity"]["capacity_usd"]["3p-weekly"]
        other_record = {
            "stage": "dispatch",
            "dispatch_id": "other-dispatch",
            "state": "started",
            "at": T0.isoformat(),
            "lease_until": (T0 + dt.timedelta(minutes=10)).isoformat(),
            "choice": "agy_opus55_high",
            "bundle": {"account": "antigravity", "provider": "antigravity-resilient", "model": "claude-opus-5-5", "effort": "high"},
            "expected_usd": round(0.03 * capacity, 2),
        }
        # Without the in-flight reservation, 7% > 5% safety margin is allowed.
        self.assertIsNone(gate.check_account_quota_exhausted(
            "antigravity", cache, T0, accounts=accounts, bundles=candidates, records=()))
        # With another dispatch's active reservation, remaining drops to 4% <= 5% and is blocked.
        reason = gate.check_account_quota_exhausted(
            "antigravity", cache, T0, accounts=accounts, bundles=candidates, records=[other_record])
        self.assertIsNotNone(reason)
        self.assertIn("3p-weekly", reason)
        # A dispatch's own reservation is excluded when claiming that dispatch itself.
        self.assertIsNone(gate.check_account_quota_exhausted(
            "antigravity", cache, T0, accounts=accounts, bundles=candidates,
            records=[other_record], exclude_dispatch_id="other-dispatch"))

    def test_a_final_report_marker_does_not_change_the_quota_hold(self):
        registry = gate.router_module("dispatch_registry")
        agent_id = "22222222-2222-4222-8222-222222222222"
        row = {
            "dispatch_id": "dispatch-final",
            "decision_id": "decision-final",
            "dispatch_mode": "paseo",
            "parent_id": "parent-a",
            "state": "started",
            "agent_id": agent_id,
            "created_at": T0.isoformat(),
            "updated_at": T0.isoformat(),
            "launched_at": T0.isoformat(),
            "lease_until": (T0 + dt.timedelta(minutes=10)).isoformat(),
            "hard_until": (T0 + dt.timedelta(hours=1)).isoformat(),
            "bundle": {"bundle_id": "lab_sonnet55_high", "account": "claude-lab",
                       "provider": "claude-lab", "model": "claude-sonnet-5-5"},
            "expected_usd": 6.0,
            "usage": {},
            "request": {"provider": "claude-lab/claude-sonnet-5-5", "labels": {}, "settings": {}},
            "events": [{"state": "started", "at": T0.isoformat()}],
        }
        with registry.transaction() as connection:
            registry.save(connection, row)
        quiet = {"tool_name": "send_agent_prompt",
                 "tool_input": {"agentId": "parent-a", "prompt": "still working", "notifyOnFinish": False}}
        report = {"tool_name": "send_agent_prompt",
                  "tool_input": {"agentId": "parent-a", "prompt": "[Worker final report]\ndone", "notifyOnFinish": False}}
        with mock.patch.dict(os.environ, {"PASEO_AGENT_ID": agent_id, "AGENTCOMPASS_WORKER": "1"}):
            self.assertEqual(gate.evaluate_payload(quiet, now=T0), {})
            self.assertEqual(registry.inspect("dispatch-final")["expected_usd"], 6.0)
            self.assertEqual(gate.evaluate_payload(report, now=T0), {})
        stored = registry.inspect("dispatch-final")
        self.assertEqual(stored["state"], "started")
        self.assertEqual(stored["expected_usd"], 6.0)
        self.assertNotIn("quota_released_reason", stored)
        self.assertTrue(registry.reservations(now=T0))
        other = dict(row, dispatch_id="dispatch-other", decision_id="decision-other", agent_id="worker-b",
                     expected_usd=6.0, quota_released_reason=None)
        other.pop("quota_released_reason")
        with registry.transaction() as connection:
            registry.save(connection, other)
        quoted = {"tool_name": "send_agent_prompt",
                  "tool_input": {"agentId": "worker-b", "prompt": "see [Worker final report] in the contract",
                                 "notifyOnFinish": False}}
        with mock.patch.dict(os.environ, {"PASEO_AGENT_ID": "parent-a", "AGENTCOMPASS_WORKER": ""}):
            gate.evaluate_payload(quoted, now=T0)
        self.assertEqual(registry.inspect("dispatch-other")["expected_usd"], 6.0)
        self.assertNotIn("quota_released_reason", registry.inspect("dispatch-other"))

    def test_without_router_modules_only_the_latch_is_checked(self):
        cache = self.quota_cache({"claude": {"windows": [self.window("five_hour", 100)]}})
        real = gate.router_module

        def missing(name):
            if name == "quota_exhaustion":
                return real(name)
            raise ImportError(name)
        with mock.patch.object(gate, "router_module", side_effect=missing):
            self.assertIsNone(gate.check_account_quota_exhausted("claude", cache, T0, model="claude-opus-5-5"))

    def test_interleaved_decisions_match_older_route_in_both_orders(self):
        a = self.route(parent_agent_id="parent-a", task_id="task-a")
        b = self.route("dot", parent_agent_id="parent-b", task_id="task-b")
        for rows in ([a, b], [b, a]):
            ledger = self.write_ledger(str(self.state), rows)
            for identity in ({}, {"parentAgentId": "parent-a", "taskId": "task-a"}):
                result = gate.evaluate_payload({"tool_name": "delegate_task", "tool_input": {
                    "provider": "codex/gpt-6-astra", "settings": {"thinkingOptionId": "high"}, **identity}},
                    ledger_path=ledger, now=T0)
                self.assertEqual(result, {})

    def test_same_task_new_decision_rejects_old_owner_with_diagnostics(self):
        identity = {"parent_agent_id": "parent-a", "task_id": "task-a"}
        ledger = self.write_ledger(str(self.state), [self.route(**identity), self.route("pi", **identity)])
        result = gate.evaluate_payload({"tool_name": "delegate_task", "tool_input": {
            "provider": "codex/gpt-6-astra", "settings": {"thinkingOptionId": "high"}, **identity}},
            ledger_path=ledger, now=T0)
        self.assertEqual(result["decision"], "block")
        self.assertIn("provider: requested='codex', routed='pi'", result["reason"])
        self.assertIn("pi_choice", result["reason"])
        self.assertIn("dispatch", result["reason"])

    def test_parent_alone_does_not_collapse_concurrent_tasks(self):
        ledger = self.write_ledger(str(self.state), [self.route(parent_agent_id="a"), self.route("dot", parent_agent_id="a")])
        self.assertEqual(gate.evaluate_payload({"session_id": "a", "tool_name": "delegate_task", "tool_input": {
            "provider": "codex/gpt-6-astra"}}, ledger_path=ledger, now=T0), {})

    def test_a_host_terminal_parent_matches_the_harness_session_it_runs(self):
        # Issue #2: roll inside an Orca terminal records the Orca session as the parent (the worker's reply
        # address) and the Claude Code session it runs in; Claude's hook payload carries only the latter.
        ledger = self.write_ledger(str(self.state), [self.route(parent_agent_id="orca-sess", host_session_id="claude-uuid")])
        launch = {"tool_name": "delegate_task", "tool_input": {"provider": "codex/gpt-6-astra"}}
        self.assertEqual(gate.evaluate_payload(dict(launch, session_id="claude-uuid"), ledger_path=ledger, now=T0), {})
        self.assertEqual(gate.evaluate_payload(dict(launch, session_id="orca-sess"), ledger_path=ledger, now=T0), {})
        self.assertEqual(gate.evaluate_payload(dict(launch, session_id="other-claude"), ledger_path=ledger, now=T0)["decision"],
                         "block")

    def test_nested_task_correlation_and_ttl_are_enforced(self):
        ledger = self.write_ledger(str(self.state), [self.route(task={"task_id": "a"}), self.route("pi", task={"task_id": "a"})])
        payload = {"tool_name": "delegate_task", "tool_input": {"provider": "codex/gpt-6-astra", "taskId": "a"}}
        self.assertIn("routed='pi'", gate.evaluate_payload(payload, ledger_path=ledger, now=T0)["reason"])
        expired = self.route()
        expired["at"] = (T0 - dt.timedelta(minutes=16)).isoformat()
        ledger = self.write_ledger(str(self.state), [expired, self.route("dot")])
        self.assertEqual(gate.evaluate_payload(payload, ledger_path=ledger, now=T0)["decision"], "block")

    @mock.patch.object(gate, 'load_dispatch_settings', return_value=({}, None))
    def test_structured_model_effort_and_correlation_mismatches(self, _settings):
        ledger = self.write_ledger(str(self.state), [self.route(parent_agent_id="a", task_id="task-a", agent_id="target-a")])
        for extra, diagnostic in (({"provider": "pi/gpt-6-astra"}, "provider"),
                                  ({"provider": "codex/gpt-6-sol"}, "model"),
                                  ({"settings": {"thinkingOptionId": "max"}}, "effort"),
                                  ({"parentAgentId": "b"}, "parent/task/target"),
                                  ({"taskId": "task-b"}, "parent/task/target"),
                                  ({"targetAgentId": "target-b"}, "parent/task/target")):
            args = {"provider": "codex/gpt-6-astra", "settings": {"thinkingOptionId": "high"}, **extra}
            result = gate.evaluate_payload({"tool_name": "delegate_task", "tool_input": args}, ledger_path=ledger, now=T0)
            self.assertIn(diagnostic, result["reason"])

    def test_claude_agent_tool_family_alias_is_the_routed_model(self):
        # Claude's Agent tool accepts only family aliases; each row changes one condition.
        ledger = self.write_ledger(str(self.state), [self.route("claude", "claude-sonnet-5-5", "high")])
        for model, effort, blocked_on in (("sonnet", "high", None),
                                          ("claude-sonnet-5-5", "high", None),
                                          ("opus", "high", "model"),
                                          ("sonnet", "max", "effort")):
            args = {"subagent_type": "general-purpose", "prompt": "do it", "model": model, "effort": effort}
            result = gate.evaluate_payload({"tool_name": "Agent", "tool_input": args}, ledger_path=ledger, now=T0)
            with self.subTest(model=model, effort=effort):
                if blocked_on:
                    self.assertIn(f"{blocked_on}: requested=", result["reason"])
                else:
                    self.assertEqual(result, {})
        # An alias names a Claude family; it cannot claim a non-Claude route.
        ledger = self.write_ledger(str(self.state), [self.route()])
        result = gate.evaluate_payload({"tool_name": "Agent", "tool_input": {"prompt": "x", "model": "sonnet"}},
                                       ledger_path=ledger, now=T0)
        self.assertIn("model: requested='sonnet'", result["reason"])

    def test_current_wrapped_launch_uses_literal_settings_and_all_calls(self):
        ledger = self.write_ledger(str(self.state), [self.route()])
        with mock.patch.object(gate.router_module("dispatch_registry"), "claim", return_value=None):
            for code, allowed in (
                ("await paseo.create_agent({provider:'codex/gpt-6-astra', settings:{thinkingOptionId:'high'}, labels:{'router.dispatch_id':'d1'}})", True),
                ('await tools.mcp__paseo__create_agent({provider:"codex/gpt-6-astra", settings:{thinkingOptionId:"high"}})', False),
                ('await paseo.create_agent({provider:"codex/gpt-6-astra", labels:{"router.dispatch_id":"d1"}}); await paseo.create_agent({provider:"pi/gpt-6-astra"})', False),
            ):
                result = gate.evaluate_payload({"tool_name": "exec", "tool_input": code}, ledger_path=ledger, now=T0)
                self.assertEqual(result == {}, allowed, result)
            # Quoted examples and readonly provider arguments are not executing calls.
            self.exhausted_cache()
            code = 'await paseo.list_models({provider:"claude-lab"}); await paseo.create_agent({provider:"codex/gpt-6-astra", labels:{"router.dispatch_id":"d1"}, initialPrompt:"create_agent({provider: \'claude-lab\'})"})'
            self.assertEqual(gate.evaluate_payload({"tool_name": "exec", "tool_input": code}, ledger_path=ledger, now=T0), {})
        quoted = '// create_agent({provider:"claude-lab"})\ntext("create_agent({})")'
        self.assertEqual(gate.evaluate_payload({"toolName": "codemode", "input": {"code": quoted}}, now=T0), {"block": False})

    def test_mixed_wrapper_still_checks_shell_launch(self):
        code = 'await paseo.send_agent_prompt({agentId:"unknown", prompt:"continue"}); await tools.exec_command({cmd:"codex exec -m gpt-6-astra \'task\'"})'
        result = gate.evaluate_payload({"tool_name": "exec", "tool_input": code}, now=T0)
        self.assertIn("shell:codex exec", result["reason"])

    @pytest.mark.slow
    def test_native_cli_entrypoint_uses_isolated_state_and_response_contract(self):
        for payload, expected in (({"tool_name": "spawn_agent", "tool_input": {}}, "block"),
                                  ({"toolCall": {"name": "invoke_subagent", "args": {}}}, "deny"),
                                  ({"toolName": "create_agent", "input": {}}, True)):
            proc = subprocess.run([sys.executable, "-X", "utf8", "-B", str(Path(gate.__file__))], input=json.dumps(payload),
                                  text=True, encoding="utf-8", capture_output=True, check=True)
            response = json.loads(proc.stdout)
            self.assertEqual(response.get("decision", response.get("block")), expected)

    def test_worker_compaction_limit_blocks_worn_turn_and_allows_fresh_or_unknown(self):
        proj_dir = self.state / "agents" / "proj"
        log_dir = self.state / ".claude" / "projects" / "proj"
        proj_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        parent_id = "00000000-0000-0000-0000-000000000001"
        wid = "11111111-2222-3333-4444-555555555555"
        fresh_wid = "22222222-3333-4444-5555-666666666666"
        worker_doc = {
            "id": wid,
            "provider": "claude",
            "labels": {"router.parent_id": parent_id, "paseo.parent-agent-id": parent_id},
            "persistence": {"provider": "claude", "sessionId": "s-comp"},
        }
        (proj_dir / f"{wid}.json").write_text(json.dumps(worker_doc), encoding="utf-8")
        (proj_dir / f"{fresh_wid}.json").write_text(
            json.dumps({"id": fresh_wid, "provider": "claude", "labels": {"router.parent_id": parent_id}}),
            encoding="utf-8",
        )
        boundary = json.dumps({"type": "system", "subtype": "compact_boundary", "compactMetadata": {"preTokens": 900000}})
        log_file = log_dir / "s-comp.jsonl"

        # 1. Degrade (missing log) and Allow (even with 2 compactions, 1.8M historical tokens < 2M threshold)
        for contents in (None, boundary + "\n" + boundary + "\n"):
            if contents is not None:
                log_file.write_text(contents, encoding="utf-8")
            res = gate.evaluate_payload({"tool_name": "mcp__paseo__run_turn", "tool_input": {"agentId": wid, "prompt": "next"}}, now=T0)
            self.assertEqual(res, {})

        # 2. Block (>= 2M historical tokens) on delegated worker across direct turn tool, literal codemode, and dynamic-var codemode
        log_file.write_text(boundary + "\n" + boundary + "\n" + boundary + "\n", encoding="utf-8")
        for payload, blocked_key in (
            ({"tool_name": "mcp__paseo__run_turn", "tool_input": {"agentId": wid, "prompt": "next"}}, "decision"),
            ({"toolName": "codemode", "input": {"code": f'await paseo.run_turn({{agentId: "{wid}", prompt: "next"}})'}}, "block"),
            ({"toolName": "codemode", "input": {"code": f'const id = "{wid}"; await paseo.send_agent_prompt({{agentId: id, prompt: "next"}})'}}, "block"),
        ):
            res = gate.evaluate_payload(payload, now=T0)
            self.assertTrue(res.get(blocked_key), payload)
            self.assertIn("historical_context_tokens=2,700,000/2,000,000", res["reason"])
            self.assertIn("roll --brief", res["reason"])

        # 3. Allow: mentioning worn worker UUID inside prompt text to a fresh worker is not blocked
        handoff_code = f'const prompt = "prev worker {wid}"; await paseo.send_agent_prompt({{agentId: "{fresh_wid}", prompt}})'
        self.assertEqual(gate.evaluate_payload({"toolName": "codemode", "input": {"code": handoff_code}}, now=T0), {"block": False})

        # 4. Allow: child reporting upward to its parent or peer messaging a user main session (labels={}) is never blocked
        child_id = "33333333-4444-5555-6666-777777777777"
        (proj_dir / f"{child_id}.json").write_text(
            json.dumps({"id": child_id, "provider": "claude", "labels": {"paseo.parent-agent-id": wid}}),
            encoding="utf-8",
        )
        self.assertEqual(
            gate.evaluate_payload({"parent_agent_id": child_id, "tool_name": "mcp__paseo__send_agent_prompt",
                                   "tool_input": {"agentId": wid, "prompt": "report"}}, now=T0),
            {},
        )
        (proj_dir / f"{wid}.json").write_text(json.dumps({**worker_doc, "labels": {}}), encoding="utf-8")
        self.assertEqual(
            gate.evaluate_payload({"tool_name": "mcp__paseo__send_agent_prompt",
                                   "tool_input": {"agentId": wid, "prompt": "cross-lane notice"}}, now=T0),
            {},
        )

    # --- MCP-prefixed names (a proxy such as Magpie re-exports tools as mcp__<server>__<tool>) ---

    def test_base_tool_name_strips_only_mcp_server_prefixes(self):
        for name, base in (
                ("Agent", "Agent"),
                ("mcp__magpie__Agent", "Agent"),
                ("mcp__magpie__mcp__paseo__send_agent_prompt", "send_agent_prompt"),
                ("mcp__t3-code__delegate_task", "delegate_task"),
                # A server name may hold `__` or end in `_`; the classifier backtracks exactly like the matcher.
                ("mcp__paseo___create_agent", "create_agent"),
                ("mcp__my__srv__Agent", "Agent"),
                ("mcp__a__b__mcp__c___Workflow", "Workflow"),
                ("mcp__claude_ai_Claude_Docs__batch", "mcp__claude_ai_Claude_Docs__batch"),  # not a hooked tool
                ("mcp__paseo__", "mcp__paseo__"),       # no tool behind the prefix: left as is
                ("xmcp__paseo__create_agent", "xmcp__paseo__create_agent"),
                ("ListAgents", "ListAgents")):
            with self.subTest(name=name):
                self.assertEqual(gate.base_tool_name(name), base)

    def test_every_sub_agent_launch_name_blocks_without_a_route_prefixed_or_not(self):
        """Each launcher, bare and behind one or two MCP prefixes, is blocked for want of a router record."""
        missing = self.state / "missing.jsonl"
        launches = {
            "Agent": {"subagent_type": "Explore", "prompt": "look"},
            "Task": {"prompt": "look"},
            "spawn_agent": {"message": "work"},
            "invoke_subagent": {"Subagents": []},
            "create_agent": {"provider": "codex/gpt-6-astra", "prompt": "work"},
            "delegate_task": {"task": "work"},
            # Workflow: its script's agent() calls start sub-agents inside the runtime, out of any hook's sight.
            "Workflow": {"script": "export const meta = {name: 'x', description: 'y'}\nawait agent('go')"},
            # create_schedule: Paseo "starts a new agent on a cron cadence".
            "create_schedule": {"prompt": "work", "cron": "7 * * * *", "provider": "codex/gpt-6-astra"},
            # update_schedule that re-targets the schedule's new-agent launch.
            "update_schedule": {"id": "s1", "provider": "codex", "model": "gpt-6-astra"},
        }
        for base, args in launches.items():
            for name in (base, f"mcp__magpie__{base}", f"mcp__magpie__mcp__paseo__{base}",
                         f"mcp__paseo___{base}", f"mcp__my__srv__{base}"):   # server names ending in `_` / holding `__`
                with self.subTest(name=name):
                    result = gate.evaluate_payload({"tool_name": name, "tool_input": args}, ledger_path=missing, now=T0)
                    self.assertEqual(result.get("decision"), "block")
                    self.assertIn(f"tool:{base}", result["reason"])
        # Antigravity and Pi payload shapes go through the same names.
        agy = gate.evaluate_payload({"toolCall": {"name": "mcp__magpie__invoke_subagent", "args": {}}},
                                    ledger_path=missing, now=T0)
        self.assertEqual(agy["decision"], "deny")
        pi = gate.evaluate_payload({"toolName": "mcp__magpie__Workflow", "input": {"script": "x"}},
                                   ledger_path=missing, now=T0)
        self.assertTrue(pi["block"])

    def test_prefixed_wrappers_and_shell_tools_are_inspected_like_bare_ones(self):
        missing = self.state / "missing.jsonl"
        for payload in (
                {"tool_name": "mcp__magpie__Bash", "tool_input": {"command": "codex exec -m gpt-6-astra 'task'"}},
                {"tool_name": "mcp__magpie__mcp__paseo__execute_code",
                 "tool_input": {"code": "await tools.mcp__magpie__mcp__paseo__create_agent({provider: 'codex'})"}},
                {"tool_name": "mcp__paseo__execute_code", "tool_input": {"code": "await create_agent({provider: 'codex'})"}}):
            with self.subTest(payload=payload):
                self.assertEqual(gate.evaluate_payload(payload, ledger_path=missing, now=T0).get("decision"), "block")
        self.assertEqual(gate.evaluate_payload(
            {"tool_name": "mcp__magpie__Bash", "tool_input": {"command": "git status"}}, ledger_path=missing, now=T0), {})

    def test_tools_that_do_not_start_an_agent_stay_allowed_without_a_route(self):
        """Names that merely contain "agent"/"schedule", and tools that only reach an existing session."""
        missing = self.state / "missing.jsonl"
        for name, args in (
                ("mcp__paseo__get_agent_status", {"agentId": "a"}),
                ("mcp__paseo__list_agents", {}),
                ("mcp__magpie__mcp__paseo__list_agents", {}),
                ("mcp__paseo__get_agent_activity", {"agentId": "a"}),
                ("mcp__paseo__archive_agent", {"agentId": "a"}),
                ("mcp__paseo__cancel_agent", {"agentId": "a"}),
                ("ListAgents", {}),
                ("TaskStop", {"task_id": "t"}),
                ("mcp__magpie__TaskStop", {"task_id": "t"}),
                ("SubagentHandback", {}),
                # SendMessage resumes an existing agent or reaches another session (incl. a worker's parent);
                # it cannot create an agent, so it is not a launch.
                ("SendMessage", {"to": "main", "message": "done"}),
                ("mcp__magpie__SendMessage", {"to": "worker", "message": "status?"}),
                # A heartbeat prompts the caller itself; run_schedule_once fires a schedule gated at creation.
                ("mcp__paseo__create_heartbeat", {"prompt": "check", "cron": "7 * * * *"}),
                ("mcp__paseo__run_schedule_once", {"id": "s1"}),
                ("mcp__paseo__update_schedule", {"id": "s1", "cron": "9 * * * *"}),
                ("mcp__paseo__list_schedules", {}),
                ("CronCreate", {"cron": "7 * * * *", "prompt": "check"}),
                ("mcp__someserver__agent_notes", {}),
                ("mcp__someserver__Agent_list", {})):
            with self.subTest(name=name):
                self.assertEqual(gate.evaluate_payload({"tool_name": name, "tool_input": args},
                                                       ledger_path=missing, now=T0), {})

    def test_prefixed_launch_passes_with_the_same_route_as_a_bare_one(self):
        ledger = self.write_ledger(str(self.state), [self.route("claude", "claude-sonnet-5-5", "high")])
        args = {"subagent_type": "general-purpose", "prompt": "do it", "model": "sonnet", "effort": "high"}
        for name in ("Agent", "mcp__magpie__Agent"):
            with self.subTest(name=name):
                self.assertEqual(gate.evaluate_payload({"tool_name": name, "tool_input": args},
                                                       ledger_path=ledger, now=T0), {})
        mismatch = gate.evaluate_payload({"tool_name": "mcp__magpie__Agent", "tool_input": dict(args, model="opus")},
                                         ledger_path=ledger, now=T0)
        self.assertIn("model: requested='opus'", mismatch["reason"])
        # A worker route is not a Workflow authorization.
        workflow = gate.evaluate_payload({"tool_name": "mcp__magpie__Workflow", "tool_input": {"script": "x"},
                                          "session_id": "s1"}, ledger_path=ledger, now=T0)
        self.assertIn("--trigger workflow", workflow["reason"])

    def test_recorded_project_review_sub_agent_stays_exempt_under_a_prefix(self):
        """`agentcompass record` for a project review sub-agent authorizes its native Agent launch."""
        record = {"at": (T0 - dt.timedelta(minutes=2)).isoformat(), "stage": "record", "choice": "constraint",
                  "source": "constraint_record", "constraint": "user_override",
                  "selected_bundle": {"provider": "claude", "account": "claude", "model": "claude-opus-5-5",
                                      "effort": "high"}}
        ledger = self.write_ledger(str(self.state), [record])
        args = {"subagent_type": "code-reviewer", "prompt": "review", "model": "opus"}
        for name in ("Agent", "mcp__magpie__Agent"):
            with self.subTest(name=name):
                self.assertEqual(gate.evaluate_payload({"tool_name": name, "tool_input": args},
                                                       ledger_path=ledger, now=T0), {})

    def test_prefixed_send_agent_prompt_keeps_worker_checks_and_parent_exemptions(self):
        wid = self.existing_agent()
        agent_file = self.state / "agents" / "project" / (wid + ".json")
        self.write_settings(allowed=[])
        prefixed = "mcp__magpie__mcp__paseo__send_agent_prompt"
        cases = (
            # (labels, status, caller, blocked): the parent's own running worker is controlled; others are turns.
            ({"router.parent_id": "parent"}, "running", "parent", False),
            ({"router.parent_id": "parent"}, "idle", "parent", True),
            ({"router.parent_id": "parent"}, "running", "peer", True),   # a worker this caller does not own
        )
        for labels, status, caller, blocked in cases:
            agent_file.write_text(json.dumps({
                "provider": "claude-lab/claude-sonnet-5-5", "lastStatus": status,
                "labels": {**labels, "router.account": "claude-lab", "router.bundle_id": "lab_sonnet55_high"}}),
                encoding="utf-8")
            for name in ("mcp__paseo__send_agent_prompt", prefixed):
                with self.subTest(name=name, status=status, caller=caller), \
                        mock.patch.dict(os.environ, {"PASEO_AGENT_ID": caller}):
                    result = gate.evaluate_payload({"tool_name": name, "tool_input": {
                        "agentId": wid, "prompt": "wrap up", "notifyOnFinish": True}}, now=T0)
                    self.assertEqual("reason" in result, blocked)
        # A parent turning off its own worker's completion notification is refused under the prefix too.
        agent_file.write_text(json.dumps({"provider": "claude-lab/claude-sonnet-5-5", "lastStatus": "running",
                                          "labels": {"router.parent_id": "parent"}}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"PASEO_AGENT_ID": "parent"}):
            result = gate.evaluate_payload({"tool_name": prefixed, "tool_input": {
                "agentId": wid, "prompt": "x", "notifyOnFinish": False}}, now=T0)
        self.assertIn("notifyOnFinish", result["reason"])

    # --- Workflow and schedules: their own authorization, matched to the call and to the calling session ---

    def workflow_record(self, **fields):
        """A record row as route_judge writes it: parent_id/host_session_id are the parent link, session_ids the writer."""
        return {"at": (T0 - dt.timedelta(minutes=2)).isoformat(), "stage": "record", "choice": "constraint",
                "source": "constraint_record", "constraint": "user_override", "trigger": "workflow",
                "parent_id": "paseo-parent", "host_session_id": "parent-claude-session",
                "session_ids": ["claude-session", "paseo-agent"],
                "selected_bundle": {"provider": "claude", "account": "claude", "model": "claude-opus-5-5",
                                    "effort": "high"}, **fields}

    def session_plan(self, provider="codex", model="gpt-6-astra", effort="high", **fields):
        return dict(self.route(provider, model, effort), session_ids=["claude-session"], **fields)

    def test_workflow_needs_this_sessions_workflow_record(self):
        call = {"tool_input": {"script": "export const meta = {name: 'x', description: 'y'}"}}
        unrelated_review = self.workflow_record(trigger=None)          # e.g. a code-reviewer record
        cases = (
            ("no ledger", [], {"session_id": "claude-session"}, "--trigger workflow"),
            ("unrelated record", [unrelated_review], {"session_id": "claude-session"}, "--trigger workflow"),
            ("worker plan", [self.session_plan("claude", "claude-opus-5-5")],
             {"session_id": "claude-session"}, "--trigger workflow"),
            ("hard constraint", [self.workflow_record(constraint="hard_constraint")],
             {"session_id": "claude-session"}, "--trigger workflow"),
            ("other session", [self.workflow_record()], {"session_id": "another-session"}, "--trigger workflow"),
            # R3: the parent link names the parent; it never authorizes the parent's Workflow.
            ("parent of the writer", [self.workflow_record()], {"session_id": "parent-claude-session"},
             "--trigger workflow"),
            ("parent agent of the writer", [self.workflow_record()], {"parent_agent_id": "paseo-parent"},
             "--trigger workflow"),
            ("row without session_ids", [self.workflow_record(session_ids=None)], {"session_id": "claude-session"},
             "--trigger workflow"),
            ("no session id", [self.workflow_record()], {}, "会话 ID"),
            ("expired", [self.workflow_record(at=(T0 - dt.timedelta(minutes=20)).isoformat())],
             {"session_id": "claude-session"}, "--trigger workflow"),
            ("same Claude session", [self.workflow_record()], {"session_id": "claude-session"}, None),
            ("same Paseo agent", [self.workflow_record()], {"parent_agent_id": "paseo-agent"}, None),
        )
        for label, rows, identity, blocked_on in cases:
            ledger = self.write_ledger(str(self.state), rows) if rows else self.state / "missing.jsonl"
            for name in ("Workflow", "mcp__magpie__Workflow"):
                with self.subTest(case=label, name=name), mock.patch.dict(os.environ, {"PASEO_AGENT_ID": ""}):
                    result = gate.evaluate_payload({"tool_name": name, **call, **identity}, ledger_path=ledger, now=T0)
                    if blocked_on:
                        self.assertIn(blocked_on, result["reason"])
                        self.assertNotIn("roll --brief", result["reason"].split("不要调用")[0])
                    else:
                        self.assertEqual(result, {})
        # Under Paseo the calling agent's own ID identifies the session too; the parent's ID does not.
        ledger = self.write_ledger(str(self.state), [self.workflow_record()])
        for agent_id, allowed in (("paseo-agent", True), ("paseo-parent", False)):
            with self.subTest(paseo_agent=agent_id), mock.patch.dict(os.environ, {"PASEO_AGENT_ID": agent_id}):
                result = gate.evaluate_payload({"tool_name": "Workflow", **call}, ledger_path=ledger, now=T0)
                self.assertEqual(result == {}, allowed)

    def test_schedule_needs_this_sessions_recent_plan_with_its_provider_and_model(self):
        plan = self.session_plan()
        review_record = self.workflow_record(trigger=None)
        same_as_plan_record = dict(plan, stage="record", choice="constraint", constraint="user_override")
        mcp_cases = (
            ("no provider", [plan], {"prompt": "x", "cron": "7 * * * *"}, "未指定 provider/model"),
            ("provider only", [plan], {"prompt": "x", "cron": "7 * * * *", "provider": "codex"}, "未指定 model"),
            ("no plan", [review_record], {"prompt": "x", "cron": "7 * * * *", "provider": "codex/gpt-6-astra"},
             "没有 plan 记录"),
            ("record is not a plan", [same_as_plan_record],
             {"prompt": "x", "cron": "7 * * * *", "provider": "codex/gpt-6-astra"}, "没有 plan 记录"),
            # R2: a plan another session made, or one that names no writer, is not this session's.
            ("other session's plan", [dict(plan, session_ids=["session-a"])],
             {"prompt": "x", "cron": "7 * * * *", "provider": "codex/gpt-6-astra"}, "本会话没有 plan 记录"),
            ("plan without session_ids", [self.route("codex", "gpt-6-astra", "high")],
             {"prompt": "x", "cron": "7 * * * *", "provider": "codex/gpt-6-astra"}, "本会话没有 plan 记录"),
            ("other model", [plan], {"prompt": "x", "cron": "7 * * * *", "provider": "codex/gpt-6.1-sol"},
             "model: requested='gpt-6.1-sol'"),
            ("matching plan", [plan], {"prompt": "x", "cron": "7 * * * *", "provider": "codex/gpt-6-astra"}, None),
        )
        for label, rows, args, blocked_on in mcp_cases:
            ledger = self.write_ledger(str(self.state), rows)
            for name in ("create_schedule", "mcp__paseo__create_schedule", "mcp__magpie__mcp__paseo__create_schedule"):
                with self.subTest(case=label, name=name):
                    result = gate.evaluate_payload({"tool_name": name, "tool_input": args, "session_id": "claude-session"},
                                                   ledger_path=ledger, now=T0)
                    if blocked_on:
                        self.assertIn(blocked_on, result["reason"])
                        self.assertIn("plan --input", result["reason"])
                        self.assertNotIn("roll --brief", result["reason"])
                    else:
                        self.assertEqual(result, {})
        ledger = self.write_ledger(str(self.state), [plan])
        with mock.patch.dict(os.environ, {"PASEO_AGENT_ID": ""}):
            anonymous = gate.evaluate_payload({"tool_name": "create_schedule", "tool_input": {
                "prompt": "x", "cron": "7 * * * *", "provider": "codex/gpt-6-astra"}}, ledger_path=ledger, now=T0)
        self.assertIn("会话 ID", anonymous["reason"])
        for args, blocked in (({"id": "s1", "provider": "codex", "model": "gpt-6-astra"}, False),
                              ({"id": "s1", "provider": "codex", "model": "gpt-6.1-sol"}, True),
                              ({"id": "s1", "model": "gpt-6-astra"}, True),
                              ({"id": "s1", "cron": "9 * * * *"}, False)):
            with self.subTest(update=args):
                result = gate.evaluate_payload({"tool_name": "mcp__paseo__update_schedule", "tool_input": args,
                                                "session_id": "claude-session"}, ledger_path=ledger, now=T0)
                self.assertEqual("reason" in result, blocked)

    def test_paseo_schedule_cli_is_the_same_launch_as_the_mcp_tool(self):
        routed = self.write_ledger(str(self.state), [self.session_plan()])
        missing = self.state / "missing.jsonl"
        script = ("import subprocess; subprocess.run(['paseo', 'schedule', 'create', '--cron', '7 * * * *', "
                  "'--provider', 'codex/gpt-6-astra', 'work'])")
        dynamic = "import subprocess; subprocess.run(['paseo', 'schedule', 'create', '--provider', p, 'work'])"
        cases = (
            # (command, ledger, blocked)
            ('paseo schedule create --cron "*/5 * * * *" --provider codex "do x"', missing, True),
            ('paseo schedule create --every 5m --provider codex/gpt-6-astra --run-now "do x"', missing, True),
            ('paseo schedule create --every 5m --provider=codex/gpt-6-astra "do x"', routed, False),
            ('paseo schedule create --every 5m --provider codex/gpt-6-astra "do x"', routed, False),
            ('paseo schedule create --every 5m --provider codex/gpt-6.1-sol "do x"', routed, True),
            ('paseo schedule create --every 5m "do x"', routed, True),               # caller's default provider
            (r'& "C:\Program Files\Paseo\paseo.cmd" schedule create --every 5m --provider codex "x"', missing, True),
            ("paseo schedule update s1 --provider codex --model gpt-6-astra", routed, False),
            ("paseo schedule update s1 --model gpt-6.1-sol --provider codex", routed, True),
            ("paseo schedule update s1 --model gpt-6-astra", routed, True),
            ("paseo schedule update s1 --cron '9 * * * *' --prompt 'use --provider x'", missing, False),
            (f'python -c "{script}"', missing, True),
            (f'python -c "{script}"', routed, False),
            (f'python -c "{dynamic}"', routed, True),
            ("paseo schedule create --help", missing, False),
            ("paseo schedule run-once s1", missing, False),
            ("paseo schedule ls", missing, False),
            ('echo "paseo schedule create --provider codex x"', missing, False),
        )
        for command, ledger, blocked in cases:
            for payload in ({"tool_name": "Bash", "tool_input": {"command": command}, "session_id": "claude-session"},
                            {"tool_name": "mcp__magpie__PowerShell", "tool_input": {"command": command},
                             "session_id": "claude-session"}):
                with self.subTest(command=command, tool=payload["tool_name"]):
                    result = gate.evaluate_payload(payload, ledger_path=ledger, now=T0)
                    self.assertEqual("reason" in result, blocked)
                    if blocked:
                        self.assertIn("plan --input", result["reason"])

    def test_paseo_cli_global_options_and_wrappers_do_not_hide_a_launch(self):
        """R1: commander takes global options before and between command words; wrappers run the same argv."""
        missing = self.state / "missing.jsonl"
        schedule = "schedule create --provider codex/gpt-6-astra --run-now x"
        launches = [
            f"paseo --json {schedule}", f"paseo -q {schedule}", f"paseo --quiet {schedule}",
            f"paseo --host localhost:6767 {schedule}", f"paseo --host=localhost:6767 {schedule}",
            f"paseo -o json {schedule}", f"paseo --home C:/h --no-color --no-headers {schedule}",
            "paseo schedule --json create --provider codex/gpt-6-astra --run-now x",
            "paseo schedule --host h:1 create --provider codex/gpt-6-astra x",
            "paseo --json schedule update s1 --provider codex/gpt-6-astra",
            f'bash -c "paseo {schedule}"', f"sh -c 'paseo --json {schedule}'", f'/usr/bin/bash -c "paseo {schedule}"',
            f'pwsh -Command "paseo {schedule}"', f'cmd /c paseo {schedule}', f'cmd.exe /c "paseo {schedule}"',
            f"npx paseo {schedule}", f"npx -y paseo --json {schedule}", f"pnpm dlx paseo {schedule}",
            f"paseo.ps1 {schedule}", rf"& C:\tools\paseo.ps1 --json {schedule}",
            'Start-Process paseo -ArgumentList "schedule","create","--provider","codex/gpt-6-astra","x"',
            'Start-Process -FilePath paseo.cmd -ArgumentList "--json schedule create --provider codex/gpt-6-astra x"',
            "Start-Process paseo -ArgumentList 'schedule','--json','create','--provider','codex/gpt-6-astra','x' -NoNewWindow",
            # The same rule for the other Paseo launch commands.
            "paseo --json run --provider codex x", "paseo -q agent run --provider codex x",
            "paseo agent --json run --provider codex x", 'bash -c "paseo --json run --provider codex x"',
            'Start-Process paseo -ArgumentList "run","--provider","codex","x"',
            "python -c \"import subprocess; subprocess.run(['paseo', '--json', 'schedule', 'create', '--provider', 'codex/gpt-6-astra', 'x'])\"",
        ]
        reads = [
            "paseo --json schedule ls", "paseo schedule --json ls", "paseo -q schedule inspect s1",
            "paseo --json schedule logs s1", "paseo --json schedule run-once s1", "paseo --json schedule create --help",
            "paseo schedule create -h", 'bash -c "paseo --json schedule ls"', "npx paseo --json ls",
            "paseo --json ls", "paseo --host h:1 inspect 11111111-1111-4111-8111-111111111111",
            'Start-Process paseo -ArgumentList "schedule","ls"', "paseo --json start", "paseo -q status",
            'echo "paseo --json schedule create --provider codex x"',
        ]
        for command in launches + reads:
            with self.subTest(command=command):
                result = gate.evaluate_payload({"tool_name": "Bash", "tool_input": {"command": command},
                                                "session_id": "claude-session"}, ledger_path=missing, now=T0)
                self.assertEqual("reason" in result, command in launches)
        # A send behind global options is still a turn to that worker (target checks apply).
        wid = self.existing_agent()
        calls = gate.cli_turn_calls("Bash", {"command": f"paseo --json send --no-wait {wid} 'go'"})
        self.assertEqual(calls, [("run_turn", {"agentId": wid})])
        calls = gate.cli_turn_calls("Bash", {"command": f"paseo -q agent --host h:1 send {wid} go"})
        self.assertEqual(calls, [("run_turn", {"agentId": wid})])

    def test_matcher_and_gate_classify_every_name_alike(self):
        """The installed matcher sends a name to the hook exactly when the gate treats it as a hooked tool."""
        import re
        matcher = re.compile(gate.hook_matcher())
        questions = re.compile("|".join(gate.WORKER_QUESTION_TOOLS))
        bases = sorted(gate.HOOKED_TOOLS) + ["TaskStop", "ListAgents", "get_agent_status", "batch", "AgentX"]
        prefixes = ("", "mcp__paseo__", "mcp__magpie__mcp__paseo__", "mcp__paseo___", "mcp__my__srv__",
                    "mcp__a_-b__", "mcp___", "mcp__", "xmcp__paseo__", "mcp__paseo__x", "mcp____", "mcp__-__")
        for base in bases:
            for prefix in prefixes:
                name = prefix + base
                with self.subTest(name=name):
                    hooked = bool(matcher.search(name)) and not questions.search(name)
                    self.assertEqual(hooked, gate.base_tool_name(name) in gate.HOOKED_TOOLS)

    def test_single_prefix_pattern_equals_the_repeated_one_on_random_names(self):
        """R4: one optional `mcp__<server>__` accepts exactly the names (and base) the repeated form did."""
        import random
        import re
        names = "|".join(sorted(gate.HOOKED_TOOLS))
        repeated = re.compile(r"(?:mcp__[A-Za-z0-9_-]+?__)*(" + names + ")")
        alphabet = ["mcp__", "__", "_", "-", "a", "Z", "9", "mcp", "m", "x", "é", " "]
        rng = random.Random(20261110)
        bases = sorted(gate.HOOKED_TOOLS) + ["Agen", "TaskStop", ""]
        for _ in range(20000):
            name = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 7))) + rng.choice(bases)
            old = repeated.fullmatch(name)
            with self.subTest(name=name):
                self.assertEqual(gate.base_tool_name(name), old.group(1) if old else name)

    @pytest.mark.slow
    def test_adversarial_names_classify_in_linear_time(self):
        """R4: the repeated form took over 60 s on this 10k-character name; the gate and the matcher must not."""
        script = (
            "import re, sys, time; sys.path.insert(0, sys.argv[1]); import router_gate_hook as g\n"
            "names = ['mcp__a__' * 1250 + 'zz', 'mcp__' + '_' * 10000 + 'zz', 'mcp__a' * 2000 + '__zz',\n"
            "         'mcp__a__' * 1250 + 'Agent', 'mcp__a__' * 1250 + 'run_turn(']\n"
            "matcher = re.compile(g.hook_matcher())\n"
            "start = time.perf_counter()\n"
            "for name in names:\n"
            "    g.base_tool_name(name); matcher.search(name); g.CODEMODE_TURN_AGENT_RE.search(name)\n"
            "    g.WRAPPED_AGENT_CALL_RE.search(name)\n"
            "print(time.perf_counter() - start)\n")
        out = subprocess.run([sys.executable, "-c", script, str(Path(gate.__file__).parent)],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertLess(float(out.stdout), 2.0)

    def test_hook_matcher_routes_exactly_the_gated_names(self):
        """Claude Code tests a non-plain matcher with `new RegExp(matcher).test(name)`; Python's search is the same."""
        import re
        matcher = re.compile(gate.hook_matcher())
        for name in ("Agent", "mcp__magpie__Agent", "Workflow", "mcp__magpie__Workflow", "Task",
                     "mcp__paseo__create_agent", "mcp__magpie__mcp__paseo__send_agent_prompt",
                     "mcp__paseo__create_schedule", "mcp__paseo__update_schedule", "mcp__t3-code__delegate_task",
                     "mcp__magpie__Bash", "Bash", "PowerShell", "exec_command", "mcp__paseo__execute_code",
                     "spawn_agent", "invoke_subagent", "AskUserQuestion", "mcp__x__request_user_input_async",
                     "functions.request_user_input"):
            with self.subTest(name=name):
                self.assertIsNotNone(matcher.search(name))
        for name in ("ListAgents", "TaskStop", "mcp__magpie__TaskStop", "SendMessage", "Read",
                     "mcp__paseo__get_agent_status", "mcp__paseo__list_agents", "mcp__paseo__create_heartbeat",
                     "mcp__paseo__run_schedule_once", "SubagentHandback", "AgentX", "xAgent", "executor"):
            with self.subTest(name=name):
                self.assertIsNone(matcher.search(name))


class BoundDispatchGateTests(unittest.TestCase):
    def setUp(self):
        dispatch_registry = gate.router_module('dispatch_registry')
        account_fleet = gate.router_module('account_fleet')
        host_policy = gate.router_module('host_policy')
        self.registry = dispatch_registry
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        env = mock.patch.dict(os.environ, {
            'ROUTER_DISPATCH_DB': str(root / 'dispatch.sqlite3'),
            'ROUTER_OVERRIDES': str(root / 'overrides.json'),
            'ROUTER_CAPACITY': str(root / 'capacity.json'),
            'ROUTER_LEDGER': str(root / 'ledger.jsonl'),
            'ROUTER_USAGE_CACHE': str(root / 'usage.json'),
            'ROUTER_GATE_BYPASS': '0', 'PASEO_AGENT_ID': '',
            'ROUTER_HOST_POLICY': str(root / 'host-policy.json'),
            'AGENTCOMPASS_ACCOUNTS_OVERRIDE': str(root / 'accounts-override.json'),
            'ROUTER_ACCOUNTS_FILE': str(Path(__file__).resolve().parents[1] / 'skills' / 'agent-compass' / 'scripts' / 'fixtures' / 'reference-accounts.json'),
        })
        env.start()
        self.addCleanup(env.stop)
        policy = mock.patch.object(host_policy, 'load_policy', return_value=None)
        policy.start()
        self.addCleanup(policy.stop)
        accounts = account_fleet.load_accounts(account_fleet.reference_fleet_path())
        bundle = dict(account_fleet.bundles(accounts)['argon_high'], bundle_id='argon_high')
        self.task = {'parent_agent_id': 'parent-fixture', 'task_id': 'task-fixture',
                     'work_kind': 'general_implementation', 'expected_hours': 0.5, 'needs': {'local_only': True},
                     'profile': {'work_kind': 'general_implementation', 'reasoning_depth': 2, 'code_complexity': 1,
                                 'needs_visual': False, 'needs_gui': False, 'local_only': True, 'wants_fast': False, 'read_only_review': False, 'access': 'write', 'expected_hours': 0.5,
                                 'hours_confidence': 'medium', 'urgency': 'normal', 'interaction_style': 'engineering',
                                 'multi_task': False, 'task_count': '1',
                                 'axis_demand': {'divergence': 0, 'coding': 1, 'aesthetics': 0, 'tool_operation': 0}}}
        self.now = dt.datetime.now(UTC)
        decision = dispatch_registry.issue({'choice': 'argon_high', 'selected_bundle': bundle}, self.task, self.now)
        self.row = dispatch_registry.prepare(decision['decision_id'], self.task,
            {'title': 'fixture', 'initialPrompt': 'do fixture work', 'cwd': str(root)},
            {'read_ok': True, 'accounts': {'antigravity': {'windows': [
                {'id': wid, 'remaining_pct_before_reset': 80, 'observed_remaining_pct': 80, 'seconds_until_reset': hours * 3600,
                 'resets_at': (self.now + dt.timedelta(hours=hours)).isoformat()}
                for wid, hours in (('gemini-5h', 3), ('gemini-weekly', 100))]}}}, accounts, self.now)

    def payload(self, request=None, **changes):
        return dict({'tool_name': 'mcp__paseo__create_agent',
            'tool_input': request or self.row['request'], 'parent_agent_id': 'parent-fixture',
            'tool_use_id': 'call-a'}, **changes)

    def test_modern_paseo_requires_supported_labels_not_global_ledger(self):
        request = dict(self.row['request'])
        request.pop('labels')
        denied = gate.evaluate_payload(self.payload(request), now=self.now)
        self.assertEqual(denied['decision'], 'block')
        self.assertIn('supports labels', denied['reason'])

    def test_route_judge_mention_does_not_hide_a_later_shell_launch(self):
        for command in ('echo route_judge.py && paseo run --provider codex/gpt-6-astra task',
                        'python route_judge.py plan --input task.json; paseo run --provider codex/gpt-6-astra task'):
            trigger = gate.detect_subagent_launch('exec_command', {'cmd': command})
            self.assertEqual(trigger, 'shell:paseo agent create')
            denied = gate.evaluate_payload({'tool_name': 'exec_command', 'tool_input': {'cmd': command}}, now=self.now)
            self.assertEqual(denied['decision'], 'block')

    def test_quoted_separators_are_argument_text_not_new_commands(self):
        for command, expected in (
                ('grep -n "a\\|b" ~/.codex/skills/dot-subagent/scripts/run_dot_task.py', None),
                ("rg 'x|y' scripts/run_dot_task.py", None),
                ('bash -c "echo hi; paseo run --provider codex/gpt-6-astra task"', 'shell:paseo agent create'),
                ('grep -n "a|b" x.py; paseo run --provider codex/gpt-6-astra task', 'shell:paseo agent create')):
            self.assertEqual(gate.detect_subagent_launch('exec_command', {'cmd': command}), expected, command)

    def test_labelled_launch_works_without_any_legacy_ledger_and_is_single_use(self):
        self.assertEqual(gate.evaluate_payload(self.payload(), now=self.now), {})
        self.assertEqual(gate.evaluate_payload(self.payload(), now=self.now), {})
        denied = gate.evaluate_payload(self.payload(tool_use_id='call-b'), now=self.now)
        self.assertEqual(denied['decision'], 'block')
        self.assertIn('already claimed', denied['reason'])

    def test_labels_cannot_authorize_another_account_task_bundle_or_parent(self):
        for key in ('router.account', 'router.task_id', 'router.parent_id', 'router.decision_id'):
            args = json.loads(json.dumps(self.row['request']))
            args['labels'][key] = 'unrelated'
            self.assertEqual(gate.evaluate_payload(self.payload(args), now=self.now)['decision'], 'block')
        wrong_bundle = dict(self.row['request'], provider='antigravity-resilient/gemini-3.8-flash-high')
        self.assertEqual(gate.evaluate_payload(self.payload(wrong_bundle), now=self.now)['decision'], 'block')
        self.assertEqual(gate.evaluate_payload(self.payload(parent_agent_id='other-parent'), now=self.now)['decision'], 'block')
        self.assertEqual(self.registry.inspect(self.row['dispatch_id'])['state'], 'prepared')

    def test_literal_wrapper_binds_routed_settings_not_assignment_text(self):
        def payload(args):
            return {'toolName': 'codemode', 'input': {'code': 'await create_agent(' + json.dumps(args) + ')'},
                    'parent_agent_id': 'parent-fixture'}
        other_effort = dict(self.row['request'], settings=dict(self.row['request']['settings'], thinkingOptionId='low'))
        self.assertTrue(gate.evaluate_payload(payload(other_effort), now=self.now)['block'])
        revised = dict(self.row['request'], initialPrompt='revised work', title='revised', workspaceId='other')
        self.assertEqual(gate.evaluate_payload(payload(revised), now=self.now), {'block': False})

    def test_dynamic_wrapper_cannot_change_a_partially_parsed_bound_request(self):
        literal = json.dumps(self.row['request'])
        for expression in (literal[:-1] + ', ...changes}',
                           literal[:-1] + ', [key]: value}',
                           literal[:-1] + ', initialPrompt: taskPrompt}',
                           literal + ' || otherRequest'):
            payload = {'toolName': 'codemode', 'input': {'code': 'await create_agent(' + expression + ')'},
                       'parent_agent_id': 'parent-fixture'}
            self.assertTrue(gate.evaluate_payload(payload, now=self.now)['block'])
        self.assertEqual(self.registry.inspect(self.row['dispatch_id'])['state'], 'prepared')


class UsageRowTests(unittest.TestCase):
    """The hook reads quota only from the canonical router cache (or an explicit named file)."""

    def setUp(self):
        self.state = Path(os.environ["ROUTER_STATE_DIR"])
        self.canonical = self.state / "usage-cache.json"
        self.old_float = gate.HOME / ".usage-float" / "usage-cache.json"
        self.addCleanup(lambda: [path.unlink() for path in (self.canonical, self.old_float) if path.exists()])
        os.environ.pop("ROUTER_USAGE_CACHE", None)

    def write(self, path, used, fetched):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"providers": {"codex": {"fetched_at": fetched, "windows": [{"id": "5h", "used_pct": used}]}}}),
                        encoding="utf-8")

    def test_canonical_cache_or_named_file_is_read_and_old_usage_float_copy_is_ignored(self):
        self.write(self.old_float, 90, "2026-01-02T00:00:00+00:00")
        self.assertIsNone(gate.usage_row("codex"))
        self.write(self.canonical, 10, "2026-01-01T00:00:00+00:00")
        self.assertEqual(gate.usage_row("codex")["windows"][0]["used_pct"], 10)
        named = self.state / "named.json"
        self.write(named, 5, "2026-01-01T00:00:00+00:00")
        self.addCleanup(named.unlink)
        self.assertEqual(gate.usage_row("codex", named)["windows"][0]["used_pct"], 5)
        self.assertIsNone(gate.usage_row("codex", self.state / "missing.json"))


class DenyMessageEntryTests(unittest.TestCase):
    """The hook tells a blocked agent to run the public command, not a script path."""

    def test_deny_texts_name_the_public_command_only(self):
        self.assertTrue(gate.ROUTER_CMD == "agentcompass" or gate.ROUTER_CMD.endswith("/agentcompass"))
        texts = [gate.build_deny_reason("Agent", 900), gate.DISABLED_RECOVERY]
        for text in texts:
            self.assertIn(gate.ROUTER_CMD + " roll --brief", text)
            self.assertNotIn("route_judge", text)
            self.assertNotIn(".py", text)


class ConfiguredRotationThresholdGateTests(unittest.TestCase):
    def setUp(self):
        import shutil

        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.agents_dir = self.tmp / ".paseo" / "agents"
        (self.agents_dir / "ws").mkdir(parents=True, exist_ok=True)
        self.wid = "88888888-4444-4444-8888-999999999999"
        (self.agents_dir / "ws" / f"{self.wid}.json").write_text(
            json.dumps({
                "id": self.wid,
                "provider": "claude",
                "lastStatus": "idle",
                "labels": {"router.parent_id": "parent-main"},
            }),
            encoding="utf-8",
        )

    def test_gate_uses_configured_threshold_and_surfaces_missing_key(self):
        account_fleet = gate.router_module("account_fleet")
        worker_sessions = gate.router_module("worker_sessions")
        custom_accounts = {"scoring": {"rotate_after_carried_tokens": 750_000}, "accounts": {}}
        with mock.patch.object(account_fleet, "load_accounts", return_value=custom_accounts):
            with mock.patch.object(
                worker_sessions,
                "session_state",
                return_value={"compactions": 1, "compacted_tokens": 600_000, "context_tokens": 100_000, "historical_context_tokens": 700_000},
            ):
                self.assertIsNone(
                    gate.check_worker_compaction_limit(self.wid, paseo_agents_dir=self.agents_dir, home=self.tmp, caller_id="parent-main")
                )
            with mock.patch.object(
                worker_sessions,
                "session_state",
                return_value={"compactions": 1, "compacted_tokens": 700_000, "context_tokens": 100_000, "historical_context_tokens": 800_000},
            ):
                reason = gate.check_worker_compaction_limit(self.wid, paseo_agents_dir=self.agents_dir, home=self.tmp, caller_id="parent-main")
                self.assertIsNotNone(reason)
                self.assertIn("historical_context_tokens=800,000/750,000", reason)
                self.assertIn("rotate_after_carried_tokens=750,000", reason)

        with mock.patch.object(account_fleet, "load_accounts", return_value={"scoring": {}, "accounts": {}}), \
                mock.patch.object(worker_sessions, "session_state", return_value={"historical_context_tokens": 800_000}):
            with self.assertRaises(KeyError):
                gate.check_worker_compaction_limit(self.wid, paseo_agents_dir=self.agents_dir, home=self.tmp, caller_id="parent-main")


if __name__ == "__main__":
    unittest.main()
