"""HTML-first deck renderer.

The deck output path uses structured slide/block data as the source of truth
and writes a self-contained `deck.html`.
"""

from __future__ import annotations

import base64
import html
import json
import mimetypes
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._contract import ToolContext
from ._font_embed import build_font_face_css
from ..schema import ArtifactType
from ..util.html_artifact import deck_data_from_spec


_LAYOUTS = {
    "full_bleed_cover",
    "editorial_split",
    "visual_grid",
    "metric_cards",
    "comparison",
    "timeline",
    "process_flow",
    "closing_action",
}

_ACADEMIC_LIGHT_THEME_PROFILES: dict[str, dict[str, str]] = {
    "modern_serif": {
        "style": "academic-light-modern-serif",
        "bg": "#F7F7F3",
        "surface": "#FFFFFF",
        "ink": "#171717",
        "muted": "#62676F",
        "accent": "#9C2F3B",
        "border": "#D7D9D2",
        "structural_border": "#858A82",
        "display_font": "PlayfairDisplay",
        "body_font": "Inter",
    },
    "metropolis_light": {
        "style": "academic-light-metropolis",
        "bg": "#F8FAFB",
        "surface": "#FFFFFF",
        "ink": "#202124",
        "muted": "#5F6368",
        "accent": "#00796B",
        "border": "#DADCE0",
        "structural_border": "#8B9198",
        "display_font": "IBMPlexSans",
        "body_font": "Inter",
    },
    "technical_light": {
        "style": "academic-light-technical",
        "bg": "#F4F7F9",
        "surface": "#FFFFFF",
        "ink": "#10212F",
        "muted": "#566575",
        "accent": "#B63B28",
        "border": "#CBD6DE",
        "structural_border": "#7C8B96",
        "display_font": "IBMPlexSans",
        "body_font": "NotoSansSC",
    },
}


@dataclass
class DeckHtmlPlacement:
    slide_id: str
    block_id: str
    kind: str
    role: str
    bbox: dict[str, int]
    slot_id: str | None = None
    panel_role: str | None = None
    layout_archetype: str | None = None
    font_family: str | None = None
    font_size_px: int | None = None
    font_weight: int | None = None
    font_style: str | None = None
    line_height: float | None = None
    letter_spacing: float | None = None
    text_transform: str | None = None
    text: str = ""
    src_path: str | None = None
    source: str | None = None
    source_id: str | None = None
    evidence_quote: str | None = None
    evidence_source: str | None = None
    covers: list[str] = field(default_factory=list)
    missing_source_visual: bool = False
    content_placeholder: bool = False


@dataclass
class DeckHtmlRenderResult:
    slide_count: int
    slide_ids: list[str] = field(default_factory=list)
    placements: list[DeckHtmlPlacement] = field(default_factory=list)
    layout_counts: dict[str, int] = field(default_factory=dict)
    layout_sequence: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


def write_html_first_deck(spec: Any, out_path: Path, ctx: ToolContext) -> DeckHtmlRenderResult:
    """Render canonical `DesignSpec.html_artifact` slide frames to deck HTML."""
    canvas = getattr(spec, "canvas", None) or {}
    slide_w = int(canvas.get("w_px") or 1920)
    slide_h = int(canvas.get("h_px") or 1080)
    deck = _normalise_deck(spec, ctx)
    _hydrate_deck_blocks(deck, ctx)

    theme = _theme_tokens(spec, deck)
    placements: list[DeckHtmlPlacement] = []
    layout_counts: Counter[str] = Counter()
    layout_sequence: list[str] = []
    slide_html: list[str] = []
    slide_ids: list[str] = []
    for idx, slide in enumerate(deck.get("slides") or []):
        layout = str(slide.get("layout") or "editorial_split")
        if layout not in _LAYOUTS:
            layout = "editorial_split"
        layout_counts[layout] += 1
        layout_sequence.append(layout)
        slide_id = str(slide.get("slide_id") or f"slide_{idx + 1:02d}")
        slide_ids.append(slide_id)
        slide_placements = _layout_slide(
            slide, slide_id=slide_id, layout=layout,
            slide_w=slide_w, slide_h=slide_h, theme=theme,
        )
        placements.extend(slide_placements)
        slide_html.append(_slide_html(slide, slide_placements, idx, slide_w, slide_h, theme))

    fonts_used = _collect_font_chars(placements, ctx)
    font_css = build_font_face_css(fonts_used, ctx)
    title = deck.get("title") or getattr(spec, "brief", "AutoDesign deck")
    body = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="generator" content="AutoDesign">',
        '<meta name="od-artifact-type" content="deck">',
        f"<title>{html.escape(str(title)[:120])}</title>",
        "<style>",
        _css(slide_w, slide_h, theme),
        font_css,
        "</style>",
        "</head>",
        (
            f'<body data-od-style="{_attr(theme["style"])}" '
            f'data-od-theme-profile="{_attr(theme["profile"])}" '
            'data-current-slide="1">'
        ),
        (
            f'<main class="od-artifact od-deck" data-od-artifact-type="deck" '
            f'data-w="{slide_w}" data-h="{slide_h}" data-current-slide="1">'
        ),
        *slide_html,
        "</main>",
        "</body>",
        "</html>",
    ]
    out_path.write_text("\n".join(body), encoding="utf-8")
    result = DeckHtmlRenderResult(
        slide_count=len(deck.get("slides") or []),
        slide_ids=slide_ids,
        placements=placements,
        layout_counts=dict(layout_counts),
        layout_sequence=layout_sequence,
    )
    result.stats = _layout_stats(result, slide_w=slide_w, slide_h=slide_h)
    return result


def audit_deck_html_layout(
    result: DeckHtmlRenderResult,
    *,
    slide_w: int,
    slide_h: int,
) -> list[dict[str, Any]]:
    """Deterministic deck layout contract for HTML-first decks."""
    stats = result.stats or _layout_stats(result, slide_w=slide_w, slide_h=slide_h)
    findings: list[dict[str, Any]] = []
    n = int(stats.get("slide_count") or 0)
    if n <= 0:
        return findings

    if int(stats.get("missing_source_visual_count") or 0) > 0:
        findings.append(_finding(
            "P0", "deck_missing_source_visual",
            "One or more source image blocks could not be resolved locally.",
            "Restore the registered local source image before compositing the deck.",
            stats,
        ))

    if int(stats.get("empty_slide_count") or 0) > 0:
        findings.append(_finding(
            "P0", "deck_empty_slide",
            "One or more deck slides contain no rendered placements.",
            "Add source-grounded content to every planned slide or remove the empty slide.",
            stats,
        ))

    if int(stats.get("content_placeholder_count") or 0) > 0:
        findings.append(_finding(
            "P0", "deck_content_placeholder",
            "One or more text or table blocks are empty content placeholders.",
            "Replace every empty placeholder with authored source-grounded content.",
            stats,
        ))

    if int(stats.get("max_repeated_layout_run") or 0) >= 3:
        findings.append(_finding(
            "P0", "deck_repeated_layout",
            "Three or more consecutive slides use the same layout.",
            "Revise the deck rhythm with varied slide layouts.",
            stats,
        ))

    if float(stats.get("avg_visual_area_ratio") or 0.0) < 0.16 and n >= 3:
        findings.append(_finding(
            "P0", "deck_low_visual_area",
            "Deck visual area is too low for an HTML-first presentation.",
            "Add or enlarge image/table/metric/shape blocks on substantive slides.",
            stats,
        ))

    if int(stats.get("top_left_text_slide_count") or 0) >= max(2, (n + 1) // 2):
        findings.append(_finding(
            "P0", "deck_top_left_text_pile",
            "Text is repeatedly anchored in the upper-left corner.",
            "Move titles/body into varied editorial, centered, grid, or split layouts.",
            stats,
        ))

    if int(stats.get("right_bottom_image_slide_count") or 0) >= max(2, (n + 1) // 2):
        findings.append(_finding(
            "P0", "deck_fixed_right_image",
            "Images repeatedly sit in the right/bottom quadrant.",
            "Use full-bleed, left-image, grid, or centered visual layouts.",
            stats,
        ))

    if int(stats.get("font_family_count") or 0) <= 1 and n >= 3:
        findings.append(_finding(
            "P1", "deck_flat_typography",
            "Deck uses only one detected font family.",
            "Use a display/body pairing with clear title/body hierarchy.",
            stats,
        ))

    return findings


def _normalise_deck(spec: Any, ctx: ToolContext) -> dict[str, Any]:
    data = deck_data_from_spec(spec)
    if data is not None and data.get("slides"):
        return data
    return _legacy_layer_graph_to_deck(spec, ctx)


def _legacy_layer_graph_to_deck(spec: Any, ctx: ToolContext) -> dict[str, Any]:
    slides = [
        node for node in (getattr(spec, "layer_graph", None) or [])
        if getattr(node, "kind", None) == "slide"
    ]
    out: list[dict[str, Any]] = []
    for idx, slide in enumerate(slides):
        blocks: list[dict[str, Any]] = []
        for child in getattr(slide, "children", None) or []:
            kind = getattr(child, "kind", None)
            if kind not in {"text", "image", "background", "table"}:
                continue
            block_kind = "image" if kind == "background" else kind
            block = {
                "block_id": getattr(child, "layer_id", None) or f"slide_{idx + 1:02d}_block_{len(blocks)}",
                "layer_id": getattr(child, "layer_id", None),
                "kind": block_kind,
                "role": getattr(child, "role", None) or getattr(child, "name", None),
                "text": getattr(child, "text", None),
                "title": getattr(child, "caption", None),
                "bbox": _bbox_dict(getattr(child, "bbox", None)),
                "src_path": getattr(child, "src_path", None),
                "rows": getattr(child, "rows", None),
                "headers": getattr(child, "headers", None),
                "caption": getattr(child, "caption", None),
                "style": _style_from_child(child),
            }
            blocks.append(block)
        layout = _infer_layout(blocks, idx=idx, total=len(slides))
        out.append({
            "slide_id": getattr(slide, "layer_id", None) or f"slide_{idx + 1:02d}",
            "title": _first_text(blocks, role_hint="title"),
            "layout": layout,
            "blocks": blocks,
            "speaker_notes": getattr(slide, "speaker_notes", None),
            "style": {},
        })
    return {
        "title": getattr(spec, "brief", "AutoDesign deck"),
        "theme": {},
        "slides": out,
    }


def _hydrate_deck_blocks(deck: dict[str, Any], ctx: ToolContext) -> None:
    rendered = ctx.state.get("rendered_layers") or {}
    for slide in deck.get("slides") or []:
        for block in slide.get("blocks") or []:
            kind = block.get("kind")
            if kind not in {"image", "table"}:
                continue
            candidates: list[dict[str, Any]] = []
            for candidate_id in (
                block.get("layer_id"),
                block.get("source_id"),
                block.get("block_id"),
            ):
                candidate = rendered.get(str(candidate_id or ""))
                if isinstance(candidate, dict):
                    candidates.append(candidate)
            if not candidates:
                continue
            if not block.get("src_path"):
                block["src_path"] = next(
                    (rec["src_path"] for rec in candidates if rec.get("src_path")),
                    block.get("src_path"),
                )
            if kind == "table":
                if not block.get("rows"):
                    block["rows"] = next((rec["rows"] for rec in candidates if rec.get("rows")), [])
                if not block.get("headers"):
                    block["headers"] = next((rec["headers"] for rec in candidates if rec.get("headers")), [])
                if not block.get("caption"):
                    block["caption"] = next(
                        (rec["caption"] for rec in candidates if rec.get("caption")),
                        block.get("caption"),
                    )


def _layout_slide(
    slide: dict[str, Any],
    *,
    slide_id: str,
    layout: str,
    slide_w: int,
    slide_h: int,
    theme: dict[str, str],
) -> list[DeckHtmlPlacement]:
    raw_blocks = list(slide.get("blocks") or [])
    blocks = _with_title_blocks(slide, raw_blocks)
    for block in blocks:
        if isinstance(block, dict):
            block.setdefault("_layout_archetype", layout)
    plan = slide.get("layout_plan") if isinstance(slide.get("layout_plan"), dict) else {}
    declared_slot_ids = {
        str(slot.get("slot_id") or "").strip()
        for slot in plan.get("slots") or []
        if isinstance(slot, dict) and str(slot.get("slot_id") or "").strip()
    }
    explicit: list[DeckHtmlPlacement] = []
    implicit: list[dict[str, Any]] = []
    for block in blocks:
        if str(block.get("slot_id") or "").strip() in declared_slot_ids:
            implicit.append(block)
            continue
        bbox = _bbox_from_block(block)
        if bbox:
            explicit.append(_placement(slide_id, block, bbox, theme))
        else:
            implicit.append(block)

    planned_placements, remaining = _layout_declared_slot_blocks(
        slide_id,
        slide,
        implicit,
        theme=theme,
    )
    implicit_placements = _layout_implicit_blocks(
        slide_id, layout, remaining, slide_w=slide_w, slide_h=slide_h, theme=theme,
    )
    return [*explicit, *planned_placements, *implicit_placements]


def _with_title_blocks(slide: dict[str, Any], blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = list(blocks)
    has_title = any((b.get("role") or "").lower() == "title" for b in out)
    if slide.get("title") and not has_title:
        out.insert(0, {
            "block_id": f'{slide.get("slide_id", "slide")}_title',
            "kind": "text",
            "role": "title",
            "text": slide.get("title"),
            "style": {"font_size_px": 82},
            "slot_id": _declared_slot_id_for_role(slide, "title"),
        })
    has_subtitle = any((b.get("role") or "").lower() == "subtitle" for b in out)
    if slide.get("subtitle") and not has_subtitle:
        insert_at = 1 if out else 0
        out.insert(insert_at, {
            "block_id": f'{slide.get("slide_id", "slide")}_subtitle',
            "kind": "text",
            "role": "subtitle",
            "text": slide.get("subtitle"),
            "style": {"font_size_px": 34, "fill": "#475569"},
            "slot_id": _declared_slot_id_for_role(slide, "subtitle"),
        })
    return out


def _declared_slot_id_for_role(slide: dict[str, Any], role: str) -> str | None:
    plan = slide.get("layout_plan") if isinstance(slide.get("layout_plan"), dict) else {}
    for slot in plan.get("slots") or []:
        if not isinstance(slot, dict):
            continue
        slot_id = str(slot.get("slot_id") or "").strip()
        slot_role = str(slot.get("role") or "").strip().lower()
        if slot_id and (slot_id.lower() == role or slot_role == role):
            return slot_id
    return None


def _layout_declared_slot_blocks(
    slide_id: str,
    slide: dict[str, Any],
    blocks: list[dict[str, Any]],
    *,
    theme: dict[str, str],
) -> tuple[list[DeckHtmlPlacement], list[dict[str, Any]]]:
    plan = slide.get("layout_plan") if isinstance(slide.get("layout_plan"), dict) else {}
    slots = [slot for slot in plan.get("slots") or [] if isinstance(slot, dict)]
    slot_boxes: dict[str, dict[str, int]] = {}
    slot_order: list[str] = []
    for slot in slots:
        slot_id = str(slot.get("slot_id") or "").strip()
        bbox = _bbox_from_block({"bbox": slot.get("bbox")})
        if not slot_id or bbox is None:
            continue
        if slot_id not in slot_boxes:
            slot_boxes[slot_id] = bbox
            slot_order.append(slot_id)
    if not slot_boxes:
        return [], blocks

    buckets: dict[str, list[dict[str, Any]]] = {slot_id: [] for slot_id in slot_order}
    remaining: list[dict[str, Any]] = []
    for block in blocks:
        slot_id = str(block.get("slot_id") or "").strip()
        if slot_id in slot_boxes:
            buckets[slot_id].append(block)
        else:
            remaining.append(block)

    placements: list[DeckHtmlPlacement] = []
    for slot_id in slot_order:
        slot_blocks = buckets.get(slot_id) or []
        if slot_blocks:
            placements.extend(_layout_blocks_inside_slot(
                slide_id,
                slot_blocks,
                slot_boxes[slot_id],
                theme=theme,
            ))
    return placements, remaining


def _layout_blocks_inside_slot(
    slide_id: str,
    blocks: list[dict[str, Any]],
    bbox: dict[str, int],
    *,
    theme: dict[str, str],
) -> list[DeckHtmlPlacement]:
    if len(blocks) == 1:
        return [_placement(slide_id, blocks[0], bbox, theme)]

    visual_kinds = {"image", "table", "shape"}
    visual_blocks = [block for block in blocks if block.get("kind") in visual_kinds]
    text_blocks = [block for block in blocks if block.get("kind") not in visual_kinds]
    gap = max(8, min(24, bbox["h"] // 24))
    placements: list[DeckHtmlPlacement] = []
    if visual_blocks and text_blocks:
        visual_h = max(1, int((bbox["h"] - gap) * 0.70))
        visual_box = {**bbox, "h": visual_h}
        for block, box in zip(
            visual_blocks,
            _grid_boxes(
                visual_box["x"],
                visual_box["y"],
                visual_box["w"],
                visual_box["h"],
                len(visual_blocks),
                max_cols=3,
            ),
        ):
            placements.append(_placement(slide_id, block, box, theme))
        text_y = bbox["y"] + visual_h + gap
        text_h = max(1, bbox["y"] + bbox["h"] - text_y)
        placements.extend(_stack_blocks_in_box(
            slide_id,
            text_blocks,
            {"x": bbox["x"], "y": text_y, "w": bbox["w"], "h": text_h},
            theme=theme,
            gap=gap,
        ))
        return placements
    if visual_blocks:
        for block, box in zip(
            visual_blocks,
            _grid_boxes(bbox["x"], bbox["y"], bbox["w"], bbox["h"], len(visual_blocks), max_cols=3),
        ):
            placements.append(_placement(slide_id, block, box, theme))
        return placements
    return _stack_blocks_in_box(slide_id, text_blocks, bbox, theme=theme, gap=gap)


def _stack_blocks_in_box(
    slide_id: str,
    blocks: list[dict[str, Any]],
    bbox: dict[str, int],
    *,
    theme: dict[str, str],
    gap: int,
) -> list[DeckHtmlPlacement]:
    if not blocks:
        return []
    weights = []
    for block in blocks:
        role = str(block.get("role") or "").lower()
        kind = str(block.get("kind") or "")
        if role == "title":
            weights.append(1.7)
        elif role == "subtitle" or kind == "quote":
            weights.append(1.3)
        elif kind == "caption" or "caption" in role or "readout" in role:
            weights.append(0.8)
        else:
            weights.append(1.0)
    available_h = max(len(blocks), bbox["h"] - gap * (len(blocks) - 1))
    total_weight = max(0.1, sum(weights))
    heights = [max(1, int(available_h * weight / total_weight)) for weight in weights]
    heights[-1] += available_h - sum(heights)
    placements: list[DeckHtmlPlacement] = []
    y = bbox["y"]
    for block, height in zip(blocks, heights):
        placements.append(_placement(
            slide_id,
            block,
            {"x": bbox["x"], "y": y, "w": bbox["w"], "h": height},
            theme,
        ))
        y += height + gap
    return placements


def _layout_implicit_blocks(
    slide_id: str,
    layout: str,
    blocks: list[dict[str, Any]],
    *,
    slide_w: int,
    slide_h: int,
    theme: dict[str, str],
) -> list[DeckHtmlPlacement]:
    text_blocks = [b for b in blocks if b.get("kind") in {"text", "quote", "caption"}]
    visual_blocks = [b for b in blocks if b.get("kind") in {"image", "table", "shape"}]
    metric_blocks = [b for b in blocks if b.get("kind") == "metric"]
    placements: list[DeckHtmlPlacement] = []

    def add_stack(items: list[dict[str, Any]], x: int, y: int, w: int, h: int, gap: int = 28) -> None:
        if not items:
            return
        heights = [_block_height(b, h, len(items)) for b in items]
        cur = y
        for b, bh in zip(items, heights):
            placements.append(_placement(slide_id, b, {"x": x, "y": cur, "w": w, "h": bh}, theme))
            cur += bh + gap

    if layout == "full_bleed_cover":
        for b in visual_blocks[:1]:
            placements.append(_placement(slide_id, b, {"x": 0, "y": 0, "w": slide_w, "h": slide_h}, theme))
        add_stack(text_blocks + metric_blocks, 120, 620, 1420, 330, gap=22)
    elif layout == "visual_grid":
        add_stack(text_blocks[:2], 110, 70, 1700, 150, gap=14)
        grid = _grid_boxes(110, 300, 1700, 650, max(1, len(visual_blocks)))
        for b, box in zip(visual_blocks, grid):
            placements.append(_placement(slide_id, b, box, theme))
        add_stack(metric_blocks + text_blocks[2:], 110, 970, 1700, 70, gap=12)
    elif layout == "metric_cards":
        add_stack(text_blocks[:2], 110, 80, 1700, 170, gap=16)
        cards = metric_blocks or text_blocks[2:]
        grid = _grid_boxes(110, 330, 1700, 420, max(1, len(cards)), max_cols=4)
        for b, box in zip(cards, grid):
            placements.append(_placement(slide_id, b, box, theme))
        for b, box in zip(visual_blocks, _grid_boxes(110, 800, 1700, 190, max(1, len(visual_blocks)), max_cols=3)):
            placements.append(_placement(slide_id, b, box, theme))
    elif layout == "comparison":
        add_stack(text_blocks[:1], 110, 70, 1700, 140, gap=12)
        left = (text_blocks[1::2] + metric_blocks[::2]) or text_blocks[1:2]
        right = (text_blocks[2::2] + metric_blocks[1::2]) or visual_blocks[:1]
        add_stack(left, 110, 290, 790, 650, gap=24)
        add_stack(right, 1020, 290, 790, 650, gap=24)
        for b, box in zip(visual_blocks[1:], _grid_boxes(1020, 760, 790, 220, len(visual_blocks[1:]), max_cols=2)):
            placements.append(_placement(slide_id, b, box, theme))
    elif layout in {"timeline", "process_flow"}:
        add_stack(text_blocks[:2], 110, 70, 1700, 160, gap=14)
        steps = metric_blocks or text_blocks[2:] or visual_blocks
        boxes = _grid_boxes(110, 360, 1700, 470, max(1, len(steps)), max_cols=5)
        for b, box in zip(steps, boxes):
            placements.append(_placement(slide_id, b, box, theme))
        for b, box in zip(visual_blocks if steps is not visual_blocks else [], _grid_boxes(110, 860, 1700, 150, len(visual_blocks), max_cols=4)):
            placements.append(_placement(slide_id, b, box, theme))
    elif layout == "closing_action":
        add_stack(text_blocks[:2], 210, 260, 1500, 250, gap=18)
        add_stack(metric_blocks + text_blocks[2:], 360, 570, 1200, 300, gap=18)
        for b in visual_blocks[:1]:
            placements.append(_placement(slide_id, b, {"x": 1350, "y": 120, "w": 390, "h": 260}, theme))
    else:
        # Editorial split defaults to a strong visual panel and varied text stack.
        if visual_blocks:
            for b in visual_blocks[:1]:
                placements.append(_placement(slide_id, b, {"x": 1010, "y": 110, "w": 780, "h": 820}, theme))
            add_stack(text_blocks + metric_blocks, 110, 110, 790, 820, gap=28)
            for b, box in zip(visual_blocks[1:], _grid_boxes(1010, 760, 780, 170, len(visual_blocks[1:]), max_cols=3)):
                placements.append(_placement(slide_id, b, box, theme))
        else:
            add_stack(text_blocks + metric_blocks, 210, 160, 1500, 740, gap=30)

    return placements


def _placement(slide_id: str, block: dict[str, Any], bbox: dict[str, int], theme: dict[str, str]) -> DeckHtmlPlacement:
    style = block.get("style") if isinstance(block.get("style"), dict) else {}
    kind = str(block.get("kind") or "text")
    role = str(block.get("role") or kind)
    block_id = str(block.get("block_id") or block.get("layer_id") or f"{slide_id}_{len(role)}")
    text = _block_text(block)
    font_family = str(style.get("font_family") or (
        theme["display_font"] if role in {"title", "quote"} else theme["body_font"]
    ))
    default_size = 78 if role == "title" else 40 if role == "subtitle" else 34
    if kind == "metric":
        default_size = 56
    elif kind == "caption":
        default_size = 22
    elif kind == "quote":
        default_size = 54
    src_path = str(block.get("src_path")) if block.get("src_path") else None
    missing_source_visual = kind == "image" and (
        not src_path or not Path(src_path).is_file()
    )
    content_placeholder = (
        kind == "table" and not _table_has_content(block)
    ) or (
        kind in {"text", "metric", "quote", "caption"} and not text.strip()
    )
    return DeckHtmlPlacement(
        slide_id=slide_id,
        block_id=block_id,
        kind=kind,
        role=role,
        bbox={k: int(v) for k, v in bbox.items()},
        slot_id=str(block.get("slot_id")) if block.get("slot_id") else None,
        panel_role=str(block.get("panel_role")) if block.get("panel_role") else None,
        layout_archetype=str(block.get("_layout_archetype")) if block.get("_layout_archetype") else None,
        font_family=font_family,
        font_size_px=int(style.get("font_size_px") or default_size),
        font_weight=_font_weight(style.get("font_weight"), font_family),
        font_style=_font_style(style.get("font_style")),
        line_height=_line_height(style.get("line_height"), role=role, kind=kind),
        letter_spacing=_float_value(style.get("letter_spacing"), 0.0),
        text_transform=_text_transform(style.get("text_transform")),
        text=text,
        src_path=src_path,
        source=str(block.get("source")) if block.get("source") else None,
        source_id=str(block.get("source_id")) if block.get("source_id") else None,
        evidence_quote=(
            str(block.get("evidence_quote")) if block.get("evidence_quote") else None
        ),
        evidence_source=(
            str(block.get("evidence_source")) if block.get("evidence_source") else None
        ),
        covers=[str(item) for item in block.get("covers") or [] if str(item)],
        missing_source_visual=missing_source_visual,
        content_placeholder=content_placeholder,
    )


def _slide_html(
    slide: dict[str, Any],
    placements: list[DeckHtmlPlacement],
    idx: int,
    slide_w: int,
    slide_h: int,
    theme: dict[str, str],
) -> str:
    slide_id = str(slide.get("slide_id") or f"slide_{idx + 1:02d}")
    layout = str(slide.get("layout") or "editorial_split")
    parts = [
        (
            f'<section class="od-frame deck-slide" data-frame-kind="slide" '
            f'data-slide data-slide-index="{idx}" data-layer-id="{_attr(slide_id)}" '
            f'data-frame-id="{_attr(slide_id)}" data-layout="{_attr(layout)}" '
            f'data-layout-archetype="{_attr(layout)}">'
        )
    ]
    if layout in {"timeline", "process_flow"}:
        parts.append('<div class="od-slide-rule" aria-hidden="true"></div>')
    block_by_id = {
        str((b.get("block_id") or b.get("layer_id"))): b
        for b in slide.get("blocks") or []
    }
    for p in placements:
        block = block_by_id.get(p.block_id, {})
        parts.append(_placement_html(p, block, theme))
    parts.append("</section>")
    return "\n".join(parts)


def _placement_html(p: DeckHtmlPlacement, block: dict[str, Any], theme: dict[str, str]) -> str:
    bbox = p.bbox
    z = _z_for_kind(p.kind, p.role)
    style = block.get("style") if isinstance(block.get("style"), dict) else {}
    box_style = _style_box(bbox, z)
    if p.content_placeholder:
        reason = "empty-table" if p.kind == "table" else "empty-content"
        return _content_placeholder_html(p, box_style, reason=reason)
    if p.kind in {"image", "table"}:
        src = p.src_path
        if p.kind == "table":
            return _table_html(p, block, box_style)
        if p.missing_source_visual:
            return _missing_source_visual_html(p, box_style)
        default_fit = "cover" if p.role in {"background", "hero"} else "contain"
        fit = str(style.get("fit") or default_fit)
        return (
            f'<figure class="od-layer od-image" {_data_attrs(p, "image")} '
            f'style="{box_style}">'
            f'<img src="{_inline_image(src)}" alt="{_attr(p.role)}" '
            f'style="object-fit:{_attr(fit)};">'
            "</figure>"
        )
    if p.kind == "shape":
        fill = str(style.get("fill") or "rgba(127, 29, 29, 0.08)")
        stroke = str(style.get("stroke") or "rgba(15, 23, 42, 0.16)")
        return (
            f'<div class="od-layer od-shape" {_data_attrs(p, "shape")} '
            f'style="{box_style}background:{_attr(fill)};border:1px solid {_attr(stroke)};"></div>'
        )
    fill = str(style.get("fill") or theme["ink"])
    align = str(style.get("align") or ("center" if p.role in {"title_center", "closing"} else "left"))
    font_weight = p.font_weight or _font_weight(style.get("font_weight"), p.font_family)
    font_style = p.font_style or _font_style(style.get("font_style"))
    line_height = p.line_height or _line_height(style.get("line_height"), role=p.role, kind=p.kind)
    letter_spacing = p.letter_spacing if p.letter_spacing is not None else _float_value(style.get("letter_spacing"), 0.0)
    text_transform = p.text_transform or _text_transform(style.get("text_transform"))
    classes = "od-layer od-text od-editable"
    if p.kind == "metric":
        classes += " od-metric"
    elif p.kind == "quote":
        classes += " od-quote"
    elif p.kind == "caption":
        classes += " od-caption"
    return (
        f'<div class="{classes}" {_data_attrs(p, "text")} '
        f'data-font-size-px="{int(p.font_size_px or 34)}" '
        f'data-font-family="{_attr(p.font_family or theme["body_font"])}" '
        f'data-font-weight="{font_weight}" '
        f'data-font-style="{_attr(font_style)}" '
        f'data-line-height="{line_height:g}" '
        f'data-letter-spacing="{letter_spacing:g}" '
        f'data-text-transform="{_attr(text_transform)}" '
        f'data-fill="{_attr(fill)}" data-align="{_attr(align)}" '
        f'style="{box_style}font-family:{_css_string(p.font_family or theme["body_font"])};'
        f'font-size:{int(p.font_size_px or 34)}px;font-weight:{font_weight};'
        f'font-style:{_attr(font_style)};line-height:{line_height:g};'
        f'letter-spacing:{letter_spacing:g}px;text-transform:{_attr(text_transform)};'
        f'color:{_attr(fill)};text-align:{_attr(align)};">'
        f'{html.escape(p.text)}'
        "</div>"
    )


def _table_html(p: DeckHtmlPlacement, block: dict[str, Any], box_style: str) -> str:
    headers = list(block.get("headers") or [])
    rows = list(block.get("rows") or [])
    if not headers and rows:
        headers = [str(v) for v in rows[0]]
        rows = rows[1:]
    n_cols = max(len(headers), max((len(r) for r in rows), default=0))
    headers = ([str(v) for v in headers] + [""] * n_cols)[:n_cols]
    norm_rows = [([str(v) for v in r] + [""] * n_cols)[:n_cols] for r in rows]
    parts = [
        f'<div class="od-layer od-table-wrap od-editable" {_data_attrs(p, "table")} style="{box_style}">',
        '<table class="od-table"><thead><tr>',
        *(f"<th>{html.escape(h)}</th>" for h in headers),
        "</tr></thead><tbody>",
    ]
    for row in norm_rows:
        parts.append("<tr>")
        parts.extend(f"<td>{html.escape(v)}</td>" for v in row)
        parts.append("</tr>")
    parts.extend(["</tbody></table></div>"])
    return "".join(parts)


def _content_placeholder_html(
    p: DeckHtmlPlacement,
    box_style: str,
    *,
    reason: str,
) -> str:
    label = f"Empty content: {p.role or p.block_id}"
    return (
        f'<div class="od-layer od-shape od-missing" {_data_attrs(p, "shape")} '
        f'data-od-content-placeholder="true" data-missing-reason="{_attr(reason)}" '
        f'style="{box_style}">'
        f"{html.escape(label)}"
        "</div>"
    )


def _missing_source_visual_html(p: DeckHtmlPlacement, box_style: str) -> str:
    label = f"Missing source visual: {p.role or p.block_id}"
    return (
        f'<div class="od-layer od-missing od-missing-source-visual" '
        f'{_data_attrs(p, "image")} data-od-missing-source-visual="true" '
        f'data-missing-reason="unresolved-local-image" role="img" '
        f'aria-label="{_attr(label)}" style="{box_style}">'
        f"{html.escape(label)}"
        "</div>"
    )


def _layout_stats(result: DeckHtmlRenderResult, *, slide_w: int, slide_h: int) -> dict[str, Any]:
    slide_area = max(1, slide_w * slide_h)
    slide_ids = list(dict.fromkeys(result.slide_ids))
    for p in result.placements:
        if p.slide_id not in slide_ids:
            slide_ids.append(p.slide_id)
    while len(slide_ids) < result.slide_count:
        slide_ids.append(f"__empty_slide_{len(slide_ids) + 1:02d}")
    by_slide: dict[str, list[DeckHtmlPlacement]] = {
        slide_id: [] for slide_id in slide_ids[:result.slide_count]
    }
    for p in result.placements:
        by_slide.setdefault(p.slide_id, []).append(p)
    ratios: list[float] = []
    top_left = 0
    right_bottom = 0
    font_families: set[str] = set()
    for placements in by_slide.values():
        visual_area = sum(
            p.bbox["w"] * p.bbox["h"]
            for p in placements
            if p.kind in {"image", "table", "metric", "shape"}
            and not p.missing_source_visual
            and not p.content_placeholder
        )
        ratios.append(visual_area / slide_area)
        text_boxes = [p for p in placements if p.kind in {"text", "quote", "caption"}]
        body_text_boxes = [
            p for p in text_boxes
            if "title" not in (p.role or "").lower()
            and p.kind != "caption"
        ]
        top_left_body = [
            p for p in body_text_boxes
            if p.bbox["x"] <= 180 and p.bbox["y"] <= 280
        ]
        if len(top_left_body) >= 2:
            top_left += 1
        image_boxes = [
            p for p in placements
            if p.kind == "image" and not p.missing_source_visual
        ]
        if image_boxes:
            largest = max(image_boxes, key=lambda p: p.bbox["w"] * p.bbox["h"])
            cx = largest.bbox["x"] + largest.bbox["w"] / 2
            cy = largest.bbox["y"] + largest.bbox["h"] / 2
            if cx >= slide_w * 0.62 and cy >= slide_h * 0.55:
                right_bottom += 1
        font_families.update(p.font_family for p in text_boxes if p.font_family)
    missing_source_visuals = [
        p for p in result.placements if p.missing_source_visual
    ]
    content_placeholders = [
        p for p in result.placements if p.content_placeholder
    ]
    empty_slide_ids = [
        slide_id for slide_id, placements in by_slide.items() if not placements
    ]
    return {
        "slide_count": result.slide_count,
        "layout_counts": dict(result.layout_counts),
        "distinct_layout_count": len(result.layout_counts),
        "avg_visual_area_ratio": round(sum(ratios) / max(1, len(ratios)), 4),
        "top_left_text_slide_count": top_left,
        "right_bottom_image_slide_count": right_bottom,
        "font_family_count": len(font_families),
        "max_repeated_layout_run": _max_repeated_layout_run(result.layout_sequence),
        "missing_source_visual_count": len(missing_source_visuals),
        "missing_source_visual_ids": [p.block_id for p in missing_source_visuals],
        "content_placeholder_count": len(content_placeholders),
        "content_placeholder_ids": [p.block_id for p in content_placeholders],
        "empty_slide_count": len(empty_slide_ids),
        "empty_slide_ids": empty_slide_ids,
    }


def _max_repeated_layout_run(layouts: list[str]) -> int:
    best = 0
    cur = 0
    last = None
    for layout in layouts:
        cur = cur + 1 if layout == last else 1
        best = max(best, cur)
        last = layout
    return best


def _finding(severity: str, fid: str, message: str, fix: str, stats: dict[str, Any]) -> dict[str, Any]:
    return {
        "severity": severity,
        "id": fid,
        "message": message,
        "fix": fix,
        "snippet": str({k: stats.get(k) for k in sorted(stats) if k != "layout_counts"})[:300],
    }


def _theme_tokens(spec: Any, deck: dict[str, Any]) -> dict[str, str]:
    theme = deck.get("theme") if isinstance(deck.get("theme"), dict) else {}
    palette = list(getattr(spec, "palette", None) or [])
    typography = getattr(spec, "typography", None) or {}
    requested_profile = str(theme.get("profile") or theme.get("profile_id") or "").strip()
    profile = _ACADEMIC_LIGHT_THEME_PROFILES.get(requested_profile, {})
    fallbacks = {
        "bg": profile.get("bg") or "#FAF7F0",
        "surface": profile.get("surface") or "#FFFFFF",
        "ink": profile.get("ink") or "#111216",
        "muted": profile.get("muted") or "#5F6368",
        "accent": profile.get("accent") or "#7F1D1D",
        "border": profile.get("border") or "#D8CFC2",
        "structural_border": profile.get("structural_border") or "#8A8F98",
    }
    bg = _valid_hex_color(
        theme.get("bg") or (palette[0] if palette else None),
        fallbacks["bg"],
    )
    surface = _valid_hex_color(theme.get("surface"), fallbacks["surface"])
    ink_candidate = theme.get("ink") or (palette[2] if len(palette) > 2 else None)
    ink = _accessible_color(ink_candidate, [bg, surface], fallbacks["ink"])
    if min(_contrast_ratio(ink, bg), _contrast_ratio(ink, surface)) < 4.5:
        surface = bg
        ink = _accessible_color(ink_candidate, [bg], fallbacks["ink"])
    return {
        "profile": requested_profile if profile else "custom",
        "style": str(theme.get("style") or profile.get("style") or getattr(spec, "visual_profile", None) or "html-first"),
        "bg": bg,
        "surface": surface,
        "ink": ink,
        "muted": _accessible_color(theme.get("muted"), [bg, surface], fallbacks["muted"]),
        "accent": _accessible_color(
            theme.get("accent") or (palette[-1] if palette else None),
            [bg, surface],
            fallbacks["accent"],
        ),
        "border": _valid_hex_color(theme.get("border"), fallbacks["border"]),
        "structural_border": _nontext_contrast_color(
            theme.get("structural_border"),
            [bg, surface],
            fallbacks["structural_border"],
        ),
        "display_font": str(
            theme.get("display_font")
            or profile.get("display_font")
            or typography.get("title_font")
            or typography.get("display")
            or "NotoSerifSC-Bold"
        ),
        "body_font": str(
            theme.get("body_font")
            or profile.get("body_font")
            or typography.get("body_font")
            or typography.get("body")
            or "NotoSansSC-Bold"
        ),
    }


def _css(slide_w: int, slide_h: int, theme: dict[str, str]) -> str:
    return f"""
      :root {{
        --od-slide-w: {slide_w}px;
        --od-slide-h: {slide_h}px;
        --od-bg: {theme["bg"]};
        --od-surface: {theme["surface"]};
        --od-ink: {theme["ink"]};
        --od-muted: {theme["muted"]};
        --od-accent: {theme["accent"]};
        --od-border: {theme["border"]};
        --od-structural-border: {theme["structural_border"]};
      }}
      @page {{ size: {slide_w}px {slide_h}px; margin: 0; }}
      html, body {{
        margin: 0; padding: 0;
        background: color-mix(in srgb, var(--od-bg) 88%, var(--od-ink));
        color: var(--od-ink);
      }}
      html {{ scroll-behavior: smooth; }}
      body {{ font-family: {theme["body_font"]}, system-ui, sans-serif; }}
      .od-deck {{
        display: flex; flex-direction: column; align-items: center;
        gap: 32px; padding: 32px;
      }}
      .deck-slide {{
        position: relative; width: var(--od-slide-w); height: var(--od-slide-h);
        overflow: hidden; background: var(--od-bg);
        box-shadow: 0 18px 70px rgba(0, 0, 0, 0.38); isolation: isolate;
      }}
      .deck-slide::before {{
        content: ""; position: absolute; inset: 48px; border: 1px solid color-mix(in srgb, var(--od-border) 80%, transparent);
        pointer-events: none; z-index: 0;
      }}
      .od-layer {{ position: absolute; box-sizing: border-box; }}
      .od-text {{ white-space: pre-wrap; overflow-wrap: break-word; line-height: 1.08; letter-spacing: 0; }}
      .od-text[data-layer-name*="title"], .od-quote {{ line-height: 0.96; }}
      .od-caption {{ color: var(--od-muted); line-height: 1.2; }}
      .od-metric {{
        padding: 28px 30px; border: 1px solid var(--od-border); background: color-mix(in srgb, var(--od-surface) 86%, transparent);
      }}
      .od-quote {{ padding-left: 28px; border-left: 6px solid var(--od-accent); }}
      .od-image {{ margin: 0; background: color-mix(in srgb, var(--od-surface) 75%, transparent); }}
      .od-image img {{ width: 100%; height: 100%; display: block; }}
      .od-shape {{ border-radius: 0; }}
      .od-missing {{ display: flex; align-items: center; justify-content: center; color: var(--od-muted); border: 1px dashed var(--od-structural-border); }}
      .od-table {{ width: 100%; height: 100%; border-collapse: collapse; table-layout: fixed; font-size: 20px; line-height: 1.18; background: var(--od-surface); }}
      .od-table th, .od-table td {{ border: 1px solid var(--od-structural-border); padding: 8px 10px; vertical-align: middle; overflow-wrap: anywhere; }}
      .od-table th {{ background: var(--od-ink); color: var(--od-surface); }}
      .od-slide-rule {{ position: absolute; left: 110px; right: 110px; top: 620px; height: 2px; background: var(--od-accent); opacity: .55; }}
      @media (prefers-reduced-motion: reduce) {{
        html {{ scroll-behavior: auto; }}
      }}
      @media print {{
        html, body {{ background: white; }}
        .od-deck {{ display: block; padding: 0; gap: 0; }}
        .deck-slide {{
          display: block !important; opacity: 1 !important; transform: none !important;
          box-shadow: none; break-after: page; page-break-after: always;
        }}
        .deck-slide:last-child {{ break-after: auto; page-break-after: auto; }}
      }}
    """


def _collect_font_chars(placements: list[DeckHtmlPlacement], ctx: ToolContext) -> dict[str, set[str]]:
    fonts: dict[str, set[str]] = {}
    for p in placements:
        if p.kind not in {"text", "metric", "quote", "caption"}:
            continue
        family = p.font_family or ctx.settings.default_text_font
        text = (p.text or "").upper() if p.text_transform == "uppercase" else (p.text or "")
        fonts.setdefault(family, set()).update(text)
    return fonts


def _grid_boxes(x: int, y: int, w: int, h: int, n: int, *, max_cols: int = 2) -> list[dict[str, int]]:
    if n <= 0:
        return []
    cols = min(max_cols, max(1, n))
    rows = (n + cols - 1) // cols
    gap = 26
    cell_w = (w - gap * (cols - 1)) // cols
    cell_h = (h - gap * (rows - 1)) // rows
    boxes: list[dict[str, int]] = []
    for i in range(n):
        col = i % cols
        row = i // cols
        boxes.append({
            "x": x + col * (cell_w + gap),
            "y": y + row * (cell_h + gap),
            "w": cell_w,
            "h": cell_h,
        })
    return boxes


def _block_height(block: dict[str, Any], available_h: int, count: int) -> int:
    kind = block.get("kind")
    if kind == "metric":
        return min(210, max(120, available_h // max(1, count)))
    if (block.get("role") or "") == "title":
        return 150
    if (block.get("role") or "") == "subtitle":
        return 80
    if kind == "quote":
        return 230
    return min(260, max(90, available_h // max(1, count)))


def _bbox_from_block(block: dict[str, Any]) -> dict[str, int] | None:
    bbox = block.get("bbox")
    if not isinstance(bbox, dict):
        return None
    try:
        x, y, w, h = int(bbox["x"]), int(bbox["y"]), int(bbox["w"]), int(bbox["h"])
    except Exception:
        return None
    if w <= 0 or h <= 0:
        return None
    return {"x": x, "y": y, "w": w, "h": h}


def _bbox_dict(bbox: Any) -> dict[str, int] | None:
    if bbox is None:
        return None
    try:
        return {
            "x": int(getattr(bbox, "x")),
            "y": int(getattr(bbox, "y")),
            "w": int(getattr(bbox, "w")),
            "h": int(getattr(bbox, "h")),
        }
    except Exception:
        if isinstance(bbox, dict):
            try:
                return {k: int(bbox[k]) for k in ("x", "y", "w", "h")}
            except Exception:
                return None
        return None


def _style_from_child(child: Any) -> dict[str, Any]:
    effects = getattr(child, "effects", None)
    fill = getattr(effects, "fill", None) if effects is not None else None
    return {
        "font_family": getattr(child, "font_family", None),
        "font_size_px": getattr(child, "font_size_px", None),
        "align": getattr(child, "align", None),
        "fill": fill,
    }


def _infer_layout(blocks: list[dict[str, Any]], *, idx: int, total: int) -> str:
    if idx == 0:
        return "full_bleed_cover"
    if idx == total - 1:
        return "closing_action"
    visuals = [b for b in blocks if b.get("kind") in {"image", "table"}]
    metrics = [b for b in blocks if b.get("kind") == "metric"]
    if len(visuals) >= 3:
        return "visual_grid"
    if len(metrics) >= 2:
        return "metric_cards"
    if visuals:
        return "editorial_split"
    return "comparison" if idx % 2 else "process_flow"


def _first_text(blocks: list[dict[str, Any]], *, role_hint: str) -> str | None:
    for b in blocks:
        if b.get("kind") == "text" and role_hint in str(b.get("role") or b.get("block_id") or "").lower():
            text = b.get("text")
            return str(text) if text else None
    return None


def _block_text(block: dict[str, Any]) -> str:
    if block.get("kind") == "metric":
        title = str(block.get("title") or "").strip()
        text = str(block.get("text") or "").strip()
        items = [str(i).strip() for i in block.get("items") or [] if str(i).strip()]
        return "\n".join([v for v in [title, text, *items] if v])
    if block.get("kind") == "quote":
        text = str(block.get("text") or "").strip()
        title = str(block.get("title") or "").strip()
        return f'"{text}"\n{title}' if title else f'"{text}"'
    items = [str(i).strip() for i in block.get("items") or [] if str(i).strip()]
    text = str(block.get("text") or "").strip()
    title = str(block.get("title") or "").strip()
    values = [v for v in [title, text, *items] if v]
    return "\n".join(values)


def _style_box(bbox: dict[str, int], z: int) -> str:
    return (
        f'left:{int(bbox["x"])}px;top:{int(bbox["y"])}px;'
        f'width:{int(bbox["w"])}px;height:{int(bbox["h"])}px;z-index:{int(z)};'
    )


def _data_attrs(p: DeckHtmlPlacement, kind: str) -> str:
    attrs = (
        f'data-layer-id="{_attr(p.block_id)}" '
        f'data-kind="{_attr(kind)}" '
        f'data-role="{_attr(p.role or "")}" '
        f'data-layer-name="{_attr(p.role or p.block_id)}" '
        f'data-z-index="{_z_for_kind(p.kind, p.role)}"'
    )
    if p.slot_id:
        attrs += f' data-slot-id="{_attr(p.slot_id)}"'
    if p.panel_role:
        attrs += f' data-panel-role="{_attr(p.panel_role)}"'
    if p.layout_archetype:
        attrs += f' data-layout-archetype="{_attr(p.layout_archetype)}"'
    if p.source:
        attrs += f' data-source="{_attr(p.source)}"'
    if p.source_id:
        attrs += f' data-source-id="{_attr(p.source_id)}"'
    if p.evidence_quote:
        attrs += f' data-evidence-quote="{_attr(p.evidence_quote)}"'
    if p.evidence_source:
        attrs += f' data-evidence-source="{_attr(p.evidence_source)}"'
    if p.covers:
        attrs += f' data-covers="{_attr(json.dumps(p.covers, separators=(",", ":")))}"'
    return attrs


def _table_has_content(block: dict[str, Any]) -> bool:
    rows = list(block.get("rows") or [])
    if not block.get("headers") and rows:
        rows = rows[1:]
    return any(str(cell).strip() for row in rows for cell in row)


def _valid_hex_color(value: Any, fallback: str) -> str:
    candidate = str(value or "").strip()
    if len(candidate) == 7 and candidate.startswith("#"):
        try:
            int(candidate[1:], 16)
        except ValueError:
            pass
        else:
            return candidate.upper()
    return fallback.upper()


def _accessible_color(value: Any, backgrounds: list[str], fallback: str) -> str:
    candidate = _valid_hex_color(value, fallback)
    choices = [candidate, _valid_hex_color(fallback, "#111111"), "#111111", "#FFFFFF"]
    for color in dict.fromkeys(choices):
        if all(_contrast_ratio(color, background) >= 4.5 for background in backgrounds):
            return color
    return max(
        dict.fromkeys(choices),
        key=lambda color: min(_contrast_ratio(color, background) for background in backgrounds),
    )


def _nontext_contrast_color(value: Any, backgrounds: list[str], fallback: str) -> str:
    candidate = _valid_hex_color(value, fallback)
    choices = [candidate, _valid_hex_color(fallback, "#767676"), "#767676", "#111111"]
    for color in dict.fromkeys(choices):
        if all(_contrast_ratio(color, background) >= 3.0 for background in backgrounds):
            return color
    return max(
        dict.fromkeys(choices),
        key=lambda color: min(_contrast_ratio(color, background) for background in backgrounds),
    )


def _contrast_ratio(foreground: str, background: str) -> float:
    fg = _relative_luminance(foreground)
    bg = _relative_luminance(background)
    lighter, darker = max(fg, bg), min(fg, bg)
    return (lighter + 0.05) / (darker + 0.05)


def _relative_luminance(color: str) -> float:
    normalized = _valid_hex_color(color, "#000000")
    channels = [int(normalized[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        for value in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _z_for_kind(kind: str, role: str) -> int:
    if kind == "image":
        return 5 if role != "background" else 1
    if kind in {"shape", "table"}:
        return 6
    return 20


def _inline_image(path_like: str) -> str:
    path = Path(path_like)
    mime, _ = mimetypes.guess_type(path.name)
    mime = mime or "image/png"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


def _attr(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _css_string(s: str) -> str:
    return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _font_weight(value: Any, family: str | None = None) -> int:
    if value is None:
        return 700 if family and "bold" in family.lower() else 400
    try:
        weight = int(float(value))
    except (TypeError, ValueError):
        return 700 if family and "bold" in family.lower() else 400
    return max(100, min(900, weight))


def _font_style(value: Any) -> str:
    return "italic" if value == "italic" else "normal"


def _text_transform(value: Any) -> str:
    return "uppercase" if value == "uppercase" else "none"


def _float_value(value: Any, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _line_height(value: Any, *, role: str, kind: str) -> float:
    default = 0.96 if role in {"title", "title_center", "closing"} or kind == "quote" else 1.2 if kind == "caption" else 1.08
    return _float_value(value, default)
