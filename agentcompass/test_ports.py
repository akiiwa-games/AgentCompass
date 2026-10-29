import json
import unittest
from pathlib import Path

from agentcompass.ports import DEFAULTS, conflicts, get, load


class PortTests(unittest.TestCase):
    def test_defaults_match_the_running_services(self):
        self.assertEqual(DEFAULTS["dot2api"], 8788)
        self.assertEqual(DEFAULTS["chatgpt_web"], 17843)
        self.assertEqual(DEFAULTS["gemini_web"], 17849)
        self.assertEqual(DEFAULTS["account_login"], 17851)
        self.assertEqual(DEFAULTS["easytier_rpc"], 15888)
        self.assertEqual(DEFAULTS["muse2api"], 18610)

    def test_env_beats_file_and_file_beats_default_without_writing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before = set(root.rglob("*"))
            path = root / "ports.json"
            path.write_text(json.dumps({"dot2api": 9000, "muse2api": "nope"}), encoding="utf-8")
            environ = {
                "AGENTCOMPASS_PORTS_FILE": str(path),
                "AGENTCOMPASS_PORT_DOT2API": "9001",
                "DOT2API_PORT": "1",
            }
            state = load(root, environ)
            self.assertEqual(state["ports"]["dot2api"], 9001)
            self.assertEqual(state["ports"]["chatgpt_web"], 17843)
            self.assertEqual(state["ports"]["muse2api"], 18610)
            self.assertTrue(any("muse2api" in item for item in state["problems"]))
            self.assertEqual(get("muse2api", root, {}), 18610)
            self.assertEqual(set(root.rglob("*")), before | {path})
            path.unlink()
            self.assertFalse(path.exists())

    def test_duplicate_assignments_are_conflicts_and_bad_env_keeps_the_previous_value(self):
        environ = {"AGENTCOMPASS_PORT_MUSE2API": "8788", "AGENTCOMPASS_PORT_DOT2API": "nope"}
        found = conflicts(Path("unused"), environ)
        self.assertTrue(any("8788" in item and "dot2api" in item and "muse2api" in item for item in found))
        self.assertTrue(any("AGENTCOMPASS_PORT_DOT2API" in item for item in found))
        self.assertEqual(get("dot2api", Path("unused"), {"AGENTCOMPASS_PORT_DOT2API": "nope"}), 8788)


if __name__ == "__main__":
    unittest.main()
