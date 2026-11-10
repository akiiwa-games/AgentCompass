"""Runtime loader for the Windows installer's tray and CLI executables.

Both ``AgentCompass.exe`` and ``cli\\agentcompass.exe`` are thin PyInstaller binaries that carry
only the Python interpreter, standard library, and locked third-party packages. Application code
(``agentcompass/``, ``tray/``, ``skills/``, ``setup/``) lives under ``<app>\\app\\current`` and is
updated in place from ``app-<version>.zip`` whenever the runtime fingerprint is unchanged.

If ``<app>\\app\\current`` is missing, corrupt, or fails to import at startup, this loader
automatically rolls back to ``<app>\\app\\previous`` (or re-extracts the last valid code package
from ``<app>\\app\\packages``) before running the entry point.

Only the main tray's own start decides that a package is bad. A ``--run-component`` child of a
running tray, or a tray that already came up, never rolls back: their failures say nothing
about the package, and rewriting ``app\\current`` under a running tray mixes two versions.
Every failure is appended with its traceback to ``~/.agent-compass/tray-errors.log``.
"""

from __future__ import annotations

import gc
import hashlib
import importlib
import json
import os
import runpy
import shutil
import subprocess
import sys
import time
import traceback
import types
import uuid
import zipfile
from pathlib import Path

REQUIRED_APP_FILES = (
    "app-manifest.json",
    "agentcompass/__init__.py",
    "agentcompass/cli.py",
    "tray/app.pyw",
    "tray/component_launch.py",
    "tray/updates.py",
    "skills/agent-compass/SKILL.md",
    "skills/agent-compass/scripts/route_judge.py",
    "setup/onboard.py",
)

CORE_HASH_CHECK_FILES = (
    "agentcompass/__init__.py",
    "agentcompass/cli.py",
    "tray/app.pyw",
    "tray/component_launch.py",
    "tray/updates.py",
)

# ``AgentCompass.exe --run-component <name>`` (tray/component_launch.py FLAG).
COMPONENT_FLAG = "--run-component"
# tray/app.pyw sets this on ``sys`` once its icon is up. A crash after that is not a bad package.
TRAY_STARTED_ATTR = "_agentcompass_tray_started"
ERROR_LOG_MAX_BYTES = 1_000_000
# tray/app.pyw single_instance(): held by the tray that owns the package for as long as it runs.
TRAY_MUTEX_NAME = "Local\\AgentCompassTray"

CORE_TRAY_SYNTAX_FILES = (
    "tray/app.pyw",
    "tray/component_launch.py",
    "tray/updates.py",
    "tray/services.py",
    "tray/deploy.py",
    "tray/router_window.py",
)


def _version_key(text: str) -> tuple[int, ...]:
    cleaned = str(text or "").strip().lstrip("vV").split("-", 1)[0]
    if not cleaned:
        return (0, 0, 0)
    parts: list[int] = []
    for item in cleaned.split("."):
        digits = "".join(ch for ch in item if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def install_root_from_executable(executable: str | Path | None = None) -> Path:
    exe = Path(executable or sys.executable).resolve()
    if exe.parent.name.lower() == "cli":
        return exe.parent.parent
    return exe.parent


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def read_json(path: Path) -> dict | None:
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_bytes((json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    tmp.replace(path)


def validate_app_dir(
    app_dir: Path,
    *,
    install_root: Path | None = None,
    verify_all_hashes: bool = False,
) -> tuple[bool, str]:
    """Check that ``app_dir`` contains a complete, uncorrupted application code package."""
    app_dir = Path(app_dir)
    if not app_dir.is_dir():
        return False, f"missing directory: {app_dir}"
    for rel in REQUIRED_APP_FILES:
        target = app_dir / rel
        if not target.is_file():
            return False, f"missing required file: {rel}"
    manifest = read_json(app_dir / "app-manifest.json")
    if not manifest:
        return False, "unreadable app-manifest.json"
    version = str(manifest.get("version") or "").strip()
    if not version:
        return False, "app-manifest.json is missing version"

    root = (
        Path(install_root)
        if install_root is not None
        else (app_dir.parents[1] if len(app_dir.parents) >= 2 and app_dir.parent.name.lower() == "app" else None)
    )
    if root is not None:
        rt_info = read_runtime_info(root)
        if isinstance(rt_info, dict):
            local_fp = str(rt_info.get("runtime_fingerprint") or "").strip()
            rt_reqs = manifest.get("runtime_requirements") if isinstance(manifest.get("runtime_requirements"), dict) else {}
            req_fp = str(manifest.get("required_runtime_fingerprint") or rt_reqs.get("runtime_fingerprint") or "").strip()
            if local_fp and req_fp and local_fp != req_fp:
                return False, f"runtime_fingerprint mismatch: app requires {req_fp[:12]}, runtime is {local_fp[:12]}"
            local_rt_ver = str(rt_info.get("runtime_version") or "").strip()
            min_rt_ver = str(manifest.get("min_runtime_version") or rt_reqs.get("min_runtime_version") or "").strip()
            if local_rt_ver and min_rt_ver and _version_key(local_rt_ver) < _version_key(min_rt_ver):
                return False, f"min_runtime_version mismatch: app requires >={min_rt_ver}, runtime is {local_rt_ver}"

    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        return False, "app-manifest.json is missing files table"
    to_verify = list(files.keys()) if verify_all_hashes else [k for k in CORE_HASH_CHECK_FILES if k in files]
    for rel in to_verify:
        entry = files.get(rel)
        expected = (
            str(entry.get("sha256") or "").strip().lower()
            if isinstance(entry, dict)
            else str(entry or "").strip().lower()
        )
        target = app_dir / rel
        if not target.is_file():
            return False, f"missing listed file: {rel}"
        if expected and sha256_file(target) != expected:
            return False, f"sha256 mismatch in {rel}"
    return True, "ok"


def read_app_state(install_root: Path) -> dict:
    return read_json(Path(install_root) / "app" / "state.json") or {}


def write_app_state(install_root: Path, state: dict) -> None:
    write_json_atomic(Path(install_root) / "app" / "state.json", state)


def read_runtime_info(install_root: Path) -> dict | None:
    return read_json(Path(install_root) / "runtime-info.json")


def _make_junction_or_copy(link: Path, target: Path) -> None:
    try:
        is_reparse = bool(os.lstat(link).st_file_attributes & 0x400) if os.name == "nt" else link.is_symlink()
    except OSError:
        is_reparse = False
    if is_reparse:
        try:
            if link.resolve() == target.resolve():
                return
        except OSError:
            pass
        try:
            os.rmdir(link) if os.name == "nt" else link.unlink()
        except OSError:
            return
    elif link.exists():
        try:
            if link.is_dir():
                shutil.rmtree(link)
            else:
                link.unlink()
        except OSError:
            return
    link.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        done = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            creationflags=creationflags,
            check=False,
        )
        if done.returncode == 0:
            return
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pass


def ensure_legacy_data_links(install_root: Path, active_dir: Path) -> None:
    """Keep ``cli\\_internal\\agentcompass\\data\\{skills,setup}`` resolving into ``active_dir``.

    Clients that ran ``agentcompass setup`` on <=0.3.18 created skill junctions pointing at
    ``cli\\_internal\\agentcompass\\data\\skills\\agent-compass``. Linking ``data\\skills`` and
    ``data\\setup`` into ``app\\current`` keeps those junctions working across upgrades without
    duplicating the files in the installer.
    """
    install_root = Path(install_root)
    active_dir = Path(active_dir)
    if not (install_root / "cli").is_dir():
        return
    data_dir = install_root / "cli" / "_internal" / "agentcompass" / "data"
    for sub in ("skills", "setup"):
        target = active_dir / sub
        if target.is_dir():
            try:
                _make_junction_or_copy(data_dir / sub, target)
            except Exception:
                pass


def _replace_or_copy_file(tmp_target: Path, target: Path) -> None:
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            tmp_target.replace(target)
            return
        except PermissionError as exc:
            last_err = exc
            try:
                shutil.copy2(tmp_target, target)
                tmp_target.unlink(missing_ok=True)
                return
            except OSError as copy_exc:
                last_err = copy_exc
                if attempt < 2:
                    time.sleep(0.05)
    tmp_target.unlink(missing_ok=True)
    if last_err is not None:
        raise last_err


def _sync_dir_in_place(src_dir: Path, dest_dir: Path) -> None:
    """Copy ``src_dir`` into ``dest_dir`` in-place when Windows holds a directory handle on ``dest_dir``."""
    src_dir = Path(src_dir).resolve()
    dest_dir = Path(dest_dir).resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    src_rel_paths: set[Path] = set()
    for item in src_dir.rglob("*"):
        rel = item.relative_to(src_dir)
        src_rel_paths.add(rel)
        target = dest_dir / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp_target = target.parent / f".{target.name}.tmp-{uuid.uuid4().hex[:8]}"
            shutil.copy2(item, tmp_target)
            _replace_or_copy_file(tmp_target, target)
    for existing in sorted(dest_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        rel = existing.relative_to(dest_dir)
        if rel not in src_rel_paths:
            if existing.is_file() or existing.is_symlink():
                try:
                    existing.unlink()
                except OSError:
                    pass
            elif existing.is_dir():
                try:
                    existing.rmdir()
                except OSError:
                    shutil.rmtree(existing, ignore_errors=True)


def _extract_package_to(zip_path: Path, dest_dir: Path, *, install_root: Path | None = None) -> tuple[bool, str]:
    staging = dest_dir.parent / f".extract-{uuid.uuid4().hex[:8]}"
    try:
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(staging)
        ok, reason = validate_app_dir(staging, install_root=install_root, verify_all_hashes=True)
        if not ok:
            shutil.rmtree(staging, ignore_errors=True)
            return False, reason
        dest_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            if dest_dir.exists():
                shutil.rmtree(dest_dir, ignore_errors=True)
            staging.replace(dest_dir)
        except PermissionError:
            _sync_dir_in_place(staging, dest_dir)
            shutil.rmtree(staging, ignore_errors=True)
        return True, "ok"
    except Exception as exc:
        shutil.rmtree(staging, ignore_errors=True)
        return False, str(exc)


def rollback_to_previous(install_root: Path, reason: str = "", *, allow_same_version: bool = False) -> Path | None:
    """Restore ``<install_root>\\app\\current`` from ``previous`` (or ``packages``) after a load failure."""
    install_root = Path(install_root)
    app_base = install_root / "app"
    current_dir = app_base / "current"
    previous_dir = app_base / "previous"
    packages_dir = app_base / "packages"
    state = read_app_state(install_root)
    current_manifest = read_json(current_dir / "app-manifest.json") or {}
    current_ver = str(
        current_manifest.get("version")
        or state.get("current_version")
        or state.get("active_version")
        or ""
    ).strip()

    restored: Path | None = None
    ok_prev, _ = validate_app_dir(previous_dir, install_root=install_root)
    if ok_prev:
        broken_dir = app_base / f".broken-{uuid.uuid4().hex[:8]}"
        try:
            if current_dir.exists():
                current_dir.replace(broken_dir)
            shutil.copytree(previous_dir, current_dir)
            shutil.rmtree(broken_dir, ignore_errors=True)
            restored = current_dir
        except OSError:
            shutil.rmtree(broken_dir, ignore_errors=True)
            try:
                _sync_dir_in_place(previous_dir, current_dir)
                restored = current_dir
            except OSError:
                restored = previous_dir
    else:
        candidates: list[Path] = []
        ver_keys = ("current_version", "active_version", "previous_version") if allow_same_version else ("previous_version",)
        for ver_key in ver_keys:
            ver = str(state.get(ver_key) or "").strip()
            if ver:
                cand = packages_dir / f"app-{ver}.zip"
                if cand.is_file() and cand not in candidates:
                    candidates.append(cand)
        if packages_dir.is_dir():
            sorted_pkgs = sorted(
                packages_dir.glob("app-*.zip"),
                key=lambda p: (_version_key(p.stem.removeprefix("app-")), p.name),
                reverse=True,
            )
            for cand in sorted_pkgs:
                if not allow_same_version and current_ver and cand.name == f"app-{current_ver}.zip":
                    continue
                if cand not in candidates:
                    candidates.append(cand)
        for zip_cand in candidates:
            ok, _ = _extract_package_to(zip_cand, current_dir, install_root=install_root)
            if ok:
                restored = current_dir
                break

    if restored is not None:
        manifest = read_json(restored / "app-manifest.json") or {}
        restored_ver = str(manifest.get("version") or state.get("previous_version") or "").strip()
        failed_ver = current_ver or str(state.get("current_version") or state.get("active_version") or "").strip()
        new_state = {
            **state,
            "active_version": restored_ver or failed_ver,
            "current_version": restored_ver or failed_ver,
            "rolled_back_from": failed_ver,
            "rollback_reason": reason,
            "rolled_back_at": time.time(),
        }
        try:
            write_app_state(install_root, new_state)
        except OSError:
            pass
        ensure_legacy_data_links(install_root, restored)
    return restored


def activate_app_dir(install_root: Path) -> Path:
    """Return the validated active application directory, rolling back to ``previous`` if needed."""
    install_root = Path(install_root)
    current_dir = install_root / "app" / "current"
    ok, reason = validate_app_dir(current_dir, install_root=install_root)
    if ok:
        ensure_legacy_data_links(install_root, current_dir)
        return current_dir
    restored = rollback_to_previous(install_root, reason=reason, allow_same_version=not current_dir.exists())
    if restored is not None:
        return restored
    raise RuntimeError(f"AgentCompass code package under {install_root / 'app'} could not be loaded ({reason}) and no valid fallback package was found.")


def clear_loaded_app_modules(install_root: Path) -> None:
    app_prefix = os.path.normcase(str((Path(install_root) / "app").resolve()))
    to_remove = []
    for name, mod in list(sys.modules.items()):
        if name == "agentcompass" or name.startswith("agentcompass."):
            to_remove.append(name)
            continue
        mod_file = getattr(mod, "__file__", None)
        if mod_file:
            try:
                norm = os.path.normcase(str(Path(mod_file).resolve()))
                if norm.startswith(app_prefix):
                    to_remove.append(name)
            except OSError:
                pass
    for name in to_remove:
        sys.modules.pop(name, None)
    importlib.invalidate_caches()


def configure_sys_path(active_dir: Path, role: str, install_root: Path | None = None) -> None:
    active_dir = Path(active_dir).resolve()
    root = Path(install_root).resolve() if install_root is not None else active_dir.parents[1]
    app_prefix = os.path.normcase(str(root / "app"))
    # Ensure no process keeps <install_root>/app/current as its working directory on Windows,
    # which would lock the directory handle and block atomic rename during incremental updates.
    try:
        cwd_norm = os.path.normcase(str(Path.cwd().resolve()))
        if cwd_norm == app_prefix or cwd_norm.startswith(app_prefix + os.sep):
            os.chdir(root)
    except OSError:
        pass
    cleaned = []
    for entry in sys.path:
        try:
            norm = os.path.normcase(str(Path(entry).resolve()))
            if norm.startswith(app_prefix):
                continue
        except OSError:
            pass
        cleaned.append(entry)
    prepend = [str(active_dir / "tray"), str(active_dir)] if role == "tray" else [str(active_dir)]
    sys.path[:] = prepend + [p for p in cleaned if p not in prepend]
    os.environ["AGENTCOMPASS_APP_DIR"] = str(active_dir)
    os.environ["AGENTCOMPASS_INSTALL_ROOT"] = str(root)


def _load_cli_main(active_dir: Path, install_root: Path):
    clear_loaded_app_modules(install_root)
    configure_sys_path(active_dir, "cli", install_root=install_root)
    pkg_dir = active_dir / "agentcompass"
    if pkg_dir.is_dir():
        for py_file in sorted(pkg_dir.glob("*.py")):
            compile(py_file.read_bytes(), str(py_file), "exec")
    from agentcompass.cli import main
    return main


def _remap_app_arg(arg: str, old_app_dir: Path, new_app_dir: Path) -> str:
    try:
        old_resolved = Path(old_app_dir).resolve()
        new_resolved = Path(new_app_dir).resolve()
        if old_resolved == new_resolved:
            return arg
        cand = Path(arg)
        if cand.is_absolute():
            cand_resolved = cand.resolve()
            if cand_resolved == old_resolved or old_resolved in cand_resolved.parents:
                rel = cand_resolved.relative_to(old_resolved)
                return str(new_resolved / rel)
    except Exception:
        pass
    return arg


def run_cli(install_root: Path | None = None, argv: list[str] | None = None) -> int:
    root = Path(install_root).resolve() if install_root is not None else install_root_from_executable()
    orig_sys_argv = list(sys.argv)
    prog = orig_sys_argv[0] if orig_sys_argv else sys.executable
    effective_argv = list(orig_sys_argv[1:] if argv is None else argv)
    initial_dir = root / "app" / "current"
    active_dir = activate_app_dir(root)
    rolled_back = False
    try:
        cli_main = _load_cli_main(active_dir, root)
    except Exception as exc:
        restored = rollback_to_previous(root, reason=f"cli import failed: {exc}")
        if restored is None:
            raise
        clear_loaded_app_modules(root)
        active_dir = restored
        rolled_back = True
        cli_main = _load_cli_main(active_dir, root)
    run_args = [_remap_app_arg(a, initial_dir, active_dir) for a in effective_argv]
    try:
        sys.argv[:] = [prog, *run_args]
        return int(cli_main(list(run_args)) or 0)
    except (ImportError, ModuleNotFoundError, SyntaxError) as exc:
        if not rolled_back:
            restored = rollback_to_previous(root, reason=f"cli runtime import failed: {exc}")
            if restored is not None:
                retry_args = [_remap_app_arg(a, active_dir, restored) for a in run_args]
                sys.argv[:] = [prog, *retry_args]
                clear_loaded_app_modules(root)
                cli_main = _load_cli_main(restored, root)
                return int(cli_main(list(retry_args)) or 0)
        raise
    finally:
        if argv is not None:
            sys.argv[:] = orig_sys_argv


def _verify_tray_imports(active_dir: Path, install_root: Path):
    clear_loaded_app_modules(install_root)
    configure_sys_path(active_dir, "tray", install_root=install_root)
    for rel in CORE_TRAY_SYNTAX_FILES:
        path = active_dir / rel
        if path.is_file():
            compile(path.read_bytes(), str(path), "exec")
    import agentcompass  # noqa: F401
    import component_launch
    return component_launch


def _close_win32_handle(handle: object) -> None:
    if not handle or os.name != "nt":
        return
    try:
        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CloseHandle(ctypes.c_void_p(handle))
    except Exception:
        pass


def _release_tray_mutex() -> None:
    closed_handles: set[int] = set()
    for mod_name in ("AgentCompass_tray_app", "__main__"):
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        handle = getattr(mod, "_SINGLE_INSTANCE_MUTEX", None)
        release_fn = getattr(mod, "release_single_instance", None)
        if callable(release_fn):
            try:
                release_fn()
            except Exception:
                pass
        remaining = getattr(mod, "_SINGLE_INSTANCE_MUTEX", None) or handle
        if remaining and int(remaining) not in closed_handles:
            _close_win32_handle(remaining)
            closed_handles.add(int(remaining))
        try:
            setattr(mod, "_SINGLE_INSTANCE_MUTEX", None)
        except Exception:
            pass
    sys_handle = getattr(sys, "_agentcompass_tray_mutex", None)
    if sys_handle and int(sys_handle) not in closed_handles:
        _close_win32_handle(sys_handle)
        closed_handles.add(int(sys_handle))
    try:
        sys._agentcompass_tray_mutex = None  # type: ignore[attr-defined]
    except Exception:
        pass


def _run_tray_once(active_dir: Path, args: list[str], component_launch) -> int:
    helper_exit = component_launch.dispatch(args)
    if helper_exit is not None:
        return int(helper_exit)

    app_pyw = active_dir / "tray" / "app.pyw"
    sys.argv = [str(app_pyw), *args[1:]]
    prev_main = sys.modules.get("__main__")
    mod = types.ModuleType("__main__")
    mod.__file__ = str(app_pyw)
    mod.__loader__ = None
    mod.__package__ = None
    sys.modules["__main__"] = mod
    sys.modules["AgentCompass_tray_app"] = mod
    try:
        code = compile(app_pyw.read_bytes(), str(app_pyw), "exec")
        exec(code, mod.__dict__)
    except SystemExit as exit_:
        code_val = exit_.code
        return code_val if isinstance(code_val, int) else 0 if code_val is None else 1
    finally:
        if prev_main is not None:
            sys.modules["__main__"] = prev_main
    return 0


def error_log_path() -> Path:
    return Path.home() / ".agent-compass" / "tray-errors.log"


def log_exception(context: str, args: list[str]) -> None:
    """Append the exception being handled, with its traceback, to ``~/.agent-compass/tray-errors.log``."""
    text = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] pid={os.getpid()} {context} argv={args[1:]!r}\n{traceback.format_exc()}\n"
    try:
        path = error_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.stat().st_size > ERROR_LOG_MAX_BYTES:
                os.replace(path, path.with_name(path.name + ".1"))
        except OSError:
            pass
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
    except Exception:
        pass


def is_component_request(args: list[str]) -> bool:
    return len(args) >= 3 and args[1] == COMPONENT_FLAG


def tray_running_elsewhere() -> bool:
    """True while another process holds the tray's single-instance mutex (tray/app.pyw ``single_instance``).

    Call it after this process released its own handle (``_release_tray_mutex``): a named mutex exists only
    while some handle to it is open.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenMutexW.restype = ctypes.c_void_p
        handle = kernel.OpenMutexW(0x00100000, False, TRAY_MUTEX_NAME)  # SYNCHRONIZE
        if not handle:
            return False
        kernel.CloseHandle(ctypes.c_void_p(handle))
        return True
    except Exception:
        return False


def run_component(root: Path, args: list[str]) -> int:
    """Run one tray child (``--run-component <name>``) from ``app\\current`` as it is.

    A component never repairs, rolls back or rewrites the installed app. It is started by a tray
    that already loaded a package, so its failure (a bug, a locked file, an update swapping files
    under it) says nothing about whether the package is bad, and replacing ``app\\current`` would
    leave the running tray on one version and the disk on another. The tray's next start judges
    the package. The failure is logged with its traceback and the component exits 1.
    """
    return _run_as_found(root, args, f"component {args[2]}")


def _run_as_found(root: Path, args: list[str], context: str) -> int:
    """Run a component, or a tray launched beside a running one, from ``app\\current`` without repairing it."""
    try:
        current = root / "app" / "current"
        ok, reason = validate_app_dir(current, install_root=root)
        if not ok:
            raise RuntimeError(f"{current} is not loadable ({reason}); the tray checks it again on its next start")
        component_launch = _verify_tray_imports(current, root)
        if is_component_request(args):
            return int(component_launch.dispatch(args) or 0)
        return _run_tray_once(current, args, component_launch)
    except Exception:
        log_exception(f"{context} failed", args)
        if sys.stderr is not None:
            try:
                traceback.print_exc(file=sys.stderr)
                sys.stderr.flush()
            except Exception:
                pass
        return 1


def run_tray(install_root: Path | None = None, argv: list[str] | None = None) -> int:
    root = Path(install_root).resolve() if install_root is not None else install_root_from_executable()
    args = list(sys.argv if argv is None else argv)
    if is_component_request(args):
        return run_component(root, args)
    # Read once and drop it: a tray restarted later inherits this environment and must judge its own package.
    rolled_back = os.environ.pop("AGENTCOMPASS_ROLLBACK_RETRY", None) == "1"
    if tray_running_elsewhere():
        # A second launch (Start menu, shortcut) only opens the running tray's window. That tray owns the
        # package; this process must not repair or replace app\current under it.
        return _run_as_found(root, args, "second tray launch")
    active_dir = activate_app_dir(root)
    try:
        component_launch = _verify_tray_imports(active_dir, root)
    except Exception as exc:
        log_exception("tray import failed", args)
        if rolled_back or tray_running_elsewhere():
            raise
        restored = rollback_to_previous(root, reason=f"tray import failed: {exc}")
        if restored is None:
            raise
        clear_loaded_app_modules(root)
        active_dir = restored
        rolled_back = True
        component_launch = _verify_tray_imports(active_dir, root)

    setattr(sys, TRAY_STARTED_ATTR, False)
    try:
        return _run_tray_once(active_dir, args, component_launch)
    except Exception as exc:
        started = bool(getattr(sys, TRAY_STARTED_ATTR, False))
        log_exception("tray failed after it started" if started else "tray startup failed", args)
        _release_tray_mutex()
        # Once the icon is up the package has loaded and run; a later crash is a runtime fault that the
        # previous package would not cure, so it is reported, not rolled back.
        if not rolled_back and not started and not tray_running_elsewhere():
            restored = rollback_to_previous(root, reason=f"tray startup failed: {exc}")
            if restored is not None:
                if getattr(sys, "frozen", False) and Path(sys.executable).is_file():
                    for stream in (sys.stdout, sys.stderr):
                        try:
                            if stream is not None:
                                stream.flush()
                        except Exception:
                            pass
                    gc.collect()
                    env = dict(os.environ)
                    env["AGENTCOMPASS_ROLLBACK_RETRY"] = "1"
                    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
                    )
                    subprocess.Popen(
                        [sys.executable, *args[1:]],
                        cwd=str(root),
                        env=env,
                        creationflags=creationflags,
                        close_fds=True,
                    )
                    return 0
                clear_loaded_app_modules(root)
                component_launch = _verify_tray_imports(restored, root)
                return _run_tray_once(restored, args, component_launch)
        raise
