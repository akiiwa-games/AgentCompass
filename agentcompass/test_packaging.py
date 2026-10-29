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
            self.assertIn("setup/host_sync/build_bundle.py", names)
            self.assertFalse(any("node_modules" in name or name.endswith(".pyc") or "/out/" in name for name in names))

    def test_metadata_describes_the_install_and_the_unsigned_tray(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        iss = (ROOT / "packaging" / "agentcompass.iss").read_text(encoding="utf-8")
        signing = (ROOT / "packaging" / "SIGNING.md").read_text(encoding="utf-8")
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        hook = (ROOT / "packaging" / "hatch_build.py").read_text(encoding="utf-8")
        spec = (ROOT / "packaging" / "agentcompass-tray.spec").read_text(encoding="utf-8")
        self.assertIn('self.target_name != "wheel"', hook)
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
