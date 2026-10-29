"""Onboarding and host-sync contracts, run in temporary homes with every CLI lookup mocked; never touch the real home."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SETUP = Path(__file__).resolve().parent
sys.path.insert(0, str(SETUP))

import host_rules  # noqa: E402
import onboard  # noqa: E402
import sync_host_globals as sg  # noqa: E402

CLAUDE_CREDENTIALS = {"claudeAiOauth": {"accessToken": "SECRET-claude", "subscriptionType": "pro"}}


class HomeCase(unittest.TestCase):
    """A throwaway home with no CLI on PATH; `which` answers from `self.cli`."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="onboard-test-")
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        self.cli: dict[str, str] = {}
        self.enterContext(patch.object(sg.shutil, "which", side_effect=lambda name, path=None: self.cli.get(name)))
        self.enterContext(patch.object(sg, "ensure_paseo_bridge_dependencies"))
        self.enterContext(patch.object(sg, "HOME", self.home))
        sg.set_home(self.home)
        self.addCleanup(sg.set_home, None)

    def write(self, rel, text):
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text if isinstance(text, str) else json.dumps(text), encoding="utf-8", newline="\n")
        return path

    def quiet(self, fn, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(*args, **kwargs)

    def sync(self, **kwargs):
        return self.quiet(sg.sync, **kwargs)

    def backups(self):
        root = self.home / ".route-manager" / "backups"
        return sorted(p.name for p in root.iterdir()) if root.is_dir() else []


class AskerTests(unittest.TestCase):
    def test_closed_stdin_falls_back_to_the_default(self):
        asker = onboard.Asker(assume_yes=False)
        asker.interactive = True
        with patch("builtins.input", side_effect=EOFError), contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(asker.ask("Route work?", True))
            self.assertFalse(asker.ask("Install hook?", False))
        self.assertFalse(asker.interactive)


class MarkerBlockTests(unittest.TestCase):
    def test_new_or_empty_file_gets_only_the_block(self):
        text, mode = sg.merge_block(None, "rules\n")
        self.assertEqual((text, mode), (f"{sg.MARKER_BEGIN}\nrules\n{sg.MARKER_END}\n", "block"))
        self.assertEqual(sg.merge_block("  \n", "rules\n")[0], text)

    def test_user_text_outside_the_block_is_preserved_on_append_and_replace(self):
        appended, mode = sg.merge_block("# mine\n", "v1\n")
        self.assertEqual(mode, "block")
        self.assertTrue(appended.startswith("# mine\n\n" + sg.MARKER_BEGIN))
        edited = "intro\n\n" + sg.make_block("v1") + "\noutro\n"
        replaced, _ = sg.merge_block(edited, "v2\n")
        self.assertEqual(replaced, "intro\n\n" + sg.make_block("v2") + "\noutro\n")
        self.assertEqual(sg.merge_block(replaced, "v2\n")[0], replaced)      # idempotent

    def test_generated_text_is_wholly_managed(self):
        self.assertEqual(sg.merge_block("generated\n", "generated\n"), ("generated\n", "whole"))
        self.assertEqual(sg.merge_block("generated\r\n", "generated\n"), ("generated\n", "whole"))
        self.assertEqual(sg.merge_block("old text", "new text", recorded_whole_sha=sg.sha256("old text")), ("new text", "whole"))

    def test_earlier_generation_of_the_same_document_is_replaced_not_duplicated(self):
        legacy = "# Same heading\n\nold body\n"
        self.assertEqual(sg.merge_block(legacy, "# Same heading\n\nnew body\n"), ("# Same heading\n\nnew body\n", "whole"))
        text, mode = sg.merge_block("# My own heading\n", "# Same heading\n\nnew body\n")
        self.assertEqual(mode, "block")

    def test_unbalanced_marker_is_refused(self):
        with self.assertRaises(ValueError):
            sg.merge_block(f"x\n{sg.MARKER_BEGIN}\nhalf\n", "rules\n")

    def test_legacy_generated_rules_migrate_to_one_block_when_overlay_changes(self):
        for legacy in ("【全局强制纪律】旧规则", "# 全局执行纪律（旧宿主）\n旧内容\n",
                       host_rules.DEFAULT_SHARED, host_rules.DEFAULT_DAEMON_APPEND):
            with self.subTest(legacy=legacy[:40]):
                text, mode = sg.merge_block(legacy, "# New overlay\nnew rules\n")
                self.assertEqual((text, mode), (sg.make_block("# New overlay\nnew rules\n"), "block"))
                self.assertEqual(sg.merge_block(text, "# New overlay\nnew rules\n"), (text, "block"))

    def test_strip_block_restores_the_original_text(self):
        original = "# mine\n"
        merged, _ = sg.merge_block(original, "rules\n")
        self.assertEqual(sg.strip_block(merged), original)
        self.assertIsNone(sg.strip_block(original))


class RuleSourceTests(HomeCase):
    def test_default_is_neutral_english_without_personal_notes(self):
        text, source = host_rules.load("shared", self.home)
        self.assertEqual(source, "default")
        self.assertNotIn("@@", text)
        self.assertNotRegex(text, r"[\u4e00-\u9fff]")
        self.assertNotRegex(text, r"20\d\d-\d\d-\d\d")
        for needle in ("roll --brief", "launched", "dispatch_mode", "caller_action", "bypassPermissions", "Parent session agent ID",
                       "notifyOnFinish", "background process"):
            self.assertIn(needle.lower(), text.lower())
        self.assertIn(host_rules.ROUTE_JUDGE, text)

    def test_overlay_replaces_the_default_and_fills_placeholders(self):
        overlay = self.write(".route-manager/host-rules/codex.md", "custom @@HOME_POSIX@@ @@ROUTER_HOOK_SCRIPT@@\n")
        text, source = host_rules.load("codex", self.home)
        self.assertEqual(source, "overlay")
        self.assertEqual(text, f"custom {self.home.as_posix()} {host_rules.ROUTER_HOOK_SCRIPT}\n")
        overlay.write_text("   \n")
        self.assertEqual(host_rules.load("codex", self.home)[1], "default")

    def test_author_overlay_round_trip_is_byte_identical(self):
        legacy = "# 全局执行纪律\n\n门禁 @@ROUTER_HOOK_SCRIPT@@\n"
        self.write(".route-manager/host-rules/shared.md", legacy)
        rendered = legacy.replace("@@ROUTER_HOOK_SCRIPT@@", host_rules.ROUTER_HOOK_SCRIPT)
        claude = self.write(".claude/CLAUDE.md", rendered)
        before = claude.read_bytes()
        self.sync()
        self.assertEqual(claude.read_bytes(), before)           # whole-file managed: no markers added
        self.assertEqual(self.backups(), [])                    # nothing changed, nothing backed up


class SyncTests(HomeCase):
    def test_new_user_files_are_created_with_markers_and_nothing_else_is_touched(self):
        self.cli = {"claude": "claude"}
        self.sync()
        text = (self.home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith(sg.MARKER_BEGIN) and text.rstrip().endswith(sg.MARKER_END))
        for absent in (".codex", ".pi", ".gemini", ".config"):
            self.assertFalse((self.home / absent).exists(), absent)

    def test_existing_user_text_survives_and_is_backed_up(self):
        self.write(".claude/CLAUDE.md", "# my rules\n")
        self.sync()
        text = (self.home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# my rules\n\n" + sg.MARKER_BEGIN))
        backup = next((self.home / ".route-manager" / "backups").glob("host-setup-*/docs/claude-CLAUDE.md"))
        self.assertEqual(backup.read_text(encoding="utf-8"), "# my rules\n")

    def test_second_run_is_idempotent_and_every_changing_run_gets_its_own_backup_dir(self):
        self.write(".claude/CLAUDE.md", "# mine\n")
        self.sync()
        first = self.backups()
        before = (self.home / ".claude" / "CLAUDE.md").read_bytes()
        self.sync()
        self.assertEqual((self.home / ".claude" / "CLAUDE.md").read_bytes(), before)
        self.assertEqual(self.backups(), first)                   # no change, no new backup directory
        self.write(".route-manager/host-rules/shared.md", "# changed rules\n")
        self.sync()
        second = self.backups()
        self.assertEqual(len(second), 2)                          # an older backup never suppresses a new one
        self.assertTrue(all(name.startswith("host-setup-") and name != "host-cleanup-20261014" for name in second))

    def test_hooks_are_opt_in_and_existing_hooks_keep_being_refreshed(self):
        self.cli = {"claude": "claude"}
        self.sync()
        self.assertFalse(sg.hooks_installed())
        self.sync(hooks=True)
        self.assertTrue(sg.hooks_installed())
        settings = json.loads((self.home / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertIn("router_gate_hook.py", json.dumps(settings))
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "X", "hooks": [{"type": "command", "command": "python old/router_gate_hook.py"}]}]}}), encoding="utf-8")
        self.sync()                                                # hooks=None: installed, so refreshed
        refreshed = json.loads((self.home / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(refreshed["hooks"]["PreToolUse"][0]["hooks"][0]["command"], sg.ROUTER_HOOK_CMD)
        self.sync(hooks=False)
        self.assertTrue(sg.hooks_installed())                      # --no-hooks never removes; uninstall does

    def test_dry_run_changes_nothing(self):
        self.cli = {"claude": "claude", "codex": "codex"}
        lines = self.sync(dry_run=True)
        self.assertTrue(any("[docs]" in line for line in lines))
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), [])

    def test_paseo_prompt_is_a_block_that_keeps_the_users_own_prompt(self):
        self.write(".paseo/config.json", {"daemon": {"appendSystemPrompt": "Be terse."}, "other": 1})
        self.sync()
        prompt = json.loads((self.home / ".paseo" / "config.json").read_text(encoding="utf-8"))["daemon"]["appendSystemPrompt"]
        self.assertTrue(prompt.startswith("Be terse.\n\n" + sg.MARKER_BEGIN))
        self.assertIn("roll --brief", prompt)

    def test_legacy_paseo_prompt_is_replaced_once_and_backed_up(self):
        legacy = "【全局强制纪律】旧的单行规则"
        config = self.write(".paseo/config.json", {"daemon": {"appendSystemPrompt": legacy}, "other": 1})
        self.write(".route-manager/host-rules/daemon-append.md", "【全局强制纪律】新的单行规则\n")
        before = config.read_bytes()
        self.sync(skills=False)
        data = json.loads(config.read_text(encoding="utf-8"))
        self.assertEqual(data, {"daemon": {"appendSystemPrompt": sg.make_block("【全局强制纪律】新的单行规则\n")}, "other": 1})
        backup = next((self.home / ".route-manager/backups").glob("host-setup-*/configs/paseo-config.json"))
        self.assertEqual(backup.read_bytes(), before)
        after = config.read_bytes()
        self.sync(skills=False)
        self.assertEqual(config.read_bytes(), after)


class GeminiMaterializationTests(HomeCase):
    def test_materialization_skips_matching_content_and_repairs_stale_files_and_links(self):
        source = self.home / "source"
        self.write("source/SKILL.md", "v1")
        self.write("source/scripts/tool.py", "tool")
        hub = self.home / ".codex/skills"
        sg.make_link(hub / "route-agent-work", source)
        (self.home / ".gemini").mkdir()
        dst = self.home / ".gemini/skills/route-agent-work"
        with patch.object(sg, "SKILLS_TO_CODEX", ["route-agent-work"]):
            self.sync(docs=False)
            self.assertTrue(sg.materialized_skill_matches(dst, source))
            before = (dst / "SKILL.md").stat().st_mtime_ns
            backups = self.backups()
            with patch.object(sg, "remove_dir_or_junction", wraps=sg.remove_dir_or_junction) as remove:
                self.sync(docs=False)
                remove.assert_not_called()
            self.assertEqual((dst / "SKILL.md").stat().st_mtime_ns, before)
            self.assertEqual(self.backups(), backups)

            self.write("source/SKILL.md", "v2")
            dry = self.sync(docs=False, dry_run=True)
            self.assertTrue(any("[skills] Materialized" in line for line in dry))
            self.assertEqual((dst / "SKILL.md").read_text(), "v1")
            self.sync(docs=False)
            self.assertEqual((dst / "SKILL.md").read_text(), "v2")
            self.assertTrue(sg.materialized_skill_matches(dst, source))
            saved = list((self.home / ".route-manager/backups").glob("host-setup-*/skills/gemini-route-agent-work/SKILL.md"))
            self.assertEqual([p.read_text() for p in saved], ["v1"])
            self.assertEqual(Path(sg.junction_target(saved[0].parent / "scripts")), (source / "scripts").resolve())

            wrong = self.home / "wrong"
            wrong.mkdir()
            sg.remove_dir_or_junction(dst / "scripts")
            sg.make_link(dst / "scripts", wrong)
            self.assertFalse(sg.materialized_skill_matches(dst, source))
            self.sync(docs=False)
            self.assertTrue(sg.materialized_skill_matches(dst, source))


class UninstallTests(HomeCase):
    def snapshot(self):
        return {str(p.relative_to(self.home)): (p.read_bytes() if p.is_file() else None)
                for p in sorted(self.home.rglob("*")) if ".route-manager" not in p.parts}

    def test_uninstall_restores_a_pristine_home(self):
        self.cli = {"claude": "claude", "codex": "codex", "pi": "pi", "agy": "agy"}
        self.write(".claude/CLAUDE.md", "# mine\n")
        self.write(".claude/settings.json", {"theme": "dark", "hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]}]}})
        self.write(".codex/config.toml", "model = 'x'\n")
        self.write(".paseo/config.json", {"daemon": {"appendSystemPrompt": "Be terse."}, "agents": {"providers": {
            "antigravity": {"env": {"PATH": "C:\\a;C:\\codex-arg0\\x;C:\\a"}}}}})
        skill_home = self.home / "checkout-skills" / "route-agent-work"
        self.write("checkout-skills/route-agent-work/SKILL.md", "---\nname: route-agent-work\n---\n")
        before = self.snapshot()
        with patch.object(sg, "REPO_SKILLS", skill_home.parent):
            self.sync(hooks=True, link_repo_skills=True)
        self.assertNotEqual(self.snapshot(), before)
        self.assertTrue((self.home / ".codex" / "skills" / "route-agent-work").exists())
        self.quiet(sg.uninstall)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.home / ".route-manager" / "setup-manifest.json").exists())

    def test_uninstall_keeps_text_the_user_added_after_setup(self):
        self.cli = {"claude": "claude"}
        self.sync()
        path = self.home / ".claude" / "CLAUDE.md"
        path.write_text(path.read_text(encoding="utf-8") + "\nadded later\n", encoding="utf-8")
        self.quiet(sg.uninstall)
        self.assertEqual(path.read_text(encoding="utf-8"), "\nadded later\n")

    def test_whole_file_that_was_edited_since_setup_is_left_alone(self):
        self.write(".route-manager/host-rules/shared.md", "# Title\n\nbody\n")
        self.write(".claude/CLAUDE.md", "# Title\n\nold body\n")           # earlier generation: replaced as a whole
        self.sync()
        path = self.home / ".claude" / "CLAUDE.md"
        self.assertEqual(path.read_text(encoding="utf-8"), "# Title\n\nbody\n")
        path.write_text("# Title\n\nhand edited\n", encoding="utf-8")
        self.quiet(sg.uninstall)
        self.assertEqual(path.read_text(encoding="utf-8"), "# Title\n\nhand edited\n")

    def test_uninstall_without_manifest_is_a_no_op(self):
        self.write(".claude/CLAUDE.md", "# mine\n")
        self.quiet(sg.uninstall)
        self.assertEqual((self.home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8"), "# mine\n")


class DetectionTests(HomeCase):
    def detect(self, *tools):
        self.cli.update({tool: tool for tool in tools})       # `which` is mocked for the whole process
        return onboard.detect(self.home, {"PATH": ""})

    def test_empty_home_without_any_cli_has_nothing_signed_in(self):
        result = self.detect()
        self.assertFalse([r for r in result["accounts"] if r["signed_in"] is not False])
        self.assertFalse(result["paseo"]["installed"])

    def test_signed_in_claude_and_unverifiable_installed_clis(self):
        self.write(".claude/.credentials.json", CLAUDE_CREDENTIALS)
        result = self.detect("agy", "opencode")
        rows = {r["name"]: r for r in result["accounts"]}
        self.assertEqual((rows["claude"]["signed_in"], rows["claude"]["plan"]), (True, "pro"))
        self.assertIsNone(rows["antigravity"]["signed_in"])
        self.assertIsNone(rows["opencode"]["signed_in"])
        self.assertFalse(rows["codex"]["signed_in"])

    def test_inherited_environment_cannot_reach_another_users_accounts(self):
        other = self.home / "other"
        (other / ".paseo").mkdir(parents=True)
        (other / ".paseo/config.json").write_text(json.dumps({"agents": {"providers": {"codex-2": {
            "extends": "codex", "enabled": True, "env": {"CODEX_HOME": str(other / ".codex-2")}}}}}), encoding="utf-8")
        (other / ".codex-2").mkdir()
        (other / ".codex-2/auth.json").write_text(json.dumps({"tokens": {"account_id": "X", "access_token": "S"}}), encoding="utf-8")
        mine = self.home / "me"
        mine.mkdir()
        leaking = {"PATH": "", "PASEO_HOME": str(other / ".paseo"), "CODEX_HOME": str(other / ".codex-2"),
                   "CLAUDE_CONFIG_DIR": str(other / ".claude")}
        env = onboard.detection_environ(mine, leaking)
        for key in ("CODEX_HOME", "CLAUDE_CONFIG_DIR"):
            self.assertNotIn(key, env)
        self.assertEqual(Path(env["PASEO_HOME"]), mine / ".paseo")
        names = {r["name"]: r for r in onboard.detect(mine, leaking)["accounts"]}
        self.assertNotIn("codex-2", names)
        self.assertFalse(names["codex"]["signed_in"])


class OverrideTests(HomeCase):
    def detection(self):
        return {"accounts": [{"name": "claude", "signed_in": True}, {"name": "opencode", "signed_in": None},
                             {"name": "codex", "signed_in": False}]}

    def test_disabled_account_is_written_and_user_entries_are_never_overwritten(self):
        path = self.write(".route-manager/accounts-override.json", {"schema_version": 2, "accounts": {
            "claude": {"bundles": {"x": {"disabled": "mine"}}}, "opencode": {"verified": True}}})
        result = onboard.write_override(self.home, self.detection(), {"opencode", "claude"})
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(document["accounts"]["claude"]["bundles"], {"x": {"disabled": "mine"}})
        self.assertIs(document["accounts"]["claude"]["verified"], False)
        self.assertIs(document["accounts"]["opencode"]["verified"], True)   # set by the user: left alone
        self.assertEqual(result["added"], ["claude"])
        self.assertTrue(Path(result["backup"]).is_file())

    def test_re_enabling_removes_only_what_onboarding_added(self):
        onboard.write_override(self.home, self.detection(), {"opencode"})
        state = {"disabled_by_onboarding": ["opencode"]}
        self.write(".route-manager/onboarding.json", state)
        result = onboard.write_override(self.home, self.detection(), set())
        self.assertEqual(result["removed"], ["opencode"])
        self.assertNotIn("accounts", json.loads(onboard.override_path(self.home).read_text(encoding="utf-8")))

    def test_dry_run_writes_nothing(self):
        onboard.write_override(self.home, self.detection(), {"opencode"}, dry_run=True)
        self.assertFalse(onboard.override_path(self.home).exists())


class UsagePluginTests(HomeCase):
    def test_existing_checkout_is_used_and_collection_runs_once(self):
        plugin = self.home / ".route-manager" / "plugins" / "usage-float"
        (plugin / ".git").mkdir(parents=True)
        (plugin / "usage_float.py").write_text("", encoding="utf-8")
        self.cli["git"] = "git"
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            if command[-1] == "once":
                (self.home / ".usage-float").mkdir()
                (self.home / ".usage-float" / "usage-cache.json").write_text("{}", encoding="utf-8")
            return type("R", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

        found = onboard.ensure_usage_float(self.home, run=fake_run)
        self.assertEqual(found["action"], "updated")
        collection = onboard.collect_usage_once(self.home, found, run=fake_run)
        self.assertTrue(collection["ok"])
        self.assertEqual(calls[-1][-1], "once")

    def test_missing_checkout_is_cloned_from_the_public_repository(self):
        self.cli["git"] = "git"
        calls = []
        with patch.object(onboard, "REPO", self.home / "no-checkout"), patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ROUTE_MANAGER_GITHUB_ORG", None)
            os.environ.pop("ROUTE_MANAGER_USAGE_FLOAT_REPO", None)
            result = onboard.ensure_usage_float(self.home, run=lambda c, **k: calls.append(c) or type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())
        self.assertEqual(result["action"], "cloned")
        self.assertIn("https://github.com/akiiwa-games/usage-float.git", calls[0])

    def test_no_git_is_reported_not_raised(self):
        with patch.object(onboard, "REPO", self.home / "no-checkout"):
            self.assertEqual(onboard.ensure_usage_float(self.home)["action"], "skipped")


class SetupFlowTests(HomeCase):
    def run_setup(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(sys.stdin, "isatty", return_value=False):
            code = onboard.main(["setup", "--home", str(self.home), "--yes", "--no-usage", *argv])
        return code, out.getvalue()

    def test_yes_flow_writes_rules_override_and_no_hook(self):
        self.cli = {"claude": "claude"}
        self.write(".claude/.credentials.json", CLAUDE_CREDENTIALS)
        code, output = self.run_setup()
        self.assertEqual(code, 0)
        self.assertIn("claude       signed in", output)
        self.assertIn("router hook   : not installed", output)
        self.assertFalse(sg.hooks_installed())
        self.assertTrue((self.home / ".route-manager" / "onboarding.json").is_file())

    def test_uninstall_command_reverses_setup_including_the_override(self):
        self.cli = {"claude": "claude", "opencode": "opencode"}
        self.write(".claude/.credentials.json", CLAUDE_CREDENTIALS)
        self.run_setup("--disable", "opencode")
        override = onboard.override_path(self.home)
        self.assertIs(json.loads(override.read_text(encoding="utf-8"))["accounts"]["opencode"]["verified"], False)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(onboard.main(["uninstall", "--home", str(self.home)]), 0)
        self.assertFalse(override.exists())
        self.assertFalse((self.home / ".claude" / "CLAUDE.md").exists())
        self.assertFalse((self.home / ".route-manager" / "onboarding.json").exists())

    def test_dry_run_writes_nothing(self):
        self.cli = {"claude": "claude"}
        self.write(".claude/.credentials.json", CLAUDE_CREDENTIALS)
        before = sorted(str(p) for p in self.home.rglob("*"))
        code, output = self.run_setup("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("Dry run complete", output)
        self.assertEqual(sorted(str(p) for p in self.home.rglob("*")), before)

    def test_no_signed_in_cli_says_so(self):
        _, output = self.run_setup("--no-docs", "--no-skills")
        self.assertIn("No signed-in agent CLI was found", output)


if __name__ == "__main__":
    unittest.main()
