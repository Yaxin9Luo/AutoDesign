#!/usr/bin/env python3
"""Audit the built-in Runtime Skills progressive-disclosure packages.

This is intentionally offline. It validates the on-disk v2 package contract and
also confirms that the active registry can load the same packages.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


BUILTIN_IDS = {
    "common.export_qa",
    "common.pdf_render_qa",
    "common.pdf_visual_curation",
    "common.playwright_browser_qa",
    "common.source_analysis_flow",
    "deck.html_ppt_general",
    "deck.paper2deck_provenance",
    "deck.ppt_beautify",
    "deck.report2deck_general",
    "landing.visual_recipe",
    "poster.paper_poster_revision",
    "poster.reference_style_extraction",
    "poster.table_craft",
    "poster.visual_recipe",
}

EXPECTED_RESOURCES = {
    "common.export_qa": {"export_checks"},
    "common.pdf_render_qa": {"pdf_render_commands", "pdf_export_checks"},
    "common.pdf_visual_curation": {"visual_role_policy"},
    "common.playwright_browser_qa": {"browser_checks"},
    "common.source_analysis_flow": {"image_policy"},
    "deck.html_ppt_general": {"layout", "theme", "scenario", "agent-flow", "image-policy"},
    "deck.paper2deck_provenance": {"visual_policy"},
    "deck.ppt_beautify": {"beautify_policy"},
    "deck.report2deck_general": {"report_scenario_map"},
    "landing.visual_recipe": {"section_map", "layout_treatments"},
    "poster.paper_poster_revision": {"layout_repair", "export", "manual_asset_policy"},
    "poster.reference_style_extraction": {"output_contract_v4", "region_geometry", "chrome_rules", "failure_checks"},
    "poster.table_craft": {"table_jobs", "academic_style", "repair"},
    "poster.visual_recipe": {"archetype", "paper_layout", "source_flow", "typography_aesthetics", "campaign_event", "reference_mode"},
}

EXPECTED_ROUTING = {
    "common.export_qa": (["all"], ["plan", "critique", "repair"], 10, True, "4f53cda18c2baa0c"),
    "common.pdf_render_qa": (["all"], ["enhance", "plan", "critique", "repair"], 93, True, "6b1dad94876e91f7"),
    "common.pdf_visual_curation": (["all"], ["enhance", "plan", "critique", "repair"], 94, True, "75060d61671e4e59"),
    "common.playwright_browser_qa": (["poster", "deck", "landing"], ["plan", "critique", "repair"], 9, True, "03cf4d1aac497be2"),
    "common.source_analysis_flow": (["all"], ["enhance", "plan", "critique", "repair"], 95, True, "ea34647a15e00631"),
    "deck.html_ppt_general": (["deck"], ["enhance", "plan", "critique", "repair"], 70, True, "7241c64aae57c509"),
    "deck.paper2deck_provenance": (["deck"], ["enhance", "plan", "critique", "repair"], 90, True, "26f3a06e97461a08"),
    "deck.ppt_beautify": (["deck"], ["enhance", "plan", "critique", "repair"], 88, True, "f5641a86386521d1"),
    "deck.report2deck_general": (["deck"], ["enhance", "plan", "critique", "repair"], 85, True, "e585b801fe4f846d"),
    "landing.visual_recipe": (["landing"], ["enhance", "plan", "critique", "repair"], 60, True, "4f53cda18c2baa0c"),
    "poster.paper_poster_revision": (["poster"], ["repair"], 80, False, "8ed09db69fb072d1"),
    "poster.reference_style_extraction": (["poster"], ["plan"], 90, False, "ba1725cb09df88c1"),
    "poster.table_craft": (["poster"], ["enhance", "plan", "critique", "repair"], 70, True, "565c2520371c3191"),
    "poster.visual_recipe": (["poster"], ["enhance", "plan", "critique", "repair"], 60, True, "4f53cda18c2baa0c"),
}

EXPECTED_OUTPUTS = {
    "common.export_qa": ["qa_policy", "repair_priorities"],
    "common.pdf_render_qa": ["pdf_render_policy", "pdf_layout_review_policy", "pdf_export_repair_policy"],
    "common.pdf_visual_curation": ["visual_candidate_policy", "source_visual_slot_plan", "pdf_visual_repair_policy"],
    "common.playwright_browser_qa": ["browser_qa_policy", "dom_audit_policy", "preview_repair_policy"],
    "common.source_analysis_flow": ["source_manifest_policy", "outline_plan", "content_plan", "asset_slot_plan"],
    "deck.html_ppt_general": ["html_scenario_arc", "layout_plan", "theme_tokens", "speaker_notes_policy"],
    "deck.paper2deck_provenance": ["html_slide_arc", "figure_slot_plan", "provenance_policy", "speaker_notes_policy"],
    "deck.ppt_beautify": ["preservation_policy", "redesign_plan", "html_frame_mapping", "qa_policy"],
    "deck.report2deck_general": ["report_outline", "slide_content_plan", "asset_slot_plan", "source_reference_policy"],
    "landing.visual_recipe": ["landing_section_arc", "proof_rhythm", "visual_reference_repair_policy"],
    "poster.paper_poster_revision": ["poster_html_revision_guidance"],
    "poster.reference_style_extraction": ["reference_style_analysis", "reference_style_blueprint", "reference_style_blueprint_review"],
    "poster.table_craft": ["poster_table_selection_policy", "native_table_design_rubric", "table_readout_repair_policy"],
    "poster.visual_recipe": ["poster_archetype", "density_policy", "visual_reference_repair_policy"],
}

EXPECTED_RESOURCE_STAGES = {
    "common.export_qa": {"export_checks": ["plan", "repair"]},
    "common.pdf_render_qa": {"pdf_render_commands": ["plan"], "pdf_export_checks": ["repair"]},
    "common.pdf_visual_curation": {"visual_role_policy": ["plan", "repair"]},
    "common.playwright_browser_qa": {"browser_checks": ["plan", "repair"]},
    "common.source_analysis_flow": {"image_policy": ["plan", "repair"]},
    "deck.html_ppt_general": {
        "layout": ["plan", "repair"], "theme": ["plan"], "scenario": ["plan"],
        "agent-flow": ["plan", "repair"], "image-policy": ["plan", "repair"],
    },
    "deck.paper2deck_provenance": {"visual_policy": ["plan", "repair"]},
    "deck.ppt_beautify": {"beautify_policy": ["plan", "repair"]},
    "deck.report2deck_general": {"report_scenario_map": ["plan", "repair"]},
    "landing.visual_recipe": {
        "section_map": ["plan", "repair"], "layout_treatments": ["plan", "repair"],
    },
    "poster.paper_poster_revision": {
        "layout_repair": ["repair"], "export": ["repair"], "manual_asset_policy": ["repair"],
    },
    "poster.reference_style_extraction": {
        "output_contract_v4": ["plan"], "region_geometry": ["plan"],
        "chrome_rules": ["plan"], "failure_checks": ["plan"],
    },
    "poster.table_craft": {
        "table_jobs": ["plan"], "academic_style": ["plan", "repair"], "repair": ["repair"],
    },
    "poster.visual_recipe": {
        "archetype": ["plan"], "paper_layout": ["plan", "repair"],
        "source_flow": ["plan", "repair"], "typography_aesthetics": ["plan", "repair"],
        "campaign_event": ["plan", "repair"], "reference_mode": ["plan", "repair"],
    },
}

VALID_STAGES = {"enhance", "plan", "critique", "repair"}
RESOURCE_CONSUMER_STAGES = {"plan", "repair"}
STAGE_HEADER = re.compile(r"^## Stage:\s*(\w+)\s*$", re.MULTILINE)
MAX_SKILL_CHARS = 4_000
MAX_STAGE_CHARS = 1_600
MAX_RESOURCE_CHARS = 12_000


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _safe_file(root: Path, relative: object) -> Path | None:
    if not isinstance(relative, str) or not relative:
        return None
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        return None
    candidate = root / relative_path
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def _canonical_hash(manifest: dict[str, Any], skill_text: str, resources: list[tuple[str, str]]) -> str:
    canonical_manifest = dict(manifest)
    canonical_manifest["resources"] = sorted(
        canonical_manifest.get("resources", []),
        key=lambda item: item.get("id", "") if isinstance(item, dict) else "",
    )
    digest = hashlib.sha256()
    digest.update(json.dumps(canonical_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    digest.update(skill_text.encode("utf-8"))
    for resource_id, content in sorted(resources):
        digest.update(resource_id.encode("utf-8"))
        digest.update(content.encode("utf-8"))
    return digest.hexdigest()


def _stage_sections(markdown: str) -> dict[str, str]:
    matches = list(STAGE_HEADER.finditer(markdown))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        sections[match.group(1)] = markdown[match.end():end].strip()
    return sections


def _audit_pack(manifest_path: Path) -> tuple[str | None, list[str], dict[str, int | str]]:
    violations: list[str] = []
    root = manifest_path.parent
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return None, [f"{root}: invalid skill.json: {error}"], {}
    if not isinstance(manifest, dict):
        return None, [f"{root}: skill.json must be an object"], {}

    skill_id = manifest.get("id")
    if not isinstance(skill_id, str):
        violations.append(f"{root}: missing string id")
        skill_id = None
    prefix = skill_id or str(root)
    if manifest.get("manifest_version") != 2:
        violations.append(f"{prefix}: manifest_version must be 2")
    if manifest.get("version") != "0.2.0":
        violations.append(f"{prefix}: version must be 0.2.0")
    description = manifest.get("description")
    if not isinstance(description, str) or not description or len(description) > 160:
        violations.append(f"{prefix}: description must be 1-160 characters")
    routing = EXPECTED_ROUTING.get(str(skill_id))
    if routing is not None:
        trigger_hash = hashlib.sha256(
            json.dumps(manifest.get("triggers", []), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        actual_routing = (
            manifest.get("applies_to"), manifest.get("stages"), manifest.get("priority"),
            manifest.get("enabled_by_default"), trigger_hash,
        )
        if actual_routing != routing:
            violations.append(f"{prefix}: routing changed from the approved baseline")
    if skill_id in EXPECTED_OUTPUTS and manifest.get("outputs") != EXPECTED_OUTPUTS[skill_id]:
        violations.append(f"{prefix}: outputs changed from the approved baseline")

    skill_path = _safe_file(root, "SKILL.md")
    skill_text = skill_path.read_text(encoding="utf-8") if skill_path else ""
    if not skill_path:
        violations.append(f"{prefix}: missing safe SKILL.md")
    elif len(skill_text) > MAX_SKILL_CHARS:
        violations.append(f"{prefix}: SKILL.md exceeds {MAX_SKILL_CHARS} characters")

    stages = manifest.get("stages")
    if not isinstance(stages, list) or not all(isinstance(stage, str) and stage in VALID_STAGES for stage in stages):
        violations.append(f"{prefix}: manifest stages must use known stage names")
        stages = []
    sections = _stage_sections(skill_text)
    if set(sections) != set(stages):
        violations.append(f"{prefix}: SKILL.md stage coverage {sorted(sections)} != manifest {sorted(stages)}")
    for stage, body in sections.items():
        if stage not in VALID_STAGES:
            violations.append(f"{prefix}: unknown SKILL.md stage {stage}")
        if len(body) > MAX_STAGE_CHARS:
            violations.append(f"{prefix}: stage {stage} exceeds {MAX_STAGE_CHARS} characters")

    resource_contents: list[tuple[str, str]] = []
    resources = manifest.get("resources")
    if not isinstance(resources, list):
        violations.append(f"{prefix}: resources must be a list")
        resources = []
    resource_ids: set[str] = set()
    for resource in resources:
        if not isinstance(resource, dict):
            violations.append(f"{prefix}: resource metadata must be an object")
            continue
        resource_id = resource.get("id")
        if not isinstance(resource_id, str) or not resource_id or resource_id in resource_ids:
            violations.append(f"{prefix}: resource ids must be nonempty and unique")
            continue
        resource_ids.add(resource_id)
        required = ("path", "description", "stages", "when_to_read", "media_type")
        if any(not resource.get(field) for field in required):
            violations.append(f"{prefix}: resource {resource_id} is missing required metadata")
            continue
        path = resource["path"]
        if not isinstance(path, str) or not path.startswith("references/"):
            violations.append(f"{prefix}: resource {resource_id} must live under references/")
            continue
        resource_stages = resource["stages"]
        if not isinstance(resource_stages, list) or not resource_stages or any(stage not in VALID_STAGES for stage in resource_stages):
            violations.append(f"{prefix}: resource {resource_id} has invalid stages")
            resource_stages = []
        if any(stage not in RESOURCE_CONSUMER_STAGES for stage in resource_stages):
            violations.append(
                f"{prefix}: resource {resource_id} is exposed to a stage without an on-demand resource consumer"
            )
        if not set(resource_stages).issubset(set(stages)):
            violations.append(f"{prefix}: resource {resource_id} exposes a stage not selected by the pack")
        resource_path = _safe_file(root, path)
        if not resource_path:
            violations.append(f"{prefix}: resource {resource_id} has an unsafe or missing path")
            continue
        try:
            content = resource_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            violations.append(f"{prefix}: resource {resource_id} is unreadable: {error}")
            continue
        if len(content) > MAX_RESOURCE_CHARS:
            violations.append(f"{prefix}: resource {resource_id} exceeds {MAX_RESOURCE_CHARS} characters")
        if resource["media_type"] == "application/json":
            try:
                json.loads(content)
            except json.JSONDecodeError as error:
                violations.append(f"{prefix}: resource {resource_id} has invalid JSON: {error.msg}")
        resource_contents.append((resource_id, content))

    if skill_id in EXPECTED_RESOURCES and resource_ids != EXPECTED_RESOURCES[skill_id]:
        violations.append(f"{prefix}: resource ids {sorted(resource_ids)} do not match the approved matrix")
    resource_stage_map = {
        str(resource.get("id")): list(resource.get("stages") or [])
        for resource in resources
        if isinstance(resource, dict) and resource.get("id")
    }
    if skill_id in EXPECTED_RESOURCE_STAGES and resource_stage_map != EXPECTED_RESOURCE_STAGES[skill_id]:
        violations.append(f"{prefix}: resource stage map changed from the approved matrix")
    assets = manifest.get("assets", [])
    if not isinstance(assets, list) or not all(isinstance(asset, str) for asset in assets):
        violations.append(f"{prefix}: assets must be a list of relative paths")
        assets = []
    for asset in assets:
        if not _safe_file(root, asset):
            violations.append(f"{prefix}: asset path is unsafe or missing: {asset!r}")
    if skill_id == "poster.visual_recipe":
        required_assets = {"assets/academic_color_palettes.json", "assets/dense_synthesis_targets.json"}
        if not required_assets.issubset(set(assets)):
            violations.append(f"{prefix}: code-read-only palette and dense-target assets must remain assets")

    stats: dict[str, int | str] = {
        "skill_chars": len(skill_text),
        "resource_count": len(resource_contents),
        "resource_chars": sum(len(content) for _, content in resource_contents),
        "hash": _canonical_hash(manifest, skill_text, resource_contents),
    }
    return skill_id, violations, stats


def _audit_registry(
    skills_root: Path,
    expected_ids: set[str],
    expected_hashes: dict[str, str],
) -> list[str]:
    try:
        from autodesign.skills.registry import SkillRegistry
    except ImportError as error:
        return [f"registry import failed: {error}"]
    registry = SkillRegistry.load(skills_root)
    loaded = {pack.id: pack for pack in registry.packs}
    violations: list[str] = []
    if set(loaded) != expected_ids:
        violations.append(f"registry loaded ids {sorted(loaded)} != expected built-ins")
    for skill_id, pack in loaded.items():
        manifest = pack.manifest
        if getattr(manifest, "manifest_version", None) != 2:
            violations.append(f"registry lacks v2 manifest support for {skill_id}")
        if not hasattr(manifest, "resources") or not hasattr(pack, "content_hash"):
            violations.append(f"registry lacks v2 resource/hash support for {skill_id}")
            continue
        if pack.content_hash != expected_hashes.get(skill_id):
            violations.append(f"registry content hash does not match canonical package for {skill_id}")
        for resource in manifest.resources:
            if pack.read_resource(resource.id, resource.stages[0]) is None:
                violations.append(f"registry resource read/hash check failed for {skill_id}:{resource.id}")
    poster_bundle = registry.select(
        brief="Generate a dense academic paper poster from the attached paper.",
        attachments=[Path("paper.pdf")],
        artifact_hint="poster",
    )
    stage_budgets = {"enhance": 8_000, "plan": 12_000, "critique": 6_000, "repair": 8_000}
    for stage, budget in stage_budgets.items():
        rendered_chars = len(poster_bundle.render(stage))
        if rendered_chars > budget:
            violations.append(
                f"paper-poster {stage} runtime context is {rendered_chars} characters; budget is {budget}"
            )
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skills-root", type=Path, default=_repo_root() / "skills")
    args = parser.parse_args()
    skills_root = args.skills_root.resolve()
    manifest_paths = sorted(skills_root.rglob("skill.json"))
    violations: list[str] = []
    seen_ids: set[str] = set()
    hashes: dict[str, str] = {}
    totals = {"skill_chars": 0, "resource_count": 0, "resource_chars": 0}

    for manifest_path in manifest_paths:
        skill_id, pack_violations, stats = _audit_pack(manifest_path)
        violations.extend(pack_violations)
        if skill_id:
            seen_ids.add(skill_id)
        if stats:
            totals["skill_chars"] += int(stats["skill_chars"])
            totals["resource_count"] += int(stats["resource_count"])
            totals["resource_chars"] += int(stats["resource_chars"])
            if skill_id:
                hashes[skill_id] = str(stats["hash"])
            print(f"{skill_id}: skill={stats['skill_chars']} resources={stats['resource_count']} resource_chars={stats['resource_chars']} hash={str(stats['hash'])[:12]}")

    if seen_ids != BUILTIN_IDS or len(manifest_paths) != len(BUILTIN_IDS):
        violations.append(f"built-ins must be exactly the approved 14; found {len(manifest_paths)} manifests and ids {sorted(seen_ids)}")
    violations.extend(_audit_registry(skills_root, BUILTIN_IDS, hashes))
    print(f"TOTAL: packs={len(manifest_paths)} skill_chars={totals['skill_chars']} resources={totals['resource_count']} resource_chars={totals['resource_chars']}")

    if violations:
        print("VIOLATIONS:", file=sys.stderr)
        for violation in violations:
            print(f"- {violation}", file=sys.stderr)
        return 1
    print("Runtime skills audit passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
