import json
import tempfile
import unittest
from pathlib import Path

import gateway_coverage as gc


class ShippedCoverageTests(unittest.TestCase):
    """Rule (owner 2026-11-18): every offered pair of every saved gateway catalog is scored at its effort or on the declared
    known-gap list with a reason, and no entry of that list is covered or offered by no catalog."""

    def test_every_offered_pair_is_scored_or_a_declared_gap(self):
        found, violations, counts = gc.run()
        self.assertEqual(violations, [])
        self.assertEqual(counts['covered'] + counts['known_gap'], counts['pairs'])
        self.assertGreater(counts['pairs'], 0)

    def test_known_gap_entries_carry_a_reason_and_the_catalogs_nothing_personal(self):
        gaps = gc.read_json(gc.KNOWN_GAPS)
        for entry in gaps['entries']:
            self.assertTrue(entry.get('reason', '').strip(), entry)
            self.assertTrue(entry['efforts'], entry)
        for cid, doc in gc.load_catalogs().items():
            for row in doc['rows']:
                self.assertEqual(set(row), {'harness', 'model', 'efforts'}, cid)


class CheckRuleTests(unittest.TestCase):
    """The check itself, on synthetic pairs."""

    def pair(self, model, effort, missing=(), catalog='c'):
        return {'catalog': catalog, 'harness': 'h', 'model': model, 'effort': effort, 'resolves_to': model,
                'source': 'exact', 'missing': list(missing)}

    def gaps(self, *entries):
        return {'entries': [{'catalog': 'c', 'model': m, 'efforts': list(e), 'reason': 'r'} for m, e in entries]}

    def test_an_uncovered_pair_off_the_list_a_covered_listed_pair_and_a_listed_pair_never_offered_all_fail(self):
        found = [self.pair('a', 'high'), self.pair('b', 'high', ['time_per_task']), self.pair('c', 'low', ['x'])]
        violations, counts = gc.check(found, self.gaps(('b', ['high']), ('c', ['low'])))
        self.assertEqual((violations, counts['covered'], counts['known_gap']), ([], 1, 2))
        violations, _ = gc.check(found, self.gaps(('b', ['high'])))
        self.assertEqual(len(violations), 1)
        self.assertIn('uncovered, not on the known-gap list', violations[0])
        violations, _ = gc.check(found, self.gaps(('a', ['high']), ('b', ['high']), ('c', ['low'])))
        self.assertEqual(len(violations), 1)
        self.assertIn('covered', violations[0])
        violations, _ = gc.check(found, self.gaps(('b', ['high']), ('c', ['low']), ('d', ['max'])))
        self.assertEqual(len(violations), 1)
        self.assertIn('offered by no saved catalog', violations[0])

    def test_a_row_without_efforts_takes_the_launch_table_and_is_a_gap_off_it(self):
        accounts = json.loads((gc.REFERENCES / 'accounts.json').read_text(encoding='utf-8'))
        five = gc.read_json(gc.REFERENCES / 'five-axis-scores.json')
        harness, table = next((h, t) for h, t in accounts['dispatch_efforts'].items() if t)
        model = next(iter(table))
        catalogs = {'m': {'rows': [{'harness': harness, 'model': model, 'efforts': None},
                                   {'harness': 'no-such-harness', 'model': model, 'efforts': None}]}}
        found = gc.pairs(accounts, catalogs, five, {'schema_version': 3, 'scores': {}})
        self.assertEqual([p['effort'] for p in found if p['harness'] == harness], list(table[model]))
        other = [p for p in found if p['harness'] == 'no-such-harness']
        self.assertEqual(len(other), 1)
        self.assertIn('not in dispatch_efforts', other[0]['missing'][0])

    def test_a_catalog_of_another_schema_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'x.json').write_text(json.dumps({'schema': 'other', 'rows': []}), encoding='utf-8')
            with self.assertRaises(ValueError):
                gc.load_catalogs(tmp)


if __name__ == '__main__':
    unittest.main()
