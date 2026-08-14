"""Visual-reference quality-loop contract helpers.

The image-conditioned visual reference pass is a quality gate for substantial
new artifacts, but the generated PNG is advisory only. These helpers keep the
gate state provider-neutral and planner-facing without coupling the runner to
the tool implementation.
"""

from __future__ import annotations

from typing import Any

from ..config import effective_poster_harness_mode

BACKEND_EXEMPT_ERROR_CATEGORIES = {"provider_unavailable", "safety_filter", "api"}
_SUPPORTED_ARTIFACTS = {"poster", "landing", "deck"}
_PROGRESSION_FINDING_IDS = {
    "visual-reference-not-attempted",
    "visual-reference-revision-required",
    "visual-reference-revision-not-composited",
}


def record_visual_reference_attempt(
    ctx: Any,
    *,
    status: str,
    error_category: str | None = None,
    message: str | None = None,
) -> None:
    ctx.state["visual_reference_attempted"] = True
    ctx.state["visual_reference_status"] = status
    if error_category:
        ctx.state["visual_reference_error_category"] = error_category
    else:
        ctx.state.pop("visual_reference_error_category", None)
    if message:
        ctx.state["visual_reference_error_message"] = message
    else:
        ctx.state.pop("visual_reference_error_message", None)


def build_visual_reference_contract(
    payload: dict[str, Any],
    *,
    ctx: Any,
) -> dict[str, Any]:
    artifact_type = str(payload.get("artifact_type") or _spec_artifact_type(ctx) or "unknown")
    required, reason = _visual_reference_required(
        artifact_type=artifact_type,
        payload=payload,
        ctx=ctx,
    )
    attempted = bool(ctx.state.get("visual_reference_attempted") or ctx.state.get("visual_reference"))
    visual_reference = ctx.state.get("visual_reference")
    status = str(ctx.state.get("visual_reference_status") or "")
    if not status:
        status = "success" if isinstance(visual_reference, dict) and visual_reference.get("visual_reference_paths") else "not_attempted"
    error_category = ctx.state.get("visual_reference_error_category")
    backend_exempt = bool(error_category in BACKEND_EXEMPT_ERROR_CATEGORIES)
    revision_required = bool(ctx.state.get("visual_reference_revision_required"))
    revision_spec_revision = ctx.state.get("visual_reference_revision_spec_revision")
    revision_composited = bool(ctx.state.get("visual_reference_revision_composited"))

    findings: list[dict[str, Any]] = []
    if required:
        if not attempted:
            findings.append({
                "severity": "P0",
                "id": "visual-reference-not-attempted",
                "message": (
                    "This substantial new artifact requires one visual-reference "
                    "pass after the first composite before finalize."
                ),
                "fix": (
                    "Call generate_visual_reference, revise the editable DesignSpec "
                    "using the style_anchor/layout guidance, then call composite again."
                ),
                "status": status,
            })
        elif status != "success" and not backend_exempt:
            findings.append({
                "severity": "P0",
                "id": "visual-reference-attempt-failed",
                "message": "Visual-reference generation was attempted but did not produce a usable reference.",
                "fix": (
                    "Resolve the visual-reference failure or retry generate_visual_reference; "
                    "only provider_unavailable, safety_filter, and api errors may skip the gate."
                ),
                "status": status,
                "error_category": error_category,
                "error_message": ctx.state.get("visual_reference_error_message"),
            })
        elif status == "success" and revision_required:
            findings.append({
                "severity": "P0",
                "id": "visual-reference-revision-required",
                "message": "Visual references were generated successfully but the editable artifact has not been revised from them.",
                "fix": (
                    "Revise the editable DesignSpec with apply_design_ops for local "
                    "layout/style fixes, or propose_design_spec for structural rewrites, "
                    "then call composite again."
                ),
                "visual_reference_iteration": ctx.state.get("visual_reference_revision_iteration"),
                "source_spec_revision": ctx.state.get("visual_reference_revision_source_spec_revision"),
            })
        elif status == "success" and revision_spec_revision is not None and not revision_composited:
            findings.append({
                "severity": "P0",
                "id": "visual-reference-revision-not-composited",
                "message": "The visual-reference-guided spec revision has not been composited yet.",
                "fix": "Call composite so final artifacts use the revised editable spec.",
                "revision_spec_revision": revision_spec_revision,
            })
        elif backend_exempt:
            findings.append({
                "severity": "P2",
                "id": "visual-reference-backend-exempt",
                "message": (
                    "Visual-reference generation was skipped because the configured "
                    "backend returned a typed limitation."
                ),
                "fix": "Continue the normal editable pipeline and surface the backend limitation in the run report.",
                "status": status,
                "error_category": error_category,
                "error_message": ctx.state.get("visual_reference_error_message"),
            })

    p0_count = sum(1 for finding in findings if str(finding.get("severity")).upper() == "P0")
    return {
        "visual_reference_required": required,
        "visual_reference_exempt_reason": None if required else reason,
        "visual_reference_attempted": attempted,
        "visual_reference_status": status if required or attempted else "not_required",
        "visual_reference_error_category": error_category,
        "visual_reference_revision_required": revision_required,
        "visual_reference_revision_composited": revision_composited,
        "visual_reference_findings": findings,
        "visual_reference_p0_count": p0_count,
    }


def visual_reference_summary(ctx: Any) -> dict[str, Any]:
    payload = ctx.state.get("last_composite_payload")
    if isinstance(payload, dict) and "visual_reference_required" in payload:
        return {
            key: payload.get(key)
            for key in (
                "visual_reference_required",
                "visual_reference_attempted",
                "visual_reference_status",
                "visual_reference_error_category",
                "visual_reference_revision_required",
                "visual_reference_revision_composited",
                "visual_reference_p0_count",
            )
        }
    return build_visual_reference_contract(payload if isinstance(payload, dict) else {}, ctx=ctx)


def only_visual_reference_progression_findings(findings: list[dict[str, Any]]) -> bool:
    """True when every blocker is an actionable visual-reference loop step."""
    if not findings:
        return False
    for finding in findings:
        if not isinstance(finding, dict) or finding.get("source") != "visual_reference":
            return False
        finding_id = str(finding.get("id") or "")
        if not any(finding_id.endswith(f":{raw_id}") for raw_id in _PROGRESSION_FINDING_IDS):
            return False
    return True


def _visual_reference_required(
    *,
    artifact_type: str,
    payload: dict[str, Any],
    ctx: Any,
) -> tuple[bool, str]:
    if bool(ctx.state.get("visual_reference_contract_disabled")):
        return False, "disabled"
    if artifact_type not in _SUPPORTED_ARTIFACTS:
        return False, "unsupported_artifact"
    harness_mode = effective_poster_harness_mode(getattr(ctx, "settings", None))
    if (
        artifact_type == "poster"
        and harness_mode == "cheap"
        and isinstance(ctx.state.get("poster_plan_contract"), dict)
    ):
        return False, "cheap_mode_paper_poster"
    brief = str(ctx.state.get("visual_reference_brief") or ctx.state.get("run_brief") or "")
    if _explicit_text_only(brief):
        return False, "text_only"
    if _looks_like_tiny_artifact(artifact_type, payload):
        return False, "tiny_artifact"
    if _looks_like_pure_repair(brief):
        return False, "pure_repair"
    return True, "required"


def _spec_artifact_type(ctx: Any) -> str | None:
    spec = ctx.state.get("design_spec")
    artifact_type = getattr(spec, "artifact_type", None)
    value = getattr(artifact_type, "value", artifact_type)
    return str(value) if value else None


def _explicit_text_only(brief: str) -> bool:
    text = brief.lower()
    markers = (
        "text-only", "text only", "no images", "without images", "no visuals",
        "不要图片", "不需要图片", "纯文字", "只要文字",
    )
    for marker in markers:
        start = 0
        while True:
            idx = text.find(marker, start)
            if idx < 0:
                break
            if not _negates_text_only_marker(text, idx):
                return True
            start = idx + len(marker)
    return False


def _negates_text_only_marker(text: str, marker_start: int) -> bool:
    prefix = text[max(0, marker_start - 48):marker_start]
    negators = (
        "do not make it ",
        "don't make it ",
        "dont make it ",
        "never make it ",
        "should not be ",
        "must not be ",
        "not make it ",
        "not be ",
        "not a ",
        "avoid ",
        "not ",
        "不要做成",
        "不要做",
        "不要变成",
        "不能是",
        "不是",
    )
    return any(prefix.endswith(negator) for negator in negators)


def _looks_like_tiny_artifact(artifact_type: str, payload: dict[str, Any]) -> bool:
    if artifact_type == "poster":
        return int(payload.get("n_layers") or 0) <= 1
    if artifact_type == "landing":
        return int(payload.get("n_sections") or 0) <= 1 and int(payload.get("n_images") or 0) == 0
    if artifact_type == "deck":
        return int(payload.get("n_slides") or payload.get("slides") or 0) <= 1
    return False


def _looks_like_pure_repair(brief: str) -> bool:
    if not brief:
        return False
    has_prior_context = (
        "## Prior artifact in this chat session" in brief
        or "[Conversation context" in brief
    )
    if not has_prior_context:
        return False
    request = _extract_current_request(brief)
    if len(request) > 220:
        return False
    lowered = request.lower()
    new_markers = (
        "new artifact", "from scratch", "different project", "different topic",
        "make a poster for", "make a landing", "landing page", "slide deck",
        "make slides", "做一个新的", "新的", "换一个主题",
    )
    if any(marker in lowered for marker in new_markers):
        return False
    repair_markers = (
        "bigger", "smaller", "darker", "lighter", "move", "resize", "change",
        "fix", "adjust", "try", "make it", "make the", "修改", "调整",
        "换成", "大一点", "小一点", "深一点", "浅一点",
    )
    return any(marker in lowered for marker in repair_markers)


def _extract_current_request(brief: str) -> str:
    for marker in ("## User's next request", "[User's current request:]"):
        idx = brief.rfind(marker)
        if idx >= 0:
            return brief[idx + len(marker):].strip()
    return brief.strip()
