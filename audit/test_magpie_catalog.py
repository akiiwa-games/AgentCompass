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


def model(mid, efforts=None, toggle=False, out=('text',)):
    opts = ([{'type': 'toggle'}] if toggle else []) + ([{'type': 'effort', 'values': list(efforts)}] if efforts else [])
    return {'id': mid, 'reasoning': bool(efforts or toggle), 'reasoning_options': opts, 'modalities': {'output': list(out)}}


SNAPSHOT = {
    'maker': {'models': {'m-1': model('m-1', ['low', 'high'], toggle=True), 'maker-own': model('maker-own', ['medium']),
                         'm-embed': model('m-embed')}},
    'google': {'models': {'gem-1': model('gem-1', ['low', 'high'])}},
    'cloud': {'models': {'m-1': model('m-1', ['low', 'high'], toggle=True), 'c-2': model('c-2')}},
    'router': {'models': {'r-3': model('r-3', ['high'])}},
    'anthropic': {'models': {'claude-x': model('claude-x', ['low', 'max'])}},
}


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

    def test_the_gate_is_magpies_makers_read_from_the_presets_kind_and_hosts(self):
        """Rule (round 4): the first-party tier is Magpie's own `makerCatalogs`, presets of Kind vendor without Hosts, plus
        the accounts whose list the source fixes; it is read from the source, so flipping Hosts moves a preset out."""
        with tempfile.TemporaryDirectory() as tmp:
            doc = mc.build(tree(tmp), '0.0.1', 'abc')
            self.assertEqual(doc['gate']['providers'],
                             ['google', 'maker', 'plan', 'account:antigravity', 'account:claude', 'account:gemini-cli'])
            self.assertEqual({r['provider'] for r in doc['rows']} - set(doc['gate']['providers']), {'cloud', 'router'})
            flipped = PRESETS.replace('{ID: "maker", Kind: KindVendor,', '{ID: "maker", Kind: KindVendor, Hosts: true,')
            doc = mc.build(tree(tmp, presets=flipped), '0.0.1', 'abc')
            self.assertNotIn('maker', doc['gate']['providers'])

    def test_rows_and_efforts_follow_magpies_own_rules(self):
        """Magpie's rules, as its source states them: `none` first for a model with a thinking switch at a provider of
        reasoningOffProviders; a non-text model left out; a model the provider has no word on borrows its maker's efforts,
        through a level suffix; no word anywhere is null (the model's one configuration)."""
        with tempfile.TemporaryDirectory() as tmp:
            rows = {(r['provider'], r['model']): r['efforts'] for r in mc.build(tree(tmp), '0.0.1', 'abc')['rows']}
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
            mc.main([str(src), '--version', '0.0.1', '--commit', 'abc', '--out', str(out_a)])
            mc.main([str(src), '--version', '0.0.1', '--commit', 'abc', '--out', str(out_b)])
            self.assertEqual(out_a.read_bytes(), out_b.read_bytes())
            doc = json.loads(out_a.read_text(encoding='utf-8'))
            keys = [(r['provider'], r['model']) for r in doc['rows']]
            self.assertEqual(keys, sorted(keys))
            lines = out_a.read_text(encoding='utf-8').splitlines()
            self.assertEqual(sum(1 for x in lines if x.startswith('  {"provider": ')), len(doc['rows']))
            self.assertEqual((doc['schema'], doc['provider_kind'], doc['source']['commit']),
                             ('agent-compass.gateway-catalog/2', 'magpie_preset', 'abc'))

    def test_a_rule_the_source_no_longer_has_stops_the_reader(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                mc.build(tree(tmp, catalog=CATALOG.replace('reasoningOffProviders', 'offProviders')), '0.0.1', 'abc')

    def test_the_shipped_fixture_is_this_readers_shape(self):
        for path in sorted(mc.OUT_DIR.glob('magpie-*.json')):
            doc = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(path.read_text(encoding='utf-8'), mc.dumps(doc), path.name)
            self.assertEqual(path.name, f"magpie-{doc['version']}.json")
            self.assertTrue(doc['source']['commit'])


if __name__ == '__main__':
    unittest.main()
