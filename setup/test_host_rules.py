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

    def test_codex_and_daemon_overlays_include_the_shared_rules_instead_of_copying_them(self):
        directory = host_rules.overlay_dir(self.home)
        directory.mkdir(parents=True)
        (directory / 'shared.md').write_bytes(b'# Rules\n\nrun `@@AGENTCOMPASS@@ roll`\n')
        (directory / 'codex.md').write_bytes(b'@@SHARED@@\n\n## Codex only\n')
        (directory / 'daemon-append.md').write_bytes(b'short pointer\n')
        command = host_rules.agentcompass_command()
        self.assertEqual(host_rules.load('codex', self.home),
                         (f'# Rules\n\nrun `{command} roll`\n\n## Codex only\n', 'overlay'))
        self.assertEqual(host_rules.load('daemon-append', self.home), ('short pointer\n', 'overlay'))
        (directory / 'shared.md').unlink()
        self.assertTrue(host_rules.load('codex', self.home)[0].startswith(host_rules.load('shared', self.home)[0].strip('\n')))


    def test_the_default_is_a_pointer_to_the_installed_skill_docs(self):
        rules = Path(host_rules.DISPATCH_RULES)
        self.assertTrue(rules.is_file())
        self.assertEqual(rules.parent.parent, Path(host_rules.SKILL_DIR))
        self.assertTrue((rules.parent.parent / 'SKILL.md').is_file())
        self.assertIn('references/dispatch-rules.md', (rules.parent.parent / 'SKILL.md').read_text(encoding='utf-8'))
        for kind in host_rules.OVERLAY_FILES:
            text = host_rules.load(kind, self.home)[0]
            self.assertIn(host_rules.DISPATCH_RULES, text, kind)
            self.assertIn('next_action', text, kind)
            # The dispatch procedure is not copied into every session any more.
            for detail in ('bypassPermissions', 'notifyOnFinish', '2M', 'gate-hook', 'record --'):
                self.assertNotIn(detail, text, kind)
        self.assertLess(len(host_rules.load('shared', self.home)[0]), 2600)
        self.assertLess(len(host_rules.load('daemon-append', self.home)[0]), 1000)

    def test_a_personal_dispatch_file_is_named_not_copied(self):
        personal = host_rules.personal_dispatch_path(self.home)
        self.assertNotIn(personal.as_posix(), host_rules.load('shared', self.home)[0])
        personal.parent.mkdir(parents=True)
        personal.write_bytes(b'   \n')
        self.assertNotIn(personal.as_posix(), host_rules.load('shared', self.home)[0])
        personal.write_bytes(b'# My worktree flow\n\nSECRET-ISH PERSONAL BODY\n')
        for kind in host_rules.OVERLAY_FILES:
            text, source = host_rules.load(kind, self.home)
            self.assertEqual(source, 'default')
            self.assertIn(personal.as_posix(), text, kind)
            self.assertNotIn('PERSONAL BODY', text, kind)
            self.assertNotIn('@@', text, kind)
        self.assertNotIn('dispatch', host_rules.OVERLAY_FILES)

    def test_an_overlay_names_the_docs_and_the_personal_file_with_placeholders(self):
        directory = host_rules.overlay_dir(self.home)
        directory.mkdir(parents=True)
        (directory / 'shared.md').write_bytes(b'read @@DISPATCH_RULES@@ then @@PERSONAL_DISPATCH@@ (@@SKILL_DIR@@)\r\n')
        text, source = host_rules.load('shared', self.home)
        self.assertEqual(source, 'overlay')
        self.assertEqual(text, f'read {host_rules.DISPATCH_RULES} then '
                               f'{host_rules.personal_dispatch_path(self.home).as_posix()} ({host_rules.SKILL_DIR})\r\n')

    def test_the_old_generated_defaults_still_migrate(self):
        for old in ('# Agent routing rules (AgentCompass)\n\nold body\n',
                    'Router discipline: ' + host_rules.EXECUTION_POLICY + ' old tail',
                    host_rules.load('daemon-append', self.home)[0]):
            self.assertTrue(host_rules.is_legacy_generated(old), old[:40])


if __name__ == '__main__':
    unittest.main()
