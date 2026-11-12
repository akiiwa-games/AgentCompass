import hashlib
import json
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import pytest

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


class ReleaseNoticeCacheTests(unittest.TestCase):
    """The integrations check shares the 6 h release cache and survives a rate-limited GitHub read."""

    def _root(self, directory: str, version: str = "0.4.3") -> Path:
        root = Path(directory) / "app"
        (root / "agentcompass").mkdir(parents=True)
        (root / "agentcompass" / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
        if str(REPO) not in sys.path:
            sys.path.insert(0, str(REPO))
        return root

    def _cache(self, state: Path, tag: str, age_seconds: float) -> None:
        state.mkdir(parents=True, exist_ok=True)
        (state / updates.RELEASE_CACHE_NAME).write_text(json.dumps({
            "checked_at": time.time() - age_seconds, "tag_name": tag, "version": tag.lstrip("v"),
            "html_url": "https://github.com/akiiwa-games/AgentCompass/releases/tag/" + tag, "assets": [],
        }), encoding="utf-8")

    def test_a_fresh_cache_answers_without_calling_github(self):
        with TemporaryDirectory() as directory:
            root = self._root(directory)
            state = Path(directory) / "state"
            self._cache(state, "v0.4.3", age_seconds=60)
            with mock.patch.object(updates.urllib.request, "urlopen",
                                   side_effect=AssertionError("GitHub was called despite a fresh cache")):
                notice = updates.release_notice(root, state_dir=state)
            self.assertEqual(notice.status, "current")
            self.assertIn("v0.4.3", notice)

    def test_a_rate_limited_read_falls_back_to_the_cached_release(self):
        with TemporaryDirectory() as directory:
            root = self._root(directory)
            state = Path(directory) / "state"
            self._cache(state, "v0.4.4", age_seconds=updates.RELEASE_CHECK_INTERVAL_SECONDS + 60)
            limited = OSError("HTTP Error 403: rate limit exceeded")
            with mock.patch.object(updates.urllib.request, "urlopen", side_effect=limited):
                notice = updates.release_notice(root, state_dir=state)
            self.assertEqual(notice.status, "newer")
            self.assertIn("v0.4.4", notice)
            self.assertIn("rate limit exceeded", notice)
            self.assertNotIn(".git", notice)

    def test_no_cache_and_no_github_is_a_failure(self):
        with TemporaryDirectory() as directory:
            root = self._root(directory)
            with mock.patch.object(updates.urllib.request, "urlopen", side_effect=OSError("offline")):
                notice = updates.release_notice(root, state_dir=Path(directory) / "state")
            self.assertEqual(notice.status, "failed")
            self.assertIn("offline", notice)

    def test_the_installer_build_points_at_the_tray_update_button(self):
        with TemporaryDirectory() as directory:
            root = self._root(directory, version="0.4.2")
            state = Path(directory) / "state"
            self._cache(state, "v0.4.3", age_seconds=60)
            with mock.patch.object(updates.component_launch, "frozen", return_value=True):
                notice = updates.release_notice(root, state_dir=state)
            self.assertEqual(notice.status, "newer")
            self.assertIn("更新 AgentCompass", notice)
            self.assertNotIn("uv tool upgrade", notice)
            self.assertNotIn(".git", notice)


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


def _make_code_package_zip(
    dest_zip: Path,
    *,
    version: str = "0.4.0",
    runtime_fp: str = "a" * 64,
    min_runtime_version: str = "0.3.18",
    extra_files: dict[str, str] | None = None,
    corrupt_manifest_hash_for: str | None = None,
) -> tuple[bytes, str]:
    import io
    import zipfile

    files = {
        "agentcompass/__init__.py": f'__version__ = "{version}"\n',
        "agentcompass/cli.py": "def main(argv=None):\n    return 0\n",
        "tray/app.pyw": f"# tray version {version}\n",
        "tray/component_launch.py": "def dispatch():\n    return False\n",
        "tray/easytier_supervisor.ps1": "Write-Output 'ok'\n",
        "skills/agent-compass/SKILL.md": "---\nname: agent-compass\n---\n",
        "setup/onboard.py": "def setup_main(argv=None):\n    return 0\n",
        "setup/setup_agent_compass.py": "print('setup')\n",
    }
    if extra_files:
        files.update(extra_files)
    file_entries = {}
    for rel, content in sorted(files.items()):
        raw = content.encode("utf-8")
        sha = hashlib.sha256(raw).hexdigest()
        if corrupt_manifest_hash_for == rel:
            sha = "f" * 64
        file_entries[rel] = {"size": len(raw), "sha256": sha}
    app_manifest = {
        "schema_version": 1,
        "version": version,
        "required_runtime_fingerprint": runtime_fp,
        "min_runtime_version": min_runtime_version,
        "file_count": len(file_entries),
        "files": file_entries,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel, content in sorted(files.items()):
            zf.writestr(rel, content.encode("utf-8"))
        zf.writestr("app-manifest.json", (json.dumps(app_manifest, indent=2) + "\n").encode("utf-8"))
    payload = buf.getvalue()
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    dest_zip.write_bytes(payload)
    return payload, hashlib.sha256(payload).hexdigest()


def _make_modular_installer_dir(
    base: Path,
    *,
    version: str = "0.3.18",
    runtime_fp: str = "a" * 64,
    runtime_version: str = "0.3.18",
    iss_sha256: str = "",
) -> Path:
    import zipfile

    inst_root = base / f"AgentCompass-{version}"
    (inst_root / "cli").mkdir(parents=True, exist_ok=True)
    (inst_root / "AgentCompass.exe").write_bytes(b"MZ-tray-boot")
    (inst_root / "cli" / "agentcompass.exe").write_bytes(b"MZ-cli-boot")
    rt_payload = {
        "schema_version": 1,
        "runtime_fingerprint": runtime_fp,
        "runtime_version": runtime_version,
    }
    if iss_sha256:
        rt_payload["iss_sha256"] = iss_sha256
    (inst_root / "runtime-info.json").write_text(
        json.dumps(rt_payload, indent=2) + "\n",
        encoding="utf-8",
    )
    pkg_dir = inst_root / "app" / "packages"
    current_dir = inst_root / "app" / "current"
    current_dir.mkdir(parents=True, exist_ok=True)
    zip_path = pkg_dir / f"app-{version}.zip"
    _make_code_package_zip(
        zip_path,
        version=version,
        runtime_fp=runtime_fp,
        min_runtime_version=runtime_version,
    )
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(current_dir)
    return inst_root


@pytest.mark.slow
class IncrementalCodePackageUpdateTests(unittest.TestCase):
    def test_non_frozen_installer_version_reads_app_current_without_false_0_0_0_mismatch(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "home"
            home.mkdir()
            inst_root = _make_modular_installer_dir(base, version="0.4.0")
            cli_exe = inst_root / "cli" / "agentcompass.exe"

            # Direct version lookup from non-frozen process reads 0.4.0 instead of 0.0.0
            self.assertEqual(updates.local_version(inst_root), "0.4.0")
            self.assertEqual(updates._installer_version(inst_root), "0.4.0")

            # Inspecting when both tray root and PATH cli point to the installer reports no version mismatch
            info = updates.inspect_installs(
                root=inst_root / "app" / "current",
                home=home,
                which_path=cli_exe,
                frozen=False,
            )
            self.assertEqual(info["tray"]["shape"], "installer")
            self.assertEqual(info["tray"]["version"], "0.4.0")
            self.assertFalse(info["version_mismatch"])
            self.assertEqual(info["display_version"], "AgentCompass 0.4.0")

    def test_matching_runtime_fingerprint_downloads_only_code_package_and_retains_previous(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            state_dir = base / "state"
            home = base / "home"
            home.mkdir()
            runtime_fp = "b" * 64
            inst_root = _make_modular_installer_dir(
                base,
                version="0.3.18",
                runtime_fp=runtime_fp,
                runtime_version="0.3.18",
            )
            zip_file = base / "release" / "app-0.4.0.zip"
            zip_bytes, zip_sha = _make_code_package_zip(
                zip_file,
                version="0.4.0",
                runtime_fp=runtime_fp,
                min_runtime_version="0.3.18",
            )
            setup_bytes = b"MZ-full-installer-should-not-be-downloaded"
            setup_sha = hashlib.sha256(setup_bytes).hexdigest()
            manifest_payload = {
                "schema_version": 1,
                "version": "0.4.0",
                "runtime_fingerprint": runtime_fp,
                "app_package": {
                    "name": "app-0.4.0.zip",
                    "size": len(zip_bytes),
                    "sha256": zip_sha,
                    "required_runtime_fingerprint": runtime_fp,
                    "min_runtime_version": "0.3.18",
                },
                "installer": {
                    "name": "AgentCompass-tray-setup.exe",
                    "size": len(setup_bytes),
                    "sha256": setup_sha,
                    "runtime_fingerprint": runtime_fp,
                },
            }
            manifest_bytes = json.dumps(manifest_payload).encode("utf-8")
            manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()

            downloaded_urls = []
            ran_commands = []

            def opener(request, timeout=300):
                url = request.full_url
                downloaded_urls.append(url)
                if url.endswith("manifest.json"):
                    return _Body(manifest_bytes)
                if url.endswith("app-0.4.0.zip"):
                    return _Body(zip_bytes)
                raise AssertionError(f"Unexpected download URL: {url}")

            summary = updates.update_agentcompass(
                root=inst_root,
                home=home,
                state_dir=state_dir,
                which_path=None,
                frozen=False,
                release={
                    "tag_name": "v0.4.0",
                    "assets": [
                        {
                            "name": "manifest.json",
                            "url": "https://example.com/manifest.json",
                            "digest": f"sha256:{manifest_sha}",
                        },
                        {
                            "name": "app-0.4.0.zip",
                            "url": "https://example.com/app-0.4.0.zip",
                            "digest": f"sha256:{zip_sha}",
                        },
                        {
                            "name": "AgentCompass-tray-setup.exe",
                            "url": "https://example.com/AgentCompass-tray-setup.exe",
                            "digest": f"sha256:{setup_sha}",
                        },
                    ],
                },
                opener=opener,
                runner=lambda args, cwd=None, env=None: ran_commands.append(list(args)) or "",
                restart_fn=lambda: "托盘已重启",
            )
            self.assertIn("已完成代码包增量更新", summary)
            self.assertEqual(
                downloaded_urls,
                ["https://example.com/manifest.json", "https://example.com/app-0.4.0.zip"],
            )
            self.assertFalse(any("AgentCompass-tray-setup.exe" in cmd[0] for cmd in ran_commands))
            self.assertEqual(len(ran_commands), 1)
            self.assertEqual(ran_commands[0][-2:], ["data", "--refresh"])
            self.assertEqual(updates.local_version(inst_root), "0.4.0")
            self.assertEqual(updates.local_version(inst_root / "app" / "previous"), "0.3.18")
            self.assertTrue((inst_root / "app" / "packages" / "app-0.4.0.zip").is_file())

    def test_mismatched_runtime_fingerprint_or_legacy_install_falls_back_to_full_installer(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            state_dir = base / "state"
            home = base / "home"
            home.mkdir()
            old_fp = "1" * 64
            new_fp = "2" * 64
            inst_root = _make_modular_installer_dir(base, version="0.3.18", runtime_fp=old_fp)
            zip_file = base / "release" / "app-0.4.0.zip"
            zip_bytes, zip_sha = _make_code_package_zip(zip_file, version="0.4.0", runtime_fp=new_fp)
            setup_bytes = b"MZ-new-runtime-full-installer"
            setup_sha = hashlib.sha256(setup_bytes).hexdigest()
            manifest_payload = {
                "schema_version": 1,
                "version": "0.4.0",
                "runtime_fingerprint": new_fp,
                "app_package": {
                    "name": "app-0.4.0.zip",
                    "size": len(zip_bytes),
                    "sha256": zip_sha,
                    "required_runtime_fingerprint": new_fp,
                },
                "installer": {
                    "name": "AgentCompass-tray-setup.exe",
                    "size": len(setup_bytes),
                    "sha256": setup_sha,
                    "runtime_fingerprint": new_fp,
                },
            }
            manifest_bytes = json.dumps(manifest_payload).encode("utf-8")

            downloaded_urls = []
            ran_commands = []

            def opener(request, timeout=300):
                url = request.full_url
                downloaded_urls.append(url)
                if url.endswith("manifest.json"):
                    return _Body(manifest_bytes)
                if url.endswith("AgentCompass-tray-setup.exe"):
                    return _Body(setup_bytes)
                raise AssertionError(f"Should not download code package when runtime differs: {url}")

            summary = updates.update_agentcompass(
                root=inst_root,
                home=home,
                state_dir=state_dir,
                which_path=None,
                frozen=False,
                release={
                    "tag_name": "v0.4.0",
                    "assets": [
                        {"name": "manifest.json", "url": "https://example.com/manifest.json"},
                        {
                            "name": "app-0.4.0.zip",
                            "url": "https://example.com/app-0.4.0.zip",
                            "digest": f"sha256:{zip_sha}",
                        },
                        {
                            "name": "AgentCompass-tray-setup.exe",
                            "url": "https://example.com/AgentCompass-tray-setup.exe",
                            "digest": f"sha256:{setup_sha}",
                        },
                    ],
                },
                opener=opener,
                runner=lambda args, cwd=None, env=None: ran_commands.append(list(args)) or "",
                restart_fn=lambda: "托盘已重启",
            )
            self.assertIn("已完成安装包静默更新", summary)
            self.assertIn("https://example.com/AgentCompass-tray-setup.exe", downloaded_urls)
            self.assertNotIn("https://example.com/app-0.4.0.zip", downloaded_urls)
            self.assertEqual(ran_commands[0][1:], ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"])

            # Legacy <=0.3.18 client (no runtime-info.json at all) also upgrades to 0.4.0 via AgentCompass-tray-setup.exe
            legacy_root = base / "legacy-0.3.18"
            legacy_root.mkdir()
            (legacy_root / "AgentCompass.exe").write_bytes(b"MZ-legacy")
            legacy_cmds = []
            summary_legacy = updates.update_agentcompass(
                root=legacy_root,
                home=home,
                state_dir=state_dir,
                which_path=None,
                frozen=False,
                release={
                    "tag_name": "v0.4.0",
                    "assets": [
                        {"name": "manifest.json", "url": "https://example.com/manifest.json"},
                        {
                            "name": "app-0.4.0.zip",
                            "url": "https://example.com/app-0.4.0.zip",
                            "digest": f"sha256:{zip_sha}",
                        },
                        {
                            "name": "AgentCompass-tray-setup.exe",
                            "url": "https://example.com/AgentCompass-tray-setup.exe",
                            "digest": f"sha256:{setup_sha}",
                        },
                    ],
                },
                opener=opener,
                runner=lambda args, cwd=None, env=None: legacy_cmds.append(list(args)) or "",
                restart_fn=lambda: "托盘已重启",
            )
            self.assertIn("已完成安装包静默更新", summary_legacy)
            self.assertTrue(legacy_cmds[0][0].endswith("AgentCompass-tray-setup.exe"))

            # NEW-2: When iss_sha256 differs between local runtime-info.json and remote manifest,
            # can_use_code_package returns False so .iss changes always trigger the full installer.
            inst_iss = _make_modular_installer_dir(
                base,
                version="0.4.0",
                runtime_fp=old_fp,
                iss_sha256="1" * 64,
            )
            ok_iss, reason_iss = updates.can_use_code_package(
                updates.read_local_runtime_info(inst_iss),
                {
                    "runtime_fingerprint": old_fp,
                    "iss_sha256": "2" * 64,
                    "app_package": {
                        "name": "app-0.4.1.zip",
                        "sha256": zip_sha,
                        "required_runtime_fingerprint": old_fp,
                    },
                },
            )
            self.assertFalse(ok_iss)
            self.assertTrue(reason_iss.startswith("iss_sha256_mismatch:"))

    def test_code_package_sha256_mismatch_or_corrupt_replacement_refuses_and_rolls_back(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            state_dir = base / "state"
            home = base / "home"
            home.mkdir()
            runtime_fp = "c" * 64
            inst_root = _make_modular_installer_dir(base, version="0.3.18", runtime_fp=runtime_fp)
            zip_file = base / "release" / "app-0.4.0.zip"
            zip_bytes, zip_sha = _make_code_package_zip(zip_file, version="0.4.0", runtime_fp=runtime_fp)
            wrong_sha = "9" * 64

            # 1. Outer zip SHA-256 mismatch is refused before touching app/current
            with self.assertRaisesRegex(RuntimeError, "校验不匹配"):
                updates.update_agentcompass(
                    root=inst_root,
                    home=home,
                    state_dir=state_dir,
                    which_path=None,
                    frozen=False,
                    release={
                        "tag_name": "v0.4.0",
                        "manifest": {
                            "version": "0.4.0",
                            "runtime_fingerprint": runtime_fp,
                            "app_package": {
                                "name": "app-0.4.0.zip",
                                "size": len(zip_bytes),
                                "sha256": wrong_sha,
                                "required_runtime_fingerprint": runtime_fp,
                            },
                        },
                        "assets": [
                            {
                                "name": "app-0.4.0.zip",
                                "url": "https://example.com/app-0.4.0.zip",
                                "digest": f"sha256:{wrong_sha}",
                            }
                        ],
                    },
                    opener=lambda req, timeout=300: _Body(zip_bytes),
                    runner=lambda args, cwd=None, env=None: "",
                    restart_fn=lambda: "restarted",
                )
            self.assertEqual(updates.local_version(inst_root), "0.3.18")

            # 2. Failure during atomic swap after moving current -> previous rolls back current to 0.3.18
            orig_validate = updates._validate_extracted_app_dir
            calls = {"count": 0}

            def fail_on_second_validate(app_dir, **kwargs):
                calls["count"] += 1
                if calls["count"] == 2:
                    raise RuntimeError("simulated post-swap validation failure")
                return orig_validate(app_dir, **kwargs)

            with mock.patch.object(updates, "_validate_extracted_app_dir", side_effect=fail_on_second_validate):
                with self.assertRaisesRegex(RuntimeError, "simulated post-swap validation failure"):
                    updates._apply_code_package(
                        inst_root,
                        zip_file,
                        expected_runtime_fingerprint=runtime_fp,
                    )
            self.assertEqual(updates.local_version(inst_root), "0.3.18")
            self.assertTrue((inst_root / "app" / "current" / "tray" / "app.pyw").is_file())

            # 3. When Windows holds a handle on app/current (PermissionError on directory rename),
            # _apply_code_package falls back to in-place sync while preserving app/previous.
            orig_replace = Path.replace

            def locked_current_replace(path_self: Path, target: Path):
                if path_self.name == "current":
                    raise PermissionError(13, "The process cannot access the file because it is being used by another process")
                return orig_replace(path_self, target)

            with mock.patch.object(Path, "replace", autospec=True, side_effect=locked_current_replace):
                applied = updates._apply_code_package(
                    inst_root,
                    zip_file,
                    expected_runtime_fingerprint=runtime_fp,
                )
            self.assertEqual(applied["version"], "0.4.0")
            self.assertEqual(updates.local_version(inst_root), "0.4.0")
            self.assertEqual(updates.local_version(inst_root / "app" / "previous"), "0.3.18")

    def test_unverified_manifest_cannot_authorize_code_package_and_safe_cwd_escapes_app_current(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            state_dir = base / "state"
            home = base / "home"
            home.mkdir()
            runtime_fp = "d" * 64
            inst_root = _make_modular_installer_dir(base, version="0.3.18", runtime_fp=runtime_fp)
            zip_file = base / "release" / "app-0.4.0.zip"
            zip_bytes, zip_sha = _make_code_package_zip(zip_file, version="0.4.0", runtime_fp=runtime_fp)
            manifest_payload = {
                "schema_version": 1,
                "version": "0.4.0",
                "runtime_fingerprint": runtime_fp,
                "app_package": {
                    "name": "app-0.4.0.zip",
                    "size": len(zip_bytes),
                    "sha256": zip_sha,
                    "required_runtime_fingerprint": runtime_fp,
                },
            }
            manifest_bytes = json.dumps(manifest_payload).encode("utf-8")

            # 1. Tampered manifest.json with mismatched digest raises RuntimeError
            with self.assertRaisesRegex(RuntimeError, "manifest.json sha256"):
                updates._load_release_manifest(
                    {
                        "assets": [
                            {
                                "name": "manifest.json",
                                "url": "https://example.com/manifest.json",
                                "digest": "sha256:" + ("0" * 64),
                            }
                        ]
                    },
                    opener=lambda req, timeout=60: _Body(manifest_bytes),
                )

            # 2. When manifest.json asset has NO digest and app-0.4.0.zip also has NO digest,
            # update_agentcompass refuses to trust manifest_sha as fallback_sha256.
            def opener(req, timeout=300):
                if req.full_url.endswith("manifest.json"):
                    return _Body(manifest_bytes)
                return _Body(zip_bytes)

            with self.assertRaisesRegex(RuntimeError, "缺少 sha256"):
                updates.update_agentcompass(
                    root=inst_root,
                    home=home,
                    state_dir=state_dir,
                    which_path=None,
                    frozen=False,
                    release={
                        "tag_name": "v0.4.0",
                        "assets": [
                            {"name": "manifest.json", "url": "https://example.com/manifest.json"},
                            {"name": "app-0.4.0.zip", "url": "https://example.com/app-0.4.0.zip"},
                        ],
                    },
                    opener=opener,
                    runner=lambda args, cwd=None, env=None: "",
                    restart_fn=lambda: "restarted",
                )

            # 3. services.safe_cwd never returns a path inside <inst_root>/app/current
            import services

            escaped = services.safe_cwd(inst_root / "app" / "current" / "tray")
            self.assertEqual(escaped.resolve(), inst_root.resolve())

            # 4. local_version reads active_version in state.json and runtime_version in runtime-info.json
            import shutil

            shutil.rmtree(inst_root / "app" / "current")
            (inst_root / "app" / "state.json").write_text(
                json.dumps({"active_version": "0.3.18"}), encoding="utf-8"
            )
            self.assertEqual(updates.local_version(inst_root), "0.3.18")
            (inst_root / "app" / "state.json").unlink()
            self.assertEqual(updates.local_version(inst_root), "0.3.18")

    def test_live_frozen_installer_failure_is_reported_and_legacy_running_status_reconciles(self):
        import deploy
        import os
        import update_status

        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            state_dir = base / "state"
            home = base / "home"
            home.mkdir()
            inst_root = _make_modular_installer_dir(base, version="0.3.18", runtime_fp="1" * 64)
            setup_bytes = b"MZ-failing-full-installer"
            setup_sha = hashlib.sha256(setup_bytes).hexdigest()

            # 1. deploy.write_restart_marker respects custom state_dir (NEW-4)
            deploy.write_restart_marker(version="0.4.0", state_dir=state_dir)
            self.assertTrue((state_dir / "tray-restarted.json").is_file())
            consumed = deploy.consume_restart_marker(state_dir=state_dir)
            self.assertIsNotNone(consumed)
            self.assertEqual(consumed["version"], "0.4.0")

            # 2. _spawn_installer_watcher is gone; when frozen live tray launches AgentCompass-tray-setup.exe
            # and the installer exits non-zero, update_agentcompass raises RuntimeError and records failure (R3-1/R3-2)
            self.assertFalse(hasattr(updates, "_spawn_installer_watcher"))
            events = []
            fake_proc = mock.Mock()
            fake_proc.pid = 999999
            fake_proc.wait.return_value = 5
            with (
                mock.patch.object(sys, "executable", str(inst_root / "AgentCompass.exe")),
                mock.patch.object(updates.subprocess, "Popen", return_value=fake_proc),
            ):
                with self.assertRaisesRegex(RuntimeError, "退出码 5"):
                    updates.update_agentcompass(
                        progress=events.append,
                        root=inst_root,
                        home=home,
                        state_dir=state_dir,
                        which_path=None,
                        frozen=True,
                        release={
                            "tag_name": "v0.4.0",
                            "assets": [
                                {
                                    "name": "AgentCompass-tray-setup.exe",
                                    "url": "https://example.com/AgentCompass-tray-setup.exe",
                                    "digest": f"sha256:{setup_sha}",
                                }
                            ],
                        },
                        opener=lambda req, timeout=300: _Body(setup_bytes),
                        runner=None,
                        restart_fn=lambda: "restarted",
                    )
            self.assertFalse(any("已完成安装包静默更新" in str(e.get("message") or "") for e in events))
            self.assertTrue(any("更新失败" in str(e.get("message") or "") for e in events))
            self.assertFalse((state_dir / "tray-restarted.json").is_file())

            # 3. reconcile_status works without psutil, never finalizes while installer PID is alive,
            # judges pending_installer by target_version == installed_version, and marks records without
            # pending_installer as "已结束，结果未知" rather than fabricating success (R3-1/R3-2)
            state_dir.mkdir(parents=True, exist_ok=True)
            with mock.patch.dict(sys.modules, {"psutil": None}):
                # 3a. While installer process is still alive (pid=os.getpid()), never finalize early even if version matches
                live_pending = {
                    "running": True,
                    "started": 1700000000.0,
                    "owner_pid": 999998,
                    "title": "AgentCompass",
                    "targets": ["AgentCompass"],
                    "pending_installer": {
                        "pid": os.getpid(),
                        "target_version": "0.4.0",
                        "started_at": 1700000000.0,
                        "install_root": str(inst_root),
                        "tag_label": "v0.4.0",
                    },
                    "results": {},
                    "events": [],
                }
                (state_dir / "update-status.json").write_text(json.dumps(live_pending), encoding="utf-8")
                still_running = update_status.reconcile_status(state_dir, current_version="0.4.0")
                self.assertTrue(still_running["running"])

                # 3b. Once installer PID has exited and installed version == target_version -> success
                done_pending = dict(live_pending)
                done_pending["pending_installer"] = dict(live_pending["pending_installer"], pid=999997)
                (state_dir / "update-status.json").write_text(json.dumps(done_pending), encoding="utf-8")
                reconciled_ok = update_status.reconcile_status(state_dir, current_version="0.4.0")
                self.assertFalse(reconciled_ok["running"])
                self.assertIn("已完成安装包静默更新", reconciled_ok["results"]["AgentCompass"])

                # 3c. Once installer PID has exited and installed version != target_version -> failure
                (state_dir / "update-status.json").write_text(json.dumps(done_pending), encoding="utf-8")
                reconciled_fail = update_status.reconcile_status(state_dir)  # inst_root is at 0.3.18 != 0.4.0
                self.assertFalse(reconciled_fail["running"])
                self.assertIn("更新失败", reconciled_fail["results"]["AgentCompass"])

                # 3d. Record without pending_installer (e.g. <=0.3.18 upgrade or interrupted check) -> "已结束，结果未知", never fabricated success
                stale_status = {
                    "running": True,
                    "started": 1700000000.0,
                    "owner_pid": 999998,
                    "title": "AgentCompass",
                    "targets": ["AgentCompass"],
                    "results": {},
                    "events": ["[15:00:00] AgentCompass · 正在运行安装包 AgentCompass-tray-setup.exe…"],
                }
                (state_dir / "update-status.json").write_text(json.dumps(stale_status), encoding="utf-8")
                reconciled_unknown = update_status.reconcile_status(state_dir, current_version="0.4.0")
                self.assertFalse(reconciled_unknown["running"])
                self.assertEqual(reconciled_unknown["results"]["AgentCompass"], "已结束，结果未知")

    def test_resident_component_processes_scopes_by_proc_cwd_and_spares_visible_router_window(self):
        import deploy

        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            our_root = base / "our-install"
            other_root = base / "other-checkout"
            (our_root / "tray").mkdir(parents=True)
            (other_root / "tray").mkdir(parents=True)
            (our_root / "tray" / "router_window.py").write_text("# ours\n", encoding="utf-8")
            (our_root / "tray" / "usage_float_host.py").write_text("# ours\n", encoding="utf-8")
            (other_root / "tray" / "router_window.py").write_text("# other\n", encoding="utf-8")

            other_proc = mock.Mock()
            other_proc.pid = 41001
            other_proc.info = {"pid": 41001, "name": "pythonw.exe", "cmdline": ["pythonw.exe", "tray/router_window.py"]}
            other_proc.cwd.return_value = str(other_root)

            our_window_proc = mock.Mock()
            our_window_proc.pid = 41002
            our_window_proc.info = {"pid": 41002, "name": "pythonw.exe", "cmdline": ["pythonw.exe", "tray/router_window.py"]}
            our_window_proc.cwd.return_value = str(our_root)

            our_float_proc = mock.Mock()
            our_float_proc.pid = 41003
            our_float_proc.info = {
                "pid": 41003,
                "name": "pythonw.exe",
                "cmdline": ["pythonw.exe", "tray/usage_float_host.py"],
            }
            our_float_proc.cwd.return_value = str(our_root)

            with (
                mock.patch.object(deploy, "TRAY_DIR", our_root / "tray"),
                mock.patch.object(
                    deploy.psutil,
                    "process_iter",
                    return_value=[other_proc, our_window_proc, our_float_proc],
                ),
            ):
                # With include_visible_windows=False, only usage_float_host is returned, sparing visible router_window
                # and never matching other_proc whose proc.cwd() is other_root (NEW-6)
                non_visible_procs = deploy.resident_component_processes(include_visible_windows=False)
                self.assertEqual([p.pid for p in non_visible_procs], [41003])

                # With include_visible_windows=True, our_window_proc is included, but other_proc is still excluded
                all_our_procs = deploy.resident_component_processes(include_visible_windows=True)
                self.assertEqual([p.pid for p in all_our_procs], [41002, 41003])

    @pytest.mark.slow
    def test_superseded_or_closing_router_window_immediately_releases_show_event(self):
        import router_window
        import panel_plugins
        import panel_widgets
        import tk_test_support

        root = tk_test_support.shared_tk_root(self)
        root.withdraw()
        patches = [
            mock.patch.object(router_window.Panel, "run"),
            mock.patch.object(router_window.Panel, "prefill"),
            mock.patch.object(router_window.theme, "set_window_icon"),
            mock.patch.object(panel_plugins.plugins, "load", return_value=panel_plugins.plugins.defaults()),
            mock.patch.object(panel_widgets, "log_error"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        panel = router_window.Panel(root)
        self.addCleanup(lambda: panel._poll_id and root.after_cancel(panel._poll_id))

        panel.show_event = 424242
        panel.resident = True
        fake_kernel = mock.Mock()
        with (
            mock.patch.object(router_window, "_kernel32", return_value=fake_kernel),
            mock.patch.object(panel, "hidden", return_value=False),
            mock.patch.object(panel, "parent_alive", return_value=True),
            mock.patch.object(panel, "version_superseded", return_value=True),
        ):
            panel.poll()
        fake_kernel.CloseHandle.assert_called_once_with(424242)
        self.assertIsNone(panel.show_event)
        self.assertFalse(panel.resident)
        self.assertEqual(panel.status.get(), "AgentCompass 已更新到新版本，关闭后重新打开即为新版")

        # Also verify close() releases show_event immediately when entering closing state with a running long_job
        panel.show_event = 535353
        panel.resident = False
        fake_kernel.reset_mock()
        alive_thread = mock.Mock()
        alive_thread.is_alive.return_value = True
        panel.long_jobs = [alive_thread]
        with (
            mock.patch.object(router_window, "_kernel32", return_value=fake_kernel),
            mock.patch.object(router_window.os, "_exit") as exit_,
        ):
            panel.close()
        exit_.assert_not_called()
        self.assertTrue(panel.closing)
        fake_kernel.CloseHandle.assert_called_once_with(535353)
        self.assertIsNone(panel.show_event)

    def test_full_installer_fallback_records_runtime_change_reason_in_events_and_status_i18n(self):
        import i18n

        for lang, expected_phrase in (
            ("zh", "运行时已变更，本次更新需要完整安装包"),
            ("en", "Runtime changed; a full installer is required for this update"),
        ):
            with self.subTest(lang=lang), TemporaryDirectory() as tmp:
                i18n.set_language(lang)
                self.addCleanup(lambda: i18n.set_language("zh"))
                base = Path(tmp)
                state_dir = base / "state"
                home = base / "home"
                home.mkdir()
                old_fp = "1" * 64
                new_fp = "2" * 64
                inst_root = _make_modular_installer_dir(base, version="0.3.18", runtime_fp=old_fp)
                zip_file = base / "release" / "app-0.4.0.zip"
                zip_bytes, zip_sha = _make_code_package_zip(zip_file, version="0.4.0", runtime_fp=new_fp)
                setup_bytes = b"MZ-new-runtime-full-installer"
                setup_sha = hashlib.sha256(setup_bytes).hexdigest()
                manifest_payload = {
                    "schema_version": 1,
                    "version": "0.4.0",
                    "runtime_fingerprint": new_fp,
                    "app_package": {
                        "name": "app-0.4.0.zip",
                        "size": len(zip_bytes),
                        "sha256": zip_sha,
                        "required_runtime_fingerprint": new_fp,
                    },
                    "installer": {
                        "name": "AgentCompass-tray-setup.exe",
                        "size": len(setup_bytes),
                        "sha256": setup_sha,
                        "runtime_fingerprint": new_fp,
                    },
                }
                manifest_bytes = json.dumps(manifest_payload).encode("utf-8")

                events = []
                updates.update_agentcompass(
                    on_event=events.append,
                    root=inst_root,
                    home=home,
                    state_dir=state_dir,
                    which_path=None,
                    frozen=False,
                    release={
                        "tag_name": "v0.4.0",
                        "assets": [
                            {"name": "manifest.json", "url": "https://example.com/manifest.json"},
                            {
                                "name": "app-0.4.0.zip",
                                "url": "https://example.com/app-0.4.0.zip",
                                "digest": f"sha256:{zip_sha}",
                            },
                            {
                                "name": "AgentCompass-tray-setup.exe",
                                "url": "https://example.com/AgentCompass-tray-setup.exe",
                                "digest": f"sha256:{setup_sha}",
                            },
                        ],
                    },
                    opener=lambda req, timeout=300: _Body(
                        manifest_bytes if req.full_url.endswith("manifest.json") else setup_bytes
                    ),
                    runner=lambda args, cwd=None, env=None: "",
                    restart_fn=lambda: "restarted",
                )
                self.assertTrue(
                    any(
                        expected_phrase in str(e.get("message") or "")
                        and "runtime_fingerprint_mismatch" in str(e.get("message") or "")
                        for e in events
                    ),
                    events,
                )
                status_file = state_dir / "update-status.json"
                self.assertTrue(status_file.is_file())
                status_doc = json.loads(status_file.read_text(encoding="utf-8"))
                self.assertIn("runtime_fingerprint_mismatch", str(status_doc.get("full_installer_reason") or ""))
                self.assertTrue(
                    any(
                        expected_phrase in line and "runtime_fingerprint_mismatch" in line
                        for line in (status_doc.get("events") or [])
                    ),
                    status_doc.get("events"),
                )

                # Also verify when _apply_code_package raises _IncompatibleCodePackageRuntime
                events_exc = []
                state_dir_exc = base / "state-exc"
                same_manifest = dict(manifest_payload, runtime_fingerprint=old_fp)
                same_manifest["app_package"] = dict(manifest_payload["app_package"], required_runtime_fingerprint=old_fp)
                same_manifest_bytes = json.dumps(same_manifest).encode("utf-8")
                updates.update_agentcompass(
                    on_event=events_exc.append,
                    root=inst_root,
                    home=home,
                    state_dir=state_dir_exc,
                    which_path=None,
                    frozen=False,
                    release={
                        "tag_name": "v0.4.0",
                        "assets": [
                            {"name": "manifest.json", "url": "https://example.com/manifest.json"},
                            {
                                "name": "app-0.4.0.zip",
                                "url": "https://example.com/app-0.4.0.zip",
                                "digest": f"sha256:{zip_sha}",
                            },
                            {
                                "name": "AgentCompass-tray-setup.exe",
                                "url": "https://example.com/AgentCompass-tray-setup.exe",
                                "digest": f"sha256:{setup_sha}",
                            },
                        ],
                    },
                    opener=lambda req, timeout=300: _Body(
                        same_manifest_bytes
                        if req.full_url.endswith("manifest.json")
                        else (zip_bytes if req.full_url.endswith(".zip") else setup_bytes)
                    ),
                    runner=lambda args, cwd=None, env=None: "",
                    restart_fn=lambda: "restarted",
                )
                self.assertTrue(
                    any(expected_phrase in str(e.get("message") or "") for e in events_exc),
                    events_exc,
                )
                status_exc_doc = json.loads((state_dir_exc / "update-status.json").read_text(encoding="utf-8"))
                self.assertTrue(status_exc_doc.get("full_installer_reason"))
                self.assertTrue(
                    any(expected_phrase in line for line in (status_exc_doc.get("events") or [])),
                    status_exc_doc.get("events"),
                )


class RollbackNoticeTests(unittest.TestCase):
    def write_state(self, root: Path, **fields) -> None:
        (root / "app").mkdir(parents=True, exist_ok=True)
        (root / "app" / "state.json").write_bytes(json.dumps({"current_version": "0.4.2", **fields}).encode("utf-8"))

    def test_a_rollback_is_reported_once_with_versions_and_reason(self):
        with TemporaryDirectory() as tmp:
            root, state_dir = Path(tmp) / "install", Path(tmp) / "state"
            self.write_state(root, rolled_back_from="0.4.3", rollback_reason="tray startup failed: boom", rolled_back_at=1794309843.8)
            message = updates.rollback_notice(install_root=root, state_dir=state_dir)
            self.assertIn("0.4.2", message)
            self.assertIn("0.4.3", message)
            self.assertIn("tray startup failed: boom", message)
            self.assertIsNone(updates.rollback_notice(install_root=root, state_dir=state_dir))
            # A later rollback is news again.
            self.write_state(root, rolled_back_from="0.4.3", rollback_reason="tray import failed: x", rolled_back_at="2026-11-11T01:00:00Z")
            self.assertIn("tray import failed: x", updates.rollback_notice(install_root=root, state_dir=state_dir))

    def test_no_rollback_and_a_checkout_say_nothing(self):
        with TemporaryDirectory() as tmp:
            root, state_dir = Path(tmp) / "install", Path(tmp) / "state"
            self.write_state(root, updated_at="2026-11-10T07:35:20Z")
            self.assertIsNone(updates.rollback_notice(install_root=root, state_dir=state_dir))
            self.assertIsNone(updates.rollback_notice(install_root=Path(tmp) / "missing", state_dir=state_dir))
            with mock.patch.object(updates.component_launch, "frozen", return_value=False):
                self.assertIsNone(updates.rollback_notice(state_dir=state_dir))


class _FakeKey:
    def __init__(self, values=None, children=None):
        self.values = dict(values or {})
        self.children = dict(children or {})

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeRegistry:
    """The winreg calls _sync_uninstall_display_version makes, over an in-memory tree."""
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ, KEY_SET_VALUE, REG_SZ = 1, 2, 1

    def __init__(self, entries):
        self.root = _FakeKey(children={updates.UNINSTALL_KEY: _FakeKey(children=entries)})

    def OpenKey(self, parent, name, *args):
        node = self.root if parent == self.HKEY_CURRENT_USER else parent
        if name not in node.children:
            raise OSError(name)
        return node.children[name]

    def EnumKey(self, key, index):
        names = list(key.children)
        if index >= len(names):
            raise OSError(index)
        return names[index]

    def QueryValueEx(self, key, name):
        if name not in key.values:
            raise OSError(name)
        return key.values[name], self.REG_SZ

    def SetValueEx(self, key, name, _reserved, _kind, value):
        key.values[name] = value

    def CloseKey(self, key):
        pass


class UninstallDisplayVersionTests(unittest.TestCase):
    def test_code_package_version_reaches_the_uninstall_entry_of_this_install_only(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "AgentCompass"
            ours = _FakeKey({"InstallLocation": str(root) + "\\", "DisplayVersion": "0.4.5"})
            other = _FakeKey({"InstallLocation": str(Path(tmp) / "Other"), "DisplayVersion": "1.0"})
            no_location = _FakeKey({"DisplayVersion": "2.0"})
            reg = _FakeRegistry({"{A}_is1": ours, "{B}": other, "{C}": no_location})
            self.assertTrue(updates._sync_uninstall_display_version(root, "0.4.6", registry=reg))
            self.assertEqual(ours.values["DisplayVersion"], "0.4.6")
            self.assertEqual(other.values["DisplayVersion"], "1.0")
            self.assertEqual(no_location.values["DisplayVersion"], "2.0")

    def test_no_entry_or_no_version_changes_nothing(self):
        reg = _FakeRegistry({})
        self.assertFalse(updates._sync_uninstall_display_version(Path("C:/x"), "0.4.6", registry=reg))
        self.assertFalse(updates._sync_uninstall_display_version(Path("C:/x"), "", registry=reg))


if __name__ == "__main__":
    unittest.main()
