"""Quota-line view of one decision, told by motion instead of words.

Each window is a small chart on its own clock: left to right is this period, from its start to its reset;
bottom to top is quota, from none to the period's whole capacity. Two lines are compared. The forecast is
the subscription's recovery line ℓ(t) = C·(t − t0)/L, straight from corner to corner. The actual usage line
is what the period has consumed so far; it ends at now. The vertical gap between them at now is the point:
under the forecast it is recovered quota that is lost at the reset if nobody spends it, over the forecast
it is debt. A candidate's spend continues the usage line from now for the task's duration: green while it
stays under the forecast, red where it rises over it, and the red part drains that candidate's score bar,
which stands beside the chart: N = F − β·X/h, with the ability beyond the task's need cut off as a pale
cap. Then time passes for the length of the task on every window: tied candidates race, the winner's
spend becomes usage, and a gap that reaches the reset unspent is lost.

Everything shown is read from the decision snapshot; the page never re-plans or re-prices a candidate.
It needs (names follow the quota-economy design, round 047):

    snapshot["quota_line"]["windows"][key] = {label, capacity_usd (C), used_usd (U), elapsed_hours (now − t0),
                                              reset_in_hours (T − now; None = never resets), beta (β_w)}
    ranking row: score (N), components.fit (F), bundle.fast, five_axis_fit[axis] = {demand, score, target},
                 quota_line = {window, hours (h), spend_usd (b·h), over_line_usd (X, net of substitute pools),
                               decided_by ("N" | "earliest_expiry" | "faster" | "cheaper": the sort key that put this
                               row below the previous one)}

and, for the usage line only, any of these optional window fields:

    samples = [[hours since the period's start, used USD], …]   readings recorded inside this period
    account, window_id                                          where the page may look such readings up itself
                                                                (the usage collector's account key and window id)

Readings are for display only. The router's economics use the current period's consumption U and nothing
else, and must not read them. With fewer than two readings the usage line is fitted from the period's start
to U and drawn dotted, never as if it were measured.

Only geometry is derived here: now (now − t0)/L, the heights U/C and b·h/C, where a projection crosses the
forecast, the bar ends F and N, and the uncounted ability Σ demand·max(0, score − target) / Σ demand.
The model and the renderer are plain Python + Pillow, so they are tested without a display.
"""
from __future__ import annotations

import math

from PIL import Image, ImageColor, ImageDraw, ImageTk

from i18n import translate as _tr
import display_names
import ui_theme as theme
from decision_theatre import DURATION, ease, finite, font
from radar_chart import clamp, dashes

ROWS, WINDOWS = 6, 4
# Story-time windows on the decision film's clock (decision_theatre): the view follows the film while it plays.
PHASES = {"clock": (.35, 2.3),      # the charts appear; the forecast and the usage line are drawn up to now
          "rise": (2.0, 3.1),       # score bars grow to each candidate's whole ability
          "cap": (3.1, 3.9),        # ability beyond the task's need is cut off and left pale
          "plan": (3.9, 6.2),       # each candidate's spend is projected from now; the part over the forecast drains its bar
          "tie": (6.0, 6.5),        # equal N: a line joins the ends of the tied bars
          "run": (6.5, 8.6),        # time passes for the task: tied candidates race, a gap that reaches the reset is lost
          "settle": (8.6, 9.35)}    # the winner is named
FIRST, LAST = min(a for a, _ in PHASES.values()), max(b for _, b in PHASES.values())
NOMINAL = (330, 200)      # layout size in 96-dpi pixels; a smaller canvas scales the whole view down
MIN_HEIGHT = 190          # below this the view shrinks with the height too
ROW_PITCH = 29.           # tallest a row gets: one per candidate
HEAD, MIN_LINES = .62, 2  # rows a window's name takes above its chart; rows a chart is at least tall
TILE_PAD = 4.             # a tile reaches this far past its block
READING_DOTS = 24         # up to this many readings each get a dot on the usage line
GAP_WIDTH = 3.6           # px: the gap between usage and forecast at now, and a projection that climbs it
SUPERSAMPLE = 2
BODY, SMALL = 12, 11      # px at 96 dpi: the tray's 9 pt body text is 12 px
# Colour carries the legend. Quiet tones are the state of a window, saturated ones are this task.
FORECAST, ACTUAL, AHEAD, ASH = "#A3B1C6", "#334155", "#FDA4AF", "#D5DCE6"
FREE, PLANNED, UNDER, OVER, BURNT = "#6EE7B7", "#34D399", theme.GREEN, theme.RED, "#FECACA"
WINNER, SAME_MODEL, SAME_MODEL_QUIET, OTHER, OTHER_QUIET = theme.ACCENT, "#818CF8", "#A5B4FC", "#A3B1C6", "#C3CCD9"
QUIET_STEPS = 8                   # steps in which the losers recede
TIE_KEYS = ("earliest_expiry", "faster", "cheaper")
AXES = ("divergence", "global_rigor", "module_precision", "vision_tools")


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


def window_geometry(window):
    """(now as a fraction of the period, recovery line ℓ(now) in USD) for one window on its own clock."""
    elapsed, left = float(window.get("elapsed_hours") or 0.), window.get("reset_in_hours")
    if not finite(left):
        return 0., 0.       # never resets: nothing recovers, the line stays at 0
    period = elapsed + max(0., left)
    at = clamp(elapsed / period) if period > 0 else 0.
    return at, float(window.get("capacity_usd") or 0.) * at


def usage_path(window, at, used, period):
    """A window's usage line up to now: ([(moment of the period 0..1, USD)], fitted segments at its start, readings).

    Recorded readings are joined as they are and closed by the decision's own reading (now, U). Before the first
    reading, and when fewer than two are recorded, the line is fitted from the period's start.
    """
    elapsed, points = float(window.get("elapsed_hours") or 0.), []
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


def crossing(window, row, hours):
    """Share of a candidate's task after which its spend is over the window's forecast; 1 = never.

    The snapshot says how much of the spend is over the line (X); the chart shows where the projection crosses.
    """
    spend, over = row["spend"], row["over"]
    if over <= 0 or spend <= 0:
        return 1.
    recorded = clamp((spend - over) / spend)
    if not window["period"] or not hours:
        return recorded
    gap = window["line"] - window["used"]                               # under the forecast now
    gain = spend - window["capacity"] * hours / window["period"]        # what the spend gains on the forecast by the end
    if gap <= 0:
        return 0.
    return gap / gain if 0 < gap < gain else recorded


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


def uncounted(fit):
    """Ability beyond the task's need, in the units of F: Σ demand·max(0, score − target) / Σ demand."""
    rows = [row for row in (fit or {}).values() if isinstance(row, dict)] if isinstance(fit, dict) else []
    total = sum(float(row.get("demand") or 0.) for row in rows)
    if total <= 0:
        return 0.
    return sum(float(row.get("demand") or 0.) * max(0., float(row.get("score") or 0.) - float(row.get("target") or 0.))
               for row in rows) / total


def model(result):
    """Display model of a snapshot, or None when it carries no quota-line record (older decisions)."""
    block = (result or {}).get("quota_line")
    recorded = block.get("windows") if isinstance(block, dict) else None
    if not isinstance(recorded, dict) or not recorded:
        return None
    # Windows keep their recorded order, so replaying another decision does not shuffle the charts.
    keys = list(recorded)[:WINDOWS]
    rows = []
    for item in result.get("ranking") or []:
        line, comp, bundle = item.get("quota_line"), item.get("components") or {}, item.get("bundle") or {}
        if (item.get("excluded") or not isinstance(line, dict) or line.get("window") not in keys
                or not finite(item.get("score")) or not finite(comp.get("fit")) or len(rows) == ROWS):
            continue
        spend, over = max(0., float(line.get("spend_usd") or 0.)), max(0., float(line.get("over_line_usd") or 0.))
        rows.append(dict(id=item.get("id"), name=display_names.model_label(str(bundle.get("model") or item.get("id") or "")),
                         label=display_names.candidate_label({"id": item.get("id"), **bundle}), window=line["window"],
                         fit=float(comp["fit"]), score=float(item["score"]), spare=uncounted(item.get("five_axis_fit")),
                         hours=float(line["hours"]) if finite(line.get("hours")) and line["hours"] > 0 else None,
                         spend=spend, over=min(over, spend), fast=bool(bundle.get("fast")),
                         decided_by=line.get("decided_by") if line.get("decided_by") in TIE_KEYS else None))
    if not rows:
        return None
    winner = next((index for index, row in enumerate(rows) if row["id"] == result.get("choice")), 0)
    # The tie is the run of rows the allocator separated from the winner by a tie-break key, not by N.
    tied = 1
    while winner == 0 and tied < len(rows) and rows[tied]["decided_by"]:
        tied += 1
    shown = []
    for key in keys:
        window = recorded[key] if isinstance(recorded[key], dict) else {}
        at, line = window_geometry(window)
        capacity = float(window.get("capacity_usd") or 0.)
        reset = float(window["reset_in_hours"]) if finite(window.get("reset_in_hours")) else None
        used = float(window.get("used_usd") or 0.)
        period = float(window.get("elapsed_hours") or 0.) + reset if reset is not None else None
        path, fitted, readings = usage_path(window, at, used, period)
        shown.append(dict(key=key, label=str(window.get("label") or key), at=at, line=line, capacity=capacity,
                          used=used, reset=reset, period=period, path=path, fitted=fitted, readings=readings,
                          rows=[index for index, row in enumerate(rows) if row["window"] == key]))
    for row in rows:
        row["window_label"] = next(window["label"] for window in shown if window["key"] == row["window"])
    raced = list(range(tied)) if tied > 1 else [winner]
    # The task runs for the longest of the raced candidates; a candidate without hours takes all of it.
    span = max([rows[index]["hours"] or 0. for index in raced]) or 1.
    for row in rows:
        row["cross"] = crossing(next(window for window in shown if window["key"] == row["window"]), row, row["hours"] or span)
    return dict(rows=rows, windows=shown, winner=winner, tied=tied if tied > 1 else 0, raced=raced, span=span,
                top=max(row["fit"] + row["spare"] for row in rows) or 1.)


class QuotaLineRenderer:
    """Draws the view at a story time; `frame(DURATION)` is the settled, static picture.

    Everything that never moves (window names, each chart's baseline and reset line, the scores' zero line)
    is drawn once into a base layer. Each window's block is cut into three tiles: its chart, its
    candidates' names, and their scores. A frame redraws only the tiles whose state changed since the last one;
    the margins around the blocks belong to no tile and are never painted again.
    """

    def __init__(self, data, width, height, scale=1.):
        self.data = data
        self.w, self.h = max(1, int(width)), max(1, int(height))
        self.s = min(scale, self.w / NOMINAL[0], self.h / MIN_HEIGHT)
        self.k = SUPERSAMPLE
        self.right, self.bottom = self.w / self.s, self.h / self.s
        self.ox = self.oy = 0
        self.tiles, self.drawn, self.repaints = {}, {}, 0
        if data:
            self.layout()
        self.base = self.static() if data else self.empty()
        self.live = self.base.reduce(self.k)

    # --- layout, in 96-dpi pixels -----------------------------------------------------------------
    def layout(self):
        data, windows = self.data, self.data["windows"]
        count = len(windows)
        lines = [max(MIN_LINES, len(window["rows"])) for window in windows]
        gap = min(12., max(3., self.bottom * .03))
        self.pitch = min(ROW_PITCH, (self.bottom - 10 - gap * (count - 1)) / (sum(lines) + HEAD * count))
        top = (self.bottom - self.pitch * (sum(lines) + HEAD * count) - gap * (count - 1)) / 2
        # Three columns: the windows' charts (each on its own clock and its own USD scale), names, scores.
        self.cx0 = 10.
        self.cx1 = self.cx0 + (self.right - 18) * .42
        self.nx0 = self.cx1 + 14
        self.sx0 = self.nx0 + max(56., min(84., self.right * .19)) + 4
        self.sx1 = self.right - 8
        self.bar_h = max(6., min(13., self.pitch * .5))
        self.blocks, y = [], top
        for window, size in zip(windows, lines):
            head = y + self.pitch * HEAD
            slack = (size - len(window["rows"])) * self.pitch / 2          # fewer candidates than lines: centred
            rows = {row: head + slack + self.pitch * (slot + .5) for slot, row in enumerate(window["rows"])}
            self.blocks.append(dict(window=window, top=y, label=y + self.pitch * HEAD / 2, y0=head + 3,
                                    y1=head + self.pitch * size - 3, rows=rows, bottom=head + self.pitch * size))
            y = self.blocks[-1]["bottom"] + gap
        # Tile boxes in screen pixels: three columns, cut between the windows' blocks. The names have their own
        # tile because text is the dearest thing to draw and they change least.
        split = min(self.w, round((self.cx1 + 7) * self.s))
        named = max(split, min(self.w, round((self.sx0 - 3) * self.s)))
        edges = ([max(0, math.floor((top - TILE_PAD) * self.s))]
                 + [min(self.h, round((a["bottom"] + b["top"]) / 2 * self.s)) for a, b in zip(self.blocks, self.blocks[1:])]
                 + [min(self.h, math.ceil((self.blocks[-1]["bottom"] + TILE_PAD) * self.s))])
        for index in range(count):
            self.tiles[index, "chart"] = (0, edges[index], split, edges[index + 1])
            self.tiles[index, "names"] = (split, edges[index], named, edges[index + 1])
            self.tiles[index, "bars"] = (named, edges[index], self.w, edges[index + 1])
        # "Now" moves in steps of one drawn pixel: a window that barely moves is barely repainted.
        self.time_steps = max(1, round((self.cx1 - self.cx0) * self.s * self.k))

    def tx(self, fraction):
        """Where a moment of a window's period lies on its chart: 0 = the period's start, 1 = its reset."""
        return self.cx0 + (self.cx1 - self.cx0) * clamp(fraction)

    def uy(self, block, usd):
        """Where an amount of a window's quota lies on its chart: 0 at the bottom, its capacity at the top."""
        return block["y1"] - (block["y1"] - block["y0"]) * clamp(usd / (block["window"]["capacity"] or 1.))

    def sx(self, value):
        """Where a score lies on the scores' common scale."""
        return self.sx0 + (self.sx1 - self.sx0) * clamp(value / self.data["top"])

    # --- drawing helpers: coordinates are 96-dpi pixels, converted once here -------------------
    def p(self, value):
        return value * self.s * self.k

    def X(self, value):
        return value * self.s * self.k - self.ox

    def Y(self, value):
        return value * self.s * self.k - self.oy

    def text(self, value, x, y, size=BODY, color=theme.INK, bold=False, anchor="lm", limit=None, alpha=1.):
        if alpha <= .01:
            return 0.
        f, value = font(round(size * self.s * self.k), bold), str(value)
        if limit is not None and f.getlength(value) > self.p(limit):
            while value and f.getlength(value + "…") > self.p(limit):
                value = value[:-1]
            value += "…"
        self.draw.text((self.X(x), self.Y(y)), value, font=f, anchor=anchor,
                       fill=self.ink(color, alpha))
        return f.getlength(value) / (self.s * self.k)

    def ink(self, color, alpha=1.):
        rgb = ImageColor.getrgb(color) if isinstance(color, str) else color
        return (*rgb, round(255 * clamp(alpha)))

    def box(self, x0, y0, x1, y1, color, alpha=1., radius=0., left=True, right=True):
        """A filled bar; `radius` rounds its left and/or right end."""
        if x1 - x0 <= .05 or y1 - y0 <= .05 or alpha <= .01:
            return
        xy = (self.X(x0), self.Y(y0), self.X(x1), self.Y(y1))
        # Pillow cannot round one end only when the radius takes the whole height or width.
        radius = math.floor(min(self.p(radius), (xy[2] - xy[0]) / 2 - 1, (xy[3] - xy[1]) / 2 - 1))
        if radius >= 1 and (left or right):
            self.draw.rounded_rectangle(xy, radius=radius, fill=self.ink(color, alpha), corners=(left, right, right, left))
        else:
            self.draw.rectangle(xy, fill=self.ink(color, alpha))

    def rule(self, start, end, color, width=1., alpha=1., dash=None):
        if alpha <= .01:
            return
        fill, size = self.ink(color, alpha), max(1, round(self.p(width)))
        points = [(self.X(x), self.Y(y)) for x, y in (start, end)]
        if not dash:
            self.draw.line(points, fill=fill, width=size)
            return
        for piece in dashes(points, self.p(dash[0]), self.p(dash[1])):
            self.draw.line(piece, fill=fill, width=size)

    def dot(self, x, y, radius, color, alpha=1.):
        if alpha <= .01 or radius <= 0:
            return
        # Whole pixels of the full picture: a dot cut by a tile's edge is the same dot on both sides of it.
        x, y, r = self.p(x), self.p(y), self.p(radius)
        self.draw.ellipse((round(x - r) - self.ox, round(y - r) - self.oy, round(x + r) - self.ox, round(y + r) - self.oy),
                          fill=self.ink(color, alpha))

    def canvas(self):
        image = Image.new("RGB", (self.w * self.k, self.h * self.k), theme.CARD)
        self.draw = ImageDraw.Draw(image, "RGBA")
        return image

    # --- base layer ------------------------------------------------------------------------------
    def empty(self):
        image = self.canvas()
        self.text(_tr("此决策没有额度恢复线记录"), self.right / 2, self.bottom / 2, BODY, theme.FAINT, anchor="mm",
                  limit=self.right - 16)
        return image

    def static(self):
        image = self.canvas()
        for block in self.blocks:
            window = block["window"]
            self.text(window["label"], self.cx0, block["label"], SMALL, theme.TEXT, True, limit=self.cx1 - self.cx0)
            # A chart's floor is no quota used; its right edge is the reset, its top the period's whole quota.
            self.rule((self.cx0, block["y1"]), (self.cx1, block["y1"]), theme.BORDER, 1.)
            if window["period"]:
                self.rule((self.cx1, block["y0"] - 2), (self.cx1, block["y1"] + 2), theme.FAINT, 1.4)
            if block["rows"]:
                ys = list(block["rows"].values())
                self.rule((self.sx0 - .5, ys[0] - self.bar_h / 2 - 2), (self.sx0 - .5, ys[-1] + self.bar_h / 2 + 2),
                          theme.BORDER, 1.)
        return image

    # --- state: one value per moving thing, shared by the painters and the redraw test ---------------
    def state(self, story):
        data = self.data
        rows, count = data["rows"], len(data["rows"])
        wound, run, settled = phase(story, "clock"), lin(story, "run"), phase(story, "settle")
        cut, joined = phase(story, "cap"), phase(story, "tie")
        cool = phase(story, "tie")
        # The losers recede in a few steps: that needs no repaint on every frame.
        out = dict(story=story, wound=wound, run=run, settled=settled, cut=cut, joined=joined, rows=[],
                   quiet=round(settled * QUIET_STEPS) / QUIET_STEPS)
        for index, row in enumerate(rows):
            grown = ease(staggered(lin(story, "rise"), index, count, .18))
            laid = staggered(lin(story, "plan"), index, count, .6)
            drawn = ease(laid)                                                # share of the projection that is drawn
            # The part of the projection above the forecast is what costs: the score drains while it is drawn.
            paid = clamp((drawn - row["cross"]) / (1 - row["cross"])) if row["over"] > 0 and row["cross"] < 1 else 0.
            hours = row["hours"] or data["span"]
            done = clamp(run * data["span"] / hours) if index in data["raced"] else 0.
            won = 0.
            if index == data["winner"]:
                won = max(settled, ease((run - min(1., hours / data["span"])) / .12) if run > 0 else 0.)
            out["rows"].append(dict(grown=grown, laid=laid, drawn=drawn, paid=paid, done=done, won=won,
                                    heat=(1 - cool) if paid > 0 else 0.))
        return out

    def window_state(self, block, state):
        """Where a window's clock and its usage stand while the task runs."""
        window, data = block["window"], self.data
        now, resets = window["at"], False
        if window["period"]:
            now = min(1., window["at"] + state["run"] * data["span"] / window["period"])
            if 0 < state["run"] < 1:
                now = max(window["at"], math.floor(now * self.time_steps) / self.time_steps)
            resets = window["reset"] <= data["span"] * (1 + 1e-6)
        winner = data["rows"][data["winner"]]
        done = state["rows"][data["winner"]]["done"] if winner["window"] == window["key"] else 0.
        return dict(now=now, resets=resets, done=done, used=window["used"] + done * winner["spend"] if done else window["used"],
                    lost=ease((state["run"] - .8) / .2) if resets else 0.)

    def key(self, name, state):
        """What a tile shows: equal keys mean an identical tile, so it is not painted again."""
        data = self.data
        block, part = self.blocks[name[0]], name[1]
        ids = list(block["rows"])
        rows = [state["rows"][row] for row in ids]
        # A drain in flight crosses from the chart to the score and moves with the clock itself.
        flying = any(0 < row["paid"] < 1 for row in rows)
        named = state["settled"] if data["winner"] in ids else 0.
        if part == "bars":
            spare = any(data["rows"][row]["spare"] > 0 for row in ids)
            tied = data["tied"] and any(row < data["tied"] for row in ids)
            return (min(1., state["wound"] * 3), state["cut"] if spare else 0., state["joined"] if tied else 0.,
                    state["quiet"], named,
                    tuple((row["grown"], row["paid"], row["done"], row["won"], row["heat"]) for row in rows),
                    state["story"] if flying else None)
        if part == "names":
            return (min(1., state["wound"] * 3), state["quiet"], named, tuple(0 < row["laid"] < 1 for row in rows),
                    state["story"] if flying else None)
        face = self.window_state(block, state)
        laid = any(row["laid"] > 0 for row in rows)
        return (state["wound"], face["now"], face["done"], face["lost"], state["quiet"] if laid else 0.,
                tuple(row["laid"] for row in rows), state["story"] if flying else None)

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
            tile = self.base.crop((x0 * k, y0 * k, x1 * k, y1 * k))
            self.draw, self.ox, self.oy = ImageDraw.Draw(tile, "RGBA"), x0 * k, y0 * k
            self.block(self.blocks[name[0]], state, name[1])
            self.live.paste(tile.reduce(k), (x0, y0))
            changed.append(name)
        self.ox = self.oy = 0
        return changed

    def frame(self, story=DURATION):
        self.advance(story)
        return self.live.copy()

    # --- one window: its chart on the left, its candidates' names and scores on the right ---------------
    def block(self, block, state, part):
        data = self.data
        if part == "chart":
            self.chart(block, state, self.window_state(block, state))
        else:
            if data["winner"] in block["rows"] and state["settled"] > 0:
                # The winner's row: its name is the one that lights up.
                y, half = block["rows"][data["winner"]], min(self.pitch / 2 - 1, self.bar_h / 2 + 5)
                self.box(self.nx0 - 7, y - half, self.right - 3, y + half, theme.ACCENT_SOFT, state["settled"], radius=6.)
            if part == "bars":
                self.level_line(block, state)
            for row in block["rows"]:
                (self.name if part == "names" else self.score)(block, row, state)
        for row in block["rows"]:
            self.drain(block, row, state)

    def usage(self, window, fraction):
        """Recorded usage (USD) at a moment of the period up to now, read off the window's usage line."""
        path = window["path"]
        for (t0, u0), (t1, u1) in zip(path, path[1:]):
            if fraction <= t1:
                return u0 if t1 <= t0 else lerp(u0, u1, clamp((fraction - t0) / (t1 - t0)))
        return path[-1][1]

    def chart(self, block, state, face):
        window, shown = block["window"], state["wound"]
        if shown <= 0:
            return
        x, y = self.tx, lambda usd: self.uy(block, usd)
        capacity, at = window["capacity"], window["at"]
        seen = min(1., shown * 3)
        if window["period"]:
            # The forecast: quota recovers evenly, from nothing at the period's start to all of it at the reset.
            self.rule((x(0.), y(0.)), (x(shown), y(capacity * shown)), FORECAST, 1.3, seen)
        # Actual usage up to now. Recorded readings are a solid line; without them the line is fitted and dotted.
        reach, path = at * shown, window["path"]
        for index, ((t0, u0), (t1, u1)) in enumerate(zip(path, path[1:])):
            if t0 >= reach and index:
                break
            t = min(t1, reach)
            u = u1 if t1 <= t0 else lerp(u0, u1, (t - t0) / (t1 - t0))
            fitted = index < window["fitted"]
            self.rule((x(t0), y(u0)), (x(t), y(u)), ACTUAL, 1.3 if fitted else 1.9, seen * (.8 if fitted else 1.),
                      dash=(1.3, 2.6) if fitted else None)
        if window["readings"] and window["readings"] <= READING_DOTS:
            for t, u in path[window["fitted"]:]:
                if t <= reach:
                    self.dot(x(t), y(u), 1.7, ACTUAL, seen)
        now = face["now"] if shown >= 1 else reach
        used = face["used"] if shown >= 1 else self.usage(window, reach)
        if shown >= 1 and now > at:
            # The task runs: time passes on every window, and only the winner's window is spent from.
            winner = self.data["rows"][self.data["winner"]]
            if face["done"] > 0:
                (ex, ey), cross = self.projected(block, winner, face["done"]), winner["cross"]
                (mx, my) = self.projected(block, winner, min(face["done"], cross))
                self.rule((x(at), y(window["used"])), (mx, my), UNDER, GAP_WIDTH)
                self.rule((mx, my), (ex, ey), OVER, GAP_WIDTH)
                self.rule((ex, ey), (x(now), ey), ACTUAL, 1.9)
            else:
                self.rule((x(at), y(used)), (x(now), y(used)), ACTUAL, 1.9)
        if window["period"]:
            # Now, on this window's own clock; and the gap to the forecast: unspent quota under it, debt over it.
            line = capacity * now
            self.rule((x(now), block["y0"]), (x(now), block["y1"]), theme.INK, 1., seen * .16)
            if abs(line - used) * (block["y1"] - block["y0"]) / (capacity or 1.) > .6:
                lost = face["lost"]
                color = mix(FREE, ASH, lost) if used < line else AHEAD
                self.box(x(now) - GAP_WIDTH / 2, y(max(line, used)), x(now) + GAP_WIDTH / 2, y(min(line, used)), color,
                         seen * (1 - .4 * lost))
        self.dot(x(now), y(used), 3.2, theme.CARD, seen)
        self.dot(x(now), y(used), 2.2, theme.INK, seen)
        # What each candidate would add: from now, for its duration, at its price. Green under the forecast, red
        # over it. A task is short on a week's clock, so its projection climbs the gap it would use up.
        for row in block["rows"]:
            self.projection(block, row, state)

    def projected(self, block, item, share):
        """The point a candidate's spend reaches after `share` of its task, on its window's chart."""
        window = block["window"]
        hours = item["hours"] or self.data["span"]
        ahead = share * hours / window["period"] if window["period"] else 0.
        return self.tx(window["at"] + ahead), self.uy(block, window["used"] + share * item["spend"])

    def projection(self, block, row, state):
        data, live, item = self.data, state["rows"][row], self.data["rows"][row]
        if live["laid"] <= 0 or live["done"] >= 1:
            return
        window = block["window"]
        start = (self.tx(window["at"]), self.uy(block, window["used"]))
        head, turn = self.projected(block, item, live["drawn"]), self.projected(block, item, min(live["drawn"], item["cross"]))
        # One candidate at a time holds the eye: a projection recedes once it is drawn, and the losers' fade further.
        rest = 1 - .4 * ease((live["laid"] - .8) / .2)
        fade = rest * ((1 - live["done"]) if row == data["winner"] else 1 - .6 * state["quiet"])
        self.rule(start, turn, UNDER, GAP_WIDTH, fade)
        self.rule(turn, head, OVER, GAP_WIDTH, fade)
        if live["laid"] < 1:
            self.dot(*head, 3., OVER if live["drawn"] > item["cross"] else UNDER)

    def name(self, block, row, state):
        data, item, live = self.data, self.data["rows"][row], state["rows"][row]
        shown = min(1., state["wound"] * 3)
        won = state["settled"] if row == data["winner"] else 0.
        quiet = state["quiet"] if row != data["winner"] else 0.
        # The candidate whose projection is being drawn is the one named in ink.
        color = theme.INK if 0 < live["laid"] < 1 else mix(theme.MUTED, theme.ACCENT, won)
        self.text(item["name"], self.nx0, block["rows"][row], SMALL, color, won >= .5,
                  limit=self.sx0 - self.nx0 - 6, alpha=shown * (1 - .35 * quiet))

    def tone(self, row, state):
        data, settled = self.data, state["quiet"]
        if row == data["winner"]:
            return mix(SAME_MODEL, WINNER, state["rows"][row]["won"])
        own = data["rows"][row]["name"] == data["rows"][data["winner"]]["name"]
        return mix(SAME_MODEL, SAME_MODEL_QUIET, settled) if own else mix(OTHER, OTHER_QUIET, settled)

    def heights(self, row, state):
        """A candidate's bar in score units: its solid part (N so far), its fit, and its whole ability."""
        item = self.data["rows"][row]
        cost = max(0., item["fit"] - item["score"])
        return item["fit"] - cost * state["rows"][row]["paid"], item["fit"], item["fit"] + item["spare"]

    def score(self, block, row, state):
        data, item, live = self.data, self.data["rows"][row], state["rows"][row]
        grown = live["grown"]
        if grown <= 0:
            return
        y, half, x = block["rows"][row], self.bar_h / 2, self.sx
        color, cut = self.tone(row, state), state["cut"]
        solid, fit, whole = self.heights(row, state)
        end, edge = x(whole * grown), x(min(fit, whole * grown))
        if item["spare"] > 0 and end > edge:
            # Ability beyond the need: cut off with a surface gap and left pale. It does not count.
            self.box(edge + 1.6 * cut, y - half, end, y + half, color, 1 - .8 * cut, radius=3., left=False)
        if live["paid"] > 0:
            # What spend over the forecast costs: hot while it drains, then a pale scar.
            self.box(x(solid) + 1.2, y - half, edge, y + half, mix(BURNT, OVER, live["heat"]), .55 + .45 * live["heat"],
                     radius=3., left=False, right=item["spare"] <= 0)
        self.box(self.sx0, y - half, x(min(solid, whole * grown)), y + half, color, radius=3., left=False,
                 right=cut > 0 or item["spare"] <= 0 or live["paid"] > 0)
        if data["tied"] and live["done"] > 0 and row in data["raced"]:
            # The race, where the tie is: equal bars fill at each candidate's own speed.
            strength = 1. if row == data["winner"] else .5 * (1 - state["quiet"])
            self.box(self.sx0, y - half, x(solid * live["done"]), y + half, WINNER, strength, radius=3., left=False,
                     right=live["done"] >= 1)
        if item["fast"] and grown >= 1 and self.bar_h >= 7:
            self.chevrons(self.sx0 + 8, y, min(3.2, self.bar_h * .27))

    def drain(self, block, row, state):
        """Simultaneous cause and effect: spend over the forecast leaves the chart and takes the end of the score with it."""
        live = state["rows"][row]
        if not 0 < live["paid"] < 1:
            return
        y = block["rows"][row]
        (x0, y0), x1 = self.projected(block, self.data["rows"][row], live["drawn"]), self.sx(self.heights(row, state)[0])
        for speck in range(5):
            u = (state["story"] * 2.4 + speck / 5) % 1
            lift = math.sin(math.pi * u)
            self.dot(lerp(x0, x1, u), lerp(y0, y, u) - 4 * lift, 1.7 - .5 * u, OVER, lift ** .6)

    def level_line(self, block, state):
        """Equal N: one line down the ends of the tied bars, whichever windows they spend from."""
        data, joined = self.data, state["joined"]
        if not data["tied"] or joined <= 0:
            return
        ys = sorted(y for owner in self.blocks for row, y in owner["rows"].items() if row < data["tied"])
        reach = min(self.bar_h / 2 + 4, self.pitch / 2)          # stays inside the tiles of the tied rows' windows
        top, bottom = ys[0] - reach, ys[-1] + reach
        at = self.sx(data["rows"][0]["score"])
        self.rule((at, top), (at, top + (bottom - top) * joined), theme.INK, 1., .75 - .3 * state["quiet"], dash=(2.5, 2.5))

    def chevrons(self, x, y, size):
        """Fast mode: » on the bar."""
        for shift in (-size * .55, size * .55):
            points = [(self.X(x + shift - size * .45), self.Y(y - size)), (self.X(x + shift + size * .45), self.Y(y)),
                      (self.X(x + shift - size * .45), self.Y(y + size))]
            self.draw.line(points, fill=(255, 255, 255, 235), width=max(1, round(self.p(1.25))), joint="curve")

    # --- hover text, for the page ---------------------------------------------------------------------------
    def tip_at(self, x, y):
        """What the chart or the row under a screen point stands for, in words; '' over empty paper."""
        if not self.data:
            return ""
        x, y = x / self.s, y / self.s
        for block in self.blocks:
            window = block["window"]
            if not block["top"] <= y <= block["bottom"]:
                continue
            if x <= self.cx1 + 7:
                gap = window["line"] - window["used"]
                state = (_tr("恢复线下还有 {0} 未用").format(usd(gap)) if gap >= 0
                         else _tr("比恢复线多用了 {0}").format(usd(-gap)))
                source = (_tr("实际用量线来自 {0} 个读数").format(window["readings"]) if window["readings"]
                          else _tr("本期没有读数，实际用量线按期初到当前拟合"))
                return _tr("{0}：本期已用 {1} / {2}，{3}；{4}").format(
                    window["label"], usd(window["used"]), usd(window["capacity"]), reset_text(window["reset"]), state) + _tr("；") + source
            for row, at in block["rows"].items():
                if abs(y - at) <= self.pitch / 2:
                    item = self.data["rows"][row]
                    cost = max(0., item["fit"] - item["score"])
                    spend = (_tr("花费 {0}，其中 {1} 越线").format(usd(item["spend"]), usd(item["over"])) if item["over"] > 0
                             else _tr("花费 {0}，全部在线下").format(usd(item["spend"])))
                    return _tr("{0} · {1}：N {2} = F {3} − {4}；{5}").format(
                        item["label"], item["window_label"], f"{item['score']:.3f}", f"{item['fit']:.3f}", f"{cost:.3f}", spend)
        return ""


class QuotaLineCanvas:
    """A Tk canvas that shows the view and follows the decision film's clock while it plays."""

    FOLLOW_FPS = 30     # the film draws at 60 fps; this view redraws at most this often while following it

    def __init__(self, parent):
        import tkinter as tk
        self.canvas = tk.Canvas(parent, bg=theme.CARD, highlightthickness=0, bd=0)
        self.data = self.renderer = self.photo = self.shown = None
        self.story = DURATION
        self.item = self.canvas.create_image(0, 0, anchor="nw")
        self.strips = {}    # tile name -> (photo, canvas item): a moving tile is pasted alone, over the full image
        self.canvas.bind("<Configure>", self.configure)
        self.canvas.bind("<Map>", lambda event: self.draw())

    def show(self, result, story=DURATION):
        self.data, self.renderer, self.shown = model(result), None, None
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
            self.renderer = QuotaLineRenderer(self.data, width, height, theme.FACTOR)
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
