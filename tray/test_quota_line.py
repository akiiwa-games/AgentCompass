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
    names, tracks, bars = ([(index, part) for index in range(4)] for part in ("names", "track", "bars"))
    assert set(renderer.tiles) == {*names, *tracks, *bars}
    # The tiles lie side by side across the whole width and leave the margins above and below the blocks alone.
    boxes = sorted(renderer.tiles.values(), key=lambda box: (box[1], box[0]))
    assert all(a[3] == b[1] for a, b in zip(boxes[::3], boxes[3::3]))
    assert all([box[0] for box in row] == [0, row[0][2], row[1][2]] and row[2][2] == size[0]
               for row in zip(boxes[::3], boxes[1::3], boxes[2::3]))
    assert 0 <= boxes[0][1] and boxes[-1][3] <= size[1]
    assert sum((x1 - x0) * (y1 - y0) for x0, y0, x1, y1 in boxes) <= size[0] * size[1]
    times = (0., .2, 1.2, 2.7, 3.5, 4.8, 5.6, 6.3, 7.4, 9.0, decision_theatre.DURATION)
    frames, changed = [], []
    for t in times:
        changed.append(renderer.advance(t))
        frames.append(renderer.live.copy())
    assert all(frame.size == size[:2] for frame in frames)
    # The first frame paints every tile once; nothing moves before the first phase.
    assert len(changed[0]) == len(renderer.tiles) and changed[1] == []
    # While the lines run up to now the names come in and the tracks are painted; then the bars grow as well.
    assert sorted(changed[2]) == sorted(names + tracks) and sorted(changed[3]) == sorted(tracks + bars)
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
    # Claude B (the winner) is laying its spend under its track, all of it behind the line: its score stands still.
    # Codex (ranked second) lays spend past the line, which drains its score. Claude A and Gemini wait.
    assert moved == [(1, "track"), (2, "track"), (2, "bars")]
    # In the earlier decision no window resets during the task, so nothing is restless between the phases.
    assert not any(renderer.window_state(block, state(early))["resets"] for block in renderer.blocks)


def test_while_the_task_runs_a_slow_window_is_repainted_only_when_its_line_moves_a_pixel():
    renderer = quota_line.QuotaLineRenderer(quota_line.model(snapshot()), 486, 425, 1.15)
    start, end = quota_line.PHASES["run"]
    renderer.advance(start)
    steps = 60
    painted = [name for step in range(1, steps) for name in renderer.advance(start + (end - start) * step / steps)]
    # Gemini: a week-long window, nobody racing on it. Two hours move its line by a few pixels; its scores stand still.
    assert painted.count((3, "bars")) == 0 and 0 < painted.count((3, "track")) <= 12
    # Claude A resets during the task: its free quota is restless, so its track is painted on every step.
    assert painted.count((0, "track")) == steps - 1
    # The picture is the same as one fresh render of the same moment, whatever was skipped on the way.
    assert same(renderer.live, quota_line.QuotaLineRenderer(renderer.data, 486, 425, 1.15).frame(start + (end - start) * (steps - 1) / steps))


@pytest.mark.parametrize("size", SIZES + [(900, 700, 2.), (200, 120, 1.)])
@pytest.mark.parametrize("count", [1, 2, 4])
def test_tracks_lanes_and_scores_stay_inside_the_canvas_and_clear_of_each_other(size, count):
    source = snapshot()
    keys = list(source["quota_line"]["windows"])[:count]
    source["quota_line"]["windows"] = {key: source["quota_line"]["windows"][key] for key in keys}
    data = quota_line.model(source)
    assert len(data["windows"]) == count
    renderer = quota_line.QuotaLineRenderer(data, *size)
    # Three columns, left to right: names, tracks, scores.
    assert 0 < renderer.gutter < renderer.tx0 < renderer.tx1 < renderer.sx0 < renderer.sx1 <= renderer.right
    assert renderer.tx1 - renderer.tx0 >= 80 and renderer.sx1 - renderer.sx0 >= 60
    assert renderer.pitch >= renderer.bar_h + 2 >= 8 and renderer.pitch >= renderer.track_h + 6
    lines = []
    for block in renderer.blocks:
        assert block["top"] >= 0 and block["bottom"] <= renderer.bottom + 1e-6
        # A window's candidates stand directly under its own track, one row each.
        ys = [block["y"]] + [block["rows"][row] for row in block["window"]["rows"]]
        assert ys == sorted(ys) and all(b - a == pytest.approx(renderer.pitch) for a, b in zip(ys, ys[1:]))
        lines += ys
    assert lines == sorted(lines) and all(b - a >= renderer.pitch - 1e-6 for a, b in zip(lines, lines[1:]))
    # A spend is laid on its window's own scale: it starts where the window's fill ends.
    for block in renderer.blocks:
        for row in block["rows"]:
            start, free, end = renderer.spend(block, row)
            assert start == pytest.approx(block["window"]["used"] / block["window"]["capacity"]) and start <= free <= end
    # The type never shrinks below what the width allows unless the canvas is shorter than the layout's minimum.
    if size[1] >= quota_line.MIN_HEIGHT * min(size[2], size[0] / quota_line.NOMINAL[0]):
        assert renderer.s == pytest.approx(min(size[2], size[0] / quota_line.NOMINAL[0]))
    assert renderer.frame(decision_theatre.DURATION).size == size[:2]


def test_the_marker_is_the_recovery_line_and_the_right_end_is_the_reset():
    renderer = quota_line.QuotaLineRenderer(quota_line.model(snapshot()), 647, 623, 1.5)
    a, b = renderer.blocks[0], renderer.blocks[1]
    now = renderer.state(quota_line.PHASES["tie"][0])
    face = renderer.window_state(a, now)
    # Each window on its own clock: Claude A is two hours from its reset, Claude B a day into its week.
    assert face["mark"] == pytest.approx(166 / 168) and face["used"] == pytest.approx(240 / 400)
    assert renderer.window_state(b, now)["mark"] == pytest.approx(24 / 168)
    assert face["resets"] and not renderer.window_state(b, now)["resets"]
    # While the tracks appear, line and spend run up together from the left end.
    half = renderer.window_state(a, renderer.state(sum(quota_line.PHASES["clock"]) / 2))
    assert half["mark"] / half["used"] == pytest.approx(face["mark"] / face["used"]) and 0 < half["mark"] < face["mark"]
    # The task runs: the lines advance by its two hours. Claude A's reaches the reset, and what it has
    # not spent by then is lost; the winner's fast run has turned part of it into spend first.
    done = renderer.window_state(a, renderer.state(quota_line.PHASES["run"][1]))
    assert done["mark"] == pytest.approx(1.) and done["lost"] == 1
    assert done["eaten"] == done["spent"] == pytest.approx(demo.FAST_PRICE * 22.86 * demo.HOURS / 400)
    assert done["used"] + done["spent"] < 1                                       # some of it was not rescued
    later = renderer.window_state(b, renderer.state(quota_line.PHASES["run"][1]))
    assert later["mark"] == pytest.approx(26 / 168) and later["lost"] == 0 and later["spent"] == 0
    # On the track: nothing spent is the left end, the whole period is the right end, where the reset is.
    assert renderer.tx(0.) == renderer.tx0 and renderer.tx(1.) == renderer.tx(1.4) == renderer.tx1
    assert renderer.tx(.5) == pytest.approx((renderer.tx0 + renderer.tx1) / 2)
    # In the picture: left of the line Claude A's unspent quota is tinted, right of it the track is empty.
    frame = renderer.frame(quota_line.PHASES["tie"][0])
    free = pixel(renderer, frame, renderer.tx(.8), a["y"])
    assert free[1] > free[0] + 40 and free[1] > free[2] + 20                    # green; restless this close to the reset
    assert pixel(renderer, frame, renderer.tx(.6), b["y"]) == ImageColor.getrgb(quota_line.TRACK)
    # After the reset what was left has gone grey.
    gone = pixel(renderer, renderer.frame(quota_line.PHASES["run"][1]), renderer.tx(.92), a["y"])
    assert gone != free and max(gone) - min(gone) < 40
    # A window that never resets has no moving line.
    source = snapshot()
    source["quota_line"]["windows"]["codex"]["reset_in_hours"] = None
    still = quota_line.QuotaLineRenderer(quota_line.model(source), 647, 623, 1.5)
    assert still.window_state(still.blocks[2], still.state(9.))["mark"] == 0


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
    # The bar loses exactly the share of β·X/h that matches the share of the over-line spend already laid.
    samples = [renderer.state(quota_line.PHASES["plan"][0] + i * .02)["rows"][sol] for i in range(120)]
    moving = [s for s in samples if 0 < s["paid"] < 1]
    assert len(moving) > 8
    assert all(s["paid"] == pytest.approx(s["drawn"]) for s in moving)        # every dollar of it is over the line
    assert [s["paid"] for s in samples] == sorted(s["paid"] for s in samples) and samples[-1]["paid"] == 1
    # A candidate whose spend stays behind the line pays nothing, at any moment.
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
    # Its spend lies under Codex's track, right of the line, in red; the winner's lies behind its line, in green.
    codex, a = renderer.blocks[2], renderer.blocks[0]
    spend_start, free, spend_end = renderer.spend(codex, sol)
    assert free == spend_start and spend_start >= codex["window"]["at"]
    lane = pixel(renderer, end, renderer.tx((spend_start + spend_end) / 2), codex["rows"][sol])
    assert lane == ImageColor.getrgb(quota_line.OVER)
    spend_start, free, spend_end = renderer.spend(a, 0)
    assert free == spend_end <= a["window"]["at"]
    assert pixel(renderer, end, renderer.tx((spend_start + spend_end) / 2), a["rows"][0]) == ImageColor.getrgb(quota_line.PLANNED)


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
    # Settled: only the winner keeps the accent, and its whole row is lit, from its name to its score.
    final = renderer.frame(decision_theatre.DURATION)
    assert bar_pixel(renderer, final, 0, .3) == ImageColor.getrgb(quota_line.WINNER)
    assert bar_pixel(renderer, final, 1, .3) == ImageColor.getrgb(quota_line.SAME_MODEL_QUIET)
    assert bar_pixel(renderer, final, 3, .3) == ImageColor.getrgb(quota_line.OTHER_QUIET)
    between = ((renderer.tx1 + renderer.sx0) / 2, renderer.blocks[0]["rows"][0])
    assert pixel(renderer, final, *between) == ImageColor.getrgb(quota_line.theme.ACCENT_SOFT)
    assert pixel(renderer, renderer.frame(run[1]), *between) == ImageColor.getrgb(quota_line.theme.CARD)
    assert pixel(renderer, final, between[0], renderer.blocks[0]["rows"][1]) == ImageColor.getrgb(quota_line.theme.CARD)
    # Without a tie only the winner runs.
    alone = quota_line.QuotaLineRenderer(quota_line.model(earlier()), 647, 623, 1.5)
    assert [row["done"] > 0 for row in alone.state(run[1])["rows"]] == [True] + [False] * 5


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


def test_hover_text_says_what_the_row_under_the_pointer_stands_for():
    data = quota_line.model(snapshot())
    renderer = quota_line.QuotaLineRenderer(data, 647, 623, 1.5)
    s = renderer.s
    a, codex = renderer.blocks[0], renderer.blocks[2]
    over_track = renderer.tip_at(renderer.tx(.5) * s, a["y"] * s)
    assert "Claude A" in over_track and "$240" in over_track and "$400" in over_track
    assert quota_line.reset_text(2) in over_track and "$155" in over_track            # 400·166/168 − 240 still unspent
    assert "$" in renderer.tip_at(10, codex["y"] * s)
    over_bar = renderer.tip_at(renderer.sx(.3) * s, a["rows"][0] * s)
    assert data["rows"][0]["label"] in over_bar and "0.667" in over_bar and "Claude A" in over_bar
    sol = next(i for i, row in enumerate(data["rows"]) if row["id"] == "sol_high")
    assert "0.616" in renderer.tip_at(renderer.tx(.2) * s, codex["rows"][sol] * s)
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
        assert view.tip_at(10, view.renderer.blocks[0]["y"] * view.renderer.s)
        tiles = dict(view.renderer.tiles)
        view.show(snapshot())
        assert frames[-1] == decision_theatre.DURATION and len(frames) == 4
        # The next decision starts from a full image again; the old strips no longer cover it.
        assert all(view.canvas.itemcget(item, "state") == "hidden" for _, item in view.strips.values())
        view.follow(5.)
        assert view.canvas.itemcget(view.strips[1, "track"][1], "state") == "normal"
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
