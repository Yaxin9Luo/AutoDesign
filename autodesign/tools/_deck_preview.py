"""Deck main-preview grid compositor.

Given a list of per-slide PNG paths, produces a single `preview.png` thumbnail
grid for chat UX (so the user sees a deck-at-a-glance, not just slide 1).

Grid rules (visual density tuned for N slides):
- 1 slide:   single full-size tile
- 2-3 slides: horizontal row
- 4-8 slides: 2-column grid
- 9+ slides: 3-column grid (caps overall max width)
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image


_GAP_PX = 24
_PAD_PX = 24
_MAX_W = 1440


def build_deck_preview_grid(slide_pngs: list[Path], out_path: Path) -> None:
    paths = [p for p in slide_pngs if Path(p).exists()]
    if not paths:
        # Fall back to a white placeholder so downstream expectations hold.
        Image.new("RGB", (960, 540), (250, 250, 250)).save(out_path, format="PNG")
        return

    if len(paths) == 1:
        cols = 1
    elif len(paths) <= 3:
        cols = len(paths)
    elif len(paths) <= 8:
        cols = 2
    else:
        cols = 3
    rows = (len(paths) + cols - 1) // cols

    # Open first to size tiles.
    first = Image.open(paths[0]).convert("RGB")
    tile_w, tile_h = first.size

    # Cap overall width at _MAX_W (grid width = cols * tile_w + gaps + pad).
    total_w = cols * tile_w + (cols - 1) * _GAP_PX + 2 * _PAD_PX
    if total_w > _MAX_W:
        scale = (_MAX_W - 2 * _PAD_PX - (cols - 1) * _GAP_PX) / (cols * tile_w)
        tile_w = max(1, int(tile_w * scale))
        tile_h = max(1, int(tile_h * scale))
        total_w = cols * tile_w + (cols - 1) * _GAP_PX + 2 * _PAD_PX

    total_h = rows * tile_h + (rows - 1) * _GAP_PX + 2 * _PAD_PX
    canvas = Image.new("RGB", (total_w, total_h), (244, 246, 250))

    for i, p in enumerate(paths):
        r, c = divmod(i, cols)
        tile = Image.open(p).convert("RGB").resize((tile_w, tile_h), Image.LANCZOS)
        x = _PAD_PX + c * (tile_w + _GAP_PX)
        y = _PAD_PX + r * (tile_h + _GAP_PX)
        canvas.paste(tile, (x, y))

    canvas.save(out_path, format="PNG", optimize=True)
