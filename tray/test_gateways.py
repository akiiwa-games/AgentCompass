import base64
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gateways
import plugins
import services
import updates

# The lines of grok2api's config.example.yaml that the first run fills in.
TEMPLATE = """server:
  listen: "127.0.0.1:8000"
secrets:
  # comment
  jwtSecret: "replace-with-example"
  credentialEncryptionKey: "replace-with-base64-key"
bootstrapAdmin:
  username: "admin"
  # [必须修改] comment
  password: "replace-with-a-strong-password"
frontend:
  staticPath: "./frontend/dist"
database:
  sqlite:
    path: "./data/backend.db"
"""


class GrokConfigTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        self.root = base / "plugins" / "grok2api"
        self.root.mkdir(parents=True)
        (self.root / "config.example.yaml").write_text(TEMPLATE, encoding="utf-8")
        self.home = base / ".grok2api"
        for name, value in (("GROK_HOME", self.home), ("GROK_CONFIG", self.home / "config.yaml"),
                            ("GROK_EXE", self.home / "bin" / "grok2api.exe")):
            patcher = mock.patch.object(gateways, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(gateways, "grok_root", return_value=self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_first_run_writes_loopback_port_fresh_secrets_and_the_console_path(self):
        self.assertTrue(gateways.grok_write_config())
        text = gateways.GROK_CONFIG.read_text(encoding="utf-8")
        self.assertIn(f'listen: "127.0.0.1:{gateways.GROK_PORT}"', text)
        self.assertNotIn("replace-with", text)
        key = text.split('credentialEncryptionKey: "')[1].split('"')[0]
        self.assertEqual(len(base64.b64decode(key)), 32)
        self.assertIn(f'staticPath: "{(self.root / "frontend" / "dist").as_posix()}"', text)
        self.assertIn('path: "./data/backend.db"', text)
        user, password = gateways.grok_admin()
        self.assertEqual(user, "admin")
        self.assertGreaterEqual(len(password), 20)

    def test_an_existing_config_is_never_rewritten(self):
        gateways.grok_write_config()
        before = gateways.GROK_CONFIG.read_text(encoding="utf-8")
        self.assertFalse(gateways.grok_write_config())
        self.assertEqual(gateways.GROK_CONFIG.read_text(encoding="utf-8"), before)

    def test_a_changed_template_fails_loudly_instead_of_keeping_placeholders(self):
        (self.root / "config.example.yaml").write_text(TEMPLATE.replace("jwtSecret", "jwtKey"), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "jwtSecret"):
            gateways.grok_write_config()
        self.assertFalse(gateways.GROK_CONFIG.exists())

    def test_no_admin_without_a_config_or_after_the_block_is_removed(self):
        self.assertEqual(gateways.grok_admin(), ("", ""))
        gateways.GROK_HOME.mkdir(parents=True)
        gateways.GROK_CONFIG.write_text("server:\n  listen: \"x\"\n", encoding="utf-8")
        self.assertEqual(gateways.grok_admin(), ("", ""))

    def test_build_compiles_the_console_and_server_then_writes_the_config(self):
        steps = []

        def run(args, cwd, env=None, timeout=0):
            steps.append((Path(args[0]).stem.lower(), args[1], Path(cwd).name))
            if args[1] == "build" and "-o" in args:
                Path(args[args.index("-o") + 1]).write_bytes(b"exe")
        with mock.patch.object(gateways, "_tool", side_effect=lambda *names: names[0]):
            gateways.grok_build(run)
        self.assertEqual(steps, [("pnpm", "install", "frontend"), ("pnpm", "build", "frontend"),
                                 ("go", "build", "backend")])
        self.assertTrue(gateways.GROK_EXE.is_file())
        self.assertTrue(gateways.GROK_CONFIG.is_file())


class MuseTests(unittest.TestCase):
    def test_env_is_loopback_browser_driver_with_data_outside_the_clone(self):
        # Chrome is host-dependent; the test only checks the env wiring.
        with mock.patch.dict(gateways.os.environ, {}, clear=False), \
                mock.patch.object(gateways, "chrome_path", return_value=Path("chrome.exe")):
            gateways.os.environ.pop("MUSE2API_DRIVER", None)
            env = gateways.muse_env()
        self.assertEqual(env["MUSE2API_HOST"], "127.0.0.1")
        self.assertEqual(env["MUSE2API_PORT"], str(gateways.MUSE_PORT))
        self.assertEqual(env["MUSE2API_DRIVER"], "browser")
        self.assertEqual(Path(env["MUSE2API_DATA_DIR"]), gateways.MUSE_HOME / "data")

    def test_sync_creates_the_venv_once_and_installs_editable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            with mock.patch.object(gateways, "muse_root", return_value=root):
                gateways.muse_sync(lambda args, cwd, **k: calls.append(args[1]), Path("uv.exe"))
                (root / ".venv" / "Scripts").mkdir(parents=True)
                (root / ".venv" / "Scripts" / "python.exe").write_bytes(b"")
                gateways.muse_sync(lambda args, cwd, **k: calls.append(args[1]), Path("uv.exe"))
        self.assertEqual(calls, ["venv", "pip", "pip"])


class LifecycleTests(unittest.TestCase):
    def test_grok_without_accounts_is_running_but_says_so(self):
        up = {f"{gateways.GROK_URL}/healthz"}
        with mock.patch.object(services, "http_ok", side_effect=lambda url, *a: url in up), \
                mock.patch.object(gateways, "grok_built", return_value=True):
            self.assertEqual(services.grok_label(), "运行中·待添加账号")
            up.add(f"{gateways.GROK_URL}/readyz")
            self.assertEqual(services.grok_label(), "运行中")

    def test_start_refuses_an_unbuilt_gateway_and_stop_kills_only_its_port(self):
        with mock.patch.object(services, "grok_ready", return_value=False), \
                mock.patch.object(gateways, "grok_built", return_value=False), \
                mock.patch.object(services, "hidden_popen") as popen:
            self.assertIn("还没有构建", services.start_grok2api())
        popen.assert_not_called()
        with mock.patch.object(services, "listener_pid", return_value=4321) as port, \
                mock.patch.object(services, "kill_pid") as kill:
            services.stop_muse2api()
        port.assert_called_once_with(gateways.MUSE_PORT)
        kill.assert_called_once_with(4321)

    def test_enabling_clones_builds_and_starts_when_set_to_autostart(self):
        events = []
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(plugins, "PLUGINS_DIR", Path(directory)), \
                mock.patch.object(updates, "run", side_effect=lambda args, cwd, **k:
                                  (Path(args[-1]) / ".git").mkdir(parents=True) if args[1] == "clone" else ""), \
                mock.patch.object(updates, "version", return_value="v1"), \
                mock.patch.object(updates, "prepare_gateway", side_effect=lambda pid: events.append(("build", pid))), \
                mock.patch.object(gateways, "built", return_value=True), \
                mock.patch.object(services, "start_muse2api", side_effect=lambda: events.append("start") or "started"):
            message = updates.deploy_gateway("muse2api", start=True, progress=lambda e: events.append(e["kind"]))
        self.assertIn("已下载并部署", message)
        self.assertIn(("build", "muse2api"), events)
        self.assertEqual(events[-3:], ["start", "result", "finish"])

    def test_update_rolls_back_and_restarts_after_a_failed_build(self):
        git_calls = []

        def git(root, *arguments, **k):
            git_calls.append(arguments[0])
            return "abc" if arguments[0] == "rev-parse" else ""
        builds = iter([RuntimeError("go build failed"), None])

        def prepare(pid):
            error = next(builds)
            if error:
                raise error
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            with mock.patch.object(plugins, "plugin_root", return_value=root), \
                    mock.patch.object(updates, "behind", return_value=2), \
                    mock.patch.object(updates, "changed", return_value=[]), \
                    mock.patch.object(updates, "version", return_value="v1"), \
                    mock.patch.object(updates, "git", side_effect=git), \
                    mock.patch.object(updates, "prepare_gateway", side_effect=prepare), \
                    mock.patch.object(services, "grok_ready", return_value=True), \
                    mock.patch.object(services, "plugin_stop") as stop, \
                    mock.patch.object(services, "plugin_start") as start:
                message = updates.update_gateway("grok2api")
        self.assertIn("更新失败", message)
        self.assertIn("已回到原版本", message)
        self.assertEqual(git_calls, ["rev-parse", "pull", "reset"])
        stop.assert_called_once_with("grok2api")
        start.assert_called_once_with("grok2api")
        self.assertNotIn("grok2api", services.UPDATING_COMPONENTS)


if __name__ == "__main__":
    unittest.main()
