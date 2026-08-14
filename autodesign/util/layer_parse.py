"""Read-only parser that turns a rendered poster/landing HTML back into a
frontend-shaped layer list.

NOTE on bbox: text layers carry data-bbox-{x,y,w,h} attrs (round-trip
contract with apply_edits). Poster image layers don't — they encode the
bbox in inline `style` (`left/top/width/height` in px). Landing image
layers use `data-bbox-tx/ty/w/h` with a `t` prefix. We handle all three
shapes so the editor's "click an image, see its size" path works.

Distinct from `autodesign.apply_edits` on purpose:

- `apply_edits` reads HTML AND re-renders each layer (writes binaries to
  disk, mutates ctx.state, ends with a fresh run_dir). It is the agent's
  edit round-trip.
- This module is a **pure** dom→dict transform. No disk I/O, no ctx, no
  side-effects. Used by the FastAPI shim to populate `Artifact.layers`
  for the web UI's Sidebar so it can show real font / bbox / fill values.

The dict shape mirrors the TypeScript `Layer` interface in
`web/src/lib/types.ts:37` so the FastAPI response is a JSON pass-through —
no translation layer needed in JS.

The data-* attribute schema is defined by `tools/html_renderer.py` (see
the comment block around line 266 of that file). When a new attribute is
added there, mirror it here.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, Tag

# Layer kinds we expose to the frontend. Anything else (`brand_asset`,
# `table`, ...) is collapsed into "image" since the editor's right-rail
# only knows how to drive text/image/shape/background fields anyway.
_FRONTEND_KIND: dict[str, str] = {
    "background": "background",
    "text": "text",
    "image": "image",
    "brand_asset": "image",
    "table": "image",
    "callout": "shape",
    "cta": "text",
    "metric": "text",
    "quote": "text",
    "caption": "text",
    "shape": "shape",
}

_DECK_MARGIN = 40
_DECK_GAP = 80
_FLOW_TEXT_TAGS = {
    "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "li", "figcaption", "blockquote", "td", "th",
}
_FLOW_TEXT_SELECTORS = ",".join([
    *sorted(_FLOW_TEXT_TAGS),
    ".identity-badge",
    ".comparison-item",
    ".formula",
    ".footer-note",
    ".lead",
    ".mechanism-side-callout",
    ".metric",
    ".muted",
    ".native-row",
    ".readout",
    ".stage",
])
_FLOW_TEXT_SCOPED_SELECTOR = ",".join(
    f".paper-poster {selector.strip()}"
    for selector in _FLOW_TEXT_SELECTORS.split(",")
)


def parse_html_layers(html_path: Path) -> list[dict[str, Any]]:
    """Walk the poster/landing HTML and emit a list of layer dicts.

    Returns an empty list if the file doesn't exist or has no `.layer`
    elements. Never raises on malformed input — the FastAPI shim falls
    back to `layers: []` and the user just sees the iframe-only view.
    """
    if not html_path.exists():
        return []
    try:
        doc = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
    except Exception:  # noqa: BLE001 — on parse failure we return []
        return []

    layers: list[dict[str, Any]] = []
    seen: set[int] = set()
    candidates = doc.select(
        '.layer, .od-layer, [data-autodesign-editable="true"][data-layer-id]'
    )
    for div in candidates:
        marker = id(div)
        if marker in seen:
            continue
        seen.add(marker)
        layer = _layer_from_div(div)
        if layer is not None:
            layers.append(layer)
    if not layers and doc.select_one(".paper-poster"):
        layers.extend(_paper_poster_flow_layers(doc))
    return layers


def _paper_poster_flow_layers(doc: BeautifulSoup) -> list[dict[str, Any]]:
    """Expose authored paper-poster flow text blocks as editable layers.

    External designer-author posters are normal-flow HTML, not absolute
    ``.layer`` boxes. We expose a lightweight editable layer map for
    sections, text blocks, and images without changing the authored flow.
    """
    out: list[dict[str, Any]] = []
    for idx, node in enumerate(doc.select(
        ".paper-poster .poster-header[data-block-id], "
        ".paper-poster .poster-section[data-block-id]",
    ), start=1):
        if not isinstance(node, Tag):
            continue
        block_id = str(node.get("data-block-id") or f"section_{idx}").strip()
        layer_id = str(node.get("data-layer-id") or f"flow_section_{_slug_id(block_id)}_{idx}").strip()
        title = _poster_section_title(node) or block_id
        out.append({
            "layer_id": layer_id,
            "name": title,
            "kind": "section",
            "z_index": len(out) + 1,
            "visible": True,
        })

    text_idx = 0
    for node in doc.select(_FLOW_TEXT_SCOPED_SELECTOR):
        if not isinstance(node, Tag):
            continue
        text = node.get_text(" ", strip=True)
        if not text:
            continue
        text_idx += 1
        nearest = _nearest_block_id(node) or f"text_{text_idx}"
        block_id = str(node.get("data-layer-id") or node.get("data-block-id") or "").strip()
        layer_id = block_id or f"flow_text_{_slug_id(nearest)}_{text_idx}"
        style = _style_map(_combined_style(node))
        align = style.get("text-align")
        out.append({
            "layer_id": layer_id,
            "name": node.get("data-layer-name") or _text_layer_name(text, layer_id),
            "kind": "text",
            "z_index": len(out) + 1,
            "text": text,
            "font_size_px": _int_attr(node, "data-font-size-px", 0)
            or _int_from_style(style, "font-size", 0)
            or None,
            "font_family": node.get("data-font-family") or _clean_font_family(style.get("font-family") or ""),
            "font_weight": _int_attr(node, "data-font-weight", 0)
            or _int_from_style(style, "font-weight", 0)
            or _font_weight(str(style.get("font-family") or ""), node),
            "font_style": node.get("data-font-style") or style.get("font-style") or "normal",
            "line_height": (
                _float_attr(node, "data-line-height")
                if node.get("data-line-height") is not None
                else _float_from_style(style, "line-height")
            ),
            "letter_spacing": (
                _float_attr(node, "data-letter-spacing")
                if node.get("data-letter-spacing") is not None
                else _float_from_style(style, "letter-spacing", 0.0)
            ),
            "text_transform": node.get("data-text-transform") or style.get("text-transform") or "none",
            "align": align if align in {"left", "center", "right"} else None,
            "effects": {"fill": node.get("data-fill") or style.get("color") or None},
            "visible": True,
        })

    image_idx = 0
    for node in doc.select(".paper-poster img"):
        if not isinstance(node, Tag):
            continue
        image_idx += 1
        nearest = (
            str(node.get("data-source-id") or "").strip()
            or str(node.get("alt") or "").strip()
            or _nearest_block_id(node)
            or f"image_{image_idx}"
        )
        layer_id = str(node.get("data-layer-id") or f"flow_image_{_slug_id(nearest)}_{image_idx}").strip()
        src = str(node.get("src") or "")
        out.append({
            "layer_id": layer_id,
            "name": node.get("data-layer-name") or node.get("alt") or layer_id,
            "kind": "image",
            "z_index": len(out) + 1,
            "src": src,
            "visible": True,
        })
    return out


def _nearest_block_id(node: Tag) -> str | None:
    cur: Any = node
    while isinstance(cur, Tag):
        raw = cur.get("data-block-id") or cur.get("data-column-id")
        if raw:
            return str(raw)
        cur = cur.parent
    return None


def _slug_id(raw: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(raw).lower()).strip("_")[:72]
    return slug or "item"


def _poster_section_title(node: Tag) -> str | None:
    heading = node.find(["h1", "h2", "h3", "h4", "h5", "h6"])
    if isinstance(heading, Tag):
        text = heading.get_text(" ", strip=True)
        if text:
            return text[:96]
    role = str(node.get("data-panel-role") or "").replace("_", " ").strip()
    return role[:96] or None


def _text_layer_name(text: str, fallback: str) -> str:
    clean = " ".join(text.split())
    if not clean:
        return fallback
    return clean[:72] + ("…" if len(clean) > 72 else "")


def parse_deck_html_as_layer_mode(
    html_path: Path,
    *,
    margin: int = _DECK_MARGIN,
    gap: int = _DECK_GAP,
) -> dict[str, Any] | None:
    """Convert generated ``deck.html`` into the frontend's layer-mode stack.

    The production deck renderer emits native PPTX plus an HTML preview whose
    DOM uses ``.od-deck`` / ``.deck-slide`` / ``.od-layer``. The web canvas
    editor, however, operates on a single tall layer stack like the editable
    slide demo. This adapter preserves the renderer's semantic layer ids while
    adding one locked frame layer per slide so the existing slide navigation and
    scene navigation heuristics work without a separate deck editor.
    """
    if not html_path.exists():
        return None
    try:
        doc = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
    except Exception:  # noqa: BLE001
        return None

    main = doc.find("main", class_="od-deck")
    if not isinstance(main, Tag):
        main = doc.find("main", class_="od-artifact")
    if not isinstance(main, Tag):
        return None

    w = _int_attr(main, "data-w", 1920) or 1920
    h = _int_attr(main, "data-h", 1080) or 1080
    slides = [
        s for s in main.find_all("section", class_="deck-slide", recursive=False)
        if isinstance(s, Tag)
    ]
    if not slides:
        slides = [
            s for s in main.find_all(["section", "div"], class_="od-frame", recursive=False)
            if isinstance(s, Tag) and str(s.get("data-frame-kind") or "") == "slide"
        ]
    if not slides:
        return None

    layers: list[dict[str, Any]] = [{
        "layer_id": "deck_canvas_bg",
        "name": "Canvas background",
        "kind": "background",
        "z_index": 0,
        "bbox": {
            "x": 0,
            "y": 0,
            "w": w + margin * 2,
            "h": h * len(slides) + gap * (len(slides) - 1) + margin * 2,
        },
        "fill_color": "#ebe7dc",
        "visible": True,
        "locked": True,
    }]
    frames: list[dict[str, Any]] = []
    native_layer_count = 0

    for idx, slide in enumerate(slides):
        slide_id = str(slide.get("data-layer-id") or f"slide_{idx + 1:02d}")
        y0 = margin + idx * (h + gap)
        frame_id = slide_id
        frame = {
            "idx": idx,
            "layer_id": frame_id,
            "bbox": {"x": margin, "y": y0, "w": w, "h": h},
        }
        frames.append(frame)
        layers.append({
            "layer_id": frame_id,
            "name": f"Slide {idx + 1}",
            "kind": "shape",
            "shape_kind": "rect",
            "z_index": idx * 1000 + 1,
            "bbox": frame["bbox"],
            "fill_color": _slide_fill(slide),
            "visible": True,
            "locked": True,
        })

        for child in slide.find_all(class_="od-layer", recursive=False):
            if not isinstance(child, Tag):
                continue
            layer = _deck_layer_from_div(child, idx=idx, offset_x=margin, offset_y=y0)
            if layer is not None:
                layers.append(layer)
                native_layer_count += 1

    if native_layer_count == 0:
        return None

    return {
        "canvas": {
            "w": w + margin * 2,
            "h": h * len(slides) + gap * (len(slides) - 1) + margin * 2,
            "background": "#ebe7dc",
        },
        "layers": layers,
        "frames": frames,
        "slide_count": len(slides),
        "slide_size": {"w": w, "h": h},
    }


def _layer_from_div(div: Tag) -> dict[str, Any] | None:
    layer_id = div.get("data-layer-id")
    raw_kind = div.get("data-kind")
    if not layer_id or not raw_kind:
        return None
    kind = _FRONTEND_KIND.get(raw_kind, "image")

    base: dict[str, Any] = {
        "layer_id": layer_id,
        "name": div.get("data-layer-name") or layer_id,
        "kind": kind,
        "z_index": _int_attr(div, "data-z-index", 0),
    }

    bbox = _bbox_from(div)
    if bbox is not None:
        base["bbox"] = bbox

    if kind == "text":
        # Text content lives in the div's text (drag-handle span is
        # stripped out below). Fonts/colors come from data-* attrs that
        # html_renderer.py keeps in lockstep with the inline style.
        for span in div.find_all(class_="ld-drag-handle"):
            span.decompose()
        text = div.get_text(strip=False).strip()
        style = _style_map(_combined_style(div))
        font_weight = (
            _int_attr(div, "data-font-weight", 0)
            or _int_from_style(style, "font-weight", 0)
            or _font_weight(str(div.get("data-font-family") or ""), div)
        )
        line_height = (
            _float_attr(div, "data-line-height")
            if div.get("data-line-height") is not None
            else _float_from_style(style, "line-height")
        )
        letter_spacing = (
            _float_attr(div, "data-letter-spacing")
            if div.get("data-letter-spacing") is not None
            else _float_from_style(style, "letter-spacing", 0.0)
        )
        base.update({
            "text": text,
            "font_size_px": _int_attr(div, "data-font-size-px", 0) or None,
            "font_family": div.get("data-font-family") or None,
            "font_weight": font_weight,
            "font_style": div.get("data-font-style") or style.get("font-style") or "normal",
            "line_height": line_height,
            "letter_spacing": letter_spacing,
            "text_transform": div.get("data-text-transform") or style.get("text-transform") or "none",
            "align": div.get("data-align") or None,
            "effects": {"fill": div.get("data-fill") or None},
        })

    return base


def _deck_layer_from_div(
    div: Tag,
    *,
    idx: int,
    offset_x: int,
    offset_y: int,
) -> dict[str, Any] | None:
    layer_id = div.get("data-layer-id")
    raw_kind = str(div.get("data-kind") or "")
    if not layer_id or not raw_kind:
        return None

    kind = _FRONTEND_KIND.get(raw_kind, "image")
    style = _style_map(_combined_style(div))
    bbox = _bbox_from_inline_style_map(style)
    if bbox is None:
        bbox = _bbox_from(div)
    if bbox is None:
        return None
    bbox = {
        "x": int(bbox["x"] + offset_x),
        "y": int(bbox["y"] + offset_y),
        "w": int(bbox["w"]),
        "h": int(bbox["h"]),
    }
    z_index = idx * 1000 + _int_from_style(style, "z-index", 10) + 10
    base: dict[str, Any] = {
        "layer_id": str(layer_id),
        "name": div.get("data-layer-name") or str(layer_id),
        "kind": kind,
        "z_index": z_index,
        "bbox": bbox,
        "visible": True,
    }

    if kind == "text":
        text = div.get_text(strip=False).strip()
        if not text:
            return None
        family = (
            div.get("data-font-family")
            or style.get("font-family")
            or "Inter"
        )
        font_size = (
            _int_attr(div, "data-font-size-px", 0)
            or _int_from_style(style, "font-size", 36)
        )
        fill = div.get("data-fill") or style.get("color") or "#0F172A"
        align = div.get("data-align") or style.get("text-align") or "left"
        line_height = (
            _float_attr(div, "data-line-height")
            if div.get("data-line-height") is not None
            else _float_from_style(style, "line-height", 1.12)
        )
        letter_spacing = (
            _float_attr(div, "data-letter-spacing")
            if div.get("data-letter-spacing") is not None
            else _float_from_style(style, "letter-spacing", 0.0)
        )
        base.update({
            "text": text,
            "font_family": _clean_font_family(str(family)),
            "font_size_px": font_size,
            "font_weight": (
                _int_attr(div, "data-font-weight", 0)
                or _int_from_style(style, "font-weight", 0)
                or _font_weight(str(family), div)
            ),
            "font_style": div.get("data-font-style") or style.get("font-style") or "normal",
            "line_height": line_height,
            "letter_spacing": letter_spacing,
            "text_transform": div.get("data-text-transform") or style.get("text-transform") or "none",
            "align": align if align in {"left", "center", "right"} else "left",
            "effects": {"fill": fill},
        })
        return base

    img = div.find("img")
    if kind == "image" and isinstance(img, Tag) and img.get("src"):
        base.update({
            "src": str(img.get("src")),
            "fit": "contain",
            "object_position": {"x": 0.5, "y": 0.5},
            "corner_radius": 0,
        })
        return base

    base.update({
        "kind": "shape",
        "shape_kind": "rect",
        "fill_color": "rgba(139, 94, 60, 0.10)" if raw_kind == "callout" else "transparent",
        "stroke_color": "rgba(139, 94, 60, 0.40)" if raw_kind == "callout" else "#d8d1bf",
        "stroke_width": 1 if raw_kind == "callout" else 0,
    })
    return base


def _bbox_from(div: Tag) -> dict[str, int] | None:
    """Try data-bbox-* first, then inline style left/top/width/height,
    then return None (landing flow layers without geometry)."""
    bbox = _bbox_from_data_attrs(div)
    if bbox is not None:
        return bbox
    return _bbox_from_inline_style(div)


def _bbox_from_data_attrs(div: Tag) -> dict[str, int] | None:
    # Text layers — `data-bbox-{x,y,w,h}` (no prefix).
    if div.get("data-bbox-w") is not None and div.get("data-bbox-h") is not None:
        w = _int_attr(div, "data-bbox-w", 0)
        h = _int_attr(div, "data-bbox-h", 0)
        if w > 0 and h > 0:
            return {
                "x": _int_attr(div, "data-bbox-x", 0),
                "y": _int_attr(div, "data-bbox-y", 0),
                "w": w,
                "h": h,
            }
    # Landing image layers — `data-bbox-{tx,ty,w,h}` with the `t` prefix.
    # When tx/ty are present but w/h are blank strings we treat the
    # bbox as flow-layout (no fixed size).
    if div.get("data-bbox-tx") is not None:
        w = _int_attr(div, "data-bbox-w", 0)
        h = _int_attr(div, "data-bbox-h", 0)
        if w > 0 and h > 0:
            return {
                "x": _int_attr(div, "data-bbox-tx", 0),
                "y": _int_attr(div, "data-bbox-ty", 0),
                "w": w,
                "h": h,
            }
    return None


# Match `left:120px; top:240px; width:800px; height:80px;` etc. in inline
# style. We intentionally don't run a full CSS parser — the html_renderer
# emits the four properties in a known order and `int(float(...))` is
# tolerant of stray whitespace.
def _bbox_from_inline_style(div: Tag) -> dict[str, int] | None:
    style = _combined_style(div)
    if not style:
        return None
    return _bbox_from_inline_style_map(_style_map(style))


def _bbox_from_inline_style_map(style: dict[str, str]) -> dict[str, int] | None:
    parts: dict[str, int] = {}
    for key in ("left", "top", "width", "height"):
        raw = style.get(key)
        if raw is None:
            continue
        v = raw.strip().lower().rstrip("px").strip()
        try:
            parts[key] = int(float(v))
        except (TypeError, ValueError):
            continue
    if "width" in parts and "height" in parts and parts["width"] > 0 and parts["height"] > 0:
        return {
            "x": parts.get("left", 0),
            "y": parts.get("top", 0),
            "w": parts["width"],
            "h": parts["height"],
        }
    return None


def _combined_style(div: Tag) -> str:
    """Return style text, including deck attrs broken by unescaped quotes.

    Older deck HTML wrote ``font-family:"NotoSansSC-Bold"`` inside a double
    quoted style attribute. Browsers recover, but BeautifulSoup splits the tail
    into a bogus attribute name. Reattaching those fragments lets us recover
    font-size/color for the layer-mode adapter.
    """
    bits = [str(div.get("style") or "")]
    for key in div.attrs:
        if not isinstance(key, str):
            continue
        lower = key.lower()
        if (
            "font-size:" in lower
            or "text-align:" in lower
            or "color:" in lower
            or "font-weight:" in lower
            or "font-style:" in lower
            or "line-height:" in lower
            or "letter-spacing:" in lower
            or "text-transform:" in lower
        ):
            repaired = lower.replace('"', "").replace("'", "")
            if bits[0].rstrip().endswith("font-family:"):
                family = repaired.split(";", 1)[0].strip()
                repaired = f"{family};" + repaired.split(";", 1)[1] if ";" in repaired else family
            bits.append(repaired)
    return ";".join(b for b in bits if b)


def _style_map(style: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for chunk in str(style).split(";"):
        if ":" not in chunk:
            continue
        key, val = chunk.split(":", 1)
        key = key.strip().lower()
        val = val.strip()
        if key:
            out[key] = val
    return out


def _int_from_style(style: dict[str, str], key: str, default: int) -> int:
    raw = style.get(key)
    if raw is None:
        return default
    try:
        return int(float(raw.lower().rstrip("px").strip()))
    except (TypeError, ValueError):
        return default


def _float_from_style(
    style: dict[str, str],
    key: str,
    default: float | None = None,
) -> float | None:
    raw = style.get(key)
    if raw is None:
        return default
    try:
        return float(raw.lower().rstrip("px").strip())
    except (TypeError, ValueError):
        return default


def _clean_font_family(raw: str) -> str:
    first = raw.split(",", 1)[0].strip().strip('"').strip("'")
    if not first or first == ":":
        return "Inter"
    return first


def _font_weight(family: str, div: Tag) -> int:
    classes = set(str(c) for c in (div.get("class") or []))
    if "od-section-label" in classes:
        return 700
    fam = family.lower()
    return 700 if "bold" in fam or "heavy" in fam else 500


def _slide_fill(slide: Tag) -> str:
    role = str(slide.get("data-role") or "")
    if role in {"cover", "section_divider", "closing"}:
        return "#f6efe3"
    return "#fbf8ef"


def _int_attr(div: Tag, name: str, default: int) -> int:
    raw = div.get(name)
    if raw is None:
        return default
    try:
        return int(float(str(raw)))
    except (TypeError, ValueError):
        return default


def _float_attr(div: Tag, name: str, default: float | None = None) -> float | None:
    raw = div.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(str(raw))
    except (TypeError, ValueError):
        return default
