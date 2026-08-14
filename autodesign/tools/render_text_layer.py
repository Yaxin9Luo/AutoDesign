"""render_text_layer — Pillow text → transparent RGBA PNG sized to full canvas.

Supports stroke and drop-shadow effects. Position uses top-left origin pixel
coords; alignment within bbox is honoured (left/center/right).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from ._contract import ToolContext, obs_error, obs_ok
from ..schema import ToolResultRecord
from ..util.io import sha256_file
from ..util.logging import log


def _resolve_font(font_family: str | None, ctx: ToolContext) -> tuple[Path | None, str, bool]:
    """Return an optional local font path and the resolved family."""
    fonts = ctx.settings.fonts
    if font_family and font_family in fonts:
        path = ctx.settings.fonts_dir / fonts[font_family]
        return (path if path.exists() else None), font_family, not path.exists()
    fallback = ctx.settings.default_text_font
    path = ctx.settings.fonts_dir / fonts[fallback]
    return (path if path.exists() else None), fallback, True


def _load_font(font_path: Path | None, font_size: int) -> ImageFont.FreeTypeFont:
    if font_path is not None:
        return ImageFont.truetype(str(font_path), size=font_size)
    return ImageFont.load_default(size=font_size)


def _coerce_font_weight(value: Any) -> int | None:
    if value is None:
        return None
    try:
        weight = int(float(value))
    except (TypeError, ValueError):
        return None
    return max(100, min(900, weight))


def _coerce_line_height(value: Any) -> float:
    if value is None:
        return 1.2
    try:
        line_height = float(value)
    except (TypeError, ValueError):
        return 1.2
    return max(0.8, min(2.5, line_height))


def _coerce_letter_spacing(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _coerce_font_style(value: Any) -> str:
    return "italic" if value == "italic" else "normal"


def _coerce_text_transform(value: Any) -> str:
    return "uppercase" if value == "uppercase" else "none"


def _layer_field(layer: Any, field: str) -> Any:
    if layer is None:
        return None
    if isinstance(layer, dict):
        return layer.get(field)
    return getattr(layer, field, None)


def _find_spec_layer(layers: Any, layer_id: str) -> Any:
    if not layers:
        return None
    for layer in layers:
        if _layer_field(layer, "layer_id") == layer_id:
            return layer
        found = _find_spec_layer(_layer_field(layer, "children"), layer_id)
        if found is not None:
            return found
    return None


def _style_arg(args: dict[str, Any], spec_layer: Any, field: str) -> tuple[Any, bool]:
    value = args.get(field)
    if value is not None:
        return value, False
    fallback = _layer_field(spec_layer, field)
    if fallback is not None:
        return fallback, True
    return None, False


def _display_text(text: str, text_transform: str) -> str:
    return text.upper() if text_transform == "uppercase" else text


def _apply_font_weight(font: ImageFont.FreeTypeFont, weight: int | None) -> None:
    """Apply a variable-font weight axis when Pillow exposes it.

    Static bundled fonts simply ignore this; HTML/SVG remain the primary
    high-fidelity text surfaces.
    """
    if weight is None:
        return
    try:
        axes = font.get_variation_axes()
    except Exception:
        return
    if not axes:
        return
    values: list[float] = []
    changed = False
    for axis in axes:
        name_raw = axis.get("name", b"")
        name = (
            name_raw.decode("utf-8", errors="ignore")
            if isinstance(name_raw, bytes)
            else str(name_raw)
        ).lower()
        default = float(axis.get("default", weight))
        if "weight" in name or "wght" in name:
            mn = float(axis.get("minimum", 100))
            mx = float(axis.get("maximum", 900))
            values.append(max(mn, min(mx, float(weight))))
            changed = True
        else:
            values.append(default)
    if not changed:
        return
    try:
        font.set_variation_by_axes(values)
    except Exception:
        return


def _text_width(
    text: str,
    font: ImageFont.FreeTypeFont,
    letter_spacing: float = 0.0,
) -> float:
    if not text:
        return 0.0
    bbox = font.getbbox(text)
    return max(0.0, float(bbox[2] - bbox[0]) + letter_spacing * max(0, len(text) - 1))


def _draw_spaced_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    *,
    font: ImageFont.FreeTypeFont,
    fill: str,
    letter_spacing: float,
    stroke_width: int = 0,
    stroke_fill: str = "#000000",
) -> None:
    if not text:
        return
    if abs(letter_spacing) < 0.01:
        kw: dict[str, Any] = {"font": font, "fill": fill}
        if stroke_width > 0:
            kw["stroke_width"] = stroke_width
            kw["stroke_fill"] = stroke_fill
        draw.text(xy, text, **kw)
        return
    x, y = xy
    for ch in text:
        kw = {"font": font, "fill": fill}
        if stroke_width > 0:
            kw["stroke_width"] = stroke_width
            kw["stroke_fill"] = stroke_fill
        draw.text((x, y), ch, **kw)
        x += _text_width(ch, font, 0.0) + letter_spacing


def _wrap_lines(
    text: str,
    font: ImageFont.FreeTypeFont,
    max_w: int,
    letter_spacing: float = 0.0,
) -> list[str]:
    """Greedy wrap. Splits Latin on spaces; CJK char-by-char."""
    if not text:
        return [""]
    tokens: list[str] = []
    buf = ""
    for ch in text:
        if ch.isspace():
            if buf:
                tokens.append(buf)
                buf = ""
            tokens.append(ch)
        elif ord(ch) > 0x2E80:  # CJK range start (rough)
            if buf:
                tokens.append(buf)
                buf = ""
            tokens.append(ch)
        else:
            buf += ch
    if buf:
        tokens.append(buf)

    lines: list[str] = []
    cur = ""
    for tok in tokens:
        candidate = cur + tok
        w = _text_width(candidate, font, letter_spacing)
        if w <= max_w or not cur:
            cur = candidate
        else:
            lines.append(cur.rstrip())
            cur = tok if not tok.isspace() else ""
    if cur:
        lines.append(cur.rstrip())
    return lines or [""]


def measure_text_height(
    text: str,
    font_family: str | None,
    font_size: int,
    bbox_width: int,
    ctx: ToolContext,
    *,
    line_height: float | None = None,
    letter_spacing: float | None = None,
    text_transform: str | None = None,
    font_weight: int | None = None,
) -> int:
    """Return the real wrapped text height used by ``render_text_png``.

    Composite-time layout repair uses this to reason about actual multi-line
    poster text, not just the planner's declared bbox height.
    """
    font_path, _resolved_family, _was_fallback = _resolve_font(font_family, ctx)
    font = _load_font(font_path, font_size)
    weight = _coerce_font_weight(font_weight)
    _apply_font_weight(font, weight)
    spacing = _coerce_letter_spacing(letter_spacing)
    transform = _coerce_text_transform(text_transform)
    lines = _wrap_lines(
        _display_text(text, transform),
        font,
        max(1, int(bbox_width)),
        spacing,
    )
    return _text_block_height(lines, font, font_size, _coerce_line_height(line_height))


def render_text_png(
    *,
    text: str,
    font_family: str | None,
    font_size: int,
    fill: str,
    bbox: dict[str, Any],
    align: str,
    effects: dict[str, Any],
    font_weight: int | None = None,
    font_style: str | None = None,
    line_height: float | None = None,
    letter_spacing: float | None = None,
    text_transform: str | None = None,
    canvas_w: int,
    canvas_h: int,
    out_path: Path,
    ctx: ToolContext,
) -> tuple[str, bool]:
    """Render one text layer to a full-canvas transparent PNG.

    Returns ``(resolved_family, was_fallback)``. This is the shared drawing
    primitive for the public tool and for composite-time layout repairs.
    """
    font_path, resolved_family, was_fallback = _resolve_font(font_family, ctx)
    font = _load_font(font_path, font_size)
    weight = _coerce_font_weight(font_weight)
    _apply_font_weight(font, weight)
    line_height_value = _coerce_line_height(line_height)
    spacing = _coerce_letter_spacing(letter_spacing)
    transform = _coerce_text_transform(text_transform)
    display_text = _display_text(text, transform)

    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    bx, by, bw, bh = int(bbox["x"]), int(bbox["y"]), int(bbox["w"]), int(bbox["h"])
    lines = _wrap_lines(display_text, font, bw, spacing)

    line_metrics = [font.getbbox(line) for line in lines]
    line_heights = [m[3] - m[1] for m in line_metrics]
    line_step = _line_step(line_heights, font_size, line_height_value)
    total_h = _text_block_height(lines, font, font_size, line_height_value)
    cy = by + max(0, (bh - total_h) // 2)

    shadow = effects.get("shadow")
    if shadow:
        sh_color = shadow.get("color", "#000000A0")
        sh_dx, sh_dy = int(shadow.get("dx", 0)), int(shadow.get("dy", 4))
        sh_blur = int(shadow.get("blur", 12))
        shadow_img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        sh_draw = ImageDraw.Draw(shadow_img)
        _y = cy
        for line, m, lh in zip(lines, line_metrics, line_heights):
            line_w = _text_width(line, font, spacing)
            x = _line_x(align, bx, bw, line_w)
            _draw_spaced_text(
                sh_draw,
                (x + sh_dx, _y + sh_dy),
                line,
                font=font,
                fill=sh_color,
                letter_spacing=spacing,
            )
            _y += line_step
        if sh_blur > 0:
            shadow_img = shadow_img.filter(ImageFilter.GaussianBlur(radius=sh_blur))
        img = Image.alpha_composite(img, shadow_img)
        draw = ImageDraw.Draw(img)

    stroke = effects.get("stroke") or {}
    stroke_width = int(stroke.get("width", 0))
    stroke_fill = stroke.get("color", "#000000")

    _y = cy
    for line, m, lh in zip(lines, line_metrics, line_heights):
        line_w = _text_width(line, font, spacing)
        x = _line_x(align, bx, bw, line_w)
        _draw_spaced_text(
            draw,
            (x, _y),
            line,
            font=font,
            fill=fill,
            letter_spacing=spacing,
            stroke_width=stroke_width,
            stroke_fill=stroke_fill,
        )
        _y += line_step

    img.save(out_path, format="PNG", optimize=True)
    return resolved_family, was_fallback


def _text_block_height(
    lines: list[str],
    font: ImageFont.FreeTypeFont,
    font_size: int,
    line_height: float = 1.2,
) -> int:
    line_metrics = [font.getbbox(line) for line in lines]
    line_heights = [m[3] - m[1] for m in line_metrics]
    if not line_heights:
        return 0
    return int(line_heights[0] + _line_step(line_heights, font_size, line_height) * max(0, len(lines) - 1))


def _line_step(line_heights: list[int], font_size: int, line_height: float) -> int:
    return max(max(line_heights, default=font_size), int(round(font_size * line_height)))


def render_text_layer(args: dict[str, Any], *, ctx: ToolContext) -> ToolResultRecord:
    spec = ctx.state.get("design_spec")
    if spec is None:
        return obs_error("propose_design_spec must be called first", category="validation")

    canvas = spec.canvas
    cw, ch = int(canvas["w_px"]), int(canvas["h_px"])

    layer_id = args["layer_id"]
    name = args["name"]
    raw_text = args.get("text")
    text = "" if raw_text is None else str(raw_text)
    if not text.strip():
        return obs_error(
            "text layer content must not be empty; use kind='background' or "
            "kind='image' for visual fills",
            category="validation",
        )
    font_family = args.get("font_family")
    font_size = int(args["font_size_px"])
    fill = args.get("fill", "#000000")
    bbox = args["bbox"]
    align = args.get("align", "left")
    effects = args.get("effects") or {}
    spec_layer = _find_spec_layer(getattr(spec, "layer_graph", None), layer_id)
    raw_font_weight, font_weight_from_spec = _style_arg(args, spec_layer, "font_weight")
    raw_font_style, font_style_from_spec = _style_arg(args, spec_layer, "font_style")
    raw_line_height, line_height_from_spec = _style_arg(args, spec_layer, "line_height")
    raw_letter_spacing, letter_spacing_from_spec = _style_arg(args, spec_layer, "letter_spacing")
    raw_text_transform, text_transform_from_spec = _style_arg(args, spec_layer, "text_transform")
    font_weight = _coerce_font_weight(raw_font_weight)
    font_style = _coerce_font_style(raw_font_style)
    line_height = _coerce_line_height(raw_line_height)
    letter_spacing = _coerce_letter_spacing(raw_letter_spacing)
    text_transform = _coerce_text_transform(raw_text_transform)
    fallback_fields = [
        name for name, used in (
            ("font_weight", font_weight_from_spec),
            ("font_style", font_style_from_spec),
            ("line_height", line_height_from_spec),
            ("letter_spacing", letter_spacing_from_spec),
            ("text_transform", text_transform_from_spec),
        )
        if used
    ]
    if fallback_fields:
        log("text.typography_fallback", layer=layer_id, fields=fallback_fields)

    font_path: Path | None = None
    try:
        font_path, resolved_family, was_fallback = _resolve_font(font_family, ctx)
        _load_font(font_path, font_size)
    except Exception as e:
        return obs_error(f"font load failed ({font_path or font_family}): {e}", category="validation")

    bx, by, bw, bh = int(bbox["x"]), int(bbox["y"]), int(bbox["w"]), int(bbox["h"])

    # v2.2 versioning: each render bumps the layer's version counter and
    # writes to a new file (prior versions stay on disk for training data).
    prior = ctx.state["rendered_layers"].get(layer_id) or {}
    prior_sha = prior.get("sha256")
    version = ctx.next_layer_version(layer_id)
    out_path = ctx.layers_dir / f"text_{layer_id}.v{version}.png"
    try:
        resolved_family, was_fallback = render_text_png(
            text=text,
            font_family=font_family,
            font_size=font_size,
            fill=fill,
            bbox={"x": bx, "y": by, "w": bw, "h": bh},
            align=align,
            effects=effects,
            font_weight=font_weight,
            font_style=font_style,
            line_height=line_height,
            letter_spacing=letter_spacing,
            text_transform=text_transform,
            canvas_w=cw,
            canvas_h=ch,
            out_path=out_path,
            ctx=ctx,
        )
    except Exception as e:
        return obs_error(f"text render failed: {e}", category="api")
    sha = sha256_file(out_path)

    ctx.state["rendered_layers"][layer_id] = {
        "layer_id": layer_id,
        "name": name,
        "kind": "text",
        "z_index": int(args.get("z_index", 1)),
        "bbox": {"x": bx, "y": by, "w": bw, "h": bh},
        "text": text,
        "font_family": resolved_family,
        "font_size_px": font_size,
        "font_weight": font_weight,
        "font_style": font_style,
        "line_height": line_height,
        "letter_spacing": letter_spacing,
        "text_transform": text_transform,
        "fill": fill,
        "align": align,
        "effects": effects,
        "src_path": str(out_path),
        "sha256": sha,
        "version": version,
    }
    log("text.rendered", layer=name, font=resolved_family, fallback=was_fallback,
        weight=font_weight, line_height=line_height, letter_spacing=letter_spacing,
        text_transform=text_transform, chars=len(text), path=str(out_path),
        version=version)

    payload: dict[str, Any] = {
        "layer_id": layer_id,
        "sha256": sha,
        "relative_path": f"layers/text_{layer_id}.v{version}.png",
        "version": version,
    }
    if prior_sha:
        payload["supersedes_sha256"] = prior_sha
    if was_fallback:
        # Surface the font fallback as a warning the policy can learn from
        # (not an error — text was still rendered). Using payload key keeps
        # the result parseable; no prose.
        payload["font_fallback"] = {
            "requested": font_family,
            "used": resolved_family,
        }
    return obs_ok(payload)


def _line_x(align: str, bx: int, bw: int, line_w: float) -> int:
    if align == "center":
        return int(bx + max(0, (bw - line_w) / 2))
    if align == "right":
        return int(bx + max(0, bw - line_w))
    return bx
