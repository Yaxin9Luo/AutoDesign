#!/usr/bin/env python3
"""Visualize the CV figure/visual-region detection that will feed visual_evidence_use.

Measurement-first: before wiring any score, look at what the detector actually finds
on real posters. Draws every detected figure region on the poster (body figures in
green, heading logos in blue), labeled with its area %, and reports per-poster signals
— figure_region_count, figure_area_ratio, text_coverage — plus PROVISIONAL two-sided
flags (no_visual_evidence / screenshot_wall). The provisional thresholds are only for
sorting/eyeballing; the report's distribution table is what we use to calibrate the
real thresholds.

Reuses basic_layout_integrity's existing CV region detection (no new CV cost).

Usage:
    uv run python scripts/visualize_visual_evidence.py --posters-dir DesignAnything_Poster
"""

from __future__ import annotations

import argparse
import base64
import html as H
import io
import sys
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from autodesign.evaluator.metrics import (  # noqa: E402
    VISUAL_EVIDENCE_WALL_AREA,
    VISUAL_EVIDENCE_WALL_TEXT,
)
from autodesign.evaluator.ocr import run_ocr  # noqa: E402
from autodesign.evaluator.spatial import basic_layout_integrity, is_figure_shaped  # noqa: E402

BODY_COLOR = (24, 140, 70)
HEAD_COLOR = (60, 110, 210)
DROP_COLOR = (150, 150, 160)


def _is_figure_shaped(region: dict[str, Any], W: int, Hh: int) -> bool:
    """Visualizer wrapper over the production filter (spatial.is_figure_shaped)."""
    return is_figure_shaped(region.get("rect") or {}, float(min(W, Hh)), float(W * Hh))


def _font(size: int):
    for p in ("/System/Library/Fonts/Supplemental/Arial.ttf", "/Library/Fonts/Arial.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _area_of(region: dict[str, Any]) -> float:
    """The figure's rectangular footprint (bbox), falling back to the pixel area."""
    if region.get("bbox_area") is not None:
        return float(region["bbox_area"])
    rect = region.get("rect") or {}
    if rect.get("w") is not None and rect.get("h") is not None:
        return float(rect["w"]) * float(rect["h"])
    return float(region.get("area") or 0.0)


def analyze(poster: Path) -> dict[str, Any]:
    img = Image.open(poster).convert("RGB")
    W, Hh = img.width, img.height
    canvas = float(max(1, W * Hh))
    ocr = run_ocr(poster, include_segments=True)
    segs = ocr.get("segments") or []
    text_cov = float(ocr.get("text_coverage_ratio") or 0.0) if ocr.get("available") else None

    li = basic_layout_integrity(img, segments=segs, include_debug_regions=True)
    dbg = li.get("debug_regions") or {}
    raw_body = list(dbg.get("visuals") or [])
    head = list(dbg.get("heading_visuals") or [])
    figs = [r for r in raw_body if _is_figure_shaped(r, W, Hh)]
    dropped = [r for r in raw_body if not _is_figure_shaped(r, W, Hh)]

    fig_area_ratio = sum(_area_of(r) for r in figs) / canvas

    draw = ImageDraw.Draw(img, "RGBA")
    scale = max(W, Hh) / 1600.0
    fsize = max(13, int(16 * scale))
    font = _font(fsize)
    # dropped strips drawn faint; real figures solid green; heading logos blue
    for region in dropped:
        rect = region.get("rect") or {}
        x0, y0, x1, y1 = rect.get("x0"), rect.get("y0"), rect.get("x1"), rect.get("y1")
        if None in (x0, y0, x1, y1):
            continue
        draw.rectangle([x0, y0, x1, y1], outline=DROP_COLOR + (150,), width=max(1, int(scale)))
    for region, color in [(r, BODY_COLOR) for r in figs] + [(r, HEAD_COLOR) for r in head]:
        rect = region.get("rect") or {}
        x0, y0, x1, y1 = rect.get("x0"), rect.get("y0"), rect.get("x1"), rect.get("y1")
        if None in (x0, y0, x1, y1):
            continue
        draw.rectangle([x0, y0, x1, y1], fill=color + (38,))
        draw.rectangle([x0, y0, x1, y1], outline=color + (255,), width=max(2, int(3 * scale)))
        pct = 100.0 * _area_of(region) / canvas
        draw.text((x0 + 3, max(0, y0 - fsize - 2)), f"{pct:.1f}%", fill=color + (255,), font=font)

    empty = len(figs) == 0  # production "no_figures_detected" flag
    wall = (fig_area_ratio >= VISUAL_EVIDENCE_WALL_AREA) and (text_cov is not None and text_cov <= VISUAL_EVIDENCE_WALL_TEXT)

    return {
        "image": img,
        "figure_region_count": len(figs),
        "raw_region_count": len(raw_body),
        "heading_logo_count": len(head),
        "figure_area_ratio": round(fig_area_ratio, 4),
        "text_coverage": round(text_cov, 4) if text_cov is not None else None,
        "prov_empty": empty,
        "prov_wall": wall,
        "regions": [{"rect": r.get("rect"), "area_ratio": round(_area_of(r) / canvas, 4)} for r in figs],
    }


def _thumb_b64(img: Image.Image, max_w: int = 900) -> str:
    if img.width > max_w:
        img = img.resize((max_w, int(img.height * max_w / img.width)))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=82)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _discover(root: Path) -> list[tuple[str, Path]]:
    out = []
    for preview in sorted(root.rglob("preview.png")):
        out.append((f"{preview.parent.parent.name}/{preview.parent.name}", preview))
    return out


def _percentiles(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    s = sorted(values)
    def pct(p: float) -> float:
        return round(s[min(len(s) - 1, int(p * (len(s) - 1)))], 4)
    return {"p10": pct(0.10), "p25": pct(0.25), "p50": pct(0.50), "p75": pct(0.75), "p90": pct(0.90)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--posters-dir", type=Path, default=_REPO / "DesignAnything_Poster")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", type=Path, default=_REPO / "out/eval/report/visual_evidence_report.html")
    args = ap.parse_args(argv)
    root = args.posters_dir if args.posters_dir.is_absolute() else _REPO / args.posters_dir
    found = _discover(root)
    if args.limit:
        found = found[:args.limit]
    print(f"analyzing {len(found)} posters for visual-evidence detection ...", flush=True)

    items: list[tuple[str, dict[str, Any]]] = []
    for i, (name, preview) in enumerate(found, 1):
        res = analyze(preview)
        items.append((name, res))
        print(f"  [{i}/{len(found)}] {name[:46]:48s} figs={res['raw_region_count']:>2}->{res['figure_region_count']:<2} "
              f"area={res['figure_area_ratio']} text={res['text_coverage']} "
              f"{'EMPTY' if res['prov_empty'] else ''}{'WALL' if res['prov_wall'] else ''}", flush=True)

    counts = [r["figure_region_count"] for _, r in items]
    areas = [r["figure_area_ratio"] for _, r in items]
    texts = [r["text_coverage"] for _, r in items if r["text_coverage"] is not None]
    n_empty = sum(1 for _, r in items if r["prov_empty"])
    n_wall = sum(1 for _, r in items if r["prov_wall"])
    print("\n=== distributions (for threshold calibration) ===", flush=True)
    print(f"  figure_region_count: {_percentiles([float(c) for c in counts])}  zero-figure posters={counts.count(0)}", flush=True)
    print(f"  figure_area_ratio:   {_percentiles(areas)}", flush=True)
    print(f"  text_coverage:       {_percentiles(texts)}", flush=True)
    print(f"  provisional flags:   empty={n_empty}  wall={n_wall}  (of {len(items)})", flush=True)

    ranked = sorted(items, key=lambda it: it[1]["figure_area_ratio"])
    srows = "".join(
        f'<tr><td><a href="#p{i}">{H.escape(n)}</a></td>'
        f'<td class="num">{r["figure_region_count"]}</td>'
        f'<td class="num">{r["figure_area_ratio"]}</td>'
        f'<td class="num">{r["text_coverage"]}</td>'
        f'<td>{"EMPTY" if r["prov_empty"] else ""}{" WALL" if r["prov_wall"] else ""}</td></tr>'
        for i, (n, r) in enumerate(ranked)
    )
    cards = "".join(
        f'<div class="card" id="p{i}"><h3>{H.escape(n)}</h3>'
        f'<div class="meta">figures <b>{r["figure_region_count"]}</b> (raw {r["raw_region_count"]}) · area <b>{r["figure_area_ratio"]}</b>'
        f' · text {r["text_coverage"]} · heading-logos {r["heading_logo_count"]}'
        f'{" · <b style=color:#b3261e>EMPTY</b>" if r["prov_empty"] else ""}'
        f'{" · <b style=color:#b3261e>WALL</b>" if r["prov_wall"] else ""}</div>'
        f'<img src="{_thumb_b64(r["image"])}"></div>'
        for i, (n, r) in enumerate(ranked)
    )
    html_doc = f"""<!doctype html><meta charset="utf-8"><title>Visual evidence detection</title>
<style>
 body{{font:15px/1.6 -apple-system,'PingFang SC',Segoe UI,sans-serif;color:#1c2330;max-width:1000px;margin:0 auto;padding:24px 18px 70px}}
 h1{{font-size:24px}} h3{{font-size:15px;margin:4px 0}}
 .lede{{background:#f3f8fc;border-left:4px solid #5b8def;padding:11px 16px;border-radius:0 10px 10px 0;font-size:14px}}
 .card{{border:1px solid #e4e7ee;border-radius:12px;padding:14px 16px;margin:14px 0;background:#fff;scroll-margin-top:14px}}
 .card img{{width:100%;border:1px solid #eceff3;border-radius:8px;margin-top:8px}}
 .meta{{font-size:13.5px;color:#3a4250}} table{{border-collapse:collapse;width:100%;margin:8px 0;font-size:12.5px}}
 td,th{{border:1px solid #e4e7ee;padding:5px 8px;text-align:left}} th{{background:#f6f7f9}} .num{{text-align:right;font-variant-numeric:tabular-nums}}
 a{{color:#2a52a0}}
</style>
<h1>视觉证据检测 · Visual Evidence Detection</h1>
<p class="lede"><b>绿框=正文图区,蓝框=标题 logo</b>(复用 basic_layout_integrity 的 CV 检测,标签为面积占比)。
这是 measurement-first:先肉眼看检测准不准(误检文字块?漏检稀疏图?),并用下方分布标定 <b>empty/wall</b> 的真实阈值。
当前 EMPTY/WALL 是临时阈值仅供排序。</p>
<h2>Overview · 按图面积升序(零图在前)</h2>
<table><tr><th>poster</th><th>figs</th><th>fig area</th><th>text cov</th><th>prov flag</th></tr>{srows}</table>
{cards}
<p style="color:#69707d;font-size:12px;margin-top:24px">Generated by scripts/visualize_visual_evidence.py</p>
"""
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html_doc, encoding="utf-8")
    print(f"wrote {args.out} ({len(html_doc)//1024} KB, {len(items)} posters)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
