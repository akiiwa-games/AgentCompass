"""Round 058 (user 2026-11-10: "后续同系列新出，缺少实测数据的情况下，先按照同系列的全分数进行折算，不要留下任何一个默认值或者
fallback"): legacy-path scores and usd_per_hour with no direct measurement are filled by calibration/apply_rows.py
`estimate`: the series conversion from the nearest same-series sibling with a direct value, else (no sibling) a
public-data estimate where the fit shows out-of-sample skill. Either yields to any value it did not write."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'calibration'))
import apply_rows  # noqa: E402
import five_axis  # noqa: E402
import series  # noqa: E402
import tiers  # noqa: E402
import auto_bundles  # noqa: E402

REFERENCES = Path(__file__).resolve().parents[1] / 'references'
GENERIC = {'tier': 2.8, 'capabilities': {'reasoning': 0.55, 'coding': 0.55, 'exploration': 0.55, 'anti_hallucination': 0.5,
                                         'visual': 0.0, 'gui': 0.0},
           'subjective': {'divergence': 0.5, 'global_rigor': 0.5, 'module_precision': 0.5}, 'basis': 'generic default'}
SMALL = {'tier': 2.8, 'capabilities': {'reasoning': 0.45, 'coding': 0.45, 'exploration': 0.45, 'anti_hallucination': 0.4,
                                       'visual': 0.0, 'gui': 0.0},
         'subjective': {'divergence': 0.4, 'global_rigor': 0.4, 'module_precision': 0.4}, 'basis': 'small default'}
NAMES = ('ash', 'birch', 'cedar', 'dogwood', 'elm', 'fir', 'ginkgo', 'hazel', 'ilex', 'juniper')
NOISE = (0.5, 0.9, 0.4, 0.8, 0.45, 0.85, 0.5, 0.9, 0.55, 0.95)       # no relation to the index: a fit with no skill


def index_of(i):
    return 20 + 4 * i


def measured_row(i):
    """A row carrying its own values: every score linear in the AA index, except global_rigor (noise)."""
    x = index_of(i)
    caps = {d: round(0.2 + 0.01 * x, 3) for d in apply_rows.ESTIMATED['capabilities']}
    sub = {d: round(0.1 + 0.012 * x, 3) for d in apply_rows.ESTIMATED['subjective']}
    sub['global_rigor'] = NOISE[i]
    return {'tier': 3.0, 'capabilities': {**caps, 'visual': 0.5, 'gui': 0.4}, 'subjective': sub}


def reading(x, **extra):
    return [{'source': 'aa.index', 'value': x, 'as_of': '2026-11-09', **extra}]


def setup(**targets):
    """(accounts, measurements) with the ten measured rows and `targets` {name: AA index or None}, each a generic row."""
    models = {name: measured_row(i) for i, name in enumerate(NAMES)}
    meas = {'models': {name: {'intelligence_index': reading(index_of(i))} for i, name in enumerate(NAMES)}}
    for name, x in targets.items():
        models[name] = copy.deepcopy({k: v for k, v in GENERIC.items() if k != 'basis'})
        if x is not None:
            meas['models'][name] = {'intelligence_index': reading(x, listed_as=f'{name} (High)')}
    return {'models': models, 'aliases': {}, 'unmeasured_models': {'generic': GENERIC, 'small': SMALL}}, meas


def run(acc, meas, measured=None):
    models = copy.deepcopy(acc['models'])
    fits = apply_rows.estimate(models, meas, acc, measured)
    return models, fits


class EstimateTests(unittest.TestCase):
    def test_a_prior_is_replaced_only_where_the_fit_shows_out_of_sample_skill(self):
        acc, meas = setup(zephyr=32)
        models, fits = run(acc, meas)
        row = models['zephyr']
        _, check = fits[('capabilities.reasoning', 'aa.index')]
        self.assertTrue(check['skill'] and check['beats_mean'] and check['beats_prior'])
        self.assertAlmostEqual(row['capabilities']['reasoning'], 0.2 + 0.01 * 32, places=2)
        est = row['score_estimates']['capabilities.reasoning']
        self.assertEqual((est['predictor'], est['x'], est['prior']), ('aa.index', 32, 0.55))
        self.assertEqual(est['input'], {'source': 'aa.index', 'as_of': '2026-11-09', 'listed_as': 'zephyr (High)'})
        self.assertEqual(est['fit']['n'], len(NAMES))             # the target itself is not a point
        self.assertNotIn('x_clamped', est)
        # global_rigor is noise against the index: its fit passes the five-axis rules but not the skill check.
        fit, check = fits[('subjective.global_rigor', 'aa.index')]
        self.assertIsNotNone(fit)
        self.assertFalse(check['skill'])
        self.assertEqual(row['subjective']['global_rigor'], 0.5)
        self.assertNotIn('subjective.global_rigor', row['score_estimates'])
        # visual and gui admit a modality: never estimated. Rows with their own values are untouched.
        self.assertEqual((row['capabilities']['visual'], row['capabilities']['gui']), (0.0, 0.0))
        self.assertEqual({k: v for k, v in models.items() if k != 'zephyr'},
                         {k: v for k, v in acc['models'].items() if k != 'zephyr'})

    def test_a_second_run_changes_nothing(self):
        acc, meas = setup(zephyr=32)
        models, _ = run(acc, meas)
        again, _ = run(dict(acc, models=models), meas)
        self.assertEqual(again, models)

    def test_a_reading_outside_the_sample_is_clamped_not_extrapolated(self):
        acc, meas = setup(zephyr=100)
        models, _ = run(acc, meas)
        est = models['zephyr']['score_estimates']['capabilities.reasoning']
        top = index_of(len(NAMES) - 1)
        self.assertEqual((est['x'], est['x_clamped']), (100, top))
        self.assertAlmostEqual(est['value'], 0.2 + 0.01 * top, places=2)

    def test_without_an_aa_reading_the_prior_stays_and_tier_predicts_nothing(self):
        acc, meas = setup(zephyr=None)
        acc['models']['zephyr']['tier'] = 4.5
        models, fits = run(acc, meas)
        self.assertEqual(models['zephyr'], acc['models']['zephyr'])
        self.assertNotIn('tier', {name for _, name in fits})

    def test_an_edited_value_wins_over_the_estimate(self):
        """Review F1: a value the estimate did not write (hand-edited or measured) is never overwritten."""
        acc, meas = setup(zephyr=32)
        models, _ = run(acc, meas)
        models['zephyr']['capabilities']['reasoning'] = 0.95       # edited, the record left in place
        after, _ = run(dict(acc, models=models), meas)
        self.assertEqual(after['zephyr']['capabilities']['reasoning'], 0.95)
        self.assertNotIn('capabilities.reasoning', after['zephyr']['score_estimates'])
        self.assertIn('capabilities.coding', after['zephyr']['score_estimates'])

    def test_an_estimate_that_loses_its_skill_reverts_to_its_prior(self):
        acc, meas = setup(zephyr=32)
        models, _ = run(acc, meas)
        self.assertIn('capabilities.reasoning', models['zephyr']['score_estimates'])
        for i, name in enumerate(NAMES):                           # new data: reasoning no longer follows the index
            models[name]['capabilities']['reasoning'] = NOISE[i]
        after, _ = run(dict(acc, models=models), meas)
        self.assertEqual(after['zephyr']['capabilities']['reasoning'], 0.55)
        self.assertNotIn('capabilities.reasoning', after['zephyr'].get('score_estimates') or {})


def series_setup(reasoning=0.5):
    """The ten measured rows, claude-fable-5-1 (measured, AA index 40) and claude-fable-5-2 (the generic row, index 60):
    one series by the router's family resolution."""
    acc, meas = setup(**{'claude-fable-5-2': 60})
    base = measured_row(5)
    base['capabilities']['reasoning'] = reasoning
    base['subjective']['divergence'] = 0.9
    acc['models']['claude-fable-5-1'] = base
    meas['models']['claude-fable-5-1'] = {'intelligence_index': reading(40)}
    return acc, meas


class SeriesTests(unittest.TestCase):
    def test_series_membership_comes_from_the_family_resolution(self):
        acc = json.loads((REFERENCES / 'accounts.json').read_text(encoding='utf-8'))
        groups = series.groups(acc)
        self.assertIn('claude-fable-5-1', groups['claude-fable-5-2'])
        self.assertIn('claude-opus-5-5', groups['claude-opus-4-7'])
        self.assertEqual(groups['gpt-oss-120b'], [])                       # no family: the estimate path
        # A small-size name is not in the series of a non-small row (qwen3-flash vs the Qwen Max line).
        self.assertNotIn('qwen3-flash-2025-07-28', groups['qwen3.8-max'])
        acc0, _ = setup(zephyr=None)
        self.assertEqual(series.groups(acc0)['zephyr'], [])

    def test_the_base_is_the_nearest_sibling_on_a_shared_reading_and_the_tier_only_without_one(self):
        index, tier = {'t': 50, 'a': 40, 'b': 58}, {'t': 3.0, 'a': 3.1, 'b': 2.0, 'c': 3.05}
        self.assertEqual(series.nearest('t', ['a', 'b', 'c'], index, {}, tier), ['b', 'a', 'c'])
        # No index: the cited boards both rows have (mean absolute difference) come before any tier distance.
        board = {'t': {'x': 0.6, 'y': 0.5}, 'a': {'x': 0.62, 'y': 0.4}, 'b': {'y': 0.52}, 'c': {'z': 0.6}}
        self.assertEqual(series.nearest('t', ['a', 'b', 'c'], {}, board, tier), ['b', 'a', 'c'])
        self.assertEqual(series.nearest('t', ['a', 'b', 'c'], {}, {}, tier), ['c', 'a', 'b'])
        self.assertEqual(series.nearest('t', ['b', 'a'], {}, {}, {}), ['a', 'b'])
        meas = {'axes': {'divergence': ['chat']}, 'models': {'m': {
            'divergence': [{'source': 's1', 'value': 0.5}, {'source': 's1', 'value': 0.9}, {'source': 's2', 'value': 'n/a'}],
            'tier': [{'source': 'fit', 'value': 2.0}]}}}
        self.assertEqual(series.boards(meas), {'m': {'s1': 0.5}})

    def test_a_modality_follows_the_sibling_sharing_a_reading_not_the_nearest_tier(self):
        # qwen3-max / 3.6-plus / 3.7-max have no AA index; qwen3.8-max shares LMArena boards with them, the text-only
        # qwen3-coder-next (nearest on tier) shares none, so visual and gui come from qwen3.8-max.
        acc = json.loads((REFERENCES / 'accounts.json').read_text(encoding='utf-8'))
        for key in ('qwen3-max', 'qwen3.6-plus', 'qwen3.7-max'):
            est = acc['models'][key]['score_estimates']
            for dim in ('visual', 'gui'):
                self.assertEqual(est[f'capabilities.{dim}']['base_model'], 'qwen3.8-max', (key, dim))
                self.assertGreater(acc['models'][key]['capabilities'][dim], 0, (key, dim))

    def test_every_score_without_a_measurement_is_converted_from_the_sibling_within_the_bounds(self):
        acc, meas = series_setup()
        models, fits = run(acc, meas)
        row, base = models['claude-fable-5-2'], acc['models']['claude-fable-5-1']
        est = row['score_estimates']
        for group in ('capabilities', 'subjective'):
            for dim in row[group]:
                e = est[f'{group}.{dim}']
                self.assertEqual((e['base_model'], e['base_value'], e['prior']),
                                 ('claude-fable-5-1', base[group][dim], GENERIC[group][dim]))
        # reasoning: 0.5 + b (60 - 40) inside the measured range; visual and gui are the sibling's values.
        f, _ = fits[('capabilities.reasoning', 'aa.index')]
        r = est['capabilities.reasoning']
        self.assertEqual((r['x'], r['x_base'], r['predictor']), (60, 40, 'aa.index'))
        self.assertEqual(row['capabilities']['reasoning'], round(0.5 + f['b'] * 20, 2))
        self.assertNotIn('y_clamped', r)
        self.assertEqual((row['capabilities']['visual'], row['capabilities']['gui']), (0.5, 0.4))
        self.assertIsNone(est['capabilities.visual']['predictor'])
        # divergence: 0.9 + b (60 - 40) is above every measured value, so it is kept at the top of the range.
        d = est['subjective.divergence']
        self.assertEqual(row['subjective']['divergence'], round(d['fit']['y_max'], 2))
        self.assertEqual(d['y_clamped'], d['fit']['y_max'])
        again, _ = run(dict(acc, models=models), meas)
        self.assertEqual(again, models)

    def test_usd_per_hour_is_the_sibling_value_times_the_list_price_ratio(self):
        acc, meas = series_setup()
        acc['models']['claude-fable-5-1']['usd_per_hour'] = 20.0
        meas['capacity'] = {'token_mix': {'input': 0.6, 'output': 0.1, 'cache_read': 0.3, 'note': 'shares'}}
        meas['models']['claude-fable-5-1']['price'] = [{'source': 'aa.price', 'value': {'input': 10, 'output': 50, 'cache_read': 1}}]
        meas['models']['claude-fable-5-2']['price'] = [{'source': 'aa.price', 'value': {'input': 8, 'output': 40, 'cache_read': 0.8}}]
        models, _ = run(acc, meas)
        e = models['claude-fable-5-2']['score_estimates']['usd_per_hour']
        self.assertEqual((models['claude-fable-5-2']['usd_per_hour'], e['ratio'], e['base_model']), (16.0, 0.8, 'claude-fable-5-1'))
        self.assertIn('claude-fable-5-1', models['claude-fable-5-2']['usd_per_hour_basis'])

    def test_a_direct_measurement_replaces_the_converted_value_and_an_edit_is_never_overwritten(self):
        acc, meas = series_setup()
        models, _ = run(acc, meas)
        row = models['claude-fable-5-2']
        row['capabilities']['reasoning'] = 0.95                 # hand-edited
        measured = {'claude-fable-5-2': {'capabilities.coding'}}
        row['capabilities']['coding'] = 0.61                     # measured (WebDev) by `rows`
        after, _ = run(dict(acc, models=models), meas, measured)
        a = after['claude-fable-5-2']
        self.assertEqual((a['capabilities']['reasoning'], a['capabilities']['coding']), (0.95, 0.61))
        self.assertNotIn('capabilities.reasoning', a['score_estimates'])
        self.assertNotIn('capabilities.coding', a['score_estimates'])
        self.assertIn('capabilities.exploration', a['score_estimates'])

    def test_a_new_series_model_has_no_default_or_fallback_anywhere(self):
        real = json.loads((REFERENCES / 'accounts.json').read_text(encoding='utf-8'))
        generic, small = real['unmeasured_models']['generic'], real['unmeasured_models'].get('small') or {}
        # Discovered at runtime: the same-series row's scores and price, no shrink and no conservative price.
        profile = auto_bundles.default_profile('claude-fable-5-9', real)
        self.assertEqual((profile['source'], profile['target']), ('family', 'claude-fable-5-2'))
        target = real['models']['claude-fable-5-2']
        for field in ('tier', 'capabilities', 'subjective', 'usd_per_hour'):
            self.assertEqual(profile['spec'][field], target[field], field)
        import allocator
        spec = allocator.model_spec(real, 'claude-fable-5-9')
        self.assertEqual(spec['usd_per_hour'], target['usd_per_hour'])
        self.assertNotIn('usd_estimated', spec)
        # Added as a row by the calibration: every score and usd_per_hour is converted from a measured sibling.
        acc = copy.deepcopy(real)
        start = auto_bundles.default_profile('claude-fable-5-9', apply_rows.without_estimates(real))['spec']   # as `rows`
        acc['models']['claude-fable-5-9'] = {k: v for k, v in start.items() if k != 'basis'}
        meas = json.loads((REFERENCES / 'public-measurements.json').read_text(encoding='utf-8'))
        meas['models']['claude-fable-5-9'] = copy.deepcopy(meas['models']['claude-fable-5-2'])
        models = copy.deepcopy(acc['models'])
        apply_rows.estimate(models, meas, acc)
        row = models['claude-fable-5-9']
        paths = [f'{g}.{d}' for g, dims in apply_rows.SCORES.items() for d in dims] + ['usd_per_hour']
        self.assertEqual(sorted(row['score_estimates']), sorted(paths))
        for path, e in row['score_estimates'].items():
            self.assertEqual(e['base_model'], 'claude-fable-5-1', path)    # the measured sibling, not the converted 5.2
        self.assertNotEqual(row['capabilities'], generic['capabilities'])
        self.assertNotEqual(row['subjective'], small.get('subjective'))


class FakeFitter:
    """The tiers.Fitter surface rows() uses, with the six anchors and one model on WebDev."""
    def __init__(self, models, listed):
        self.models = models
        self.boards = {'webdev': {name: (1400 + 60 * i, 0, 0, name, '2026-11-10')
                                  for i, name in enumerate(tiers.ANCHORS.values())}}
        self.listed = listed

    def fit(self, key):
        if key not in self.listed:
            return {'id': key, 'sources': [], 'fits': {}, 'method': 'unmeasured'}
        row = (1700, 1690, 1710, key, '2026-11-10')
        return {'id': key, 'sources': [('webdev', row)], 'fits': {'webdev': 4.0}, 'tier': 4.0, 'method': 'primary'}


class FakeEfforts:
    def high_tier(self, key, tier):
        return tier, {}


class TakeoverTests(unittest.TestCase):
    def test_a_webdev_listing_replaces_the_estimated_coding_on_the_next_run(self):
        acc, meas = setup(zephyr=32)
        for i, key in enumerate(tiers.ANCHORS):
            acc['models'][key] = measured_row(i)
        models, _ = run(acc, meas)
        self.assertIn('capabilities.coding', models['zephyr']['score_estimates'])
        later = dict(acc, models=copy.deepcopy(models))
        measured = {}
        new = apply_rows.rows(FakeFitter(later['models'], {'zephyr'}), ['zephyr'], later, FakeEfforts(), measured)
        self.assertEqual(measured, {'zephyr': {'capabilities.coding'}})
        coding = new['zephyr']['capabilities']['coding']
        self.assertNotIn('capabilities.coding', new['zephyr']['score_estimates'])
        later['models'].update(new)
        after, _ = run(later, meas, measured)
        self.assertEqual(after['zephyr']['capabilities']['coding'], coding)
        self.assertNotIn('capabilities.coding', after['zephyr']['score_estimates'])
        self.assertIn('capabilities.reasoning', after['zephyr']['score_estimates'])

    def test_refresh_runs_the_estimate_step_on_the_rebuilt_measurements(self):
        import refresh
        meas = {'models': {}}
        with patch.object(refresh.sources, 'data_dir', return_value=Path('.')), \
                patch.object(refresh, 'rebuild', return_value=(meas, {'source_ages': {}})), \
                patch.object(refresh.apply_rows, 'refresh_estimates', return_value=({}, {}, {})) as step, \
                patch.object(sys, 'argv', ['refresh.py', '--no-fetch']):
            refresh.main()
        step.assert_called_once_with(meas)


class ShippedTests(unittest.TestCase):
    def test_shipped_values_are_current_converted_from_siblings_or_skilled(self):
        acc = json.loads((REFERENCES / 'accounts.json').read_text(encoding='utf-8'))
        meas = json.loads((REFERENCES / 'public-measurements.json').read_text(encoding='utf-8'))
        groups = series.groups(acc)
        estimated = {k: r for k, r in acc['models'].items() if isinstance(r, dict) and r.get('score_estimates')}
        self.assertIn('claude-fable-5-2', estimated)
        for key, row in estimated.items():
            for path, est in row['score_estimates'].items():
                self.assertEqual(apply_rows.get(row, path), est['value'], (key, path))
                if 'base_model' in est:                          # series conversion
                    self.assertIn(est['base_model'], groups[key], (key, path))
                    self.assertNotIn(path, acc['models'][est['base_model']].get('score_estimates') or {}, (key, path))
                    if est.get('fit'):
                        self.assertTrue(est['fit']['y_min'] <= est['value'] <= est['fit']['y_max'], (key, path))
                    continue
                self.assertEqual(groups[key], [], (key, path))   # an estimate only for a model with no sibling
                self.assertIn(est['predictor'], apply_rows.PREDICTORS[path])
                self.assertTrue(est['skill']['skill'], (key, path))
                self.assertGreaterEqual(est['fit']['n'], five_axis.MIN_FIT_POINTS)
                self.assertTrue(est['fit']['x_min'] <= est.get('x_clamped', est['x']) <= est['fit']['x_max'])
        models = copy.deepcopy(acc['models'])
        apply_rows.estimate(models, meas, acc)
        self.assertEqual(models, acc['models'])                    # what a calibration run produces now

    def test_fixture_rows_take_the_same_estimates(self):
        acc = json.loads((REFERENCES / 'accounts.json').read_text(encoding='utf-8'))
        fixture = json.loads((Path(__file__).resolve().parent / 'fixtures' / 'reference-accounts.json').read_text(encoding='utf-8'))
        for key, row in acc['models'].items():
            if isinstance(row, dict) and key in fixture['models']:
                self.assertEqual(fixture['models'][key].get('score_estimates'), row.get('score_estimates'), key)


if __name__ == '__main__':
    unittest.main()
