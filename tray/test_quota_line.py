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


def bar_pixel(renderer, image, row, value):
    """The pixel of a candidate's score bar at `value` on the scores' scale."""
    block = next(block for block in renderer.blocks if row in block["rows"])
    return pixel(renderer, image, renderer.sx(value), block["rows"][row])


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
    # Three candidates tie on N: the earliest-expiring under-line quota comes first, and on it the faster run.
    assert [row["id"] for row in rows[:3]] == ["opus55_high_fast", "opus55_high", "opus55_high_b"]
    assert [row["quota_line"]["decided_by"] for row in rows[:4]] == ["N", "faster", "earliest_expiry", "N"]
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
    ranking = source["ranking"][:quota_line.ROWS]
    assert [row["id"] for row in data["rows"]] == [row["id"] for row in ranking]
    assert [(row["fit"], row["score"]) for row in data["rows"]] == [(row["components"]["fit"], row["score"]) for row in ranking]
    assert data["winner"] == 0 and data["tied"] == 3 and data["raced"] == [0, 1, 2]
    assert [row["decided_by"] for row in data["rows"]] == [None, "faster", "earliest_expiry", None, None, None]
    # The clocks run for the slowest of the tied candidates; fast mode needs less of that time.
    assert data["span"] == demo.HOURS and data["rows"][0]["hours"] == pytest.approx(demo.HOURS / demo.FAST_SPEED, abs=.01)
    assert data["rows"][0]["fast"] and not data["rows"][1]["fast"]
    # Every recorded window is a chart, in its recorded order, with the candidates that would spend there.
    assert [window["key"] for window in data["windows"]] == ["claude-a", "claude-b", "codex", "gemini"]
    assert [window["rows"] for window in data["windows"]] == [[0, 1, 4], [2], [3], [5]]
    first, codex = data["windows"][0], data["windows"][2]
    assert first["at"] == pytest.approx(166 / 168) and first["period"] == 168 and first["reset"] == 2
    assert first["line"] > first["used"] and codex["line"] < codex["used"]
    # Ability beyond the need, in the units of F: Opus is far above the task's low need for divergence and vision.
    opus = demo.HEADLINE_SPECS[0][5]
    targets = [demo.ANCHOR * level / 3 for level in demo.DEMAND.values()]
    spare = sum(d * max(0, s - t) for d, s, t in zip(demo.DEMAND.values(), opus, targets)) / sum(demo.DEMAND.values())
    assert data["rows"][0]["spare"] == pytest.approx(spare, abs=1e-4) and spare > .1
    assert data["top"] == pytest.approx(max(row["fit"] + row["spare"] for row in data["rows"]))
    # The same accounts keep their order when another decision is replayed; nothing ties there.
    before = quota_line.model(earlier())
    assert [w["key"] for w in before["windows"]] == ["claude-a", "claude-b", "codex", "gemini"]
    assert before["tied"] == 0 and before["raced"] == [0] and before["rows"][0]["id"] == demo.EARLIER_WINNER
    # The same model and spend: over the line on the window that is ahead, under it on the other account.
    a, b = (next(r for r in before["rows"] if r["id"] == key) for key in ("opus55_high", "opus55_high_b"))
    assert a["spend"] == b["spend"] == a["over"] and b["over"] == 0


def test_decisions_without_a_quota_line_record_show_an_empty_state_not_invented_numbers():
    source = snapshot()
    assert quota_line.model({}) is None and quota_line.model(None) is None
    assert quota_line.model({**source, "quota_line": None}) is None
    stripped = {**source, "ranking": [{k: v for k, v in row.items() if k != "quota_line"} for row in source["ranking"]]}
    assert quota_line.model(stripped) is None
    unknown = copy.deepcopy(source)
    unknown["ranking"][0]["quota_line"]["window"] = "missing"
    assert [row["id"] for row in quota_line.model(unknown)["rows"]][0] == source["ranking"][1]["id"]
    # A row without per-axis fit has no uncounted ability to show; a chosen bundle that is not ranked first is still the winner.
    bare = copy.deepcopy(source)
    del bare["ranking"][0]["five_axis_fit"]
    bare["choice"] = bare["ranking"][3]["id"]
    data = quota_line.model(bare)
    assert data["rows"][0]["spare"] == 0 and data["winner"] == 3 and data["tied"] == 0 and data["raced"] == [3]
    renderer = quota_line.QuotaLineRenderer(None, 400, 300, 1.)
    assert renderer.frame(3.).size == (400, 300) and same(renderer.frame(3.), renderer.frame(9.))
    assert renderer.repaints == 0 and renderer.tip_at(200, 150) == ""


@pytest.mark.parametrize("size", SIZES)
def test_frames_render_without_a_display_and_repaint_only_the_tiles_that_moved(size):
    data = quota_line.model(earlier())
    renderer = quota_line.QuotaLineRenderer(data, *size)
    base = renderer.base.copy()
    charts, names, bars = ([(index, part) for index in range(4)] for part in ("chart", "names", "bars"))
    assert set(renderer.tiles) == {*charts, *names, *bars}
    # The tiles lie side by side across the whole width and leave the margins above and below the blocks alone.
    boxes = sorted(renderer.tiles.values(), key=lambda box: (box[1], box[0]))
    assert all(a[3] == b[1] for a, b in zip(boxes[::3], boxes[3::3]))
    assert all(left[0] == 0 and left[2] == mid[0] and mid[2] == right[0] and right[2] == size[0]
               for left, mid, right in zip(boxes[::3], boxes[1::3], boxes[2::3]))
    assert 0 <= boxes[0][1] and boxes[-1][3] <= size[1]
    times = (0., .2, 1.2, 2.7, 3.5, 4.8, 5.6, 6.3, 7.4, 9.0, decision_theatre.DURATION)
    frames, changed = [], []
    for t in times:
        changed.append(renderer.advance(t))
        frames.append(renderer.live.copy())
    assert all(frame.size == size[:2] for frame in frames)
    # The first frame paints every tile once; nothing moves before the first phase.
    assert len(changed[0]) == len(renderer.tiles) and changed[1] == []
    # While the lines are drawn up to now the charts move and the names come in; while the ability beyond the
    # need is cut off, only the scores move.
    assert sorted(changed[2]) == sorted(charts + names + bars) and sorted(changed[4]) == sorted(bars)
    for again in (decision_theatre.DURATION, quota_line.LAST, 9.6):
        assert renderer.advance(again) == [] and same(renderer.live, frames[-1])
    # Every phase adds marks, the caller owns each frame, and the static base is never drawn on.
    assert all(not same(a, b) for a, b in zip(frames[1:], frames[2:]))
    assert renderer.frame(5.) is not renderer.frame(5.)
    assert same(base, renderer.base)
    # Tile-by-tile painting in any order gives the same picture as one fresh render of that moment.
    for t in (8.4, 3.5, 6.6, 0.):
        assert same(renderer.frame(t), quota_line.QuotaLineRenderer(data, *size).frame(t)), t


def test_a_window_that_is_not_moving_is_not_repainted():
    renderer = quota_line.QuotaLineRenderer(quota_line.model(earlier()), 486, 425, 1.15)
    state = renderer.state
    plan = quota_line.PHASES["plan"]
    # Candidates project their spend one after another, best first: early in the phase only the first two are moving.
    early = plan[0] + .2 * (plan[1] - plan[0])
    renderer.advance(early)
    assert [0 < row["laid"] < 1 for row in state(early)["rows"]] == [True, True, False, False, False, False]
    moved = renderer.advance(early + .02)
    # Claude B (the winner) projects its spend, all of it under the forecast: its score stands still.
    # Codex (ranked second) is over its forecast, which drains its score. Claude A and Gemini wait.
    # The specks of a drain cross the names on their way from the chart to the score.
    assert moved == [(1, "chart"), (2, "chart"), (2, "names"), (2, "bars")]


def test_while_the_task_runs_a_slow_window_is_repainted_only_when_its_clock_moves_a_pixel():
    renderer = quota_line.QuotaLineRenderer(quota_line.model(snapshot()), 486, 425, 1.15)
    start, end = quota_line.PHASES["run"]
    renderer.advance(start)
    steps = 60
    painted = [name for step in range(1, steps) for name in renderer.advance(start + (end - start) * step / steps)]
    # Gemini: a week-long window, nobody racing on it. Two hours move its "now" by a few pixels; its scores stand still.
    assert painted.count((3, "bars")) == painted.count((3, "names")) == 0 and 0 < painted.count((3, "chart")) <= 12
    # The winner's score fills while the task runs; its name is not drawn again for that.
    assert painted.count((0, "bars")) > 20 and painted.count((0, "names")) <= 2
    # Claude A is being spent from by the winner: its usage line rises while the task runs.
    assert painted.count((0, "chart")) > 3 * painted.count((3, "chart"))
    # The picture is the same as one fresh render of the same moment, whatever was skipped on the way.
    assert same(renderer.live, quota_line.QuotaLineRenderer(renderer.data, 486, 425, 1.15).frame(start + (end - start) * (steps - 1) / steps))


@pytest.mark.parametrize("size", SIZES + [(900, 700, 2.), (200, 120, 1.)])
@pytest.mark.parametrize("count", [1, 2, 4])
def test_charts_names_and_scores_stay_inside_the_canvas_and_clear_of_each_other(size, count):
    source = snapshot()
    keys = list(source["quota_line"]["windows"])[:count]
    source["quota_line"]["windows"] = {key: source["quota_line"]["windows"][key] for key in keys}
    data = quota_line.model(source)
    assert len(data["windows"]) == count
    renderer = quota_line.QuotaLineRenderer(data, *size)
    # Three columns, left to right: charts, names, scores.
    assert 0 < renderer.cx0 < renderer.cx1 < renderer.nx0 < renderer.sx0 < renderer.sx1 <= renderer.right
    assert renderer.cx1 - renderer.cx0 >= 75 and renderer.sx0 - renderer.nx0 >= 56 and renderer.sx1 - renderer.sx0 >= 50
    assert renderer.pitch >= renderer.bar_h + 2 >= 8
    edges = []
    for block in renderer.blocks:
        assert block["top"] >= 0 and block["bottom"] <= renderer.bottom + 1e-6
        # The window's name stands above its chart; the chart is at least two rows tall.
        assert block["top"] < block["label"] < block["y0"] < block["y1"] <= block["bottom"]
        assert block["y1"] - block["y0"] >= quota_line.MIN_LINES * renderer.pitch - 6 - 1e-6
        # A window's candidates stand beside its own chart, one row each, inside the chart's height.
        ys = [block["rows"][row] for row in block["window"]["rows"]]
        assert ys == sorted(ys) and all(b - a == pytest.approx(renderer.pitch) for a, b in zip(ys, ys[1:]))
        assert all(block["y0"] - 3 <= y <= block["y1"] + 3 for y in ys)
        edges += [block["top"], block["bottom"]]
    assert edges == sorted(edges)
    # The type never shrinks below what the width allows unless the canvas is shorter than the layout's minimum.
    if size[1] >= quota_line.MIN_HEIGHT * min(size[2], size[0] / quota_line.NOMINAL[0]):
        assert renderer.s == pytest.approx(min(size[2], size[0] / quota_line.NOMINAL[0]))
    assert renderer.frame(decision_theatre.DURATION).size == size[:2]


def greenish(color):
    return color[1] > color[0] + 25 and color[1] > color[2]


def reddish(color):
    return color[0] > color[1] + 25 and color[0] > color[2] + 25


def test_each_chart_is_the_window_s_own_clock_with_the_forecast_and_the_usage_line():
    data = quota_line.model(snapshot())
    renderer = quota_line.QuotaLineRenderer(data, 647, 623, 1.5)
    image = renderer.frame(quota_line.PHASES["clock"][1])
    paper = ImageColor.getrgb(quota_line.theme.CARD)
    for block in renderer.blocks:
        window = block["window"]
        # Left to right is the period: now is where the usage line ends, at the share of the period that has passed.
        assert renderer.tx(window["at"]) == pytest.approx(renderer.cx0 + (renderer.cx1 - renderer.cx0) * window["at"])
        # Bottom to top is the quota: nothing at the floor, the whole capacity at the top.
        assert renderer.uy(block, 0.) == block["y1"] and renderer.uy(block, window["capacity"]) == block["y0"]
        # The forecast runs straight from the period's start to the whole capacity at the reset.
        for moment in (.25, .5, .75):
            spot = pixel(renderer, image, renderer.tx(moment), renderer.uy(block, window["capacity"] * moment))
            assert spot != paper, (window["key"], moment)
        # The dot of "now" sits on the usage line's end.
        assert sum(pixel(renderer, image, renderer.tx(window["at"]), renderer.uy(block, window["used"]))) < 200
    # Each window is at its own moment of its own period.
    assert len({round(block["window"]["at"], 3) for block in renderer.blocks}) == 4
    # Claude A is under its forecast two hours before its reset: the gap at now is quota about to be lost (green).
    def gap(block):
        # Between the usage line's end and the forecast, clear of the dot of "now" and of the forecast line itself.
        window = block["window"]
        return [pixel(renderer, image, renderer.tx(window["at"]),
                      renderer.uy(block, window["used"] + (window["line"] - window["used"]) * share))
                for share in (.4, .5, .6, .7, .8)]

    assert any(greenish(spot) for spot in gap(renderer.blocks[0])) and not any(reddish(spot) for spot in gap(renderer.blocks[0]))
    # Codex is over its forecast: the gap is debt (pink).
    assert any(reddish(spot) for spot in gap(renderer.blocks[2])) and not any(greenish(spot) for spot in gap(renderer.blocks[2]))


def test_the_usage_line_is_recorded_readings_or_a_fit_that_is_drawn_as_a_fit():
    source = snapshot()
    recorded = source["quota_line"]["windows"]
    assert "samples" in recorded["claude-a"] and "samples" not in recorded["gemini"]
    data = quota_line.model(source)
    a, gemini = data["windows"][0], data["windows"][3]
    # Recorded: the readings as they are (as moments of the period), closed by the decision's own reading.
    assert a["readings"] == len(recorded["claude-a"]["samples"]) and a["fitted"] == 1
    assert a["path"][0] == (0., 0.) and a["path"][-1] == (a["at"], a["used"])
    assert a["path"][1] == (pytest.approx(recorded["claude-a"]["samples"][0][0] / 168), recorded["claude-a"]["samples"][0][1])
    assert [u for _, u in a["path"]] == sorted(u for _, u in a["path"])
    # No readings: a straight fit from the period's start to the current usage, marked as fitted.
    assert gemini["readings"] == 0 and gemini["fitted"] == 1 and gemini["path"] == [(0., 0.), (gemini["at"], gemini["used"])]
    # One reading is not a line, readings after the decision or malformed ones are ignored: still a fit.
    for samples in ([[10, 20]], [[500, 20], [600, 30]], [["x", 1], [1], None], "n/a"):
        broken = copy.deepcopy(source)
        broken["quota_line"]["windows"]["claude-a"]["samples"] = samples
        assert quota_line.model(broken)["windows"][0]["readings"] == 0
    # On the picture a fitted line is dotted (paper shows through along it); a recorded one is solid.
    renderer = quota_line.QuotaLineRenderer(data, 647, 623, 1.5)
    image = renderer.frame(quota_line.PHASES["clock"][1])
    paper = ImageColor.getrgb(quota_line.theme.CARD)

    def inked(block, t0, t1):
        window = block["window"]
        shares = [t0 + (t1 - t0) * step / 80 for step in range(81)]
        return [pixel(renderer, image, renderer.tx(t), renderer.uy(block, renderer.usage(window, t))) != paper for t in shares]

    fitted = inked(renderer.blocks[3], .15, gemini["at"] * .9)
    assert any(fitted) and not all(fitted)
    (t0, _), (t1, _) = a["path"][3], a["path"][4]
    assert all(inked(renderer.blocks[0], t0, t1))
    # The hover text says which it is.
    s = renderer.s
    assert str(a["readings"]) in renderer.tip_at(renderer.tx(.5) * s, renderer.blocks[0]["y0"] * s)
    assert renderer.tip_at(renderer.tx(.5) * s, renderer.blocks[3]["y0"] * s) != renderer.tip_at(renderer.tx(.5) * s, renderer.blocks[0]["y0"] * s)


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


def test_ability_beyond_the_need_is_cut_off_and_left_pale():
    renderer = quota_line.QuotaLineRenderer(quota_line.model(snapshot()), 647, 623, 1.5)
    row = renderer.data["rows"][0]
    inside, beyond = row["fit"] * .6, row["fit"] + row["spare"] * .5
    grown, cut = renderer.frame(quota_line.PHASES["cap"][0]), renderer.frame(quota_line.PHASES["cap"][1])
    solid = ImageColor.getrgb(quota_line.SAME_MODEL)
    # First the bar shows the whole ability in one colour; then the part beyond the need fades, the rest stays.
    assert bar_pixel(renderer, grown, 0, inside) == bar_pixel(renderer, grown, 0, beyond) == solid
    assert bar_pixel(renderer, cut, 0, inside) == solid
    pale = bar_pixel(renderer, cut, 0, beyond)
    assert pale != solid and sum(pale) > sum(solid) + 150 and pale != ImageColor.getrgb(quota_line.theme.CARD)


def test_a_projection_is_green_under_the_forecast_and_red_over_it_and_the_red_part_drains_the_score():
    data = quota_line.model(earlier())
    renderer = quota_line.QuotaLineRenderer(data, 647, 623, 1.5)
    rows = data["rows"]
    winner = data["winner"]
    codex = next(i for i, row in enumerate(rows) if row["window_label"] == "Codex")
    # The winner's spend stays under its window's forecast: it never crosses, and nothing is taken off its score.
    assert rows[winner]["over"] == 0 and rows[winner]["cross"] == 1
    # Codex is already over its forecast: every dollar of the projection is over it.
    assert rows[codex]["over"] == rows[codex]["spend"] and rows[codex]["cross"] == 0
    # A window under its forecast whose candidate spends more than the gap: the projection crosses part way.
    window = dict(period=168., line=300., used=280., capacity=400.)
    row = dict(spend=60., over=60. - (400 * (126 + 2) / 168 - 280))
    assert 0 < row["over"] < row["spend"]
    cross = quota_line.crossing(window, row, 2.)
    assert cross == pytest.approx(20 / (60 - 400 * 2 / 168))
    # At the crossing the projection is exactly on the forecast, and at its end exactly X over it.
    assert 280 + 60 * cross == pytest.approx(300 + 400 * 2 / 168 * cross)
    assert 280 + 60 - (300 + 400 * 2 / 168) == pytest.approx(row["over"])
    assert quota_line.crossing(window, dict(spend=60., over=0.), 2.) == 1
    assert quota_line.crossing({**window, "period": None}, row, 2.) == pytest.approx((60 - row["over"]) / 60)
    # The score loses its end while the red part is drawn, in proportion to how much of it is drawn.
    plan = quota_line.PHASES["plan"]
    block = next(block for block in renderer.blocks if codex in block["rows"])
    cost = rows[codex]["fit"] - rows[codex]["score"]
    seen = []
    for step in range(0, 41):
        story = plan[0] + (plan[1] - plan[0]) * step / 40
        state = renderer.state(story)
        live = state["rows"][codex]
        assert live["paid"] == pytest.approx(live["drawn"])            # all of Codex's projection is over the forecast
        assert renderer.heights(codex, state)[0] == pytest.approx(rows[codex]["fit"] - cost * live["paid"])
        assert state["rows"][winner]["paid"] == 0
        seen.append(live["paid"])
        if live["laid"] < 1 and live["drawn"] > .6:
            # The head of the projection is on the chart, above the point of now (clear of its dot), and red.
            x, y = renderer.projected(block, rows[codex], live["drawn"])
            assert y < renderer.uy(block, block["window"]["used"]) + 1e-6
            assert reddish(pixel(renderer, renderer.frame(story), x, y)), step
    assert seen == sorted(seen) and seen[0] == 0 and seen[-1] == 1
    done = renderer.frame(plan[1])
    assert bar_pixel(renderer, done, codex, rows[codex]["score"] * .5) == ImageColor.getrgb(quota_line.OTHER)
    scar = bar_pixel(renderer, done, codex, (rows[codex]["score"] + rows[codex]["fit"]) / 2)
    assert scar != ImageColor.getrgb(quota_line.OTHER) and reddish(scar)


def test_tied_candidates_race_the_winner_s_spend_becomes_usage_and_an_unspent_gap_is_lost_at_the_reset():
    data = quota_line.model(snapshot())
    renderer = quota_line.QuotaLineRenderer(data, 647, 623, 1.5)
    assert data["tied"] == 3 and data["raced"] == [0, 1, 2] and data["winner"] == 0
    start, end = quota_line.PHASES["run"]
    half = renderer.state(start + (end - start) * .5)
    # Equal N: the fast candidate has run further than the others at the same moment.
    assert half["rows"][0]["done"] > half["rows"][1]["done"] == half["rows"][2]["done"] > 0
    block, a = renderer.blocks[0], renderer.blocks[0]["window"]
    before, after = renderer.window_state(block, renderer.state(start)), renderer.window_state(block, renderer.state(end))
    # Time passes on the window's own clock: two hours take Claude A to its reset.
    assert before["now"] == pytest.approx(a["at"]) and after["now"] == 1 and after["resets"]
    # The winner's spend is now usage; what is still under the forecast at the reset is lost.
    assert after["used"] == pytest.approx(a["used"] + data["rows"][0]["spend"]) and after["used"] < a["capacity"]
    assert before["lost"] == 0 and after["lost"] == 1
    settled = renderer.frame(decision_theatre.DURATION)
    gap = pixel(renderer, settled, renderer.tx(1.) - 1, renderer.uy(block, (after["used"] + a["capacity"]) / 2))
    assert not greenish(gap) and gap != ImageColor.getrgb(quota_line.theme.CARD)
    # A window nobody spends from only moves along its clock: its usage stays where it was.
    other = renderer.blocks[3]
    moved = renderer.window_state(other, renderer.state(end))
    assert moved["used"] == other["window"]["used"] and other["window"]["at"] < moved["now"] < 1 and not moved["resets"]
    # The winner's row lights up; its name is the only one in the accent colour.
    y = block["rows"][0]
    assert pixel(renderer, settled, renderer.nx0 - 4, y) == ImageColor.getrgb(quota_line.theme.ACCENT_SOFT)
    assert pixel(renderer, settled, renderer.nx0 - 4, block["rows"][1]) == ImageColor.getrgb(quota_line.theme.CARD)


def test_the_picture_carries_only_names(monkeypatch):
    data = quota_line.model(snapshot())
    renderer = quota_line.QuotaLineRenderer.__new__(quota_line.QuotaLineRenderer)
    written = []
    real = quota_line.QuotaLineRenderer.text
    monkeypatch.setattr(quota_line.QuotaLineRenderer, "text",
                        lambda self, value, *a, **k: written.append(str(value)) or real(self, value, *a, **k))
    renderer.__init__(data, 647, 623, 1.5)
    # The window names are part of the still picture.
    assert written == ["Claude A", "Claude B", "Codex", "Gemini"]
    written.clear()
    for i in range(100):
        renderer.frame(i / 10)
    # While it plays, the only other words are the candidates' model names: no formula, no legend, no numbers.
    names = {row["name"] for row in data["rows"]}
    assert set(written) == names and len(names) <= quota_line.ROWS
    assert all(len(name) <= 12 and not any(sign in name for sign in "$=%") for name in names)


def test_hover_text_says_what_the_chart_or_the_row_under_the_pointer_stands_for():
    data = quota_line.model(snapshot())
    renderer = quota_line.QuotaLineRenderer(data, 647, 623, 1.5)
    s = renderer.s
    a, codex = renderer.blocks[0], renderer.blocks[2]
    over_chart = renderer.tip_at(renderer.tx(.5) * s, (a["y0"] + a["y1"]) / 2 * s)
    assert "Claude A" in over_chart and "$240" in over_chart and "$400" in over_chart
    assert quota_line.reset_text(2) in over_chart and "$155" in over_chart            # 400·166/168 − 240 still unspent
    assert "$" in renderer.tip_at(12, codex["label"] * s)
    over_bar = renderer.tip_at(renderer.sx(.3) * s, a["rows"][0] * s)
    assert data["rows"][0]["label"] in over_bar and "0.667" in over_bar and "Claude A" in over_bar
    sol = next(i for i, row in enumerate(data["rows"]) if row["id"] == "sol_high")
    assert "0.616" in renderer.tip_at((renderer.nx0 + 5) * s, codex["rows"][sol] * s)
    assert renderer.tip_at(2, 1) == "" and renderer.tip_at(2, (a["bottom"] + renderer.blocks[1]["top"]) / 2 * s) == ""


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
        assert set(view.strips) == set(view.renderer.tiles)
        assert view.tip_at(10, view.renderer.blocks[0]["y0"] * view.renderer.s)
        tiles = dict(view.renderer.tiles)
        view.show(snapshot())
        assert frames[-1] == decision_theatre.DURATION and len(frames) == 4
        # The next decision starts from a full image again; the old strips no longer cover it.
        assert all(view.canvas.itemcget(item, "state") == "hidden" for _, item in view.strips.values())
        view.follow(5.)
        assert view.canvas.itemcget(view.strips[1, "chart"][1], "state") == "normal"
        # The new decision cuts its tiles at other places: every strip that is shown again sits on its new tile.
        assert view.renderer.tiles != tiles
        for name, (_, item) in view.strips.items():
            if view.canvas.itemcget(item, "state") == "normal":
                assert view.canvas.coords(item) == [float(v) for v in view.renderer.tiles[name][:2]], name
        monkeypatch.setattr(view.canvas, "winfo_width", lambda: 300)
        view.configure(None)                             # a new size drops them
        assert not view.strips and view.photo.width() == 300
    finally:
        view.canvas.destroy()
        reset_tk_root(root)
