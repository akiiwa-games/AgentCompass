"""AgentCompass main panel: decision compass, quota and live workers, routing settings, integrations.

Runs as its own pythonw process, like the network and update windows, so the tray thread never waits on it.
The tray starts that process hidden (prewarm) so its pages are built before the first open; closing the window
only hides it, and open_window() shows it again. All reads and actions run on worker threads.
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

import component_launch
import plugins
import radar_chart
import page_stack
import decision_theatre
import decision_summary
import quota_line
import display_names
import ui_theme as theme
import i18n

TITLE = "AgentCompass"
LEGACY_TITLE = "Route Manager · 控制台"  # a panel started before the rename
_process = None
SHOW_EVENT = "Local\\AgentCompassWindowShow"  # created by the window process; open_window() sets it to show it
PAGES = (("scoring", "决策罗盘", "◎"), ("quota", "额度监控", "▤"), ("policy", "模型设置", "⚙"), ("plugins", "外部集成", "▦"))
PAGES = LocalizedSequence(PAGES)
SETTINGS = (("models", "模型与加权"), ("strategy", "额度阈值"), ("system", "系统选项"))
SETTING_ALIASES = {"accounts": "models", "boost": "models", "cli": "models", "tuning": "strategy"}
SETTINGS = LocalizedSequence(SETTINGS)
TRACK = "#E2E8F0"  # segmented switch track on the page background
STRATEGY_PAD = 14    # card padding on the strategy settings page
STRATEGY_FIELD = 10  # entry and spinbox text size there, matching the 10pt labels beside them
AUTO_REPLAY_ROWS = 5  # newest ledger rows tried for the opening decision; legacy rows replan, so keep it small


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
    window = win32gui.FindWindow("TkTopLevel", TITLE) or win32gui.FindWindow("TkTopLevel", LEGACY_TITLE)
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


def tokens_text(value) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    return f"{value / 1_000_000:.2f}M" if value >= 100_000 else f"{value / 1000:.0f}k"


def verdict_summary(result: dict, pair: list[dict]) -> tuple[list[tuple[str, str]], str]:
    """(label, value) rows for the compact verdict grid under the radar, and the full router reason for its tooltip.

    Values come from the plan's structured fields; the long reason only fills the quota row when no capacity is recorded.
    """
    decision = (result or {}).get("decision") or {}
    reason = " ".join(str(decision.get("reason") or "").split())
    if not pair:
        if (result or {}).get("status") == "blocked" or (result or {}).get("error") == "scorer_unavailable":
            scorer = (result or {}).get("scorer") or {}
            detail = scorer.get("fallback_detail") or scorer.get("fallback_reason") or (result or {}).get("error") or "scorer_unavailable"
            return [("", _tr("未完成评分：小模型分类器不可用（{0}）").format(detail))], ""
        return [("", reason or _tr("没有可用候选"))], ""
    first = pair[0]
    rows = [(_tr("胜出"), f"{decision_summary.candidate_label(first)}  {fmt(first['score'])}")]
    if len(pair) > 1:
        second = pair[1]
        lead = (first.get("score") or 0.0) - (second.get("score") or 0.0)
        gap = _tr("并列") if abs(lead) < 0.00005 else _tr("领先 {0}").format(fmt(lead, 4))
        rows.append((_tr("次选"), f"{decision_summary.candidate_label(second)}  {fmt(second['score'])} · {gap}"))
    rows.append((_tr("任务"), decision_summary.task_label(result)))
    candidate = next((c for c in (result or {}).get("ranking") or [] if c.get("id") == first["id"]), {})
    capacity = candidate.get("capacity") if isinstance(candidate.get("capacity"), dict) else {}
    pct = candidate.get("expected_pct", capacity.get("step_pct"))
    cost = (first.get("components") or {}).get("opportunity_cost", capacity.get("opportunity_cost"))
    if capacity.get("window"):
        parts = [_tr("{0} 剩 {1}%").format(capacity["window"], fmt(capacity.get("remaining"), 0))]
        if isinstance(pct, (int, float)):
            parts.append(_tr("本单 ~{0}%").format(fmt(pct, 1)))
        if isinstance(cost, (int, float)):
            parts.append(_tr("机会成本 {0}").format(fmt(cost, 3)))
        rows.append((_tr("额度"), " · ".join(parts)))
    elif reason:
        rows.append((_tr("额度"), clip(reason.split(";", 1)[0], 72)))
    return rows, reason


def pair_ids(pair: list[dict]) -> str:
    """Raw candidate ids of the verdict pair, kept for the tooltip and the plain-text mirror."""
    return "ID  " + " · ".join(str(r.get("id")) for r in pair) if pair else ""


def bundle_labels(bundles: list[dict]) -> list[str]:
    """Display names for an account's bundles; a name shared by two bundles falls back to the bundle id."""
    counts = {}
    for b in bundles:
        counts[b["label"]] = counts.get(b["label"], 0) + 1
    return [b["label"] if counts[b["label"]] == 1 else b["id"] for b in bundles]


def short_badge(text: str) -> str:
    """Show only the status on quota cards and settings rows."""
    return (text or "").split("：", 1)[0].split(": ", 1)[0]


def card_title(card: dict) -> str:
    """A quota card's name; an account in trouble or excluded from dispatch says so on the card itself, with the reason on the hover."""
    if card.get("status") not in ("plan_inactive", "usage_failing", "auth_failed") and not card.get("retry_hold"):
        return f'{card["label"]} · {_tr("不派发")}' if card.get("host_enabled") is False else card["label"]
    return f'{card["label"]} · {short_badge(card.get("badge")).split("，", 1)[0].split(", ", 1)[0]}'


def clip(text, limit: int = 22) -> str:
    text = str(text or "—")
    return text if len(text) <= limit else text[:limit - 1] + "…"


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
        return "\n".join([*table, *extra])
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
    return "\n".join(lines)


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


def matching_preset(tuning: dict, presets: dict) -> str | None:
    """The strategy preset the saved tuning equals, if any."""
    for name, preset in presets.items():
        try:
            if all(abs(float(tuning.get(k)) - float(v)) < 1e-6 for k, v in preset.items()):
                return name
        except (TypeError, ValueError):
            continue
    return None


def selected_chip_layout(widths, available, edit_width, overflow_width=42, gap=6):
    """Largest prefix fitting two compact rows, with overflow and edit controls at its tail."""
    available = max(1, available)
    for count in range(len(widths), -1, -1):
        items = [*widths[:count], *([overflow_width] if count < len(widths) else []), edit_width]
        positions, x, row = [], 0, 0
        for width in items:
            if width > available:
                break
            if x and x + width > available:
                x, row = 0, row + 1
            if row >= 2:
                break
            positions.append((x, row))
            x += width + gap
        else:
            return count, positions, row + 1
    return 0, [(0, 0), (0, 1)], 2


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
                    log_error(exc)
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
            log_error(exc)
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
            log_error(exc)
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

    # ---------- page 1: decision compass ----------

    def build_scoring(self, page):
        tk, ttk = self.tk, self.ttk
        self.header(page, _tr("决策罗盘"), ((_tr("刷新"), self.load_ledger, "SmallAccent.TButton"),))
        # One chart: decision components when available, otherwise the model catalog.
        self.mode = tk.StringVar(value="score")
        body = ttk.Frame(page)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1, uniform="split")
        body.columnconfigure(1, weight=1, uniform="split")
        body.rowconfigure(0, weight=1)

        radar_card = theme.card(body, padding=12)
        radar_card.outer.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        self.radar_title = ttk.Label(radar_card, text=radar_chart.axis_title(), style="CardH3.TLabel")
        self.radar_title.pack(anchor="w")
        self.radar_source = ttk.Label(radar_card, style="CardTiny.TLabel")
        self.radar_source.pack(anchor="w", pady=(2, 8))
        theatre_controls = ttk.Frame(radar_card, style="Card.TFrame")
        theatre_controls.pack(fill="x", pady=(0, 4))
        self.theatre_enabled = tk.BooleanVar(value=True)
        ttk.Checkbutton(theatre_controls, text=_tr("动画"), variable=self.theatre_enabled,
                        command=self.toggle_theatre).pack(side="right")
        replay = ttk.Button(theatre_controls, text="↻", style="Tiny.TButton", width=3,
                            command=self.replay_theatre)
        replay.pack(side="right", padx=(0, 5))
        replay._tooltip_text = _tr("重播 · 点击画面或按 Esc 跳过")
        self.attach_tooltip(replay)
        picks = ttk.Frame(radar_card, style="Card.TFrame")
        self.radar_picks = picks
        picks.pack(fill="x")
        tk.Label(picks, text="●", bg=theme.CARD, fg=theme.ACCENT, font=(theme.FONT, 10)).pack(side="left")
        self.pick_a = ttk.Combobox(picks, state="readonly", width=17)
        self.pick_a.pack(side="left", padx=(3, 10))
        tk.Label(picks, text="◌", bg=theme.CARD, fg=theme.CYAN, font=(theme.FONT, 10, "bold")).pack(side="left")
        self.pick_b = ttk.Combobox(picks, state="readonly", width=17)
        self.pick_b.pack(side="left", padx=(3, 0))
        self.pick_a.bind("<<ComboboxSelected>>", lambda e: self.render_radar())
        self.pick_b.bind("<<ComboboxSelected>>", lambda e: self.render_radar())
        self.verdict = tk.StringVar(value="")  # plain-text mirror of the verdict grid
        # Label/value rows instead of one dense paragraph; the full router reason is the grid's hover tooltip.
        self.verdict_box = ttk.Frame(radar_card, style="Card.TFrame")
        self.verdict_box.pack(side="bottom", fill="x", pady=(4, 0))
        self.verdict_box.columnconfigure(1, weight=1)
        self.verdict_box.bind("<Configure>", lambda e: self.wrap_verdict(e.width))
        self._verdict_width = None
        self.radar = radar_chart.RadarChartCanvas(radar_card, size=theme.px(300), bg=theme.CARD)
        self.radar.pack(fill="both", expand=True, pady=(6, 0))
        self.theatre = decision_theatre.DecisionTheatre(radar_card)

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
        # The button is packed first so it keeps its space at any width; the options flow onto
        # extra rows when the window is too narrow for one line (the minimum size, English).
        self.simulate_button = ttk.Button(options, text=_tr("模拟评分"), style="SmallAccent.TButton", command=self.simulate)
        self.simulate_button.pack(side="right", anchor="n", padx=(8, 0))
        flow = ttk.Frame(options, style="Card.TFrame")
        flow.pack(side="left", fill="x", expand=True)
        self.hours = tk.StringVar(value="2")
        hours_box = ttk.Frame(flow, style="Card.TFrame")
        ttk.Spinbox(hours_box, from_=0.5, to=24, increment=0.5, textvariable=self.hours, width=4).pack(side="left")
        ttk.Label(hours_box, text="h", style="Card.TLabel").pack(side="left", padx=(3, 2))
        self.local_only = tk.BooleanVar()
        self.needs_visual = tk.BooleanVar()
        self.live_scorer = tk.BooleanVar(value=True)
        self._option_items = [hours_box] + [ttk.Checkbutton(flow, text=_tr(text), variable=variable) for text, variable in
                                            (("仅本机", self.local_only), ("需视觉", self.needs_visual))]
        self._option_rows, self._option_layout = [], None
        flow.bind("<Configure>", lambda event: self.reflow_options(flow, event.width))
        self.reflow_options(flow, 10 ** 6)

        lists = theme.card(right, padding=12, fill="both", expand=True, pady=(12, 0))
        top = ttk.Frame(lists, style="Card.TFrame")
        top.pack(fill="x", pady=(0, 8))
        self.list_switch = theme.Segmented(top, (("ledger", _tr("最近决策")), ("breakdown", _tr("候选明细")),
                                                 ("quota", _tr("额度恢复线"))), self.show_list)
        self.list_switch.frame.pack(side="left")
        stack = ttk.Frame(lists, style="Card.TFrame")
        self.list_stack = stack
        stack.pack_propagate(False)
        stack.pack(fill="both", expand=True)
        self.ledger_frame, self.ledger_table = self.tree(stack, (
            ("at", _tr("时间"), 72, "w"), ("kind", _tr("类型"), 108, "w"), ("choice", _tr("胜出"), 132, "w"), ("task", _tr("任务"), 120, "w")), 8)
        self.ledger_table.bind("<<TreeviewSelect>>", lambda e: self.replay_selected())
        self.attach_row_tooltip(self.ledger_table, self.ledger_row_tip)
        self.breakdown_frame, self.breakdown = self.tree(stack, (
            ("id", _tr("候选"), 108, "w"), ("score", _tr("总分"), 48, "e"), ("fit", _tr("契合"), 50, "e"),
            ("opp", _tr("额度成本"), 62, "e"), ("boost", _tr("加成"), 50, "e"), ("note", _tr("状态"), 44, "w")), 8)
        self.breakdown.bind("<<TreeviewSelect>>", lambda e: self.pick_from_breakdown())
        self.attach_row_tooltip(self.breakdown, self.breakdown_row_tip)
        # The quota-line view of the same decision; it fills in on the film's clock while the film plays.
        self.quota_frame = ttk.Frame(stack, style="Card.TFrame")
        self.quota_line = quota_line.QuotaLineCanvas(self.quota_frame)
        self.quota_line.canvas.pack(fill="both", expand=True)
        self.attach_quota_tooltip(self.quota_line, _tr(
            "每个小图是一个额度窗口：从左到右是本周期（右端是重置），从下到上是额度。浅色直线是额度线：按订阅的节奏应该用到哪里。"
            "粗线是实际用量：在额度线下面是绿色，这个窗口被优先使用；超过额度线变红，这个窗口被降权。"
            "点线是在重置前把剩余额度用完所需的节奏。高亮的窗口是本次任务用的。点击画面重播。"))
        self.theatre.on_frame = self.quota_line.follow
        self.show_list("ledger")
        self.set_mode("score")
        self.prefill(lambda: self.data_module().model_table(), self.models_loaded)
        self.prefill(lambda: self.data_module().recent_ledger(30), self.ledger_loaded)
        if self.plan_result:
            self.plan_loaded(self.plan_result)

    def show_list(self, which):
        self.list_switch.set(which)
        with freeze_redraw(getattr(self, "list_stack", None)):
            for name, frame in (("ledger", self.ledger_frame), ("breakdown", self.breakdown_frame),
                                ("quota", self.quota_frame)):
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
        if mode == "dna":
            if not self.models:
                try:
                    self.models = self.data_module().model_table()
                except Exception as exc:
                    log_error(exc)
        self.fill_pickers()
        self.render_radar()

    def models_loaded(self, models):
        self.models = models or {}
        if "scoring" in self.built and not self.plan_result:
            self.set_mode("dna")

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
            labels = display_names.unique_labels(rows, self.data_module().account_label)
            self.pick_ids = dict(zip(labels, (r["id"] for r in rows)))
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
        if hasattr(self, "theatre"):
            self.theatre.stop()
            self.theatre.canvas.pack_forget()
            self.radar_title.pack(anchor="w")
            self.radar_source.pack(anchor="w", pady=(2, 8))
            self.radar_picks.pack(fill="x")
            self.verdict_box.pack(side="bottom", fill="x", pady=(4, 0))
            self.radar.pack(fill="both", expand=True, pady=(6, 0))
        data = self.data_module()
        self.radar_title.configure(text=radar_chart.axis_title(self.mode.get()))
        self.radar_source.configure(text=_tr("模型能力 · 静态价格经济性") if self.mode.get() == "dna"
                                    else _tr("决策候选 · 本次额度成本"))
        if self.mode.get() == "dna":
            if not self.models:
                self.radar.show((), [])
                return
            series, raw = data.dna_series(self.models, self.picked(self.pick_a), self.picked(self.pick_b))
            self.radar.show(radar_chart.DNA_AXES, series, raw)
            self.show_verdict([("", " vs ".join(s["label"] for s in series))])
            return
        if not self.plan_result:
            self.radar.show((), [])
            self.show_verdict([])
            return
        pair = data.decision_pair(self.plan_result, self.picked(self.pick_a), self.picked(self.pick_b))
        series, raw = data.decision_series(pair)
        self.radar.show(radar_chart.SCORE_AXES, series, raw)
        self.show_verdict(*verdict_summary(self.plan_result, pair))

    def verdict_text(self):
        """The verdict as plain text (label: value per line), as the grid shows it for the router's own pair."""
        pair = self.data_module().decision_pair(self.plan_result or {})
        rows, _ = verdict_summary(self.plan_result or {}, pair)
        lines = [f"{label}  {value}" if label else value for label, value in rows]
        return "\n".join(lines + ([pair_ids(pair)] if pair else []))

    def show_verdict(self, rows, reason: str = ""):
        box = self.verdict_box
        for child in box.winfo_children():
            child.destroy()
        self._verdict_values, self._verdict_keys = [], []
        self.verdict.set("\n".join(f"{label}  {value}" if label else value for label, value in rows))
        if self.mode.get() == "score" and self.plan_result:
            pair = self.data_module().decision_pair(self.plan_result, self.picked(self.pick_a), self.picked(self.pick_b))
            if pair:
                self.verdict.set(self.verdict.get() + "\n" + pair_ids(pair))
                self.verdict_strip = decision_summary.SummaryCanvas(box, decision_summary.summary_model(self.plan_result,pair))
                canvas = self.verdict_strip.canvas
                canvas.grid(row=0,column=0,columnspan=2,sticky="ew")
                box.columnconfigure(0,weight=1)
                box.columnconfigure(1,weight=0)
                canvas._tooltip_text = self.verdict.get() + ("\n"+reason if reason else "")
                self.attach_tooltip(canvas)
                return
        box.columnconfigure(0,weight=0)
        box.columnconfigure(1,weight=1)
        for index, (label, value) in enumerate(rows):
            widgets = []
            if label:
                hint = label + (" ⓘ" if reason and index == len(rows) - 1 else "")
                key = self.ttk.Label(box, text=hint, style="CardSub.TLabel")
                key.grid(row=index, column=0, sticky="nw", padx=(0, theme.px(10)), pady=1)
                widgets.append(key)
                self._verdict_keys.append(key)
            text = self.ttk.Label(box, text=value, style="Card.TLabel", font=(theme.FONT, 9), foreground=theme.INK,
                                  justify="left", anchor="w")
            text.grid(row=index, column=1 if label else 0, columnspan=1 if label else 2, sticky="w", pady=1)
            widgets.append(text)
            self._verdict_values.append((text, bool(label)))
            if reason:
                for widget in widgets:
                    widget._tooltip_text = reason
                    self.attach_tooltip(widget)
        self.verdict.set("\n".join(f"{label}  {value}" if label else value for label, value in rows))
        self._verdict_width = None
        self.wrap_verdict(box.winfo_width())

    def wrap_verdict(self, width):
        """Wrap each value to the grid's real width so nothing is clipped at any window size or language."""
        if width <= 1 or width == self._verdict_width:
            return
        self._verdict_width = width
        key_width = max((w.winfo_reqwidth() for w in getattr(self, "_verdict_keys", [])), default=0) + theme.px(10)
        for label, keyed in getattr(self, "_verdict_values", []):
            label.configure(wraplength=max(theme.px(120), width - (key_width if keyed else 0) - theme.px(4)))

    def reflow_options(self, flow, width):
        """Lay the simulation options out left to right, starting a new row when the next one would not fit."""
        if width <= 1:
            return
        gap, layout, row, used = 8, [], 0, 0
        for widget in self._option_items:
            need = widget.winfo_reqwidth()
            if used and used + gap + need > width:
                row, used = row + 1, 0
            layout.append(row)
            used += (gap if used else 0) + need
        if layout == self._option_layout:
            return
        self._option_layout = layout
        while len(self._option_rows) <= row:
            frame = self.ttk.Frame(flow, style="Card.TFrame")
            frame.pack(fill="x", pady=(4 if self._option_rows else 0, 0))
            self._option_rows.append(frame)
        for frame in self._option_rows[row + 1:]:
            frame.pack_forget()
        for index, frame in enumerate(self._option_rows[:row + 1]):
            frame.pack(fill="x", pady=(4 if index else 0, 0))
        for index, widget in enumerate(self._option_items):
            first = index == 0 or layout[index] != layout[index - 1]
            widget.pack_forget()
            widget.pack(in_=self._option_rows[layout[index]], side="left", padx=(0 if first else gap, 0))
            widget.lift()  # the row frames are created later, so they would otherwise stack above the widgets

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
        self.run(lambda: self.data_module().replay(row), self.plan_loaded,
                 tr("正在读取台账中保存的该次决策…", "Loading this decision from the ledger…"))

    def plan_loaded(self, result):
        self.simulate_button.configure(state="normal", text=_tr("模拟评分"))
        self.plan_result = result or {}
        # The quota view keeps a record it cannot read to itself: the rest of the decision always loads.
        self.quota_line.show(self.plan_result)
        if self.quota_line.error is not None:
            log_error(self.quota_line.error)
        data = self.data_module()
        rows = data.candidate_rows(self.plan_result)
        if self.plan_result.get("theatre_source") == "unreplayable":
            self.breakdown_rows = rows
            self.breakdown.delete(*self.breakdown.get_children())
            self.status.set(self.plan_result["replay_note"])
            return
        self.breakdown_rows = rows
        self.breakdown.delete(*self.breakdown.get_children())
        self.breakdown_labels = display_names.unique_labels(rows, data.account_label)
        for index, row in enumerate(rows):
            excluded = row["excluded"]
            winner = row["id"] == self.plan_result.get("choice")
            note = _tr("拦截") if excluded else _tr("胜出") if winner else _tr("可选")
            self.breakdown.insert("", "end", iid=str(index), tags=("muted",) if excluded else ("win",) if winner else (),
                                  values=(self.breakdown_labels[index], fmt(row["score"]), fmt(row["fit"], 2),
                                          fmt(row["opportunity_cost"], 3), fmt(row["boost"], 2, True),
                                          note))
        if self.mode.get() != "score":
            self.set_mode("score")
        else:
            self.fill_pickers()
            self.render_radar()
        choice = self.plan_result.get("choice")
        winner = next((label for label, row in zip(self.breakdown_labels, rows) if row["id"] == choice), choice or "—")
        scorer = self.plan_result.get("scorer") or {}
        if self.plan_result.get("status") == "blocked" or self.plan_result.get("error") == "scorer_unavailable":
            detail = scorer.get("fallback_detail") or scorer.get("fallback_reason") or self.plan_result.get("error") or "scorer_unavailable"
            message = _tr('未完成评分：小模型分类器不可用（{0}）').format(f"{detail}")
        else:
            message = _tr('评分完成：{0} 个候选，胜出 {1}').format(f'{len(rows)}', f"{winner}")
            if self.plan_result.get("theatre_source") == "recomputed":
                message += tr("（按当前额度重算）", " (recomputed with current quota)")
            if scorer:
                source = scorer.get("source")
                message += (_tr('（小模型 {0} · {1}ms）').format(f"{scorer.get('model')}", f"{scorer.get('duration_ms')}") if source == "system_one"
                            else _tr('（小模型不可用：{0}，请主会话自主选择）').format(f"{scorer.get('fallback_detail') or scorer.get('fallback_reason')}"))
        self.status.set(message)
        self.replay_theatre()

    def replay_theatre(self):
        if not self.plan_result or not hasattr(self, "theatre"):
            return
        self.radar.canvas.pack_forget()
        self.radar_title.pack_forget()
        self.radar_source.pack_forget()
        self.radar_picks.pack_forget()
        self.verdict_box.pack(side="bottom", fill="x", pady=(4, 0))
        self.theatre.canvas.pack(fill="both", expand=True, pady=(6, 0))
        self.theatre.canvas.update_idletasks()
        self.theatre.play(self.plan_result, self.theatre_enabled.get())

    def toggle_theatre(self):
        if not self.theatre_enabled.get():
            self.theatre.skip()

    def pick_from_breakdown(self):
        selected = self.breakdown.selection()
        if not selected or not selected[0].isdigit():
            return
        index = int(selected[0])
        row = self.breakdown_rows[index]
        if row["excluded"]:
            self.status.set(_tr('{0} 被拦截：{1}').format(f"{self.breakdown_labels[index]}", f"{'；'.join(row['excluded'])}"))
            return
        # Clicking an eligible row makes it the comparison series against the primary.
        label = next((label for label, bundle in getattr(self, "pick_ids", {}).items() if bundle == row["id"]), None)
        if self.mode.get() == "score" and label in self.pick_a.cget("values") and row["id"] != self.picked(self.pick_a):
            self.pick_b.set(label)
            self.render_radar()

    def load_ledger(self):
        self.run(lambda: self.data_module().recent_ledger(30), self.ledger_loaded)

    def ledger_loaded(self, rows):
        self.ledger_rows = rows or []
        self.ledger_table.delete(*self.ledger_table.get_children())
        for index, row in enumerate(self.ledger_rows):
            s = self.data_module().ledger_summary(row)
            self.ledger_table.insert("", "end", iid=str(index),
                                     values=(s["at"], s["kind_label"], s["choice_label"], s["objective"]))
        # Open on the latest real decision so the radar is never empty on first sight.
        if self.ledger_rows and not self.plan_result and not self.auto_replayed:
            self.auto_replayed = True
            self.prefill(self.latest_decision, self.auto_selected)

    def latest_decision(self):
        """The newest ledger row whose replay names a winner; a None result has no film to play."""
        for index, row in enumerate(self.ledger_rows[:AUTO_REPLAY_ROWS]):
            try:
                result = self.data_module().replay(row)
            except Exception as exc:
                log_error(exc)
                continue
            if result and result.get("choice"):
                return index, result
        return None

    def auto_selected(self, found):
        if found is None:
            return
        index, result = found
        self._suppress_next_select = True
        self.ledger_table.selection_set(str(index))
        self.ledger_table.see(str(index))
        self.plan_loaded(result)

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
        frame, self.worker_table = self.tree(workers, (
            ("agent", "Agent", 70, "w"), ("title", _tr("任务"), 200, "w"), ("bundle", _tr("模型"), 110, "w"),
            ("state", _tr("状态"), 66, "w"), ("compacted", _tr("已压缩"), 62, "e"), ("context", _tr("窗口"), 58, "e"),
            ("total", _tr("累计"), 62, "e"), ("bar", _tr("占 2M 上限"), 130, "w")), 4)
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
        self.run(lambda: (self.cached_account_cards(force), data.workers()), self.quota_loaded, _tr("正在读取账号水位…"))

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
                self.quota_updated.configure(
                    text=f"{tr('剩余额度%', 'Remaining %')} · {tr('读数于', 'Read at')} {read_stamp} ↻")
            else:
                stamp = time.strftime("%H:%M:%S")
                self.quota_updated.configure(
                    text=f"{tr('剩余额度%', 'Remaining %')} · {tr('更新于', 'Updated')} {stamp} ↻")
        self.sync_quota_geometry()

    def quota_loaded(self, value):
        cards, workers = value
        self.worker_rows = workers
        if cards is not getattr(self, "_rendered_quota_cards", None):
            self.quota_cards_loaded(cards)
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

    # ---------- page 3: model settings ----------

    def build_policy(self, page):
        ttk = self.ttk
        bar = self.header(page, _tr("模型设置"))
        self.setting_switch = theme.Segmented(bar, SETTINGS, self.show_setting, track=TRACK)
        self.setting_switch.frame.pack(side="right")
        self.policy_stack = ttk.Frame(page)
        self.policy_stack.pack(fill="both", expand=True)
        self.policy_stack.pack_propagate(False)
        self.setting_stack = page_stack.PageStack(self.policy_stack)
        self.setting_frames = {key: ttk.Frame(self.policy_stack) for key, _ in SETTINGS}
        self.settings_built = set()
        self.current_setting = None
        self.show_setting(getattr(self, "_initial_setting", None) or "models")

    def show_setting(self, key):
        key = SETTING_ALIASES.get(key, key)
        if key != getattr(self, "current_setting", None):
            self.close_model_menu()
        if "policy" not in self.built:
            self.show("policy")
        if key not in self.setting_frames or key == self.current_setting:
            return
        self.setting_switch.set(key)
        revisit = key in self.settings_built
        if not revisit:
            self.settings_built.add(key)
            getattr(self, f"build_setting_{key}")(self.setting_frames[key])
        self.current_setting = key
        self.setting_stack.show(self.setting_frames[key])
        if revisit and key == "models":
            if time.monotonic() - getattr(self, "_models_loaded_at", 0.0) >= 5.0:
                self.load_models()

    open_setting = show_setting

    def load_policy(self):
        for key in getattr(self, "settings_built", ()):
            getattr(self, f"load_{key}")()

    def build_setting_models(self, frame):
        ttk = self.ttk
        self.build_cli_toolbar(frame)
        # The priority strip owns its one line; the table scrolls inside the remaining space.
        strip = theme.card(frame, padding=(12, 8), fill="x", side="bottom", pady=(8, 0))
        ttk.Label(strip, text=_tr("临时优先加权"), style="Card.TLabel").pack(side="left", padx=(0, 8))
        quick = ttk.Frame(strip, style="Card.TFrame")
        quick.pack(side="left")
        self.fill_priority_buttons(quick, "Tiny.TButton", (0, 4), "+ ")
        ttk.Button(strip, text=_tr("自定义…"), style="Small.TButton", command=self.open_boost_editor).pack(side="right")
        self.boost_chips = ttk.Frame(strip, style="Card.TFrame")
        self.boost_chips.pack(side="left", fill="x", expand=True, padx=(6, 4))
        box = theme.card(frame, padding=(12, 6), fill="both", expand=True, pady=(8, 0))
        titles = ttk.Frame(box, style="Card.TFrame")
        titles.pack(fill="x", pady=(0, 4))
        ttk.Label(titles, text="Harness", style="CardTiny.TLabel", width=19).pack(side="left")
        ttk.Label(titles, text=_tr("账号 / 状态"), style="CardTiny.TLabel", width=20).pack(side="left")
        ttk.Label(titles, text=_tr("已选模型 · 推理档位自动选择"), style="CardTiny.TLabel", width=1).pack(side="left", fill="x", expand=True)
        self.model_scroll = ScrollFrame(box, bg=theme.CARD)
        self.model_rows = self.model_scroll.body
        self.model_rows.columnconfigure(2, weight=1)
        self.model_shape = None
        self.model_widgets = []
        self._catalog_refreshing = False
        self._models_prefilled = False
        self.prefill(lambda: self.data_module().harness_model_rows(), self.models_rows_loaded)
        self.load_models(skip_rows=self._models_prefilled)

    def load_models(self, skip_rows: bool = False):
        if not skip_rows:
            self.run(lambda: self.data_module().harness_model_rows(), self.models_rows_loaded)
        self.load_cli()
        self.load_boost()
        self.refresh_model_availability()

    def refresh_model_availability(self, force=False):
        if getattr(self, "_catalog_refreshing", False) or self.closing:
            return
        self._catalog_refreshing = True
        self.model_refresh_button.state(["disabled"])
        generation = self._generation
        def work():
            try:
                return self.data_module().refresh_model_catalog(force=force)
            except Exception as error:
                log_error(error)
                return self.data_module().harness_model_rows()
        def done(rows):
            self._catalog_refreshing = False
            self.model_refresh_button.state(["!disabled"])
            self.models_rows_loaded(rows)
            self.root.after(600000, lambda: self.refresh_model_availability() if generation == self._generation else None)
        self.run(work, done, long=True)

    def models_rows_loaded(self, rows):
        self._models_prefilled = True
        self._models_loaded_at = time.monotonic()
        shape = tuple((row["account"], row["harness"]) for row in rows)
        if shape != getattr(self, "model_shape", None):
            with freeze_redraw(self.model_rows):
                for child in self.model_rows.winfo_children():
                    child.destroy()
                self.model_widgets = [self.build_model_row(index, row) for index, row in enumerate(rows)]
                self.model_shape = shape
        self.model_inventory = rows
        for widgets, row in zip(self.model_widgets, rows):
            widgets["on"].set(bool(row["enabled"]))
            theme.set_pill(widgets["pill"], short_badge(row["badge"]), row["kind"])
            widgets["row"] = row
            pattern = row.get("cwd_pattern") or row.get("cwd_regex") or ""
            widgets["cwd"].configure(text="⌁•" if pattern else "⌁")
            widgets["cwd"]._tooltip_text = pattern or _tr("不限目录")
            self.fill_selected_models(widgets, row)
            self.fill_harness_version(widgets, row)
        menu = getattr(self, "model_menu", None)
        if menu is not None and menu.winfo_exists():
            row = next((row for row in rows if (row["account"], row["harness"]) == self.model_menu_key), None)
            if row:
                self.fill_model_menu(row)

    def build_model_row(self, index, row):
        tk, ttk = self.tk, self.ttk
        grid = index * 2
        box = self.model_rows
        if index:
            self.divider(box, row=grid - 1, column=0, columnspan=3, pady=1)
        harness = ttk.Frame(box, style="Card.TFrame")
        harness.grid(row=grid, column=0, sticky="nw", pady=5, padx=(0, 10))
        on = tk.BooleanVar(value=row["enabled"])
        check = ttk.Checkbutton(harness, text=row["harness_label"], variable=on,
                                command=lambda a=row["account"], h=row["harness"], v=on: self.toggle_harness_row(a, h, v.get()))
        check.pack(anchor="w")
        version = ttk.Label(harness, style="CardTiny.TLabel")
        version.pack(anchor="w", padx=(20, 0))
        account = ttk.Frame(box, style="Card.TFrame")
        account.grid(row=grid, column=1, sticky="nw", pady=5, padx=(0, 14))
        name = ttk.Frame(account, style="Card.TFrame")
        name.pack(fill="x")
        ttk.Label(name, text=row["account_label"], style="Card.TLabel").pack(side="left")
        cwd = ttk.Button(name, text="⌁", width=2, style="Tiny.TButton",
                         command=lambda: self.open_account_cwd(row["account"]))
        cwd.pack(side="left")
        self.attach_tooltip(cwd)
        status = ttk.Frame(account, style="Card.TFrame")
        status.pack(anchor="w")
        pill = theme.pill(status, "", "idle")
        pill.pack(side="left")
        if row["harness"] == "grok-bot":
            ttk.Button(status, text="↗", width=2, style="Tiny.TButton", command=lambda: self.show("plugins")).pack(side="left", padx=3)
        selection = ttk.Frame(box, style="Card.TFrame")
        selection.grid(row=grid, column=2, sticky="new", pady=7)
        dropdown = ttk.Button(selection, text="✎ ▾", style="Tiny.TButton")
        dropdown.configure(command=lambda: self.open_model_menu(row["account"], row["harness"], dropdown))
        overflow = ttk.Button(selection, style="Tiny.TButton", command=lambda: dropdown.invoke())
        widgets = {"on": on, "check": check, "version": version, "pill": pill, "chips": selection,
                   "dropdown": dropdown, "overflow": overflow, "cwd": cwd, "row": row, "selected_models": [], "chip_labels": []}
        def resize(event):
            if event.width <= 1 or event.width == widgets.get("_last_chip_width"):
                return
            widgets["_last_chip_width"] = event.width
            pending = getattr(selection, "_chip_after", None)
            if pending:
                selection.after_cancel(pending)
            selection._chip_after = selection.after(60, lambda: self.layout_selected_chips(widgets) if selection.winfo_exists() else None)
        selection.bind("<Configure>", resize)
        selection.bind("<Button-1>", lambda event: (dropdown.invoke(), "break")[1] if event.widget is selection else None)
        return widgets

    def fill_selected_models(self, widgets, row):
        holder = widgets["chips"]
        selected = [model for model in row["models"] if model.get("routed", True) and (model["enabled"] or model["partial"])]
        widgets["selected_models"] = selected
        signature = tuple((model["model"], model["model_label"], model["partial"]) for model in selected)
        if signature != widgets.get("chip_signature"):
            for child in widgets["chip_labels"]:
                child.destroy()
            widgets["chip_labels"] = []
            for model in selected:
                label = self.tk.Label(holder, text=("◐ " if model["partial"] else "") + model["model_label"],
                          bg=theme.ACCENT_SOFT if row["enabled"] else theme.BG,
                          fg=theme.ACCENT if row["enabled"] else theme.FAINT,
                          padx=5, pady=2, font=(theme.FONT, 9))
                label.bind("<Button-1>", lambda event: (widgets["dropdown"].invoke(), "break")[1])
                widgets["chip_labels"].append(label)
            if not selected:
                widgets["chip_labels"].append(self.ttk.Label(holder, text=_tr("未选模型"), style="CardTiny.TLabel"))
            widgets["chip_signature"] = signature
        for label in widgets["chip_labels"]:
            if label.winfo_class() == "Label":
                label.configure(fg=theme.ACCENT if row["enabled"] else theme.FAINT,
                                bg=theme.ACCENT_SOFT if row["enabled"] else theme.BG)
        self.layout_selected_chips(widgets)

    def layout_selected_chips(self, widgets):
        holder = widgets["chips"]
        holder._chip_after = None
        labels, edit, overflow = widgets["chip_labels"], widgets["dropdown"], widgets["overflow"]
        live_w = holder.winfo_width()
        if live_w > 1:
            widgets["_last_chip_width"] = live_w
        width = live_w if live_w > 1 else theme.px(420)
        sizes = [label.winfo_reqwidth() for label in labels]
        overflow.configure(text=f"+{len(labels)}")
        count, positions, rows = selected_chip_layout(sizes, width, edit.winfo_reqwidth(), overflow.winfo_reqwidth(), theme.px(5))
        for label in labels:
            label.place_forget()
        overflow.place_forget()
        height = max([edit.winfo_reqheight(), *(label.winfo_reqheight() for label in labels)], default=theme.px(24))
        controls = labels[:count]
        if count < len(labels):
            overflow.configure(text=f"+{len(labels) - count}")
            controls = [*controls, overflow]
        for widget, (x, row) in zip([*controls, edit], positions):
            widget.place(x=x, y=row * (height + theme.px(3)))
        holder.configure(height=rows * (height + theme.px(3)))
        widgets["visible_chips"] = count

    def close_model_menu(self):
        menu = getattr(self, "model_menu", None)
        if menu is not None and menu.winfo_exists():
            menu.destroy()
        self.model_menu = None

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

    def ledger_row_tip(self, iid):
        rows = getattr(self, "ledger_rows", ())
        if not iid.isdigit() or int(iid) >= len(rows):
            return ""
        row = rows[int(iid)]
        return f"{row.get('kind') or '—'} · {row.get('choice') or '—'}"

    def breakdown_row_tip(self, iid):
        rows = getattr(self, "breakdown_rows", ())
        if not iid.isdigit() or int(iid) >= len(rows):
            return ""
        row = rows[int(iid)]
        return " · ".join(str(v) for v in (row["id"], row.get("account")) if v)

    def attach_quota_tooltip(self, view, legend):
        """Hover text for the quota-line view: the mark under the pointer in words, else how to read the picture."""
        canvas, current = view.canvas, {"text": None}
        show, hide = self.attach_tooltip(canvas, bind=False)

        def move(event):
            text = view.tip_at(event.x, event.y) or legend
            if text == current["text"]:
                return
            hide()
            current["text"] = canvas._tooltip_text = text
            show(event)

        def leave(_event=None):
            current["text"] = None
            hide()

        def replay(_event=None):
            leave()
            self.replay_theatre()
        canvas.bind("<Motion>", move, add="+")
        canvas.bind("<Leave>", leave, add="+")
        canvas.bind("<Button-1>", replay, add="+")

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

    def open_account_cwd(self, account):
        self.close_model_menu()
        old = getattr(self, "cwd_dialog", None)
        if old is not None and old.winfo_exists():
            old.destroy()
        window = self.cwd_dialog = self.tk.Toplevel(self.root)
        theme.apply(window, _tr("账号目录限制"), "570x310", (520, 290))
        window.transient(self.root)
        width, height = theme.px(570), theme.px(310)
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - width) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - height) // 2)
        window.geometry(f"{width}x{height}+{x}+{y}")
        box = theme.card(window, padding=14, fill="both", expand=True)
        self.ttk.Label(box, text=self.data_module().account_label(account), style="CardH3.TLabel").pack(anchor="w")
        self.ttk.Label(box, text=_tr("同一账号的所有 Harness 共用此限制。留空允许全部目录。"),
                       style="CardTiny.TLabel").pack(anchor="w", pady=(4, 10))
        self.cwd_pattern = self.tk.StringVar(value=self.data_module().read_account_cwd(account))
        self.ttk.Entry(box, textvariable=self.cwd_pattern, font=(self.mono, 10)).pack(fill="x")
        self.ttk.Label(box, text=_tr("路径统一为 /，匹配忽略大小写。"), style="CardTiny.TLabel").pack(anchor="w", pady=(4, 8))
        self.cwd_preview = self.tk.StringVar()
        self.cwd_preview_label = self.ttk.Label(box, textvariable=self.cwd_preview, style="Card.TLabel", justify="left")
        self.cwd_preview_label.pack(anchor="w", fill="x", pady=6)
        buttons = self.ttk.Frame(box, style="Card.TFrame")
        buttons.pack(side="bottom", fill="x")
        self.cwd_save_button = self.ttk.Button(buttons, text=_tr("保存"), style="SmallAccent.TButton",
                                              command=lambda: self.save_account_cwd(account))
        self.cwd_save_button.pack(side="right")
        self.ttk.Button(buttons, text=_tr("取消"), style="Small.TButton", command=window.destroy).pack(side="right", padx=8)
        self.cwd_pattern.trace_add("write", lambda *args: self.update_cwd_preview())
        self.update_cwd_preview()

    def update_cwd_preview(self):
        paths = [str(Path.cwd()), "D:/projects/example", "C:/Temp"]
        try:
            preview = self.data_module().preview_account_cwd(self.cwd_pattern.get(), paths)
            self.cwd_preview.set("\n".join(("✓ " if matches else "— ") + directory for directory, matches in preview))
            self.cwd_preview_label.configure(foreground=theme.TEXT)
            self.cwd_save_button.state(["!disabled"])
        except ValueError as error:
            self.cwd_preview.set(_tr("无效正则：{0}").format(str(error)))
            self.cwd_preview_label.configure(foreground=theme.RED)
            self.cwd_save_button.state(["disabled"])

    def save_account_cwd(self, account):
        pattern = self.cwd_pattern.get()
        def done(message):
            self.status.set(message)
            self.cwd_dialog.destroy()
            self.after_model_change()
        self.run(lambda: self.data_module().save_account_cwd(account, pattern), done)

    def open_model_menu(self, account, harness, anchor):
        self.close_model_menu()
        row = next((row for row in self.model_inventory if (row["account"], row["harness"]) == (account, harness)), None)
        if row is None:
            return
        menu = self.model_menu = self.tk.Toplevel(self.root)
        menu.transient(self.root)
        menu.overrideredirect(True)
        menu.attributes("-topmost", True)
        menu.configure(bg=theme.BORDER, padx=1, pady=1)
        self.model_menu_key = (account, harness)
        width, height = theme.px(380), theme.px(min(360, 95 + 31 * max(1, len(row["models"]))))
        x = min(anchor.winfo_rootx(), self.root.winfo_rootx() + self.root.winfo_width() - width - 8)
        y = anchor.winfo_rooty() + anchor.winfo_height()
        if y + height > self.root.winfo_screenheight():
            y = max(0, anchor.winfo_rooty() - height)
        menu.geometry(f"{width}x{height}+{max(0, x)}+{y}")
        menu.bind("<Escape>", lambda event: self.close_model_menu())
        self.fill_model_menu(row)
        menu.lift()
        menu.focus_set()

    def fill_model_menu(self, row):
        tk, ttk = self.tk, self.ttk
        menu = self.model_menu
        for child in menu.winfo_children():
            child.destroy()
        head = ttk.Frame(menu, style="Card.TFrame", padding=(8, 6))
        head.pack(fill="x")
        self.model_menu_actions = {}
        for action, label in (("all", "全选"), ("invert", "反选"), ("clear", "清空")):
            button = ttk.Button(head, text=_tr(label), style="Tiny.TButton",
                                command=lambda mode=action: self.bulk_model_selection(row["account"], row["harness"], mode))
            button.pack(side="left", padx=(0, 5))
            button.state(["!disabled"] if row["enabled"] and any(m.get("routed", True) for m in row["models"]) else ["disabled"])
            self.model_menu_actions[action] = button
        ttk.Button(head, text="×", width=2, style="Tiny.TButton", command=self.close_model_menu).pack(side="right")
        hint = _tr("监测失败，使用已配置模型") if row.get("models_probe_failed") else (
            _tr("上次检测：{0}").format(time.strftime("%H:%M", time.localtime(row["models_checked_at"])))
            if row.get("models_checked_at") else _tr("后台监测中，先显示已配置模型"))
        ttk.Label(menu, text=hint, style="CardTiny.TLabel", padding=6).pack(side="bottom", fill="x")
        scroll = ScrollFrame(menu, bg=theme.CARD)
        self.model_menu_items = []
        if not row["models"]:
            ttk.Label(scroll.body, text=_tr("尚未发现可用模型"), style="CardTiny.TLabel").pack(anchor="w", padx=10, pady=8)
        for model in sorted(row["models"], key=lambda item: not (item["enabled"] or item["partial"])):
            routed = model.get("routed", True)
            marks = [_tr(tag) for tag in model.get("tags", [])]
            if model.get("availability") == "missing":
                marks.append(_tr("未检测到"))
            if model.get("launch_unavailable"):
                marks.append(model["launch_unavailable"])
            text = model["model_label"] + (" · " + " · ".join(marks) if marks else "")
            var = tk.BooleanVar(value=model["enabled"])
            if model["partial"]:
                # Some efforts enabled: ◐ stands in for the checkbox (ttk's alternate state is invisible under the
                # card theme). Clicking enables every effort; hovering lists which efforts are on and off.
                check = ttk.Label(scroll.body, text="◐ " + text, style="Card.TLabel", cursor="hand2")
                check.pack(anchor="w", fill="x", padx=(12, 10), pady=4)
                if routed and row["enabled"]:
                    check.bind("<Button-1>", lambda _e, m=model["model"]: self.toggle_model(row["account"], row["harness"], m, True), add="+")
                check._tooltip_text = _tr("已启用：{0}；未启用：{1}").format(
                    ", ".join(model.get("efforts_on", [])) or "-", ", ".join(model.get("efforts_off", [])) or "-")
                self.attach_tooltip(check)
                self.model_menu_items.append((model, var, check))
                continue
            check = ttk.Checkbutton(scroll.body, text=text, variable=var,
                                    command=lambda m=model["model"], v=var: self.toggle_model(row["account"], row["harness"], m, v.get()))
            check.pack(anchor="w", fill="x", padx=10, pady=4)
            check.state(["!disabled"] if routed and row["enabled"] else ["disabled"])
            self.model_menu_items.append((model, var, check))

    def bulk_model_selection(self, account, harness, action):
        self.run(lambda: self.data_module().set_harness_models(account, harness, action), self.say(self.after_model_change))

    def fill_harness_version(self, widgets, row):
        provider = {"antigravity-resilient": "agy"}.get(row["harness"], row["harness"])
        item = next((item for item in getattr(self, "cli_inventory", [])
                     if row["account"] in item.get("providers", []) and item["id"].split("#")[0] == provider), None)
        if item is None:
            item = next((item for item in getattr(self, "cli_inventory", []) if item["id"] == provider), {})
        widgets["version"].configure(text=f"v{item['version']}" if item.get("version") else "")

    def after_model_change(self):
        self._cards_cache = None
        self.run(lambda: self.data_module().harness_model_rows(), self.models_rows_loaded)
        if "quota" in self.built:
            self.load_quota(True)

    def toggle_harness_row(self, account, harness, enabled):
        self.run(lambda: self.data_module().toggle_harness(harness, account, enabled), self.say(self.after_model_change))

    def toggle_model(self, account, harness, model, enabled=None):
        def work():
            selected = self.data_module().toggle_harness_model(account, harness, model, enabled)
            return _tr("已启用") if selected else _tr("已停用")
        self.run(work, self.say(self.after_model_change))

    # temporary priority

    def open_boost_editor(self):
        window = getattr(self, "boost_dialog", None)
        if window is not None and window.winfo_exists():
            window.lift()
            return
        self.boost_dialog = window = self.tk.Toplevel(self.root)
        theme.apply(window, _tr("设定预期重置时间"), "900x400", (760, 360))
        window.transient(self.root)
        self.build_setting_boost(window)
        self.load_boost()

    def cancel_boost(self, rule):
        import router_priority
        self.run(lambda: router_priority.cancel(rule["id"], rule["revision"]), self.say(self.load_boost))

    def build_setting_boost(self, frame):
        ttk = self.ttk
        box = theme.card(frame, padding=14, fill="both", expand=True)
        quick = ttk.Frame(box, style="Card.TFrame")
        quick.pack(fill="x")
        ttk.Label(quick, text=_tr("一键优先"), style="CardH3.TLabel").pack(side="left", padx=(0, 10))
        self.fill_priority_buttons(quick, "Small.TButton", (0, 6))
        form = ttk.Frame(box, style="Card.TFrame")
        form.pack(fill="x", pady=(12, 10))
        ttk.Label(form, text=_tr("设定预期重置时间"), style="CardH3.TLabel").pack(side="left", padx=(0, 10))
        self.boost_target = ttk.Combobox(form, state="readonly", width=24)
        self.boost_target.pack(side="left")
        self.boost_hours = ttk.Combobox(form, state="readonly", width=4, values=["1h", "3h", "6h", "12h", "24h"])
        self.boost_hours.set("6h")
        self.boost_hours.pack(side="left")
        ttk.Button(form, text=_tr("添加"), style="SmallAccent.TButton", command=self.add_boost).pack(side="left", padx=10)
        ttk.Label(box, text=_tr("后重置 · 越近越优先，额度回升后自动撤销"), style="CardTiny.TLabel").pack(fill="x")
        bottom = ttk.Frame(box, style="Card.TFrame")
        bottom.pack(side="bottom", fill="x", pady=(8, 0))
        ttk.Button(bottom, text=_tr("取消所选规则"), style="Small.TButton", command=self.cancel_rule).pack(side="right")
        table, self.rule_table = self.tree(box, (("label", _tr("账号 / 模型"), 180, "w"), ("boost", _tr("加成"), 160, "e"),
                                                 ("until", _tr("预期重置 / 倒计时"), 240, "w"), ("reset", _tr("重置撤销"), 70, "w"),
                                                 ("intent", _tr("来源"), 160, "w")), 6)
        table.pack(fill="both", expand=True)
        data = self.data_module()
        self.prefill(lambda: (data.overrides(), data.boost_targets()), self.boost_loaded)

    def load_boost(self):
        self._boost_refreshed = time.monotonic()
        data = self.data_module()
        self.run(lambda: (data.overrides(), data.boost_targets()), self.boost_loaded)

    def boost_loaded(self, value):
        rules, targets = value
        data = self.data_module()
        self.rule_rows = rules
        if hasattr(self, "boost_chips") and self.boost_chips.winfo_exists():
            for child in self.boost_chips.winfo_children():
                child.destroy()
            for rule in rules[:2]:
                label = data.model_label(rule.get("model")) if rule.get("model") else data.account_label(rule.get("account"))
                detail = (f" {rule.get('remaining_hours', 0):.1f}h +{(rule.get('boost') or 0):.2f}"
                          if rule.get('remaining_hours') is not None else "")
                self.ttk.Button(self.boost_chips, text=f"{clip(label, 10)}{detail} ×", style="Tiny.TButton",
                                command=lambda item=rule: self.cancel_boost(item)).pack(side="left", padx=(0, 3))
            if len(rules) > 2:
                self.ttk.Button(self.boost_chips, text=f"+{len(rules) - 2}", style="Tiny.TButton",
                                command=self.open_boost_editor).pack(side="left")
        if not hasattr(self, "rule_table") or not self.rule_table.winfo_exists():
            return
        self.rule_table.delete(*self.rule_table.get_children())
        for index, rule in enumerate(rules):
            self.rule_table.insert("", "end", iid=str(index), values=(
                rule["label"], fmt(rule["boost"], 2, True),
                f"{rule['until']} · {rule.get('countdown', '')}",
                _tr("是") if rule["stop_on_reset"] else _tr("否"), rule["intent"]))
        if not rules:
            self.rule_table.insert("", "end", values=(_tr("当前没有生效规则"), "", "", "", ""), tags=("muted",))
        self.boost_pairs = [(a, None) for a in dict.fromkeys(a for a, _ in targets)] + targets
        self.boost_target.configure(values=[
            f"{data.account_label(a)} / {data.model_label(m) if m else _tr('全部模型')}" for a, m in self.boost_pairs])
        if not self.boost_target.get() and self.boost_pairs:
            self.boost_target.current(0)

    def fill_priority_buttons(self, frame, style, padx, prefix=""):
        """One-click reset-priority buttons for the targets the router derives from this fleet."""
        import router_priority

        def work():
            allocator, _ = router_priority.modules()
            return router_priority.targets(allocator, allocator.load_accounts())

        def done(targets):
            for target in targets or ():
                self.ttk.Button(frame, text=f"{prefix}{target['label']}", style=style,
                                command=lambda a=target["account"], m=target["model"]: self.enable_priority(a, m)
                                ).pack(side="left", padx=padx)
        self.run(work, done)

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
        hours = int(self.boost_hours.get().rstrip("h"))
        self.run(lambda: self.data_module().add_boost(account, model, hours=hours), self.say(self.load_boost))

    def cancel_rule(self):
        selected = self.rule_table.selection()
        if not selected or not getattr(self, "rule_rows", None) or not selected[0].isdigit():
            self.status.set(_tr("先选择一条规则"))
            return
        rule = self.rule_rows[int(selected[0])]
        import router_priority

        self.run(lambda: router_priority.cancel(rule["id"], rule["revision"]), self.say(self.load_boost))

    # strategy & parameters

    def build_setting_strategy(self, frame):
        footer = self.ttk.Frame(frame)
        footer.pack(side="bottom", fill="x", pady=(10, 0))
        self.strategy_save = self.ttk.Button(footer, text=_tr("保存额度阈值"), style="SmallAccent.TButton", command=self.save_tuning)
        self.strategy_save.pack(side="right")
        self.ttk.Button(footer, text=_tr("恢复默认参数"), style="Small.TButton", command=self.restore_tuning).pack(side="left")
        scroll = ScrollFrame(frame)
        self.strategy_scroll = scroll
        self.build_setting_tuning(scroll.body)

    def build_setting_tuning(self, frame):
        tk, ttk = self.tk, self.ttk
        from router_data import STRATEGY_PRESETS

        box = theme.card(frame, padding=STRATEGY_PAD, fill="x")
        ttk.Label(box, text=_tr("策略与阈值"), style="CardH2.TLabel").pack(anchor="w", pady=(0, 8))
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
        numbers = ttk.Frame(box, style="Card.TFrame")
        numbers.pack(fill="x")
        ttk.Label(numbers, text=_tr("额度阈值"), style="CardH3.TLabel").grid(row=0, column=0, columnspan=2, sticky="w",
                                                                    pady=(0, 8))
        self.tuning_vars = {}
        for row, (key, label) in enumerate((("safety_margin_pct", _tr("周安全余量 %")), ("short_window_floor_pct", _tr("短窗底线 %")),
                                            ("rotate_after_carried_tokens", _tr("上下文轮换阈值"))), start=1):
            ttk.Label(numbers, text=label, style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=4)
            var = tk.StringVar()
            ttk.Entry(numbers, textvariable=var, width=11, font=(self.mono, STRATEGY_FIELD)).grid(
                row=row, column=1, sticky="e", pady=4)
            self.tuning_vars[key] = var
        numbers.columnconfigure(0, weight=1)

        self.prefill(lambda: self.data_module().read_tuning(), self.tuning_loaded)

    def load_strategy(self):
        self.run(lambda: self.data_module().read_tuning(), self.tuning_loaded)

    def tuning_loaded(self, tuning):
        from router_data import STRATEGY_PRESETS

        for key, var in self.tuning_vars.items():
            val = tuning.get(key)
            var.set("" if val is None else str(val))
        active = matching_preset(tuning, STRATEGY_PRESETS)
        for name, button in self.preset_buttons.items():
            button.configure(style="SmallAccent.TButton" if name == active else "Small.TButton")

    def apply_strategy_preset(self, name):
        from router_data import STRATEGY_PRESETS

        name = self.data_module().strategy_preset_key(name)
        preset = STRATEGY_PRESETS.get(name)
        if not preset:
            return
        self.tuning_loaded(preset)
        self.status.set(_tr("已载入预设「{0}」，保存后生效").format(_tr(name)))

    def save_tuning(self):
        values = {key: var.get().strip() for key, var in self.tuning_vars.items() if var.get().strip()}
        self.run(lambda: self.data_module().save_tuning(values), self.say(self.load_strategy))

    def restore_tuning(self):
        from tkinter import messagebox

        if messagebox.askyesno(TITLE, _tr("恢复默认路由参数？"), parent=self.root):
            self.run(lambda: self.data_module().restore_tuning(), self.say(self.load_strategy))

    # system

    def build_setting_system(self, frame):
        tk, ttk = self.tk, self.ttk
        scroll = ScrollFrame(frame)
        frame = scroll.body
        self.build_version_card(frame)
        box = theme.card(frame, padding=14, fill="x")
        box.columnconfigure(1, weight=1)
        rows = ((_tr("开机自启"), _tr("AgentCompass 随 Windows 启动")), (_tr("宿主 Skill"), _tr("Claude / Codex 的 agent-compass 链接")),
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
        import cli_updates
        ttk.Button(box, text=_tr("查看 CLI 日志"), style="Small.TButton",
                   command=lambda: self.run(cli_updates.open_log, self.say())).grid(row=6, column=1, sticky="e", padx=8)
        self.build_scorer_card(frame)
        self._system_prefilled = False
        self.prefill(self._read_system_fast, self._system_fast_loaded)
        self.load_system(skip_fast=self._system_prefilled)

    def build_version_card(self, frame):
        import updates

        tk, ttk = self.tk, self.ttk
        bar = theme.card(frame, padding=0, fill="x", pady=(0, 12))
        tk.Frame(bar, bg=theme.ACCENT, width=4).pack(side="left", fill="y")
        inner = ttk.Frame(bar, style="Card.TFrame", padding=(14, 10))
        inner.pack(side="left", fill="both", expand=True)
        top = ttk.Frame(inner, style="Card.TFrame")
        top.pack(fill="x")
        local_ver = updates.local_version()
        self.ac_version_var = tk.StringVar(value=f"AgentCompass {local_ver}")
        self.ac_version_label = ttk.Label(top, textvariable=self.ac_version_var, style="CardH3.TLabel")
        self.ac_version_label.pack(side="left")
        self.ac_version_pill = theme.pill(top, _tr("检测中"), "idle")
        self.ac_version_pill.pack(side="left", padx=(8, 0))
        self.ac_update_button = ttk.Button(
            top, text=_tr("更新 AgentCompass"), style="SmallAccent.TButton", command=self.update_agentcompass_now
        )
        self.ac_update_button.pack(side="right", padx=(6, 0))
        self.ac_check_update_button = ttk.Button(
            top, text=_tr("检查更新"), style="Small.TButton", command=lambda: self.check_agentcompass_update(force=True)
        )
        self.ac_check_update_button.pack(side="right", padx=(6, 0))
        self.ac_update_progress_button = ttk.Button(
            top, text=_tr("更新进度"), style="Small.TButton", command=self.open_updates
        )
        self.ac_update_progress_button.pack(side="right", padx=(6, 0))
        self.ac_version_detail_var = tk.StringVar(value=_tr("正在读取安装状态…"))
        self.ac_version_detail_label = ttk.Label(
            inner,
            textvariable=self.ac_version_detail_var,
            style="CardTiny.TLabel",
            wraplength=theme.px(680),
            justify="left",
        )
        self.ac_version_detail_label.pack(anchor="w", pady=(4, 0))
        self.ac_updating = False
        self.ac_version_info = None

    def ac_version_loaded(self, status: dict):
        if not isinstance(status, dict):
            return
        self.ac_version_info = status
        display_ver = status.get("display_version") or f"AgentCompass {status.get('tray_version', '0.0.0')}"
        if hasattr(self, "ac_version_var"):
            self.ac_version_var.set(display_ver)
        if hasattr(self, "ac_version_pill") and not getattr(self, "ac_updating", False):
            if status.get("update_available"):
                theme.set_pill(self.ac_version_pill, status.get("update_label") or _tr("可更新"), "warn")
            elif status.get("version_mismatch"):
                theme.set_pill(self.ac_version_pill, _tr("版本不一致"), "warn")
            elif not status.get("ok", True):
                theme.set_pill(self.ac_version_pill, _tr("检查失败"), "warn")
            else:
                theme.set_pill(self.ac_version_pill, _tr("已是最新"), "ok")
        if hasattr(self, "ac_version_detail_var") and not getattr(self, "ac_updating", False):
            shape_map = {
                "git": _tr("Git 仓库"),
                "installer": _tr("安装包"),
                "wheel": _tr("Wheel 包"),
            }
            shape_text = shape_map.get(status.get("shape"), status.get("shape") or "")
            parts = []
            if shape_text:
                parts.append(_tr("安装形态：{0}").format(shape_text))
            if status.get("status_line"):
                parts.append(str(status["status_line"]))
            installs = status.get("installs") or {}
            for r_info in installs.get("separate_routers") or ():
                r_tool = r_info.get("installer") or r_info.get("shape") or "router"
                parts.append(f"Router ({r_tool} {r_info.get('version')}): {r_info.get('root')}")
            self.ac_version_detail_var.set(" · ".join(parts))

    def check_agentcompass_update(self, force: bool = False):
        import updates

        if getattr(self, "ac_updating", False):
            return
        if hasattr(self, "ac_check_update_button") and force:
            self.ac_check_update_button.state(["disabled"])

        def work():
            return updates.version_status(force=force, network=True)

        def done(status):
            if hasattr(self, "ac_check_update_button"):
                self.ac_check_update_button.state(["!disabled"])
            self.ac_version_loaded(status)
            if force and isinstance(status, dict) and status.get("status_line"):
                self.status.set(status["status_line"])

        self.run(work, done, _tr("正在检查 AgentCompass 更新…") if force else None)

    def update_agentcompass_now(self):
        import update_status
        import updates

        if getattr(self, "ac_updating", False):
            return
        self.ac_updating = True
        if hasattr(self, "ac_update_button"):
            self.ac_update_button.state(["disabled"])
            self.ac_update_button.configure(text=_tr("更新中…"))
        if hasattr(self, "ac_version_pill"):
            theme.set_pill(self.ac_version_pill, _tr("更新中"), "warn")
        if hasattr(self, "ac_version_detail_var"):
            self.ac_version_detail_var.set(_tr("正在更新 AgentCompass…"))
        self._open_update_window_if_interactive()
        generation = self._generation

        def progress(event):
            update_status.record(event)
            msg = event.get("message", "") if isinstance(event, dict) else str(event)
            if msg:
                self._post_ui_progress(generation, self._ac_update_progress, msg)

        def work():
            try:
                summary = updates.update_agentcompass(progress=progress)
                return {"ok": True, "summary": summary, "status": updates.version_status(force=False, network=False)}
            except Exception as error:
                return {
                    "ok": False,
                    "summary": _tr("AgentCompass 更新失败：{0}").format(error),
                    "status": updates.version_status(force=False, network=False),
                }

        def done(result):
            self.ac_updating = False
            if hasattr(self, "ac_update_button"):
                self.ac_update_button.state(["!disabled"])
                self.ac_update_button.configure(text=_tr("更新 AgentCompass"))
            if isinstance(result.get("status"), dict):
                self.ac_version_loaded(result["status"])
            summary = result.get("summary") or ""
            if not result.get("ok") and hasattr(self, "ac_version_pill"):
                theme.set_pill(self.ac_version_pill, _tr("更新失败"), "warn")
            if summary:
                if hasattr(self, "ac_version_detail_var"):
                    self.ac_version_detail_var.set(summary)
                self.status.set(summary)

        self.run(work, done, _tr("正在更新 AgentCompass…"), long=True)

    def _ac_update_progress(self, message: str):
        if hasattr(self, "ac_version_detail_var"):
            self.ac_version_detail_var.set(message)
        self.status.set(message)

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

    def _read_version_safe(self, network: bool = False):
        import updates

        try:
            return updates.version_status(force=False, network=network)
        except Exception as exc:
            log_error(exc)
            ver = updates.local_version()
            return {"ok": False, "tray_version": ver, "display_version": f"AgentCompass {ver}", "status_line": str(exc)}

    def _read_system_fast(self):
        return self.data_module().dot_details(), self._read_scorer_safe(), self._read_version_safe(network=False)

    def _system_fast_loaded(self, value):
        self._system_prefilled = True
        dot, scorer = value[0], value[1]
        self.dot_repo_var.set(dot.get("transfer_repo") or "")
        self.scorer_loaded(scorer)
        if len(value) > 2:
            self.ac_version_loaded(value[2])

    def load_system(self, skip_fast: bool = False):
        import deploy

        if skip_fast:
            self.run(
                lambda: (deploy.startup_enabled(), self._read_version_safe(network=True)),
                lambda pair: (self.autostart_var.set(bool(pair[0])), self.ac_version_loaded(pair[1])),
            )
        else:
            self.run(
                lambda: (deploy.startup_enabled(), self.data_module().dot_details(), self._read_scorer_safe(), self._read_version_safe(network=True)),
                self.system_loaded,
            )

    def system_loaded(self, value):
        startup_on = value[0]
        self.autostart_var.set(bool(startup_on))
        self._system_fast_loaded(value[1:])

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

    # CLI updates on the model settings home

    def build_cli_toolbar(self, frame):
        import cli_updates
        tk, ttk = self.tk, self.ttk
        bar = theme.card(frame, padding=0, fill="x")
        tk.Frame(bar, bg=theme.ACCENT, width=4).pack(side="left", fill="y")
        inner = ttk.Frame(bar, style="Card.TFrame", padding=(12, 8))
        inner.pack(side="left", fill="both", expand=True)
        self.cli_pill = theme.pill(inner, _tr("检测中"), "idle")
        self.cli_pill.pack(side="left", padx=(0, 8))
        self.cli_status = tk.StringVar(value=_tr("正在读取 CLI 版本…"))
        self.cli_status_label = ttk.Label(inner, textvariable=self.cli_status, style="Card.TLabel", width=1)
        self.cli_status_label.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.cli_update_button = ttk.Button(inner, text=_tr("检查更新"), style="SmallAccent.TButton", command=self.update_all_cli)
        self.cli_update_button.pack(side="right", padx=(6, 0))
        self.model_refresh_button = ttk.Button(inner, text=_tr("刷新模型"), style="Small.TButton",
                                               command=lambda: self.refresh_model_availability(force=True))
        self.model_refresh_button.pack(side="right", padx=(6, 0))
        self.cli_progress_button = ttk.Button(inner, text=_tr("更新进度"), style="Small.TButton", command=self.open_updates)
        self.cli_progress_button.pack(side="right", padx=(6, 0))
        self.cli_log_button = ttk.Button(inner, text=_tr("更新日志"), style="Small.TButton", command=self.open_cli_log)
        self.cli_log_button.pack(side="right", padx=(6, 0))
        self.cli_auto = tk.BooleanVar(value=cli_updates.get_auto_update())
        self.cli_auto_check = ttk.Checkbutton(inner, text=_tr("自动更新（每天）"), variable=self.cli_auto,
                                              command=self.set_cli_auto_update)
        self.cli_auto_check.pack(side="right", padx=(0, 6))
        self.cli_updating = False
        self.prefill(lambda: cli_updates.snapshot(probe=False), self.cli_loaded)

    def open_cli_log(self):
        import cli_updates
        self.run(cli_updates.open_log, self.say())

    def open_update_log(self):
        import update_status
        self.run(update_status.open_log, self.say())

    def load_cli(self, summary=None):
        import cli_updates
        self._cli_generation = getattr(self, "_cli_generation", 0) + 1
        generation = self._cli_generation
        def done(rows):
            if generation == self._cli_generation:
                self.cli_loaded(rows)
                if summary is not None:
                    self.cli_status.set(summary)
        self.run(cli_updates.snapshot, done)

    def cli_loaded(self, rows):
        self.cli_inventory = rows
        installed = sum(bool(row.get("path")) and row.get("source") not in ("missing", "custom") for row in rows)
        available = sum(row.get("status") == "outdated" for row in rows)
        updatable = [row for row in rows if row.get("can_update", row.get("source") in ("npm", "native"))]
        unchecked = bool(updatable) and all(row.get("status") == "unchecked" for row in updatable)
        if hasattr(self, "cli_pill"):
            if getattr(self, "cli_updating", False):
                theme.set_pill(self.cli_pill, _tr("更新中"), "warn")
            elif available:
                theme.set_pill(self.cli_pill, _tr("{0} 可更新").format(available), "warn")
            elif unchecked:
                theme.set_pill(self.cli_pill, _tr("待检查"), "idle")
            else:
                theme.set_pill(self.cli_pill, _tr("已是最新"), "ok")
        if not getattr(self, "cli_updating", False):
            self.cli_status.set(_tr("CLI：{0} 已安装 · 尚未检查更新").format(installed) if unchecked and not available else
                                _tr("CLI：{0} 已安装 · {1} 可更新").format(installed, available))
        for widgets, row in zip(getattr(self, "model_widgets", []), getattr(self, "model_inventory", [])):
            self.fill_harness_version(widgets, row)

    def set_cli_auto_update(self):
        import cli_updates
        enabled = bool(self.cli_auto.get())
        self.run(lambda: cli_updates.set_auto_update(enabled), lambda value: self.status.set(
            _tr("已开启每日 CLI 自动更新") if value else _tr("已关闭 CLI 自动更新")))

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
            log_error(exc)

    def update_all_cli(self):
        import cli_updates
        if self.cli_updating:
            return
        self.cli_updating = True
        self.cli_update_button.state(["disabled"])
        self.cli_update_button.configure(text=_tr("更新中…"))
        if hasattr(self, "cli_pill"):
            theme.set_pill(self.cli_pill, _tr("更新中"), "warn")
        self.cli_status.set(_tr("正在检查并更新 CLI…"))
        self._open_update_window_if_interactive()
        generation = self._generation
        def progress(message):
            # Use the panel's event queue; Tk must only be touched on its UI thread.
            self._post_ui_progress(generation, self.cli_progress, message)
        def work():
            try:
                return cli_updates.update_all(progress=progress)
            except Exception as error:
                return {"summary": _tr("CLI 更新失败：{0}").format(str(error)), "updated": [], "failed": ["batch"]}
        def done(result):
            self.cli_updating = False
            self.cli_update_button.state(["!disabled"])
            self.cli_update_button.configure(text=_tr("检查更新"))
            if hasattr(self, "cli_pill"):
                failed = bool(result.get("failed") or result.get("deferred"))
                theme.set_pill(self.cli_pill, _tr("部分异常") if failed else _tr("已是最新"), "warn" if failed else "ok")
            self.cli_status.set(result["summary"])
            self.status.set(result["summary"])
            self.load_cli(summary=result["summary"])
        self.run(work, done, _tr("正在检查并更新 CLI…"), long=True)

    def cli_progress(self, message):
        self.cli_status.set(message)
        self.status.set(message)


    # ---------- page 4: integrations ----------

    def build_plugins_toolbar(self, frame):
        tk, ttk = self.tk, self.ttk
        bar = theme.card(frame, padding=0, fill="x", pady=(0, 12))
        tk.Frame(bar, bg=theme.ACCENT, width=4).pack(side="left", fill="y")
        inner = ttk.Frame(bar, style="Card.TFrame", padding=(12, 8))
        inner.pack(side="left", fill="both", expand=True)
        self.plugins_pill = theme.pill(inner, _tr("检测中"), "idle")
        self.plugins_pill.pack(side="left", padx=(0, 8))
        self.plugins_status = tk.StringVar(value=_tr("正在读取集成状态…"))
        self.plugins_status_label = ttk.Label(inner, textvariable=self.plugins_status, style="Card.TLabel", width=1)
        self.plugins_status_label.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.plugins_update_button = ttk.Button(inner, text=_tr("检查更新"), style="SmallAccent.TButton",
                                                command=self.update_components)
        self.plugins_update_button.pack(side="right", padx=(6, 0))
        self.plugins_progress_button = ttk.Button(inner, text=_tr("更新进度"), style="Small.TButton",
                                                  command=self.open_updates)
        self.plugins_progress_button.pack(side="right", padx=(6, 0))
        self.plugins_log_button = ttk.Button(inner, text=_tr("更新日志"), style="Small.TButton",
                                             command=self.open_update_log)
        self.plugins_log_button.pack(side="right", padx=(6, 0))
        self.plugins_updating = False

    def build_plugins(self, page):
        tk, ttk = self.tk, self.ttk
        self.header(page, _tr("外部集成"), (
            (_tr("重启服务"), self.restart_services, "Small.TButton"),
            (_tr("刷新"), self.load_plugins, "SmallAccent.TButton"),
        ))
        self.build_plugins_toolbar(page)

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
            elif plugin.id == "muse2api":
                ttk.Button(actions, text=_tr("复制 API Key"), style="Tiny.TButton",
                           command=self.copy_muse_key).pack(side="left")
            if plugin.id == "muse2api":
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
                widgets["info"].set(
                    (self.tunnel_url or _tr("隧道未发布"))
                    if (deployed or self.tunnel_url)
                    else _tr("未下载 · 启用后自动部署")
                )
            elif plugin.id == "easytier":
                widgets["info"].set(_tr("正在读取对端…"))
            elif plugin.id == "muse2api" and deployed:
                import gateways

                chrome = _tr("Chrome 已找到") if gateways.chrome_path().is_file() else _tr("缺 Chrome")
                widgets["info"].set(_tr('{0}/v1 · {1} · muse.ai cookie 在管理台导入').format(f'{gateways.MUSE_URL}', f'{chrome}'))
            else:
                widgets["info"].set(str(plugins.plugin_root(plugin.id)) if deployed else _tr("未下载 · 启用后自动部署"))

    def plugin_actions(self, plugin_id):
        import services

        if plugin_id == "grok-bot":
            import grok_bot_cli
            return ((_tr("安装/修复"), grok_bot_cli.install), (_tr("检查连接"), grok_bot_cli.check),
                    (_tr("使用说明"), grok_bot_cli.guide))

        def log(path_or_fn):
            def open_log():
                target = Path(path_or_fn() if callable(path_or_fn) else path_or_fn)
                if not target.exists():
                    return _tr('日志不存在：{0}').format(f'{target}')
                os.startfile(target)
                return _tr('已打开日志 {0}').format(f'{target.name}')
            return open_log
        start = lambda: services.plugin_start(plugin_id)
        stop = lambda: services.plugin_stop(plugin_id)
        if plugin_id == "dot2api":
            import dot_reconnect

            return ((_tr("启动"), start), (_tr("停止"), stop), (_tr("重连"), lambda: dot_reconnect.reconnect() or _tr("隧道地址未变化，无需重连")),
                    (_tr("日志"), log(lambda: services.LOG_DIR / "serve.log")))
        if plugin_id == "muse2api":
            import gateways

            console = f"{gateways.MUSE_URL}/dashboard"
            return ((_tr("管理台"), lambda: gateways.open_console(console)), (_tr("启动"), start), (_tr("停止"), stop),
                    (_tr("日志"), log(lambda: gateways.MUSE_LOG)))
        if plugin_id == "easytier":
            import easytier
            import network_status

            return ((_tr("局域网"), lambda: network_status.open_window() or _tr("已打开局域网状态")), (_tr("启动"), start),
                    (_tr("停止"), stop), (_tr("重启"), easytier.restart))
        if plugin_id == "chatgpt-web":
            return ((_tr("打开"), lambda: services.start_chatgpt_web(True)), (_tr("启动"), start), (_tr("停止"), stop),
                    (_tr("重启"), lambda: f"{services.stop_chatgpt_web()}；{services.start_chatgpt_web(False)}"),
                    (_tr("日志"), log(lambda: services.STATE_DIR / "chatgpt-web.log")))
        if plugin_id == "gemini-web":
            return ((_tr("打开"), lambda: services.start_gemini_web(True)), (_tr("停止"), stop),
                    (_tr("日志"), log(lambda: services.STATE_DIR / "gemini-web.log")))
        if plugin_id == "usage-float":
            return ((_tr("悬浮窗"), lambda: services.component_action("usage-float", "show")), (_tr("启动"), start),
                    (_tr("停止"), stop), (_tr("刷新"), lambda: services.component_action("usage-float", "refresh")),
                    (_tr("设置"), lambda: services.component_action("usage-float", "settings")),
                    (_tr("日志"), log(lambda: services.STATE_DIR / "usage-float.log")))
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
                self._keep_status_once = message
                try:
                    self.load_plugins(probe=False)
                finally:
                    self._keep_status_once = None
        # Let an in-flight bounded read finish on close, as with existing long actions.
        self.run(work, done, _tr('正在检查 Grok Bot 连接…'), long=True)

    def _record_plugin_flag(self, plugin_id: str, key: str, value: bool) -> None:
        flags = getattr(self, "_user_plugin_flags", None)
        if flags is None:
            flags = {}
            self._user_plugin_flags = flags
        flags[(plugin_id, key)] = (getattr(self, "_plugins_load_generation", 0), bool(value))

    def _persist_plugin_flag(self, plugin_id: str, key: str, value: bool) -> bool:
        value = bool(value)
        try:
            plugins.set_flag(plugin_id, key, value)
        except Exception as exc:
            disk_val = bool(plugins.load().get(plugin_id, {}).get(key, not value))
            widgets = getattr(self, "plugin_widgets", {}).get(plugin_id)
            if widgets and key in widgets:
                widgets[key].set(disk_val)
            flags = getattr(self, "_user_plugin_flags", None)
            if flags is not None:
                flags.pop((plugin_id, key), None)
            self.status.set(_tr('操作失败：{0}').format(f'{exc}'))
            return False
        self._record_plugin_flag(plugin_id, key, value)
        return True

    def toggle_enabled(self, plugin_id, value):
        import services

        value = bool(value)
        if not self._persist_plugin_flag(plugin_id, "enabled", value):
            return
        self._cards_cache = None
        self._plugins_update_result = None

        def work():
            name = plugins.BY_ID[plugin_id].name
            current = plugins.enabled(plugin_id)
            if current != value:
                return _tr('已启用 {0}').format(f'{name}') if current else _tr('已停用 {0}').format(f'{name}')
            if not value:
                try:
                    return _tr('已停用 {0}；{1}').format(f'{name}', f'{services.plugin_stop(plugin_id)}')
                except Exception as exc:
                    return _tr('已停用 {0}（停止时出错：{1}）').format(f'{name}', f'{exc}')
            if plugins.autostarts(plugin_id):
                try:
                    return _tr('已启用 {0}；{1}').format(f'{name}', f'{services.plugin_start(plugin_id)}')
                except Exception as exc:
                    return _tr('已启用 {0}（启动时出错：{1}）').format(f'{name}', f'{exc}')
            spec = plugins.BY_ID[plugin_id]
            root = plugins.plugin_root(plugin_id)
            if spec.repo and not (root / ".git").exists():
                import updates

                try:
                    deployed = updates.ensure_plugin(plugin_id)
                    services.refresh_plugin_roots()
                    return _tr('已启用 {0}；{1}').format(f'{name}', f'{deployed}')
                except Exception as exc:
                    return _tr('已启用 {0}（部署时出错：{1}）').format(f'{name}', f'{exc}')
            return _tr('已启用 {0}').format(f'{name}')
        if value and plugin_id == "muse2api":
            # First enable downloads and installs its dependencies: show it in the update window.
            import update_status
            import updates

            self._open_update_window_if_interactive()

            def deploy_work():
                name = plugins.BY_ID[plugin_id].name
                if not plugins.enabled(plugin_id):
                    return _tr('已停用 {0}').format(f'{name}')
                try:
                    return _tr('已启用 {0}；').format(f'{name}') + updates.deploy_gateway(
                        plugin_id, plugins.autostarts(plugin_id), progress=update_status.record
                    )
                except Exception as exc:
                    log_error(exc)
                    return _tr('已启用 {0}（部署时出错：{1}）').format(f'{name}', f'{exc}')

            self.run(deploy_work, self.say(self.load_plugins), _tr("正在下载并构建…（关闭窗口不会中断）"), long=True)
            return
        self.run(work, self.say(self.load_plugins))

    def toggle_autostart(self, plugin_id, value):
        value = bool(value)
        if not self._persist_plugin_flag(plugin_id, "autostart", value):
            return
        self._plugins_update_result = None
        name = plugins.BY_ID[plugin_id].name
        state_text = _tr('开') if value else _tr('关')
        self.run(lambda: _tr('{0} 随托盘自启：{1}').format(f'{name}', f'{state_text}'))

    def load_plugins(self, probe=True):
        import services

        keep_status = getattr(self, "_keep_status_once", None)
        update_result = getattr(self, "_plugins_update_result", None)
        if not keep_status or (update_result and keep_status != update_result[0]):
            self._plugins_update_result = None
            update_result = None
        if probe and not keep_status:
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
                        peers_raw = reading.get("peers")
                        peer_count = (
                            None
                            if reading.get("state") == "running" and peers_raw is None
                            else len(easytier.remote_peers(peers_raw))
                        )
                        return easytier.label(reading), peer_count
                    return services.plugin_label(plugin_id), None
                except Exception as exc:
                    return _tr('状态异常：{0}').format(f'{exc}')[:30], None
            found = {}
            threads = []
            for pid in plugins.BY_ID:
                t = threading.Thread(target=lambda p=pid: found.__setitem__(p, label(p)), daemon=True)
                t.start()
                threads.append(t)
            for t in threads:
                t.join()
            config = plugins.load()
            labels = {plugin_id: value[0] for plugin_id, value in found.items()}
            data = self.data_module()
            extras = {"dot2api": data.dot_details(), "easytier": found["easytier"][1],
                      "grok-bot": found["grok-bot"][1]}
            extras["dot2api"]["subscription"] = data.subscription_text()
            return config, labels, extras
        def done(value):
            if generation == self._plugins_load_generation:
                config, labels, extras = value
                for (pid, key), (rec_gen, flag_val) in (getattr(self, "_user_plugin_flags", None) or {}).items():
                    if rec_gen >= generation and pid in config and isinstance(config[pid], dict):
                        config[pid][key] = flag_val
                self.plugins_loaded(
                    (config, labels, extras),
                    keep_status=keep_status,
                    update_result=update_result,
                )
        self.run(work, done, None if keep_status else _tr("正在读取集成状态…"))

    def plugins_loaded(self, value, keep_status=None, update_result=None):
        config, labels, extras = value
        enabled_count = 0
        running_count = 0
        expected_running_count = 0
        expected_ok_count = 0
        attention_count = 0
        for plugin in plugins.PLUGINS:
            widgets = self.plugin_widgets[plugin.id]
            row = config[plugin.id]
            if plugin.id == "grok-bot":
                import grok_bot_cli
                reading = extras.get(plugin.id) or {}
                kind = reading.get('kind', 'idle')
                if reading.get('installed'):
                    enabled_count += 1
                    if kind == 'ok':
                        running_count += 1
                    elif kind in ('warn', 'error', 'danger'):
                        attention_count += 1
                theme.set_pill(widgets["status"], labels[plugin.id], kind)
                widgets["info"].set(grok_bot_cli.info())
                if getattr(self, '_grok_auto_check_pending', False):
                    self._grok_auto_check_pending = False
                    if reading.get('installed'):
                        self.check_grok_bot()
                continue
            deployed = not plugin.repo or (plugins.plugin_root(plugin.id) / ".git").exists()
            raw_label = labels[plugin.id]
            if not deployed and raw_label == _tr("已停止"):
                raw_label = _tr("未安装")
            raw_kind = theme.status_kind(raw_label)
            if row["enabled"]:
                enabled_count += 1
                if raw_kind == "ok":
                    running_count += 1
                elif raw_kind in ("warn", "error", "danger"):
                    attention_count += 1
                if row["autostart"]:
                    expected_running_count += 1
                    if raw_kind == "ok":
                        expected_ok_count += 1
            label = raw_label if row["enabled"] else _tr('未启用 · {0}').format(f'{raw_label}')
            pill_label = label if len(label) <= 42 else f"{label[:41]}…"
            theme.set_pill(widgets["status"], pill_label, raw_kind if row["enabled"] else "idle")
            widgets["enabled"].set(row["enabled"])
            widgets["autostart"].set(row["autostart"])
            if plugin.id == "dot2api":
                dot = extras["dot2api"]
                self.tunnel_url = dot.get("tunnel_url") or ""
                if deployed or self.tunnel_url or raw_kind == "ok":
                    widgets["info"].set(f"{self.tunnel_url or _tr('隧道未发布')} · {dot.get('subscription')}")
                else:
                    widgets["info"].set(_tr("未下载 · 启用后自动部署"))
            elif plugin.id == "easytier":
                peers = extras.get("easytier")
                widgets["info"].set(_tr("对端读取失败") if peers is None else _tr('可见对端 {0} 个').format(f'{peers}'))
            elif plugin.id == "muse2api" and deployed:
                import gateways

                chrome = _tr("Chrome 已找到") if gateways.chrome_path().is_file() else _tr("缺 Chrome")
                widgets["info"].set(_tr('{0}/v1 · {1} · muse.ai cookie 在管理台导入').format(f'{gateways.MUSE_URL}', f'{chrome}'))
            else:
                widgets["info"].set(str(plugins.plugin_root(plugin.id)) if deployed else _tr("未下载 · 启用后自动部署"))
        if not getattr(self, "plugins_updating", False):
            if update_result:
                summary_text, pill = update_result
                if hasattr(self, "plugins_status") and summary_text:
                    self.plugins_status.set(summary_text)
                if hasattr(self, "plugins_pill") and pill:
                    theme.set_pill(self.plugins_pill, *pill)
            else:
                if hasattr(self, "plugins_status"):
                    self.plugins_status.set(_tr("集成：{0} 已启用 · {1} 运行中").format(enabled_count, running_count))
                if hasattr(self, "plugins_pill"):
                    if attention_count > 0:
                        theme.set_pill(self.plugins_pill, _tr("部分异常"), "warn")
                    else:
                        all_ok = enabled_count > 0 and (
                            (expected_running_count > 0 and expected_ok_count >= expected_running_count)
                            or running_count >= enabled_count
                        )
                        theme.set_pill(self.plugins_pill, _tr("正常") if all_ok else _tr("就绪"), "ok" if all_ok else "idle")
            self.status.set(keep_status or _tr('集成状态已更新 · {0}').format(f"{time.strftime('%H:%M:%S')}"))

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

    def import_account(self, plugin_id):
        """Sign in to muse.ai in a dedicated Chrome; the cookie is then imported through the gateway's API."""
        import account_login
        import services

        def work():
            ready = services.muse_ready
            if not ready():
                services.plugin_start(plugin_id)
                deadline = time.monotonic() + 60
                while not ready():
                    if time.monotonic() >= deadline:
                        return _tr('{0} 没有启动，未导入，详见日志').format(plugins.BY_ID[plugin_id].name)
                    time.sleep(1)
            return account_login.login_and_import(plugin_id)

        self.run(work, self.say(self.load_plugins), _tr("已打开登录窗口：在里面登录，完成后自动导入（10 分钟内有效）…"), long=True)

    def copy_muse_key(self):
        import gateways

        self.copy_secret(gateways.muse_api_key(), _tr(" Muse2API API Key（首次启动后才生成）"))

    def restart_services(self):
        import services

        self.run(services.restart_services, self.say(self.load_plugins), _tr("正在重启已启用服务…"))

    def update_components(self):
        import update_status
        import updates

        if getattr(self, "plugins_updating", False):
            return
        self.plugins_updating = True
        self._plugins_update_result = None
        if hasattr(self, "plugins_update_button"):
            self.plugins_update_button.state(["disabled"])
            self.plugins_update_button.configure(text=_tr("更新中…"))
        if hasattr(self, "plugins_pill"):
            theme.set_pill(self.plugins_pill, _tr("更新中"), "warn")
        if hasattr(self, "plugins_status"):
            self.plugins_status.set(_tr("正在检查并更新组件…（关闭窗口不会中断）"))
        self._open_update_window_if_interactive()
        generation = self._generation

        def progress(event):
            update_status.record(event)
            comp = event.get("component", "") if isinstance(event, dict) else ""
            msg = event.get("message", "") if isinstance(event, dict) else str(event)
            text = f"{comp} · {msg}" if comp and msg and not msg.startswith(comp) else (msg or comp)
            if text:
                self._post_ui_progress(generation, self.plugins_progress, text)

        def work():
            try:
                return updates.update_all(progress=progress)
            except Exception as error:
                log_error(error)
                return updates.UpdateAllResult(
                    _tr("检查更新失败：{0}").format(error),
                    components={"batch": {"status": "failed", "message": str(error)}},
                )

        def done(result):
            self.plugins_updating = False
            if hasattr(self, "plugins_update_button"):
                self.plugins_update_button.state(["!disabled"])
                self.plugins_update_button.configure(text=_tr("检查更新"))
            structured = isinstance(result, dict) or hasattr(result, "statuses")
            text = str(result.get("summary") if isinstance(result, dict) else (result or ""))
            busy = bool(result.get("busy")) if structured else (text == _tr("正在更新，请稍候"))
            statuses = dict(result.get("statuses") or {}) if structured else {}
            if structured and not statuses and isinstance(result.get("components"), dict):
                statuses = {
                    k: (v.get("status", "failed") if isinstance(v, dict) else str(v))
                    for k, v in result["components"].items()
                }
            failed = bool(
                (result.get("failed") or result.get("deferred")) if structured else False
            ) or any(st in ("failed", "deferred") for st in statuses.values())
            product_status = str(result.get("product_status") or "dev") if structured else "dev"
            product_warn = product_status in ("newer", "failed")
            active_statuses = [st for st in statuses.values() if st != "skipped"]
            all_up_to_date = (
                structured
                and not busy
                and not failed
                and not product_warn
                and product_status in ("current", "ahead", "dev")
                and bool(active_statuses)
                and all(st in ("current", "updated") for st in active_statuses)
            )
            if busy:
                pill = (_tr("正在更新"), "warn")
            elif failed:
                pill = (_tr("部分异常"), "warn")
            elif product_warn:
                pill = (_tr("需注意"), "warn")
            elif all_up_to_date:
                pill = (_tr("已是最新"), "ok")
            else:
                pill = (_tr("就绪"), "idle")
            if hasattr(self, "plugins_pill"):
                theme.set_pill(self.plugins_pill, *pill)
            if hasattr(self, "plugins_status") and text:
                self.plugins_status.set(text)
            if text:
                self.status.set(text)
            self._plugins_update_result = (text, pill) if text else None
            self._keep_status_once = text or None
            try:
                self.load_plugins()
            finally:
                self._keep_status_once = None

        self.run(work, done,
                 _tr("正在检查并更新组件…（关闭窗口不会中断）"), long=True)

    def plugins_progress(self, message):
        if hasattr(self, "plugins_status"):
            self.plugins_status.set(message)
        self.status.set(message)

    def open_updates(self):
        import update_status

        update_status.open_window()


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
        log_error(exc)
        raise
    root.mainloop()


if __name__ == "__main__":
    main()
