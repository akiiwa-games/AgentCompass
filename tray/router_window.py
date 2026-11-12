"""AgentCompass main panel: decision compass, quota and live workers, routing settings, integrations.

Runs as its own pythonw process, like the network and update windows, so the tray thread never waits on it.
The tray starts that process hidden (prewarm) so its pages are built before the first open; closing the window
only hides it, and open_window() shows it again. All reads and actions run on worker threads.
The first two pages only watch; everything that changes how the router chooses lives on the settings page.
This module holds the process plumbing and the window shell; each page and settings tab is a panel_* mixin.
"""
from __future__ import annotations

from i18n import translate as _tr, LocalizedSequence, tr

import ctypes
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import component_launch
import page_stack
import ui_theme as theme
import i18n
from panel_scoring import ScoringPage
from panel_quota import QuotaPage
from panel_policy import PolicyPage
from panel_models import ModelsTab
from panel_strategy import StrategyTab
from panel_system import SystemTab
from panel_plugins import PluginsPage
from panel_widgets import SETTINGS, TITLE, freeze_redraw
import panel_widgets

_process = None
SHOW_EVENT = "Local\\AgentCompassWindowShow"  # created by the window process; open_window() sets it to show it
PAGES = (("scoring", "决策罗盘", "◎"), ("quota", "额度监控", "▤"), ("policy", "模型设置", "⚙"), ("plugins", "外部集成", "▦"))
PAGES = LocalizedSequence(PAGES)


def _kernel32():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenEventW.restype = kernel.CreateEventW.restype = kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.OpenEventW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
    kernel.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.SetEvent.argtypes = kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    return kernel


def warm_window_running() -> bool:
    kernel = _kernel32()
    handle = kernel.OpenEventW(0x00100000, False, SHOW_EVENT)  # SYNCHRONIZE
    if handle:
        kernel.CloseHandle(handle)
    return bool(handle)


def signal_warm_window() -> bool:
    """Ask the running window process to show itself; False when there is none."""
    kernel = _kernel32()
    handle = kernel.OpenEventW(0x0002, False, SHOW_EVENT)  # EVENT_MODIFY_STATE
    if not handle:
        return False
    try:
        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY: let it come to the front
        return bool(kernel.SetEvent(handle))
    finally:
        kernel.CloseHandle(handle)


def _start(*args: str) -> None:
    global _process
    if _process is not None and _process.poll() is None:
        return
    _process = subprocess.Popen(component_launch.command(__file__, *args, "--parent", str(os.getpid())),
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                close_fds=True, creationflags=0x08000000)


def open_window(page: str | None = None) -> None:
    """Show the panel: the warm process if one runs, else start it; never blocks the caller."""
    import win32con
    import win32gui

    if signal_warm_window():
        return
    # A panel from before the warm window has no show event.
    window = win32gui.FindWindow("TkTopLevel", TITLE)
    if window and win32gui.IsWindowVisible(window):
        win32gui.ShowWindow(window, win32con.SW_RESTORE)
        try:
            win32gui.SetForegroundWindow(window)
        except Exception:
            win32gui.FlashWindow(window, True)
        return
    _start(*(["--page", page] if page else []))


def prewarm() -> None:
    """Start the panel hidden so its pages are built before the first open; the tray calls this once."""
    if not warm_window_running():
        _start("--hidden")


class Panel(ScoringPage, QuotaPage, PolicyPage, ModelsTab, StrategyTab, SystemTab, PluginsPage):
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
        self._startup_version = self._read_disk_version()
        self._version_checked_at = time.monotonic()
        self._version_superseded = False
        self._superseded_notified = False
        self.stale = set()  # built pages to read again when next raised, after the window was hidden
        root.bind("<<PanelResult>>", lambda e: self.drain_results())
        root.bind("<Button-1>", lambda e: self.close_model_menu(), add="+")
        with freeze_redraw(self.root):
            self.build_shell(page)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._poll_id = root.after(80, self.poll)
        self.prebuild = False  # start_prebuild(), once the window is on screen

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
        self.page_stack = page_stack.PageStack(self.content)
        self.pages = {}
        for key, _, _ in PAGES:
            self.pages[key] = ttk.Frame(self.content)  # stacked by page_stack on first show or by prebuild_next()
        self.built = set()
        self.show(page if page in self.pages else "scoring")

    def change_language(self, lang):
        """Rebuild display widgets; preserve worker ownership and discard stale UI callbacks."""
        lang = self.data_module().save_ui_language(lang)
        self.close_model_menu()
        cwd_dialog = getattr(self, "cwd_dialog", None)
        if cwd_dialog is not None and cwd_dialog.winfo_exists():
            cwd_dialog.destroy()
        page, setting = self.current, getattr(self, "current_setting", "system")
        dialog = getattr(self, "boost_dialog", None)
        if dialog is not None and dialog.winfo_exists():
            dialog.destroy()
        self._generation += 1
        with freeze_redraw(self.root):
            self.shell.destroy()
            # These caches refer to destroyed widgets, not the underlying account/model data.
            for name in ("quota_shape", "account_shape", "_cards_cache", "model_shape", "model_widgets", "model_inventory"):
                self.__dict__.pop(name, None)
            self._dna_redraw_pending = False
            self._initial_setting = setting
            self.build_shell(page)
        if self.prebuild:
            self.schedule_prebuild()
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
                panel_widgets.log_error(exc)
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
                self.trim_due = True  # a fill touches memory; give it back once the hidden window is idle
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
                    panel_widgets.log_error(exc)
                    self.status.set(_tr('界面更新失败：{0}').format(f'{exc}'))
        except queue.Empty:
            pass

    def _read_disk_version(self) -> str:
        try:
            import updates

            return str(updates.local_version() or "")
        except Exception:
            return ""

    def version_superseded(self) -> bool:
        if getattr(self, "_version_superseded", False):
            return True
        now = time.monotonic()
        if now - getattr(self, "_version_checked_at", 0.0) < 2.0:
            return False
        self._version_checked_at = now
        startup_ver = getattr(self, "_startup_version", "")
        if not startup_ver or startup_ver == "0.0.0":
            return False
        cur_ver = self._read_disk_version()
        if cur_ver and cur_ver != "0.0.0" and cur_ver != startup_ver:
            self._version_superseded = True
            return True
        return False

    def _release_show_event(self) -> None:
        if getattr(self, "show_event", None):
            try:
                _kernel32().CloseHandle(self.show_event)
            except Exception:
                pass
            self.show_event = None
        self.resident = False

    def poll(self):
        if self.closing:
            self._release_show_event()
            if not any(t.is_alive() for t in self.long_jobs):
                import updates
                updates.abandon_usage_float_mpv()
                os._exit(0)
            self._poll_id = self.root.after(30, self.poll)
            return
        self.drain_results()
        if self.show_requested():
            self.reveal()
        hidden = self.hidden()
        parent_ok = self.parent_alive()
        if not parent_ok:
            self._version_checked_at = 0.0
        superseded = self.version_superseded()
        if not parent_ok or superseded:
            self._release_show_event()
        if hidden and (not parent_ok or superseded) and not any(t.is_alive() for t in self.long_jobs):
            import updates
            updates.abandon_usage_float_mpv()
            os._exit(0)  # the tray that kept this window warm is gone or code package was upgraded
        if not hidden and superseded and not getattr(self, "_superseded_notified", False):
            self._superseded_notified = True
            self.status.set(_tr("AgentCompass 已更新到新版本，关闭后重新打开即为新版"))
            try:
                self.root.title(f"{TITLE} ({_tr('旧版本')})")
            except Exception:
                pass
        if hidden and self.trim_due and not self.busy_jobs and not (self.prebuild and not self.prebuild_finished):
            self.trim_due = False
            self.trim_memory()
        if not hidden and getattr(self, '_boost_refreshed', None) is not None \
                and time.monotonic() - self._boost_refreshed >= 60:
            self.load_boost()
        if not hidden and self.current == "quota" and not self.busy_jobs \
                and getattr(self, "_quota_refreshed", None) is not None \
                and time.monotonic() - self._quota_refreshed >= 15:
            self.load_quota(True)
        self._poll_id = self.root.after(100 if hidden else 30, self.poll)

    # ---------- warm window ----------

    resident = False  # closing hides the window; the process stays for the next open
    trim_due = False  # hidden and touched since the last trim
    prebuild_finished = False
    show_event = None
    parent_handle = None

    def stay_resident(self, event, parent_pid: int) -> None:
        """Keep this process after the window closes while the tray `parent_pid` runs; `event` asks it to show."""
        self.show_event = event
        if parent_pid:
            self.parent_handle = _kernel32().OpenProcess(0x00100000, False, parent_pid)  # SYNCHRONIZE
        self.resident = bool(self.show_event and self.parent_handle)

    def show_requested(self) -> bool:
        return bool(self.show_event) and _kernel32().WaitForSingleObject(self.show_event, 0) == 0

    def parent_alive(self) -> bool:
        return not self.parent_handle or _kernel32().WaitForSingleObject(self.parent_handle, 0) != 0

    def hidden(self) -> bool:
        return self.root.state() == "withdrawn"

    def hide(self):
        """Withdraw the window but keep its built pages; the next open is only a show."""
        self.close_model_menu()
        self.root.withdraw()
        page = self.pages[self.current]
        page._page_hidden = True  # Tk still counts a withdrawn window's widgets as mapped
        page_stack.notify(page, False)
        self.stale = set(self.built)
        self.trim_due = True

    def reveal(self):
        """Show the window again: read the page on screen afresh, then bring the window to the front."""
        was_hidden = self.hidden()
        self.stale.update(self.built - {self.current})
        self.stale.discard(self.current)
        self.refresh_page(self.current)
        if was_hidden:
            self.root.deiconify()
            page = self.pages[self.current]
            page._page_hidden = False
            page_stack.notify(page, True)
        self.root.lift()
        self.root.focus_force()

    def refresh_page(self, key):
        if key == "quota":
            self._cards_cache = None
        {"scoring": self.load_ledger, "quota": self.load_quota, "policy": self.load_policy,
         "plugins": self.load_plugins}[key]()

    def trim_memory(self):
        """While hidden, hand the working set back to Windows; pages fault back in when the window shows."""
        if not self.hidden() or sys.platform != "win32":
            return
        import gc

        gc.collect()
        kernel = _kernel32()
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        psapi = ctypes.WinDLL("psapi")
        psapi.EmptyWorkingSet.argtypes = [ctypes.c_void_p]
        psapi.EmptyWorkingSet(kernel.GetCurrentProcess())

    def close(self):
        if self.resident and self.parent_alive() and not self.version_superseded():
            self.hide()
            return
        self.closing = True
        self._release_show_event()
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
            import updates
            updates.abandon_usage_float_mpv()
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
        data = self.data_module()
        if (force or cached is None) and hasattr(data, "_invalidate_accounts_cache"):
            data._invalidate_accounts_cache()
        cards = data.account_cards()
        if cards and any("reserved_pct" not in w for c in cards for w in c.get("windows") or ()):
            res_fn = getattr(data, "pool_reservations", None)
            reservations = res_fn() if callable(res_fn) else {}
            for c in cards:
                acct = c.get("account")
                for w in c.get("windows") or ():
                    res = reservations.get((acct, w.get("id"))) or {}
                    w.setdefault("reserved_pct", res.get("pct", 0.0))
                    w.setdefault("reserved_usd", res.get("usd", 0.0))
                    w.setdefault("reserved_workers", list(res.get("workers") or ()))
        self._cards_cache = (now, cards)
        return cards

    def prefill(self, work, done):
        """Fill the page on screen before it shows, so it never shows empty first.

        A page built underneath reads on a thread instead: it isn't visible, and its reads (account files, CLI
        probes, process scans) took up to a second, which froze the page in front during the idle prebuild."""
        if self.prebuilding:
            self.run(work, done)
            return
        try:
            value = work()
        except Exception as exc:
            panel_widgets.log_error(exc)
            self.status.set(_tr('读取失败：{0}').format(f'{exc}'))
            return
        done(value)

    def say(self, then=None):
        """A done() callback that shows the message and optionally runs then()."""
        def done(message):
            text = message or _tr("完成")
            self.status.set(text)
            if then:
                self._keep_status_once = text
                try:
                    then()
                finally:
                    self._keep_status_once = None
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
            panel_widgets.log_error(exc)
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
        """Raise one page over the others; a page is built unmapped on its first show, so it appears complete."""
        if key == self.current:
            return
        already_built = key in self.built
        self.current = key
        for name in self.nav:
            active = name == key
            self.paint_nav(name, theme.ACCENT if active else theme.INK, "#FFFFFF" if active else "#CBD5E1", active)
        if not already_built:
            self.built.add(key)
            getattr(self, f"build_{key}")(self.pages[key])
        elif (key in self.stale or key == "quota") and key != 'plugins':
            self.refresh_page(key)
        elif key == "policy" and "models" in getattr(self, "settings_built", ()):
            if time.monotonic() - getattr(self, "_models_loaded_at", 0.0) >= 5.0:
                self.run(lambda: self.data_module().harness_model_rows(), self.models_rows_loaded)
                self.load_boost()
        self.stale.discard(key)
        self.page_stack.show(self.pages[key])
        if key == 'plugins' and already_built:
            self.load_plugins()

    PREBUILD_DELAY_MS = 1200  # after the opening page has drawn and its first reads are in
    PREBUILD_GAP_MS = 250  # between pages, so clicks and redraws get through
    prebuilding = False  # inside prebuild_next(): the page being built is covered

    def start_prebuild(self):
        self.prebuild = True
        self.schedule_prebuild()

    def schedule_prebuild(self, delay: int = PREBUILD_DELAY_MS):
        generation = self._generation
        self.root.after(delay, lambda: self.prebuild_next() if generation == self._generation and not self.closing else None)

    def prebuild_next(self):
        """Build one page not opened yet under the current one, so its first visit is only a raise.

        Creating a native window costs about a millisecond where global window hooks are installed, so a page
        built on its first click froze the window for 0.5-1 s; built ahead, one page per idle slot, it doesn't."""
        self.prebuilding = True
        try:
            for key, _, _ in PAGES:
                if key not in self.built:
                    self.built.add(key)
                    self.stale.add(key)
                    getattr(self, f"build_{key}")(self.pages[key])
                    self.page_stack.prepare(self.pages[key])
                    break
            else:
                key = next((key for key, _ in SETTINGS if key not in self.settings_built), None)
                if key is None:
                    self.prebuild_finished = True
                    self.trim_due = True  # a warm window built everything before anyone opened it
                    return
                self.settings_built.add(key)
                self.stale.add("policy")
                getattr(self, f"build_setting_{key}")(self.setting_frames[key])
                self.setting_stack.prepare(self.setting_frames[key])
        except Exception as exc:
            panel_widgets.log_error(exc)
            return
        finally:
            self.prebuilding = False
        self.schedule_prebuild(self.PREBUILD_GAP_MS)

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

    def attach_row_tooltip(self, table, text_for):
        """Hover text per Treeview row (raw ids for auditing); text_for(iid) returns '' for none."""
        current = {"iid": None}
        show, hide = self.attach_tooltip(table, bind=False)

        def move(event):
            iid = table.identify_row(event.y)
            if iid == current["iid"]:
                return
            hide()
            current["iid"] = iid
            table._tooltip_text = text_for(iid) if iid else ""
            if table._tooltip_text:
                show(event)

        def leave(_event=None):
            current["iid"] = None
            hide()
        table.bind("<Motion>", move, add="+")
        table.bind("<Leave>", leave, add="+")
        table.bind("<Button-1>", leave, add="+")

    def attach_tooltip(self, widget, bind=True):
        def hide(_event=None):
            tip = getattr(widget, "_tooltip_window", None)
            if tip is not None and tip.winfo_exists():
                tip.destroy()
        def show(event):
            hide()
            tip = widget._tooltip_window = self.tk.Toplevel(self.root)
            tip.overrideredirect(True)
            tip.attributes("-topmost", True)
            tip.geometry(f"+{event.x_root + 12}+{event.y_root + 18}")
            self.ttk.Label(tip, text=getattr(widget, "_tooltip_text", ""), padding=6,
                           wraplength=theme.px(480), style="CardTiny.TLabel").pack()
        if not bind:
            return show, hide
        widget.bind("<Enter>", show)
        widget.bind("<Leave>", hide)
        widget.bind("<Button-1>", hide, add="+")

    def _post_ui_progress(self, generation: int, callback, message: str):
        self.busy_jobs += 1
        self.results.put((generation, callback, message, None))
        try:
            self.root.event_generate("<<PanelResult>>", when="tail")
        except Exception:
            pass

    def _open_update_window_if_interactive(self):
        import update_status
        try:
            if hasattr(update_status.open_window, "_mock_name") or self.root.state() != "withdrawn":
                update_status.open_window()
        except Exception as exc:
            panel_widgets.log_error(exc)


def main() -> None:
    import tkinter as tk

    hidden = "--hidden" in sys.argv
    parent = int(sys.argv[sys.argv.index("--parent") + 1]) if "--parent" in sys.argv[:-1] else 0
    kernel = _kernel32()
    ctypes.set_last_error(0)
    event = kernel.CreateEventW(None, False, False, SHOW_EVENT)  # auto-reset
    if event and ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS: another window process serves opens
        if not hidden:
            kernel.SetEvent(event)
        return
    theme.identify_process()
    theme.dpi_aware()
    page = sys.argv[sys.argv.index("--page") + 1] if "--page" in sys.argv[:-1] else "scoring"
    root = tk.Tk()
    root.withdraw()
    try:
        panel = Panel(root, page)
        panel.stay_resident(event, parent)
        root.update_idletasks()
        if not hidden:
            root.deiconify()
        panel.start_prebuild()
    except Exception as exc:
        panel_widgets.log_error(exc)
        raise
    root.mainloop()


if __name__ == "__main__":
    main()