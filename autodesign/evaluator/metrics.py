"""Common deterministic metrics for evaluated artifacts."""

from __future__ import annotations

from functools import lru_cache
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import fitz
from PIL import Image, ImageFilter, ImageStat

from autodesign.quality_assets import lint_html_quality

from .schema import ArtifactSnapshot, EvaluationFinding, MetricBundle


# Number core (sign, thousands separators, decimal, scientific) + an optional
# directly-attached suffix (%, ×, or up to 5 letters: a unit, magnitude, or — when
# it is an unrecognized multi-letter blob — an OCR merge artifact like "1LoRA").
NUMERIC_TOKEN_RE = re.compile(
    r"(?<![\w.])([+-]?\d[\d,]*(?:\.\d+)?(?:[eE][+-]?\d+)?)([%×]|[A-Za-z]{1,5})?"
)

# --- numeric grounding calibration (commented; tune on fixtures + real posters) --
_MAGNITUDE_SUFFIX = {"k": 1e3, "m": 1e6, "b": 1e9, "g": 1e9, "t": 1e12}
# Multi-char units that legitimately attach to a number. A directly-attached alpha
# suffix that is NOT one of these and is >= 2 chars is treated as an OCR merge.
_UNIT_ALLOW = {
    "x", "ms", "s", "h", "hz", "khz", "mhz", "ghz", "gb", "mb", "kb", "tb",
    "fps", "db", "px", "pt", "em", "pts", "min", "sec", "ep", "epoch", "iter",
}
SALIENCE_MIN_INT = 21          # bare integers <= 20 are "trivial" (counts/sections)
GROUNDED_REL_TOL = 0.001       # within 0.1% of a paper value == grounded (exact-ish)
NEARMISS_REL_TOL = 0.08        # within 8% (or one digit off) == likely OCR misread


def file_integrity_metrics(snapshot: ArtifactSnapshot) -> tuple[MetricBundle, list[EvaluationFinding]]:
    path = Path(snapshot.artifact_path)
    exists = path.exists()
    size = path.stat().st_size if exists and path.is_file() else snapshot.metadata.get("size_bytes")
    metrics = {
        "exists": exists,
        "kind": snapshot.artifact_kind,
        "size_bytes": size,
        "has_text": bool(snapshot.text.strip()),
        "has_html": bool(snapshot.html),
        "has_preview_image": bool(snapshot.preview_image),
        "width": snapshot.width,
        "height": snapshot.height,
        "page_count": snapshot.page_count,
        "resolved_files": dict(snapshot.resolved_files),
    }
    findings: list[EvaluationFinding] = []
    if not exists:
        findings.append(EvaluationFinding("artifact-missing", "P0", "Artifact path does not exist.", "file_integrity", "render_integrity"))
    elif size == 0:
        findings.append(EvaluationFinding("artifact-empty", "P0", "Artifact file is empty.", "file_integrity", "render_integrity"))
    if snapshot.metadata.get("error"):
        findings.append(EvaluationFinding(
            "artifact-snapshot-error",
            "P0",
            "Artifact snapshot adapter reported an error.",
            "file_integrity",
            "render_integrity",
            {"error": snapshot.metadata.get("error")},
        ))
    if snapshot.artifact_kind in {"html", "run_dir"} and not (snapshot.html or snapshot.resolved_files.get("html")):
        findings.append(EvaluationFinding("html-missing", "P1", "No HTML file was resolved.", "file_integrity", "render_integrity"))
    return MetricBundle("file_integrity", metrics, _status_from_findings(findings), findings_count=len(findings)), findings


def image_density_metrics(
    snapshot: ArtifactSnapshot,
    *,
    artifact_type: str,
) -> tuple[MetricBundle, list[EvaluationFinding]]:
    if not snapshot.preview_image:
        return MetricBundle("image_density", {"available": False}, "skipped"), []
    path = Path(snapshot.preview_image)
    try:
        with Image.open(path) as image:
            image = image.convert("RGB")
            image.thumbnail((900, 900), Image.Resampling.LANCZOS)
            width, height = image.size
            gray = image.convert("L")
            pixels = list(gray.getdata())
            total = max(1, len(pixels))
            non_white = sum(1 for value in pixels if value < 245)
            dark = sum(1 for value in pixels if value < 96)
            edges = gray.filter(ImageFilter.FIND_EDGES)
            edge_density = sum(1 for value in edges.getdata() if value > 28) / total
            row_nonwhite = _row_nonwhite_ratios(gray, width, height)
            longest_blank = _longest_blank_run_ratio(row_nonwhite, threshold=0.018)
            stat = ImageStat.Stat(gray)
            metrics = {
                "available": True,
                "width": width,
                "height": height,
                "nonwhite_pixel_ratio": round(non_white / total, 4),
                "dark_ink_ratio": round(dark / total, 4),
                "edge_density": round(edge_density, 4),
                "longest_blank_vertical_run_ratio": round(longest_blank, 4),
                "mean_luma": round(float(stat.mean[0]), 2),
                "luma_stddev": round(float(stat.stddev[0]), 2),
            }
    except Exception as exc:
        finding = EvaluationFinding(
            "preview-image-unreadable",
            "P0",
            "Preview image could not be opened for density metrics.",
            "image_density",
            "render_integrity",
            {"path": str(path), "error": f"{type(exc).__name__}: {exc}"},
        )
        return MetricBundle("image_density", {"available": False, "error": finding.details["error"]}, "error", findings_count=1), [finding]
    findings: list[EvaluationFinding] = []
    if artifact_type != "poster":
        metrics["threshold_findings_enabled"] = False
        return MetricBundle("image_density", metrics, findings_count=0), findings
    metrics["threshold_findings_enabled"] = True
    if metrics["nonwhite_pixel_ratio"] < 0.08:
        findings.append(EvaluationFinding("preview-too-empty", "P1", "Preview image appears very sparse.", "image_density", "visual_density", metrics))
    if metrics["longest_blank_vertical_run_ratio"] > 0.22:
        findings.append(EvaluationFinding("preview-long-blank-band", "P1", "Preview contains a long vertical blank band.", "image_density", "visual_density", metrics))
    if metrics["dark_ink_ratio"] > 0.75:
        findings.append(EvaluationFinding("preview-too-dark", "P1", "Preview image is dominated by dark pixels.", "image_density", "visual_density", metrics))
    return MetricBundle("image_density", metrics, _status_from_findings(findings), findings_count=len(findings)), findings


# Visual-evidence thresholds. Advisory only — the visual_evidence_use SCORE is the
# VLM's. CV figure detection is threshold-based and misses sparse line-charts + text
# tables, so these never hard-cap the score; they ground the VLM's judgment.
# Calibrated on 96 real posters (figure_area_ratio p50=0.11, p90=0.24; text_coverage
# clustered 0.30-0.40) PLUS synthetic screenshot-wall probes. The "wall" discriminator
# is LOW TEXT, not high figure area: CV merges adjacent figures and caps single-region
# area at 0.28, so figure_area_ratio never reaches a "wall" level even on a literal
# wall (synthetic walls measured 0.0 and 0.26). But a real poster always has text
# coverage >= 0.25 while a wall has ~0, so text_coverage <= 0.15 (with some figure
# area present) is the reachable, false-positive-safe wall signal.
VISUAL_EVIDENCE_WALL_AREA = 0.15   # some figure area present ...
VISUAL_EVIDENCE_WALL_TEXT = 0.15   # ... AND text coverage <= this (a wall has ~0; real posters >= 0.25)
VISUAL_EVIDENCE_GROUP_GAP_FRAC = 0.08
VISUAL_EVIDENCE_GROUP_MAX_CANVAS_GAP_FRAC = 0.0125
VISUAL_EVIDENCE_GROUP_AXIS_OVERLAP_FRAC = 0.25
VISUAL_EVIDENCE_THUMBNAIL_SHORT_EDGE_RATIO = 0.10


PAPER_BODY_COPY_NGRAM = 8
PAPER_BODY_COPY_MIN_GRAM_CHARS = 35
PAPER_BODY_SCAN_MIN_WORDS = 600
PAPER_BODY_SEVERE_MIN_WORDS = 1100
PAPER_BODY_SEVERE_MIN_TEXT_COVERAGE = 0.42
PAPER_BODY_SEVERE_MIN_HIT_RATIO = 0.08
PAPER_BODY_SEVERE_MIN_COPIED_TOKENS = 450
PAPER_BODY_SEVERE_MIN_COPIED_RATIO = 0.30
PAPER_BODY_DENSE_CROP_MIN_WORDS = 1300
PAPER_BODY_DENSE_CROP_MIN_TEXT_COVERAGE = 0.34
PAPER_BODY_DENSE_CROP_MIN_HIT_RATIO = 0.05
PAPER_BODY_DENSE_CROP_MIN_COPIED_TOKENS = 220
PAPER_BODY_DENSE_CROP_MIN_BODY_SEGMENTS = 20
PAPER_BODY_DENSE_CROP_MIN_BODY_AREA = 0.07
PAPER_BODY_PAGE_CROP_MIN_WORDS = 1250
PAPER_BODY_PAGE_CROP_MODERATE_MIN_WORDS = 1000
PAPER_BODY_PAGE_CROP_MIN_TEXT_COVERAGE = 0.32
PAPER_BODY_PAGE_CROP_MIN_HIT_RATIO = 0.05
PAPER_BODY_PAGE_CROP_MIN_COPIED_TOKENS = 150
PAPER_BODY_PAGE_CROP_MIN_COPIED_BODY_SEGMENTS = 10
PAPER_BODY_PAGE_CROP_MIN_COPIED_BODY_AREA = 0.07
PAPER_BODY_PAGE_CROP_MIN_BODY_SEGMENTS = 180
PAPER_BODY_PAGE_CROP_MAX_MEDIAN_HEIGHT_REF_PX = 18.5
PAPER_BODY_DISTRIBUTED_CROP_MIN_COPIED_TOKENS = 200
PAPER_BODY_DISTRIBUTED_CROP_MIN_COPIED_BODY_SEGMENTS = 15
PAPER_BODY_DISTRIBUTED_CROP_MIN_COPIED_BODY_AREA = 0.04
PAPER_BODY_DISTRIBUTED_CROP_MIN_BODY_SEGMENTS = 200
PAPER_BODY_DISTRIBUTED_CROP_MIN_MICROTEXT_RATIO = 0.75
PAPER_BODY_DISTRIBUTED_CROP_MIN_PAGE_BODY_AREA = 0.28
PAPER_BODY_MODERATE_MIN_WORDS = 800
PAPER_BODY_MODERATE_MIN_TEXT_COVERAGE = 0.25
PAPER_BODY_MODERATE_MIN_HIT_RATIO = 0.06
PAPER_BODY_MODERATE_MIN_COPIED_TOKENS = 250
PAPER_BODY_MODERATE_MIN_COPIED_RATIO = 0.22
PAPER_BODY_REGIONAL_CROP_MIN_WORDS = 800
PAPER_BODY_REGIONAL_CROP_MIN_HIT_RATIO = 0.12
PAPER_BODY_REGIONAL_CROP_MIN_COPIED_TOKENS = 220
PAPER_BODY_REGIONAL_CROP_MIN_COPIED_RATIO = 0.24
PAPER_BODY_REGIONAL_CROP_MIN_BODY_SEGMENTS = 15
PAPER_BODY_REGIONAL_CROP_MIN_BODY_AREA = 0.10
PAPER_BODY_REGIONAL_CROP_MIN_PAGE_BODY_AREA = 0.18
PAPER_BODY_REGIONAL_CROP_MAX_MEDIAN_HEIGHT_REF_PX = 21.0
PAPER_BODY_SPARSE_CROP_MIN_WORDS = 650
PAPER_BODY_SPARSE_CROP_MAX_WORDS = 799
PAPER_BODY_SPARSE_CROP_MAX_TEXT_COVERAGE = 0.24
PAPER_BODY_SPARSE_CROP_MIN_HIT_RATIO = 0.12
PAPER_BODY_SPARSE_CROP_MIN_COPIED_TOKENS = 180
PAPER_BODY_SPARSE_CROP_MIN_COPIED_RATIO = 0.24
PAPER_BODY_SPARSE_CROP_MIN_BODY_SEGMENTS = 12
PAPER_BODY_SPARSE_CROP_MIN_BODY_AREA = 0.07
PAPER_BODY_SPARSE_CROP_MIN_PAGE_BODY_AREA = 0.18
PAPER_BODY_CATASTROPHIC_MIN_COPIED_BODY_AREA = 0.25
PAPER_BODY_CATASTROPHIC_MIN_COPIED_RATIO = 0.30
PAPER_BODY_CATASTROPHIC_MIN_PAGE_BODY_AREA = 0.40
PAPER_BODY_CATASTROPHIC_MAX_BODY_SEGMENTS = 170
PAPER_BODY_CATASTROPHIC_MIN_MICROTEXT_RATIO = 0.55
_COPY_WORD_RE = re.compile(r"[a-z0-9]+")


def visual_evidence_metrics(
    figure_rects: list[dict[str, Any]] | None,
    text_coverage: float | None,
    *,
    figure_detection_available: bool = True,
) -> tuple[MetricBundle, list[EvaluationFinding]]:
    """Deterministic visual-evidence GROUNDING for visual_evidence_use.

    From the figure-shaped CV regions (``basic_layout_integrity.figure_region_rects``),
    report how much visual evidence the poster carries — figure count + canvas area —
    and two advisory failure flags: no figures at all, and a 'screenshot wall' (figures
    cover most of the canvas with little authored text). These GROUND the VLM judge,
    they are not a score: CV is threshold-based and misses sparse line-charts and text
    tables, so a figure-bearing poster can read as 0 figures here. The VLM, which sees
    the image, owns the final visual_evidence_use score (so these never hard-cap it).
    """
    if not figure_detection_available:
        metrics = {
            "available": False,
            "status": "degraded",
            "figure_detection_available": False,
            "note": "CV figure detection was unavailable; do not interpret missing figure regions as no visual evidence.",
        }
        return MetricBundle("visual_evidence", metrics, "degraded"), []

    rects = [_normalized_visual_rect(r) for r in (figure_rects or []) if isinstance(r, dict)]
    rects = [r for r in rects if r is not None]
    groups = _group_visual_evidence_rects(rects)
    count = len(rects)
    area_ratio = round(sum(float(r.get("area_ratio") or 0.0) for r in rects), 4)
    group_count = len(groups)
    group_area_ratio = round(sum(float(r.get("area_ratio") or 0.0) for r in groups), 4)
    largest_group_area_ratio = round(max((float(r.get("area_ratio") or 0.0) for r in groups), default=0.0), 4)
    group_short_edge_ratios = [float(r.get("short_edge_ratio") or 0.0) for r in groups]
    median_group_short_edge_ratio = round(_median_float(group_short_edge_ratios), 4)
    thumbnail_count = sum(
        1 for value in group_short_edge_ratios
        if 0.0 < value <= VISUAL_EVIDENCE_THUMBNAIL_SHORT_EDGE_RATIO
    )
    tcov = round(float(text_coverage), 4) if text_coverage is not None else None
    no_figures = group_count == 0
    wall = (
        group_area_ratio >= VISUAL_EVIDENCE_WALL_AREA
        and tcov is not None
        and tcov <= VISUAL_EVIDENCE_WALL_TEXT
    )
    cramming = _detect_figure_cramming(groups)
    metrics: dict[str, Any] = {
        "available": True,
        "evidence_group_count": group_count,
        "evidence_group_area_ratio": group_area_ratio,
        "largest_group_area_ratio": largest_group_area_ratio,
        "median_group_short_edge_ratio": median_group_short_edge_ratio,
        "thumbnail_group_count": thumbnail_count,
        "evidence_group_rects": groups[:24],
        "figure_region_count": count,
        "figure_area_ratio": area_ratio,
        "text_coverage": tcov,
        "no_figures_detected": no_figures,
        "possible_screenshot_wall": wall,
        "figure_cramming": cramming["crammed"],
        "cramming_cluster_size": cramming["cluster_size"],
        "cramming_fill_ratio": cramming["fill_ratio"],
        "figure_rects": rects[:24],
        "debug_note": "figure_region_count and figure_area_ratio are raw CV detector outputs for debugging; use evidence_group_* for judge grounding.",
        "note": "Advisory grounding for the VLM judge; grouped CV may miss sparse line-charts/tables.",
        "figure_detection_available": True,
    }
    findings: list[EvaluationFinding] = []
    if cramming["crammed"]:
        findings.append(EvaluationFinding(
            "visual-evidence-cramming",
            "P2",
            f"{cramming['cluster_size']} figures are packed tightly into one block "
            f"(fill {round(cramming['fill_ratio'] * 100)}%) — a likely figure collage/cramming, "
            "which real conference posters avoid; verify against the image.",
            "visual_evidence",
            "visual_evidence_use",
            {"cluster_size": cramming["cluster_size"], "fill_ratio": cramming["fill_ratio"]},
        ))
    if no_figures:
        findings.append(EvaluationFinding(
            "visual-evidence-none",
            "P2",
            "No figure/chart/table regions detected on the poster (CV is threshold-based "
            "and may miss sparse line-charts or text tables — verify against the image).",
            "visual_evidence",
            "visual_evidence_use",
            {"evidence_group_area_ratio": group_area_ratio},
        ))
    if wall:
        findings.append(EvaluationFinding(
            "visual-evidence-wall",
            "P2",
            f"Evidence groups cover {round(group_area_ratio * 100)}% of the canvas with low text coverage "
            f"({tcov}) — possible screenshot wall; confirm figures carry readouts/synthesis.",
            "visual_evidence",
            "visual_evidence_use",
            {"evidence_group_area_ratio": group_area_ratio, "text_coverage": tcov},
        ))
    return MetricBundle("visual_evidence", metrics, _status_from_findings(findings), findings_count=len(findings)), findings


def paper_body_screenshot_metrics(
    paper: Path | None,
    ocr: dict[str, Any],
    *,
    paper_text: str | None = None,
) -> tuple[MetricBundle, list[EvaluationFinding]]:
    """Detect large pasted paper-body screenshots masquerading as poster content.

    Normal posters are source-grounded and will share paper vocabulary. This detector
    fires when the rendered poster has a large amount of OCR prose plus either many
    exact contiguous word runs copied from the paper or a page-like microtext block
    with source-paper overlap. That combination is the failure mode where a model
    drops a PDF body crop into the poster: density/source metrics look high, but the
    design has not synthesized the paper.
    """
    if not paper:
        return MetricBundle("paper_body_screenshot", {"available": False, "reason": "no_paper"}, "skipped"), []
    if not ocr.get("available"):
        return MetricBundle(
            "paper_body_screenshot",
            {"available": False, "reason": str(ocr.get("reason") or ocr.get("error") or "ocr_unavailable")},
            "skipped",
        ), []
    poster_text = str(ocr.get("text") or "")
    poster_tokens = _copy_words(poster_text)
    if len(poster_tokens) < PAPER_BODY_SCAN_MIN_WORDS:
        return MetricBundle(
            "paper_body_screenshot",
            {
                "available": True,
                "status": "ok",
                "severity_level": "none",
                "poster_token_count": len(poster_tokens),
                "ocr_word_count": ocr.get("word_count"),
                "text_coverage_ratio": ocr.get("text_coverage_ratio"),
                "reason": "poster_text_below_scan_floor",
            },
            "ok",
        ), []

    try:
        paper_tokens = _copy_words(paper_text if paper_text is not None else _extract_paper_text(paper))
    except Exception as exc:
        finding = EvaluationFinding(
            "paper-body-source-unreadable",
            "P2",
            "Paper text could not be extracted for paper-body screenshot detection.",
            "paper_body_screenshot",
            "text_density",
            {"paper_path": str(paper), "error": f"{type(exc).__name__}: {exc}"},
        )
        return MetricBundle(
            "paper_body_screenshot",
            {"available": False, "error": finding.details["error"], "paper_path": str(paper)},
            "degraded",
            findings_count=1,
        ), [finding]

    scan = _copied_ngram_scan(poster_tokens, paper_tokens, n=PAPER_BODY_COPY_NGRAM)
    segment_metrics = _copied_segment_metrics(
        ocr.get("segments") if isinstance(ocr.get("segments"), list) else [],
        paper_tokens,
        n=PAPER_BODY_COPY_NGRAM,
        image_size=ocr.get("image_size"),
    )
    page_crop_layout = _page_crop_layout_metrics(ocr)
    word_count = _float_metric(ocr.get("word_count"), default=float(len(poster_tokens)))
    text_coverage = _float_metric(ocr.get("text_coverage_ratio"), default=0.0)
    hit_ratio = scan["hit_ratio"]
    copied_tokens = scan["copied_token_count"]
    copied_ratio = scan["copied_token_ratio"]
    severe_by_verbatim_mass = (
        word_count >= PAPER_BODY_SEVERE_MIN_WORDS
        and text_coverage >= PAPER_BODY_SEVERE_MIN_TEXT_COVERAGE
        and hit_ratio >= PAPER_BODY_SEVERE_MIN_HIT_RATIO
        and (
            copied_tokens >= PAPER_BODY_SEVERE_MIN_COPIED_TOKENS
            or copied_ratio >= PAPER_BODY_SEVERE_MIN_COPIED_RATIO
        )
    )
    severe_by_dense_crop = (
        word_count >= PAPER_BODY_DENSE_CROP_MIN_WORDS
        and text_coverage >= PAPER_BODY_DENSE_CROP_MIN_TEXT_COVERAGE
        and hit_ratio >= PAPER_BODY_DENSE_CROP_MIN_HIT_RATIO
        and copied_tokens >= PAPER_BODY_DENSE_CROP_MIN_COPIED_TOKENS
        and segment_metrics["copied_body_segment_count"] >= PAPER_BODY_DENSE_CROP_MIN_BODY_SEGMENTS
        and segment_metrics["copied_body_segment_area_ratio"] >= PAPER_BODY_DENSE_CROP_MIN_BODY_AREA
    )
    severe_by_page_crop = (
        word_count >= PAPER_BODY_PAGE_CROP_MIN_WORDS
        and text_coverage >= PAPER_BODY_PAGE_CROP_MIN_TEXT_COVERAGE
        and hit_ratio >= PAPER_BODY_PAGE_CROP_MIN_HIT_RATIO
        and copied_tokens >= PAPER_BODY_PAGE_CROP_MIN_COPIED_TOKENS
        and segment_metrics["copied_body_segment_count"] >= PAPER_BODY_PAGE_CROP_MIN_COPIED_BODY_SEGMENTS
        and segment_metrics["copied_body_segment_area_ratio"] >= PAPER_BODY_PAGE_CROP_MIN_COPIED_BODY_AREA
        and page_crop_layout["body_segment_count"] >= PAPER_BODY_PAGE_CROP_MIN_BODY_SEGMENTS
        and page_crop_layout["median_body_segment_height_ref_px"] <= PAPER_BODY_PAGE_CROP_MAX_MEDIAN_HEIGHT_REF_PX
    )
    severe_by_distributed_page_crop = (
        word_count >= PAPER_BODY_PAGE_CROP_MIN_WORDS
        and text_coverage >= PAPER_BODY_PAGE_CROP_MIN_TEXT_COVERAGE
        and hit_ratio >= PAPER_BODY_PAGE_CROP_MIN_HIT_RATIO
        and copied_tokens >= PAPER_BODY_DISTRIBUTED_CROP_MIN_COPIED_TOKENS
        and segment_metrics["copied_body_segment_count"] >= PAPER_BODY_DISTRIBUTED_CROP_MIN_COPIED_BODY_SEGMENTS
        and segment_metrics["copied_body_segment_area_ratio"] >= PAPER_BODY_DISTRIBUTED_CROP_MIN_COPIED_BODY_AREA
        and page_crop_layout["body_segment_count"] >= PAPER_BODY_DISTRIBUTED_CROP_MIN_BODY_SEGMENTS
        and page_crop_layout["median_body_segment_height_ref_px"] <= PAPER_BODY_PAGE_CROP_MAX_MEDIAN_HEIGHT_REF_PX
        and page_crop_layout["microtext_segment_ratio"] >= PAPER_BODY_DISTRIBUTED_CROP_MIN_MICROTEXT_RATIO
        and page_crop_layout["body_segment_area_ratio"] >= PAPER_BODY_DISTRIBUTED_CROP_MIN_PAGE_BODY_AREA
    )
    severe_by_regional_crop = (
        word_count >= PAPER_BODY_REGIONAL_CROP_MIN_WORDS
        and hit_ratio >= PAPER_BODY_REGIONAL_CROP_MIN_HIT_RATIO
        and copied_tokens >= PAPER_BODY_REGIONAL_CROP_MIN_COPIED_TOKENS
        and copied_ratio >= PAPER_BODY_REGIONAL_CROP_MIN_COPIED_RATIO
        and segment_metrics["copied_body_segment_count"] >= PAPER_BODY_REGIONAL_CROP_MIN_BODY_SEGMENTS
        and segment_metrics["copied_body_segment_area_ratio"] >= PAPER_BODY_REGIONAL_CROP_MIN_BODY_AREA
        and page_crop_layout["body_segment_area_ratio"] >= PAPER_BODY_REGIONAL_CROP_MIN_PAGE_BODY_AREA
        and page_crop_layout["median_body_segment_height_ref_px"] <= PAPER_BODY_REGIONAL_CROP_MAX_MEDIAN_HEIGHT_REF_PX
    )
    severe_by_sparse_crop = (
        PAPER_BODY_SPARSE_CROP_MIN_WORDS <= word_count <= PAPER_BODY_SPARSE_CROP_MAX_WORDS
        and text_coverage <= PAPER_BODY_SPARSE_CROP_MAX_TEXT_COVERAGE
        and hit_ratio >= PAPER_BODY_SPARSE_CROP_MIN_HIT_RATIO
        and copied_tokens >= PAPER_BODY_SPARSE_CROP_MIN_COPIED_TOKENS
        and copied_ratio >= PAPER_BODY_SPARSE_CROP_MIN_COPIED_RATIO
        and segment_metrics["copied_body_segment_count"] >= PAPER_BODY_SPARSE_CROP_MIN_BODY_SEGMENTS
        and segment_metrics["copied_body_segment_area_ratio"] >= PAPER_BODY_SPARSE_CROP_MIN_BODY_AREA
        and page_crop_layout["body_segment_area_ratio"] >= PAPER_BODY_SPARSE_CROP_MIN_PAGE_BODY_AREA
        and page_crop_layout["median_body_segment_height_ref_px"] <= PAPER_BODY_PAGE_CROP_MAX_MEDIAN_HEIGHT_REF_PX
    )
    severe = (
        severe_by_verbatim_mass
        or severe_by_dense_crop
        or severe_by_page_crop
        or severe_by_distributed_page_crop
        or severe_by_regional_crop
        or severe_by_sparse_crop
    )
    moderate_by_verbatim = (
        word_count >= PAPER_BODY_MODERATE_MIN_WORDS
        and text_coverage >= PAPER_BODY_MODERATE_MIN_TEXT_COVERAGE
        and hit_ratio >= PAPER_BODY_MODERATE_MIN_HIT_RATIO
        and (
            copied_tokens >= PAPER_BODY_MODERATE_MIN_COPIED_TOKENS
            or copied_ratio >= PAPER_BODY_MODERATE_MIN_COPIED_RATIO
        )
    )
    moderate_by_page_crop = (
        word_count >= PAPER_BODY_PAGE_CROP_MODERATE_MIN_WORDS
        and text_coverage >= PAPER_BODY_PAGE_CROP_MIN_TEXT_COVERAGE
        and hit_ratio >= PAPER_BODY_PAGE_CROP_MIN_HIT_RATIO
        and copied_tokens >= PAPER_BODY_PAGE_CROP_MIN_COPIED_TOKENS
        and segment_metrics["copied_body_segment_count"] >= PAPER_BODY_PAGE_CROP_MIN_COPIED_BODY_SEGMENTS
        and segment_metrics["copied_body_segment_area_ratio"] >= PAPER_BODY_PAGE_CROP_MIN_COPIED_BODY_AREA
        and page_crop_layout["body_segment_count"] >= PAPER_BODY_PAGE_CROP_MIN_BODY_SEGMENTS
        and page_crop_layout["median_body_segment_height_ref_px"] <= PAPER_BODY_PAGE_CROP_MAX_MEDIAN_HEIGHT_REF_PX
    )
    moderate = moderate_by_verbatim or moderate_by_page_crop
    catastrophic_by_canvas_wall = (
        severe
        and segment_metrics["copied_body_segment_area_ratio"] >= PAPER_BODY_CATASTROPHIC_MIN_COPIED_BODY_AREA
    )
    catastrophic_by_microtext_wall = (
        severe
        and copied_ratio >= PAPER_BODY_CATASTROPHIC_MIN_COPIED_RATIO
        and page_crop_layout["body_segment_area_ratio"] >= PAPER_BODY_CATASTROPHIC_MIN_PAGE_BODY_AREA
        and page_crop_layout["body_segment_count"] <= PAPER_BODY_CATASTROPHIC_MAX_BODY_SEGMENTS
        and page_crop_layout["microtext_segment_ratio"] >= PAPER_BODY_CATASTROPHIC_MIN_MICROTEXT_RATIO
    )
    catastrophic = catastrophic_by_canvas_wall or catastrophic_by_microtext_wall
    severity_level = "catastrophic" if catastrophic else ("severe" if severe else ("moderate" if moderate else "none"))
    severe_reason = (
        "verbatim_mass" if severe_by_verbatim_mass
        else ("dense_paper_crop" if severe_by_dense_crop
              else ("paper_page_microtext_crop" if severe_by_page_crop
                    else ("distributed_paper_page_crop" if severe_by_distributed_page_crop
                          else ("regional_paper_crop" if severe_by_regional_crop
                                else ("sparse_paper_crop" if severe_by_sparse_crop else "")))))
    )
    catastrophic_reason = (
        "copied_body_canvas_wall" if catastrophic_by_canvas_wall
        else ("paper_page_microtext_wall" if catastrophic_by_microtext_wall else "")
    )
    moderate_reason = (
        "verbatim_copying" if moderate_by_verbatim
        else ("paper_page_microtext_crop" if moderate_by_page_crop else "")
    )
    metrics: dict[str, Any] = {
        "available": True,
        "status": severity_level,
        "severity_level": severity_level,
        "paper_path": str(paper),
        "ngram_size": PAPER_BODY_COPY_NGRAM,
        "poster_token_count": len(poster_tokens),
        "ocr_word_count": int(word_count),
        "text_coverage_ratio": round(text_coverage, 4),
        "exact_ngram_count": scan["ngram_count"],
        "exact_ngram_hit_count": scan["hit_count"],
        "exact_ngram_hit_ratio": round(hit_ratio, 4),
        "copied_token_count": copied_tokens,
        "copied_token_ratio": round(copied_ratio, 4),
        "copied_segment_count": segment_metrics["copied_segment_count"],
        "copied_body_segment_count": segment_metrics["copied_body_segment_count"],
        "copied_body_segment_area_ratio": segment_metrics["copied_body_segment_area_ratio"],
        "page_crop_layout": page_crop_layout,
        "severe_reason": severe_reason,
        "catastrophic_reason": catastrophic_reason,
        "moderate_reason": moderate_reason,
        "examples": scan["examples"],
        "thresholds": {
            "scan_min_words": PAPER_BODY_SCAN_MIN_WORDS,
            "severe_min_words": PAPER_BODY_SEVERE_MIN_WORDS,
            "severe_min_text_coverage": PAPER_BODY_SEVERE_MIN_TEXT_COVERAGE,
            "severe_min_hit_ratio": PAPER_BODY_SEVERE_MIN_HIT_RATIO,
            "severe_min_copied_tokens": PAPER_BODY_SEVERE_MIN_COPIED_TOKENS,
            "severe_min_copied_ratio": PAPER_BODY_SEVERE_MIN_COPIED_RATIO,
            "dense_crop_min_words": PAPER_BODY_DENSE_CROP_MIN_WORDS,
            "dense_crop_min_text_coverage": PAPER_BODY_DENSE_CROP_MIN_TEXT_COVERAGE,
            "dense_crop_min_hit_ratio": PAPER_BODY_DENSE_CROP_MIN_HIT_RATIO,
            "dense_crop_min_copied_tokens": PAPER_BODY_DENSE_CROP_MIN_COPIED_TOKENS,
            "dense_crop_min_body_segments": PAPER_BODY_DENSE_CROP_MIN_BODY_SEGMENTS,
            "dense_crop_min_body_area": PAPER_BODY_DENSE_CROP_MIN_BODY_AREA,
            "page_crop_min_words": PAPER_BODY_PAGE_CROP_MIN_WORDS,
            "page_crop_moderate_min_words": PAPER_BODY_PAGE_CROP_MODERATE_MIN_WORDS,
            "page_crop_min_text_coverage": PAPER_BODY_PAGE_CROP_MIN_TEXT_COVERAGE,
            "page_crop_min_hit_ratio": PAPER_BODY_PAGE_CROP_MIN_HIT_RATIO,
            "page_crop_min_copied_tokens": PAPER_BODY_PAGE_CROP_MIN_COPIED_TOKENS,
            "page_crop_min_copied_body_segments": PAPER_BODY_PAGE_CROP_MIN_COPIED_BODY_SEGMENTS,
            "page_crop_min_copied_body_area": PAPER_BODY_PAGE_CROP_MIN_COPIED_BODY_AREA,
            "page_crop_min_body_segments": PAPER_BODY_PAGE_CROP_MIN_BODY_SEGMENTS,
            "page_crop_max_median_height_ref_px": PAPER_BODY_PAGE_CROP_MAX_MEDIAN_HEIGHT_REF_PX,
            "distributed_crop_min_copied_tokens": PAPER_BODY_DISTRIBUTED_CROP_MIN_COPIED_TOKENS,
            "distributed_crop_min_copied_body_segments": PAPER_BODY_DISTRIBUTED_CROP_MIN_COPIED_BODY_SEGMENTS,
            "distributed_crop_min_copied_body_area": PAPER_BODY_DISTRIBUTED_CROP_MIN_COPIED_BODY_AREA,
            "distributed_crop_min_body_segments": PAPER_BODY_DISTRIBUTED_CROP_MIN_BODY_SEGMENTS,
            "distributed_crop_min_microtext_ratio": PAPER_BODY_DISTRIBUTED_CROP_MIN_MICROTEXT_RATIO,
            "distributed_crop_min_page_body_area": PAPER_BODY_DISTRIBUTED_CROP_MIN_PAGE_BODY_AREA,
            "moderate_min_words": PAPER_BODY_MODERATE_MIN_WORDS,
            "moderate_min_text_coverage": PAPER_BODY_MODERATE_MIN_TEXT_COVERAGE,
            "moderate_min_hit_ratio": PAPER_BODY_MODERATE_MIN_HIT_RATIO,
            "moderate_min_copied_tokens": PAPER_BODY_MODERATE_MIN_COPIED_TOKENS,
            "moderate_min_copied_ratio": PAPER_BODY_MODERATE_MIN_COPIED_RATIO,
            "regional_crop_min_words": PAPER_BODY_REGIONAL_CROP_MIN_WORDS,
            "regional_crop_min_hit_ratio": PAPER_BODY_REGIONAL_CROP_MIN_HIT_RATIO,
            "regional_crop_min_copied_tokens": PAPER_BODY_REGIONAL_CROP_MIN_COPIED_TOKENS,
            "regional_crop_min_copied_ratio": PAPER_BODY_REGIONAL_CROP_MIN_COPIED_RATIO,
            "regional_crop_min_body_segments": PAPER_BODY_REGIONAL_CROP_MIN_BODY_SEGMENTS,
            "regional_crop_min_body_area": PAPER_BODY_REGIONAL_CROP_MIN_BODY_AREA,
            "regional_crop_max_median_height_ref_px": PAPER_BODY_REGIONAL_CROP_MAX_MEDIAN_HEIGHT_REF_PX,
            "sparse_crop_min_words": PAPER_BODY_SPARSE_CROP_MIN_WORDS,
            "sparse_crop_max_words": PAPER_BODY_SPARSE_CROP_MAX_WORDS,
            "sparse_crop_max_text_coverage": PAPER_BODY_SPARSE_CROP_MAX_TEXT_COVERAGE,
            "catastrophic_min_copied_body_area": PAPER_BODY_CATASTROPHIC_MIN_COPIED_BODY_AREA,
            "catastrophic_min_copied_ratio": PAPER_BODY_CATASTROPHIC_MIN_COPIED_RATIO,
            "catastrophic_min_page_body_area": PAPER_BODY_CATASTROPHIC_MIN_PAGE_BODY_AREA,
        },
        "note": "Flags large verbatim paper-body prose in the rendered poster; source-grounded synthesized summaries are allowed.",
    }
    findings: list[EvaluationFinding] = []
    if catastrophic:
        findings.append(EvaluationFinding(
            "paper-body-screenshot-catastrophic",
            "P0",
            "Paper-body screenshots dominate the poster canvas; the artifact is not functioning as a synthesized poster.",
            "paper_body_screenshot",
            "text_density",
            {
                "gate_ceiling": 0.0,
                "ocr_word_count": int(word_count),
                "text_coverage_ratio": round(text_coverage, 4),
                "exact_ngram_hit_ratio": round(hit_ratio, 4),
                "copied_token_count": copied_tokens,
                "copied_token_ratio": round(copied_ratio, 4),
                "copied_body_segment_area_ratio": segment_metrics["copied_body_segment_area_ratio"],
                "catastrophic_reason": catastrophic_reason,
                "page_crop_layout": page_crop_layout,
                "examples": scan["examples"][:3],
            },
        ))
    elif severe:
        findings.append(EvaluationFinding(
            "paper-body-screenshot-severe",
            "P0",
            "Large paper-like OCR text regions overlap the source paper; likely pasted paper-body screenshots rather than synthesized poster content.",
            "paper_body_screenshot",
            "text_density",
            {
                "gate_ceiling": 30.0,
                "ocr_word_count": int(word_count),
                "text_coverage_ratio": round(text_coverage, 4),
                "exact_ngram_hit_ratio": round(hit_ratio, 4),
                "copied_token_count": copied_tokens,
                "copied_token_ratio": round(copied_ratio, 4),
                "severe_reason": metrics["severe_reason"],
                "page_crop_layout": page_crop_layout,
                "examples": scan["examples"][:3],
            },
        ))
    elif moderate:
        findings.append(EvaluationFinding(
            "paper-body-screenshot-copying",
            "P1",
            "Substantial source-paper prose or page-like microtext appears in the poster; verify it is not a paper-body crop standing in for synthesized content.",
            "paper_body_screenshot",
            "text_density",
            {
                "ocr_word_count": int(word_count),
                "text_coverage_ratio": round(text_coverage, 4),
                "exact_ngram_hit_ratio": round(hit_ratio, 4),
                "copied_token_count": copied_tokens,
                "copied_token_ratio": round(copied_ratio, 4),
                "moderate_reason": metrics["moderate_reason"],
                "page_crop_layout": page_crop_layout,
                "examples": scan["examples"][:3],
            },
        ))
    return MetricBundle("paper_body_screenshot", metrics, _status_from_findings(findings), findings_count=len(findings)), findings


def _normalized_visual_rect(raw: dict[str, Any]) -> dict[str, float] | None:
    try:
        x0 = float(raw.get("x0"))
        y0 = float(raw.get("y0"))
        x1 = float(raw.get("x1"))
        y1 = float(raw.get("y1"))
    except (TypeError, ValueError):
        return None
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    w = x1 - x0
    h = y1 - y0
    if w <= 0 or h <= 0:
        return None
    rect = {
        "x0": x0,
        "y0": y0,
        "x1": x1,
        "y1": y1,
        "w": w,
        "h": h,
        "area_ratio": _float_metric(raw.get("area_ratio"), default=0.0),
    }
    canvas_width = _float_metric(raw.get("canvas_width"), default=0.0)
    canvas_height = _float_metric(raw.get("canvas_height"), default=0.0)
    if canvas_width > 0 and canvas_height > 0:
        rect["canvas_width"] = canvas_width
        rect["canvas_height"] = canvas_height
    return rect


def _group_visual_evidence_rects(rects: list[dict[str, float]]) -> list[dict[str, float]]:
    """Merge CV subfigure boxes into conservative visual-evidence groups."""
    if not rects:
        return []
    canvas_area, canvas_short_edge = _infer_visual_canvas(rects)
    shorts = sorted(min(r["w"], r["h"]) for r in rects)
    gap = VISUAL_EVIDENCE_GROUP_GAP_FRAC * shorts[len(shorts) // 2]
    if canvas_short_edge > 0:
        gap = min(gap, VISUAL_EVIDENCE_GROUP_MAX_CANVAS_GAP_FRAC * canvas_short_edge)
    gap = max(1.0, gap)
    n = len(rects)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri = find(i)
        rj = find(j)
        if ri != rj:
            parent[ri] = rj

    for i in range(n):
        for j in range(i + 1, n):
            if _visual_rects_belong_to_same_group(rects[i], rects[j], gap):
                union(i, j)

    clusters: dict[int, list[dict[str, float]]] = {}
    for i, rect in enumerate(rects):
        clusters.setdefault(find(i), []).append(rect)

    groups: list[dict[str, float]] = []
    for members in clusters.values():
        x0 = min(r["x0"] for r in members)
        y0 = min(r["y0"] for r in members)
        x1 = max(r["x1"] for r in members)
        y1 = max(r["y1"] for r in members)
        w = x1 - x0
        h = y1 - y0
        area_ratio = (w * h / canvas_area) if canvas_area > 0 else sum(r.get("area_ratio", 0.0) for r in members)
        short_edge_ratio = min(w, h) / canvas_short_edge if canvas_short_edge > 0 else 0.0
        groups.append({
            "x0": round(x0, 2),
            "y0": round(y0, 2),
            "x1": round(x1, 2),
            "y1": round(y1, 2),
            "w": round(w, 2),
            "h": round(h, 2),
            "area_ratio": round(area_ratio, 4),
            "short_edge_ratio": round(short_edge_ratio, 4),
            "raw_region_count": len(members),
        })
    groups.sort(key=lambda r: (r["y0"], r["x0"]))
    return groups


def _infer_visual_canvas(rects: list[dict[str, float]]) -> tuple[float, float]:
    widths = [r["canvas_width"] for r in rects if r.get("canvas_width", 0.0) > 0 and r.get("canvas_height", 0.0) > 0]
    heights = [r["canvas_height"] for r in rects if r.get("canvas_width", 0.0) > 0 and r.get("canvas_height", 0.0) > 0]
    if widths and heights:
        width = _median_float(widths)
        height = _median_float(heights)
        return max(1.0, width * height), min(width, height)

    inferred_areas = [
        (r["w"] * r["h"]) / r["area_ratio"]
        for r in rects
        if r.get("area_ratio", 0.0) > 0
    ]
    if inferred_areas:
        area = max(1.0, _median_float(inferred_areas))
        return area, area ** 0.5

    max_x = max(r["x1"] for r in rects)
    max_y = max(r["y1"] for r in rects)
    return max(1.0, max_x * max_y), min(max_x, max_y)


def _visual_rects_belong_to_same_group(a: dict[str, float], b: dict[str, float], gap: float) -> bool:
    overlap_w = min(a["x1"], b["x1"]) - max(a["x0"], b["x0"])
    overlap_h = min(a["y1"], b["y1"]) - max(a["y0"], b["y0"])
    if overlap_w > 0 and overlap_h > 0:
        return True

    gap_x = max(0.0, max(a["x0"], b["x0"]) - min(a["x1"], b["x1"]))
    gap_y = max(0.0, max(a["y0"], b["y0"]) - min(a["y1"], b["y1"]))
    y_overlap = _axis_overlap_fraction(a["y0"], a["y1"], b["y0"], b["y1"])
    x_overlap = _axis_overlap_fraction(a["x0"], a["x1"], b["x0"], b["x1"])
    if gap_x <= gap and y_overlap >= VISUAL_EVIDENCE_GROUP_AXIS_OVERLAP_FRAC:
        return True
    if gap_y <= gap and x_overlap >= VISUAL_EVIDENCE_GROUP_AXIS_OVERLAP_FRAC:
        return True
    return False


def _axis_overlap_fraction(a0: float, a1: float, b0: float, b1: float) -> float:
    overlap = min(a1, b1) - max(a0, b0)
    if overlap <= 0:
        return 0.0
    return overlap / max(1.0, min(a1 - a0, b1 - b0))


# A cluster of >= this many tightly-packed figures that FILL >= this fraction of their
# bounding box is a figure collage/cramming — several figures squished into one block
# with no breathing room, which real conference posters avoid.
_CRAMMING_MIN_FIGURES = 3
_CRAMMING_MIN_FILL = 0.55
_CRAMMING_GAP_FRAC = 0.35   # two figures are "adjacent" if their bbox gap < this x median short-side


def _detect_figure_cramming(rects: list[dict[str, Any]]) -> dict[str, Any]:
    """Cluster figures by proximity; flag a tightly-packed cluster of >=3 figures that
    fills most of its bounding box (a collage). Pure geometry over figure_region_rects."""
    figs = [r for r in rects if r.get("w") and r.get("h")]
    n = len(figs)
    none = {"crammed": False, "cluster_size": 0, "fill_ratio": 0.0}
    if n < _CRAMMING_MIN_FIGURES:
        return none
    shorts = sorted(min(float(r["w"]), float(r["h"])) for r in figs)
    tol = _CRAMMING_GAP_FRAC * shorts[len(shorts) // 2]  # gap tolerance ~ median short side
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def adjacent(a: dict, b: dict) -> bool:
        gx = max(0.0, max(float(a["x0"]), float(b["x0"])) - min(float(a["x1"]), float(b["x1"])))
        gy = max(0.0, max(float(a["y0"]), float(b["y0"])) - min(float(a["y1"]), float(b["y1"])))
        return gx <= tol and gy <= tol

    for i in range(n):
        for j in range(i + 1, n):
            if adjacent(figs[i], figs[j]):
                parent[find(i)] = find(j)
    clusters: dict[int, list[dict]] = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(figs[i])

    best = dict(none)
    for cl in clusters.values():
        if len(cl) < _CRAMMING_MIN_FIGURES:
            continue
        x0 = min(float(r["x0"]) for r in cl); y0 = min(float(r["y0"]) for r in cl)
        x1 = max(float(r["x1"]) for r in cl); y1 = max(float(r["y1"]) for r in cl)
        bbox = max(1.0, (x1 - x0) * (y1 - y0))
        fill = sum(float(r["w"]) * float(r["h"]) for r in cl) / bbox
        if fill >= _CRAMMING_MIN_FILL and len(cl) >= best["cluster_size"]:
            best = {"crammed": True, "cluster_size": len(cl), "fill_ratio": round(fill, 3)}
    return best


def _copy_words(text: str) -> list[str]:
    return _COPY_WORD_RE.findall((text or "").lower())


def _paper_ngram_set(tokens: list[str], *, n: int) -> set[str]:
    out: set[str] = set()
    if len(tokens) < n:
        return out
    for i in range(len(tokens) - n + 1):
        gram = " ".join(tokens[i:i + n])
        if len(gram) >= PAPER_BODY_COPY_MIN_GRAM_CHARS:
            out.add(gram)
    return out


def _copied_ngram_scan(poster_tokens: list[str], paper_tokens: list[str], *, n: int) -> dict[str, Any]:
    paper_grams = _paper_ngram_set(paper_tokens, n=n)
    covered = [False] * len(poster_tokens)
    hit_count = 0
    gram_count = 0
    examples: list[str] = []
    if len(poster_tokens) >= n and paper_grams:
        for i in range(len(poster_tokens) - n + 1):
            gram = " ".join(poster_tokens[i:i + n])
            if len(gram) < PAPER_BODY_COPY_MIN_GRAM_CHARS:
                continue
            gram_count += 1
            if gram in paper_grams:
                hit_count += 1
                if len(examples) < 5:
                    examples.append(gram)
                for j in range(i, i + n):
                    covered[j] = True
    copied = sum(1 for flag in covered if flag)
    return {
        "ngram_count": gram_count,
        "hit_count": hit_count,
        "hit_ratio": hit_count / max(1, gram_count),
        "copied_token_count": copied,
        "copied_token_ratio": copied / max(1, len(poster_tokens)),
        "examples": examples,
    }


def _copied_segment_metrics(
    segments: list[dict[str, Any]],
    paper_tokens: list[str],
    *,
    n: int,
    image_size: Any = None,
) -> dict[str, Any]:
    paper_grams = _paper_ngram_set(paper_tokens, n=n)
    copied_count = 0
    copied_body_count = 0
    copied_body_area = 0.0
    total_area = _segments_canvas_area(segments)
    try:
        image_width = float(image_size[0])
        image_height = float(image_size[1])
    except (TypeError, ValueError, IndexError):
        image_width = image_height = 0.0
    if image_width > 0 and image_height > 0:
        total_area = image_width * image_height
    for seg in segments or []:
        text = str(seg.get("text") or "")
        tokens = _copy_words(text)
        if len(tokens) < n:
            continue
        matched = any(
            " ".join(tokens[i:i + n]) in paper_grams
            for i in range(len(tokens) - n + 1)
            if len(" ".join(tokens[i:i + n])) >= PAPER_BODY_COPY_MIN_GRAM_CHARS
        )
        if not matched:
            continue
        copied_count += 1
        if len(tokens) >= 6:
            copied_body_count += 1
            copied_body_area += _segment_area(seg.get("box"))
    return {
        "copied_segment_count": copied_count,
        "copied_body_segment_count": copied_body_count,
        "copied_body_segment_area_ratio": round(copied_body_area / max(1.0, total_area), 4),
    }


def _page_crop_layout_metrics(ocr: dict[str, Any]) -> dict[str, Any]:
    """OCR geometry signal for a pasted PDF page/body crop.

    Direct paper-page crops tend to create many tiny, regular OCR body lines. Normal
    authored posters can be dense, but at the same 2048px reference long edge their
    body text is usually larger. This is deliberately only a companion signal; the
    caller also requires source-paper n-gram overlap before flagging.
    """
    segments = ocr.get("segments") if isinstance(ocr.get("segments"), list) else []
    image_size = ocr.get("image_size") if isinstance(ocr.get("image_size"), list) else None
    try:
        width = float(image_size[0])
        height = float(image_size[1])
    except (TypeError, ValueError, IndexError):
        width = height = 0.0
    long_edge = max(width, height, 1.0)
    scale = 2048.0 / long_edge
    body_heights: list[float] = []
    body_area = 0.0
    microtext_count = 0
    for seg in segments:
        tokens = _copy_words(str(seg.get("text") or ""))
        if len(tokens) < 3:
            continue
        box = seg.get("box")
        height_ref = _segment_height(box) * scale
        if height_ref <= 0:
            continue
        body_heights.append(height_ref)
        body_area += _segment_area(box)
        if height_ref <= PAPER_BODY_PAGE_CROP_MAX_MEDIAN_HEIGHT_REF_PX:
            microtext_count += 1
    body_count = len(body_heights)
    median_height = _median_float(body_heights)
    canvas_area = width * height if width > 0 and height > 0 else _segments_canvas_area(segments)
    return {
        "body_segment_count": body_count,
        "median_body_segment_height_ref_px": round(median_height, 2),
        "microtext_segment_count": microtext_count,
        "microtext_segment_ratio": round(microtext_count / max(1, body_count), 4),
        "body_segment_area_ratio": round(body_area / max(1.0, canvas_area), 4),
    }


def _segment_area(box: Any) -> float:
    try:
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
    except (TypeError, ValueError, IndexError):
        return 0.0
    return max(0.0, max(xs) - min(xs)) * max(0.0, max(ys) - min(ys))


def _segments_canvas_area(segments: list[dict[str, Any]]) -> float:
    x1 = y1 = 0.0
    for seg in segments or []:
        box = seg.get("box")
        try:
            x1 = max(x1, max(float(p[0]) for p in box))
            y1 = max(y1, max(float(p[1]) for p in box))
        except (TypeError, ValueError, IndexError):
            continue
    return max(1.0, x1 * y1)


def _float_metric(value: Any, *, default: float) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _median_float(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def paper_integrity_metrics(paper: Path | None) -> tuple[MetricBundle, list[EvaluationFinding]]:
    metrics = {
        "provided": paper is not None,
        "path": str(paper) if paper else None,
        "exists": paper.exists() if paper else None,
        "size_bytes": paper.stat().st_size if paper and paper.exists() and paper.is_file() else None,
    }
    findings: list[EvaluationFinding] = []
    if paper is not None and not paper.exists():
        findings.append(EvaluationFinding(
            "paper-missing",
            "P1",
            "Source paper path was provided but does not exist.",
            "paper_integrity",
            "source_grounding",
            {"paper_path": str(paper)},
        ))
    elif paper is not None and metrics["size_bytes"] == 0:
        findings.append(EvaluationFinding(
            "paper-empty",
            "P1",
            "Source paper file is empty.",
            "paper_integrity",
            "source_grounding",
            {"paper_path": str(paper)},
        ))
    return MetricBundle("paper_integrity", metrics, "ok" if not findings else "error", findings_count=len(findings)), findings


def html_quality_metrics(snapshot: ArtifactSnapshot) -> tuple[MetricBundle, list[EvaluationFinding]]:
    if not snapshot.html:
        return MetricBundle("html_quality", {"available": False}, "skipped"), []
    lint_findings = lint_html_quality(snapshot.html)
    findings = [
        EvaluationFinding(
            id=str(item.get("id") or "html-quality"),
            severity=_map_lint_severity(str(item.get("severity") or "P2")),
            message=str(item.get("message") or item.get("summary") or "HTML quality lint finding."),
            metric="html_quality",
            category="aesthetic_lint",
            details=dict(item),
        )
        for item in lint_findings
    ]
    metrics = {
        "available": True,
        "finding_count": len(lint_findings),
        "p0_count": sum(1 for item in lint_findings if item.get("severity") == "P0"),
        "p1_count": sum(1 for item in lint_findings if item.get("severity") == "P1"),
        "p2_count": sum(1 for item in lint_findings if item.get("severity") == "P2"),
    }
    return MetricBundle("html_quality", metrics, _status_from_findings(findings), findings_count=len(findings)), findings


def html_structure_metrics(snapshot: ArtifactSnapshot) -> tuple[MetricBundle, list[EvaluationFinding]]:
    if not snapshot.html:
        return MetricBundle("html_structure", {"available": False}, "skipped"), []
    text_layers = [
        layer for layer in snapshot.dom_layers
        if _word_count(str(layer.get("text") or "")) > 0
    ]
    image_layers = [
        layer for layer in snapshot.dom_layers
        if str(layer.get("kind") or "").lower() in {"img", "image"}
        or bool(layer.get("src"))
    ]
    missing_local_images = [
        layer for layer in image_layers
        if _is_missing_local_image(layer, snapshot)
    ]
    visible_words = sum(_word_count(str(layer.get("text") or "")) for layer in text_layers)
    if visible_words == 0:
        visible_words = _word_count(snapshot.text or "")
    metrics = {
        "available": True,
        "dom_layer_count": len(snapshot.dom_layers),
        "text_layer_count": len(text_layers),
        "image_layer_count": len(image_layers),
        "visible_text_word_count": visible_words,
        "missing_local_image_count": len(missing_local_images),
        "missing_local_image_samples": missing_local_images[:8],
    }
    findings: list[EvaluationFinding] = []
    if visible_words == 0:
        findings.append(EvaluationFinding("html-visible-text-empty", "P1", "HTML artifact has no extracted visible text.", "html_structure", "text_density", metrics))
    if missing_local_images:
        findings.append(EvaluationFinding("html-local-image-missing", "P0", "HTML references local images that do not exist.", "html_structure", "render_integrity", metrics))
    return MetricBundle("html_structure", metrics, _status_from_findings(findings), findings_count=len(findings)), findings


def numeric_token_metrics(
    snapshot: ArtifactSnapshot,
    paper: Path | None,
    *,
    segments: list[dict[str, Any]] | None = None,
    visual_rects: list[dict[str, Any]] | None = None,
) -> tuple[MetricBundle, list[EvaluationFinding]]:
    """Deterministic numeric grounding: are the poster's OWN asserted numbers
    traceable to the paper, or fabricated?

    Scope (v2): in image/OCR mode (``segments`` given) only the poster's
    AUTHORED-text numbers are checked. EXCLUDED, because they are not the poster's
    own claims: numbers rendered inside a detected figure/chart/table-screenshot
    region (``visual_rects``); bibliographic metadata — any citation segment (DOI,
    "et al.", arXiv id, proceedings/volume), whether in the title block or a footer
    reference line; and bare chart axis/data labels. Numbers are canonicalized
    (units, magnitudes, %, ×, thousands, scientific), OCR-merge garbage is dropped,
    and only *salient* numbers are scored. Each is matched to the paper with tolerance
    and a kind guard: grounded / near-miss (OCR) / fabricated. The reference must be
    the FULL source paper; without a paper this dimension is N/A.
    """
    figure_excluded = 0
    if segments is not None:
        artifact_text, figure_excluded = _authored_numeric_text(segments, visual_rects or [])
    else:
        artifact_text = snapshot.text or ""
    artifact_tokens, garbage = _parse_numeric_tokens(artifact_text)
    if not artifact_tokens:
        return MetricBundle(
            "numeric_token_exact_match",
            {
                "available": False,
                "artifact_numeric_token_count": 0,
                "garbage_filtered_count": garbage,
                "figure_excluded_count": figure_excluded,
                "reason": "no_extracted_artifact_numeric_tokens",
            },
            "skipped",
        ), []
    if not paper:
        return MetricBundle(
            "numeric_token_exact_match",
            {"available": False, "artifact_numeric_token_count": len(artifact_tokens)},
            "skipped",
        ), []
    try:
        paper_text = _extract_paper_text(paper)
    except Exception as exc:
        finding = EvaluationFinding(
            "paper-text-unreadable",
            "P1",
            "Paper text could not be extracted for numeric grounding.",
            "numeric_token_exact_match",
            "source_grounding",
            {"paper_path": str(paper), "error": f"{type(exc).__name__}: {exc}"},
        )
        return MetricBundle(
            "numeric_token_exact_match",
            {"available": False, "artifact_numeric_token_count": len(artifact_tokens), "error": finding.details["error"]},
            "error",
            findings_count=1,
        ), [finding]

    paper_tokens, _paper_garbage = _parse_numeric_tokens(paper_text)
    paper_values = [tok["value"] for tok in paper_tokens]

    grounded = near_miss = fabricated = trivial_count = 0
    fabricated_examples: list[str] = []
    near_miss_examples: list[str] = []
    seen: set[tuple[float, str]] = set()
    for tok in artifact_tokens:
        if not _is_salient(tok):
            trivial_count += 1
            continue
        key = (round(tok["value"], 6), tok["kind"])
        if key in seen:
            continue
        seen.add(key)
        status = _match_number(tok, paper_tokens)
        if status == "grounded":
            grounded += 1
        elif status == "near_miss":
            near_miss += 1
            near_miss_examples.append(tok["raw"])
        else:
            fabricated += 1
            fabricated_examples.append(tok["raw"])

    salient_count = grounded + near_miss + fabricated
    paper_value_set = {round(v, 6) for v in paper_values}
    all_values = {round(tok["value"], 6) for tok in artifact_tokens}
    exact_match_ratio = round(sum(1 for v in all_values if v in paper_value_set) / max(1, len(all_values)), 4)
    salient_grounding_ratio = (
        round((grounded + 0.5 * near_miss) / salient_count, 4) if salient_count > 0 else exact_match_ratio
    )

    metrics = {
        "available": True,
        "paper_path": str(paper),
        "artifact_numeric_token_count": len(artifact_tokens),
        "paper_numeric_token_count": len(paper_tokens),
        "salient_token_count": salient_count,
        "salient_grounded": grounded,
        "salient_near_miss": near_miss,
        "salient_fabricated": fabricated,
        "salient_grounding_ratio": salient_grounding_ratio,
        "trivial_token_count": trivial_count,
        "garbage_filtered_count": garbage,
        "figure_excluded_count": figure_excluded,
        "exact_match_ratio": exact_match_ratio,
        "fabricated_examples": fabricated_examples[:15],
        "near_miss_examples": near_miss_examples[:15],
        "missing_examples": fabricated_examples[:25],  # back-compat key
    }

    findings: list[EvaluationFinding] = []
    if fabricated > 0:
        findings.append(
            EvaluationFinding(
                "numeric-token-mismatch",
                "P1",
                "Salient poster number(s) were not found in the paper — possible fabrication.",
                "numeric_token_exact_match",
                "source_grounding",
                {
                    "fabricated_examples": fabricated_examples[:15],
                    "near_miss_examples": near_miss_examples[:15],
                    "salient_grounding_ratio": salient_grounding_ratio,
                },
            )
        )
    elif near_miss > 0:
        findings.append(
            EvaluationFinding(
                "numeric-token-mismatch",
                "P2",
                "Some poster numbers are near-misses to the paper (likely OCR), not exact.",
                "numeric_token_exact_match",
                "source_grounding",
                {"near_miss_examples": near_miss_examples[:15], "salient_grounding_ratio": salient_grounding_ratio},
            )
        )
    return MetricBundle("numeric_token_exact_match", metrics, _status_from_findings(findings), findings_count=len(findings)), findings


# A bare number rendered this many times taller than the poster's median text line
# is a prominent headline stat callout (the poster's own claim), not a chart label.
_PROMINENT_HEIGHT_MULT = 2.2


# Citation/reference markers — a segment carrying any of these is bibliographic
# metadata (a venue line, DOI, or "et al." citation), not the poster's own claim.
# These never appear in a real results assertion, so matching is low-risk.
# Note: "pages"/"pp." are safe; "volume" is NOT (it appears in real claims like
# "tumor volume", "data volume"), so the journal-volume case relies on "vol."/"pages".
_CITATION_MARKERS = ("doi", "et al", "arxiv", "proceedings", "vol.", "pages ", "pp.", "isbn", "issn")


def _authored_numeric_text(
    segments: list[dict[str, Any]], visual_rects: list[dict[str, Any]]
) -> tuple[str, int]:
    """Join the text of OCR segments that carry the poster's AUTHORED prose numbers,
    excluding (and counting) numbers that are not the poster's own claims (see
    `_numbers_are_figure_reproductions`)."""
    median_h = _median_segment_height(segments)
    kept: list[str] = []
    excluded = 0
    for seg in segments or []:
        text = str(seg.get("text") or "")
        toks, _g = _parse_numeric_tokens(text)
        if not toks:
            kept.append(text)
            continue
        is_repro, _reason = _numbers_are_figure_reproductions(
            text, seg.get("box"), visual_rects, median_h)
        if is_repro:
            excluded += len(toks)
            continue
        kept.append(text)
    return " ".join(kept), excluded


def _numbers_are_figure_reproductions(
    text: str, box: Any, visual_rects: list[dict[str, Any]], median_height: float
) -> tuple[bool, str]:
    """Whether a segment's numbers should be excluded from authored-claim grounding
    (they are not the poster's own assertions):

    - **citation metadata**: the segment carries a citation marker (DOI, "et al.",
      arXiv id, proceedings/volume) — a bibliographic reference line in the title
      block OR a footer, e.g. "Jones et al., Nature 451, 990-993 (2008), doi:…".
      (Excluded by nature, not by position, so a fabricated hero stat that happens to
      sit high in the poster stays checkable.)
    - **figure region**: inside a detected figure/table screenshot.
    - **bare label**: a small "bare" numeric segment — just a number + unit, no words
      (a chart axis/data label that sparse-chart region detection misses). A real
      claim sits in a phrase ("reaches 71.2% accuracy"); a chart label ("190K") does
      not. A *prominently large* bare number is kept — a headline stat callout (e.g. a
      fabricated "99.9%" in 96pt) is the poster's own claim and must stay checkable.
    """
    if _is_citation_segment(text):
        return True, "citation"
    if box and _segment_in_any_rect(box, visual_rects):
        return True, "figure"
    if _is_bare_numeric_segment(text):
        height = _segment_height(box) if box else 0.0
        prominent = median_height > 0 and height >= median_height * _PROMINENT_HEIGHT_MULT
        if not prominent:
            return True, "bare-label"
    return False, ""


def _is_citation_segment(text: str) -> bool:
    """True if the segment is a bibliographic/citation line (DOI, et al., venue)."""
    low = text.lower()
    return any(marker in low for marker in _CITATION_MARKERS)


def _is_bare_numeric_segment(text: str) -> bool:
    """A segment that is just a number (+ short unit), no real words — i.e. a chart
    axis/data label or a lone table cell, not an authored sentence claim."""
    return sum(1 for c in text if c.isalpha()) < 3


def _segment_height(box: Any) -> float:
    try:
        ys = [float(p[1]) for p in box]
    except (TypeError, ValueError, IndexError):
        return 0.0
    return max(ys) - min(ys)


def _median_segment_height(segments: list[dict[str, Any]] | None) -> float:
    heights = sorted(_segment_height(s.get("box")) for s in (segments or []) if s.get("box"))
    n = len(heights)
    if n == 0:
        return 0.0
    return heights[n // 2] if n % 2 else (heights[n // 2 - 1] + heights[n // 2]) / 2.0


def _segment_in_any_rect(box: Any, rects: list[dict[str, Any]], *, frac: float = 0.5) -> bool:
    """True if >= frac of the OCR segment box area overlaps any figure/visual rect."""
    try:
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
    except (TypeError, ValueError, IndexError):
        return False
    sx0, sy0, sx1, sy1 = min(xs), min(ys), max(xs), max(ys)
    seg_area = max(1e-6, (sx1 - sx0) * (sy1 - sy0))
    for r in rects:
        ix0 = max(sx0, float(r.get("x0", 0)))
        iy0 = max(sy0, float(r.get("y0", 0)))
        ix1 = min(sx1, float(r.get("x1", 0)))
        iy1 = min(sy1, float(r.get("y1", 0)))
        if ix1 > ix0 and iy1 > iy0 and (ix1 - ix0) * (iy1 - iy0) / seg_area >= frac:
            return True
    return False


def _parse_numeric_tokens(text: str) -> tuple[list[dict[str, Any]], int]:
    """Parse numbers into {raw, value, kind}; drop OCR-merge garbage (and count it)."""
    text = text or ""
    tokens: list[dict[str, Any]] = []
    garbage = 0
    for match in NUMERIC_TOKEN_RE.finditer(text):
        # Digits that are part of an alphanumeric identifier (a model/dataset/sample
        # name like "ViT-L/16", "ImageNet-21k", "TCGA-99-7458") are proper-noun
        # components, not measured quantities — drop them.
        if _is_identifier_component(text, match.start()):
            garbage += 1
            continue
        # A number immediately followed by ".<digit>" is a malformed multi-decimal
        # OCR merge (e.g. a "(1.34-2.34)" CI read as "1.342.34" -> "1.342"). Drop it.
        end = match.end()
        if end + 1 < len(text) and text[end] == "." and text[end + 1].isdigit():
            garbage += 1
            continue
        parsed = _canonical_value(match.group(1), match.group(2) or "", match.group(0))
        if parsed is None:
            garbage += 1
        else:
            tokens.append(parsed)
    return tokens, garbage


def _is_identifier_component(text: str, start: int) -> bool:
    """True if the number at `start` is glued into an alphanumeric identifier token —
    a model/dataset/sample name like "ViT-L/16121k", "ImageNet-21k", "TCGA-99-7458",
    where the digits are part of a proper noun, not a measured quantity. Such a token
    is a single whitespace-delimited word that starts with a letter and glues the
    number on with an identifier separator (/, -, _). Words that start with a digit
    ("4.5M", "5e-4", "60.3%", "46/284") are NOT identifiers and stay checkable.
    """
    lo = start
    while lo > 0 and not text[lo - 1].isspace():
        lo -= 1
    hi = start
    while hi < len(text) and not text[hi].isspace():
        hi += 1
    word = text[lo:hi]
    if not word or not (word[0].isascii() and word[0].isalpha()):
        return False
    return any(sep in word for sep in ("/", "-", "_"))


def _canonical_value(core: str, suffix: str, raw: str) -> dict[str, Any] | None:
    """Canonicalize a number + attached suffix; return None for OCR-merge garbage."""
    try:
        value = float(core.replace(",", ""))
    except ValueError:
        return None
    suffix_l = suffix.strip().lower()
    kind = "plain"
    if not suffix_l:
        pass
    elif suffix_l[0] == "%":
        kind = "pct"
    elif suffix_l[0] in ("x", "×"):
        kind = "mult"  # keep the number even if OCR merged a word ("994.7xfas" -> 994.7×)
    elif suffix_l in _MAGNITUDE_SUFFIX:
        value *= _MAGNITUDE_SUFFIX[suffix_l]
        kind = "magnitude"
    elif suffix_l in _UNIT_ALLOW:
        kind = "plain"
    elif len(suffix_l) >= 2:
        return None  # a digit glued to >=2 non-unit letters (e.g. 1LoRA, 0oo, 2free)
    return {"raw": raw.strip(), "value": value, "kind": kind}


def _is_salient(token: dict[str, Any]) -> bool:
    """Does the number carry a scientific claim (vs a trivial/structural integer)?"""
    if token["kind"] in ("pct", "mult", "magnitude"):
        return True
    value = abs(token["value"])
    if 1900 <= value <= 2099 and value == int(value):
        return False  # year-like
    if value != int(value):
        return True  # any decimal is a measured value
    return value >= SALIENCE_MIN_INT


def _match_number(token: dict[str, Any], paper_tokens: list[dict[str, Any]]) -> str:
    """Classify an artifact number against the paper: grounded / near_miss / ungrounded.

    Distinctive kinds (a percentage or a ×-multiplier) only ground/near-match another
    of the same kind — so a fabricated "99.9%" can't be absorbed by a coincidental
    plain "100" elsewhere in the paper. An exact-value match still grounds across
    kinds (e.g. the paper wrote "71.2" where the poster shows "71.2%").
    """
    if not paper_tokens:
        return "ungrounded"
    value, kind = token["value"], token["kind"]
    best = min(paper_tokens, key=lambda p: abs(p["value"] - value))
    diff = abs(best["value"] - value)
    if diff < 1e-6:
        return "grounded"  # identical value, format/OCR variant of the same number
    rel = diff / max(abs(value), 1e-9)
    kind_ok = _kind_compatible(kind, best["kind"])
    if rel <= GROUNDED_REL_TOL and kind_ok:
        return "grounded"
    if (rel <= NEARMISS_REL_TOL and kind_ok) or _one_digit_off(value, best["value"]):
        return "near_miss"
    return "ungrounded"


def _kind_compatible(a: str, b: str) -> bool:
    """pct and mult are distinctive claims; they must not near-match a plain number."""
    if a in ("pct", "mult") or b in ("pct", "mult"):
        return a == b
    return True


def _one_digit_off(a: float, b: float) -> bool:
    da, db = _sig_digits(a), _sig_digits(b)
    return len(da) == len(db) and sum(1 for x, y in zip(da, db) if x != y) == 1


def _sig_digits(value: float) -> str:
    return re.sub(r"[^0-9]", "", f"{abs(value):.6g}")


@lru_cache(maxsize=128)
def _extract_paper_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        with fitz.open(path) as doc:
            return "\n".join(page.get_text("text") for page in doc)
    return path.read_text(encoding="utf-8", errors="replace")


def _map_lint_severity(severity: str) -> str:
    if severity == "P0":
        return "P0"
    if severity == "P1":
        return "P1"
    return "P2"


def _status_from_findings(findings: list[EvaluationFinding]) -> str:
    if any(finding.severity == "P0" for finding in findings):
        return "error"
    if findings:
        return "warning"
    return "ok"


def _row_nonwhite_ratios(gray: Image.Image, width: int, height: int) -> list[float]:
    data = list(gray.getdata())
    rows: list[float] = []
    for y in range(height):
        start = y * width
        row = data[start:start + width]
        rows.append(sum(1 for value in row if value < 245) / max(1, width))
    return rows


def _longest_blank_run_ratio(rows: list[float], *, threshold: float) -> float:
    longest = 0
    current = 0
    for value in rows:
        if value <= threshold:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest / max(1, len(rows))


def _word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)?", text))


def _is_missing_local_image(layer: dict[str, Any], snapshot: ArtifactSnapshot) -> bool:
    src = str(layer.get("src") or "").strip()
    if not src or src.startswith(("#", "data:")):
        return False
    parsed = urlparse(src)
    if parsed.scheme in {"http", "https", "mailto", "tel"}:
        return False
    src_path = unquote(parsed.path or src)
    html_path = snapshot.resolved_files.get("html")
    if not html_path:
        return False
    candidate = Path(src_path)
    if not candidate.is_absolute():
        candidate = Path(html_path).parent / src_path.lstrip("/")
    return not candidate.exists()
