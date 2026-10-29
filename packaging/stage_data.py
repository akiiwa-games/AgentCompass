"""Copy skill and setup sources into a wheel staging tree.

Skills stay real files (hosts read SKILL.md; route_judge.py loads siblings by path).
The staged setup directory sits next to staged skills so
``Path(__file__).parents[1] / "skills"`` inside a bundled setup script still resolves.

``setup/host_sync/out`` and ``node_modules`` are never copied.
"""

from __future__ import annotations

import shutil
from pathlib import Path

SKIP_DIRS = {"__pycache__", "node_modules", "out", ".pytest_cache", ".staged", ".build"}
PASEO_BRIDGE_FILES = ("package.json", "package-lock.json", "README.md")


def staged_root(root: Path) -> Path:
    return Path(root) / "packaging" / ".staged"


def _copy_file(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)


def copy_skills(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    for path in src.rglob("*"):
        relative = path.relative_to(src)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if path.is_dir():
            continue
        if path.suffix == ".pyc":
            continue
        _copy_file(path, dest / relative)


def copy_setup(src: Path, dest: Path) -> None:
    """Top-level and nested ``*.py`` / ``*.mjs``, plus the paseo-bridge manifest files."""
    if dest.exists():
        shutil.rmtree(dest)
    for path in src.rglob("*"):
        relative = path.relative_to(src)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if path.is_dir():
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
