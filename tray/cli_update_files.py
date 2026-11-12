"""Retain locked npm native images without owning or stopping their processes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import stat
import uuid


NATIVE_SUFFIXES = {".exe", ".dll", ".node"}
RETIRED_DIRECTORY = ".agentcompass-cli-retired"
RETIRED_NAME = re.compile(r"^\..+\.agentcompass-old-[0-9a-f]{32}$")
PACKAGE_NAME = re.compile(r"^(?:@[A-Za-z0-9_][\w.-]*/)?[A-Za-z0-9_][\w.-]*$")


def reparse_point(path: Path) -> bool:
    return path.is_symlink() or (os.name == "nt" and bool(
        path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT))


def npm_roots(package: Path, prefix: Path) -> list[Path]:
    """The package plus its installed dependency closure, including hoisted optional binaries.

    Never walk the whole global prefix: other tools can share its node_modules.
    npm links/junctions to external packages are left to their owning installer.
    """
    boundary = prefix.resolve()
    pending, roots, seen = [package], [], set()
    while pending:
        root = pending.pop()
        resolved = root.resolve()
        if resolved in seen or not resolved.is_relative_to(boundary) or reparse_point(root):
            continue
        seen.add(resolved)
        roots.append(root)
        manifest = root / "package.json"
        if not manifest.is_file():
            continue
        data = json.loads(manifest.read_text(encoding="utf-8-sig"))
        names = set(data.get("dependencies", {})) | set(data.get("optionalDependencies", {}))
        for name in names:
            if not PACKAGE_NAME.fullmatch(name):
                raise ValueError(f"Invalid npm dependency name: {name}")
            # Resolve like Node: nearest node_modules first, then ancestors.
            for parent in (root, *root.parents):
                if not parent.is_relative_to(boundary):
                    break
                candidate = parent / "node_modules" / name
                if candidate.is_dir():
                    pending.append(candidate)
                    break
    return roots


def retain_locked(path: Path, directory: Path, remember) -> Path | None:
    """Move an unwritable image aside, restoring an unlocked copy for npm to replace.

    Backups live outside node_modules, so npm's package retirement cannot remove
    them. A unique name also supports several updates during one long session.
    An inability to rename, copy or record the image is an update failure.
    """
    try:
        with path.open("r+b"):
            return None
    except PermissionError:
        pass
    directory.mkdir(parents=True, exist_ok=True)
    backup = directory / f".{path.name}.agentcompass-old-{uuid.uuid4().hex}"
    path.rename(backup)
    try:
        shutil.copy2(backup, path)
        remember(backup)
    except Exception:
        # Restore the original pathname even if copying left a partial file.
        path.unlink(missing_ok=True)
        backup.rename(path)
        raise
    return backup


def prepare_npm(package: Path, prefix: Path, remember) -> None:
    """Free only locked Windows images in this install and its dependencies."""
    if os.name != "nt":
        return
    visited = set()
    for root in npm_roots(package, prefix):
        def walk_error(error):
            raise error
        for folder, dirs, files in os.walk(root, onerror=walk_error, followlinks=False):
            dirs[:] = [name for name in dirs if not reparse_point(Path(folder) / name)]
            for name in files:
                path = Path(folder) / name
                if path.suffix.lower() not in NATIVE_SUFFIXES or path in visited or path.is_symlink():
                    continue
                visited.add(path)
                retain_locked(path, prefix / RETIRED_DIRECTORY, remember)


def remove_retired(path: Path) -> bool:
    """Delete only our retained files; Windows protects images still in use."""
    if path.parent.name != RETIRED_DIRECTORY or not RETIRED_NAME.fullmatch(path.name):
        raise ValueError(f"Not a retained CLI image: {path}")
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    try:
        path.parent.rmdir()  # Only an empty directory; never recursive deletion.
    except OSError:
        pass
    return True
