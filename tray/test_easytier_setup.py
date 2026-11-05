import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from unittest import mock
from xml.etree import ElementTree

import easytier_setup as setup


class TaskDefinitionTests(unittest.TestCase):
    NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}

    def parse(self, **kwargs):
        defaults = dict(command=r"C:\Windows\powershell.exe", arguments='-File "E:\\a & b\\x.ps1"', trigger="boot", restart=True)
        return ElementTree.fromstring(setup.task_xml(**{**defaults, **kwargs}).split("?>", 1)[1])

    def test_runs_as_system_at_boot_with_restart_and_no_time_limit(self):
        root = self.parse()
        self.assertEqual(root.find(".//t:UserId", self.NS).text, "S-1-5-18")
        self.assertIsNotNone(root.find(".//t:BootTrigger", self.NS))
        self.assertEqual(root.find(".//t:RestartOnFailure/t:Count", self.NS).text, "999")
        self.assertEqual(root.find(".//t:ExecutionTimeLimit", self.NS).text, "PT0S")
        self.assertEqual(root.find(".//t:MultipleInstancesPolicy", self.NS).text, "IgnoreNew")
        self.assertEqual(root.find(".//t:Hidden", self.NS).text, "true")

    def test_arguments_with_special_characters_survive_xml(self):
        self.assertIn("a & b", self.parse().find(".//t:Arguments", self.NS).text)

    def test_helper_tasks_have_no_boot_trigger(self):
        self.assertIsNone(self.parse(trigger="none", restart=False).find(".//t:BootTrigger", self.NS))

    def test_the_user_gets_read_and_execute_on_the_task_but_not_write_or_delete(self):
        with mock.patch.object(setup, "powershell") as shell:
            setup.register_task("T", "<Task/>", "S-1-5-21-1-2-3-1001")
        script = shell.call_args[0][0]
        self.assertIn("D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;GRGX;;;S-1-5-21-1-2-3-1001)", script)
        self.assertEqual(script.count("S-1-5-21-1-2-3-1001"), 1)

    def test_helper_tasks_are_admin_only(self):
        with mock.patch.object(setup, "powershell") as shell:
            setup.register_task("T", "<Task/>", None)
        self.assertNotIn("GRGX", shell.call_args[0][0])


class StageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.source = base / "src"
        self.source.mkdir()
        for name in (*setup.REQUIRED, "WinDivert64.sys"):
            (self.source / name).write_bytes(name.encode())
        self.config = base / "my.toml"
        self.config.write_text("network_secret = 'x'\n")
        home = base / "easytier"
        patches = [mock.patch.object(setup, "HOME", home)] + [
            mock.patch.object(setup, attr, home / name)
            for attr, name in (("BIN", "bin"), ("PRIVATE", "private"), ("LOGS", "logs"), ("STATE", "state"), ("BACKUP", "backup"))]
        patches.append(mock.patch.object(setup, "PINNED", {"easytier-core.exe": setup.hashlib.sha256(b"easytier-core.exe").hexdigest()}))
        self.lock = mock.Mock()
        patches.append(mock.patch.object(setup, "lock_down", self.lock))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.home = home

    def test_stage_copies_binaries_supervisor_and_this_hosts_config_only(self):
        setup.stage_files(self.source, self.config, None)
        self.assertEqual(sorted(path.name for path in (self.home / "bin").iterdir()),
                         sorted([*setup.REQUIRED, "WinDivert64.sys", "easytier-supervisor.ps1", "manifest.json"]))
        self.assertEqual((self.home / "private" / "node.toml").read_text(), "network_secret = 'x'\n")
        manifest = json.loads((self.home / "bin" / "manifest.json").read_text())
        self.assertEqual(manifest["easytier-core.exe"], setup.hashlib.sha256(b"easytier-core.exe").hexdigest())
        others_read = {call.args[0].name: call.args[1] for call in self.lock.call_args_list}
        self.assertFalse(others_read["private"])
        self.assertFalse(others_read["backup"])
        self.assertTrue(others_read["bin"])

    def test_the_manifest_never_holds_config_text(self):
        setup.stage_files(self.source, self.config, None)
        self.assertNotIn("network_secret", (self.home / "bin" / "manifest.json").read_text())

    def test_a_core_that_is_not_the_pinned_release_is_refused_before_anything_is_written(self):
        (self.source / "easytier-core.exe").write_bytes(b"tampered")
        with self.assertRaisesRegex(RuntimeError, "SHA-256"):
            setup.stage_files(self.source, self.config, None)
        self.assertFalse(self.home.exists())

    def test_missing_required_files_are_reported(self):
        (self.source / "wintun.dll").unlink()
        with self.assertRaises(FileNotFoundError):
            setup.stage_files(self.source, self.config, None)

    def test_windivert_is_optional(self):
        (self.source / "WinDivert64.sys").unlink()
        setup.stage_files(self.source, self.config, None)
        self.assertFalse((self.home / "bin" / "WinDivert64.sys").exists())

    def test_the_host_hook_lands_in_the_private_directory(self):
        hook = Path(self.temp.name) / "hook.ps1"
        hook.write_text("# host policy")
        setup.stage_files(self.source, self.config, hook)
        self.assertEqual((self.home / "private" / "pre-start.ps1").read_text(), "# host policy")


    def test_the_supervisor_guards_single_instance_runs_the_hook_and_returns_the_core_exit_code(self):
        if not setup.POWERSHELL.is_file():
            self.skipTest("Windows PowerShell unavailable")
        setup.stage_files(self.source, self.config, None)
        hook = self.home / 'private/pre-start.ps1'
        marker = self.home / 'hook-ran'
        hook.write_text("Set-Content -LiteralPath '" + str(marker).replace("'", "''") + "' -Value yes")
        # Execute the shipped script, replacing only OS process APIs. No vendor
        # executable, task registration, network or real process inspection runs.
        wrapper = self.home / 'fixture.ps1'
        wrapper.write_text(r'''
function Get-CimInstance { param($Filter, $ErrorAction) if ($env:FIXTURE_MODE -eq 'busy') { [pscustomobject]@{ProcessId=12345} } }
function Start-Process {
    param($FilePath, $ArgumentList, $WorkingDirectory, [switch]$NoNewWindow,
          [switch]$Wait, [switch]$PassThru, $RedirectStandardOutput, $RedirectStandardError)
    ConvertTo-Json -Compress -InputObject @($ArgumentList) | Add-Content -LiteralPath $env:FIXTURE_CALLS
    $code = if ($ArgumentList -contains '--check-config') { if ($env:FIXTURE_MODE -eq 'bad-config') { 9 } else { 0 } } else { 17 }
    [pscustomobject]@{Id=12345;ExitCode=$code} | Add-Member -MemberType ScriptMethod -Name Refresh -Value {} -PassThru
}
function Wait-Process { param($Id) }
& (Join-Path $PSScriptRoot 'bin/easytier-supervisor.ps1')
exit $LASTEXITCODE
''', encoding='utf-8')
        for mode, expected, count in (('busy', 5, 0), ('bad-config', 6, 1), ('run', 17, 2)):
            with self.subTest(mode=mode):
                marker.unlink(missing_ok=True)
                calls = self.home / ('calls-' + mode)
                env = {k: v for k, v in os.environ.items() if k.upper() in ('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP')}
                env.update(FIXTURE_MODE=mode, FIXTURE_CALLS=str(calls))
                done = subprocess.run([str(setup.POWERSHELL), '-NoProfile', '-ExecutionPolicy', 'Bypass',
                                       '-File', str(wrapper)], env=env, capture_output=True, text=True, timeout=20)
                self.assertEqual(done.returncode, expected, done.stderr + done.stdout)
                rows = [json.loads(line) for line in calls.read_text(encoding='utf-8-sig').splitlines()] if calls.exists() else []
                self.assertEqual(len(rows), count)
                self.assertEqual(marker.exists(), mode != 'busy')
                if mode == 'run':
                    self.assertIn('127.0.0.1:15888', rows[-1])
                    self.assertIn('127.0.0.1/32,::1/128', rows[-1])
                    self.assertIn(str(self.home / 'private/node.toml'), rows[-1])


class SwitchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        state = Path(self.temp.name)
        (state / "legacy.json").write_text(json.dumps({"task": "Old", "command": ["x"], "kill_cmdline_contains": ["old"]}))
        self.calls = []
        patches = [
            mock.patch.object(setup, "STATE", state),
            mock.patch.object(setup, "LEGACY_STATE", state / "legacy.json"),
            mock.patch.object(setup, "SWITCH_STATE", state / "switch.json"),
            mock.patch.object(setup, "check_config"),
            mock.patch.object(setup, "task_exists", return_value=True),
            mock.patch.object(setup, "stop_legacy", side_effect=lambda spec: ["legacy"]),
            mock.patch.object(setup, "cores", return_value=[]),
            mock.patch.object(setup, "wait_until", side_effect=lambda predicate, *a, **k: predicate()),
            mock.patch.object(setup, "run", side_effect=lambda args, **k: self.calls.append(" ".join(args[:5]))),
            mock.patch.object(setup, "start_legacy", side_effect=lambda spec: self.calls.append("start_legacy") or "legacy"),
            mock.patch.object(setup, "log"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.state = state

    def status(self):
        return json.loads((self.state / "switch.json").read_text(encoding="utf-8"))["status"]

    def test_a_healthy_new_node_disables_the_legacy_task_but_keeps_it_for_rollback(self):
        with mock.patch.object(setup, "healthy", return_value=True):
            self.assertEqual(setup.switch(["PEER"]), 0)
        self.assertEqual(self.status(), "ok")
        self.assertNotIn("start_legacy", self.calls)
        self.assertTrue(any("/Change /TN Old /DISABLE" in call for call in self.calls))
        self.assertFalse(any("/Delete" in call for call in self.calls))

    def test_an_unhealthy_new_node_rolls_back_to_the_legacy_runtime(self):
        with mock.patch.object(setup, "healthy", return_value=False), \
                mock.patch.object(setup, "peer_names", return_value=["PEER"]), \
                mock.patch.object(setup, "cores", side_effect=[[], [], [{"ProcessId": 1}], [{"ProcessId": 1}]]):
            self.assertEqual(setup.switch(["PEER"]), 1)
        self.assertIn("start_legacy", self.calls)
        self.assertEqual(self.status(), "rolled_back")

    def test_legacy_that_does_not_exit_aborts_before_the_new_task_starts(self):
        with mock.patch.object(setup, "cores", return_value=[{"ProcessId": 3, "ExecutablePath": "old", "Name": "easytier-core.exe"}]), \
                mock.patch.object(setup, "peer_names", return_value=None), mock.patch.object(setup, "wait_until", return_value=False):
            self.assertEqual(setup.switch(["PEER"]), 1)
        self.assertFalse(any("/Run /TN Route" in call for call in self.calls))

    def test_recover_rolls_back_a_switch_that_never_reported(self):
        (self.state / "switch.json").write_text(json.dumps({"status": "switching"}))
        with mock.patch.object(setup, "rollback", return_value=True) as back:
            self.assertEqual(setup.recover(["PEER"]), 0)
        back.assert_called_once()

    def test_recover_leaves_a_healthy_completed_switch_alone_and_removes_itself(self):
        (self.state / "switch.json").write_text(json.dumps({"status": "ok"}))
        with mock.patch.object(setup, "healthy", return_value=True), mock.patch.object(setup, "rollback") as back, \
                mock.patch.object(setup, "delete_task") as delete:
            self.assertEqual(setup.recover(["PEER"]), 0)
        back.assert_not_called()
        delete.assert_called_once_with(setup.RECOVER_TASK, end=False)

    def test_recover_rolls_back_a_completed_switch_whose_node_has_since_died(self):
        (self.state / "switch.json").write_text(json.dumps({"status": "ok"}))
        with mock.patch.object(setup, "healthy", return_value=False), mock.patch.object(setup, "rollback", return_value=True) as back:
            setup.recover(["PEER"])
        back.assert_called_once()


class MiscTests(unittest.TestCase):
    def test_without_option_drops_the_flag_and_its_value(self):
        self.assertEqual(setup.without_option(["install", "--log", "x", "--switch"], "--log"), ["install", "--switch"])



if __name__ == "__main__":
    unittest.main()


class MigrationTests(unittest.TestCase):
    """install --migrate-from moves a checkout-era node to the per-machine home."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.old = base / "checkout" / "easytier"
        (self.old / "bin").mkdir(parents=True)
        for name in (*setup.REQUIRED, "easytier-supervisor.ps1"):
            (self.old / "bin" / name).write_bytes(name.encode())
        (self.old / "private").mkdir()
        (self.old / "private" / "node.toml").write_text("network_secret = 'x'\n")
        (self.old / "private" / "pre-start.ps1").write_text("# host policy")
        (self.old / "state").mkdir()
        (self.old / "state" / "legacy.json").write_text('{"task": "Old"}')
        self.new = base / "ProgramData" / "AgentCompass" / "easytier"
        self.registered = []
        self.calls = []
        self.healthy = True
        saved = setup.HOME
        self.addCleanup(setup.use_home, saved)
        setup.use_home(self.new)
        patches = [
            mock.patch.dict(os.environ, {setup.easytier_paths.OVERRIDE: str(self.new)}),
            mock.patch.object(setup, "PINNED", {"easytier-core.exe": setup.hashlib.sha256(b"easytier-core.exe").hexdigest()}),
            mock.patch.object(setup, "lock_down"),
            mock.patch.object(setup, "check_config"),
            mock.patch.object(setup, "task_exists", return_value=True),
            mock.patch.object(setup, "cores", return_value=[]),
            mock.patch.object(setup, "wait_until", side_effect=lambda predicate, *a, **k: predicate()),
            mock.patch.object(setup, "healthy", side_effect=lambda expect: self.healthy),
            mock.patch.object(setup, "run", side_effect=lambda args, **k: self.calls.append(" ".join(args))),
            mock.patch.object(setup, "register_main_task",
                              side_effect=lambda sid, bin_dir=None: self.registered.append(bin_dir or setup.BIN)),
            mock.patch.object(setup, "delete_task", side_effect=lambda name, end=True: self.calls.append(f"delete {name}")),
            mock.patch.object(setup, "log"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def migrate(self, *extra):
        args = setup.argparse.Namespace(migrate_from=str(self.old), dry_run=False, keep_old=False, hook=None,
                                        user_sid="S-1-5-21-1", expect_peer=["PEER"], switch=False)
        for name in extra:
            setattr(args, name, True)
        return setup.command_install(args)

    def test_a_healthy_move_carries_bin_config_hook_and_state_and_retargets_the_task(self):
        self.assertEqual(self.migrate(), 0)
        self.assertTrue((self.new / "bin" / "easytier-core.exe").is_file())
        self.assertEqual((self.new / "private" / "node.toml").read_text(), "network_secret = 'x'\n")
        self.assertEqual((self.new / "private" / "pre-start.ps1").read_text(), "# host policy")
        self.assertEqual((self.new / "state" / "legacy.json").read_text(), '{"task": "Old"}')
        self.assertEqual(self.registered, [self.new / "bin"])
        for name in setup.LEGACY_TASKS:
            self.assertIn(f"delete {name}", self.calls)
        self.assertTrue(any(call.startswith("schtasks /End /TN Route Manager EasyTier") for call in self.calls))
        self.assertFalse(self.old.exists())

    def test_keep_old_leaves_the_checkout_copy(self):
        self.assertEqual(self.migrate("keep_old"), 0)
        self.assertTrue((self.old / "private" / "node.toml").is_file())

    def test_an_unhealthy_node_points_the_task_back_and_removes_the_new_copy(self):
        self.healthy = False
        self.assertEqual(self.migrate(), 1)
        self.assertEqual(self.registered, [self.new / "bin", self.old.resolve() / "bin"])
        self.assertFalse(self.new.exists())
        self.assertTrue((self.old / "private" / "node.toml").is_file())

    def test_a_rerun_after_success_is_a_no_op(self):
        self.assertEqual(self.migrate("keep_old"), 0)
        self.registered.clear()
        with mock.patch.object(setup, "task_supervisor", return_value=str(self.new / "bin" / "easytier-supervisor.ps1").lower()):
            self.assertEqual(self.migrate(), 0)
        self.assertEqual(self.registered, [])
        self.assertFalse(self.old.exists())

    def test_a_second_node_the_task_does_not_run_is_refused(self):
        self.assertEqual(self.migrate("keep_old"), 0)
        with mock.patch.object(setup, "task_supervisor", return_value=r"-file e:\checkout\easytier\bin\easytier-supervisor.ps1"):
            with self.assertRaisesRegex(RuntimeError, "没有指向它"):
                self.migrate()
        self.assertTrue(self.old.exists())

    def test_dry_run_changes_nothing_and_needs_no_elevation(self):
        output = io.StringIO()
        with mock.patch.object(setup, "is_admin", return_value=False), \
                mock.patch.object(setup, "relaunch_elevated") as elevate, redirect_stdout(output):
            code = setup.main(["install", "--migrate-from", str(self.old), "--dry-run"])
        self.assertEqual(code, 0)
        elevate.assert_not_called()
        self.assertFalse(self.new.exists())
        self.assertEqual(self.registered, [])
        self.assertIn(str(self.new), output.getvalue())
        self.assertIn("private/node.toml: 有", output.getvalue())

    def test_install_always_targets_the_product_home(self):
        setup.use_home(self.old)
        with mock.patch.object(setup, "is_admin", return_value=True), \
                mock.patch.object(setup, "command_install", return_value=0) as install:
            setup.main(["install", "--migrate-from", str(self.old)])
        install.assert_called_once()
        self.assertEqual(setup.HOME, self.new)

    def test_a_planted_junction_at_the_home_is_refused(self):
        with mock.patch.object(setup, "is_reparse_point", side_effect=lambda path: path == self.new):
            with self.assertRaisesRegex(RuntimeError, "联接"):
                setup.prepare_home()

    def test_the_programdata_parent_is_locked_before_the_home(self):
        program_data = Path(self.temp.name) / "PD"
        setup.use_home(program_data / "AgentCompass" / "easytier")
        with mock.patch.dict(os.environ, {"ProgramData": str(program_data)}):
            os.environ.pop(setup.easytier_paths.OVERRIDE, None)
            setup.prepare_home()
        locked = [call.args[0] for call in setup.lock_down.call_args_list]
        self.assertEqual(locked[:2], [program_data / "AgentCompass", program_data / "AgentCompass" / "easytier"])
