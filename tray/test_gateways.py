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
    def test_start_refuses_an_unbuilt_gateway_and_stop_kills_only_its_port(self):
        with mock.patch.object(services, "muse_ready", return_value=False), \
                mock.patch.object(gateways, "muse_built", return_value=False), \
                mock.patch.object(services, "hidden_popen") as popen:
            self.assertIn("还没有安装依赖", services.start_muse2api())
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
        builds = iter([RuntimeError("dependency install failed"), None])

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
                    mock.patch.object(services, "muse_ready", return_value=True), \
                    mock.patch.object(services, "plugin_stop") as stop, \
                    mock.patch.object(services, "plugin_start") as start:
                message = updates.update_gateway("muse2api")
        self.assertIn("更新失败", message)
        self.assertIn("已回到原版本", message)
        self.assertEqual(git_calls, ["rev-parse", "pull", "reset"])
        stop.assert_called_once_with("muse2api")
        start.assert_called_once_with("muse2api")
        self.assertNotIn("muse2api", services.UPDATING_COMPONENTS)


if __name__ == "__main__":
    unittest.main()
