import os
import unittest

from agentcompass.origin import DEFAULT_ORG, RELEASES_API, github_org, sibling_repo, usage_float_repo


class OriginTests(unittest.TestCase):
    def test_packaged_fallback_is_the_public_org(self):
        environ = {}
        self.assertEqual(github_org(environ), DEFAULT_ORG)
        self.assertEqual(DEFAULT_ORG, "akiiwa-games")
        self.assertEqual(sibling_repo("usage-float", environ), "https://github.com/akiiwa-games/usage-float.git")
        self.assertNotIn("agent-compass", sibling_repo("gemini-web", environ))
        self.assertIn("akiiwa-games/AgentCompass", RELEASES_API)

    def test_env_org_and_usage_float_url_win(self):
        environ = {
            "AGENTCOMPASS_GITHUB_ORG": "override-org",
            "AGENTCOMPASS_USAGE_FLOAT_REPO": "https://example.test/usage-float.git",
        }
        self.assertEqual(github_org(environ, git_config="github.com/from-git/AgentCompass"), "override-org")
        self.assertEqual(usage_float_repo(environ), "https://example.test/usage-float.git")

    def test_git_config_org_is_used_when_env_is_empty(self):
        config = '[remote "origin"]\n\turl = git@github.com:fork-org/AgentCompass.git\n'
        self.assertEqual(sibling_repo("usage-float", {}, config), "https://github.com/fork-org/usage-float.git")

    def test_pre_rename_variables_still_work(self):
        self.assertEqual(github_org({"ROUTE_MANAGER_GITHUB_ORG": "old-org"}), "old-org")
        self.assertEqual(github_org({"AGENTCOMPASS_GITHUB_ORG": "new-org", "ROUTE_MANAGER_GITHUB_ORG": "old-org"}), "new-org")

    def test_usage_float_without_git_config_stays_on_the_public_org(self):
        keys = ("AGENTCOMPASS_GITHUB_ORG", "AGENTCOMPASS_USAGE_FLOAT_REPO", "ROUTE_MANAGER_GITHUB_ORG",
                "ROUTE_MANAGER_USAGE_FLOAT_REPO")
        saved = [os.environ.pop(key, None) for key in keys]
        try:
            self.assertEqual(usage_float_repo({}), "https://github.com/akiiwa-games/usage-float.git")
        finally:
            for key, value in zip(keys, saved):
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
