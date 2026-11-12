import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

import deploy
import usage_float_host as host


class HostTests(unittest.TestCase):
    def setUp(self):
        with host._autostart_lock:
            self._saved_autostart = dict(host._autostart_state)
            host._autostart_state.update(
                value=None, checked_at=0.0, refreshing=False, generation=0
            )

    def tearDown(self):
        with host._autostart_lock:
            host._autostart_state.clear()
            host._autostart_state.update(self._saved_autostart)

    def scope(self):
        return {"state": {"refreshing": False, "always_on_top": True, "display_mode": "panel", "panel_active": True},
                **{name: mock.Mock() for name in ("refresh_async", "request_open_settings", "toggle_panel_mode",
                   "toggle_topmost", "do_quit", "set_wallpaper_active", "stack_text_above_plate")},
                "bg_layer": mock.Mock()}

    def test_menu_uses_native_callbacks_and_preserves_panel_background_behavior(self):
        root, scope = mock.Mock(), self.scope()
        with mock.patch.object(host.local_controls, "ControlServer") as server:
            host.attach(root, scope)
        actions = server.return_value.publish.call_args.args[1]
        self.assertIs(actions["settings"], scope["request_open_settings"])
        self.assertIs(actions["panel"], scope["toggle_panel_mode"])
        self.assertIs(actions["quit"], scope["do_quit"])
        actions["refresh"]()
        scope["refresh_async"].assert_called_once_with(force=True)
        actions["show"]()
        scope["bg_layer"].deiconify.assert_not_called()
        root.deiconify.assert_called_once()

    def test_interface_drift_is_reported_before_registering_commands(self):
        with mock.patch.object(host.local_controls, "ControlServer") as server:
            with self.assertRaisesRegex(RuntimeError, "接口变化"):
                host.attach(mock.Mock(), {})
        server.assert_not_called()

    def test_is_autostart_enabled_never_blocks_ui_thread_and_updates_cache_async(self):
        gate = threading.Event()
        done = threading.Event()

        def slow_startup_enabled():
            gate.wait(timeout=1.0)
            done.set()
            return False

        with mock.patch.object(deploy, "startup_enabled", side_effect=slow_startup_enabled):
            t0 = time.perf_counter()
            initial = host.is_autostart_enabled()
            elapsed = time.perf_counter() - t0
            self.assertTrue(initial)
            self.assertLess(elapsed, 0.05)
            gate.set()
            self.assertTrue(done.wait(timeout=1.0))
            for _ in range(20):
                if host._autostart_state["value"] is False:
                    break
                time.sleep(0.01)
            self.assertFalse(host.is_autostart_enabled())

    def test_set_autostart_updates_cached_state_immediately_and_runs_deploy_async(self):
        applied = threading.Event()

        def fake_remove():
            applied.set()

        with mock.patch.object(deploy, "remove_startup", side_effect=fake_remove) as remove_mock, \
                mock.patch.object(deploy, "startup_enabled", return_value=False):
            ok, msg = host.set_autostart(False)
            self.assertTrue(ok)
            self.assertIn("开机启动", msg)
            self.assertFalse(host.is_autostart_enabled())
            self.assertTrue(applied.wait(timeout=1.0))
            remove_mock.assert_called_once()


class ControlRecoveryTests(unittest.TestCase):
    class Root:
        def __init__(self):
            self.callbacks = []
            self.alive = True

        def winfo_exists(self):
            return self.alive

        def after(self, delay, callback):
            self.callbacks.append(callback)

        def tick(self):
            self.callbacks.pop(0)()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        patcher = mock.patch.object(host.local_controls, "DIRECTORY", Path(temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = self.Root()
        self.scope = HostTests().scope()

    def attach(self):
        server = host.attach(self.root, self.scope)
        self.addCleanup(server.close)
        return server

    @unittest.skipUnless(os.name == "nt", "Windows readers deny file replacement")
    def test_windows_reader_conflict_preserves_snapshot_and_commands_recover(self):
        server = self.attach()
        path = server.directory / "menu.json"
        original = path.read_bytes()
        self.scope["state"]["refreshing"] = True
        with path.open("rb"), self.assertLogs(host.__name__, level="ERROR") as logs:
            self.root.tick()
            self.root.tick()
        self.assertEqual(len(logs.records), 1)
        self.assertIsInstance(logs.records[0].exc_info[1], PermissionError)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(len(self.root.callbacks), 1)

        request = server.directory / "recover.request.json"
        host.local_controls.write_json(request, {
            "instance": server.instance, "action": "refresh", "expires": time.time() + 10,
        })
        self.root.tick()
        snapshot = host.local_controls.read_snapshot("usage-float")
        refresh = next(item for item in snapshot["items"] if item.get("id") == "refresh")
        self.assertFalse(refresh["enabled"])
        self.scope["refresh_async"].assert_called_once_with(force=True)
        self.assertTrue(json.loads((server.directory / "recover.response.json").read_text())["ok"])
        self.assertEqual(len(self.root.callbacks), 1)

    def test_first_publish_failure_retries_without_aborting_ui_start(self):
        with mock.patch.object(host.local_controls, "write_json", side_effect=PermissionError("busy")), \
                self.assertLogs(host.__name__, level="ERROR"):
            server = self.attach()
        self.assertFalse((server.directory / "menu.json").exists())
        self.root.tick()
        self.assertEqual(host.local_controls.read_snapshot("usage-float")["instance"], server.instance)

    def test_request_poll_failure_retries_and_logs_again_after_recovery(self):
        server = self.attach()
        for _ in range(2):
            with mock.patch.object(server, "poll", side_effect=OSError("busy")), \
                    self.assertLogs(host.__name__, level="ERROR") as logs:
                self.root.tick()
                self.root.tick()
            self.assertEqual(len(logs.records), 1)
            self.assertIsNotNone(host.local_controls.read_snapshot("usage-float"))
            self.root.tick()
            self.assertEqual(len(self.root.callbacks), 1)

    def test_quit_cleans_snapshot_and_does_not_schedule_more_callbacks(self):
        server = self.attach()
        self.scope["do_quit"].side_effect = lambda: setattr(self.root, "alive", False)
        host.local_controls.write_json(server.directory / "quit.request.json", {
            "instance": server.instance, "action": "quit", "expires": time.time() + 10,
        })
        self.root.tick()
        self.assertFalse((server.directory / "menu.json").exists())
        self.assertEqual(self.root.callbacks, [])

    def test_destroyed_tk_interpreter_stops_retries(self):
        from tkinter import TclError
        server = self.attach()
        with mock.patch.object(self.root, "winfo_exists", side_effect=TclError("destroyed")), \
                self.assertLogs(host.__name__, level="ERROR"):
            self.root.tick()
        self.assertFalse((server.directory / "menu.json").exists())
        self.assertEqual(self.root.callbacks, [])


class SharedCacheTests(unittest.TestCase):
    """The hosted Usage Float points at the router's read-only cache file, seeded once from the legacy copy (real files, isolated home)."""

    def setUp(self):
        import json
        import os
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.json = json
        self.native = SimpleNamespace(CACHE_PATH=None)
        env = {"HOME": str(self.home), "USERPROFILE": str(self.home)}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("ROUTER_STATE_DIR", "ROUTER_USAGE_CACHE", "ROUTE_MANAGER_ACTIVITY_FILE", "AGENTCOMPASS_STATE_DIR"):
            os.environ.pop(name, None)
        self.legacy = self.home / ".usage-float" / "usage-cache.json"
        self.legacy.parent.mkdir(parents=True)
        self.legacy.write_text(json.dumps({"providers": {"codex": {"fetched_at": "2026-01-01T00:00:00+00:00", "windows": [{"id": "5h"}]}}}),
                               encoding="utf-8")

    def test_the_hosted_plugin_is_pointed_at_the_router_cache_seeded_from_the_legacy_one(self):
        import os
        path = host.share_cache(self.native)
        self.assertEqual(path, self.home / ".agent-compass" / "usage-cache.json")
        self.assertEqual(self.native.CACHE_PATH, path)
        self.assertEqual(self.json.loads(path.read_text(encoding="utf-8"))["providers"]["codex"]["windows"], [{"id": "5h"}])
        self.assertTrue(self.legacy.is_file())
        self.assertEqual(os.environ["ROUTE_MANAGER_ACTIVITY_FILE"], str(self.home / ".agent-compass" / "usage-activity.json"))

    def test_a_named_cache_file_is_used_as_given_and_nothing_is_seeded(self):
        import os
        named = self.home / "named.json"
        os.environ["ROUTER_USAGE_CACHE"] = str(named)
        self.assertEqual(host.share_cache(self.native), named)
        self.assertFalse((self.home / ".agent-compass" / "usage-cache.json").exists())

    def test_without_the_routers_scripts_the_plugin_keeps_its_own_default(self):
        with mock.patch.object(host.component_launch, "skill_scripts", return_value=None):
            self.assertIsNone(host.share_cache(self.native))
        self.assertIsNone(self.native.CACHE_PATH)


class HostedSingleReaderTests(unittest.TestCase):
    """Verify the single-reader contract: standalone fetches itself; hosted reads shared cache and delegates refresh to usage_core."""

    def setUp(self):
        import importlib.util
        import sys
        from datetime import datetime, timedelta, timezone

        scripts = host.component_launch.skill_scripts()
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        import allocator
        import provider_inventory
        import host_policy
        import usage_cache
        import usage_collector
        import usage_core

        self.allocator = allocator
        self.host_policy = host_policy
        self.usage_collector = usage_collector
        self.usage_core = usage_core

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)

        # Register path reset before env patch so LIFO cleanup restores usage_core after os.environ is restored.
        self.addCleanup(self.usage_core.configure_paths)

        env = {
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "APPDATA": str(self.home / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(self.home / "AppData" / "Local"),
            "ROUTER_PROVIDER_HOME": str(self.home),
            "ROUTER_STATE_DIR": str(self.home / ".agent-compass"),
            "AGENTCOMPASS_PUBLIC_DATA": "off",
            "AGENTCOMPASS_ANTIGRAVITY_PLAN": "off",
            "AGENTCOMPASS_LIVE_MODELS": "off",
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in (
            "ROUTER_USAGE_CACHE",
            "ROUTE_MANAGER_ACTIVITY_FILE",
            "AGENTCOMPASS_STATE_DIR",
            "AGENTCOMPASS_USAGE_CONFIG",
            "ROUTER_HOST_POLICY",
            "ROUTER_ACCOUNTS_FILE",
            "ROUTER_ACCOUNTS_OVERRIDE",
            "ROUTER_CAPACITY",
            "ROUTER_OVERRIDES",
            "CLAUDE_HOME",
            "CLAUDE_HOME_2",
            "CODEX_HOME",
            "CODEX_HOME_2",
            "GROK_HOME",
            "PASEO_CONFIG",
            "PASEO_HOME",
        ):
            os.environ.pop(name, None)

        for target, attr, repl in (
            (Path, "home", classmethod(lambda _cls, h=self.home: h)),
            (usage_cache, "_registry", lambda _name: ""),
            (
                host_policy,
                "policy_path",
                lambda h=self.home: (
                    (Path(os.environ["ROUTER_HOST_POLICY"]).expanduser(), True)
                    if os.environ.get("ROUTER_HOST_POLICY")
                    else (h / ".agent-compass" / "host-policy.json", False)
                ),
            ),
            (provider_inventory, "current", lambda: {"accounts": {}, "launchers": {}}),
        ):
            p = mock.patch.object(target, attr, repl)
            p.start()
            self.addCleanup(p.stop)

        (self.home / ".claude-2").mkdir(parents=True)
        (self.home / ".claude-2" / "settings.json").write_text(
            json.dumps({
                "env": {
                    "ANTHROPIC_BASE_URL": "https://magpie.example",
                    "ANTHROPIC_AUTH_TOKEN": "sk-test-magpie",
                }
            }),
            encoding="utf-8",
        )
        (self.home / ".codex").mkdir(parents=True)
        (self.home / ".codex" / "auth.json").write_text(
            json.dumps({"tokens": {"access_token": "tok-codex", "account_id": "acc-codex"}}),
            encoding="utf-8",
        )
        (self.home / ".grok").mkdir(parents=True)
        (self.home / ".grok" / "auth.json").write_text(
            json.dumps({"sso": "tok-grok-session"}),
            encoding="utf-8",
        )

        self.usage_core.configure_paths(home=self.home)

        now = datetime.now(timezone.utc)
        self.now = now
        self.now_iso = now.isoformat()
        self.reset_5h = (now + timedelta(hours=4)).isoformat()
        self.reset_7d = (now + timedelta(days=5)).isoformat()

        plugin_dir = host.ROOT / "usage-float"
        if str(plugin_dir) not in sys.path:
            sys.path.insert(0, str(plugin_dir))
        mod_name = f"_uf_isolated_{id(self)}"
        spec = importlib.util.spec_from_file_location(mod_name, plugin_dir / "usage_float.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        self.addCleanup(sys.modules.pop, mod_name, None)
        spec.loader.exec_module(module)
        agy_patch = mock.patch.object(module, "find_antigravity_cli", return_value=None)
        agy_patch.start()
        self.addCleanup(agy_patch.stop)
        self.native = module

    def _magpie_payload(self, fable_used: float = 20.0) -> dict:
        return {
            "data": [
                {
                    "id": "alice@example.com",
                    "provider": "claude",
                    "kind": "subscription",
                    "plan": "pro",
                    "readAt": self.now_iso,
                    "windows": [
                        {"name": "5 hours", "used": 10.0, "resetsAt": self.reset_5h},
                        {"name": "7 days", "used": 25.0, "resetsAt": self.reset_7d},
                        {"name": "7 days · Fable", "used": fable_used, "resetsAt": self.reset_7d},
                    ],
                },
                {
                    "id": "bob@example.com",
                    "provider": "claude",
                    "kind": "subscription",
                    "plan": "pro",
                    "readAt": self.now_iso,
                    "windows": [
                        {"name": "5 hours", "used": 30.0, "resetsAt": self.reset_5h},
                        {"name": "7 days", "used": 35.0, "resetsAt": self.reset_7d},
                        {"name": "7 days · Fable", "used": fable_used + 10.0, "resetsAt": self.reset_7d},
                    ],
                },
            ]
        }

    def _codex_payload(self, weekly_used: float = 25.0) -> dict:
        return {
            "plan_type": "plus",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 10.0,
                    "limit_window_seconds": 18000,
                    "reset_at": int(self.now.timestamp()) + 14400,
                },
                "secondary_window": {
                    "used_percent": weekly_used,
                    "limit_window_seconds": 604800,
                    "reset_at": int(self.now.timestamp()) + 432000,
                },
            },
        }

    def _poison_plugin_fetchers_and_writers(self):
        forbidden = AssertionError("Hosted Usage Float must never invoke plugin fetchers or cache writers")
        for attr in (
            "fetch_all",
            "collect_local_usage",
            "fetch_claude_usage",
            "fetch_magpie_claude_usage",
            "fetch_codex_usage",
            "fetch_grok_usage",
            "fetch_pi_usage",
            "fetch_api_proxy_usage",
            "fetch_antigravity_usage",
            "http_json",
            "claude_authorized_json",
            "codex_authorized_json",
            "update_usage_cache",
            "save_usage_cache",
            "cache_put_provider",
            "mark_provider_stale",
            "record_usage_attempt",
            "set_claude_backoff",
            "clear_claude_backoff",
        ):
            patcher = mock.patch.object(self.native, attr, side_effect=forbidden)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(self.native.magpie_usage, "parse_quota", side_effect=forbidden)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run_hosted_ui(self, callback):
        import sys
        import tkinter as tk
        import tk_test_support

        shared_root = tk_test_support.shared_tk_root(self)
        errors = []
        real_thread_start = self.native.threading.Thread.start

        def run_sync_thread(thread_self):
            target = getattr(thread_self, "_target", None)
            if getattr(target, "__name__", "") == "work":
                thread_self.run()
                return
            real_thread_start(thread_self)

        def drain_immediate(root):
            # Execute queued after(0, ...) callbacks on the Tk interpreter without blocking on long timers.
            root.update()

        def fake_mainloop(root, *args, **kwargs):
            scope = sys._getframe(1).f_locals
            ctrl_job = scope["state"].get("double_ctrl_job")
            if ctrl_job is not None:
                try:
                    root.after_cancel(ctrl_job)
                except Exception:
                    pass
                scope["state"]["double_ctrl_job"] = None
            try:
                with mock.patch.object(tk.Wm, "deiconify"), \
                        mock.patch.object(tk.Misc, "focus_set"), \
                        mock.patch.object(tk.Misc, "lift"):
                    drain_immediate(root)
                    callback(root, scope, drain_immediate)
            finally:
                scope["do_quit"]()

        try:
            with mock.patch.object(self.native, "_report_tk_callback_exception", side_effect=lambda *exc: errors.append(exc)), \
                    mock.patch.object(self.native, "_start_mpv_provisioning", return_value=False), \
                    mock.patch.object(self.native, "_hide_from_taskbar"), \
                    mock.patch.object(self.native.threading.Thread, "start", run_sync_thread), \
                    mock.patch.object(tk, "Tk", return_value=shared_root), \
                    mock.patch.object(shared_root, "destroy"), \
                    mock.patch.object(tk.Wm, "deiconify"), \
                    mock.patch.object(tk.Misc, "mainloop", fake_mainloop):
                self.native.run_ui()
        finally:
            try:
                shared_root.wm_attributes("-transparentcolor", "")
            except Exception:
                pass
            try:
                shared_root.overrideredirect(False)
            except Exception:
                pass
            tk_test_support.reset_tk_root(shared_root)
        self.assertEqual(errors, [])

    @staticmethod
    def _collect_label_texts(widget) -> list[str]:
        import tkinter as tk

        out = []
        for child in widget.winfo_children():
            if isinstance(child, tk.Label):
                txt = str(child.cget("text") or "").strip()
                if txt:
                    out.append(txt)
            out.extend(HostedSingleReaderTests._collect_label_texts(child))
        return out

    @staticmethod
    def _find_buttons(widget) -> dict[str, object]:
        import tkinter as tk

        found = {}
        for child in widget.winfo_children():
            if isinstance(child, tk.Button):
                found[str(child.cget("text") or "").strip()] = child
            found.update(HostedSingleReaderTests._find_buttons(child))
        return found

    def test_outdated_plugin_without_seam_fails_fast_without_fallback(self):
        from types import SimpleNamespace

        legacy = SimpleNamespace(CACHE_PATH=None)
        with self.assertRaisesRegex(RuntimeError, "Usage Float 接口变化.*get_usage.*refresh_usage"):
            host.bind_usage(legacy)
        self.assertIsNone(legacy.CACHE_PATH)

    def test_standalone_mode_still_fetches_with_its_own_readers(self):
        calls = []

        def fake_http(url, *, method="GET", headers=None, body=None, timeout=20.0):
            calls.append((method, url))
            return 200, self._magpie_payload(20.0)

        with mock.patch.object(self.native, "http_json", side_effect=fake_http):
            regular = self.native.fetch_display_usage(["claude-2:fable", "claude-2:7d", "claude-2:5h"], [], force=False)
            forced = self.native.fetch_display_usage(["claude-2:fable", "claude-2:7d", "claude-2:5h"], regular, force=True)

        self.assertTrue((self.home / ".usage-float" / "usage-cache.json").is_file())
        self.assertFalse((self.home / ".agent-compass" / "usage-cache.json").exists())
        self.assertIn(("GET", "https://magpie.example/v1/magpie/quotas"), calls)
        self.assertTrue(any(m == "POST" and u.startswith("https://magpie.example/v1/magpie/quotas/refresh") for m, u in calls))
        titles = [r.title for r in self.native.iter_display_rows(forced, ["claude-2:fable", "claude-2:7d", "claude-2:5h"])]
        self.assertIn("claude-2 fable 7d×2", titles)

    def test_hosted_mode_never_calls_plugin_fetchers_and_reads_collector_cache(self):
        shared = self.home / ".agent-compass" / "usage-cache.json"
        self.usage_core.configure_paths(shared, home=self.home)
        with mock.patch.object(self.usage_core, "http_json", return_value=(200, self._magpie_payload(15.0))), \
                mock.patch.object(
                    self.usage_core,
                    "fetch_grok_usage",
                    side_effect=lambda: self._write_grok_via_core(18.0),
                ):
            self.usage_core.fetch_all(["claude-2", "grok"], force=True)

        host.bind_usage(self.native)
        self._poison_plugin_fetchers_and_writers()
        before = shared.read_bytes()

        items = self.native.fetch_display_usage(
            ["claude-2:fable", "claude-2:7d", "claude-2:5h", "grok"],
            [],
            force=False,
        )
        self.assertEqual(shared.read_bytes(), before)
        self.assertTrue(all(isinstance(p, self.native.ProviderUsage) for p in items))
        rows = {r.title: r for r in self.native.iter_display_rows(items, ["claude-2:fable", "claude-2:7d", "claude-2:5h", "grok"])}
        self.assertIn("claude-2 fable 7d×2", rows)
        self.assertEqual(rows["claude-2 fable 7d×2"].left_pct, 160.0)
        self.assertIn("grok", rows)
        self.assertEqual(rows["grok"].left_pct, 82.0)

    def _write_grok_via_core(self, used_pct: float):
        item = self.usage_core.ProviderUsage(
            provider_id="grok",
            display_name="grok",
            plan_label="SuperGrok",
            windows=[self.usage_core.UsageWindow("seven_day", "7d", used_pct, self.reset_7d)],
            fetched_at=self.now_iso,
        )
        self.usage_core.cache_put_provider(item)
        return item

    def test_forced_refresh_collects_and_renders_dispatch_excluded_account_while_router_still_excludes_it(self):
        # Excluding `codex` in `allowed_accounts` only blocks router dispatch; usage must still be collected and rendered.
        (self.home / ".agent-compass").mkdir(parents=True, exist_ok=True)
        (self.home / ".agent-compass" / "host-policy.json").write_text(
            json.dumps({"allowed_accounts": ["claude-2"]}),
            encoding="utf-8",
        )
        (self.home / ".usage-float").mkdir(parents=True, exist_ok=True)
        (self.home / ".usage-float" / "config.json").write_text(
            json.dumps({
                "language": "zh",
                "display_mode": "float",
                "card_width": 360,
                "providers": ["codex:7d", "claude-2:fable", "claude-2:7d", "claude-2:5h"],
                "disabled_providers": ["codex:5h"],
            }),
            encoding="utf-8",
        )
        host.bind_usage(self.native)
        self.assertFalse(self.native.PERIODIC_REFRESH)
        self._poison_plugin_fetchers_and_writers()

        core_http_calls = []

        def fake_core_http(url, *, method="GET", headers=None, body=None, timeout=20.0):
            core_http_calls.append((method, url))
            if "chatgpt.com" in url:
                return 200, self._codex_payload(25.0)
            return 200, self._magpie_payload(30.0)

        selected = ["codex:7d", "claude-2:fable", "claude-2:7d", "claude-2:5h"]
        state = {
            "refreshing": False,
            "always_on_top": True,
            "display_mode": "panel",
            "panel_active": True,
            "providers": [],
        }

        def refresh_async(force: bool = False):
            state["providers"] = self.native.fetch_display_usage(selected, state["providers"], force=force)

        scope = {
            "state": state,
            "refresh_async": mock.Mock(side_effect=refresh_async),
            **{
                name: mock.Mock()
                for name in (
                    "request_open_settings",
                    "toggle_panel_mode",
                    "toggle_topmost",
                    "do_quit",
                    "set_wallpaper_active",
                    "stack_text_above_plate",
                )
            },
            "bg_layer": mock.Mock(),
        }

        with mock.patch.object(self.usage_core, "http_json", side_effect=fake_core_http), \
                mock.patch.object(self.usage_core, "fetch_all", wraps=self.usage_core.fetch_all) as core_fetch, \
                mock.patch.object(host.local_controls, "ControlServer") as server:
            host.attach(mock.Mock(), scope)
            actions = server.return_value.publish.call_args.args[1]
            # First forced refresh collects both codex (dispatch-excluded) and claude-2 via usage_collector.
            actions["refresh"]()
            # Second immediate forced refresh still bypasses the 300s poll throttle (force=True semantics).
            actions["refresh"]()

        self.assertEqual(scope["refresh_async"].call_args_list, [mock.call(force=True), mock.call(force=True)])
        self.assertEqual(
            core_fetch.call_args_list,
            [mock.call(["codex", "claude-2"], force=True), mock.call(["codex", "claude-2"], force=True)],
        )
        refresh_posts = [c for c in core_http_calls if c[0] == "POST" and c[1].startswith("https://magpie.example/v1/magpie/quotas/refresh")]
        self.assertEqual(len(refresh_posts), 4)
        cached_providers = json.loads((self.home / ".agent-compass" / "usage-cache.json").read_text(encoding="utf-8"))["providers"]
        self.assertIn("claude-2", cached_providers)
        self.assertIn("codex", cached_providers)

        rows = {r.title: r for r in self.native.iter_display_rows(state["providers"], selected)}
        self.assertIn("codex 7d", rows)
        self.assertFalse(rows["codex 7d"].failed)
        self.assertEqual(rows["codex 7d"].left_pct, 75.0)
        self.assertIn("claude-2 fable 7d×2", rows)
        self.assertFalse(any(r.failed for r in rows.values()))

        # Render the real Tk HUD rows in hosted mode: codex shows real usage ("75%"), never "更新失败".
        rendered_hud = {}

        def check_hud(_root, ui_scope, _drain):
            rendered_hud["dispatch_excluded"] = self._collect_label_texts(ui_scope["lines_frame"])
            # When an explicit `usage_providers` list narrows usage, the excluded account is omitted cleanly without "更新失败".
            (self.home / ".agent-compass" / "host-policy.json").write_text(
                json.dumps({"allowed_accounts": ["claude-2"], "usage_providers": ["claude-2"]}),
                encoding="utf-8",
            )
            ui_scope["refresh_async"](force=False)
            _drain(_root)
            rendered_hud["explicit_usage_narrowed"] = self._collect_label_texts(ui_scope["lines_frame"])

        self._run_hosted_ui(check_hud)
        self.assertIn("codex 7d", rendered_hud["dispatch_excluded"])
        self.assertIn("75%", rendered_hud["dispatch_excluded"])
        self.assertNotIn("更新失败", rendered_hud["dispatch_excluded"])
        self.assertNotIn("codex 7d", rendered_hud["explicit_usage_narrowed"])
        self.assertNotIn("更新失败", rendered_hud["explicit_usage_narrowed"])

        # Restore dispatch-only exclusion and verify the router still never dispatches to codex despite fresh usage.
        (self.home / ".agent-compass" / "host-policy.json").write_text(
            json.dumps({"allowed_accounts": ["claude-2"]}),
            encoding="utf-8",
        )
        import account_fleet
        fleet = account_fleet.load_accounts(account_fleet.reference_fleet_path())
        import router_test_support
        planned = self.allocator.plan(
            {"work_kind": "general_implementation", "difficulty": "hard", "profile": router_test_support.profile()},
            {"accounts": {"codex": {"status": "fresh", "available": True, "stale": False, "windows": [
                {"id": "usage", "remaining_pct_before_reset": 75.0, "observed_remaining_pct": 75.0, "seconds_until_reset": 86400}
            ]}}},
            now=self.now,
            accounts=fleet,
        )
        self.assertNotEqual((planned.get("selected_bundle") or {}).get("account"), "codex")
        codex_rankings = [r for r in planned["ranking"] if r["bundle"]["account"] == "codex"]
        self.assertTrue(codex_rankings)
        for r in codex_rankings:
            self.assertIn("excluded by host policy", r["excluded"])

    def test_single_writer_keeps_fable_7d_stable_and_notifies_ui_on_external_collector_write(self):
        temp_controls = tempfile.TemporaryDirectory()
        self.addCleanup(temp_controls.cleanup)
        with mock.patch.object(host.local_controls, "DIRECTORY", Path(temp_controls.name)):
            host.bind_usage(self.native)
            self._poison_plugin_fetchers_and_writers()

            # Initial collector write: claude-2 has weekly_fable (20% / 30% -> mean 25%, left 150%).
            with mock.patch.object(self.usage_core, "http_json", return_value=(200, self._magpie_payload(20.0))):
                self.usage_core.fetch_all(["claude-2"], force=True)

            selected = ["claude-2:fable", "claude-2:7d", "claude-2:5h"]
            state = {
                "refreshing": False,
                "always_on_top": True,
                "display_mode": "panel",
                "panel_active": True,
                "providers": self.native.fetch_display_usage(selected, [], force=False),
            }
            refresh_calls = []

            def refresh_async(force: bool = False):
                refresh_calls.append(force)
                state["providers"] = self.native.fetch_display_usage(selected, state["providers"], force=force)

            root = ControlRecoveryTests.Root()
            scope = {
                "state": state,
                "refresh_async": refresh_async,
                **{
                    name: mock.Mock()
                    for name in (
                        "request_open_settings",
                        "toggle_panel_mode",
                        "toggle_topmost",
                        "do_quit",
                        "set_wallpaper_active",
                        "stack_text_above_plate",
                    )
                },
                "bg_layer": mock.Mock(),
            }
            server = host.attach(root, scope, watched_cache=Path(self.native.CACHE_PATH))
            self.addCleanup(server.close)

            # Multiple hosted UI ticks never overwrite providers['claude-2'] or drop weekly_fable.
            for _ in range(3):
                state["providers"] = self.native.fetch_display_usage(selected, state["providers"], force=False)
                rows = {r.title: r for r in self.native.iter_display_rows(state["providers"], selected)}
                self.assertIn("claude-2 fable 7d×2", rows)
                self.assertEqual(rows["claude-2 fable 7d×2"].left_pct, 150.0)

            # External collector updates usage-cache.json on disk (40% / 50% -> mean 45%, left 110%).
            with mock.patch.object(self.usage_core, "http_json", return_value=(200, self._magpie_payload(40.0))):
                self.usage_core.fetch_all(["claude-2"], force=True)

            # Advance 5 control ticks (~1s): attach() sees the new cache signature and triggers refresh_async(force=False).
            for _ in range(5):
                root.tick()
            self.assertEqual(refresh_calls, [False])
            rows = {r.title: r for r in self.native.iter_display_rows(state["providers"], selected)}
            self.assertIn("claude-2 fable 7d×2", rows)
            self.assertEqual(rows["claude-2 fable 7d×2"].left_pct, 110.0)

    def test_lock_contention_during_forced_refresh_never_swallows_external_collector_write(self):
        temp_controls = tempfile.TemporaryDirectory()
        self.addCleanup(temp_controls.cleanup)
        with mock.patch.object(host.local_controls, "DIRECTORY", Path(temp_controls.name)):
            host.bind_usage(self.native)
            self._poison_plugin_fetchers_and_writers()

            # Initial cache: grok has 50% used (50% left), claude-2 has fable 20% / 30% (150% left).
            self._write_grok_via_core(50.0)
            with mock.patch.object(self.usage_core, "http_json", return_value=(200, self._magpie_payload(20.0))):
                self.usage_core.fetch_all(["claude-2"], force=True)

            selected = ["grok", "claude-2:fable"]
            state = {
                "refreshing": False,
                "always_on_top": True,
                "display_mode": "panel",
                "panel_active": True,
                "providers": self.native.fetch_display_usage(selected, [], force=False),
            }
            refresh_calls = []

            def refresh_async(force: bool = False):
                refresh_calls.append(force)
                state["providers"] = self.native.fetch_display_usage(selected, state["providers"], force=force)

            root = ControlRecoveryTests.Root()
            scope = {
                "state": state,
                "refresh_async": refresh_async,
                **{
                    name: mock.Mock()
                    for name in (
                        "request_open_settings",
                        "toggle_panel_mode",
                        "toggle_topmost",
                        "do_quit",
                        "set_wallpaper_active",
                        "stack_text_above_plate",
                    )
                },
                "bg_layer": mock.Mock(),
            }
            server = host.attach(root, scope, watched_cache=Path(self.native.CACHE_PATH))
            self.addCleanup(server.close)

            # Simulate `.fetch.lock` contention on grok during forced refresh, then external collector finishes grok (10% used -> 90% left)
            # before forced refresh returns. Forced refresh must NOT advance attach()'s watched signature past that external write.
            def contending_http(url, *, method="GET", headers=None, body=None, timeout=20.0):
                # Background collector finishes grok while claude-2 HTTP call is in flight.
                self._write_grok_via_core(10.0)
                return 200, self._magpie_payload(20.0)

            with mock.patch.object(self.usage_core, "fetch_grok_usage", side_effect=TimeoutError("locked")), \
                    mock.patch.object(self.usage_core, "http_json", side_effect=contending_http):
                refresh_async(force=True)

            # Immediately after contending forced refresh, grok in memory still has fallback 50% left.
            rows_before = {r.title: r for r in self.native.iter_display_rows(state["providers"], selected)}
            self.assertEqual(rows_before["grok"].left_pct, 50.0)

            # Next 5 control ticks detect the changed cache signature and pick up grok's fresh 90% left reading.
            for _ in range(5):
                root.tick()
            self.assertEqual(refresh_calls, [True, False])
            rows_after = {r.title: r for r in self.native.iter_display_rows(state["providers"], selected)}
            self.assertEqual(rows_after["grok"].left_pct, 90.0)

    def test_hosted_settings_timezone_persists_to_collector_usage_config(self):
        (self.home / ".agent-compass").mkdir(parents=True, exist_ok=True)
        collector_cfg = self.home / ".agent-compass" / "usage-config.json"
        collector_cfg.write_text(json.dumps({"llm_proxy_timezone": "UTC"}), encoding="utf-8")
        (self.home / ".usage-float").mkdir(parents=True, exist_ok=True)
        (self.home / ".usage-float" / "config.json").write_text(
            json.dumps({
                "language": "zh",
                "display_mode": "float",
                "llm_proxy_timezone": "UTC+8",
                "providers": ["claude-2:7d"],
            }),
            encoding="utf-8",
        )
        host.bind_usage(self.native)
        self._poison_plugin_fetchers_and_writers()

        def find_timezone_combobox(widget):
            from tkinter import ttk

            for child in widget.winfo_children():
                if isinstance(child, ttk.Combobox) and "UTC" in tuple(child.cget("values") or ()):
                    return child
                found = find_timezone_combobox(child)
                if found is not None:
                    return found
            return None

        with mock.patch.object(self.usage_core, "http_json", return_value=(200, self._magpie_payload(20.0))):
            def exercise_settings(root, scope, drain):
                # Authoritative collector config ("UTC") wins over stale legacy config ("UTC+8") on load.
                self.assertEqual(scope["state"]["llm_proxy_timezone"], "UTC")
                scope["open_settings"]()
                win = scope["state"]["settings_win"]
                win.update_idletasks()
                combo = find_timezone_combobox(win)
                self.assertIsNotNone(combo)
                combo.set("UTC-5")
                combo.event_generate("<<ComboboxSelected>>")
                drain(root)
                win.tk.call(win.protocol("WM_DELETE_WINDOW"))

            self._run_hosted_ui(exercise_settings)

        saved_collector = json.loads(collector_cfg.read_text(encoding="utf-8"))
        self.assertEqual(saved_collector["llm_proxy_timezone"], "UTC-5")
        self.assertEqual(self.usage_core.read_usage_config()["llm_proxy_timezone"], "UTC-5")

    def test_hosted_reset_popups_route_through_product_and_respect_429_backoff(self):
        from tkinter import messagebox
        import tkinter as tk

        (self.home / ".claude").mkdir(parents=True)
        (self.home / ".claude" / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "tok-claude-primary"}}),
            encoding="utf-8",
        )
        (self.home / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"organizationUuid": "org-claude-1"}}),
            encoding="utf-8",
        )
        # Dynamic Codex account (.codex-3) + decoy CODEX_HOME env var to verify zero home drift.
        os.environ["CODEX_HOME"] = str(self.home / "decoy-codex-env")
        (self.home / ".codex-3").mkdir(parents=True)
        (self.home / ".codex-3" / "auth.json").write_text(
            json.dumps({"tokens": {"access_token": "tok-codex-3", "account_id": "acc-codex-3"}}),
            encoding="utf-8",
        )
        (self.home / ".usage-float").mkdir(parents=True, exist_ok=True)
        (self.home / ".usage-float" / "config.json").write_text(
            json.dumps({
                "language": "zh",
                "display_mode": "float",
                "providers": ["claude:7d", "claude:5h", "codex-3:7d"],
            }),
            encoding="utf-8",
        )

        host.bind_usage(self.native)
        self._poison_plugin_fetchers_and_writers()

        core_calls = []
        session_429 = {"active": True}

        def fake_core_http(url, *, method="GET", headers=None, body=None, timeout=20.0):
            core_calls.append((method, url, dict(headers or {}), body))
            if "at_wall=1" in url:
                if session_429["active"]:
                    return 429, {"error": {"message": "Too Many Requests"}}
                return 200, {
                    "juniper_tide": {
                        "eligible": True,
                        "arm": "reset",
                        "available": True,
                        "resets_per_week": 1,
                        "ineligible_reason": None,
                    }
                }
            if "/api/oauth/usage" in url:
                return 200, {
                    "five_hour": {"utilization": 20.0, "resets_at": self.reset_5h},
                    "seven_day": {"utilization": 40.0, "resets_at": self.reset_7d},
                    "cedar_ember": {
                        "eligible": True,
                        "resets_left_total": 1,
                        "next_grant_id": "grant-weekly-1",
                        "grants": [{
                            "id": "grant-weekly-1",
                            "resets_left": 1,
                            "ends_at": self.reset_7d,
                            "clears": ["seven_day"],
                            "usable_now": True,
                            "use_requires_limit": False,
                        }],
                    },
                }
            if url.endswith("/reset_rate_limits"):
                return 200, {"result": "reset", "cleared": ["seven_day"], "resets_left": 0}
            if url.endswith("/wham/rate-limit-reset-credits"):
                return 200, {
                    "credits": [{
                        "id": "card-codex-3",
                        "status": "available",
                        "reset_type": "wham_reset",
                        "expires_at": int(self.now.timestamp()) + 86400,
                        "granted_at": int(self.now.timestamp()) - 3600,
                    }]
                }
            if url.endswith("/wham/rate-limit-reset-credits/consume"):
                return 200, {"ok": True}
            if url.endswith("/wham/usage"):
                return 200, self._codex_payload(30.0)
            return 200, {}

        def find_radiobuttons(widget) -> list[tk.Radiobutton]:
            found = []
            for child in widget.winfo_children():
                if isinstance(child, tk.Radiobutton):
                    found.append(child)
                found.extend(find_radiobuttons(child))
            return found

        with mock.patch.object(self.usage_core, "http_json", side_effect=fake_core_http), \
                mock.patch.object(messagebox, "askokcancel", return_value=True):
            def exercise_popups(root, scope, drain):
                state = scope["state"]

                # 1. Claude weekly reset popup loads grants through refresh_usage and claims via usage_core.
                scope["open_claude_reset_info"]("claude")
                drain(root)
                win_7d = state["claude_reset_win"]
                self.assertEqual(len(find_radiobuttons(win_7d)), 1)
                btns_7d = self._find_buttons(win_7d)
                self.assertEqual(str(btns_7d["使用"].cget("state")), "normal")
                btns_7d["使用"].invoke()
                drain(root)
                self.assertTrue(
                    any(
                        m == "POST"
                        and u.endswith("/reset_rate_limits")
                        and isinstance(b, dict)
                        and b.get("program") == "cedar_ember"
                        for m, u, _, b in core_calls
                    )
                )
                btns_7d["关闭"].invoke()

                # 2. Claude 5h session reset popup: 429 triggers product backoff and blocks subsequent reloads.
                scope["open_claude_session_reset"]("claude")
                drain(root)
                self.assertTrue(self.usage_core.claude_in_backoff("claude"))
                win_5h = state["claude_session_win"]
                labels_429 = " ".join(self._collect_label_texts(win_5h))
                self.assertIn("请求太频繁", labels_429)
                calls_after_429 = len(core_calls)
                btns_5h = self._find_buttons(win_5h)
                btns_5h["刷新列表"].invoke()
                drain(root)
                # Zero upstream HTTP requests while Claude 429 backoff is active!
                self.assertEqual(len(core_calls), calls_after_429)

                # Clear backoff and reload -> loads juniper_tide and redeems via usage_core.
                session_429["active"] = False
                self.usage_core.clear_claude_backoff("claude")
                btns_5h["刷新列表"].invoke()
                drain(root)
                self.assertEqual(len(find_radiobuttons(win_5h)), 1)
                self.assertEqual(str(btns_5h["使用"].cget("state")), "normal")
                btns_5h["使用"].invoke()
                drain(root)
                self.assertTrue(
                    any(
                        m == "POST"
                        and u.endswith("/reset_rate_limits")
                        and isinstance(b, dict)
                        and b.get("program") == "juniper_tide"
                        for m, u, _, b in core_calls
                    )
                )
                btns_5h["关闭"].invoke()

                # 3. Codex reset cards popup resolves dynamic `.codex-3` without drifting to CODEX_HOME or default codex.
                scope["open_codex_reset_cards"]("codex-3")
                drain(root)
                win_cx = state["reset_cards_win"]
                self.assertEqual(len(find_radiobuttons(win_cx)), 1)
                btns_cx = self._find_buttons(win_cx)
                self.assertEqual(str(btns_cx["使用"].cget("state")), "normal")
                btns_cx["使用"].invoke()
                drain(root)
                codex_headers = [h for m, u, h, _ in core_calls if "/wham/rate-limit-reset-credits" in u]
                self.assertEqual(len(codex_headers), 3)
                for hdrs in codex_headers:
                    self.assertEqual(hdrs.get("Authorization"), "Bearer tok-codex-3")
                    self.assertEqual(hdrs.get("ChatGPT-Account-ID"), "acc-codex-3")
                btns_cx["关闭"].invoke()

                # Unknown Codex slot reports missing credentials rather than falling back to default `codex`.
                calls_before_missing = len(core_calls)
                scope["open_codex_reset_cards"]("codex-missing")
                drain(root)
                win_missing = state["reset_cards_win"]
                missing_labels = " ".join(self._collect_label_texts(win_missing))
                self.assertIn("未找到可用的 Codex 登录凭证", missing_labels)
                self.assertEqual(len(core_calls), calls_before_missing)
                self._find_buttons(win_missing)["关闭"].invoke()

            self._run_hosted_ui(exercise_popups)

    def test_forced_refresh_respects_claude_429_backoff_in_product_collector(self):
        (self.home / ".claude").mkdir(parents=True)
        (self.home / ".claude" / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "tok-claude-primary"}}),
            encoding="utf-8",
        )
        host.bind_usage(self.native)
        self._poison_plugin_fetchers_and_writers()
        self.usage_core.set_claude_backoff(900, provider_id="claude")

        with mock.patch.object(
            self.usage_core,
            "claude_authorized_json",
            side_effect=AssertionError("429 backoff must block HTTP even on forced refresh"),
        ):
            [item] = self.native.fetch_display_usage(["claude:7d"], [], force=True)

        self.assertTrue(item.stale)
        self.assertIn("限流", item.error or "")
