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
                                        'ROUTER_LEDGER': os.path.join(directory, 'ledger.jsonl')}), \
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
                        self.assertEqual(res['bundle'].get('bundle_id'), next(iter(derived['bundles']['opencode'])))
                    prep = dispatch_registry.prepare(res['decision_id'], task, {'title': 'x', 'initialPrompt': 'go'},
                                                     copy.deepcopy(self.usage), accounts=copy.deepcopy(fleet), now=self.at)
                    self.assertEqual(prep['request']['provider'], want_provider)

        # roll.named_route_conflict on an endpoint worker and on a launcher worker:
        ep_bundle = {'provider': 'opencode', 'account': 'opencode', 'model': 'opencode/deepseek-v4-flash',
                     'launch_model': 'deepseek-v4-flash', 'effort': 'high', '_served': ['opencode']}
        novel_bundle = {'provider': 'codex', 'account': 'codex', 'model': 'codex/novel-gpt-9',
                        'launch_model': 'codex/novel-gpt-9', 'effort': 'high'}
        ns = lambda m, e=None, p='opencode', a='opencode': argparse.Namespace(provider=p, account=a, model=m, effort=e, fast=False)
        self.assertIsNone(roll.named_route_conflict(ns('deepseek-v4-flash'), ep_bundle, fleet))
        self.assertIsNone(roll.named_route_conflict(ns('opencode/deepseek-v4-flash:high'), ep_bundle, fleet))
        self.assertIsNone(roll.named_route_conflict(ns('openai/novel-gpt-9', p='codex', a='codex'), novel_bundle, fleet))
        self.assertIn('--model', roll.named_route_conflict(ns('deepseek/deepseek-v4-flash'), ep_bundle, fleet) or '')
        self.assertIn('--model', roll.named_route_conflict(ns('deepseek-v4-flash-medium'), ep_bundle, fleet) or '')

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

    def test_host_provider_mapping_is_scoped_to_magpie_and_all_hy3_preview_listings_are_declared(self):
        """Rule (R2-F1/F2: provider_names.host_providers['magpie'], model_resolution.expand_host_providers,
        auto_bundles.endpoint_providers): Magpie's preset table applies only to Magpie preset ids (never to prefixes in
        names or non-Magpie accounts whose provider id is `zai`, `minimax-cn`, `stepfun` or `xiaomi`); disagreeing
        catalogs under one multi-catalog Magpie preset are a `conflict` on that preset only; and every models.dev
        listing of `tencent/hy3-preview` is declared `other_weights`."""
        import reference_provenance
        names_doc = json.loads((Path(__file__).resolve().parents[3] / 'audit' / 'models-dev-names.json').read_text(encoding='utf-8'))['names']

        # F2: every (provider, name) listing in models-dev-names.json with canonical id `tencent/hy3-preview` resolves
        # to `other_weights` on that provider (both as an endpoint and as a launcher prefix):
        hy3_preview_pairs = [(p, n) for n, entries in sorted(names_doc.items()) for p, c, *_ in entries if c == 'tencent/hy3-preview']
        self.assertTrue(hy3_preview_pairs)
        for provider, listed_name in hy3_preview_pairs:
            with self.subTest(provider=provider, listed_name=listed_name):
                ep_res = model_resolution.resolve_in(listed_name, SHIPPED, (provider,))
                pref_res = model_resolution.resolve_in(
                    listed_name if listed_name.lower().startswith(provider.lower() + '/') else f'{provider}/{listed_name}',
                    SHIPPED, (),
                )
                self.assertEqual((ep_res['key'], ep_res['source'], ep_res['provider']), (None, 'other_weights', provider.lower()))
                self.assertEqual((pref_res['key'], pref_res['source'], pref_res['provider']), (None, 'other_weights', provider.lower()))

        # F1: non-Magpie prefixes and non-Magpie OpenAI-compatible accounts never read Magpie's preset table:
        self.assertEqual(model_resolution.resolve_in('opencode-zen/deepseek-v4-flash', SHIPPED, ())['source'], 'default')
        self.assertEqual(auto_bundles.endpoint_providers({'type': 'openai_compatible'}, 'zai', 'zai', SHIPPED), ('zai',))
        self.assertEqual(auto_bundles.endpoint_providers({'type': 'openai_compatible'}, 'opencode-zen', 'opencode-zen', SHIPPED),
                         ('opencode-zen',))

        test_acc = copy.deepcopy(SHIPPED)
        test_acc['provider_names']['rows'] = {
            **test_acc['provider_names']['rows'],
            'stepfun-ai-step-plan': {'step-test': decl('gpt-5.5')},
            'stepfun-ai': {'step-test': decl('gpt-6-astra')},
            'xiaomi-token-plan-cn': {'mimo-test': decl('gpt-5.5')},
            'xiaomi-token-plan-sgp': {'mimo-test': decl(None, status='other_weights')},
            'zhipuai': {'glm-test': decl(None, status='other_weights')},
            'zai': {'glm-test': decl('glm-5.2')},
            'minimax': {'mm-test': decl(None, status='other_weights')},
            'minimax-cn': {'mm-test': decl('glm-5.2')},
        }
        # Non-Magpie prefix or non-Magpie endpoint reads only its own provider id, never Magpie's mapped catalogs:
        self.assertEqual(model_resolution.resolve_in('zai/glm-test', test_acc, ())['key'], 'glm-5.2')
        self.assertEqual(model_resolution.resolve_in('glm-test', test_acc, ('zai',))['key'], 'glm-5.2')
        self.assertEqual(model_resolution.resolve_in('minimax-cn/mm-test', test_acc, ())['key'], 'glm-5.2')
        self.assertEqual(model_resolution.resolve_in('stepfun/step-test', test_acc, ())['source'], 'unknown')
        self.assertEqual(model_resolution.resolve_in('xiaomi/mimo-test', test_acc, ())['source'], 'unknown')
        # Magpie preset `zai` replaces `zai` with `zhipuai` (no collision with `zai`), while a multi-catalog Magpie preset
        # whose catalogs disagree resolves to `conflict` on that Magpie preset only:
        magpie_zai = model_resolution.expand_host_providers(('zai',), 'magpie', test_acc)
        self.assertEqual(magpie_zai, ('zhipuai',))
        self.assertEqual(model_resolution.resolve_in('glm-test', test_acc, magpie_zai)['source'], 'other_weights')
        magpie_stepfun = model_resolution.expand_host_providers(('stepfun',), 'magpie', test_acc)
        self.assertEqual(model_resolution.resolve_in('step-test', test_acc, magpie_stepfun)['source'], 'conflict')
        magpie_xiaomi = model_resolution.expand_host_providers(('xiaomi',), 'magpie', test_acc)
        self.assertEqual(model_resolution.resolve_in('mimo-test', test_acc, magpie_xiaomi)['source'], 'conflict')

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
            fleet = self.subscription_fleet({'my-zen': ('codex', derived, 'balance')})
            self.assertEqual(self.plan_pin(fleet, model='deepseek-v4-flash')['choice'], next(iter(derived)))

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


if __name__ == '__main__':
    unittest.main()
