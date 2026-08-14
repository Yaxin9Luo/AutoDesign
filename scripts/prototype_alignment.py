#!/usr/bin/env python3
"""Prototype: layout alignment / grid-regularity signal from the occupancy grid.

Idea: a well-organized poster snaps content to a few aligned columns separated by
clean vertical gutters, so content-run left/right edges concentrate on a few
column positions (low entropy). A jumbled poster scatters edges everywhere (high
entropy). We also count clean full-height gutters. This visualizes both so we can
confirm the signal separates clean from messy before wiring it into the score.
"""

from __future__ import annotations

import argparse
import html
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PIL import Image, ImageDraw

from autodesign.evaluator.ocr import run_ocr
from autodesign.evaluator.spatial import content_occupancy
from autodesign.evaluator.viz import image_b64
from autodesign.util.browser_render import screenshot_html


def alignment_metrics(occ_result: dict[str, Any]) -> dict[str, Any]:
    occ = occ_result["occ"]
    rows, cols = occ_result["rows"], occ_result["cols"]
    col_profile = [sum(1 for r in range(rows) if occ[r][c]) / max(1, rows) for c in range(cols)]
    row_profile = [sum(1 for c in range(cols) if occ[r][c]) / max(1, cols) for r in range(rows)]

    # content-run vertical edges per row (transitions empty<->content)
    edges: Counter[int] = Counter()
    for r in range(rows):
        prev = False
        for c in range(cols):
            cur = occ[r][c]
            if cur != prev:
                edges[c] += 1
            prev = cur
        if prev:
            edges[cols] += 1
    total = sum(edges.values())
    if total > 0 and cols > 1:
        probs = [v / total for v in edges.values()]
        ent = -sum(p * math.log(p) for p in probs)
        alignment = max(0.0, 1.0 - ent / math.log(cols))
    else:
        alignment = 0.0

    # clean vertical gutters: columns that are mostly empty top-to-bottom
    gutter_cols = [c for c in range(cols) if col_profile[c] <= 0.12]
    gutters = _contiguous(gutter_cols)
    # ignore the outer margins (first/last column) as gutters
    inner_gutters = [g for g in gutters if g[0] > 0 and g[1] < cols - 1]

    return {
        "col_profile": [round(v, 2) for v in col_profile],
        "row_profile": [round(v, 2) for v in row_profile],
        "edge_hist": dict(edges),
        "edge_entropy": round(ent, 3) if total > 0 else None,
        "alignment_score": round(alignment, 3),
        "inner_gutter_count": len(inner_gutters),
        "inner_gutters": inner_gutters,
    }


def _contiguous(cols: list[int]) -> list[tuple[int, int]]:
    if not cols:
        return []
    runs = []
    start = prev = cols[0]
    for c in cols[1:]:
        if c == prev + 1:
            prev = c
        else:
            runs.append((start, prev))
            start = prev = c
    runs.append((start, prev))
    return runs


def _overlay(base: Image.Image, occ_result: dict[str, Any], al: dict[str, Any]) -> Image.Image:
    canvas = base.convert("RGB").copy()
    draw = ImageDraw.Draw(canvas, "RGBA")
    cols, rows = occ_result["cols"], occ_result["rows"]
    w, h = canvas.size
    cw = w / cols
    # gutters as green bands
    for (g0, g1) in al["inner_gutters"]:
        draw.rectangle([g0 * cw, 0, (g1 + 1) * cw, h], fill=(0, 180, 0, 50))
    # content-edge columns as blue ticks (height ∝ edge count)
    hist = al["edge_hist"]
    mx = max(hist.values()) if hist else 1
    for c, n in hist.items():
        x = c * cw
        draw.line([x, h - int(60 * n / mx), x, h], fill=(20, 90, 220, 220), width=3)
    return canvas


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--candidate", action="append", required=True)
    p.add_argument("--out-dir", required=True, type=Path)
    args = p.parse_args()
    out_dir = args.out_dir.expanduser().resolve()
    (out_dir / "items").mkdir(parents=True, exist_ok=True)

    cards = []
    print(f"{'poster':12s} {'align':>6s} {'edge_ent':>8s} {'gutters':>7s}")
    for item in args.candidate:
        name, raw = item.split("=", 1)
        base = Image.open(Path(raw).expanduser()).convert("RGB")
        segs = (run_ocr(raw, include_segments=True) or {}).get("segments") or []
        occ = content_occupancy(base, segments=segs)
        al = alignment_metrics(occ)
        png = out_dir / "items" / f"{name}.png"
        _overlay(base, occ, al).save(png)
        print(f"{name:12s} {al['alignment_score']:>6.3f} {al['edge_entropy']!s:>8s} {al['inner_gutter_count']:>7d}")
        cards.append((name, al, png))

    html_path = out_dir / "alignment.html"
    body = "\n".join(
        f"<div class='card'><h3>{html.escape(n)} — align={a['alignment_score']} · "
        f"edge_entropy={a['edge_entropy']} · inner_gutters={a['inner_gutter_count']}</h3>"
        f"<img src='data:image/png;base64,{image_b64(png)}'></div>"
        for n, a, png in cards
    )
    html_path.write_text(
        "<!doctype html><meta charset='utf-8'><style>body{font-family:sans-serif;margin:20px;max-width:1100px}"
        ".card{margin:14px 0;border:1px solid #ddd;border-radius:8px;padding:10px}img{width:520px;border:1px solid #ccc}"
        "h3{font-size:14px}</style>"
        "<h1>Layout alignment prototype — green=gutters, blue ticks=content-edge columns</h1>" + body,
        encoding="utf-8",
    )
    report = out_dir / "alignment.png"
    screenshot_html(html_path, report, viewport_width=1120, viewport_height=2000, full_page=True, max_edge=3600, timeout_ms=30000)
    print(f"wrote {html_path} and {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
