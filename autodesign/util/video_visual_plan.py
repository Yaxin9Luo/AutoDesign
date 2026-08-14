"""Deterministic full-ingest visual planning for conference videos."""

from __future__ import annotations

import hashlib
from pathlib import Path
import posixpath
import re
from typing import Any
from urllib.parse import urlparse

from ..schema import VIDEO_MAX_DURATION_S, VIDEO_MIN_DURATION_S
from .source_visual_eligibility import classify_source_visual


_ALLOWED_KINDS = {"background", "figure", "image", "table"}
_STALE_ELIGIBILITY_KEYS = {
    "designer_eligible",
    "planner_eligible",
    "planner_visible",
    "designer_reject_reasons",
    "planner_reject_reasons",
    "severe_crop_flags",
    "visual_selection_tier",
    "eligibility_policy_version",
    "eligible",
    "video_eligible",
}
_SUBPANEL_ID_RE = re.compile(r"_(?:fig|table)_\d+_[a-z0-9]$", re.IGNORECASE)
_ROLE_SCENES = {
    "method": (2, 3, 4, 5),
    "results": (6, 7, 8, 9),
    "qualitative": (1, 8, 9, 10),
    "other": (1, 5, 10, 11),
}


def build_video_visual_asset_catalog(
    paper_visual_provenance: dict[str, Any],
    *,
    rendered_layers: dict[str, dict[str, Any]] | None = None,
    trusted_run_root: Path | None = None,
) -> dict[str, Any]:
    """Return full provenance with eligibility recomputed from source evidence."""
    raw_assets = paper_visual_provenance.get("assets")
    if not isinstance(raw_assets, list):
        raw_assets = []
    rendered = rendered_layers if isinstance(rendered_layers, dict) else {}

    source_assets: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw_asset in raw_assets:
        if not isinstance(raw_asset, dict) or not _catalog_asset(raw_asset):
            continue
        asset_id = str(raw_asset["asset_id"]).strip()
        if asset_id in seen_ids:
            continue
        seen_ids.add(asset_id)
        layer = rendered.get(asset_id) if isinstance(rendered.get(asset_id), dict) else {}
        eligibility = classify_source_visual(asset_id, raw_asset, layer)
        output_file = _staged_output_file(raw_asset, layer)
        if not _local_output_path(output_file):
            eligibility = {
                **eligibility,
                "designer_eligible": False,
                "planner_eligible": False,
                "planner_visible": False,
                "visual_selection_tier": "rejected",
                "designer_reject_reasons": [
                    *list(eligibility.get("designer_reject_reasons") or []),
                    "nonlocal_or_unsafe_output_file",
                ],
            }
        source_record = _without_stale_eligibility(raw_asset)
        layer_record = _without_stale_eligibility(layer)
        fingerprint, actual_sha256, claimed_sha256, hash_verified = (
            _visual_fingerprint(
                raw_asset,
                layer,
                output_file,
                trusted_run_root=trusted_run_root,
            )
        )
        if trusted_run_root is not None and not actual_sha256:
            eligibility = {
                **eligibility,
                "designer_eligible": False,
                "planner_eligible": False,
                "planner_visible": False,
                "visual_selection_tier": "rejected",
                "designer_reject_reasons": [
                    *list(eligibility.get("designer_reject_reasons") or []),
                    "missing_trusted_source_payload",
                ],
            }
        source_assets.append({
            "asset_id": asset_id,
            "fingerprint": fingerprint,
            "actual_sha256": actual_sha256,
            "claimed_sha256": claimed_sha256,
            "provenance_hash_verified": hash_verified,
            "kind": str(raw_asset.get("kind") or layer.get("kind") or "image"),
            "output_file": output_file,
            "caption_short": str(raw_asset.get("caption_short") or ""),
            "caption_full": str(
                raw_asset.get("caption_full") or layer.get("caption") or ""
            ),
            "visual_role": str(
                raw_asset.get("visual_role") or layer.get("visual_role") or ""
            ),
            "visual_score": raw_asset.get("visual_score") or layer.get("visual_score"),
            "source_page": raw_asset.get("source_page") or layer.get("source_page"),
            "video_evidence_role": _evidence_role({**raw_asset, **layer}),
            "can_satisfy_required_coverage": (
                str(eligibility.get("visual_selection_tier") or "rejected")
                == "eligible"
            ),
            "eligibility": {
                "eligible": bool(eligibility.get("designer_eligible")),
                "tier": str(eligibility.get("visual_selection_tier") or "rejected"),
                "reject_reasons": list(
                    eligibility.get("designer_reject_reasons") or []
                ),
                "policy_version": eligibility.get("eligibility_policy_version"),
            },
            "provenance": _json_value(source_record),
            "rendered_layer": _json_value(layer_record),
        })

    catalog_by_fingerprint: dict[str, dict[str, Any]] = {}
    for asset in source_assets:
        fingerprint = str(asset["fingerprint"])
        current = catalog_by_fingerprint.get(fingerprint)
        if current is None or _representative_rank(asset) > _representative_rank(current):
            catalog_by_fingerprint[fingerprint] = asset
    catalog_assets = list(catalog_by_fingerprint.values())
    catalog_assets.sort(key=_asset_sort_key)
    return {
        "kind": "video_visual_asset_catalog",
        "version": 1,
        "source_manifest": "paper_visual_provenance.json",
        "selection_scope": "full_paper_visual_provenance",
        "source_asset_count": len(source_assets),
        "asset_count": len(catalog_assets),
        "unique_visual_count": len(catalog_assets),
        "eligible_asset_count": sum(
            1 for asset in catalog_assets if asset["eligibility"]["eligible"]
        ),
        "required_eligible_asset_count": sum(
            1 for asset in catalog_assets if asset["can_satisfy_required_coverage"]
        ),
        "rejected_asset_count": sum(
            1 for asset in catalog_assets if not asset["eligibility"]["eligible"]
        ),
        "assets": catalog_assets,
    }


def build_video_visual_plan(
    paper_visual_provenance: dict[str, Any],
    *,
    rendered_layers: dict[str, dict[str, Any]] | None = None,
    trusted_run_root: Path | None = None,
    scene_count: int = 12,
    target_duration_s: int | None = None,
) -> dict[str, Any]:
    """Recommend 8-16 unique full-provenance visuals across 10-14 scenes."""
    if not 10 <= scene_count <= 14:
        raise ValueError("conference video visual plans require 10-14 scenes")
    if (
        target_duration_s is not None
        and not VIDEO_MIN_DURATION_S <= target_duration_s <= VIDEO_MAX_DURATION_S
    ):
        raise ValueError("conference video duration must be within 300-600 seconds")

    catalog = build_video_visual_asset_catalog(
        paper_visual_provenance,
        rendered_layers=rendered_layers,
        trusted_run_root=trusted_run_root,
    )
    assets = [
        asset for asset in catalog["assets"]
        if asset["eligibility"]["eligible"]
    ]
    required_assets = [
        asset for asset in assets if asset["can_satisfy_required_coverage"]
    ]
    reserve_assets = [
        asset for asset in assets if not asset["can_satisfy_required_coverage"]
    ]
    recommendation_count = min(16, len(required_assets))
    required_recommended = _balanced_recommendations(
        required_assets, recommendation_count
    )
    reserve_allowance = min(max(0, 8 - len(required_recommended)), 2)
    recommended = [
        *required_recommended,
        *reserve_assets[:reserve_allowance],
    ]
    scene_visual_map = _map_assets_to_scenes(recommended, scene_count)
    roles_present = sorted({
        str(asset["video_evidence_role"]) for asset in required_recommended
    })

    plan = {
        "kind": "video_visual_plan",
        "version": 1,
        "source_manifest": "paper_visual_provenance.json",
        "asset_catalog": "video_visual_asset_catalog.json",
        "selection_scope": "full_paper_visual_provenance",
        "target_duration_range_s": {
            "minimum": VIDEO_MIN_DURATION_S,
            "maximum": VIDEO_MAX_DURATION_S,
        },
        "duration_selection_policy": (
            "Choose a target from 300-600 seconds based on the paper's complexity, "
            "evidence density, and the time needed for a clear conference narrative."
            if target_duration_s is None
            else (
                f"Preserve the selected {target_duration_s}-second target during "
                "repair, resume, and final delivery."
            )
        ),
        "scene_count": scene_count,
        "narration_contract": {
            "intent_semantics": "verbatim_spoken_transcript",
            "language": "en",
            "minimum_spoken_wpm": 90,
            "minimum_speech_coverage_ratio": 0.72,
            "maximum_tts_speed": 1.25,
            "subtitle_source": "canonical_narration_transcript",
            "padding_policy": "no_repeated_or_empty_filler",
        },
        "eligible_asset_count": len(assets),
        "unique_eligible_visual_count": len(assets),
        "required_eligible_asset_count": len(required_assets),
        "minimum_required_visual_count": min(8, len(required_assets)),
        "recommended_asset_count": len(recommended),
        "required_recommended_asset_count": len(required_recommended),
        "recommended_assets": [_compact_asset(asset) for asset in recommended],
        "scene_visual_map": scene_visual_map,
        "coverage": {
            "roles_present": roles_present,
            "method": "method" in roles_present,
            "results": "results" in roles_present,
            "qualitative": "qualitative" in roles_present,
        },
        "repetition_policy": {
            "default": "use each recommended asset in one scene",
            "mapped_asset_ids_are_unique": True,
            "mapped_asset_fingerprints_are_unique": True,
            "placement_target_basis": "content_fingerprint",
            "max_placements_per_fingerprint": 1,
        },
    }
    if target_duration_s is not None:
        plan["target_duration_s"] = target_duration_s
    return plan


def _catalog_asset(asset: dict[str, Any]) -> bool:
    asset_id = str(asset.get("asset_id") or "").strip()
    kind = str(asset.get("kind") or "image").strip().lower()
    if not asset_id or kind not in _ALLOWED_KINDS:
        return False
    if _SUBPANEL_ID_RE.search(asset_id):
        return False
    return True


def _local_output_path(output_file: str) -> bool:
    if not output_file:
        return False
    parsed = urlparse(output_file)
    candidate = Path(output_file)
    return not (
        parsed.scheme
        or output_file.startswith("//")
        or candidate.is_absolute()
        or ".." in candidate.parts
    )


def _staged_output_file(
    source: dict[str, Any],
    layer: dict[str, Any],
) -> str:
    src_path = str(layer.get("src_path") or layer.get("png_path") or "").strip()
    if src_path:
        return f"layers/{Path(src_path).name}"
    output_file = str(source.get("output_file") or "").strip().replace("\\", "/")
    return output_file[2:] if output_file.startswith("./") else output_file


def _visual_fingerprint(
    source: dict[str, Any],
    layer: dict[str, Any],
    output_file: str,
    *,
    trusted_run_root: Path | None = None,
) -> tuple[str, str | None, str | None, bool | None]:
    claimed_sha256: str | None = None
    for record in (source, layer):
        for key in ("output_sha256", "sha256"):
            value = str(record.get(key) or "").strip().lower()
            if value:
                claimed_sha256 = value
                break
        if claimed_sha256:
            break
    actual_path = _actual_visual_path(
        source,
        layer,
        trusted_run_root=trusted_run_root,
    )
    if actual_path is not None:
        actual_sha256 = _file_sha256(actual_path)
        return (
            f"sha256:{actual_sha256}",
            actual_sha256,
            claimed_sha256,
            actual_sha256 == claimed_sha256 if claimed_sha256 else None,
        )
    normalized_path = _normalized_staged_path(output_file)
    if normalized_path:
        return f"path:{normalized_path}", None, None, None
    return f"asset:{str(source.get('asset_id') or '').strip()}", None, None, None


def _actual_visual_path(
    source: dict[str, Any],
    layer: dict[str, Any],
    *,
    trusted_run_root: Path | None = None,
) -> Path | None:
    trusted_root = trusted_run_root.resolve() if trusted_run_root is not None else None
    for record, keys in (
        (layer, ("src_path", "png_path")),
        (source, ("output_file",)),
    ):
        for key in keys:
            value = str(record.get(key) or "").strip()
            if not value:
                continue
            candidate = Path(value).expanduser()
            if not candidate.is_absolute() and trusted_root is not None:
                candidate = trusted_root / candidate
            if not candidate.is_file():
                continue
            resolved = candidate.resolve()
            if trusted_root is not None and not resolved.is_relative_to(trusted_root):
                continue
            return resolved
    return None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_staged_path(output_file: str) -> str:
    normalized = str(output_file or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized:
        return ""
    return posixpath.normpath(normalized)


def _evidence_role(asset: dict[str, Any]) -> str:
    role = str(asset.get("visual_role") or "").strip().lower()
    if role in {"method", "architecture", "framework", "pipeline"}:
        return "method"
    if role in {"qualitative", "demo", "case_study", "example"}:
        return "qualitative"
    if role in {"result", "results", "evidence", "benchmark", "evaluation", "ablation"}:
        return "results"
    text = " ".join(
        str(asset.get(key) or "").lower()
        for key in ("caption_short", "caption_full", "curation_reason", "asset_id")
    )
    combined = f"{role} {text}"
    if any(token in combined for token in ("qualitative", "case study", "demo", "example")):
        return "qualitative"
    if any(
        token in combined
        for token in (
            "result",
            "evidence",
            "benchmark",
            "evaluation",
            "ablation",
            "comparison",
            "metric",
        )
    ):
        return "results"
    if any(
        token in combined
        for token in ("method", "architecture", "framework", "pipeline", "overview")
    ):
        return "method"
    return "other"


def _asset_sort_key(asset: dict[str, Any]) -> tuple[float, int, str]:
    try:
        score = float(asset.get("visual_score") or 0)
    except (TypeError, ValueError):
        score = 0.0
    try:
        page = int(asset.get("source_page") or 0)
    except (TypeError, ValueError):
        page = 0
    return (-score, page, str(asset.get("asset_id") or ""))


def _representative_rank(asset: dict[str, Any]) -> tuple[Any, ...]:
    eligibility = asset.get("eligibility")
    eligibility = eligibility if isinstance(eligibility, dict) else {}
    tier = str(eligibility.get("tier") or "rejected")
    tier_rank = {"eligible": 3, "reserve_unmatched": 2, "rejected": 0}.get(
        tier, 1
    )
    provenance = asset.get("provenance")
    provenance = provenance if isinstance(provenance, dict) else {}
    rendered = asset.get("rendered_layer")
    rendered = rendered if isinstance(rendered, dict) else {}
    association = str(
        provenance.get("caption_association_method")
        or rendered.get("caption_association_method")
        or ""
    ).lower()
    association_rank = {
        "captioned_group": 5,
        "geometry": 4,
        "vlm": 3,
        "geometry_fallback": 2,
        "unmatched": 0,
    }.get(association, 1)
    confidence = _number(
        provenance.get("caption_confidence")
        or rendered.get("caption_confidence")
    )
    visual_score = _number(asset.get("visual_score"))
    width = _number(
        provenance.get("output_width_px") or rendered.get("output_width_px")
    )
    height = _number(
        provenance.get("output_height_px") or rendered.get("output_height_px")
    )
    try:
        page_rank = -int(asset.get("source_page") or 0)
    except (TypeError, ValueError):
        page_rank = 0
    return (
        tier_rank,
        int(bool(asset.get("can_satisfy_required_coverage"))),
        association_rank,
        confidence,
        visual_score,
        width * height,
        page_rank,
        str(asset.get("asset_id") or ""),
    )


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _balanced_recommendations(
    assets: list[dict[str, Any]],
    count: int,
) -> list[dict[str, Any]]:
    if count <= 0:
        return []
    by_role = {
        role: [asset for asset in assets if asset["video_evidence_role"] == role]
        for role in _ROLE_SCENES
    }
    selected: list[dict[str, Any]] = []
    selected_fingerprints: set[str] = set()

    for role in ("method", "results", "qualitative"):
        if by_role[role]:
            asset = by_role[role].pop(0)
            selected.append(asset)
            selected_fingerprints.add(str(asset["fingerprint"]))

    role_order = ("method", "results", "qualitative", "other")
    while len(selected) < count:
        added = False
        for role in role_order:
            if len(selected) >= count:
                break
            while (
                by_role[role]
                and str(by_role[role][0]["fingerprint"]) in selected_fingerprints
            ):
                by_role[role].pop(0)
            if not by_role[role]:
                continue
            asset = by_role[role].pop(0)
            selected.append(asset)
            selected_fingerprints.add(str(asset["fingerprint"]))
            added = True
        if not added:
            break
    return selected


def _map_assets_to_scenes(
    assets: list[dict[str, Any]],
    scene_count: int,
) -> list[dict[str, Any]]:
    scene_assets: list[list[str]] = [[] for _ in range(scene_count)]
    scene_fingerprints: list[list[str]] = [[] for _ in range(scene_count)]
    placed_fingerprints: set[str] = set()
    for asset in assets:
        fingerprint = str(asset["fingerprint"])
        if fingerprint in placed_fingerprints:
            continue
        placed_fingerprints.add(fingerprint)
        role = str(asset["video_evidence_role"])
        candidates = [
            index
            for index in _ROLE_SCENES.get(role, _ROLE_SCENES["other"])
            if index < scene_count
        ]
        scene_index = min(candidates, key=lambda index: (len(scene_assets[index]), index))
        scene_assets[scene_index].append(str(asset["asset_id"]))
        scene_fingerprints[scene_index].append(fingerprint)
    return [
        {
            "scene_id": f"scene_{index + 1:02d}",
            "visual_ids": visual_ids,
            "visual_fingerprints": scene_fingerprints[index],
        }
        for index, visual_ids in enumerate(scene_assets)
    ]


def _compact_asset(asset: dict[str, Any]) -> dict[str, Any]:
    return {
        "asset_id": asset["asset_id"],
        "fingerprint": asset["fingerprint"],
        "role": asset["video_evidence_role"],
        "output_file": asset["output_file"],
        "caption": asset.get("caption_short") or asset.get("caption_full") or "",
        "source_page": asset.get("source_page"),
        "visual_score": asset.get("visual_score"),
    }


def _without_stale_eligibility(record: dict[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in record.items()
        if str(key) not in _STALE_ELIGIBILITY_KEYS
    }


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
