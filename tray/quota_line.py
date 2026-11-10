"""Quota-line view of one decision, told by motion instead of words.

Each window is a clock face. One turn is one period, so the hand is the recovery line ℓ(t) = C·(t − t0)/L:
it sweeps on the window's own clock and reaches twelve at the reset. Spend is an arc that grows from twelve.
Behind the hand lie beads: quota that has recovered and is still unspent. Spend that stays behind the hand
eats beads and costs nothing; spend that runs past the hand is drawn hot, and what it costs drains out of that
candidate's bar at the same moment. Under every clock stand the candidates that would spend there: a bar is
N = F − β·X/h, the pale cap above it is ability beyond the task's need (not counted), the red slice is the
cost. Then the clocks run for the length of the task: tied candidates race, and beads the hand carries past
twelve are lost unless the winner has eaten them first.

Everything shown is read from the decision snapshot; the page never re-plans or re-prices a candidate.
It needs (names follow the quota-economy design, round 047):

    snapshot["quota_line"]["windows"][key] = {label, capacity_usd (C), used_usd (U), elapsed_hours (now − t0),
                                              reset_in_hours (T − now; None = never resets), beta (β_w)}
    ranking row: score (N), components.fit (F), bundle.fast, five_axis_fit[axis] = {demand, score, target},
                 quota_line = {window, hours (h), spend_usd (b·h), over_line_usd (X, net of substitute pools),
                               decided_by ("N" | "earliest_expiry" | "faster" | "cheaper": the sort key that put this
                               row below the previous one)}

Only geometry is derived here: the hand's angle (now − t0)/L, the arc lengths U/C and b·h/C, the bar ends F
and N, and the uncounted ability Σ demand·max(0, score − target) / Σ demand.
The model and the renderer are plain Python + Pillow, so they are tested without a display.
"""
from __future__ import annotations

import math

from PIL import Image, ImageColor, ImageDraw, ImageTk

from i18n import translate as _tr
import display_names
import ui_theme as theme
from decision_theatre import DURATION, ease, finite, font
from radar_chart import clamp

ROWS, WINDOWS = 6, 4
# Story-time windows on the decision film's clock (decision_theatre): the view follows the film while it plays.
PHASES = {"clock": (.35, 2.3),      # the faces appear; hands and spend wind up to now
          "rise": (2.0, 3.1),       # bars grow to each candidate's whole ability
          "cap": (3.1, 3.9),        # ability beyond the task's need is cut off and left pale
          "plan": (3.9, 6.2),       # each candidate's spend is laid on its clock; over-line spend drains its bar
          "tie": (6.0, 6.5),        # equal N: a level joins the tied bars
          "run": (6.5, 8.6),        # the clocks run for the task: tied candidates race, expiring quota is lost
          "settle": (8.6, 9.35)}    # the winner is named
FIRST, LAST = min(a for a, _ in PHASES.values()), max(b for _, b in PHASES.values())
NOMINAL = (420, 372)      # layout size in 96-dpi pixels; a smaller canvas scales the whole view down
MIN_HEIGHT = 226          # below this the view shrinks with the height too
SUPERSAMPLE = 2
BODY, SMALL = 12, 11      # px at 96 dpi: the tray's 9 pt body text is 12 px
# Colour carries the legend. Quiet tones are the state of a window, saturated ones are this task.
TRACK, USED, AHEAD, ASH = "#EDF1F6", "#B4C0D0", "#FDA4AF", "#D5DCE6"
FREE, PLANNED, UNDER, OVER, BURNT = "#34D399", "#6EE7B7", theme.GREEN, theme.RED, "#FECACA"
WINNER, SAME_MODEL, SAME_MODEL_QUIET, OTHER, OTHER_QUIET = theme.ACCENT, "#818CF8", "#A5B4FC", "#A3B1C6", "#C3CCD9"
LANE, LANE_PITCH, LANE_ROOM = 3.2, 5., 17.    # thickness and pitch of a what-if spend arc; room they take outside the face
PLOT, GAP = 176., 5.                          # tallest the bars get; gap between two bars
QUIET_STEPS, BEATS = 8, 15                    # steps in which the losers recede; ticks per second of restless beads
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
    # Windows keep their recorded order, so replaying another decision does not shuffle the clocks.
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
        shown.append(dict(key=key, label=str(window.get("label") or key), at=at, line=line, capacity=capacity,
                          used=float(window.get("used_usd") or 0.), reset=reset,
                          period=float(window.get("elapsed_hours") or 0.) + reset if reset is not None else None,
                          rows=[index for index, row in enumerate(rows) if row["window"] == key]))
    for row in rows:
        row["window_label"] = next(window["label"] for window in shown if window["key"] == row["window"])
    raced = list(range(tied)) if tied > 1 else [winner]
    return dict(rows=rows, windows=shown, winner=winner, tied=tied if tied > 1 else 0, raced=raced,
                # The clocks run for the longest of the raced candidates; a candidate without hours takes all of it.
                span=max([rows[index]["hours"] or 0. for index in raced]) or 1.,
                top=max(row["fit"] + row["spare"] for row in rows) or 1.)


class QuotaLineRenderer:
    """Draws the view at a story time; `frame(DURATION)` is the settled, static picture.

    Everything that never moves (window names, clock faces, baselines) is drawn once into a base layer.
    The picture is cut into tiles: per window one for its clock and one for its candidates, and one for the
    winner's name. A frame redraws only the tiles whose state changed since the last one.
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
        data, count = self.data, len(self.data["windows"])
        self.cell = min(150., (self.right - 4) / count)
        left = (self.right - self.cell * count) / 2
        # The clocks are as large as the width allows, up to two fifths of the height.
        self.radius = max(14., min(38., (self.cell - 6) / 2 - LANE_ROOM, (.4 * self.bottom - 6) / 2 - LANE_ROOM))
        self.thick = max(4.5, self.radius * .26)
        face = 2 * (self.radius + LANE_ROOM)
        # Height the bars do not need opens the space the spend crosses, then the margins.
        spare = max(0., self.bottom - (20 + face + 12 + PLOT + 36))
        air = min(22., spare * .5)
        self.name_y = 9. + (spare - air) * .5
        self.cy = self.name_y + 11 + face / 2
        self.plot_top = self.cy + face / 2 + 12 + air
        self.base_y = self.bottom - 36 - (spare - air) * .5
        self.strip_y = self.base_y + 5
        self.pill_y = self.base_y + 21
        self.columns = []
        for index, window in enumerate(data["windows"]):
            x0 = left + index * self.cell
            rows = window["rows"]
            width = max(4., min(15., (self.cell - 24 - GAP * (len(rows) - 1)) / max(1, len(rows))))
            start = x0 + self.cell / 2 - (len(rows) * width + GAP * (len(rows) - 1)) / 2
            self.columns.append(dict(window=window, x0=x0, x1=x0 + self.cell, cx=x0 + self.cell / 2,
                                     bars={row: (start + slot * (width + GAP), start + slot * (width + GAP) + width)
                                           for slot, row in enumerate(rows)},
                                     pitch=min(LANE_PITCH, (LANE_ROOM - 2) / max(3, len(rows))),
                                     beads=max(12, round(2 * math.pi * (self.radius - self.thick / 2) / 5.4))))
        # Tile boxes in screen pixels; together they cover the canvas.
        edges = [0] + [round(column["x1"] * self.s) for column in self.columns[:-1]] + [self.w]
        strip = min(self.h, round(self.strip_y * self.s))
        split = min(strip, round((self.cy + face / 2 + 6) * self.s))
        for index in range(count):
            self.tiles[index, "clock"] = (edges[index], 0, edges[index + 1], split)
            self.tiles[index, "bars"] = (edges[index], split, edges[index + 1], strip)
        self.tiles["winner"] = (0, strip, self.w, self.h)
        # A hand moves in steps of one drawn pixel at its tip: a window that barely moves is barely repainted.
        self.hand_steps = max(1, round(2 * math.pi * (self.radius + LANE_ROOM) * self.s * self.k))

    def level(self, value):
        return self.base_y - (self.base_y - self.plot_top) * clamp(value / self.data["top"])

    def on_face(self, column, fraction, radius):
        """A point on a window's clock: `fraction` of a turn clockwise from twelve."""
        angle = 2 * math.pi * fraction - math.pi / 2
        return column["cx"] + radius * math.cos(angle), self.cy + radius * math.sin(angle)

    def lane_radius(self, column, row):
        """Outer radius of the what-if spend arc of a candidate, just outside its window's face."""
        return self.radius + 2 + LANE + column["window"]["rows"].index(row) * column["pitch"]

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
                       fill=(*ImageColor.getrgb(color), round(255 * clamp(alpha))))
        return f.getlength(value) / (self.s * self.k)

    def width(self, value, size, bold=False):
        return font(round(size * self.s * self.k), bold).getlength(str(value)) / (self.s * self.k)

    def ink(self, color, alpha=1.):
        rgb = ImageColor.getrgb(color) if isinstance(color, str) else color
        return (*rgb, round(255 * clamp(alpha)))

    def box(self, x0, y0, x1, y1, color, alpha=1., round_top=0.):
        if x1 - x0 <= 0 or y1 - y0 <= .05 or alpha <= .01:
            return
        xy = (self.X(x0), self.Y(y0), self.X(x1), self.Y(y1))
        radius = min(self.p(round_top), (xy[2] - xy[0]) / 2, xy[3] - xy[1])
        if radius >= 1:
            self.draw.rounded_rectangle(xy, radius=radius, fill=self.ink(color, alpha), corners=(True, True, False, False))
        else:
            self.draw.rectangle(xy, fill=self.ink(color, alpha))

    def rule(self, start, end, color, width=1., alpha=1., dash=None):
        if alpha <= .01:
            return
        fill, size = self.ink(color, alpha), max(1, round(self.p(width)))
        (x0, y0), (x1, y1) = ((self.X(x), self.Y(y)) for x, y in (start, end))
        length = math.hypot(x1 - x0, y1 - y0)
        if not dash or length <= 0:
            self.draw.line(((x0, y0), (x1, y1)), fill=fill, width=size)
            return
        on, off = self.p(dash[0]), self.p(dash[1])
        at = 0.
        while at < length:
            a, b = at / length, min(length, at + on) / length
            self.draw.line(((x0 + (x1 - x0) * a, y0 + (y1 - y0) * a), (x0 + (x1 - x0) * b, y0 + (y1 - y0) * b)),
                           fill=fill, width=size)
            at += on + off

    def dot(self, x, y, radius, color, alpha=1.):
        if alpha <= .01 or radius <= 0:
            return
        x, y, r = self.X(x), self.Y(y), self.p(radius)
        self.draw.ellipse((x - r, y - r, x + r, y + r), fill=self.ink(color, alpha))

    def arc(self, column, outer, thick, start, end, color, alpha=1.):
        """Part of a ring around a window's centre, from `start` to `end` turns clockwise from twelve."""
        start, end = max(0., start), min(1., end)
        if end - start <= 1e-4 or alpha <= .01:
            return
        x, y, r = self.X(column["cx"]), self.Y(self.cy), self.p(outer)
        self.draw.arc((x - r, y - r, x + r, y + r), 360 * start - 90, 360 * end - 90, fill=self.ink(color, alpha),
                      width=max(1, round(self.p(thick))))

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
        for column in self.columns:
            window = column["window"]
            self.text(window["label"], column["cx"], self.name_y, SMALL, theme.TEXT, True, anchor="mm", limit=self.cell - 6)
            self.rule((column["x0"] + 9, self.base_y + .5), (column["x1"] - 9, self.base_y + .5), theme.BORDER, 1.)
        return image

    # --- state: one value per moving thing, shared by the painters and the redraw test ---------------
    def state(self, story):
        data = self.data
        rows, count = data["rows"], len(data["rows"])
        wound, run, settled = phase(story, "clock"), lin(story, "run"), phase(story, "settle")
        cut, joined = phase(story, "cap"), phase(story, "tie")
        cool = phase(story, "tie")
        # The losers recede in a few steps and restless beads tick: neither needs a repaint on every frame.
        out = dict(story=story, wound=wound, run=run, settled=settled, cut=cut, joined=joined, rows=[],
                   quiet=round(settled * QUIET_STEPS) / QUIET_STEPS, beat=math.floor(story * BEATS) / BEATS)
        for index, row in enumerate(rows):
            grown = ease(staggered(lin(story, "rise"), index, count, .18))
            laid = staggered(lin(story, "plan"), index, count, .32)
            drawn = ease((laid - .2) / .8)                                    # share of the spend arc that is laid
            red = max(0., drawn * row["spend"] - (row["spend"] - row["over"]))  # USD of it that are over the line
            paid = red / row["over"] if row["over"] > 0 else 0.
            hours = row["hours"] or data["span"]
            done = clamp(run * data["span"] / hours) if index in data["raced"] else 0.
            won = 0.
            if index == data["winner"]:
                won = max(settled, ease((run - min(1., hours / data["span"])) / .12) if run > 0 else 0.)
            out["rows"].append(dict(grown=grown, laid=laid, drawn=drawn, paid=paid, done=done, won=won,
                                    heat=(1 - cool) if paid > 0 else 0.))
        return out

    def window_state(self, column, state):
        """Where a window's hand and spend stand, in turns."""
        window, data = column["window"], self.data
        capacity = window["capacity"] or 1.
        used = clamp(window["used"] / capacity)
        hand, resets = window["at"], False
        if window["period"]:
            hand = min(1., window["at"] + state["run"] * data["span"] / window["period"])
            if 0 < state["run"] < 1:
                hand = max(window["at"], math.floor(hand * self.hand_steps) / self.hand_steps)
            resets = window["reset"] <= data["span"] * (1 + 1e-6)
        winner = data["rows"][data["winner"]]
        eaten = spent = 0.
        if winner["window"] == window["key"]:
            done = state["rows"][data["winner"]]["done"]
            spent = clamp(done * winner["spend"] / capacity)
            eaten = clamp(min(done * winner["spend"], winner["spend"] - winner["over"]) / capacity)
        return dict(used=used * state["wound"], hand=hand * state["wound"], resets=resets, eaten=eaten, spent=spent,
                    lost=ease((state["run"] - .8) / .2) if resets else 0.)

    def key(self, name, state):
        """What a tile shows: equal keys mean an identical tile, so it is not painted again."""
        data = self.data
        if name == "winner":
            return (state["settled"],)
        column, part = self.columns[name[0]], name[1]
        rows = [state["rows"][row] for row in column["window"]["rows"]]
        if part == "bars":
            spare = any(data["rows"][row]["spare"] > 0 for row in column["window"]["rows"])
            # A spend leaving its bar and a drain falling onto it are in flight over this tile.
            flying = any(0 < row["laid"] < .2 or 0 < row["paid"] < 1 for row in rows)
            return (state["cut"] if spare else 0., state["joined"] if data["tied"] else 0., state["quiet"],
                    tuple((row["grown"], row["paid"], row["done"], row["won"], row["heat"]) for row in rows),
                    state["story"] if flying else None)
        flying = any(0 < row["laid"] < 1 for row in rows)
        face = self.window_state(column, state)
        laid = any(row["laid"] > 0 for row in rows)
        # Beads about to be lost are restless until the reset takes them.
        restless = face["resets"] and state["wound"] >= 1 and face["lost"] < 1 and state["story"] < PHASES["run"][1]
        return (state["wound"], face["hand"], face["eaten"], face["spent"], face["lost"], state["quiet"] if laid else 0.,
                tuple((row["laid"], row["done"]) for row in rows),
                state["story"] if flying else state["beat"] if restless else None)

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
            if name == "winner":
                self.winner(state)
            else:
                self.column(self.columns[name[0]], state, name[1])
            self.live.paste(tile.reduce(k), (x0, y0))
            changed.append(name)
        self.ox = self.oy = 0
        return changed

    def frame(self, story=DURATION):
        self.advance(story)
        return self.live.copy()

    # --- one window: its clock, then its candidates ---------------------------------------------------
    def column(self, column, state, part):
        if part == "clock":
            self.clock(column, state, self.window_state(column, state))
        else:
            self.level_line(column, state)
        for row in column["window"]["rows"]:
            if part == "bars":
                self.bar(column, row, state)
            self.spend(column, row, state, part == "clock")

    def clock(self, column, state, face):
        window, shown = column["window"], state["wound"]
        if shown <= 0:
            return
        radius, thick, story = self.radius, self.thick, state["beat"]
        self.arc(column, radius, thick, 0., 1., TRACK, min(1., shown * 3))
        used, hand = face["used"], face["hand"]
        # Spend behind the hand is quiet; spend that ran past the hand is the window's debt.
        self.arc(column, radius, thick, 0., min(used, hand), USED)
        self.arc(column, radius, thick, hand, used, AHEAD)
        # The winner's own spend, once the clocks run: it eats beads, or runs hot past the hand.
        self.arc(column, radius, thick, used, used + face["eaten"], UNDER)
        self.arc(column, radius, thick, used + face["eaten"], used + face["spent"], OVER)
        front, mid = used + face["spent"], radius - thick / 2
        size = min(2.1, thick * .3)
        for bead in range(column["beads"]):
            at = (bead + .5) / column["beads"]
            if not front < at < hand:
                continue
            if face["resets"]:
                # About to be lost: restless while the hand closes on twelve, then gone with the reset.
                flicker = .62 + .38 * math.sin(story * 8.5 + bead * 1.9) if story < PHASES["run"][1] else 1.
                lost = face["lost"]
                self.dot(*self.on_face(column, at, mid), size, ASH, lost)
                self.dot(*self.on_face(column, at, mid + 10 * lost), size * (1 - .4 * lost), FREE, flicker * (1 - lost))
            else:
                self.dot(*self.on_face(column, at, mid), size, FREE)
        # Twelve o'clock is the reset: a fixed notch the hand closes on.
        if window["period"]:
            self.rule(self.on_face(column, 0., radius - thick - 2.5), self.on_face(column, 0., radius + 2.5), theme.FAINT, 1.6,
                      min(1., shown * 3))
        seen = min(1., shown * 3)
        self.rule(self.on_face(column, hand, radius + 2), self.on_face(column, hand, radius + LANE_ROOM - 2), theme.MUTED, 1., seen * .7)
        self.rule((column["cx"], self.cy), self.on_face(column, hand, radius + 2), theme.INK, 1.9, seen)
        self.dot(*self.on_face(column, hand, radius + 2), .95, theme.INK, seen)
        self.dot(column["cx"], self.cy, 2.6, theme.INK, seen)
        self.dot(column["cx"], self.cy, 1., theme.CARD, seen)

    def level_line(self, column, state):
        """Equal N: one level across the tied bars, wherever their windows stand."""
        data, joined = self.data, state["joined"]
        if not data["tied"] or joined <= 0:
            return
        ends = []
        for index in range(data["tied"]):
            owner = next(c for c in self.columns if index in c["bars"])
            ends += owner["bars"][index]
        left, right = min(ends) - 5, max(ends) + 5
        right = left + (right - left) * joined
        y = self.level(data["rows"][0]["score"])
        if min(column["x1"], right) > max(column["x0"], left):
            self.rule((max(column["x0"], left), y), (min(column["x1"], right), y), theme.INK, 1.,
                      .75 - .3 * state["quiet"], dash=(2.5, 2.5))

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

    def bar(self, column, row, state):
        data, item, live = self.data, self.data["rows"][row], state["rows"][row]
        x0, x1 = column["bars"][row]
        middle = (x0 + x1) / 2
        settled, color = state["quiet"], self.tone(row, state)
        grown = live["grown"]
        solid, fit, whole = self.heights(row, state)
        if grown > 0:
            cut = state["cut"]
            top, edge = self.level(whole * grown), self.level(min(fit, whole * grown))
            if item["spare"] > 0 and top < edge:
                # Ability beyond the need: cut off with a surface gap and left pale. It does not count.
                self.box(x0, top, x1, edge - 1.6 * cut, color, 1 - .8 * cut, round_top=3.5)
            if live["paid"] > 0:
                # What over-line spend costs: hot while it drains, then a pale scar.
                self.box(x0, edge, x1, self.level(solid) - 1.2, mix(BURNT, OVER, live["heat"]), .55 + .45 * live["heat"],
                         round_top=0 if item["spare"] > 0 else 3.5)
            self.box(x0, self.level(min(solid, whole * grown)), x1, self.base_y, color,
                     round_top=3.5 if cut > 0 or item["spare"] <= 0 or live["paid"] > 0 else 0)
            if data["tied"] and live["done"] > 0 and row in data["raced"]:
                # The race, where the tie is: equal bars fill at each candidate's own speed.
                strength = 1. if row == data["winner"] else .5 * (1 - settled)
                self.box(x0, self.level(solid * live["done"]), x1, self.base_y, mix(SAME_MODEL, WINNER, 1.), strength,
                         round_top=3.5 if live["done"] >= 1 else 0)
            if item["fast"] and grown >= 1 and x1 - x0 >= 7:
                self.chevrons(middle, self.base_y - 9, min(3.2, (x1 - x0) * .2))

    def spend(self, column, row, state, arcs):
        """This candidate's spend on its window's clock. Without `arcs`, only what is in flight to or from its bar."""
        data, item, live = self.data, self.data["rows"][row], state["rows"][row]
        window, settled = column["window"], state["quiet"]
        laid = live["laid"]
        if laid <= 0 or window["capacity"] <= 0 or not (arcs or laid < 1):
            return
        x0, x1 = column["bars"][row]
        middle, color = (x0 + x1) / 2, self.tone(row, state)
        solid, fit, whole = self.heights(row, state)
        capacity = window["capacity"]
        outer = self.lane_radius(column, row)
        start = clamp(window["used"] / capacity)
        under, total = (item["spend"] - item["over"]) / capacity, item["spend"] / capacity
        tip = self.level(whole) - 3
        entry = self.on_face(column, start, outer - LANE / 2)
        if laid < .2:
            # The spend leaves its bar for the clock.
            u = ease(laid / .2)
            self.dot(*self.curve((middle, tip), entry, u), 2.2, color)
            return
        drawn = live["drawn"] * total
        # A raced candidate's planned arc turns solid as its run progresses; the winner's moves onto the face.
        merged = live["done"] if row == data["winner"] else 0.
        solid_part = live["done"] * total if row != data["winner"] else 0.
        fade = (1 - merged) * (1 - .45 * settled * (row != data["winner"]))
        head = self.on_face(column, min(1., start + drawn), outer - LANE / 2)
        if arcs:
            self.arc(column, outer, LANE, start, start + min(drawn, under), PLANNED, fade)
            self.arc(column, outer, LANE, start, start + min(solid_part, under) * (1 - settled), UNDER, fade)
            self.arc(column, outer, LANE, start + under, start + drawn, OVER, fade)
            if laid < 1:
                self.dot(*head, 2., OVER if drawn > under else UNDER)
            elif 0 < live["done"] < 1 and row != data["winner"]:
                self.dot(*self.on_face(column, min(1., start + solid_part), outer - LANE / 2), 2., UNDER)
        if 0 < live["paid"] < 1:
            # Simultaneous cause and effect: over-line spend falls from the arc and takes the bar's top with it.
            target = (middle, self.level(solid))
            for speck in range(5):
                u = (state["story"] * 2.4 + speck / 5) % 1
                self.dot(*self.curve(head, target, u), 1.7 - .5 * u, OVER, math.sin(math.pi * u) ** .6)

    def curve(self, start, end, u):
        """A point on the gentle arc between a bar's top and its clock."""
        bend = (start[0] + (end[0] - start[0]) * .5 + (end[1] - start[1]) * .12, (start[1] + end[1]) / 2)
        a = (lerp(start[0], bend[0], u), lerp(start[1], bend[1], u))
        b = (lerp(bend[0], end[0], u), lerp(bend[1], end[1], u))
        return lerp(a[0], b[0], u), lerp(a[1], b[1], u)

    def chevrons(self, x, y, size):
        """Fast mode: » on the bar."""
        for shift in (-size * .55, size * .55):
            points = [(self.X(x + shift - size * .45), self.Y(y - size)), (self.X(x + shift + size * .45), self.Y(y)),
                      (self.X(x + shift - size * .45), self.Y(y + size))]
            self.draw.line(points, fill=(255, 255, 255, 235), width=max(1, round(self.p(1.25))), joint="curve")

    # --- the winner's name, under its bar ----------------------------------------------------------------
    def winner(self, state):
        shown = state["settled"]
        if shown <= 0:
            return
        data = self.data
        row = data["rows"][data["winner"]]
        column = next(c for c in self.columns if data["winner"] in c["bars"])
        x0, x1 = column["bars"][data["winner"]]
        middle = (x0 + x1) / 2
        limit = self.right - 28
        width = min(limit, self.width(row["label"], BODY, True)) + 20
        left = min(max(4., middle - width / 2), self.right - 4 - width)
        y = self.pill_y + 4 * (1 - shown)
        top = self.strip_y + 3
        xy = (self.X(left), self.Y(y - 11), self.X(left + width), self.Y(y + 11))
        self.draw.polygon([(self.X(middle - 5), self.Y(y - 10.5)), (self.X(middle), self.Y(top)),
                           (self.X(middle + 5), self.Y(y - 10.5))], fill=self.ink(theme.ACCENT_SOFT, shown))
        self.draw.rounded_rectangle(xy, radius=self.p(8), fill=self.ink(theme.ACCENT_SOFT, shown))
        self.text(row["label"], left + width / 2, y - .5, BODY, theme.ACCENT, True, anchor="mm", limit=limit, alpha=shown)

    # --- hover text, for the page ---------------------------------------------------------------------------
    def tip_at(self, x, y):
        """What the mark under a screen point stands for, in words; '' over empty paper."""
        if not self.data:
            return ""
        x, y = x / self.s, y / self.s
        for column in self.columns:
            window = column["window"]
            if math.hypot(x - column["cx"], y - self.cy) <= self.radius + LANE_ROOM:
                gap = window["line"] - window["used"]
                state = (_tr("恢复线下还有 {0} 未用").format(usd(gap)) if gap >= 0
                         else _tr("比恢复线多用了 {0}").format(usd(-gap)))
                return _tr("{0}：本期已用 {1} / {2}，{3}；{4}").format(
                    window["label"], usd(window["used"]), usd(window["capacity"]), reset_text(window["reset"]), state)
            for row, (x0, x1) in column["bars"].items():
                item = self.data["rows"][row]
                if x0 - 3 <= x <= x1 + 3 and self.level(item["fit"] + item["spare"]) - 3 <= y <= self.base_y + 3:
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
                self.canvas.itemconfigure(held[1], state="normal")
            else:
                if held is not None:
                    self.canvas.delete(held[1])
                photo = ImageTk.PhotoImage(strip, master=self.canvas)
                self.strips[name] = (photo, self.canvas.create_image(box[0], box[1], anchor="nw", image=photo))
