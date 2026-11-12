import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import privacy_check


class PrivacyCheckTests(unittest.TestCase):
    def scan(self, text, denied=()):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "f.py").write_text(text, encoding="utf-8")
            return privacy_check.problems(["f.py"], list(denied), root=Path(tmp))

    def test_a_denylist_entry_is_reported_by_number_without_its_text(self):
        found = self.scan("cwd = 'D:/Secret-Project/x'\n", ["secret-project"])
        self.assertEqual(found, ["f.py:1: denylist entry #1"])

    def test_reserved_example_mail_passes_and_a_real_one_fails(self):
        self.assertEqual(self.scan("a = 'user@example.test' b = 'noreply@anthropic.com'\n"), [])
        self.assertEqual(len(self.scan("owner = 'someone" + "@" + "gmail.com'\n")), 1)

    def test_credentials_are_reported(self):
        self.assertEqual(len(self.scan("key = 'sk-ant-" + "a" * 30 + "'\n")), 1)
        self.assertEqual(len(self.scan("-----BEGIN OPENSSH " + "PRIVATE KEY-----\n")), 1)


if __name__ == "__main__":
    unittest.main()
