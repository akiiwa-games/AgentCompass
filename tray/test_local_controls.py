import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

import local_controls as controls


class ControlsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patcher = mock.patch.object(controls, "DIRECTORY", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def server(self):
        server = controls.ControlServer("usage-float")
        self.addCleanup(server.close)
        return server

    def test_client_receives_ack_and_action_runs_on_the_ui_polling_thread(self):
        server = self.server()
        calls, results = [], []
        server.publish([{"label": "刷新", "id": "refresh"}],
                       {"refresh": lambda: calls.append(threading.get_ident())})
        worker = threading.Thread(target=lambda: results.append(controls.send_action("usage-float", "refresh", timeout=2)))
        worker.start()
        try:
            deadline = time.monotonic() + 2
            while worker.is_alive() and time.monotonic() < deadline:
                server.poll()
                time.sleep(0.01)
        finally:
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(calls, [threading.get_ident()])
        self.assertEqual(results, ["操作完成"])
        self.assertEqual(list(server.directory.glob("*.request.json")), [])
        self.assertEqual(list(server.directory.glob("*.response.json")), [])

    def test_request_for_old_instance_never_reaches_new_ui(self):
        server = self.server()
        action = mock.Mock()
        server.publish([{"label": "退出", "id": "quit"}], {"quit": action})
        request = server.directory / "old.request.json"
        controls.write_json(request, {"instance": "old", "action": "quit", "expires": time.time() + 1})
        server.poll()
        action.assert_not_called()
        self.assertFalse(json.loads((server.directory / "old.response.json").read_text(encoding="utf-8"))["ok"])

    def test_expired_requests_are_removed_without_running_them(self):
        server = self.server()
        action = mock.Mock()
        server.publish([], {"quit": action})
        request = server.directory / "expired.request.json"
        controls.write_json(request, {"instance": server.instance, "action": "quit", "expires": 0})
        server.poll()
        action.assert_not_called()
        self.assertFalse(request.exists())

    def test_timeout_cleans_request_so_it_cannot_run_later(self):
        server = self.server()
        server.publish([], {})
        with self.assertRaises(TimeoutError):
            controls.send_action("usage-float", "refresh", timeout=0.05)
        self.assertFalse(list(server.directory.glob("*.request.json")))

    def test_reused_pid_does_not_make_a_stale_menu_look_alive(self):
        server = self.server()
        server.publish([], {})
        with mock.patch.object(controls.psutil, "Process") as process:
            process.return_value.is_running.return_value = True
            process.return_value.create_time.return_value = time.time() + 100
            self.assertIsNone(controls.read_snapshot("usage-float"))

    def test_old_server_cannot_remove_new_servers_snapshot(self):
        old = self.server()
        old.publish([], {})
        new = self.server()
        new.publish([], {})
        old.close()
        self.assertEqual(controls.read_snapshot("usage-float")["instance"], new.instance)

    def test_unchanged_snapshot_is_not_rewritten(self):
        server = self.server()
        server.publish([], {})
        with mock.patch.object(controls, "write_json") as write:
            server.publish([], {})
        write.assert_not_called()


class ComponentLifecycleTests(unittest.TestCase):
    def test_component_action_during_update_does_not_launch_or_send_commands(self):
        import services
        with mock.patch.object(services, "UPDATING_COMPONENTS", {"usage-float"}), \
                mock.patch.object(services, "start_usage_float") as start, \
                mock.patch.object(services.local_controls, "send_action") as send:
            self.assertIn("更新中", services.component_action("usage-float", "show"))
        start.assert_not_called()
        send.assert_not_called()

    def test_usage_float_launches_from_the_migrated_path_once(self):
        import services
        with mock.patch.object(services, "_component_children", {}), \
                mock.patch.object(services.USAGE_FLOAT_SCRIPT.__class__, "is_file", return_value=True), \
                mock.patch.object(services.local_controls, "read_snapshot", return_value=None), \
                mock.patch.object(services, "hidden_popen") as popen:
            popen.return_value.poll.return_value = None
            services.start_usage_float()
            services.start_usage_float()
        popen.assert_called_once()
        args, env, cwd, _log = popen.call_args.args
        self.assertEqual(Path(args[1]), services.ROOT / "tray" / "usage_float_host.py")
        self.assertEqual(cwd, services.USAGE_FLOAT_ROOT)
        self.assertEqual(env["AGENTCOMPASS_HOSTED"], "1")


if __name__ == "__main__":
    unittest.main()
