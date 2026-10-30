"""Render docs/hero.gif off-screen: the real Decision Compass window replaying the synthetic decision (Windows, Pillow).

    python docs/tools/render_hero.py [--scale 1.15] [--fps 25] [--check-dir DIR]

The window is opened once on a private desktop by render_ui_screenshots.py and captured with
PrintWindow; each frame pastes the window's own TheatreRenderer output into the theatre canvas
rectangle, which is exactly what the canvas shows. Settled and mid-replay composites are checked
against real PrintWindow captures before the GIF is written.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from PIL import Image, ImageChops

sys.path.insert(0, str(Path(__file__).resolve().parent))
import render_ui_screenshots as shots

OUTPUT = shots.ROOT / "docs" / "hero.gif"
HOLD_MS = 2000
# Flat page and key ink colours kept exact so the paper tone never bands.
RESERVED = [(254, 254, 253), (255, 255, 255), (79, 70, 229), (6, 182, 212),
            (15, 23, 42), (100, 116, 139), (148, 163, 184), (226, 232, 240)]


def differs(a, b, top: int = 0) -> int:
    """Number of pixels below row `top` that differ between two RGB images of the same size."""
    r, g, b2 = ImageChops.difference(a.crop((0, top, a.width, a.height)), b.crop((0, top, b.width, b.height))).split()
    mask = ImageChops.lighter(ImageChops.lighter(r, g), b2).point(lambda v: 255 if v else 0)
    return sum(mask.histogram()[255:])


def frames_from_window(args, state):
    import decision_theatre as theatre
    import win32gui

    real_story_time = theatre.story_time
    args.moment = theatre.DURATION
    root, panel, top = shots.open_page(args, state, "theatre")
    theatre.story_time = real_story_time
    film = panel.theatre
    film.skip()
    shots.pump(root, 10)
    settled = shots.shoot(top, args.scale)
    left, upper = win32gui.GetWindowRect(top)[:2]
    x, y = (a - b for a, b in zip(win32gui.GetWindowRect(film.canvas.winfo_id())[:2], (left, upper)))
    # The OS may flip the caption between active and inactive on the private desktop; compare the client area.
    client = win32gui.ClientToScreen(top, (0, 0))[1] - upper
    renderer = film.renderer
    width, height = renderer.frame(theatre.DURATION).size

    def composite(wall):
        frame = settled.copy()
        frame.paste(renderer.frame(theatre.story_time(wall)), (x, y))
        return frame

    checks = {}
    for wall in (2.2, 6.0, theatre.DURATION):  # kind under the title, radar being drawn, settled
        film.elapsed = wall
        film.draw()
        shots.pump(root, 10)
        real = shots.shoot(top, args.scale)
        checks[wall] = differs(real, composite(wall), client)
        if args.check_dir:
            args.check_dir.mkdir(parents=True, exist_ok=True)
            real.save(args.check_dir / f"real-{wall:.1f}.png")
            composite(wall).save(args.check_dir / f"composite-{wall:.1f}.png")
    print(f"window {settled.width}x{settled.height}, stage {width}x{height} at ({x},{y}); "
          f"differing pixels real vs composite: {checks}")
    settled.paste(real.crop((0, 0, real.width, client)), (0, 0))  # one caption state for the whole film
    if any(checks.values()):
        raise SystemExit("composite does not match the real window capture")

    # Start once the title is fully in, so the first frame reads on its own.
    start = next(i for i in range(1000) if theatre.story_time(i / args.fps) >= theatre.TITLE_IN)
    stop = round(theatre.DURATION * args.fps)
    args.frames = [composite(min(i / args.fps, theatre.DURATION)) for i in range(start, stop + 1)]
    args.stage = (x, y, x + width, y + height)
    panel.closing = True
    root.destroy()


def encode(frames, stage):
    # One shared palette (page + sampled stage frames) prevents shimmer; no dithering grain.
    crops = [frames[round(i * (len(frames) - 1) / 23)].crop(stage) for i in range(24)]
    w, h = crops[0].width // 2, crops[0].height // 2
    page = frames[-1]
    atlas = Image.new("RGB", (max(page.width, w * 6), page.height + h * 4), RESERVED[0])
    atlas.paste(page, (0, 0))
    for i, crop in enumerate(crops):
        atlas.paste(crop.resize((w, h), Image.Resampling.BOX), (i % 6 * w, page.height + i // 6 * h))
    count = 256 - len(RESERVED)
    palette = atlas.quantize(colors=count, method=Image.Quantize.MEDIANCUT)
    palette.putpalette(palette.getpalette()[:count * 3] + [v for rgb in RESERVED for v in rgb])
    encoded = []
    for frame in frames:
        indexed = frame.quantize(palette=palette, dither=Image.Dither.NONE)
        # Pillow's nearest-colour lookup rounds RGB; pin exact flat backgrounds explicitly.
        for index, color in enumerate(RESERVED[:2], start=count):
            r, g, b = ImageChops.difference(frame, Image.new("RGB", frame.size, color)).split()
            mask = ImageChops.lighter(ImageChops.lighter(r, g), b).point(lambda v: 255 if v == 0 else 0)
            indexed.paste(index, mask=mask)
        encoded.append(indexed)
    return encoded


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--scale", type=float, default=1.15, help="display scale; 1.15 gives a ~1300 px wide window")
    parser.add_argument("--fps", type=int, default=25)
    parser.add_argument("--check-dir", type=Path, help="also save the real/composite check captures here")
    args = parser.parse_args()
    import ui_theme
    ui_theme.dpi_aware()
    shots.offscreen(lambda state: frames_from_window(args, state), "agentcompass-hero-")
    encoded = encode(args.frames, args.stage)
    durations = [round(1000 / args.fps)] * (len(encoded) - 1) + [HOLD_MS]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    encoded[0].save(args.output, save_all=True, append_images=encoded[1:], duration=durations,
                    loop=0, optimize=True, disposal=1)
    size = args.output.stat().st_size
    if size > 8 * 1024 * 1024:
        raise RuntimeError("README GIF exceeds the 8 MiB limit")
    with Image.open(args.output) as gif:
        print(f"{args.output.name}: {gif.width}x{gif.height}, {gif.n_frames} frames, "
              f"{args.fps} fps, {sum(durations)} ms, {size:,} bytes")


if __name__ == "__main__":
    main()
