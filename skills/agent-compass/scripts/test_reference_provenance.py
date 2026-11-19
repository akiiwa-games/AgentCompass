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

    def test_a_maintainer_alias_is_only_a_shorthand_and_an_identity_alias_cites_a_public_page(self):
        """Rule (round 5 review A): a dated maintainer declaration stands only for a shorthand (`shorthand: true`, "short
        name for <row>"); every other alias cites an https page; a shorthand flag on a cited row is refused."""
        accounts = json.loads((REFERENCES / 'accounts.json').read_text(encoding='utf-8'))
        row = next(k for k, v in accounts['models'].items() if isinstance(v, dict))
        short = {'kind': 'maintainer', 'as_of': '2026-11-18', 'shorthand': True, 'statement': f'short name for {row}'}
        cited = {'kind': 'vendor', 'url': 'https://example.test/x', 'as_of': '2026-11-18', 'statement': f'x is {row}'}

        def check(entry):
            doc = {**accounts, 'aliases': {**accounts['aliases'], 'x-alias': row},
                   'alias_sources': {**accounts['alias_sources'], 'rows': {**accounts['alias_sources']['rows'], 'x-alias': entry}}}
            reference_provenance.validate_accounts_provenance(doc)
        check(short)
        check(cited)
        for bad in ({**short, 'shorthand': False}, {k: v for k, v in short.items() if k != 'shorthand'},
                    {**short, 'statement': 'short name for another-row'}, {**cited, 'shorthand': True},
                    {**cited, 'url': 'http://example.test/x'},
                    {**short, 'shorthand': False, 'url': 'https://example.test/x'},     # a maintainer row with a page
                    {**cited, 'shorthand': True, 'statement': f'short name for {row}'}):  # a cited row as a shorthand
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, 'x-alias'):
                check(bad)

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


class DriftCorrectionShapeTests(unittest.TestCase):
    """Rule (calibration/refresh.py board_drift): a measurement corrected by its board's drift states drift, n >= 2
    named panel members, the spread {min, max, mad} around the drift and both dates, on a scaled source; a five-axis
    cell source carries the same record as its measurement."""
    GOOD = {'drift': -0.04, 'n': 3, 'members': ['a-m', 'b-m', 'c-m'], 'spread': {'min': -0.1, 'max': 0.02, 'mad': 0.03},
            'snapshot_as_of': '2026-11-18', 'fetched_as_of': '2026-11-19'}

    def doc(self, correction, scaled=True):
        return {'sources': {'b.x': {'scale': {'anchor': 'a-m'}} if scaled else {}},
                'models': {'new-m': {'coding': [{'source': 'b.x', 'value': 0.1, 'drift_correction': correction}]}}}

    def test_the_shape(self):
        reference_provenance.validate_measurements_provenance(self.doc(self.GOOD))
        bad = [dict(self.GOOD, n=1, members=['a-m']), dict(self.GOOD, members=['a-m']),
               dict(self.GOOD, spread={'min': 0.0, 'max': 0.1, 'mad': 0.0}), dict(self.GOOD, fetched_as_of=None),
               {k: v for k, v in self.GOOD.items() if k != 'spread'}]
        for correction in bad:
            with self.subTest(correction=correction), self.assertRaises(ValueError):
                reference_provenance.validate_measurements_provenance(self.doc(correction))
        with self.assertRaisesRegex(ValueError, 'without a scale'):
            reference_provenance.validate_measurements_provenance(self.doc(self.GOOD, scaled=False))
        cell = lambda c: {'models': {'new-m': {'axes': {'coding': {'facets': {'execution': {'sources': {
            'b.x': {'drift_correction': c}}}}}}}}}
        reference_provenance.validate_five_axis_provenance(cell(self.GOOD), self.doc(self.GOOD))
        with self.assertRaisesRegex(ValueError, 'differs'):
            reference_provenance.validate_five_axis_provenance(cell(dict(self.GOOD, drift=0.0)), self.doc(self.GOOD))


if __name__ == '__main__':
    unittest.main()
