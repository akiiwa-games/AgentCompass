import unittest
from pathlib import Path
from unittest import mock

import easytier

PEERS = [
    {"cost": "Local", "hostname": "ME", "ipv4": "192.0.2.1"},
    {"cost": "p2p", "hostname": "relay", "ipv4": "", "lat_ms": "8.1"},
    {"cost": "p2p", "hostname": "OTHER", "ipv4": "192.0.2.2", "lat_ms": "7.7"},
]


def reading(state, pids=(), peers=None):
    return {"state": state, "pids": list(pids), "peers": peers}


class PathTests(unittest.TestCase):
    def test_everything_lives_under_the_router_directory(self):
        root = Path(easytier.__file__).resolve().parents[1]
        for path in (easytier.CORE, easytier.CLI, easytier.CONFIG, easytier.LOGS, easytier.STATE, easytier.SUPERVISOR):
            self.assertTrue(path.is_relative_to(root / "easytier"), path)
        self.assertEqual(easytier.CORE, root / "easytier" / "bin" / "easytier-core.exe")
        self.assertEqual(easytier.CONFIG, root / "easytier" / "private" / "node.toml")


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
