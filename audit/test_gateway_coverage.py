import json
import tempfile
import unittest
from pathlib import Path

import gateway_coverage as gc


def notice(**extra):
    return {'kind': 'vendor', 'url': 'https://example.com/models', 'as_of': '2026-11-18', 'statement': 's', **extra}


KINDS = {k: {'missing': f'the input of {k}', 'closes_when': 'a reading'} for k in gc.GAP_KINDS}
ID_LISTS = {'https://example.com/models': {'as_of': '2026-11-18', 'statement': 'the page names every callable model id'}}


class ShippedCoverageTests(unittest.TestCase):
    """Rule (owner 2026-11-18): every gated offered pair of every saved gateway catalog is scored at its effort or on the
    declared known-gap list with the input that is missing, and no entry of that list is covered, ungated or offered by no
    catalog."""

    def test_every_offered_pair_is_scored_or_a_declared_gap(self):
        found, violations, counts = gc.run()
        self.assertEqual(violations, [])
        self.assertEqual(counts['covered'] + counts['known_gap'], counts['pairs'])
        self.assertGreater(counts['pairs'], 0)

    def test_the_catalogs_keep_provider_model_and_efforts_only(self):
        for cid, doc in gc.load_catalogs().items():
            self.assertIn(doc['provider_kind'], gc.PROVIDER_KINDS, cid)
            for row in doc['rows']:
                self.assertEqual(set(row), gc.ROW_KEYS | ({'maker'} if doc.get('gate') else set()), cid)
            if doc.get('gate'):
                self.assertLessEqual(set(doc['gate']['makers']), {r['provider'] for r in doc['rows']}, cid)

    def test_every_alias_names_one_model_by_the_saved_models_dev_listings(self):
        accounts = json.loads((gc.REFERENCES / 'accounts.json').read_text(encoding='utf-8'))
        self.assertEqual(gc.alias_violations(accounts, gc.read_json(gc.NAMES), gc.load_catalogs()), [])


class CheckRuleTests(unittest.TestCase):
    """The check itself, on synthetic pairs."""

    def pair(self, model, effort, missing=(), catalog='c', ungated=None):
        return {'catalog': catalog, 'provider': 'h', 'model': model, 'effort': effort, 'resolves_to': model,
                'source': 'exact', 'missing': list(missing), 'ungated': ungated}

    def gaps(self, *entries):
        return {'entries': [{'catalog': 'c', 'model': m, 'efforts': list(e), 'row': m, 'kind': gc.GAP_KINDS[0], 'detail': 'd'}
                            for m, e in entries]}

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

    def test_an_ungated_pair_is_reported_never_a_failure_and_never_a_known_gap(self):
        """Rule (owner 2026-11-18, round 4): a pair outside the gate or on the out-of-scope list is a report; listing it as a
        known gap fails (the list holds gated pairs only)."""
        found = [self.pair('a', 'high', ['x'], ungated='outside the gate'), self.pair('b', 'high', ['x'], ungated='retired (b)')]
        violations, counts = gc.check(found, {'entries': []})
        self.assertEqual((violations, counts['pairs'], counts['reported']), ([], 0, 2))
        violations, _ = gc.check(found, self.gaps(('a', ['high'])))
        self.assertEqual(len(violations), 1)
        self.assertIn('not gated', violations[0])

    def test_the_gate_is_per_pair_by_maker_and_the_out_of_scope_list_applies_to_every_catalog(self):
        """Rule (round 5 review E): a pair is gated when the row's maker is its provider's declared maker (`gate.makers`),
        read from data, never from names in code; a row without a maker is reported as such; the out-of-scope list applies
        to every catalog; the launch table is always gated."""
        doc = {'gate': {'makers': {'maker': 'mk', 'relay': None}, 'statement': 's'}}
        oos = {'old': {'status': 'retired', 'date': '2026-01-01', 'notice': notice()}}
        self.assertIsNone(gc.ungated_reason(doc, {'provider': 'maker', 'model': 'm', 'maker': 'mk'}, oos))
        self.assertEqual(gc.ungated_reason(doc, {'provider': 'relay', 'model': 'm', 'maker': 'mk'}, oos),
                         'not first-party (mk on relay)')
        self.assertEqual(gc.ungated_reason(doc, {'provider': 'maker', 'model': 'm', 'maker': 'other'}, oos),
                         'not first-party (other on maker)')
        self.assertEqual(gc.ungated_reason(doc, {'provider': 'maker', 'model': 'm', 'maker': None}, oos), 'maker unknown')
        self.assertEqual(gc.ungated_reason(doc, {'provider': 'maker', 'model': 'old', 'maker': 'mk'}, oos), 'retired (old)')
        self.assertIsNone(gc.ungated_reason({}, {'provider': 'host', 'model': 'm'}, {}))
        accounts = json.loads((gc.REFERENCES / 'accounts.json').read_text(encoding='utf-8'))
        five = gc.read_json(gc.REFERENCES / 'five-axis-scores.json')
        model = next(m for m in accounts['models'] if isinstance(accounts['models'][m], dict))
        catalogs = {'g': {'provider_kind': 'magpie_preset', 'gate': {'makers': {'maker': 'mk'}, 'statement': 's'},
                          'rows': [{'provider': 'maker', 'model': model, 'efforts': ['high'], 'maker': 'mk'},
                                   {'provider': 'host', 'model': model, 'efforts': ['high'], 'maker': 'mk'},
                                   {'provider': 'maker', 'model': 'old', 'efforts': ['high'], 'maker': 'mk'}]},
                    gc.LAUNCH_TABLE: {'provider_kind': 'harness', 'rows': [{'provider': 'h', 'model': 'old', 'efforts': ['high']}]}}
        found = gc.pairs(accounts, catalogs, five, {'schema_version': 3, 'scores': {}}, oos)
        self.assertEqual([(p['catalog'], p['provider'], p['model'], p['ungated']) for p in found],
                         [('g', 'maker', model, None), ('g', 'host', model, 'not first-party (mk on host)'),
                          ('g', 'maker', 'old', 'retired (old)'), (gc.LAUNCH_TABLE, 'h', 'old', None)])

    def test_an_out_of_scope_entry_needs_its_makers_notice_and_what_its_status_requires(self):
        """Rule (round 4, round 5 review F): "retired" and "current" come from cited vendor notices, not name lists;
        `unlisted` only on a page that enumerates the callable ids (`id_lists`); a deprecated entry carries the vendor's
        shutdown date (null when the page states none); a `-latest` pointer is `moving_alias`; another spelling of the
        maker's model cites the identity."""
        ok = {'a': {'status': 'unlisted', 'notice': notice()},
              'b': {'status': 'deprecated', 'date': '2026-10-01', 'shutdown': '2027-01-01', 'notice': notice()},
              'b2': {'status': 'deprecated', 'shutdown': None, 'notice': notice()},
              'm-latest': {'status': 'moving_alias', 'notice': notice()},
              'r': {'status': 'retired', 'notice': notice()},
              'p.c': {'status': 'retired', 'date': '2026-10-01', 'notice': notice(), 'model': 'c',
                      'same_as': {'kind': 'community', 'url': 'https://models.dev/api.json', 'as_of': '2026-11-10',
                                  'statement': 's'}}}
        bad = [{'x': {'status': 'gone', 'notice': notice()}},
               {'x': {'status': 'unlisted', 'notice': {**notice(), 'kind': 'community'}}},
               {'x': {'status': 'unlisted', 'notice': {**notice(), 'url': 'http://example.com'}}},
               {'x': {'status': 'unlisted', 'notice': {**notice(), 'url': 'https://example.com/other'}}},
               {'x': {'status': 'deprecated', 'notice': notice()}},
               {'x': {'status': 'deprecated', 'shutdown': 'late December', 'notice': notice()}},
               {'x': {'status': 'retired', 'shutdown': '2027-01-01', 'notice': notice()}},
               {'x': {'status': 'retired', 'date': 'October', 'notice': notice()}},
               {'p.x': {'status': 'unlisted', 'notice': notice(), 'model': 'x'}}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'oos.json')
            path.write_text(json.dumps({'schema': gc.OUT_OF_SCOPE_SCHEMA, 'id_lists': ID_LISTS, 'entries': ok}), encoding='utf-8')
            self.assertEqual(set(gc.load_out_of_scope(path)), set(ok))
            for entries in bad:
                path.write_text(json.dumps({'schema': gc.OUT_OF_SCOPE_SCHEMA, 'id_lists': ID_LISTS, 'entries': entries}),
                                encoding='utf-8')
                with self.assertRaises(ValueError, msg=entries):
                    gc.load_out_of_scope(path)
            path.write_text(json.dumps({'schema': gc.OUT_OF_SCOPE_SCHEMA, 'entries': ok}), encoding='utf-8')
            with self.assertRaises(ValueError):                 # no id_lists table
                gc.load_out_of_scope(path)
        found = [self.pair('a', 'high')]
        violations, _ = gc.check(found, {'entries': []}, {'a': ok['a'], 'gone': ok['b']})
        self.assertEqual(violations, ['on the out-of-scope list but offered by no saved catalog: gone'])

    def test_a_known_gap_names_its_missing_input_as_a_kind_of_the_closed_set(self):
        """Rule (round 5 review G): a known gap names the input that is missing as one kind of `GAP_KINDS`, which the
        file's `kinds` table states with what would close it, and its detail says it for the model."""
        entry = {'catalog': 'c', 'model': 'm', 'efforts': ['high'], 'row': 'm', 'kind': gc.GAP_KINDS[0], 'detail': 'd'}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'gaps.json')
            for e, kinds, good in ((entry, KINDS, True), ({**entry, 'kind': 'other'}, KINDS, False),
                                   ({**entry, 'detail': ' '}, KINDS, False), ({**entry, 'efforts': []}, KINDS, False),
                                   ({k: v for k, v in entry.items() if k != 'row'}, KINDS, False),
                                   ({**entry, 'reason': 'r'}, KINDS, False),
                                   (entry, {k: v for k, v in KINDS.items() if k != gc.GAP_KINDS[-1]}, False),
                                   (entry, {**KINDS, 'extra': {'missing': 'x', 'closes_when': 'y'}}, False),
                                   (entry, {**KINDS, gc.GAP_KINDS[0]: {'missing': 'x'}}, False)):
                path.write_text(json.dumps({'schema': gc.GAPS_SCHEMA, 'kinds': kinds, 'entries': [e]}), encoding='utf-8')
                if good:
                    self.assertEqual(gc.load_gaps(path)['entries'], [e])
                else:
                    with self.assertRaises(ValueError, msg=(e, kinds)):
                        gc.load_gaps(path)

    def test_an_alias_whose_names_models_dev_tells_apart_is_refused(self):
        """Rule (round 5 review A): where models.dev lists both names of an alias, they share a canonical id (unless the
        alias cites the maker's own statement) and, at a provider listing both, the list prices; a name missing from the
        names file asks for a rebuild; a shorthand is not a name a saved catalog offers or a shipped row."""
        listed = {'schema': gc.NAMES_SCHEMA, 'names': {
            'a': [['p', 'mk/a', 1, 2]], 'a-row': [['p', 'mk/a', 1, 2], ['q', 'mk/a', 1, 2]],
            'b': [['p', 'mk/b-0101', 1, 2]], 'b-row': [['p', 'mk/b', 1, 2]],
            'c': [['p', 'mk/c', 1, 2]], 'c-row': [['p', 'mk/c', 0, 0]],
            'short': [], 'row': []}}
        acc = {'models': {'a-row': {}, 'b-row': {}, 'c-row': {}, 'row': {}},
               'aliases': {'a': 'a-row', 'b': 'b-row', 'c': 'c-row', 'short': 'row'},
               'alias_sources': {'rows': {'short': {'kind': 'maintainer', 'shorthand': True}}}}
        catalogs = {'cat': {'rows': [{'provider': 'p', 'model': 'x/a', 'efforts': None}]}}
        out = gc.alias_violations(acc, listed, catalogs)
        self.assertEqual([v.split(':')[0] for v in out], ['alias b -> b-row', 'alias c -> c-row'])
        self.assertIn('different canonical ids', out[0])
        self.assertIn('different prices', out[1])
        maker = {**acc, 'alias_sources': {'rows': {**acc['alias_sources']['rows'], 'b': {'kind': 'vendor'}}}}
        self.assertEqual([v.split(':')[0] for v in gc.alias_violations(maker, listed, catalogs)], ['alias c -> c-row'])
        stale = {**listed, 'names': {k: v for k, v in listed['names'].items() if k != 'a'}}
        self.assertIn('not in the models.dev names file', gc.alias_violations(acc, stale, catalogs)[0])
        clash = {'cat': {'rows': [{'provider': 'p', 'model': 'host/short', 'efforts': None}]}}
        self.assertIn('shorthand short is a name the saved catalog cat offers', gc.alias_violations(acc, listed, clash))
        shipped = {**acc, 'models': {**acc['models'], 'short': {}}}
        self.assertIn('shorthand short is a shipped row', gc.alias_violations(shipped, listed, catalogs))

    def test_a_row_without_efforts_takes_the_launch_table_at_a_harness_and_default_at_a_magpie_preset(self):
        """Rule (`pairs`): a harness row that lists no efforts is offered at the launch table's efforts, and is a gap off
        it; a Magpie row that lists none is the model's one configuration, offered at `default`."""
        accounts = json.loads((gc.REFERENCES / 'accounts.json').read_text(encoding='utf-8'))
        five = gc.read_json(gc.REFERENCES / 'five-axis-scores.json')
        harness, table = next((h, t) for h, t in accounts['dispatch_efforts'].items() if t)
        model = next(iter(table))
        catalogs = {'m': {'provider_kind': 'harness', 'rows': [{'provider': harness, 'model': model, 'efforts': None},
                                                               {'provider': 'no-such-harness', 'model': model, 'efforts': None}]},
                    'g': {'provider_kind': 'magpie_preset', 'rows': [{'provider': harness, 'model': model, 'efforts': None}]}}
        found = gc.pairs(accounts, catalogs, five, {'schema_version': 3, 'scores': {}})
        self.assertEqual([p['effort'] for p in found if p['catalog'] == 'm' and p['provider'] == harness], list(table[model]))
        other = [p for p in found if p['provider'] == 'no-such-harness']
        self.assertEqual(len(other), 1)
        self.assertIn('not in dispatch_efforts', other[0]['missing'][0])
        self.assertEqual([p['effort'] for p in found if p['catalog'] == 'g'], ['default'])

    def test_missing_at_names_each_facet_and_the_time_and_reads_an_effort_off_the_scale_where_declared(self):
        """Rule (`missing_at`): a pair is covered only when every facet and Time/Task has a value at the effort; `default`
        reads the effort the data declares."""
        def info():
            by = {'high': 0.0, 'max': 0.1}
            cats = {}
            for c, f in gc.FACETS:
                cats.setdefault(c, {'facets': {}})['facets'][f] = {'by_effort': dict(by)}
            return {'five_axis': {'effort_readings': {'default': 'high'}, 'categories': cats,
                                  'duration': {'by_effort': dict(by)}}}
        full = info()
        self.assertEqual([gc.missing_at(full, e) for e in ('high', 'max', 'default')], [[], [], []])
        self.assertEqual(gc.missing_at(full, 'low'), [f'{c}.{f}' for c, f in gc.FACETS] + ['time_per_task'])
        no_time = info()
        del no_time['five_axis']['duration']['by_effort']['high']
        self.assertEqual((gc.missing_at(no_time, 'high'), gc.missing_at(no_time, 'default'), gc.missing_at(no_time, 'max')),
                         (['time_per_task'], ['time_per_task'], []))
        no_facet = info()
        del no_facet['five_axis']['categories']['coding']['facets']['faithfulness']['by_effort']['max']
        self.assertEqual(gc.missing_at(no_facet, 'max'), ['coding.faithfulness'])

    def test_the_launch_table_rows_are_every_dispatch_entry_and_every_bundle(self):
        """Rule (`launch_rows`): the check also covers every (row, effort) the shipped launch table offers; an effort-less
        bundle is offered at `default`."""
        accounts = {'dispatch_efforts': {'h': {'m1': ['low', 'ultra']}},
                    'accounts': {'a': {'bundles': {'b1': {'model': 'm2', 'effort': 'max'}, 'b2': {'model': 'm3'}}}}}
        self.assertEqual(gc.launch_rows(accounts), [{'provider': 'h', 'model': 'm1', 'efforts': ['low', 'ultra']},
                                                    {'provider': 'a', 'model': 'm2', 'efforts': ['max']},
                                                    {'provider': 'a', 'model': 'm3', 'efforts': ['default']}])
        found, _, _ = gc.run()
        shipped = json.loads((gc.REFERENCES / 'accounts.json').read_text(encoding='utf-8'))
        offered = {(p['provider'], p['model'], p['effort']) for p in found if p['catalog'] == gc.LAUNCH_TABLE}
        for row in gc.launch_rows(shipped):
            for e in row['efforts']:
                self.assertIn((row['provider'], row['model'], e), offered)

    def test_a_catalog_of_another_schema_kind_row_shape_or_gate_is_refused(self):
        good = {'schema': gc.CATALOG_SCHEMA, 'provider_kind': 'magpie_preset', 'gate': {'makers': {'p': 'mk'}, 'statement': 's'},
                'rows': [{'provider': 'p', 'model': 'm', 'efforts': None, 'maker': 'mk'}]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'x.json')
            path.write_text(json.dumps(good), encoding='utf-8')
            self.assertEqual(set(gc.load_catalogs(tmp)), {'x'})
            for doc in ({**good, 'schema': 'agent-compass.gateway-catalog/1'}, {**good, 'provider_kind': 'other'},
                        {**good, 'rows': [{'harness': 'p', 'model': 'm', 'efforts': None, 'maker': 'mk'}]},
                        {**good, 'rows': [{'provider': 'p', 'model': 'm', 'efforts': None}]},
                        {**good, 'rows': [{'provider': 'p', 'model': 'm', 'efforts': None, 'maker': 'mk', 'account': 'a'}]},
                        {**good, 'gate': {'makers': {'p': None}, 'statement': 's'}},
                        {**good, 'gate': {'makers': {'p': 'mk'}}}):
                path.write_text(json.dumps(doc), encoding='utf-8')
                with self.assertRaises(ValueError, msg=doc):
                    gc.load_catalogs(tmp)


if __name__ == '__main__':
    unittest.main()
