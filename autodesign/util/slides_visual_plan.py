"""Deterministic visual planning for external paper-to-slides authoring."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .source_visual_eligibility import classify_source_visual


_METHOD_TERMS = frozenset({
    "architecture", "framework", "mechanism", "method", "methodology",
    "model", "pipeline", "system", "workflow",
})
_RESULT_TERMS = frozenset({
    "ablation", "analysis", "benchmark", "comparison", "evidence",
    "evaluation", "experiment", "qualitative", "result", "results", "table",
})


def build_slides_asset_catalog(
    paper_visual_provenance: dict[str, Any] | None,
    *,
    rendered_layers: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return the full paper visual catalog with recomputed eligibility."""
    provenance = paper_visual_provenance if isinstance(paper_visual_provenance, dict) else {}
    rendered = rendered_layers if isinstance(rendered_layers, dict) else {}
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in provenance.get("assets") or []:
        if not isinstance(source, dict):
            continue
        asset_id = str(source.get("asset_id") or "").strip()
        if not asset_id or asset_id in seen:
            continue
        seen.add(asset_id)
        layer = rendered.get(asset_id) if isinstance(rendered.get(asset_id), dict) else {}
        eligibility = classify_source_visual(asset_id, source, layer)
        tier = str(eligibility.get("visual_selection_tier") or "rejected")
        records.append({
            "asset_id": asset_id,
            "fingerprint": _visual_fingerprint(source, layer),
            "kind": str(source.get("kind") or layer.get("kind") or "image"),
            "caption": str(
                source.get("caption_full")
                or source.get("caption_short")
                or layer.get("caption")
                or ""
            ),
            "visual_role": str(source.get("visual_role") or layer.get("visual_role") or ""),
            "visual_score": _number(source.get("visual_score") or layer.get("visual_score")),
            "source_page": source.get("source_page") or layer.get("source_page"),
            "staged_path": _staged_path(source, layer),
            "eligibility": {
                "eligible": tier == "eligible",
                "reserve": tier == "reserve_unmatched",
                "tier": tier,
                "reject_reasons": list(eligibility.get("designer_reject_reasons") or []),
                "policy_version": eligibility.get("eligibility_policy_version"),
            },
            "provenance": _json_value(source),
            "rendered_layer": _json_value(layer),
        })
    return {
        "kind": "slides_asset_catalog",
        "version": 1,
        "source": "paper_visual_provenance",
        "assets": records,
        "metrics": {
            "asset_count": len(records),
            "eligible_asset_count": sum(
                1 for record in records if record["eligibility"]["eligible"]
            ),
            "reserve_asset_count": sum(
                1 for record in records if record["eligibility"]["reserve"]
            ),
            "rejected_asset_count": sum(
                1
                for record in records
                if not record["eligibility"]["eligible"]
                and not record["eligibility"]["reserve"]
            ),
        },
    }


def build_slides_visual_plan(
    paper_visual_provenance: dict[str, Any] | None,
    *,
    rendered_layers: dict[str, dict[str, Any]] | None = None,
    expected_slide_count: int = 18,
    color_system: dict[str, Any] | None = None,
    deck_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Recommend source visuals from the complete provenance catalog."""
    catalog = build_slides_asset_catalog(
        paper_visual_provenance,
        rendered_layers=rendered_layers,
    )
    eligible = [
        record for record in catalog["assets"]
        if record["eligibility"]["eligible"]
    ]
    reserves = [
        record for record in catalog["assets"]
        if record["eligibility"]["reserve"] and record.get("staged_path")
    ]
    ranked = _rank_with_evidence_coverage(_unique_by_fingerprint(eligible))
    slide_count = max(1, int(expected_slide_count or 18))
    substantive_slide_slots = list(range(2, slide_count)) if slide_count > 2 else []
    substantive_slide_capacity = len(substantive_slide_slots)
    unique_source_cap = (
        10
        if slide_count <= 12
        else min(18, max(10, round(slide_count * 0.7)))
    )
    unique_source_target = min(
        unique_source_cap,
        len(ranked),
        substantive_slide_capacity,
    )
    source_reuse_cap = (
        2
        if 0 < len(ranked) <= (4 if slide_count <= 12 else max(4, slide_count // 3))
        else 1
    )
    source_placement_cap = (
        10
        if slide_count <= 12
        else min(20, max(10, round(slide_count * 0.8)))
    )
    source_placement_target = min(
        substantive_slide_capacity,
        unique_source_target * source_reuse_cap,
        source_placement_cap,
    )
    visual_unit_slide_target = min(
        8 if slide_count <= 12 else max(8, round(slide_count * 0.7)),
        substantive_slide_capacity,
    )
    source_visual_slide_target = min(visual_unit_slide_target, source_placement_target)
    recommendations = ranked[:unique_source_target]
    placement_assets = [
        recommendations[index % len(recommendations)]
        for index in range(source_placement_target)
    ] if recommendations else []
    slide_slots = substantive_slide_slots[:visual_unit_slide_target]
    placements = [
        {
            "placement_id": f"source_visual_{index:02d}",
            "asset_id": record["asset_id"],
            "suggested_slide": slide_slots[(index - 1) % len(slide_slots)],
            "story_role": _story_role(record),
            "reason": _recommendation_reason(record),
            "local_interpretation_required": True,
            "reuse_policy": "different_slide_different_readout",
        }
        for index, record in enumerate(placement_assets, start=1)
    ]
    recommended_ids = [record["asset_id"] for record in recommendations]
    method_ids = [
        record["asset_id"] for record in recommendations
        if _story_role(record) == "method"
    ]
    results_ids = [
        record["asset_id"] for record in recommendations
        if _story_role(record) == "results"
    ]
    reserve_allowance = min(max(0, unique_source_cap - len(ranked)), 2)
    active_deck_plan = deck_plan if isinstance(deck_plan, dict) else {}
    talk_profile = str(
        active_deck_plan.get("talk_profile") or "standard_conference"
    )
    storyboard = _storyboard_from_deck_plan(active_deck_plan, slide_count)
    optional_reserves = sorted(
        reserves,
        key=lambda record: (
            -float(record.get("visual_score") or 0),
            int(record.get("source_page") or 10**6),
            record["asset_id"],
        ),
    )[:reserve_allowance]
    return {
        "kind": "slides_visual_plan",
        "version": 3,
        "source": "paper_visual_provenance",
        "asset_catalog_file": "slides_asset_catalog.json",
        "targets": {
            "unique_source_visual_count": unique_source_target,
            "minimum_unique_source_visual_count": unique_source_target,
            "source_visual_placement_count": source_placement_target,
            "minimum_source_visual_placement_count": source_placement_target,
            "visual_unit_slide_count": visual_unit_slide_target,
            "minimum_visual_unit_slide_count": visual_unit_slide_target,
            "source_visual_reuse_cap": source_reuse_cap,
            "minimum_substantive_word_count": 30,
            "recommended_substantive_word_range": [45, 110],
            "cover_and_closing_word_floor_exempt": True,
            "require_speaker_notes": talk_profile == "full_formal",
            "speaker_note_format": "[Sources] exact source anchors; [Talk] spoken delivery cue",
            "visual_slide_count": source_visual_slide_target,
            "minimum_visual_slide_count": source_visual_slide_target,
            "visual_placement_count": source_placement_target,
            "minimum_visual_placement_count": source_placement_target,
            "visual_slide_range": [source_visual_slide_target, visual_unit_slide_target],
            "visual_placement_range": [source_placement_target, source_placement_target],
            "source_corpus_capacity_limited": len(ranked) < unique_source_cap,
            "corpus_capacity_limited": source_placement_target < visual_unit_slide_target,
        },
        "visual_unit_contract": {
            "counted_units": [
                "eligible_original_source_visual",
                "native_html_table",
                "verifiable_equation",
                "editable_mechanism_diagram",
            ],
            "source_reuse": "never repeat an asset on one slide; each reuse needs a distinct local interpretation",
        },
        "visual_contract": {
            "direction": "formal_academic",
            "palette_policy": (
                "preserve_current_palette_metadata_use_one_restrained_accent"
                if isinstance(color_system, dict) and color_system
                else "derive_one_restrained_academic_accent"
            ),
            "canvas": "white_or_near_white",
            "ink": "near_black",
            "rules": "thin_neutral_gray",
            "surface_policy": "flat_editorial_no_cards",
            "typography": "serif_main_hierarchy_sans_small_labels",
            "navigation": "keyboard_and_hash_without_visible_controls",
        },
        "narrative_contract": {
            "assertion_led_titles": True,
            "chapter_checkpoints": talk_profile == "full_formal",
            "role_word_ranges": {
                "cover": [0, 35],
                "outline_or_checkpoint": [30, 65],
                "problem_and_context": [45, 100],
                "method_and_algorithm": [55, 140],
                "results_and_analysis": [45, 110],
                "closing": [20, 60],
            },
            "hard_minimum_substantive_words": 30,
            "anti_repetition": "thesis, mechanism, results, and takeaways each advance the narrative",
        },
        "talk_profile": talk_profile,
        "storyboard": storyboard,
        "color_system": _json_value(color_system) if isinstance(color_system, dict) else {},
        "recommended_asset_ids": recommended_ids,
        "recommended_assets": [
            {
                "asset_id": record["asset_id"],
                "kind": record["kind"],
                "caption": record["caption"],
                "visual_role": record["visual_role"],
                "staged_path": record["staged_path"],
                "story_role": _story_role(record),
            }
            for record in recommendations
        ],
        "optional_reserve_asset_ids": [record["asset_id"] for record in optional_reserves],
        "optional_reserve_assets": [
            {
                "asset_id": record["asset_id"],
                "kind": record["kind"],
                "caption": "",
                "visual_role": "supporting",
                "staged_path": record["staged_path"],
                "story_role": "supporting",
                "claim_policy": "shortfall_only_no_method_or_results_claims",
            }
            for record in optional_reserves
        ],
        "placement_recommendations": placements,
        "evidence_coverage": {
            "method": bool(method_ids),
            "results": bool(results_ids),
            "method_asset_ids": method_ids,
            "results_asset_ids": results_ids,
        },
        "catalog_summary": dict(catalog["metrics"]),
    }


def _storyboard_from_deck_plan(
    deck_plan: dict[str, Any],
    slide_count: int,
) -> list[dict[str, Any]]:
    outline = deck_plan.get("outline")
    if not isinstance(outline, list):
        return []
    storyboard: list[dict[str, Any]] = []
    for fallback_index, item in enumerate(outline[:slide_count], start=1):
        if not isinstance(item, dict):
            continue
        visual_refs = [
            str(value)
            for value in (item.get("visual_refs") or [])
            if str(value).strip()
        ]
        evidence_refs = [
            str(value)
            for value in (item.get("evidence_refs") or visual_refs)
            if str(value).strip()
        ]
        storyboard.append({
            "slide_index": int(item.get("slide_index") or fallback_index),
            "title": str(item.get("title") or ""),
            "role": str(item.get("role") or "content"),
            "chapter": str(item.get("chapter") or ""),
            "communication_job": str(item.get("communication_job") or ""),
            "assertion_title": str(item.get("assertion_title") or item.get("title") or ""),
            "scope": str(item.get("scope") or ""),
            "layout_family": str(item.get("layout_family") or ""),
            "visual_refs": visual_refs,
            "evidence_refs": evidence_refs,
            "speaker_note_intent": str(
                item.get("speaker_note_intent")
                or item.get("speaker_note")
                or ""
            ),
        })
    return storyboard


def _rank_with_evidence_coverage(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(
        records,
        key=lambda record: (
            -float(record.get("visual_score") or 0),
            int(record.get("source_page") or 10**6),
            record["asset_id"],
        ),
    )
    selected: list[dict[str, Any]] = []
    for required_role in ("method", "results"):
        match = next((record for record in ordered if _story_role(record) == required_role), None)
        if match is not None:
            selected.append(match)
    selected_ids = {record["asset_id"] for record in selected}
    selected.extend(record for record in ordered if record["asset_id"] not in selected_ids)
    return selected


def _unique_by_fingerprint(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        fingerprint = str(record.get("fingerprint") or record.get("asset_id") or "")
        if not fingerprint or fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append(record)
    return unique


def _story_role(record: dict[str, Any]) -> str:
    text = " ".join((
        str(record.get("visual_role") or ""),
        str(record.get("caption") or ""),
        str(record.get("kind") or ""),
    )).lower()
    tokens = set(text.replace("/", " ").replace("-", " ").split())
    if tokens & _METHOD_TERMS:
        return "method"
    if tokens & _RESULT_TERMS:
        return "results"
    return "supporting"


def _recommendation_reason(record: dict[str, Any]) -> str:
    role = _story_role(record)
    if role == "method":
        return "Explain the paper method with an original source visual."
    if role == "results":
        return "Ground a result or evaluation claim in original paper evidence."
    return "Provide source-backed visual context for the paper narrative."


def _staged_path(source: dict[str, Any], layer: dict[str, Any]) -> str:
    src_path = str(layer.get("src_path") or "").strip()
    if src_path:
        return f"layers/{Path(src_path).name}"
    output_file = str(source.get("output_file") or "").strip().replace("\\", "/")
    if output_file:
        return output_file.lstrip("./")
    return ""


def _visual_fingerprint(source: dict[str, Any], layer: dict[str, Any]) -> str:
    for candidate in (
        source.get("output_sha256"),
        source.get("sha256"),
        layer.get("sha256"),
    ):
        value = str(candidate or "").strip()
        if value:
            return f"sha256:{value}"
    return f"asset:{str(source.get('asset_id') or '').strip()}"


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
