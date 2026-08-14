"""Spike / debug tool: extract figures from a PDF using pymupdf ONLY.

Thin wrapper around `autodesign.util.pdf` — same code paths the
production ingest uses, so what you see here is what ingest gets
before the VLM does caption matching. No LLM calls.

Run:
    uv run python scripts/spike_pdf_figures.py \\
        ./data/example/paper.pdf out/pdf-figure-spike

Outputs manifest.json + PNG crops. For visual inspection.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

# Allow running the script directly from repo root without -m:
# `uv run python scripts/spike_pdf_figures.py ...`
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import fitz  # pymupdf

from autodesign.util.pdf import (
    dedup_raster_vector,
    detect_scanned_pdf,
    extract_embedded_rasters,
    extract_vector_clusters,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path, help="PDF to inspect.")
    parser.add_argument("out_dir", type=Path, help="Directory for generated crops and manifest.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    pdf_path = args.pdf.expanduser().resolve()
    out_root = args.out_dir.expanduser().resolve()

    if not pdf_path.exists():
        print(f"PDF not found: {pdf_path}")
        return 2

    if out_root.exists():
        shutil.rmtree(out_root)
    out_root.mkdir(parents=True)

    doc = fitz.open(pdf_path)
    try:
        print(f"pdf: {pdf_path}  pages={len(doc)}")
        if detect_scanned_pdf(doc):
            print("WARN: PDF appears to be scanned (no text, no vector drawings).")

        rasters = extract_embedded_rasters(doc, out_root / "raster")
        print(f"raster: {len(rasters)} embedded image(s) at native resolution")

        vectors = extract_vector_clusters(doc, out_root / "vector", dpi=300)
        print(f"vector: {len(vectors)} drawing cluster(s) at 300 dpi")
    finally:
        doc.close()

    candidates = dedup_raster_vector(rasters + vectors)
    print(f"dedup: {len(candidates)} candidate(s) after raster/vector merge")

    manifest = {
        "pdf": str(pdf_path),
        "out_root": str(out_root),
        "candidates": [
            {
                "strategy": c.strategy,
                "page": c.page,
                "xref": c.xref,
                "bbox_pt": list(c.bbox_pt) if c.bbox_pt else None,
                "path": str(c.path),
                "width_px": c.width_px,
                "height_px": c.height_px,
            }
            for c in candidates
        ],
    }
    (out_root / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"manifest: {out_root / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
