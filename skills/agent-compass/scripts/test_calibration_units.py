"""Direct tests of the calibration helpers the five-axis scores rest on: series.py (same-series siblings and the base a
converted model takes), tiers.py (the tier is read off arena.ai Overall and has one use, estimating a missing type score)
and the usd series conversion in five_axis.py."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'calibration'))

import five_axis as fa  # noqa: E402
import series  # noqa: E402
import tiers  # noqa: E402


class SeriesTests(unittest.TestCase):
    ACC = {'models': {m: {} for m in ('gpt-6-luna', 'gpt-5.6-luna', 'gpt-6-astra', 'dot', 'claude-opus-5-5')},
           'aliases': {},
           'superseded': {'gpt-5.6-luna': {'by': 'gpt-6-luna'}},
           'same_model': {'dot': {'model': 'gpt-6-astra', 'carries': ['divergence'], 'url': 'https://example.test/dot'}}}

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
        meas = {'axes': {'divergence': {}, 'coding': {}},
                'models': {'m': {'divergence': [{'source': 'a.x', 'value': 0.5}, {'source': 'a.x', 'value': 0.9},
                                                {'source': 'a.bad', 'value': True}, {'source': 'a.str', 'value': 'high'}],
                                 'coding': [{'source': 'b.y', 'value': 0.3}],
                                 'other_field': [{'source': 'c.z', 'value': 1.0}]},
                           'n': {}}}
        self.assertEqual(series.boards(meas), {'m': {'a.x': 0.5, 'b.y': 0.3}, 'n': {}})
        self.assertEqual(series.boards({}), {})

    def test_nearest_orders_by_the_declared_model_then_index_then_boards_then_tier(self):
        same = {'t': {'model': 'declared'}}
        index = {'t': 50.0, 'near': 51.0, 'far': 70.0, 'declared': 10.0}
        board = {'t': {'s1': 0.5}, 'b_near': {'s1': 0.52}, 'b_far': {'s1': 0.9}}
        tier = {'t': 4.0, 't_near': 4.1, 't_far': 2.0}
        # the declared model first, however far its index; then index distance; then the shared boards; then the tier; then none
        order = series.nearest('t', ['far', 'near', 'declared', 'b_far', 'b_near', 't_far', 't_near', 'blank'],
                               index, board, tier, same)
        self.assertEqual(order, ['declared', 'near', 'far', 'b_near', 'b_far', 't_near', 't_far', 'blank'])
        # without the declaration its index is just far
        self.assertEqual(series.nearest('t', ['far', 'declared'], index, board, tier)[0], 'far')

    def test_the_key_breaks_ties(self):
        index = {'t': 50.0, 'b': 51.0, 'a': 49.0}
        self.assertEqual(series.nearest('t', ['b', 'a'], index, {}, {}), ['a', 'b'])


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

    def test_the_floor_is_one_and_one_point_is_no_map(self):
        f = tiers.Interpolation([10, 20], [1.2, 2.0])
        self.assertEqual(f(-1000), 1.0)
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

    def test_norm_keeps_letters_and_digits_only(self):
        self.assertEqual(tiers.norm('Claude Opus-5.5 (high)'), 'claudeopus55high')


class FitterTests(unittest.TestCase):
    @staticmethod
    def board():
        """An arena Overall board that orders the six anchors as the user scale does: value = 1000 + 100 x (tier - 3)."""
        return {name: (1000 + 100 * (tiers.ANCHOR_TIERS[k] - 3.0),) for k, name in tiers.ANCHORS.items()}

    def fitter(self):
        f = tiers.Fitter.__new__(tiers.Fitter)
        f.aa_read = '2026-11-09'
        f.overall = self.board()
        f.overall.update({'Model A': (1010.0,), 'Model B': (1060.0,), 'Model C': (1130.0,), 'Model D': (1150.0,)})
        f.overall_map = f.anchor_map(f.overall)
        f.aa = {'Model A': 40.0, 'Model B': 50.0, 'Model C': 60.0, 'Model D': 70.0, 'Only AA': 55.0}
        f.maps = {'aa.index': f.board_map(f.aa)}
        return f

    def test_the_anchor_rows_map_to_their_own_tiers_and_nothing_else_is_an_input(self):
        f = self.fitter()
        for k, name in tiers.ANCHORS.items():
            self.assertAlmostEqual(f.overall_map(f.overall[name][0]), tiers.ANCHOR_TIERS[k], places=6)
        self.assertAlmostEqual(f.arena_overall_tier('Model B'), 3.6, places=6)       # between Grok 4.7 and Sol 6.1 on this board
        self.assertIsNone(f.arena_overall_tier('Unlisted'))

    def test_anchors_that_the_board_orders_against_the_scale_are_pooled_to_one_tier(self):
        board = self.board()
        board[tiers.ANCHORS['claude-opus-5-5']] = (1000.0 + 100 * (3.9 - 3.0) - 5,)      # Opus listed below Sonnet
        f = tiers.Fitter.anchor_map(board)
        self.assertEqual(f.y, sorted(f.y))
        opus, sonnet = (f(board[tiers.ANCHORS[k]][0]) for k in ('claude-opus-5-5', 'claude-sonnet-5-5'))
        self.assertAlmostEqual(opus, sonnet, places=6)           # tied after pooling: one tier for the two

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
        acc = {'models': {m: {} for m in rows}, 'aliases': {}, 'superseded': {'gpt-5.6-luna': {'by': 'gpt-6-luna'}}}
        meas = {'capacity': {'token_mix': {'input': 0.5, 'output': 0.5}}, 'models': {m: {} for m in rows}}
        scorer = fa.Scorer(meas, acc)
        fa.convert_usd(usd, rows, scorer, meas, acc, {m: {'value': None, 'method': 'none'} for m in rows})
        got = usd['gpt-6-luna']
        self.assertEqual((got['value'], got['kind'], got['sources']['base']['model'], got['sources']['ratio']),
                         (8.0, 'converted', 'gpt-5.6-luna', 2.0))
        self.assertEqual(usd['gpt-5.6-luna']['value'], 4.0)                          # direct values are not touched
        self.assertEqual(usd['claude-opus-5-5']['value'], 7.0)

    def test_no_price_means_no_conversion_and_no_default(self):
        rows = {'gpt-5.6-luna': {'fields': {'price': []}}, 'gpt-6-luna': {'fields': {'price': []}}}
        usd = {'gpt-5.6-luna': {'value': 4.0, 'status': 'ok', 'kind': 'community'},
               'gpt-6-luna': {'value': None, 'status': 'missing', 'kind': 'none'}}
        acc = {'models': {m: {} for m in rows}, 'aliases': {}, 'superseded': {'gpt-5.6-luna': {'by': 'gpt-6-luna'}}}
        meas = {'capacity': {'token_mix': {'input': 0.5, 'output': 0.5}}, 'models': {m: {} for m in rows}}
        fa.convert_usd(usd, rows, fa.Scorer(meas, acc), meas, acc, {m: {'value': None} for m in rows})
        self.assertIsNone(usd['gpt-6-luna']['value'])


if __name__ == '__main__':
    unittest.main()
