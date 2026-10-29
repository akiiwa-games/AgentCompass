"""Fail when a release payload holds a file git does not track.

    python packaging/check_payload.py [--wheel X.whl] [--sdist X.tar.gz] [--toc Analysis-00.toc]

``--toc`` is the PyInstaller analysis table under ``packaging/.build``: every module it took from
the repository (tray scripts, the agentcompass package) must be tracked. The one-folder tray that
Inno Setup packs is built from that table, so a clean table means a clean installer payload.
Run it against a build made from a clean checkout; the exit code is the number of problems found.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
WHEEL_DATA = {"agentcompass/data/skills/": "skills/", "agentcompass/data/setup/": "setup/"}


def tracked(root: Path = ROOT) -> set[str]:
    result = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True)
    return {name.decode("utf-8") for name in result.stdout.split(b"\0") if name}


def wheel_problems(wheel: Path, files: set[str]) -> list[str]:
    problems = []
    for name in zipfile.ZipFile(wheel).namelist():
        if name.endswith("/") or ".dist-info/" in name:
            continue
        source = name
        for prefix, repo in WHEEL_DATA.items():
            if name.startswith(prefix):
                source = repo + name[len(prefix):]
        if source not in files:
            problems.append(f"{Path(wheel).name}: {name} is not tracked in git")
    return problems


def sdist_problems(sdist: Path, files: set[str]) -> list[str]:
    problems = []
    with tarfile.open(sdist) as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            parts = PurePosixPath(member.name).parts
            relative = "/".join(parts[1:])
            if relative and relative != "PKG-INFO" and relative not in files:
                problems.append(f"{Path(sdist).name}: {relative} is not tracked in git")
    return problems


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def toc_problems(toc: Path, files: set[str], root: Path = ROOT) -> list[str]:
    """Repository files PyInstaller pulled into the tray bundle."""
    problems = []
    root = Path(root).resolve()
    seen = set()
    for source in _strings(ast.literal_eval(Path(toc).read_text(encoding="utf-8"))):
        if not Path(source).is_absolute():
            continue
        try:
            relative = Path(source).resolve().relative_to(root).as_posix()
        except (OSError, ValueError):
            continue
        if relative in seen or relative.startswith(("packaging/.build/", "packaging/.staged/", "dist/")):
            continue
        seen.add(relative)
        if relative not in files and (root / relative).is_file():
            problems.append(f"{Path(toc).name}: {relative} is not tracked in git")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--sdist", type=Path)
    parser.add_argument("--toc", type=Path)
    parser.add_argument("--root", type=Path, default=ROOT, help="the checkout the build came from")
    args = parser.parse_args(argv)
    files = tracked(args.root)
    problems: list[str] = []
    if args.wheel:
        problems += wheel_problems(args.wheel, files)
    if args.sdist:
        problems += sdist_problems(args.sdist, files)
    if args.toc:
        problems += toc_problems(args.toc, files, args.root)
    for line in problems:
        print(line)
    print(f"{len(problems)} problem(s)")
    return len(problems)


if __name__ == "__main__":
    sys.exit(main())
