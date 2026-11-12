"""Quota-line view of one decision: a quota line, and the usage line that rises against it.

Each window is a small chart on its own clock: left to right is this period, from its start to its reset;
bottom to top is quota, from none to the period's whole capacity. The quota line is the subscription's
recovery line ℓ(t) = C·(t − t0)/L, straight from corner to corner. The usage line is what the period has
consumed; the view draws it rising from the period's start to now. While it runs under the quota line it is
green and the window is "used first": quota that has recovered and is unspent costs nothing and is lost at
the reset. Where it runs over the quota line it is red and the window is "held back": spending more there
costs. A window's state is the one the decision saw, before this task; the colour of the line, the colour
of its end, the word above the chart and the hover text all come from one comparison. The window the chosen
candidate draws from is lit and the task's own spend is added to its line in the accent colour; a task that
runs past the reset spends only the share of its hours before it here, and a mark on the reset edge says the
rest belongs to the next period. Last, a dotted line runs from each line's end to the corner where the two
lines meet at the reset: the pace that uses up what is left.

Everything shown is read from the decision snapshot; the page never re-plans or re-prices a candidate.
It needs (names follow the quota-economy design, round 047):

    snapshot["quota_line"]["windows"][key] = {label, capacity_usd (C), used_usd (U), elapsed_hours (now − t0),
                                              reset_in_hours (T − now; None = never resets)}
    snapshot["choice"] and its ranking row: bundle, quota_line = {window, hours (h), spend_usd (b·h)}

and, for the usage line only, any of these optional window fields:

    samples = [[hours since the period's start, used USD], …]   readings recorded inside this period
    account, window_id                                          where the page may look such readings up itself
                                                                (the usage collector's account key and window id)

A snapshot is read field by field and never trusted. A window whose usage or clock is recorded as something
that is not a number is left out and counted in a note under the charts; a capacity that is missing, zero or
not a number is an unknown capacity: the usage line is drawn plain, with no quota line, no state and no ratio.
At most WINDOWS charts are shown: the chosen candidate's window always, then the other windows of its account,
then the rest in their recorded order, and the note says how many are not shown.

Readings are for display only. The router's economics use the current period's consumption U and nothing
else, and must not read them. With fewer than two readings the usage line is fitted from the period's start
to U and drawn dotted, never as if it were measured.

Only geometry is derived here: now (now − t0)/L, the heights U/C and b·h/C, and a smooth monotone curve
through the readings. The model and the renderer are plain Python + Pillow, so they are tested without a display.
"""
from __future__ import annotations

import bisect
import math

from PIL import Image, ImageColor, ImageDraw, ImageTk

from i18n import translate as _tr
import display_names
import ui_theme as theme
from decision_theatre import DURATION, ease, finite, font
from radar_chart import clamp

WINDOWS = 4
# Story-time windows on the decision film's clock (decision_theatre): the view follows the film while it plays.
PHASES = {"line": (.3, 1.4),        # the quota line is drawn from the period's start to its reset
          "sweep": (1.1, 6.0),      # the usage line rises from the period's start to now
          "spend": (6.2, 7.3),      # the window this task draws from is lit; the task's spend is added to its line
          "close": (7.3, 8.8)}      # the pace that uses up the rest runs on to the corner where the lines meet
FIRST, LAST = min(a for a, _ in PHASES.values()), max(b for _, b in PHASES.values())
NOMINAL = (330, 200)      # layout size in 96-dpi pixels; a smaller canvas scales the whole view down
MIN_HEIGHT = 190          # below this the view shrinks with the height too
MARGIN, GUTTER = 6., 8.   # px around the cells and between them
HEAD = 22.                # px a window's name and state take above its chart
COLUMN = 56               # screen px: a chart is cut into columns about this wide, repainted one by one
REACH = 14.               # px around a line's end that its dot, its specks and its ripple may touch
DOT_PITCH = 4.4           # px between the dots of a dotted line
PIECE = 8.                # px: a growing straight line is drawn through fixed points this far apart
TILE_MARGIN = 6           # screen px a tile is painted beyond its box
NOTE = 14.                # px under the charts for the note that says what is not shown
HEADROOM = 1.25           # a chart without a known capacity is this much taller than its usage
STEPS = 8                 # steps in which a state word and the lit card fade
SPECKS, SPECK_EVERY, SPECK_TRAIL = 4, 9., 11.   # specks shed by the rising end: how many, px of travel per speck, px they fall behind
TINT = .13                # opacity of the area between the two lines
SUPERSAMPLE = 2
BODY, SMALL, CHIP = 12, 11, 10    # px at 96 dpi: the tray's 9 pt body text is 12 px
# Colour is the state: under the quota line, over it, and this task.
QUOTA, PACE, AXIS, LIT_EDGE, PLAIN = "#A5B4FC", "#94A3B8", "#EEF2F7", "#C7D2FE", "#64748B"
UNDER, UNDER_SOFT, UNDER_DEEP = theme.GREEN, "#D1FAE5", "#047857"
OVER, OVER_SOFT, OVER_DEEP = theme.RED, "#FEE2E2", "#B91C1C"


def lin(story, name):
    start, end = PHASES[name]
    return clamp((story - start) / (end - start))


def phase(story, name):
    return ease(lin(story, name))


def staggered(progress, index, count, overlap):
    """Progress 0..1 of item `index` of `count` that start one after another; `overlap` 0 = all at once."""
    share = 1 / (1 + overlap * max(0, count - 1))
    return clamp((progress - index * overlap * share) / share)


def lerp(a, b, t):
    return a + (b - a) * t


def mix(a, b, t):
    """Colour between two hex colours."""
    a, b, t = ImageColor.getrgb(a), ImageColor.getrgb(b), clamp(t)
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def usd(value):
    return f"${value:,.0f}" if abs(value) >= 10 else f"${value:.1f}"


def reset_text(hours):
    if hours is None:
        return _tr("不重置")
    if hours < 48:
        return _tr("{0} 小时后重置").format(f"{hours:.2g}")
    return _tr("{0} 天后重置").format(f"{hours / 24:.2g}")


BAD = object()            # a recorded field that is there but is not a finite number


def number(value, missing=None):
    """A recorded number as a float: `missing` when it is absent, BAD when it is there but is not a finite number."""
    if value is None:
        return missing
    if isinstance(value, bool) or not finite(value):
        return BAD
    return float(value)


def window_geometry(window):
    """(now as a fraction of the period, recovery line ℓ(now) in USD) for one window on its own clock."""
    elapsed, left, capacity = (number(window.get(name), default) for name, default in
                               (("elapsed_hours", 0.), ("reset_in_hours", None), ("capacity_usd", 0.)))
    if left is None or elapsed is BAD or left is BAD:
        return 0., 0.       # never resets: nothing recovers, the line stays at 0
    period = max(0., elapsed) + max(0., left)
    at = clamp(max(0., elapsed) / period) if period > 0 else 0.
    return at, (0. if capacity is BAD else max(0., capacity)) * at


def read_window(key, window):
    """One recorded window as the view shows it, or None when its usage or its clock is not a number."""
    if not isinstance(window, dict):
        return None
    used, elapsed, reset = (number(window.get(name), default) for name, default in
                            (("used_usd", 0.), ("elapsed_hours", 0.), ("reset_in_hours", None)))
    if used is BAD or elapsed is BAD or reset is BAD:
        return None
    capacity = number(window.get("capacity_usd"))
    capacity = None if capacity is None or capacity is BAD or capacity <= 0 else capacity       # None: unknown
    used, elapsed, reset = max(0., used), max(0., elapsed), None if reset is None else max(0., reset)
    period = elapsed + reset if reset is not None else None
    at = clamp(elapsed / period) if period else 0.
    lined = bool(period) and capacity is not None        # a quota line needs a reset and a capacity
    path, fitted, readings = usage_path(window, at, used, period)
    account = window.get("account")
    return dict(key=key, label=str(window.get("label") or key), account=account if isinstance(account, str) and account else None,
                at=at, line=capacity * at if lined else 0., capacity=capacity, lined=lined, used=used, reset=reset,
                period=period, path=path, fitted=fitted, readings=readings)


def usage_path(window, at, used, period):
    """A window's usage line up to now: ([(moment of the period 0..1, USD)], fitted segments at its start, readings).

    Recorded readings are joined as they are and closed by the decision's own reading (now, U). Before the first
    reading, and when fewer than two are recorded, the line is fitted from the period's start.
    """
    elapsed, points = number(window.get("elapsed_hours"), 0.), []
    elapsed = 0. if elapsed is BAD else elapsed
    samples = window.get("samples")
    if isinstance(samples, (list, tuple)) and period:
        for item in samples:
            if (isinstance(item, (list, tuple)) and len(item) >= 2 and finite(item[0]) and finite(item[1])
                    and 0 <= item[0] <= elapsed + 1e-9):
                points.append((clamp(item[0] / period), max(0., float(item[1]))))
        points.sort()
    if len(points) < 2:
        return [(0., 0.), (at, used)], 1, 0
    lead = [(0., 0.)] if points[0][0] > 0 else []
    tail = [(at, used)] if points[-1] != (at, used) else []
    return lead + points + tail, len(lead), len(points)


def period_samples(series, reset_at, decided_at, elapsed_hours, capacity_usd, slack=120.):
    """Snapshot `samples` from the usage collector's readings of one window: only those of the period running
    at `decided_at` (the one that ends at `reset_at`), none after the decision.

    series: [[first seen, used %, reset time, last confirmed], …] with times in epoch seconds (capacity_stats).
    A reading that was confirmed again later is a step: the usage stood still until then.
    """
    start, out = decided_at - elapsed_hours * 3600, []
    for row in series or []:
        if not (isinstance(row, (list, tuple)) and len(row) >= 4 and all(finite(v) for v in row[:4])):
            continue
        first, used, reset, last = row[:4]
        if abs(reset - reset_at) > slack or first > decided_at or first < start - slack:
            continue
        usd_used = round(capacity_usd * used / 100, 4)
        out.append([round(max(0., first - start) / 3600, 4), usd_used])
        if first < min(last, decided_at):
            out.append([round((min(last, decided_at) - start) / 3600, 4), usd_used])
    return sorted(out)


def empty_text(result):
    """Why a decision has no chart: no candidate spent from a priced long window (the economy term is 0), or a record
    older than the quota economy. The page derives the block from `decision.system.quota_windows` when it can
    (ledger_snapshot.with_quota_line), so only these two remain."""
    system = ((result or {}).get("decision") or {}).get("system") if isinstance(result, dict) else None
    ranking = (result or {}).get("ranking") if isinstance(result, dict) else None
    eligible = [c for c in (ranking if isinstance(ranking, (list, tuple)) else ())
                if isinstance(c, dict) and not c.get("excluded")]
    has_economy = not eligible or any(isinstance(c.get("economy"), dict) for c in eligible)
    if isinstance(system, dict) and isinstance(system.get("quota_windows"), list) and has_economy:
        return _tr("这次决策没有候选从按额度计价的长周期窗口花费：额度经济项为 0（5 小时窗口只判断能否放下）")
    return _tr("此决策没有额度恢复线记录（记录早于额度经济）")


def model(result):
    """Display model of a snapshot, or None when it carries no quota-line record (older decisions).

    dict(windows, winner, hidden, dropped): the charts to show, the chosen candidate's spend (None when its
    window is not known), the labels of readable windows that are not shown, and the keys of unreadable ones.
    """
    block = result.get("quota_line") if isinstance(result, dict) else None
    recorded = block.get("windows") if isinstance(block, dict) else None
    if not isinstance(recorded, dict) or not recorded:
        return None
    read = {key: read_window(key, window) for key, window in recorded.items()}
    good = {key: window for key, window in read.items() if window is not None}
    # The task's own spend, on the window the chosen candidate draws from.
    chosen, ranking = None, result.get("ranking")
    for item in ranking if isinstance(ranking, (list, tuple)) else []:
        if not isinstance(item, dict) or item.get("id") != result.get("choice") or item.get("excluded"):
            continue
        line, bundle = item.get("quota_line"), item.get("bundle")
        key = line.get("window") if isinstance(line, dict) else None
        if not isinstance(key, str) or key not in good:
            continue
        window = good[key]
        hours, spend = number(line.get("hours"), 0.), number(line.get("spend_usd"))
        hours = 0. if hours is BAD or hours <= 0 else hours
        try:
            label = display_names.candidate_label({"id": item.get("id"), **(bundle if isinstance(bundle, dict) else {})})
        except Exception:       # a bundle recorded with fields of another type still names its candidate
            label = str(item.get("id"))
        # A task that runs past the reset spends only the share of its hours before it in this period.
        inside = clamp(window["reset"] / hours) if window["reset"] is not None and hours > window["reset"] else 1.
        chosen = dict(window=key, label=label, hours=hours, inside=inside,
                      spend=None if spend is None or spend is BAD else max(0., spend),       # None: unknown
                      share=clamp(hours / window["period"]) if window["period"] else 0.)
        break
    # Windows keep their recorded order, so replaying another decision does not shuffle the charts. When there
    # are more than fit, the chosen one stays, then its account's other windows, then the rest.
    order, hidden = list(good), []
    if len(order) > WINDOWS:
        first = [chosen["window"]] if chosen else []
        account = good[first[0]]["account"] if first else None
        kin = [key for key in order if key not in first and account and good[key]["account"] == account]
        rest = [key for key in order if key not in first and key not in kin]
        keep = set((first + kin + rest)[:WINDOWS])
        hidden = [good[key]["label"] for key in order if key not in keep]
        order = [key for key in order if key in keep]
    return dict(windows=[good[key] for key in order], winner=chosen, hidden=hidden,
                dropped=[str(key) for key, window in read.items() if window is None])


def smooth(path, fitted, width):
    """A usage path as a dense curve [(moment, USD)], and how many of its points are the fitted lead.

    Readings are joined by a monotone cubic: it passes through every reading and never dips, as usage cannot.
    The fitted lead stays the straight line it is. `width` is the chart's width in px: one point every 3 px,
    on straight stretches too, so that a line that grows keeps the pixels it already has.
    """
    lead, points = [], [list(p) for p in path[fitted:]]
    for a, b in zip(path[:fitted], path[1:fitted + 1]):
        pieces = max(1, min(160, math.ceil((b[0] - a[0]) * width / 3)))
        lead += [(a[0] + (b[0] - a[0]) * step / pieces, a[1] + (b[1] - a[1]) * step / pieces) for step in range(pieces)]
    if fitted:
        lead.append(tuple(path[fitted]))
    for before, here in zip(points, points[1:]):
        here[0] = max(here[0], before[0] + 1e-6)             # a step at one moment is a very steep rise
    if len(points) < 2:
        return lead, max(0, len(lead) - 1)
    spans = [b[0] - a[0] for a, b in zip(points, points[1:])]
    rises = [(b[1] - a[1]) / h for a, b, h in zip(points, points[1:], spans)]
    slopes = [rises[0]]
    for index in range(1, len(points) - 1):
        d0, d1, h0, h1 = rises[index - 1], rises[index], spans[index - 1], spans[index]
        slopes.append(0. if d0 * d1 <= 0 else (3 * (h0 + h1)) / ((2 * h1 + h0) / d0 + (h1 + 2 * h0) / d1))
    slopes.append(rises[-1])
    out = lead[:-1] if lead else []
    for index, (a, b) in enumerate(zip(points, points[1:])):
        h = spans[index]
        for step in range(max(1, min(24, math.ceil(h * width / 3)))):
            u = step / max(1, min(24, math.ceil(h * width / 3)))
            out.append((a[0] + h * u,
                        (2*u**3 - 3*u**2 + 1) * a[1] + (u**3 - 2*u**2 + u) * h * slopes[index]
                        + (-2*u**3 + 3*u**2) * b[1] + (u**3 - u**2) * h * slopes[index + 1]))
    out.append(tuple(points[-1]))
    return out, max(0, len(lead) - 1)


def over_line(moment, amount, capacity):
    """The one comparison of usage with the quota line: over it, or not (on it or under it).

    The colour of a stretch of the usage line, the colour of its end, the state word and the hover text all
    come from here.
    """
    return amount - capacity * moment > 1e-9


def runs_by_state(curve, lead, capacity, has_line):
    """Cut a usage curve where it crosses the quota line: [{over, fitted, pts}], each a stretch on one side of it.

    over: True above the line, False under it, None when the window has no line (it never resets, or its
    capacity is unknown).
    """
    gap = lambda p: p[1] - capacity * p[0]
    out = []
    for index, (a, b) in enumerate(zip(curve, curve[1:])):
        fitted = index < lead
        ga, gb = gap(a), gap(b)
        # Each piece is judged at its end away from the line: a crossing itself lies on neither side.
        pieces = [(a, b, a if abs(ga) >= abs(gb) else b)]
        if has_line and ga * gb < 0:
            u = ga / (ga - gb)
            cross = (a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u)
            pieces = [(a, cross, a), (cross, b, b)]
        for start, end, away in pieces:
            over = over_line(*away, capacity) if has_line else None
            if out and out[-1]["over"] == over and out[-1]["fitted"] == fitted:
                out[-1]["pts"].append(end)
            else:
                out.append(dict(over=over, fitted=fitted, pts=[start, end]))
    for run in out:
        run["ts"] = [p[0] for p in run["pts"]]
    return out


class QuotaLineRenderer:
    """Draws the view at a story time; `frame(DURATION)` is the settled, static picture.

    Each window's cell is cut into tiles: its head (the name and the state) and columns of its chart. What a
    tile shows depends only on how far each line has come inside it, so a frame redraws just the columns the
    lines' ends are passing through. Everything that never moves is drawn once into a base layer.
    """

    def __init__(self, data, width, height, scale=1., unreadable=False, empty_text=None):
        self.data = data
        self.empty_text = empty_text
        self.w, self.h = max(1, int(width)), max(1, int(height))
        self.s = min(scale, self.w / NOMINAL[0], self.h / MIN_HEIGHT)
        self.k = SUPERSAMPLE
        self.right, self.bottom = self.w / self.s, self.h / self.s
        self.ox = self.oy = 0
        self.tiles, self.drawn, self.repaints = {}, {}, 0
        self.cells = []
        # What is not shown is said under the charts.
        notes = [text.format(len(items)) for text, items in (
            (_tr("另有 {0} 个窗口未显示"), (data or {}).get("hidden")), (_tr("{0} 个窗口的记录无法读取"), (data or {}).get("dropped"))) if items]
        self.note = _tr("；").join(notes)
        self.foot = NOTE if self.note else 0.
        if data is None or not data["windows"]:
            self.base = self.empty(unreadable or bool(self.note))
        else:
            self.layout()
            self.base = self.static()
        self.live = self.base.reduce(self.k)

    # --- layout, in 96-dpi pixels -----------------------------------------------------------------
    def layout(self):
        windows, winner = self.data["windows"], self.data["winner"]
        count = len(windows)
        columns = 1 if count == 1 or (count == 2 and self.right < self.bottom * 1.2) else 2
        lines = math.ceil(count / columns)
        width = (self.right - 2 * MARGIN - GUTTER * (columns - 1)) / columns
        height = (self.bottom - self.foot - 2 * MARGIN - GUTTER * (lines - 1)) / lines
        s = self.s
        for index, window in enumerate(windows):
            x0, y0 = MARGIN + (width + GUTTER) * (index % columns), MARGIN + (height + GUTTER) * (index // columns)
            cell = dict(window=window, index=index, x0=x0, y0=y0, x1=x0 + width, y1=y0 + height,
                        px0=x0 + 13, px1=x0 + width - 15, py0=y0 + HEAD + 12, py1=y0 + height - 11)
            curve, lead = smooth(window["path"], window["fitted"], cell["px1"] - cell["px0"])
            cell["runs"] = runs_by_state(curve, lead, window["capacity"] or 0., window["lined"])
            # The window's state as the decision saw it, before this task: where its usage stood against its line.
            cell["over"] = over_line(window["at"], window["used"], window["capacity"]) if window["lined"] else None
            cell["task"] = winner if winner and winner["window"] == window["key"] else None
            # Where the usage line stands once the task has run, and whether a pace to the reset is left to draw.
            # Only the spend before the reset belongs to this period; the rest starts the next one.
            task = cell["task"]
            spend = (task["spend"] or 0.) if task else 0.
            cell["after"] = spend * (1 - task["inside"]) if task else 0.
            cell["end"] = (min(1., window["at"] + task["share"]), window["used"] + spend - cell["after"]) if task else (window["at"], window["used"])
            # Bottom to top is the period's capacity; usage beyond it, or a capacity nobody knows, sets its own height.
            top = max([cell["end"][1]] + [amount for _, amount in window["path"]])
            cell["scale"] = max(window["capacity"], top) if window["capacity"] else top * HEADROOM or 1.
            cell["closes"] = window["lined"] and cell["end"][0] < 1 and cell["end"][1] < window["capacity"]
            self.cells.append(cell)
            # Tile boxes in screen pixels: the head above, then columns of the chart.
            sx0, sx1 = max(0, math.floor(x0 * s)), min(self.w, math.ceil((x0 + width) * s))
            sy0, sy1 = max(0, math.floor(y0 * s)), min(self.h, math.ceil((y0 + height) * s))
            mid = min(sy1, round((y0 + HEAD + 3) * s))
            self.tiles[index, "head"] = (sx0, sy0, sx1, mid)
            strips = max(1, min(12, round((sx1 - sx0) / COLUMN)))
            edges = [sx0 + round((sx1 - sx0) * j / strips) for j in range(strips + 1)]
            span = (cell["px1"] - cell["px0"]) or 1.
            cell["columns"] = {}
            for j in range(strips):
                self.tiles[index, j] = (edges[j], mid, edges[j + 1], sy1)
                # The moments of the period a column covers, widened by what a line's end carries around it.
                cell["columns"][j] = ((edges[j] / s - REACH - cell["px0"]) / span, (edges[j + 1] / s + REACH - cell["px0"]) / span)

    def at(self, cell, moment, amount):
        """Where a moment of a window's period and an amount of its quota lie: left to right, bottom to top."""
        return (cell["px0"] + (cell["px1"] - cell["px0"]) * clamp(moment),
                cell["py1"] - (cell["py1"] - cell["py0"]) * clamp(amount / cell["scale"]))

    def grown(self, start, end, share, cell):
        """The first `share` of a straight line on a chart, through fixed points along it.

        A line that grows keeps the pixels it already has: only its last short piece is drawn anew.
        """
        a, b = self.at(cell, *start), self.at(cell, *end)
        pieces = max(1, math.ceil(math.dist(a, b) / PIECE))
        whole = int(share * pieces + 1e-9)
        points = [(a[0] + (b[0] - a[0]) * j / pieces, a[1] + (b[1] - a[1]) * j / pieces) for j in range(whole + 1)]
        if whole < share * pieces:
            points.append((a[0] + (b[0] - a[0]) * share, a[1] + (b[1] - a[1]) * share))
        return points

    # --- drawing helpers: 96-dpi pixels in, whole pixels of the full picture out -------------------
    def p(self, value):
        return value * self.s * self.k

    def P(self, point):
        # Rounded before the tile's corner is taken off: a mark cut by a tile's edge is the same on both sides.
        return round(point[0] * self.s * self.k) - self.ox, round(point[1] * self.s * self.k) - self.oy

    def ink(self, color, alpha=1.):
        rgb = ImageColor.getrgb(color) if isinstance(color, str) else color
        return (*rgb, round(255 * clamp(alpha)))

    def text(self, value, x, y, size=BODY, color=theme.INK, bold=False, anchor="lm", limit=None, alpha=1.):
        if alpha <= .01:
            return 0.
        f, value = font(round(size * self.s * self.k), bold), str(value)
        if limit is not None and f.getlength(value) > self.p(limit):
            while value and f.getlength(value + "…") > self.p(limit):
                value = value[:-1]
            value += "…"
        self.draw.text(self.P((x, y)), value, font=f, anchor=anchor, fill=self.ink(color, alpha))
        return f.getlength(value) / (self.s * self.k)

    def stroke(self, points, color, width, alpha=1.):
        """A line with round ends and joints."""
        if alpha <= .01 or len(points) < 2:
            return
        fill, size = self.ink(color, alpha), max(1, round(self.p(width)))
        pixels = [self.P(point) for point in points]
        self.draw.line(pixels, fill=fill, width=size, joint="curve")
        for x, y in (pixels[0], pixels[-1]):
            self.draw.ellipse((x - size / 2, y - size / 2, x + size / 2, y + size / 2), fill=fill)

    def dotted(self, start, end, color, width, alpha=1., share=1.):
        """A straight dotted line, drawn up to `share` of its length; the dots keep their places as it grows."""
        if alpha <= .01 or share <= 0:
            return
        length = math.dist(start, end)
        fill, r = self.ink(color, alpha), self.p(width) / 2
        for index in range(int(length * share / DOT_PITCH) + 1):
            u = index * DOT_PITCH / length if length else 0.
            x, y = self.P((start[0] + (end[0] - start[0]) * u, start[1] + (end[1] - start[1]) * u))
            self.draw.ellipse((x - r, y - r, x + r, y + r), fill=fill)

    def dot(self, point, radius, color, alpha=1.):
        if alpha <= .01 or radius <= 0:
            return
        (x, y), r = self.P(point), round(self.p(radius))
        self.draw.ellipse((x - r, y - r, x + r, y + r), fill=self.ink(color, alpha))

    def ring(self, point, radius, color, width, alpha=1.):
        if alpha <= .01 or radius <= 0:
            return
        (x, y), r = self.P(point), round(self.p(radius))
        self.draw.ellipse((x - r, y - r, x + r, y + r), outline=self.ink(color, alpha), width=max(1, round(self.p(width))))

    def canvas(self):
        image = Image.new("RGB", (self.w * self.k, self.h * self.k), theme.CARD)
        self.draw = ImageDraw.Draw(image, "RGBA")
        return image

    # --- base layer ------------------------------------------------------------------------------
    def empty(self, unreadable=False):
        image = self.canvas()
        self.text(_tr("额度恢复线记录无法读取") if unreadable else (self.empty_text or _tr("此决策没有额度恢复线记录（记录早于额度经济）")),
                  self.right / 2, self.bottom / 2,
                  BODY, theme.FAINT, anchor="mm", limit=self.right - 16)
        return image

    def static(self):
        image = self.canvas()
        for cell in self.cells:
            # A chart's floor is no quota used; its right edge is the reset.
            floor, edge = self.at(cell, 0., 0.), self.at(cell, 1., 0.)
            self.draw.line((self.P(floor), self.P(edge)), fill=self.ink(AXIS), width=max(1, round(self.p(1.))))
            if cell["window"]["period"]:
                self.draw.line((self.P(edge), self.P((edge[0], cell["py0"]))), fill=self.ink(AXIS), width=max(1, round(self.p(1.))))
        if self.note:
            self.text(self.note, self.right / 2, self.bottom - self.foot / 2 - 2, CHIP, theme.FAINT, anchor="mm", limit=self.right - 16)
        return image

    # --- state at a story time --------------------------------------------------------------------
    def state(self, story):
        count = len(self.cells)
        out = dict(story=story, cells=[])
        for cell in self.cells:
            window, index = cell["window"], cell["index"]
            drawn = ease(staggered(lin(story, "line"), index, count, .12))
            # The usage line rises at an even pace, a little softer at its start and its end.
            pace = staggered(lin(story, "sweep"), index, count, .08)
            reach = window["at"] * (.6 * pace + .4 * ease(pace))
            spent = ease(lin(story, "spend")) if cell["task"] else 0.
            closed = ease(staggered(lin(story, "close"), index, count, .12)) if cell["closes"] else 0.
            if pace >= 1:
                # The line has reached now. The task's spend moves its end on, but not the window's state: that
                # is the one the decision saw, and the reason this window was or was not chosen.
                head = (window["at"] + (cell["end"][0] - window["at"]) * spent, window["used"] + (cell["end"][1] - window["used"]) * spent)
                over = cell["over"]
            else:
                # While the line rises, its state is where its end stands against the quota line.
                head = (reach, self.usage(cell, reach))
                over = over_line(*head, window["capacity"]) if window["lined"] else None
            out["cells"].append(dict(drawn=drawn, pace=pace, reach=reach, spent=spent, closed=closed, head=head, over=over,
                                     lit=round(ease(lin(story, "spend") * 2.5) * STEPS) / STEPS if cell["task"] else 0.))
        return out

    def usage(self, cell, moment):
        """Usage (USD) at a moment of the period up to now, read off the window's usage line."""
        for run in cell["runs"]:
            if moment <= run["ts"][-1]:
                index = max(1, bisect.bisect_left(run["ts"], moment))
                (t0, u0), (t1, u1) = run["pts"][index - 1], run["pts"][index]
                return u0 if t1 <= t0 else u0 + (u1 - u0) * clamp((moment - t0) / (t1 - t0))
        return cell["window"]["used"]

    def key(self, name, state):
        """What a tile shows: equal keys mean an identical tile, so it is not painted again."""
        cell, live = self.cells[name[0]], state["cells"][name[0]]
        if name[1] == "head":
            shown = round(clamp(live["pace"] * 6) * STEPS)
            return (live["lit"], shown, live["over"] if shown else None)
        low, high = cell["columns"][name[1]]
        inside = lambda moment: min(max(moment, low), high)
        head = live["head"][0]
        near = low <= head <= high
        start = cell["end"][0]
        close = inside(start + (1 - start) * live["closed"]) if live["closed"] > 0 else None
        return (live["lit"], inside(live["drawn"]), inside(cell["window"]["at"] if live["pace"] >= 1 else live["reach"]), inside(head),
                (live["pace"], live["spent"], live["over"]) if near else None, close,
                live["closed"] if high >= 1 and live["closed"] > .8 else None)

    # --- frame -------------------------------------------------------------------------------------
    def advance(self, story=DURATION):
        """Bring `self.live` to `story`; returns the names of the tiles that had to be repainted."""
        changed = []
        if self.data is None:
            return changed
        k, state = self.k, self.state(story)
        for name, (x0, y0, x1, y1) in self.tiles.items():
            key = self.key(name, state)
            if self.drawn.get(name) == key or x1 <= x0 or y1 <= y0:
                continue
            self.drawn[name] = key
            self.repaints += 1
            # Painted a little larger than it is: Pillow cuts a shape at an image's edge a touch differently from
            # the same shape drawn whole, and that must not show where two tiles meet.
            left, top = max(0, x0 - TILE_MARGIN), max(0, y0 - TILE_MARGIN)
            tile = self.base.crop((left * k, top * k, min(self.w, x1 + TILE_MARGIN) * k, min(self.h, y1 + TILE_MARGIN) * k))
            self.draw, self.ox, self.oy = ImageDraw.Draw(tile, "RGBA"), left * k, top * k
            cell, live = self.cells[name[0]], state["cells"][name[0]]
            self.card(cell, live)
            if name[1] == "head":
                self.head(cell, live)
            else:
                self.chart(cell, live, *cell["columns"][name[1]])
            self.live.paste(tile.reduce(k).crop((x0 - left, y0 - top, x1 - left, y1 - top)), (x0, y0))
            changed.append(name)
        self.ox = self.oy = 0
        return changed

    def frame(self, story=DURATION):
        self.advance(story)
        return self.live.copy()

    # --- one window --------------------------------------------------------------------------------
    def card(self, cell, live):
        """The window this task draws from is lit."""
        if live["lit"] <= 0:
            return
        (x0, y0), (x1, y1) = self.P((cell["x0"] + .5, cell["y0"] + .5)), self.P((cell["x1"] - .5, cell["y1"] - .5))
        self.draw.rounded_rectangle((x0, y0, x1, y1), radius=round(self.p(9)), fill=self.ink(theme.ACCENT_SOFT, live["lit"] * .8),
                                    outline=self.ink(LIT_EDGE, live["lit"]), width=max(1, round(self.p(1.))))
        # The floor and the reset edge stay visible on the lit card.
        floor, edge = self.at(cell, 0., 0.), self.at(cell, 1., 0.)
        self.draw.line((self.P(floor), self.P(edge)), fill=self.ink(LIT_EDGE, live["lit"]), width=max(1, round(self.p(1.))))
        if cell["window"]["period"]:
            self.draw.line((self.P(edge), self.P((edge[0], cell["py0"]))), fill=self.ink(LIT_EDGE, live["lit"]), width=max(1, round(self.p(1.))))

    def head(self, cell, live):
        """The window's name, and what the usage line says about it right now."""
        window, y = cell["window"], cell["y0"] + HEAD / 2 + 2
        # In the steps of the tile's key: the tile is the same picture whenever it is painted.
        shown = round(clamp(live["pace"] * 6) * STEPS) / STEPS
        over = live["over"]
        word = "" if over is None else _tr("降权") if over else _tr("优先使用")
        f = font(round(CHIP * self.s * self.k), True)
        width = f.getlength(word) / (self.s * self.k) + 14 if word else 0.
        self.text(window["label"], cell["x0"] + 13, y, SMALL, mix(theme.TEXT, theme.ACCENT, live["lit"]), True,
                  limit=cell["x1"] - cell["x0"] - 30 - width)
        if not word or shown <= 0:
            return
        x1 = cell["x1"] - 11
        (ax, ay), (bx, by) = self.P((x1 - width, y - 8)), self.P((x1, y + 8))
        self.draw.rounded_rectangle((ax, ay, bx, by), radius=(by - ay) // 2, fill=self.ink(OVER_SOFT if over else UNDER_SOFT, shown))
        self.text(word, x1 - width / 2, y, CHIP, OVER_DEEP if over else UNDER_DEEP, True, anchor="mm", alpha=shown)

    def chart(self, cell, live, low, high):
        """The part of a window's chart between two moments of its period."""
        window, capacity = cell["window"], cell["window"]["capacity"] or 0.
        at = lambda moment, amount: self.at(cell, moment, amount)
        reach = window["at"] if live["pace"] >= 1 else live["reach"]
        strokes = []
        for run in cell["runs"]:
            if run["ts"][0] >= reach or run["ts"][0] > high or run["ts"][-1] < low:
                continue
            first = max(0, bisect.bisect_left(run["ts"], low) - 1)
            last = bisect.bisect_right(run["ts"], min(reach, high))
            points = list(run["pts"][first:last])
            if last < len(run["pts"]):
                (t0, u0), (t1, u1) = run["pts"][last - 1], run["pts"][last]
                until = min(reach, t1)
                points.append((until, u0 + (u1 - u0) * (until - t0) / (t1 - t0) if t1 > t0 else u1))
            if len(points) < 2:
                continue
            color = PLAIN if run["over"] is None else OVER if run["over"] else UNDER
            if run["over"] is not None:
                # Between the two lines: quota that has recovered and is unspent, or usage ahead of the quota line.
                area = [at(*point) for point in points] + [at(moment, capacity * moment) for moment, _ in reversed(points)]
                self.draw.polygon([self.P(point) for point in area], fill=self.ink(color, TINT))
            strokes.append((run, points, color))
        if window["lined"] and live["drawn"] > 0:
            # The quota line: quota recovers evenly, from nothing at the period's start to all of it at the reset.
            self.stroke(self.grown((0., 0.), (1., capacity), live["drawn"], cell), QUOTA, 1.5)
        for run, points, color in strokes:
            if run["fitted"]:
                # No readings here: a fit, drawn as one.
                start, end = at(*run["pts"][0]), at(*run["pts"][-1])
                span = (run["ts"][-1] - run["ts"][0]) or 1.
                self.dotted(start, end, color, 1.9, share=clamp((reach - run["ts"][0]) / span))
            else:
                self.stroke([at(*point) for point in points], color, 2.4)
        head, task = live["head"], cell["task"]
        if task and live["spent"] > 0:
            # This task's own spend, on top of the usage so far.
            self.stroke(self.grown((window["at"], window["used"]), cell["end"], live["spent"], cell), theme.ACCENT, 3.)
            self.ring(at(*head), 4 + 5 * live["spent"], theme.ACCENT, 1.4, .6 * (1 - live["spent"]))
            if cell["after"] > 0:
                # The task runs on past the reset: what it spends after it belongs to the next period, not to this chart.
                x, y = at(*cell["end"])
                self.stroke([(x + 7, y - 3.5), (x + 10.5, y), (x + 7, y + 3.5)], theme.ACCENT, 1.6, clamp((live["spent"] - .7) / .3))
        if cell["closes"] and live["closed"] > 0:
            # The pace that uses up what is left by the reset: the two lines close there.
            self.dotted(at(*cell["end"]), at(1., capacity), PACE, 1.6, share=live["closed"])
        if window["lined"] and live["drawn"] >= 1:
            done = live["closed"] >= 1
            ripple = clamp((live["closed"] - .8) / .2) if cell["closes"] else 0.
            if 0 < ripple < 1:
                self.ring(at(1., capacity), 4 + 5 * ripple, theme.ACCENT, 1.2, .6 * (1 - ripple))
            self.dot(at(1., capacity), 3.8, theme.CARD if not live["lit"] else mix(theme.CARD, theme.ACCENT_SOFT, live["lit"] * .8))
            self.dot(at(1., capacity), 3.8 if done else 0, theme.ACCENT)
            self.ring(at(1., capacity), 3.8, theme.ACCENT if done else QUOTA, 1.4)
        if live["pace"] <= 0:
            return
        color = PLAIN if live["over"] is None else OVER if live["over"] else UNDER
        if task and live["spent"] > 0:
            color = mix(color, theme.ACCENT, live["spent"] * 3)         # the end now belongs to the task's spend
        if 0 < live["pace"] < 1:
            # Specks are shed by the rising end: each leaves it, drifts off the line and fades, as the line moves on.
            span = (cell["px1"] - cell["px0"]) or 1.
            for speck in range(SPECKS):
                age = (reach * span / SPECK_EVERY + speck / SPECKS) % 1
                behind = reach - age * SPECK_TRAIL / span
                if behind > 0:
                    x, y = at(behind, self.usage(cell, behind))
                    self.dot((x, y + (1 if speck % 2 else -1) * (2 + 5 * age)), 1.5 - .7 * age, color, .7 * (1 - age))
        self.dot(at(*head), 7., color, .2)
        self.dot(at(*head), 4.1, color)
        self.dot(at(*head), 1.7, theme.CARD)

    def tip_at(self, x, y):
        """What the chart under a screen point stands for, in words; '' over empty paper."""
        if not self.data:
            return ""
        x, y = x / self.s, y / self.s
        for cell in self.cells:
            if not (cell["x0"] <= x <= cell["x1"] and cell["y0"] <= y <= cell["y1"]):
                continue
            window, over, task = cell["window"], cell["over"], cell["task"]
            if window["capacity"] is None:
                text = _tr("{0}：本期已用 {1}，容量未知，{2}").format(window["label"], usd(window["used"]), reset_text(window["reset"]))
            else:
                gap = window["line"] - window["used"]
                if window["lined"]:
                    # The same comparison as the line's colour and the word above the chart.
                    state = ((_tr("比恢复线多用了 {0}") if over else _tr("恢复线下还有 {0} 未用")).format(usd(abs(gap)))
                             + _tr("；") + (_tr("这个窗口被降权") if over else _tr("这个窗口被优先使用")))
                else:
                    state = (_tr("恢复线下还有 {0} 未用").format(usd(gap)) if gap >= 0
                             else _tr("比恢复线多用了 {0}").format(usd(-gap)))
                text = _tr("{0}：本期已用 {1} / {2}，{3}；{4}").format(
                    window["label"], usd(window["used"]), usd(window["capacity"]), reset_text(window["reset"]), state)
            text += _tr("；") + (_tr("实际用量线来自 {0} 个读数").format(window["readings"]) if window["readings"]
                                else _tr("本期没有读数，实际用量线按期初到当前拟合"))
            if task:
                text += _tr("；") + (_tr("本次任务用这个窗口：{0}，花费未知").format(task["label"]) if task["spend"] is None
                                    else _tr("本次任务用这个窗口：{0}，花费 {1}").format(task["label"], usd(task["spend"])))
                if cell["after"] > 0:
                    text += _tr("；") + _tr("其中 {0} 在重置之后，计入下一周期").format(usd(cell["after"]))
            return text
        if self.note and y >= self.bottom - self.foot - 2:
            hidden = self.data.get("hidden")
            return self.note + (_tr("；") + _tr("未显示的窗口：{0}").format(", ".join(hidden)) if hidden else "")
        return ""


class QuotaLineCanvas:
    """A Tk canvas that shows the view and follows the decision film's clock while it plays."""

    FOLLOW_FPS = 30     # the film draws at 60 fps; this view redraws at most this often while following it

    def __init__(self, parent):
        import tkinter as tk
        self.canvas = tk.Canvas(parent, bg=theme.CARD, highlightthickness=0, bd=0)
        self.data = self.renderer = self.photo = self.shown = self.error = self.empty = None
        self.story = DURATION
        self.item = self.canvas.create_image(0, 0, anchor="nw")
        self.strips = {}    # tile name -> (photo, canvas item): a moving tile is pasted alone, over the full image
        self.canvas.bind("<Configure>", self.configure)
        self.canvas.bind("<Map>", lambda event: self.draw())

    def show(self, result, story=DURATION):
        """Show a decision. A record this view cannot read never stops the page from loading the rest of the
        decision: the view shows that it is unreadable and keeps the exception in `self.error`."""
        self.renderer = self.shown = self.error = None
        try:
            self.data = model(result)
            self.empty = None if self.data else empty_text(result)
        except Exception as exc:
            self.data, self.error = None, exc
        self.story = story
        self.draw()

    def follow(self, story):
        """Called by the film for every frame it draws; a hidden view only remembers the time."""
        self.story = story
        self.draw()

    def configure(self, event):
        self.renderer = self.shown = None
        self.draw()

    def tip_at(self, x, y):
        return self.renderer.tip_at(x, y) if self.renderer is not None else ""

    def draw(self):
        try:
            self.paint()
        except Exception as exc:
            if self.error is not None:
                return      # the unreadable state itself failed to paint: leave the canvas as it is
            self.data, self.renderer, self.shown, self.error = None, None, None, exc
            self.draw()

    def paint(self):
        if not self.canvas.winfo_ismapped():
            return
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        if width <= 1 or height <= 1:
            return
        # Nothing moves outside the phases, and inside them one redraw per FOLLOW_FPS step is enough.
        story = min(max(self.story, FIRST), LAST)
        step = story if story in (FIRST, LAST) else round(story * self.FOLLOW_FPS)
        if self.renderer is not None and step == self.shown:
            return
        fresh = self.renderer is None
        if fresh:
            self.renderer = QuotaLineRenderer(self.data, width, height, theme.FACTOR, unreadable=self.error is not None,
                                              empty_text=self.empty)
        self.shown = step
        changed = self.renderer.advance(self.story)
        live = self.renderer.live
        if fresh or self.photo is None:
            # A new decision or size: one full image, and the strips of the previous one step aside.
            if self.photo is None or (self.photo.width(), self.photo.height()) != live.size:
                self.photo = ImageTk.PhotoImage(live, master=self.canvas)
                self.canvas.itemconfigure(self.item, image=self.photo)
                for _, item in self.strips.values():
                    self.canvas.delete(item)
                self.strips = {}
            else:
                self.photo.paste(live)
                for _, item in self.strips.values():
                    self.canvas.itemconfigure(item, state="hidden")
            return
        # Only the tiles that moved reach Tk: a smaller paste and a smaller area for the canvas to redraw.
        for name in changed:
            box = self.renderer.tiles[name]
            strip = live.crop(box)
            held = self.strips.get(name)
            if held is not None and (held[0].width(), held[0].height()) == strip.size:
                held[0].paste(strip)
                # Another decision cuts its tiles elsewhere: a strip of the same size may belong at a new place.
                self.canvas.coords(held[1], box[0], box[1])
                self.canvas.itemconfigure(held[1], state="normal")
            else:
                if held is not None:
                    self.canvas.delete(held[1])
                photo = ImageTk.PhotoImage(strip, master=self.canvas)
                self.strips[name] = (photo, self.canvas.create_image(box[0], box[1], anchor="nw", image=photo))
