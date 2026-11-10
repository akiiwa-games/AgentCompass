"""Raster quality, transition endpoints and Tk callback ownership for the radar."""
import sys
import math
import unittest

import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tk_test_support import require_tk
require_tk()
import radar_chart as radar


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

    def test_the_only_backdrop_is_the_hairline_frame_of_full_scale(self):
        renderer = radar.RadarRenderer(90, 5, 1, "#FFFFFF")
        image = renderer.render([[.3] * 5])
        at = lambda axis, level: radar.point(renderer.center, renderer.center, renderer.radius * level, axis, 5)
        between = lambda level: tuple(round((a + b) / 2) for a, b in zip(at(0, level), at(1, level)))
        # Outside the shape the card shows through: no filled pentagon, no rings, and no spokes along the axes.
        for level in (.5, .75, .95):
            self.assertEqual(image.getpixel(between(level))[:3], (255, 255, 255))
        for axis in range(5):
            for level in (.5, .8):
                x, y = at(axis, level)
                box = image.crop((round(x) - 2, round(y) - 2, round(x) + 3, round(y) + 3)).convert("RGB")
                self.assertEqual(box.getcolors(64), [(25, (255, 255, 255))])
        # The frame is there: a pentagon through full scale on every axis.
        self.assertNotEqual(image.getpixel(between(1.))[:3], (255, 255, 255))

    def test_a_shape_is_a_sheet_of_tinted_glass_and_says_nothing_but_its_values(self):
        renderer = radar.RadarRenderer(90, 5, 1.5, "#FFFFFF")
        image = renderer.render([[.9] * 5])
        centre, radius = renderer.center, renderer.radius
        mean = lambda x, y: sum(sum(pixel[:3]) for pixel in image.crop((round(x) - 2, round(y) - 2, round(x) + 3, round(y) + 3)).getdata()) / 25
        # The hue runs from light at the top to deep at the bottom: never a flat fill.
        self.assertGreater(mean(centre, centre - radius * .45), mean(centre, centre + radius * .45) + 8)
        # A denser band lies just inside the edge.
        (x0, y0), (x1, y1) = (radar.point(centre, centre, radius * .9, axis, 5) for axis in (1, 2))
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        reach = math.hypot(mx - centre, my - centre)
        inward = lambda px: (mx - (mx - centre) / reach * px * 1.5, my - (my - centre) / reach * px * 1.5)
        rim, body = image.getpixel(tuple(round(v) for v in inward(2.2)))[:3], image.getpixel(tuple(round(v) for v in inward(9.)))[:3]
        self.assertLess(sum(rim), sum(body) - 20)
        # The second sheet is the lighter of the two.
        both = renderer.render([[.2] * 5, [.9] * 5])
        other = sum(both.getpixel((round(centre), round(centre + radius * .45)))[:3])
        self.assertGreater(other, sum(image.getpixel((round(centre), round(centre + radius * .45)))[:3]))
        # The task's need, quota cost and anything else that is not a model's five values have no place here.
        with self.assertRaises(TypeError):
            renderer.render([[.9] * 5], [.3, .8, .8, .3, None])
        self.assertEqual(renderer.render([[.9] * 5]).tobytes(), image.tobytes())


if __name__ == "__main__":
    unittest.main()


def test_glass_is_cut_to_the_shape_and_touches_nothing_else():
    from PIL import Image
    style = radar.SERIES_STYLES[0]
    sheet = radar.glass_sheet(200, style)
    top, bottom = sheet.getpixel((100, 0)), sheet.getpixel((100, 199))
    assert sum(top) > sum(bottom) and sheet.getpixel((0, 90)) == sheet.getpixel((199, 90))
    shape = [(100, 20), (180, 100), (100, 180), (20, 100)]
    image = Image.new("RGB", (200, 200), "#FFFFFF")
    radar.glass(image, shape, sheet, (0, 0), style, 2.)
    assert image.getpixel((100, 100)) != (255, 255, 255) and image.getpixel((30, 30)) == (255, 255, 255)
    # The rim is denser than the body; a sheet that has not opened yet leaves the image alone.
    assert sum(image.getpixel((100, 24))) < sum(image.getpixel((100, 40)))
    blank = Image.new("RGB", (200, 200), "#FFFFFF")
    radar.glass(blank, shape, sheet, (0, 0), style, 2., strength=0.)
    radar.glass(blank, shape[:2], sheet, (0, 0), style, 2.)
    radar.glass(blank, [(x + 500, y) for x, y in shape], sheet, (500, 0), style, 2.)       # off the image
    assert blank.getcolors() == [(40000, (255, 255, 255))]


def test_light_falls_from_the_upper_left_and_specks_ride_the_shape():
    shape = radar.polygon([.9] * 5, 100, 100, 80)
    lit = radar.highlights(shape, 1.)
    # Only the edges that face the light shine, each just inside its edge.
    assert 1 <= len(lit) <= 3 and all(0 < strength <= 1 for _, _, strength in lit)
    for a, b, _ in lit:
        outward = ((a[0] + b[0]) / 2 - 100, (a[1] + b[1]) / 2 - 100)
        assert outward[0] * radar.LIGHT[0] + outward[1] * radar.LIGHT[1] > 0
    assert radar.highlights(radar.polygon([.05] * 5, 100, 100, 80), 1.) == []       # too small to have an inside
    specks = radar.glints(shape, (100, 100))
    assert len(specks) == radar.GLINTS and specks == radar.glints(shape, (100, 100))
    assert all(math.hypot(x - 100, y - 100) < 80 * .9 and 0 < light < 1 for x, y, _, light in specks)
    # They move with the shape: the same specks, twice as far from the centre, on a shape twice the size.
    wide = radar.glints(radar.polygon([.9] * 5, 100, 100, 160), (100, 100))
    for (x, y, size, light), (x2, y2, size2, light2) in zip(specks, wide):
        assert (x2 - 100, y2 - 100) == pytest.approx((2 * (x - 100), 2 * (y - 100))) and (size, light) == (size2, light2)


def test_the_technical_reference_names_the_radar_s_real_axes_and_pair():
    # F7 of the redesign-4 review: the reference described axes and a pairing the page does not have.
    import i18n_catalog
    text = (Path(__file__).resolve().parents[1] / "docs" / "technical-reference.md").read_text(encoding="utf-8")
    line = next(row for row in text.splitlines() if row.lstrip().startswith("- **Radar Comparison**")).lower()
    for axes in (radar.SCORE_AXES, radar.DNA_AXES):
        for _, label, _ in axes:
            assert i18n_catalog.EN.get(label, label).lower() in line, label
    assert "best-ranked other model" in line
    for stale in ("runner-up", "subjective fit", "capability fit", "route fit", "net ability", "anti-hallucination", "visual/gui"):
        assert stale not in line, stale
