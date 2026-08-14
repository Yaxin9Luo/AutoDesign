#!/usr/bin/env python3
"""Audit every 'fabricated' numeric flag the grounding metric raises, with full
context, so each can be judged by hand: a TRUE fabrication vs a detection error
(OCR misread / identifier digits / merged values / reference gap).

For each flag it prints the raw token, its canonical value+kind, the OCR segment it
came from, the nearest paper value, and whether the raw digit string appears anywhere
in the reference text (a cheap "is it really absent?" probe).

Usage:
    uv run python scripts/audit_numeric_fabrications.py --posters-dir DesignAnything_Poster
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from PIL import Image

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from autodesign.evaluator.metrics import (  # noqa: E402
    _match_number,
    _median_segment_height,
    _numbers_are_figure_reproductions,
    _parse_numeric_tokens,
    _is_salient,
)
from autodesign.evaluator.ocr import run_ocr  # noqa: E402
from autodesign.evaluator.spatial import basic_layout_integrity  # noqa: E402
from scripts.visualize_numeric_grounding import _discover_poster_dirs  # noqa: E402


def _digits(s: str) -> str:
    return re.sub(r"[^\d.]", "", s)


def audit_poster(preview: Path, paper_text: str) -> list[dict]:
    img = Image.open(preview).convert("RGB")
    ocr = run_ocr(preview, include_segments=True)
    segs = ocr.get("segments") or []
    vr = basic_layout_integrity(img.copy(), segments=segs).get("visual_region_rects") or []
    med = _median_segment_height(segs)
    ptoks, _ = _parse_numeric_tokens(paper_text)
    pvals = [p["value"] for p in ptoks]
    paper_digits = _digits(paper_text)
    flags: list[dict] = []
    for seg in segs:
        text = str(seg.get("text") or "")
        box = seg.get("box")
        toks, _g = _parse_numeric_tokens(text)
        if not toks or not box:
            continue
        excluded, _reason = _numbers_are_figure_reproductions(text, box, vr, med)
        if excluded:
            continue
        for tok in toks:
            if not _is_salient(tok):
                continue
            status = _match_number(tok, ptoks) if ptoks else "ungrounded"
            if status != "ungrounded":
                continue
            d = _digits(tok["raw"])
            flags.append({
                "raw": tok["raw"],
                "value": tok["value"],
                "kind": tok["kind"],
                "segment": text[:60],
                "nearest_paper": min(pvals, key=lambda v: abs(v - tok["value"])) if pvals else None,
                "raw_digits_in_paper": (d in paper_digits) if len(d) >= 2 else False,
            })
    return flags


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--posters-dir", type=Path, default=_REPO / "DesignAnything_Poster")
    args = ap.parse_args(argv)
    root = args.posters_dir if args.posters_dir.is_absolute() else _REPO / args.posters_dir
    found = _discover_poster_dirs(root)
    print(f"AUDIT of fabricated flags across {len(found)} posters\n", flush=True)
    total = 0
    for name, preview, paper_text in found:
        flags = audit_poster(preview, paper_text)
        if not flags:
            continue
        print(f"### {name}", flush=True)
        for f in flags:
            total += 1
            print(f"   {f['raw']:>10s} ({f['kind']:9s}) near={f['nearest_paper']}"
                  f"  digits_in_paper={f['raw_digits_in_paper']}  seg='{f['segment']}'", flush=True)
    print(f"\n=== {total} fabricated flags total ===", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
