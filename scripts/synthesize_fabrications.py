#!/usr/bin/env python3
"""Stress-test fabrication RECALL: inject numbers that are absent from the source
paper into real posters and check the detector flags them.

This is the complement of `audit_numeric_fabrications.py` (which checks precision —
are the flags real?). Here we check recall — does the scoping/exclusion work we did
for precision open BLIND SPOTS where a genuine fabrication slips through?

For each poster we append synthetic OCR segments carrying fabricated values (chosen
to be absent from the reference) in several forms and placements, run the metric, and
record which were caught. Placements are chosen to probe each exclusion rule:

  prose_pct / prose_mult / prose_mag  — fabricated number embedded in an authored
      sentence (the realistic threat). MUST be caught.
  headline_prominent                  — a large bare callout ("88.2%"). MUST be caught
      (the prominence carve-out keeps it).
  label_bare_small                    — a small bare label. Expected MISS (bare-label
      exclusion) — a known blind spot to quantify.
  in_figure                           — a number whose box sits inside a figure region.
      Expected MISS (figure exclusion) — a known blind spot to quantify.

Usage:
    uv run python scripts/synthesize_fabrications.py --posters-dir DesignAnything_Poster
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from PIL import Image

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from autodesign.evaluator.metrics import (  # noqa: E402
    _median_segment_height,
    _parse_numeric_tokens,
    _segment_in_any_rect,
    numeric_token_metrics,
)
from autodesign.evaluator.ocr import run_ocr  # noqa: E402
from autodesign.evaluator.schema import ArtifactSnapshot  # noqa: E402
from autodesign.evaluator.spatial import basic_layout_integrity  # noqa: E402
from scripts.visualize_numeric_grounding import _discover_poster_dirs  # noqa: E402


def _box(x0: float, y0: float, w: float, h: float) -> list[list[float]]:
    return [[x0, y0], [x0 + w, y0], [x0 + w, y0 + h], [x0, y0 + h]]


def _absent_value(target: float, reference_values: list[float]) -> float:
    """Nudge `target` until it is not within 1% of any reference number (so the
    injected value is a genuine fabrication, not a coincidental real one)."""
    v = target
    for _ in range(200):
        if all(abs(v - r) > max(0.01, abs(r) * 0.01) for r in reference_values):
            return round(v, 2)
        v += 0.07
    return round(v, 2)


def _box_clear_of_figures(x0: float, y0: float, w: float, h: float, visual_rects: list[dict]) -> bool:
    return not _segment_in_any_rect(_box(x0, y0, w, h), visual_rects)


def _free_anchor(segments: list[dict], visual_rects: list[dict], width: int, height: int) -> tuple[float, float]:
    """A body location with clearance from figure regions: prefer a real text segment
    in the upper-middle of the poster (titles/intro text, away from bottom figures),
    so injected boxes — even the tall headline — stay clear of figures."""
    cands = []
    for seg in segments:
        box = seg.get("box")
        if not box or _segment_in_any_rect(box, visual_rects):
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        x0, y0 = float(min(xs)), float(min(ys))
        # require ~4 line-heights of clearance below for the tall headline box
        clear = _box_clear_of_figures(x0, y0, width * 0.45, max(40.0, height * 0.12), visual_rects)
        cands.append((clear, 0.15 < y0 / height < 0.55, y0, x0))
    if cands:
        # prefer boxes that are clear AND in the upper-middle band
        cands.sort(key=lambda c: (not c[0], not c[1], c[2]))
        return cands[0][3], cands[0][2]
    return width * 0.1, height * 0.4


def build_injections(segments: list[dict], visual_rects: list[dict], width: int, height: int,
                     reference_values: list[float]) -> list[dict]:
    med = _median_segment_height(segments) or max(12.0, height / 60.0)
    ax, ay = _free_anchor(segments, visual_rects, width, height)
    pct = _absent_value(96.83, reference_values)
    mult = _absent_value(137.6, reference_values)
    mag = _absent_value(73.4, reference_values)
    head = _absent_value(88.21, reference_values)
    small = _absent_value(64.77, reference_values)
    fig = _absent_value(51.93, reference_values)
    # Each injection is tested independently (one at a time), so they all sit at the
    # same figure-clear anchor; only their text/box-height differ. in_figure is the
    # sole one deliberately placed inside a figure region.
    inj = [
        {"id": "prose_pct", "value": pct, "expect": "catch",
         "seg": {"text": f"Our method reaches {pct}% accuracy on the held-out benchmark.",
                 "box": _box(ax, ay, width * 0.45, med)}},
        {"id": "prose_mult", "value": mult, "expect": "catch",
         "seg": {"text": f"It delivers a {mult}x speedup over the strongest baseline.",
                 "box": _box(ax, ay, width * 0.45, med)}},
        {"id": "prose_mag", "value": mag, "expect": "catch",
         "seg": {"text": f"The model is pretrained on {mag}M annotated examples.",
                 "box": _box(ax, ay, width * 0.45, med)}},
        {"id": "headline_prominent", "value": head, "expect": "catch",
         "seg": {"text": f"{head}%", "box": _box(ax, ay, med * 4, med * 3)}},
        {"id": "label_bare_small", "value": small, "expect": "blindspot",
         "seg": {"text": f"{small}x", "box": _box(ax, ay, med * 2, med)}},
    ]
    if visual_rects:
        r = visual_rects[0]
        fx = float(r.get("x0", 0)) + 5
        fy = float(r.get("y0", 0)) + 5
        inj.append({"id": "in_figure", "value": fig, "expect": "blindspot",
                    "seg": {"text": f"{fig}", "box": _box(fx, fy, med * 2, med)}})
    return inj


def run_poster(preview: Path, paper_text: str) -> list[dict]:
    img = Image.open(preview).convert("RGB")
    ocr = run_ocr(preview, include_segments=True)
    segs = list(ocr.get("segments") or [])
    vr = basic_layout_integrity(img.copy(), segments=segs).get("visual_region_rects") or []
    ref_values = [t["value"] for t in _parse_numeric_tokens(paper_text)[0]]
    injections = build_injections(segs, vr, img.width, img.height, ref_values)
    snap = ArtifactSnapshot(artifact_path="<t>", artifact_kind="image", text=ocr.get("text", ""))
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
        tf.write(paper_text)
        ref_path = Path(tf.name)

    def flagged(extra: list[dict]) -> int:
        """Count salient numbers surfaced for review (fabricated OR near-miss) — i.e.
        NOT silently grounded. A fabrication caught either way is caught."""
        bundle, _f = numeric_token_metrics(snap, ref_path, segments=segs + extra, visual_rects=vr)
        m = bundle.metrics
        return int(m.get("salient_fabricated", 0) or 0) + int(m.get("salient_near_miss", 0) or 0)

    try:
        base = flagged([])
        results = []
        for inj in injections:
            # caught = adding this one injected number raises the flagged (fab+near) count,
            # i.e. it was surfaced rather than excluded or silently grounded.
            caught = flagged([inj["seg"]]) > base
            results.append({"id": inj["id"], "value": inj["value"], "expect": inj["expect"], "caught": caught})
    finally:
        ref_path.unlink(missing_ok=True)
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--posters-dir", type=Path, default=_REPO / "DesignAnything_Poster")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)
    root = args.posters_dir if args.posters_dir.is_absolute() else _REPO / args.posters_dir
    found = _discover_poster_dirs(root)
    if args.limit:
        found = found[:args.limit]
    print(f"FABRICATION RECALL: inject absent numbers into {len(found)} posters\n", flush=True)
    by_kind: dict[str, list[bool]] = {}
    for name, preview, paper_text in found:
        results = run_poster(preview, paper_text)
        for r in results:
            by_kind.setdefault(r["id"], []).append(r["caught"])
        misses = [r["id"] for r in results if r["expect"] == "catch" and not r["caught"]]
        flag = "  <-- MISSED A REAL FABRICATION" if misses else ""
        print(f"  {name.split('/')[-1][:44]:46s} "
              f"catch={sum(1 for r in results if r['expect']=='catch' and r['caught'])}/"
              f"{sum(1 for r in results if r['expect']=='catch')}{flag}", flush=True)
    print("\n=== recall by injection type ===", flush=True)
    for kind, hits in by_kind.items():
        rate = sum(hits) / len(hits) if hits else 0.0
        tag = "(MUST catch)" if kind in ("prose_pct", "prose_mult", "prose_mag", "headline_prominent") else "(known blind spot)"
        print(f"  {kind:20s} {sum(hits):>3}/{len(hits):<3} = {rate:.2f}  {tag}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
