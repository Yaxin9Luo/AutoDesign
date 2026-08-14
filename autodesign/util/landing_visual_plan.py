"""Deterministic visual planning for paper project landing pages."""

from __future__ import annotations

from collections import defaultdict
import re
from typing import Any

from .source_visual_eligibility import classify_source_visual


LANDING_VISUAL_ROLES = (
    "hero",
    "method",
    "results",
    "data",
    "qualitative",
    "analysis",
)

_ROLE_TERMS = {
    "hero": ("hero", "overview", "teaser", "summary", "concept", "motivation"),
    "method": ("method", "framework", "pipeline", "architecture", "workflow", "system"),
    "results": ("result", "evidence", "benchmark", "performance", "ablation", "comparison"),
    "data": ("table", "data", "dataset", "statistics", "distribution", "measurement"),
    "qualitative": ("qualitative", "example", "demo", "gallery", "sample", "case study"),
    "analysis": ("analysis", "limitation", "discussion", "error", "failure", "sensitivity"),
}


def eligible_landing_assets(
    provenance: dict[str, Any] | None,
    *,
    rendered_layers: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return every designer-eligible asset from the full ingest provenance."""
    if not isinstance(provenance, dict):
        return []
    rendered = rendered_layers if isinstance(rendered_layers, dict) else {}
    eligible: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw_asset in provenance.get("assets") or []:
        if not isinstance(raw_asset, dict):
            continue
        asset_id = str(raw_asset.get("asset_id") or "").strip()
        if not asset_id or asset_id in seen_ids:
            continue
        layer = rendered.get(asset_id) if isinstance(rendered.get(asset_id), dict) else {}
        classification = classify_source_visual(asset_id, raw_asset, layer)
        if not classification.get("designer_eligible"):
            continue
        seen_ids.add(asset_id)
        asset = dict(raw_asset)
        asset.update(classification)
        asset["asset_id"] = asset_id
        asset["landing_role"] = _landing_role(asset)
        eligible.append(asset)
    return eligible


def build_landing_asset_catalog(
    provenance: dict[str, Any] | None,
    *,
    rendered_layers: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the full author-visible catalog without poster selection filters."""
    assets = eligible_landing_assets(provenance, rendered_layers=rendered_layers)
    formal = [asset for asset in assets if asset.get("visual_selection_tier") == "eligible"]
    reserves = [
        asset for asset in assets if asset.get("visual_selection_tier") == "reserve_unmatched"
    ]
    return {
        "kind": "landing_asset_catalog",
        "version": 1,
        "source": "paper_visual_provenance",
        "eligible_asset_count": len(formal),
        "reserve_asset_count": len(reserves),
        "selectable_asset_count": len(assets),
        "assets": assets,
    }


def build_landing_visual_plan(
    provenance: dict[str, Any] | None,
    *,
    rendered_layers: dict[str, dict[str, Any]] | None = None,
    current_color_system: dict[str, Any] | None = None,
    brief: str = "",
) -> dict[str, Any]:
    """Recommend a role-balanced subset while retaining provenance as truth."""
    selectable = eligible_landing_assets(provenance, rendered_layers=rendered_layers)
    eligible = [
        asset for asset in selectable if asset.get("visual_selection_tier") == "eligible"
    ]
    reserves = [
        asset
        for asset in selectable
        if asset.get("visual_selection_tier") == "reserve_unmatched"
    ]
    available = [asset for asset in eligible if str(asset.get("output_file") or "").strip()]
    unique = _unique_visuals(available)
    unique_reserves = _unique_visuals(
        [asset for asset in reserves if str(asset.get("output_file") or "").strip()]
    )
    limit = min(16, len(unique))

    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for asset in unique:
        buckets[str(asset["landing_role"])].append(asset)
    for bucket in buckets.values():
        bucket.sort(key=_asset_rank, reverse=True)

    recommended: list[dict[str, Any]] = []
    while len(recommended) < limit:
        added = False
        for role in LANDING_VISUAL_ROLES:
            if buckets[role] and len(recommended) < limit:
                recommended.append(buckets[role].pop(0))
                added = True
        if not added:
            break

    reserve_allowance = min(max(0, 8 - len(unique)), 2)
    optional_reserves = unique_reserves[:reserve_allowance]
    visual_experience_contract = _visual_experience_contract(
        provenance,
        current_color_system=current_color_system,
        brief=brief,
    )
    return {
        "kind": "landing_visual_plan",
        "version": 2,
        "source": "paper_visual_provenance",
        "visual_experience_contract": visual_experience_contract,
        "eligible_asset_count": len(eligible),
        "reserve_asset_count": len(reserves),
        "available_unique_asset_count": len(unique),
        "recommendation_target": {"min": 8, "max": 16},
        "validation_targets": {
            "required_unique_source_visuals": min(8, len(unique)),
            "corpus_capacity_limited": len(unique) < 8,
        },
        "recommended_asset_count": len(recommended),
        "recommended_assets": [_compact_asset(asset) for asset in recommended],
        "optional_reserve_asset_count": len(optional_reserves),
        "optional_reserve_assets": [
            {
                **_compact_asset(asset),
                "story_role": "supporting",
                "claim_policy": "shortfall_only_no_method_or_results_claims",
            }
            for asset in optional_reserves
        ],
        "catalog_file": "landing_asset_catalog.json",
        "selection_policy": "role_balanced_formal_evidence_with_optional_unmatched_reserve",
    }


def _visual_experience_contract(
    provenance: dict[str, Any] | None,
    *,
    current_color_system: dict[str, Any] | None,
    brief: str,
) -> dict[str, Any]:
    brief_opt_in = _explicit_3d_request(brief)
    source_opt_in = _source_requires_3d(provenance)
    if brief_opt_in and source_opt_in:
        opt_in_source = "brief_and_source"
    elif brief_opt_in:
        opt_in_source = "brief"
    elif source_opt_in:
        opt_in_source = "source"
    else:
        opt_in_source = "none"

    color_system = dict(current_color_system) if isinstance(current_color_system, dict) else None
    return {
        "kind": "landing_visual_experience_contract",
        "version": 1,
        "surface": {
            "mode": "academic_light_editorial",
            "dark_default_allowed": False,
        },
        "color": {
            "primary_accent_count": 1,
            "current_color_system": color_system,
        },
        "icons": {
            "format": "inline_svg",
            "count": {"min": 3, "max": 8},
            "style": "restrained_functional",
            "accessible_name_required_for_icon_only_controls": True,
        },
        "interaction": {
            "source_grounded_required": True,
            "purpose": "inspect_compare_or_navigate_paper_evidence",
        },
        "motion": {
            "purposeful_only": True,
            "reduced_motion_required_when_used": True,
            "content_visibility": "visible_without_javascript",
        },
        "three_d": {
            "enabled": opt_in_source != "none",
            "default": "off",
            "opt_in_source": opt_in_source,
            "policy": "source_or_brief_explicit_opt_in_only",
        },
    }


def _explicit_3d_request(value: str) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    if re.search(r"\b(?:no|without|avoid|disable)\s+(?:interactive\s+)?3d\b|\b3d\s+off\b", text):
        return False
    return bool(
        re.search(
            r"\b(?:interactive|source|paper|use|show|render|explore)\b[^.\n]{0,48}\b3d\b"
            r"|\b3d\s+(?:reconstruction|scene|model|view|viewer|visualization)\b"
            r"|\bwebgl\b|\bthree\.js\b",
            text,
        )
    )


def _source_requires_3d(provenance: dict[str, Any] | None) -> bool:
    if not isinstance(provenance, dict):
        return False
    for asset in provenance.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        kind = str(asset.get("kind") or "").strip().lower()
        mime_type = str(asset.get("mime_type") or "").strip().lower()
        output_file = str(asset.get("output_file") or "").strip().lower()
        description = " ".join(
            str(asset.get(key) or "")
            for key in ("caption_short", "caption_full", "visual_role")
        ).lower()
        if (
            kind in {"3d_model", "model_3d", "scene_3d", "webgl_scene"}
            or mime_type.startswith("model/")
            or re.search(r"\.(?:glb|gltf|obj)(?:$|[?#])", output_file)
            or re.search(
                r"\binteractive\s+3d\b|\b3d\s+(?:reconstruction|scene|model|viewer|visualization)\b|\bwebgl\b",
                description,
            )
        ):
            return True
    return False


def _unique_visuals(assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for asset in assets:
        fingerprint = str(asset.get("output_sha256") or "").strip()
        key = f"sha256:{fingerprint}" if fingerprint else f"asset:{asset['asset_id']}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(asset)
    return unique


def _landing_role(asset: dict[str, Any]) -> str:
    declared = str(asset.get("visual_role") or "").strip().lower()
    if declared in LANDING_VISUAL_ROLES:
        return declared
    blob = " ".join(
        str(asset.get(key) or "").lower()
        for key in (
            "visual_role",
            "kind",
            "caption_short",
            "caption_full",
            "anchor_kind",
            "anchor_reason",
        )
    )
    if str(asset.get("kind") or "").lower() == "table":
        return "data"
    for role in LANDING_VISUAL_ROLES:
        if any(term in blob for term in _ROLE_TERMS[role]):
            return role
    return "analysis"


def _asset_rank(asset: dict[str, Any]) -> tuple[float, int, int, str]:
    try:
        score = float(asset.get("visual_score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    return (
        score,
        1 if asset.get("protected_anchor") else 0,
        1 if asset.get("caption_full") or asset.get("caption_short") else 0,
        str(asset.get("asset_id") or ""),
    )


def _compact_asset(asset: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "asset_id",
        "landing_role",
        "kind",
        "output_file",
        "output_sha256",
        "output_width_px",
        "output_height_px",
        "caption_short",
        "caption_full",
        "visual_role",
        "visual_score",
        "source_page",
    )
    return {key: asset.get(key) for key in keys if asset.get(key) is not None}
