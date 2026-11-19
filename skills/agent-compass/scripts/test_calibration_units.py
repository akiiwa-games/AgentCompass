"""Direct tests of the calibration helpers the five-axis scores rest on: series.py (same-series siblings and the base a
converted model takes), tiers.py (the tier is read off arena.ai Overall and has one use, estimating a missing type score)
and the usd series conversion in five_axis.py."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'calibration'))

import five_axis as fa  # noqa: E402
import measurements  # noqa: E402
import series  # noqa: E402
import sources  # noqa: E402
import tiers  # noqa: E402

# accounts.json's declarations the build reads (sources.effort_readings, five_axis.estimation_order)
READINGS = {'effort_readings': {'labels': {'non-reasoning': 'none', 'reasoning': 'default'}, 'reads': {'default': 'high'}},
            'estimation_order': {'order': list(fa.ESTIMATION_ORDER), 'sibling_first_up_to_distance': 1,
                                 'evidence': 'synthetic'}}


class SeriesTests(unittest.TestCase):
    ACC = {'models': {m: {} for m in ('gpt-6-luna', 'gpt-5.6-luna', 'gpt-6-astra', 'dot', 'claude-opus-5-5')},
           'aliases': {}, 'families': {'rows': []},
           'superseded': {'gpt-5.6-luna': {'by': 'gpt-6-luna'}},
           'same_model': {'dot': {'model': 'gpt-6-astra', 'carries': ['creativity'], 'url': 'https://example.test/dot'}}}

    def test_same_model_returns_only_the_declarations_that_are_objects(self):
        self.assertEqual(set(series.same_model(self.ACC)), {'dot'})
        self.assertEqual(series.same_model({'same_model': {'dot': 'gpt-6-astra'}}), {})
        self.assertEqual(series.same_model({}), {})

    def test_superseded_pairs_and_same_model_rows_join_a_series(self):
        groups = series.groups(self.ACC)
        self.assertEqual(groups['gpt-6-luna'], ['gpt-5.6-luna'])
        self.assertEqual(groups['gpt-5.6-luna'], ['gpt-6-luna'])
        self.assertEqual(groups['dot'], ['gpt-6-astra'])
        self.assertIn('dot', groups['gpt-6-astra'])
        self.assertNotIn('claude-opus-5-5', groups['dot'] + groups['gpt-6-luna'])

    def test_a_same_model_row_to_a_row_that_is_not_scored_joins_nothing(self):
        acc = dict(self.ACC, same_model={'dot': {'model': 'not-a-row'}})
        self.assertNotIn('not-a-row', series.groups(acc)['dot'])

    def test_boards_keep_the_first_numeric_reading_per_source_on_the_component_dimensions(self):
        meas = {'axes': {'creativity': {}, 'coding': {}},
                'models': {'m': {'creativity': [{'source': 'a.x', 'value': 0.5}, {'source': 'a.x', 'value': 0.9},
                                                {'source': 'a.bad', 'value': True}, {'source': 'a.str', 'value': 'high'}],
                                 'coding': [{'source': 'b.y', 'value': 0.3}],
                                 'other_field': [{'source': 'c.z', 'value': 1.0}]},
                           'n': {}}}
        self.assertEqual(series.boards(meas), {'m': {'a.x': 0.5, 'b.y': 0.3}, 'n': {}})
        self.assertEqual(series.boards({}), {})

    def test_nearest_orders_by_the_declared_model_then_version_distance_then_size(self):
        """Rule (series.py, round 5 review D): the declared same_model first, then the declared version distance, then
        size equality; a candidate without a declared version ranks after every declared distance."""
        same = {'t': {'model': 'declared'}}
        versions = {'t': 3.0, 'declared': 1.0, 'v2': 2.0, 'v2-mini': 2.0, 'v1': 1.0, 'v3': 3.0}
        series_rows = ['declared', 'v2', 'v2-mini', 'v1', 'v3', 'blank']
        small = {'v2-mini'}
        order = series.nearest('t', ['v1', 'blank', 'v2-mini', 'v2', 'declared', 'v3'], series_rows, versions, small, same)
        self.assertEqual(order, [(0, ['declared']), (0, ['v3']), (1, ['v2']), (1, ['v2-mini']), (2, ['v1']),
                                 (None, ['blank'])])
        # without the declaration the declared model is one more relative at its version distance
        self.assertEqual(series.nearest('t', ['declared', 'v1'], series_rows, versions, small)[0], (2, ['declared', 'v1']))

    def test_tied_relatives_form_one_group_and_no_name_breaks_the_tie(self):
        """Rule (series.py): relatives at one distance and one size are one group (the caller takes their median);
        their names never order them."""
        versions = {'t': 2.0, 'a': 1.0, 'b': 3.0}
        for candidates in (['a', 'b'], ['b', 'a']):
            self.assertEqual(series.nearest('t', candidates, ['a', 'b'], versions, set()), [(1, ['a', 'b'])])

    def test_a_row_that_is_not_the_nearest_relative_changes_no_estimate(self):
        """Stability (round 5 review D): adding a row that is not the nearest relative of X by the declared distance
        leaves X's nearest group as it was."""
        versions = {'x': 5.0, 'near': 4.0, 'old': 2.0}
        before = series.nearest('x', ['near', 'old'], ['near', 'old'], versions, set())[0]
        for added, version in (('older', 1.0), ('zzz', 3.0), ('aaa', 3.0)):
            v = {**versions, added: version}
            rows = ['near', 'old', added]
            self.assertEqual(series.nearest('x', ['near', 'old', added], rows, v, set())[0], before, added)

    def test_the_version_distance_counts_distinct_declared_versions_between_the_two(self):
        versions = {'t': 3.0, 'a': 3.0, 'b': 2.0, 'c': 2.0, 'd': 1.0}
        rows = ['a', 'b', 'c', 'd', 'e']
        self.assertEqual([series.distance('t', x, rows, versions) for x in rows], [0, 1, 1, 2, None])


class PooledAndInterpolationTests(unittest.TestCase):
    def test_pooled_averages_ties_and_pools_an_inversion(self):
        xs, ys = tiers.pooled([1, 2, 2, 3, 4], [1.0, 2.0, 4.0, 2.0, 5.0])
        self.assertEqual(xs, [1.0, 2.0, 3.0, 4.0])
        self.assertAlmostEqual(ys[1], 8 / 3)            # x=2 averages 3.0 (two readings), x=3 reads 2.0: an inversion, pooled by weight
        self.assertEqual(ys[1], ys[2])
        self.assertEqual((ys[0], ys[3]), (1.0, 5.0))
        self.assertEqual(ys, sorted(ys))

    def test_interpolation_runs_between_points_and_by_the_end_to_end_slope_beyond_them(self):
        f = tiers.Interpolation([10, 20, 40], [2.0, 3.0, 5.0])
        self.assertAlmostEqual(f(15), 2.5)
        self.assertAlmostEqual(f(30), 4.0)
        slope = (5.0 - 2.0) / 30
        self.assertAlmostEqual(f.slope, slope)
        self.assertAlmostEqual(f(50), 5.0 + slope * 10)
        self.assertAlmostEqual(f(5), 2.0 - slope * 5)

    def test_optional_floor_and_one_point_is_no_map(self):
        unfloored = tiers.Interpolation([10, 20], [-0.5, 0.5])
        self.assertAlmostEqual(unfloored(0), -1.5)
        floored = tiers.Interpolation([10, 20], [1.2, 2.0], floor=1.0)
        self.assertEqual(floored(-1000), 1.0)
        with self.assertRaises(ValueError):
            tiers.Interpolation([5, 5], [2.0, 2.0])(5)

    def test_neighbours_name_the_two_points_a_reading_lies_between_or_the_end_segment(self):
        f = tiers.Interpolation([10, 20, 40], [2.0, 3.0, 5.0], ['a', 'b', 'c'])
        low, high = f.neighbours(30)
        self.assertEqual((low['models'], high['models']), (['b'], ['c']))
        low, high = f.neighbours(99)
        self.assertEqual((low['models'], high['models']), (['b'], ['c']))        # beyond the top: the end segment
        low, high = f.neighbours(1)
        self.assertEqual((low['models'], high['models']), (['a'], ['b']))

    def test_spread_is_the_rms_around_the_map_in_sample_and_leaving_each_model_out(self):
        """Rule (tiers.Interpolation.spread): residual_sd is the root mean square of the shared models' tiers around the
        map, loo_rmse the same with each model left out of the map that predicts it."""
        f = tiers.Interpolation([10, 20, 30, 40], [1.0, 3.0, 2.0, 4.0])
        res = [y - f(x) for x, y in f.raw]
        loo = []
        for i, (x, y) in enumerate(f.raw):
            rest = f.raw[:i] + f.raw[i + 1:]
            loo.append(y - tiers.Interpolation([p[0] for p in rest], [p[1] for p in rest])(x))
        rms = lambda v: round(sum(e * e for e in v) / len(v), 12) ** 0.5     # noqa: E731
        got = f.spread()
        self.assertAlmostEqual(got['residual_sd'], rms(res), places=4)
        self.assertAlmostEqual(got['loo_rmse'], rms(loo), places=4)
        self.assertGreater(got['loo_rmse'], got['residual_sd'])
        self.assertEqual(tiers.Interpolation([10, 20], [1.0, 2.0]).spread()['loo_rmse'], None)    # one point left: no map

    def test_norm_keeps_letters_and_digits_only(self):
        self.assertEqual(tiers.norm('Claude Opus-5.5 (high)'), 'claudeopus55high')


class FitterTests(unittest.TestCase):
    @staticmethod
    def board():
        """An arena Overall @High board centered on Claude Opus 5.5, with distinct Elo values."""
        return {
            'Claude Opus 5.5': (1500.0, 1495.0, 1505.0, 'Claude Opus 5.5 (High)', '2026-11-09'),
            'GPT-6.1 Sol': (1480.0, 1475.0, 1485.0, 'GPT-6.1 Sol (High)', '2026-11-09'),
            'Claude Sonnet 5.5': (1470.0, 1465.0, 1475.0, 'Claude Sonnet 5.5 (High)', '2026-11-09'),
            'GPT-6 Astra': (1460.0, 1455.0, 1465.0, 'GPT-6 Astra (High)', '2026-11-09'),
            'Grok 4.7': (1440.0, 1435.0, 1445.0, 'Grok 4.7 (High)', '2026-11-09'),
        }

    def fitter(self):
        f = tiers.Fitter.__new__(tiers.Fitter)
        f.aa_read = '2026-11-09'
        f.overall = self.board()
        f.overall.update({
            'Model A': (1410.0, 1405.0, 1415.0, 'Model A', '2026-11-09'),
            'Model B': (1450.0, 1445.0, 1455.0, 'Model B', '2026-11-09'),
            'Model C': (1490.0, 1485.0, 1495.0, 'Model C', '2026-11-09'),
            'Model D': (1520.0, 1515.0, 1525.0, 'Model D', '2026-11-09'),
        })
        f.anchor_name = 'Claude Opus 5.5'
        f.anchor_raw, f.sd = f.overall_scale(f.overall, f.anchor_name)
        f.aa = {'Model A': 40.0, 'Model B': 50.0, 'Model C': 60.0, 'Model D': 70.0, 'Only AA': 55.0}
        f.maps = {'aa.index': f.board_map(f.aa)}
        return f

    def test_arena_overall_is_centered_on_opus_at_high_with_no_hand_set_anchor_tiers(self):
        self.assertFalse(hasattr(tiers, 'ANCHOR_TIERS'))
        f = self.fitter()
        self.assertEqual(f.arena_overall_tier('Claude Opus 5.5'), 0.0)
        # Distinct Elo rows keep strictly distinct continuous standard-score tiers (never flattened into a PAV plateau)
        ordered = ['Grok 4.7', 'GPT-6 Astra', 'Claude Sonnet 5.5', 'GPT-6.1 Sol', 'Claude Opus 5.5', 'Model D']
        vals = [f.arena_overall_tier(name) for name in ordered]
        self.assertEqual(vals, sorted(vals))
        self.assertEqual(len(set(round(v, 6) for v in vals)), len(vals))
        self.assertIsNone(f.arena_overall_tier('Unlisted'))

    def test_arena_high_rows_rejects_max_and_xhigh_substitutes(self):
        rows = [
            (1510.0, 1500.0, 1520.0, 'GPT-6 Sol (Max)', '2026-11-09'),
            (1495.0, 1490.0, 1500.0, 'Claude Opus 5.5 (High)', '2026-11-09'),
            (1525.0, 1520.0, 1530.0, 'Claude Opus 5.5 (Max)', '2026-11-09'),
            (1430.0, 1425.0, 1435.0, 'Single Config Model', '2026-11-09'),
        ]
        picked = tiers.arena_high_rows(rows, sources.effort_readings(READINGS))
        self.assertNotIn('GPT-6 Sol', picked)
        self.assertEqual(picked['Claude Opus 5.5'][0], 1495.0)
        self.assertEqual(picked['Single Config Model'][0], 1430.0)

    def test_a_board_map_is_the_listed_models_common_with_arena_overall_and_needs_three(self):
        f = self.fitter()
        mapping = f.maps['aa.index']
        self.assertEqual(mapping.n, 4)                          # 'Only AA' is not on arena Overall
        self.assertIsNone(f.board_map({'Model A': 40.0, 'Model B': 50.0}))

    def test_a_model_off_arena_takes_its_reading_from_the_other_overall_board_with_its_neighbours(self):
        f = self.fitter()
        out = f.other_overall({'aa.index': 'Only AA'})
        reading = out['aa.index']
        self.assertEqual((reading['listed_as'], reading['value'], reading['n']), ('Only AA', 55.0, 4))
        self.assertEqual((reading['neighbours']['below']['models'], reading['neighbours']['above']['models']),
                         (['Model B'], ['Model C']))
        self.assertAlmostEqual(reading['tier'], (f.arena_overall_tier('Model B') + f.arena_overall_tier('Model C')) / 2, places=3)
        self.assertEqual(reading['as_of'], '2026-11-09')
        self.assertEqual(f.other_overall({}), {})
        self.assertEqual(f.other_overall({'aa.index': 'Unlisted'}), {})


class UsdConversionTests(unittest.TestCase):
    PRICE = staticmethod(lambda i, o: {'value': {'input': i, 'output': o}})

    def test_list_price_ratio_is_the_ratio_of_the_mix_weighted_costs(self):
        mix = {'input': 0.5, 'output': 0.5}
        self.assertAlmostEqual(fa.list_price_ratio(self.PRICE(2.0, 10.0), self.PRICE(1.0, 5.0), mix), 2.0)
        self.assertAlmostEqual(fa.list_price_ratio(self.PRICE(1.0, 5.0), self.PRICE(2.0, 6.0), mix), 0.75)

    def test_list_price_ratio_is_none_without_a_mix_a_price_or_a_component(self):
        mix = {'input': 0.5, 'output': 0.5}
        self.assertIsNone(fa.list_price_ratio(self.PRICE(2.0, 10.0), self.PRICE(1.0, 5.0), {}))
        self.assertIsNone(fa.list_price_ratio(None, self.PRICE(1.0, 5.0), mix))
        self.assertIsNone(fa.list_price_ratio(self.PRICE(2.0, 10.0), None, mix))
        self.assertIsNone(fa.list_price_ratio({'value': {'input': 2.0}}, self.PRICE(1.0, 5.0), mix))
        self.assertIsNone(fa.list_price_ratio(self.PRICE(2.0, 10.0), self.PRICE(0.0, 0.0), mix))

    def test_a_model_without_a_usd_takes_the_nearest_siblings_times_the_price_ratio_and_cites_it(self):
        def entry(i, o):
            return {'source': 'aa.price', 'kind': 'vendor', 'value': {'input': i, 'output': o}, 'as_of': '2026-11-09',
                    'url': 'https://example.test/p', 'listed_as': 'x'}
        rows = {'gpt-5.6-luna': {'fields': {'price': [entry(1.0, 5.0)]}},
                'gpt-6-luna': {'fields': {'price': [entry(2.0, 10.0)]}},
                'claude-opus-5-5': {'fields': {'price': [entry(9.0, 9.0)]}}}
        usd = {'gpt-5.6-luna': {'value': 4.0, 'status': 'ok', 'kind': 'community'},
               'gpt-6-luna': {'value': None, 'status': 'missing', 'kind': 'none'},
               'claude-opus-5-5': {'value': 7.0, 'status': 'ok', 'kind': 'community'}}
        acc = {**READINGS, 'models': {m: {} for m in rows}, 'aliases': {}, 'families': {'rows': []}, 'superseded': {'gpt-5.6-luna': {'by': 'gpt-6-luna'}}}
        meas = {'capacity': {'token_mix': {'input': 0.5, 'output': 0.5}}, 'models': {m: {} for m in rows}}
        scorer = fa.Scorer(meas, acc)
        fa.convert_usd(usd, rows, scorer, meas, acc)
        got = usd['gpt-6-luna']
        self.assertEqual((got['value'], got['kind'], got['sources']['base']['model'], got['sources']['ratio']),
                         (8.0, 'converted', 'gpt-5.6-luna', 2.0))
        self.assertEqual(usd['gpt-5.6-luna']['value'], 4.0)                          # direct values are not touched
        self.assertEqual(usd['claude-opus-5-5']['value'], 7.0)

    def test_no_price_means_no_conversion_and_no_default(self):
        rows = {'gpt-5.6-luna': {'fields': {'price': []}}, 'gpt-6-luna': {'fields': {'price': []}}}
        usd = {'gpt-5.6-luna': {'value': 4.0, 'status': 'ok', 'kind': 'community'},
               'gpt-6-luna': {'value': None, 'status': 'missing', 'kind': 'none'}}
        acc = {**READINGS, 'models': {m: {} for m in rows}, 'aliases': {}, 'families': {'rows': []}, 'superseded': {'gpt-5.6-luna': {'by': 'gpt-6-luna'}}}
        meas = {'capacity': {'token_mix': {'input': 0.5, 'output': 0.5}}, 'models': {m: {} for m in rows}}
        fa.convert_usd(usd, rows, fa.Scorer(meas, acc), meas, acc)
        self.assertIsNone(usd['gpt-6-luna']['value'])

    def test_tier_up_older_gives_an_older_row_its_declared_larger_model_and_nothing_else_does(self):
        """Rule (series.tier_up): a current member of a small row gets the first model of its `tier_up` row; an older row of
        the line only through the row's `tier_up_older` declaration, with its declared version."""
        older = {'fam-0-mini': {'model': 'fam-1', 'version': 'nearest'}}
        row = {'pattern': '^fam.*mini', 'models': ['fam-1-mini'], 'size': 'small', 'tier_up': '^fam', 'tier_up_version': 'same'}
        acc = {'models': {m: {} for m in ('fam-1-mini', 'fam-0-mini', 'fam-1')},
               'families': {'rows': [{**row, 'tier_up_older': older}, {'pattern': '^fam', 'models': ['fam-1'], 'size': 'full'}]}}
        self.assertEqual(series.tier_up(acc), {'fam-0-mini': older['fam-0-mini'],
                                               'fam-1-mini': {'model': 'fam-1', 'version': 'same'}})
        acc['families']['rows'][0] = row
        self.assertEqual(list(series.tier_up(acc)), ['fam-1-mini'])

    def test_a_model_with_no_priced_sibling_takes_its_larger_models_usd_times_the_ratio_on_one_price_list(self):
        """Rule (`convert_usd`, `pair_prices`): with no same-series sibling, the larger model (series.tier_up) is the base,
        `relation: size`; both prices come from the first source that lists both, not one from each."""
        def entry(src, i, o):
            return {'source': src, 'kind': 'vendor', 'value': {'input': i, 'output': o}, 'as_of': '2026-11-09',
                    'url': 'https://example.test/p', 'listed_as': 'x'}
        rows = {'fam-big': {'fields': {'price': [entry('aa.price', 8.0, 8.0), entry('openrouter.price', 4.0, 4.0)]}},
                'fam-big-flash': {'fields': {'price': [entry('openrouter.price', 1.0, 1.0)]}}}
        usd = {'fam-big': {'value': 10.0, 'status': 'ok', 'kind': 'community'},
               'fam-big-flash': {'value': None, 'status': 'missing', 'kind': 'none'}}
        acc = {**READINGS, 'models': {m: {} for m in rows}, 'aliases': {},
               'families': {'rows': [{'pattern': '^fam.*flash', 'models': ['fam-big-flash'], 'size': 'small',
                                      'tier_up': '^fam', 'tier_up_version': 'same'},
                                     {'pattern': '^fam', 'models': ['fam-big'], 'size': 'full'}]}}
        meas = {'capacity': {'token_mix': {'input': 0.5, 'output': 0.5}}, 'models': {m: {} for m in rows}}
        fa.convert_usd(usd, rows, fa.Scorer(meas, acc), meas, acc)
        got = usd['fam-big-flash']
        self.assertEqual((got['value'], got['relation'], got['sources']['base']['model'], got['sources']['ratio']),
                         (2.5, 'size', 'fam-big', 0.25))                     # openrouter on both: 1 / 4, not 1 / 8
        self.assertEqual(fa.pair_prices([entry('aa.price', 8.0, 8.0)], [entry('openrouter.price', 1.0, 1.0)])[0]['source'],
                         'aa.price')                                         # no shared list: each its own first


if __name__ == '__main__':
    unittest.main()


class ListingIdentityTests(unittest.TestCase):
    """Round 5 review A: a listing names one snapshot and one model; a row reads another name only on a cited statement."""
    LABELS = sources.effort_readings(READINGS)

    def test_a_date_tag_stays_part_of_the_base_name_and_an_effort_tag_does_not(self):
        self.assertEqual(sources.base_name('Qwen-Max (0919)'), 'Qwen-Max 0919')
        self.assertEqual(sources.base_name('DeepSeek-V4-Pro (High, 20260813)'), 'DeepSeek-V4-Pro 20260813')
        self.assertEqual(sources.base_name('GPT-6 Astra (xhigh)'), 'GPT-6 Astra')
        self.assertEqual(sources.base_name('GPT-6 Astra high'), 'GPT-6 Astra')
        self.assertEqual(sources.base_name('Qwen3 Max'), 'Qwen3 Max')          # "Max" in a Qwen name is the name

    def test_a_reasoning_label_reads_as_declared(self):
        """Rule (accounts.json effort_readings): `non-reasoning` is reasoning off whatever else the tag names; `reasoning`
        is a listing without an effort unless the tag names one; a declaration naming an effort word as a label stops."""
        effort = lambda n: sources.aa_effort(n, self.LABELS)      # noqa: E731
        self.assertEqual([effort('M (Non-reasoning)'), effort('M (Non-reasoning, High)'), effort('M (Reasoning)'),
                          effort('M (Reasoning, Low)'), effort('M')], ['none', 'none', '', 'low', ''])
        with self.assertRaises(ValueError):
            sources.effort_readings({'effort_readings': {'labels': {'high': 'none'}, 'reads': {'default': 'high'}}})
        with self.assertRaises(ValueError):
            sources.effort_readings({'effort_readings': {'labels': {'reasoning': 'max'}, 'reads': {'default': 'high'}}})

    def test_a_fallback_part_is_a_serving_note_not_an_effort(self):
        """Rule (measurements.aa_variants): a tag part ending in `fallback` is AA's note of the model its harness falls back
        to; the listing is read at the effort the rest of the tag names."""
        recs = [{'name': 'M (High, Opus 4.8 Fallback)', 'v': 1}, {'name': 'M (Low)', 'v': 2}, {'name': 'M (Preview build)', 'v': 3}]
        got = measurements.aa_variants(recs, self.LABELS)
        self.assertEqual({e: r['v'] for e, r in got['M'].items()}, {'high': 1, 'low': 2})
        self.assertTrue(measurements.EFFORT_TAG.match('default fallback'))

    def test_a_listed_as_under_another_name_needs_a_cited_identity_statement(self):
        """Rule (measurements.listed_as_identity): a row's own spellings need no citation; another name needs
        listed_as_source with kind vendor, community or benchmark, an https url, as_of and statement; a dated maintainer
        declaration is refused, as is a listed_as_source without its listed_as."""
        cite = {'kind': 'vendor', 'url': 'https://example.test/m', 'as_of': '2026-11-18', 'statement': 'X is m-1'}
        measurements.listed_as_identity({'m-1-high': {'listed_as': {'aa': 'M-1 (High)'}}})
        measurements.listed_as_identity({'m-1': {'listed_as': {'aa': 'X'}, 'listed_as_source': {'aa': cite}}})
        for bad in ({'m-1': {'listed_as': {'aa': 'X'}}},
                    {'m-1': {'listed_as': {'aa': 'X'}, 'listed_as_source': {'aa': {**cite, 'kind': 'maintainer'}}}},
                    {'m-1': {'listed_as': {'aa': 'X'}, 'listed_as_source': {'aa': {**cite, 'url': 'http://example.test'}}}},
                    {'m-1': {'listed_as': {'aa': 'X'}, 'listed_as_source': {'aa': {**cite, 'statement': ' '}}}},
                    {'m-1': {'listed_as_source': {'aa': cite}}}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                measurements.listed_as_identity(bad)
        with self.assertRaises(ValueError):          # a dated listing is another snapshot, not the row's own spelling
            measurements.listed_as_identity({'m-1': {'listed_as': {'aa': 'M-1 (0919)'}}})


class EstimationOrderTests(unittest.TestCase):
    def test_the_declared_order_must_be_the_codes_order_with_a_distance_and_its_evidence(self):
        """Rule (five_axis.estimation_order): the data declares the order the code applies; another order, a missing
        distance or missing evidence stops the build."""
        doc = READINGS['estimation_order']
        self.assertEqual(fa.estimation_order({'estimation_order': doc})['sibling_first_up_to_distance'], 1)
        for bad in ({**doc, 'order': list(reversed(doc['order']))}, {**doc, 'sibling_first_up_to_distance': '1'},
                    {**doc, 'evidence': ''}, {k: v for k, v in doc.items() if k != 'evidence'}, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                fa.estimation_order({'estimation_order': bad})
