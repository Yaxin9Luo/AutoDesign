"""Schema objects for the poster-quality benchmark reports.

Kept separate from ``schema.py`` (the deterministic artifact-evaluator types) so
this rubric-scored report can evolve without churning the common metric contract.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from .protocol import EVAL_PROTOCOL, EVALUATOR_FINGERPRINT


QUALITY_SCHEMA_VERSION = "0.1.0"
EvalMode = Literal["dry_run", "deterministic", "benchmark"]
Verdict = Literal["pass", "revise", "fail", "incomplete"]
FindingSeverity = Literal["P0", "P1", "P2"]


@dataclass
class RubricDimensionScore:
    id: str
    weight: float
    owner: str
    score_0_10: float | None = None
    normalized: float | None = None
    source: str = "placeholder"  # tools | judge | blend | placeholder
    status: str = "ok"  # ok | warning | degraded | needs_judge | skipped | error
    rationale: str = ""
    visible_evidence: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PosterQualityFinding:
    id: str
    severity: FindingSeverity
    message: str
    dimension: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PosterQualityReport:
    candidate_name: str
    artifact: str
    paper: str | None
    mode: EvalMode
    profile: str | None = None
    overall_score_0_100: float | None = None
    gate_ceiling: float | None = None
    gate_triggered: bool = False
    verdict: Verdict = "incomplete"
    dimensions: list[RubricDimensionScore] = field(default_factory=list)
    findings: list[PosterQualityFinding] = field(default_factory=list)
    executive_summary: str = ""
    deterministic_report_path: str | None = None
    judge_report_path: str | None = None
    version: str = QUALITY_SCHEMA_VERSION
    eval_protocol: str = EVAL_PROTOCOL
    evaluator_fingerprint: str = EVALUATOR_FINGERPRINT
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "eval_protocol": self.eval_protocol,
            "evaluator_fingerprint": self.evaluator_fingerprint,
            "generated_at": self.generated_at,
            "mode": self.mode,
            "candidate_name": self.candidate_name,
            "artifact": self.artifact,
            "paper": self.paper,
            "profile": self.profile,
            "overall_score_0_100": self.overall_score_0_100,
            "gate_ceiling": self.gate_ceiling,
            "gate_triggered": self.gate_triggered,
            "verdict": self.verdict,
            "dimensions": [d.to_dict() for d in self.dimensions],
            "findings": [f.to_dict() for f in self.findings],
            "finding_counts": finding_counts(self.findings),
            "executive_summary": self.executive_summary,
            "deterministic_report_path": self.deterministic_report_path,
            "judge_report_path": self.judge_report_path,
        }


def finding_counts(findings: list[PosterQualityFinding]) -> dict[str, int]:
    counts = {"P0": 0, "P1": 0, "P2": 0, "total": len(findings)}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    return counts
