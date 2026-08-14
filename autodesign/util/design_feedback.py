"""Normalize composite-stage environment feedback.

The composite tool historically returned separate payload keys for browser
layout grounding, quality lint, paper-density checks, deck export warnings, and
other harness signals. This module folds those source-specific records into a
single planner/runner-facing ``DesignFeedback`` contract while preserving every
legacy key on the original payload.
"""

from __future__ import annotations

import hashlib
from typing import Any

from ..schema import DesignFeedback, DesignFeedbackFinding


def build_design_feedback(
    payload: dict[str, Any],
    *,
    artifact_type: str | None = None,
    iteration: int | None = None,
) -> DesignFeedback:
    """Return normalized feedback for a composite payload."""
    artifact = str(artifact_type or payload.get("artifact_type") or "unknown")
    iter_num = _safe_int(iteration if iteration is not None else payload.get("iteration"), 0)
    findings: list[DesignFeedbackFinding] = []

    for idx, issue in enumerate(_as_list(payload.get("layout_grounding_issues"))):
        record = _as_dict(issue)
        raw_severity = str(record.get("severity") or "medium").lower()
        severity = "blocker" if raw_severity in {"blocker", "high"} else _severity(raw_severity)
        findings.append(_finding(
            artifact_type=artifact,
            source="layout_grounding",
            idx=idx,
            raw=record,
            severity=severity,
            default_message="Browser layout grounding found an issue.",
            target=_target_from(record),
            evidence=record,
            suggested_action=(
                record.get("suggested_action")
                or record.get("suggested_fix")
                or "Revise the affected layer geometry, re-run composite, then finalize."
            ),
            repairable=True,
        ))

    for idx, finding in enumerate(_as_list(payload.get("quality_lint_findings"))):
        record = _as_dict(finding)
        findings.append(_finding(
            artifact_type=artifact,
            source="quality_lint",
            idx=idx,
            raw=record,
            severity=_p_severity(record.get("severity")),
            default_message="Deterministic quality lint found a visual quality issue.",
            target=_target_from(record),
            evidence=_compact(record, exclude={"message", "fix", "suggested_action"}),
            suggested_action=(
                record.get("fix")
                or record.get("suggested_action")
                or "Revise the HTML/spec to remove the linted pattern, then composite again."
            ),
            repairable=True,
        ))

    for idx, finding in enumerate(_as_list(payload.get("paper_density_findings"))):
        record = _as_dict(finding)
        findings.append(_finding(
            artifact_type=artifact,
            source="paper_density",
            idx=idx,
            raw=record,
            severity=_p_severity(record.get("severity")),
            default_message="Paper poster density audit found a problem.",
            target=_target_from(record),
            evidence=_compact(record, exclude={"message", "fix", "suggested_action"}),
            suggested_action=(
                record.get("fix")
                or record.get("suggested_action")
                or "Add/reposition source figures or tables and reduce low-value body copy."
            ),
            repairable=True,
        ))

    for idx, finding in enumerate(_as_list(payload.get("visual_reference_findings"))):
        record = _as_dict(finding)
        findings.append(_finding(
            artifact_type=artifact,
            source="visual_reference",
            idx=idx,
            raw=record,
            severity=_p_severity(record.get("severity")),
            default_message="Visual-reference quality loop requires planner action.",
            target=_target_from(record),
            evidence=_compact(record, exclude={"message", "fix", "suggested_action"}),
            suggested_action=(
                record.get("fix")
                or record.get("suggested_action")
                or "Run the visual-reference loop, revise editable layers, then composite again."
            ),
            repairable=True,
        ))

    for idx, finding in enumerate(_as_list(payload.get("paper_information_findings"))):
        record = _as_dict(finding)
        findings.append(_finding(
            artifact_type=artifact,
            source="paper_information",
            idx=idx,
            raw=record,
            severity=_p_severity(record.get("severity")),
            default_message="Paper poster information-density audit found a problem.",
            target=_target_from(record),
            evidence=_compact(record, exclude={"message", "fix", "suggested_action"}),
            suggested_action=(
                record.get("fix")
                or record.get("suggested_action")
                or (
                    "Add sourced figure captions, method callouts, evidence "
                    "bullets, or compact conclusion/limitation text."
                )
            ),
            repairable=True,
        ))

    for idx, finding in enumerate(_as_list(payload.get("paper_poster_dom_findings"))):
        record = _as_dict(finding)
        findings.append(_finding(
            artifact_type=artifact,
            source="paper_poster_dom",
            idx=idx,
            raw=record,
            severity=_p_severity(record.get("severity")),
            default_message="Authored paper-poster DOM audit found a rendering issue.",
            target=_target_from(record),
            evidence=_compact(record, exclude={"message", "fix", "suggested_action"}),
            suggested_action=(
                record.get("fix")
                or record.get("suggested_action")
                or "Revise authored_body_html/authored_css and composite again."
            ),
            repairable=True,
        ))

    for idx, finding in enumerate(_as_list(payload.get("poster_gate_findings"))):
        record = _as_dict(finding)
        findings.append(_finding(
            artifact_type=artifact,
            source="poster_gate",
            idx=idx,
            raw=record,
            severity=_p_severity(record.get("severity")),
            default_message="Poster gate audit found a print/layout issue.",
            target=_target_from(record),
            evidence=_compact(record, exclude={"message", "fix", "suggested_action"}),
            suggested_action=(
                record.get("fix")
                or record.get("suggested_action")
                or "Revise authored_body_html/authored_css and composite again."
            ),
            repairable=True,
        ))

    for idx, finding in enumerate(_as_list(payload.get("poster_contract_findings"))):
        record = _as_dict(finding)
        findings.append(_finding(
            artifact_type=artifact,
            source="poster_contract",
            idx=idx,
            raw=record,
            severity=_p_severity(record.get("severity")),
            default_message="Paper poster plan contract found a conformance issue.",
            target=_target_from(record),
            evidence=_compact(record, exclude={"message", "fix", "suggested_action"}),
            suggested_action=(
                record.get("fix")
                or record.get("suggested_action")
                or "Revise the paper-poster content, visual curation, or storyboard contract."
            ),
            repairable=True,
        ))

    for idx, finding in enumerate(_as_list(payload.get("authored_generation_contract_findings"))):
        record = _as_dict(finding)
        findings.append(_finding(
            artifact_type=artifact,
            source="authored_generation_contract",
            idx=idx,
            raw=record,
            severity=_p_severity(record.get("severity")),
            default_message="Authored HTML does not yet satisfy the generator-side poster contract.",
            target=_target_from(record),
            evidence=_compact(record, exclude={"message", "fix", "suggested_action"}),
            suggested_action=(
                record.get("fix")
                or record.get("suggested_action")
                or (
                    "Revise the named data-slot-id panel with more paper-specific "
                    "leaf text, native units, and local source-visual bindings."
                )
            ),
            repairable=True,
        ))

    for idx, finding in enumerate(_as_list(payload.get("deck_layout_findings"))):
        record = _as_dict(finding)
        findings.append(_finding(
            artifact_type=artifact,
            source="deck_layout",
            idx=idx,
            raw=record,
            severity=_p_severity(record.get("severity")),
            default_message="Deck HTML layout contract found a presentation-quality issue.",
            target=_target_from(record),
            evidence=_compact(record, exclude={"message", "fix", "suggested_action"}),
            suggested_action=(
                record.get("fix")
                or record.get("suggested_action")
                or "Revise deck_html layouts, block positions, image scale, and type hierarchy."
            ),
            repairable=True,
        ))

    for idx, finding in enumerate(_as_list(payload.get("frame_layout_findings"))):
        record = _as_dict(finding)
        findings.append(_finding(
            artifact_type=artifact,
            source="frame_layout",
            idx=idx,
            raw=record,
            severity=_p_severity(record.get("severity")),
            default_message="Spatial storyboard contract found a layout-plan issue.",
            target=_target_from(record),
            evidence=_compact(record, exclude={"message", "fix", "suggested_action"}),
            suggested_action=(
                record.get("fix")
                or record.get("suggested_action")
                or "Revise layout_plan slots/groups first, then adjust child blocks."
            ),
            repairable=True,
        ))

    direct_contract_ids = _direct_payload_finding_ids(payload)
    for idx, finding in enumerate(_as_list(payload.get("html_artifact_contract_findings"))):
        record = _as_dict(finding)
        raw_id = str(record.get("id") or "")
        if raw_id in direct_contract_ids:
            continue
        if raw_id.startswith("frame_layout_") or str(record.get("target") or "") == "frame_layout":
            continue
        findings.append(_finding(
            artifact_type=artifact,
            source="html_artifact_contract",
            idx=idx,
            raw=record,
            severity=_p_severity(record.get("severity")),
            default_message="Unified HTML artifact contract found a target-specific issue.",
            target=_target_from(record),
            evidence=_compact(record, exclude={"message", "fix", "suggested_action"}),
            suggested_action=(
                record.get("fix")
                or record.get("suggested_action")
                or "Revise html_artifact frames/blocks and composite again."
            ),
            repairable=True,
        ))

    for idx, warning in enumerate(_as_list(payload.get("text_overlap_warnings"))):
        record = _as_dict(warning)
        findings.append(_finding(
            artifact_type=artifact,
            source="text_overlap",
            idx=idx,
            raw=record,
            severity=_severity(record.get("severity") or "high"),
            default_message="Text overlaps another rendered layer.",
            target=_target_from(record),
            evidence=record,
            suggested_action="Move, resize, or shorten the overlapping layers and composite again.",
            repairable=True,
        ))

    for idx, layer_id in enumerate(_as_list(payload.get("xref_misses"))):
        record = _as_dict(layer_id)
        if not record:
            record = {"layer_id": str(layer_id)}
        findings.append(_finding(
            artifact_type=artifact,
            source="xref_miss",
            idx=idx,
            raw=record,
            severity="medium",
            default_message=f"Placed paper visual {record.get('layer_id', '?')} is not referenced in body copy.",
            target=_target_from(record),
            evidence=record,
            suggested_action="Add a nearby local source readout or remove the unreferenced visual.",
            repairable=True,
        ))

    for source_key, source_name, severity, default_message, suggested_action in (
        (
            "orphan_callout_warnings",
            "orphan_callout",
            "high",
            "A deck callout could not be anchored and was dropped.",
            "Fix the callout anchor_layer_id/region or remove the callout.",
        ),
        (
            "sanitizer_warnings",
            "export_sanitizer",
            "high",
            "Export sanitizer removed placeholder or unsafe deck content.",
            "Replace placeholder/debug text with real content and composite again.",
        ),
        (
            "alignment_warnings",
            "slide_alignment",
            "high",
            "Slide title/body alignment appears semantically mismatched.",
            "Revise slide content so title, body, and evidence support the same point.",
        ),
        (
            "closing_warnings",
            "closing_stub",
            "high",
            "Closing slide is thin or still contains template-style placeholder content.",
            "Populate the closing slide with concrete takeaways, implications, or next steps.",
        ),
    ):
        for idx, warning in enumerate(_as_list(payload.get(source_key))):
            record = _as_dict(warning)
            findings.append(_finding(
                artifact_type=artifact,
                source=source_name,
                idx=idx,
                raw=record,
                severity=_severity(record.get("severity") or severity),
                default_message=default_message,
                target=_target_from(record),
                evidence=record,
                suggested_action=record.get("suggested_action") or suggested_action,
                repairable=True,
            ))

    counts = _counts(findings)
    return DesignFeedback(
        artifact_type=artifact,
        iteration=iter_num,
        findings=findings,
        counts=counts,
        has_blocking_findings=counts.get("blocker", 0) > 0,
    )


def coerce_design_feedback(value: Any) -> DesignFeedback | None:
    """Parse a model/dict feedback value if possible."""
    if value is None:
        return None
    if isinstance(value, DesignFeedback):
        return value
    if isinstance(value, dict):
        try:
            return DesignFeedback.model_validate(value)
        except Exception:
            return None
    return None


def design_feedback_to_dict(value: Any) -> dict[str, Any] | None:
    feedback = coerce_design_feedback(value)
    if feedback is None:
        return None
    return feedback.model_dump(mode="json")


def blocking_design_findings(value: Any) -> list[dict[str, Any]]:
    """Return blocker findings as JSON-serializable dicts."""
    feedback = coerce_design_feedback(value)
    if feedback is None:
        return []
    return [
        finding.model_dump(mode="json")
        for finding in feedback.findings
        if finding.severity == "blocker"
    ]


def _finding(
    *,
    artifact_type: str,
    source: str,
    idx: int,
    raw: dict[str, Any],
    severity: str,
    default_message: str,
    target: dict[str, Any],
    evidence: dict[str, Any],
    suggested_action: str,
    repairable: bool,
) -> DesignFeedbackFinding:
    raw_id = raw.get("id") or raw.get("rule_id") or raw.get("kind") or raw.get("reason")
    finding_id = _id(source, idx, raw_id, raw)
    return DesignFeedbackFinding(
        id=finding_id,
        source=source,
        severity=_severity(severity),
        artifact_type=artifact_type,
        message=str(raw.get("message") or raw.get("description") or default_message),
        target=target,
        evidence=evidence,
        suggested_action=str(suggested_action or ""),
        stage=_stage(raw.get("stage")),
        repair_route=_repair_route(raw.get("repair_route")),
        repairable=bool(raw.get("repairable", repairable)),
    )


def _counts(findings: list[DesignFeedbackFinding]) -> dict[str, int]:
    counts = {
        "total": len(findings),
        "blocker": 0,
        "high": 0,
        "medium": 0,
        "low": 0,
    }
    for finding in findings:
        counts[finding.severity] += 1
    return counts


def _id(source: str, idx: int, raw_id: Any, raw: dict[str, Any]) -> str:
    if raw_id:
        clean = str(raw_id).strip().replace(" ", "-").lower()
        return f"{source}:{clean}"
    digest = hashlib.sha1(repr(sorted(raw.items())).encode("utf-8")).hexdigest()[:8]
    return f"{source}:{idx + 1:02d}:{digest}"


def _p_severity(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if raw == "P0":
        return "blocker"
    if raw == "P1":
        return "high"
    if raw == "P2":
        return "medium"
    return _severity(value or "medium")


def _severity(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"blocker", "high", "medium", "low"}:
        return raw
    if raw in {"major", "p1", "error", "warning"}:
        return "high"
    if raw in {"minor", "p2", "info"}:
        return "medium"
    if raw in {"p3", "debug"}:
        return "low"
    return "medium"


def _stage(value: Any) -> str | None:
    raw = str(value or "").strip()
    if raw in {
        "content_strategy",
        "visual_curation",
        "layout_storyboard",
        "typography_system",
        "rendering_export",
    }:
        return raw
    return None


def _repair_route(value: Any) -> str | None:
    raw = str(value or "").strip()
    if raw in {
        "local_refine",
        "pivot_layout_archetype",
        "revise_content_strategy",
        "revise_visual_curation",
        "revise_typography_system",
        "revise_authored_html",
        "shrink_text",
        "swap_visual",
        "resize_visual",
        "adjust_size_or_archetype",
        "none",
    }:
        return raw
    return None


def _target_from(record: dict[str, Any]) -> dict[str, Any]:
    target: dict[str, Any] = {}
    for key in (
        "artifact_type", "slide_id", "layer_id", "layer_a", "layer_b",
        "text_layer", "shape_layer", "callout_layer_id", "anchor_layer_id",
        "selector", "node", "element_id", "block_id", "slot_id",
        "visual_id", "section_id", "role",
    ):
        value = record.get(key)
        if value not in (None, ""):
            target[key] = value
    nested = record.get("target")
    if isinstance(nested, dict):
        target.update({str(k): v for k, v in nested.items() if v not in (None, "")})
    return target


def _compact(record: dict[str, Any], *, exclude: set[str]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k not in exclude}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _direct_payload_finding_ids(payload: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for key in (
        "quality_lint_findings",
        "paper_density_findings",
        "paper_information_findings",
        "paper_poster_dom_findings",
        "poster_contract_findings",
        "deck_layout_findings",
        "visual_reference_findings",
    ):
        for finding in _as_list(payload.get(key)):
            record = _as_dict(finding)
            raw_id = str(record.get("id") or "").strip()
            if raw_id:
                out.add(raw_id)
    return out


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
