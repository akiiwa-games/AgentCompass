"""Which row a served name is (provider_names, owner 2026-11-19): rules of model_resolution, auto_bundles and the shipped
declarations. Properties of the declared rules only; no routing outcome is pinned."""
import copy
import itertools
import json
from pathlib import Path
import unittest

import account_fleet
import auto_bundles
import model_resolution
import public_data
import reference_provenance
import user_scores

from router_test_support import isolated_router_state


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup)


SHIPPED = json.loads((Path(__file__).resolve().parents[1] / 'references' / 'accounts.json').read_text(encoding='utf-8'))
NOW = 2_000_000_000.0


def doc_with(defaults=None, rows=None, base=SHIPPED):
    out = copy.deepcopy(base)
    out['provider_names'] = {'statement': 'test', 'defaults': defaults or {}, 'rows': rows or {}}
    return out


def decl(model, **extra):
    return {'model': model, 'kind': 'vendor', 'url': 'https://example.com/x', 'as_of': '2026-11-19', 'statement': 's',
            **extra}


def two_rows():
    """Two scored rows of the shipped catalog with different cells, neither an alias target the other resolves through."""
    rows = [m for m, v in SHIPPED['models'].items() if isinstance(v, dict) and v.get('five_axis')]
    rows = rows or [m for m, v in SHIPPED['models'].items() if isinstance(v, dict)]
    a = next(m for m in rows if m not in SHIPPED.get('aliases', {}).values())
    b = next(m for m in rows if m != a and SHIPPED['models'][m] != SHIPPED['models'][a])
    return a, b


class ResolutionRules(unittest.TestCase):
    def test_a_declared_pair_reads_the_declared_row_and_never_the_same_name_row(self):
        """Rule: a name on a provider whose declaration names row R reads R's row (cells and price basis), source
        `provider`; the same-name row is not read."""
        a, b = two_rows()
        doc = doc_with(rows={'acme': {a.lower(): decl(b)}})
        found = account_fleet.resolve_model(a, doc, ('acme',))
        self.assertEqual((found['key'], found['source'], found['provider']), (b, 'provider', 'acme'))
        self.assertIs(account_fleet.model_spec(doc, a, ('acme',)), doc['models'][b])
        self.assertIsNot(account_fleet.model_spec(doc, a, ('acme',)), doc['models'][a])
        self.assertEqual(account_fleet.row_key(a, doc, ('acme',)), b)

    def test_without_a_declaration_a_name_resolves_as_with_no_table(self):
        """Rule: the same name with no provider, or on a provider with no declaration, resolves as it does when nothing
        is declared."""
        a, b = two_rows()
        doc, bare = doc_with(rows={'acme': {a.lower(): decl(b)}}), doc_with()
        for name in (a, f'other/{a}', f'{a}:high', 'claude-opus-future', 'no-such-vendor/x'):
            for providers in ((), ('other',)):
                with self.subTest(name=name, providers=providers):
                    self.assertEqual(account_fleet.resolve_model(name, doc, providers),
                                     account_fleet.resolve_model(name, bare, providers))

    def test_resolution_does_not_depend_on_the_order_of_declarations_or_endpoints(self):
        """Rule: invariance to input order; endpoints that declare different rows give `conflict` whatever the order."""
        a, b = two_rows()
        rows = {'acme': {a.lower(): decl(b)}, 'relay': {a.lower(): decl(a)}, 'zed': {a.lower(): decl(b)}}
        seen = set()
        for order in itertools.permutations(rows):
            doc = doc_with(rows={k: rows[k] for k in order})
            for endpoints in itertools.permutations(('acme', 'zed')):
                seen.add(tuple(account_fleet.resolve_model(a, doc, endpoints).items()))
            for endpoints in itertools.permutations(('acme', 'relay')):
                self.assertEqual(account_fleet.resolve_model(a, doc, endpoints)['source'], 'conflict')
        self.assertEqual(len(seen), 1)

    def test_a_provider_declaration_beats_the_makers_default_which_beats_the_same_name_row(self):
        """Rule (owner, 2026-11-19): a provider's own declaration, else the maker's default (on any provider and with
        no provider known), else today's resolution."""
        a, b = two_rows()
        doc = doc_with(defaults={a.lower(): decl(b)}, rows={'acme': {a.lower(): decl(a)}})
        for providers in ((), ('relay',)):
            found = account_fleet.resolve_model(a, doc, providers)
            self.assertEqual((found['key'], found['source']), (b, 'default'))
        self.assertEqual(account_fleet.resolve_model(f'relay/{a}', doc)['key'], b)       # a prefix with no declaration
        found = account_fleet.resolve_model(a, doc, ('acme',))
        self.assertEqual((found['key'], found['source']), (a, 'provider'))
        self.assertEqual(account_fleet.resolve_model(f'acme/{a}', doc)['source'], 'provider')

    def test_a_name_with_no_row_on_its_provider_has_no_cells(self):
        """Rule: a declaration with no row (retired, other_weights) leaves the pair with no row and no cells there, and
        its row key is not the row the name is elsewhere."""
        a, _ = two_rows()
        for status in model_resolution.PROVIDER_NAME_STATUSES:
            doc = doc_with(rows={'acme': {a.lower(): decl(None, status=status)}})
            with self.subTest(status=status):
                found = account_fleet.resolve_model(a, doc, ('acme',))
                self.assertEqual((found['key'], found['source']), (None, status))
                self.assertEqual(account_fleet.model_spec(doc, a, ('acme',)), {})
                self.assertNotEqual(account_fleet.row_key(a, doc, ('acme',)), a)
                self.assertEqual(account_fleet.resolve_model(a, doc)['key'], a)          # elsewhere: as before

    def test_an_endpoints_ids_are_its_own_namespace_a_launchers_ids_name_the_provider(self):
        """Rule (serving_providers): with an endpoint known, a `provider/name` id is in the endpoint's namespace; with
        none, the provider part names the serving provider."""
        self.assertEqual(model_resolution.serving_providers('acme/m:high'), [('acme', 'm')])
        self.assertEqual(model_resolution.serving_providers('acme/m', ('relay',)), [('relay', 'acme/m')])
        self.assertEqual(model_resolution.serving_providers('m'), [])
        a, b = two_rows()
        doc = doc_with(rows={'acme': {a.lower(): decl(b)}})
        self.assertEqual(account_fleet.resolve_model(f'acme/{a}', doc)['key'], b)
        self.assertNotEqual(account_fleet.resolve_model(f'acme/{a}', doc, ('relay',))['key'], b)

    def test_a_document_without_the_table_is_a_named_error_not_an_empty_table(self):
        """Rule: no silent fallback on a missing key."""
        doc = {k: v for k, v in SHIPPED.items() if k != 'provider_names'}
        with self.assertRaisesRegex(public_data.ReferenceDataError, 'provider_names'):
            account_fleet.resolve_model('anything', doc)


class ShippedDeclarations(unittest.TestCase):
    def test_every_declaration_is_cited_and_names_a_row_or_a_status(self):
        """Rule (reference_provenance._validate_provider_names): the shipped table passes; each broken declaration is
        refused."""
        reference_provenance.validate_accounts_provenance(SHIPPED)
        table = SHIPPED['provider_names']
        name, entry = next(iter(table['defaults'].items()))
        provider, rows = next(iter(table['rows'].items()))
        breaks = {
            'default not the maker': {'defaults': {name: {**entry, 'kind': 'community'}}},
            'default with no row': {'defaults': {name: {**entry, 'model': None}}},
            'row the catalog lacks': {'rows': {provider: {'x-name': decl('no-such-row')}}},
            'no row without status': {'rows': {provider: {'x-name': decl(None)}}},
            'retired not from the maker': {'rows': {provider: {'x-name': decl(None, status='retired', kind='community')}}},
            'undated': {'rows': {provider: {'x-name': {**decl(entry['model']), 'as_of': None}}}},
            'uppercase name': {'rows': {provider: {'X-Name': decl(entry['model'])}}},
            'name with provider part': {'rows': {provider: {'a/b': decl(entry['model'])}}},
        }
        for label, change in breaks.items():
            broken = copy.deepcopy(SHIPPED)
            for part, value in change.items():
                broken['provider_names'][part] = {**broken['provider_names'][part], **value}
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, 'provider_names'):
                reference_provenance.validate_accounts_provenance(broken)

    def test_a_shipped_bundle_runs_the_row_its_launch_name_is(self):
        """Rule: a shipped bundle whose launch name a declaration makes another row runs that row."""
        broken = copy.deepcopy(SHIPPED)
        name, entry = next(iter(broken['provider_names']['defaults'].items()))
        account = next(iter(broken['accounts']))
        broken['accounts'][account].setdefault('bundles', {})['x'] = {'model': name, 'effort': 'high'}
        with self.assertRaisesRegex(ValueError, 'the bundle runs'):
            reference_provenance.validate_accounts_provenance(broken)
        broken['accounts'][account]['bundles']['x'] = {'model': entry['model'], 'launch_model': name, 'effort': 'high'}
        reference_provenance.validate_accounts_provenance(broken)


def gateway_accounts(provider_names=None):
    base = copy.deepcopy(SHIPPED)
    if provider_names is not None:
        base['provider_names'] = provider_names
    base['accounts'] = {'gw': {'billing': 'metered', 'backend_kind': 'metered_usd', 'family': 'codex',
                               'windows': {'balance': {}}, 'bundles': {}}}
    return base


def catalog(names, kind='openai_compatible'):
    return {'providers': {'gw': {'ok': True, 'type': kind, 'checked_at': NOW, 'models': [{'id': n} for n in names]}}}


class DerivedBundles(unittest.TestCase):
    def test_a_discovered_name_runs_the_row_its_declaration_names_and_keeps_its_launch_name(self):
        """Rule (auto_bundles.default_profile, derive): on an endpoint the bundle's model is the row the name resolves
        to with that endpoint as serving provider, its launch_model the advertised name; a name with no row there is
        out of automatic routing with no cells (its model qualified by the declaring provider)."""
        a, b = two_rows()
        table = {'statement': 't', 'defaults': {}, 'rows': {'gw': {a.lower(): decl(b)}, 'zed': {'gone': decl(None, status='retired')}}}
        acc = gateway_accounts(table)
        out = auto_bundles.derive(acc, catalog([a, 'zed/gone']), NOW)['bundles']['gw']
        by_launch = {bundle['launch_model']: bundle for bundle in out.values()}
        self.assertEqual(by_launch[a]['model'], b)
        merged = auto_bundles.merge_into(acc, {'bundles': {'gw': out}})
        self.assertIs(account_fleet.model_spec(merged, by_launch[a]['model']), merged['models'][b])
        # an endpoint's id is its own namespace: zed's declaration does not reach `zed/gone` listed by gw
        self.assertNotEqual(by_launch['zed/gone'].get('score_source'), 'retired')
        launcher = auto_bundles.derive(acc, catalog(['zed/gone'], kind='paseo_provider'), NOW)['bundles']['gw']
        gone = next(iter(launcher.values()))
        self.assertEqual((gone['score_source'], account_fleet.model_spec(merged, gone['model'])), ('retired', {}))

    def test_the_default_checkbox_reads_the_row_the_name_resolves_to(self):
        """Rule (auto_bundles.superseded_by): a declared name's checkbox default is its row's `superseded` entry, never
        an entry of the row the name is elsewhere."""
        for name, entry in SHIPPED['provider_names']['defaults'].items():
            row = (SHIPPED['superseded'].get(entry['model']) or {}).get('by')
            with self.subTest(name=name):
                self.assertEqual(auto_bundles.superseded_by(name, SHIPPED), row)


class StoredStateIsKept(unittest.TestCase):
    """What v0.7.1 wrote for the user (AGENTS.md): v0.7.1 declared no provider_names."""
    NAMES = ['deepseek-v4-pro', 'deepseek-v4-flash', 'deepseek-flash', 'deepseek-v4.1-flash']

    def v071_ids(self):
        """{advertised name: the id v0.7.1 gave its bundle}: derive with nothing declared, which is how v0.7.1
        resolved (released_names.as_v0_7_0 replays the same)."""
        bare = gateway_accounts({'statement': 'v0.7.1', 'defaults': {}, 'rows': {}})
        out = auto_bundles.derive(bare, catalog(self.NAMES), NOW)['bundles']['gw']
        return {b['launch_model']: bid for bid, b in out.items()}

    def test_v071_ids_are_the_ones_v071_wrote(self):
        ids = self.v071_ids()
        self.assertEqual(ids['deepseek-v4-pro'], 'auto:gw:codex:deepseek-v4-pro@high')
        self.assertEqual(ids['deepseek-v4-flash'], 'auto:gw:codex:deepseek-v4-flash@high')

    def test_a_choice_stored_under_the_v071_id_is_read_by_the_bundle_of_that_name_now(self):
        """Rule (auto_bundles.choice_for, `_v0_7_0_id`): a bundle whose model now resolves to another row reads the choice
        the user stored under the id v0.7.1 gave it, checked or unchecked; names that are one row now and hold choices
        that disagree leave the bundle disabled and listed (one checkbox per model)."""
        ids = self.v071_ids()
        acc = gateway_accounts()
        bare = gateway_accounts({'statement': 'v0.7.1', 'defaults': {}, 'rows': {}})
        replayed = {b['launch_model']: b['_v0_7_0_id']
                    for b in auto_bundles.derive(bare, catalog(self.NAMES), NOW)['bundles']['gw'].values()}
        for name, value in itertools.product(self.NAMES, (True, False)):
            doc = {'schema_version': 2, 'accounts': {'gw': {'bundles': {ids[name]: {'disabled': value}}}}}
            out = auto_bundles.derive(acc, catalog(self.NAMES), NOW, overrides=doc)['bundles']['gw']
            bundle = next(b for b in out.values() if b['launch_model'] == name)
            with self.subTest(name=name, value=value):
                # the id v0.7.0 gave the name is replayed as it was: no release before this one declared provider_names
                self.assertEqual(bundle['_v0_7_0_id'], replayed[name])
                self.assertIn(ids[name], bundle.get('_choice_ids') or [])
                self.assertEqual(bundle['disabled'], value)
        doc = {'schema_version': 2, 'accounts': {'gw': {'bundles': {ids[n]: {'disabled': n == 'deepseek-v4-flash'}
                                                                    for n in self.NAMES}}}}
        out = auto_bundles.derive(acc, catalog(self.NAMES), NOW, overrides=doc)['bundles']['gw']
        for bundle in out.values():
            mates = [n for n in self.NAMES if account_fleet.row_key(n, acc) == bundle['model']]
            values = {n == 'deepseek-v4-flash' for n in mates}
            with self.subTest(launch=bundle['launch_model']):
                self.assertEqual(bundle['disabled'], True in values)
                self.assertEqual(bool(bundle.get('_choice_conflict')), len(values) > 1)

    def test_a_user_score_entered_under_a_name_lands_where_v071_put_it(self):
        """Rule (user_scores.resolve_model_key, unchanged): a score keyed by a name resolves to the row v0.7.1 resolved it
        to (its name as a row, then an alias), whatever the name's provider declarations say now."""
        bare = doc_with()
        for name in [*SHIPPED['provider_names']['defaults'],
                     *(f'{p}/{n}' for p, rows in SHIPPED['provider_names']['rows'].items() for n in rows)]:
            with self.subTest(name=name):
                self.assertEqual(user_scores.resolve_model_key(name, SHIPPED), user_scores.resolve_model_key(name, bare))


if __name__ == '__main__':
    unittest.main()
