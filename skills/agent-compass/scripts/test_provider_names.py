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

    def test_a_decorated_spelling_reads_the_declarations_of_the_name_it_reaches(self):
        """Rule (resolve_model: declarations read for each undecorated spelling before its row): the spellings catalogs
        use (case, a dash or colon effort, -latest, -preview, a date tag, a provider prefix) read what the plain name
        reads, the maker's default and a provider's declaration alike; the effort the name asked for is kept."""
        for name, entry in SHIPPED['provider_names']['defaults'].items():
            for spelling, effort in ((name.upper(), None), (f'{name}-high', 'high'), (f'{name}:high', 'high'),
                                     (f'{name}-latest', None), (f'{name}-preview', None), (f'{name}@20261017', None),
                                     (f'vendor/{name}-xhigh', 'xhigh')):
                with self.subTest(spelling=spelling):
                    found = account_fleet.resolve_model(spelling, SHIPPED)
                    self.assertEqual((found['key'], found['source'], found['effort']), (entry['model'], 'default', effort))
        a, b = two_rows()
        doc = doc_with(rows={'acme': {a.lower(): decl(b)}})
        for spelling in (f'{a}-latest', f'{a}-high', f'acme/{a}-latest'):
            with self.subTest(spelling=spelling):
                providers = () if spelling.startswith('acme/') else ('acme',)
                self.assertEqual(account_fleet.resolve_model(spelling, doc, providers)['key'], b)

    def test_a_full_id_is_read_before_its_last_segment(self):
        """Rule (model_resolution.default_for): a `defaults` key with an owner part says what that full id is, read before
        the last segment's declaration, on any provider, in any case, behind a provider prefix; another owner part reads
        the last segment's."""
        a, b = two_rows()
        doc = doc_with(defaults={'moved': decl(b), 'owner/moved': decl(a)})
        for name, providers, want in (('owner/moved', (), a), ('Owner/Moved', ('host',), a), ('host/owner/moved', (), a),
                                      ('owner/moved-latest', (), a), ('other/moved', (), b), ('moved', ('host',), b)):
            with self.subTest(name=name, providers=providers):
                self.assertEqual(account_fleet.resolve_model(name, doc, providers)['key'], want)
        for name, entry in SHIPPED['provider_names']['defaults'].items():
            if '/' in name:
                with self.subTest(shipped=name):
                    self.assertEqual(account_fleet.resolve_model(name.upper(), SHIPPED, ('somehost',))['key'], entry['model'])

    def test_a_spelling_that_is_a_row_itself_is_that_row(self):
        """Rule: the declarations of an undecorated spelling are read only where resolution would reach that spelling's
        row: a decorated spelling that is a row of its own (a dated snapshot) stays that row."""
        for name, entry in SHIPPED['provider_names']['defaults'].items():
            if '/' in name:
                continue                       # a full id names fixed weights: its row is the undated one
            with self.subTest(row=entry['model']):
                self.assertEqual(account_fleet.resolve_model(entry['model'], SHIPPED)['source'], 'exact')

    def test_provider_ids_are_matched_lowercase(self):
        a, b = two_rows()
        doc = doc_with(rows={'acme': {a.lower(): decl(b)}})
        for name, providers in ((a, ('ACME',)), (a, ('Acme',)), (f'AcMe/{a}', ())):
            with self.subTest(name=name, providers=providers):
                found = account_fleet.resolve_model(name, doc, providers)
                self.assertEqual((found['key'], found['provider']), (b, 'acme'))

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
            'default not the maker or the maintainer': {'defaults': {name: {**entry, 'kind': 'community'}}},
            'default whose name is no row': {'defaults': {'no-row-name': dict(entry)}},
            'provider id not lowercase': {'rows': {provider.upper(): dict(rows)}},
            'default with no row': {'defaults': {name: {**entry, 'model': None}}},
            'row the catalog lacks': {'rows': {provider: {'x-name': decl('no-such-row')}}},
            'no row without status': {'rows': {provider: {'x-name': decl(None)}}},
            'retired not from the maker': {'rows': {provider: {'x-name': decl(None, status='retired', kind='community')}}},
            'undated': {'rows': {provider: {'x-name': {**decl(entry['model']), 'as_of': None}}}},
            'uppercase name': {'rows': {provider: {'X-Name': decl(entry['model'])}}},
            'name with an empty part': {'rows': {provider: {'a//b': decl(entry['model'])}}},
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

    def test_a_stored_score_file_keeps_its_keys(self):
        """Rule (AGENTS.md, what a released version wrote is kept): reading a score file never rewrites or drops a key;
        a key that is a moved name still names a row (unresolved_models lists none of them)."""
        layer = {name: {'coding.faithfulness': {'level': 2.0, 'as_of': '2026-11-11', 'kind': 'user'}}
                 for name in SHIPPED['provider_names']['defaults']}
        before = copy.deepcopy(layer)
        user_scores.score_layer(layer, SHIPPED)
        self.assertEqual(layer, before)
        self.assertEqual(user_scores.unresolved_models({'scores': layer}, SHIPPED), [])


    def test_a_name_stored_for_an_account_reads_what_derive_read_for_that_account(self):
        """Rule (account_fleet.stored_model, account_providers): a temporary priority stored for an account under a bare
        name (v0.7.1 stored the row key) is read with the serving providers derive used there: on an endpoint account the
        endpoint (so the provider's declaration applies, as it does to the bundle), on a launcher account none (as at
        v0.7.1, moved only by a maker's default)."""
        a, b = two_rows()
        table = {'statement': 't', 'defaults': {'moved-name': decl(b)},
                 'rows': {'gw': {a.lower(): decl(None, status='other_weights')}}}
        for kind, listed, reaches in (
                ('openai_compatible', [a, 'moved-name'], {a: [a], 'moved-name': ['moved-name']}),
                ('paseo_provider', [a, f'gw/{a}', 'moved-name'], {a: [a], 'moved-name': ['moved-name']})):
            acc = gateway_accounts(table)
            acc['accounts']['gw']['bundles'] = auto_bundles.derive(acc, catalog(listed, kind=kind), NOW)['bundles']['gw']
            by_launch = {bl['launch_model']: bl for bl in acc['accounts']['gw']['bundles'].values()}
            for stored, launches in reaches.items():
                with self.subTest(kind=kind, stored=stored):
                    got = sorted(launch for launch, bl in by_launch.items()
                                 if account_fleet.stored_model(stored, acc, 'gw') == account_fleet.canonical(bl['model'], acc))
                    self.assertEqual(got, launches)

    def test_what_a_writer_stores_reads_its_bundle_again(self):
        """Rule (account_fleet.stored_form): the value a priority writer stores for a bundle's advertised name reads the
        bundle's row again, on both catalog kinds, for a moved name, a repository id and a name with no row there."""
        a, b = two_rows()
        table = {'statement': 't', 'defaults': {a.lower(): decl(b), f'owner/{a.lower()}': decl(a)},
                 'rows': {'gw': {'gone': decl(None, status='other_weights')}}}
        for kind, listed in (('openai_compatible', [a, f'owner/{a}', 'gone']), ('paseo_provider', [a, f'owner/{a}', 'gw/gone'])):
            acc = gateway_accounts(table)
            acc['accounts']['gw']['bundles'] = auto_bundles.derive(acc, catalog(listed, kind=kind), NOW)['bundles']['gw']
            for bundle in acc['accounts']['gw']['bundles'].values():
                with self.subTest(kind=kind, launch=bundle['launch_model']):
                    stored = account_fleet.stored_form(bundle['launch_model'], acc, 'gw')
                    self.assertEqual(account_fleet.stored_model(stored, acc, 'gw'), account_fleet.canonical(bundle['model'], acc))

ENTRY = {'level': 2.0, 'as_of': '2026-11-11', 'kind': 'user'}


class ScoreKeys(unittest.TestCase):
    """A score names a model, not a provider's offer; a name whose maker moved it means what it means now (owner,
    2026-11-19): user_scores.resolve_model_key, score_layer."""

    def test_a_moved_name_key_lands_on_the_row_its_maker_declares(self):
        for name, entry in SHIPPED['provider_names']['defaults'].items():
            for key in (name, f'some-gateway/{name}'):
                with self.subTest(key=key):
                    self.assertEqual(user_scores.resolve_model_key(key, SHIPPED), entry['model'])

    def test_a_score_key_reads_no_providers_declaration(self):
        no_rows = doc_with(defaults=SHIPPED['provider_names']['defaults'])
        for provider, rows in SHIPPED['provider_names']['rows'].items():
            for name in rows:
                with self.subTest(key=f'{provider}/{name}'):
                    self.assertEqual(user_scores.resolve_model_key(f'{provider}/{name}', SHIPPED),
                                     user_scores.resolve_model_key(f'{provider}/{name}', no_rows))

    def test_a_key_no_default_names_resolves_as_with_nothing_declared(self):
        bare, defaults = doc_with(), SHIPPED['provider_names']['defaults']
        names = [*SHIPPED['models'], *SHIPPED.get('aliases', {}), 'vendor/claude-opus-future', 'no-such-model']
        for name in names:
            if name.rsplit('/', 1)[-1].lower() in defaults:
                continue
            with self.subTest(key=name):
                self.assertEqual(user_scores.resolve_model_key(name, SHIPPED), user_scores.resolve_model_key(name, bare))

    def test_a_row_takes_a_score_under_the_key_that_reaches_it(self):
        """Rule (user_scores.score_key_for_row, set_score): a row whose own id lands on it is keyed by its id; a row
        whose id lands elsewhere (the undated row of a moved name) is keyed by a declared name that reaches it (a
        repository id), and set_score writes that key, never the row id; a row nothing reaches takes none."""
        a, b = two_rows()
        doc = doc_with(defaults={a.lower(): decl(b), f'owner/{a.lower()}': decl(a)})
        self.assertEqual(user_scores.score_key_for_row(b, doc), b)
        self.assertEqual(user_scores.score_key_for_row(a, doc), f'owner/{a.lower()}')
        self.assertIsNone(user_scores.score_key_for_row(a, doc_with(defaults={a.lower(): decl(b)})))
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / 'user-scores.json'
            got = user_scores.set_score(f'owner/{a.lower()}', 'creativity', 2.0, accounts=doc, local_path=local)
            self.assertEqual(got['model'], f'owner/{a.lower()}')
            self.assertEqual(user_scores.resolve_model_key(got['model'], doc), a)
            got = user_scores.set_score(b, 'creativity', 2.0, accounts=doc, local_path=local)
            self.assertEqual(got['model'], b)

    def test_the_rows_own_key_wins_over_a_moved_name_for_each_target_whatever_the_order(self):
        a, b = two_rows()
        doc = doc_with(defaults={a.lower(): decl(b)})
        moved = {'coding.faithfulness': {**ENTRY, 'level': 1.0}, 'creativity': {**ENTRY, 'level': 1.5}}
        own = {'coding.faithfulness': {**ENTRY, 'level': 2.5}}
        for layer in ({a: moved, b: own}, {b: own, a: moved}):
            with self.subTest(order=list(layer)):
                grouped = user_scores.score_layer(layer, doc)
                self.assertEqual(set(grouped), {b})
                self.assertEqual(grouped[b], {'coding.faithfulness': own['coding.faithfulness'],
                                              'creativity': moved['creativity']})

    def test_two_moved_names_of_one_row_merge_whatever_the_order(self):
        a, b = two_rows()
        doc = doc_with(defaults={'moved-one': decl(b), 'moved-two': decl(b)})
        one, two = {'creativity': {**ENTRY, 'level': 1.0}}, {'creativity': {**ENTRY, 'level': 3.0}}
        got = [user_scores.score_layer(layer, doc) for layer in ({'moved-one': one, 'moved-two': two},
                                                                  {'moved-two': two, 'moved-one': one})]
        self.assertEqual(got[0], got[1])


class BundleModelsReadTheirRowAgain(unittest.TestCase):
    def test_a_derived_bundles_model_reads_the_row_it_was_derived_for_with_no_provider_known(self):
        """Rule (auto_bundles.default_profile): every later resolution of a bundle's model, which knows no provider,
        reads the row the advertised name resolved to on its endpoint: an endpoint declaring the undated row of a moved
        name, an alias of such a row and a moved name each keep their row."""
        defaults = SHIPPED['provider_names']['defaults']
        rows = {'gw': {name: decl(name) for name in defaults if '/' not in name}}
        acc = gateway_accounts({'statement': 't', 'defaults': defaults, 'rows': rows})
        alias = next((al for al, row in SHIPPED.get('aliases', {}).items() if row in defaults), None)
        names = [*defaults, *([alias] if alias else [])]
        for kind, served in (('openai_compatible', ('gw',)), ('paseo_provider', ())):
            out = auto_bundles.derive(acc, catalog(names, kind=kind), NOW)['bundles']['gw']
            for bundle in out.values():
                with self.subTest(kind=kind, launch=bundle['launch_model']):
                    want = model_resolution.resolve_in(bundle['launch_model'], acc, served)['key']
                    self.assertEqual(account_fleet.row_key(bundle['model'], acc), want)


if __name__ == '__main__':
    unittest.main()
