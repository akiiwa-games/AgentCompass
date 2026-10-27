"""One visual language for every AgentCompass window (main panel, network status, update progress)."""
from __future__ import annotations

BG = "#F4F6F9"
CARD = "#FFFFFF"
BORDER = "#E2E8F0"
INK = "#0F172A"
TEXT = "#334155"
MUTED = "#64748B"
FAINT = "#94A3B8"
ACCENT = "#4F46E5"
ACCENT_SOFT = "#EEF2FF"
GREEN = "#10B981"
GREEN_SOFT = "#ECFDF5"
AMBER = "#F59E0B"
AMBER_SOFT = "#FFFBEB"
RED = "#EF4444"
RED_SOFT = "#FEF2F2"
CYAN = "#06B6D4"
CYAN_SOFT = "#ECFEFF"
SELECTED = "#E0E7FF"

FONT = "Microsoft YaHei UI"
MONO = "Cascadia Mono"

FACTOR = 1.0  # screen pixels per 96-dpi pixel, set by apply()


def px(value: float) -> int:
    """A layout size given at 96 dpi, in this screen's pixels (fonts scale by themselves; pixel sizes do not)."""
    return int(round(value * FACTOR))


STATUS_COLORS = {"ok": (GREEN, GREEN_SOFT), "warn": (AMBER, AMBER_SOFT), "error": (RED, RED_SOFT),
                 "info": (ACCENT, ACCENT_SOFT), "idle": (MUTED, "#F1F5F9")}


def status_kind(label: str) -> str:
    """Colour class of a service label such as 运行中 / 已停止 / 未安装."""
    text = label or ""
    if text.startswith(("运行中", "Running")) or text in ("就绪", "已启用", "Ready", "Enabled"):
        return "ok"
    if any(word in text for word in ("启动中", "更新中", "检测中", "隧道未开", "暂停",
                                     "Starting", "Updating", "Checking", "tunnel stopped", "Paused")):
        return "warn"
    if any(word in text for word in ("失败", "异常", "错误", "failed", "error", "Error")):
        return "error"
    return "idle"


def mono_font(root) -> str:
    """Cascadia Mono when installed, else Consolas."""
    try:
        from tkinter import font

        return MONO if MONO in font.families(root) else "Consolas"
    except Exception:
        return "Consolas"


def apply(root, title: str | None = None, geometry: str | None = None, minsize: tuple[int, int] | None = None):
    """Configure the root window and the shared ttk styles; returns the ttk.Style. Sizes are given at 96 dpi."""
    global FACTOR
    from tkinter import ttk

    try:
        FACTOR = max(1.0, float(root.tk.call("tk", "scaling")) / (96 / 72))
    except Exception:
        FACTOR = 1.0
    if title:
        root.title(title)
    if geometry:
        width, height = (int(v) for v in geometry.split("x"))
        root.geometry(f"{px(width)}x{px(height)}")
    if minsize:
        root.minsize(px(minsize[0]), px(minsize[1]))
    root.configure(bg=BG)
    mono = mono_font(root)
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=BG, foreground=TEXT, font=(FONT, 10))
    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=CARD)
    style.configure("TLabel", background=BG, foreground=TEXT, font=(FONT, 10))
    style.configure("Card.TLabel", background=CARD)
    style.configure("Title.TLabel", foreground=INK, font=(FONT, 20, "bold"))
    style.configure("H2.TLabel", foreground=INK, font=(FONT, 13, "bold"))
    style.configure("CardH2.TLabel", background=CARD, foreground=INK, font=(FONT, 12, "bold"))
    style.configure("Sub.TLabel", foreground=MUTED)
    style.configure("CardSub.TLabel", background=CARD, foreground=MUTED, font=(FONT, 9))
    style.configure("Mono.TLabel", background=CARD, foreground=INK, font=(mono, 10))
    style.configure("TButton", font=(FONT, 10), padding=(12, 6), background=CARD, foreground=INK,
                    bordercolor=BORDER, lightcolor=CARD, darkcolor=CARD, focuscolor=CARD)
    style.map("TButton", background=[("active", ACCENT_SOFT), ("disabled", BG)],
              foreground=[("disabled", FAINT)])
    style.configure("Accent.TButton", background=ACCENT, foreground="#FFFFFF", bordercolor=ACCENT,
                    lightcolor=ACCENT, darkcolor=ACCENT, focuscolor=ACCENT)
    style.map("Accent.TButton", background=[("active", "#4338CA"), ("disabled", "#A5B4FC")],
              foreground=[("disabled", "#EEF2FF")])
    style.configure("Small.TButton", font=(FONT, 9), padding=(8, 3), width=-6)
    for widget in ("TCheckbutton", "TRadiobutton"):
        style.configure(widget, background=CARD, foreground=TEXT, font=(FONT, 10), indicatorbackground=CARD,
                        indicatorforeground=ACCENT, indicatormargin=(0, 0, px(6), 0), indicatorsize=px(14),
                        upperbordercolor=FAINT, lowerbordercolor=FAINT)
        style.map(widget, background=[("active", CARD)],
                  indicatorbackground=[("selected", ACCENT_SOFT), ("active", ACCENT_SOFT)])
    try:
        check_indicator(root, style)
    except Exception:
        pass  # clam's own indicator stays
    style.configure("TEntry", fieldbackground=CARD, bordercolor=BORDER, lightcolor=BORDER, padding=6)
    style.configure("TCombobox", fieldbackground=CARD, bordercolor=BORDER, padding=4)
    style.map("TCombobox", fieldbackground=[("readonly", CARD)], selectbackground=[("readonly", CARD)],
              selectforeground=[("readonly", INK)])
    style.configure("TSpinbox", fieldbackground=CARD, bordercolor=BORDER, padding=4)
    style.configure("Treeview", rowheight=px(30), font=(FONT, 10), background=CARD, fieldbackground=CARD,
                    foreground=TEXT, borderwidth=0)
    style.configure("Treeview.Heading", font=(FONT, 9, "bold"), background=BG, foreground=MUTED,
                    relief="flat", padding=(8, 8))
    style.map("Treeview", background=[("selected", SELECTED)], foreground=[("selected", INK)])
    style.map("Treeview.Heading", background=[("active", BG)])
    style.configure("Horizontal.TProgressbar", troughcolor="#E2E8F0", background=ACCENT, bordercolor="#E2E8F0",
                    lightcolor=ACCENT, darkcolor=ACCENT, thickness=px(6))
    for name, color in (("Green", GREEN), ("Amber", AMBER), ("Red", RED)):
        style.configure(f"{name}.Horizontal.TProgressbar", troughcolor="#E2E8F0", background=color,
                        bordercolor="#E2E8F0", lightcolor=color, darkcolor=color, thickness=px(6))
    style.configure("Vertical.TScrollbar", background=BG, troughcolor=BG, bordercolor=BG, arrowcolor=MUTED)
    # Compact variants for the main panel.
    style.configure("Page.TLabel", foreground=INK, font=(FONT, 14, "bold"))
    style.configure("CardH3.TLabel", background=CARD, foreground=INK, font=(FONT, 10, "bold"))
    style.configure("CardTiny.TLabel", background=CARD, foreground=MUTED, font=(FONT, 8))
    style.configure("Tiny.TButton", font=(FONT, 9), padding=(6, 1), width=-4)
    style.configure("SmallAccent.TButton", font=(FONT, 9), padding=(8, 3), width=-6, background=ACCENT, foreground="#FFFFFF",
                    bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT, focuscolor=ACCENT)
    style.map("SmallAccent.TButton", background=[("active", "#4338CA"), ("disabled", "#A5B4FC")],
              foreground=[("disabled", "#EEF2FF")])
    style.configure("Card.Horizontal.TScale", background=CARD, troughcolor="#E2E8F0", bordercolor=BORDER,
                    lightcolor=CARD, darkcolor=CARD)
    style.configure("Slim.Treeview", rowheight=px(25), font=(FONT, 9), background=CARD, fieldbackground=CARD,
                    foreground=TEXT, borderwidth=0)
    style.configure("Slim.Treeview.Heading", font=(FONT, 9, "bold"), background=BG, foreground=MUTED,
                    relief="flat", padding=(6, 4))
    style.map("Slim.Treeview", background=[("selected", SELECTED)], foreground=[("selected", INK)])
    style.map("Slim.Treeview.Heading", background=[("active", BG)])
    return style


def check_indicator(root, style) -> None:
    """A rounded accent box with a white tick for ttk checkbuttons (clam draws an X)."""
    from PIL import Image, ImageDraw, ImageTk

    size, scale = px(16), 4
    big = size * scale

    def make(checked: bool):
        image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        radius = big // 4
        if checked:
            draw.rounded_rectangle((0, 0, big - 1, big - 1), radius, fill=ACCENT)
            draw.line([(big * 0.25, big * 0.52), (big * 0.43, big * 0.70), (big * 0.76, big * 0.32)],
                      fill="#FFFFFF", width=max(2, big // 8), joint="curve")
        else:
            draw.rounded_rectangle((2, 2, big - 3, big - 3), radius, fill="#FFFFFF", outline=FAINT, width=max(2, big // 14))
        return ImageTk.PhotoImage(image.resize((size, size), Image.LANCZOS), master=root)

    images = (make(False), make(True))
    root._route_manager_checks = images  # PhotoImages must outlive this call
    style.element_create("RM.Checkbutton.indicator", "image", images[0], ("selected", images[1]),
                         sticky="w", padding=(0, 0, px(8), 0))
    style.layout("TCheckbutton", [("Checkbutton.padding", {"sticky": "nswe", "children": [
        ("RM.Checkbutton.indicator", {"side": "left", "sticky": ""}),
        ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [("Checkbutton.label", {"sticky": "nswe"})]}),
    ]})])


def compass_image(size: int = 64, tile: bool = True):
    """The AgentCompass mark as a PIL RGBA image: a dial with N/E/S/W ticks and a two-tone needle pointing NE.

    Drawn at 4x on a 64-unit grid and downscaled. With tile=True it sits on the dark rounded tray tile, nudged up
    and right so the tray's status dot (lower left) does not cover the needle."""
    import math

    from PIL import Image, ImageDraw

    scale = 4 * size / 64
    image = Image.new("RGBA", (size * 4, size * 4), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    def at(x, y):
        return x * scale, y * scale

    if tile:
        draw.rounded_rectangle((*at(2, 2), *at(62, 62)), 15 * scale, fill=(15, 23, 42, 255))
        cx, cy, r = 34, 30, 20
    else:
        cx, cy, r = 32, 32, 29
    draw.ellipse((*at(cx - r, cy - r), *at(cx + r, cy + r)), fill=(23, 32, 60, 255), outline="#475569",
                 width=round(2.4 * scale))
    draw.ellipse((*at(cx - r + 3.2, cy - r + 3.2), *at(cx + r - 3.2, cy + r - 3.2)), outline="#6366F1",
                 width=max(1, round(0.9 * scale)))
    for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
        draw.line((*at(cx + dx * (r - 3.5), cy + dy * (r - 3.5)), *at(cx + dx * (r - 8), cy + dy * (r - 8))),
                  fill="#94A3B8", width=round(2.2 * scale))
    length, half = r - 5.5, r * 0.24
    ux, uy = math.sqrt(0.5), -math.sqrt(0.5)  # towards north-east
    px_, py_ = -uy, ux                        # perpendicular, towards south-east
    centre = at(cx, cy)
    north_east = at(cx + ux * length, cy + uy * length)
    south_west = at(cx - ux * length, cy - uy * length)
    north_west = at(cx - px_ * half, cy - py_ * half)
    south_east = at(cx + px_ * half, cy + py_ * half)
    for tip, lit, shade in ((north_east, "#67E8F9", "#38BDF8"), (south_west, "#818CF8", "#6366F1")):
        draw.polygon((tip, centre, north_west), fill=lit)
        draw.polygon((tip, centre, south_east), fill=shade)
    pivot = 2.6 if tile else 3.2
    draw.ellipse((*at(cx - pivot, cy - pivot), *at(cx + pivot, cy + pivot)), fill="#F8FAFC", outline=(15, 23, 42, 255),
                 width=max(1, round(0.8 * scale)))
    return image.resize((size, size), Image.LANCZOS)


def set_window_icon(root) -> None:
    """The AgentCompass mark in the titlebar and on the taskbar button."""
    from PIL import ImageTk

    images = [ImageTk.PhotoImage(compass_image(n), master=root) for n in (64, 32)]
    root._compass_icon = images  # PhotoImages must outlive this call
    root.iconphoto(True, *images)


def card(parent, padding: int = 16, **pack):
    """A white card with a hairline border; returns the inner ttk frame."""
    import tkinter as tk
    from tkinter import ttk

    outer = tk.Frame(parent, bg=BORDER, highlightthickness=0, bd=0)
    if pack:
        outer.pack(**pack)
    inner = ttk.Frame(outer, style="Card.TFrame", padding=padding)
    inner.pack(fill="both", expand=True, padx=1, pady=1)
    inner.outer = outer
    return inner


def pill(parent, text: str, kind: str = "info", bg: str = CARD):
    """A small rounded-looking status badge."""
    import tkinter as tk

    fg, soft = STATUS_COLORS.get(kind, STATUS_COLORS["info"])
    return tk.Label(parent, text=f" {text} ", bg=soft, fg=fg, font=(FONT, 9, "bold"), padx=6, pady=1, bd=0)


def set_pill(label, text: str, kind: str) -> None:
    fg, soft = STATUS_COLORS.get(kind, STATUS_COLORS["info"])
    label.configure(text=f" {text} ", bg=soft, fg=fg)


class Segmented:
    """A pill-shaped segmented switch; command(value) runs on click, set(value) only repaints."""

    def __init__(self, parent, options, command, track: str = "#F1F5F9", active_bg: str = CARD):
        import tkinter as tk

        self.track, self.active_bg = track, active_bg
        self.frame = tk.Frame(parent, bg=track, padx=2, pady=2)
        self.buttons = {}
        self.value = None
        for value, label in options:
            button = tk.Label(self.frame, text=label, font=(FONT, 9, "bold"), padx=10, pady=3, cursor="hand2",
                              bg=track, fg=MUTED)
            button.pack(side="left")
            button.bind("<Button-1>", lambda e, v=value: command(v))
            self.buttons[value] = button

    def set(self, value) -> None:
        self.value = value
        for key, button in self.buttons.items():
            active = key == value
            button.configure(bg=self.active_bg if active else self.track, fg=ACCENT if active else MUTED)


def dpi_aware() -> None:
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
