import tempfile
import unittest
from pathlib import Path
from unittest import mock

import easytier
import easytier_paths

PEERS = [
    {"cost": "Local", "hostname": "ME", "ipv4": "192.0.2.1"},
    {"cost": "p2p", "hostname": "relay", "ipv4": "", "lat_ms": "8.1"},
    {"cost": "p2p", "hostname": "OTHER", "ipv4": "192.0.2.2", "lat_ms": "7.7"},
]


def reading(state, pids=(), peers=None):
    return {"state": state, "pids": list(pids), "peers": peers}


class PathTests(unittest.TestCase):
    def test_everything_lives_under_one_home(self):
        for path in (easytier.CORE, easytier.CLI, easytier.CONFIG, easytier.LOGS, easytier.STATE, easytier.SUPERVISOR):
            self.assertTrue(path.is_relative_to(easytier.HOME), path)
        self.assertEqual(easytier.CORE, easytier.HOME / "bin" / "easytier-core.exe")
        self.assertEqual(easytier.CONFIG, easytier.HOME / "private" / "node.toml")

    def test_installed_looks_at_the_home_found_now_not_at_import(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "AgentCompass" / "easytier"
            with mock.patch.object(easytier.easytier_paths, "home", return_value=home):
                self.assertFalse(easytier.installed())
                (home / "bin").mkdir(parents=True)
                for name in ("easytier-core.exe", "easytier-cli.exe"):
                    (home / "bin" / name).write_bytes(b"")
                self.assertTrue(easytier.installed())


class HomeResolutionTests(unittest.TestCase):
    """The node's home is per machine, outside the checkout and the installer's app directory."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.env = {"ProgramData": str(self.base / "ProgramData")}
        self.product = self.base / "ProgramData" / "AgentCompass" / "easytier"

    def node(self, home):
        (home / "bin").mkdir(parents=True)
        (home / "bin" / "easytier-core.exe").write_bytes(b"")

    def test_the_product_home_is_under_programdata(self):
        self.assertEqual(easytier_paths.product_home(self.env), self.product)
        self.assertEqual(easytier_paths.product_parent(self.env), self.product.parent)

    def test_a_frozen_or_wheel_install_never_resolves_into_its_own_directory(self):
        for shape in ("frozen", "wheel"):
            with self.subTest(shape=shape):
                root = self.base / shape
                if shape == "frozen":
                    (root / "_internal").mkdir(parents=True)
                    (root / "AgentCompass.exe").write_bytes(b"")
                else:
                    (root / "agentcompass-0.0.0.dist-info").mkdir(parents=True)
                self.assertEqual(easytier_paths.home(self.env, root=root), self.product)

    def test_a_node_left_in_the_checkout_is_found_until_it_is_migrated(self):
        checkout = self.base / "checkout"
        self.node(checkout / "easytier")
        self.assertEqual(easytier_paths.home(self.env, root=checkout), checkout / "easytier")
        self.node(self.product)
        self.assertEqual(easytier_paths.home(self.env, root=checkout), self.product)

    def test_plugin_root_follows_the_same_resolution(self):
        import plugins

        checkout = self.base / "checkout"
        with mock.patch.dict(easytier_paths.os.environ, self.env, clear=False):
            easytier_paths.os.environ.pop(easytier_paths.OVERRIDE, None)
            self.assertEqual(plugins.plugin_root("easytier", repo_root=checkout), self.product)
            self.node(checkout / "easytier")
            self.assertEqual(plugins.plugin_root("easytier", repo_root=checkout), checkout / "easytier")

    def test_the_override_moves_the_home_and_disables_the_parent_lock(self):
        env = {**self.env, easytier_paths.OVERRIDE: str(self.base / "isolated")}
        self.assertEqual(easytier_paths.product_home(env), self.base / "isolated")
        self.assertIsNone(easytier_paths.product_parent(env))


class SnapshotTests(unittest.TestCase):
    def snap(self, installed=True, task=easytier.TASK_RUNNING, pids=(100,), table=PEERS):
        with mock.patch.object(easytier, "installed", return_value=installed), \
                mock.patch.object(easytier, "task_state", return_value=task), \
                mock.patch.object(easytier, "core_pids", return_value=list(pids)), \
                mock.patch.object(easytier, "peers", return_value=table):
            return easytier.snapshot()

    def test_missing_without_program_or_task(self):
        self.assertEqual(self.snap(installed=False)["state"], easytier.MISSING)
        self.assertEqual(self.snap(task=None)["state"], easytier.MISSING)

    def test_running_needs_the_task_and_an_answering_rpc_portal(self):
        self.assertEqual(self.snap()["state"], easytier.RUNNING)
        self.assertEqual(self.snap(table=None)["state"], easytier.STARTING)
        self.assertEqual(self.snap(pids=())["state"], easytier.STARTING)

    def test_a_core_the_task_did_not_start_is_foreign(self):
        self.assertEqual(self.snap(task=easytier.TASK_READY)["state"], easytier.FOREIGN)

    def test_stopped_and_disabled(self):
        self.assertEqual(self.snap(task=easytier.TASK_READY, pids=())["state"], easytier.STOPPED)
        self.assertEqual(self.snap(task=easytier.TASK_DISABLED, pids=())["state"], easytier.DISABLED)

    def test_the_cli_is_not_run_when_no_core_exists(self):
        with mock.patch.object(easytier, "installed", return_value=True), \
                mock.patch.object(easytier, "task_state", return_value=easytier.TASK_RUNNING), \
                mock.patch.object(easytier, "core_pids", return_value=[]), \
                mock.patch.object(easytier, "peers") as cli:
            easytier.snapshot()
        cli.assert_not_called()


class LabelTests(unittest.TestCase):
    def test_labels_count_remote_peers_only(self):
        self.assertEqual(easytier.label(reading("running", [1], PEERS)), "运行中（2 个对端）")
        self.assertEqual(easytier.label(reading("running", [1], PEERS[:1])), "运行中（无对端）")
        self.assertEqual(easytier.label(reading("missing")), "未安装")
        self.assertEqual(easytier.label(reading("foreign", [9])), "外部实例在运行")

    def test_peer_report_lists_hosts_and_never_the_config(self):
        text = easytier.peer_report(reading("running", [1], PEERS))
        self.assertIn("OTHER 192.0.2.2 p2p 7.7ms", text)
        self.assertIn("relay 中继", text)
        self.assertNotIn("ME", text)
        self.assertEqual(easytier.peer_report(reading("stopped")), "EasyTier：已停止")


class ActionTests(unittest.TestCase):
    def act(self, function, *states, **patches):
        with mock.patch.object(easytier, "snapshot", side_effect=list(states)), \
                mock.patch.object(easytier, "_task_call") as call, \
                mock.patch.object(easytier, "core_pids", return_value=patches.get("pids", [])), \
                mock.patch.object(easytier, "task_state", return_value=patches.get("task", easytier.TASK_READY)), \
                mock.patch.object(easytier.time, "sleep"):
            message = function()
        return message, call

    def test_start_runs_the_task_only_when_stopped(self):
        message, call = self.act(easytier.start, reading("stopped"))
        call.assert_called_once_with("run")
        self.assertIn("已启动", message)

    def test_start_never_duplicates_a_running_or_foreign_core(self):
        for state in (reading("running", [1], PEERS), reading("starting", [1]), reading("foreign", [7])):
            _, call = self.act(easytier.start, state)
            call.assert_not_called()
        message, _ = self.act(easytier.start, reading("foreign", [7]))
        self.assertIn("7", message)

    def test_start_reports_missing_and_disabled(self):
        for state in ("missing", "disabled"):
            _, call = self.act(easytier.start, reading(state))
            call.assert_not_called()

    def test_stop_ends_the_task_and_waits_for_the_core_to_go(self):
        message, call = self.act(easytier.stop, reading("running", [5], PEERS))
        call.assert_called_once_with("stop")
        self.assertEqual(message, "已停止 EasyTier")

    def test_stop_leaves_a_foreign_core_alone(self):
        _, call = self.act(easytier.stop, reading("foreign", [7]))
        call.assert_not_called()

    def test_stop_does_not_claim_success_while_the_core_survives(self):
        with mock.patch.object(easytier, "snapshot", return_value=reading("running", [5], PEERS)), \
                mock.patch.object(easytier, "_task_call"), mock.patch.object(easytier, "core_pids", return_value=[5]), \
                mock.patch.object(easytier, "task_state", return_value=easytier.TASK_READY), \
                mock.patch.object(easytier.time, "sleep"):
            self.assertIn("仍在", easytier.stop(wait=0))

    def test_restart_stops_then_starts(self):
        with mock.patch.object(easytier, "snapshot", side_effect=[reading("running", [5], PEERS), reading("running", [5], PEERS),
                                                                  reading("stopped")]), \
                mock.patch.object(easytier, "_task_call") as call, mock.patch.object(easytier, "core_pids", return_value=[]), \
                mock.patch.object(easytier, "task_state", return_value=easytier.TASK_READY):
            easytier.restart()
        self.assertEqual([c.args[0] for c in call.call_args_list], ["stop", "run"])

    def test_login_start_is_silent_unless_the_task_is_stopped(self):
        for state in ("running", "starting", "foreign", "missing", "disabled"):
            with mock.patch.object(easytier, "snapshot", return_value=reading(state)), \
                    mock.patch.object(easytier, "_task_call") as call:
                self.assertIsNone(easytier.start_if_stopped())
            call.assert_not_called()
        with mock.patch.object(easytier, "snapshot", return_value=reading("stopped")), \
                mock.patch.object(easytier, "_task_call") as call:
            self.assertIn("已启动", easytier.start_if_stopped())
        call.assert_called_once_with("run")


if __name__ == "__main__":
    unittest.main()
