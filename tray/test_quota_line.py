"""Quota-line view: snapshot → display model → frames, and the synthetic decisions the README films replay."""
import copy
from pathlib import Path
import sys

import pytest
from PIL import ImageChops

from tk_test_support import import_theatre_without_user_diagnostics

# Before quota_line imports it: the film's optional import-time diagnostics must not see the user's home.
decision_theatre = import_theatre_without_user_diagnostics()
import quota_line  # noqa: E402
import router_data  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docs" / "tools"))
import synthetic_decisions as demo  # noqa: E402


def same(a, b):
    return ImageChops.difference(a, b).getbbox() is None


def snapshot(windows=demo.WINDOWS, winner=demo.HEADLINE_SPECS[0][0]):
    row = demo.decision_row(demo.HEADLINE, demo.HEADLINE_KIND, demo.HOURS, winner, "2026-10-30T09:40:00+00:00",
                            windows=windows)
    return router_data.replay(row)


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
    rows = {row["id"]: row for row in snapshot(demo.EARLIER, demo.EARLIER_WINNER)["ranking"]}
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
    assert [row["id"] for row in data["rows"]] == [row["id"] for row in source["ranking"][:quota_line.ROWS]]
    assert [(row["fit"], row["score"]) for row in data["rows"]] == [
        (row["components"]["fit"], row["score"]) for row in source["ranking"][:quota_line.ROWS]]
    assert data["tied"] == 3 and [row["decided_by"] for row in data["rows"]] == [None, "faster", "earliest_expiry", None, None]
    assert [window["key"] for window in data["windows"]] == ["claude-a", "claude-b", "codex"]
    first = data["windows"][0]
    # Each window shows the winner's model where it can run there, else its best-ranked candidate.
    assert (first["spend"], first["over"]) == (source["ranking"][0]["quota_line"]["spend_usd"], 0.)
    assert data["windows"][2]["spend"] == next(r for r in source["ranking"] if r["id"] == "sol_high")["quota_line"]["spend_usd"]
    assert first["line"] > first["used"] and data["windows"][2]["line"] < data["windows"][2]["used"]
    assert [axis["name"] for axis in data["axes"]] == ["Opus 5.5", "Sol 6.1"]
    assert data["axes"][0]["values"]["divergence"] == (.74, pytest.approx(.2667), 1)
    # The same accounts keep their order when another decision is replayed.
    earlier = quota_line.model(snapshot(demo.EARLIER, demo.EARLIER_WINNER))["windows"]
    assert [w["key"] for w in earlier] == ["claude-a", "claude-b", "codex"]
    # The same model and spend: over the line on the window that is ahead, under it on the other account.
    assert earlier[0]["spend"] == earlier[1]["spend"] == earlier[0]["over"] and earlier[1]["over"] == 0


def test_decisions_without_a_quota_line_record_show_an_empty_state_not_invented_numbers():
    source = snapshot()
    assert quota_line.model({}) is None and quota_line.model(None) is None
    assert quota_line.model({**source, "quota_line": None}) is None
    stripped = {**source, "ranking": [{k: v for k, v in row.items() if k != "quota_line"} for row in source["ranking"]]}
    assert quota_line.model(stripped) is None
    unknown = copy.deepcopy(source)
    unknown["ranking"][0]["quota_line"]["window"] = "missing"
    assert [row["id"] for row in quota_line.model(unknown)["rows"]][0] == source["ranking"][1]["id"]
    renderer = quota_line.QuotaLineRenderer(None, 400, 300, 1.)
    assert renderer.frame(3.).size == (400, 300) and same(renderer.frame(3.), renderer.frame(9.))
    assert renderer.repaints == 0


@pytest.mark.parametrize("size", [(300, 280, 1.), (483, 446, 1.15), (640, 620, 1.5)])
def test_frames_render_without_a_display_and_repaint_only_the_band_that_moved(size):
    data = quota_line.model(snapshot())
    renderer = quota_line.QuotaLineRenderer(data, *size)
    base = renderer.base.copy()
    times = (0., 2.8, 3.5, 4.8, 6., 6.6, 7.3, 8.4, decision_theatre.DURATION)
    frames, repaints = [], []
    for t in times:
        before = renderer.repaints
        frames.append(renderer.frame(t))
        repaints.append(renderer.repaints - before)
    assert all(frame.size == size[:2] for frame in frames)
    # The first frame paints all four bands once; nothing moves before the first phase or after the last.
    assert repaints[:2] == [4, 0] and all(0 < n <= 2 for n in repaints[2:])
    for again in (decision_theatre.DURATION, quota_line.LAST, 9.5):
        before = renderer.repaints
        assert same(renderer.frame(again), frames[-1]) and renderer.repaints == before
    # 3.5 s: only the axis bars are growing; 6.0 s: only the spends are landing on the windows.
    assert repaints[2] == 1 and repaints[4] == 1
    # Every phase adds marks, the caller owns each frame, and the static base is never drawn on.
    assert all(not same(a, b) for a, b in zip(frames[1:], frames[2:]))
    assert renderer.frame(5.) is not renderer.frame(5.)
    assert same(base, renderer.base)
    # Band-by-band painting in any order gives the same picture as one fresh render of that moment.
    for t in (8.4, 3.5, 6.6, 0.):
        assert same(renderer.frame(t), quota_line.QuotaLineRenderer(data, *size).frame(t)), t


def test_a_short_canvas_drops_blocks_before_it_shrinks_the_type():
    data = quota_line.model(snapshot())
    shown = lambda *size: (set(quota_line.QuotaLineRenderer(data, *size).y), quota_line.QuotaLineRenderer(data, *size).s)
    everything = {"header", "windows", "axes", "scores", "tie"}
    assert shown(420, 388, 1.) == (everything, 1.)
    assert shown(483, 425, 1.15) == (everything, pytest.approx(425 / 388))         # the default window: 5% smaller
    assert shown(647, 623, 1.5) == (everything, 1.5)
    # The minimum window: the per-axis block (also on the radar) goes first, the type keeps the width's scale.
    blocks, scale = shown(300, 215, 1.)
    assert blocks == everything - {"axes"} and scale == pytest.approx(300 / 420)
    blocks, scale = shown(300, 150, 1.)
    assert blocks == {"header", "scores", "tie"} and scale == pytest.approx(300 / 420)
    for size in ((300, 215, 1.), (300, 150, 1.), (200, 60, 1.)):
        renderer = quota_line.QuotaLineRenderer(data, *size)
        assert renderer.frame(5.).size == size[:2] and set(renderer.bands) == set(renderer.y) - {"header"}
        assert same(renderer.frame(decision_theatre.DURATION), quota_line.QuotaLineRenderer(data, *size).frame(9.5))


def test_cost_shortens_only_the_bars_of_over_line_candidates():
    data = quota_line.model(snapshot())
    renderer = quota_line.QuotaLineRenderer(data, 483, 446, 1.15)
    # From the moment every bar has grown to F, to the moment the cost has been taken off.
    before, after = renderer.frame(quota_line.PHASES["fit"][1]), renderer.frame(quota_line.PHASES["cost"][1])
    _, left, right, _, _ = renderer.score_columns()
    rows = {row["id"]: cy for _, row, cy in renderer.score_rows()}
    s = renderer.s

    def bar_changed(cy):
        box = (round(left * s), round((cy - 3) * s), round(right * s), round((cy + 3) * s))
        return ImageChops.difference(before.crop(box), after.crop(box)).getbbox() is not None
    assert bar_changed(rows["sol_high"])                                  # over its line: pays β·X/h
    assert not bar_changed(rows["opus55_high_fast"]) and not bar_changed(rows["sonnet55_high"])   # under: free


def test_canvas_follows_the_film_clock_only_while_visible(monkeypatch):
    from tk_test_support import shared_tk_root, reset_tk_root
    root = shared_tk_root()
    view = quota_line.QuotaLineCanvas(root)
    try:
        frames = []
        real = quota_line.QuotaLineRenderer.advance
        monkeypatch.setattr(quota_line.QuotaLineRenderer, "advance",
                            lambda self, story=decision_theatre.DURATION: frames.append(story) or real(self, story))
        view.show(snapshot())
        view.follow(5.)
        assert not frames and view.story == 5.          # hidden: remembers the time, draws nothing
        monkeypatch.setattr(view.canvas, "winfo_ismapped", lambda: True)
        monkeypatch.setattr(view.canvas, "winfo_width", lambda: 420)
        monkeypatch.setattr(view.canvas, "winfo_height", lambda: 390)
        view.draw()
        assert frames == [5.] and view.photo is not None
        assert not view.strips                           # the first picture of a decision is one full image
        view.follow(5.001)                               # same 1/30 s step: no redraw
        view.follow(5.04)
        assert frames == [5., 5.04]
        # 5.04 s: only the recovery lines are moving, so only that band is pasted.
        assert set(view.strips) == {"windows"}
        top, bottom = view.renderer.bands["windows"][2:]
        assert (view.strips["windows"][0].width(), view.strips["windows"][0].height()) == (420, bottom - top)
        assert view.canvas.coords(view.strips["windows"][1]) == [0., float(top)]
        for story in (9.2, 9.5, decision_theatre.DURATION, decision_theatre.DURATION):
            view.follow(story)                           # after the last phase: one settled frame, then nothing
        assert frames == [5., 5.04, 9.2]
        assert set(view.strips) == {"windows", "scores", "tie"}
        view.show(snapshot(demo.EARLIER, demo.EARLIER_WINNER))
        assert frames[-1] == decision_theatre.DURATION and len(frames) == 4
        # The next decision starts from a full image again; the old strips no longer cover it.
        assert all(view.canvas.itemcget(item, "state") == "hidden" for _, item in view.strips.values())
        view.follow(5.)
        assert view.canvas.itemcget(view.strips["windows"][1], "state") == "normal"
        monkeypatch.setattr(view.canvas, "winfo_width", lambda: 300)
        view.configure(None)                             # a new size drops them
        assert not view.strips and view.photo.width() == 300
    finally:
        view.canvas.destroy()
        reset_tk_root(root)
