import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "agent-compass" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import provider_inventory  # noqa: E402

from agentcompass import doctor


class DoctorTests(unittest.TestCase):
    def test_render_reports_a_busy_port_from_the_given_report(self):
        text = doctor.render({
            "version": "0.1.0",
            "python": "3.11.0 (python)",
            "install": "package test",
            "skill": "missing",
            "clis": [("git", "not found")],
            "tools": ["  uv: missing"],
            "accounts": [{"name": "demo", "family": "codex", "status": "signed-in", "plan": None, "home": "x"}],
            "cache": "missing (cache)",
            "plugins": ["  directories: none"],
            "ports": {"ports": {"dot2api": 8788}, "problems": []},
            "listeners": {8788: [("127.0.0.1", 1)]},
            "conflicts": [],
            "hooks": [],
        })
        self.assertIn("8788 in use", text)
        self.assertNotIn("credential_key", text)
        self.assertNotIn("api_key", text)

    def test_collect_on_an_isolated_home_does_not_create_files(self):
        with TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            home.mkdir()
            report = doctor.collect(home)
            self.assertTrue(report['cache'].startswith('missing'), report['cache'])
            created = [path for path in home.rglob("*")]
            self.assertEqual(created, [])
            text = doctor.render(report)
            self.assertNotIn("credential_key", text)
            self.assertNotIn("api_key", text)
            self.assertIn("doctor", text)
            self.assertFalse((home / ".agent-compass" / "ports.json").exists())

    def test_render_uses_the_same_unverified_sign_in_words_as_setup(self):
        row = {"family": "opencode", "status": "ready", "sources": ["native"]}
        label = provider_inventory.account_state_label(row)
        self.assertEqual(label, "installed, sign-in unverified")
        text = doctor.render({
            "version": "0.1.0",
            "python": "3.11.0 (python)",
            "install": "package test",
            "skill": "missing",
            "clis": [],
            "tools": [],
            "accounts": [{"name": "opencode", "family": "opencode", "status": label, "plan": "free", "home": "x"}],
            "cache": "missing",
            "plugins": [],
            "ports": {"ports": {}, "problems": []},
            "listeners": {},
            "conflicts": [],
            "hooks": [],
        })
        self.assertIn("opencode: installed, sign-in unverified", text)

    def test_doctor_names_the_active_install_and_an_agentcompass_that_hides_the_installer(self):
        import os
        from unittest import mock
        from agentcompass import install
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            bundled = base / "Local" / "AgentCompass" / "cli" / "agentcompass.exe"
            pip_bin = base / "Python" / "Scripts" / "agentcompass.exe"
            for exe in (bundled, pip_bin, base / "Python" / "python.exe"):
                exe.parent.mkdir(parents=True, exist_ok=True)
                exe.write_bytes(b"MZ")
            env = {"LOCALAPPDATA": str(base / "Local"), "PATHEXT": ".EXE",
                   "PATH": os.pathsep.join([str(pip_bin.parent), str(bundled.parent)])}
            with mock.patch.object(install, "_registry_install_dir", return_value=None):
                lines = doctor.install_lines(env)
        text = "\n".join(lines)
        self.assertIn("active: checkout", text)
        self.assertIn(f"installer exe: {bundled}", text)
        self.assertIn(f"WARNING: a new terminal runs {pip_bin}", text)
        self.assertIn("-m pip uninstall -y agentcompass", text)

    def test_hooks_pointing_at_python_while_the_installer_exists_are_flagged(self):
        from unittest import mock
        from agentcompass import install
        with TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            exe = Path(tmp) / "AgentCompass" / "cli" / "agentcompass.exe"
            settings.write_text('{"command": "C:/Python/python.exe -X utf8 E:/repo/setup/router_gate_hook.py"}', encoding="utf-8")
            with mock.patch.object(install, "bundled_cli", return_value=exe):
                self.assertIn("not the installer's agentcompass.exe", doctor._hook_installed(settings))
                settings.write_text('{"command": "%s gate-hook"}' % exe.as_posix(), encoding="utf-8")
                self.assertEqual(doctor._hook_installed(settings), "installed")

    def test_without_a_checkout_a_tool_in_its_installers_own_location_is_found_off_path(self):
        # Bun's installer puts bun.exe in ~/.bun/bin; the tray finds it there, so doctor must not call it missing.
        from unittest import mock
        with TemporaryDirectory() as tmp:
            bun = Path(tmp) / ".bun" / "bin" / "bun.exe"
            bun.parent.mkdir(parents=True)
            bun.write_bytes(b"")
            with mock.patch.object(doctor, "_which", return_value=None), \
                    mock.patch.object(Path, "home", return_value=Path(tmp)):
                lines = doctor._tool_lines(None)
        self.assertIn(f"  bun: {bun}", lines)
        self.assertTrue(any(line.startswith("  uv: missing") for line in lines))


if __name__ == "__main__":
    unittest.main()
