import contextlib
import io
import json
import unittest

import run_dot_task as runner


class ParseTests(unittest.TestCase):
    def test_sse_collects_one_content_delta_and_ignores_keepalive(self):
        chunk = {
            "id": "task_1",
            "choices": [{"delta": {"content": "researched"}, "finish_reason": None}],
        }
        payload = f": keepalive\n\ndata: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()
        reply, task_id, error = runner.parse_sse(payload)
        self.assertEqual(reply, "researched")
        self.assertEqual(task_id, "task_1")
        self.assertIsNone(error)

    def test_sse_error_keeps_task_id(self):
        payload = b'data: {"error": {"message": "later", "task_id": "task_9"}}\n\n'
        reply, task_id, error = runner.parse_sse(payload)
        self.assertEqual(reply, "")
        self.assertEqual(task_id, "task_9")
        self.assertEqual(error, "later")

    def test_reply_text_prefers_content(self):
        self.assertEqual(runner.reply_text({"content": "done", "text": "other"}), "done")
        self.assertEqual(runner.reply_text("plain"), "plain")

    def test_describe_shows_attempts_and_ages_and_survives_missing_fields(self):
        task = {"status": "running", "attempts": 2, "max_attempts": 3, "created_at": "1970-01-01T00:16:40Z", "updated_at": "1970-01-01T00:18:20Z"}
        self.assertEqual(runner.describe(task, now=1500.0), "status=running attempts=2/3 age=500s idle=400s")
        self.assertEqual(runner.describe({"status": "failed"}, now=5.0), "status=failed attempts=None/None")


class WaitTests(unittest.TestCase):
    def setUp(self):
        self.saved = runner.task_view
        self.addCleanup(lambda: setattr(runner, "task_view", self.saved))

    def run_wait(self, task, deadline=10**12):
        runner.task_view = lambda config, task_id: task
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, message = runner.wait_task({}, "task_x", deadline)
        return code, message, err.getvalue()

    def test_a_failed_task_reports_its_id_attempts_and_message_and_exits_1(self):
        code, message, err = self.run_wait({"status": "failed", "error": "lease lost", "attempts": 2, "max_attempts": 3})
        self.assertEqual((code, message), (1, "lease lost"))
        self.assertIn("task_id=task_x", err)
        self.assertIn("FAILED status=failed attempts=2/3", err)

    def test_a_task_still_open_at_the_deadline_exits_3_with_its_id(self):
        code, _, err = self.run_wait({"status": "running", "attempts": 1, "max_attempts": 3}, deadline=0)
        self.assertEqual(code, 3)
        self.assertIn("task_id=task_x", err)
        self.assertIn("OPEN status=running", err)

    def test_status_command_exit_codes_follow_the_runner_convention(self):
        for state, expected in (("completed", 0), ("running", 3), ("failed", 1)):
            runner.task_view = lambda config, task_id, state=state: {"status": state, "error": "boom"}
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(runner.status_command({}, "task_x"), expected)

    def test_submit_and_apply_to_pack_files_and_apply_returned_blocks(self):
        import hashlib
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            src = ws / "hello.py"
            src.write_text("print('old')\n", encoding="utf-8")

            prompt = runner.augment_prompt_with_files(
                "Update hello.py",
                files=[str(src)],
                base_dir=ws,
                expect_files=True,
            )
            self.assertIn("=== FILE: hello.py sha256=", prompt)
            self.assertIn("## File Return Format", prompt)

            new_body = "print('new')\n"
            sha = hashlib.sha256(new_body.encode("utf-8")).hexdigest()
            reply = f"Done.\n=== FILE: hello.py sha256={sha} ===\n{new_body}=== END FILE ===\n"

            saved_req = runner.request
            self.addCleanup(lambda: setattr(runner, "request", saved_req))
            runner.request = lambda cfg, method, path, body, timeout: (
                201,
                json.dumps({"task": {"id": "task_fast_1", "status": "queued"}}).encode("utf-8"),
            )
            runner.task_view = lambda cfg, tid: {"status": "completed", "result": {"content": reply}}

            out = io.StringIO()
            err = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = runner.run_task(
                    {"base_url": "http://127.0.0.1:8765", "api_key": "k", "queue": "dot", "task_ttl": 7200, "completion_timeout": 900},
                    prompt,
                    None,
                    None,
                    60.0,
                    apply_to=ws,
                )
            self.assertEqual(code, 0)
            self.assertIn("task_id=task_fast_1", err.getvalue())
            self.assertIn("applied hello.py: ok", err.getvalue())
            self.assertEqual(src.read_text(encoding="utf-8"), new_body)


if __name__ == "__main__":
    unittest.main()
