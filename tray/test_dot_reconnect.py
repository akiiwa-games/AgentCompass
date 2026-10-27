import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dot_reconnect as dr

NOW = 1_800_000_000.0
BASE = "https://new-address.trycloudflare.com"
MCP = BASE + "/mcp/secret-token"


class DueTests(unittest.TestCase):
    def test_the_registered_address_needs_nothing(self):
        self.assertFalse(dr.due({"url": BASE}, BASE, NOW))

    def test_a_new_address_is_due(self):
        self.assertTrue(dr.due({"url": "https://old"}, BASE, NOW))

    def test_a_failed_address_waits_before_the_next_try_and_stops_after_the_last(self):
        failed = {"url": "https://old", "failed_url": BASE, "failed_at": NOW - 60, "tries": 1}
        self.assertFalse(dr.due(failed, BASE, NOW))
        self.assertTrue(dr.due(failed, BASE, NOW + dr.RETRY_SECONDS))
        self.assertFalse(dr.due({**failed, "tries": dr.MAX_TRIES}, BASE, NOW + 10 * dr.RETRY_SECONDS))

    def test_a_failure_on_an_older_address_does_not_hold_back_the_new_one(self):
        state = {"url": "https://old", "failed_url": "https://older", "failed_at": NOW, "tries": dr.MAX_TRIES}
        self.assertTrue(dr.due(state, BASE, NOW))


class TurnTests(unittest.TestCase):
    def log(self, directory, *events):
        path = Path(directory) / "launcher.jsonl"
        path.write_text("".join(json.dumps({"event": event}) + "\n" for event in events), encoding="utf-8")
        return path

    def test_a_turn_without_an_end_is_active(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(dr.turn_active(self.log(directory, "browser.turn_started", "browser.turn_ended", "browser.turn_started")))
            self.assertFalse(dr.turn_active(self.log(directory, "browser.turn_started", "browser.turn_ended")))
            self.assertFalse(dr.turn_active(self.log(directory, "other")))
            self.assertFalse(dr.turn_active(Path(directory) / "missing.jsonl"))


class ReconnectTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state_path = Path(directory.name) / "dot-plugin.json"
        patches = [
            mock.patch.object(dr, "STATE", self.state_path),
            mock.patch.object(dr, "published", return_value=(BASE, MCP)),
            mock.patch.object(dr, "bridge_endpoint", return_value="http://127.0.0.1:1"),
            mock.patch.object(dr, "turn_active", return_value=False),
            mock.patch.object(dr.services, "dot_ready", return_value=True),
            mock.patch.object(dr.services, "tunnel_pids", return_value=[5]),
            mock.patch.object(dr.services, "chatgpt_ready", return_value=True),
            mock.patch.object(dr.services, "http_ok", return_value=True),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.calls = []

    def node(self, results):
        def run(endpoint, *arguments):
            self.calls.append(arguments)
            return results[arguments[0]]
        return mock.patch.object(dr, "run_node", side_effect=run)

    def test_empty_state_bootstraps_only_this_host_and_sends_one_wake(self):
        with self.node({"register": {"ok": True}, "wake": {"ok": True}}):
            self.assertIn("已自动更新", dr.reconnect(NOW))
        self.assertEqual([c[0] for c in self.calls], ["register", "wake"])
        self.assertEqual(dr.load_state()["host"], dr.HOST)

    def test_nothing_runs_while_a_prerequisite_is_missing(self):
        dr.save_state({"url": "https://old", "name": "old"})
        for target, value in (("turn_active", True), ("bridge_endpoint", None)):
            with mock.patch.object(dr, target, return_value=value), self.node({}):
                self.assertIsNone(dr.reconnect(NOW))
        for target, value in (("dot_ready", False), ("tunnel_pids", []), ("chatgpt_ready", False), ("http_ok", False)):
            with mock.patch.object(dr.services, target, return_value=value), self.node({}):
                self.assertIsNone(dr.reconnect(NOW))
        self.assertEqual(self.calls, [])

    def test_a_new_address_adds_the_plugin_wakes_the_dot_and_removes_the_dead_plugins(self):
        old, older = dr.BASE_NAME + " old", dr.BASE_NAME + " older"
        dr.save_state({"url": "https://old", "name": old, "old_names": [older, "Dot OTHERHOST old", "本机 Dot"]})
        with self.node({"register": {"ok": True}, "wake": {"ok": True}, "remove": {"ok": True}}):
            message = dr.reconnect(NOW)
        self.assertEqual([call[0] for call in self.calls], ["register", "wake", "remove"])
        self.assertEqual(self.calls[2][1:], (old, older))
        self.assertIn("已自动更新", message)
        state = dr.load_state()
        self.assertEqual((state["url"], state["old_names"]), (BASE, []))
        self.assertTrue(state["name"].startswith(dr.BASE_NAME + " "))

    def test_the_secret_url_is_passed_in_a_file_not_on_the_command_line(self):
        dr.save_state({"url": "https://old", "name": "old"})
        with self.node({"register": {"ok": True}, "wake": {"ok": True}, "remove": {"ok": True}}):
            dr.reconnect(NOW)
        for call in self.calls:
            self.assertNotIn(MCP, " ".join(call))

    def test_a_refused_address_is_recorded_reported_once_and_retried_later(self):
        dr.save_state({"url": "https://old", "name": "old", "old_names": []})
        refused = {"register": {"ok": False, "step": "create", "error": "refused"}}
        with self.node(refused):
            self.assertIn("失败", dr.reconnect(NOW))
            self.assertIsNone(dr.reconnect(NOW + 10))
            second = dr.reconnect(NOW + dr.RETRY_SECONDS + 1)
        self.assertIsNone(second)
        state = dr.load_state()
        self.assertEqual((state["url"], state["failed_url"], state["tries"]), ("https://old", BASE, 2))
        self.assertEqual([call[0] for call in self.calls], ["register", "register"])

    def test_a_failed_wake_is_reported_but_the_new_plugin_is_kept(self):
        dr.save_state({"url": "https://old", "name": "old"})
        with self.node({"register": {"ok": True}, "wake": {"ok": False, "step": "type", "error": "x"}, "remove": {"ok": True}}):
            message = dr.reconnect(NOW)
        self.assertIn("没能给 Dot 发消息", message)
        self.assertEqual(dr.load_state()["url"], BASE)
        self.assertTrue(dr.load_state()["wake_pending"])
        self.assertFalse(dr.due(dr.load_state(), BASE, NOW + 100000))
        self.assertNotIn("remove", [call[0] for call in self.calls])

    def test_dead_plugins_stay_on_the_list_when_removing_them_fails(self):
        old = dr.BASE_NAME + " old"
        dr.save_state({"url": "https://old", "name": old})
        with self.node({"register": {"ok": True}, "wake": {"ok": True}, "remove": {"ok": False, "step": "x", "error": "y"}}):
            dr.reconnect(NOW)
        self.assertEqual(dr.load_state()["old_names"], [old])

    def test_wake_preserves_other_hosts_and_does_not_cancel_their_subscriptions(self):
        message = dr.wake_message(dr.BASE_NAME + " new")
        self.assertIn(dr.HOST, message)
        self.assertIn("保留所有其他主机的插件、队列订阅和任务", message)
        self.assertIn("只替换本主机", message)


if __name__ == "__main__":
    unittest.main()
