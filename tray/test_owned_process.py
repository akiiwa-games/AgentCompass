import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import psutil

import owned_process

CHILD = textwrap.dedent("""
    import subprocess, sys, time
    from pathlib import Path
    mode, folder = sys.argv[1], Path(sys.argv[2])
    if mode == "output":
        print("out 你好")
        print("err", file=sys.stderr)
        raise SystemExit(3)
    code = {"orphan": "import os, time; open(r'%s', 'w').write(str(os.getpid())); time.sleep(120)",
            "finishes": "import time; time.sleep(1); open(r'%s', 'w').write('done')",
            "hang": "import os, time; open(r'%s', 'w').write(str(os.getpid())); time.sleep(120)"}[mode.split(":")[0]]
    marker = folder / "marker.txt"
    subprocess.Popen([sys.executable, "-c", code % marker])    # inherits stdout/stderr
    if mode == "hang":
        open(folder / "parent.txt", "w").write(str(__import__("os").getpid()))
        time.sleep(120)
    print("parent exits")
""")


def wait_for(path: Path, seconds: float = 10) -> str:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists() and path.read_text():
            return path.read_text()
        time.sleep(0.05)
    raise AssertionError(f"{path} was not written")


class OwnedProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="owned process ")    # a space in every path
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.script = self.folder / "child script.py"
        self.script.write_text(CHILD, encoding="utf-8")
        self.addCleanup(self.reap)

    def reap(self):
        for name in ("marker.txt", "parent.txt"):
            try:
                psutil.Process(int((self.folder / name).read_text())).kill()
            except (OSError, ValueError, psutil.Error):
                pass

    def run_child(self, mode, **options):
        return owned_process.run([sys.executable, str(self.script), mode, str(self.folder)], **options)

    def test_output_exit_code_and_non_ascii_text_are_returned(self):
        result = self.run_child("output", timeout=30, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(result.returncode, 3)
        self.assertEqual((result.stdout, result.stderr), ("out 你好", "err"))

    def test_a_descendant_holding_the_output_after_its_parent_exits_cannot_outlive_the_timeout(self):
        started = time.monotonic()
        with self.assertRaises(owned_process.OwnedTimeout) as caught:
            self.run_child("orphan", timeout=3)
        self.assertLess(time.monotonic() - started, 20)    # no pipe to wait on: the deadline, not the descendant, ends it
        self.assertEqual(caught.exception.stdout, "parent exits")
        self.assertFalse(psutil.pid_exists(int(wait_for(self.folder / "marker.txt"))))

    def test_a_helper_finishing_after_the_parent_exits_keeps_the_rest_of_the_time_budget(self):
        result = self.run_child("finishes", timeout=60)
        self.assertEqual((result.returncode, result.stdout), (0, "parent exits"))
        self.assertEqual((self.folder / "marker.txt").read_text(), "done")

    def test_timeout_terminates_the_parent_and_its_descendants_and_keeps_partial_output(self):
        with self.assertRaises(owned_process.OwnedTimeout) as caught:
            self.run_child("hang", timeout=3)
        parent = int((self.folder / "parent.txt").read_text())
        child = int(wait_for(self.folder / "marker.txt"))
        self.assertFalse(psutil.pid_exists(parent))
        self.assertFalse(psutil.pid_exists(child))
        self.assertIsInstance(caught.exception.stdout, str)

    def test_a_missing_program_is_a_plain_failure_not_a_hang(self):
        result = owned_process.run([str(self.folder / "no such program.exe")], timeout=30)
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
