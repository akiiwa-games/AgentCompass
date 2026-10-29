"""Copy skill and setup sources into a wheel staging tree.

Skills stay real files (hosts read SKILL.md; route_judge.py loads siblings by path).
The staged setup directory sits next to staged skills so
``Path(__file__).parents[1] / "skills"`` inside a bundled setup script still resolves.

What may ship is decided by git, not by what happens to sit on disk. In a checkout only files
``git ls-files`` reports as tracked are candidates, so git-ignored host files (installed runtimes,
private notes, legacy sync scripts) and untracked scratch files never reach a wheel. An sdist
carries no ``.git``; hatch already built it from the same tracked and unignored set, and the same
extension and directory rules below apply to it.

``setup/host_sync`` (legacy host-sync scripts), ``setup/host_sync/out`` and ``node_modules`` are never copied.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

SKIP_DIRS = {"__pycache__", "node_modules", "out", ".pytest_cache", ".staged", ".build"}
LEGACY_SETUP_DIRS = {"host_sync"}
PASEO_BRIDGE_FILES = ("package.json", "package-lock.json", "README.md")


def staged_root(root: Path) -> Path:
    return Path(root) / "packaging" / ".staged"


def _copy_file(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)


def tracked_files(root: Path, subdir: str) -> set[Path] | None:
    """Paths under ``subdir`` (relative to ``root``) that git tracks, or None outside a git checkout."""
    root = Path(root)
    if not (root / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--", subdir],
            capture_output=True, check=False,
        )
    except OSError as error:
        raise RuntimeError(f"{root} is a git checkout but git cannot be run to list its tracked files: {error}") from error
    if result.returncode != 0:
        raise RuntimeError(f"git ls-files failed in {root}: {result.stderr.decode(errors='replace').strip()}")
    return {Path(name.decode("utf-8")) for name in result.stdout.split(b"\0") if name}


def _candidates(src: Path) -> list[Path]:
    """Source files below ``src``: the tracked ones in a checkout, every file in an sdist."""
    src = Path(src)
    root = src.parent
    tracked = tracked_files(root, src.name)
    if tracked is None:
        return [path for path in sorted(src.rglob("*")) if path.is_file()]
    return [root / name for name in sorted(tracked) if (root / name).is_file()]


def copy_skills(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    for path in _candidates(src):
        relative = path.relative_to(src)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if path.suffix == ".pyc":
            continue
        _copy_file(path, dest / relative)


def copy_setup(src: Path, dest: Path) -> None:
    """Tracked top-level and nested ``*.py`` / ``*.mjs``, plus the paseo-bridge manifest files."""
    if dest.exists():
        shutil.rmtree(dest)
    for path in _candidates(src):
        relative = path.relative_to(src)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if relative.parts[0] in LEGACY_SETUP_DIRS:
            continue
        bridge = relative.parts[:1] == ("paseo-bridge",)
        if bridge:
            if path.name not in PASEO_BRIDGE_FILES or len(relative.parts) != 2:
                continue
        elif path.suffix not in {".py", ".mjs"}:
            continue
        _copy_file(path, dest / relative)


def stage(root: Path, dest: Path | None = None) -> Path:
    root = Path(root)
    dest = Path(dest) if dest is not None else staged_root(root)
    dest.mkdir(parents=True, exist_ok=True)
    copy_skills(root / "skills", dest / "skills")
    copy_setup(root / "setup", dest / "setup")
    return dest
