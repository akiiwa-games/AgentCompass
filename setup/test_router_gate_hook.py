import datetime as dt
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import router_gate_hook as gate

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 14, 6, 0, tzinfo=UTC)

SKILL_ROOT = Path(__file__).resolve().parents[1] / "skills/route-agent-work"


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
        DEFAULT_LEDGER=home / '.route-manager/router-ledger.jsonl',
        DEFAULT_USAGE_CACHE=home / '.usage-float/usage-cache.json',
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
            "ROUTER_PASEO_LEGACY_NO_LABELS": "1",
            "ROUTER_DISPATCH_DB": str(self.state / "dispatch.sqlite3"),
            "ROUTER_HOST_POLICY": str(self.state / "host-policy.json"),
            "ROUTER_ACCOUNTS_OVERRIDE": str(self.state / "accounts-override.json"),
            "ROUTER_ACCOUNTS_FILE": str(Path(__file__).resolve().parents[1] / "skills" / "route-agent-work" / "scripts" / "fixtures" / "reference-accounts.json"),
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
            for payload in (
                {'tool_name': 'functions.request_user_input_async', 'tool_input': {}},
                {'tool_name': 'AskUserQuestion', 'tool_input': {}},
                {'toolName': 'request_user_input_async', 'input': {}},
                {'toolCall': {'name': 'request_user_input', 'args': {}}},
                {'tool_name': 'exec', 'tool_input': 'await tools.request_user_input_async({questions: []})'},
            ):
                with self.subTest(labels=labels, payload=payload):
                    with mock.patch.dict(os.environ, {'PASEO_AGENT_ID': worker_id, 'ROUTER_WORKER': ''}):
                        denied = gate.evaluate_payload(payload)
                        self.assertIn('actual parent ID', denied['reason'])
                    with mock.patch.dict(os.environ, {'PASEO_AGENT_ID': '', 'ROUTER_WORKER': ''}):
                        allowed = gate.evaluate_payload(payload)
                        self.assertNotIn('reason', allowed)
            with mock.patch.dict(os.environ, {'PASEO_AGENT_ID': worker_id}):
                self.assertEqual(gate.evaluate_payload({'tool_name': 'exec', 'tool_input':
                    'text("request_user_input_async({questions: []})")'}), {})
                self.assertEqual(gate.evaluate_payload({'tool_name': 'send_agent_prompt', 'tool_input':
                    {'agentId': 'actual-parent', 'prompt': 'question', 'background': True, 'notifyOnFinish': False}}), {})
        with mock.patch.dict(os.environ, {'PASEO_AGENT_ID': '', 'ROUTER_WORKER': '1'}):
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
            for payload in (
                {'tool_name': 'send_agent_prompt', 'tool_input': args},
                {'tool_name': 'mcp__paseo__send_agent_prompt', 'tool_input': args},
                {'toolName': 'send_agent_prompt', 'input': args},
                {'toolCall': {'name': 'send_agent_prompt', 'args': args}},
                {'tool_name': 'exec', 'tool_input': 'await tools.mcp__paseo__send_agent_prompt(' + json.dumps(args) + ')'},
            ):
                with self.subTest(case=label, payload=payload), \
                        mock.patch.dict(os.environ, {'PASEO_AGENT_ID': caller}), \
                        mock.patch.object(gate, 'load_dispatch_settings', return_value=({}, None)):
                    result = gate.evaluate_payload(payload)
                    self.assertEqual('reason' in result, blocked)
                    if blocked:
                        self.assertIn('notifyOnFinish: true or omit it', result['reason'])

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
            for account_name, choice_name, agent_uuid in (
                ("claude-lab", "lab_sonnet55_high", "11111111-1111-4111-8111-111111111111"),
                ("claude-magpie", "sonnet55_high_claude_magpie", "22222222-2222-4222-8222-222222222222"),
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
                                            "used_pct": 85.0,
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
                # 1. Blocks new secondary Claude create_agent / CLI when five_hour >= 75% used even if ledger matches
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

                # 2. Blocks run_turn / send_message to existing secondary Claude Paseo agent when five_hour >= 75% used
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
                        "toolName": "codemode",
                        "input": {"code": f'await paseo.create_agent({{provider: "{account_name}", model: "claude-sonnet-5-5"}})'},
                    },
                    ledger_path=other_ledger,
                    usage_cache_path=cache,
                    paseo_agents_dir=paseo_dir,
                    now=T0,
                )
                self.assertTrue(res_mismatch["block"])
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
        ):
            self.assertIn("five_hour", gate.evaluate_payload(payload, now=T0)["reason"])
        self.exhausted_cache(stale=True)
        self.assertIn("five_hour", gate.evaluate_payload({"tool_name": "send_agent_prompt", "tool_input": {
            "agentId": agent_id, "prompt": "continue"}}, now=T0)["reason"])

    def test_pi_launch_namespace_matches_canonical_ledger_direct_and_codemode(self):
        # Actual ledger records retain canonical model, not bundle.launch_model.
        for prov, canonical_model, launch_model in (
            ("pi", "gpt-6-astra", "openai-codex/gpt-6-astra"),
            ("opencode", "deepseek-v4.1-flash-free", "opencode/deepseek-v4.1-flash-free"),
        ):
            ledger = self.write_ledger(str(self.state), [self.route(prov, canonical_model, "high")])
            for args in (
                {"provider": f"{prov}/{launch_model}"},
                {"provider": prov, "model": launch_model},
            ):
                args["settings"] = {"thinkingOptionId": "high"}
                direct = {"tool_name": "mcp__paseo__create_agent", "tool_input": args}
                wrapped = {"toolName": "codemode", "input": {
                    "code": "await paseo.create_agent(" + json.dumps(args) + ")"}}
                self.assertEqual(gate.evaluate_payload(direct, ledger_path=ledger, now=T0), {})
                self.assertEqual(gate.evaluate_payload(wrapped, ledger_path=ledger, now=T0), {"block": False})

    def test_pi_namespace_normalization_preserves_owner_model_and_effort_checks(self):
        ledger = self.write_ledger(str(self.state), [self.route("pi")])
        for provider, effort, diagnostic in (
            ("pi/openai-codex/gpt-6.1-sol", "high", "model"),
            ("pi/openai-codex/gpt-6-astra", "max", "effort"),
            ("codex/openai-codex/gpt-6-astra", "high", "provider"),
            ("pi/other-backend/gpt-6-astra", "high", "model"),
        ):
            args = {"provider": provider, "settings": {"thinkingOptionId": effort}}
            result = gate.evaluate_payload({"tool_name": "create_agent", "tool_input": args}, ledger_path=ledger, now=T0)
            self.assertIn(diagnostic, result["reason"])

    def test_antigravity_high_model_accepts_omitted_thinking_option(self):
        for provider in ("antigravity", "antigravity-resilient"):
            for model in ("gemini-4-argon-high", "gemini-3.8-flash-high"):
                ledger = self.write_ledger(str(self.state), [self.route(provider, model, "high")])
                args = {"provider": provider + "/" + model, "settings": {"modeId": "full-access"}}
                self.assertEqual(gate.evaluate_payload({"tool_name": "create_agent", "tool_input": args},
                                                      ledger_path=ledger, now=T0), {})
                self.assertEqual(gate.evaluate_payload({"toolName": "codemode", "input": {
                    "code": "await paseo.create_agent(" + json.dumps(args) + ")"}},
                    ledger_path=ledger, now=T0), {"block": False})

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
        (self.state / "accounts-override.json").write_text(json.dumps({"accounts": accounts or {}}), encoding="utf-8")

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
                "provider": f"claude-lab/claude-{model}-5-5", "settings": {"thinkingOptionId": "high"}}}

        resume_sonnet = (
            turn("sonnet"), turn("sonnet", "mcp__paseo__run_turn"), turn("sonnet", "send_message"),
            {"toolName": "codemode", "input": {"code":
                f'await tools.mcp__paseo__send_agent_prompt({{agentId: "{ids["sonnet"]}", prompt: "continue"}})'}})
        bound_sonnet = {"tool_name": "mcp__paseo__create_agent", "tool_input": {"provider": "claude-lab/claude-sonnet-5-5",
            "labels": {"router.account": "claude-lab", "router.bundle_id": "lab_sonnet55_high"}}}
        shell_launch = {"tool_name": "Bash", "tool_input": {"command": "paseo agent create --provider claude-lab/claude-sonnet-5-5"}}
        everyone = ["claude", "claude-lab", "codex", "antigravity", "dot"]
        # (settings, cause in the block reason, blocked calls, calls differing only in the judged condition)
        for settings, cause, blocked, passed in (
                ({"allowed": [a for a in everyone if a != "claude-lab"]}, "host-policy.json",
                 resume_sonnet + (turn("bare"), launch("sonnet"), bound_sonnet, shell_launch),
                 (turn("codex"), turn("sonnet", parent_agent_id=ids["child"]))),
                ({"accounts": {"claude-lab": {"bundles": {"lab_sonnet55_high": {"disabled": True},
                                                        "lab_opus55_high": {"disabled": False}}}}}, "档位 lab_sonnet55_high",
                 resume_sonnet + (turn("bare"), launch("sonnet"), bound_sonnet), (turn("opus"), launch("opus"))),
                ({"accounts": {"claude-lab": {"verified": False}}}, "verified: false",
                 (turn("bare"), launch("sonnet")), (turn("codex"),))):
            self.write_settings(**settings)
            for payload in blocked:
                with self.subTest(cause=cause, blocked=payload):
                    result = gate.evaluate_payload(payload, ledger_path=ledger, now=T0)
                    self.assertIn(cause, result["reason"])
                    self.assertIn("route_judge.py roll --brief", result["reason"])
            for payload in passed:
                with self.subTest(cause=cause, passed=payload):
                    self.assertNotIn("reason", gate.evaluate_payload(payload, ledger_path=ledger, now=T0))
        # Turning the account back on, or having no host policy, lets the same calls through.
        for settings in ({"allowed": everyone}, {}):
            self.write_settings(**settings)
            for payload in resume_sonnet + (turn("bare"), launch("sonnet"), shell_launch):
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
        allocator = gate.router_module('allocator')
        for settings, tokens, cause in (
                ({'allowed': []}, 0, 'host-policy.json'),
                ({'accounts': {'claude-lab': {'verified': False}}}, 0, 'verified: false'),
                ({'accounts': {'claude-lab': {'bundles': {'lab_sonnet55_high': {'disabled': True}}}}}, 0, '档位 lab_sonnet55_high'),
                ({}, 2_000_000, 'historical_context_tokens=2,000,000/2,000,000')):
            self.write_settings(**settings)
            with mock.patch.object(allocator, 'session_state', return_value={'context_tokens': tokens}):
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
                    for payload in payloads:
                        with self.subTest(gate=cause, case=label, payload=payload), \
                                mock.patch.dict(os.environ, {'PASEO_AGENT_ID': caller}):
                            result = gate.evaluate_payload(payload, now=T0)
                            self.assertEqual('reason' in result, blocked)
                            if blocked:
                                self.assertIn(cause, result['reason'])
                                self.assertIn('cancel_agent', result['reason'])
                                self.assertIn('route_judge.py roll --brief', result['reason'])

    def test_running_control_keeps_launch_quota_and_notification_checks(self):
        wid = self.existing_agent()
        labels = {'router.parent_id': 'parent', 'router.account': 'claude-lab', 'router.bundle_id': 'lab_sonnet55_high'}
        (self.state / 'agents' / 'project' / (wid + '.json')).write_text(json.dumps({
            'provider': 'claude-lab/claude-sonnet-5-5', 'labels': labels, 'lastStatus': 'running'}), encoding='utf-8')
        self.write_settings(allowed=[])
        with mock.patch.dict(os.environ, {'PASEO_AGENT_ID': 'parent'}):
            # Spoofing running state in a launch request cannot bypass account disablement.
            result = gate.evaluate_payload({'tool_name': 'create_agent', 'tool_input': {
                'provider': 'claude-lab/claude-sonnet-5-5', 'labels': labels, 'lastStatus': 'running'}}, now=T0)
            self.assertIn('host-policy.json', result['reason'])
            for notify, cause in ((False, 'notifyOnFinish: true or omit it'), (True, 'five_hour')):
                self.exhausted_cache()
                result = gate.evaluate_payload({'tool_name': 'send_agent_prompt', 'tool_input': {
                    'agentId': wid, 'prompt': 'wrap up', 'notifyOnFinish': notify}}, now=T0)
                self.assertIn(cause, result['reason'])

    def test_interleaved_decisions_match_older_route_in_both_orders(self):
        a = self.route(parent_agent_id="parent-a", task_id="task-a")
        b = self.route("dot", parent_agent_id="parent-b", task_id="task-b")
        for rows in ([a, b], [b, a]):
            ledger = self.write_ledger(str(self.state), rows)
            for identity in ({}, {"parentAgentId": "parent-a", "taskId": "task-a"}):
                result = gate.evaluate_payload({"tool_name": "create_agent", "tool_input": {
                    "provider": "codex/gpt-6-astra", "settings": {"thinkingOptionId": "high"}, **identity}},
                    ledger_path=ledger, now=T0)
                self.assertEqual(result, {})

    def test_same_task_new_decision_rejects_old_owner_with_diagnostics(self):
        identity = {"parent_agent_id": "parent-a", "task_id": "task-a"}
        ledger = self.write_ledger(str(self.state), [self.route(**identity), self.route("pi", **identity)])
        result = gate.evaluate_payload({"tool_name": "create_agent", "tool_input": {
            "provider": "codex/gpt-6-astra", "settings": {"thinkingOptionId": "high"}, **identity}},
            ledger_path=ledger, now=T0)
        self.assertEqual(result["decision"], "block")
        self.assertIn("provider: requested='codex', routed='pi'", result["reason"])
        self.assertIn("pi_choice", result["reason"])
        self.assertIn("dispatch", result["reason"])

    def test_parent_alone_does_not_collapse_concurrent_tasks(self):
        ledger = self.write_ledger(str(self.state), [self.route(parent_agent_id="a"), self.route("dot", parent_agent_id="a")])
        self.assertEqual(gate.evaluate_payload({"session_id": "a", "tool_name": "create_agent", "tool_input": {
            "provider": "codex/gpt-6-astra"}}, ledger_path=ledger, now=T0), {})

    def test_nested_task_correlation_and_ttl_are_enforced(self):
        ledger = self.write_ledger(str(self.state), [self.route(task={"task_id": "a"}), self.route("pi", task={"task_id": "a"})])
        payload = {"tool_name": "create_agent", "tool_input": {"provider": "codex/gpt-6-astra", "taskId": "a"}}
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
            result = gate.evaluate_payload({"tool_name": "create_agent", "tool_input": args}, ledger_path=ledger, now=T0)
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
        for code, allowed in (
            ("await paseo.create_agent({provider:'codex/gpt-6-astra', settings:{thinkingOptionId:'high'}})", True),
            ('await tools.mcp__paseo__create_agent({provider:"codex/gpt-6-astra", settings:{thinkingOptionId:"max"}})', False),
            ('await paseo.create_agent({provider:"codex/gpt-6-astra"}); await paseo.create_agent({provider:"pi/gpt-6-astra"})', False),
        ):
            result = gate.evaluate_payload({"tool_name": "exec", "tool_input": code}, ledger_path=ledger, now=T0)
            self.assertEqual(result == {}, allowed, result)
        # Quoted examples and readonly provider arguments are not executing calls.
        self.exhausted_cache()
        code = 'await paseo.list_models({provider:"claude-lab"}); await paseo.create_agent({provider:"codex/gpt-6-astra", initialPrompt:"create_agent({provider: \'claude-lab\'})"})'
        self.assertEqual(gate.evaluate_payload({"tool_name": "exec", "tool_input": code}, ledger_path=ledger, now=T0), {})
        quoted = '// create_agent({provider:"claude-lab"})\ntext("create_agent({})")'
        self.assertEqual(gate.evaluate_payload({"toolName": "codemode", "input": {"code": quoted}}, now=T0), {"block": False})

    def test_mixed_wrapper_still_checks_shell_launch(self):
        code = 'await paseo.send_agent_prompt({agentId:"unknown", prompt:"continue"}); await tools.exec_command({cmd:"codex exec -m gpt-6-astra \'task\'"})'
        result = gate.evaluate_payload({"tool_name": "exec", "tool_input": code}, now=T0)
        self.assertIn("shell:codex exec", result["reason"])

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
            self.assertIn("route_judge.py roll --brief", res["reason"])

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


class BoundDispatchGateTests(unittest.TestCase):
    def setUp(self):
        dispatch_registry = gate.router_module('dispatch_registry')
        allocator = gate.router_module('allocator')
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
            'ROUTER_GATE_BYPASS': '0', 'PASEO_AGENT_ID': '', 'ROUTER_PASEO_LEGACY_NO_LABELS': '0',
            'ROUTER_HOST_POLICY': str(root / 'host-policy.json'),
            'ROUTER_ACCOUNTS_OVERRIDE': str(root / 'accounts-override.json'),
            'ROUTER_ACCOUNTS_FILE': str(Path(__file__).resolve().parents[1] / 'skills' / 'route-agent-work' / 'scripts' / 'fixtures' / 'reference-accounts.json'),
        })
        env.start()
        self.addCleanup(env.stop)
        policy = mock.patch.object(allocator.host_policy, 'load_policy', return_value=None)
        policy.start()
        self.addCleanup(policy.stop)
        accounts = allocator.load_accounts(allocator.reference_fleet_path())
        bundle = dict(allocator.bundles(accounts)['argon_high'], bundle_id='argon_high')
        self.task = {'parent_agent_id': 'parent-fixture', 'task_id': 'task-fixture',
                     'work_kind': 'general_implementation', 'difficulty': 'hard', 'expected_hours': 0.5,
                     'needs': {'local_only': True}}
        self.now = dt.datetime.now(UTC)
        decision = dispatch_registry.issue({'choice': 'argon_high', 'selected_bundle': bundle}, self.task, self.now)
        self.row = dispatch_registry.prepare(decision['decision_id'], self.task,
            {'title': 'fixture', 'initialPrompt': 'do fixture work', 'workspaceId': 'workspace'},
            {'accounts': {}}, accounts, self.now)

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


if __name__ == "__main__":
    unittest.main()
