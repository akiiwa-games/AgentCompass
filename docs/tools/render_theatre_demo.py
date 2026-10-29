"""Regenerate the synthetic README GIF off-screen: python docs/tools/render_theatre_demo.py (Pillow required)."""
from __future__ import annotations

import math
from pathlib import Path
import sys

from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tray"))

import i18n

# Force English in this process before building the scene; never load/save UI settings.
i18n.get_language = lambda: "en"

import decision_summary as summary
import decision_theatre as theatre
import router_data

WIDTH, STAGE_HEIGHT, SCALE, FPS = 680, 480, 1.5, 25
OUTPUT = ROOT / "docs" / "decision-theatre.gif"


def synthetic_result():
    # Illustrative values only, not benchmark claims or live allocation inputs.
    # Score = mean(four capabilities) - opportunity cost + reset bonus - effort penalty.
    specs = [
        ("claude-opus-5-5", "high", (.82, .94, .96, .78), .025, .04, .02),
        ("gpt-6.1-sol", "high", (.76, .88, .94, .84), .045, .02, .02),
        ("gemini-4-argon-high", "high", (.95, .82, .77, .86), .075, .01, .03),
        ("claude-sonnet-5-5", "medium", (.72, .78, .84, .72), .035, .02, .01),
        ("gemini-3.8-flash-high", "medium", (.65, .64, .68, .70), .020, .01, .01),
    ]
    ranking = []
    for index, (model, effort, abilities, cost, bonus, penalty) in enumerate(specs):
        excluded = ["below task ability floor"] if index == 4 else []
        components = dict(zip(("divergence", "global_rigor", "module_precision", "capability"), abilities))
        components.update(opportunity_cost=cost, reset_bonus=bonus, effort_penalty=penalty)
        ranking.append({
            "id": f"demo-{index + 1}",
            "bundle": {"model": model, "effort": effort},
            "score": None if excluded else round(sum(abilities) / 4 - cost + bonus - penalty, 4),
            "components": components,
            "excluded": excluded,
            "capacity": {"remaining": 72 - index * 7},
            "expected_pct": 6,
        })
    return theatre.snapshot_result({"decision_snapshot": {
        "task": {"objective": "Refactor payment retries with integration tests",
                 "work_kind": "general_implementation", "difficulty": "hard", "hours": 2},
        "ranking": ranking,
        "choice": "demo-1",
    }})


def main():
    result = synthetic_result()
    scene = theatre.scene_from_result(result)
    assert not scene.recomputed and scene.winner.id == "demo-1"
    stage = theatre.TheatreRenderer(scene, WIDTH, STAGE_HEIGHT, SCALE)
    strip = summary.SummaryRenderer(
        summary.summary_model(result, router_data.decision_pair(result)), WIDTH, SCALE
    ).render()
    frames = []
    for index in range(math.ceil(theatre.DURATION * FPS) + 1):
        frame = Image.new("RGB", (WIDTH, STAGE_HEIGHT + 16 + strip.height), "white")
        frame.paste(stage.frame(theatre.story_time(min(index / FPS, theatre.DURATION))), (0, 0))
        frame.paste(strip, (0, STAGE_HEIGHT + 16))
        frames.append(frame)

    # One shared adaptive palette prevents color shimmer; no error-diffusion grain.
    samples = [frames[round(i * (len(frames) - 1) / 31)].resize((340, 329)) for i in range(32)]
    atlas = Image.new("RGB", (340 * 8, 329 * 4))
    for i, sample in enumerate(samples):
        atlas.paste(sample, ((i % 8) * 340, (i // 8) * 329))
    palette = atlas.quantize(colors=248, method=Image.Quantize.MEDIANCUT)
    # Reserve the actual paper/strip and key ink colors, keeping large flat areas exact.
    reserved = [(254, 254, 253), (255, 255, 255), (79, 70, 229), (6, 182, 212),
                (15, 23, 42), (100, 116, 139), (148, 163, 184), (226, 232, 240)]
    palette.putpalette(palette.getpalette()[:248 * 3] + [v for rgb in reserved for v in rgb])
    encoded = []
    for frame in frames:
        indexed = frame.quantize(palette=palette, dither=Image.Dither.NONE)
        # Pillow's nearest-color lookup rounds RGB; pin exact flat backgrounds explicitly.
        for index, color in enumerate(reserved[:2], start=248):
            r, g, b = ImageChops.difference(frame, Image.new("RGB", frame.size, color)).split()
            mask = ImageChops.lighter(ImageChops.lighter(r, g), b).point(lambda v: 255 if v == 0 else 0)
            indexed.paste(index, mask=mask)
        encoded.append(indexed)
    durations = [1000 // FPS] * (len(encoded) - 1) + [1500]
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    encoded[0].save(OUTPUT, save_all=True, append_images=encoded[1:], duration=durations,
                    loop=0, optimize=True, disposal=1)
    if OUTPUT.stat().st_size > 8 * 1024 * 1024:
        raise RuntimeError("README GIF exceeds the 8 MiB limit")
    with Image.open(OUTPUT) as gif:
        print(f"{OUTPUT.name}: {gif.width}x{gif.height}, {gif.n_frames} frames, "
              f"{FPS} fps, {sum(durations)} ms, {OUTPUT.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
