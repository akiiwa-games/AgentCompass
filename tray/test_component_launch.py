import sys
import unittest
from pathlib import Path
from unittest import mock

import component_launch as launch


class CommandTests(unittest.TestCase):
    def test_from_source_a_helper_runs_as_a_script_under_the_windowed_or_console_python(self):
        with mock.patch.object(launch, "frozen", return_value=False), \
                mock.patch.object(launch.sys, "executable", "C:/Python/python.exe"):
            self.assertEqual(launch.command("E:/repo/tray/router_window.py", "--page", "x"),
                             [str(Path("C:/Python/pythonw.exe")), str(Path("E:/repo/tray/router_window.py")), "--page", "x"])
            self.assertEqual(launch.command("E:/repo/tray/capacity_stats.py", windowed=False),
                             [str(Path("C:/Python/python.exe")), str(Path("E:/repo/tray/capacity_stats.py"))])
            bare = launch.command("network_status")
        self.assertEqual(Path(bare[1]), Path(launch.__file__).resolve().parent / "network_status.py")

    def test_frozen_a_helper_is_the_same_executable_with_a_component_name(self):
        with mock.patch.object(launch, "frozen", return_value=True), \
                mock.patch.object(launch.sys, "executable", "C:/Apps/AgentCompass.exe"):
            self.assertEqual(launch.command("E:/anything/tray/router_window.py", "--page", "x"),
                             ["C:/Apps/AgentCompass.exe", "--run-component", "router_window", "--page", "x"])

    def test_every_listed_component_is_a_module_in_this_folder(self):
        folder = Path(launch.__file__).resolve().parent
        for name in launch.COMPONENTS:
            self.assertTrue((folder / f"{name}.py").is_file(), name)


class DispatchTests(unittest.TestCase):
    def setUp(self):
        saved = list(sys.argv)
        self.addCleanup(lambda: sys.argv.__setitem__(slice(None), saved))

    def test_ordinary_start_is_left_alone(self):
        self.assertIsNone(launch.dispatch(["AgentCompass.exe"]))
        self.assertIsNone(launch.dispatch(["AgentCompass.exe", "--attach-only"]))

    def test_a_known_component_runs_as_main_with_its_own_arguments(self):
        seen = {}

        def run(name, run_name, alter_sys):
            seen.update(name=name, run_name=run_name, argv=list(sys.argv))

        with mock.patch.object(launch.runpy, "run_module", side_effect=run):
            code = launch.dispatch(["AgentCompass.exe", "--run-component", "capacity_stats", "--once"])
        self.assertEqual(code, 0)
        self.assertEqual(seen, {"name": "capacity_stats", "run_name": "__main__", "argv": ["capacity_stats", "--once"]})

    def test_exit_codes_pass_through_and_unknown_names_are_refused(self):
        with mock.patch.object(launch.runpy, "run_module", side_effect=SystemExit(3)):
            self.assertEqual(launch.dispatch(["x", "--run-component", "router_window"]), 3)
        with mock.patch.object(launch.runpy, "run_module", side_effect=SystemExit(None)):
            self.assertEqual(launch.dispatch(["x", "--run-component", "router_window"]), 0)
        self.assertEqual(launch.dispatch(["x", "--run-component", "os"]), 2)


if __name__ == "__main__":
    unittest.main()
