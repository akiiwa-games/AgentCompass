import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import plugins
import runtime_paths
import services



def setUpModule():
    # start_all / restart_services / update_all read plugins.json; never the real one.
    global _plugins_isolation
    directory = tempfile.TemporaryDirectory()
    _plugins_isolation = (directory, mock.patch.object(plugins, "PATH", Path(directory.name) / "plugins.json"))
    _plugins_isolation[1].start()


def tearDownModule():
    _plugins_isolation[1].stop()
    _plugins_isolation[0].cleanup()

class PublishTests(unittest.TestCase):
    def test_publish_rewrites_mcp_url_without_logging_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "connect.json"
            path.write_text(
                json.dumps({"dot_token": "secret-token", "public_base_url": "", "mcp_url": ""}),
                encoding="utf-8",
            )
            with mock.patch.object(services, "LOG_DIR", Path(directory)):
                changed = services.publish_tunnel("https://example.trycloudflare.com")
            saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(changed)
        self.assertEqual(saved["public_base_url"], "https://example.trycloudflare.com")
        self.assertEqual(saved["mcp_url"], "https://example.trycloudflare.com/mcp/secret-token")

    def test_same_url_is_not_a_change(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "connect.json"
            url = "https://example.trycloudflare.com"
            path.write_text(
                json.dumps({"dot_token": "secret-token", "public_base_url": url, "mcp_url": f"{url}/mcp/secret-token"}),
                encoding="utf-8",
            )
            with mock.patch.object(services, "LOG_DIR", Path(directory)):
                changed = services.publish_tunnel(url)
        self.assertFalse(changed)


class RenewalTests(unittest.TestCase):
    def test_renewal_waits_until_the_last_two_hours(self):
        self.assertIsNone(services.renewal_decision(23 * 3600, None, False, False))

    def test_renewal_submits_one_task_before_expiry(self):
        self.assertEqual(services.renewal_decision(3600, None, False, False), "submit")
        self.assertIsNone(services.renewal_decision(3600, "queued", False, False))
        self.assertIsNone(services.renewal_decision(3600, "running", False, False))

    def test_renewal_reports_a_miss_once(self):
        self.assertEqual(services.renewal_decision(None, None, False, False), "missing")
        self.assertEqual(services.renewal_decision(0, None, False, False), "missing")
        self.assertIsNone(services.renewal_decision(None, None, True, False))

    def test_finished_renewal_without_extension_reports_once(self):
        self.assertEqual(services.renewal_decision(3600, "completed", False, False), "stuck")
        self.assertIsNone(services.renewal_decision(3600, "completed", False, True))


def process(pid, name, command, parent=None):
    return SimpleNamespace(pid=pid, info={"name": name, "cmdline": command}, parent=lambda: parent, name=lambda: name,
                           cmdline=lambda: command or [], is_running=lambda: True)


class ListenerTests(unittest.TestCase):
    @staticmethod
    def connection(port, status="LISTEN", pid=7, ip="127.0.0.1"):
        return SimpleNamespace(status=status, pid=pid, laddr=SimpleNamespace(ip=ip, port=port))

    def test_the_listening_loopback_socket_on_the_port_is_found(self):
        connections = [self.connection(8788, status="ESTABLISHED", pid=1), self.connection(8789, pid=2),
                       self.connection(8788, pid=3, ip="192.0.2.5"), self.connection(8788, pid=4)]
        with mock.patch.object(services.psutil, "net_connections", return_value=connections):
            self.assertEqual(services.listener_pid(8788), 4)
            self.assertIsNone(services.listener_pid(9999))

    def test_a_refused_listing_is_no_listener(self):
        with mock.patch.object(services.psutil, "net_connections", side_effect=services.psutil.AccessDenied()):
            self.assertIsNone(services.listener_pid(8788))


class TunnelTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.multiple(services, _tunnel_processes=None, _tunnel_checked=0.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_only_cloudflared_tunnelling_the_dot_port_counts(self):
        listing = [process(1, "cloudflared.exe", ["cloudflared.exe", "tunnel", "--url", "http://127.0.0.1:8788"]),
                   process(2, "cloudflared.exe", ["cloudflared.exe", "tunnel", "--url", "http://127.0.0.1:9000"]),
                   process(3, "cloudflared.exe", ["cloudflared.exe", "tunnel", "--url", "http://127.0.0.1:87880"]),
                   process(4, "cloudflared.exe", None)]
        with mock.patch.object(services, "named_processes", return_value=listing):
            self.assertEqual(services.tunnel_pids(), [1])

    def test_live_cached_tunnel_needs_no_scan_or_command_line_read(self):
        tunnel = mock.Mock(pid=5)
        tunnel.cmdline.return_value = ["cloudflared.exe", "tunnel", "--url", "http://127.0.0.1:8788"]
        tunnel.is_running.return_value = True
        with mock.patch.object(services, "named_processes", return_value=[tunnel]) as scan:
            self.assertEqual(services.tunnel_pids(), [5])
            self.assertEqual(services.tunnel_pids(), [5])
        scan.assert_called_once_with("cloudflared.exe")
        tunnel.cmdline.assert_called_once()

    def test_exited_or_reused_pid_is_replaced_immediately(self):
        old = mock.Mock(pid=5)
        old.is_running.return_value = False
        services._tunnel_processes = [old]
        new = process(6, "cloudflared.exe", ["cloudflared.exe", "tunnel", "--url", "http://127.0.0.1:8788"])
        with mock.patch.object(services, "named_processes", return_value=[new]):
            self.assertEqual(services.tunnel_pids(), [6])

    def test_missing_tunnel_is_rechecked_after_retry_interval(self):
        with mock.patch.object(services, "named_processes", return_value=[]) as scan, \
                mock.patch.object(services.time, "monotonic", return_value=100):
            self.assertEqual(services.tunnel_pids(), [])
            self.assertEqual(services.tunnel_pids(), [])
            scan.assert_called_once()
            with mock.patch.object(services.time, "monotonic", return_value=111):
                self.assertEqual(services.tunnel_pids(), [])
        self.assertEqual(scan.call_count, 2)

    def test_refresh_and_reconnect_share_one_discovery(self):
        entered, release = threading.Event(), threading.Event()
        tunnel = process(7, "cloudflared.exe", ["cloudflared.exe", "tunnel", "--url", "http://127.0.0.1:8788"])
        def discover(name):
            entered.set()
            if not release.wait(2):
                raise TimeoutError("test discovery was not released")
            return [tunnel]
        results = []
        workers = [threading.Thread(target=lambda: results.append(services.tunnel_pids())) for _ in range(2)]
        with mock.patch.object(services, "named_processes", side_effect=discover) as scan:
            try:
                workers[0].start()
                self.assertTrue(entered.wait(2))
                workers[1].start()
            finally:
                release.set()
                for worker in workers:
                    if worker.ident is not None:
                        worker.join(3)
            self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(results, [[7], [7]])
        scan.assert_called_once()

    def test_new_tunnel_is_cached_even_when_negative_cache_is_fresh(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(services, "LOG_DIR", Path(directory)), \
                mock.patch.object(services, "dot_ready", return_value=True), \
                mock.patch.object(services, "named_processes", return_value=[]) as scan, \
                mock.patch.object(services, "cloudflared_path", return_value=Path("cloudflared.exe")), \
                mock.patch.object(services, "hidden_popen", return_value=mock.Mock(pid=8)) as popen, \
                mock.patch.object(services.psutil, "Process", return_value=process(8, "cloudflared.exe", [])):
            self.assertIn("已启动隧道", services.start_dot2api())
            self.assertIn("隧道已在运行", services.start_dot2api())
        scan.assert_called_once()
        popen.assert_called_once()


class NamedProcessTests(unittest.TestCase):
    def test_only_filtered_pids_are_inspected_and_races_are_skipped(self):
        output = '\x22cloudflared.exe\x22,\x2212\x22,\x22Console\x22\n\x22cloudflared.exe\x22,\x2213\x22\n\x22other.exe\x22,\x2214\x22\n'
        candidate = process(12, "cloudflared.exe", [])
        with mock.patch.object(services.subprocess, "run", return_value=SimpleNamespace(stdout=output)) as run, \
                mock.patch.object(services.psutil, "Process", side_effect=[candidate, services.psutil.NoSuchProcess(13)]) as lookup:
            self.assertEqual(services.named_processes("cloudflared.exe"), [candidate])
        self.assertEqual(lookup.call_args_list, [mock.call(12), mock.call(13)])
        self.assertIn("IMAGENAME eq cloudflared.exe", run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs["timeout"], 10)

    def test_empty_listing_does_not_inspect_processes(self):
        with mock.patch.object(services.subprocess, "run", return_value=SimpleNamespace(stdout="INFO: no matching tasks\n")), \
                mock.patch.object(services.psutil, "Process") as lookup:
            self.assertEqual(services.named_processes("cloudflared.exe"), [])
        lookup.assert_not_called()

    def test_reused_pid_with_different_name_is_ignored(self):
        with mock.patch.object(services.subprocess, "run", return_value=SimpleNamespace(stdout='\x22cloudflared.exe\x22,\x2212\x22\n')), \
                mock.patch.object(services.psutil, "Process", return_value=process(12, "other.exe", [])):
            self.assertEqual(services.named_processes("cloudflared.exe"), [])

class ChatGPTWebTests(unittest.TestCase):
    def listing(self):
        exe = str(services.chatgpt_exe())
        main = process(50, "electron.exe", [exe, str(services.CHATGPT_LAUNCHER), "--hidden"])
        return main, [process(51, "electron.exe", [exe, "--type=renderer"]),
                      process(52, "electron.exe", [exe, "x/browser-helper.cjs"]),
                      process(53, "electron.exe", ["C:\\Other\\electron.exe", "app"]),
                      main]

    def test_only_the_launchers_own_electron_binary_counts(self):
        exe = str(services.chatgpt_exe())
        self.assertTrue(services.launcher_process("electron.exe", [exe, "app"]))
        self.assertTrue(services.launcher_process("Electron.exe", [exe.upper()]))
        self.assertFalse(services.launcher_process("electron.exe", ["C:\\Other\\electron.exe"]))
        self.assertFalse(services.launcher_process("python.exe", [exe]))
        self.assertFalse(services.launcher_process("electron.exe", None))

    def test_the_main_process_is_the_launchers_parent_of_the_bridge_runtime(self):
        main, _ = self.listing()
        runtime = process(60, "bun.exe", ["bun.exe"], parent=main)
        with mock.patch.object(services, "bridge_port", return_value=17843), \
                mock.patch.object(services, "listener_pid", return_value=60), \
                mock.patch.object(services.psutil, "Process", return_value=runtime):
            self.assertEqual(services.chatgpt_main_pid(), 50)

    def test_a_bridge_runtime_of_another_parent_falls_back_to_the_process_list(self):
        _, listing = self.listing()
        stranger = process(70, "electron.exe", ["C:\\Other\\electron.exe", "app"])
        runtime = process(60, "bun.exe", ["bun.exe"], parent=stranger)
        with mock.patch.object(services, "bridge_port", return_value=17843), \
                mock.patch.object(services, "listener_pid", return_value=60), \
                mock.patch.object(services.psutil, "Process", return_value=runtime), \
                mock.patch.object(services, "named_processes", return_value=listing):
            self.assertEqual(services.chatgpt_main_pid(), 50)

    def test_without_a_listener_the_main_process_is_the_one_that_is_no_helper(self):
        _, listing = self.listing()
        with mock.patch.object(services, "bridge_port", side_effect=RuntimeError), \
                mock.patch.object(services, "named_processes", return_value=listing):
            self.assertEqual(services.chatgpt_main_pid(), 50)
        with mock.patch.object(services, "bridge_port", side_effect=RuntimeError), \
                mock.patch.object(services, "named_processes", return_value=listing[:3]):
            self.assertIsNone(services.chatgpt_main_pid())

    def test_the_launcher_is_started_from_its_source_for_the_tray_and_never_twice(self):
        with mock.patch.object(services, "hidden_popen") as popen, mock.patch.object(services, "chatgpt_ready", return_value=False), \
                mock.patch.object(services, "chatgpt_built", return_value=True), \
                mock.patch.object(services, "bun_exe", return_value=Path("Z:/tools/bun.exe")):
            with mock.patch.object(services, "chatgpt_main_pid", return_value=None):
                self.assertEqual(services.start_chatgpt_web(False), "已启动 ChatGPT Web")
            args, env = popen.call_args.args[0], popen.call_args.args[1]
            self.assertEqual(args, [str(services.chatgpt_exe()), str(services.CHATGPT_LAUNCHER), "--hidden"])
            self.assertEqual(popen.call_args.args[2], services.CHATGPT_LAUNCHER)
            self.assertEqual(env["CODEX_WEB_GPT_EXTERNAL_TRAY"], "1")
            self.assertEqual(env["CODEX_CHATGPT_WEB_BUN"], str(Path("Z:/tools/bun.exe")))
            self.assertEqual(env["CODEX_WEB_GPT_BUN"], str(Path("Z:/tools/bun.exe")))
            self.assertTrue(env["CODEX_CHATGPT_WEB_HOME"].endswith(".codex-chatgpt-web-consult"))
            popen.reset_mock()
            with mock.patch.object(services, "chatgpt_main_pid", return_value=50):
                self.assertEqual(services.start_chatgpt_web(False), "ChatGPT Web 正在启动")
                popen.assert_not_called()
                services.start_chatgpt_web(True)
                self.assertEqual(popen.call_args.args[0][2:], [])

    def test_an_unbuilt_launcher_is_not_started(self):
        with mock.patch.object(services, "hidden_popen") as popen, mock.patch.object(services, "chatgpt_built", return_value=False), \
                mock.patch.object(services, "chatgpt_main_pid", return_value=None):
            self.assertIn("deploy.py install", services.start_chatgpt_web(False))
        popen.assert_not_called()

    def test_the_launcher_counts_as_built_only_with_everything_it_needs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = root / "launcher"
            files = [launcher / "node_modules" / "electron" / "dist" / "electron.exe", root / "bun.exe",
                     launcher / "dist" / "index.html", root / ".launcher-runtime" / "browser-helper.cjs"]
            with mock.patch.object(services, "CHATGPT_ROOT", root), mock.patch.object(services, "CHATGPT_LAUNCHER", launcher), \
                    mock.patch.object(services, "bun_exe", return_value=root / "bun.exe"):
                self.assertFalse(services.chatgpt_built())
                for file in files:
                    file.parent.mkdir(parents=True, exist_ok=True)
                    file.write_bytes(b"x")
                self.assertTrue(services.chatgpt_built())
                files[2].unlink()
                self.assertFalse(services.chatgpt_built())
                files[2].write_bytes(b"x")
                with mock.patch.object(services, "bun_exe", return_value=None):
                    self.assertFalse(services.chatgpt_built())

    def test_a_bun_found_through_the_runtime_paths_counts_without_a_workspace_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            bun = Path(directory) / "bun.exe"
            bun.write_bytes(b"x")
            with mock.patch.object(runtime_paths, "bun_path", return_value=bun):
                self.assertEqual(services.bun_exe(), bun)
        with mock.patch.object(runtime_paths, "bun_path", side_effect=runtime_paths.ToolMissing("bun", "x", "y", "Z")):
            self.assertIsNone(services.bun_exe())

    def test_the_launcher_data_folder_follows_localappdata_and_the_users_choice(self):
        with mock.patch.dict(services.os.environ, {"LOCALAPPDATA": "D:/Redirected/Local"}):
            services.os.environ.pop("CODEX_WEB_GPT_LAUNCHER_DATA_DIR", None)
            self.assertEqual(services.launcher_data_dir(), Path("D:/Redirected/Local") / "Codex Pro Consult")
            services.os.environ["CODEX_WEB_GPT_LAUNCHER_DATA_DIR"] = "D:/My Launcher Data"
            self.assertEqual(services.launcher_data_dir(), Path("D:/My Launcher Data"))
            with mock.patch.object(services, "bridge_home", return_value=Path("D:/bridge")), \
                    mock.patch.object(services, "bun_exe", return_value=None):
                env = services.chatgpt_env()
            self.assertEqual(env["CODEX_WEB_GPT_LAUNCHER_DATA_DIR"], str(Path("D:/My Launcher Data")))

    def test_the_bridge_profile_can_be_chosen(self):
        with mock.patch.dict(services.os.environ, {"ROUTE_MANAGER_CHATGPT_CODEX_HOME": "D:/profiles/pro"}), \
                mock.patch.object(services, "bridge_home", return_value=Path("D:/bridge")), \
                mock.patch.object(services, "bun_exe", return_value=None):
            self.assertEqual(services.chatgpt_env()["CODEX_HOME"], "D:/profiles/pro")

    def test_stop_refuses_while_a_request_is_in_flight(self):
        with mock.patch.object(services, "chatgpt_main_pid", return_value=50), \
                mock.patch.object(services, "chatgpt_busy", return_value=True), \
                mock.patch.object(services, "hidden_popen") as popen:
            self.assertIn("正在处理请求", services.stop_chatgpt_web())
            popen.assert_not_called()
        with mock.patch.object(services, "chatgpt_main_pid", return_value=None):
            self.assertEqual(services.stop_chatgpt_web(), "ChatGPT Web 未在运行")

    def test_stop_asks_the_launcher_to_quit_and_only_forces_what_stays(self):
        with mock.patch.object(services, "chatgpt_main_pid", return_value=50), \
                mock.patch.object(services, "chatgpt_busy", return_value=False), \
                mock.patch.object(services, "hidden_popen") as popen, mock.patch.object(services, "kill_pid") as kill, \
                mock.patch.object(services, "listener_pid", return_value=None), \
                mock.patch.object(services, "bridge_port", return_value=1), \
                mock.patch.object(services.time, "sleep"):
            with mock.patch.object(services.psutil, "pid_exists", side_effect=[True, False, False]):
                self.assertEqual(services.stop_chatgpt_web(), "已停止 ChatGPT Web")
            self.assertEqual(popen.call_args.args[0][2:], ["--quit"])
            kill.assert_not_called()
            with mock.patch.object(services.psutil, "pid_exists", return_value=True):
                self.assertIn("强制", services.stop_chatgpt_web(wait=0))
            kill.assert_called_once_with(50)

    def test_labels_tell_running_starting_and_stopped_apart(self):
        with mock.patch.object(services, "chatgpt_ready", return_value=True):
            self.assertEqual(services.chatgpt_label(), "运行中")
        with mock.patch.object(services, "chatgpt_ready", return_value=False), \
                mock.patch.object(services, "chatgpt_main_pid", return_value=50):
            self.assertEqual(services.chatgpt_label(), "启动中")
        with mock.patch.object(services, "chatgpt_ready", return_value=False), \
                mock.patch.object(services, "chatgpt_main_pid", return_value=None):
            self.assertEqual(services.chatgpt_label(), "已停止")


class CompositeActionTests(unittest.TestCase):
    def test_restart_keeps_the_tunnel_and_restarts_in_order(self):
        calls = []
        with mock.patch.object(services, "stop_dot2api", side_effect=lambda tunnel=True: calls.append(("stop_dot", tunnel)) or "a"), \
                mock.patch.object(services, "stop_chatgpt_web", side_effect=lambda: calls.append(("stop_web",)) or "b"), \
                mock.patch.object(services, "start_dot2api", side_effect=lambda: calls.append(("start_dot",)) or "c"), \
                mock.patch.object(services, "start_chatgpt_web", side_effect=lambda show=False: calls.append(("start_web", show)) or "d"), \
                mock.patch.object(services.time, "sleep"):
            self.assertEqual(services.restart_services(), "a；b；c；d")
        self.assertEqual(calls, [("stop_dot", False), ("stop_web",), ("start_dot",), ("start_web", False)])


class GeminiWebTests(unittest.TestCase):
    """The tray only wraps gemini-web/gemini_web.py; it never starts Chrome on its own."""

    def module(self, healthy=None, pid=None):
        fake = mock.Mock()
        fake.CHROME = mock.Mock(is_file=lambda: True)
        fake.health.return_value = healthy
        fake.main_pid.return_value = pid
        fake.start.side_effect = lambda show=False: "shown" if show else "started"
        fake.stop.return_value = "stopped"
        return mock.patch.multiple(services, _gemini_module=fake,
                                   gemini_built=mock.Mock(return_value=True))

    def test_labels_tell_running_starting_and_stopped_apart(self):
        with self.module(healthy={"pid": 1}):
            self.assertEqual(services.gemini_label(), "运行中")
        with self.module(pid=7):
            self.assertEqual(services.gemini_label(), "启动中")
        with self.module():
            self.assertEqual(services.gemini_label(), "已停止")

    def test_the_tray_line_shows_the_window_and_stop_delegates(self):
        with self.module():
            self.assertEqual(services.start_gemini_web(True), "shown")
            self.assertEqual(services.stop_gemini_web(), "stopped")

    def test_it_does_not_start_with_the_other_services(self):
        with mock.patch.object(services, "start_dot2api", return_value="a"), \
                mock.patch.object(services, "start_chatgpt_web", return_value="b"), \
                mock.patch.object(services, "USAGE_FLOAT_SCRIPT", Path("missing-usage-float.py")), \
                mock.patch.object(services.easytier, "start_if_stopped", return_value=None), \
                mock.patch.object(services, "start_gemini_web") as gemini:
            services.start_all()
        gemini.assert_not_called()


class EasyTierLoginTests(unittest.TestCase):
    def start_all(self, easytier_result):
        with mock.patch.object(services, "start_dot2api", return_value="a"), \
                mock.patch.object(services, "start_chatgpt_web", return_value="b"), \
                mock.patch.object(services, "USAGE_FLOAT_SCRIPT", Path("missing-usage-float.py")), \
                mock.patch.object(services.easytier, "start_if_stopped", return_value=easytier_result) as start:
            return services.start_all(), start

    def test_login_start_includes_a_stopped_easytier(self):
        message, start = self.start_all("已启动 EasyTier")
        self.assertIn("已启动 EasyTier", message)
        start.assert_called_once_with()

    def test_restart_services_leaves_easytier_alone(self):
        with mock.patch.object(services, "stop_dot2api", return_value="a"), mock.patch.object(services, "stop_chatgpt_web", return_value="b"), \
                mock.patch.object(services, "start_dot2api", return_value="c"), mock.patch.object(services, "start_chatgpt_web", return_value="d"), \
                mock.patch.object(services.time, "sleep"), \
                mock.patch.object(services.easytier, "stop") as stop, mock.patch.object(services.easytier, "restart") as restart:
            services.restart_services()
        stop.assert_not_called()
        restart.assert_not_called()



class CapacityTickTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(services._capacity.update, proc=None, started=None)
        services._capacity.update(proc=None, started=None)

    def child(self, running):
        proc = mock.Mock(pid=4242)
        proc.poll.return_value = None if running else 0
        return proc

    def test_first_tick_starts_a_low_priority_pass(self):
        with mock.patch.object(services, "hidden_popen", return_value=self.child(True)) as popen:
            self.assertTrue(services.capacity_tick())
        args, _env, _cwd, log, flags = popen.call_args.args
        self.assertEqual(args[1:], [str(services.CAPACITY_SCRIPT), "--once", "--quiet"])
        self.assertEqual(log.name, "capacity.log")
        self.assertEqual(flags, services.BELOW_NORMAL_PRIORITY_CLASS)

    def test_the_frozen_tray_runs_the_statistics_pass_through_its_own_executable(self):
        with mock.patch.object(services.component_launch, "frozen", return_value=True), \
                mock.patch.object(services.sys, "executable", "C:/Apps/AgentCompass.exe"), \
                mock.patch.object(services, "hidden_popen", return_value=self.child(True)) as popen:
            self.assertTrue(services.capacity_tick())
        self.assertEqual(popen.call_args.args[0],
                         ["C:/Apps/AgentCompass.exe", "--run-component", "capacity_stats", "--once", "--quiet"])

    def test_a_running_pass_is_not_doubled(self):
        with mock.patch.object(services, "hidden_popen", return_value=self.child(True)) as popen:
            services.capacity_tick()
            self.assertFalse(services.capacity_tick(force=True))
        self.assertEqual(popen.call_count, 1)

    def test_next_pass_waits_for_the_interval_unless_forced(self):
        with mock.patch.object(services, "hidden_popen", return_value=self.child(False)) as popen:
            self.assertTrue(services.capacity_tick())
            self.assertFalse(services.capacity_tick())
            self.assertTrue(services.capacity_tick(force=True))
            services._capacity["started"] -= services.CAPACITY_INTERVAL + 1
            self.assertTrue(services.capacity_tick())
        self.assertEqual(popen.call_count, 3)

    def test_stop_kills_only_a_running_pass(self):
        with mock.patch.object(services, "kill_pid") as kill:
            services.stop_capacity()
            services._capacity["proc"] = self.child(False)
            services.stop_capacity()
            kill.assert_not_called()
            services._capacity["proc"] = self.child(True)
            services.stop_capacity()
            kill.assert_called_once_with(4242)


class ToolMissingDegradesTests(unittest.TestCase):
    def test_missing_uv_does_not_start_dot2api(self):
        missing = runtime_paths.ToolMissing("uv", "Dot2API", "Install uv.", "ROUTE_MANAGER_UV")
        with mock.patch.object(services, "dot_ready", return_value=False), \
                mock.patch.object(services, "uv_path", side_effect=missing), \
                mock.patch.object(services, "hidden_popen") as popen:
            message = services.start_dot2api()
        self.assertIn("uv", message)
        self.assertIn("Install uv", message)
        popen.assert_not_called()

    def test_missing_cloudflared_still_starts_the_local_server(self):
        missing = runtime_paths.ToolMissing(
            "cloudflared", "the Dot2API public quick tunnel", "Install cloudflared.", "ROUTE_MANAGER_CLOUDFLARED"
        )
        with mock.patch.object(services, "dot_ready", return_value=False), \
                mock.patch.object(services, "tunnel_pids", return_value=[]), \
                mock.patch.object(services, "uv_path", return_value=Path("uv.exe")), \
                mock.patch.object(services, "cloudflared_path", side_effect=missing), \
                mock.patch.object(services, "hidden_popen") as popen:
            message = services.start_dot2api()
        self.assertIn("已启动 Dot2API", message)
        self.assertIn("cloudflared", message)
        self.assertEqual(popen.call_count, 1)

    def test_unbuilt_launcher_with_bun_keeps_the_install_sentence(self):
        with tempfile.TemporaryDirectory() as directory:
            bun = Path(directory) / "bun.exe"
            bun.write_bytes(b"x")
            with mock.patch.object(services, "chatgpt_built", return_value=False), \
                    mock.patch.object(services, "chatgpt_main_pid", return_value=None), \
                    mock.patch.object(services, "bun_exe", return_value=bun), \
                    mock.patch.object(services, "hidden_popen") as popen:
                message = services.start_chatgpt_web(False)
        self.assertIn("deploy.py install", message)
        self.assertNotIn("is not installed", message)
        popen.assert_not_called()

    def test_unbuilt_launcher_without_bun_names_the_tool(self):
        missing = runtime_paths.ToolMissing(
            "bun", "the ChatGPT Web bridge", "Install Bun.", "ROUTE_MANAGER_BUN"
        )
        with mock.patch.object(services, "chatgpt_built", return_value=False), \
                mock.patch.object(services, "chatgpt_main_pid", return_value=None), \
                mock.patch.object(services, "bun_exe", return_value=None), \
                mock.patch.object(runtime_paths, "bun_path", side_effect=missing), \
                mock.patch.object(services, "hidden_popen") as popen:
            message = services.start_chatgpt_web(False)
        self.assertIn("deploy.py install", message)
        self.assertIn("bun", message)
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
