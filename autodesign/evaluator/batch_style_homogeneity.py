"""Batch-level style homogeneity adjustment for poster benchmarks.

This module is intentionally runner-neutral: callers pass poster artifacts,
an optional LLMBackend-compatible judge, and an optional batch cache path. It
does not know system names and never uses folder names in the judge image or
prompt.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image, ImageDraw, UnidentifiedImageError

from ..eval_protocol import EVAL_PROTOCOL, fingerprint_python_symbols
from ..util.io import atomic_write_json
from ..util.pipeline_cache import stable_cache_key


LEGACY_BATCH_STYLE_MODULE_VERSION = "0.1.0"
LEGACY_BATCH_STYLE_RUBRIC_VERSION = "batch-style-homogeneity-v1"
MIN_BATCH_SIZE = 20

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
_HTML_SUFFIXES = {".html", ".htm"}
_CONTACT_THUMB_SIZE = (320, 180)
_CONTACT_GAP = 14
_CONTACT_LABEL_H = 26
_CONTACT_BG = (248, 249, 251)
_CONTACT_FRAME = (214, 220, 228)
_CONTACT_TEXT = (31, 41, 55)


def evaluate_batch_style_homogeneity(
    artifacts: Sequence[Path | str],
    *,
    out_dir: Path | str,
    judge_model: str,
    judge_backend: Any | None = None,
    cache_path: Path | str | None = None,
    force_judge: bool = False,
) -> dict[str, Any]:
    """Evaluate batch-level style adaptability and return an adjustment record.

    ``judge_backend`` may be any provider-specific implementation of the local
    ``LLMBackend`` protocol. When neither a valid cache nor a judge is available,
    the module returns ``status=skipped`` with ``adjustment_points=0.0``.
    """
    artifact_paths = [Path(p) for p in artifacts]
    if len(artifact_paths) < MIN_BATCH_SIZE:
        return _skipped(
            "Batch style homogeneity requires a minimum of 20 poster artifacts.",
            artifact_count=len(artifact_paths),
            cache_status="not_checked",
        )

    try:
        cache_key = build_batch_style_cache_key(
            artifact_paths,
            judge_model=judge_model,
        )
        legacy_cache_key = build_legacy_batch_style_cache_key(
            artifact_paths,
            judge_model=judge_model,
        )
    except OSError as exc:
        return _skipped(
            f"Unable to hash poster artifacts for batch style cache: {type(exc).__name__}: {exc}",
            artifact_count=len(artifact_paths),
            cache_status="not_checked",
        )

    cache_status = "disabled"
    cache_file = Path(cache_path) if cache_path is not None else None
    if cache_file is not None and not force_judge:
        cached, cache_status = _read_valid_cache(
            cache_file,
            cache_key=cache_key,
            legacy_cache_key=legacy_cache_key,
            judge_model=judge_model,
        )
        if cached is not None:
            cached = dict(cached)
            cached["cache_status"] = cache_status
            cached.setdefault("source", "cache")
            if cache_status == "legacy_hit":
                cached["legacy_batch_style_rubric_version"] = cached.pop(
                    "rubric_version", None
                )
                cached["legacy_batch_style_module_version"] = cached.pop(
                    "module_version", None
                )
                cached["eval_protocol"] = EVAL_PROTOCOL
                cached["batch_style_fingerprint"] = BATCH_STYLE_FINGERPRINT
            return cached

    if judge_backend is None:
        return _skipped(
            "No valid batch style cache or judge backend is available; no adjustment applied.",
            artifact_count=len(artifact_paths),
            cache_key=cache_key,
            cache_status=cache_status,
        )

    try:
        records = _prepare_anonymous_records(artifact_paths)
    except OSError as exc:
        return _skipped(
            f"Unable to read poster artifacts for anonymous batch style review: {type(exc).__name__}: {exc}",
            artifact_count=len(artifact_paths),
            cache_key=cache_key,
            cache_status=cache_status,
        )
    if len(records) < MIN_BATCH_SIZE:
        return _skipped(
            "Fewer than 20 readable poster previews are available for batch style review.",
            artifact_count=len(artifact_paths),
            readable_artifact_count=len(records),
            cache_key=cache_key,
            cache_status=cache_status,
        )

    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    contact_sheet = output_dir / "batch_style_contact_sheet.png"
    contact_meta = _write_contact_sheet(records, contact_sheet)
    prompt = build_batch_style_prompt(records)

    try:
        raw_response = _call_batch_judge(
            judge_backend,
            contact_sheet=contact_sheet,
            prompt=prompt,
        )
    except Exception as exc:  # noqa: BLE001
        return _degraded(
            f"Batch style judge failed: {type(exc).__name__}: {exc}",
            artifact_count=len(artifact_paths),
            cache_key=cache_key,
            cache_status=cache_status,
            contact_meta=contact_meta,
            judge_status="error",
        )

    parsed = parse_batch_style_judge_response(raw_response)
    if parsed.get("status") != "ok":
        return _degraded(
            str(parsed.get("error") or "Batch style judge returned an invalid response."),
            artifact_count=len(artifact_paths),
            cache_key=cache_key,
            cache_status=cache_status,
            contact_meta=contact_meta,
            judge_status=str(parsed.get("status") or "parse_error"),
            raw_excerpt=str(parsed.get("raw_excerpt") or "")[:1500],
        )

    result = {
        "status": "ok",
        "source": "judge",
        "eval_protocol": EVAL_PROTOCOL,
        "batch_style_fingerprint": BATCH_STYLE_FINGERPRINT,
        "judge_model": judge_model,
        "cache_key": cache_key,
        "cache_status": cache_status,
        "artifact_count": len(artifact_paths),
        "reviewed_artifact_count": len(records),
        "artifact_hashes": [record["content_hash"] for record in records],
        "contact_sheet": contact_meta,
        "layout_signatures": [
            {"id": record["id"], "signature": record["layout_signature"]}
            for record in records
        ],
        "style_adaptability_score_0_10": parsed["style_adaptability_score_0_10"],
        "adjustment_points": parsed["adjustment_points"],
        "explanation": parsed.get("rationale") or _score_explanation(
            parsed["style_adaptability_score_0_10"],
            parsed["adjustment_points"],
        ),
        "evidence": parsed.get("evidence", []),
        "judge_status": "ok",
    }
    for key in (
        "skeleton_reuse",
        "adaptation_to_visual_needs",
        "text_accent_only_variation",
        "judge_confidence",
    ):
        if key in parsed:
            result[key] = parsed[key]

    if cache_file is not None:
        atomic_write_json(cache_file, result)
    return result


def build_batch_style_cache_key(
    artifacts: Sequence[Path | str],
    *,
    judge_model: str,
    batch_style_fingerprint: str | None = None,
) -> str:
    """Return an order-independent cache key for the batch style judge."""
    artifact_hashes = sorted(_artifact_content_hash(Path(path)) for path in artifacts)
    return stable_cache_key(
        {
            "artifact_hashes": artifact_hashes,
            "judge_model": str(judge_model),
            "eval_protocol": EVAL_PROTOCOL,
            "batch_style_fingerprint": batch_style_fingerprint or BATCH_STYLE_FINGERPRINT,
        }
    )


def build_legacy_batch_style_cache_key(
    artifacts: Sequence[Path | str],
    *,
    judge_model: str,
) -> str:
    """Return the previous manual-version key for read-only cache migration."""

    artifact_hashes = sorted(_artifact_content_hash(Path(path)) for path in artifacts)
    return stable_cache_key({
        "artifact_hashes": artifact_hashes,
        "judge_model": str(judge_model),
        "rubric_version": LEGACY_BATCH_STYLE_RUBRIC_VERSION,
        "module_version": LEGACY_BATCH_STYLE_MODULE_VERSION,
    })


def compute_layout_signature(artifact: Path | str) -> dict[str, Any]:
    """Return an anonymous layout signature for explanation, not scoring.

    Text nodes, image ``src`` values, file names, and method/system identifiers
    are deliberately omitted. The signature keeps only coarse layout geometry,
    occupancy, and palette structure.
    """
    path = Path(artifact)
    if path.suffix.lower() in _HTML_SUFFIXES:
        return _html_layout_signature(path)
    image = _resolve_preview_image(path)
    if image is None:
        return _empty_layout_signature("unavailable")
    return _image_layout_signature(image)


def build_batch_style_prompt(records: Sequence[Mapping[str, Any]]) -> str:
    """Build the text prompt paired with the anonymous contact sheet."""
    signature_packet = [
        {
            "id": record["id"],
            "layout_signature": record["layout_signature"],
        }
        for record in records
    ]
    return (
        "Judge batch-level poster style adaptability from the attached anonymous "
        "contact sheet. Posters are labeled only P001, P002, ...; do not infer, "
        "ask for, or mention systems, methods, folders, file names, or run names.\n\n"
        "Task: assign style_adaptability_score_0_10 for the whole batch.\n"
        "- 9-10: strong adaptation across papers; recurring craft is outweighed by "
        "paper-specific visual structure and layout decisions.\n"
        "- 7-8: acceptable family resemblance, with meaningful variation for paper "
        "visual needs.\n"
        "- 5-6: noticeable skeleton reuse; some adaptation but many posters share "
        "the same grid, header, panel proportions, or visual placement.\n"
        "- 3-4: heavy skeleton reuse with mostly text/accent/color swaps.\n"
        "- 0-2: near-template cloning across the batch.\n\n"
        "Judge these factors explicitly: skeleton reuse, adaptation to paper visual "
        "needs, and text/accent-only variation. Do not score individual paper "
        "correctness, method identity, or benchmark system identity.\n\n"
        "Deterministic layout signatures below are explanation aids only. They "
        "suppress poster text and source-image details and retain only coarse "
        "header, columns, section bands, panel geometry, occupancy, and palette "
        "structure:\n"
        "```json\n"
        f"{json.dumps(signature_packet, ensure_ascii=False, sort_keys=True)}\n"
        "```\n\n"
        "Return JSON only with keys: style_adaptability_score_0_10, rationale, "
        "evidence, skeleton_reuse, adaptation_to_visual_needs, "
        "text_accent_only_variation, judge_confidence."
    )


def parse_batch_style_judge_response(response: str | Mapping[str, Any]) -> dict[str, Any]:
    """Parse the batch style judge response and attach the adjustment."""
    if isinstance(response, Mapping):
        data = dict(response)
    else:
        parsed = _parse_json_object(str(response))
        if parsed is None:
            return {
                "status": "parse_error",
                "error": "Batch style judge response did not contain a JSON object.",
                "raw_excerpt": str(response)[:1500],
                "adjustment_points": 0.0,
            }
        data = parsed

    raw_score = data.get("style_adaptability_score_0_10")
    try:
        score = float(raw_score)
    except (TypeError, ValueError):
        return {
            "status": "invalid_score",
            "error": "Missing numeric style_adaptability_score_0_10.",
            "raw_excerpt": json.dumps(data, ensure_ascii=False)[:1500],
            "adjustment_points": 0.0,
        }
    if not math.isfinite(score) or score < 0.0 or score > 10.0:
        return {
            "status": "invalid_score",
            "error": "style_adaptability_score_0_10 must be in [0, 10].",
            "raw_excerpt": json.dumps(data, ensure_ascii=False)[:1500],
            "adjustment_points": 0.0,
        }

    data["status"] = "ok"
    data["style_adaptability_score_0_10"] = score
    data["adjustment_points"] = map_style_score_to_adjustment(score)
    return data


def map_style_score_to_adjustment(score_0_10: float) -> float:
    """Map style adaptability score to the benchmark adjustment."""
    score = float(score_0_10)
    if score >= 7.0:
        return 0.0
    if score >= 5.0:
        return -0.5
    if score >= 3.0:
        return -1.0
    return -1.5


def _prepare_anonymous_records(artifact_paths: Sequence[Path]) -> list[dict[str, Any]]:
    hashed: list[tuple[str, Path]] = []
    for path in artifact_paths:
        hashed.append((_artifact_content_hash(path), path))
    hashed.sort(key=lambda item: item[0])

    records: list[dict[str, Any]] = []
    for index, (content_hash, path) in enumerate(hashed, start=1):
        preview = _resolve_preview_image(path)
        if preview is None:
            continue
        records.append(
            {
                "id": f"P{index:03d}",
                "content_hash": content_hash,
                "path": path,
                "preview": preview,
                "layout_signature": compute_layout_signature(path),
            }
        )
    return records


def _call_batch_judge(
    judge_backend: Any,
    *,
    contact_sheet: Path,
    prompt: str,
) -> str:
    message = judge_backend.vision_user_message(
        image_b64=base64.b64encode(contact_sheet.read_bytes()).decode("ascii"),
        media_type="image/png",
        text=prompt,
    )
    response = judge_backend.create_turn(
        system=_BATCH_STYLE_SYSTEM,
        messages=[message],
        tools=[],
        thinking_budget=0,
        max_tokens=4096,
    )
    return str(getattr(response, "text", "") or "")


_BATCH_STYLE_SYSTEM = (
    "You are a strict batch-level visual reviewer for academic poster benchmarks. "
    "Judge only anonymous rendered poster thumbnails and the provided anonymous "
    "layout signatures. Do not infer systems, methods, folders, prompts, or hidden "
    "metadata. Return JSON only."
)


def _write_contact_sheet(records: Sequence[Mapping[str, Any]], out_path: Path) -> dict[str, Any]:
    columns = min(10, max(1, math.ceil(math.sqrt(len(records)))))
    rows = math.ceil(len(records) / columns)
    cell_w = _CONTACT_THUMB_SIZE[0]
    cell_h = _CONTACT_THUMB_SIZE[1] + _CONTACT_LABEL_H
    width = _CONTACT_GAP + columns * cell_w + (columns - 1) * _CONTACT_GAP + _CONTACT_GAP
    height = _CONTACT_GAP + rows * cell_h + (rows - 1) * _CONTACT_GAP + _CONTACT_GAP
    sheet = Image.new("RGB", (width, height), _CONTACT_BG)
    draw = ImageDraw.Draw(sheet)

    for index, record in enumerate(records):
        col = index % columns
        row = index // columns
        x = _CONTACT_GAP + col * (cell_w + _CONTACT_GAP)
        y = _CONTACT_GAP + row * (cell_h + _CONTACT_GAP)
        with Image.open(Path(record["preview"])) as image:
            thumb = _fit_thumbnail(image.convert("RGB"), _CONTACT_THUMB_SIZE)
        sheet.paste(thumb, (x, y))
        draw.rectangle(
            [x, y, x + _CONTACT_THUMB_SIZE[0] - 1, y + _CONTACT_THUMB_SIZE[1] - 1],
            outline=_CONTACT_FRAME,
            width=1,
        )
        draw.text((x + 8, y + _CONTACT_THUMB_SIZE[1] + 6), str(record["id"]), fill=_CONTACT_TEXT)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path, format="PNG")
    return {
        "path": str(out_path),
        "poster_count": len(records),
        "columns": columns,
        "rows": rows,
        "labels": [record["id"] for record in records],
    }


def _fit_thumbnail(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
    image.thumbnail(size, resample)
    canvas = Image.new("RGB", size, "white")
    x = (size[0] - image.width) // 2
    y = (size[1] - image.height) // 2
    canvas.paste(image, (x, y))
    return canvas


def _read_valid_cache(
    cache_path: Path,
    *,
    cache_key: str,
    legacy_cache_key: str,
    judge_model: str,
) -> tuple[dict[str, Any] | None, str]:
    if not cache_path.exists():
        return None, "miss"
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return None, "invalid"
    if not isinstance(payload, dict):
        return None, "invalid"
    if payload.get("judge_model") != judge_model:
        return None, "invalid"
    current_cache = (
        payload.get("cache_key") == cache_key
        and payload.get("eval_protocol") == EVAL_PROTOCOL
        and payload.get("batch_style_fingerprint") == BATCH_STYLE_FINGERPRINT
    )
    legacy_cache = (
        payload.get("cache_key") == legacy_cache_key
        and payload.get("rubric_version") == LEGACY_BATCH_STYLE_RUBRIC_VERSION
        and payload.get("module_version") == LEGACY_BATCH_STYLE_MODULE_VERSION
    )
    if not current_cache and not legacy_cache:
        return None, "invalid"
    if payload.get("status") != "ok":
        return None, "invalid"
    parsed = parse_batch_style_judge_response(payload)
    if parsed.get("status") != "ok":
        return None, "invalid"
    try:
        cached_adjustment = float(
            payload.get("adjustment_points", parsed["adjustment_points"])
        )
    except (TypeError, ValueError):
        return None, "invalid"
    if not math.isfinite(cached_adjustment) or cached_adjustment != parsed["adjustment_points"]:
        return None, "invalid"
    return payload, "hit" if current_cache else "legacy_hit"


def _artifact_content_hash(path: Path) -> str:
    target = _hash_target(path)
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_target(path: Path) -> Path:
    if path.is_dir():
        preview = _resolve_preview_image(path)
        if preview is not None:
            return preview
    return path


def _resolve_preview_image(path: Path) -> Path | None:
    if path.is_file() and path.suffix.lower() in _IMAGE_SUFFIXES:
        return path if _is_readable_image(path) else None
    if path.is_dir():
        for candidate in (
            path / "final" / "preview.png",
            path / "preview.png",
            path / "final" / "poster.png",
            path / "poster.png",
        ):
            if (
                candidate.exists()
                and candidate.suffix.lower() in _IMAGE_SUFFIXES
                and _is_readable_image(candidate)
            ):
                return candidate
    return None


def _is_readable_image(path: Path) -> bool:
    try:
        with Image.open(path) as image:
            image.verify()
    except (OSError, UnidentifiedImageError):
        return False
    return True


def _parse_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL)
    if fence:
        stripped = fence.group(1)
    else:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            stripped = stripped[start:end + 1]
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _html_layout_signature(path: Path) -> dict[str, Any]:
    parser = _GeometryHTMLParser()
    parser.feed(path.read_text(encoding="utf-8", errors="replace"))
    elements = parser.elements
    if not elements:
        return _empty_layout_signature("html")

    canvas = _infer_canvas(elements)
    canvas_w = max(1.0, canvas["w"])
    canvas_h = max(1.0, canvas["h"])
    rect_elements = [element for element in elements if element.get("rect")]
    sections = [
        element for element in rect_elements
        if element["tag"] in {"section", "article", "aside"} or _rect_area(element["rect"]) >= canvas_w * canvas_h * 0.04
    ]
    visuals = [element for element in rect_elements if element["tag"] in {"img", "svg", "figure", "canvas"}]
    header = _pick_header(rect_elements, canvas_w, canvas_h)
    palette = sorted({color for element in elements for color in element.get("colors", [])})

    return {
        "source": "html",
        "canvas": {"aspect_ratio": _round(canvas_w / canvas_h), "orientation": _orientation(canvas_w, canvas_h)},
        "header": _header_signature(header, canvas_w, canvas_h),
        "columns": _column_signature(sections, canvas_w),
        "section_bands": _band_signature(sections, canvas_h),
        "panel_geometry": _panel_signature(sections + visuals, canvas_w, canvas_h),
        "occupancy": _occupancy_signature(sections + visuals, canvas_w, canvas_h),
        "palette_structure": _palette_signature(palette),
    }


class _GeometryHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {name.lower(): value or "" for name, value in attrs}
        style = attr_map.get("style", "")
        rect = _rect_from_style(style)
        colors = _colors_from_style(style)
        if rect or colors:
            self.elements.append({"tag": tag.lower(), "rect": rect, "colors": colors})


def _rect_from_style(style: str) -> dict[str, float] | None:
    values = {
        name: _css_px(style, name)
        for name in ("left", "top", "width", "height")
    }
    if values["width"] is None or values["height"] is None:
        return None
    return {
        "x": float(values["left"] or 0.0),
        "y": float(values["top"] or 0.0),
        "w": float(values["width"] or 0.0),
        "h": float(values["height"] or 0.0),
    }


def _css_px(style: str, name: str) -> float | None:
    match = re.search(rf"(?:^|;)\s*{re.escape(name)}\s*:\s*(-?\d+(?:\.\d+)?)px\b", style, flags=re.I)
    return float(match.group(1)) if match else None


def _colors_from_style(style: str) -> list[str]:
    colors: list[str] = []
    for match in re.finditer(r"(?:background|background-color|color)\s*:\s*(#[0-9a-fA-F]{3,8})\b", style):
        colors.append(_normalize_hex(match.group(1)))
    return colors


def _normalize_hex(color: str) -> str:
    color = color.lower()
    if len(color) == 4:
        return "#" + "".join(ch * 2 for ch in color[1:])
    return color[:7]


def _infer_canvas(elements: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    rects = [element["rect"] for element in elements if element.get("rect")]
    if not rects:
        return {"w": 1.0, "h": 1.0}
    largest = max(rects, key=_rect_area)
    max_right = max(rect["x"] + rect["w"] for rect in rects)
    max_bottom = max(rect["y"] + rect["h"] for rect in rects)
    return {
        "w": max(float(largest["w"]), max_right),
        "h": max(float(largest["h"]), max_bottom),
    }


def _pick_header(elements: Sequence[Mapping[str, Any]], canvas_w: float, canvas_h: float) -> Mapping[str, Any] | None:
    candidates = []
    for element in elements:
        rect = element.get("rect") or {}
        y = float(rect.get("y", 0.0))
        h = float(rect.get("h", 0.0))
        w = float(rect.get("w", 0.0))
        if element.get("tag") == "header" or (y <= canvas_h * 0.18 and w >= canvas_w * 0.45 and h <= canvas_h * 0.35):
            candidates.append(element)
    return min(candidates, key=lambda item: (item["rect"]["y"], -item["rect"]["w"])) if candidates else None


def _header_signature(header: Mapping[str, Any] | None, canvas_w: float, canvas_h: float) -> dict[str, Any]:
    if not header:
        return {"present": False}
    rect = header["rect"]
    return {
        "present": True,
        "band": [_round(rect["y"] / canvas_h), _round((rect["y"] + rect["h"]) / canvas_h)],
        "width_ratio": _round(rect["w"] / canvas_w),
    }


def _column_signature(elements: Sequence[Mapping[str, Any]], canvas_w: float) -> list[dict[str, float]]:
    spans: list[tuple[float, float]] = []
    for element in elements:
        rect = element.get("rect") or {}
        if float(rect.get("w", 0.0)) <= 0:
            continue
        spans.append((_round(rect["x"] / canvas_w), _round((rect["x"] + rect["w"]) / canvas_w)))
    return [{"x0": x0, "x1": x1} for x0, x1 in sorted(set(spans))]


def _band_signature(elements: Sequence[Mapping[str, Any]], canvas_h: float) -> list[dict[str, float]]:
    bands: list[tuple[float, float]] = []
    for element in elements:
        rect = element.get("rect") or {}
        if float(rect.get("h", 0.0)) <= 0:
            continue
        bands.append((_round(rect["y"] / canvas_h), _round((rect["y"] + rect["h"]) / canvas_h)))
    return [{"y0": y0, "y1": y1} for y0, y1 in sorted(set(bands))]


def _panel_signature(elements: Sequence[Mapping[str, Any]], canvas_w: float, canvas_h: float) -> list[dict[str, Any]]:
    panels: list[dict[str, Any]] = []
    for element in elements:
        rect = element.get("rect") or {}
        if float(rect.get("w", 0.0)) <= 0 or float(rect.get("h", 0.0)) <= 0:
            continue
        panels.append(
            {
                "kind": "visual" if element.get("tag") in {"img", "svg", "figure", "canvas"} else "section",
                "x": _round(rect["x"] / canvas_w),
                "y": _round(rect["y"] / canvas_h),
                "w": _round(rect["w"] / canvas_w),
                "h": _round(rect["h"] / canvas_h),
            }
        )
    return sorted(panels, key=lambda item: (item["y"], item["x"], item["kind"], item["w"], item["h"]))


def _occupancy_signature(elements: Sequence[Mapping[str, Any]], canvas_w: float, canvas_h: float) -> dict[str, Any]:
    total_area = canvas_w * canvas_h
    areas = [_rect_area(element["rect"]) for element in elements if element.get("rect")]
    return {
        "panel_count": len(areas),
        "area_ratio": _round(min(1.0, sum(areas) / total_area if total_area else 0.0)),
        "largest_panel_ratio": _round(max(areas) / total_area if areas and total_area else 0.0),
    }


def _palette_signature(colors: Sequence[str]) -> dict[str, Any]:
    if not colors:
        return {"color_count": 0, "colors": []}
    return {"color_count": len(colors), "colors": list(colors[:8])}


def _image_layout_signature(path: Path) -> dict[str, Any]:
    try:
        with Image.open(path) as image:
            rgb = image.convert("RGB")
    except (OSError, UnidentifiedImageError):
        return _empty_layout_signature("unavailable")

    small = rgb.resize((64, max(1, round(64 * rgb.height / max(1, rgb.width)))))
    pixels = _image_pixels(small)
    corner_samples = [
        small.getpixel((0, 0)),
        small.getpixel((small.width - 1, 0)),
        small.getpixel((0, small.height - 1)),
        small.getpixel((small.width - 1, small.height - 1)),
    ]
    background = tuple(round(sum(sample[i] for sample in corner_samples) / len(corner_samples)) for i in range(3))
    occupied = [
        _color_distance(pixel, background) > 30
        for pixel in pixels
    ]
    row_counts = [
        sum(occupied[y * small.width:(y + 1) * small.width])
        for y in range(small.height)
    ]
    col_counts = [
        sum(occupied[y * small.width + x] for y in range(small.height))
        for x in range(small.width)
    ]
    row_bands = _projection_bands(row_counts, small.width, small.height)
    col_bands = _projection_bands(col_counts, small.height, small.width)
    occupancy_ratio = sum(1 for value in occupied if value) / max(1, len(occupied))

    return {
        "source": "image",
        "canvas": {"aspect_ratio": _round(rgb.width / max(1, rgb.height)), "orientation": _orientation(rgb.width, rgb.height)},
        "header": {"present": bool(row_bands and row_bands[0][0] <= 0.18), "band": list(row_bands[0]) if row_bands else []},
        "columns": [{"x0": x0, "x1": x1} for x0, x1 in col_bands],
        "section_bands": [{"y0": y0, "y1": y1} for y0, y1 in row_bands],
        "panel_geometry": [],
        "occupancy": {"area_ratio": _round(occupancy_ratio), "band_count": len(row_bands), "column_band_count": len(col_bands)},
        "palette_structure": _sampled_palette(rgb),
    }


def _projection_bands(counts: Sequence[int], denominator: int, length: int) -> list[tuple[float, float]]:
    threshold = max(1, int(denominator * 0.08))
    bands: list[tuple[float, float]] = []
    start: int | None = None
    for index, count in enumerate(counts):
        if count >= threshold and start is None:
            start = index
        elif count < threshold and start is not None:
            if index - start >= 2:
                bands.append((_round(start / length), _round(index / length)))
            start = None
    if start is not None and length - start >= 2:
        bands.append((_round(start / length), _round(1.0)))
    return bands[:8]


def _sampled_palette(image: Image.Image) -> dict[str, Any]:
    sample = image.resize((32, max(1, round(32 * image.height / max(1, image.width)))))
    counts: dict[tuple[int, int, int], int] = {}
    for r, g, b in _image_pixels(sample):
        key = ((r // 32) * 32, (g // 32) * 32, (b // 32) * 32)
        counts[key] = counts.get(key, 0) + 1
    colors = [
        "#%02x%02x%02x" % color
        for color, _count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:8]
    ]
    return {"color_count": len(counts), "colors": colors}


def _empty_layout_signature(source: str) -> dict[str, Any]:
    return {
        "source": source,
        "canvas": {"aspect_ratio": None, "orientation": "unknown"},
        "header": {"present": False},
        "columns": [],
        "section_bands": [],
        "panel_geometry": [],
        "occupancy": {"area_ratio": 0.0, "panel_count": 0},
        "palette_structure": {"color_count": 0, "colors": []},
    }


def _image_pixels(image: Image.Image) -> list[tuple[int, int, int]]:
    flattened = getattr(image, "get_flattened_data", None)
    if callable(flattened):
        return list(flattened())
    return list(image.getdata())


def _rect_area(rect: Mapping[str, Any]) -> float:
    return max(0.0, float(rect.get("w", 0.0))) * max(0.0, float(rect.get("h", 0.0)))


def _color_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    return math.sqrt(sum((float(a[i]) - float(b[i])) ** 2 for i in range(3)))


def _round(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 3)


def _orientation(width: float, height: float) -> str:
    if width > height:
        return "landscape"
    if height > width:
        return "portrait"
    return "square"


def _score_explanation(score: float, adjustment: float) -> str:
    return f"Batch style adaptability score {score:.2f}/10 maps to {adjustment:.1f} adjustment points."


def _skipped(explanation: str, **extra: Any) -> dict[str, Any]:
    return {
        "status": "skipped",
        "eval_protocol": EVAL_PROTOCOL,
        "batch_style_fingerprint": BATCH_STYLE_FINGERPRINT,
        "adjustment_points": 0.0,
        "style_adaptability_score_0_10": None,
        "explanation": explanation,
        **extra,
    }


def _degraded(
    explanation: str,
    *,
    artifact_count: int,
    cache_key: str,
    cache_status: str,
    contact_meta: Mapping[str, Any],
    judge_status: str,
    raw_excerpt: str = "",
) -> dict[str, Any]:
    result = {
        "status": "degraded",
        "eval_protocol": EVAL_PROTOCOL,
        "batch_style_fingerprint": BATCH_STYLE_FINGERPRINT,
        "adjustment_points": 0.0,
        "style_adaptability_score_0_10": None,
        "artifact_count": artifact_count,
        "cache_key": cache_key,
        "cache_status": cache_status,
        "contact_sheet": dict(contact_meta),
        "judge_status": judge_status,
        "explanation": explanation,
    }
    if raw_excerpt:
        result["raw_excerpt"] = raw_excerpt
    return result


BATCH_STYLE_FINGERPRINT = fingerprint_python_symbols(
    Path(__file__),
    ["evaluate_batch_style_homogeneity"],
    namespace=f"{EVAL_PROTOCOL}:batch-style",
)
