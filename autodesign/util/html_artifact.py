"""Canonical HTML artifact adapters and contract audits.

`DesignSpec.html_artifact` is the shared scene graph for poster, deck,
landing, and video. This module keeps v1 compatible by converting legacy
`layer_graph` and `deck_html` structures into that graph, and by deriving the
legacy renderer inputs from it when a new designer emits only html_artifact.
"""

from __future__ import annotations

import re
from collections import Counter
from copy import deepcopy
from typing import Any

from ..schema import (
    ArtifactType,
    DeckHtmlSpec,
    DesignSpec,
    HtmlArtifactSpec,
    HtmlBlock,
    HtmlFrame,
    LayerNode,
    VIDEO_MAX_DURATION_S,
)


_LEGACY_THEME_KEY = "_autodesign_legacy_source"
_LEGACY_THEME_KEYS = (_LEGACY_THEME_KEY, "_designanything_legacy_source")
_SUBSTANTIVE_KINDS = {"text", "image", "table", "metric", "quote", "caption", "chart", "embed"}
_TEXT_KINDS = {"text", "metric", "quote", "caption"}
_VISUAL_KINDS = {"image", "table", "chart", "embed"}


def has_legacy_source_marker(theme: dict[str, Any]) -> bool:
    return any(theme.get(key) for key in _LEGACY_THEME_KEYS)


_GLOBAL_ROLES = {"background", "rule", "divider", "title", "subtitle", "kicker", "label", "source", "metadata", "footer", "global", "nav"}


def canonicalize_design_spec(
    spec: DesignSpec,
    *,
    prefer_html_artifact: bool | None = None,
) -> DesignSpec:
    """Populate canonical and compatibility structures on a DesignSpec.

    `prefer_html_artifact=True` means html_artifact is authoritative and legacy
    renderer fields should be regenerated from it. `False` means a legacy
    operation changed layer_graph/deck_html and html_artifact should be rebuilt.
    `None` defaults to html_artifact when present, otherwise legacy.
    """
    if prefer_html_artifact is False:
        spec.html_artifact = legacy_to_html_artifact(spec)
        return spec

    if spec.html_artifact is None:
        spec.html_artifact = legacy_to_html_artifact(spec)

    if spec.html_artifact is not None:
        if spec.artifact_type == ArtifactType.DECK:
            spec.deck_html = html_artifact_to_deck_html(spec.html_artifact)
        elif spec.artifact_type in {ArtifactType.POSTER, ArtifactType.LANDING}:
            spec.layer_graph = html_artifact_to_layer_graph(
                spec.html_artifact,
                artifact_type=spec.artifact_type.value,
            )
    return spec


def legacy_to_html_artifact(spec: DesignSpec) -> HtmlArtifactSpec | None:
    if spec.artifact_type == ArtifactType.DECK and spec.deck_html is not None:
        return deck_html_to_html_artifact(spec.deck_html, spec)
    if spec.layer_graph:
        return layer_graph_to_html_artifact(spec.layer_graph, spec)
    return None


def deck_html_to_html_artifact(deck: DeckHtmlSpec | dict[str, Any], spec: DesignSpec | None = None) -> HtmlArtifactSpec:
    data = _model_or_dict(deck)
    frames: list[HtmlFrame] = []
    for idx, slide in enumerate(data.get("slides") or []):
        blocks = [
            _html_block_from_deck_block(block)
            for block in (slide.get("blocks") or [])
            if isinstance(block, dict)
        ]
        slide_id = str(slide.get("slide_id") or f"slide_{idx + 1:02d}")
        frames.append(HtmlFrame(
            frame_id=slide_id,
            kind="slide",
            role=str(slide.get("role") or slide.get("layout") or "slide"),
            title=slide.get("title"),
            subtitle=slide.get("subtitle"),
            layout=slide.get("layout") or "editorial_split",
            layout_plan=deepcopy(slide.get("layout_plan")) if isinstance(slide.get("layout_plan"), dict) else None,
            speaker_notes=slide.get("speaker_notes"),
            style=dict(slide.get("style") or {}),
            blocks=blocks,
        ))
    return HtmlArtifactSpec(
        title=data.get("title") or (getattr(spec, "brief", None) if spec is not None else None),
        target="deck",
        theme={**dict(data.get("theme") or {}), _LEGACY_THEME_KEY: "deck_html"},
        frames=frames,
    )


def html_artifact_to_deck_html(artifact: HtmlArtifactSpec | dict[str, Any]) -> DeckHtmlSpec:
    data = _model_or_dict(artifact)
    slides: list[dict[str, Any]] = []
    for idx, frame in enumerate(data.get("frames") or []):
        if str(frame.get("kind") or "") != "slide":
            continue
        blocks: list[dict[str, Any]] = []
        for block in frame.get("blocks") or []:
            blocks.extend(_deck_blocks_from_html_block(block))
        layout_plan = frame.get("layout_plan") if isinstance(frame.get("layout_plan"), dict) else {}
        slides.append({
            "slide_id": str(frame.get("frame_id") or f"slide_{idx + 1:02d}"),
            "title": frame.get("title"),
            "subtitle": frame.get("subtitle"),
            "layout": _deck_layout_from_frame(frame, layout_plan),
            "speaker_notes": frame.get("speaker_notes"),
            "style": dict(frame.get("style") or {}),
            "layout_plan": deepcopy(layout_plan) if layout_plan else None,
            "blocks": blocks,
        })
    return DeckHtmlSpec.model_validate({
        "title": data.get("title"),
        "theme": dict(data.get("theme") or {}),
        "slides": slides,
    })


def layer_graph_to_html_artifact(nodes: list[LayerNode], spec: DesignSpec) -> HtmlArtifactSpec:
    artifact_type = spec.artifact_type.value
    theme = {
        "palette": list(spec.palette or []),
        "typography": dict(spec.typography or {}),
        "visual_profile": spec.visual_profile,
        _LEGACY_THEME_KEY: "layer_graph",
    }
    if artifact_type == "deck":
        frames = [
            _frame_from_layer(node, kind="slide")
            for node in nodes
            if getattr(node, "kind", None) == "slide"
        ]
    elif artifact_type == "landing":
        frames = [
            _frame_from_layer(node, kind="section")
            for node in nodes
            if getattr(node, "kind", None) == "section"
        ]
        for node in nodes:
            if getattr(node, "kind", None) != "section":
                frames.append(HtmlFrame(
                    frame_id=f"section_{len(frames) + 1:02d}",
                    kind="section",
                    role="implicit",
                    title=getattr(node, "name", None),
                    blocks=[_block_from_layer(node)],
                ))
    else:
        frames = [HtmlFrame(
            frame_id="poster_canvas",
            kind="canvas",
            role="poster",
            title=getattr(spec, "brief", None),
            bbox={
                "x": 0,
                "y": 0,
                "w": int(spec.canvas.get("w_px") or 0),
                "h": int(spec.canvas.get("h_px") or 0),
            },
            blocks=[_block_from_layer(node) for node in nodes],
        )]
    return HtmlArtifactSpec(
        title=getattr(spec, "brief", None),
        target=artifact_type,
        theme=theme,
        frames=frames,
    )


def html_artifact_to_layer_graph(
    artifact: HtmlArtifactSpec | dict[str, Any],
    *,
    artifact_type: str,
) -> list[LayerNode]:
    data = _model_or_dict(artifact)
    if artifact_type == "landing":
        nodes: list[dict[str, Any]] = []
        for idx, frame in enumerate(data.get("frames") or []):
            if str(frame.get("kind") or "") != "section":
                continue
            frame_id = str(frame.get("frame_id") or f"section_{idx + 1:02d}")
            nodes.append({
                "layer_id": frame_id,
                "name": str(frame.get("role") or frame.get("title") or frame_id or frame.get("layout") or "section"),
                "kind": "section",
                "z_index": idx,
                "children": [
                    layer for block in frame.get("blocks") or []
                    for layer in _layers_from_html_block(block, artifact_type=artifact_type)
                ],
            })
        return [LayerNode.model_validate(node) for node in nodes]

    if artifact_type == "deck":
        nodes = []
        for idx, frame in enumerate(data.get("frames") or []):
            if str(frame.get("kind") or "") != "slide":
                continue
            nodes.append({
                "layer_id": str(frame.get("frame_id") or f"slide_{idx + 1:02d}"),
                "name": str(frame.get("title") or frame.get("role") or "slide"),
                "kind": "slide",
                "z_index": idx,
                "speaker_notes": frame.get("speaker_notes"),
                "children": [
                    layer for block in frame.get("blocks") or []
                    for layer in _layers_from_html_block(block, artifact_type=artifact_type)
                ],
            })
        return [LayerNode.model_validate(node) for node in nodes]

    blocks: list[dict[str, Any]] = []
    for frame in data.get("frames") or []:
        if str(frame.get("kind") or "") == "canvas":
            blocks.extend(frame.get("blocks") or [])
    if not blocks:
        for frame in data.get("frames") or []:
            blocks.extend(frame.get("blocks") or [])
    layers = [
        layer for block in blocks
        for layer in _layers_from_html_block(block, artifact_type=artifact_type)
    ]
    return [LayerNode.model_validate(layer) for layer in layers]


def deck_data_from_spec(spec: Any) -> dict[str, Any] | None:
    artifact = getattr(spec, "html_artifact", None)
    if artifact is not None:
        deck = html_artifact_to_deck_html(artifact).model_dump(mode="json")
        if deck.get("slides"):
            return deck
    deck_html = getattr(spec, "deck_html", None)
    if deck_html is not None:
        data = _model_or_dict(deck_html)
        if data.get("slides"):
            return data
    return None


def html_artifact_stats(artifact: HtmlArtifactSpec | dict[str, Any] | None) -> dict[str, Any]:
    if artifact is None:
        return {
            "frame_count": 0,
            "block_count": 0,
            "visual_block_count": 0,
            "text_block_count": 0,
            "frame_kinds": {},
            "layout_counts": {},
        }
    data = _model_or_dict(artifact)
    frames = [f for f in data.get("frames") or [] if isinstance(f, dict)]
    blocks: list[dict[str, Any]] = []
    for frame in frames:
        blocks.extend(_flatten_blocks(frame.get("blocks") or []))
    visual_kinds = {"image", "table", "chart", "embed", "shape"}
    frame_kinds = Counter(str(f.get("kind") or "unknown") for f in frames)
    layout_counts = Counter(str(f.get("layout") or f.get("role") or "none") for f in frames)
    layout_plan_frames = [
        f for f in frames
        if isinstance(f.get("layout_plan"), dict)
    ]
    slot_count = sum(
        len([s for s in (f.get("layout_plan") or {}).get("slots") or [] if isinstance(s, dict)])
        for f in layout_plan_frames
    )
    return {
        "frame_count": len(frames),
        "block_count": len(blocks),
        "visual_block_count": sum(1 for b in blocks if str(b.get("kind")) in visual_kinds),
        "text_block_count": sum(1 for b in blocks if str(b.get("kind")) in {"text", "metric", "quote", "caption"}),
        "group_block_count": sum(1 for b in blocks if str(b.get("kind")) == "group"),
        "layout_plan_frame_count": len(layout_plan_frames),
        "slot_count": slot_count,
        "frame_kinds": dict(frame_kinds),
        "layout_counts": dict(layout_counts),
    }


def audit_html_artifact_contract(
    spec: Any,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = payload or {}
    artifact = getattr(spec, "html_artifact", None)
    findings: list[dict[str, Any]] = []
    findings.extend(_wrap_payload_findings(payload))
    frame_layout_findings: list[dict[str, Any]] = []
    if artifact is not None:
        artifact_type = str(getattr(getattr(spec, "artifact_type", None), "value", None) or getattr(spec, "artifact_type", "") or "")
        frame_layout_findings = audit_frame_layout_plan(artifact, artifact_type=artifact_type)
        findings.extend(frame_layout_findings)
        if artifact_type == "landing":
            findings.extend(_audit_landing_artifact(artifact, spec=spec))
        findings.extend(_audit_video_frames(artifact))
    p0 = sum(1 for f in findings if str(f.get("severity")) == "P0")
    frame_p0 = sum(1 for f in frame_layout_findings if str(f.get("severity")) == "P0")
    return {
        "html_artifact_contract_findings": findings,
        "html_artifact_contract_p0_count": p0,
        "frame_layout_findings": frame_layout_findings,
        "frame_layout_p0_count": frame_p0,
        "html_artifact_layout_stats": html_artifact_stats(artifact),
    }


def audit_frame_layout_plan(
    artifact: HtmlArtifactSpec | dict[str, Any] | None,
    *,
    artifact_type: str | None = None,
) -> list[dict[str, Any]]:
    """Audit the spatial storyboard layer before target-specific rendering.

    New substantial poster/deck/landing artifacts should declare named slots
    and group blocks so repairs can reason about panels instead of loose boxes.
    Legacy-converted specs are intentionally skipped for compatibility.
    """
    if artifact is None:
        return []
    data = _model_or_dict(artifact)
    theme = data.get("theme") if isinstance(data.get("theme"), dict) else {}
    if has_legacy_source_marker(theme):
        return []
    target = str(artifact_type or data.get("target") or "").lower()
    if target not in {"poster", "deck", "landing"}:
        return []

    findings: list[dict[str, Any]] = []
    frames = [f for f in data.get("frames") or [] if isinstance(f, dict)]
    for frame_idx, frame in enumerate(frames):
        kind = str(frame.get("kind") or "").lower()
        if target == "poster" and kind != "canvas":
            continue
        if target == "deck" and kind != "slide":
            continue
        if target == "landing" and kind != "section":
            continue
        if str(frame.get("render_mode") or "") == "authored_html":
            continue

        frame_id = str(frame.get("frame_id") or f"frame_{frame_idx + 1:02d}")
        blocks = [b for b in frame.get("blocks") or [] if isinstance(b, dict)]
        flat_blocks = _flatten_blocks(blocks)
        substantive = [b for b in flat_blocks if _is_substantive_block(b)]
        plan = frame.get("layout_plan") if isinstance(frame.get("layout_plan"), dict) else None
        slots = [s for s in (plan or {}).get("slots") or [] if isinstance(s, dict)]
        valid_slot_ids = {
            str(slot.get("slot_id") or "").strip()
            for slot in slots
            if str(slot.get("slot_id") or "").strip()
        }
        archetype = str((plan or {}).get("archetype") or frame.get("layout") or "").lower()
        layout_severity = "P1" if target == "landing" else "P0"

        if target == "deck":
            findings.extend(_deck_visible_content_findings(flat_blocks, frame_id=frame_id))

        if len(substantive) >= 6 and (plan is None or not slots):
            findings.append(_contract_finding(
                layout_severity,
                "frame_layout_missing_plan",
                "Substantial frame has many editable blocks but no populated spatial storyboard.",
                "Add frame.layout_plan.slots[] before placing html_artifact blocks.",
                frame_id,
            ))
            continue

        if plan is None:
            continue

        groups = [b for b in flat_blocks if str(b.get("kind") or "") == "group"]
        slot_groups = _slot_group_index(groups)
        top_level_groups = [b for b in blocks if isinstance(b, dict) and str(b.get("kind") or "") == "group"]
        top_level_slots = [s for s in slots if not str(s.get("parent_slot_id") or "").strip()]

        for slot in slots:
            slot_id = str(slot.get("slot_id") or "").strip()
            if not slot_id:
                findings.append(_contract_finding(
                    "P0",
                    "frame_layout_slot_missing_id",
                    "A layout slot is missing slot_id.",
                    "Give every FrameSlot a stable slot_id and matching group block.",
                    frame_id,
                ))
                continue
            if bool(slot.get("required")):
                group = slot_groups.get(slot_id)
                direct_slot_content = [
                    block
                    for block in flat_blocks
                    if str(block.get("slot_id") or "").strip() == slot_id
                    and _is_substantive_block(block)
                ]
                if (group is None or not _flatten_blocks(group.get("children") or [])) and not direct_slot_content:
                    findings.append(_contract_finding(
                        layout_severity,
                        "frame_layout_required_slot_empty",
                        "Required storyboard slot has no matching grouped content.",
                        "Create a group block whose block_id or slot_id matches the required slot.",
                        f"{frame_id}:{slot_id}",
                    ))

        top_substantive = [
            b for b in blocks
            if isinstance(b, dict)
            and _is_free_floating_substantive(b, valid_slot_ids=valid_slot_ids)
        ]
        too_many_free_floating = bool(top_substantive) and (
            target == "deck"
            or len(top_substantive) / max(1, len(substantive)) > 0.25
        )
        if too_many_free_floating:
            findings.append(_contract_finding(
                layout_severity,
                "frame_layout_free_floating_blocks",
                (
                    "Deck has substantive blocks outside declared slots."
                    if target == "deck"
                    else "More than 25% of substantive blocks are outside named panels/slots."
                ),
                "Wrap substantive blocks in group panels or assign them to layout_plan slots.",
                frame_id,
            ))

        findings.extend(_slot_geometry_findings(
            slots,
            frame_id=frame_id,
            gutter_px=_int_or_none(plan.get("gutter_px")),
            target=target,
        ))
        findings.extend(_group_child_findings(groups, frame_id=frame_id))
        findings.extend(_caption_findings(
            blocks,
            frame_id=frame_id,
            severity=layout_severity,
            valid_slot_ids=valid_slot_ids,
        ))
        findings.extend(_slot_content_findings(slots, slot_groups, frame_id=frame_id))
        findings.extend(_source_backed_visual_slot_findings(
            target=target,
            frame=frame,
            slots=slots,
            groups=groups,
            frame_id=frame_id,
        ))
        findings.extend(_slot_rhythm_findings(
            top_level_slots,
            top_level_groups,
            archetype=archetype,
            frame_id=frame_id,
        ))
    return findings


def _deck_visible_content_findings(
    blocks: list[dict[str, Any]],
    *,
    frame_id: str,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for block in blocks:
        block_id = str(block.get("block_id") or "?")
        role = str(block.get("role") or "").strip().lower().replace("-", "_")
        visible_values = [
            str(value).strip()
            for value in (
                block.get("title"),
                block.get("text"),
                *(block.get("items") or []),
            )
            if str(value or "").strip()
        ]
        visible_authoring_prefix = any(
            re.match(
                r"^(?:intent|speaker\s+note|design\s+note|layout\s+note)\s*:",
                value,
                flags=re.IGNORECASE,
            )
            for value in visible_values
        )
        if role in {
            "speaker_note_intent",
            "notes_intent",
            "authoring_intent",
            "design_note",
            "layout_note",
        } or visible_authoring_prefix:
            findings.append(_contract_finding(
                "P0",
                "frame_layout_visible_authoring_note",
                "Internal authoring intent is present as visible slide content.",
                "Move the instruction into frame.speaker_notes and remove the visible block.",
                f"{frame_id}:{block_id}",
            ))

        kind = str(block.get("kind") or "")
        if kind not in {"metric", "text"}:
            continue
        if kind == "text" and "card" not in role:
            continue
        if visible_values and all(re.fullmatch(r"[1-9][.)]?", value) for value in visible_values):
            findings.append(_contract_finding(
                "P0",
                "frame_layout_empty_metric_card",
                "Metric/takeaway card contains only an ordinal and no substantive claim.",
                "Add a concise source-grounded metric or takeaway, or remove the empty card.",
                f"{frame_id}:{block_id}",
            ))
    return findings


def _html_block_from_deck_block(block: dict[str, Any]) -> HtmlBlock:
    return HtmlBlock.model_validate({
        "block_id": str(block.get("block_id") or block.get("layer_id") or "block"),
        "kind": _html_kind(str(block.get("kind") or "text")),
        "role": block.get("role"),
        "layer_id": block.get("layer_id"),
        "text": block.get("text"),
        "title": block.get("title"),
        "items": list(block.get("items") or []),
        "bbox": block.get("bbox"),
        "src_path": block.get("src_path"),
        "prompt": block.get("prompt"),
        "aspect_ratio": block.get("aspect_ratio"),
        "rows": block.get("rows"),
        "headers": block.get("headers"),
        "caption": block.get("caption"),
        "col_highlight_rule": block.get("col_highlight_rule"),
        "style": dict(block.get("style") or {}),
        "source": block.get("source"),
        "source_id": block.get("source_id"),
        "evidence_quote": block.get("evidence_quote"),
        "evidence_source": block.get("evidence_source"),
        "slot_id": block.get("slot_id"),
        "panel_role": block.get("panel_role"),
        "covers": list(block.get("covers") or []),
    })


def _deck_blocks_from_html_block(block: dict[str, Any]) -> list[dict[str, Any]]:
    return _deck_blocks_from_html_block_inner(block, parent_slot_id=None, parent_panel_role=None)


def _deck_blocks_from_html_block_inner(
    block: dict[str, Any],
    *,
    parent_slot_id: str | None,
    parent_panel_role: str | None,
) -> list[dict[str, Any]]:
    kind = str(block.get("kind") or "text")
    if kind == "group":
        slot_id = _slot_id_for_group(block) or parent_slot_id
        panel_role = str(block.get("panel_role") or block.get("role") or parent_panel_role or "")
        return [
            item for child in block.get("children") or []
            for item in _deck_blocks_from_html_block_inner(
                child,
                parent_slot_id=slot_id,
                parent_panel_role=panel_role or None,
            )
        ]
    if kind in {"chart", "embed"}:
        kind = "image"
    if kind not in {"text", "image", "table", "metric", "quote", "shape", "caption"}:
        kind = "text"
    out = {
        "block_id": str(block.get("block_id") or block.get("layer_id") or "block"),
        "kind": kind,
        "role": block.get("role"),
        "layer_id": block.get("layer_id") or block.get("block_id"),
        "text": block.get("text"),
        "title": block.get("title"),
        "items": list(block.get("items") or []),
        "bbox": block.get("bbox"),
        "src_path": block.get("src_path"),
        "prompt": block.get("prompt"),
        "aspect_ratio": block.get("aspect_ratio"),
        "rows": block.get("rows"),
        "headers": block.get("headers"),
        "caption": block.get("caption"),
        "col_highlight_rule": block.get("col_highlight_rule"),
        "style": dict(block.get("style") or {}),
        "slot_id": block.get("slot_id") or parent_slot_id,
        "panel_role": block.get("panel_role") or parent_panel_role,
        "source": block.get("source"),
        "source_id": block.get("source_id"),
        "evidence_quote": block.get("evidence_quote"),
        "evidence_source": block.get("evidence_source"),
        "covers": list(block.get("covers") or []),
    }
    return [out]


def _frame_from_layer(node: LayerNode, *, kind: str) -> HtmlFrame:
    return HtmlFrame(
        frame_id=node.layer_id,
        kind=kind,  # type: ignore[arg-type]
        role=getattr(node, "role", None) or node.name,
        title=node.name,
        layout=_frame_layout(node, frame_kind=kind),
        bbox=_bbox_dict(getattr(node, "bbox", None)),
        speaker_notes=getattr(node, "speaker_notes", None),
        blocks=[_block_from_layer(child) for child in (node.children or [])],
    )


def _frame_layout(node: LayerNode, *, frame_kind: str) -> str | None:
    if frame_kind != "slide":
        return getattr(node, "role", None) or getattr(node, "name", None)
    role = str(getattr(node, "role", None) or "")
    archetype = str(getattr(node, "archetype", None) or "")
    role_map = {
        "cover": "full_bleed_cover",
        "closing": "closing_action",
        "content_with_table": "visual_grid",
        "content_with_figure": "editorial_split",
        "section_divider": "process_flow",
    }
    archetype_map = {
        "cover_editorial": "full_bleed_cover",
        "evidence_snapshot": "metric_cards",
        "takeaway_list": "closing_action",
        "thanks_qa": "closing_action",
        "pipeline_horizontal": "process_flow",
        "tension_two_column": "comparison",
        "section_divider": "process_flow",
        "cover_technical": "full_bleed_cover",
        "residual_stack_vertical": "visual_grid",
        "conflict_vs_cooperation": "comparison",
    }
    return role_map.get(role) or archetype_map.get(archetype) or "editorial_split"


def _deck_layout_from_frame(frame: dict[str, Any], layout_plan: dict[str, Any]) -> str:
    allowed = {
        "full_bleed_cover",
        "editorial_split",
        "visual_grid",
        "metric_cards",
        "comparison",
        "timeline",
        "process_flow",
        "closing_action",
    }
    raw = str(frame.get("layout") or layout_plan.get("archetype") or "").strip()
    if raw in allowed:
        return raw
    value = raw.lower().replace("-", "_").replace(" ", "_")
    if "cover" in value or "hero" in value and "method" not in value:
        return "full_bleed_cover"
    if "grid" in value or "evidence" in value or "visual" in value or "gallery" in value:
        return "visual_grid"
    if "metric" in value or "kpi" in value or "band" in value:
        return "metric_cards"
    if "compare" in value or "comparison" in value or "_vs_" in value:
        return "comparison"
    if "timeline" in value or "roadmap" in value:
        return "timeline"
    if "process" in value or "flow" in value or "method" in value or "workflow" in value:
        return "process_flow"
    if "closing" in value or "takeaway" in value or "action" in value or "footer" in value:
        return "closing_action"
    return "editorial_split"


def _block_from_layer(node: LayerNode) -> HtmlBlock:
    style: dict[str, Any] = {"z_index": int(getattr(node, "z_index", 1) or 1)}
    for field in ("font_family", "font_size_px", "font_weight", "font_style",
                  "line_height", "letter_spacing", "text_transform", "align"):
        value = getattr(node, field, None)
        if value is not None:
            style[field] = value
    effects = getattr(node, "effects", None)
    if effects is not None:
        style["effects"] = effects.model_dump(mode="json") if hasattr(effects, "model_dump") else effects
        fill = getattr(effects, "fill", None)
        if fill:
            style["fill"] = fill
    return HtmlBlock.model_validate({
        "block_id": node.layer_id,
        "kind": _html_kind(str(node.kind)),
        "role": getattr(node, "role", None) or node.name,
        "layer_id": node.layer_id,
        "text": getattr(node, "text", None) or getattr(node, "callout_text", None),
        "title": node.name,
        "bbox": _bbox_dict(getattr(node, "bbox", None)),
        "src_path": getattr(node, "src_path", None),
        "prompt": getattr(node, "prompt", None),
        "aspect_ratio": getattr(node, "aspect_ratio", None),
        "image_size": getattr(node, "image_size", None),
        "rows": getattr(node, "rows", None),
        "headers": getattr(node, "headers", None),
        "caption": getattr(node, "caption", None),
        "col_highlight_rule": getattr(node, "col_highlight_rule", None),
        "href": getattr(node, "href", None),
        "variant": getattr(node, "variant", None),
        "evidence_quote": getattr(node, "evidence_quote", None),
        "evidence_source": getattr(node, "evidence_source", None),
        "style": style,
        "children": [_block_from_layer(child) for child in (node.children or [])],
    })


def _layers_from_html_block(block: dict[str, Any], *, artifact_type: str) -> list[dict[str, Any]]:
    kind = str(block.get("kind") or "text")
    if kind == "group":
        return [
            layer for child in block.get("children") or []
            for layer in _layers_from_html_block(child, artifact_type=artifact_type)
        ]
    layer_kind = _layer_kind(kind, block, artifact_type=artifact_type)
    if layer_kind is None:
        return []
    style = dict(block.get("style") or {})
    source_ref = str(block.get("source_id") or block.get("asset_id") or "").strip()
    layer_id = str(block.get("layer_id") or block.get("block_id"))
    if (
        layer_kind in {"image", "table"}
        and source_ref.startswith(("ingest_fig_", "ingest_table_"))
    ):
        layer_id = source_ref
    layer: dict[str, Any] = {
        "layer_id": layer_id,
        "name": str(block.get("title") or block.get("role") or block.get("block_id")),
        "kind": layer_kind,
        "z_index": int(style.get("z_index") or block.get("z_index") or 1),
        "bbox": block.get("bbox"),
        "text": block.get("text"),
        "src_path": block.get("src_path"),
        "prompt": block.get("prompt"),
        "aspect_ratio": block.get("aspect_ratio"),
        "image_size": block.get("image_size"),
        "rows": block.get("rows"),
        "headers": block.get("headers"),
        "caption": block.get("caption"),
        "col_highlight_rule": block.get("col_highlight_rule"),
        "href": block.get("href"),
        "variant": block.get("variant"),
        "evidence_quote": block.get("evidence_quote"),
        "evidence_source": block.get("evidence_source"),
        "covers": list(block.get("covers") or []),
        "children": [
            child_layer for child in block.get("children") or []
            for child_layer in _layers_from_html_block(child, artifact_type=artifact_type)
        ],
    }
    if kind in {"metric", "quote", "caption"} and not layer.get("text"):
        layer["text"] = _block_text(block)
    for key in ("font_family", "font_size_px", "font_weight", "font_style",
                "line_height", "letter_spacing", "text_transform", "align"):
        if key in style:
            layer[key] = style[key]
    fill = style.get("fill")
    effects = style.get("effects")
    if fill or effects:
        merged = dict(effects or {})
        if fill:
            merged["fill"] = fill
        layer["effects"] = merged
    return [layer]


def _wrap_payload_findings(payload: dict[str, Any]) -> list[dict[str, Any]]:
    keys = (
        ("quality_lint_findings", "quality_lint"),
        ("paper_density_findings", "poster_density"),
        ("paper_information_findings", "poster_information"),
        ("paper_poster_dom_findings", "paper_poster_dom"),
        ("deck_layout_findings", "deck_layout"),
        ("visual_reference_findings", "visual_reference"),
    )
    out: list[dict[str, Any]] = []
    for key, target in keys:
        for idx, finding in enumerate(payload.get(key) or []):
            if not isinstance(finding, dict):
                continue
            wrapped = {
                "severity": str(finding.get("severity") or "P1"),
                "id": str(finding.get("id") or f"{target}_{idx}"),
                "message": str(finding.get("message") or "Contract finding."),
                "fix": str(finding.get("fix") or finding.get("suggested_action") or "Revise html_artifact and composite again."),
                "snippet": str(finding.get("snippet") or ""),
                "target": target,
            }
            out.append(wrapped)
    return out


def _audit_landing_artifact(
    artifact: HtmlArtifactSpec | dict[str, Any],
    *,
    spec: Any | None = None,
) -> list[dict[str, Any]]:
    data = _model_or_dict(artifact)
    frames = [f for f in data.get("frames") or [] if str(f.get("kind") or "") == "section"]
    findings: list[dict[str, Any]] = []
    if not frames:
        return [_contract_finding("P0", "landing_missing_sections", "Landing page has no section frames.", "Add hero, proof/features, CTA, and footer sections.", "landing")]
    is_paper_page = _is_paper_project_page(data, frames, spec=spec)
    first = frames[0]
    first_text = " ".join(_text_values(first.get("blocks") or [])).lower()
    first_role = str(first.get("role") or first.get("title") or first.get("layout") or first.get("frame_id") or "").lower()
    if not is_paper_page and "hero" not in first_role and not any(word in first_text for word in ("build", "launch", "scale", "ship", "automate", "生成", "发布")):
        findings.append(_contract_finding("P0", "landing_missing_hero", "First landing section does not read as a hero/fold.", "Make the first frame a clear hero with headline, value proposition, and primary CTA.", "landing.hero"))
    all_blocks = [b for frame in frames for b in _flatten_blocks(frame.get("blocks") or [])]
    has_cta = any(
        str(b.get("role") or "").lower() == "cta"
        or str(b.get("kind") or "") == "embed" and b.get("href")
        or b.get("href")
        for b in all_blocks
    )
    if not is_paper_page and not has_cta:
        findings.append(_contract_finding("P0", "landing_missing_cta", "Landing page has no clear CTA block.", "Add at least one primary CTA with href/role near the hero or conversion section.", "landing.cta"))
    for frame in frames:
        frame_box = frame.get("bbox") if isinstance(frame.get("bbox"), dict) else {}
        max_w = int(frame_box.get("w") or 1440)
        max_h = int(frame_box.get("h") or 9000)
        for block in _flatten_blocks(frame.get("blocks") or []):
            bbox = block.get("bbox")
            if not isinstance(bbox, dict):
                continue
            if int(bbox.get("x") or 0) + int(bbox.get("w") or 0) > max_w + 4:
                findings.append(_contract_finding("P0", "landing_horizontal_overflow", "A block overflows the desktop landing frame width.", "Constrain bbox width/x or keep this section inside the desktop frame.", str(block.get("block_id") or "block")))
                break
            if int(bbox.get("y") or 0) + int(bbox.get("h") or 0) > max_h + 4:
                findings.append(_contract_finding("P1", "landing_section_overflow", "A block overflows the section height.", "Increase section height or tighten vertical spacing.", str(block.get("block_id") or "block")))
                break
    if len(frames) < 3:
        findings.append(_contract_finding("P1", "landing_thin_section_rhythm", "Landing page has fewer than three sections.", "Add proof/features and a conversion/footer section.", "landing.sections"))
    footer_text = " ".join(_text_values(frames[-1].get("blocks") or [])).lower()
    last_role = str(frames[-1].get("role") or frames[-1].get("title") or "").lower()
    if not is_paper_page and "footer" not in last_role and not any(word in footer_text for word in ("contact", "privacy", "email", "github", "联系", "邮箱")):
        findings.append(_contract_finding("P1", "landing_missing_footer", "Landing page lacks a footer/contact close.", "Add a compact footer or contact block.", "landing.footer"))
    if is_paper_page:
        findings.extend(_audit_paper_project_page(frames))
    return findings


def _is_paper_project_page(
    artifact_data: dict[str, Any],
    frames: list[dict[str, Any]],
    *,
    spec: Any | None,
) -> bool:
    theme = artifact_data.get("theme") if isinstance(artifact_data.get("theme"), dict) else {}
    subtype = str(
        theme.get("page_subtype")
        or theme.get("landing_subtype")
        or theme.get("subtype")
        or ""
    ).lower()
    if subtype in {"paper_project_page", "research_project_page", "paper_page"}:
        return True
    brief = str(getattr(spec, "brief", "") or "").lower()
    if any(marker in brief for marker in (
        "paper project page",
        "paper page",
        "paper-to-page",
        "paper to page",
        "project page for this paper",
        "网页",
        "项目页",
        "论文页面",
    )):
        return True
    section_text = " ".join(
        str(frame.get(key) or "").lower()
        for frame in frames
        for key in ("frame_id", "role", "title", "layout")
    )
    return (
        any(marker in section_text for marker in ("resources", "framework", "citation", "bibtex"))
        and any(marker in section_text for marker in ("paper", "research", "project"))
    )


def _audit_paper_project_page(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    frame_index = [_frame_search_text(frame) for frame in frames]
    first_frame = frames[0] if frames else {}
    first_blocks = _flatten_blocks(first_frame.get("blocks") or [])
    first_title = str(first_frame.get("title") or "").strip()
    has_identity_title = bool(first_title) or any(
        str(block.get("text") or block.get("title") or "").strip()
        and _has_any(
            " ".join(str(block.get(key) or "").lower() for key in ("block_id", "role", "title")),
            ("title", "headline", "paper_name"),
        )
        for block in first_blocks
    )
    if not has_identity_title:
        findings.append(_contract_finding(
            "P0",
            "paper_project_missing_identity_hero",
            "The first paper-project section does not identify the paper.",
            "Put the real paper title and author identity in the first section before method or evidence content.",
            "landing.paper_project.identity",
        ))
    all_blocks = [block for frame in frames for block in _flatten_blocks(frame.get("blocks") or [])]
    all_text = " ".join(_text_values([*all_blocks])).lower()
    image_blocks = [
        block for block in all_blocks
        if str(block.get("kind") or "").lower() == "image"
    ]
    hrefs = [
        str(block.get("href") or "").strip()
        for block in all_blocks
        if str(block.get("href") or "").strip()
    ]
    valid_hrefs = [
        href for href in hrefs
        if href not in {"#", "todo", "tbd"} and not href.lower().startswith("javascript:")
    ]
    invalid_hrefs = [href for href in hrefs if href not in valid_hrefs]

    if not any(_has_any(text, ("abstract", "overview")) for text in frame_index):
        findings.append(_contract_finding(
            "P1",
            "paper_project_missing_abstract_overview",
            "Paper project page lacks a readable abstract/overview panel.",
            "Add an Abstract/Overview section with normal web paragraph typography, not raw text dumped into a card.",
            "landing.paper_project.abstract",
        ))

    if not any(_has_any(text, ("resource", "links", "github", "hugging", "arxiv", "code")) for text in frame_index):
        findings.append(_contract_finding(
            "P0",
            "paper_project_missing_resources",
            "Paper project page has no resource/link section.",
            "Add a resources section with native link blocks for arXiv/PDF, code, model/data, blog/demo, and citation links when available.",
            "landing.paper_project.resources",
        ))
    elif len(valid_hrefs) < 2:
        findings.append(_contract_finding(
            "P1",
            "paper_project_sparse_links",
            "Paper project page exposes too few valid resource links.",
            "Add available arXiv/PDF, GitHub, Hugging Face, blog, demo, Twitter/X, model weights, or BibTeX links; avoid fake URLs.",
            "landing.paper_project.resources",
        ))
    if invalid_hrefs:
        findings.append(_contract_finding(
            "P1",
            "paper_project_placeholder_links",
            "Paper project page contains placeholder resource URLs.",
            "Remove placeholder links or replace them with small native text notes such as 'Code not released in the source'.",
            "landing.paper_project.resources",
        ))

    if not any(_has_any(text, ("framework", "architecture", "pipeline", "method", "model", "system")) for text in frame_index):
        findings.append(_contract_finding(
            "P1",
            "paper_project_missing_framework",
            "Paper project page lacks a framework or method section.",
            "Add a source-backed framework/method section with the key architecture, pipeline, system, dataset, or model visual when available.",
            "landing.paper_project.framework",
        ))
    if not image_blocks:
        findings.append(_contract_finding(
            "P0",
            "paper_project_missing_source_visual",
            "Paper project page has no actual source-backed image block.",
            "Add at least one native image block using an ingested figure, framework, teaser, demo, or qualitative/result visual; tables and text-only references to figures are not enough.",
            "landing.paper_project.visuals",
        ))
    else:
        if len(image_blocks) < 2:
            findings.append(_contract_finding(
                "P1",
                "paper_project_sparse_source_visuals",
                "Paper project page uses too few actual source visuals.",
                "Use additional ingested framework, demo, qualitative, chart, or result figures in evidence sections when the paper provides them.",
                "landing.paper_project.visuals",
            ))
        elif len(image_blocks) < 4:
            findings.append(_contract_finding(
                "P2",
                "paper_project_underused_source_visuals",
                "Paper project page underuses available source visuals.",
                "A publishable research project page should normally show framework plus multiple evidence/demo/result figures when the paper provides them.",
                "landing.paper_project.visuals",
            ))
        early_frames = frames[:3]
        early_blocks = [
            block
            for frame in early_frames
            for block in _flatten_blocks(frame.get("blocks") or [])
        ]
        if not any(str(block.get("kind") or "").lower() == "image" for block in early_blocks):
            findings.append(_contract_finding(
                "P1",
                "paper_project_late_source_visual",
                "Paper project page delays all source visuals too far down the page.",
                "Place the primary framework, teaser, demo, or result figure in the hero/framework viewport, not only near the footer.",
                "landing.paper_project.visuals",
            ))

    has_evidence_section = any(
        _has_any(text, ("finding", "result", "benchmark", "ablation", "demo", "showcase", "qualitative", "example"))
        for text in frame_index
    )
    if not has_evidence_section:
        findings.append(_contract_finding(
            "P1",
            "paper_project_missing_evidence_sections",
            "Paper project page lacks demo/result/benchmark evidence sections.",
            "Add source-backed key findings, demos, benchmark tables/charts, or ablations instead of generic feature sections.",
            "landing.paper_project.evidence",
        ))
    if not any(_has_any(text, ("sample", "samples", "demo", "gallery", "qualitative", "example")) for text in frame_index):
        findings.append(_contract_finding(
            "P2",
            "paper_project_missing_samples_gallery",
            "Paper project page lacks a sample/demo gallery.",
            "Add a compact gallery of qualitative examples, screenshots, demo panels, or representative paper figures when available.",
            "landing.paper_project.samples",
        ))

    has_table = any(str(block.get("kind") or "") == "table" for block in all_blocks)
    if not has_table and not any(_has_any(text, ("benchmark", "leaderboard", "ablation", "result table")) for text in frame_index):
        findings.append(_contract_finding(
            "P1",
            "paper_project_missing_table_or_benchmark",
            "Paper project page has no benchmark table or result-table section.",
            "Use a native HTML table for benchmark or ablation data when the source provides structured results.",
            "landing.paper_project.benchmarks",
        ))

    if not any(_has_any(text, ("citation", "bibtex", "cite", "license", "model weights")) for text in frame_index) and not _has_any(all_text, ("bibtex", "@inproceedings", "@article", "citation")):
        findings.append(_contract_finding(
            "P1",
            "paper_project_missing_citation",
            "Paper project page lacks citation or reproducibility footer metadata.",
            "Add a citation/BibTeX/footer section with available citation, license, model-weight, or use-policy information.",
            "landing.paper_project.citation",
        ))
    return findings


def _frame_search_text(frame: dict[str, Any]) -> str:
    frame_text = " ".join(
        str(frame.get(key) or "")
        for key in ("frame_id", "role", "title", "layout")
    )
    block_text = " ".join(_text_values(frame.get("blocks") or []))
    return f"{frame_text} {block_text}".lower()


def _has_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


def _audit_video_frames(artifact: HtmlArtifactSpec | dict[str, Any]) -> list[dict[str, Any]]:
    data = _model_or_dict(artifact)
    scenes = [f for f in data.get("frames") or [] if str(f.get("kind") or "") == "scene"]
    if not scenes:
        return []
    findings: list[dict[str, Any]] = []
    for idx, scene in enumerate(scenes):
        duration = scene.get("duration_s")
        try:
            dur = float(duration)
        except (TypeError, ValueError):
            dur = 0.0
        target = str(scene.get("frame_id") or f"scene_{idx + 1:02d}")
        if dur < 0.5 or dur > VIDEO_MAX_DURATION_S:
            findings.append(_contract_finding(
                "P0",
                "video_invalid_scene_duration",
                "Video scene duration is outside the 0.5-600s bound.",
                "Set duration_s between 0.5 and 600 seconds; the full timeline "
                "must still satisfy the 300-600 second delivery contract.",
                target,
            ))
        for block in _flatten_blocks(scene.get("blocks") or []):
            role = str(block.get("role") or "").lower()
            if role not in {"caption", "subtitle"} and str(block.get("kind") or "") != "caption":
                continue
            style = dict(block.get("style") or {})
            size = int(style.get("font_size_px") or 0)
            if size and size < 28:
                findings.append(_contract_finding("P0", "video_caption_unreadable", "Caption text is too small for video export.", "Use caption font_size_px >= 28 and high contrast.", str(block.get("block_id") or target)))
            bbox = block.get("bbox")
            if isinstance(bbox, dict) and int(bbox.get("y") or 0) < 32:
                findings.append(_contract_finding("P1", "video_caption_safe_area", "Caption sits too close to the top edge.", "Move captions inside safe margins.", str(block.get("block_id") or target)))
    return findings


def _slot_group_index(groups: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for group in groups:
        for key in (_slot_id_for_group(group), str(group.get("block_id") or "").strip()):
            if key and key not in out:
                out[key] = group
    return out


def _slot_id_for_group(block: dict[str, Any]) -> str | None:
    slot_id = str(block.get("slot_id") or "").strip()
    if slot_id:
        return slot_id
    role = str(block.get("role") or "").lower()
    if role.endswith("_panel") or role in {"panel", "hero_panel", "evidence_cell", "caption_zone"}:
        block_id = str(block.get("block_id") or "").strip()
        return block_id or None
    return None


def _is_substantive_block(block: dict[str, Any]) -> bool:
    return str(block.get("kind") or "") in _SUBSTANTIVE_KINDS


def _is_free_floating_substantive(
    block: dict[str, Any],
    *,
    valid_slot_ids: set[str] | None = None,
) -> bool:
    if not _is_substantive_block(block):
        return False
    role = str(block.get("role") or "").lower()
    if role in _GLOBAL_ROLES or role.startswith("global_"):
        return False
    slot_id = str(block.get("slot_id") or "").strip()
    if slot_id and slot_id in (valid_slot_ids or set()):
        return False
    return True


def _slot_geometry_findings(
    slots: list[dict[str, Any]],
    *,
    frame_id: str,
    gutter_px: int | None,
    target: str,
) -> list[dict[str, Any]]:
    boxes_by_parent: dict[str, list[tuple[dict[str, Any], dict[str, int]]]] = {}
    findings: list[dict[str, Any]] = []
    seen_slot_ids: set[str] = set()
    for slot in slots:
        slot_id = str(slot.get("slot_id") or "").strip()
        if slot_id and slot_id in seen_slot_ids:
            findings.append(_contract_finding(
                "P0",
                "frame_layout_duplicate_slot_id",
                "Storyboard slots reuse the same slot_id.",
                "Give every slot in the frame a unique stable slot_id.",
                f"{frame_id}:{slot_id}",
            ))
            continue
        if slot_id:
            seen_slot_ids.add(slot_id)
        bbox = _as_bbox(slot.get("bbox"))
        if bbox is None:
            findings.append(_contract_finding(
                "P0",
                "frame_layout_slot_missing_bbox",
                "A layout slot is missing a valid bbox.",
                "Give every FrameSlot a positive x/y/w/h bbox.",
                f"{frame_id}:{slot.get('slot_id') or '?'}",
            ))
            continue
        if target == "deck" and (bbox["w"] <= 2 or bbox["h"] <= 2):
            findings.append(_contract_finding(
                "P0",
                "frame_layout_slot_bbox_too_small",
                "Deck layout slot uses a normalized or effectively empty bbox.",
                "Use 1920x1080 canvas pixel coordinates for every slot bbox; do not use 0-1 normalized values.",
                f"{frame_id}:{slot.get('slot_id') or '?'}",
            ))
            continue
        if target == "deck" and (
            bbox["x"] < 0
            or bbox["y"] < 0
            or bbox["x"] + bbox["w"] > 1920
            or bbox["y"] + bbox["h"] > 1080
        ):
            findings.append(_contract_finding(
                "P0",
                "frame_layout_slot_out_of_canvas",
                "Deck layout slot extends outside the 1920x1080 canvas.",
                "Move and resize the slot so its full bbox stays on canvas.",
                f"{frame_id}:{slot.get('slot_id') or '?'}",
            ))
            continue
        parent_slot_id = str(slot.get("parent_slot_id") or "").strip()
        boxes_by_parent.setdefault(parent_slot_id, []).append((slot, bbox))
    for sibling_boxes in boxes_by_parent.values():
        for i, (left_slot, left_box) in enumerate(sibling_boxes):
            for right_slot, right_box in sibling_boxes[i + 1:]:
                overlap = _bbox_overlap(left_box, right_box)
                if overlap > 4:
                    findings.append(_contract_finding(
                        "P0",
                        "frame_layout_slot_overlap",
                        "Sibling storyboard slots overlap.",
                        "Move/resize slots so sibling panels do not overlap.",
                        f"{frame_id}:{left_slot.get('slot_id')}~{right_slot.get('slot_id')}",
                    ))
                    continue
                if gutter_px and gutter_px > 0 and _too_close_for_gutter(left_box, right_box, gutter_px):
                    findings.append(_contract_finding(
                        "P0",
                        "frame_layout_slot_gutter",
                        "Sibling storyboard slots are closer than the declared gutter.",
                        "Increase spacing between slots or lower gutter_px to match the design.",
                        f"{frame_id}:{left_slot.get('slot_id')}~{right_slot.get('slot_id')}",
                    ))
    return findings


def _group_child_findings(groups: list[dict[str, Any]], *, frame_id: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    visited: set[int] = set()
    for group in groups:
        findings.extend(_group_child_findings_for_group(group, frame_id=frame_id, visited=visited))
    return findings


def _group_child_findings_for_group(
    group: dict[str, Any],
    *,
    frame_id: str,
    visited: set[int],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    marker = id(group)
    if marker in visited:
        return findings
    visited.add(marker)
    parent = _as_bbox(group.get("bbox"))
    if parent is None:
        return findings
    for child in [c for c in group.get("children") or [] if isinstance(c, dict)]:
        child_box = _as_bbox(child.get("bbox"))
        if child_box is not None and not _child_bbox_contained(parent, child_box, tolerance=2):
            overflow_px = _child_bbox_overflow_px(parent, child_box)
            severity = "P0" if overflow_px > 8 else "P1"
            findings.append(_contract_finding(
                severity,
                "frame_layout_child_escape",
                "A child block escapes its parent panel/slot bbox.",
                "Move or resize the child inside the group, or resize the slot with children scaled.",
                f"{frame_id}:{child.get('block_id') or '?'}",
            ))
        if str(child.get("kind") or "") == "group":
            findings.extend(_group_child_findings_for_group(child, frame_id=frame_id, visited=visited))
    return findings


def _child_bbox_contained(parent: dict[str, int], child: dict[str, int], *, tolerance: int = 0) -> bool:
    """Match renderer semantics: group children may use absolute or parent-local bboxes."""
    looks_absolute = child["x"] >= parent["x"] and child["y"] >= parent["y"]
    if looks_absolute:
        return _bbox_contains(parent, child, tolerance=tolerance)
    local_parent = {"x": 0, "y": 0, "w": parent["w"], "h": parent["h"]}
    return _bbox_contains(local_parent, child, tolerance=tolerance)


def _child_bbox_overflow_px(parent: dict[str, int], child: dict[str, int]) -> int:
    looks_absolute = child["x"] >= parent["x"] and child["y"] >= parent["y"]
    if looks_absolute:
        effective_parent = parent
        effective_child = child
    else:
        effective_parent = {"x": 0, "y": 0, "w": parent["w"], "h": parent["h"]}
        effective_child = child
    return max(
        effective_parent["x"] - effective_child["x"],
        effective_parent["y"] - effective_child["y"],
        effective_child["x"] + effective_child["w"] - (effective_parent["x"] + effective_parent["w"]),
        effective_child["y"] + effective_child["h"] - (effective_parent["y"] + effective_parent["h"]),
        0,
    )


def _caption_findings(
    blocks: list[dict[str, Any]],
    *,
    frame_id: str,
    severity: str = "P0",
    valid_slot_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    flat = _flatten_blocks(blocks)
    for block in flat:
        kind = str(block.get("kind") or "")
        if kind not in {"image", "table"}:
            continue
        role = str(block.get("role") or "").lower()
        if role in {"background", "logo", "icon", "decorative"}:
            continue
        if _visual_has_caption_or_explanation(
            block,
            blocks,
            valid_slot_ids=valid_slot_ids,
        ):
            continue
        findings.append(_contract_finding(
            severity,
            "frame_layout_missing_caption",
            "Image/table block lacks local explanatory text in the same or adjacent slot.",
            "Add a short local readout/explanation inside the same panel or adjacent visual explanation slot.",
            f"{frame_id}:{block.get('block_id') or '?'}",
        ))
    return findings


def _slot_content_findings(
    slots: list[dict[str, Any]],
    slot_groups: dict[str, dict[str, Any]],
    *,
    frame_id: str,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for slot in slots:
        slot_id = str(slot.get("slot_id") or "").strip()
        if not slot_id:
            continue
        max_words = _int_or_none(slot.get("max_text_words"))
        if max_words is None or max_words <= 0:
            continue
        group = slot_groups.get(slot_id)
        if group is None:
            continue
        words = _word_count(_block_text_recursive(group))
        if words > max_words:
            findings.append(_contract_finding(
                "P1",
                "frame_layout_slot_text_over_budget",
                "Slot text exceeds its declared max_text_words budget.",
                "Tighten copy, split the slot, or increase the budget only if the visual rhythm still works.",
                f"{frame_id}:{slot_id}:{words}>{max_words}",
            ))
    return findings


def _source_backed_visual_slot_findings(
    *,
    target: str,
    frame: dict[str, Any],
    slots: list[dict[str, Any]],
    groups: list[dict[str, Any]],
    frame_id: str,
) -> list[dict[str, Any]]:
    if target not in {"poster", "deck"}:
        return []
    flat = _flatten_blocks(frame.get("blocks") or [])
    source_backed = any(
        b.get("source") or b.get("source_id") or b.get("source_text") or b.get("provenance")
        for b in flat
    )
    if not source_backed:
        return []
    role_text = " ".join(
        _layout_semantic_text(item)
        for item in [*slots, *groups]
        if isinstance(item, dict)
    )
    if any(token in role_text for token in ("visual", "figure", "table", "evidence", "method", "hero", "qualitative", "result")):
        return []
    return [_contract_finding(
        "P0",
        "frame_layout_missing_evidence_slot",
        "Source-backed frame lacks required visual/evidence storyboard slots.",
        "Add method/hero/evidence slots before placing paper/report figures, tables, or source-backed claims.",
        frame_id,
    )]


def _layout_semantic_text(item: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in (
        "slot_id",
        "role",
        "panel_role",
        "content_policy",
        "job",
        "panel_job",
        "purpose",
        "text_budget",
        "space_fill",
        "space_fill_policy",
        "visual_role",
        "source_role",
        "kind",
        "title",
        "label",
    ):
        value = item.get(key)
        if value is None:
            continue
        parts.extend(_semantic_value_parts(value))
    for key in ("visual_ids", "source_ids", "asset_ids"):
        value = item.get(key)
        if value:
            parts.append(key)
            parts.extend(_semantic_value_parts(value))
    return " ".join(parts).lower()


def _semantic_value_parts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (int, float, bool)):
        return [str(value)]
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            parts.extend(_semantic_value_parts(item))
        return parts
    if isinstance(value, dict):
        parts: list[str] = []
        for key, item in value.items():
            parts.append(str(key))
            parts.extend(_semantic_value_parts(item))
        return parts
    return [str(value)]


def _slot_rhythm_findings(
    slots: list[dict[str, Any]],
    groups: list[dict[str, Any]],
    *,
    archetype: str,
    frame_id: str,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if len(slots) > 12:
        findings.append(_contract_finding(
            "P1",
            "frame_layout_too_many_slots",
            "Frame has too many top-level slots without hierarchy.",
            "Group related cells under parent slots or simplify the storyboard.",
            frame_id,
        ))
    if "grid" in archetype:
        return findings
    boxes = [_as_bbox(slot.get("bbox")) for slot in slots]
    boxes = [b for b in boxes if b is not None]
    if len(boxes) >= 4:
        areas = [_bbox_area(b) for b in boxes]
        if min(areas) > 0 and max(areas) / min(areas) < 1.18:
            findings.append(_contract_finding(
                "P1",
                "frame_layout_flat_rhythm",
                "Non-grid storyboard uses nearly identical slot sizes.",
                "Create clearer panel rhythm with a hero/evidence/secondary hierarchy.",
                frame_id,
            ))
    group_boxes = [_as_bbox(group.get("bbox")) for group in groups]
    if not boxes and len([b for b in group_boxes if b is not None]) > 12:
        findings.append(_contract_finding(
            "P1",
            "frame_layout_too_many_slots",
            "Frame has too many top-level panels without a declared slot hierarchy.",
            "Add parent slots or reduce panel count.",
            frame_id,
        ))
    return findings


def _visual_has_caption_or_explanation(
    visual: dict[str, Any],
    top_blocks: list[dict[str, Any]],
    *,
    valid_slot_ids: set[str] | None = None,
) -> bool:
    if str(visual.get("caption") or "").strip():
        return True
    visual_id = str(visual.get("block_id") or "")
    for group in [b for b in _flatten_blocks(top_blocks) if str(b.get("kind") or "") == "group"]:
        children = _flatten_blocks(group.get("children") or [])
        if not any(str(child.get("block_id") or "") == visual_id for child in children):
            continue
        if any(_is_visual_explanation_block(child) for child in children):
            return True
    visual_slot_id = str(visual.get("slot_id") or "").strip()
    if visual_slot_id and visual_slot_id in (valid_slot_ids or set()):
        if any(
            block is not visual
            and str(block.get("slot_id") or "").strip() == visual_slot_id
            and _is_visual_explanation_block(block)
            for block in _flatten_blocks(top_blocks)
        ):
            return True
    visual_box = _as_bbox(visual.get("bbox"))
    if visual_box is None:
        return False
    for block in _flatten_blocks(top_blocks):
        if _is_visual_explanation_block(block):
            caption_box = _as_bbox(block.get("bbox"))
            if caption_box is not None and _boxes_adjacent(visual_box, caption_box, max_gap=72):
                return True
    return False


def _is_caption_block(block: dict[str, Any]) -> bool:
    kind = str(block.get("kind") or "")
    role = str(block.get("role") or "").lower()
    text = _block_text(block)
    return kind == "caption" or "caption" in role or (kind == "text" and role in {"fig_caption", "table_caption"} and bool(text))


def _is_visual_explanation_block(block: dict[str, Any]) -> bool:
    if _is_caption_block(block):
        return True
    kind = str(block.get("kind") or "")
    if kind not in {"text", "quote", "metric"}:
        return False
    role = str(block.get("role") or "").lower()
    if any(token in role for token in ("title", "heading", "badge", "eyebrow", "section_label")):
        return False
    text = _block_text(block)
    return _word_count(text) >= 6


def _block_text_recursive(block: dict[str, Any]) -> str:
    values = [_block_text(block)]
    for child in block.get("children") or []:
        if isinstance(child, dict):
            values.append(_block_text_recursive(child))
    return "\n".join(v for v in values if v)


def _word_count(text: str) -> int:
    return len([part for part in text.replace("\n", " ").split(" ") if part.strip()])


def _as_bbox(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    try:
        bbox = {key: int(value[key]) for key in ("x", "y", "w", "h")}
    except (KeyError, TypeError, ValueError):
        return None
    if bbox["w"] <= 0 or bbox["h"] <= 0:
        return None
    return bbox


def _bbox_area(bbox: dict[str, int]) -> int:
    return max(0, int(bbox.get("w") or 0)) * max(0, int(bbox.get("h") or 0))


def _bbox_overlap(a: dict[str, int], b: dict[str, int]) -> int:
    x1 = max(a["x"], b["x"])
    y1 = max(a["y"], b["y"])
    x2 = min(a["x"] + a["w"], b["x"] + b["w"])
    y2 = min(a["y"] + a["h"], b["y"] + b["h"])
    return max(0, x2 - x1) * max(0, y2 - y1)


def _bbox_contains(parent: dict[str, int], child: dict[str, int], *, tolerance: int = 0) -> bool:
    return (
        child["x"] >= parent["x"] - tolerance
        and child["y"] >= parent["y"] - tolerance
        and child["x"] + child["w"] <= parent["x"] + parent["w"] + tolerance
        and child["y"] + child["h"] <= parent["y"] + parent["h"] + tolerance
    )


def _too_close_for_gutter(a: dict[str, int], b: dict[str, int], gutter_px: int) -> bool:
    x_overlap = min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"])
    y_overlap = min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"])
    if y_overlap > 0:
        gap = max(a["x"], b["x"]) - min(a["x"] + a["w"], b["x"] + b["w"])
        if 0 <= gap < gutter_px:
            return True
    if x_overlap > 0:
        gap = max(a["y"], b["y"]) - min(a["y"] + a["h"], b["y"] + b["h"])
        if 0 <= gap < gutter_px:
            return True
    return False


def _boxes_adjacent(a: dict[str, int], b: dict[str, int], *, max_gap: int) -> bool:
    if _bbox_overlap(a, b) > 0:
        return True
    horizontal_overlap = min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"])
    vertical_gap = max(a["y"], b["y"]) - min(a["y"] + a["h"], b["y"] + b["h"])
    if horizontal_overlap > min(a["w"], b["w"]) * 0.35 and 0 <= vertical_gap <= max_gap:
        return True
    vertical_overlap = min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"])
    horizontal_gap = max(a["x"], b["x"]) - min(a["x"] + a["w"], b["x"] + b["w"])
    return vertical_overlap > min(a["h"], b["h"]) * 0.35 and 0 <= horizontal_gap <= max_gap


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _contract_finding(severity: str, fid: str, message: str, fix: str, target: str) -> dict[str, Any]:
    return {
        "severity": severity,
        "id": fid,
        "message": message,
        "fix": fix,
        "snippet": target,
        "target": target,
    }


def _model_or_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return deepcopy(value)
    return {}


def _bbox_dict(value: Any) -> dict[str, int] | None:
    if value is None:
        return None
    data = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    if not isinstance(data, dict):
        return None
    try:
        return {key: int(data[key]) for key in ("x", "y", "w", "h") if key in data}
    except (TypeError, ValueError):
        return None


def _html_kind(kind: str) -> str:
    mapping = {
        "background": "image",
        "brand_asset": "image",
        "cta": "text",
        "callout": "shape",
        "slide": "group",
        "section": "group",
    }
    kind = mapping.get(kind, kind)
    allowed = {"text", "image", "table", "metric", "quote", "shape", "caption", "chart", "embed", "group"}
    return kind if kind in allowed else "text"


def _layer_kind(kind: str, block: dict[str, Any], *, artifact_type: str) -> str | None:
    role = str(block.get("role") or "").lower()
    if kind in {"text", "metric", "quote", "caption"}:
        return "cta" if block.get("href") or role == "cta" else "text"
    if kind in {"image", "chart", "embed"}:
        if artifact_type == "poster" and role == "background":
            return "background"
        return "image"
    if kind == "table":
        return "table"
    if kind == "shape":
        return "callout" if artifact_type == "deck" else None
    return None


def _block_text(block: dict[str, Any]) -> str:
    values = [
        str(block.get("title") or "").strip(),
        str(block.get("text") or "").strip(),
        *(str(item).strip() for item in block.get("items") or []),
    ]
    return "\n".join(v for v in values if v)


def _flatten_blocks(blocks: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        out.append(block)
        out.extend(_flatten_blocks(block.get("children") or []))
    return out


def _text_values(blocks: list[Any]) -> list[str]:
    values: list[str] = []
    for block in _flatten_blocks(blocks):
        text = _block_text(block)
        if text:
            values.append(text)
    return values
