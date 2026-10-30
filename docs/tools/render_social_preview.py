"""Compose docs/social-preview.png (1280x640) from a UI screenshot: python docs/tools/render_social_preview.py <screenshot.png>

Windows only (Segoe UI). Pass the output of render_ui_screenshots.py.
"""
from __future__ import annotations

from pathlib import Path
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "docs" / "social-preview.png"
FONTS = Path("C:/Windows/Fonts")
BG, ACCENT, WHITE = (16, 23, 39), (165, 160, 255), (255, 255, 255)
HEAD, SUB, TAG, MUTED, RULE, FRAME = (245, 247, 255), (185, 197, 218), (189, 201, 221), (143, 159, 185), (51, 64, 89), (72, 88, 118)
POINTS = (("01", "Task-aware routing", "Match model ability to the work."),
          ("02", "Quota economics", "Plan around 5h / 7d windows."),
          ("03", "Auditable decisions", "See every score and exclusion."))


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / name), size)


def ink(draw, x, y, text, face, fill, right=False, tracking=0.0):
    """Draw text so its ink box starts at (x, y), or ends at x when `right`."""
    if tracking:
        width = sum(face.getlength(c) + tracking for c in text) - tracking
    else:
        width = face.getlength(text)
    left, top = face.getbbox(text)[:2]
    x0 = x - width if right else x
    if tracking:
        cursor = x0
        for char in text:
            draw.text((cursor, y - top), char, font=face, fill=fill)
            cursor += face.getlength(char) + tracking
    else:
        draw.text((x0 - left, y - top), text, font=face, fill=fill)


def main() -> None:
    shot = Image.open(sys.argv[1]).convert("RGB")
    card = Image.new("RGB", (1280, 640), BG)
    draw = ImageDraw.Draw(card)
    draw.rectangle((0, 0, 1280, 7), fill=ACCENT)
    ink(draw, 48, 47, "AgentCompass", font("segoeuib.ttf", 58), WHITE)
    ink(draw, 50, 111, "The right AI worker. The right quota. Every task.", font("segoeuil.ttf", 26), TAG)
    draw.line((48, 155, 1232, 155), fill=RULE)
    for index, (number, heading, sub) in enumerate(POINTS):
        top = 113 * index
        ink(draw, 49, 207 + top, number, font("segoeuib.ttf", 17), ACCENT)
        ink(draw, 88, 202 + top, heading, font("segoeuib.ttf", 25), HEAD)
        ink(draw, 89, 237 + top, sub, font("segoeui.ttf", 18), SUB)
    ink(draw, 50, 602, "github.com/akiiwa-games/AgentCompass", font("segoeui.ttf", 17), MUTED)
    ink(draw, 1195, 602, "MIT / OPEN SOURCE", font("seguisb.ttf", 16), ACCENT, right=True)

    width = 652
    height = round(width * shot.height / shot.width)
    window = shot.resize((width, height), Image.Resampling.LANCZOS)
    left, top = 521, 175
    shadow = Image.new("RGBA", card.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rectangle((left + 2, top + 8, left + width + 2, top + height + 8), fill=(0, 0, 0, 150))
    card = Image.alpha_composite(card.convert("RGBA"), shadow.filter(ImageFilter.GaussianBlur(10)))
    card.paste(window, (left, top))
    ImageDraw.Draw(card).rectangle((left - 1, top - 1, left + width, top + height), outline=FRAME)
    card.convert("RGB").save(OUTPUT, optimize=True)
    print(f"{OUTPUT}: {OUTPUT.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
