"""Build the AgentCompass runtime fingerprint, versioned code package (``app-<version>.zip``),
and release manifest (``manifest.json``).

Why ``runtime_fingerprint`` hashes the boot executables' deterministic inputs rather than the
raw ``.exe`` output bytes:
PyInstaller embeds build timestamps and archive offsets into the Windows PE header and PYZ
structures on every run, and optional Authenticode signing (``signtool``) modifies the PE
checksum and certificate table after PyInstaller finishes. Hashing the raw ``AgentCompass.exe``
or ``cli\\agentcompass.exe`` binaries directly into ``runtime_fingerprint`` would cause two builds
of the exact same runtime inputs to produce different fingerprints, forcing a full 36 MB
reinstall on every code-only release. Instead, ``runtime_fingerprint`` is the SHA-256 of the
canonical JSON of all runtime inputs (Python version, PyInstaller version, locked third-party
package versions, ``packaging/agentcompass-tray.spec``, ``packaging/agentcompass.iss``, and the
boot loader entry scripts), while the built ``.exe`` SHA-256 digests are recorded alongside it
in ``boot_executables`` for auditing.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import importlib.metadata
import importlib.util
import json
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import app_bootstrap  # noqa: E402
import stage_data  # noqa: E402

RUNTIME_DISTRIBUTIONS = (
    "Pillow",
    "psutil",
    "pystray",
    "pywin32",
    "tzdata",
)

BOOT_LOADER_FILES = (
    "packaging/app_bootstrap.py",
    "packaging/cli_entry.py",
    "packaging/tray_entry.py",
)

STDLIB_SKIP_ROOTS = frozenset({
    "antigravity",
    "crypt",
    "curses",
    "ensurepip",
    "fcntl",
    "grp",
    "idlelib",
    "lib2to3",
    "nis",
    "ossaudiodev",
    "posix",
    "pty",
    "pwd",
    "readline",
    "resource",
    "spwd",
    "syslog",
    "termios",
    "test",
    "tty",
    "turtle",
    "turtledemo",
})

NON_RUNTIME_EXCLUDES = (
    "numpy",
    "scipy",
    "matplotlib",
    "pandas",
    "PIL.ImageQt",
    "PIL.AvifImagePlugin",
    "PIL._avif",
    "PySide6",
    "PySide2",
    "PyQt6",
    "PyQt5",
    "Pythonwin",
    "pywin",
    "win32ui",
    "win32uiole",
    "yaml",
    "charset_normalizer",
    "pytest",
    "_pytest",
)

TRAY_THIRD_PARTY_IMPORTS = (
    "pystray",
    "pystray._win32",
    "PIL",
    "PIL.Image",
    "PIL.ImageDraw",
    "PIL.ImageFont",
    "PIL.ImageColor",
    "PIL.ImageFilter",
    "PIL.ImageEnhance",
    "PIL.ImageOps",
    "PIL.ImageChops",
    "PIL.ImageTk",
    "PIL.PngImagePlugin",
    "PIL.IcoImagePlugin",
    "PIL.BmpImagePlugin",
    "win32api",
    "win32con",
    "win32gui",
    "win32gui_struct",
    "win32process",
    "win32event",
    "win32job",
    "win32com",
    "win32com.client",
    "win32com.shell",
    "win32com.propsys",
    "pywintypes",
    "pythoncom",
    "psutil",
)

CLI_THIRD_PARTY_IMPORTS = (
    "win32api",
    "win32con",
    "win32event",
    "win32job",
    "win32process",
    "pywintypes",
)

TRAY_RUNTIME_SUFFIXES = {".py", ".pyw", ".ps1", ".mjs"}
TRAY_EXCLUDE_NAMES = {"conftest.py", "tk_test_support.py"}


@functools.lru_cache(maxsize=4)
def collect_bundled_stdlib(include_tkinter: bool = True) -> tuple[str, ...]:
    """Return the complete Windows standard-library module list for the frozen runtime."""
    roots = sorted(
        name
        for name in sys.stdlib_module_names
        if name not in STDLIB_SKIP_ROOTS
        and (include_tkinter or name != "tkinter")
        and not name.startswith(("_xx", "xx", "_test"))
    )
    out: set[str] = set()
    for name in roots:
        try:
            spec = importlib.util.find_spec(name)
        except Exception:
            continue
        if spec is None:
            continue
        out.add(name)
        if spec.submodule_search_locations:
            for loc in spec.submodule_search_locations:
                base = Path(loc)
                if not base.is_dir():
                    continue
                for path in base.rglob("*.py"):
                    rel = path.relative_to(base).with_suffix("")
                    parts = list(rel.parts)
                    if any(part.startswith("__") and part != "__init__" for part in parts):
                        continue
                    if parts and parts[-1] == "__init__":
                        parts = parts[:-1]
                    sub = f"{name}.{'.'.join(parts)}" if parts else name
                    sub_parts = sub.split(".")[1:]
                    if any(part in ("test", "tests", "idle_test") for part in sub_parts):
                        continue
                    out.add(sub)
    return tuple(sorted(out))


def tray_hidden_imports() -> list[str]:
    return sorted({"app_bootstrap", *TRAY_THIRD_PARTY_IMPORTS, *collect_bundled_stdlib(include_tkinter=True)})


def cli_hidden_imports() -> list[str]:
    return sorted({"app_bootstrap", *CLI_THIRD_PARTY_IMPORTS, *collect_bundled_stdlib(include_tkinter=False)})


_FROZEN_TREE_HASH_DIRS = frozenset({"_tcl_data", "_tk_data", "tcl8", "tzdata"})
_FROZEN_SKIP_DIRS = frozenset({"__pycache__", "agentcompass"})
_FROZEN_CODE_SUFFIXES = frozenset({".py", ".pyc", ".pyo", ".pyw"})


def _is_windows_api_set_dll(name: str) -> bool:
    """Universal CRT and API-set forwarders come from the build machine's Windows image, not from us: GitHub's
    runner bundles them, a desktop build does not, and Windows supplies them anyway. Counting them would split
    local and CI fingerprints and push one full reinstall whenever the runner image refreshes."""
    lower = name.lower()
    return lower.endswith(".dll") and (lower.startswith(("api-ms-win-", "ext-ms-win-")) or lower == "ucrtbase.dll")


def _directory_tree_sha256(folder: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if "__pycache__" in path.parts or path.suffix.lower() in _FROZEN_CODE_SUFFIXES:
            continue
        rel = path.relative_to(folder).as_posix()
        digest.update(f"{rel}:{file_sha256(path)}\n".encode("utf-8"))
    return digest.hexdigest().lower()


def collect_runtime_asset_hashes(
    root: Path = ROOT,
    *,
    dist_app_dir: Path | None = None,
) -> dict[str, str]:
    """Hash non-code assets bundled inside the frozen runtime directories (``_internal`` and ``cli/_internal``)."""
    root = Path(root)
    frozen_root = Path(dist_app_dir) if dist_app_dir is not None else (root / "dist" / "AgentCompass")
    assets: dict[str, str] = {}
    for rel_internal in ("_internal", "cli/_internal"):
        internal_dir = frozen_root / rel_internal
        if not internal_dir.is_dir():
            continue
        for child in sorted(internal_dir.iterdir()):
            if (child.name == "base_library.zip" or child.name in _FROZEN_SKIP_DIRS or child.is_symlink()
                    or _is_windows_api_set_dll(child.name)):
                continue
            if child.is_file():
                if child.suffix.lower() not in _FROZEN_CODE_SUFFIXES:
                    rel = child.relative_to(frozen_root).as_posix()
                    assets[rel] = file_sha256(child)
            elif child.is_dir():
                if child.name in _FROZEN_TREE_HASH_DIRS:
                    rel = child.relative_to(frozen_root).as_posix()
                    assets[rel] = _directory_tree_sha256(child)
                else:
                    for path in sorted(child.rglob("*")):
                        if not path.is_file() or path.is_symlink():
                            continue
                        if "__pycache__" in path.parts or path.suffix.lower() in _FROZEN_CODE_SUFFIXES:
                            continue
                        rel = path.relative_to(frozen_root).as_posix()
                        assets[rel] = file_sha256(path)
    return assets


def read_version(root: Path = ROOT) -> str:
    init_file = Path(root) / "agentcompass" / "__init__.py"
    text = init_file.read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*[\'"]([^\'"]+)[\'"]', text, re.M)
    if not match:
        raise RuntimeError(f"Could not read __version__ from {init_file}")
    return match.group(1).strip()


def normalized_file_sha256(path: Path) -> str:
    """SHA-256 of a text/source file with CRLF normalized to LF so git autocrlf cannot skew fingerprints."""
    raw = Path(path).read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(raw).hexdigest().lower()


def file_sha256(path: Path) -> str:
    return app_bootstrap.sha256_file(Path(path))


def installed_distribution_versions(names: tuple[str, ...] = RUNTIME_DISTRIBUTIONS) -> dict[str, str]:
    versions: dict[str, str] = {}
    for name in sorted(names, key=str.lower):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "missing"
    return versions


def compute_runtime_fingerprint(
    root: Path = ROOT,
    *,
    python_version: str | None = None,
    pyinstaller_version: str | None = None,
    third_party_packages: dict[str, str] | None = None,
    spec_path: Path | None = None,
    iss_path: Path | None = None,
    loader_paths: dict[str, Path] | None = None,
    bundled_modules: dict[str, list[str]] | None = None,
    dist_app_dir: Path | None = None,
) -> dict:
    """Compute the deterministic runtime fingerprint and its full input breakdown."""
    root = Path(root)
    py_ver = python_version or f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if pyinstaller_version is None:
        try:
            pyi_ver = importlib.metadata.version("pyinstaller")
        except importlib.metadata.PackageNotFoundError:
            pyi_ver = "unknown"
    else:
        pyi_ver = pyinstaller_version

    packages = (
        {k: str(v) for k, v in sorted(third_party_packages.items())}
        if third_party_packages is not None
        else installed_distribution_versions()
    )
    spec_file = Path(spec_path) if spec_path is not None else (root / "packaging" / "agentcompass-tray.spec")
    iss_file = Path(iss_path) if iss_path is not None else (root / "packaging" / "agentcompass.iss")
    spec_sha = normalized_file_sha256(spec_file)
    iss_sha = normalized_file_sha256(iss_file) if iss_file.is_file() else ""

    if loader_paths is not None:
        loaders = {rel: normalized_file_sha256(path) for rel, path in sorted(loader_paths.items())}
    else:
        loaders = {rel: normalized_file_sha256(root / rel) for rel in BOOT_LOADER_FILES}

    if bundled_modules is not None:
        modules_payload = {
            "tray": sorted(bundled_modules.get("tray", [])),
            "cli": sorted(bundled_modules.get("cli", [])),
            "excludes": sorted(bundled_modules.get("excludes", list(NON_RUNTIME_EXCLUDES))),
        }
    else:
        modules_payload = {
            "tray": tray_hidden_imports(),
            "cli": cli_hidden_imports(),
            "excludes": sorted(NON_RUNTIME_EXCLUDES),
        }
    bundled_modules_sha = hashlib.sha256(
        json.dumps(modules_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest().lower()

    runtime_assets = collect_runtime_asset_hashes(root, dist_app_dir=dist_app_dir)

    canonical_inputs = {
        "schema_version": 1,
        "python_version": py_ver,
        "pyinstaller_version": pyi_ver,
        "third_party_packages": packages,
        "spec_sha256": spec_sha,
        "iss_sha256": iss_sha,
        "boot_loader_sha256": loaders,
        "bundled_modules_sha256": bundled_modules_sha,
        "bundled_runtime_assets_sha256": runtime_assets,
    }
    encoded = json.dumps(canonical_inputs, sort_keys=True, separators=(",", ":")).encode("utf-8")
    fingerprint = hashlib.sha256(encoded).hexdigest().lower()

    return {
        **canonical_inputs,
        "bundled_module_counts": {
            "tray": len(modules_payload["tray"]),
            "cli": len(modules_payload["cli"]),
        },
        "runtime_fingerprint": fingerprint,
        "boot_exe_hash_policy": (
            "inputs_sha256: PyInstaller PE headers/archives include per-build timestamps and optional "
            "Authenticode signatures modify binary hashes; runtime_fingerprint hashes all deterministic "
            "inputs of AgentCompass.exe and cli/agentcompass.exe while boot_executables records the built binary hashes."
        ),
    }


def _copy_package_dir(root: Path, subdir: str, dest: Path, allowed_suffixes: set[str], exclude_names: set[str]) -> None:
    src = root / subdir
    tracked = stage_data.tracked_files(root, subdir)
    if tracked is None:
        candidates = sorted(src.rglob("*"))
    else:
        candidates = [root / rel for rel in sorted(tracked)]
    for path in candidates:
        if not path.is_file():
            continue
        if path.name.startswith("test_") or path.name in exclude_names:
            continue
        if path.suffix.lower() not in allowed_suffixes:
            continue
        rel = path.relative_to(src)
        if any(part in stage_data.SKIP_DIRS for part in rel.parts):
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def stage_app_tree(
    root: Path,
    dest: Path,
    *,
    version: str | None = None,
    runtime_info: dict | None = None,
    min_runtime_version: str | None = None,
) -> dict:
    """Populate ``dest`` with ``agentcompass/``, ``tray/``, ``skills/``, ``setup/`` and ``app-manifest.json``."""
    root = Path(root)
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)

    ver = version or read_version(root)
    rt_info = runtime_info or compute_runtime_fingerprint(root)
    rt_fp = str(rt_info["runtime_fingerprint"])

    _copy_package_dir(root, "agentcompass", dest / "agentcompass", {".py"}, {"conftest.py"})
    _copy_package_dir(root, "tray", dest / "tray", TRAY_RUNTIME_SUFFIXES, TRAY_EXCLUDE_NAMES)
    stage_data.copy_skills(root / "skills", dest / "skills")
    stage_data.copy_setup(root / "setup", dest / "setup")

    init_path = dest / "agentcompass" / "__init__.py"
    if init_path.is_file():
        init_text = init_path.read_text(encoding="utf-8")
        updated_init = re.sub(
            r'^(__version__\s*=\s*[\'"])([^\'"]+)([\'"])',
            rf"\g<1>{ver}\g<3>",
            init_text,
            count=1,
            flags=re.M,
        )
        if updated_init != init_text:
            init_path.write_bytes(updated_init.replace("\r\n", "\n").encode("utf-8"))

    files: dict[str, str] = {}
    for file_path in sorted(dest.rglob("*")):
        if not file_path.is_file():
            continue
        rel = file_path.relative_to(dest).as_posix()
        if rel == "app-manifest.json":
            continue
        files[rel] = file_sha256(file_path)

    app_manifest = {
        "schema_version": 1,
        "version": ver,
        "required_runtime_fingerprint": rt_fp,
        "runtime_requirements": {
            "runtime_fingerprint": rt_fp,
            "python_version": rt_info.get("python_version", ""),
            "pyinstaller_version": rt_info.get("pyinstaller_version", ""),
            **(
                {"min_runtime_version": min_runtime_version}
                if min_runtime_version
                else {}
            ),
        },
        "files": files,
    }
    app_bootstrap.write_json_atomic(dest / "app-manifest.json", app_manifest)
    return app_manifest


def build_app_package(
    root: Path,
    out_zip: Path | None = None,
    *,
    version: str | None = None,
    runtime_info: dict | None = None,
    min_runtime_version: str | None = None,
) -> dict:
    """Build ``app-<version>.zip`` and return its metadata dictionary."""
    root = Path(root)
    ver = version or read_version(root)
    rt_info = runtime_info or compute_runtime_fingerprint(root)
    if out_zip is None:
        out_zip = root / "dist" / f"app-{ver}.zip"
    out_zip = Path(out_zip)
    out_zip.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="agentcompass-app-pkg-") as tmp:
        staged = Path(tmp) / "app"
        app_manifest = stage_app_tree(
            root,
            staged,
            version=ver,
            runtime_info=rt_info,
            min_runtime_version=min_runtime_version,
        )
        tmp_zip = out_zip.with_name(f"{out_zip.name}.tmp")
        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for file_path in sorted(staged.rglob("*")):
                if not file_path.is_file():
                    continue
                rel = file_path.relative_to(staged).as_posix()
                info = zipfile.ZipInfo(rel, date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                archive.writestr(info, file_path.read_bytes())
        tmp_zip.replace(out_zip)

    sha = file_sha256(out_zip)
    size = out_zip.stat().st_size
    return {
        "name": out_zip.name,
        "path": str(out_zip),
        "version": ver,
        "size": size,
        "sha256": sha,
        "digest": f"sha256:{sha}",
        "required_runtime_fingerprint": app_manifest["required_runtime_fingerprint"],
        **(
            {"min_runtime_version": min_runtime_version}
            if min_runtime_version
            else {}
        ),
    }


def collect_boot_executables(dist_app_dir: Path) -> dict[str, dict]:
    dist_app_dir = Path(dist_app_dir)
    exes: dict[str, dict] = {}
    for rel in ("AgentCompass.exe", "cli/agentcompass.exe"):
        cand = dist_app_dir / rel
        if cand.is_file():
            exes[rel] = {
                "size": cand.stat().st_size,
                "sha256": file_sha256(cand),
            }
    return exes


def stage_installed_app(
    root: Path,
    dist_app_dir: Path,
    *,
    dist_dir: Path | None = None,
    version: str | None = None,
    runtime_info: dict | None = None,
) -> dict:
    """Populate ``dist/AgentCompass`` with ``runtime-info.json``, ``app/current/``, and ``app/packages/app-<version>.zip``."""
    root = Path(root)
    dist_app_dir = Path(dist_app_dir)
    dist_root = Path(dist_dir) if dist_dir is not None else dist_app_dir.parent
    ver = version or read_version(root)
    rt_info = dict(runtime_info or compute_runtime_fingerprint(root, dist_app_dir=dist_app_dir))
    rt_info["runtime_version"] = ver
    rt_info["boot_executables"] = collect_boot_executables(dist_app_dir)

    app_bootstrap.write_json_atomic(dist_app_dir / "runtime-info.json", rt_info)

    out_zip = dist_root / f"app-{ver}.zip"
    pkg_meta = build_app_package(root, out_zip, version=ver, runtime_info=rt_info)

    app_base = dist_app_dir / "app"
    if app_base.exists():
        shutil.rmtree(app_base)
    packages_dir = app_base / "packages"
    packages_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(out_zip, packages_dir / out_zip.name)

    current_dir = app_base / "current"
    current_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_zip) as archive:
        archive.extractall(current_dir)

    app_bootstrap.write_app_state(
        dist_app_dir,
        {
            "schema_version": 1,
            "current_version": ver,
            "active_version": ver,
            "previous_version": None,
            "active_package": f"packages/{out_zip.name}",
            "runtime_fingerprint": rt_info["runtime_fingerprint"],
        },
    )
    return {
        "version": ver,
        "runtime_info": rt_info,
        "app_package": pkg_meta,
    }


def build_release_manifest(
    root: Path,
    dist_dir: Path,
    *,
    version: str | None = None,
    runtime_info: dict | None = None,
    app_zip: Path | None = None,
    installer_exe: Path | None = None,
    min_runtime_version: str | None = None,
) -> dict:
    """Write ``dist/manifest.json`` (and ``dist/AgentCompass/manifest.json`` when present) and return it."""
    root = Path(root)
    dist_dir = Path(dist_dir)
    dist_dir.mkdir(parents=True, exist_ok=True)
    ver = version or read_version(root)

    dist_app_dir = dist_dir / "AgentCompass"
    if runtime_info is None:
        existing_rt = app_bootstrap.read_runtime_info(dist_app_dir)
        runtime_info = existing_rt or compute_runtime_fingerprint(root, dist_app_dir=dist_app_dir)
    else:
        runtime_info = dict(runtime_info)

    if dist_app_dir.is_dir():
        runtime_info["boot_executables"] = collect_boot_executables(dist_app_dir)
        app_bootstrap.write_json_atomic(dist_app_dir / "runtime-info.json", runtime_info)

    rt_fp = str(runtime_info["runtime_fingerprint"])

    zip_path = Path(app_zip) if app_zip is not None else (dist_dir / f"app-{ver}.zip")
    if not zip_path.is_file():
        pkg_meta = build_app_package(
            root,
            zip_path,
            version=ver,
            runtime_info=runtime_info,
            min_runtime_version=min_runtime_version,
        )
    else:
        sha = file_sha256(zip_path)
        pkg_meta = {
            "name": zip_path.name,
            "size": zip_path.stat().st_size,
            "sha256": sha,
            "digest": f"sha256:{sha}",
            "required_runtime_fingerprint": rt_fp,
            **(
                {"min_runtime_version": min_runtime_version}
                if min_runtime_version
                else {}
            ),
        }

    setup_path = Path(installer_exe) if installer_exe is not None else (dist_dir / "AgentCompass-tray-setup.exe")
    installer_meta = None
    if setup_path.is_file():
        setup_sha = file_sha256(setup_path)
        installer_meta = {
            "name": setup_path.name,
            "size": setup_path.stat().st_size,
            "sha256": setup_sha,
            "digest": f"sha256:{setup_sha}",
            "runtime_fingerprint": rt_fp,
        }

    manifest = {
        "schema_version": 1,
        "version": ver,
        "tag_name": f"v{ver}",
        "runtime_fingerprint": rt_fp,
        "runtime": {
            "python_version": runtime_info.get("python_version", ""),
            "pyinstaller_version": runtime_info.get("pyinstaller_version", ""),
            "third_party_packages": runtime_info.get("third_party_packages", {}),
            "spec_sha256": runtime_info.get("spec_sha256", ""),
            "iss_sha256": runtime_info.get("iss_sha256", ""),
            "boot_loader_sha256": runtime_info.get("boot_loader_sha256", {}),
            "bundled_modules_sha256": runtime_info.get("bundled_modules_sha256", ""),
            "bundled_runtime_assets_sha256": runtime_info.get("bundled_runtime_assets_sha256", {}),
            "boot_exe_hash_policy": runtime_info.get("boot_exe_hash_policy", ""),
            "boot_executables": runtime_info.get("boot_executables", {}),
        },
        "app_package": {
            "name": pkg_meta["name"],
            "size": pkg_meta["size"],
            "sha256": pkg_meta["sha256"],
            "digest": pkg_meta["digest"],
            "required_runtime_fingerprint": pkg_meta["required_runtime_fingerprint"],
            **(
                {"min_runtime_version": pkg_meta["min_runtime_version"]}
                if pkg_meta.get("min_runtime_version")
                else {}
            ),
        },
        "installer": installer_meta,
    }
    app_bootstrap.write_json_atomic(dist_dir / "manifest.json", manifest)
    if dist_app_dir.is_dir():
        app_bootstrap.write_json_atomic(dist_app_dir / "manifest.json", manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_stage = sub.add_parser("stage", help="Stage runtime-info.json, app-<version>.zip, and app/current into dist/AgentCompass.")
    p_stage.add_argument("--root", type=Path, default=ROOT)
    p_stage.add_argument("--dist", type=Path, default=ROOT / "dist")
    p_stage.add_argument("--version", type=str, default=None)

    p_manifest = sub.add_parser("build-manifest", help="Write dist/manifest.json after Inno Setup and optional signing.")
    p_manifest.add_argument("--root", type=Path, default=ROOT)
    p_manifest.add_argument("--dist", type=Path, default=ROOT / "dist")
    p_manifest.add_argument("--version", type=str, default=None)

    args = parser.parse_args(argv)
    if args.command == "stage":
        stage_installed_app(args.root, args.dist / "AgentCompass", dist_dir=args.dist, version=args.version)
        build_release_manifest(args.root, args.dist, version=args.version)
        return 0
    if args.command == "build-manifest":
        manifest = build_release_manifest(args.root, args.dist, version=args.version)
        print(f"manifest.json written: version={manifest['version']} runtime_fingerprint={manifest['runtime_fingerprint'][:12]}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
