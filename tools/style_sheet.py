#!/usr/bin/env python3
"""Regenerate the wallpaper style sheet the README shows, using the builder itself.

The picture in ``docs/wallpaper-styles.png`` is not a screenshot and not a file somebody
pasted in: every tile is drawn by ``os_builder.wallpaper_pixels``, the same function that
renders the PNG shipped inside a real image. That is the whole reason this tool exists --
a README image that is generated cannot lie about what the program produces.

    python3 tools/style_sheet.py                            # rewrite docs/wallpaper-styles.png
    python3 tools/style_sheet.py --tile 320x180 --out /tmp/sheet.png

Keep the default small: a README image that costs tens of kilobytes is one people still
load, and every style here is cheap to redraw.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # the tool must work from any folder
    sys.path.insert(0, str(ROOT))

import os_builder as ob  # noqa: E402 - the sys.path line above is what makes this work

BACKGROUND = (16, 18, 24)


def parse_tile(text: str) -> tuple[int, int]:
    """``240x135`` for one tile; small enough that the sheet stays a README image.

    Deliberately not :func:`os_builder.parse_size`, whose bounds are the ones a *desktop
    wallpaper* needs: a contact sheet is allowed to draw a 48x27 thumbnail.
    """
    match = ob.SIZE_RE.match(text.strip())
    if match is None:
        raise SystemExit(f"style_sheet: --tile wants WxH, like 240x135, got {text!r}")
    width, height = int(match.group(1)), int(match.group(2))
    if not (16 <= width <= 1024 and 16 <= height <= 1024):
        raise SystemExit(f"style_sheet: --tile must be between 16x16 and 1024x1024, got {text!r}")
    return width, height


def build_sheet(
    styles: list[str], tile: tuple[int, int], columns: int, seed: int, gap: int, accent: str
) -> list[list[tuple[int, int, int]]]:
    """Render every style and stitch the tiles into one grid, as rows of RGB triples."""
    width, height = tile
    rows = math.ceil(len(styles) / columns)
    sheet_w = columns * width + (columns + 1) * gap
    sheet_h = rows * height + (rows + 1) * gap
    canvas: list[list[tuple[int, int, int]]] = [[BACKGROUND] * sheet_w for _ in range(sheet_h)]
    for index, style in enumerate(styles):
        row, column = divmod(index, columns)
        recipe = ob.Recipe(
            name="Style Sheet",
            wallpaper_style=style,
            wallpaper_seed=seed,
            accent=accent,
            wallpaper_size=f"{width}x{height}",
        )
        top = gap + row * (height + gap)
        left = gap + column * (width + gap)
        for offset, line in enumerate(ob.wallpaper_pixels(recipe, width, height)):
            target = canvas[top + offset]
            for x, pixel in enumerate(line):
                target[left + x] = pixel
    return canvas


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="draw the README's wallpaper style sheet")
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "wallpaper-styles.png")
    parser.add_argument("--tile", default="240x135", help="one style, as WxH (default 240x135)")
    parser.add_argument("--columns", type=int, default=4)
    parser.add_argument("--gap", type=int, default=6)
    parser.add_argument("--seed", type=int, default=7, help="which variation of each style to draw")
    parser.add_argument("--accent", default="#4f9cf9", help="the accent every tile is drawn with")
    args = parser.parse_args(argv)

    styles = list(ob.WALLPAPER_STYLES)
    tile = parse_tile(args.tile)
    columns = max(1, min(args.columns, len(styles)))
    gap = max(0, args.gap)
    if not ob.HEX_COLOR_RE.match(args.accent):
        raise SystemExit(f"style_sheet: --accent {args.accent!r} is not a #rrggbb colour")
    rows = build_sheet(styles, tile, columns, args.seed, gap, args.accent)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    written = ob.write_png(args.out, rows)
    print(f"style_sheet: wrote {args.out} ({written[0]}x{written[1]}, {len(styles)} styles at {args.tile})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
