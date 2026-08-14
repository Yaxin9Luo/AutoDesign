"""OCR bridge for image-mode poster evaluation.

The benchmark's default input is a rendered image (PNG/JPG/PDF), which has no DOM
or text layer. This module recovers text + layout signals from pixels so the
image-native metrics (numeric grounding, text coverage) can run on any format.

Engine policy: RapidOCR (PaddleOCR models on ONNX Runtime) is the primary engine
— CPU-only, no system binary, deterministic, returns boxes. It is an *optional*
dependency (``pip install 'autodesign[ocr]'``); when absent the bridge
degrades gracefully and text-based metrics are simply skipped.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image


@lru_cache(maxsize=1)
def _rapidocr() -> Any | None:
    """Return a cached RapidOCR engine, or None when the package is absent."""
    try:
        from rapidocr_onnxruntime import RapidOCR
    except Exception:  # noqa: BLE001 - optional dependency
        return None
    try:
        return RapidOCR()
    except Exception:  # noqa: BLE001 - model/init failure
        return None


def ocr_available() -> bool:
    return _rapidocr() is not None


def run_ocr(image_path: Path | str, *, include_segments: bool = False) -> dict[str, Any]:
    """Extract text + resolution-invariant layout signals from a rendered image.

    Returns a dict with ``available``. When available it includes the recognized
    ``text``, ``word_count``, and ``text_coverage_ratio`` (sum of text-box area /
    image area) — the last is resolution-invariant and is the signal the density
    metric relies on for format/resolution fairness. Set ``include_segments`` to
    also return per-box ``segments`` (``box``/``text``/``score``) for visualization.
    """
    path = Path(image_path)
    engine = _rapidocr()
    if engine is None:
        return {"available": False, "engine": None, "reason": "no_ocr_engine"}
    if not path.exists():
        return {"available": False, "engine": "rapidocr", "error": f"missing image: {path}"}
    try:
        with Image.open(path) as image:
            width, height = image.size
        result, _elapse = engine(str(path))
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "engine": "rapidocr", "error": f"{type(exc).__name__}: {exc}"}

    segments = result or []
    texts = [str(seg[1]) for seg in segments if len(seg) >= 2]
    text = "\n".join(texts)
    word_count = sum(len(t.split()) for t in texts)
    total_area = float(max(1, width * height))
    box_area = 0.0
    confidences: list[float] = []
    for seg in segments:
        box = seg[0] if seg else None
        if isinstance(box, (list, tuple)) and len(box) >= 3:
            xs = [float(pt[0]) for pt in box]
            ys = [float(pt[1]) for pt in box]
            box_area += max(0.0, max(xs) - min(xs)) * max(0.0, max(ys) - min(ys))
        if len(seg) >= 3:
            try:
                confidences.append(float(seg[2]))
            except (TypeError, ValueError):
                pass
    payload: dict[str, Any] = {
        "available": True,
        "engine": "rapidocr",
        "image_size": [width, height],
        "segment_count": len(segments),
        "word_count": word_count,
        "text_coverage_ratio": round(box_area / total_area, 4),
        "mean_confidence": round(sum(confidences) / len(confidences), 3) if confidences else None,
        "text": text,
    }
    if include_segments:
        payload["segments"] = [
            {
                "box": [[float(p[0]), float(p[1])] for p in seg[0]] if seg and isinstance(seg[0], (list, tuple)) else [],
                "text": str(seg[1]) if len(seg) >= 2 else "",
                "score": float(seg[2]) if len(seg) >= 3 else None,
            }
            for seg in segments
        ]
    return payload
