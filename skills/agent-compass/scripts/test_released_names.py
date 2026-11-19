"""`released_names.V0_7_0` is the alias diff between the v0.7.0 tag and this release (round 6 item 2)."""
import json
from pathlib import Path
import subprocess
import unittest

import released_names

REPO_ROOT = Path(__file__).resolve().parents[3]
ACCOUNTS = REPO_ROOT / 'skills' / 'agent-compass' / 'references' / 'accounts.json'


def v070_resolution(old, name):
    """The row v0.7.0's accounts.json resolved `name` to through its alias table (user_scores.resolve_model_key)."""
    target = (old.get('aliases') or {}).get(name)
    return target if target in (old.get('models') or {}) else None


class ReleasedNamesTests(unittest.TestCase):
    def setUp(self):
        self.now = json.loads(ACCOUNTS.read_text(encoding='utf-8-sig'))

    def test_every_entry_is_a_name_now_resolved_otherwise_and_its_target_is_still_a_row(self):
        for name, row in released_names.V0_7_0.items():
            with self.subTest(name=name):
                self.assertIn(row, self.now['models'])
                self.assertNotEqual(released_names.current_key(name, self.now), row)

    def test_the_view_as_v070_resolves_every_entry_to_the_row_it_meant_and_holds_none_as_a_row(self):
        """`as_v0_7_0` (read where auto_bundles.derive replays a v0.7.0 id): v0.7.0 had no row of these names, so none
        is one in the view, and each resolves there, bare or provider-qualified, to the row v0.7.0 meant."""
        import auto_bundles
        view = released_names.as_v0_7_0(self.now)
        for name, row in released_names.V0_7_0.items():
            with self.subTest(name=name):
                self.assertNotIn(name, view['models'])
                self.assertEqual(auto_bundles.default_profile(name, view)['model'], row)
                self.assertEqual(auto_bundles.default_profile('provider/' + name.upper(), view)['model'], row)

    def test_the_table_is_the_alias_diff_against_the_v070_tag(self):
        try:
            raw = subprocess.run(['git', 'show', 'v0.7.0:skills/agent-compass/references/accounts.json'], cwd=REPO_ROOT,
                                 capture_output=True, text=True, encoding='utf-8', timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            self.skipTest(f'git unavailable: {exc}')
        if raw.returncode != 0:
            self.skipTest('the v0.7.0 tag is not in this checkout (a shallow clone)')
        old = json.loads(raw.stdout.lstrip('﻿'))
        diff = {}
        for name in old.get('aliases') or {}:
            row = v070_resolution(old, name)
            if row is not None and released_names.current_key(name, self.now) != row:
                diff[name] = row
        self.assertEqual(released_names.V0_7_0, diff)

    def test_the_segment_rule_is_v070s_resolution_of_a_provider_qualified_name(self):
        """Round 7 item 4: v0.7.0 resolved a name as a row, an alias, its last `/` segment as a row, then as an alias;
        `v0_7_0_row` gives that row for a provider-qualified name, because no provider-qualified alias of v0.7.0 has a
        last segment in the table (it would have resolved before the segment)."""
        try:
            raw = subprocess.run(['git', 'show', 'v0.7.0:skills/agent-compass/references/accounts.json'], cwd=REPO_ROOT,
                                 capture_output=True, text=True, encoding='utf-8', timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            self.skipTest(f'git unavailable: {exc}')
        if raw.returncode != 0:
            self.skipTest('the v0.7.0 tag is not in this checkout (a shallow clone)')
        old = json.loads(raw.stdout.lstrip('﻿'))
        self.assertEqual([a for a in old['aliases'] if '/' in a and a.rsplit('/', 1)[-1] in released_names.V0_7_0], [])
        for name, row in released_names.V0_7_0.items():
            with self.subTest(name=name):
                leaf = f'vendor/{name}'.rsplit('/', 1)[-1]
                self.assertEqual(v070_resolution(old, leaf), row)
                self.assertEqual(released_names.v0_7_0_row(f'vendor/{name}'), row)
        self.assertIsNone(released_names.v0_7_0_row('vendor/claude-opus-5-5'))


if __name__ == '__main__':
    unittest.main()
