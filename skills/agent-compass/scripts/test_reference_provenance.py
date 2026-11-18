"""Shipped reference data: the provenance checks. Which set the readers read is test_public_data.py. No network."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest

import reference_provenance
import user_scores

REFERENCES = reference_provenance.REFERENCES
CHECKED = ('accounts.json', 'plan-templates.json', 'user-scores.json')


def write_json(path, document):
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')


class ShippedReferencesTests(unittest.TestCase):
    def test_the_shipped_references_pass_the_provenance_checks(self):
        reference_provenance.validate_references(REFERENCES)


class ProvenanceTests(unittest.TestCase):
    """Public reference files accept cited https:// URLs or dated kind='maintainer' declarations, and reject uncited
    kind='user' or undated declarations."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        for name in CHECKED:
            shutil.copyfile(REFERENCES / name, self.folder / name)

    def assertRejected(self, file_name, *words):
        with self.assertRaises(ValueError) as raised:
            reference_provenance.validate_references(self.folder)
        self.assertIn(file_name, str(raised.exception))
        for word in words:
            self.assertIn(word, str(raised.exception))

    def test_families_must_be_a_dated_maintainer_declaration(self):
        accounts = json.loads((REFERENCES / 'accounts.json').read_text(encoding='utf-8'))
        reference_provenance.validate_accounts_provenance(accounts)
        for broken in (accounts['families']['rows'], {**accounts['families'], 'as_of': None},
                       {key: value for key, value in accounts['families'].items() if key != 'rows'}):
            with self.subTest(broken=str(broken)[:60]), self.assertRaisesRegex(ValueError, 'families'):
                reference_provenance.validate_accounts_provenance({**accounts, 'families': broken})

    def test_accounts_provenance_accepts_dated_maintainer_and_rejects_uncited_or_hard_coded_declarations(self):
        reference_provenance.validate_references(self.folder)
        accounts = json.loads((self.folder / 'accounts.json').read_text(encoding='utf-8'))
        grok = accounts['same_model']['grok-bot']

        grok['kind'] = 'user'                                       # uncited kind='user'
        write_json(self.folder / 'accounts.json', accounts)
        self.assertRejected('accounts.json', 'grok-bot')

        grok['kind'] = 'maintainer'                                 # undated kind='maintainer'
        grok.pop('as_of', None)
        write_json(self.folder / 'accounts.json', accounts)
        self.assertRejected('accounts.json', 'as_of')

        grok['as_of'] = '2026-11-11'                                # image_input without image_input_source
        accounts['models']['grok-bot'] = {'image_input': True}
        write_json(self.folder / 'accounts.json', accounts)
        self.assertRejected('accounts.json', 'image_input')

        accounts['models'].pop('grok-bot', None)                    # capacity_rule with a plan literal
        grok['capacity_rule']['plan'] = 'premium'
        write_json(self.folder / 'accounts.json', accounts)
        self.assertRejected('accounts.json', 'capacity_rule')

        grok['capacity_rule'].pop('plan', None)                     # capacity_rule with a price literal
        grok['capacity_rule']['price_usd_per_month'] = 30
        write_json(self.folder / 'accounts.json', accounts)
        self.assertRejected('accounts.json', 'capacity_rule')

    def test_plan_templates_reject_a_non_https_vendor_price_source(self):
        templates = json.loads((self.folder / 'plan-templates.json').read_text(encoding='utf-8'))
        templates['grok']['premium']['price_source']['url'] = 'http://x.ai/insecure'
        write_json(self.folder / 'plan-templates.json', templates)
        self.assertRejected('plan-templates.json', 'price_source')

    def test_public_user_scores_reject_a_local_kind(self):
        write_json(self.folder / 'user-scores.json', {
            'schema_version': user_scores.SCHEMA_VERSION,
            'scores': {'grok-bot': {'coding': {'level': 2.0, 'as_of': '2026-11-11', 'kind': 'user'}}},
        })
        self.assertRejected('user-scores.json')


if __name__ == '__main__':
    unittest.main()
