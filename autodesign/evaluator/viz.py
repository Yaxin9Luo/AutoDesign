"""Shared drawing primitives for evaluator visualizations.

These render, on top of the real poster pixels, what the deterministic detectors
"see": the longest blank vertical band + per-row ink, OCR text boxes, and which
numeric tokens are grounded in the paper. Used by the step walkthrough and the
multi-poster gallery so the drawing logic lives in one place.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from .metrics import _extract_paper_text, _numeric_tokens

_DIGIT_RE = re.compile(r"\d")
BLANK_ROW_THRESHOLD = 0.018  # matches metrics._longest_blank_run_ratio default


def density_overlay(base: Image.Image) -> tuple[Image.Image, dict[str, Any]]:
    """Poster + longest blank vertical band shaded + per-row ink bars in a gutter."""
    w, h = base.size
    gutter = max(140, w // 8)
    canvas = Image.new("RGB", (w + gutter, h), "white")
    canvas.paste(base, (0, 0))
    draw = ImageDraw.Draw(canvas, "RGBA")

    gray = base.convert("L")
    px = gray.load()
    row_ratio = [sum(1 for x in range(w) if px[x, y] < 245) / max(1, w) for y in range(h)]

    best_len = best_end = cur = 0
    for y in range(h):
        if row_ratio[y] <= BLANK_ROW_THRESHOLD:
            cur += 1
            if cur > best_len:
                best_len, best_end = cur, y
        else:
            cur = 0
    band = None
    if best_len > 0:
        y0, y1 = best_end - best_len + 1, best_end
        band = (y0, y1)
        draw.rectangle([0, y0, w, y1], fill=(255, 40, 40, 70))
        draw.line([0, y0, w, y0], fill=(220, 0, 0, 255), width=3)
        draw.line([0, y1, w, y1], fill=(220, 0, 0, 255), width=3)

    for y in range(0, h, 2):
        bar = int(row_ratio[y] * (gutter - 12))
        draw.line([w + 6, y, w + 6 + bar, y], fill=(40, 90, 200, 200), width=2)
    draw.line([w + 6, 0, w + 6, h], fill=(180, 180, 180, 255), width=1)
    return canvas, {"blank_band_rows": band, "blank_run_ratio": round(best_len / max(1, h), 4)}


def ocr_overlay(base: Image.Image, segments: list[dict[str, Any]]) -> Image.Image:
    canvas = base.copy()
    draw = ImageDraw.Draw(canvas, "RGBA")
    for seg in segments:
        box = seg.get("box") or []
        if len(box) >= 4:
            draw.polygon([(p[0], p[1]) for p in box], outline=(0, 160, 0, 255), fill=(0, 200, 0, 36))
    return canvas


def numeric_overlay(
    base: Image.Image,
    segments: list[dict[str, Any]],
    paper_tokens: set[str] | None,
) -> tuple[Image.Image, int, int]:
    canvas = base.copy()
    draw = ImageDraw.Draw(canvas, "RGBA")
    matched = missing = 0
    for seg in segments:
        text = str(seg.get("text") or "")
        box = seg.get("box") or []
        if not _DIGIT_RE.search(text) or len(box) < 4:
            continue
        toks = _numeric_tokens(text)
        if not toks:
            continue
        if paper_tokens is None:
            color, fill = (40, 90, 200, 255), (40, 90, 200, 36)
        elif any(t in paper_tokens for t in toks):
            matched += 1
            color, fill = (30, 110, 230, 255), (30, 110, 230, 40)
        else:
            missing += 1
            color, fill = (220, 30, 30, 255), (220, 30, 30, 45)
        draw.polygon([(p[0], p[1]) for p in box], outline=color, fill=fill)
    return canvas, matched, missing


def paper_numeric_tokens(paper: Path | None) -> set[str] | None:
    if not paper or not Path(paper).exists():
        return None
    try:
        return set(_numeric_tokens(_extract_paper_text(Path(paper))))
    except Exception:  # noqa: BLE001
        return None


def image_b64(path: Path) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")
