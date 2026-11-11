"""Helpers and widgets shared by the pages of the AgentCompass panel (router_window)."""
from __future__ import annotations

from i18n import LocalizedSequence

import contextlib
import ctypes
import sys
import time
import traceback
from pathlib import Path

import ui_theme as theme


TITLE = "AgentCompass"
SETTINGS = (("models", "模型与加权"), ("strategy", "额度阈值"), ("system", "系统选项"))
SETTINGS = LocalizedSequence(SETTINGS)
TRACK = "#E2E8F0"  # segmented switch track on the page background


def log_error(exc: BaseException) -> None:
    path = Path.home() / ".agent-compass" / "router-window.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(time.strftime("[%Y-%m-%d %H:%M:%S]\n"))
        traceback.print_exception(exc, file=stream)


def fmt(value, digits: int = 3, sign: bool = False) -> str:
    if value is None:
        return "—"
    if isinstance(value, (int, float)):
        return f"{value:+.{digits}f}" if sign else f"{value:.{digits}f}"
    return str(value)


def short_badge(text: str) -> str:
    """Show only the status on quota cards and settings rows."""
    return (text or "").split("：", 1)[0].split(": ", 1)[0]


def clip(text, limit: int = 22) -> str:
    text = str(text or "—")
    return text if len(text) <= limit else text[:limit - 1] + "…"


@contextlib.contextmanager
def freeze_redraw(widget):
    """Suspend Win32 painting on widget while child HWNDs are unmapped/mapped, then repaint once without erasing."""
    hwnd = 0
    user32 = None
    try:
        if sys.platform == "win32" and widget is not None and widget.winfo_ismapped():
            user32 = ctypes.windll.user32
            hwnd = int(widget.winfo_id())
            if hwnd:
                user32.SendMessageW(hwnd, 0x000B, 0, 0)
    except Exception:
        hwnd = 0
    try:
        yield
        if hwnd:
            widget.update_idletasks()
    finally:
        if hwnd and user32 is not None:
            try:
                user32.SendMessageW(hwnd, 0x000B, 1, 0)
                user32.RedrawWindow(hwnd, None, None, 0x0181)
            except Exception:
                pass


class ScrollFrame:
    """A vertically scrolling area; put children in .body.

    fill=True stretches .body to the visible height, so expand=True children still fill a tall window and only
    scroll once the content outgrows it; call fit() when a child's requested height may have changed."""

    def __init__(self, parent, bg: str | None = None, fill: bool = False):
        import tkinter as tk
        from tkinter import ttk

        bg = bg or theme.BG
        self.canvas = tk.Canvas(parent, bg=bg, highlightthickness=0, bd=0)
        self.bar = ttk.Scrollbar(parent, orient="vertical", command=self.canvas.yview)
        self.body = ttk.Frame(self.canvas, style="Card.TFrame" if bg == theme.CARD else "TFrame")
        self.window = self.canvas.create_window(0, 0, anchor="nw", window=self.body)
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.fill = fill
        self._last_w = self._last_bbox = self._last_h = None
        self.body.bind("<Configure>", self.body_changed)
        self.canvas.bind("<Configure>", self.canvas_changed)
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self.wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))
        self.canvas.pack(side="left", fill="both", expand=True)
        self.bar.pack(side="right", fill="y")

    # Each reconfigure repaints the canvas, so only pass real changes on (re-mapping sends <Configure> too).
    def fit(self, _event=None):
        if not self.fill:
            return
        height = max(self.body.winfo_reqheight(), self.canvas.winfo_height())
        if height > 1 and height != self._last_h:
            self._last_h = height
            self.canvas.itemconfigure(self.window, height=height)

    def body_changed(self, _event):
        self.fit()
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        bh = self.body.winfo_reqheight()
        bbox = self.canvas.bbox("all") if cw <= 1 and ch <= 1 else (0, 0, max(1, cw), max(bh, ch))
        if bbox != self._last_bbox:
            self._last_bbox = bbox
            self.canvas.configure(scrollregion=bbox)
        if self.body.winfo_height() <= self.canvas.winfo_height() and self.canvas.yview()[0] != 0.0:
            self.canvas.yview_moveto(0.0)

    def canvas_changed(self, event):
        if event.width > 1 and event.width != self._last_w:
            self._last_w = event.width
            self.canvas.itemconfigure(self.window, width=event.width)
        self.body_changed(event)

    def wheel(self, event):
        if hasattr(event.widget, "winfo_class") and event.widget.winfo_class() == "Treeview":
            return  # a table scrolls its own rows
        if self.body.winfo_height() > self.canvas.winfo_height():
            self.canvas.yview_scroll(int(-event.delta / 120), "units")
