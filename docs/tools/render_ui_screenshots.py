"""Render the real AgentCompass Decision Compass page off-screen with synthetic data (Windows).

    python docs/tools/render_ui_screenshots.py <output.png> [--scale 1.5]

The window lives on a private, never-displayed Win32 desktop (CreateDesktop) owned by a worker
thread, is captured with PrintWindow, then destroyed. It never takes focus and never appears on
the user's desktop. State lives in a temporary directory (neutral reference fleet, synthetic
ledger); nothing under ~/.route-manager is read or written.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tray"))
sys.path.insert(0, str(ROOT / "skills" / "route-agent-work" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))



def isolate(state: Path) -> None:
    import allocator

    accounts = state / "accounts.json"
    shutil.copy(allocator.reference_fleet_path(), accounts)
    policy = state / "host-policy.json"
    policy.write_text(json.dumps({"allowed_accounts": list(json.loads(accounts.read_text("utf-8-sig"))["accounts"])}),
                      encoding="utf-8")
    os.environ.update({
        "ROUTER_STATE_DIR": str(state), "ROUTER_ACCOUNTS_FILE": str(accounts),
        "ROUTER_ACCOUNTS_OVERRIDE": str(state / "accounts-override.json"), "ROUTER_HOST_POLICY": str(policy),
        "ROUTER_LEDGER": str(state / "ledger.jsonl"), "ROUTER_CAPACITY": str(state / "capacity.json"),
        "USERPROFILE": str(state), "HOME": str(state),
    })


def write_ledger(path: Path) -> None:
    import synthetic_decisions as demo

    rows = [demo.decision_row(demo.HEADLINE, demo.HEADLINE_KIND, 2, "opus55_high", "2026-10-30T09:40:00+00:00")]
    stamps = ("2026-10-30T08:55:00+00:00", "2026-10-30T07:35:00+00:00", "2026-10-30T06:20:00+00:00",
              "2026-10-29T16:05:00+00:00", "2026-10-29T10:30:00+00:00")
    for (objective, kind, hours, winner), at in zip(demo.HISTORY, stamps):
        rows.append(demo.decision_row(objective, kind, hours, winner, at))
    path.write_text("\n".join(json.dumps(row) for row in reversed(rows)) + "\n", encoding="utf-8")


def capture(top: int, width: int, height: int):
    import win32con
    import win32gui
    import win32ui
    from PIL import Image

    window_dc = win32gui.GetWindowDC(top)
    source = win32ui.CreateDCFromHandle(window_dc)
    memory = source.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(source, width, height)
    memory.SelectObject(bitmap)
    ctypes.windll.user32.PrintWindow(top, memory.GetSafeHdc(), 2)  # PW_RENDERFULLCONTENT
    info = bitmap.GetInfo()
    raw = bitmap.GetBitmapBits(True)
    image = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]), raw, "raw", "BGRX", 0, 1)
    win32gui.DeleteObject(bitmap.GetHandle())
    memory.DeleteDC()
    source.DeleteDC()
    win32gui.ReleaseDC(top, window_dc)
    return image


def render(args, state: Path) -> None:
    import i18n
    i18n.get_language = lambda: "en"
    import allocator
    import route_judge
    import router_data
    router_data.modules = lambda: (allocator, route_judge)
    router_data.STATE_DIR = state
    import decision_theatre
    import router_window
    import synthetic_decisions as demo
    import ui_theme
    import tkinter as tk
    import win32gui

    root = tk.Tk()
    root.withdraw()
    root.tk.call("tk", "scaling", 96 * args.scale / 72)
    panel = router_window.Panel(root, "scoring")
    panel.theatre_enabled.set(False)
    root.deiconify()
    deadline = time.time() + 30
    while time.time() < deadline and not (panel.plan_result and panel.ledger_rows):
        root.update()
        time.sleep(0.05)
    for _ in range(40):
        root.update()
        time.sleep(0.02)
    # Freeze the theatre at the radar-comparison moment; the real canvas draws it.
    decision_theatre.story_time = lambda wall: args.moment
    panel.theatre.skip()
    if args.view == "radar":
        panel.render_radar()  # the static radar a user gets by picking a candidate
    panel.theatre_enabled.set(True)  # the default; only the freeze above turned it off
    panel.objective.insert("1.0", demo.HEADLINE)
    for column, width in (("at", 92), ("kind", 130), ("choice", 112), ("task", 96)):
        panel.ledger_table.column(column, width=ui_theme.px(width))
    panel._suppress_next_select = True
    panel.ledger_table.selection_set("0")
    for _ in range(20):
        root.update()
        time.sleep(0.02)
    top = ctypes.windll.user32.GetParent(root.winfo_id())
    left, topy, right, bottom = win32gui.GetWindowRect(top)
    image = capture(top, right - left, bottom - topy)
    # PrintWindow includes the invisible resize border; keep the visible frame only.
    image = image.crop((0, 0, image.width - round(11 * args.scale / 1.5), image.height - round(10 * args.scale / 1.5)))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    image.save(args.output)
    print(f"{args.output} {image.width}x{image.height}")
    panel.closing = True
    root.destroy()


def main() -> None:
    import threading

    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--scale", type=float, default=1.5, help="display scale, 1.5 = 150%% DPI")
    parser.add_argument("--view", choices=("radar", "theatre"), default="radar")
    parser.add_argument("--moment", type=float, default=7.75, help="theatre story time (s) to freeze")
    args = parser.parse_args()
    import ui_theme
    ui_theme.dpi_aware()
    user32 = ctypes.windll.user32
    user32.CreateDesktopW.restype = ctypes.c_void_p
    user32.SetThreadDesktop.argtypes = [ctypes.c_void_p]
    desktop = user32.CreateDesktopW("agentcompass-shot", None, None, 0, 0x10000000, None)
    if not desktop:
        raise OSError("CreateDesktop failed; refusing to draw on the user's desktop")
    errors = []
    with tempfile.TemporaryDirectory(prefix="agentcompass-shot-") as directory:
        state = Path(directory)
        isolate(state)
        write_ledger(state / "ledger.jsonl")

        def work():
            try:
                if not user32.SetThreadDesktop(desktop):
                    raise OSError("SetThreadDesktop failed; refusing to draw on the user's desktop")
                render(args, state)
            except BaseException as exc:
                import traceback
                errors.append(traceback.format_exc())

        thread = threading.Thread(target=work)
        thread.start()
        thread.join()
    user32.CloseDesktop(ctypes.c_void_p(desktop))
    if errors:
        raise SystemExit(errors[0])


if __name__ == "__main__":
    main()
