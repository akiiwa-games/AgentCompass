import json
import os
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import psutil

import cli_updates
import owned_process

# The fake npm records its arguments and mimics `view` and `install -g <pkg>@latest --prefix <dir>` on disk.
FAKE_NPM = textwrap.dedent("""
    import json, os, subprocess, sys, time
    from pathlib import Path
    args = sys.argv[1:]
    root = Path(os.environ["FAKE_NPM_STATE"])
    mode = (root / "mode.txt").read_text().strip() if (root / "mode.txt").exists() else "ok"
    with (root / "calls.jsonl").open("a") as stream:
        stream.write(json.dumps(args) + "\\n")
    (root / "seen-path.txt").write_text(os.environ.get("PATH", ""))
    (root / "sentinel.txt").write_text(os.environ.get("PROVIDER_SENTINEL", ""))
    (root / "npm-home.txt").write_text(str(Path(sys.argv[0]).resolve()))
    latest = (root / "latest.txt").read_text().strip()
    if args[0] == "view":
        if mode == "offline":
            print("npm error network request failed", file=sys.stderr)
            raise SystemExit(1)
        print(latest)
        raise SystemExit(0)
    package = args[2].rsplit("@", 1)[0]
    prefix = Path(args[args.index("--prefix") + 1])
    if mode == "fail":
        print("npm error code EBUSY: resource busy or locked", file=sys.stderr)
        raise SystemExit(1)
    if mode == "hang":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        (root / "child.pid").write_text(str(child.pid))
        (root / "parent.pid").write_text(str(os.getpid()))
        time.sleep(120)
    if mode == "slow":
        time.sleep(4)
    if mode != "noop":
        manifest = prefix.joinpath("node_modules", *package.split("/"), "package.json")
        data = json.loads(manifest.read_text())
        data["version"] = latest
        manifest.write_text(json.dumps(data))
    print("changed 1 package")
""")


def npm_package(prefix: Path, package: str, command: str, version: str, shim_package: str | None = None):
    """A global npm package plus the .cmd shim npm writes next to it."""
    directory = prefix.joinpath("node_modules", *package.split("/"))
    (directory / "bin").mkdir(parents=True, exist_ok=True)
    (directory / "package.json").write_text(json.dumps({"name": package, "version": version}))
    target = "\\".join((shim_package or package).split("/"))
    (prefix / f"{command}.cmd").write_text(f'@ECHO off\r\n"%dp0%\\node_modules\\{target}\\bin\\{command}.js" %*\r\n')
    return directory


def wait_for(path: Path, seconds: float = 15) -> str:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return path.read_text().strip()
        time.sleep(0.05)
    raise AssertionError(f"{path} was not written")


class CliUpdateCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cli update ")    # spaces in every path
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home dir"
        self.prefix = self.root / "Roaming npm"
        self.other_prefix = self.root / "Other npm"
        self.bin = self.root / "bin dir"
        self.state = self.root / "fake npm state"
        for folder in (self.home, self.prefix, self.other_prefix, self.bin, self.state):
            folder.mkdir(parents=True)
        script = self.bin / "fake_npm.py"
        script.write_text(FAKE_NPM)
        (self.bin / "npm.cmd").write_text(f'@"{sys.executable}" "{script}" %*\r\n')
        (self.state / "latest.txt").write_text("0.2.0")
        self.environ = {**os.environ, "PATH": os.pathsep.join([str(self.prefix), str(self.bin), os.environ["SystemRoot"] + "\\System32"]),
                        "FAKE_NPM_STATE": str(self.state), "LOCALAPPDATA": str(self.root / "local")}
        self.environ.pop("PASEO_HOME", None)    # the host's own Paseo config must not leak into the fixture
        patcher = mock.patch.dict(os.environ, {"ROUTER_STATE_DIR": str(self.root / "state")})
        patcher.start()
        self.addCleanup(patcher.stop)
        for table in (cli_updates._busy, cli_updates._latest):
            table.clear()
        self.addCleanup(self.reap)

    def reap(self):
        for name in ("child.pid", "parent.pid"):
            try:
                psutil.Process(int((self.state / name).read_text())).kill()
            except (OSError, ValueError, psutil.Error):
                pass

    def mode(self, text):
        (self.state / "mode.txt").write_text(text)

    def calls(self):
        path = self.state / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def installs(self):
        return [call for call in self.calls() if call[0] == "install"]

    def row(self, cli_id, **options):
        return next(row for row in cli_updates.snapshot(self.home, self.environ, **options) if row["id"] == cli_id)

    def paseo(self, providers):
        folder = self.home / ".paseo"
        folder.mkdir(exist_ok=True)
        (folder / "config.json").write_text(json.dumps({"agents": {"providers": providers}}))


class DiscoveryTests(CliUpdateCase):
    def test_accounts_sharing_a_binary_are_one_row_and_a_visit_runs_nothing(self):
        npm_package(self.prefix, "@openai/codex", "codex", "0.1.0")
        npm_package(self.prefix, "@anthropic-ai/claude-code", "claude", "2.0.0")
        self.paseo({"claude-2": {"extends": "claude", "enabled": True, "env": ["ANTHROPIC_API_KEY"]},
                    "codex-2": {"extends": "codex", "enabled": False}})
        rows = cli_updates.snapshot(self.home, self.environ)
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id["claude"]["providers"], ["claude", "claude-2"])
        self.assertEqual(by_id["codex"]["providers"], ["codex"])    # the disabled codex-2 is not a user
        self.assertEqual([row["id"] for row in rows].count("claude"), 1)
        self.assertEqual((by_id["codex"]["source"], by_id["codex"]["version"], by_id["codex"]["status"]), ("npm", "0.1.0", "unchecked"))
        self.assertEqual(by_id["pi"]["status"], "missing")
        self.assertFalse(by_id["pi"]["can_update"])
        self.assertEqual(self.calls(), [])    # no npm view, no install: nothing touched the network or the disk

    def test_a_registered_custom_acp_launcher_gets_no_guessed_command(self):
        self.paseo({"dsh": {"extends": "acp", "label": "DeepSeek Harness", "enabled": True,
                            "command": ["node.exe", "C:/x/launcher.cjs", "C:/x/bin.js", "--profile", "acp"], "env": ["DEEPSEEK_API_KEY"]},
                    "off": {"extends": "acp", "enabled": False, "command": ["x.exe"]}})
        rows = {row["id"]: row for row in cli_updates.snapshot(self.home, self.environ)}
        self.assertNotIn("acp:dsh", rows)
        self.assertNotIn("acp:off", rows)
        self.assertIn("没有受支持", cli_updates.update("acp:dsh", self.home, self.environ))
        self.assertEqual(self.calls(), [])

    def test_the_shim_must_belong_to_the_expected_package_in_its_own_prefix(self):
        npm_package(self.prefix, "@openai/codex", "codex", "0.1.0", shim_package="some-other-package")
        row = self.row("codex")
        self.assertEqual((row["source"], row["can_update"], row["status"]), ("unknown", False, "manual"))
        self.assertIn("手动更新", row["note"])
        message = cli_updates.update("codex", self.home, self.environ)
        self.assertIn("手动更新", message)
        self.assertEqual(self.calls(), [])

    def test_a_portable_executable_outside_the_vendor_folder_is_routable_but_not_updatable(self):
        portable = self.root / "portable"
        portable.mkdir()
        (portable / "grok.exe").write_bytes(b"MZ")
        environ = {**self.environ, "PATH": str(portable)}
        with mock.patch.object(cli_updates.owned_process, "run", return_value=owned_process.Result(0, "grok 9.9.9 (x)", "")) as run:
            row = next(r for r in cli_updates.snapshot(self.home, environ) if r["id"] == "grok")
            message = cli_updates.update("grok", self.home, environ)
        self.assertEqual((row["source"], row["can_update"]), ("unknown", False))
        self.assertIn("手动更新", message)
        for call in run.call_args_list:
            self.assertEqual(call.args[0][1:], ["--version"])    # only the read-only version probe ever ran


class NpmUpdateTests(CliUpdateCase):
    def setUp(self):
        super().setUp()
        self.package = npm_package(self.prefix, "@openai/codex", "codex", "0.1.0")
        # A second copy elsewhere must never be the one that gets updated.
        self.decoy = npm_package(self.other_prefix, "@openai/codex", "codex", "0.0.9")

    def version(self, directory):
        return json.loads((directory / "package.json").read_text())["version"]

    def test_update_installs_into_the_prefix_of_the_executable_the_router_resolves(self):
        message = cli_updates.update("codex", self.home, self.environ)
        self.assertIn("0.1.0 → 0.2.0", message)
        self.assertIn("未重启任何会话", message)
        install = self.installs()
        self.assertEqual(len(install), 1)
        self.assertEqual(install[0][:4], ["install", "-g", "@openai/codex@latest", "--prefix"])
        self.assertEqual(install[0][4], str(self.prefix))
        self.assertEqual((self.version(self.package), self.version(self.decoy)), ("0.2.0", "0.0.9"))
        self.assertEqual(self.row("codex")["status"], "current")
        self.assertEqual(cli_updates.update("codex", self.home, self.environ).count("已是最新"), 1)
        self.assertEqual(len(self.installs()), 1)    # up to date: no second install
        log = (self.root / "state" / "cli-updates.log").read_text(encoding="utf-8")
        self.assertIn("codex:", log)


    def test_an_offline_check_is_a_clear_failure_and_changes_nothing(self):
        self.mode("offline")
        message = cli_updates.update("codex", self.home, self.environ)
        self.assertIn("失败", message)
        self.assertEqual(self.installs(), [])
        self.assertEqual(self.version(self.package), "0.1.0")
        self.assertEqual(cli_updates._busy, {})

    def test_a_failed_install_reports_the_reason_and_keeps_the_old_version(self):
        self.mode("fail")
        message = cli_updates.update("codex", self.home, self.environ)
        self.assertIn("更新失败（退出码 1）", message)
        self.assertIn("EBUSY", message)
        self.assertEqual(self.version(self.package), "0.1.0")
        self.assertEqual(cli_updates._busy, {})

    def test_an_install_that_leaves_the_old_version_is_not_reported_as_updated(self):
        self.mode("noop")
        message = cli_updates.update("codex", self.home, self.environ)
        self.assertNotIn("已更新", message)
        self.assertIn("仍是 0.1.0", message)

    def test_running_sessions_block_the_update_before_npm_touches_anything(self):
        with mock.patch.object(cli_updates, "running_instances", return_value=2):
            message = cli_updates.update("codex", self.home, self.environ)
        self.assertIn("2 个 Codex CLI 进程正在运行", message)
        self.assertEqual(self.installs(), [])

    def test_only_processes_inside_the_package_count_as_running(self):
        inside = self.package / "node_modules" / "codex-win32-x64" / "codex.exe"
        rows = [mock.Mock(info={"exe": str(inside)}), mock.Mock(info={"exe": str(self.decoy / "bin" / "codex.exe")}),
                mock.Mock(info={"exe": None}), mock.Mock(info={"exe": str(self.package) + "-sibling\\x.exe"})]
        with mock.patch("psutil.process_iter", return_value=rows):
            self.assertEqual(cli_updates.running_instances(self.package), 1)

    def test_a_hung_install_is_stopped_with_its_whole_tree(self):
        self.mode("hang")
        with mock.patch.object(cli_updates, "UPDATE_TIMEOUT", 3):
            message = cli_updates.update("codex", self.home, self.environ)
        self.assertIn("超过 3 秒", message)
        self.assertFalse(psutil.pid_exists(int(wait_for(self.state / "parent.pid"))))
        self.assertFalse(psutil.pid_exists(int(wait_for(self.state / "child.pid"))))
        self.assertEqual(self.version(self.package), "0.1.0")
        self.assertEqual(cli_updates._busy, {})    # the claim is released, so the next click works

    def test_duplicate_clicks_and_a_second_update_are_refused_while_one_runs(self):
        npm_package(self.prefix, "@earendil-works/pi-coding-agent", "pi", "1.0.0")
        self.mode("slow")
        outcome = {}
        worker = threading.Thread(target=lambda: outcome.setdefault("message", cli_updates.update("codex", self.home, self.environ)))
        worker.start()
        deadline = time.monotonic() + 20
        while not self.installs() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(len(self.installs()), 1)
        self.assertTrue(self.row("codex")["busy"] == "update")
        self.assertIn("Codex CLI 正在更新", cli_updates.update("codex", self.home, self.environ))
        self.assertIn("另一个 CLI 正在更新", cli_updates.update("pi", self.home, self.environ))
        worker.join(60)
        self.assertIn("0.1.0 → 0.2.0", outcome["message"])
        self.assertEqual(len(self.installs()), 1)
        self.assertEqual(cli_updates._busy, {})


    def test_npm_is_run_through_node_directly_when_its_cli_is_beside_it(self):
        folder = self.root / "node js"
        (folder / "node_modules" / "npm" / "bin").mkdir(parents=True)
        (folder / "node_modules" / "npm" / "bin" / "npm-cli.js").write_text("")
        (folder / "node.exe").write_bytes(b"MZ")
        (folder / "npm.cmd").write_text("")
        command = cli_updates.npm_command({"PATH": str(folder)})
        self.assertEqual(command, [str(folder / "node.exe"), str(folder / "node_modules" / "npm" / "bin" / "npm-cli.js")])
        self.assertIsNone(cli_updates.npm_command({"PATH": str(self.root / "empty")}))


class NativeUpdateFixture(CliUpdateCase):
    """Vendor self-updaters are driven through the real command contract, with the process layer replaced."""

    def setUp(self):
        super().setUp()
        folder = self.home / ".grok" / "bin"
        folder.mkdir(parents=True)
        self.exe = folder / "grok.exe"
        self.exe.write_bytes(b"MZ")
        self.environ["PATH"] = str(folder)
        self.version = "1.0.60"
        self.latest = "1.0.70"
        self.commands = []
        self.executables = []
        patcher = mock.patch.object(cli_updates.owned_process, "run", side_effect=self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake(self, argv, **options):
        self.commands.append(argv[1:])
        self.executables.append(argv[0])
        if argv[1:] == ["--version"]:
            return owned_process.Result(0, f"grok {self.version} (01a8e1c)", "")
        if argv[1:] == ["update", "--check", "--json"]:
            return owned_process.Result(0, json.dumps({"currentVersion": self.version, "latestVersion": self.latest,
                                                       "updateAvailable": self.version != self.latest, "installer": "internal",
                                                       "channel": "stable", "autoUpdate": True, "error": None}), "")
        if argv[1:] == ["update"]:
            self.version = self.latest
            return owned_process.Result(0, "updated", "")
        raise AssertionError(argv)

class NativeUpdateTests(NativeUpdateFixture):
    def test_the_vendor_updater_runs_without_channel_or_version_flags(self):
        message = cli_updates.update("grok", self.home, self.environ)
        self.assertIn("1.0.60 → 1.0.70", message)
        self.assertIn(["update"], self.commands)
        self.assertNotIn("--alpha", sum(self.commands, []))

    def test_an_up_to_date_native_cli_is_not_reinstalled(self):
        self.latest = self.version
        self.assertIn("已是最新", cli_updates.update("grok", self.home, self.environ))
        self.assertNotIn(["update"], self.commands)



    def test_a_cli_without_a_check_command_just_runs_its_own_updater(self):
        folder = self.home / "AppData" / "Local" / "agy" / "bin"
        folder.mkdir(parents=True)
        (folder / "agy.exe").write_bytes(b"MZ")
        self.environ.update(PATH=str(folder), LOCALAPPDATA=str(self.home / "AppData" / "Local"))
        row = self.row("agy")
        self.assertEqual((row["source"], row["can_update"]), ("native", True))
        self.assertIn("已更新", cli_updates.update("agy", self.home, self.environ))
        self.assertIn(["update"], self.commands)


class SourceConsistencyTests(NativeUpdateFixture):
    """The update targets the executable the router launches (configured Paseo command first, then PATH), never another copy."""

    def make(self, folder, *names):
        folder.mkdir(parents=True, exist_ok=True)
        for name in names:
            (folder / name).write_bytes(b"MZ")
        return folder

    def updates(self):
        return [exe for exe, command in zip(self.executables, self.commands) if command == ["update"]]

    def test_a_configured_agent_command_updates_its_sibling_grok_not_the_one_on_path(self):
        vendor = self.make(self.home / ".grok" / "bin", "agent.exe", "grok.exe")
        elsewhere = self.make(self.root / "elsewhere", "grok.exe")
        self.environ["PATH"] = str(elsewhere)
        self.paseo({"grok": {"extends": "acp", "enabled": True, "command": [str(vendor / "agent.exe"), "agent", "--always-approve", "stdio"]}})
        rows = [r for r in cli_updates.snapshot(self.home, self.environ) if r["id"].startswith("grok")]
        self.assertEqual([(r["id"], r["path"].lower(), r["providers"]) for r in rows],
                         [("grok", str(vendor / "grok.exe").lower(), ["grok"])])
        self.assertIn("1.0.60 → 1.0.70", cli_updates.update("grok", self.home, self.environ))
        self.assertEqual([Path(exe).parent for exe in self.updates()], [vendor])
        self.assertNotIn(str(elsewhere / "grok.exe"), self.executables)

    def test_a_configured_copy_outside_the_vendor_folder_is_managed_by_hand_even_if_path_has_a_vendor_install(self):
        vendor = self.make(self.home / ".grok" / "bin", "grok.exe")
        portable = self.make(self.root / "portable tools", "grok.exe")
        self.environ["PATH"] = str(vendor)
        self.paseo({"grok": {"extends": "acp", "enabled": True, "command": [str(portable / "grok.exe"), "stdio"]}})
        row = next(r for r in cli_updates.snapshot(self.home, self.environ) if r["id"] == "grok")
        self.assertEqual((row["source"], row["status"], row["can_update"]), ("unknown", "manual", False))
        self.assertEqual(Path(row["path"]), portable / "grok.exe")
        message = cli_updates.update("grok", self.home, self.environ)
        self.assertIn("手动更新", message)
        self.assertEqual(self.updates(), [])    # the PATH copy was not "fixed" instead

    def test_providers_with_different_executables_are_separate_rows_with_their_own_updates(self):
        vendor = self.make(self.home / ".grok" / "bin", "grok.exe")
        second = self.make(self.home / ".grok-second" / "bin", "grok.exe")
        self.environ["PATH"] = str(vendor)
        self.paseo({"grok": {"extends": "acp", "enabled": True, "command": [str(vendor / "grok.exe"), "stdio"]},
                    "grok-second": {"extends": "grok", "enabled": True, "command": [str(second / "grok.exe"), "stdio"]}})
        ids = [(r["id"], r["providers"]) for r in cli_updates.snapshot(self.home, self.environ) if r["id"].startswith("grok")]
        self.assertEqual(ids, [("grok", ["grok"]), ("grok#grok-second", ["grok-second"])])
        self.assertIn("手动更新", cli_updates.update("grok#grok-second", self.home, self.environ))
        self.assertEqual(self.updates(), [])
        cli_updates.update("grok", self.home, self.environ)
        self.assertEqual([Path(exe).parent for exe in self.updates()], [vendor])

    def test_a_configured_command_that_is_not_the_tool_or_does_not_exist_is_reported_not_replaced(self):
        vendor = self.make(self.home / ".grok" / "bin", "grok.exe")
        self.environ["PATH"] = str(vendor)
        self.paseo({"grok": {"extends": "acp", "enabled": True, "command": [str(self.root / "node.exe"), "x.js"]}})
        row = next(r for r in cli_updates.snapshot(self.home, self.environ) if r["id"] == "grok")
        self.assertEqual((row["source"], row["can_update"]), ("unknown", False))
        self.assertIn("不是 grok 的可执行文件", row["note"])
        self.paseo({"grok": {"extends": "acp", "enabled": True, "command": [str(self.root / "gone" / "grok.exe")]}})
        row = next(r for r in cli_updates.snapshot(self.home, self.environ) if r["id"] == "grok")
        self.assertIn("不存在", row["note"])
        self.assertEqual(self.updates(), [])

    def test_a_bare_configured_name_resolves_through_the_providers_own_path_then_the_global_one(self):
        vendor = self.make(self.home / ".grok" / "bin", "grok.exe")
        decoy = self.make(self.root / "decoy", "grok.exe")
        self.environ["PATH"] = str(decoy)
        self.paseo({"grok": {"extends": "acp", "enabled": True, "command": ["grok", "stdio"], "env": {"PATH": str(vendor)}}})
        row = next(r for r in cli_updates.snapshot(self.home, self.environ) if r["id"] == "grok")
        self.assertEqual((row["source"], Path(row["path"])), ("native", vendor / "grok.exe"))

    def test_grok_installed_only_in_its_own_folder_is_found_like_the_router_finds_it(self):
        self.make(self.home / ".grok" / "bin", "grok.exe")
        self.environ["PATH"] = str(self.root / "nothing")
        row = next(r for r in cli_updates.snapshot(self.home, self.environ) if r["id"] == "grok")
        self.assertEqual((row["source"], row["can_update"]), ("native", True))

    def test_a_second_claude_account_with_its_own_command_updates_its_own_npm_prefix(self):
        main = npm_package(self.prefix, "@anthropic-ai/claude-code", "claude", "2.0.0")
        second_prefix = self.root / "claude two npm"
        second = npm_package(second_prefix, "@anthropic-ai/claude-code", "claude", "2.0.0")
        self.environ["PATH"] = os.pathsep.join([str(self.prefix), str(self.bin), self.environ["PATH"]])
        self.paseo({"claude-2": {"extends": "claude", "enabled": True, "command": [str(second_prefix / "claude.cmd")]}})
        rows = {r["id"]: r for r in cli_updates.snapshot(self.home, self.environ) if r["id"].startswith("claude")}
        self.assertEqual({k: v["providers"] for k, v in rows.items()}, {"claude": ["claude"], "claude#claude-2": ["claude-2"]})
        with mock.patch.object(cli_updates.owned_process, "run", side_effect=lambda argv, **kw: owned_process.Result(0, "9.9.9", "")):
            with mock.patch.object(cli_updates, "run_tool", side_effect=lambda argv, timeout, install: (
                    json.loads((install.package_dir / "package.json").read_text()) and
                    owned_process.Result(0, "9.9.9" if argv[1] == "view" else "ok", ""))) as tool:
                cli_updates.update("claude#claude-2", self.home, self.environ)
        installs = [call.args[0] for call in tool.call_args_list if "install" in call.args[0]]
        self.assertEqual(len(installs), 1)
        self.assertEqual(installs[0][installs[0].index("--prefix") + 1], str(second_prefix))
        self.assertEqual(json.loads((main / "package.json").read_text())["version"], "2.0.0")


class ProviderEnvTests(CliUpdateCase):
    def test_provider_env_replaces_path_and_keeps_keys_from_the_provider_it_extends(self):
        provider = self.root / "provider prefix"
        decoy = self.root / "decoy prefix"
        npm_package(provider, "@openai/codex", "codex", "0.1.0")
        script = (self.bin / "fake_npm.py").read_text(encoding="utf-8")
        for folder in (provider, decoy):
            folder.mkdir(exist_ok=True)
            target = folder / "fake_npm.py"
            target.write_text(script, encoding="utf-8")
            (folder / "npm.cmd").write_text(f'@"{sys.executable}" "{target}" %*\r\n')
        system = os.environ["SystemRoot"] + "\\System32"
        provider_path = os.pathsep.join([str(provider), system])
        self.environ["PATH"] = str(decoy)
        self.paseo({"codex": {"env": {"PROVIDER_SENTINEL": "from-parent", "PATH": str(decoy)}},
                    "codex-2": {"extends": "codex", "enabled": True, "command": ["codex"],
                                "env": {"PATH": provider_path}}})
        message = cli_updates.update("codex#codex-2", self.home, self.environ)
        self.assertIn("0.1.0 → 0.2.0", message)
        seen = (self.state / "seen-path.txt").read_text()
        self.assertEqual(seen, provider_path)
        self.assertNotIn(str(decoy), seen)
        self.assertEqual((self.state / "sentinel.txt").read_text(), "from-parent")
        self.assertEqual(os.path.normcase((self.state / "npm-home.txt").read_text()),
                         os.path.normcase(str((provider / "fake_npm.py").resolve())))
        self.assertEqual(self.installs()[0][self.installs()[0].index("--prefix") + 1], str(provider))

    def test_a_commandless_provider_resolves_through_its_own_path_and_distinct_paths_stay_separate(self):
        provider = self.root / "provider npm"
        decoy = self.root / "decoy npm"
        other = self.root / "other npm"
        npm_package(provider, "@openai/codex", "codex", "1.0.0")
        npm_package(decoy, "@openai/codex", "codex", "9.0.0")
        npm_package(other, "@openai/codex", "codex", "3.0.0")
        self.environ["PATH"] = str(decoy)
        self.paseo({"codex": {"enabled": True, "env": {"PATH": str(provider)}}})
        row = self.row("codex", probe=False)
        self.assertEqual((row["source"], Path(row["path"])), ("npm", provider / "codex.cmd"))
        self.paseo({"codex": {"enabled": True, "env": {"PATH": str(provider)}},
                    "codex-2": {"extends": "codex", "enabled": True, "env": {"PATH": str(other)}}})
        paths = {Path(item["path"]) for item in cli_updates.snapshot(self.home, self.environ, probe=False)
                 if str(item["id"]).startswith("codex")}
        self.assertEqual(paths, {provider / "codex.cmd", other / "codex.cmd"})
        self.paseo({"codex": {"enabled": True}})
        row = self.row("codex", probe=False)
        self.assertEqual(Path(row["path"]), decoy / "codex.cmd")


if __name__ == "__main__":
    unittest.main()
