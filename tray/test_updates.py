import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import chatgpt_web_patch as web_patch
import plugins
import services
import updates
from test_chatgpt_web_patch import FILES



def git(root: Path, *arguments: str) -> str:
    done = subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True, text=True)
    return done.stdout.strip()


class Upstream:
    """A bare 'GitHub' repository, a clone of it to update, and a second clone to push new commits from.

    Built once with git per project, then copied for each test.
    """

    def __init__(self, base: Path, name: str, files: dict[str, str] | None = None, template: "Upstream | None" = None):
        self.bare = base / f"{name}.git"
        self.clone = base / name
        self.pusher = base / f"{name}-pusher"
        if template is not None:
            for mine, theirs in ((self.bare, template.bare), (self.clone, template.clone), (self.pusher, template.pusher)):
                shutil.copytree(theirs, mine)
            for repository in (self.clone, self.pusher):
                git(repository, "remote", "set-url", "origin", str(self.bare))
            return
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.bare)], check=True)
        subprocess.run(["git", "clone", "-q", str(self.bare), str(self.clone)], check=True, capture_output=True)
        for relative, text in files.items():
            target = self.clone / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(text.encode("utf-8"))
        for repository in (self.clone,):
            for key, value in (("user.email", "test@example.invalid"), ("user.name", "test"), ("core.autocrlf", "false")):
                git(repository, "config", key, value)
        git(self.clone, "add", ".")
        git(self.clone, "commit", "-q", "-m", "first")
        git(self.clone, "branch", "-M", "main")
        git(self.clone, "push", "-q", "-u", "origin", "main")
        subprocess.run(["git", "clone", "-q", str(self.bare), str(self.pusher)], check=True, capture_output=True)
        for key, value in (("user.email", "test@example.invalid"), ("user.name", "test"), ("core.autocrlf", "false")):
            git(self.pusher, "config", key, value)

    def publish(self, relative: str, text: str) -> None:
        target = self.pusher / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
        git(self.pusher, "add", "--", relative)
        git(self.pusher, "commit", "-q", "-m", f"change {relative}")
        git(self.pusher, "push", "-q", "origin", "main")

    def head(self) -> str:
        return git(self.clone, "rev-parse", "HEAD")


TEMPLATES: dict = {}


def setUpModule():
    # start_all / restart_services / update_all read plugins.json; never the real one.
    global _plugins_isolation
    directory = tempfile.TemporaryDirectory()
    _plugins_isolation = (directory, mock.patch.object(plugins, "PATH", Path(directory.name) / "plugins.json"))
    _plugins_isolation[1].start()
    TEMPLATES["directory"] = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    base = Path(TEMPLATES["directory"].name)
    TEMPLATES["dot"] = Upstream(base, "dot2api", {"pyproject.toml": "[project]\n", "src/app.py": "print(1)\n"})
    TEMPLATES["web"] = Upstream(base, "codex-chatgpt-web", {**FILES, "README.md": "readme\n"})


def tearDownModule():
    TEMPLATES["directory"].cleanup()
    _plugins_isolation[1].stop()
    _plugins_isolation[0].cleanup()


class UpdateTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.dot = Upstream(self.base, "dot2api", template=TEMPLATES["dot"])
        self.web = Upstream(self.base, "codex-chatgpt-web", template=TEMPLATES["web"])
        web_patch.apply(self.web.clone)
        patches = [
            mock.patch.object(services, "DOT_ROOT", self.dot.clone),
            mock.patch.object(services, "CHATGPT_ROOT", self.web.clone),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.calls = []
        uf_update = mock.patch.object(updates, "update_usage_float", return_value="Usage Float 已是最新")
        uf_update.start()
        self.addCleanup(uf_update.stop)
        gemini_update = mock.patch.object(updates, "update_gemini_web", return_value="Gemini Web 已是最新")
        gemini_update.start()
        self.addCleanup(gemini_update.stop)
        self.build_failures = 0
        self.sync_failures = 0

        def record(name, result=None, after=None):
            def fake(*arguments, **keywords):
                self.calls.append(name)
                if after:
                    after()
                return result
            return fake

        def build():
            self.calls.append("build")
            if self.build_failures:
                self.build_failures -= 1
                raise RuntimeError("vite failed")

        def sync():
            self.calls.append("sync")
            if self.sync_failures:
                self.sync_failures -= 1
                raise RuntimeError("no network")

        for target, name, value in (
            (services, "stop_dot2api", record("stop_dot")), (services, "start_dot2api", record("start_dot")),
            (services, "stop_chatgpt_web", record("stop_web", "已停止 ChatGPT Web", lambda: self.state.update(web_pid=None))),
            (services, "start_chatgpt_web", record("start_web", None, lambda: self.state.update(web_pid=60))),
            (updates, "build", build), (updates, "sync_dot2api", sync),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.state = {"dot_ready": True, "web_pid": 50, "busy": False}
        for target, name, function in (
            (services, "dot_ready", lambda: self.state["dot_ready"]),
            (services, "chatgpt_busy", lambda: self.state["busy"]),
            (services, "chatgpt_main_pid", lambda: self.state["web_pid"]),
        ):
            patcher = mock.patch.object(target, name, function)
            patcher.start()
            self.addCleanup(patcher.stop)


class DotUpdateTests(UpdateTestCase):
    def test_an_up_to_date_clone_is_left_alone(self):
        self.assertIn("已是最新", updates.update_dot2api())
        self.assertEqual(self.calls, [])

    def test_a_newer_upstream_is_pulled_and_the_service_restarted_around_it(self):
        before = self.dot.head()
        self.dot.publish("src/app.py", "print(2)\n")
        message = updates.update_dot2api()
        self.assertNotEqual(self.dot.head(), before)
        self.assertEqual((self.dot.clone / "src/app.py").read_text(), "print(2)\n")
        self.assertEqual(self.calls, ["stop_dot", "sync", "start_dot"])
        self.assertIn("已更新", message)

    def test_a_service_that_was_down_stays_down(self):
        self.state["dot_ready"] = False
        self.dot.publish("src/app.py", "print(2)\n")
        updates.update_dot2api()
        self.assertEqual(self.calls, ["sync"])

    def test_the_tunnel_is_kept_when_the_service_is_stopped(self):
        self.dot.publish("src/app.py", "print(2)\n")
        with mock.patch.object(services, "stop_dot2api") as stop:
            updates.update_dot2api()
        stop.assert_called_once_with(tunnel=False)

    def test_local_changes_stop_the_update_before_anything_is_touched(self):
        self.dot.publish("src/app.py", "print(2)\n")
        (self.dot.clone / "src/app.py").write_text("mine\n")
        before = self.dot.head()
        self.assertIn("本地改动", updates.update_dot2api())
        self.assertEqual(self.dot.head(), before)
        self.assertEqual(self.calls, [])
        self.assertEqual((self.dot.clone / "src/app.py").read_text(), "mine\n")

    def test_a_failed_dependency_sync_returns_to_the_old_commit_and_restarts(self):
        before = self.dot.head()
        self.dot.publish("src/app.py", "print(2)\n")
        self.sync_failures = 1
        message = updates.update_dot2api()
        self.assertEqual(self.dot.head(), before)
        self.assertIn("失败", message)
        self.assertIn("已回到原版本", message)
        self.assertEqual(self.calls[-1], "start_dot")


class ChatGPTWebUpdateTests(UpdateTestCase):
    def patched(self) -> bool:
        return web_patch.status(self.web.clone) == "patched"

    def test_extra_features_in_an_adapter_file_are_not_discarded_on_update(self):
        self.web.publish("README.md", "new\n")
        file = self.web.clone / web_patch.MAIN
        file.write_bytes(file.read_bytes() + b'\nfunction myExistingFeature() { return 42; }\n')
        message = updates.update_chatgpt_web()
        self.assertIn("本地改动", message)
        self.assertIn("myExistingFeature", file.read_text())
        self.assertTrue(self.patched())

    def test_an_up_to_date_clone_is_left_alone(self):
        self.assertIn("已是最新", updates.update_chatgpt_web())
        self.assertEqual(self.calls, [])

    def test_a_request_in_flight_postpones_the_update(self):
        self.state["busy"] = True
        self.web.publish("README.md", "new\n")
        before = self.web.head()
        self.assertIn("正在处理请求", updates.update_chatgpt_web())
        self.assertEqual(self.web.head(), before)
        self.assertEqual(self.calls, [])

    def test_a_newer_upstream_is_pulled_patched_again_built_and_started(self):
        before = self.web.head()
        self.web.publish("README.md", "new\n")
        message = updates.update_chatgpt_web()
        self.assertNotEqual(self.web.head(), before)
        self.assertTrue(self.patched())
        self.assertEqual(self.calls, ["stop_web", "build", "start_web"])
        self.assertIn("已更新", message)

    def test_a_launcher_that_was_down_stays_down(self):
        self.state["web_pid"] = None
        self.web.publish("README.md", "new\n")
        updates.update_chatgpt_web()
        self.assertEqual(self.calls, ["build"])

    def test_a_launcher_that_will_not_stop_is_not_updated(self):
        before = self.web.head()
        self.web.publish("README.md", "new\n")
        with mock.patch.object(services, "stop_chatgpt_web", return_value="busy") as stop:
            message = updates.update_chatgpt_web()
        stop.assert_called_once()
        self.assertIn("没能停下", message)
        self.assertEqual(self.web.head(), before)
        self.assertTrue(self.patched())

    def test_upstream_moving_a_patched_line_rolls_back_to_the_working_version(self):
        before = self.web.head()
        moved = FILES[web_patch.SESSION].replace("aria-label=\"Stop\"", "aria-label=\"Halt\"")
        self.web.publish(web_patch.SESSION, moved)
        message = updates.update_chatgpt_web()
        self.assertEqual(self.web.head(), before)
        self.assertTrue(self.patched())
        self.assertIn("失败", message)
        self.assertIn("已回到原版本", message)
        self.assertEqual(self.calls[-1], "start_web")

    def test_a_failed_build_rolls_back_too(self):
        before = self.web.head()
        self.web.publish("README.md", "new\n")
        self.build_failures = 1
        message = updates.update_chatgpt_web()
        self.assertIn("vite failed", message)
        self.assertIn("已回到原版本", message)
        self.assertEqual(self.web.head(), before)
        self.assertEqual((self.web.clone / "README.md").read_text(), "readme\n")
        self.assertTrue(self.patched())

    def test_changes_beyond_the_patches_are_kept_and_stop_the_update(self):
        self.web.publish("README.md", "new\n")
        (self.web.clone / "README.md").write_text("mine\n")
        before = self.web.head()
        message = updates.update_chatgpt_web()
        self.assertEqual(self.web.head(), before)
        self.assertEqual((self.web.clone / "README.md").read_text(), "mine\n")
        self.assertTrue(self.patched())
        self.assertIn("本地改动", message)


class UpdateAllTests(UpdateTestCase):
    def test_both_clones_are_updated_one_after_the_other(self):
        self.dot.publish("src/app.py", "print(2)\n")
        self.web.publish("README.md", "new\n")
        message = updates.update_all()
        self.assertIn("Dot2API 已更新", message)
        self.assertIn("ChatGPT Web 已更新", message)
        self.assertIn("Usage Float 已是最新", message)
        self.assertLess(self.calls.index("start_dot"), self.calls.index("stop_web"))

    def test_one_failure_does_not_stop_the_other_update(self):
        self.web.publish("README.md", "new\n")
        with mock.patch.object(updates, "update_dot2api", side_effect=RuntimeError("offline")):
            message = updates.update_all()
        self.assertIn("Dot2API 更新出错：offline", message)
        self.assertIn("ChatGPT Web 已更新", message)

    def test_a_second_update_started_meanwhile_is_refused(self):
        self.assertTrue(updates.LOCK.acquire(blocking=False))
        self.addCleanup(lambda: updates.LOCK.locked() and updates.LOCK.release())
        self.assertIn("正在更新", updates.update_all())
        updates.LOCK.release()
        self.assertNotIn("正在更新", updates.update_all())

    def test_all_enabled_git_plugins_are_updated_in_order(self):
        with mock.patch.object(updates, "update_dot2api", return_value="dot") as dot, \
                mock.patch.object(updates, "update_chatgpt_web", return_value="web") as web, \
                mock.patch.object(updates, "update_usage_float", return_value="uf") as uf, \
                mock.patch.object(updates, "update_gemini_web", return_value="gemini") as gemini:
            self.assertEqual(updates.update_all(), "dot；web；uf；gemini")
        dot.assert_called_once()
        web.assert_called_once()
        uf.assert_called_once()
        gemini.assert_called_once()


if __name__ == "__main__":
    unittest.main()
