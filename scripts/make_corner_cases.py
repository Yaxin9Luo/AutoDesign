#!/usr/bin/env python3
"""Generate synthetic corner-case 'posters' and probe the density/layout detector.

Each case targets a known-risky mode (blank, text wall, solid color block, dark
void, random noise, gradient, single figure, thin gaps, scattered sparse). We
score each with the production path and compare to the expected behavior, so
blind spots (e.g. noise read as content) become obvious.

Usage:
    uv run python scripts/make_corner_cases.py --out-dir out/eval/corner
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path
from typing import Any, Callable

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PIL import Image, ImageDraw

from autodesign.evaluator.quality_rubric import compute_deterministic_report
from autodesign.evaluator.spatial import content_occupancy, occupancy_overlay

W, H = 1600, 800
random.seed(7)


def _blank() -> Image.Image:
    return Image.new("RGB", (W, H), "white")


def _text_block(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], line_h: int = 14, jitter: bool = True) -> None:
    x0, y0, x1, y1 = box
    y = y0 + 6
    while y < y1 - 6:
        x = x0 + 6
        while x < x1 - 10:
            seg = random.randint(20, 70) if jitter else 50
            draw.rectangle([x, y, min(x + seg, x1 - 6), y + 4], fill=(30, 30, 30))
            x += seg + random.randint(6, 16)
        y += line_h


def _full_text() -> Image.Image:
    im = _blank()
    _text_block(ImageDraw.Draw(im), (40, 40, W - 40, H - 40))
    return im


def _top_half_text() -> Image.Image:
    im = _blank()
    _text_block(ImageDraw.Draw(im), (40, 40, W - 40, H // 2))
    return im


def _solid_color_block() -> Image.Image:
    im = _blank()
    d = ImageDraw.Draw(im)
    _text_block(d, (40, 40, W // 3, H - 40))
    d.rectangle([W // 3 + 10, 40, W - 40, H - 40], fill=(214, 228, 246))  # flat pale panel, no content
    return im


def _black_void_center() -> Image.Image:
    im = _full_text()
    ImageDraw.Draw(im).rectangle([W // 3, H // 4, 2 * W // 3, 3 * H // 4], fill=(0, 0, 0))
    return im


def _noise() -> Image.Image:
    im = Image.new("RGB", (W, H))
    im.putdata([(random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)) for _ in range(W * H)])
    return im


def _gradient() -> Image.Image:
    im = Image.new("RGB", (W, H))
    px = im.load()
    for y in range(H):
        v = 230 - int(60 * y / H)
        for x in range(W):
            px[x, y] = (v, v, v + 6)
    _text_block(ImageDraw.Draw(im), (60, 60, W // 2, H // 3))  # sparse text on gradient
    return im


def _single_figure() -> Image.Image:
    im = _blank()
    fig = Image.new("RGB", (int(W * 0.7), int(H * 0.7)))
    fig.putdata([(random.randint(40, 210), random.randint(40, 210), random.randint(60, 230)) for _ in range(fig.size[0] * fig.size[1])])
    im.paste(fig, (int(W * 0.15), int(H * 0.15)))  # one big "photo", no text
    return im


def _thin_gaps() -> Image.Image:
    im = _blank()
    d = ImageDraw.Draw(im)
    cols = 4
    gw = 16
    cw = (W - 80 - gw * (cols - 1)) / cols
    for c in range(cols):
        x0 = int(40 + c * (cw + gw))
        _text_block(d, (x0, 40, int(x0 + cw), H - 40))
    return im


def _scattered_sparse() -> Image.Image:
    im = _blank()
    d = ImageDraw.Draw(im)
    for _ in range(6):
        x = random.randint(40, W - 300)
        y = random.randint(40, H - 120)
        _text_block(d, (x, y, x + 240, y + 80))
    return im


CASES: list[tuple[str, Callable[[], Image.Image], str]] = [
    ("blank", _blank, "density~0, layout~0"),
    ("full_text", _full_text, "both high"),
    ("top_half_text", _top_half_text, "big bottom void → layout low"),
    ("solid_color_block", _solid_color_block, "right color panel = empty → low/mid"),
    ("black_void_center", _black_void_center, "center void → layout low"),
    ("noise", _noise, "BLIND SPOT: noise may read as full"),
    ("gradient_bg", _gradient, "flat gradient = empty; only text counts"),
    ("single_figure", _single_figure, "wordless figure: high content, 0 words"),
    ("thin_gaps", _thin_gaps, "thin gutters → should stay ~full"),
    ("scattered_sparse", _scattered_sparse, "mostly empty → low"),
]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out-dir", required=True, type=Path)
    args = p.parse_args()
    out_dir = args.out_dir.expanduser().resolve()
    cases_dir = out_dir / "cases"
    cases_dir.mkdir(parents=True, exist_ok=True)

    print(f"{'case':18s} {'content':>7s} {'void':>5s} {'density':>7s} {'layout':>6s}  expectation")
    for name, fn, expect in CASES:
        img = fn()
        png = cases_dir / f"{name}.png"
        img.save(png)
        r = compute_deterministic_report(paper=None, candidate_artifact=png, out_dir=out_dir / "items" / name)
        sp = r.get("spatial", {})
        c = r.get("dimension_components", {})
        dens = (c.get("information_density_and_synthesis") or {}).get("score_0_10")
        lay = (c.get("layout_readability") or {}).get("score_0_10")
        occ = content_occupancy(img.convert("RGB"))
        occupancy_overlay(img.convert("RGB"), occ).save(cases_dir / f"{name}_occ.png")
        print(f"{name:18s} {sp.get('content_coverage')!s:>7s} {sp.get('largest_empty_rect_cell_ratio')!s:>5s} "
              f"{dens!s:>7s} {lay!s:>6s}  {expect}")
    print(f"\noverlays + cases in {cases_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
