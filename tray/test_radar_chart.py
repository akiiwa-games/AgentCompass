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


if __name__ == "__main__":
    unittest.main()
