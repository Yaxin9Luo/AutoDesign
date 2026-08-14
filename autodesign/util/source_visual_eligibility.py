"""Shared deterministic eligibility policy for ingested paper visuals."""

from __future__ import annotations

from typing import Any, Iterable, Mapping


ELIGIBILITY_POLICY_VERSION = 2
MAX_UNMATCHED_RESERVE_ASSETS = 2

SEVERE_CROP_FLAGS = frozenset({
    "algorithm_caption_leak",
    "body_text_leak",
    "caption_in_crop",
    "caption_strip_leak",
    "edge_visual_remnant",
    "figure_caption_leak",
    "header_band_leak",
    "multi_caption_leak",
    "neighbor_asset_leak",
    "other_caption_in_crop",
    "page_furniture_leak",
    "page_like_table_crop",
    "table_body_text_leak",
    "section_heading_leak",
    "running_header_leak",
    "partial_visual_crop",
    "page_like_figure_crop",
    "table_fragment_crop",
    "table_without_structure",
})

HARD_BLOCKING_FLAGS = SEVERE_CROP_FLAGS | frozenset({
    "image_payload_unavailable",
    "low_information_visual",
    "unlocated_raster_component",
})

WEAK_RANKING_FLAGS = frozenset({
    "high_edge_whitespace",
    "low_caption_confidence",
    "low_detail_visual_content",
    "low_value_example_crop",
    "mostly_white_visual",
    "no_caption",
    "source_page_unknown",
})

_DERIVED_KEYS = frozenset({
    "designer_eligible",
    "planner_eligible",
    "planner_visible",
    "designer_reject_reasons",
    "planner_reject_reasons",
    "severe_crop_flags",
    "visual_selection_tier",
    "eligibility_policy_version",
})


def classify_source_visual(
    layer_id: str,
    *records: dict[str, Any] | None,
) -> dict[str, Any]:
    """Recompute selection eligibility from source evidence, not stale booleans."""
    sources = [record for record in records if isinstance(record, dict)]
    primary = sources[0] if sources else {}
    strategy = _first_text(sources, "extract_strategy").lower()
    is_embedded_raster = strategy in {"raster", "embedded"}

    placement_flags = _unique_flags(sources, keys=("placement_quality_flags",))
    crop_flags = _unique_flags(sources, keys=("crop_quality_flags", "severe_crop_flags"))
    curation_flags = _unique_flags(sources, keys=("curation_flags",))
    material_flags = _material_warnings(sources)
    if is_embedded_raster:
        for flag in crop_flags:
            if flag in SEVERE_CROP_FLAGS and flag not in placement_flags:
                placement_flags.append(flag)
        hard_flags = [
            flag for flag in [*curation_flags, *material_flags]
            if flag in HARD_BLOCKING_FLAGS
        ]
    else:
        hard_flags = [
            flag for flag in [*crop_flags, *curation_flags, *material_flags]
            if flag in HARD_BLOCKING_FLAGS
        ]
    hard_flags = _unique_strings(hard_flags)
    severe_flags = [flag for flag in hard_flags if flag in SEVERE_CROP_FLAGS]

    reasons: list[str] = []
    if str(primary.get("kind") or strategy).lower() == "source_table_crop_candidate":
        reasons.append("audit_only_source_table_crop_candidate")
    for flag in hard_flags:
        prefix = "severe_crop" if flag in SEVERE_CROP_FLAGS else "selected_blocking_flag"
        reasons.append(f"{prefix}:{flag}")

    caption = _caption_text(sources)
    captioned_group = any(
        bool(
            source.get("captioned_source_group")
            or source.get("source_group_id")
            or source.get("source_group_label")
            or source.get("source_group_caption")
            or (
                source.get("protected_anchor")
                and str(source.get("anchor_reason") or "") == "captioned_source_group"
            )
        )
        for source in sources
    )
    confidence = max((_safe_float(source.get("caption_confidence")) for source in sources), default=0.0)
    association_method = _first_text(sources, "caption_association_method").lower()
    is_table = str(primary.get("kind") or "").lower() == "table" or str(layer_id).startswith("ingest_table_")
    table_has_structure = any(bool(source.get("headers") or source.get("rows")) for source in sources)
    reliable_caption = (
        association_method != "unmatched"
        and bool(caption)
        and (captioned_group or confidence >= 0.35)
    )
    reliable_caption = reliable_caption or (is_table and table_has_structure)
    if "low_value_example_crop" in {*curation_flags, *material_flags} and not reliable_caption:
        reasons.append("selected_blocking_flag:low_value_example_crop")

    if reasons:
        tier = "rejected"
    elif reliable_caption:
        tier = "eligible"
    elif _clean_unmatched_visual_can_be_reserved(layer_id, sources, strategy):
        tier = "reserve_unmatched"
    else:
        tier = "rejected"
        reasons.append("unmatched_caption_not_reservable")

    eligible = tier != "rejected"
    return {
        "designer_eligible": eligible,
        "planner_eligible": eligible,
        "planner_visible": eligible,
        "designer_reject_reasons": list(reasons),
        "planner_reject_reasons": list(reasons),
        "severe_crop_flags": severe_flags,
        "placement_quality_flags": placement_flags,
        "visual_selection_tier": tier,
        "eligibility_policy_version": ELIGIBILITY_POLICY_VERSION,
        "unmatched_caption": tier == "reserve_unmatched",
    }


def source_visual_tier(layer_id: str, *records: dict[str, Any] | None) -> str:
    return str(classify_source_visual(layer_id, *records).get("visual_selection_tier") or "rejected")


def constrain_optional_source_visual_ids(
    values: Iterable[str],
    records_by_id: Mapping[str, dict[str, Any]],
    *,
    minimum_count: int,
) -> list[str]:
    """Keep eligible optionals and only the unmatched reserves needed for shortfall."""
    tiers = {
        str(layer_id): source_visual_tier(str(layer_id), record)
        for layer_id, record in records_by_id.items()
        if isinstance(record, dict)
    }
    eligible_count = sum(1 for tier in tiers.values() if tier == "eligible")
    reserve_allowance = min(
        max(0, int(minimum_count or 0) - eligible_count),
        MAX_UNMATCHED_RESERVE_ASSETS,
    )
    out: list[str] = []
    reserve_count = 0
    for raw in values:
        layer_id = str(raw or "").strip()
        if not layer_id or layer_id in out:
            continue
        tier = tiers.get(layer_id, "rejected")
        if tier == "eligible":
            out.append(layer_id)
        elif tier == "reserve_unmatched" and reserve_count < reserve_allowance:
            out.append(layer_id)
            reserve_count += 1
    return out


def _clean_unmatched_visual_can_be_reserved(
    layer_id: str,
    sources: list[dict[str, Any]],
    strategy: str,
) -> bool:
    if not str(layer_id or "").startswith("ingest_fig_"):
        return False
    if strategy not in {"raster", "embedded", "vector", "captioned_group"}:
        return False
    source_page = max((_safe_int(source.get("source_page")) for source in sources), default=0)
    if source_page <= 0:
        return False
    width, height = _largest_dimensions(sources)
    return width >= 180 and height >= 120


def _largest_dimensions(sources: list[dict[str, Any]]) -> tuple[int, int]:
    best = (0, 0)
    for source in sources:
        width = _safe_int(source.get("output_width_px") or source.get("width"))
        height = _safe_int(source.get("output_height_px") or source.get("height"))
        if not width or not height:
            raw = str(source.get("image_size") or "")
            if "x" in raw.lower():
                left, right = raw.lower().split("x", 1)
                width = _safe_int(left)
                height = _safe_int(right)
        if width * height > best[0] * best[1]:
            best = (width, height)
    return best


def _caption_text(sources: list[dict[str, Any]]) -> str:
    values: list[str] = []
    for source in sources:
        for key in ("caption", "caption_text", "caption_full", "caption_short", "source_group_caption"):
            value = str(source.get(key) or "").strip()
            if value:
                values.append(value)
    return " ".join(values).strip()


def _first_text(sources: list[dict[str, Any]], key: str) -> str:
    for source in sources:
        value = str(source.get(key) or "").strip()
        if value:
            return value
    return ""


def _unique_flags(sources: list[dict[str, Any]], *, keys: tuple[str, ...]) -> list[str]:
    values: list[str] = []
    for source in sources:
        for key in keys:
            if key in _DERIVED_KEYS and key != "severe_crop_flags":
                continue
            for raw in list(source.get(key) or []):
                value = str(raw or "").strip()
                if value and value not in values:
                    values.append(value)
    return values


def _material_warnings(sources: list[dict[str, Any]]) -> list[str]:
    values: list[str] = []
    for source in sources:
        material = source.get("material_quality")
        if not isinstance(material, dict):
            continue
        for raw in list(material.get("warnings") or []):
            value = str(raw or "").strip()
            if value and value not in values:
                values.append(value)
    return values


def _unique_strings(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if item and item not in out:
            out.append(item)
    return out


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
