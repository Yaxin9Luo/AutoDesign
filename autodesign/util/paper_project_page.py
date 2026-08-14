"""Deterministic enrichments for paper project pages.

Planner output is still authoritative, but paper project pages have a stable
research-site backbone. This module adds missing source-backed evidence modules
from the ingest state so a page does not collapse into short text-only panels
when the planner underuses figures, native tables, or the parsed abstract.
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from ..schema import ArtifactType, DesignSpec, HtmlFrame, LayerNode
from ..tools._contract import ToolContext
from .html_artifact import canonicalize_design_spec
from .io import atomic_write_json
from .logging import log


_VISUAL_TARGET_MIN = 4
_SAMPLE_GALLERY_MAX = 4
_PANEL_TEMPLATE_ROLES = (
    "hero",
    "resources",
    "abstract",
    "framework",
    "findings",
    "demo",
    "benchmark",
    "analysis",
    "citation",
)


def enhance_paper_project_page_spec(
    spec: DesignSpec,
    ctx: ToolContext,
) -> tuple[DesignSpec, list[str]]:
    """Add missing paper-page modules without touching non-paper landings."""

    if not _is_paper_project_page(spec, ctx):
        return spec, []
    if spec.artifact_type != ArtifactType.LANDING:
        return spec, []
    if not spec.layer_graph:
        return spec, []

    working = spec.model_copy(deep=True)
    changes: list[str] = []
    rendered = ctx.state.get("rendered_layers") if isinstance(ctx.state, dict) else {}
    if not isinstance(rendered, dict):
        rendered = {}

    manifest = _first_pdf_manifest(ctx)
    if _split_monolithic_sections(
        working,
        paper_context_confirmed=bool(manifest or ctx.state.get("paper_memory")),
    ):
        changes.append("section_panel_split")
    if _replace_identity_hero_misuse(working, rendered):
        changes.append("identity_hero_replaced")
    if _ensure_abstract_framework(working, manifest, rendered):
        changes.append("abstract_framework")
    if _ensure_native_benchmark_table(working, ctx, rendered):
        changes.append("native_benchmark_table")
    if _ensure_samples_gallery(working, ctx, rendered):
        changes.append("samples_gallery")
    if _ensure_source_visual_density(working, ctx, rendered):
        changes.append("source_visual_density")

    if not changes:
        _persist_paper_project_panel_plan(working, ctx, rendered)
        return spec, []

    enhanced = _sync_html_artifact_from_layer_graph(working, spec)
    _persist_paper_project_panel_plan(enhanced, ctx, rendered)
    ctx.state["paper_project_page_enhancements"] = changes
    log(
        "paper_project_page.enhanced",
        changes=changes,
        sections=len(enhanced.layer_graph or []),
    )
    return enhanced, changes


def _persist_paper_project_panel_plan(
    spec: DesignSpec,
    ctx: ToolContext,
    rendered: dict[str, Any],
) -> None:
    plan = _build_paper_project_panel_plan(spec, ctx, rendered)
    if not plan:
        return
    artifact = getattr(spec, "html_artifact", None)
    theme = getattr(artifact, "theme", None)
    if isinstance(theme, dict):
        theme.setdefault("art_direction", plan.get("selected_art_direction"))
        theme.setdefault("selected_art_direction", plan.get("selected_art_direction"))
    ctx.state["paper_project_panel_plan"] = plan
    try:
        atomic_write_json(ctx.run_dir / "paper_project_panel_plan.json", plan)
    except OSError:
        pass


def _build_paper_project_panel_plan(
    spec: DesignSpec,
    ctx: ToolContext,
    rendered: dict[str, Any],
) -> dict[str, Any]:
    sections = _sections(spec)
    if not sections:
        return {}
    provenance = _paper_visual_provenance_by_id(ctx)
    section_plans = [
        _panel_plan_for_section(section, rendered, provenance)
        for section in sections
    ]
    present_roles = {str(item.get("role") or "") for item in section_plans}
    missing_roles = [
        role for role in _PANEL_TEMPLATE_ROLES
        if role not in present_roles and not (role == "analysis" and "findings" in present_roles)
    ]
    storyboard = ctx.state.get("paper_visual_storyboard") if isinstance(ctx.state, dict) else {}
    resources = ctx.state.get("paper_resources") if isinstance(ctx.state, dict) else {}
    plan = {
        "kind": "paper_project_panel_plan",
        "version": 1,
        "target": "paper_project_page",
        "template_sequence": list(_PANEL_TEMPLATE_ROLES),
        "art_direction_profiles": [
            "light_academic_project",
            "demo_first_gallery",
            "benchmark_dashboard",
            "dark_editorial_research",
            "systems_model_card",
        ],
        "selected_art_direction": _suggest_art_direction(section_plans, storyboard),
        "sections": section_plans,
        "missing_roles": missing_roles,
        "resources": {
            "resource_count": (resources or {}).get("resource_count") if isinstance(resources, dict) else None,
            "chip_count": len((resources or {}).get("resource_chips") or []) if isinstance(resources, dict) else None,
        },
        "material_requirements": {
            "source_visual_provenance_required": True,
            "crop_score_required": True,
            "dedupe_required": True,
            "demo_gallery_min_items": 3,
            "wide_table_cols_local_scroll_threshold": 6,
        },
        "interaction_contract": _interaction_contract(section_plans),
        "next_agents": [
            "aesthetic_reviewer",
            "text_evidence_reviewer",
            "layout_reviewer",
            "material_reviewer",
            "iteration_director",
        ],
        "tool_boundaries": {
            "generative_visual_tool": (
                "decorative backgrounds/icons/frames only; never replaces paper figures, "
                "benchmark plots, table values, or scientific diagrams"
            ),
        },
    }
    log(
        "paper_project_page.panel_plan",
        sections=len(section_plans),
        missing_roles=missing_roles,
        art_direction=plan["selected_art_direction"],
    )
    return plan


def _panel_plan_for_section(
    section: LayerNode,
    rendered: dict[str, Any],
    provenance: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    children = list(section.children or [])
    role = _section_role(section)
    source_figure_ids = [
        str(child.layer_id)
        for child in children
        if child.kind == "image"
        and _verified_paper_source_node(child, rendered, provenance)
    ]
    source_table_ids = [
        str(child.layer_id)
        for child in children
        if child.kind == "table"
        and _verified_paper_source_node(child, rendered, provenance)
    ]
    sortable_table_ids = [
        str(child.layer_id)
        for child in children
        if str(child.layer_id) in source_table_ids
        and _table_values_permit_sorting(child)
    ]
    source_visuals = [
        _source_visual_summary(layer_id, rendered)
        for layer_id in [*source_figure_ids, *source_table_ids]
    ]
    table_cols = [
        len(child.headers or (child.rows[0] if child.rows else []))
        for child in children
        if child.kind == "table"
    ]
    return {
        "section_id": section.layer_id,
        "name": section.name,
        "role": role,
        "template": _template_for_role(role),
        "text_blocks": len([child for child in children if child.kind == "text"]),
        "image_blocks": len([child for child in children if child.kind == "image"]),
        "table_blocks": len([child for child in children if child.kind == "table"]),
        "source_image_blocks": len(source_figure_ids),
        "sortable_table_blocks": len(sortable_table_ids),
        "source_figure_ids": source_figure_ids,
        "sortable_table_ids": sortable_table_ids,
        "source_visuals": source_visuals,
        "max_table_cols": max(table_cols or [0]),
        "warnings": _panel_plan_warnings(role, children, table_cols),
    }


def _interaction_contract(section_plans: list[dict[str, Any]]) -> dict[str, Any]:
    source_figure_ids = _dedupe([
        str(source_id)
        for item in section_plans
        for source_id in (item.get("source_figure_ids") or [])
    ])
    sortable_table_ids = _dedupe([
        str(source_id)
        for item in section_plans
        for source_id in (item.get("sortable_table_ids") or [])
    ])
    selected: list[str] = []
    if source_figure_ids:
        selected.append("source_figure_focus_viewer")
    if sortable_table_ids:
        selected.append("sortable_result_table")
    return {
        "version": 1,
        "source_grounded_required": True,
        "available_affordances": {
            "source_figures": len(source_figure_ids),
            "sortable_tables": len(sortable_table_ids),
        },
        "eligible_source_ids": {
            "source_figure_focus_viewer": source_figure_ids,
            "sortable_result_table": sortable_table_ids,
        },
        "selected": selected,
        "accessibility": [
            "keyboard_operable",
            "reduced_motion_safe",
        ],
        "status": "selected" if selected else "source_affordance_unavailable",
    }


def is_verified_paper_source_node(node: LayerNode, ctx: ToolContext) -> bool:
    """Return whether a source interaction is backed by current ingest state."""

    rendered = ctx.state.get("rendered_layers") if isinstance(ctx.state, dict) else {}
    if not isinstance(rendered, dict):
        rendered = {}
    return _verified_paper_source_node(
        node,
        rendered,
        _paper_visual_provenance_by_id(ctx),
    )


def _verified_paper_source_node(
    node: LayerNode,
    rendered: dict[str, Any],
    provenance: dict[str, dict[str, Any]],
) -> bool:
    layer_id = str(node.layer_id or "")
    rec = rendered.get(layer_id) if isinstance(rendered, dict) else None
    asset = provenance.get(layer_id) if isinstance(provenance, dict) else None
    records = [item for item in (rec, asset) if isinstance(item, dict) and item]
    if node.kind == "image":
        if not layer_id.startswith("ingest_fig_") or not records:
            return False
        return any(
            str(item.get("kind") or "").lower() in {"", "image", "figure"}
            and bool(
                item.get("src_path")
                or item.get("output_sha256")
                or item.get("source_page") is not None
            )
            for item in records
        )
    if node.kind != "table" or not layer_id.startswith("ingest_table_"):
        return False
    return any(_table_matches_source_record(node, item) for item in records)


def _table_matches_source_record(table: LayerNode, record: dict[str, Any]) -> bool:
    if str(record.get("kind") or "").lower() not in {"", "table"}:
        return False
    source_headers = [str(value) for value in (record.get("headers") or [])]
    source_rows = [[str(value) for value in row] for row in (record.get("rows") or [])]
    if not source_headers and not source_rows:
        return False
    table_headers = [str(value) for value in (table.headers or [])]
    table_rows = [[str(value) for value in row] for row in (table.rows or [])]
    return table_headers == source_headers and table_rows == source_rows


def _table_values_permit_sorting(table: LayerNode) -> bool:
    rows = list(table.rows or [])
    if len(rows) < 2:
        return False
    column_count = max(
        len(table.headers or []),
        max((len(row) for row in rows), default=0),
    )
    for column in range(column_count):
        values = {
            str(row[column]).strip()
            for row in rows
            if column < len(row) and str(row[column]).strip()
        }
        if len(values) >= 2:
            return True
    return False


def _source_visual_summary(layer_id: str, rendered: dict[str, Any]) -> dict[str, Any]:
    rec = rendered.get(layer_id) if isinstance(rendered, dict) else {}
    if not isinstance(rec, dict):
        rec = {}
    return {
        "source_id": layer_id,
        "visual_role": rec.get("visual_role"),
        "caption": rec.get("caption_short") or rec.get("caption") or rec.get("title"),
        "image_size": rec.get("image_size"),
        "source_page": rec.get("source_page"),
    }


def _section_role(section: LayerNode) -> str:
    blob = " ".join(
        str(getattr(section, field, "") or "").lower()
        for field in ("layer_id", "name", "role")
    )
    text = " ".join(
        str(child.text or "").lower()
        for child in (section.children or [])
        if child.kind == "text"
    )[:600]
    combined = f"{blob} {text}"
    if any(key in combined for key in ("resource", "links", "github", "hugging", "arxiv", "code")):
        return "resources"
    if any(key in combined for key in ("abstract", "overview")):
        return "abstract"
    if any(key in combined for key in ("framework", "method", "architecture", "pipeline", "system")):
        return "framework"
    if any(key in combined for key in ("demo", "sample", "gallery", "qualitative", "example")):
        return "demo"
    if any(key in combined for key in ("benchmark", "result", "ablation", "leaderboard", "experiment")):
        return "benchmark"
    if any(key in combined for key in ("analysis", "discussion", "reflection", "limitation")):
        return "analysis"
    if any(key in combined for key in ("citation", "bibtex", "cite", "footer", "license")):
        return "citation"
    if any(key in combined for key in ("finding", "takeaway", "conclusion")):
        return "findings"
    return "hero" if int(section.z_index or 0) <= 1 else "content"


def _template_for_role(role: str) -> str:
    return {
        "hero": "hero_identity_teaser",
        "resources": "resource_chip_row",
        "abstract": "abstract_narrative_plus_framework",
        "framework": "method_visual_explainer",
        "findings": "result_dashboard",
        "demo": "demo_gallery_or_case_strip",
        "benchmark": "benchmark_table_with_callouts",
        "analysis": "analysis_notes_with_visual_anchor",
        "citation": "citation_resource_footer",
    }.get(role, "evidence_panel")


def _panel_plan_warnings(role: str, children: list[LayerNode], table_cols: list[int]) -> list[str]:
    warnings: list[str] = []
    has_visual = any(child.kind in {"image", "table"} for child in children)
    if role in {"framework", "findings", "demo", "benchmark"} and not has_visual:
        warnings.append("evidence_panel_without_visual_anchor")
    if role == "demo" and len([child for child in children if child.kind == "image"]) < 3:
        warnings.append("demo_gallery_underbuilt")
    if any(cols > 6 for cols in table_cols):
        warnings.append("wide_table_requires_summary_or_local_scroll")
    return warnings


def _suggest_art_direction(section_plans: list[dict[str, Any]], storyboard: Any) -> str:
    roles = {str(item.get("role") or "") for item in section_plans}
    if "demo" in roles:
        return "demo_first_gallery"
    if "benchmark" in roles and any((item.get("max_table_cols") or 0) >= 5 for item in section_plans):
        return "benchmark_dashboard"
    if isinstance(storyboard, dict):
        selected = " ".join(
            str(item.get("story_role") or "")
            for item in storyboard.get("selected_assets") or []
            if isinstance(item, dict)
        ).lower()
        if "analysis" in selected or "systems" in selected:
            return "systems_model_card"
    return "light_academic_project"


def _is_paper_project_page(spec: DesignSpec, ctx: ToolContext) -> bool:
    artifact = getattr(spec, "html_artifact", None)
    theme = getattr(artifact, "theme", None)
    if isinstance(theme, dict):
        raw = str(
            theme.get("page_subtype")
            or theme.get("landing_subtype")
            or theme.get("subtype")
            or ""
        ).lower()
        if raw in {"paper_project_page", "research_project_page", "paper_page"}:
            return True
    brief = " ".join(
        str(value or "").lower()
        for value in (
            getattr(spec, "brief", None),
            ctx.state.get("raw_user_brief") if isinstance(ctx.state, dict) else None,
            ctx.state.get("run_brief") if isinstance(ctx.state, dict) else None,
        )
    )
    return any(marker in brief for marker in (
        "paper project page",
        "paper page",
        "paper-to-page",
        "paper to page",
        "project page",
        "website",
        "webpage",
        "网页",
        "项目页",
        "论文页面",
    ))


def _first_pdf_manifest(ctx: ToolContext) -> dict[str, Any]:
    ingested = ctx.state.get("ingested") if isinstance(ctx.state, dict) else None
    if not isinstance(ingested, list):
        return {}
    for summary in ingested:
        if not isinstance(summary, dict) or summary.get("type") != "pdf":
            continue
        manifest = summary.get("manifest")
        if isinstance(manifest, dict):
            return manifest
    return {}


def _ensure_abstract_framework(
    spec: DesignSpec,
    manifest: dict[str, Any],
    rendered: dict[str, Any],
) -> bool:
    abstract = _clean_sentence_block(manifest.get("abstract") or "", max_chars=900)
    if not abstract:
        return False

    sections = _sections(spec)
    existing = _find_section(sections, ("abstract", "overview"))
    if existing is not None:
        if not _section_has_abstract_copy(existing):
            existing.children.insert(1, _text_node(
                "paper_abstract",
                "abstract",
                abstract,
                z=2,
                role="abstract",
                font_size=18,
                weight=430,
                line_height=1.72,
            ))
            return True
        return False

    method_visual = _first_available_visual(
        rendered,
        _paper_visual_ids_by_role(spec, rendered, roles=("method", "hero_method", "key_mechanism", "fallback")),
        used=_used_source_ids(spec),
    )
    children = [
        _text_node("abstract_framework_heading", "section_heading", "Abstract & Framework", z=1, role="heading"),
        _text_node("paper_abstract", "abstract", abstract, z=2, role="abstract", font_size=18, weight=430, line_height=1.72),
    ]
    if method_visual:
        children.append(_image_node(method_visual, "framework_visual", rendered, z=3))
        caption = _caption_for_visual(method_visual, rendered)
        if caption:
            children.append(_text_node(
                "abstract_framework_caption",
                "figure_caption",
                caption,
                z=4,
                role="caption",
                font_size=13,
                weight=420,
                line_height=1.45,
            ))

    section = _section_node(
        "abstract_framework",
        "abstract_framework",
        children,
        z=_next_section_z(spec),
    )
    _insert_after_hero_or_resources(spec, section)
    return True


def _ensure_native_benchmark_table(
    spec: DesignSpec,
    ctx: ToolContext,
    rendered: dict[str, Any],
) -> bool:
    if _has_kind(spec, "table"):
        return False

    table_ids = _available_table_ids(ctx, rendered)
    children = [
        _text_node("benchmarks_heading", "section_heading", "Results & Benchmarks", z=1, role="heading"),
    ]
    if table_ids:
        for idx, table_id in enumerate(table_ids[:2], start=1):
            children.append(_table_node(table_id, rendered, z=idx + 1))
    else:
        generated = _generated_result_table(ctx)
        if not generated:
            return False
        children.append(generated)

    section = _find_section(_sections(spec), ("benchmark", "result", "leaderboard"))
    if section is None:
        section = _section_node("benchmarks", "benchmarks", [], z=_next_section_z(spec))
        _insert_before_citation(spec, section)
    section.children.extend(children)
    return True


def _ensure_source_visual_density(
    spec: DesignSpec,
    ctx: ToolContext,
    rendered: dict[str, Any],
) -> bool:
    available = _paper_visual_ids_by_role(
        spec,
        rendered,
        roles=("method", "hero_method", "key_mechanism", "evidence", "main_evidence", "qualitative", "fallback"),
    )
    available = [vid for vid in available if _visual_kind(vid, rendered) == "image"]
    used = _used_source_ids(spec)
    target = min(max(_VISUAL_TARGET_MIN, len(available[:_VISUAL_TARGET_MIN])), len(available))
    if len([vid for vid in used if vid in available]) >= target:
        return False

    missing = [vid for vid in available if vid not in used][:_SAMPLE_GALLERY_MAX]
    if not missing:
        return False

    framework = _find_section(_sections(spec), ("framework", "method", "architecture", "pipeline"))
    changed = False
    if framework is not None and not any(child.kind == "image" for child in framework.children or []):
        visual_id = missing.pop(0)
        framework.children.append(_image_node(visual_id, "framework_visual", rendered, z=len(framework.children) + 1))
        caption = _caption_for_visual(visual_id, rendered)
        if caption:
            framework.children.append(_text_node(
                f"{visual_id}_caption",
                "figure_caption",
                caption,
                z=len(framework.children) + 1,
                role="caption",
                font_size=13,
                line_height=1.45,
            ))
        changed = True

    if missing:
        gallery = _find_section(_sections(spec), ("evidence_gallery", "visual_evidence"))
        if gallery is None:
            gallery = _section_node("visual_evidence", "visual_evidence", [
                _text_node("visual_evidence_heading", "section_heading", "Visual Evidence", z=1, role="heading"),
                _text_node(
                    "visual_evidence_intro",
                    "body",
                    "Additional source figures surface the paper's method, qualitative examples, and quantitative evidence.",
                    z=2,
                    role="body",
                    font_size=16,
                    line_height=1.6,
                ),
            ], z=_next_section_z(spec))
            _insert_before_citation(spec, gallery)
        start_z = len(gallery.children) + 1
        for idx, visual_id in enumerate(missing, start=0):
            gallery.children.append(_image_node(visual_id, "evidence_image", rendered, z=start_z + idx * 2))
            caption = _caption_for_visual(visual_id, rendered)
            if caption:
                gallery.children.append(_text_node(
                    f"{visual_id}_caption",
                    "figure_caption",
                    caption,
                    z=start_z + idx * 2 + 1,
                    role="caption",
                    font_size=13,
                    line_height=1.45,
                ))
        changed = True
    return changed


def _ensure_samples_gallery(
    spec: DesignSpec,
    ctx: ToolContext,
    rendered: dict[str, Any],
) -> bool:
    if _find_section(_sections(spec), ("sample", "samples", "demo", "gallery", "qualitative")) is not None:
        return False
    if _has_section_heading(spec, ("sample", "samples", "demo", "gallery", "qualitative")):
        return False
    raw_candidates = _paper_visual_ids_by_role(
        spec,
        rendered,
        roles=("qualitative", "demo", "evidence", "fallback"),
    )
    candidates = _select_sample_visual_ids(
        raw_candidates,
        rendered,
        ctx,
        used=_used_source_ids(spec),
        limit=_SAMPLE_GALLERY_MAX,
    )
    if len(candidates) < 3:
        return False
    children = [
        _text_node("samples_heading", "section_heading", "Samples & Demos", z=1, role="heading"),
        _text_node(
            "samples_intro",
            "body",
            "Representative examples from the paper make the qualitative behavior inspectable without opening the PDF.",
            z=2,
            role="body",
            font_size=16,
            line_height=1.6,
        ),
    ]
    for idx, visual_id in enumerate(candidates, start=1):
        children.append(_image_node(visual_id, "sample_image", rendered, z=idx * 2 + 1))
        caption = _caption_for_visual(visual_id, rendered)
        if caption:
            children.append(_text_node(
                f"{visual_id}_sample_caption",
                "figure_caption",
                caption,
                z=idx * 2 + 2,
                role="caption",
                font_size=13,
                line_height=1.45,
            ))
    _insert_before_citation(
        spec,
        _section_node("samples_gallery", "samples_gallery", children, z=_next_section_z(spec)),
    )
    return True


def _select_sample_visual_ids(
    candidate_ids: list[str],
    rendered: dict[str, Any],
    ctx: ToolContext,
    *,
    used: set[str],
    limit: int,
) -> list[str]:
    provenance = _paper_visual_provenance_by_id(ctx)
    selected: list[str] = []
    seen_hashes: set[str] = set()
    for layer_id in candidate_ids:
        if layer_id in used or _visual_kind(layer_id, rendered) != "image":
            continue
        material = _visual_material_quality(layer_id, rendered, provenance)
        if _safe_float(material.get("material_score"), 1.0) < 0.55:
            continue
        rec = rendered.get(layer_id) if isinstance(rendered, dict) else {}
        asset = provenance.get(layer_id, {})
        digest = str(
            (asset if isinstance(asset, dict) else {}).get("output_sha256")
            or (rec if isinstance(rec, dict) else {}).get("sha256")
            or ""
        )
        if digest and digest in seen_hashes:
            continue
        if digest:
            seen_hashes.add(digest)
        selected.append(layer_id)
        if len(selected) >= limit:
            break
    return selected


def _split_monolithic_sections(
    spec: DesignSpec,
    *,
    paper_context_confirmed: bool = False,
) -> bool:
    """Turn one giant generated page frame into explicit project-page panels."""

    shell_changed = _normalize_paper_landing_shell_frames(
        spec,
        paper_context_confirmed=paper_context_confirmed,
    )
    if _promote_semantic_html_groups(
        spec,
        paper_context_confirmed=paper_context_confirmed,
    ):
        canonicalize_design_spec(spec, prefer_html_artifact=True)
        return True
    if not paper_context_confirmed and not _has_explicit_paper_page_subtype(spec):
        return shell_changed
    if shell_changed:
        canonicalize_design_spec(spec, prefer_html_artifact=True)
    if not spec.layer_graph:
        return shell_changed
    new_graph: list[LayerNode] = []
    changed = False
    used_ids: set[str] = set()
    for section in spec.layer_graph:
        if section.kind != "section":
            new_graph.append(section)
            used_ids.add(str(section.layer_id))
            continue
        children = list(section.children or [])
        heading_indexes = [idx for idx, child in enumerate(children) if _is_section_heading(child)]
        if len(heading_indexes) < 2:
            new_graph.append(section)
            used_ids.add(str(section.layer_id))
            continue

        changed = True
        base_z = int(section.z_index or len(new_graph))
        prefix = children[: heading_indexes[0]]
        next_z = base_z
        if prefix:
            hero_id = _unique_layer_id("paper_project_hero", used_ids)
            new_graph.append(_section_like(
                section,
                layer_id=hero_id,
                name="paper_project_hero",
                children=prefix,
                z=next_z,
            ))
            next_z += 1

        for chunk_idx, start in enumerate(heading_indexes):
            end = heading_indexes[chunk_idx + 1] if chunk_idx + 1 < len(heading_indexes) else len(children)
            chunk = children[start:end]
            heading = chunk[0]
            raw_name = str(getattr(heading, "text", None) or getattr(heading, "name", None) or heading.layer_id)
            section_id = _unique_layer_id(_slug(raw_name) or f"{section.layer_id}_{chunk_idx}", used_ids)
            new_graph.append(_section_like(
                section,
                layer_id=section_id,
                name=section_id,
                children=chunk,
                z=next_z,
            ))
            next_z += 1
    if changed:
        spec.layer_graph = new_graph
    return changed or shell_changed


_SEMANTIC_SECTION_TOKENS = {
    "hero", "identity", "overview", "abstract", "introduction", "problem",
    "motivation", "framework", "method", "methodology", "architecture",
    "approach", "details", "findings", "results", "experiments", "benchmark",
    "analysis", "demo", "samples", "resources", "citation", "limitations",
    "conclusion",
}
_LAYOUT_GROUP_TOKENS = {
    "copy", "visual", "media", "content", "body", "grid", "row", "column",
    "stack", "wrapper", "container", "left", "right",
}


def _has_explicit_paper_page_subtype(spec: DesignSpec) -> bool:
    artifact = spec.html_artifact
    theme = artifact.theme if artifact is not None and isinstance(artifact.theme, dict) else {}
    return str(
        theme.get("page_subtype")
        or theme.get("landing_subtype")
        or theme.get("subtype")
        or ""
    ).lower() in {"paper_project_page", "research_project_page", "paper_page"}


def _normalize_paper_landing_shell_frames(
    spec: DesignSpec,
    *,
    paper_context_confirmed: bool,
) -> bool:
    artifact = spec.html_artifact
    if (
        artifact is None
        or str(getattr(spec.artifact_type, "value", spec.artifact_type) or "").lower() != "landing"
        or not _paper_landing_shell_normalization_allowed(
            spec,
            paper_context_confirmed=paper_context_confirmed,
        )
    ):
        return False
    theme = artifact.theme if isinstance(artifact.theme, dict) else {}
    if any(theme.get(key) for key in ("_autodesign_legacy_source", "_designanything_legacy_source")):
        return False
    frames = list(artifact.frames or [])
    removable = [
        frame for frame in frames
        if _is_placeholder_shell_frame(frame) or _is_navigation_only_frame(frame)
    ]
    if not removable or len(removable) == len(frames):
        return False
    retained = [frame for frame in frames if frame not in removable]
    navigation_blocks = [
        block.model_copy(deep=True)
        for frame in removable
        if _is_navigation_only_frame(frame)
        for block in list(frame.blocks or [])
    ]
    if navigation_blocks:
        target_idx = next(
            (idx for idx, frame in enumerate(retained) if _frame_has_substantive_content(frame)),
            None,
        )
        if target_idx is None:
            retained = [frame for frame in frames if not _is_placeholder_shell_frame(frame)]
        else:
            retained[target_idx] = retained[target_idx].model_copy(
                update={"blocks": [*navigation_blocks, *list(retained[target_idx].blocks or [])]},
                deep=True,
            )
    artifact.frames = retained
    return True


def _is_placeholder_shell_frame(frame: HtmlFrame) -> bool:
    if (
        list(frame.blocks or [])
        or frame.render_mode == "authored_html"
        or str(frame.authored_body_html or "").strip()
        or str(frame.authored_css or "").strip()
        or frame.layout_plan is not None
    ):
        return False
    tokens = {
        _slug(str(value or ""))
        for value in (frame.frame_id, frame.role, frame.title)
        if str(value or "").strip()
    }
    return not tokens or bool(tokens & {
        "page", "page_root", "paper_project_page", "landing", "landing_page", "root",
    })


def _is_navigation_only_frame(frame: HtmlFrame) -> bool:
    if (
        frame.render_mode == "authored_html"
        or str(frame.authored_body_html or "").strip()
        or str(frame.authored_css or "").strip()
        or frame.layout_plan is not None
    ):
        return False
    blocks = list(frame.blocks or [])
    if not blocks:
        return False
    allowed_roles = {"brand", "nav", "navigation", "nav_link", "menu", "menu_item"}
    return all(
        _slug(str(block.role or block.block_id or "")) in allowed_roles
        for block in blocks
    )


def _paper_landing_shell_normalization_allowed(
    spec: DesignSpec,
    *,
    paper_context_confirmed: bool,
) -> bool:
    if _has_explicit_paper_page_subtype(spec):
        return True
    if not paper_context_confirmed:
        return False
    brief = str(getattr(spec, "brief", None) or "").lower()
    return any(marker in brief for marker in (
        "paper project",
        "research project",
        "paper page",
        "paper website",
        "research website",
        "academic paper",
        "publication page",
        "论文",
        "研究项目",
    ))


def _frame_has_substantive_content(frame: HtmlFrame) -> bool:
    if str(frame.authored_body_html or "").strip():
        return True
    substantive_kinds = {"text", "image", "table", "metric", "quote", "caption", "chart", "embed"}
    pending = list(frame.blocks or [])
    while pending:
        block = pending.pop()
        if str(block.kind or "") in substantive_kinds:
            return True
        pending.extend(list(block.children or []))
    return False


def _promote_semantic_html_groups(
    spec: DesignSpec,
    *,
    paper_context_confirmed: bool,
) -> bool:
    artifact = spec.html_artifact
    artifact_target = str(getattr(spec.artifact_type, "value", spec.artifact_type) or "").lower()
    declared_target = str(getattr(artifact, "target", None) or "").lower()
    if artifact is None or artifact_target != "landing" or declared_target not in {"", "landing"}:
        return False
    theme = artifact.theme if isinstance(artifact.theme, dict) else {}
    explicit_paper_subtype = _has_explicit_paper_page_subtype(spec)
    if not explicit_paper_subtype and not paper_context_confirmed:
        return False
    if any(theme.get(key) for key in ("_autodesign_legacy_source", "_designanything_legacy_source")):
        return False

    promoted_frames: list[HtmlFrame] = []
    changed = False
    for frame in artifact.frames:
        if (
            frame.render_mode == "authored_html"
            or frame.authored_body_html
            or frame.authored_css
            or frame.layout_plan is not None
        ):
            promoted_frames.append(frame)
            continue
        blocks = list(frame.blocks or [])
        wrapper_style: dict[str, Any] = {}
        if len(blocks) == 1 and blocks[0].kind == "group":
            nested_blocks = list(blocks[0].children or [])
            nested_semantic_count = sum(
                1 for block in nested_blocks
                if (
                    block.kind == "group"
                    and _semantic_group_role(block)
                    and _group_has_substantive_content(block)
                )
            )
            if nested_semantic_count >= 3:
                wrapper_style = dict(blocks[0].style or {})
                blocks = nested_blocks
        semantic = [
            (idx, block, _semantic_group_role(block))
            for idx, block in enumerate(blocks)
            if (
                block.kind == "group"
                and _semantic_group_role(block)
                and _group_has_substantive_content(block)
            )
        ]
        if len(semantic) < 3 or len({role for _, _, role in semantic}) < 3:
            promoted_frames.append(frame)
            continue

        changed = True
        first_idx = semantic[0][0]
        prefix_blocks = [item.model_copy(deep=True) for item in blocks[:first_idx]]
        for semantic_idx, (block_idx, block, role) in enumerate(semantic):
            next_idx = semantic[semantic_idx + 1][0] if semantic_idx + 1 < len(semantic) else len(blocks)
            section_blocks = [
                *(prefix_blocks if semantic_idx == 0 else []),
                block.model_copy(deep=True),
                *[item.model_copy(deep=True) for item in blocks[block_idx + 1:next_idx]],
            ]
            promoted_frames.append(HtmlFrame(
                frame_id=str(block.block_id or f"{frame.frame_id}_{semantic_idx + 1:02d}"),
                kind="section",
                role=role,
                title=block.title,
                subtitle=frame.subtitle,
                layout=frame.layout,
                source=frame.source,
                style={**dict(frame.style or {}), **wrapper_style},
                blocks=section_blocks,
            ))
    if changed:
        artifact.frames = [
            frame for frame in promoted_frames
            if not _is_empty_shell_frame(frame)
        ]
    return changed


def _is_empty_shell_frame(frame: HtmlFrame) -> bool:
    return not (
        list(frame.blocks or [])
        or str(frame.title or "").strip()
        or str(frame.subtitle or "").strip()
        or str(frame.authored_body_html or "").strip()
    )


def _semantic_group_role(block: Any) -> str:
    raw = " ".join(
        str(value or "").strip().lower()
        for value in (
            getattr(block, "role", None),
            getattr(block, "panel_role", None),
            getattr(block, "block_id", None),
            getattr(block, "title", None),
        )
    )
    tokens = [token for token in re.split(r"[^a-z0-9]+", raw) if token]
    if any(token in _LAYOUT_GROUP_TOKENS for token in tokens) and not any(
        token in _SEMANTIC_SECTION_TOKENS for token in tokens
    ):
        return ""
    for token in tokens:
        if token in _SEMANTIC_SECTION_TOKENS:
            return token
        if token.endswith("s") and token[:-1] in _SEMANTIC_SECTION_TOKENS:
            return token[:-1]
    return ""


def _group_has_substantive_content(block: Any) -> bool:
    for child in list(getattr(block, "children", None) or []):
        kind = str(getattr(child, "kind", None) or "")
        if kind == "group" and _group_has_substantive_content(child):
            return True
        if kind in {"text", "metric", "quote", "caption"} and str(
            getattr(child, "text", None) or getattr(child, "title", None) or ""
        ).strip():
            return True
        if kind in {"image", "chart", "embed"} and any(
            str(getattr(child, field, None) or "").strip()
            for field in ("src_path", "source_id", "asset_id", "href")
        ):
            return True
        if kind == "table" and (
            list(getattr(child, "rows", None) or [])
            or list(getattr(child, "headers", None) or [])
        ):
            return True
    return False


def _replace_identity_hero_misuse(spec: DesignSpec, rendered: dict[str, Any]) -> bool:
    sections = _sections(spec)
    if not sections:
        return False
    candidates = _paper_visual_ids_by_role(
        spec,
        rendered,
        roles=("method", "hero_method", "key_mechanism", "fallback"),
    )
    used = _used_source_ids(spec)
    changed = False
    for section in sections[:1]:
        children = list(section.children or [])
        for idx, child in enumerate(children):
            if child.kind != "image" or not _looks_like_identity_logo(child):
                continue
            replacement = _first_available_visual(rendered, candidates, used=used)
            if not replacement:
                continue
            children[idx] = _image_node(
                replacement,
                "hero_visual",
                rendered,
                z=int(child.z_index or idx + 1),
            )
            used.add(replacement)
            section.children = children
            changed = True
            break
    return changed


def _sync_html_artifact_from_layer_graph(
    spec: DesignSpec,
    original: DesignSpec,
) -> DesignSpec:
    old_theme = {}
    if getattr(original, "html_artifact", None) is not None:
        theme = getattr(original.html_artifact, "theme", None)
        if isinstance(theme, dict):
            old_theme = deepcopy(theme)
    synced = canonicalize_design_spec(spec, prefer_html_artifact=False)
    if synced.html_artifact is not None:
        synced.html_artifact.target = "landing"
        synced.html_artifact.theme.update(old_theme)
        synced.html_artifact.theme["page_subtype"] = "paper_project_page"
        synced.html_artifact.theme["paper_project_enhanced"] = True
    return synced


def _sections(spec: DesignSpec) -> list[LayerNode]:
    return [node for node in list(spec.layer_graph or []) if node.kind == "section"]


def _find_section(sections: list[LayerNode], keys: tuple[str, ...]) -> LayerNode | None:
    for section in sections:
        if _node_matches(section, keys):
            return section
    return None


def _node_matches(node: Any, keys: tuple[str, ...]) -> bool:
    blob = " ".join(str(getattr(node, field, "") or "").lower() for field in ("layer_id", "name", "role", "text"))
    return any(key in blob for key in keys)


def _is_section_heading(node: LayerNode) -> bool:
    if node.kind != "text":
        return False
    id_name = " ".join(str(getattr(node, field, "") or "").lower() for field in ("layer_id", "name"))
    if "section_heading" in id_name:
        return True
    return any(part.endswith("_heading") or part.endswith("-heading") for part in id_name.split())


def _section_has_abstract_copy(section: LayerNode) -> bool:
    for child in section.children or []:
        if child.kind != "text":
            continue
        if _is_section_heading(child):
            continue
        text = str(child.text or "").strip()
        if len(text) >= 140 and _node_matches(child, ("abstract", "overview", "framework", "intro")):
            return True
    return False


def _has_section_heading(spec: DesignSpec, keys: tuple[str, ...]) -> bool:
    return any(
        _is_section_heading(child) and _node_matches(child, keys)
        for section in _sections(spec)
        for child in (section.children or [])
    )


def _has_kind(spec: DesignSpec, kind: str) -> bool:
    return any(child.kind == kind for section in _sections(spec) for child in (section.children or []))


def _used_source_ids(spec: DesignSpec) -> set[str]:
    used: set[str] = set()
    for section in _sections(spec):
        for child in section.children or []:
            if child.kind in {"image", "table"}:
                used.add(str(child.layer_id))
                if getattr(child, "src_path", None):
                    used.add(str(child.src_path))
    return used


def _paper_visual_provenance_by_id(ctx: ToolContext) -> dict[str, dict[str, Any]]:
    provenance = ctx.state.get("paper_visual_provenance") if isinstance(ctx.state, dict) else {}
    if not isinstance(provenance, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for item in provenance.get("assets") or []:
        if isinstance(item, dict) and str(item.get("asset_id") or ""):
            out[str(item.get("asset_id"))] = item
    return out


def _visual_material_quality(
    layer_id: str,
    rendered: dict[str, Any],
    provenance: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    asset = provenance.get(layer_id)
    if isinstance(asset, dict) and isinstance(asset.get("material_quality"), dict):
        return asset.get("material_quality") or {}
    rec = rendered.get(layer_id) if isinstance(rendered, dict) else {}
    if isinstance(rec, dict) and isinstance(rec.get("material_quality"), dict):
        return rec.get("material_quality") or {}
    return {}


def _paper_visual_ids_by_role(
    spec: DesignSpec,
    rendered: dict[str, Any],
    *,
    roles: tuple[str, ...],
) -> list[str]:
    del spec
    role_set = {role.lower() for role in roles}
    ids = []
    for layer_id, rec in rendered.items():
        if not isinstance(rec, dict) or not str(layer_id).startswith(("ingest_fig_", "ingest_table_")):
            continue
        role = str(rec.get("visual_role") or "").lower()
        caption = " ".join(str(rec.get(k) or "").lower() for k in ("caption", "caption_short", "name", "title"))
        if role in role_set or any(role_key in caption for role_key in role_set) or "fallback" in role_set:
            ids.append(str(layer_id))
    return _dedupe(ids)


def _available_table_ids(ctx: ToolContext, rendered: dict[str, Any]) -> list[str]:
    storyboard_ids = [
        str(item.get("asset_id") or "")
        for item in ((ctx.state.get("paper_visual_storyboard") or {}).get("selected_assets") or [])
        if isinstance(item, dict)
    ]
    ids = storyboard_ids + [
        str(layer_id) for layer_id, rec in rendered.items()
        if str(layer_id).startswith("ingest_table_")
        and isinstance(rec, dict)
        and (rec.get("rows") or rec.get("headers"))
    ]
    return [
        layer_id for layer_id in _dedupe(ids)
        if _visual_kind(layer_id, rendered) == "table"
    ]


def _first_available_visual(
    rendered: dict[str, Any],
    candidate_ids: list[str],
    *,
    used: set[str],
) -> str:
    for layer_id in candidate_ids:
        if layer_id in used:
            continue
        if _visual_kind(layer_id, rendered) == "image":
            return layer_id
    return ""


def _visual_kind(layer_id: str, rendered: dict[str, Any]) -> str:
    rec = rendered.get(layer_id) if isinstance(rendered, dict) else None
    if not isinstance(rec, dict):
        return ""
    if str(rec.get("kind") or "") == "table" or layer_id.startswith("ingest_table_"):
        return "table"
    if rec.get("src_path"):
        return "image"
    return ""


def _generated_result_table(ctx: ToolContext) -> LayerNode | None:
    rows: list[list[str]] = []
    units = ctx.state.get("paper_memory_dossier") if isinstance(ctx.state, dict) else None
    if isinstance(units, dict):
        for section in units.get("sections") or []:
            if not isinstance(section, dict):
                continue
            blob = " ".join(str(section.get(k) or "") for k in ("title", "summary", "takeaway"))
            if _looks_quantitative(blob):
                rows.append([_short_cell(section.get("title") or "Finding"), _short_cell(blob, 140)])
            for item in section.get("evidence") or section.get("bullets") or []:
                text = str(item.get("text") if isinstance(item, dict) else item)
                if _looks_quantitative(text):
                    rows.append([_short_cell(section.get("title") or "Evidence"), _short_cell(text, 140)])
                if len(rows) >= 5:
                    break
            if len(rows) >= 5:
                break
    if not rows:
        recommended = ctx.state.get("poster_content_brief") if isinstance(ctx.state, dict) else None
        if isinstance(recommended, dict):
            for section in recommended.get("sections") or []:
                if not isinstance(section, dict):
                    continue
                for item in section.get("evidence_bullets") or section.get("claims") or []:
                    text = str(item.get("text") if isinstance(item, dict) else item)
                    if _looks_quantitative(text):
                        rows.append([_short_cell(section.get("title") or "Result"), _short_cell(text, 140)])
                    if len(rows) >= 5:
                        break
                if len(rows) >= 5:
                    break
    if not rows:
        return None
    return LayerNode.model_validate({
        "layer_id": "paper_project_result_table",
        "name": "result_table",
        "kind": "table",
        "z_index": 3,
        "headers": ["Evidence", "Reported value / claim"],
        "rows": rows[:5],
        "caption": "Result snippets summarized as a native HTML table.",
    })


def _looks_quantitative(text: str) -> bool:
    return bool(re.search(r"\b\d+(?:\.\d+)?\s*(?:%|x|×|k|m|b|gb|ms|s|fps|bleu|acc|accuracy|ppl|score|points?)\b", text, re.IGNORECASE))


def _short_cell(value: Any, limit: int = 72) -> str:
    text = _clean_sentence_block(str(value or ""), max_chars=limit)
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clean_sentence_block(value: Any, *, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars].rsplit(".", 1)[0].strip()
    return cut + "." if cut else text[:max_chars].rstrip() + "…"


def _caption_for_visual(layer_id: str, rendered: dict[str, Any]) -> str:
    rec = rendered.get(layer_id) if isinstance(rendered, dict) else None
    if not isinstance(rec, dict):
        return ""
    caption = str(rec.get("caption") or rec.get("title") or rec.get("caption_short") or "").strip()
    return _clean_sentence_block(caption, max_chars=260)


def _text_node(
    layer_id: str,
    name: str,
    text: str,
    *,
    z: int,
    role: str | None = None,
    font_size: int | None = None,
    weight: int | None = None,
    line_height: float | None = None,
) -> LayerNode:
    payload: dict[str, Any] = {
        "layer_id": layer_id,
        "name": name,
        "kind": "text",
        "z_index": z,
        "text": text,
    }
    # LayerNode.role is deck-slide-specific; landing roles are inferred from
    # stable layer names and then written into HtmlBlock.role during sync.
    _ = role
    if font_size is not None:
        payload["font_size_px"] = font_size
    if weight is not None:
        payload["font_weight"] = weight
    if line_height is not None:
        payload["line_height"] = line_height
    return LayerNode.model_validate(payload)


def _image_node(layer_id: str, name: str, rendered: dict[str, Any], *, z: int) -> LayerNode:
    rec = rendered.get(layer_id) if isinstance(rendered, dict) else {}
    return LayerNode.model_validate({
        "layer_id": layer_id,
        "name": name,
        "kind": "image",
        "z_index": z,
        "src_path": rec.get("src_path") if isinstance(rec, dict) else None,
        "aspect_ratio": rec.get("aspect_ratio") if isinstance(rec, dict) else None,
        "image_size": rec.get("image_size") if isinstance(rec, dict) else None,
    })


def _table_node(layer_id: str, rendered: dict[str, Any], *, z: int) -> LayerNode:
    rec = rendered.get(layer_id) if isinstance(rendered, dict) else {}
    return LayerNode.model_validate({
        "layer_id": layer_id,
        "name": "benchmark_table",
        "kind": "table",
        "z_index": z,
        "src_path": rec.get("src_path") if isinstance(rec, dict) else None,
        "rows": rec.get("rows") if isinstance(rec, dict) else None,
        "headers": rec.get("headers") if isinstance(rec, dict) else None,
        "caption": (rec.get("caption") or rec.get("title")) if isinstance(rec, dict) else None,
        "col_highlight_rule": rec.get("col_highlight_rule") if isinstance(rec, dict) else None,
    })


def _section_node(layer_id: str, name: str, children: list[LayerNode], *, z: int) -> LayerNode:
    return LayerNode.model_validate({
        "layer_id": layer_id,
        "name": name,
        "kind": "section",
        "z_index": z,
        "children": children,
    })


def _section_like(
    section: LayerNode,
    *,
    layer_id: str,
    name: str,
    children: list[LayerNode],
    z: int,
) -> LayerNode:
    data = section.model_dump()
    data.update({
        "layer_id": layer_id,
        "name": name,
        "children": children,
        "z_index": z,
    })
    return LayerNode.model_validate(data)


def _next_section_z(spec: DesignSpec) -> int:
    return max((int(node.z_index or 0) for node in spec.layer_graph or []), default=0) + 1


def _insert_after_hero_or_resources(spec: DesignSpec, section: LayerNode) -> None:
    insert_at = 1
    for idx, node in enumerate(spec.layer_graph or []):
        if _node_matches(node, ("hero", "resource", "links")):
            insert_at = idx + 1
    spec.layer_graph.insert(insert_at, section)


def _insert_before_citation(spec: DesignSpec, section: LayerNode) -> None:
    for idx, node in enumerate(spec.layer_graph or []):
        if _node_matches(node, ("citation", "bibtex", "footer")):
            spec.layer_graph.insert(idx, section)
            return
    spec.layer_graph.append(section)


def _looks_like_identity_logo(node: LayerNode) -> bool:
    blob = " ".join(str(value or "").lower() for value in (
        node.layer_id,
        node.name,
        node.src_path,
    ))
    return any(marker in blob for marker in (
        "identity_",
        "conference_on_",
        "venue_logo",
        "logo",
    ))


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug[:64]


def _unique_layer_id(base: str, used: set[str]) -> str:
    root = _slug(base) or "section"
    candidate = root
    suffix = 2
    while candidate in used:
        candidate = f"{root}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = str(value or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out
