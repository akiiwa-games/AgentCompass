"""Shipped instructions never tell an agent to run a script file; they name `agentcompass`."""
import re
import unittest

from agentcompass import locations

OPERATIONAL = re.compile(
    r"python[^\n`]*route_judge\.py"                                    # python .../route_judge.py
    r"|(?<![\w/\[`.])route_judge\.py\s+(?:roll|plan|providers|record|score|scorer|delegate|select|override|dispatch|launch|usage)\b"
    r"|dispatch-state")                                                 # a stage that no longer exists



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

    def test_no_document_teaches_running_route_judge_directly(self):
        found = [f"{where}: {line.strip()[:120]}" for where, line in self.lines() if OPERATIONAL.search(line)]
        self.assertEqual(found, [])



if __name__ == "__main__":
    unittest.main()
