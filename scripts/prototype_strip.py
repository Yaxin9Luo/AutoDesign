#!/usr/bin/env python3
"""Prototype: dedicated horizontal blank-strip detector (section-bottom gaps).

Coarse content_coverage misses these gaps (cells absorb them) and a globally
finer grid over-detects normal line spacing. So detect blank strips separately:
scan each column in thin horizontal slices, keep only runs of >= min_run
consecutive blank slices (so single line/paragraph gaps are filtered, real
section gaps survive), then take the largest contiguous blank strip.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PIL import Image, ImageDraw, ImageStat

from autodesign.evaluator.spatial import _largest_rect


def strip_metrics(
    image: Image.Image, *, heading_fraction: float = 0.14, col_px: int = 64,
    row_px: int = 14, ds: int = 4, std_threshold: float = 12.0, min_run: int = 3,
) -> dict[str, Any]:
    gray = image.convert("L")
    w, h = gray.size
    cols = max(1, round(w / col_px))
    rows = max(1, round(h / row_px))
    cw, ch = w / cols, h / rows
    heading_rows = int(round(rows * heading_fraction))
    small = gray.resize((max(1, w // ds), max(1, h // ds)), Image.BILINEAR)
    sw, sh = small.size
    scw, sch = sw / cols, sh / rows

    empty = [[False] * cols for _ in range(rows)]
    for r in range(rows):
        if r < heading_rows:
            continue
        for c in range(cols):
            cell = small.crop((int(c * scw), int(r * sch),
                               max(int(c * scw) + 1, int((c + 1) * scw)),
                               max(int(r * sch) + 1, int((r + 1) * sch))))
            empty[r][c] = ImageStat.Stat(cell).stddev[0] < std_threshold

    # keep only vertical runs >= min_run (filters line/paragraph gaps)
    strip = [[False] * cols for _ in range(rows)]
    for c in range(cols):
        run = 0
        for r in range(rows + 1):
            is_empty = r < rows and empty[r][c]
            if is_empty:
                run += 1
            else:
                if run >= min_run:
                    for rr in range(r - run, r):
                        strip[rr][c] = True
                run = 0

    area, (r0, c0, r1, c1) = _largest_rect(strip)
    body = max(1, (rows - heading_rows) * cols)
    rect = {"x0": int(c0 * cw), "y0": int(r0 * ch), "x1": int((c1 + 1) * cw), "y1": int((r1 + 1) * ch)} if area > 0 else None
    strip_cells = sum(1 for r in range(rows) for c in range(cols) if strip[r][c])
    return {
        "cols": cols, "rows": rows, "heading_rows": heading_rows, "cw": cw, "ch": ch,
        "strip": strip, "largest_strip_ratio": round(area / body, 4),
        "strip_area_ratio": round(strip_cells / body, 4), "rect_px": rect,
    }


def overlay(image: Image.Image, m: dict[str, Any]) -> Image.Image:
    canvas = image.convert("RGB").copy()
    d = ImageDraw.Draw(canvas, "RGBA")
    cw, ch = m["cw"], m["ch"]
    for r in range(m["rows"]):
        for c in range(m["cols"]):
            if m["strip"][r][c]:
                d.rectangle([c * cw, r * ch, (c + 1) * cw, (r + 1) * ch], fill=(255, 40, 40, 90))
    if m["heading_rows"]:
        d.rectangle([0, 0, canvas.size[0], m["heading_rows"] * ch], fill=(150, 150, 150, 30))
    if m["rect_px"]:
        rp = m["rect_px"]
        d.rectangle([rp["x0"], rp["y0"], rp["x1"], rp["y1"]], outline=(220, 0, 0, 255), width=5)
    return canvas


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--candidate", action="append", required=True)
    p.add_argument("--out-dir", required=True, type=Path)
    p.add_argument("--min-run", type=int, default=3)
    p.add_argument("--row-px", type=int, default=14)
    args = p.parse_args()
    out = args.out_dir.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    print(f"{'poster':14s} {'largest_strip':>13s} {'strip_area':>10s}  (min_run={args.min_run}, row_px={args.row_px})")
    for item in args.candidate:
        name, raw = item.split("=", 1)
        img = Image.open(Path(raw).expanduser()).convert("RGB")
        m = strip_metrics(img, min_run=args.min_run, row_px=args.row_px)
        overlay(img, m).save(out / f"{name}_strip.png")
        print(f"{name:14s} {m['largest_strip_ratio']:>13.4f} {m['strip_area_ratio']:>10.4f}")
    print(f"overlays in {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
