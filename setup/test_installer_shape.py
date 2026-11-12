"""Setup run by the Windows installer's agentcompass.exe: hooks call the exe, rules say `agentcompass`, older entries are taken over.

Each case runs in a child Python that sets ``sys.frozen`` and ``sys.executable`` the way the frozen CLI does before the setup
modules are imported (they read both at import time). Everything happens under a temporary home.
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow

SETUP = Path(__file__).resolve().parent
REPO = SETUP.parent

PRELUDE = textwrap.dedent("""
    import json, pathlib, sys
    sys.frozen = True
    sys.executable = sys.argv[1]
    sys.path[:0] = [sys.argv[2], sys.argv[3]]
    home = pathlib.Path(sys.argv[4])
""")


class InstallerShapeSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="installer shape ")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / "home"
        (self.home / ".claude").mkdir(parents=True)
        (self.home / ".codex").mkdir(parents=True)
        self.exe = root / "App Data" / "AgentCompass" / "cli" / "agentcompass.exe"
        self.exe.parent.mkdir(parents=True)
        self.exe.write_bytes(b"MZ")
        self.env = {k: v for k, v in os.environ.items() if k.upper() in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT")}
        self.env.update(HOME=str(self.home), USERPROFILE=str(self.home), APPDATA=str(self.home / "AppData" / "Roaming"),
                        LOCALAPPDATA=str(self.home / "AppData" / "Local"), PATH="", ROUTER_PROVIDER_HOME=str(self.home),
                        ROUTER_STATE_DIR=str(self.home / ".agent-compass"), PASEO_HOME=str(self.home / ".paseo"))

    def child(self, body: str) -> dict:
        done = subprocess.run([sys.executable, "-X", "utf8", "-c", PRELUDE + textwrap.dedent(body), str(self.exe), str(SETUP),
                               str(REPO), str(self.home)], env=self.env, capture_output=True, text=True, encoding="utf-8", timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return json.loads(done.stdout.strip().splitlines()[-1])

    def test_hook_command_rules_and_pi_extension_use_the_exe(self):
        out = self.child("""
            import host_rules, sync_host_globals as sg
            print(json.dumps({"cmd": sg.ROUTER_HOOK_CMD, "argv": sg.ROUTER_HOOK_ARGV, "pi": sg.PI_ROUTER_GATE_TS,
                              "router": host_rules.agentcompass_command(),
                              "rendered": host_rules.render("run @@AGENTCOMPASS@@ roll; hook @@ROUTER_HOOK_SCRIPT@@", home)}))
        """)
        exe = self.exe.resolve().as_posix()
        self.assertEqual(out["argv"], [exe, "gate-hook"])
        self.assertIn("gate-hook", out["cmd"])
        self.assertNotIn("python", out["cmd"].lower())
        self.assertIn(json.dumps([exe, "gate-hook"]), out["pi"])
        self.assertNotIn("PYTHON_EXE", out["pi"])
        self.assertEqual(out["router"], "agentcompass")
        self.assertTrue(out["rendered"].startswith("run agentcompass roll; hook "), out["rendered"])
        self.assertIn("agentcompass.exe", out["rendered"])
        self.assertIn("gate-hook", out["rendered"])

    def test_setup_takes_over_a_checkout_hook_and_uninstall_removes_the_exe_hook(self):
        old = "C:/Python313/python.exe -X utf8 D:/work/agentcompass/setup/router_gate_hook.py"
        keep = {"type": "command", "command": "node C:/tools/other-hook.js", "timeout": 5}
        settings = {"model": "opus", "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": old}, keep]}]}}
        (self.home / ".claude" / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        out = self.child("""
            import sync_host_globals as sg
            sg.set_home(home)
            sg.begin_run()
            sg.configure_hooks([("claude", home / ".claude")])
            sg.MANIFEST["runs"].append({"id": sg.RUN_ID})
            sg.save_manifest()
            after = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
            installed = sg.hooks_installed([("claude", home / ".claude")])
            sg.set_home(home)
            sg.uninstall()
            removed = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
            print(json.dumps({"after": after, "installed": installed, "removed": removed}))
        """)
        commands = [h["command"] for g in out["after"]["hooks"]["PreToolUse"] for h in g["hooks"]]
        self.assertEqual(commands[0], f"'{self.exe.resolve().as_posix()}' gate-hook")
        self.assertNotIn(old, commands)
        self.assertIn(keep["command"], commands)
        self.assertEqual(out["after"]["model"], "opus")
        self.assertTrue(out["installed"])
        left = [h["command"] for g in out["removed"].get("hooks", {}).get("PreToolUse", []) for h in g["hooks"]]
        self.assertEqual(left, [keep["command"]])


if __name__ == "__main__":
    unittest.main()
