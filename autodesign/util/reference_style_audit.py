"""Deterministic audits for reference-style extraction artifacts."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, Tag
from PIL import Image

from .io import sha256_file


_FORBIDDEN_PIPELINE_ARTIFACTS = (
    "designer_author",
    "final",
    "html_first",
    "paper_memory.json",
    "paper_memory",
    "poster_content_brief.json",
    "poster_plan_contract.json",
    "poster.html",
    "run_events.jsonl",
)
_FORBIDDEN_BLUEPRINT_TAGS = {
    "a", "audio", "base", "button", "canvas", "embed", "form", "iframe",
    "img", "input", "link", "meta", "object", "script", "svg", "video",
}


def audit_reference_style_artifacts(
    run_dir: Path,
    *,
    expected_source_sha256: str = "",
    expected_page_index: int | None = None,
    expected_skill_sha256: str = "",
    expected_skill_bundle_sha256: str = "",
    expected_skill_resource_sha256: dict[str, str] | None = None,
    enforce_extraction_only_artifacts: bool = False,
) -> dict[str, Any]:
    """Return a fail-closed audit without invoking any generation pipeline stage."""

    root = Path(run_dir).resolve()
    reference_dir = root / "reference_poster"
    contract = _read_json(root / "reference_style_contract.json")
    metadata = _read_json(reference_dir / "reference_source_metadata.json")
    review = _read_json(reference_dir / "reference_style_agent_review.json")
    blueprint_path = root / "reference_style_blueprint.html"
    raw_blueprint_path = reference_dir / "reference_style_blueprint.html"
    preview_path = root / "reference_style_blueprint_preview.png"
    skill_path = reference_dir / "reference_style_agent_skill.md"
    issues: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str) -> bool:
        if not ok:
            issues.append({"check": name, "detail": detail})
        return ok

    checks: dict[str, bool] = {}
    checks["contract_present"] = check(
        "contract_present", bool(contract), "reference_style_contract.json is missing or invalid"
    )
    source_sha = str(metadata.get("source_sha256") or "")
    expected_source = expected_source_sha256 or str(contract.get("source_sha256") or "")
    checks["source_binding"] = check(
        "source_binding",
        bool(source_sha and source_sha == expected_source == str(contract.get("source_sha256") or "")),
        "source SHA does not match metadata, contract, and requested source",
    )
    requested_page = (
        int(expected_page_index)
        if expected_page_index is not None
        else int(contract.get("source_page_index") or 0)
    )
    checks["page_binding"] = check(
        "page_binding",
        int(metadata.get("page_index") or 0) == requested_page
        and int(contract.get("source_page_index") or 0) == requested_page,
        "reference page index does not match metadata and contract",
    )
    skill_sha = sha256_file(skill_path) if skill_path.exists() else ""
    expected_skill = expected_skill_sha256 or str(contract.get("extraction_skill_sha256") or "")
    checks["skill_binding"] = check(
        "skill_binding",
        bool(skill_sha and skill_sha == expected_skill == str(contract.get("extraction_skill_sha256") or "")),
        "staged extraction skill SHA does not match the contract/current skill",
    )
    contract_bundle_sha = str(contract.get("extraction_skill_bundle_sha256") or "")
    expected_bundle_sha = expected_skill_bundle_sha256 or contract_bundle_sha
    checks["skill_bundle_binding"] = check(
        "skill_bundle_binding",
        bool(
            len(contract_bundle_sha) == 64
            and contract_bundle_sha == expected_bundle_sha
        ),
        "extraction skill bundle SHA does not match the current bundle",
    )
    contract_resource_hashes = contract.get("extraction_skill_resource_sha256")
    contract_resource_hashes = (
        {str(key): str(value) for key, value in contract_resource_hashes.items()}
        if isinstance(contract_resource_hashes, dict)
        else {}
    )
    expected_resource_hashes = (
        {str(key): str(value) for key, value in expected_skill_resource_sha256.items()}
        if isinstance(expected_skill_resource_sha256, dict)
        else contract_resource_hashes
    )
    staged_resources_ok = bool(expected_resource_hashes)
    if staged_resources_ok:
        for relative_path, expected_sha in expected_resource_hashes.items():
            staged_path = reference_dir / "runtime_skills" / relative_path
            if not staged_path.is_file() or sha256_file(staged_path) != expected_sha:
                staged_resources_ok = False
                break
    checks["skill_resources_binding"] = check(
        "skill_resources_binding",
        staged_resources_ok and contract_resource_hashes == expected_resource_hashes,
        "staged extraction skill resources do not match the contract/current bundle",
    )

    raw_sha = sha256_file(raw_blueprint_path) if raw_blueprint_path.exists() else ""
    checks["raw_review_hash"] = check(
        "raw_review_hash",
        bool(raw_sha and raw_sha == str(review.get("blueprint_sha256") or "")),
        "agent review is not bound to the exact raw blueprint",
    )
    blueprint = contract.get("blueprint") if isinstance(contract.get("blueprint"), dict) else {}
    sanitized_sha = sha256_file(blueprint_path) if blueprint_path.exists() else ""
    checks["sanitized_blueprint_hash"] = check(
        "sanitized_blueprint_hash",
        bool(sanitized_sha and sanitized_sha == str(blueprint.get("sha256") or "")),
        "sanitized blueprint SHA does not match the contract",
    )
    visual_diff = float(blueprint.get("sanitization_visual_diff_ratio") or 0.0)
    raw_preview_path = root / str(blueprint.get("raw_preview_path") or "")
    recomputed_visual_diff = _image_diff_ratio(raw_preview_path, preview_path)
    checks["sanitized_visual_equivalence"] = check(
        "sanitized_visual_equivalence",
        raw_preview_path.is_file()
        and visual_diff == 0.0
        and recomputed_visual_diff == 0.0
        and str(blueprint.get("raw_preview_sha256") or "") == sha256_file(raw_preview_path)
        and str(blueprint.get("preview_sha256") or "") == sha256_file(preview_path),
        "sanitized blueprint is not pixel-identical to the reviewed raw blueprint",
    )
    checks["runtime_fingerprint"] = check(
        "runtime_fingerprint",
        len(str(contract.get("extraction_runtime_fingerprint") or "")) == 64,
        "contract is not bound to the extraction command, harness, and model hint",
    )
    checks["prompt_schema_binding"] = check(
        "prompt_schema_binding",
        len(str(contract.get("extraction_prompt_schema_sha256") or "")) == 64,
        "contract is not bound to the Reference Style Agent prompt/schema",
    )
    metadata_canvas = (
        metadata.get("canvas_contract")
        if isinstance(metadata.get("canvas_contract"), dict)
        else {}
    )
    contract_canvas = (
        contract.get("canvas_contract")
        if isinstance(contract.get("canvas_contract"), dict)
        else {}
    )
    expected_canvas = [
        int(contract_canvas.get("w_px") or metadata_canvas.get("w_px") or 3072),
        int(contract_canvas.get("h_px") or metadata_canvas.get("h_px") or 1536),
    ]
    checks["canvas_binding"] = check(
        "canvas_binding",
        bool(metadata_canvas and contract_canvas and metadata_canvas == contract_canvas),
        "reference canvas does not match metadata and contract",
    )
    preview_size: list[int] = []
    preview_nonblank = False
    if preview_path.exists():
        try:
            with Image.open(preview_path) as image:
                preview_size = [int(image.width), int(image.height)]
                sample = image.convert("RGB").resize((256, 128))
                colors = sample.getcolors(maxcolors=256 * 128) or []
                preview_nonblank = len(colors) >= 2 and max(count for count, _ in colors) < 256 * 128
        except OSError:
            preview_size = []
    checks["preview_canvas"] = check(
        "preview_canvas", preview_size == expected_canvas,
        f"sanitized blueprint preview must be {expected_canvas[0]}x{expected_canvas[1]}, "
        f"got {preview_size or 'unreadable'}",
    )
    checks["preview_nonblank"] = check(
        "preview_nonblank", preview_nonblank,
        "sanitized blueprint preview is blank or visually uniform",
    )

    soup = BeautifulSoup(
        blueprint_path.read_text(encoding="utf-8") if blueprint_path.exists() else "",
        "html.parser",
    )
    root_tag = soup.select_one(".reference-style-blueprint")
    region_structure = (
        ((contract.get("style_tokens") or {}).get("body_region_structure") or {})
        if isinstance(contract.get("style_tokens"), dict)
        else {}
    )
    expected_regions = [
        item for item in region_structure.get("regions") or [] if isinstance(item, dict)
    ]
    region_tags = soup.select('[data-style-role="body-region"]')
    actual_ids = [str(tag.get("data-region-id") or "") for tag in region_tags]
    expected_ids = [str(item.get("region_id") or "") for item in expected_regions]
    expected_roles = [str(item.get("region_role") or "") for item in expected_regions]
    actual_roles = [str(tag.get("data-region-role") or "") for tag in region_tags]
    checks["region_ids"] = check(
        "region_ids",
        bool(expected_ids and actual_ids == expected_ids and len(actual_ids) == len(set(actual_ids))),
        f"body region IDs differ: expected={expected_ids}, actual={actual_ids}",
    )
    checks["region_roles"] = check(
        "region_roles", actual_roles == expected_roles,
        f"body region roles differ: expected={expected_roles}, actual={actual_roles}",
    )
    top_sections = [
        tag for tag in soup.select('[data-style-role="section"]')
        if not isinstance(tag.find_parent(attrs={"data-style-role": "section"}), Tag)
    ]
    owned = [
        tag for tag in top_sections
        if isinstance(tag.find_parent(attrs={"data-style-role": "body-region"}), Tag)
    ]
    checks["all_top_level_sections_owned"] = check(
        "all_top_level_sections_owned",
        bool(top_sections and len(owned) == len(top_sections)),
        f"{len(top_sections) - len(owned)} top-level sections are outside body regions",
    )
    actual_counts = [
        sum(
            1 for section in top_sections
            if section.find_parent(attrs={"data-style-role": "body-region"}) is region
        )
        for region in region_tags
    ]
    expected_counts = [
        int(item) for item in region_structure.get("major_sections_per_region") or []
    ]
    checks["region_section_counts"] = check(
        "region_section_counts", actual_counts == expected_counts,
        f"section counts differ: expected={expected_counts}, actual={actual_counts}",
    )
    checks["safe_blueprint_dom"] = check(
        "safe_blueprint_dom",
        not any(soup.find(tag_name) for tag_name in _FORBIDDEN_BLUEPRINT_TAGS),
        "sanitized blueprint contains forbidden executable, remote, or bitmap elements",
    )
    chrome_layers = soup.select('[data-style-role="chrome-layer"]')
    chrome_contract = (
        ((contract.get("style_tokens") or {}).get("chrome_treatment") or {})
        if isinstance(contract.get("style_tokens"), dict)
        else {}
    )
    chrome_required = bool(chrome_contract.get("present"))
    chrome_visible_structure = bool(
        len(chrome_layers) == 1
        and chrome_layers[0].find(True) is not None
        and not re.search(
            r"(?:display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0(?:\D|$))",
            str(chrome_layers[0].get("style") or ""),
            flags=re.I,
        )
    )
    checks["chrome_isolation"] = check(
        "chrome_isolation",
        len(chrome_layers) <= 1
        and all(isinstance(root_tag, Tag) and layer.parent is root_tag for layer in chrome_layers),
        "chrome must be absent or one direct child of the blueprint root",
    )
    checks["chrome_presence"] = check(
        "chrome_presence",
        chrome_visible_structure if chrome_required else not chrome_layers,
        "chrome contract and sanitized root-level chrome structure disagree",
    )

    boxes = (
        ((contract.get("style_tokens") or {}).get("layout_rhythm") or {}).get("region_boxes")
        if isinstance(contract.get("style_tokens"), dict)
        else []
    )
    geometry_ok = isinstance(boxes, list) and len(boxes) == len(expected_ids)
    normalized_boxes: list[tuple[float, float, float, float]] = []
    if geometry_ok:
        for box in boxes:
            try:
                values = tuple(float(box[key]) for key in ("x_pct", "y_pct", "w_pct", "h_pct"))
            except (KeyError, TypeError, ValueError):
                geometry_ok = False
                break
            x, y, width, height = values
            if (
                not all(math.isfinite(value) for value in values)
                or width < 2 or height < 2 or x < -0.25 or y < -0.25
                or x + width > 100.25 or y + height > 100.25
            ):
                geometry_ok = False
                break
            normalized_boxes.append(values)
    if geometry_ok:
        for index, left in enumerate(normalized_boxes):
            for right in normalized_boxes[index + 1:]:
                overlap_w = max(0.0, min(left[0] + left[2], right[0] + right[2]) - max(left[0], right[0]))
                overlap_h = max(0.0, min(left[1] + left[3], right[1] + right[3]) - max(left[1], right[1]))
                if overlap_w * overlap_h > 0.01:
                    geometry_ok = False
                    break
            if not geometry_ok:
                break
    checks["region_geometry"] = check(
        "region_geometry", geometry_ok,
        "measured body region boxes are missing, out of bounds, or overlap",
    )

    forbidden_found: list[str] = []
    if enforce_extraction_only_artifacts:
        forbidden_names = set(_FORBIDDEN_PIPELINE_ARTIFACTS)
        forbidden_found = [
            str(path.relative_to(root))
            for path in root.rglob("*")
            if path.name in forbidden_names
        ]
        layers_dir = root / "layers"
        if layers_dir.exists() and any(layers_dir.iterdir()):
            forbidden_found.append("layers/*")
    checks["forbidden_pipeline_artifacts_absent"] = check(
        "forbidden_pipeline_artifacts_absent", not forbidden_found,
        f"extraction-only run produced pipeline artifacts: {forbidden_found}",
    )
    return {
        "schema_version": 1,
        "status": "pass" if not issues else "fail",
        "run_dir": str(root),
        "source_sha256": source_sha,
        "page_index": requested_page,
        "style_reference_id": str(contract.get("style_reference_id") or ""),
        "preview_size": preview_size,
        "region_count": len(region_tags),
        "region_roles": [str(tag.get("data-region-role") or "") for tag in region_tags],
        "checks": checks,
        "issues": issues,
    }


def semantic_reference_style_issues(
    contract: dict[str, Any],
    expectation: Any,
) -> list[str]:
    """Compare a contract with optional benchmark-only semantic expectations."""
    if not isinstance(expectation, dict):
        return []
    tokens = contract.get("style_tokens") if isinstance(contract.get("style_tokens"), dict) else {}
    regions = tokens.get("body_region_structure") if isinstance(tokens.get("body_region_structure"), dict) else {}
    header = tokens.get("header_treatment") if isinstance(tokens.get("header_treatment"), dict) else {}
    section = tokens.get("section_heading_treatment") if isinstance(tokens.get("section_heading_treatment"), dict) else {}
    typography = tokens.get("typography_style") if isinstance(tokens.get("typography_style"), dict) else {}
    roles = [
        str(item.get("region_role") or "")
        for item in regions.get("regions") or []
        if isinstance(item, dict)
    ]
    actual = {
        "region_count": int(regions.get("region_count") or 0),
        "layout_mode": str(regions.get("layout_mode") or ""),
        "lead_band_present": bool((tokens.get("lead_band") or {}).get("present")),
        "chrome_present": bool((tokens.get("chrome_treatment") or {}).get("present")),
        "header_rule_placement": str(header.get("rule_placement") or "none"),
        "header_mode": str(header.get("mode") or ""),
        "header_background_role": str(header.get("background_role") or ""),
        "header_rule_width_px": int(header.get("rule_width_px") or 0),
        "section_heading_mode": str(section.get("mode") or ""),
        "section_heading_fill_role": str(section.get("fill_role") or ""),
        "section_heading_corner_style": str(section.get("corner_style") or ""),
        "section_heading_border_width_px": int(section.get("border_width_px") or 0),
        "display_family_category": str(typography.get("display_family_category") or ""),
        "body_family_category": str(typography.get("body_family_category") or ""),
    }
    issues: list[str] = []
    for key in (
        "region_count", "layout_mode", "lead_band_present", "chrome_present",
        "header_rule_placement", "header_mode", "header_background_role",
        "header_rule_width_px", "section_heading_mode", "section_heading_fill_role",
        "section_heading_corner_style", "section_heading_border_width_px",
        "display_family_category", "body_family_category",
    ):
        if key in expectation and actual[key] != expectation[key]:
            issues.append(f"{key}: expected {expectation[key]!r}, got {actual[key]!r}")
    if "min_region_count" in expectation and actual["region_count"] < int(expectation["min_region_count"]):
        issues.append(f"region_count below minimum {expectation['min_region_count']}")
    if "max_region_count" in expectation and actual["region_count"] > int(expectation["max_region_count"]):
        issues.append(f"region_count above maximum {expectation['max_region_count']}")
    for role in expectation.get("required_region_roles") or []:
        if str(role) not in roles:
            issues.append(f"missing required region role {role!r}")
    role_counts = {role: roles.count(role) for role in set(roles)}
    for role, expected_count in (expectation.get("region_role_counts") or {}).items():
        if role_counts.get(str(role), 0) != int(expected_count):
            issues.append(
                f"region role {role!r}: expected {expected_count}, got {role_counts.get(str(role), 0)}"
            )
    if "max_region_width_spread_pct" in expectation:
        boxes = ((tokens.get("layout_rhythm") or {}).get("region_boxes") or [])
        widths = [float(item.get("w_pct") or 0) for item in boxes if isinstance(item, dict)]
        spread = max(widths) - min(widths) if widths else float("inf")
        if spread > float(expectation["max_region_width_spread_pct"]):
            issues.append(
                f"region width spread {spread:.3f} exceeds {expectation['max_region_width_spread_pct']}"
            )
    return issues


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _image_diff_ratio(left_path: Path, right_path: Path) -> float:
    if not left_path.is_file() or not right_path.is_file():
        return 1.0
    from PIL import ImageChops

    with Image.open(left_path).convert("RGB") as left, Image.open(right_path).convert("RGB") as right:
        if left.size != right.size:
            return 1.0
        histogram = ImageChops.difference(left, right).convert("L").histogram()
        changed = left.width * left.height - histogram[0]
        return round(changed / max(1, left.width * left.height), 8)
