import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import gen_routing_parity as gen


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', *args],
                          capture_output=True, text=True, check=True).stdout.strip()


class PinnedSourceTests(unittest.TestCase):
    def test_the_commit_comes_from_the_constant_unless_overridden(self):
        self.assertEqual(gen.pinned_sha({}), gen.PINNED_SHA)
        self.assertEqual(gen.pinned_sha({'AGENTCOMPASS_PARITY_SHA': 'abc123'}), 'abc123')

    def test_a_caller_keeps_its_own_modules(self):
        probe = subprocess.run(['git', '-C', str(gen.REPOSITORY), 'cat-file', '-e', f'{gen.pinned_sha()}^{{commit}}'], capture_output=True)
        if probe.returncode:
            self.skipTest('the pinned commit is not in this clone')
        held = types.ModuleType('allocator')
        with patch.dict(sys.modules, {'allocator': held}):
            gen.generate(gen.pinned_sha())
            self.assertIs(sys.modules['allocator'], held)

    def test_extract_reads_the_commit_not_the_working_tree(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            repo = Path(directory) / 'repo'
            data = repo / 'skills/agent-compass/references/accounts.json'
            data.parent.mkdir(parents=True)
            data.write_text('committed', encoding='utf-8')
            git(repo.parent, 'init', '-q', str(repo))
            git(repo, 'add', '-A')
            git(repo, 'commit', '-qm', 'seed')
            sha = git(repo, 'rev-parse', 'HEAD')
            data.write_text('edited', encoding='utf-8')
            out = Path(directory) / 'out'
            self.assertEqual(gen.extract(sha, out, repo), sha)
            self.assertEqual((out / 'skills/agent-compass/references/accounts.json').read_text(encoding='utf-8'), 'committed')
            with self.assertRaisesRegex(ValueError, 'ref name'):
                gen.extract('HEAD', Path(directory) / 'out2', repo)


@unittest.skipUnless((gen.REPOSITORY / '.git').exists(), 'needs the repository history')
class GeneratedDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sha = gen.pinned_sha()
        probe = subprocess.run(['git', '-C', str(gen.REPOSITORY), 'cat-file', '-e', f'{sha}^{{commit}}'], capture_output=True)
        if probe.returncode:
            raise unittest.SkipTest(f'{sha} is not in this clone')
        cls.data = gen.generate(sha)

    def test_the_output_is_plain_json_from_the_pinned_commit(self):
        self.assertTrue(self.data['source']['full_commit'].startswith(gen.pinned_sha()))
        json.loads(json.dumps(self.data, allow_nan=False))

    def test_resolutions_carry_their_source(self):
        rows = self.data['resolutions']
        self.assertEqual(rows['claude-opus-5-5-20261001'], {'key': 'claude-opus-5-5', 'source': 'alias', 'effort': None})
        self.assertEqual(rows['claude-opus-5-5:medium'], {'key': 'claude-opus-5-5', 'source': 'exact', 'effort': 'medium'})
        self.assertEqual(rows['gemini-2.5-pro']['source'], 'family')
        self.assertEqual(rows['totally-unknown-model']['source'], 'unknown')

    def test_the_reading_of_the_contract_travels_with_the_data(self):
        self.assertIn('highest-scoring level that clears the floor', self.data['interpretations']['R3.best_level'])
        self.assertEqual(self.data['row_keys']['claude-opus-4-20250514'], 'claude-opus-4-20250514')
        self.assertEqual(self.data['row_keys']['claude-opus-5'], 'claude-opus-5-5')

    def test_a_flagship_and_its_family_row_stay_apart_in_the_data(self):
        cases = {(r['label'], r['realtime_loop']): r for r in self.data['row_pins']}
        for realtime in (False, True):
            self.assertEqual(cases['flagship beside family, pin flagship', realtime]['model'], 'claude-opus-5-5')
            self.assertEqual(cases['family beside flagship, pin family', realtime]['model'], 'claude-opus-9')
            self.assertEqual(cases['flagship level off, family row present, pin flagship', realtime]['choice'], 'none')
            self.assertEqual(cases['same model level off, pin that level', realtime]['choice'], 'none')
        for label, model in (('family level off, pin flagship at that level', 'claude-opus-5-5'),
                             ('flagship level off, pin family at that level', 'claude-opus-9')):
            found = cases[label, False]
            self.assertEqual((found['model'], found['effort']), (model, 'high'))
        self.assertEqual(cases['a row listing one level beside one that steps, unnamed', True]['choice'], 'y')
        self.assertEqual(cases['a row listing one level beside one that steps, pin model', True]['choice'], 'y@low')
        for key in ('R1.pin_match', 'R2.unchecked_level', 'R2.listed_levels'):
            self.assertIn(key, self.data['interpretations'])

    def test_a_family_guess_answers_with_the_default_speed(self):
        self.assertEqual(self.data['responsiveness']['gemini-2.5-pro']['high'], [0.52, 'default'])

    def test_profile_validation_and_pins_are_recorded(self):
        verdicts = {(r['multi_task'], json.dumps(r['task_count'])): r for r in self.data['profile_validation']}
        self.assertEqual(verdicts[(True, '4')]['bucket'], '>3')
        self.assertFalse(verdicts[(True, '"4"')]['valid'])
        self.assertFalse(verdicts[(True, '"nan"')]['valid'])
        self.assertFalse(verdicts[(True, '"1"')]['valid'])
        pinned = next(p for p in self.data['pins'] if p['task'] == {'constraints': {'model': 'claude-opus-5-5:medium'}} and not p['realtime_loop'])
        self.assertEqual((pinned['model'], pinned['effort']), ('claude-opus-5-5', 'medium'))


if __name__ == '__main__':
    unittest.main()
