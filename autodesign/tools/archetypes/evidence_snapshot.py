"""`evidence_snapshot` archetype — one giant number + a one-line footnote.

Cloud Design's "stat slide" pattern: huge number (~250 px) dominates the
slide; everything else is whitespace. The slide's job is to make ONE
benchmark / metric land in the audience's memory.

This archetype is also the **default fallback** wired into the dispatch
table. To preserve byte-identical rendering for pre-v2.8.1 decks, this
function is NOT called when the slide carries the default
`archetype="evidence_snapshot"` AND has no obvious "big-number" child —
the dispatcher falls through to `_render_slide` (the original inline
path). This module's `render_evidence_snapshot` is only invoked when
the slide explicitly opts into the snapshot layout, which we detect by
the presence of a child whose `name` contains "stat" / "big_number" /
"hero_number" or whose `font_size_px >= 200`.

The dispatcher path is:

    archetype="evidence_snapshot" + has_big_number_child  →  this file
    archetype="evidence_snapshot" + no big-number child   →  default
"""

from __future__ import annotations

from typing import Any

from ._common import (
    add_textbox,
    collect_text_children,
    write_speaker_notes,
)


_BIG_NUMBER_HINTS = ("big_number", "hero_number", "stat", "headline_number",
                     "metric_value")
_FOOTNOTE_HINTS = ("footnote", "stat_caption", "metric_caption", "subtitle",
                   "byline_sub", "caption")
_DEFAULT_NUMBER_HEX = "#0f172a"
_DEFAULT_FOOTNOTE_HEX = "#475569"


def has_big_number(slide_node: Any) -> bool:
    """Heuristic for the dispatcher: is this slide opting into the
    `evidence_snapshot` layout? True when ANY text child is named like
    a hero metric OR has `font_size_px >= 200`.

    Pre-v2.8.1 decks set neither — they keep the default inline render.
    """
    for c in collect_text_children(slide_node):
        cname = (getattr(c, "name", None) or "").lower()
        if any(h in cname for h in _BIG_NUMBER_HINTS):
            return True
        size = int(getattr(c, "font_size_px", None) or 0)
        if size >= 200:
            return True
    return False


def render_evidence_snapshot(
    slide_node: Any,
    slide: Any,
    slide_w: int,
    slide_h: int,
    ctx: Any,
) -> None:
    """Lay out one hero number centered horizontally with a single
    footnote line beneath. No title bar — the number IS the slide.

    Picks one big-number child and one footnote child:

    - **big_number** — first child named like a stat OR with
      `font_size_px >= 200`.
    - **footnote** — first text child whose name contains "footnote" /
      "caption" / "stat_caption", OR the second non-number child.
    """
    big_child, footnote_child = _pick_children(slide_node)

    margin = 120
    text_w = max(1, slide_w - 2 * margin)

    # Default number font_size = 250 px so the smoke can assert >=200.
    big_size = int(getattr(big_child, "font_size_px", None) or 250)
    big_text = (getattr(big_child, "text", None) or "0").strip() or "0"
    big_font = getattr(big_child, "font_family", None) or "NotoSerifSC-Bold"
    big_fill = _resolve_fill(big_child, _DEFAULT_NUMBER_HEX)
    # Big number is centered; reserve a generous box so descenders fit.
    big_h = int(big_size * 1.2)
    big_y = max(0, (slide_h - big_h) // 2 - 60)
    add_textbox(
        slide,
        text=big_text,
        x_px=margin, y_px=big_y, w_px=text_w, h_px=big_h,
        font_family=big_font, font_size_px=big_size,
        align="center", fill_hex=big_fill, bold=True,
        name="evidence_snapshot_number",
    )

    # Footnote — one line, smaller, directly under the number.
    foot_text = (getattr(footnote_child, "text", None) or "").strip() \
        if footnote_child is not None else ""
    if not foot_text:
        # Always emit a footnote shape so the smoke can verify the
        # 1-number-1-footnote contract; empty string is a valid TextFrame.
        foot_text = ""
    foot_size = int(getattr(footnote_child, "font_size_px", None) or 28) \
        if footnote_child is not None else 28
    foot_font = (getattr(footnote_child, "font_family", None)
                 if footnote_child is not None else None) or "NotoSansSC-Bold"
    foot_fill = _resolve_fill(footnote_child, _DEFAULT_FOOTNOTE_HEX)
    foot_y = big_y + big_h + 16
    add_textbox(
        slide,
        text=foot_text,
        x_px=margin, y_px=foot_y, w_px=text_w, h_px=80,
        font_family=foot_font, font_size_px=foot_size,
        align="center", fill_hex=foot_fill,
        name="evidence_snapshot_footnote",
    )

    write_speaker_notes(slide, slide_node)


def _pick_children(slide_node: Any) -> tuple[Any, Any | None]:
    """Return (big_number_child, footnote_child).

    big_number_child is mandatory; falls back to a synthetic LayerNode
    with text="0" if the slide has zero text children. footnote_child
    is optional (None when no candidate exists).
    """
    children = collect_text_children(slide_node)
    big = None
    foot = None
    for c in children:
        cname = (getattr(c, "name", None) or "").lower()
        size = int(getattr(c, "font_size_px", None) or 0)
        is_big = (
            any(h in cname for h in _BIG_NUMBER_HINTS) or size >= 200
        )
        if is_big and big is None:
            big = c
            continue
        if foot is None and any(h in cname for h in _FOOTNOTE_HINTS):
            foot = c
            continue
    # If we still have no big number but at least one child, promote it.
    if big is None and children:
        big = children[0]
        if foot is None and len(children) > 1:
            foot = children[1]
    if foot is None and len(children) > 1:
        for c in children:
            if c is not big:
                foot = c
                break
    if big is None:
        big = _SyntheticChild()
    return big, foot


class _SyntheticChild:
    text = "0"
    font_family = "NotoSerifSC-Bold"
    font_size_px = 250
    align = "center"
    effects = None


def _resolve_fill(child: Any | None, default: str) -> str:
    if child is None:
        return default
    effects = getattr(child, "effects", None)
    fill = getattr(effects, "fill", None) if effects is not None else None
    if isinstance(fill, str) and fill.startswith("#"):
        return fill
    return default
