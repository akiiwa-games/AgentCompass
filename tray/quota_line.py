"""Quota-line view of one decision: recovery line per window, capped fit per axis, N = F − β·X/h per candidate.

Everything shown is read from the decision snapshot; the page never re-plans or re-prices a candidate.
It needs (names follow the quota-economy design, round 047):

    snapshot["quota_line"]["windows"][key] = {label, capacity_usd (C), used_usd (U), elapsed_hours (now − t0),
                                              reset_in_hours (T − now; None = never resets), beta (β_w)}
    ranking row: score (N), components.fit (F), bundle.fast, five_axis_fit[axis] = {demand, score, target},
                 quota_line = {window, hours (h), spend_usd (b·h), over_line_usd (X, net of substitute pools),
                               decided_by ("N" | "earliest_expiry" | "faster" | "cheaper": the sort key that put this
                               row below the previous one)}

Only geometry is derived here: L = elapsed + reset_in, ℓ(now) = C·elapsed/L, the bar ends F and N.
The model and the renderer are plain Python + Pillow, so they are tested without a display.
"""
from __future__ import annotations

from PIL import Image, ImageColor, ImageDraw, ImageTk

from i18n import translate as _tr
import display_names
import ui_theme as theme
from decision_theatre import DURATION, ease, finite, font
from radar_chart import clamp

AXES = (("divergence", "发散创新"), ("global_rigor", "全局严密"), ("module_precision", "模块精准"),
        ("vision_tools", "视觉与工具"))
ROWS, WINDOWS = 5, 3
# Story-time windows on the decision film's clock (decision_theatre): each block fills in while the film
# reaches the matching beat, so the two canvases read as one explanation.
PHASES = {"axes": (2.85, 4.3), "line": (4.2, 5.2), "spend": (5.2, 6.4), "fit": (6.2, 7.0),
          "cost": (6.9, 7.65), "tie": (7.9, 9.0)}
FIRST, LAST = min(a for a, _ in PHASES.values()), max(b for _, b in PHASES.values())
NOMINAL = (420, 388)      # layout size in 96-dpi pixels; a smaller canvas scales the whole view down
# Height of each block, top to bottom. A short canvas drops blocks (first the per-axis fit, which the radar
# also shows, then the windows) before it lets the type shrink below 90% of what the width allows.
BLOCKS = (("header", 30), ("windows", 110), ("axes", 96), ("scores", 134), ("tie", 18))
TIERS = ((), ("axes",), ("axes", "windows"))
SUPERSAMPLE = 2
BODY, SMALL, TITLE = 12, 11, 14       # px at 96 dpi: the tray's 9 pt body text is 12 px
UNDER, OVER = theme.GREEN, theme.RED
TIE_KEYS = ("earliest_expiry", "faster", "cheaper")
TIE_STEPS = {"earliest_expiry": "最早到期的线下额度", "faster": "更快完成", "cheaper": "花费更少"}
TIE_BADGES = {"earliest_expiry": "到期更晚", "faster": "更慢", "cheaper": "更贵"}


def phase(story, name):
    start, end = PHASES[name]
    return ease((story - start) / (end - start))


def lerp(a, b, t):
    return a + (b - a) * t


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


def model(result):
    """Display model of a snapshot, or None when it carries no quota-line record (older decisions)."""
    block = (result or {}).get("quota_line")
    windows = block.get("windows") if isinstance(block, dict) else None
    if not isinstance(windows, dict) or not windows:
        return None
    rows, axes = [], []
    for item in result.get("ranking") or []:
        line, comp, bundle = item.get("quota_line"), item.get("components") or {}, item.get("bundle") or {}
        if (item.get("excluded") or not isinstance(line, dict) or line.get("window") not in windows
                or not finite(item.get("score")) or not finite(comp.get("fit"))):
            continue
        name = display_names.model_label(str(bundle.get("model") or item.get("id") or ""))
        fit = item.get("five_axis_fit")
        if isinstance(fit, dict) and len(axes) < 2 and name not in [a["name"] for a in axes]:
            axes.append(dict(name=name, fit=float(comp["fit"]), values={
                key: (float(row.get("score") or 0.), float(row.get("target") or 0.), row.get("demand") or 0)
                for key, row in fit.items() if isinstance(row, dict)}))
        if len(rows) < ROWS:
            label = display_names.candidate_label({"id": item.get("id"), **bundle})
            spend, over = float(line.get("spend_usd") or 0.), max(0., float(line.get("over_line_usd") or 0.))
            rows.append(dict(id=item.get("id"), name=name, label=label, window=line["window"], window_label=str(windows[line["window"]].get("label") or line["window"]),
                             fit=float(comp["fit"]), score=float(item["score"]), hours=line.get("hours"),
                             spend=spend, over=min(over, spend),
                             decided_by=line.get("decided_by") if line.get("decided_by") in TIE_KEYS else None))
    if not rows:
        return None
    # Windows keep their recorded order, so replaying another decision does not shuffle them. Each shows one
    # candidate's spend: the winner's model where it can run there (the same spend, under one line and over
    # another), otherwise the best-ranked candidate that draws on the window.
    shown = []
    for key, window in windows.items():
        here = [row for row in rows if row["window"] == key]
        row = next((row for row in here if row["name"] == rows[0]["name"]), here[0] if here else None)
        if row is None or len(shown) == WINDOWS:
            continue
        at, line = window_geometry(window)
        shown.append(dict(key=key, label=row["window_label"], at=at, line=line,
                          capacity=float(window.get("capacity_usd") or 0.), used=float(window.get("used_usd") or 0.),
                          reset=window.get("reset_in_hours") if finite(window.get("reset_in_hours")) else None,
                          spend=row["spend"], over=row["over"]))
    # The tie is the run of rows the allocator separated from the winner by a tie-break key, not by N.
    tied = 1
    while tied < len(rows) and rows[tied]["decided_by"]:
        tied += 1
    return dict(rows=rows, windows=shown, axes=axes, tied=tied if tied > 1 else 0,
                top=max(row["fit"] for row in rows) or 1.)


class QuotaLineRenderer:
    """Draws the view at a story time; `frame(DURATION)` is the settled, static picture.

    Everything that never moves (type, tracks, plot frames) is drawn once into a base layer. The view
    is four horizontal bands (windows, axes, scores, tie-break); a frame redraws only the bands whose
    phases moved since the last one, so most frames repaint a quarter of the picture or nothing.
    """

    def __init__(self, data, width, height, scale=1.):
        self.data = data
        self.w, self.h = max(1, int(width)), max(1, int(height))
        fit = min(scale, self.w / NOMINAL[0])
        for dropped in TIERS:
            shown = [(name, size) for name, size in BLOCKS if name not in dropped]
            total = sum(size for _, size in shown)
            if self.h / total >= .9 * fit:
                break
        self.s = min(fit, self.h / total)
        self.k = SUPERSAMPLE
        self.right = self.w / self.s
        # Extra height goes between the blocks, never into the type.
        gap = max(0., self.h / self.s - total) / max(1, len(shown) - 2)
        self.y, at = {}, 0.
        for index, (name, size) in enumerate(shown):
            self.y[name] = at
            at += size + (gap if 0 < index < len(shown) - 1 else 0.)
        self.oy = 0
        self.base = self.static() if data else self.empty()
        self.live = self.base.reduce(self.k)
        # band name -> (painter, the phases it depends on, first and last screen row)
        bands = [band for band in (("windows", self.windows, ("line", "spend")), ("axes", self.axes, ("axes",)),
                                   ("scores", self.scores, ("fit", "cost", "tie")), ("tie", self.tie, ("tie",)))
                 if band[0] in self.y]
        edges = [round(self.y[name] * self.s) for name, _, _ in bands] + [self.h]
        self.bands = {name: (paint, phases, edges[i], edges[i + 1]) for i, (name, paint, phases) in enumerate(bands)}
        self.drawn = {}
        self.repaints = 0

    # --- drawing helpers: coordinates are 96-dpi pixels, converted once here -------------------
    def p(self, value):
        return value * self.s * self.k

    def width(self, value, size, bold=False):
        return font(round(size * self.s * self.k), bold).getlength(str(value)) / (self.s * self.k)

    def text(self, value, x, y, size=BODY, color=theme.INK, bold=False, anchor="lm", limit=None, alpha=1.):
        if alpha <= .01:
            return 0.
        f, value = font(round(size * self.s * self.k), bold), str(value)
        if limit is not None and f.getlength(value) > self.p(limit):
            while value and f.getlength(value + "…") > self.p(limit):
                value = value[:-1]
            value += "…"
        self.draw.text((self.p(x), self.p(y) - self.oy), value, font=f, anchor=anchor,
                       fill=(*ImageColor.getrgb(color), round(255 * clamp(alpha))))
        return f.getlength(value) / (self.s * self.k)

    def box(self, x0, y0, x1, y1, color, alpha=1.):
        if x1 - x0 <= 0 or y1 - y0 <= 0 or alpha <= .01:
            return
        self.draw.rectangle((self.p(x0), self.p(y0) - self.oy, self.p(x1), self.p(y1) - self.oy),
                            fill=(*ImageColor.getrgb(color), round(255 * clamp(alpha))))

    def rule(self, start, end, color, width=1., alpha=1., dash=None):
        fill = (*ImageColor.getrgb(color), round(255 * clamp(alpha)))
        size = max(1, round(self.p(width)))
        (x0, y0), (x1, y1) = ((self.p(x), self.p(y) - self.oy) for x, y in (start, end))
        length = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** .5
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

    def canvas(self):
        image = Image.new("RGB", (self.w * self.k, self.h * self.k), theme.CARD)
        self.draw = ImageDraw.Draw(image, "RGBA")
        return image

    # --- geometry shared by the base layer and the moving marks -------------------------------
    def window_cells(self):
        gap = 14.
        cell = (self.right - 4 - gap * (WINDOWS - 1)) / WINDOWS
        y = self.y["windows"]
        for index, window in enumerate(self.data["windows"]):
            x0 = 2 + index * (cell + gap)
            yield window, x0, x0 + cell, y + 44, y + 84

    def axis_rows(self):
        left, right = 108., self.right - 4
        for row, (key, label) in enumerate(AXES):
            values = [candidate["values"].get(key) for candidate in self.data["axes"]]
            yield label, values, left, right, self.y["axes"] + 27 + row * 17

    def score_columns(self):
        """x of: window label, bar start, bar end, N, note's right edge."""
        return 152., 212., self.right - 124, self.right - 118, self.right - 2

    def score_rows(self):
        for index, row in enumerate(self.data["rows"]):
            yield index, row, self.y["scores"] + 28 + index * 22

    # --- base layer ------------------------------------------------------------------------------
    def empty(self):
        image = self.canvas()
        self.text(_tr("此决策没有额度恢复线记录"), self.right / 2, self.h / self.s / 2, BODY, theme.FAINT, anchor="mm",
                  limit=self.right - 16)
        return image

    def static(self):
        image = self.canvas()
        y = self.y["header"]
        used = self.text("N = F − β × X / h", 2, y + 10, TITLE, theme.INK, True)
        x = self.right - 2
        for color, label in ((OVER, _tr("越线：每 USD 计 β")), (UNDER, _tr("线下：不计成本"))):
            width = self.width(label, SMALL)
            if x - width - 13 < used + 16:
                break
            self.text(label, x - width, y + 10, SMALL, theme.MUTED)
            self.box(x - width - 12, y + 6, x - width - 4, y + 14, color)
            x -= width + 26

        y = self.y.get("windows", 0.)
        if "windows" in self.y:
            self.text(_tr("恢复线：每个窗口按自己的周期"), 2, y + 7, BODY, theme.MUTED, True, limit=self.right - 4)
        for window, x0, x1, top, bottom in self.window_cells() if "windows" in self.y else ():
            left = max(0., window["capacity"] - window["used"]) / window["capacity"] if window["capacity"] > 0 else 0.
            share = _tr("剩 {0}%").format(f"{left * 100:.0f}")
            self.text(share, x1, y + 24, SMALL, theme.MUTED, anchor="rm")
            self.text(window["label"], x0, y + 24, BODY, theme.INK, True, limit=x1 - x0 - self.width(share, SMALL) - 6)
            self.text(reset_text(window["reset"]), x0, y + 37, SMALL, theme.MUTED, limit=x1 - x0)
            self.box(x0, top, x1, bottom, "#F8FAFC")
            self.rule((x0, bottom), (x1, bottom), "#CBD5E1", .75)

        y = self.y.get("axes", 0.)
        if "axes" in self.y:
            self.text(_tr("契合 F：能力只算到任务需要的高度"), 2, y + 7, BODY, theme.MUTED, True, limit=self.right - 4)
        for label, values, left, right, cy in self.axis_rows() if "axes" in self.y else ():
            needed = any(v and v[2] for v in values)
            self.text(_tr(label), 2, cy, SMALL, theme.MUTED if needed else theme.FAINT, limit=left - 8)
            self.box(left, cy - .5, right, cy + .5, theme.TRACK)
            if not needed:
                self.text(_tr("本任务不需要"), left + 6, cy - 7, 10, theme.FAINT, limit=right - left)

        y = self.y["scores"]
        self.text(_tr("每个候选的 N"), 2, y + 7, BODY, theme.MUTED, True, limit=self.right - 4)
        at, left, right, _, _ = self.score_columns()
        for index, row, cy in self.score_rows():
            self.text(row["label"], 2, cy, BODY, theme.ACCENT if index == 0 else theme.INK, limit=at - 8)
            self.text(row["window_label"], at, cy, SMALL, theme.FAINT, limit=left - at - 6)
            self.box(left, cy - 3.5, right, cy + 3.5, "#F1F5F9")
        return image

    # --- frame -------------------------------------------------------------------------------------
    def advance(self, story=DURATION):
        """Bring `self.live` to `story`; returns the names of the bands that had to be repainted."""
        changed = []
        if self.data is not None:
            k = self.k
            for name, (paint, phases, top, bottom) in self.bands.items():
                values = tuple(phase(story, each) for each in phases)
                if self.drawn.get(name) == values or bottom <= top:
                    continue
                self.drawn[name] = values
                self.repaints += 1
                band = self.base.crop((0, top * k, self.w * k, bottom * k))
                self.draw, self.oy = ImageDraw.Draw(band, "RGBA"), top * k
                paint(story)
                self.live.paste(band.reduce(k), (0, top))
                changed.append(name)
            self.oy = 0
        return changed

    def frame(self, story=DURATION):
        self.advance(story)
        return self.live.copy()

    def windows(self, story):
        drawn, land = phase(story, "line"), phase(story, "spend")
        if drawn <= 0:
            return
        y = self.y["windows"]
        for window, x0, x1, top, bottom in self.window_cells():
            capacity = window["capacity"] or 1.
            level = lambda value: bottom - (bottom - top) * clamp(value / capacity)
            # Recovery line ℓ(t) = C·(t − t0)/L over the whole period, drawn from t0 as time passes.
            if window["reset"] is not None:
                self.rule((x0, bottom), (x0 + (x1 - x0) * drawn, bottom - (bottom - top) * drawn), theme.MUTED, 1.,
                          dash=(3, 2.5))
            else:
                self.rule((x0, bottom - .8), (x0 + (x1 - x0) * drawn, bottom - .8), theme.MUTED, 1., dash=(3, 2.5))
            now = min(max(x0 + (x1 - x0) * window["at"], x0 + 5), x1 - 5)
            # Used so far (U), then the quota already recovered but unspent: what expires at the reset.
            self.box(now - 4, lerp(bottom, level(window["used"]), drawn), now + 4, bottom, theme.FAINT)
            if window["line"] > window["used"]:
                self.box(now - 4, level(window["line"]), now + 4, level(window["used"]), UNDER, .16 * drawn)
            if land > 0:
                under, base = window["spend"] - window["over"], window["used"]
                self.box(now - 4, level(base + under * land), now + 4, level(base), UNDER)
                if window["over"] > 0:
                    self.box(now - 4, level(base + under + window["over"] * land), now + 4, level(base + under), OVER)
                over = window["over"] > 0
                note = (_tr("{0} 越线").format(usd(window["over"])) if over
                        else _tr("{0} 在线下").format(usd(window["spend"])))
                self.text(note, x0, y + 95, SMALL, OVER if over else UNDER, True, alpha=land, limit=x1 - x0)

    def axes(self, story):
        grown = phase(story, "axes")
        if grown <= 0:
            return
        pair, y = self.data["axes"], self.y["axes"]
        x = self.right - 2
        for index in reversed(range(len(pair))):
            label = f"{pair[index]['name']} {pair[index]['fit']:.3f}"
            width = self.width(label, SMALL, True)
            self.text(label, x - width, y + 7, SMALL, theme.ACCENT if index == 0 else theme.CYAN, True, alpha=grown)
            x -= width + 12
        for label, values, left, right, cy in self.axis_rows():
            if not any(v and v[2] for v in values):
                continue
            target = next((v[1] for v in values if v), 0.)
            scale = lambda value: left + (right - left) * clamp(value)
            for index, value in enumerate(values):
                if not value:
                    continue
                color = theme.ACCENT if index == 0 else theme.CYAN
                top = cy - 5.5 + index * 6
                score = value[0] * grown
                self.box(left, top, scale(min(score, target)), top + 5, color)
                # Ability above the need is drawn, but pale: it does not count toward F.
                if score > target:
                    self.box(scale(target), top, scale(score), top + 5, color, .22)
            self.rule((scale(target), cy - 8), (scale(target), cy + 8), theme.INK, 1.2, grown)

    def scores(self, story):
        grown, paid, tie = phase(story, "fit"), phase(story, "cost"), phase(story, "tie")
        if grown <= 0:
            return
        tied = self.data["tied"]
        _, left, right, number, edge = self.score_columns()
        scale = lambda value: left + (right - left) * clamp(value / self.data["top"])
        for index, row, cy in self.score_rows():
            winner = index == 0
            cost = max(0., row["fit"] - row["score"])
            end, lost = row["fit"] * grown, cost * paid
            self.box(left, cy - 3.5, scale(end - lost), cy + 3.5, theme.ACCENT if winner else "#A5B4FC")
            if lost > 0:
                self.box(scale(end - lost), cy - 3.5, scale(end), cy + 3.5, OVER)
            self.text(f"{end - lost:.3f}", number + 6, cy, BODY, theme.ACCENT if winner else theme.INK, True, alpha=grown)
            if cost > 0:
                self.text(f"−{cost:.3f}", edge, cy, SMALL, OVER, True, anchor="rm", alpha=paid)
            elif tied and index < tied:
                note = _tr("胜出") if winner else _tr(TIE_BADGES[row["decided_by"]])
                self.text(note, edge, cy, SMALL, theme.ACCENT if winner else theme.MUTED, winner, anchor="rm",
                          alpha=tie, limit=edge - number - 48)
        if tied and tie > 0:
            # One bracket joins the candidates whose N is equal: only these go to the tie-break.
            rows = [cy for _, _, cy in self.score_rows()][:tied]
            x, top, bottom = left - 5, rows[0] - 6, rows[-1] + 6
            self.rule((x, top), (x, bottom), theme.ACCENT, 1.2, tie)
            for y in (top, bottom):
                self.rule((x, y), (x + 3, y), theme.ACCENT, 1.2, tie)

    def tie(self, story):
        shown, y = phase(story, "tie"), self.y["tie"]
        decided = {row["decided_by"] for row in self.data["rows"][:self.data["tied"]]} if self.data["tied"] else set()
        x = 2 + self.text(_tr("N 相等时"), 2, y + 8, SMALL, theme.MUTED, True, limit=self.right * .3)
        for key in TIE_KEYS:
            room = self.right - x - 2
            if room < 40:
                break
            x += self.text(" → ", x, y + 8, SMALL, theme.FAINT)
            lit = shown if key in decided else 0.
            # The keys that decided this ranking light up once the tie is shown.
            self.text(_tr(TIE_STEPS[key]), x, y + 8, SMALL, theme.MUTED, limit=room - 14, alpha=1 - lit)
            x += self.text(_tr(TIE_STEPS[key]), x, y + 8, SMALL, theme.ACCENT, limit=room - 14, alpha=lit) or \
                self.width(_tr(TIE_STEPS[key]), SMALL)


class QuotaLineCanvas:
    """A Tk canvas that shows the view and follows the decision film's clock while it plays."""

    FOLLOW_FPS = 30     # the film draws at 60 fps; this view redraws at most this often while following it

    def __init__(self, parent):
        import tkinter as tk
        self.canvas = tk.Canvas(parent, bg=theme.CARD, highlightthickness=0, bd=0)
        self.data = self.renderer = self.photo = self.shown = None
        self.story = DURATION
        self.item = self.canvas.create_image(0, 0, anchor="nw")
        self.strips = {}    # band name -> (photo, canvas item): a moving band is pasted alone, over the full image
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
        # Only the bands that moved reach Tk: a smaller paste and a smaller area for the canvas to redraw.
        for name in changed:
            top, bottom = self.renderer.bands[name][2:]
            strip = live.crop((0, top, live.width, bottom))
            if name in self.strips:
                photo, item = self.strips[name]
                photo.paste(strip)
                self.canvas.itemconfigure(item, state="normal")
            else:
                photo = ImageTk.PhotoImage(strip, master=self.canvas)
                self.strips[name] = (photo, self.canvas.create_image(0, top, anchor="nw", image=photo))
