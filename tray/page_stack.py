"""Pages stacked in one container: switching raises one page and leaves the others mapped underneath.

Unmapping one page and mapping another makes Tk re-place and re-show every native child window of the page that
comes back. On Windows each of those calls goes through every global window hook installed on the machine
(input methods, security and remote-access agents); where such hooks are present a single call takes about a
millisecond, so a 150-widget page took 0.3-0.5 s to reappear.

Here a covered page keeps its Tk geometry and stays mapped as far as Tk knows; only its native window is hidden,
so Windows neither repaints it under the page on top nor re-places its children when it comes back.

Because covered pages stay mapped, `<Map>`/`<Unmap>` no longer mark a page switch: widgets that animate only while
seen register with watch() and ask shown() instead of winfo_ismapped().
"""

from __future__ import annotations

import sys
import weakref

_watchers: weakref.WeakSet = weakref.WeakSet()


def watch(watcher) -> None:
    """watcher.canvas is its widget; watcher.page_shown() / page_hidden() run when its page is raised / covered."""
    _watchers.add(watcher)


def shown(widget) -> bool:
    """Mapped and not on a page another page covers (or in a hidden window: its page is marked the same way)."""
    if not widget.winfo_ismapped():
        return False
    while widget is not None:
        if getattr(widget, "_page_hidden", False):
            return False
        widget = getattr(widget, "master", None)
    return True


def _native_show(page, visible: bool) -> None:
    if sys.platform != "win32":
        return
    import ctypes

    ctypes.windll.user32.ShowWindow(int(page.winfo_id()), 8 if visible else 0)  # SW_SHOWNA / SW_HIDE


def _hold_size(page) -> None:
    """A covered page keeps its size, so resizing the window lays out only the page on top."""
    page.place_configure(relwidth=0, relheight=0, width=max(1, page.winfo_width()), height=max(1, page.winfo_height()))


def _inside(widget, page) -> bool:
    return str(widget).startswith(str(page) + ".")


def notify(page, visible: bool) -> None:
    """Tell watchers on `page` it was raised or covered; also used when the whole window is hidden or shown."""
    for watcher in list(_watchers):
        canvas = getattr(watcher, "canvas", None)
        if canvas is None or not _inside(canvas, page):
            continue
        try:
            if visible:
                if shown(canvas):
                    watcher.page_shown()
            else:
                watcher.page_hidden()
        except Exception:
            # One watcher's failure must not stop the switch or the others.
            pass


class PageStack:
    def __init__(self, container):
        self.container = container
        self.current = None
        self._placed: set[str] = set()

    def prepare(self, page) -> None:
        """Lay a page out under the current one, so its native windows are created and sized out of sight."""
        if str(page) in self._placed:
            return
        self._placed.add(str(page))
        page._page_hidden = self.current is not None
        page.place(x=0, y=0, relwidth=1, relheight=1)
        if self.current is not None:
            page.lower(self.current)
        page.update_idletasks()
        if self.current is not None:
            _native_show(page, False)
            _hold_size(page)

    def show(self, page) -> None:
        previous = self.current
        if page is previous:
            return
        self.prepare(page)  # a first show comes up complete
        page._page_hidden = False
        page.place_configure(relwidth=1, relheight=1, width=0, height=0)
        _native_show(page, True)
        page.tkraise()
        self.current = page
        if previous is not None:
            _native_show(previous, False)
            _hold_size(previous)
            previous._page_hidden = True
            self._move_focus(previous, page)
            notify(previous, False)
        notify(page, True)

    def _move_focus(self, previous, page) -> None:
        """Keys must not go on landing in a widget on the page that was just covered."""
        try:
            focus = self.container.focus_get()
        except (KeyError, AttributeError):
            return
        if focus is not None and _inside(focus, previous):
            page.focus_set()
