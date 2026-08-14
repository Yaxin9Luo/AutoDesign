"""pymupdf (fitz) helpers used by `tools/ingest_document.py` (v1.2).

v1.1 treated PDFs as raster sources and asked a VLM to guess figure
bboxes on rasterized pages. That was unreliable — produced half-page
crops, clipped diagrams, and hallucinated "figures" on text-only
pages. v1.2 extracts figures directly from PDF structure:

- `extract_embedded_rasters`: pulls embedded images at their native
  resolution via `doc.extract_image(xref)` and records page placement
  bboxes when PyMuPDF exposes them.
- `extract_vector_clusters`: clusters vector `get_drawings()` by
  proximity, renders each cluster at high dpi. Catches architecture
  diagrams + pipeline figures that are stored as vector paths.

The VLM is still used downstream for caption↔figure matching and
fake-figure filtering — not for localization.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import re
from typing import Any

import fitz  # pymupdf
from PIL import Image

from .logging import log


# ─────────────────────────── public types ─────────────────────────────

@dataclass(frozen=True)
class PdfFigureCandidate:
    """A figure candidate extracted from a PDF, on disk as a PNG.

    Produced by `extract_embedded_rasters` or `extract_vector_clusters`.
    Downstream (`tools/ingest_document._ingest_pdf`) dedups across the
    two strategies and then asks a VLM to match captions.
    """
    page: int                                 # 1-indexed page
    bbox_pt: tuple[float, float, float, float] | None  # PDF-point coords
    path: Path                                # absolute PNG path on disk
    width_px: int
    height_px: int
    strategy: str                             # "raster" | "vector"
    xref: int | None                          # PDF xref (raster only)
    protected_anchor: bool = False
    anchor_kind: str = ""
    anchor_label: str = ""
    anchor_reason: str = ""
    captioned_source_group: bool = False
    source_group_id: str = ""
    source_group_kind: str = ""
    source_group_label: str = ""
    source_group_caption: str = ""
    source_group_source: str = ""


@dataclass(frozen=True)
class PdfTableCandidate:
    """A table candidate extracted from a PDF.

    pymupdf `page.find_tables()` is used for **localization only** — its
    cell-level splits are unreliable on paper layouts (we've observed
    headers jammed into one cell, math-equation arrays detected as
    tables, figure diagrams misclassified as tables). Downstream asks a
    VLM to read the cropped region and return clean structured data
    (or reject the candidate as "not a data table").
    """
    page: int
    bbox_pt: tuple[float, float, float, float]
    image_path: Path                          # 300 dpi PNG of the bbox
    width_px: int
    height_px: int
    raw_cells: list[list[str]]                # pymupdf's best-effort split
    nrows: int
    ncols: int
    protected_anchor: bool = False
    anchor_kind: str = ""
    anchor_label: str = ""
    anchor_reason: str = ""
    captioned_source_group: bool = False
    source_group_id: str = ""
    source_group_kind: str = ""
    source_group_label: str = ""
    source_group_caption: str = ""
    source_group_source: str = ""


@dataclass(frozen=True)
class CaptionedVisualGroup:
    """A block-level paper caption and the source asset it describes."""

    kind: str
    label: str
    page: int
    caption_rect: tuple[float, float, float, float]
    caption_text: str
    source: str = "pdf_caption_block"

    @property
    def group_id(self) -> str:
        safe_label = re.sub(r"[^a-zA-Z0-9]+", "_", self.label).strip("_").lower()
        return f"p{int(self.page or 0):03d}:{self.kind}:{safe_label or self.label.lower()}"

    @property
    def source_group_label(self) -> str:
        prefix = "Table" if self.kind == "table" else "Figure"
        return f"{prefix} {self.label}".strip()


PdfCaptionedVisualGroup = CaptionedVisualGroup


class ScannedPdfError(RuntimeError):
    """Raised when a PDF has no embedded images AND no vector drawings
    AND almost no extractable text — i.e. a scanned PDF we cannot
    mine figures from without OCR."""


# ─────────────────────── legacy thin wrappers ─────────────────────────
# Kept because `tools/ingest_document` still uses these on the rare
# fallback path, and `scripts/spike_pdf_figures.py` imports them.

def page_count(pdf_path: Path) -> int:
    """Number of pages in the PDF."""
    doc = fitz.open(pdf_path)
    try:
        return len(doc)
    finally:
        doc.close()


def render_page_png(pdf_path: Path, page_num: int, out_path: Path,
                    dpi: int = 192) -> tuple[int, int]:
    """Render 1-indexed `page_num` to `out_path` as PNG. Returns (width,
    height) of the rendered image in pixels.
    """
    doc = fitz.open(pdf_path)
    try:
        if page_num < 1 or page_num > len(doc):
            raise ValueError(
                f"page_num={page_num} out of range 1..{len(doc)} for {pdf_path.name}"
            )
        pix = doc[page_num - 1].get_pixmap(dpi=dpi)
        pix.save(str(out_path))
        return pix.width, pix.height
    finally:
        doc.close()


def crop_bbox(page_png: Path, bbox: tuple[int, int, int, int],
              out_path: Path) -> tuple[int, int]:
    """Crop `page_png` to `bbox = (x, y, w, h)` and save as PNG."""
    with Image.open(page_png) as img:
        iw, ih = img.size
        x, y, w, h = bbox
        x = max(0, min(x, iw - 1))
        y = max(0, min(y, ih - 1))
        w = max(1, min(w, iw - x))
        h = max(1, min(h, ih - y))
        cropped = img.crop((x, y, x + w, y + h))
        cropped.save(out_path, format="PNG", optimize=True)
        return cropped.width, cropped.height


def probe_pdf(pdf_path: Path) -> dict[str, Any]:
    """Lightweight metadata probe (bytes + page count + first-page size)."""
    data = pdf_path.read_bytes()
    doc = fitz.open(pdf_path)
    try:
        first = doc[0] if len(doc) > 0 else None
        size_pt = (first.rect.width, first.rect.height) if first else (0, 0)
        return {
            "bytes": len(data),
            "pages": len(doc),
            "first_page_size_pt": size_pt,
        }
    finally:
        doc.close()


# ──────────────────────── figure extraction ───────────────────────────

def extract_embedded_rasters(
    doc: fitz.Document,
    out_dir: Path,
    *,
    min_w: int = 120,
    min_h: int = 80,
    min_display_max_side_pt: float = 80.0,
    max_page: int | None = None,
) -> list[PdfFigureCandidate]:
    """Pull every embedded raster image from the PDF at its native
    resolution. Returns one `PdfFigureCandidate` per kept image.

    Dedup: the same xref can appear on many pages (headers, logos,
    footers). We register each xref only once (first page that hosts it).
    Channel fixup: CMYK / palette (P) / grayscale (L) modes are
    converted to RGB so downstream renderers don't corrupt colors.
    Size filter: drops images smaller than `min_w × min_h` (typically
    decorative icons, bullet glyphs, tiny badges).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    records: list[PdfFigureCandidate] = []
    seen_xrefs: set[int] = set()

    for page_num, page in enumerate(doc, start=1):
        if max_page is not None and page_num > max_page:
            break
        placements = _image_placement_bboxes(page)
        for img_info in page.get_images(full=True):
            xref = img_info[0]
            if xref in seen_xrefs:
                continue

            try:
                extracted = doc.extract_image(xref)
            except Exception:
                # corrupt xref — skip quietly, caller logs totals
                continue
            seen_xrefs.add(xref)

            w = int(extracted.get("width", 0))
            h = int(extracted.get("height", 0))
            if w < min_w or h < min_h:
                continue

            placement = placements.get(xref)
            if placement is not None:
                px0, py0, px1, py1 = placement
                display_w = max(0.0, px1 - px0)
                display_h = max(0.0, py1 - py0)
                if max(display_w, display_h) < min_display_max_side_pt:
                    continue

            ext = extracted.get("ext", "png")
            data = extracted["image"]

            raw_path = out_dir / f"_tmp_p{page_num:03d}_xref{xref}.{ext}"
            raw_path.write_bytes(data)
            png_path = out_dir / f"p{page_num:03d}_xref{xref}.png"

            # Normalize to RGB/RGBA PNG so CMYK/L/P don't leak through.
            try:
                with Image.open(raw_path) as im:
                    if im.mode not in ("RGB", "RGBA"):
                        im = im.convert("RGBA" if im.mode in ("LA", "P") else "RGB")
                    im.save(png_path, format="PNG", optimize=True)
                    out_w, out_h = im.size
            except Exception:
                try:
                    raw_path.unlink()
                except OSError:
                    pass
                continue

            try:
                raw_path.unlink()
            except OSError:
                pass

            records.append(PdfFigureCandidate(
                page=page_num,
                bbox_pt=placements.get(xref),
                path=png_path,
                width_px=out_w,
                height_px=out_h,
                strategy="raster",
                xref=xref,
            ))

    return records


def _image_placement_bboxes(page: fitz.Page) -> dict[int, tuple[float, float, float, float]]:
    """Best-effort xref -> page bbox mapping for embedded rasters.

    `page.get_images()` tells us which image objects exist, but not where
    they sit on the page. `get_image_info(xrefs=True)` carries placement
    bboxes in modern PyMuPDF. When unavailable, callers still get the
    native image but cannot dedup it against a full-page figure crop.
    """
    out: dict[int, tuple[float, float, float, float]] = {}
    try:
        infos = page.get_image_info(xrefs=True)
    except Exception:
        return out
    for info in infos:
        if not isinstance(info, dict):
            continue
        try:
            xref = int(info.get("xref") or 0)
        except (TypeError, ValueError):
            continue
        if xref <= 0 or xref in out:
            continue
        bbox = info.get("bbox")
        try:
            rect = fitz.Rect(bbox)
        except Exception:
            continue
        if rect.is_empty or rect.is_infinite:
            continue
        out[xref] = (
            round(rect.x0, 2),
            round(rect.y0, 2),
            round(rect.x1, 2),
            round(rect.y1, 2),
        )
    return out


def _merge_rects(rects: list[fitz.Rect], tol: float) -> list[fitz.Rect]:
    """Union-merge overlapping or near-touching rects. O(n²) — fine for
    <1000 drawings per page. Expand by `tol` before testing overlap so
    arrows, axis ticks, and label boxes bundle into one cluster."""
    merged = [fitz.Rect(r) for r in rects]
    changed = True
    while changed:
        changed = False
        new: list[fitz.Rect] = []
        consumed = [False] * len(merged)
        for i, a in enumerate(merged):
            if consumed[i]:
                continue
            cur = fitz.Rect(a)
            probe = fitz.Rect(cur.x0 - tol, cur.y0 - tol,
                              cur.x1 + tol, cur.y1 + tol)
            for j in range(i + 1, len(merged)):
                if consumed[j]:
                    continue
                if probe.intersects(merged[j]):
                    cur |= merged[j]
                    consumed[j] = True
                    changed = True
                    probe = fitz.Rect(cur.x0 - tol, cur.y0 - tol,
                                      cur.x1 + tol, cur.y1 + tol)
            new.append(cur)
        merged = new
    return merged


def _is_likely_vector_background_rect(
    drawing: dict[str, Any],
    page_rect: fitz.Rect,
    *,
    min_area_frac: float = 0.10,
) -> bool:
    """Skip large light clip/background rectangles before clustering.

    Some PDFs expose an oversized white or pale fill rectangle around a figure
    group. If it participates in proximity merging, the final crop can swallow
    nearby title/body text even though the actual diagram elements are much
    tighter.
    """
    rect_obj = drawing.get("rect")
    if rect_obj is None:
        return False
    rect = fitz.Rect(rect_obj) & page_rect
    if rect.is_empty:
        return False
    page_area = max(1.0, page_rect.width * page_rect.height)
    if (rect.width * rect.height) / page_area < min_area_frac:
        return False

    fill = drawing.get("fill")
    if not fill:
        return False
    channels = [float(channel) for channel in fill[:3]]
    light_neutral_fill = min(channels) >= 0.90 and (max(channels) - min(channels)) <= 0.10
    if not light_neutral_fill:
        return False
    if drawing.get("color") is not None:
        return False

    items = drawing.get("items") or []
    return len(items) == 1 and items[0][0] == "re"


def extract_vector_clusters(
    doc: fitz.Document,
    out_dir: Path,
    *,
    dpi: int = 300,
    min_side_pt: float = 80.0,
    merge_tol_pt: float = 12.0,
    clip_padding_pt: float = 12.0,
    max_area_frac: float = 0.80,
    max_page: int | None = None,
    max_raw_bboxes_per_page: int = 3000,
) -> list[PdfFigureCandidate]:
    """Cluster vector drawings by proximity and render each cluster at
    `dpi`. Filters: drop clusters whose shorter side is < `min_side_pt`
    (horizontal rules, underlines, header bars) or whose area exceeds
    `max_area_frac` of the page (full-page decorative overlays).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    records: list[PdfFigureCandidate] = []

    for page_num, page in enumerate(doc, start=1):
        if max_page is not None and page_num > max_page:
            break
        try:
            drawings = page.get_drawings()
        except Exception as e:  # noqa: BLE001 - skip pathological pages
            log(
                "ingest.pdf.vector_drawings_fail",
                page=page_num,
                error=f"{type(e).__name__}: {e}"[:200],
            )
            continue
        if not drawings:
            continue

        page_area = page.rect.width * page.rect.height
        raw_bboxes = [
            fitz.Rect(d["rect"])
            for d in drawings
            if d.get("rect") is not None
            and not _is_likely_vector_background_rect(d, page.rect)
        ]
        if not raw_bboxes:
            continue
        if max_raw_bboxes_per_page > 0 and len(raw_bboxes) > max_raw_bboxes_per_page:
            log(
                "ingest.pdf.vector_page_skipped",
                page=page_num,
                raw_bboxes=len(raw_bboxes),
                max_raw_bboxes=max_raw_bboxes_per_page,
            )
            continue

        clusters = _merge_rects(raw_bboxes, merge_tol_pt)

        keep: list[fitz.Rect] = []
        for c in clusters:
            if c.width < min_side_pt or c.height < min_side_pt:
                continue
            if (c.width * c.height) / page_area > max_area_frac:
                continue
            keep.append(c)

        for cidx, c in enumerate(keep, start=1):
            clip = fitz.Rect(
                c.x0 - clip_padding_pt,
                c.y0 - clip_padding_pt,
                c.x1 + clip_padding_pt,
                c.y1 + clip_padding_pt,
            ) & page.rect
            png_path = out_dir / f"p{page_num:03d}_vec{cidx:02d}.png"
            try:
                pix = page.get_pixmap(clip=clip, dpi=dpi)
                pix.save(str(png_path))
            except Exception as e:  # noqa: BLE001 - one bad crop is non-fatal
                log(
                    "ingest.pdf.vector_render_fail",
                    page=page_num,
                    bbox=[round(clip.x0, 2), round(clip.y0, 2), round(clip.x1, 2), round(clip.y1, 2)],
                    error=f"{type(e).__name__}: {e}"[:200],
                )
                continue
            records.append(PdfFigureCandidate(
                page=page_num,
                bbox_pt=(round(clip.x0, 2), round(clip.y0, 2),
                         round(clip.x1, 2), round(clip.y1, 2)),
                path=png_path,
                width_px=pix.width,
                height_px=pix.height,
                strategy="vector",
                xref=None,
            ))

    return records


def extract_table_candidates(
    doc: fitz.Document,
    out_dir: Path,
    *,
    dpi: int = 300,
    min_rows: int = 2,
    min_cols: int = 2,
    min_side_pt: float = 60.0,
    max_page: int | None = None,
) -> list[PdfTableCandidate]:
    """Per-page table candidates. pymupdf `page.find_tables()` returns
    TableFinder objects; we take each one as a LOCALIZATION hint only —
    cell splits are often wrong, so the VLM parses the crop separately.

    Filters:
    - drop candidates below `min_rows × min_cols` (often false positives
      on single-row headers or key-value annotation lists),
    - drop candidates with bbox side < `min_side_pt` (tiny layout artifacts),
    - do NOT drop "full-page" candidates here — some papers genuinely
      have page-spanning tables and the VLM decides from content.

    Each candidate is rendered to a PNG at `dpi` so the VLM gets a
    high-resolution view of the actual cells + borders.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    records: list[PdfTableCandidate] = []

    for page_num, page in enumerate(doc, start=1):
        if max_page is not None and page_num > max_page:
            break
        try:
            finder = page.find_tables()
        except Exception as e:
            log_ignore_msg = f"find_tables failed on page {page_num}: {e}"
            # Swallow per-page — some pages (especially heavily-annotated ones)
            # trip internal assertions; they aren't fatal for the document.
            import sys as _sys
            print(log_ignore_msg, file=_sys.stderr)
            continue

        for tidx, tbl in enumerate(finder.tables, start=1):
            rect = fitz.Rect(tbl.bbox)
            if rect.width < min_side_pt or rect.height < min_side_pt:
                continue
            try:
                raw_cells = tbl.extract() or []
            except Exception:
                raw_cells = []
            nrows = len(raw_cells)
            ncols = max((len(r) for r in raw_cells), default=0)
            if nrows < min_rows or ncols < min_cols:
                continue

            png_path = out_dir / f"p{page_num:03d}_tbl{tidx:02d}.png"
            try:
                pix = page.get_pixmap(clip=rect, dpi=dpi)
                pix.save(str(png_path))
            except Exception as e:  # noqa: BLE001 - one bad crop is non-fatal
                log(
                    "ingest.pdf.table_render_fail",
                    page=page_num,
                    bbox=[round(rect.x0, 2), round(rect.y0, 2), round(rect.x1, 2), round(rect.y1, 2)],
                    error=f"{type(e).__name__}: {e}"[:200],
                )
                continue

            # Coerce cells to strings (pymupdf sometimes emits None).
            norm_cells = [[(c if c is not None else "") for c in row]
                          for row in raw_cells]

            records.append(PdfTableCandidate(
                page=page_num,
                bbox_pt=(round(rect.x0, 2), round(rect.y0, 2),
                         round(rect.x1, 2), round(rect.y1, 2)),
                image_path=png_path,
                width_px=pix.width,
                height_px=pix.height,
                raw_cells=norm_cells,
                nrows=nrows,
                ncols=ncols,
            ))

    return records


_CAPTION_LABEL_PATTERN = r"(?:[A-Z]?\d+(?:[A-Za-z])?(?:\([a-z]\))?|[IVXLCDM]+)"
_CAPTION_LABEL_RE = re.compile(
    rf"(?i)\b(?P<kind>fig(?:ure)?\.?|table)\s*(?P<label>{_CAPTION_LABEL_PATTERN})\s*[:.)\-\u2013\u2014]?"
)
_CAPTION_BLOCK_START_RE = re.compile(
    r"(?is)^\s*(?P<kind>fig(?:ure)?\.?|table)\s*"
    rf"(?P<label>{_CAPTION_LABEL_PATTERN})\s*(?P<punc>[:.)\-\u2013\u2014])?\s*(?P<rest>.*)$"
)
_SECONDARY_CAPTION_LABEL_RE = re.compile(
    rf"(?i)\b(?P<kind>fig(?:ure)?\.?|table)\s*"
    rf"(?P<label>{_CAPTION_LABEL_PATTERN})\s*(?P<punc>[:\-\u2013\u2014])\s*"
)
_CAPTION_BODY_MENTION_RE = re.compile(
    r"(?i)^(?:also\s+)?(shows?|illustrates?|depicts?|presents?|reports?|summarizes?|lists?|"
    r"contains?|describes?|compares?|provides?|gives?|demonstrates?|indicates?|"
    r"is|are|was|were|has|have|can|will)\b"
)
_CAPTION_BODY_REFERENCE_RE = re.compile(
    r"(?i)^(?:also|again|further|instead)\s+"
    r"(?:shows?|illustrates?|depicts?|presents?|reports?|summarizes?|lists?|"
    r"contains?|describes?|compares?|provides?|gives?|demonstrates?|indicates?)\b|"
    r"^(?:we|this|these|the|our|it|they)\s+"
    r"(?:show|shows|illustrate|illustrates|present|presents|report|reports|"
    r"summarize|summarizes|compare|compares|provide|provides|use|uses)\b"
)
_INLINE_CAPTION_REFERENCE_START_RE = re.compile(
    rf"(?i)^\s*(?:(?:in|from|see|cf\.?|refer to|according to|as shown in|as reported in)\s+)"
    rf"(?:fig(?:ure)?\.?|table)\s*{_CAPTION_LABEL_PATTERN}\b"
)
_ANCHOR_PRIORITY_KEYWORDS = (
    "overview",
    "architecture",
    "method",
    "pipeline",
    "framework",
    "benchmark",
    "comparison",
    "ablation",
    "results",
    "result",
    "performance",
)


def discover_captioned_visual_groups(
    doc: fitz.Document,
    *,
    manifest: dict[str, Any] | None = None,
    max_page: int | None = None,
) -> list[CaptionedVisualGroup]:
    """Find block-level figure/table captions in the paper body.

    This intentionally ignores inline prose references such as "Table 2 shows"
    because those do not start a PDF text block. Manifest captions only enrich
    text for labels already found in page blocks; they never create a group by
    themselves.
    """

    manifest_captions = _manifest_caption_lookup(manifest or {})
    groups: list[CaptionedVisualGroup] = []
    seen: set[tuple[str, str, int]] = set()
    page_limit = min(len(doc), max_page or len(doc))
    for page_num in range(1, page_limit + 1):
        page = doc[page_num - 1]
        for block in page.get_text("blocks"):
            rect = fitz.Rect(block[:4])
            text = " ".join(str(block[4] or "").split())
            for kind, label, caption_text, caption_rect in _caption_segments_from_block(page, rect, text):
                key = (kind, label, page_num)
                if key in seen:
                    continue
                if _caption_block_is_suspicious(caption_text, caption_rect, page.rect, kind):
                    continue
                seen.add(key)
                caption_text = manifest_captions.get((kind, label, page_num)) or caption_text
                groups.append(CaptionedVisualGroup(
                    kind=kind,
                    label=label,
                    page=page_num,
                    caption_rect=(
                        round(caption_rect.x0, 2),
                        round(caption_rect.y0, 2),
                        round(caption_rect.x1, 2),
                        round(caption_rect.y1, 2),
                    ),
                    caption_text=caption_text,
                ))
    return sorted(
        groups,
        key=lambda item: (
            item.page,
            item.caption_rect[1],
            0 if item.kind == "figure" else 1,
            _anchor_label_number(item.label),
            item.label,
        ),
    )


def _caption_segments_from_block(
    page: fitz.Page,
    block_rect: fitz.Rect,
    text: str,
) -> list[tuple[str, str, str, fitz.Rect]]:
    parsed = _parse_caption_block_text(text)
    if parsed is None:
        return []

    matches = list(_SECONDARY_CAPTION_LABEL_RE.finditer(text))
    if not matches or matches[0].start() > 2:
        kind, label = parsed
        return [(kind, label, text, fitz.Rect(block_rect))]

    segments: list[tuple[str, str, str]] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        segment_text = text[start:end].strip()
        if not segment_text:
            continue
        raw_kind = match.group("kind").lower()
        kind = "table" if raw_kind.startswith("table") else "figure"
        label = str(match.group("label") or "").strip().rstrip(".").lower()
        segments.append((kind, label, segment_text))
    if len(segments) <= 1:
        kind, label = parsed
        return [(kind, label, text, fitz.Rect(block_rect))]

    label_rects = [
        _caption_label_rect(page, block_rect, match, index, len(matches))
        for index, match in enumerate(matches)
    ]
    same_row = max(rect.y0 for rect in label_rects) - min(rect.y0 for rect in label_rects) <= 12.0
    segment_rects: list[fitz.Rect] = []
    for index, label_rect in enumerate(label_rects):
        if same_row:
            x0 = block_rect.x0 if index == 0 else label_rect.x0
            x1 = (
                block_rect.x1
                if index + 1 == len(label_rects)
                else label_rects[index + 1].x0 - 2.0
            )
            segment_rects.append(fitz.Rect(x0, block_rect.y0, x1, block_rect.y1))
        else:
            y0 = block_rect.y0 if index == 0 else (label_rects[index - 1].y0 + label_rect.y0) / 2.0
            y1 = (
                block_rect.y1
                if index + 1 == len(label_rects)
                else (label_rect.y0 + label_rects[index + 1].y0) / 2.0
            )
            segment_rects.append(fitz.Rect(block_rect.x0, y0, block_rect.x1, y1))
    return [
        (kind, label, segment_text, segment_rect)
        for (kind, label, segment_text), segment_rect in zip(segments, segment_rects)
    ]


def _caption_label_rect(
    page: fitz.Page,
    block_rect: fitz.Rect,
    match: re.Match[str],
    index: int,
    count: int,
) -> fitz.Rect:
    needle = f"{match.group('kind').strip()} {match.group('label').strip()}"
    hits = [rect for rect in page.search_for(needle, clip=block_rect) if rect.intersects(block_rect)]
    if hits:
        return min(hits, key=lambda rect: (rect.y0, rect.x0))
    fraction = index / max(1, count)
    return fitz.Rect(
        block_rect.x0 + block_rect.width * fraction,
        block_rect.y0,
        block_rect.x0 + block_rect.width * min(1.0, fraction + 0.08),
        block_rect.y1,
    )


def _manifest_caption_lookup(manifest: dict[str, Any]) -> dict[tuple[str, str, int], str]:
    out: dict[tuple[str, str, int], str] = {}
    for kind, key in (("figure", "figures"), ("table", "tables")):
        for item in list(manifest.get(key) or []):
            if not isinstance(item, dict):
                continue
            caption = str(item.get("caption") or item.get("title") or "").strip()
            label_kind, label = _caption_label(caption)
            if label_kind and label_kind != kind:
                continue
            if not label:
                continue
            try:
                page = int(item.get("page") or 0)
            except (TypeError, ValueError):
                page = 0
            if page <= 0:
                continue
            out[(kind, label, page)] = " ".join(caption.split())
    return out


def _parse_caption_block_text(text: str) -> tuple[str, str] | None:
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return None
    if _INLINE_CAPTION_REFERENCE_START_RE.match(cleaned):
        return None
    match = _CAPTION_BLOCK_START_RE.match(cleaned)
    if not match:
        return None
    rest = str(match.group("rest") or "").strip()
    if len(rest) < 2:
        return None
    if _CAPTION_BODY_REFERENCE_RE.match(rest):
        return None
    if not match.group("punc") and _CAPTION_BODY_MENTION_RE.match(rest):
        return None
    raw_kind = match.group("kind").lower()
    kind = "table" if raw_kind.startswith("table") else "figure"
    label = str(match.group("label") or "").strip().rstrip(".").lower()
    return kind, label


def _caption_block_is_suspicious(
    text: str,
    rect: fitz.Rect,
    page_rect: fitz.Rect,
    kind: str,
) -> bool:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) < 14:
        return True
    # TOCs, lists of figures, and prose snippets often contain many labels in
    # one block. A real caption block should describe one figure/table.
    labels = list(_SECONDARY_CAPTION_LABEL_RE.finditer(cleaned))
    if len(labels) > 1 and len(cleaned) < 180:
        return True
    if re.match(r"(?i)^\s*(?:list of figures|list of tables|contents)\b", cleaned):
        return True
    if rect.y0 < page_rect.height * 0.03 or rect.y1 > page_rect.height * 0.98:
        return True
    words = re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", cleaned)
    if kind == "table":
        return len(words) < 2 and not re.search(r"\d", cleaned)
    return len(words) < 3


def recover_caption_anchored_visuals(
    doc: fitz.Document,
    out_dir: Path,
    *,
    manifest: dict[str, Any] | None = None,
    page_texts: list[str] | None = None,
    figures: list[PdfFigureCandidate] | None = None,
    tables: list[PdfTableCandidate] | None = None,
    max_page: int | None = None,
    max_anchor_pages: int = 10,
) -> tuple[list[PdfFigureCandidate], list[PdfTableCandidate]]:
    """Compatibility wrapper for the generalized captioned source-group pass."""

    return recover_captioned_visual_groups(
        doc,
        out_dir,
        manifest=manifest,
        figures=figures,
        tables=tables,
        max_page=max_page,
    )


def recover_captioned_visual_groups(
    doc: fitz.Document,
    out_dir: Path,
    *,
    manifest: dict[str, Any] | None = None,
    figures: list[PdfFigureCandidate] | None = None,
    tables: list[PdfTableCandidate] | None = None,
    max_page: int | None = None,
) -> tuple[list[PdfFigureCandidate], list[PdfTableCandidate]]:
    """Recover complete source figure/table crops for block-level captions."""

    out_dir.mkdir(parents=True, exist_ok=True)
    figure_candidates = list(figures or [])
    table_candidates = list(tables or [])
    groups = discover_captioned_visual_groups(
        doc,
        manifest=manifest or {},
        max_page=max_page or len(doc),
    )
    if not groups:
        return figure_candidates, table_candidates

    for group in groups:
        page_num = int(group.page)
        if page_num < 1 or page_num > len(doc):
            continue
        if max_page is not None and page_num > max_page:
            continue
        page = doc[page_num - 1]
        caption_rect = fitz.Rect(group.caption_rect)
        if group.kind == "table":
            table_candidates = _protect_or_recover_table_group(
                page,
                out_dir,
                group,
                caption_rect,
                table_candidates,
                figure_candidates,
            )
        else:
            figure_candidates = _protect_or_recover_figure_group(
                page,
                out_dir,
                group,
                caption_rect,
                figure_candidates,
            )
    return figure_candidates, table_candidates


def _anchor_label_number(label: str) -> int:
    m = re.match(r"([0-9]+)", str(label or ""))
    if not m:
        return 999
    try:
        return int(m.group(1))
    except ValueError:
        return 999


def _caption_label(caption: str) -> tuple[str, str]:
    match = _CAPTION_LABEL_RE.search(str(caption or ""))
    if not match:
        return "", ""
    raw_kind = match.group("kind").lower()
    return ("table" if raw_kind.startswith("table") else "figure", match.group("label").lower())


def _protect_or_recover_figure_group(
    page: fitz.Page,
    out_dir: Path,
    group: CaptionedVisualGroup,
    caption_rect: fitz.Rect,
    candidates: list[PdfFigureCandidate],
) -> list[PdfFigureCandidate]:
    bbox, skip_reason = _caption_anchor_visual_bbox_with_reason(page, caption_rect, kind="figure")
    if bbox is None:
        _log_captioned_group_recovery_skip(group, skip_reason)
        return candidates
    claimed_idx = _overlapping_figure_candidate_index(candidates, int(group.page), bbox, threshold=0.82)
    if claimed_idx is not None:
        claimed = candidates[claimed_idx]
        if claimed.captioned_source_group and claimed.source_group_id != group.group_id:
            _log_captioned_group_recovery_skip(group, "no_visual_components")
            return candidates
    idx = _complete_captioned_figure_candidate_index(candidates, int(group.page), bbox, page, caption_rect)
    if idx is not None:
        candidates[idx] = _captioned_source_figure(candidates[idx], group)
        return candidates
    path = out_dir / _group_crop_name(group, "fig")
    rendered = _render_anchor_crop(page, bbox, path)
    if rendered is None:
        return candidates
    width_px, height_px = rendered
    candidates.append(PdfFigureCandidate(
        page=int(group.page),
        bbox_pt=(round(bbox.x0, 2), round(bbox.y0, 2), round(bbox.x1, 2), round(bbox.y1, 2)),
        path=path,
        width_px=width_px,
        height_px=height_px,
        strategy="captioned_group",
        xref=None,
        protected_anchor=True,
        anchor_kind="figure",
        anchor_label=group.label,
        anchor_reason="captioned_source_group",
        captioned_source_group=True,
        source_group_id=group.group_id,
        source_group_kind=group.kind,
        source_group_label=group.source_group_label,
        source_group_caption=group.caption_text,
        source_group_source=group.source,
    ))
    log(
        "ingest.pdf.captioned_group.figure_recovered",
        page=group.page,
        label=group.label,
        path=path.name,
        bbox=[round(v, 2) for v in bbox],
    )
    return candidates


def _protect_or_recover_table_group(
    page: fitz.Page,
    out_dir: Path,
    group: CaptionedVisualGroup,
    caption_rect: fitz.Rect,
    tables: list[PdfTableCandidate],
    figures: list[PdfFigureCandidate],
) -> list[PdfTableCandidate]:
    page_num = int(group.page)
    protected_any = False
    for idx, table in enumerate(tables):
        if table.page != page_num:
            continue
        rect = fitz.Rect(table.bbox_pt)
        if (
            (_table_near_caption(rect, caption_rect) or _table_loose_near_caption(rect, caption_rect))
            and _captioned_table_bbox_has_structure(page, rect, caption_rect)
            and _captioned_table_bbox_quality_ok(page, rect, caption_rect)
        ):
            tables[idx] = _captioned_source_table(table, group)
            protected_any = True
    if protected_any:
        return tables

    hint = _table_hint_for_group(page, group, caption_rect)
    if hint is not None:
        bbox, raw_cells, nrows, ncols = hint
        if _overlapping_table_candidate_index(tables, page_num, bbox) is None:
            path = out_dir / _group_crop_name(group, "tbl")
            rendered = _render_anchor_crop(page, bbox, path)
            if rendered is None:
                return tables
            width_px, height_px = rendered
            tables.append(PdfTableCandidate(
                page=page_num,
                bbox_pt=(round(bbox.x0, 2), round(bbox.y0, 2), round(bbox.x1, 2), round(bbox.y1, 2)),
                image_path=path,
                width_px=width_px,
                height_px=height_px,
                raw_cells=raw_cells,
                nrows=nrows,
                ncols=ncols,
                protected_anchor=True,
                anchor_kind="table",
                anchor_label=group.label,
                anchor_reason="captioned_source_group",
                captioned_source_group=True,
                source_group_id=group.group_id,
                source_group_kind=group.kind,
                source_group_label=group.source_group_label,
                source_group_caption=group.caption_text,
                source_group_source=group.source,
            ))
            log(
                "ingest.pdf.captioned_group.table_hint_recovered",
                page=group.page,
                label=group.label,
                path=path.name,
                bbox=[round(v, 2) for v in bbox],
            )
            return tables

    bbox, skip_reason = _caption_anchor_visual_bbox_with_reason(page, caption_rect, kind="table")
    if bbox is None:
        _log_captioned_group_recovery_skip(group, skip_reason)
        return tables
    overlap_idx = _overlapping_table_candidate_index(tables, page_num, bbox)
    if overlap_idx is not None:
        existing = fitz.Rect(tables[overlap_idx].bbox_pt)
        if (
            _rect_coverage_of_target(existing, bbox) >= 0.82
            and _captioned_table_bbox_quality_ok(page, existing, caption_rect)
        ):
            tables[overlap_idx] = _captioned_source_table(tables[overlap_idx], group)
            return tables
    if _overlapping_figure_candidate_index(figures, page_num, bbox) is not None:
        # Still recover protected tables. Figure overlap is handled later by
        # table dedup, which now keeps protected anchors.
        pass
    path = out_dir / _group_crop_name(group, "tbl")
    rendered = _render_anchor_crop(page, bbox, path)
    if rendered is None:
        return tables
    width_px, height_px = rendered
    tables.append(PdfTableCandidate(
        page=page_num,
        bbox_pt=(round(bbox.x0, 2), round(bbox.y0, 2), round(bbox.x1, 2), round(bbox.y1, 2)),
        image_path=path,
        width_px=width_px,
        height_px=height_px,
        raw_cells=[],
        nrows=0,
        ncols=0,
        protected_anchor=True,
        anchor_kind="table",
        anchor_label=group.label,
        anchor_reason="captioned_source_group",
        captioned_source_group=True,
        source_group_id=group.group_id,
        source_group_kind=group.kind,
        source_group_label=group.source_group_label,
        source_group_caption=group.caption_text,
        source_group_source=group.source,
    ))
    log(
        "ingest.pdf.captioned_group.table_recovered",
        page=group.page,
        label=group.label,
        path=path.name,
        bbox=[round(v, 2) for v in bbox],
    )
    return tables


def _captioned_source_figure(candidate: PdfFigureCandidate, group: CaptionedVisualGroup) -> PdfFigureCandidate:
    return replace(
        candidate,
        protected_anchor=True,
        anchor_kind="figure",
        anchor_label=group.label,
        anchor_reason="captioned_source_group",
        captioned_source_group=True,
        source_group_id=group.group_id,
        source_group_kind=group.kind,
        source_group_label=group.source_group_label,
        source_group_caption=group.caption_text,
        source_group_source=group.source,
    )


def _captioned_source_table(candidate: PdfTableCandidate, group: CaptionedVisualGroup) -> PdfTableCandidate:
    return replace(
        candidate,
        protected_anchor=True,
        anchor_kind="table",
        anchor_label=group.label,
        anchor_reason="captioned_source_group",
        captioned_source_group=True,
        source_group_id=group.group_id,
        source_group_kind=group.kind,
        source_group_label=group.source_group_label,
        source_group_caption=group.caption_text,
        source_group_source=group.source,
    )


def _caption_anchor_visual_bbox(
    page: fitz.Page,
    caption_rect: fitz.Rect,
    *,
    kind: str,
) -> fitz.Rect | None:
    bbox, _reason = _caption_anchor_visual_bbox_with_reason(page, caption_rect, kind=kind)
    return bbox


def _caption_anchor_visual_bbox_with_reason(
    page: fitz.Page,
    caption_rect: fitz.Rect,
    *,
    kind: str,
) -> tuple[fitz.Rect | None, str]:
    margin = 24.0
    page_rect = page.rect
    x0, x1 = _caption_search_x_bounds(caption_rect, page_rect, margin=margin)
    if kind == "table":
        if caption_rect.width < page_rect.width * 0.45:
            x0, x1 = margin, page_rect.width - margin
        search_regions = _caption_table_search_regions(
            page,
            caption_rect,
            page_rect,
            x0=x0,
            x1=x1,
            margin=margin,
            distance=360.0,
        )
    else:
        above = fitz.Rect(
            x0,
            max(margin, caption_rect.y0 - 360.0),
            x1,
            max(margin + 1.0, caption_rect.y0 - 3.0),
        )
        below = fitz.Rect(
            x0,
            min(page_rect.height - margin - 1.0, caption_rect.y1 + 3.0),
            x1,
            min(page_rect.height - margin, caption_rect.y1 + 360.0),
        )
        search_regions = _caption_figure_side_search_regions(
            caption_rect,
            page_rect,
            margin=margin,
        )
        search_regions.extend([above, below])
        if (
            (x0 > margin + 1.0 or x1 < page_rect.width - margin - 1.0)
            and not _has_peer_figure_caption_on_same_row(page, caption_rect)
        ):
            search_regions.extend([
                fitz.Rect(margin, above.y0, page_rect.width - margin, above.y1),
                fitz.Rect(margin, below.y0, page_rect.width - margin, below.y1),
            ])
        side_region_count = len(_caption_figure_side_search_regions(
            caption_rect,
            page_rect,
            margin=margin,
        ))
        search_regions = [
            region
            for region in search_regions[:side_region_count]
            if not region.is_empty
        ] + [
            clipped
            for region, above_caption in (
                (region, region.y1 <= caption_rect.y0)
                for region in search_regions[side_region_count:]
            )
            if (
                clipped := _truncate_table_search_region_at_boundaries(
                    page,
                    region,
                    caption_rect,
                    above_caption=above_caption,
                )
            ) is not None
        ]
    saw_visual_components = False
    saw_boundary_collision = False
    saw_hard_contamination = False
    for region in search_regions:
        table_choices: list[tuple[float, fitz.Rect]] = []
        visual_rects = (
            _table_rects_in_region(page, region)
            if kind == "table"
            else _visual_rects_in_region(page, region)
        )
        if kind == "table":
            visual_rects = _select_table_rect_cluster(visual_rects, caption_rect, region)
        else:
            visual_rects = _select_figure_rect_cluster(visual_rects, caption_rect, region, page_rect)
        if not visual_rects:
            continue
        saw_visual_components = True
        bbox = _union_rects(visual_rects)
        if bbox is None:
            continue
        if kind != "table":
            bbox = _include_nearby_label_text(page, bbox, region)
            bbox = _trim_page_furniture_from_bbox(page, bbox, caption_rect)
            bbox = _trim_caption_or_body_from_figure_bbox(page, bbox, caption_rect)
        else:
            bbox = _trim_captioned_table_bbox(page, bbox, caption_rect, region)
            if bbox is None:
                continue
        pad = 4.0 if kind == "table" else 18.0
        if kind == "table" and not _captioned_table_bbox_has_structure(page, bbox, caption_rect):
            continue
        if kind == "table":
            bbox = fitz.Rect(bbox.x0 - pad, bbox.y0 - pad, bbox.x1 + pad, bbox.y1 + pad) & page_rect
        else:
            padded, boundary_collision = _boundary_aware_figure_padding(
                page,
                bbox,
                caption_rect,
                pad=pad,
            )
            bbox = padded
            if boundary_collision and not _anchor_bbox_usable(bbox, page.rect, kind="figure"):
                saw_boundary_collision = True
                continue
        if kind != "table":
            bbox = _trim_page_furniture_from_bbox(page, bbox, caption_rect)
            bbox = _trim_caption_or_body_from_figure_bbox(page, bbox, caption_rect)
        if kind == "table":
            if _captioned_table_bbox_quality_ok(page, bbox, caption_rect):
                table_choices.append((_score_captioned_table_bbox(page, bbox, caption_rect), bbox))
            else:
                saw_hard_contamination = True
            if table_choices:
                return max(table_choices, key=lambda item: item[0])[1], ""
            continue
        hard_flags = _captioned_bbox_hard_flags(page, bbox, kind=kind, caption_rect=caption_rect)
        if not hard_flags:
            return bbox, ""
        saw_hard_contamination = True
    if saw_boundary_collision:
        return None, "text_boundary_collision"
    if saw_hard_contamination:
        return None, "hard_crop_contamination"
    if saw_visual_components:
        return None, "hard_crop_contamination"
    return None, "no_visual_components"


def _boundary_aware_figure_padding(
    page: fitz.Page,
    bbox: fitz.Rect,
    caption_rect: fitz.Rect,
    *,
    pad: float,
) -> tuple[fitz.Rect, bool]:
    padded = fitz.Rect(bbox.x0 - pad, bbox.y0 - pad, bbox.x1 + pad, bbox.y1 + pad) & page.rect
    collided = False
    boundaries: list[fitz.Rect] = [fitz.Rect(caption_rect)]
    for block in page.get_text("blocks"):
        rect = fitz.Rect(block[:4])
        if rect.intersects(caption_rect):
            continue
        text = " ".join(str(block[4] or "").split())
        if _table_text_boundary_kind(text) in {
            "body_text",
            "section_heading",
            "figure_caption",
            "table_caption",
            "algorithm_caption",
        }:
            boundaries.append(rect)

    for boundary in boundaries:
        if _horizontal_overlap(bbox, boundary) >= 0.10:
            if boundary.y1 <= bbox.y0 and boundary.y1 >= padded.y0 - 1.0:
                padded.y0 = max(padded.y0, boundary.y1 + 2.0)
                collided = True
            elif boundary.y0 >= bbox.y1 and boundary.y0 <= padded.y1 + 1.0:
                padded.y1 = min(padded.y1, boundary.y0 - 2.0)
                collided = True
        if _vertical_overlap(bbox, boundary) >= 0.20:
            if boundary.x1 <= bbox.x0 and boundary.x1 >= padded.x0 - 1.0:
                padded.x0 = max(padded.x0, boundary.x1 + 2.0)
                collided = True
            elif boundary.x0 >= bbox.x1 and boundary.x0 <= padded.x1 + 1.0:
                padded.x1 = min(padded.x1, boundary.x0 - 2.0)
                collided = True
    if not _anchor_bbox_usable(padded, page.rect, kind="figure"):
        return padded, True
    return padded, collided


def _log_captioned_group_recovery_skip(group: CaptionedVisualGroup, reason: str) -> None:
    log(
        "ingest.pdf.captioned_group.recovery_skipped",
        page=group.page,
        kind=group.kind,
        label=group.label,
        reason=reason or "hard_crop_contamination",
    )


def _caption_search_x_bounds(
    caption_rect: fitz.Rect,
    page_rect: fitz.Rect,
    *,
    margin: float,
) -> tuple[float, float]:
    if caption_rect.width >= page_rect.width * 0.62:
        return margin, page_rect.width - margin
    center = (caption_rect.x0 + caption_rect.x1) / 2.0
    gutter = 26.0
    if center <= page_rect.width * 0.40:
        return margin, min(page_rect.width - margin, page_rect.width / 2.0 + gutter)
    if center >= page_rect.width * 0.60:
        return max(margin, page_rect.width / 2.0 - gutter), page_rect.width - margin
    return margin, page_rect.width - margin


def _caption_figure_side_search_regions(
    caption_rect: fitz.Rect,
    page_rect: fitz.Rect,
    *,
    margin: float,
) -> list[fitz.Rect]:
    """Search beside narrow outer-column captions used by journal layouts."""
    if caption_rect.width > page_rect.width * 0.38:
        return []
    center = (caption_rect.x0 + caption_rect.x1) / 2.0
    y0 = max(margin, caption_rect.y0 - 28.0)
    y1 = min(
        page_rect.height - margin,
        caption_rect.y1 + max(240.0, caption_rect.height * 1.20),
    )
    if center >= page_rect.width * 0.62 and caption_rect.x0 - margin >= 120.0:
        return [fitz.Rect(margin, y0, caption_rect.x0 - 3.0, y1)]
    if center <= page_rect.width * 0.38 and page_rect.width - margin - caption_rect.x1 >= 120.0:
        return [fitz.Rect(caption_rect.x1 + 3.0, y0, page_rect.width - margin, y1)]
    return []


def _has_peer_figure_caption_on_same_row(page: fitz.Page, caption_rect: fitz.Rect) -> bool:
    for block in page.get_text("blocks"):
        block_rect = fitz.Rect(block[:4])
        text = " ".join(str(block[4] or "").split())
        for kind, _label, _caption, peer_rect in _caption_segments_from_block(page, block_rect, text):
            if kind != "figure" or _rect_coverage_of_target(peer_rect, caption_rect) >= 0.85:
                continue
            vertical_gap = max(0.0, peer_rect.y0 - caption_rect.y1, caption_rect.y0 - peer_rect.y1)
            if vertical_gap > 24.0:
                continue
            if peer_rect.x1 <= caption_rect.x0 or peer_rect.x0 >= caption_rect.x1:
                return True
    return False


def _caption_table_search_regions(
    page: fitz.Page,
    caption_rect: fitz.Rect,
    page_rect: fitz.Rect,
    *,
    x0: float,
    x1: float,
    margin: float,
    distance: float,
) -> list[fitz.Rect]:
    above = fitz.Rect(
        x0,
        max(margin, caption_rect.y0 - distance),
        x1,
        max(margin + 1.0, caption_rect.y0 - 3.0),
    )
    below = fitz.Rect(
        x0,
        min(page_rect.height - margin - 1.0, caption_rect.y1 + 3.0),
        x1,
        min(page_rect.height - margin, caption_rect.y1 + distance),
    )
    regions: list[fitz.Rect] = []
    for region, above_caption in ((above, True), (below, False)):
        clipped = _truncate_table_search_region_at_boundaries(
            page,
            region,
            caption_rect,
            above_caption=above_caption,
        )
        if clipped is not None:
            regions.append(clipped)
    return regions


def _truncate_table_search_region_at_boundaries(
    page: fitz.Page,
    region: fitz.Rect,
    caption_rect: fitz.Rect,
    *,
    above_caption: bool,
) -> fitz.Rect | None:
    region = fitz.Rect(region)
    if region.is_empty or region.height < 18.0:
        return None
    boundaries = _table_boundary_blocks_in_region(page, region, caption_rect)
    if above_caption:
        blockers = [
            rect for rect, _reason in boundaries
            if rect.y1 <= caption_rect.y0 + 1.0
        ]
        if blockers:
            closest = max(blockers, key=lambda rect: rect.y1)
            region.y0 = max(region.y0, closest.y1 + 2.0)
    else:
        blockers = [
            rect for rect, _reason in boundaries
            if rect.y0 >= caption_rect.y1 - 1.0
        ]
        if blockers:
            closest = min(blockers, key=lambda rect: rect.y0)
            region.y1 = min(region.y1, closest.y0 - 2.0)
    if region.is_empty or region.height < 18.0:
        return None
    return region


def _select_figure_rect_cluster(
    rects: list[fitz.Rect],
    caption_rect: fitz.Rect,
    region: fitz.Rect,
    page_rect: fitz.Rect,
    *,
    merge_tol: float = 28.0,
) -> list[fitz.Rect]:
    if not rects:
        return []
    cleaned: list[fitz.Rect] = []
    for rect in rects:
        clipped = fitz.Rect(rect) & region
        if clipped.is_empty:
            continue
        if _is_likely_page_furniture_rect(clipped, page_rect):
            continue
        cleaned.append(clipped)
    if not cleaned:
        return []

    clusters = _merge_rects(cleaned, merge_tol)
    clusters = _expanded_figure_cluster_boxes(clusters, caption_rect, region, page_rect)
    below = region.y0 >= caption_rect.y1 - 1.0
    page_area = max(1.0, page_rect.width * page_rect.height)
    caption_center = (caption_rect.x0 + caption_rect.x1) / 2.0

    def score(box: fitz.Rect) -> tuple[float, float, float]:
        if below:
            distance = max(0.0, box.y0 - caption_rect.y1)
        else:
            distance = max(0.0, caption_rect.y0 - box.y1)
        area_frac = min(0.18, (box.width * box.height) / page_area)
        horizontal = _horizontal_overlap(box, caption_rect)
        box_center = (box.x0 + box.x1) / 2.0
        center_penalty = abs(box_center - caption_center) / max(1.0, page_rect.width)
        primary = (
            horizontal * 150.0
            + area_frac * 700.0
            + min(180.0, box.height) * 0.25
            - distance * 1.15
            - center_penalty * 70.0
        )
        return (primary, area_frac, -distance)

    usable = [box for box in clusters if _anchor_bbox_usable(box, page_rect, kind="figure")]
    if not usable:
        usable = [box for box in clusters if box.width >= 30.0 and box.height >= 18.0]
    if not usable:
        return []
    return [max(usable, key=score)]


def _expanded_figure_cluster_boxes(
    clusters: list[fitz.Rect],
    caption_rect: fitz.Rect,
    region: fitz.Rect,
    page_rect: fitz.Rect,
) -> list[fitz.Rect]:
    expanded: list[fitz.Rect] = []
    for cluster in clusters:
        group = fitz.Rect(cluster)
        changed = True
        while changed:
            changed = False
            for other in clusters:
                if other == cluster or _rect_coverage_of_target(group, other) >= 0.98:
                    continue
                if not _figure_clusters_belong_together(group, other, caption_rect, region, page_rect):
                    continue
                group |= other
                changed = True
        expanded.append(group)
    deduped: list[fitz.Rect] = []
    for box in expanded:
        if any(_rect_coverage_of_target(existing, box) >= 0.98 for existing in deduped):
            continue
        deduped = [
            existing
            for existing in deduped
            if _rect_coverage_of_target(box, existing) < 0.98
        ]
        deduped.append(box)
    return deduped


def _figure_clusters_belong_together(
    a: fitz.Rect,
    b: fitz.Rect,
    caption_rect: fitz.Rect,
    region: fitz.Rect,
    page_rect: fitz.Rect,
) -> bool:
    union = fitz.Rect(a)
    union |= b
    if _crop_looks_page_like(union, page_rect):
        return False
    if not _anchor_bbox_usable(union, page_rect, kind="figure"):
        return False

    x_gap = max(0.0, max(a.x0, b.x0) - min(a.x1, b.x1))
    y_gap = max(0.0, max(a.y0, b.y0) - min(a.y1, b.y1))
    same_row = (
        (_vertical_overlap(a, b) >= 0.22 or abs(_rect_center_y(a) - _rect_center_y(b)) <= 42.0)
        and x_gap <= max(72.0, page_rect.width * 0.13)
    )
    same_column = (
        (_horizontal_overlap(a, b) >= 0.18 or abs(_rect_center_x(a) - _rect_center_x(b)) <= 72.0)
        and y_gap <= max(58.0, page_rect.height * 0.075)
    )
    if not (same_row or same_column):
        return False

    below = region.y0 >= caption_rect.y1 - 1.0
    dist_a = _visual_distance_to_caption(a, caption_rect, below=below)
    dist_b = _visual_distance_to_caption(b, caption_rect, below=below)
    return abs(dist_a - dist_b) <= 135.0 or min(dist_a, dist_b) <= 90.0


def _select_table_rect_cluster(
    rects: list[fitz.Rect],
    caption_rect: fitz.Rect,
    region: fitz.Rect,
    *,
    max_gap: float = 24.0,
) -> list[fitz.Rect]:
    if not rects:
        return []
    ordered = sorted(rects, key=lambda rect: (rect.y0, rect.x0))
    clusters: list[list[fitz.Rect]] = []
    current: list[fitz.Rect] = []
    current_bottom: float | None = None
    for rect in ordered:
        if not current or current_bottom is None or rect.y0 - current_bottom <= max_gap:
            current.append(rect)
            current_bottom = max(current_bottom if current_bottom is not None else rect.y1, rect.y1)
            continue
        clusters.append(current)
        current = [rect]
        current_bottom = rect.y1
    if current:
        clusters.append(current)

    below = region.y0 >= caption_rect.y1 - 1.0

    def cluster_box(cluster: list[fitz.Rect]) -> fitz.Rect:
        box = fitz.Rect(cluster[0])
        for item in cluster[1:]:
            box |= item
        return box

    def score(cluster: list[fitz.Rect]) -> tuple[float, float, int]:
        box = cluster_box(cluster)
        distance = abs(box.y0 - caption_rect.y1) if below else abs(caption_rect.y0 - box.y1)
        return (-distance, box.height, len(cluster))

    return max(clusters, key=score)


def _trim_captioned_table_bbox(
    page: fitz.Page,
    bbox: fitz.Rect,
    caption_rect: fitz.Rect,
    region: fitz.Rect,
) -> fitz.Rect | None:
    trimmed = fitz.Rect(bbox) & region
    if trimmed.is_empty:
        return None

    for boundary, _reason in _table_boundary_blocks_in_region(page, trimmed, caption_rect):
        if not boundary.intersects(trimmed):
            continue
        middle = (trimmed.y0 + trimmed.y1) / 2.0
        boundary_middle = (boundary.y0 + boundary.y1) / 2.0
        if boundary_middle <= middle:
            trimmed.y0 = max(trimmed.y0, boundary.y1 + 2.0)
        else:
            trimmed.y1 = min(trimmed.y1, boundary.y0 - 2.0)
        if trimmed.is_empty or trimmed.height < 30.0:
            return None

    components = _table_component_rects_in_bbox(page, trimmed)
    component_box = _union_rects(components)
    if component_box is None:
        return trimmed if not pdf_table_crop_quality_flags(page, trimmed, caption_rect) else None

    wide_rules = [
        rect for rect in components
        if rect.width >= max(80.0, trimmed.width * 0.42) and rect.height <= 6.0
    ]
    pad_x = 4.0
    pad_y = 2.0
    if wide_rules:
        trimmed = fitz.Rect(
            max(region.x0, component_box.x0 - pad_x),
            max(region.y0, component_box.y0 - pad_y),
            min(region.x1, component_box.x1 + pad_x),
            min(region.y1, component_box.y1 + pad_y),
        )
    else:
        trimmed = fitz.Rect(
            trimmed.x0,
            max(region.y0, component_box.y0 - pad_y),
            trimmed.x1,
            min(region.y1, component_box.y1 + pad_y),
        )
    if trimmed.is_empty or trimmed.width < 80.0 or trimmed.height < 30.0:
        return None
    return trimmed


def _table_component_rects_in_bbox(page: fitz.Page, bbox: fitz.Rect) -> list[fitz.Rect]:
    components: list[fitz.Rect] = []
    for drawing in page.get_drawings():
        rect_obj = drawing.get("rect")
        if rect_obj is None:
            continue
        rect = _nonempty_rect(rect_obj) & bbox
        if rect.is_empty or not _looks_like_table_rule_rect(rect):
            continue
        components.append(rect)
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines") or []:
            spans = line.get("spans") or []
            text = " ".join(str(span.get("text") or "") for span in spans).strip()
            if not text:
                continue
            if _table_text_boundary_kind(text):
                continue
            if not (_looks_like_table_line(text) or _looks_like_table_cell_text(text)):
                continue
            try:
                rect = fitz.Rect(line.get("bbox")) & bbox
            except Exception:
                continue
            if rect.is_empty:
                continue
            components.append(rect)
    return components


def _captioned_bbox_hard_flags(
    page: fitz.Page,
    bbox: fitz.Rect,
    *,
    kind: str,
    caption_rect: fitz.Rect | None,
) -> list[str]:
    if not _anchor_bbox_usable(bbox, page.rect, kind=kind):
        return ["unusable_bbox"]
    if kind == "table":
        flags = pdf_table_crop_quality_flags(page, bbox, caption_rect=caption_rect)
        return [flag for flag in flags if flag in _TABLE_CROP_HARD_REJECT_FLAGS]
    flags = pdf_figure_crop_quality_flags(page, bbox, caption_rect=caption_rect)
    return [flag for flag in flags if flag in _FIGURE_CROP_HARD_REJECT_FLAGS]


_FIGURE_CROP_HARD_REJECT_FLAGS = {
    "body_text_leak",
    "caption_in_crop",
    "caption_strip_leak",
    "edge_visual_remnant",
    "figure_edge_text_clipping",
    "header_band_leak",
    "neighbor_asset_leak",
    "partial_visual_crop",
    "page_like_figure_crop",
    "running_header_leak",
    "section_heading_leak",
}
_TABLE_CROP_HARD_REJECT_FLAGS = {
    "algorithm_caption_leak",
    "body_text_leak",
    "figure_caption_leak",
    "header_band_leak",
    "other_caption_in_crop",
    "page_like_table_crop",
    "running_header_leak",
    "section_heading_leak",
    "table_fragment_crop",
    "table_missing_header_context",
    "table_open_left_context",
    "table_open_top_context",
    "table_partial_row_strip",
    "table_without_structure",
}


def pdf_figure_crop_quality_flags(
    page: fitz.Page,
    bbox: fitz.Rect,
    caption_rect: fitz.Rect | None = None,
) -> list[str]:
    """Local crop-quality flags for caption-anchored figure bboxes."""

    if bbox.is_empty:
        return []
    flags: list[str] = []
    if _crop_looks_page_like(bbox, page.rect):
        _append_flag(flags, "page_like_figure_crop")
    if _captioned_crop_has_page_furniture_leak(page, bbox, caption_rect):
        _append_flag(flags, "running_header_leak")
    for flag in _captioned_crop_text_quality_flags(page, bbox, caption_rect, kind="figure"):
        _append_flag(flags, flag)
    if _captioned_crop_has_edge_visual_remnant(page, bbox, caption_rect):
        _append_flag(flags, "edge_visual_remnant")
    if _captioned_crop_has_figure_edge_text_clipping(page, bbox, caption_rect):
        _append_flag(flags, "figure_edge_text_clipping")
        _append_flag(flags, "partial_visual_crop")

    crop_area_frac = (bbox.width * bbox.height) / max(1.0, page.rect.width * page.rect.height)
    if "body_text_leak" in flags and crop_area_frac >= 0.10:
        _append_flag(flags, "page_like_figure_crop")
    return flags


def pdf_table_crop_quality_flags(
    page: fitz.Page,
    bbox: fitz.Rect,
    caption_rect: fitz.Rect | None = None,
) -> list[str]:
    """Local crop-quality flags for caption-anchored table bboxes.

    Kept in `util.pdf` so table localization can reject bad crops before the
    VLM table parser, and so ingest can later expose the same evidence as
    candidate metadata without reimplementing PDF geometry checks.
    """

    if bbox.is_empty:
        return []
    flags: list[str] = []
    if _crop_looks_page_like(bbox, page.rect):
        _append_flag(flags, "page_like_table_crop")
    if _captioned_crop_has_page_furniture_leak(page, bbox, caption_rect):
        _append_flag(flags, "running_header_leak")
    for flag in _captioned_crop_text_quality_flags(page, bbox, caption_rect, kind="table"):
        _append_flag(flags, flag)

    prose_blocks = 0
    foreign_caption_blocks = 0
    crop_area_frac = (bbox.width * bbox.height) / max(1.0, page.rect.width * page.rect.height)
    for block in page.get_text("blocks"):
        rect = fitz.Rect(block[:4])
        if not rect.intersects(bbox):
            continue
        if _horizontal_overlap(bbox, rect) < 0.18:
            continue
        text = " ".join(str(block[4] or "").split())
        if not text:
            continue

        parsed_caption = _parse_caption_block_text(text)
        if parsed_caption is not None:
            if caption_rect is not None and rect.intersects(caption_rect):
                if _rect_overlap_height(rect, bbox) >= min(8.0, max(1.0, rect.height * 0.25)):
                    _append_flag(flags, "caption_in_crop")
                continue
            foreign_caption_blocks += 1
            if parsed_caption[0] == "figure":
                _append_flag(flags, "figure_caption_leak")
            else:
                _append_flag(flags, "other_caption_in_crop")
            continue

        boundary = _table_text_boundary_kind(text)
        if boundary == "algorithm_caption":
            _append_flag(flags, "algorithm_caption_leak")
            continue
        if boundary == "section_heading":
            _append_flag(flags, "section_heading_leak")
            continue
        if boundary == "body_text":
            prose_blocks += 1
            continue
        if _looks_like_prose_block(text):
            prose_blocks += 1

    if foreign_caption_blocks >= 2:
        _append_flag(flags, "multi_caption_leak")
    if prose_blocks:
        _append_flag(flags, "body_text_leak")
    if prose_blocks >= 2 or (prose_blocks >= 1 and crop_area_frac >= 0.18):
        _append_flag(flags, "page_like_table_crop")
    signals = _table_crop_signal_counts(page, bbox, caption_rect)
    has_structure = _table_crop_has_enough_structure_signal(signals)
    if not has_structure:
        _append_flag(flags, "table_without_structure")
    if signals.get("prose_lines", 0) and not has_structure:
        _append_flag(flags, "body_text_leak")
        _append_flag(flags, "page_like_table_crop")
    if signals.get("prose_lines", 0) >= 2:
        _append_flag(flags, "body_text_leak")
        _append_flag(flags, "page_like_table_crop")
    completeness_flags = _table_crop_completeness_flags(page, bbox, caption_rect, signals)
    for flag in completeness_flags:
        _append_flag(flags, flag)
    if completeness_flags:
        _append_flag(flags, "table_fragment_crop")
    if _table_crop_looks_fragmentary(signals, bbox, page.rect):
        _append_flag(flags, "table_fragment_crop")
    return flags


def _append_flag(flags: list[str], flag: str) -> None:
    if flag not in flags:
        flags.append(flag)


def _captioned_crop_text_quality_flags(
    page: fitz.Page,
    bbox: fitz.Rect,
    caption_rect: fitz.Rect | None = None,
    *,
    kind: str,
) -> list[str]:
    flags: list[str] = []
    prose_blocks = 0
    edge_prose_blocks = 0
    section_heading_blocks = 0
    header_band_blocks = 0
    foreign_caption_blocks = 0
    own_caption_blocks = 0
    for block in page.get_text("blocks"):
        rect = fitz.Rect(block[:4])
        clipped = rect & bbox
        if clipped.is_empty:
            continue
        if _horizontal_overlap(bbox, rect) < 0.10 and clipped.width < min(32.0, bbox.width * 0.18):
            continue
        text = " ".join(str(block[4] or "").split())
        if not text:
            continue

        parsed_caption = _parse_caption_block_text(text)
        own_caption = caption_rect is not None and rect.intersects(caption_rect)
        if parsed_caption is not None:
            if own_caption:
                if _rect_overlap_height(rect, bbox) >= min(8.0, max(1.0, rect.height * 0.22)):
                    own_caption_blocks += 1
                    if _rect_touches_crop_edge(clipped, bbox) or _rect_is_clipped_by_bbox(rect, bbox):
                        _append_flag(flags, "caption_strip_leak")
                continue
            foreign_caption_blocks += 1
            if kind == "table":
                _append_flag(flags, "figure_caption_leak" if parsed_caption[0] == "figure" else "other_caption_in_crop")
            else:
                _append_flag(flags, "neighbor_asset_leak")
            continue

        boundary = _table_text_boundary_kind(text)
        if boundary in {"figure_caption", "table_caption"}:
            if own_caption:
                own_caption_blocks += 1
                if _rect_touches_crop_edge(clipped, bbox) or _rect_is_clipped_by_bbox(rect, bbox):
                    _append_flag(flags, "caption_strip_leak")
            else:
                foreign_caption_blocks += 1
                if kind == "table":
                    _append_flag(flags, "figure_caption_leak" if boundary == "figure_caption" else "other_caption_in_crop")
                else:
                    _append_flag(flags, "neighbor_asset_leak")
            continue
        if boundary == "algorithm_caption":
            _append_flag(flags, "algorithm_caption_leak")
            continue

        if _looks_like_header_band_text(page, text, rect, bbox):
            header_band_blocks += 1
            continue
        if boundary == "section_heading":
            section_heading_blocks += 1
            continue
        if boundary == "body_text" or _looks_like_prose_block(text):
            prose_blocks += 1
            if _rect_touches_crop_edge(clipped, bbox) or _rect_is_clipped_by_bbox(rect, bbox):
                edge_prose_blocks += 1
            continue
        if (
            _looks_like_paragraph_fragment_text(text)
            and (_rect_touches_crop_edge(clipped, bbox) or _rect_is_clipped_by_bbox(rect, bbox))
            and not _text_rect_has_visual_context(page, rect, bbox)
        ):
            edge_prose_blocks += 1

    if own_caption_blocks:
        _append_flag(flags, "caption_in_crop")
    if foreign_caption_blocks >= 2:
        _append_flag(flags, "multi_caption_leak")
    if prose_blocks or edge_prose_blocks:
        _append_flag(flags, "body_text_leak")
    if section_heading_blocks:
        _append_flag(flags, "section_heading_leak")
    if header_band_blocks:
        _append_flag(flags, "header_band_leak")
    return flags


def _crop_edge_band(bbox: fitz.Rect) -> float:
    return max(8.0, min(24.0, min(bbox.width, bbox.height) * 0.08))


def _rect_touches_crop_edge(rect: fitz.Rect, bbox: fitz.Rect, band: float | None = None) -> bool:
    edge = _crop_edge_band(bbox) if band is None else band
    return (
        rect.x0 <= bbox.x0 + edge
        or rect.x1 >= bbox.x1 - edge
        or rect.y0 <= bbox.y0 + edge
        or rect.y1 >= bbox.y1 - edge
    )


def _rect_is_clipped_by_bbox(rect: fitz.Rect, bbox: fitz.Rect, *, tolerance: float = 1.5) -> bool:
    return (
        rect.x0 < bbox.x0 - tolerance
        or rect.x1 > bbox.x1 + tolerance
        or rect.y0 < bbox.y0 - tolerance
        or rect.y1 > bbox.y1 + tolerance
    )


def _looks_like_paragraph_fragment_text(text: str) -> bool:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) < 45:
        return False
    if _parse_caption_block_text(cleaned) is not None or _looks_like_algorithm_caption_text(cleaned):
        return False
    if _looks_like_table_line(cleaned) or _looks_like_table_cell_text(cleaned):
        return False
    words = re.findall(r"[A-Za-z][A-Za-z-]+", cleaned)
    if len(words) < 8:
        return False
    if re.search(r"[.!?;]\s*(?:[A-Z]|$)", cleaned):
        return True
    function_words = len(re.findall(
        r"(?i)\b(?:the|this|these|those|that|with|without|between|from|into|"
        r"because|while|where|which|when|using|through|across|during|after|before)\b",
        cleaned,
    ))
    lowercase_words = sum(1 for word in words if word[:1].islower())
    return len(cleaned) >= 70 and function_words >= 2 and lowercase_words >= max(4, len(words) // 3)


def _looks_like_header_band_text(page: fitz.Page, text: str, rect: fitz.Rect, bbox: fitz.Rect) -> bool:
    cleaned = " ".join(str(text or "").split()).strip()
    if not cleaned:
        return False
    page_rect = page.rect
    top_band = max(96.0, page_rect.height * 0.135)
    if rect.y0 > page_rect.y0 + top_band:
        return False
    crop_top_band = bbox.y0 + max(26.0, bbox.height * 0.16)
    if rect.y0 > crop_top_band and bbox.y0 > page_rect.y0 + 42.0:
        return False
    if _parse_caption_block_text(cleaned) is not None or _looks_like_algorithm_caption_text(cleaned):
        return False
    if _text_rect_has_visual_context(page, rect, bbox):
        return False

    lowered = cleaned.lower()
    words = re.findall(r"[A-Za-z][A-Za-z-]+", cleaned)
    if not words:
        return False
    if _looks_like_table_cell_text(cleaned) and len(words) <= 6 and rect.width < page_rect.width * 0.50:
        return False
    if re.search(r"@|\b(?:university|institute|department|school|laborator(?:y|ies)|center|centre)\b", lowered):
        return True
    if 2 <= len(words) <= 18 and ("," in cleaned or re.search(r"(?i)\band\b", cleaned)):
        if not re.search(r"[.!?;:]", cleaned):
            return True
    if rect.width < page_rect.width * 0.35:
        return False
    if re.search(r"[.!?;]", cleaned):
        return False
    titlecase_words = sum(1 for word in words if word[:1].isupper())
    return 4 <= len(words) <= 24 and titlecase_words >= max(3, len(words) // 2)


def _text_rect_has_visual_context(page: fitz.Page, rect: fitz.Rect, bbox: fitz.Rect) -> bool:
    probe = fitz.Rect(rect.x0 - 6.0, rect.y0 - 6.0, rect.x1 + 6.0, rect.y1 + 6.0) & bbox
    if probe.is_empty:
        return False
    for visual in _crop_visual_component_rects(page, bbox):
        if _rect_coverage_of_target(visual, probe) >= 0.55:
            return True
        if visual.intersects(probe) and (
            _horizontal_overlap(visual, probe) >= 0.35 or _vertical_overlap(visual, probe) >= 0.35
        ):
            return True
    return False


def _captioned_crop_has_edge_visual_remnant(
    page: fitz.Page,
    bbox: fitz.Rect,
    caption_rect: fitz.Rect | None = None,
) -> bool:
    edge = _crop_edge_band(bbox)
    for rect in _crop_visual_component_rects(page, bbox):
        if caption_rect is not None and rect.intersects(caption_rect):
            continue
        clipped = rect & bbox
        if clipped.is_empty:
            continue
        if not _rect_is_clipped_by_bbox(rect, bbox, tolerance=3.0):
            continue
        if not _rect_touches_crop_edge(clipped, bbox, band=edge):
            continue
        if clipped.width * clipped.height < 18.0:
            continue
        rect_area = max(1.0, rect.width * rect.height)
        coverage = (clipped.width * clipped.height) / rect_area
        strip_like = clipped.width <= max(7.0, bbox.width * 0.055) or clipped.height <= max(7.0, bbox.height * 0.055)
        large_partial = (
            coverage <= 0.72
            and (rect.width >= bbox.width * 0.18 or rect.height >= bbox.height * 0.18)
            and clipped.width >= 4.0
            and clipped.height >= 4.0
        )
        if coverage <= 0.35 or (strip_like and coverage <= 0.65) or large_partial:
            return True
    return False


def _captioned_crop_has_figure_edge_text_clipping(
    page: fitz.Page,
    bbox: fitz.Rect,
    caption_rect: fitz.Rect | None = None,
) -> bool:
    tight_edge = max(1.0, min(2.5, min(bbox.width, bbox.height) * 0.012))
    edge_band = max(6.0, min(14.0, min(bbox.width, bbox.height) * 0.045))
    visual_touches_edge = _captioned_crop_has_top_side_visual_touch(page, bbox, caption_rect)
    for rect, text in _text_line_rects_in_bbox(page, bbox):
        if caption_rect is not None and rect.intersects(caption_rect):
            continue
        if not _looks_like_internal_figure_label_text(text):
            continue
        if _looks_like_header_band_text(page, text, rect, bbox):
            continue
        clipped = rect & bbox
        if clipped.is_empty:
            continue
        touches_tight_edge = (
            clipped.y0 <= bbox.y0 + tight_edge
            or clipped.x0 <= bbox.x0 + tight_edge
            or clipped.x1 >= bbox.x1 - tight_edge
        )
        in_edge_band = (
            clipped.y0 <= bbox.y0 + edge_band
            or clipped.x0 <= bbox.x0 + edge_band
            or clipped.x1 >= bbox.x1 - edge_band
        )
        clipped_by_crop = (
            rect.y0 < bbox.y0 - 1.0
            or rect.x0 < bbox.x0 - 1.0
            or rect.x1 > bbox.x1 + 1.0
        )
        if clipped_by_crop and in_edge_band:
            return True
        if touches_tight_edge and visual_touches_edge:
            return True
    return False


def _captioned_crop_has_top_side_visual_touch(
    page: fitz.Page,
    bbox: fitz.Rect,
    caption_rect: fitz.Rect | None = None,
) -> bool:
    edge = max(3.0, min(10.0, min(bbox.width, bbox.height) * 0.035))
    for rect in _crop_visual_component_rects(page, bbox):
        if caption_rect is not None and rect.intersects(caption_rect):
            continue
        clipped = rect & bbox
        if clipped.is_empty or clipped.width * clipped.height < 18.0:
            continue
        if (
            clipped.y0 <= bbox.y0 + edge
            or clipped.x0 <= bbox.x0 + edge
            or clipped.x1 >= bbox.x1 - edge
        ):
            return True
    return False


def _looks_like_internal_figure_label_text(text: str) -> bool:
    cleaned = " ".join(str(text or "").split())
    if not _looks_like_figure_label_text(cleaned):
        return False
    if _table_text_boundary_kind(cleaned):
        return False
    if _looks_like_paragraph_fragment_text(cleaned) or _looks_like_prose_block(cleaned):
        return False
    tokens = re.findall(r"[A-Za-z0-9.+/%→✓-]+", cleaned)
    return bool(tokens) and len(tokens) <= 12


def _text_line_rects_in_bbox(page: fitz.Page, bbox: fitz.Rect) -> list[tuple[fitz.Rect, str]]:
    lines: list[tuple[fitz.Rect, str]] = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines") or []:
            try:
                rect = fitz.Rect(line.get("bbox"))
            except Exception:
                continue
            if not rect.intersects(bbox):
                continue
            text = " ".join(str(span.get("text") or "") for span in (line.get("spans") or [])).strip()
            if text:
                lines.append((rect, text))
    return lines


def _crop_visual_component_rects(page: fitz.Page, bbox: fitz.Rect) -> list[fitz.Rect]:
    rects: list[fitz.Rect] = []
    page_rect = page.rect
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 1:
            continue
        try:
            rect = fitz.Rect(block.get("bbox"))
        except Exception:
            continue
        if rect.is_empty or not rect.intersects(bbox):
            continue
        if _is_likely_page_furniture_rect(rect, page_rect):
            continue
        rects.append(rect)
    for drawing in page.get_drawings():
        rect_obj = drawing.get("rect")
        if rect_obj is None or _is_likely_vector_background_rect(drawing, page_rect):
            continue
        rect = _nonempty_rect(rect_obj)
        if rect.is_empty or not rect.intersects(bbox):
            continue
        if _is_likely_page_furniture_rect(rect, page_rect):
            continue
        if max(rect.width, rect.height) < 4.0:
            continue
        rects.append(rect)
    return rects


def _table_crop_signal_counts(
    page: fitz.Page,
    bbox: fitz.Rect,
    caption_rect: fitz.Rect | None = None,
) -> dict[str, int]:
    counts = {
        "wide_rules": 0,
        "thin_rules": 0,
        "table_lines": 0,
        "numeric_lines": 0,
        "numeric_cells": 0,
        "cell_lines": 0,
        "prose_lines": 0,
        "foreign_captions": 0,
        "caption_lines": 0,
    }
    for drawing in page.get_drawings():
        rect_obj = drawing.get("rect")
        if rect_obj is None:
            continue
        rect = _nonempty_rect(rect_obj)
        if not rect.intersects(bbox) or not _looks_like_table_rule_rect(rect):
            continue
        if rect.width >= bbox.width * 0.45 and rect.height <= 6.0:
            counts["wide_rules"] += 1
        else:
            counts["thin_rules"] += 1

    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines") or []:
            try:
                rect = fitz.Rect(line.get("bbox"))
            except Exception:
                continue
            if not rect.intersects(bbox) or _horizontal_overlap(rect, bbox) < 0.12:
                continue
            text = " ".join(str(span.get("text") or "") for span in (line.get("spans") or [])).strip()
            if not text:
                continue
            boundary = _table_text_boundary_kind(text)
            if boundary:
                if boundary in {"figure_caption", "table_caption"}:
                    if caption_rect is not None and rect.intersects(caption_rect):
                        counts["caption_lines"] += 1
                    else:
                        counts["foreign_captions"] += 1
                elif boundary in {"algorithm_caption", "body_text", "section_heading"}:
                    counts["prose_lines"] += 1
                continue
            if (
                _looks_like_prose_block(text)
                or _looks_like_paragraph_fragment_text(text)
                or (len(text) > 70 and re.search(r"[.!?;]\s+[A-Z]", text))
            ):
                counts["prose_lines"] += 1
                continue
            if _looks_like_table_cell_text(text):
                counts["cell_lines"] += 1
                if re.search(r"\d", text):
                    counts["numeric_cells"] += 1
            if _looks_like_table_line(text):
                counts["table_lines"] += 1
                if re.search(r"\d", text):
                    counts["numeric_lines"] += 1
    return counts


def _table_crop_has_enough_structure_signal(counts: dict[str, int]) -> bool:
    wide_rules = counts.get("wide_rules", 0)
    thin_rules = counts.get("thin_rules", 0)
    table_lines = counts.get("table_lines", 0)
    numeric_lines = counts.get("numeric_lines", 0)
    numeric_cells = counts.get("numeric_cells", 0)
    cell_lines = counts.get("cell_lines", 0)
    rule_signal = wide_rules + thin_rules
    rich_table_structure = (wide_rules + thin_rules) >= 5 and numeric_cells >= 8 and cell_lines >= 12
    if rich_table_structure:
        return True
    if rule_signal >= 4 and cell_lines >= 8:
        return True
    if wide_rules >= 3 and cell_lines >= 6:
        return True
    if wide_rules >= 2 and cell_lines >= 6 and table_lines >= 3:
        return True
    if wide_rules >= 2 and numeric_cells >= 4 and cell_lines >= 6:
        return True
    if table_lines >= 3 and numeric_lines >= 2:
        return True
    if table_lines >= 4 and cell_lines >= 4 and rule_signal >= 2:
        return True
    if table_lines >= 2 and numeric_lines >= 1 and (wide_rules >= 1 or thin_rules >= 2):
        return True
    return False


def _table_crop_looks_fragmentary(
    counts: dict[str, int],
    bbox: fitz.Rect,
    page_rect: fitz.Rect,
) -> bool:
    data_lines = max(counts.get("table_lines", 0), counts.get("cell_lines", 0))
    numeric_signal = max(counts.get("numeric_lines", 0), counts.get("numeric_cells", 0))
    rule_signal = counts.get("wide_rules", 0) + counts.get("thin_rules", 0)
    short_crop = bbox.height < max(90.0, page_rect.height * 0.16)
    if data_lines <= 2 and numeric_signal <= 2:
        return True
    if short_crop and data_lines <= 5 and numeric_signal <= 2 and rule_signal >= 1:
        return True
    if counts.get("foreign_captions", 0) and data_lines <= 5 and numeric_signal <= 2:
        return True
    return False


def _table_crop_completeness_flags(
    page: fitz.Page,
    bbox: fitz.Rect,
    caption_rect: fitz.Rect | None,
    counts: dict[str, int],
) -> list[str]:
    flags: list[str] = []
    data_lines = max(counts.get("table_lines", 0), counts.get("cell_lines", 0))
    if data_lines < 2:
        return flags

    lines = _table_text_line_records(page, bbox, caption_rect)
    if not lines:
        return flags

    partial_top = _table_has_partial_edge_text(lines, bbox, edge="top")
    partial_bottom = _table_has_partial_edge_text(lines, bbox, edge="bottom")
    partial_left = _table_has_partial_edge_text(lines, bbox, edge="left")
    text_context_above = _table_has_text_context_above_crop(page, bbox, caption_rect)
    context_above = _table_has_context_above_crop(page, bbox, caption_rect)
    context_left = _table_has_context_left_of_crop(page, bbox, caption_rect)
    top_rule = _table_has_top_rule_inside_crop(page, bbox)

    if partial_top or partial_bottom:
        _append_flag(flags, "table_partial_row_strip")
    if partial_top or (context_above and not top_rule):
        _append_flag(flags, "table_open_top_context")
    if partial_left or context_left:
        _append_flag(flags, "table_open_left_context")
    if (partial_top or text_context_above) and _table_top_row_looks_like_data(lines, bbox):
        _append_flag(flags, "table_missing_header_context")
    return flags


def _table_text_line_records(
    page: fitz.Page,
    region: fitz.Rect,
    caption_rect: fitz.Rect | None = None,
) -> list[tuple[fitz.Rect, str]]:
    records: list[tuple[fitz.Rect, str]] = []
    for rect, text in _text_line_rects_in_bbox(page, region):
        if caption_rect is not None and rect.intersects(caption_rect):
            continue
        if _table_text_boundary_kind(text):
            continue
        if not (_looks_like_table_line(text) or _looks_like_table_cell_text(text)):
            continue
        if _horizontal_overlap(rect, region) < 0.08 and _vertical_overlap(rect, region) < 0.35:
            continue
        records.append((rect, text))
    return records


def _table_has_partial_edge_text(
    lines: list[tuple[fitz.Rect, str]],
    bbox: fitz.Rect,
    *,
    edge: str,
) -> bool:
    for rect, _text in lines:
        if edge == "top" and rect.y0 < bbox.y0 - 1.0 and rect.y1 <= bbox.y0 + max(18.0, bbox.height * 0.16):
            return True
        if edge == "bottom" and rect.y1 > bbox.y1 + 1.0 and rect.y0 >= bbox.y1 - max(18.0, bbox.height * 0.16):
            return True
        if edge == "left" and rect.x0 < bbox.x0 - 1.0 and _vertical_overlap(rect, bbox) >= 0.35:
            return True
    return False


def _table_has_context_above_crop(
    page: fitz.Page,
    bbox: fitz.Rect,
    caption_rect: fitz.Rect | None = None,
) -> bool:
    region = fitz.Rect(
        bbox.x0 - 8.0,
        max(page.rect.y0, bbox.y0 - 44.0),
        bbox.x1 + 8.0,
        min(page.rect.y1, bbox.y0 + 2.0),
    )
    if region.is_empty:
        return False
    for rect, _text in _table_text_line_records(page, region, caption_rect):
        if rect.y1 <= bbox.y0 + 3.0 and _horizontal_overlap(rect, bbox) >= 0.18:
            return True
    for rect in _table_rule_rects_in_region(page, region):
        if rect.height <= 6.0 and rect.y1 <= bbox.y0 + 3.0 and _horizontal_overlap(rect, bbox) >= 0.35:
            return True
    return False


def _table_has_text_context_above_crop(
    page: fitz.Page,
    bbox: fitz.Rect,
    caption_rect: fitz.Rect | None = None,
) -> bool:
    region = fitz.Rect(
        bbox.x0 - 8.0,
        max(page.rect.y0, bbox.y0 - 44.0),
        bbox.x1 + 8.0,
        min(page.rect.y1, bbox.y0 + 2.0),
    )
    if region.is_empty:
        return False
    return any(
        rect.y1 <= bbox.y0 + 3.0 and _horizontal_overlap(rect, bbox) >= 0.18
        for rect, _text in _table_text_line_records(page, region, caption_rect)
    )


def _table_has_context_left_of_crop(
    page: fitz.Page,
    bbox: fitz.Rect,
    caption_rect: fitz.Rect | None = None,
) -> bool:
    region = fitz.Rect(
        max(page.rect.x0, bbox.x0 - max(72.0, min(180.0, bbox.width * 0.45))),
        bbox.y0 - 4.0,
        min(page.rect.x1, bbox.x0 + 2.0),
        bbox.y1 + 4.0,
    )
    if region.is_empty:
        return False
    context_lines = 0
    for rect, _text in _table_text_line_records(page, region, caption_rect):
        if rect.x1 <= bbox.x0 + 3.0 and _vertical_overlap(rect, bbox) >= 0.35:
            context_lines += 1
            if context_lines >= 2:
                return True
    return False


def _table_has_top_rule_inside_crop(page: fitz.Page, bbox: fitz.Rect) -> bool:
    top_band = max(8.0, min(24.0, bbox.height * 0.16))
    for rect in _table_rule_rects_in_region(page, bbox):
        if rect.height > 6.0:
            continue
        clipped = rect & bbox
        if clipped.is_empty:
            continue
        if clipped.y0 <= bbox.y0 + top_band and clipped.width >= max(80.0, bbox.width * 0.42):
            return True
    return False


def _table_rule_rects_in_region(page: fitz.Page, region: fitz.Rect) -> list[fitz.Rect]:
    rects: list[fitz.Rect] = []
    for drawing in page.get_drawings():
        rect_obj = drawing.get("rect")
        if rect_obj is None:
            continue
        rect = _nonempty_rect(rect_obj)
        if rect.intersects(region) and _looks_like_table_rule_rect(rect):
            rects.append(rect)
    return rects


def _table_top_row_looks_like_data(lines: list[tuple[fitz.Rect, str]], bbox: fitz.Rect) -> bool:
    inside = [(rect, text) for rect, text in lines if rect.intersects(bbox)]
    if not inside:
        return False
    top_y = min(rect.y0 for rect, _text in inside)
    top_lines = [
        (rect, text)
        for rect, text in inside
        if rect.y0 <= top_y + max(6.0, rect.height * 0.65)
    ]
    joined = " ".join(text for _rect, text in top_lines)
    if re.search(r"\d", joined):
        return True
    header_terms = re.search(
        r"(?i)\b(?:method|model|dataset|metric|score|setting|component|module|"
        r"task|backbone|input|output|category|type)\b",
        joined,
    )
    return len(top_lines) <= 2 and header_terms is None


def _rect_overlap_height(a: fitz.Rect, b: fitz.Rect) -> float:
    return max(0.0, min(a.y1, b.y1) - max(a.y0, b.y0))


def _table_boundary_blocks_in_region(
    page: fitz.Page,
    region: fitz.Rect,
    caption_rect: fitz.Rect,
) -> list[tuple[fitz.Rect, str]]:
    boundaries: list[tuple[fitz.Rect, str]] = []
    for block in page.get_text("blocks"):
        rect = fitz.Rect(block[:4])
        if rect.intersects(caption_rect):
            continue
        clipped = rect & region
        if clipped.is_empty:
            continue
        if _horizontal_overlap(region, rect) < 0.12:
            continue
        if _horizontal_overlap(caption_rect, rect) < 0.10 and rect.width < region.width * 0.62:
            continue
        text = " ".join(str(block[4] or "").split())
        reason = _table_text_boundary_kind(text)
        if reason:
            boundaries.append((rect, reason))
    return boundaries


def _table_text_boundary_kind(text: str) -> str:
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return ""
    if _looks_like_caption_body_reference_text(cleaned):
        return "body_text"
    parsed_caption = _parse_caption_block_text(cleaned)
    if parsed_caption is not None:
        return "figure_caption" if parsed_caption[0] == "figure" else "table_caption"
    if _looks_like_algorithm_caption_text(cleaned):
        return "algorithm_caption"
    if _looks_like_section_heading_text(cleaned):
        return "section_heading"
    if _looks_like_prose_block(cleaned):
        return "body_text"
    return ""


def _looks_like_caption_body_reference_text(text: str) -> bool:
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return False
    if _INLINE_CAPTION_REFERENCE_START_RE.match(cleaned):
        return True
    match = _CAPTION_BLOCK_START_RE.match(cleaned)
    if not match:
        return False
    rest = str(match.group("rest") or "").strip()
    if not rest:
        return False
    if _CAPTION_BODY_REFERENCE_RE.match(rest):
        return True
    return not match.group("punc") and _CAPTION_BODY_MENTION_RE.match(rest) is not None


def _looks_like_algorithm_caption_text(text: str) -> bool:
    return re.match(
        r"(?i)^\s*algorithm\s+(?:\d+(?:[A-Za-z])?|[IVXLCDM]+)\s*(?:[:.)\-\u2013\u2014]|\b)",
        str(text or ""),
    ) is not None


_SECTION_HEADING_SINGLETONS = {
    "abstract",
    "introduction",
    "references",
    "appendix",
    "acknowledgment",
    "acknowledgments",
    "acknowledgement",
    "acknowledgements",
    "conclusion",
    "conclusions",
}
_SECTION_HEADING_PHRASES = {
    "related work",
    "background",
    "methodology",
    "methods",
    "approach",
    "experiments",
    "experimental setup",
    "experimental results",
    "results",
    "discussion",
    "limitations",
    "future work",
}


def _looks_like_section_heading_text(text: str) -> bool:
    cleaned = " ".join(str(text or "").split()).strip(" .")
    if not cleaned or len(cleaned) > 90:
        return False
    if _parse_caption_block_text(cleaned) is not None or _looks_like_algorithm_caption_text(cleaned):
        return False
    if re.search(r"[.!?;:]", cleaned):
        return False
    if _looks_like_table_line(cleaned) or _looks_like_table_text(cleaned):
        return False
    normalized = re.sub(r"^\d+(?:\.\d+)*\.?\s+", "", cleaned).strip().lower()
    normalized = re.sub(r"\s+", " ", normalized)
    if not normalized:
        return False
    has_numbered_prefix = normalized != cleaned.lower()
    if normalized in _SECTION_HEADING_SINGLETONS:
        return True
    if normalized in _SECTION_HEADING_PHRASES:
        return has_numbered_prefix or len(normalized.split()) >= 2
    if has_numbered_prefix:
        words = normalized.split()
        return 1 <= len(words) <= 6 and not re.search(r"\d", normalized)
    return False


def _score_captioned_table_bbox(page: fitz.Page, bbox: fitz.Rect, caption_rect: fitz.Rect) -> float:
    wide_rules = 0
    thin_rules = 0
    for drawing in page.get_drawings():
        rect_obj = drawing.get("rect")
        if rect_obj is None:
            continue
        rect = _nonempty_rect(rect_obj)
        if not rect.intersects(bbox) or not _looks_like_table_rule_rect(rect):
            continue
        if rect.width >= bbox.width * 0.55 and rect.height <= 6.0:
            wide_rules += 1
        else:
            thin_rules += 1

    table_lines = 0
    numeric_lines = 0
    prose_blocks = 0
    other_caption_blocks = 0
    for block in page.get_text("blocks"):
        rect = fitz.Rect(block[:4])
        if not rect.intersects(bbox) or _horizontal_overlap(bbox, rect) < 0.15:
            continue
        text = " ".join(str(block[4] or "").split())
        if not text:
            continue
        parsed_caption = _parse_caption_block_text(text)
        if parsed_caption is not None:
            if not rect.intersects(caption_rect):
                other_caption_blocks += 1
            continue
        if _looks_like_table_line(text) or _looks_like_table_text(text):
            table_lines += 1
        if re.search(r"\d", text):
            numeric_lines += 1
        if _looks_like_prose_block(text):
            prose_blocks += 1

    distance = min(abs(bbox.y0 - caption_rect.y1), abs(caption_rect.y0 - bbox.y1))
    plot_grid_penalty = 90.0 if wide_rules == 0 and thin_rules >= 8 else 0.0
    return (
        wide_rules * 85.0
        + thin_rules * 1.0
        + table_lines * 8.0
        + numeric_lines * 5.0
        + min(80.0, bbox.width / 8.0)
        - prose_blocks * 35.0
        - other_caption_blocks * 80.0
        - plot_grid_penalty
        - distance * 0.15
    )


def _crop_looks_page_like(bbox: fitz.Rect, page_rect: fitz.Rect) -> bool:
    page_area = max(1.0, page_rect.width * page_rect.height)
    area_frac = (bbox.width * bbox.height) / page_area
    if area_frac >= 0.78:
        return True
    return bbox.width >= page_rect.width * 0.92 and bbox.height >= page_rect.height * 0.68


def _captioned_table_bbox_quality_ok(
    page: fitz.Page,
    bbox: fitz.Rect,
    caption_rect: fitz.Rect,
) -> bool:
    if not _anchor_bbox_usable(bbox, page.rect, kind="table"):
        return False
    flags = pdf_table_crop_quality_flags(page, bbox, caption_rect)
    return not any(flag in _TABLE_CROP_HARD_REJECT_FLAGS for flag in flags)


def _table_hint_for_group(
    page: fitz.Page,
    group: CaptionedVisualGroup,
    caption_rect: fitz.Rect,
) -> tuple[fitz.Rect, list[list[str]], int, int] | None:
    try:
        finder = page.find_tables()
    except Exception as exc:  # noqa: BLE001 - best effort hint only
        log(
            "ingest.pdf.captioned_group.find_tables_fail",
            page=group.page,
            label=group.label,
            error=f"{type(exc).__name__}: {exc}"[:200],
        )
        return None

    margin = 24.0
    page_rect = page.rect
    x0, x1 = _caption_search_x_bounds(caption_rect, page_rect, margin=margin)
    if caption_rect.width < page_rect.width * 0.45:
        x0, x1 = margin, page_rect.width - margin
    regions = _caption_table_search_regions(
        page,
        caption_rect,
        page_rect,
        x0=x0,
        x1=x1,
        margin=margin,
        distance=460.0,
    )
    best: tuple[float, fitz.Rect, list[list[str]], int, int] | None = None
    for tbl in getattr(finder, "tables", []) or []:
        try:
            raw_rect = fitz.Rect(tbl.bbox)
        except Exception:
            continue
        matching_regions = [
            (region_idx, region)
            for region_idx, region in enumerate(regions)
            if raw_rect.intersects(region)
        ]
        if not matching_regions:
            continue
        region_idx, region = max(
            matching_regions,
            key=lambda item: _rect_coverage_of_target(raw_rect, item[1]),
        )
        rect = _trim_captioned_table_bbox(page, raw_rect, caption_rect, region)
        if rect is None:
            continue
        if not _captioned_table_bbox_has_structure(page, rect, caption_rect):
            continue
        if not _captioned_table_bbox_quality_ok(page, rect, caption_rect):
            continue
        try:
            raw_cells = tbl.extract() or []
        except Exception:
            raw_cells = []
        norm_cells = [[(cell if cell is not None else "") for cell in row] for row in raw_cells]
        nrows = len(norm_cells)
        ncols = max((len(row) for row in norm_cells), default=0)
        distance = min(abs(rect.y0 - caption_rect.y1), abs(caption_rect.y0 - rect.y1))
        score = (
            _horizontal_overlap(rect, caption_rect) * 1000.0
            - distance
            + min(120.0, rect.height)
            - region_idx * 2000.0
        )
        if best is None or score > best[0]:
            best = (score, rect, norm_cells, nrows, ncols)
    if best is None:
        return None
    return best[1], best[2], best[3], best[4]


def _visual_rects_in_region(page: fitz.Page, region: fitz.Rect) -> list[fitz.Rect]:
    rects: list[fitz.Rect] = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 1:
            continue
        try:
            rect = fitz.Rect(block.get("bbox"))
        except Exception:
            continue
        raw_rect = fitz.Rect(rect)
        rect = raw_rect & region
        if rect.is_empty:
            continue
        if (rect.width * rect.height) / max(1.0, raw_rect.width * raw_rect.height) < 0.58:
            continue
        if _is_likely_page_furniture_rect(rect, page.rect):
            continue
        if max(rect.width, rect.height) < 6:
            continue
        rects.append(rect)
    for drawing in page.get_drawings():
        rect_obj = drawing.get("rect")
        if rect_obj is None or _is_likely_vector_background_rect(drawing, page.rect):
            continue
        raw_rect = _nonempty_rect(rect_obj)
        rect = raw_rect & region
        if rect.is_empty:
            continue
        if (rect.width * rect.height) / max(1.0, raw_rect.width * raw_rect.height) < 0.58:
            continue
        if _is_likely_page_furniture_rect(rect, page.rect):
            continue
        if max(rect.width, rect.height) < 2:
            continue
        rects.append(rect)
    return rects


def _table_rects_in_region(page: fitz.Page, region: fitz.Rect) -> list[fitz.Rect]:
    rects: list[fitz.Rect] = []
    for drawing in page.get_drawings():
        rect_obj = drawing.get("rect")
        if rect_obj is None or _is_likely_vector_background_rect(drawing, page.rect):
            continue
        rect = _nonempty_rect(rect_obj) & region
        if rect.is_empty:
            continue
        if _looks_like_table_horizontal_rule_rect(rect, region):
            rects.append(rect)
    line_rects = _table_line_rects_in_region(page, region)
    if rects and line_rects:
        rule_box = _union_rects(rects)
        if rule_box is not None:
            rects.extend(
                line_rect
                for line_rect in line_rects
                if line_rect.intersects(rule_box) or _horizontal_overlap(line_rect, rule_box) >= 0.35
            )
    if rects:
        rule_box = _union_rects(rects)
        if rule_box is not None:
            cell_region = fitz.Rect(
                max(region.x0, rule_box.x0 - 10.0),
                max(region.y0, rule_box.y0 - 22.0),
                min(region.x1, rule_box.x1 + 10.0),
                min(region.y1, rule_box.y1 + 34.0),
            )
            rects.extend(_table_cell_line_rects_in_region(page, cell_region))
    if rects:
        return rects
    if _table_lines_have_enough_signal(page, line_rects):
        return line_rects
    return []


def _nonempty_rect(rect_obj: Any, *, min_thickness: float = 1.0) -> fitz.Rect:
    rect = fitz.Rect(rect_obj)
    if rect.width <= 0:
        rect.x0 -= min_thickness / 2.0
        rect.x1 += min_thickness / 2.0
    if rect.height <= 0:
        rect.y0 -= min_thickness / 2.0
        rect.y1 += min_thickness / 2.0
    return rect


def _looks_like_table_rule_rect(rect: fitz.Rect) -> bool:
    if rect.is_empty:
        return False
    if rect.width >= 32.0 and rect.height <= 6.0:
        return True
    if rect.height >= 24.0 and rect.width <= 3.0:
        return True
    return False


def _looks_like_table_horizontal_rule_rect(rect: fitz.Rect, region: fitz.Rect) -> bool:
    if rect.is_empty or rect.height > 4.0:
        return False
    return rect.width >= 80.0 or rect.width >= region.width * 0.22


def _table_line_rects_in_region(page: fitz.Page, region: fitz.Rect) -> list[fitz.Rect]:
    rects: list[fitz.Rect] = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines") or []:
            spans = line.get("spans") or []
            text = " ".join(str(span.get("text") or "") for span in spans).strip()
            if _table_text_boundary_kind(text):
                continue
            if not _looks_like_table_line(text):
                continue
            try:
                rect = fitz.Rect(line.get("bbox")) & region
            except Exception:
                continue
            if rect.is_empty:
                continue
            rects.append(rect)
    return rects


def _looks_like_table_line(text: str) -> bool:
    if _parse_caption_block_text(text) is not None:
        return False
    if _looks_like_caption_body_reference_text(text) or _looks_like_algorithm_caption_text(text):
        return False
    if _looks_like_prose_block(text):
        return False
    if len(text) > 55 and re.search(r"[.!?;]\s+[A-Z]", text):
        return False
    tokens = re.findall(r"[A-Za-z0-9.+/%-]+", text or "")
    if len(tokens) < 2:
        return False
    numeric = sum(1 for token in tokens if re.search(r"\d", token))
    if numeric >= 2:
        return True
    return len(tokens) >= 4 and not re.search(r"[.!?]\s+[A-Z]", text or "")


def _table_cell_line_rects_in_region(page: fitz.Page, region: fitz.Rect) -> list[fitz.Rect]:
    rects: list[fitz.Rect] = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines") or []:
            spans = line.get("spans") or []
            text = " ".join(str(span.get("text") or "") for span in spans).strip()
            if _table_text_boundary_kind(text):
                continue
            if not _looks_like_table_cell_text(text):
                continue
            try:
                rect = fitz.Rect(line.get("bbox")) & region
            except Exception:
                continue
            if rect.is_empty:
                continue
            rects.append(rect)
    return rects


def _looks_like_table_cell_text(text: str) -> bool:
    cleaned = " ".join(str(text or "").split())
    if not cleaned or len(cleaned) > 80:
        return False
    if _looks_like_caption_body_reference_text(cleaned) or _looks_like_algorithm_caption_text(cleaned):
        return False
    if _parse_caption_block_text(cleaned) is not None or _looks_like_prose_block(cleaned):
        return False
    if re.search(r"[.!?;]\s+[A-Z]", cleaned):
        return False
    tokens = re.findall(r"[A-Za-z0-9.+/%→✓-]+", cleaned)
    if not tokens or len(tokens) > 10:
        return False
    if re.search(r"\d", cleaned):
        return True
    if re.search(
        r"(?i)\b(?:ap|ap50|ap75|aps|apm|apl|bleu|ppl|acc|accuracy|auc|f1|"
        r"method|model|baseline|ours|dataset|metric|score|latency|backbone|"
        r"params|layers|heads|module|component|stage|step|input|output|signal|"
        r"token|tokens|tokenizer|detokenizer|de-tokenizer|usage|type|category|"
        r"domain|task|recipe|setting|source|target|feature|loss|objective)\b",
        cleaned,
    ):
        return True
    return bool(re.search(r"[→✓+\-]", cleaned))


def _captioned_table_bbox_has_structure(
    page: fitz.Page,
    bbox: fitz.Rect,
    caption_rect: fitz.Rect,
) -> bool:
    if bbox.width < 95.0 or bbox.height < 42.0:
        return False
    counts = _table_crop_signal_counts(page, bbox, caption_rect)
    if counts.get("foreign_captions", 0):
        return False
    if counts.get("prose_lines", 0) >= 2:
        return False
    if _table_crop_looks_fragmentary(counts, bbox, page.rect):
        return False
    return _table_crop_has_enough_structure_signal(counts)


def _table_lines_have_enough_signal(page: fitz.Page, rects: list[fitz.Rect]) -> bool:
    if len(rects) < 3:
        return False
    region = _union_rects(rects)
    if region is None:
        return False
    texts: list[str] = []
    for block in page.get_text("blocks"):
        rect = fitz.Rect(block[:4])
        if rect.intersects(region):
            texts.append(str(block[4] or ""))
    joined = " ".join(texts)
    return len(re.findall(r"\d", joined)) >= 3 or len(rects) >= 5


def _looks_like_table_text(text: str) -> bool:
    if not text:
        return False
    if _looks_like_prose_block(text):
        return False
    tokens = re.findall(r"[A-Za-z0-9.+/%-]+", text)
    if len(tokens) < 8:
        return False
    numeric = sum(1 for token in tokens if re.search(r"\d", token))
    separators = text.count("  ") + text.count("\t")
    return numeric >= 3 or separators >= 3


def _looks_like_prose_block(text: str) -> bool:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) < 90:
        return False
    words = re.findall(r"[A-Za-z][A-Za-z-]+", cleaned)
    if len(words) < 18:
        return False
    sentence_marks = len(re.findall(r"[.!?;]", cleaned))
    numeric = len(re.findall(r"\d", cleaned))
    return sentence_marks >= 2 and numeric < max(6, len(words) // 5)


def _trim_caption_or_body_from_figure_bbox(
    page: fitz.Page,
    bbox: fitz.Rect,
    caption_rect: fitz.Rect | None = None,
) -> fitz.Rect:
    out = fitz.Rect(bbox) & page.rect
    if out.is_empty:
        return out
    min_boundary_y = out.y0 + max(42.0, out.height * 0.48)
    trim_y: float | None = None
    for block in page.get_text("blocks"):
        rect = fitz.Rect(block[:4])
        if not rect.intersects(out) or _horizontal_overlap(out, rect) < 0.18:
            continue
        if rect.y0 < min_boundary_y:
            continue
        text = " ".join(str(block[4] or "").split())
        if not text:
            continue
        parsed_caption = _parse_caption_block_text(text)
        boundary = _table_text_boundary_kind(text)
        if (
            parsed_caption is not None
            or boundary in {"body_text", "section_heading", "figure_caption", "table_caption", "algorithm_caption"}
            or _looks_like_prose_block(text)
        ):
            candidate_y = rect.y0 - 4.0
            if candidate_y > out.y0 + 34.0:
                trim_y = candidate_y if trim_y is None else min(trim_y, candidate_y)
    if trim_y is None:
        return out
    trimmed = fitz.Rect(out.x0, out.y0, out.x1, trim_y) & page.rect
    if trimmed.height < 48.0 or (trimmed.width * trimmed.height) < (out.width * out.height) * 0.22:
        return out
    return trimmed


def _is_likely_page_furniture_rect(rect: fitz.Rect, page_rect: fitz.Rect) -> bool:
    if rect.is_empty:
        return False
    edge_band = max(48.0, page_rect.height * 0.075)
    in_top = rect.y1 <= page_rect.y0 + edge_band
    in_bottom = rect.y0 >= page_rect.y1 - edge_band
    if not (in_top or in_bottom):
        return False
    if rect.height <= 5.0 and rect.width >= page_rect.width * 0.22:
        return True
    if rect.height <= 34.0 and rect.width >= page_rect.width * 0.18:
        return True
    if rect.height <= 26.0 and rect.width <= page_rect.width * 0.86:
        return True
    return rect.width <= 110.0 and rect.height <= 38.0


def _page_furniture_rects(page: fitz.Page, caption_rect: fitz.Rect | None = None) -> list[fitz.Rect]:
    rects: list[fitz.Rect] = []
    page_rect = page.rect
    for block in page.get_text("blocks"):
        rect = fitz.Rect(block[:4])
        if caption_rect is not None and rect.intersects(caption_rect):
            continue
        if _is_likely_page_furniture_rect(rect, page_rect):
            rects.append(rect)
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 1:
            continue
        try:
            rect = fitz.Rect(block.get("bbox"))
        except Exception:
            continue
        if _is_likely_page_furniture_rect(rect, page_rect):
            rects.append(rect)
    for drawing in page.get_drawings():
        rect_obj = drawing.get("rect")
        if rect_obj is None:
            continue
        rect = _nonempty_rect(rect_obj) & page_rect
        if _is_likely_page_furniture_rect(rect, page_rect):
            rects.append(rect)
    return rects


def _trim_page_furniture_from_bbox(
    page: fitz.Page,
    bbox: fitz.Rect,
    caption_rect: fitz.Rect | None = None,
) -> fitz.Rect:
    out = fitz.Rect(bbox) & page.rect
    if out.is_empty:
        return out
    y0 = out.y0
    y1 = out.y1
    edge_zone = max(28.0, out.height * 0.18)
    for furniture in _page_furniture_rects(page, caption_rect):
        if not furniture.intersects(out):
            continue
        if _horizontal_overlap(out, furniture) < 0.08:
            continue
        if furniture.y1 <= out.y0 + edge_zone:
            y0 = max(y0, furniture.y1 + 4.0)
        if furniture.y0 >= out.y1 - edge_zone:
            y1 = min(y1, furniture.y0 - 4.0)
    if y1 - y0 < 34.0:
        return out
    return fitz.Rect(out.x0, y0, out.x1, y1)


def _captioned_crop_has_page_furniture_leak(
    page: fitz.Page,
    bbox: fitz.Rect,
    caption_rect: fitz.Rect | None = None,
) -> bool:
    for furniture in _page_furniture_rects(page, caption_rect):
        if furniture.intersects(bbox) and _horizontal_overlap(bbox, furniture) >= 0.08:
            return True
    return False


def _group_crop_name(group: CaptionedVisualGroup, prefix: str) -> str:
    label = re.sub(r"[^a-zA-Z0-9]+", "_", str(group.label or "x")).strip("_") or "x"
    page = int(group.page or 0)
    return f"p{page:03d}_group_{prefix}_{label}.png"



def _include_nearby_label_text(page: fitz.Page, bbox: fitz.Rect, region: fitz.Rect) -> fitz.Rect:
    expanded = fitz.Rect(bbox.x0 - 28.0, bbox.y0 - 28.0, bbox.x1 + 28.0, bbox.y1 + 28.0)
    out = fitz.Rect(bbox)
    for block in page.get_text("blocks"):
        rect = fitz.Rect(block[:4]) & region
        if rect.is_empty or not rect.intersects(expanded):
            continue
        text = " ".join(str(block[4] or "").split())
        if not _looks_like_figure_label_text(text):
            continue
        out |= rect
    return out


def _looks_like_figure_label_text(text: str) -> bool:
    if not text or len(text) > 120:
        return False
    if re.search(r"[a-z][.!?]\s+[A-Z]", text):
        return False
    if len(re.findall(r"[A-Za-z][A-Za-z-]+", text)) > 14:
        return False
    return True


def _anchor_bbox_usable(bbox: fitz.Rect, page_rect: fitz.Rect, *, kind: str) -> bool:
    if bbox.is_empty:
        return False
    if bbox.width < 60 or bbox.height < (34 if kind == "figure" else 38):
        return False
    page_area = max(1.0, page_rect.width * page_rect.height)
    max_frac = 0.72 if kind == "table" else 0.55
    if (bbox.width * bbox.height) / page_area > max_frac:
        return False
    return True


def _union_rects(rects: list[fitz.Rect]) -> fitz.Rect | None:
    if not rects:
        return None
    out = fitz.Rect(rects[0])
    for rect in rects[1:]:
        out |= rect
    return out


def _complete_captioned_figure_candidate_index(
    candidates: list[PdfFigureCandidate],
    page: int,
    bbox: fitz.Rect,
    pdf_page: fitz.Page,
    caption_rect: fitz.Rect,
    *,
    coverage_threshold: float = 0.90,
    max_extra_area_frac: float = 0.28,
) -> int | None:
    best: tuple[float, int] | None = None
    target_area = max(1.0, bbox.width * bbox.height)
    for idx, cand in enumerate(candidates):
        if cand.page != page or cand.bbox_pt is None:
            continue
        rect = fitz.Rect(cand.bbox_pt)
        coverage = _rect_coverage_of_target(rect, bbox)
        if coverage < coverage_threshold:
            continue
        overlap_area = _rect_intersection_area(rect, bbox)
        extra_area_frac = max(0.0, rect.width * rect.height - overlap_area) / target_area
        if extra_area_frac > max_extra_area_frac:
            continue
        if rect.intersects(caption_rect) and _rect_overlap_score(rect, caption_rect) >= 0.12:
            continue
        flags = pdf_figure_crop_quality_flags(pdf_page, rect, caption_rect)
        if any(flag in _FIGURE_CROP_HARD_REJECT_FLAGS for flag in flags):
            continue
        if best is None or coverage > best[0]:
            best = (coverage, idx)
    return best[1] if best else None


def _overlapping_figure_candidate_index(
    candidates: list[PdfFigureCandidate],
    page: int,
    bbox: fitz.Rect,
    *,
    threshold: float = 0.42,
) -> int | None:
    best: tuple[float, int] | None = None
    for idx, cand in enumerate(candidates):
        if cand.page != page or cand.bbox_pt is None:
            continue
        score = _rect_coverage_of_target(fitz.Rect(cand.bbox_pt), bbox)
        if score >= threshold and (best is None or score > best[0]):
            best = (score, idx)
    return best[1] if best else None


def _overlapping_table_candidate_index(
    candidates: list[PdfTableCandidate],
    page: int,
    bbox: fitz.Rect,
    *,
    threshold: float = 0.35,
) -> int | None:
    best: tuple[float, int] | None = None
    for idx, cand in enumerate(candidates):
        if cand.page != page:
            continue
        score = _rect_coverage_of_target(fitz.Rect(cand.bbox_pt), bbox)
        if score >= threshold and (best is None or score > best[0]):
            best = (score, idx)
    return best[1] if best else None


def _rect_intersection_area(a: fitz.Rect, b: fitz.Rect) -> float:
    ix0 = max(a.x0, b.x0)
    iy0 = max(a.y0, b.y0)
    ix1 = min(a.x1, b.x1)
    iy1 = min(a.y1, b.y1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    return (ix1 - ix0) * (iy1 - iy0)


def _rect_overlap_score(a: fitz.Rect, b: fitz.Rect) -> float:
    ix0 = max(a.x0, b.x0)
    iy0 = max(a.y0, b.y0)
    ix1 = min(a.x1, b.x1)
    iy1 = min(a.y1, b.y1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    overlap = (ix1 - ix0) * (iy1 - iy0)
    return overlap / max(1.0, min(a.width * a.height, b.width * b.height))


def _rect_coverage_of_target(candidate: fitz.Rect, target: fitz.Rect) -> float:
    ix0 = max(candidate.x0, target.x0)
    iy0 = max(candidate.y0, target.y0)
    ix1 = min(candidate.x1, target.x1)
    iy1 = min(candidate.y1, target.y1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    overlap = (ix1 - ix0) * (iy1 - iy0)
    return overlap / max(1.0, target.width * target.height)


def _table_near_caption(table_rect: fitz.Rect, caption_rect: fitz.Rect) -> bool:
    if abs(table_rect.y0 - caption_rect.y1) <= 240 and _horizontal_overlap(table_rect, caption_rect) >= 0.15:
        return True
    if abs(caption_rect.y0 - table_rect.y1) <= 240 and _horizontal_overlap(table_rect, caption_rect) >= 0.15:
        return True
    return False


def _table_loose_near_caption(table_rect: fitz.Rect, caption_rect: fitz.Rect) -> bool:
    if _horizontal_overlap(table_rect, caption_rect) < 0.08:
        return False
    return (
        abs(table_rect.y0 - caption_rect.y1) <= 420
        or abs(caption_rect.y0 - table_rect.y1) <= 420
    )


def _horizontal_overlap(a: fitz.Rect, b: fitz.Rect) -> float:
    overlap = max(0.0, min(a.x1, b.x1) - max(a.x0, b.x0))
    return overlap / max(1.0, min(a.width, b.width))


def _vertical_overlap(a: fitz.Rect, b: fitz.Rect) -> float:
    overlap = max(0.0, min(a.y1, b.y1) - max(a.y0, b.y0))
    return overlap / max(1.0, min(a.height, b.height))


def _rect_center_x(rect: fitz.Rect) -> float:
    return (rect.x0 + rect.x1) / 2.0


def _rect_center_y(rect: fitz.Rect) -> float:
    return (rect.y0 + rect.y1) / 2.0


def _visual_distance_to_caption(rect: fitz.Rect, caption_rect: fitz.Rect, *, below: bool) -> float:
    if below:
        return max(0.0, rect.y0 - caption_rect.y1)
    return max(0.0, caption_rect.y0 - rect.y1)


def _rect_horizontal_overlap(a: fitz.Rect, b: fitz.Rect) -> float:
    overlap = max(0.0, min(a.x1, b.x1) - max(a.x0, b.x0))
    return overlap / max(1.0, min(a.width, b.width))


def _render_anchor_crop(page: fitz.Page, bbox: fitz.Rect, path: Path) -> tuple[int, int] | None:
    try:
        pix = page.get_pixmap(clip=bbox, dpi=300)
        pix.save(str(path))
        return pix.width, pix.height
    except Exception as exc:  # noqa: BLE001
        log(
            "ingest.pdf.caption_anchor.render_fail",
            page=page.number + 1,
            bbox=[round(v, 2) for v in bbox],
            error=f"{type(exc).__name__}: {exc}"[:200],
        )
        return None


def _anchor_crop_name(anchor: dict[str, Any], prefix: str) -> str:
    label = re.sub(r"[^a-zA-Z0-9]+", "_", str(anchor.get("label") or "x")).strip("_") or "x"
    page = int(anchor.get("page") or 0)
    return f"p{page:03d}_anchor_{prefix}_{label}.png"


def dedup_tables_against_figures(
    tables: list[PdfTableCandidate],
    figures: list[PdfFigureCandidate],
    *,
    containment_frac: float = 0.70,
) -> list[PdfTableCandidate]:
    """Drop a table candidate when a same-page vector figure bbox covers
    ≥ `containment_frac` of its bbox — stops "figure that looks like a
    table" regions from being processed twice (once via caption matching
    on the figure side, once via VLM table parse).

    Raster figure candidates (no PDF-point bbox) are ignored for dedup.
    The check is asymmetric: we drop the TABLE, never the figure, because
    the figure path handles composite visuals better than the table path.
    """
    if not tables or not figures:
        return list(tables)

    # Index figures with bboxes by page.
    by_page: dict[int, list[PdfFigureCandidate]] = {}
    for f in figures:
        if f.bbox_pt is None or f.strategy != "vector":
            continue
        by_page.setdefault(f.page, []).append(f)

    keep: list[PdfTableCandidate] = []
    for t in tables:
        if t.protected_anchor:
            log(
                "ingest.pdf.table_candidate.protected_from_figure_dedup",
                page=t.page,
                label=t.anchor_label,
                reason=t.anchor_reason,
            )
            keep.append(t)
            continue
        same_page_figs = by_page.get(t.page, [])
        tx0, ty0, tx1, ty1 = t.bbox_pt
        t_area = max(1.0, (tx1 - tx0) * (ty1 - ty0))
        dropped = False
        for f in same_page_figs:
            assert f.bbox_pt is not None  # guard above
            fx0, fy0, fx1, fy1 = f.bbox_pt
            ix0 = max(tx0, fx0); iy0 = max(ty0, fy0)
            ix1 = min(tx1, fx1); iy1 = min(ty1, fy1)
            if ix1 <= ix0 or iy1 <= iy0:
                continue
            overlap = (ix1 - ix0) * (iy1 - iy0)
            if overlap / t_area >= containment_frac:
                dropped = True
                break
        if not dropped:
            keep.append(t)
    return keep


def extract_page_text(
    doc: fitz.Document,
    max_chars_per_page: int = 4000,
    *,
    max_page: int | None = None,
) -> list[str]:
    """Extract text per page as a list of strings (1-indexed: index 0
    is page 1). Truncates each page to `max_chars_per_page` to keep
    downstream prompts bounded for very dense pages. Lossless for
    anything under the cap.
    """
    out: list[str] = []
    for page_num, page in enumerate(doc, start=1):
        if max_page is not None and page_num > max_page:
            break
        txt = page.get_text("text") or ""
        if len(txt) > max_chars_per_page:
            txt = txt[:max_chars_per_page] + "\n[…page truncated…]"
        out.append(txt)
    return out


def detect_scanned_pdf(doc: fitz.Document) -> bool:
    """Heuristic: if the whole doc has almost no extractable text AND
    no vector drawings, it's almost certainly a scanned PDF. Caller
    (ingest_document) raises ScannedPdfError so the user gets a clear
    message instead of a silent zero-figure result.
    """
    total_text = 0
    total_drawings = 0
    for page in doc:
        total_text += len(page.get_text("text"))
        if total_text > 400:
            return False
        total_drawings += len(page.get_drawings())
        if total_drawings > 0:
            return False
    return total_text < 400 and total_drawings == 0


def dedup_raster_vector(
    candidates: list[PdfFigureCandidate],
    *,
    containment_frac: float = 0.80,
    raster_min_side_px: int = 200,
    major_raster_min_side_px: int = 600,
    major_raster_min_bbox_area_pt: float = 7000.0,
    major_raster_vector_area_ratio: float = 2.0,
    component_containment_frac: float = 0.90,
    component_area_ratio: float = 1.35,
) -> list[PdfFigureCandidate]:
    """Dedup rules:

    Per page, for each vector cluster V, look at every raster R on the
    same page whose position we can test. We only know a raster's
    bbox_pt when PyMuPDF exposes page placement metadata; otherwise this
    defaults to a no-op and both candidates are kept — caption matching
    will reject the duplicate.

    Explicit containment rules for callers that populate raster bbox_pt:
    - raster wins if its bbox covers ≥ `containment_frac` of the vector
      cluster and the raster is printable;
    - a high-resolution, page-significant placed raster wins if a much
      broader vector cluster wraps it; PyMuPDF often merges nearby
      text/tables into these vector bboxes, while the embedded image is
      the clean source;
    - vector wins if it covers nearly all of a smaller raster component.
      This removes internal photos/icons/waveforms that belong to a
      larger paper figure crop.
    """
    by_page: dict[int, list[PdfFigureCandidate]] = {}
    for c in candidates:
        by_page.setdefault(c.page, []).append(c)

    keep: list[PdfFigureCandidate] = []
    for page, cands in by_page.items():
        vecs = [c for c in cands if c.strategy == "vector"]
        rasters = [c for c in cands if c.strategy == "raster"]
        dropped_vec_ids: set[int] = set()
        dropped_raster_ids: set[int] = set()

        for vi, v in enumerate(vecs):
            if v.bbox_pt is None:
                continue
            vx0, vy0, vx1, vy1 = v.bbox_pt
            v_area = max(1.0, (vx1 - vx0) * (vy1 - vy0))
            for ri, r in enumerate(rasters):
                if r.bbox_pt is None:
                    continue
                rx0, ry0, rx1, ry1 = r.bbox_pt
                r_area = max(1.0, (rx1 - rx0) * (ry1 - ry0))
                ix0 = max(vx0, rx0); iy0 = max(vy0, ry0)
                ix1 = min(vx1, rx1); iy1 = min(vy1, ry1)
                if ix1 <= ix0 or iy1 <= iy0:
                    continue
                overlap = (ix1 - ix0) * (iy1 - iy0)
                if (
                    overlap / r_area >= component_containment_frac
                    and v_area / r_area >= major_raster_vector_area_ratio
                    and r_area >= major_raster_min_bbox_area_pt
                    and min(r.width_px, r.height_px) >= major_raster_min_side_px
                ):
                    dropped_vec_ids.add(vi)
                    continue
                if (
                    overlap / r_area >= component_containment_frac
                    and v_area / r_area >= component_area_ratio
                ):
                    dropped_raster_ids.add(ri)
                    continue
                if overlap / v_area >= containment_frac and min(r.width_px, r.height_px) >= raster_min_side_px:
                    dropped_vec_ids.add(vi)
                    break

        for i, v in enumerate(vecs):
            if i not in dropped_vec_ids:
                keep.append(v)
        for i, r in enumerate(rasters):
            if i not in dropped_raster_ids:
                keep.append(r)

    # Stable by (page, strategy priority: raster first, then vector idx).
    return sorted(keep, key=lambda c: (c.page, 0 if c.strategy == "raster" else 1,
                                       c.path.name))
