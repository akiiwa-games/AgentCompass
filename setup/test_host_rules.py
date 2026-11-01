"""The rules written to every agent host name the public `agentcompass` command, never a script path."""
import tempfile
import unittest
from pathlib import Path

import host_rules


class HostRulesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)

    def test_the_shipped_defaults_teach_only_the_public_command(self):
        command = host_rules.agentcompass_command()
        for kind in host_rules.OVERLAY_FILES:
            text, source = host_rules.load(kind, self.home)
            self.assertEqual(source, 'default')
            self.assertIn(f'{command} roll --task', text, kind)
            self.assertNotIn('route_judge', text, kind)
            self.assertNotIn('onboard.py', text, kind)
            self.assertNotIn('@@', text, kind)

    def test_a_checkout_command_runs_the_package_directory_from_anywhere(self):
        if 'site-packages' in Path(host_rules.__file__).resolve().parts:
            self.assertEqual(host_rules.agentcompass_command(), 'agentcompass')
            return
        self.assertEqual(host_rules.agentcompass_command(), f'python -X utf8 {(host_rules.REPO / "agentcompass").as_posix()}')
        self.assertTrue((host_rules.REPO / 'agentcompass' / '__main__.py').is_file())

    def test_an_overlay_with_the_old_placeholder_spelling_is_rewritten(self):
        directory = host_rules.overlay_dir(self.home)
        directory.mkdir(parents=True)
        (directory / 'shared.md').write_bytes(
            b'run `python -X utf8 @@ROUTE_JUDGE@@ roll --task "x"` or `@@AGENTCOMPASS@@ plan`\n')
        text, source = host_rules.load('shared', self.home)
        command = host_rules.agentcompass_command()
        self.assertEqual((source, text), ('overlay', f'run `{command} roll --task "x"` or `{command} plan`\n'))


if __name__ == '__main__':
    unittest.main()
