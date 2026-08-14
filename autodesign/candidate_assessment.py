"""Artifact delivery eligibility independent from strict quality acceptance."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from .schema import AttemptIssue, AttemptSafetyState


ArtifactKind = Literal["poster", "deck", "landing", "video"]

_QUALITY_ONLY_ISSUES = {
    "deck": frozenset({
        "empty_slide",
        "insufficient_substantive_slide_words",
        "insufficient_source_visual_placements",
        "insufficient_unique_source_visuals",
        "insufficient_visual_placements",
        "insufficient_visual_unit_slides",
        "invalid_slide_ids",
        "invalid_speaker_note_format",
        "missing_16_9_viewport",
        "missing_keyboard_navigation",
        "missing_speaker_notes",
        "slide_count_mismatch",
        "slides_browser_slide_count_mismatch",
        "slides_content_clipped",
        "source_visual_repeated_on_same_slide",
        "source_visual_reuse_cap_exceeded",
        "source_visual_missing_local_interpretation",
        "source_visual_not_visible",
        "source_visual_outside_slide",
        "slides_internal_overflow",
        "slides_visible_navigation_controls",
    }),
    "landing": frozenset({
        "landing_content_clipped",
        "landing_document_horizontal_overflow",
        "landing_horizontal_overflow",
        "landing_icon_control_missing_accessible_name",
        "landing_insufficient_content",
        "landing_insufficient_sections",
        "landing_insufficient_source_visual_density",
        "landing_javascript_reveal_dependency",
        "landing_missing_method_section",
        "landing_missing_results_section",
        "landing_missing_source_grounded_interaction",
        "landing_missing_title",
        "landing_motion_without_reduced_motion",
        "landing_source_evidence_missing",
        "landing_source_evidence_not_visible",
        "landing_source_visual_missing_id",
        "landing_source_visual_not_visible",
    }),
    "poster": frozenset({
        "paper_poster_editorial_lead_keys_missing",
        "paper_poster_editorial_lead_keys_overused",
        "paper_poster_html_block_out_of_bounds",
        "paper_poster_html_editorial_flow_fill_failed",
        "paper_poster_html_editorial_flow_shape_failed",
        "paper_poster_html_heading_flow_overflow",
        "paper_poster_html_local_flow_overflow",
        "paper_poster_html_narrow_math_container",
        "paper_poster_html_palette_id_missing",
        "paper_poster_html_palette_css_variable_mismatch",
        "paper_poster_html_palette_extra_authored_hex",
        "paper_poster_html_panel_content_contract_failed",
        "paper_poster_html_panel_flow_shape_failed",
        "paper_poster_html_post_overflow_density_conservation_failed",
        "paper_poster_html_reference_default_typography_leakage",
        "paper_poster_html_reference_lead_band_missing",
        "paper_poster_html_reference_section_divider_leakage",
        "paper_poster_html_reference_style_attribute_mismatch",
        "paper_poster_html_reference_style_contract_failed",
        "paper_poster_html_reference_vertical_rule_leakage",
        "paper_poster_html_required_palette_mismatch",
        "paper_poster_html_required_palette_validation_failed",
        "paper_poster_html_root_wrapper_padding_overflow",
        "paper_poster_html_row_allocation_density_regression",
        "paper_poster_html_severe_canvas_underfill",
        "paper_poster_html_severe_text_clipping",
        "paper_poster_html_severe_text_overlap",
        "paper_poster_html_source_coverage_low",
        "paper_poster_html_source_visible_caption",
        "paper_poster_html_source_visual_repair_regression",
        "paper_poster_html_source_visual_too_small",
        "paper_poster_html_source_wrap_missing",
        "paper_poster_html_typography_contract_failed",
    }),
    "video": frozenset(),
}


@dataclass(frozen=True)
class DeliveryAssessment:
    safety_state: AttemptSafetyState
    hard_blockers: tuple[AttemptIssue, ...]
    quality_diagnostics: tuple[AttemptIssue, ...]


def assess_delivery_issues(
    artifact_type: ArtifactKind | str,
    issues: Sequence[Mapping[str, Any]],
) -> DeliveryAssessment:
    """Classify known quality diagnostics while failing closed on everything else."""

    kind = str(artifact_type or "").strip().lower()
    hard_blockers: list[AttemptIssue] = []
    quality_diagnostics: list[AttemptIssue] = []
    hard_issue_ids: set[str] = set()
    quality_issue_ids: set[str] = set()
    for raw_issue in issues:
        issue_id = str(
            raw_issue.get("issue_id")
            or raw_issue.get("id")
            or "unknown_delivery_issue"
        ).strip() or "unknown_delivery_issue"
        issue = AttemptIssue(
            issue_id=issue_id,
            message=str(
                raw_issue.get("message")
                or raw_issue.get("reason")
                or issue_id
            ),
        )
        if _is_quality_only_issue(kind, issue_id, raw_issue):
            if issue_id not in quality_issue_ids:
                quality_issue_ids.add(issue_id)
                quality_diagnostics.append(issue)
        else:
            if issue_id not in hard_issue_ids:
                hard_issue_ids.add(issue_id)
                hard_blockers.append(issue)

    if hard_issue_ids:
        quality_diagnostics = [
            issue
            for issue in quality_diagnostics
            if issue.issue_id not in hard_issue_ids
        ]

    safety_state: AttemptSafetyState
    if hard_blockers:
        safety_state = "blocked"
    elif quality_diagnostics:
        safety_state = "ready_with_warnings"
    else:
        safety_state = "ready"
    return DeliveryAssessment(
        safety_state=safety_state,
        hard_blockers=tuple(hard_blockers),
        quality_diagnostics=tuple(quality_diagnostics),
    )


def _is_quality_only_issue(
    artifact_type: str,
    issue_id: str,
    issue: Mapping[str, Any],
) -> bool:
    evidence = issue.get("evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    declared_class = str(
        issue.get("delivery_class") or evidence.get("delivery_class") or ""
    ).strip().lower()
    if declared_class in {"hard", "required", "safety"}:
        return False
    if declared_class in {"quality", "polish"}:
        return True
    if issue_id in _QUALITY_ONLY_ISSUES.get(artifact_type, ()):
        return True
    if issue_id.startswith(("slides_required_palette_", "landing_required_palette_")):
        return True
    return False
