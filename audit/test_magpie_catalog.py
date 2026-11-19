import gzip
import json
import tempfile
import unittest
from pathlib import Path

import magpie_catalog as mc

PRESETS = '''package provider

// a comment with { braces } that the reader ignores
var presets = []PresetDef{
	{ID: "maker", Kind: KindVendor, Catalog: "maker"},
	{ID: "google", Kind: KindVendor, Catalog: "google"},
	{ID: "cloud", Kind: KindVendor, Hosts: true, Catalog: "cloud"},
	{ID: "router", Kind: KindAggregator, Catalog: "router"},
	{ID: "plan", Kind: KindVendor, Models: []string{"maker-own", "nobody-1"}},
}
'''
CATALOG = '''package catalog

var reasoningOffProviders = map[string]struct{}{
	"maker": {},
}

func textModel(m mdModel) bool {
	for _, bad := range []string{"embed", "tts"} {
		_ = bad
	}
	return true
}

var (
	variantSuffix = lazyre.New(`-(?:low|high|fast)$`)
)

var bedrockGeos = []string{"us.", "global."}
'''
ULTRA = 'package provider\n\nvar datedSuffix = lazyre.New(`-\\d{8}$`)\n'
BUNDLED = 'package catalog\n\nvar bundledAt = time.Date(2026, 11, 10, 0, 0, 0, 0, time.UTC)\n'
GOOGLE = '''package provider

var geminiModels = []catalog.Model{
	{ID: "gem-1", Name: "Gem 1"},
}

var antigravityModels = []catalog.Model{
	{ID: "gem-1-low", Name: "Gem 1 (Low)"},
}
'''


def model(mid, efforts=None, toggle=False, out=('text',), canonical=None, cost=None):
    opts = ([{'type': 'toggle'}] if toggle else []) + ([{'type': 'effort', 'values': list(efforts)}] if efforts else [])
    return {'id': mid, 'reasoning': bool(efforts or toggle), 'reasoning_options': opts, 'modalities': {'output': list(out)},
            **({'canonical_model_id': canonical} if canonical else {}), **({'cost': cost} if cost else {})}


SNAPSHOT = {
    'maker': {'models': {'m-1': model('m-1', ['low', 'high'], toggle=True, canonical='maker/m-1', cost={'input': 1, 'output': 2}),
                         'maker-own': model('maker-own', ['medium']),           # no canonical id: the namespace's own listing
                         'm-embed': model('m-embed')}},
    'google': {'models': {'gem-1': model('gem-1', ['low', 'high'], canonical='google/gem-1')}},
    'cloud': {'models': {'m-1': model('m-1', ['low', 'high'], toggle=True, canonical='maker/m-1', cost={'input': 1, 'output': 3}),
                         'c-2': model('c-2')}},                                  # no canonical id, no namespace: maker unknown
    'router': {'models': {'r-3': model('r-3', ['high'], canonical='other/r-3')}},
    'anthropic': {'models': {'claude-x': model('claude-x', ['low', 'max'], canonical='anthropic/claude-x')}},
}
SOURCE = 'internal/provider/presets.go:5'
MAKERS = {'maker': 'maker', 'google': 'google', 'cloud': None, 'plan': 'maker', 'account:claude': 'anthropic',
          'account:gemini-cli': 'google', 'account:antigravity': 'google'}


def makers_file(root, makers=MAKERS):
    path = Path(root, 'makers.json')
    path.write_text(json.dumps({'schema': mc.MAKERS_SCHEMA, 'makers': {
        p: {'maker': k, 'source': SOURCE, 'reads': 'r'} for p, k in makers.items()}}), encoding='utf-8')
    return path


def tree(root, presets=PRESETS, catalog=CATALOG):
    files = {'internal/provider/presets.go': presets, 'internal/catalog/catalog.go': catalog, 'internal/provider/ultra.go': ULTRA,
             'internal/catalog/bundled.go': BUNDLED, 'internal/provider/google.go': GOOGLE}
    for rel, text in files.items():
        Path(root, rel).parent.mkdir(parents=True, exist_ok=True)
        Path(root, rel).write_text(text, encoding='utf-8')
    Path(root, 'internal/catalog/bundled').mkdir(parents=True, exist_ok=True)
    with gzip.open(Path(root, 'internal/catalog/bundled/api.json.gz'), 'wt', encoding='utf-8') as f:
        json.dump(SNAPSHOT, f)
    return Path(root)


class MagpieCatalogTests(unittest.TestCase):
    """The saved Magpie catalog is a static read of Magpie's source (audit/magpie_catalog.py)."""

    def test_the_gate_is_per_pair_the_models_maker_against_the_providers_declared_maker(self):
        """Rule (round 5 review E): a row's maker is the namespace of its models.dev canonical id, else the provider when
        the provider's id is a namespace, else the one maker the snapshot's listings of the id agree on, else none; a pair is
        gated when that maker is the provider's maker declared in the makers file (with its Magpie source line)."""
        with tempfile.TemporaryDirectory() as tmp:
            doc = mc.build(tree(tmp), '0.0.1', 'abc', makers_file(tmp))
        self.assertEqual(doc['gate']['makers'], MAKERS)
        maker = {(r['provider'], r['model']): r['maker'] for r in doc['rows']}
        self.assertEqual((maker[('maker', 'm-1')], maker[('maker', 'maker-own')], maker[('cloud', 'm-1')],
                          maker[('cloud', 'c-2')], maker[('router', 'r-3')], maker[('plan', 'maker-own')],
                          maker[('plan', 'nobody-1')]), ('maker', 'maker', 'maker', None, 'other', 'maker', None))
        gated = {k for k, v in maker.items() if v and doc['gate']['makers'].get(k[0]) == v}
        self.assertEqual(gated, {('maker', 'm-1'), ('maker', 'maker-own'), ('google', 'gem-1'), ('plan', 'maker-own'),
                                 ('account:claude', 'claude-x'), ('account:gemini-cli', 'gem-1')})

    def test_a_vendor_preset_or_account_without_a_declared_maker_stops_the_reader(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = tree(tmp)
            with self.assertRaises(SystemExit):
                mc.build(src, '0.0.1', 'abc', makers_file(tmp, {k: v for k, v in MAKERS.items() if k != 'cloud'}))
            bad = Path(tmp, 'bad.json')
            bad.write_text(json.dumps({'schema': mc.MAKERS_SCHEMA, 'makers': {
                p: {'maker': k, 'source': 'presets.go', 'reads': 'r'} for p, k in MAKERS.items()}}), encoding='utf-8')
            with self.assertRaises(SystemExit):           # the source names no Magpie source line
                mc.build(src, '0.0.1', 'abc', bad)

    def test_the_names_file_lists_every_alias_name_and_target_with_its_listings(self):
        """Rule (round 5 review A; provider_names, 2026-11-19): every alias name and target and every provider_names name of
        accounts.json, each with every listing of that exact id as [provider, canonical id, input, output] (a namespace
        provider's listing without canonical id is its own)."""
        with tempfile.TemporaryDirectory() as tmp:
            doc = mc.names(tree(tmp), '0.0.1', 'abc', {'aliases': {'m-1': 'maker-own', 'short': 'nowhere'},
                                                       'provider_names': {'defaults': {'moved': {}},
                                                                          'rows': {'cloud': {'served': {}}}}})
        self.assertEqual(doc['schema'], mc.NAMES_SCHEMA)
        self.assertEqual(doc['names'], {'m-1': [['cloud', 'maker/m-1', 1, 3], ['maker', 'maker/m-1', 1, 2]],
                                        'maker-own': [['maker', 'maker/maker-own', None, None]], 'nowhere': [], 'short': [],
                                        'moved': [], 'served': []})

    def test_rows_and_efforts_follow_magpies_own_rules(self):
        """Magpie's rules, as its source states them: `none` first for a model with a thinking switch at a provider of
        reasoningOffProviders; a non-text model left out; a model the provider has no word on borrows its maker's efforts,
        through a level suffix; no word anywhere is null (the model's one configuration)."""
        with tempfile.TemporaryDirectory() as tmp:
            rows = {(r['provider'], r['model']): r['efforts'] for r in mc.build(tree(tmp), '0.0.1', 'abc', makers_file(tmp))['rows']}
        self.assertEqual(rows[('maker', 'm-1')], ['none', 'low', 'high'])
        self.assertEqual(rows[('cloud', 'm-1')], ['low', 'high'])
        self.assertNotIn(('maker', 'm-embed'), rows)
        self.assertEqual(rows[('plan', 'maker-own')], ['medium'])
        self.assertIsNone(rows[('plan', 'nobody-1')])
        self.assertEqual(rows[('account:antigravity', 'gem-1-low')], ['low', 'high'])
        self.assertEqual(rows[('account:gemini-cli', 'gem-1')], ['low', 'high'])
        self.assertEqual(rows[('account:claude', 'claude-x')], ['low', 'max'])

    def test_the_fixture_regenerates_byte_for_byte(self):
        """Rule (round 4): the same checkout gives the same bytes; rows are sorted and one per line."""
        with tempfile.TemporaryDirectory() as tmp:
            src = tree(Path(tmp, 'src'))
            out_a, out_b = Path(tmp, 'a.json'), Path(tmp, 'b.json')
            names_a, names_b = Path(tmp, 'na.json'), Path(tmp, 'nb.json')
            makers = makers_file(tmp)
            for out, names in ((out_a, names_a), (out_b, names_b)):
                mc.main([str(src), '--version', '0.0.1', '--commit', 'abc', '--out', str(out), '--names-out', str(names),
                         '--makers', str(makers)])
            self.assertEqual(out_a.read_bytes(), out_b.read_bytes())
            self.assertEqual(names_a.read_bytes(), names_b.read_bytes())
            doc = json.loads(out_a.read_text(encoding='utf-8'))
            keys = [(r['provider'], r['model']) for r in doc['rows']]
            self.assertEqual(keys, sorted(keys))
            lines = out_a.read_text(encoding='utf-8').splitlines()
            self.assertEqual(sum(1 for x in lines if x.startswith('  {"provider": ')), len(doc['rows']))
            self.assertEqual((doc['schema'], doc['provider_kind'], doc['source']['commit']),
                             ('agent-compass.gateway-catalog/3', 'magpie_preset', 'abc'))

    def test_a_rule_the_source_no_longer_has_stops_the_reader(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                mc.build(tree(tmp, catalog=CATALOG.replace('reasoningOffProviders', 'offProviders')), '0.0.1', 'abc',
                         makers_file(tmp))

    def test_the_shipped_fixture_is_this_readers_shape(self):
        for path in sorted(mc.OUT_DIR.glob('magpie-*.json')):
            doc = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(path.read_text(encoding='utf-8'), mc.dumps(doc), path.name)
            self.assertEqual(path.name, f"magpie-{doc['version']}.json")
            self.assertTrue(doc['source']['commit'])
        names = json.loads(mc.NAMES.read_text(encoding='utf-8'))
        self.assertEqual(mc.NAMES.read_text(encoding='utf-8'), mc.dumps_names(names))


if __name__ == '__main__':
    unittest.main()
