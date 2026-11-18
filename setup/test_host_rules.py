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
            for detail in ('bypassPermissions', 'notifyOnFinish', '2M', 'rotate_after_carried_tokens', 'gate-hook', 'record --'):
                self.assertNotIn(detail, text, kind)
        # The default stays a short pointer. Its length is the text's, not the length of the path the checkout lives in.
        def text_length(kind):
            text = host_rules.load(kind, self.home)[0]
            for machine_path in (host_rules.DISPATCH_RULES, host_rules.SKILL_DIR, host_rules.agentcompass_command()):
                text = text.replace(machine_path, '@')
            return len(text)
        self.assertLess(text_length('shared'), 2600)
        self.assertLess(text_length('daemon-append'), 1000)

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


class HostRulesKeptAcrossUpdatesTests(unittest.TestCase):
    """The user's own overlay of an earlier release is read in today's names; the file is never rewritten."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.directory = host_rules.overlay_dir(self.home)
        self.directory.mkdir(parents=True)
        self.overlay = self.directory / 'shared.md'

    def test_an_overlay_written_for_an_earlier_release_keeps_its_words_and_takes_the_current_names(self):
        old = ('# My rules\n\nrun `python -X utf8 @@ROUTE_JUDGE@@ roll --task "x"`\n'
               'a worker (`ROUTER_WORKER=1`) with ROUTER_ACCOUNTS_OVERRIDE and ROUTER_LIVE_MODELS set\n'
               'load the route-agent-work skill\n')
        self.overlay.write_bytes(old.encode('utf-8'))
        text, source = host_rules.load('shared', self.home)
        self.assertEqual(source, 'overlay')
        command = host_rules.agentcompass_command()
        self.assertEqual(text, ('# My rules\n\nrun `' + command + ' roll --task "x"`\n'
                                'a worker (`AGENTCOMPASS_WORKER=1`) with AGENTCOMPASS_ACCOUNTS_OVERRIDE and '
                                'AGENTCOMPASS_LIVE_MODELS set\nload the agent-compass skill\n'))
        self.assertEqual(self.overlay.read_bytes(), old.encode('utf-8'))   # a read never writes

    def test_every_pair_renames_a_token_and_leaves_nothing_the_check_still_refuses(self):
        import state_upgrade
        for old, new in state_upgrade.HOST_RULE_UPGRADES.items():
            with self.subTest(old=old):
                self.assertNotEqual(old, new)
                self.assertFalse([tok for tok in state_upgrade.LEGACY_RULE_TOKENS if tok in new])
                self.assertEqual(state_upgrade.upgrade_host_rule_text(f'a {old} b'), f'a {new} b')

    def test_a_token_with_no_current_name_is_refused_not_guessed(self):
        for bad in (b'path `@@ROUTE_JUDGE@@` here\n', b'set ROUTE_MANAGER_HOST\n', b'run python route_judge.py plan\n'):
            with self.subTest(bad=bad):
                self.overlay.write_bytes(bad)
                with self.assertRaises(ValueError) as ctx:
                    host_rules.load('shared', self.home)
                self.assertIn('shared.md', str(ctx.exception))
                self.assertIn('delete', str(ctx.exception))

    def test_doctor_names_nothing_for_an_overlay_that_has_a_step(self):
        import state_upgrade
        self.overlay.write_bytes(b'worker (`ROUTER_WORKER=1`) runs `python -X utf8 @@ROUTE_JUDGE@@ plan`\n')
        self.assertIsNone(state_upgrade.check_host_rule_file(self.overlay))


if __name__ == '__main__':
    unittest.main()
