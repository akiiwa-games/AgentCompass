"""The user's accounts-override.json is kept across updates (AGENTS.md, "What a released version wrote for the user is
kept"): a file an earlier release wrote is carried into the current shape by `accounts_override.LOCAL_UPGRADES`."""
import json
import ntpath
from pathlib import Path, PureWindowsPath
import re
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parents[2]
for _entry in (str(SCRIPTS_DIR), str(REPO_ROOT / 'tray')):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import accounts_override  # noqa: E402
import cwd_policy  # noqa: E402
import router_data  # noqa: E402
import state_upgrade  # noqa: E402


# The entries and directories of the review's grid (18 x 25).
ALLOWED_CWD_GRID_ENTRIES = ['D:/work/app', 'D:\\work\\app\\', 'D:/work/app/', 'D:/', 'D:\\', '/', '/work/app', 'app',
                            'work/app', 'D:/work/a.b', 'D:/work/(x)+', '~/proj', 'D:/work/../work/app', './app',
                            'D:/Work/App', '//srv/share/app', 'D:/work/app ', ' D:/work/app']
ALLOWED_CWD_GRID_DIRECTORIES = ['D:/work/app', 'D:\\work\\app', 'D:/work/app/', 'D:/work/app2', 'D:/work/app/sub',
                                'D:/WORK/APP/sub', 'D:/work/apple', 'D:/', 'D:/other', 'C:/work/app', 'C:/',
                                'D:/work/a.b', 'D:/work/aXb', 'D:/work/(x)+/q', 'D:/work/xx', 'D:/x/app/y',
                                'D:/work/app/../app2', 'D:/work/./app', 'C:/Users/foo/proj/z', 'D:/work/other/app',
                                'D:/work/app//sub', 'E:/work/app', 'D:/work/app/../app', 'D:/work/app/./sub',
                                'D:/work//app']
SEPARATORS = '\\/'


def v0_5_0_allowed_cwd(entries, cwd):
    """`account_fleet.account_cwd_allowed` of tag v0.5.0, the `allowed_cwd` part only, on Windows path semantics."""
    def parts(path):
        return tuple(p.lower().rstrip(SEPARATORS) for p in path.parts if p not in ('\\', '/'))
    actual = parts(PureWindowsPath(ntpath.abspath(cwd)))
    for raw in entries:
        item = raw.strip()
        candidate = PureWindowsPath(item)
        if candidate.is_absolute() or ':' in item or item.startswith(('/', '\\')):
            wanted = parts(PureWindowsPath(ntpath.abspath(item)))
            if wanted and len(actual) >= len(wanted) and actual[:len(wanted)] == wanted:
                return True
        else:
            wanted = parts(candidate)
            n = len(wanted)
            if n and any(actual[i:i + n] == wanted for i in range(len(actual) - n + 1)):
                return True
    return False


class AccountsOverrideKeptAcrossUpdatesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'accounts-override.json'
        patched = mock.patch.object(router_data, 'modules')   # it only puts the router scripts on the path; they are
        patched.start()                                       # already importable here, and no skill is installed
        self.addCleanup(patched.stop)

    def write(self, doc):
        with self.path.open('w', encoding='utf-8', newline='\n') as handle:
            json.dump(doc, handle)

    def test_a_checkbox_on_a_name_whose_v070_alias_changed_is_kept_on_the_id_the_name_gets_now(self):
        """Round 6 item 2: an auto bundle id holds the row its name resolved to. `LOCAL_UPGRADES` (schema 2 to 3) sets the
        v0.7.0 id's checkbox also on the id each `released_names.V0_7_0` name of that row gets now, never over one the
        user set, and keeps the v0.7.0 id's; a `#hash` id carries only to the name it hashes."""
        import hashlib
        accounts = {'models': {'gemini-4-argon-high': {}, 'claude-opus-5-5': {}, 'claude-opus-5': {},
                               'deepseek-v4.1-flash-free': {}, 'deepseek-v4.1-flash-1017': {}, 'deepseek-v4-flash': {},
                               'deepseek-v4.1-flash': {}},
                    'aliases': {'deepseek-flash': 'deepseek-v4.1-flash-1017'}}
        h = '#' + hashlib.sha256(b'claude-opus-5').hexdigest()[:8]
        doc = {'schema_version': 2, 'accounts': {'acct-a': {'bundles': {
            'auto:acct-a:agy:gemini-4-argon-high@high': {'disabled': True},
            'auto:acct-a:claude:claude-opus-5-5' + h: {'disabled': True},
            'auto:acct-a:opencode:deepseek-v4.1-flash-free': {'disabled': False},
            'auto:acct-a:opencode:deepseek-v4-flash': {'disabled': True},
            'manual_bundle': {'disabled': True}}}}}
        got = accounts_override.LOCAL_UPGRADES['schema 2 (v0.2.0 to v0.7.0)'][1](doc, accounts)
        self.assertEqual(got['schema_version'], accounts_override.SCHEMA_VERSION)
        self.assertEqual(got['accounts']['acct-a']['bundles'], {
            **doc['accounts']['acct-a']['bundles'],
            'auto:acct-a:agy:gemini-4-pro@high': {'disabled': True},
            'auto:acct-a:agy:gemini-4-ultra@high': {'disabled': True},
            'auto:acct-a:claude:claude-opus-5': {'disabled': True},
            'auto:acct-a:opencode:deepseek-v4.1-flash-1017': {'disabled': False},
            'auto:acct-a:opencode:deepseek-v4.1-flash': {'disabled': False}})
        self.assertEqual(doc['schema_version'], 2)                     # the step copies, it does not edit its input
        self.write(doc)
        self.assertEqual(accounts_override.load_override(self.path)['schema_version'], accounts_override.SCHEMA_VERSION)

    def test_a_reason_string_in_disabled_is_read_as_the_released_versions_read_it(self):
        # v0.1.0 to v0.5.0: a string (even an empty one) meant disabled, any other value kept its truth value.
        self.write({'schema_version': 2, 'accounts': {'acct-a': {'verified': True, 'bundles': {
            'b1': {'disabled': 'user turned it off'}, 'b2': {'disabled': ''}, 'b3': {'disabled': 0},
            'b4': {'disabled': 1}, 'b5': {'disabled': False}}}}})
        bundles = accounts_override.load_override(self.path)['accounts']['acct-a']['bundles']
        self.assertEqual({bid: spec['disabled'] for bid, spec in bundles.items()},
                         {'b1': True, 'b2': True, 'b3': False, 'b4': True, 'b5': False})

    def test_allowed_cwd_alone_becomes_a_cwd_regex_that_accepts_what_v0_5_0_accepted(self):
        self.write({'schema_version': 2, 'accounts': {
            'acct-a': {'allowed_cwd': ['D:/work/game', 'src/tools']}, 'acct-b': {'allowed_cwd': 'D:/work/docs/'},
            'acct-c': {'allowed_cwd': [], 'cwd_regex': 'keep-me'}, 'acct-d': {'allowed_cwd': ''}}})
        accounts = accounts_override.load_override(self.path)['accounts']
        self.assertEqual(set(accounts['acct-a']) | set(accounts['acct-b']), {'cwd_regex'})
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

    def test_the_carried_class_agrees_with_the_v0_5_0_matcher_and_every_other_entry_is_refused(self):
        # Rule: a step carries an entry only where the regex accepts exactly the directories the v0.5.0 matcher
        # accepted (the directories here all have a drive: a drive-less one resolves against the process's drive).
        carried, refused = [], []
        for entry in ALLOWED_CWD_GRID_ENTRIES:
            doc = {'schema_version': 2, 'accounts': {'a': {'allowed_cwd': [entry]}}}
            upgraded = accounts_override.upgrade_document(doc)['accounts']['a']
            if 'allowed_cwd' in upgraded:
                refused.append(entry)
                continue
            carried.append(entry)
            for cwd in ALLOWED_CWD_GRID_DIRECTORIES:
                with self.subTest(entry=entry, cwd=cwd):
                    self.assertEqual(cwd_policy.matches(upgraded['cwd_regex'], cwd), v0_5_0_allowed_cwd([entry], cwd))
        self.assertEqual(sorted(refused), sorted(['/', '/work/app', '~/proj', './app', '//srv/share/app']))
        self.assertGreaterEqual(len(carried), 10)   # the check above is not vacuous

    def test_a_directory_is_matched_in_its_normalized_form_for_every_cwd_regex(self):
        # Rule: `D:/work/app/../app2` is the directory `D:/work/app2`, never one inside `D:/work/app`; a `cwd_regex`
        # (upgraded or written by the user) sees the same directory the operating system would.
        inside = '^D:/work/app(/|$)'
        for outside in ('D:/work/app/../app2', 'D:\\work\\app\\..\\app2', 'D:/work/app/../../other', 'D:/work//app2'):
            with self.subTest(directory=outside):
                self.assertFalse(cwd_policy.matches(inside, outside))
        for within in ('D:/work/app/sub/..', 'D:/work/./app/sub', 'D:/work//app', 'D:/other/../work/app/x'):
            with self.subTest(directory=within):
                self.assertTrue(cwd_policy.matches(inside, within))

    def test_a_file_with_an_entry_that_is_not_carried_is_refused_with_a_fix_that_works(self):
        for entry in ('~/proj', '/work/app', './app'):
            with self.subTest(entry=entry):
                self.write({'schema_version': 2, 'accounts': {'acct-a': {'verified': True, 'allowed_cwd': [entry]}}})
                with self.assertRaises(ValueError) as caught:
                    accounts_override.load_override(self.path)
                self.assertIn('cwd_regex', str(caught.exception))
                self.assertEqual(str(caught.exception), state_upgrade.check_accounts_override(self.path))
                self.write({'schema_version': 2, 'accounts': {'acct-a': {'verified': True, 'cwd_regex': '^D:/work(/|$)'}}})
                self.assertEqual(accounts_override.load_override(self.path)['accounts']['acct-a']['verified'], True)

    def test_the_migration_record_of_the_v0_2_to_v0_5_tray_is_dropped_and_nothing_else_changes(self):
        # Those releases applied none of its entries, so the file without it is the file they ran with.
        doc = {'schema_version': 2, 'scoring': {'k': 1}, 'accounts': {'acct-a': {'verified': True, 'bundles': {
            'b1': {'disabled': True}}}}}
        self.write({**doc, 'migration_ambiguous_enabled': [{'account': 'acct-a', 'bundle': 'b2'}]})
        before = self.path.read_bytes()
        self.assertEqual(accounts_override.load_override(self.path), {**doc, 'schema_version': accounts_override.SCHEMA_VERSION})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIsNone(state_upgrade.check_accounts_override(self.path))

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
        self.assertEqual((written['schema_version'], written['scoring']), (accounts_override.SCHEMA_VERSION, {'k': 1}))
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
        current = {'schema_version': accounts_override.SCHEMA_VERSION, 'scoring': {'k': 1}, 'accounts': {'acct-a': {
            'verified': True, 'cwd_regex': '^D:/work', 'bundles': {'b1': {'disabled': True}}}}}
        for shape, (has_shape, _step) in accounts_override.LOCAL_UPGRADES.items():
            with self.subTest(shape=shape):
                self.assertFalse(has_shape(current))
        self.assertEqual(accounts_override.upgrade_document(current), current)

    def test_a_shape_that_cannot_be_carried_without_guessing_is_refused(self):
        for doc in ({'accounts': {'acct-a': {'verified': True}}},                          # v0.1.0: no schema_version
                    {'schema_version': 1, 'accounts': {}},
                    {'schema_version': accounts_override.SCHEMA_VERSION + 1, 'accounts': {}},
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
