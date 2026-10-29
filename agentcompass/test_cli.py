import io
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from agentcompass import cli


def _fake_onboard(directory: Path) -> Path:
    path = directory / "onboard.py"
    path.write_text(
        "from pathlib import Path\n"
        f"LOG = Path(r'{directory}') / 'calls.txt'\n"
        "def setup_main(argv=None):\n"
        "    LOG.write_text('setup ' + repr(list(argv or [])), encoding='utf-8')\n"
        "    return 0\n"
        "def uninstall_main(argv=None):\n"
        "    LOG.write_text('uninstall ' + repr(list(argv or [])), encoding='utf-8')\n"
        "    return 0\n"
        "def main(argv=None):\n"
        "    LOG.write_text('main ' + repr(list(argv or [])), encoding='utf-8')\n"
        "    return 0\n",
        encoding="utf-8",
    )
    return path


class CliTests(unittest.TestCase):
    def test_setup_and_uninstall_call_the_onboard_entry_points(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = _fake_onboard(root)
            with patch("agentcompass.cli.locations.setup_script", return_value=path):
                self.assertEqual(cli.main(["setup", "--dry-run"]), 0)
                self.assertEqual((root / "calls.txt").read_text(encoding="utf-8"), "setup ['--dry-run']")
                self.assertEqual(cli.main(["uninstall", "--yes"]), 0)
                self.assertEqual((root / "calls.txt").read_text(encoding="utf-8"), "uninstall ['--yes']")

    def test_plan_command_is_an_isolated_interpreter(self):
        command = cli.route_judge_command("plan", ["--input", "examples/task.json", "--dry-run"])
        self.assertEqual(command[:4], [sys.executable, "-I", "-X", "utf8"])
        self.assertTrue(command[4].replace("\\", "/").endswith("skills/route-agent-work/scripts/route_judge.py"))
        self.assertEqual(command[5:], ["plan", "--input", "examples/task.json", "--dry-run"])

    def test_providers_is_passed_through_to_route_judge(self):
        with patch("agentcompass.cli.subprocess.call", return_value=0) as call:
            self.assertEqual(cli.main(["providers", "--format", "json"]), 0)
        self.assertEqual(call.call_args.args[0][5:], ["providers", "--format", "json"])

    def test_unknown_command_returns_2(self):
        self.assertEqual(cli.main(["nope"]), 2)

    def test_plan_without_a_task_explains_how_to_pass_one(self):
        out = io.StringIO()
        with patch("agentcompass.cli.readiness_notice", return_value=None), \
                patch("agentcompass.cli.subprocess.call", side_effect=AssertionError("route_judge must not run")), \
                patch("sys.stdout", out):
            code = cli.main(["plan", "--dry-run"])
        text = out.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("agentcompass plan --input <task.json>", text)
        self.assertIn("agentcompass roll --brief <task.md>", text)
        self.assertIn("agentcompass plan --demo --dry-run", text)

    def test_plan_without_a_task_includes_the_setup_guidance_when_nothing_is_ready(self):
        notice = (
            "No provider account is ready on this machine (no signed-in CLI was found). "
            "Run `python -X utf8 setup/onboard.py` (or `agentcompass setup`) to detect the CLIs you are signed in to "
            "and start usage collection."
        )
        out = io.StringIO()
        with patch("agentcompass.cli.readiness_notice", return_value=notice), \
                patch("agentcompass.cli.subprocess.call", side_effect=AssertionError("route_judge must not run")), \
                patch("sys.stdout", out):
            code = cli.main(["plan", "--brief", "task.md"])
        text = out.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("agentcompass setup", text)
        self.assertIn("--input", text)

    def test_plan_demo_still_reaches_route_judge(self):
        with patch("agentcompass.cli.subprocess.call", return_value=0) as call, \
                patch("agentcompass.cli.readiness_notice", side_effect=AssertionError("demo has a task")):
            self.assertEqual(cli.main(["plan", "--demo", "--dry-run"]), 0)
        self.assertEqual(call.call_args.args[0][5:], ["plan", "--demo", "--dry-run"])


if __name__ == "__main__":
    unittest.main()
