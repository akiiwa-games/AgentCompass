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
            skill = root / "skills" / "route-agent-work" / "scripts"
            skill.mkdir(parents=True)
            (skill / "route_judge.py").write_text("print('ok')\n", encoding="utf-8")
            cache = skill / "__pycache__"
            cache.mkdir()
            (cache / "route_judge.cpython-311.pyc").write_bytes(b"pyc")
            setup = root / "setup"
            setup.mkdir()
            (setup / "onboard.py").write_text("def setup_main(argv=None):\n    return 0\n", encoding="utf-8")
            (setup / "paseo_dispatch_bridge.mjs").write_text("export {}\n", encoding="utf-8")
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
            judge = staged / "skills" / "route-agent-work" / "scripts" / "route_judge.py"
            self.assertTrue(judge.is_file())
            self.assertFalse(judge.is_symlink())
            self.assertEqual(judge.read_text(encoding="utf-8"), "print('ok')\n")
            names = {path.relative_to(staged).as_posix() for path in staged.rglob("*") if path.is_file()}
            self.assertIn("setup/onboard.py", names)
            self.assertIn("setup/paseo-bridge/package.json", names)
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
                "skills/route-agent-work/SKILL.md": "skill\n",
                "skills/route-agent-work/scripts/route_judge.py": "print(1)\n",
                "skills/route-agent-work/grok-bot/runtime/bin/gbot.mjs": "installed runtime\n",
                "skills/route-agent-work/notes-private.md": "private\n",
                "setup/onboard.py": "x = 1\n",
                "setup/scratch_untracked.py": "y = 2\n",
                "setup/host_sync/repair-host.mjs": "legacy\n",
                "setup/paseo-bridge/package.json": "{}\n",
                "setup/paseo-bridge/secret-endpoint.txt": "http://private\n",
                ".gitignore": "skills/route-agent-work/grok-bot/runtime/\nskills/route-agent-work/notes-private.md\n",
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
                "skills/route-agent-work/SKILL.md",
                "skills/route-agent-work/scripts/route_judge.py",
                "setup/onboard.py",
                "setup/paseo-bridge/package.json",
            })

    def test_the_real_checkout_stages_no_ignored_or_untracked_file(self):
        import shutil
        import subprocess
        from tempfile import TemporaryDirectory
        if shutil.which("git") is None or not (ROOT / ".git").exists():
            self.skipTest("not a git checkout")
        stage_data = _stage_data()
        listed = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True).stdout
        tracked = {name.decode("utf-8") for name in listed.split(b"\0") if name}
        with TemporaryDirectory() as directory:
            staged = stage_data.stage(ROOT, Path(directory) / "staged")
            names = {path.relative_to(staged).as_posix() for path in staged.rglob("*") if path.is_file()}
        self.assertTrue(names)
        self.assertEqual(sorted(name for name in names if name not in tracked), [])
        self.assertFalse(any(name.startswith("setup/host_sync/") for name in names))

    def test_the_frozen_tray_carries_the_helpers_it_starts_and_what_the_router_scripts_import(self):
        import re
        spec = (ROOT / "packaging" / "agentcompass-tray.spec").read_text(encoding="utf-8")
        launch = (ROOT / "tray" / "component_launch.py").read_text(encoding="utf-8")
        components = re.findall(r'"(\w+)",', launch[launch.index("COMPONENTS"):launch.index(")", launch.index("COMPONENTS"))])
        self.assertTrue(components)
        helpers = spec[spec.index("HELPER_MODULES = ["):spec.index("]", spec.index("HELPER_MODULES = ["))]
        for name in components:
            self.assertIn(f'"{name}"', helpers)
        self.assertIn("*stdlib_imports(router_scripts)", spec)
        # provider_inventory reads TOML, which the frozen interpreter only has when it is named.
        scripts = (ROOT / "skills" / "route-agent-work" / "scripts" / "provider_inventory.py").read_text(encoding="utf-8")
        self.assertIn("import tomllib", scripts)

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

    def test_metadata_describes_the_install_and_the_unsigned_tray(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        iss = (ROOT / "packaging" / "agentcompass.iss").read_text(encoding="utf-8")
        signing = (ROOT / "packaging" / "SIGNING.md").read_text(encoding="utf-8")
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        hook = (ROOT / "packaging" / "hatch_build.py").read_text(encoding="utf-8")
        spec = (ROOT / "packaging" / "agentcompass-tray.spec").read_text(encoding="utf-8")
        self.assertIn('self.target_name not in ("wheel", "sdist")', hook)
        self.assertIn("only-include", pyproject)
        self.assertIn('"**/node_modules"', pyproject)
        self.assertIn('"setup/host_sync/out"', pyproject)
        self.assertIn('requires-python = ">=3.11"', pyproject)
        self.assertIn('agentcompass = "agentcompass.cli:main"', pyproject)
        self.assertIn("pystray", pyproject)
        self.assertIn("pywin32", pyproject)
        self.assertIn("PrivilegesRequired=lowest", iss)
        self.assertIn("agentcompass uninstall", iss)
        self.assertIn("Route Manager", iss)
        self.assertIn("PRO_CONSULT_LAUNCHER", iss)
        self.assertIn("CODEX_WEB_GPT_EXTERNAL_TRAY", iss)
        self.assertIn("SmartScreen", signing)
        self.assertIn("signtool sign /fd SHA256", signing)
        self.assertIn('tags:', workflow)
        self.assertIn('contents: write', workflow)
        self.assertIn("console=False", spec)
        self.assertIn("app.pyw", spec)
        self.assertIn('name="AgentCompass"', spec)


if __name__ == "__main__":
    unittest.main()
