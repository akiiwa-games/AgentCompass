"""Shipped reference data: the provenance checks, and readers that read only the shipped files. No network."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import account_fleet
import axis_scores
import provider_inventory
import reference_provenance
import scorer_client
import user_scores
from router_test_support import isolated_router_state

REFERENCES = reference_provenance.REFERENCES
CHECKED = ('accounts.json', 'plan-templates.json', 'user-scores.json')


def write_json(path, document):
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')


class ShippedReferencesTests(unittest.TestCase):
    def test_the_shipped_references_pass_the_provenance_checks(self):
        reference_provenance.validate_references(REFERENCES)

    def test_a_leftover_between_release_cache_is_never_read(self):
        """Rule (router-internals, Reference data): every reader reads the reference file shipped with the version."""
        isolated_router_state(self.addCleanup)
        os.environ.pop('ROUTER_ACCOUNTS_FILE', None)
        state = Path(os.environ['ROUTER_STATE_DIR'])
        leftover = state / 'public-data' / 'sets' / '0123456789ab'
        leftover.mkdir(parents=True)
        for name in ('accounts.json', 'plan-templates.json', 'classifier-cases.json', 'five-axis-scores.json',
                     'user-scores.json'):
            write_json(leftover / name, {'leftover': True})
        write_json(state / 'public-data' / 'state.json', {'version': '0123456789ab'})
        write_json(state / 'public-data-config.json', {'enabled': True})

        def shipped(name):
            return json.loads((REFERENCES / name).read_text(encoding='utf-8-sig'))

        self.assertEqual(account_fleet.fleet_document_path(), REFERENCES / 'accounts.json')
        self.assertEqual(axis_scores.load_five_axis(), shipped('five-axis-scores.json'))
        self.assertEqual(user_scores.load_public(),
                         user_scores.validate_document(shipped('user-scores.json'), expected_kind='maintainer'))
        cases = shipped('classifier-cases.json')
        self.assertEqual(scorer_client.load_classifier_cases(),
                         {k: cases[k] for k in ('route_definitions', 'dimension_rubric', 'calibration_cases') if k in cases})
        with patch.object(provider_inventory, '_SHIPPED_PLAN_TEMPLATES', None):
            self.assertEqual(provider_inventory._shipped_plan_templates(), shipped('plan-templates.json'))


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

    def test_families_rows_declare_their_size_and_tier_up_names_a_full_row(self):
        """Rule (calibration/series.py): every families row declares `size`; `tier_up` sits on a small row, names the
        pattern of a full row and comes with `tier_up_version` `same` or `nearest`."""
        accounts = json.loads((REFERENCES / 'accounts.json').read_text(encoding='utf-8'))
        rows = accounts['families']['rows']
        small = next(i for i, r in enumerate(rows) if r.get('tier_up'))
        full = next(i for i, r in enumerate(rows) if r['size'] == 'full')
        for i, change in ((full, {'size': None}), (full, {'size': 'medium'}), (full, {'tier_up': rows[full]['pattern']}),
                          (small, {'tier_up': rows[small]['pattern']}), (small, {'tier_up': '^no-such-row'}),
                          (small, {'tier_up_version': 'older'}), (full, {'tier_up_version': 'same'})):
            broken = [dict(r) for r in rows]
            broken[i].update(change)
            with self.subTest(row=i, change=change), self.assertRaisesRegex(ValueError, 'families.rows'):
                reference_provenance.validate_accounts_provenance({**accounts, 'families': {**accounts['families'], 'rows': broken}})

    def test_tier_up_older_and_effort_readings_are_declared_in_shape(self):
        """Rule (calibration/series.py, five_axis.effort_readings): `tier_up_older` maps an older row of the small row's
        pattern (no current member) to a row of its `tier_up` pattern with a version; `effort_readings.reads` maps an effort
        name off the scale to a measured effort, as a dated maintainer declaration."""
        accounts = json.loads((REFERENCES / 'accounts.json').read_text(encoding='utf-8'))
        rows = accounts['families']['rows']
        i = next(i for i, r in enumerate(rows) if r.get('tier_up_older'))
        older, rel = next(iter(rows[i]['tier_up_older'].items()))
        current = rows[i]['models'][0]
        for change in ({older: {**rel, 'version': 'older'}}, {older: {'model': current, 'version': 'same'}},
                       {current: rel}, {'no-such-model': rel}, {older: {'model': rel['model']}}):
            broken = [dict(r) for r in rows]
            broken[i]['tier_up_older'] = change
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'tier_up_older'):
                reference_provenance.validate_accounts_provenance({**accounts, 'families': {**accounts['families'], 'rows': broken}})
        for readings in (None, {**accounts['effort_readings'], 'reads': {'default': 'ultra'}},
                         {**accounts['effort_readings'], 'reads': {'high': 'high'}},
                         {k: v for k, v in accounts['effort_readings'].items() if k != 'as_of'}):
            broken = {k: v for k, v in accounts.items() if k != 'effort_readings'}
            if readings is not None:
                broken['effort_readings'] = readings
            with self.subTest(readings=str(readings)[:60]), self.assertRaisesRegex(ValueError, 'effort_readings'):
                reference_provenance.validate_accounts_provenance(broken)

    def test_cited_alias_and_launch_rows_must_exist_and_be_cited(self):
        accounts = json.loads((REFERENCES / 'accounts.json').read_text(encoding='utf-8'))
        alias = next(iter(accounts['alias_sources']['rows']))
        harness = next(iter(accounts['dispatch_efforts_sources']['rows']))
        for table, rows in (('alias_sources', {'no-such-alias': accounts['alias_sources']['rows'][alias]}),
                            ('alias_sources', {alias: {'kind': 'vendor', 'statement': 'no url'}}),
                            ('dispatch_efforts_sources', {harness: {'no-such-model': {'kind': 'maintainer', 'as_of': '2026-11-18'}}})):
            with self.subTest(table=table, rows=str(rows)[:60]), self.assertRaisesRegex(ValueError, table):
                reference_provenance.validate_accounts_provenance({**accounts, table: {**accounts[table], 'rows': rows}})

    def test_every_alias_carries_its_citation(self):
        """Rule (round 4): an alias is a public statement of identity, name by name (the maker's word, models.dev's
        canonical_model_id with its date, or a dated maintainer declaration); an alias without its row is refused."""
        accounts = json.loads((REFERENCES / 'accounts.json').read_text(encoding='utf-8'))
        self.assertEqual(set(accounts['aliases']), set(accounts['alias_sources']['rows']))
        row = next(k for k, v in accounts['models'].items() if isinstance(v, dict))
        cited = {'kind': 'community', 'url': 'https://models.dev/api.json', 'as_of': '2026-11-10',
                 'statement': f'models.dev lists p/x-alias with canonical_model_id p/{row}'}
        doc = {**accounts, 'aliases': {**accounts['aliases'], 'x-alias': row}}
        with self.assertRaisesRegex(ValueError, 'x-alias'):
            reference_provenance.validate_accounts_provenance(doc)
        doc['alias_sources'] = {**accounts['alias_sources'], 'rows': {**accounts['alias_sources']['rows'], 'x-alias': cited}}
        reference_provenance.validate_accounts_provenance(doc)
        doc['alias_sources']['rows']['x-alias'] = {**cited, 'as_of': None}
        with self.assertRaisesRegex(ValueError, 'x-alias'):
            reference_provenance.validate_accounts_provenance(doc)

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
