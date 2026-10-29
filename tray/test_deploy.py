import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tk_test_support import require_windows

require_windows("win32gui", "win32process")
import deploy


def proc(pid, name, command):
    return SimpleNamespace(pid=pid, info={"name": name, "cmdline": command})


class TrayProcessTests(unittest.TestCase):
    def test_only_the_tray_script_run_by_python_counts(self):
        listing = [proc(1, "pythonw.exe", ["pythonw.exe", str(deploy.APP)]),
                   proc(2, "pythonw.exe", ["pythonw.exe", "E:\\other\\app.pyw"]),
                   proc(3, "python.exe", ["python.exe", "-m", "unittest"]),
                   proc(4, "pythonw.exe", None),
                   proc(5, "notepad.exe", ["notepad.exe", str(deploy.APP)])]
        with mock.patch.object(deploy.psutil, "process_iter", return_value=listing):
            self.assertEqual([p.pid for p in deploy.tray_processes()], [1])


class LauncherTrayTests(unittest.TestCase):
    EXE = str(deploy.services.chatgpt_exe())
    CLASSES = {101: "Electron_NotifyIconHostWindow", 102: "Chrome_WidgetWin_0", 103: "Electron_NotifyIconHostWindow",
               104: "Electron_NotifyIconHostWindow", 105: "Electron_PowerMonitorHostWindow"}
    PIDS = {101: 11, 102: 11, 103: 12, 104: 13, 105: 11}
    PROCESSES = {11: ("electron.exe", [EXE, "launcher"]), 12: ("electron.exe", ["C:\\Other\\electron.exe", "app"])}

    def owners(self, *arguments):
        def named(pid):
            if pid not in self.PROCESSES:
                raise deploy.psutil.NoSuchProcess(pid)
            name, command = self.PROCESSES[pid]
            return SimpleNamespace(name=lambda: name, cmdline=lambda: command)

        def enumerate_windows(visit, extra):
            for hwnd in self.CLASSES:
                visit(hwnd, extra)

        import win32gui
        import win32process
        with mock.patch.object(win32gui, "EnumWindows", side_effect=enumerate_windows), \
                mock.patch.object(win32gui, "GetClassName", side_effect=lambda hwnd: self.CLASSES[hwnd]), \
                mock.patch.object(win32process, "GetWindowThreadProcessId", side_effect=lambda hwnd: (0, self.PIDS[hwnd])), \
                mock.patch.object(deploy.psutil, "Process", side_effect=named):
            return deploy.tray_icon_owners(*arguments)

    def test_only_the_launchers_own_electron_with_a_tray_icon_host_counts(self):
        self.assertEqual(self.owners(), [11])

    def test_another_kind_of_process_can_be_asked_for(self):
        self.assertEqual(self.owners(lambda proc: proc.name() == "electron.exe"), [11, 12])

    def test_the_switch_that_removes_the_launchers_own_icon_is_part_of_the_environment(self):
        self.assertEqual(deploy.wanted_env()["CODEX_WEB_GPT_EXTERNAL_TRAY"], "1")


class SkillLinkTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.repo = root / "repo" / "skills"
        (self.repo / "gemini-web").mkdir(parents=True)
        (self.repo / "gemini-web" / "SKILL.md").write_text("skill", encoding="utf-8")
        (self.repo / "notes").mkdir()      # no SKILL.md, not a skill
        self.codex = root / ".codex" / "skills"
        self.claude = root / ".claude" / "skills"
        for name, value in {"SKILLS": self.repo, "CODEX_SKILLS": self.codex, "CLAUDE_SKILLS": self.claude,
                            "SKILL_BACKUPS": root / "backups"}.items():
            patcher = mock.patch.object(deploy, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_new_skill_is_linked_into_codex_and_claude(self):
        done = deploy.link_skills()
        self.assertEqual(len(done), 2)
        for link in (self.codex / "gemini-web", self.claude / "gemini-web"):
            self.assertTrue(deploy.is_link(link))
            self.assertEqual(link.resolve(), (self.repo / "gemini-web").resolve())
        self.assertFalse((self.codex / "notes").exists())
        self.assertEqual(deploy.link_skills(), [])

    def test_a_real_directory_in_the_way_is_backed_up(self):
        old = self.codex / "gemini-web"
        old.mkdir(parents=True)
        (old / "SKILL.md").write_text("old copy", encoding="utf-8")
        done = deploy.link_skills()
        self.assertTrue(deploy.is_link(old))
        backups = list((Path(self.directory.name) / "backups").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "SKILL.md").read_text(encoding="utf-8"), "old copy")
        self.assertEqual(len(done), 3)

    def test_a_link_to_somewhere_else_is_repointed(self):
        elsewhere = Path(self.directory.name) / "elsewhere"
        elsewhere.mkdir()
        deploy.link_skills()
        link = self.claude / "gemini-web"
        os.rmdir(link)
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(elsewhere)], check=True, capture_output=True)
        self.assertFalse(deploy.skill_linked(link, self.codex / "gemini-web"))
        deploy.link_skills()
        self.assertTrue(deploy.skill_linked(link, self.codex / "gemini-web"))
        self.assertTrue(elsewhere.is_dir())


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.startup = Path(self.directory.name) / "Startup"
        self.startup.mkdir()
        self.old = self.startup / "Pro Consult.lnk"
        self.old.write_text("shortcut", encoding="utf-8")
        for name, value in {"SUPERSEDED": (self.old,), "BACKUPS": Path(self.directory.name) / "backups"}.items():
            patcher = mock.patch.object(deploy, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_install(self, patch_state="original", running=None, link=None, env=None, built=False):
        env = env or {}
        with mock.patch.object(deploy, "brand_shortcuts", return_value=[]), \
                mock.patch.object(deploy.web_patch, "status", return_value=patch_state), \
                mock.patch.object(deploy.web_patch, "apply") as apply, \
                mock.patch.object(deploy.services, "chatgpt_built", return_value=built), \
                mock.patch.object(deploy.updates, "build") as build, \
                mock.patch.object(deploy.services, "chatgpt_main_pid", side_effect=[running, None]), \
                mock.patch.object(deploy.services, "stop_chatgpt_web", return_value="已停止 ChatGPT Web") as stop, \
                mock.patch.object(deploy, "startup_fields", return_value=link), \
                mock.patch.object(deploy, "write_startup") as write, \
                mock.patch.object(deploy, "user_env", side_effect=lambda name: env.get(name)), \
                mock.patch.object(deploy, "set_user_env") as set_env, \
                mock.patch.object(deploy, "broadcast_environment") as broadcast, \
                mock.patch.object(deploy, "adopt_usage_float_startup", return_value=[]),                 mock.patch.object(deploy, "link_skills", return_value=[]):
            done = deploy.install()
        self.build = build
        return done, apply, stop, write, set_env, broadcast

    def test_a_fresh_machine_gets_every_step(self):
        done, apply, stop, write, set_env, broadcast = self.run_install()
        apply.assert_called_once()
        stop.assert_not_called()
        write.assert_called_once()
        self.assertEqual({call.args[0] for call in set_env.call_args_list},
                         {"PRO_CONSULT_LAUNCHER", "PRO_CONSULT_LAUNCHER_ARGS", "CODEX_WEB_GPT_EXTERNAL_TRAY"})
        broadcast.assert_called_once()
        self.assertFalse(self.old.exists())
        self.assertTrue((deploy.BACKUPS / "Pro Consult.lnk").is_file())
        self.build.assert_called_once()
        self.assertEqual(len(done), 7)

    def test_a_running_launcher_is_stopped_before_it_is_patched(self):
        _, apply, stop, *_ = self.run_install(running=50)
        stop.assert_called_once()
        apply.assert_called_once()

    def test_a_tree_that_is_patched_but_not_built_is_built(self):
        self.old.unlink()
        self.run_install(patch_state="patched", built=False)
        self.build.assert_called_once()

    def test_a_launcher_that_will_not_stop_blocks_the_patch(self):
        with mock.patch.object(deploy.web_patch, "status", return_value="original"), \
                mock.patch.object(deploy.web_patch, "apply") as apply, \
                mock.patch.object(deploy.services, "chatgpt_main_pid", return_value=50), \
                mock.patch.object(deploy.services, "stop_chatgpt_web", return_value="ChatGPT Web 正在处理请求，稍后再停"):
            with self.assertRaisesRegex(RuntimeError, "正在处理请求"):
                deploy.install()
        apply.assert_not_called()

    def test_everything_in_place_changes_nothing(self):
        self.old.unlink()
        link = (str(deploy.PYTHONW), f'"{deploy.APP}"')
        env = deploy.wanted_env()
        done, apply, stop, write, set_env, broadcast = self.run_install(patch_state="patched", link=link, env=env, built=True)
        self.assertEqual(done, ["无需改动"])
        for mocked in (apply, write, set_env, broadcast, self.build):
            mocked.assert_not_called()

    def test_a_half_patched_tree_is_an_error(self):
        with mock.patch.object(deploy.web_patch, "status", return_value="partial"):
            with self.assertRaisesRegex(RuntimeError, "partial"):
                deploy.install()


class ShortcutBrandingTests(unittest.TestCase):
    def test_owned_links_get_icon_and_identity_without_changing_launch_or_foreign_links(self):
        from win32com.client import Dispatch
        from win32com.propsys import propsys, pscon
        from win32com.shell import shell, shellcon
        import ui_theme

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            programs, desktop, startup = (base / name for name in ("Programs", "Desktop", "Startup"))
            for folder in (programs, desktop, startup):
                folder.mkdir()
            links = Dispatch("WScript.Shell")
            owned = programs / "Route Manager.lnk"
            foreign = desktop / "UsageFloat.lnk"
            for path, argument in ((owned, str(deploy.APP)), (foreign, "C:/Other/usage_float.py")):
                link = links.CreateShortcut(str(path))
                link.TargetPath = str(deploy.PYTHONW)
                link.Arguments = f'"{argument}"'
                link.Save()
            original_foreign = foreign.read_bytes()
            original_owned = owned.read_bytes()
            folders = {shellcon.CSIDL_PROGRAMS: str(programs), shellcon.CSIDL_DESKTOPDIRECTORY: str(desktop)}
            with mock.patch.object(shell, "SHGetFolderPath", side_effect=lambda _, kind, *args: folders[kind]), \
                    mock.patch.object(deploy, "STARTUP", startup), \
                    mock.patch.object(deploy, "BACKUPS", base / "backup"), \
                    mock.patch.object(ui_theme, "ICON_CACHE_DIR", base / "icons"):
                self.assertEqual(len(deploy.brand_shortcuts()), 1)
                self.assertEqual(deploy.brand_shortcuts(), [])
                link = links.CreateShortcut(str(owned))
                self.assertEqual(link.Arguments, f'"{deploy.APP}"')
                self.assertEqual(link.IconLocation, f"{ui_theme.window_icon_path()},0")
                store = propsys.SHGetPropertyStoreFromParsingName(str(owned))
                self.assertEqual(store.GetValue(pscon.PKEY_AppUserModel_ID).GetValue(), ui_theme.APP_ID)
                del store
                self.assertEqual((base / "backup/icons/Programs/Route Manager.lnk").read_bytes(), original_owned)
                self.assertEqual(foreign.read_bytes(), original_foreign)


class UsageStartupTests(unittest.TestCase):
    def test_only_the_retired_projects_entry_is_removed_and_backed_up(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(deploy.services, "STATE_DIR", Path(directory)), \
                mock.patch.object(deploy.winreg, "OpenKey"), \
                mock.patch.object(deploy.winreg, "QueryValueEx", return_value=(
                    str(deploy.services.ROOT.parent / "usage-float" / "usage_float.py"), deploy.winreg.REG_SZ)), \
                mock.patch.object(deploy.winreg, "DeleteValue") as delete:
            self.assertTrue(deploy.adopt_usage_float_startup())
            delete.assert_called_once()
            self.assertTrue((Path(directory) / "backups" / "usage-float-autostart.json").is_file())

    def test_an_unrelated_usage_float_install_is_untouched(self):
        with mock.patch.object(deploy.winreg, "OpenKey"), \
                mock.patch.object(deploy.winreg, "QueryValueEx", return_value=("C:/Other/UsageFloat.exe", deploy.winreg.REG_SZ)), \
                mock.patch.object(deploy.winreg, "DeleteValue") as delete:
            self.assertEqual(deploy.adopt_usage_float_startup(), [])
            delete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
