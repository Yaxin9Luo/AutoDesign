"""Canvas planning helpers.

The planner should not guess poster dimensions from scratch. This module
chooses a scene-appropriate canvas plan before the LLM sees the brief, then
refines paper posters once ingest reveals figure shape.
"""

from __future__ import annotations

import re
from copy import deepcopy
from math import gcd, isfinite
from pathlib import Path
from typing import Any

from ..config import POSTER_TEMPLATES, resolve_template
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


class CanvasIntentError(ValueError):
    """A stable validation failure for explicit current-request geometry."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def parse_canvas_intent(brief: str) -> dict[str, Any] | None:
    """Parse compatible canvas directives from the current user request only."""
    text = _current_request_text((brief or "").lower())
    pixels = _explicit_canvas_pixel_values(text)
    templates = _explicit_template_ids(text)
    ratios = _explicit_ratio_values(text)
    orientations = _explicit_orientations(text)

    if len(set(pixels)) > 1:
        _raise_canvas_conflict("Multiple exact canvas sizes disagree.")
    if len(set(templates)) > 1:
        template_ratios = {_template_ratio(value) for value in templates}
        if len(template_ratios) > 1:
            _raise_canvas_conflict("Multiple named canvas templates disagree.")
    if ratios:
        first_ratio = ratios[0][0]
        if any(not _ratios_match(first_ratio, ratio) for ratio, _label in ratios[1:]):
            _raise_canvas_conflict("Multiple canvas aspect ratios disagree.")
    if len(set(orientations)) > 1:
        _raise_canvas_conflict("Landscape and portrait were both requested.")

    pixel_value = pixels[0] if pixels else None
    template_id = templates[0] if templates else None
    ratio_value = ratios[0] if ratios else None
    orientation = orientations[0] if orientations else None
    constraints: list[tuple[str, float]] = []
    if pixel_value:
        constraints.append(("exact pixels", pixel_value[0] / pixel_value[1]))
    if template_id:
        constraints.append(("named template", _template_ratio(template_id)))
    if ratio_value:
        constraints.append(("aspect ratio", ratio_value[0]))
    if constraints:
        base_name, base_ratio = constraints[0]
        for name, ratio in constraints[1:]:
            if not _ratios_match(base_ratio, ratio):
                _raise_canvas_conflict(f"{base_name.title()} and {name} disagree.")
        if orientation and _orientation_for_ratio(base_ratio) != orientation:
            _raise_canvas_conflict("Canvas orientation disagrees with the requested geometry.")

    if not any((pixel_value, template_id, ratio_value, orientation)):
        return None
    return {
        "pixels": pixel_value,
        "template_id": template_id,
        "ratio": ratio_value,
        "orientation": orientation,
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
    prompt_intent = parse_canvas_intent(brief)
    prompt_template_id = prompt_intent.get("template_id") if prompt_intent else None
    if prompt_template_id and artifact_type != "poster":
        _raise_canvas_conflict(
            "A Poster template cannot be combined with a different explicit artifact type."
        )
    template_key: str | None = None
    template_canvas: dict[str, object] | None = None
    if requested_template:
        template_key = _norm(requested_template)
        template_canvas = resolve_template(template_key)
        if template_canvas:
            artifact_type = "poster"

    explicit_pixels = prompt_intent.get("pixels") if prompt_intent else None
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
    prompt_template = prompt_template_id
    if prompt_template:
        prompt_canvas = resolve_template(prompt_template) or {}
        plan = _plan(
            artifact_type="poster",
            poster_subtype=_subtype_from_template(prompt_template),
            preset_id=prompt_template,
            canvas=prompt_canvas,
            lock_level="hard",
            source="explicit_template",
            rationale=f"User wording selected the registered template {prompt_template!r}.",
        )
        if prompt_template == "cvpr-landscape":
            return _with_body_grid(plan, "editorial_3col")
        return plan
    prompt_ratio = prompt_intent.get("ratio") if prompt_intent else None
    prompt_orientation = prompt_intent.get("orientation") if prompt_intent else None
    if artifact_type == "poster" and prompt_ratio:
        ratio, aspect_label = prompt_ratio
        preset_id = _preset_id_for_ratio(ratio, aspect_label)
        return _plan(
            artifact_type="poster",
            poster_subtype=_subtype_from_template(preset_id, text=text),
            preset_id=preset_id,
            canvas=_canvas_for_ratio(ratio, aspect_label),
            lock_level="hard",
            source="explicit_ratio",
            rationale=f"User wording requested a {aspect_label} poster canvas.",
        )
    if artifact_type == "poster" and prompt_orientation:
        preset_id = "cvpr-landscape" if prompt_orientation == "landscape" else "event-2x3"
        plan = _plan(
            artifact_type="poster",
            poster_subtype=_subtype_from_template(preset_id, text=text),
            preset_id=preset_id,
            canvas=resolve_template(preset_id) or {},
            lock_level="hard",
            source="explicit_orientation",
            rationale=f"User wording requested {prompt_orientation} poster orientation.",
        )
        if preset_id == "cvpr-landscape":
            return _with_body_grid(plan, "editorial_3col")
        return plan
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
    explicit_type = _explicit_artifact_type(text)
    if explicit_type:
        return explicit_type
    if _has_any(text, _DECK_TOKENS):
        return "deck"
    if _has_any(text, _LANDING_TOKENS):
        return "landing"
    if _has_any(text, _POSTER_TOKENS):
        return "poster"
    if _has_any(text, _VIDEO_TOKENS):
        return "video"
    return "poster"


def _explicit_artifact_type(text: str) -> str | None:
    match = re.search(r"(?:^|\n)\s*type\s*:\s*(poster|landing|deck|video)\b", text)
    return match.group(1) if match else None


def _current_request_text(text: str) -> str:
    marker = "[user's current request:]"
    return text.rsplit(marker, 1)[-1] if marker in text else text


def _explicit_canvas_pixels(text: str) -> tuple[int, int] | None:
    values = _explicit_canvas_pixel_values(text)
    return values[0] if values else None


def _explicit_canvas_pixel_values(text: str) -> list[tuple[int, int]]:
    patterns = (
        r"(?<!\d)(\d{2,5})\s*[x×]\s*(\d{2,5})\s*(?:px|pixels?|像素)(?!\w)",
        r"(?:canvas|size|resolution|dimensions?|画布|尺寸|分辨率)[^\n]{0,40}?(\d{2,5})\s*[x×]\s*(\d{2,5})(?!\d)",
        r"(?:poster|海报)\s*(?:at|in|为|尺寸为|大小为)\s*(\d{2,5})\s*[x×]\s*(\d{2,5})(?!\d)",
        r"(?<!\d)(\d{2,5})\s*[x×]\s*(\d{2,5})\s*(?:academic\s+)?(?:poster|海报)\b",
        r"\bw_px\s*[:=]\s*(\d{2,5})\b[^\n]{0,80}?\bh_px\s*[:=]\s*(\d{2,5})\b",
    )
    values: list[tuple[int, int]] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            if _pixel_match_describes_source_asset(text, match):
                continue
            width, height = int(match.group(1)), int(match.group(2))
            value = (width, height)
            if width >= 64 and height >= 64 and value not in values:
                values.append(value)
    return values


def _explicit_template_ids(text: str) -> list[str]:
    values: list[str] = []
    explicit_pattern = re.compile(
        r"(?:template|preset|模板|预设)\s*[:：=]\s*([a-z0-9][a-z0-9_.-]*)",
        flags=re.IGNORECASE,
    )
    for match in explicit_pattern.finditer(text):
        key = _norm(match.group(1))
        if resolve_template(key) is None:
            raise CanvasIntentError(
                "unknown_canvas_template",
                f"Unknown canvas template: {match.group(1)}",
            )
        if key not in values:
            values.append(key)
    for template_id in POSTER_TEMPLATES:
        pattern = rf"(?<![a-z0-9_-]){re.escape(template_id)}(?![a-z0-9_-])"
        if re.search(pattern, text) and template_id not in values:
            values.append(template_id)
    if re.search(r"\bcvpr\b", text) and _has_any(text, ("template", "preset", "landscape", "poster")):
        if "cvpr-landscape" not in values:
            values.append("cvpr-landscape")
    if re.search(r"\ba0\b", text):
        a0_id = (
            "a0-landscape"
            if _has_any(text, ("landscape", "horizontal", "横版", "横向"))
            else "a0-portrait"
        )
        if a0_id not in values:
            values.append(a0_id)
    return values


def _explicit_ratio_values(text: str) -> list[tuple[float, str]]:
    values: list[tuple[float, str]] = []
    pattern = re.compile(
        r"(?<![\w.-])([+-]?\d+(?:\.\d+)?)\s*([:/x×])\s*([+-]?\d+(?:\.\d+)?)(?![\d.])",
        flags=re.IGNORECASE,
    )
    matches = list(pattern.finditer(text))
    contextual = [_ratio_has_canvas_context(text, match) for match in matches]
    for index in range(1, len(matches)):
        connector = text[matches[index - 1].end():matches[index].start()]
        if re.fullmatch(r"\s*(?:and|or|,|和|或)\s*", connector) and (
            contextual[index - 1] or contextual[index]
        ):
            contextual[index - 1] = contextual[index] = True
    for match, has_context in zip(matches, contextual):
        if not has_context:
            continue
        left = float(match.group(1))
        right = float(match.group(3))
        if not isfinite(left) or not isfinite(right) or left <= 0 or right <= 0:
            raise CanvasIntentError(
                "invalid_canvas_ratio",
                "Canvas aspect ratio values must be finite and greater than zero.",
            )
        if match.group(2).lower() in {"x", "×"} and left >= 64 and right >= 64:
            continue
        ratio = left / right
        label = f"{_format_ratio_number(left)}:{_format_ratio_number(right)}"
        if not any(_ratios_match(ratio, prior) for prior, _prior_label in values):
            values.append((ratio, label))
    decimal_pattern = re.compile(
        r"(?:aspect\s+ratios?|ratios?|宽高比|比例)\s*(?:of|is|[:=])?\s*"
        r"([+-]?\d+\.\d+)(?!\s*[:/x×]\s*[+-]?\d)",
        flags=re.IGNORECASE,
    )
    for match in decimal_pattern.finditer(text):
        ratio = float(match.group(1))
        if not isfinite(ratio) or ratio <= 0:
            raise CanvasIntentError(
                "invalid_canvas_ratio",
                "Canvas aspect ratio values must be finite and greater than zero.",
            )
        if not any(_ratios_match(ratio, prior) for prior, _prior_label in values):
            values.append((ratio, f"{_format_ratio_number(ratio)}:1"))
    return values


def _ratio_has_canvas_context(text: str, match: re.Match[str]) -> bool:
    before = text[max(0, match.start() - 48):match.start()]
    after = text[match.end():match.end() + 48]
    explicit_cue = (
        r"(?:aspect\s+ratios?|ratios?|canvas|size|resolution|dimensions?|"
        r"宽高比|比例|画布|尺寸|分辨率)"
    )
    orientation = r"(?:landscape|horizontal|portrait|vertical|横版|横向|竖版|竖向)"
    return bool(
        re.search(rf"{explicit_cue}\s*(?:of|is|[:=])?\s*$", before)
        or re.match(rf"\s*{explicit_cue}\b", after)
        or re.search(rf"{orientation}\s*$", before)
        or re.match(rf"\s*(?:{orientation}\s+)?(?:academic\s+)?(?:poster|海报)\b", after)
        or re.search(r"(?:poster|海报)\s+in\s*(?:an?\s+)?$", before)
    )


def _pixel_match_describes_source_asset(text: str, match: re.Match[str]) -> bool:
    before = text[max(0, match.start() - 64):match.start()]
    return bool(re.search(
        r"\b(?:source|paper|original)\s+(?:figure|image|asset|visual)\b[^\n]{0,32}$",
        before,
    ))


def _explicit_orientations(text: str) -> list[str]:
    values: list[str] = []
    if _has_any(text, _LANDSCAPE_PAPER_TOKENS):
        values.append("landscape")
    if _has_any(text, ("竖版", "竖向", "portrait", "vertical")):
        values.append("portrait")
    return values


def _template_ratio(template_id: str) -> float:
    canvas = resolve_template(template_id) or {}
    aspect = str(canvas.get("aspect_ratio") or "")
    match = re.fullmatch(r"\s*([0-9.]+)\s*:\s*([0-9.]+)\s*", aspect)
    if match and float(match.group(2)) > 0:
        return float(match.group(1)) / float(match.group(2))
    return float(canvas["w_px"]) / float(canvas["h_px"])


def _ratios_match(left: float, right: float) -> bool:
    return abs(left - right) <= max(abs(left), abs(right), 1.0) * 0.001


def _orientation_for_ratio(ratio: float) -> str:
    if _ratios_match(ratio, 1.0):
        return "square"
    return "landscape" if ratio > 1.0 else "portrait"


def _raise_canvas_conflict(message: str) -> None:
    raise CanvasIntentError("conflicting_canvas_directives", message)


def _format_ratio_number(value: float) -> str:
    return f"{value:g}"


def _nearest_even(value: float) -> int:
    return max(2, int(round(value / 2.0)) * 2)


def _canvas_for_ratio(ratio: float, aspect_label: str) -> dict[str, object]:
    short_edge = 1536
    max_long_edge = 4096
    if ratio >= 1.0:
        width = _nearest_even(short_edge * ratio)
        height = short_edge
        if width > max_long_edge:
            width = max_long_edge
            height = _nearest_even(width / ratio)
    else:
        width = short_edge
        height = _nearest_even(short_edge / ratio)
        if height > max_long_edge:
            height = max_long_edge
            width = _nearest_even(height * ratio)
    return {
        "w_px": width,
        "h_px": height,
        "dpi": 150,
        "aspect_ratio": aspect_label,
        "color_mode": "RGB",
    }


def _preset_id_for_ratio(ratio: float, aspect_label: str) -> str:
    canonical = (
        (2.0, "cvpr-landscape"),
        (5 / 3, "academic-landscape-5x3"),
        (1.4, "academic-landscape-1.4"),
        (4 / 3, "poster-classic-4x3"),
        (3 / 4, "neurips-portrait"),
    )
    for expected, preset_id in canonical:
        if _ratios_match(ratio, expected):
            return preset_id
    return "custom-ratio-" + aspect_label.replace(":", "x").replace(".", "p")


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
