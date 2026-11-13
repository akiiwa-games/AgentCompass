"""Shipped instructions never tell an agent to run a script file; they name `agentcompass`, and the repository paths they cite exist."""
import re
import subprocess
import unittest

from agentcompass import locations

OPERATIONAL = re.compile(
    r"python[^\n`]*route_judge\.py"                                    # python .../route_judge.py
    r"|(?<![\w/\[`.])route_judge\.py\s+(?:roll|plan|providers|record|score|scorer|delegate|select|override|dispatch|launch|usage)\b"
    r"|dispatch-state")                                                 # a stage that no longer exists

FENCE = re.compile(r"^(```|~~~).*?^\1[ \t]*$", re.MULTILINE | re.DOTALL)
LINK = re.compile(r"(?<!!)\[[^\]\n]*\]\(\s*<?([^)\s>]+)")
CODE_SPAN = re.compile(r"`([^`\n]+)`")
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
PATH_CHARS = re.compile(r"[A-Za-z0-9_./-]+")
LINE_SUFFIX = re.compile(r"(?::\d+)+$")


def link_targets(text):
    """Relative link targets of a Markdown text: not a URL, not a same-page anchor; fragment and query removed."""
    for target in LINK.findall(FENCE.sub("", text)):
        if SCHEME.match(target) or target.startswith(("#", "/")):
            continue
        target = target.split("#", 1)[0].split("?", 1)[0]
        if target:
            yield target


def cited_paths(text, top_levels):
    """Backticked repository paths: a token of an inline code span that starts with a tracked top-level directory.
    A token with other characters (placeholder, glob, brace list, option) is not a file name and is not a path."""
    for span in CODE_SPAN.findall(FENCE.sub("", text)):
        for token in span.replace("\\", "/").split():
            token = LINE_SUFFIX.sub("", token.rstrip(".,;:)"))
            if "/" in token and token.split("/", 1)[0] in top_levels and PATH_CHARS.fullmatch(token):
                yield token


def normalise(path):
    """The repository-relative form of a joined path; None when it climbs out of the repository."""
    parts = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


class DocsTests(unittest.TestCase):
    def setUp(self):
        self.root = locations.checkout_root()
        if self.root is None:
            self.skipTest("not a source checkout")

    def documents(self):
        skill = self.root / "skills" / "agent-compass"
        yield from (self.root / "README.md", self.root / "llms.txt", skill / "SKILL.md", skill / "agents" / "openai.yaml")
        yield from sorted((self.root / "docs").glob("*.md"))
        yield from sorted((skill / "references").glob("*.md"))

    def lines(self):
        for path in self.documents():
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                yield f"{path.relative_to(self.root).as_posix()}:{number}", line

    def tracked(self):
        try:
            listing = subprocess.run(["git", "-C", str(self.root), "ls-files"], capture_output=True, text=True,
                                     encoding="utf-8", check=True).stdout
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("git file list unavailable")
        return set(listing.splitlines())

    def test_no_document_teaches_running_route_judge_directly(self):
        found = [f"{where}: {line.strip()[:120]}" for where, line in self.lines() if OPERATIONAL.search(line)]
        self.assertEqual(found, [])

    def test_link_and_path_extraction_follows_the_rule(self):
        text = ("[a](docs/x.md#top) [b](https://example.org/x) [c](#here) ![i](hero.gif) [d](<../y.md>)\n"
                "`tray/app.pyw:12` `skills/<name>/SKILL.md` `~/.agent-compass/x` `tray\\deploy.py` `setup/*.py` "
                "`python docs/a.md --flag` `usage-float/` `tray/`\n```\n[e](nope.md) `tray/ghost.py`\n```\n")
        self.assertEqual(list(link_targets(text)), ["docs/x.md", "../y.md"])
        self.assertEqual(list(cited_paths(text, {"docs", "setup", "skills", "tray"})),
                         ["tray/app.pyw", "tray/deploy.py", "docs/a.md", "tray/"])

    def test_every_repository_path_a_document_cites_exists(self):
        """Tracked Markdown and llms.txt: a relative link target, or a backticked path under a tracked top-level
        directory, names a tracked file or directory. Home-directory, install, URL and placeholder text is not a path."""
        tracked = self.tracked()
        top_levels = {name.split("/", 1)[0] for name in tracked if "/" in name}
        known = set(tracked)
        for name in tracked:
            parts = name.split("/")[:-1]
            known.update("/".join(parts[:index]) for index in range(1, len(parts) + 1))
        missing = []
        for name in sorted(tracked):
            if not (name.endswith(".md") or name == "llms.txt"):
                continue
            text = (self.root / name).read_text(encoding="utf-8")
            base = name.rsplit("/", 1)[0] + "/" if "/" in name else ""
            for target in link_targets(text):
                resolved = normalise(base + target)
                if resolved is None or resolved not in known:
                    missing.append(f"{name}: link {target}")
            for path in cited_paths(text, top_levels):
                if normalise(path) not in known:
                    missing.append(f"{name}: `{path}`")
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
