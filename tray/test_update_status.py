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


if __name__ == "__main__":
    unittest.main()
