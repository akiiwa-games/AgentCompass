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
    """The colour of a candidate's bar at the height of `value` on the N scale."""
    column = next(c for c in renderer.columns if row in c["bars"])
    x0, x1 = column["bars"][row]
    return pixel(renderer, image, (x0 + x1) / 2, renderer.level(value))


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
    # Every recorded window is a clock, in its recorded order, with the candidates that would spend there.
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
    clocks, bars = [(index, "clock") for index in range(4)], [(index, "bars") for index in range(4)]
    tiles = 2 * len(data["windows"]) + 1
    assert set(renderer.tiles) == {*clocks, *bars, "winner"}
    # The tiles cover the canvas exactly once.
    assert sum((x1 - x0) * (y1 - y0) for x0, y0, x1, y1 in renderer.tiles.values()) == size[0] * size[1]
    times = (0., .2, 1.2, 2.7, 3.5, 4.8, 5.6, 6.3, 7.4, 9.0, decision_theatre.DURATION)
    frames, changed = [], []
    for t in times:
        changed.append(renderer.advance(t))
        frames.append(renderer.live.copy())
    assert all(frame.size == size[:2] for frame in frames)
    # The first frame paints every tile once; nothing moves before the first phase.
    assert len(changed[0]) == tiles and changed[1] == []
    # The winner is named only at the end; until then its tile is never painted again.
    assert all("winner" not in names for names in changed[1:9]) and "winner" in changed[9]
    # While the clocks wind up only the clocks are painted; once the bars grow too, both halves of every window.
    assert changed[2] == clocks and sorted(changed[3]) == sorted(clocks + bars)
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
    # Candidates lay their spend one after another, best first: early in the phase only the first two are moving.
    early = plan[0] + .2 * (plan[1] - plan[0])
    renderer.advance(early)
    assert [0 < row["laid"] < 1 for row in state(early)["rows"]] == [True, True, False, False, False, False]
    moved = renderer.advance(early + .02)
    # Claude B (the winner) is laying its spend on its clock, all of it under the line: its bars stand still.
    # Codex (ranked second) is still leaving its bar. Claude A and Gemini wait.
    assert moved == [(1, "clock"), (2, "clock"), (2, "bars")]
    # In the earlier decision no window resets during the task, so no bead is restless between the phases.
    assert not any(renderer.window_state(column, state(early))["resets"] for column in renderer.columns)


def test_while_the_task_runs_a_slow_window_is_repainted_only_when_its_hand_moves_a_pixel():
    renderer = quota_line.QuotaLineRenderer(quota_line.model(snapshot()), 486, 425, 1.15)
    start, end = quota_line.PHASES["run"]
    renderer.advance(start)
    steps = 60
    painted = [name for step in range(1, steps) for name in renderer.advance(start + (end - start) * step / steps)]
    # Gemini: a week-long window, nobody racing on it. Two hours move its hand by a few pixels; its bars stand still.
    assert painted.count((3, "bars")) == 0 and 0 < painted.count((3, "clock")) <= 12
    # Claude A resets during the task: its beads are restless, so its clock is painted on every step.
    assert painted.count((0, "clock")) == steps - 1
    # The picture is the same as one fresh render of the same moment, whatever was skipped on the way.
    assert same(renderer.live, quota_line.QuotaLineRenderer(renderer.data, 486, 425, 1.15).frame(start + (end - start) * (steps - 1) / steps))


@pytest.mark.parametrize("size", SIZES + [(900, 700, 2.), (200, 120, 1.)])
@pytest.mark.parametrize("count", [1, 2, 4])
def test_clocks_bars_and_the_winner_s_name_stay_inside_the_canvas_and_clear_of_each_other(size, count):
    source = snapshot()
    keys = list(source["quota_line"]["windows"])[:count]
    source["quota_line"]["windows"] = {key: source["quota_line"]["windows"][key] for key in keys}
    data = quota_line.model(source)
    assert len(data["windows"]) == count
    renderer = quota_line.QuotaLineRenderer(data, *size)
    reach = renderer.radius + quota_line.LANE_ROOM
    assert renderer.name_y - 6 >= 0 and renderer.cy - reach >= renderer.name_y + 6
    assert renderer.cy + reach < renderer.plot_top < renderer.base_y < renderer.pill_y - 11
    assert renderer.pill_y + 11 <= renderer.bottom + .5
    assert renderer.base_y - renderer.plot_top >= 40          # the bars keep room to differ
    for column in renderer.columns:
        assert column["x0"] >= 0 and column["x1"] <= renderer.right + 1e-6
        assert column["cx"] - reach >= column["x0"] and column["cx"] + reach <= column["x1"]
        bars = sorted(column["bars"].values())
        assert all(column["x0"] < x0 < x1 < column["x1"] for x0, x1 in bars)
        assert all(a[1] < b[0] for a, b in zip(bars, bars[1:]))
        # Each candidate's what-if arc has its own lane between the face and the edge of the window's room.
        radii = [renderer.lane_radius(column, row) for row in column["window"]["rows"]]
        assert all(b - a >= quota_line.LANE for a, b in zip(radii, radii[1:])) and all(
            renderer.radius < r <= reach for r in radii)
    # The type never shrinks below what the width allows unless the canvas is shorter than the layout's minimum.
    if size[1] >= quota_line.MIN_HEIGHT * min(size[2], size[0] / quota_line.NOMINAL[0]):
        assert renderer.s == pytest.approx(min(size[2], size[0] / quota_line.NOMINAL[0]))
    assert renderer.frame(decision_theatre.DURATION).size == size[:2]


def test_the_hand_is_the_recovery_line_and_twelve_is_the_reset():
    renderer = quota_line.QuotaLineRenderer(quota_line.model(snapshot()), 647, 623, 1.5)
    a, b = renderer.columns[0], renderer.columns[1]
    now = renderer.state(quota_line.PHASES["tie"][0])
    face = renderer.window_state(a, now)
    # Each window on its own clock: Claude A is two hours from its reset, Claude B a day into its week.
    assert face["hand"] == pytest.approx(166 / 168) and face["used"] == pytest.approx(240 / 400)
    assert renderer.window_state(b, now)["hand"] == pytest.approx(24 / 168)
    assert face["resets"] and not renderer.window_state(b, now)["resets"]
    # While the clocks wind up, hand and spend turn together from twelve.
    half = renderer.window_state(a, renderer.state(sum(quota_line.PHASES["clock"]) / 2))
    assert half["hand"] / half["used"] == pytest.approx(face["hand"] / face["used"]) and 0 < half["hand"] < face["hand"]
    # The task runs: the clocks move by its two hours. Claude A's hand reaches twelve, and what it has
    # not spent by then is lost; the winner's fast run has turned part of it into spend first.
    done = renderer.window_state(a, renderer.state(quota_line.PHASES["run"][1]))
    assert done["hand"] == pytest.approx(1.) and done["lost"] == 1
    assert done["eaten"] == done["spent"] == pytest.approx(demo.FAST_PRICE * 22.86 * demo.HOURS / 400)
    assert done["used"] + done["spent"] < 1                                       # some beads were not rescued
    later = renderer.window_state(b, renderer.state(quota_line.PHASES["run"][1]))
    assert later["hand"] == pytest.approx(26 / 168) and later["lost"] == 0 and later["spent"] == 0
    # A point on the face: twelve is straight up, a quarter turn is to the right.
    assert renderer.on_face(a, 0., 10) == pytest.approx((a["cx"], renderer.cy - 10))
    assert renderer.on_face(a, .25, 10) == pytest.approx((a["cx"] + 10, renderer.cy))
    # A window that never resets has no moving hand.
    source = snapshot()
    source["quota_line"]["windows"]["codex"]["reset_in_hours"] = None
    still = quota_line.QuotaLineRenderer(quota_line.model(source), 647, 623, 1.5)
    assert still.window_state(still.columns[2], still.state(9.))["hand"] == 0


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


def test_over_line_spend_shrinks_its_candidate_s_bar_at_the_same_moment_and_in_proportion():
    data = quota_line.model(snapshot())
    renderer = quota_line.QuotaLineRenderer(data, 647, 623, 1.5)
    sol = next(i for i, row in enumerate(data["rows"]) if row["id"] == "sol_high")
    row = data["rows"][sol]
    assert row["over"] == row["spend"] > 0 and row["fit"] > row["score"]
    before = renderer.state(quota_line.PHASES["plan"][0])
    assert all(r["paid"] == 0 and r["laid"] == 0 for r in before["rows"])
    # The bar loses exactly the share of β·X/h that matches the share of the over-line arc already laid.
    samples = [renderer.state(quota_line.PHASES["plan"][0] + i * .02)["rows"][sol] for i in range(120)]
    moving = [s for s in samples if 0 < s["paid"] < 1]
    assert len(moving) > 8
    assert all(s["paid"] == pytest.approx(s["drawn"]) for s in moving)        # every dollar of it is over the line
    assert [s["paid"] for s in samples] == sorted(s["paid"] for s in samples) and samples[-1]["paid"] == 1
    # A candidate whose spend stays behind the hand pays nothing, at any moment.
    assert all(renderer.state(quota_line.PHASES["plan"][0] + i * .02)["rows"][0]["paid"] == 0 for i in range(120))
    # In the picture: the slice between N and F leaves Sol's bar and is hot while it drains; the winner's bar is untouched.
    lost = (row["fit"] + row["score"]) / 2
    start, end = renderer.frame(quota_line.PHASES["plan"][0]), renderer.frame(quota_line.PHASES["plan"][1])
    assert bar_pixel(renderer, start, sol, lost) == ImageColor.getrgb(quota_line.OTHER)
    scar = bar_pixel(renderer, end, sol, lost)
    assert scar[0] > 235 and scar[0] > scar[1] + 20 and scar[0] > scar[2] + 20          # red, not the bar's colour
    half = renderer.frame(next(quota_line.PHASES["plan"][0] + i * .02 for i, s in enumerate(samples) if s["paid"] > .9))
    assert bar_pixel(renderer, half, sol, row["fit"] - (row["fit"] - row["score"]) * .3)[0] > 235
    winner = (data["rows"][0]["fit"] * .98)
    assert bar_pixel(renderer, start, 0, winner) == bar_pixel(renderer, end, 0, winner)


def test_tied_candidates_race_and_the_faster_one_is_named_first():
    data = quota_line.model(snapshot())
    renderer = quota_line.QuotaLineRenderer(data, 647, 623, 1.5)
    run = quota_line.PHASES["run"]
    middle = renderer.state(run[0] + .5 * (run[1] - run[0]))["rows"]
    # Equal N, different speed: fast mode is three quarters through when the others are half way.
    assert [row["done"] for row in middle[:3]] == pytest.approx([.75, .5, .5], abs=.01) and all(row["done"] == 0 for row in middle[3:])
    assert middle[0]["won"] == 0
    # The winner is marked when its own run completes, before the slower ones finish.
    late = renderer.state(run[0] + .85 * (run[1] - run[0]))["rows"]
    assert late[0]["done"] == 1 and late[0]["won"] == 1 and late[1]["done"] < 1 and late[1]["won"] == 0
    frame = renderer.frame(run[0] + .85 * (run[1] - run[0]))
    assert bar_pixel(renderer, frame, 0, data["rows"][0]["score"] * .5) == ImageColor.getrgb(quota_line.WINNER)
    # Settled: only the winner keeps the accent, and its name sits in the strip under the bars.
    final = renderer.frame(decision_theatre.DURATION)
    assert bar_pixel(renderer, final, 0, .3) == ImageColor.getrgb(quota_line.WINNER)
    assert bar_pixel(renderer, final, 1, .3) == ImageColor.getrgb(quota_line.SAME_MODEL_QUIET)
    assert bar_pixel(renderer, final, 3, .3) == ImageColor.getrgb(quota_line.OTHER_QUIET)
    x0, y0, x1, y1 = renderer.tiles["winner"]
    paper = ImageColor.getrgb(quota_line.theme.CARD)
    assert final.crop((x0, y0, x1, y1)).getcolors(10 ** 6) != [((x1 - x0) * (y1 - y0), paper)]
    assert renderer.frame(run[1]).crop((x0, y0, x1, y1)).getcolors() == [((x1 - x0) * (y1 - y0), paper)]
    # Without a tie only the winner runs.
    alone = quota_line.QuotaLineRenderer(quota_line.model(earlier()), 647, 623, 1.5)
    assert [row["done"] > 0 for row in alone.state(run[1])["rows"]] == [True] + [False] * 5


def test_the_picture_carries_at_most_a_handful_of_short_labels(monkeypatch):
    data = quota_line.model(snapshot())
    renderer = quota_line.QuotaLineRenderer.__new__(quota_line.QuotaLineRenderer)
    written = []
    real = quota_line.QuotaLineRenderer.text
    monkeypatch.setattr(quota_line.QuotaLineRenderer, "text",
                        lambda self, value, *a, **k: written.append(str(value)) or real(self, value, *a, **k))
    renderer.__init__(data, 647, 623, 1.5)
    renderer.frame(decision_theatre.DURATION)
    # The window names and the winner: nothing else is written, at any moment of the film.
    assert written == ["Claude A", "Claude B", "Codex", "Gemini", data["rows"][0]["label"]]
    written.clear()
    for i in range(100):
        renderer.frame(i / 10)
    assert set(written) == {data["rows"][0]["label"]}


def test_hover_text_says_what_the_mark_under_the_pointer_stands_for():
    data = quota_line.model(snapshot())
    renderer = quota_line.QuotaLineRenderer(data, 647, 623, 1.5)
    s = renderer.s
    a, codex = renderer.columns[0], renderer.columns[2]
    over_clock = renderer.tip_at(a["cx"] * s, renderer.cy * s)
    assert "Claude A" in over_clock and "$240" in over_clock and "$400" in over_clock
    assert quota_line.reset_text(2) in over_clock and "$155" in over_clock            # 400·166/168 − 240 still unspent
    assert "$" in renderer.tip_at(codex["cx"] * s, renderer.cy * s)
    x0, x1 = a["bars"][0]
    over_bar = renderer.tip_at((x0 + x1) / 2 * s, renderer.level(.3) * s)
    assert data["rows"][0]["label"] in over_bar and "0.667" in over_bar and "Claude A" in over_bar
    sol = next(i for i, row in enumerate(data["rows"]) if row["id"] == "sol_high")
    x0, x1 = codex["bars"][sol]
    assert "0.616" in renderer.tip_at((x0 + x1) / 2 * s, renderer.level(.3) * s)
    assert renderer.tip_at(2, renderer.plot_top * s) == ""


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
        assert moved and "winner" not in moved and moved < set(view.renderer.tiles)
        for name in moved:
            x0, y0, x1, y1 = view.renderer.tiles[name]
            assert (view.strips[name][0].width(), view.strips[name][0].height()) == (x1 - x0, y1 - y0)
            assert view.canvas.coords(view.strips[name][1]) == [float(x0), float(y0)]
        for story in (9.45, 9.6, decision_theatre.DURATION, decision_theatre.DURATION):
            view.follow(story)                           # after the last phase: one settled frame, then nothing
        assert frames == [5., 5.04, 9.45]
        assert set(view.strips) == set(view.renderer.tiles)
        assert view.tip_at(view.renderer.columns[0]["cx"] * view.renderer.s, view.renderer.cy * view.renderer.s)
        view.show(snapshot())
        assert frames[-1] == decision_theatre.DURATION and len(frames) == 4
        # The next decision starts from a full image again; the old strips no longer cover it.
        assert all(view.canvas.itemcget(item, "state") == "hidden" for _, item in view.strips.values())
        view.follow(5.)
        assert view.canvas.itemcget(view.strips[0, "clock"][1], "state") == "normal"
        monkeypatch.setattr(view.canvas, "winfo_width", lambda: 300)
        view.configure(None)                             # a new size drops them
        assert not view.strips and view.photo.width() == 300
    finally:
        view.canvas.destroy()
        reset_tk_root(root)
