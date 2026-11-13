"""The quota page: per-account quota cards and the live worker table.

A mixin of router_window.Panel; methods run on the panel instance.
"""
from __future__ import annotations

from i18n import translate as _tr, tr

import time

import ui_theme as theme
import i18n
from panel_widgets import TRACK, clip, freeze_redraw, short_badge


def tokens_text(value) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    return f"{value / 1_000_000:.2f}M" if value >= 100_000 else f"{value / 1000:.0f}k"


def cap_tokens_text(value: int) -> str:
    value = int(value)
    if value >= 1_000_000 and value % 10_000 == 0:
        return f"{value / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M"
    if 1_000 <= value < 1_000_000 and value % 1_000 == 0:
        return f"{value // 1_000}k"
    return f"{value:,}"


def worker_cap_heading(limit: int) -> str:
    return _tr("占 {0} 上限").format(cap_tokens_text(limit))


def card_title(card: dict) -> str:
    """A quota card's name; an account in trouble or excluded from dispatch says so on the card itself, with the reason on the hover."""
    if (card.get("status") not in ("plan_inactive", "usage_failing", "auth_failed", "agy_account_flapping")
            and not card.get("retry_hold") and not card.get("plan_unknown_reason")):
        return f'{card["label"]} · {_tr("不派发")}' if card.get("host_enabled") is False else card["label"]
    return f'{card["label"]} · {short_badge(card.get("badge")).split("，", 1)[0].split(", ", 1)[0]}'


def quota_columns(width: int, count: int = 4, current: int | None = None,
                  step: int | None = None, hysteresis: int | None = None) -> int:
    """Responsive column count for the quota grid, with hysteresis around column boundaries."""
    step = step or max(1, theme.px(200))
    hysteresis = theme.px(8) if hysteresis is None else hysteresis
    upper = max(1, min(5, count or 4))
    if width <= 1:
        return min(4, upper)
    if current is not None and 1 <= current <= upper:
        low = current * step - hysteresis
        high = (current + 1) * step + hysteresis
        if low <= width < high:
            return current
    return max(1, min(upper, width // step))


def short_countdown(text: str | None) -> str:
    """Compact countdown like usage-float ('1d 10h', '1h 47m', '45m')."""
    import re

    raw = str(text or "").strip()
    if not raw or raw == "—":
        return "—"
    m = re.fullmatch(r"(\d+)\s*天\s*(\d+)\s*小时", raw)
    if m:
        return f"{int(m.group(1))}d {int(m.group(2))}h"
    m = re.fullmatch(r"(\d+)\s*小时\s*(\d+)\s*分", raw)
    if m:
        return f"{int(m.group(1))}h {int(m.group(2))}m"
    m = re.fullmatch(r"(\d+)\s*分", raw)
    if m:
        return f"{int(m.group(1))}m"
    return raw


def short_window_name(window: dict, multi: bool = True) -> str:
    """Short window tag ('5h', '7d', 'fable 7d', 'gemini 7d', 'credits') without verbose labels."""
    wid = str(window.get("id") or "").strip()
    hours = window.get("hours")
    if hours == 168:
        span = "7d"
    elif isinstance(hours, (int, float)) and hours >= 24 and hours % 24 == 0:
        span = f"{int(hours // 24)}d"
    elif isinstance(hours, (int, float)):
        span = f"{hours:g}h"
    else:
        span = ""
    if wid in ("five_hour", "5h"):
        return "5h"
    if wid in ("seven_day", "7d", "weekly"):
        return span or "7d"
    if wid.startswith("weekly_"):
        return f"{wid[7:].replace('_', ' ')} {span or '7d'}"
    if wid.endswith("-weekly"):
        return f"{wid[:-7]} {span or '7d'}"
    if wid.endswith("-5h"):
        return f"{wid[:-3]} 5h"
    if wid == "credits":
        return span or _tr("订阅额度")
    if wid == "balance":
        return _tr("已用")
    if wid in ("usage", "spend", "default", ""):
        return span or wid or "7d"
    return f"{wid} {span}".strip() if span and not wid.endswith(span) else wid


def card_window_name(card: dict, window: dict) -> str:
    """Window tag on an account card, matching Usage Float: a pool of N accounts reads '7d×N'; an account whose
    only general window is the week (Codex, Claude Ultra) shows no tag; model-scoped weeks keep theirs."""
    count = window.get("accounts")
    suffix = f"×{count}" if isinstance(count, int) and not isinstance(count, bool) and count > 1 else ""
    general = [w for w in card.get("windows") or () if not str(w.get("id") or "").startswith("weekly_")]
    if (not suffix and card.get("account") != "antigravity" and len(general) == 1
            and general[0].get("id") == window.get("id")):
        return ""
    return short_window_name(window) + suffix


def quota_window_title(card: dict, window: dict) -> str:
    """Compact cell title combining account short name and window short tag."""
    acct = "AGY" if card.get("account") == "antigravity" else (card.get("label") or card.get("account") or "")
    multi = len(card.get("windows") or ()) > 1
    w_short = short_window_name(window, multi=multi)
    return f"{acct} {w_short}".strip()


def card_read_stamp(card: dict) -> str:
    """Short reading time or stale/failure note for a quota card."""
    import router_data

    raw = card.get("fetched_at") or card.get("last_success_at")
    moment = router_data._iso_datetime(raw) if raw else None
    stamp = moment.astimezone().strftime("%m-%d %H:%M") if moment else None
    age = card.get("usage_age_seconds")
    if not stamp and isinstance(age, (int, float)) and age >= 0:
        stamp = _tr("{0}前读").format(ago_text(age))
    failing = card.get("status") in ("usage_failing", "plan_inactive", "auth_failed")
    stale = failing or card.get("usage_status") == "stale" or any(w.get("stale") for w in card.get("windows") or ())
    if stale:
        if stamp:
            return _tr("旧读数 {0}").format(stamp) if moment else stamp
        return _tr("旧读数") if card.get("windows") else ""
    return stamp or ""


def quota_reading_stamp(cards: list[dict]) -> str | None:
    """Latest quota reading timestamp across cards, formatted in local time."""
    import router_data

    moments = [
        m for card in (cards or ())
        if (m := router_data._iso_datetime(card.get("fetched_at") or card.get("last_success_at"))) is not None
    ]
    if not moments:
        return None
    return max(moments).astimezone().strftime("%m-%d %H:%M")


def quota_tooltip_text(card: dict, window: dict | None = None, refreshable: bool = False) -> str:
    """Hover: plan, what is left per window and when it resets, in-flight reservations.

    A Magpie pool's hover is Usage Float's: its accounts' table alone, the card already showing the pooled windows;
    a badge, a stale reading or in-flight reservations still follow it."""
    table = pool_member_lines(card.get("pool_members"))
    if table:
        extra = []
        if card.get("kind") not in (None, "ok") and card.get("badge"):
            extra.append(f"[{card['badge']}]")
        if card.get("usage_status") == "stale" and card.get("usage_age_seconds") is not None:
            extra.append(_tr("{0}前读").format(ago_text(card["usage_age_seconds"])))
        for w_item in [window] if window is not None else list(card.get("windows") or ()):
            reserved = float(w_item.get("reserved_pct") or 0.0)
            if reserved > 0:
                tag = card_window_name(card, w_item) or short_window_name(w_item)
                extra.append(f"{tag} " + _tr("在途 {0}%").format(f"{reserved:.0f}"))
        return _with_usage_hover("\n".join([*table, *extra]), card)
    head =" · ".join(str(v) for v in (card.get("label") or card.get("account") or "—", card.get("plan")) if v)
    if card.get("kind") not in (None, "ok") and card.get("badge"):
        head += f"  [{card['badge']}]"
    stamp = card_read_stamp(card)
    if stamp:
        head += f" · {stamp}"
        if card.get("usage_age_seconds") is not None and (card.get("fetched_at") or card.get("last_success_at")):
            head += f" ({_tr('{0}前读').format(ago_text(card['usage_age_seconds']))})"
    lines = [head]
    for w_item in [window] if window is not None else list(card.get("windows") or ()):
        tag = card_window_name(card, w_item) or short_window_name(w_item)
        rem, reset_in = w_item.get("remaining_pct"), w_item.get("reset_in")
        if reset_in == _tr("已重置"):
            lines.append(f"{tag} {_tr('已重置')}")
            continue
        text = f"{tag} {'—' if rem is None else f'{rem:.0f}%'}"
        if reset_in and reset_in != "—":
            text += f" · {short_countdown(reset_in)}"
        reserved = float(w_item.get("reserved_pct") or 0.0)
        if reserved > 0:
            text += " · " + _tr("在途 {0}%").format(f"{reserved:.0f}")
        lines.append(text)
    return _with_usage_hover("\n".join(lines), card)


def _with_usage_hover(text: str, card: dict) -> str:
    """Append a metered miss reason when the badge does not already carry it."""
    hover = str(card.get("usage_hover") or "").strip()
    if not hover or hover in text:
        return text
    return f"{text}\n{hover}" if text else hover


def ago_text(seconds) -> str:
    """'3分钟' / '5小时' / '2天' for how long ago a reading was taken."""
    if not isinstance(seconds, (int, float)) or seconds < 0:
        return "—"
    if seconds < 3600:
        return _tr("{0}分钟").format(max(1, int(seconds // 60)))
    if seconds < 86400:
        return _tr("{0}小时").format(int(seconds // 3600))
    return _tr("{0}天").format(int(seconds // 86400))


# A Magpie account is read only when asked, so a reading this old is marked on its hover line.
POOL_MEMBER_STALE_SECONDS = 1800


def compact_countdown(seconds) -> str:
    """'3d7h' / '2h8m' / '45m': a table cell's countdown."""
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, minutes = rest // 3600, rest % 3600 // 60
    if days:
        return f"{days}d{hours}h" if hours else f"{days}d"
    return (f"{hours}h{minutes}m" if minutes else f"{hours}h") if hours else f"{max(1, minutes)}m"


def pool_member_lines(members) -> list[str]:
    """A Magpie pool's accounts as a small table: one row per account, per window what is left and when that
    account resets."""
    if not isinstance(members, list) or len(members) < 2:
        return []
    members = [m for m in members if isinstance(m, dict)]
    names = [str(m.get("label") or "—") for m in members]
    short = [n.split("@")[0] for n in names]
    names = short if len(set(short)) == len(short) else names
    present = [(wid, tag) for wid, tag in (("seven_day", "7d"), ("five_hour", "5h"))
               if any(isinstance((m.get("windows") or {}).get(wid), dict) for m in members)]
    cells = []
    for member in members:
        row = []
        for wid, _tag in present:
            w = (member.get("windows") or {}).get(wid)
            if not isinstance(w, dict) or not isinstance(w.get("used_pct"), (int, float)):
                row.append("")
                continue
            cell = f"{100 - w['used_pct']:.0f}%"
            if isinstance(w.get("reset_seconds"), (int, float)):
                cell += " " + compact_countdown(w["reset_seconds"])
            row.append(cell)
        cells.append(row)
    name_w = max(len(n) for n in names)
    widths = [max(len(tag), *(len(r[i]) for r in cells)) for i, (_wid, tag) in enumerate(present)]
    lines = [(" " * name_w +"".join(f"  {tag.ljust(w)}" for (_wid, tag), w in zip(present, widths))).rstrip()]
    for name, member, row in zip(names, members, cells):
        note = "" if member.get("counted", True) else " " + _tr("(不计入)")
        age = member.get("age_seconds")
        if isinstance(age, (int, float)) and age > POOL_MEMBER_STALE_SECONDS:
            note += " " + _tr("{0}前读").format(ago_text(age))
        lines.append((name.ljust(name_w) +"".join(f"  {c.ljust(w)}" for c, w in zip(row, widths))).rstrip()
                     + note)
    return lines


class QuotaBar:
    """Usage-float presentation: remaining quota fills from the left."""

    def __init__(self, tk_mod, parent, height: int = 6):
        self.tk = tk_mod
        self.canvas = tk_mod.Canvas(parent, bg=TRACK, height=height, highlightthickness=0, bd=0)
        self.remaining_pct: float | None = 0.0
        self.reserved_pct: float = 0.0
        self.colour: str = "Green"
        self.stale: bool = False
        self.value: float = 0.0
        self.segments: dict = {"used_pct": 100.0, "reserved_pct": 0.0, "remaining_pct": 0.0}
        self.canvas.bind("<Configure>", self._redraw, add="+")

    def pack(self, **kwargs):
        self.canvas.pack(**kwargs)

    def bind(self, sequence=None, func=None, add=None):
        return self.canvas.bind(sequence, func, add=add)

    def winfo_children(self):
        return ()

    def cget(self, key: str):
        if key == "value":
            return self.value
        if key == "segments":
            return self.segments
        return self.canvas.cget(key)

    def __getitem__(self, key: str):
        return self.cget(key)

    def set_segments(self, remaining_pct: float | None, reserved_pct: float = 0.0,
                     colour: str = "Green", stale: bool = False):
        self.remaining_pct = remaining_pct
        self.reserved_pct = max(0.0, float(reserved_pct or 0.0))
        self.colour = colour
        self.stale = stale
        rem = 0.0 if remaining_pct is None else max(0.0, min(100.0, float(remaining_pct)))
        used = 0.0 if remaining_pct is None else max(0.0, 100.0 - rem)
        eff_res = min(rem, self.reserved_pct)
        free_rem = max(0.0, rem - eff_res)
        self.value = rem
        self.segments = {
            "used_pct": round(used, 2),
            "reserved_pct": round(eff_res, 2),
            "remaining_pct": round(free_rem, 2),
        }
        self._redraw()

    def _redraw(self, _event=None):
        c = self.canvas
        c.delete("all")
        w = max(c.winfo_width(), 120)
        h = max(c.winfo_height(), theme.px(6))
        fill = ("#94A3B8" if self.stale else
                {"Green": theme.GREEN, "Amber": theme.AMBER, "Red": theme.RED}.get(self.colour, theme.GREEN))
        c.create_rectangle(0, 0, w, h, fill=TRACK, width=0, tags=("usage_track",))
        x_rem = int(round(w * self.value / 100.0))
        if x_rem > 0:
            c.create_rectangle(0, 0, x_rem, h, fill=fill, width=0, tags=("seg_remaining",))


def quota_card_placements(cards: list[dict], cols: int) -> list[tuple[int, int, int]]:
    """Return (row, column, rowspan) for each card: metered cards fill shortest columns first, unmetered follow."""
    if not cards:
        return []
    cols = max(1, min(cols, len(cards)))
    cursors = [0] * cols
    counts = [0] * cols
    spans = [3 + 2 * len(c.get("windows") or ()) for c in cards]
    order = sorted(range(len(cards)), key=lambda i: (0 if cards[i].get("windows") else 1, i))
    placements = [(0, 0, 3)] * len(cards)
    for index in order:
        span = spans[index]
        col = min(range(cols), key=lambda c: (cursors[c], counts[c], c))
        row = cursors[col]
        cursors[col] += span
        counts[col] += 1
        placements[index] = (row, col, span)
    return placements


class QuotaPage:
    # ---------- page 2: quota & workers ----------
    def build_quota(self, page):
        tk, ttk = self.tk, self.ttk
        self.quota_page = page
        self.quota_header = self.header(page, _tr("额度监控"), (
            (_tr("刷新"), lambda: self.load_quota(True), "SmallAccent.TButton"),))
        self.quota_updated = tk.Label(
            self.quota_header, text=tr("剩余额度% · 点击 ↻ 刷新", "Remaining % · Click ↻ to refresh"),
            bg=theme.BG, fg=theme.MUTED, font=(theme.FONT, 9), cursor="hand2")
        self.quota_updated.pack(side="right", padx=(0, 10))
        self.quota_updated.bind("<Button-1>", lambda _e: self.refresh_usage())
        self.attach_tooltip(self.quota_updated)   # shows the usage statistics' price error, when there is one

        self.quota_cols = None
        self._configured_quota_cols = 0
        self._quota_last_w = self._quota_last_h = self._quota_last_bbox = None
        self._quota_tip = None

        # Window-level tiles (usage-float style) + compact 1-line unmetered strip at the bottom.
        self.quota_holder = ttk.Frame(page)
        self.quota_holder.pack(fill="x")
        self.quota_canvas = tk.Canvas(self.quota_holder, bg=theme.BG, highlightthickness=0, bd=0,
                                      height=theme.px(210))
        self.quota_bar = ttk.Scrollbar(self.quota_holder, orient="vertical", command=self.quota_canvas.yview)
        self.quota_body = ttk.Frame(self.quota_canvas)
        self.quota_grid = ttk.Frame(self.quota_body)
        self.quota_grid.pack(fill="x")
        self.quota_unmetered = tk.Frame(self.quota_body, bg=theme.BG)
        self.quota_unmetered.pack(fill="x", pady=(theme.px(2), 0))
        self._quota_win = self.quota_canvas.create_window(0, 0, anchor="nw", window=self.quota_body)
        self.quota_canvas.configure(yscrollcommand=self.quota_bar.set)
        self.quota_canvas.pack(side="left", fill="x", expand=True)
        self.quota_canvas.bind("<Enter>", lambda e: self.quota_canvas.bind_all("<MouseWheel>", self.quota_wheel))
        self.quota_canvas.bind("<Leave>", lambda e: self.quota_canvas.unbind_all("<MouseWheel>"))

        workers = theme.card(page, padding=12, fill="both", expand=True, pady=(6, 0))
        self.workers_card = workers
        ttk.Label(workers, text=_tr("在途与活跃会话"), style="CardH3.TLabel").pack(anchor="w", pady=(0, 6))
        limit = self.data_module().rotation_limit()
        frame, self.worker_table = self.tree(workers, (
            ("agent", "Agent", 70, "w"), ("title", _tr("任务"), 200, "w"), ("bundle", _tr("模型"), 110, "w"),
            ("state", _tr("状态"), 66, "w"), ("compacted", _tr("已压缩"), 62, "e"), ("context", _tr("窗口"), 58, "e"),
            ("total", _tr("累计"), 62, "e"), ("bar", worker_cap_heading(limit), 130, "w")), 4)
        frame.pack(fill="both", expand=True)
        self.attach_row_tooltip(self.worker_table, self.worker_row_tip)

        for widget in (page, self.quota_holder, self.quota_body, self.quota_grid):
            widget.bind("<Configure>", self.sync_quota_geometry, add="+")

        self.prefill(self.cached_account_cards, self.quota_cards_loaded)
        self.load_quota()

    def show_quota_tip(self, event, widgets):
        text = widgets.get("tooltip_text") or ""
        if not text:
            return
        self.hide_quota_tip()
        tk = self.tk
        tip = tk.Toplevel(self.root)
        tip.wm_overrideredirect(True)
        try:
            tip.wm_attributes("-topmost", True)
        except Exception:
            pass
        # Usage Float's hover look (colours, monospace, padding), so a pool reads the same in both.
        lbl = tk.Label(
            tip, text=text, justify="left", anchor="w", bg="#1f2937", fg="#f9fafb",
            font=("Consolas", 12), padx=theme.px(12), pady=theme.px(8), bd=0)
        lbl.pack()
        x = getattr(event, "x_root", self.root.winfo_rootx() + 80) + 12
        y = getattr(event, "y_root", self.root.winfo_rooty() + 80) + 12
        tip.wm_geometry(f"+{x}+{y}")
        self._quota_tip = tip

    def hide_quota_tip(self, _event=None):
        tip = getattr(self, "_quota_tip", None)
        if tip is not None:
            self._quota_tip = None
            try:
                tip.destroy()
            except Exception:
                pass

    def _bind_quota_interactions(self, root_widget, widgets, account: str, refreshable: bool):
        again = widgets.get("again")

        def on_enter(event):
            if again is not None:
                again.configure(fg=theme.TEXT)
            self.show_quota_tip(event, widgets)

        def on_leave(_event):
            if again is not None:
                again.configure(fg="#CBD5E1")
            self.hide_quota_tip()

        stack = [root_widget]
        while stack:
            w = stack.pop()
            w.bind("<Enter>", on_enter, add="+")
            w.bind("<Leave>", on_leave, add="+")
            if refreshable:
                w.bind("<Button-3>", lambda _e, a=account: self.refresh_usage([a]), add="+")
            stack.extend(w.winfo_children())

    def quota_wheel(self, event):
        if hasattr(event.widget, "winfo_class") and event.widget.winfo_class() == "Treeview":
            return
        if self.quota_body.winfo_reqheight() > self.quota_canvas.winfo_height():
            self.quota_canvas.yview_scroll(int(-event.delta / 120), "units")

    def quota_column_count(self, width: int | None = None) -> int:
        if width is None or width <= 1:
            width = self.quota_holder.winfo_width() if hasattr(self, "quota_holder") else 0
        if width <= 1:
            root_w = self.root.winfo_width()
            if root_w > 200:
                width = root_w - theme.px(198 if i18n.is_en() else 168) - theme.px(36)
        count = len(getattr(self, "quota_cards", None) or ()) or 4
        return quota_columns(width, count, getattr(self, "quota_cols", None))

    def layout_quota_cards(self, cols: int | None = None):
        cards = getattr(self, "quota_cards", None)
        if not cards:
            return
        if cols is None:
            cols = self.quota_column_count()
        cols = max(1, min(cols, len(cards)))
        self.quota_cols = cols
        old_cols = getattr(self, "_configured_quota_cols", 0)
        for col in range(max(cols, old_cols)):
            self.quota_grid.columnconfigure(col, weight=1 if col < cols else 0,
                                            uniform="cards" if col < cols else "")
        self._configured_quota_cols = cols
        for widgets, (row, col, span) in zip(cards, quota_card_placements(cards, cols)):
            widgets["outer"].grid_configure(
                row=row, column=col, rowspan=span, sticky="new",
                padx=(0 if col == 0 else theme.px(8), 0), pady=(0, theme.px(6)))

    def sync_quota_geometry(self, _event=None):
        if not hasattr(self, "quota_holder") or not hasattr(self, "quota_body"):
            return
        holder_w = self.quota_holder.winfo_width()
        cols = self.quota_column_count(holder_w)
        if cols != getattr(self, "quota_cols", None) and getattr(self, "quota_cards", None):
            with freeze_redraw(self.quota_grid):
                self.layout_quota_cards(cols)
        req_h = self.quota_body.winfo_reqheight()
        page_h = self.quota_page.winfo_height() if hasattr(self, "quota_page") else 0
        if page_h > 1:
            header_h = self.quota_header.winfo_height() + theme.px(10)
            workers_min_h = max(self.workers_card.outer.winfo_reqheight(), theme.px(175))
            avail_h = page_h - header_h - theme.px(6)
            cap_h = max(theme.px(140), min(theme.px(300), avail_h - workers_min_h))
        else:
            cap_h = theme.px(260)
        need_scroll = req_h > cap_h + 2
        if need_scroll and not self.quota_bar.winfo_manager():
            self.quota_bar.pack(side="right", fill="y", padx=(4, 0))
        elif not need_scroll and self.quota_bar.winfo_manager():
            self.quota_bar.pack_forget()
            self.quota_canvas.yview_moveto(0.0)
        if holder_w > 1:
            bar_w = max(self.quota_bar.winfo_reqwidth(), theme.px(16)) + 4 if need_scroll else 0
            canvas_w = max(1, holder_w - bar_w)
            if canvas_w != self._quota_last_w:
                self._quota_last_w = canvas_w
                self.quota_canvas.itemconfigure(self._quota_win, width=canvas_w)
        else:
            canvas_w = self._quota_last_w or 1
        target_h = min(req_h, cap_h) if req_h > 1 else theme.px(180)
        if target_h != self._quota_last_h:
            self._quota_last_h = target_h
            self.quota_canvas.configure(height=target_h)
        bbox = (0, 0, max(1, canvas_w), max(req_h, target_h))
        if bbox != self._quota_last_bbox:
            self._quota_last_bbox = bbox
            self.quota_canvas.configure(scrollregion=bbox)

    def load_quota(self, force: bool = False):
        self._quota_refreshed = time.monotonic()
        data = self.data_module()
        self.run(
            lambda: (self.cached_account_cards(force), data.workers(), data.rotation_limit()),
            self.quota_loaded,
            _tr("正在读取账号水位…"),
        )

    def refresh_usage(self, accounts=None):
        def done(_msg):
            self._cards_cache = None
            self.load_quota(True)
        self.run(lambda: self.data_module().refresh_usage(accounts), self.say(lambda: done(None)), _tr("正在请求刷新额度…"))

    def quota_cards_loaded(self, cards):
        self._quota_refreshed = time.monotonic()
        self._rendered_quota_cards = cards
        shape = tuple((card["account"], tuple(w["id"] for w in card["windows"])) for card in cards)
        metered_cards = [card for card in cards if card["windows"]]
        unmetered_cards = [card for card in cards if not card["windows"]]
        if shape != getattr(self, "quota_shape", None):
            # First load, or an account / window appeared or went away: build the account cards once.
            self.hide_quota_tip()
            with freeze_redraw(self.quota_body):
                for child in self.quota_grid.winfo_children():
                    child.destroy()
                for child in self.quota_unmetered.winfo_children():
                    child.destroy()
                self.quota_cards = [
                    self.build_quota_card(index, card, bool(card.get("refreshable")))
                    for index, card in enumerate(metered_cards)
                ]
                self.quota_unmetered_cards = [
                    self.build_unmetered_chip(index, card, bool(card.get("refreshable")))
                    for index, card in enumerate(unmetered_cards)
                ]
                self.quota_shape = shape
                self.quota_cols = None
                self.layout_quota_cards()
        for widgets, card in zip(self.quota_cards, metered_cards):
            self.fill_quota_card(widgets, card)
        for widgets, card in zip(getattr(self, "quota_unmetered_cards", ()), unmetered_cards):
            self.fill_quota_card(widgets, card)
        if hasattr(self, "quota_updated"):
            read_stamp = quota_reading_stamp(cards)
            if read_stamp:
                text = f"{tr('剩余额度%', 'Remaining %')} · {tr('读数于', 'Read at')} {read_stamp} ↻"
            else:
                text = f"{tr('剩余额度%', 'Remaining %')} · {tr('更新于', 'Updated')} {time.strftime('%H:%M:%S')} ↻"
            price_error = self.data_module().capacity_price_error()
            if price_error:
                text += f" · ⚠ {tr('用量无法计价', 'Usage unpriced')}"
            self.quota_updated._tooltip_text = price_error or ""
            self.quota_updated.configure(text=text)
        self.sync_quota_geometry()

    def quota_loaded(self, value):
        if len(value) == 3:
            cards, workers, limit = value
        else:
            cards, workers = value
            limit = workers[0]["limit"] if workers and workers[0].get("limit") else self.data_module().rotation_limit()
        self.worker_rows = workers
        if cards is not getattr(self, "_rendered_quota_cards", None):
            self.quota_cards_loaded(cards)
        self.worker_table.heading("bar", text=worker_cap_heading(limit))
        self.worker_table.delete(*self.worker_table.get_children())
        for index, worker in enumerate(workers):
            share = (worker["historical_tokens"] or 0) / worker["limit"] if worker["limit"] else 0
            blocks = min(10, int(round(share * 10)))
            bar = "█" * blocks + "░" * (10 - blocks) + f" {share * 100:.0f}%"
            tag = (("danger",) if share >= 1 else ("warn",) if share >= 0.8
                   else ("muted",) if worker["state"] == _tr("近期待命") else ())
            self.worker_table.insert("", "end", iid=str(index), tags=tag, values=(
                (worker["agent_id"] or "")[:8], worker["title"], worker.get("label") or worker["bundle"], worker["state"],
                tokens_text(worker["compacted_tokens"]), tokens_text(worker["context_tokens"]),
                tokens_text(worker["historical_tokens"]), bar if worker["telemetry"] != "unknown" else _tr("遥测不可用")))
        if not workers:
            self.worker_table.insert("", "end", values=("", _tr("当前没有在途或活跃的 worker"), "", "", "", "", "", ""),
                                     tags=("muted",))
        running = sum(1 for w in workers if w["state"] != _tr("近期待命"))
        self.status.set(_tr('已读取 {0} 个账号、{1} 个在途 worker、{2} 个近期待命 · {3}').format(f'{len(cards)}', f'{running}', f'{len(workers) - running}', f"{time.strftime('%H:%M:%S')}"))

    def worker_row_tip(self, iid):
        rows = getattr(self, "worker_rows", ())
        if not iid.isdigit() or int(iid) >= len(rows):
            return ""
        worker = rows[int(iid)]
        return " · ".join(str(v) for v in (worker.get("agent_id"), worker.get("bundle")) if v)

    def build_quota_card(self, index, card, refreshable):
        """One account card with usage-float inner layout: header (dot + name + weakened ↻) + ultra-clean window rows."""
        tk = self.tk
        box = theme.card(self.quota_grid, padding=(10, 6))
        top = tk.Frame(box, bg=theme.CARD)
        top.pack(fill="x", pady=(0, 2))
        top.columnconfigure(1, weight=0)
        top.columnconfigure(2, weight=1)
        dot = tk.Label(top, text="●", bg=theme.CARD, fg=theme.GREEN, font=(theme.FONT, 7), padx=0, pady=0)
        dot.grid(row=0, column=0, sticky="w", padx=(0, 4))
        name = tk.Label(top, text=card["label"], bg=theme.CARD, fg=theme.TEXT, font=(theme.FONT, 9, "bold"),
                        anchor="w", padx=0, pady=0)
        name.grid(row=0, column=1, sticky="w")
        read_at = tk.Label(top, text="", bg=theme.CARD, fg=theme.MUTED, font=(self.mono, 8),
                           anchor="e", padx=2, pady=0)
        read_at.grid(row=0, column=2, sticky="e", padx=2)
        again = None
        if refreshable:
            # Weakened ↻ in card header: faint at rest, darkens on hover, or right-click anywhere on the card.
            again = tk.Label(top, text="↻", bg=theme.CARD, fg="#CBD5E1", font=(theme.FONT, 9),
                             cursor="hand2", bd=0, padx=2, pady=0)
            again.bind("<Button-1>", lambda _e, a=card["account"]: self.refresh_usage([a]))
            again.grid(row=0, column=3, sticky="e")
        clear = self.retry_clear_button(top, card["account"])
        clear.grid(row=0, column=4, sticky="e")
        clear.grid_remove()

        win_widgets = []
        for _ in card["windows"]:
            w_box = tk.Frame(box, bg=theme.CARD)
            w_box.pack(fill="x", pady=(3, 0))
            head = tk.Frame(w_box, bg=theme.CARD)
            head.pack(fill="x")
            w_name = tk.Label(head, text="", bg=theme.CARD, fg=theme.TEXT, font=(theme.FONT, 8, "bold"),
                              anchor="w", padx=0, pady=0)
            w_name.pack(side="left")
            w_detail = tk.Label(head, text="", bg=theme.CARD, fg=theme.MUTED, font=(self.mono, 8),
                                anchor="w", padx=6, pady=0)
            w_detail.pack(side="left")
            w_share = tk.Label(head, text="", bg=theme.CARD, fg=theme.GREEN, font=(self.mono, 11, "bold"),
                               anchor="e", padx=0, pady=0)
            w_share.pack(side="right")
            bar = QuotaBar(tk, w_box, height=theme.px(5))
            bar.pack(fill="x", pady=(1, 0))
            win_widgets.append((w_name, w_share, bar, w_detail))

        widgets = {
            "outer": box.outer,
            "account": card["account"],
            "refreshable": refreshable,
            "again": again,
            "clear": clear, "clear_layout": "grid",
            "read_at": read_at,
            "dot": dot,
            "name": name,
            "windows": win_widgets,
            "tooltip_text": "",
        }
        self._bind_quota_interactions(box.outer, widgets, card["account"], refreshable)
        return widgets

    def build_unmetered_chip(self, index, card, refreshable):
        """Compact 1-line status strip item for unmetered accounts (OpenCode, Dot, ChatGPT Web)."""
        tk = self.tk
        outer = tk.Frame(self.quota_unmetered, bg=theme.BORDER, padx=1, pady=1)
        outer.pack(side="left", padx=(0, theme.px(6)), pady=(0, theme.px(2)))
        inner = tk.Frame(outer, bg=theme.CARD, padx=theme.px(8), pady=theme.px(2))
        inner.pack(fill="both")
        dot = tk.Label(inner, text="●", bg=theme.CARD, fg=theme.GREEN, font=(theme.FONT, 7), padx=0, pady=0)
        dot.pack(side="left", padx=(0, 4))
        name = tk.Label(inner, text=card["label"], bg=theme.CARD, fg=theme.TEXT, font=(theme.FONT, 8, "bold"),
                        padx=0, pady=0)
        name.pack(side="left")
        detail = tk.Label(inner, text="", bg=theme.CARD, fg=theme.MUTED, font=(theme.FONT, 8), padx=4, pady=0)
        detail.pack(side="left")
        clear = self.retry_clear_button(inner, card["account"])
        widgets = {
            "outer": outer,
            "account": card["account"],
            "refreshable": refreshable,
            "again": None,
            "clear": clear, "clear_layout": "pack",
            "dot": dot,
            "name": name,
            "detail": detail,
            "windows": [],
            "tooltip_text": "",
        }
        self._bind_quota_interactions(outer, widgets, card["account"], refreshable)
        return widgets

    def retry_clear_button(self, parent, account):
        """A card's 清除 link, shown only while the account (or a pool or model of it) is held for a provider failure.
        With pools held it clears one pool at a time, the one it names (fill_quota_card sets `hold_pool`)."""
        button = self.tk.Label(parent, text=_tr("清除"), bg=theme.CARD, fg=theme.AMBER, font=(theme.FONT, 8, "underline"),
                               cursor="hand2", bd=0, padx=4, pady=0)
        button.hold_pool = None
        button.bind("<Button-1>", lambda _e, a=account, b=button: self.clear_retry_hold(a, b.hold_pool))
        return button

    def clear_retry_hold(self, account, pool=None):
        def done(_msg):
            self._cards_cache = None
            self.load_quota(True)
        def clear():
            # The status bar shows what this returns: a sentence, never the bare True/False ("1").
            data = self.data_module()
            cleared = data.clear_retry_holds(account, pool) if pool else data.clear_retry_holds(account)
            return _tr("已清除提供方失败标记") if cleared else _tr("没有需要清除的提供方失败标记")
        self.run(clear, self.say(lambda: done(None)), _tr("正在清除提供方失败标记…"))

    @staticmethod
    def fill_quota_card(widgets, card):
        refreshable = bool(widgets.get("refreshable"))
        clear = widgets.get("clear")
        if clear is not None:
            pool = card.get("retry_clear_pool") if card.get("retry_hold") else None
            clear.hold_pool = pool
            clear.configure(text=_tr("清除 {0}").format(pool) if pool else _tr("清除"))
            if widgets.get("clear_layout") == "grid":
                clear.grid() if card.get("retry_hold") else clear.grid_remove()
            elif card.get("retry_hold"):
                clear.pack(side="left")
            else:
                clear.pack_forget()
        widgets["tooltip_text"] = quota_tooltip_text(card, None, refreshable)
        is_failing = card.get("status") in ("usage_failing", "plan_inactive", "auth_failed")
        dot_fg = (theme.AMBER if is_failing or card.get("kind") == "warn"
                  else theme.GREEN if card.get("kind") == "ok"
                  else theme.RED if card.get("kind") == "bad"
                  else theme.FAINT)
        if "dot" in widgets:
            widgets["dot"].configure(fg=dot_fg)
        if "name" in widgets:
            widgets["name"].configure(text=card_title(card))
            if widgets["name"].winfo_manager() == "grid":
                widgets["name"].grid_configure(row=0, column=1, columnspan=2 if is_failing else 1, sticky="w")
        if "read_at" in widgets:
            stamp = card_read_stamp(card)
            widgets["read_at"].configure(
                text=stamp,
                fg=theme.AMBER if is_failing else theme.MUTED,
                anchor="w" if is_failing else "e",
            )
            if widgets["read_at"].winfo_manager() in ("grid", ""):
                if not stamp:
                    widgets["read_at"].grid_remove()
                elif is_failing:
                    widgets["read_at"].grid(row=1, column=1, columnspan=3, sticky="w", padx=0, pady=(1, 0))
                else:
                    widgets["read_at"].grid(row=0, column=2, columnspan=1, sticky="e", padx=2, pady=0)
        if not card.get("windows"):
            plan = card.get('usage_note') or clip(card.get("plan"), 14)
            badge = short_badge(card.get("badge"))
            if "detail" in widgets:
                widgets["detail"].configure(text=f"{badge} · {plan}" if plan and plan != "—" else badge)
            return
        for (w_name, w_share, bar, w_detail), window in zip(widgets["windows"], card["windows"]):
            remaining = window.get("remaining_pct")
            reserved = float(window.get("reserved_pct") or 0.0)
            is_stale = bool(window.get("stale") or card.get("usage_status") != "fresh" or is_failing)
            if window.get("metered_usd"):
                used = window.get("used_pct")
                share = None if used is None else max(0.0, min(100.0, float(used)))
                colour = "Red" if share is not None and share >= 90.0 else "Amber" if share is not None and share >= 70.0 else "Green"
                fg = ("#64748B" if is_stale
                      else theme.GREEN if colour == "Green"
                      else theme.AMBER if colour == "Amber"
                      else theme.RED)
                w_name.configure(text="")
                w_detail.configure(text=window.get("spent_text") or "")
                w_share.configure(text="—" if used is None else f"{max(0.0, float(used)):.0f}%", fg=fg)
                if hasattr(bar, "set_segments"):
                    bar.set_segments(share, 0, colour=colour, stale=is_stale)
                else:
                    bar.configure(style=f"{colour}.Horizontal.TProgressbar", value=share or 0)
                continue
            w_name.configure(text=card_window_name(card, window))
            # A pool counts down to its next partial refill; each account's own reset is on the hover.
            w_detail.configure(text=short_countdown(window.get("reset_in")))
            # remaining_pct is in percent of one account (a fresh pool of two reads 200%); the bar and colour
            # describe the whole pool.
            count = window.get("accounts") if isinstance(window.get("accounts"), int) and window["accounts"] > 1 else 1
            share = None if remaining is None else max(0.0, min(100.0, float(remaining) / count))
            colour = "Red" if share is not None and share <= 10.0 else "Amber" if share is not None and share <= 30.0 else "Green"
            fg = ("#64748B" if is_stale
                  else theme.GREEN if colour == "Green"
                  else theme.AMBER if colour == "Amber"
                  else theme.RED)
            w_share.configure(text="—" if remaining is None else f"{max(0.0, float(remaining)):.0f}%", fg=fg)
            if hasattr(bar, "set_segments"):
                bar.set_segments(share, reserved / count, colour=colour, stale=is_stale)
            else:
                bar.configure(style=f"{colour}.Horizontal.TProgressbar", value=share or 0)
