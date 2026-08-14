#!/usr/bin/env python3
"""Sweep the image-native density/layout metrics over many genuine posters.

Discovers real ``.paper-poster`` runs under out/runs, takes a density-stratified
sample, scores each (image mode, no paper), writes a CSV/JSON, and flags likely
blind-spot cases via heuristics so we can eyeball where the metric disagrees with
reality.

Usage:
    uv run python scripts/eval_metric_sweep.py --limit 40 --out-dir out/eval/sweep
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PIL import Image

from autodesign.evaluator.quality_rubric import compute_deterministic_report


def discover_posters() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for fin in sorted(Path("out/runs").glob("*/final")):
        htmls = [h for h in fin.glob("*.html") if h.is_file()]
        is_poster = any("paper-poster" in h.read_text(errors="replace")[:20000] for h in htmls)
        prev = fin / "preview.png"
        if not is_poster or not prev.exists():
            continue
        try:
            im = Image.open(prev).convert("L")
            w, h = im.size
            im.thumbnail((400, 400))
            hist = im.histogram()
            nw = sum(hist[:245]) / max(1, sum(hist))
        except Exception:
            continue
        out.append({"run": fin.parent.name, "preview": prev, "nonwhite": round(nw, 3), "aspect": round(h / w, 2)})
    out.sort(key=lambda r: r["nonwhite"])
    return out


def stratified(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if limit >= len(items) or limit <= 0:
        return items
    step = len(items) / limit
    return [items[int(i * step)] for i in range(limit)]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--out-dir", required=True, type=Path)
    args = p.parse_args()
    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    posters = discover_posters()
    sample = stratified(posters, args.limit)
    print(f"discovered {len(posters)} genuine posters; scoring {len(sample)}", file=sys.stderr)

    rows: list[dict[str, Any]] = []
    for i, item in enumerate(sample):
        print(f"  [{i+1}/{len(sample)}] {item['run']}", file=sys.stderr)
        try:
            r = compute_deterministic_report(
                paper=None, candidate_artifact=item["preview"], out_dir=out_dir / "items" / item["run"],
            )
        except Exception as exc:  # noqa: BLE001
            print(f"    error: {exc}", file=sys.stderr)
            continue
        sp = r.get("spatial", {})
        ocr = r.get("ocr", {})
        img = r.get("metric_bundles", {}).get("image_density", {})
        comps = r.get("dimension_components", {})
        rows.append({
            "run": item["run"],
            "aspect": item["aspect"],
            "nonwhite": img.get("nonwhite_pixel_ratio"),
            "content_cov": sp.get("content_coverage"),
            "empty_frac": sp.get("empty_cell_fraction"),
            "largest_void": sp.get("largest_empty_rect_cell_ratio"),
            "ocr_words": ocr.get("word_count"),
            "text_cov": ocr.get("text_coverage_ratio"),
            "density": (comps.get("information_density_and_synthesis") or {}).get("score_0_10"),
            "layout": (comps.get("layout_readability") or {}).get("score_0_10"),
            "preview": str(item["preview"]),
        })

    _write(out_dir, rows)
    _flag(rows)
    print(f"\nwrote {out_dir/'sweep.csv'} and sweep.json ({len(rows)} rows)")
    return 0


def _write(out_dir: Path, rows: list[dict[str, Any]]) -> None:
    (out_dir / "sweep.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    if rows:
        with (out_dir / "sweep.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _flag(rows: list[dict[str, Any]]) -> None:
    """Heuristics that surface likely blind spots (metric vs reality disagreement)."""
    flags: dict[str, list[str]] = {
        "over_detect_low_text": [],     # full content but almost no text → texture/noise false positive
        "void_but_high_density": [],    # big empty region yet density stayed high
        "ink_content_disagree": [],     # lots of ink but low content → big solid/dark blocks
        "layout_high_with_void": [],    # layout high despite a sizable void
        "dense_pixels_low_density": [],  # very inky but density low → possible under-detection
    }
    for r in rows:
        cc, void, dens, lay = _num(r["content_cov"]), _num(r["largest_void"]), _num(r["density"]), _num(r["layout"])
        nw, words = _num(r["nonwhite"]), _num(r["ocr_words"])
        tag = f"{r['run']} (cc={cc:.2f} void={void:.2f} d={dens} l={lay} nw={nw:.2f} w={words})"
        if cc > 0.95 and words < 150:
            flags["over_detect_low_text"].append(tag)
        if void >= 0.10 and dens >= 7.0:
            flags["void_but_high_density"].append(tag)
        if nw >= 0.5 and cc < 0.6:
            flags["ink_content_disagree"].append(tag)
        if lay >= 7.0 and void >= 0.10:
            flags["layout_high_with_void"].append(tag)
        if nw >= 0.45 and dens < 5.0:
            flags["dense_pixels_low_density"].append(tag)

    print("\n=== blind-spot flags ===", file=sys.stderr)
    for name, items in flags.items():
        print(f"\n[{name}] {len(items)}", file=sys.stderr)
        for t in items[:12]:
            print(f"  {t}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
