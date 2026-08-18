"""Build the WeChat footer guide image from a generated background.

The background can stay visual and text-free; this script renders the Chinese
copy with an installed font so the wording is deterministic and crisp.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFont


WIDTH = 1080
HEIGHT = 480
BRAND = "#AB1942"
NAVY = "#22324A"


def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size=size)


def _centered(draw: ImageDraw.ImageDraw, xy_y: int, text: str, font, fill: str) -> None:
    box = draw.textbbox((0, 0), text, font=font)
    width = box[2] - box[0]
    draw.text(((WIDTH - width) / 2, xy_y), text, font=font, fill=fill)


def build(background: Path, output: Path, regular_font: Path, bold_font: Path) -> None:
    source = Image.open(background).convert("RGB")
    scale = max(WIDTH / source.width, HEIGHT / source.height)
    resized = source.resize(
        (round(source.width * scale), round(source.height * scale)),
        Image.Resampling.LANCZOS,
    )
    left = max(0, (resized.width - WIDTH) // 2)
    top = max(0, (resized.height - HEIGHT) // 2)
    canvas = resized.crop((left, top, left + WIDTH, top + HEIGHT))
    canvas = ImageEnhance.Contrast(canvas).enhance(0.96).convert("RGBA")

    overlay = Image.new("RGBA", canvas.size, (255, 255, 255, 0))
    odraw = ImageDraw.Draw(overlay)
    odraw.rounded_rectangle(
        (165, 58, WIDTH - 165, HEIGHT - 52),
        radius=30,
        fill=(255, 255, 255, 220),
        outline=(171, 25, 66, 38),
        width=2,
    )
    canvas = Image.alpha_composite(canvas, overlay)
    draw = ImageDraw.Draw(canvas)

    regular = _font(regular_font, 31)
    small = _font(regular_font, 22)
    bold = _font(bold_font, 60)

    pill_text = "ORIGINAL RESEARCH"
    pill_box = draw.textbbox((0, 0), pill_text, font=small)
    pill_w = pill_box[2] - pill_box[0] + 42
    pill_h = 42
    pill_x = (WIDTH - pill_w) / 2
    draw.rounded_rectangle(
        (pill_x, 86, pill_x + pill_w, 86 + pill_h),
        radius=21,
        fill=BRAND,
    )
    text_x = (WIDTH - (pill_box[2] - pill_box[0])) / 2
    draw.text((text_x, 92), pill_text, font=small, fill="white")

    _centered(draw, 150, "获取原文 PDF", bold, NAVY)
    _centered(draw, 239, "点击文末「阅读原文」查看并下载", regular, BRAND)

    arrow_y = 320
    draw.line((510, arrow_y, 540, arrow_y + 28), fill=BRAND, width=7)
    draw.line((570, arrow_y, 540, arrow_y + 28), fill=BRAND, width=7)
    draw.line((516, arrow_y + 38, 540, arrow_y + 60), fill=NAVY, width=5)
    draw.line((564, arrow_y + 38, 540, arrow_y + 60), fill=NAVY, width=5)

    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(output, format="PNG", optimize=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("background", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--font", type=Path, default=Path(r"C:\Windows\Fonts\msyh.ttc"))
    parser.add_argument("--bold-font", type=Path, default=Path(r"C:\Windows\Fonts\msyhbd.ttc"))
    args = parser.parse_args()
    build(args.background, args.output, args.font, args.bold_font)


if __name__ == "__main__":
    main()
