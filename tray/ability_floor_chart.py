"""Polished, high-DPI ability-floor chart with draggable difficulty & route thresholds.

Uses 2x supersampled PIL rendering with LANCZOS downsampling (matching radar_chart.py and
decision_theatre.py) for crisp anti-aliased lines, markers, shaded difficulty bands, and
grabbable floor handles in both light and dark themes.
"""
from __future__ import annotations

import math
from typing import Callable

from PIL import Image, ImageDraw, ImageFont

from i18n import translate as _tr
import display_names
import ui_theme as theme


PALETTE_LIGHT = {
    "surface": (255, 255, 255, 255),
    "plot_bg": (249, 250, 252, 255),
    "row_alt": (242, 245, 250, 180),
    "row_hover": (224, 234, 252, 190),
    "row_gained": (209, 250, 229, 180),
    "row_lost": (254, 226, 226, 180),
    "header_bg": (244, 247, 251, 255),
    "border": (218, 226, 236, 255),
    "grid_major": (220, 228, 238, 255),
    "grid_minor": (235, 240, 246, 255),
    "text": (15, 23, 42, 255),
    "text_mid": (71, 85, 105, 255),
    "text_muted": (148, 163, 184, 255),
    "band_below": (241, 245, 249, 130),
    "band_routine": (236, 253, 245, 155),
    "band_hard": (239, 246, 255, 165),
    "band_coupled": (245, 243, 255, 165),
    "band_frontier": (255, 251, 235, 175),
    "floor_routine": (13, 148, 136, 255),     # Teal 600
    "floor_hard": (37, 99, 235, 255),         # Blue 600
    "floor_coupled": (124, 58, 237, 255),     # Violet 600
    "floor_frontier": (217, 119, 6, 255),     # Amber 600
    "floor_route": (2, 132, 199, 235),        # Sky 600
    "floor_critical": (225, 29, 72, 210),     # Rose 600
    "handle_bg": (255, 255, 255, 250),
    "handle_shadow": (15, 23, 42, 24),
    "tooltip_bg": (255, 255, 255, 248),
    "tooltip_border": (203, 213, 225, 255),
    "tooltip_shadow": (15, 23, 42, 38),
    "est_badge_bg": (254, 243, 199, 255),
    "est_badge_fg": (180, 83, 9, 255),
    "off_badge_bg": (241, 245, 249, 255),
    "off_badge_fg": (100, 116, 139, 255),
    "gain_fg": (5, 150, 105, 255),
    "loss_fg": (225, 29, 72, 255),
    "family_claude": (217, 107, 67, 255),
    "family_codex": (37, 99, 235, 255),
    "family_antigravity": (124, 58, 237, 255),
    "family_opencode": (13, 148, 136, 255),
    "family_dot": (5, 150, 105, 255),
    "family_chatgpt": (16, 163, 127, 255),
    "family_grok": (71, 85, 105, 255),
    "family_default": (59, 130, 246, 255),
}

PALETTE_DARK = {
    "surface": (15, 23, 42, 255),
    "plot_bg": (19, 28, 49, 255),
    "row_alt": (25, 36, 62, 185),
    "row_hover": (37, 56, 94, 215),
    "row_gained": (6, 78, 59, 150),
    "row_lost": (127, 29, 29, 150),
    "header_bg": (17, 25, 44, 255),
    "border": (42, 58, 90, 255),
    "grid_major": (38, 53, 84, 255),
    "grid_minor": (27, 39, 64, 255),
    "text": (241, 245, 249, 255),
    "text_mid": (160, 174, 198, 255),
    "text_muted": (100, 116, 139, 255),
    "band_below": (15, 23, 42, 120),
    "band_routine": (13, 148, 136, 30),
    "band_hard": (59, 130, 246, 34),
    "band_coupled": (139, 92, 246, 36),
    "band_frontier": (245, 158, 11, 38),
    "floor_routine": (45, 212, 191, 255),     # Teal 400
    "floor_hard": (96, 165, 250, 255),        # Blue 400
    "floor_coupled": (167, 139, 250, 255),    # Violet 400
    "floor_frontier": (251, 191, 36, 255),    # Amber 400
    "floor_route": (56, 189, 248, 235),       # Sky 400
    "floor_critical": (251, 113, 133, 215),   # Rose 400
    "handle_bg": (28, 40, 68, 250),
    "handle_shadow": (0, 0, 0, 85),
    "tooltip_bg": (23, 33, 56, 250),
    "tooltip_border": (59, 79, 118, 255),
    "tooltip_shadow": (0, 0, 0, 110),
    "est_badge_bg": (78, 53, 16, 240),
    "est_badge_fg": (251, 191, 36, 255),
    "off_badge_bg": (30, 41, 59, 240),
    "off_badge_fg": (148, 163, 184, 255),
    "gain_fg": (52, 211, 153, 255),
    "loss_fg": (251, 113, 133, 255),
    "family_claude": (244, 138, 98, 255),
    "family_codex": (96, 165, 250, 255),
    "family_antigravity": (167, 139, 250, 255),
    "family_opencode": (45, 212, 191, 255),
    "family_dot": (52, 211, 153, 255),
    "family_chatgpt": (52, 211, 153, 255),
    "family_grok": (148, 163, 184, 255),
    "family_default": (96, 165, 250, 255),
}

DIFFICULTY_ORDER = ("routine", "hard", "coupled", "frontier")

# Layout in 96-dpi pixels (scaled by dpi_scale). Text sizes follow the panel's type ramp: row labels match the 9pt
# table text, annotations the 8pt hint text, the column title the 10pt bold card heading.
TOP_HEADER_H = 108
BAND_STRIP_TOP = 84
CRIT_FOOTER_H = 28
BOTTOM_PAD = 52
ROW_H_DENSE = 30.0  # more than 18 models
ROW_H = 32.0
LEFT_COL_MIN = 228
LEFT_COL_MAX = 340
FONT_PX = {"title": 13.5, "sub": 11.5, "row": 12.0, "acct": 10.5, "mono": 11.5, "mono_sm": 11.0,
           "tiny": 10.5, "pill": 11.5, "tip_head": 12.5, "tip_body": 11.0}
DIFFICULTY_SHORT = {
    "routine": "常规",
    "hard": "较难",
    "coupled": "耦合",
    "frontier": "前沿",
}
EFFORT_SHORT = {
    "minimal": "Min",
    "low": "L",
    "medium": "M",
    "standard": "Std",
    "default": "H",
    "high": "H",
    "extended": "Ext",
    "xhigh": "XH",
    "max": "Max",
    "ultra": "Ult",
}

_FONT_CACHE: dict[tuple[str, int], ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}


def _load_font(kind: str, size: int):
    key = (kind, size)
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached
    if kind == "bold":
        candidates = ("msyhbd.ttc", "msyh.ttc", "segoeuib.ttf", "arialbd.ttf")
    else:
        # Use Microsoft YaHei first even for numeric/mixed labels so CJK + numbers render with zero missing-glyph boxes.
        candidates = ("msyh.ttc", "segoeui.ttf", "consola.ttf", "arial.ttf")
    font = None
    for name in candidates:
        try:
            font = ImageFont.truetype(name, size)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


def _with_alpha(rgba: tuple[int, int, int, int], alpha: int) -> tuple[int, int, int, int]:
    return (rgba[0], rgba[1], rgba[2], max(0, min(255, int(alpha))))


def _family_color(account: str, pal: dict) -> tuple[int, int, int, int]:
    acc = str(account or "").lower()
    if acc.startswith("claude"):
        return pal["family_claude"]
    if acc.startswith("codex") or acc == "pi":
        return pal["family_codex"]
    if acc.startswith("antigravity"):
        return pal["family_antigravity"]
    if acc.startswith("opencode"):
        return pal["family_opencode"]
    if acc.startswith("dot"):
        return pal["family_dot"]
    if acc.startswith("chatgpt"):
        return pal["family_chatgpt"]
    if acc.startswith("grok"):
        return pal["family_grok"]
    return pal["family_default"]


def _draw_dashed_line(draw: ImageDraw.ImageDraw, p1: tuple[float, float], p2: tuple[float, float],
                      fill: tuple[int, int, int, int], width: int = 2, dash: int = 8, gap: int = 5) -> None:
    x1, y1 = p1
    x2, y2 = p2
    dx, dy = x2 - x1, y2 - y1
    dist = math.hypot(dx, dy)
    if dist < 1e-3:
        return
    ux, uy = dx / dist, dy / dist
    pos = 0.0
    step = dash + gap
    while pos < dist:
        end = min(dist, pos + dash)
        draw.line(
            [(x1 + ux * pos, y1 + uy * pos), (x1 + ux * end, y1 + uy * end)],
            fill=fill,
            width=width,
        )
        pos += step


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_w: float, max_lines: int = 3) -> list[str]:
    import re
    text = " ".join(str(text or "").split())
    if not text:
        return []
    tokens = re.findall(r"[A-Za-z0-9_./%+-]+|\s+|.", text)
    lines: list[str] = []
    current = ""
    for tok in tokens:
        if not current and tok.isspace():
            continue
        trial = current + tok
        bbox = draw.textbbox((0, 0), trial, font=font)
        if bbox[2] - bbox[0] <= max_w or not current:
            current = trial
        else:
            lines.append(current.rstrip())
            current = "" if tok.isspace() else tok
            if len(lines) >= max_lines:
                break
    if len(lines) < max_lines and current.strip():
        lines.append(current.rstrip())
    elif len(lines) >= max_lines and current.strip():
        last = lines[-1]
        if len(last) > 2:
            lines[-1] = last[:-2].rstrip() + "…"
    return lines


def _fit_text(draw: ImageDraw.ImageDraw, text: str, font, max_w: float) -> str:
    """text, or its longest prefix plus an ellipsis that fits max_w (the hover card still shows it in full)."""
    if max_w <= 0 or draw.textlength(text, font=font) <= max_w:
        return text
    while len(text) > 1 and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text.rstrip() + "…"


class AbilityFloorRenderer:
    """High-DPI PIL renderer for the ability-floor chart."""

    MAIN_MIN = 2.0
    MAIN_MAX = 5.48
    OUTLIER_MIN = 0.6

    def __init__(self, width: int = 880, height: int | None = None, theme_mode: str = "light",
                 dpi_scale: float | None = None, supersample: int = 2):
        self.theme_mode = "dark" if str(theme_mode).lower() == "dark" else "light"
        self.pal = PALETTE_DARK if self.theme_mode == "dark" else PALETTE_LIGHT
        self.dpi_scale = float(dpi_scale if dpi_scale is not None else getattr(theme, "FACTOR", 1.0))
        self.supersample = max(1, int(supersample))
        self.width = max(560, int(width))
        self.height = int(height) if height is not None else 460
        self.handle_hitboxes: list[dict] = []
        self.row_hitboxes: list[dict] = []
        self._layout: dict = {}

    def _row_height_px(self, model_count: int) -> int:
        s = self.dpi_scale
        unit = ROW_H_DENSE if model_count > 18 else ROW_H
        return int(round(unit * s))

    def _left_column_width(self, draw, models, k, left_margin, f_acct, f_row, f_tiny, f_title) -> int:
        """Wide enough for the longest account pill + model name + badges, within LEFT_COL_MIN..MAX."""
        def badge(text):
            return draw.textlength(text, font=f_tiny) + 12 * k

        need = left_margin + 4 * k + draw.textlength(_tr("模型与账号档位"), font=f_title)
        for m in models:
            acct = str(m.get("account_label") or m.get("account") or "")
            label = str(m.get("model_label") or m.get("model") or "")
            width = (left_margin + 4 * k + draw.textlength(acct, font=f_acct) + 19 * k
                     + draw.textlength(label, font=f_row) + 6 * k)
            if m.get("fixed_effort"):
                fixed = str(m["fixed_effort"]).lower()
                width += badge(f"{_tr('固定')}·{EFFORT_SHORT.get(fixed, fixed)}")
            if m.get("estimated"):
                width += badge(_tr("估"))
            if not m.get("enabled", True):
                width += badge(_tr("未选"))
            need = max(need, width + 10 * k)
        return int(round(max(LEFT_COL_MIN * k, min(LEFT_COL_MAX * k, need))))

    def compute_height_for_models(self, model_count: int) -> int:
        s = self.dpi_scale
        top_pad = int(round(TOP_HEADER_H * s))
        crit_footer_pad = int(round(CRIT_FOOTER_H * s))
        bottom_pad = int(round(BOTTOM_PAD * s))
        row_h = self._row_height_px(model_count)
        rows = max(6, int(model_count))
        return top_pad + rows * row_h + crit_footer_pad + bottom_pad

    def value_to_x(self, val: float, plot_left: float, plot_right: float, has_low_outlier: bool = True) -> float:
        v = float(val)
        if not has_low_outlier:
            span = self.MAIN_MAX - self.MAIN_MIN
            t = (v - self.MAIN_MIN) / span
            return plot_left + max(0.0, min(1.0, t)) * (plot_right - plot_left)
        gutter_w = max(54.0 * self.dpi_scale, (plot_right - plot_left) * 0.095)
        main_left = plot_left + gutter_w
        if v < self.MAIN_MIN:
            t = (max(self.OUTLIER_MIN, v) - self.OUTLIER_MIN) / (self.MAIN_MIN - self.OUTLIER_MIN)
            return plot_left + 12.0 * self.dpi_scale + t * (gutter_w - 22.0 * self.dpi_scale)
        span = self.MAIN_MAX - self.MAIN_MIN
        t = (v - self.MAIN_MIN) / span
        return main_left + max(-0.02, min(1.02, t)) * (plot_right - main_left)

    def x_to_value(self, x: float) -> float:
        if not self._layout:
            return 3.0
        plot_left = self._layout["plot_left"]
        plot_right = self._layout["plot_right"]
        has_low_outlier = self._layout["has_low_outlier"]
        if not has_low_outlier:
            t = (float(x) - plot_left) / max(1.0, plot_right - plot_left)
            return round(max(1.0, min(5.5, self.MAIN_MIN + t * (self.MAIN_MAX - self.MAIN_MIN))), 2)
        gutter_w = max(54.0 * self.dpi_scale, (plot_right - plot_left) * 0.095)
        main_left = plot_left + gutter_w
        if float(x) < main_left:
            t = (float(x) - (plot_left + 12.0 * self.dpi_scale)) / max(1.0, gutter_w - 22.0 * self.dpi_scale)
            return round(max(1.0, min(self.MAIN_MIN, self.OUTLIER_MIN + t * (self.MAIN_MIN - self.OUTLIER_MIN))), 2)
        t = (float(x) - main_left) / max(1.0, plot_right - main_left)
        return round(max(1.0, min(5.5, self.MAIN_MIN + t * (self.MAIN_MAX - self.MAIN_MIN))), 2)

    def hit_test_handle(self, x: float, y: float) -> dict | None:
        for hb in reversed(self.handle_hitboxes):
            x1, y1, x2, y2 = hb["bbox"]
            if x1 - 4 <= x <= x2 + 4 and y1 - 4 <= y <= y2 + 4:
                return hb
        for hb in reversed(self.handle_hitboxes):
            lx = hb["line_x"]
            y_top, y_bot = hb["line_y"]
            if abs(x - lx) <= 6 * self.dpi_scale and y_top <= y <= y_bot:
                return hb
        return None

    def hit_test_row(self, x: float, y: float) -> int | None:
        for rb in self.row_hitboxes:
            x1, y1, x2, y2 = rb["bbox"]
            if x1 <= x <= x2 and y1 <= y <= y2:
                return rb["index"]
        return None

    def render(self, chart_data: dict, *, hovered_row: int | None = None,
               active_handle: str | None = None, hovered_handle: str | None = None,
               preview_diff: dict | None = None) -> Image.Image:
        models = chart_data.get("models") or []
        floors = chart_data.get("floors") or {}
        route_floors = chart_data.get("route_floors") or {}
        critical_gates = chart_data.get("critical_gates") or {}

        needed_h = self.compute_height_for_models(len(models))
        w1 = self.width
        h1 = max(self.height, needed_h)

        ss = self.supersample
        W = w1 * ss
        H = h1 * ss
        k = self.dpi_scale * ss
        pal = self.pal

        img = Image.new("RGBA", (W, H), pal["surface"])
        draw = ImageDraw.Draw(img, "RGBA")

        def font(kind: str, name: str):
            return _load_font(kind, max(8, int(round(FONT_PX[name] * k))))

        f_title = font("bold", "title")
        f_sub = font("regular", "sub")
        f_row = font("bold", "row")
        f_acct = font("bold", "acct")
        f_mono = font("mono", "mono")
        f_mono_sm = font("mono", "mono_sm")
        f_tiny = font("regular", "tiny")
        f_pill = font("bold", "pill")

        left_margin = int(round(14 * k))
        left_col_w = self._left_column_width(draw, models, k, left_margin, f_acct, f_row, f_tiny, f_title)
        right_pad = int(round(22 * k))
        band_strip_top = int(round(BAND_STRIP_TOP * k))
        top_header_h = int(round(TOP_HEADER_H * k))
        crit_footer_h = int(round(CRIT_FOOTER_H * k))

        plot_left = left_col_w
        plot_right = W - right_pad
        plot_top = top_header_h
        row_h = self._row_height_px(len(models)) * ss
        rows_bottom = plot_top + max(6, len(models)) * row_h
        plot_bottom = rows_bottom + crit_footer_h

        has_low_outlier = any(m.get("min_ability", 3.0) < self.MAIN_MIN for m in models)

        self._layout = {
            "plot_left": plot_left / ss,
            "plot_right": plot_right / ss,
            "plot_top": plot_top / ss,
            "plot_bottom": plot_bottom / ss,
            "has_low_outlier": has_low_outlier,
            "width": w1,
            "height": h1,
        }
        self.handle_hitboxes = []
        self.row_hitboxes = []

        def vx(val: float) -> float:
            return self.value_to_x(val, plot_left / ss, plot_right / ss, has_low_outlier) * ss

        # 1. Plot background & softly shaded difficulty bands
        draw.rounded_rectangle(
            [int(round(8 * k)), int(round(6 * k)), W - int(round(8 * k)), H - int(round(6 * k))],
            radius=int(round(10 * k)),
            fill=pal["surface"],
            outline=pal["border"],
            width=max(1, int(round(1 * ss))),
        )
        draw.rectangle([plot_left, band_strip_top, plot_right, plot_bottom], fill=pal["plot_bg"])

        band_overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        bdraw = ImageDraw.Draw(band_overlay, "RGBA")
        x_routine = vx(floors.get("routine", 2.3))
        x_hard = vx(floors.get("hard", 3.1))
        x_coupled = vx(floors.get("coupled", 3.6))
        x_frontier = vx(floors.get("frontier", 4.2))

        bands = [
            (plot_left, x_routine, pal["band_below"], _tr("基础区")),
            (x_routine, x_hard, pal["band_routine"], _tr("常规达标")),
            (x_hard, x_coupled, pal["band_hard"], _tr("较难达标")),
            (x_coupled, x_frontier, pal["band_coupled"], _tr("耦合达标")),
            (x_frontier, plot_right, pal["band_frontier"], _tr("前沿达标")),
        ]
        for bx1, bx2, bcol, blabel in bands:
            cx1 = max(plot_left, min(plot_right, bx1))
            cx2 = max(plot_left, min(plot_right, bx2))
            if cx2 > cx1 + 1:
                bdraw.rectangle([cx1, band_strip_top, cx2, plot_bottom], fill=bcol)
                if cx2 - cx1 >= 52 * k:
                    bdraw.text(((cx1 + cx2) / 2, (band_strip_top + plot_top) / 2), blabel,
                               fill=_with_alpha(pal["text_mid"], 185), font=f_tiny, anchor="mm")

        img = Image.alpha_composite(img, band_overlay)
        draw = ImageDraw.Draw(img, "RGBA")

        # 2. Alternating row stripes & hover / diff highlights
        gained_labels = set()
        lost_labels = set()
        if preview_diff and preview_diff.get("has_changes"):
            for d_info in (preview_diff.get("difficulties") or {}).values():
                gained_labels.update(d_info.get("gained_models") or ())
                lost_labels.update(d_info.get("lost_models") or ())
            for r_info in (preview_diff.get("routes") or {}).values():
                gained_labels.update(r_info.get("gained_models") or ())
                lost_labels.update(r_info.get("lost_models") or ())

        row_overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        rdraw = ImageDraw.Draw(row_overlay, "RGBA")
        for idx, m in enumerate(models):
            ry1 = plot_top + idx * row_h
            ry2 = ry1 + row_h
            self.row_hitboxes.append({
                "index": idx,
                "key": m["key"],
                "bbox": (left_margin / ss, ry1 / ss, plot_right / ss, ry2 / ss),
            })
            dlbl = m.get("display_label")
            if idx == hovered_row:
                rdraw.rounded_rectangle(
                    [left_margin, ry1 + 1, plot_right, ry2 - 1],
                    radius=int(round(5 * k)),
                    fill=pal["row_hover"],
                )
            elif dlbl in gained_labels:
                rdraw.rectangle([left_margin, ry1, plot_right, ry2], fill=pal["row_gained"])
            elif dlbl in lost_labels:
                rdraw.rectangle([left_margin, ry1, plot_right, ry2], fill=pal["row_lost"])
            elif idx % 2 == 1:
                rdraw.rectangle([left_margin, ry1, plot_right, ry2], fill=pal["row_alt"])
            rdraw.line([(left_margin, ry2), (plot_right, ry2)], fill=_with_alpha(pal["border"], 95), width=1)

        img = Image.alpha_composite(img, row_overlay)
        draw = ImageDraw.Draw(img, "RGBA")

        # 3. Clean gridlines & axis ticks
        if has_low_outlier:
            gutter_w = max(54.0 * self.dpi_scale, (plot_right / ss - plot_left / ss) * 0.095) * ss
            break_x = plot_left + gutter_w
            _draw_dashed_line(
                draw,
                (break_x, band_strip_top),
                (break_x, plot_bottom),
                fill=pal["border"],
                width=max(1, int(round(1.2 * ss))),
                dash=int(round(4 * ss)),
                gap=int(round(4 * ss)),
            )
            for by in (band_strip_top, plot_bottom):
                draw.line(
                    [(break_x - 4 * ss, by + 4 * ss), (break_x - 1 * ss, by - 4 * ss)],
                    fill=pal["text_muted"],
                    width=max(1, int(round(1.3 * ss))),
                )
                draw.line(
                    [(break_x + 1 * ss, by + 4 * ss), (break_x + 4 * ss, by - 4 * ss)],
                    fill=pal["text_muted"],
                    width=max(1, int(round(1.3 * ss))),
                )
            ox = vx(1.0)
            draw.line([(ox, plot_bottom), (ox, plot_bottom + 5 * k)], fill=pal["text_muted"], width=max(1, ss))
            otxt = "1.0"
            ob = draw.textbbox((0, 0), otxt, font=f_mono_sm)
            draw.text((ox - (ob[2] - ob[0]) / 2, plot_bottom + 6 * k), otxt, fill=pal["text_muted"], font=f_mono_sm)

        # Minor grid ticks every 0.1 and major gridlines every 0.5 in [2.0, 5.4]
        for step_i in range(20, 55):
            val = round(step_i * 0.1, 1)
            gx = vx(val)
            is_major = (step_i % 5 == 0)
            if is_major:
                draw.line(
                    [(gx, plot_top), (gx, plot_bottom)],
                    fill=pal["grid_major"],
                    width=max(1, int(round(1.1 * ss))),
                )
                draw.line(
                    [(gx, plot_bottom), (gx, plot_bottom + 5 * k)],
                    fill=pal["text_muted"],
                    width=max(1, int(round(1.3 * ss))),
                )
                lbl = f"{val:.1f}"
                bb = draw.textbbox((0, 0), lbl, font=f_mono)
                tw = bb[2] - bb[0]
                draw.text((gx - tw / 2, plot_bottom + 6 * k), lbl, fill=pal["text_mid"], font=f_mono)
            else:
                draw.line(
                    [(gx, plot_top), (gx, plot_bottom)],
                    fill=pal["grid_minor"],
                    width=1,
                )
                draw.line(
                    [(gx, plot_bottom), (gx, plot_bottom + 2.5 * k)],
                    fill=pal["grid_major"],
                    width=1,
                )

        # Left column header & axis title
        draw.text(
            (left_margin + 4 * k, int(round(14 * k))),
            _tr("模型与账号档位"),
            fill=pal["text"],
            font=f_title,
        )
        legend = _wrap_text(draw, _tr("实心=已启用 · 双环=固定档 · 菱形=估算分 · 浅色=未勾选"), f_tiny,
                            plot_left - left_margin - 16 * k, max_lines=2)
        for li, line in enumerate(legend):
            draw.text((left_margin + 4 * k, int(round((38 + li * 15.5) * k))), line, fill=pal["text_muted"], font=f_tiny)
        draw.text(
            (left_margin + 4 * k, (band_strip_top + plot_top) / 2),
            _tr("所属账号 / 模型名称"),
            fill=pal["text_muted"],
            font=f_tiny,
            anchor="lm",
        )

        # Bottom axis caption
        axis_title = _tr("有效能力值（Base Tier × 思考档位加成）")
        at_box = draw.textbbox((0, 0), axis_title, font=f_sub)
        draw.text(
            (plot_right - (at_box[2] - at_box[0]), plot_bottom + int(round(26 * k))),
            axis_title,
            fill=pal["text_muted"],
            font=f_sub,
        )

        # 4. Critical gate lines (quiet dotted lines with a tag in the dedicated bottom strip below rows_bottom)
        crit_to_show = [("hard", critical_gates.get("hard")), ("frontier", critical_gates.get("frontier"))]
        if active_handle in DIFFICULTY_ORDER and active_handle not in ("frontier", "hard"):
            crit_to_show.append((active_handle, critical_gates.get(active_handle)))
        crit_tags = []  # drawn with the markers, above the floor lines that cross the footer strip
        for c_diff, c_val in crit_to_show:
            if c_val is None or not (self.MAIN_MIN <= float(c_val) <= self.MAIN_MAX):
                continue
            cx = vx(float(c_val))
            _draw_dashed_line(
                draw,
                (cx, plot_top),
                (cx, plot_bottom),
                fill=_with_alpha(pal["floor_critical"], 155),
                width=max(1, int(round(1.2 * ss))),
                dash=int(round(3 * ss)),
                gap=int(round(4 * ss)),
            )
            crit_tags.append((cx, f"{_tr(DIFFICULTY_SHORT.get(c_diff, c_diff))}+{_tr('关键')} {float(c_val):.2f}"))

        # 5. Secondary route-specific floor lines & handles (e.g. tool_execution.hard)
        route_items = sorted(route_floors.items(), key=lambda kv: kv[1])
        for rk, rval in route_items:
            rx = vx(rval)
            is_act = active_handle == f"route:{rk}" or hovered_handle == f"route:{rk}"
            rcol = pal["floor_route"]
            _draw_dashed_line(
                draw,
                (rx, band_strip_top),
                (rx, plot_bottom),
                fill=rcol,
                width=max(2, int(round((2.4 if is_act else 1.8) * ss))),
                dash=int(round(7 * ss)),
                gap=int(round(4 * ss)),
            )
            r_name, r_diff = rk.split(".", 1) if "." in rk else (rk, "hard")
            short_route = display_names.kind_label(r_name)
            r_text = f"{short_route}·{_tr(DIFFICULTY_SHORT.get(r_diff, r_diff))} {rval:.2f}"
            rw = draw.textlength(r_text, font=f_tiny) + int(round(22 * k))
            rh = int(round(22 * k))
            ry = int(round(8 * k))
            rx1 = max(plot_left, min(plot_right - rw, rx - rw / 2))
            draw.line([(rx1 + rw / 2, ry + rh), (rx, band_strip_top)], fill=_with_alpha(rcol, 175), width=max(1, ss))
            draw.rounded_rectangle(
                [rx1, ry, rx1 + rw, ry + rh],
                radius=int(round(5 * k)),
                fill=pal["handle_bg"],
                outline=rcol,
                width=max(1, int(round((2.0 if is_act else 1.3) * ss))),
            )
            # Vector grip dots on route handle pill
            gx_base = rx1 + int(round(6 * k))
            gy_base = ry + rh / 2 - int(round(3.4 * k))
            dot_r = max(1, int(round(1.0 * k)))
            for g_row in range(3):
                for g_col in range(2):
                    dx = gx_base + g_col * int(round(3.0 * k))
                    dy = gy_base + g_row * int(round(3.4 * k))
                    draw.ellipse([dx - dot_r, dy - dot_r, dx + dot_r, dy + dot_r], fill=_with_alpha(rcol, 200))
            draw.text((rx1 + int(round(15 * k)), ry + rh / 2), r_text, fill=rcol, font=f_tiny, anchor="lm")
            self.handle_hitboxes.append({
                "id": f"route:{rk}",
                "kind": "route",
                "key": rk,
                "value": rval,
                "bbox": (rx1 / ss, ry / ss, (rx1 + rw) / ss, (ry + rh) / ss),
                "line_x": rx / ss,
                "line_y": (band_strip_top / ss, plot_bottom / ss),
            })

        # 6. Primary difficulty floor lines & grabbable header handles (with collision avoidance)
        floor_colors = {
            "routine": pal["floor_routine"],
            "hard": pal["floor_hard"],
            "coupled": pal["floor_coupled"],
            "frontier": pal["floor_frontier"],
        }
        handle_specs = []
        for d in DIFFICULTY_ORDER:
            fval = float(floors.get(d, 0.0))
            fx = vx(fval)
            d_lbl = _tr(DIFFICULTY_SHORT.get(d, d))
            txt = f"{d_lbl} {fval:.2f}"
            pw = draw.textlength(txt, font=f_pill) + int(round(26 * k))
            ph = int(round(23 * k))
            handle_specs.append({
                "key": d,
                "val": fval,
                "fx": fx,
                "text": txt,
                "pw": pw,
                "ph": ph,
                "cx": max(plot_left + pw / 2, min(plot_right - pw / 2, fx)),
                "tier": 0,
            })

        # Horizontal collision avoidance for floor pills
        min_gap = int(round(6 * k))
        for _pass in range(4):
            for i in range(len(handle_specs) - 1):
                a, b = handle_specs[i], handle_specs[i + 1]
                overlap = (a["cx"] + a["pw"] / 2 + min_gap) - (b["cx"] - b["pw"] / 2)
                if overlap > 0:
                    shift = overlap / 2
                    a["cx"] = max(plot_left + a["pw"] / 2, a["cx"] - shift)
                    b["cx"] = min(plot_right - b["pw"] / 2, b["cx"] + shift)
        for i in range(len(handle_specs) - 1):
            a, b = handle_specs[i], handle_specs[i + 1]
            if (a["cx"] + a["pw"] / 2 + 2 * k) > (b["cx"] - b["pw"] / 2):
                b["tier"] = 1 - a["tier"]

        for spec in handle_specs:
            d = spec["key"]
            fx = spec["fx"]
            col = floor_colors[d]
            is_act = (active_handle == d or hovered_handle == d)
            line_w = max(2, int(round((2.6 if is_act else 1.9) * ss)))

            if is_act:
                draw.line(
                    [(fx, band_strip_top), (fx, plot_bottom)],
                    fill=_with_alpha(col, 55),
                    width=int(round(7 * ss)),
                )
            draw.line([(fx, band_strip_top), (fx, plot_bottom)], fill=col, width=line_w)

            pw, ph = spec["pw"], spec["ph"]
            hx1 = spec["cx"] - pw / 2
            hy1 = int(round((36 if spec["tier"] == 0 else 58) * k))
            hx2 = hx1 + pw
            hy2 = hy1 + ph

            draw.line(
                [(spec["cx"], hy2), (fx, band_strip_top)],
                fill=_with_alpha(col, 195),
                width=max(1, int(round(1.5 * ss))),
            )
            tri = int(round(4.2 * k))
            draw.polygon(
                [(fx - tri, band_strip_top), (fx + tri, band_strip_top), (fx, band_strip_top + tri * 1.3)],
                fill=col,
            )

            draw.rounded_rectangle(
                [hx1 + 1 * ss, hy1 + 2 * ss, hx2 + 1 * ss, hy2 + 2 * ss],
                radius=int(round(6 * k)),
                fill=pal["handle_shadow"],
            )
            draw.rounded_rectangle(
                [hx1, hy1, hx2, hy2],
                radius=int(round(6 * k)),
                fill=pal["handle_bg"],
                outline=col,
                width=max(1, int(round((2.2 if is_act else 1.5) * ss))),
            )

            gx_base = hx1 + int(round(7 * k))
            gy_base = hy1 + ph / 2 - int(round(3.8 * k))
            dot_r = max(1, int(round(1.1 * k)))
            for g_row in range(3):
                for g_col in range(2):
                    dx = gx_base + g_col * int(round(3.4 * k))
                    dy = gy_base + g_row * int(round(3.8 * k))
                    draw.ellipse([dx - dot_r, dy - dot_r, dx + dot_r, dy + dot_r], fill=_with_alpha(col, 200))

            draw.text(
                (hx1 + int(round(17 * k)), hy1 + ph / 2),
                spec["text"],
                fill=col if is_act else pal["text"],
                font=f_pill,
                anchor="lm",
            )

            self.handle_hitboxes.append({
                "id": d,
                "kind": "difficulty",
                "key": d,
                "value": spec["val"],
                "bbox": (hx1 / ss, hy1 / ss, hx2 / ss, hy2 / ss),
                "line_x": fx / ss,
                "line_y": (band_strip_top / ss, plot_bottom / ss),
            })

        # 7. Model tracks: left labels + effort range bars + effort markers + inline value labels
        marker_overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        mdraw = ImageDraw.Draw(marker_overlay, "RGBA")

        for idx, m in enumerate(models):
            ry1 = plot_top + idx * row_h
            ry_mid = ry1 + row_h / 2
            is_hov = (idx == hovered_row)
            enabled = bool(m.get("enabled", True))
            estimated = bool(m.get("estimated", False))
            alpha = 255 if enabled else 105
            fam_col = _with_alpha(_family_color(m.get("account", ""), pal), alpha)

            # Left column: account pill + model label
            acct_txt = str(m.get("account_label") or m.get("account") or "")
            aw = mdraw.textlength(acct_txt, font=f_acct) + int(round(12 * k))
            ah = int(round(19 * k))
            ax1 = left_margin + int(round(4 * k))
            ay1 = ry_mid - ah / 2
            mdraw.rounded_rectangle(
                [ax1, ay1, ax1 + aw, ay1 + ah],
                radius=int(round(4 * k)),
                fill=_with_alpha(fam_col, 34 if enabled else 18),
                outline=_with_alpha(fam_col, 135 if enabled else 65),
                width=1,
            )
            mdraw.text(
                (ax1 + aw / 2, ry_mid),
                acct_txt,
                fill=_with_alpha(fam_col, 245 if enabled else 130),
                font=f_acct,
                anchor="mm",
            )

            mx = ax1 + aw + int(round(7 * k))
            mdl_txt = _fit_text(mdraw, str(m.get("model_label") or m.get("model") or ""), f_row,
                                plot_left - mx - int(round(8 * k)))
            mdraw.text(
                (mx, ry_mid),
                mdl_txt,
                fill=_with_alpha(pal["text"], 255 if enabled else 120),
                font=f_row,
                anchor="lm",
            )

            # Badges in left column (固定 / 估 / 未选)
            fixed_eff = m.get("fixed_effort")
            bx_cursor = mx + mdraw.textlength(mdl_txt, font=f_row) + int(round(6 * k))
            if fixed_eff and bx_cursor < plot_left - int(round(26 * k)):
                ftag = f"{_tr('固定')}·{EFFORT_SHORT.get(str(fixed_eff).lower(), str(fixed_eff))}"
                fw = mdraw.textlength(ftag, font=f_tiny) + int(round(8 * k))
                fh = int(round(17 * k))
                fy = ry_mid - fh / 2
                mdraw.rounded_rectangle(
                    [bx_cursor, fy, bx_cursor + fw, fy + fh],
                    radius=int(round(3 * k)),
                    fill=pal["off_badge_bg"],
                    outline=_with_alpha(fam_col, 130),
                    width=1,
                )
                mdraw.text(
                    (bx_cursor + fw / 2, ry_mid),
                    ftag,
                    fill=_with_alpha(pal["text_mid"], alpha),
                    font=f_tiny,
                    anchor="mm",
                )
                bx_cursor += fw + int(round(4 * k))

            if estimated and bx_cursor < plot_left - int(round(22 * k)):
                etag = _tr("估")
                ew = mdraw.textlength(etag, font=f_tiny) + int(round(8 * k))
                eh = int(round(17 * k))
                ey = ry_mid - eh / 2
                mdraw.rounded_rectangle(
                    [bx_cursor, ey, bx_cursor + ew, ey + eh],
                    radius=int(round(3 * k)),
                    fill=_with_alpha(pal["est_badge_bg"], alpha),
                )
                mdraw.text(
                    (bx_cursor + ew / 2, ry_mid),
                    etag,
                    fill=_with_alpha(pal["est_badge_fg"], alpha),
                    font=f_tiny,
                    anchor="mm",
                )
                bx_cursor += ew + int(round(4 * k))

            if not enabled and bx_cursor < plot_left - int(round(28 * k)):
                otag = _tr("未选")
                ow = mdraw.textlength(otag, font=f_tiny) + int(round(8 * k))
                oh = int(round(17 * k))
                oy = ry_mid - oh / 2
                mdraw.rounded_rectangle(
                    [bx_cursor, oy, bx_cursor + ow, oy + oh],
                    radius=int(round(3 * k)),
                    fill=pal["off_badge_bg"],
                )
                mdraw.text((bx_cursor + ow / 2, ry_mid), otag, fill=pal["off_badge_fg"], font=f_tiny, anchor="mm")

            # Effort range bar & dots inside plot
            efforts = m.get("efforts") or []
            if not efforts:
                continue
            x_min = vx(m["min_ability"])
            x_max = vx(m["max_ability"])

            if x_max > x_min + 1:
                bar_w = max(2, int(round((3.6 if is_hov else 2.8) * k)))
                if estimated:
                    _draw_dashed_line(
                        mdraw,
                        (x_min, ry_mid),
                        (x_max, ry_mid),
                        fill=_with_alpha(fam_col, 190 if enabled else 85),
                        width=bar_w,
                        dash=int(round(5 * k)),
                        gap=int(round(3.5 * k)),
                    )
                else:
                    mdraw.line(
                        [(x_min, ry_mid), (x_max, ry_mid)],
                        fill=_with_alpha(fam_col, 195 if enabled else 80),
                        width=bar_w,
                    )

            for eff in efforts:
                ex = vx(eff["ability"])
                e_on = bool(eff.get("enabled", True)) and enabled
                e_alpha = 255 if e_on else 100
                dot_col = _with_alpha(_family_color(m.get("account", ""), pal), e_alpha)
                is_high = str(eff.get("effort") or "").lower() in ("high", "default")
                is_fixed = bool(eff.get("fixed") or fixed_eff)
                r = int(round((5.8 if is_hov else 4.8) * k))

                if estimated:
                    # Distinct diamond marker for estimated/unmeasured models
                    dr = r + int(round(1.0 * k))
                    poly = [(ex, ry_mid - dr), (ex + dr, ry_mid), (ex, ry_mid + dr), (ex - dr, ry_mid)]
                    mdraw.polygon(poly, fill=pal["plot_bg"], outline=dot_col)
                    mdraw.line(poly + [poly[0]], fill=dot_col, width=max(1, int(round(1.8 * ss))))
                elif is_fixed:
                    # Distinct locked/fixed marker: vertical pin tick + outer double ring + inner solid core
                    tick_h = r + int(round(4.0 * k))
                    mdraw.line(
                        [(ex, ry_mid - tick_h), (ex, ry_mid + tick_h)],
                        fill=_with_alpha(dot_col, 185),
                        width=max(1, int(round(1.4 * ss))),
                    )
                    hr = r + int(round(2.6 * k))
                    mdraw.ellipse(
                        [ex - hr, ry_mid - hr, ex + hr, ry_mid + hr],
                        fill=_with_alpha(dot_col, 36 if e_on else 18),
                        outline=_with_alpha(dot_col, 210 if e_on else 85),
                        width=max(1, int(round(1.5 * ss))),
                    )
                    ir = max(2, int(round(r * 0.72)))
                    mdraw.ellipse(
                        [ex - ir, ry_mid - ir, ex + ir, ry_mid + ir],
                        fill=dot_col if e_on else pal["plot_bg"],
                    )
                else:
                    if is_high and len(efforts) > 1:
                        hr = r + int(round(2.4 * k))
                        mdraw.ellipse(
                            [ex - hr, ry_mid - hr, ex + hr, ry_mid + hr],
                            fill=_with_alpha(dot_col, 42 if e_on else 20),
                            outline=_with_alpha(dot_col, 150 if e_on else 60),
                            width=max(1, int(round(1.1 * ss))),
                        )
                    mdraw.ellipse(
                        [ex - r, ry_mid - r, ex + r, ry_mid + r],
                        fill=dot_col if e_on else pal["plot_bg"],
                        outline=dot_col if not e_on else pal["surface"],
                        width=max(1, int(round(1.4 * ss))),
                    )

            # Inline score / effort range callout next to the track (never overlaps other rows)
            if abs(m["max_ability"] - m["min_ability"]) < 1e-3:
                e_name = EFFORT_SHORT.get(str(efforts[0].get("effort") or "high").lower(), "H")
                if fixed_eff:
                    val_txt = f"{m['max_ability']:.2f} ({e_name}·{_tr('固定')})"
                else:
                    val_txt = f"{m['max_ability']:.2f} ({e_name})"
            else:
                val_txt = f"{m['min_ability']:.2f}–{m['max_ability']:.2f}"
            dlbl = m.get("display_label")
            if dlbl in gained_labels:
                val_txt += f"  +{_tr('新增达标')}"
                vcol = pal["gain_fg"]
            elif dlbl in lost_labels:
                val_txt += f"  -{_tr('失去达标')}"
                vcol = pal["loss_fg"]
            else:
                vcol = pal["text"] if is_hov else _with_alpha(pal["text_mid" if enabled else "text_muted"], 235 if enabled else 140)
            vfont = f_row if is_hov else f_mono_sm
            vw = mdraw.textlength(val_txt, font=vfont)
            vx_label = x_max + int(round(10 * k))
            if vx_label + vw > plot_right - int(round(6 * k)):
                vx_label = x_min - vw - int(round(10 * k))
            # A soft backing keeps the figure readable where a floor line crosses it.
            mdraw.rounded_rectangle([vx_label - 2 * k, ry_mid - 9 * k, vx_label + vw + 2 * k, ry_mid + 9 * k],
                                    radius=int(round(3 * k)), fill=_with_alpha(pal["plot_bg"], 225))
            mdraw.text((vx_label, ry_mid), val_txt, fill=vcol, font=vfont, anchor="lm")

        for cx, c_lbl in crit_tags:
            cw = mdraw.textlength(c_lbl, font=f_tiny)
            ch = 19 * k
            cy = rows_bottom + (crit_footer_h - ch) / 2
            px1 = max(plot_left + 2 * k, min(plot_right - cw - 12 * k, cx - cw / 2 - 6 * k))
            mdraw.rounded_rectangle(
                [px1, cy, px1 + cw + 12 * k, cy + ch],
                radius=int(round(4 * k)),
                fill=_with_alpha(pal["surface"], 245),
                outline=_with_alpha(pal["floor_critical"], 175),
                width=1,
            )
            mdraw.text((px1 + 6 * k, cy + ch / 2), c_lbl, fill=pal["floor_critical"], font=f_tiny, anchor="lm")

        # Left column vertical divider & horizontal rails
        mdraw.line([(plot_left, int(round(6 * k))), (plot_left, plot_bottom)], fill=pal["border"], width=max(1, ss))
        mdraw.line([(left_margin, band_strip_top), (plot_right, band_strip_top)], fill=pal["border"], width=max(1, ss))
        mdraw.line([(left_margin, plot_top), (plot_right, plot_top)], fill=pal["border"], width=max(1, ss))
        mdraw.line([(left_margin, rows_bottom), (plot_right, rows_bottom)], fill=_with_alpha(pal["border"], 140), width=1)
        mdraw.line([(left_margin, plot_bottom), (plot_right, plot_bottom)], fill=pal["border"], width=max(1, ss))

        img = Image.alpha_composite(img, marker_overlay)

        # 8. Floating hover tooltip card when hovering a model row
        if hovered_row is not None and 0 <= hovered_row < len(models):
            img = self._draw_hover_tooltip(img, models[hovered_row], hovered_row, len(models), floors, route_floors,
                                           critical_gates, plot_left, plot_right, plot_top, rows_bottom, row_h, k, ss, vx)

        # An exact box reduce of the supersampled image: same anti-aliasing, a fraction of LANCZOS's cost at panel size.
        return img.reduce(ss) if ss > 1 else img

    def _draw_hover_tooltip(self, img: Image.Image, m: dict, row_idx: int, total_rows: int, floors: dict,
                            route_floors: dict, critical_gates: dict, plot_left: int, plot_right: int,
                            plot_top: int, rows_bottom: int, row_h: int, k: float, ss: int, vx) -> Image.Image:
        pal = self.pal
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        tdraw = ImageDraw.Draw(overlay, "RGBA")

        f_head = _load_font("bold", max(10, int(round(FONT_PX["tip_head"] * k))))
        f_body = _load_font("regular", max(9, int(round(FONT_PX["tip_body"] * k))))
        f_bold_sm = _load_font("bold", max(9, int(round(FONT_PX["tip_body"] * k))))

        card_w = int(round(300 * k))
        pad = int(round(11 * k))

        title = f"{m.get('account_label')} · {m.get('model_label')}"
        status_tags = [
            _tr("已启用") if m.get("enabled") else _tr("未勾选"),
            _tr("估算先验") if m.get("estimated") else _tr("实测校准"),
            f"Base Tier {m.get('tier', 0.0):.2f}",
        ]
        status_line = "  ·  ".join(status_tags)

        eff_parts = [
            f"{e['effort']}: {e['ability']:.2f}" + ("" if e.get("enabled") else f"({_tr('停用')})")
            for e in (m.get("efforts") or [])
        ]
        eff_line = _tr("思考档位：") + "  |  ".join(eff_parts)

        clears_parts = []
        for d in DIFFICULTY_ORDER:
            ok = m.get("clears", {}).get(d, False)
            state_str = "OK" if ok else "—"
            clears_parts.append(f"{_tr(DIFFICULTY_SHORT[d])}({floors.get(d, 0):.2f}):{state_str}")
        clears_line = _tr("门槛达标：") + "   ".join(clears_parts)

        # Widen the card to its longest fixed line (the clears line runs past the base width in Chinese),
        # never past the plot area.
        widest = max(
            tdraw.textbbox((0, 0), text, font=font)[2]
            for text, font in ((title, f_head), (status_line, f_bold_sm), (eff_line, f_body), (clears_line, f_body))
        )
        card_w = min(max(card_w, widest + 2 * pad), plot_right - plot_left - int(round(24 * k)))

        basis_raw = m.get("basis") or _tr("公开基准校准数据")
        basis_lines = _wrap_text(tdraw, _tr("依据：") + basis_raw, f_body, card_w - 2 * pad, max_lines=3)

        line_h = int(round(18 * k))
        card_h = pad * 2 + int(round(22 * k)) + line_h * (3 + len(basis_lines)) + int(round(6 * k))

        # Models form a top-right to bottom-left staircase: place tooltip in the empty bottom-right
        # quadrant when hovering top rows, or in the empty top-left quadrant when hovering bottom rows.
        if row_idx < total_rows * 0.55:
            cx2 = plot_right - int(round(12 * k))
            cx1 = max(plot_left + int(round(12 * k)), cx2 - card_w)
            cy2 = rows_bottom - int(round(10 * k))
            cy1 = max(plot_top + int(round(10 * k)), cy2 - card_h)
        else:
            cx1 = plot_left + int(round(12 * k))
            cx2 = min(plot_right - int(round(12 * k)), cx1 + card_w)
            cy1 = plot_top + int(round(10 * k))
            cy2 = min(rows_bottom - int(round(10 * k)), cy1 + card_h)

        tdraw.rounded_rectangle(
            [cx1 + 2 * ss, cy1 + 3 * ss, cx2 + 2 * ss, cy2 + 3 * ss],
            radius=int(round(8 * k)),
            fill=pal["tooltip_shadow"],
        )
        tdraw.rounded_rectangle(
            [cx1, cy1, cx2, cy2],
            radius=int(round(8 * k)),
            fill=pal["tooltip_bg"],
            outline=pal["tooltip_border"],
            width=max(1, int(round(1.3 * ss))),
        )

        y_cur = cy1 + pad
        tdraw.text((cx1 + pad, y_cur), title, fill=pal["text"], font=f_head)
        y_cur += int(round(22 * k))
        tdraw.text((cx1 + pad, y_cur), status_line, fill=pal["floor_hard"], font=f_bold_sm)
        y_cur += line_h
        tdraw.text((cx1 + pad, y_cur), eff_line, fill=pal["text"], font=f_body)
        y_cur += line_h
        tdraw.text((cx1 + pad, y_cur), clears_line, fill=pal["text_mid"], font=f_body)
        y_cur += line_h + int(round(3 * k))
        for bl in basis_lines:
            tdraw.text((cx1 + pad, y_cur), bl, fill=pal["text_muted"], font=f_body)
            y_cur += line_h

        return Image.alpha_composite(img, overlay)


class AbilityFloorChartCanvas:
    """Interactive Tkinter Canvas hosting the high-DPI AbilityFloorRenderer."""

    def __init__(self, parent, *, data_loader: Callable[[], dict] | None = None,
                 on_floor_drag: Callable[[dict, dict, dict], None] | None = None,
                 on_floor_commit: Callable[[dict, dict], None] | None = None,
                 theme_mode: str = "light", width: int = 860, height: int = 430):
        import tkinter as tk
        from PIL import ImageTk

        self._tk = tk
        self._ImageTk = ImageTk
        self.data_loader = data_loader
        self.on_floor_drag = on_floor_drag
        self.on_floor_commit = on_floor_commit
        self.theme_mode = theme_mode
        self.renderer = AbilityFloorRenderer(width=width, height=height, theme_mode=theme_mode)

        bg_hex = "#0F172A" if theme_mode == "dark" else theme.CARD
        self.canvas = tk.Canvas(
            parent,
            width=width,
            height=height,
            bg=bg_hex,
            highlightthickness=0,
            bd=0,
            cursor="arrow",
        )
        self.widget = self.canvas
        self._photo = None
        self._image_item = None
        self._chart_data: dict = {}
        self._draft_floors: dict[str, float] = {}
        self._draft_route_floors: dict[str, float] = {}
        self._preview_diff: dict | None = None
        self._hovered_row: int | None = None
        self._hovered_handle: str | None = None
        self._active_handle: dict | None = None

        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)

    def pack(self, **kwargs):
        self.canvas.pack(**kwargs)

    def grid(self, **kwargs):
        self.canvas.grid(**kwargs)

    def set_theme_mode(self, mode: str) -> None:
        self.theme_mode = "dark" if str(mode).lower() == "dark" else "light"
        self.renderer = AbilityFloorRenderer(
            width=self.renderer.width,
            height=self.renderer.height,
            theme_mode=self.theme_mode,
        )
        self.redraw()

    def set_data(self, chart_data: dict, preview_diff: dict | None = None) -> None:
        self._chart_data = dict(chart_data or {})
        self._draft_floors = dict(self._chart_data.get("floors") or {})
        self._draft_route_floors = dict(self._chart_data.get("route_floors") or {})
        self._preview_diff = preview_diff
        needed_h = self.renderer.compute_height_for_models(len(self._chart_data.get("models") or []))
        if abs(int(self.canvas.winfo_reqheight()) - needed_h) > 4:
            self.canvas.configure(height=needed_h)
            self.renderer.height = needed_h
        self.redraw()

    def render_pil_image(self) -> Image.Image:
        if not self._chart_data and self.data_loader is not None:
            self._chart_data = self.data_loader()
            self._draft_floors = dict(self._chart_data.get("floors") or {})
            self._draft_route_floors = dict(self._chart_data.get("route_floors") or {})
        render_data = dict(self._chart_data)
        if self._draft_floors:
            render_data["floors"] = dict(self._draft_floors)
        if self._draft_route_floors:
            render_data["route_floors"] = dict(self._draft_route_floors)
        crit_h = render_data.get("critical_headrooms") or {}
        if crit_h and self._draft_floors:
            render_data["critical_gates"] = {
                k: round(self._draft_floors.get(k, 0.0) + float(crit_h.get(k, 0.3)), 2)
                for k in DIFFICULTY_ORDER
            }
        return self.renderer.render(
            render_data,
            hovered_row=self._hovered_row,
            active_handle=self._active_handle["id"] if self._active_handle else None,
            hovered_handle=self._hovered_handle,
            preview_diff=self._preview_diff,
        )

    def redraw(self) -> None:
        if not self.canvas.winfo_exists():
            return
        pil_img = self.render_pil_image()
        self._photo = self._ImageTk.PhotoImage(pil_img)
        if self._image_item is None:
            self._image_item = self.canvas.create_image(0, 0, anchor="nw", image=self._photo)
        else:
            self.canvas.itemconfigure(self._image_item, image=self._photo)

    def _on_configure(self, event) -> None:
        w = max(560, int(event.width))
        if abs(w - self.renderer.width) > 4:
            self.renderer.width = w
            self.redraw()

    def _on_motion(self, event) -> None:
        if self._active_handle is not None:
            return
        hb = self.renderer.hit_test_handle(event.x, event.y)
        row = self.renderer.hit_test_row(event.x, event.y) if hb is None else None
        new_handle = hb["id"] if hb else None
        cursor = "sb_h_double_arrow" if hb is not None else ("hand2" if row is not None else "arrow")
        self.canvas.configure(cursor=cursor)
        if new_handle != self._hovered_handle or row != self._hovered_row:
            self._hovered_handle = new_handle
            self._hovered_row = row
            self.redraw()

    def _on_leave(self, _event) -> None:
        if self._active_handle is not None:
            return
        if self._hovered_handle is not None or self._hovered_row is not None:
            self._hovered_handle = None
            self._hovered_row = None
            self.canvas.configure(cursor="arrow")
            self.redraw()

    def _on_press(self, event) -> None:
        hb = self.renderer.hit_test_handle(event.x, event.y)
        if hb is not None:
            self._active_handle = hb
            self._hovered_row = None
            self.canvas.configure(cursor="sb_h_double_arrow")
            self.redraw()

    def _on_drag(self, event) -> None:
        if self._active_handle is None:
            return
        import router_data

        raw_val = self.renderer.x_to_value(event.x)
        kind = self._active_handle["kind"]
        key = self._active_handle["key"]
        if kind == "difficulty":
            clamped = router_data.clamp_ability_floor_drag(key, raw_val, self._draft_floors, self._draft_route_floors)
            if self._draft_floors.get(key) == clamped:
                return
            self._draft_floors[key] = clamped
        else:
            low, high = router_data.ABILITY_FLOOR_LIMITS
            clamped = round(max(low, min(high, raw_val)), 2)
            if self._draft_route_floors.get(key) == clamped:
                return
            self._draft_route_floors[key] = clamped

        self._preview_diff = router_data.preview_ability_floor_change(
            floors_override=self._draft_floors,
            route_floors_override=self._draft_route_floors,
        )
        if self._preview_diff and "candidate" in self._preview_diff:
            self._chart_data = self._preview_diff["candidate"]
        if self.on_floor_drag is not None:
            self.on_floor_drag(dict(self._draft_floors), dict(self._draft_route_floors), self._preview_diff)
        self.redraw()

    def _on_release(self, _event) -> None:
        if self._active_handle is None:
            return
        self._active_handle = None
        if self.on_floor_commit is not None:
            self.on_floor_commit(dict(self._draft_floors), dict(self._draft_route_floors))
        self.redraw()
