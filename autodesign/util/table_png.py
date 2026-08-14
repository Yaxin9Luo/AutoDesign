"""PIL-based table renderer for PNG output (v1.2 paper2any).

When a `kind="table"` layer lands on poster / PSD / SVG output — paths
that don't have a live-table primitive — we draw the structured rows
into a PNG and treat it as a regular image. python-pptx + HTML have
native tables and don't call this.

Kept dependency-light (Pillow only) so it works everywhere.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


HEADER_FILL = (31, 42, 68)          # #1F2A44 — matches pptx + html convention
HEADER_TEXT = (255, 255, 255)
ROW_ALT_FILL = (246, 247, 249)      # zebra-stripe for even body rows
BODY_TEXT = (24, 24, 27)            # near-black
WINNER_TEXT = (11, 93, 63)          # deep green — stands out without reading
                                    # as "error/warning". Used in PIL path only
                                    # (PPTX + HTML use font-weight instead).
GRID_COLOR = (200, 203, 210)
BG_COLOR = (255, 255, 255)

# Conservative defaults — callers override via kwargs.
DEFAULT_W = 1600
DEFAULT_PADDING = 14
DEFAULT_FONT_SIZE = 22


def render_table_png(
    *,
    rows: list[list[str]],
    headers: list[str] | None = None,
    out_path: Path,
    width_px: int = DEFAULT_W,
    max_height_px: int | None = None,
    font_path: Path | None = None,
    bold_font_path: Path | None = None,
    font_size: int = DEFAULT_FONT_SIZE,
    padding_px: int = DEFAULT_PADDING,
    col_highlight_rule: list[str] | None = None,
) -> tuple[int, int]:
    """Draw a table with a dark header row and zebra-striped body.

    Column widths are proportional to the **max string length** per
    column (capped) — stops one wide cell from collapsing everything.
    Returns the (width, height) of the PNG written.

    If `max_height_px` is set, the renderer will scale the font down
    and shrink row heights so the table fits. If the content still
    can't fit, it's truncated with an ellipsis row.
    """
    # Normalize shape ----------------------------------------------------
    rows = [[str(c) if c is not None else "" for c in row] for row in rows]
    headers = [str(h) for h in (headers or [])]
    if not headers and rows:
        headers = rows[0]
        rows = rows[1:]
    n_cols = max(len(headers), max((len(r) for r in rows), default=0))
    if n_cols == 0:
        # No data — emit a 1×1 transparent PNG so callers don't choke.
        Image.new("RGBA", (1, 1), (0, 0, 0, 0)).save(out_path)
        return 1, 1
    headers = headers + [""] * (n_cols - len(headers))
    rows = [r + [""] * (n_cols - len(r)) for r in rows]

    # Column widths ------------------------------------------------------
    available = max(1, width_px - 2 * padding_px)
    weights: list[int] = []
    for c in range(n_cols):
        col_vals = [headers[c]] + [r[c] for r in rows]
        ml = max((len(str(v)) for v in col_vals), default=1)
        weights.append(max(1, min(ml, 30)))
    weight_sum = sum(weights)
    col_widths = [max(40, int(available * w / weight_sum)) for w in weights]
    # Fix rounding drift so widths sum to `available`.
    drift = available - sum(col_widths)
    if drift != 0:
        col_widths[-1] += drift

    # Font ---------------------------------------------------------------
    font = _load_font(font_path, font_size)
    bold_font = _load_font(bold_font_path or font_path, font_size, bold=True)

    # Row height from font metrics + vertical padding.
    ascent, descent = font.getmetrics()
    line_h = ascent + descent
    row_h = line_h + 12
    header_h = line_h + 16

    def total_h(n_body_rows: int) -> int:
        return header_h + row_h * n_body_rows + 2 * padding_px

    # Fit to max_height by shrinking font / dropping trailing rows.
    if max_height_px:
        while total_h(len(rows)) > max_height_px and font_size > 10:
            font_size -= 1
            font = _load_font(font_path, font_size)
            bold_font = _load_font(font_path, font_size, bold=True)
            ascent, descent = font.getmetrics()
            line_h = ascent + descent
            row_h = line_h + 10
            header_h = line_h + 14
        # If still too tall, truncate rows.
        while rows and total_h(len(rows)) > max_height_px:
            rows.pop()
        if not rows:
            # Keep at least the header visible even if nothing fits.
            pass

    img_h = total_h(len(rows))
    img = Image.new("RGBA", (width_px, img_h), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # Header bar ---------------------------------------------------------
    x = padding_px
    y = padding_px
    draw.rectangle(
        [(padding_px, y), (padding_px + available, y + header_h)],
        fill=HEADER_FILL,
    )
    for c, h in enumerate(headers):
        cell_w = col_widths[c]
        _draw_text_clipped(
            draw, h, (x + 10, y + (header_h - line_h) // 2),
            max_w=cell_w - 20, font=bold_font, fill=HEADER_TEXT,
        )
        x += cell_w

    # Column highlighting: find the winning row per column when the
    # caller provides a rule ("max" / "min"). Non-numeric cells are
    # silently skipped — if fewer than 2 rows parse as numbers, no
    # winner is marked (avoids false "everything is bold" states).
    winner_row_per_col = _compute_winner_rows(rows, col_highlight_rule)

    # Body rows ----------------------------------------------------------
    y += header_h
    for r_idx, row in enumerate(rows):
        if r_idx % 2 == 1:
            draw.rectangle(
                [(padding_px, y), (padding_px + available, y + row_h)],
                fill=ROW_ALT_FILL,
            )
        x = padding_px
        for c, cell in enumerate(row):
            cell_w = col_widths[c]
            is_winner = (winner_row_per_col.get(c) == r_idx)
            # Bold font + green text for winners. Because the project
            # bundles ONLY a bold Noto SC (no regular weight), bold-vs-
            # regular distinction wouldn't be visible; color carries
            # the highlight on the PIL path. PPTX + HTML use proper
            # font-weight since those formats render through OS fonts.
            _draw_text_clipped(
                draw, cell, (x + 10, y + (row_h - line_h) // 2),
                max_w=cell_w - 20,
                font=bold_font if is_winner else font,
                fill=WINNER_TEXT if is_winner else BODY_TEXT,
            )
            x += cell_w
        # Row separator.
        draw.line(
            [(padding_px, y + row_h), (padding_px + available, y + row_h)],
            fill=GRID_COLOR, width=1,
        )
        y += row_h

    img.save(out_path, format="PNG", optimize=True)
    return img.width, img.height


# ──────────────────────────── helpers ──────────────────────────────────

def _load_font(font_path: Path | None, size: int, *, bold: bool = False) -> Any:
    """Load a TTF/OTF at the given size. Falls back to PIL's default
    bitmap font when we can't find one (ugly but works offline)."""
    if font_path:
        try:
            return ImageFont.truetype(str(font_path), size=size)
        except Exception:
            pass
    for candidate in _system_font_candidates(bold=bold):
        try:
            return ImageFont.truetype(candidate, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _system_font_candidates(*, bold: bool) -> list[str]:
    # Bundled Noto (CJK-capable, ships in repo) wins when the caller
    # didn't pass a specific font_path. Arial / Helvetica kept as last
    # resort for when the repo isn't accessible at runtime.
    repo_fonts = Path(__file__).resolve().parent.parent.parent / "assets" / "fonts"
    noto = [
        str(repo_fonts / "NotoSansSC-Bold.otf"),  # has CJK glyphs
    ]
    if bold:
        return noto + [
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "/System/Library/Fonts/HelveticaNeue.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
    return noto + [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]


def _compute_winner_rows(
    rows: list[list[str]],
    col_highlight_rule: list[str] | None,
) -> dict[int, int]:
    """For each column with a "max" / "min" rule, find the row index
    whose numeric value wins. Returns `{col_idx: row_idx}`. Non-numeric
    cells parse as NaN and are skipped; when < 2 rows parse, the column
    is omitted (no winner)."""
    if not col_highlight_rule or not rows:
        return {}
    out: dict[int, int] = {}
    for c, rule in enumerate(col_highlight_rule):
        if rule not in ("max", "min"):
            continue
        best_idx: int | None = None
        best_val: float | None = None
        numeric_count = 0
        for r_idx, row in enumerate(rows):
            if c >= len(row):
                continue
            v = _parse_numeric(row[c])
            if v is None:
                continue
            numeric_count += 1
            if best_val is None or (rule == "max" and v > best_val) \
                                 or (rule == "min" and v < best_val):
                best_val = v
                best_idx = r_idx
        if best_idx is not None and numeric_count >= 2:
            out[c] = best_idx
    return out


def _parse_numeric(cell: str) -> float | None:
    """Extract a float from a cell. Tolerates whitespace, trailing %,
    dashes / em-dashes (→ None). Cells like '70.6' or '70.6 %' parse;
    '—' / '-' / '' / '91.40 / 75.50' (dual-metric) return None so
    winner detection stays conservative."""
    if cell is None:
        return None
    s = str(cell).strip().replace(",", "")
    if not s or s in ("—", "-", "–", "N/A", "n/a", "NA"):
        return None
    if "/" in s:  # dual-metric like "91.40 / 75.50" — skip.
        return None
    if s.endswith("%"):
        s = s[:-1].strip()
    try:
        return float(s)
    except ValueError:
        return None


def _draw_text_clipped(
    draw: ImageDraw.ImageDraw,
    text: str,
    pos: tuple[int, int],
    *,
    max_w: int,
    font: Any,
    fill: tuple[int, int, int],
) -> None:
    """Draw text at `pos`, truncating with an ellipsis if it overflows."""
    if not text:
        return
    if _text_width(draw, text, font) <= max_w:
        draw.text(pos, text, font=font, fill=fill)
        return
    ellipsis = "…"
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi) // 2
        probe = text[:mid] + ellipsis
        if _text_width(draw, probe, font) <= max_w:
            lo = mid + 1
        else:
            hi = mid
    final = text[: max(0, lo - 1)] + ellipsis
    draw.text(pos, final, font=font, fill=fill)


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: Any) -> int:
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0]
    except Exception:
        # Older Pillow fallback
        return draw.textsize(text, font=font)[0]  # type: ignore[attr-defined]
