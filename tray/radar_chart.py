"""One five-axis radar: four capability categories and quota economy, from decisions or model settings.

A category's spoke is its share (axis_scores.five_axis_shares): 0 at the panel's weakest model, 1 at the level-3 target
(the anchor), from the panel standard scores of five-axis-scores.json.

The maths (dimension values, polygon geometry) is plain Python so it is unit-tested without a display;
RadarChartCanvas draws it on a tk.Canvas.
"""
from __future__ import annotations

from i18n import translate as _tr, LocalizedSequence, tr
from i18n import is_en

import math
import time

from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageTk

import page_stack


def clamp(value, low=0.0, high=1.0) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return low
    if math.isnan(value):
        return low
    return max(low, min(high, value))


# (key, label, formula shown on hover)
SCORE_AXES = (
    ("divergence", "创意", "创意：本档位的标准分在面板最弱（0）到 anchor（1）之间的位置"),
    ("coding", "编码能力", "编码能力：本档位的标准分在面板最弱（0）到 anchor（1）之间的位置"),
    ("aesthetics", "审美能力", "审美能力：本档位的标准分在面板最弱（0）到 anchor（1）之间的位置"),
    ("tool_operation", "工具操作", "工具操作：响应速度与读图中较弱者，在面板最弱（0）到三级目标（1）之间的位置"),
    ("economy", "额度经济", "clamp(1 − opportunity_cost + boost)：本次额度成本（额度价格 × 消耗）与临时加成"),
)
SCORE_AXES = LocalizedSequence(SCORE_AXES)

DNA_AXES = (
    ("divergence", "创意", "创意：High 档标准分在面板最弱（0）到 anchor（1）之间的位置；带 ~ 表示含估算值"),
    ("coding", "编码能力", "编码能力：High 档标准分在面板最弱（0）到 anchor（1）之间的位置；带 ~ 表示含估算值"),
    ("aesthetics", "审美能力", "审美能力：High 档标准分在面板最弱（0）到 anchor（1）之间的位置；带 ~ 表示含估算值"),
    ("tool_operation", "工具操作", "工具操作：High 档响应速度与读图中较弱者的位置；带 ~ 表示含估算值"),
    ("economy", "额度经济", "1 − usd_per_hour / 模型表中最高的 usd_per_hour：静态价格经济性，免费为 1，未知为 0；不代表实时剩余额度"),
)
DNA_AXES = LocalizedSequence(DNA_AXES)


def axis_title(mode: str = "score") -> str:
    return tr("能力与额度五维", "Capabilities & Quota")


ABILITY_AXES = ("divergence", "coding", "aesthetics", "tool_operation")


def score_dimensions(components: dict) -> dict:
    """Five 0..1 values from an allocator candidate's `components`: the four category shares and the economy."""
    c = components or {}
    return {
        **{key: clamp(c.get(f"share_{key}") or 0) for key in ABILITY_AXES},
        "economy": clamp(1.0 - (c.get("opportunity_cost") or 0) + (c.get("boost") or 0)),
    }


def score_raw(components: dict, headroom: dict | None = None) -> dict:
    """The raw component values each score axis is built from, for the hover text. A category the task demands at level 3
    also names the score above the anchor the fit counts (capped at the ceiling) and the level-3 cap, the ceiling with
    the model that sets it (`five_axis_headroom`); the spoke itself stops at the anchor."""
    c = components or {}
    out = {**{key: {k: c[k] for k in (key, f"share_{key}") if k in c} for key in ABILITY_AXES},
           "economy": {k: c[k] for k in ("opportunity_cost", "boost") if k in c}}
    for key, h in (headroom or {}).items():
        if key in out and isinstance(h, dict):
            if h.get("above_anchor") is not None:
                out[key]["above_anchor"] = f"{h['above_anchor']:+.3f}"
            if h.get("ceiling") is not None:
                out[key]["ceiling"] = f"{h['ceiling']:+.3f} ({h.get('model') or '—'})"
            if h.get("facet"):
                out[key]["facet"] = h["facet"]
    return out


def estimate_note(estimates: dict, axis: str) -> str:
    """The hover line for a category with estimated facets: `{'category.facet': provenance}` of the model's record."""
    parts = []
    for name, est in sorted((estimates or {}).items()):
        if not name.startswith(axis + "."):
            continue
        how = est.get("method")
        text = name.split(".", 1)[1]
        if how == "sibling_shift":
            text += f": {est.get('base_model')} {est.get('base_value')} + fit shift {est.get('shift', 0):+g} (tier {est.get('tier')} vs {est.get('base_tier')})"
        else:
            text += f": fit on tier {est.get('tier')} (r {est.get('r')}, n {est.get('n')})"
        parts.append(text)
    return "; ".join(parts)


def dna_dimensions(info: dict, ceiling: float | None = None) -> dict:
    """Five 0..1 values from a model row of the router's model table (`five_axis` shares at High and usd_per_hour); the
    economy is 1 - usd_per_hour / `ceiling`, the highest usd_per_hour of the table (0 when the price or the ceiling is
    unknown)."""
    info = info or {}
    shares = (info.get("five_axis") or {}).get("shares") or {}
    price = info.get("usd_per_hour")
    return {
        **{key: clamp(shares.get(key) or 0) for key in ABILITY_AXES},
        "economy": clamp(1 - float(price) / ceiling) if price is not None and ceiling else 0.0,
    }


def dna_raw(info: dict) -> dict:
    info = info or {}
    five = info.get("five_axis") or {}
    tier = (five.get("tier") or {}).get("value")
    return {**{key: {key: (five.get("scores") or {}).get(key), f"share_{key}": (five.get("shares") or {}).get(key),
                     **({"tier": tier} if tier is not None else {})}
               for key in ABILITY_AXES},
            "economy": {"usd_per_hour": info.get("usd_per_hour")}}


def dna_estimated(info: dict) -> dict:
    """{category: provenance text} for the categories that contain an estimated facet (sibling or tier fit)."""
    five = (info or {}).get("five_axis") or {}
    estimates = five.get("estimates") or {}
    return {axis: estimate_note(estimates, axis) for axis in ABILITY_AXES if any(k.startswith(axis + ".") for k in estimates)}


def axis_angle(index: int, count: int) -> float:
    """Radians; the first axis points straight up, the rest follow clockwise."""
    return -math.pi / 2 + 2 * math.pi * index / count


def point(cx: float, cy: float, radius: float, index: int, count: int) -> tuple[float, float]:
    angle = axis_angle(index, count)
    return cx + radius * math.cos(angle), cy + radius * math.sin(angle)


def ring(cx: float, cy: float, radius: float, count: int = 5) -> list[tuple[float, float]]:
    return [point(cx, cy, radius, i, count) for i in range(count)]


def polygon(values: list[float], cx: float, cy: float, radius: float) -> list[tuple[float, float]]:
    """Vertices of a value polygon; each value is clamped to 0..1 of the radius."""
    count = len(values)
    return [point(cx, cy, radius * clamp(v), i, count) for i, v in enumerate(values)]


# The winner (or first pick) and the model it is compared with: one hue each, validated as a pair for
# colour-vision separation and 3:1 contrast on the card. Line weight and marker size repeat the distinction.
# A shape is a sheet of tinted glass and says nothing beyond its five values: its hue runs from `top` to
# `bottom` at the opacity `body`, denser (`rim`) in a band just inside the edge. `dot` is a vertex marker's
# surface ring and its core, in px.
SERIES_STYLES = (
    {"color": "#4F46E5", "dash": (), "weight": 2.2, "top": "#C7D2FE", "bottom": "#4338CA", "body": 62, "rim": 118,
     "dot": (4.4, 3.0)},
    {"color": "#0891B2", "dash": (), "weight": 1.5, "top": "#CFFAFE", "bottom": "#0E7490", "body": 44, "rim": 92,
     "dot": (3.6, 2.4)},
)
FRAME_INK = "#E2E8F0"         # the hairline pentagon of full scale: the only thing that stays put
RIM = 3.5                     # px: the denser band inside a sheet's edge
LIGHT = (-.6, -.8)            # the light comes from the upper left
SHINE, SHINE_INSET = 210, 4.6  # the white hairline inside the edges that face the light: strength, px from the edge
GLINTS = 14                   # specks of light in the first sheet


def noise(n: float) -> float:
    """A fixed pseudo-random share 0..1 for the n-th speck: the same picture every time."""
    value = math.sin(n * 127.1 + 311.7) * 43758.5453
    return value - math.floor(value)


def glass_sheet(size, style):
    """A sheet's colour before it is cut to a shape: its hue from light at the top to deep at the bottom."""
    size = max(1, int(size))
    ramp = Image.linear_gradient("L").resize((size, size), Image.Resampling.BILINEAR)
    top, bottom = ImageColor.getrgb(style["top"]), ImageColor.getrgb(style["bottom"])
    return Image.merge("RGB", [ramp.point([round(top[i] + (bottom[i] - top[i]) * v / 255) for v in range(256)])
                               for i in range(3)])


def glass(image, points, sheet, origin, style, unit, strength=1.):
    """Lay `sheet`, cut to the polygon `points`, on an RGB image; `origin` is where the sheet's corner lies.

    Only the polygon's bounding box is touched. `unit` is image pixels per px; `strength` fades the sheet in.
    """
    if strength <= 0 or len(points) < 3:
        return
    ox, oy = round(origin[0]), round(origin[1])
    x0 = max(0, ox, math.floor(min(x for x, _ in points)) - 1)
    y0 = max(0, oy, math.floor(min(y for _, y in points)) - 1)
    x1 = min(image.width, ox + sheet.width, math.ceil(max(x for x, _ in points)) + 2)
    y1 = min(image.height, oy + sheet.height, math.ceil(max(y for _, y in points)) + 2)
    if x1 <= x0 or y1 <= y0:
        return
    local = [(x - x0, y - y0) for x, y in points]
    inside = Image.new("L", (x1 - x0, y1 - y0), 0)
    ImageDraw.Draw(inside).polygon(local, fill=255)
    body = Image.new("L", inside.size, round(style["body"] * strength))
    ImageDraw.Draw(body).line(local + local[:2], fill=round(style["rim"] * strength),
                              width=max(1, round(2 * RIM * unit)), joint="curve")
    image.paste(sheet.crop((x0 - ox, y0 - oy, x1 - ox, y1 - oy)), (x0, y0), ImageChops.darker(inside, body))


def centroid(points):
    return sum(x for x, _ in points) / len(points), sum(y for _, y in points) / len(points)


def highlights(points, unit):
    """[(a, b, strength 0..1)]: the edges of a clockwise polygon that face the light, moved just inside the rim."""
    cx, cy = centroid(points)
    by = SHINE_INSET * unit
    if min(math.hypot(x - cx, y - cy) for x, y in points) < 3 * by:
        return []                                  # too small a shape to have an inside
    inner = []
    for x, y in points:
        reach = math.hypot(x - cx, y - cy)
        inner.append((x - (x - cx) / reach * by, y - (y - cy) / reach * by))
    out = []
    for a, b in zip(inner, inner[1:] + inner[:1]):
        nx, ny = b[1] - a[1], a[0] - b[0]          # the outward normal of a clockwise edge on a screen
        length = math.hypot(nx, ny)
        lit = (nx * LIGHT[0] + ny * LIGHT[1]) / length if length else 0.
        if lit > .05:
            out.append((a, b, lit))
    return out


def glints(points, centre, count=GLINTS):
    """[(x, y, radius in px, brightness 0..1)]: specks of light that ride a shape.

    Each sits on the line from `centre` to a fixed point of the outline, so the specks move with the shape.
    """
    cx, cy = centre
    out = []
    for n in range(count):
        edge = int(noise(n) * len(points)) % len(points)
        (ax, ay), (bx, by) = points[edge], points[(edge + 1) % len(points)]
        along, inward = noise(n + 40), .25 + .7 * math.sqrt(noise(n + 80))
        x, y = ax + (bx - ax) * along, ay + (by - ay) * along
        out.append((cx + (x - cx) * inward, cy + (y - cy) * inward, .55 + .9 * noise(n + 120) ** 2,
                    .47 + .47 * noise(n + 160)))
    return out


def ease_out(progress: float) -> float:
    """Cubic ease-out with exact endpoints and no overshoot."""
    return 1 - (1 - clamp(progress)) ** 3


def interpolate_values(start, target, progress):
    if progress >= 1:
        return [list(row) for row in target]
    amount = ease_out(progress)
    return [[a + (b - a) * amount for a, b in zip(old, new)]
            for old, new in zip(start, target)]


class RadarRenderer:
    """Cache the static scaffold and the sheets' colour at 3x physical resolution; rasterize only the plot, never text.

    The only thing that stays put is a hairline pentagon at full scale. A model is a sheet of tinted glass
    cut to its five values (`glass`), with a crisp outline and a marker on each vertex; the first sheet
    carries a few specks of light. Nothing is blurred, and nothing is said beyond the two shapes.
    """

    supersample = 3

    def __init__(self, radius, count, scale, bg):
        self.radius, self.count, self.scale = radius, count, scale
        padding = math.ceil(8 * scale)
        self.center = radius + padding
        self.size = math.ceil(self.center * 2)
        self.grid = Image.new("RGB", (self.size * self.supersample,) * 2, bg)
        frame = self.points([1] * count)
        ImageDraw.Draw(self.grid).line(frame + frame[:2], fill=FRAME_INK, width=self.width(1.), joint="curve")
        self.sheets = [glass_sheet(self.size * self.supersample, style) for style in SERIES_STYLES]

    def width(self, logical):
        # Preserve fractional DIP widths until rasterization, instead of rounding at screen resolution.
        return max(1, round(logical * self.scale * self.supersample))

    def points(self, values):
        return [(x * self.supersample, y * self.supersample)
                for x, y in polygon(values, self.center, self.center, self.radius)]

    def render(self, values, hollow=()):
        """values: one list per series, primary first; `hollow`: per series the axis indexes whose value is estimated."""
        image = self.grid.copy()
        unit = self.scale * self.supersample
        centre = (self.center * self.supersample,) * 2
        orders = range(len(values) - 1, -1, -1)       # the other model first, the first pick on top
        shapes = [self.points(row) for row in values]
        for order in orders:
            glass(image, shapes[order], self.sheets[order], (0, 0), SERIES_STYLES[order], unit)
        # Blend alpha directly into the opaque backing. RGB downsampling is much cheaper than RGBA.
        draw = ImageDraw.Draw(image, "RGBA")
        for order in orders:
            for a, b, lit in highlights(shapes[order], unit):
                draw.line((a, b), fill=(255, 255, 255, round(SHINE * lit)), width=self.width(1.1))
        if shapes:
            for x, y, size, light in glints(shapes[0], centre):
                r = size * unit
                draw.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255, round(255 * light)))
        for order in orders:
            style = SERIES_STYLES[order]
            color = ImageColor.getrgb(style["color"])
            points = shapes[order]
            closed = points + points[:2]      # past the seam, so the first corner is joined like the others
            draw.line(closed, fill=color, width=max(1, round(style["weight"] * unit)), joint="curve")
            for index, (x, y) in enumerate(points):
                estimated = order < len(hollow) and index in hollow[order]
                for size, fill in zip(style["dot"], ("#FFFFFF", color)):
                    r = size * unit
                    if estimated and fill == color:          # an estimated value is a ring, not a solid dot
                        draw.ellipse((x - r, y - r, x + r, y + r), fill="#FFFFFF", outline=color, width=max(1, round(unit * 1.2)))
                    else:
                        draw.ellipse((x - r, y - r, x + r, y + r), fill=fill)
        return image.reduce(self.supersample).convert("RGBA")


class RadarChartCanvas:
    """A five-axis radar on a tk.Canvas: a hairline frame, axis labels with value pills, up to two series."""

    def __init__(self, parent, size: int = 420, bg: str = "#FFFFFF"):
        import tkinter as tk

        import ui_theme

        self.theme = ui_theme
        self.canvas = tk.Canvas(parent, width=size, height=size, bg=bg, highlightthickness=0, bd=0)
        self.bg = bg
        self.axes = ()
        self.series = []
        self.raw = []
        self.hover = None
        self._last_size = None
        self._animation_id = None
        self._display_values = []
        self._renderer = None
        self._photo = None
        self._destroyed = False
        self.canvas.bind("<Configure>", self.on_configure)
        self.canvas.bind("<Map>", self.on_map)
        self.canvas.bind("<Unmap>", lambda event: self.cancel_animation())
        page_stack.watch(self)  # a covered page stays mapped: page_shown / page_hidden mark the switch
        self.canvas.bind("<Destroy>", self.on_destroy)
        self.canvas.bind("<Motion>", self.on_motion)
        self.canvas.bind("<Leave>", lambda event: self.hide_tip())
        self.canvas.bind("<Button-1>", self.on_motion)

    def on_configure(self, event) -> None:
        # Re-mapping a page or a sibling's layout pass sends <Configure> without a size change; redraw only on resize.
        if event.width <= 1 or event.height <= 1:
            return
        size = (event.width, event.height)
        if size != self._last_size:
            self._last_size = size
            self.draw()

    def pack(self, **kw):
        self.canvas.pack(**kw)

    def grid(self, **kw):
        self.canvas.grid(**kw)

    def show(self, axes, series: list[dict], raw: list[dict] | None = None) -> None:
        """series: [{'label', 'values': {axis key: 0..1}}], primary first."""
        previous_axes = [axis[0] for axis in self.axes]
        previous = self._display_values
        self.cancel_animation()
        self.axes = tuple(axes)
        self.series = [{**s, "values": dict(s["values"])} for s in series[:2]]
        self.raw = list(raw or [])[:2]
        target = self.target_values()
        start = [previous[i] if previous_axes == [axis[0] for axis in self.axes] and i < len(previous)
                 else [0] * len(self.axes) for i in range(len(target))]
        visible = page_stack.shown(self.canvas)
        self.draw(start if visible else target)
        if start != target and visible:
            self.animate(start)

    def target_values(self):
        return [[clamp(s["values"].get(key, 0)) for key, _, _ in self.axes] for s in self.series]

    def hollow(self):
        """Per series the indexes of the axes whose value is estimated (series key `estimated`: {axis key: provenance})."""
        return [{i for i, (key, _, _) in enumerate(self.axes) if key in (s.get("estimated") or {})} for s in self.series]

    def cancel_animation(self):
        if self._animation_id is not None:
            self.canvas.after_cancel(self._animation_id)
            self._animation_id = None

    def on_destroy(self, event):
        if event.widget is self.canvas:
            self.cancel_animation()
            self._destroyed = True
            self._photo = self._renderer = None

    def page_shown(self):
        self.on_map(None)

    def page_hidden(self):
        self.cancel_animation()

    def on_map(self, _event):
        if len(self.axes) >= 3 and self.series:
            self.cancel_animation()
            self.animate([[0] * len(self.axes) for _ in self.series])

    def animate(self, start):
        if len(self.axes) < 3 or not self.series or self._destroyed:
            return
        target = self.target_values()
        began = time.perf_counter()

        def frame():
            self._animation_id = None
            if self._destroyed:
                return
            frame_start = time.perf_counter()
            progress = min(1, (frame_start - began) / .320)
            self._display_values = interpolate_values(start, target, progress)
            self.draw_series()
            if progress < 1:
                delay = max(1, 16 - round((time.perf_counter() - frame_start) * 1000))
                self._animation_id = self.canvas.after(delay, frame)

        frame()

    def geometry(self):
        px = self.theme.px
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        width = w if w > 1 else max(int(self.canvas["width"]), px(180))
        height = h if h > 1 else max(int(self.canvas["height"]), px(180))
        radius = min(width / 2 - px(88), (height - px(102)) / 1.809)
        radius = max(radius, px(36))
        cy = min(height / 2 + 0.095 * radius, height - px(49) - 0.809 * radius)
        return width / 2, cy, radius

    def draw(self, values=None) -> None:
        self.cancel_animation()
        self._display_values = self.target_values() if values is None else values
        theme = self.theme
        c = self.canvas
        c.delete("all")
        self.hide_tip()
        count = len(self.axes)
        if count < 3:
            self._photo = None
            c.create_text(c.winfo_width() / 2, c.winfo_height() / 2, text=_tr("选择一次决策或两个模型以查看五维图"),
                          fill=theme.FAINT, font=(theme.FONT, 10))
            return
        cx, cy, radius = self.geometry()
        key = (radius, count, theme.FACTOR, self.bg)
        if self._renderer is None or key != self._renderer_key:
            self._renderer = RadarRenderer(*key)
            self._renderer_key = key
        self._image_item = c.create_image(cx - self._renderer.center, cy - self._renderer.center, anchor="nw")
        for order in range(len(self.series)):
            for index in range(count):
                c.create_oval(0, 0, 0, 0, fill="", outline="", tags=(f"vertex-{order}-{index}",))
        self.draw_series()
        self.draw_labels(cx, cy, radius)
        self.draw_legend()

    def draw_series(self) -> None:
        c = self.canvas
        self._photo = ImageTk.PhotoImage(self._renderer.render(self._display_values, self.hollow()), master=c)
        c.itemconfigure(self._image_item, image=self._photo)
        cx, cy, radius = self.geometry()
        for order, values in enumerate(self._display_values):
            for index, (x, y) in enumerate(polygon(values, cx, cy, radius)):
                r = self.theme.px(6)
                c.coords(f"vertex-{order}-{index}", x - r, y - r, x + r, y + r)

    def draw_labels(self, cx: float, cy: float, radius: float) -> None:
        theme = self.theme
        c = self.canvas
        count = len(self.axes)
        px = theme.px
        canvas_w = c.winfo_width() if c.winfo_width() > 1 else max(int(c["width"]), px(180))
        for index, (key, label, _) in enumerate(self.axes):
            x, y = point(cx, cy, radius + px(26), index, count)
            anchor = "center"
            if x < cx - 10:
                anchor = "e"
            elif x > cx + 10:
                anchor = "w"
            if is_en():
                if y > cy + radius * 0.4:
                    x += -px(22) if x < cx else px(22)
                elif abs(x - cx) > 10:
                    x += -px(12) if x < cx else px(12)
                x = max(px(58), min(canvas_w - px(58), x))
            text_anchor = "s" if is_en() or anchor == "center" else ("se" if anchor == "e" else "sw")
            text_item = c.create_text(
                x, y, text=label,
                anchor=text_anchor, justify="center" if is_en() else ("right" if anchor == "e" else "left"),
                width=px(108) if is_en() else 0,
                fill=theme.INK, font=(theme.FONT, 9, "bold"), tags=("axis-label",))
            box = c.bbox(text_item)
            if box:
                if box[0] < px(4):
                    c.move(text_item, px(4) - box[0], 0)
                elif box[2] > canvas_w - px(4):
                    c.move(text_item, canvas_w - px(4) - box[2], 0)
            parts = []
            for order, series in enumerate(self.series):
                mark = "~" if key in (series.get("estimated") or {}) else ""
                parts.append((f"{mark}{series['values'].get(key, 0) * 100:.0f}", SERIES_STYLES[order]["color"]))
            offset = 0
            widths = [px(len(text) * 8 + 14) for text, _ in parts]
            total = sum(widths) + px(4) * (len(parts) - 1)
            pill_anchor = "center" if is_en() else anchor
            start = x - total / 2 if pill_anchor == "center" else x - total if pill_anchor == "e" else x
            start = max(px(4), min(canvas_w - total - px(4), start))
            for (text, color), width in zip(parts, widths):
                left = start + offset
                c.create_rectangle(left, y + px(2), left + width, y + px(20), fill=color, outline=color)
                c.create_text(left + width / 2, y + px(11), text=text, fill="#FFFFFF", font=(theme.FONT, 8, "bold"))
                offset += width + px(4)

    def draw_legend(self, compact: bool = False) -> None:
        theme = self.theme
        c = self.canvas
        px = theme.px
        cx, cy, radius = self.geometry()
        top_label_y = cy - radius - px(26) - px(17)
        max_x = cx - px(36) if top_label_y < px(32) else max(c.winfo_width(), px(200)) - px(10)
        x, y = px(4), px(12)
        for order, series in enumerate(self.series):
            style = SERIES_STYLES[order]
            if order > 0 and (compact or x + px(80) > max_x):
                x, y = px(4), y + px(16 if compact else 18)
            c.create_line(x, y, x + px(16), y, fill=style["color"], width=px(2.2 if order == 0 else 1.7),
                          dash=tuple(max(1, px(d)) for d in style["dash"]), tags=("legend",))
            wrap = max(px(80), int(max_x - x - px(20))) if top_label_y < px(32) and not compact else 0
            label = self.shortened(series["label"], max_x - x - px(20)) if compact else series["label"]
            text = c.create_text(x + px(20), y, text=label, anchor="w", width=wrap,
                                 fill=theme.TEXT, font=(theme.FONT, 8), tags=("legend",))
            box = c.bbox(text)
            if box and box[3] - box[1] > px(20):
                c.move(text, 0, (box[3] - box[1]) / 2 - px(8))
                box = c.bbox(text)
            x = (box[2] if box else x + px(100)) + px(14)
            if top_label_y < px(32) and box:
                y = max(y, box[3] - px(8))
        if not compact and self.legend_collides():
            # A wrapped legend would run into an axis name on a small canvas: one shortened line per series instead.
            c.delete("legend")
            self.draw_legend(compact=True)

    def legend_collides(self) -> bool:
        c = self.canvas
        legend = c.bbox("legend")
        if not legend:
            return False
        for item in c.find_withtag("axis-label"):
            box = c.bbox(item)
            if box and box[0] < legend[2] and legend[0] < box[2] and box[1] < legend[3] and legend[1] < box[3]:
                return True
        return False

    def shortened(self, text: str, room: float) -> str:
        """`text` cut to fit `room` pixels of the legend's font, with an ellipsis."""
        import tkinter.font as tkfont
        font = tkfont.Font(root=self.canvas, font=(self.theme.FONT, 8))
        if font.measure(text) <= room:
            return text
        while text and font.measure(text.rstrip(" ·") + "…") > room:
            text = text[:-1]
        return text.rstrip(" ·") + "…"

    def nearest_axis(self, x: float, y: float):
        if not self.axes:
            return None
        cx, cy, radius = self.geometry()
        best, distance = None, float(self.theme.px(30))
        for order, values in enumerate(self._display_values):
            for index, (px, py) in enumerate(polygon(values, cx, cy, radius)):
                d = math.hypot(px - x, py - y)
                if d < distance:
                    best, distance = (order, index), d
        for index in range(len(self.axes)):
            ax, ay = point(cx, cy, radius + self.theme.px(26), index, len(self.axes))
            d = math.hypot(ax - x, ay - y)
            if d < distance:
                best, distance = (0, index), d
        return best

    def on_motion(self, event) -> None:
        hit = self.nearest_axis(event.x, event.y)
        if hit is None:
            self.hide_tip()
            return
        if hit == self.hover:
            return
        self.hover = hit
        _, index = hit
        key, label, formula = self.axes[index]
        lines = [f"{label}", formula]
        for order, series in enumerate(self.series):
            raw = self.raw[order].get(key) if order < len(self.raw) else None
            detail = tr("，", ", ").join(f"{k}={v}" for k, v in (raw or {}).items() if v is not None)
            estimated = (series.get("estimated") or {}).get(key)
            lines.append(f"{series['label']}{tr('：', ': ')}{'~' if estimated is not None else ''}{series['values'].get(key, 0):.2f}"
                         + (tr("（{0}）", " ({0})").format(detail) if detail else ""))
            if estimated is not None:
                lines.append(tr("~ 含按档位估算的类型分：", "~ includes type scores estimated through the tier: ") + estimated)
        self.show_tip(event.x, event.y, "\n".join(lines))

    def show_tip(self, x: float, y: float, text: str) -> None:
        theme = self.theme
        c = self.canvas
        c.delete("tip")
        width = c.winfo_width()
        label = c.create_text(0, 0, text=text, anchor="nw", fill="#FFFFFF", font=(theme.FONT, 9), width=theme.px(320),
                              tags="tip")
        box = c.bbox(label)
        w, h = box[2] - box[0], box[3] - box[1]
        left = min(max(8, x + 14), width - w - 18)
        top = max(8, y - h - 18)
        c.move(label, left + 8, top + 6)
        c.create_rectangle(left, top, left + w + 16, top + h + 12, fill=theme.INK, outline=theme.INK, tags="tip")
        c.tag_raise(label)

    def hide_tip(self) -> None:
        self.hover = None
        self.canvas.delete("tip")
