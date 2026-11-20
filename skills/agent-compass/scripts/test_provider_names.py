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

    def test_any_spelling_of_a_row_writes_one_key_and_clear_removes_every_key_landing_on_it(self):
        """Rule (user_scores.write_key, clear_score): every writer stores score_key_for_row of the row the given spelling
        resolves to, so two spellings leave one key; clearing a row removes every stored key that lands on it, also one
        written before this rule or by hand; keys of other rows stay."""
        a, b = two_rows()
        doc = doc_with(defaults={a.lower(): decl(b), f'owner/{a.lower()}': decl(a)})
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / 'user-scores.json'
            for spelling in (f'Owner/{a.upper()}', f'owner/{a.lower()}'):
                user_scores.set_score(spelling, 'creativity', 2.0, accounts=doc, local_path=local)
            self.assertEqual(list(user_scores.load_local(local)['scores']), [f'owner/{a.lower()}'])
            hand = json.loads(local.read_text(encoding='utf-8'))
            hand['scores'][f'Owner/{a.upper()}'] = {'creativity': dict(ENTRY)}
            hand['scores'][b] = {'creativity': dict(ENTRY)}
            local.write_text(json.dumps(hand), encoding='utf-8')
            user_scores.clear_score(f'owner/{a.lower()}', 'creativity', accounts=doc, local_path=local)
            left = user_scores.load_local(local)['scores']
            self.assertEqual([k for k in left if user_scores.resolve_model_key(k, doc) == a], [])
            self.assertEqual(list(left), [b])

    def test_a_row_its_own_id_reaches_is_written_as_v071_wrote_it(self):
        """Rule: for every row whose id lands on it, write_key is the key v0.7.1 wrote (resolve_model_key)."""
        for row in SHIPPED['models']:
            if user_scores.resolve_model_key(row, SHIPPED) == row:
                with self.subTest(row=row):
                    self.assertEqual(user_scores.write_key(row, SHIPPED), row)

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


class PinIdentityMatchesBundle(unittest.TestCase):
    """Rule (account_fleet.pin_names_bundle, candidate_eval.model_pin_matches): a pin whose identity is bundle A's and
    not bundle B's never selects B, whatever the order of the pool; every call site and neighbour reads the same rule."""

    @classmethod
    def setUpClass(cls):
        from datetime import datetime, timezone
        from router_test_support import profile, usage_windows
        cls.at = datetime(2026, 11, 19, 3, 0, tzinfo=timezone.utc)
        cls.prof = profile(primary_level=2, local_only=True, reason='pin identity rule')
        cls.usage = usage_windows(cls.at, {
            'codex': [('usage', 90, 100)],
            'opencode': [('balance', 90, 100)],
            'gw': [('balance', 90, 100)],
        })

    def setUp(self):
        import live_models
        live_models.invalidate()
        self.addCleanup(live_models.invalidate)

    def subscription_fleet(self, account_bundles, extra_rows=None):
        doc = copy.deepcopy(SHIPPED)
        if extra_rows:
            for prov, entries in extra_rows.items():
                doc['provider_names']['rows'] = {**doc['provider_names']['rows'],
                                                 prov: {**(doc['provider_names']['rows'].get(prov) or {}), **entries}}
        doc['accounts'] = {}
        for acct, (family, bundles, win) in account_bundles.items():
            doc['accounts'][acct] = {
                'billing': 'subscription', 'plan': 'pro', 'verified': True, 'family': family,
                'windows': {win: {'hours': 168}}, 'capacity_usd': {win: 100.0},
                'capacity_source': {win: {'evidence': 'test'}}, 'bundles': bundles,
            }
        return doc

    def plan_pin(self, fleet, **constraints):
        import allocator
        task = {'objective': 'x', 'profile': self.prof, 'expected_hours': 0.5, 'disable_fast': True,
                'constraints': constraints}
        return allocator.plan(task, self.usage, self.at, accounts=copy.deepcopy(fleet), overrides=[],
                              read_local_state=False)

    def assert_pin_isolates(self, items, pin_constraints, mine_ids, other_ids, extra_rows=None):
        """Across both pool orders, `pin_constraints` never leaves any bundle in `other_ids` eligible or selected, and
        every bundle in `mine_ids` clears `explicit model constraint`."""
        for ordered in (list(items), list(reversed(items))):
            fleet = self.subscription_fleet({'codex': ('codex', dict(ordered), 'usage')}, extra_rows=extra_rows)
            planned = self.plan_pin(fleet, **pin_constraints)
            by_id = {c['id']: c for c in planned['ranking']}
            for oid in other_ids:
                self.assertIn('explicit model constraint', by_id[oid]['excluded'], (ordered, pin_constraints, oid))
                self.assertNotEqual(planned['choice'], oid)
            for mid in mine_ids:
                self.assertNotIn('explicit model constraint', by_id[mid]['excluded'], (ordered, pin_constraints, mid))

    def test_no_row_pairs_and_moved_names_never_cross_namespaces_in_any_pool_order(self):
        """Defects 1 & 2 + retired pairs + uncalibrated provider/name pairs: no leaf-against-leaf or resolved-id-against-raw crossing."""
        doc = copy.deepcopy(SHIPPED)
        doc['provider_names']['rows'] = {**doc['provider_names']['rows'],
                                         'acme': {'ministral-14b-2512': decl(None, status='retired')}}
        doc['accounts'] = {'codex': {'billing': 'subscription', 'family': 'codex', 'windows': {'usage': {'hours': 168}}, 'bundles': {}}}
        listed = [
            'opencode/deepseek-v4-flash',
            'deepseek/deepseek-v4-flash',
            'deepseek-ai/DeepSeek-V4-Flash',
            'deepseek-ai/DeepSeek-V4-Pro',
            'deepseek-v4-pro',
            'google/gemini-3.1-flash-lite-preview',
            'openrouter/gemini-3.1-flash-lite-preview',
            'acme/ministral-14b-2512',
            'mistral/ministral-14b-2512',
            'vendor-a/novel-unmeasured',
            'vendor-b/novel-unmeasured',
            'codex/novel-gpt-9',
            'gateway/sub/novel-deep',
        ]
        derived = auto_bundles.derive(doc, {'providers': {'codex': {'ok': True, 'type': 'paseo_provider',
                                                                    'checked_at': NOW, 'models': listed}}}, NOW)['bundles']['codex']
        # Also include a manual bundle whose raw `model` spelling is the moved name `deepseek-v4-pro` (reads `-0813`).
        derived['manual_bare_pro'] = {'provider': 'codex', 'harness': 'codex', 'model': 'deepseek-v4-pro',
                                      'effort': 'high', 'draws': ['usage']}
        id_of = lambda launch: next(bid for bid, b in derived.items() if b.get('launch_model') == launch)
        items = list(derived.items())
        extra = {'acme': {'ministral-14b-2512': decl(None, status='retired')}}

        cases = [
            ({'model': 'opencode/deepseek-v4-flash'}, [id_of('opencode/deepseek-v4-flash')],
             [id_of('deepseek/deepseek-v4-flash'), id_of('deepseek-ai/DeepSeek-V4-Flash')]),
            ({'model': 'OpenCode/DeepSeek-V4-Flash'}, [id_of('opencode/deepseek-v4-flash')],
             [id_of('deepseek/deepseek-v4-flash'), id_of('deepseek-ai/DeepSeek-V4-Flash')]),
            ({'model': 'opencode/deepseek-v4-flash:high'}, [id_of('opencode/deepseek-v4-flash')],
             [id_of('deepseek/deepseek-v4-flash'), id_of('deepseek-ai/DeepSeek-V4-Flash')]),
            ({'model': 'opencode/deepseek-v4-flash-high'}, [id_of('opencode/deepseek-v4-flash')],
             [id_of('deepseek/deepseek-v4-flash'), id_of('deepseek-ai/DeepSeek-V4-Flash')]),
            ({'model': 'deepseek-ai/DeepSeek-V4-Pro'}, [id_of('deepseek-ai/DeepSeek-V4-Pro')],
             [id_of('deepseek-v4-pro'), 'manual_bare_pro']),
            ({'model': 'DeepSeek-AI/DeepSeek-V4-Pro-high'}, [id_of('deepseek-ai/DeepSeek-V4-Pro')],
             [id_of('deepseek-v4-pro'), 'manual_bare_pro']),
            ({'model': 'deepseek-v4-pro'}, [id_of('deepseek-v4-pro'), 'manual_bare_pro'],
             [id_of('deepseek-ai/DeepSeek-V4-Pro')]),
            ({'model': 'google/gemini-3.1-flash-lite-preview'}, [id_of('google/gemini-3.1-flash-lite-preview')],
             [id_of('openrouter/gemini-3.1-flash-lite-preview')]),
            ({'model': 'acme/ministral-14b-2512'}, [id_of('acme/ministral-14b-2512')],
             [id_of('mistral/ministral-14b-2512')]),
            ({'model': 'mistral/ministral-14b-2512'}, [id_of('mistral/ministral-14b-2512')],
             [id_of('acme/ministral-14b-2512')]),
            # Exact advertised spelling in the pool is never outvoted by an identity match:
            ({'model': 'vendor-a/novel-unmeasured'}, [id_of('vendor-a/novel-unmeasured')],
             [id_of('vendor-b/novel-unmeasured')]),
            ({'model': 'Vendor-A/Novel-Unmeasured:high'}, [id_of('vendor-a/novel-unmeasured')],
             [id_of('vendor-b/novel-unmeasured')]),
            # With no exact spelling in the pool, v0.7.2 reach of an unmeasured prefixed pin holds:
            ({'model': 'openai/novel-gpt-9'}, [id_of('codex/novel-gpt-9')],
             [id_of('vendor-a/novel-unmeasured'), id_of('gateway/sub/novel-deep')]),
            ({'model': 'other/sub/novel-deep'}, [id_of('gateway/sub/novel-deep')],
             [id_of('codex/novel-gpt-9'), id_of('vendor-a/novel-unmeasured')]),
        ]
        for pin_constraints, mine, others in cases:
            with self.subTest(pin=pin_constraints):
                self.assert_pin_isolates(items, pin_constraints, mine, others, extra_rows=extra)

        # Even when its own bundle is absent from the pool, neither a no-row pair nor a repository id ever falls back to
        # the same name's other row:
        only_other_row = self.subscription_fleet({'codex': ('codex', {
            'ds_flash': derived[id_of('deepseek/deepseek-v4-flash')],
            'bare_pro': derived[id_of('deepseek-v4-pro')],
        }, 'usage')})
        for absent_pin in ('opencode/deepseek-v4-flash', 'deepseek-ai/DeepSeek-V4-Pro'):
            with self.subTest(absent_pin=absent_pin):
                self.assertEqual(self.plan_pin(only_other_row, model=absent_pin)['choice'], 'none')

    def test_unscored_named_pair_dispatches_unscored_even_beside_scored_same_leaf_bundle(self):
        """candidate_ranking line 79 (R2-F7): a no-row pair pinned without --effort is not excluded by another provider's
        scored bundle of the same leaf; it proceeds as unscored_named_dispatch."""
        doc = copy.deepcopy(SHIPPED)
        doc['accounts'] = {'codex': {'billing': 'subscription', 'family': 'codex', 'windows': {'usage': {'hours': 168}}, 'bundles': {}}}
        derived = auto_bundles.derive(doc, {'providers': {'codex': {'ok': True, 'type': 'paseo_provider', 'checked_at': NOW,
                                                                    'models': ['opencode/deepseek-v4-flash', 'deepseek-ai/DeepSeek-V4-Flash']}}},
                                      NOW)['bundles']['codex']
        for ordered in (list(derived.items()), list(reversed(list(derived.items())))):
            fleet = self.subscription_fleet({'codex': ('codex', dict(ordered), 'usage')})
            planned = self.plan_pin(fleet, model='opencode/deepseek-v4-flash')
            self.assertEqual(planned['selected_bundle']['launch_model'], 'opencode/deepseek-v4-flash')
            top = next(c for c in planned['ranking'] if c['id'] == planned['choice'])
            self.assertTrue(top.get('unscored_named_dispatch'))

    def test_bare_moved_name_on_endpoint_declaring_other_weights_reaches_its_bundle(self):
        """A bare name pinned on an endpoint account (`_served`) reads as that endpoint serves it."""
        doc = copy.deepcopy(SHIPPED)
        doc['accounts'] = {'opencode': {'billing': 'subscription', 'family': 'codex', 'windows': {'balance': {'hours': 168}}, 'bundles': {}}}
        derived = auto_bundles.derive(doc, {'providers': {'opencode': {'ok': True, 'type': 'openai_compatible', 'checked_at': NOW,
                                                                       'models': [{'id': 'deepseek-v4-flash'}]}}},
                                      NOW)['bundles']['opencode']
        fleet = self.subscription_fleet({'opencode': ('codex', derived, 'balance')})
        bid = next(iter(derived))
        for pin, matches in (('deepseek-v4-flash', True), ('DeepSeek-V4-Flash:high', True),
                             ('deepseek-v4-flash-high', True), ('opencode/deepseek-v4-flash', True),
                             ('deepseek/deepseek-v4-flash', False), ('deepseek-v4.1-flash-1017', False)):
            with self.subTest(pin=pin):
                planned = self.plan_pin(fleet, model=pin)
                self.assertEqual(planned['choice'] == bid, matches, (pin, planned['choice']))

    def test_pin_by_bundle_id_never_selects_another_bundle_sharing_the_model_or_leaf(self):
        """Pinning by bundle id (`constraints.bundle` or an `auto:...` id in `constraints.model`) matches only that id."""
        fleet = self.subscription_fleet({
            'codex': ('codex', {'gpt-6-astra': {'provider': 'codex', 'harness': 'codex', 'model': 'gpt-6-astra',
                                                'effort': 'high', 'draws': ['usage']}}, 'usage'),
            'gw': ('codex', {'other_astra': {'provider': 'gw', 'harness': 'codex', 'model': 'gpt-6-astra',
                                             'effort': 'high', 'draws': ['balance']}}, 'balance'),
        })
        planned = self.plan_pin(fleet, bundle='gpt-6-astra')
        by_id = {c['id']: c for c in planned['ranking']}
        self.assertEqual(planned['choice'], 'gpt-6-astra')
        self.assertIn('explicit model constraint', by_id['other_astra']['excluded'])

        auto_id = 'auto:gw:opencode:opencode/deepseek-v4-flash'
        fleet2 = self.subscription_fleet({
            'gw': ('opencode', {auto_id: {'provider': 'gw', 'harness': 'opencode', 'model': 'opencode/deepseek-v4-flash',
                                          'draws': ['balance']}}, 'balance'),
            'codex': ('codex', {'other_flash': {'provider': 'codex', 'harness': 'codex', 'model': 'deepseek-v4.1-flash-1017',
                                                'launch_model': 'deepseek/deepseek-v4-flash', 'effort': 'high', 'draws': ['usage']}}, 'usage'),
        })
        for constraints in ({'bundle': auto_id}, {'model': auto_id}):
            with self.subTest(constraints=constraints):
                planned2 = self.plan_pin(fleet2, **constraints)
                by_id2 = {c['id']: c for c in planned2['ranking']}
                self.assertEqual(planned2['choice'], auto_id)
                self.assertIn('explicit model constraint', by_id2['other_flash']['excluded'])

    def test_qualified_pin_matches_bare_bundle_and_case_and_efforts_and_all_seven_eval_sites(self):
        """What leaf matching existed for: `openai/gpt-5.5` on bare `gpt-5.5`, case, `:effort` / `-effort`, family stays
        itself, `named_effort_bundles`, `exact_pin`, `disabled_by_default`, `metered`, and rule-B `named`."""
        bundles = {
            'vb_novel': {'provider': 'codex', 'harness': 'codex', 'model': 'vendor-b/novel-unmeasured',
                         'launch_model': 'vendor-b/novel-unmeasured', 'effort': 'high', 'efforts': ['medium', 'high'],
                         'draws': ['usage']},
            'va_novel': {'provider': 'codex', 'harness': 'codex', 'model': 'vendor-a/novel-unmeasured',
                         'launch_model': 'vendor-a/novel-unmeasured', 'effort': 'high', 'efforts': ['medium', 'high'],
                         'draws': ['usage']},
            'bare55': {'provider': 'codex', 'harness': 'codex', 'model': 'gpt-5.5', 'effort': 'high',
                       'efforts': ['medium', 'high'], 'disabled': True, 'disabled_by_default': True, 'draws': ['usage']},
            'fam25': {'provider': 'codex', 'harness': 'codex', 'model': 'gemini-2.5-pro', 'effort': 'high', 'draws': ['usage']},
            'oc_flash': {'provider': 'codex', 'harness': 'codex', 'model': 'opencode/deepseek-v4-flash',
                         'launch_model': 'opencode/deepseek-v4-flash', 'effort': 'high', 'efforts': ['medium', 'high'],
                         'disabled': True, 'disabled_by_default': True, 'draws': ['usage']},
            'ds_flash': {'provider': 'codex', 'harness': 'codex', 'model': 'deepseek-v4.1-flash-1017',
                         'launch_model': 'deepseek/deepseek-v4-flash', 'effort': 'high', 'efforts': ['medium', 'high'],
                         'disabled': True, 'disabled_by_default': True, 'draws': ['usage']},
        }
        for ordered in (list(bundles.items()), list(reversed(list(bundles.items())))):
            fleet = self.subscription_fleet({'codex': ('codex', dict(ordered), 'usage')})
            fleet['accounts']['codex']['billing'] = 'metered'
            fleet['dispatch_efforts']['codex']['gpt-5.5'] = ['medium', 'high']
            fleet['dispatch_efforts']['codex']['novel-unmeasured'] = ['medium', 'high']
            for pin, want_id, want_effort in (
                ('openai/gpt-5.5', 'bare55', 'high'),
                ('OpenAI/GPT-5.5:medium', 'bare55@medium', 'medium'),
                ('openai/gpt-5.5-medium', 'bare55@medium', 'medium'),
                ('GEMINI-2.5-PRO', 'fam25', 'high'),
                ('opencode/deepseek-v4-flash:medium', 'oc_flash@medium', 'medium'),
                ('vendor-a/novel-unmeasured:medium', 'va_novel@medium', 'medium'),
            ):
                with self.subTest(pin=pin, reversed_order=(ordered[0][0] != 'vb_novel')):
                    planned = self.plan_pin(fleet, model=pin)
                    self.assertEqual((planned['choice'], planned['selected_bundle']['effort']), (want_id, want_effort))
                    if want_id.startswith('oc_flash'):
                        self.assertFalse([c for c in planned['ranking'] if c['id'].startswith('ds_flash') and not c['excluded']])
                    if want_id.startswith('va_novel'):
                        self.assertFalse([c for c in planned['ranking'] if c['id'].startswith('vb_novel') and not c['excluded']])

    def test_named_pin_block_reports_what_the_user_named_when_only_moved_target_exists(self):
        """allocator.named_pin_block: naming `deepseek-ai/DeepSeek-V4-Pro` on a pool with only bare `deepseek-v4-pro` (`-0813`)
        blocks and names `deepseek-ai/DeepSeek-V4-Pro`, never `deepseek-v4-pro`."""
        fleet = self.subscription_fleet({'codex': ('codex', {
            'bare_pro': {'provider': 'codex', 'harness': 'codex', 'model': 'deepseek-v4-pro-0813',
                         'launch_model': 'deepseek-v4-pro', 'effort': 'high', 'draws': ['usage']},
        }, 'usage')})
        planned = self.plan_pin(fleet, model='deepseek-ai/DeepSeek-V4-Pro')
        self.assertEqual(planned['choice'], 'none')
        self.assertEqual(planned['decision']['blocked'], 'named_model_unavailable')
        self.assertIn('deepseek-ai/DeepSeek-V4-Pro: no configured bundle offers it', planned['decision']['reason'])

    def test_record_prepare_and_roll_conflict_preserve_identity_across_same_leaf_bundles(self):
        """Neighbours: route_judge.record_constraint -> dispatch_registry.issue -> prepare, and roll.named_route_conflict.
        Covers unmeasured same-leaf pairs (`vendor-b` before `vendor-a`, written and non-written efforts so both
        `route_judge.match_bundle` and `dispatch_registry.resolve_bundle` must honour exact spelling), unmeasured prefixed
        pins with no exact spelling (`openai/novel-gpt-9` -> `codex/novel-gpt-9`), and bare moved names on `_served`
        endpoints (`opencode` serving `deepseek-v4-flash`)."""
        import argparse
        import os
        import tempfile
        from unittest.mock import patch
        import dispatch_registry
        import live_models
        import roll
        import route_judge
        import usage_reading

        # Order `vendor-b/novel-unmeasured` BEFORE `vendor-a/novel-unmeasured` and give `vendor-b` a `@medium` row too,
        # so reverting either `match_bundle` or `resolve_bundle` to `row_key == row_key` hijacks `vendor-a` onto `vendor-b`.
        listed = ['vendor-b/novel-unmeasured', 'vendor-a/novel-unmeasured', 'codex/novel-gpt-9',
                  'opencode/deepseek-v4-flash', 'deepseek/deepseek-v4-flash',
                  'deepseek-ai/DeepSeek-V4-Pro', 'deepseek-v4-pro']
        doc = copy.deepcopy(SHIPPED)
        doc['accounts'] = {
            'codex': {'billing': 'subscription', 'plan': 'pro', 'verified': True, 'family': 'codex',
                      'windows': {'usage': {'hours': 168}}, 'capacity_usd': {'usage': 100.0},
                      'capacity_source': {'usage': {'evidence': 'test'}}, 'bundles': {}},
            'opencode': {'billing': 'subscription', 'plan': 'pro', 'verified': True, 'family': 'codex',
                         'windows': {'balance': {'hours': 168}}, 'capacity_usd': {'balance': 100.0},
                         'capacity_source': {'balance': {'evidence': 'test'}}, 'bundles': {}},
        }
        derived = auto_bundles.derive(doc, {'providers': {
            'codex': {'ok': True, 'type': 'paseo_provider', 'checked_at': NOW, 'models': listed},
            'opencode': {'ok': True, 'type': 'openai_compatible', 'checked_at': NOW, 'models': [{'id': 'deepseek-v4-flash'}]},
        }}, NOW)
        fleet = auto_bundles.merge_into(doc, derived)
        fleet['accounts']['codex']['bundles']['vb_novel_med'] = {
            'provider': 'codex', 'harness': 'codex', 'model': 'vendor-b/novel-unmeasured',
            'launch_model': 'vendor-b/novel-unmeasured', 'effort': 'medium', 'draws': ['usage'],
        }
        fleet['dispatch_efforts']['codex']['novel-unmeasured'] = ['medium', 'high']
        fetch = lambda providers, **kw: {'ok': True, 'providers': {
            'codex': {'ok': True, 'models': [{'id': m} for m in listed]},
            'opencode': {'ok': True, 'models': [{'id': 'deepseek-v4-flash'}]},
        }}

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory, \
                patch.dict(os.environ, {'ROUTER_DISPATCH_DB': os.path.join(directory, 'db.sqlite3'),
                                        'ROUTER_LEDGER': os.path.join(directory, 'ledger.jsonl'),
                                        'ROUTER_LIVE_MODELS': os.path.join(directory, 'live-models.json')}), \
                patch.object(account_fleet, 'load_accounts', return_value=copy.deepcopy(fleet)), \
                patch.object(usage_reading, 'usage_snapshot', return_value=copy.deepcopy(self.usage)), \
                patch.object(live_models, 'fetch_from_paseo', side_effect=fetch):
            for acct, prov, pin, eff, want_provider in (
                ('codex', 'codex', 'opencode/deepseek-v4-flash', 'high', 'codex/opencode/deepseek-v4-flash'),
                ('codex', 'codex', 'deepseek-ai/DeepSeek-V4-Pro', 'high', 'codex/deepseek-ai/DeepSeek-V4-Pro'),
                ('codex', 'codex', 'vendor-a/novel-unmeasured', 'high', 'codex/vendor-a/novel-unmeasured'),
                ('codex', 'codex', 'vendor-a/novel-unmeasured', 'medium', 'codex/vendor-a/novel-unmeasured'),
                ('codex', 'codex', 'openai/novel-gpt-9', 'high', 'codex/codex/novel-gpt-9'),
                ('opencode', 'opencode', 'deepseek-v4-flash', 'high', 'opencode/deepseek-v4-flash'),
            ):
                with self.subTest(acct=acct, pin=pin, eff=eff):
                    task = {'objective': 'x', 'profile': self.prof, 'expected_hours': 0.5,
                            'parent_agent_id': 'parent-1', 'task_id': f't-{acct}-{pin}-{eff}'}
                    args = argparse.Namespace(basis='user named it', account=acct, provider=prov, model=pin,
                                              effort=eff, input=None, usage_cache=None, accounts=None,
                                              constraint='user_override', specialty=None, reason_category=None,
                                              fast=False, parent_id='parent-1')
                    res = route_judge.record_constraint(args, task)
                    if acct == 'opencode':
                        self.assertEqual(res['selected_bundle'].get('bundle_id'), next(iter(derived['bundles']['opencode'])))
                    prep = dispatch_registry.prepare(res['decision_id'], task, {'title': 'x', 'initialPrompt': 'go'},
                                                     copy.deepcopy(self.usage), accounts=copy.deepcopy(fleet), now=self.at)
                    self.assertEqual(prep['request']['provider'], want_provider)

        # roll.named_route_conflict on an endpoint worker and on a launcher worker:
        ep_bundle = {'provider': 'opencode', 'account': 'opencode', 'model': 'opencode/deepseek-v4-flash',
                     'launch_model': 'deepseek-v4-flash', 'effort': 'high', '_served': ['opencode']}
        novel_bundle = {'provider': 'codex', 'account': 'codex', 'model': 'codex/novel-gpt-9',
                        'launch_model': 'codex/novel-gpt-9', 'effort': 'high'}
        vb_bundle = {'provider': 'codex', 'account': 'codex', 'model': 'vendor-b/novel-unmeasured',
                     'launch_model': 'vendor-b/novel-unmeasured', 'effort': 'high'}
        ns = lambda m, e=None, p='opencode', a='opencode': argparse.Namespace(provider=p, account=a, model=m, effort=e, fast=False)
        self.assertIsNone(roll.named_route_conflict(ns('deepseek-v4-flash'), ep_bundle, fleet))
        self.assertIsNone(roll.named_route_conflict(ns('opencode/deepseek-v4-flash:high'), ep_bundle, fleet))
        self.assertIsNone(roll.named_route_conflict(ns('openai/novel-gpt-9', p='codex', a='codex'), novel_bundle, fleet))
        self.assertIn('--model', roll.named_route_conflict(ns('vendor-a/novel-unmeasured', p='codex', a='codex'), vb_bundle, fleet) or '')
        self.assertIn('--model', roll.named_route_conflict(ns('vendor-a/novel-unmeasured', p=None, a=None), vb_bundle, fleet) or '')
        self.assertIn('--model', roll.named_route_conflict(ns('deepseek/deepseek-v4-flash'), ep_bundle, fleet) or '')
        self.assertIn('--model', roll.named_route_conflict(ns('deepseek-v4-flash-medium'), ep_bundle, fleet) or '')

    def test_pin_pool_includes_existing_bundles_before_preference_availability_modality_or_effort(self):
        """Rule (R2-1 & M12a: account_fleet.pin_pool): which bundle a name names is decided over every bundle existing on
        the addressed accounts before any filter of preference (`host_policy`, `disabled`), availability (`unavailable`,
        `discovery_unavailable`), modality (`file_transfer`) or effort; those filters apply to the named bundles only and
        never fall back onto another provider's same-leaf bundle."""
        import argparse
        import os
        import tempfile
        from unittest.mock import patch
        import allocator
        import dispatch_registry
        import host_policy
        import live_models
        import roll
        import route_judge
        import usage_reading
        from router_test_support import usage_windows

        usage_two = usage_windows(self.at, {'codex': [('usage', 90, 100)], 'other': [('usage', 90, 100)]})
        live_ok = lambda providers, **kw: {'ok': True, 'providers': {
            p: {'ok': True, 'models': [{'id': 'vendor-a/novel-unmeasured'}, {'id': 'vendor-b/novel-unmeasured'},
                                       {'id': 'openai/novel-gpt-9'}, {'id': 'codex/novel-gpt-9'}]}
            for p in providers
        }}

        # 1. A2 & A5: exact spelling `disabled` (unchecked) or `disabled_by_default` on `codex` beside enabled `vendor-b`.
        for reversed_order in (False, True):
            pairs = [
                ('b_vb', {'provider': 'codex', 'harness': 'codex', 'model': 'vendor-b/novel-unmeasured',
                          'launch_model': 'vendor-b/novel-unmeasured', 'effort': 'high', 'disabled': False, 'draws': ['usage']}),
                ('b_va', {'provider': 'codex', 'harness': 'codex', 'model': 'vendor-a/novel-unmeasured',
                          'launch_model': 'vendor-a/novel-unmeasured', 'effort': 'high', 'disabled': True, 'draws': ['usage']}),
            ]
            if reversed_order:
                pairs.reverse()
            unchecked_fleet = self.subscription_fleet({'codex': ('codex', dict(pairs), 'usage')})
            p_unchecked = self.plan_pin(unchecked_fleet, model='vendor-a/novel-unmeasured')
            self.assertEqual(p_unchecked['choice'], 'none')
            self.assertIn('unchecked in model management', p_unchecked['decision']['reason'])

            default_off_fleet = copy.deepcopy(unchecked_fleet)
            default_off_fleet['accounts']['codex']['bundles']['b_va']['disabled_by_default'] = True
            p_default_off = self.plan_pin(default_off_fleet, model='vendor-a/novel-unmeasured')
            self.assertEqual(p_default_off['choice'], 'b_va')

            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td, \
                    patch.dict(os.environ, {'ROUTER_DISPATCH_DB': os.path.join(td, 'db.sqlite3'),
                                            'ROUTER_LEDGER': os.path.join(td, 'ledger.jsonl'),
                                            'ROUTER_LIVE_MODELS': os.path.join(td, 'live-models.json')}), \
                    patch.object(usage_reading, 'usage_snapshot', return_value=copy.deepcopy(usage_two)), \
                    patch.object(live_models, 'fetch_from_paseo', side_effect=live_ok):
                task = {'objective': 'x', 'profile': self.prof, 'expected_hours': 0.5,
                        'parent_agent_id': 'parent-1', 'task_id': f't-a2-{reversed_order}'}
                mk_args = lambda constraint: argparse.Namespace(
                    basis='user asked', account='codex', provider='codex', model='vendor-a/novel-unmeasured',
                    effort='high', input=None, usage_cache=None, accounts=None, constraint=constraint,
                    specialty=None, reason_category=None, fast=False, parent_id='parent-1')
                with patch.object(account_fleet, 'load_accounts', return_value=copy.deepcopy(unchecked_fleet)):
                    with self.assertRaisesRegex(ValueError, 'unchecked in model management'):
                        route_judge.record_constraint(mk_args('hard_constraint'), task)
                    rec = route_judge.record_constraint(mk_args('user_override'), task)
                    self.assertEqual(rec['selected_bundle']['bundle_id'], 'b_va')
                    prep = dispatch_registry.prepare(rec['decision_id'], task, {'title': 'x', 'initialPrompt': 'go'},
                                                     copy.deepcopy(usage_two), accounts=copy.deepcopy(unchecked_fleet), now=self.at)
                    self.assertEqual(prep['request']['provider'], 'codex/vendor-a/novel-unmeasured')
                with patch.object(account_fleet, 'load_accounts', return_value=copy.deepcopy(default_off_fleet)):
                    rec_def = route_judge.record_constraint(mk_args('hard_constraint'), dict(task, task_id=f't-a5-{reversed_order}'))
                    self.assertEqual(rec_def['selected_bundle']['bundle_id'], 'b_va')

        # 2. A3 (`unavailable` and `discovery_unavailable`) & M12a (missing effort on fixed-effort exact bundle):
        enabled_fleet = self.subscription_fleet({'codex': ('codex', {
            'b_vb': {'provider': 'codex', 'harness': 'codex', 'model': 'vendor-b/novel-unmeasured',
                     'launch_model': 'vendor-b/novel-unmeasured', 'effort': 'medium', 'draws': ['usage']},
            'b_va': {'provider': 'codex', 'harness': 'codex', 'model': 'vendor-a/novel-unmeasured',
                     'launch_model': 'vendor-a/novel-unmeasured:high', 'effort': 'high', 'effort_fixed': True, 'draws': ['usage']},
        }, 'usage')})
        p_unavail = allocator.plan(
            {'objective': 'x', 'profile': self.prof, 'expected_hours': 0.5, 'constraints': {'model': 'vendor-a/novel-unmeasured'}},
            usage_two, self.at, unavailable=('b_va',), accounts=copy.deepcopy(enabled_fleet), overrides=[], read_local_state=False,
        )
        self.assertEqual(p_unavail['choice'], 'none')
        self.assertIn('marked unavailable by the parent', p_unavail['decision']['reason'])

        # M12a: `b_va` is fixed at `high`, `b_vb` offers `medium`; pinning `vendor-a/novel-unmeasured:medium` blocks rather than selecting `b_vb`.
        p_missing_eff = self.plan_pin(enabled_fleet, model='vendor-a/novel-unmeasured:medium')
        self.assertEqual(p_missing_eff['choice'], 'none')
        self.assertIn('explicit model constraint', next(c['excluded'] for c in p_missing_eff['ranking'] if c['id'] == 'b_vb'))

        disc_unavail_fleet = copy.deepcopy(enabled_fleet)
        disc_unavail_fleet['accounts']['codex']['bundles']['b_va']['discovery_unavailable'] = 'not detected for 7+ days'
        self.assertEqual(self.plan_pin(disc_unavail_fleet, model='vendor-a/novel-unmeasured')['choice'], 'none')

        # 3. A4 (modality / file_transfer gate) & A1 (`host_policy` excluding exact-spelling account):
        cross_fleet = self.subscription_fleet({
            'other': ('grok-bot', {
                'b_other': {'provider': 'other', 'harness': 'grok-bot', 'model': 'openai/novel-gpt-9',
                            'launch_model': 'openai/novel-gpt-9', 'effort': 'high', 'draws': ['usage']},
            }, 'usage'),
            'codex': ('codex', {
                'b_codex': {'provider': 'codex', 'harness': 'codex', 'model': 'codex/novel-gpt-9',
                            'launch_model': 'codex/novel-gpt-9', 'effort': 'high', 'draws': ['usage']},
            }, 'usage'),
        })
        p_a4 = allocator.plan(
            {'objective': 'x', 'profile': self.prof, 'expected_hours': 0.5, 'needs': {'file_transfer': True},
             'constraints': {'model': 'openai/novel-gpt-9'}},
            usage_two, self.at, accounts=copy.deepcopy(cross_fleet), overrides=[], read_local_state=False,
        )
        self.assertEqual(p_a4['choice'], 'none')
        self.assertIn('cannot transfer or apply local files', p_a4['decision']['reason'])

        only_codex_policy = {'allowed_accounts': ['codex'], 'known_accounts': ['codex', 'other']}
        p_a1 = allocator.plan(
            {'objective': 'x', 'profile': self.prof, 'expected_hours': 0.5, 'constraints': {'model': 'openai/novel-gpt-9'}},
            usage_two, self.at, accounts=copy.deepcopy(cross_fleet), overrides=[], host_policy_config=only_codex_policy,
            read_local_state=False,
        )
        self.assertEqual(p_a1['choice'], 'none')
        self.assertIn('excluded by host policy', p_a1['decision']['reason'])

        # Un-accounted `roll --resume` on `b_codex` sees `b_other` in the full pool and flags `--model openai/novel-gpt-9`,
        # while an accounted resume on `codex` addresses `codex` only, and a bare `--model novel-gpt-9` names both:
        b_codex = dict(cross_fleet['accounts']['codex']['bundles']['b_codex'], account='codex', id='b_codex', bundle_id='b_codex')
        self.assertIn('--model', roll.named_route_conflict(
            argparse.Namespace(provider=None, account=None, model='openai/novel-gpt-9', effort=None, fast=False), b_codex, cross_fleet) or '')
        self.assertIsNone(roll.named_route_conflict(
            argparse.Namespace(provider='codex', account='codex', model='openai/novel-gpt-9', effort=None, fast=False), b_codex, cross_fleet))
        self.assertIsNone(roll.named_route_conflict(
            argparse.Namespace(provider=None, account=None, model='novel-gpt-9', effort=None, fast=False), b_codex, cross_fleet))

    def test_bare_pin_reaches_every_account_while_prefixed_pin_obeys_prefix_and_clause_three(self):
        """Rule (round 4: account_fleet.pin_matching_bundles across plan, record_constraint, prepare, roll.named_route_conflict
        and quota_overrides):
        1. Bare name: names every bundle of its row across accounts, even when one advertises the bare name verbatim and
           cannot run (unchecked, outside host policy, exhausted, held, unavailable, discovery_unavailable); when both can
           run, score decides; when none can run, named_model_unavailable lists every named bundle with its reason.
        2. Prefixed name `P/name`: obeys `P` (exact advertised spelling, or `P` as account/provider/bundle_providers), never
           substituting a bundle `P` does not name when `P`'s cannot run or when `P` is a pool account without that model.
        3. Prefix naming nothing in the pool (`_unrecognized_prefix_matches`): read as part of the model's name by identity."""
        import argparse
        import os
        import tempfile
        from unittest.mock import patch
        import allocator
        import dispatch_registry
        import live_models
        import provider_retry
        import roll
        import route_judge
        import usage_reading
        from router_test_support import usage_windows

        usage_both = usage_windows(self.at, {'acct_a': [('usage', 90, 100)], 'codex': [('usage', 90, 100)]})
        usage_a_exhausted = usage_windows(self.at, {'acct_a': [('usage', 0, 100)], 'codex': [('usage', 90, 100)]})
        usage_a_expensive = usage_windows(self.at, {'acct_a': [('usage', 10, 100)], 'codex': [('usage', 95, 100)]})
        usage_both_exhausted = usage_windows(self.at, {'acct_a': [('usage', 0, 100)], 'codex': [('usage', 0, 100)]})

        for reversed_order in (False, True):
            def two_acct_fleet(a_bundle, b_bundle):
                items = [('acct_a', ('codex', {'b_a': a_bundle}, 'usage')),
                         ('codex', ('codex', {'b_b': b_bundle}, 'usage'))]
                if reversed_order:
                    items.reverse()
                return self.subscription_fleet(dict(items))

            # --- Clause 1: bare `gpt-5.5` when A lists bare `gpt-5.5` verbatim and cannot run, B lists `openai/gpt-5.5` ---
            base_a = {'provider': 'acct_a', 'harness': 'codex', 'model': 'gpt-5.5',
                      'launch_model': 'gpt-5.5', 'effort': 'high', 'draws': ['usage']}
            base_b = {'provider': 'codex', 'harness': 'codex', 'model': 'gpt-5.5',
                      'launch_model': 'openai/gpt-5.5', 'effort': 'high', 'draws': ['usage']}
            six_states = (
                ('unchecked', dict(base_a, disabled=True), usage_both, {}, (), {}),
                ('host_policy', base_a, usage_both, {'allowed_accounts': ['codex'], 'known_accounts': ['acct_a', 'codex']}, (), {}),
                ('exhausted', base_a, usage_a_exhausted, {}, (), {}),
                ('held', base_a, usage_both, {}, (), {'acct_a': {'account': 'acct_a', 'retry_at': '2026-11-20T00:00:00+00:00',
                                                                 'attempts': 1, 'reason': '429 hold'}}),
                ('unavailable', base_a, usage_both, {}, ('b_a',), {}),
                ('discovery_unavailable', dict(base_a, discovery_unavailable='not detected for 7+ days'), usage_both, {}, (), {}),
            )
            for state_name, a_row, u_doc, hpol, unavail, holds in six_states:
                fleet = two_acct_fleet(a_row, base_b)
                for bare_pin in ('gpt-5.5', 'GPT-5.5', 'gpt-5.5:high'):
                    with self.subTest(clause=1, state=state_name, pin=bare_pin, rev=reversed_order), \
                            patch.object(provider_retry, 'active', return_value=holds):
                        p = allocator.plan(
                            {'objective': 'x', 'profile': self.prof, 'expected_hours': 0.5, 'constraints': {'model': bare_pin}},
                            copy.deepcopy(u_doc), self.at, unavailable=unavail, accounts=copy.deepcopy(fleet),
                            overrides=[], host_policy_config=hpol or None, read_local_state=bool(holds),
                        )
                        self.assertEqual(p['choice'], 'b_b')

            # Both can run: economy in the score decides (`codex` under recovery line beats `acct_a` over recovery line,
            # even though `acct_a` advertises the bare `gpt-5.5` verbatim):
            both_fleet = two_acct_fleet(base_a, base_b)
            p_score = allocator.plan(
                {'objective': 'x', 'profile': self.prof, 'expected_hours': 2.0, 'constraints': {'model': 'gpt-5.5'}},
                copy.deepcopy(usage_a_expensive), self.at, accounts=copy.deepcopy(both_fleet), overrides=[], read_local_state=False,
            )
            self.assertEqual(p_score['choice'], 'b_b')

            # Neither can run (`b_a` marked unavailable by parent, `b_b` unchecked): `named_model_unavailable` lists EVERY named bundle:
            none_fleet = two_acct_fleet(base_a, dict(base_b, disabled=True))
            p_none = allocator.plan(
                {'objective': 'x', 'profile': self.prof, 'expected_hours': 0.5, 'constraints': {'model': 'gpt-5.5'}},
                copy.deepcopy(usage_both), self.at, unavailable=('b_a',), accounts=copy.deepcopy(none_fleet),
                overrides=[], read_local_state=False,
            )
            self.assertEqual((p_none['choice'], p_none['decision']['blocked']), ('none', 'named_model_unavailable'))
            self.assertIn('b_a: marked unavailable by the parent', p_none['decision']['reason'])
            self.assertIn('b_b: unchecked in model management', p_none['decision']['reason'])

            # --- Clause 2: prefixed `openai/gpt-5.5` when A lists `openai/gpt-5.5` verbatim and Codex lists bare `gpt-5.5` ---
            pref_a = dict(base_a, launch_model='openai/gpt-5.5')
            pref_b = dict(base_b, launch_model='gpt-5.5')
            for state_name, a_mod, u_doc, hpol, unavail in (
                ('unchecked', dict(pref_a, disabled=True), usage_both, None, ()),
                ('host_policy', pref_a, usage_both, {'allowed_accounts': ['codex'], 'known_accounts': ['acct_a', 'codex']}, ()),
                ('exhausted', pref_a, usage_a_exhausted, None, ()),
                ('unavailable', pref_a, usage_both, None, ('b_a',)),
                ('discovery_unavailable', dict(pref_a, discovery_unavailable='not detected for 7+ days'), usage_both, None, ()),
            ):
                p_pref = allocator.plan(
                    {'objective': 'x', 'profile': self.prof, 'expected_hours': 0.5, 'constraints': {'model': 'openai/gpt-5.5'}},
                    copy.deepcopy(u_doc), self.at, unavailable=unavail,
                    accounts=two_acct_fleet(a_mod, pref_b), overrides=[], host_policy_config=hpol, read_local_state=False,
                )
                self.assertEqual((p_pref['choice'], p_pref['decision']['blocked']), ('none', 'named_model_unavailable'), state_name)
                self.assertIn('b_a:', p_pref['decision']['reason'])
                self.assertNotIn('b_b:', p_pref['decision']['reason'])

            # --- Clause 2 by account/provider/`bundle_providers`: `codex/gpt-5.5` when `codex` advertises bare `gpt-5.5` ---
            bare_both = two_acct_fleet(pref_b | {'provider': 'acct_a'}, pref_b)
            p_codex_ok = self.plan_pin(bare_both, model='codex/gpt-5.5', usage=usage_both)
            self.assertEqual(p_codex_ok['choice'], 'b_b')
            p_acct_a_down = allocator.plan(
                {'objective': 'x', 'profile': self.prof, 'expected_hours': 0.5, 'constraints': {'model': 'acct_a/gpt-5.5'}},
                copy.deepcopy(usage_a_exhausted), self.at, accounts=copy.deepcopy(bare_both), overrides=[], read_local_state=False,
            )
            self.assertEqual(p_acct_a_down['choice'], 'none')

            # Clause 3 MUST NOT fire when `P` is an account/provider/serving-provider in the pool without that model:
            acct_a_only_astra = two_acct_fleet(
                {'provider': 'acct_a', 'harness': 'codex', 'model': 'gpt-6-astra', 'launch_model': 'gpt-6-astra',
                 'effort': 'high', 'draws': ['usage']},
                pref_b,
            )
            p_no_gpt55_on_a = self.plan_pin(acct_a_only_astra, model='acct_a/gpt-5.5', usage=usage_both)
            self.assertEqual(p_no_gpt55_on_a['choice'], 'none')
            self.assertIn('no configured bundle offers it', p_no_gpt55_on_a['decision']['reason'])

            # --- Clause 3: `openai/gpt-5.5` when nobody lists `openai/gpt-5.5` and no account/provider is `openai` ---
            p_clause3 = self.plan_pin(acct_a_only_astra, model='openai/gpt-5.5', usage=usage_both)
            self.assertEqual(p_clause3['choice'], 'b_b')

            # Bare `gpt-5.5:medium` when A advertises bare `gpt-5.5:high` (fixed) and B advertises `openai/gpt-5.5` (carrier):
            carrier_fleet = two_acct_fleet(
                dict(base_a, launch_model='gpt-5.5:high', effort='high', effort_fixed=True),
                dict(base_b, launch_model='openai/gpt-5.5', effort='high'),
            )
            carrier_fleet['dispatch_efforts']['codex']['gpt-5.5'] = ['medium', 'high']
            self.assertEqual(self.plan_pin(carrier_fleet, model='gpt-5.5:medium', usage=usage_both)['choice'], 'b_b@medium')

            # --- Unmeasured bare vs prefixed & missing effort (`:medium`) & quota overrides ---
            unm_pairs = [
                ('b_vb', {'provider': 'codex', 'harness': 'codex', 'model': 'vendor-b/novel-unmeasured',
                          'launch_model': 'vendor-b/novel-unmeasured', 'effort': 'high', 'draws': ['usage']}),
                ('b_va', {'provider': 'codex', 'harness': 'codex', 'model': 'vendor-a/novel-unmeasured',
                          'launch_model': 'vendor-a/novel-unmeasured:high', 'effort': 'high', 'effort_fixed': True, 'draws': ['usage']}),
            ]
            if reversed_order:
                unm_pairs.reverse()
            unm_fleet = self.subscription_fleet({'codex': ('codex', dict(unm_pairs), 'usage')})
            unm_fleet['dispatch_efforts']['codex']['novel-unmeasured'] = ['medium', 'high']
            # Bare `:medium` carries on `b_vb` (`b_vb@medium`); prefixed `vendor-a/...:medium` stays blocked:
            self.assertEqual(self.plan_pin(unm_fleet, model='novel-unmeasured:medium')['choice'], 'b_vb@medium')
            self.assertEqual(self.plan_pin(unm_fleet, model='vendor-a/novel-unmeasured:medium')['choice'], 'none')
            # Bare `novel-unmeasured` with `b_va` unavailable launches `b_vb`:
            p_bare_unm = allocator.plan(
                {'objective': 'x', 'profile': self.prof, 'expected_hours': 0.5, 'constraints': {'model': 'novel-unmeasured'}},
                copy.deepcopy(usage_both), self.at, unavailable=('b_va',), accounts=copy.deepcopy(unm_fleet),
                overrides=[], read_local_state=False,
            )
            self.assertEqual(p_bare_unm['choice'], 'b_vb')
            # Prefixed override boosts only `b_va`, whereas bare override boosts both:
            p_pref_ov = allocator.plan(
                {'objective': 'x', 'profile': self.prof, 'expected_hours': 0.5, 'constraints': {'model': 'novel-unmeasured'}},
                copy.deepcopy(usage_both), self.at, accounts=copy.deepcopy(unm_fleet),
                overrides=[{'id': 'o1', 'account': 'codex', 'model': 'vendor-a/novel-unmeasured', 'boost': 0.25,
                            'until': '2026-11-21T00:00:00+00:00'}],
                read_local_state=False,
            )
            self.assertEqual(p_pref_ov['choice'], 'b_va')
            by_id = {c['id']: c['components'].get('boost', 0) for c in p_pref_ov['ranking']}
            self.assertEqual((by_id['b_va'], by_id['b_vb']), (0.25, 0))
            p_bare_ov = allocator.plan(
                {'objective': 'x', 'profile': self.prof, 'expected_hours': 0.5, 'constraints': {'model': 'novel-unmeasured'}},
                copy.deepcopy(usage_both), self.at, accounts=copy.deepcopy(unm_fleet),
                overrides=[{'id': 'o2', 'account': 'codex', 'model': 'novel-unmeasured', 'boost': 0.25,
                            'until': '2026-11-21T00:00:00+00:00'}],
                read_local_state=False,
            )
            by_id_bare = {c['id']: c['components'].get('boost', 0) for c in p_bare_ov['ranking']}
            self.assertEqual((by_id_bare['b_va'], by_id_bare['b_vb']), (0.25, 0.25))

            # Reset expectations (`window_reset_expectation` via `allocator.plan`): a prefixed target's reset expectation
            # reads only the bundles its prefix names, while a bare target's reads every bundle of the row (M26):
            reset_soon = (self.at + timedelta(hours=2)).isoformat()
            until_later = (self.at + timedelta(hours=24)).isoformat()
            cal_pairs = [
                ('b_vb', {'provider': 'codex', 'harness': 'codex', 'model': 'gpt-5.5',
                          'launch_model': 'vendor-b/gpt-5.5', 'effort': 'high', 'draws': ['usage']}),
                ('b_va', {'provider': 'codex', 'harness': 'codex', 'model': 'gpt-5.5',
                          'launch_model': 'vendor-a/gpt-5.5', 'effort': 'high', 'draws': ['usage']}),
            ]
            if reversed_order:
                cal_pairs.reverse()
            cal_fleet = self.subscription_fleet({'codex': ('codex', dict(cal_pairs), 'usage')})
            p_pref_reset = allocator.plan(
                {'objective': 'x', 'profile': self.prof, 'expected_hours': 0.5, 'constraints': {'model': 'gpt-5.5'}},
                copy.deepcopy(usage_both), self.at, accounts=copy.deepcopy(cal_fleet),
                overrides=[{'id': 'or1', 'account': 'codex', 'model': 'vendor-a/gpt-5.5',
                            'expected_reset_at': reset_soon, 'until': until_later}],
                read_local_state=False,
            )
            reset_by_id = {c['id']: c['components'].get('earliest_reset_hours') for c in p_pref_reset['ranking']}
            self.assertEqual(reset_by_id['b_va'], 2.0)
            self.assertGreater(reset_by_id['b_vb'], 24.0)
            p_bare_reset = allocator.plan(
                {'objective': 'x', 'profile': self.prof, 'expected_hours': 0.5, 'constraints': {'model': 'gpt-5.5'}},
                copy.deepcopy(usage_both), self.at, accounts=copy.deepcopy(cal_fleet),
                overrides=[{'id': 'or2', 'account': 'codex', 'model': 'gpt-5.5',
                            'expected_reset_at': reset_soon, 'until': until_later}],
                read_local_state=False,
            )
            reset_bare_by_id = {c['id']: c['components'].get('earliest_reset_hours') for c in p_bare_reset['ranking']}
            self.assertEqual((reset_bare_by_id['b_va'], reset_bare_by_id['b_vb']), (2.0, 2.0))
            unm_pool = account_fleet.pin_pool(unm_fleet, account='codex')
            unm_by_id = {b['id']: b for b in unm_pool}
            for bid in ('b_va', 'b_vb'):
                self.assertEqual(
                    quota_overrides.window_reset_expectation(
                        [{'id': 'ou1', 'account': 'codex', 'model': 'novel-unmeasured',
                          'expected_reset_at': reset_soon, 'until': until_later}],
                        'codex', unm_by_id[bid]['model'], 'usage', unm_fleet['accounts']['codex'], unm_fleet, self.at,
                        bundle=unm_by_id[bid], pool=unm_pool,
                    ),
                    self.at + timedelta(hours=2),
                )

            # Same-account `record_constraint` + `prepare`: bare `gpt-5.5` skips unchecked `openai/gpt-5.5` to enabled `gpt-5.5`,
            # while prefixed `openai/gpt-5.5` refuses on `hard_constraint`:
            same_acct_pairs = [
                ('b_pref', {'provider': 'codex', 'harness': 'codex', 'model': 'gpt-5.5',
                            'launch_model': 'openai/gpt-5.5', 'effort': 'high', 'disabled': True, 'draws': ['usage']}),
                ('b_bare', {'provider': 'codex', 'harness': 'codex', 'model': 'gpt-5.5',
                            'launch_model': 'gpt-5.5', 'effort': 'high', 'disabled': False, 'draws': ['usage']}),
            ]
            if reversed_order:
                same_acct_pairs.reverse()
            same_acct_fleet = self.subscription_fleet({'codex': ('codex', dict(same_acct_pairs), 'usage')})
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td, \
                    patch.dict(os.environ, {'ROUTER_DISPATCH_DB': os.path.join(td, 'db.sqlite3'),
                                            'ROUTER_LEDGER': os.path.join(td, 'ledger.jsonl'),
                                            'ROUTER_LIVE_MODELS': os.path.join(td, 'live-models.json')}), \
                    patch.object(account_fleet, 'load_accounts', return_value=copy.deepcopy(same_acct_fleet)), \
                    patch.object(usage_reading, 'usage_snapshot', return_value=copy.deepcopy(usage_both)), \
                    patch.object(live_models, 'fetch_from_paseo', return_value={'ok': True, 'providers': {
                        'codex': {'ok': True, 'models': [{'id': 'openai/gpt-5.5'}, {'id': 'gpt-5.5'}]}}
                    }):
                mk = lambda m, c: argparse.Namespace(
                    basis='pin test', account='codex', provider='codex', model=m, effort='high',
                    input=None, usage_cache=None, accounts=None, constraint=c, specialty=None,
                    reason_category=None, fast=False, parent_id='p1')
                t_bare = {'objective': 'x', 'profile': self.prof, 'expected_hours': 0.5, 'parent_agent_id': 'p1', 'task_id': f'tb-{reversed_order}'}
                r_bare = route_judge.record_constraint(mk('gpt-5.5', 'hard_constraint'), t_bare)
                self.assertEqual(r_bare['selected_bundle']['bundle_id'], 'b_bare')
                pr_bare = dispatch_registry.prepare(r_bare['decision_id'], t_bare, {'title': 'x', 'initialPrompt': 'go'},
                                                    copy.deepcopy(usage_both), accounts=copy.deepcopy(same_acct_fleet), now=self.at)
                self.assertEqual(pr_bare['request']['provider'], 'codex/gpt-5.5')
                with self.assertRaisesRegex(ValueError, 'unchecked in model management'):
                    route_judge.record_constraint(mk('openai/gpt-5.5', 'hard_constraint'), dict(t_bare, task_id=f'tp-{reversed_order}'))

        # Malformed model pins (leading `/`, trailing `/`, empty `/` or `:` segment, incomplete `auto:` id) are refused at
        # every caller write/dispatch boundary, while `model_pin_problem` never raises on any string (0..4 `:` x 0..4 `/`,
        # with and without `auto:`), and a stored `router-overrides.json` entry with a malformed model is kept inert until
        # its own `until` expiry without asking to delete the file:
        import quota_overrides
        import state_upgrade
        import task_intake
        self.assertFalse(account_fleet._same_provider('codex', ''))
        self.assertFalse(account_fleet._same_provider('codex', None))
        odd_strings = {'auto:acct:harness'}
        for n_colons in range(5):
            for n_slashes in range(5):
                for seps in (
                    (':' * n_colons) + ('/' * n_slashes),
                    ('/' * n_slashes) + (':' * n_colons),
                    ''.join(':/'[k % 2] for k in range(n_colons + n_slashes)
                            if (k % 2 == 0 and k // 2 < n_colons) or (k % 2 == 1 and k // 2 < n_slashes))
                    + ':' * max(0, n_colons - (n_colons + n_slashes + 1) // 2)
                    + '/' * max(0, n_slashes - (n_colons + n_slashes) // 2),
                ):
                    slots = len(seps) + 1
                    for fill in (
                        [''] * slots,
                        ['seg'] * slots,
                        [''] + ['seg'] * (slots - 1),
                        ['seg'] * (slots - 1) + [''],
                        ['seg'] * (slots - 1) + ['high'],
                        [''] + ['seg'] * max(0, slots - 2) + (['high'] if slots >= 2 else []),
                        ['seg' if idx != slots // 2 else '' for idx in range(slots)],
                    ):
                        built = ''.join(fill[idx] + (seps[idx] if idx < len(seps) else '') for idx in range(slots))
                        odd_strings.add(built)
                        odd_strings.add('auto:' + built)
        for candidate in sorted(odd_strings):
            verdict = model_resolution.model_pin_problem(candidate, 'model')
            self.assertTrue(verdict is None or (isinstance(verdict, str) and verdict.startswith('invalid_model_pin:')),
                            (candidate, verdict))
        self.assertTrue((model_resolution.model_pin_problem('auto:acct:harness') or '').startswith('invalid_model_pin:'))

        malformed_fleet = self.subscription_fleet({'codex': ('codex', {
            'b_cx': {'provider': 'codex', 'harness': 'codex', 'model': 'gpt-5.5', 'launch_model': 'gpt-5.5',
                     'effort': 'high', 'draws': ['usage']},
        }, 'usage')})
        for bad_pin in ('/gpt-5.5', '/gpt-5.5:high', '/', 'openai/', 'openai//gpt-5.5', ':high', 'gpt-5.5:', '   ',
                        'auto:acct:harness', 'auto:acct:harness:', 'auto::harness:gpt-5.5'):
            with self.subTest(bad_pin=bad_pin):
                with self.assertRaisesRegex(ValueError, 'invalid_model_pin'):
                    task_intake.named_pin({'model': bad_pin}, {}, SHIPPED)
                with self.assertRaisesRegex(ValueError, 'invalid_model_pin'):
                    task_intake.named_pin({}, {'pinned_model': bad_pin}, SHIPPED)
                with self.assertRaisesRegex(ValueError, 'invalid_model_pin'):
                    allocator.plan(
                        {'objective': 'x', 'profile': self.prof, 'expected_hours': 0.5, 'constraints': {'model': bad_pin}},
                        copy.deepcopy(usage_both), self.at, accounts=copy.deepcopy(malformed_fleet),
                        overrides=[], read_local_state=False,
                    )
                with self.assertRaisesRegex(ValueError, 'invalid_model_pin'):
                    route_judge.match_bundle(malformed_fleet, argparse.Namespace(
                        account='codex', provider='codex', model=bad_pin, effort='high', fast=False))
                with self.assertRaisesRegex(ValueError, 'invalid_model_pin'):
                    route_judge.record_constraint(argparse.Namespace(
                        basis='user asked', account='codex', provider='codex', model=bad_pin, effort='high',
                        input=None, usage_cache=None, accounts=None, constraint='user_override',
                        specialty=None, reason_category=None, fast=False, parent_id='p1'))
                with self.assertRaisesRegex(ValueError, 'invalid_model_pin'):
                    roll.named_route_conflict(
                        argparse.Namespace(provider=None, account=None, model=bad_pin, effort=None, fast=False),
                        {'account': 'codex', 'provider': 'codex', 'model': 'gpt-5.5', 'effort': 'high'}, malformed_fleet)
                with self.assertRaisesRegex(ValueError, 'invalid_model_pin'):
                    roll._run(argparse.Namespace(task='do work', brief=None, model=bad_pin), route_judge)
                with self.assertRaisesRegex(ValueError, 'invalid_model_pin'):
                    account_fleet.pin_matching_bundles(bad_pin, account_fleet.pin_pool(malformed_fleet), malformed_fleet)

        # Stored `router-overrides.json` with a malformed entry + a well-formed entry: kept, never asks to delete,
        # plans with only the well-formed entry applied, expires at its own `until`, and refuses new malformed writes:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home_dir = Path(td)
            sdir = home_dir / '.agent-compass'
            sdir.mkdir(parents=True, exist_ok=True)
            ov_path = sdir / 'router-overrides.json'
            for stored_bad in ('/gpt-5.5', 'openai/', '/', ':high', '/unknown-model', 'auto:acct:harness'):
                ov_doc = {'v': 1, 'overrides': [
                    {'id': 'o_bad', 'account': 'codex', 'model': stored_bad, 'boost': 0.35,
                     'until': (self.at + timedelta(hours=6)).isoformat()},
                    {'id': 'o_good', 'account': 'codex', 'model': 'gpt-5.5', 'boost': 0.20,
                     'until': (self.at + timedelta(hours=12)).isoformat()},
                ]}
                ov_path.write_text(json.dumps(ov_doc), encoding='utf-8')
                self.assertIsNone(state_upgrade.check_router_overrides(ov_path), stored_bad)
                self.assertEqual(state_upgrade.collect_issues(home_dir, {'ROUTER_STATE_DIR': str(sdir)}), [])
                with patch.dict(os.environ, {'ROUTER_OVERRIDES': str(ov_path)}), \
                        patch.object(account_fleet, 'load_accounts', return_value=copy.deepcopy(malformed_fleet)), \
                        patch.object(usage_reading, 'usage_snapshot', return_value=copy.deepcopy(usage_both)):
                    loaded_now = quota_overrides.load_overrides(self.at, ov_path, usage=usage_both, accounts=malformed_fleet)
                    self.assertEqual([o['id'] for o in loaded_now], ['o_bad', 'o_good'])
                    planned_ov = allocator.plan(
                        {'objective': 'x', 'profile': self.prof, 'expected_hours': 0.5},
                        copy.deepcopy(usage_both), self.at, accounts=copy.deepcopy(malformed_fleet),
                        overrides=loaded_now, read_local_state=False,
                    )
                    self.assertEqual(planned_ov['choice'], 'b_cx')
                    self.assertEqual(planned_ov['ranking'][0]['components'].get('boost'), 0.20)
                    self.assertEqual([o['id'] for o in planned_ov['decision'].get('overrides', [])], ['o_good'])
                    # Ends at its own `until` expiry while the well-formed entry continues:
                    loaded_after = quota_overrides.load_overrides(
                        self.at + timedelta(hours=7), ov_path, usage=usage_both, accounts=malformed_fleet)
                    self.assertEqual([o['id'] for o in loaded_after], ['o_good'])
                    # New writes stay refused without corrupting the stored file:
                    before_bytes = ov_path.read_bytes()
                    with self.assertRaisesRegex(ValueError, 'invalid_model_pin'):
                        quota_overrides.manage_overrides(argparse.Namespace(
                            list=False, clear=None, account='codex', model=stored_bad, window=None, boost=0.1,
                            resets_at=None, resets_in=None, expires_at=None, expires_in=None, until=None,
                            stop_on_reset=False, reset_confirmation=None, rebaseline=False,
                            basis='user asked', intent='test', usage_cache=None,
                        ), now=self.at)
                    self.assertEqual(ov_path.read_bytes(), before_bytes)

    def test_slash_suffixes_apply_uniformly_across_declaration_default_exact_and_alias(self):
        """Rule (model_resolution.slash_suffixes): declaration, default, exact row and alias all try the full id, then
        what follows each `/` in turn, then the last segment—both undecorated and after stripping decorations/dash effort."""
        for full_id in (
            'google-vertex/deepseek-ai/deepseek-v4.1-flash-maas',
            'google-vertex/openai/gpt-oss-120b-maas',
            'google-vertex/zai-org/glm-5.2-maas',
        ):
            head, tail = full_id.split('/', 1)
            for variant, want_eff in ((full_id, None), (full_id.upper(), None), (f'{full_id}-high', 'high'),
                                      (f'{full_id}@20260801', None)):
                v_tail = variant.split('/', 1)[1]
                r_full = model_resolution.resolve_in(variant, SHIPPED, ())
                r_served = model_resolution.resolve_in(v_tail, SHIPPED, (head,))
                self.assertEqual((r_full['key'], r_full['source'], r_full['effort']),
                                 (r_served['key'], 'alias', want_eff), variant)
            # Fixed-point on moved-name alias (`deepseek-v4.1-flash`): stored_form / default_profile re-resolves to the same row.
            prof = auto_bundles.default_profile(full_id, SHIPPED, ())
            self.assertEqual(prof['source'], 'calibrated')
            self.assertEqual(model_resolution.resolve_in(prof['model'], SHIPPED)['key'], prof['target'])

        # Longer suffix wins before shorter suffix across all four tables (declaration, default, exact row, alias):
        custom = copy.deepcopy(SHIPPED)
        custom['models'] = {**custom['models'], 'pub/exact-row': {}, 'exact-row': {}}
        custom['aliases'] = {**custom['aliases'], 'pub/aliased-row': 'gpt-6-astra', 'aliased-row': 'gpt-6.1-sol'}
        custom['provider_names'] = {
            'statement': 's',
            'defaults': {**custom['provider_names']['defaults'],
                         'pub/def-row': decl('claude-opus-5-5'), 'def-row': decl('claude-sonnet-5-5')},
            'rows': {'gw': {'pub/decl-row': decl('grok-4.7'), 'decl-row': decl('glm-5.2'),
                            'deepseek-v4-flash': decl(None, status='other_weights')}},
        }
        self.assertEqual(model_resolution.resolve_in('gw/pub/exact-row', custom)['key'], 'pub/exact-row')
        self.assertEqual(model_resolution.resolve_in('pub/exact-row', custom)['source'], 'exact')
        self.assertEqual(model_resolution.resolve_in('gw/pub/aliased-row', custom)['key'], 'gpt-6-astra')
        self.assertEqual(model_resolution.resolve_in('gw/pub/aliased-row-high', custom),
                         {'key': 'gpt-6-astra', 'source': 'alias', 'effort': 'high', 'bare': 'aliased-row', 'provider': None})
        self.assertEqual(model_resolution.resolve_in('gw/pub/def-row', custom)['key'], 'claude-opus-5-5')
        self.assertEqual(model_resolution.resolve_in('pub/decl-row', custom, ('gw',))['key'], 'grok-4.7')
        self.assertEqual(model_resolution.resolve_in('other/decl-row', custom, ('gw',))['key'], 'glm-5.2')
        self.assertEqual(model_resolution.resolve_in('opencode/deepseek-v4-flash', custom, ('gw',))['source'], 'other_weights')

    def test_host_provider_mapping_is_scoped_to_magpie_and_preview_names_read_released_rows(self):
        """Rule (R2-F1 & owner 2026-11-20: provider_names.host_providers['magpie'], model_resolution.without_preview):
        Magpie's preset table applies only to Magpie preset ids (never to prefixes in names or non-Magpie accounts whose
        provider id is `zai`, `minimax-cn`, `stepfun` or `xiaomi`); disagreeing catalogs under one multi-catalog Magpie
        preset are a `conflict` on that preset only; a `-preview` name is the released row of that name (`hy3-preview` ->
        `hy3` on every provider), while a provider that retired its preview endpoint (`google`'s
        `gemini-3.1-flash-lite-preview`) stays `retired` there and reads the released row elsewhere."""
        import reference_provenance
        names_doc = json.loads((Path(__file__).resolve().parents[3] / 'audit' / 'models-dev-names.json').read_text(encoding='utf-8'))['names']

        # Owner 2026-11-20: every `-preview` listing of `tencent/hy3-preview` in models-dev-names.json resolves to `hy3`
        # (`hy3-preview-free` ends in `-free`, not `-preview`, so it stays `family`, as in v0.7.2):
        hy3_preview_pairs = [(p, n) for n, entries in sorted(names_doc.items()) if not n.endswith('-free')
                             for p, c, *_ in entries if c == 'tencent/hy3-preview']
        self.assertTrue(hy3_preview_pairs)
        for provider, listed_name in hy3_preview_pairs:
            with self.subTest(provider=provider, listed_name=listed_name):
                ep_res = model_resolution.resolve_in(listed_name, SHIPPED, (provider,))
                pref_res = model_resolution.resolve_in(
                    listed_name if listed_name.lower().startswith(provider.lower() + '/') else f'{provider}/{listed_name}',
                    SHIPPED, (),
                )
                self.assertEqual((ep_res['key'], ep_res['source']), ('hy3', 'alias'))
                self.assertEqual((pref_res['key'], pref_res['source']), ('hy3', 'alias'))
        self.assertEqual(model_resolution.resolve_in('hy3-preview-free', SHIPPED, ('opencode',))['source'], 'family')

        # `gemini-3.1-flash-lite-preview` stays `retired` on `google` (endpoint shut down) and reads `gemini-3.1-flash-lite` elsewhere:
        self.assertEqual(model_resolution.resolve_in('gemini-3.1-flash-lite-preview', SHIPPED, ('google',))['source'], 'retired')
        self.assertEqual(model_resolution.resolve_in('google/gemini-3.1-flash-lite-preview', SHIPPED, ())['source'], 'retired')
        for relay in ('openrouter', 'vivgrid'):
            self.assertEqual(model_resolution.resolve_in('gemini-3.1-flash-lite-preview', SHIPPED, (relay,))['key'],
                             'gemini-3.1-flash-lite')

        # F1: non-Magpie prefixes and non-Magpie OpenAI-compatible accounts never read Magpie's preset table:
        self.assertEqual(model_resolution.resolve_in('opencode-zen/deepseek-v4-flash', SHIPPED, ())['source'], 'default')
        self.assertEqual(auto_bundles.endpoint_providers({'type': 'openai_compatible'}, 'zai', 'zai', SHIPPED), ('zai',))
        self.assertEqual(auto_bundles.endpoint_providers({'type': 'openai_compatible'}, 'opencode-zen', 'opencode-zen', SHIPPED),
                         ('opencode-zen',))

        test_acc = copy.deepcopy(SHIPPED)
        test_acc['provider_names']['rows'] = {
            **test_acc['provider_names']['rows'],
            'stepfun-ai-step-plan': {'probe-step': decl('gpt-5.5')},
            'stepfun-ai': {'probe-step': decl('gpt-6-astra')},
            'xiaomi-token-plan-cn': {'probe-mimo': decl('gpt-5.5')},
            'xiaomi-token-plan-sgp': {'probe-mimo': decl(None, status='other_weights')},
            'zhipuai': {'glm-test': decl(None, status='other_weights')},
            'zai': {'glm-test': decl('glm-5.2')},
            'minimax': {'mm-test': decl(None, status='other_weights')},
            'minimax-cn': {'mm-test': decl('glm-5.2')},
        }
        # Non-Magpie prefix or non-Magpie endpoint reads only its own provider id, never Magpie's mapped catalogs:
        self.assertEqual(model_resolution.resolve_in('zai/glm-test', test_acc, ())['key'], 'glm-5.2')
        self.assertEqual(model_resolution.resolve_in('glm-test', test_acc, ('zai',))['key'], 'glm-5.2')
        self.assertEqual(model_resolution.resolve_in('minimax-cn/mm-test', test_acc, ())['key'], 'glm-5.2')
        self.assertEqual(model_resolution.resolve_in('stepfun/probe-step', test_acc, ())['source'], 'unknown')
        self.assertEqual(model_resolution.resolve_in('xiaomi/probe-mimo', test_acc, ())['source'], 'unknown')
        # Magpie preset `zai` replaces `zai` with `zhipuai` (no collision with `zai`), while a multi-catalog Magpie preset
        # whose catalogs disagree resolves to `conflict` on that Magpie preset only:
        magpie_zai = model_resolution.expand_host_providers(('zai',), 'magpie', test_acc)
        self.assertEqual(magpie_zai, ('zhipuai',))
        self.assertEqual(model_resolution.resolve_in('glm-test', test_acc, magpie_zai)['source'], 'other_weights')
        magpie_stepfun = model_resolution.expand_host_providers(('stepfun',), 'magpie', test_acc)
        self.assertEqual(model_resolution.resolve_in('probe-step', test_acc, magpie_stepfun)['source'], 'conflict')
        magpie_xiaomi = model_resolution.expand_host_providers(('xiaomi',), 'magpie', test_acc)
        self.assertEqual(model_resolution.resolve_in('probe-mimo', test_acc, magpie_xiaomi)['source'], 'conflict')

        # Magpie-bound accounts (`magpie_providers` / `endpoint_providers`) expand through `host_providers.magpie`:
        doc = copy.deepcopy(SHIPPED)
        doc['accounts'] = {'my-zen': {'billing': 'subscription', 'family': 'codex', 'windows': {'balance': {'hours': 168}}, 'bundles': {}}}
        doc['_discovery'] = {'accounts': {'my-zen': {'magpie_providers': ['opencode-zen', 'my-zen']}}}
        for cat_entry in (
            {'ok': True, 'type': 'configured', 'checked_at': NOW, 'endpoint_providers': ['opencode-zen'],
             'models': ['deepseek-v4-flash']},
            {'ok': True, 'type': 'openai_compatible', 'checked_at': NOW, 'models': [{'id': 'deepseek-v4-flash'}]},
        ):
            derived = auto_bundles.derive(doc, {'providers': {'my-zen': cat_entry}}, NOW)['bundles']['my-zen']
            b = next(iter(derived.values()))
            self.assertEqual((b['model'], b['score_source']), ('opencode/deepseek-v4-flash', 'other_weights'))
            self.assertIn('opencode', b['_served'])
            self.assertIn('opencode-zen', b['_presets'])
            fleet = self.subscription_fleet({'my-zen': ('codex', derived, 'balance')})
            self.assertEqual(self.plan_pin(fleet, model='deepseek-v4-flash')['choice'], next(iter(derived)))
            self.assertEqual(self.plan_pin(fleet, model='opencode-zen/deepseek-v4-flash')['choice'], next(iter(derived)))
            self.assertEqual(self.plan_pin(fleet, model='opencode/deepseek-v4-flash')['choice'], next(iter(derived)))

        # Rule (Magpie preset id as prefix): on a Magpie account, the preset id (`_presets`) and each catalog id it serves
        # (`_served`) name that account's bundles of the row and no other account's, whether the bundle advertises the bare
        # name or a prefixed one, without changing which `provider_names` declarations are read.
        from router_test_support import usage_windows
        for acct_name, preset_id, served_ids in (
            ('my-zen', 'opencode-zen', ('opencode',)),
            ('my-step', 'stepfun', ('stepfun-ai-step-plan', 'stepfun-ai')),
        ):
            for advertised in ('gpt-6-astra', f'{preset_id}/gpt-6-astra', f'{served_ids[0]}/gpt-6-astra'):
                mdoc = copy.deepcopy(SHIPPED)
                mdoc['accounts'] = {acct_name: {'billing': 'subscription', 'family': 'codex',
                                                'windows': {'usage': {'hours': 168}}, 'bundles': {}}}
                mdoc['_discovery'] = {'accounts': {acct_name: {'magpie_providers': [preset_id]}}}
                m_derived = auto_bundles.derive(
                    mdoc,
                    {'providers': {acct_name: {'ok': True, 'type': 'openai_compatible', 'checked_at': NOW,
                                               'models': [{'id': advertised}]}}},
                    NOW,
                )['bundles'][acct_name]
                m_bid = next(iter(m_derived))
                codex_bundles = {
                    'b_codex_astra': {'provider': 'codex', 'harness': 'codex', 'model': 'gpt-6-astra',
                                      'launch_model': 'gpt-6-astra', 'effort': 'high', 'draws': ['usage']},
                    'b_codex_gpt55': {'provider': 'codex', 'harness': 'codex', 'model': 'gpt-5.5',
                                      'launch_model': 'gpt-5.5', 'effort': 'high', 'draws': ['usage']},
                }
                u_mag = usage_windows(self.at, {acct_name: [('usage', 90, 100)], 'codex': [('usage', 90, 100)]})
                for rev in (False, True):
                    items = [(acct_name, ('codex', copy.deepcopy(m_derived), 'usage')),
                             ('codex', ('codex', copy.deepcopy(codex_bundles), 'usage'))]
                    if rev:
                        items.reverse()
                    fl = self.subscription_fleet(dict(items))
                    pool = account_fleet.pin_pool(fl)
                    for pfx in (acct_name, preset_id, *served_ids):
                        with self.subTest(preset=preset_id, advertised=advertised, rev=rev, prefix=pfx):
                            self.assertEqual(
                                [x['id'] for x in account_fleet.pin_matching_bundles(f'{pfx}/gpt-6-astra', pool, fl)],
                                [m_bid],
                            )
                            self.assertEqual(self.plan_pin(fl, model=f'{pfx}/gpt-6-astra', usage=u_mag)['choice'], m_bid)
                            # Disabling the Magpie bundle blocks rather than falling onto Codex's `gpt-6-astra`:
                            fl_dis = copy.deepcopy(fl)
                            fl_dis['accounts'][acct_name]['bundles'][m_bid]['disabled'] = True
                            p_dis = self.plan_pin(fl_dis, model=f'{pfx}/gpt-6-astra', usage=u_mag)
                            self.assertEqual((p_dis['choice'], p_dis['decision']['blocked']), ('none', 'named_model_unavailable'))
                            # Asking `<pfx>/gpt-5.5` (which only Codex holds) returns `none` rather than Clause 3:
                            p_miss = self.plan_pin(fl, model=f'{pfx}/gpt-5.5', usage=u_mag)
                            self.assertEqual(p_miss['choice'], 'none')
                            self.assertIn('no configured bundle offers it', p_miss['decision']['reason'])
                    # Bare `gpt-6-astra` still names both accounts' bundles:
                    self.assertEqual(
                        {x['id'] for x in account_fleet.pin_matching_bundles('gpt-6-astra', pool, fl)},
                        {m_bid, 'b_codex_astra'},
                    )

        # `_presets` does not change which `provider_names` declarations are read: Magpie preset `zai` reads `zhipuai`'s
        # `other_weights` declaration (never `rows['zai']`'s `glm-5.2`), while `zai/glm-test` and `zhipuai/glm-test` both name it:
        zai_doc = copy.deepcopy(test_acc)
        zai_doc['accounts'] = {'my-zai': {'billing': 'subscription', 'family': 'codex', 'windows': {'usage': {'hours': 168}}, 'bundles': {}}}
        zai_doc['_discovery'] = {'accounts': {'my-zai': {'magpie_providers': ['zai']}}}
        zai_derived = auto_bundles.derive(
            zai_doc,
            {'providers': {'my-zai': {'ok': True, 'type': 'openai_compatible', 'checked_at': NOW, 'models': [{'id': 'glm-test'}]}}},
            NOW,
        )['bundles']['my-zai']
        zai_b = next(iter(zai_derived.values()))
        self.assertEqual((zai_b['model'], zai_b['score_source']), ('zhipuai/glm-test', 'other_weights'))
        zai_fl = self.subscription_fleet({'my-zai': ('codex', zai_derived, 'usage')})
        for pfx in ('zai', 'zhipuai', 'my-zai'):
            self.assertEqual(
                [x['id'] for x in account_fleet.pin_matching_bundles(f'{pfx}/glm-test', account_fleet.pin_pool(zai_fl), zai_fl)],
                [next(iter(zai_derived))],
            )

        # Provenance validation checks `provider_names.host_providers` citations and rejects top-level `providers`:
        reference_provenance.validate_accounts_provenance(SHIPPED)
        zen_entry = SHIPPED['provider_names']['host_providers']['magpie']['opencode-zen']
        for bad_pn in (
            {**SHIPPED['provider_names'], 'providers': {'opencode-zen': zen_entry}},
            {**SHIPPED['provider_names'], 'host_providers': {'Magpie': {'opencode-zen': zen_entry}}},
            {**SHIPPED['provider_names'], 'host_providers': {'magpie': {'OpenCode-Zen': zen_entry}}},
            {**SHIPPED['provider_names'], 'host_providers': {'magpie': {'opencode-zen': {**zen_entry, 'providers': []}}}},
            {**SHIPPED['provider_names'], 'host_providers': {'magpie': {'opencode-zen': {**zen_entry, 'providers': ['opencode-zen']}}}},
            {**SHIPPED['provider_names'], 'host_providers': {'magpie': {'opencode-zen': {**zen_entry, 'url': 'not-https'}}}},
        ):
            broken = copy.deepcopy(SHIPPED)
            broken['provider_names'] = bad_pn
            with self.assertRaisesRegex(ValueError, 'provider_names'):
                reference_provenance.validate_accounts_provenance(broken)

    def test_row_key_functions_never_re_resolve_row_ids_that_are_also_declared_names(self):
        """Rule (R2-F4: account_fleet.row_spec, quota_pools.applicable_windows, quota_overrides.override_depleted):
        over every row id in `accounts.models` whose id is also a declared name in `provider_names`, a function that
        takes a resolved row key answers for that row and never reads it again as the moved name."""
        import quota_overrides
        import quota_pools

        pn = SHIPPED['provider_names']
        declared_names = set(pn.get('defaults') or {}) | {
            name for entries in (pn.get('rows') or {}).values() for name in (entries or {})
        }
        declared_row_ids = sorted(rid for rid in SHIPPED['models'] if rid.lower() in declared_names)
        moved_row_ids = [rid for rid in declared_row_ids if model_resolution.resolve_in(rid, SHIPPED)['key'] != rid]
        self.assertTrue(moved_row_ids)

        for rid in declared_row_ids:
            with self.subTest(row_id=rid):
                self.assertIs(account_fleet.row_spec(SHIPPED, rid), SHIPPED['models'][rid])
                spec = {
                    'billing': 'subscription',
                    'windows': {
                        'w_own': {'hours': 168, 'models': [rid]},
                        'w_other': {'hours': 168, 'models_except': [rid]},
                    },
                }
                acc = {**SHIPPED, 'accounts': {'codex': spec}}
                bound = [wid for wid, _, _ in quota_pools.applicable_windows(spec, rid, {}, accounts=acc)]
                self.assertEqual(bound, ['w_own'])

        # Windowless override on repository id `deepseek-ai/DeepSeek-V4-Pro` (`stored_model == 'deepseek-v4-pro'`)
        # checks `w_repo` (80% remaining -> healthy) and never `w_moved` (`deepseek-v4-pro-0813`, 2% remaining -> depleted):
        split_spec = {
            'billing': 'subscription',
            'windows': {
                'w_repo': {'hours': 168, 'models_except': ['deepseek-v4-pro-0813']},
                'w_moved': {'hours': 168, 'models': ['deepseek-v4-pro-0813']},
            },
        }
        split_acc = {**SHIPPED, 'accounts': {'codex': split_spec}}
        split_usage = {
            'accounts': {
                'codex': {
                    'account': 'codex', 'status': 'fresh', 'stale': False, 'available': True,
                    'fetched_at': self.at.isoformat(),
                    'windows': [
                        {'id': 'w_repo', 'used_pct': 20.0, 'resets_at': '2026-11-25T00:00:00+00:00'},
                        {'id': 'w_moved', 'used_pct': 98.0, 'resets_at': '2026-11-25T00:00:00+00:00'},
                    ],
                },
            },
        }
        stored_repo = account_fleet.stored_form('deepseek-ai/DeepSeek-V4-Pro', split_acc, 'codex')
        stored_moved = account_fleet.stored_form('deepseek-v4-pro', split_acc, 'codex')
        self.assertEqual((stored_repo, stored_moved), ('deepseek-ai/DeepSeek-V4-Pro', 'deepseek-v4-pro-0813'))
        self.assertFalse(quota_overrides.override_depleted(
            {'account': 'codex', 'model': stored_repo, 'boost': 0.1}, split_usage, split_acc))
        self.assertTrue(quota_overrides.override_depleted(
            {'account': 'codex', 'model': stored_moved, 'boost': 0.1}, split_usage, split_acc))

        # Public-path checks for M14b (`dispatch_registry.prepare` fast validation), M14c (`record_constraint`
        # safety-margin `launch_blockers` unit_rate), and M14d (`roll.cli` metered proxy `api_family`):
        import argparse
        import os
        import tempfile
        from unittest.mock import MagicMock, patch
        import axis_scores
        import dispatch_registry
        import live_models
        import roll
        import route_judge
        import usage_reading

        fast_fleet = copy.deepcopy(SHIPPED)
        fast_fleet['models'] = {
            **fast_fleet['models'],
            'deepseek-v4-pro': {**fast_fleet['models']['deepseek-v4-pro'], 'fast_capable': True, 'api_family': 'codex',
                                'usd_per_hour': 5.0},
            'deepseek-v4-pro-0813': {k: v for k, v in fast_fleet['models']['deepseek-v4-pro-0813'].items() if k != 'usd_per_hour'},
        }
        fast_fleet['models']['deepseek-v4-pro-0813']['fast_capable'] = False
        fast_fleet['models']['deepseek-v4-pro-0813']['api_family'] = 'claude'
        fast_fleet['accounts'] = {
            'codex': {
                'billing': 'subscription', 'plan': 'pro', 'verified': True, 'family': 'codex',
                'windows': {'usage': {'hours': 168}}, 'capacity_usd': {'usage': 100.0},
                'capacity_source': {'usage': {'evidence': 'test'}},
                'bundles': {
                    'repo_pro': {'provider': 'codex', 'harness': 'codex', 'model': 'deepseek-ai/DeepSeek-V4-Pro',
                                 'launch_model': 'deepseek-ai/DeepSeek-V4-Pro', 'effort': 'high', 'draws': ['usage']},
                },
            },
        }
        real_overlay = axis_scores.five_axis_overlay

        def custom_overlay(acc, *a, **kw):
            out = real_overlay(acc, *a, **kw)
            out['models']['deepseek-v4-pro'] = {**out['models']['deepseek-v4-pro'], 'fast_capable': True,
                                                'api_family': 'codex', 'usd_per_hour': 5.0}
            out['models']['deepseek-v4-pro-0813'] = {k: v for k, v in out['models']['deepseek-v4-pro-0813'].items()
                                                     if k != 'usd_per_hour'}
            out['models']['deepseek-v4-pro-0813']['fast_capable'] = False
            out['models']['deepseek-v4-pro-0813'].setdefault('five_axis', {})['usd_status'] = 'missing'
            return out

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td, \
                patch.dict(os.environ, {'ROUTER_DISPATCH_DB': os.path.join(td, 'db.sqlite3'),
                                        'ROUTER_LEDGER': os.path.join(td, 'ledger.jsonl'),
                                        'ROUTER_LIVE_MODELS': os.path.join(td, 'live-models.json')}), \
                patch.object(account_fleet, 'load_accounts', return_value=copy.deepcopy(fast_fleet)), \
                patch.object(axis_scores, 'five_axis_overlay', side_effect=custom_overlay), \
                patch.object(live_models, 'fetch_from_paseo', return_value={'ok': True, 'providers': {
                    'codex': {'ok': True, 'models': [{'id': 'deepseek-ai/DeepSeek-V4-Pro'}]},
                }}):
            # M14b: `record_constraint(..., fast=True)` -> `dispatch_registry.prepare` reads `row_spec('deepseek-v4-pro')`.
            task_fast = {'objective': 'x', 'profile': self.prof, 'expected_hours': 0.5,
                         'parent_agent_id': 'parent-1', 'task_id': 't-m14b'}
            args_fast = argparse.Namespace(
                basis='user asked fast', account='codex', provider='codex', model='deepseek-ai/DeepSeek-V4-Pro',
                effort='high', input=None, usage_cache=None, accounts=None, constraint='user_override',
                specialty=None, reason_category=None, fast=True, parent_id='parent-1',
            )
            with patch.object(usage_reading, 'usage_snapshot', return_value=copy.deepcopy(self.usage)):
                rec_fast = route_judge.record_constraint(args_fast, task_fast)
                prep_fast = dispatch_registry.prepare(rec_fast['decision_id'], task_fast,
                                                      {'title': 'x', 'initialPrompt': 'go'},
                                                      copy.deepcopy(self.usage), accounts=copy.deepcopy(fast_fleet), now=self.at)
                self.assertTrue(prep_fast['bundle'].get('fast'))

            # M14c: at 4% remaining (<= 5% safety margin), `launch_blockers` reads `row_spec('deepseek-v4-pro')` (`usd_per_hour=5.0`).
            from datetime import datetime, timezone
            from router_test_support import usage_windows
            low_usage = usage_windows(datetime.now(timezone.utc), {'codex': [('usage', 4, 100)]})
            low_usage['accounts']['codex']['status'] = 'fresh'
            args_margin = argparse.Namespace(**{**vars(args_fast), 'fast': False})
            with patch.object(usage_reading, 'usage_snapshot', return_value=low_usage), \
                    self.assertRaisesRegex(ValueError, 'safety margin'):
                route_judge.record_constraint(args_margin, dict(task_fast, task_id='t-m14c'))

            # M14d: `roll.cli` on a metered override proxy with no `harness`/`family` reads `row_spec('deepseek-v4-pro')['api_family']`.
            proxy_fleet = copy.deepcopy(SHIPPED)
            proxy_fleet['models'] = {
                **proxy_fleet['models'],
                'deepseek-v4-pro': {**proxy_fleet['models']['deepseek-v4-pro'], 'api_family': 'claude'},
                'deepseek-v4-pro-0813': {**proxy_fleet['models']['deepseek-v4-pro-0813'], 'api_family': 'codex'},
            }
            ov_path = Path(td) / 'accounts-override.json'
            ov_path.write_text(json.dumps({
                'schema_version': 2,
                'accounts': {'my-proxy': {'billing': 'metered', 'endpoint': 'https://proxy.example/anthropic', 'api_key': 'sk-test'}},
            }), encoding='utf-8')
            fake_proc = MagicMock(pid=42424)
            with patch.dict(os.environ, {'AGENTCOMPASS_ACCOUNTS_OVERRIDE': str(ov_path)}), \
                    patch.object(account_fleet, 'load_accounts', return_value=proxy_fleet), \
                    patch.object(roll, 'CLI_RUNS_DIR', Path(td) / 'cli-runs'), \
                    patch.object(roll, 'cli_command', return_value=['claude-bin']), \
                    patch.object(roll.subprocess, 'Popen', return_value=fake_proc) as popen_mock:
                cli_res, cli_code = roll.cli(
                    argparse.Namespace(workspace=None),
                    {'stage': 'roll', 'parent_id': 'parent-1', 'parent_host': 'paseo', 'parent_address': 'parent-1'},
                    {'provider': 'my-proxy', 'account': 'my-proxy', 'model': 'deepseek-ai/DeepSeek-V4-Pro',
                     'launch_model': 'deepseek-ai/DeepSeek-V4-Pro', 'effort': 'high'},
                    'run task',
                )
                self.assertEqual((cli_code, cli_res['worker']['cli']), (0, 'claude'))
                self.assertEqual(popen_mock.call_args.kwargs['env'].get('ANTHROPIC_BASE_URL'), 'https://proxy.example/anthropic')


if __name__ == '__main__':
    unittest.main()
