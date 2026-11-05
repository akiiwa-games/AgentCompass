import hashlib
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import updates

REPO = Path(__file__).resolve().parents[1]


class _Body:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        if isinstance(self.payload, (bytes, bytearray)):
            return bytes(self.payload)
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _make_wheel_install(
    base: Path,
    version: str = "0.3.5",
    installer: str = "pip",
    direct_url: dict | None = None,
) -> tuple[Path, Path, Path]:
    """Create a fixture venv with site-packages, .dist-info, python.exe, and agentcompass.exe."""
    venv = base / f"venv-{installer}-{version}"
    scripts = venv / "Scripts"
    sp = venv / "Lib" / "site-packages"
    pkg = sp / "agentcompass"
    skill_scripts = pkg / "data" / "skills" / "agent-compass" / "scripts"
    setup_dir = pkg / "data" / "setup"
    dist_info = sp / f"agentcompass-{version}.dist-info"
    for folder in (scripts, pkg, skill_scripts, setup_dir, dist_info):
        folder.mkdir(parents=True, exist_ok=True)
    py_exe = scripts / "python.exe"
    py_exe.write_bytes(b"fake-python")
    cli_exe = scripts / "agentcompass.exe"
    cli_exe.write_bytes(b"MZ-launcher\r\n#!" + str(py_exe).encode("utf-8") + b"\r\nPK\x03\x04")
    (pkg / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    (skill_scripts / "route_judge.py").write_text("# route_judge\n", encoding="utf-8")
    (skill_scripts / "allocator.py").write_text("# allocator\n", encoding="utf-8")
    (setup_dir / "router_gate_hook.py").write_text("# gate hook\n", encoding="utf-8")
    (dist_info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: agentcompass\nVersion: {version}\n", encoding="utf-8")
    (dist_info / "INSTALLER").write_text(f"{installer}\n", encoding="utf-8")
    if direct_url is not None:
        (dist_info / "direct_url.json").write_text(json.dumps(direct_url), encoding="utf-8")
    return sp, py_exe, cli_exe


class ProductNoticeTests(unittest.TestCase):
    def test_a_checkout_with_git_does_not_change_the_joined_update_result(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").write_text("gitdir: somewhere\n", encoding="utf-8")

            def boom(*args, **kwargs):
                raise AssertionError("release check ran for a checkout")

            self.assertEqual(updates.product_notice(root, opener=boom), "")

    def test_a_packaged_tree_points_at_the_public_release(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "agentcompass"
            package.mkdir()
            (package / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
            if str(REPO) not in sys.path:
                sys.path.insert(0, str(REPO))
            seen = {}

            def opener(request, timeout=20):
                seen["url"] = request.full_url
                seen["agent"] = request.get_header("User-agent")
                return _Body({
                    "tag_name": "v9.9.9",
                    "html_url": "https://github.com/akiiwa-games/AgentCompass/releases/tag/v9.9.9",
                })

            text = updates.product_notice(root, opener=opener)
            self.assertIn("akiiwa-games/AgentCompass", seen["url"])
            self.assertEqual(seen["agent"], "agentcompass")
            self.assertIn("uv tool upgrade agentcompass", text)
            self.assertIn("v9.9.9", text)
            self.assertNotIn("agent-compass", text)


class InstallShapeAndVersionDisplayTests(unittest.TestCase):
    def test_detect_install_shape_for_git_installer_and_wheel(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)

            git_root = base / "git-checkout"
            (git_root / ".git").mkdir(parents=True)
            (git_root / "agentcompass").mkdir()
            (git_root / "agentcompass" / "__init__.py").write_text('__version__ = "0.3.8"\n', encoding="utf-8")
            self.assertEqual(updates.detect_install_shape(git_root, frozen=False), "git")

            installer_root = base / "installed-tray"
            installer_root.mkdir()
            (installer_root / "AgentCompass.exe").write_bytes(b"MZ")
            self.assertEqual(updates.detect_install_shape(installer_root, frozen=False), "installer")
            self.assertEqual(updates.detect_install_shape(installer_root, frozen=True), "installer")

            wheel_sp, _, _ = _make_wheel_install(base, version="0.3.8", installer="uv")
            self.assertEqual(updates.detect_install_shape(wheel_sp, frozen=False), "wheel")

    def test_version_display_flags_mismatch_from_path_skills_or_gate_hooks(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "home"
            home.mkdir()

            tray_root = base / "repo"
            (tray_root / ".git").mkdir(parents=True)
            (tray_root / "agentcompass").mkdir()
            (tray_root / "agentcompass" / "__init__.py").write_text('__version__ = "0.3.8"\n', encoding="utf-8")

            # Same install / no separate router -> single version
            same = updates.inspect_installs(root=tray_root, home=home, which_path=None, frozen=False)
            self.assertFalse(same["version_mismatch"])
            self.assertEqual(same["display_version"], "AgentCompass 0.3.8")

            # Separate router on PATH with same version -> shows both versions without mismatch flag
            sp_same, _, cli_same = _make_wheel_install(base, version="0.3.8", installer="pip")
            both_same = updates.inspect_installs(root=tray_root, home=home, which_path=cli_same, frozen=False)
            self.assertFalse(both_same["version_mismatch"])
            self.assertEqual(both_same["display_version"], "AgentCompass 0.3.8 · 命令行 0.3.8")

            # Separate router on PATH with older version -> flagged as mismatch
            sp_old, _, cli_old = _make_wheel_install(
                base,
                version="0.3.5",
                installer="uv",
                direct_url={"url": "file:///E:/PROJECTS/route_manager", "dir_info": {}},
            )
            mismatch_path = updates.inspect_installs(root=tray_root, home=home, which_path=cli_old, frozen=False)
            self.assertTrue(mismatch_path["version_mismatch"])
            self.assertEqual(mismatch_path["display_version"], "AgentCompass 0.3.8 · 命令行 0.3.5（版本不一致）")
            self.assertEqual(mismatch_path["routers"]["path"]["installer"], "uv")
            self.assertIsNotNone(mismatch_path["routers"]["path"]["direct_url"])

            # Mismatch via gate hooks (.claude/settings.json pointing to wheel's router_gate_hook.py)
            claude_dir = home / ".claude"
            claude_dir.mkdir(parents=True, exist_ok=True)
            hook_script = sp_old / "agentcompass" / "data" / "setup" / "router_gate_hook.py"
            (claude_dir / "settings.json").write_text(
                json.dumps({"hooks": {"PreToolUse": [{"command": f"{(sp_old.parents[1] / 'Scripts' / 'python.exe').as_posix()} -X utf8 {hook_script.as_posix()}"}]}}),
                encoding="utf-8",
            )
            mismatch_hook = updates.inspect_installs(root=tray_root, home=home, which_path=None, frozen=False)
            self.assertTrue(mismatch_hook["version_mismatch"])
            self.assertIn("0.3.5", mismatch_hook["display_version"])
            self.assertIn("版本不一致", mismatch_hook["display_version"])

            # Mismatch via router_priority.modules() (.codex/skills/agent-compass/scripts/route_judge.py)
            (claude_dir / "settings.json").unlink()
            fake_mod = mock.Mock(__file__=str(sp_old / "agentcompass" / "data" / "skills" / "agent-compass" / "scripts" / "allocator.py"))
            with mock.patch("router_priority.modules", return_value=(fake_mod, fake_mod)):
                mismatch_skills = updates.inspect_installs(root=tray_root, home=home, which_path=None, frozen=False)
            self.assertTrue(mismatch_skills["version_mismatch"])
            self.assertIn("0.3.5", mismatch_skills["display_version"])

    def test_release_check_caches_and_shows_update_available_or_silent_failure(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            state_dir = base / "state"
            home = base / "home"
            home.mkdir()
            tray_root = base / "repo"
            (tray_root / ".git").mkdir(parents=True)
            (tray_root / "agentcompass").mkdir()
            (tray_root / "agentcompass" / "__init__.py").write_text('__version__ = "0.3.8"\n', encoding="utf-8")

            calls = []

            def opener(request, timeout=20):
                calls.append(request.full_url)
                return _Body({
                    "tag_name": "v0.4.0",
                    "html_url": "https://github.com/akiiwa-games/AgentCompass/releases/tag/v0.4.0",
                    "assets": [
                        {
                            "name": "AgentCompass-tray-setup.exe",
                            "browser_download_url": "https://example.com/AgentCompass-tray-setup.exe",
                            "digest": "sha256:" + "a" * 64,
                            "size": 1024,
                        },
                        {
                            "name": "agentcompass-0.4.0-py3-none-any.whl",
                            "browser_download_url": "https://example.com/agentcompass-0.4.0-py3-none-any.whl",
                            "digest": "sha256:" + "b" * 64,
                            "size": 512,
                        },
                    ],
                })

            status = updates.check_product_release(
                force=True, root=tray_root, state_dir=state_dir, opener=opener, home=home, which_path=None
            )
            self.assertTrue(status["ok"])
            self.assertTrue(status["update_available"])
            self.assertEqual(status["update_label"], "可更新到 v0.4.0")
            self.assertIn("可更新到 v0.4.0", status["menu_label"])
            self.assertTrue((state_dir / "product-release.json").is_file())
            self.assertEqual(len(calls), 1)

            # Cached check within 6h does not hit network when opener=None and force=False
            cached_status = updates.check_product_release(
                force=False, root=tray_root, state_dir=state_dir, opener=None, home=home, which_path=None
            )
            self.assertTrue(cached_status["update_available"])
            self.assertEqual(cached_status["remote_tag"], "v0.4.0")

            # Network failure is silent apart from status_line
            def failing_opener(request, timeout=20):
                raise OSError("network unreachable")

            failed = updates.check_product_release(
                force=True, root=tray_root, state_dir=state_dir, opener=failing_opener, home=home, which_path=None
            )
            self.assertFalse(failed["ok"])
            self.assertIn("network unreachable", failed["status_line"])


class OneClickUpdateTests(unittest.TestCase):
    def test_installer_shape_verifies_sha256_and_refuses_mismatch_or_missing_digest(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            state_dir = base / "state"
            home = base / "home"
            home.mkdir()
            inst_root = base / "installed"
            inst_root.mkdir()
            (inst_root / "AgentCompass.exe").write_bytes(b"MZ-tray")
            (inst_root / "agentcompass").mkdir()
            (inst_root / "agentcompass" / "__init__.py").write_text('__version__ = "0.3.7"\n', encoding="utf-8")

            setup_bytes = b"MZ-real-installer-binary-payload"
            good_sha = hashlib.sha256(setup_bytes).hexdigest()
            wrong_sha = "0" * 64

            ran_commands = []

            def runner(args, cwd=None, env=None):
                ran_commands.append(list(args))
                return ""

            # 1. Missing digest -> refused before running anything
            with self.assertRaisesRegex(RuntimeError, "缺少 sha256"):
                updates.update_agentcompass(
                    root=inst_root,
                    home=home,
                    state_dir=state_dir,
                    which_path=None,
                    frozen=False,
                    release={
                        "tag_name": "v0.3.8",
                        "assets": [{"name": "AgentCompass-tray-setup.exe", "url": "https://example.com/setup.exe", "digest": ""}],
                    },
                    opener=lambda req, timeout=300: _Body(setup_bytes),
                    runner=runner,
                    restart_fn=lambda: "restarted",
                )
            self.assertEqual(ran_commands, [])

            # 2. Mismatched digest -> refused before running anything
            with self.assertRaisesRegex(RuntimeError, "校验不匹配"):
                updates.update_agentcompass(
                    root=inst_root,
                    home=home,
                    state_dir=state_dir,
                    which_path=None,
                    frozen=False,
                    release={
                        "tag_name": "v0.3.8",
                        "assets": [
                            {
                                "name": "AgentCompass-tray-setup.exe",
                                "url": "https://example.com/setup.exe",
                                "digest": f"sha256:{wrong_sha}",
                            }
                        ],
                    },
                    opener=lambda req, timeout=300: _Body(setup_bytes),
                    runner=runner,
                    restart_fn=lambda: "restarted",
                )
            self.assertEqual(ran_commands, [])

            # 3. Valid digest -> runs installer silently, then data --refresh, then restarts tray
            events = []
            summary = updates.update_agentcompass(
                progress=events.append,
                root=inst_root,
                home=home,
                state_dir=state_dir,
                which_path=None,
                frozen=False,
                release={
                    "tag_name": "v0.3.8",
                    "assets": [
                        {
                            "name": "AgentCompass-tray-setup.exe",
                            "url": "https://example.com/setup.exe",
                            "digest": f"sha256:{good_sha}",
                        }
                    ],
                },
                opener=lambda req, timeout=300: _Body(setup_bytes),
                runner=runner,
                restart_fn=lambda: "托盘已重启",
            )
            self.assertEqual(len(ran_commands), 2)
            self.assertEqual(ran_commands[0][1:], ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"])
            self.assertTrue(ran_commands[0][0].endswith("AgentCompass-tray-setup.exe"))
            self.assertEqual(ran_commands[1][-2:], ["data", "--refresh"])
            self.assertFalse(any("setup" in cmd for cmd in ran_commands))
            self.assertIn("托盘已重启", summary)
            self.assertTrue(any(e.get("kind") == "finish" for e in events))

    def test_wheel_shape_uses_uv_or_pip_according_to_dist_info_and_never_runs_setup(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            state_dir = base / "state"
            home = base / "home"
            home.mkdir()

            wheel_url = "https://github.com/akiiwa-games/AgentCompass/releases/download/v0.3.8/agentcompass-0.3.8-py3-none-any.whl"
            release = {
                "tag_name": "v0.3.8",
                "assets": [{"name": "agentcompass-0.3.8-py3-none-any.whl", "url": wheel_url}],
            }

            # Case A: uv installer
            sp_uv, _, cli_uv = _make_wheel_install(base, version="0.3.5", installer="uv")
            uv_cmds = []
            uv_cwds = []
            with mock.patch.object(updates, "_uv_executable", return_value="uv"):
                updates.update_agentcompass(
                    root=sp_uv,
                    home=home,
                    state_dir=state_dir,
                    which_path=cli_uv,
                    frozen=False,
                    release=release,
                    runner=lambda args, cwd=None, env=None: uv_cmds.append(list(args)) or uv_cwds.append(cwd) or "",
                    restart_fn=lambda: "restarted",
                )
            self.assertEqual(uv_cmds[0], ["uv", "tool", "install", "--force", wheel_url])
            # uv removes the tool venv; a cwd inside it locks the directory on Windows (os error 32)
            self.assertNotIn(sp_uv.parents[1].resolve(), [Path(uv_cwds[0]).resolve(), *Path(uv_cwds[0]).resolve().parents])
            self.assertEqual(uv_cmds[1], [str(cli_uv), "data", "--refresh"])
            self.assertFalse(any("setup" in cmd for cmd in uv_cmds))

            # Case B: pip installer
            sp_pip, py_pip, cli_pip = _make_wheel_install(base, version="0.3.5", installer="pip")
            pip_cmds = []
            pip_cwds = []
            updates.update_agentcompass(
                root=sp_pip,
                home=home,
                state_dir=state_dir,
                which_path=cli_pip,
                frozen=False,
                release=release,
                runner=lambda args, cwd=None, env=None: pip_cmds.append(list(args)) or pip_cwds.append(cwd) or "",
                restart_fn=lambda: "restarted",
            )
            self.assertEqual(pip_cmds[0], [str(py_pip), "-m", "pip", "install", "--upgrade", wheel_url])
            self.assertNotIn(sp_pip.parents[1].resolve(), [Path(pip_cwds[0]).resolve(), *Path(pip_cwds[0]).resolve().parents])
            self.assertEqual(pip_cmds[1], [str(cli_pip), "data", "--refresh"])
            self.assertFalse(any("setup" in cmd for cmd in pip_cmds))

    def test_git_shape_pulls_when_clean_and_refuses_dirty_tree_while_still_upgrading_separate_wheel(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            state_dir = base / "state"
            home = base / "home"
            home.mkdir()
            repo = base / "repo"
            (repo / ".git").mkdir(parents=True)
            (repo / "agentcompass").mkdir()
            (repo / "agentcompass" / "__init__.py").write_text('__version__ = "0.3.8"\n', encoding="utf-8")

            # 1. Dirty git checkout with no separate wheel -> refused, no pull, no data --refresh, no restart
            dirty_cmds = []

            def dirty_runner(args, cwd=None, env=None):
                dirty_cmds.append(list(args))
                if args[:2] == ["git", "status"]:
                    return " M tray/updates.py"
                return ""

            restarted = []
            summary = updates.update_agentcompass(
                root=repo,
                home=home,
                state_dir=state_dir,
                which_path=None,
                frozen=False,
                runner=dirty_runner,
                restart_fn=lambda: restarted.append(True) or "restarted",
            )
            self.assertIn("有本地改动", summary)
            self.assertEqual(dirty_cmds, [["git", "status", "--porcelain", "--untracked-files=no"]])
            self.assertEqual(restarted, [])

            # 2. Clean git checkout tracking origin/main + separate pip wheel -> runs git fetch + git pull --ff-only AND pip upgrade
            sp_pip, py_pip, cli_pip = _make_wheel_install(base, version="0.3.5", installer="pip")
            wheel_url = "https://example.com/agentcompass-0.3.8-py3-none-any.whl"
            clean_cmds = []

            def clean_runner(args, cwd=None, env=None):
                clean_cmds.append(list(args))
                if args[:2] == ["git", "status"]:
                    return ""
                if args == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
                    return "main"
                if args[:3] == ["git", "rev-parse", "--abbrev-ref"]:
                    return "origin/main"
                return ""

            summary2 = updates.update_agentcompass(
                root=repo,
                home=home,
                state_dir=state_dir,
                which_path=cli_pip,
                frozen=False,
                release={
                    "tag_name": "v0.3.8",
                    "assets": [{"name": "agentcompass-0.3.8-py3-none-any.whl", "url": wheel_url}],
                },
                runner=clean_runner,
                restart_fn=lambda: restarted.append(True) or "托盘已重启",
            )
            self.assertIn(["git", "fetch", "origin"], clean_cmds)
            self.assertIn(["git", "pull", "--ff-only"], clean_cmds)
            self.assertIn([str(py_pip), "-m", "pip", "install", "--upgrade", wheel_url], clean_cmds)
            self.assertIn([str(cli_pip), "data", "--refresh"], clean_cmds)
            self.assertEqual(len(restarted), 1)
            self.assertIn("git pull --ff-only", summary2)
            self.assertIn("pip", summary2)


if __name__ == "__main__":
    unittest.main()
