"""Evaluation orchestration and report writing."""

from __future__ import annotations

from pathlib import Path

from autodesign.util.io import atomic_write_json

from .adapter import snapshot_artifact
from .metrics import (
    file_integrity_metrics,
    html_structure_metrics,
    html_quality_metrics,
    image_density_metrics,
    numeric_token_metrics,
    paper_integrity_metrics,
)
from .rubrics import route_rubric
from .schema import ArtifactType, EvaluationInput, EvaluationReport


def evaluate_artifact(
    artifact: Path,
    *,
    paper: Path | None,
    artifact_type: ArtifactType,
    out_dir: Path,
    profile: str | None = None,
) -> EvaluationReport:
    out_dir.mkdir(parents=True, exist_ok=True)
    input_obj = EvaluationInput(
        artifact=artifact.expanduser().resolve(),
        paper=paper.expanduser().resolve() if paper else None,
        artifact_type=artifact_type,
        out_dir=out_dir.expanduser().resolve(),
        profile=profile,
    )
    snapshot = snapshot_artifact(
        input_obj.artifact,
        input_obj.out_dir,
        artifact_type=input_obj.artifact_type,
    )
    metric_fns = (
        file_integrity_metrics,
        html_structure_metrics,
        html_quality_metrics,
    )
    metrics = []
    findings = []
    for fn in metric_fns:
        bundle, bundle_findings = fn(snapshot)
        metrics.append(bundle)
        findings.extend(bundle_findings)
    image_bundle, image_findings = image_density_metrics(snapshot, artifact_type=input_obj.artifact_type)
    metrics.append(image_bundle)
    findings.extend(image_findings)
    paper_bundle, paper_findings = paper_integrity_metrics(input_obj.paper)
    metrics.append(paper_bundle)
    findings.extend(paper_findings)
    numeric_bundle, numeric_findings = numeric_token_metrics(snapshot, input_obj.paper)
    metrics.append(numeric_bundle)
    findings.extend(numeric_findings)
    rubrics = route_rubric(input_obj.artifact_type, metrics, findings, profile=input_obj.profile)
    report = EvaluationReport(input_obj, snapshot, metrics, findings, rubrics)
    write_report_files(report, input_obj.out_dir)
    return report


def write_report_files(report: EvaluationReport, out_dir: Path) -> None:
    data = report.to_dict()
    atomic_write_json(out_dir / "evaluation_report.json", data)
    atomic_write_json(out_dir / "metrics.json", [m.to_dict() for m in report.metrics])
    atomic_write_json(out_dir / "findings.json", [f.to_dict() for f in report.findings])
    (out_dir / "report.md").write_text(_markdown_report(report), encoding="utf-8")


def _markdown_report(report: EvaluationReport) -> str:
    lines = [
        "# Evaluation Report",
        "",
        f"- Artifact: `{report.input.artifact}`",
        f"- Artifact type: `{report.input.artifact_type}`",
        f"- Paper: `{report.input.paper}`" if report.input.paper else "- Paper: none",
        f"- Snapshot kind: `{report.snapshot.artifact_kind}`",
        f"- Profile: `{report.input.profile}`" if report.input.profile else "- Profile: none",
        f"- Rubric status: `{report.rubrics.get('status')}`",
        f"- Rubric score: `{report.rubrics.get('score')}`",
        "",
        "## Metrics",
        "",
    ]
    for bundle in report.metrics:
        lines.append(f"- `{bundle.name}`: `{bundle.status}`")
    lines.extend(["", "## Findings", ""])
    if report.findings:
        for finding in report.findings:
            lines.append(f"- **{finding.severity}** `{finding.id}`: {finding.message}")
    else:
        lines.append("- No findings.")
    lines.append("")
    return "\n".join(lines)
