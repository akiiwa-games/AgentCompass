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
                    mock.patch.object(updates, "product_notice", return_value=""), \
                    mock.patch.object(updates, "update_dot2api") as dot, \
                    mock.patch.object(updates, "update_chatgpt_web", return_value="web"), \
                    mock.patch.object(updates, "update_usage_float", return_value="uf"), \
                    mock.patch.object(updates, "update_gemini_web", return_value="gemini"):
                message = updates.update_all()
        dot.assert_not_called()
        self.assertEqual(message, "Dot2API 插件未启用，已跳过；web；uf；gemini")


    def test_plugin_root_resolves_user_data_directory_and_auto_clones_when_missing(self):
        self.addCleanup(services.refresh_plugin_roots)      # ensure_plugin re-points services' module roots at the temporary directory
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
        for key in ("AGENTCOMPASS_GITHUB_ORG", "AGENTCOMPASS_USAGE_FLOAT_REPO", "ROUTE_MANAGER_GITHUB_ORG",
                    "ROUTE_MANAGER_USAGE_FLOAT_REPO"):
            env.pop(key, None)
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
                os.environ, self._env(AGENTCOMPASS_GITHUB_ORG="override-org"), clear=True
            ):
                self.assertEqual(
                    plugins._sibling_repo("usage-float"),
                    "https://github.com/override-org/usage-float.git",
                )
            with mock.patch.object(plugins, "ROOT", root), mock.patch.dict(
                os.environ, self._env(AGENTCOMPASS_USAGE_FLOAT_REPO="C:/local/usage-float"), clear=True
            ):
                self.assertEqual(
                    plugins._sibling_repo("usage-float"),
                    "C:/local/usage-float",
                )


class IntegrationsPageEndToEndRegressionTests(unittest.TestCase):
    """End-to-end regression tests for the '外部集成' (plugins) page in an isolated temp profile."""

    def setUp(self):
        import account_login
        import dot_reconnect
        import easytier
        import gateways
        import grok_bot_cli
        import i18n
        import network_status
        import router_data
        import router_window
        import tk_test_support
        import update_status

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.state_dir = self.base / ".agent-compass"
        self.plugins_path = self.state_dir / "plugins.json"
        self.plugins_dir = self.state_dir / "plugins"
        self.repo_root = self.base / "repo"
        self.dot_dir = self.base / ".dot2api"
        self.muse_home = self.plugins_dir / "muse2api" / "home"
        self.muse_log = self.state_dir / "muse2api.log"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        self.repo_root.mkdir(parents=True, exist_ok=True)
        self.dot_dir.mkdir(parents=True, exist_ok=True)

        self.account_login = account_login
        self.dot_reconnect = dot_reconnect
        self.easytier = easytier
        self.gateways = gateways
        self.grok_bot_cli = grok_bot_cli
        self.i18n = i18n
        self.network_status = network_status
        self.router_data = router_data
        self.router_window = router_window
        self.tk_test_support = tk_test_support
        self.update_status = update_status

        for patcher in (
            mock.patch.object(router_data, "STATE_DIR", self.state_dir, create=True),
            mock.patch.object(i18n, "settings_path", return_value=self.state_dir / "ui-settings.json"),
            mock.patch.object(plugins, "STATE_DIR", self.state_dir),
            mock.patch.object(plugins, "PATH", self.plugins_path),
            mock.patch.object(plugins, "PLUGINS_DIR", self.plugins_dir),
            mock.patch.object(plugins, "ROOT", self.repo_root),
            mock.patch.object(services, "STATE_DIR", self.state_dir),
            mock.patch.object(services, "LOG_DIR", self.dot_dir),
            mock.patch.object(update_status, "DIRECTORY", self.state_dir),
            mock.patch.object(gateways, "MUSE_HOME", self.muse_home),
            mock.patch.object(gateways, "MUSE_LOG", self.muse_log),
            mock.patch.object(router_window.theme, "set_window_icon"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(setattr, i18n, "_cache_key", None)
        i18n.set_language("zh")

    def _mark_deployed(self, *plugin_ids: str) -> None:
        for pid in plugin_ids:
            (self.plugins_dir / pid / ".git").mkdir(parents=True, exist_ok=True)

    def _make_panel(self, sync_run: bool = True, labels: dict | None = None,
                    easytier_reading: dict | None = None, grok_reading: dict | None = None,
                    tunnel_url: str = "https://demo.trycloudflare.com", subscription: str = "订阅正常"):
        root = self.tk_test_support.shared_tk_root(self)
        root.withdraw()
        label_map = {
            "usage-float": "运行中",
            "chatgpt-web": "运行中",
            "gemini-web": "已停止",
            "dot2api": "运行中",
            "muse2api": "未安装",
        }
        if labels:
            label_map.update(labels)
        et_snap = easytier_reading or {"state": "running", "pids": [10], "peers": [{"cost": "p2p"}, {"cost": "p2p"}]}
        gb_snap = grok_reading or {"installed": False, "label": "CLI 未安装", "kind": "idle", "detail": ""}
        fake_data = mock.Mock()
        fake_data.dot_details.return_value = {"tunnel_url": tunnel_url}
        fake_data.subscription_text.return_value = subscription

        for patcher in (
            mock.patch.object(services, "plugin_label", side_effect=lambda pid: label_map[pid]),
            mock.patch.object(self.easytier, "snapshot", return_value=et_snap),
            mock.patch.object(self.easytier, "label", side_effect=lambda r=None: (
                "未安装" if (r or et_snap)["state"] == "missing"
                else "已停止" if (r or et_snap)["state"] == "stopped"
                else "运行中（2 个对端）"
            )),
            mock.patch.object(self.grok_bot_cli, "snapshot", return_value=gb_snap),
            mock.patch.object(self.grok_bot_cli, "info", return_value="Grok Bot 运行时未安装"),
            mock.patch.object(self.router_window.Panel, "data_module", return_value=fake_data),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        if sync_run:
            def immediate_run(panel_self, work, done=None, message=None, long=False):
                if message:
                    panel_self.status.set(message)
                try:
                    val = work()
                except Exception as exc:
                    panel_self.status.set(f"操作失败：{exc}")
                    return
                if done:
                    done(val)
                elif isinstance(val, str) and val:
                    panel_self.status.set(val)

            run_patch = mock.patch.object(self.router_window.Panel, "run", immediate_run)
            run_patch.start()
            self.addCleanup(run_patch.stop)

        panel = self.router_window.Panel(root, "plugins")
        self.addCleanup(lambda: root.after_cancel(panel._poll_id) if getattr(panel, "_poll_id", None) else None)
        return panel

    def test_concurrent_set_flag_writes_preserve_all_checkbox_changes_and_boolean_schema(self):
        import threading

        # Force concurrent read-modify-write contention across all 7 plugins on the same plugins.json
        original_load = plugins.load
        barrier = threading.Barrier(len(plugins.PLUGINS))
        errors = []

        def contending_set(plugin_id: str, flag: str, val: bool):
            try:
                barrier.wait(timeout=5)
                for _ in range(5):
                    plugins.set_flag(plugin_id, flag, val, self.plugins_path)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=contending_set, args=(p.id, "enabled", idx % 2 == 0))
            for idx, p in enumerate(plugins.PLUGINS)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(errors, [])

        raw = json.loads(self.plugins_path.read_text(encoding="utf-8"))
        self.assertEqual(set(raw.keys()), {"v", "plugins"})
        self.assertEqual(raw["v"], 1)
        for idx, p in enumerate(plugins.PLUGINS):
            entry = raw["plugins"][p.id]
            self.assertEqual(set(entry.keys()), {"enabled", "autostart"})
            self.assertIsInstance(entry["enabled"], bool)
            self.assertIsInstance(entry["autostart"], bool)
            self.assertEqual(entry["enabled"], idx % 2 == 0)
        self.assertEqual(list(self.state_dir.glob("*.tmp")), [])
        loaded = original_load(self.plugins_path)
        for idx, p in enumerate(plugins.PLUGINS):
            self.assertEqual(loaded[p.id]["enabled"], idx % 2 == 0)

    def test_checkbox_toggles_persist_across_reopen_and_in_flight_load_never_reverts_ui(self):
        self._mark_deployed("usage-float", "chatgpt-web", "gemini-web", "dot2api")
        jobs = []

        def queued_run(panel_self, work, done=None, message=None, long=False):
            if message:
                panel_self.status.set(message)
            jobs.append((work, done))

        with mock.patch.object(self.router_window.Panel, "run", queued_run), \
                mock.patch.object(services, "plugin_stop", return_value="已停止服务"), \
                mock.patch.object(services, "plugin_start", return_value="已启动服务"):
            panel = self._make_panel(sync_run=False)
            # Initial load_plugins job is queued; snapshot its work before toggling checkboxes
            initial_work, initial_done = jobs.pop(0)
            stale_snapshot = initial_work()

            # User unchecks dot2api enabled and chatgpt-web autostart while initial load is in flight
            panel.plugin_widgets["dot2api"]["enabled"].set(False)
            panel.toggle_enabled("dot2api", False)
            panel.plugin_widgets["chatgpt-web"]["autostart"].set(False)
            panel.toggle_autostart("chatgpt-web", False)

            # Even if the stale initial load callback runs before toggle jobs finish, UI checkboxes must not revert
            initial_done(stale_snapshot)
            self.assertFalse(panel.plugin_widgets["dot2api"]["enabled"].get())
            self.assertFalse(panel.plugin_widgets["chatgpt-web"]["autostart"].get())

            # Settings file must already be updated with pure booleans on disk
            saved = json.loads(self.plugins_path.read_text(encoding="utf-8"))
            self.assertIs(saved["plugins"]["dot2api"]["enabled"], False)
            self.assertIs(saved["plugins"]["chatgpt-web"]["autostart"], False)

            # Drain the queued toggle jobs and their follow-up load_plugins refreshes
            while jobs:
                w, d = jobs.pop(0)
                res = w()
                if d:
                    d(res)

            self.assertFalse(panel.plugin_widgets["dot2api"]["enabled"].get())
            self.assertFalse(panel.plugin_widgets["chatgpt-web"]["autostart"].get())
            self.assertTrue(panel.plugin_widgets["usage-float"]["enabled"].get())
            self.assertIn("已停用 Dot2API", panel.status.get())

            # Rapid on->off toggle with out-of-order worker completion must not overwrite newer False write
            service_calls = []
            with mock.patch.object(services, "plugin_stop", side_effect=lambda pid: service_calls.append("stop") or "已停止"), \
                    mock.patch.object(services, "plugin_start", side_effect=lambda pid: service_calls.append("start") or "已启动"):
                plugins.set_flag("chatgpt-web", "enabled", False, self.plugins_path)
                panel.toggle_enabled("chatgpt-web", True)
                panel.plugin_widgets["chatgpt-web"]["enabled"].set(False)
                panel.toggle_enabled("chatgpt-web", False)
                (w1, d1), (w2, d2) = jobs[:2]
                del jobs[:2]
                r2 = w2()
                r1 = w1()
                d2(r2)
                d1(r1)
                while jobs:
                    w, d = jobs.pop(0)
                    v = w()
                    if d:
                        d(v)
            self.assertIs(plugins.load(self.plugins_path)["chatgpt-web"]["enabled"], False)
            self.assertFalse(panel.plugin_widgets["chatgpt-web"]["enabled"].get())
            self.assertEqual(service_calls, ["stop"])

        # Reopen a fresh Panel instance to simulate tray / window restart: state must persist
        panel2 = self._make_panel(sync_run=True)
        self.assertFalse(panel2.plugin_widgets["dot2api"]["enabled"].get())
        self.assertFalse(panel2.plugin_widgets["chatgpt-web"]["autostart"].get())
        self.assertTrue(panel2.plugin_widgets["usage-float"]["enabled"].get())
        self.assertIn("未启用", panel2.plugin_widgets["dot2api"]["status"].cget("text"))

    def test_displayed_status_reflects_actual_installation_enablement_and_peers(self):
        # None of the repo plugins are cloned yet; easytier is running but peer reading failed (peers=None)
        panel = self._make_panel(
            sync_run=True,
            labels={
                "usage-float": "已停止",
                "chatgpt-web": "已停止",
                "gemini-web": "未安装",
                "dot2api": "已停止",
                "muse2api": "未安装",
            },
            easytier_reading={"state": "running", "pids": [10], "peers": None},
            tunnel_url="",
        )
        # Uncloned repo plugins must show '未安装' (or '未启用 · 未安装'), never '已停止'
        for pid in ("usage-float", "chatgpt-web", "gemini-web", "dot2api"):
            self.assertEqual(panel.plugin_widgets[pid]["status"].cget("text").strip(), "未安装", pid)
            self.assertEqual(panel.plugin_widgets[pid]["info"].get(), "未下载 · 启用后自动部署", pid)
        self.assertEqual(panel.plugin_widgets["muse2api"]["status"].cget("text").strip(), "未启用 · 未安装")
        self.assertEqual(panel.plugin_widgets["easytier"]["info"].get(), "对端读取失败")

        # Mark plugins deployed and running; gemini-web stays idle ('已停止', autostart=False)
        self._mark_deployed("usage-float", "chatgpt-web", "gemini-web", "dot2api")
        panel2 = self._make_panel(sync_run=True)
        self.assertEqual(panel2.plugins_pill.cget("text").strip(), "正常")
        self.assertEqual(panel2.plugins_status.get(), "集成：5 已启用 · 4 运行中")
        self.assertEqual(panel2.plugin_widgets["easytier"]["info"].get(), "可见对端 2 个")
        self.assertIn("https://demo.trycloudflare.com", panel2.plugin_widgets["dot2api"]["info"].get())

        # English long status labels must not be chopped at 18 chars
        self.i18n.set_language("en")
        plugins.set_flag("muse2api", "enabled", False, self.plugins_path)
        panel_en = self._make_panel(
            sync_run=True,
            labels={"muse2api": "Not installed", "dot2api": "Server running, tunnel stopped"},
        )
        self.assertEqual(panel_en.plugin_widgets["muse2api"]["status"].cget("text").strip(), "Disabled · Not installed")
        self.assertEqual(panel_en.plugin_widgets["dot2api"]["status"].cget("text").strip(), "Server running, tunnel stopped")
        self.assertEqual(panel_en.plugins_pill.cget("text").strip(), "Attention")

    def test_enabling_undownloaded_gemini_web_deploys_it_and_autostart_starts_it(self):
        panel = self._make_panel(sync_run=True)
        with mock.patch.object(updates, "ensure_plugin", return_value="Gemini Web 已下载并部署（v1）") as ensure, \
                mock.patch.object(services, "plugin_start", return_value="已启动 Gemini Web") as start:
            # Default autostart=False on gemini-web: enabling when not cloned deploys it without opening browser
            panel.toggle_enabled("gemini-web", True)
            ensure.assert_called_once_with("gemini-web")
            start.assert_not_called()
            self.assertIn("已启用 Gemini Web；Gemini Web 已下载并部署（v1）", panel.status.get())

            # When user enables autostart on gemini-web and enables it, plugin_start is called
            self._mark_deployed("gemini-web")
            panel.toggle_autostart("gemini-web", True)
            panel.toggle_enabled("gemini-web", True)
            start.assert_called_once_with("gemini-web")
            self.assertIn("已启用 Gemini Web；已启动 Gemini Web", panel.status.get())

    def test_account_badge_immediately_reflects_plugin_config_switch(self):
        allocator = mock.Mock()
        allocator.host_policy.account_allowed.return_value = True
        with mock.patch.object(self.router_data, "modules", return_value=(allocator, mock.Mock())):
            # Stale inventory says ready, but plugins.json disabled dot2api
            badge, kind = self.router_data.account_badge(
                "dot", {"status": "ready"}, {"dot2api": {"enabled": False, "autostart": True}}, None
            )
            self.assertEqual((badge, kind), ("集成已关闭", "idle"))
            # Stale inventory says plugin_disabled_in_agentcompass, but user re-enabled dot2api
            badge2, kind2 = self.router_data.account_badge(
                "dot",
                {"status": "paused", "reason": "plugin_disabled_in_agentcompass"},
                {"dot2api": {"enabled": True, "autostart": True}},
                None,
            )
            self.assertEqual((badge2, kind2), ("已启用，等待刷新", "idle"))

    def test_save_retries_permission_error_and_ui_toggle_recovers_when_file_locked_by_subprocess(self):
        import subprocess
        import sys

        self._mark_deployed("usage-float", "chatgpt-web", "gemini-web", "dot2api")
        plugins.save(plugins.defaults(), self.plugins_path)
        panel = self._make_panel(sync_run=True)

        # 1. Brief lock held by a real reader subprocess (~60ms): save() retries within ~200ms and succeeds
        brief_reader = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys,time;f=open(sys.argv[1],encoding='utf-8');sys.stdout.write('open\\n');sys.stdout.flush();time.sleep(0.06)",
                str(self.plugins_path),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(brief_reader.stdout.readline().strip(), "open")
            plugins.set_flag("dot2api", "enabled", False, self.plugins_path)
            self.assertIs(plugins.load(self.plugins_path)["dot2api"]["enabled"], False)
        finally:
            brief_reader.wait(timeout=5)
            if brief_reader.stdout:
                brief_reader.stdout.close()

        # 2. Persistent lock held by a real reader subprocess across the retry budget:
        # UI toggle catches PermissionError, reports in status bar, restores checkbox to disk state, and does not start/stop.
        plugins.set_flag("dot2api", "enabled", True, self.plugins_path)
        panel.load_plugins(probe=False)
        self.assertTrue(panel.plugin_widgets["dot2api"]["enabled"].get())

        long_reader = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys;f=open(sys.argv[1],encoding='utf-8');sys.stdout.write('open\\n');sys.stdout.flush();sys.stdin.readline()",
                str(self.plugins_path),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(long_reader.stdout.readline().strip(), "open")
            with mock.patch.object(services, "plugin_stop") as stop_fn, \
                    mock.patch.object(services, "plugin_start") as start_fn:
                panel.plugin_widgets["dot2api"]["enabled"].set(False)
                panel.toggle_enabled("dot2api", False)
                self.assertIn("操作失败：", panel.status.get())
                self.assertTrue(panel.plugin_widgets["dot2api"]["enabled"].get())
                self.assertIs(plugins.load(self.plugins_path)["dot2api"]["enabled"], True)
                stop_fn.assert_not_called()
                start_fn.assert_not_called()

                panel.plugin_widgets["chatgpt-web"]["autostart"].set(False)
                panel.toggle_autostart("chatgpt-web", False)
                self.assertIn("操作失败：", panel.status.get())
                self.assertTrue(panel.plugin_widgets["chatgpt-web"]["autostart"].get())
                self.assertIs(plugins.load(self.plugins_path)["chatgpt-web"]["autostart"], True)
                self.assertEqual(list(self.state_dir.glob("*.tmp")), [])
        finally:
            if long_reader.stdin:
                try:
                    long_reader.stdin.write("\n")
                    long_reader.stdin.flush()
                    long_reader.stdin.close()
                except OSError:
                    pass
            long_reader.wait(timeout=5)
            if long_reader.stdout:
                long_reader.stdout.close()

    def test_update_components_covers_progress_up_to_date_updated_partial_fail_network_fail_and_disabled_skip(self):
        self._mark_deployed("usage-float", "chatgpt-web", "gemini-web", "dot2api")
        panel = self._make_panel(sync_run=True)

        # Prepare an installed-product root (no .git) so real product_notice -> release_notice runs with fake GitHub responses
        inst_root = self.base / "installed-app"
        (inst_root / "agentcompass").mkdir(parents=True, exist_ok=True)
        (inst_root / "agentcompass" / "__init__.py").write_text('__version__ = "0.4.2"\n', encoding="utf-8")

        class _FakeGithubResponse:
            def __init__(self, body: bytes):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self) -> bytes:
                return self.body

        def _fake_opener(tag_name: str):
            payload = json.dumps(
                {
                    "tag_name": tag_name,
                    "html_url": f"https://github.com/akiiwa-games/AgentCompass/releases/tag/{tag_name}",
                }
            ).encode("utf-8")
            return lambda request, timeout=20: _FakeGithubResponse(payload)

        def _failing_opener(request, timeout=20):
            raise OSError("github api timeout")

        real_product_notice = updates.product_notice

        # Disable dot2api; keep chatgpt-web, usage-float, gemini-web enabled; muse2api is disabled by default
        plugins.set_flag("dot2api", "enabled", False, self.plugins_path)

        # 1a. Installed product matching GitHub release ("matches" notice, product_status='current')
        # and all enabled components current -> pill shows '已是最新', notice text stays in status
        matches_notice = updates.release_notice(inst_root, opener=_fake_opener("v0.4.2"))
        self.assertIn("AgentCompass 0.4.2 matches GitHub release v0.4.2. No upgrade is waiting.", matches_notice)
        with mock.patch.object(updates, "update_dot2api") as up_dot, \
                mock.patch.object(updates, "update_chatgpt_web", return_value="ChatGPT Web 已是最新（1111111）"), \
                mock.patch.object(updates, "update_usage_float", return_value="Usage Float 已是最新（2222222）"), \
                mock.patch.object(updates, "update_gemini_web", return_value="Gemini Web 已是最新（3333333）"), \
                mock.patch.object(updates, "update_gateway") as up_muse, \
                mock.patch.object(
                    updates,
                    "product_notice",
                    side_effect=lambda: real_product_notice(inst_root, opener=_fake_opener("v0.4.2")),
                ):
            panel.update_components()
            res = updates.update_all()
        up_dot.assert_not_called()
        up_muse.assert_not_called()
        self.assertFalse(panel.plugins_updating)
        self.assertEqual(panel.plugins_update_button.cget("text"), "检查更新")
        self.assertEqual(panel.plugins_pill.cget("text").strip(), "已是最新")
        self.assertIn("Dot2API 插件未启用，已跳过", panel.plugins_status.get())
        self.assertIn("ChatGPT Web 已是最新", panel.plugins_status.get())
        self.assertIn("matches GitHub release v0.4.2", panel.plugins_status.get())
        self.assertEqual(panel.status.get(), panel.plugins_status.get())
        self.assertEqual(matches_notice.status, "current")
        self.assertFalse(res.busy)
        self.assertEqual(
            res.statuses,
            {"Dot2API": "skipped", "ChatGPT Web": "current", "Usage Float": "current", "Gemini Web": "current"},
        )
        self.assertEqual(res.product_status, "current")
        self.assertEqual(res.product_notice, matches_notice)

        # 1b. Installed product ahead of GitHub release (product_status='ahead') -> pill still shows '已是最新'
        ahead_notice = updates.release_notice(inst_root, opener=_fake_opener("v0.3.0"))
        self.assertIn("AgentCompass 0.4.2 is newer than GitHub release v0.3.0.", ahead_notice)
        with mock.patch.object(updates, "update_dot2api"), \
                mock.patch.object(updates, "update_chatgpt_web", return_value="ChatGPT Web 已是最新（1111111）"), \
                mock.patch.object(updates, "update_usage_float", return_value="Usage Float 已是最新（2222222）"), \
                mock.patch.object(updates, "update_gemini_web", return_value="Gemini Web 已是最新（3333333）"), \
                mock.patch.object(updates, "update_gateway"), \
                mock.patch.object(
                    updates,
                    "product_notice",
                    side_effect=lambda: real_product_notice(inst_root, opener=_fake_opener("v0.3.0")),
                ):
            panel.update_components()
            res_ahead = updates.update_all()
        self.assertEqual(panel.plugins_pill.cget("text").strip(), "已是最新")
        self.assertIn("is newer than GitHub release v0.3.0", panel.plugins_status.get())
        self.assertEqual(ahead_notice.status, "ahead")
        self.assertEqual(res_ahead.product_status, "ahead")

        # 1c. Newer GitHub release (product_status='newer') -> pill shows '需注意' (not '已是最新'),
        # and AgentCompass notice does not overwrite Muse2API in update-status.json
        newer_notice = updates.release_notice(inst_root, opener=_fake_opener("v0.4.3"))
        self.assertEqual(newer_notice.status, "newer")
        self.assertIn("GitHub release v0.4.3 is newer", newer_notice)
        with mock.patch.object(updates, "update_dot2api"), \
                mock.patch.object(updates, "update_chatgpt_web", return_value="ChatGPT Web 已是最新（1111111）"), \
                mock.patch.object(updates, "update_usage_float", return_value="Usage Float 已是最新（2222222）"), \
                mock.patch.object(updates, "update_gemini_web", return_value="Gemini Web 已是最新（3333333）"), \
                mock.patch.object(updates, "update_gateway"), \
                mock.patch.object(
                    updates,
                    "product_notice",
                    side_effect=lambda: real_product_notice(inst_root, opener=_fake_opener("v0.4.3")),
                ):
            res_newer = updates.update_all()
            self.assertEqual(res_newer.product_status, "newer")
            panel.update_components()
        self.assertEqual(panel.plugins_pill.cget("text").strip(), "需注意")
        self.assertIn("GitHub release v0.4.3 is newer", panel.plugins_status.get())
        status_doc = self.update_status.read_status(self.state_dir)
        self.assertNotIn("Muse2API", status_doc.get("results", {}))
        self.assertIn("AgentCompass", status_doc.get("results", {}))

        # 1d. Product release check fails (product_status='failed') -> pill shows '需注意'
        failed_notice = updates.release_notice(inst_root, opener=_failing_opener)
        self.assertEqual(failed_notice.status, "failed")
        self.assertIn("Could not read GitHub releases (github api timeout)", failed_notice)
        with mock.patch.object(updates, "update_dot2api"), \
                mock.patch.object(updates, "update_chatgpt_web", return_value="ChatGPT Web 已是最新（1111111）"), \
                mock.patch.object(updates, "update_usage_float", return_value="Usage Float 已是最新（2222222）"), \
                mock.patch.object(updates, "update_gemini_web", return_value="Gemini Web 已是最新（3333333）"), \
                mock.patch.object(updates, "update_gateway"), \
                mock.patch.object(
                    updates,
                    "product_notice",
                    side_effect=lambda: real_product_notice(inst_root, opener=_failing_opener),
                ):
            res_failed = updates.update_all()
            self.assertEqual(res_failed.product_status, "failed")
            panel.update_components()
        self.assertEqual(panel.plugins_pill.cget("text").strip(), "需注意")
        self.assertIn("Could not read GitHub releases (github api timeout)", panel.plugins_status.get())

        # 1e. updates.LOCK held -> update_all returns busy=True, pill shows '正在更新' (not '已是最新')
        self.assertTrue(updates.LOCK.acquire(blocking=False))
        try:
            busy_res = updates.update_all()
            self.assertTrue(busy_res.busy)
            panel.update_components()
        finally:
            updates.LOCK.release()
        self.assertEqual(panel.plugins_pill.cget("text").strip(), "正在更新")
        self.assertEqual(panel.plugins_status.get(), "正在更新，请稍候")

        # 2. Partial component failure -> pill shows '部分异常' ('warn') and summary persists after load_plugins
        with mock.patch.object(updates, "update_dot2api") as up_dot, \
                mock.patch.object(updates, "update_chatgpt_web", return_value="ChatGPT Web 更新失败：构建失败；已回到原版本"), \
                mock.patch.object(updates, "update_usage_float", return_value="Usage Float 已更新：111 → 222"), \
                mock.patch.object(updates, "update_gemini_web", return_value="Gemini Web 已是最新（333）"), \
                mock.patch.object(updates, "product_notice", return_value=""):
            panel.update_components()
        self.assertEqual(panel.plugins_pill.cget("text").strip(), "部分异常")
        self.assertIn("ChatGPT Web 更新失败", panel.plugins_status.get())
        self.assertIn("Usage Float 已更新：111 → 222", panel.status.get())

        # 3. Network failure / unhandled exception in update_all -> button recovers, pill shows '部分异常'
        with mock.patch.object(updates, "update_all", side_effect=RuntimeError("network unreachable")):
            panel.update_components()
        self.assertFalse(panel.plugins_updating)
        self.assertNotIn("disabled", panel.plugins_update_button.state())
        self.assertEqual(panel.plugins_update_button.cget("text"), "检查更新")
        self.assertEqual(panel.plugins_pill.cget("text").strip(), "部分异常")
        self.assertIn("检查更新失败：network unreachable", panel.plugins_status.get())
        self.assertIn("检查更新失败：network unreachable", panel.status.get())

    def test_all_other_page_controls_preserve_action_feedback_and_resolve_isolated_paths(self):
        self._mark_deployed("usage-float", "chatgpt-web", "gemini-web", "dot2api", "muse2api")
        panel = self._make_panel(sync_run=True, tunnel_url="https://tunnel.trycloudflare.com")

        # 1. Header '重启服务' button preserves restart summary
        with mock.patch.object(services, "restart_services", return_value="已重启 Dot2API 与 ChatGPT Web") as restart_fn:
            panel.restart_services()
            restart_fn.assert_called_once_with()
            self.assertEqual(panel.status.get(), "已重启 Dot2API 与 ChatGPT Web")

        # 2. Toolbar '更新进度' and '更新日志' buttons
        with mock.patch.object(self.update_status, "open_window") as open_win, \
                mock.patch.object(os, "startfile", create=True) as startfile:
            panel.open_updates()
            open_win.assert_called_once_with()
            panel.open_update_log()
            startfile.assert_called_once_with(self.state_dir / "updates.log")
            self.assertEqual(panel.status.get(), "已打开日志 updates.log")

        # 3. Dot2API '复制 URL' and Muse2API '复制 API Key' & '登录导入账号'
        panel.copy_tunnel()
        self.assertEqual(panel.status.get(), "已复制 https://tunnel.trycloudflare.com")
        panel.tunnel_url = ""
        panel.copy_tunnel()
        self.assertEqual(panel.status.get(), "还没有隧道 URL")

        panel.copy_muse_key()
        self.assertIn("还没有", panel.status.get())
        (self.muse_home / "data").mkdir(parents=True, exist_ok=True)
        (self.muse_home / "data" / "api_key").write_text("muse-secret-123\n", encoding="utf-8")
        panel.copy_muse_key()
        self.assertIn("已复制", panel.status.get())

        with mock.patch.object(services, "muse_ready", return_value=True), \
                mock.patch.object(self.account_login, "login_and_import", return_value="已导入 Muse2API 账号"):
            panel.import_account("muse2api")
            self.assertEqual(panel.status.get(), "已导入 Muse2API 账号")

        # 4. Per-card action buttons and isolated log paths
        (self.dot_dir / "serve.log").write_text("dot log", encoding="utf-8")
        (self.state_dir / "chatgpt-web.log").write_text("web log", encoding="utf-8")
        with mock.patch.object(os, "startfile", create=True) as startfile:
            dot_actions = dict(panel.plugin_actions("dot2api"))
            panel.plugin_act("dot2api", dot_actions["日志"])
            startfile.assert_called_once_with(self.dot_dir / "serve.log")
            self.assertEqual(panel.status.get(), "已打开日志 serve.log")

            uf_actions = dict(panel.plugin_actions("usage-float"))
            panel.plugin_act("usage-float", uf_actions["日志"])
            self.assertIn("日志不存在", panel.status.get())
            self.assertIn(str(self.state_dir / "usage-float.log"), panel.status.get())

        # 5. Grok Bot '检查连接' preserves result message in status bar
        with mock.patch.object(self.grok_bot_cli, "check", return_value="Grok Bot 连接正常"):
            panel.check_grok_bot()
            self.assertEqual(panel.status.get(), "Grok Bot 连接正常")


if __name__ == "__main__":
    unittest.main()
