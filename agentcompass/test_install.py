"""The Windows installer's command line: shape detection, the gate hook command, PATH shadowing and the Python stand-in."""
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from agentcompass import cli, frozen_python, install

ENTRY = Path(__file__).resolve().parent


class FrozenInterpreter:
    """sys.frozen / sys.executable as the installer's agentcompass.exe sets them."""

    def __init__(self, exe: Path):
        self.exe = exe
        self.patches = [mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(sys, "executable", str(exe))]

    def __enter__(self):
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in reversed(self.patches):
            patch.stop()


class InstallShapeTests(unittest.TestCase):
    def test_frozen_cli_is_the_installer_shape_and_hooks_call_the_exe(self):
        with TemporaryDirectory() as tmp:
            exe = Path(tmp) / "App Dir" / "cli" / "agentcompass.exe"
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b"MZ")
            with FrozenInterpreter(exe):
                self.assertEqual(install.shape(), "installer")
                argv = install.gate_hook_argv(Path(tmp) / "router_gate_hook.py")
                self.assertEqual(argv, [exe.resolve().as_posix(), "gate-hook"])
                self.assertEqual(install.bundled_cli({}), exe.resolve())
                self.assertTrue(install.is_gate_hook_command(install.gate_hook_command("x")))

    def test_script_shapes_keep_the_python_hook_command(self):
        argv = install.gate_hook_argv("/somewhere/router_gate_hook.py")
        self.assertEqual(argv[1:3], ["-X", "utf8"])
        self.assertTrue(argv[-1].endswith("router_gate_hook.py"))
        self.assertEqual(install.shape("/x/Lib/site-packages/agentcompass/install.py"), "wheel")
        self.assertEqual(install.shape("/x/route_manager/agentcompass/install.py"), "checkout")

    def test_gate_hook_entries_of_every_shape_are_recognised(self):
        for command in ("C:/Py/python.exe -X utf8 C:/repo/setup/router_gate_hook.py",
                        "C:/Users/a/AppData/Local/AgentCompass/cli/agentcompass.exe gate-hook",
                        "'C:/Users/a b/AppData/Local/AgentCompass/cli/agentcompass.exe' gate-hook",
                        '"C:\\Users\\a b\\AgentCompass\\cli\\agentcompass.exe" gate-hook'):
            self.assertTrue(install.is_gate_hook_command(command), command)
        for command in ("agentcompass.exe roll --task gate-hook", "node other-hook.js", ""):
            self.assertFalse(install.is_gate_hook_command(command), command)

    def test_an_older_agentcompass_first_on_path_is_reported_with_its_removal_command(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            bundled = base / "Local" / "AgentCompass" / "cli" / "agentcompass.exe"
            uv_bin = base / "home" / ".local" / "bin" / "agentcompass.exe"
            for exe in (bundled, uv_bin):
                exe.parent.mkdir(parents=True)
                exe.write_bytes(b"MZ")
            env = {"LOCALAPPDATA": str(base / "Local"), "PATHEXT": ".EXE",
                   "PATH": os.pathsep.join([str(uv_bin.parent), str(bundled.parent)])}
            with mock.patch.object(install, "_registry_install_dir", return_value=None):
                shadow = install.shadowing(env)
                self.assertEqual(Path(shadow["first"]), uv_bin)
                self.assertEqual(Path(shadow["bundled"]), bundled)
                self.assertEqual(shadow["remove"], "uv tool uninstall agentcompass")
                env["PATH"] = os.pathsep.join([str(bundled.parent), str(uv_bin.parent)])
                self.assertIsNone(install.shadowing(env))


class FrozenPythonTests(unittest.TestCase):
    """agentcompass.exe accepts the command lines the skill scripts give sys.executable."""

    def test_only_interpreter_command_lines_are_taken(self):
        for argv in (["-I", "-X", "utf8", "x.py"], ["-X", "utf8", "x.py"], ["-S", "-c", "pass"], ["-m", "json.tool"], ["route_judge.py"]):
            self.assertTrue(frozen_python.looks_like_python(argv), argv)
        for argv in (["roll", "--task", "x"], ["--version"], ["-h"], ["setup", "--yes"], []):
            self.assertFalse(frozen_python.looks_like_python(argv), argv)

    def test_runs_a_script_with_its_arguments_and_exit_code(self):
        with TemporaryDirectory() as tmp:
            script = Path(tmp) / "child.py"
            (Path(tmp) / "sibling.py").write_text("VALUE = 'sibling'\n", encoding="utf-8")
            script.write_text("import sys, sibling\nprint(sibling.VALUE, sys.argv[1:])\nraise SystemExit(3)\n", encoding="utf-8")
            out = io.StringIO()
            saved = (list(sys.argv), list(sys.path))
            try:
                with redirect_stdout(out):
                    code = frozen_python.run(["-I", "-X", "utf8", str(script), "a", "b c"])
            finally:
                sys.argv[:], sys.path[:] = saved
                sys.modules.pop("sibling", None)
            self.assertEqual(code, 3)
            self.assertEqual(out.getvalue().strip(), "sibling ['a', 'b c']")

    def test_dash_c_and_the_package_directory(self):
        out = io.StringIO()
        saved = list(sys.argv)
        try:
            with redirect_stdout(out):
                self.assertEqual(frozen_python.run(["-S", "-c", "import sys; print(sys.argv[1:])", "x"]), 0)
                self.assertEqual(frozen_python.run(["-X", "utf8", str(ENTRY), "--version"]), 0)
        finally:
            sys.argv[:] = saved
        from agentcompass import __version__
        self.assertEqual(out.getvalue().split(), ["['x']", __version__])

    def test_frozen_main_hands_interpreter_command_lines_to_the_stand_in(self):
        with TemporaryDirectory() as tmp:
            exe = Path(tmp) / "agentcompass.exe"
            with FrozenInterpreter(exe), mock.patch.object(frozen_python, "run", return_value=7) as run:
                self.assertEqual(cli.main(["-X", "utf8", "route_judge.py", "plan"]), 7)
                run.assert_called_once_with(["-X", "utf8", "route_judge.py", "plan"])
                run.reset_mock()
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(cli.main(["--version"]), 0)
                run.assert_not_called()


class GateHookCommandTests(unittest.TestCase):
    def test_gate_hook_subcommand_runs_the_hook_on_stdin(self):
        with TemporaryDirectory() as tmp:
            env = {k: v for k, v in os.environ.items() if k.upper() in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP")}
            env.update(HOME=tmp, USERPROFILE=tmp, PATH="", ROUTER_STATE_DIR=str(Path(tmp) / ".agent-compass"))
            payload = {"tool_name": "Agent", "tool_input": {"subagent_type": "Explore", "prompt": "look"}, "hook_event_name": "PreToolUse"}
            done = subprocess.run([sys.executable, "-X", "utf8", str(ENTRY), "gate-hook"], input=json.dumps(payload),
                                  env=env, cwd=tmp, capture_output=True, text=True, encoding="utf-8", timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            verdict = json.loads(done.stdout)
            self.assertIn("deny", json.dumps(verdict), verdict)   # no roll in the last 15 minutes: the launch is blocked


if __name__ == "__main__":
    unittest.main()
