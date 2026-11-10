import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest import mock

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


    def test_the_installer_ships_a_console_cli_on_the_user_path(self):
        spec = (ROOT / "packaging" / "agentcompass-tray.spec").read_text(encoding="utf-8")
        self.assertIn('name="agentcompass"', spec)
        self.assertIn("console=True", spec)
        self.assertIn('dist_app / "cli"', spec)
        self.assertIn("app_bundle.stage_installed_app", spec)
        self.assertIn('("X utf8", None, "OPTION")', spec)
        iss = (ROOT / "packaging" / "agentcompass.iss").read_text(encoding="utf-8")
        self.assertIn("ChangesEnvironment=yes", iss)
        self.assertIn("AddCliToPath();", iss)
        self.assertIn("RemoveCliFromPath();", iss)
        self.assertIn("{param:SETUP|}", iss)
        # [Run] entries run before ssPostInstall: the folder the /SETUP log is appended to must come from [Dirs].
        dirs = iss.split("[Dirs]", 1)[1].split("\n[", 1)[0] if "[Dirs]" in iss else ""
        self.assertIn(r'Name: "{%USERPROFILE}\.agent-compass"', dirs)
        self.assertIn(r'Filename: "{app}\cli\agentcompass.exe"; Parameters: "uninstall --yes"', iss)
        self.assertNotIn("/D /C agentcompass uninstall", iss)       # never whichever agentcompass PATH finds first

    def test_the_installer_leaves_a_start_menu_entry_to_launch_the_tray(self):
        iss = (ROOT / "packaging" / "agentcompass.iss").read_text(encoding="utf-8")
        icons = iss.split("[Icons]", 1)[1].split("\n[", 1)[0] if "[Icons]" in iss else ""
        self.assertIn(r'Name: "{userprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"', icons)
        self.assertIn("Tasks: desktopicon", icons)                  # the desktop icon stays opt-in

    def test_release_gate_profiles_pass_validation(self):
        """The release gate plans with these literals; an invalid one stops the release, and no unit suite runs them."""
        import ast
        import sys
        sys.path.insert(0, str(ROOT / "skills" / "agent-compass" / "scripts"))
        import allocator
        for name in ("sandbox_check.py", "guest_check.py"):
            tree = ast.parse((ROOT / "packaging" / name).read_text(encoding="utf-8"))
            literals = [ast.literal_eval(node) for node in ast.walk(tree) if isinstance(node, ast.Dict)
                        and any(isinstance(k, ast.Constant) and k.value == "interaction_style" for k in node.keys)]
            self.assertTrue(literals, name)
            for literal in literals:
                with self.subTest(file=name):
                    allocator.validate_caller_profile(literal)

    def test_fixture_repos_hold_tracked_files_only(self):
        """An untracked file must stay out of the fixture, or `git add -A` there hides it from check_payload."""
        def git(repo, *args):
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                           check=True, capture_output=True)

        from tempfile import TemporaryDirectory
        sandbox = self._load_packaging_module("sandbox_check")
        foreign = self._load_packaging_module("foreign_machine_check")
        with TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source-tree"
            source.mkdir()
            (source / "tracked.txt").write_text("committed", encoding="utf-8")
            git(source, "init", "-q")
            git(source, "add", "tracked.txt")
            git(source, "commit", "-qm", "seed")
            (source / "tracked.txt").write_text("edited", encoding="utf-8")
            (source / "stray.txt").write_text("not tracked", encoding="utf-8")
            with mock.patch.object(sandbox, "ROOT", source):
                sandbox_repo = sandbox.build_fixture_repo(base / "sandbox")
            with mock.patch.object(foreign, "USAGE_FLOAT_SRC", source):
                foreign_repo = foreign.build_usage_float_fixture_repo(base / "foreign")
            for repo in (sandbox_repo, foreign_repo):
                with self.subTest(repo=repo.parent.name):
                    self.assertEqual((repo / "tracked.txt").read_text(encoding="utf-8"), "edited")
                    self.assertFalse((repo / "stray.txt").exists())

    @staticmethod
    def _load_packaging_module(name: str):
        path = ROOT / "packaging" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(f"agentcompass_{name}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @classmethod
    def _make_mini_repo(cls, base: Path, version: str = "0.4.0") -> Path:
        repo = base / f"repo-{version}"
        files = {
            "agentcompass/__init__.py": f'__version__ = "{version}"\n',
            "agentcompass/cli.py": "def main(argv=None):\n    return 0\n",
            "tray/app.pyw": "# tray app\n",
            "tray/component_launch.py": "def dispatch(argv=None):\n    return None\n",
            "tray/updates.py": "# updates\n",
            "tray/easytier_supervisor.ps1": "Write-Output 'ok'\n",
            "skills/agent-compass/SKILL.md": "---\nname: agent-compass\n---\n",
            "skills/agent-compass/scripts/route_judge.py": "print('judge')\n",
            "setup/setup_agent_compass.py": "print('setup')\n",
            "setup/onboard.py": "def setup_main(argv=None):\n    return 0\n",
            "packaging/agentcompass-tray.spec": "# spec v1\n",
            "packaging/agentcompass.iss": "; iss v1\n",
            "packaging/app_bootstrap.py": (ROOT / "packaging" / "app_bootstrap.py").read_text(encoding="utf-8"),
            "packaging/cli_entry.py": (ROOT / "packaging" / "cli_entry.py").read_text(encoding="utf-8"),
            "packaging/tray_entry.py": (ROOT / "packaging" / "tray_entry.py").read_text(encoding="utf-8"),
            "packaging/stage_data.py": (ROOT / "packaging" / "stage_data.py").read_text(encoding="utf-8"),
            "packaging/build.ps1": "# build v1\n",
        }
        for rel, content in files.items():
            target = repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
        frozen_dll = repo / "dist" / "AgentCompass" / "_internal" / "python313.dll"
        frozen_dll.parent.mkdir(parents=True, exist_ok=True)
        frozen_dll.write_bytes(b"MZ-frozen-runtime-dll-v1")
        return repo

    def test_runtime_fingerprint_tracks_runtime_inputs_and_ignores_app_code_edits(self):
        from tempfile import TemporaryDirectory

        app_bundle = self._load_packaging_module("app_bundle")
        if (ROOT / "dist" / "AgentCompass" / "_internal").is_dir():
            self.assertTrue(app_bundle.collect_runtime_asset_hashes(ROOT))
        with TemporaryDirectory() as tmp:
            repo = self._make_mini_repo(Path(tmp), version="0.4.0")
            base_fp = app_bundle.compute_runtime_fingerprint(repo)
            self.assertEqual(len(base_fp["runtime_fingerprint"]), 64)
            self.assertEqual(
                set(base_fp["bundled_runtime_assets_sha256"].keys()),
                {"_internal/python313.dll"},
            )

            # 1. Editing application code (tray/, agentcompass/, skills/, setup/) or build scripts (build.ps1, stage_data.py)
            # does NOT change runtime_fingerprint (R3-4)
            (repo / "tray" / "app.pyw").write_text("# modified tray code\nprint(123)\n", encoding="utf-8", newline="\n")
            (repo / "agentcompass" / "cli.py").write_text("def main(argv=None):\n    return 42\n", encoding="utf-8", newline="\n")
            (repo / "skills" / "agent-compass" / "scripts" / "route_judge.py").write_text("print('v2')\n", encoding="utf-8", newline="\n")
            (repo / "packaging" / "build.ps1").write_text("# build script log tweak\n", encoding="utf-8", newline="\n")
            (repo / "packaging" / "stage_data.py").write_text(
                (repo / "packaging" / "stage_data.py").read_text(encoding="utf-8") + "\n# comment\n",
                encoding="utf-8",
                newline="\n",
            )
            same_fp = app_bundle.compute_runtime_fingerprint(repo)
            self.assertEqual(same_fp["runtime_fingerprint"], base_fp["runtime_fingerprint"])

            # 1b. Universal CRT / API-set DLLs that only the CI runner image bundles do not change it either
            internal = repo / "dist" / "AgentCompass" / "_internal"
            for name in ("api-ms-win-core-file-l1-1-0.dll", "API-MS-WIN-CRT-heap-l1-1-0.dll", "ucrtbase.dll"):
                (internal / name).write_bytes(b"MZ-runner-image")
            crt_fp = app_bundle.compute_runtime_fingerprint(repo)
            self.assertEqual(crt_fp["runtime_fingerprint"], base_fp["runtime_fingerprint"])
            self.assertEqual(set(crt_fp["bundled_runtime_assets_sha256"]), {"_internal/python313.dll"})

            # 2. Editing .spec changes runtime_fingerprint
            (repo / "packaging" / "agentcompass-tray.spec").write_text("# spec v2\n", encoding="utf-8", newline="\n")
            spec_fp = app_bundle.compute_runtime_fingerprint(repo)
            self.assertNotEqual(spec_fp["runtime_fingerprint"], base_fp["runtime_fingerprint"])
            (repo / "packaging" / "agentcompass-tray.spec").write_text("# spec v1\n", encoding="utf-8", newline="\n")

            # 3. Editing .iss updates iss_sha256 AND invalidates runtime_fingerprint so installer changes trigger full setup
            (repo / "packaging" / "agentcompass.iss").write_text("; iss v2\n", encoding="utf-8", newline="\n")
            iss_fp = app_bundle.compute_runtime_fingerprint(repo)
            self.assertNotEqual(iss_fp["iss_sha256"], base_fp["iss_sha256"])
            self.assertNotEqual(iss_fp["runtime_fingerprint"], base_fp["runtime_fingerprint"])
            (repo / "packaging" / "agentcompass.iss").write_text("; iss v1\n", encoding="utf-8", newline="\n")

            # 4. Editing boot loader entry (app_bootstrap.py / tray_entry.py) changes runtime_fingerprint
            (repo / "packaging" / "tray_entry.py").write_text("# boot loader v2\n", encoding="utf-8", newline="\n")
            boot_fp = app_bundle.compute_runtime_fingerprint(repo)
            self.assertNotEqual(boot_fp["runtime_fingerprint"], base_fp["runtime_fingerprint"])
            (repo / "packaging" / "tray_entry.py").write_text(
                (ROOT / "packaging" / "tray_entry.py").read_text(encoding="utf-8"),
                encoding="utf-8",
                newline="\n",
            )

            # 5. Changing third-party package version, bundled modules, or frozen _internal runtime binary assets changes runtime_fingerprint
            pkg_fp = app_bundle.compute_runtime_fingerprint(
                repo,
                third_party_packages={"Pillow": "1.0.0", "psutil": "99.0.0", "pystray": "1.0.0", "pywin32": "1.0.0"},
            )
            self.assertNotEqual(pkg_fp["runtime_fingerprint"], base_fp["runtime_fingerprint"])

            mod_fp = app_bundle.compute_runtime_fingerprint(
                repo,
                bundled_modules={"tray": ["os", "sys"], "cli": ["os", "sys"]},
            )
            self.assertNotEqual(mod_fp["runtime_fingerprint"], base_fp["runtime_fingerprint"])

            (repo / "dist" / "AgentCompass" / "_internal" / "python313.dll").write_bytes(b"MZ-frozen-runtime-dll-v2")
            asset_fp = app_bundle.compute_runtime_fingerprint(repo)
            self.assertNotEqual(asset_fp["runtime_fingerprint"], base_fp["runtime_fingerprint"])

    def test_bundled_stdlib_covers_all_standard_modules_and_excludes_bloat(self):
        app_bundle = self._load_packaging_module("app_bundle")
        tray_mods = set(app_bundle.tray_hidden_imports())
        cli_mods = set(app_bundle.cli_hidden_imports())
        required_stdlib = {
            "configparser",
            "asyncio",
            "socketserver",
            "wsgiref",
            "plistlib",
            "venv",
            "unittest",
            "unittest.mock",
            "xml.etree.ElementTree",
            "xml.dom.minidom",
            "http.server",
            "logging.handlers",
            "logging.config",
            "email.mime.text",
            "email.mime.multipart",
        }
        self.assertTrue(required_stdlib.issubset(tray_mods), required_stdlib - tray_mods)
        self.assertTrue(required_stdlib.issubset(cli_mods), required_stdlib - cli_mods)
        self.assertIn("tkinter.scrolledtext", tray_mods)
        self.assertIn("win32job", tray_mods)
        self.assertIn("numpy", app_bundle.NON_RUNTIME_EXCLUDES)
        self.assertIn("Pythonwin", app_bundle.NON_RUNTIME_EXCLUDES)
        self.assertIn("PIL._avif", app_bundle.NON_RUNTIME_EXCLUDES)

    def test_build_app_package_stage_and_release_manifest(self):
        import json
        import zipfile
        from tempfile import TemporaryDirectory

        app_bundle = self._load_packaging_module("app_bundle")
        check = self._load_packaging_module("check_payload")
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = self._make_mini_repo(base, version="0.4.0")
            dist_dir = base / "dist"
            install_root = dist_dir / "AgentCompass"
            (install_root / "cli").mkdir(parents=True)
            (install_root / "AgentCompass.exe").write_bytes(b"MZ-tray-boot")
            (install_root / "cli" / "agentcompass.exe").write_bytes(b"MZ-cli-boot")

            staged = app_bundle.stage_installed_app(repo, install_root, dist_dir=dist_dir, version="0.4.0")
            app_zip = Path(staged["app_package"]["path"])
            self.assertEqual(app_zip.name, "app-0.4.0.zip")
            self.assertTrue(app_zip.is_file())
            self.assertTrue((install_root / "runtime-info.json").is_file())
            self.assertTrue((install_root / "app" / "current" / "app-manifest.json").is_file())
            self.assertTrue((install_root / "app" / "current" / "tray" / "easytier_supervisor.ps1").is_file())
            self.assertTrue((install_root / "app" / "packages" / "app-0.4.0.zip").is_file())

            with zipfile.ZipFile(app_zip, "r") as zf:
                names = set(zf.namelist())
                self.assertIn("app-manifest.json", names)
                self.assertIn("agentcompass/__init__.py", names)
                self.assertIn("tray/app.pyw", names)
                self.assertIn("tray/easytier_supervisor.ps1", names)
                self.assertIn("skills/agent-compass/SKILL.md", names)
                self.assertIn("setup/onboard.py", names)
                app_manifest = json.loads(zf.read("app-manifest.json").decode("utf-8"))
            self.assertEqual(app_manifest["version"], "0.4.0")
            self.assertEqual(app_manifest["required_runtime_fingerprint"], staged["runtime_info"]["runtime_fingerprint"])

            setup_exe = dist_dir / "AgentCompass-tray-setup.exe"
            setup_exe.write_bytes(b"MZ-full-installer-setup")
            manifest = app_bundle.build_release_manifest(
                repo,
                dist_dir,
                version="0.4.0",
                app_zip=app_zip,
                installer_exe=setup_exe,
            )
            manifest_path = dist_dir / "manifest.json"
            self.assertTrue(manifest_path.is_file())
            self.assertEqual(manifest["version"], "0.4.0")
            self.assertEqual(manifest["runtime_fingerprint"], staged["runtime_info"]["runtime_fingerprint"])
            self.assertEqual(manifest["app_package"]["name"], "app-0.4.0.zip")
            self.assertEqual(manifest["app_package"]["sha256"], staged["app_package"]["sha256"])
            self.assertEqual(manifest["installer"]["name"], "AgentCompass-tray-setup.exe")
            self.assertEqual(len(manifest["installer"]["sha256"]), 64)

            # Verify check_payload.app_zip_problems accepts tracked files + app-manifest.json and flags untracked files
            tracked = {
                "agentcompass/__init__.py",
                "agentcompass/cli.py",
                "tray/app.pyw",
                "tray/component_launch.py",
                "tray/updates.py",
                "tray/easytier_supervisor.ps1",
                "skills/agent-compass/SKILL.md",
                "skills/agent-compass/scripts/route_judge.py",
                "setup/setup_agent_compass.py",
                "setup/onboard.py",
            }
            self.assertEqual(check.app_zip_problems(app_zip, tracked), [])
            # Verify check_payload._resolve_cli_paths expands pwsh literal glob patterns like dist/app-*.zip
            resolved_zips, glob_problems = check._resolve_cli_paths(dist_dir / "app-*.zip")
            self.assertEqual(glob_problems, [])
            self.assertEqual(resolved_zips, [app_zip])
            _, missing_problems = check._resolve_cli_paths(dist_dir / "app-nonexistent-*.zip")
            self.assertEqual(len(missing_problems), 1)
            self.assertIn("no matching file found", missing_problems[0])

            bad_zip = dist_dir / "app-bad.zip"
            with zipfile.ZipFile(bad_zip, "w") as zf:
                zf.writestr("app-manifest.json", "{}")
                zf.writestr("tray/untracked_secret.py", "x = 1\n")
            self.assertEqual(
                check.app_zip_problems(bad_zip, tracked),
                ["app-bad.zip: tray/untracked_secret.py is not tracked in git"],
            )

    def test_app_bootstrap_rolls_back_to_previous_when_current_is_corrupted(self):
        import json
        import shutil
        from tempfile import TemporaryDirectory

        app_bundle = self._load_packaging_module("app_bundle")
        app_bootstrap = self._load_packaging_module("app_bootstrap")
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo_v1 = self._make_mini_repo(base, version="0.4.0")
            repo_v2 = self._make_mini_repo(base, version="0.4.1")
            dist_dir = base / "dist"
            install_root = dist_dir / "AgentCompass"
            install_root.mkdir(parents=True)

            # Stage 0.4.0 as current, then copy it to previous and stage 0.4.1 as current
            app_bundle.stage_installed_app(repo_v1, install_root, dist_dir=dist_dir, version="0.4.0")
            prev_copy = base / "prev-0.4.0"
            shutil.copytree(install_root / "app" / "current", prev_copy)
            app_bundle.stage_installed_app(repo_v2, install_root, dist_dir=dist_dir, version="0.4.1")
            shutil.copytree(prev_copy, install_root / "app" / "previous")

            # Healthy current activates 0.4.1
            active = app_bootstrap.activate_app_dir(install_root)
            self.assertEqual(
                json.loads((active / "app-manifest.json").read_text(encoding="utf-8"))["version"],
                "0.4.1",
            )

            # Corrupt a core file in app/current -> activate_app_dir detects sha256 mismatch and rolls back to 0.4.0
            (install_root / "app" / "current" / "tray" / "app.pyw").write_text("# corrupted!\n", encoding="utf-8")
            recovered = app_bootstrap.activate_app_dir(install_root)
            self.assertEqual(
                json.loads((recovered / "app-manifest.json").read_text(encoding="utf-8"))["version"],
                "0.4.0",
            )
            state = json.loads((install_root / "app" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["current_version"], "0.4.0")
            self.assertEqual(state["active_version"], "0.4.0")
            self.assertIn("mismatch", state["rollback_reason"])

            # Runtime fingerprint mismatch in app/current is also rejected by validate_app_dir
            rt_info = json.loads((install_root / "runtime-info.json").read_text(encoding="utf-8"))
            rt_info["runtime_fingerprint"] = "f" * 64
            (install_root / "runtime-info.json").write_text(json.dumps(rt_info), encoding="utf-8")
            ok_fp, reason_fp = app_bootstrap.validate_app_dir(install_root / "app" / "current", install_root=install_root)
            self.assertFalse(ok_fp)
            self.assertIn("runtime_fingerprint mismatch", reason_fp)

    def test_run_tray_and_cli_rollback_after_startup_crash_and_sort_packages_semantically(self):
        import ctypes
        import json
        import shutil
        import sys
        import uuid
        from tempfile import TemporaryDirectory

        app_bundle = self._load_packaging_module("app_bundle")
        app_bootstrap = self._load_packaging_module("app_bootstrap")
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo_v9 = self._make_mini_repo(base, version="0.3.9")
            repo_v18 = self._make_mini_repo(base, version="0.3.18")
            repo_v40 = self._make_mini_repo(base, version="0.4.0")
            marker_file = base / "started-version.txt"
            cli_marker = base / "cli-version.txt"
            mutex_name = f"Local\\AgentCompassTrayTest_{uuid.uuid4().hex}"
            (repo_v18 / "tray" / "app.pyw").write_text(
                f"from pathlib import Path\nPath({str(marker_file)!r}).write_text('0.3.18', encoding='utf-8')\n",
                encoding="utf-8",
                newline="\n",
            )
            real_frozen_py = (ROOT / "agentcompass" / "frozen_python.py").read_text(encoding="utf-8")
            shim_cli = (
                "import sys\n"
                "from . import frozen_python\n"
                "def main(argv=None):\n"
                "    args = list(sys.argv[1:] if argv is None else argv)\n"
                "    if frozen_python.looks_like_python(args):\n"
                "        return frozen_python.run(args)\n"
                "    raise SystemExit(f'unknown command {args!r}')\n"
            )
            (repo_v18 / "agentcompass" / "cli.py").write_text(shim_cli, encoding="utf-8", newline="\n")
            (repo_v18 / "agentcompass" / "frozen_python.py").write_text(real_frozen_py, encoding="utf-8", newline="\n")
            (repo_v18 / "skills" / "agent-compass" / "scripts" / "route_judge.py").write_text(
                f"import sys\nfrom pathlib import Path\nPath({str(cli_marker)!r}).write_text('0.3.18:' + ' '.join(sys.argv[1:]), encoding='utf-8')\n",
                encoding="utf-8",
                newline="\n",
            )
            # 0.4.0 acquires a real Windows named mutex into _SINGLE_INSTANCE_MUTEX and crashes outside try/except
            (repo_v40 / "tray" / "app.pyw").write_text(
                f"import ctypes, sys\n"
                f"_SINGLE_INSTANCE_MUTEX = ctypes.windll.kernel32.CreateMutexW(None, False, {mutex_name!r})\n"
                f"sys._agentcompass_tray_mutex = _SINGLE_INSTANCE_MUTEX\n"
                f"raise RuntimeError('simulated 0.4.0 startup crash after mutex')\n",
                encoding="utf-8",
                newline="\n",
            )
            (repo_v40 / "agentcompass" / "cli.py").write_text(shim_cli, encoding="utf-8", newline="\n")
            (repo_v40 / "agentcompass" / "frozen_python.py").write_text(real_frozen_py, encoding="utf-8", newline="\n")
            (repo_v40 / "skills" / "agent-compass" / "scripts" / "route_judge.py").write_text(
                "import nonexistent_subcommand_module\n",
                encoding="utf-8",
                newline="\n",
            )
            dist_dir = base / "dist"
            install_root = dist_dir / "AgentCompass"
            install_root.mkdir(parents=True)

            app_bundle.stage_installed_app(repo_v9, install_root, dist_dir=dist_dir, version="0.3.9")
            app_bundle.stage_installed_app(repo_v18, install_root, dist_dir=dist_dir, version="0.3.18")
            shutil.copy2(dist_dir / "app-0.3.9.zip", install_root / "app" / "packages" / "app-0.3.9.zip")
            app_bundle.stage_installed_app(repo_v40, install_root, dist_dir=dist_dir, version="0.4.0")
            shutil.copy2(dist_dir / "app-0.3.9.zip", install_root / "app" / "packages" / "app-0.3.9.zip")
            shutil.copy2(dist_dir / "app-0.3.18.zip", install_root / "app" / "packages" / "app-0.3.18.zip")

            # Even without app/previous directory, run_tray rolls back to app-0.3.18.zip (not 0.3.9!),
            # releases the Windows mutex acquired by 0.4.0, and succeeds
            self.assertFalse((install_root / "app" / "previous").exists())
            saved_path = list(sys.path)
            saved_modules = dict(sys.modules)
            error_log = base / "tray-errors.log"
            try:
                with mock.patch.object(app_bootstrap, "error_log_path", return_value=error_log), \
                        mock.patch.object(app_bootstrap, "TRAY_MUTEX_NAME", f"Local\\AgentCompassTrayTest_{uuid.uuid4().hex}"):
                    rc = app_bootstrap.run_tray(install_root, argv=["AgentCompass.exe"])
            finally:
                sys.path[:] = saved_path
                sys.modules.clear()
                sys.modules.update(saved_modules)
            self.assertEqual(rc, 0)
            self.assertEqual(marker_file.read_text(encoding="utf-8"), "0.3.18")
            log_text = error_log.read_text(encoding="utf-8")
            self.assertIn("tray startup failed", log_text)
            self.assertIn("simulated 0.4.0 startup crash after mutex", log_text)
            # Verify the named mutex acquired by 0.4.0 was closed by _release_tray_mutex()
            test_handle = ctypes.windll.kernel32.CreateMutexW(None, False, mutex_name)
            last_err = ctypes.windll.kernel32.GetLastError()
            if test_handle:
                ctypes.windll.kernel32.CloseHandle(test_handle)
            self.assertNotEqual(last_err, 183)
            state = json.loads((install_root / "app" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["current_version"], "0.3.18")
            self.assertEqual(state["active_version"], "0.3.18")
            self.assertEqual(state["rolled_back_from"], "0.4.0")

            # Also verify run_cli rolls back when invoked as python-shim (`-I -X utf8 <script.py> --once`)
            # without losing sys.argv / script path during frozen_python.run() mutation (NEW-3)
            app_bundle.stage_installed_app(repo_v40, install_root, dist_dir=dist_dir, version="0.4.0")
            shutil.copy2(dist_dir / "app-0.3.18.zip", install_root / "app" / "packages" / "app-0.3.18.zip")
            script_arg = str(install_root / "app" / "current" / "skills" / "agent-compass" / "scripts" / "route_judge.py")
            saved_argv = list(sys.argv)
            try:
                sys.argv = ["agentcompass.exe", "-I", "-X", "utf8", script_arg, "--once"]
                cli_rc = app_bootstrap.run_cli(install_root, argv=None)
            finally:
                sys.argv = saved_argv
                sys.path[:] = saved_path
                sys.modules.clear()
                sys.modules.update(saved_modules)
            self.assertEqual(cli_rc, 0)
            self.assertEqual(cli_marker.read_text(encoding="utf-8"), "0.3.18:--once")

            # Verify setup/router_gate_hook.py under app/current is recognized as installed product (NEW-5)
            import importlib.util

            gate_src = (ROOT / "setup" / "router_gate_hook.py").read_text(encoding="utf-8")
            gate_dst = install_root / "app" / "current" / "setup" / "router_gate_hook.py"
            gate_dst.parent.mkdir(parents=True, exist_ok=True)
            gate_dst.write_text(gate_src, encoding="utf-8", newline="\n")
            spec = importlib.util.spec_from_file_location("_test_installed_gate_hook", gate_dst)
            gate_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(gate_mod)
            self.assertEqual(gate_mod.router_command(), "agentcompass")

    def _stage_previous_and_current(self, base: Path, current_files: dict[str, str]):
        """Install 0.4.0 as app/previous and 0.4.1 (with ``current_files`` overridden) as app/current."""
        import shutil

        app_bundle = self._load_packaging_module("app_bundle")
        repo_v1 = self._make_mini_repo(base, version="0.4.0")
        repo_v2 = self._make_mini_repo(base, version="0.4.1")
        for rel, content in current_files.items():
            (repo_v2 / rel).write_bytes(content.encode("utf-8"))
        dist_dir = base / "dist"
        install_root = dist_dir / "AgentCompass"
        install_root.mkdir(parents=True)
        app_bundle.stage_installed_app(repo_v1, install_root, dist_dir=dist_dir, version="0.4.0")
        prev_copy = base / "prev-0.4.0"
        shutil.copytree(install_root / "app" / "current", prev_copy)
        app_bundle.stage_installed_app(repo_v2, install_root, dist_dir=dist_dir, version="0.4.1")
        shutil.copytree(prev_copy, install_root / "app" / "previous")
        return install_root

    @staticmethod
    def _app_snapshot(install_root: Path):
        app = install_root / "app"
        current = sorted(
            (str(path.relative_to(app / "current")), path.read_bytes())
            for path in (app / "current").rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        )
        return (app / "state.json").read_bytes(), current, sorted(path.name for path in app.iterdir())

    def _run_tray_isolated(self, app_bootstrap, install_root: Path, argv: list[str], error_log: Path,
                           mutex_name: str | None = None, retry_env: str | None = None, after=None):
        import os
        import sys
        import uuid

        saved_path = list(sys.path)
        saved_modules = dict(sys.modules)
        sys.modules.pop("component_launch", None)
        # A unique mutex name: the real tray on this machine holds Local\AgentCompassTray.
        mutex_name = mutex_name or f"Local\\AgentCompassTrayTest_{uuid.uuid4().hex}"
        try:
            with mock.patch.dict(os.environ, {}, clear=False), \
                    mock.patch.object(app_bootstrap, "error_log_path", return_value=error_log), \
                    mock.patch.object(app_bootstrap, "TRAY_MUTEX_NAME", mutex_name):
                os.environ.pop("AGENTCOMPASS_ROLLBACK_RETRY", None)
                if retry_env is not None:
                    os.environ["AGENTCOMPASS_ROLLBACK_RETRY"] = retry_env
                result = app_bootstrap.run_tray(install_root, argv=argv)
                if after is not None:
                    after()
                return result
        finally:
            sys.path[:] = saved_path
            sys.modules.clear()
            sys.modules.update(saved_modules)
            if hasattr(sys, app_bootstrap.TRAY_STARTED_ATTR):
                delattr(sys, app_bootstrap.TRAY_STARTED_ATTR)

    def test_a_failing_component_never_rolls_back_or_rewrites_the_installed_app(self):
        from tempfile import TemporaryDirectory

        app_bootstrap = self._load_packaging_module("app_bootstrap")
        failing_component = (
            "FLAG = '--run-component'\n"
            "def dispatch(argv=None):\n"
            "    if argv and len(argv) >= 3 and argv[1] == FLAG:\n"
            "        raise OSError(145, 'The directory is not empty', 'C:/Temp/tmp2jlt612p')\n"
            "    return None\n"
        )
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            install_root = self._stage_previous_and_current(base, {"tray/component_launch.py": failing_component})
            before = self._app_snapshot(install_root)
            error_log = base / "tray-errors.log"

            rc = self._run_tray_isolated(
                app_bootstrap, install_root,
                ["AgentCompass.exe", "--run-component", "capacity_stats", "--once", "--quiet"], error_log,
            )

            self.assertEqual(rc, 1)
            self.assertEqual(self._app_snapshot(install_root), before)
            log_text = error_log.read_text(encoding="utf-8")
            self.assertIn("component capacity_stats failed", log_text)
            self.assertIn("Traceback (most recent call last)", log_text)
            self.assertIn("The directory is not empty", log_text)

            # A component that finds app/current unloadable (an update mid-swap) fails alone too; the tray's next start judges it.
            (install_root / "app" / "current" / "tray" / "app.pyw").write_bytes(b"# half-written by an update\n")
            before = self._app_snapshot(install_root)
            rc = self._run_tray_isolated(
                app_bootstrap, install_root, ["AgentCompass.exe", "--run-component", "usage_collect"], error_log,
            )
            self.assertEqual(rc, 1)
            self.assertEqual(self._app_snapshot(install_root), before)
            self.assertIn("component usage_collect failed", error_log.read_text(encoding="utf-8"))

    def test_a_tray_crash_after_the_icon_is_up_is_logged_but_not_rolled_back(self):
        from tempfile import TemporaryDirectory

        app_bootstrap = self._load_packaging_module("app_bootstrap")
        crashing_after_start = (
            "import sys\n"
            "sys._agentcompass_tray_started = True\n"
            "raise RuntimeError('runtime fault long after start')\n"
        )
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            install_root = self._stage_previous_and_current(base, {"tray/app.pyw": crashing_after_start})
            before = self._app_snapshot(install_root)
            error_log = base / "tray-errors.log"

            with self.assertRaisesRegex(RuntimeError, "runtime fault long after start"):
                self._run_tray_isolated(app_bootstrap, install_root, ["AgentCompass.exe"], error_log)

            self.assertEqual(self._app_snapshot(install_root), before)
            log_text = error_log.read_text(encoding="utf-8")
            self.assertIn("tray failed after it started", log_text)
            self.assertIn("runtime fault long after start", log_text)

    def test_a_tray_startup_crash_still_rolls_back_to_previous(self):
        import json
        from tempfile import TemporaryDirectory

        app_bootstrap = self._load_packaging_module("app_bootstrap")
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            install_root = self._stage_previous_and_current(
                base, {"tray/app.pyw": "raise RuntimeError('bad package at startup')\n"},
            )
            error_log = base / "tray-errors.log"

            rc = self._run_tray_isolated(app_bootstrap, install_root, ["AgentCompass.exe"], error_log)

            self.assertEqual(rc, 0)
            state = json.loads((install_root / "app" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["current_version"], "0.4.0")
            self.assertEqual(state["rolled_back_from"], "0.4.1")
            self.assertIn("bad package at startup", state["rollback_reason"])
            self.assertIn("tray startup failed", error_log.read_text(encoding="utf-8"))

    def test_a_second_launch_beside_a_running_tray_never_rolls_back(self):
        import ctypes
        import uuid
        from tempfile import TemporaryDirectory

        app_bootstrap = self._load_packaging_module("app_bootstrap")
        # Same shape as app.pyw main() when single_instance() fails: open the running tray's window, which raises.
        second_launch = "raise OSError(32, 'open_window failed beside the running tray')\n"
        mutex_name = f"Local\\AgentCompassTrayTest_{uuid.uuid4().hex}"
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.restype = ctypes.c_void_p
        running_tray = kernel.CreateMutexW(None, False, mutex_name)  # the first tray, still running
        self.addCleanup(kernel.CloseHandle, ctypes.c_void_p(running_tray))
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            install_root = self._stage_previous_and_current(base, {"tray/app.pyw": second_launch})
            before = self._app_snapshot(install_root)
            error_log = base / "tray-errors.log"

            rc = self._run_tray_isolated(app_bootstrap, install_root, ["AgentCompass.exe"], error_log, mutex_name=mutex_name)

            self.assertEqual(rc, 1)
            self.assertEqual(self._app_snapshot(install_root), before)
            self.assertIn("second tray launch failed", error_log.read_text(encoding="utf-8"))

            # Nor does a second launch that finds app\current mid-swap repair it under the running tray.
            (install_root / "app" / "current" / "tray" / "updates.py").write_bytes(b"# half-written by an update\n")
            before = self._app_snapshot(install_root)
            rc = self._run_tray_isolated(app_bootstrap, install_root, ["AgentCompass.exe"], error_log, mutex_name=mutex_name)
            self.assertEqual(rc, 1)
            self.assertEqual(self._app_snapshot(install_root), before)

    def test_the_rollback_retry_flag_is_consumed_so_a_later_restart_judges_its_own_package(self):
        import json
        import os
        from tempfile import TemporaryDirectory

        app_bootstrap = self._load_packaging_module("app_bootstrap")
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            install_root = self._stage_previous_and_current(base, {"tray/app.pyw": "# a healthy restored tray\n"})
            error_log = base / "tray-errors.log"
            seen = {}

            rc = self._run_tray_isolated(
                app_bootstrap, install_root, ["AgentCompass.exe"], error_log, retry_env="1",
                after=lambda: seen.update(flag=os.environ.get("AGENTCOMPASS_ROLLBACK_RETRY")),
            )
            self.assertEqual(rc, 0)
            self.assertIsNone(seen["flag"])  # children and later restarts of this tray do not inherit it

            # The flag still stops a second rollback in the process that got it.
            bad = self._stage_previous_and_current(base / "bad", {"tray/app.pyw": "raise RuntimeError('restored package is bad too')\n"})
            with self.assertRaisesRegex(RuntimeError, "restored package is bad too"):
                self._run_tray_isolated(app_bootstrap, bad, ["AgentCompass.exe"], error_log, retry_env="1")
            state = json.loads((bad / "app" / "state.json").read_text(encoding="utf-8"))
            self.assertNotIn("rolled_back_from", state)

    def test_the_error_log_rotates_instead_of_growing_without_bound(self):
        from tempfile import TemporaryDirectory

        app_bootstrap = self._load_packaging_module("app_bootstrap")
        with TemporaryDirectory() as tmp:
            error_log = Path(tmp) / "tray-errors.log"
            error_log.write_bytes(b"x" * (app_bootstrap.ERROR_LOG_MAX_BYTES + 1))
            with mock.patch.object(app_bootstrap, "error_log_path", return_value=error_log):
                try:
                    raise ValueError("logged")
                except ValueError:
                    app_bootstrap.log_exception("component demo failed", ["AgentCompass.exe", "--run-component", "demo"])
            self.assertTrue(error_log.with_name("tray-errors.log.1").is_file())
            self.assertIn("ValueError: logged", error_log.read_text(encoding="utf-8"))
            self.assertLess(error_log.stat().st_size, 10_000)


if __name__ == "__main__":
    unittest.main()
