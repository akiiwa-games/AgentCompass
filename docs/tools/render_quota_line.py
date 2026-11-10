"""Render docs/quota-line.gif off-screen: the Decision Compass "Quota line" tab replaying two synthetic decisions.

    python docs/tools/render_quota_line.py [--scale 1.5] [--fps 20] [--speed 1.6] [--check-dir DIR]

The same task is routed twice. First while Claude A is ahead of its recovery line: that window is spared and
the task runs on the account that is under its line. Then two hours before Claude A resets with 40% left:
the quota that would expire is spent first, in fast mode. Per window the picture is a quota line and a usage
line that rises to now: green and "used first" under the line, red and "held back" over it; the window the
task runs on is lit, and a dotted pace line closes at the reset. The real page is opened on a private
desktop by render_ui_screenshots.py; each frame pastes the view's own renderer output into its canvas rectangle
and is cropped to the list card. Composites are checked against real PrintWindow captures before the GIF is
written. The GIF opens on the settled second decision, so a viewer without autoplay still sees a whole picture.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import render_hero as hero
import render_ui_screenshots as shots

OUTPUT = shots.ROOT / "docs" / "quota-line.gif"
POSTER_MS, LEAD_MS, HOLD_MS = 600, 400, 1400      # the opening still, the bare view before a replay, its settled result
CHECKS = (0., 1.4, 3.5, 5.2, 7.6, 9.9)             # story times compared against the real window


def frames_from_window(args, state):
    import decision_theatre as theatre
    import quota_line
    import router_data
    import ui_theme
    import win32gui

    args.moment, args.list = theatre.DURATION, "quota"
    root, panel, top = shots.open_page(args, state, "theatre")
    panel.theatre_enabled.set(False)
    view = panel.quota_line
    left, upper = win32gui.GetWindowRect(top)[:2]

    def rect(widget):
        x0, y0, x1, y1 = win32gui.GetWindowRect(widget.winfo_id())
        return x0 - left, y0 - upper, x1 - left, y1 - upper

    card = rect(panel.list_stack.master.outer)
    x, y = rect(view.canvas)[:2]
    start = quota_line.FIRST - .1
    count = round((quota_line.LAST - start) * args.fps / args.speed)
    stories = [min(quota_line.LAST, start + i * args.speed / args.fps) for i in range(count + 1)]
    args.frames, args.durations, checks, poster = [], [], {}, None
    # Ledger rows are newest first: the earlier decision (window spared) is the last row, the headline the first.
    for index in (len(panel.ledger_rows) - 1, 0):
        panel.plan_loaded(router_data.replay(panel.ledger_rows[index]))
        panel.theatre.skip()
        shots.pump(root, 10)
        settled = shots.shoot(top, args.scale)
        # A renderer of its own: the window's is advanced only by the window, which the checks below compare against.
        film = quota_line.QuotaLineRenderer(view.data, view.renderer.w, view.renderer.h, ui_theme.FACTOR)

        def composite(story):
            frame = settled.copy()
            frame.paste(film.frame(story), (x, y))
            return frame.crop(card)

        for story in CHECKS:
            view.follow(story)
            for _ in range(4):  # a busy machine may capture before the canvas has painted: look again
                shots.pump(root, 10)
                real = shots.shoot(top, args.scale).crop(card)
                checks[(index, story)] = hero.differs(real, composite(story))
                if not checks[(index, story)]:
                    break
            if args.check_dir:
                args.check_dir.mkdir(parents=True, exist_ok=True)
                real.save(args.check_dir / f"quota-real-{index}-{story:.1f}.png")
                composite(story).save(args.check_dir / f"quota-composite-{index}-{story:.1f}.png")
        args.frames += [composite(story) for story in stories]
        args.durations += [LEAD_MS] + [round(1000 / args.fps)] * (len(stories) - 2) + [HOLD_MS]
        poster = args.frames[-1]
    args.frames.insert(0, poster)
    args.durations.insert(0, POSTER_MS)
    size = args.frames[0].size
    args.stage = (x - card[0], y - card[1], min(size[0], x - card[0] + view.renderer.w), min(size[1], y - card[1] + view.renderer.h))
    print(f"card {size[0]}x{size[1]}, view {view.renderer.w}x{view.renderer.h}; "
          f"differing pixels real vs composite: {checks}")
    if any(checks.values()):
        raise SystemExit("composite does not match the real window capture")
    panel.closing = True
    root.destroy()


def main() -> None:
    from PIL import Image

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--scale", type=float, default=1.5, help="display scale; 1.5 gives a ~630 px wide card")
    parser.add_argument("--fps", type=int, default=20)
    parser.add_argument("--speed", type=float, default=1.6, help="story seconds per second of film")
    parser.add_argument("--check-dir", type=Path, help="also save the real/composite check captures here")
    args = parser.parse_args()
    import ui_theme
    ui_theme.dpi_aware()
    shots.offscreen(lambda state: frames_from_window(args, state), "agentcompass-quota-line-")
    encoded = hero.encode(args.frames, args.stage)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    encoded[0].save(args.output, save_all=True, append_images=encoded[1:], duration=args.durations,
                    loop=0, optimize=True, disposal=1)
    size = args.output.stat().st_size
    if size > 8 * 1024 * 1024:
        raise RuntimeError("README GIF exceeds the 8 MiB limit")
    with Image.open(args.output) as gif:
        print(f"{args.output.name}: {gif.width}x{gif.height}, {gif.n_frames} frames, "
              f"{args.fps} fps, {sum(args.durations)} ms, {size:,} bytes")


if __name__ == "__main__":
    main()
