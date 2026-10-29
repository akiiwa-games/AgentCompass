import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import plugins
import services
import updates


class PluginStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "plugins.json"

    def test_missing_or_broken_file_means_built_ins_enabled_opt_ins_off_and_gemini_on_demand(self):
        opt_in = {"muse2api"}
        for content in (None, "{not json", json.dumps([1, 2]), json.dumps({"plugins": "x"})):
            with self.subTest(content=content):
                if content is not None:
                    self.path.write_text(content, encoding="utf-8")
                config = plugins.load(self.path)
                self.assertEqual({pid for pid, row in config.items() if not row["enabled"]}, opt_in)
                self.assertFalse(config["gemini-web"]["autostart"])
                self.assertTrue(config["dot2api"]["autostart"])

    def test_stored_values_override_defaults_and_unknown_or_bad_entries_are_ignored(self):
        self.path.write_text(json.dumps({"v": 1, "plugins": {
            "dot2api": {"enabled": False}, "gemini-web": {"autostart": True},
            "chatgpt-web": {"enabled": "no"}, "unknown": {"enabled": False}}}), encoding="utf-8")
        config = plugins.load(self.path)
        self.assertFalse(config["dot2api"]["enabled"])
        self.assertTrue(config["dot2api"]["autostart"])
        self.assertTrue(config["gemini-web"]["autostart"])
        self.assertTrue(config["chatgpt-web"]["enabled"])
        self.assertNotIn("unknown", config)

    def test_the_old_usage_float_marker_migrates_until_plugins_json_exists(self):
        (self.path.parent / "usage-float-autostart-disabled").write_text("", encoding="utf-8")
        self.assertFalse(plugins.load(self.path)["usage-float"]["autostart"])
        self.assertTrue(plugins.load(self.path)["usage-float"]["enabled"])
        plugins.set_flag("usage-float", "autostart", True, self.path)
        self.assertTrue(plugins.load(self.path)["usage-float"]["autostart"])

    def test_save_is_atomic_and_round_trips(self):
        plugins.set_flag("easytier", "enabled", False, self.path)
        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(document["v"], 1)
        self.assertEqual(set(document["plugins"]), set(plugins.BY_ID))
        self.assertFalse(document["plugins"]["easytier"]["enabled"])
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
        with self.assertRaises(ValueError):
            plugins.set_flag("easytier", "visible", True, self.path)
        with self.assertRaises(ValueError):
            plugins.set_flag("nope", "enabled", True, self.path)

    def test_only_enabled_plugins_with_a_window_are_menu_plugins_in_fixed_order(self):
        config = plugins.defaults()
        self.assertEqual([p.id for p in plugins.menu_plugins(config)],
                         ["usage-float", "chatgpt-web", "gemini-web", "easytier"])
        config["usage-float"]["enabled"] = False
        self.assertNotIn("usage-float", [p.id for p in plugins.menu_plugins(config)])
        self.assertFalse(plugins.BY_ID["dot2api"].has_window_action)

    def test_autostart_needs_enabled(self):
        config = plugins.defaults()
        config["dot2api"]["enabled"] = False
        self.assertFalse(plugins.autostarts("dot2api", config))
        self.assertTrue(plugins.autostarts("chatgpt-web", config))


class LifecycleTests(unittest.TestCase):
    def config(self, **changes):
        config = plugins.defaults()
        for plugin_id, row in changes.items():
            config[plugin_id.replace("_", "-")].update(row)
        return config

    def start_all(self, config):
        calls = []
        record = lambda name, result="": (lambda *a, **k: calls.append(name) or result or name)
        with mock.patch.multiple(services, start_dot2api=record("dot"), start_chatgpt_web=record("web"),
                                 start_usage_float=record("uf"), start_gemini_web=record("gemini")), \
                mock.patch.object(services, "USAGE_FLOAT_SCRIPT", mock.Mock(is_file=lambda: True)), \
                mock.patch.object(services, "start_muse2api", side_effect=lambda: calls.append("muse") or "muse"), \
                mock.patch.object(services.easytier, "start_if_stopped", side_effect=lambda: calls.append("et")):
            message = services.start_all(config)
        return calls, message

    def test_login_starts_each_enabled_autostart_plugin(self):
        calls, _ = self.start_all(self.config())
        self.assertEqual(calls, ["dot", "web", "uf", "et"])
        calls, _ = self.start_all(self.config(muse2api={"enabled": True}))
        self.assertEqual(calls, ["dot", "web", "muse", "uf", "et"])

    def test_disabled_plugins_never_start(self):
        config = self.config()
        for row in config.values():
            row["enabled"] = False
        calls, message = self.start_all(config)
        self.assertEqual(calls, [])
        self.assertEqual(message, "核心服务已就绪")
        config = self.config(dot2api={"autostart": False}, gemini_web={"autostart": True})
        calls, _ = self.start_all(config)
        self.assertNotIn("dot", calls)
        self.assertIn("gemini", calls)

    def test_restart_touches_only_enabled_bridges(self):
        calls = []
        record = lambda name: (lambda *a, **k: calls.append(name) or name)
        with mock.patch.multiple(services, stop_dot2api=record("stop_dot"), stop_chatgpt_web=record("stop_web"),
                                 start_dot2api=record("start_dot"), start_chatgpt_web=record("start_web")), \
                mock.patch.object(services.time, "sleep"):
            services.restart_services(self.config(dot2api={"enabled": False}))
        self.assertEqual(calls, ["stop_web", "start_web"])

    def test_plugin_actions_stop_only_the_window_of_ui_plugins(self):
        with mock.patch.object(services, "stop_component_ui", return_value="closed") as stop:
            self.assertEqual(services.plugin_stop("usage-float"), "closed")
        stop.assert_called_once_with("usage-float")
        with self.assertRaises(ValueError):
            services.plugin_start("nope")

    def test_update_skips_disabled_plugins(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plugins.json"
            plugins.set_flag("dot2api", "enabled", False, path)
            with mock.patch.object(plugins, "PATH", path), \
                    mock.patch.object(updates, "update_dot2api") as dot, \
                    mock.patch.object(updates, "update_chatgpt_web", return_value="web"), \
                    mock.patch.object(updates, "update_usage_float", return_value="uf"), \
                    mock.patch.object(updates, "update_gemini_web", return_value="gemini"):
                message = updates.update_all()
        dot.assert_not_called()
        self.assertEqual(message, "Dot2API 插件未启用，已跳过；web；uf；gemini")


    def test_plugin_root_resolves_user_data_directory_and_auto_clones_when_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            plugins_dir = Path(directory) / "plugins"
            repo_root = Path(directory) / "repo"
            target = plugins.plugin_root("usage-float", plugins_dir=plugins_dir, repo_root=repo_root)
            self.assertEqual(target, plugins_dir / "usage-float")
            with mock.patch.object(plugins, "PLUGINS_DIR", plugins_dir), \
                    mock.patch.object(updates, "run") as run_cmd, \
                    mock.patch.object(updates, "version", return_value="abcdef"):
                def fake_clone(args, *a, **k):
                    (plugins_dir / "usage-float" / ".git").mkdir(parents=True, exist_ok=True)
                    return ""
                run_cmd.side_effect = fake_clone
                msg = updates.ensure_plugin("usage-float")
            self.assertIn("已下载并部署", msg)
            self.assertTrue((plugins_dir / "usage-float" / ".git").exists())


class SiblingRepoTests(unittest.TestCase):
    def _env(self, **extra):
        env = os.environ.copy()
        env.pop("ROUTE_MANAGER_GITHUB_ORG", None)
        env.pop("ROUTE_MANAGER_USAGE_FLOAT_REPO", None)
        env.update(extra)
        return env

    def test_packaged_fallback_uses_akiiwa_games(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(plugins, "ROOT", root), mock.patch.dict(os.environ, self._env(), clear=True):
                self.assertEqual(
                    plugins._sibling_repo("usage-float"),
                    "https://github.com/akiiwa-games/usage-float.git",
                )
                self.assertNotIn("agent-compass", plugins._sibling_repo("gemini-web"))

    def test_env_org_and_git_origin_override_the_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git = root / ".git"
            git.mkdir()
            (git / "config").write_text(
                '[remote "origin"]\n\turl = git@github.com:fork-org/AgentCompass.git\n',
                encoding="utf-8",
            )
            with mock.patch.object(plugins, "ROOT", root), mock.patch.dict(os.environ, self._env(), clear=True):
                self.assertEqual(
                    plugins._sibling_repo("usage-float"),
                    "https://github.com/fork-org/usage-float.git",
                )
            with mock.patch.object(plugins, "ROOT", root), mock.patch.dict(
                os.environ, self._env(ROUTE_MANAGER_GITHUB_ORG="override-org"), clear=True
            ):
                self.assertEqual(
                    plugins._sibling_repo("usage-float"),
                    "https://github.com/override-org/usage-float.git",
                )


if __name__ == "__main__":
    unittest.main()
