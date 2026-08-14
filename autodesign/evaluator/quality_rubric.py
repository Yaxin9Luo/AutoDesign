"""Deterministic pre-pass and the reproducible score aggregator.

``compute_deterministic_report`` runs the rule tools once and scores the
objective dimension components plus the hard-gate ceiling — this is code-owned and
fully reproducible. ``aggregate_final`` combines those numbers with per-dimension
judge scores into the final ``PosterQualityReport``. The arithmetic lives here so
the same inputs always produce the same overall.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..util.io import atomic_write_json
from .adapter import snapshot_artifact
from .metrics import (
    file_integrity_metrics,
    html_structure_metrics,
    image_density_metrics,
    numeric_token_metrics,
    paper_body_screenshot_metrics,
    visual_evidence_metrics,
)
from .ocr import run_ocr
from .spatial import basic_layout_integrity, blank_strips, content_occupancy
from .poster_rubric import (
    BODY_SCREENSHOT_CATASTROPHIC_GATE_CEILING_SCORE,
    BODY_SCREENSHOT_GATE_CEILING_SCORE,
    DENSITY_CONTENT_WEIGHT,
    DENSITY_TEXT_WEIGHT,
    DENSITY_VOID_PENALTY_K,
    DIMENSIONS,
    EMPTY_VISUAL_PLACEHOLDER_CEILING_SCORE,
    EMPTY_VISUAL_PLACEHOLDER_SEVERE_GATE_CEILING_SCORE,
    EMPTY_RECT_CAP,
    GATE_CEILING_SCORE,
    LAYOUT_VOID_CAP,
    LAYOUT_VOID_PENALTY_K,
    MULTI_PANEL_CROP_FAILURE_CEILING_SCORE,
    PASS_THRESHOLD,
    REF_CONTENT_COVERAGE,
    REF_TEXT_COVERAGE_RATIO,
    REVISE_THRESHOLD,
)
from .protocol import EVAL_PROTOCOL, EVALUATOR_FINGERPRINT
from .quality_schema import (
    QUALITY_SCHEMA_VERSION,
    PosterQualityFinding,
    PosterQualityReport,
    RubricDimensionScore,
)
from .tools import tool_render_audit


GOLD_SPEC_PATH = Path(__file__).with_name("assets") / "poster_gold_reference_specs.json"
MAJOR_VISUAL_FAILURE_CEILING_SCORE = 49.0
MULTI_JUDGE_VISUAL_FAILURE_CEILING_SCORE = 55.0
SERIOUS_VISUAL_DEFECT_PASS_CEILING_SCORE = 69.0
BASIC_LAYOUT_COLLAPSE_SCORE = 1.0
LAYOUT_COUPLED_SEVERE_CEILING_SCORE = 60.0
LAYOUT_COUPLED_WARNING_CEILING_SCORE = 68.0
_VISUAL_QUALITY_DIMENSIONS = {
    "visual_evidence_use",
    "layout_readability",
    "professional_aesthetics",
}
_OBJECTIVE_VISUAL_DIMENSIONS = {
    "information_density_and_synthesis",
    "basic_layout_integrity",
    "layout_readability",
}
_LAYOUT_BLOCKING_FINDING_IDS = {
    "basic-layout-canvas-overflow",
    "basic-layout-content-on-export-edge",
    "basic-layout-bottom-truncation",
    "basic-layout-section-bottom-truncated",
    "basic-layout-section-edge-tight",
    "basic-layout-panel-visual-overflow",
    "basic-layout-panel-text-overflow",
    "basic-layout-panel-content-tight",
    "basic-layout-section-content-overflow",
    "basic-layout-inter-section-collision",
    "basic-layout-heading-canvas-overflow",
}
_LAYOUT_VISUAL_CROP_FINDING_IDS = {
    "basic-layout-visual-crop-damage",
}
_LAYOUT_COUPLED_CAP_FINDING_ID = "layout-coupled-score-cap"
_ACADEMIC_AESTHETICS_CAP_FINDING_ID = "academic-poster-aesthetics-density-cap"
_POSTER_SCALE_LEGIBILITY_CAP_FINDING_ID = "poster-scale-legibility-cap"
_PRESENTATION_VIABILITY_CAP_FINDING_ID = "presentation-viability-score-cap"


# --- deterministic pre-pass --------------------------------------------------

def compute_deterministic_report(
    *,
    paper: Path | None,
    candidate_artifact: Path,
    out_dir: Path,
    profile: str | None = None,
    case_slug: str | None = None,
    gold_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    snap = snapshot_artifact(candidate_artifact, out_dir / "snapshot", artifact_type="poster")

    bundles: dict[str, dict[str, Any]] = {}
    findings: list[dict[str, Any]] = []
    for fn in (file_integrity_metrics, html_structure_metrics):
        bundle, bundle_findings = fn(snap)
        bundles[bundle.name] = bundle.metrics
        findings.extend(f.to_dict() for f in bundle_findings)
    img_bundle, img_findings = image_density_metrics(snap, artifact_type="poster")
    bundles[img_bundle.name] = img_bundle.metrics
    findings.extend(f.to_dict() for f in img_findings)

    # OCR bridge: recover text + resolution-invariant layout signals from the
    # rendered image. This is what makes the image mode (PNG/JPG/raster PDF) a
    # first-class input — density and numeric grounding stop depending on a DOM.
    ocr = run_ocr(snap.preview_image, include_segments=True) if snap.preview_image else {"available": False, "reason": "no_preview"}
    native_text = bool((snap.text or "").strip())
    if not native_text and ocr.get("available") and ocr.get("text"):
        # Image input had no text layer; score numeric grounding on OCR text.
        snap.text = str(ocr.get("text") or "")

    # Image-native content-occupancy grid: drives density + layout so colored/dark
    # backgrounds and large internal empty regions are handled correctly.
    occupancy: dict[str, Any] = {}
    strips: dict[str, Any] = {}
    layout_integrity: dict[str, Any] = {"available": False, "reason": "no_preview"}
    if snap.preview_image and Path(snap.preview_image).exists():
        from PIL import Image

        with Image.open(snap.preview_image) as preview_img:
            rgb = preview_img.convert("RGB")
            seg = ocr.get("segments") or []
            # Pass 1: preliminary occupancy feeds figure/panel detection.
            occupancy = content_occupancy(rgb, segments=seg)
            layout_integrity = basic_layout_integrity(
                rgb,
                segments=seg,
                occupancy=occupancy,
                ocr_status="ok" if ocr.get("available") else str(ocr.get("reason") or ocr.get("error") or "unavailable"),
            )
            # Pass 2: re-score density crediting the detected source figures/tables
            # so a placed figure's footprint is not counted as void even where its
            # interior is pale (2026-07-03 fix for image-heavy posters).
            fig_rects = layout_integrity.get("visual_region_rects") or []
            vmask = layout_integrity.pop("_visual_mask", None)
            if fig_rects or vmask is not None:
                occupancy = content_occupancy(rgb, segments=seg, visual_rects=fig_rects, visual_mask=vmask)
            strips = blank_strips(rgb, visual_rects=fig_rects, visual_mask=vmask)
            # the strip detector is the better void measure (catches section-bottom
            # gaps that the coarse occupancy grid absorbs); merge it in for scoring.
            occupancy["largest_blank_strip_ratio"] = strips.get("largest_blank_strip_ratio")
            occupancy["blank_strip_area_ratio"] = strips.get("blank_strip_area_ratio")
    bundles["basic_layout_integrity"] = {
        k: v for k, v in layout_integrity.items()
        if k != "findings"
    }
    findings.extend(layout_integrity.get("findings") or [])
    # Scope numeric grounding to the poster's authored text: pass OCR segments + the
    # detected figure regions so numbers inside figures/charts/screenshots (paper
    # reproductions) are excluded from fabrication checking.
    num_bundle, num_findings = numeric_token_metrics(
        snap,
        paper,
        segments=ocr.get("segments") if ocr.get("available") else None,
        visual_rects=layout_integrity.get("visual_region_rects"),
    )
    bundles[num_bundle.name] = num_bundle.metrics
    bundles[num_bundle.name]["text_source"] = "native" if native_text else ("ocr" if ocr.get("available") else "none")
    findings.extend(f.to_dict() for f in num_findings)

    # Visual-evidence GROUNDING (advisory, format-fair CV figure detection). Feeds the
    # VLM judge for visual_evidence_use; it is NOT a deterministic score (the component
    # below stays deferred_vlm) because CV misses sparse line-charts and text tables.
    visual_evidence: dict[str, Any] = {}
    if layout_integrity.get("available"):
        ve_bundle, ve_findings = visual_evidence_metrics(
            layout_integrity.get("figure_region_rects"),
            ocr.get("text_coverage_ratio") if ocr.get("available") else None,
            figure_detection_available=bool((layout_integrity.get("detector_coverage") or {}).get("cv_status") == "ok"),
        )
        bundles[ve_bundle.name] = {**ve_bundle.metrics, "bundle_status": ve_bundle.status}
        visual_evidence = ve_bundle.metrics
        findings.extend(f.to_dict() for f in ve_findings)

    body_screenshot_bundle, body_screenshot_findings = paper_body_screenshot_metrics(paper, ocr)
    bundles[body_screenshot_bundle.name] = {
        **body_screenshot_bundle.metrics,
        "bundle_status": body_screenshot_bundle.status,
    }
    body_screenshot = body_screenshot_bundle.metrics
    findings.extend(f.to_dict() for f in body_screenshot_findings)

    html_path = snap.resolved_files.get("html")
    canvas = _canvas_from_snapshot(snap)
    gate_audit: dict[str, Any] | None = None
    if html_path and canvas["w"] > 0 and canvas["h"] > 0:
        gate_audit = tool_render_audit(html=Path(html_path), canvas_w=canvas["w"], canvas_h=canvas["h"])
        for raw in gate_audit.get("findings", []):
            findings.append(_norm_finding(raw))

    floor = _load_gold_floor(case_slug, gold_spec)

    components: dict[str, Any] = {}
    score, metrics = _score_density(occupancy, ocr)
    score, metrics = _apply_body_screenshot_cap(
        score,
        metrics,
        body_screenshot,
        severe_cap=2.0,
        moderate_cap=5.5,
    )
    components["information_density_and_synthesis"] = _component(score, "tools", metrics)
    score, metrics = _score_faithfulness_numeric(bundles.get("numeric_token_exact_match", {}))
    components["source_faithfulness"] = _component(score, "tools", metrics)
    # visual_evidence_use: the SCORE stays deferred to the VLM judge — CV figure
    # detection is format-fair (image-native) but threshold-based, so it misses sparse
    # line-charts and text tables and must not hard-cap the score. The deterministic
    # signals (figure count/area + no-figures/wall flags) are carried as GROUNDING for
    # the VLM, which sees the image and owns the meaningful/decorative judgment.
    visual_score, visual_metrics, visual_status = _visual_component_with_body_screenshot_penalty(
        visual_evidence,
        body_screenshot,
    )
    components["visual_evidence_use"] = _component(
        visual_score,
        "tools" if visual_score is not None else "deferred_vlm",
        visual_metrics,
        status=visual_status,
    )
    components["basic_layout_integrity"] = _component(
        layout_integrity.get("score_0_10") if layout_integrity.get("available") else None,
        "tools",
        {k: v for k, v in layout_integrity.items() if k != "findings"},
        status=str(layout_integrity.get("status") or "ok"),
    )
    score, metrics = _score_layout(occupancy)
    score, metrics = _apply_body_screenshot_cap(
        score,
        metrics,
        body_screenshot,
        severe_cap=3.0,
        moderate_cap=6.0,
    )
    components["layout_readability"] = _component(score, "tools", metrics)

    norm_findings = [_norm_finding(f) for f in findings]
    p0_ids = [f["id"] for f in norm_findings if f["severity"] == "P0"]
    gate_ceiling = GATE_CEILING_SCORE
    if "paper-body-screenshot-catastrophic" in p0_ids:
        gate_ceiling = BODY_SCREENSHOT_CATASTROPHIC_GATE_CEILING_SCORE
    if "paper-body-screenshot-severe" in p0_ids:
        gate_ceiling = min(gate_ceiling, BODY_SCREENSHOT_GATE_CEILING_SCORE)
    if "basic-layout-empty-visual-placeholder-severe" in p0_ids:
        gate_ceiling = min(gate_ceiling, EMPTY_VISUAL_PLACEHOLDER_SEVERE_GATE_CEILING_SCORE)

    artifact_text_path = out_dir / "artifact_text.txt"
    artifact_text_path.write_text(snap.text or "", encoding="utf-8")

    report = {
        "version": QUALITY_SCHEMA_VERSION,
        "eval_protocol": EVAL_PROTOCOL,
        "evaluator_fingerprint": EVALUATOR_FINGERPRINT,
        "candidate_artifact": str(candidate_artifact),
        "paper": str(paper) if paper else None,
        "profile": profile,
        "case_slug": case_slug,
        "preview_image": snap.preview_image,
        "html_path": html_path,
        "canvas": canvas,
        "artifact_text_path": str(artifact_text_path),
        "metric_bundles": bundles,
        "gold_floor": floor,
        "ocr": {k: v for k, v in ocr.items() if k not in {"text", "segments"}},
        "spatial": {k: v for k, v in occupancy.items() if k not in {"occ", "std_grid"}},
        "blank_strips": {k: v for k, v in strips.items() if k not in {"strip"}},
        "tier_b_optional": {
            "html_available": bool(html_path),
            "dom_visible_text_word_count": bundles.get("html_structure", {}).get("visible_text_word_count"),
            "dom_image_layer_count": bundles.get("html_structure", {}).get("image_layer_count"),
            "note": "DOM signals are optional diagnostics only; they never enter the comparable image-mode score.",
        },
        "dimension_components": components,
        "gate": {
            "triggered": bool(p0_ids),
            "ceiling": gate_ceiling,
            "p0_finding_ids": p0_ids,
        },
        "findings": norm_findings,
    }
    atomic_write_json(out_dir / "deterministic_report.json", report)
    return report


# --- aggregator (reproducible arithmetic) ------------------------------------

def aggregate_final(
    deterministic: dict[str, Any],
    judge_report: dict[str, Any] | None,
    *,
    mode: str,
    candidate_name: str,
    artifact: Path | str,
    paper: Path | str | None,
    profile: str | None = None,
    deterministic_path: str | None = None,
    judge_path: str | None = None,
) -> PosterQualityReport:
    components = deterministic.get("dimension_components", {}) or {}
    judge_dims = (judge_report or {}).get("dimension_scores", {}) if judge_report else {}
    if not isinstance(judge_dims, dict):
        judge_dims = {}
    basic_layout_entry = components.get("basic_layout_integrity")
    basic_layout_score = (
        _float_or_none(basic_layout_entry.get("score_0_10"))
        if isinstance(basic_layout_entry, dict)
        else None
    )
    layout_cap_policy = layout_coupled_cap_policy(
        basic_layout_score=basic_layout_score,
        findings=deterministic.get("findings", []) or [],
    )
    information_density_entry = components.get("information_density_and_synthesis")
    information_density_score = (
        _float_or_none(information_density_entry.get("score_0_10"))
        if isinstance(information_density_entry, dict)
        else None
    )
    aesthetics_cap_policy = academic_poster_aesthetics_cap_policy(
        information_density_score=information_density_score,
    )
    basic_layout_metrics = (
        basic_layout_entry.get("metrics", {})
        if isinstance(basic_layout_entry, dict)
        else {}
    )
    legibility_cap_policy = poster_scale_legibility_cap_policy(
        median_body_text_height_ref_px=_float_or_none(
            basic_layout_metrics.get("median_body_text_height_ref_px")
            if isinstance(basic_layout_metrics, dict)
            else None
        ),
    )
    layout_dimension_caps = layout_cap_policy.get("dimension_caps", {})
    aesthetics_dimension_caps = aesthetics_cap_policy.get("dimension_caps", {})
    legibility_dimension_caps = legibility_cap_policy.get("dimension_caps", {})
    dimension_caps = _merge_dimension_caps(
        layout_dimension_caps,
        aesthetics_dimension_caps,
        legibility_dimension_caps,
    )
    capped_dimensions: list[dict[str, Any]] = []

    dim_scores: list[RubricDimensionScore] = []
    scored_weight = 0.0
    weighted = 0.0
    for dim in DIMENSIONS:
        det = components.get(dim.id) if isinstance(components.get(dim.id), dict) else None
        det_score = det.get("score_0_10") if det else None
        subj_score, subj_rationale, subj_evidence = _judge_dim(judge_dims.get(dim.id))

        final_score: float | None = None
        source = "placeholder"
        status = str(det.get("status") or "ok") if det else "ok"
        if dim.owner == "deterministic":
            final_score, source = det_score, "tools"
            if final_score is None:
                status = "skipped"
        elif dim.owner == "subjective":
            status = "ok"
            final_score, source = subj_score, "judge"
            if final_score is None:
                status = "needs_judge"
        else:  # mixed
            present = [v for v in (det_score, subj_score) if v is not None]
            if present:
                final_score = round(sum(present) / len(present), 2)
                source = "blend" if len(present) == 2 else ("tools" if det_score is not None else "judge")
                if det and str(det.get("status") or "ok") != "ok" and subj_score is None:
                    status = str(det.get("status"))
            else:
                status = "needs_judge"

        cap = _float_or_none(dimension_caps.get(dim.id) if isinstance(dimension_caps, dict) else None)
        cap_sources = _cap_sources_for_dimension(
            dim.id,
            final_score,
            layout_dimension_caps,
            aesthetics_dimension_caps,
            legibility_dimension_caps,
        )
        if final_score is not None and cap is not None and final_score > cap:
            capped_dimensions.append({
                "dimension": dim.id,
                "original_score_0_10": final_score,
                "capped_score_0_10": cap,
                "cap_sources": cap_sources,
            })
            final_score = cap
            status = "warning"

        norm = (final_score / 10.0) if final_score is not None else None
        if norm is not None:
            scored_weight += dim.weight
            weighted += dim.weight * norm
        metrics = dict(det.get("metrics", {}) if det else {})
        if any(item["dimension"] == dim.id and "layout" in item.get("cap_sources", []) for item in capped_dimensions):
            metrics["layout_coupled_cap"] = {
                "score_ceiling": _float_or_none(layout_dimension_caps.get(dim.id) if isinstance(layout_dimension_caps, dict) else None),
                "basic_layout_score": basic_layout_score,
                "triggered_rules": layout_cap_policy.get("triggered_rules", []),
            }
        if any(item["dimension"] == dim.id and "academic_aesthetics" in item.get("cap_sources", []) for item in capped_dimensions):
            metrics["academic_poster_aesthetics_cap"] = {
                "score_ceiling": _float_or_none(aesthetics_dimension_caps.get(dim.id) if isinstance(aesthetics_dimension_caps, dict) else None),
                "information_density_score": information_density_score,
                "triggered_rules": aesthetics_cap_policy.get("triggered_rules", []),
            }
        if any(item["dimension"] == dim.id and "poster_scale_legibility" in item.get("cap_sources", []) for item in capped_dimensions):
            metrics["poster_scale_legibility_cap"] = {
                "score_ceiling": _float_or_none(
                    legibility_dimension_caps.get(dim.id)
                    if isinstance(legibility_dimension_caps, dict)
                    else None
                ),
                "median_body_text_height_ref_px": legibility_cap_policy.get(
                    "median_body_text_height_ref_px"
                ),
                "triggered_rules": legibility_cap_policy.get("triggered_rules", []),
            }
        dim_scores.append(RubricDimensionScore(
            id=dim.id,
            weight=dim.weight,
            owner=dim.owner,
            score_0_10=final_score,
            normalized=round(norm, 4) if norm is not None else None,
            source=source,
            status=status,
            rationale=subj_rationale or (det.get("rationale", "") if det else ""),
            visible_evidence=subj_evidence,
            metrics=metrics,
        ))

    findings = _collect_findings(deterministic, judge_report)

    overall = round(100.0 * weighted / scored_weight, 2) if scored_weight > 0 else None
    layout_overall_ceiling = _float_or_none(layout_cap_policy.get("overall_ceiling"))
    if overall is not None and layout_overall_ceiling is not None:
        overall = min(overall, layout_overall_ceiling)
    layout_capped_dimensions = [
        item for item in capped_dimensions
        if "layout" in item.get("cap_sources", [])
    ]
    aesthetics_capped_dimensions = [
        item for item in capped_dimensions
        if "academic_aesthetics" in item.get("cap_sources", [])
    ]
    legibility_capped_dimensions = [
        item for item in capped_dimensions
        if "poster_scale_legibility" in item.get("cap_sources", [])
    ]
    if layout_capped_dimensions or layout_overall_ceiling is not None:
        findings.append(PosterQualityFinding(
            id=_LAYOUT_COUPLED_CAP_FINDING_ID,
            severity="P1" if layout_overall_ceiling is not None else "P2",
            message="Basic layout damage caps related visual-quality scores.",
            dimension="basic_layout_integrity",
            evidence={
                **layout_cap_policy,
                "capped_dimensions": layout_capped_dimensions,
            },
        ))
    if aesthetics_capped_dimensions:
        findings.append(PosterQualityFinding(
            id=_ACADEMIC_AESTHETICS_CAP_FINDING_ID,
            severity="P2",
            message="Low information density caps human academic-poster aesthetics.",
            dimension="professional_aesthetics",
            evidence={
                **aesthetics_cap_policy,
                "capped_dimensions": aesthetics_capped_dimensions,
            },
        ))
    if legibility_capped_dimensions:
        findings.append(PosterQualityFinding(
            id=_POSTER_SCALE_LEGIBILITY_CAP_FINDING_ID,
            severity="P2",
            message="Poster-scale body text limits the layout-readability score.",
            dimension="layout_readability",
            evidence={
                **legibility_cap_policy,
                "capped_dimensions": legibility_capped_dimensions,
            },
        ))
    presentation_viability_cap = presentation_viability_cap_policy(dim_scores)
    if overall is not None and presentation_viability_cap is not None:
        overall = min(overall, float(presentation_viability_cap["score_ceiling"]))
        findings.append(PosterQualityFinding(
            id=_PRESENTATION_VIABILITY_CAP_FINDING_ID,
            severity="P1",
            message="Low presentation viability caps the overall poster score.",
            dimension="layout_readability",
            evidence=presentation_viability_cap,
        ))
    deterministic_visual_failure = _deterministic_visual_failure_ceiling(deterministic)
    if overall is not None and deterministic_visual_failure:
        score_ceiling = float(deterministic_visual_failure["score_ceiling"])
        overall = min(overall, score_ceiling)
        findings.append(PosterQualityFinding(
            id="deterministic-major-visual-failure",
            severity="P1",
            message="A reproducible image-native check found a severe visible poster failure.",
            dimension="basic_layout_integrity",
            evidence=deterministic_visual_failure,
        ))
    major_visual_failure = _confirmed_major_visual_failure(deterministic, judge_report)
    if overall is not None and major_visual_failure:
        score_ceiling = float(major_visual_failure["score_ceiling"])
        overall = min(overall, score_ceiling)
        findings.append(PosterQualityFinding(
            id="judge-confirmed-major-visual-failure",
            severity="P1",
            message="Serious visible defects were confirmed across multiple visual-quality signals.",
            dimension="layout_readability",
            evidence={
                **major_visual_failure,
            },
        ))
    elif overall is not None:
        serious_dimensions, serious_defects = _confirmed_serious_visual_defects(judge_report)
        if len(serious_dimensions) >= 2:
            overall = min(overall, SERIOUS_VISUAL_DEFECT_PASS_CEILING_SCORE)
            findings.append(PosterQualityFinding(
                id="judge-confirmed-serious-visual-defect",
                severity="P1",
                message="Serious visible defects across multiple dimensions prevent a clean pass.",
                dimension=serious_dimensions[0],
                evidence={
                    "serious_dimensions": serious_dimensions,
                    "serious_defects": serious_defects[:6],
                    "score_ceiling": SERIOUS_VISUAL_DEFECT_PASS_CEILING_SCORE,
                },
            ))
    gate = deterministic.get("gate", {}) or {}
    raw_ceiling = gate.get("ceiling")
    ceiling = GATE_CEILING_SCORE if raw_ceiling is None else float(raw_ceiling)
    judge_p0 = any(getattr(f, "severity", None) == "P0" for f in findings)
    gate_triggered = bool(gate.get("triggered")) or judge_p0
    if overall is not None and gate_triggered:
        overall = min(overall, ceiling)

    verdict = _verdict(overall, gate_triggered, mode)
    return PosterQualityReport(
        candidate_name=candidate_name,
        artifact=str(artifact),
        paper=str(paper) if paper else None,
        mode=mode,  # type: ignore[arg-type]
        profile=profile,
        overall_score_0_100=overall,
        gate_ceiling=ceiling if gate_triggered else None,
        gate_triggered=gate_triggered,
        verdict=verdict,  # type: ignore[arg-type]
        dimensions=dim_scores,
        findings=findings,
        executive_summary=str((judge_report or {}).get("executive_summary", "")),
        deterministic_report_path=deterministic_path,
        judge_report_path=judge_path,
    )


# --- scoring helpers ---------------------------------------------------------

def _component(score: float | None, source: str, metrics: dict[str, Any], *, status: str | None = None) -> dict[str, Any]:
    return {"score_0_10": score, "source": source, "status": status or "ok", "metrics": metrics}


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def presentation_viability_cap_policy(
    dimensions: list[RubricDimensionScore],
) -> dict[str, Any] | None:
    scores = {dimension.id: dimension.score_0_10 for dimension in dimensions}
    density = _float_or_none(scores.get("information_density_and_synthesis"))
    readability = _float_or_none(scores.get("layout_readability"))
    aesthetics = _float_or_none(scores.get("professional_aesthetics"))
    if density is None or readability is None or aesthetics is None:
        return None

    minimum = min(density, readability, aesthetics)
    viability = 0.50 * density + 0.25 * readability + 0.25 * aesthetics
    if minimum >= 6.0 or viability >= 6.25:
        return None

    score_ceiling = min(69.0, 10.0 * (viability + 1.0))
    weak_dimensions = sorted(
        dimension
        for dimension, score in {
            "information_density_and_synthesis": density,
            "layout_readability": readability,
            "professional_aesthetics": aesthetics,
        }.items()
        if score < 6.0
    )
    return {
        "information_density_and_synthesis": density,
        "layout_readability": readability,
        "professional_aesthetics": aesthetics,
        "presentation_viability": round(viability, 4),
        "minimum_input_score": minimum,
        "minimum_input_below_6": minimum < 6.0,
        "presentation_viability_below_6_25": viability < 6.25,
        "weak_dimensions": weak_dimensions,
        "input_score_stage": "post_dimension_caps",
        "minimum_input_threshold_0_10": 6.0,
        "presentation_viability_threshold_0_10": 6.25,
        "score_ceiling": round(score_ceiling, 2),
    }


def layout_coupled_cap_policy(
    *,
    basic_layout_score: float | None,
    findings: list[Any],
) -> dict[str, Any]:
    """Return dimension/overall caps when deterministic layout evidence is severe.

    BLI is the reproducible image-native signal. When it reports clear P1
    export-edge, truncation, or section overflow damage, the subjective visual
    dimensions should not retain clean-pass scores merely because the VLM liked
    the poster globally.
    """
    blocking_findings: list[dict[str, Any]] = []
    crop_findings: list[dict[str, Any]] = []
    inferred_untrusted_penalty = 0.0
    for finding in findings:
        fid = _finding_attr(finding, "id")
        if not fid or fid == _LAYOUT_COUPLED_CAP_FINDING_ID:
            continue
        severity = _finding_attr(finding, "severity").upper()
        dimension = _finding_attr(finding, "dimension")
        if dimension and dimension != "basic_layout_integrity":
            continue
        evidence = _finding_evidence(finding)
        trusted_p1 = severity == "P1" and _trusted_layout_p1(evidence)
        record = {
            "id": fid,
            "severity": severity or "P2",
            "trusted_p1": trusted_p1,
            "confidence": str(evidence.get("confidence") or ""),
            "source": str(evidence.get("boundary_source") or evidence.get("source") or ""),
        }
        if not trusted_p1 and record["source"].lower() in {"inferred", "inferred_open_grid"}:
            penalty = _float_or_none(_finding_attr(finding, "penalty"))
            if penalty is not None and penalty > 0:
                inferred_untrusted_penalty += penalty
        if fid in _LAYOUT_BLOCKING_FINDING_IDS and trusted_p1:
            blocking_findings.append(record)
        if fid in _LAYOUT_VISUAL_CROP_FINDING_IDS and trusted_p1:
            crop_findings.append(record)

    dimension_caps: dict[str, float] = {}
    triggered_rules: list[str] = []
    cap_basis_score = (
        min(10.0, basic_layout_score + inferred_untrusted_penalty)
        if basic_layout_score is not None
        else None
    )
    if cap_basis_score is not None and cap_basis_score < 6.0 and blocking_findings:
        dimension_caps.update({
            "visual_evidence_use": 5.0,
            "layout_readability": 6.0,
            "professional_aesthetics": 6.0,
        })
        triggered_rules.append("basic_layout_below_6_caps_visual_dimensions")
    elif cap_basis_score is not None and cap_basis_score < 7.0 and blocking_findings:
        dimension_caps.update({
            "layout_readability": 7.0,
            "professional_aesthetics": 7.0,
        })
        triggered_rules.append("p1_layout_damage_caps_readability_and_aesthetics")

    if cap_basis_score is not None and cap_basis_score < 7.0 and crop_findings:
        dimension_caps["visual_evidence_use"] = min(
            dimension_caps.get("visual_evidence_use", 10.0),
            6.0,
        )
        triggered_rules.append("visible_crop_damage_caps_visual_evidence")

    overall_ceiling: float | None = None
    if cap_basis_score is not None and blocking_findings:
        if cap_basis_score <= 4.5:
            overall_ceiling = LAYOUT_COUPLED_SEVERE_CEILING_SCORE
            triggered_rules.append("p1_layout_damage_with_bli_at_or_below_4_5_caps_overall_60")
        elif cap_basis_score < 6.0:
            overall_ceiling = LAYOUT_COUPLED_WARNING_CEILING_SCORE
            triggered_rules.append("p1_layout_damage_with_bli_below_6_caps_overall_68")

    return {
        "basic_layout_score": basic_layout_score,
        "cap_basis_score": cap_basis_score,
        "excluded_inferred_penalty": round(inferred_untrusted_penalty, 3),
        "dimension_caps": dimension_caps,
        "overall_ceiling": overall_ceiling,
        "blocking_findings": blocking_findings,
        "crop_findings": crop_findings,
        "triggered_rules": triggered_rules,
    }


def academic_poster_aesthetics_cap_policy(
    *,
    information_density_score: float | None,
) -> dict[str, Any]:
    """Cap generic clean aesthetics when the poster is too sparse for a conference poster.

    ``professional_aesthetics`` should reward human academic-poster craft, not a
    clean report page with generous whitespace. This cap is intentionally narrow:
    it only touches the aesthetics dimension and leaves density itself to its
    existing weighted score.
    """
    dimension_caps: dict[str, float] = {}
    triggered_rules: list[str] = []
    if information_density_score is not None:
        if information_density_score < 6.0:
            dimension_caps["professional_aesthetics"] = 6.0
            triggered_rules.append("info_density_below_6_caps_academic_aesthetics_6")
        elif information_density_score < 6.5:
            dimension_caps["professional_aesthetics"] = 6.5
            triggered_rules.append("info_density_below_6_5_caps_academic_aesthetics_6_5")
        elif information_density_score < 7.0:
            dimension_caps["professional_aesthetics"] = 7.0
            triggered_rules.append("info_density_below_7_caps_academic_aesthetics_7")

    return {
        "information_density_score": information_density_score,
        "dimension_caps": dimension_caps,
        "triggered_rules": triggered_rules,
    }


def poster_scale_legibility_cap_policy(
    *,
    median_body_text_height_ref_px: float | None,
) -> dict[str, Any]:
    """Return the poster-scale readability ceiling for OCR body text."""
    dimension_caps: dict[str, float] = {}
    triggered_rules: list[str] = []
    value = median_body_text_height_ref_px
    if value is not None:
        if value < 16.0:
            dimension_caps["layout_readability"] = 5.0
            triggered_rules.append("median_body_text_below_16_caps_readability_5")
        elif value < 18.0:
            dimension_caps["layout_readability"] = 6.0
            triggered_rules.append("median_body_text_below_18_caps_readability_6")
        elif value < 20.0:
            dimension_caps["layout_readability"] = 7.0
            triggered_rules.append("median_body_text_below_20_caps_readability_7")
    return {
        "median_body_text_height_ref_px": value,
        "dimension_caps": dimension_caps,
        "triggered_rules": triggered_rules,
    }


def _merge_dimension_caps(*caps_list: Any) -> dict[str, float]:
    merged: dict[str, float] = {}
    for caps in caps_list:
        if not isinstance(caps, dict):
            continue
        for dim_id, raw_cap in caps.items():
            cap = _float_or_none(raw_cap)
            if cap is None:
                continue
            merged[str(dim_id)] = min(merged.get(str(dim_id), cap), cap)
    return merged


def _cap_sources_for_dimension(
    dim_id: str,
    score: float | None,
    layout_dimension_caps: Any,
    aesthetics_dimension_caps: Any,
    legibility_dimension_caps: Any,
) -> list[str]:
    if score is None:
        return []
    sources: list[str] = []
    layout_cap = _float_or_none(layout_dimension_caps.get(dim_id) if isinstance(layout_dimension_caps, dict) else None)
    aesthetics_cap = _float_or_none(aesthetics_dimension_caps.get(dim_id) if isinstance(aesthetics_dimension_caps, dict) else None)
    legibility_cap = _float_or_none(legibility_dimension_caps.get(dim_id) if isinstance(legibility_dimension_caps, dict) else None)
    if layout_cap is not None and score > layout_cap:
        sources.append("layout")
    if aesthetics_cap is not None and score > aesthetics_cap:
        sources.append("academic_aesthetics")
    if legibility_cap is not None and score > legibility_cap:
        sources.append("poster_scale_legibility")
    return sources


def _finding_evidence(finding: Any) -> dict[str, Any]:
    if isinstance(finding, dict):
        evidence = finding.get("evidence")
    else:
        evidence = getattr(finding, "evidence", None)
    return evidence if isinstance(evidence, dict) else {}


def _trusted_layout_p1(evidence: dict[str, Any]) -> bool:
    if evidence.get("trusted_p1") is not None:
        return bool(evidence.get("trusted_p1"))
    source = str(evidence.get("boundary_source") or evidence.get("source") or "").lower()
    confidence = str(evidence.get("confidence") or "").lower()
    return source in {"canvas", "closed_panel", "closed-frame", "closed_frame"} and confidence in {
        "high",
        "trusted",
    }


def _finding_attr(finding: Any, attr: str) -> str:
    if isinstance(finding, dict):
        value = finding.get(attr)
    else:
        value = getattr(finding, attr, None)
    return str(value or "")


def _effective_void_ratio(occupancy: dict[str, Any]) -> tuple[float | None, dict[str, Any]]:
    """Combine single large voids with cumulative blank-band area.

    ``largest_blank_strip_ratio`` catches horizontal dead bands. The occupancy
    grid's largest empty rectangle catches vertical/local voids, but normal
    gutters can look like empty rectangles, so only the excess beyond a small
    gutter allowance contributes. Repeated blank strips also contribute only
    beyond the normal spacing baseline observed in dense posters.
    """
    blank_strip = _float_or_none(occupancy.get("largest_blank_strip_ratio"))
    empty_rect = _float_or_none(occupancy.get("largest_empty_rect_cell_ratio"))
    strip_area = _float_or_none(occupancy.get("blank_strip_area_ratio"))
    if blank_strip is None and empty_rect is None and strip_area is None:
        return None, {}

    strip_void = max(0.0, blank_strip or 0.0)
    local_void = min(0.16, 1.25 * max(0.0, (empty_rect or 0.0) - 0.035))
    cumulative_void = min(0.14, 0.50 * max(0.0, (strip_area or 0.0) - 0.20))
    effective = max(strip_void, local_void, cumulative_void)
    return effective, {
        "largest_blank_strip_ratio": blank_strip,
        "largest_empty_rect_cell_ratio": empty_rect,
        "blank_strip_area_ratio": strip_area,
        "local_empty_void_ratio": round(local_void, 4),
        "cumulative_blank_void_ratio": round(cumulative_void, 4),
        "effective_void_ratio": round(effective, 4),
    }


def _score_density(occupancy: dict[str, Any], ocr: dict[str, Any]) -> tuple[float | None, dict[str, Any]]:
    """Image-native, format-normalized density score.

    Density base = content occupancy (fraction of the canvas with real content —
    not flat color/dark fills) plus OCR text richness, both as resolution-invariant
    ratios. A large empty region anywhere in the layout applies a multiplicative
    void penalty. Computed purely from the rendered image, so PNG/JPG/PDF/HTML of
    the same poster score identically.
    """
    content = occupancy.get("content_coverage")
    rect, void_metrics = _effective_void_ratio(occupancy)
    if content is None:
        return None, {}
    text_cov = ocr.get("text_coverage_ratio") if ocr.get("available") else None

    content_norm = min(1.0, float(content) / REF_CONTENT_COVERAGE) if REF_CONTENT_COVERAGE else 0.0
    text_norm = (
        min(1.0, float(text_cov) / REF_TEXT_COVERAGE_RATIO)
        if (text_cov is not None and REF_TEXT_COVERAGE_RATIO)
        else None
    )
    # Text only *lifts*: max() against content means a text-poor or OCR-failed
    # poster falls back to its content score instead of being capped.
    text_eff = max(text_norm, content_norm) if text_norm is not None else content_norm
    base = DENSITY_CONTENT_WEIGHT * content_norm + DENSITY_TEXT_WEIGHT * text_eff

    void_penalty = 1.0
    if rect is not None and EMPTY_RECT_CAP:
        void_penalty = 1.0 - DENSITY_VOID_PENALTY_K * min(1.0, float(rect) / EMPTY_RECT_CAP)
    score = round(max(0.0, min(10.0, 10.0 * base * void_penalty)), 2)
    return score, {
        "content_coverage": content,
        "text_coverage_ratio": text_cov,
        **void_metrics,
        "content_ref": REF_CONTENT_COVERAGE,
        "text_coverage_ref": REF_TEXT_COVERAGE_RATIO,
        "content_norm": round(content_norm, 3),
        "text_lift": round(text_eff - content_norm, 3),
        "void_penalty": round(void_penalty, 3),
        "text_source": "ocr" if text_cov is not None else "none",
    }


def _score_layout(occupancy: dict[str, Any]) -> tuple[float | None, dict[str, Any]]:
    """Image-native layout score: content spread minus large empty regions.

    A poster with a big empty rectangle (a void between/below sections, an empty
    half, etc.) is a layout failure even if other regions are dense, so the void
    drives the score toward zero independently of overall coverage.
    """
    content = occupancy.get("content_coverage")
    rect, void_metrics = _effective_void_ratio(occupancy)
    if content is None:
        return None, {}
    content_norm = min(1.0, float(content) / REF_CONTENT_COVERAGE) if REF_CONTENT_COVERAGE else float(content)
    void_factor = (
        1.0 - LAYOUT_VOID_PENALTY_K * min(1.0, float(rect or 0.0) / LAYOUT_VOID_CAP)
        if LAYOUT_VOID_CAP else 1.0
    )
    score = round(max(0.0, min(10.0, 10.0 * content_norm * void_factor)), 2)
    return score, {
        "content_coverage": content,
        "content_norm": round(content_norm, 3),
        **void_metrics,
        "void_factor": round(void_factor, 3),
    }


def _apply_body_screenshot_cap(
    score: float | None,
    metrics: dict[str, Any],
    body_screenshot: dict[str, Any],
    *,
    severe_cap: float,
    moderate_cap: float,
) -> tuple[float | None, dict[str, Any]]:
    level = str(body_screenshot.get("severity_level") or "none")
    cap = 0.0 if level == "catastrophic" else (severe_cap if level == "severe" else (moderate_cap if level == "moderate" else None))
    if cap is None or score is None:
        return score, metrics
    capped = round(min(float(score), cap), 2)
    updated = dict(metrics)
    updated["paper_body_screenshot_penalty"] = {
        "severity_level": level,
        "score_before_cap": score,
        "cap": cap,
        "score_after_cap": capped,
        "copied_token_count": body_screenshot.get("copied_token_count"),
        "copied_token_ratio": body_screenshot.get("copied_token_ratio"),
        "exact_ngram_hit_ratio": body_screenshot.get("exact_ngram_hit_ratio"),
    }
    return capped, updated


def _visual_component_with_body_screenshot_penalty(
    visual_evidence: dict[str, Any],
    body_screenshot: dict[str, Any],
) -> tuple[float | None, dict[str, Any], str]:
    metrics = dict(visual_evidence or {})
    level = str(body_screenshot.get("severity_level") or "none")
    if level in {"catastrophic", "severe"}:
        metrics["paper_body_screenshot_penalty"] = {
            "severity_level": level,
            "score_0_10": 0.0,
            "reason": "Raw source-paper body screenshots are not meaningful visual evidence.",
            "copied_token_count": body_screenshot.get("copied_token_count"),
            "copied_token_ratio": body_screenshot.get("copied_token_ratio"),
            "exact_ngram_hit_ratio": body_screenshot.get("exact_ngram_hit_ratio"),
        }
        return 0.0, metrics, "error"
    if level == "moderate":
        metrics["paper_body_screenshot_penalty"] = {
            "severity_level": level,
            "score_0_10": 2.0,
            "reason": "Substantial raw source prose weakens visual-evidence use.",
            "copied_token_count": body_screenshot.get("copied_token_count"),
            "copied_token_ratio": body_screenshot.get("copied_token_ratio"),
            "exact_ngram_hit_ratio": body_screenshot.get("exact_ngram_hit_ratio"),
        }
        return 2.0, metrics, "warning"
    return None, metrics, str(metrics.get("status") or "ok") if metrics else "ok"


def _score_faithfulness_numeric(metrics: dict[str, Any]) -> tuple[float | None, dict[str, Any]]:
    if not metrics.get("available"):
        return None, {}
    # Score over SALIENT grounding (decimals/%/×/magnitudes/large ints), so trivial
    # coincidental matches can't dilute a fabrication and unit/format variants don't
    # false-flag. Fall back to the legacy value-exact ratio when nothing is salient.
    ratio = metrics.get("salient_grounding_ratio")
    if ratio is None:
        ratio = metrics.get("exact_match_ratio") or 0.0
    ratio = float(ratio)
    return round(ratio * 10.0, 2), {
        "salient_grounding_ratio": ratio,
        "salient_token_count": metrics.get("salient_token_count"),
        "salient_fabricated": metrics.get("salient_fabricated"),
        "salient_near_miss": metrics.get("salient_near_miss"),
        "exact_match_ratio": metrics.get("exact_match_ratio"),
    }


# --- findings + verdict ------------------------------------------------------

def _judge_dim(entry: Any) -> tuple[float | None, str, list[str]]:
    if not isinstance(entry, dict):
        return None, "", []
    raw = entry.get("score_0_10", entry.get("score"))
    try:
        score = float(raw)
    except (TypeError, ValueError):
        score = None
    if score is not None and not (0.0 <= score <= 10.0):
        score = max(0.0, min(10.0, score))
    evidence = entry.get("visible_evidence")
    return score, str(entry.get("rationale") or ""), evidence if isinstance(evidence, list) else []


def _confirmed_major_visual_failure(
    deterministic: dict[str, Any],
    judge_report: dict[str, Any] | None,
) -> dict[str, Any] | None:
    serious_dimensions, serious_defects = _confirmed_serious_visual_defects(judge_report)
    components = deterministic.get("dimension_components", {}) or {}
    weak_objective_dimensions: list[str] = []
    for dimension in _OBJECTIVE_VISUAL_DIMENSIONS:
        entry = components.get(dimension)
        if not isinstance(entry, dict):
            continue
        score = _float_or_none(entry.get("score_0_10"))
        if score is not None and score <= 4.0:
            weak_objective_dimensions.append(dimension)

    basic_layout = components.get("basic_layout_integrity")
    basic_layout_score = (
        _float_or_none(basic_layout.get("score_0_10"))
        if isinstance(basic_layout, dict)
        else None
    )
    basic_layout_collapsed = (
        basic_layout_score is not None
        and basic_layout_score <= BASIC_LAYOUT_COLLAPSE_SCORE
    )

    if serious_dimensions and len(weak_objective_dimensions) >= 2:
        score_ceiling = MAJOR_VISUAL_FAILURE_CEILING_SCORE
        corroboration = "judge_and_objective_metrics"
    elif len(serious_dimensions) >= 3:
        score_ceiling = MULTI_JUDGE_VISUAL_FAILURE_CEILING_SCORE
        corroboration = "three_judge_dimensions"
    elif len(serious_dimensions) >= 2 and basic_layout_collapsed:
        score_ceiling = MULTI_JUDGE_VISUAL_FAILURE_CEILING_SCORE
        corroboration = "multiple_judge_dimensions_and_basic_layout_collapse"
    else:
        return None
    return {
        "serious_dimensions": serious_dimensions,
        "weak_objective_dimensions": sorted(weak_objective_dimensions),
        "basic_layout_score": basic_layout_score,
        "serious_defects": serious_defects[:6],
        "corroboration": corroboration,
        "score_ceiling": score_ceiling,
    }


def _deterministic_visual_failure_ceiling(deterministic: dict[str, Any]) -> dict[str, Any] | None:
    """Return a ceiling for image-native failures that do not need VLM agreement."""
    findings = deterministic.get("findings", []) or []
    finding_ids = {
        str(finding.get("id") or "")
        for finding in findings
        if isinstance(finding, dict)
    }
    triggered_rules: list[str] = []
    if finding_ids & {
        "basic-layout-empty-visual-placeholder",
        "basic-layout-near-empty-visual-slot",
    }:
        triggered_rules.append("empty_or_near_empty_visual_slot")
    if "basic-layout-multi-panel-crop-failure" in finding_ids:
        triggered_rules.append("multi_panel_crop_failure")
    if not triggered_rules:
        return None
    return {
        "triggered_rules": triggered_rules,
        "score_ceiling": min(
            EMPTY_VISUAL_PLACEHOLDER_CEILING_SCORE,
            MULTI_PANEL_CROP_FAILURE_CEILING_SCORE,
        ),
    }


def _confirmed_serious_visual_defects(
    judge_report: dict[str, Any] | None,
) -> tuple[list[str], list[dict[str, Any]]]:
    judge_dims = (judge_report or {}).get("dimension_scores", {})
    if not isinstance(judge_dims, dict):
        return [], []
    serious_dimensions: list[str] = []
    serious_defects: list[dict[str, Any]] = []
    for dimension in _VISUAL_QUALITY_DIMENSIONS:
        entry = judge_dims.get(dimension)
        if not isinstance(entry, dict):
            continue
        defects = entry.get("defects_found")
        if not isinstance(defects, list):
            continue
        confirmed = [
            defect for defect in defects
            if isinstance(defect, dict) and str(defect.get("severity") or "").lower() == "serious"
        ]
        if confirmed:
            serious_dimensions.append(dimension)
            serious_defects.extend({"dimension": dimension, **defect} for defect in confirmed[:3])
    return sorted(serious_dimensions), serious_defects


def _collect_findings(deterministic: dict[str, Any], judge_report: dict[str, Any] | None) -> list[PosterQualityFinding]:
    out: list[PosterQualityFinding] = []
    for raw in deterministic.get("findings", []) or []:
        out.append(PosterQualityFinding(
            id=str(raw.get("id") or "finding"),
            severity=raw.get("severity") or "P2",
            message=str(raw.get("message") or ""),
            dimension=raw.get("dimension"),
            evidence=raw.get("evidence") or {},
        ))
    for raw in (judge_report or {}).get("findings", []) or []:
        if not isinstance(raw, dict):
            continue
        out.append(PosterQualityFinding(
            id=str(raw.get("id") or "agent-finding"),
            severity=_sev(raw),
            message=str(raw.get("message") or raw.get("description") or ""),
            dimension=raw.get("dimension"),
            evidence=raw.get("evidence") or {},
        ))
    return out


def _verdict(overall: float | None, gate_triggered: bool, mode: str) -> str:
    if mode != "benchmark" or overall is None:
        return "incomplete"
    if overall >= PASS_THRESHOLD and not gate_triggered:
        return "pass"
    if overall >= REVISE_THRESHOLD:
        return "revise"
    return "fail"


def _norm_finding(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(raw.get("id") or "finding"),
        "severity": _sev(raw),
        "message": str(raw.get("message") or raw.get("description") or ""),
        "dimension": raw.get("dimension") or _dimension_for(raw),
        "evidence": raw.get("evidence") or raw.get("details") or {},
    }


def _sev(raw: dict[str, Any]) -> str:
    value = str(raw.get("severity") or "").strip().upper()
    if value in {"P0", "P1", "P2"}:
        return value
    if value in {"WARNING", "WARN", "INFO"}:
        return "P2"
    if value in {"BLOCKER", "HIGH", "ERROR"}:
        return "P0" if value == "BLOCKER" else "P1"
    return "P2"


def _dimension_for(raw: dict[str, Any]) -> str | None:
    category = str(raw.get("category") or "").lower()
    text = f"{raw.get('id', '')} {raw.get('message', '')}".lower()
    if category in {"render_integrity"} or any(t in text for t in ("overflow", "clip", "missing", "root")):
        return "render_integrity"
    if category == "source_grounding" or "numeric" in text:
        return "source_faithfulness"
    if category in {"visual_density", "text_density"} or "blank" in text or "sparse" in text:
        return "information_density_and_synthesis"
    return None


def _canvas_from_snapshot(snap: Any) -> dict[str, int]:
    if getattr(snap, "html", None):
        try:
            from .adapter import _html_render_hint

            w, h, _selector, _full = _html_render_hint(snap.html)
            return {"w": int(w), "h": int(h)}
        except Exception:  # noqa: BLE001
            pass
    return {"w": int(getattr(snap, "width", 0) or 0), "h": int(getattr(snap, "height", 0) or 0)}


def _load_gold_floor(case_slug: str | None, gold_spec: dict[str, Any] | None) -> dict[str, Any]:
    spec = gold_spec
    if spec is None:
        try:
            spec = json.loads(GOLD_SPEC_PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
    refs = spec.get("primary_gold_references") if isinstance(spec, dict) else None
    if not isinstance(refs, dict) or not refs:
        return {}
    if case_slug and isinstance(refs.get(case_slug), dict):
        floor = refs[case_slug].get("floor")
        if isinstance(floor, dict):
            return floor
    floors = [ref.get("floor") for ref in refs.values() if isinstance(ref, dict) and isinstance(ref.get("floor"), dict)]
    if not floors:
        return {}
    return {
        "min_nonwhite_pixel_ratio": min(float(f.get("min_nonwhite_pixel_ratio") or 0.0) for f in floors),
        "max_longest_blank_vertical_run_ratio": max(float(f.get("max_longest_blank_vertical_run_ratio") or 0.0) for f in floors),
        "min_leaf_visible_words": min(float(f.get("min_leaf_visible_words") or 0.0) for f in floors),
        "min_native_information_units": min(float(f.get("min_native_information_units") or 0.0) for f in floors),
        "min_source_figure_area_ratio": min(float(f.get("min_source_figure_area_ratio") or 0.0) for f in floors),
    }
