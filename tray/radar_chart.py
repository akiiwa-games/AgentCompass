"""The five-axis radar: routing-score dimensions of a decision, or a model's capability profile.

The maths (dimension values, polygon geometry) is plain Python so it is unit-tested without a display;
RadarChartCanvas draws it on a tk.Canvas.
"""
from __future__ import annotations

from i18n import translate as _tr, LocalizedSequence, tr
from i18n import is_en

import math
import time

from PIL import Image, ImageColor, ImageDraw, ImageTk


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
    ("subjective", "主观倾向", "subjective_fit：任务对发散创新 / 全局严密 / 模块精准的倾向与模型主观评分的匹配度（合成权重 65%）"),
    ("capability", "能力满足", "vector_fit：6D 能力向量对任务需求阈值的满足率，min(1, 能力/需求) 的加权平均（20%）"),
    ("route", "路由顺位", "route_fit：work_kind 路由表中该模型的顺位契合度（15%）"),
    ("tier", "档位裕度", "ability − overqualification − effort_penalty：跨过难度底线后的有效能力净值"),
    ("economy", "额度经济", "1 − 5×opportunity_cost + reset_bonus + free_usage + boost：额度充裕、低机会成本与加成"),
)
SCORE_AXES = LocalizedSequence(SCORE_AXES)

DNA_AXES = (
    ("divergence", "发散创新", "subjective.divergence：发散思维、方案创新"),
    ("global_rigor", "全局严密", "subjective.global_rigor：架构落地严密度与全局思考"),
    ("module_precision", "模块精准", "subjective.module_precision：具体模块实现精度"),
    ("reasoning", "推理抗幻觉", "(capabilities.reasoning + capabilities.anti_hallucination) / 2"),
    ("visual", "视觉与 GUI", "max(capabilities.visual, capabilities.gui)"),
)
DNA_AXES = LocalizedSequence(DNA_AXES)


def axis_title(mode: str = "score") -> str:
    return tr("模型基因五维", "5-Axis Model DNA") if mode == "dna" else tr("决策得分五维", "5-Axis Decision Score")


def score_dimensions(components: dict) -> dict:
    """Five 0..1 values from an allocator candidate's `components`."""
    c = components or {}
    fit = c.get("fit", 0)
    return {
        "subjective": clamp(c.get("subjective_fit", fit)),
        "capability": clamp(c.get("vector_fit", fit)),
        "route": clamp(c.get("route_fit", fit)),
        "tier": clamp((c.get("ability") or 0) - (c.get("overqualification") or 0) - (c.get("effort_penalty") or 0)),
        "economy": clamp(1.0 - (c.get("opportunity_cost") or 0) * 5.0 + (c.get("reset_bonus") or 0)
                         + (c.get("free_usage") or 0) + (c.get("boost") or 0)),
    }


def score_raw(components: dict) -> dict:
    """The raw component values each score axis is built from, for the hover text."""
    c = components or {}
    pick = lambda *keys: {k: c[k] for k in keys if k in c}
    return {"subjective": pick("subjective_fit", "fit"), "capability": pick("vector_fit", "fit"),
            "route": pick("route_fit", "fit"), "tier": pick("ability", "overqualification", "effort_penalty"),
            "economy": pick("opportunity_cost", "reset_bonus", "free_usage", "boost")}


def dna_dimensions(info: dict) -> dict:
    """Five 0..1 values from a model's entry in accounts.json `models`."""
    info = info or {}
    subjective = info.get("subjective") or {}
    caps = info.get("capabilities") or {}
    return {
        "divergence": clamp(subjective.get("divergence", 0.5)),
        "global_rigor": clamp(subjective.get("global_rigor", 0.5)),
        "module_precision": clamp(subjective.get("module_precision", 0.5)),
        "reasoning": round(clamp((clamp(caps.get("reasoning")) + clamp(caps.get("anti_hallucination"))) / 2), 2),
        "visual": round(max(clamp(caps.get("visual")), clamp(caps.get("gui"))), 2),
    }


def dna_raw(info: dict) -> dict:
    info = info or {}
    subjective = info.get("subjective") or {}
    caps = info.get("capabilities") or {}
    return {"divergence": {"divergence": subjective.get("divergence")},
            "global_rigor": {"global_rigor": subjective.get("global_rigor")},
            "module_precision": {"module_precision": subjective.get("module_precision")},
            "reasoning": {"reasoning": caps.get("reasoning"), "anti_hallucination": caps.get("anti_hallucination")},
            "visual": {"visual": caps.get("visual"), "gui": caps.get("gui")}}


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


SERIES_STYLES = (
    {"color": "#4F46E5", "dash": ()},
    {"color": "#06B6D4", "dash": (5, 3.5)},
)


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
    """Cache the static grid at 3x physical resolution; rasterize only the plot, never text."""

    supersample = 3

    def __init__(self, radius, count, scale, bg):
        self.radius, self.count, self.scale = radius, count, scale
        padding = math.ceil(8 * scale)
        self.center = radius + padding
        self.size = math.ceil(self.center * 2)
        self.grid = Image.new("RGB", (self.size * self.supersample,) * 2, bg)
        draw = ImageDraw.Draw(self.grid)
        outer = self.points([1] * count)
        draw.polygon(outer, fill="#F8FAFC")
        for level in (.25, .5, .75, 1):
            points = self.points([level] * count)
            draw.line(points + points[:1], fill="#E2E8F0" if level < 1 else "#CBD5E1",
                      width=self.width(.75 if level < 1 else 1), joint="curve")
        center = (self.center * self.supersample,) * 2
        for vertex in outer:
            draw.line([center, vertex], fill="#E2E8F0", width=self.width(.75))

    def width(self, logical):
        # Preserve fractional DIP widths until rasterization, instead of rounding at screen resolution.
        return max(1, round(logical * self.scale * self.supersample))

    def points(self, values):
        return [(x * self.supersample, y * self.supersample)
                for x, y in polygon(values, self.center, self.center, self.radius)]

    def dashed_line(self, draw, points, color, width):
        dash, gap = 5 * self.scale * self.supersample, 3.5 * self.scale * self.supersample
        phase = 0.0
        for start, end in zip(points, points[1:] + points[:1]):
            length = math.dist(start, end)
            position = 0.0
            while position < length:
                visible = phase < dash
                step = min((dash if visible else dash + gap) - phase, length - position)
                if visible:
                    a, b = position / length, (position + step) / length
                    draw.line([(start[0] + (end[0] - start[0]) * t,
                                start[1] + (end[1] - start[1]) * t) for t in (a, b)],
                              fill=color, width=width)
                position += step
                phase = (phase + step) % (dash + gap)

    def render(self, values):
        image = self.grid.copy()
        # Blend alpha directly into the opaque backing. RGB downsampling is much cheaper than RGBA.
        draw = ImageDraw.Draw(image, "RGBA")
        for order in range(len(values) - 1, -1, -1):
            color = ImageColor.getrgb(SERIES_STYLES[order]["color"])
            points = self.points(values[order])
            draw.polygon(points, fill=(*color, 28 if order == 0 else 12))
            if order == 0:
                draw.line(points + points[:1], fill=color, width=self.width(2.2), joint="curve")
            else:
                self.dashed_line(draw, points, color, self.width(1.7))
            for x, y in points:
                r = (3.5 if order == 0 else 3) * self.scale * self.supersample
                if order == 0:
                    halo = 6 * self.scale * self.supersample
                    draw.ellipse((x - halo, y - halo, x + halo, y + halo), fill=(*color, 22))
                draw.ellipse((x - r, y - r, x + r, y + r),
                             fill=color if order == 0 else "#FFFFFF", outline=color,
                             width=self.width(1.5))
        return image.resize((self.size, self.size), Image.Resampling.LANCZOS).convert("RGBA")


class RadarChartCanvas:
    """A five-axis radar on a tk.Canvas: four grid rings, spokes, axis labels with value pills, up to two series."""

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
        self.draw(start if self.canvas.winfo_ismapped() else target)
        if start != target and self.canvas.winfo_ismapped():
            self.animate(start)

    def target_values(self):
        return [[clamp(s["values"].get(key, 0)) for key, _, _ in self.axes] for s in self.series]

    def cancel_animation(self):
        if self._animation_id is not None:
            self.canvas.after_cancel(self._animation_id)
            self._animation_id = None

    def on_destroy(self, event):
        if event.widget is self.canvas:
            self.cancel_animation()
            self._destroyed = True
            self._photo = self._renderer = None

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
        for level in ((.25, .5, .75) if radius >= theme.px(70) else ()):
            x, y = point(cx, cy, radius * level, 0, count)
            c.create_text(x + theme.px(8), y, text=f"{int(level * 100)}", anchor="w",
                          fill=theme.FAINT, font=(theme.FONT, 7))
        self.draw_labels(cx, cy, radius)
        self.draw_legend()

    def draw_series(self) -> None:
        c = self.canvas
        self._photo = ImageTk.PhotoImage(self._renderer.render(self._display_values), master=c)
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
                fill=theme.INK, font=(theme.FONT, 9, "bold"))
            box = c.bbox(text_item)
            if box:
                if box[0] < px(4):
                    c.move(text_item, px(4) - box[0], 0)
                elif box[2] > canvas_w - px(4):
                    c.move(text_item, canvas_w - px(4) - box[2], 0)
            parts = []
            for order, series in enumerate(self.series):
                parts.append((f"{series['values'].get(key, 0) * 100:.0f}", SERIES_STYLES[order]["color"]))
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

    def draw_legend(self) -> None:
        theme = self.theme
        c = self.canvas
        px = theme.px
        cx, cy, radius = self.geometry()
        top_label_y = cy - radius - px(26) - px(17)
        max_x = cx - px(36) if top_label_y < px(32) else max(c.winfo_width(), px(200)) - px(10)
        x, y = px(4), px(12)
        for order, series in enumerate(self.series):
            style = SERIES_STYLES[order]
            if order > 0 and x + px(80) > max_x:
                x, y = px(4), y + px(18)
            c.create_line(x, y, x + px(16), y, fill=style["color"], width=px(2.2 if order == 0 else 1.7),
                          dash=tuple(max(1, px(d)) for d in style["dash"]))
            wrap = max(px(80), int(max_x - x - px(20))) if top_label_y < px(32) else 0
            text = c.create_text(x + px(20), y, text=series["label"], anchor="w", width=wrap,
                                 fill=theme.TEXT, font=(theme.FONT, 8))
            box = c.bbox(text)
            if box and box[3] - box[1] > px(20):
                c.move(text, 0, (box[3] - box[1]) / 2 - px(8))
                box = c.bbox(text)
            x = (box[2] if box else x + px(100)) + px(14)
            if top_label_y < px(32) and box:
                y = max(y, box[3] - px(8))

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
            lines.append(f"{series['label']}{tr('：', ': ')}{series['values'].get(key, 0):.2f}"
                         + (tr("（{0}）", " ({0})").format(detail) if detail else ""))
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
