"""Conservative local checks for paper-source PDFs."""

from __future__ import annotations

import re
from pathlib import Path
from statistics import median
from typing import Any

import fitz


ISSUE_ID = "paper_source_generated_poster_detected"
REPAIR_ROUTE = "replace_paper_source"
INPUT_ISSUE_ID = "paper_source_unreadable"

_LARGE_CANVAS_MIN_AREA_PT2 = 1_000_000.0
_LARGE_CANVAS_MIN_EDGE_PT = 1_000.0
_MAX_PANEL_BLOCK_WIDTH_RATIO = 0.46
_MIN_BLOCKS_PER_REGION = 2
_MIN_PANEL_HEADINGS = 4
_MIN_HEADING_REGIONS = 3
_MIN_POSTER_BODY_FONT_PT = 12.0
_POSTER_FILENAME_RE = re.compile(r"(?:^|[_\-.\s])poster(?:$|[_\-.\s])", re.IGNORECASE)
_SECTION_HEADING_RE = re.compile(
    r"^(?:\d+(?:\.\d+)*[.)]?\s+\S+|abstract|introduction|method|results?|"
    r"analysis|limitations?|takeaways?|discussion|conclusion|references?)\b",
    re.IGNORECASE,
)


class PaperSourceSanityError(ValueError):
    """Raised when a required paper source is confidently poster-like."""

    issue_id = ISSUE_ID
    repair_route = REPAIR_ROUTE

    def __init__(self, path: Path, report: dict[str, Any]) -> None:
        self.path = path
        self.report = report
        super().__init__(
            f"{path.name} looks like a generated poster PDF, not a source paper. "
            "Attach the original paper PDF as the source; use the poster only as "
            "a visual reference."
        )


class PaperSourceInputError(ValueError):
    """Raised when a PDF cannot be inspected as a paper source."""

    issue_id = INPUT_ISSUE_ID
    repair_route = REPAIR_ROUTE

    def __init__(self, path: Path, cause: Exception) -> None:
        self.path = path
        self.cause = cause
        self.report = {
            "kind": "paper_source_sanity",
            "version": 1,
            "source_file": str(path),
            "classification": "unreadable",
            "hard_reject": False,
            "input_rejected": True,
        }
        super().__init__(
            f"{path.name} could not be read as a PDF paper source. "
            "Attach a valid original paper PDF."
        )


class PaperSourceVerificationError(ValueError):
    """Raised when a reused/resumed run cannot prove its original paper source."""

    issue_id = "paper_source_sanity_unverifiable"
    repair_route = REPAIR_ROUTE

    def __init__(self, message: str) -> None:
        self.report = {
            "kind": "paper_source_sanity",
            "version": 1,
            "classification": "unverifiable",
            "hard_reject": False,
            "input_rejected": True,
        }
        super().__init__(message)


def inspect_paper_source_pdf(path: str | Path) -> dict[str, Any]:
    """Return a JSON-serializable report without deciding from weak signals alone."""
    pdf_path = Path(path).expanduser()
    try:
        with fitz.open(pdf_path) as doc:
            page_count = len(doc)
            metadata = dict(doc.metadata or {})
            if page_count:
                first_page = doc[0]
                page_width = float(first_page.rect.width)
                page_height = float(first_page.rect.height)
                layout = _inspect_page_layout(first_page)
            else:
                page_width = 0.0
                page_height = 0.0
                layout = _empty_layout_report()

            text = "\n".join(page.get_text("text") for page in doc)
            image_count = sum(len(page.get_images(full=True)) for page in doc)
    except (fitz.FileDataError, fitz.EmptyFileError, OSError, RuntimeError, ValueError) as exc:
        raise PaperSourceInputError(pdf_path, exc) from exc

    single_page = page_count == 1
    page_area = page_width * page_height
    large_poster_canvas = (
        page_area >= _LARGE_CANVAS_MIN_AREA_PT2
        and max(page_width, page_height) >= _LARGE_CANVAS_MIN_EDGE_PT
    )
    panelized_layout = bool(layout["panelized_multi_region_layout"])
    lowered_text = text.casefold()
    has_abstract = bool(re.search(r"\babstract\b", lowered_text))
    has_references = bool(re.search(r"\b(?:references|bibliography)\b", lowered_text))
    article_flow_signal = (
        has_abstract
        and has_references
        and float(layout.get("median_font_size_pt") or 0.0) <= 14.0
    )
    hard_reject = (
        single_page
        and large_poster_canvas
        and panelized_layout
        and not article_flow_signal
    )
    producer = str(metadata.get("producer") or "")
    creator = str(metadata.get("creator") or "")

    return {
        "kind": "paper_source_sanity",
        "version": 1,
        "source_file": str(pdf_path),
        "classification": "poster_like" if hard_reject else "paper_or_ambiguous",
        "hard_reject": hard_reject,
        "single_page": single_page,
        "large_poster_canvas": large_poster_canvas,
        "panelized_multi_region_layout": panelized_layout,
        "article_flow_signal": article_flow_signal,
        "page_count": page_count,
        "page": {
            "width_pt": round(page_width, 3),
            "height_pt": round(page_height, 3),
            "area_pt2": round(page_area, 3),
            "aspect_ratio": round(
                max(page_width, page_height) / min(page_width, page_height),
                4,
            ) if min(page_width, page_height) > 0 else None,
        },
        "layout": layout,
        "content": {
            "image_count": image_count,
            "has_abstract": has_abstract,
            "has_references": has_references,
        },
        "metadata": {
            "creator": creator,
            "producer": producer,
            "filename_mentions_poster": bool(_POSTER_FILENAME_RE.search(pdf_path.name)),
        },
    }


def assert_valid_paper_source_pdf(path: str | Path) -> dict[str, Any]:
    """Return the report or raise the typed high-confidence source error."""
    pdf_path = Path(path).expanduser()
    report = inspect_paper_source_pdf(pdf_path)
    if report["hard_reject"]:
        raise PaperSourceSanityError(pdf_path, report)
    return report


def _inspect_page_layout(page: fitz.Page) -> dict[str, Any]:
    page_width = float(page.rect.width)
    blocks: list[dict[str, Any]] = []
    font_sizes: list[float] = []
    text_dict = page.get_text("dict")
    for raw_block in text_dict.get("blocks") or []:
        if raw_block.get("type") != 0:
            continue
        spans = [
            span
            for line in raw_block.get("lines") or []
            for span in line.get("spans") or []
            if str(span.get("text") or "").strip()
        ]
        if not spans:
            continue
        text = " ".join(str(span.get("text") or "").strip() for span in spans).strip()
        bbox = tuple(float(value) for value in raw_block.get("bbox") or (0, 0, 0, 0))
        if len(bbox) != 4 or not text:
            continue
        sizes = [float(span.get("size") or 0.0) for span in spans]
        font_sizes.extend(size for size in sizes if size > 0)
        blocks.append({"text": text, "bbox": bbox, "max_font_size": max(sizes or [0.0])})

    body_font_size = float(median(font_sizes)) if font_sizes else 0.0
    heading_size = max(14.0, body_font_size * 1.3)
    region_block_counts = [0, 0, 0]
    region_heading_counts = [0, 0, 0]
    panel_heading_count = 0

    for block in blocks:
        x0, _y0, x1, _y1 = block["bbox"]
        block_width = max(0.0, x1 - x0)
        if page_width <= 0 or block_width > page_width * _MAX_PANEL_BLOCK_WIDTH_RATIO:
            continue
        center_ratio = ((x0 + x1) / 2.0) / page_width
        region = min(2, max(0, int(center_ratio * 3)))
        region_block_counts[region] += 1
        text = str(block["text"])
        looks_like_heading = len(text) <= 120 and (
            float(block["max_font_size"]) >= heading_size
            or bool(_SECTION_HEADING_RE.match(text))
        )
        if looks_like_heading:
            panel_heading_count += 1
            region_heading_counts[region] += 1

    occupied_region_count = sum(
        count >= _MIN_BLOCKS_PER_REGION for count in region_block_counts
    )
    heading_region_count = sum(count > 0 for count in region_heading_counts)
    panelized = (
        occupied_region_count >= 3
        and panel_heading_count >= _MIN_PANEL_HEADINGS
        and heading_region_count >= _MIN_HEADING_REGIONS
        and body_font_size >= _MIN_POSTER_BODY_FONT_PT
    )
    return {
        "text_block_count": len(blocks),
        "median_font_size_pt": round(body_font_size, 3),
        "region_block_counts": region_block_counts,
        "occupied_region_count": occupied_region_count,
        "panel_heading_count": panel_heading_count,
        "heading_region_count": heading_region_count,
        "panelized_multi_region_layout": panelized,
    }


def _empty_layout_report() -> dict[str, Any]:
    return {
        "text_block_count": 0,
        "median_font_size_pt": 0.0,
        "region_block_counts": [0, 0, 0],
        "occupied_region_count": 0,
        "panel_heading_count": 0,
        "heading_region_count": 0,
        "panelized_multi_region_layout": False,
    }
