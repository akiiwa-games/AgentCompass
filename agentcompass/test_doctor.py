import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

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
            created = [path for path in home.rglob("*")]
            self.assertEqual(created, [])
            text = doctor.render(report)
            self.assertNotIn("credential_key", text)
            self.assertNotIn("api_key", text)
            self.assertIn("doctor", text)
            self.assertFalse((home / ".route-manager" / "ports.json").exists())


if __name__ == "__main__":
    unittest.main()
