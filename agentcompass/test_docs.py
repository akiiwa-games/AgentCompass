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
LINK = re.compile(r"\[[^\]\n]*\]\(\s*<?([^)\s>]+)")
HTML_ATTRIBUTE = re.compile(r"""\b(?:src|href)\s*=\s*["']([^"']+)["']""")
HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(.+?)[ \t#]*$", re.MULTILINE)
CODE_SPAN = re.compile(r"`([^`\n]+)`")
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
PATH_CHARS = re.compile(r"[A-Za-z0-9_./-]+")
LINE_SUFFIX = re.compile(r"(?::\d+)+$")


def link_targets(text):
    """(path, fragment) of every Markdown link and HTML src/href in a text, outside fenced code (code is not markup).
    URLs are skipped; a root-relative target is read from the repository root; a same-page anchor has an empty path."""
    prose = FENCE.sub("", text)
    for target in LINK.findall(prose) + HTML_ATTRIBUTE.findall(prose):
        if SCHEME.match(target) or target.startswith("//"):
            continue
        target, _, fragment = target.partition("#")
        yield target.split("?", 1)[0], fragment


def cited_paths(text, top_levels):
    """Repository paths in code: a token of an inline code span or a fenced block that starts with a tracked top-level
    directory. A token with other characters (placeholder, glob, brace list, option) is not a file name and is not a
    path; a `#fragment` or `:line` suffix is not part of the path."""
    spans = CODE_SPAN.findall(FENCE.sub("", text)) + [m.group(0) for m in FENCE.finditer(text)]
    for span in spans:
        for token in span.replace("\\", "/").split():
            token = token.strip("\"'(").rstrip("\"').,;:").split("#", 1)[0]
            token = LINE_SUFFIX.sub("", token)
            if "/" in token and token.split("/", 1)[0] in top_levels and PATH_CHARS.fullmatch(token):
                yield token


def anchors(text):
    """GitHub heading anchors of a Markdown text: lower case, markup and punctuation dropped, spaces to hyphens,
    repeated headings numbered."""
    found, seen = set(), {}
    for heading in HEADING.findall(FENCE.sub("", text)):
        heading = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading).replace("`", "")
        slug = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        found.add(slug if not count else f"{slug}-{count}")
    return found


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

    def repository_files(self):
        """Tracked and not-ignored new files that exist on disk now: a file deleted in the working tree is not one."""
        try:
            listing = subprocess.run(["git", "-C", str(self.root), "ls-files", "--cached", "--others", "--exclude-standard"],
                                     capture_output=True, text=True, encoding="utf-8", check=True).stdout
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("git file list unavailable")
        return {name for name in listing.splitlines() if (self.root / name).is_file()}

    def test_no_document_teaches_running_route_judge_directly(self):
        found = [f"{where}: {line.strip()[:120]}" for where, line in self.lines() if OPERATIONAL.search(line)]
        self.assertEqual(found, [])

    def test_link_and_path_extraction_follows_the_rule(self):
        text = ("[a](docs/x.md#top) [b](https://example.org/x) [c](#here) ![i](hero.gif) [d](<../y.md>) [r](/setup/z.py)\n"
                '<img src="docs/p.png"> `tray/app.pyw:12` `skills/<name>/SKILL.md` `~/.agent-compass/x` `tray\\deploy.py` '
                "`setup/*.py` `python docs/a.md --flag` `usage-float/` `tray/` `docs/b.md#sec`\n"
                "```\n[e](nope.md) python tray/ghost.py --x\n```\n")
        self.assertEqual(list(link_targets(text)),
                         [("docs/x.md", "top"), ("", "here"), ("hero.gif", ""), ("../y.md", ""), ("/setup/z.py", ""), ("docs/p.png", "")])
        self.assertEqual(list(cited_paths(text, {"docs", "setup", "skills", "tray"})),
                         ["tray/app.pyw", "tray/deploy.py", "docs/a.md", "tray/", "docs/b.md", "tray/ghost.py"])
        self.assertEqual(anchors("# A `b` [c](d)\n## A `b` [c](d)\n## 1. How & Why\n"), {"a-b-c", "a-b-c-1", "1-how--why"})

    def test_every_repository_path_a_document_cites_exists(self):
        """Every tracked Markdown file and llms.txt, against the files on disk. A relative or root-relative link or HTML
        src/href must name an existing file or directory, and a `#fragment` on a Markdown target must be one of its
        headings. A token of a code span or fenced block that starts with a tracked top-level directory must exist too.
        Out of scope by rule: URLs, home-directory and install paths, and tokens that are not plain file names
        (placeholders, globs, options). Links inside fenced code are code, not links."""
        files = self.repository_files()
        top_levels = {name.split("/", 1)[0] for name in files if "/" in name}
        known = set(files)
        for name in files:
            parts = name.split("/")[:-1]
            known.update("/".join(parts[:index]) for index in range(1, len(parts) + 1))
        missing = []
        for name in sorted(files):
            if not (name.endswith(".md") or name == "llms.txt"):
                continue
            text = (self.root / name).read_text(encoding="utf-8")
            base = name.rsplit("/", 1)[0] + "/" if "/" in name else ""
            for target, fragment in link_targets(text):
                shown = target + ("#" + fragment if fragment else "")
                resolved = name if not target else normalise(target[1:] if target.startswith("/") else base + target)
                if resolved is None or resolved not in known:
                    missing.append(f"{name}: link {shown}")
                elif fragment and resolved.endswith(".md") and resolved in files:
                    if fragment.lower() not in anchors((self.root / resolved).read_text(encoding="utf-8")):
                        missing.append(f"{name}: link {shown} (no such heading)")
            for path in cited_paths(text, top_levels):
                if normalise(path) not in known:
                    missing.append(f"{name}: `{path}`")
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
