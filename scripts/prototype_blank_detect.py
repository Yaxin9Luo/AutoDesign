#!/usr/bin/env python3
"""Prototype: visualize image-native content-occupancy / whitespace detection."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PIL import Image

from autodesign.evaluator.ocr import run_ocr
from autodesign.evaluator.spatial import content_occupancy, occupancy_overlay


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--candidate", action="append", required=True, help="name=/path/to/preview.png")
    p.add_argument("--out-dir", required=True, type=Path)
    p.add_argument("--std-threshold", type=float, default=12.0)
    p.add_argument("--cell-px", type=int, default=64)
    args = p.parse_args()
    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"{'poster':8s} {'content_cov':>11s} {'empty_frac':>10s} {'largest_empty_rect':>18s}")
    for item in args.candidate:
        name, raw = item.split("=", 1)
        path = Path(raw).expanduser()
        base = Image.open(path).convert("RGB")
        segs = (run_ocr(path, include_segments=True) or {}).get("segments") or []
        occ = content_occupancy(base, segments=segs, cell_px=args.cell_px, std_threshold=args.std_threshold)
        occupancy_overlay(base, occ).save(out_dir / f"{name}_occ.png")
        print(f"{name:8s} {occ['content_coverage']:>11.3f} {occ['empty_cell_fraction']:>10.3f} "
              f"{occ['largest_empty_rect_cell_ratio']:>18.3f}")
    print(f"overlays in {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
