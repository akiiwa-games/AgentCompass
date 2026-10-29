import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "route-agent-work" / "scripts"
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
            created = [path for path in home.rglob("*")]
            self.assertEqual(created, [])
            text = doctor.render(report)
            self.assertNotIn("credential_key", text)
            self.assertNotIn("api_key", text)
            self.assertIn("doctor", text)
            self.assertFalse((home / ".route-manager" / "ports.json").exists())

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


if __name__ == "__main__":
    unittest.main()
