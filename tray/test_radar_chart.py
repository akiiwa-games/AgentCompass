"""Raster quality, transition endpoints and Tk callback ownership for the radar."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tk_test_support import require_tk
require_tk()
import radar_chart as radar
import tk_test_support


class RasterTests(unittest.TestCase):
    def test_transition_is_bounded_monotonic_and_finishes_exactly(self):
        samples = [radar.ease_out(i / 100) for i in range(101)]
        self.assertEqual(samples, sorted(samples))
        self.assertEqual((radar.ease_out(-1), radar.ease_out(2)), (0, 1))
        self.assertGreater(samples[50], .5)
        start, target = [[.2, .9]], [[.8, .1]]
        self.assertEqual(radar.interpolate_values(start, target, 0), start)
        self.assertEqual(radar.interpolate_values(start, target, 1), target)

    def test_grid_survives_fill_and_fractional_dpi_is_supersampled(self):
        for scale in (1, 1.5, 2):
            renderer = radar.RadarRenderer(90 * scale, 5, scale, "#FFFFFF")
            self.assertEqual(renderer.grid.size, (renderer.size * 3,) * 2)
            before = renderer.grid.tobytes()
            image = renderer.render([[1] * 5, [.6] * 5])
            self.assertEqual(image.size, (renderer.size,) * 2)
            self.assertEqual(renderer.grid.tobytes(), before)
            # A true alpha wash keeps the face light but visibly tinted, with no stipple holes.
            x = round(renderer.center - 10 * scale)
            y = round(renderer.center - 16 * scale)
            r, g, b, a = image.getpixel((x, y))
            self.assertTrue(200 < r < b <= 255)
            self.assertEqual(a, 255)
            self.assertGreater(len(image.getcolors(image.width * image.height)), 150)

    def test_zero_length_dashed_edges_render(self):
        renderer = radar.RadarRenderer(60, 5, 1, "#FFFFFF")
        self.assertEqual(renderer.render([[0] * 5, [0] * 5]).size, (renderer.size,) * 2)
        self.assertEqual(renderer.render([[0] * 5, [0] * 5], [0, 0, 0, 0, None]).size, (renderer.size,) * 2)

    def test_there_is_no_fixed_backdrop_only_spokes(self):
        renderer = radar.RadarRenderer(90, 5, 1, "#FFFFFF")
        image = renderer.render([[.3] * 5])
        at = lambda axis, level: radar.point(renderer.center, renderer.center, renderer.radius * level, axis, 5)
        # Between two spokes, outside the shape, the card shows through: no filled pentagon, no rings.
        for level in (.5, .75, .97):
            (x0, y0), (x1, y1) = at(0, level), at(1, level)
            self.assertEqual(image.getpixel((round((x0 + x1) / 2), round((y0 + y1) / 2)))[:3], (255, 255, 255))
        # A spoke still runs from the centre to full scale on every axis.
        for axis in range(5):
            x, y = at(axis, .8)
            box = image.crop((round(x) - 2, round(y) - 2, round(x) + 3, round(y) + 3)).convert("RGB")
            self.assertLess(min(sum(p) for _, p in box.getcolors(64)), 3 * 250)

    def test_need_values_reads_only_recorded_targets(self):
        fit = {"divergence": {"demand": 1, "score": .7, "target": .2667}, "global_rigor": {"target": .8},
               "module_precision": {"target": 1.4}, "vision_tools": {"target": None}, "economy": {"target": .5}}
        self.assertEqual(radar.need_values(fit), {"divergence": .2667, "global_rigor": .8, "module_precision": 1.0})
        for missing in (None, {}, "fit", {"divergence": .3}, {"divergence": {"target": float("nan")}},
                        {"divergence": {"target": True}}):
            self.assertEqual(radar.need_values(missing), {})

    def test_a_shape_is_one_flat_tint_and_marks_along_the_axes_carry_the_need_and_the_cost(self):
        renderer = radar.RadarRenderer(90, 5, 1.5, "#FFFFFF")
        values = [[.9, .9, .9, .9, .7]]
        need = [.3, .8, .8, .3, None]
        marked, plain = renderer.render(values, need, 4), renderer.render(values)
        centre, unit = renderer.center, renderer.scale
        spot = lambda axis, level, side=0.: tuple(round(v) for v in radar.axis_segment(
            centre, centre, renderer.radius, axis, 5, level, level, side * unit)[0])
        ink = lambda image, xy: sum(image.getpixel(xy)[:3])
        beside = 6.                                           # clear of the spoke and of the marks along it
        # One tint for the whole shape, with or without a need: the same colour near the centre and far from it.
        for image in (marked, plain):
            self.assertEqual(image.getpixel(spot(0, .15, beside)), image.getpixel(spot(0, .6, beside)))
            self.assertLess(ink(image, spot(0, .6, beside)), 3 * 255 - 8)
        shift = radar.MARK_SHIFT * radar.SERIES_STYLES[0]["side"]
        # Divergence: the need is .3 and the model reaches .9. From the target to the model's point a pale segment
        # in the model's colour lies beside the spoke; inside the target there is none.
        self.assertLess(ink(marked, spot(0, .6, shift)), ink(plain, spot(0, .6, shift)) - 40)
        self.assertEqual(marked.getpixel(spot(0, .15, shift)), plain.getpixel(spot(0, .15, shift)))
        # Quota economy has no target. From the model's point (.7) to full scale a red segment is the quota cost.
        red, green, blue, _ = marked.getpixel(spot(4, .85, shift))
        self.assertGreater(red, green + 60)
        self.assertGreater(red, blue + 60)
        self.assertEqual(marked.getpixel(spot(4, .4, shift)), plain.getpixel(spot(4, .4, shift)))
        # Without a cost axis (model DNA) that segment is not drawn.
        self.assertEqual(renderer.render(values, need).getpixel(spot(4, .85, shift)), plain.getpixel(spot(4, .85, shift)))
        # A need on no axis, or of the wrong length, is the same as none.
        for ignored in ([None] * 5, [.3, .8], None):
            self.assertEqual(renderer.render(values, ignored).tobytes(), plain.tobytes())


class AnimationTests(unittest.TestCase):
    def setUp(self):
        self.root = tk_test_support.shared_tk_root(self)
        self.chart = radar.RadarChartCanvas(self.root, size=420)
        self.data = [{"label": "A", "values": {key: .8 for key, _, _ in radar.DNA_AXES}}]
        self.chart.show(radar.DNA_AXES, self.data)

    def start(self):
        self.chart.animate([[0] * 5])
        return self.chart._animation_id

    def pending(self):
        return self.root.tk.splitlist(self.root.tk.call("after", "info"))

    def test_new_data_resize_and_destroy_cancel_owned_callback(self):
        chart = self.chart
        old = self.start()
        self.assertIn(old, self.pending())
        chart.show(radar.DNA_AXES, self.data)
        self.assertNotIn(old, self.pending())
        old = self.start()
        chart.on_configure(SimpleNamespace(width=440, height=420))
        self.assertNotIn(old, self.pending())
        self.assertEqual(chart._display_values, chart.target_values())
        old = self.start()
        chart.canvas.destroy()
        self.assertNotIn(old, self.pending())
        self.assertIsNone(chart._photo)

    def test_frames_reuse_grid_and_finish_at_target(self):
        chart = self.chart
        renderer = chart._renderer
        with mock.patch.object(radar.time, "perf_counter", side_effect=[0, .320]):
            chart.animate([[0] * 5])
        self.assertIs(chart._renderer, renderer)
        self.assertIsNone(chart._animation_id)
        self.assertEqual(chart._display_values, chart.target_values())

    def test_hover_and_vertex_tags_follow_displayed_values(self):
        chart = self.chart
        chart._display_values = [[.4] * 5]
        chart.draw_series()
        cx, cy, radius = chart.geometry()
        x, y = radar.point(cx, cy, radius * .4, 0, 5)
        self.assertEqual(chart.nearest_axis(x, y), (0, 0))
        x0, y0, x1, y1 = chart.canvas.coords("vertex-0-0")
        self.assertAlmostEqual((x0 + x1) / 2, x)
        self.assertAlmostEqual((y0 + y1) / 2, y)

    def test_interrupted_update_preserves_visible_values_across_unified_sources(self):
        chart = self.chart
        chart._display_values = [[.35] * 5]
        new = [{"label": "B", "values": {key: .6 for key, _, _ in radar.DNA_AXES}}]
        with mock.patch.object(chart.canvas, "winfo_ismapped", return_value=True), \
                mock.patch.object(radar.time, "perf_counter", return_value=0):
            chart.show(radar.DNA_AXES, new)
            self.assertEqual(chart._display_values, [[.35] * 5])
            old = chart._animation_id
            new[0]["values"]["divergence"] = 1
            self.assertEqual(chart.series[0]["values"]["divergence"], .6)
            chart.show(radar.SCORE_AXES, [{"label": "Score", "values": {key: .7 for key, _, _ in radar.SCORE_AXES}}])
            self.assertNotIn(old, self.pending())
            self.assertEqual(chart._display_values, [[.35] * 5])
            old = chart._animation_id
            chart.show((), [])
            self.assertNotIn(old, self.pending())
            self.assertIsNone(chart._animation_id)

    def test_remap_restarts_growth_and_unmapped_show_stays_idle(self):
        chart = self.chart
        self.assertIsNone(chart._animation_id)
        chart.on_map(None)
        self.assertIsNotNone(chart._animation_id)
        self.assertLess(chart._display_values[0][0], .1)
        chart.cancel_animation()
        self.assertIsNone(chart._animation_id)

    def test_a_chart_on_a_covered_page_stays_idle_until_its_page_is_raised(self):
        # Covered pages stay mapped (page_stack), so winfo_ismapped() alone no longer means "on screen".
        import tkinter as tk

        page = tk.Frame(self.root)
        self.addCleanup(page.destroy)
        chart = radar.RadarChartCanvas(page, size=420)
        page._page_hidden = True
        new = [{"label": "B", "values": {key: .6 for key, _, _ in radar.DNA_AXES}}]
        with mock.patch.object(chart.canvas, "winfo_ismapped", return_value=True):
            chart.show(radar.DNA_AXES, new)
            self.assertIsNone(chart._animation_id)
            self.assertEqual(chart._display_values, [[.6] * len(radar.DNA_AXES)])
            page._page_hidden = False
            chart.page_shown()
            self.assertIsNotNone(chart._animation_id)
            chart.page_hidden()
            self.assertIsNone(chart._animation_id)


if __name__ == "__main__":
    unittest.main()


def test_marks_are_quantities_spare_ability_from_the_target_and_cost_from_the_point_to_full_scale():
    values, need = [.74, .86, .88, .70, .82], [.35, .9, .9, .35, None]
    # Divergence and vision exceed the need; rigor and precision do not; quota economy has no need, only a cost.
    assert radar.axis_marks(values, need, 4) == [("spare", 0, .35, .74), ("spare", 3, .35, .70), ("cost", 4, .82, 1.)]
    assert radar.axis_marks(values, need) == [("spare", 0, .35, .74), ("spare", 3, .35, .70)]
    assert radar.axis_marks(values, None, 4) == [("cost", 4, .82, 1.)]
    assert radar.axis_marks([.35, .9, .9, .35, 1.], need, 4) == []          # exactly at the need, nothing lost
    # A mark lies along its axis, beside the spoke.
    (x0, y0), (x1, y1) = radar.axis_segment(0, 0, 100, 0, 5, .35, .74, 2.)
    assert (round(x0, 6), round(x1, 6)) == (2., 2.) and (round(y0, 6), round(y1, 6)) == (-35., -74.)


def test_the_need_line_stops_at_an_axis_without_a_need():
    assert radar.need_runs([.3, .8, .8, .3, None]) == [[0, 1, 2, 3]]       # open: it does not cross quota economy
    assert radar.need_runs([.3, None, .8, .3, .5]) == [[2, 3, 4, 0]]
    assert radar.need_runs([None] * 5) == []


def test_dashes_keep_their_pattern_around_corners_and_can_stop_part_way():
    path = [(0, 0), (10, 0), (10, 10)]
    whole = radar.dashes(path, 4, 3)
    assert whole == [((0, 0), (4, 0)), ((7, 0), (10, 0)), ((10, 0), (10, 1)), ((10, 4), (10, 8))]
    assert radar.dashes(path, 4, 3, reach=8.) == [((0, 0), (4, 0)), ((7, 0), (8, 0))]
    assert radar.dashes(path, 4, 3, reach=0.) == []
