"""Quota-line view: snapshot → display model → frames, and the synthetic decisions the README films replay."""
import copy
import math
from pathlib import Path
import sys

import pytest
from PIL import ImageChops, ImageColor

from tk_test_support import import_theatre_without_user_diagnostics

# Before quota_line imports it: the film's optional import-time diagnostics must not see the user's home.
decision_theatre = import_theatre_without_user_diagnostics()
import quota_line  # noqa: E402
import router_data  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docs" / "tools"))
import synthetic_decisions as demo  # noqa: E402

SIZES = [(300, 215, 1.), (486, 425, 1.15), (647, 623, 1.5)]      # the minimum window, the default one, the README film


def same(a, b):
    return ImageChops.difference(a, b).getbbox() is None


def snapshot(windows=demo.WINDOWS, winner=demo.HEADLINE_SPECS[0][0]):
    row = demo.decision_row(demo.HEADLINE, demo.HEADLINE_KIND, demo.HOURS, winner, "2026-10-30T09:40:00+00:00",
                            windows=windows)
    return router_data.replay(row)


def earlier():
    return snapshot(demo.EARLIER, demo.EARLIER_WINNER)


def pixel(renderer, image, x, y):
    return image.getpixel((round(x * renderer.s), round(y * renderer.s)))


def test_synthetic_decision_follows_the_quota_line_formula_and_tie_break_order():
    rows = snapshot()["ranking"]
    for row in rows:
        line, window = row["quota_line"], demo.WINDOWS[row["quota_line"]["window"]]
        cost = window[4] * line["over_line_usd"] / demo.HOURS        # β·X/h
        assert row["score"] == pytest.approx(row["components"]["fit"] - cost, abs=1e-4)
        assert row["components"]["opportunity_cost"] == pytest.approx(cost, abs=1e-4)
        assert 0 <= line["over_line_usd"] <= line["spend_usd"]
        # Capped fit: no axis counts above the task's target.
        fit = row["five_axis_fit"]
        total = sum(axis["demand"] for axis in fit.values())
        assert row["components"]["fit"] == pytest.approx(
            sum(axis["demand"] * min(axis["score"], axis["target"]) for axis in fit.values()) / total, abs=1e-4)
    assert [row["score"] for row in rows] == sorted((row["score"] for row in rows), reverse=True)
    # The rows follow the router's own order (candidate_ranking.five_axis_order, R2-F4): each row is at or after the
    # one above it, `decided_by` names the first key that separates them, and quota expiry is never such a key.
    import candidate_ranking
    for above, below in zip(rows, rows[1:]):
        assert candidate_ranking.five_axis_order(above) <= candidate_ranking.five_axis_order(below)
        assert below["quota_line"]["decided_by"] == candidate_ranking.five_axis_deciding_key(above, below)
        assert below["quota_line"]["decided_by"] in candidate_ranking.FIVE_AXIS_ORDER_KEYS + ("tie",)
    # Equal N: the faster run ranks first (expiring quota is optimised for speed).
    for above, below in zip(rows, rows[1:]):
        if above["score"] == below["score"] and above["duration_hours"] != below["duration_hours"]:
            assert above["duration_hours"] < below["duration_hours"]
    assert rows[0]["bundle"]["fast"] and rows[0]["quota_line"]["hours"] < rows[1]["quota_line"]["hours"]
    assert rows[0]["quota_line"]["spend_usd"] == pytest.approx(demo.FAST_PRICE * rows[1]["quota_line"]["spend_usd"])


def test_a_window_ahead_of_its_line_is_spared_in_the_earlier_decision():
    rows = {row["id"]: row for row in earlier()["ranking"]}
    ranking = list(rows)
    # Claude A is over its line, so every dollar there costs β; the same model on Claude B is free and wins.
    assert ranking[0] == demo.EARLIER_WINNER
    assert rows["opus55_high"]["quota_line"]["over_line_usd"] == rows["opus55_high"]["quota_line"]["spend_usd"]
    assert rows["opus55_high"]["score"] < rows["opus55_high_b"]["score"] == rows["opus55_high_b"]["components"]["fit"]
    assert rows["opus55_high_fast"]["score"] < rows["opus55_high"]["score"]
    # Used quota only grows between the two decisions of the same period.
    assert demo.EARLIER["claude-a"][2] < demo.WINDOWS["claude-a"][2]


def test_window_geometry_uses_each_window_s_own_clock():
    assert quota_line.window_geometry(dict(capacity_usd=400, elapsed_hours=166, reset_in_hours=2)) == pytest.approx(
        (166 / 168, 400 * 166 / 168))
    assert quota_line.window_geometry(dict(capacity_usd=400, elapsed_hours=24, reset_in_hours=144)) == pytest.approx(
        (1 / 7, 400 / 7))
    # A starter allowance never resets: nothing recovers, so its line stays at 0.
    assert quota_line.window_geometry(dict(capacity_usd=50, elapsed_hours=300, reset_in_hours=None)) == (0., 0.)
    assert quota_line.window_geometry({}) == (0., 0.)


def test_model_reads_the_snapshot_as_recorded_and_never_mutates_it():
    source = snapshot()
    original = copy.deepcopy(source)
    data = quota_line.model(source)
    assert source == original
    # Every recorded window is a chart, in its recorded order.
    assert [window["key"] for window in data["windows"]] == list(source["quota_line"]["windows"])
    first, codex = data["windows"][0], data["windows"][2]
    assert first["at"] == pytest.approx(166 / 168) and first["period"] == 168 and first["reset"] == 2
    assert first["line"] > first["used"] and codex["line"] < codex["used"]
    # The task's own spend lies on the window the chosen candidate draws from: fast mode, less than the two hours.
    chosen, row = data["winner"], source["ranking"][0]
    assert chosen["window"] == row["quota_line"]["window"] and chosen["spend"] == row["quota_line"]["spend_usd"]
    assert chosen["hours"] == pytest.approx(demo.HOURS / demo.FAST_SPEED, abs=.01)
    assert chosen["share"] == pytest.approx(chosen["hours"] / 168)
    # The same accounts keep their order when another decision is replayed; that one runs on the other account.
    earlier_source = earlier()
    before = quota_line.model(earlier_source)
    assert [w["key"] for w in before["windows"]] == [w["key"] for w in data["windows"]]
    earlier_row = next(r for r in earlier_source["ranking"] if r["id"] == earlier_source["choice"])
    assert before["winner"]["window"] == earlier_row["quota_line"]["window"] != chosen["window"]
    assert before["windows"][0]["used"] > before["windows"][0]["line"]


def test_decisions_without_a_quota_line_record_show_an_empty_state_not_invented_numbers():
    source = snapshot()
    assert quota_line.model({}) is None and quota_line.model(None) is None
    assert quota_line.model({**source, "quota_line": None}) is None
    # The windows are shown as recorded even when the chosen candidate's spend is unknown: nothing is lit.
    stripped = {**source, "ranking": [{k: v for k, v in row.items() if k != "quota_line"} for row in source["ranking"]]}
    assert quota_line.model(stripped)["winner"] is None and len(quota_line.model(stripped)["windows"]) == 4
    unknown = copy.deepcopy(source)
    unknown["ranking"][0]["quota_line"]["window"] = "missing"
    assert quota_line.model(unknown)["winner"] is None
    # A chosen bundle that is not ranked first is still the one whose spend is shown.
    other = copy.deepcopy(source)
    other["choice"] = other["ranking"][3]["id"]
    assert quota_line.model(other)["winner"]["window"] == other["ranking"][3]["quota_line"]["window"]
    renderer = quota_line.QuotaLineRenderer(quota_line.model(stripped), 400, 300, 1.)
    assert all(state["lit"] == 0 and state["spent"] == 0 for state in renderer.state(decision_theatre.DURATION)["cells"])
    renderer = quota_line.QuotaLineRenderer(None, 400, 300, 1.)
    assert renderer.frame(3.).size == (400, 300) and same(renderer.frame(3.), renderer.frame(9.))
    assert renderer.repaints == 0 and renderer.tip_at(200, 150) == ""


def test_an_empty_view_says_whether_nothing_was_priced_or_the_record_predates_the_economy():
    priced_nothing = quota_line.empty_text({"decision": {"system": {"quota_windows": []}}, "ranking": []})
    legacy = quota_line.empty_text({"ranking": []})
    assert priced_nothing != legacy
    assert "0" in priced_nothing
    renderer = quota_line.QuotaLineRenderer(None, 400, 300, 1., empty_text=priced_nothing)
    assert renderer.empty_text == priced_nothing


@pytest.mark.parametrize("size", SIZES)
def test_frames_render_without_a_display_and_repaint_only_the_tiles_that_moved(size):
    data = quota_line.model(earlier())
    renderer = quota_line.QuotaLineRenderer(data, *size)
    base = renderer.base.copy()
    # Every window has a head and at least one column; the tiles lie inside the canvas and none overlaps another.
    assert {name[0] for name in renderer.tiles} == {0, 1, 2, 3}
    assert all((index, "head") in renderer.tiles and (index, 0) in renderer.tiles for index in range(4))
    boxes = list(renderer.tiles.values())
    assert all(0 <= x0 < x1 <= size[0] and 0 <= y0 < y1 <= size[1] for x0, y0, x1, y1 in boxes)
    assert not any(a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]
                   for i, a in enumerate(boxes) for b in boxes[i + 1:])
    times = (0., .2, 1.0, 2.7, 3.5, 4.8, 5.6, 6.8, 7.9, 8.6, decision_theatre.DURATION)
    frames, changed = [], []
    for t in times:
        changed.append(renderer.advance(t))
        frames.append(renderer.live.copy())
    assert all(frame.size == size[:2] for frame in frames)
    # The first frame paints every tile once; nothing moves before the first phase.
    assert len(changed[0]) == len(renderer.tiles) and changed[1] == []
    for again in (decision_theatre.DURATION, quota_line.LAST, 9.6):
        assert renderer.advance(again) == [] and same(renderer.live, frames[-1])
    # Every phase adds marks, the caller owns each frame, and the static base is never drawn on.
    assert all(not same(a, b) for a, b in zip(frames[1:], frames[2:]))
    assert renderer.frame(5.) is not renderer.frame(5.)
    assert same(base, renderer.base)
    # Tile-by-tile painting in any order gives the same picture as one fresh render of that moment.
    for t in (8.4, 3.5, 6.6, 0.):
        assert same(renderer.frame(t), quota_line.QuotaLineRenderer(data, *size).frame(t)), t


@pytest.mark.parametrize("source", [snapshot, earlier])
def test_a_chart_cut_into_columns_is_the_same_picture_as_one_painted_whole(monkeypatch, source):
    data = quota_line.model(source())
    cut = quota_line.QuotaLineRenderer(data, 486, 425, 1.15)
    monkeypatch.setattr(quota_line, "COLUMN", 10 ** 6)
    whole = quota_line.QuotaLineRenderer(data, 486, 425, 1.15)
    assert len(cut.tiles) > len(whole.tiles) == 8
    for step in range(0, 100, 3):
        assert same(cut.frame(step / 10), whole.frame(step / 10)), step / 10


def test_only_the_columns_a_line_s_end_passes_through_are_repainted():
    renderer = quota_line.QuotaLineRenderer(quota_line.model(earlier()), 486, 425, 1.15)
    start, end = quota_line.PHASES["sweep"]
    middle = start + .45 * (end - start)
    renderer.advance(middle)
    moved = renderer.advance(middle + .02)
    # Each usage line has risen a little: its end's column is painted again, and at most the one next to it.
    assert {name[0] for name in moved} == {0, 1, 2, 3} and len(moved) <= 8
    state = renderer.state(middle + .02)
    for index, part in moved:
        low, high = renderer.cells[index]["columns"][part]              # never a head: no state has turned
        assert low <= state["cells"][index]["head"][0] <= high
    # The rest of the picture is the same as one fresh render of that moment.
    assert same(renderer.live, quota_line.QuotaLineRenderer(renderer.data, 486, 425, 1.15).frame(middle + .02))


@pytest.mark.parametrize("count", [1, 2, 3, 4])
@pytest.mark.parametrize("size", [(297, 196, 1.), (300, 215, 1.), (486, 425, 1.15), (647, 623, 1.5), (420, 800, 1.)])
def test_charts_stay_inside_the_canvas_and_clear_of_each_other(count, size):
    source = snapshot()
    keys = list(source["quota_line"]["windows"])[:count]
    source["quota_line"]["windows"] = {key: source["quota_line"]["windows"][key] for key in keys}
    renderer = quota_line.QuotaLineRenderer(quota_line.model(source), *size)
    assert len(renderer.cells) == count
    right, bottom = size[0] / renderer.s, size[1] / renderer.s
    for cell in renderer.cells:
        assert 0 <= cell["x0"] < cell["px0"] < cell["px1"] < cell["x1"] <= right + 1e-6
        assert 0 <= cell["y0"] < cell["py0"] < cell["py1"] < cell["y1"] <= bottom + 1e-6
        # Room for the name and the state above the chart, and for a line's end around it.
        assert cell["py0"] - cell["y0"] >= quota_line.HEAD and cell["py1"] - cell["py0"] >= 30
    for a, b in zip(renderer.cells, renderer.cells[1:]):
        assert a["x1"] <= b["x0"] or a["y1"] <= b["y0"]
    assert renderer.frame(decision_theatre.DURATION).size == size[:2]


def test_readings_come_only_from_the_period_of_the_decision():
    hour, at, reset = 3600., 1_000_000., 1_000_000. + 20 * 3600
    series = [[at - 200 * hour, 90., reset - 168 * hour, at - 190 * hour],      # the period before: another reset time
              [at - 100 * hour, 10., reset, at - 100 * hour],
              [at - 50 * hour, 25., reset + 30, at - 40 * hour],                 # confirmed again later: a step
              [at - 2 * hour, 40., reset, at + 5 * hour],                        # still standing at the decision
              [at + hour, 45., reset, at + hour],                                # after the decision
              [at - 10 * hour, None, reset, at], "junk"]
    samples = quota_line.period_samples(series, reset, at, 148., 400.)
    assert samples == [[48., 40.], [98., 100.], [108., 100.], [146., 160.], [148., 160.]]
    assert quota_line.period_samples([], reset, at, 148., 400.) == [] and quota_line.period_samples(None, reset, at, 148., 400.) == []


def test_the_page_looks_readings_up_itself_and_never_changes_the_snapshot(monkeypatch):
    from datetime import datetime
    source = snapshot()
    at = datetime.fromisoformat("2026-10-30T09:40:00+00:00").timestamp()
    window = source["quota_line"]["windows"]["gemini"]
    window.update(account="gemini-1", window_id="gemini-weekly")
    reset = at + window["reset_in_hours"] * 3600
    state = {"files": {}, "usage_samples": {"gemini-1": {"gemini-weekly": [[at - 40 * 3600, 20., reset, at - 40 * 3600],
                                                                           [at - 10 * 3600, 50., reset, at - 10 * 3600]]}}}
    import capacity_stats
    monkeypatch.setattr(router_data, "usage_readings_state", lambda: (capacity_stats, state))
    before = copy.deepcopy(source)
    shown = router_data.with_usage_readings(source, "2026-10-30T09:40:00+00:00")
    assert source == before                                              # the snapshot is not touched
    elapsed, capacity = window["elapsed_hours"], window["capacity_usd"]
    assert shown["quota_line"]["windows"]["gemini"]["samples"] == [[elapsed - 40, capacity * .2], [elapsed - 10, capacity * .5]]
    assert quota_line.model(shown)["windows"][3]["readings"] == 2
    # Windows that carry their own samples, or do not say where to look, are left as recorded.
    assert shown["quota_line"]["windows"]["claude-a"] is source["quota_line"]["windows"]["claude-a"]
    assert shown["quota_line"]["windows"]["codex"] is source["quota_line"]["windows"]["codex"]
    # No readings in this period, an unreadable time or a broken store: the snapshot as it is.
    assert router_data.with_usage_readings(source, "2026-06-01T00:00:00+00:00") is source
    assert router_data.with_usage_readings(source, "not a time") is source
    monkeypatch.setattr(router_data, "usage_readings_state", lambda: (_ for _ in ()).throw(OSError("gone")))
    assert router_data.with_usage_readings(source, "2026-10-30T09:40:00+00:00") is source


def greenish(color):
    return color[1] > color[0] + 25 and color[1] > color[2]


def reddish(color):
    return color[0] > color[1] + 25 and color[0] > color[2] + 25


def accent(color):
    return color[2] > color[0] + 60 and color[2] > color[1] + 60


def settled(source=None, size=(647, 623, 1.5)):
    renderer = quota_line.QuotaLineRenderer(quota_line.model(source or snapshot()), *size)
    return renderer, renderer.frame(decision_theatre.DURATION)


def spot(renderer, image, cell, moment, amount):
    return pixel(renderer, image, *renderer.at(cell, moment, amount))


def state_pixel(renderer, image, cell):
    """A pixel of the state word's pill, clear of the word itself."""
    return pixel(renderer, image, cell["x1"] - 17, cell["y0"] + quota_line.HEAD / 2 + 2)


def test_each_chart_is_the_window_s_own_clock_with_the_quota_line_from_corner_to_corner():
    renderer, image = settled()
    paper = ImageColor.getrgb(quota_line.theme.CARD)
    for cell in renderer.cells[1:]:                                   # the first one is lit: its card is not paper
        window = cell["window"]
        # Left to right is the period, bottom to top is the quota: nothing at the floor, all of it at the top.
        assert renderer.at(cell, 0., 0.) == (cell["px0"], cell["py1"])
        assert renderer.at(cell, 1., window["capacity"]) == (cell["px1"], cell["py0"])
        assert renderer.at(cell, .5, window["capacity"] / 2) == pytest.approx(((cell["px0"] + cell["px1"]) / 2, (cell["py0"] + cell["py1"]) / 2))
        # The quota line runs straight from the period's start to the whole capacity at the reset.
        beyond = [moment for moment in (.3, .5, .7, .9) if moment > window["at"] + .08]
        assert beyond and all(spot(renderer, image, cell, moment, window["capacity"] * moment) != paper for moment in beyond)
        # Off the lines the chart is bare.
        assert spot(renderer, image, cell, .2, window["capacity"] * .9) == paper
    # Each window is at its own moment of its own period.
    assert len({round(cell["window"]["at"], 3) for cell in renderer.cells}) == 4


def test_the_usage_line_is_green_under_the_quota_line_and_red_over_it_and_says_so():
    renderer, image = settled(earlier())
    words = {}
    for cell in renderer.cells:
        window = cell["window"]
        for run in cell["runs"]:
            if run["fitted"] or len(run["pts"]) < 6:
                continue
            # A stretch of the line lies on one side of the quota line, and wears that side's colour.
            moment, amount = run["pts"][len(run["pts"]) // 2]
            assert (amount > window["capacity"] * moment) == run["over"]
            assert (reddish if run["over"] else greenish)(spot(renderer, image, cell, moment, amount)), (window["key"], moment)
        # The state is that of the line's end, now.
        over = window["used"] > window["line"]
        assert (reddish if over else greenish)(state_pixel(renderer, image, cell)), window["key"]
        words[window["key"]] = over
    assert words == {"claude-a": True, "claude-b": False, "codex": True, "gemini": False}
    # Both sides occur along one line: Claude A started under its quota line and overtook it.
    assert [run["over"] for run in renderer.cells[0]["runs"] if not run["fitted"]] == [False, True]
    # The area between the two lines is tinted on the line's side: clearly lighter than the line itself.
    cell = renderer.cells[2]
    window = cell["window"]
    between = spot(renderer, image, cell, window["at"] - .03, (window["used"] + window["line"]) / 2 - window["capacity"] * .03)
    assert reddish(between) or between[0] > between[1] > 200, between


def test_the_state_turns_as_the_rising_line_crosses_the_quota_line():
    # Two hours before its reset Claude A is under its quota line, but early in the period it ran ahead of it.
    renderer = quota_line.QuotaLineRenderer(quota_line.model(snapshot()), 647, 623, 1.5)
    cell = renderer.cells[0]
    start, end = quota_line.PHASES["sweep"]
    seen = []
    for step in range(1, 40):
        story = start + (end - start) * step / 40
        live = renderer.state(story)["cells"][0]
        image = renderer.frame(story)
        if live["pace"] * 6 >= 1 and live["over"] in (0., 1.):
            # The word's pill and the line's end wear the colour of the side the end is on.
            tone = reddish if live["over"] else greenish
            assert tone(state_pixel(renderer, image, cell)), story
            x, y = renderer.at(cell, *live["head"])
            assert tone(pixel(renderer, image, x + 3.2, y)), story
            seen.append(live["over"])
    assert seen[0] == 1. and seen[-1] == 0. and seen == sorted(seen, reverse=True)
    # The usage line only rises, and it ends where the decision read it.
    heads = [renderer.state(start + (end - start) * step / 40)["cells"][0]["head"] for step in range(41)]
    assert all(a[0] <= b[0] and a[1] <= b[1] + 1e-9 for a, b in zip(heads, heads[1:]))
    assert heads[-1] == pytest.approx((cell["window"]["at"], cell["window"]["used"]))


def test_the_usage_line_is_recorded_readings_or_a_fit_that_is_drawn_as_a_fit():
    source = snapshot()
    recorded = source["quota_line"]["windows"]
    assert "samples" in recorded["claude-a"] and "samples" not in recorded["gemini"]
    renderer, image = settled(source)
    measured, fitted = renderer.cells[1], renderer.cells[3]
    assert measured["window"]["readings"] == len(recorded["claude-b"]["samples"]) and fitted["window"]["readings"] == 0
    assert all(run["fitted"] for run in fitted["runs"]) and not all(run["fitted"] for run in measured["runs"])

    def inked(cell, run):
        (t0, u0), (t1, u1) = run["pts"][0], run["pts"][-1]
        return [sum(spot(renderer, image, cell, t0 + (t1 - t0) * step / 60, u0 + (u1 - u0) * step / 60)) < 560
                for step in range(4, 57)]

    # A fit is dotted: ink and gaps along it. It still starts at nothing and ends at the usage read now.
    dots = inked(fitted, max(fitted["runs"], key=lambda run: len(run["pts"])))
    assert any(dots) and not all(dots)
    assert fitted["runs"][0]["pts"][0] == (0., 0.) and fitted["runs"][-1]["pts"][-1] == pytest.approx((fitted["window"]["at"], fitted["window"]["used"]))
    # Recorded readings are one solid line through every reading.
    run = max((run for run in measured["runs"] if not run["fitted"]), key=lambda run: len(run["pts"]))
    assert all(sum(spot(renderer, image, measured, *point)) < 560 for point in run["pts"][2:-2])
    period = measured["window"]["period"]
    points = [point for run in measured["runs"] for point in run["pts"]]
    for hours, used in recorded["claude-b"]["samples"]:
        assert any(abs(t - hours / period) < 1e-6 and abs(u - used) < 1e-6 for t, u in points)
    # A snapshot without usable readings falls back to the fit.
    for bad in ([], [[1., 2.]], "junk", [[-5., 1.], [9e9, 2.]]):
        recorded["claude-b"]["samples"] = bad
        assert quota_line.model(source)["windows"][1]["readings"] == 0


def test_the_curve_through_the_readings_never_dips_and_misses_none():
    path = [(0., 0.), (.1, 5.), (.2, 5.), (.2, 30.), (.5, 31.), (.6, 70.)]         # a pause, a step, a slow stretch
    curve, lead = quota_line.smooth(path, 1, 300)
    assert curve[0] == (0., 0.) and curve[-1] == (.6, 70.) and lead > 0
    assert all(a[0] <= b[0] and a[1] <= b[1] + 1e-9 for a, b in zip(curve, curve[1:]))
    for moment, amount in path:
        assert any(abs(t - moment) < 1e-5 and abs(u - amount) < 1e-6 for t, u in curve)
    # The fitted lead is the straight line it claims to be.
    assert all(u == pytest.approx(50 * t) for t, u in curve[:lead + 1])
    # A fit alone is a straight line too.
    straight, lead = quota_line.smooth([(0., 0.), (.4, 20.)], 1, 300)
    assert lead == len(straight) - 1 and all(u == pytest.approx(50 * t) for t, u in straight)
    # Where the curve crosses the quota line it is cut, and each stretch knows its side.
    runs = quota_line.runs_by_state(curve, 0, 100., True)
    assert [run["over"] for run in runs] == [False, True, False, True]
    assert all(a["pts"][-1] == b["pts"][0] for a, b in zip(runs, runs[1:]))
    assert all(run["over"] is None for run in quota_line.runs_by_state(curve, 0, 100., False))


def test_the_window_the_task_draws_from_is_lit_and_its_spend_is_added_to_the_line():
    renderer = quota_line.QuotaLineRenderer(quota_line.model(snapshot()), 647, 623, 1.5)
    card = ImageColor.getrgb(quota_line.theme.CARD)
    before = renderer.frame(quota_line.PHASES["spend"][0])
    after = renderer.frame(quota_line.PHASES["spend"][1])
    chosen, other = renderer.cells[0], renderer.cells[1]
    inside = lambda image, cell: pixel(renderer, image, cell["x0"] + 4, (cell["y0"] + cell["y1"]) / 2)
    assert inside(before, chosen) == inside(before, other) == inside(after, other) == card
    assert inside(after, chosen) != card and inside(after, chosen)[2] > inside(after, chosen)[0]
    # The task's spend continues the usage line from now, in the accent colour, for the task's own duration.
    window, task = chosen["window"], chosen["task"]
    assert other["task"] is None and task["spend"] > 0
    assert chosen["end"] == pytest.approx((window["at"] + task["share"], window["used"] + task["spend"]))
    middle = (window["at"] + task["share"] / 2, window["used"] + task["spend"] / 2)
    assert accent(spot(renderer, after, chosen, *middle)) and not accent(spot(renderer, before, chosen, *middle))
    # A window nobody draws from keeps its end where the decision read it.
    assert other["end"] == (other["window"]["at"], other["window"]["used"])


def test_the_two_lines_close_at_the_reset():
    renderer = quota_line.QuotaLineRenderer(quota_line.model(earlier()), 647, 623, 1.5)
    before = renderer.frame(quota_line.PHASES["close"][0])
    after = renderer.frame(decision_theatre.DURATION)
    for cell in renderer.cells:
        window = cell["window"]
        assert cell["closes"]
        corner = renderer.at(cell, 1., window["capacity"])
        # The corner where the lines meet is an open ring until the pace line reaches it, then it is filled.
        assert not accent(pixel(renderer, before, *corner)) and accent(pixel(renderer, after, *corner))
        # The pace that uses up what is left: a dotted line from the usage line's end to that corner.
        (x0, y0), (x1, y1) = renderer.at(cell, *cell["end"]), corner
        along = [(x0 + (x1 - x0) * step / 200, y0 + (y1 - y0) * step / 200) for step in range(30, 180)]
        dark = [sum(pixel(renderer, after, x, y)) < 600 for x, y in along]
        assert any(dark) and not all(dark)
        assert sum(sum(pixel(renderer, before, x, y)) < 600 for x, y in along) < sum(dark)
    # A window that is already full, or never resets, has no pace to draw.
    source = earlier()
    source["quota_line"]["windows"]["claude-a"]["used_usd"] = source["quota_line"]["windows"]["claude-a"]["capacity_usd"]
    source["quota_line"]["windows"]["codex"]["reset_in_hours"] = None
    renderer = quota_line.QuotaLineRenderer(quota_line.model(source), 647, 623, 1.5)
    assert [cell["closes"] for cell in renderer.cells] == [False, True, False, True]
    final = renderer.state(decision_theatre.DURATION)["cells"]
    assert final[0]["closed"] == 0 and final[2]["closed"] == 0 and final[1]["closed"] == 1
    # Without a reset there is no quota line and no state to tell: the usage line is plain.
    assert final[2]["over"] is None and all(run["over"] is None for run in renderer.cells[2]["runs"])
    assert renderer.frame(decision_theatre.DURATION).size == (647, 623)


def test_the_picture_carries_only_window_names_and_their_state(monkeypatch):
    words = []
    real = quota_line.QuotaLineRenderer.text
    monkeypatch.setattr(quota_line.QuotaLineRenderer, "text",
                        lambda self, value, *args, **kwargs: words.append(str(value)) or real(self, value, *args, **kwargs))
    renderer = quota_line.QuotaLineRenderer(quota_line.model(earlier()), 486, 425, 1.15)
    for step in range(0, 100, 4):
        renderer.advance(step / 10)
    labels = {window["label"] for window in renderer.data["windows"]}
    assert set(words) == labels | {quota_line._tr("优先使用"), quota_line._tr("降权")}


def test_hover_text_says_what_the_chart_under_the_pointer_stands_for():
    renderer, _ = settled(earlier(), (486, 425, 1.15))
    s = renderer.s
    at = lambda cell: ((cell["px0"] + cell["px1"]) / 2 * s, (cell["py0"] + cell["py1"]) / 2 * s)
    over, under = renderer.tip_at(*at(renderer.cells[0])), renderer.tip_at(*at(renderer.cells[1]))
    assert over.startswith("Claude A") and quota_line._tr("这个窗口被降权") in over
    assert quota_line._tr("实际用量线来自 {0} 个读数").format(renderer.cells[0]["window"]["readings"]) in over
    assert under.startswith("Claude B") and quota_line._tr("这个窗口被优先使用") in under
    # The window the task draws from also says which candidate that is and what it spends.
    task = renderer.cells[1]["task"]
    assert task["label"] in under and quota_line.usd(task["spend"]) in under and task["label"] not in over
    assert quota_line._tr("本期没有读数，实际用量线按期初到当前拟合") in renderer.tip_at(*at(renderer.cells[3]))
    assert renderer.tip_at(1, 1) == ""                                 # the margin around the charts


@pytest.mark.slow
def test_canvas_follows_the_film_clock_only_while_visible(monkeypatch):
    from tk_test_support import shared_tk_root, reset_tk_root
    root = shared_tk_root()
    view = quota_line.QuotaLineCanvas(root)
    try:
        frames = []
        real = quota_line.QuotaLineRenderer.advance
        monkeypatch.setattr(quota_line.QuotaLineRenderer, "advance",
                            lambda self, story=decision_theatre.DURATION: frames.append(story) or real(self, story))
        view.show(earlier())
        view.follow(5.)
        assert not frames and view.story == 5. and view.tip_at(10, 10) == ""   # hidden: remembers the time, draws nothing
        monkeypatch.setattr(view.canvas, "winfo_ismapped", lambda: True)
        monkeypatch.setattr(view.canvas, "winfo_width", lambda: 420)
        monkeypatch.setattr(view.canvas, "winfo_height", lambda: 390)
        view.draw()
        assert frames == [5.] and view.photo is not None
        assert not view.strips                           # the first picture of a decision is one full image
        view.follow(5.001)                               # same 1/30 s step: no redraw
        view.follow(5.04)
        assert frames == [5., 5.04]
        # Only the tiles that moved are pasted, each at its own place.
        moved = set(view.strips)
        assert moved and moved < set(view.renderer.tiles)
        for name in moved:
            x0, y0, x1, y1 = view.renderer.tiles[name]
            assert (view.strips[name][0].width(), view.strips[name][0].height()) == (x1 - x0, y1 - y0)
            assert view.canvas.coords(view.strips[name][1]) == [float(x0), float(y0)]
        for story in (9.45, 9.6, decision_theatre.DURATION, decision_theatre.DURATION):
            view.follow(story)                           # after the last phase: one settled frame, then nothing
        assert frames == [5., 5.04, 9.45]
        assert moved < set(view.strips) <= set(view.renderer.tiles)
        cell = view.renderer.cells[0]
        assert view.tip_at((cell["px0"] + 20) * view.renderer.s, (cell["py0"] + 20) * view.renderer.s)
        view.show(snapshot())
        assert frames[-1] == decision_theatre.DURATION and len(frames) == 4
        # The next decision starts from a full image again; the old strips no longer cover it.
        assert all(view.canvas.itemcget(item, "state") == "hidden" for _, item in view.strips.values())
        view.follow(5.)
        shown = [name for name, (_, item) in view.strips.items() if view.canvas.itemcget(item, "state") == "normal"]
        assert shown
        for name in shown:
            assert view.canvas.coords(view.strips[name][1]) == [float(v) for v in view.renderer.tiles[name][:2]], name
        monkeypatch.setattr(view.canvas, "winfo_width", lambda: 300)
        view.configure(None)                             # a new size drops them
        assert not view.strips and view.photo.width() == 300
    finally:
        view.canvas.destroy()
        reset_tk_root(root)


# --- review of redesign 4: one test per finding -------------------------------------------------------------
def plain(windows, row=None):
    """A minimal snapshot: recorded windows and, with `row`, the chosen candidate's quota-line record."""
    ranking = [dict(id="pick", bundle=dict(model="opus-5.5", effort="high"), quota_line=row)] if row is not None else []
    return dict(choice="pick", ranking=ranking, quota_line=dict(windows=windows))


def half(used, capacity=100, **more):
    """A weekly window halfway through its period: its quota line stands at half its capacity."""
    return {**dict(label="Claude A", capacity_usd=capacity, used_usd=used, elapsed_hours=84, reset_in_hours=84), **more}


def tip_of(renderer, cell):
    return renderer.tip_at((cell["px0"] + 20) * renderer.s, (cell["py0"] + 20) * renderer.s)


def words_of(monkeypatch, data, size=(486, 425, 1.15), **options):
    """Every text the view draws for a model, settled."""
    words = []
    real = quota_line.QuotaLineRenderer.text
    monkeypatch.setattr(quota_line.QuotaLineRenderer, "text",
                        lambda self, value, *args, **kwargs: words.append(str(value)) or real(self, value, *args, **kwargs))
    renderer = quota_line.QuotaLineRenderer(data, *size, **options)
    renderer.frame(decision_theatre.DURATION)
    monkeypatch.setattr(quota_line.QuotaLineRenderer, "text", real)
    return renderer, set(words)


@pytest.mark.parametrize("used, spend, over", [(49, 10, False), (60, 5, True)])
def test_the_state_is_the_one_the_decision_saw_and_the_task_s_own_spend_does_not_turn_it(used, spend, over):
    # F1. Under its line by $1 when the decision was taken, and this task's $10 takes it over the line: the
    # router chose the window because it was "used first", and that is what it keeps saying.
    data = quota_line.model(plain({"a": half(used)}, dict(window="a", hours=2, spend_usd=spend)))
    renderer = quota_line.QuotaLineRenderer(data, 647, 623, 1.5)
    cell = renderer.cells[0]
    window = cell["window"]
    assert cell["over"] is over and cell["end"][1] == used + spend > 100 * cell["end"][0]
    tone = reddish if over else greenish
    start, end = quota_line.PHASES["spend"]
    for story in (start, (start + end) / 2, end, decision_theatre.DURATION):
        assert renderer.state(story)["cells"][0]["over"] is over, story
        assert tone(state_pixel(renderer, renderer.frame(story), cell)), story
    image = renderer.frame(decision_theatre.DURATION)
    # The hover text agrees with the word above the chart.
    tip = tip_of(renderer, cell)
    assert (quota_line._tr("这个窗口被降权") in tip) is over and (quota_line._tr("这个窗口被优先使用") in tip) is not over
    # The spend itself is the accent segment, and the line's end belongs to it once the task has run.
    assert accent(spot(renderer, image, cell, (window["at"] + cell["end"][0]) / 2, (window["used"] + cell["end"][1]) / 2))
    x, y = renderer.at(cell, *cell["end"])
    assert accent(pixel(renderer, image, x + 3.2, y))
    # Before the task the end wears the window's state.
    x, y = renderer.at(cell, window["at"], window["used"])
    assert tone(pixel(renderer, renderer.frame(start), x + 3.2, y))


GARBAGE = ("n/a", "", float("nan"), float("inf"), [], {}, True)


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def test_a_malformed_snapshot_is_read_field_by_field_and_never_raises(monkeypatch):
    # F2. One field of one window is recorded as something else; the other window is always shown.
    row = dict(window="a", hours=2, spend_usd=10)
    for field in ("capacity_usd", "used_usd", "elapsed_hours", "reset_in_hours", "label", "samples", "account", "window_id"):
        for value in GARBAGE + (None, -5, "100"):
            data = quota_line.model(plain({"a": {**half(40), field: value}, "b": half(60, label="B")}, row))
            bad = field in ("used_usd", "elapsed_hours", "reset_in_hours") and value is not None and not is_number(value)
            # A window whose usage or clock is not a number is left out, and the view says so.
            assert [window["key"] for window in data["windows"]] == (["b"] if bad else ["a", "b"]), (field, value)
            assert data["dropped"] == (["a"] if bad else []) and (data["winner"] is None) is bad
            renderer = quota_line.QuotaLineRenderer(data, 400, 300, 1.)
            assert renderer.frame(decision_theatre.DURATION).size == (400, 300)
            assert all(tip_of(renderer, cell) for cell in renderer.cells)
            assert (quota_line._tr("{0} 个窗口的记录无法读取").format(1) in renderer.note) is bad
            if field == "capacity_usd" and not (is_number(value) and value > 0):
                assert data["windows"][0]["capacity"] is None           # unknown, not zero
    # The chosen candidate's record: an unknown spend still lights its window, an unknown window lights none.
    for field in ("window", "hours", "spend_usd"):
        for value in GARBAGE + (None, -5):
            data = quota_line.model(plain({"a": half(40)}, {**row, field: value}))
            if field == "window":
                assert data["winner"] is None
                continue
            winner = data["winner"]
            assert winner["window"] == row["window"] and winner["hours"] == (2 if field == "spend_usd" else 0)
            if field == "spend_usd":
                assert winner["spend"] == (0 if value == -5 else None)
            renderer = quota_line.QuotaLineRenderer(data, 400, 300, 1.)
            renderer.frame(decision_theatre.DURATION)
            assert renderer.state(decision_theatre.DURATION)["cells"][0]["lit"] == 1
            if field == "spend_usd" and value != -5:
                assert quota_line._tr("本次任务用这个窗口：{0}，花费未知").format(winner["label"]) in tip_of(renderer, renderer.cells[0])
    # Rows and containers of another type.
    source = plain({"a": half(40)}, row)
    for ranking in ("abc", None, 7, {}, ["x", None, 5, [], {"id": "pick", "bundle": [], "quota_line": row}],
                    [{"id": "pick", "bundle": {"model": 5, "effort": []}, "quota_line": "x"}]):
        data = quota_line.model({**source, "ranking": ranking})
        assert [window["key"] for window in data["windows"]] == ["a"]
        assert (data["winner"] is not None) is (isinstance(ranking, list) and len(ranking) == 5)
    for broken in ([], "x", 5, {"quota_line": "x"}, {"quota_line": {"windows": []}}, {"quota_line": {"windows": "x"}}):
        assert quota_line.model(broken) is None
    # Nothing readable at all: the view says the record cannot be read, which is not the same as having none.
    data = quota_line.model(plain({"a": "x", "b": {**half(40), "used_usd": "?"}}, row))
    assert data["windows"] == [] and data["dropped"] == ["a", "b"] and data["winner"] is None
    renderer, words = words_of(monkeypatch, data)
    assert words == {quota_line._tr("额度恢复线记录无法读取")} and not renderer.cells and renderer.tip_at(200, 200) == ""
    assert words_of(monkeypatch, None)[1] == {quota_line._tr("此决策没有额度恢复线记录（记录早于额度经济）")}
    assert words_of(monkeypatch, None, unreadable=True)[1] == {quota_line._tr("额度恢复线记录无法读取")}


@pytest.mark.slow
def test_a_record_the_view_cannot_read_never_raises_into_the_page(monkeypatch):
    # F2. The page shows a decision by calling show(); follow() and a resize draw it again.
    from tk_test_support import shared_tk_root, reset_tk_root
    root = shared_tk_root()
    view = quota_line.QuotaLineCanvas(root)
    try:
        monkeypatch.setattr(view.canvas, "winfo_ismapped", lambda: True)
        monkeypatch.setattr(view.canvas, "winfo_width", lambda: 420)
        monkeypatch.setattr(view.canvas, "winfo_height", lambda: 390)
        unreadable = quota_line.QuotaLineRenderer(None, 420, 390, quota_line.theme.FACTOR, unreadable=True).frame()
        real = quota_line.model
        monkeypatch.setattr(quota_line, "model", lambda result: 1 / 0)
        view.show(earlier())
        assert isinstance(view.error, ZeroDivisionError) and view.data is None
        assert same(view.renderer.live, unreadable) and view.tip_at(100, 100) == ""
        monkeypatch.setattr(quota_line, "model", real)
        # The model is read but cannot be drawn: the same unreadable state, and the film's clock is still followed.
        layout = quota_line.QuotaLineRenderer.layout
        monkeypatch.setattr(quota_line.QuotaLineRenderer, "layout", lambda self: [][0])
        view.show(earlier())
        assert isinstance(view.error, IndexError) and view.data is None and same(view.renderer.live, unreadable)
        view.follow(5.)
        view.configure(None)
        assert same(view.renderer.live, unreadable)
        # The next readable decision is shown as usual.
        monkeypatch.setattr(quota_line.QuotaLineRenderer, "layout", layout)
        view.show(earlier())
        assert view.error is None and len(view.renderer.cells) == 4
    finally:
        view.canvas.destroy()
        reset_tk_root(root)


def six_windows():
    return {f"acct{n}-{span}": dict(label=f"Account {n} {span}", account=f"acct{n}", capacity_usd=100, used_usd=10 + 10 * n,
                                    elapsed_hours=hours, reset_in_hours=hours)
            for n in range(3) for span, hours in (("5h", 2.5), ("7d", 84))}


def test_the_chosen_window_is_always_shown_and_the_view_says_how_many_are_not(monkeypatch):
    # F3. Three accounts with a 5-hour and a 7-day window each; the task runs on the last one recorded.
    row = dict(window="acct2-7d", hours=2, spend_usd=10)
    data = quota_line.model(plain(six_windows(), row))
    # The chosen window, its account's other window, then the others; shown in their recorded order.
    assert [window["key"] for window in data["windows"]] == ["acct0-5h", "acct0-7d", "acct2-5h", "acct2-7d"]
    assert data["winner"]["window"] == row["window"] and data["hidden"] == ["Account 1 5h", "Account 1 7d"]
    renderer, words = words_of(monkeypatch, data)
    assert renderer.cells[3]["task"] is data["winner"] and renderer.state(decision_theatre.DURATION)["cells"][3]["lit"] == 1
    note = quota_line._tr("另有 {0} 个窗口未显示").format(2)
    assert renderer.note == note and note in words
    # The note has its own room under the charts, and names what is hidden when pointed at.
    bottom = renderer.h / renderer.s
    assert all(cell["y1"] <= bottom - quota_line.NOTE + 1e-6 for cell in renderer.cells)
    assert renderer.tip_at(renderer.w / 2, renderer.h - 3) == note + quota_line._tr("；") + quota_line._tr("未显示的窗口：{0}").format(
        "Account 1 5h, Account 1 7d")
    # Without account names the chosen window still comes first; without a chosen candidate, the first four.
    bare = {key: {k: v for k, v in window.items() if k != "account"} for key, window in six_windows().items()}
    assert [window["key"] for window in quota_line.model(plain(bare, row))["windows"]] == ["acct0-5h", "acct0-7d", "acct1-5h", "acct2-7d"]
    assert [window["key"] for window in quota_line.model(plain(six_windows()))["windows"]] == list(six_windows())[:4]
    # An unreadable window is counted apart, and does not take a chart's place.
    source = six_windows()
    source["acct0-5h"]["used_usd"] = "?"
    data = quota_line.model(plain(source, row))
    assert [window["key"] for window in data["windows"]] == ["acct0-7d", "acct1-5h", "acct2-5h", "acct2-7d"]
    assert data["hidden"] == ["Account 1 7d"] and data["dropped"] == ["acct0-5h"]
    renderer = quota_line.QuotaLineRenderer(data, 297, 196, 1.)
    assert renderer.note == quota_line._tr("；").join((quota_line._tr("另有 {0} 个窗口未显示").format(1),
                                                       quota_line._tr("{0} 个窗口的记录无法读取").format(1)))
    for cell in renderer.cells:                                         # the smallest view still has room for every chart
        assert cell["py1"] - cell["py0"] >= 30 and cell["y1"] <= renderer.h / renderer.s - quota_line.NOTE + 1e-6
    # Four windows or fewer: no note, no room taken.
    assert quota_line.QuotaLineRenderer(quota_line.model(snapshot()), 486, 425, 1.15).note == ""


MISSING = object()


@pytest.mark.parametrize("capacity", [0, MISSING, None, "n/a", -3, float("nan")])
def test_a_window_without_a_known_capacity_has_no_state_and_no_ratio(monkeypatch, capacity):
    # F4.
    window = dict(label="W", used_usd=5, elapsed_hours=10, reset_in_hours=10)
    if capacity is not MISSING:
        window["capacity_usd"] = capacity
    data = quota_line.model(plain({"w": window}, dict(window="w", hours=1, spend_usd=1)))
    assert data["windows"][0]["capacity"] is None and not data["windows"][0]["lined"]
    renderer, words = words_of(monkeypatch, data)
    cell = renderer.cells[0]
    # No quota line to compare with: no state word, a plain usage line, no pace to close.
    assert words == {"W"}
    assert cell["over"] is None and all(run["over"] is None for run in cell["runs"]) and not cell["closes"]
    assert all(live["over"] is None for story in (2., 5., 9.9) for live in renderer.state(story)["cells"])
    image = renderer.frame(decision_theatre.DURATION)
    middle = spot(renderer, image, cell, .25, 2.5)
    assert not greenish(middle) and not reddish(middle) and middle != ImageColor.getrgb(quota_line.theme.CARD)
    # The usage sets the chart's height itself and does not stand at the top as if the window were full.
    assert renderer.at(cell, *cell["end"])[1] > cell["py0"] + 10
    tip = tip_of(renderer, cell)
    assert tip.startswith(quota_line._tr("{0}：本期已用 {1}，容量未知，{2}").format("W", quota_line.usd(5), quota_line.reset_text(10)))
    assert quota_line.usd(0) not in tip and " / " not in tip
    assert quota_line._tr("这个窗口被降权") not in tip and quota_line._tr("这个窗口被优先使用") not in tip
    assert data["winner"]["label"] in tip


@pytest.mark.parametrize("used, over", [(50.1, True), (50.0001, True), (50., False), (49.9999, False), (49.9, False)])
def test_the_line_s_colour_the_state_word_and_the_hover_text_are_one_comparison(used, over):
    # F5. Just over the line is over it, everywhere; on the line is not.
    renderer = quota_line.QuotaLineRenderer(quota_line.model(plain({"a": half(used)})), 647, 623, 1.5)
    cell = renderer.cells[0]
    window = cell["window"]
    image = renderer.frame(decision_theatre.DURATION)
    assert cell["runs"][-1]["over"] is over and cell["over"] is over
    assert renderer.state(decision_theatre.DURATION)["cells"][0]["over"] is over
    tone = reddish if over else greenish
    x, y = renderer.at(cell, window["at"], window["used"])
    assert tone(state_pixel(renderer, image, cell)) and tone(pixel(renderer, image, x + 3.2, y))
    assert tone(spot(renderer, image, cell, .25, used / 2))                     # the fitted line itself
    tip = tip_of(renderer, cell)
    assert (quota_line._tr("这个窗口被降权") in tip) is over and (quota_line._tr("这个窗口被优先使用") in tip) is not over
    gap = quota_line.usd(abs(50 - used))
    assert (quota_line._tr("比恢复线多用了 {0}").format(gap) in tip) is over
    assert (quota_line._tr("恢复线下还有 {0} 未用").format(gap) in tip) is not over


def test_with_readings_the_three_still_agree_wherever_the_line_ends():
    # F5. Readings that end on, just over and just under the quota line, and lines that cross it on the way.
    import random
    rng = random.Random(52)
    for _ in range(300):
        capacity, at = rng.choice((20, 100, 400)), rng.choice((.1, .5, .9))
        used = capacity * at + rng.choice((0, 1e-12, -1e-12, 1e-6, -1e-6, .001, -.001, 3, -3)) * rng.choice((1, capacity / 100))
        used = max(0., used)
        moments = sorted(rng.uniform(0, at) for _ in range(rng.randint(0, 5)))
        amounts = sorted(rng.uniform(0, used) for _ in moments)
        window = dict(label="W", capacity_usd=capacity, used_usd=used, elapsed_hours=100 * at, reset_in_hours=100 * (1 - at),
                      samples=[[100 * moment, amount] for moment, amount in zip(moments, amounts)])
        renderer = quota_line.QuotaLineRenderer(quota_line.model(plain({"w": window})), 300, 215, 1.)
        cell = renderer.cells[0]
        over = cell["over"]
        assert over is quota_line.over_line(cell["window"]["at"], used, capacity), (capacity, at, used)
        assert renderer.state(decision_theatre.DURATION)["cells"][0]["over"] is over
        assert (quota_line._tr("这个窗口被降权") in tip_of(renderer, cell)) is over
        stretch = lambda moment: next(run for run in cell["runs"] if moment <= run["ts"][-1])["over"]
        clear = lambda moment, amount: abs(amount - capacity * moment) > 1e-6 * capacity
        # The stretch the line ends on wears that state's colour, unless it ends on the quota line itself.
        if clear(cell["window"]["at"], used):
            assert cell["runs"][-1]["over"] is over
        # While it rises, the word and the colour of the stretch its end is on agree, away from a crossing.
        for step in range(1, 12):
            live = renderer.state(quota_line.PHASES["sweep"][0] + step * .4)["cells"][0]
            if live["pace"] < 1 and clear(*live["head"]):
                assert live["over"] is stretch(live["head"][0]), (capacity, at, used, live["head"])


def test_a_task_that_runs_past_the_reset_spends_only_its_share_before_it_in_this_period():
    # F6. A 5-hour window one hour from its reset; the task runs for five hours and spends $12.
    window = dict(label="5h", capacity_usd=20, used_usd=15, elapsed_hours=4, reset_in_hours=1)
    data = quota_line.model(plain({"w": window}, dict(window="w", hours=5, spend_usd=12)))
    assert data["winner"]["inside"] == pytest.approx(.2) and data["winner"]["spend"] == 12
    renderer = quota_line.QuotaLineRenderer(data, 647, 623, 1.5)
    cell = renderer.cells[0]
    # One hour of the five is spent here: $2.40 up to the reset. It is not pushed into the corner.
    assert cell["end"] == pytest.approx((1., 17.4)) and cell["after"] == pytest.approx(9.6) and not cell["closes"]
    x, y = renderer.at(cell, *cell["end"])
    assert y > renderer.at(cell, 1., 20.)[1] + 10
    before, after = renderer.frame(quota_line.PHASES["spend"][0]), renderer.frame(decision_theatre.DURATION)
    assert accent(spot(renderer, after, cell, .9, 16.2))
    # A mark beyond the reset edge says the task runs on into the next period.
    mark = [(x + dx, y + dy) for dx in (9.5, 10., 10.5) for dy in (-.5, 0., .5)]
    assert any(accent(pixel(renderer, after, *point)) for point in mark)
    assert not any(accent(pixel(renderer, before, *point)) for point in mark)
    tip = tip_of(renderer, cell)
    assert quota_line.usd(12) in tip and quota_line._tr("其中 {0} 在重置之后，计入下一周期").format(quota_line.usd(9.6)) in tip
    # A task that ends before the reset spends all of it here: no mark, and a pace is left to draw.
    data = quota_line.model(plain({"w": window}, dict(window="w", hours=.5, spend_usd=1.2)))
    renderer = quota_line.QuotaLineRenderer(data, 647, 623, 1.5)
    cell = renderer.cells[0]
    assert data["winner"]["inside"] == 1 and cell["after"] == 0 and cell["closes"]
    assert cell["end"] == pytest.approx((.9, 16.2))
    x, y = renderer.at(cell, *cell["end"])
    after = renderer.frame(decision_theatre.DURATION)
    assert not any(accent(pixel(renderer, after, x + dx, y - 6)) for dx in (9.5, 10., 10.5))
    assert quota_line._tr("其中 {0} 在重置之后，计入下一周期").format(quota_line.usd(0)).split("{")[0] not in tip_of(renderer, cell)
    # Spend that takes the usage past the capacity makes the chart taller; it is not cut off at the top.
    data = quota_line.model(plain({"w": {**window, "used_usd": 19}}, dict(window="w", hours=.5, spend_usd=5)))
    renderer = quota_line.QuotaLineRenderer(data, 647, 623, 1.5)
    cell = renderer.cells[0]
    assert cell["scale"] == 24 and cell["end"] == pytest.approx((.9, 24.))
    assert renderer.at(cell, 1., 20.)[1] > renderer.at(cell, *cell["end"])[1] + 10
    # A window that never resets has no reset to run past.
    data = quota_line.model(plain({"w": {**window, "reset_in_hours": None}}, dict(window="w", hours=5, spend_usd=12)))
    assert data["winner"]["inside"] == 1 and quota_line.QuotaLineRenderer(data, 400, 300, 1.).cells[0]["after"] == 0
