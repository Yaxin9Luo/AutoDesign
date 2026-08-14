"""Canvas planning helpers.

The planner should not guess poster dimensions from scratch. This module
chooses a scene-appropriate canvas plan before the LLM sees the brief, then
refines paper posters once ingest reveals figure shape.
"""

from __future__ import annotations

import re
from copy import deepcopy
from math import gcd
from pathlib import Path
from typing import Any

from ..config import resolve_template
from .reference_poster import reference_canvas_from_metadata

CanvasPlan = dict[str, Any]

_POSTER_TOKENS = ("poster", "海报", "主视觉", "宣传图", "flyer")
_DECK_TOKENS = (
    "deck", "slide", "slides", "ppt", "pptx", "powerpoint", "keynote",
    "幻灯片", "演示", "演讲稿",
)
_LANDING_TOKENS = (
    "landing", "one-pager", "web page", "website", "网页", "网站", "着陆页",
    "project page", "page",
)
_VIDEO_TOKENS = ("video", "mp4", "animation", "animated", "视频", "动画")
_ACADEMIC_TOKENS = (
    "paper", "academic", "research", "conference", "neurips", "cvpr",
    "icml", "iclr", "论文", "学术", "会议",
)
_LANDSCAPE_PAPER_TOKENS = (
    "landscape", "horizontal", "横版", "横向",
)
_WIDE_PAPER_TOKENS = (
    "wide", "motion", "video", "qualitative", "comparison", "frame",
    "frames", "sequence", "text-to-video", "temporal", "demo",
)
_EVENT_TOKENS = (
    "event", "movie", "music", "concert", "festival", "party", "show",
    "活动", "电影", "音乐", "演出", "展览", "讲座",
)
_PRODUCT_TOKENS = ("product", "promo", "campaign", "social", "ad", "商品", "促销")
_CVPR_CANONICAL_PAPER_PRESETS = {
    "academic-wide-2x1",
    "academic-wide-3280x1860",
    "academic-landscape-1.414",
    "cvpr-landscape",
}


def plan_canvas(
    brief: str,
    attachments: list[Path],
    *,
    requested_template: str | None = None,
    reference_metadata: dict[str, Any] | None = None,
) -> CanvasPlan:
    """Return an initial canvas plan from user intent and attachments."""
    text = (brief or "").lower()
    request_text = _current_request_text(text)
    artifact_type = _infer_artifact_type(request_text)
    template_key: str | None = None
    template_canvas: dict[str, object] | None = None
    if requested_template:
        requested_key = _norm(requested_template)
        template_key = "cvpr-landscape" if requested_key in _CVPR_CANONICAL_PAPER_PRESETS else requested_key
        template_canvas = resolve_template(template_key)
        if template_canvas:
            artifact_type = "poster"

    explicit_pixels = _explicit_canvas_pixels(request_text)
    if explicit_pixels is not None:
        width, height = explicit_pixels
        divisor = gcd(width, height)
        return _plan(
            artifact_type=artifact_type,
            poster_subtype="custom_poster" if artifact_type == "poster" else None,
            preset_id=(
                f"custom-{width}x{height}"
                if artifact_type == "poster"
                else f"custom-{artifact_type}-{width}x{height}"
            ),
            canvas={
                "w_px": width,
                "h_px": height,
                "dpi": 150 if artifact_type == "poster" else 96,
                "aspect_ratio": f"{width // divisor}:{height // divisor}",
                "color_mode": "RGB",
            },
            lock_level="hard",
            source="explicit_pixels",
            rationale=f"User wording requested an exact {width}x{height} pixel canvas.",
        )
    if requested_template and template_key and template_canvas:
        plan = _plan(
            artifact_type="poster",
            poster_subtype=_subtype_from_template(template_key),
            preset_id=template_key,
            canvas=template_canvas,
            lock_level="hard",
            source="template",
            rationale=f"Explicit template {requested_template!r} was requested.",
        )
        if template_key == "cvpr-landscape":
            return _with_body_grid(plan, "editorial_3col")
        return plan

    if artifact_type == "deck":
        return _plan(
            artifact_type="deck",
            poster_subtype=None,
            preset_id="deck-16x9",
            canvas={"w_px": 1920, "h_px": 1080, "dpi": 96, "aspect_ratio": "16:9", "color_mode": "RGB"},
            lock_level="advisory",
            source="artifact_default",
            rationale="Deck artifacts use a 16:9 slide canvas by default.",
        )
    if artifact_type == "landing":
        return _plan(
            artifact_type="landing",
            poster_subtype=None,
            preset_id="landing-responsive",
            canvas={"w_px": 1440, "h_px": 1200, "dpi": 96, "aspect_ratio": "responsive", "color_mode": "RGB"},
            lock_level="advisory",
            source="artifact_default",
            rationale="Landing pages are responsive; canvas is only an authoring preview.",
        )
    if artifact_type == "video":
        return _plan(
            artifact_type="video",
            poster_subtype=None,
            preset_id="video-16x9",
            canvas={"w_px": 1920, "h_px": 1080, "dpi": 96, "aspect_ratio": "16:9", "color_mode": "RGB"},
            lock_level="advisory",
            source="artifact_default",
            rationale="Video artifacts use a 16:9 scene canvas by default.",
        )

    explicit = _explicit_poster_preset(request_text)
    if explicit is not None:
        preset_id, source = explicit
        canvas = resolve_template(preset_id) or {}
        return _plan(
            artifact_type="poster",
            poster_subtype=_subtype_from_template(preset_id, text=text),
            preset_id=preset_id,
            canvas=canvas,
            lock_level="hard" if source == "explicit_ratio" else "soft",
            source=source,
            rationale=f"User wording selected poster preset {preset_id}.",
        )

    reference_canvas = reference_canvas_from_metadata(reference_metadata or {})
    if reference_canvas:
        return _plan(
            artifact_type="poster",
            poster_subtype="reference_poster",
            preset_id="reference-poster",
            canvas=reference_canvas,
            lock_level="hard",
            source="reference_poster",
            rationale="Reference poster geometry supplies the default canvas.",
        )

    has_pdf = any(Path(p).suffix.lower() == ".pdf" for p in attachments)
    if has_pdf or _has_any(text, _ACADEMIC_TOKENS):
        return _with_body_grid(_plan(
            artifact_type="poster",
            poster_subtype="academic_paper_cvpr_landscape",
            preset_id="cvpr-landscape",
            canvas=resolve_template("cvpr-landscape") or {},
            lock_level="soft",
            source="brief_scene",
            rationale=(
                "Academic paper posters default to a fixed 3072x1536 "
                "three-column conference editorial-flow board."
            ),
        ), "editorial_3col")

    if _has_any(text, _EVENT_TOKENS):
        return _plan(
            artifact_type="poster",
            poster_subtype="event_poster",
            preset_id="event-2x3",
            canvas=resolve_template("event-2x3") or {},
            lock_level="soft",
            source="brief_scene",
            rationale="Event/movie/music posters usually benefit from portrait 2:3 composition.",
        )
    if _has_any(text, _PRODUCT_TOKENS):
        return _plan(
            artifact_type="poster",
            poster_subtype="product_social_poster",
            preset_id="social-4x5",
            canvas=resolve_template("social-4x5") or {},
            lock_level="soft",
            source="brief_scene",
            rationale="Product/social poster wording favors 4:5 feed composition.",
        )
    return _plan(
        artifact_type="poster",
        poster_subtype="generic_poster",
        preset_id="poster-classic-4x3",
        canvas=resolve_template("poster-classic-4x3") or {},
        lock_level="advisory",
        source="ambiguous_default",
        rationale="Poster intent was present, but subtype/aspect was ambiguous.",
    )


def refine_canvas_plan_from_ingest(
    plan: CanvasPlan | None,
    summaries: list[dict[str, Any]],
    rendered_layers: dict[str, dict[str, Any]],
    *,
    brief: str = "",
) -> CanvasPlan | None:
    """Refine a poster plan using ingested paper figure shape."""
    if not plan or plan.get("artifact_type") != "poster":
        return plan
    if plan.get("lock_level") == "hard":
        return plan
    if not any((s.get("type") == "pdf") for s in summaries):
        return plan

    ids: list[str] = []
    for summary in summaries:
        ids.extend(list(summary.get("registered_figure_ids") or summary.get("registered_layer_ids") or []))
        ids.extend(list(summary.get("registered_table_ids") or []))
    stats = _ingest_shape_stats(ids, rendered_layers, summaries=summaries)
    grid_family = _choose_paper_grid_family(plan, stats, brief=brief)
    target_preset = _preset_for_grid_family(grid_family)
    current = str(plan.get("preset_id") or "")
    current_grid = ((plan.get("body_grid") or {}).get("family") if isinstance(plan.get("body_grid"), dict) else None)
    refined = deepcopy(plan)
    if current != target_preset:
        refined.update({
            "poster_subtype": "academic_paper_wide" if grid_family == "4x2_balanced" else "academic_paper_landscape",
            "preset_id": target_preset,
            "canvas": resolve_template(target_preset) or {},
            "lock_level": "soft",
            "source": "ingest_shape",
            "rationale": _grid_family_rationale(grid_family, stats),
        })
    elif current_grid != grid_family:
        refined.update({
            "source": "ingest_shape",
            "rationale": _grid_family_rationale(grid_family, stats),
        })
    refined["density_budget"] = _density_budget(target_preset)
    refined["ingest_shape"] = stats
    _apply_body_grid(refined, grid_family)
    return refined


def _ingest_shape_stats(
    ids: list[str],
    rendered_layers: dict[str, dict[str, Any]],
    *,
    summaries: list[dict[str, Any]],
) -> dict[str, Any]:
    wide_count = 0
    ultra_wide_count = 0
    tall_count = 0
    ordinary_count = 0
    table_count = 0
    motionish_count = 0
    for layer_id in ids:
        rec = rendered_layers.get(layer_id) or {}
        ratio = _record_ratio(rec)
        if ratio >= 2.8:
            ultra_wide_count += 1
        if ratio >= 1.6:
            wide_count += 1
        elif ratio > 0 and ratio < 0.85:
            tall_count += 1
        elif 0.85 <= ratio < 1.6:
            ordinary_count += 1
        if _is_table_like_record(layer_id, rec):
            table_count += 1
        text = " ".join(str(rec.get(k) or "") for k in (
            "caption", "caption_short", "title", "name", "source_ref",
        )).lower()
        if _has_any(text, _WIDE_PAPER_TOKENS):
            motionish_count += 1
    manifest = _first_manifest(summaries)
    title_words = len(re.findall(r"[^\W_]+", str(manifest.get("title") or ""), flags=re.UNICODE))
    authors_count = len(manifest.get("authors") or []) if isinstance(manifest.get("authors"), list) else 0
    section_count = len(manifest.get("sections") or []) if isinstance(manifest.get("sections"), list) else 0
    resultish_sections = 0
    for section in manifest.get("sections") or []:
        if not isinstance(section, dict):
            continue
        haystack = " ".join(str(section.get(k) or "") for k in ("title", "heading", "summary", "text")).lower()
        if any(token in haystack for token in ("result", "experiment", "benchmark", "ablation", "evaluation", "table")):
            resultish_sections += 1
    return {
        "n_visuals": len(ids),
        "wide_visuals": wide_count,
        "ultra_wide_visuals": ultra_wide_count,
        "ordinary_visuals": ordinary_count,
        "tall_visuals": tall_count,
        "table_visuals": table_count,
        "motion_or_sequence_visuals": motionish_count,
        "title_words": title_words,
        "authors_count": authors_count,
        "paper_section_count": section_count,
        "result_or_benchmark_section_count": resultish_sections,
    }


def _choose_paper_grid_family(plan: CanvasPlan, stats: dict[str, Any], *, brief: str = "") -> str:
    if str(plan.get("preset_id") or "") == "cvpr-landscape":
        return "editorial_3col"
    text = (brief or "").lower()
    n_visuals = int(stats.get("n_visuals") or 0)
    wide = int(stats.get("wide_visuals") or 0)
    ultra = int(stats.get("ultra_wide_visuals") or 0)
    tall = int(stats.get("tall_visuals") or 0)
    tables = int(stats.get("table_visuals") or 0)
    motionish = int(stats.get("motion_or_sequence_visuals") or 0)
    sections = int(stats.get("paper_section_count") or 0)
    result_sections = int(stats.get("result_or_benchmark_section_count") or 0)
    title_words = int(stats.get("title_words") or 0)
    authors = int(stats.get("authors_count") or 0)
    tall_ratio = tall / max(1, n_visuals)
    identity_heavy = title_words >= 14 or authors >= 10
    text_or_result_pressure = sections >= 8 or result_sections >= 3 or tables >= 2 or identity_heavy
    wide_pressure = ultra >= 1 or wide >= 4 or motionish >= 3 or _has_any(text, _WIDE_PAPER_TOKENS)
    tall_pressure = tall >= 2 or tall_ratio >= 0.35
    if tall_pressure or (text_or_result_pressure and not wide_pressure):
        return "3x3_landscape"
    if wide_pressure or (n_visuals >= 8 and (wide >= 3 or tables >= 1)):
        return "4x2_balanced"
    if n_visuals >= 8 or text_or_result_pressure:
        return "3x3_landscape"
    return "3x2_landscape"


def _preset_for_grid_family(grid_family: str) -> str:
    if grid_family in {"cvpr_3col", "editorial_3col"}:
        return "cvpr-landscape"
    if grid_family == "4x2_balanced":
        return "academic-wide-3280x1860"
    return "academic-landscape-1.414"


def _grid_family_rationale(grid_family: str, stats: dict[str, Any]) -> str:
    if grid_family == "editorial_3col":
        return "Use the fixed 3072x1536 three-column editorial-flow paper-poster layout."
    if grid_family == "cvpr_3col":
        return "Use the fixed CVPR-style 3072x1536 three-column paper-poster grid."
    if grid_family == "4x2_balanced":
        return (
            "Ingest found wide/result-heavy visual evidence; use a balanced "
            "4x2 academic board. Automatic 2:1 compression is disabled because "
            "3x2 is the minimum paper-poster capacity."
        )
    if grid_family == "3x3_landscape":
        return (
            "Ingest found enough text, table, identity, or tall-figure pressure "
            "to expand beyond the 3x2 minimum while preserving landscape readability."
        )
    return "Use the 3x2 landscape academic grid as the minimum paper-poster capacity."


def _with_body_grid(plan: CanvasPlan, family: str) -> CanvasPlan:
    out = deepcopy(plan)
    _apply_body_grid(out, family)
    return out


def _apply_body_grid(plan: CanvasPlan, family: str) -> None:
    if family == "editorial_3col":
        plan["grid_family"] = family
        plan["body_grid"] = {
            "family": family,
            "cols": 3,
            "rows": 1,
            "layout_mode": "editorial_flow",
            "min_sections_total": 7,
            "target_sections_total": 9,
            "allowed_families": ["editorial_3col"],
            "disallowed_families": ["cvpr_3col", "2x1_wide", "4x3_expanded"],
        }
        return
    if family == "cvpr_3col":
        cols, rows = 3, 2
    elif family == "4x2_balanced":
        cols, rows = 4, 2
    elif family == "3x3_landscape":
        cols, rows = 3, 3
    else:
        family, cols, rows = "3x2_landscape", 3, 2
    plan["grid_family"] = family
    plan["body_grid"] = {
        "family": family,
        "cols": cols,
        "rows": rows,
        "main_panel_count": cols * rows,
        "min_main_panels": 6,
        "target_main_panels": cols * rows,
        "max_main_panels": cols * rows,
        "allowed_families": ["cvpr_3col", "3x2_landscape", "4x2_balanced", "3x3_landscape"],
        "disallowed_families": ["2x1_wide", "4x3_expanded"],
    }


def _is_table_like_record(layer_id: str, rec: dict[str, Any]) -> bool:
    if str(layer_id).startswith("ingest_table_"):
        return True
    text = " ".join(str(rec.get(k) or "") for k in (
        "kind", "visual_role", "caption", "caption_short", "title", "name", "source_ref",
    )).lower()
    return any(token in text for token in ("table", "benchmark", "leaderboard"))


def _first_manifest(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    for summary in summaries:
        manifest = summary.get("manifest") if isinstance(summary, dict) else None
        if isinstance(manifest, dict):
            return manifest
    return {}


def apply_canvas_plan_prologue(brief: str, plan: CanvasPlan | None) -> str:
    if not plan:
        return brief
    return canvas_plan_prologue_block(plan) + "\n\n---\n\n" + brief


def canvas_plan_prologue_block(plan: CanvasPlan | None) -> str:
    if not plan:
        return ""
    canvas = plan.get("canvas") or {}
    budget = plan.get("density_budget") or {}
    return (
        "Canvas Plan:\n"
        f"  artifact_type: {plan.get('artifact_type')}\n"
        f"  poster_subtype: {plan.get('poster_subtype')}\n"
        f"  preset_id: {plan.get('preset_id')}\n"
        f"  lock_level: {plan.get('lock_level')}\n"
        f"  source: {plan.get('source')}\n"
        f"  canvas: {_compact_kv(canvas)}\n"
        f"  density_budget: {_compact_kv(budget)}\n"
        f"  body_grid: {_compact_kv(plan.get('body_grid') or {})}\n"
        f"  rationale: {plan.get('rationale')}\n\n"
        "Use this canvas on DesignSpec.canvas. For lock_level=hard, copy it exactly. "
        "For lock_level=soft, keep the same aspect family unless you add "
        "`canvas_plan_override_reason` to DesignSpec.canvas. For lock_level=advisory, "
        "you may adapt if the scene clearly needs it."
    )


def _plan(
    *,
    artifact_type: str,
    poster_subtype: str | None,
    preset_id: str,
    canvas: dict[str, object],
    lock_level: str,
    source: str,
    rationale: str,
) -> CanvasPlan:
    return {
        "artifact_type": artifact_type,
        "poster_subtype": poster_subtype,
        "preset_id": preset_id,
        "canvas": dict(canvas),
        "lock_level": lock_level,
        "density_budget": _density_budget(preset_id),
        "rationale": rationale,
        "source": source,
    }


def _infer_artifact_type(text: str) -> str:
    explicit_type = re.search(r"(?:^|\n)\s*type\s*:\s*(poster|landing|deck|video)\b", text)
    if explicit_type:
        return explicit_type.group(1)
    if _has_any(text, _DECK_TOKENS):
        return "deck"
    if _has_any(text, _LANDING_TOKENS):
        return "landing"
    if _has_any(text, _POSTER_TOKENS):
        return "poster"
    if _has_any(text, _VIDEO_TOKENS):
        return "video"
    return "poster"


def _current_request_text(text: str) -> str:
    marker = "[user's current request:]"
    return text.rsplit(marker, 1)[-1] if marker in text else text


def _explicit_poster_preset(text: str) -> tuple[str, str] | None:
    if "a0" in text:
        if _has_any(text, ("landscape", "horizontal", "横版", "横向")):
            return ("a0-landscape", "explicit_ratio")
        return ("a0-portrait", "explicit_ratio")
    ratios = (
        (r"\b2\s*[:x×]\s*1\b", "cvpr-landscape"),
        (r"\b4\s*[:x×]\s*3\b", "poster-classic-4x3"),
        (r"\b3\s*[:x×]\s*4\b", "neurips-portrait"),
        (r"\b2\s*[:x×]\s*3\b", "event-2x3"),
        (r"\b4\s*[:x×]\s*5\b", "social-4x5"),
        (r"\b9\s*[:x×]\s*16\b", "story-9x16"),
        (r"\b1\s*[:x×]\s*1\b", "square-1x1"),
    )
    for pattern, preset in ratios:
        if re.search(pattern, text):
            return (preset, "explicit_ratio")
    if _has_any(text, _LANDSCAPE_PAPER_TOKENS):
        return ("cvpr-landscape", "explicit_orientation")
    if _has_any(text, ("竖版", "竖向", "portrait", "vertical")):
        return ("event-2x3", "explicit_orientation")
    return None


def _explicit_canvas_pixels(text: str) -> tuple[int, int] | None:
    patterns = (
        r"(?<!\d)(\d{2,5})\s*[x×]\s*(\d{2,5})\s*(?:px|pixels?|像素)(?!\w)",
        r"(?:canvas|size|画布|尺寸)[^\n]{0,40}?(\d{2,5})\s*[x×]\s*(\d{2,5})(?!\d)",
        r"\bw_px\s*[:=]\s*(\d{2,5})\b[^\n]{0,80}?\bh_px\s*[:=]\s*(\d{2,5})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        width, height = int(match.group(1)), int(match.group(2))
        if width >= 64 and height >= 64:
            return width, height
    return None


def _subtype_from_template(template: str, *, text: str = "") -> str:
    key = _norm(template)
    if key == "cvpr-landscape":
        return "academic_paper_cvpr_landscape"
    if (
        key.startswith("a0")
        or key.startswith("academic-landscape")
        or "conference" in key
        or "neurips" in key
        or "cvpr" in key
        or "icml" in key
    ):
        return "academic_paper"
    if key.startswith("academic-wide"):
        return "academic_paper_wide"
    if key.startswith("event"):
        return "event_poster"
    if key.startswith("social") or key.startswith("story") or key.startswith("square"):
        return "product_social_poster" if _has_any(text, _PRODUCT_TOKENS) else "generic_poster"
    return "generic_poster"


def _density_budget(preset_id: str) -> dict[str, Any]:
    if preset_id in {"deck-16x9", "video-16x9"} or preset_id.startswith(("custom-deck-", "custom-video-")):
        return {"target_visuals_min": 1, "target_visuals_max": 4, "max_visuals": 6, "visual_area_min": 0.20, "max_text_layers": 12}
    if preset_id == "landing-responsive" or preset_id.startswith("custom-landing-"):
        return {"target_visuals_min": 2, "target_visuals_max": 5, "max_visuals": 8, "visual_area_min": 0.20, "max_text_layers": 18}
    if preset_id == "conference-poster-portrait":
        return {"target_visuals_min": 8, "target_visuals_max": 12, "max_visuals": 12, "visual_area_min": 0.55, "max_text_layers": 16}
    if preset_id.startswith("academic-wide"):
        return {"target_visuals_min": 8, "target_visuals_max": 12, "max_visuals": 12, "visual_area_min": 0.50, "max_text_layers": 14}
    if preset_id == "academic-landscape-1.414":
        return {"target_visuals_min": 8, "target_visuals_max": 12, "max_visuals": 12, "visual_area_min": 0.47, "max_text_layers": 14}
    if preset_id == "cvpr-landscape":
        return {"target_visuals_min": 6, "target_visuals_max": 10, "max_visuals": 10, "visual_area_min": 0.42, "max_text_layers": 14}
    if preset_id == "reference-poster":
        return {"target_visuals_min": 6, "target_visuals_max": 10, "max_visuals": 10, "visual_area_min": 0.42, "max_text_layers": 14}
    if preset_id == "poster-classic-4x3":
        return {"target_visuals_min": 3, "target_visuals_max": 5, "max_visuals": 5, "visual_area_min": 0.30, "max_text_layers": 10}
    if preset_id in {"neurips-portrait", "icml-portrait", "a0-portrait", "a0-landscape"}:
        return {"target_visuals_min": 5, "target_visuals_max": 9, "max_visuals": 9, "visual_area_min": 0.42, "max_text_layers": 14}
    return {"target_visuals_min": 1, "target_visuals_max": 3, "max_visuals": 4, "visual_area_min": 0.20, "max_text_layers": 8}


def _record_ratio(rec: dict[str, Any]) -> float:
    raw = str(rec.get("image_size") or "")
    match = re.match(r"\s*(\d+)\s*x\s*(\d+)\s*", raw)
    if match:
        w, h = int(match.group(1)), int(match.group(2))
        if h:
            return w / h
    raw_aspect = str(rec.get("aspect_ratio") or "")
    match = re.match(r"\s*([0-9.]+)\s*[:/]\s*([0-9.]+)\s*", raw_aspect)
    if match:
        h = float(match.group(2))
        return float(match.group(1)) / h if h else 1.0
    return 1.0


def _compact_kv(data: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in data.items())


def _norm(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def _has_any(text: str, tokens: tuple[str, ...]) -> bool:
    return any(token in text for token in tokens)
