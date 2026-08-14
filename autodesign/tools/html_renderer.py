"""html_renderer — compose layers into a single self-contained .html file.

Poster mode only (v1.0 #6). Landing mode ships with #8's semantic schema.

Output properties:
- Pixel-accurate absolute-positioned layers matching the layer_graph (1:1
  with the PSD / SVG). Canvas size is preserved verbatim.
- Zero external dependencies: CSS + JS inline, background images as data:
  URIs, fonts embedded as WOFF2 subsets via @font-face.
- Every text layer carries the authoritative state in data-* attrs (source
  of truth for the `apply-edits` CLI round-trip in v1.0 #6.5):
  data-bbox-x / -y / -w / -h, data-font-size-px, data-fill, data-font-family.
  Inline style is derived from these; keep them in sync on every edit.
- In-browser edit toolbar (v1.0 #6):
    * Click any text layer → floating toolbar appears above it with
      font-family dropdown, font-size number input, color picker, and a
      Save button (copy-to-clipboard / download edited HTML).
    * Drag handle (⤢) at the layer's top-left lets users reposition.
    * Double-click into text → native contenteditable for content edits.
  All edits update both inline style and the data-* attrs so that the file
  round-trips losslessly.
"""

from __future__ import annotations

import base64
import html
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ._contract import ToolContext
from ._font_embed import build_font_face_css
from ..util.math_typesetting import has_tex_math, inline_katex_bundle
from ..util.logging import log


def write_html(
    layers: list[dict[str, Any]],
    cw: int,
    ch: int,
    out_path: Path,
    ctx: ToolContext,
    *,
    inline_images: bool = True,
) -> None:
    """Write a self-contained poster HTML to out_path.

    `layers` is expected sorted by z_index ascending (matches composite's
    `sorted_layers`); we paint them in that order, letting DOM order drive
    stacking.
    """
    text_layers = [L for L in layers if L["kind"] == "text" and L.get("text")]
    fonts_used: dict[str, set[str]] = {}
    for L in text_layers:
        family = L.get("font_family") or ctx.settings.default_text_font
        fonts_used.setdefault(family, set()).update(
            _display_text_html(L["text"], L.get("text_transform"))
        )

    font_face_css = build_font_face_css(fonts_used, ctx)
    bundled_families = sorted(ctx.settings.fonts.keys())

    head = _head_block(cw, ch, font_face_css, _doc_title(ctx),
                       run_id=getattr(ctx, "run_id", "") or "")
    body_parts: list[str] = [
        "<body>",
        _user_comment(),
        '<main class="od-artifact" data-od-artifact-type="poster">',
        f'<div class="canvas od-frame" data-frame-kind="canvas" data-frame-id="poster_canvas" data-w="{cw}" data-h="{ch}">',
    ]
    for L in layers:
        kind = L.get("kind")
        if kind == "background":
            body_parts.append(_background_html(L, inline_images=inline_images))
        elif kind == "text" and L.get("text"):
            body_parts.append(_text_html(L, ctx))
        elif kind == "brand_asset":
            body_parts.append(_asset_html(L, inline_images=inline_images))
        elif kind == "image" and L.get("src_path"):
            # v1.1 paper2any: ingested PDF figures / user-passthrough images
            body_parts.append(_image_html(L, inline_images=inline_images))
        elif kind == "table" and (L.get("rows") or L.get("headers")):
            # v1.2 paper2any: structured table from ingest_document →
            # native <table> on poster/landing.
            body_parts.append(_table_html(L))
        elif kind == "shape" and L.get("bbox"):
            body_parts.append(_shape_html(L))
        else:
            body_parts.append(
                f'  <!-- skipped layer kind={kind!r} id={L.get("layer_id", "?")} -->'
            )
    body_parts.append("</div>")
    body_parts.append("</main>")
    body_parts.append(_edit_toolbar_html(bundled_families))
    body_parts.append(_save_modal_html())
    body_parts.append(f"<script>{_edit_script(bundled_families)}</script>")
    body_parts.append("</body>")
    body_parts.append("</html>")

    doc = head + "\n".join(body_parts)
    out_path.write_text(doc, encoding="utf-8")
    log("html.written",
        path=str(out_path),
        bytes=out_path.stat().st_size,
        layers=len(layers),
        text_layers=len(text_layers),
        fonts=len(fonts_used))


# --- section builders -----------------------------------------------------


def _head_block(cw: int, ch: int, font_face_css: str, title: str,
                run_id: str = "") -> str:
    run_id_meta = (
        f'<meta name="ld-run-id" content="{_attr(run_id)}">\n' if run_id else ""
    )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="generator" content="AutoDesign">\n'
        + run_id_meta
        + f"<title>{html.escape(title)}</title>\n"
        "<style>\n"
        + _base_css(cw, ch)
        + _toolbar_css()
        + _modal_css()
        + f"  {font_face_css}\n"
        "</style>\n"
        "</head>\n"
    )


def _base_css(cw: int, ch: int) -> str:
    return (
        "  html, body { margin: 0; padding: 0; }\n"
        "  body { background: #111; display: flex; justify-content: center;\n"
        "         align-items: flex-start; min-height: 100vh; padding: 24px;\n"
        "         box-sizing: border-box; font-family: system-ui, sans-serif;\n"
        "         color: #eee; }\n"
        f"  .canvas {{ position: relative; width: {cw}px; height: {ch}px;\n"
        "             background: #fff; box-shadow: 0 16px 64px rgba(0,0,0,0.45);\n"
        "             overflow: hidden; }\n"
        "  .layer { position: absolute; top: 0; left: 0; }\n"
        "  .layer.bg { width: 100%; height: 100%; pointer-events: none;\n"
        "              user-select: none; }\n"
        "  .layer.bg img { width: 100%; height: 100%; display: block; }\n"
        "  .layer.brand img { width: 100%; height: 100%; display: block; }\n"
        "  .layer.text { outline: none; line-height: 1.1;\n"
        "                word-break: break-word; overflow: visible;\n"
        "                box-sizing: border-box; cursor: text; }\n"
        "  .layer.text:hover { outline: 1px dashed rgba(120,180,255,0.35);\n"
        "                      outline-offset: 2px; }\n"
        "  .layer.text.ld-active { outline: 1px solid rgba(120,180,255,0.9);\n"
        "                          outline-offset: 2px; }\n"
    )


def _toolbar_css() -> str:
    return (
        "  .ld-drag-handle { position: absolute; top: -14px; left: -14px;\n"
        "                    width: 18px; height: 18px; border-radius: 50%;\n"
        "                    background: rgba(120,180,255,0.9); color: #fff;\n"
        "                    font-size: 11px; line-height: 18px;\n"
        "                    text-align: center; cursor: grab;\n"
        "                    user-select: none; display: none;\n"
        "                    box-shadow: 0 2px 6px rgba(0,0,0,0.4);\n"
        "                    font-family: system-ui; z-index: 10; }\n"
        "  .layer.text.ld-active .ld-drag-handle,\n"
        "  .layer.image.ld-active .ld-drag-handle { display: block; }\n"
        "  .ld-drag-handle.ld-grabbing { cursor: grabbing; background: #4a9eff; }\n"
        # v2.4.3 — resize handles on .draggable-resizable images (landing)
        "  .layer.image.draggable-resizable { position: relative; }\n"
        "  .layer.image.draggable-resizable .ld-resize-handle {\n"
        "                    position: absolute; width: 12px; height: 12px;\n"
        "                    background: rgba(120,180,255,0.95); border: 1.5px solid #fff;\n"
        "                    border-radius: 2px; display: none; z-index: 10;\n"
        "                    box-shadow: 0 1px 3px rgba(0,0,0,0.4); user-select: none; }\n"
        "  .layer.image.ld-active .ld-resize-handle { display: block; }\n"
        "  .ld-resize-handle.ld-rh-nw { top: -6px; left: -6px; cursor: nwse-resize; }\n"
        "  .ld-resize-handle.ld-rh-ne { top: -6px; right: -6px; cursor: nesw-resize; }\n"
        "  .ld-resize-handle.ld-rh-sw { bottom: -6px; left: -6px; cursor: nesw-resize; }\n"
        "  .ld-resize-handle.ld-rh-se { bottom: -6px; right: -6px; cursor: nwse-resize; }\n"
        "  .ld-resize-handle.ld-grabbing { background: #4a9eff; }\n"
        "  .layer.image.draggable-resizable:hover {\n"
        "                    outline: 1px dashed rgba(120,180,255,0.35);\n"
        "                    outline-offset: 4px; }\n"
        "  .layer.image.ld-active {\n"
        "                    outline: 1px solid rgba(120,180,255,0.9);\n"
        "                    outline-offset: 4px; }\n"
        # Mobile: disable drag/resize UI (ROADMAP v2.4.3 risk note)
        "  @media (max-width: 768px) {\n"
        "    .layer.image.draggable-resizable .ld-drag-handle,\n"
        "    .layer.image.draggable-resizable .ld-resize-handle { display: none !important; }\n"
        "    .layer.image.draggable-resizable { pointer-events: auto; }\n"
        "  }\n"
        "  .ld-toolbar { position: fixed; display: none; z-index: 100;\n"
        "                background: #1f2024; color: #eee;\n"
        "                border: 1px solid #3a3d44;\n"
        "                border-radius: 8px; padding: 6px;\n"
        "                box-shadow: 0 8px 24px rgba(0,0,0,0.5);\n"
        "                font-family: system-ui, sans-serif; font-size: 12px;\n"
        "                gap: 6px; align-items: center; white-space: nowrap; }\n"
        "  .ld-toolbar.ld-visible { display: inline-flex; }\n"
        "  .ld-toolbar select, .ld-toolbar input[type=number] {\n"
        "                background: #2a2d33; color: #eee;\n"
        "                border: 1px solid #3a3d44; border-radius: 4px;\n"
        "                padding: 4px 6px; font-size: 12px; }\n"
        "  .ld-toolbar input[type=number] { width: 56px; }\n"
        "  .ld-toolbar input[type=color] { width: 28px; height: 24px;\n"
        "                padding: 0; border: 1px solid #3a3d44;\n"
        "                border-radius: 4px; background: transparent;\n"
        "                cursor: pointer; }\n"
        "  .ld-toolbar button { background: #2a2d33; color: #eee;\n"
        "                border: 1px solid #3a3d44; border-radius: 4px;\n"
        "                padding: 4px 10px; cursor: pointer; font-size: 12px; }\n"
        "  .ld-toolbar button:hover { background: #363a42; }\n"
        "  .ld-toolbar .ld-label { color: #8a8d94; font-size: 11px;\n"
        "                padding: 0 2px 0 4px; }\n"
        "  .ld-toolbar .ld-save { background: #2a5aa0;\n"
        "                border-color: #3a6ab0; }\n"
        "  .ld-toolbar .ld-save:hover { background: #3269b8; }\n"
    )


def _modal_css() -> str:
    return (
        "  .ld-modal-backdrop { position: fixed; top: 0; left: 0; right: 0;\n"
        "                bottom: 0; background: rgba(0,0,0,0.6); z-index: 500;\n"
        "                display: none; align-items: center;\n"
        "                justify-content: center;\n"
        "                font-family: system-ui, sans-serif; }\n"
        "  .ld-modal-backdrop.ld-visible { display: flex; }\n"
        "  .ld-modal { background: #1f2024; color: #eee; padding: 24px 28px;\n"
        "                border-radius: 10px; max-width: 540px;\n"
        "                box-shadow: 0 20px 60px rgba(0,0,0,0.7);\n"
        "                border: 1px solid #3a3d44; }\n"
        "  .ld-modal h3 { margin: 0 0 8px; font-size: 15px; font-weight: 600; }\n"
        "  .ld-modal p { margin: 0 0 16px; color: #b8bcc4; font-size: 13px;\n"
        "                line-height: 1.5; }\n"
        "  .ld-modal .ld-row { display: flex; gap: 8px;\n"
        "                flex-wrap: wrap; margin-bottom: 10px; }\n"
        "  .ld-modal button { background: #2a5aa0; color: #fff;\n"
        "                border: 1px solid #3a6ab0; border-radius: 6px;\n"
        "                padding: 8px 16px; cursor: pointer; font-size: 13px;\n"
        "                font-family: inherit; }\n"
        "  .ld-modal button.ld-secondary { background: #2a2d33;\n"
        "                border-color: #3a3d44; }\n"
        "  .ld-modal button:hover { filter: brightness(1.15); }\n"
        "  .ld-modal code { background: #0e0f12; padding: 2px 8px;\n"
        "                border-radius: 4px; font-size: 12px; color: #b8d4ff;\n"
        "                font-family: ui-monospace, monospace; }\n"
    )


def _user_comment() -> str:
    return (
        "<!--\n"
        "  AutoDesign HTML output.\n"
        "  \n"
        "  Click any text layer to activate its edit toolbar:\n"
        "    • double-click text to edit content (contenteditable)\n"
        "    • drag the ⤢ handle (top-left) to reposition the layer\n"
        "    • use the floating toolbar to change font, size, color\n"
        "  Click 💾 Save to copy the edited HTML or download it.\n"
        "  \n"
        "  Edits live in this browser page only. To propagate them to the\n"
        "  PSD / SVG / PNG outputs, run `autodesign apply-edits <file>`\n"
        "  on the downloaded HTML (v1.0 #6.5).\n"
        "  \n"
        "  Layer state: authoritative source is the data-* attrs on each\n"
        "  .layer element — data-bbox-x/y/w/h, data-font-size-px, data-fill,\n"
        "  data-font-family, data-layer-id, data-kind, data-z-index,\n"
        "  data-layer-name. Inline style is derived and kept in sync.\n"
        "-->"
    )


def _background_html(L: dict[str, Any], *, inline_images: bool = True) -> str:
    src = _image_src(L["src_path"], inline_images=inline_images)
    return (
        f'  <div class="layer od-layer bg" '
        f'data-layer-id="{_attr(L.get("layer_id", ""))}" '
        f'data-kind="background" '
        f'data-role="{_attr(L.get("role", "background"))}" '
        f'data-z-index="{int(L.get("z_index", 0))}">'
        f'<img src="{src}" alt=""></div>'
    )


def _asset_html(L: dict[str, Any], *, inline_images: bool = True) -> str:
    src = _image_src(L["src_path"], inline_images=inline_images)
    bbox = L.get("bbox") or {}
    return (
        f'  <div class="layer od-layer brand" '
        f'data-layer-id="{_attr(L.get("layer_id", ""))}" '
        f'data-kind="brand_asset" '
        f'data-role="{_attr(L.get("role", ""))}" '
        f'data-z-index="{int(L.get("z_index", 0))}" '
        f'data-layer-name="{_attr(L.get("name", ""))}" '
        f'style="left:{int(bbox.get("x", 0))}px; '
        f'top:{int(bbox.get("y", 0))}px; '
        f'width:{int(bbox.get("w", 0))}px; '
        f'height:{int(bbox.get("h", 0))}px;">'
        f'<img src="{src}" alt=""></div>'
    )


def _image_html(L: dict[str, Any], *, inline_images: bool = True) -> str:
    """Poster-mode `<img>` layer for v1.1 ingested figures + passthrough images.
    Native-sized PNG resized to bbox dimensions via CSS width/height on img."""
    src = _image_src(L["src_path"], inline_images=inline_images)
    bbox = L.get("bbox") or {}
    caption = L.get("caption") or ""
    return (
        f'  <div class="layer od-layer image" '
        f'data-layer-id="{_attr(L.get("layer_id", ""))}" '
        f'data-kind="image" '
        f'data-role="{_attr(L.get("role", ""))}" '
        f'data-z-index="{int(L.get("z_index", 0))}" '
        f'data-layer-name="{_attr(L.get("name", ""))}" '
        f'data-source="{_attr(L.get("source", ""))}" '
        f'data-caption="{_attr(caption)}" '
        f'data-slot-id="{_attr(L.get("slot_id", ""))}" '
        f'data-panel-role="{_attr(L.get("panel_role", ""))}" '
        f'style="left:{int(bbox.get("x", 0))}px; '
        f'top:{int(bbox.get("y", 0))}px; '
        f'width:{int(bbox.get("w", 0))}px; '
        f'height:{int(bbox.get("h", 0))}px;">'
        f'<img src="{src}" alt="{_attr(L.get("name", ""))}" '
        f'style="width:100%;height:100%;object-fit:contain;display:block;"></div>'
    )


def _shape_html(L: dict[str, Any]) -> str:
    bbox = L.get("bbox") or {}
    fill = str(L.get("fill") or "transparent")
    stroke = str(L.get("stroke") or "transparent")
    stroke_width = int(L.get("stroke_width") or 0)
    radius = int(L.get("radius") or 0)
    border = (
        f"{stroke_width}px solid {stroke}"
        if stroke_width > 0 and stroke.lower() != "transparent"
        else "0"
    )
    return (
        f'  <div class="layer od-layer shape" '
        f'data-layer-id="{_attr(L.get("layer_id", ""))}" '
        f'data-kind="shape" '
        f'data-role="{_attr(L.get("role", ""))}" '
        f'data-z-index="{int(L.get("z_index", 0))}" '
        f'data-layer-name="{_attr(L.get("name", ""))}" '
        f'data-slot-id="{_attr(L.get("slot_id", ""))}" '
        f'data-panel-role="{_attr(L.get("panel_role", ""))}" '
        f'style="left:{int(bbox.get("x", 0))}px; '
        f'top:{int(bbox.get("y", 0))}px; '
        f'width:{int(bbox.get("w", 0))}px; '
        f'height:{int(bbox.get("h", 0))}px; '
        f'box-sizing:border-box; '
        f'background:{_attr(fill)}; '
        f'border:{_attr(border)}; '
        f'border-radius:{radius}px;"></div>'
    )


def _table_html(L: dict[str, Any]) -> str:
    """Poster-mode `<table>` layer for v1.2 ingested data tables.

    Emits a semantic `<table>` with `<thead>` / `<tbody>` and inline
    CSS that renders legibly at poster scale (14-18px font, alt-row
    striping, dark header). The layer stays absolutely-positioned in
    the canvas so planner bboxes are respected.
    """
    rows = list(L.get("rows") or [])
    headers = list(L.get("headers") or [])
    col_rule = list(L.get("col_highlight_rule") or [])
    if not headers and rows:
        headers = [str(c) for c in rows[0]]
        rows = rows[1:]
    n_cols = max(len(headers), max((len(r) for r in rows), default=0))
    if n_cols == 0:
        return f'  <!-- skipped empty table id={L.get("layer_id", "?")} -->'
    headers = [str(h) for h in headers] + [""] * (n_cols - len(headers))
    rows = [[str(c) for c in r] + [""] * (n_cols - len(r)) for r in rows]

    bbox = L.get("bbox") or {}
    caption = L.get("caption") or L.get("title") or ""
    table_mode = _paper_table_mode(n_cols)

    # Resolve winner rows per column for bold highlighting.
    from ..util.table_png import _compute_winner_rows
    winner_rows = _compute_winner_rows(rows, col_rule) if col_rule else {}

    head_cells = "".join(f"<th>{_html_escape(h)}</th>" for h in headers)
    body_rows_html: list[str] = []
    for r_idx, row in enumerate(rows):
        cells: list[str] = []
        for c, val in enumerate(row):
            is_winner = winner_rows.get(c) == r_idx
            cls = ' class="ld-table-winner"' if is_winner else ""
            cells.append(f"<td{cls}>{_html_escape(val)}</td>")
        body_rows_html.append(f"<tr>{''.join(cells)}</tr>")
    body_rows = "".join(body_rows_html)
    fontsize_px = 16 if len(rows) <= 6 else 13 if len(rows) <= 14 else 11

    return (
        f'  <div class="layer od-layer table" '
        f'data-layer-id="{_attr(L.get("layer_id", ""))}" '
        f'data-kind="table" '
        f'data-role="{_attr(L.get("role", ""))}" '
        f'data-z-index="{int(L.get("z_index", 0))}" '
        f'data-layer-name="{_attr(L.get("name", ""))}" '
        f'data-col-count="{n_cols}" '
        f'data-table-mode="{_attr(table_mode)}" '
        f'data-overflow-mode="{_attr(_paper_table_overflow_mode(table_mode))}" '
        f'data-source="{_attr(L.get("source", ""))}" '
        f'data-caption="{_attr(caption)}" '
        f'data-slot-id="{_attr(L.get("slot_id", ""))}" '
        f'data-panel-role="{_attr(L.get("panel_role", ""))}" '
        f'style="left:{int(bbox.get("x", 0))}px; '
        f'top:{int(bbox.get("y", 0))}px; '
        f'width:{int(bbox.get("w", 0))}px; '
        f'height:{int(bbox.get("h", 0))}px; '
        f'overflow:auto;">'
        f'<style>.ld-table-winner{{font-weight:700;}}</style>'
        f'<table style="width:100%;border-collapse:collapse;'
        f'font-family:system-ui,sans-serif;font-size:{fontsize_px}px;'
        f'background:#fff;color:#18181b;">'
        f'<thead><tr style="background:#1F2A44;color:#fff;">{head_cells}</tr></thead>'
        f'<tbody>{body_rows}</tbody>'
        f'</table>'
        + (f'<div class="caption" style="margin-top:6px;font-size:11px;'
           f'color:#52525b;">{_html_escape(caption)}</div>' if caption else "")
        + f'</div>'
    )


def _paper_table_mode(n_cols: int) -> str:
    if n_cols > 6:
        return "local_scroll"
    return "standard"


def _paper_table_overflow_mode(table_mode: str) -> str:
    return "local_scroll" if table_mode in {"local_scroll", "summary_plus_full_scroll"} else "standard"


def _html_escape(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _text_html(L: dict[str, Any], ctx: ToolContext) -> str:
    bbox = L["bbox"]
    bx, by = int(bbox["x"]), int(bbox["y"])
    bw, bh = int(bbox["w"]), int(bbox["h"])
    font_size = int(L["font_size_px"])
    family = L.get("font_family") or ctx.settings.default_text_font
    font_weight = _font_weight_css(L.get("font_weight"), family)
    font_style = _font_style_css(L.get("font_style"))
    line_height = _float_css(L.get("line_height"), 1.1)
    letter_spacing = _float_css(L.get("letter_spacing"), 0.0)
    text_transform = _text_transform_css(L.get("text_transform"))
    align = L.get("align") or "left"
    fill = L.get("fill") or "#000000"
    effects = L.get("effects") or {}
    shadow = effects.get("shadow") or {}
    stroke = effects.get("stroke") or {}

    justify = {"left": "flex-start", "center": "center", "right": "flex-end"}[align]
    style_pairs: list[str] = [
        f"left:{bx}px", f"top:{by}px",
        f"width:{bw}px", f"height:{bh}px",
        f"font-family:'{family}'",
        f"font-size:{font_size}px",
        f"font-weight:{font_weight}",
        f"font-style:{font_style}",
        f"line-height:{line_height:g}",
        f"letter-spacing:{letter_spacing:g}px",
        f"text-transform:{text_transform}",
        f"color:{fill}",
        f"text-align:{align}",
        "display:flex",
        "align-items:center",
        f"justify-content:{justify}",
    ]
    if shadow:
        dx = int(shadow.get("dx", 0))
        dy = int(shadow.get("dy", 4))
        blur = int(shadow.get("blur", 12))
        color = shadow.get("color", "rgba(0,0,0,0.5)")
        style_pairs.append(f"text-shadow:{dx}px {dy}px {blur}px {color}")
    if stroke and int(stroke.get("width", 0)) > 0:
        sw = int(stroke["width"])
        sc = stroke.get("color", "#000000")
        style_pairs.append(f"-webkit-text-stroke:{sw}px {sc}")

    style = "; ".join(style_pairs)
    inner = html.escape(L["text"])

    # data-* attrs are authoritative for apply-edits round-trip.
    return (
        f'  <div class="layer od-layer text" '
        f'data-layer-id="{_attr(L.get("layer_id", ""))}" '
        f'data-kind="text" '
        f'data-role="{_attr(L.get("role", ""))}" '
        f'data-z-index="{int(L.get("z_index", 0))}" '
        f'data-layer-name="{_attr(L.get("name", ""))}" '
        f'data-bbox-x="{bx}" data-bbox-y="{by}" '
        f'data-bbox-w="{bw}" data-bbox-h="{bh}" '
        f'data-slot-id="{_attr(L.get("slot_id", ""))}" '
        f'data-panel-role="{_attr(L.get("panel_role", ""))}" '
        f'data-font-size-px="{font_size}" '
        f'data-font-weight="{font_weight}" '
        f'data-font-style="{_attr(font_style)}" '
        f'data-line-height="{line_height:g}" '
        f'data-letter-spacing="{letter_spacing:g}" '
        f'data-text-transform="{_attr(text_transform)}" '
        f'data-fill="{_attr(fill)}" '
        f'data-font-family="{_attr(family)}" '
        f'data-align="{_attr(align)}" '
        f'contenteditable="true" spellcheck="false" '
        f'style="{style}">'
        f'<span class="ld-drag-handle" contenteditable="false" '
        f'title="drag to reposition">⤢</span>'
        f'{inner}</div>'
    )


def _edit_toolbar_html(families: list[str]) -> str:
    opts = "".join(
        f'<option value="{_attr(f)}">{html.escape(f)}</option>' for f in families
    )
    # The layout.json button is only functional on landing pages (v2.4.3);
    # it's always in the DOM but JS hides it unless ld-artifact-type="landing".
    return (
        '<div class="ld-toolbar" id="ld-toolbar">\n'
        '  <span class="ld-label">font</span>\n'
        f'  <select id="ld-family">{opts}</select>\n'
        '  <span class="ld-label">px</span>\n'
        '  <input type="number" id="ld-size" min="8" max="999" step="1">\n'
        '  <span class="ld-label">color</span>\n'
        '  <input type="color" id="ld-color">\n'
        '  <button class="ld-save" id="ld-save">💾 Save</button>\n'
        '  <button class="ld-layout" id="ld-layout" '
        'style="display:none" '
        'title="Download positions/sizes as layout.json">'
        '📐 layout.json</button>\n'
        "</div>"
    )


def _save_modal_html() -> str:
    return (
        '<div class="ld-modal-backdrop" id="ld-modal-backdrop">\n'
        '  <div class="ld-modal" role="dialog" aria-label="Save edited HTML">\n'
        "    <h3>✓ Your edits are live in this page</h3>\n"
        "    <p>Choose how to save:</p>\n"
        '    <div class="ld-row">\n'
        '      <button id="ld-copy">📋 Copy edited HTML</button>\n'
        '      <button id="ld-download">⬇️ Download edited HTML</button>\n'
        '      <button class="ld-secondary" id="ld-close">Cancel</button>\n'
        "    </div>\n"
        "    <p>To regenerate PSD/SVG/PNG from these edits, run on the downloaded file:<br>\n"
        "      <code>autodesign apply-edits &lt;downloaded-file&gt;</code></p>\n"
        "  </div>\n"
        "</div>"
    )


def _edit_script(families: list[str]) -> str:
    """Return the inline JS as a plain string. No external deps.

    v2.4.3: the image-drag/resize branch activates only on
    `.layer.image.draggable-resizable` (landing pages — the class isn't
    emitted on poster images). State lives on `data-bbox-tx/ty/w/h` and
    is mirrored to `localStorage` under the run-id key for persistence
    across reloads.
    """
    families_json = json.dumps(families)
    # Use a raw template. Keep it readable; no f-strings so curly braces don't clash.
    template = r"""
(() => {
  const FAMILIES = __FAMILIES__;
  const toolbar = document.getElementById('ld-toolbar');
  const familySel = document.getElementById('ld-family');
  const sizeInp = document.getElementById('ld-size');
  const colorInp = document.getElementById('ld-color');
  const saveBtn = document.getElementById('ld-save');
  const layoutBtn = document.getElementById('ld-layout');
  const modal = document.getElementById('ld-modal-backdrop');
  const copyBtn = document.getElementById('ld-copy');
  const dlBtn = document.getElementById('ld-download');
  const closeBtn = document.getElementById('ld-close');
  let active = null;
  let dragging = null;      // text drag (absolute bbox) OR image drag (transform)
  let resizing = null;      // image resize via corner handle

  // v2.4.3 — detect landing mode (determines layout.json button visibility
  // + enables transform-based image drag/resize).
  const artifactTypeMeta = document.querySelector('meta[name="ld-artifact-type"]');
  const isLanding = artifactTypeMeta && artifactTypeMeta.content === 'landing';
  const runIdMeta = document.querySelector('meta[name="ld-run-id"]');
  const runId = runIdMeta ? (runIdMeta.content || '') : '';
  const LS_KEY = runId ? 'autodesign.layout.' + runId : '';
  const LEGACY_LS_KEY = runId ? 'designanything.layout.' + runId : '';
  if (isLanding && layoutBtn) layoutBtn.style.display = 'inline-block';

  // --- activation ---
  document.querySelectorAll('.layer.text').forEach(el => {
    el.addEventListener('mousedown', e => {
      // Don't steal focus from native text editing
      if (e.target.classList && e.target.classList.contains('ld-drag-handle')) return;
      setActive(el);
    });
  });
  // v2.4.3 — image layers become selectable too. Click activates them
  // (toolbar hidden since images have no font/size/color — handles only).
  document.querySelectorAll('.layer.image.draggable-resizable').forEach(el => {
    el.addEventListener('mousedown', e => {
      if (e.target.classList && (e.target.classList.contains('ld-drag-handle')
          || e.target.classList.contains('ld-resize-handle'))) return;
      setActive(el);
    });
  });

  document.addEventListener('mousedown', e => {
    const layer = e.target.closest && e.target.closest('.layer.text, .layer.image.draggable-resizable');
    const insideToolbar = e.target.closest && e.target.closest('.ld-toolbar, .ld-modal-backdrop');
    if (!layer && !insideToolbar) setActive(null);
  });

  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      if (modal.classList.contains('ld-visible')) closeModal();
      else setActive(null);
    }
  });

  function setActive(el) {
    if (active === el) return;
    if (active) active.classList.remove('ld-active');
    active = el;
    if (!el) { toolbar.classList.remove('ld-visible'); return; }
    el.classList.add('ld-active');
    // Text toolbar is text-only; hide it for image selection.
    if (el.classList.contains('text')) {
      const fam = el.getAttribute('data-font-family') || FAMILIES[0];
      if (!Array.from(familySel.options).some(o => o.value === fam)) {
        const opt = document.createElement('option');
        opt.value = fam; opt.textContent = fam + ' (not bundled)'; familySel.appendChild(opt);
      }
      familySel.value = fam;
      sizeInp.value = el.getAttribute('data-font-size-px') || '';
      colorInp.value = normalizeColor(el.getAttribute('data-fill') || '#000000');
      positionToolbar(el);
      toolbar.classList.add('ld-visible');
    } else {
      toolbar.classList.remove('ld-visible');
    }
  }

  function positionToolbar(el) {
    const rect = el.getBoundingClientRect();
    const tbRect = toolbar.getBoundingClientRect();
    // Prefer above, fall back to below if no room
    let top = rect.top - tbRect.height - 8;
    if (top < 12) top = rect.bottom + 8;
    let left = rect.left;
    const maxLeft = window.innerWidth - tbRect.width - 12;
    if (left > maxLeft) left = maxLeft;
    if (left < 12) left = 12;
    toolbar.style.top = top + 'px';
    toolbar.style.left = left + 'px';
  }

  window.addEventListener('resize', () => { if (active && active.classList.contains('text')) positionToolbar(active); });
  window.addEventListener('scroll', () => { if (active && active.classList.contains('text')) positionToolbar(active); }, true);

  // --- inputs ---
  familySel.addEventListener('change', () => {
    if (!active || !active.classList.contains('text')) return;
    const f = familySel.value;
    active.setAttribute('data-font-family', f);
    active.style.fontFamily = "'" + f + "'";
  });
  sizeInp.addEventListener('input', () => {
    if (!active || !active.classList.contains('text')) return;
    const n = parseInt(sizeInp.value, 10);
    if (!(n > 0)) return;
    active.setAttribute('data-font-size-px', String(n));
    active.style.fontSize = n + 'px';
  });
  colorInp.addEventListener('input', () => {
    if (!active || !active.classList.contains('text')) return;
    const c = colorInp.value;
    active.setAttribute('data-fill', c);
    active.style.color = c;
  });

  // --- drag (text + image) ---
  document.addEventListener('pointerdown', e => {
    if (!e.target.classList || !e.target.classList.contains('ld-drag-handle')) return;
    // Dispatch by parent layer kind.
    const imageLayer = e.target.closest('.layer.image.draggable-resizable');
    if (imageLayer) {
      e.preventDefault();
      e.stopPropagation();
      e.target.classList.add('ld-grabbing');
      const startX = e.clientX, startY = e.clientY;
      const tx0 = parseFloat(imageLayer.getAttribute('data-bbox-tx') || '0');
      const ty0 = parseFloat(imageLayer.getAttribute('data-bbox-ty') || '0');
      dragging = {
        kind: 'image', layer: imageLayer, handle: e.target,
        startX, startY, tx0, ty0,
      };
      return;
    }
    const textLayer = e.target.closest('.layer.text');
    if (!textLayer) return;
    e.preventDefault();
    e.stopPropagation();
    e.target.classList.add('ld-grabbing');
    const canvas = document.querySelector('.canvas');
    const startX = e.clientX, startY = e.clientY;
    const x0 = parseInt(textLayer.getAttribute('data-bbox-x') || '0', 10);
    const y0 = parseInt(textLayer.getAttribute('data-bbox-y') || '0', 10);
    dragging = { kind: 'text', layer: textLayer, startX, startY, x0, y0, canvas, handle: e.target };
    textLayer.setPointerCapture && textLayer.setPointerCapture(e.pointerId);
  });

  // v2.4.3 — resize pointerdown (images only).
  document.addEventListener('pointerdown', e => {
    if (!e.target.classList || !e.target.classList.contains('ld-resize-handle')) return;
    const layer = e.target.closest('.layer.image.draggable-resizable');
    if (!layer) return;
    e.preventDefault();
    e.stopPropagation();
    e.target.classList.add('ld-grabbing');
    const rect = layer.getBoundingClientRect();
    const w0 = rect.width;
    const h0 = rect.height;
    const img = layer.querySelector('img');
    const aspect = (img && img.naturalWidth && img.naturalHeight)
      ? (img.naturalWidth / img.naturalHeight)
      : (w0 / Math.max(h0, 1));
    // Corner determines which edges move. "nw" both shift (drag adjusts
    // anchor too); for simplicity we fix the opposite corner by pretending
    // the top-left anchor is stable and applying scale from there.
    const corner =
      e.target.classList.contains('ld-rh-nw') ? 'nw' :
      e.target.classList.contains('ld-rh-ne') ? 'ne' :
      e.target.classList.contains('ld-rh-sw') ? 'sw' : 'se';
    resizing = {
      layer, handle: e.target, startX: e.clientX, startY: e.clientY,
      w0, h0, aspect, corner,
      tx0: parseFloat(layer.getAttribute('data-bbox-tx') || '0'),
      ty0: parseFloat(layer.getAttribute('data-bbox-ty') || '0'),
    };
  });

  document.addEventListener('pointermove', e => {
    if (dragging && dragging.kind === 'text') {
      const cRect = dragging.canvas.getBoundingClientRect();
      const scale = cRect.width / dragging.canvas.offsetWidth || 1;
      const dx = (e.clientX - dragging.startX) / scale;
      const dy = (e.clientY - dragging.startY) / scale;
      const nx = Math.round(dragging.x0 + dx);
      const ny = Math.round(dragging.y0 + dy);
      dragging.layer.setAttribute('data-bbox-x', String(nx));
      dragging.layer.setAttribute('data-bbox-y', String(ny));
      dragging.layer.style.left = nx + 'px';
      dragging.layer.style.top = ny + 'px';
      if (active === dragging.layer) positionToolbar(dragging.layer);
      return;
    }
    if (dragging && dragging.kind === 'image') {
      // Transform-based: preserves flow layout for sibling elements.
      const dx = e.clientX - dragging.startX;
      const dy = e.clientY - dragging.startY;
      const tx = Math.round(dragging.tx0 + dx);
      const ty = Math.round(dragging.ty0 + dy);
      dragging.layer.setAttribute('data-bbox-tx', String(tx));
      dragging.layer.setAttribute('data-bbox-ty', String(ty));
      applyImageTransform(dragging.layer);
      return;
    }
    if (resizing) {
      const dx = e.clientX - resizing.startX;
      const dy = e.clientY - resizing.startY;
      let nw, nh;
      // Sign of width/height change depends on which corner is dragged.
      const sx = (resizing.corner === 'ne' || resizing.corner === 'se') ? 1 : -1;
      const sy = (resizing.corner === 'sw' || resizing.corner === 'se') ? 1 : -1;
      nw = Math.max(40, resizing.w0 + sx * dx);
      nh = Math.max(40, resizing.h0 + sy * dy);
      if (e.shiftKey && resizing.aspect) {
        // Lock aspect — pick the dominant axis.
        if (Math.abs(dx) > Math.abs(dy)) nh = nw / resizing.aspect;
        else nw = nh * resizing.aspect;
      }
      nw = Math.round(nw); nh = Math.round(nh);
      resizing.layer.style.width = nw + 'px';
      resizing.layer.style.height = nh + 'px';
      resizing.layer.setAttribute('data-bbox-w', String(nw));
      resizing.layer.setAttribute('data-bbox-h', String(nh));
      // NW / N / SW corners anchor by moving tx/ty so the far corner
      // stays put visually; NE and SE don't need offset.
      if (resizing.corner === 'nw' || resizing.corner === 'sw') {
        const tx = Math.round(resizing.tx0 + (resizing.w0 - nw));
        resizing.layer.setAttribute('data-bbox-tx', String(tx));
      }
      if (resizing.corner === 'nw' || resizing.corner === 'ne') {
        const ty = Math.round(resizing.ty0 + (resizing.h0 - nh));
        resizing.layer.setAttribute('data-bbox-ty', String(ty));
      }
      applyImageTransform(resizing.layer);
    }
  });

  document.addEventListener('pointerup', () => {
    if (dragging) {
      dragging.handle.classList.remove('ld-grabbing');
      if (dragging.kind === 'image') persistLayout();
      dragging = null;
    }
    if (resizing) {
      resizing.handle.classList.remove('ld-grabbing');
      persistLayout();
      resizing = null;
    }
  });

  function applyImageTransform(el) {
    const tx = parseFloat(el.getAttribute('data-bbox-tx') || '0');
    const ty = parseFloat(el.getAttribute('data-bbox-ty') || '0');
    el.style.transform = 'translate(' + tx + 'px, ' + ty + 'px)';
  }

  // --- localStorage persist / restore ---
  function persistLayout() {
    if (!LS_KEY) return;
    const state = {};
    document.querySelectorAll('.layer.image.draggable-resizable').forEach(el => {
      const id = el.getAttribute('data-layer-id');
      if (!id) return;
      const tx = parseFloat(el.getAttribute('data-bbox-tx') || '0');
      const ty = parseFloat(el.getAttribute('data-bbox-ty') || '0');
      const w = el.getAttribute('data-bbox-w') || '';
      const h = el.getAttribute('data-bbox-h') || '';
      if (tx || ty || w || h) {
        state[id] = { tx, ty, w: w ? parseInt(w, 10) : null, h: h ? parseInt(h, 10) : null };
      }
    });
    try { localStorage.setItem(LS_KEY, JSON.stringify(state)); } catch (err) {}
  }

  function restoreLayout() {
    if (!LS_KEY) return;
    let raw;
    try {
      raw = localStorage.getItem(LS_KEY);
      if (!raw && LEGACY_LS_KEY) raw = localStorage.getItem(LEGACY_LS_KEY);
    } catch (err) { return; }
    if (!raw) return;
    let state;
    try { state = JSON.parse(raw); } catch (err) { return; }
    Object.keys(state).forEach(id => {
      const el = document.querySelector('.layer.image.draggable-resizable[data-layer-id="' + CSS.escape(id) + '"]');
      if (!el) return;
      const s = state[id];
      if (typeof s.tx === 'number') el.setAttribute('data-bbox-tx', String(s.tx));
      if (typeof s.ty === 'number') el.setAttribute('data-bbox-ty', String(s.ty));
      if (s.w) { el.style.width = s.w + 'px'; el.setAttribute('data-bbox-w', String(s.w)); }
      if (s.h) { el.style.height = s.h + 'px'; el.setAttribute('data-bbox-h', String(s.h)); }
      applyImageTransform(el);
    });
  }

  if (isLanding) restoreLayout();

  // --- save modal ---
  saveBtn.addEventListener('click', () => {
    modal.classList.add('ld-visible');
  });
  closeBtn.addEventListener('click', closeModal);
  modal.addEventListener('click', e => { if (e.target === modal) closeModal(); });
  function closeModal() { modal.classList.remove('ld-visible'); }

  function buildEditedHTML() {
    // Strip drag/resize handles + toolbar + modal + script so the output
    // is clean and the HTML doesn't accumulate nested copies if the file
    // is round-tripped. The data-bbox-* attrs + inline styles are kept so
    // the next apply-edits (or browser reload) sees the adjusted layout.
    const clone = document.documentElement.cloneNode(true);
    clone.querySelectorAll(
      '.ld-drag-handle, .ld-resize-handle, .ld-toolbar, .ld-modal-backdrop, script'
    ).forEach(n => n.remove());
    clone.querySelectorAll('.layer.ld-active').forEach(el => el.classList.remove('ld-active'));
    return '<!DOCTYPE html>\n' + clone.outerHTML;
  }

  copyBtn.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(buildEditedHTML());
      copyBtn.textContent = '✓ Copied!';
      setTimeout(() => { copyBtn.textContent = '📋 Copy edited HTML'; }, 1500);
    } catch (err) {
      alert('Copy failed: ' + err.message + '. Try Download instead.');
    }
  });

  dlBtn.addEventListener('click', () => {
    const blob = new Blob([buildEditedHTML()], { type: 'text/html;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    const stem = (document.title || 'poster').replace(/[^\w\u4e00-\u9fa5-]+/g, '_').slice(0, 40) || 'poster';
    a.href = url;
    a.download = stem + '.edited.html';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    dlBtn.textContent = '✓ Downloaded';
    setTimeout(() => { dlBtn.textContent = '⬇️ Download edited HTML'; }, 1500);
  });

  // v2.4.3 — layout.json export (landing only; button hidden on poster).
  if (layoutBtn) {
    layoutBtn.addEventListener('click', () => {
      const state = {};
      document.querySelectorAll('.layer.image.draggable-resizable').forEach(el => {
        const id = el.getAttribute('data-layer-id');
        if (!id) return;
        state[id] = {
          tx: parseFloat(el.getAttribute('data-bbox-tx') || '0'),
          ty: parseFloat(el.getAttribute('data-bbox-ty') || '0'),
          w: el.getAttribute('data-bbox-w') ? parseInt(el.getAttribute('data-bbox-w'), 10) : null,
          h: el.getAttribute('data-bbox-h') ? parseInt(el.getAttribute('data-bbox-h'), 10) : null,
        };
      });
      const payload = {
        schema: 'autodesign.layout.v1',
        run_id: runId || null,
        images: state,
      };
      const blob = new Blob([JSON.stringify(payload, null, 2)],
        { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = 'layout.json';
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
      layoutBtn.textContent = '✓ layout.json';
      setTimeout(() => { layoutBtn.textContent = '📐 layout.json'; }, 1500);
    });
  }

  function normalizeColor(c) {
    // <input type=color> only accepts #rrggbb. Expand #rgb to #rrggbb;
    // reject rgba/hsl by falling back to black.
    if (!c) return '#000000';
    if (/^#[0-9a-fA-F]{6}$/.test(c)) return c.toLowerCase();
    if (/^#[0-9a-fA-F]{3}$/.test(c)) {
      return '#' + c.slice(1).split('').map(ch => ch + ch).join('').toLowerCase();
    }
    return '#000000';
  }
})();
"""
    return template.replace("__FAMILIES__", families_json)


# --- landing mode (v1.0 #8) -----------------------------------------------


def write_landing_html(
    spec: Any,  # DesignSpec — avoid schema import cycle at module level
    out_path: Path,
    ctx: ToolContext,
) -> None:
    """Write a self-contained landing-page HTML from a section-tree spec.

    Unlike the poster renderer, this takes `spec` (not a flat `rendered_layers`
    list) because landing pages are a tree: each top-level LayerNode has
    kind="section" with text-layer children. No PNG rasterization — text
    lives directly as HTML, flow layout, max-width container.
    """
    layer_graph = list(getattr(spec, "layer_graph", []) or [])
    canvas = getattr(spec, "canvas", {}) or {}
    cw = int(canvas.get("w_px", 1200))

    # Font subsetting — walk the tree and collect every character used.
    fonts_used: dict[str, set[str]] = {}
    for family, chars in _walk_text_chars(layer_graph, ctx).items():
        fonts_used[family] = chars
    font_face_css = build_font_face_css(fonts_used, ctx)
    bundled_families = sorted(ctx.settings.fonts.keys())

    title = _doc_title(ctx)
    ds = getattr(spec, "design_system", None)
    style = (getattr(ds, "style", None) or "minimalist").lower()
    accent_override = getattr(ds, "accent_color", None) if ds else None
    style_css = _load_design_system_css(style, ctx, accent_override)
    page_subtype = _landing_page_subtype(spec)
    is_paper_project_page = page_subtype == "paper_project_page"
    art_direction = _landing_art_direction(spec, ctx)
    figure_focus_ids, sortable_table_ids = _landing_interaction_source_ids(ctx)
    if is_paper_project_page:
        style_css += "\n" + _landing_paper_project_css()

    # v2.3 — KaTeX auto-typeset when any text layer contains math
    # delimiters. Gate ensures non-math landings stay lean (~645 KB
    # savings per landing without math).
    katex_block = _inline_katex(ctx) if _has_math(layer_graph) else ""
    log("html.landing.katex",
        injected=bool(katex_block),
        bytes=len(katex_block))

    head = _landing_head_block(cw, font_face_css, title,
                               run_id=getattr(ctx, "run_id", "") or "",
                               style_name=style,
                               style_css=style_css,
                               katex_block=katex_block)

    # v1.3 — detect sections + CTA. Last section with variant=="footer"
    # is auto-upgraded to <footer> outside <main> for accessibility.
    section_nodes = [n for n in layer_graph
                     if getattr(n, "kind", None) == "section"]
    section_count = len(section_nodes)
    footer_node: Any | None = None
    if section_nodes:
        last = section_nodes[-1]
        if _section_variant(getattr(last, "name", "") or "") == "footer":
            footer_node = last

    show_nav = getattr(ds, "show_nav", None) if ds else None
    if show_nav is not None:
        need_nav = show_nav
    else:
        need_nav = section_count >= (2 if is_paper_project_page else 4)
    nav_html = (
        _landing_nav_html(
            section_nodes,
            footer_node,
            show_reading_progress=is_paper_project_page,
        )
        if need_nav else ""
    )

    sections_html: list[str] = []
    for node in layer_graph:
        kind = getattr(node, "kind", None)
        if kind == "section":
            if node is footer_node:
                # Emit the footer node OUTSIDE <main> below — skip here.
                continue
            sections_html.append(
                _landing_section_html(
                    node,
                    ctx,
                    paper_project_page=is_paper_project_page,
                    figure_focus_ids=figure_focus_ids,
                    sortable_table_ids=sortable_table_ids,
                )
            )
        elif kind == "text" and getattr(node, "text", None):
            # Orphan text at top level — wrap in an implicit section.
            sections_html.append(
                '  <section class="ld-section od-frame" data-frame-kind="section" '
                'data-frame-id="__implicit__" data-layer-id="__implicit__" '
                'data-layer-name="content">\n'
                + _landing_text_html(node, ctx)
                + "\n  </section>"
            )

    footer_html = (_landing_section_html(
                       footer_node,
                       ctx,
                       as_footer=True,
                       paper_project_page=is_paper_project_page,
                       figure_focus_ids=figure_focus_ids,
                       sortable_table_ids=sortable_table_ids,
                   )
                   if footer_node is not None else "")

    body_parts: list[str] = [
        f'<body data-ld-style="{_attr(style)}" data-page-subtype="{_attr(page_subtype)}" '
        f'data-art-direction="{_attr(art_direction)}">',
        _landing_user_comment(style),
    ]
    if nav_html:
        body_parts.append(nav_html)
    body_parts.extend([
        f'<main class="od-artifact ld-landing" data-od-artifact-type="landing" data-mode="landing" data-w="{cw}" '
        f'data-ld-style="{_attr(style)}" data-page-subtype="{_attr(page_subtype)}" '
        f'data-art-direction="{_attr(art_direction)}">',
        *sections_html,
        "</main>",
    ])
    if footer_html:
        body_parts.append(footer_html)
    if is_paper_project_page:
        body_parts.append(_landing_figure_viewer_html())
    body_parts.extend([
        _edit_toolbar_html(bundled_families),
        _save_modal_html(),
        f"<script>{_edit_script(bundled_families)}</script>",
        f"<script>{_landing_interactive_js()}</script>",
    ])
    if is_paper_project_page:
        body_parts.append(f"<script>{_landing_paper_project_js()}</script>")
    body_parts.extend(["</body>", "</html>"])

    doc = head + "\n".join(body_parts)
    out_path.write_text(doc, encoding="utf-8")
    total_texts = sum(
        1 for sec in layer_graph
        for child in ([sec] if getattr(sec, "kind", None) == "text"
                      else getattr(sec, "children", []) or [])
        if getattr(child, "kind", None) == "text"
    )
    log("html.landing.written",
        path=str(out_path),
        bytes=out_path.stat().st_size,
        sections=sum(1 for n in layer_graph if getattr(n, "kind", None) == "section"),
        text_layers=total_texts,
        fonts=len(fonts_used))


def _walk_text_chars(nodes: list, ctx: ToolContext) -> dict[str, set[str]]:
    acc: dict[str, set[str]] = {}
    for n in nodes:
        kind = getattr(n, "kind", None)
        if kind == "text":
            fam = getattr(n, "font_family", None) or ctx.settings.default_text_font
            text = getattr(n, "text", None) or ""
            acc.setdefault(fam, set()).update(
                _display_text_html(text, getattr(n, "text_transform", None))
            )
        children = getattr(n, "children", None) or []
        if children:
            for fam, chars in _walk_text_chars(children, ctx).items():
                acc.setdefault(fam, set()).update(chars)
    return acc


def _landing_head_block(cw: int, font_face_css: str, title: str,
                        run_id: str = "", style_name: str = "minimalist",
                        style_css: str = "",
                        katex_block: str = "") -> str:
    run_id_meta = (
        f'<meta name="ld-run-id" content="{_attr(run_id)}">\n' if run_id else ""
    )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="generator" content="AutoDesign">\n'
        '<meta name="ld-artifact-type" content="landing">\n'
        '<link rel="icon" href="data:,">\n'
        f'<meta name="ld-design-system" content="{_attr(style_name)}">\n'
        + run_id_meta
        + f"<title>{html.escape(title)}</title>\n"
        "<style>\n"
        "  /* --- base landing CSS --- */\n"
        + _landing_base_css(cw)
        + _toolbar_css()
        + _modal_css()
        + f"  {font_face_css}\n"
        f"\n  /* --- design-system: {style_name} --- */\n"
        + style_css + "\n"
        "</style>\n"
        # v2.3 — KaTeX bundle injected here (empty string when no math detected
        # in the layer_graph; self-contained <style>+<script> when inlined).
        + katex_block
        + "</head>\n"
    )


def _has_math(layer_graph: list[Any]) -> bool:
    """Scan the landing layer tree for KaTeX delimiters in text content.
    Returns True if ANY text layer (top-level or inside a section.children)
    contains `$…$`, `$$…$$`, `\\(…\\)`, or `\\[…\\]`. Used to gate the ~645 KB
    KaTeX injection so landings without math stay lean."""
    def scan(nodes: list[Any]) -> bool:
        for n in nodes:
            if getattr(n, "kind", None) == "text":
                t = getattr(n, "text", None) or ""
                if has_tex_math(t):
                    return True
            children = getattr(n, "children", None) or []
            if scan(children):
                return True
        return False
    return scan(layer_graph)


def _inline_katex(ctx: ToolContext) -> str:
    """Return inline `<style>` + `<script>` blocks that ship a self-contained
    KaTeX 0.16.9 bundle (CSS + core JS + auto-render). Fonts are base64-inlined
    as data: URIs so the landing HTML stays portable (no CDN, no external files).

    Only woff2 font refs are kept; the CSS's fallback `url(...woff)` /
    `url(...ttf)` declarations for each font face are stripped, since every
    modern browser supports woff2. Saves ~50 % of the CSS + font bytes vs
    keeping the fallbacks.

    Vendor files live at `assets/vendor/katex/`:
      - katex.min.css        (23 KB)
      - katex.min.js         (277 KB)
      - auto-render.min.js   (3 KB)
      - fonts/*.woff2        (16 × ~15 KB ≈ 268 KB)

    Total inline addition: ~645 KB per landing that contains math. Landings
    without math skip this entirely (gated at call site via `_has_math`).
    """
    return inline_katex_bundle(
        ctx.settings.repo_root,
        root_selector=".ld-landing",
        style_id="ld-katex-css",
        log_prefix="html.landing.katex",
    )


def _load_design_system_css(style: str, ctx: ToolContext,
                            accent_override: str | None = None) -> str:
    """Read assets/design-systems/<style>.css from the repo. Falls back to
    minimalist if the requested style isn't found. If `accent_override` is
    set, appends a `:root { --ld-accent: <hex>; }` rule so brand colors
    propagate into the style's tokens without editing the CSS file."""
    valid = {"minimalist", "editorial", "neubrutalism",
             "glassmorphism", "claymorphism", "liquid-glass"}
    if style not in valid:
        log("html.landing.unknown_style", requested=style, fallback="minimalist")
        style = "minimalist"
    css_path = (ctx.settings.repo_root / "assets" / "design-systems"
                / f"{style}.css")
    if not css_path.exists():
        log("html.landing.css_missing", path=str(css_path))
        return ""
    css = css_path.read_text(encoding="utf-8")
    if accent_override:
        # Append at the end so it wins against the :root block defined above.
        css += (
            f"\n/* accent_color override from DesignSystem.accent_color */\n"
            f":root {{ --ld-accent: {accent_override}; }}\n"
        )
    return css


def _landing_page_subtype(spec: Any) -> str:
    artifact = getattr(spec, "html_artifact", None)
    theme = getattr(artifact, "theme", None)
    if isinstance(theme, dict):
        raw = theme.get("page_subtype") or theme.get("landing_subtype") or theme.get("subtype")
        if str(raw or "").strip().lower() in {"paper_project_page", "research_project_page", "paper_page"}:
            return "paper_project_page"
    return ""


def _landing_art_direction(spec: Any, ctx: ToolContext) -> str:
    artifact = getattr(spec, "html_artifact", None)
    theme = getattr(artifact, "theme", None)
    if isinstance(theme, dict):
        raw = theme.get("art_direction") or theme.get("selected_art_direction")
        if str(raw or "").strip():
            return str(raw).strip()
    panel_plan = ctx.state.get("paper_project_panel_plan") if isinstance(ctx.state, dict) else {}
    if isinstance(panel_plan, dict) and str(panel_plan.get("selected_art_direction") or "").strip():
        return str(panel_plan.get("selected_art_direction")).strip()
    return ""


def _landing_interaction_source_ids(ctx: ToolContext) -> tuple[set[str], set[str]]:
    panel_plan = ctx.state.get("paper_project_panel_plan") if isinstance(ctx.state, dict) else {}
    contract = panel_plan.get("interaction_contract") if isinstance(panel_plan, dict) else {}
    if not isinstance(contract, dict):
        return set(), set()
    selected = {str(value) for value in (contract.get("selected") or [])}
    eligible = contract.get("eligible_source_ids")
    if not isinstance(eligible, dict):
        return set(), set()
    figure_ids = (
        {str(value) for value in (eligible.get("source_figure_focus_viewer") or [])}
        if "source_figure_focus_viewer" in selected else set()
    )
    table_ids = (
        {str(value) for value in (eligible.get("sortable_result_table") or [])}
        if "sortable_result_table" in selected else set()
    )
    return figure_ids, table_ids


def _landing_base_css(cw: int) -> str:
    """Structural landing CSS only — no colors/backgrounds/shadows/typography.
    All visual design is owned by `assets/design-systems/<style>.css`, which
    is appended AFTER this base block and wins for any overlapping rule."""
    return (
        "  html, body { margin: 0; padding: 0; }\n"
        f"  .ld-landing {{ max-width: {cw}px; margin: 0 auto;\n"
        "             min-height: 100vh; }\n"
        "  .ld-section { display: flex; flex-direction: column;\n"
        "             position: relative; }\n"
        "  .ld-landing .layer.text { outline: none; word-break: break-word;\n"
        "             box-sizing: border-box; cursor: text; margin: 0; }\n"
        "  .ld-landing .layer.text:hover { outline: 1px dashed rgba(120,180,255,0.35);\n"
        "             outline-offset: 2px; }\n"
        "  .ld-landing .layer.text.ld-active { outline: 1px solid rgba(120,180,255,0.9);\n"
        "             outline-offset: 4px; }\n"
        "  /* Drag handle hidden in landing mode — flow layout doesn't drag. */\n"
        "  .ld-landing .ld-drag-handle { display: none !important; }\n"
        "  /* v1.2 paper2any: ingested table layer. */\n"
        "  .ld-landing .layer.table { margin: 1.25em 0; overflow-x: auto; }\n"
        "  .ld-landing .ld-table { width: 100%; border-collapse: collapse;\n"
        "             font-size: 0.95em; }\n"
        "  .ld-landing .ld-table thead tr { background: #1F2A44; color: #fff; }\n"
        "  .ld-landing .ld-table th, .ld-landing .ld-table td {\n"
        "             padding: 0.5em 0.75em; border-bottom: 1px solid rgba(0,0,0,0.08);\n"
        "             text-align: left; }\n"
        "  .ld-landing .ld-table tbody tr:nth-child(even) { background: rgba(0,0,0,0.03); }\n"
        "  .ld-landing .ld-table .ld-table-winner { font-weight: 700; }\n"
        "  .ld-landing .layer.table figcaption {\n"
        "             margin-top: 0.4em; font-size: 0.85em; opacity: 0.7; }\n"
        "  /* v1.3 reveal-on-scroll — structural only; per-style CSS may tune. */\n"
        "  [data-reveal] { opacity: 0; transform: translateY(12px);\n"
        "             transition: opacity .6s ease, transform .6s ease; }\n"
        "  [data-reveal].is-revealed { opacity: 1; transform: none; }\n"
        "  @media (prefers-reduced-motion: reduce) {\n"
        "    [data-reveal] { opacity: 1; transform: none; transition: none; }\n"
        "  }\n"
        "  /* v1.3 top nav — structural; per-style CSS owns colors/typography. */\n"
        "  .ld-header { display: flex; justify-content: center;\n"
        "             padding: 1rem 2rem; }\n"
        "  .ld-nav ul { list-style: none; display: flex; flex-wrap: wrap;\n"
        "             gap: 1.5rem; margin: 0; padding: 0; }\n"
        "  .ld-nav a { text-decoration: none; color: inherit;\n"
        "             padding: 0.3em 0.2em; }\n"
        "  /* v1.3 CTA — structural only; per-style CSS paints the chrome. */\n"
        "  .ld-cta { display: inline-block; text-decoration: none;\n"
        "             cursor: pointer; align-self: flex-start;\n"
        "             margin-top: 0.5em; }\n"
        "  /* Paper project page primitives: resource chips, evidence figures,\n"
        "     and citation/BibTeX blocks. Visual systems can override colors. */\n"
        "  .ld-section[data-section-variant=\"resources\"] { display: block; }\n"
        "  .ld-section[data-section-variant=\"resources\"] .layer.text {\n"
        "             margin: 0 0 1rem 0; }\n"
        "  .ld-section[data-section-variant=\"resources\"] .ld-cta {\n"
        "             margin: 0 0.75rem 0.75rem 0; }\n"
        "  .ld-section[data-section-variant=\"framework\"] figure.layer.image,\n"
        "  .ld-section[data-section-variant=\"demo\"] figure.layer.image,\n"
        "  .ld-section[data-section-variant=\"benchmark\"] figure.layer.image,\n"
        "  .ld-section[data-section-variant=\"ablation\"] figure.layer.image {\n"
        "             max-width: 100%; margin: 1.25rem 0; }\n"
        "  .ld-codeblock { white-space: pre-wrap; overflow-x: auto;\n"
        "             font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;\n"
        "             font-size: 14px; line-height: 1.45; padding: 1rem;\n"
        "             border: 1px solid rgba(0,0,0,0.12);\n"
        "             background: rgba(0,0,0,0.035); border-radius: 6px; }\n"
    )


def _landing_paper_project_css() -> str:
    return """
/* Paper project page profile.
   This intentionally overrides generic landing/page styles. Paper pages should
   read like research websites: compact identity, link row, readable abstract,
   dominant method visual, and evidence-first sections. */
body[data-page-subtype="paper_project_page"] {
  --paper-surface: #ffffff;
  --paper-surface-muted: #f5f7f8;
  --paper-ink: #15171d;
  --paper-muted: #596273;
  --paper-rule: rgba(15, 23, 42, 0.12);
  --paper-accent: #b42332;
}
.ld-landing[data-page-subtype="paper_project_page"] {
  --paper-surface: #ffffff;
  --paper-surface-muted: #f5f7f8;
  --paper-ink: #15171d;
  --paper-muted: #596273;
  --paper-rule: rgba(15, 23, 42, 0.12);
  --paper-accent: #b42332;
  max-width: none;
  margin: 0;
  background: var(--paper-surface);
  color: var(--paper-ink);
  box-shadow: none;
}
.ld-landing[data-page-subtype="paper_project_page"][data-art-direction="benchmark_dashboard"] {
  background: #f7fafc;
}
.ld-landing[data-page-subtype="paper_project_page"][data-art-direction="systems_model_card"] {
  background: #f8fafb;
}
.ld-landing[data-page-subtype="paper_project_page"][data-art-direction="demo_first_gallery"] {
  background: #fbfaf7;
}
.ld-landing[data-page-subtype="paper_project_page"][data-art-direction="dark_editorial_research"] {
  background: #111316;
  color: #f6f7f9;
}
body[data-page-subtype="paper_project_page"][data-art-direction="dark_editorial_research"] {
  --paper-ink: #ffffff;
  --paper-rule: rgba(255, 255, 255, 0.16);
  --paper-accent: #ff6675;
}
body[data-page-subtype="paper_project_page"] .ld-header {
  position: sticky;
  top: 0;
  z-index: 20;
  justify-content: center;
  border-bottom: 1px solid rgba(15, 23, 42, 0.08);
  background: rgba(255, 255, 255, 0.92);
  backdrop-filter: blur(16px);
}
.ld-landing[data-page-subtype="paper_project_page"] + footer,
.ld-landing[data-page-subtype="paper_project_page"] .ld-section {
  scroll-margin-top: 72px;
}
.ld-reading-progress {
  position: absolute;
  right: 0;
  bottom: 0;
  left: 0;
  height: 2px;
  overflow: hidden;
  background: rgba(15, 23, 42, 0.06);
}
.ld-reading-progress > span {
  display: block;
  width: 100%;
  height: 100%;
  background: var(--paper-accent);
  transform: scaleX(0);
  transform-origin: left center;
}
body[data-page-subtype="paper_project_page"] .ld-nav ul {
  justify-content: center;
  gap: 26px;
}
body[data-page-subtype="paper_project_page"] .ld-nav a {
  color: #3f4654;
  font-size: 14px;
  font-weight: 500;
  letter-spacing: 0;
}
body[data-page-subtype="paper_project_page"] .ld-nav a[aria-current="page"] {
  color: var(--paper-ink);
  box-shadow: inset 0 -2px 0 var(--paper-accent);
}
body[data-page-subtype="paper_project_page"][data-art-direction="dark_editorial_research"] .ld-header {
  border-bottom-color: rgba(255, 255, 255, 0.16);
  background: rgba(17, 19, 22, 0.96);
}
body[data-page-subtype="paper_project_page"][data-art-direction="dark_editorial_research"] .ld-reading-progress {
  background: rgba(255, 255, 255, 0.12);
}
body[data-page-subtype="paper_project_page"][data-art-direction="dark_editorial_research"] .ld-nav a[aria-current="page"] {
  color: #ffffff !important;
}
.ld-landing[data-page-subtype="paper_project_page"] [data-reveal] {
  opacity: 1;
  transform: none;
}
.ld-landing[data-page-subtype="paper_project_page"] .ld-section {
  width: 100%;
  padding: 96px max(32px, calc((100vw - 1120px) / 2));
  gap: 24px;
  border-bottom: 1px solid var(--paper-rule);
  box-sizing: border-box;
}
.ld-landing[data-page-subtype="paper_project_page"] .ld-section > .layer.text,
.ld-landing[data-page-subtype="paper_project_page"] .ld-section > .ld-codeblock,
.ld-landing[data-page-subtype="paper_project_page"] .ld-section > .layer.table {
  max-width: 960px;
  margin-left: auto;
  margin-right: auto;
}
.ld-landing[data-page-subtype="paper_project_page"] .ld-section[data-section-variant="hero"] {
  display: block !important;
  min-height: auto;
  padding-top: 72px;
  padding-bottom: 56px;
  align-items: center;
  text-align: center;
  background: var(--paper-surface-muted);
}
.ld-landing[data-page-subtype="paper_project_page"][data-art-direction="benchmark_dashboard"] .ld-section[data-section-variant="hero"] {
  background: #eef6f8;
}
.ld-landing[data-page-subtype="paper_project_page"][data-art-direction="systems_model_card"] .ld-section[data-section-variant="hero"] {
  background: #f3f5f7;
}
.ld-landing[data-page-subtype="paper_project_page"][data-art-direction="demo_first_gallery"] .ld-section[data-section-variant="hero"] {
  background: #f8f2e8;
}
.ld-landing[data-page-subtype="paper_project_page"][data-art-direction="dark_editorial_research"] .ld-section {
  border-bottom-color: rgba(255, 255, 255, 0.14);
}
.ld-landing[data-page-subtype="paper_project_page"][data-art-direction="dark_editorial_research"] .ld-section[data-section-variant="hero"] {
  background: #171a1e;
}
body[data-page-subtype="paper_project_page"][data-art-direction="dark_editorial_research"] .ld-nav a,
.ld-landing[data-page-subtype="paper_project_page"][data-art-direction="dark_editorial_research"] [data-layer-name="authors"],
.ld-landing[data-page-subtype="paper_project_page"][data-art-direction="dark_editorial_research"] [data-layer-name="meta"],
.ld-landing[data-page-subtype="paper_project_page"][data-art-direction="dark_editorial_research"] [data-layer-name="summary"] {
  color: #d9dde5 !important;
}
.ld-landing[data-page-subtype="paper_project_page"][data-art-direction="dark_editorial_research"] [data-layer-name="title"] {
  color: #ffffff !important;
}
.ld-landing[data-page-subtype="paper_project_page"] [data-layer-name="eyebrow"] {
  color: #c72534 !important;
  font-size: 12px !important;
  font-weight: 800 !important;
  letter-spacing: 0.22em !important;
  text-transform: uppercase !important;
}
.ld-landing[data-page-subtype="paper_project_page"] [data-layer-name="title"] {
  max-width: 1000px !important;
  color: #15171d !important;
  font-size: 58px !important;
  font-weight: 760 !important;
  line-height: 1.04 !important;
  letter-spacing: 0 !important;
}
.ld-landing[data-page-subtype="paper_project_page"] [data-layer-name="authors"],
.ld-landing[data-page-subtype="paper_project_page"] [data-layer-name="meta"] {
  color: #343a46 !important;
  font-size: 15px !important;
  line-height: 1.6 !important;
}
.ld-landing[data-page-subtype="paper_project_page"] [data-layer-name="summary"] {
  max-width: 800px !important;
  color: #303642 !important;
  font-size: 19px !important;
  line-height: 1.55 !important;
}
.ld-landing[data-page-subtype="paper_project_page"] [data-layer-name="pull_quote"] {
  max-width: 820px !important;
  color: #475569 !important;
  font-size: 14px !important;
  line-height: 1.55 !important;
  border-left: 3px solid #c72534;
  padding-left: 16px;
  text-align: left !important;
}
.ld-landing[data-page-subtype="paper_project_page"] .ld-section[data-section-variant="hero"] .ld-cta {
  align-self: auto;
  margin: 8px 5px 0;
}
.ld-landing[data-page-subtype="paper_project_page"] .ld-cta.resource-chip,
.ld-landing[data-page-subtype="paper_project_page"] .resource-chip {
  display: inline-flex;
  width: auto;
  min-height: 0;
  padding: 11px 18px;
  gap: 9px;
  align-items: center;
  justify-content: center;
  border-radius: 999px;
  font-size: 14px !important;
  line-height: 1.1;
}
.ld-landing[data-page-subtype="paper_project_page"] .resource-chip::before {
  content: attr(data-resource-icon);
  display: inline-flex;
  min-width: 28px;
  height: 22px;
  padding: 0 6px;
  align-items: center;
  justify-content: center;
  border-radius: 999px;
  background: rgba(15, 23, 42, 0.08);
  color: #1f2937;
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0;
}
.ld-landing[data-page-subtype="paper_project_page"] .resource-chip.unavailable {
  padding: 0 8px;
  border: 0;
  background: transparent;
  color: #8a94a6;
  font-size: 12px !important;
}
.ld-landing[data-page-subtype="paper_project_page"] .ld-section[data-section-variant="hero"] figure.layer.image {
  width: min(960px, 100%);
  margin: 34px auto 0;
  border-radius: 12px;
}
.ld-landing[data-page-subtype="paper_project_page"] .layer.image {
  border: 1px solid var(--paper-rule);
  background: var(--paper-surface);
  box-shadow: none;
}
.ld-landing[data-page-subtype="paper_project_page"] figure.layer.image img {
  width: 100%;
  height: auto;
  max-height: 620px;
  object-fit: contain;
  background: var(--paper-surface);
}
.ld-landing[data-page-subtype="paper_project_page"] .ld-figure-focus-trigger {
  position: relative;
  display: block;
  width: 100%;
  margin: 0;
  padding: 0;
  border: 0;
  background: transparent;
  color: inherit;
  cursor: zoom-in;
  text-align: inherit;
}
.ld-landing[data-page-subtype="paper_project_page"] .ld-figure-focus-trigger:focus-visible {
  outline: 3px solid var(--paper-accent);
  outline-offset: 4px;
}
.ld-landing[data-page-subtype="paper_project_page"] .ld-figure-focus-label {
  position: absolute;
  right: 12px;
  bottom: 12px;
  padding: 7px 10px;
  border: 1px solid rgba(255, 255, 255, 0.72);
  background: rgba(17, 24, 39, 0.88);
  color: #ffffff;
  font: 600 12px/1 system-ui, sans-serif;
}
.ld-landing[data-page-subtype="paper_project_page"] .ld-section[data-section-variant="abstract"] {
  display: grid !important;
  grid-template-columns: minmax(0, 0.9fr) minmax(360px, 1.1fr);
  gap: 36px;
  align-items: center;
  background: #ffffff;
}
.ld-landing[data-page-subtype="paper_project_page"] .ld-section[data-section-variant="abstract"] [data-layer-name="section_heading"] {
  grid-column: 1 / -1;
}
.ld-landing[data-page-subtype="paper_project_page"] .paper-abstract {
  max-width: 760px !important;
  color: #2f3745 !important;
  font-size: 18px !important;
  line-height: 1.78 !important;
  text-align: left !important;
}
.ld-landing[data-page-subtype="paper_project_page"] .paper-abstract p {
  margin: 0 0 1em;
}
.ld-landing[data-page-subtype="paper_project_page"] .paper-abstract p:last-child {
  margin-bottom: 0;
}
.ld-landing[data-page-subtype="paper_project_page"] .ld-section[data-section-variant="demo"][data-has-image="true"],
.ld-landing[data-page-subtype="paper_project_page"] .ld-section[data-section-variant="benchmark"][data-has-image="true"] {
  display: grid !important;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 22px;
  align-items: start;
}
.ld-landing[data-page-subtype="paper_project_page"] .ld-section[data-section-variant="demo"] [data-layer-name="section_heading"],
.ld-landing[data-page-subtype="paper_project_page"] .ld-section[data-section-variant="benchmark"] [data-layer-name="section_heading"],
.ld-landing[data-page-subtype="paper_project_page"] .ld-section[data-section-variant="demo"] [data-layer-name="body"],
.ld-landing[data-page-subtype="paper_project_page"] .ld-section[data-section-variant="benchmark"] [data-layer-name="body"],
.ld-landing[data-page-subtype="paper_project_page"] .ld-section[data-section-variant="demo"] .layer.table,
.ld-landing[data-page-subtype="paper_project_page"] .ld-section[data-section-variant="benchmark"] .layer.table {
  grid-column: 1 / -1;
}
.ld-landing[data-page-subtype="paper_project_page"] .ld-section[data-section-variant="demo"] figure.layer.image,
.ld-landing[data-page-subtype="paper_project_page"] .ld-section[data-section-variant="benchmark"] figure.layer.image {
  margin: 0;
}
.ld-landing[data-page-subtype="paper_project_page"] .ld-section[data-section-variant="demo"] figure.layer.image img,
.ld-landing[data-page-subtype="paper_project_page"] .ld-section[data-section-variant="benchmark"] figure.layer.image img {
  max-height: 420px;
}
.ld-landing[data-page-subtype="paper_project_page"] .layer.table {
  width: 100%;
  max-width: min(100%, 960px);
  box-sizing: border-box;
  overflow-x: auto;
  overflow-y: hidden;
  -webkit-overflow-scrolling: touch;
  border: 1px solid rgba(15, 23, 42, 0.10);
  border-radius: 8px;
  background: var(--paper-surface);
  box-shadow: none;
}
.ld-landing[data-page-subtype="paper_project_page"] .ld-table {
  width: max-content;
  min-width: 100%;
  max-width: none;
  border-radius: 8px;
}
.ld-landing[data-page-subtype="paper_project_page"] .ld-table thead tr {
  background: #111827 !important;
}
.ld-landing[data-page-subtype="paper_project_page"] .ld-table th,
.ld-landing[data-page-subtype="paper_project_page"] .ld-table td {
  padding: 12px 14px !important;
  vertical-align: top;
  line-height: 1.42;
}
.ld-landing[data-page-subtype="paper_project_page"] .ld-sort-button {
  display: inline-flex;
  width: 100%;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 0;
  border: 0;
  background: transparent;
  color: inherit;
  font: inherit;
  font-weight: 700;
  text-align: left;
  cursor: pointer;
}
.ld-landing[data-page-subtype="paper_project_page"] .ld-sort-button:focus-visible {
  outline: 2px solid #ffffff;
  outline-offset: 3px;
}
.ld-sort-indicator::before { content: "sort"; font-size: 10px; opacity: 0.72; }
.ld-sort-button[data-sort-direction="ascending"] .ld-sort-indicator::before { content: "asc"; }
.ld-sort-button[data-sort-direction="descending"] .ld-sort-indicator::before { content: "desc"; }
.ld-landing[data-page-subtype="paper_project_page"] [data-layer-name="section_heading"] {
  max-width: 960px !important;
  color: #15171d !important;
  font-size: 32px !important;
  font-weight: 760 !important;
  line-height: 1.15 !important;
  text-align: center !important;
}
.ld-landing[data-page-subtype="paper_project_page"] [data-layer-name="body"],
.ld-landing[data-page-subtype="paper_project_page"] [data-layer-name="feature"],
.ld-landing[data-page-subtype="paper_project_page"] [data-layer-name="flow_box"] {
  color: #303642 !important;
  font-size: 16px !important;
  line-height: 1.65 !important;
}
.ld-landing[data-page-subtype="paper_project_page"] [data-layer-name="figure_caption"] {
  color: #5f6878 !important;
  font-size: 13px !important;
  line-height: 1.45 !important;
  text-align: center !important;
}
.ld-landing[data-page-subtype="paper_project_page"] [data-layer-name="flow_box"],
.ld-landing[data-page-subtype="paper_project_page"] [data-layer-name="feature"],
.ld-landing[data-page-subtype="paper_project_page"] [data-layer-name="result_metric"] {
  display: block;
  padding: 18px 0 18px 20px;
  border: 0;
  border-left: 3px solid var(--paper-accent);
  border-radius: 0;
  background: transparent;
  box-shadow: none;
}
.ld-landing[data-page-subtype="paper_project_page"] [data-layer-name="result_metric"] {
  font-size: 18px !important;
  font-weight: 760 !important;
}
.ld-landing[data-page-subtype="paper_project_page"] .ld-table {
  font-size: 14px;
}
.ld-landing[data-page-subtype="paper_project_page"] .ld-codeblock {
  max-width: 960px;
  color: #dbeafe !important;
  background: #0f172a;
  border-color: rgba(148, 163, 184, 0.20);
  font-size: 13px !important;
}
.ld-figure-viewer[hidden] { display: none !important; }
.ld-figure-viewer {
  position: fixed;
  inset: 0;
  z-index: 200;
  display: grid;
  place-items: center;
  padding: 24px;
  box-sizing: border-box;
}
.ld-figure-viewer-backdrop {
  position: absolute;
  inset: 0;
  background: rgba(17, 24, 39, 0.86);
}
.ld-figure-viewer-panel {
  position: relative;
  z-index: 1;
  display: grid;
  width: min(1180px, 100%);
  max-height: calc(100vh - 48px);
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 16px;
  padding: 48px 18px 18px;
  box-sizing: border-box;
  border: 1px solid rgba(255, 255, 255, 0.28);
  background: #111827;
  color: #ffffff;
}
.ld-figure-viewer-panel figure {
  min-width: 0;
  margin: 0;
  text-align: center;
}
.ld-figure-viewer-panel img {
  display: block;
  max-width: 100%;
  max-height: calc(100vh - 180px);
  margin: 0 auto;
  object-fit: contain;
  background: #ffffff;
}
.ld-figure-viewer-panel figcaption {
  margin-top: 12px;
  color: #e5e7eb;
  font: 14px/1.5 system-ui, sans-serif;
}
.ld-figure-viewer button {
  min-height: 40px;
  padding: 8px 12px;
  border: 1px solid rgba(255, 255, 255, 0.42);
  border-radius: 4px;
  background: #111827;
  color: #ffffff;
  cursor: pointer;
}
.ld-figure-viewer button:focus-visible {
  outline: 3px solid #ffffff;
  outline-offset: 3px;
}
.ld-figure-viewer-close {
  position: absolute;
  top: 10px;
  right: 10px;
}
.ld-visually-hidden {
  position: absolute !important;
  width: 1px !important;
  height: 1px !important;
  padding: 0 !important;
  margin: -1px !important;
  overflow: hidden !important;
  clip: rect(0, 0, 0, 0) !important;
  white-space: nowrap !important;
  border: 0 !important;
}
body.ld-figure-viewer-open { overflow: hidden; }
@media (max-width: 760px) {
  .ld-landing[data-page-subtype="paper_project_page"] .ld-section {
    padding: 64px 22px;
  }
  .ld-landing[data-page-subtype="paper_project_page"] [data-layer-name="title"] {
    font-size: 38px !important;
  }
  .ld-landing[data-page-subtype="paper_project_page"] .ld-section[data-section-variant="abstract"],
  .ld-landing[data-page-subtype="paper_project_page"] .ld-section[data-section-variant="demo"][data-has-image="true"],
  .ld-landing[data-page-subtype="paper_project_page"] .ld-section[data-section-variant="benchmark"][data-has-image="true"] {
    grid-template-columns: 1fr;
  }
  body[data-page-subtype="paper_project_page"] .ld-header {
    justify-content: flex-start;
    overflow-x: auto;
    padding: 10px 16px;
  }
  body[data-page-subtype="paper_project_page"] .ld-nav ul {
    flex-wrap: nowrap;
    gap: 18px;
  }
  .ld-figure-viewer { padding: 10px; }
  .ld-figure-viewer-panel {
    max-height: calc(100vh - 20px);
    grid-template-columns: 1fr 1fr;
    padding: 48px 12px 12px;
  }
  .ld-figure-viewer-panel figure { grid-column: 1 / -1; grid-row: 1; }
  .ld-figure-viewer-prev { grid-column: 1; grid-row: 2; }
  .ld-figure-viewer-next { grid-column: 2; grid-row: 2; }
  .ld-figure-viewer-panel img { max-height: calc(100vh - 190px); }
}
@media (prefers-reduced-motion: reduce) {
  html { scroll-behavior: auto !important; }
  .ld-landing[data-page-subtype="paper_project_page"] *,
  .ld-figure-viewer * {
    scroll-behavior: auto !important;
    transition: none !important;
    animation: none !important;
  }
}
"""


def _landing_user_comment(style: str = "minimalist") -> str:
    return (
        "<!--\n"
        f"  AutoDesign landing page · design-system: {style}\n"
        "  \n"
        "  Sections stack in flow layout — no pixel positioning. Click any\n"
        "  text to edit with the floating toolbar (font/size/color) or\n"
        "  double-click for content edits. Save button copies/downloads.\n"
        "  \n"
        "  `autodesign apply-edits <file>` round-trips edits back into\n"
        "  a fresh run + new HTML (no PSD/SVG for landing mode).\n"
        "-->"
    )


def _landing_section_html(
    section_node: Any,
    ctx: ToolContext,
    *,
    as_footer: bool = False,
    paper_project_page: bool = False,
    figure_focus_ids: set[str] | None = None,
    sortable_table_ids: set[str] | None = None,
) -> str:
    layer_id = getattr(section_node, "layer_id", "") or ""
    name = getattr(section_node, "name", "") or "content"
    variant = _section_variant(name)
    children = getattr(section_node, "children", None) or []
    has_image = any(getattr(c, "kind", None) == "image" for c in children)
    slug = _slugify(name) or _slugify(layer_id) or f"section-{variant}"
    tag = "footer" if as_footer else "section"
    has_image_attr = ' data-has-image="true"' if has_image else ""

    parts: list[str] = [
        f'  <{tag} class="ld-section od-frame" id="sec-{_attr(slug)}"'
        f'{has_image_attr} '
        f'data-frame-kind="section" '
        f'data-frame-id="{_attr(layer_id)}" '
        f'data-layer-id="{_attr(layer_id)}" '
        f'data-kind="section" '
        f'data-layer-name="{_attr(name)}" '
        f'data-section-variant="{_attr(variant)}" '
        f'data-section-slug="{_attr(slug)}" '
        f'data-reveal="true" '
        f'data-z-index="{int(getattr(section_node, "z_index", 0) or 0)}">',
    ]
    for child in children:
        kind = getattr(child, "kind", None)
        if kind == "text" and getattr(child, "text", None):
            parts.append(_landing_text_html(child, ctx))
        elif kind == "image" and getattr(child, "src_path", None):
            source_verified = _landing_source_interaction_verified(child, ctx)
            parts.append(_landing_image_html(
                child,
                ctx,
                enable_focus_viewer=(
                    paper_project_page
                    and str(child.layer_id or "") in (figure_focus_ids or set())
                    and source_verified
                ),
                source_verified=source_verified,
            ))
        elif kind == "table" and (getattr(child, "rows", None)
                                  or getattr(child, "headers", None)):
            source_verified = _landing_source_interaction_verified(child, ctx)
            parts.append(_landing_table_html(
                child,
                ctx,
                enable_sorting=(
                    paper_project_page
                    and str(child.layer_id or "") in (sortable_table_ids or set())
                    and source_verified
                ),
                source_verified=source_verified,
            ))
        elif kind == "cta" and getattr(child, "text", None):
            parts.append(_landing_cta_html(child))
    parts.append(f"  </{tag}>")
    return "\n".join(parts)


def _landing_table_html(
    table_node: Any,
    ctx: ToolContext,
    *,
    enable_sorting: bool = False,
    source_verified: bool = False,
) -> str:
    """Flow-layout `<table>` inside a landing section. No pixel bbox —
    the table sizes itself to the section, with CSS-driven typography
    for legibility. Uses the same header-fill convention as poster."""
    rows = list(getattr(table_node, "rows", None) or [])
    headers = list(getattr(table_node, "headers", None) or [])
    col_rule = list(getattr(table_node, "col_highlight_rule", None) or [])
    if not headers and rows:
        headers = [str(c) for c in rows[0]]
        rows = rows[1:]
    n_cols = max(len(headers), max((len(r) for r in rows), default=0))
    if n_cols == 0:
        return ""
    headers = [str(h) for h in headers] + [""] * (n_cols - len(headers))
    rows = [[str(c) for c in r] + [""] * (n_cols - len(r)) for r in rows]

    layer_id = getattr(table_node, "layer_id", "") or ""
    name = getattr(table_node, "name", "") or layer_id
    caption = (getattr(table_node, "caption", None)
               or getattr(table_node, "title", None) or "")
    table_mode = _paper_table_mode(n_cols)
    source_meta = _landing_image_source_meta(layer_id, ctx)
    source_id = layer_id if source_verified else ""
    source_attrs = _landing_image_source_attrs(source_meta)

    from ..util.table_png import _compute_winner_rows
    winner_rows = _compute_winner_rows(rows, col_rule) if col_rule else {}

    sortable_columns = _landing_sortable_columns(rows, n_cols) if enable_sorting else {}
    head_cells_parts: list[str] = []
    for column, header in enumerate(headers):
        escaped_header = _html_escape(header)
        sort_kind = sortable_columns.get(column)
        if sort_kind:
            head_cells_parts.append(
                f'<th scope="col" aria-sort="none">'
                f'<button type="button" class="ld-sort-button" '
                f'data-sort-column="{column}" data-sort-kind="{_attr(sort_kind)}" '
                f'data-sort-direction="none">{escaped_header}'
                f'<span class="ld-sort-indicator" aria-hidden="true"></span>'
                f'</button></th>'
            )
        else:
            head_cells_parts.append(f'<th scope="col">{escaped_header}</th>')
    head_cells = "".join(head_cells_parts)
    body_rows_html: list[str] = []
    for r_idx, row in enumerate(rows):
        cells: list[str] = []
        for c, val in enumerate(row):
            is_winner = winner_rows.get(c) == r_idx
            cls = ' class="ld-table-winner"' if is_winner else ""
            numeric_value = (
                _landing_numeric_value(val)
                if sortable_columns.get(c) == "numeric" else None
            )
            sort_attr = (
                f' data-sort-value="{_attr(format(numeric_value, ".15g"))}"'
                if numeric_value is not None else ""
            )
            cells.append(f"<td{cls}{sort_attr}>{_html_escape(val)}</td>")
        body_rows_html.append(f"<tr>{''.join(cells)}</tr>")
    body_rows = "".join(body_rows_html)

    return (
        f'    <figure class="layer od-layer table" '
        f'data-layer-id="{_attr(layer_id)}" '
        + (f'data-source-id="{_attr(source_id)}" ' if source_id else "")
        + source_attrs
        + (
        f'data-kind="table" '
        f'data-role="{_attr(getattr(table_node, "role", "") or "")}" '
        f'data-z-index="{int(getattr(table_node, "z_index", 0) or 0)}" '
        f'data-layer-name="{_attr(name)}" '
        f'data-col-count="{n_cols}" '
        f'data-table-mode="{_attr(table_mode)}" '
        f'data-overflow-mode="{_attr(_paper_table_overflow_mode(table_mode))}">'
        f'<table class="ld-table">'
        f'<thead><tr>{head_cells}</tr></thead>'
        f'<tbody>{body_rows}</tbody>'
        f'</table>'
        + (f'<figcaption>{_html_escape(caption)}</figcaption>' if caption else "")
        + f'</figure>')
    )


def _landing_sortable_columns(
    rows: list[list[str]],
    n_cols: int,
) -> dict[int, str]:
    if len(rows) < 2:
        return {}
    sortable: dict[int, str] = {}
    for column in range(n_cols):
        values = [
            str(row[column]).strip()
            for row in rows
            if column < len(row) and str(row[column]).strip()
        ]
        if len(values) < 2 or len(set(values)) < 2:
            continue
        measures = [_landing_numeric_measure(value) for value in values]
        dimensions = {
            measure[0] for measure in measures if measure is not None
        }
        sortable[column] = (
            "numeric"
            if all(measure is not None for measure in measures)
            and len(dimensions) == 1
            else "text"
        )
    return sortable


def _landing_numeric_value(value: str) -> float | None:
    measure = _landing_numeric_measure(value)
    return measure[1] if measure is not None else None


def _landing_numeric_measure(value: str) -> tuple[str, float] | None:
    normalized = str(value or "").strip().replace(",", "")
    match = re.fullmatch(
        r"([+-]?(?:\d+(?:\.\d+)?|\.\d+))\s*"
        r"(bytes?|[kmgt]i?b|ms|s|fps|%|x|k|m|b)?",
        normalized,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    try:
        coefficient = float(match.group(1))
    except ValueError:
        return None
    unit = str(match.group(2) or "")
    unit_lower = unit.lower()
    if not unit:
        return "number", coefficient
    if unit_lower == "ms":
        return "duration_seconds", coefficient / 1000
    if unit_lower == "s":
        return "duration_seconds", coefficient
    if unit_lower in {"k", "m", "b"}:
        return "count", coefficient * {"k": 1e3, "m": 1e6, "b": 1e9}[unit_lower]
    if unit_lower in {"byte", "bytes"}:
        return "bytes", coefficient
    if unit_lower in {"kb", "mb", "gb", "tb"}:
        return "bytes", coefficient * {
            "kb": 1e3,
            "mb": 1e6,
            "gb": 1e9,
            "tb": 1e12,
        }[unit_lower]
    if unit_lower in {"kib", "mib", "gib", "tib"}:
        return "bytes", coefficient * {
            "kib": 1024,
            "mib": 1024 ** 2,
            "gib": 1024 ** 3,
            "tib": 1024 ** 4,
        }[unit_lower]
    if unit == "%":
        return "percentage", coefficient
    if unit_lower == "fps":
        return "frames_per_second", coefficient
    if unit_lower == "x":
        return "multiplier", coefficient
    return None


def _landing_image_html(
    image_node: Any,
    ctx: ToolContext,
    *,
    enable_focus_viewer: bool = False,
    source_verified: bool = False,
) -> str:
    """Inline image layer inside a landing section — embedded as data: URI.

    v2.4.3: images become draggable + resizable in the browser. Drag is
    applied via CSS `transform: translate(tx, ty)` (preserves flow layout
    for siblings); resize sets inline `width` (height follows aspect).
    State rides on `data-bbox-tx/ty/w/h` attrs; clicking an image exposes
    a drag handle + 4 corner resize handles. Mobile (<768 px) hides
    handles via media query.
    """
    src_path = getattr(image_node, "src_path", None)
    if not src_path:
        return ""
    data_uri = _inline_image(src_path)
    layer_id = getattr(image_node, "layer_id", "") or ""
    name = getattr(image_node, "name", "") or layer_id
    aspect = getattr(image_node, "aspect_ratio", None) or ""
    source_meta = _landing_image_source_meta(layer_id, ctx)
    alt = str(
        source_meta.get("caption_short")
        or source_meta.get("caption_full")
        or name.replace("_", " ")
    )
    source_id = layer_id if source_verified else ""
    source_id_attr = f'data-source-id="{_attr(source_id)}" ' if source_id else ""
    source_attrs = _landing_image_source_attrs(source_meta)
    image_html = (
        f'<img src="{data_uri}" alt="{_attr(alt)}" loading="lazy" '
        f'data-layer-id="{_attr(layer_id)}" {source_id_attr}{source_attrs}>'
    )
    if enable_focus_viewer and source_id:
        image_html = (
            f'<button type="button" class="ld-figure-focus-trigger" '
            f'data-figure-focus data-source-id="{_attr(source_id)}" '
            f'aria-haspopup="dialog" '
            f'aria-label="Open figure viewer: {_attr(alt)}">'
            f'{image_html}'
            f'<span class="ld-figure-focus-label" aria-hidden="true">Open figure</span>'
            f'</button>'
        )
    return (
        f'    <figure class="layer image od-layer draggable-resizable" '
        f'data-layer-id="{_attr(layer_id)}" '
        f'{source_id_attr}'
        f'data-kind="image" '
        f'data-role="{_attr(getattr(image_node, "role", "") or "")}" '
        f'data-z-index="{int(getattr(image_node, "z_index", 0) or 0)}" '
        f'data-layer-name="{_attr(name)}" '
        f'data-aspect-ratio="{_attr(aspect)}" '
        f'{source_attrs}'
        f'data-bbox-tx="0" data-bbox-ty="0" '
        f'data-bbox-w="" data-bbox-h="">'
        f'{image_html}'
        f'<span class="ld-drag-handle" aria-hidden="true" '
        f'title="drag to reposition">⤢</span>'
        f'<span class="ld-resize-handle ld-rh-nw" aria-hidden="true"></span>'
        f'<span class="ld-resize-handle ld-rh-ne" aria-hidden="true"></span>'
        f'<span class="ld-resize-handle ld-rh-sw" aria-hidden="true"></span>'
        f'<span class="ld-resize-handle ld-rh-se" aria-hidden="true"></span>'
        f'</figure>'
    )


def _landing_source_interaction_verified(node: Any, ctx: ToolContext) -> bool:
    from ..util.paper_project_page import is_verified_paper_source_node

    return is_verified_paper_source_node(node, ctx)


def _landing_image_source_meta(layer_id: str, ctx: ToolContext) -> dict[str, Any]:
    rendered = ctx.state.get("rendered_layers") if isinstance(ctx.state, dict) else {}
    rec = rendered.get(layer_id) if isinstance(rendered, dict) else {}
    provenance = ctx.state.get("paper_visual_provenance") if isinstance(ctx.state, dict) else {}
    asset = {}
    if isinstance(provenance, dict):
        for item in provenance.get("assets") or []:
            if isinstance(item, dict) and str(item.get("asset_id") or "") == str(layer_id):
                asset = item
                break
    meta: dict[str, Any] = {}
    for source in (rec if isinstance(rec, dict) else {}, asset):
        if not isinstance(source, dict):
            continue
        for key in (
            "caption_short",
            "caption_full",
            "caption",
            "source_page",
            "source_bbox_pdf_points",
            "visual_role",
            "visual_score",
            "curation_reason",
            "output_sha256",
            "output_width_px",
            "output_height_px",
            "material_quality",
        ):
            if key not in meta and source.get(key) is not None:
                meta[key] = source.get(key)
    return meta


def _landing_image_source_attrs(meta: dict[str, Any]) -> str:
    if not meta:
        return ""
    quality = meta.get("material_quality") if isinstance(meta.get("material_quality"), dict) else {}
    attrs: list[str] = []
    scalar_fields = {
        "source_page": meta.get("source_page"),
        "visual_role": meta.get("visual_role"),
        "visual_score": meta.get("visual_score"),
        "caption_short": meta.get("caption_short"),
        "caption_full": meta.get("caption_full") or meta.get("caption"),
        "output_sha256": meta.get("output_sha256"),
        "output_width_px": meta.get("output_width_px"),
        "output_height_px": meta.get("output_height_px"),
        "material_score": quality.get("material_score"),
        "white_ratio": quality.get("white_ratio"),
        "edge_white_ratio": quality.get("edge_white_ratio"),
    }
    for key, value in scalar_fields.items():
        if value is None or value == "":
            continue
        attrs.append(f'data-{key.replace("_", "-")}="{_attr(value)}" ')
    bbox = meta.get("source_bbox_pdf_points")
    if bbox:
        attrs.append(f'data-source-bbox="{_attr(json.dumps(bbox, separators=(",", ":")))}" ')
    flags = quality.get("warnings") if isinstance(quality.get("warnings"), list) else []
    if flags:
        attrs.append(f'data-material-warnings="{_attr(",".join(str(flag) for flag in flags))}" ')
    return "".join(attrs)


def _landing_text_html(text_node: Any, ctx: ToolContext) -> str:
    layer_id = getattr(text_node, "layer_id", "") or ""
    name = getattr(text_node, "name", "") or layer_id
    text = getattr(text_node, "text", "") or ""
    role = _landing_node_role(text_node)
    defaults = _landing_text_style_defaults(text_node, role)
    font_family = getattr(text_node, "font_family", None) or ctx.settings.default_text_font
    raw_font_size = getattr(text_node, "font_size_px", None)
    font_size = int(raw_font_size if raw_font_size is not None else defaults["font_size"])
    raw_font_weight = getattr(text_node, "font_weight", None)
    font_weight = _font_weight_css(
        raw_font_weight if raw_font_weight is not None else defaults["font_weight"],
        font_family,
    )
    font_style = _font_style_css(getattr(text_node, "font_style", None))
    line_height = _float_css(getattr(text_node, "line_height", None), defaults["line_height"])
    letter_spacing = _float_css(getattr(text_node, "letter_spacing", None), 0.0)
    text_transform = _text_transform_css(getattr(text_node, "text_transform", None))
    align = getattr(text_node, "align", None) or defaults["align"]
    effects = getattr(text_node, "effects", None)
    fill = getattr(effects, "fill", None) if effects else None
    fill = fill or "inherit"

    style_pairs: list[str] = [
        f"font-family:'{font_family}'",
        f"font-size:{font_size}px",
        f"font-weight:{font_weight}",
        f"font-style:{font_style}",
        f"line-height:{line_height:g}",
        f"letter-spacing:{letter_spacing:g}px",
        f"text-transform:{text_transform}",
        f"color:{fill}" if fill != "inherit" else "color:inherit",
        f"text-align:{align}",
    ]
    style = "; ".join(style_pairs)
    is_codeblock = role in {"bibtex", "citation", "code", "license"}
    tag = "pre" if is_codeblock else "div"
    class_names = ["layer", "od-layer", "text"]
    if is_codeblock:
        class_names.append("ld-codeblock")
    class_names.extend(_landing_text_extra_classes(name=name, role=role, text=text))
    inner = (
        html.escape(text)
        if is_codeblock
        else _landing_text_inner_html(text, name=name, role=role)
    )
    return (
        f'    <{tag} class="{" ".join(_attr(cls) for cls in class_names)}" '
        f'data-layer-id="{_attr(layer_id)}" '
        f'data-kind="text" '
        f'data-role="{_attr(role)}" '
        f'data-z-index="{int(getattr(text_node, "z_index", 0) or 0)}" '
        f'data-layer-name="{_attr(name)}" '
        f'data-font-size-px="{font_size}" '
        f'data-font-weight="{font_weight}" '
        f'data-font-style="{_attr(font_style)}" '
        f'data-line-height="{line_height:g}" '
        f'data-letter-spacing="{letter_spacing:g}" '
        f'data-text-transform="{_attr(text_transform)}" '
        f'data-fill="{_attr(fill if fill != "inherit" else "")}" '
        f'data-font-family="{_attr(font_family)}" '
        f'data-align="{_attr(align)}" '
        f'contenteditable="true" spellcheck="false" '
        f'style="{style}">'
        f"{inner}</{tag}>"
    )


def _landing_text_extra_classes(*, name: str, role: str, text: str) -> list[str]:
    blob = f"{name} {role}".lower()
    classes: list[str] = []
    if "abstract" in blob or "overview" in blob:
        classes.append("paper-abstract")
    if len(text) > 520 and ("body" in blob or "summary" in blob or "abstract" in blob):
        classes.append("paper-longform")
    return classes


def _landing_text_inner_html(text: str, *, name: str, role: str) -> str:
    blob = f"{name} {role}".lower()
    text = str(text or "")
    should_paragraph = (
        "abstract" in blob
        or "overview" in blob
        or ("body" in blob and len(text) > 520)
    )
    if not should_paragraph:
        return html.escape(text)
    parts = [part.strip() for part in re.split(r"\n\s*\n+", text) if part.strip()]
    if not parts:
        parts = [text.strip()] if text.strip() else []
    if not parts:
        return ""
    return "".join(f"<p>{html.escape(part)}</p>" for part in parts)


def _landing_text_style_defaults(text_node: Any, role: str) -> dict[str, Any]:
    text_key = " ".join(str(v or "").lower() for v in (
        getattr(text_node, "name", None),
        getattr(text_node, "layer_id", None),
        role,
    ))
    if "eyebrow" in text_key or "venue_badge" in text_key:
        return {"font_size": 12, "font_weight": 760, "line_height": 1.15, "align": "left"}
    if "section_heading" in text_key:
        return {"font_size": 32, "font_weight": 760, "line_height": 1.18, "align": "left"}
    if "title" in text_key and "subtitle" not in text_key:
        return {"font_size": 54, "font_weight": 760, "line_height": 1.08, "align": "left"}
    if "summary" in text_key or "subtitle" in text_key or "thesis" in text_key:
        return {"font_size": 22, "font_weight": 450, "line_height": 1.42, "align": "left"}
    if "authors" in text_key or "meta" in text_key or "anchor" in text_key:
        return {"font_size": 15, "font_weight": 450, "line_height": 1.55, "align": "left"}
    if "caption" in text_key:
        return {"font_size": 13, "font_weight": 450, "line_height": 1.45, "align": "left"}
    if role in {"bibtex", "citation", "code", "license"}:
        return {"font_size": 13, "font_weight": 450, "line_height": 1.5, "align": "left"}
    if "metric" in text_key or "result" in text_key:
        return {"font_size": 18, "font_weight": 720, "line_height": 1.25, "align": "left"}
    if "resource" in text_key:
        return {"font_size": 14, "font_weight": 520, "line_height": 1.45, "align": "left"}
    if "feature" in text_key or "flow_box" in text_key:
        return {"font_size": 16, "font_weight": 520, "line_height": 1.5, "align": "left"}
    return {"font_size": 16, "font_weight": 450, "line_height": 1.65, "align": "left"}


def _section_variant(name: str) -> str:
    """Map section name → CSS variant class for themed styling."""
    low = (name or "").lower()
    if any(key in low for key in ("abstract", "overview")):
        return "abstract"
    if any(key in low for key in ("resource", "links", "github", "hugging", "arxiv", "code")):
        return "resources"
    if any(key in low for key in ("framework", "architecture", "pipeline", "method", "model", "system")):
        return "framework"
    if any(key in low for key in ("demo", "gallery", "showcase", "example", "qualitative", "sample")):
        return "demo"
    if any(key in low for key in ("benchmark", "result", "table", "chart", "scaling", "leaderboard", "evidence")):
        return "benchmark"
    if "ablation" in low:
        return "ablation"
    if any(key in low for key in ("citation", "bibtex", "license", "weights", "model card")):
        return "citation"
    for key in ("hero", "features", "cta", "footer", "header"):
        if key in low:
            return key
    return "content"


def _landing_node_role(node: Any) -> str:
    raw_role = getattr(node, "role", None)
    if raw_role:
        return str(raw_role)
    text = " ".join(str(v or "").lower() for v in (
        getattr(node, "name", None),
        getattr(node, "layer_id", None),
        getattr(node, "caption", None),
    ))
    if "bibtex" in text or "citation" in text:
        return "bibtex"
    if "license" in text:
        return "license"
    if "code" in text and "github" not in text:
        return "code"
    if any(key in text for key in ("arxiv", "github", "hugging", "blog", "demo", "twitter", "weights")):
        return "resource"
    return ""


# v1.3 — interactive landing primitives -----------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    """Lowercase + collapse non-alnum to dashes + strip edges.

    Used for both `<section id>` and matching nav anchor hrefs. Must be
    idempotent so planners re-running composite get stable ids.
    """
    low = (name or "").strip().lower()
    slug = _SLUG_RE.sub("-", low).strip("-")
    return slug


def _landing_cta_html(cta_node: Any) -> str:
    """Render a v1.3 `kind="cta"` layer as a styled `<a role="button">`.

    The anchor carries all round-trip state as data-* attrs so
    `apply_edits._landing_cta_from_a` can reconstitute a LayerNode(cta).
    `contenteditable="false"` keeps the edit toolbar from accidentally
    mutating the link text (planner still revises via `edit_layer` or
    `propose_design_spec`).
    """
    layer_id = getattr(cta_node, "layer_id", "") or ""
    name = getattr(cta_node, "name", "") or layer_id or "cta"
    text = getattr(cta_node, "text", "") or ""
    href = getattr(cta_node, "href", None) or "#"
    variant = (getattr(cta_node, "variant", None) or "primary").lower()
    if variant not in ("primary", "secondary", "ghost"):
        variant = "primary"
    z = int(getattr(cta_node, "z_index", 0) or 0)
    resource_type = _cta_resource_type(name=name, text=text, href=href)
    icon = _cta_resource_icon(resource_type)
    classes = ["ld-cta", f"ld-cta--{variant}", "od-layer"]
    if resource_type:
        classes.append("resource-chip")
    if _cta_is_unavailable(text=text, href=href):
        classes.append("unavailable")
    return (
        f'    <a class="{" ".join(_attr(cls) for cls in classes)}" '
        f'role="button" href="{_attr(href)}" '
        f'contenteditable="false" '
        f'data-kind="cta" '
        f'data-role="cta" '
        f'data-resource-type="{_attr(resource_type)}" '
        f'data-resource-icon="{_attr(icon)}" '
        f'data-layer-id="{_attr(layer_id)}" '
        f'data-layer-name="{_attr(name)}" '
        f'data-variant="{_attr(variant)}" '
        f'data-href="{_attr(href)}" '
        f'data-z-index="{z}">'
        f'{html.escape(text)}'
        f'</a>'
    )


def _cta_resource_type(*, name: str, text: str, href: str) -> str:
    blob = f"{name} {text} {href}".lower()
    host = urlparse(href).netloc.lower() if href else ""
    path = urlparse(href).path.lower() if href else ""
    if "arxiv.org" in host:
        return "pdf" if path.startswith("/pdf/") else "arxiv"
    if "github.com" == host:
        return "github"
    if host.endswith("github.io") or "project" in blob:
        return "project"
    if host == "huggingface.co":
        if path.startswith("/spaces/"):
            return "demo"
        if path.startswith("/datasets/"):
            return "dataset"
        return "huggingface"
    if "twitter.com" in host or host == "x.com":
        return "twitter"
    if any(key in blob for key in ("demo", "space", "gradio", "colab")):
        return "demo"
    if any(key in blob for key in ("weight", "checkpoint", "ckpt")):
        return "weights"
    if any(key in blob for key in ("hardware", "interface", "sdk", "device")):
        return "hardware"
    if "blog" in blob or "medium.com" in host:
        return "blog"
    if any(key in blob for key in ("paper", "pdf")):
        return "pdf"
    if any(key in blob for key in ("code", "repo")):
        return "github"
    return "resource" if href and href != "#" else ""


def _cta_resource_icon(resource_type: str) -> str:
    return {
        "arxiv": "arXiv",
        "pdf": "PDF",
        "github": "GH",
        "huggingface": "HF",
        "dataset": "Data",
        "demo": "Demo",
        "project": "Web",
        "blog": "Blog",
        "twitter": "X",
        "weights": "Wts",
        "hardware": "HW",
        "resource": "Link",
    }.get(resource_type, "")


def _cta_is_unavailable(*, text: str, href: str) -> bool:
    blob = f"{text} {href}".lower()
    return (
        href in {"", "#", "todo", "tbd"}
        or "unavailable" in blob
        or "not released" in blob
        or "not found" in blob
    )


def _landing_nav_html(section_nodes: list[Any],
                      footer_node: Any | None,
                      *,
                      show_reading_progress: bool = False) -> str:
    """Build `<header><nav>…` with anchor links to each section.

    Skips `hero` and the footer-upgraded section (hero is usually the
    page's top so a nav link to itself is weird; footer lives outside
    `<main>` and has its own role). Returns empty string when the nav
    would have 0 links.
    """
    items: list[str] = []
    for node in section_nodes:
        if node is footer_node:
            continue
        name = getattr(node, "name", "") or ""
        variant = _section_variant(name)
        if variant == "hero":
            continue
        slug = _slugify(name) or _slugify(
            getattr(node, "layer_id", "") or ""
        ) or f"section-{variant}"
        label = _landing_nav_label(name, variant)
        items.append(
            f'      <li><a href="#sec-{_attr(slug)}" '
            f'data-nav-target="sec-{_attr(slug)}">'
            f'{html.escape(label)}</a></li>'
        )
    if not items:
        return ""
    lines = [
        '  <header class="ld-header">',
        '    <nav class="ld-nav" aria-label="Section navigation">',
        '      <ul>',
        *items,
        '      </ul>',
        '    </nav>',
    ]
    if show_reading_progress:
        lines.extend([
            '    <div class="ld-reading-progress" aria-hidden="true">',
            '      <span data-reading-progress-bar></span>',
            '    </div>',
        ])
    lines.append('  </header>')
    return "\n".join(lines)


def _landing_figure_viewer_html() -> str:
    return """  <div class="ld-figure-viewer" data-figure-viewer hidden
       role="dialog" aria-modal="true" aria-labelledby="ld-figure-viewer-title">
    <div class="ld-figure-viewer-backdrop" data-figure-viewer-close></div>
    <div class="ld-figure-viewer-panel" role="document">
      <h2 id="ld-figure-viewer-title" class="ld-visually-hidden">Source figure viewer</h2>
      <button type="button" class="ld-figure-viewer-close" data-figure-viewer-close
              aria-label="Close figure viewer">Close</button>
      <button type="button" class="ld-figure-viewer-prev" data-figure-viewer-prev
              aria-label="Previous source figure">Previous</button>
      <figure>
        <img data-figure-viewer-image src="" alt="">
        <figcaption data-figure-viewer-caption aria-live="polite"></figcaption>
      </figure>
      <button type="button" class="ld-figure-viewer-next" data-figure-viewer-next
              aria-label="Next source figure">Next</button>
    </div>
  </div>"""


def _landing_nav_label(name: str, variant: str) -> str:
    cleaned = re.sub(r"^section[_\-\s]*", "", (name or "").strip(), flags=re.IGNORECASE)
    cleaned = cleaned.replace("_", " ").replace("-", " ").strip()
    label_map = {
        "abstract": "Abstract",
        "abstract framework": "Overview",
        "overview": "Overview",
        "framework": "Method",
        "method": "Method",
        "findings": "Findings",
        "evidence": "Evidence",
        "demo": "Demos",
        "demos": "Demos",
        "results": "Results",
        "benchmark": "Benchmarks",
        "benchmarks": "Benchmarks",
        "ablation": "Ablations",
        "resources": "Resources",
        "citation": "Citation",
        "footer": "Citation",
    }
    key = cleaned.lower() or variant.lower()
    if key in label_map:
        return label_map[key]
    if variant.lower() in label_map:
        return label_map[variant.lower()]
    if cleaned:
        return cleaned.title()
    return variant.title()


def _landing_interactive_js() -> str:
    """Vanilla IIFE: reveal-on-scroll + smooth anchor scroll + active-nav.

    Self-contained. No external deps. Feature-detects
    `IntersectionObserver` and falls back to revealing everything if
    the browser is ancient. Sits AFTER the edit-toolbar script so it
    doesn't interfere with its init.
    """
    return """(() => {
  if (typeof document === 'undefined') return;
  const hasIO = 'IntersectionObserver' in window;
  const reducedMotion = window.matchMedia
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const targets = document.querySelectorAll('[data-reveal]');
  if (hasIO) {
    const io = new IntersectionObserver((entries) => {
      entries.forEach((e) => {
        if (e.isIntersecting) {
          e.target.classList.add('is-revealed');
          io.unobserve(e.target);
        }
      });
    }, { threshold: 0.15 });
    targets.forEach((el) => io.observe(el));
  } else {
    targets.forEach((el) => el.classList.add('is-revealed'));
  }

  document.addEventListener('click', (ev) => {
    const a = ev.target.closest && ev.target.closest('a[href^="#"]');
    if (!a) return;
    const id = a.getAttribute('href').slice(1);
    if (!id) return;
    const el = document.getElementById(id);
    if (!el) return;
    ev.preventDefault();
    el.scrollIntoView({ behavior: reducedMotion ? 'auto' : 'smooth', block: 'start' });
  });

  const navLinks = document.querySelectorAll('.ld-nav a[data-nav-target]');
  if (hasIO && navLinks.length) {
    const byId = new Map();
    navLinks.forEach((a) => byId.set(a.dataset.navTarget, a));
    const navIO = new IntersectionObserver((entries) => {
      entries.forEach((e) => {
        if (e.isIntersecting) {
          navLinks.forEach((a) => a.removeAttribute('aria-current'));
          const a = byId.get(e.target.id);
          if (a) a.setAttribute('aria-current', 'page');
        }
      });
    }, { threshold: 0.5 });
    document.querySelectorAll('.ld-section, footer.ld-section')
      .forEach((s) => { if (s.id) navIO.observe(s); });
  }
})();"""


def _landing_paper_project_js() -> str:
    """Deterministic paper-page interactions with no external dependencies."""
    return """(() => {
  if (typeof document === 'undefined') return;
  const root = document.querySelector(
    '.ld-landing[data-page-subtype="paper_project_page"]'
  );
  if (!root) return;

  const progressBar = document.querySelector('[data-reading-progress-bar]');
  const navLinks = Array.from(document.querySelectorAll('.ld-nav a[data-nav-target]'));
  const sections = Array.from(root.querySelectorAll('.ld-section[id]'));
  const updateReadingState = () => {
    if (progressBar) {
      const maxScroll = Math.max(
        1,
        document.documentElement.scrollHeight - window.innerHeight
      );
      const ratio = Math.max(0, Math.min(1, window.scrollY / maxScroll));
      progressBar.style.transform = 'scaleX(' + ratio + ')';
    }
    if (navLinks.length && sections.length) {
      let current = sections[0];
      sections.forEach((section) => {
        if (section.getBoundingClientRect().top <= 120) current = section;
      });
      navLinks.forEach((link) => {
        if (link.dataset.navTarget === current.id) {
          link.setAttribute('aria-current', 'page');
        } else {
          link.removeAttribute('aria-current');
        }
      });
    }
  };
  updateReadingState();
  window.addEventListener('scroll', updateReadingState, { passive: true });
  window.addEventListener('resize', updateReadingState);

  root.querySelectorAll('.ld-sort-button').forEach((button) => {
    button.addEventListener('click', () => {
      const table = button.closest('table');
      const tbody = table && table.querySelector('tbody');
      if (!table || !tbody) return;
      const column = Number(button.dataset.sortColumn);
      const kind = button.dataset.sortKind || 'text';
      const nextDirection = button.dataset.sortDirection === 'ascending'
        ? 'descending' : 'ascending';
      const multiplier = nextDirection === 'ascending' ? 1 : -1;
      const rows = Array.from(tbody.querySelectorAll('tr'));
      rows.sort((left, right) => {
        const leftText = (left.cells[column]?.textContent || '').trim();
        const rightText = (right.cells[column]?.textContent || '').trim();
        if (kind === 'numeric') {
          const leftRaw = left.cells[column]?.dataset.sortValue;
          const rightRaw = right.cells[column]?.dataset.sortValue;
          const leftValue = leftRaw === undefined ? Number.NEGATIVE_INFINITY : Number(leftRaw);
          const rightValue = rightRaw === undefined ? Number.NEGATIVE_INFINITY : Number(rightRaw);
          return (leftValue - rightValue) * multiplier;
        }
        return leftText.localeCompare(rightText, undefined, {
          numeric: true,
          sensitivity: 'base',
        }) * multiplier;
      });
      rows.forEach((row) => tbody.appendChild(row));
      table.querySelectorAll('.ld-sort-button').forEach((candidate) => {
        candidate.dataset.sortDirection = candidate === button ? nextDirection : 'none';
        const heading = candidate.closest('th');
        if (heading) {
          heading.setAttribute(
            'aria-sort',
            candidate === button ? nextDirection : 'none'
          );
        }
      });
    });
  });

  const viewer = document.querySelector('[data-figure-viewer]');
  const figures = Array.from(root.querySelectorAll('.ld-figure-focus-trigger'));
  if (!viewer || !figures.length) return;
  const viewerImage = viewer.querySelector('[data-figure-viewer-image]');
  const viewerCaption = viewer.querySelector('[data-figure-viewer-caption]');
  const previousButton = viewer.querySelector('[data-figure-viewer-prev]');
  const nextButton = viewer.querySelector('[data-figure-viewer-next]');
  const closeButton = viewer.querySelector('.ld-figure-viewer-close');
  let activeIndex = 0;
  let returnFocus = null;

  const showFigure = (index) => {
    activeIndex = (index + figures.length) % figures.length;
    const trigger = figures[activeIndex];
    const image = trigger.querySelector('img');
    const figure = trigger.closest('figure');
    if (!image || !viewerImage) return;
    viewerImage.src = image.src;
    viewerImage.alt = image.alt || 'Source figure';
    if (viewerCaption) {
      viewerCaption.textContent =
        figure?.dataset.captionShort
        || figure?.dataset.captionFull
        || image.alt
        || '';
    }
    const multiple = figures.length > 1;
    if (previousButton) previousButton.hidden = !multiple;
    if (nextButton) nextButton.hidden = !multiple;
  };
  const openViewer = (index, trigger) => {
    returnFocus = trigger;
    showFigure(index);
    viewer.hidden = false;
    document.body.classList.add('ld-figure-viewer-open');
    closeButton?.focus();
  };
  const closeViewer = () => {
    viewer.hidden = true;
    document.body.classList.remove('ld-figure-viewer-open');
    returnFocus?.focus();
  };
  figures.forEach((trigger, index) => {
    trigger.addEventListener('click', () => openViewer(index, trigger));
  });
  viewer.querySelectorAll('[data-figure-viewer-close]').forEach((control) => {
    control.addEventListener('click', closeViewer);
  });
  previousButton?.addEventListener('click', () => showFigure(activeIndex - 1));
  nextButton?.addEventListener('click', () => showFigure(activeIndex + 1));
  viewer.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      closeViewer();
    } else if (event.key === 'ArrowLeft' && figures.length > 1) {
      event.preventDefault();
      showFigure(activeIndex - 1);
    } else if (event.key === 'ArrowRight' && figures.length > 1) {
      event.preventDefault();
      showFigure(activeIndex + 1);
    } else if (event.key === 'Tab') {
      const focusable = Array.from(
        viewer.querySelectorAll('button:not([hidden])')
      );
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
  });
})();"""


# --- helpers --------------------------------------------------------------


def _inline_image(src_path: str) -> str:
    p = Path(src_path)
    with open(p, "rb") as f:
        data = f.read()
    ext = p.suffix.lower().lstrip(".")
    mime = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png", "webp": "image/webp", "gif": "image/gif",
    }.get(ext, "image/png")
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _image_src(src_path: str, *, inline_images: bool) -> str:
    if inline_images:
        return _inline_image(src_path)
    return Path(src_path).resolve().as_uri()


def _attr(s: str) -> str:
    return html.escape(str(s), quote=True)


def _font_weight_css(value: Any, family: str | None = None) -> int:
    if value is None:
        return 700 if family and "bold" in family.lower() else 400
    try:
        weight = int(float(value))
    except (TypeError, ValueError):
        return 700 if family and "bold" in family.lower() else 400
    return max(100, min(900, weight))


def _font_style_css(value: Any) -> str:
    return "italic" if value == "italic" else "normal"


def _text_transform_css(value: Any) -> str:
    return "uppercase" if value == "uppercase" else "none"


def _display_text_html(text: str, text_transform: Any) -> str:
    return str(text).upper() if _text_transform_css(text_transform) == "uppercase" else str(text)


def _float_css(value: Any, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _doc_title(ctx: ToolContext) -> str:
    spec = ctx.state.get("design_spec")
    if spec is not None:
        brief = getattr(spec, "brief", None)
        if brief:
            return brief[:80]
    return "AutoDesign output"
