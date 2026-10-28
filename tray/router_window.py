"""AgentCompass main panel: decision compass, quota and live workers, routing settings, integrations.

Runs as its own pythonw process (open_window), like the network and update windows, so the tray thread never
waits on it and closing the window frees the GUI memory. All reads and actions run on worker threads.
The first two pages only watch; everything that changes how the router chooses lives on the settings page.
"""
from __future__ import annotations

from i18n import translate as _tr, LocalizedSequence, tr

import contextlib
import ctypes
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import plugins
import radar_chart
import ui_theme as theme
import i18n

TITLE = "AgentCompass"
LEGACY_TITLE = "Route Manager · 控制台"  # a panel started before the rename
_process = None
PAGES = (("scoring", "决策罗盘", "◎"), ("quota", "额度监控", "▤"), ("policy", "调度设置", "⚙"), ("plugins", "外部集成", "▦"))
PAGES = LocalizedSequence(PAGES)
SETTINGS = (("accounts", "账号模型"), ("boost", "优先加权"), ("tuning", "策略权重"), ("dna", "模型画像"), ("system", "系统选项"))
SETTINGS = LocalizedSequence(SETTINGS)
DNA_KEYS = (("divergence", "发散创新"), ("global_rigor", "全局严密"), ("module_precision", "模块精准"))
DNA_KEYS = LocalizedSequence(DNA_KEYS)
TRACK = "#E2E8F0"  # segmented switch track on the page background


def open_window(page: str | None = None) -> None:
    """Raise the panel if it is open, else start it in its own process; never blocks the caller."""
    global _process
    import win32con
    import win32gui

    window = win32gui.FindWindow(None, TITLE) or win32gui.FindWindow(None, LEGACY_TITLE)
    if window:
        win32gui.ShowWindow(window, win32con.SW_RESTORE)
        try:
            win32gui.SetForegroundWindow(window)
        except Exception:
            win32gui.FlashWindow(window, True)
        return
    if _process is not None and _process.poll() is None:
        return
    python = Path(sys.executable).with_name("pythonw.exe")
    _process = subprocess.Popen([str(python), str(Path(__file__).resolve())] + (["--page", page] if page else []),
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                close_fds=True, creationflags=0x08000000)


def log_error(exc: BaseException) -> None:
    path = Path.home() / ".route-manager" / "router-window.log"
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


def tokens_text(value) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    return f"{value / 1_000_000:.2f}M" if value >= 100_000 else f"{value / 1000:.0f}k"


def bundle_labels(bundles: list[dict]) -> list[str]:
    """Display names for an account's bundles; a name shared by two bundles falls back to the bundle id."""
    counts = {}
    for b in bundles:
        counts[b["label"]] = counts.get(b["label"], 0) + 1
    return [b["label"] if counts[b["label"]] == 1 else b["id"] for b in bundles]


def short_badge(text: str) -> str:
    """'已暂停：disabled' → '已暂停' for the narrow quota cards; the settings page shows the reason."""
    return (text or "").split("：", 1)[0].split(": ", 1)[0]


def clip(text, limit: int = 22) -> str:
    text = str(text or "—")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def matching_preset(tuning: dict, presets: dict) -> str | None:
    """The strategy preset the saved tuning equals, if any."""
    for name, preset in presets.items():
        try:
            if all(abs(float(tuning.get(k)) - float(v)) < 1e-6 for k, v in preset.items()):
                return name
        except (TypeError, ValueError):
            continue
    return None


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


def follow_width(container, label, margin: int, floor: int) -> None:
    """Wrap label to container's width; only real width changes re-wrap, so a re-wrap cannot re-trigger itself."""
    last = [None]

    def changed(event):
        if event.width <= 1:
            return
        if last[0] is None or abs(event.width - last[0]) > 2:
            last[0] = event.width
            label.configure(wraplength=max(theme.px(floor), event.width - margin))
    container.bind("<Configure>", changed)


class ScrollFrame:
    """A vertically scrolling area; put children in .body."""

    def __init__(self, parent, bg: str | None = None):
        import tkinter as tk
        from tkinter import ttk

        bg = bg or theme.BG
        self.canvas = tk.Canvas(parent, bg=bg, highlightthickness=0, bd=0)
        self.bar = ttk.Scrollbar(parent, orient="vertical", command=self.canvas.yview)
        self.body = ttk.Frame(self.canvas, style="Card.TFrame" if bg == theme.CARD else "TFrame")
        self.window = self.canvas.create_window(0, 0, anchor="nw", window=self.body)
        self.canvas.configure(yscrollcommand=self.bar.set)
        self._last_w = self._last_bbox = None
        self.body.bind("<Configure>", self.body_changed)
        self.canvas.bind("<Configure>", self.canvas_changed)
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self.wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))
        self.canvas.pack(side="left", fill="both", expand=True)
        self.bar.pack(side="right", fill="y")

    # Each reconfigure repaints the canvas, so only pass real changes on (re-mapping sends <Configure> too).
    def body_changed(self, _event):
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
        if self.body.winfo_height() > self.canvas.winfo_height():
            self.canvas.yview_scroll(int(-event.delta / 120), "units")


class Panel:
    def __init__(self, root, page: str = "scoring"):
        import tkinter as tk
        from tkinter import ttk

        self.tk, self.ttk = tk, ttk
        self.root = root
        self.results = queue.Queue()
        self.busy_jobs = 0
        self.closing = False
        self.long_jobs: list[threading.Thread] = []
        theme.apply(root, TITLE, "1120x680" if i18n.is_en() else "1020x660", (880, 560))
        try:
            theme.set_window_icon(root)
        except Exception as exc:
            log_error(exc)
        self.mono = theme.mono_font(root)
        self.status = tk.StringVar(value=_tr("就绪"))
        self.plan_result = None
        self.ledger_rows = []
        self.breakdown_rows = []
        self.models = {}
        self.data = None
        self.auto_replayed = False
        self._generation = 0
        self._cards_cache = None
        root.bind("<<PanelResult>>", lambda e: self.drain_results())
        with freeze_redraw(self.root):
            self.build_shell(page)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._poll_id = root.after(80, self.poll)

    def build_shell(self, page):
        tk, ttk, root = self.tk, self.ttk, self.root

        shell = tk.Frame(root, bg=theme.BG)
        self.shell = shell
        shell.pack(fill="both", expand=True)
        self.build_sidebar(shell)
        main = tk.Frame(shell, bg=theme.BG)
        main.pack(side="left", fill="both", expand=True)
        footer = tk.Frame(main, bg=theme.CARD, highlightthickness=1, highlightbackground=theme.BORDER)
        footer.pack(side="bottom", fill="x")
        tk.Label(footer, textvariable=self.status, bg=theme.CARD, fg=theme.MUTED, font=(theme.FONT, 9),
                 anchor="w").pack(side="left", padx=12, pady=3)
        self.content = ttk.Frame(main, padding=(18, 14, 18, 12))
        self.content.pack(fill="both", expand=True)
        self.content.pack_propagate(False)
        self.pages = {}
        for key, _, _ in PAGES:
            self.pages[key] = ttk.Frame(self.content)  # mapped only while shown, see show()
        self.built = set()
        self.show(page if page in self.pages else "scoring")

    def change_language(self, lang):
        """Rebuild display widgets; preserve worker ownership and discard stale UI callbacks."""
        lang = self.data_module().save_ui_language(lang)
        page, setting = self.current, getattr(self, "current_setting", "system")
        self._generation += 1
        with freeze_redraw(self.root):
            self.shell.destroy()
            # These caches refer to destroyed widgets, not the underlying account/model data.
            for name in ("quota_shape", "account_shape", "dna_picker", "dna_labels", "_cards_cache"):
                self.__dict__.pop(name, None)
            self._dna_redraw_pending = False
            self._initial_setting = setting
            self.build_shell(page)
        self.status.set(tr("已切换界面语言为简体中文", "Language switched to English"))

    # ---------- plumbing ----------

    def run(self, work, done=None, message: str | None = None, long: bool = False):
        """Run work() on a thread; done(result) runs on the UI thread. Errors land in the status bar."""
        if self.closing:
            return
        if message:
            self.status.set(message)
        self.busy_jobs += 1
        generation = self._generation

        def runner():
            try:
                self.results.put((generation, done, work(), None))
            except Exception as exc:
                log_error(exc)
                self.results.put((generation, done, None, exc))
            try:
                if not self.closing:
                    self.root.event_generate("<<PanelResult>>", when="tail")
            except Exception:
                pass
        thread = threading.Thread(target=runner, daemon=not long)
        if long:
            self.long_jobs.append(thread)
        thread.start()

    def drain_results(self):
        if self.closing:
            return
        try:
            while True:
                generation, done, value, error = self.results.get_nowait()
                self.busy_jobs -= 1
                if generation != self._generation:
                    continue
                if error is not None:
                    self.status.set(_tr('操作失败：{0}').format(f'{error}'))
                    continue
                try:
                    if done:
                        done(value)
                    elif isinstance(value, str) and value:
                        self.status.set(value)
                except Exception as exc:
                    log_error(exc)
                    self.status.set(_tr('界面更新失败：{0}').format(f'{exc}'))
        except queue.Empty:
            pass

    def poll(self):
        if self.closing:
            if not any(t.is_alive() for t in self.long_jobs):
                os._exit(0)
            self._poll_id = self.root.after(30, self.poll)
            return
        self.drain_results()
        self._poll_id = self.root.after(30, self.poll)

    def close(self):
        self.closing = True
        if getattr(self, "_poll_id", None) is not None:
            try:
                self.root.after_cancel(self._poll_id)
            except Exception:
                pass
            self._poll_id = None
        try:
            self.root.withdraw()
            self.root.update_idletasks()
        except Exception:
            pass
        # An update owns child installers; keep the process until it finishes, without the window.
        if not any(t.is_alive() for t in self.long_jobs):
            os._exit(0)
        self.drain_results()
        self._poll_id = self.root.after(30, self.poll)

    def data_module(self):
        if self.data is None:
            import router_data

            self.data = router_data
        return self.data

    def cached_account_cards(self, force: bool = False):
        cached = getattr(self, "_cards_cache", None)
        now = time.monotonic()
        if not force and cached is not None and now - cached[0] < 1.5:
            return cached[1]
        cards = self.data_module().account_cards()
        self._cards_cache = (now, cards)
        return cards

    def prefill(self, work, done):
        """Run a fast local read (a few ms) and fill a page while it is still unmapped, so it never shows empty first."""
        try:
            value = work()
        except Exception as exc:
            log_error(exc)
            self.status.set(_tr('读取失败：{0}').format(f'{exc}'))
            return
        done(value)

    def say(self, then=None):
        """A done() callback that shows the message and optionally runs then()."""
        def done(message):
            self.status.set(message or _tr("完成"))
            if then:
                then()
        return done

    # ---------- shell ----------

    def build_sidebar(self, shell):
        tk = self.tk
        k = theme.px
        side = tk.Frame(shell, bg=theme.INK, width=k(198 if i18n.is_en() else 168))
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        brand = tk.Frame(side, bg=theme.INK)
        brand.pack(fill="x", padx=14, pady=(16, 18))
        logo = tk.Canvas(brand, width=k(26), height=k(26), bg=theme.INK, highlightthickness=0)
        logo.pack(side="left")
        try:
            from PIL import ImageTk

            # The same compass as the tray icon, anti-aliased by PIL (Tk canvas shapes are not on Windows).
            logo.image = ImageTk.PhotoImage(theme.compass_image(k(26), tile=False), master=self.root)
            logo.create_image(0, 0, anchor="nw", image=logo.image)
        except Exception as exc:
            log_error(exc)
        tk.Label(brand, text="AgentCompass", bg=theme.INK, fg="#FFFFFF",
                 font=(theme.FONT, 11, "bold")).pack(side="left", padx=8)
        self.nav = {}
        for key, label, glyph in PAGES:
            # Glyphs differ in advance width, so each sits centred in a fixed cell and every label starts at one x.
            row = tk.Frame(side, bg=theme.INK, cursor="hand2")
            row.pack(fill="x", padx=8, pady=1)
            row.columnconfigure(0, minsize=k(30))
            icon = tk.Label(row, text=glyph, anchor="center", bg=theme.INK, fg="#CBD5E1", font=(theme.FONT, 10),
                            pady=7, cursor="hand2")
            icon.grid(row=0, column=0, sticky="ew", padx=(6, 0))
            text = tk.Label(row, text=label, anchor="w", bg=theme.INK, fg="#CBD5E1", font=(theme.FONT, 10),
                            pady=7, cursor="hand2")
            text.grid(row=0, column=1, sticky="w", padx=(6, 0))
            for widget in (row, icon, text):
                widget.bind("<Button-1>", lambda e, k=key: self.show(k))
                widget.bind("<Enter>", lambda e, k=key: self.hover_nav(k, e))
                widget.bind("<Leave>", lambda e, k=key: self.hover_nav(k, e))
            self.nav[key] = (row, icon, text)
        self.current = None

    def paint_nav(self, key, bg, fg, bold=False):
        row, icon, text = self.nav[key]
        row.configure(bg=bg)
        icon.configure(bg=bg, fg=fg)
        text.configure(bg=bg, fg=fg, font=(theme.FONT, 10, "bold" if bold else "normal"))

    def hover_nav(self, key, event):
        if key == self.current:
            return
        # Moving between the row and its labels sends Leave/Enter pairs; judge by what is under the pointer.
        try:
            under = self.root.winfo_containing(event.x_root, event.y_root)
        except KeyError:  # a Tk-internal window such as a combobox list
            under = None
        inside = under is not None and under in self.nav[key]
        self.paint_nav(key, "#1E293B" if inside else theme.INK, "#CBD5E1")

    def show(self, key):
        """Map one page and unmap the last; inactive pages stay unmapped so their native widgets never repaint."""
        if key == self.current:
            return
        already_built = key in self.built
        if key not in self.built:
            self.built.add(key)
            getattr(self, f"build_{key}")(self.pages[key])  # built while unmapped: it appears complete
        previous, self.current = self.current, key
        for name in self.nav:
            active = name == key
            self.paint_nav(name, theme.ACCENT if active else theme.INK, "#FFFFFF" if active else "#CBD5E1", active)
        with freeze_redraw(self.content):
            if previous is not None:
                self.pages[previous].pack_forget()
            self.pages[key].pack(fill="both", expand=True)
        if key == 'plugins' and already_built:
            self.load_plugins()

    def header(self, parent, title, actions=()):
        ttk = self.ttk
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(0, 10))
        ttk.Label(bar, text=title, style="Page.TLabel").pack(side="left")
        for label, command, style in reversed(actions):
            ttk.Button(bar, text=label, command=command, style=style or "Small.TButton").pack(side="right", padx=(6, 0))
        return bar

    def tree(self, parent, columns, height=8):
        ttk = self.ttk
        frame = ttk.Frame(parent, style="Card.TFrame")
        table = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", height=height,
                             selectmode="browse", style="Slim.Treeview")
        for key, title, width, anchor in columns:
            table.heading(key, text=title, anchor=anchor)
            table.column(key, width=theme.px(width), minwidth=theme.px(36), anchor=anchor,
                         stretch=key == columns[-1][0] or width > 180)
        bar = ttk.Scrollbar(frame, orient="vertical", command=table.yview)
        table.configure(yscrollcommand=bar.set)
        table.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        table.tag_configure("muted", foreground=theme.FAINT)
        table.tag_configure("win", foreground=theme.ACCENT)
        table.tag_configure("warn", foreground="#B45309")
        table.tag_configure("danger", foreground=theme.RED)
        return frame, table

    def divider(self, parent, **grid):
        line = self.tk.Frame(parent, bg=theme.BORDER, height=1)
        line.grid(sticky="ew", **grid)
        return line

    # ---------- page 1: decision compass ----------

    def build_scoring(self, page):
        tk, ttk = self.tk, self.ttk
        bar = self.header(page, _tr("决策罗盘"))
        self.mode = tk.StringVar(value="score")
        self.mode_switch = theme.Segmented(bar, (("score", _tr("单次评分")), ("dna", _tr("模型画像"))), self.set_mode,
                                           track=TRACK)
        self.mode_switch.frame.pack(side="right")
        body = ttk.Frame(page)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1, uniform="split")
        body.columnconfigure(1, weight=1, uniform="split")
        body.rowconfigure(0, weight=1)

        radar_card = theme.card(body, padding=12)
        radar_card.outer.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        self.radar_title = ttk.Label(radar_card, text=radar_chart.axis_title(), style="CardH3.TLabel")
        self.radar_title.pack(anchor="w", pady=(0, 8))
        picks = ttk.Frame(radar_card, style="Card.TFrame")
        picks.pack(fill="x")
        tk.Label(picks, text="●", bg=theme.CARD, fg=theme.ACCENT, font=(theme.FONT, 10)).pack(side="left")
        self.pick_a = ttk.Combobox(picks, state="readonly", width=17)
        self.pick_a.pack(side="left", padx=(3, 10))
        tk.Label(picks, text="◌", bg=theme.CARD, fg=theme.CYAN, font=(theme.FONT, 10, "bold")).pack(side="left")
        self.pick_b = ttk.Combobox(picks, state="readonly", width=17)
        self.pick_b.pack(side="left", padx=(3, 0))
        self.pick_a.bind("<<ComboboxSelected>>", lambda e: self.render_radar())
        self.pick_b.bind("<<ComboboxSelected>>", lambda e: self.render_radar())
        self.tune_link = ttk.Button(picks, text=_tr("微调"), style="Tiny.TButton", command=self.open_dna_settings)
        self.verdict = tk.StringVar(value="")
        verdict = ttk.Label(radar_card, textvariable=self.verdict, style="CardSub.TLabel", justify="left")
        verdict.pack(side="bottom", fill="x", pady=(4, 0))
        follow_width(radar_card, verdict, 8, 200)
        self.radar = radar_chart.RadarChartCanvas(radar_card, size=theme.px(300), bg=theme.CARD)
        self.radar.pack(fill="both", expand=True, pady=(6, 0))

        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")
        sim = theme.card(right, padding=12, fill="x")
        self.preset = ttk.Combobox(sim, state="readonly", values=[p[0] for p in self.data_presets()])
        self.preset.set(_tr("典型场景…"))
        self.preset.pack(fill="x")
        self.preset.bind("<<ComboboxSelected>>", lambda e: self.apply_preset())
        self.objective = tk.Text(sim, height=3, width=30, wrap="word", font=(theme.FONT, 9), relief="flat", bd=0,
                                 bg="#F8FAFC", fg=theme.INK, highlightthickness=1, highlightbackground=theme.BORDER,
                                 highlightcolor=theme.ACCENT, padx=6, pady=4)
        self.objective.pack(fill="x", pady=8)
        options = ttk.Frame(sim, style="Card.TFrame")
        options.pack(fill="x")
        self.hours = tk.StringVar(value="2")
        ttk.Spinbox(options, from_=0.5, to=24, increment=0.5, textvariable=self.hours, width=4).pack(side="left")
        ttk.Label(options, text="h", style="Card.TLabel").pack(side="left", padx=(3, 10))
        self.local_only = tk.BooleanVar()
        self.needs_visual = tk.BooleanVar()
        ttk.Checkbutton(options, text=_tr("仅本机"), variable=self.local_only).pack(side="left")
        ttk.Checkbutton(options, text=_tr("需视觉"), variable=self.needs_visual).pack(side="left", padx=(8, 0))
        self.live_scorer = tk.BooleanVar()
        ttk.Checkbutton(options, text=_tr("小模型打分"), variable=self.live_scorer).pack(side="left", padx=(8, 0))
        self.simulate_button = ttk.Button(options, text=_tr("模拟评分"), style="SmallAccent.TButton", command=self.simulate)
        self.simulate_button.pack(side="right")

        lists = theme.card(right, padding=12, fill="both", expand=True, pady=(12, 0))
        top = ttk.Frame(lists, style="Card.TFrame")
        top.pack(fill="x", pady=(0, 8))
        self.list_switch = theme.Segmented(top, (("ledger", _tr("最近决策")), ("breakdown", _tr("候选明细"))), self.show_list)
        self.list_switch.frame.pack(side="left")
        ttk.Button(top, text=_tr("刷新"), style="Tiny.TButton", command=self.load_ledger).pack(side="right")
        stack = ttk.Frame(lists, style="Card.TFrame")
        self.list_stack = stack
        stack.pack_propagate(False)
        stack.pack(fill="both", expand=True)
        self.ledger_frame, self.ledger_table = self.tree(stack, (
            ("at", _tr("时间"), 76, "w"), ("kind", _tr("类型"), 104, "w"), ("choice", _tr("胜出"), 100, "w"), ("task", _tr("任务"), 120, "w")), 8)
        self.ledger_table.bind("<<TreeviewSelect>>", lambda e: self.replay_selected())
        self.breakdown_frame, self.breakdown = self.tree(stack, (
            ("id", _tr("候选"), 108, "w"), ("score", _tr("总分"), 48, "e"), ("fit", _tr("契合"), 44, "e"), ("ability", _tr("能力"), 44, "e"),
            ("opp", _tr("机会"), 50, "e"), ("net", _tr("加减"), 50, "e"), ("note", _tr("状态"), 44, "w")), 8)
        self.breakdown.bind("<<TreeviewSelect>>", lambda e: self.pick_from_breakdown())
        self.show_list("ledger")
        self.set_mode("score")
        self.prefill(lambda: self.data_module().model_table(), self.models_loaded)
        self.prefill(lambda: self.data_module().recent_ledger(30), self.ledger_loaded)
        if self.plan_result:
            self.plan_loaded(self.plan_result)

    def show_list(self, which):
        self.list_switch.set(which)
        with freeze_redraw(getattr(self, "list_stack", None)):
            for name, frame in (("ledger", self.ledger_frame), ("breakdown", self.breakdown_frame)):
                if name == which:
                    frame.pack(fill="both", expand=True)
                else:
                    frame.pack_forget()

    def data_presets(self):
        from router_data import PRESETS

        return PRESETS

    def apply_preset(self):
        name = self.preset.get()
        for label, objective, hours, local, visual in self.data_presets():
            if label == name:
                self.objective.delete("1.0", "end")
                self.objective.insert("1.0", objective)
                self.hours.set(f"{hours:g}")
                self.local_only.set(local)
                self.needs_visual.set(visual)

    def set_mode(self, mode):
        self.mode.set(mode)
        self.mode_switch.set(mode)
        if mode == "dna":
            if not self.models:
                try:
                    self.models = self.data_module().model_table()
                except Exception as exc:
                    log_error(exc)
            self.tune_link.pack(side="right")
        else:
            self.tune_link.pack_forget()
        self.fill_pickers()
        self.render_radar()

    def models_loaded(self, models):
        self.models = models or {}
        if "scoring" in self.built and self.mode.get() == "dna":
            self.fill_pickers()
            self.render_radar()
        if getattr(self, "dna_picker", None) is not None:
            self.fill_dna_models()

    def fill_pickers(self):
        self.pick_ids = {}
        if self.mode.get() == "dna":
            for m in self.models:
                label = self.data_module().model_label(m)
                self.pick_ids[m if label in self.pick_ids else label] = m
            labels = list(self.pick_ids)
            defaults = ["claude-opus-5-5", "gemini-4-argon-high"]
        else:
            rows = [r for r in self.data_module().candidate_rows(self.plan_result or {}) if not r["excluded"]]
            labels = [r["id"] for r in rows]
            pair = self.data_module().decision_pair(self.plan_result or {})
            defaults = [r["id"] for r in pair]
        self.pick_a.configure(values=labels)
        self.pick_b.configure(values=[_tr("（自动：次选模型）")] + labels if self.mode.get() == "score" else labels)
        for picker, wanted in ((self.pick_a, defaults[0] if defaults else None),
                               (self.pick_b, defaults[1] if len(defaults) > 1 else None)):
            match = next((label for label in labels if wanted and self.pick_ids.get(label, label) == wanted), "")
            picker.set(match)
        if self.mode.get() == "score" and not self.pick_b.get():
            self.pick_b.set(_tr("（自动：次选模型）"))

    def picked(self, picker):
        value = picker.get()
        if not value or value == _tr("（自动：次选模型）"):
            return None
        return getattr(self, "pick_ids", {}).get(value, value)

    def render_radar(self):
        data = self.data_module()
        self.radar_title.configure(text=radar_chart.axis_title(self.mode.get()))
        if self.mode.get() == "dna":
            if not self.models:
                self.radar.show((), [])
                return
            series, raw = data.dna_series(self.models, self.picked(self.pick_a), self.picked(self.pick_b))
            self.radar.show(radar_chart.DNA_AXES, series, raw)
            self.verdict.set(" vs ".join(s["label"] for s in series))
            return
        if not self.plan_result:
            self.radar.show((), [])
            self.verdict.set("")
            return
        pair = data.decision_pair(self.plan_result, self.picked(self.pick_a), self.picked(self.pick_b))
        series, raw = data.decision_series(pair)
        self.radar.show(radar_chart.SCORE_AXES, series, raw)
        self.verdict.set(self.verdict_text())

    def verdict_text(self):
        decision = (self.plan_result or {}).get("decision") or {}
        task = decision.get("task") or {}
        pair = self.data_module().decision_pair(self.plan_result or {})
        if not pair:
            return decision.get("reason") or _tr("没有可用候选")
        text = _tr('胜出 {0} {1}').format(f"{pair[0]['id']}", f"{fmt(pair[0]['score'])}")
        if len(pair) > 1:
            text += _tr(' · 次选 {0} {1} · 领先 {2}').format(f"{pair[1]['id']}", f"{fmt(pair[1]['score'])}", f"{fmt(decision.get('margin'), 4)}")
        text += f"\n{task.get('kind', '—')} · {task.get('difficulty', '—')} · {task.get('hours', '—')}h"
        reason = decision.get("reason")
        return text + (f" · {reason}" if reason else "")

    def open_dna_settings(self):
        model = self.picked(self.pick_a)
        self.show("policy")
        self.show_setting("dna")
        if model:
            self.select_dna_model(model)

    def simulate(self):
        objective = self.objective.get("1.0", "end").strip()
        if not objective:
            self.status.set(_tr("先输入任务简述或选择一个典型场景"))
            return
        try:
            hours = float(self.hours.get())
            if hours <= 0:
                raise ValueError
        except ValueError:
            self.status.set(_tr("预计工时需为正数"))
            return
        self.simulate_button.configure(state="disabled", text=_tr("评分中…"))
        local, visual, live = self.local_only.get(), self.needs_visual.get(), self.live_scorer.get()

        def work():
            try:
                return self.data_module().simulate(objective, hours, local, visual, live)
            except Exception as exc:
                log_error(exc)
                return exc

        def done(result):
            if isinstance(result, Exception):
                self.simulate_button.configure(state="normal", text=_tr("模拟评分"))
                self.status.set(_tr('模拟评分失败：{0}').format(f'{result}'))
                return
            self.plan_loaded(result)
            self.show_list("breakdown")
        self.run(work, done, _tr("正在用小模型打分并模拟路由…") if live else _tr("正在模拟路由评分…"))

    def replay_selected(self):
        if getattr(self, "_suppress_next_select", False):
            self._suppress_next_select = False
            return
        selected = self.ledger_table.selection()
        if not selected:
            return
        row = self.ledger_rows[int(selected[0])]
        self.run(lambda: self.data_module().replay(row), self.plan_loaded, _tr("正在按当前额度重放该次决策…"))

    def plan_loaded(self, result):
        self.simulate_button.configure(state="normal", text=_tr("模拟评分"))
        self.plan_result = result or {}
        data = self.data_module()
        rows = data.candidate_rows(self.plan_result)
        self.breakdown_rows = rows
        self.breakdown.delete(*self.breakdown.get_children())
        for index, row in enumerate(rows):
            excluded = row["excluded"]
            winner = row["id"] == self.plan_result.get("choice")
            note = _tr("拦截") if excluded else _tr("胜出") if winner else _tr("可选")
            self.breakdown.insert("", "end", iid=str(index), tags=("muted",) if excluded else ("win",) if winner else (),
                                  values=(row["id"], fmt(row["score"]), fmt(row["fit"], 2), fmt(row["ability"], 2),
                                          fmt(row["opportunity_cost"], 3), fmt(row["bonus"] - row["penalty"], 2, True),
                                          note))
        if self.mode.get() != "score":
            self.set_mode("score")
        else:
            self.fill_pickers()
            self.render_radar()
        message = _tr('评分完成：{0} 个候选，胜出 {1}').format(f'{len(rows)}', f"{self.plan_result.get('choice')}")
        scorer = self.plan_result.get("scorer") or {}
        if scorer:
            source = scorer.get("source")
            message += (_tr('（小模型 {0} · {1}ms）').format(f"{scorer.get('model')}", f"{scorer.get('duration_ms')}") if source == "system_one"
                        else _tr("（离线规则直判）") if source == "heuristic_configured"
                        else _tr('（小模型失败已回退规则：{0}）').format(f"{scorer.get('fallback_detail') or scorer.get('fallback_reason')}"))
        self.status.set(message)

    def pick_from_breakdown(self):
        selected = self.breakdown.selection()
        if not selected or not selected[0].isdigit():
            return
        row = self.breakdown_rows[int(selected[0])]
        if row["excluded"]:
            self.status.set(_tr('{0} 被拦截：{1}').format(f"{row['id']}", f"{'；'.join(row['excluded'])}"))
            return
        # Clicking an eligible row makes it the comparison series against the primary.
        if self.mode.get() == "score" and row["id"] in self.pick_a.cget("values") and row["id"] != self.picked(self.pick_a):
            self.pick_b.set(row["id"])
            self.render_radar()

    def load_ledger(self):
        self.run(lambda: self.data_module().recent_ledger(30), self.ledger_loaded)

    def ledger_loaded(self, rows):
        self.ledger_rows = rows or []
        self.ledger_table.delete(*self.ledger_table.get_children())
        for index, row in enumerate(self.ledger_rows):
            s = self.data_module().ledger_summary(row)
            self.ledger_table.insert("", "end", iid=str(index), values=(s["at"], s["kind"], s["choice"], s["objective"]))
        # Open on the latest real decision so the radar is never empty on first sight.
        if self.ledger_rows and not self.plan_result and not self.auto_replayed:
            self.auto_replayed = True
            self._suppress_next_select = True
            self.ledger_table.selection_set("0")
            self.prefill(lambda: self.data_module().replay(self.ledger_rows[0]), self.plan_loaded)

    # ---------- page 2: quota & workers ----------

    def build_quota(self, page):
        ttk = self.ttk
        self.header(page, _tr("额度监控"), ((_tr("刷新额度"), self.refresh_usage, None),
                                         (_tr("刷新"), lambda: self.load_quota(True), "SmallAccent.TButton")))

        self.quota_grid = ttk.Frame(page)
        self.quota_grid.pack(fill="x")
        for column in range(4):
            self.quota_grid.columnconfigure(column, weight=1, uniform="cards")
        workers = theme.card(page, padding=12, fill="both", expand=True, pady=(2, 0))
        ttk.Label(workers, text=_tr("在途与活跃会话"), style="CardH3.TLabel").pack(anchor="w", pady=(0, 6))
        frame, self.worker_table = self.tree(workers, (
            ("agent", "Agent", 70, "w"), ("title", _tr("任务"), 200, "w"), ("bundle", "Bundle", 110, "w"),
            ("state", _tr("状态"), 66, "w"), ("compacted", _tr("已压缩"), 62, "e"), ("context", _tr("窗口"), 58, "e"),
            ("total", _tr("累计"), 62, "e"), ("bar", _tr("占 2M 上限"), 130, "w")), 4)
        frame.pack(fill="both", expand=True)
        self.prefill(self.cached_account_cards, self.quota_cards_loaded)
        self.load_quota()

    def load_quota(self, force: bool = False):
        data = self.data_module()
        self.run(lambda: (self.cached_account_cards(force), data.workers()), self.quota_loaded, _tr("正在读取账号水位…"))

    def refresh_usage(self, accounts=None):
        def done(_msg):
            self._cards_cache = None
            self.load_quota(True)
        self.run(lambda: self.data_module().refresh_usage(accounts), self.say(lambda: done(None)), _tr("正在请求刷新额度…"))

    def quota_cards_loaded(self, cards):
        refreshable = self.data_module().USAGE_ACCOUNTS
        shape = tuple((card["account"], tuple(w["id"] for w in card["windows"])) for card in cards)
        if shape != getattr(self, "quota_shape", None):
            # First load, or an account / window appeared or went away: build the cards once.
            with freeze_redraw(self.quota_grid):
                for child in self.quota_grid.winfo_children():
                    child.destroy()
                self.quota_cards = [self.build_quota_card(index, card, card["account"] in refreshable)
                                    for index, card in enumerate(cards)]
                self.quota_shape = shape
        for widgets, card in zip(self.quota_cards, cards):
            self.fill_quota_card(widgets, card)

    def quota_loaded(self, value):
        cards, workers = value
        self.quota_cards_loaded(cards)
        self.worker_table.delete(*self.worker_table.get_children())
        for index, worker in enumerate(workers):
            share = (worker["historical_tokens"] or 0) / worker["limit"] if worker["limit"] else 0
            blocks = min(10, int(round(share * 10)))
            bar = "█" * blocks + "░" * (10 - blocks) + f" {share * 100:.0f}%"
            tag = (("danger",) if share >= 1 else ("warn",) if share >= 0.8
                   else ("muted",) if worker["state"] == _tr("近期待命") else ())
            self.worker_table.insert("", "end", iid=str(index), tags=tag, values=(
                (worker["agent_id"] or "")[:8], worker["title"], worker["bundle"], worker["state"],
                tokens_text(worker["compacted_tokens"]), tokens_text(worker["context_tokens"]),
                tokens_text(worker["historical_tokens"]), bar if worker["telemetry"] != "unknown" else _tr("遥测不可用")))
        if not workers:
            self.worker_table.insert("", "end", values=("", _tr("当前没有在途或活跃的 worker"), "", "", "", "", "", ""),
                                     tags=("muted",))
        running = sum(1 for w in workers if w["state"] != _tr("近期待命"))
        self.status.set(_tr('已读取 {0} 个账号、{1} 个在途 worker、{2} 个近期待命 · {3}').format(f'{len(cards)}', f'{running}', f'{len(workers) - running}', f"{time.strftime('%H:%M:%S')}"))

    def build_quota_card(self, index, card, refreshable):
        """One account card's fixed widgets; fill_quota_card writes the numbers, so a refresh destroys nothing."""
        tk, ttk = self.tk, self.ttk
        box = theme.card(self.quota_grid, padding=10)
        box.outer.grid(row=index // 4, column=index % 4, sticky="nsew", padx=(0 if index % 4 == 0 else 10, 0),
                       pady=(0, 10))
        top = ttk.Frame(box, style="Card.TFrame")
        top.pack(fill="x")
        ttk.Label(top, text=card["label"], style="CardH3.TLabel").pack(side="left")
        if refreshable:
            again = tk.Label(top, text="↻", bg=theme.CARD, fg=theme.MUTED, font=(theme.FONT, 10), cursor="hand2")
            again.bind("<Button-1>", lambda e, a=card["account"]: self.refresh_usage([a]))
            again.pack(side="right", padx=(4, 0))
        widgets = {"pill": theme.pill(top, "", "idle"), "plan": ttk.Label(box, style="CardTiny.TLabel"), "windows": []}
        widgets["pill"].pack(side="right")
        widgets["plan"].pack(anchor="w", pady=(0, 2))
        if not card["windows"]:
            ttk.Label(box, text=_tr("无计量窗口"), style="CardTiny.TLabel").pack(anchor="w", pady=(4, 0))
        for _ in card["windows"]:
            head = ttk.Frame(box, style="Card.TFrame")
            head.pack(fill="x", pady=(4, 0))
            name = ttk.Label(head, style="Card.TLabel", font=(theme.FONT, 9))
            name.pack(side="left")
            share = ttk.Label(head, style="Mono.TLabel")
            share.pack(side="right")
            bar = ttk.Progressbar(box, maximum=100)
            bar.pack(fill="x", pady=(2, 1))
            detail = ttk.Label(box, style="CardTiny.TLabel")
            detail.pack(anchor="w")
            widgets["windows"].append((name, share, bar, detail))
        return widgets

    @staticmethod
    def fill_quota_card(widgets, card):
        theme.set_pill(widgets["pill"], short_badge(card["badge"]), card["kind"])
        stale = "" if card["usage_status"] == "fresh" else _tr(' · 额度{0}').format(f"{(_tr('过期') if card['usage_status'] == 'stale' else _tr('未知'))}")
        widgets["plan"].configure(text=f"{clip(card.get('plan'))}{stale}")
        for (name, share, bar, detail), window in zip(widgets["windows"], card["windows"]):
            remaining = window["remaining_pct"]
            hours = window.get("hours")
            span = _tr("周") if hours == 168 else f"{hours:g}h" if isinstance(hours, (int, float)) else ""
            name.configure(text=f"{window['id']} {span}")
            share.configure(text="—" if remaining is None else f"{remaining:.0f}%")
            colour = "Green" if remaining is None or remaining >= 40 else "Amber" if remaining >= 15 else "Red"
            bar.configure(style=f"{colour}.Horizontal.TProgressbar", value=remaining or 0)
            parts = [_tr('{0}后重置').format(f"{window['reset_in']}")]
            if window["remaining_usd"] is not None:
                parts.append(f"≈${window['remaining_usd']:.0f}")
            rate = window["rate_3h"] if window["rate_3h"] is not None else window["rate_24h"]
            if rate is not None:
                parts.append(f"{rate:.2f}%/h")
            detail.configure(text=" · ".join(parts))

    # ---------- page 3: routing settings ----------

    def build_policy(self, page):
        ttk = self.ttk
        bar = self.header(page, _tr("调度设置"))
        self.setting_switch = theme.Segmented(page if i18n.is_en() else bar, SETTINGS, self.show_setting, track=TRACK)
        self.setting_switch.frame.pack(anchor="w", pady=(0, 10)) if i18n.is_en() else self.setting_switch.frame.pack(side="right")
        stack = ttk.Frame(page)
        self.policy_stack = stack
        stack.pack(fill="both", expand=True)
        stack.pack_propagate(False)
        self.setting_frames = {key: ttk.Frame(stack) for key, _ in SETTINGS}
        self.settings_built = set()
        self.current_setting = None
        self.show_setting(getattr(self, "_initial_setting", None) or "accounts")

    def show_setting(self, key):
        if "policy" not in self.built:
            self.show("policy")
        if key == self.current_setting:
            return
        self.setting_switch.set(key)
        if key not in self.settings_built:
            self.settings_built.add(key)
            getattr(self, f"build_setting_{key}")(self.setting_frames[key])
        with freeze_redraw(getattr(self, "policy_stack", None)):
            if self.current_setting is not None:
                self.setting_frames[self.current_setting].pack_forget()
            self.current_setting = key
            self.setting_frames[key].pack(fill="both", expand=True)

    def load_policy(self):
        """Reload every settings section that has been opened."""
        for key in getattr(self, "settings_built", ()):
            getattr(self, f"load_{key}")()

    # accounts & bundles

    def build_setting_accounts(self, frame):
        box = theme.card(frame, padding=(14, 6), fill="both", expand=True)
        scroll = ScrollFrame(box, bg=theme.CARD)
        self.account_rows = scroll.body
        self.account_rows.columnconfigure(3, weight=1)
        self.prefill(self.cached_account_cards, self.accounts_loaded)
        self.load_accounts()

    def load_accounts(self, force: bool = False):
        self.run(lambda: self.cached_account_cards(force), self.accounts_loaded, _tr("正在读取账号与档位…"))

    def accounts_loaded(self, cards):
        shape = tuple((card["account"], tuple(b["id"] for b in card["bundles"])) for card in cards)
        if shape != getattr(self, "account_shape", None):
            with freeze_redraw(self.account_rows):
                for child in self.account_rows.winfo_children():
                    child.destroy()
                self.account_widgets = [self.build_account_row(index, card) for index, card in enumerate(cards)]
                self.account_shape = shape
        for widgets, card in zip(self.account_widgets, cards):
            widgets["plan"].configure(text=clip(card.get("plan"), 26))
            theme.set_pill(widgets["pill"], clip(card["badge"], 16), card["kind"])
            widgets["on"].set(card["host_enabled"])
            for (var, check), bundle, label in zip(widgets["bundles"], card["bundles"], bundle_labels(card["bundles"])):
                var.set(bundle["enabled"])
                check.configure(text=label)
                check.state(["!disabled"] if card["host_enabled"] else ["disabled"])
        self.status.set(_tr('已读取 {0} 个账号 · {1}').format(f'{len(cards)}', f"{time.strftime('%H:%M:%S')}"))

    def build_account_row(self, index, card):
        """One account's fixed widgets; accounts_loaded sets their values, so a toggle rebuilds nothing."""
        tk, ttk = self.tk, self.ttk
        box = self.account_rows
        row = index * 2
        if index:
            self.divider(box, row=row - 1, column=0, columnspan=4, pady=2)
        names = ttk.Frame(box, style="Card.TFrame")
        names.grid(row=row, column=0, sticky="nw", pady=6)
        ttk.Label(names, text=card["label"], style="CardH3.TLabel").pack(anchor="w")
        plan = ttk.Label(names, style="CardTiny.TLabel")
        plan.pack(anchor="w")
        box.grid_columnconfigure(0, minsize=theme.px(130))
        pill = theme.pill(box, "", "idle")
        pill.grid(row=row, column=1, sticky="nw", padx=(0, 12), pady=8)
        box.grid_columnconfigure(1, minsize=theme.px(96))
        on = tk.BooleanVar(value=card["host_enabled"])
        ttk.Checkbutton(box, text=_tr("启用"), variable=on,
                        command=lambda a=card["account"]: self.toggle_account(a)).grid(
            row=row, column=2, sticky="nw", padx=(0, 16), pady=7)
        chips = ttk.Frame(box, style="Card.TFrame")
        chips.grid(row=row, column=3, sticky="nw", pady=7)
        bundles = []
        for slot, bundle in enumerate(card["bundles"]):
            var = tk.BooleanVar(value=bundle["enabled"])
            check = ttk.Checkbutton(chips, variable=var,
                                    command=lambda a=card["account"], b=bundle["id"]: self.toggle_bundle(a, b))
            check.grid(row=slot // 3, column=slot % 3, sticky="w", padx=(0, 14), pady=(0, 2))
            bundles.append((var, check))
        for column in range(3):
            chips.columnconfigure(column, minsize=theme.px(140))
        return {"plan": plan, "pill": pill, "on": on, "bundles": bundles}

    def after_account_change(self):
        self._cards_cache = None
        self.load_accounts(True)
        if "quota" in self.built:
            self.load_quota(True)

    def toggle_account(self, account):
        self.run(lambda: self.data_module().toggle_account_host(account), self.say(self.after_account_change))

    def toggle_bundle(self, account, bundle_id):
        self.run(lambda: self.data_module().toggle_bundle(account, bundle_id), self.say(self.after_account_change))

    # temporary priority

    def build_setting_boost(self, frame):
        tk, ttk = self.tk, self.ttk
        box = theme.card(frame, padding=14, fill="both", expand=True)
        quick = ttk.Frame(box, style="Card.TFrame")
        quick.pack(fill="x")
        ttk.Label(quick, text=_tr("一键优先"), style="CardH3.TLabel").pack(side="left", padx=(0, 10))
        import router_priority

        for account, model, label in router_priority.TOP_MODELS:
            ttk.Button(quick, text=label, style="Small.TButton",
                       command=lambda a=account, m=model: self.enable_priority(a, m)).pack(side="left", padx=(0, 6))
        form = ttk.Frame(box, style="Card.TFrame")
        form.pack(fill="x", pady=(12, 10))
        ttk.Label(form, text=_tr("自定义加成"), style="CardH3.TLabel").pack(side="left", padx=(0, 10))
        self.boost_target = ttk.Combobox(form, state="readonly", width=24)
        self.boost_target.pack(side="left")
        self.boost_value = tk.StringVar(value="0.20")
        ttk.Spinbox(form, from_=0.10, to=0.50, increment=0.05, textvariable=self.boost_value, width=5,
                    format="%.2f").pack(side="left", padx=6)
        self.boost_hours = ttk.Combobox(form, state="readonly", width=4, values=["3h", "6h", "24h"])
        self.boost_hours.set("6h")
        self.boost_hours.pack(side="left")
        self.boost_reset = tk.BooleanVar(value=True)
        ttk.Checkbutton(form, text=_tr("重置后撤销"), variable=self.boost_reset).pack(side="left", padx=10)
        ttk.Button(form, text=_tr("添加"), style="SmallAccent.TButton", command=self.add_boost).pack(side="left")
        bottom = ttk.Frame(box, style="Card.TFrame")
        bottom.pack(side="bottom", fill="x", pady=(8, 0))
        ttk.Button(bottom, text=_tr("取消所选规则"), style="Small.TButton", command=self.cancel_rule).pack(side="right")
        table, self.rule_table = self.tree(box, (("label", _tr("账号 / 模型"), 180, "w"), ("boost", _tr("加成"), 60, "e"),
                                                 ("until", _tr("最迟失效"), 120, "w"), ("reset", _tr("重置撤销"), 70, "w"),
                                                 ("intent", _tr("来源"), 160, "w")), 6)
        table.pack(fill="both", expand=True)
        data = self.data_module()
        self.prefill(lambda: (data.overrides(), data.boost_targets()), self.boost_loaded)

    def load_boost(self):
        data = self.data_module()
        self.run(lambda: (data.overrides(), data.boost_targets()), self.boost_loaded)

    def boost_loaded(self, value):
        rules, targets = value
        data = self.data_module()
        self.rule_rows = rules
        self.rule_table.delete(*self.rule_table.get_children())
        for index, rule in enumerate(rules):
            self.rule_table.insert("", "end", iid=str(index), values=(
                rule["label"], fmt(rule["boost"], 2, True) if rule["boost"] else "—", rule["until"],
                _tr("是") if rule["stop_on_reset"] else _tr("否"), rule["intent"]))
        if not rules:
            self.rule_table.insert("", "end", values=(_tr("当前没有生效规则"), "", "", "", ""), tags=("muted",))
        self.boost_pairs = [(a, None) for a in dict.fromkeys(a for a, _ in targets)] + targets
        self.boost_target.configure(values=[
            f"{data.ACCOUNT_LABELS.get(a, a)} / {data.model_label(m) if m else _tr('全部模型')}" for a, m in self.boost_pairs])
        if not self.boost_target.get() and self.boost_pairs:
            self.boost_target.current(0)

    def enable_priority(self, account, model):
        import router_priority

        def work():
            allocator, _ = router_priority.modules()
            accounts = allocator.load_accounts()
            target = next((t for t in router_priority.targets(allocator, accounts)
                           if t["account"] == account and t["model"] == model), None)
            if target is None:
                return _tr("该模型当前不可设为优先")
            return router_priority.perform({"kind": "enable", **{k: target[k] for k in ("account", "model", "window")}})
        self.run(work, self.say(self.load_boost))

    def add_boost(self):
        index = self.boost_target.current()
        if index < 0:
            self.status.set(_tr("先选择账号 / 模型"))
            return
        account, model = self.boost_pairs[index]
        try:
            boost = float(self.boost_value.get())
        except ValueError:
            self.status.set(_tr("加成需为数字"))
            return
        hours = int(self.boost_hours.get().rstrip("h"))
        reset = self.boost_reset.get()
        self.run(lambda: self.data_module().add_boost(account, model, boost, hours, reset), self.say(self.load_boost))

    def cancel_rule(self):
        selected = self.rule_table.selection()
        if not selected or not getattr(self, "rule_rows", None) or not selected[0].isdigit():
            self.status.set(_tr("先选择一条规则"))
            return
        rule = self.rule_rows[int(selected[0])]
        import router_priority

        self.run(lambda: router_priority.cancel(rule["id"], rule["revision"]), self.say(self.load_boost))

    # strategy & parameters

    def build_setting_tuning(self, frame):
        tk, ttk = self.tk, self.ttk
        from router_data import STRATEGY_PRESETS, TUNING_DEFAULTS

        box = theme.card(frame, padding=14, fill="x")
        presets = ttk.Frame(box, style="Card.TFrame")
        presets.pack(fill="x")
        ttk.Label(presets, text=_tr("策略预设"), style="CardH3.TLabel").pack(side="left", padx=(0, 10))
        self.preset_buttons = {}
        for name in STRATEGY_PRESETS:
            button = ttk.Button(presets, text=_tr(name), style="Small.TButton",
                                command=lambda n=name: self.apply_strategy_preset(n))
            button.pack(side="left", padx=(0, 6))
            self.preset_buttons[name] = button
        tk.Frame(box, bg=theme.BORDER, height=1).pack(fill="x", pady=12)
        cols = ttk.Frame(box, style="Card.TFrame")
        cols.pack(fill="x")
        cols.columnconfigure(0, weight=1, uniform="tune")
        cols.columnconfigure(1, weight=1, uniform="tune")

        weights = ttk.Frame(cols, style="Card.TFrame")
        weights.grid(row=0, column=0, sticky="nsew", padx=(0, 20))
        ttk.Label(weights, text=_tr("合成权重（和为 1）"), style="CardH3.TLabel").grid(row=0, column=0, columnspan=3,
                                                                          sticky="w", pady=(0, 6))
        self.tuning_vars = {}
        self.weight_scales = {}
        for row, (key, label) in enumerate((("subjective.weight", _tr("主观倾向")), ("subjective.vector_weight", _tr("能力向量")),
                                            ("subjective.route_weight", _tr("路由顺位"))), start=1):
            ttk.Label(weights, text=label, style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=5)
            s_var = tk.DoubleVar(value=TUNING_DEFAULTS[key])
            ttk.Scale(weights, from_=0.0, to=1.0, variable=s_var, style="Card.Horizontal.TScale",
                      command=lambda _v, k=key: self.on_weight_slide(k)).grid(row=row, column=1, sticky="ew", padx=10)
            t_var = tk.StringVar(value=f"{TUNING_DEFAULTS[key]:.2f}")
            ttk.Label(weights, textvariable=t_var, style="Mono.TLabel", width=5).grid(row=row, column=2, sticky="e")
            self.weight_scales[key] = s_var
            self.tuning_vars[key] = t_var
        weights.columnconfigure(1, weight=1)

        numbers = ttk.Frame(cols, style="Card.TFrame")
        numbers.grid(row=0, column=1, sticky="nsew")
        ttk.Label(numbers, text=_tr("额度阈值"), style="CardH3.TLabel").grid(row=0, column=0, columnspan=2, sticky="w",
                                                                    pady=(0, 6))
        for row, (key, label) in enumerate((("safety_margin_pct", _tr("周安全余量 %")), ("short_window_floor_pct", _tr("短窗底线 %")),
                                            ("reset_horizon_hours", _tr("临期消纳窗口 h")), ("reset_bonus_max", _tr("临期最大加成")),
                                            ("rotate_after_carried_tokens", _tr("上下文轮换阈值"))), start=1):
            ttk.Label(numbers, text=label, style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=2)
            var = tk.StringVar()
            ttk.Entry(numbers, textvariable=var, width=11, font=(self.mono, 9)).grid(row=row, column=1, sticky="e", pady=2)
            self.tuning_vars[key] = var
        numbers.columnconfigure(0, weight=1)

        buttons = ttk.Frame(box, style="Card.TFrame")
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(buttons, text=_tr("保存参数"), style="SmallAccent.TButton", command=self.save_tuning).pack(side="left")
        ttk.Button(buttons, text=_tr("恢复默认"), style="Small.TButton", command=self.restore_tuning).pack(side="left", padx=8)
        self.prefill(lambda: self.data_module().read_tuning(), self.tuning_loaded)

    def load_tuning(self):
        self.run(lambda: self.data_module().read_tuning(), self.tuning_loaded)

    def tuning_loaded(self, tuning):
        from router_data import STRATEGY_PRESETS

        self._syncing_weights = True
        try:
            for key, var in self.tuning_vars.items():
                val = tuning.get(key)
                if key in self.weight_scales and val is not None:
                    self.weight_scales[key].set(float(val))
                    var.set(f"{float(val):.2f}")
                else:
                    var.set("" if val is None else str(val))
        finally:
            self._syncing_weights = False
        active = matching_preset(tuning, STRATEGY_PRESETS)
        for name, button in self.preset_buttons.items():
            button.configure(style="SmallAccent.TButton" if name == active else "Small.TButton")

    def on_weight_slide(self, changed_key):
        if getattr(self, "_syncing_weights", False):
            return
        current = {k: float(self.tuning_vars[k].get() or 0) for k in self.weight_scales}
        balanced = self.data_module().normalize_weights(changed_key, self.weight_scales[changed_key].get(), current)
        self._syncing_weights = True
        try:
            for k, val in balanced.items():
                self.weight_scales[k].set(val)
                self.tuning_vars[k].set(f"{val:.2f}")
        finally:
            self._syncing_weights = False

    def apply_strategy_preset(self, name):
        from router_data import STRATEGY_PRESETS

        name = self.data_module().strategy_preset_key(name)
        preset = STRATEGY_PRESETS.get(name)
        if not preset:
            return
        self.run(lambda: _tr('已应用「{0}」：{1}').format(_tr(name), self.data_module().save_tuning(preset)), self.say(self.load_tuning))

    def save_tuning(self):
        values = {key: var.get().strip() for key, var in self.tuning_vars.items() if var.get().strip()}
        self.run(lambda: self.data_module().save_tuning(values), self.say(self.load_tuning))

    def restore_tuning(self):
        from tkinter import messagebox

        if messagebox.askyesno(TITLE, _tr("恢复默认路由参数？"), parent=self.root):
            self.run(lambda: self.data_module().restore_tuning(), self.say(self.load_tuning))

    # model DNA

    def build_setting_dna(self, frame):
        tk, ttk = self.tk, self.ttk
        box = theme.card(frame, padding=14, fill="both", expand=True)
        box.columnconfigure(1, weight=1)
        box.rowconfigure(0, weight=1)
        left = ttk.Frame(box, style="Card.TFrame")
        left.grid(row=0, column=0, sticky="nsw", padx=(0, 16))
        ttk.Label(left, text=_tr("模型"), style="CardH3.TLabel").grid(row=0, column=0, sticky="w")
        self.dna_picker = ttk.Combobox(left, state="readonly", width=26)
        self.dna_picker.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(4, 14))
        self.dna_picker.bind("<<ComboboxSelected>>", lambda e: self.dna_model_changed())
        self.dna_model = None
        self.dna_draft = {}
        self.dna_vars, self.dna_texts = {}, {}
        for row, (key, label) in enumerate(DNA_KEYS, start=2):
            ttk.Label(left, text=label, style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=6)
            var = tk.DoubleVar(value=0.5)
            ttk.Scale(left, from_=0.0, to=1.0, variable=var, length=theme.px(150), style="Card.Horizontal.TScale",
                      command=lambda _v, k=key: self.on_dna_slide(k)).grid(row=row, column=1, sticky="ew", padx=10)
            text = tk.StringVar(value="0.50")
            ttk.Label(left, textvariable=text, style="Mono.TLabel", width=5).grid(row=row, column=2, sticky="e")
            self.dna_vars[key], self.dna_texts[key] = var, text
        buttons = ttk.Frame(left, style="Card.TFrame")
        buttons.grid(row=5, column=0, columnspan=3, sticky="w", pady=(14, 0))
        ttk.Button(buttons, text=_tr("保存画像"), style="SmallAccent.TButton", command=self.save_dna).pack(side="left")
        ttk.Button(buttons, text=_tr("还原"), style="Small.TButton", command=self.revert_dna).pack(side="left", padx=8)
        self.dna_radar = radar_chart.RadarChartCanvas(box, size=theme.px(280), bg=theme.CARD)
        self.dna_radar.grid(row=0, column=1, sticky="nsew")
        if self.models:
            self.fill_dna_models()
        else:
            self.prefill(lambda: self.data_module().model_table(), self.models_loaded)

    def load_dna(self):
        if self.models:
            self.fill_dna_models()
        self.run(lambda: self.data_module().model_table(), self.models_loaded)

    def fill_dna_models(self):
        data = self.data_module()
        self.dna_labels = {f"{data.model_label(m)} · {m}": m for m in self.models}
        self.dna_picker.configure(values=list(self.dna_labels))
        wanted = self.dna_model if self.dna_model in self.models else next(iter(self.models), None)
        if wanted:
            self.select_dna_model(wanted)

    def select_dna_model(self, model):
        if not getattr(self, "dna_labels", None) or model not in self.models:
            self.dna_model = model
            return
        label = next(label for label, m in self.dna_labels.items() if m == model)
        self.dna_picker.set(label)
        self.dna_model_changed()

    def saved_dna(self, model):
        subj = (self.models.get(model) or {}).get("subjective") or {}
        return {key: round(float(subj.get(key, 0.5)), 2) for key, _ in DNA_KEYS}

    def dna_model_changed(self):
        self.dna_model = self.dna_labels.get(self.dna_picker.get())
        self.dna_draft = self.saved_dna(self.dna_model) if self.dna_model else {}
        self.sync_dna_sliders()

    def sync_dna_sliders(self):
        self._syncing_dna = True
        try:
            for key, _ in DNA_KEYS:
                value = self.dna_draft.get(key, 0.5)
                self.dna_vars[key].set(value)
                self.dna_texts[key].set(f"{value:.2f}")
        finally:
            self._syncing_dna = False
        self.render_dna_preview()

    def on_dna_slide(self, key):
        if getattr(self, "_syncing_dna", False) or not self.dna_model:
            return
        value = round(float(self.dna_vars[key].get()), 2)
        self.dna_draft[key] = value
        self.dna_texts[key].set(f"{value:.2f}")
        if not getattr(self, "_dna_redraw_pending", False):
            self._dna_redraw_pending = True
            generation = self._generation
            self.root.after_idle(lambda: self.render_dna_preview() if generation == self._generation else None)

    def render_dna_preview(self):
        self._dna_redraw_pending = False
        if self.closing:
            return
        model = self.dna_model
        if not model or model not in self.models:
            self.dna_radar.show((), [])
            return
        info = self.models[model]
        label = self.data_module().model_label(model)
        draft = {**info, "subjective": dict(self.dna_draft)}
        series = [{"label": label, "values": radar_chart.dna_dimensions(draft)}]
        raw = [radar_chart.dna_raw(draft)]
        if self.dna_draft != self.saved_dna(model):
            series[0]["label"] = _tr('{0} · 调整后').format(f'{label}')
            series.append({"label": _tr("已保存"), "values": radar_chart.dna_dimensions(info)})
            raw.append(radar_chart.dna_raw(info))
        self.dna_radar.show(radar_chart.DNA_AXES, series, raw)

    def revert_dna(self):
        if self.dna_model:
            self.dna_draft = self.saved_dna(self.dna_model)
            self.sync_dna_sliders()

    def save_dna(self):
        model = self.dna_model
        if not model:
            return
        draft = dict(self.dna_draft)

        def done(message):
            self.status.set(message)
            self.models[model]["subjective"] = {**(self.models[model].get("subjective") or {}), **draft}
            self.render_dna_preview()
            if "scoring" in self.built and self.mode.get() == "dna":
                self.render_radar()
        self.run(lambda: self.data_module().save_model_subjective(
            model, draft["divergence"], draft["global_rigor"], draft["module_precision"]), done)

    # system

    def build_setting_system(self, frame):
        tk, ttk = self.tk, self.ttk
        box = theme.card(frame, padding=14, fill="x")
        box.columnconfigure(1, weight=1)
        rows = ((_tr("开机自启"), _tr("AgentCompass 随 Windows 启动")), (_tr("宿主 Skill"), _tr("Claude / Codex 的 route-agent-work 链接")),
                (_tr("Dot 中转库"), _tr("GitHub 仓库 owner/name")))
        for row, (title, hint) in enumerate(rows):
            if row:
                self.divider(box, row=row * 2 - 1, column=0, columnspan=3, pady=8)
            names = ttk.Frame(box, style="Card.TFrame")
            names.grid(row=row * 2, column=0, sticky="w")
            ttk.Label(names, text=title, style="CardH3.TLabel").pack(anchor="w")
            ttk.Label(names, text=hint, style="CardTiny.TLabel").pack(anchor="w")
        self.autostart_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text=_tr("开启"), variable=self.autostart_var,
                        command=self.toggle_tray_autostart).grid(row=0, column=2, sticky="e")
        ttk.Button(box, text=_tr("同步链接"), style="Small.TButton", command=self.sync_host_skills).grid(row=2, column=2, sticky="e")
        repo = ttk.Frame(box, style="Card.TFrame")
        repo.grid(row=4, column=1, columnspan=2, sticky="e")
        self.dot_repo_var = tk.StringVar()
        ttk.Entry(repo, textvariable=self.dot_repo_var, width=28, font=(self.mono, 9)).pack(side="left")
        ttk.Button(repo, text=_tr("保存"), style="Small.TButton", command=self.save_dot_repo).pack(side="left", padx=(6, 0))
        self.divider(box, row=5, column=0, columnspan=3, pady=8)
        ttk.Label(box, text=tr("界面语言", "Language"), style="CardH3.TLabel").grid(row=6, column=0, sticky="w")
        self.language_box = ttk.Combobox(box, state="readonly", values=("简体中文", "English"), width=18)
        self.language_box.set("English" if i18n.is_en() else "简体中文")
        self.language_box.grid(row=6, column=2, sticky="e")
        self.language_box.bind("<<ComboboxSelected>>",
                               lambda event: self.change_language("en" if self.language_box.get() == "English" else "zh"))
        self.build_scorer_card(frame)
        self.prefill(self._read_system_fast, self._system_fast_loaded)
        self.load_system()

    def build_scorer_card(self, frame):
        tk, ttk = self.tk, self.ttk
        data = self.data_module()
        box = theme.card(frame, padding=14, fill="x", pady=(12, 0))
        box.columnconfigure(1, weight=1)
        head = ttk.Frame(box, style="Card.TFrame")
        head.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        ttk.Label(head, text=_tr("路由分类小模型（System-One Scorer）"), style="CardH3.TLabel").pack(anchor="w")
        ttk.Label(head, text=_tr("route_judge 用它给任务分类打分；失败时回退到 Gemini Flash，再回退到离线规则"),
                  style="CardTiny.TLabel", wraplength=theme.px(630), justify="left").pack(anchor="w")
        self.scorer_types = data.SCORER_TYPES
        self.scorer_config = {}
        self.scorer_type_box = ttk.Combobox(box, state="readonly", width=38, values=[row[1] for row in self.scorer_types])
        self.scorer_key_var, self.scorer_url_var, self.scorer_model_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.scorer_key_entry = ttk.Entry(box, textvariable=self.scorer_key_var, show="•", font=(self.mono, 9))
        self.scorer_key_toggle = ttk.Button(box, text=_tr("显示"), style="Small.TButton", width=5, command=self.toggle_scorer_key)
        self.scorer_url_entry = ttk.Entry(box, textvariable=self.scorer_url_var, font=(self.mono, 9))
        self.scorer_model_entry = ttk.Entry(box, textvariable=self.scorer_model_var, font=(self.mono, 9))
        fields = ((_tr("小模型类型"), self.scorer_type_box), (_tr("API Key（Jev / 自定义）"), self.scorer_key_entry),
                  (_tr("接口地址（可选）"), self.scorer_url_entry), (_tr("模型名（可选）"), self.scorer_model_entry))
        for row, (title, widget) in enumerate(fields, start=1):
            ttk.Label(box, text=title, style="Card.TLabel").grid(row=row, column=0, sticky="w", padx=(0, 12), pady=3)
            widget.grid(row=row, column=1, sticky="ew", pady=3)
        self.scorer_key_toggle.grid(row=2, column=2, sticky="e", padx=(6, 0))
        self.scorer_type_box.bind("<<ComboboxSelected>>", lambda e: self.scorer_type_changed())
        bottom = ttk.Frame(box, style="Card.TFrame")
        bottom.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.scorer_hint = ttk.Label(bottom, text="", style="CardTiny.TLabel", wraplength=theme.px(420), justify="left")
        self.scorer_hint.pack(side="left", fill="x", expand=True)
        ttk.Button(bottom, text=_tr("保存小模型配置"), style="SmallAccent.TButton", command=self.save_scorer).pack(side="right")
        self.scorer_test_button = ttk.Button(bottom, text=_tr("测试打分"), style="Small.TButton", command=self.test_scorer)
        self.scorer_test_button.pack(side="right", padx=(0, 8))
        self.scorer_result = ttk.Label(box, text="", style="CardTiny.TLabel", wraplength=theme.px(560), justify="left")
        self.scorer_result.grid(row=6, column=0, columnspan=3, sticky="w", pady=(6, 0))

    def selected_scorer(self):
        label = self.scorer_type_box.get()
        return next((row for row in self.scorer_types if row[1] == label), self.scorer_types[0])

    def scorer_type_changed(self):
        type_id, _, needs_key, takes_url, hint = self.selected_scorer()
        field = (self.scorer_config.get("fields") or {}).get(type_id) or {}
        self.scorer_key_var.set(field.get("api_key_masked") or "")
        self.scorer_url_var.set(field.get("base_url") or "")
        self.scorer_model_var.set(field.get("model") or "")
        self.scorer_key_entry.configure(state="normal" if needs_key else "disabled")
        self.scorer_key_toggle.configure(state="normal" if needs_key else "disabled")
        for entry in (self.scorer_url_entry, self.scorer_model_entry):
            entry.configure(state="normal" if takes_url else "disabled")
        notes = [hint]
        if field.get("key_hint"):
            notes.append(field["key_hint"])
        if self.scorer_config.get("type_from_env"):
            notes.append(_tr('注意：环境变量 {0} 覆盖了此处的选择').format(f"{self.scorer_config['type_from_env']}"))
        self.scorer_hint.configure(text="；".join(notes))

    def scorer_loaded(self, config):
        self.scorer_config = config or {}
        type_id = self.scorer_config.get("scorer_type")
        label = next((row[1] for row in self.scorer_types if row[0] == type_id), self.scorer_types[0][1])
        self.scorer_type_box.set(label)
        self.scorer_type_changed()
        if self.scorer_config.get("config_error"):
            self.scorer_result.configure(text=_tr('配置有误：{0}').format(f"{self.scorer_config['config_error']}"))

    def toggle_scorer_key(self):
        hidden = self.scorer_key_entry.cget("show") != ""
        self.scorer_key_entry.configure(show="" if hidden else "•")
        self.scorer_key_toggle.configure(text=_tr("隐藏") if hidden else _tr("显示"))

    def scorer_form(self):
        type_id, _, needs_key, takes_url, _ = self.selected_scorer()
        return (type_id, self.scorer_key_var.get() if needs_key else None,
                self.scorer_url_var.get() if takes_url else "", self.scorer_model_var.get() if takes_url else "")

    def save_scorer(self):
        type_id, key, url, model = self.scorer_form()
        data = self.data_module()

        def work():
            message = data.save_scorer_config(type_id, key, url, model)
            return message, data.read_scorer_config()

        def done(value):
            message, config = value
            self.scorer_loaded(config)
            self.scorer_result.configure(text=message)
            self.status.set(message)
        self.run(work, done, _tr("正在保存小模型配置…"))

    def test_scorer(self):
        type_id, key, url, model = self.scorer_form()
        self.scorer_test_button.configure(state="disabled", text=_tr("测试中…"))
        self.scorer_result.configure(text=_tr("正在用示例任务测试打分…"))

        def work():
            try:
                return self.data_module().test_scorer_config(type_id, key, url or None, model or None)
            except Exception as exc:
                return _tr('测试失败：{0}').format(f'{exc}')

        def done(message):
            self.scorer_test_button.configure(state="normal", text=_tr("测试打分"))
            self.scorer_result.configure(text=message)
            self.status.set(message)
        self.run(work, done)

    def _read_scorer_safe(self):
        try:
            return self.data_module().read_scorer_config()
        except Exception as exc:
            log_error(exc)
            return {"config_error": _tr('读取失败：{0}').format(f'{exc}')}

    def _read_system_fast(self):
        return self.data_module().dot_details(), self._read_scorer_safe()

    def _system_fast_loaded(self, value):
        dot, scorer = value
        self.dot_repo_var.set(dot.get("transfer_repo") or "")
        self.scorer_loaded(scorer)

    def load_system(self):
        import deploy

        self.run(lambda: (deploy.startup_enabled(), *self._read_system_fast()), self.system_loaded)

    def system_loaded(self, value):
        startup_on, dot, scorer = value
        self.autostart_var.set(bool(startup_on))
        self._system_fast_loaded((dot, scorer))

    def toggle_tray_autostart(self):
        import deploy

        on = bool(self.autostart_var.get())

        def work():
            if on:
                deploy.write_startup()
                return _tr("已开启 AgentCompass 开机自启")
            deploy.remove_startup()
            return _tr("已关闭 AgentCompass 开机自启")
        self.run(work, self.say())

    def sync_host_skills(self):
        import deploy

        self.run(lambda: "；".join(deploy.link_skills() or [_tr("宿主 Skill 链接均已就绪")]), self.say())

    def save_dot_repo(self):
        repo = self.dot_repo_var.get()
        self.run(lambda: self.data_module().save_dot_repo(repo), self.say())

    # ---------- page 4: integrations ----------

    def build_plugins(self, page):
        tk, ttk = self.tk, self.ttk
        self.header(page, _tr("外部集成"), ((_tr("更新进度"), self.open_updates, None), (_tr("检查更新"), self.update_components, None),
                                       (_tr("重启服务"), self.restart_services, None),
                                       (_tr("刷新"), self.load_plugins, "SmallAccent.TButton")))

        scroll = ScrollFrame(page)
        grid = scroll.body
        grid.columnconfigure(0, weight=1, uniform="plugins")
        grid.columnconfigure(1, weight=1, uniform="plugins")
        config = plugins.load()
        self.plugin_widgets = {}
        for index, plugin in enumerate(plugins.PLUGINS):
            box = theme.card(grid, padding=0)
            box.outer.grid(row=index // 2, column=index % 2, sticky="nsew", padx=(0 if index % 2 == 0 else 12, 0),
                           pady=(0, 12))
            tk.Frame(box, bg=plugin.color, width=4).pack(side="left", fill="y")
            inner = ttk.Frame(box, style="Card.TFrame", padding=(12, 10))
            inner.pack(side="left", fill="both", expand=True)
            top = ttk.Frame(inner, style="Card.TFrame")
            top.pack(fill="x")
            ttk.Label(top, text=plugin.name, style="CardH3.TLabel").pack(side="left")
            ttk.Label(top, text=plugin.description, style="CardTiny.TLabel").pack(side="left", padx=(8, 0))
            status = theme.pill(top, _tr("检测中"), "idle")
            status.pack(side="right")
            switches = ttk.Frame(inner, style="Card.TFrame")
            switches.pack(fill="x", pady=(6, 0))
            enabled = tk.BooleanVar(value=config[plugin.id]["enabled"])
            autostart = tk.BooleanVar(value=config[plugin.id]["autostart"])
            if plugin.id != "grok-bot":  # CLI account enablement belongs to routing settings, not a daemon toggle.
                ttk.Checkbutton(switches, text=_tr("启用"), variable=enabled,
                                command=lambda p=plugin.id, v=enabled: self.toggle_enabled(p, v.get())).pack(side="left")
                ttk.Checkbutton(switches, text=_tr("随托盘自启"), variable=autostart,
                                command=lambda p=plugin.id, v=autostart: self.toggle_autostart(p, v.get())).pack(
                    side="left", padx=12)
            info = tk.StringVar(value="")
            info_label = ttk.Label(inner, textvariable=info, style="CardTiny.TLabel")
            info_label.pack(fill="x", pady=(4, 0))
            follow_width(inner, info_label, 24, 160)
            actions = ttk.Frame(inner, style="Card.TFrame")
            actions.pack(fill="x", pady=(8, 0))
            for label, work in self.plugin_actions(plugin.id):
                ttk.Button(actions, text=label, style="Tiny.TButton",
                           command=lambda w=work, p=plugin.id: self.plugin_act(p, w)).pack(side="left", padx=(0, 4))
            if plugin.id == "dot2api":
                ttk.Button(actions, text=_tr("复制 URL"), style="Tiny.TButton", command=self.copy_tunnel).pack(side="left")
            elif plugin.id == "grok2api":
                ttk.Button(actions, text=_tr("复制管理员密码"), style="Tiny.TButton",
                           command=self.copy_grok_admin).pack(side="left")
            elif plugin.id == "muse2api":
                ttk.Button(actions, text=_tr("复制 API Key"), style="Tiny.TButton",
                           command=self.copy_muse_key).pack(side="left")
            if plugin.id in ("grok2api", "muse2api"):
                ttk.Button(actions, text=_tr("登录导入账号"), style="Tiny.TButton",
                           command=lambda p=plugin.id: self.import_account(p)).pack(side="left", padx=(4, 0))
            self.plugin_widgets[plugin.id] = {"status": status, "info": info, "enabled": enabled, "autostart": autostart}
        self.prefill(self._prefill_plugins_info, lambda _v: None)
        self.load_plugins()

    def _prefill_plugins_info(self):
        for plugin in plugins.PLUGINS:
            widgets = self.plugin_widgets.get(plugin.id)
            if not widgets:
                continue
            deployed = (plugins.plugin_root(plugin.id) / ".git").exists()
            if plugin.id == "grok-bot":
                import grok_bot_cli
                widgets["info"].set(grok_bot_cli.info())
            elif plugin.id == "dot2api":
                dot = self.data_module().dot_details()
                self.tunnel_url = dot.get("tunnel_url") or ""
                widgets["info"].set(self.tunnel_url or _tr("隧道未发布"))
            elif plugin.id == "easytier":
                widgets["info"].set(_tr("正在读取对端…"))
            elif plugin.id in ("grok2api", "muse2api") and deployed:
                import gateways

                if plugin.id == "grok2api":
                    user = gateways.grok_admin()[0]
                    widgets["info"].set(_tr('{0}/v1 · 管理员 {1} · 账号与客户端 Key 在管理台添加').format(f'{gateways.GROK_URL}', f"{user or _tr('已改密')}"))
                else:
                    chrome = _tr("Chrome 已找到") if gateways.CHROME.is_file() else _tr("缺 Chrome")
                    widgets["info"].set(_tr('{0}/v1 · {1} · muse.ai cookie 在管理台导入').format(f'{gateways.MUSE_URL}', f'{chrome}'))
            else:
                widgets["info"].set(f"~/.route-manager/plugins/{plugin.id}" if deployed else _tr("未下载 · 启用后自动部署"))

    def plugin_actions(self, plugin_id):
        import services

        if plugin_id == "grok-bot":
            import grok_bot_cli
            return ((_tr("安装/修复"), grok_bot_cli.install), (_tr("检查连接"), grok_bot_cli.check),
                    (_tr("使用说明"), grok_bot_cli.guide))

        state = Path.home() / ".route-manager"

        def log(path):
            def open_log():
                if not Path(path).exists():
                    return _tr('日志不存在：{0}').format(f'{path}')
                os.startfile(path)
                return _tr('已打开日志 {0}').format(f'{Path(path).name}')
            return open_log
        start = lambda: services.plugin_start(plugin_id)
        stop = lambda: services.plugin_stop(plugin_id)
        if plugin_id == "dot2api":
            import dot_reconnect

            return ((_tr("启动"), start), (_tr("停止"), stop), (_tr("重连"), lambda: dot_reconnect.reconnect() or _tr("隧道地址未变化，无需重连")),
                    (_tr("日志"), log(Path.home() / ".dot2api" / "serve.log")))
        if plugin_id in ("grok2api", "muse2api"):
            import gateways

            grok = plugin_id == "grok2api"
            console = gateways.GROK_URL if grok else f"{gateways.MUSE_URL}/dashboard"
            return ((_tr("管理台"), lambda: gateways.open_console(console)), (_tr("启动"), start), (_tr("停止"), stop),
                    (_tr("日志"), log(gateways.GROK_LOG if grok else gateways.MUSE_LOG)))
        if plugin_id == "easytier":
            import easytier
            import network_status

            return ((_tr("局域网"), lambda: network_status.open_window() or _tr("已打开局域网状态")), (_tr("启动"), start),
                    (_tr("停止"), stop), (_tr("重启"), easytier.restart))
        if plugin_id == "chatgpt-web":
            return ((_tr("打开"), lambda: services.start_chatgpt_web(True)), (_tr("启动"), start), (_tr("停止"), stop),
                    (_tr("重启"), lambda: f"{services.stop_chatgpt_web()}；{services.start_chatgpt_web(False)}"),
                    (_tr("日志"), log(state / "chatgpt-web.log")))
        if plugin_id == "gemini-web":
            return ((_tr("打开"), lambda: services.start_gemini_web(True)), (_tr("停止"), stop),
                    (_tr("日志"), log(state / "gemini-web.log")))
        if plugin_id == "usage-float":
            return ((_tr("悬浮窗"), lambda: services.component_action("usage-float", "show")), (_tr("启动"), start),
                    (_tr("停止"), stop), (_tr("刷新"), lambda: services.component_action("usage-float", "refresh")),
                    (_tr("设置"), lambda: services.component_action("usage-float", "settings")),
                    (_tr("日志"), log(state / "usage-float.log")))
        return ()

    def plugin_act(self, plugin_id, work):
        if plugin_id == 'grok-bot':
            import grok_bot_cli
            if work is grok_bot_cli.check:
                self.check_grok_bot()
                return
        self.run(work, self.say(self.load_plugins), _tr("正在执行…"), long=plugin_id == "grok-bot")

    def check_grok_bot(self):
        if self.closing or self.current != 'plugins' or getattr(self, '_grok_probe_busy', False):
            return
        import grok_bot_cli
        self._grok_probe_busy = True

        def work():
            try:
                return grok_bot_cli.check()
            finally:
                self._grok_probe_busy = False

        def done(message):
            if not self.closing and self.current == 'plugins':
                self.status.set(message)
                self.load_plugins(probe=False)
        # Let an in-flight bounded read finish on close, as with existing long actions.
        self.run(work, done, _tr('正在检查 Grok Bot 连接…'), long=True)

    def toggle_enabled(self, plugin_id, value):
        import services

        def work():
            plugins.set_flag(plugin_id, "enabled", value)
            name = plugins.BY_ID[plugin_id].name
            if not value:
                try:
                    return _tr('已停用 {0}；{1}').format(f'{name}', f'{services.plugin_stop(plugin_id)}')
                except Exception as exc:
                    return _tr('已停用 {0}（停止时出错：{1}）').format(f'{name}', f'{exc}')
            if plugins.autostarts(plugin_id) and plugin_id != "gemini-web":
                return _tr('已启用 {0}；{1}').format(f'{name}', f'{services.plugin_start(plugin_id)}')
            return _tr('已启用 {0}').format(f'{name}')
        if value and plugin_id in ("grok2api", "muse2api"):
            # First enable downloads and builds it (minutes for Grok2API): show it in the update window.
            import update_status
            import updates

            plugins.set_flag(plugin_id, "enabled", True)
            try:
                update_status.open_window()
            except Exception as exc:
                log_error(exc)
            self.run(lambda: _tr('已启用 {0}；').format(f'{plugins.BY_ID[plugin_id].name}') + updates.deploy_gateway(
                plugin_id, plugins.autostarts(plugin_id), progress=update_status.record),
                self.say(self.load_plugins), _tr("正在下载并构建…（关闭窗口不会中断）"), long=True)
            return
        self.run(work, self.say(self.load_plugins))

    def toggle_autostart(self, plugin_id, value):
        self.run(lambda: plugins.set_flag(plugin_id, "autostart", value) and
                 _tr('{0} 随托盘自启：{1}').format(f'{plugins.BY_ID[plugin_id].name}', f"{(_tr('开') if value else _tr('关'))}"))

    def load_plugins(self, probe=True):
        import services

        if probe:
            self._grok_auto_check_pending = True
        self._plugins_load_generation = getattr(self, '_plugins_load_generation', 0) + 1
        generation = self._plugins_load_generation

        def work():
            import easytier

            def label(plugin_id):
                try:
                    if plugin_id == "grok-bot":
                        import grok_bot_cli
                        reading = grok_bot_cli.snapshot()
                        return reading['label'], reading
                    if plugin_id == "easytier":
                        reading = easytier.snapshot()
                        return easytier.label(reading), len(easytier.remote_peers(reading.get("peers")))
                    return services.plugin_label(plugin_id), None
                except Exception as exc:
                    return _tr('状态异常：{0}').format(f'{exc}')[:30], None
            config = plugins.load()
            found = {}
            threads = []
            for pid in plugins.BY_ID:
                t = threading.Thread(target=lambda p=pid: found.__setitem__(p, label(p)), daemon=True)
                t.start()
                threads.append(t)
            for t in threads:
                t.join()
            labels = {plugin_id: value[0] for plugin_id, value in found.items()}
            data = self.data_module()
            extras = {"dot2api": data.dot_details(), "easytier": found["easytier"][1],
                      "grok-bot": found["grok-bot"][1]}
            extras["dot2api"]["subscription"] = data.subscription_text()
            return config, labels, extras
        def done(value):
            if generation == self._plugins_load_generation:
                self.plugins_loaded(value)
        self.run(work, done, _tr("正在读取集成状态…"))

    def plugins_loaded(self, value):
        config, labels, extras = value
        for plugin in plugins.PLUGINS:
            widgets = self.plugin_widgets[plugin.id]
            row = config[plugin.id]
            if plugin.id == "grok-bot":
                import grok_bot_cli
                reading = extras.get(plugin.id) or {}
                theme.set_pill(widgets["status"], labels[plugin.id], reading.get('kind', 'idle'))
                widgets["info"].set(grok_bot_cli.info())
                if getattr(self, '_grok_auto_check_pending', False):
                    self._grok_auto_check_pending = False
                    if reading.get('installed'):
                        self.check_grok_bot()
                continue
            label = labels[plugin.id] if row["enabled"] else _tr('未启用 · {0}').format(f'{labels[plugin.id]}')
            theme.set_pill(widgets["status"], label[:18], theme.status_kind(labels[plugin.id]) if row["enabled"] else "idle")
            widgets["enabled"].set(row["enabled"])
            widgets["autostart"].set(row["autostart"])
            if plugin.id == "dot2api":
                dot = extras["dot2api"]
                self.tunnel_url = dot.get("tunnel_url") or ""
                widgets["info"].set(f"{self.tunnel_url or _tr('隧道未发布')} · {dot.get('subscription')}")
            elif plugin.id == "easytier":
                peers = extras.get("easytier")
                widgets["info"].set(_tr("对端读取失败") if peers is None else _tr('可见对端 {0} 个').format(f'{peers}'))
            elif plugin.id in ("grok2api", "muse2api") and (plugins.plugin_root(plugin.id) / ".git").exists():
                import gateways

                if plugin.id == "grok2api":
                    user = gateways.grok_admin()[0]
                    widgets["info"].set(_tr('{0}/v1 · 管理员 {1} · 账号与客户端 Key 在管理台添加').format(f'{gateways.GROK_URL}', f"{user or _tr('已改密')}"))
                else:
                    chrome = _tr("Chrome 已找到") if gateways.CHROME.is_file() else _tr("缺 Chrome")
                    widgets["info"].set(_tr('{0}/v1 · {1} · muse.ai cookie 在管理台导入').format(f'{gateways.MUSE_URL}', f'{chrome}'))
            else:
                deployed = (plugins.plugin_root(plugin.id) / ".git").exists()
                widgets["info"].set(f"~/.route-manager/plugins/{plugin.id}" if deployed else _tr("未下载 · 启用后自动部署"))
        self.status.set(_tr('集成状态已更新 · {0}').format(f"{time.strftime('%H:%M:%S')}"))

    def copy_tunnel(self):
        url = getattr(self, "tunnel_url", "")
        if not url:
            self.status.set(_tr("还没有隧道 URL"))
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(url)
        self.status.set(_tr('已复制 {0}').format(f'{url}'))

    def copy_secret(self, value, label):
        if not value:
            self.status.set(_tr('还没有{0}').format(f'{label}'))
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(value)
        self.status.set(_tr('已复制{0}').format(f'{label}'))

    def import_account(self, plugin_id, password=None):
        """Sign in to Grok / muse.ai in a dedicated Chrome; the cookie is then imported through the gateway's API."""
        import account_login
        import services

        def work():
            ready = services.grok_ready if plugin_id == "grok2api" else services.muse_ready
            if not ready():
                services.plugin_start(plugin_id)
                deadline = time.monotonic() + 60
                while not ready():
                    if time.monotonic() >= deadline:
                        return _tr('{0} 没有启动，未导入，详见日志').format(plugins.BY_ID[plugin_id].name)
                    time.sleep(1)
            try:
                return account_login.login_and_import(plugin_id, password)
            except account_login.PasswordNeeded:
                return account_login.PasswordNeeded

        def done(result):
            if result is account_login.PasswordNeeded:
                from tkinter import simpledialog

                entered = simpledialog.askstring("Grok2API", _tr("管理员密码已修改，请输入当前密码："), show="*",
                                                 parent=self.root)
                if entered:
                    self.import_account(plugin_id, entered)
                else:
                    self.status.set(_tr("已取消导入"))
                return
            self.say(self.load_plugins)(result)
        self.run(work, done, _tr("已打开登录窗口：在里面登录，完成后自动导入（10 分钟内有效）…"), long=True)

    def copy_grok_admin(self):
        import gateways

        self.copy_secret(gateways.grok_admin()[1], _tr(" Grok2API 管理员密码"))

    def copy_muse_key(self):
        import gateways

        self.copy_secret(gateways.muse_api_key(), _tr(" Muse2API API Key（首次启动后才生成）"))

    def restart_services(self):
        import services

        self.run(services.restart_services, self.say(self.load_plugins), _tr("正在重启已启用服务…"))

    def update_components(self):
        import update_status
        import updates

        try:
            update_status.open_window()
        except Exception as exc:
            log_error(exc)
        self.run(lambda: updates.update_all(progress=update_status.record), self.say(self.load_plugins),
                 _tr("正在检查并更新组件…（关闭窗口不会中断）"), long=True)

    def open_updates(self):
        import update_status

        update_status.open_window()


def main() -> None:
    import tkinter as tk

    theme.dpi_aware()
    page = sys.argv[sys.argv.index("--page") + 1] if "--page" in sys.argv[:-1] else "scoring"
    root = tk.Tk()
    root.withdraw()
    try:
        Panel(root, page)
        root.update_idletasks()
        root.deiconify()
    except Exception as exc:
        log_error(exc)
        raise
    root.mainloop()


if __name__ == "__main__":
    main()
