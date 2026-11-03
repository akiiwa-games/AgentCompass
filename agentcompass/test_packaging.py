import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _stage_data():
    path = ROOT / "packaging" / "stage_data.py"
    spec = importlib.util.spec_from_file_location("agentcompass_stage_data", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PackagingTests(unittest.TestCase):
    def test_stage_keeps_real_skill_files_and_skips_heavy_trees(self):
        stage_data = _stage_data()
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            root = Path(directory) / "src"
            skill = root / "skills" / "agent-compass" / "scripts"
            skill.mkdir(parents=True)
            (skill / "route_judge.py").write_text("print('ok')\n", encoding="utf-8")
            cache = skill / "__pycache__"
            cache.mkdir()
            (cache / "route_judge.cpython-311.pyc").write_bytes(b"pyc")
            setup = root / "setup"
            setup.mkdir()
            (setup / "onboard.py").write_text("def setup_main(argv=None):\n    return 0\n", encoding="utf-8")
            (setup / "paseo_dispatch_bridge.mjs").write_text("export {}\n", encoding="utf-8")
            for folder, name in ((skill, "test_allocator.py"), (skill, "test_host_bridge.mjs"),
                                 (skill, "router_test_support.py"), (skill, "conftest.py"),
                                 (setup, "test_onboard.py")):
                (folder / name).write_text("development test\n", encoding="utf-8")
            bridge = setup / "paseo-bridge"
            bridge.mkdir()
            (bridge / "package.json").write_text("{}\n", encoding="utf-8")
            (bridge / "package-lock.json").write_text("{}\n", encoding="utf-8")
            (bridge / "README.md").write_text("bridge\n", encoding="utf-8")
            modules = bridge / "node_modules" / "left"
            modules.mkdir(parents=True)
            (modules / "index.js").write_text("nope\n", encoding="utf-8")
            huge = setup / "host_sync" / "out"
            huge.mkdir(parents=True)
            (huge / "blob.bin").write_bytes(b"x" * 32)
            (setup / "host_sync" / "build_bundle.py").write_text("print(1)\n", encoding="utf-8")
            staged = stage_data.stage(root, Path(directory) / "staged")
            judge = staged / "skills" / "agent-compass" / "scripts" / "route_judge.py"
            self.assertTrue(judge.is_file())
            self.assertFalse(judge.is_symlink())
            self.assertEqual(judge.read_text(encoding="utf-8"), "print('ok')\n")
            names = {path.relative_to(staged).as_posix() for path in staged.rglob("*") if path.is_file()}
            self.assertIn("setup/onboard.py", names)
            self.assertIn("setup/paseo-bridge/package.json", names)
            self.assertIn("setup/paseo_dispatch_bridge.mjs", names)
            self.assertFalse(any(Path(name).name.startswith("test_")
                                 or Path(name).name in {"conftest.py", "router_test_support.py"} for name in names))
            self.assertFalse(any(name.startswith("setup/host_sync/") for name in names))
            self.assertFalse(any("node_modules" in name or name.endswith(".pyc") or "/out/" in name for name in names))

    @staticmethod
    def _git(root, *args):
        import subprocess
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

    def test_a_checkout_stages_only_what_git_tracks(self):
        import shutil
        from tempfile import TemporaryDirectory
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        stage_data = _stage_data()
        with TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            for relative, text in {
                "skills/agent-compass/SKILL.md": "skill\n",
                "skills/agent-compass/scripts/route_judge.py": "print(1)\n",
                "skills/agent-compass/grok-bot/runtime/bin/gbot.mjs": "installed runtime\n",
                "skills/agent-compass/notes-private.md": "private\n",
                "setup/onboard.py": "x = 1\n",
                "setup/scratch_untracked.py": "y = 2\n",
                "setup/host_sync/repair-host.mjs": "legacy\n",
                "setup/paseo-bridge/package.json": "{}\n",
                "setup/paseo-bridge/secret-endpoint.txt": "http://private\n",
                ".gitignore": "skills/agent-compass/grok-bot/runtime/\nskills/agent-compass/notes-private.md\n",
            }.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            self._git(root, "init", "-q")
            # Track the legacy script on purpose: tracked is necessary, not sufficient.
            self._git(root, "add", ".gitignore", "skills", "setup/onboard.py", "setup/host_sync",
                      "setup/paseo-bridge/package.json")
            staged = stage_data.stage(root, Path(directory) / "staged")
            names = {path.relative_to(staged).as_posix() for path in staged.rglob("*") if path.is_file()}
            self.assertEqual(names, {
                "skills/agent-compass/SKILL.md",
                "skills/agent-compass/scripts/route_judge.py",
                "setup/onboard.py",
                "setup/paseo-bridge/package.json",
            })



    def test_payload_check_flags_untracked_files_in_a_wheel_sdist_and_pyinstaller_table(self):
        import tarfile
        import zipfile
        from tempfile import TemporaryDirectory
        path = ROOT / "packaging" / "check_payload.py"
        spec = importlib.util.spec_from_file_location("agentcompass_check_payload", path)
        check = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(check)
        files = {"agentcompass/cli.py", "skills/a/SKILL.md", "setup/onboard.py", "README.md", "tray/app.pyw"}
        with TemporaryDirectory() as directory:
            base = Path(directory)
            wheel = base / "x.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                for name in ("agentcompass/cli.py", "agentcompass/data/skills/a/SKILL.md",
                             "agentcompass/data/setup/onboard.py", "agentcompass-0.1.0.dist-info/METADATA",
                             "agentcompass/data/setup/host_sync/repair-host.mjs",
                             "agentcompass/data/skills/a/grok-bot/runtime/x.mjs"):
                    archive.writestr(name, "x")
            self.assertEqual(check.wheel_problems(wheel, files), [
                "x.whl: agentcompass/data/setup/host_sync/repair-host.mjs is not tracked in git",
                "x.whl: agentcompass/data/skills/a/grok-bot/runtime/x.mjs is not tracked in git",
            ])
            sdist = base / "x.tar.gz"
            with tarfile.open(sdist, "w:gz") as archive:
                for name in ("pkg-0.1/README.md", "pkg-0.1/PKG-INFO", "pkg-0.1/SYNC-REPORT.md"):
                    source = base / "member"
                    source.write_text("x", encoding="utf-8")
                    archive.add(source, arcname=name)
            self.assertEqual(check.sdist_problems(sdist, files), ["x.tar.gz: SYNC-REPORT.md is not tracked in git"])
            repo = base / "repo"
            (repo / "tray").mkdir(parents=True)
            (repo / "tray" / "app.pyw").write_text("x", encoding="utf-8")
            (repo / "tray" / "private_scratch.py").write_text("x", encoding="utf-8")
            toc = base / "Analysis-00.toc"
            toc.write_text(repr(([str(repo / "tray" / "app.pyw")],
                                 [("scratch", str(repo / "tray" / "private_scratch.py"), "PYMODULE")],
                                 [("os", str(base / "outside" / "os.py"), "PYMODULE")])), encoding="utf-8")
            self.assertEqual(check.toc_problems(toc, files, repo),
                             ["Analysis-00.toc: tray/private_scratch.py is not tracked in git"])



if __name__ == "__main__":
    unittest.main()
