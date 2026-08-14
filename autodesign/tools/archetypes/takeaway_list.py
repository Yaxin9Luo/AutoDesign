"""`takeaway_list` archetype — 3 bullet items + optional closing slogan.

Cloud Design's "what to remember" pattern: title strip, exactly 3
bullets evenly distributed down the slide, optional one-line slogan
at the bottom. Each bullet renders as a *group* of two textboxes
(marker + body) so it stays editable in PowerPoint as two distinct
text runs the user can re-style independently.
"""

from __future__ import annotations

from typing import Any

from ._common import (
    add_textbox,
    collect_text_children,
    title_text_with_section,
    write_speaker_notes,
)


_BULLET_NAME_HINTS = ("bullet", "takeaway", "point", "item")
_SLOGAN_NAME_HINTS = ("slogan", "tagline", "closer", "summary_line")
_DEFAULT_TITLE_HEX = "#0f172a"
_DEFAULT_BULLET_HEX = "#0f172a"
_DEFAULT_MARKER_HEX = "#7f1d1d"
_DEFAULT_SLOGAN_HEX = "#475569"


def render_takeaway_list(
    slide_node: Any,
    slide: Any,
    slide_w: int,
    slide_h: int,
    ctx: Any,
) -> None:
    """Lay out a takeaway slide.

    Three bullet rows. Each row is two shapes:

    - **marker** — the index ("01", "02", "03") in oxblood, mono.
    - **body** — the bullet text.

    Always emits 3 bullet groups even if the planner provided fewer
    text children — empty bullets render as empty TextFrames so the
    visual rhythm stays consistent.
    """
    margin = 120
    text_w = max(1, slide_w - 2 * margin)

    # Title
    title_text = title_text_with_section(
        slide_node, fallback="Key takeaways",
    )
    add_textbox(
        slide,
        text=title_text,
        x_px=margin, y_px=80, w_px=text_w, h_px=140,
        font_family="NotoSerifSC-Bold", font_size_px=72,
        align="left", fill_hex=_DEFAULT_TITLE_HEX, bold=True,
        name="takeaway_list_title",
    )

    bullets, slogan = _pick_children(slide_node)

    # Bullet rows. Reserve y=280..880 for bullets; 200 px per row.
    bullet_top = 280
    row_h = 200
    marker_w = 120

    for i in range(3):
        child = bullets[i] if i < len(bullets) else None
        body = (getattr(child, "text", None) or "").strip() if child else ""
        body_size = int(
            (getattr(child, "font_size_px", None) if child else None) or 36
        )
        body_font = (
            getattr(child, "font_family", None) if child else None
        ) or "NotoSansSC-Bold"
        body_fill = _resolve_fill(child, _DEFAULT_BULLET_HEX)

        y = bullet_top + i * row_h
        marker = f"{i + 1:02d}"
        add_textbox(
            slide,
            text=marker,
            x_px=margin, y_px=y, w_px=marker_w, h_px=row_h - 40,
            font_family="JetBrainsMono", font_size_px=44,
            align="left", fill_hex=_DEFAULT_MARKER_HEX, bold=True,
            name=f"takeaway_list_marker_{i + 1}",
        )
        add_textbox(
            slide,
            text=body,
            x_px=margin + marker_w + 24, y_px=y,
            w_px=max(1, text_w - marker_w - 24), h_px=row_h - 40,
            font_family=body_font, font_size_px=body_size,
            align="left", fill_hex=body_fill,
            name=f"takeaway_list_body_{i + 1}",
        )

    # Optional slogan bar at the bottom.
    if slogan is not None:
        slogan_text = (getattr(slogan, "text", None) or "").strip()
        if slogan_text:
            slogan_size = int(getattr(slogan, "font_size_px", None) or 28)
            slogan_font = (getattr(slogan, "font_family", None)
                           or "NotoSerifSC-Bold")
            slogan_fill = _resolve_fill(slogan, _DEFAULT_SLOGAN_HEX)
            add_textbox(
                slide,
                text=slogan_text,
                x_px=margin, y_px=max(0, slide_h - 140),
                w_px=text_w, h_px=80,
                font_family=slogan_font, font_size_px=slogan_size,
                align="center", fill_hex=slogan_fill, italic=True,
                name="takeaway_list_slogan",
            )

    write_speaker_notes(slide, slide_node)


def _pick_children(slide_node: Any) -> tuple[list[Any], Any | None]:
    """Return (up_to_3_bullet_children, optional_slogan_child).

    Bullets are the first 3 text children whose `name` matches a
    bullet hint, falling through to the first 3 non-title text children
    when no explicit naming is used. Slogan is the first child whose
    `name` matches a slogan hint, OR None.
    """
    children = collect_text_children(slide_node)
    title_child = None
    for c in children:
        cname = (getattr(c, "name", None) or "").lower()
        if "title" in cname or "headline" in cname:
            title_child = c
            break

    bullets: list[Any] = []
    slogan: Any | None = None
    leftovers: list[Any] = []

    for c in children:
        if c is title_child:
            continue
        cname = (getattr(c, "name", None) or "").lower()
        if any(h in cname for h in _SLOGAN_NAME_HINTS):
            if slogan is None:
                slogan = c
            continue
        if any(h in cname for h in _BULLET_NAME_HINTS):
            bullets.append(c)
        else:
            leftovers.append(c)

    if len(bullets) < 3:
        for c in leftovers:
            if len(bullets) >= 3:
                break
            bullets.append(c)
    return bullets[:3], slogan


def _resolve_fill(child: Any | None, default: str) -> str:
    if child is None:
        return default
    effects = getattr(child, "effects", None)
    fill = getattr(effects, "fill", None) if effects is not None else None
    if isinstance(fill, str) and fill.startswith("#"):
        return fill
    return default
