"""Fail when a tracked file carries personal content: a real e-mail address, a credential, or a personal identifier.

Generic patterns are public and live here. Personal identifiers (user names, private project names, machine paths) must
never be written into this public repository, so they come from a denylist outside it, one literal per line:
``--denylist FILE``, else ``$AGENTCOMPASS_PRIVACY_DENYLIST``, else ``~/.agent-compass/privacy-denylist.txt`` when it exists.
It runs locally as the first step of ``packaging/verify_release.py`` before a release; the denylist never leaves this machine.

Prints only file:line and the kind of hit, never the matched personal text.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESERVED_MAIL = re.compile(r"@(?:[\w-]+\.)*(?:example|test|invalid|localhost)(?:\.\w+)?$|@(?:users\.)?noreply\.github\.com$|^noreply@anthropic\.com$|^git@github\.com$", re.I)
MAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SECRETS = {
    "api key": re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{24,}"),
    "GitHub token": re.compile(r"\b(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{30,}"),
    "AWS key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
}


def tracked(root: Path = ROOT) -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True).stdout
    return [p for p in out.decode("utf-8").split("\0") if p]


def denylist(path: Path | None) -> list[str]:
    if path is None:
        env = os.environ.get("AGENTCOMPASS_PRIVACY_DENYLIST")
        path = Path(env) if env else Path.home() / ".agent-compass" / "privacy-denylist.txt"
        if not path.is_file():
            return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def problems(files: list[str], denied: list[str], root: Path = ROOT) -> list[str]:
    found = []
    lowered = [(d, d.lower()) for d in denied]
    for rel in files:
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            low = line.lower()
            for index, (_, needle) in enumerate(lowered, 1):
                if needle in low:
                    found.append(f"{rel}:{number}: denylist entry #{index}")
            for mail in MAIL.findall(line):
                if not RESERVED_MAIL.search(mail):
                    found.append(f"{rel}:{number}: e-mail address outside the reserved example domains")
            for kind, pattern in SECRETS.items():
                if pattern.search(line):
                    found.append(f"{rel}:{number}: {kind}")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--denylist", type=Path, default=None)
    args = parser.parse_args(argv)
    denied = denylist(args.denylist)
    found = problems(tracked(), denied)
    for line in found:
        print(line)
    print(f"privacy check: {len(found)} hit(s) in tracked files ({len(denied)} denylist entries)")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
