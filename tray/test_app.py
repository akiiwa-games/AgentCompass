import importlib.machinery
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

LOADER = importlib.machinery.SourceFileLoader("route_manager_tray_app", str(Path(__file__).with_name("app.pyw")))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
app = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(app)

ALL_RUNNING = dict(dot="运行中", web="运行中", gemini="已停止", float="运行中",
                   easytier="运行中（2 个对端）", grok=app.DISABLED, muse=app.DISABLED)


def setUpModule():
    # Never read or write the real ~/.route-manager/plugins.json.
    global _isolation
    directory = tempfile.TemporaryDirectory()
    _isolation = [directory, mock.patch.object(app.plugins, "PATH", Path(directory.name) / "plugins.json")]
    _isolation[1].start()


def tearDownModule():
    _isolation[1].stop()
    _isolation[0].cleanup()


class TrayTestCase(unittest.TestCase):
    def setUp(self):
        saved = dict(app.STATE)
        self.addCleanup(lambda: (app.STATE.clear(), app.STATE.update(saved)))
        app.STATE.update(ALL_RUNNING)
        for name, value in (("PRIORITY", {"label": "重置前优先", "entries": [], "targets": []}),
                            ("PLUGINS", app.plugins.defaults())):
            patcher = mock.patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def items(self):
        return [item for item in app.build_menu().items if item.visible]

    def shown(self):
        return [item.text for item in self.items() if item.text != app.Menu.SEPARATOR.text]

    def by_text(self):
        return {item.text: item for item in self.items()}

    def click(self, item):
        with mock.patch.object(app, "act") as act:
            item(mock.Mock())
        if act.called:
            act.call_args[0][1]()
        return act


class MenuTests(TrayTestCase):
    def test_panel_first_then_priority_then_enabled_window_plugins_then_quit(self):
        self.assertEqual(self.shown(), [
            "打开 AgentCompass", "重置前优先", "Usage Float：运行中",
            "ChatGPT Web：运行中", "Gemini Web：已停止", "EasyTier：运行中（2 个对端）", "退出"])
        items = self.items()
        self.assertTrue(items[0].default)
        separators = [index for index, item in enumerate(items) if item.text == app.Menu.SEPARATOR.text]
        self.assertEqual(separators, [2, 7])

    def test_dot2api_has_no_window_so_it_is_never_in_the_first_level(self):
        self.assertFalse(any("Dot" in text for text in self.shown()))
        app.PLUGINS["dot2api"]["enabled"] = True
        self.assertFalse(any("Dot" in text for text in self.shown()))

    def test_a_disabled_plugin_leaves_the_menu(self):
        app.PLUGINS["chatgpt-web"]["enabled"] = False
        app.PLUGINS["easytier"]["enabled"] = False
        shown = self.shown()
        self.assertNotIn("ChatGPT Web：运行中", shown)
        self.assertFalse(any(text.startswith("EasyTier") for text in shown))
        self.assertIn("Usage Float：运行中", shown)

    def test_with_no_window_plugin_enabled_only_the_core_items_remain(self):
        for row in app.PLUGINS.values():
            row["enabled"] = False
        self.assertEqual(self.shown(), ["打开 AgentCompass", "重置前优先", "退出"])
        self.assertEqual(sum(item.text == app.Menu.SEPARATOR.text for item in self.items()), 1)

    def test_menu_is_two_levels_at_most_and_plugins_have_no_submenu(self):
        for item in self.items()[2:]:
            self.assertIsNone(item.submenu)

    def test_opening_the_menu_reads_no_file_or_service(self):
        guard = mock.Mock(side_effect=AssertionError("menu must use cached snapshots"))
        with mock.patch.object(app.plugins, "load", guard), mock.patch.object(app.router_priority, "snapshot", guard), \
                mock.patch.multiple(app.services, dot_label=guard, chatgpt_label=guard, gemini_label=guard,
                                    usage_float_label=guard), \
                mock.patch.object(app.easytier, "snapshot", guard):
            self.shown()

    def test_the_first_item_opens_the_panel(self):
        with mock.patch.object(app.router_window, "open_window") as window:
            self.items()[0](mock.Mock())
        window.assert_called_once_with()

    def test_each_plugin_line_opens_its_window(self):
        cases = (("Usage Float：运行中", "component_action", ("usage-float", "show")),
                 ("ChatGPT Web：运行中", "start_chatgpt_web", (True,)),
                 ("Gemini Web：已停止", "start_gemini_web", (True,)))
        for text, name, arguments in cases:
            with self.subTest(text), mock.patch.object(app.services, name, return_value="ok") as target:
                act = self.click(self.by_text()[text])
                act.assert_called_once()
            target.assert_called_once_with(*arguments)

    def test_easytier_line_opens_the_network_window_without_probing(self):
        with mock.patch.object(app.network_status, "open_window") as window, \
                mock.patch.object(app.easytier, "snapshot") as probe, mock.patch.object(app.easytier, "peers") as cli:
            self.by_text()["EasyTier：运行中（2 个对端）"](mock.Mock())
        window.assert_called_once_with()
        probe.assert_not_called()
        cli.assert_not_called()

    def test_priority_menu_is_two_levels_direct_models_without_grey_help(self):
        app.PRIORITY = {'label': '优先中：Astra', 'entries': [
            {'label': 'Astra', 'id': 'rule-a', 'revision': 'revision-a'}],
            'targets': [
                {'account': a, 'model': m, 'window': 'usage', 'label': label, 'selected': label == 'Astra'}
                for a, m, label in app.router_priority.TOP_MODELS]}
        with mock.patch.object(app.router_priority, 'snapshot', side_effect=AssertionError('UI must not read files')):
            menu = app.build_menu().items[1].submenu
            items = [i for i in menu.items if i.text != app.Menu.SEPARATOR.text]
            self.assertEqual([i.text for i in items], ['Astra', 'Argon', 'Opus 5.5', '取消优先'])
            self.assertTrue(all(i.submenu is None and i.enabled for i in items))
            self.assertTrue(items[0].checked)
            with mock.patch.object(app, 'act') as act, mock.patch.object(app.router_priority, 'perform') as perform:
                items[-1](mock.Mock()); act.call_args.args[1]()
                self.assertEqual(perform.call_args.args[0], {'kind': 'cancel', 'id': 'rule-a', 'revision': 'revision-a'})
                for item, target in zip(items, app.PRIORITY['targets']):
                    item(mock.Mock()); act.call_args.args[1]()
                    self.assertEqual(perform.call_args.args[0], {'kind': 'enable',
                        **{k: target[k] for k in ('account', 'model', 'window')}})

    def test_priority_refresh_sees_router_removal_without_changing_service_state(self):
        icon = mock.Mock(); services_before = dict(app.STATE)
        with mock.patch.object(app.router_priority, 'snapshot', return_value={
                'label': '优先中：Astra', 'entries': [], 'targets': []}):
            app.refresh_priority(icon)
        self.assertEqual(app.PRIORITY['label'], '优先中：Astra')
        with mock.patch.object(app.router_priority, 'snapshot', return_value={
                'label': '重置前优先', 'entries': [], 'targets': []}):
            app.refresh_priority(icon)
        self.assertEqual(app.PRIORITY['label'], '重置前优先')
        self.assertEqual(app.STATE, services_before)

    def test_switching_a_plugin_off_in_the_panel_rebuilds_the_menu(self):
        icon = mock.Mock()
        app.plugins.set_flag("chatgpt-web", "enabled", False)
        self.addCleanup(app.plugins.PATH.unlink)
        app.refresh_plugins(icon)
        self.assertNotIn("ChatGPT Web：运行中", [i.text for i in icon.menu.items])
        menu = icon.menu
        app.refresh_plugins(icon)
        self.assertIs(icon.menu, menu)  # unchanged switches do not rebuild

    def test_ui_only_reload_does_not_start_services(self):
        for attach in (False, True):
            icon = mock.Mock()
            icon.run.side_effect = lambda setup: setup(icon)
            with mock.patch.object(app, 'single_instance', return_value=True), \
                 mock.patch.object(app.pystray, 'Icon', return_value=icon), \
                 mock.patch.object(app.threading, 'Thread'), \
                 mock.patch.object(app.sys, 'argv', ['app.pyw'] + (['--attach-only'] if attach else [])), \
                 mock.patch.object(app, 'log_info'), \
                 mock.patch.object(app, 'act') as act:
                app.main()
                if attach:
                    act.assert_not_called()
                else:
                    act.assert_called_once_with(icon, app.services.start_all)


class IconTests(TrayTestCase):
    STATUS = (14, 52)

    def test_the_status_dot_is_green_amber_or_red(self):
        self.assertEqual(app.draw_icon().size, (64, 64))
        self.assertEqual(app.draw_icon().getpixel(self.STATUS)[:3], app.STATUS_COLOURS["ok"])
        app.PRIORITY = {"label": "优先中：Astra", "entries": [{"id": "a"}], "targets": []}
        self.assertEqual(app.draw_icon().getpixel(self.STATUS)[:3], app.STATUS_COLOURS["attention"])
        app.STATE["float"] = "已停止"
        self.assertEqual(app.draw_icon().getpixel(self.STATUS)[:3], app.STATUS_COLOURS["error"])

    def test_a_stopped_always_on_plugin_is_an_error_but_on_demand_or_disabled_ones_are_not(self):
        app.STATE["gemini"] = "已停止"
        self.assertEqual(app.health(), "ok")
        app.STATE["dot"] = "已停止"
        self.assertEqual(app.health(), "error")
        app.PLUGINS["dot2api"]["enabled"] = False
        self.assertEqual(app.health(), "ok")

    def test_title_lists_only_enabled_plugins_within_the_windows_limit(self):
        app.PLUGINS["gemini-web"]["enabled"] = False
        title = app.title()
        self.assertTrue(title.startswith("AgentCompass · 正常"))
        self.assertNotIn("Gemini", title)
        self.assertIn("Dot 运行中", title)
        app.STATE.update({key: "x" * 40 for key in ALL_RUNNING if key != "monitor"})
        self.assertLessEqual(len(app.title()), app.TITLE_LIMIT)


class RefreshTests(TrayTestCase):
    def setUp(self):
        super().setUp()
        probe = mock.patch.object(app.easytier, "snapshot", return_value={"state": "running", "pids": [1], "peers": None})
        probe.start()
        self.addCleanup(probe.stop)
        label = mock.patch.object(app.easytier, "label", return_value="运行中（2 个对端）")
        label.start()
        self.addCleanup(label.stop)

    def labels(self, **overrides):
        values = {"dot_label": "运行中", "chatgpt_label": "运行中", "gemini_label": "已停止",
                  "usage_float_label": "运行中"}
        values.update(overrides)
        return mock.patch.multiple(app.services, **{name: (lambda v=value: v) for name, value in values.items()})

    def test_nothing_is_redrawn_while_nothing_changes(self):
        icon = mock.Mock()
        with self.labels():
            app.refresh(icon)
        icon.update_menu.assert_not_called()

    def test_a_change_redraws_the_icon_retitles_it_and_rebuilds_the_menu(self):
        icon = mock.Mock()
        with self.labels(chatgpt_label="已停止"):
            app.refresh(icon)
        self.assertEqual(app.STATE["web"], "已停止")
        self.assertIn("Web 已停止", icon.title)
        icon.update_menu.assert_called_once()

    def test_a_disabled_plugin_is_not_probed(self):
        app.PLUGINS["dot2api"]["enabled"] = False
        app.PLUGINS["easytier"]["enabled"] = False
        with self.labels(), mock.patch.object(app.services, "dot_label", side_effect=AssertionError("probed")), \
                mock.patch.object(app.easytier, "snapshot", side_effect=AssertionError("probed")):
            app.refresh(mock.Mock())
        self.assertEqual(app.STATE["dot"], app.DISABLED)
        self.assertEqual(app.STATE["easytier"], app.DISABLED)

    def test_a_failing_easytier_probe_keeps_the_last_reading(self):
        icon = mock.Mock()
        with self.labels(), mock.patch.object(app.easytier, "snapshot", side_effect=OSError("boom")), \
                mock.patch.object(app, "log_error"):
            app.refresh(icon)
        self.assertEqual(app.STATE["easytier"], "运行中（2 个对端）")

    def test_long_notifications_are_truncated_to_fit_win32_szinfo_limit(self):
        icon = mock.Mock()
        app.notify(icon, "x" * 400)
        notified = icon.notify.call_args.args[0]
        self.assertLessEqual(len(notified), app.MAX_NOTIFY_LENGTH)
        self.assertTrue(notified.endswith("…"))


class Stop(BaseException):
    pass


class WatchTests(TrayTestCase):
    def run_watch_once(self):
        event = mock.Mock()
        event.return_value.wait.side_effect = Stop
        with mock.patch.object(app.threading, "Event", event), mock.patch.object(app.services, "capacity_tick"), \
                mock.patch.object(app, "refresh"), mock.patch.object(app.services, "maintain_subscription",
                                                                     return_value=None) as renew, \
                mock.patch.object(app.threading, "Thread") as thread, mock.patch.object(app.time, "monotonic",
                                                                                       return_value=1000.0):
            with self.assertRaises(Stop):
                app.watch(mock.Mock())
        return renew, thread

    def test_renewal_and_reconnect_run_only_while_dot2api_is_enabled(self):
        renew, thread = self.run_watch_once()
        renew.assert_called_once_with()
        self.assertIs(thread.call_args.kwargs["target"], app.reconnect_dot)
        app.PLUGINS["dot2api"]["enabled"] = False
        renew, thread = self.run_watch_once()
        renew.assert_not_called()
        thread.assert_not_called()


if __name__ == "__main__":
    unittest.main()
