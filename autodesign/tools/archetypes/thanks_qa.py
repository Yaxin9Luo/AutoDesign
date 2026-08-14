"""`thanks_qa` archetype — closing slide with a Thanks/Q&A headline,
contact row, and optional code link.

Three text shapes:

- **headline** — large serif "Thanks · Questions?" (or whatever the
  planner provided as the title child).
- **contact row** — a single line with the speaker's contact info
  (email / handle / affiliation), centered.
- **code link** — optional second line for arxiv / repo / website URL.

Section_number is suppressed on this archetype: closing slides never
carry "§N" prefixes (matches v2.7.2's chrome-slide rule in
`section_renumber._is_chrome_slide`).
"""

from __future__ import annotations

from typing import Any

from ._common import (
    add_textbox,
    collect_text_children,
    first_title_child,
    write_speaker_notes,
)


_CONTACT_HINTS = ("contact", "email", "handle", "affiliation",
                  "byline", "subtitle")
_LINK_HINTS = ("link", "url", "code", "repo", "arxiv", "website")
_DEFAULT_HEADLINE_HEX = "#0f172a"
_DEFAULT_CONTACT_HEX = "#475569"
_DEFAULT_LINK_HEX = "#7f1d1d"


def render_thanks_qa(
    slide_node: Any,
    slide: Any,
    slide_w: int,
    slide_h: int,
    ctx: Any,
) -> None:
    """Lay out a thanks / Q&A closing slide.

    Headline anchored at ~38% slide height; contact line directly
    underneath; optional link line under that. All centered. Section
    prefix is intentionally NOT applied — closing slides should not
    inherit "§N · " labels.
    """
    margin = 120
    text_w = max(1, slide_w - 2 * margin)

    # Headline — pull from first title child or fall back to "Thanks".
    title_child = first_title_child(slide_node)
    headline = (getattr(title_child, "text", None) or "").strip() \
        if title_child is not None else ""
    if not headline:
        headline = "Thanks · Questions?"
    head_size = int(
        (getattr(title_child, "font_size_px", None)
         if title_child is not None else None) or 96
    )
    head_font = (
        getattr(title_child, "font_family", None)
        if title_child is not None else None
    ) or "NotoSerifSC-Bold"
    head_fill = _resolve_fill(title_child, _DEFAULT_HEADLINE_HEX)
    head_y = int(slide_h * 0.38)
    add_textbox(
        slide,
        text=headline,
        x_px=margin, y_px=head_y, w_px=text_w, h_px=180,
        font_family=head_font, font_size_px=head_size,
        align="center", fill_hex=head_fill, bold=True,
        name="thanks_qa_headline",
    )

    contact_child, link_child = _pick_children(slide_node)

    contact_text = (getattr(contact_child, "text", None) or "").strip() \
        if contact_child is not None else ""
    contact_size = int(
        (getattr(contact_child, "font_size_px", None)
         if contact_child is not None else None) or 28
    )
    contact_font = (
        getattr(contact_child, "font_family", None)
        if contact_child is not None else None
    ) or "NotoSansSC-Bold"
    contact_fill = _resolve_fill(contact_child, _DEFAULT_CONTACT_HEX)
    contact_y = head_y + 200
    add_textbox(
        slide,
        text=contact_text,
        x_px=margin, y_px=contact_y, w_px=text_w, h_px=80,
        font_family=contact_font, font_size_px=contact_size,
        align="center", fill_hex=contact_fill,
        name="thanks_qa_contact",
    )

    if link_child is not None:
        link_text = (getattr(link_child, "text", None) or "").strip()
        link_size = int(getattr(link_child, "font_size_px", None) or 24)
        link_font = (getattr(link_child, "font_family", None)
                     or "JetBrainsMono")
        link_fill = _resolve_fill(link_child, _DEFAULT_LINK_HEX)
        link_y = contact_y + 80
        add_textbox(
            slide,
            text=link_text,
            x_px=margin, y_px=link_y, w_px=text_w, h_px=60,
            font_family=link_font, font_size_px=link_size,
            align="center", fill_hex=link_fill,
            name="thanks_qa_link",
        )

    write_speaker_notes(slide, slide_node)


def _pick_children(slide_node: Any) -> tuple[Any | None, Any | None]:
    """Return (contact_child, link_child).

    Contact is the first text child matching a contact hint;
    link is the first child matching a link/code hint. Children whose
    name contains "title" / "headline" are excluded — those drive the
    headline shape.
    """
    contact: Any | None = None
    link: Any | None = None
    leftovers: list[Any] = []
    for c in collect_text_children(slide_node):
        cname = (getattr(c, "name", None) or "").lower()
        if "title" in cname or "headline" in cname:
            continue
        if any(h in cname for h in _LINK_HINTS) and link is None:
            link = c
            continue
        if any(h in cname for h in _CONTACT_HINTS) and contact is None:
            contact = c
            continue
        leftovers.append(c)
    if contact is None and leftovers:
        contact = leftovers.pop(0)
    if link is None and leftovers:
        link = leftovers.pop(0)
    return contact, link


def _resolve_fill(child: Any | None, default: str) -> str:
    if child is None:
        return default
    effects = getattr(child, "effects", None)
    fill = getattr(effects, "fill", None) if effects is not None else None
    if isinstance(fill, str) and fill.startswith("#"):
        return fill
    return default
