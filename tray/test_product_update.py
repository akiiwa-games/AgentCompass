import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent))

import updates

REPO = Path(__file__).resolve().parents[1]


class _Body:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class ProductNoticeTests(unittest.TestCase):
    def test_a_checkout_with_git_does_not_change_the_joined_update_result(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").write_text("gitdir: somewhere\n", encoding="utf-8")

            def boom(*args, **kwargs):
                raise AssertionError("release check ran for a checkout")

            self.assertEqual(updates.product_notice(root, opener=boom), "")

    def test_a_packaged_tree_points_at_the_public_release(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "agentcompass"
            package.mkdir()
            (package / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
            if str(REPO) not in sys.path:
                sys.path.insert(0, str(REPO))
            seen = {}

            def opener(request, timeout=20):
                seen["url"] = request.full_url
                seen["agent"] = request.get_header("User-agent")
                return _Body({
                    "tag_name": "v9.9.9",
                    "html_url": "https://github.com/akiiwa-games/AgentCompass/releases/tag/v9.9.9",
                })

            text = updates.product_notice(root, opener=opener)
            self.assertIn("akiiwa-games/AgentCompass", seen["url"])
            self.assertEqual(seen["agent"], "agentcompass")
            self.assertIn("uv tool upgrade agentcompass", text)
            self.assertIn("v9.9.9", text)
            self.assertNotIn("agent-compass", text)


if __name__ == "__main__":
    unittest.main()
