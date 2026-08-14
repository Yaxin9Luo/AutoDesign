#!/usr/bin/env python3
"""Unified calibration of the density/layout SCORING params.

Detection params (cell size, std threshold, min_run, heading fraction, noise
downsample) are held at their validated values; only the raw-signal -> 0-10
mapping is tuned. Because the raw signals (content_coverage, largest_blank_strip,
strip_area, text_coverage) do not depend on the scoring params, we cache them once
over a large stratified poster sample + corner cases, then grid-search the scoring
params cheaply.

Objective:
  * corner cases are ground truth — params must land each within its target range;
  * on the real sample, reward spread (discrimination) and penalize saturation;
  * prefer a config in a STABLE region (low objective variance under perturbation).

    uv run python scripts/calibrate_density_layout.py --build-cache --limit 100 --out out/eval/calib
    uv run python scripts/calibrate_density_layout.py --search --out out/eval/calib
"""

from __future__ import annotations

import argparse
import itertools
import json
import statistics as stats
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PIL import Image

from autodesign.evaluator.quality_rubric import compute_deterministic_report


# Corner-case ground-truth target ranges (density, layout). None = unconstrained.
CORNER_TARGETS: dict[str, dict[str, tuple[float, float] | None]] = {
    "blank": {"density": (0.0, 1.0), "layout": (0.0, 1.0)},
    "full_text": {"density": (8.2, 10.0), "layout": (8.0, 10.0)},
    "top_half_text": {"density": (1.0, 4.5), "layout": (0.0, 2.5)},
    "solid_color_block": {"density": (1.0, 4.5), "layout": (0.0, 2.5)},
    "black_void_center": {"density": (1.5, 5.5), "layout": (0.0, 3.5)},
    "noise": {"density": (0.0, 1.0), "layout": (0.0, 1.0)},
    "gradient_bg": {"density": (0.0, 2.0), "layout": (0.0, 2.0)},
    "thin_gaps": {"density": (8.0, 10.0), "layout": (8.0, 10.0)},
    "scattered_sparse": {"density": (0.0, 3.0), "layout": (0.0, 2.5)},
}

GRID = {
    "ref_content": [0.80, 0.85, 0.90, 0.95],
    "ref_text": [0.30, 0.35, 0.40],
    "w_content": [0.6, 0.7, 0.8],
    "void_cap": [0.10, 0.12, 0.15, 0.20],
    "void_k": [0.3, 0.4, 0.5, 0.6],
    "layout_void_cap": [0.10, 0.12, 0.15, 0.20],
    "layout_void_k": [0.4, 0.5, 0.6, 0.7],
}


# --- parameterized scoring (must mirror quality_rubric._score_density/layout) ---

def score_density(sig: dict[str, Any], p: dict[str, float]) -> float | None:
    content = sig.get("content")
    if content is None:
        return None
    strip = sig.get("strip") or 0.0
    text = sig.get("text_cov")
    cn = min(1.0, content / p["ref_content"])
    tn = min(1.0, text / p["ref_text"]) if text is not None else None
    teff = max(tn, cn) if tn is not None else cn
    base = p["w_content"] * cn + (1 - p["w_content"]) * teff
    vp = 1.0 - p["void_k"] * min(1.0, strip / p["void_cap"])
    return round(max(0.0, min(10.0, 10.0 * base * vp)), 2)


def score_layout(sig: dict[str, Any], p: dict[str, float]) -> float | None:
    content = sig.get("content")
    if content is None:
        return None
    strip = sig.get("strip") or 0.0
    cn = min(1.0, content / p["ref_content"])
    vf = 1.0 - p["layout_void_k"] * min(1.0, strip / p["layout_void_cap"])
    return round(max(0.0, min(10.0, 10.0 * cn * vf)), 2)


# --- cache building ---------------------------------------------------------

def _signals(report: dict[str, Any]) -> dict[str, Any]:
    sp = report.get("spatial", {})
    bs = report.get("blank_strips", {})
    ocr = report.get("ocr", {})
    img = report.get("metric_bundles", {}).get("image_density", {})
    return {
        "content": sp.get("content_coverage"),
        "strip": bs.get("largest_blank_strip_ratio"),
        "strip_area": bs.get("blank_strip_area_ratio"),
        "text_cov": ocr.get("text_coverage_ratio") if ocr.get("available") else None,
        "nonwhite": img.get("nonwhite_pixel_ratio"),
    }


def build_cache(limit: int, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    used_skip: set[str] = set()
    real: list[dict[str, Any]] = []
    rows = []
    for fin in sorted(Path("out/runs").glob("*/final")):
        htmls = [h for h in fin.glob("*.html") if h.is_file()]
        if not any("paper-poster" in h.read_text(errors="replace")[:20000] for h in htmls):
            continue
        prev = fin / "preview.png"
        if not prev.exists():
            continue
        try:
            im = Image.open(prev).convert("L"); w, h = im.size; im.thumbnail((400, 400))
            nw = sum(im.histogram()[:245]) / max(1, sum(im.histogram()))
        except Exception:
            continue
        rows.append((round(nw, 3), fin.parent.name, prev))
    rows.sort()
    sample = rows if limit >= len(rows) else [rows[int(i * len(rows) / limit)] for i in range(limit)]
    print(f"caching {len(sample)} real posters of {len(rows)}", file=sys.stderr)
    for i, (_nw, rid, prev) in enumerate(sample):
        print(f"  [{i+1}/{len(sample)}] {rid}", file=sys.stderr)
        try:
            r = compute_deterministic_report(paper=None, candidate_artifact=prev, out_dir=out / "items" / rid)
            real.append({"name": rid, **_signals(r)})
        except Exception as exc:  # noqa: BLE001
            print(f"    err {exc}", file=sys.stderr)

    corner: list[dict[str, Any]] = []
    corner_dir = out.parent / "corner_cal" / "cases"
    if corner_dir.exists():
        for png in sorted(corner_dir.glob("*.png")):
            if png.stem.endswith("_occ") or png.stem not in CORNER_TARGETS:
                continue
            r = compute_deterministic_report(paper=None, candidate_artifact=png, out_dir=out / "corner" / png.stem)
            corner.append({"name": png.stem, **_signals(r)})

    (out / "cache.json").write_text(json.dumps({"real": real, "corner": corner}, indent=2), encoding="utf-8")
    print(f"wrote {out/'cache.json'} (real={len(real)}, corner={len(corner)})", file=sys.stderr)


# --- search -----------------------------------------------------------------

def _violation(score: float | None, rng: tuple[float, float] | None) -> float:
    if score is None or rng is None:
        return 0.0
    lo, hi = rng
    return max(0.0, lo - score) + max(0.0, score - hi)


def evaluate(p: dict[str, float], cache: dict[str, Any]) -> dict[str, Any]:
    corner_loss = 0.0
    corner_fail = 0
    for item in cache["corner"]:
        t = CORNER_TARGETS.get(item["name"], {})
        dv = _violation(score_density(item, p), t.get("density"))
        lv = _violation(score_layout(item, p), t.get("layout"))
        corner_loss += dv * dv + lv * lv
        if dv > 0.01 or lv > 0.01:
            corner_fail += 1
    dens = [s for s in (score_density(i, p) for i in cache["real"]) if s is not None]
    lays = [s for s in (score_layout(i, p) for i in cache["real"]) if s is not None]
    spread = (stats.pstdev(dens) if len(dens) > 1 else 0) + (stats.pstdev(lays) if len(lays) > 1 else 0)
    n = max(1, len(dens))
    sat = (sum(1 for s in dens if s >= 9.5) + sum(1 for s in dens if s <= 0.3)
           + sum(1 for s in lays if s >= 9.5) + sum(1 for s in lays if s <= 0.3)) / (2 * n)
    objective = corner_loss * 50.0 - spread + sat * 6.0
    return {"objective": objective, "corner_loss": round(corner_loss, 3), "corner_fail": corner_fail,
            "spread": round(spread, 3), "saturation": round(sat, 3),
            "density_mean": round(stats.mean(dens), 2) if dens else None,
            "density_std": round(stats.pstdev(dens), 2) if len(dens) > 1 else None}


def search(out: Path) -> None:
    cache = json.loads((out / "cache.json").read_text(encoding="utf-8"))
    keys = list(GRID)
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*[GRID[k] for k in keys])]
    print(f"searching {len(combos)} configs over {len(cache['real'])} real + {len(cache['corner'])} corner",
          file=sys.stderr)
    results = [{"params": p, **evaluate(p, cache)} for p in combos]
    feasible = [r for r in results if r["corner_fail"] == 0]
    pool = feasible if feasible else results
    pool.sort(key=lambda r: r["objective"])
    print(f"feasible (all corners satisfied): {len(feasible)}/{len(results)}", file=sys.stderr)

    # stability: among the top 30, prefer the one whose grid-neighbors keep low objective
    top = pool[:30]
    by_key = {tuple(sorted(r["params"].items())): r["objective"] for r in results}
    for r in top:
        neigh = []
        for k in keys:
            vals = GRID[k]
            idx = vals.index(r["params"][k])
            for j in (idx - 1, idx + 1):
                if 0 <= j < len(vals):
                    q = dict(r["params"]); q[k] = vals[j]
                    neigh.append(by_key[tuple(sorted(q.items()))])
        r["stability"] = round(stats.pstdev(neigh), 3) if len(neigh) > 1 else 0.0
    top.sort(key=lambda r: (r["objective"], r["stability"]))

    best = top[0]
    report = {
        "feasible_count": len(feasible), "total": len(results),
        "best": best,
        "top10": [{k: r[k] for k in ("params", "objective", "corner_loss", "spread", "saturation", "stability", "density_mean", "density_std")} for r in top[:10]],
    }
    (out / "calibration_result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("\n=== BEST (objective, then stability) ===")
    print(json.dumps(best["params"], indent=2))
    print(f"objective={best['objective']:.3f} corner_loss={best['corner_loss']} spread={best['spread']} "
          f"saturation={best['saturation']} stability={best.get('stability')} dens_mean={best['density_mean']} dens_std={best['density_std']}")
    print("\n=== top 10 ===")
    print(f"{'obj':>7s} {'corner':>6s} {'spread':>6s} {'sat':>5s} {'stab':>5s}  params")
    for r in top[:10]:
        print(f"{r['objective']:>7.2f} {r['corner_loss']:>6.2f} {r['spread']:>6.2f} {r['saturation']:>5.2f} "
              f"{r.get('stability',0):>5.2f}  {r['params']}")


def refresh_strips(out: Path) -> None:
    """Recompute only the blank-strip signal (no OCR) and update the cache in place."""
    from autodesign.evaluator.spatial import blank_strips

    cache = json.loads((out / "cache.json").read_text(encoding="utf-8"))
    for item in cache["real"]:
        p = Path("out/runs") / item["name"] / "final" / "preview.png"
        if not p.exists():
            continue
        bs = blank_strips(Image.open(p).convert("RGB"))
        item["strip"] = bs["largest_blank_strip_ratio"]
        item["strip_area"] = bs["blank_strip_area_ratio"]
    for item in cache["corner"]:
        p = out.parent / "corner_cal" / "cases" / f"{item['name']}.png"
        if not p.exists():
            continue
        bs = blank_strips(Image.open(p).convert("RGB"))
        item["strip"] = bs["largest_blank_strip_ratio"]
        item["strip_area"] = bs["blank_strip_area_ratio"]
    (out / "cache.json").write_text(json.dumps(cache, indent=2), encoding="utf-8")
    print(f"refreshed strips for {len(cache['real'])} real + {len(cache['corner'])} corner", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-cache", action="store_true")
    ap.add_argument("--refresh-strips", action="store_true")
    ap.add_argument("--search", action="store_true")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()
    out = args.out.expanduser().resolve()
    if args.build_cache:
        build_cache(args.limit, out)
    if args.refresh_strips:
        refresh_strips(out)
    if args.search:
        search(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
