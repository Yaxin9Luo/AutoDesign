"""Deterministic paper-poster visual storyboard selection.

This module captures the useful part of the Codex-native poster workflow:
read the paper's figure/table catalog, choose a conference-poster-sized
source-backed visual set that tells a coherent story, and explain why each
asset was selected.
It is deliberately local and deterministic so ingest can use it before any
planner model call.
"""

from __future__ import annotations

import re
from typing import Any

from .source_visual_eligibility import classify_source_visual


_ROLE_PRIORITY = {
    "method": 5,
    "table": 4,
    "evidence": 4,
    "qualitative": 3,
    "fallback": 1,
}
_SEVERE_CROP_CURATION_FLAGS = frozenset({
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
_WEAK_VISUAL_CURATION_FLAGS = frozenset({
    "high_edge_whitespace",
    "low_caption_confidence",
    "low_detail_visual_content",
    "mostly_white_visual",
    "no_caption",
})
_SOURCE_HASH_FIELDS = (
    "output_sha256",
    "sha256",
    "image_sha256",
    "source_sha256",
    "source_image_sha256",
    "crop_sha256",
    "source_crop_sha256",
    "png_sha256",
)
_PERCEPTUAL_HASH_FIELDS = (
    "perceptual_hash",
    "image_perceptual_hash",
    "phash",
    "p_hash",
    "dhash",
    "ahash",
    "average_hash",
)


def build_paper_visual_storyboard(
    *,
    manifest: dict[str, Any] | None,
    recommended_text_units: dict[str, list[dict[str, Any]]] | None,
    recommended_figures: dict[str, list[str]] | None,
    visual_candidate_scores: list[dict[str, Any]] | None,
    paper_visual_provenance: dict[str, Any] | None,
    canvas_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a Codex-style source-backed visual storyboard."""
    provenance = paper_visual_provenance if isinstance(paper_visual_provenance, dict) else {}
    assets = [
        asset for asset in list(provenance.get("assets") or [])
        if isinstance(asset, dict) and str(asset.get("asset_id") or "").strip()
        and not _is_audit_only_source_asset(asset)
    ]
    if not assets:
        return {}

    by_id = {str(asset.get("asset_id")): asset for asset in assets}
    recommended_figures = recommended_figures if isinstance(recommended_figures, dict) else {}
    scores = visual_candidate_scores if isinstance(visual_candidate_scores, list) else []
    score_by_id = {
        str(item.get("layer_id") or ""): item
        for item in scores
        if isinstance(item, dict) and str(item.get("layer_id") or "").strip()
    }
    capacity = _source_asset_capacity(canvas_plan, len(assets))
    target_count = capacity["target_count"]
    primary_count = capacity["primary_count"]
    prefer_wide_assets = _prefers_wide_assets(canvas_plan)

    selected: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    def choose(
        role: str,
        candidates: list[str],
        reason: str,
        *,
        max_count: int = 1,
        allow_duplicate_group: bool = False,
    ) -> None:
        kept = 0
        for asset_id in _rank_asset_ids(candidates, by_id, score_by_id, prefer_wide=prefer_wide_assets):
            if kept >= max_count or len(selected) >= target_count:
                return
            if _already_selected(selected, asset_id):
                continue
            asset = by_id.get(asset_id)
            score = score_by_id.get(asset_id) or {}
            if not asset:
                continue
            if _visual_selection_tier(asset, score) != "eligible":
                continue
            planner_reject_reasons = _planner_reject_reasons(asset, score)
            if planner_reject_reasons:
                _append_rejected(rejected, asset_id, "; ".join(planner_reject_reasons))
                continue
            if _is_low_information(asset, score, by_id=by_id, score_by_id=score_by_id):
                if asset_id:
                    _append_rejected(rejected, asset_id, "low information or weak source asset")
                continue
            duplicate_index = None if allow_duplicate_group else _duplicate_visual_group_index(selected, asset)
            if duplicate_index is not None:
                existing = selected[duplicate_index]
                if _should_replace_selected_duplicate(existing, asset, score):
                    old_asset_id = str(existing.get("asset_id") or "")
                    selected[duplicate_index] = _selection_record(
                        asset,
                        str(existing.get("story_role") or role),
                        str(existing.get("reason") or reason),
                        score,
                    )
                    _append_rejected(
                        rejected,
                        old_asset_id,
                        "replaced by cleaner captioned source-group asset",
                    )
                    continue
                _append_rejected(
                    rejected,
                    asset_id,
                    "near-duplicate figure/table group already selected",
                )
                continue
            selected.append(_selection_record(asset, role, reason, score))
            kept += 1

    protected_method_candidates = _protected_anchor_ids(by_id, roles=("method", "fallback", "evidence"))
    protected_table_candidates = _protected_anchor_ids(by_id, roles=("table", "evidence"))
    method_candidates = (
        protected_method_candidates
        + list(recommended_figures.get("method") or [])
        + _asset_ids_matching(by_id, ("framework", "pipeline", "instructional", "annotation", "hierarchical", "task"))
    )
    mechanism_candidates = (
        list(recommended_figures.get("method") or [])
        + _asset_ids_matching(by_id, ("hierarchical", "planning", "annotation", "framework", "pipeline"))
    )
    choose(
        "hero_method",
        method_candidates,
        "primary architecture/method visual for the poster's central mechanism",
        max_count=1,
    )
    choose(
        "key_mechanism",
        mechanism_candidates,
        "secondary method visual that explains the enabling mechanism",
        max_count=1,
    )
    choose(
        "main_evidence",
        protected_table_candidates + list(recommended_figures.get("evidence") or []),
        "main quantitative evidence or benchmark figure",
        max_count=2,
    )
    choose(
        "benchmark_table",
        protected_table_candidates + list(recommended_figures.get("table") or []),
        "legible benchmark or comparison table",
        max_count=2,
    )
    choose(
        "qualitative_evidence",
        list(recommended_figures.get("qualitative") or []),
        "qualitative examples that make the paper claim concrete",
        max_count=1,
    )
    choose(
        "analysis_or_systems",
        _asset_ids_matching(by_id, ("analysis", "ablation", "data", "infra", "training", "scaling")),
        "supporting analysis, data, or infrastructure evidence",
        max_count=2,
    )
    choose(
        "supporting_visual",
        list(recommended_figures.get("fallback") or []) + [str(a.get("asset_id")) for a in assets],
        "high-scoring source-backed visual to fill the remaining evidence grid",
        max_count=max(0, target_count - len(selected)),
    )

    regular_selected_count = len(selected)
    shortfall = max(0, capacity["minimum_count"] - regular_selected_count)
    if shortfall:
        unmatched_ids = _rank_asset_ids(
            [str(asset.get("asset_id") or "") for asset in assets],
            by_id,
            score_by_id,
            prefer_wide=prefer_wide_assets,
        )
        for asset_id in unmatched_ids:
            if len(selected) - regular_selected_count >= min(shortfall, 2):
                break
            if _already_selected(selected, asset_id):
                continue
            asset = by_id.get(asset_id)
            score = score_by_id.get(asset_id) or {}
            if not asset or _visual_selection_tier(asset, score) != "reserve_unmatched":
                continue
            selected.append(_selection_record(
                asset,
                "shortfall_secondary",
                "clean unmatched source image used only to fill a source-visual shortfall",
                score,
            ))

    selected_ids = {str(item.get("asset_id") or "") for item in selected}
    rejected_ids = {str(item.get("asset_id") or "") for item in rejected if str(item.get("asset_id") or "").strip()}
    reserve: list[dict[str, Any]] = []
    reserve_candidates = _rank_asset_ids(
        [str(asset.get("asset_id") or "") for asset in assets],
        by_id,
        score_by_id,
        prefer_wide=prefer_wide_assets,
    )
    for asset_id in reserve_candidates:
        if (
            asset_id in selected_ids
            or asset_id in rejected_ids
            or len(reserve) >= capacity["reserve_count"]
        ):
            continue
        asset = by_id.get(asset_id)
        score = score_by_id.get(asset_id) or {}
        if (
            not asset
            or _planner_reject_reasons(asset, score)
            or _is_low_information(asset, score, by_id=by_id, score_by_id=score_by_id)
        ):
            continue
        reserve.append(_selection_record(
            asset,
            "reserve",
            "reserve source asset: usable if a primary/secondary asset does not fit or fails rendering",
            score,
        ))

    reserve_ids = {str(item.get("asset_id") or "") for item in reserve}
    for asset in assets:
        asset_id = str(asset.get("asset_id") or "")
        if asset_id and asset_id not in selected_ids and asset_id not in reserve_ids and asset_id not in rejected_ids and len(rejected) < 24:
            rejected.append({"asset_id": asset_id, "reason": _rejection_reason(asset, score_by_id.get(asset_id) or {})})

    central_thesis = _central_thesis(manifest or {}, recommended_text_units or {})
    panel_jobs = _panel_jobs(selected)
    primary_assets = selected[:min(primary_count, regular_selected_count)]
    secondary_assets = selected[len(primary_assets):]
    return {
        "kind": "paper_visual_storyboard",
        "version": 1,
        "central_thesis": central_thesis,
        "storyline": [
            "problem",
            "central_thesis",
            "method_mechanism",
            "main_evidence",
            "supporting_analysis",
            "takeaway",
        ],
        "target_visual_count": target_count,
        "primary_asset_count": len(primary_assets),
        "secondary_asset_count": len(secondary_assets),
        "selected_assets": selected,
        "primary_assets": primary_assets,
        "secondary_assets": secondary_assets,
        "reserve_assets": reserve,
        "rejected_assets": rejected,
        "panel_jobs": panel_jobs,
        "selection_policy": {
            "source_backed_assets_only": True,
            "prefer_captioned_high_resolution_assets": True,
            "require_method_and_evidence": True,
            "avoid_low_information_or_duplicate_assets": True,
            "canvas_capacity_aware": True,
            "primary_assets_are_mandatory": True,
            "secondary_assets_are_optional": True,
            "reserve_assets_are_replacements_only": True,
            "designer_eligible_assets_only": True,
            "severe_crop_flags_block_selection": sorted(_SEVERE_CROP_CURATION_FLAGS),
        },
        "metrics": {
            "asset_count": len(assets),
            "designer_eligible_asset_count": sum(
                1 for asset in assets
                if _is_designer_eligible(asset, score_by_id.get(str(asset.get("asset_id") or "")) or {})
            ),
            "planner_visible_asset_count": sum(
                1 for asset in assets
                if _is_planner_visible(asset, score_by_id.get(str(asset.get("asset_id") or "")) or {})
            ),
            "severe_crop_rejected_asset_count": sum(
                1 for asset in assets
                if _severe_crop_flags(asset, score_by_id.get(str(asset.get("asset_id") or "")) or {})
            ),
            "selected_asset_count": len(selected),
            "primary_asset_count": len(primary_assets),
            "secondary_asset_count": len(secondary_assets),
            "reserve_asset_count": len(reserve),
            "capacity": capacity,
            "method_selected": any(str(item.get("story_role") or "").endswith("method") for item in selected),
            "evidence_selected": any("evidence" in str(item.get("story_role") or "") for item in selected),
        },
    }


def _rank_asset_ids(
    candidate_ids: list[str],
    by_id: dict[str, dict[str, Any]],
    score_by_id: dict[str, dict[str, Any]],
    *,
    prefer_wide: bool = False,
) -> list[str]:
    ids = []
    for raw in candidate_ids:
        asset_id = str(raw or "").strip()
        if asset_id and asset_id in by_id and asset_id not in ids:
            ids.append(asset_id)
    return sorted(
        ids,
        key=lambda asset_id: _asset_rank_key(
            by_id[asset_id],
            score_by_id.get(asset_id) or {},
            prefer_wide=prefer_wide,
        ),
        reverse=True,
    )


def _is_audit_only_source_asset(asset: dict[str, Any]) -> bool:
    return str(asset.get("kind") or asset.get("extract_strategy") or "").lower() == "source_table_crop_candidate"


def _merged_curation_flags(asset: dict[str, Any], score: dict[str, Any] | None = None) -> list[str]:
    out: list[str] = []
    for source in (asset, score or {}):
        if not isinstance(source, dict):
            continue
        for key in ("crop_quality_flags", "curation_flags", "severe_crop_flags"):
            for raw in list(source.get(key) or []):
                flag = str(raw or "").strip()
                if flag and flag not in out:
                    out.append(flag)
        material = source.get("material_quality")
        if isinstance(material, dict):
            for raw in list(material.get("warnings") or []):
                flag = str(raw or "").strip()
                if flag and flag not in out:
                    out.append(flag)
    return out


def _severe_crop_flags(asset: dict[str, Any], score: dict[str, Any] | None = None) -> list[str]:
    score = score or {}
    asset_id = str(asset.get("asset_id") or score.get("layer_id") or "")
    return list(classify_source_visual(asset_id, asset, score).get("severe_crop_flags") or [])


def _explicit_false(key: str, *sources: dict[str, Any]) -> bool:
    return any(isinstance(source, dict) and source.get(key) is False for source in sources)


def _designer_reject_reasons(asset: dict[str, Any], score: dict[str, Any] | None = None) -> list[str]:
    score = score or {}
    asset_id = str(asset.get("asset_id") or score.get("layer_id") or "")
    return list(classify_source_visual(asset_id, asset, score).get("designer_reject_reasons") or [])


def _planner_reject_reasons(asset: dict[str, Any], score: dict[str, Any] | None = None) -> list[str]:
    score = score or {}
    asset_id = str(asset.get("asset_id") or score.get("layer_id") or "")
    return list(classify_source_visual(asset_id, asset, score).get("planner_reject_reasons") or [])


def _visual_selection_tier(asset: dict[str, Any], score: dict[str, Any] | None = None) -> str:
    score = score or {}
    asset_id = str(asset.get("asset_id") or score.get("layer_id") or "")
    return str(classify_source_visual(asset_id, asset, score).get("visual_selection_tier") or "rejected")


def _is_designer_eligible(asset: dict[str, Any], score: dict[str, Any] | None = None) -> bool:
    return not _designer_reject_reasons(asset, score)


def _is_planner_visible(asset: dict[str, Any], score: dict[str, Any] | None = None) -> bool:
    return not _planner_reject_reasons(asset, score)


def _unique_strings(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if item and item not in out:
            out.append(item)
    return out


def _append_rejected(rejected: list[dict[str, Any]], asset_id: str, reason: str) -> None:
    if not asset_id:
        return
    if any(str(item.get("asset_id") or "") == asset_id for item in rejected):
        return
    rejected.append({"asset_id": asset_id, "reason": reason})


def _is_uncaptioned_object_level_crop(asset: dict[str, Any], score: dict[str, Any] | None = None) -> bool:
    score = score or {}
    kind = str(asset.get("kind") or score.get("kind") or "").lower()
    asset_id = str(asset.get("asset_id") or score.get("layer_id") or "")
    if kind == "table" or asset_id.startswith("ingest_table_"):
        return False
    strategy = str(asset.get("extract_strategy") or score.get("extract_strategy") or "").lower()
    if strategy not in {"raster", "vector", "sub_panel", "embedded"}:
        return False
    if str(
        asset.get("caption_full")
        or asset.get("caption_short")
        or score.get("caption_short")
        or ""
    ).strip():
        return False
    if bool(asset.get("protected_anchor") or score.get("protected_anchor") or asset.get("captioned_source_group") or score.get("captioned_source_group")):
        return False
    return not bool(
        str(asset.get("source_group_label") or score.get("source_group_label") or "").strip()
        or str(asset.get("source_group_caption") or score.get("source_group_caption") or "").strip()
        or str(asset.get("anchor_label") or score.get("anchor_label") or "").strip()
    )


def _has_captioned_source_group(asset: dict[str, Any], score: dict[str, Any] | None = None) -> bool:
    score = score or {}
    return bool(
        asset.get("captioned_source_group")
        or score.get("captioned_source_group")
        or asset.get("source_group_label")
        or score.get("source_group_label")
        or asset.get("source_group_caption")
        or score.get("source_group_caption")
    )


def _asset_rank_key(
    asset: dict[str, Any],
    score: dict[str, Any],
    *,
    prefer_wide: bool = False,
) -> tuple[int, ...]:
    role = str(score.get("visual_role") or asset.get("visual_role") or "fallback").lower()
    visual_score = _safe_int(score.get("visual_score") or asset.get("visual_score"), 0)
    w = _safe_int(asset.get("output_width_px"), 0)
    h = _safe_int(asset.get("output_height_px"), 0)
    min_side = min(w, h)
    page = _safe_int(asset.get("source_page"), 999)
    source_value_gate = 0 if _looks_like_low_value_appendix_example(asset, score) else 1
    aspect = w / float(h) if w > 0 and h > 0 else 0.0
    wide_fit_gate = 1
    if prefer_wide and not (1.35 <= aspect <= 5.2):
        wide_fit_gate = 0
    clean_group_rank = _clean_captioned_group_rank(asset, score)
    weak_crop_gate = 0 if _has_selected_blocking_weak_crop(asset, score) else 1
    material = asset.get("material_quality") if isinstance(asset.get("material_quality"), dict) else {}
    material_score = _safe_int(round(_safe_float(material.get("material_score"), 0.0) * 100), 0)
    source_role_gate = 1 if role in {"method", "table", "evidence"} else 0
    early_page_score = max(0, 12 - page) if page < 999 else 0
    return (
        1 if _is_planner_visible(asset, score) else 0,
        source_value_gate,
        weak_crop_gate,
        source_role_gate,
        wide_fit_gate,
        clean_group_rank,
        _ROLE_PRIORITY.get(role, 1),
        visual_score,
        min(min_side, 2400),
        early_page_score,
        -page,
        material_score,
    )


def _selection_record(
    asset: dict[str, Any],
    role: str,
    reason: str,
    score: dict[str, Any] | None = None,
) -> dict[str, Any]:
    score = score or {}
    eligibility = classify_source_visual(
        str(asset.get("asset_id") or score.get("layer_id") or ""),
        asset,
        score,
    )
    flags = _merged_curation_flags(asset, score)
    severe_flags = _severe_crop_flags(asset, score)
    return {
        "asset_id": asset.get("asset_id"),
        "kind": asset.get("kind"),
        "story_role": role,
        "reason": reason,
        "designer_eligible": eligibility["designer_eligible"],
        "planner_visible": eligibility["planner_visible"],
        "planner_eligible": eligibility["planner_eligible"],
        "planner_reject_reasons": eligibility["planner_reject_reasons"],
        "visual_selection_tier": eligibility["visual_selection_tier"],
        "eligibility_policy_version": eligibility["eligibility_policy_version"],
        "unmatched_caption": eligibility["unmatched_caption"],
        "replacement_only": eligibility["visual_selection_tier"] == "reserve_unmatched",
        "shortfall_only": eligibility["visual_selection_tier"] == "reserve_unmatched",
        "severe_crop_flags": severe_flags,
        "source_page": asset.get("source_page"),
        "source_bbox_pdf_points": asset.get("source_bbox_pdf_points"),
        "source_pdf": asset.get("source_pdf"),
        "source_pdf_sha256": asset.get("source_pdf_sha256"),
        "source_image_xref": asset.get("source_image_xref"),
        "output_file": asset.get("output_file"),
        "output_sha256": asset.get("output_sha256"),
        "caption_short": asset.get("caption_short"),
        "caption_full": asset.get("caption_full"),
        "visual_role": asset.get("visual_role"),
        "visual_score": asset.get("visual_score"),
        "protected_anchor": bool(asset.get("protected_anchor")),
        "anchor_kind": asset.get("anchor_kind"),
        "anchor_label": asset.get("anchor_label"),
        "anchor_reason": asset.get("anchor_reason"),
        "captioned_source_group": bool(asset.get("captioned_source_group")),
        "source_group_id": asset.get("source_group_id"),
        "source_group_kind": asset.get("source_group_kind"),
        "source_group_label": asset.get("source_group_label"),
        "source_group_caption": asset.get("source_group_caption"),
        "source_group_source": asset.get("source_group_source"),
        "table_parse_status": asset.get("table_parse_status"),
        "extract_strategy": asset.get("extract_strategy"),
        "parent_asset_id": asset.get("parent_asset_id"),
        "curation_flags": flags,
        "output_width_px": asset.get("output_width_px"),
        "output_height_px": asset.get("output_height_px"),
    }


def _source_asset_capacity(canvas_plan: dict[str, Any] | None, asset_count: int) -> dict[str, int]:
    budget = canvas_plan.get("density_budget") if isinstance(canvas_plan, dict) else {}
    canvas = canvas_plan.get("canvas") if isinstance(canvas_plan, dict) and isinstance(canvas_plan.get("canvas"), dict) else {}
    preset_id = str(canvas_plan.get("preset_id") or "") if isinstance(canvas_plan, dict) else ""
    w = _safe_int(canvas.get("w_px") or canvas.get("w"), 0)
    h = _safe_int(canvas.get("h_px") or canvas.get("h"), 0)
    ratio = (w / float(h)) if w > 0 and h > 0 else 0.0
    if preset_id == "cvpr-landscape" or (ratio >= 1.75 and h <= 1700):
        target = 8
        primary = 6
        minimum = 5
        reserve = 4
    elif h >= 2200 or (w * h) >= 7_000_000:
        target = 10
        primary = 7
        minimum = 6
        reserve = 5
    else:
        target = _safe_int((budget or {}).get("target_visuals_max"), 8)
        primary = _safe_int((budget or {}).get("target_visuals_min"), min(6, target))
        minimum = max(1, min(primary, _safe_int((budget or {}).get("target_visuals_min"), primary)))
        reserve = 4
    max_visuals = _safe_int((budget or {}).get("max_visuals"), target)
    if max_visuals > 0:
        target = min(target, max_visuals)
    target = max(1, min(target, max(1, asset_count)))
    primary = max(1, min(primary, target))
    minimum = max(1, min(minimum, primary, target))
    return {
        "target_count": target,
        "primary_count": primary,
        "minimum_count": minimum,
        "reserve_count": max(0, min(reserve, max(0, asset_count - target))),
    }


def _already_selected(selected: list[dict[str, Any]], asset_id: str) -> bool:
    return any(str(item.get("asset_id") or "") == asset_id for item in selected)


def _duplicates_visual_group(selected: list[dict[str, Any]], asset: dict[str, Any]) -> bool:
    return _duplicate_visual_group_index(selected, asset) is not None


def _duplicate_visual_group_index(selected: list[dict[str, Any]], asset: dict[str, Any]) -> int | None:
    for idx, item in enumerate(selected):
        if _is_duplicate_visual(item, asset):
            return idx
    return None


def _is_duplicate_visual(a: dict[str, Any], b: dict[str, Any]) -> bool:
    keys_a = _visual_group_keys(a)
    keys_b = _visual_group_keys(b)
    if keys_a and keys_b and keys_a & keys_b:
        return True
    source_keys_a = _source_duplicate_keys(a)
    source_keys_b = _source_duplicate_keys(b)
    if source_keys_a and source_keys_b and source_keys_a & source_keys_b:
        return True
    if _perceptual_hashes_match(a, b):
        return True
    return _near_duplicate_visual_crop(a, b)


def _visual_group_key(asset: dict[str, Any]) -> str:
    keys = sorted(_visual_group_keys(asset))
    if keys:
        return keys[0]
    page = _safe_int(asset.get("source_page"), -1)
    if page >= 0:
        bbox = asset.get("source_bbox_pdf_points")
        if isinstance(bbox, list) and len(bbox) >= 4:
            try:
                x0, y0, x1, y1 = [float(v) for v in bbox[:4]]
                return (
                    f"page:{page}:bbox:"
                    f"{round(x0 / 40)}:{round(y0 / 40)}:{round(x1 / 40)}:{round(y1 / 40)}"
                )
            except (TypeError, ValueError):
                pass
        return f"page:{page}"
    return ""


def _visual_group_keys(asset: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    source_group_id = str(asset.get("source_group_id") or "").strip()
    if source_group_id:
        keys.add("source_group:" + source_group_id.lower())
    source_group_label = _normalized_identity_text(asset.get("source_group_label"))
    if source_group_label:
        keys.add("source_group_label:" + source_group_label[:120])
    source_group_caption = _normalized_identity_text(asset.get("source_group_caption"))
    if source_group_caption:
        keys.add("source_group_caption:" + source_group_caption[:160])
    anchor_kind = _normalized_identity_text(asset.get("anchor_kind"))
    anchor_label = _normalized_identity_text(asset.get("anchor_label"))
    if anchor_label:
        keys.add(f"anchor:{anchor_kind or 'visual'}:{anchor_label}")
    text = " ".join(
        str(asset.get(k) or "")
        for k in ("caption_full", "caption_short", "source_group_caption", "anchor_kind", "anchor_label")
    ).lower()
    text = re.sub(r"\s+", " ", text).strip()
    keys.update(_figure_table_ref_keys(text))
    return keys


def _figure_table_ref_keys(text: str) -> set[str]:
    keys: set[str] = set()
    for match in re.finditer(r"\b(fig(?:ure)?|table)\.?\s*([0-9]+[a-z]?)\b", text.lower()):
        prefix = "table" if match.group(1) == "table" else "figure"
        keys.add(f"{prefix}:{match.group(2)}")
    return keys


def _source_duplicate_keys(asset: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for field in _SOURCE_HASH_FIELDS:
        value = str(asset.get(field) or "").strip().lower()
        if len(value) >= 12:
            keys.add(f"{field}:{value}")
            keys.add("source_hash:" + value)
    xref = str(asset.get("source_image_xref") or "").strip()
    if xref:
        page = _safe_int(asset.get("source_page"), -1)
        source_doc = str(asset.get("source_pdf_sha256") or asset.get("source_pdf") or "").strip().lower()
        keys.add(f"source_image_xref:{source_doc}:{page}:{xref}")
    return keys


def _perceptual_hashes_match(a: dict[str, Any], b: dict[str, Any]) -> bool:
    hashes_a = _perceptual_hash_values(a)
    hashes_b = _perceptual_hash_values(b)
    for value_a in hashes_a:
        for value_b in hashes_b:
            if value_a == value_b:
                return True
            if len(value_a) == len(value_b) and len(value_a) >= 16:
                distance = _hex_hamming_distance(value_a, value_b)
                if distance is not None and distance <= 6:
                    return True
    return False


def _perceptual_hash_values(asset: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for field in _PERCEPTUAL_HASH_FIELDS:
        raw = str(asset.get(field) or "").strip().lower()
        value = re.sub(r"[^0-9a-f]", "", raw)
        if len(value) >= 8 and value not in out:
            out.append(value)
    return out


def _hex_hamming_distance(a: str, b: str) -> int | None:
    try:
        return (int(a, 16) ^ int(b, 16)).bit_count()
    except ValueError:
        return None


def _near_duplicate_visual_crop(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if not _same_source_document(a, b):
        return False
    page_a = _safe_int(a.get("source_page"), -1)
    page_b = _safe_int(b.get("source_page"), -2)
    if page_a < 0 or page_a != page_b:
        return False
    bbox_a = _bbox_tuple(a.get("source_bbox_pdf_points"))
    bbox_b = _bbox_tuple(b.get("source_bbox_pdf_points"))
    if bbox_a is None or bbox_b is None:
        return False
    ax0, ay0, ax1, ay1 = bbox_a
    bx0, by0, bx1, by1 = bbox_b
    aw = max(0.0, ax1 - ax0)
    ah = max(0.0, ay1 - ay0)
    bw = max(0.0, bx1 - bx0)
    bh = max(0.0, by1 - by0)
    if aw <= 0 or ah <= 0 or bw <= 0 or bh <= 0:
        return False
    area_a = aw * ah
    area_b = bw * bh
    intersect_w = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    intersect_h = max(0.0, min(ay1, by1) - max(ay0, by0))
    intersect_area = intersect_w * intersect_h
    if intersect_area > 0:
        union_area = max(1.0, area_a + area_b - intersect_area)
        iou = intersect_area / union_area
        containment = intersect_area / max(1.0, min(area_a, area_b))
        if iou >= 0.82:
            return True
        if containment >= 0.90 and (
            _share_visual_identity(a, b)
            or _is_subpanel_or_object_crop(a)
            or _is_subpanel_or_object_crop(b)
        ):
            return True
        if _share_visual_identity(a, b) and (iou >= 0.45 or containment >= 0.72):
            return True
    vertical_overlap = max(0.0, min(ay1, by1) - max(ay0, by0)) / max(1.0, min(ah, bh))
    height_ratio = min(ah, bh) / max(ah, bh)
    width_ratio = min(aw, bw) / max(aw, bw)
    if vertical_overlap < 0.72 or height_ratio < 0.55 or width_ratio < 0.45:
        return False
    horizontal_overlap = max(0.0, min(ax1, bx1) - max(ax0, bx0)) / max(1.0, min(aw, bw))
    horizontal_gap = max(0.0, max(ax0, bx0) - min(ax1, bx1))
    adjacent = horizontal_overlap >= 0.35 or horizontal_gap <= max(18.0, min(aw, bw) * 0.20)
    if not adjacent:
        return False
    if _share_visual_identity(a, b):
        return True
    weak_flags = _WEAK_VISUAL_CURATION_FLAGS | frozenset({"low_information_visual", "low_value_example_crop"})
    if not (
        set(_merged_curation_flags(a)) & weak_flags
        or set(_merged_curation_flags(b)) & weak_flags
        or _is_subpanel_or_object_crop(a)
        or _is_subpanel_or_object_crop(b)
    ):
        return False
    return True


def _same_source_document(a: dict[str, Any], b: dict[str, Any]) -> bool:
    for field in ("source_pdf_sha256", "source_pdf"):
        value_a = str(a.get(field) or "").strip()
        value_b = str(b.get(field) or "").strip()
        if value_a and value_b and value_a != value_b:
            return False
    return True


def _share_visual_identity(a: dict[str, Any], b: dict[str, Any]) -> bool:
    keys_a = _visual_group_keys(a)
    keys_b = _visual_group_keys(b)
    if keys_a and keys_b and keys_a & keys_b:
        return True
    source_keys_a = _source_duplicate_keys(a)
    source_keys_b = _source_duplicate_keys(b)
    if source_keys_a and source_keys_b and source_keys_a & source_keys_b:
        return True
    return _perceptual_hashes_match(a, b)


def _should_replace_selected_duplicate(
    existing: dict[str, Any],
    asset: dict[str, Any],
    score: dict[str, Any] | None = None,
) -> bool:
    score = score or {}
    if not _is_whole_captioned_group(asset, score):
        return False
    if _is_whole_captioned_group(existing):
        return _duplicate_quality_rank(asset, score) > _duplicate_quality_rank(existing, {})
    if not (
        _is_subpanel_or_object_crop(existing)
        or _has_selected_blocking_weak_crop(existing, {})
        or _clean_captioned_group_rank(asset, score) > _clean_captioned_group_rank(existing, {})
    ):
        return False
    return _duplicate_quality_rank(asset, score) > _duplicate_quality_rank(existing, {})


def _duplicate_quality_rank(asset: dict[str, Any], score: dict[str, Any] | None = None) -> tuple[int, ...]:
    score = score or {}
    role = str(score.get("visual_role") or asset.get("visual_role") or "fallback").lower()
    visual_score = _safe_int(score.get("visual_score") or asset.get("visual_score"), 0)
    min_side = min(
        _safe_int(asset.get("output_width_px"), 0),
        _safe_int(asset.get("output_height_px"), 0),
    )
    return (
        1 if _is_planner_visible(asset, score) else 0,
        0 if _severe_crop_flags(asset, score) else 1,
        0 if _has_selected_blocking_weak_crop(asset, score) else 1,
        _clean_captioned_group_rank(asset, score),
        0 if _is_subpanel_or_object_crop(asset, score) else 1,
        1 if role in {"method", "table", "evidence"} else 0,
        _ROLE_PRIORITY.get(role, 1),
        visual_score,
        min(min_side, 2400),
    )


def _clean_captioned_group_rank(asset: dict[str, Any], score: dict[str, Any] | None = None) -> int:
    score = score or {}
    rank = 0
    if _has_captioned_source_group(asset, score):
        rank += 2
    if bool(asset.get("protected_anchor") or score.get("protected_anchor")):
        rank += 1
    if _is_whole_captioned_group(asset, score):
        rank += 2
    if _is_subpanel_or_object_crop(asset, score):
        rank -= 1
    if _has_selected_blocking_weak_crop(asset, score) or _severe_crop_flags(asset, score):
        rank -= 1
    return max(0, rank)


def _is_whole_captioned_group(asset: dict[str, Any], score: dict[str, Any] | None = None) -> bool:
    return _has_captioned_source_group(asset, score) and not _is_subpanel_or_object_crop(asset, score)


def _is_subpanel_or_object_crop(asset: dict[str, Any], score: dict[str, Any] | None = None) -> bool:
    score = score or {}
    strategy = str(asset.get("extract_strategy") or score.get("extract_strategy") or "").lower()
    if strategy in {"sub_panel", "subpanel", "embedded", "object", "component"}:
        return True
    text = " ".join(
        str(source.get(key) or "")
        for source in (asset, score)
        for key in ("kind", "source_group_kind", "anchor_reason", "curation_reason")
    ).lower()
    if any(token in text for token in ("subpanel", "sub-panel", "object crop", "component crop")):
        return True
    parent_asset_id = str(asset.get("parent_asset_id") or score.get("parent_asset_id") or "").strip()
    return bool(parent_asset_id)


def _has_selected_blocking_weak_crop(asset: dict[str, Any], score: dict[str, Any] | None = None) -> bool:
    score = score or {}
    flags = set(_merged_curation_flags(asset, score))
    if "low_information_visual" in flags:
        return True
    if "low_detail_visual_content" in flags and _has_low_caption_or_detail(asset, score, flags):
        return True
    if flags & {"mostly_white_visual", "high_edge_whitespace"}:
        if _is_high_confidence_captioned_source(asset, score) and not (flags & {"low_detail_visual_content", "no_caption"}):
            return False
        return _has_low_caption_or_detail(asset, score, flags)
    return False


def _is_high_confidence_captioned_source(asset: dict[str, Any], score: dict[str, Any] | None = None) -> bool:
    score = score or {}
    visual_score = _safe_int(score.get("visual_score") or asset.get("visual_score"), 0)
    if visual_score < 72:
        return False
    if not _has_captioned_source_group(asset, score) and not bool(asset.get("protected_anchor") or score.get("protected_anchor")):
        return False
    return bool(_caption_text(asset, score))


def _has_low_caption_or_detail(
    asset: dict[str, Any],
    score: dict[str, Any] | None = None,
    flags: set[str] | None = None,
) -> bool:
    score = score or {}
    flags = flags or set(_merged_curation_flags(asset, score))
    if "low_caption_confidence" in flags and _is_high_confidence_captioned_source(asset, score):
        return False
    if flags & {"low_caption_confidence", "low_detail_visual_content", "no_caption"}:
        return True
    caption = _caption_text(asset, score)
    if not caption and not _has_captioned_source_group(asset, score):
        return True
    return bool(caption and len(caption.split()) < 3 and not _has_captioned_source_group(asset, score))


def _caption_text(asset: dict[str, Any], score: dict[str, Any] | None = None) -> str:
    score = score or {}
    text = " ".join(
        str(source.get(key) or "")
        for source in (asset, score)
        for key in ("caption_full", "caption", "title", "caption_short", "source_group_caption")
    )
    return re.sub(r"\s+", " ", text).strip()


def _normalized_identity_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").lower()).strip()


def _is_table_asset(asset: dict[str, Any]) -> bool:
    asset_id = str(asset.get("asset_id") or asset.get("layer_id") or "")
    return str(asset.get("kind") or "").lower() == "table" or asset_id.startswith("ingest_table_")


def _bbox_tuple(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 4:
        return None
    try:
        x0, y0, x1, y1 = [float(v) for v in value[:4]]
    except (TypeError, ValueError):
        return None
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def _is_low_information(
    asset: dict[str, Any],
    score: dict[str, Any],
    *,
    by_id: dict[str, dict[str, Any]] | None = None,
    score_by_id: dict[str, dict[str, Any]] | None = None,
) -> bool:
    if not _is_planner_visible(asset, score):
        return True
    if _is_audit_only_source_asset(asset):
        return True
    flags = set(_merged_curation_flags(asset, score))
    if _severe_crop_flags(asset, score):
        return True
    if flags & {"table_fragment_crop", "table_without_structure"}:
        return True
    if "low_information_visual" in flags:
        return True
    if _has_selected_blocking_weak_crop(asset, score):
        return True
    low_value_example = "low_value_example_crop" in flags or _looks_like_low_value_appendix_example(asset, score)
    reliable_captioned = bool(_caption_text(asset, score)) and _visual_selection_tier(asset, score) == "eligible"
    if (
        low_value_example
        and not reliable_captioned
        and _has_better_evidence_asset(asset, score, by_id=by_id, score_by_id=score_by_id)
    ):
        return True
    if _safe_int(asset.get("output_width_px"), 0) < 180 or _safe_int(asset.get("output_height_px"), 0) < 120:
        return True
    return False


def _has_better_evidence_asset(
    asset: dict[str, Any],
    score: dict[str, Any],
    *,
    by_id: dict[str, dict[str, Any]] | None = None,
    score_by_id: dict[str, dict[str, Any]] | None = None,
) -> bool:
    if not by_id:
        return False
    score_by_id = score_by_id or {}
    asset_id = str(asset.get("asset_id") or score.get("layer_id") or "").strip()
    current_rank = _evidence_quality_rank(asset, score)
    for other_id, other in by_id.items():
        if str(other_id or "") == asset_id:
            continue
        other_score = score_by_id.get(str(other_id or "")) or {}
        if not _is_stronger_evidence_candidate(other, other_score):
            continue
        if _evidence_quality_rank(other, other_score) > current_rank:
            return True
    return False


def _is_stronger_evidence_candidate(asset: dict[str, Any], score: dict[str, Any]) -> bool:
    if not _is_planner_visible(asset, score):
        return False
    if _is_audit_only_source_asset(asset) or _severe_crop_flags(asset, score):
        return False
    flags = set(_merged_curation_flags(asset, score))
    if flags & {"low_information_visual", "low_value_example_crop", "table_fragment_crop", "table_without_structure"}:
        return False
    if _has_selected_blocking_weak_crop(asset, score):
        return False
    role = str(score.get("visual_role") or asset.get("visual_role") or "").lower()
    return bool(role in {"method", "table", "evidence"} or _has_captioned_source_group(asset, score))


def _evidence_quality_rank(asset: dict[str, Any], score: dict[str, Any]) -> tuple[int, ...]:
    role = str(score.get("visual_role") or asset.get("visual_role") or "fallback").lower()
    visual_score = _safe_int(score.get("visual_score") or asset.get("visual_score"), 0)
    min_side = min(
        _safe_int(asset.get("output_width_px"), 0),
        _safe_int(asset.get("output_height_px"), 0),
    )
    return (
        1 if role in {"method", "table", "evidence"} else 0,
        _ROLE_PRIORITY.get(role, 1),
        _clean_captioned_group_rank(asset, score),
        visual_score,
        min(min_side, 2400),
    )


def _looks_like_low_value_appendix_example(asset: dict[str, Any], score: dict[str, Any]) -> bool:
    flags = set(asset.get("curation_flags") or [])
    flags.update(score.get("curation_flags") or [])
    if "low_value_example_crop" in flags:
        return True
    text = " ".join(
        str(source.get(key) or "")
        for source in (asset, score)
        for key in ("asset_id", "caption_short", "caption_full", "source_group_caption", "visual_role")
    ).lower()
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return False
    low_value_tokens = (
        "appendix example",
        "image editing example",
        "video editing example",
        "video creation example",
        "example with photoshop",
        "example with davinci",
        "example with runway",
        "creation example with",
        "qa pair",
        "scroll qa",
        "key / press action",
        "key and press action",
        "illustration of how we evaluate the key",
        "illustration of how we create",
        "prompt template",
        "instruction prompt",
    )
    if any(token in text for token in low_value_tokens):
        return True
    if (
        any(token in text for token in ("qualitative", "example", "sample", "demo", "case"))
        and not any(token in text for token in (
            "method", "architecture", "pipeline", "framework", "benchmark",
            "result", "evaluation", "ablation", "comparison", "performance",
        ))
    ):
        return True
    return bool(
        re.search(r"\bfig(?:ure)?\.?\s*(?:1[4-9]|[2-9][0-9])\b", text)
        and any(token in text for token in ("example", "qa", "prompt", "illustration of how"))
    )


def _asset_ids_matching(by_id: dict[str, dict[str, Any]], keywords: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for asset_id, asset in by_id.items():
        text = " ".join(str(asset.get(k) or "") for k in (
            "asset_id", "caption_short", "caption_full", "source_group_caption", "visual_role",
        )).lower()
        if any(keyword in text for keyword in keywords):
            out.append(asset_id)
    return out


def _protected_anchor_ids(by_id: dict[str, dict[str, Any]], *, roles: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    allowed = set(roles)
    for asset_id, asset in by_id.items():
        role = str(asset.get("visual_role") or "").lower()
        if not asset.get("protected_anchor") and not _has_captioned_source_group(asset):
            continue
        if not _is_planner_visible(asset):
            continue
        if role in allowed:
            out.append(asset_id)
    return out


def _anchor_label_number(value: Any) -> int:
    match = re.match(r"([0-9]+)", str(value or "").strip())
    if not match:
        return 999
    try:
        return int(match.group(1))
    except ValueError:
        return 999


def _rejection_reason(asset: dict[str, Any], score: dict[str, Any]) -> str:
    planner_reject_reasons = _planner_reject_reasons(asset, score)
    if planner_reject_reasons:
        return "planner-ineligible source asset: " + ", ".join(planner_reject_reasons)
    flags = set(_merged_curation_flags(asset, score))
    if "page_like_figure_crop" in flags:
        return "page-like PDF crop with body text; use sub-panels or tighter figure crops instead"
    if "high_edge_whitespace" in flags:
        return "large white margins; needs tighter crop before use as a hero or gallery visual"
    if "mostly_white_visual" in flags:
        return "mostly white or low-content visual; reject or recrop before display"
    if "low_value_example_crop" in flags or _looks_like_low_value_appendix_example(asset, score):
        return "low-value qualitative/example crop; prioritize method, table, or evidence source assets"
    if _is_low_information(asset, score):
        return "low information, low resolution, or weak source asset"
    role = str(score.get("visual_role") or asset.get("visual_role") or "")
    if role:
        return f"not needed after higher-ranked {role} assets filled the storyboard"
    return "not needed after higher-ranked source assets filled the storyboard"


def _central_thesis(
    manifest: dict[str, Any],
    recommended_text_units: dict[str, list[dict[str, Any]]],
) -> str:
    for bucket in ("takeaways", "method", "evidence"):
        for item in list(recommended_text_units.get(bucket) or []):
            text = str((item or {}).get("text") or "").strip()
            if len(text.split()) >= 5:
                return text
    title = str(manifest.get("title") or "").strip()
    return title or "A source-backed paper poster built from selected figures and tables."


def _panel_jobs(selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ids_by_role: dict[str, list[str]] = {}
    for item in selected:
        role = str(item.get("story_role") or "supporting_visual")
        asset_id = str(item.get("asset_id") or "")
        if asset_id:
            ids_by_role.setdefault(role, []).append(asset_id)
    return [
        {
            "slot_id": "title_thesis",
            "job": (
                "State paper identity only: title, authors, and "
                "school/institution/company names."
            ),
            "asset_ids": [],
        },
        {
            "slot_id": "method_visual",
            "job": "Use the hero method visual as the main explanatory anchor.",
            "asset_ids": ids_by_role.get("hero_method", []) + ids_by_role.get("key_mechanism", []),
        },
        {
            "slot_id": "evidence_grid",
            "job": "Show benchmark, table, and qualitative evidence with source captions.",
            "asset_ids": ids_by_role.get("main_evidence", [])
            + ids_by_role.get("benchmark_table", [])
            + ids_by_role.get("qualitative_evidence", []),
        },
        {
            "slot_id": "supporting_analysis",
            "job": "Use analysis, data, or infrastructure visuals to explain why the result scales.",
            "asset_ids": ids_by_role.get("analysis_or_systems", []) + ids_by_role.get("supporting_visual", []),
        },
        {
            "slot_id": "footer_takeaway",
            "job": "Close with key takeaways, source PDF hash/provenance, and optional links.",
            "asset_ids": [],
        },
    ]


def _prefers_wide_assets(canvas_plan: dict[str, Any] | None) -> bool:
    if not isinstance(canvas_plan, dict):
        return False
    canvas = canvas_plan.get("canvas") if isinstance(canvas_plan.get("canvas"), dict) else {}
    try:
        w = int(canvas.get("w_px") or 0)
        h = int(canvas.get("h_px") or 0)
    except (TypeError, ValueError):
        return False
    return bool(w >= h * 1.7 and h < 1800)


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
