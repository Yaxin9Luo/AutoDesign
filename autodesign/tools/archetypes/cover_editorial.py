"""`cover_editorial` archetype — large serif title + subtitle + author byline.

Renders a self-contained cover slide whose visual hierarchy is:

- Big serif headline (NotoSerifSC-Bold, 120 px) anchored ~52% down the slide.
- Smaller subtitle / tagline directly underneath (32–48 px, sans).
- Author / affiliation byline at the bottom strip (24–28 px).

The renderer never rasterizes text — every layer is a native python-pptx
TextFrame. Background / image children are left to the caller's existing
inline render path (this file only emits text shapes); they get rendered
via the dispatcher's pre-pass over non-text kinds.

Title text receives v2.7.2's `_with_section_prefix` so a planner-supplied
`section_number` shows up in the cover headline if present (rare on
covers, but keeps the contract uniform).
"""

from __future__ import annotations

from typing import Any

from ._common import (
    add_textbox,
    find_text_child,
    first_title_child,
    title_text_with_section,
    write_speaker_notes,
)


# Default theme — overridden by any planner-supplied
# `effects.fill` on individual children.
_DEFAULT_TITLE_HEX = "#0f172a"
_DEFAULT_SUBTITLE_HEX = "#475569"
_DEFAULT_AUTHORS_HEX = "#64748b"


def render_cover_editorial(
    slide_node: Any,
    slide: Any,
    slide_w: int,
    slide_h: int,
    ctx: Any,
) -> None:
    """Lay out a cover_editorial slide.

    The renderer pulls three text children from `slide_node` if present:

    - **title** — first child matching `_is_title_child` (or whose name
      contains "title" / "headline").
    - **subtitle** — first non-title child whose name contains
      "subtitle" / "tagline" / "byline_sub".
    - **authors** — first child whose name contains "author" / "byline" /
      "affiliation".

    Missing children fall back to safe defaults (slide.name for title,
    empty subtitle / authors). Side margins are 120 px (matches the
    deck typography rules in designer.md).
    """
    margin = 120
    text_w = max(1, slide_w - 2 * margin)

    # ── Title ──────────────────────────────────────────────────────
    title_child = first_title_child(slide_node)
    title_text = title_text_with_section(slide_node, fallback="")
    title_font = (
        getattr(title_child, "font_family", None)
        if title_child is not None else None
    ) or "NotoSerifSC-Bold"
    title_size = int(
        (getattr(title_child, "font_size_px", None)
         if title_child is not None else None) or 120
    )
    title_fill = _resolve_fill(title_child, _DEFAULT_TITLE_HEX)
    title_align = _resolve_align(title_child, "left")

    # Anchor title around 50% slide height; reserve 240 px height so two
    # lines of 120 px serif fit comfortably.
    title_y = int(slide_h * 0.50) - 80
    add_textbox(
        slide,
        text=title_text,
        x_px=margin, y_px=title_y, w_px=text_w, h_px=240,
        font_family=title_font, font_size_px=title_size,
        align=title_align, fill_hex=title_fill, bold=True,
        name="cover_editorial_title",
    )

    # ── Subtitle ───────────────────────────────────────────────────
    subtitle_child = find_text_child(
        slide_node, name_hints=("subtitle", "tagline", "byline_sub", "deck"),
    )
    subtitle_text = _child_text(subtitle_child)
    if subtitle_text:
        sub_font = (
            getattr(subtitle_child, "font_family", None)
            or "NotoSansSC-Bold"
        )
        sub_size = int(getattr(subtitle_child, "font_size_px", None) or 36)
        sub_fill = _resolve_fill(subtitle_child, _DEFAULT_SUBTITLE_HEX)
        sub_align = _resolve_align(subtitle_child, title_align)
        sub_y = title_y + 240 + 16
        add_textbox(
            slide,
            text=subtitle_text,
            x_px=margin, y_px=sub_y, w_px=text_w, h_px=120,
            font_family=sub_font, font_size_px=sub_size,
            align=sub_align, fill_hex=sub_fill,
            name="cover_editorial_subtitle",
        )

    # ── Author byline (bottom strip) ───────────────────────────────
    authors_child = find_text_child(
        slide_node, name_hints=("author", "byline", "affiliation"),
    )
    authors_text = _child_text(authors_child)
    if authors_text:
        a_font = (
            getattr(authors_child, "font_family", None)
            or "NotoSansSC-Bold"
        )
        a_size = int(getattr(authors_child, "font_size_px", None) or 26)
        a_fill = _resolve_fill(authors_child, _DEFAULT_AUTHORS_HEX)
        a_align = _resolve_align(authors_child, title_align)
        # Bottom strip — 80 px from the slide bottom, 60 px tall.
        a_y = max(0, slide_h - 140)
        add_textbox(
            slide,
            text=authors_text,
            x_px=margin, y_px=a_y, w_px=text_w, h_px=80,
            font_family=a_font, font_size_px=a_size,
            align=a_align, fill_hex=a_fill,
            name="cover_editorial_authors",
        )

    write_speaker_notes(slide, slide_node)


def _child_text(child: Any | None) -> str:
    if child is None:
        return ""
    return (getattr(child, "text", None) or "").strip()


def _resolve_fill(child: Any | None, default: str) -> str:
    if child is None:
        return default
    effects = getattr(child, "effects", None)
    fill = getattr(effects, "fill", None) if effects is not None else None
    if isinstance(fill, str) and fill.startswith("#"):
        return fill
    return default


def _resolve_align(child: Any | None, default: str) -> str:
    if child is None:
        return default
    align = getattr(child, "align", None)
    if align in ("left", "center", "right"):
        return align
    return default
