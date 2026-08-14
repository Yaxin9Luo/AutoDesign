"""Generic renderer for the canonical `HtmlArtifactSpec` scene graph.

Target renderers still own final product exports, but this module provides a
small common DOM contract for tests, parsers, and future shared composition:
`.od-artifact` -> `.od-frame` -> `.od-layer`.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class HtmlArtifactRenderResult:
    frame_count: int
    block_count: int
    frame_kinds: dict[str, int] = field(default_factory=dict)


def write_html_artifact(artifact: Any, out_path: Path) -> HtmlArtifactRenderResult:
    data = artifact.model_dump(mode="json") if hasattr(artifact, "model_dump") else dict(artifact or {})
    frames = [f for f in data.get("frames") or [] if isinstance(f, dict)]
    target = str(data.get("target") or "artifact")
    title = str(data.get("title") or "AutoDesign artifact")
    frame_html: list[str] = []
    frame_kinds: dict[str, int] = {}
    block_count = 0
    for idx, frame in enumerate(frames):
        kind = str(frame.get("kind") or "canvas")
        frame_kinds[kind] = frame_kinds.get(kind, 0) + 1
        frame_id = str(frame.get("frame_id") or f"frame_{idx + 1:02d}")
        layout_plan = frame.get("layout_plan") if isinstance(frame.get("layout_plan"), dict) else {}
        layout_archetype = str(layout_plan.get("archetype") or frame.get("layout") or "")
        blocks = [b for b in frame.get("blocks") or [] if isinstance(b, dict)]
        block_count += len(_flatten(blocks))
        frame_html.append(
            f'<section class="od-frame" data-frame-kind="{_attr(kind)}" '
            f'data-frame-id="{_attr(frame_id)}" data-layer-id="{_attr(frame_id)}" '
            f'data-layout="{_attr(frame.get("layout") or "")}" '
            f'data-layout-archetype="{_attr(layout_archetype)}">'
            + "".join(_block_html(block, parent_bbox=None) for block in blocks)
            + "</section>"
        )
    doc = "\n".join([
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="generator" content="AutoDesign">',
        f"<title>{html.escape(title[:120])}</title>",
        "<style>",
        _css(),
        "</style>",
        "</head>",
        "<body>",
        f'<main class="od-artifact" data-od-artifact-type="{_attr(target)}">',
        *frame_html,
        "</main>",
        "</body>",
        "</html>",
    ])
    out_path.write_text(doc, encoding="utf-8")
    return HtmlArtifactRenderResult(
        frame_count=len(frames),
        block_count=block_count,
        frame_kinds=frame_kinds,
    )


def _block_html(block: dict[str, Any], *, parent_bbox: dict[str, int] | None) -> str:
    block_id = str(block.get("block_id") or block.get("layer_id") or "block")
    kind = str(block.get("kind") or "text")
    role = str(block.get("role") or "")
    slot_id = str(block.get("slot_id") or (block_id if kind == "group" else "") or "")
    panel_role = str(block.get("panel_role") or (role if kind == "group" else "") or "")
    bbox = _abs_or_relative_bbox(block.get("bbox"), parent_bbox)
    style = _style(block, bbox=bbox)
    attrs = (
        f'data-layer-id="{_attr(block_id)}" data-kind="{_attr(kind)}" '
        f'data-role="{_attr(role)}" data-layer-name="{_attr(block.get("title") or role or block_id)}"'
    )
    if slot_id:
        attrs += f' data-slot-id="{_attr(slot_id)}"'
    if panel_role:
        attrs += f' data-panel-role="{_attr(panel_role)}"'
    if block.get("source") is not None:
        attrs += f' data-source="{_attr(block.get("source"))}"'
    if block.get("source_id") is not None:
        attrs += f' data-source-id="{_attr(block.get("source_id"))}"'
    if kind == "group":
        return f'<div class="od-layer od-group" {attrs} style="{style}">' + "".join(
            _block_html(child, parent_bbox=bbox) for child in block.get("children") or [] if isinstance(child, dict)
        ) + "</div>"
    if kind in {"image", "chart", "embed"}:
        src = str(block.get("src_path") or "")
        inner = f'<img src="{_attr(src)}" alt="{_attr(role or block_id)}">' if src else ""
        return f'<figure class="od-layer od-image" {attrs} style="{style}">{inner}</figure>'
    if kind == "table":
        return f'<div class="od-layer od-table" {attrs} style="{style}">{_table(block)}</div>'
    if kind == "shape":
        fill = str((block.get("style") or {}).get("fill") or "rgba(15,23,42,.08)")
        return f'<div class="od-layer od-shape" {attrs} style="{style}background:{_attr(fill)};"></div>'
    text = _text(block)
    return (
        f'<div class="od-layer od-text od-editable" {attrs} '
        f'contenteditable="{str(bool(block.get("editable", True))).lower()}" '
        f'spellcheck="false" style="{style}">{html.escape(text)}</div>'
    )


def _style(block: dict[str, Any], *, bbox: dict[str, int] | None = None) -> str:
    styles = dict(block.get("style") or {})
    pairs = ["position:absolute", "box-sizing:border-box"]
    if bbox:
        pairs.extend([
            f'left:{int(bbox.get("x") or 0)}px',
            f'top:{int(bbox.get("y") or 0)}px',
            f'width:{int(bbox.get("w") or 0)}px',
            f'height:{int(bbox.get("h") or 0)}px',
        ])
    for key, css_key in (
        ("font_family", "font-family"),
        ("fontFamily", "font-family"),
        ("font_size_px", "font-size"),
        ("fontSize", "font-size"),
        ("font_weight", "font-weight"),
        ("fontWeight", "font-weight"),
        ("line_height", "line-height"),
        ("lineHeight", "line-height"),
        ("letter_spacing", "letter-spacing"),
        ("letterSpacing", "letter-spacing"),
        ("fill", "color"),
        ("color", "color"),
        ("align", "text-align"),
        ("textAlign", "text-align"),
    ):
        if key not in styles:
            continue
        value = styles[key]
        if key == "font_size_px":
            value = f"{int(value)}px"
        elif key == "letter_spacing":
            value = f"{float(value):g}px"
        pairs.append(f"{css_key}:{value}")
    return ";".join(pairs)


def _abs_or_relative_bbox(value: Any, parent_bbox: dict[str, int] | None) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    try:
        bbox = {key: int(value[key]) for key in ("x", "y", "w", "h")}
    except (KeyError, TypeError, ValueError):
        return None
    if parent_bbox is None:
        return bbox
    looks_absolute = bbox["x"] >= parent_bbox["x"] and bbox["y"] >= parent_bbox["y"]
    if looks_absolute:
        return {**bbox, "x": bbox["x"] - parent_bbox["x"], "y": bbox["y"] - parent_bbox["y"]}
    return bbox


def _table(block: dict[str, Any]) -> str:
    headers = [str(v) for v in block.get("headers") or []]
    rows = [[str(v) for v in row] for row in block.get("rows") or []]
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _text(block: dict[str, Any]) -> str:
    values = [
        str(block.get("title") or "").strip(),
        str(block.get("text") or "").strip(),
        *(str(item).strip() for item in block.get("items") or []),
    ]
    return "\n".join(v for v in values if v)


def _flatten(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in blocks:
        out.append(block)
        out.extend(_flatten([c for c in block.get("children") or [] if isinstance(c, dict)]))
    return out


def _css() -> str:
    return (
        "html,body{margin:0;padding:0;background:#111;font-family:Inter,system-ui,sans-serif;}"
        ".od-artifact{position:relative;}"
        ".od-frame{position:relative;width:1920px;height:1080px;background:#fff;overflow:hidden;margin:32px auto;}"
        ".od-layer img{width:100%;height:100%;object-fit:contain;display:block;}"
        ".od-text{white-space:pre-wrap;overflow:hidden;}"
        "table{width:100%;border-collapse:collapse;}td,th{border:1px solid #ddd;padding:6px;}"
    )


def _attr(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)
