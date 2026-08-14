"""Artifact-type rubric routing."""

from __future__ import annotations

from typing import Any

from .schema import ArtifactType, EvaluationFinding, MetricBundle


def route_rubric(
    artifact_type: ArtifactType,
    metrics: list[MetricBundle],
    findings: list[EvaluationFinding],
    *,
    profile: str | None = None,
) -> dict[str, Any]:
    if artifact_type == "poster":
        return _poster_rubric(metrics, findings, profile=profile)
    if artifact_type in {"deck", "landing", "video"}:
        return {
            "artifact_type": artifact_type,
            "status": "stub",
            "score": None,
            "profile": profile,
            "summary": f"{artifact_type} rubric routing is present but not implemented yet.",
            "common_findings_count": len(findings),
        }
    return {"artifact_type": artifact_type, "status": "unknown", "score": None}


def _poster_rubric(
    metrics: list[MetricBundle],
    findings: list[EvaluationFinding],
    *,
    profile: str | None,
) -> dict[str, Any]:
    by_name = {bundle.name: bundle.metrics for bundle in metrics}
    score = 1.0
    score -= 0.25 * sum(1 for finding in findings if finding.severity == "P0")
    score -= 0.08 * sum(1 for finding in findings if finding.severity == "P1")
    score -= 0.02 * sum(1 for finding in findings if finding.severity == "P2")
    density = by_name.get("image_density", {})
    if density.get("available") and density.get("nonwhite_pixel_ratio", 0) < 0.12:
        score -= 0.1
    html = by_name.get("html_quality", {})
    score -= 0.1 * int(html.get("p0_count") or 0)
    numeric = by_name.get("numeric_token_exact_match", {})
    if numeric.get("available") and numeric.get("artifact_numeric_token_count", 0) >= 3:
        score -= min(0.18, 0.18 * (1.0 - float(numeric.get("exact_match_ratio") or 0.0)))
    score = max(0.0, round(score, 3))
    return {
        "artifact_type": "poster",
        "status": "initial_common_plus_basic_poster",
        "profile": profile,
        "score": score,
        "summary": (
            "Initial poster rubric over common integrity, preview density, "
            "HTML lint, text structure, and numeric grounding. Full poster "
            "quality scoring remains a follow-up migration from the mature "
            "poster harness."
        ),
    }
