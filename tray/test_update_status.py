import tempfile
import unittest
from pathlib import Path
from unittest import mock

import plugins
import update_status
import updates
from test_app import app



def setUpModule():
    # start_all / restart_services / update_all read plugins.json; never the real one.
    global _plugins_isolation
    directory = tempfile.TemporaryDirectory()
    _plugins_isolation = (directory, mock.patch.object(plugins, "PATH", Path(directory.name) / "plugins.json"))
    _plugins_isolation[1].start()


def tearDownModule():
    _plugins_isolation[1].stop()
    _plugins_isolation[0].cleanup()

class UpdateProgressTests(unittest.TestCase):
    def test_progress_survives_component_failure_and_can_be_reopened(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(update_status, "DIRECTORY", Path(directory)), \
                mock.patch.object(plugins, "PATH", Path(directory) / "plugins.json"), \
                mock.patch.object(updates, "product_notice", return_value=""), \
                mock.patch.object(updates, "update_dot2api", return_value="Dot2API 已是最新"), \
                mock.patch.object(updates, "update_chatgpt_web", side_effect=RuntimeError("offline")), \
                mock.patch.object(updates, "update_usage_float", return_value="Usage Float 已更新"), \
                mock.patch.object(updates, "update_gemini_web", return_value="Gemini Web 已是最新"):
            observed = []
            def progress(event):
                update_status.record(event)
                observed.append(update_status.read_status())
            updates.update_all(progress=progress)
            self.assertTrue(observed[0]["running"])
            self.assertFalse(observed[-1]["running"])
            self.assertEqual(list(observed[-1]["results"]), ["Dot2API", "ChatGPT Web", "Usage Float", "Gemini Web"])
            self.assertIn("offline", observed[-1]["results"]["ChatGPT Web"])
            self.assertIn("Usage Float 已更新", (Path(directory) / "updates.log").read_text(encoding="utf-8"))

    def test_click_opens_progress_before_the_update_thread_starts(self):
        events = []
        thread = mock.Mock()
        thread.start.side_effect = lambda: events.append("thread")
        with mock.patch.object(app.update_status, "open_window", side_effect=lambda: events.append("window")), \
                mock.patch.object(app.threading, "Thread", return_value=thread):
            app.act(mock.Mock(), updates.update_all)
        self.assertEqual(events, ["window", "thread"])

    def test_duplicate_click_does_not_erase_running_progress(self):
        with updates.LOCK, mock.patch.object(update_status, "record") as record:
            self.assertIn("正在更新", updates.update_all(progress=record))
        record.assert_not_called()

    def test_cli_update_all_records_structured_progress_in_shared_update_status(self):
        import cli_updates

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory)
            target = cli_updates.Target(
                "codex", "Codex CLI", cli_updates.Install(cli_updates.BY_ID["codex"], state_path / "codex.cmd", "npm"), ["codex"]
            )
            def fake_update(ident, *args, outcome):
                outcome.update(status="current", message="Codex CLI 已是最新（0.2.0）")
                return "Codex CLI 已是最新（0.2.0）"

            with mock.patch.object(cli_updates, "state_dir", return_value=state_path), \
                    mock.patch.object(cli_updates, "targets", return_value=[target]), \
                    mock.patch.object(cli_updates, "update", side_effect=fake_update) as installer:
                result = cli_updates.update_all()
            installer.assert_called_once()
            self.assertNotIn("deferred", result)
            status = update_status.read_status(state_path)
            self.assertFalse(status["running"])
            self.assertEqual(status["targets"], ["Codex CLI"])
            self.assertIn("已是最新", status["results"]["Codex CLI"])
            self.assertIn("CLI：", result["summary"])

    def test_unverified_and_failed_cli_results_remain_visible_as_needing_attention(self):
        import cli_updates
        import i18n
        for language in ("zh", "en"):
            for outcome_status in ("unverified", "failed"):
                with self.subTest(language=language, status=outcome_status), tempfile.TemporaryDirectory() as directory, \
                        mock.patch.object(i18n, "get_language", return_value=language):
                    state_path = Path(directory)
                    target = cli_updates.Target("agy", "Antigravity", cli_updates.Install(
                        cli_updates.BY_ID["agy"], state_path / "agy.exe", "native"), ["agy"])
                    def fake_update(ident, *args, outcome):
                        outcome.update(status=outcome_status, message="opaque vendor result")
                        return "opaque vendor result"
                    with mock.patch.object(cli_updates, "state_dir", return_value=state_path), \
                            mock.patch.object(cli_updates, "targets", return_value=[target]), \
                            mock.patch.object(cli_updates, "update", side_effect=fake_update):
                        batch = cli_updates.update_all()
                    state = update_status.read_status(state_path)
                    self.assertEqual(batch[outcome_status], ["agy"])
                    self.assertEqual(batch["current"], [])
                    self.assertEqual(batch["updated"], [])
                    self.assertEqual(state["statuses"]["Antigravity"], outcome_status)
                    self.assertNotEqual(update_status.completion_heading(state), update_status.completion_heading(
                        {"statuses": {"Antigravity": "current"}}))


if __name__ == "__main__":
    unittest.main()
