"""Browser-frame renderers shared by poster / landing / deck composites.

The existing HTML renderers already cover poster and landing. This module adds
the missing fixed-canvas deck frame so decks can be previewed and screenshot by
the browser before being exported to PPTX.
"""

from __future__ import annotations

import base64
import html
import mimetypes
from pathlib import Path
from typing import Any

from ._contract import ToolContext
from ._font_embed import build_font_face_css
from .pptx_renderer import (
    _is_title_child,
    _with_legacy_bbox,
    _with_section_prefix,
)
from ..util.logging import log


def write_deck_html(spec: Any, out_path: Path, ctx: ToolContext) -> None:
    """Write a self-contained, fixed-size HTML preview for a deck DesignSpec.

    Every top-level ``kind="slide"`` node becomes a ``.deck-slide`` element.
    Text/table elements carry ``.od-editable`` so the browser screenshot pass
    can hide them when producing the visual base for hybrid PPTX export.
    """
    canvas = getattr(spec, "canvas", None) or {}
    slide_w = int(canvas.get("w_px") or 1920)
    slide_h = int(canvas.get("h_px") or 1080)
    slides = [
        n for n in (getattr(spec, "layer_graph", None) or [])
        if getattr(n, "kind", None) == "slide"
    ]

    fonts_used = _collect_text_chars(slides, ctx)
    font_css = build_font_face_css(fonts_used, ctx)

    html_parts: list[str] = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="generator" content="AutoDesign">',
        '<meta name="od-artifact-type" content="deck">',
        f"<title>{html.escape(_doc_title(spec, ctx))}</title>",
        "<style>",
        _deck_css(slide_w, slide_h),
        font_css,
        "</style>",
        "</head>",
        '<body data-od-style="blank">',
        f'<main class="od-deck" data-w="{slide_w}" data-h="{slide_h}">',
    ]

    for idx, slide in enumerate(slides):
        html_parts.append(_slide_html(slide, idx, slide_w, slide_h, ctx))

    html_parts.extend(["</main>", "</body>", "</html>"])
    out_path.write_text("\n".join(html_parts), encoding="utf-8")
    log(
        "frame.deck_html.written",
        path=str(out_path),
        bytes=out_path.stat().st_size,
        slides=len(slides),
        style="blank",
    )


def _deck_css(slide_w: int, slide_h: int) -> str:
    slide_bg = "#FFFFFF"
    ink = "#0F172A"
    muted = "#64748B"
    accent = "#2563EB"
    rule = "#CBD5E1"
    return f"""
      :root {{
        --od-slide-w: {slide_w}px;
        --od-slide-h: {slide_h}px;
        --od-bg: {slide_bg};
        --od-ink: {ink};
        --od-muted: {muted};
        --od-accent: {accent};
        --od-rule: {rule};
      }}
      html, body {{
        margin: 0;
        padding: 0;
        background: #111827;
        color: var(--od-ink);
        font-family: Inter, system-ui, sans-serif;
      }}
      .od-deck {{
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 32px;
        padding: 32px;
      }}
      .deck-slide {{
        position: relative;
        width: var(--od-slide-w);
        height: var(--od-slide-h);
        overflow: hidden;
        background: var(--od-bg);
        box-shadow: 0 18px 70px rgba(0, 0, 0, 0.38);
        isolation: isolate;
      }}
      .deck-slide[data-role="section_divider"],
      .deck-slide[data-role="closing"] {{
        background:
          linear-gradient(135deg, rgba(127, 29, 29, 0.10), transparent 42%),
          var(--od-bg);
      }}
      .od-layer {{
        position: absolute;
        box-sizing: border-box;
      }}
      .od-image img,
      .od-bg img {{
        width: 100%;
        height: 100%;
        display: block;
      }}
      .od-image img {{ object-fit: contain; }}
      .od-bg img {{ object-fit: cover; }}
      .od-text {{
        white-space: pre-wrap;
        overflow-wrap: break-word;
        line-height: 1.08;
      }}
      .od-table {{
        border-collapse: collapse;
        width: 100%;
        height: 100%;
        table-layout: fixed;
        font-size: 20px;
        line-height: 1.18;
        background: rgba(255, 255, 255, 0.58);
      }}
      .od-table th,
      .od-table td {{
        border: 1px solid var(--od-rule);
        padding: 8px 10px;
        vertical-align: middle;
        overflow-wrap: anywhere;
      }}
      .od-table th {{
        background: #1F2A44;
        color: white;
        font-weight: 700;
      }}
      .od-table td {{ color: var(--od-ink); }}
      .od-table tr:nth-child(even) td {{ background: rgba(15, 23, 42, 0.035); }}
      .od-system-text {{
        color: var(--od-muted);
        font-size: 20px;
        letter-spacing: 0;
      }}
      .od-section-label {{
        color: var(--od-accent);
        font-weight: 700;
        letter-spacing: 0.08em;
        text-transform: uppercase;
      }}
      .od-callout {{
        border: 4px solid var(--od-accent);
        background: transparent;
      }}
      .od-callout-label {{
        border: 2px solid var(--od-accent);
        background: rgba(250, 247, 240, 0.94);
        color: var(--od-ink);
        padding: 8px 12px;
        font-size: 22px;
        line-height: 1.15;
      }}
    """


def _slide_html(
    slide: Any,
    idx: int,
    slide_w: int,
    slide_h: int,
    ctx: ToolContext,
) -> str:
    role = getattr(slide, "role", None) or "content"
    sid = getattr(slide, "layer_id", None) or f"slide_{idx:02d}"
    parts: list[str] = [
        (
            f'<section class="deck-slide" data-slide data-slide-index="{idx}" '
            f'data-layer-id="{_attr(sid)}" data-role="{_attr(role)}">'
        )
    ]

    children = sorted(
        list(getattr(slide, "children", None) or []),
        key=lambda c: int(getattr(c, "z_index", 0) or 0),
    )
    section_number = getattr(slide, "section_number", None)
    title_seen = False
    for child in children:
        kind = getattr(child, "kind", None)
        effective = _with_legacy_bbox(child, role)
        if kind == "text" and not title_seen and _is_title_child(effective):
            effective = _with_section_prefix(effective, section_number)
            title_seen = True
        if kind == "background":
            parts.append(_image_layer_html(effective, slide_w, slide_h, is_bg=True))
        elif kind == "image":
            parts.append(_image_layer_html(effective, slide_w, slide_h, is_bg=False))
        elif kind == "text":
            parts.append(_text_layer_html(effective, slide_w, slide_h, ctx))
        elif kind == "table":
            parts.append(_table_layer_html(effective, slide_w, slide_h))
        elif kind == "callout":
            parts.append(_callout_layer_html(effective, slide_w, slide_h))

    parts.append("</section>")
    return "\n".join(p for p in parts if p)


def _image_layer_html(node: Any, slide_w: int, slide_h: int, *, is_bg: bool) -> str:
    src_path = getattr(node, "src_path", None)
    if not src_path or not Path(src_path).exists():
        return ""
    bbox = _resolve_bbox(node, slide_w, slide_h, default_full=is_bg)
    if bbox is None:
        return ""
    x, y, w, h = bbox
    src = _inline_image(src_path)
    cls = "od-bg" if is_bg else "od-image"
    kind = "background" if is_bg else "image"
    return (
        f'<div class="od-layer {cls}" {_data_attrs(node, kind)} '
        f'style="{_style_box(x, y, w, h, getattr(node, "z_index", 0))}">'
        f'<img src="{src}" alt="{_attr(getattr(node, "name", "") or "")}">'
        "</div>"
    )


def _text_layer_html(node: Any, slide_w: int, slide_h: int, ctx: ToolContext) -> str:
    text = (getattr(node, "text", None) or "").strip()
    if not text:
        return ""
    bbox = _resolve_bbox(node, slide_w, slide_h)
    if bbox is None:
        return ""
    x, y, w, h = bbox
    family = getattr(node, "font_family", None) or ctx.settings.default_text_font
    size = int(getattr(node, "font_size_px", None) or 36)
    align = getattr(node, "align", None) or "left"
    effects = getattr(node, "effects", None)
    fill = getattr(effects, "fill", None) if effects is not None else None
    fill = fill if isinstance(fill, str) and fill else "#0F172A"
    extra_cls = " od-section-label" if getattr(node, "name", "") == "section_label" else ""
    return (
        f'<div class="od-layer od-text od-editable{extra_cls}" '
        f'{_data_attrs(node, "text")} '
        f'style="{_style_box(x, y, w, h, getattr(node, "z_index", 0))}'
        f'font-family:{_css_string(family)};font-size:{size}px;'
        f'text-align:{_attr(align)};color:{_attr(fill)};">'
        f'{html.escape(text)}'
        "</div>"
    )


def _table_layer_html(node: Any, slide_w: int, slide_h: int) -> str:
    rows = list(getattr(node, "rows", None) or [])
    headers = list(getattr(node, "headers", None) or [])
    if not rows and not headers:
        return ""
    if not headers and rows:
        headers = [str(v) for v in rows[0]]
        rows = rows[1:]
    n_cols = max(len(headers), max((len(r) for r in rows), default=0))
    if n_cols <= 0:
        return ""
    headers = ([str(v) for v in headers] + [""] * n_cols)[:n_cols]
    norm_rows = [([str(v) for v in r] + [""] * n_cols)[:n_cols] for r in rows]
    bbox = _resolve_bbox(node, slide_w, slide_h)
    if bbox is None:
        return ""
    x, y, w, h = bbox
    body = [
        f'<div class="od-layer od-editable" {_data_attrs(node, "table")} '
        f'style="{_style_box(x, y, w, h, getattr(node, "z_index", 0))}">',
        '<table class="od-table">',
        "<thead><tr>",
        *(f"<th>{html.escape(hv)}</th>" for hv in headers),
        "</tr></thead>",
        "<tbody>",
    ]
    for row in norm_rows:
        body.append("<tr>")
        body.extend(f"<td>{html.escape(v)}</td>" for v in row)
        body.append("</tr>")
    body.extend(["</tbody>", "</table>", "</div>"])
    return "".join(body)


def _callout_layer_html(node: Any, slide_w: int, slide_h: int) -> str:
    region = getattr(node, "callout_region", None)
    if region is None:
        return ""
    try:
        x, y, w, h = int(region.x), int(region.y), int(region.w), int(region.h)
    except Exception:
        return ""
    style = (getattr(node, "callout_style", None) or "highlight").lower()
    cls = "od-callout-label" if style == "label" else "od-callout"
    text = html.escape(getattr(node, "callout_text", None) or "")
    if style == "label":
        w = max(w, 180)
        h = max(h, 48)
    return (
        f'<div class="od-layer {cls}" {_data_attrs(node, "callout")} '
        f'style="{_style_box(x, y, w, h, getattr(node, "z_index", 20))}">'
        f"{text}</div>"
    )


def _resolve_bbox(node: Any, slide_w: int, slide_h: int,
                  *, default_full: bool = False) -> tuple[int, int, int, int] | None:
    bbox = getattr(node, "bbox", None)
    if bbox is not None:
        try:
            x = int(getattr(bbox, "x", 0) or 0)
            y = int(getattr(bbox, "y", 0) or 0)
            w = int(getattr(bbox, "w", slide_w) or slide_w)
            h = int(getattr(bbox, "h", slide_h) or slide_h)
            return _clamp_bbox(x, y, w, h, slide_w, slide_h)
        except Exception:
            pass
    if default_full:
        return 0, 0, slide_w, slide_h
    return None


def _clamp_bbox(x: int, y: int, w: int, h: int,
                slide_w: int, slide_h: int) -> tuple[int, int, int, int]:
    x = max(0, min(x, slide_w - 1))
    y = max(0, min(y, slide_h - 1))
    w = max(1, min(w, slide_w - x))
    h = max(1, min(h, slide_h - y))
    return x, y, w, h


def _style_box(x: int, y: int, w: int, h: int, z: Any) -> str:
    try:
        zi = int(z or 0)
    except Exception:
        zi = 0
    return f"left:{x}px;top:{y}px;width:{w}px;height:{h}px;z-index:{zi};"


def _data_attrs(node: Any, kind: str) -> str:
    return (
        f'data-layer-id="{_attr(getattr(node, "layer_id", "") or "")}" '
        f'data-kind="{_attr(kind)}" '
        f'data-layer-name="{_attr(getattr(node, "name", "") or "")}"'
    )


def _collect_text_chars(nodes: list[Any], ctx: ToolContext) -> dict[str, set[str]]:
    acc: dict[str, set[str]] = {}
    for n in nodes:
        if getattr(n, "kind", None) == "text":
            family = getattr(n, "font_family", None) or ctx.settings.default_text_font
            acc.setdefault(family, set()).update(getattr(n, "text", None) or "")
        for child in (getattr(n, "children", None) or []):
            for family, chars in _collect_text_chars([child], ctx).items():
                acc.setdefault(family, set()).update(chars)
    return acc


def _inline_image(path_like: str) -> str:
    path = Path(path_like)
    mime, _ = mimetypes.guess_type(path.name)
    mime = mime or "image/png"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


def _doc_title(spec: Any, ctx: ToolContext) -> str:
    brief = (getattr(spec, "brief", None) or "").strip()
    if brief:
        return brief[:80]
    return f"AutoDesign deck {getattr(ctx, 'run_id', '')}".strip()


def _attr(s: Any) -> str:
    return html.escape(str(s), quote=True)


def _css_string(s: str) -> str:
    # Single-quote the value so it can sit inside the outer
    # `style="..."` HTML attribute without prematurely closing it.
    # Embedding double quotes here breaks HTML parsing, dropping every
    # CSS property after font-family — which silently strips font-size,
    # color, etc. from the rendered slide and makes the editable canvas
    # look unstyled vs the actual PPTX.
    return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'") + "'"
