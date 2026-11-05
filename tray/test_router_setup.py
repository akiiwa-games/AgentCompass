"""Tray first-run router setup: offered once while the router is missing, previews before it writes, uses the installer's exe."""
import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import router_setup
import updates


class RouterSetupOfferTests(unittest.TestCase):
    def test_offered_once_and_only_while_the_router_is_missing(self):
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertTrue(router_setup.should_offer(home, environ={}))
            router_setup.record("declined", home)
            self.assertFalse(router_setup.should_offer(home, environ={}))
            state = json.loads((home / ".agent-compass" / "router-setup.json").read_text(encoding="utf-8"))
            self.assertEqual((state["offered"], state["outcome"]), (True, "declined"))

            fresh = home / "other"
            scripts = fresh / ".codex" / "skills" / "agent-compass" / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "route_judge.py").write_text("", encoding="utf-8")
            self.assertTrue(router_setup.router_ready(fresh, environ={}))
            self.assertFalse(router_setup.should_offer(fresh, environ={}))

    def test_the_packaged_tray_runs_the_cli_beside_it(self):
        with TemporaryDirectory() as tmp:
            app = Path(tmp) / "AgentCompass"
            (app / "cli").mkdir(parents=True)
            (app / "cli" / "agentcompass.exe").write_bytes(b"MZ")
            command = router_setup.cli_command(frozen=True, executable=str(app / "AgentCompass.exe"))
            self.assertEqual(command, [str((app / "cli" / "agentcompass.exe").resolve())])

    def test_preview_is_a_dry_run_and_the_hook_follows_the_checkbox(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "would write ~/.claude/CLAUDE.md\n", "")

        code, text = router_setup.run_setup(False, True, base=["agentcompass.exe"], runner=runner)
        self.assertEqual((code, text), (0, "would write ~/.claude/CLAUDE.md"))
        router_setup.run_setup(True, False, base=["agentcompass.exe"], runner=runner)
        self.assertEqual(calls, [["agentcompass.exe", "setup", "--yes", "--no-hooks", "--dry-run"],
                                 ["agentcompass.exe", "setup", "--yes", "--hooks"]])


class InstallerOnlyVersionDisplayTests(unittest.TestCase):
    def _installed(self, base: Path) -> tuple[Path, Path]:
        app = base / "AgentCompass"
        cli = app / "cli" / "agentcompass.exe"
        cli.parent.mkdir(parents=True)
        (app / "AgentCompass.exe").write_bytes(b"MZ")
        (app / "unins000.exe").write_bytes(b"MZ")
        cli.write_bytes(b"MZ")
        skill = app / "cli" / "_internal" / "agentcompass" / "data" / "skills" / "agent-compass" / "scripts"
        skill.mkdir(parents=True)
        (skill / "route_judge.py").write_text("", encoding="utf-8")
        return app, cli

    def test_installer_hooks_skills_and_path_are_one_install(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            app, cli = self._installed(base)
            home = base / "home"
            (home / ".claude").mkdir(parents=True)
            (home / ".claude" / "settings.json").write_text(json.dumps(
                {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": f"{cli.as_posix()} gate-hook"}]}]}}), encoding="utf-8")
            with mock.patch.object(updates, "local_version", return_value="0.3.11"), \
                    mock.patch.dict("sys.modules", {"router_priority": mock.Mock(modules=lambda: (_ for _ in ()).throw(RuntimeError()))}):
                found = updates.inspect_installs(root=app, home=home, which_path=cli, frozen=True)
            self.assertEqual(found["routers"]["hooks"]["shape"], "installer")
            self.assertEqual(found["routers"]["path"]["shape"], "installer")
            self.assertEqual(found["separate_routers"], [])
            self.assertEqual(found["display_version"], "AgentCompass 0.3.11")

    def test_the_running_trays_own_install_reports_its_version_not_0_0_0(self):
        # No local_version mock: in the real bundle the package sits in PyInstaller's archive, so the installer
        # folder has no __init__.py to read. The hook, skill and PATH entries are the tray's own install.
        import agentcompass
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            app, cli = self._installed(base)
            home = base / "home"
            (home / ".claude").mkdir(parents=True)
            (home / ".claude" / "settings.json").write_text(json.dumps(
                {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": f"{cli.as_posix()} gate-hook"}]}]}}), encoding="utf-8")
            (home / ".codex" / "skills").mkdir(parents=True)
            with mock.patch.object(updates.component_launch, "frozen", return_value=True), \
                    mock.patch.object(updates.sys, "executable", str(app / "AgentCompass.exe")), \
                    mock.patch.dict("sys.modules", {"router_priority": mock.Mock(modules=lambda: (_ for _ in ()).throw(RuntimeError()))}):
                found = updates.inspect_installs(home=home, which_path=cli)
            self.assertEqual(found["routers"]["hooks"]["version"], agentcompass.__version__)
            self.assertEqual(found["routers"]["path"]["version"], agentcompass.__version__)
            self.assertEqual(found["display_version"], f"AgentCompass {agentcompass.__version__}")


if __name__ == "__main__":
    unittest.main()
