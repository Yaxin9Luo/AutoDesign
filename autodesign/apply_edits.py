"""apply-edits — round-trip an edited HTML back into PSD / SVG / HTML / PNG.

Reads an edited AutoDesign HTML (typically the file downloaded from the
browser's Save button), extracts per-layer state from the `data-*` attrs
and inline images, re-renders text layers from scratch via `render_text_layer`,
recomposites, and writes a new run_dir. The returned result captures the
parent run and restored layer ids for product lineage.

Key properties:
- The edited HTML is the authoritative source — the original run_dir is not
  required. Background PNG is decoded from its `data:image/*;base64,...` URI.
- Poster image layers are decoded from their inline `data:image/*` URI and
  restored with their absolute canvas bbox.
- Text layer PNGs are re-rendered from the data-* attrs (bbox, font_size_px,
  fill, font_family, align) plus the text content inside the .layer.text div.
- Empty text layers are skipped with a warning. Layer ordering follows the
  DOM (which is authored in z_index-ascending order by html_renderer.py).
"""

from __future__ import annotations

import base64
from datetime import datetime
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, Tag

from .config import Settings, load_settings
from .schema import (
    ApplyEditsResult, ArtifactType, DesignSpec, LayerNode, TextEffect,
)
from .tools import ToolContext
from .tools.composite import composite
from .tools.render_text_layer import render_text_layer
from .util.ids import new_run_id
from .util.io import sha256_file
from .util.logging import log


# --- public entry ---------------------------------------------------------


def apply_edits(
    edited_html: Path,
    *,
    settings: Settings | None = None,
    out_dir: Path | None = None,
    run_id: str | None = None,
    cancellation_token: Any | None = None,
) -> ApplyEditsResult:
    """Apply edits from `edited_html`.

    Returns an `ApplyEditsResult` with the new run id, run dir, parent run id,
    restored layer ids, and skipped layer ids.

    Side-effects: creates `out/runs/<new_run_id>/` with poster.{psd,svg,html,
    preview.png} or landing HTML outputs.
    """
    if not edited_html.exists():
        raise FileNotFoundError(f"edited HTML not found: {edited_html}")

    settings = settings or load_settings()
    doc = BeautifulSoup(edited_html.read_text(encoding="utf-8"), "html.parser")

    parent_run_id = _meta_content(doc, "ld-run-id") or None
    title = doc.title.get_text(strip=True) if doc.title else ""

    new_id = run_id or new_run_id()
    run_dir = out_dir or settings.out_dir / "runs" / new_id
    layers_dir = run_dir / "layers"
    layers_dir.mkdir(parents=True, exist_ok=True)

    ctx = ToolContext(settings=settings, run_dir=run_dir,
                      layers_dir=layers_dir, run_id=new_id,
                      cancellation_token=cancellation_token)
    ctx.raise_if_cancelled("apply_edits.start")

    # Detect artifact mode — landing has <main class="ld-landing">,
    # poster has <div class="canvas">.
    landing_main = doc.find("main", class_="ld-landing")
    poster_canvas = doc.find("div", class_="canvas")

    skipped: list[str] = []

    if landing_main is not None:
        cw = int(landing_main.get("data-w") or 1200)
        ctx.state["design_spec"] = _stub_design_spec(title, cw, 2400,
                                                     ArtifactType.LANDING)
        layer_graph = _restore_landing(landing_main, ctx, skipped)
        # Landing doesn't populate rendered_layers; it stores the tree on
        # design_spec.layer_graph for composite to walk.
        ctx.state["design_spec"] = ctx.state["design_spec"].model_copy(
            update={"layer_graph": layer_graph}
        )
        log("apply.landing.restored",
            sections=sum(1 for n in layer_graph if n.kind == "section"),
            text_layers=sum(
                1 for s in layer_graph for c in (s.children or [])
                if c.kind == "text"
            ),
            skipped=len(skipped),
            parent=parent_run_id or "(none)")
    elif poster_canvas is not None:
        cw = int(poster_canvas.get("data-w") or 0)
        ch = int(poster_canvas.get("data-h") or 0)
        if not (cw > 0 and ch > 0):
            raise ValueError(f"canvas missing data-w/data-h in {edited_html}")
        ctx.state["design_spec"] = _stub_design_spec(title, cw, ch,
                                                     ArtifactType.POSTER)
        for layer_div in poster_canvas.select(".layer"):
            assert isinstance(layer_div, Tag)
            action = _restore_layer(layer_div, cw, ch, ctx)
            if action == "skipped":
                skipped.append(layer_div.get("data-layer-id", "?"))
        if not ctx.state["rendered_layers"]:
            raise RuntimeError(
                f"no layers recovered from {edited_html} "
                "(is the HTML an actual AutoDesign output?)"
            )
        log("apply.poster.restored",
            count=len(ctx.state["rendered_layers"]),
            skipped=len(skipped),
            parent=parent_run_id or "(none)")
    else:
        raise ValueError(
            f"neither a poster `.canvas` nor a landing `.ld-landing` container "
            f"found in {edited_html} — was this HTML produced by AutoDesign?"
        )

    ctx.raise_if_cancelled("apply_edits.before_composite")
    result = composite({}, ctx=ctx)
    ctx.raise_if_cancelled("apply_edits.after_composite")
    if result.status != "ok":
        raise RuntimeError(f"composite failed: {result.error_message or result.payload}")
    high_layout_issues = [
        issue for issue in (result.payload.get("layout_grounding_issues") or [])
        if issue.get("severity") in {"high", "blocker"}
    ]
    if poster_canvas is not None and high_layout_issues:
        examples = [
            {
                "type": issue.get("type"),
                "layer_id": issue.get("layer_id")
                or issue.get("layer_a")
                or issue.get("layer_b"),
            }
            for issue in high_layout_issues[:5]
        ]
        raise RuntimeError(
            "apply-edits produced high-severity layout grounding issues; "
            f"not publishing edited artifact. examples={examples}"
        )

    # Recover the actually-restored layer_ids for the caller to display.
    restored_ids = sorted(ctx.state["rendered_layers"].keys())

    a_type = "landing" if landing_main is not None else "poster"
    log("apply.done", run_id=new_id, parent=parent_run_id or "(none)",
        artifact_type=a_type, layers=len(restored_ids))

    return ApplyEditsResult(
        run_id=new_id,
        run_dir=str(run_dir),
        parent_run_id=parent_run_id,
        restored_layer_ids=restored_ids,
        skipped=skipped,
        artifact_type=a_type,
    )


# --- layer restoration ----------------------------------------------------


def _restore_layer(div: Tag, cw: int, ch: int, ctx: ToolContext) -> str:
    kind = div.get("data-kind")
    layer_id = div.get("data-layer-id")
    if not (kind and layer_id):
        return "skipped"

    z_index = _int_attr(div, "data-z-index", 0)
    name = div.get("data-layer-name") or layer_id

    if kind == "background":
        return _restore_image(div, layer_id, name, kind, z_index,
                              ctx, full_canvas=(cw, ch))
    if kind in ("brand_asset", "image"):
        return _restore_image(div, layer_id, name, kind, z_index,
                              ctx, full_canvas=None)
    if kind == "text":
        return _restore_text(div, layer_id, name, z_index, ctx)

    log("apply.skip", reason=f"unknown kind={kind!r}", layer_id=layer_id)
    return "skipped"


def _restore_text(div: Tag, layer_id: str, name: str, z_index: int,
                  ctx: ToolContext) -> str:
    # Strip the drag-handle span before reading text — downloaded HTML
    # already omits it, but re-running apply-edits on a non-downloaded source
    # should still work.
    for span in div.find_all(class_="ld-drag-handle"):
        span.decompose()
    text = div.get_text(strip=False).strip()
    if not text:
        log("apply.skip", reason="empty text", layer_id=layer_id)
        return "skipped"

    bbox = {
        "x": _int_attr(div, "data-bbox-x", 0),
        "y": _int_attr(div, "data-bbox-y", 0),
        "w": _int_attr(div, "data-bbox-w", 0),
        "h": _int_attr(div, "data-bbox-h", 0),
    }
    if not (bbox["w"] > 0 and bbox["h"] > 0):
        log("apply.skip", reason="zero-size bbox", layer_id=layer_id, bbox=bbox)
        return "skipped"

    font_size_px = _int_attr(div, "data-font-size-px",
                             ctx.settings.fonts and 64 or 64)
    font_family = div.get("data-font-family") or ctx.settings.default_text_font
    font_weight = _int_attr(div, "data-font-weight", 0) or None
    font_style = div.get("data-font-style") or None
    line_height = _float_attr(div, "data-line-height", 0.0) or None
    letter_spacing = _float_attr(div, "data-letter-spacing", 0.0)
    text_transform = div.get("data-text-transform") or None
    fill = div.get("data-fill") or "#000000"
    align = div.get("data-align") or "left"

    obs = render_text_layer({
        "layer_id": layer_id,
        "name": name,
        "text": text,
        "font_family": font_family,
        "font_size_px": font_size_px,
        "font_weight": font_weight,
        "font_style": font_style,
        "line_height": line_height,
        "letter_spacing": letter_spacing,
        "text_transform": text_transform,
        "fill": fill,
        "bbox": bbox,
        "align": align,
        "z_index": z_index,
    }, ctx=ctx)
    if obs.status not in ("ok", "partial"):
        log("apply.error", layer_id=layer_id, summary=obs.summary)
        return "skipped"
    return "ok"


def _restore_image(div: Tag, layer_id: str, name: str, kind: str,
                   z_index: int, ctx: ToolContext,
                   *, full_canvas: tuple[int, int] | None) -> str:
    img = div.find("img")
    if img is None or not img.get("src", "").startswith("data:image/"):
        log("apply.skip", reason="image has no data: URI", layer_id=layer_id)
        return "skipped"

    if full_canvas is not None:
        cw, ch = full_canvas
        bbox = {"x": 0, "y": 0, "w": cw, "h": ch}
    else:
        bbox = _bbox_from_attrs_or_style(div)
        if not (bbox["w"] > 0 and bbox["h"] > 0):
            log("apply.skip", reason="zero-size image bbox",
                layer_id=layer_id, bbox=bbox)
            return "skipped"

    mime, b64_data = _parse_data_uri(img["src"])
    ext_map = {"image/png": "png", "image/jpeg": "jpg",
               "image/webp": "webp", "image/gif": "gif"}
    ext = ext_map.get(mime, "png")
    if kind == "background":
        prefix = "bg"
    elif kind == "image":
        prefix = "img"
    else:
        prefix = "asset"
    out_path = ctx.layers_dir / f"{prefix}_{layer_id}.{ext}"
    try:
        out_path.write_bytes(base64.b64decode(b64_data))
    except Exception as e:
        log("apply.skip", reason="image decode failed",
            layer_id=layer_id, error=str(e))
        return "skipped"

    ctx.state["rendered_layers"][layer_id] = {
        "layer_id": layer_id,
        "name": name,
        "kind": kind,
        "z_index": z_index,
        "bbox": bbox,
        "src_path": str(out_path),
        "sha256": sha256_file(out_path),
    }
    return "ok"


# --- restored spec assembly ----------------------------------------------


def _stub_design_spec(title: str, cw: int, ch: int,
                      artifact_type: ArtifactType = ArtifactType.POSTER) -> DesignSpec:
    """Minimal spec — canvas is the only field render_text_layer touches
    (poster path). Landing path reads layer_graph directly."""
    return DesignSpec(
        brief=(title or "(restored from edited HTML)")[:500],
        artifact_type=artifact_type,
        canvas={"w_px": cw, "h_px": ch, "dpi": 96 if artifact_type == ArtifactType.LANDING else 300,
                "aspect_ratio": f"{cw}:{ch}", "color_mode": "RGB"},
        palette=[],
        typography={},
        mood=[],
        composition_notes="Restored from an edited HTML via apply-edits.",
        layer_graph=[],
    )


def _restore_landing(main_el: Tag, ctx: ToolContext,
                     skipped: list[str]) -> list[LayerNode]:
    """Walk a `<main class='ld-landing'>` tree and return a list[LayerNode]
    for design_spec.layer_graph. Each <section> becomes a kind='section'
    node with children = text / image / cta layers inside.

    v1.3 note: a footer-variant section may render as `<footer class="ld-section">`
    OUTSIDE `<main>` for accessibility. We pick it up by searching the
    main's parent for a sibling `<footer class="ld-section">` AFTER
    walking the in-main sections, so the round-trip preserves it.
    """
    out: list[LayerNode] = []
    # In-main <section> elements + the sibling <footer class="ld-section"> if any.
    nodes = list(
        main_el.find_all("section", class_="ld-section", recursive=False)
    )
    parent = main_el.parent
    if parent is not None:
        for footer in parent.find_all("footer", class_="ld-section", recursive=False):
            nodes.append(footer)
    for section in nodes:
        s_layer_id = section.get("data-layer-id") or ""
        s_name = section.get("data-layer-name") or "content"
        s_z = _int_attr(section, "data-z-index", len(out) + 1)

        children: list[LayerNode] = []
        # Text layers are usually <div class="layer text">; paper project page
        # citation/BibTeX blocks may use <pre class="layer text">.
        for div in section.find_all(["div", "pre"], class_="layer", recursive=False):
            text_node = _landing_text_from_div(div, skipped)
            if text_node is not None:
                children.append(text_node)
        # Image layers are <figure class="layer image"> — decode data: URI
        for figure in section.find_all("figure", class_="layer", recursive=False):
            image_node = _landing_image_from_figure(figure, ctx, skipped)
            if image_node is not None:
                children.append(image_node)
        # v1.3 — CTA layers are <a class="ld-cta ld-cta--*">
        for a in section.find_all("a", class_="ld-cta", recursive=False):
            cta_node = _landing_cta_from_a(a, skipped)
            if cta_node is not None:
                children.append(cta_node)

        # Sort children by their z_index so the DOM-order preservation isn't
        # lost (text and image were in different select calls).
        children.sort(key=lambda n: int(getattr(n, "z_index", 0) or 0))

        out.append(LayerNode(
            layer_id=s_layer_id or f"S{len(out) + 1}",
            name=s_name,
            kind="section",
            z_index=s_z,
            bbox=None,
            children=children,
        ))
    return out


def _landing_image_from_figure(fig: Tag, ctx: ToolContext,
                               skipped: list[str]) -> LayerNode | None:
    if fig.get("data-kind") != "image":
        return None
    img = fig.find("img")
    if img is None:
        return None
    src = img.get("src", "")
    if not src.startswith("data:image/"):
        return None
    layer_id = fig.get("data-layer-id") or f"img-{id(fig)}"
    name = fig.get("data-layer-name") or layer_id

    # Decode data: URI → write a real PNG into the new run's layers_dir so
    # composite.html_renderer can re-inline it.
    mime, b64_data = _parse_data_uri(src)
    ext = {"image/png": "png", "image/jpeg": "jpg",
           "image/webp": "webp"}.get(mime, "png")
    out_path = ctx.layers_dir / f"img_{layer_id}.{ext}"
    try:
        out_path.write_bytes(base64.b64decode(b64_data))
    except Exception as e:
        log("apply.landing.image_decode_fail", layer_id=layer_id, error=str(e))
        skipped.append(layer_id)
        return None

    return LayerNode(
        layer_id=layer_id,
        name=name,
        kind="image",
        z_index=_int_attr(fig, "data-z-index", 1),
        bbox=None,
        src_path=str(out_path),
        aspect_ratio=fig.get("data-aspect-ratio") or None,
    )


def _landing_cta_from_a(a: Tag, skipped: list[str]) -> LayerNode | None:
    """Decode a `<a class="ld-cta ld-cta--*">` back into a CTA LayerNode.

    Reads `text` from the element's innerText and `href / variant` from
    the authoritative `data-*` attributes that the renderer wrote (so
    a toolbar click that mutates `href` on the anchor without touching
    `data-href` still round-trips cleanly — the data-attr wins).
    """
    if a.get("data-kind") != "cta":
        return None
    layer_id = a.get("data-layer-id") or ""
    name = a.get("data-layer-name") or layer_id or "cta"
    text = a.get_text(strip=True)
    if not text:
        skipped.append(layer_id or "?")
        return None
    href = a.get("data-href") or a.get("href") or "#"
    variant_raw = (a.get("data-variant") or "primary").lower()
    variant = variant_raw if variant_raw in (
        "primary", "secondary", "ghost"
    ) else "primary"
    return LayerNode(
        layer_id=layer_id or f"cta-{id(a)}",
        name=name,
        kind="cta",
        z_index=_int_attr(a, "data-z-index", 1),
        bbox=None,
        text=text,
        href=href,
        variant=variant,  # type: ignore[arg-type]
    )


def _landing_text_from_div(div: Tag, skipped: list[str]) -> LayerNode | None:
    kind = div.get("data-kind")
    if kind != "text":
        return None
    layer_id = div.get("data-layer-id") or ""
    # Strip the drag-handle span defensively (shouldn't be there for landing,
    # but round-tripped HTMLs might).
    for span in div.find_all(class_="ld-drag-handle"):
        span.decompose()
    text = div.get_text(strip=False).strip()
    if not text:
        skipped.append(layer_id or "?")
        return None
    fill_raw = div.get("data-fill") or ""
    return LayerNode(
        layer_id=layer_id or f"L-{id(div)}",
        name=div.get("data-layer-name") or layer_id,
        kind="text",
        z_index=_int_attr(div, "data-z-index", 1),
        bbox=None,
        text=text,
        font_family=div.get("data-font-family") or None,
        font_size_px=_int_attr(div, "data-font-size-px", 40) or None,
        font_weight=_int_attr(div, "data-font-weight", 0) or None,
        font_style=div.get("data-font-style") or None,
        line_height=_float_attr(div, "data-line-height", 0.0) or None,
        letter_spacing=_float_attr(div, "data-letter-spacing", 0.0),
        text_transform=div.get("data-text-transform") or None,
        align=div.get("data-align") or None,
        effects=TextEffect(fill=fill_raw) if fill_raw else None,
    )


# --- helpers --------------------------------------------------------------


def _meta_content(doc: BeautifulSoup, name: str) -> str:
    tag = doc.find("meta", attrs={"name": name})
    if tag is None:
        return ""
    return tag.get("content", "") or ""


def _parse_data_uri(uri: str) -> tuple[str, str]:
    """Returns (mime, base64_payload)."""
    header, _, data = uri.partition(",")
    header = header.removeprefix("data:")
    mime = header.split(";", 1)[0]
    return mime, data


def _int_attr(tag: Tag, name: str, default: int) -> int:
    raw = tag.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _float_attr(tag: Tag, name: str, default: float) -> float:
    raw = tag.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _bbox_from_attrs_or_style(tag: Tag) -> dict[str, int]:
    """Read bbox from data attrs first, then poster image inline CSS.

    Poster text layers carry `data-bbox-*`, while poster image layers are
    emitted by html_renderer with `left/top/width/height` in inline style.
    """
    bbox = {
        "x": _int_attr(tag, "data-bbox-x", 0),
        "y": _int_attr(tag, "data-bbox-y", 0),
        "w": _int_attr(tag, "data-bbox-w", 0),
        "h": _int_attr(tag, "data-bbox-h", 0),
    }
    if bbox["w"] > 0 and bbox["h"] > 0:
        return bbox
    return _bbox_from_inline_style(tag)


def _bbox_from_inline_style(tag: Tag) -> dict[str, int]:
    props: dict[str, str] = {}
    raw_style = tag.get("style") or ""
    for part in str(raw_style).split(";"):
        key, sep, value = part.partition(":")
        if not sep:
            continue
        props[key.strip().lower()] = value.strip()

    def px(name: str) -> int:
        raw = props.get(name, "")
        if raw.endswith("px"):
            raw = raw[:-2]
        try:
            return int(round(float(raw)))
        except (TypeError, ValueError):
            return 0

    return {
        "x": px("left"),
        "y": px("top"),
        "w": px("width"),
        "h": px("height"),
    }
