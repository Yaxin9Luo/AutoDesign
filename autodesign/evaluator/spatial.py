"""Image-native spatial content/whitespace detection.

The global non-white ratio is fooled three ways on real posters:
  * a big colored panel background or a solid dark/black void counts as "ink"
    even though it carries no information;
  * a large empty region inside one column never shows up as a full-width blank
    row, so the 1-D blank-run detector misses it;
  * high-frequency noise/texture has lots of pixel variation but no information.

This module builds a coarse occupancy grid where a cell counts as *content* only
if it has real STRUCTURE — measured as luma variation that survives a 4x
downsample (so pixel noise averages away while text strokes and figure edges
persist) — or recognized OCR text. Flat regions of any color (white, solid color,
black) and pure noise are treated as empty. From that grid it derives content
coverage and the largest empty rectangle: robust, format/resolution-invariant
signals for density and layout.
"""

from __future__ import annotations

from typing import Any

from PIL import Image, ImageDraw, ImageStat

# Cell sizes are expressed at this reference long edge and scaled to the actual
# image, so the grid has the same cell COUNT at any resolution → the same poster
# scores identically whether submitted at 1x, 0.6x, or 2x (resolution invariance).
REFERENCE_LONG_EDGE = 2048


def _scaled_px(px: int, w: int, h: int) -> int:
    return max(6, round(px * max(w, h) / REFERENCE_LONG_EDGE))


def _visual_cell_grid(visual_mask: Any, rows: int, cols: int, coverage: float = 0.22) -> list[list[bool]] | None:
    """Grid of cells whose visual-mask coverage exceeds ``coverage``.

    ``visual_mask`` is the raw per-pixel non-background mask from
    ``_cv_layout_regions``. A cell that is substantially covered by real visual
    content (figure/chart/table pixels) is treated as occupied even if its luma
    variance is low — a placed figure fills its footprint. Empty white gutters
    have ~0 mask coverage and stay void. Returns None when unavailable.
    """
    if visual_mask is None:
        return None
    try:
        import numpy as np

        m = np.asarray(visual_mask)
        if m.ndim != 2 or m.size == 0:
            return None
        binary = (m > 0)
        h, w = binary.shape
        grid = [[False] * cols for _ in range(rows)]
        for r in range(rows):
            y0, y1 = int(r * h / rows), max(int(r * h / rows) + 1, int((r + 1) * h / rows))
            for c in range(cols):
                x0, x1 = int(c * w / cols), max(int(c * w / cols) + 1, int((c + 1) * w / cols))
                if float(binary[y0:y1, x0:x1].mean()) >= coverage:
                    grid[r][c] = True
        return grid
    except Exception:  # noqa: BLE001 - CV/numpy optional; degrade to no crediting.
        return None


def content_occupancy(
    image: Image.Image,
    *,
    segments: list[dict[str, Any]] | None = None,
    visual_rects: list[dict[str, Any]] | None = None,
    visual_mask: Any = None,
    cell_px: int = 40,
    std_threshold: float = 12.0,
    noise_downsample: int = 4,
    heading_fraction: float = 0.14,
) -> dict[str, Any]:
    """Return a content-occupancy grid + coverage / largest-empty-region metrics.

    A cell counts as *content* only if its luma variation survives a
    ``noise_downsample``x shrink (structure persists, pixel noise collapses) and
    exceeds ``std_threshold``, or it is covered by recognized OCR text. Flat
    regions of any color, dark voids, and pure noise are treated as empty.

    ``heading_fraction`` excludes the top identity/title band from the density
    analysis — headers are allowed to be airy, so whitespace there is not a
    density problem and would otherwise be a false positive. The grid is kept
    full-size (so overlays line up) but heading rows are ignored: they never count
    as empty and are excluded from coverage / void detection.
    """
    gray = image.convert("L")
    w, h = gray.size
    eff_cell = _scaled_px(cell_px, w, h)  # resolution-invariant grid count
    cols = max(1, round(w / eff_cell))
    rows = max(1, round(h / eff_cell))
    cw = w / cols
    ch = h / rows
    heading_rows = max(0, min(rows - 1, int(round(rows * heading_fraction))))

    # Downsample once: real structure (text strokes, figure edges) keeps its
    # contrast at low resolution; high-frequency noise averages toward flat gray.
    ds = max(1, int(noise_downsample))
    small = gray.resize((max(1, w // ds), max(1, h // ds)), Image.BILINEAR) if ds > 1 else gray
    sw, sh = small.size
    scw = sw / cols
    sch = sh / rows

    occ = [[False] * cols for _ in range(rows)]
    std_grid = [[0.0] * cols for _ in range(rows)]
    for r in range(rows):
        sy0, sy1 = int(r * sch), max(int(r * sch) + 1, int((r + 1) * sch))
        for c in range(cols):
            sx0, sx1 = int(c * scw), max(int(c * scw) + 1, int((c + 1) * scw))
            cell = small.crop((sx0, sy0, sx1, sy1))
            std = ImageStat.Stat(cell).stddev[0]
            std_grid[r][c] = round(std, 2)
            occ[r][c] = std >= std_threshold

    # OCR text always counts as content (covers faint anti-aliased small text).
    for seg in segments or []:
        box = seg.get("box") or []
        if len(box) < 4:
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        for r in range(max(0, int(min(ys) / ch)), min(rows, int(max(ys) / ch) + 1)):
            for c in range(max(0, int(min(xs) / cw)), min(cols, int(max(xs) / cw) + 1)):
                occ[r][c] = True

    # Detected source figures/tables/charts count as content across their whole
    # bounding box. 2026-07-03: a figure deliberately placed in the layout occupies
    # that space even where its own interior is pale (LAI/data maps are white land +
    # sparse colored speckles; charts have white plot areas). The per-cell texture
    # test scored those interiors as void, tanking density on image-heavy posters
    # (greening/gravitational/planck) that a human reads as full. Crediting the
    # figure's footprint fixes that; empty white gaps between panels stay void.
    for rect in visual_rects or []:
        if not isinstance(rect, dict):
            continue
        x0, y0, x1, y1 = rect.get("x0"), rect.get("y0"), rect.get("x1"), rect.get("y1")
        if None in (x0, y0, x1, y1):
            continue
        for r in range(max(0, int(y0 / ch)), min(rows, int(y1 / ch) + 1)):
            for c in range(max(0, int(x0 / cw)), min(cols, int(x1 / cw) + 1)):
                occ[r][c] = True

    # Per-pixel visual mask credits figure/chart/table footprints that the coarse
    # rects miss (sparse maps that fragment into many small components).
    vgrid = _visual_cell_grid(visual_mask, rows, cols)
    if vgrid is not None:
        for r in range(rows):
            for c in range(cols):
                if vgrid[r][c]:
                    occ[r][c] = True

    # Exclude the heading band: those rows never count as empty and are dropped
    # from the coverage / void denominators.
    body_total = max(1, (rows - heading_rows) * cols)
    occupied = sum(1 for r in range(heading_rows, rows) for c in range(cols) if occ[r][c])
    empty_mask = [
        [(not occ[r][c]) if r >= heading_rows else False for c in range(cols)]
        for r in range(rows)
    ]
    area, (r0, c0, r1, c1) = _largest_rect(empty_mask)
    rect_px = {
        "x0": int(c0 * cw), "y0": int(r0 * ch),
        "x1": int((c1 + 1) * cw), "y1": int((r1 + 1) * ch),
    } if area > 0 else None

    return {
        "cols": cols,
        "rows": rows,
        "heading_rows": heading_rows,
        "cell_px": cell_px,
        "occ": occ,
        "std_grid": std_grid,
        "content_coverage": round(occupied / body_total, 4),
        "empty_cell_fraction": round(1 - occupied / body_total, 4),
        "largest_empty_rect_cell_ratio": round(area / body_total, 4),
        "largest_empty_rect_px": rect_px,
        "std_threshold": std_threshold,
        "noise_downsample": ds,
        "heading_fraction": heading_fraction,
    }


def blank_strips(
    image: Image.Image,
    *,
    visual_rects: list[dict[str, Any]] | None = None,
    visual_mask: Any = None,
    heading_fraction: float = 0.14,
    margin_fraction: float = 0.04,
    col_px: int = 64,
    row_px: int = 14,
    noise_downsample: int = 4,
    std_threshold: float = 12.0,
    min_run: int = 3,
) -> dict[str, Any]:
    """Detect horizontal blank strips (section-bottom gaps, empty bands).

    Coarse content_coverage absorbs these gaps and a globally finer grid
    over-detects line spacing. So scan each column in thin horizontal slices and
    keep only runs of >= ``min_run`` consecutive blank slices (~``min_run*row_px``
    px tall): single line/paragraph gaps are filtered out, real section gaps
    survive. The top heading band and a thin outer page margin
    (``margin_fraction`` on the left/right/bottom) are excluded — a normal poster
    frame is structurally-allowed whitespace, not a content gap. Returns the
    largest contiguous blank strip and total strip area relative to the body.
    """
    gray = image.convert("L")
    w, h = gray.size
    cols = max(1, round(w / _scaled_px(col_px, w, h)))   # resolution-invariant
    rows = max(1, round(h / _scaled_px(row_px, w, h)))
    cw, ch = w / cols, h / rows
    heading_rows = max(0, min(rows - 1, int(round(rows * heading_fraction))))
    margin_c = max(0, int(round(cols * margin_fraction)))
    margin_r = max(0, int(round(rows * margin_fraction)))
    c_lo, c_hi = margin_c, cols - margin_c
    r_hi = rows - margin_r
    ds = max(1, int(noise_downsample))
    small = gray.resize((max(1, w // ds), max(1, h // ds)), Image.BILINEAR) if ds > 1 else gray
    sw, sh = small.size
    scw, sch = sw / cols, sh / rows

    # Cells inside a detected source figure/table are never "blank" — a placed
    # figure occupies that band even where its interior is pale (see content_occupancy).
    in_visual = [[False] * cols for _ in range(rows)]
    for rect in visual_rects or []:
        if not isinstance(rect, dict):
            continue
        x0, y0, x1, y1 = rect.get("x0"), rect.get("y0"), rect.get("x1"), rect.get("y1")
        if None in (x0, y0, x1, y1):
            continue
        for r in range(max(0, int(y0 / ch)), min(rows, int(y1 / ch) + 1)):
            for c in range(max(0, int(x0 / cw)), min(cols, int(x1 / cw) + 1)):
                in_visual[r][c] = True
    vgrid = _visual_cell_grid(visual_mask, rows, cols)
    if vgrid is not None:
        for r in range(rows):
            for c in range(cols):
                if vgrid[r][c]:
                    in_visual[r][c] = True

    empty = [[False] * cols for _ in range(rows)]
    for r in range(heading_rows, r_hi):
        for c in range(c_lo, c_hi):
            if in_visual[r][c]:
                continue
            cell = small.crop((int(c * scw), int(r * sch),
                               max(int(c * scw) + 1, int((c + 1) * scw)),
                               max(int(r * sch) + 1, int((r + 1) * sch))))
            empty[r][c] = ImageStat.Stat(cell).stddev[0] < std_threshold

    strip = [[False] * cols for _ in range(rows)]
    for c in range(cols):
        run = 0
        for r in range(rows + 1):
            if r < rows and empty[r][c]:
                run += 1
            else:
                if run >= min_run:
                    for rr in range(r - run, r):
                        strip[rr][c] = True
                run = 0

    area, (r0, c0, r1, c1) = _largest_rect(strip)
    body = max(1, (r_hi - heading_rows) * (c_hi - c_lo))
    strip_cells = sum(1 for r in range(rows) for c in range(cols) if strip[r][c])
    rect_px = {
        "x0": int(c0 * cw), "y0": int(r0 * ch),
        "x1": int((c1 + 1) * cw), "y1": int((r1 + 1) * ch),
    } if area > 0 else None
    return {
        "cols": cols,
        "rows": rows,
        "cw": cw,
        "ch": ch,
        "heading_rows": heading_rows,
        "strip": strip,
        "largest_blank_strip_ratio": round(area / body, 4),
        "blank_strip_area_ratio": round(strip_cells / body, 4),
        "largest_blank_strip_px": rect_px,
        "min_run_px": min_run * row_px,
    }


# Figure-shape filter. Raw CV visual regions include section header bars, divider
# rules, and stylized-text rows OCR missed (thin wide strips, tiny specks). A real
# figure/table is a block, not a 20:1 strip — these keep only figure-shaped regions
# so visual-evidence signals get a realistic figure count. Calibrated on 96 real
# posters: raw regions run 11-14/poster (strip-dominated); shaped figures land at a
# realistic median 4, p90 7.
FIGURE_MAX_ASPECT = 5.0          # drop strips wider/taller than this ratio
FIGURE_MIN_SIDE_FRAC = 0.04      # short side >= 4% of the canvas short edge
FIGURE_MIN_AREA_FRAC = 0.004     # bbox area >= 0.4% of the canvas


def is_figure_shaped(rect: dict[str, float], short_edge: float, canvas_area: float) -> bool:
    """True if a detected visual region is figure/table-shaped (a block), not a thin
    header bar / rule / speck — used to turn noisy raw CV regions into a figure set."""
    w = float(rect.get("w") or 0.0)
    h = float(rect.get("h") or 0.0)
    if w <= 0 or h <= 0:
        return False
    aspect = max(w, h) / min(w, h)
    return (
        aspect <= FIGURE_MAX_ASPECT
        and min(w, h) >= FIGURE_MIN_SIDE_FRAC * float(short_edge)
        and (w * h) / max(1.0, float(canvas_area)) >= FIGURE_MIN_AREA_FRAC
    )


def _layout_damage_visual_regions(
    visual_regions: list[dict[str, Any]],
    *,
    width: int,
    height: int,
    long_edge: int,
) -> list[dict[str, Any]]:
    return [
        region for region in visual_regions
        if isinstance(region.get("rect"), dict)
        and not _is_flat_rule_like_visual(region["rect"], width=width, height=height, long_edge=long_edge)
    ]


def _is_flat_rule_like_visual(rect: dict[str, float], *, width: int, height: int, long_edge: int) -> bool:
    w = float(rect.get("w") or 0.0)
    h = float(rect.get("h") or 0.0)
    if w <= 0 or h <= 0:
        return True
    short_side = min(w, h)
    long_side = max(w, h)
    aspect = long_side / max(1.0, short_side)
    short_edge = float(max(1, min(width, height)))
    thin_rule_side = max(14.0, short_edge * 0.045)
    return aspect >= 8.0 and short_side <= thin_rule_side and long_side >= max(80.0, long_edge * 0.08)


def basic_layout_integrity(
    image: Image.Image,
    *,
    segments: list[dict[str, Any]] | None = None,
    occupancy: dict[str, Any] | None = None,
    ocr_status: str | None = None,
    heading_fraction: float = 0.14,
    include_debug_regions: bool = False,
) -> dict[str, Any]:
    """Image-native basic layout integrity score.

    This is deliberately narrower than semantic layout judging. It detects
    benchmark-safe production failures that can be inferred from the final
    rendered image alone: unusably low raster size, extreme poster aspect,
    unreadably tiny OCR text, content pressed against the export edge, content
    crossing detected panel/frame bounds, and high-confidence overlap damage.

    It does NOT try to prove that a source figure/table was cropped or that a
    chart's content is meaningful; those require source metadata or a VLM.
    """
    rgb = image.convert("RGB")
    w, h = rgb.size
    long_edge = max(1, max(w, h))
    short_edge = max(1, min(w, h))
    scale = long_edge / float(REFERENCE_LONG_EDGE)
    aspect = w / float(max(1, h))
    heading_y = h * heading_fraction
    edge_margin_px = max(2.0, 10.0 * scale)

    findings: list[dict[str, Any]] = []
    penalties: list[dict[str, Any]] = []

    def add_finding(
        finding_id: str,
        severity: str,
        message: str,
        penalty: float,
        evidence: dict[str, Any],
    ) -> None:
        penalties.append({"id": finding_id, "penalty": round(float(penalty), 3)})
        findings.append({
            "id": finding_id,
            "severity": severity,
            "message": message,
            "penalty": round(float(penalty), 3),
            "metric": "basic_layout_integrity",
            "category": "layout_integrity",
            "dimension": "basic_layout_integrity",
            "evidence": evidence,
        })

    # Raster viability: this is intentionally loose. A low-res final poster is a
    # basic usability problem, but many fixture previews are moderate-size rasters.
    if short_edge < 600:
        add_finding(
            "basic-layout-low-resolution",
            "P1",
            "Rendered poster raster is too low-resolution for reliable reading.",
            2.5,
            {"short_edge_px": short_edge, "threshold_px": 600, "image_size": [w, h]},
        )
    elif short_edge < 850:
        add_finding(
            "basic-layout-low-resolution",
            "P2",
            "Rendered poster raster is below the preferred benchmark reading size.",
            1.0,
            {"short_edge_px": short_edge, "preferred_min_px": 850, "image_size": [w, h]},
        )

    if aspect < 0.45 or aspect > 2.4:
        severe = aspect < 0.35 or aspect > 2.9
        add_finding(
            "basic-layout-aspect-outlier",
            "P1" if severe else "P2",
            "Rendered poster aspect ratio is outside the expected poster range.",
            2.5 if severe else 1.2,
            {
                "aspect_ratio": round(aspect, 4),
                "soft_range": [0.45, 2.4],
                "hard_range": [0.35, 2.9],
                "image_size": [w, h],
            },
        )

    ocr_text_regions = _text_regions(segments or [], scale=scale, heading_y=heading_y, min_area=max(4.0, 4.0 * scale))
    ocr_body_count = sum(1 for region in ocr_text_regions if region.get("body"))
    fallback_text_regions = (
        _raster_text_line_regions(rgb, scale=scale, heading_y=heading_y)
        if ocr_body_count < 8 else []
    )
    text_regions = _merge_text_regions(ocr_text_regions, fallback_text_regions)
    seg_rects = [region["rect"] for region in text_regions]
    body_rects = [rect for rect in seg_rects if (rect["y0"] + rect["y1"]) / 2.0 >= heading_y]
    body_heights_ref = sorted(
        rect["h"] / scale
        for rect in body_rects
        if rect["w"] >= 2 and rect["h"] >= 2
    )
    median_text_h = _median(body_heights_ref)
    small_text_fraction = (
        sum(1 for height in body_heights_ref if height < 7.5) / len(body_heights_ref)
        if body_heights_ref else None
    )
    if len(body_heights_ref) >= 8 and median_text_h is not None and small_text_fraction is not None:
        if median_text_h < 6.5 or small_text_fraction >= 0.55:
            add_finding(
                "basic-layout-text-too-small",
                "P1",
                "Most detected body text is below the image-native readability floor.",
                2.0,
                {
                    "median_body_text_height_ref_px": round(median_text_h, 2),
                    "small_text_fraction": round(small_text_fraction, 3),
                    "reference_long_edge_px": REFERENCE_LONG_EDGE,
                    "segment_count": len(body_heights_ref),
                },
            )
        elif median_text_h < 8.0 or small_text_fraction >= 0.35:
            add_finding(
                "basic-layout-text-too-small",
                "P2",
                "A substantial share of detected body text is close to the readability floor.",
                1.0,
                {
                    "median_body_text_height_ref_px": round(median_text_h, 2),
                    "small_text_fraction": round(small_text_fraction, 3),
                    "reference_long_edge_px": REFERENCE_LONG_EDGE,
                    "segment_count": len(body_heights_ref),
                },
            )

    edge_body = [
        rect for rect in body_rects
        if rect["x0"] <= edge_margin_px
        or rect["x1"] >= w - edge_margin_px
        or rect["y1"] >= h - edge_margin_px
    ]
    body_text_area = sum(max(0.0, rect["w"]) * max(0.0, rect["h"]) for rect in body_rects)
    edge_text_area = sum(max(0.0, rect["w"]) * max(0.0, rect["h"]) for rect in edge_body)
    edge_text_area_ratio = edge_text_area / body_text_area if body_text_area > 0 else None
    edge_text_segment_ratio = len(edge_body) / len(body_rects) if body_rects else None
    if len(body_rects) >= 6 and edge_text_area_ratio is not None and edge_text_segment_ratio is not None:
        if (edge_text_area_ratio >= 0.16 or edge_text_segment_ratio >= 0.18) and len(edge_body) >= 4:
            add_finding(
                "basic-layout-text-on-export-edge",
                "P1",
                "Detected body text is pressed against the rendered poster edge.",
                2.0,
                {
                    "edge_text_area_ratio": round(edge_text_area_ratio, 4),
                    "edge_text_segment_ratio": round(edge_text_segment_ratio, 4),
                    "edge_segment_count": len(edge_body),
                    "body_segment_count": len(body_rects),
                    "edge_margin_ref_px": 10.0,
                },
            )
        elif (edge_text_area_ratio >= 0.08 or edge_text_segment_ratio >= 0.10) and len(edge_body) >= 3:
            add_finding(
                "basic-layout-text-on-export-edge",
                "P2",
                "Some detected body text is unusually close to the rendered poster edge.",
                0.9,
                {
                    "edge_text_area_ratio": round(edge_text_area_ratio, 4),
                    "edge_text_segment_ratio": round(edge_text_segment_ratio, 4),
                    "edge_segment_count": len(edge_body),
                    "body_segment_count": len(body_rects),
                    "edge_margin_ref_px": 10.0,
                },
            )

    edge_occ = _edge_occupancy_ratio(occupancy)
    edge_by_side = _edge_occupancy_by_side(occupancy)
    if edge_by_side and edge_by_side.get("edge_cell_count", 0) >= 10:
        severe_sides = edge_by_side.get("severe_sides") or []
        warning_sides = edge_by_side.get("warning_sides") or []
        if severe_sides or len(warning_sides) >= 2:
            sev = "P1" if severe_sides or len(warning_sides) >= 3 else "P2"
            add_finding(
                "basic-layout-content-on-export-edge",
                sev,
                "Structured content occupies much of the outer safety edge.",
                1.4 if sev == "P1" else 0.8,
                edge_by_side,
            )
    elif edge_occ and edge_occ["edge_cell_count"] >= 10:
        if edge_occ["edge_occupied_ratio"] >= 0.78:
            add_finding(
                "basic-layout-content-on-export-edge",
                "P2",
                "Structured content occupies much of the outer safety edge.",
                0.8,
                edge_occ,
            )

    cv = _cv_layout_regions(rgb, text_regions=text_regions, heading_y=heading_y)
    panel_regions = cv.get("panels", [])
    visual_regions = cv.get("visuals", [])
    heading_visual_regions = cv.get("heading_visuals", [])
    inferred_sections = _infer_open_grid_sections(
        text_regions=text_regions,
        visual_regions=visual_regions,
        occupancy=occupancy,
        width=w,
        height=h,
        heading_y=heading_y,
        long_edge=long_edge,
    )
    credible_panel_regions = [
        panel for panel in panel_regions
        if _is_credible_closed_boundary(panel)
    ]
    effective_sections = (
        credible_panel_regions
        if len(credible_panel_regions) >= 2
        else (inferred_sections or credible_panel_regions)
    )
    heading_metrics = _audit_heading_integrity(
        text_regions=text_regions,
        heading_visual_regions=heading_visual_regions,
        heading_dividers=cv.get("heading_dividers", []),
        width=w,
        inset=edge_margin_px,
        heading_y=heading_y,
        long_edge=long_edge,
    )
    for item in heading_metrics.get("findings", []):
        add_finding(
            item["id"],
            item["severity"],
            item["message"],
            item["penalty"],
            item["evidence"],
        )
    canvas_metrics = _audit_canvas_overflow(
        text_regions=text_regions,
        visual_regions=visual_regions,
        width=w,
        height=h,
        inset=edge_margin_px,
        heading_y=heading_y,
    )
    if canvas_metrics["finding"]:
        sev = "P1" if canvas_metrics["max_true_edge_count"] >= 2 else "P2"
        add_finding(
            "basic-layout-canvas-overflow",
            sev,
            "Rendered content appears clipped or overflowing at the poster canvas edge.",
            1.8 if sev == "P1" else 0.8,
            canvas_metrics,
        )
    bottom_metrics = _audit_bottom_truncation(
        text_regions=text_regions,
        visual_regions=visual_regions,
        occupancy=occupancy,
        width=w,
        height=h,
        inset=edge_margin_px,
        heading_y=heading_y,
    )
    if bottom_metrics.get("finding"):
        sev = str(bottom_metrics.get("severity") or "P2")
        add_finding(
            "basic-layout-bottom-truncation",
            sev,
            "Rendered content appears cut off at the bottom edge.",
            1.8 if sev == "P1" else 0.9,
            bottom_metrics,
        )

    panel_metrics = _audit_panel_bounds(
        text_regions=text_regions,
        visual_regions=visual_regions,
        panel_regions=panel_regions,
        canvas_width=w,
        long_edge=long_edge,
        canvas_height=h,
    )
    for item in panel_metrics.get("findings", []):
        add_finding(
            item["id"],
            item["severity"],
            item["message"],
            item["penalty"],
            item["evidence"],
        )
    section_metrics = _audit_section_bounds(
        text_regions=text_regions,
        visual_regions=visual_regions,
        section_regions=effective_sections,
        panel_region_count=len(panel_regions),
        long_edge=long_edge,
        canvas_width=w,
        canvas_height=h,
        heading_y=heading_y,
    )
    for item in section_metrics.get("findings", []):
        add_finding(
            item["id"],
            item["severity"],
            item["message"],
            item["penalty"],
            item["evidence"],
        )
    crop_metrics = _audit_visual_crop_damage(
        visual_regions=visual_regions,
        section_regions=effective_sections,
        width=w,
        height=h,
        long_edge=long_edge,
        visual_mask=cv.get("visual_mask"),
    )
    for item in crop_metrics.get("findings", []):
        add_finding(
            item["id"],
            item["severity"],
            item["message"],
            item["penalty"],
            item["evidence"],
        )

    empty_visual_metrics = _audit_empty_visual_placeholders(
        image=rgb,
        panel_regions=panel_regions,
        heading_y=heading_y,
    )
    for item in empty_visual_metrics.get("findings", []):
        add_finding(
            item["id"],
            item["severity"],
            item["message"],
            item["penalty"],
            item["evidence"],
        )

    multi_crop_metrics = _audit_multi_panel_crop_failure(
        canvas_metrics=canvas_metrics,
        bottom_metrics=bottom_metrics,
        crop_metrics=crop_metrics,
    )
    for item in multi_crop_metrics.get("findings", []):
        add_finding(
            item["id"],
            item["severity"],
            item["message"],
            item["penalty"],
            item["evidence"],
        )

    overlap_metrics = _audit_overlaps(
        text_regions=text_regions,
        visual_regions=visual_regions,
        panel_regions=panel_regions,
        visual_mask=cv.get("visual_mask"),
    )
    for item in overlap_metrics.get("findings", []):
        add_finding(
            item["id"],
            item["severity"],
            item["message"],
            item["penalty"],
            item["evidence"],
        )

    detector_coverage = _detector_coverage(
        ocr_status=ocr_status,
        ocr_body_count=ocr_body_count,
        fallback_text_count=len(fallback_text_regions),
        effective_text_count=len(text_regions),
        cv_available=bool(cv.get("available")),
        panel_count=len(panel_regions),
        visual_count=len(visual_regions),
        effective_section_count=len(effective_sections),
        occupancy=occupancy,
    )
    if detector_coverage.get("blind"):
        add_finding(
            "basic-layout-detector-blind",
            "P2",
            "Layout detector had no usable text, panel, or visual regions despite structured image content.",
            0.6,
            detector_coverage,
        )

    penalty_total = round(sum(item["penalty"] for item in _dedupe_penalties(penalties)), 3)
    score = round(max(0.0, min(10.0, 10.0 - penalty_total)), 2)
    p1_count = sum(1 for finding in findings if finding["severity"] == "P1")
    p2_count = sum(1 for finding in findings if finding["severity"] == "P2")
    only_blind = bool(findings) and all(finding["id"] == "basic-layout-detector-blind" for finding in findings)
    status = "degraded" if only_blind or (not findings and detector_coverage.get("reduced_confidence_reasons")) else ("warning" if findings else "ok")
    payload = {
        "available": True,
        # Raw per-pixel visual-content mask (non-background pixels: figures, charts,
        # tables, colored elements). Non-serializable numpy array — the caller uses
        # it to credit figure footprints in density/void scoring, then drops it
        # before the bundle is written. Underscore marks it as an internal handle.
        "_visual_mask": cv.get("visual_mask"),
        "score_0_10": score,
        "status": status,
        "width": w,
        "height": h,
        "aspect_ratio": round(aspect, 4),
        "short_edge_px": short_edge,
        "long_edge_px": long_edge,
        "reference_long_edge_px": REFERENCE_LONG_EDGE,
        "ocr_body_segment_count": ocr_body_count,
        "fallback_text_region_count": len(fallback_text_regions),
        "effective_text_region_count": len(body_rects),
        "median_body_text_height_ref_px": round(median_text_h, 2) if median_text_h is not None else None,
        "small_text_fraction": round(small_text_fraction, 4) if small_text_fraction is not None else None,
        "edge_text_area_ratio": round(edge_text_area_ratio, 4) if edge_text_area_ratio is not None else None,
        "edge_text_segment_ratio": round(edge_text_segment_ratio, 4) if edge_text_segment_ratio is not None else None,
        "edge_occupancy": edge_occ,
        "edge_occupancy_by_side": edge_by_side,
        "panel_count": len(panel_regions),
        "inferred_section_count": len(inferred_sections),
        "effective_section_count": len(effective_sections),
        "content_region_count": len(visual_regions),
        # Figure/chart/screenshot/logo region rects {x0,y0,x1,y1,w,h}. Numeric
        # grounding uses these to EXCLUDE numbers rendered inside figures (paper
        # reproductions, not the poster's own claims) from fabrication checking.
        "visual_region_rects": [
            r["rect"] for r in (visual_regions + heading_visual_regions)
            if isinstance(r.get("rect"), dict)
        ],
        # Figure-shaped BODY regions only (header bars / rules / specks filtered out),
        # each with its canvas area ratio. Drives the visual_evidence_use signals; CV
        # is threshold-based so this still misses sparse line-charts and text tables.
        "figure_region_rects": [
            {
                **r["rect"],
                "area_ratio": round(_rect_area(r["rect"]) / float(max(1, w * h)), 4),
                "canvas_width": w,
                "canvas_height": h,
            }
            for r in visual_regions
            if isinstance(r.get("rect"), dict) and is_figure_shaped(r["rect"], short_edge, float(w * h))
        ],
        "heading_integrity": {k: v for k, v in heading_metrics.items() if k != "findings" and k != "samples"},
        "canvas_overflow": {k: v for k, v in canvas_metrics.items() if k != "samples"},
        "bottom_truncation": {k: v for k, v in bottom_metrics.items() if k != "samples"},
        "panel_overflow": {k: v for k, v in panel_metrics.items() if k != "findings" and k != "samples"},
        "section_bounds": {k: v for k, v in section_metrics.items() if k != "findings" and k != "samples"},
        "visual_crop_damage": {k: v for k, v in crop_metrics.items() if k != "findings" and k != "samples"},
        "empty_visual_placeholders": {k: v for k, v in empty_visual_metrics.items() if k != "findings" and k != "samples"},
        "multi_panel_crop_failure": {k: v for k, v in multi_crop_metrics.items() if k != "findings"},
        "overlap": {k: v for k, v in overlap_metrics.items() if k != "findings" and k != "samples"},
        "detector_coverage": detector_coverage,
        "penalty_total": penalty_total,
        "penalties": _dedupe_penalties(penalties),
        "p1_count": p1_count,
        "p2_count": p2_count,
        "findings_count": len(findings),
        "findings": findings,
    }
    if include_debug_regions:
        payload["debug_regions"] = {
            "panels": [_debug_region(region) for region in panel_regions],
            "inferred_sections": [_debug_region(region) for region in inferred_sections],
            "effective_sections": [_debug_region(region) for region in effective_sections],
            "text": [_debug_region(region) for region in text_regions],
            "fallback_text": [_debug_region(region) for region in fallback_text_regions],
            "visuals": [_debug_region(region) for region in visual_regions],
            "heading_visuals": [_debug_region(region) for region in heading_visual_regions],
            "heading_samples": heading_metrics.get("samples", []),
            "canvas_overflow_samples": canvas_metrics.get("samples", []),
            "bottom_truncation_samples": bottom_metrics.get("samples", []),
            "panel_overflow_samples": panel_metrics.get("samples", []),
            "section_samples": section_metrics.get("samples", []),
            "visual_crop_samples": crop_metrics.get("samples", []),
            "empty_visual_placeholder_samples": empty_visual_metrics.get("samples", []),
            "overlap_samples": overlap_metrics.get("samples", []),
        }
    return payload


def _text_regions(
    segments: list[dict[str, Any]],
    *,
    scale: float,
    heading_y: float,
    min_area: float,
) -> list[dict[str, Any]]:
    regions: list[dict[str, Any]] = []
    for i, seg in enumerate(segments):
        rect = _segment_rect(seg)
        if rect is None or rect["w"] * rect["h"] < min_area:
            continue
        try:
            score = float(seg.get("score")) if seg.get("score") is not None else None
        except (TypeError, ValueError):
            score = None
        if score is not None and score < 0.25:
            continue
        region = {
            "id": f"text-{i+1}",
            "kind": "text",
            "rect": rect,
            "area": round(rect["w"] * rect["h"], 2),
            "text": str(seg.get("text") or "")[:120],
            "score": score,
            "height_ref_px": round(rect["h"] / max(1e-6, scale), 2),
            "body": (rect["y0"] + rect["y1"]) / 2.0 >= heading_y,
        }
        regions.append(region)
    return regions


def _raster_text_line_regions(
    image: Image.Image,
    *,
    scale: float,
    heading_y: float,
) -> list[dict[str, Any]]:
    """OCR-free text-line candidates from image structure.

    This fallback is deliberately conservative: it groups dark structured pixels
    into horizontal line boxes so edge/overflow checks still have primitives when
    OCR is absent. It is not used as recognized text.
    """
    try:
        import numpy as np
    except Exception:  # noqa: BLE001 - numpy should be present but keep graceful degradation.
        return []

    gray = np.asarray(image.convert("L"))
    if gray.ndim != 2 or gray.size == 0:
        return []
    h, w = gray.shape[:2]
    long_edge = max(w, h)
    dark = gray < 215
    rows: list[dict[str, float]] = []
    min_row_pixels = max(10, int(w * 0.006))
    row_has_ink = dark.sum(axis=1) >= min_row_pixels
    clusters = _index_clusters([int(i) for i, value in enumerate(row_has_ink) if bool(value)], gap=max(1, int(2 * scale)))
    min_h = max(3.0, 3.5 * scale)
    max_h = max(28.0, 34.0 * scale)
    min_w = max(26.0, 0.018 * float(w))
    for cluster in clusters:
        y0 = max(0, cluster[0] - int(1 * scale))
        y1 = min(h, cluster[-1] + int(2 * scale) + 1)
        height = float(y1 - y0)
        if height < min_h or height > max_h:
            continue
        cols = dark[y0:y1, :].sum(axis=0)
        active_cols = [int(i) for i, value in enumerate(cols) if int(value) >= max(1, int(height * 0.12))]
        if not active_cols:
            continue
        for x_cluster in _index_clusters(active_cols, gap=max(3, int(0.012 * long_edge))):
            x0 = max(0, x_cluster[0] - int(2 * scale))
            x1 = min(w, x_cluster[-1] + int(2 * scale) + 1)
            width = float(x1 - x0)
            if width < min_w:
                continue
            fill = float(dark[y0:y1, x0:x1].mean())
            aspect = width / max(1.0, height)
            if aspect < 2.8 or fill < 0.012 or fill > 0.55:
                continue
            rows.append({
                "x0": float(x0),
                "y0": float(y0),
                "x1": float(x1),
                "y1": float(y1),
                "w": width,
                "h": height,
            })
    regions = [
        {
            "id": f"fallback-text-{i+1}",
            "kind": "fallback-text",
            "rect": rect,
            "area": round(rect["w"] * rect["h"], 2),
            "text": "",
            "score": None,
            "source": "raster_text_line",
            "height_ref_px": round(rect["h"] / max(1e-6, scale), 2),
            "body": (rect["y0"] + rect["y1"]) / 2.0 >= heading_y,
        }
        for i, rect in enumerate(_dedupe_rects(rows, iou_threshold=0.72)[:450])
    ]
    return regions


def _merge_text_regions(ocr_regions: list[dict[str, Any]], fallback_regions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not fallback_regions:
        return list(ocr_regions)
    merged = list(ocr_regions)
    for fallback in fallback_regions:
        rect = fallback["rect"]
        if any(_intersection_area(rect, region["rect"]) / max(1.0, _rect_area(rect)) >= 0.62 for region in ocr_regions):
            continue
        merged.append(fallback)
    merged.sort(key=lambda r: (r["rect"]["y0"], r["rect"]["x0"]))
    return merged


def _cv_layout_regions(
    image: Image.Image,
    *,
    text_regions: list[dict[str, Any]],
    heading_y: float,
) -> dict[str, Any]:
    try:
        import cv2  # type: ignore
        import numpy as np
    except Exception:  # noqa: BLE001 - OpenCV/numpy are optional at package level.
        return {"panels": [], "visuals": [], "visual_mask": None, "available": False}

    rgb = image.convert("RGB")
    arr = np.asarray(rgb)
    h, w = arr.shape[:2]
    long_edge = max(w, h)
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    sat = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)[:, :, 1]

    text_mask = np.zeros((h, w), dtype=np.uint8)
    pad = max(1, round(long_edge * 0.0015))
    for region in text_regions:
        r = region["rect"]
        cv2.rectangle(
            text_mask,
            (max(0, int(r["x0"] - pad)), max(0, int(r["y0"] - pad))),
            (min(w - 1, int(r["x1"] + pad)), min(h - 1, int(r["y1"] + pad))),
            255,
            thickness=-1,
        )

    edges = cv2.Canny(gray, 50, 150)
    k = max(3, int(round(long_edge / 700)))
    kernel = np.ones((k, k), dtype=np.uint8)
    edge_mask = cv2.dilate(edges, kernel, iterations=1)

    # Background luma = the single most frequent luma (histogram mode), NOT the
    # median of the top/bottom edge rows. 2026-07-03: edge-band estimation broke on
    # posters whose header/footer is a full-width dark band running to the poster
    # edge — the border sampled the dark band (bg_luma~140) while the real body
    # background is white (~255), so the entire white body read as "non-background"
    # (non_bg ~0.97), the visual mask filled the whole canvas, and figure detection
    # returned ZERO discrete figures (greening/gravitational/planck: big LAI/data
    # maps undetected -> density scored them as void). The luma mode is the dominant
    # flat background regardless of edge bands (white body -> 255, dark poster ->
    # dark), so figures stand out correctly. Fall back to the median if degenerate.
    hist = np.bincount(gray.reshape(-1), minlength=256)
    bg_luma = float(int(hist.argmax())) if hist.sum() > 0 else float(np.median(gray))
    luma_diff = np.abs(gray.astype("int16") - int(bg_luma))
    non_bg = ((luma_diff > 38) | (sat > 45)).astype("uint8") * 255

    raw_visual_mask = cv2.morphologyEx(
        cv2.bitwise_or(edge_mask, non_bg),
        cv2.MORPH_CLOSE,
        np.ones((max(3, k * 2), max(3, k * 2)), dtype=np.uint8),
        iterations=1,
    )
    heading_visual_mask = raw_visual_mask.copy()
    heading_visual_mask[text_mask > 0] = 0
    heading_visual_mask[int(heading_y):, :] = 0
    visual_mask = raw_visual_mask.copy()
    visual_mask[text_mask > 0] = 0
    visual_mask[: int(heading_y), :] = 0
    panels = _detect_panel_regions_cv(gray, edges, heading_y=heading_y)
    heading_dividers = _detect_heading_dividers(edges, heading_y=heading_y)
    frame_thickness = max(4, k * 3)
    for panel in panels:
        rect = panel["rect"]
        cv2.rectangle(
            visual_mask,
            (max(0, int(rect["x0"])), max(0, int(rect["y0"]))),
            (min(w - 1, int(rect["x1"])), min(h - 1, int(rect["y1"]))),
            0,
            thickness=frame_thickness,
        )
    visual_mask = cv2.morphologyEx(visual_mask, cv2.MORPH_OPEN, kernel, iterations=1)

    visuals = _connected_regions_from_mask(
        visual_mask,
        prefix="visual",
        min_area=max(80, int(w * h * 0.0009)),
        max_area=int(w * h * 0.55),
        min_side=max(10, int(long_edge * 0.01)),
    )
    visuals = _merge_nearby_regions(visuals, gap_px=max(8.0, frame_thickness * 2.0))
    visuals = [
        region for region in visuals
        if not _is_panel_sized_component(region["rect"], panels)
    ]
    canvas_area = float(max(1, w * h))
    # 2026-07-03: raised the single-figure area cap 0.28 -> 0.45. Genuinely large
    # source figures (full-column data/LAI maps, wide result panels) exceed 28% of
    # the canvas and were being dropped as "too big", so density then scored the
    # space they fill as void. The full-width-band guard below still rejects a
    # whole column/panel masquerading as one figure.
    visuals = [
        region for region in visuals
        if _rect_area(region["rect"]) / canvas_area <= 0.45
        and not (region["rect"]["w"] >= 0.78 * w and region["rect"]["h"] >= 0.5 * h)
    ]
    heading_visuals = _connected_regions_from_mask(
        heading_visual_mask,
        prefix="heading-visual",
        min_area=max(70, int(w * h * 0.00035)),
        max_area=int(w * h * 0.18),
        min_side=max(8, int(long_edge * 0.006)),
    )
    heading_visuals = _merge_nearby_regions(heading_visuals, gap_px=max(6.0, frame_thickness * 1.5))
    heading_visuals = [
        region for region in heading_visuals
        if not _is_heading_band_region(region["rect"], width=w, heading_y=heading_y, pad=max(4.0, float(frame_thickness)))
    ]
    return {
        "panels": panels,
        "visuals": visuals[:80],
        "heading_visuals": heading_visuals[:30],
        "heading_dividers": heading_dividers,
        "visual_mask": visual_mask,
        "available": True,
    }


def _detect_panel_regions_cv(gray: Any, edges: Any, *, heading_y: float) -> list[dict[str, Any]]:
    import cv2  # type: ignore
    import numpy as np

    h, w = gray.shape[:2]
    long_edge = max(w, h)
    kernel = np.ones((max(3, round(long_edge / 420)), max(3, round(long_edge / 420))), dtype=np.uint8)
    panel_edges = cv2.dilate(edges, kernel, iterations=1)
    contours, _hier = cv2.findContours(panel_edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    candidates: list[dict[str, Any]] = []
    canvas_area = float(max(1, w * h))
    for contour in contours:
        x, y, bw, bh = cv2.boundingRect(contour)
        x, y, bw, bh = _refine_panel_rect_from_edges(edges, x, y, bw, bh)
        if bw < max(80, long_edge * 0.045) or bh < max(55, long_edge * 0.035):
            continue
        area = bw * bh
        area_ratio = area / canvas_area
        if area_ratio < 0.012 or area_ratio > 0.92:
            continue
        if y + bh < heading_y:
            continue
        rect = {"x0": float(x), "y0": float(y), "x1": float(x + bw), "y1": float(y + bh), "w": float(bw), "h": float(bh)}
        contour_area = float(cv2.contourArea(contour))
        rectangularity = contour_area / max(1.0, float(area))
        # Keep both bordered panels and filled cards; reject tiny text contours.
        if rectangularity < 0.08 and area_ratio < 0.04:
            continue
        candidates.append({
            "id": f"panel-{len(candidates)+1}",
            "kind": "panel",
            "rect": rect,
            "area": round(float(area), 2),
            "confidence": round(min(1.0, max(0.25, rectangularity)), 3),
        })
    candidates = _dedupe_regions(candidates, iou_threshold=0.86)
    # Drop panels that are just the whole poster body frame when smaller panels exist.
    if len(candidates) > 1:
        candidates = [
            c for c in candidates
            if _rect_area(c["rect"]) / canvas_area < 0.72
            or not any(_rect_area(o["rect"]) < _rect_area(c["rect"]) * 0.6 for o in candidates if o is not c)
        ]
    for i, region in enumerate(candidates):
        region["id"] = f"panel-{i+1}"
    return candidates[:60]


def _detect_heading_dividers(edges: Any, *, heading_y: float) -> list[dict[str, Any]]:
    import numpy as np

    h, w = edges.shape[:2]
    search_h = max(1, min(h, int(heading_y)))
    counts = (edges[:search_h, :] > 0).sum(axis=1)
    threshold = max(80, int(w * 0.35))
    rows = [int(i) for i, count in enumerate(counts) if int(count) >= threshold]
    clusters = _index_clusters(rows, gap=2)
    dividers: list[dict[str, Any]] = []
    for cluster in clusters:
        weights = [int(counts[y]) for y in cluster]
        total = max(1, sum(weights))
        y = sum(float(row) * weight for row, weight in zip(cluster, weights, strict=False)) / total
        if y < max(8.0, 0.12 * heading_y):
            continue
        dividers.append({
            "y": round(y, 2),
            "row_start": cluster[0],
            "row_end": cluster[-1],
            "edge_px": max(weights),
        })
    return dividers[:12]


def _audit_heading_integrity(
    *,
    text_regions: list[dict[str, Any]],
    heading_visual_regions: list[dict[str, Any]],
    heading_dividers: list[dict[str, Any]],
    width: int,
    inset: float,
    heading_y: float,
    long_edge: int,
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    header_text = [r for r in text_regions if not r.get("body")]
    min_region_area = max(16.0, (0.004 * float(long_edge)) ** 2)
    header_regions = [
        region for region in header_text + heading_visual_regions
        if _rect_area(region["rect"]) >= min_region_area and region["rect"]["y0"] <= heading_y
    ]
    side_counts = {"left": 0, "right": 0, "top": 0}
    true_edge_counts = {"left": 0, "right": 0, "top": 0}
    edge_samples: list[dict[str, Any]] = []
    for region in header_regions:
        rect = region["rect"]
        if _is_heading_band_region(rect, width=width, heading_y=heading_y, pad=inset):
            continue
        touches = {
            "left": rect["x0"] <= inset,
            "right": rect["x1"] >= width - inset,
            "top": rect["y0"] <= inset,
        }
        true_touches = {
            "left": rect["x0"] <= 1.5,
            "right": rect["x1"] >= width - 1.5,
            "top": rect["y0"] <= 1.5,
        }
        hit = False
        for side, value in touches.items():
            if value:
                side_counts[side] += 1
                hit = True
        for side, value in true_touches.items():
            if value:
                true_edge_counts[side] += 1
        if hit and len(edge_samples) < 6:
            edge_samples.append({
                "id": region.get("id"),
                "kind": region.get("kind"),
                "rect": _round_rect_f(rect),
                "sides": [s for s, v in touches.items() if v],
                "true_sides": [s for s, v in true_touches.items() if v],
            })

    true_total = sum(true_edge_counts.values())
    if true_total:
        sev = "P1" if true_total >= 3 else "P2"
        findings.append({
            "id": "basic-layout-heading-canvas-overflow",
            "severity": sev,
            "message": "Detected heading content clipped or pressed against the poster canvas edge.",
            "penalty": 1.5 if sev == "P1" else 0.8,
            "evidence": {
                "true_edge_counts": true_edge_counts,
                "side_region_counts": side_counts,
                "inset_px": round(inset, 2),
                "boundary_source": "canvas",
                "trusted_p1": sev == "P1",
                "trusted_sources": ["canvas"] if sev == "P1" else [],
                "confidence": "high" if sev == "P1" else "medium",
                "samples": edge_samples[:5],
            },
        })
        samples.extend(edge_samples[:5])

    text_overlap = _text_overlap_samples(header_text)
    if text_overlap:
        sev = "P1" if len(text_overlap) >= 2 else "P2"
        findings.append({
            "id": "basic-layout-heading-text-overlap",
            "severity": sev,
            "message": "Detected overlapping OCR text boxes in the poster heading.",
            "penalty": 1.5 if sev == "P1" else 0.8,
            "evidence": {"count": len(text_overlap), "samples": text_overlap[:5]},
        })
        samples.extend(text_overlap[:5])

    divider_over = _heading_divider_overlap_samples(header_text, heading_dividers)
    if divider_over:
        findings.append({
            "id": "basic-layout-heading-panel-overflow",
            "severity": "P2",
            "message": "Detected heading text crossing a header panel/frame divider.",
            "penalty": 0.8,
            "evidence": {"count": len(divider_over), "boundary_source": "closed_frame", "trusted_p1": False, "samples": divider_over[:5]},
        })
        samples.extend(divider_over[:5])

    return {
        "text_region_count": len(header_text),
        "visual_region_count": len(heading_visual_regions),
        "edge_region_counts": side_counts,
        "true_edge_counts": true_edge_counts,
        "text_overlap_count": len(text_overlap),
        "divider_overlap_count": len(divider_over),
        "finding_count": len(findings),
        "findings": findings,
        "samples": samples[:10],
    }


def _heading_divider_overlap_samples(text_regions: list[dict[str, Any]], dividers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for text in text_regions:
        rect = text["rect"]
        if rect["w"] < max(80.0, rect["h"] * 4.0) or rect["h"] < 6.0:
            continue
        for divider in dividers:
            y = float(divider.get("y") or 0.0)
            if not (rect["y0"] < y < rect["y1"]):
                continue
            below_fraction = (rect["y1"] - y) / max(1.0, rect["h"])
            if below_fraction < 0.18:
                continue
            samples.append({
                "id": text.get("id"),
                "kind": "heading-text",
                "rect": _round_rect_f(rect),
                "divider_y": round(y, 2),
                "below_fraction": round(below_fraction, 3),
            })
            break
        if len(samples) >= 8:
            break
    return samples


def _is_heading_band_region(rect: dict[str, float], *, width: int, heading_y: float, pad: float) -> bool:
    x_pad = max(pad, 0.02 * float(width))
    y_pad = max(pad, 0.12 * heading_y)
    return (
        rect["w"] >= 0.72 * float(width)
        and rect["x0"] <= x_pad
        and rect["x1"] >= float(width) - x_pad
        and rect["y0"] <= y_pad
        and rect["y1"] <= heading_y + max(pad, 0.08 * heading_y)
    )


def _refine_panel_rect_from_edges(edges: Any, x: int, y: int, w: int, h: int) -> tuple[int, int, int, int]:
    """Recover true frame bounds when a contour bbox swallowed nearby content."""
    if w <= 0 or h <= 0:
        return x, y, w, h
    roi = edges[y:y + h, x:x + w]
    if roi.size == 0:
        return x, y, w, h
    mask = roi > 0
    v_counts = mask.sum(axis=0)
    h_counts = mask.sum(axis=1)
    v_threshold = max(24, int(round(h * 0.35)))
    h_threshold = max(24, int(round(w * 0.35)))
    v_clusters = _index_clusters([int(i) for i, count in enumerate(v_counts) if int(count) >= v_threshold])
    h_clusters = _index_clusters([int(i) for i, count in enumerate(h_counts) if int(count) >= h_threshold])
    if len(v_clusters) < 2 or len(h_clusters) < 2:
        return x, y, w, h
    left = v_clusters[0][0]
    right = v_clusters[-1][-1] + 1
    top = h_clusters[0][0]
    bottom = h_clusters[-1][-1] + 1
    refined_w = right - left
    refined_h = bottom - top
    if refined_w < max(60, w * 0.45) or refined_h < max(45, h * 0.45):
        return x, y, w, h
    return x + left, y + top, refined_w, refined_h


def _index_clusters(indices: list[int], *, gap: int = 3) -> list[list[int]]:
    if not indices:
        return []
    clusters = [[indices[0]]]
    for idx in indices[1:]:
        if idx - clusters[-1][-1] <= gap:
            clusters[-1].append(idx)
        else:
            clusters.append([idx])
    return clusters


def _connected_regions_from_mask(
    mask: Any,
    *,
    prefix: str,
    min_area: int,
    max_area: int,
    min_side: int,
) -> list[dict[str, Any]]:
    import cv2  # type: ignore

    count, labels, stats, _centroids = cv2.connectedComponentsWithStats((mask > 0).astype("uint8"), 8)
    regions: list[dict[str, Any]] = []
    for idx in range(1, count):
        x, y, w, h, area = stats[idx]
        if area < min_area or area > max_area:
            continue
        if w < min_side and h < min_side:
            continue
        rect = {"x0": float(x), "y0": float(y), "x1": float(x + w), "y1": float(y + h), "w": float(w), "h": float(h)}
        regions.append({
            "id": f"{prefix}-{len(regions)+1}",
            "kind": prefix,
            "rect": rect,
            "area": round(float(area), 2),
            "bbox_area": round(float(w * h), 2),
        })
    regions.sort(key=lambda item: item["area"], reverse=True)
    return regions


def _merge_nearby_regions(regions: list[dict[str, Any]], *, gap_px: float) -> list[dict[str, Any]]:
    merged = [dict(region) for region in regions]
    changed = True
    while changed:
        changed = False
        next_regions: list[dict[str, Any]] = []
        used: set[int] = set()
        for i, region in enumerate(merged):
            if i in used:
                continue
            current = dict(region)
            for j in range(i + 1, len(merged)):
                if j in used:
                    continue
                other = merged[j]
                if not _regions_should_merge(current["rect"], other["rect"], gap_px=gap_px):
                    continue
                rect = _union_rect(current["rect"], other["rect"])
                current = {
                    **current,
                    "rect": rect,
                    "area": round(float(current.get("area") or 0.0) + float(other.get("area") or 0.0), 2),
                    "bbox_area": round(_rect_area(rect), 2),
                }
                used.add(j)
                changed = True
            used.add(i)
            next_regions.append(current)
        merged = next_regions
    merged.sort(key=lambda item: item.get("area", 0), reverse=True)
    for i, region in enumerate(merged):
        region["id"] = f"{region.get('kind') or 'region'}-{i+1}"
    return merged


def _regions_should_merge(a: dict[str, float], b: dict[str, float], *, gap_px: float) -> bool:
    x_gap = max(0.0, max(a["x0"], b["x0"]) - min(a["x1"], b["x1"]))
    y_gap = max(0.0, max(a["y0"], b["y0"]) - min(a["y1"], b["y1"]))
    x_overlap = max(0.0, min(a["x1"], b["x1"]) - max(a["x0"], b["x0"]))
    y_overlap = max(0.0, min(a["y1"], b["y1"]) - max(a["y0"], b["y0"]))
    min_w = max(1.0, min(a["w"], b["w"]))
    min_h = max(1.0, min(a["h"], b["h"]))
    return (
        x_gap <= gap_px and y_overlap / min_h >= 0.35
    ) or (
        y_gap <= gap_px and x_overlap / min_w >= 0.35
    )


def _audit_canvas_overflow(
    *,
    text_regions: list[dict[str, Any]],
    visual_regions: list[dict[str, Any]],
    width: int,
    height: int,
    inset: float,
    heading_y: float,
) -> dict[str, Any]:
    side_counts = {"left": 0, "right": 0, "top": 0, "bottom": 0}
    true_edge_counts = {"left": 0, "right": 0, "top": 0, "bottom": 0}
    samples: list[dict[str, Any]] = []
    long_edge = max(width, height)
    visual_candidates = _layout_damage_visual_regions(visual_regions, width=width, height=height, long_edge=long_edge)
    regions = list(text_regions) + visual_candidates
    true_top_heading_count = 0
    for region in regions:
        rect = region["rect"]
        is_heading_text = (
            not region.get("body")
            and "text" in str(region.get("kind") or "")
        )
        heading_text_clipped = is_heading_text and rect["y0"] <= 1.5
        if rect["y1"] < heading_y and not heading_text_clipped:
            continue
        if heading_text_clipped:
            true_top_heading_count += 1
        horizontal_full_bleed = (
            not region.get("body")
            and rect["x0"] <= 1.5
            and rect["x1"] >= width - 1.5
            and rect["w"] >= width * 0.80
            and rect["y1"] < height - inset
        )
        touches = {
            "left": rect["x0"] <= inset and not horizontal_full_bleed,
            "right": rect["x1"] >= width - inset and not horizontal_full_bleed,
            "top": rect["y0"] <= inset,
            "bottom": rect["y1"] >= height - inset,
        }
        true_touches = {
            "left": rect["x0"] <= 1.5 and not horizontal_full_bleed,
            "right": rect["x1"] >= width - 1.5 and not horizontal_full_bleed,
            "top": rect["y0"] <= 1.5,
            "bottom": rect["y1"] >= height - 1.5,
        }
        hit = False
        for side, value in touches.items():
            if value:
                side_counts[side] += 1
                hit = True
        for side, value in true_touches.items():
            if value:
                true_edge_counts[side] += 1
        if hit and len(samples) < 8:
            samples.append({"id": region.get("id"), "kind": region.get("kind"), "rect": _round_rect_f(rect), "sides": [s for s, v in touches.items() if v]})
    max_true = max(true_edge_counts.values()) if true_edge_counts else 0
    finding = (
        true_top_heading_count >= 1
        or (max_true >= 1 and sum(true_edge_counts.values()) >= 2)
    )
    trusted_p1 = bool(finding and (true_top_heading_count >= 1 or max_true >= 2))
    return {
        "side_region_counts": side_counts,
        "true_edge_counts": true_edge_counts,
        "max_true_edge_count": max_true,
        "true_top_heading_count": true_top_heading_count,
        "region_count": len(regions),
        "inset_px": round(inset, 2),
        "finding": finding,
        "source": "canvas",
        "boundary_source": "canvas",
        "trusted_p1": trusted_p1,
        "trusted_sources": ["canvas"] if max_true else [],
        "confidence": "high" if trusted_p1 else "medium",
        "samples": samples,
    }


def _audit_bottom_truncation(
    *,
    text_regions: list[dict[str, Any]],
    visual_regions: list[dict[str, Any]],
    occupancy: dict[str, Any] | None,
    width: int,
    height: int,
    inset: float,
    heading_y: float,
) -> dict[str, Any]:
    bottom_band = max(inset * 2.5, height * 0.035)
    true_band = max(2.0, height * 0.004)
    samples: list[dict[str, Any]] = []
    bottom_regions: list[dict[str, Any]] = []
    true_touch_count = 0
    long_edge = max(width, height)
    visual_candidates = _layout_damage_visual_regions(visual_regions, width=width, height=height, long_edge=long_edge)
    for region in [r for r in text_regions if r.get("body")] + visual_candidates:
        rect = region["rect"]
        if rect["y1"] < heading_y or rect["y1"] < height - bottom_band:
            continue
        sample = {
            "id": region.get("id"),
            "kind": region.get("kind"),
            "rect": _round_rect_f(rect),
            "distance_to_bottom_px": round(float(height) - rect["y1"], 2),
        }
        bottom_regions.append(sample)
        if rect["y1"] >= height - true_band:
            true_touch_count += 1
        if len(samples) < 8:
            samples.append(sample)

    by_side = _edge_occupancy_by_side(occupancy) or {}
    bottom_occ = ((by_side.get("sides") or {}).get("bottom") or {})
    bottom_ratio = float(bottom_occ.get("occupied_ratio") or 0.0)
    finding = bool(true_touch_count) or bottom_ratio >= 0.82 or (bottom_ratio >= 0.68 and len(bottom_regions) >= 2)
    trusted_p1 = true_touch_count >= 2 or (true_touch_count >= 1 and bottom_ratio >= 0.86)
    severity = "P1" if trusted_p1 else "P2"
    return {
        "finding": finding,
        "severity": severity,
        "bottom_region_count": len(bottom_regions),
        "true_bottom_touch_count": true_touch_count,
        "bottom_occupancy_ratio": round(bottom_ratio, 4),
        "bottom_band_px": round(bottom_band, 2),
        "source": "canvas_bottom",
        "boundary_source": "canvas",
        "trusted_p1": bool(trusted_p1 and finding),
        "trusted_sources": ["canvas"] if trusted_p1 and finding else [],
        "confidence": "high" if trusted_p1 and finding else "medium",
        "samples": samples,
    }


def _audit_panel_bounds(
    *,
    text_regions: list[dict[str, Any]],
    visual_regions: list[dict[str, Any]],
    panel_regions: list[dict[str, Any]],
    canvas_width: int,
    long_edge: int,
    canvas_height: int,
) -> dict[str, Any]:
    inset = max(6.0, 0.006 * float(long_edge))
    overflow_gap = max(inset, 0.015 * float(long_edge))
    severe_overflow_gap = max(overflow_gap * 1.8, 0.03 * float(long_edge))
    findings: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    text_over: list[dict[str, Any]] = []
    visual_over: list[dict[str, Any]] = []
    tight: list[dict[str, Any]] = []
    cross_panel: list[dict[str, Any]] = []
    if not panel_regions:
        return {
            "panel_count": 0,
            "visual_internal_panel_count": 0,
            "text_overflow_count": 0,
            "visual_overflow_count": 0,
            "tight_count": 0,
            "cross_panel_count": 0,
            "findings": [],
            "samples": [],
        }

    visual_candidates = _layout_damage_visual_regions(
        visual_regions,
        width=canvas_width,
        height=canvas_height,
        long_edge=long_edge,
    )
    visual_internal_panels = [
        panel for panel in panel_regions
        if any(
            _panel_is_internal_to_visual(panel["rect"], visual["rect"], long_edge=long_edge)
            for visual in visual_candidates
        )
    ]
    panel_regions = [panel for panel in panel_regions if panel not in visual_internal_panels]
    if not panel_regions:
        return {
            "panel_count": 0,
            "visual_internal_panel_count": len(visual_internal_panels),
            "text_overflow_count": 0,
            "visual_overflow_count": 0,
            "tight_count": 0,
            "cross_panel_count": 0,
            "findings": [],
            "samples": [],
        }
    for region in [r for r in text_regions if r.get("body")]:
        _classify_panel_region(region, panel_regions, inset, overflow_gap, severe_overflow_gap, canvas_height, text_over, tight, cross_panel, "text")
    for region in visual_candidates:
        _classify_panel_region(region, panel_regions, inset, overflow_gap, severe_overflow_gap, canvas_height, visual_over, tight, cross_panel, "visual")

    if text_over:
        trusted_text_over = [sample for sample in text_over if sample.get("closed_boundary_credible")]
        trusted_p1 = False
        sev = "P2"
        penalty = _panel_overflow_penalty(
            kind="text",
            severity=sev,
            count=len(text_over),
            max_gap=max(s["outside_gap_px"] for s in text_over),
            severe_gap=severe_overflow_gap,
        )
        findings.append({
            "id": "basic-layout-panel-text-overflow",
            "severity": sev,
            "message": "Detected text extends outside its panel/frame boundary.",
            "penalty": penalty,
            "evidence": {"count": len(text_over), "credible_boundary_count": len(trusted_text_over), "safe_inset_px": round(inset, 2), "overflow_gap_px": round(overflow_gap, 2), "severe_overflow_gap_px": round(severe_overflow_gap, 2), "scaled_penalty": penalty, "boundary_source": "panel_candidate", "trusted_p1": False, "trusted_sources": [], "confidence": "limited", "samples": text_over[:5]},
        })
    if visual_over:
        trusted_visual_over = [sample for sample in visual_over if sample.get("closed_boundary_credible")]
        trusted_p1 = False
        sev = "P2"
        penalty = _panel_overflow_penalty(
            kind="visual",
            severity=sev,
            count=len(visual_over),
            max_gap=max(s["outside_gap_px"] for s in visual_over),
            severe_gap=severe_overflow_gap,
        )
        findings.append({
            "id": "basic-layout-panel-visual-overflow",
            "severity": sev,
            "message": "Detected visual content extends outside its panel/frame boundary.",
            "penalty": penalty,
            "evidence": {"count": len(visual_over), "credible_boundary_count": len(trusted_visual_over), "safe_inset_px": round(inset, 2), "overflow_gap_px": round(overflow_gap, 2), "severe_overflow_gap_px": round(severe_overflow_gap, 2), "scaled_penalty": penalty, "boundary_source": "panel_candidate", "trusted_p1": False, "trusted_sources": [], "confidence": "limited", "samples": visual_over[:5]},
        })
    if tight and len(tight) >= max(18, int(0.65 * max(1, len(text_regions) + len(visual_regions)))):
        findings.append({
            "id": "basic-layout-panel-content-tight",
            "severity": "P2",
            "message": "A substantial amount of content is pressed against panel/frame edges.",
            "penalty": 0.7,
            "evidence": {"count": len(tight), "safe_inset_px": round(inset, 2), "samples": tight[:5]},
        })
    samples.extend(text_over[:4] + visual_over[:4] + tight[:4] + cross_panel[:4])
    return {
        "panel_count": len(panel_regions),
        "visual_internal_panel_count": len(visual_internal_panels),
        "text_overflow_count": len(text_over),
        "visual_overflow_count": len(visual_over),
        "tight_count": len(tight),
        "cross_panel_count": len(cross_panel),
        "findings": findings,
        "samples": samples,
    }


def _panel_is_internal_to_visual(
    panel_rect: dict[str, float],
    visual_rect: dict[str, float],
    *,
    long_edge: int,
) -> bool:
    panel_area = max(1.0, _rect_area(panel_rect))
    visual_area = max(1.0, _rect_area(visual_rect))
    if visual_area < panel_area * 0.85:
        return False
    if _intersection_area(panel_rect, visual_rect) / panel_area < 0.82:
        return False
    return _outside_gap(visual_rect, panel_rect) >= max(
        0.01 * float(long_edge),
        0.08 * min(panel_rect["w"], panel_rect["h"]),
    )


def _audit_section_bounds(
    *,
    text_regions: list[dict[str, Any]],
    visual_regions: list[dict[str, Any]],
    section_regions: list[dict[str, Any]],
    panel_region_count: int,
    long_edge: int,
    canvas_width: int,
    canvas_height: int,
    heading_y: float,
) -> dict[str, Any]:
    inset = max(8.0, 0.007 * float(long_edge))
    overflow_gap = max(inset * 1.4, 0.018 * float(long_edge))
    findings: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    overflow: list[dict[str, Any]] = []
    tight: list[dict[str, Any]] = []
    cross: list[dict[str, Any]] = []
    late: list[dict[str, Any]] = []
    underfilled: list[dict[str, Any]] = []
    if not section_regions:
        return {
            "section_count": 0,
            "source": "none",
            "content_overflow_count": 0,
            "edge_tight_count": 0,
            "inter_section_collision_count": 0,
            "bottom_truncated_section_count": 0,
            "underfilled_section_count": 0,
            "findings": [],
            "samples": [],
        }

    credible_section_count = sum(
        1 for section in section_regions
        if _is_credible_closed_boundary(section)
    )
    source = "panel" if panel_region_count >= 2 and credible_section_count >= 2 else "inferred"
    visual_candidates = _layout_damage_visual_regions(
        visual_regions,
        width=canvas_width,
        height=canvas_height,
        long_edge=long_edge,
    )
    content = [r for r in text_regions if r.get("body")] + visual_candidates
    for region in content:
        rect = region["rect"]
        section = _assign_panel(rect, section_regions, kind="text" if str(region.get("kind", "")).endswith("text") else "visual")
        if section is None:
            continue
        section_rect = section["rect"]
        outside = _outside_gap(rect, section_rect)
        sample = {
            "id": region.get("id"),
            "kind": region.get("kind"),
            "section_id": section.get("id"),
            "rect": _round_rect_f(rect),
            "section_rect": _round_rect_f(section_rect),
            "outside_gap_px": round(outside, 2),
        }
        if source == "panel":
            continue
        intersections = [
            s for s in section_regions
            if s is not section
            and not _nested_boundary_pair(section_rect, s["rect"], pad=inset)
            if _intersection_area(rect, s["rect"]) / max(1.0, _rect_area(rect)) >= 0.20
        ]
        if intersections:
            cross.append({**sample, "section_hits": [section.get("id")] + [s.get("id") for s in intersections[:3]]})
        if outside >= overflow_gap and _rect_area(rect) / max(1.0, _rect_area(section_rect)) >= 0.001:
            if source == "inferred" and outside < overflow_gap * 1.8:
                continue
            overflow.append(sample)
            continue
        distances = [
            rect["x0"] - section_rect["x0"],
            section_rect["x1"] - rect["x1"],
            rect["y0"] - section_rect["y0"],
            section_rect["y1"] - rect["y1"],
        ]
        if source != "panel" and min(distances) < inset and _rect_area(rect) / max(1.0, _rect_area(section_rect)) >= 0.0012:
            tight.append({**sample, "min_section_edge_distance_px": round(min(distances), 2)})

    bottom_zone_y = canvas_height * 0.90
    for section in section_regions:
        rect = section["rect"]
        if rect["y0"] >= bottom_zone_y or rect["y1"] >= canvas_height - max(4.0, inset * 0.5):
            hits = [
                r for r in content
                if _intersection_area(r["rect"], rect) / max(1.0, _rect_area(r["rect"])) >= 0.35
            ]
            if hits or rect["h"] <= canvas_height * 0.09:
                late.append({
                    "section_id": section.get("id"),
                    "rect": _round_rect_f(rect),
                    "content_hits": len(hits),
                })
        bottom_half_hits = [
            r for r in content
            if _point_in_rect(_rect_center(r["rect"]), rect)
            and r["rect"]["y0"] >= rect["y0"] + rect["h"] * 0.52
        ]
        all_hits = [
            r for r in content
            if _point_in_rect(_rect_center(r["rect"]), rect)
        ]
        if rect["h"] >= canvas_height * 0.16 and len(all_hits) >= 2 and not bottom_half_hits:
            lower_rect = {"x0": rect["x0"], "y0": rect["y0"] + rect["h"] * 0.52, "x1": rect["x1"], "y1": rect["y1"], "w": rect["w"], "h": rect["h"] * 0.48}
            if _rect_area(lower_rect) / max(1.0, canvas_width * canvas_height) >= 0.035:
                underfilled.append({"section_id": section.get("id"), "rect": _round_rect_f(rect), "content_hits": len(all_hits)})

    if overflow:
        trusted_p1 = source == "panel" and len(overflow) >= 3
        sev = "P1" if trusted_p1 else "P2"
        findings.append({
            "id": "basic-layout-section-content-overflow",
            "severity": sev,
            "message": "Detected content extending outside its inferred section box.",
            "penalty": 1.5 if sev == "P1" else 0.8,
            "evidence": {
                "count": len(overflow),
                "source": source,
                "boundary_source": "closed_panel" if source == "panel" else "inferred_open_grid",
                "trusted_p1": trusted_p1,
                "trusted_sources": ["closed_panel"] if trusted_p1 else [],
                "confidence": "high" if trusted_p1 else "medium",
                "samples": overflow[:5],
            },
        })
    if tight and len(tight) >= max(10, int(0.45 * max(1, len(content)))):
        findings.append({
            "id": "basic-layout-section-edge-tight",
            "severity": "P2",
            "message": "A substantial amount of content is pressed against section edges.",
            "penalty": 0.7,
            "evidence": {
                "count": len(tight),
                "source": source,
                "boundary_source": "closed_panel" if source == "panel" else "inferred_open_grid",
                "trusted_p1": False,
                "samples": tight[:5],
            },
        })
    if cross:
        findings.append({
            "id": "basic-layout-inter-section-collision",
            "severity": "P2",
            "message": "Detected content crossing between adjacent section boxes.",
            "penalty": 0.9,
            "evidence": {
                "count": len(cross),
                "source": source,
                "boundary_source": "closed_panel" if source == "panel" else "inferred_open_grid",
                "trusted_p1": False,
                "samples": cross[:5],
            },
        })
    if late:
        findings.append({
            "id": "basic-layout-section-bottom-truncated",
            "severity": "P2",
            "message": "A section starts or continues too close to the poster bottom edge.",
            "penalty": 0.9,
            "evidence": {
                "count": len(late),
                "source": source,
                "boundary_source": "closed_panel" if source == "panel" else "inferred_open_grid",
                "trusted_p1": False,
                "samples": late[:5],
            },
        })
    if underfilled and any(item.get("rect", {}).get("y1", 0) >= canvas_height * 0.70 for item in late + overflow):
        findings.append({
            "id": "basic-layout-panel-underfilled",
            "severity": "P2",
            "message": "Some sections are underfilled while later content appears truncated or overflowing.",
            "penalty": 0.7,
            "evidence": {
                "count": len(underfilled),
                "source": source,
                "boundary_source": "closed_panel" if source == "panel" else "inferred_open_grid",
                "trusted_p1": False,
                "samples": underfilled[:5],
            },
        })

    samples.extend(overflow[:4] + tight[:4] + cross[:4] + late[:4] + underfilled[:4])
    return {
        "section_count": len(section_regions),
        "source": source,
        "content_overflow_count": len(overflow),
        "edge_tight_count": len(tight),
        "inter_section_collision_count": len(cross),
        "bottom_truncated_section_count": len(late),
        "underfilled_section_count": len(underfilled),
        "findings": findings,
        "samples": samples[:12],
    }


def _audit_visual_crop_damage(
    *,
    visual_regions: list[dict[str, Any]],
    section_regions: list[dict[str, Any]],
    width: int,
    height: int,
    long_edge: int,
    visual_mask: Any | None = None,
) -> dict[str, Any]:
    inset = max(5.0, 0.004 * float(long_edge))
    true_edge = max(1.5, 0.0015 * float(long_edge))
    overflow_gap = max(inset * 1.6, 0.010 * float(long_edge))
    strip_px = max(3.0, inset)
    samples: list[dict[str, Any]] = []
    visual_candidates = _layout_damage_visual_regions(
        visual_regions,
        width=width,
        height=height,
        long_edge=long_edge,
    )
    for visual in visual_candidates:
        rect = visual["rect"]
        if _rect_area(rect) / max(1.0, width * height) < 0.006:
            continue
        sides = []
        if rect["x0"] <= true_edge:
            sides.append("left")
        if rect["x1"] >= width - true_edge:
            sides.append("right")
        if rect["y1"] >= height - true_edge:
            sides.append("bottom")
        if (
            "left" in sides
            and "right" in sides
            and "bottom" not in sides
            and rect["w"] >= width * 0.80
        ):
            sides = []
        if sides and "bottom" not in sides and len(sides) < 2:
            sides = []
        candidate_sections = [
            section for section in section_regions
            if not _panel_is_internal_to_visual(
                section["rect"],
                rect,
                long_edge=long_edge,
            )
        ]
        assigned = _assign_panel(rect, candidate_sections, kind="visual") if candidate_sections else None
        local_sides: list[str] = []
        boundary_activity: list[dict[str, Any]] = []
        if assigned and _is_credible_closed_boundary(assigned):
            srect = assigned["rect"]
            candidate_sides = []
            if rect["x0"] <= srect["x0"] - overflow_gap:
                candidate_sides.append("section-left")
            if rect["x1"] >= srect["x1"] + overflow_gap:
                candidate_sides.append("section-right")
            if rect["y1"] >= srect["y1"] + overflow_gap:
                candidate_sides.append("section-bottom")
            for side in candidate_sides:
                activity = _boundary_strip_activity(
                    visual_mask,
                    rect,
                    side=side,
                    boundary_rect=srect,
                    width=width,
                    height=height,
                    strip_px=strip_px,
                )
                boundary_activity.append(activity)
                if activity.get("sustained"):
                    local_sides.append(side)
        if sides or local_sides:
            trusted_sources = []
            if sides:
                trusted_sources.append("canvas")
            if local_sides:
                trusted_sources.append("closed_panel")
            samples.append({
                "id": visual.get("id"),
                "rect": _round_rect_f(rect),
                "canvas_sides": sides,
                "section_sides": local_sides,
                "section_id": assigned.get("id") if assigned else None,
                "section_boundary_source": "closed_panel" if assigned and _is_credible_closed_boundary(assigned) else None,
                "boundary_activity": boundary_activity,
                "trusted_sources": trusted_sources,
            })
        if len(samples) >= 8:
            break
    findings = []
    if samples:
        canvas_hits = sum(1 for sample in samples if sample.get("canvas_sides"))
        closed_boundary_hits = sum(1 for sample in samples if sample.get("section_sides"))
        trusted_sources = sorted({source for sample in samples for source in sample.get("trusted_sources", [])})
        trusted_p1 = canvas_hits >= 2 or closed_boundary_hits >= 2
        sev = "P1" if trusted_p1 else "P2"
        findings.append({
            "id": "basic-layout-visual-crop-damage",
            "severity": sev,
            "message": "Detected figure/table-like visual content clipped or pressed against a canvas/section edge.",
            "penalty": 1.5 if sev == "P1" else 0.8,
            "evidence": {
                "count": len(samples),
                "canvas_hit_count": canvas_hits,
                "closed_boundary_hit_count": closed_boundary_hits,
                "trusted_p1": trusted_p1,
                "trusted_sources": trusted_sources,
                "boundary_source": "canvas" if canvas_hits else ("closed_panel" if closed_boundary_hits else "none"),
                "confidence": "high" if trusted_p1 else "medium",
                "source_confidence": "trusted" if trusted_p1 else "limited",
                "overflow_gap_px": round(overflow_gap, 2),
                "boundary_strip_px": round(strip_px, 2),
                "samples": samples[:5],
            },
        })
    return {"crop_damage_count": len(samples), "findings": findings, "samples": samples}


def _is_credible_closed_boundary(region: dict[str, Any]) -> bool:
    kind = str(region.get("kind") or "")
    if "inferred" in kind:
        return False
    return float(region.get("confidence") or 0.0) >= 0.72


def _boundary_strip_activity(
    visual_mask: Any | None,
    rect: dict[str, float],
    *,
    side: str,
    boundary_rect: dict[str, float],
    width: int,
    height: int,
    strip_px: float,
) -> dict[str, Any]:
    if visual_mask is None:
        return {"side": side, "available": False, "sustained": False}
    try:
        import numpy as np

        mask = np.asarray(visual_mask)
    except Exception:  # noqa: BLE001 - optional CV/numpy evidence only.
        return {"side": side, "available": False, "sustained": False}
    if mask.ndim != 2 or mask.size == 0:
        return {"side": side, "available": False, "sustained": False}

    strip = max(1, int(round(strip_px)))
    rx0 = max(0, int(rect["x0"]))
    rx1 = min(width, int(rect["x1"]))
    ry0 = max(0, int(rect["y0"]))
    ry1 = min(height, int(rect["y1"]))
    if side == "section-left":
        bx = int(round(boundary_rect["x0"]))
        box = (max(rx0, bx - strip), ry0, min(rx1, bx + strip), ry1)
    elif side == "section-right":
        bx = int(round(boundary_rect["x1"]))
        box = (max(rx0, bx - strip), ry0, min(rx1, bx + strip), ry1)
    elif side == "section-bottom":
        by = int(round(boundary_rect["y1"]))
        box = (rx0, max(ry0, by - strip), rx1, min(ry1, by + strip))
    else:
        return {"side": side, "available": True, "sustained": False}

    x0, y0, x1, y1 = box
    x0 = max(0, min(mask.shape[1], x0))
    x1 = max(0, min(mask.shape[1], x1))
    y0 = max(0, min(mask.shape[0], y0))
    y1 = max(0, min(mask.shape[0], y1))
    if x1 <= x0 or y1 <= y0:
        return {"side": side, "available": True, "sustained": False, "active_px": 0, "strip_px": [x0, y0, x1, y1]}

    active = mask[y0:y1, x0:x1] > 0
    active_px = int(active.sum())
    total_px = int(active.size)
    active_fraction = active_px / max(1, total_px)
    min_active_px = max(18, int(total_px * 0.018))
    if side in {"section-left", "section-right"}:
        boundary_offset = max(0, min(active.shape[1], int(round(boundary_rect["x0" if side == "section-left" else "x1"])) - x0))
        deadzone = max(2, int(round(strip * 0.35)))
        before = active[:, :max(0, boundary_offset - deadzone)]
        after = active[:, min(active.shape[1], boundary_offset + deadzone):]
    else:
        boundary_offset = max(0, min(active.shape[0], int(round(boundary_rect["y1"])) - y0))
        deadzone = max(2, int(round(strip * 0.35)))
        before = active[:max(0, boundary_offset - deadzone), :]
        after = active[min(active.shape[0], boundary_offset + deadzone):, :]
    before_px = int(before.sum())
    after_px = int(after.sum())
    min_half_active = max(6, int(min(before.size, after.size) * 0.01))
    crosses_boundary = before_px >= min_half_active and after_px >= min_half_active
    sustained = active_px >= min_active_px and active_fraction >= 0.012 and crosses_boundary
    return {
        "side": side,
        "available": True,
        "sustained": bool(sustained),
        "active_px": active_px,
        "active_fraction": round(active_fraction, 4),
        "min_active_px": min_active_px,
        "inside_active_px": before_px,
        "outside_active_px": after_px,
        "crosses_boundary": crosses_boundary,
        "strip_px": [x0, y0, x1, y1],
    }


def _audit_empty_visual_placeholders(
    *,
    image: Image.Image,
    panel_regions: list[dict[str, Any]],
    heading_y: float,
) -> dict[str, Any]:
    """Find framed visual slots that are blank or contain only a token label.

    These are rendered-image failures, not an attempt to infer whether a sparse
    scientific chart is useful. The near-empty branch deliberately excludes a
    panel nested inside a normal figure section, which prevents a light line
    chart from being treated as an empty image frame.
    """
    try:
        import numpy as np
    except Exception:  # noqa: BLE001 - image-native checks degrade without numpy.
        return {
            "available": False,
            "reason": "numpy-unavailable",
            "blank_placeholder_count": 0,
            "near_empty_slot_count": 0,
            "findings": [],
            "samples": [],
        }

    gray = np.asarray(image.convert("L"))
    height, width = gray.shape[:2]
    canvas_area = float(max(1, width * height))
    short_edge = float(min(width, height))
    blank: list[dict[str, Any]] = []
    near_empty: list[dict[str, Any]] = []

    for panel in panel_regions:
        rect = panel.get("rect")
        if not isinstance(rect, dict):
            continue
        area_ratio = _rect_area(rect) / canvas_area
        if not 0.012 <= area_ratio <= 0.05:
            continue
        if rect["y0"] < heading_y or min(rect["w"], rect["h"]) < short_edge * 0.12:
            continue
        if max(rect["w"], rect["h"]) / max(1.0, min(rect["w"], rect["h"])) > 2.6:
            continue

        inset = max(2, int(round(min(rect["w"], rect["h"]) * 0.035)))
        x0 = max(0, int(rect["x0"]) + inset)
        x1 = min(width, int(rect["x1"]) - inset)
        y0 = max(0, int(rect["y0"]) + inset)
        y1 = min(height, int(rect["y1"]) - inset)
        interior = gray[y0:y1, x0:x1]
        if interior.size == 0:
            continue

        ink_fraction = float((interior < 235).mean())
        bright_fraction = float((interior > 245).mean())
        luma_std = float(interior.std())
        medium_parent_present = any(
            other is not panel
            and isinstance(other.get("rect"), dict)
            and _rect_contains(other["rect"], rect, pad=-10.0)
            and 1.8 <= _rect_area(other["rect"]) / max(1.0, _rect_area(rect)) <= 14.0
            for other in panel_regions
        )
        sample = {
            "panel_id": panel.get("id"),
            "rect": _round_rect_f(rect),
            "area_ratio": round(area_ratio, 4),
            "interior_ink_fraction": round(ink_fraction, 4),
            "interior_bright_fraction": round(bright_fraction, 4),
            "interior_luma_std": round(luma_std, 2),
            "medium_parent_present": medium_parent_present,
        }

        # A frame that is nearly uniform white has no usable visual evidence.
        if ink_fraction <= 0.006 and bright_fraction >= 0.985 and luma_std <= 13.0:
            blank.append(sample)
        # A token label in a mostly empty, standalone framed slot is likewise
        # unusable. Nested light charts are excluded by the parent check above.
        elif (
            not medium_parent_present
            and ink_fraction <= 0.02
            and bright_fraction >= 0.91
            and luma_std <= 24.0
        ):
            near_empty.append(sample)

    findings: list[dict[str, Any]] = []
    if len(blank) >= 2:
        findings.append({
            "id": "basic-layout-empty-visual-placeholder-severe",
            "severity": "P0",
            "message": "Multiple framed visual slots are effectively empty.",
            "penalty": 7.0,
            "evidence": {"blank_placeholder_count": len(blank), "samples": blank[:5]},
        })
    elif blank:
        findings.append({
            "id": "basic-layout-empty-visual-placeholder",
            "severity": "P1",
            "message": "A framed visual slot is effectively empty.",
            "penalty": 3.0,
            "evidence": {"blank_placeholder_count": len(blank), "samples": blank[:5]},
        })
    if near_empty:
        findings.append({
            "id": "basic-layout-near-empty-visual-slot",
            "severity": "P1",
            "message": "A framed visual slot contains only token content and is visually underfilled.",
            "penalty": 3.0,
            "evidence": {"near_empty_slot_count": len(near_empty), "samples": near_empty[:5]},
        })
    return {
        "available": True,
        "blank_placeholder_count": len(blank),
        "near_empty_slot_count": len(near_empty),
        "findings": findings,
        "samples": (blank + near_empty)[:8],
    }


def _audit_multi_panel_crop_failure(
    *,
    canvas_metrics: dict[str, Any],
    bottom_metrics: dict[str, Any],
    crop_metrics: dict[str, Any],
) -> dict[str, Any]:
    """Require three independent crop signals before calling a multi-panel failure."""
    canvas_edge_count = int(canvas_metrics.get("max_true_edge_count") or 0)
    bottom_touch_count = int(bottom_metrics.get("true_bottom_touch_count") or 0)
    crop_damage_count = int(crop_metrics.get("crop_damage_count") or 0)
    detected = canvas_edge_count >= 2 and bottom_touch_count >= 3 and crop_damage_count >= 2
    findings: list[dict[str, Any]] = []
    if detected:
        findings.append({
            "id": "basic-layout-multi-panel-crop-failure",
            "severity": "P1",
            "message": "Multiple independent image-native signals show several panels cut off at the poster edge.",
            "penalty": 2.5,
            "evidence": {
                "canvas_true_edge_count": canvas_edge_count,
                "bottom_true_touch_count": bottom_touch_count,
                "visual_crop_damage_count": crop_damage_count,
                "boundary_source": "canvas",
                "trusted_p1": True,
                "trusted_sources": ["canvas", "bottom_truncation"],
                "confidence": "high",
            },
        })
    return {
        "detected": detected,
        "canvas_true_edge_count": canvas_edge_count,
        "bottom_true_touch_count": bottom_touch_count,
        "visual_crop_damage_count": crop_damage_count,
        "findings": findings,
    }


def _panel_overflow_penalty(*, kind: str, severity: str, count: int, max_gap: float, severe_gap: float) -> float:
    if severity != "P1":
        return 0.8
    if kind == "text":
        penalty = 1.8
        if count >= 8:
            penalty = 2.5
        elif count >= 4:
            penalty = 2.2
    else:
        penalty = 1.8
        if count >= 6:
            penalty = 2.5
        elif count >= 3:
            penalty = 2.4
    if max_gap >= severe_gap * 1.6:
        penalty = max(penalty, 2.5)
    return round(min(2.5, penalty), 3)


def _classify_panel_region(
    region: dict[str, Any],
    panels: list[dict[str, Any]],
    inset: float,
    overflow_gap: float,
    severe_overflow_gap: float,
    canvas_height: int,
    overflow_out: list[dict[str, Any]],
    tight_out: list[dict[str, Any]],
    cross_out: list[dict[str, Any]],
    kind: str,
) -> None:
    rect = region["rect"]
    panel = _assign_panel(rect, panels, kind=kind)
    if panel is None:
        return
    panel_rect = panel["rect"]
    outside = _outside_gap(rect, panel_rect)
    intersections = [
        p for p in panels
        if _intersection_area(rect, p["rect"]) / max(1.0, _rect_area(rect)) >= 0.18
    ]
    closed_boundary_credible = _is_credible_closed_boundary(panel)
    sample = {
        "id": region.get("id"),
        "kind": kind,
        "panel_id": panel.get("id"),
        "rect": _round_rect_f(rect),
        "panel_rect": _round_rect_f(panel_rect),
        "outside_gap_px": round(outside, 2),
        "panel_confidence": panel.get("confidence"),
        "closed_boundary_credible": closed_boundary_credible,
    }
    if len(intersections) >= 2:
        cross_out.append({**sample, "panel_hits": [p.get("id") for p in intersections[:4]]})
    if outside >= overflow_gap:
        if kind == "text" and outside < severe_overflow_gap:
            _area_cover, x_cover, _y_cover = _text_panel_cover_ratios(rect, panel_rect)
            if x_cover < 0.75:
                return
        overflow_out.append(sample)
        return
    bottom_gap = rect["y1"] - panel_rect["y1"]
    near_page_bottom = panel_rect["y1"] >= float(canvas_height) - max(28.0, 0.07 * float(canvas_height))
    if kind == "text" and near_page_bottom and bottom_gap >= max(4.0, inset * 0.5):
        _area_cover, x_cover, _y_cover = _text_panel_cover_ratios(rect, panel_rect)
        if x_cover >= 0.75:
            overflow_out.append({**sample, "outside_gap_px": round(bottom_gap, 2), "minor_bottom_overflow": True})
            return
    distances = [
        rect["x0"] - panel_rect["x0"],
        panel_rect["x1"] - rect["x1"],
        rect["y0"] - panel_rect["y0"],
        panel_rect["y1"] - rect["y1"],
    ]
    if min(distances) < inset and _rect_area(rect) / max(1.0, _rect_area(panel_rect)) > 0.0008:
        tight_out.append({**sample, "min_panel_edge_distance_px": round(min(distances), 2)})


def _audit_overlaps(
    *,
    text_regions: list[dict[str, Any]],
    visual_regions: list[dict[str, Any]],
    panel_regions: list[dict[str, Any]],
    visual_mask: Any | None,
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    text_text = _text_overlap_samples([r for r in text_regions if r.get("body")])
    text_visual = _text_visual_overlap_samples([r for r in text_regions if r.get("body")], visual_regions, visual_mask)
    visual_visual = _visual_overlap_samples(visual_regions)
    if len(text_text) >= 2:
        findings.append({
            "id": "basic-layout-text-overlap",
            "severity": "P1",
            "message": "Detected overlapping OCR text boxes that are unlikely to be normal line spacing.",
            "penalty": 1.6,
            "evidence": {"count": len(text_text), "samples": text_text[:5]},
        })
    if text_visual:
        findings.append({
            "id": "basic-layout-text-visual-overlap",
            "severity": "P1" if len(text_visual) >= 2 else "P2",
            "message": "Detected readable text drawn over independent visual content.",
            "penalty": 1.6 if len(text_visual) >= 2 else 0.8,
            "evidence": {"count": len(text_visual), "samples": text_visual[:5]},
        })
    if visual_visual:
        findings.append({
            "id": "basic-layout-visual-overlap",
            "severity": "P2",
            "message": "Detected overlapping visual regions.",
            "penalty": 0.8,
            "evidence": {"count": len(visual_visual), "samples": visual_visual[:5]},
        })
    samples.extend(text_text[:4] + text_visual[:4] + visual_visual[:4])
    return {
        "text_overlap_count": len(text_text),
        "text_visual_overlap_count": len(text_visual),
        "visual_overlap_count": len(visual_visual),
        "overlap_count": len(text_text) + len(text_visual) + len(visual_visual),
        "findings": findings,
        "samples": samples,
    }


def _text_overlap_samples(text_regions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    regs = sorted((r for r in text_regions if _text_overlap_candidate(r)), key=lambda r: (r["rect"]["y0"], r["rect"]["x0"]))[:350]
    for i, left in enumerate(regs):
        a = left["rect"]
        for right in regs[i + 1:]:
            b = right["rect"]
            if b["y0"] > a["y1"] + max(8.0, a["h"]):
                break
            inter = _intersection_area(a, b)
            if inter <= 0:
                continue
            ratio = inter / max(1.0, min(_rect_area(a), _rect_area(b)))
            if ratio >= 0.6 and abs(_rect_center(a)[1] - _rect_center(b)[1]) < max(a["h"], b["h"]) * 0.65:
                samples.append({"a": left["id"], "b": right["id"], "ratio": round(ratio, 3), "rect": _round_rect_f(_union_rect(a, b))})
                if len(samples) >= 10:
                    return samples
    return samples


def _text_overlap_candidate(region: dict[str, Any]) -> bool:
    rect = region["rect"]
    return rect["h"] >= 6.0 and rect["w"] >= max(32.0, rect["h"] * 2.5)


def _text_visual_overlap_samples(text_regions: list[dict[str, Any]], visual_regions: list[dict[str, Any]], visual_mask: Any | None) -> list[dict[str, Any]]:
    if visual_mask is None:
        return []
    samples: list[dict[str, Any]] = []
    h, w = visual_mask.shape[:2]
    for text in text_regions:
        rect = text["rect"]
        if float(text.get("height_ref_px") or 0.0) < 42.0:
            continue
        x0, y0, x1, y1 = _int_box(rect, w, h)
        if x1 <= x0 or y1 <= y0:
            continue
        mask_area = int((visual_mask[y0:y1, x0:x1] > 0).sum())
        overlap_ratio = mask_area / max(1.0, _rect_area(rect))
        if overlap_ratio < 0.38:
            continue
        for visual in visual_regions:
            vrect = visual["rect"]
            bbox_ratio = _intersection_area(rect, vrect) / max(1.0, _rect_area(rect))
            if bbox_ratio < 0.45:
                continue
            # Most chart/table labels are tiny OCR boxes inside a large visual; keep
            # the deterministic rule focused on larger text pasted over a visual.
            if _rect_area(vrect) > _rect_area(rect) * 25 and float(text.get("height_ref_px") or 0.0) < 22.0:
                continue
            samples.append({"text_id": text["id"], "visual_id": visual["id"], "mask_overlap_ratio": round(overlap_ratio, 3), "bbox_overlap_ratio": round(bbox_ratio, 3), "rect": _round_rect_f(_union_rect(rect, vrect))})
            if len(samples) >= 10:
                return samples
            break
    return samples


def _visual_overlap_samples(visual_regions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    regs = sorted(visual_regions, key=lambda r: r.get("area", 0), reverse=True)[:80]
    for i, left in enumerate(regs):
        for right in regs[i + 1:]:
            inter = _intersection_area(left["rect"], right["rect"])
            if inter <= 0:
                continue
            ratio = inter / max(1.0, min(_rect_area(left["rect"]), _rect_area(right["rect"])))
            if ratio >= 0.28 and not _rect_contains(left["rect"], right["rect"], pad=4.0) and not _rect_contains(right["rect"], left["rect"], pad=4.0):
                samples.append({"a": left["id"], "b": right["id"], "ratio": round(ratio, 3), "rect": _round_rect_f(_union_rect(left["rect"], right["rect"]))})
                if len(samples) >= 8:
                    return samples
    return samples


def _largest_rect(mask: list[list[bool]]) -> tuple[int, tuple[int, int, int, int]]:
    """Largest all-True rectangle in a binary grid. Returns (area_cells, (r0,c0,r1,c1))."""
    if not mask or not mask[0]:
        return 0, (0, 0, -1, -1)
    rows, cols = len(mask), len(mask[0])
    heights = [0] * cols
    best = (0, (0, 0, -1, -1))
    for r in range(rows):
        for c in range(cols):
            heights[c] = heights[c] + 1 if mask[r][c] else 0
        # largest rectangle in histogram, tracking column span
        stack: list[int] = []  # indices of increasing heights
        c = 0
        while c <= cols:
            cur = heights[c] if c < cols else 0
            start = c
            while stack and heights[stack[-1]] > cur:
                top = stack.pop()
                left = stack[-1] + 1 if stack else 0
                width = c - left
                area = heights[top] * width
                if area > best[0]:
                    best = (area, (r - heights[top] + 1, left, r, c - 1))
                start = left
            stack.append(c)
            c += 1
    return best


def _segment_rect(seg: dict[str, Any]) -> dict[str, float] | None:
    box = seg.get("box") if isinstance(seg, dict) else None
    if not isinstance(box, list) or len(box) < 3:
        return None
    try:
        xs = [float(pt[0]) for pt in box if isinstance(pt, (list, tuple)) and len(pt) >= 2]
        ys = [float(pt[1]) for pt in box if isinstance(pt, (list, tuple)) and len(pt) >= 2]
    except (TypeError, ValueError):
        return None
    if not xs or not ys:
        return None
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    return {
        "x0": x0,
        "y0": y0,
        "x1": x1,
        "y1": y1,
        "w": max(0.0, x1 - x0),
        "h": max(0.0, y1 - y0),
    }


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    n = len(vals)
    mid = n // 2
    if n % 2:
        return float(vals[mid])
    return (float(vals[mid - 1]) + float(vals[mid])) / 2.0


def _edge_occupancy_ratio(occupancy: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(occupancy, dict):
        return None
    occ = occupancy.get("occ")
    if not isinstance(occ, list) or not occ or not isinstance(occ[0], list):
        return None
    rows = len(occ)
    cols = len(occ[0])
    if rows <= 2 or cols <= 2:
        return None
    heading_rows = int(occupancy.get("heading_rows") or 0)
    band = max(1, round(min(rows, cols) * 0.025))
    edge_cells = []
    for r in range(max(0, heading_rows), rows):
        for c in range(cols):
            if c < band or c >= cols - band or r >= rows - band:
                edge_cells.append(bool(occ[r][c]))
    if not edge_cells:
        return None
    occupied = sum(1 for value in edge_cells if value)
    return {
        "edge_occupied_ratio": round(occupied / len(edge_cells), 4),
        "edge_occupied_cell_count": occupied,
        "edge_cell_count": len(edge_cells),
        "edge_band_cells": band,
        "heading_rows_excluded": heading_rows,
    }


def _edge_occupancy_by_side(occupancy: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(occupancy, dict):
        return None
    occ = occupancy.get("occ")
    if not isinstance(occ, list) or not occ or not isinstance(occ[0], list):
        return None
    rows = len(occ)
    cols = len(occ[0])
    if rows <= 2 or cols <= 2:
        return None
    heading_rows = int(occupancy.get("heading_rows") or 0)
    band = max(1, round(min(rows, cols) * 0.025))
    side_cells = {
        "left": [bool(occ[r][c]) for r in range(max(0, heading_rows), rows) for c in range(0, min(band, cols))],
        "right": [bool(occ[r][c]) for r in range(max(0, heading_rows), rows) for c in range(max(0, cols - band), cols)],
        "bottom": [bool(occ[r][c]) for r in range(max(0, rows - band), rows) for c in range(cols)],
    }
    sides: dict[str, Any] = {}
    warning_sides: list[str] = []
    severe_sides: list[str] = []
    for side, cells in side_cells.items():
        total = len(cells)
        occupied = sum(1 for value in cells if value)
        ratio = occupied / total if total else 0.0
        sides[side] = {
            "occupied_ratio": round(ratio, 4),
            "occupied_cell_count": occupied,
            "cell_count": total,
        }
        warn = 0.62 if side == "bottom" else 0.68
        severe = 0.82 if side == "bottom" else 0.88
        if ratio >= severe:
            severe_sides.append(side)
        elif ratio >= warn:
            warning_sides.append(side)
    return {
        "sides": sides,
        "warning_sides": warning_sides,
        "severe_sides": severe_sides,
        "edge_cell_count": sum(len(cells) for cells in side_cells.values()),
        "edge_band_cells": band,
        "heading_rows_excluded": heading_rows,
    }


def _infer_open_grid_sections(
    *,
    text_regions: list[dict[str, Any]],
    visual_regions: list[dict[str, Any]],
    occupancy: dict[str, Any] | None,
    width: int,
    height: int,
    heading_y: float,
    long_edge: int,
) -> list[dict[str, Any]]:
    content = [r for r in text_regions if r.get("body")] + visual_regions
    if len(content) < 6:
        return []
    rects = [r["rect"] for r in content if r.get("rect")]
    min_col_w = width * 0.18
    body_top = max(heading_y, min((rect["y0"] for rect in rects), default=heading_y))
    body_bottom = min(float(height), max((rect["y1"] for rect in rects), default=float(height)))
    columns = _infer_columns_from_rects(rects, width=width, heading_y=heading_y)
    if not columns:
        columns = [{"x0": 0.0, "x1": float(width)}]
    sections: list[dict[str, Any]] = []
    gap_min = max(16.0, 0.012 * float(long_edge))
    pad_x = max(12.0, 0.008 * float(long_edge))
    pad_y = max(10.0, 0.007 * float(long_edge))
    for col in columns:
        if col["x1"] - col["x0"] < min_col_w:
            continue
        col_rects = [
            rect for rect in rects
            if _rect_center(rect)[0] >= col["x0"] - pad_x and _rect_center(rect)[0] <= col["x1"] + pad_x
        ]
        if len(col_rects) < 2:
            continue
        col_rects.sort(key=lambda r: (r["y0"], r["x0"]))
        groups: list[list[dict[str, float]]] = []
        current: list[dict[str, float]] = []
        last_bottom: float | None = None
        for rect in col_rects:
            if last_bottom is not None and rect["y0"] - last_bottom >= gap_min:
                groups.append(current)
                current = []
            current.append(rect)
            last_bottom = max(last_bottom or rect["y1"], rect["y1"])
        if current:
            groups.append(current)
        for group in groups:
            if not group:
                continue
            union = group[0]
            for rect in group[1:]:
                union = _union_rect(union, rect)
            x0 = max(0.0, col["x0"] - pad_x)
            x1 = min(float(width), col["x1"] + pad_x)
            y0 = max(body_top, union["y0"] - pad_y)
            y1 = min(float(height), union["y1"] + pad_y)
            if y1 - y0 < max(44.0, 0.03 * float(long_edge)):
                continue
            sections.append({
                "id": f"section-{len(sections)+1}",
                "kind": "inferred-section",
                "rect": {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "w": x1 - x0, "h": y1 - y0},
                "area": round((x1 - x0) * (y1 - y0), 2),
                "confidence": 0.55,
            })
    sections = _dedupe_regions(sections, iou_threshold=0.72)
    # When grouping is too fine, add coarse column sections so edge/bottom
    # checks still have a section frame without forcing many false overflows.
    if len(sections) < 3:
        sections = []
        for col in columns:
            x0 = max(0.0, col["x0"] - pad_x)
            x1 = min(float(width), col["x1"] + pad_x)
            sections.append({
                "id": f"section-{len(sections)+1}",
                "kind": "inferred-section",
                "rect": {"x0": x0, "y0": body_top, "x1": x1, "y1": body_bottom, "w": x1 - x0, "h": body_bottom - body_top},
                "area": round((x1 - x0) * max(0.0, body_bottom - body_top), 2),
                "confidence": 0.42,
            })
    return sections[:36]


def _infer_columns_from_rects(rects: list[dict[str, float]], *, width: int, heading_y: float) -> list[dict[str, float]]:
    if not rects:
        return []
    bins = 96
    counts = [0] * bins
    for rect in rects:
        if rect["y1"] < heading_y:
            continue
        c0 = max(0, min(bins - 1, int(rect["x0"] / max(1, width) * bins)))
        c1 = max(0, min(bins - 1, int(rect["x1"] / max(1, width) * bins)))
        for c in range(c0, c1 + 1):
            counts[c] += 1
    active = [i for i, count in enumerate(counts) if count > 0]
    clusters = _index_clusters(active, gap=2)
    columns = []
    for cluster in clusters:
        x0 = cluster[0] / bins * width
        x1 = (cluster[-1] + 1) / bins * width
        if x1 - x0 >= width * 0.12:
            columns.append({"x0": float(x0), "x1": float(x1)})
    if len(columns) > 5:
        return []
    return columns


def _detector_coverage(
    *,
    ocr_status: str | None,
    ocr_body_count: int,
    fallback_text_count: int,
    effective_text_count: int,
    cv_available: bool,
    panel_count: int,
    visual_count: int,
    effective_section_count: int,
    occupancy: dict[str, Any] | None,
) -> dict[str, Any]:
    reduced: list[str] = []
    if ocr_status and ocr_status != "ok":
        reduced.append(f"ocr:{ocr_status}")
    if not cv_available:
        reduced.append("cv:unavailable")
    structured = False
    if isinstance(occupancy, dict):
        try:
            structured = float(occupancy.get("content_coverage") or 0.0) >= 0.18
        except (TypeError, ValueError):
            structured = False
    blind = (
        structured
        and effective_text_count == 0
        and panel_count == 0
        and visual_count == 0
        and effective_section_count == 0
    )
    disabled = []
    if effective_text_count == 0:
        disabled.extend(["text_size", "text_edge", "text_overlap"])
    if not cv_available:
        disabled.extend(["cv_visual_detection", "cv_panel_detection"])
    if effective_section_count == 0:
        disabled.append("section_bounds")
    return {
        "ocr_status": ocr_status or ("ok" if ocr_body_count else "unknown"),
        "cv_status": "ok" if cv_available else "unavailable",
        "ocr_body_region_count": ocr_body_count,
        "fallback_text_region_count": fallback_text_count,
        "effective_text_region_count": effective_text_count,
        "panel_count": panel_count,
        "visual_count": visual_count,
        "effective_section_count": effective_section_count,
        "disabled_checks": sorted(set(disabled)),
        "reduced_confidence_reasons": reduced,
        "structured_content_seen": structured,
        "blind": blind,
    }


def _dedupe_penalties(penalties: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the strongest penalty per finding id so repeated boxes do not over-penalize."""
    strongest: dict[str, dict[str, Any]] = {}
    for item in penalties:
        key = str(item.get("id") or "penalty")
        if key not in strongest or float(item.get("penalty") or 0.0) > float(strongest[key].get("penalty") or 0.0):
            strongest[key] = item
    return list(strongest.values())


def _debug_region(region: dict[str, Any]) -> dict[str, Any]:
    out = {
        "id": region.get("id"),
        "kind": region.get("kind"),
        "rect": _round_rect_f(region.get("rect") or {}),
        "area": region.get("area"),
    }
    for key in ("score", "height_ref_px", "body", "confidence", "text", "bbox_area"):
        if key in region:
            out[key] = region.get(key)
    return out


def _dedupe_regions(regions: list[dict[str, Any]], *, iou_threshold: float) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for region in sorted(regions, key=lambda r: _rect_area(r["rect"]), reverse=True):
        if any(_rect_iou(region["rect"], other["rect"]) >= iou_threshold for other in kept):
            continue
        kept.append(region)
    kept.sort(key=lambda r: (r["rect"]["y0"], r["rect"]["x0"], _rect_area(r["rect"])))
    return kept


def _dedupe_rects(rects: list[dict[str, float]], *, iou_threshold: float) -> list[dict[str, float]]:
    kept: list[dict[str, float]] = []
    for rect in sorted(rects, key=_rect_area, reverse=True):
        if any(_rect_iou(rect, other) >= iou_threshold for other in kept):
            continue
        kept.append(rect)
    kept.sort(key=lambda r: (r["y0"], r["x0"], _rect_area(r)))
    return kept


def _is_panel_sized_component(rect: dict[str, float], panels: list[dict[str, Any]]) -> bool:
    for panel in panels:
        panel_rect = panel["rect"]
        if not _content_matches_panel_rect(rect, panel_rect):
            continue
        if float(panel.get("confidence") or 1.0) < 0.82:
            continue
        nested_or_associated_with_larger_panel = any(
            other is not panel
            and _rect_area(other["rect"]) >= _rect_area(rect) * 1.8
            and (
                _point_in_rect(_rect_center(rect), other["rect"], pad=4.0)
                or _intersection_area(rect, other["rect"]) / max(1.0, _rect_area(rect)) >= 0.35
            )
            for other in panels
        )
        if not nested_or_associated_with_larger_panel:
            return True
    return False


def _content_matches_panel_rect(rect: dict[str, float], panel_rect: dict[str, float]) -> bool:
    panel_area = max(1.0, _rect_area(panel_rect))
    return (
        _rect_iou(rect, panel_rect) >= 0.82
        or (
            _intersection_area(rect, panel_rect) / max(1.0, _rect_area(rect)) >= 0.96
            and _rect_area(rect) >= 0.65 * panel_area
        )
    )


def _assign_panel(rect: dict[str, float], panels: list[dict[str, Any]], *, kind: str = "visual") -> dict[str, Any] | None:
    usable_panels = [panel for panel in panels if not _content_matches_panel_rect(rect, panel["rect"])]
    panels = usable_panels or panels
    if kind == "text":
        cover = [(panel, _text_panel_cover_ratios(rect, panel["rect"])) for panel in panels]
        mostly_containing = [panel for panel, ratios in cover if ratios[0] >= 0.88 and ratios[1] >= 0.86 and ratios[2] >= 0.70]
        cx, cy = _rect_center(rect)
        center_containing = [
            panel for panel in panels
            if panel["rect"]["x0"] <= cx <= panel["rect"]["x1"]
            and panel["rect"]["y0"] <= cy <= panel["rect"]["y1"]
            and panel["rect"]["w"] >= rect["w"] * 1.25
            and panel["rect"]["h"] >= max(rect["h"] * 3.0, rect["h"] + 20.0)
        ]
        if mostly_containing:
            child_center = [
                panel for panel in center_containing
                if any(_rect_area(panel["rect"]) <= _rect_area(parent["rect"]) * 0.8 for parent in mostly_containing)
            ]
            if child_center:
                return min(child_center, key=lambda p: _rect_area(p["rect"]))
            return min(mostly_containing, key=lambda p: _rect_area(p["rect"]))
        partial_text_matches = [panel for panel, ratios in cover if ratios[0] >= 0.55 and ratios[1] >= 0.86 and ratios[2] >= 0.55]
        if partial_text_matches:
            return min(partial_text_matches, key=lambda p: _rect_area(p["rect"]))
        if center_containing:
            return min(center_containing, key=lambda p: _rect_area(p["rect"]))
        return None
    region_area = max(1.0, _rect_area(rect))
    mostly_containing = [
        panel for panel in panels
        if _intersection_area(rect, panel["rect"]) / region_area >= 0.75
    ]
    if mostly_containing:
        return min(mostly_containing, key=lambda p: _rect_area(p["rect"]))
    cx, cy = _rect_center(rect)
    containing = [
        panel for panel in panels
        if panel["rect"]["x0"] <= cx <= panel["rect"]["x1"] and panel["rect"]["y0"] <= cy <= panel["rect"]["y1"]
    ]
    if containing:
        return min(containing, key=lambda p: _rect_area(p["rect"]))
    overlaps = [
        panel for panel in panels
        if _intersection_area(rect, panel["rect"]) / max(1.0, _rect_area(rect)) >= 0.35
    ]
    if overlaps:
        return min(overlaps, key=lambda p: _rect_area(p["rect"]))
    return None


def _text_panel_cover_ratios(rect: dict[str, float], panel_rect: dict[str, float]) -> tuple[float, float, float]:
    inter = _intersection_area(rect, panel_rect)
    area_cover = inter / max(1.0, _rect_area(rect))
    x_overlap = max(0.0, min(rect["x1"], panel_rect["x1"]) - max(rect["x0"], panel_rect["x0"]))
    y_overlap = max(0.0, min(rect["y1"], panel_rect["y1"]) - max(rect["y0"], panel_rect["y0"]))
    x_cover = x_overlap / max(1.0, rect["w"])
    y_cover = y_overlap / max(1.0, rect["h"])
    return area_cover, x_cover, y_cover


def _outside_gap(rect: dict[str, float], outer: dict[str, float]) -> float:
    return max(
        0.0,
        outer["x0"] - rect["x0"],
        rect["x1"] - outer["x1"],
        outer["y0"] - rect["y0"],
        rect["y1"] - outer["y1"],
    )


def _rect_area(rect: dict[str, float]) -> float:
    return max(0.0, float(rect.get("w", float(rect.get("x1", 0.0)) - float(rect.get("x0", 0.0))))) * max(
        0.0,
        float(rect.get("h", float(rect.get("y1", 0.0)) - float(rect.get("y0", 0.0)))),
    )


def _intersection_area(a: dict[str, float], b: dict[str, float]) -> float:
    x0 = max(float(a.get("x0", 0.0)), float(b.get("x0", 0.0)))
    y0 = max(float(a.get("y0", 0.0)), float(b.get("y0", 0.0)))
    x1 = min(float(a.get("x1", 0.0)), float(b.get("x1", 0.0)))
    y1 = min(float(a.get("y1", 0.0)), float(b.get("y1", 0.0)))
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def _rect_iou(a: dict[str, float], b: dict[str, float]) -> float:
    inter = _intersection_area(a, b)
    if inter <= 0:
        return 0.0
    return inter / max(1.0, _rect_area(a) + _rect_area(b) - inter)


def _rect_contains(outer: dict[str, float], inner: dict[str, float], *, pad: float = 0.0) -> bool:
    return (
        inner["x0"] >= outer["x0"] - pad
        and inner["y0"] >= outer["y0"] - pad
        and inner["x1"] <= outer["x1"] + pad
        and inner["y1"] <= outer["y1"] + pad
    )


def _nested_boundary_pair(a: dict[str, float], b: dict[str, float], *, pad: float = 0.0) -> bool:
    return _rect_contains(a, b, pad=pad) or _rect_contains(b, a, pad=pad)


def _rect_center(rect: dict[str, float]) -> tuple[float, float]:
    return ((float(rect.get("x0", 0.0)) + float(rect.get("x1", 0.0))) / 2.0,
            (float(rect.get("y0", 0.0)) + float(rect.get("y1", 0.0))) / 2.0)


def _point_in_rect(point: tuple[float, float], rect: dict[str, float], *, pad: float = 0.0) -> bool:
    x, y = point
    return rect["x0"] - pad <= x <= rect["x1"] + pad and rect["y0"] - pad <= y <= rect["y1"] + pad


def _union_rect(a: dict[str, float], b: dict[str, float]) -> dict[str, float]:
    x0 = min(float(a.get("x0", 0.0)), float(b.get("x0", 0.0)))
    y0 = min(float(a.get("y0", 0.0)), float(b.get("y0", 0.0)))
    x1 = max(float(a.get("x1", 0.0)), float(b.get("x1", 0.0)))
    y1 = max(float(a.get("y1", 0.0)), float(b.get("y1", 0.0)))
    return {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "w": x1 - x0, "h": y1 - y0}


def _int_box(rect: dict[str, float], width: int, height: int) -> tuple[int, int, int, int]:
    return (
        max(0, min(width, int(float(rect.get("x0", 0.0))))),
        max(0, min(height, int(float(rect.get("y0", 0.0))))),
        max(0, min(width, int(float(rect.get("x1", 0.0)) + 0.999))),
        max(0, min(height, int(float(rect.get("y1", 0.0)) + 0.999))),
    )


def _round_rect_f(rect: dict[str, Any]) -> dict[str, float]:
    return {
        "x0": round(float(rect.get("x0", 0.0)), 2),
        "y0": round(float(rect.get("y0", 0.0)), 2),
        "x1": round(float(rect.get("x1", 0.0)), 2),
        "y1": round(float(rect.get("y1", 0.0)), 2),
        "w": round(float(rect.get("w", float(rect.get("x1", 0.0)) - float(rect.get("x0", 0.0)))), 2),
        "h": round(float(rect.get("h", float(rect.get("y1", 0.0)) - float(rect.get("y0", 0.0)))), 2),
    }


def occupancy_overlay(
    image: Image.Image,
    occ_result: dict[str, Any],
    *,
    show_content: bool = True,
    strips: dict[str, Any] | None = None,
) -> Image.Image:
    """Visualize detection: gray heading band (excluded), green-outline content cells,
    light-red empty cells, strong-red blank strips (section gaps) + largest strip box."""
    canvas = image.convert("RGB").copy()
    draw = ImageDraw.Draw(canvas, "RGBA")
    cols, rows = occ_result["cols"], occ_result["rows"]
    heading_rows = occ_result.get("heading_rows", 0)
    occ = occ_result["occ"]
    w, h = canvas.size
    cw, ch = w / cols, h / rows
    for r in range(rows):
        for c in range(cols):
            box = [c * cw, r * ch, (c + 1) * cw, (r + 1) * ch]
            if r < heading_rows:
                # heading band: excluded from density analysis
                draw.rectangle(box, fill=(150, 150, 150, 28))
            elif not occ[r][c]:
                draw.rectangle(box, fill=(255, 40, 40, 70))
            elif show_content:
                draw.rectangle([box[0] + 1, box[1] + 1, box[2] - 1, box[3] - 1], outline=(0, 170, 0, 130), width=1)
    if heading_rows:
        draw.line([0, heading_rows * ch, w, heading_rows * ch], fill=(90, 90, 90, 200), width=2)
    if strips and strips.get("strip"):
        smask = strips["strip"]
        scw, sch = w / strips["cols"], h / strips["rows"]
        for r in range(strips["rows"]):
            for c in range(strips["cols"]):
                if smask[r][c]:
                    draw.rectangle([c * scw, r * sch, (c + 1) * scw, (r + 1) * sch], fill=(230, 0, 0, 110))
        sbox = strips.get("largest_blank_strip_px")
        if sbox:
            draw.rectangle([sbox["x0"], sbox["y0"], sbox["x1"], sbox["y1"]], outline=(180, 0, 0, 255), width=5)
    else:
        rect = occ_result.get("largest_empty_rect_px")
        if rect:
            draw.rectangle([rect["x0"], rect["y0"], rect["x1"], rect["y1"]], outline=(220, 0, 0, 255), width=5)
    return canvas
