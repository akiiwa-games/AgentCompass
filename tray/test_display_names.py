"""One owner of the effort order (owner 2026-11-18): user_scores.EFFORTS holds every effort on the scale, five-axis-scores.json
`effort_readings` every effort name off it, and every effort a shipped launch row, account bundle or saved gateway catalog
can offer is one of them and has a tray label in both languages."""
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / 'skills/agent-compass/scripts'))

import axis_scores  # noqa: E402
import display_names  # noqa: E402
from i18n_catalog import EN  # noqa: E402
import user_scores  # noqa: E402


def offered_efforts():
    """{effort: where it is offered} over the shipped launch table, the account bundles and the saved gateway catalogs."""
    accounts = json.loads((ROOT / 'skills/agent-compass/references/accounts.json').read_text(encoding='utf-8'))
    out = {}
    for harness, table in (accounts.get('dispatch_efforts') or {}).items():
        for model, efforts in (table or {}).items():
            for e in efforts if isinstance(efforts, list) else ():
                out.setdefault(e, f'dispatch_efforts[{harness}][{model}]')
    for name, spec in (accounts.get('accounts') or {}).items():
        for bid, b in ((spec or {}).get('bundles') or {}).items():
            if isinstance(b, dict) and b.get('effort'):
                out.setdefault(b['effort'], f'accounts[{name}].bundles[{bid}]')
    for path in sorted((ROOT / 'audit/gateway-catalogs').glob('*.json')):
        for row in json.loads(path.read_text(encoding='utf-8'))['rows']:
            for e in row.get('efforts') or ():
                out.setdefault(e, f'{path.name}: {row["model"]}')
    return out


class EffortOrderTests(unittest.TestCase):

    def test_every_offered_effort_is_on_the_order_or_declared_off_it_and_has_a_label_in_both_languages(self):
        readings = axis_scores.load_five_axis()['effort_readings']
        known = set(user_scores.EFFORTS) | set(readings)
        self.assertTrue(set(readings).isdisjoint(user_scores.EFFORTS))
        self.assertTrue(set(readings.values()) <= set(user_scores.EFFORTS))
        for effort, where in offered_efforts().items():
            self.assertIn(effort, known, where)
        for effort in known:
            label = display_names.EFFORTS.get(effort)
            self.assertTrue(label, effort)
            self.assertIn(label, EN, effort)
        self.assertEqual(set(display_names.EFFORTS), known)      # no label for an effort the order does not know


if __name__ == '__main__':
    unittest.main()
