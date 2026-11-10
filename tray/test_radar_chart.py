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

    def test_ability_beyond_the_need_is_paler_than_ability_within_it(self):
        renderer = radar.RadarRenderer(90, 5, 1.5, "#FFFFFF")
        values = [[.9, .9, .9, .9, .9]]
        need = [.3, .8, .8, .3, None]
        capped, whole = renderer.render(values, need), renderer.render(values)
        at = lambda axis, level: tuple(round(v) + 4 for v in radar.point(renderer.center, renderer.center,
                                                                        renderer.radius * level, axis, 5))
        ink = lambda image, xy: sum(image.getpixel(xy)[:3])
        self.assertLess(ink(capped, at(0, .15)), ink(capped, at(0, .6)) - 20)       # within the need: deeper
        self.assertLess(ink(capped, at(0, .6)), 3 * 255 - 12)                      # beyond it: pale, not gone
        self.assertLess(abs(ink(whole, at(0, .15)) - ink(whole, at(0, .6))), 8)    # no need recorded: one depth
        # A need on no axis, or of the wrong length, is the same as none.
        for ignored in ([None] * 5, [.3, .8], None):
            self.assertEqual(renderer.render(values, ignored).tobytes(), whole.tobytes())


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


def test_only_the_corner_beyond_the_need_is_cut_and_an_axis_without_a_need_keeps_its_vertex():
    values, need = [.74, .86, .88, .70, 1.], [.35, .9, .9, .35, None]
    points = radar.polygon(values, 0, 0, 100)
    capped = radar.polygon([v if t is None else min(v, t) for v, t in zip(values, need)], 0, 0, 100)
    shape = radar.counted_shape(points, capped)
    # Divergence and vision exceed the need: each loses its tip between the midpoints of its two edges.
    assert len(shape) == 3 + 1 + 1 + 3 + 1
    middle = lambda a, b: ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    assert shape[:3] == [middle(points[4], points[0]), capped[0], middle(points[0], points[1])]
    # Rigor and precision are within the need, and quota economy has none: their vertices stay where they are,
    # so no spike reaches for the economy axis between two cut neighbours.
    assert shape[3:5] == points[1:3] and shape[-1] == points[4]
    assert radar.counted_shape(points, points) == points
