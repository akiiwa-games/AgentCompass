"""The user's accounts-override.json is kept across updates (AGENTS.md, "What a released version wrote for the user is
kept"): a file an earlier release wrote is carried into the current shape by `accounts_override.LOCAL_UPGRADES`."""
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parents[2]
for _entry in (str(SCRIPTS_DIR), str(REPO_ROOT / 'tray')):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import accounts_override  # noqa: E402
import cwd_policy  # noqa: E402
import router_data  # noqa: E402
import state_upgrade  # noqa: E402


class AccountsOverrideKeptAcrossUpdatesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'accounts-override.json'

    def write(self, doc):
        with self.path.open('w', encoding='utf-8', newline='\n') as handle:
            json.dump(doc, handle)

    def test_a_reason_string_in_disabled_is_read_as_the_released_versions_read_it(self):
        # v0.1.0 to v0.5.0: a string (even an empty one) meant disabled, any other value kept its truth value.
        self.write({'schema_version': 2, 'accounts': {'acct-a': {'verified': True, 'bundles': {
            'b1': {'disabled': 'user turned it off'}, 'b2': {'disabled': ''}, 'b3': {'disabled': 0},
            'b4': {'disabled': 1}, 'b5': {'disabled': False}}}}})
        bundles = accounts_override.load_override(self.path)['accounts']['acct-a']['bundles']
        self.assertEqual({bid: spec['disabled'] for bid, spec in bundles.items()},
                         {'b1': True, 'b2': True, 'b3': False, 'b4': True, 'b5': False})

    def test_allowed_cwd_alone_becomes_the_cwd_regex_the_tray_showed_and_saved(self):
        self.write({'schema_version': 2, 'accounts': {
            'acct-a': {'allowed_cwd': ['D:/work/game', 'src/tools']}, 'acct-b': {'allowed_cwd': 'D:/work/docs/'},
            'acct-c': {'allowed_cwd': [], 'cwd_regex': 'keep-me'}, 'acct-d': {'allowed_cwd': ''}}})
        accounts = accounts_override.load_override(self.path)['accounts']
        self.assertEqual(set(accounts['acct-a']) | set(accounts['acct-b']), {'cwd_regex'})
        # Declared rule: a cwd under an allowed path, or one that contains the relative segments, matches; others do not.
        regex_a, regex_b = accounts['acct-a']['cwd_regex'], accounts['acct-b']['cwd_regex']
        self.assertTrue(cwd_policy.matches(regex_a, 'd:\\Work\\Game\\sub'))
        self.assertTrue(cwd_policy.matches(regex_a, 'E:/a/src/tools/x'))
        self.assertFalse(cwd_policy.matches(regex_a, 'D:/work/other'))
        self.assertFalse(cwd_policy.matches(regex_a, 'D:/work/gamer'))
        self.assertTrue(cwd_policy.matches(regex_b, 'D:/work/docs'))
        self.assertFalse(cwd_policy.matches(regex_b, 'D:/work/docsx'))
        self.assertEqual(accounts['acct-c'], {'cwd_regex': 'keep-me'})  # an empty restriction restricts nothing
        self.assertEqual(accounts['acct-d'], {})
        re.compile(regex_a)

    def test_a_read_never_writes_and_the_next_save_writes_the_current_shape_keeping_the_choices(self):
        self.write({'schema_version': 2, 'scoring': {'k': 1}, 'accounts': {'acct-a': {
            'verified': True, 'allowed_cwd': ['D:/work'], 'bundles': {'b1': {'disabled': 'off'}}}}})
        before = self.path.read_bytes()
        loaded = accounts_override.load_override(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        loaded['accounts']['acct-a']['bundles']['b2'] = {'disabled': True}   # what a tray save does after the read
        router_data._atomic_json(self.path, loaded)
        written = json.loads(self.path.read_text(encoding='utf-8'))
        spec = written['accounts']['acct-a']
        self.assertEqual((written['schema_version'], written['scoring']), (2, {'k': 1}))
        self.assertEqual((spec['verified'], spec['bundles']), (True, {'b1': {'disabled': True}, 'b2': {'disabled': True}}))
        self.assertNotIn('allowed_cwd', spec)
        self.assertTrue(cwd_policy.matches(spec['cwd_regex'], 'D:/work/x'))
        self.assertEqual(accounts_override.load_override(self.path), written)  # now the current shape: no step applies

    def test_the_tray_edit_of_an_earlier_releases_file_keeps_its_choices(self):
        self.write({'schema_version': 2, 'accounts': {'acct-a': {'allowed_cwd': 'D:/work',
                                                                   'bundles': {'b1': {'disabled': 'off'}}}}})
        before = router_data.STATE_DIR
        router_data.STATE_DIR = self.path.parent
        self.addCleanup(setattr, router_data, 'STATE_DIR', before)
        router_data._edit_preferences(lambda doc: doc['accounts']['acct-a'].update(verified=True))
        spec = json.loads(self.path.read_text(encoding='utf-8'))['accounts']['acct-a']
        self.assertEqual((spec['verified'], spec['bundles']), (True, {'b1': {'disabled': True}}))
        self.assertTrue(cwd_policy.matches(spec['cwd_regex'], 'D:/work/x'))

    def test_every_step_recognises_only_a_shape_the_current_one_cannot_have(self):
        current = {'schema_version': 2, 'scoring': {'k': 1}, 'accounts': {'acct-a': {
            'verified': True, 'cwd_regex': '^D:/work', 'bundles': {'b1': {'disabled': True}}}}}
        for shape, (has_shape, _step) in accounts_override.LOCAL_UPGRADES.items():
            with self.subTest(shape=shape):
                self.assertFalse(has_shape(current))
        self.assertEqual(accounts_override.upgrade_document(current), current)

    def test_a_shape_that_cannot_be_carried_without_guessing_is_refused(self):
        for doc in ({'accounts': {'acct-a': {'verified': True}}},                          # v0.1.0: no schema_version
                    {'schema_version': 1, 'accounts': {}},
                    {'schema_version': 3, 'accounts': {}},
                    {'schema_version': 2, 'migration_ambiguous_enabled': [{'account': 'acct-a', 'bundle': 'b1'}]},
                    {'schema_version': 2, 'accounts': {'acct-a': {'allowed_cwd': ['D:/work'], 'cwd_regex': 'game'}}},
                    {'schema_version': 2, 'accounts': {'acct-a': {'allowed_cwd': {'path': 'D:/work'}}}},
                    ['not', 'an', 'object']):
            with self.subTest(doc=doc):
                self.write(doc)
                with self.assertRaises(ValueError) as caught:
                    accounts_override.load_override(self.path)
                self.assertEqual(str(caught.exception), state_upgrade.check_accounts_override(self.path))

    def test_doctor_names_nothing_for_a_shape_that_has_a_step(self):
        self.write({'schema_version': 2, 'accounts': {'acct-a': {'allowed_cwd': ['D:/work'],
                                                                   'bundles': {'b1': {'disabled': 'off'}}}}})
        self.assertIsNone(state_upgrade.check_accounts_override(self.path))


if __name__ == '__main__':
    unittest.main()
