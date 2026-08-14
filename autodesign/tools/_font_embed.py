"""Shared font subsetting + base64 @font-face embedding.

Used by both the SVG renderer (tools/composite.py) and the HTML renderer
(tools/html_renderer.py) so their self-contained outputs embed only the
glyphs that actually appear in the text layers, encoded as inline WOFF2.

Shape: `build_font_face_css({family_name: {used chars}}, ctx) -> str`.
Returns an empty string when fonttools isn't installed (output then relies
on system fonts — a graceful degradation for minimal environments).
"""

from __future__ import annotations

import base64
import io
from typing import TYPE_CHECKING

from ..util.logging import log

if TYPE_CHECKING:
    from ._contract import ToolContext


def build_font_face_css(fonts_used: dict[str, set[str]],
                        ctx: "ToolContext") -> str:
    """Subset each font to only-used glyphs, WOFF2-encode, return @font-face CSS.

    Returns an empty string if fonttools isn't installed or no fonts resolved.
    Logs one `svg.font.embed` event per family on success.
    """
    if not fonts_used:
        return ""
    try:
        from fontTools import subset as ft_subset
    except ImportError:
        log("svg.font.skip", reason="fonttools not installed; output relies on system fonts")
        return ""

    css_parts: list[str] = []
    for family, chars in fonts_used.items():
        font_file = ctx.settings.fonts.get(family)
        if not font_file:
            continue
        font_path = ctx.settings.fonts_dir / font_file
        if not font_path.exists():
            continue
        try:
            options = ft_subset.Options()
            options.flavor = "woff2"
            options.with_zopfli = False
            options.layout_features = ["*"]
            options.name_IDs = ["*"]
            options.notdef_glyph = True
            options.recommended_glyphs = True
            options.drop_tables += ["DSIG"]
            font = ft_subset.load_font(str(font_path), options)
            subsetter = ft_subset.Subsetter(options=options)
            subsetter.populate(unicodes={ord(c) for c in chars if not c.isspace()})
            subsetter.subset(font)
            buf = io.BytesIO()
            ft_subset.save_font(font, buf, options)
            woff2_bytes = buf.getvalue()
        except Exception as e:
            log("svg.font.subset_fail", family=family, error=str(e))
            continue
        b64 = base64.b64encode(woff2_bytes).decode("ascii")
        weight_range = " font-weight: 100 900;" if "Variable" in font_file else ""
        css_parts.append(
            f"@font-face {{ font-family: '{family}'; "
            f"src: url(data:font/woff2;base64,{b64}) format('woff2');"
            f"{weight_range} }}"
        )
        log("svg.font.embed", family=family, glyphs=len(chars), bytes=len(woff2_bytes))
    return "\n".join(css_parts)
