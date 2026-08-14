"""Shared schema objects for independent artifact evaluation.

The evaluator report shape is intentionally artifact-neutral. Poster, deck,
landing, and video rubrics can add their own payloads later without changing the
common adapter/metric contract.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal


ArtifactType = Literal["poster", "deck", "landing", "video"]
FindingSeverity = Literal["P0", "P1", "P2"]
EVALUATOR_SCHEMA_VERSION = "0.1.0"


@dataclass(frozen=True)
class EvaluationInput:
    artifact: Path
    paper: Path | None
    artifact_type: ArtifactType
    out_dir: Path
    profile: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return {k: str(v) if isinstance(v, Path) else v for k, v in data.items()}


@dataclass
class ArtifactSnapshot:
    artifact_path: str
    artifact_kind: str
    resolved_files: dict[str, str] = field(default_factory=dict)
    text: str = ""
    html: str | None = None
    preview_image: str | None = None
    dom_layers: list[dict[str, Any]] = field(default_factory=list)
    width: int | None = None
    height: int | None = None
    page_count: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MetricBundle:
    name: str
    metrics: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"  # ok | warning | degraded | skipped | error
    findings_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvaluationFinding:
    id: str
    severity: FindingSeverity
    message: str
    metric: str | None = None
    category: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvaluationReport:
    input: EvaluationInput
    snapshot: ArtifactSnapshot
    metrics: list[MetricBundle]
    findings: list[EvaluationFinding]
    rubrics: dict[str, Any] = field(default_factory=dict)
    version: str = EVALUATOR_SCHEMA_VERSION
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "generated_at": self.generated_at,
            "input": self.input.to_dict(),
            "snapshot": self.snapshot.to_dict(),
            "metrics": [m.to_dict() for m in self.metrics],
            "findings": [f.to_dict() for f in self.findings],
            "rubrics": dict(self.rubrics),
            "finding_counts": finding_counts(self.findings),
        }


def finding_counts(findings: list[EvaluationFinding]) -> dict[str, int]:
    counts = {"P0": 0, "P1": 0, "P2": 0, "total": len(findings)}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    return counts
