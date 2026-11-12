"""Public parsing/recovery; artifact journeys are covered by packaging QA."""
import os
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from agentcompass import __version__

ENTRY = Path(__file__).resolve().parent


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="public cli empty home ")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env = {k: v for k, v in os.environ.items()
                    if k.upper() in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP")}
        self.env.update(HOME=str(self.home), USERPROFILE=str(self.home), PATH="")

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-X", "utf8", str(ENTRY), *args],
                              cwd=self.home, env=self.env, capture_output=True,
                              text=True, encoding="utf-8", timeout=30)

    def test_version_works_outside_the_checkout_without_accounts(self):
        done = self.run_cli("--version")
        self.assertEqual((done.returncode, done.stdout.strip()), (0, __version__), done.stderr)

    def test_missing_task_gives_public_recovery_without_creating_state(self):
        done = self.run_cli("plan", "--dry-run")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("agentcompass plan --input", done.stdout)
        self.assertIn("agentcompass roll --task", done.stdout)
        self.assertIn("agentcompass setup", done.stdout)
        self.assertNotIn("route_judge.py", done.stdout)
        self.assertFalse((self.home / ".agent-compass/router-ledger.jsonl").exists())

    def test_unknown_command_and_invalid_stage_flags_are_rejected(self):
        for args in (("nope",), ("roll", "--x")):
            with self.subTest(args=args):
                done = self.run_cli(*args)
                self.assertNotEqual(done.returncode, 0, done.stdout)
                self.assertIn("agentcompass", done.stdout + done.stderr)

    def test_usage_help_does_not_collect_or_expose_internal_entry(self):
        done = self.run_cli("usage", "-h")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("agentcompass usage", done.stdout)
        self.assertIn("--show", done.stdout)
        self.assertNotIn(".py", done.stdout)

    def test_no_accounts_preserves_the_complete_setup_recovery_command(self):
        # The host identity is external fixture context. Disable the scorer so
        # this recovery test cannot contact an authenticated model service.
        self.env['CODEX_THREAD_ID'] = 'fixture-session'
        done = self.run_cli('roll', '--task', 'Inspect the fixture project', '--no-score')
        self.assertEqual(done.returncode, 2, done.stderr + done.stdout)
        result = json.loads(done.stdout)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn(' setup`', result['next_action'])
        command = result['next_action'].split('`', 2)[1]
        import shlex
        # Execute the documented recovery's help in the same isolated home.
        recovered = subprocess.run([*shlex.split(command), '-h'], cwd=self.home,
                                   env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(recovered.returncode, 0, recovered.stderr)

    def test_data_refresh_subcommand_runs_single_entry_and_guards_non_checkout(self):
        from unittest.mock import patch
        from agentcompass import cli

        help_done = self.run_cli("data", "refresh", "-h")
        self.assertEqual(help_done.returncode, 0, help_done.stderr)
        self.assertIn("--no-fetch", help_done.stdout)

        scripts_dir = ENTRY.parent / "skills" / "agent-compass" / "scripts"
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        import test_refresh_entry

        data_dir, refs_dir = test_refresh_entry.build_calibration_fixture(self.home / "cal-fixture")
        done = self.run_cli("data", "refresh", "--no-fetch", "--data", str(data_dir), "--references", str(refs_dir))
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        for name in (
            "public-measurements.json",
            "five-axis-scores.json",
            "model-capabilities.json",
            "model-capabilities.md",
            "public-data.json",
        ):
            self.assertTrue((refs_dir / name).is_file(), name)
        before_manifest = (refs_dir / "public-data.json").read_bytes()
        done_again = self.run_cli("data", "refresh", "--no-fetch", "--data", str(data_dir), "--references", str(refs_dir))
        self.assertEqual(done_again.returncode, 0, done_again.stderr + done_again.stdout)
        self.assertEqual((refs_dir / "public-data.json").read_bytes(), before_manifest)

        # Outside a git checkout, `agentcompass data refresh` without `--references` refuses cleanly and suggests `data --refresh`.
        import io
        err = io.StringIO()
        with patch.object(cli.locations, "checkout_root", return_value=None), patch("sys.stderr", err):
            rc = cli.data_command(["refresh", "--no-fetch"])
        self.assertEqual(rc, 2)
        self.assertIn("agentcompass data --refresh", err.getvalue())


if __name__ == "__main__":
    unittest.main()
