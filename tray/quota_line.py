"""Quota-line view of one decision, told by motion instead of words.

Each window is a horizontal track whose length is the period's quota. Used quota fills it from the left. The
recovery line ℓ(t) = C·(t − t0)/L is a marker that advances along the track on the window's own clock and
reaches the right end at the reset, so "behind the line" and "past the line" are literally left and right of
it. Between the fill and the marker lies quota that has recovered and is still unspent. Under every track,
on the same scale, each candidate that would spend there lays its spend from the end of the fill: the part
behind the line is free, the part past it turns red and drains that candidate's score bar, which stands in
the same row: N = F − β·X/h, with the ability beyond the task's need cut off as a pale cap. Then the lines
advance for the length of the task: tied candidates race, and free quota the line carries to the reset is
lost unless the winner has spent it first.

Everything shown is read from the decision snapshot; the page never re-plans or re-prices a candidate.
It needs (names follow the quota-economy design, round 047):

    snapshot["quota_line"]["windows"][key] = {label, capacity_usd (C), used_usd (U), elapsed_hours (now − t0),
                                              reset_in_hours (T − now; None = never resets), beta (β_w)}
    ranking row: score (N), components.fit (F), bundle.fast, five_axis_fit[axis] = {demand, score, target},
                 quota_line = {window, hours (h), spend_usd (b·h), over_line_usd (X, net of substitute pools),
                               decided_by ("N" | "earliest_expiry" | "faster" | "cheaper": the sort key that put this
                               row below the previous one)}

Only geometry is derived here: the line's place (now − t0)/L, the lengths U/C and b·h/C, the bar ends F
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
PHASES = {"clock": (.35, 2.3),      # the tracks appear; each line and its window's spend run up to now
          "rise": (2.0, 3.1),       # score bars grow to each candidate's whole ability
          "cap": (3.1, 3.9),        # ability beyond the task's need is cut off and left pale
          "plan": (3.9, 6.2),       # each candidate's spend is laid under its track; over-line spend drains its bar
          "tie": (6.0, 6.5),        # equal N: a line joins the ends of the tied bars
          "run": (6.5, 8.6),        # the lines advance for the task: tied candidates race, expiring quota is lost
          "settle": (8.6, 9.35)}    # the winner is named
FIRST, LAST = min(a for a, _ in PHASES.values()), max(b for _, b in PHASES.values())
NOMINAL = (330, 200)      # layout size in 96-dpi pixels; a smaller canvas scales the whole view down
MIN_HEIGHT = 190          # below this the view shrinks with the height too
ROW_PITCH = 29.           # tallest a row gets: one per window track and one per candidate
TILE_PAD = 4.             # how far a recovery line's handle may reach above the first block
SUPERSAMPLE = 2
BODY, SMALL = 12, 11      # px at 96 dpi: the tray's 9 pt body text is 12 px
# Colour carries the legend. Quiet tones are the state of a window, saturated ones are this task.
TRACK, USED, AHEAD, ASH = "#EDF1F6", "#B4C0D0", "#FDA4AF", "#D5DCE6"
FREE, PLANNED, UNDER, OVER, BURNT = "#A7F3D0", "#34D399", theme.GREEN, theme.RED, "#FECACA"
WINNER, SAME_MODEL, SAME_MODEL_QUIET, OTHER, OTHER_QUIET = theme.ACCENT, "#818CF8", "#A5B4FC", "#A3B1C6", "#C3CCD9"
QUIET_STEPS, BEATS = 8, 15        # steps in which the losers recede; ticks per second of quota about to expire
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
    # Windows keep their recorded order, so replaying another decision does not shuffle the tracks.
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
                # The task runs for the longest of the raced candidates; a candidate without hours takes all of it.
                span=max([rows[index]["hours"] or 0. for index in raced]) or 1.,
                top=max(row["fit"] + row["spare"] for row in rows) or 1.)


class QuotaLineRenderer:
    """Draws the view at a story time; `frame(DURATION)` is the settled, static picture.

    Everything that never moves (window names, empty tracks, the scores' zero line) is drawn once into a base
    layer. Each window's block is cut into three tiles: its candidates' names, its track and spend lanes, and
    its candidates' scores. A frame redraws only the tiles whose state changed since the last one; the margins
    around the blocks belong to no tile and are never painted again.
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
        count, lines = len(windows), len(windows) + len(data["rows"])
        gap = min(12., max(3., self.bottom * .03))
        self.pitch = min(ROW_PITCH, (self.bottom - 10 - gap * (count - 1)) / lines)
        top = (self.bottom - self.pitch * lines - gap * (count - 1)) / 2
        # Three columns: names, the windows' tracks (one USD scale per window), the candidates' scores.
        self.gutter = max(76., min(88., self.right * .2))
        self.tx0 = self.gutter + 6
        self.tx1 = self.tx0 + (self.right - self.tx0 - 8) * .54
        self.sx0, self.sx1 = self.tx1 + 20, self.right - 8
        self.track_h = max(5., min(10., self.pitch * .4))
        self.lane_h = max(3., min(5.5, self.pitch * .2))
        self.bar_h = max(6., min(13., self.pitch * .5))
        self.blocks, y = [], top
        for window in windows:
            rows = {row: y + self.pitch * (slot + 1.5) for slot, row in enumerate(window["rows"])}
            self.blocks.append(dict(window=window, top=y, y=y + self.pitch / 2, rows=rows,
                                    bottom=y + self.pitch * (1 + len(rows))))
            y = self.blocks[-1]["bottom"] + gap
        # Tile boxes in screen pixels: three columns, cut between the windows' blocks.
        names = min(self.w, round((self.gutter + 1) * self.s))
        split = min(self.w, round((self.tx1 + self.sx0) / 2 * self.s))
        edges = ([max(0, math.floor((top - TILE_PAD) * self.s))]
                 + [min(self.h, round((a["bottom"] + b["top"]) / 2 * self.s)) for a, b in zip(self.blocks, self.blocks[1:])]
                 + [min(self.h, math.ceil((self.blocks[-1]["bottom"] + TILE_PAD) * self.s))])
        for index in range(count):
            self.tiles[index, "names"] = (0, edges[index], names, edges[index + 1])
            self.tiles[index, "track"] = (names, edges[index], split, edges[index + 1])
            self.tiles[index, "bars"] = (split, edges[index], self.w, edges[index + 1])
        # A recovery line moves in steps of one drawn pixel: a window that barely moves is barely repainted.
        self.mark_steps = max(1, round((self.tx1 - self.tx0) * self.s * self.k))

    def tx(self, fraction):
        """Where a fraction of a window's capacity lies on its track."""
        return self.tx0 + (self.tx1 - self.tx0) * clamp(fraction)

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
        half = self.track_h / 2
        for block in self.blocks:
            window, y = block["window"], block["y"]
            self.text(window["label"], 6, y, SMALL, theme.TEXT, True, limit=self.gutter - 6)
            # The whole track is the period's quota; its right end is the reset.
            self.box(self.tx0, y - half, self.tx1, y + half, TRACK, radius=2.)
            if window["period"]:
                self.rule((self.tx1, y - half - 3), (self.tx1, y + half + 3), theme.FAINT, 1.6)
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
        # The losers recede in a few steps and expiring quota ticks: neither needs a repaint on every frame.
        out = dict(story=story, wound=wound, run=run, settled=settled, cut=cut, joined=joined, rows=[],
                   quiet=round(settled * QUIET_STEPS) / QUIET_STEPS, beat=math.floor(story * BEATS) / BEATS)
        for index, row in enumerate(rows):
            grown = ease(staggered(lin(story, "rise"), index, count, .18))
            laid = staggered(lin(story, "plan"), index, count, .32)
            drawn = ease(laid)                                                # share of the spend that is laid
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

    def window_state(self, block, state):
        """Where a window's recovery line and spend stand, as fractions of its capacity."""
        window, data = block["window"], self.data
        capacity = window["capacity"] or 1.
        used = clamp(window["used"] / capacity)
        mark, resets = window["at"], False
        if window["period"]:
            mark = min(1., window["at"] + state["run"] * data["span"] / window["period"])
            if 0 < state["run"] < 1:
                mark = max(window["at"], math.floor(mark * self.mark_steps) / self.mark_steps)
            resets = window["reset"] <= data["span"] * (1 + 1e-6)
        winner = data["rows"][data["winner"]]
        eaten = spent = 0.
        if winner["window"] == window["key"]:
            done = state["rows"][data["winner"]]["done"]
            spent = clamp(done * winner["spend"] / capacity)
            eaten = clamp(min(done * winner["spend"], winner["spend"] - winner["over"]) / capacity)
        return dict(used=used * state["wound"], mark=mark * state["wound"], resets=resets, eaten=eaten, spent=spent,
                    lost=ease((state["run"] - .8) / .2) if resets else 0.)

    def key(self, name, state):
        """What a tile shows: equal keys mean an identical tile, so it is not painted again."""
        data = self.data
        block, part = self.blocks[name[0]], name[1]
        ids = list(block["rows"])
        rows = [state["rows"][row] for row in ids]
        # A drain in flight crosses from the spend lane to the score and moves with the clock itself.
        flying = any(0 < row["paid"] < 1 for row in rows)
        named = state["settled"] if data["winner"] in ids else 0.
        if part == "names":
            return (min(1., state["wound"] * 3), state["quiet"], named)
        if part == "bars":
            spare = any(data["rows"][row]["spare"] > 0 for row in ids)
            tied = data["tied"] and any(row < data["tied"] for row in ids)
            return (state["cut"] if spare else 0., state["joined"] if tied else 0., state["quiet"], named,
                    tuple((row["grown"], row["paid"], row["done"], row["won"], row["heat"]) for row in rows),
                    state["story"] if flying else None)
        face = self.window_state(block, state)
        laid = any(row["laid"] > 0 for row in rows)
        # Quota about to expire is restless until the reset takes it.
        restless = face["resets"] and state["wound"] >= 1 and face["lost"] < 1 and state["story"] < PHASES["run"][1]
        return (state["wound"], face["mark"], face["eaten"], face["spent"], face["lost"], state["quiet"] if laid else 0.,
                named, tuple((row["laid"], row["done"]) for row in rows),
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
            self.block(self.blocks[name[0]], state, name[1])
            self.live.paste(tile.reduce(k), (x0, y0))
            changed.append(name)
        self.ox = self.oy = 0
        return changed

    def frame(self, story=DURATION):
        self.advance(story)
        return self.live.copy()

    # --- one window: its candidates' names, its track and their spend lanes, their scores ---------------
    def block(self, block, state, part):
        data = self.data
        if data["winner"] in block["rows"] and state["settled"] > 0:
            # The winner's row, across both columns: its name is the one that lights up.
            y, half = block["rows"][data["winner"]], min(self.pitch / 2 - 1, self.bar_h / 2 + 5)
            self.box(3, y - half, self.right - 3, y + half, theme.ACCENT_SOFT, state["settled"], radius=6.)
        if part == "track":
            self.track(block, state, self.window_state(block, state))
        elif part == "bars":
            self.level_line(block, state)
        for row in block["rows"]:
            if part == "names":
                self.name(block, row, state)
                continue
            if part == "track":
                self.lane(block, row, state)
            else:
                self.score(block, row, state)
            self.drain(block, row, state)

    def track(self, block, state, face):
        window, shown = block["window"], state["wound"]
        if shown <= 0:
            return
        y, half, x = block["y"], self.track_h / 2, self.tx
        used, mark = face["used"], face["mark"]
        front = used + face["spent"]
        if window["period"] and mark > front:
            # Recovered and not spent: free to use, and gone at the reset.
            lost, calm = face["lost"], 1.
            if face["resets"] and state["story"] < PHASES["run"][1]:
                calm = .62 + .38 * math.sin(state["beat"] * 8.5)       # restless while the line closes on the reset
            thin = half * (1 - .55 * lost)
            self.box(x(front), y - thin, x(mark), y + thin, mix(FREE, ASH, lost), calm * (1 - .45 * lost))
        if block["rows"]:
            # Where this window's spend stands: every candidate's spend starts under this point.
            low = max(block["rows"].values()) + self.lane_h / 2 + 2
            self.rule((x(used), y + half + 1), (x(used), low), theme.FAINT, 1., min(1., shown * 3) * .55, dash=(1.5, 2.5))
        # Spend behind the line is quiet; spend that ran past the line is the window's debt.
        self.box(x(0.), y - half, x(min(used, mark)), y + half, USED, radius=2., right=False)
        self.box(x(mark), y - half, x(used), y + half, AHEAD)
        # The winner's own spend, once the task runs: it takes the free quota, or runs hot past the line.
        self.box(x(used), y - half, x(used + face["eaten"]), y + half, UNDER)
        self.box(x(used + face["eaten"]), y - half, x(front), y + half, OVER)
        if window["period"]:
            # The recovery line: it advances with the clock and crosses the spends planned on this window.
            seen, at = min(1., shown * 3), x(mark)
            low = max(block["rows"].values()) + self.lane_h / 2 + 2 if block["rows"] else y + half + 3
            self.rule((at, y + half + 1), (at, low), theme.INK, 1., seen * .28)
            self.rule((at, y - half - 3), (at, y + half + 2), theme.INK, 1.8, seen)
            self.draw.polygon([(self.X(at - 3.4), self.Y(y - half - 7)), (self.X(at + 3.4), self.Y(y - half - 7)),
                               (self.X(at), self.Y(y - half - 2))], fill=self.ink(theme.INK, seen))

    def name(self, block, row, state):
        data, item = self.data, self.data["rows"][row]
        shown = min(1., state["wound"] * 3)
        won = state["settled"] if row == data["winner"] else 0.
        quiet = state["quiet"] if row != data["winner"] else 0.
        self.text(item["name"], 12, block["rows"][row], SMALL, mix(theme.MUTED, theme.ACCENT, won), won >= .5,
                  limit=self.gutter - 12, alpha=shown * (1 - .35 * quiet))

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

    def spend(self, block, row):
        """Where a candidate's spend would lie on its window's track: (start, end of the free part, end)."""
        item, window = self.data["rows"][row], block["window"]
        capacity = window["capacity"]
        if capacity <= 0:
            return None
        start = clamp(window["used"] / capacity)
        return start, start + (item["spend"] - item["over"]) / capacity, start + item["spend"] / capacity

    def lane(self, block, row, state):
        """What this candidate would spend, laid under its window's track on the same scale."""
        data, live, span = self.data, state["rows"][row], self.spend(block, row)
        if live["laid"] <= 0 or span is None:
            return
        start, free, end = span
        head = start + live["drawn"] * (end - start)
        y, half, x = block["rows"][row], self.lane_h / 2, self.tx
        # The winner's lane moves onto the track as it runs; the others fade once the winner is named.
        fade = (1 - live["done"]) if row == data["winner"] else 1 - .5 * state["quiet"]
        self.box(x(start), y - half, x(min(head, free)), y + half, PLANNED, fade, radius=half, right=head <= free)
        self.box(x(free), y - half, x(head), y + half, OVER, fade, radius=half, left=False)
        if live["laid"] < 1:
            self.dot(x(head), y, half + 1.1, OVER if head > free else UNDER)

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
            # What over-line spend costs: hot while it drains, then a pale scar.
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
        """Simultaneous cause and effect: over-line spend leaves the lane and takes the end of the score with it."""
        live, span = state["rows"][row], self.spend(block, row)
        if not 0 < live["paid"] < 1 or span is None:
            return
        start, free, end = span
        y = block["rows"][row]
        x0, x1 = self.tx(start + live["drawn"] * (end - start)), self.sx(self.heights(row, state)[0])
        for speck in range(5):
            u = (state["story"] * 2.4 + speck / 5) % 1
            lift = math.sin(math.pi * u)
            self.dot(lerp(x0, x1, u), y - (self.bar_h / 2 + 3) * lift, 1.7 - .5 * u, OVER, lift ** .6)

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
        """What the row under a screen point stands for, in words; '' over empty paper."""
        if not self.data:
            return ""
        x, y = x / self.s, y / self.s
        for block in self.blocks:
            window = block["window"]
            if abs(y - block["y"]) <= self.pitch / 2:
                gap = window["line"] - window["used"]
                state = (_tr("恢复线下还有 {0} 未用").format(usd(gap)) if gap >= 0
                         else _tr("比恢复线多用了 {0}").format(usd(-gap)))
                return _tr("{0}：本期已用 {1} / {2}，{3}；{4}").format(
                    window["label"], usd(window["used"]), usd(window["capacity"]), reset_text(window["reset"]), state)
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
