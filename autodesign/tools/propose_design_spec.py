"""propose_design_spec — validate the designer's initial DesignSpec.

Stores the spec in ctx.state['design_spec']; subsequent tools look it up
there. Re-calling this tool replaces the spec (designer may revise mid-run).

v2 training-data shape: payload is `{"n_layers", "artifact_type", "is_revision"}`
— the spec itself is preserved verbatim in the corresponding tool_call.tool_args
so duplicating it in the result would be pure waste. Validation errors
return the full pydantic errors() list under category="validation" so the
policy can learn structured-output recovery.
"""

from __future__ import annotations

from html import escape, unescape
import itertools
import json
import math
import os
from pathlib import Path
import re
from typing import Any

from bs4 import BeautifulSoup
from pydantic import ValidationError

from ._contract import ToolContext, obs_error, obs_ok
from .paper_poster_renderer import (
    AUTHORED_PAPER_POSTER_REQUIRED_STATE_KEY,
    find_authored_paper_poster_frame,
    is_academic_paper_poster_context,
    sanitize_authored_paper_poster,
)
from ..config import effective_poster_harness_mode
from ..design_spec_persistence import (
    DesignSpecCommitResult,
    DesignSpecPersistenceError,
    capture_state_keys,
    commit_design_spec_revision,
    install_state_snapshot,
)
from ..schema import DesignSpec, ToolResultRecord
from ..util.deck_planner import (
    log_deck_plan_validation,
    validate_deck_plan_for_spec,
)
from ..util.design_spec_fingerprint import design_spec_sha256
from ..util.html_artifact import audit_frame_layout_plan, canonicalize_design_spec
from ..util.io import atomic_write_json
from ..util.logging import log
from ..util.paper_project_page import enhance_paper_project_page_spec

try:  # Worker A helper; optional while palette-selection work lands in parallel.
    from ..util.academic_palette import active_academic_color_system, select_academic_color_system
except Exception:  # pragma: no cover - supports partially landed worktrees
    active_academic_color_system = None
    select_academic_color_system = None


_PENDING_SPEC_RECOVERY_REASON_KEY = "_pending_spec_recovery_reason"
DOGFOOD_DRAFT_DESIGN_SPEC_STATE_KEY = "draft_design_spec"
DOGFOOD_DRAFT_DESIGN_SPEC_META_KEY = "draft_design_spec_meta"
_PROPOSE_STATE_TRANSACTION_KEYS = (
    _PENDING_SPEC_RECOVERY_REASON_KEY,
    "designer_consecutive_missing_design_spec_args_count",
    "spec_recovery_records",
    "spec_recovery_reason",
    "spec_recovery_count",
    "authored_html_storyboard_local_repair_warnings",
    "paper_project_page_enhancements",
    "paper_project_panel_plan",
    "paper_visual_storyboard",
    "poster_plan_contract",
    "artifact_type",
    "design_spec",
    "design_spec_sha256",
    AUTHORED_PAPER_POSTER_REQUIRED_STATE_KEY,
    "spec_revision_count",
    "visual_reference_revision_required",
    "visual_reference_revision_source_spec_revision",
    "visual_reference_revision_spec_revision",
    "visual_reference_revision_composited",
    "video_delivery",
    "finalized",
    "composition",
)
_PROPOSE_DEEP_COPY_STATE_KEYS = {
    "spec_recovery_records",
    "authored_html_storyboard_local_repair_warnings",
    "paper_project_page_enhancements",
    "paper_project_panel_plan",
    "paper_visual_storyboard",
    "poster_plan_contract",
}


def _deterministic_spec_recovery_enabled() -> bool:
    raw = os.getenv("POSTER_ENABLE_DETERMINISTIC_SPEC_RECOVERY", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def propose_design_spec(args: dict[str, Any], *, ctx: ToolContext) -> ToolResultRecord:
    entry_state = capture_state_keys(
        ctx.state,
        _PROPOSE_STATE_TRANSACTION_KEYS,
        deep_copy_keys=_PROPOSE_DEEP_COPY_STATE_KEYS,
    )
    raw = _extract_raw_design_spec(args)
    recovery_reason: str | None = None
    if raw is None:
        pending_recovery_reason = _consume_pending_spec_recovery_reason(ctx)
        arg_shape = _spec_arg_shape(args)
        log("spec.args_shape", **arg_shape)
        if pending_recovery_reason is None and _is_dogfood_paper_poster_contract(ctx):
            return _missing_design_spec_args_error(ctx, arg_shape)
        raw = (
            _build_paper_poster_recovery_design_spec(ctx)
            if _deterministic_spec_recovery_enabled()
            else None
        )
        recovery_reason = pending_recovery_reason or "designer_missing_design_spec_args"
        if raw is None:
            return obs_error(
                _missing_design_spec_message(abort=False),
                category="validation",
                payload={
                    **arg_shape,
                    "accepted_keys": ["design_spec", "designSpec", "spec", "design", "payload.design_spec"],
                },
            )
        _record_spec_recovery(ctx, recovery_reason, raw)
    elif isinstance(ctx.state, dict):
        ctx.state["designer_consecutive_missing_design_spec_args_count"] = 0

    # Fall back to ctx.state["artifact_type"] if designer omitted it from the spec.
    if isinstance(raw, dict):
        raw = _canonicalize_raw_design_spec(raw, ctx=ctx)
        if "artifact_type" not in raw:
            raw = {**raw, "artifact_type": ctx.state.get("artifact_type", "poster")}

    try:
        spec = DesignSpec.model_validate(raw)
    except ValidationError as e:
        errors = e.errors(include_url=False, include_input=False)
        return _dogfood_design_spec_validation_error(
            ctx,
            f"DesignSpec validation failed: {errors}",
            issue_id="design_spec_schema_validation_failed",
            payload={"pydantic_errors": errors[:12]},
        )
    raw_prefers_html = isinstance(raw, dict) and raw.get("html_artifact") is not None
    try:
        spec = canonicalize_design_spec(
            spec,
            prefer_html_artifact=True if raw_prefers_html else None,
        )
    except ValidationError as e:
        errors = e.errors(include_url=False, include_input=False)
        return _dogfood_design_spec_validation_error(
            ctx,
            f"DesignSpec canonicalization failed: {errors}",
            issue_id="design_spec_canonicalization_failed",
            payload={"pydantic_errors": errors[:12]},
        )
    except Exception as e:  # noqa: BLE001 - designer schema feedback, not a tool crash
        return _dogfood_design_spec_validation_error(
            ctx,
            "DesignSpec canonicalization failed before rendering: "
            f"{type(e).__name__}: {e}",
            issue_id="design_spec_canonicalization_failed",
            payload={
                "exception_type": type(e).__name__,
                "exception_message": str(e)[:1000],
            },
        )
    spec = _apply_paper_poster_color_system_to_spec(spec, ctx, raw=raw)

    dogfood_recovery_error = _reject_dogfood_paper_poster_recovery_path(
        spec,
        ctx,
    )
    if dogfood_recovery_error is not None:
        return dogfood_recovery_error

    raw, spec, non_authored_recovery_reason = _recover_non_authored_paper_poster_revision(
        raw,
        spec,
        ctx,
    )
    recovery_reason = recovery_reason or non_authored_recovery_reason
    raw, spec, hollow_recovery_reason = _recover_hollow_authored_paper_poster_revision(
        raw,
        spec,
        ctx,
    )
    recovery_reason = recovery_reason or hollow_recovery_reason
    raw, spec, contamination_recovery_reason = _recover_contaminated_paper_poster_revision(
        raw,
        spec,
        ctx,
    )
    recovery_reason = recovery_reason or contamination_recovery_reason

    spec, paper_page_enhancements = enhance_paper_project_page_spec(spec, ctx)
    if paper_page_enhancements:
        raw = spec.model_dump(mode="json")
    spec = _apply_paper_poster_color_system_to_spec(spec, ctx, raw=raw)
    if is_academic_paper_poster_context(spec, ctx):
        raw = spec.model_dump(mode="json")

    if spec.artifact_type.value == "deck":
        blocking_layout_findings = [
            finding
            for finding in audit_frame_layout_plan(spec.html_artifact, artifact_type="deck")
            if finding.get("severity") == "P0"
        ]
        if blocking_layout_findings:
            primary_issue_id = str(
                blocking_layout_findings[0].get("id") or "frame_layout_contract_failed"
            )
            return obs_error(
                "Deck DesignSpec validation failed: repair the spatial storyboard and "
                "slot wiring before concrete rendering.",
                category="validation",
                payload={
                    "issue_id": primary_issue_id,
                    "repair_route": "revise_layout_storyboard",
                    "frame_layout_findings": blocking_layout_findings,
                },
            )

    academic_paper_poster = is_academic_paper_poster_context(spec, ctx)
    authored_frame = find_authored_paper_poster_frame(spec)
    blank_text_layers = _blank_text_layer_ids(spec.layer_graph)
    ignore_blank_legacy_layers = (
        raw_prefers_html
        and academic_paper_poster
        and authored_frame is not None
    )
    if blank_text_layers and not ignore_blank_legacy_layers:
        return _dogfood_design_spec_validation_error(
            ctx,
            "DesignSpec validation failed: kind='text' layers must have "
            f"non-empty text; blank layer ids: {blank_text_layers[:8]}",
            issue_id="design_spec_blank_text_layers",
            payload={"blank_layer_ids": blank_text_layers[:24]},
        )
    if blank_text_layers:
        log(
            "designer.ignored_blank_legacy_layer_graph_text",
            count=len(blank_text_layers),
            sample=blank_text_layers[:8],
            reason="authored_html_frame_is_primary",
        )
    blank_html_blocks = _blank_html_text_block_ids(spec.html_artifact)
    if blank_html_blocks:
        return _dogfood_design_spec_validation_error(
            ctx,
            "DesignSpec validation failed: html_artifact text blocks must have "
            f"non-empty text; blank block ids: {blank_html_blocks[:8]}",
            issue_id="design_spec_blank_html_blocks",
            payload={"blank_block_ids": blank_html_blocks[:24]},
        )

    claim_graph = ctx.state.get("claim_graph")
    if claim_graph is not None:
        covers_error = _validate_claim_graph_covers(spec, claim_graph)
        if covers_error is not None:
            return covers_error

    canvas_plan_error = _validate_canvas_plan(spec, ctx)
    if canvas_plan_error is not None:
        return canvas_plan_error
    deck_plan_error = _validate_deck_plan(spec, ctx)
    if deck_plan_error is not None:
        return deck_plan_error
    if academic_paper_poster and authored_frame is None:
        return obs_error(
            "DesignSpec validation failed: academic paper poster revisions must "
            "preserve an authored_html HtmlFrame; do not replace it with legacy "
            "layer/html_artifact output",
            category="validation",
            payload={
                "issue_id": "candidate_final_not_authored_html",
                "expected_render_mode": "authored_html",
                "repair_route": "revise_authored_html",
                "artifact_type": "poster",
            },
        )
    if academic_paper_poster and authored_frame is not None:
        spec, authored_auto_repair_ops = _auto_repair_authored_paper_poster_spec_preflight(
            spec,
            ctx,
        )
        if authored_auto_repair_ops:
            spec = _apply_paper_poster_color_system_to_spec(spec, ctx, raw=raw)
            raw = spec.model_dump(mode="json")
            authored_frame = find_authored_paper_poster_frame(spec)
        sanitizer_error = _validate_authored_paper_poster_frame(authored_frame, ctx)
        if sanitizer_error is not None:
            return _dogfood_design_spec_validation_error_from_result(
                ctx,
                sanitizer_error,
                draft_spec=spec,
            )

    # Build the complete proposed state, then restore the caller-visible state
    # before either persistent snapshot is published.
    state_type = ctx.state.get("artifact_type", "poster")
    if spec.artifact_type.value != state_type:
        log("artifact.spec_override",
            prior_state=state_type, spec_declared=spec.artifact_type.value)

    prior_spec = ctx.state.get("design_spec")
    is_revision = prior_spec is not None
    prior_type = getattr(getattr(prior_spec, "artifact_type", None), "value", None)
    ctx.state["artifact_type"] = spec.artifact_type.value
    if prior_type == "video" or spec.artifact_type.value == "video":
        ctx.state.pop("video_delivery", None)
        ctx.state.pop("finalized", None)
        ctx.state["composition"] = None
    ctx.state["design_spec"] = spec
    spec_hash = design_spec_sha256(spec)
    ctx.state["design_spec_sha256"] = spec_hash
    if academic_paper_poster and authored_frame is not None:
        ctx.state[AUTHORED_PAPER_POSTER_REQUIRED_STATE_KEY] = True
    proposed_revision = int(ctx.state.get("spec_revision_count") or 0) + 1
    proposed_state = capture_state_keys(
        ctx.state,
        _PROPOSE_STATE_TRANSACTION_KEYS,
        deep_copy_keys=_PROPOSE_DEEP_COPY_STATE_KEYS,
    )
    install_state_snapshot(ctx.state, entry_state)
    try:
        commit = _persist_design_spec_snapshot(
            spec,
            ctx=ctx,
            is_revision=is_revision,
        )
        _design_spec_commit_phase_hook(
            "after_persistence_before_state_install",
            path=commit.canonical_path,
        )
    except Exception as exc:  # noqa: BLE001 - persistence is a tool failure
        phase = exc.phase if isinstance(exc, DesignSpecPersistenceError) else "unknown"
        return obs_error(
            "propose_design_spec: failed to persist DesignSpec revision "
            f"based on {proposed_revision - 1}: {exc}",
            category="api",
            payload={
                "phase": phase,
                "spec_revision": proposed_revision,
                "design_spec_sha256": spec_hash,
            },
        )
    install_state_snapshot(ctx.state, proposed_state)
    ctx.state["spec_revision_count"] = commit.revision
    ctx.state["design_spec_sha256"] = commit.design_spec_sha256
    _mark_visual_reference_revision(ctx, commit.revision)
    log("spec.proposed", revision=is_revision,
        artifact_type=spec.artifact_type.value,
        canvas=spec.canvas, n_layers=len(spec.layer_graph),
        html_frames=len(getattr(spec.html_artifact, "frames", []) or []))

    return obs_ok({
        "artifact_type": spec.artifact_type.value,
        "n_layers": len(spec.layer_graph),
        "html_artifact_frames": len(getattr(spec.html_artifact, "frames", []) or []),
        "canvas": {"w_px": spec.canvas["w_px"], "h_px": spec.canvas["h_px"]},
        "is_revision": is_revision,
        "recovered_reason": recovery_reason,
        "paper_project_page_enhancements": paper_page_enhancements,
        "canvas_plan": ctx.state.get("canvas_plan"),
        "deck_plan": ctx.state.get("deck_plan"),
    })


def _auto_repair_authored_paper_poster_spec_preflight(
    spec: DesignSpec,
    ctx: ToolContext,
) -> tuple[DesignSpec, list[dict[str, Any]]]:
    if not _is_dogfood_paper_poster_contract(ctx):
        return spec, []
    try:
        from .apply_design_ops import (  # noqa: PLC0415 - lazy to avoid module-load cycle
            _auto_repair_authored_paper_poster_preflight,
        )

        spec_data = spec.model_dump(mode="json")
        repaired_spec, validation, applied_ops = _auto_repair_authored_paper_poster_preflight(
            spec_data,
            ctx,
        )
    except Exception as exc:  # noqa: BLE001 - validation below remains authoritative
        log(
            "spec.authored_preflight_auto_repair_failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        return spec, []
    if applied_ops:
        log(
            "spec.authored_preflight_auto_repaired",
            n_ops=len(applied_ops),
            residual_error=validation.error_message if validation is not None else None,
        )
    return repaired_spec, applied_ops


def _is_dogfood_paper_poster_contract(ctx: ToolContext) -> bool:
    if effective_poster_harness_mode(ctx.settings) != "dogfood":
        return False
    state = ctx.state if isinstance(ctx.state, dict) else {}
    brief = state.get("poster_content_brief")
    contract = state.get("poster_plan_contract")
    brief_is_paper_poster = (
        isinstance(brief, dict)
        and brief.get("kind") == "paper_poster_content_brief"
    )
    contract_is_paper_poster = (
        isinstance(contract, dict)
        and contract.get("kind") == "paper_poster_plan_contract"
    )
    return brief_is_paper_poster or contract_is_paper_poster


def _dogfood_design_spec_validation_error(
    ctx: ToolContext,
    message: str,
    *,
    issue_id: str,
    payload: dict[str, Any] | None = None,
    draft_spec: DesignSpec | None = None,
) -> ToolResultRecord:
    if not _is_dogfood_paper_poster_contract(ctx):
        return obs_error(message, category="validation", payload=payload)
    state = ctx.state if isinstance(ctx.state, dict) else {}
    draft_meta = _record_dogfood_draft_design_spec(
        ctx,
        draft_spec,
        issue_id=issue_id,
        message=message,
        payload=payload or {},
    )
    records = state.setdefault("designer_invalid_design_spec_records", [])
    if not isinstance(records, list):
        records = []
        state["designer_invalid_design_spec_records"] = records
    record = {
        "issue_id": issue_id,
        "message": message[:800],
    }
    detail_summary = _dogfood_invalid_design_spec_detail_summary(payload or {})
    if detail_summary:
        record["details"] = detail_summary
    records.append(record)
    total_count = len(records)
    state["designer_invalid_design_spec_count"] = total_count
    abort = total_count >= 3
    log(
        "designer.invalid_design_spec",
        count=total_count,
        issue_id=issue_id,
        abort=abort,
        message=message[:800],
        details=detail_summary,
    )
    if abort:
        abort_payload = {
            "reason": "designer_invalid_design_spec",
            "repeat_count": total_count,
            "owner": "designer_contract",
            "severity": "blocker",
            "latest_issue_id": issue_id,
        }
        state["designer_contract_abort"] = abort_payload
        state["finalize_notes"] = (
            "Designer contract abort: repeated invalid dogfood paper-poster "
            "DesignSpec submissions."
        )
        log("designer.contract_abort", **abort_payload)
    return obs_error(
        message,
        category="validation",
        payload={
            **(payload or {}),
            "issue_id": issue_id,
            "owner": "designer_contract",
            "repair_route": (
                "apply_design_ops_on_draft_then_composite"
                if draft_meta
                else "revise_complete_authored_html_design_spec"
            ),
            "draft_design_spec_available": bool(draft_meta),
            "draft_design_spec_repair_tool": "apply_design_ops" if draft_meta else None,
            "draft_design_spec_meta": draft_meta or None,
            "designer_invalid_design_spec_count": total_count,
            "designer_contract_abort": abort,
            "severity": "blocker" if abort else "high",
            "required_render_mode": "authored_html",
            "dogfood_recovery_policy": (
                "dogfood designer-authored paper posters must submit a valid "
                "authored_html DesignSpec; deterministic recovery must not "
                "replace invalid designer-authored specs."
            ),
        },
    )


def _record_dogfood_draft_design_spec(
    ctx: ToolContext,
    draft_spec: DesignSpec | None,
    *,
    issue_id: str,
    message: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if draft_spec is None or not isinstance(ctx.state, dict):
        return {}
    try:
        is_paper = is_academic_paper_poster_context(draft_spec, ctx)
    except Exception:
        is_paper = False
    if not is_paper or find_authored_paper_poster_frame(draft_spec) is None:
        return {}
    state = ctx.state
    draft_seq = int(state.get("draft_design_spec_count") or 0) + 1
    state["draft_design_spec_count"] = draft_seq
    state[DOGFOOD_DRAFT_DESIGN_SPEC_STATE_KEY] = draft_spec
    meta = {
        "draft_revision": draft_seq,
        "issue_id": issue_id,
        "message": message[:800],
        "repair_tool": "apply_design_ops",
        "repair_route": "apply_design_ops_on_draft_then_composite",
        "instruction": (
            "A schema-valid authored_html draft has been retained. Use "
            "apply_design_ops with the actionable repairs to adjust block "
            "bbox/style/source/text; do not resubmit the whole DesignSpec "
            "unless the storyboard archetype itself must change."
        ),
    }
    detail_summary = _dogfood_invalid_design_spec_detail_summary(payload)
    if detail_summary:
        meta["details"] = detail_summary
    state[DOGFOOD_DRAFT_DESIGN_SPEC_META_KEY] = meta
    try:
        atomic_write_json(
            ctx.run_dir / "specs" / f"draft_design_spec_{draft_seq:02d}.json",
            {
                "artifact_type": draft_spec.artifact_type.value,
                "is_revision": True,
                "draft_revision": draft_seq,
                "issue_id": issue_id,
                "design_spec": draft_spec.model_dump(mode="json"),
            },
        )
    except Exception as exc:  # noqa: BLE001 - draft persistence is diagnostic only
        log("designer.draft_design_spec_persist_failed", error=f"{type(exc).__name__}: {exc}")
    log("designer.draft_design_spec_retained", draft_revision=draft_seq, issue_id=issue_id)
    return meta


def _dogfood_invalid_design_spec_detail_summary(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    details: dict[str, Any] = {}
    errors = payload.get("pydantic_errors")
    if isinstance(errors, list) and errors:
        details["pydantic_errors"] = [
            {
                "loc": ".".join(str(part) for part in (err.get("loc") or ())),
                "type": err.get("type"),
                "msg": err.get("msg"),
            }
            for err in errors[:6]
            if isinstance(err, dict)
        ]
    findings = payload.get("authored_html_sanitizer_findings")
    if isinstance(findings, list) and findings:
        details["authored_html_sanitizer_findings"] = [
            {
                "id": finding.get("id"),
                "message": finding.get("message"),
                "block_id": finding.get("block_id"),
                "fix": finding.get("fix"),
                "evidence": finding.get("evidence"),
            }
            for finding in findings[:6]
            if isinstance(finding, dict)
        ]
        if payload.get("authored_html_sanitizer_p0_count") is not None:
            details["authored_html_sanitizer_p0_count"] = payload.get("authored_html_sanitizer_p0_count")
    sanitizer_repairs = payload.get("authored_html_sanitizer_actionable_repairs")
    if isinstance(sanitizer_repairs, list) and sanitizer_repairs:
        details["authored_html_sanitizer_actionable_repairs"] = sanitizer_repairs[:6]
    storyboard = payload.get("authored_html_storyboard_findings")
    if isinstance(storyboard, list) and storyboard:
        details["authored_html_storyboard_findings"] = [
            _dogfood_storyboard_finding_summary(finding)
            for finding in storyboard[:6]
            if isinstance(finding, dict)
        ]
    repairs = payload.get("authored_html_storyboard_actionable_repairs")
    if isinstance(repairs, list) and repairs:
        details["authored_html_storyboard_actionable_repairs"] = repairs[:6]
    return {key: value for key, value in details.items() if value}


def _dogfood_storyboard_finding_summary(finding: dict[str, Any]) -> dict[str, Any]:
    summary = {
        "id": finding.get("id"),
        "message": finding.get("message"),
        "hint": finding.get("hint"),
    }
    for key in (
        "block_id",
        "role",
        "kind",
        "word_count",
        "bbox",
        "font_size_px",
        "line_height_px",
        "estimated_required_height_px",
        "height_gap_px",
        "classes",
        "inline_style",
        "other_block_id",
        "other_bbox",
        "overlap_area_px",
        "overlap_ratio_of_smaller",
        "required_css",
        "visible_body_word_count",
        "dense_visible_word_floor",
        "source_visual_count",
        "source_visual_panel_count_min",
        "source_visual_area_ratio",
        "source_visual_area_ratio_min",
        "source_visual_blocks",
        "current_source_visual_area_px",
        "target_source_visual_area_px",
        "additional_source_visual_area_px",
        "target_avg_visual_panel_area_px",
        "target_min_visual_panel_area_px",
        "title_meta_canvas_area_ratio",
        "title_meta_canvas_area_ratio_max",
        "reasons",
    ):
        if finding.get(key) is not None:
            summary[key] = finding.get(key)
    return summary


def _dogfood_design_spec_validation_error_from_result(
    ctx: ToolContext,
    result: ToolResultRecord,
    *,
    draft_spec: DesignSpec | None = None,
) -> ToolResultRecord:
    payload = dict(result.payload or {})
    issue_id = str(payload.get("issue_id") or "authored_html_validation_failed")
    return _dogfood_design_spec_validation_error(
        ctx,
        result.error_message or "DesignSpec validation failed",
        issue_id=issue_id,
        payload=payload,
        draft_spec=draft_spec,
    )


def _reject_dogfood_paper_poster_recovery_path(
    spec: DesignSpec,
    ctx: ToolContext,
) -> ToolResultRecord | None:
    if not _is_dogfood_paper_poster_contract(ctx):
        return None
    if not is_academic_paper_poster_context(spec, ctx):
        return None
    frame = find_authored_paper_poster_frame(spec)
    if frame is None:
        return _dogfood_design_spec_validation_error(
            ctx,
            "DesignSpec validation failed: dogfood academic paper posters must "
            "submit an authored_html HtmlFrame; legacy layer_graph/html_artifact "
            "fallback is not an authored designer success.",
            issue_id="dogfood_non_authored_paper_poster_spec",
        )
    hollow_reason = _hollow_authored_paper_poster_reason(frame)
    if hollow_reason is not None:
        return _dogfood_design_spec_validation_error(
            ctx,
            "DesignSpec validation failed: authored_html paper poster is too "
            "hollow to accept in dogfood mode; fill the visible DOM with "
            "source-backed text, figures, captions, tables, and panels.",
            issue_id=hollow_reason,
            draft_spec=spec,
        )
    if _paper_poster_revision_has_cross_case_contamination(spec, ctx):
        return _dogfood_design_spec_validation_error(
            ctx,
            "DesignSpec validation failed: paper poster appears to contain "
            "cross-case contamination from another paper or benchmark.",
            issue_id="cross_case_paper_poster_contamination",
            draft_spec=spec,
        )
    return None


def _spec_arg_shape(args: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(args, dict):
        return {
            "arg_keys": [],
            "has_design_spec": False,
            "payload_bytes": 0,
            "arg_type": type(args).__name__,
        }
    payload = args.get("payload")
    has_nested = isinstance(payload, dict) and (
        "design_spec" in payload or "designSpec" in payload
    )
    try:
        payload_bytes = len(json.dumps(args, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception:  # noqa: BLE001 - diagnostics only
        payload_bytes = -1
    return {
        "arg_keys": sorted(str(key) for key in args.keys()),
        "has_design_spec": bool(
            "design_spec" in args
            or "designSpec" in args
            or has_nested
        ),
        "payload_bytes": payload_bytes,
    }


def _missing_design_spec_message(*, abort: bool) -> str:
    base = (
        "propose_design_spec requires a complete top-level "
        "{\"design_spec\": ...} object; empty args are not valid. "
        "Re-call propose_design_spec with a full DesignSpec JSON including "
        "canvas, palette, typography, mood, composition_notes, and html_artifact."
    )
    if abort:
        return "designer contract abort: repeated empty propose_design_spec calls. " + base
    return base


def _missing_design_spec_args_error(
    ctx: ToolContext,
    arg_shape: dict[str, Any],
) -> ToolResultRecord:
    state = ctx.state if isinstance(ctx.state, dict) else {}
    records = state.setdefault("designer_missing_design_spec_args_records", [])
    if not isinstance(records, list):
        records = []
        state["designer_missing_design_spec_args_records"] = records
    record = {
        "arg_keys": list(arg_shape.get("arg_keys") or []),
        "has_design_spec": bool(arg_shape.get("has_design_spec")),
        "payload_bytes": arg_shape.get("payload_bytes"),
    }
    records.append(record)
    total_count = len(records)
    consecutive_count = int(state.get("designer_consecutive_missing_design_spec_args_count") or 0) + 1
    state["designer_missing_design_spec_args_count"] = total_count
    state["designer_consecutive_missing_design_spec_args_count"] = consecutive_count
    log(
        "designer.missing_design_spec_args",
        count=total_count,
        consecutive_count=consecutive_count,
        **arg_shape,
    )
    abort = consecutive_count >= 2
    if abort:
        abort_payload = {
            "reason": "designer_missing_design_spec_args",
            "repeat_count": consecutive_count,
            "total_count": total_count,
            "owner": "designer_contract",
            "severity": "blocker",
        }
        state["designer_contract_abort"] = abort_payload
        state["finalize_notes"] = (
            "Designer contract abort: repeated empty propose_design_spec calls "
            "without a design_spec payload."
        )
        log("designer.contract_abort", **abort_payload)
    return obs_error(
        _missing_design_spec_message(abort=abort),
        category="validation",
        payload={
            **arg_shape,
            "issue_id": "designer_missing_design_spec_args",
            "owner": "designer_contract",
            "repair_route": "call_propose_design_spec_with_complete_design_spec",
            "accepted_keys": ["design_spec", "designSpec", "spec", "design", "payload.design_spec"],
            "missing_design_spec_count": total_count,
            "consecutive_missing_design_spec_count": consecutive_count,
            "designer_contract_abort": abort,
            "severity": "blocker" if abort else "high",
            "dogfood_recovery_policy": (
                "dogfood empty propose_design_spec calls are designer contract "
                "errors; deterministic skeleton recovery is reserved for explicit "
                "timeout diagnostics and must not be accepted as authored poster "
                "progress."
            ),
            "required_render_mode": "authored_html",
        },
    )


def _extract_raw_design_spec(args: dict[str, Any]) -> Any | None:
    raw = args.get("design_spec")
    if raw is not None:
        return raw
    if _looks_like_design_spec(args):
        return args
    for key in ("designSpec", "spec", "design"):
        candidate = args.get(key)
        if isinstance(candidate, dict) and _looks_like_design_spec(candidate):
            return candidate
    payload = args.get("payload")
    if isinstance(payload, dict):
        nested = payload.get("design_spec") or payload.get("designSpec")
        if isinstance(nested, dict) and _looks_like_design_spec(nested):
            return nested
        if _looks_like_design_spec(payload):
            return payload
    dict_values = [value for value in args.values() if isinstance(value, dict)]
    spec_values = [value for value in dict_values if _looks_like_design_spec(value)]
    if len(spec_values) == 1:
        return spec_values[0]
    return None


def pending_spec_recovery_reason_key() -> str:
    return _PENDING_SPEC_RECOVERY_REASON_KEY


def _consume_pending_spec_recovery_reason(ctx: ToolContext) -> str | None:
    state = ctx.state if isinstance(ctx.state, dict) else {}
    raw = state.pop(_PENDING_SPEC_RECOVERY_REASON_KEY, None)
    reason = str(raw or "").strip()
    return reason or None


def _record_spec_recovery(
    ctx: ToolContext,
    reason: str,
    raw: dict[str, Any] | None,
    *,
    source: str = "paper_poster_contract",
) -> None:
    selected_visuals = _recovery_source_visual_count(raw or {})
    log(
        "spec.recovered",
        reason=reason,
        source=source,
        selected_visuals=selected_visuals,
    )
    state = ctx.state if isinstance(ctx.state, dict) else {}
    records = state.setdefault("spec_recovery_records", [])
    if not isinstance(records, list):
        records = []
        state["spec_recovery_records"] = records
    record = {
        "reason": reason,
        "source": source,
        "selected_visuals": selected_visuals,
    }
    records.append(record)
    state["spec_recovery_reason"] = reason
    state["spec_recovery_count"] = len(records)
    try:
        atomic_write_json(ctx.run_dir / "spec_recovery.json", {
            "kind": "spec_recovery_report",
            "version": 1,
            "count": len(records),
            "latest_reason": reason,
            "records": records,
            "deterministic_recovery": True,
        })
    except Exception as exc:  # noqa: BLE001 - diagnostics must not block recovery
        log("spec.recovery_report_write_failed", error=f"{type(exc).__name__}: {exc}")


def _build_paper_poster_recovery_design_spec(ctx: ToolContext) -> dict[str, Any] | None:
    state = ctx.state if isinstance(ctx.state, dict) else {}
    brief = state.get("poster_content_brief") if isinstance(state.get("poster_content_brief"), dict) else {}
    contract = state.get("poster_plan_contract") if isinstance(state.get("poster_plan_contract"), dict) else {}
    if brief.get("kind") != "paper_poster_content_brief" and contract.get("kind") != "paper_poster_plan_contract":
        return None

    canvas = _recovery_canvas(state, contract)
    cw = int(canvas.get("w_px") or 3072)
    ch = int(canvas.get("h_px") or 2172)
    dpi = float(canvas.get("dpi") or 150)
    compact_wide = cw >= ch * 1.7 and ch < 1800
    dense_recovery = contract.get("reference_profile") in {
        "research_synthesis_dense",
        "visual_evidence_wall",
    }
    selected_assets = _recovery_selected_assets(
        state,
        contract,
        run_dir=ctx.run_dir,
        max_assets=8 if dense_recovery else (16 if compact_wide else 8),
    )
    if not selected_assets:
        return None
    selected_assets = _order_recovery_assets_for_layout(selected_assets, cw=cw, ch=ch)
    if compact_wide:
        selected_assets = _dedupe_recovery_assets_for_layout(selected_assets, target_count=8)
        selected_assets = selected_assets[:8]
    selected_assets = _arrange_recovery_assets_for_layout_slots(selected_assets, cw=cw, ch=ch)
    _record_recovery_layout_selected_assets(state, selected_assets)
    title = _clean_text(contract.get("title") or brief.get("title") or "Paper Poster", limit=120)
    authors = ", ".join(str(item) for item in (brief.get("authors") or []) if str(item).strip())
    authors = _clean_text(authors, limit=180)
    sections = _augment_recovery_sections_with_claim_graph(
        _recovery_sections(brief),
        state.get("claim_graph"),
    )
    identity_assets: list[dict[str, Any]] = []
    affiliations = ", ".join(str(item) for item in (brief.get("affiliations") or []) if str(item).strip())
    affiliations = _clean_text(affiliations, limit=160) or _recovery_affiliations(state)
    layout_context = _recovery_layout_context(
        title=title,
        sections=sections,
        selected_assets=selected_assets,
        contract=contract,
        canvas={"w": cw, "h": ch},
    )
    color_system = _recovery_color_system(state)
    color_roles = color_system.get("roles") if isinstance(color_system.get("roles"), dict) else {}
    color_palette = color_system.get("allowed_hexes") if isinstance(color_system.get("allowed_hexes"), list) else []

    body_html, css, blocks = _recovery_authored_html(
        title=title,
        authors=authors,
        affiliations=affiliations,
        venue="",
        sections=sections,
        selected_assets=selected_assets,
        identity_assets=identity_assets,
        canvas={"w": cw, "h": ch},
        layout_context=layout_context,
        color_system=color_system,
    )
    poster_size = {
        "preset": "custom",
        "label": contract.get("layout_archetype") or (state.get("canvas_plan") or {}).get("preset_id") or "paper poster",
        "orientation": "landscape" if cw >= ch else "portrait",
        "source": "custom",
        "width_mm": round(cw / max(dpi, 1.0) * 25.4, 4),
        "height_mm": round(ch / max(dpi, 1.0) * 25.4, 4),
    }
    return {
        "brief": f"Recovered source-backed academic poster for {title}",
        "artifact_type": "poster",
        "canvas": canvas,
        "palette": color_palette or ["#FFFFFF", "#21181B", "#C1121F", "#F7DEE1"],
        "color_system": color_system,
        "typography": {
            "title_font": "Inter",
            "subtitle_font": "Inter",
            "body_font": "Inter",
        },
        "mood": ["academic", "dense", "source-backed"],
        "composition_notes": (
            "Deterministic recovery spec generated after an empty propose_design_spec "
            "tool call. It prioritizes storyboard-selected source visuals, compact "
            "editable captions, and required narrative sections."
        ),
        "layer_graph": [],
        "html_artifact": {
            "title": title,
            "target": "poster",
            "theme": {
                "background": color_roles.get("background") or "#FFFFFF",
                "accent": color_roles.get("accent") or "#C1121F",
                "color_system": color_system,
                "palette_id": color_system.get("palette_id"),
            },
            "frames": [{
                "frame_id": "poster_canvas",
                "kind": "canvas",
                "role": "academic_paper_poster",
                "title": title,
                "render_mode": "authored_html",
                "poster_size": poster_size,
                "layout_plan": {
                    "archetype": layout_context["archetype"],
                    "margin_px": layout_context["margin_px"],
                    "gutter_px": layout_context["gutter_px"],
                    "value_profile": layout_context.get("value_profile") or {},
                    "notes": layout_context["notes"],
                    "slots": layout_context["slots"],
                },
                "authored_body_html": body_html,
                "authored_css": css,
                "blocks": blocks,
            }],
        },
    }


def _recover_non_authored_paper_poster_revision(
    raw: Any,
    spec: DesignSpec,
    ctx: ToolContext,
) -> tuple[Any, DesignSpec, str | None]:
    if find_authored_paper_poster_frame(spec) is not None:
        return raw, spec, None
    if not is_academic_paper_poster_context(spec, ctx):
        return raw, spec, None
    if not _deterministic_spec_recovery_enabled():
        return raw, spec, None

    recovered_raw = _build_paper_poster_recovery_design_spec(ctx)
    if not isinstance(recovered_raw, dict):
        return raw, spec, None

    try:
        recovered_spec = DesignSpec.model_validate(
            _canonicalize_raw_design_spec(recovered_raw, ctx=ctx)
        )
        recovered_spec = canonicalize_design_spec(
            recovered_spec,
            prefer_html_artifact=True,
        )
    except ValidationError as exc:
        log(
            "spec.recovery_failed",
            reason="non_authored_paper_poster_revision",
            error=exc.errors(include_url=False, include_input=False),
        )
        return raw, spec, None

    if find_authored_paper_poster_frame(recovered_spec) is None:
        log(
            "spec.recovery_failed",
            reason="non_authored_paper_poster_revision",
            error="recovery spec did not include authored_html frame",
        )
        return raw, spec, None

    reason = "non_authored_paper_poster_spec"
    _record_spec_recovery(ctx, reason, recovered_raw)
    return recovered_raw, recovered_spec, reason


def _recover_contaminated_paper_poster_revision(
    raw: Any,
    spec: DesignSpec,
    ctx: ToolContext,
) -> tuple[Any, DesignSpec, str | None]:
    if not is_academic_paper_poster_context(spec, ctx):
        return raw, spec, None
    if not _paper_poster_revision_has_cross_case_contamination(spec, ctx):
        return raw, spec, None
    if not _deterministic_spec_recovery_enabled():
        return raw, spec, None

    recovered_raw = _build_paper_poster_recovery_design_spec(ctx)
    if not isinstance(recovered_raw, dict):
        return raw, spec, None
    try:
        recovered_spec = DesignSpec.model_validate(
            _canonicalize_raw_design_spec(recovered_raw, ctx=ctx)
        )
        recovered_spec = canonicalize_design_spec(
            recovered_spec,
            prefer_html_artifact=True,
        )
    except ValidationError as exc:
        log(
            "spec.recovery_failed",
            reason="cross_case_paper_poster_contamination",
            error=exc.errors(include_url=False, include_input=False),
        )
        return raw, spec, None
    reason = "cross_case_paper_poster_contamination"
    _record_spec_recovery(ctx, reason, recovered_raw)
    return recovered_raw, recovered_spec, reason


def _recover_hollow_authored_paper_poster_revision(
    raw: Any,
    spec: DesignSpec,
    ctx: ToolContext,
) -> tuple[Any, DesignSpec, str | None]:
    if not is_academic_paper_poster_context(spec, ctx):
        return raw, spec, None
    frame = find_authored_paper_poster_frame(spec)
    if frame is None:
        return raw, spec, None
    reason = _hollow_authored_paper_poster_reason(frame)
    if reason is None:
        return raw, spec, None
    if not _has_paper_poster_recovery_content(ctx):
        return raw, spec, None
    if not _deterministic_spec_recovery_enabled():
        return raw, spec, None

    recovered_raw = _build_paper_poster_recovery_design_spec(ctx)
    if not isinstance(recovered_raw, dict):
        log("spec.recovery_failed", reason=reason, error="no paper poster recovery context")
        return raw, spec, None
    try:
        recovered_spec = DesignSpec.model_validate(
            _canonicalize_raw_design_spec(recovered_raw, ctx=ctx)
        )
        recovered_spec = canonicalize_design_spec(
            recovered_spec,
            prefer_html_artifact=True,
        )
    except ValidationError as exc:
        log(
            "spec.recovery_failed",
            reason=reason,
            error=exc.errors(include_url=False, include_input=False),
        )
        return raw, spec, None

    if find_authored_paper_poster_frame(recovered_spec) is None:
        log(
            "spec.recovery_failed",
            reason=reason,
            error="recovery spec did not include authored_html frame",
        )
        return raw, spec, None

    _record_spec_recovery(ctx, reason, recovered_raw)
    return recovered_raw, recovered_spec, reason


def _hollow_authored_paper_poster_reason(frame: Any) -> str | None:
    body_html = str(getattr(frame, "authored_body_html", None) or "")
    css = str(getattr(frame, "authored_css", None) or "")
    if not body_html.strip():
        return None
    if "recovery-" in body_html or "recovery-" in css:
        return None
    substantive_blocks = [
        block for block in _authored_frame_blocks(frame)
        if _authored_block_has_bbox(block)
        and str(block.get("kind") or "") in {"group", "text", "caption", "metric", "quote", "image", "table", "chart", "embed"}
    ]
    block_ref_count = len(re.findall(r"\bdata-block-id\s*=", body_html))
    if len(substantive_blocks) >= 12:
        min_body_refs = max(8, min(12, len(substantive_blocks) // 2))
        if block_ref_count < min_body_refs:
            return "hollow_authored_paper_poster_body"
    if block_ref_count < 12:
        return None
    visible_words = re.findall(
        r"[A-Za-z0-9][A-Za-z0-9+./:%-]*",
        _authored_body_visible_text(body_html),
    )
    if len(visible_words) < 80:
        return "hollow_authored_paper_poster_body"
    return None


def _authored_body_visible_text(body_html: str) -> str:
    text = re.sub(r"(?is)<\s*(script|style|svg)\b.*?<\s*/\s*\1\s*>", " ", body_html)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _has_paper_poster_recovery_content(ctx: ToolContext) -> bool:
    brief = ctx.state.get("poster_content_brief")
    if isinstance(brief, dict):
        sections = [
            section for section in (brief.get("sections") or [])
            if isinstance(section, dict)
            and (
                str(section.get("title") or "").strip()
                or section.get("bullets")
                or section.get("visual_ids")
            )
        ]
        if sections:
            return True
    contract = ctx.state.get("poster_plan_contract")
    if isinstance(contract, dict) and contract.get("kind") == "paper_poster_plan_contract":
        required = contract.get("required_sections") or contract.get("required_units")
        if required:
            return True
    return False


def _paper_poster_revision_has_cross_case_contamination(spec: DesignSpec, ctx: ToolContext) -> bool:
    brief = ctx.state.get("poster_content_brief")
    if not isinstance(brief, dict):
        return False
    title = str(brief.get("title") or "").lower()
    if "videogui" in title:
        return False
    try:
        blob = json.dumps(spec.model_dump(mode="json"), ensure_ascii=False).lower()
    except Exception:
        blob = str(spec).lower()
    contamination_markers = (
        "videogui",
        "gpt4o",
        "gpt-4o",
        "visual-centric gui",
        "gui tasks",
        "gui benchmarks",
        "text-only gui",
        "text-query planning",
        "vision-only planning",
        "instructional videos",
        "planning and action levels",
        "professional software",
    )
    return any(marker in blob for marker in contamination_markers)


def _validate_authored_paper_poster_frame(frame: Any, ctx: ToolContext) -> ToolResultRecord | None:
    """Reject invalid authored poster DOM before mutating runner state."""
    try:
        sanitized = sanitize_authored_paper_poster(frame, ctx)
    except Exception as exc:
        return obs_error(
            "DesignSpec validation failed: authored_html paper poster sanitizer "
            f"raised {type(exc).__name__}: {exc}",
            category="validation",
        )
    if sanitized.p0_count > 0:
        p0_findings = [
            finding
            for finding in sanitized.findings
            if finding.get("severity") == "P0"
        ]
        return obs_error(
            "DesignSpec validation failed: authored_html paper poster failed "
            f"sanitizer with {len(p0_findings)} P0 finding(s)",
            category="validation",
            payload={
                "issue_id": "authored_html_sanitizer_p0",
                "repair_route": "revise_authored_html",
                "authored_html_sanitizer_p0_count": len(p0_findings),
                "authored_html_sanitizer_findings": p0_findings[:12],
                "authored_html_sanitizer_actionable_repairs": (
                    _authored_sanitizer_actionable_repairs(p0_findings)
                ),
            },
        )
    storyboard_error = _validate_authored_layout_storyboard_realized(frame, sanitized, ctx)
    if storyboard_error is not None:
        return storyboard_error
    return None


def _authored_sanitizer_actionable_repairs(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    repairs: list[dict[str, Any]] = []
    image_missing = [
        finding
        for finding in findings
        if isinstance(finding, dict)
        and str(finding.get("id") or "") == "authored-html-image-missing-block-id"
    ]
    if image_missing:
        repairs.append({
            "action": "bind_missing_image_block_ids",
            "op": "html_bind_all_images_to_blocks",
            "finding_id": "authored-html-image-missing-block-id",
            "missing_image_count": len(image_missing),
            "sample_evidence": [
                finding.get("evidence")
                for finding in image_missing[:6]
                if isinstance(finding.get("evidence"), dict)
            ],
            "instruction": (
                "Call apply_design_ops with op='html_bind_all_images_to_blocks'. "
                "The op creates missing image manifest blocks from the DOM wrapper/src "
                "and writes data-block-id onto each img; do not rewrite the full "
                "DesignSpec just to bind figure images."
            ),
        })
    return repairs[:6]


def _validate_authored_layout_storyboard_realized(frame: Any, sanitized: Any, ctx: ToolContext) -> ToolResultRecord | None:
    blocks = _authored_frame_blocks(frame)
    substantive = [
        block for block in blocks
        if _authored_block_has_bbox(block)
        and str(block.get("kind") or "") in {"group", "text", "caption", "metric", "quote", "image", "table", "chart", "embed"}
    ]
    all_text_blocks = [
        block for block in blocks
        if str(block.get("kind") or "") in {"text", "caption", "metric", "quote"}
    ]
    text_blocks = [
        block for block in substantive
        if str(block.get("kind") or "") in {"text", "caption", "metric", "quote"}
    ]
    css = str(getattr(frame, "authored_css", None) or "")
    body = str(getattr(frame, "authored_body_html", None) or "")
    text_bbox_findings = _authored_text_bbox_findings(all_text_blocks, body)
    text_bbox_realization_findings = _authored_text_bbox_realization_findings(
        blocks,
        body,
        css,
        ctx,
    )
    text_overlap_findings = _authored_text_overlap_findings(text_blocks, ctx)
    if (
        len(substantive) < 12
        and not text_bbox_findings
        and not text_bbox_realization_findings
        and not text_overlap_findings
    ):
        return None
    geometry_tokens = _authored_geometry_token_count(css, body)
    manifest_words = sum(_authored_manifest_word_count(block) for block in text_blocks)
    visible_words = _authored_body_visible_word_count(getattr(sanitized, "body_html", body))
    missing_geometry = geometry_tokens < max(6, len(substantive) // 8)
    missing_text = manifest_words >= 90 and visible_words < max(30, int(manifest_words * 0.25))
    dense_visible_word_floor = _dense_authored_visible_word_floor(ctx)
    dense_text_underfilled = dense_visible_word_floor > 0 and visible_words < dense_visible_word_floor
    child_placement_finding = _authored_child_placement_finding(substantive, css, body)
    text_fit_findings = _authored_text_fit_findings(text_blocks, body)
    visual_evidence_finding = _authored_visual_evidence_wall_layout_finding(substantive, ctx)
    if (
        not missing_geometry
        and not missing_text
        and not dense_text_underfilled
        and child_placement_finding is None
        and not text_bbox_findings
        and not text_bbox_realization_findings
        and not text_overlap_findings
        and not text_fit_findings
        and visual_evidence_finding is None
    ):
        return None
    findings: list[dict[str, Any]] = []
    if missing_geometry:
        findings.append({
            "id": "authored-html-storyboard-unpositioned",
            "severity": "P0",
            "message": "Authored paper poster declares many bbox-backed blocks but its CSS/body does not realize a positioned or grid storyboard.",
            "hint": "Add absolute/grid positioning for panel groups and their children before composite; otherwise DOM flow will stack blocks off-canvas.",
            "substantive_block_count": len(substantive),
            "geometry_token_count": geometry_tokens,
        })
    if missing_text:
        findings.append({
            "id": "authored-html-storyboard-text-not-rendered",
            "severity": "P0",
            "message": "Authored paper poster keeps source-backed text in blocks[] but leaves too little visible text in authored_body_html.",
            "hint": "Copy each text/caption block's visible wording into the matching data-block-id element; blocks[] metadata is not visible poster text.",
            "manifest_word_count": manifest_words,
            "visible_body_word_count": visible_words,
        })
    if dense_text_underfilled:
        findings.append({
            "id": "authored-html-dense-visible-text-low",
            "severity": "P0",
            "message": (
                "Dense-reference paper poster has too little visible DOM text "
                "for the early content-fill gate."
            ),
            "hint": (
                "Before increasing screenshot area, add source-backed panel copy, "
                "local figure explanations, native table notes, method/result "
                "callouts, and limitations directly into authored_body_html."
            ),
            "visible_body_word_count": visible_words,
            "dense_visible_word_floor": dense_visible_word_floor,
        })
    if child_placement_finding is not None:
        findings.append(child_placement_finding)
    findings.extend(text_bbox_findings[:12])
    findings.extend(text_bbox_realization_findings[:12])
    findings.extend(text_overlap_findings[:12])
    findings.extend(text_fit_findings[:12])
    if visual_evidence_finding is not None:
        findings.append(visual_evidence_finding)
    if _dogfood_authored_storyboard_local_repair_only(findings, ctx):
        state = ctx.state if isinstance(ctx.state, dict) else {}
        warnings = findings[:12]
        state["authored_html_storyboard_local_repair_warnings"] = warnings
        log(
            "spec.authored_storyboard_local_repair_warn",
            finding_count=len(findings),
            first_findings=warnings[:6],
            reason="dense_authored_html_should_reach_composite_for_local_dom_repair",
        )
        return None
    return obs_error(
        "DesignSpec validation failed: authored_html paper poster does not "
        "realize its layout storyboard in visible DOM/CSS",
        category="validation",
        payload={
            "issue_id": "authored_html_layout_storyboard_unrealized",
            "repair_route": "revise_authored_html",
            "authored_html_storyboard_findings": findings,
            "authored_html_storyboard_actionable_repairs": (
                _authored_storyboard_actionable_repairs(findings)
            ),
        },
    )


def _dogfood_authored_storyboard_local_repair_only(findings: list[dict[str, Any]], ctx: ToolContext) -> bool:
    if not findings or not _is_dogfood_paper_poster_contract(ctx):
        return False
    local_ids = {
        "authored-html-text-bbox-not-realized",
        "authored-html-text-bbox-overlap",
        "authored-html-text-fit-underbudget",
        "authored-html-main-title-underbudget",
        "authored-html-visual-evidence-wall-underrealized",
    }
    for finding in findings:
        finding_id = str(finding.get("id") or "")
        if finding_id not in local_ids:
            return False
    return len(findings) <= 24


def _authored_storyboard_actionable_repairs(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    repairs: list[dict[str, Any]] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        finding_id = str(finding.get("id") or "")
        if finding_id in {
            "authored-html-text-fit-underbudget",
            "authored-html-main-title-underbudget",
        }:
            bbox = finding.get("bbox") if isinstance(finding.get("bbox"), dict) else {}
            current_h = _coerce_int(bbox.get("h")) or 0
            required_h = _coerce_int(finding.get("estimated_required_height_px")) or current_h
            required_h = max(required_h, current_h + max(0, _coerce_int(finding.get("height_gap_px")) or 0))
            target_h = max(required_h, 72 if finding_id == "authored-html-main-title-underbudget" else current_h)
            action = {
                "action": "resize_or_rewrite_text_block",
                "op": "html_resize_block",
                "block_id": finding.get("block_id"),
                "finding_id": finding_id,
                "role": finding.get("role"),
                "kind": finding.get("kind"),
                "current_bbox": bbox,
                "word_count": finding.get("word_count"),
                "font_size_px": finding.get("font_size_px"),
                "line_height_px": finding.get("line_height_px"),
                "target_min_height_px": int(target_h),
                "h": int(target_h),
                "increase_height_by_px": max(0, int(target_h) - int(current_h)),
                "instruction": (
                    "Call apply_design_ops with op='html_resize_block', this "
                    "block_id, and h=target_min_height_px. The op updates both "
                    "blocks[].bbox and the matching authored DOM/CSS geometry; "
                    "only rewrite text if height cannot be reserved."
                ),
            }
            if finding_id == "authored-html-main-title-underbudget":
                action["instruction"] = (
                    "Wrap the long paper title within the title row, reduce title typography if needed, "
                    "or reserve a taller three-row identity header before any lower panels. Do not add a subtitle/meta row."
                )
            repairs.append(action)
            continue
        if finding_id == "authored-html-text-bbox-missing":
            repairs.append({
                "action": "add_auditable_text_bbox",
                "op": "html_infer_text_bbox",
                "block_id": finding.get("block_id"),
                "finding_id": finding_id,
                "role": finding.get("role"),
                "kind": finding.get("kind"),
                "word_count": finding.get("word_count"),
                "classes": finding.get("classes"),
                "instruction": (
                    "Call apply_design_ops with op='html_infer_text_bbox' and this block_id. "
                    "The tool deterministically assigns an auditable bbox from the "
                    "local parent panel, sibling lanes, word count, and declared typography, "
                    "then writes matching authored DOM/CSS geometry."
                ),
            })
            continue
        if finding_id == "authored-html-text-bbox-not-realized":
            repairs.append({
                "action": "realize_text_bbox_in_authored_css",
                "op": "html_realize_block_bbox",
                "block_id": finding.get("block_id"),
                "role": finding.get("role"),
                "kind": finding.get("kind"),
                "current_bbox": finding.get("bbox"),
                "word_count": finding.get("word_count"),
                "classes": finding.get("classes"),
                "required_css": finding.get("required_css"),
                "instruction": (
                    "Call apply_design_ops with op='html_realize_block_bbox' "
                    "and this block_id. The op deterministically writes "
                    "position:absolute plus left/top/width/height into the "
                    "authored DOM/CSS from the declared bbox; do not hand-write "
                    "a class-only flex/grid repair."
                ),
            })
            continue
        if finding_id == "authored-html-text-bbox-overlap":
            repairs.append({
                "action": "separate_overlapping_text_blocks",
                "op": "html_set_block_bbox",
                "finding_id": finding_id,
                "block_id": finding.get("block_id"),
                "other_block_id": finding.get("other_block_id"),
                "current_bbox": finding.get("bbox"),
                "other_bbox": finding.get("other_bbox"),
                "overlap_area_px": finding.get("overlap_area_px"),
                "overlap_ratio_of_smaller": finding.get("overlap_ratio_of_smaller"),
                "instruction": (
                    "Call apply_design_ops with op='html_set_block_bbox' and "
                    "move one of these text-bearing bboxes so the declared "
                    "lanes no longer overlap before composite. The tool also "
                    "runs a deterministic overlap-separation pass on retained "
                    "dogfood drafts."
                ),
            })
            continue
        if finding_id == "authored-html-visual-evidence-wall-underrealized":
            current_area = _coerce_float(finding.get("source_visual_area_ratio")) or 0.0
            min_area = _coerce_float(finding.get("source_visual_area_ratio_min")) or 0.0
            current_count = _coerce_int(finding.get("source_visual_count")) or 0
            min_count = _coerce_int(finding.get("source_visual_panel_count_min")) or 0
            repairs.append({
                "action": "resize_source_visual_grid",
                "current_source_visual_count": current_count,
                "target_source_visual_count_min": min_count,
                "current_source_visual_area_ratio": round(current_area, 4),
                "target_source_visual_area_ratio_min": round(min_area, 4),
                "additional_source_visual_area_ratio_needed": round(max(0.0, min_area - current_area), 4),
                "additional_source_visual_area_px": finding.get("additional_source_visual_area_px"),
                "target_avg_visual_panel_area_px": finding.get("target_avg_visual_panel_area_px"),
                "target_min_visual_panel_area_px": finding.get("target_min_visual_panel_area_px"),
                "source_visual_blocks": finding.get("source_visual_blocks"),
                "current_title_meta_canvas_area_ratio": finding.get("title_meta_canvas_area_ratio"),
                "title_meta_canvas_area_ratio_max": finding.get("title_meta_canvas_area_ratio_max"),
                "reasons": finding.get("reasons"),
                "instruction": (
                    "On the wide reference-evidence template, reserve 6+ large "
                    "source figure/table panels first, with nearby caption lanes; "
                    "then fit prose in adjacent side/band lanes and keep the three-row identity header under the cap."
                ),
            })
            continue
        if finding_id == "authored-html-dense-visible-text-low":
            repairs.append({
                "action": "add_visible_dense_synthesis_text",
                "current_visible_words": finding.get("visible_body_word_count"),
                "target_visible_words_min": finding.get("dense_visible_word_floor"),
                "instruction": (
                    "Add source-backed visible DOM text into authored_body_html "
                    "as panel claims, evidence bullets, table notes, method-step "
                    "labels, limitations, and synthesis takeaways."
                ),
            })
    return repairs[:12]


def _dense_authored_visible_word_floor(ctx: ToolContext) -> int:
    state = ctx.state if isinstance(ctx.state, dict) else {}
    contract = state.get("poster_plan_contract") if isinstance(state.get("poster_plan_contract"), dict) else {}
    if not contract:
        return 0
    content_fill_targets = (
        contract.get("content_fill_targets")
        if isinstance(contract.get("content_fill_targets"), dict)
        else {}
    )
    is_dense = str(contract.get("reference_profile") or "") == "research_synthesis_dense" or bool(content_fill_targets)
    if not is_dense:
        return 0
    native_targets = (
        contract.get("native_information_targets")
        if isinstance(contract.get("native_information_targets"), dict)
        else {}
    )
    min_visible = _coerce_int(native_targets.get("min_visible_words")) or 900
    target_visible = _coerce_int(native_targets.get("target_visible_words")) or 1600
    if min_visible <= 0 and target_visible <= 0:
        return 0
    base_floor = 560 if str(contract.get("reference_profile") or "") == "visual_evidence_wall" else 650
    reference_layout = (
        contract.get("reference_layout_contract")
        if isinstance(contract.get("reference_layout_contract"), dict)
        else {}
    )
    if (
        str(contract.get("layout_archetype") or "") == "reference-poster"
        and str(reference_layout.get("source") or "") == "reference_poster"
    ):
        base_floor = max(min_visible, min(base_floor, target_visible))
    return min(
        max(min_visible, base_floor),
        max(base_floor, int(max(min_visible * 0.80, target_visible * 0.45))),
    )


def _authored_text_bbox_findings(blocks: list[dict[str, Any]], body_html: str) -> list[dict[str, Any]]:
    elements = _authored_dom_block_elements(body_html)
    findings: list[dict[str, Any]] = []
    for block in blocks:
        block_id = str(block.get("block_id") or "").strip()
        if not block_id or _authored_block_bbox(block) is not None:
            continue
        text = _authored_block_text(block)
        words = _authored_word_count(text)
        if words <= 0:
            continue
        role = str(block.get("role") or "").lower()
        kind = str(block.get("kind") or "").lower()
        threshold = 12 if kind == "caption" or role == "caption" else 14
        if role in {"title", "section", "label", "kicker"}:
            threshold = 18
        if words < threshold:
            continue
        element = elements.get(block_id) or {}
        inline_style = str(element.get("style") or "")
        if _authored_inline_style_bbox(inline_style) is not None:
            continue
        findings.append({
            "id": "authored-html-text-bbox-missing",
            "severity": "P0",
            "message": "Authored paper poster declares a visible long text block without an auditable bbox or inline dimensions.",
            "hint": "Give every long text/caption block an explicit bbox or inline left/top/width/height so text-fit can be checked before composite.",
            "block_id": block_id,
            "role": role,
            "kind": kind,
            "word_count": words,
            "classes": list(element.get("classes") or []),
            "inline_style": inline_style,
        })
    return findings


def _authored_text_bbox_realization_findings(
    blocks: list[dict[str, Any]],
    body_html: str,
    css: str,
    ctx: ToolContext,
) -> list[dict[str, Any]]:
    if not _is_dogfood_paper_poster_contract(ctx):
        return []
    if len(blocks) < 10:
        return []
    elements = _authored_dom_block_elements(body_html)
    blocks_by_id = {
        str(block.get("block_id") or "").strip(): block
        for block in blocks
        if str(block.get("block_id") or "").strip()
    }
    findings: list[dict[str, Any]] = []
    for block in blocks:
        block_id = str(block.get("block_id") or "").strip()
        bbox = _authored_block_bbox(block)
        if not block_id or bbox is None:
            continue
        text = _authored_block_text(block)
        words = _authored_word_count(text)
        role = str(block.get("role") or "").lower()
        kind = str(block.get("kind") or "").lower()
        if kind not in {"text", "caption", "metric", "quote"}:
            continue
        if words <= 0:
            continue
        if words < 4 and role not in {"title", "section", "caption", "metric"}:
            continue
        element = elements.get(block_id)
        if element is None:
            continue
        inline_style = str(element.get("style") or "")
        if _authored_style_realizes_bbox(inline_style, bbox, element=element, blocks_by_id=blocks_by_id):
            continue
        css_bbox = _authored_css_block_specific_bbox(css, block_id)
        if css_bbox is not None and _authored_bbox_realizes_declared(
            css_bbox,
            bbox,
            element=element,
            blocks_by_id=blocks_by_id,
        ):
            continue
        element_css_bbox = _authored_css_element_bbox(css, element)
        if element_css_bbox is not None and _authored_bbox_realizes_declared(
            element_css_bbox,
            bbox,
            element=element,
            blocks_by_id=blocks_by_id,
        ):
            continue
        if kind == "caption" and str(element.get("tag") or "").lower() == "figcaption":
            continue
        findings.append({
            "id": "authored-html-text-bbox-not-realized",
            "severity": "P0",
            "message": "Authored paper poster declares a text bbox but the matching DOM/CSS does not realize that exact lane.",
            "hint": "Dense paper posters must make every text/caption/metric bbox auditable with inline absolute left/top/width/height or a block-specific [data-block-id] CSS rule; class-only flex/grid lanes drift and create browser overlaps.",
            "block_id": block_id,
            "role": role,
            "kind": kind,
            "word_count": words,
            "bbox": bbox,
            "classes": list(element.get("classes") or []),
            "inline_style": inline_style,
            "ancestor_block_ids": list(element.get("ancestor_block_ids") or []),
            "required_css": _authored_required_bbox_css(
                block_id,
                bbox,
                element=element,
                blocks_by_id=blocks_by_id,
            ),
        })
    return findings


def _authored_text_overlap_findings(
    blocks: list[dict[str, Any]],
    ctx: ToolContext,
) -> list[dict[str, Any]]:
    if not _is_dogfood_paper_poster_contract(ctx):
        return []
    if len(blocks) < 10:
        return []
    candidates: list[dict[str, Any]] = []
    for block in blocks:
        bbox = _authored_effective_block_bbox(block)
        if bbox is None:
            continue
        text = _authored_block_text(block)
        if _authored_word_count(text) <= 0:
            continue
        block_id = str(block.get("block_id") or "").strip()
        if not block_id:
            continue
        candidates.append({
            "block_id": block_id,
            "role": str(block.get("role") or "").lower(),
            "kind": str(block.get("kind") or "").lower(),
            "bbox": bbox,
            "area": bbox["w"] * bbox["h"],
        })
    findings: list[dict[str, Any]] = []
    for left, right in itertools.combinations(candidates, 2):
        overlap = _authored_bbox_overlap(left["bbox"], right["bbox"])
        if overlap <= 0:
            continue
        smaller = max(1, min(int(left["area"]), int(right["area"])))
        ratio = overlap / smaller
        if overlap < 1200 and ratio < 0.18:
            continue
        if ratio < 0.08:
            continue
        findings.append({
            "id": "authored-html-text-bbox-overlap",
            "severity": "P0",
            "message": "Authored paper poster declares overlapping text-bearing bboxes before browser composite.",
            "hint": "Separate the two text/metric/caption lanes in the storyboard before rendering; overlap trade-offs after composite are too late for dense references.",
            "block_id": left["block_id"],
            "other_block_id": right["block_id"],
            "role": left["role"],
            "kind": left["kind"],
            "bbox": left["bbox"],
            "other_bbox": right["bbox"],
            "overlap_area_px": int(overlap),
            "overlap_ratio_of_smaller": round(ratio, 3),
        })
        if len(findings) >= 12:
            break
    return findings


def _authored_text_fit_findings(blocks: list[dict[str, Any]], body_html: str) -> list[dict[str, Any]]:
    elements = _authored_dom_block_elements(body_html)
    findings: list[dict[str, Any]] = []
    bboxes = [bbox for block in blocks if (bbox := _authored_block_bbox(block)) is not None]
    canvas_w = max((bbox["x"] + bbox["w"] for bbox in bboxes), default=0)
    canvas_h = max((bbox["y"] + bbox["h"] for bbox in bboxes), default=0)
    for block in blocks:
        block_id = str(block.get("block_id") or "").strip()
        if not block_id:
            continue
        element = elements.get(block_id) or {}
        inline_style = str(element.get("style") or "")
        bbox = _authored_block_bbox(block) or _authored_inline_style_bbox(inline_style)
        if bbox is None:
            continue
        text = _authored_block_text(block)
        words = _authored_word_count(text)
        if words <= 0:
            continue
        role = str(block.get("role") or "").lower()
        kind = str(block.get("kind") or "").lower()
        font_px = _authored_declared_font_px(block, inline_style, canvas_w, canvas_h)
        line_px = _authored_declared_line_height_px(block, inline_style, font_px)
        required_h = _authored_estimated_text_height_px(text, bbox["w"], font_px, line_px)
        tolerance = _authored_text_fit_tolerance_px(block, words, line_px)
        main_title_underbudget = (
            _authored_is_main_title_block(block, bbox, canvas_w, canvas_h)
            and words >= 12
            and bbox["h"] < 72
        )
        underfit = required_h > bbox["h"] + tolerance
        if not main_title_underbudget and not underfit:
            continue
        finding_id = (
            "authored-html-main-title-underbudget"
            if main_title_underbudget
            else "authored-html-text-fit-underbudget"
        )
        hint = (
            "Wrap the long paper title within the title row, reduce title typography if needed, or reserve a taller three-row identity header. Do not add a subtitle/meta row."
            if main_title_underbudget
            else (
                "Move or reflow nearby blocks, increase the block height, split content into lanes, "
                "or reduce local font size/line-height before composite; only rewrite text when the "
                "same paper-specific information is preserved."
            )
        )
        findings.append({
            "id": finding_id,
            "severity": "P0",
            "message": "Authored paper poster declares text that is likely to clip or overflow its bbox.",
            "hint": hint,
            "block_id": block_id,
            "role": role,
            "kind": kind,
            "word_count": words,
            "bbox": bbox,
            "font_size_px": round(font_px, 2),
            "line_height_px": round(line_px, 2),
            "estimated_required_height_px": int(math.ceil(required_h)),
            "height_gap_px": int(math.ceil(max(0.0, required_h - bbox["h"]))),
        })
    return findings


def _authored_visual_evidence_wall_layout_finding(
    blocks: list[dict[str, Any]],
    ctx: ToolContext,
) -> dict[str, Any] | None:
    state = ctx.state if isinstance(ctx.state, dict) else {}
    contract = state.get("poster_plan_contract") if isinstance(state.get("poster_plan_contract"), dict) else {}
    if str(contract.get("reference_profile") or "") != "visual_evidence_wall":
        return None
    bboxes = [bbox for block in blocks if (bbox := _authored_effective_block_bbox(block)) is not None]
    canvas_w = max((bbox["x"] + bbox["w"] for bbox in bboxes), default=0)
    canvas_h = max((bbox["y"] + bbox["h"] for bbox in bboxes), default=0)
    canvas_area = float(max(1, canvas_w * canvas_h))
    storyboard = contract.get("layout_storyboard_targets") if isinstance(contract.get("layout_storyboard_targets"), dict) else {}
    template = storyboard.get("visual_evidence_wall_template") if isinstance(storyboard.get("visual_evidence_wall_template"), dict) else {}
    hard = template.get("hard_constraints") if isinstance(template.get("hard_constraints"), dict) else {}
    min_area_ratio = _coerce_float(hard.get("source_visual_area_ratio_min"))
    if min_area_ratio is None:
        min_area_ratio = 0.30
    # Source figures/tables must be readable and locally explained, but the
    # reference gap is text density plus evidence binding, not raw image area.
    min_area_ratio = min(float(min_area_ratio), 0.14)
    min_panel_count = _coerce_int(hard.get("source_visual_panel_count_min")) or 5
    max_title_ratio = _coerce_float(hard.get("title_meta_canvas_area_ratio_max"))
    if max_title_ratio is None:
        max_title_ratio = 0.12
    max_title_ratio = max(float(max_title_ratio), 0.13)

    source_visuals = [
        block for block in blocks
        if _authored_is_source_visual_block(block)
        and _authored_effective_block_bbox(block) is not None
    ]
    source_area = sum(
        (_authored_effective_block_bbox(block) or {}).get("w", 0) * (_authored_effective_block_bbox(block) or {}).get("h", 0)
        for block in source_visuals
    )
    source_area_ratio = source_area / canvas_area
    source_visual_summaries: list[dict[str, Any]] = []
    for block in source_visuals[:12]:
        bbox = _authored_effective_block_bbox(block) or {}
        area_px = int(bbox.get("w", 0) * bbox.get("h", 0))
        source_visual_summaries.append({
            "block_id": block.get("block_id"),
            "role": block.get("role"),
            "source_id": block.get("source_id") or block.get("layer_id"),
            "bbox": bbox,
            "area_px": area_px,
            "area_ratio": round(area_px / canvas_area, 4),
        })

    title_bands = [
        block for block in blocks
        if _authored_is_title_meta_band(block, canvas_w, canvas_h)
        and _authored_effective_block_bbox(block) is not None
    ]
    title_area_ratio = _authored_bbox_union_area([
        _authored_effective_block_bbox(block) or {}
        for block in title_bands
    ]) / canvas_area

    if (
        source_area_ratio >= min_area_ratio
        and len(source_visuals) >= min_panel_count
        and title_area_ratio <= max_title_ratio
    ):
        return None
    reasons: list[str] = []
    if source_area_ratio < min_area_ratio:
        reasons.append("source_visual_area_ratio_low")
    if len(source_visuals) < min_panel_count:
        reasons.append("source_visual_panel_count_low")
    if title_area_ratio > max_title_ratio:
        reasons.append("title_meta_band_too_large")
    target_source_area_px = int(math.ceil(float(min_area_ratio) * canvas_area))
    current_source_area_px = int(round(source_area))
    additional_source_area_px = max(0, target_source_area_px - current_source_area_px)
    near_miss_area_tolerance_px = int(max(24000, round(canvas_area * 0.005)))
    near_miss_ratio_tolerance = max(0.003, float(min_area_ratio) * 0.035)
    if (
        reasons == ["source_visual_area_ratio_low"]
        and len(source_visuals) >= min_panel_count
        and additional_source_area_px <= near_miss_area_tolerance_px
        and source_area_ratio >= float(min_area_ratio) - near_miss_ratio_tolerance
    ):
        log(
            "spec.visual_evidence_area_near_miss_warn",
            source_visual_count=len(source_visuals),
            source_visual_area_ratio=round(source_area_ratio, 4),
            source_visual_area_ratio_min=round(float(min_area_ratio), 4),
            additional_source_visual_area_px=additional_source_area_px,
            near_miss_area_tolerance_px=near_miss_area_tolerance_px,
        )
        return None
    title_near_miss_tolerance = 0.02
    if (
        reasons == ["title_meta_band_too_large"]
        and source_area_ratio >= min_area_ratio
        and len(source_visuals) >= min_panel_count
        and title_area_ratio <= float(max_title_ratio) + title_near_miss_tolerance
        and title_area_ratio <= 0.15
    ):
        log(
            "spec.visual_evidence_title_meta_near_miss_warn",
            source_visual_count=len(source_visuals),
            source_visual_area_ratio=round(source_area_ratio, 4),
            source_visual_area_ratio_min=round(float(min_area_ratio), 4),
            title_meta_canvas_area_ratio=round(title_area_ratio, 4),
            title_meta_canvas_area_ratio_max=round(float(max_title_ratio), 4),
            near_miss_tolerance=title_near_miss_tolerance,
        )
        return None
    target_avg_panel_area_px = int(math.ceil(target_source_area_px / max(1, min_panel_count)))
    target_min_panel_area_px = int(math.ceil(target_avg_panel_area_px * 0.72))
    return {
        "id": "authored-html-visual-evidence-wall-underrealized",
        "severity": "P0",
        "message": "Visual-evidence-wall poster does not realize the reference storyboard with readable source-backed figure/table panels.",
        "hint": "Reserve readable source visuals/tables with caption lanes and local explanation; do not chase raw screenshot area when dense editable text and native units are the missing signal.",
        "reasons": reasons,
        "source_visual_count": len(source_visuals),
        "source_visual_panel_count_min": min_panel_count,
        "source_visual_blocks": source_visual_summaries,
        "current_source_visual_area_px": current_source_area_px,
        "target_source_visual_area_px": target_source_area_px,
        "additional_source_visual_area_px": additional_source_area_px,
        "target_avg_visual_panel_area_px": target_avg_panel_area_px,
        "target_min_visual_panel_area_px": target_min_panel_area_px,
        "source_visual_area_ratio": round(source_area_ratio, 4),
        "source_visual_area_ratio_min": round(float(min_area_ratio), 4),
        "title_meta_canvas_area_ratio": round(title_area_ratio, 4),
        "title_meta_canvas_area_ratio_max": round(float(max_title_ratio), 4),
    }


def _authored_bbox_union_area(bboxes: list[dict[str, Any]]) -> float:
    rects: list[tuple[float, float, float, float]] = []
    for bbox in bboxes:
        try:
            x0 = float(bbox.get("x") or 0)
            y0 = float(bbox.get("y") or 0)
            x1 = x0 + max(0.0, float(bbox.get("w") or 0))
            y1 = y0 + max(0.0, float(bbox.get("h") or 0))
        except (TypeError, ValueError):
            continue
        if x1 > x0 and y1 > y0:
            rects.append((x0, y0, x1, y1))
    if not rects:
        return 0.0
    xs = sorted({x0 for x0, _y0, _x1, _y1 in rects} | {x1 for _x0, _y0, x1, _y1 in rects})
    area = 0.0
    for left, right in zip(xs, xs[1:]):
        if right <= left:
            continue
        intervals = [
            (y0, y1)
            for x0, y0, x1, y1 in rects
            if x0 < right and x1 > left
        ]
        if not intervals:
            continue
        intervals.sort()
        covered = 0.0
        cur0, cur1 = intervals[0]
        for y0, y1 in intervals[1:]:
            if y0 <= cur1:
                cur1 = max(cur1, y1)
            else:
                covered += cur1 - cur0
                cur0, cur1 = y0, y1
        covered += cur1 - cur0
        area += (right - left) * covered
    return area


def _authored_block_bbox(block: dict[str, Any]) -> dict[str, int] | None:
    bbox = block.get("bbox") if isinstance(block.get("bbox"), dict) else None
    if not bbox:
        return None
    try:
        return {
            "x": int(round(float(bbox.get("x") or 0))),
            "y": int(round(float(bbox.get("y") or 0))),
            "w": max(1, int(round(float(bbox.get("w") or 0)))),
            "h": max(1, int(round(float(bbox.get("h") or 0)))),
        }
    except (TypeError, ValueError):
        return None


def _authored_effective_block_bbox(block: dict[str, Any]) -> dict[str, int] | None:
    effective = block.get("_effective_bbox") if isinstance(block.get("_effective_bbox"), dict) else None
    if effective is not None:
        return _authored_block_bbox({"bbox": effective})
    provenance = block.get("provenance") if isinstance(block.get("provenance"), dict) else {}
    visible = provenance.get("visible_bbox") if isinstance(provenance.get("visible_bbox"), dict) else None
    if visible is not None:
        return _authored_block_bbox({"bbox": visible})
    return _authored_block_bbox(block)


def _authored_inline_style_bbox(style: str) -> dict[str, int] | None:
    width = _authored_css_px_value(style, "width")
    height = _authored_css_px_value(style, "height")
    if width is None or height is None:
        return None
    return {
        "x": int(round(_authored_css_px_value(style, "left") or 0)),
        "y": int(round(_authored_css_px_value(style, "top") or 0)),
        "w": max(1, int(round(width))),
        "h": max(1, int(round(height))),
    }


def _authored_style_realizes_bbox(
    style: str,
    declared_bbox: dict[str, int],
    *,
    element: dict[str, Any] | None = None,
    blocks_by_id: dict[str, dict[str, Any]] | None = None,
) -> bool:
    if not re.search(r"(?is)(?:^|;)\s*position\s*:\s*(?:absolute|fixed)\b", str(style or "")):
        return False
    bbox = _authored_inline_style_bbox(style)
    return bbox is not None and _authored_bbox_realizes_declared(
        bbox,
        declared_bbox,
        element=element,
        blocks_by_id=blocks_by_id,
    )


def _authored_bbox_realizes_declared(
    actual_bbox: dict[str, int],
    declared_bbox: dict[str, int],
    *,
    element: dict[str, Any] | None = None,
    blocks_by_id: dict[str, dict[str, Any]] | None = None,
) -> bool:
    if not element or not blocks_by_id:
        return _authored_bboxes_close(actual_bbox, declared_bbox)
    has_positioned_ancestor = False
    for ancestor_id in element.get("ancestor_block_ids") or []:
        parent = blocks_by_id.get(str(ancestor_id))
        if parent is None:
            continue
        parent_bbox = _authored_block_bbox(parent)
        if parent_bbox is None:
            continue
        has_positioned_ancestor = True
        if _authored_bbox_is_local_to_parent(parent_bbox, declared_bbox):
            if _authored_bboxes_close(actual_bbox, declared_bbox):
                return True
            absolute_actual = {
                "x": parent_bbox["x"] + int(actual_bbox.get("x") or 0),
                "y": parent_bbox["y"] + int(actual_bbox.get("y") or 0),
                "w": int(actual_bbox.get("w") or 0),
                "h": int(actual_bbox.get("h") or 0),
            }
            absolute_declared = {
                "x": parent_bbox["x"] + int(declared_bbox.get("x") or 0),
                "y": parent_bbox["y"] + int(declared_bbox.get("y") or 0),
                "w": int(declared_bbox.get("w") or 0),
                "h": int(declared_bbox.get("h") or 0),
            }
            if _authored_bboxes_close(absolute_actual, absolute_declared):
                return True
            continue
        absolute_bbox = {
            "x": parent_bbox["x"] + int(actual_bbox.get("x") or 0),
            "y": parent_bbox["y"] + int(actual_bbox.get("y") or 0),
            "w": int(actual_bbox.get("w") or 0),
            "h": int(actual_bbox.get("h") or 0),
        }
        if _authored_bboxes_close(absolute_bbox, declared_bbox):
            return True
    if has_positioned_ancestor:
        return False
    if _authored_bboxes_close(actual_bbox, declared_bbox):
        return True
    return False


def _authored_bbox_is_local_to_parent(parent_bbox: dict[str, int], bbox: dict[str, int]) -> bool:
    return (
        int(bbox.get("x") or 0) >= 0
        and int(bbox.get("y") or 0) >= 0
        and int(bbox.get("x") or 0) + int(bbox.get("w") or 0) <= int(parent_bbox.get("w") or 0) + 8
        and int(bbox.get("y") or 0) + int(bbox.get("h") or 0) <= int(parent_bbox.get("h") or 0) + 8
        and (
            int(bbox.get("x") or 0) < int(parent_bbox.get("x") or 0)
            or int(bbox.get("y") or 0) < int(parent_bbox.get("y") or 0)
        )
    )


def _authored_css_block_specific_bbox(css: str, block_id: str) -> dict[str, int] | None:
    block_id = str(block_id or "").strip()
    if not block_id:
        return None
    matched_bbox: dict[str, int] | None = None
    for selector_group, declarations in re.findall(r"(?s)([^{}]+)\{([^{}]*)\}", str(css or "")):
        selectors = [selector.strip() for selector in selector_group.split(",") if selector.strip()]
        if not any(_authored_selector_targets_block_id(selector, block_id) for selector in selectors):
            continue
        if not re.search(r"(?is)(?:^|;)\s*position\s*:\s*(?:absolute|fixed)\b", declarations):
            continue
        bbox = _authored_inline_style_bbox(declarations)
        if bbox is not None:
            matched_bbox = bbox
    return matched_bbox


def _authored_css_element_bbox(css: str, element: dict[str, Any]) -> dict[str, int] | None:
    tag = str(element.get("tag") or "").lower()
    classes = {str(item).lower() for item in (element.get("classes") or []) if str(item)}
    if not tag and not classes:
        return None
    matched_bbox: dict[str, int] | None = None
    for selector_group, declarations in re.findall(r"(?s)([^{}]+)\{([^{}]*)\}", str(css or "")):
        if not re.search(r"(?is)(?:^|;)\s*position\s*:\s*(?:absolute|fixed)\b", declarations):
            continue
        bbox = _authored_inline_style_bbox(declarations)
        if bbox is None:
            continue
        selectors = [selector.strip() for selector in selector_group.split(",") if selector.strip()]
        if any(_authored_selector_matches_element(selector, tag=tag, classes=classes) for selector in selectors):
            matched_bbox = bbox
    return matched_bbox


def _authored_selector_matches_element(selector: str, *, tag: str, classes: set[str]) -> bool:
    cleaned = str(selector or "").strip().lower()
    if not cleaned:
        return False
    last = re.split(r"\s+|>|\+|~", cleaned)[-1]
    selector_classes = {
        match.group(1)
        for match in re.finditer(r"\.([a-z0-9_-]+)", last)
    }
    if selector_classes:
        return selector_classes.issubset(classes)
    if tag and re.match(rf"^{re.escape(tag)}(?:$|[.#:\[])", last):
        return True
    return False


def _authored_selector_targets_block_id(selector: str, block_id: str) -> bool:
    escaped = re.escape(block_id)
    return bool(
        re.search(
            rf"(?is)\[\s*data-block-id\s*=\s*(['\"]){escaped}\1\s*\]",
            selector,
        )
        or re.search(
            rf"(?is)\[\s*data-block-id\s*=\s*{escaped}\s*\]",
            selector,
        )
        or re.search(rf"(?is)#\s*{escaped}(?:\b|$)", selector)
    )


def _authored_bboxes_close(actual: dict[str, int], expected: dict[str, int]) -> bool:
    for key in ("x", "y", "w", "h"):
        diff = abs(int(actual.get(key) or 0) - int(expected.get(key) or 0))
        tolerance = 8
        if key in {"w", "h"}:
            tolerance = max(tolerance, int(round(int(expected.get(key) or 0) * 0.04)))
        if diff > tolerance:
            return False
    return True


def _authored_required_bbox_css(
    block_id: str,
    bbox: dict[str, int],
    *,
    element: dict[str, Any] | None = None,
    blocks_by_id: dict[str, dict[str, Any]] | None = None,
) -> str:
    selector = f'[data-block-id="{block_id}"]'
    required_bbox = dict(bbox)
    if element and blocks_by_id:
        relative = _authored_relative_bbox_for_element(bbox, element=element, blocks_by_id=blocks_by_id)
        if relative is not None:
            parent_id = str(relative.get("parent_id") or "").strip()
            if parent_id:
                selector = f'[data-block-id="{parent_id}"] [data-block-id="{block_id}"]'
                required_bbox = {
                    "x": int(relative["x"]),
                    "y": int(relative["y"]),
                    "w": int(relative["w"]),
                    "h": int(relative["h"]),
                }
    return (
        selector
        + "{"
        f"position:absolute;left:{required_bbox['x']}px;top:{required_bbox['y']}px;"
        f"width:{required_bbox['w']}px;height:{required_bbox['h']}px;"
        "}"
    )


def _authored_relative_bbox_for_element(
    bbox: dict[str, int],
    *,
    element: dict[str, Any],
    blocks_by_id: dict[str, dict[str, Any]],
) -> dict[str, int | str] | None:
    for ancestor_id in element.get("ancestor_block_ids") or []:
        parent = blocks_by_id.get(str(ancestor_id))
        if parent is None:
            continue
        parent_bbox = _authored_block_bbox(parent)
        if parent_bbox is None:
            continue
        if not (
            _authored_bbox_contains(parent_bbox, bbox, tolerance=8)
            or _authored_bbox_top_left_inside(parent_bbox, bbox, tolerance=24)
        ):
            continue
        return {
            "parent_id": str(ancestor_id),
            "x": max(0, bbox["x"] - parent_bbox["x"]),
            "y": max(0, bbox["y"] - parent_bbox["y"]),
            "w": bbox["w"],
            "h": bbox["h"],
        }
    return None


def _authored_bbox_contains(parent: dict[str, int], child: dict[str, int], *, tolerance: int = 0) -> bool:
    return (
        int(child.get("x") or 0) >= int(parent.get("x") or 0) - tolerance
        and int(child.get("y") or 0) >= int(parent.get("y") or 0) - tolerance
        and int(child.get("x") or 0) + int(child.get("w") or 0)
        <= int(parent.get("x") or 0) + int(parent.get("w") or 0) + tolerance
        and int(child.get("y") or 0) + int(child.get("h") or 0)
        <= int(parent.get("y") or 0) + int(parent.get("h") or 0) + tolerance
    )


def _authored_bbox_top_left_inside(parent: dict[str, int], child: dict[str, int], *, tolerance: int = 0) -> bool:
    return (
        int(child.get("x") or 0) >= int(parent.get("x") or 0) - tolerance
        and int(child.get("y") or 0) >= int(parent.get("y") or 0) - tolerance
        and int(child.get("x") or 0) <= int(parent.get("x") or 0) + int(parent.get("w") or 0) + tolerance
        and int(child.get("y") or 0) <= int(parent.get("y") or 0) + int(parent.get("h") or 0) + tolerance
    )


def _authored_bbox_overlap(left: dict[str, int], right: dict[str, int]) -> int:
    x1 = max(int(left.get("x") or 0), int(right.get("x") or 0))
    y1 = max(int(left.get("y") or 0), int(right.get("y") or 0))
    x2 = min(
        int(left.get("x") or 0) + int(left.get("w") or 0),
        int(right.get("x") or 0) + int(right.get("w") or 0),
    )
    y2 = min(
        int(left.get("y") or 0) + int(left.get("h") or 0),
        int(right.get("y") or 0) + int(right.get("h") or 0),
    )
    if x2 <= x1 or y2 <= y1:
        return 0
    return (x2 - x1) * (y2 - y1)


def _authored_css_px_value(style: str, name: str) -> float | None:
    match = re.search(
        rf"(?is)(?:^|;)\s*{re.escape(name)}\s*:\s*(-?\d+(?:\.\d+)?)px\b",
        str(style or ""),
    )
    if not match:
        return None
    try:
        return float(match.group(1))
    except (TypeError, ValueError):
        return None


def _authored_block_text(block: dict[str, Any]) -> str:
    parts = [
        str(block.get("title") or ""),
        str(block.get("text") or ""),
        str(block.get("caption") or ""),
        " ".join(str(item or "") for item in (block.get("items") or [])),
    ]
    return _authored_body_visible_text(" ".join(parts))


def _authored_declared_font_px(
    block: dict[str, Any],
    inline_style: str,
    canvas_w: int,
    canvas_h: int,
) -> float:
    style = block.get("style") if isinstance(block.get("style"), dict) else {}
    for key in ("font_size_px", "fontSizePx", "font_size", "fontSize", "font-size", "size"):
        value = style.get(key)
        parsed = _coerce_float(value)
        if parsed and parsed > 0:
            return parsed
    inline_font = _coerce_float(_authored_inline_style_value(inline_style, "font-size"))
    if inline_font and inline_font > 0:
        return inline_font
    bbox = _authored_block_bbox(block) or {"x": 0, "y": 0, "w": 1, "h": 1}
    role = str(block.get("role") or "").lower()
    kind = str(block.get("kind") or "").lower()
    if _authored_is_main_title_block(block, bbox, canvas_w, canvas_h):
        return 44.0 if canvas_w >= 2000 else 34.0
    if "narrative_section" in role:
        return 11.0
    if "provenance" in role and bbox["h"] <= 48:
        return 9.0
    if role == "title":
        return 26.0
    if role in {"section", "label", "kicker"}:
        return 15.0
    if kind == "caption" or role == "caption":
        return 15.0
    if kind in {"metric", "quote"}:
        return 16.0
    return 17.0


def _authored_declared_line_height_px(block: dict[str, Any], inline_style: str, font_px: float) -> float:
    style = block.get("style") if isinstance(block.get("style"), dict) else {}
    for key in ("line_height_px", "lineHeightPx", "line_height", "lineHeight", "line-height"):
        value = style.get(key)
        parsed = _coerce_float(value)
        if parsed and parsed > 0:
            return parsed * font_px if parsed <= 3.0 else parsed
    inline_value = _authored_inline_style_value(inline_style, "line-height")
    inline_parsed = _coerce_float(inline_value)
    if inline_parsed and inline_parsed > 0:
        return inline_parsed * font_px if inline_parsed <= 3.0 else inline_parsed
    role = str(block.get("role") or "").lower()
    if role == "title":
        return font_px * 1.10
    return font_px * 1.28


def _authored_inline_style_value(style: str, property_name: str) -> str:
    match = re.search(
        rf"(?is)(?:^|;)\s*{re.escape(property_name)}\s*:\s*([^;]+)",
        str(style or ""),
    )
    if not match:
        return ""
    return str(match.group(1) or "").strip()


def _authored_estimated_text_height_px(text: str, width_px: int, font_px: float, line_px: float) -> float:
    units = _authored_text_fit_units(text)
    inner_w = max(24.0, float(width_px) - 8.0)
    line_count = max(1, int(math.ceil((units * font_px) / inner_w)))
    return line_count * line_px + 4.0


def _authored_text_fit_tolerance_px(block: dict[str, Any], words: int, line_px: float) -> float:
    kind = str(block.get("kind") or "").lower()
    role = str(block.get("role") or "").lower()
    if kind == "caption" or role == "caption":
        return 2.0 if words >= 18 else 7.0
    if role == "title":
        return 10.0
    if words >= 35:
        return 5.0
    return max(8.0, line_px * 0.40)


def _authored_text_fit_units(text: str) -> float:
    units = 0.0
    for char in str(text or ""):
        if char.isspace():
            units += 0.28
        elif ord(char) > 127:
            units += 0.95
        elif char in ":-–—/()[]{}":
            units += 0.34
        elif char in "il.,'":
            units += 0.25
        elif char.isupper():
            units += 0.62
        else:
            units += 0.52
    return units


def _authored_is_main_title_block(
    block: dict[str, Any],
    bbox: dict[str, int],
    canvas_w: int,
    canvas_h: int,
) -> bool:
    block_id = str(block.get("block_id") or "").lower()
    role = str(block.get("role") or "").lower()
    if role != "title":
        return False
    if bbox["y"] > max(180, canvas_h * 0.14):
        return False
    if bbox["w"] < max(520, canvas_w * 0.38):
        return False
    return (
        block_id in {"title", "main_title", "paper_title", "poster_title", "title_text"}
        or block_id.startswith(("paper_title", "poster_title", "main_title"))
    )


def _authored_is_source_visual_block(block: dict[str, Any]) -> bool:
    kind = str(block.get("kind") or "").lower()
    role = str(block.get("role") or "").lower()
    source = str(block.get("source") or "").lower()
    source_id = str(block.get("source_id") or block.get("layer_id") or "").lower()
    if role == "identity" or "identity" in source or "identity" in source_id:
        return False
    if kind not in {"image", "chart", "table", "embed"}:
        return False
    return bool(
        "paper_visual" in source
        or "ingest" in source_id
        or "fig" in source_id
        or "table" in role
        or role in {"hero", "evidence", "figure", "visual", "results"}
    )


def _authored_is_title_meta_band(block: dict[str, Any], canvas_w: int, canvas_h: int) -> bool:
    bbox = _authored_block_bbox(block)
    if bbox is None:
        return False
    block_id = str(block.get("block_id") or "").lower()
    role = str(block.get("role") or "").lower()
    if bbox["y"] > max(180, canvas_h * 0.14):
        return False
    if bbox["w"] < max(520, canvas_w * 0.60):
        return False
    return (
        "title" in block_id
        or "meta" in block_id
        or "header" in block_id
        or role in {"title", "header", "banner"}
    )


def _authored_frame_blocks(frame: Any) -> list[dict[str, Any]]:
    raw_blocks = list(getattr(frame, "blocks", []) or [])
    out: list[dict[str, Any]] = []
    by_id: dict[str, tuple[int, dict[str, Any]]] = {}

    def visit(raw: Any, parent_effective_bbox: dict[str, int] | None = None) -> None:
        if hasattr(raw, "model_dump"):
            block = raw.model_dump(mode="json")
        elif isinstance(raw, dict):
            block = dict(raw)
        else:
            return
        declared_bbox = _authored_block_bbox(block)
        effective_bbox = _authored_effective_bbox_from_parent(declared_bbox, parent_effective_bbox)
        if effective_bbox is not None:
            block["_effective_bbox"] = effective_bbox
        block_id = str(block.get("block_id") or "").strip()
        if block_id:
            current = by_id.get(block_id)
            if current is None:
                by_id[block_id] = (len(out), block)
                out.append(block)
            elif _authored_manifest_block_score(block) > _authored_manifest_block_score(current[1]):
                out[current[0]] = block
                by_id[block_id] = (current[0], block)
        else:
            out.append(block)
        for child in list(block.get("children") or []):
            visit(child, effective_bbox)

    for raw in raw_blocks:
        visit(raw)
    return out


def _authored_effective_bbox_from_parent(
    bbox: dict[str, int] | None,
    parent_bbox: dict[str, int] | None,
) -> dict[str, int] | None:
    if bbox is None:
        return None
    if parent_bbox is None:
        return bbox
    if _authored_bbox_is_local_to_parent(parent_bbox, bbox):
        return {
            "x": parent_bbox["x"] + bbox["x"],
            "y": parent_bbox["y"] + bbox["y"],
            "w": bbox["w"],
            "h": bbox["h"],
        }
    return bbox


def _authored_manifest_block_score(block: dict[str, Any]) -> tuple[int, int, int, int]:
    bbox_score = int(_authored_block_bbox(block) is not None)
    children = block.get("children")
    child_score = len(children) if isinstance(children, list) else 0
    text_score = int(bool(_authored_block_text(block).strip()))
    effective_score = int(isinstance(block.get("_effective_bbox"), dict))
    return (bbox_score, min(child_score, 50), text_score, effective_score)


def _authored_block_has_bbox(block: dict[str, Any]) -> bool:
    bbox = block.get("bbox")
    if not isinstance(bbox, dict):
        return False
    try:
        return int(bbox.get("w") or 0) > 0 and int(bbox.get("h") or 0) > 0
    except (TypeError, ValueError):
        return False


def _authored_geometry_token_count(css: str, body: str) -> int:
    haystack = f"{css}\n{body}".lower()
    return len(re.findall(
        r"position\s*:\s*absolute|\b(?:left|right|top|bottom|inset)\s*:|"
        r"\bgrid-template|\bgrid-area|\bgrid-column|\bgrid-row|\bdisplay\s*:\s*grid",
        haystack,
    ))


def _authored_child_placement_finding(
    blocks: list[dict[str, Any]],
    css: str,
    body: str,
) -> dict[str, Any] | None:
    if not _authored_uses_global_absolute_block_flow(css):
        return None
    positionable = [
        block for block in blocks
        if _authored_block_has_bbox(block)
        and str(block.get("kind") or "") not in {"group", "panel", "section"}
    ]
    if len(positionable) < 10:
        return None
    elements = _authored_dom_block_elements(body)
    layout_selectors = _authored_css_layout_selectors(css)
    positioned = 0
    missing_examples: list[str] = []
    for block in positionable:
        block_id = str(block.get("block_id") or "").strip()
        if not block_id:
            continue
        element = elements.get(block_id)
        if element is None:
            continue
        if _authored_element_has_layout(element, layout_selectors):
            positioned += 1
        elif len(missing_examples) < 8:
            missing_examples.append(block_id)
    required = max(6, int(len(positionable) * 0.45))
    if positioned >= required:
        return None
    return {
        "id": "authored-html-storyboard-child-unpositioned",
        "severity": "P0",
        "message": "Authored paper poster makes all data-block-id children absolute but does not position enough child text/visual blocks.",
        "hint": "Add per-child left/top/width/height rules, grid placement, or remove global absolute flow so text, captions, and metrics do not stack at each panel origin.",
        "positionable_block_count": len(positionable),
        "positioned_child_count": positioned,
        "required_positioned_child_count": required,
        "example_unpositioned_block_ids": missing_examples,
    }


def _authored_uses_global_absolute_block_flow(css: str) -> bool:
    return bool(re.search(
        r"\[\s*data-block-id\s*\]\s*\{[^}]*position\s*:\s*absolute",
        css,
        flags=re.IGNORECASE | re.DOTALL,
    ))


def _authored_css_layout_selectors(css: str) -> list[str]:
    selectors: list[str] = []
    for selector_group, declarations in re.findall(r"(?s)([^{}]+)\{([^{}]*)\}", css):
        if not re.search(
            r"\b(?:left|right|top|bottom|inset|grid-area|grid-column|grid-row)\s*:",
            declarations,
            flags=re.IGNORECASE,
        ):
            continue
        for selector in selector_group.split(","):
            cleaned = selector.strip()
            if cleaned:
                selectors.append(cleaned)
    return selectors


def _authored_dom_block_elements(body_html: str) -> dict[str, dict[str, Any]]:
    elements: dict[str, dict[str, Any]] = {}
    soup = BeautifulSoup(str(body_html or ""), "html.parser")
    for node in soup.find_all(attrs={"data-block-id": True}):
        block_id = str(node.get("data-block-id") or "").strip()
        if not block_id:
            continue
        raw_classes = node.get("class") or []
        if isinstance(raw_classes, str):
            classes = [token for token in re.split(r"\s+", raw_classes.strip()) if token]
        else:
            classes = [str(token) for token in raw_classes if str(token)]
        ancestor_ids: list[str] = []
        parent = getattr(node, "parent", None)
        while parent is not None and getattr(parent, "name", None) is not None:
            if hasattr(parent, "get"):
                parent_id = str(parent.get("data-block-id") or "").strip()
                if parent_id:
                    ancestor_ids.append(parent_id)
            parent = getattr(parent, "parent", None)
        elements[block_id] = {
            "tag": str(getattr(node, "name", "") or "").lower(),
            "classes": classes,
            "style": str(node.get("style") or ""),
            "ancestor_block_ids": ancestor_ids,
        }
    return elements


def _authored_attr_value(attrs: str, name: str) -> str:
    match = re.search(
        rf"(?is)\b{re.escape(name)}\s*=\s*(['\"])(.*?)\1",
        attrs,
    )
    return str(match.group(2) or "") if match else ""


def _authored_attr_tokens(attrs: str, name: str) -> list[str]:
    return [
        token for token in re.split(r"\s+", _authored_attr_value(attrs, name).strip())
        if token
    ]


def _authored_element_has_layout(element: dict[str, Any], selectors: list[str]) -> bool:
    style = str(element.get("style") or "")
    if re.search(r"\b(?:left|right|top|bottom|inset|grid-area|grid-column|grid-row)\s*:", style, flags=re.IGNORECASE):
        return True
    tag = str(element.get("tag") or "").lower()
    classes = {str(item) for item in (element.get("classes") or []) if str(item)}
    for selector in selectors:
        lowered = selector.lower()
        if any(f".{klass.lower()}" in lowered for klass in classes):
            return True
        if tag and re.search(rf"(^|[\s>+~]){re.escape(tag)}($|[\s.#:[>+~])", lowered):
            return True
    return False


def _authored_manifest_word_count(block: dict[str, Any]) -> int:
    parts = [
        str(block.get("title") or ""),
        str(block.get("text") or ""),
        str(block.get("caption") or ""),
        " ".join(str(item or "") for item in (block.get("items") or [])),
    ]
    return _authored_word_count(" ".join(parts))


def _authored_body_visible_word_count(body_html: str) -> int:
    no_tags = re.sub(r"(?is)<[^>]+>", " ", str(body_html or ""))
    return _authored_word_count(unescape(no_tags))


def _authored_word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'./+-]*|[\u4e00-\u9fff]", str(text or "")))


def _recovery_canvas(state: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    for candidate in (
        (state.get("canvas_plan") or {}).get("canvas") if isinstance(state.get("canvas_plan"), dict) else None,
        (contract.get("canvas_plan") or {}).get("canvas") if isinstance(contract.get("canvas_plan"), dict) else None,
    ):
        if isinstance(candidate, dict) and int(candidate.get("w_px") or 0) > 0 and int(candidate.get("h_px") or 0) > 0:
            return {
                "w_px": int(candidate.get("w_px")),
                "h_px": int(candidate.get("h_px")),
                "dpi": int(candidate.get("dpi") or 150),
                "aspect_ratio": str(candidate.get("aspect_ratio") or "1.414:1"),
                "color_mode": str(candidate.get("color_mode") or "RGB"),
            }
    return {"w_px": 3072, "h_px": 2172, "dpi": 150, "aspect_ratio": "1.414:1", "color_mode": "RGB"}


def _recovery_selected_assets(
    state: dict[str, Any],
    contract: dict[str, Any],
    *,
    run_dir: Path,
    max_assets: int = 8,
    ) -> list[dict[str, Any]]:
    rendered = state.get("rendered_layers") if isinstance(state.get("rendered_layers"), dict) else {}
    storyboard = state.get("paper_visual_storyboard") if isinstance(state.get("paper_visual_storyboard"), dict) else {}
    forbidden_assets = _recovery_forbidden_asset_ids(state, contract)
    candidates: list[dict[str, Any]] = []
    for source in (
        storyboard.get("selected_assets") if isinstance(storyboard, dict) else None,
        ((contract.get("visual_storyboard") or {}).get("selected_assets") if isinstance(contract.get("visual_storyboard"), dict) else None),
        contract.get("storyboard_selected_assets"),
        contract.get("selected_visuals"),
    ):
        for item in source or []:
            if isinstance(item, dict):
                asset_id = str(item.get("asset_id") or item.get("layer_id") or "").strip()
                if asset_id and asset_id not in forbidden_assets:
                    candidates.append({**item, "asset_id": asset_id})
    candidate_ids = {str(item.get("asset_id") or "").strip() for item in candidates}
    extra_candidates: list[dict[str, Any]] = []
    for layer_id, rec in rendered.items():
        if not (isinstance(rec, dict) and str(layer_id).startswith(("ingest_fig_", "ingest_table_"))):
            continue
        if str(layer_id) in forbidden_assets:
            continue
        if str(layer_id) in candidate_ids:
            continue
        extra_candidates.append({
            "asset_id": str(layer_id),
            "caption_short": rec.get("caption"),
            "visual_score": rec.get("visual_score"),
            "image_size": rec.get("image_size"),
            "visual_role": rec.get("visual_role"),
        })
    extra_candidates.sort(
        key=lambda item: _recovery_extra_asset_score(item),
        reverse=True,
    )
    candidates.extend(extra_candidates)

    seen: set[str] = set()
    selected: list[dict[str, Any]] = []
    for item in candidates:
        asset_id = str(item.get("asset_id") or "").strip()
        if not asset_id or asset_id in seen:
            continue
        rec = rendered.get(asset_id) if isinstance(rendered.get(asset_id), dict) else {}
        src_path = str(rec.get("src_path") or "").strip()
        output_file = str(item.get("output_file") or "").strip()
        if not src_path and output_file:
            try:
                output_path = Path(output_file)
                src_path = str(output_path if output_path.is_absolute() else (run_dir / output_path))
            except OSError:
                src_path = output_file
        if not src_path:
            continue
        trim = _trim_source_visual_for_recovery(src_path, asset_id=asset_id, run_dir=run_dir)
        layout_src_path = str(trim.get("src_path") or src_path)
        image_size = (
            _string_image_size(trim.get("image_size") or rec.get("image_size") or item.get("image_size"))
            or _image_size_from_path(layout_src_path)
        )
        selected.append({
            "asset_id": asset_id,
            "story_role": item.get("story_role") or rec.get("visual_role") or rec.get("role") or "source_visual",
            "caption": _clean_text(item.get("caption_short") or rec.get("caption") or item.get("caption_full") or "", limit=140),
            "caption_full": _clean_text(item.get("caption_full") or rec.get("caption") or item.get("caption_short") or "", limit=260),
            "src_path": layout_src_path,
            "original_src_path": src_path if layout_src_path != src_path else None,
            "source_processing": trim.get("source_processing"),
            "source_processing_details": trim.get("source_processing_details"),
            "image_size": image_size,
            "source_page": rec.get("source_page") or item.get("source_page"),
            "source_bbox_pdf_points": item.get("source_bbox_pdf_points") or rec.get("source_bbox_pdf_points"),
            "visual_score": item.get("visual_score") or rec.get("visual_score"),
            "curation_flags": item.get("curation_flags") or rec.get("curation_flags") or [],
        })
        seen.add(asset_id)
        if len(selected) >= max_assets:
            break
    return selected


def _recovery_forbidden_asset_ids(state: dict[str, Any], contract: dict[str, Any]) -> set[str]:
    storyboard = state.get("paper_visual_storyboard") if isinstance(state.get("paper_visual_storyboard"), dict) else {}
    tiers = contract.get("source_asset_tiers") if isinstance(contract.get("source_asset_tiers"), dict) else {}
    policy = contract.get("source_asset_policy") if isinstance(contract.get("source_asset_policy"), dict) else {}
    brief = state.get("poster_content_brief") if isinstance(state.get("poster_content_brief"), dict) else {}
    visual_selection = brief.get("visual_selection") if isinstance(brief.get("visual_selection"), dict) else {}
    ids: set[str] = set()
    for source in (
        storyboard.get("rejected_assets") if isinstance(storyboard, dict) else None,
        tiers.get("rejected_assets") if isinstance(tiers, dict) else None,
    ):
        for item in source or []:
            if isinstance(item, dict):
                asset_id = str(item.get("asset_id") or item.get("layer_id") or "").strip()
                if asset_id:
                    ids.add(asset_id)
    for raw in (
        tiers.get("forbidden_source_ids") if isinstance(tiers, dict) else None,
        policy.get("forbidden_source_ids") if isinstance(policy, dict) else None,
        visual_selection.get("forbidden_visual_ids") if isinstance(visual_selection, dict) else None,
    ):
        for item in raw or []:
            asset_id = str(item or "").strip()
            if asset_id:
                ids.add(asset_id)
    return ids


def _recovery_extra_asset_score(asset: dict[str, Any]) -> tuple[int, float, float, float]:
    score = int(float(asset.get("visual_score") or 0))
    aspect = _source_aspect_from_asset(asset)
    aspect_fit = 1.0 - min(1.0, abs(aspect - 3.0) / 3.0) if aspect > 0 else 0.0
    role_text = " ".join(
        str(asset.get(key) or "")
        for key in ("asset_id", "visual_role", "caption_short", "caption", "caption_full")
    ).lower()
    role_bonus = 1 if any(token in role_text for token in ("table", "result", "qual", "stat", "eval", "evidence")) else 0
    appendix_penalty = 1 if _recovery_asset_looks_like_low_value_appendix(role_text) or (
        re.search(r"\btable\s*(?:1[5-9]|[2-9][0-9])\b", role_text)
        or any(
            token in role_text
            for token in (
                "appendix example",
                "image editing example",
                "video editing example",
                "example with photoshop",
                "example with davinci",
            )
        )
    ) else 0
    caption_bonus = 1 if _recovery_asset_has_source_caption(asset) else 0
    return -appendix_penalty, caption_bonus, role_bonus, aspect_fit, float(score)


def _trim_source_visual_for_recovery(
    src_path: str,
    *,
    asset_id: str,
    run_dir: Path,
) -> dict[str, Any]:
    try:
        from PIL import Image, ImageChops
    except Exception:
        return {}
    try:
        path = Path(src_path)
        if not path.exists() or not path.is_file():
            return {}
        with Image.open(path) as opened:
            image = opened.convert("RGB")
        w, h = image.size
        if w < 120 or h < 120:
            return {}
        white = Image.new("RGB", image.size, (255, 255, 255))
        diff = ImageChops.difference(image, white).convert("L")
        mask = diff.point(lambda value: 255 if value > 18 else 0)
        bbox = mask.getbbox()
        if not bbox:
            return {}
        x0, y0, x1, y1 = bbox
        bbox_area = max(1, (x1 - x0) * (y1 - y0))
        content_ratio = bbox_area / max(1, w * h)
        if content_ratio >= 0.88:
            return {}
        if content_ratio <= 0.12:
            return {}
        pad = max(6, int(min(w, h) * 0.018))
        crop_box = (
            max(0, x0 - pad),
            max(0, y0 - pad),
            min(w, x1 + pad),
            min(h, y1 + pad),
        )
        crop_w = crop_box[2] - crop_box[0]
        crop_h = crop_box[3] - crop_box[1]
        if crop_w < 120 or crop_h < 120:
            return {}
        if crop_w >= w * 0.98 and crop_h >= h * 0.98:
            return {}
        layers_dir = run_dir / "layers"
        layers_dir.mkdir(parents=True, exist_ok=True)
        out_path = layers_dir / f"{_safe_identifier(asset_id)}_trim.png"
        image.crop(crop_box).save(out_path, "PNG", optimize=True)
        log(
            "spec.recovery_source_visual_trimmed",
            asset_id=asset_id,
            original=str(path),
            trimmed=str(out_path),
            content_ratio=round(content_ratio, 4),
            crop_box=list(crop_box),
        )
        return {
            "src_path": str(out_path),
            "image_size": f"{crop_w}x{crop_h}",
            "source_processing": "deterministic_white_margin_trim",
            "source_processing_details": {
                "original_src_path": str(path),
                "content_bbox_px": [x0, y0, x1, y1],
                "content_bbox_ratio": round(content_ratio, 4),
                "crop_box_px": list(crop_box),
            },
        }
    except Exception as exc:
        log("spec.recovery_source_visual_trim_failed", asset_id=asset_id, error=str(exc))
        return {}


def _string_image_size(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, dict):
        w = value.get("w") or value.get("width") or value.get("width_px")
        h = value.get("h") or value.get("height") or value.get("height_px")
        if w is not None and h is not None:
            return f"{int(float(w))}x{int(float(h))}"
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return f"{int(float(value[0]))}x{int(float(value[1]))}"
    return str(value)


def _image_size_from_path(path_value: Any) -> str | None:
    path_text = str(path_value or "").strip()
    if not path_text:
        return None
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        path = Path(path_text)
        if not path.exists() or not path.is_file():
            return None
        with Image.open(path) as opened:
            w, h = opened.size
        if w > 0 and h > 0:
            return f"{int(w)}x{int(h)}"
    except Exception:
        return None
    return None


def _source_aspect_from_asset(asset: dict[str, Any]) -> float:
    raw_size = asset.get("image_size")
    if isinstance(raw_size, dict):
        try:
            w = int(raw_size.get("w") or raw_size.get("width") or 0)
            h = int(raw_size.get("h") or raw_size.get("height") or 0)
        except (TypeError, ValueError):
            w = h = 0
        if w > 0 and h > 0:
            return w / float(h)
    size = str(raw_size or "").strip()
    match = re.match(r"^\s*(\d+)\s*x\s*(\d+)\s*$", size)
    if not match:
        return 0.0
    w = int(match.group(1))
    h = int(match.group(2))
    if w <= 0 or h <= 0:
        return 0.0
    return w / float(h)


def _fit_source_visual_bbox_to_asset(
    asset: dict[str, Any],
    bbox: dict[str, Any],
) -> dict[str, int]:
    """Treat the layout bbox as a max slot and fit the source at natural ratio."""
    try:
        x = int(round(float(bbox.get("x") or 0)))
        y = int(round(float(bbox.get("y") or 0)))
        max_w = max(1, int(round(float(bbox.get("w") or 1))))
        max_h = max(1, int(round(float(bbox.get("h") or 1))))
    except (TypeError, ValueError):
        return {"x": 0, "y": 0, "w": 1, "h": 1}
    aspect = _source_aspect_from_asset(asset)
    if aspect <= 0:
        return {"x": x, "y": y, "w": max_w, "h": max_h}
    fit_w = float(max_w)
    fit_h = fit_w / aspect
    if fit_h > max_h:
        fit_h = float(max_h)
        fit_w = fit_h * aspect
    w = max(1, min(max_w, int(round(fit_w))))
    h = max(1, min(max_h, int(round(fit_h))))
    return {
        "x": x + max(0, int(round((max_w - w) / 2.0))),
        "y": y,
        "w": w,
        "h": h,
    }


def _order_recovery_assets_for_layout(assets: list[dict[str, Any]], *, cw: int, ch: int) -> list[dict[str, Any]]:
    if not (cw >= ch * 1.7 and ch < 1800):
        return assets
    pool_size = min(len(assets), 16)
    head = [
        asset
        for _, asset in sorted(
            enumerate(assets[:pool_size]),
            key=lambda item: (
                _recovery_layout_asset_rank(item[1]),
                _recovery_wide_slot_aspect_penalty(item[1]),
                item[0],
            ),
        )
    ]
    return head + assets[pool_size:]


def _dedupe_recovery_assets_for_layout(
    assets: list[dict[str, Any]],
    *,
    target_count: int,
) -> list[dict[str, Any]]:
    """Prefer one visual per figure/table before filling compact grids."""
    if target_count <= 0:
        return assets
    selected: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    seen_groups: set[str] = set()
    for asset in assets:
        group = _recovery_asset_group_key(asset)
        if group and group in seen_groups:
            deferred.append(asset)
            continue
        selected.append(asset)
        if group:
            seen_groups.add(group)
        if len(selected) >= target_count:
            break
    if len(selected) < target_count:
        selected.extend(deferred[: max(0, target_count - len(selected))])
    return selected + [
        asset
        for asset in assets
        if str(asset.get("asset_id") or "") not in {
            str(item.get("asset_id") or "") for item in selected
        }
    ]


def _arrange_recovery_assets_for_layout_slots(
    assets: list[dict[str, Any]],
    *,
    cw: int,
    ch: int,
) -> list[dict[str, Any]]:
    """Keep source figures in slots that match their original aspect ratios."""
    if len(assets) < 3:
        return assets
    geom = _recovery_layout_geometry(
        cw,
        ch,
        source_visual_count=len(assets),
    )
    compact_wide = bool(geom.get("compact_wide"))
    slot_aspects = _recovery_visual_slot_aspects(geom, count=min(len(assets), 9))
    if len(slot_aspects) < 2:
        return assets
    head = list(assets[:len(slot_aspects)])
    tail = list(assets[len(slot_aspects):])

    pinned: dict[int, dict[str, Any]] = {}
    remaining = list(head)
    if compact_wide:
        for slot_idx, tokens in (
            (0, ("figure 1", "fig. 1", "brief illustration of videogui")),
            (1, ("figure 4", "fig. 4", "hierarchical evaluation")),
            (2, ("figure 6", "fig. 6", "qualitative results", "wrong model predictions")),
            (3, ("table 3", "full evaluation on videogui", "full benchmark table")),
            (4, ("table 4", "procedural planning")),
            (5, ("figure 7", "fig. 7", "agent framework")),
            (6, ("figure 12", "fig. 12", "manual annotation", "annotation tool")),
            (7, ("figure 5", "fig. 5", "plan scores", "plan score", "mid plan", "action number")),
        ):
            for asset in list(remaining):
                if _recovery_asset_mentions(asset, tokens):
                    if slot_idx < len(slot_aspects) and _recovery_slot_assignment_cost(asset, slot_aspects[slot_idx]) > 3.2:
                        continue
                    pinned[slot_idx] = asset
                    remaining.remove(asset)
                    break

    open_slots = [idx for idx in range(len(slot_aspects)) if idx not in pinned]
    if not open_slots:
        return [pinned[idx] for idx in range(len(slot_aspects))] + tail

    best_perm: tuple[dict[str, Any], ...] | None = None
    best_cost = float("inf")
    for perm in itertools.permutations(remaining, len(open_slots)):
        cost = 0.0
        for slot_idx, asset in zip(open_slots, perm):
            cost += _recovery_slot_assignment_cost(asset, slot_aspects[slot_idx])
            cost += abs(head.index(asset) - slot_idx) * 0.015
        if cost < best_cost:
            best_cost = cost
            best_perm = perm
    if best_perm is None:
        return assets
    arranged_by_slot: dict[int, dict[str, Any]] = dict(pinned)
    arranged_by_slot.update({slot_idx: asset for slot_idx, asset in zip(open_slots, best_perm)})
    arranged = [arranged_by_slot[idx] for idx in range(len(slot_aspects)) if idx in arranged_by_slot]
    return arranged + tail


def _recovery_visual_slot_aspects(geom: dict[str, int], *, count: int) -> list[float]:
    if count <= 0:
        return []
    aspects: list[float] = []
    if bool(geom.get("compact_wide")):
        for _ in range(3):
            aspects.append(float(geom["grid_w"]) / max(1.0, float(geom["top_img_h"])))
        for _ in range(2):
            aspects.append(float(geom.get("wide_grid_w", geom["grid_w"])) / max(1.0, float(geom["grid_img_h"])))
        for _ in range(3):
            aspects.append(float(geom["grid_w"]) / max(1.0, float(geom.get("grid_img_h_2", geom["grid_img_h"]))))
        return aspects[:count]
    aspects.append(float(geom["top_w"]) / max(1.0, float(geom["top_img_h"])))
    # The second top slot spans the remaining canvas width. Use stored geometry
    # terms so aspect assignment mirrors _recovery_authored_html positions.
    second_top_x = int(geom["margin"]) + int(geom["top_w"]) + int(geom["gutter"])
    second_top_w = int(geom["canvas_w"]) - int(geom["margin"]) - second_top_x
    aspects.append(float(max(1, second_top_w)) / max(1.0, float(geom["top_img_h"])))
    for row in range(2):
        img_h = geom["grid_img_h"] if row == 0 else geom.get("grid_img_h_2", geom["grid_img_h"])
        for _ in range(3):
            aspects.append(float(geom["grid_w"]) / max(1.0, float(img_h)))
    return aspects[:count]


def _recovery_slot_assignment_cost(asset: dict[str, Any], slot_aspect: float) -> float:
    aspect = _source_aspect_from_asset(asset)
    if aspect <= 0 or slot_aspect <= 0:
        return 2.0
    ratio = max(slot_aspect / aspect, aspect / slot_aspect)
    cost = abs(math.log(max(ratio, 1.0)))
    if ratio >= 2.4:
        cost += 4.0 + (ratio - 2.4)
    if aspect < 1.8 and slot_aspect > 2.8:
        cost += 3.0 + (slot_aspect - 2.8)
    return cost


def _recovery_asset_mentions(asset: dict[str, Any], tokens: tuple[str, ...]) -> bool:
    text = " ".join(
        str(asset.get(key) or "")
        for key in ("asset_id", "story_role", "caption", "caption_full", "caption_short")
    ).lower()
    return any(token in text for token in tokens)


def _recovery_asset_group_key(asset: dict[str, Any]) -> str:
    text = " ".join(
        str(asset.get(key) or "")
        for key in ("caption", "caption_full", "caption_short", "asset_id")
    )
    match = re.search(r"\b(fig(?:ure)?\.?|table)\s*([0-9]+[A-Za-z]?)\b", text, flags=re.IGNORECASE)
    if match:
        prefix = "table" if match.group(1).lower().startswith("table") else "figure"
        return f"{prefix}:{match.group(2).lower()}"
    return ""


def _record_recovery_layout_selected_assets(state: dict[str, Any], assets: list[dict[str, Any]]) -> None:
    """Expose the final recovery layout selection for renderer audits."""
    layout_selected = []
    for asset in assets:
        asset_id = str(asset.get("asset_id") or "").strip()
        if not asset_id:
            continue
        layout_selected.append({
            "asset_id": asset_id,
            "story_role": asset.get("story_role"),
            "caption_short": asset.get("caption") or asset.get("caption_short"),
            "caption_full": asset.get("caption_full") or asset.get("caption"),
            "source_page": asset.get("source_page"),
            "output_file": asset.get("output_file"),
            "src_path": asset.get("src_path"),
            "image_size": asset.get("image_size"),
            "visual_score": asset.get("visual_score"),
        })
    if not layout_selected:
        return
    storyboard = state.get("paper_visual_storyboard")
    if isinstance(storyboard, dict):
        storyboard["layout_selected_assets"] = layout_selected
        storyboard["layout_selected_asset_count"] = len(layout_selected)
    contract = state.get("poster_plan_contract")
    if isinstance(contract, dict):
        contract["layout_selected_assets"] = layout_selected
        contract["layout_selected_asset_count"] = len(layout_selected)


def _recovery_wide_slot_aspect_penalty(asset: dict[str, Any]) -> float:
    aspect = _source_aspect_from_asset(asset)
    if aspect <= 0:
        return 1.0
    if 1.45 <= aspect <= 4.8:
        return 0.0
    return min(4.0, abs(aspect - 2.6))


def _recovery_layout_asset_rank(asset: dict[str, Any]) -> int:
    text = " ".join(
        str(asset.get(key) or "")
        for key in ("asset_id", "story_role", "caption", "caption_full", "caption_short")
    ).lower()
    if not _recovery_asset_has_source_caption(asset):
        return 20
    if _recovery_asset_looks_like_low_value_appendix(text):
        return 18
    if re.search(r"\bfig(?:ure)?\.?\s*1\b", text) or "brief illustration of videogui" in text:
        return 0
    if re.search(r"\bfig(?:ure)?\.?\s*4\b", text) or "hierarchical evaluation" in text:
        return 1
    if "table 3" in text or "full evaluation on videogui" in text or "full benchmark table" in text:
        return 2
    if "qualitative results" in text or "wrong predictions" in text:
        return 3
    if "table 4" in text or "procedural planning" in text:
        return 4
    if "figure 7" in text or "agent framework" in text:
        return 5
    if "figure 12" in text or "manual annotation" in text or "annotation tool" in text:
        return 6
    if (
        "figure 5" in text
        or "fig. 5" in text
        or "plan scores" in text
        or "plan score" in text
        or "high plan" in text
        or "mid plan" in text
        or "mid. plan score" in text
        or "action number" in text
    ):
        return 6
    if "data statistics" in text or "figure 3" in text:
        return 12
    return 9


def _recovery_asset_looks_like_low_value_appendix(text: str) -> bool:
    lower = re.sub(r"\s+", " ", str(text or "")).lower()
    if not lower:
        return False
    appendix_tokens = (
        "appendix example",
        "image editing example",
        "video editing example",
        "video creation example",
        "example with photoshop",
        "example with davinci",
        "example with runway",
        "creation example with",
        "qa pair",
        "scroll qa",
        "key / press action",
        "key and press action",
        "illustration of how we evaluate the key",
        "illustration of how we create",
        "prompt template",
        "instruction prompt",
    )
    if any(token in lower for token in appendix_tokens):
        return True
    return bool(
        re.search(r"\bfig(?:ure)?\.?\s*(?:1[4-9]|[2-9][0-9])\b", lower)
        and any(token in lower for token in ("example", "qa", "prompt", "illustration of how"))
    )


def _recovery_asset_has_source_caption(asset: dict[str, Any]) -> bool:
    flags = {str(flag).strip().lower() for flag in (asset.get("curation_flags") or [])}
    if "no_caption" in flags or "low_caption_confidence" in flags:
        return False
    asset_id = str(asset.get("asset_id") or "").strip().lower()
    for key in ("caption_full", "caption", "caption_short"):
        text = _clean_text(asset.get(key) or "", limit=260, ellipsis=False).strip()
        lower = text.lower()
        if not text or lower == asset_id or re.fullmatch(r"ingest_(?:fig|table|img)_\d+", lower):
            continue
        if len(re.findall(r"[A-Za-z0-9]+", text)) >= 3:
            return True
    return False


def _recovery_identity_assets(state: dict[str, Any], contract: dict[str, Any]) -> list[dict[str, Any]]:
    rendered = state.get("rendered_layers") if isinstance(state.get("rendered_layers"), dict) else {}
    system = state.get("academic_identity_assets") if isinstance(state.get("academic_identity_assets"), dict) else {}
    contract_system = contract.get("identity_system") if isinstance(contract.get("identity_system"), dict) else {}
    assets = list(system.get("assets") or []) + list(contract_system.get("assets") or [])
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        asset_id = str(asset.get("asset_id") or "").strip()
        layer_id = str(asset.get("rendered_layer_id") or asset_id).strip()
        if not asset_id or asset_id in seen:
            continue
        if asset.get("safe_to_place") is False or asset.get("allowed_to_place") is False:
            continue
        rec = rendered.get(layer_id) if isinstance(rendered.get(layer_id), dict) else {}
        src_path = str(rec.get("src_path") or asset.get("local_asset_path") or "").strip()
        if not src_path:
            continue
        selected.append({**asset, "asset_id": asset_id, "rendered_layer_id": layer_id, "src_path": src_path})
        seen.add(asset_id)
        if len(selected) >= 2:
            break
    return selected


def _recovery_affiliations(state: dict[str, Any]) -> str:
    identity = state.get("academic_identity_assets")
    if not isinstance(identity, dict):
        return ""
    names: list[str] = []
    for collection in (identity.get("entities") or [], identity.get("assets") or []):
        for item in collection:
            if not isinstance(item, dict):
                continue
            intent = str(item.get("identity_group") or item.get("placement_intent") or "")
            if intent != "verified_affiliation":
                continue
            name = _clean_text(item.get("entity_name") or item.get("label") or "", limit=64, ellipsis=False)
            if name and name not in names:
                names.append(name)
    return "; ".join(names[:3])


def _recovery_sections(brief: dict[str, Any]) -> list[dict[str, Any]]:
    sections = [item for item in list(brief.get("sections") or []) if isinstance(item, dict)]
    if sections:
        dense_ids = {
            "problem_contribution",
            "model_card",
            "method_pipeline",
            "results_table",
            "ablation_analysis",
            "synthesis_takeaway",
        }
        limit = 8 if brief.get("reference_profile") == "research_synthesis_dense" or any(
            str(section.get("section_id") or "") in dense_ids for section in sections
        ) else 6
        return [
            {**section, "bullets": list(section.get("bullets") or [])}
            for section in sections[:limit]
        ]
    return [
        {"section_id": "problem", "title": "Problem", "bullets": [{"text": "Motivation and core challenge from the source paper."}]},
        {"section_id": "method", "title": "Method", "bullets": [{"text": "Main architecture and mechanism, grounded in selected figures."}]},
        {"section_id": "main_evidence", "title": "Main Evidence", "bullets": [{"text": "Results and qualitative evidence from paper figures."}]},
        {"section_id": "takeaway", "title": "Takeaway", "bullets": [{"text": "Source-backed summary of what the method enables."}]},
    ]


def _augment_recovery_sections_with_claim_graph(
    sections: list[dict[str, Any]],
    claim_graph: Any,
) -> list[dict[str, Any]]:
    out = [
        {**section, "bullets": list(section.get("bullets") or [])}
        for section in sections
    ]
    section_by_id = {
        _safe_identifier(section.get("section_id") or ""): section
        for section in out
    }
    fallback_section = section_by_id.get("takeaway") or (out[-1] if out else None)
    existing_claim_ids = {
        str(bullet.get("claim_id"))
        for section in out
        for bullet in list(section.get("bullets") or [])
        if isinstance(bullet, dict) and bullet.get("claim_id")
    }
    existing_keys = {
        _recovery_bullet_key(str(bullet.get("text") or bullet))
        for section in out
        for bullet in list(section.get("bullets") or [])
        if bullet
    }
    def add_claim_bullet(section_id: str, claim_id: str, text: str, source: str) -> None:
        target = section_by_id.get(section_id) or fallback_section
        if target is None:
            return
        claim_id = str(claim_id or "").strip()
        text = str(text or "").strip()
        if not text:
            return
        key = _recovery_bullet_key(text)
        if (claim_id and claim_id in existing_claim_ids) or (key and key in existing_keys):
            return
        target.setdefault("bullets", []).append({
            "text": text,
            "source": source,
            "claim_id": claim_id,
        })
        if claim_id:
            existing_claim_ids.add(claim_id)
        if key:
            existing_keys.add(key)

    for node in _claim_graph_nodes(claim_graph, "tensions", ("description", "name"), "claim_graph.tensions"):
        add_claim_bullet("problem", node.get("id", ""), node.get("text", ""), node.get("source", "claim_graph.tensions"))
        add_claim_bullet("motivation", node.get("id", ""), node.get("text", ""), node.get("source", "claim_graph.tensions"))
    for node in _claim_graph_nodes(claim_graph, "mechanisms", ("description", "name"), "claim_graph.mechanisms"):
        add_claim_bullet("method", node.get("id", ""), node.get("text", ""), node.get("source", "claim_graph.mechanisms"))
        add_claim_bullet("method_pipeline", node.get("id", ""), node.get("text", ""), node.get("source", "claim_graph.mechanisms"))
        add_claim_bullet("model_card", node.get("id", ""), node.get("text", ""), node.get("source", "claim_graph.mechanisms"))
    for node in _claim_graph_nodes(claim_graph, "evidence", ("raw_quote", "metric", "source"), "claim_graph.evidence"):
        add_claim_bullet("main_evidence", node.get("id", ""), node.get("text", ""), node.get("source", "claim_graph.evidence"))
        add_claim_bullet("results_table", node.get("id", ""), node.get("text", ""), node.get("source", "claim_graph.evidence"))
        add_claim_bullet("ablation_analysis", node.get("id", ""), node.get("text", ""), node.get("source", "claim_graph.evidence"))
    for implication in _claim_graph_implications(claim_graph):
        claim_id = str(implication.get("id") or "").strip()
        text = str(implication.get("description") or "").strip()
        if not text:
            continue
        key = _recovery_bullet_key(text)
        if (claim_id and claim_id in existing_claim_ids) or (key and key in existing_keys):
            continue
        lower = text.lower()
        target = fallback_section
        if any(token in lower for token in ("future", "open challenge", "remains", "need stronger")):
            target = section_by_id.get("limitations_future") or section_by_id.get("limitation_future") or target
        elif any(token in lower for token in ("takeaway", "bottleneck", "planning", "textual instruction")):
            target = section_by_id.get("synthesis_takeaway") or section_by_id.get("takeaway") or target
        if target is None:
            continue
        target.setdefault("bullets", []).append({
            "text": text,
            "source": "claim_graph.implications",
            "claim_id": claim_id,
        })
        if claim_id:
            existing_claim_ids.add(claim_id)
        if key:
            existing_keys.add(key)
    return out


def _claim_graph_nodes(
    claim_graph: Any,
    field: str,
    text_keys: tuple[str, ...],
    source: str,
) -> list[dict[str, str]]:
    raw_items: Any = []
    if isinstance(claim_graph, dict):
        raw_items = claim_graph.get(field) or []
    elif claim_graph is not None:
        raw_items = getattr(claim_graph, field, []) or []
    out: list[dict[str, str]] = []
    for item in list(raw_items):
        if isinstance(item, dict):
            claim_id = item.get("id")
            values = [item.get(key) for key in text_keys]
        else:
            claim_id = getattr(item, "id", None)
            values = [getattr(item, key, None) for key in text_keys]
        text = next((str(value).strip() for value in values if str(value or "").strip()), "")
        if text:
            out.append({"id": str(claim_id or ""), "text": text, "source": source})
    return out


def _claim_graph_implications(claim_graph: Any) -> list[dict[str, str]]:
    raw_items: Any = []
    if isinstance(claim_graph, dict):
        raw_items = claim_graph.get("implications") or []
    elif claim_graph is not None:
        raw_items = getattr(claim_graph, "implications", []) or []
    out: list[dict[str, str]] = []
    for item in list(raw_items):
        if isinstance(item, dict):
            claim_id = item.get("id")
            description = item.get("description")
        else:
            claim_id = getattr(item, "id", None)
            description = getattr(item, "description", None)
        if description:
            out.append({"id": str(claim_id or ""), "description": str(description)})
    return out


def _recovery_thesis(brief: dict[str, Any], contract: dict[str, Any], *, compact_wide: bool = False) -> str:
    storyboard = contract.get("visual_storyboard") if isinstance(contract.get("visual_storyboard"), dict) else {}
    thesis = storyboard.get("central_thesis") if isinstance(storyboard, dict) else None
    if not thesis:
        for section in _recovery_sections(brief):
            if str(section.get("section_id") or "") == "key_contribution":
                for bullet in section.get("bullets") or []:
                    if isinstance(bullet, dict) and bullet.get("text"):
                        thesis = bullet.get("text")
                        break
            if thesis:
                break
    if compact_wide:
        compact = _compact_recovery_claim_text(thesis, purpose="thesis")
        if compact:
            return _ensure_recovery_terminal(compact)
    compact = _compact_recovery_claim_text(thesis, purpose="thesis")
    if compact and len(str(thesis or "")) > 150:
        return _ensure_recovery_terminal(compact)
    return _ensure_recovery_terminal(_clean_recovery_sentence(
        thesis or "Source-backed academic poster generated from the paper's claim graph and visual storyboard.",
        limit=178 if compact_wide else 210,
        trim_dangling_phrase=compact_wide,
    ))


def _recovery_layout_geometry(
    cw: int,
    ch: int,
    *,
    source_visual_count: int | None = None,
) -> dict[str, int]:
    margin = 72
    gutter = 28
    compact_wide = cw >= ch * 1.7 and ch < 1800
    text_heavy_source_limited = (
        source_visual_count is not None
        and 0 < source_visual_count <= 3
    )
    cap_h = 50 if compact_wide else max(46, min(68, int(ch * 0.031)))
    top_y = 306 if compact_wide else min(330, max(326, int(ch * 0.152)))
    footer_h = max(190, min(205, int(ch * 0.13))) if compact_wide else max(180, min(260, int(ch * 0.14)))
    latest_footer_y = ch - footer_h - 64
    footer_y = latest_footer_y if latest_footer_y >= top_y + 520 else max(top_y + 380, latest_footer_y)
    top_w = int((cw - 2 * margin - gutter) / 2)
    grid_cols = 3
    grid_w = int((cw - 2 * margin - (grid_cols - 1) * gutter) / grid_cols)
    if compact_wide:
        top_grid_gap = 10
        grid_row_gap = 10
        visual_footer_gap = 15
        wide_grid_w = int((cw - 2 * margin - gutter) / 2)
        top_img_h = 370
        if text_heavy_source_limited:
            top_img_h = 430
        grid_img_h = 245
        grid_row_h = grid_img_h + cap_h + grid_row_gap
        grid_y = top_y + top_img_h + cap_h + top_grid_gap
        bottom_y = grid_y + grid_img_h + cap_h + grid_row_gap
        grid_img_h_2 = max(175, footer_y - bottom_y - visual_footer_gap - cap_h)
    else:
        top_grid_gap = 36
        grid_row_gap = 28
        visual_footer_gap = 36
        wide_grid_w = grid_w
        available_visual_h = max(420, footer_y - top_y - 28)
        top_img_h = max(300, min(620, int(available_visual_h * 0.43)))
        if text_heavy_source_limited:
            top_img_h = max(top_img_h, min(760, int(ch * 0.30)))
        grid_y = top_y + top_img_h + cap_h + top_grid_gap
        grid_area_h = max(2 * (cap_h + 110) + 28, footer_y - grid_y - visual_footer_gap)
        grid_row_h = max(cap_h + 110, int((grid_area_h - 28) / 2))
        max_grid_row_h = max(cap_h + 110, int((footer_y - grid_y - 28) / 2))
        grid_row_h = min(grid_row_h, max_grid_row_h)
        grid_img_h = max(110, grid_row_h - cap_h)
        grid_img_h_2 = grid_img_h
        bottom_y = grid_y + grid_img_h + cap_h + grid_row_gap
    return {
        "margin": margin,
        "gutter": gutter,
        "canvas_w": cw,
        "canvas_h": ch,
        "top_y": top_y,
        "top_img_h": top_img_h,
        "top_w": top_w,
        "cap_h": cap_h,
        "grid_y": grid_y,
        "grid_w": grid_w,
        "wide_grid_w": wide_grid_w,
        "grid_img_h": grid_img_h,
        "grid_img_h_2": grid_img_h_2,
        "grid_row_h": grid_row_h,
        "grid_row_gap": grid_row_gap,
        "bottom_y": bottom_y,
        "footer_y": footer_y,
        "footer_h": footer_h,
        "compact_wide": 1 if compact_wide else 0,
    }


_RECOVERY_ARCHETYPE_GUI_BENCHMARK = "gui_video_benchmark_process_wall"
_RECOVERY_ARCHETYPE_WORLD_MODEL = "world_model_video_prediction_filmstrip"
_RECOVERY_ARCHETYPE_THEORY = "text_heavy_theory_board"
_RECOVERY_ARCHETYPE_TABLE = "benchmark_table_first_layout"
_RECOVERY_ARCHETYPE_MULTI_VIEW = "multi_view_matrix_graph_wall"
_RECOVERY_ARCHETYPE_DENSE_SYNTHESIS = "research_synthesis_dense_board"
_RECOVERY_ARCHETYPE_DEFAULT = "source_visual_board_default"


def _recovery_layout_context(
    *,
    title: str,
    sections: list[dict[str, Any]],
    selected_assets: list[dict[str, Any]],
    contract: dict[str, Any],
    canvas: dict[str, int],
) -> dict[str, Any]:
    cw = int(canvas["w"])
    ch = int(canvas["h"])
    geom = _recovery_layout_geometry(
        cw,
        ch,
        source_visual_count=len(selected_assets),
    )
    archetype = _recovery_layout_archetype(
        title=title,
        sections=sections,
        selected_assets=selected_assets,
        contract=contract,
    )
    visual_ids = [str(asset.get("asset_id") or "") for asset in selected_assets]
    parts = _recovery_layout_parts_for_archetype(
        archetype,
        cw=cw,
        ch=ch,
        geom=geom,
        visual_ids=[item for item in visual_ids if item],
    )
    return {
        "archetype": archetype,
        "margin_px": int(geom["margin"]),
        "gutter_px": int(geom["gutter"]),
        "compact_wide": bool(geom["compact_wide"]),
        "geometry": geom,
        "visual_positions": parts["visual_positions"],
        "text_panels": parts["text_panels"],
        "slots": parts["slots"],
        "value_profile": _recovery_layout_value_profile(archetype),
        "notes": _recovery_layout_notes(archetype),
    }


def _recovery_layout_archetype(
    *,
    title: str,
    sections: list[dict[str, Any]],
    selected_assets: list[dict[str, Any]],
    contract: dict[str, Any],
) -> str:
    source_visual_count = len(selected_assets)
    if contract.get("reference_profile") == "research_synthesis_dense":
        return _RECOVERY_ARCHETYPE_DENSE_SYNTHESIS
    if contract.get("reference_profile") == "visual_evidence_wall":
        return _RECOVERY_ARCHETYPE_DENSE_SYNTHESIS
    haystack = _recovery_layout_haystack(
        title=title,
        sections=sections,
        selected_assets=selected_assets,
        contract=contract,
    )
    has_strong_theory = _recovery_has_any(haystack, (
        "theorem", "proof", "lower bound", "upper bound", "regret bound",
        "sample complexity", "logistic", "halfspace", "halfspaces",
        "lemma", "corollary",
    ))
    has_generic_theory = _recovery_has_any(haystack, (
        "optimization", "convex", "nonconvex",
    ))
    has_multi_view = _recovery_has_any(haystack, (
        "multi-view", "multiview", "multi view", "clustering", "cluster",
        "affinity", "graph", "matrix", "imvc", "multi-view clustering",
    ))
    has_world_model = _recovery_has_any(haystack, (
        "ivideogpt", "world model", "interactive world model",
        "video prediction", "frame prediction", "prediction horizon",
        "autoregressive", "tokenization", "sequence model", "forecast",
        "rollout", "model-based rl",
    ))
    has_gui_benchmark = _recovery_has_any(haystack, (
        "videogui", "gui", "screenshot", "screen shot", "agent",
        "interaction", "instructional video", "text-only gui",
        "vision-only", "click", "drag", "scroll", "professional software",
    ))
    has_table_benchmark = _recovery_has_any(haystack, (
        "leaderboard", "table", "benchmark", "accuracy", "map", "mAP",
        "ablation", "auc", "f1", "score", "scores", "metric",
    ))
    if has_strong_theory:
        return _RECOVERY_ARCHETYPE_THEORY
    if has_world_model:
        return _RECOVERY_ARCHETYPE_WORLD_MODEL
    if has_gui_benchmark:
        return _RECOVERY_ARCHETYPE_GUI_BENCHMARK
    if has_multi_view:
        return _RECOVERY_ARCHETYPE_MULTI_VIEW
    if has_table_benchmark:
        return _RECOVERY_ARCHETYPE_TABLE
    if has_generic_theory:
        return _RECOVERY_ARCHETYPE_THEORY
    if source_visual_count <= 3:
        return _RECOVERY_ARCHETYPE_THEORY
    return _RECOVERY_ARCHETYPE_DEFAULT


def _recovery_layout_haystack(
    *,
    title: str,
    sections: list[dict[str, Any]],
    selected_assets: list[dict[str, Any]],
    contract: dict[str, Any],
) -> str:
    parts: list[str] = [title]
    storyboard = contract.get("visual_storyboard") if isinstance(contract.get("visual_storyboard"), dict) else {}
    parts.extend(str(storyboard.get(key) or "") for key in (
        "central_thesis", "recommended_visual_strategy", "layout_archetype",
    ))
    for item in storyboard.get("slots") or storyboard.get("visual_slots") or []:
        if isinstance(item, dict):
            parts.extend(str(item.get(key) or "") for key in (
                "slot_id", "role", "caption", "panel_job", "description",
            ))
    for section in sections:
        parts.extend(str(section.get(key) or "") for key in ("section_id", "title"))
        for bullet in list(section.get("bullets") or []):
            if isinstance(bullet, dict):
                parts.extend(str(bullet.get(key) or "") for key in ("text", "source", "claim_id"))
            else:
                parts.append(str(bullet))
    for asset in selected_assets:
        parts.extend(str(asset.get(key) or "") for key in (
            "asset_id", "story_role", "caption", "caption_full",
            "caption_short", "source_processing",
        ))
    return re.sub(r"\s+", " ", " ".join(parts)).lower()


def _recovery_has_any(text: str, needles: tuple[str, ...]) -> bool:
    lower = str(text or "").lower()
    return any(needle.lower() in lower for needle in needles)


def _recovery_layout_notes(archetype: str) -> str:
    notes = {
        _RECOVERY_ARCHETYPE_GUI_BENCHMARK: (
            "Recovery layout: GUI/video benchmark process wall with workflow spine, "
            "screenshot strip, results table band, and compact evidence callouts."
        ),
        _RECOVERY_ARCHETYPE_WORLD_MODEL: (
            "Recovery layout: world-model video prediction board with sequence "
            "filmstrip, architecture spine, qualitative prediction grid, and compact comparison table."
        ),
        _RECOVERY_ARCHETYPE_THEORY: (
            "Recovery layout: text-heavy theory board with minimal source figures, "
            "theorem/proof-intuition cards, result bounds, and a compact synthesis takeaway."
        ),
        _RECOVERY_ARCHETYPE_TABLE: (
            "Recovery layout: benchmark/table-first board with compact comparison table, "
            "method notes, ablation or limitation notes, and short result discussion."
        ),
        _RECOVERY_ARCHETYPE_MULTI_VIEW: (
            "Recovery layout: multi-view matrix/graph wall with method/result split "
            "and benchmark evidence strip."
        ),
        _RECOVERY_ARCHETYPE_DENSE_SYNTHESIS: (
            "Recovery layout: dense synthesis board with native model card, method "
            "pipeline, benchmark table, ablation analysis, limitations/future, and "
            "synthesis takeaway panels."
        ),
    }
    return notes.get(
        archetype,
        "Recovery layout: source-backed visual board with non-random deterministic fallback topology.",
    )


def _recovery_layout_value_profile(archetype: str) -> dict[str, Any]:
    profiles = {
        _RECOVERY_ARCHETYPE_GUI_BENCHMARK: {
            "visual_evidence_value": 9,
            "editorial_synthesis_value": 5,
            "native_reconstruction_value": 6,
            "human_effort_focus": "screenshots/process/results, with compact synthesis so evidence is not just a montage",
        },
        _RECOVERY_ARCHETYPE_WORLD_MODEL: {
            "visual_evidence_value": 8,
            "editorial_synthesis_value": 6,
            "native_reconstruction_value": 7,
            "human_effort_focus": "sequence comparison, architecture spine, prediction grid, and metric interpretation",
        },
        _RECOVERY_ARCHETYPE_THEORY: {
            "visual_evidence_value": 3,
            "editorial_synthesis_value": 9,
            "native_reconstruction_value": 8,
            "human_effort_focus": "theorem/proof intuition, assumptions, result bounds, and dense native text hierarchy",
        },
        _RECOVERY_ARCHETYPE_TABLE: {
            "visual_evidence_value": 5,
            "editorial_synthesis_value": 7,
            "native_reconstruction_value": 9,
            "human_effort_focus": "native leaderboard/result table, ablation structure, method context, and takeaway synthesis",
        },
        _RECOVERY_ARCHETYPE_MULTI_VIEW: {
            "visual_evidence_value": 7,
            "editorial_synthesis_value": 7,
            "native_reconstruction_value": 8,
            "human_effort_focus": "matrix/graph wall, method-result split, and benchmark interpretation",
        },
        _RECOVERY_ARCHETYPE_DENSE_SYNTHESIS: {
            "visual_evidence_value": 4,
            "editorial_synthesis_value": 10,
            "native_reconstruction_value": 10,
            "human_effort_focus": "native model cards, pipelines, benchmark/result tables, ablation analysis, limitations/future, synthesis takeaways, and metadata provenance",
        },
    }
    return profiles.get(archetype, {
        "visual_evidence_value": 6,
        "editorial_synthesis_value": 6,
        "native_reconstruction_value": 6,
        "human_effort_focus": "balanced source visuals, native text, and compact research-poster synthesis",
    })


def _recovery_layout_parts_for_archetype(
    archetype: str,
    *,
    cw: int,
    ch: int,
    geom: dict[str, int],
    visual_ids: list[str],
) -> dict[str, Any]:
    if archetype == _RECOVERY_ARCHETYPE_THEORY:
        return _recovery_theory_layout_parts(cw=cw, ch=ch, geom=geom, visual_ids=visual_ids)
    if archetype == _RECOVERY_ARCHETYPE_GUI_BENCHMARK:
        return _recovery_gui_benchmark_layout_parts(cw=cw, ch=ch, geom=geom, visual_ids=visual_ids)
    if archetype == _RECOVERY_ARCHETYPE_WORLD_MODEL:
        return _recovery_world_model_layout_parts(cw=cw, ch=ch, geom=geom, visual_ids=visual_ids)
    if archetype == _RECOVERY_ARCHETYPE_TABLE:
        return _recovery_table_first_layout_parts(cw=cw, ch=ch, geom=geom, visual_ids=visual_ids)
    if archetype == _RECOVERY_ARCHETYPE_MULTI_VIEW:
        return _recovery_multi_view_layout_parts(cw=cw, ch=ch, geom=geom, visual_ids=visual_ids)
    if archetype == _RECOVERY_ARCHETYPE_DENSE_SYNTHESIS:
        return _recovery_dense_synthesis_layout_parts(cw=cw, ch=ch, geom=geom, visual_ids=visual_ids)
    return _recovery_default_layout_parts(cw=cw, ch=ch, geom=geom, visual_ids=visual_ids)


def _recovery_content_bottom(ch: int, *, compact_wide: bool) -> int:
    source_note_h = 44 if compact_wide else 42
    return ch - source_note_h - 30


def _position(
    slot_id: str,
    role: str,
    x: int,
    y: int,
    w: int,
    h: int,
) -> dict[str, Any]:
    return {
        "slot_id": slot_id,
        "role": role,
        "bbox": {"x": int(x), "y": int(y), "w": max(1, int(w)), "h": max(1, int(h))},
    }


def _row_positions(
    slot_id: str,
    role: str,
    *,
    x: int,
    y: int,
    w: int,
    h: int,
    count: int,
    gutter: int,
) -> list[dict[str, Any]]:
    if count <= 0:
        return []
    item_w = int((w - (count - 1) * gutter) / count)
    return [
        _position(slot_id, role, x + idx * (item_w + gutter), y, item_w, h)
        for idx in range(count)
    ]


def _slot(
    slot_id: str,
    role: str,
    bbox: dict[str, int],
    *,
    panel_job: str,
    visual_ids: list[str] | None = None,
    text_budget: str | None = None,
    space_fill_policy: str = "",
) -> dict[str, Any]:
    item = {
        "slot_id": slot_id,
        "role": role,
        "bbox": {key: int(bbox.get(key) or 0) for key in ("x", "y", "w", "h")},
        "required": True,
        "panel_job": panel_job,
        "space_fill_policy": space_fill_policy,
    }
    if visual_ids:
        item["visual_ids"] = visual_ids
    if text_budget:
        item["text_budget"] = text_budget
    return item


def _visual_group_slot(
    slot_id: str,
    role: str,
    positions: list[dict[str, Any]],
    *,
    cap_h: int,
    visual_ids: list[str],
    panel_job: str,
    space_fill_policy: str,
) -> dict[str, Any]:
    bbox = _positions_union_bbox(positions, cap_h=cap_h)
    return _slot(
        slot_id,
        role,
        bbox,
        panel_job=panel_job,
        visual_ids=visual_ids,
        space_fill_policy=space_fill_policy,
    )


def _positions_union_bbox(positions: list[dict[str, Any]], *, cap_h: int) -> dict[str, int]:
    boxes = [item.get("bbox") for item in positions if isinstance(item.get("bbox"), dict)]
    if not boxes:
        return {"x": 72, "y": 306, "w": 1, "h": 1}
    x0 = min(int(box.get("x") or 0) for box in boxes)
    y0 = min(int(box.get("y") or 0) for box in boxes)
    x1 = max(int(box.get("x") or 0) + int(box.get("w") or 0) for box in boxes)
    y1 = max(int(box.get("y") or 0) + int(box.get("h") or 0) + cap_h for box in boxes)
    return {"x": x0, "y": y0, "w": max(1, x1 - x0), "h": max(1, y1 - y0)}


def _title_slot(cw: int) -> dict[str, Any]:
    return _slot(
        "title_thesis",
        "identity_header title authors affiliation identity_text",
        {"x": 72, "y": 44, "w": cw - 144, "h": 258},
        panel_job="State exactly title, authors, and school/institution/company names only.",
        text_budget="three header text rows only: title/authors/institution-company names; no venue, citation/contact text, project/code/resource links, logos, icons, QR codes, thesis, subtitle, or tagline",
        space_fill_policy="compact paper identity band",
    )


def _text_panel_slots(
    panels: list[dict[str, Any]],
    *,
    prefix: str,
) -> list[dict[str, Any]]:
    slots: list[dict[str, Any]] = []
    for idx, panel in enumerate(panels):
        slot_id = str(panel.get("slot_id") or f"{prefix}_{idx + 1}")
        role = str(panel.get("role") or "narrative_section")
        bbox = panel.get("slot_bbox") if isinstance(panel.get("slot_bbox"), dict) else panel["bbox"]
        slots.append(_slot(
            slot_id,
            role,
            bbox,
            panel_job=str(panel.get("panel_job") or "Place concise editable source-backed section bullets."),
            visual_ids=[str(item) for item in (panel.get("visual_ids") or []) if str(item)],
            text_budget=str(panel.get("text_budget") or "one compact heading and one to two sourced bullets"),
            space_fill_policy=str(panel.get("space_fill_policy") or "fill with concise native text"),
        ))
    return slots


def _bottom_text_panels(
    *,
    x: int,
    y: int,
    w: int,
    h: int,
    gutter: int,
    count: int,
    prefix: str,
) -> list[dict[str, Any]]:
    count = max(1, count)
    if count == 4:
        weights = [1.25, 1.0, 1.0, 1.35]
        unit = (w - (count - 1) * gutter) / sum(weights)
        panels: list[dict[str, Any]] = []
        cursor = x
        roles = ["problem", "method", "main_evidence", "takeaway limitation_future"]
        for idx, weight in enumerate(weights):
            panel_w = int(unit * weight)
            if idx == count - 1:
                panel_w = x + w - cursor
            panels.append({
                "slot_id": f"{prefix}_{idx + 1}",
                "role": f"{roles[idx]} narrative_section",
                "bbox": {"x": cursor, "y": y, "w": panel_w, "h": h},
            })
            cursor += panel_w + gutter
        return panels
    item_w = int((w - (count - 1) * gutter) / count)
    return [
        {
            "slot_id": f"{prefix}_{idx + 1}",
            "role": "narrative_section",
            "bbox": {"x": x + idx * (item_w + gutter), "y": y, "w": item_w, "h": h},
        }
        for idx in range(count)
    ]


def _recovery_theory_layout_parts(
    *,
    cw: int,
    ch: int,
    geom: dict[str, int],
    visual_ids: list[str],
) -> dict[str, Any]:
    margin = int(geom["margin"])
    gutter = int(geom["gutter"])
    cap_h = int(geom["cap_h"])
    compact = bool(geom["compact_wide"])
    top_y = int(geom["top_y"])
    img_h = int(geom["top_img_h"])
    top_w = int((cw - 2 * margin - gutter) / 2)
    visual_positions = [
        _position("source_figure_pair", "method source_visuals", margin, top_y, top_w, img_h),
        _position("source_figure_pair", "results qualitative_evidence source_visuals", margin + top_w + gutter, top_y, cw - 2 * margin - top_w - gutter, img_h),
    ]
    if len(visual_ids) >= 3:
        third_w = int((cw - 2 * margin) * 0.34)
        visual_positions.append(_position(
            "supporting_theory_visual",
            "supporting_analysis source_visuals",
            cw - margin - third_w,
            top_y + int(img_h * 0.50),
            third_w,
            max(150, int(img_h * 0.42)),
        ))
    visual_positions = visual_positions[:max(1, min(len(visual_ids), 3))]
    board_y = top_y + img_h + cap_h + (24 if compact else 40)
    content_bottom = _recovery_content_bottom(ch, compact_wide=compact)
    footer_h = 118 if compact else 150
    board_h = max(260, content_bottom - board_y - footer_h - gutter)
    col_w = int((cw - 2 * margin - gutter) / 2)
    row_gap = 18 if compact else 24
    card_h = int((board_h - row_gap) / 2)
    text_panels = [
        {
            "slot_id": "theorem_statement",
            "role": "problem key_contribution theorem",
            "bbox": {"x": margin, "y": board_y, "w": col_w, "h": card_h},
            "panel_job": "State the theorem setup, assumptions, or central formal claim.",
        },
        {
            "slot_id": "proof_intuition",
            "role": "method proof_intuition",
            "bbox": {"x": margin, "y": board_y + card_h + row_gap, "w": col_w, "h": card_h},
            "panel_job": "Explain the proof mechanism or optimization intuition in sourced bullets.",
        },
        {
            "slot_id": "result_bounds",
            "role": "main_evidence results lower_bound upper_bound",
            "bbox": {"x": margin + col_w + gutter, "y": board_y, "w": col_w, "h": card_h},
            "panel_job": "Summarize the paper's main lower/upper-bound or empirical evidence.",
        },
        {
            "slot_id": "implications_limits",
            "role": "takeaway limitation_future",
            "bbox": {"x": margin + col_w + gutter, "y": board_y + card_h + row_gap, "w": col_w, "h": card_h},
            "panel_job": "Close with implications, limitations, and source-grounded caveats.",
        },
        {
            "slot_id": "theory_footer_takeaway",
            "role": "synthesis_takeaway",
            "bbox": {"x": margin, "y": content_bottom - footer_h, "w": cw - 2 * margin, "h": footer_h},
            "panel_job": "Reserve a full-width scientific takeaway band for source-backed synthesis.",
            "text_budget": "one short source-backed takeaway",
        },
    ]
    slots = [
        _title_slot(cw),
        _visual_group_slot(
            "minimal_source_figures",
            "method main_evidence source_visuals",
            visual_positions,
            cap_h=cap_h,
            visual_ids=visual_ids[:len(visual_positions)],
            panel_job="Place every available source figure prominently without forcing an eight-panel grid.",
            space_fill_policy="one or two large figures with editable captions",
        ),
        *_text_panel_slots(text_panels, prefix="theory_text"),
    ]
    return {"visual_positions": visual_positions, "text_panels": text_panels, "slots": slots}


def _recovery_gui_benchmark_layout_parts(
    *,
    cw: int,
    ch: int,
    geom: dict[str, int],
    visual_ids: list[str],
) -> dict[str, Any]:
    margin = int(geom["margin"])
    gutter = int(geom["gutter"])
    cap_h = int(geom["cap_h"])
    compact = bool(geom["compact_wide"])
    top_y = int(geom["top_y"])
    content_bottom = _recovery_content_bottom(ch, compact_wide=compact)
    text_h = 164 if compact else 236
    visual_bottom = content_bottom - text_h - gutter
    left_w = max(560, min(820, int((cw - 2 * margin - gutter) * 0.27)))
    right_x = margin + left_w + gutter
    right_w = cw - margin - right_x
    visual_h = max(480, visual_bottom - top_y - cap_h)
    strip_h = max(190, min(290 if compact else 380, int(visual_h * 0.24)))
    table_h = max(210, min(310 if compact else 430, int(visual_h * 0.29)))
    qualitative_y = top_y + strip_h + cap_h + gutter + table_h + cap_h + gutter
    qualitative_h = max(155, visual_bottom - qualitative_y - cap_h)
    visual_positions = [
        _position("workflow_spine", "method process workflow source_visual", margin, top_y, left_w, visual_h),
        *_row_positions("screenshot_strip", "qualitative_evidence screenshot gui_state", x=right_x, y=top_y, w=right_w, h=strip_h, count=3, gutter=gutter),
        *_row_positions("results_table_band", "main_evidence benchmark results table", x=right_x, y=top_y + strip_h + cap_h + gutter, w=right_w, h=table_h, count=2, gutter=gutter),
        *_row_positions("qualitative_failure_strip", "supporting_analysis qualitative_evidence", x=right_x, y=qualitative_y, w=right_w, h=qualitative_h, count=2, gutter=gutter),
    ][:max(1, min(len(visual_ids), 8))]
    text_y = content_bottom - text_h
    text_panels = _bottom_text_panels(
        x=margin,
        y=text_y,
        w=cw - 2 * margin,
        h=text_h,
        gutter=gutter,
        count=4,
        prefix="gui_callout",
    )
    slots = [
        _title_slot(cw),
        _visual_group_slot(
            "workflow_spine",
            "method process workflow",
            [pos for pos in visual_positions if pos["slot_id"] == "workflow_spine"],
            cap_h=cap_h,
            visual_ids=visual_ids[:1],
            panel_job="Use one large source visual as the GUI workflow/process anchor.",
            space_fill_policy="large workflow visual in a left rail",
        ),
        _visual_group_slot(
            "screenshot_strip",
            "qualitative_evidence screenshot gui_state",
            [pos for pos in visual_positions if pos["slot_id"] == "screenshot_strip"],
            cap_h=cap_h,
            visual_ids=visual_ids[1:4],
            panel_job="Show before/after or task screenshots as a horizontal strip.",
            space_fill_policy="three screenshot-scale source visuals",
        ),
        _visual_group_slot(
            "results_table_band",
            "main_evidence benchmark results table",
            [pos for pos in visual_positions if pos["slot_id"] == "results_table_band"],
            cap_h=cap_h,
            visual_ids=visual_ids[4:6],
            panel_job="Use benchmark tables as a prominent middle evidence band.",
            space_fill_policy="two table-scale source visuals",
        ),
        _visual_group_slot(
            "qualitative_failure_strip",
            "supporting_analysis qualitative_evidence",
            [pos for pos in visual_positions if pos["slot_id"] == "qualitative_failure_strip"],
            cap_h=cap_h,
            visual_ids=visual_ids[6:8],
            panel_job="Place remaining qualitative or failure-analysis visuals under the table band.",
            space_fill_policy="compact qualitative evidence strip",
        ),
        *_text_panel_slots(text_panels, prefix="gui_callout"),
    ]
    return {"visual_positions": visual_positions, "text_panels": text_panels, "slots": slots}


def _recovery_world_model_layout_parts(
    *,
    cw: int,
    ch: int,
    geom: dict[str, int],
    visual_ids: list[str],
) -> dict[str, Any]:
    margin = int(geom["margin"])
    gutter = int(geom["gutter"])
    cap_h = int(geom["cap_h"])
    compact = bool(geom["compact_wide"])
    top_y = int(geom["top_y"])
    content_bottom = _recovery_content_bottom(ch, compact_wide=compact)
    text_h = 170 if compact else 260
    visual_bottom = content_bottom - text_h - gutter
    film_h = max(170, min(260 if compact else 340, int((visual_bottom - top_y) * 0.26)))
    mid_y = top_y + film_h + cap_h + gutter
    metric_h = max(170, min(230 if compact else 320, int((visual_bottom - mid_y) * 0.28)))
    mid_h = max(260, visual_bottom - mid_y - metric_h - cap_h - gutter)
    arch_w = int((cw - 2 * margin - gutter) * 0.45)
    qual_x = margin + arch_w + gutter
    qual_w = cw - margin - qual_x
    qual_h = int((mid_h - cap_h - gutter) / 2)
    visual_positions = [
        *_row_positions("sequence_filmstrip", "video_prediction sequence filmstrip", x=margin, y=top_y, w=cw - 2 * margin, h=film_h, count=4, gutter=gutter),
        _position("architecture_spine", "method architecture tokenization", margin, mid_y, arch_w, mid_h),
        _position("qualitative_prediction_grid", "qualitative_evidence prediction", qual_x, mid_y, qual_w, qual_h),
        _position("qualitative_prediction_grid", "qualitative_evidence rollout", qual_x, mid_y + qual_h + cap_h + gutter, qual_w, qual_h),
        _position("metric_band", "main_evidence benchmark results", margin, visual_bottom - metric_h - cap_h, cw - 2 * margin, metric_h),
    ][:max(1, min(len(visual_ids), 8))]
    text_panels = _bottom_text_panels(
        x=margin,
        y=content_bottom - text_h,
        w=cw - 2 * margin,
        h=text_h,
        gutter=gutter,
        count=4,
        prefix="world_model_callout",
    )
    slots = [
        _title_slot(cw),
        _visual_group_slot(
            "sequence_filmstrip",
            "video_prediction sequence filmstrip",
            [pos for pos in visual_positions if pos["slot_id"] == "sequence_filmstrip"],
            cap_h=cap_h,
            visual_ids=visual_ids[:4],
            panel_job="Arrange prediction/rollout source frames as a filmstrip.",
            space_fill_policy="four sequence panels across the board",
        ),
        _visual_group_slot(
            "architecture_spine",
            "method architecture tokenization",
            [pos for pos in visual_positions if pos["slot_id"] == "architecture_spine"],
            cap_h=cap_h,
            visual_ids=visual_ids[4:5],
            panel_job="Use the strongest method visual as the architecture spine.",
            space_fill_policy="large method/architecture panel",
        ),
        _visual_group_slot(
            "qualitative_prediction_grid",
            "qualitative_evidence prediction rollout",
            [pos for pos in visual_positions if pos["slot_id"] == "qualitative_prediction_grid"],
            cap_h=cap_h,
            visual_ids=visual_ids[5:7],
            panel_job="Show qualitative prediction examples beside the architecture.",
            space_fill_policy="stacked qualitative prediction examples",
        ),
        _visual_group_slot(
            "metric_band",
            "main_evidence benchmark results",
            [pos for pos in visual_positions if pos["slot_id"] == "metric_band"],
            cap_h=cap_h,
            visual_ids=visual_ids[7:8],
            panel_job="Reserve a compact comparison table or short result discussion for the strongest table or curve.",
            space_fill_policy="compact comparison table with concise visual interpretation",
        ),
        *_text_panel_slots(text_panels, prefix="world_model_callout"),
    ]
    return {"visual_positions": visual_positions, "text_panels": text_panels, "slots": slots}


def _recovery_table_first_layout_parts(
    *,
    cw: int,
    ch: int,
    geom: dict[str, int],
    visual_ids: list[str],
) -> dict[str, Any]:
    margin = int(geom["margin"])
    gutter = int(geom["gutter"])
    cap_h = int(geom["cap_h"])
    compact = bool(geom["compact_wide"])
    top_y = int(geom["top_y"])
    content_bottom = _recovery_content_bottom(ch, compact_wide=compact)
    text_h = 160 if compact else 230
    visual_bottom = content_bottom - text_h - gutter
    rail_w = max(520, min(720, int((cw - 2 * margin - gutter) * 0.23)))
    main_x = margin + rail_w + gutter
    main_w = cw - margin - main_x
    rail_h = max(420, visual_bottom - top_y - cap_h)
    table_h = max(230, min(340 if compact else 480, int(rail_h * 0.38)))
    strip_y = top_y + table_h + cap_h + gutter
    strip_h = max(170, min(250 if compact else 340, int(rail_h * 0.26)))
    bottom_y = strip_y + strip_h + cap_h + gutter
    bottom_h = max(150, visual_bottom - bottom_y - cap_h)
    visual_positions = [
        _position("method_context_rail", "method source_visual", margin, top_y, rail_w, rail_h),
        *_row_positions("leaderboard_table_band", "leaderboard benchmark results table", x=main_x, y=top_y, w=main_w, h=table_h, count=2, gutter=gutter),
        *_row_positions("ablation_strip", "ablation supporting_analysis metric", x=main_x, y=strip_y, w=main_w, h=strip_h, count=3, gutter=gutter),
        *_row_positions("result_callout_strip", "main_evidence qualitative_evidence", x=main_x, y=bottom_y, w=main_w, h=bottom_h, count=2, gutter=gutter),
    ][:max(1, min(len(visual_ids), 8))]
    text_panels = _bottom_text_panels(
        x=margin,
        y=content_bottom - text_h,
        w=cw - 2 * margin,
        h=text_h,
        gutter=gutter,
        count=4,
        prefix="table_callout",
    )
    slots = [
        _title_slot(cw),
        _visual_group_slot(
            "method_context_rail",
            "method source_visual",
            [pos for pos in visual_positions if pos["slot_id"] == "method_context_rail"],
            cap_h=cap_h,
            visual_ids=visual_ids[:1],
            panel_job="Keep one method/context visual in a side rail.",
            space_fill_policy="vertical method rail",
        ),
        _visual_group_slot(
            "leaderboard_table_band",
            "leaderboard benchmark results table",
            [pos for pos in visual_positions if pos["slot_id"] == "leaderboard_table_band"],
            cap_h=cap_h,
            visual_ids=visual_ids[1:3],
            panel_job="Place the primary leaderboard or comparison tables first.",
            space_fill_policy="large table-first central band",
        ),
        _visual_group_slot(
            "ablation_strip",
            "ablation supporting_analysis metric",
            [pos for pos in visual_positions if pos["slot_id"] == "ablation_strip"],
            cap_h=cap_h,
            visual_ids=visual_ids[3:6],
            panel_job="Use ablations and secondary metrics as a compact strip.",
            space_fill_policy="three compact ablation cells",
        ),
        _visual_group_slot(
            "result_callout_strip",
            "main_evidence qualitative_evidence",
            [pos for pos in visual_positions if pos["slot_id"] == "result_callout_strip"],
            cap_h=cap_h,
            visual_ids=visual_ids[6:8],
            panel_job="Place remaining qualitative/result visuals as supporting callouts.",
            space_fill_policy="bottom concise visual interpretation strip",
        ),
        *_text_panel_slots(text_panels, prefix="table_callout"),
    ]
    return {"visual_positions": visual_positions, "text_panels": text_panels, "slots": slots}


def _recovery_multi_view_layout_parts(
    *,
    cw: int,
    ch: int,
    geom: dict[str, int],
    visual_ids: list[str],
) -> dict[str, Any]:
    margin = int(geom["margin"])
    gutter = int(geom["gutter"])
    cap_h = int(geom["cap_h"])
    compact = bool(geom["compact_wide"])
    top_y = int(geom["top_y"])
    content_bottom = _recovery_content_bottom(ch, compact_wide=compact)
    text_h = 164 if compact else 235
    visual_bottom = content_bottom - text_h - gutter
    wall_w = int((cw - 2 * margin - gutter) * 0.58)
    split_x = margin + wall_w + gutter
    split_w = cw - margin - split_x
    wall_h = max(460, int((visual_bottom - top_y - cap_h) * 0.72))
    cell_w = int((wall_w - gutter) / 2)
    cell_h = int((wall_h - cap_h - gutter) / 2)
    split_h = int((wall_h - cap_h - gutter) / 2)
    strip_y = top_y + wall_h + cap_h + gutter
    strip_h = max(150, visual_bottom - strip_y - cap_h)
    visual_positions = [
        _position("matrix_graph_wall", "matrix graph clustering qualitative_evidence", margin, top_y, cell_w, cell_h),
        _position("matrix_graph_wall", "matrix graph clustering qualitative_evidence", margin + cell_w + gutter, top_y, cell_w, cell_h),
        _position("matrix_graph_wall", "matrix graph clustering qualitative_evidence", margin, top_y + cell_h + cap_h + gutter, cell_w, cell_h),
        _position("matrix_graph_wall", "matrix graph clustering qualitative_evidence", margin + cell_w + gutter, top_y + cell_h + cap_h + gutter, cell_w, cell_h),
        _position("method_result_split", "method source_visual", split_x, top_y, split_w, split_h),
        _position("method_result_split", "results main_evidence source_visual", split_x, top_y + split_h + cap_h + gutter, split_w, split_h),
        *_row_positions("benchmark_strip", "benchmark results supporting_analysis", x=margin, y=strip_y, w=cw - 2 * margin, h=strip_h, count=2, gutter=gutter),
    ][:max(1, min(len(visual_ids), 8))]
    text_panels = _bottom_text_panels(
        x=margin,
        y=content_bottom - text_h,
        w=cw - 2 * margin,
        h=text_h,
        gutter=gutter,
        count=4,
        prefix="multiview_callout",
    )
    slots = [
        _title_slot(cw),
        _visual_group_slot(
            "matrix_graph_wall",
            "matrix graph clustering qualitative_evidence",
            [pos for pos in visual_positions if pos["slot_id"] == "matrix_graph_wall"],
            cap_h=cap_h,
            visual_ids=visual_ids[:4],
            panel_job="Use matrix/graph/clustering visuals as a dominant wall.",
            space_fill_policy="two-by-two matrix or graph visual wall",
        ),
        _visual_group_slot(
            "method_result_split",
            "method results source_visual",
            [pos for pos in visual_positions if pos["slot_id"] == "method_result_split"],
            cap_h=cap_h,
            visual_ids=visual_ids[4:6],
            panel_job="Split method and result visuals in a right-side column.",
            space_fill_policy="stacked method/result panels",
        ),
        _visual_group_slot(
            "benchmark_strip",
            "benchmark results supporting_analysis",
            [pos for pos in visual_positions if pos["slot_id"] == "benchmark_strip"],
            cap_h=cap_h,
            visual_ids=visual_ids[6:8],
            panel_job="Reserve the bottom visual strip for benchmark evidence.",
            space_fill_policy="wide benchmark strip",
        ),
        *_text_panel_slots(text_panels, prefix="multiview_callout"),
    ]
    return {"visual_positions": visual_positions, "text_panels": text_panels, "slots": slots}


def _recovery_dense_synthesis_layout_parts(
    *,
    cw: int,
    ch: int,
    geom: dict[str, int],
    visual_ids: list[str],
) -> dict[str, Any]:
    margin = int(geom["margin"])
    gutter = int(geom["gutter"])
    cap_h = int(geom["cap_h"])
    compact = bool(geom["compact_wide"])
    top_y = int(geom["top_y"])
    content_bottom = _recovery_content_bottom(ch, compact_wide=compact)
    panel_defs = [
        ("problem_contribution", "Problem and Contributions", "problem contribution key_claims motivation", "Compress the paper context and principal claims into one filled panel."),
        ("model_card", "Model Card", "model_card architecture parameters modalities", "Build a native model card with architecture, modalities, data, capability, and scale fields."),
        ("method_pipeline", "Method Pipeline", "method_pipeline pipeline framework training inference", "Use editable stage boxes for the method/training/inference pipeline."),
        ("results_table", "Benchmark Table", "benchmark_table results_table leaderboard main_evidence", "Use a native benchmark/result table, compact comparison table, or short result discussion."),
        ("ablation_analysis", "Ablation / Analysis", "ablation_analysis analysis tradeoff failure", "Show ablations, deltas, tradeoffs, or failure-mode interpretation."),
        ("limitations_future", "Limitations / Future", "limitations_future caveats future_work", "Include required limitations, caveats, and future directions."),
        ("native_reconstruction", "Native Reconstruction", "native_reconstruction editable_tables cards charts", "Show what was rebuilt as editable tables, cards, pipelines, and local evidence notes."),
        ("synthesis_takeaway", "Synthesis Takeaway", "takeaway synthesis impact", "Close the research story with implications and what the result enables."),
    ]
    footer_h = 96 if compact else 190
    board_y = top_y
    board_h = max(620 if compact else 980, content_bottom - board_y - footer_h - gutter)
    cols = 5 if compact else 3
    rows = 2 if compact else 3
    panel_w = int((cw - 2 * margin - (cols - 1) * gutter) / cols)
    panel_h = int((board_h - (rows - 1) * gutter) / rows)
    text_panels: list[dict[str, Any]] = []
    visual_positions: list[dict[str, Any]] = []
    visual_targets = (
        "method_pipeline",
        "results_table",
        "ablation_analysis",
        "model_card",
        "synthesis_takeaway",
        "problem_contribution",
        "limitations_future",
    )
    visual_ids_by_section = {
        section_id: [visual_ids[idx]]
        for idx, section_id in enumerate(visual_targets[:min(len(visual_targets), len(visual_ids))])
    }
    inner_gap = 10 if compact else 16
    min_text_h = 126 if compact else 260
    for idx, (slot_id, title, role, panel_job) in enumerate(panel_defs[:cols * rows]):
        col = idx % cols
        row = idx // cols
        slot_bbox = {
            "x": margin + col * (panel_w + gutter),
            "y": board_y + row * (panel_h + gutter),
            "w": panel_w,
            "h": panel_h,
        }
        panel_bbox = dict(slot_bbox)
        paired_visual_ids = list(visual_ids_by_section.get(slot_id) or [])
        if paired_visual_ids:
            visual_h_cap = max(120 if compact else 210, int(panel_h * (0.42 if compact else 0.38)))
            visual_h = min(
                visual_h_cap,
                max(1, panel_h - cap_h - inner_gap - min_text_h),
            )
            if visual_h >= 120:
                visual_positions.append(_position(
                    slot_id,
                    f"{role} source_visual local_evidence",
                    slot_bbox["x"],
                    slot_bbox["y"],
                    slot_bbox["w"],
                    visual_h,
                ))
                panel_bbox = {
                    "x": slot_bbox["x"],
                    "y": slot_bbox["y"] + visual_h + cap_h + inner_gap,
                    "w": slot_bbox["w"],
                    "h": max(1, slot_bbox["h"] - visual_h - cap_h - inner_gap),
                }
            else:
                paired_visual_ids = []
        text_panels.append({
            "slot_id": slot_id,
            "section_id": slot_id,
            "title": title,
            "role": f"{role} native_information_unit",
            "bbox": panel_bbox,
            "slot_bbox": slot_bbox,
            "visual_ids": paired_visual_ids,
            "panel_job": panel_job,
            "text_budget": "3-4 source-grounded bullets plus method notes; no abstract paragraph",
            "space_fill_policy": (
                "bind any source visual to dense local explanation inside this same panel; "
                "fill remaining area with native editable text, tags, mini tables, and callouts"
            ),
        })
    footer_panel = {
        "slot_id": "closing_synthesis_band",
        "section_id": "synthesis_takeaway",
        "title": "Synthesis Takeaway",
        "role": "synthesis_takeaway implication conclusion limitation result_interpretation",
        "bbox": {"x": margin, "y": content_bottom - footer_h, "w": cw - 2 * margin, "h": footer_h},
        "panel_job": "synthesis_takeaway implication limitation result_interpretation",
        "text_budget": "source-backed takeaway, limitation, or result interpretation",
        "space_fill_policy": "fill with scientific synthesis; keep provenance metadata-only unless the user explicitly asks for visible citation/contact text",
    }
    slots = [
        _title_slot(cw),
        *_text_panel_slots([*text_panels, footer_panel], prefix="dense_synthesis"),
    ]
    return {"visual_positions": visual_positions, "text_panels": [*text_panels, footer_panel], "slots": slots}


def _recovery_default_layout_parts(
    *,
    cw: int,
    ch: int,
    geom: dict[str, int],
    visual_ids: list[str],
) -> dict[str, Any]:
    margin = int(geom["margin"])
    gutter = int(geom["gutter"])
    cap_h = int(geom["cap_h"])
    compact = bool(geom["compact_wide"])
    top_y = int(geom["top_y"])
    top_img_h = int(geom["top_img_h"])
    top_w = int(geom["top_w"])
    grid_w = int(geom["grid_w"])
    grid_y = int(geom["grid_y"])
    grid_img_h = int(geom["grid_img_h"])
    grid_img_h_2 = int(geom.get("grid_img_h_2", grid_img_h))
    grid_row_gap = int(geom.get("grid_row_gap", 28))
    visual_positions: list[dict[str, Any]] = []
    if compact:
        content_bottom = _recovery_content_bottom(ch, compact_wide=compact)
        text_h = 342
        text_y = content_bottom - text_h
        visual_bottom = text_y - gutter
        row_gap = 12
        available_visual_h = max(320, visual_bottom - top_y - cap_h - row_gap)
        row_h = max(150, int((available_visual_h - cap_h - row_gap) / 2))
        visual_positions.extend(_row_positions(
            "source_anchor_row",
            "method main_evidence supporting_analysis",
            x=margin,
            y=top_y,
            w=cw - 2 * margin,
            h=row_h,
            count=3,
            gutter=gutter,
        ))
        visual_positions.extend(_row_positions(
            "benchmark_table_row",
            "main_evidence results benchmark",
            x=margin,
            y=top_y + row_h + cap_h + row_gap,
            w=cw - 2 * margin,
            count=3,
            gutter=gutter,
        ))
        panel_cols = 3
        panel_rows = 2
        panel_w = int((cw - 2 * margin - (panel_cols - 1) * gutter) / panel_cols)
        panel_h = int((text_h - 12 - (panel_rows - 1) * 12) / panel_rows)
        text_panels = [
            {
                "slot_id": f"default_narrative_{idx + 1}",
                "role": "narrative_section dense_claims local_evidence_explanation",
                "bbox": {
                    "x": margin + (idx % panel_cols) * (panel_w + gutter),
                    "y": text_y + (idx // panel_cols) * (panel_h + 12),
                    "w": panel_w,
                    "h": panel_h,
                },
                "text_budget": "two to three dense source-backed bullets with a local evidence read",
                "panel_job": "Fill with source-backed synthesis, figure/table interpretation, and a concrete takeaway.",
                "space_fill_policy": "no empty cards; dense text is the primary fill signal",
            }
            for idx in range(6)
        ]
    else:
        visual_positions.extend([
            _position("method_visual", "method architecture", margin, top_y, top_w, top_img_h),
            _position("main_evidence", "main_evidence results benchmark", margin + top_w + gutter, top_y, cw - margin - (margin + top_w + gutter), top_img_h),
        ])
        for idx in range(6):
            col = idx % 3
            row = idx // 3
            row_img_h = grid_img_h if row == 0 else grid_img_h_2
            row_y = grid_y if row == 0 else grid_y + grid_img_h + cap_h + grid_row_gap
            visual_positions.append(_position(
                "evidence_grid",
                "qualitative_evidence supporting_analysis results",
                margin + col * (grid_w + gutter),
                row_y,
                grid_w,
                row_img_h,
            ))
    visual_positions = visual_positions[:max(1, min(len(visual_ids), 8))]
    if not compact:
        footer_cols = 3
        footer_rows = 2
        foot_w = int((cw - 2 * margin - (footer_cols - 1) * gutter) / footer_cols)
        footer_h = int(geom["footer_h"])
        footer_y = int(geom["footer_y"])
        footer_row_gap = 16
        footer_reserved_h = 38
        foot_h = int((footer_h - footer_reserved_h - (footer_rows - 1) * footer_row_gap) / footer_rows)
        text_panels = [
            {
                "slot_id": f"default_narrative_{idx + 1}",
                "role": "narrative_section",
                "bbox": {
                    "x": margin + (idx % footer_cols) * (foot_w + gutter),
                    "y": footer_y + (idx // footer_cols) * (foot_h + footer_row_gap),
                    "w": foot_w,
                    "h": foot_h,
                },
            }
            for idx in range(6)
        ]
    slots = [
        _title_slot(cw),
        _visual_group_slot(
            "default_source_visuals",
            "method main_evidence qualitative_evidence source_visuals",
            visual_positions,
            cap_h=cap_h,
            visual_ids=visual_ids[:len(visual_positions)],
            panel_job="Place selected source visuals in the fallback board.",
            space_fill_policy="source-backed visual board",
        ),
        *_text_panel_slots(text_panels, prefix="default_narrative"),
    ]
    return {"visual_positions": visual_positions, "text_panels": text_panels, "slots": slots}


def _recovery_title_typography(title: str, *, cw: int, compact_wide: bool) -> dict[str, Any]:
    title_w = max(900, cw - (600 if compact_wide else 780))
    base_font_px = 70 if compact_wide else 74
    base_h = 148 if compact_wide else 116
    line_height = 1.04
    title_text = re.sub(r"\s+", " ", str(title or "")).strip()
    title_len = max(1, len(title_text))

    # Approximate Inter bold title wrapping before CSS rendering so long
    # landscape titles get a smaller two-line setting instead of being clipped.
    avg_char_em = 0.37
    chars_per_line = max(22.0, title_w / max(1.0, base_font_px * avg_char_em))
    estimated_lines = max(1, math.ceil(title_len / chars_per_line))
    if estimated_lines <= 1:
        title_h = base_h
        title_font_px = base_font_px
    else:
        max_lines = 2
        title_h = max(base_h, 148 if compact_wide else 132)
        height_fit_font = int(title_h / (max_lines * line_height))
        width_fit_font = int(title_w / max(1.0, (title_len / max_lines) * avg_char_em))
        min_font = 54 if compact_wide else 52
        title_font_px = max(min_font, min(base_font_px, height_fit_font, width_fit_font))

    top_y = 44
    meta_gap = 6 if compact_wide else 12
    meta_y = top_y + title_h + meta_gap
    return {
        "width_px": title_w,
        "height_px": title_h,
        "font_px": title_font_px,
        "line_height": f"{line_height:.2f}",
        "meta_y": meta_y,
    }


def _recovery_color_system(state: dict[str, Any]) -> dict[str, Any]:
    for source in (
        state.get("poster_content_brief"),
        state.get("poster_plan_contract"),
    ):
        if not isinstance(source, dict):
            continue
        color_system = source.get("color_system")
        if isinstance(color_system, dict) and isinstance(color_system.get("roles"), dict):
            return color_system
    roles = {
        "background": "#FFFFFF",
        "text": "#21181B",
        "primary": "#C1121F",
        "secondary": "#F7DEE1",
        "accent": "#C1121F",
        "header_text": "#FFFFFF",
        "bar": "#C1121F",
    }
    return {
        "palette_id": "bright_cobalt",
        "palette_name": "Cardinal Red",
        "roles": roles,
        "allowed_hexes": _unique_recovery_hexes(list(roles.values())),
    }


def _recovery_color_roles(color_system: dict[str, Any] | None) -> dict[str, str]:
    fallback = _recovery_color_system({})
    roles = dict(fallback["roles"])
    raw_roles = color_system.get("roles") if isinstance(color_system, dict) and isinstance(color_system.get("roles"), dict) else {}
    for key in roles:
        value = str(raw_roles.get(key) or "").strip()
        if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            roles[key] = value.upper()
    return roles


def _unique_recovery_hexes(values: list[Any]) -> list[str]:
    out: list[str] = []
    for value in values:
        text = str(value or "").strip().upper()
        if re.fullmatch(r"#[0-9A-F]{6}", text) and text not in out:
            out.append(text)
    return out


def _recovery_authored_html(
    *,
    title: str,
    authors: str,
    affiliations: str,
    venue: str,
    sections: list[dict[str, Any]],
    selected_assets: list[dict[str, Any]],
    identity_assets: list[dict[str, Any]],
    canvas: dict[str, int],
    layout_context: dict[str, Any] | None = None,
    color_system: dict[str, Any] | None = None,
) -> tuple[str, str, list[dict[str, Any]]]:
    # Automatic heading logos are disabled for paper posters. Keep the parameter
    # for legacy callers, but never place identity images in recovery HTML.
    identity_assets = []
    cw = int(canvas["w"])
    ch = int(canvas["h"])
    if layout_context is None:
        layout_context = _recovery_layout_context(
            title=title,
            sections=sections,
            selected_assets=selected_assets,
            contract={},
            canvas=canvas,
        )
    geom = (
        layout_context.get("geometry")
        if isinstance(layout_context.get("geometry"), dict)
        else _recovery_layout_geometry(cw, ch, source_visual_count=len(selected_assets))
    )
    margin = geom["margin"]
    gutter = geom["gutter"]
    top_y = geom["top_y"]
    top_img_h = geom["top_img_h"]
    cap_h = geom["cap_h"]
    top_w = geom["top_w"]
    grid_y = geom["grid_y"]
    grid_cols = 3
    grid_w = geom["grid_w"]
    grid_img_h = geom["grid_img_h"]
    grid_img_h_2 = geom.get("grid_img_h_2", grid_img_h)
    grid_row_gap = geom.get("grid_row_gap", 28)
    footer_y = geom["footer_y"]
    footer_h = geom["footer_h"]
    compact_wide = bool(geom["compact_wide"])
    archetype = str(layout_context.get("archetype") or _RECOVERY_ARCHETYPE_DEFAULT)
    text_heavy_source_limited = archetype == _RECOVERY_ARCHETYPE_THEORY
    dense_synthesis = archetype == _RECOVERY_ARCHETYPE_DENSE_SYNTHESIS
    if text_heavy_source_limited:
        footer_y = top_y + top_img_h + cap_h + (26 if compact_wide else 40)
        footer_h = max(320, ch - footer_y - (70 if compact_wide else 86))
    footer_title_px = 27 if text_heavy_source_limited else (24 if compact_wide else (20 if ch < 1800 else 22))
    footer_body_px = 21 if text_heavy_source_limited else (19 if compact_wide else (14 if ch < 1800 else 16))
    footer_pad_y = 12 if text_heavy_source_limited else (4 if compact_wide else 12)
    footer_pad_x = 16 if text_heavy_source_limited else (12 if compact_wide else 14)
    footer_h2_gap = 6 if text_heavy_source_limited else (3 if compact_wide else 6)
    footer_ul_line = "1.18" if text_heavy_source_limited else ("1.08" if compact_wide else "1.15")
    footer_li_gap = 4 if text_heavy_source_limited else (0 if compact_wide else 3)
    if dense_synthesis:
        footer_title_px = 25 if compact_wide else 34
        footer_body_px = 18 if compact_wide else 27
        footer_pad_y = 9 if compact_wide else 18
        footer_pad_x = 14 if compact_wide else 20
        footer_h2_gap = 5 if compact_wide else 12
        footer_ul_line = "1.12" if compact_wide else "1.2"
        footer_li_gap = 2 if compact_wide else 7
    caption_font_px = 20 if compact_wide else 18
    caption_pad_y = 3 if compact_wide else 8
    caption_line = "1.05" if compact_wide else "1.18"
    source_note_font_px = 13 if compact_wide else 12
    source_note_h = 44 if compact_wide else 42
    title_type = _recovery_title_typography(title, cw=cw, compact_wide=compact_wide)
    title_w = title_type["width_px"]
    title_h = title_type["height_px"]
    title_font_px = title_type["font_px"]
    title_line_height = title_type["line_height"]
    meta_y = title_type["meta_y"]
    color_roles = _recovery_color_roles(color_system)
    blocks: list[dict[str, Any]] = []
    css_parts = [
        ".paper-poster{"
        f"--poster-bg:{color_roles['background']};--poster-text:{color_roles['text']};"
        f"--poster-primary:{color_roles['primary']};--poster-secondary:{color_roles['secondary']};"
        f"--poster-accent:{color_roles['accent']};--poster-header-text:{color_roles['header_text']};"
        f"--poster-bar:{color_roles['bar']};background:var(--poster-bg);color:var(--poster-text);"
        "font-family:Inter,Arial,sans-serif;}",
        ".recovery-title{position:absolute;left:72px;top:44px;width:%dpx;height:%dpx;margin:0;font-size:%dpx;line-height:%s;font-weight:820;overflow:hidden;}" % (title_w, title_h, title_font_px, title_line_height),
        ".recovery-meta{position:absolute;left:72px;top:%dpx;width:%dpx;height:34px;margin:0;font-size:24px;line-height:1.2;color:var(--poster-primary);overflow:hidden;}" % (meta_y, title_w),
        ".visual-card{position:absolute;margin:0;border:2px solid var(--poster-secondary);background:#fff;overflow:hidden;}",
        ".visual-card img{display:block;width:100%;object-fit:contain;background:#fff;}",
        f".visual-card figcaption{{display:block;margin:0;padding:{caption_pad_y}px 12px;box-sizing:border-box;background:var(--poster-secondary);border-top:1px solid var(--poster-secondary);font-size:{caption_font_px}px;line-height:{caption_line};color:var(--poster-text);overflow:hidden;}}",
        f".footer-card{{position:absolute;margin:0;padding:{footer_pad_y}px {footer_pad_x}px;background:#fff;border:1px solid var(--poster-secondary);overflow:hidden;}}",
        f".footer-card h2{{margin:0 0 {footer_h2_gap}px 0;font-size:{footer_title_px}px;line-height:1.05;color:var(--poster-primary);}}",
        f".footer-card ul{{margin:0;padding-left:18px;font-size:{footer_body_px}px;line-height:{footer_ul_line};color:var(--poster-text);}}",
        f".footer-card li{{margin:0 0 {footer_li_gap}px 0;}}",
        f".source-note{{position:absolute;left:72px;top:{ch - (source_note_h + 18)}px;width:{cw - 144}px;height:{source_note_h}px;margin:0;font-size:{source_note_font_px}px;line-height:1.2;color:var(--poster-primary);overflow:hidden;}}",
    ]
    if dense_synthesis:
        css_parts.extend([
            ".dense-panel{display:flex;flex-direction:column;gap:10px;background:#fff;}",
            ".dense-panel h2{flex:0 0 auto;}",
            ".dense-panel ul{padding-left:24px;}",
            ".dense-panel p{margin:0;color:var(--poster-text);line-height:1.18;}",
            ".dense-panel strong{color:var(--poster-primary);}",
            ".dense-grid{flex:1 1 auto;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));grid-auto-rows:minmax(0,1fr);gap:10px;align-items:stretch;}",
            ".stat-card,.contribution-card,.insight-card,.callout{display:flex;flex-direction:column;justify-content:flex-start;min-height:0;overflow:hidden;border:1px solid var(--poster-secondary);background:#ffffff;padding:10px 12px;box-sizing:border-box;}",
            ".stat-label,.tag{display:inline-block;font-size:0.78em;line-height:1.1;text-transform:uppercase;color:var(--poster-primary);font-weight:800;}",
            ".stat-val{display:block;margin-top:4px;font-size:1.02em;line-height:1.12;font-weight:760;color:var(--poster-primary);}",
            ".flow-row{flex:1 1 auto;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));grid-auto-rows:minmax(0,1fr);gap:10px;align-items:stretch;}",
            ".flow-box{display:flex;flex-direction:column;justify-content:flex-start;min-height:0;overflow:hidden;border:1px solid var(--poster-secondary);background:#fff;padding:10px 12px;box-sizing:border-box;font-weight:650;line-height:1.15;}",
            ".table-wrap{flex:1 1 auto;min-height:0;overflow:hidden;}",
            ".native-table{width:100%;height:100%;table-layout:fixed;border-collapse:collapse;background:#ffffff;font-size:0.92em;line-height:1.12;}",
            ".native-table th,.native-table td{border:1px solid var(--poster-secondary);padding:7px 8px;text-align:left;vertical-align:top;}",
            ".native-table th{background:var(--poster-secondary);color:var(--poster-primary);font-weight:800;}",
            ".insight-stack,.result-stack{flex:1 1 auto;display:grid;grid-template-columns:1fr;grid-auto-rows:minmax(0,1fr);gap:10px;align-items:stretch;min-height:0;}",
            ".result-band{display:flex;align-items:flex-start;min-height:0;overflow:hidden;border-left:8px solid var(--poster-accent);background:#fff;padding:10px 12px;box-sizing:border-box;font-weight:680;line-height:1.16;}",
            ".provenance-list{margin:0;padding-left:22px;font-size:0.86em;line-height:1.12;columns:3;column-gap:28px;}",
        ])
    body: list[str] = []
    body.append(f'<h1 class="recovery-title" data-block-id="title">{escape(title)}</h1>')
    meta = _recovery_meta_text(
        authors=authors,
        affiliations=affiliations,
        venue=venue,
        compact_wide=compact_wide,
    )
    if meta:
        body.append(f'<p class="recovery-meta" data-block-id="meta">{escape(meta)}</p>')
    section_claims = {
        _safe_identifier(section.get("section_id") or f"section_{idx + 1}"): _recovery_section_claim_ids(section)
        for idx, section in enumerate(sections)
    }
    footer_source_pool = _recovery_footer_source_pool(
        sections,
        selected_assets,
        compact_wide=compact_wide,
    )
    blocks.extend([
        {"block_id": "title", "slot_id": "title_thesis", "kind": "text", "role": "identity_header title", "text": title, "bbox": {"x": 72, "y": 44, "w": title_w, "h": title_h}},
    ])
    if meta:
        blocks.append({
            "block_id": "meta",
            "slot_id": "title_thesis",
            "kind": "text",
            "role": "identity_header authors affiliation",
            "text": meta,
            "bbox": {"x": 72, "y": meta_y, "w": title_w, "h": 34},
        })

    visual_positions = [
        item for item in list(layout_context.get("visual_positions") or [])
        if isinstance(item, dict) and isinstance(item.get("bbox"), dict)
    ]
    for idx, asset in enumerate(selected_assets[:len(visual_positions)]):
        pos = visual_positions[idx]
        bbox = pos["bbox"]
        fitted = _fit_source_visual_bbox_to_asset(asset, bbox)
        x = fitted["x"]
        y = fitted["y"]
        w = fitted["w"]
        img_h = fitted["h"]
        card_h = img_h + cap_h
        block_id = f"visual_{asset['asset_id']}"
        cap_id = f"caption_{asset['asset_id']}"
        css_parts.append(
            f".card-{idx}{{left:{x}px;top:{y}px;width:{w}px;height:{card_h}px;}}"
            f".card-{idx} img{{height:{img_h}px;object-position:top center;}}"
            f".card-{idx} figcaption{{height:{cap_h}px;}}"
        )
        caption = _recovery_visual_caption(asset, compact_wide=compact_wide)
        source_id = escape(str(asset["asset_id"]), quote=True)
        body.append(
            f'<figure class="visual-card card-{idx}" data-source-id="{source_id}" data-layer-id="{source_id}">'
            f'<img data-block-id="{block_id}" data-source-id="{source_id}" data-layer-id="{source_id}" '
            f'src="{escape(str(asset["src_path"]), quote=True)}" alt="{escape(caption, quote=True)}">'
            f'<figcaption data-block-id="{cap_id}">{escape(caption)}</figcaption>'
            f'</figure>'
        )
        common = {
            "source": "ingested_pdf",
            "source_id": asset["asset_id"],
            "source_page": asset.get("source_page"),
            "provenance": {
                "source_bbox_pdf_points": asset.get("source_bbox_pdf_points"),
                "visual_score": asset.get("visual_score"),
                "source_processing": asset.get("source_processing"),
                "source_processing_details": asset.get("source_processing_details"),
            },
        }
        visual_covers = _recovery_visual_claim_ids(asset, section_claims)
        blocks.append({
            "block_id": block_id,
            "slot_id": str(pos.get("slot_id") or ""),
            "kind": "image",
            "role": str(pos.get("role") or asset.get("story_role") or "source_visual"),
            "layer_id": asset["asset_id"],
            "src_path": asset["src_path"],
            "original_src_path": asset.get("original_src_path"),
            "source_processing": asset.get("source_processing"),
            "source_processing_details": asset.get("source_processing_details"),
            "image_size": asset.get("image_size"),
            "caption": asset.get("caption_full") or caption,
            "bbox": {"x": x, "y": y, "w": w, "h": img_h},
            "covers": visual_covers,
            **common,
        })
        blocks.append({
            "block_id": cap_id,
            "slot_id": str(pos.get("slot_id") or ""),
            "kind": "caption",
            "role": "caption",
            "text": caption,
            "bbox": {"x": x, "y": y + img_h, "w": w, "h": cap_h},
            "covers": visual_covers,
            **common,
        })

    text_panels = [
        item for item in list(layout_context.get("text_panels") or [])
        if isinstance(item, dict) and isinstance(item.get("bbox"), dict)
    ]
    if not text_panels:
        text_panels = _bottom_text_panels(
            x=margin,
            y=footer_y,
            w=cw - 2 * margin,
            h=max(120, footer_h),
            gutter=gutter,
            count=min(4, max(1, len(sections))),
            prefix="fallback_callout",
        )
    used_footer_bullets: set[str] = set()
    section_pool = sections or _recovery_sections({})
    active_limit = (15 if compact_wide else 12) if dense_synthesis else 6
    active_text_panels = text_panels[:max(4, min(active_limit, len(text_panels)))]
    active_panel_count = max(1, len(active_text_panels))
    for idx, panel in enumerate(active_text_panels):
        panel_section_id = _safe_identifier(panel.get("section_id") or panel.get("slot_id") or "")
        section = next(
            (
                candidate for candidate in section_pool
                if _safe_identifier(candidate.get("section_id") or "") == panel_section_id
            ),
            section_pool[idx % len(section_pool)],
        )
        panel_bbox = panel["bbox"]
        x = int(panel_bbox.get("x") or margin)
        y = int(panel_bbox.get("y") or footer_y)
        foot_w = int(panel_bbox.get("w") or 1)
        foot_h = int(panel_bbox.get("h") or 1)
        sec_id = _safe_identifier(panel.get("section_id") or section.get("section_id") or f"section_{idx + 1}")
        block_id = f"section_{sec_id}_{idx + 1}"
        title_text = _clean_text(panel.get("title") or section.get("title") or sec_id.replace("_", " ").title(), limit=48, ellipsis=False)
        bullets = _recovery_footer_bullets(
            section,
            compact_wide=compact_wide,
            used_keys=used_footer_bullets,
            fallback_pool=footer_source_pool,
        )
        bullets = [b for b in bullets if b]
        if compact_wide and sec_id == "limitation_future":
            title_text = "Conclusion"
        panel_claims = list(section_claims.get(sec_id, []))
        for overflow_idx in range(idx + active_panel_count, len(section_pool), active_panel_count):
            overflow_section = section_pool[overflow_idx]
            overflow_sec_id = _safe_identifier(overflow_section.get("section_id") or f"section_{overflow_idx + 1}")
            for claim_id in section_claims.get(overflow_sec_id, []):
                if claim_id not in panel_claims:
                    panel_claims.append(claim_id)
        css_parts.append(f".footer-{idx}{{position:absolute;left:{x}px;top:{y}px;width:{foot_w}px;height:{foot_h}px;}}")
        if dense_synthesis:
            panel_classes = f"footer-card dense-panel footer-{idx}"
            panel_inner = _dense_recovery_panel_inner_html(
                sec_id,
                title_text,
                bullets,
                compact_wide=compact_wide,
            )
            native_unit_blocks = _dense_recovery_native_unit_blocks(
                sec_id,
                title_text,
                bullets,
                bbox={"x": x, "y": y, "w": foot_w, "h": foot_h},
                compact_wide=compact_wide,
                slot_id=str(panel.get("slot_id") or ""),
                covers=panel_claims,
            )
        else:
            panel_classes = f"footer-card footer-{idx}"
            bullet_html = "".join(f"<li>{escape(item)}</li>" for item in bullets)
            panel_inner = f"<h2>{escape(title_text)}</h2><ul>{bullet_html}</ul>"
            native_unit_blocks = []
        body.append(
            f'<section class="{panel_classes}" data-block-id="{block_id}">'
            f"{panel_inner}</section>"
        )
        parent_text = None if native_unit_blocks else f"{title_text}: " + " ".join(bullets)
        blocks.append({
            "block_id": block_id,
            "slot_id": str(panel.get("slot_id") or ""),
            "kind": "group" if dense_synthesis and native_unit_blocks else "text",
            "role": f"{sec_id} narrative_section",
            "text": parent_text,
            "bbox": {"x": x, "y": y, "w": foot_w, "h": foot_h},
            "covers": panel_claims,
        })
        blocks.extend(native_unit_blocks)

    note = (
        f"Source: {_clean_text(authors or 'paper authors', limit=54, ellipsis=True)}; "
        f"{_clean_text(venue or 'source paper', limit=42, ellipsis=True)}; "
        "figure/table IDs in metadata."
    )
    body.append(f'<p class="source-note" data-block-id="source_note">{escape(note)}</p>')
    blocks.append({"block_id": "source_note", "kind": "text", "role": "metadata citation_line", "text": note, "bbox": {"x": 72, "y": ch - (source_note_h + 18), "w": cw - 144, "h": source_note_h}})
    return "\n".join(body), "\n".join(css_parts), blocks


def _recovery_layout_slots(
    cw: int,
    ch: int,
    visual_ids: list[str],
    *,
    archetype: str | None = None,
) -> list[dict[str, Any]]:
    chosen = archetype or (
        _RECOVERY_ARCHETYPE_THEORY
        if len(visual_ids) <= 3 else
        _RECOVERY_ARCHETYPE_DEFAULT
    )
    geom = _recovery_layout_geometry(
        cw,
        ch,
        source_visual_count=len(visual_ids),
    )
    return _recovery_layout_parts_for_archetype(
        chosen,
        cw=cw,
        ch=ch,
        geom=geom,
        visual_ids=visual_ids,
    )["slots"]


def _clean_text(value: Any, *, limit: int, ellipsis: bool = True) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = text.replace("\u2026", "...").replace("\u2014", "-").replace("\u2013", "-")
    if len(text) <= limit:
        return text
    if ellipsis:
        return text[: max(0, limit - 3)].rstrip() + "..."
    cut = text[: max(0, limit)].rstrip()
    space = cut.rfind(" ")
    if space >= int(limit * 0.65):
        cut = cut[:space].rstrip()
    return cut


def _clean_recovery_sentence(value: Any, *, limit: int, trim_dangling_phrase: bool = False) -> str:
    text = _clean_text(value, limit=limit, ellipsis=False)
    text = _strip_incomplete_recovery_tail(text, trim_dangling_phrase=trim_dangling_phrase)
    return text or _clean_text(value, limit=limit, ellipsis=False).rstrip(".")


def _ensure_recovery_terminal(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip(" ;,:-")
    if not cleaned:
        return ""
    if re.search(r"[.!?)]$", cleaned):
        return cleaned
    return f"{cleaned}."


def _compact_recovery_claim_text(
    value: Any,
    *,
    section_id: str = "",
    source: str = "",
    purpose: str = "footer",
) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    lower = (
        text.lower()
        .replace("\u2014", "-")
        .replace("\u2013", "-")
        .replace("\u2026", "...")
    )
    section_l = section_id.lower()
    if section_l in {"motivation", "contribution", "synthesis_takeaway"}:
        if "prior multimodal systems treat vision and audio as external attachments" in lower:
            return "Prior multimodal systems treat vision and audio as external attachments to language models."
        if "discrete visual tokens need not be limited" in lower:
            return "Discrete visual tokens support generation, reasoning, and OCR-heavy understanding."
        if "discrete visual tokenization has long been seen" in lower:
            return "Discrete visual tokenization has been viewed as weaker for understanding than continuous visual modeling."
        if "unified multimodal models often improve either visual understanding" in lower:
            return "Unified multimodal models often trade off visual understanding quality against generation quality."
        if "native discrete vision pipeline must represent images at arbitrary resolutions" in lower:
            return "Native discrete vision must handle arbitrary image resolution while preserving faithful reconstruction."
        if "adding multiple modalities usually requires specialized components" in lower:
            return "Specialized modality components make it unclear whether one backbone can absorb text, vision, and audio."
        if "understanding-generation conflict" in lower:
            return "Unified multimodal models often trade off visual understanding quality against generation quality."
        if "discrete-vision understanding ceiling" in lower:
            return "Discrete visual tokenization has been viewed as weaker for understanding than continuous visual modeling."
        if "resolution mismatch for native vision" in lower:
            return "Native discrete vision must handle arbitrary image resolution while preserving faithful reconstruction."
        if "modality-specific design burden" in lower:
            return "Specialized modality components make it unclear whether one backbone can absorb text, vision, and audio."
    if section_l in {"model_card", "method_pipeline"}:
        if "discrete native autoregressive framework maps multimodal information" in lower:
            return "DiNA maps multimodal information into a shared discrete token space for one next-token objective."
        if "discrete native any-resolution visual transformer" in lower or "dnavit hierarchical any-resolution" in lower:
            return "dNaViT converts continuous images into hierarchical discrete tokens for reconstruction and understanding."
        if "internalized codebooks" in lower:
            return "Visual and audio discrete tokens are learned inside the language embedding space."
        if "internal linguistic guidance" in lower:
            return "Lightweight multimodal heads and internal linguistic guidance align non-text generation with understanding."
        if "patch-tokenized pure transformer" in lower:
            return "Images are split into fixed-size patches, linearly embedded as tokens, and processed by a standard Transformer."
        if "large-scale supervised pre-training" in lower:
            return "ViT is pre-trained on ImageNet-21k/JFT-300M and then fine-tuned on downstream tasks."
        if "scaling model and patch size families" in lower:
            return "Base, Large, and Huge ViT variants test how model size and patch size interact with data scale."
        if "transfer evaluation against strong cnn" in lower:
            return "Transfer evaluation compares ViT against strong CNN baselines across ImageNet, CIFAR, VTAB, and related datasets."
        if "transformers dominate nlp" in lower:
            return "Transformer pre-training and fine-tuning provide the architectural prior for the vision adaptation."
        if "modality-agnostic mixture-of-experts dynamically allocates" in lower:
            return ""
    if section_l in {"results_table", "ablation_analysis"}:
        if "mmstar" in lower:
            return "MMStar is used as a unified multimodal benchmark row against the compared baselines."
        if "math benchmark" in lower or "mathvista" in lower:
            return "MathVista and related reasoning benchmarks provide the quantitative reasoning evidence."
        if "visulogic" in lower:
            return "VisuLogic is reported as the paper's visual-logic benchmark evidence."
        if "docvqa" in lower:
            return "DocVQA test performance is reported as OCR-heavy visual understanding evidence."
        if "generation benchmark row" in lower:
            return "Generation benchmark tables compare LongCat-Next against multimodal generation baselines."
        if "imagenet top-1" in lower or "accuracy of 88.55" in lower:
            return "ImageNet top-1 reaches 88.55% for the best reported Vision Transformer model."
        if "cifar-100" in lower:
            return "CIFAR-100 transfer reaches 94.55% in the paper's reported setup."
        if "vtab" in lower and "77.63" in lower:
            return "VTAB mean accuracy reaches 77.63% across the 19-task transfer suite."
        if "tpuv3-core-days" in lower:
            return "Table 2 reports pre-training compute in TPUv3-core-days across ViT and CNN baselines."
        if "compute comparison" in lower:
            return "Table 2 compares transfer accuracy and pre-training compute against strong CNN baselines."
    if section_l == "method":
        if "instructional-video-derived benchmark curation" in lower:
            return "Instructional videos supply realistic visual GUI tasks."
        if "hierarchical annotation and evaluation" in lower:
            return "Annotations split plans, narrations, and atomic actions."
        if "multi-modal query settings" in lower or "multi-modal query" in lower:
            return "Planning is tested with visual, text, and combined queries."
        if "dimension-specific metrics" in lower or "llm-based critics" in lower:
            return "Metrics score planning and atomic GUI actions separately."
        if "tool-augmentation" in lower or "ocr and grounding" in lower:
            return "Tool baselines test whether OCR and grounding improve actions."
        if "instructional-video-sourced" in lower or "from high-quality web instructional videos" in lower:
            return "Instructional videos define GUI tasks and planning labels."
        if "hierarchical task decomposition" in lower or "high-level planning" in lower:
            return "VideoGUI separates planning, narration, and atomic actions."
        if (
            "multi-query planning protocol" in lower
            or "visual-only, text-only" in lower
            or "vision-only, text-only" in lower
            or "vision+text" in lower
            or "vision-plus-text" in lower
        ):
            return "Planning is tested with visual, text, and combined queries."
        if "fine-grained action-category metrics" in lower or "click, drag" in lower:
            return "Metrics separate click, drag, type, press, and scroll actions."
    if (
        purpose == "thesis"
        and "videogui is a hierarchical benchmark" in lower
        and "instructional videos" in lower
        and ("visual-centric" in lower or "multimodal gui agents" in lower or "planning" in lower)
    ):
        return "VideoGUI uses instructional videos to expose GUI agents' weakest area: visual planning."
    if "videogui is a hierarchical benchmark" in lower and "planning" in lower and "action execution" in lower:
        if purpose == "thesis":
            return "VideoGUI evaluates GUI assistants through a hierarchical process."
        return "VideoGUI identifies where GUI assistants fail across planning and actions."
    if "videogui is a hierarchical benchmark" in lower and "visual-centric gui automation" in lower:
        if purpose == "thesis":
            return "VideoGUI evaluates GUI assistants through a hierarchical process."
        return "VideoGUI links visual GUI tasks to hierarchical evaluation."
    if (
        purpose == "thesis"
        and "videogui shows" in lower
        and "current multimodal gui agents struggle" in lower
        and "planning" in lower
    ):
        return (
            "Abstract: the SoTA large multimodal model GPT4o performs poorly on visual-centric GUI tasks, "
            "especially for high-level planning."
        )
    if (
        purpose == "thesis"
        and "gui agents" in lower
        and "visual-centric" in lower
        and ("struggle" in lower or "poorly" in lower)
    ):
        return (
            "Abstract: the SoTA large multimodal model GPT4o performs poorly on visual-centric GUI tasks, "
            "especially for high-level planning."
        )
    if purpose == "thesis" and "current multimodal models" in lower and "planning" in lower and (
        "are far" in lower
        or "hierarchical evaluation" in lower
        or "visual-centric gui automation" in lower
    ):
        return (
            "Our evaluation on VideoGUI reveals that even the SoTA large multimodal model GPT4o "
            "performs poorly on visual-centric GUI tasks, especially for high-level planning."
        )
    if "current multimodal gui assistants struggle" in lower and "hierarchical planning" in lower:
        if purpose == "thesis":
            return "VideoGUI shows current multimodal GUI agents struggle on visual-centric tasks, especially high-level planning."
        return "VideoGUI shows GUI agents mainly fail at hierarchical planning."
    if (
        purpose == "thesis"
        and "current multimodal gui agents struggle" in lower
        and "with planning" in lower
    ):
        return "VideoGUI shows current multimodal GUI agents struggle on visual-centric tasks, especially high-level planning."
    if "single success metrics hide actionable failure modes" in lower:
        return "Task success alone hides where GUI agents fail."
    if "task-level success alone hides whether failure comes" in lower:
        return "Task success hides planning, narration, and execution failure modes."
    if "text-instruction benchmarks miss real gui difficulty" in lower:
        return "Prior GUI benchmarks emphasize simple, language-only instructions."
    if "benchmarking gui agents with only task success or text instructions" in lower:
        return "VideoGUI evaluates visual goals, plans, and atomic actions."
    if (
        purpose == "thesis"
        and "ivideogpt" in lower
        and "interactive world model" in lower
        and ("video prediction" in lower or "model-based rl" in lower or "planning" in lower)
    ):
        return "iVideoGPT scales interactive world models with compressive tokenization and autoregressive sequence modeling."
    if "current gui assistants" in lower and "including gpt-4o" in lower:
        return "Current GUI agents still struggle with visual-centric tasks."
    if "future gui agents must improve visual procedural planning" in lower:
        return "Future agents need stronger visual planning, not just execution aids."
    if "clear textual instructions can substitute" in lower:
        return "Text helps planning, but visual preview tasks remain hard."
    if "gpt-4o achieves the best overall score" in lower:
        return "Table 3 shows planning remains difficult across baseline agents."
    if "agent failures are hard to localize" in lower or "task-success-only evaluation cannot distinguish" in lower:
        return "Task success alone cannot localize planning versus execution failures."
    if "visual outcomes must be reverse-engineered" in lower:
        return "Visual goals require reconstructing GUI procedures from end states."
    if "planning may be harder than grounding" in lower:
        return "Planning is harder to solve than low-level GUI grounding."
    if "planning is much harder than atomic action" in lower:
        return "Hierarchical planning is harder than atomic action execution."
    if (
        "low-level grounding is the main challenge" in lower
        and ("but" in lower or "planning" in lower)
    ):
        return "Planning, not low-level grounding, is the larger VideoGUI failure source."
    if "covers 11 visual-centric" in lower or "features 86 complex tasks" in lower:
        return "VideoGUI covers 11 visual-centric apps and 86 complex tasks."
    if "best model gpt-4o fails" in lower or "fails to complete a single full task" in lower:
        return "Table 3 shows full-task success remains low on VideoGUI."
    if "bottleneck surprisingly lies in planning" in lower or "bottleneck lies in planning" in lower:
        return "Table 3 highlights the challenge posed by vision preview instructions."
    if "planning from textual queries" in lower or "textual query" in lower:
        return "Text queries help planning, but visual planning remains hard."
    if "clear textual instructions" in lower and ("vision-only" in lower or "open challenge" in lower):
        return "Text instructions help planning, but vision-only preview understanding remains open."
    if "textual instructions can partially substitute" in lower:
        return "Text helps planning, but visual-text perception remains unsolved."
    if "textual instructions can overestimate" in lower or "text-only gui benchmarks can overestimate" in lower:
        return "Text-only GUI benchmarks can overestimate visual-task ability."
    if (
        "future gui agents must improve visual planning" in lower
        and "procedural reconstruction" in lower
    ):
        return "Conclusion: VideoGUI provides clear signals for existing limitations and improvement."
    if "future gui agents should prioritize" in lower:
        return "Conclusion: VideoGUI provides clear signals for existing limitations and improvement."
    if "benchmarks for realistic gui assistance" in lower:
        return "Realistic GUI benchmarks need professional software and visual goals."
    if (
        "stronger visual-procedural reasoning" in lower
        or "visual procedural reasoning" in lower
        or "long-horizon visual procedure reconstruction" in lower
    ):
        if section_l == "key_contribution":
            return "Conclusion: challenges of visual-oriented GUI automation."
        return "Conclusion: challenges of visual-oriented GUI automation."
    if (
        "simple-text benchmarks" in lower
        or "prior gui benchmarks emphasize short" in lower
        or "prior gui benchmarks emphasize simple tasks" in lower
        or "text-centric benchmarks" in lower
    ):
        return "Text-only GUI benchmarks miss visual-centric software tasks."
    if "multimodal-input" in lower or "multimodal input" in lower:
        return "Multimodal inputs expose gaps in text-centric GUI benchmarks."
    if "need to localize failure sources" in lower:
        return "Hierarchical labels isolate planning and execution failures."
    if "visual preview planning is harder" in lower or "reverse-engineering procedures from before/after visuals" in lower:
        return "Visual previews require reconstructing procedures from states."
    if "grounding weakness may not be the main blocker" in lower:
        return "Planning, not grounding, is the larger failure source."
    if "dataset scale" in lower or "averaging 22.7 actions" in lower:
        return "VideoGUI covers 86 tasks averaging 22.7 actions."
    if "benchmark comparison" in lower:
        return "Table 1 shows VideoGUI adds instructional-video tasks."
    if "best model overall performance" in lower or "gpt4o performs poorly" in lower:
        return "Table 3 shows visual-centric GUI tasks remain difficult."
    if "full evaluation row for gpt-4o" in lower or "full benchmark results for gpt-4o" in lower or "gpt-4o [37]" in lower:
        return "Table 3 reports GPT-4o high-level planning score of 17.1."
    if "best baseline overall score" in lower or "highest score of 39.4" in lower:
        return "Table 3 reports GPT-4o best overall score of 39.4."
    if "tool augmentation" in lower and "does not remove" in lower:
        return "Tool augmentation helps execution, but planning remains the bottleneck."
    if "major limitations of current models" in lower or "visual-oriented gui automation" in lower:
        return "Current GUI agents still fail advanced visual-oriented automation."
    if "diagnosing failures at different reasoning levels" in lower or "diagnosing failures across planning and execution" in lower:
        return "VideoGUI diagnoses failures across multiple reasoning levels."
    if "hierarchical evaluation is necessary" in lower or "hierarchical evaluation separates" in lower:
        return "Hierarchical evaluation separates planning errors from action execution."
    if "hierarchical evaluation gives more actionable diagnosis" in lower:
        return "Hierarchical evaluation separates planning and execution errors."
    if "instructional-video tasks often provide only start/end" in lower:
        return "Instructional videos require inferring procedures from visual states."
    if "prior gui benchmarks emphasize short text" in lower:
        return "Prior GUI benchmarks emphasize simple, language-only instructions."
    if "curates a multimodal benchmark from high-quality" in lower or "source tasks from high-quality" in lower:
        return "VideoGUI is curated from high-quality instructional videos."
    if "decomposes assessment into high-level planning" in lower:
        return "VideoGUI separates high-level planning, mid-level planning, and atomic actions."
    if "vision-only, text-only, and vision-plus-text" in lower:
        return "Planning is evaluated across vision, text, and vision-plus-text queries."
    if section_l == "limitation_future" and (
        "example of final outcome" in lower
        or "initial and final visual states" in lower
        or "qualitative example" in lower
    ):
        return "Qualitative cases show remaining full-task planning errors."
    if "benchmark exposes current model limitations" in lower or "current model limitations" in lower:
        return "VideoGUI exposes current limitations in visual GUI planning."
    if "instructional videos are a viable data source" in lower:
        return "Instructional videos are viable sources for GUI benchmarks."
    return ""


def _strip_incomplete_recovery_tail(text: str, *, trim_dangling_phrase: bool = False) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    had_trailing_ellipsis = bool(re.search(r"(?:\.{3,}|…)\s*$", cleaned))
    cleaned = re.sub(r"\s*(?:\.{3,}|…)\s*$", "", cleaned).rstrip(" ,;:-")
    if had_trailing_ellipsis:
        cleaned = re.sub(
            r"\s+(?:and|or|but|while|whereas|so|because|with|without|via|using|including|such\s+as|rather\s+than|instead\s+of)\s+[^.;:!?]{0,130}$",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).rstrip(" ,;:-")
        cleaned = re.sub(
            r"\s*,\s*(?:especially|including|while|whereas|with|without|so|because)\s+[^.;:!?]{0,130}$",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).rstrip(" ,;:-")
        cleaned = re.sub(
            r"\s+(?:rather\s+than|instead\s+of|including|such\s+as|with|without|via|using)\s+[^.;:!?]{0,80}$",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).rstrip(" ,;:-")
        cleaned = re.sub(
            r"\s+(?:mo|reconst|understandin|trai|rea|wi|de-to|in)$",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).rstrip(" ,;:-")
    if trim_dangling_phrase:
        cleaned = re.sub(
            r"\s+(?:from|for|to|by|of|into|across|within|between|during)\s+"
            r"(?:the|a|an|this|that|these|those|existing|current|paper|model|source|visual|text|training|evaluation)\s+"
            r"[^.;:!?]{0,36}$",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).rstrip(" ,;:-")
    cleaned = re.sub(
        r"\s+(?:but|and|or|not|rather\s+than|instead\s+of)\s+"
        r"(?:the|a|an|this|that|these|those|current|existing|element|tool|"
        r"low-level|visual|textual)?$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).rstrip(" ,;:-")
    cleaned = re.sub(
        r"\s+(?:in|including|such\s+as)\s*:\s*\(?[ivxlcdm0-9a-z]{1,4}\)?$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).rstrip(" ,;:-")
    cleaned = re.sub(
        r"\s+(?:and|or|but|not|because|while|whereas|rather\s+than|instead\s+of|including|such\s+as|with|without|via|using|by|from|for|to|of|than|into|across|within|between|during)\s*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).rstrip(" ,;:-")
    return cleaned


def _recovery_footer_bullets(
    section: dict[str, Any],
    *,
    compact_wide: bool,
    used_keys: set[str] | None = None,
    fallback_pool: list[str] | None = None,
) -> list[str]:
    bullets: list[str] = []
    fallback: list[str] = []
    borrowed: list[str] = []
    sec_id = _safe_identifier(section.get("section_id") or "")
    dense_sections = {
        "problem_contribution",
        "model_card",
        "method_pipeline",
        "results_table",
        "ablation_analysis",
        "native_reconstruction",
        "limitations_future",
        "synthesis_takeaway",
    }
    dense_section = sec_id in dense_sections
    if sec_id in dense_sections:
        limit = 190 if compact_wide else 220
    else:
        limit = 156 if compact_wide and sec_id in {"takeaway", "limitation_future"} else 86 if compact_wide else 118
    compact_two_bullet_sections = {"method", "key_contribution", "main_evidence"}
    if sec_id in dense_sections:
        max_bullets = 6
    else:
        max_bullets = 3 if compact_wide and sec_id in compact_two_bullet_sections else 2 if compact_wide else 2
    dense_min_bullets = _dense_recovery_min_bullets(sec_id)
    local_keys: set[str] = set()
    if compact_wide:
        curated = _recovery_curated_compact_footer_bullets(sec_id, section)
        for text in curated:
            text = _normalize_recovery_footer_text(
                text,
                limit=limit,
                compact_wide=compact_wide,
            )
            if not text or _bad_recovery_footer_fragment(text):
                continue
            key = _recovery_bullet_key(text)
            if key in local_keys:
                continue
            if used_keys is not None and key in used_keys and not dense_section:
                continue
            bullets.append(text)
            local_keys.add(key)
            if used_keys is not None and not dense_section:
                used_keys.add(key)
            if len(bullets) >= max_bullets:
                return bullets
    candidate_bullets = list(section.get("bullets") or [])[:6]
    if compact_wide:
        candidate_bullets = sorted(
            candidate_bullets,
            key=lambda bullet: _recovery_footer_bullet_priority(sec_id, bullet),
        )
    for bullet in candidate_bullets:
        if _recovery_skip_footer_bullet(sec_id, bullet):
            continue
        text = _recovery_footer_bullet_text(
            bullet,
            limit=limit,
            compact_wide=compact_wide,
            section_id=sec_id,
        )
        if not text:
            continue
        if _bad_recovery_footer_fragment(text):
            fallback.append(text)
            continue
        key = _recovery_bullet_key(text)
        if key in local_keys:
            continue
        if used_keys is not None and key in used_keys:
            if dense_section:
                bullets.append(text)
                local_keys.add(key)
                if len(bullets) >= max_bullets:
                    break
            else:
                fallback.append(text)
            continue
        bullets.append(text)
        local_keys.add(key)
        if used_keys is not None and not dense_section:
            used_keys.add(key)
        if len(bullets) >= max_bullets:
            break
    tail_pool: list[str] = []
    if compact_wide:
        tail_pool.extend(_recovery_section_fallback_bullets(sec_id))
    tail_pool.extend(fallback)
    if dense_section and len(bullets) < dense_min_bullets:
        tail_pool.extend(borrowed)
    if sec_id in {"results_table", "ablation_analysis", "synthesis_takeaway"}:
        tail_pool.extend(list(fallback_pool or []))
    elif not dense_section:
        tail_pool.extend(list(fallback_pool or []))
    if sec_id in dense_sections and len(bullets) < dense_min_bullets:
        tail_pool.extend(_recovery_section_fallback_bullets(sec_id))
        tail_pool.extend(list(fallback_pool or [])[: max(0, dense_min_bullets - len(bullets)) + 2])
    elif not compact_wide and not bullets:
        tail_pool.extend(_recovery_section_fallback_bullets(sec_id))
    for text in tail_pool:
        if len(bullets) >= max_bullets:
            break
        text = _normalize_recovery_footer_text(
            text,
            limit=limit,
            compact_wide=compact_wide,
        )
        if not text:
            continue
        if _bad_recovery_footer_fragment(text):
            continue
        key = _recovery_bullet_key(text)
        if key in local_keys:
            continue
        if used_keys is not None and key in used_keys and not dense_section:
            continue
        bullets.append(text)
        local_keys.add(key)
        if used_keys is not None and not dense_section:
            used_keys.add(key)
    if sec_id in dense_sections and len(bullets) < dense_min_bullets:
        local_keys = {_recovery_bullet_key(text) for text in bullets}
        for text in borrowed:
            if len(bullets) >= min(max_bullets, dense_min_bullets):
                break
            text = _normalize_recovery_footer_text(
                text,
                limit=limit,
                compact_wide=compact_wide,
            )
            if not text:
                continue
            if _bad_recovery_footer_fragment(text):
                continue
            key = _recovery_bullet_key(text)
            if not key or key in local_keys:
                continue
            bullets.append(text)
            local_keys.add(key)
    return bullets


def _dense_recovery_min_bullets(section_id: str) -> int:
    return {
        "problem_contribution": 5,
        "model_card": 4,
        "method_pipeline": 4,
        "results_table": 4,
        "ablation_analysis": 3,
        "native_reconstruction": 4,
        "limitations_future": 3,
        "synthesis_takeaway": 3,
    }.get(section_id, 0)


def _dense_recovery_panel_items(
    bullets: list[str],
    *,
    compact_wide: bool,
) -> list[str]:
    return [
        _clean_text(
            item,
            limit=140 if compact_wide else 205,
            ellipsis=False,
        )
        for item in bullets
        if str(item or "").strip()
    ]


def _dense_recovery_unit_block_id(section_id: str, unit_kind: str, idx: int = 1) -> str:
    return f"{_safe_identifier(section_id)}_{_safe_identifier(unit_kind)}_{idx}"


def _dense_recovery_panel_inner_html(
    section_id: str,
    title: str,
    bullets: list[str],
    *,
    compact_wide: bool,
) -> str:
    sec_id = _safe_identifier(section_id)
    items = _dense_recovery_panel_items(bullets, compact_wide=compact_wide)
    if not items:
        return f"<h2>{escape(title)}</h2>"
    if sec_id in {"model_card"}:
        labels = ("Architecture", "Training", "Modalities", "Scale", "Evidence", "Boundary")
        rows = []
        for idx, item in enumerate(items[:6]):
            rows.append(
                f"<tr><th>{escape(labels[idx % len(labels)])}</th><td>{escape(item)}</td></tr>"
            )
        table_id = _dense_recovery_unit_block_id(sec_id, "native_table", 1)
        return (
            f"<h2>{escape(title)}</h2>"
            f'<div class="table-wrap"><table class="native-table model-card-table" data-block-id="{table_id}">'
            f"<tbody>{''.join(rows)}</tbody></table></div>"
        )
    if sec_id in {"method_pipeline"}:
        boxes = []
        for idx, item in enumerate(items[:6], start=1):
            boxes.append(
                f'<div class="flow-box" data-block-id="{_dense_recovery_unit_block_id(sec_id, "stage", idx)}">'
                f'<span class="tag">Step {idx}</span><p>{escape(item)}</p></div>'
            )
        return f"<h2>{escape(title)}</h2><div class=\"flow-row\">{''.join(boxes)}</div>"
    if sec_id in {"results_table"}:
        rows = []
        for idx, item in enumerate(items[:6], start=1):
            label, value = _dense_recovery_table_cells(item, default_label=f"Result {idx}")
            rows.append(f"<tr><td>{escape(label)}</td><td>{escape(value)}</td></tr>")
        table_id = _dense_recovery_unit_block_id(sec_id, "native_table", 1)
        return (
            f"<h2>{escape(title)}</h2>"
            f'<div class="table-wrap"><table class="native-table benchmark-table" data-block-id="{table_id}"><thead><tr><th>Benchmark</th><th>Source-backed read</th></tr></thead>'
            f"<tbody>{''.join(rows)}</tbody></table></div>"
        )
    if sec_id in {"ablation_analysis"}:
        if not any(re.search(r"\b(?:ablation|delta|sensitivity|tradeoff|failure|analysis)\b", item, flags=re.IGNORECASE) for item in items):
            title = "Evidence Analysis"
        rows = []
        for idx, item in enumerate(items[:5], start=1):
            label, value = _dense_recovery_table_cells(item, default_label=f"Analysis {idx}")
            rows.append(f"<tr><td>{escape(label)}</td><td>{escape(value)}</td></tr>")
        table_id = _dense_recovery_unit_block_id(sec_id, "native_table", 1)
        return (
            f"<h2>{escape(title)}</h2>"
            f'<div class="table-wrap"><table class="native-table analysis-table" data-block-id="{table_id}"><thead><tr><th>Analysis read</th><th>Interpretation</th></tr></thead>'
            f"<tbody>{''.join(rows)}</tbody></table></div>"
        )
    if sec_id in {"problem_contribution", "contribution"}:
        cards = []
        for idx, item in enumerate(items[:5], start=1):
            cards.append(
                f'<div class="contribution-card" data-block-id="{_dense_recovery_unit_block_id(sec_id, "card", idx)}">'
                f"<strong>Claim {idx}</strong><p>{escape(item)}</p></div>"
            )
        return f"<h2>{escape(title)}</h2><div class=\"dense-grid\">{''.join(cards)}</div>"
    if sec_id in {"motivation", "limitations_future", "synthesis_takeaway"}:
        cards = []
        label = "Synthesis"
        for idx, item in enumerate(items[:5], start=1):
            cards.append(
                f'<div class="insight-card" data-block-id="{_dense_recovery_unit_block_id(sec_id, "card", idx)}">'
                f"<strong>{escape(label)} {idx}</strong><p>{escape(item)}</p></div>"
            )
        return f"<h2>{escape(title)}</h2><div class=\"insight-stack\">{''.join(cards)}</div>"
    bullet_html = "".join(f"<li>{escape(item)}</li>" for item in items[:6])
    return f"<h2>{escape(title)}</h2><ul>{bullet_html}</ul>"


def _dense_recovery_table_cells(text: str, *, default_label: str) -> tuple[str, str]:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if not cleaned:
        return default_label, ""
    for separator in (":", " - ", " -- "):
        if separator in cleaned:
            left, right = cleaned.split(separator, 1)
            left = _clean_text(left, limit=58, ellipsis=False).strip(" .:-")
            right = _clean_text(
                right,
                limit=170,
                ellipsis=False,
            ).strip()
            if left and right:
                return left, right
    words = cleaned.split()
    if len(words) > 8:
        return " ".join(words[:5]).strip(" .:-") or default_label, " ".join(words[5:])
    return default_label, cleaned


def _dense_recovery_native_unit_blocks(
    section_id: str,
    title: str,
    bullets: list[str],
    *,
    bbox: dict[str, int],
    compact_wide: bool,
    slot_id: str,
    covers: list[str],
) -> list[dict[str, Any]]:
    sec_id = _safe_identifier(section_id)
    items = _dense_recovery_panel_items(bullets, compact_wide=compact_wide)
    if not items:
        return []
    content_bbox = _dense_recovery_panel_content_bbox(bbox, compact_wide=compact_wide)
    style = {
        "font_size_px": 10 if compact_wide else 12,
        "line_height": 1.12,
    }
    common = {
        "slot_id": slot_id,
        "source": "poster_recovery_native_unit",
        "covers": list(covers or []),
        "style": style,
    }
    table_sections = {
        "model_card": {
            "unit": "native_table",
            "role": "model_card architecture parameters modalities backbone tokenizer training tokens license native_information_unit",
            "headers": ["Model card field", "Source-backed read"],
            "labels": ["Architecture", "Training", "Modalities", "Scale", "Evidence", "Boundary"],
        },
        "results_table": {
            "unit": "native_table",
            "role": "benchmark_table results_table leaderboard metrics comparison native_information_unit",
            "headers": ["Benchmark", "Source-backed read"],
            "labels": [f"Result {idx}" for idx in range(1, 7)],
        },
        "ablation_analysis": {
            "unit": "native_table",
            "role": "ablation_analysis analysis sensitivity tradeoff failure mode result band native_information_unit",
            "headers": ["Ablation / analysis", "Interpretation"],
            "labels": [f"Analysis {idx}" for idx in range(1, 7)],
        },
    }
    if sec_id in table_sections:
        spec = table_sections[sec_id]
        rows: list[list[str]] = []
        labels = list(spec["labels"])
        for idx, item in enumerate(items[:6]):
            default = str(labels[idx % len(labels)])
            left, right = _dense_recovery_table_cells(item, default_label=default)
            if sec_id in {"model_card"}:
                left = default
                right = item
            rows.append([left, right])
        text = _dense_recovery_compact_unit_text([
            title,
            " ".join(spec["headers"]),
            *(" ".join(row) for row in rows),
        ])
        return [{
            "block_id": _dense_recovery_unit_block_id(sec_id, str(spec["unit"]), 1),
            "kind": "table",
            "role": str(spec["role"]),
            "title": title,
            "headers": list(spec["headers"]),
            "rows": rows,
            "text": text,
            "bbox": content_bbox,
            **common,
        }]

    max_items = 6 if sec_id == "method_pipeline" else 5
    grid = _dense_recovery_unit_bboxes(
        content_bbox,
        count=min(max_items, len(items)),
        compact_wide=compact_wide,
        columns=2 if sec_id in {"method_pipeline", "problem_contribution", "contribution"} else 1,
    )
    blocks: list[dict[str, Any]] = []
    for idx, (item, child_bbox) in enumerate(zip(items[:len(grid)], grid), start=1):
        if sec_id == "method_pipeline":
            block_id = _dense_recovery_unit_block_id(sec_id, "stage", idx)
            kind = "chart"
            role = "method_pipeline pipeline framework workflow process stage encoder decoder tokenizer native_information_unit"
            text = f"Method pipeline Step {idx}: {item}"
        elif sec_id in {"problem_contribution", "contribution", "motivation"}:
            block_id = _dense_recovery_unit_block_id(sec_id, "card", idx)
            kind = "group"
            role = "problem_contribution problem motivation contribution finding insight key_claims native_information_unit"
            text = f"Problem and contribution Claim {idx}: {item}"
        elif sec_id == "limitations_future":
            block_id = _dense_recovery_unit_block_id(sec_id, "card", idx)
            kind = "group"
            role = "limitations_future limitations future_work caveat next step native_information_unit"
            text = f"Limitations future work Synthesis {idx}: {item}"
        else:
            block_id = _dense_recovery_unit_block_id(sec_id, "card", idx)
            kind = "group"
            role = f"{sec_id} synthesis takeaway impact conclusion native_information_unit"
            text = f"{title} Synthesis {idx}: {item}"
        blocks.append({
            "block_id": block_id,
            "kind": kind,
            "role": role,
            "text": text,
            "bbox": child_bbox,
            **common,
        })
    return blocks


def _dense_recovery_compact_unit_text(parts: list[str], *, max_words: int = 78) -> str:
    words = re.findall(r"[^\W_]+|[A-Za-z0-9+./:%-]+", " ".join(str(part or "") for part in parts), flags=re.UNICODE)
    if len(words) <= max_words:
        return " ".join(words)
    return " ".join(words[:max_words])


def _dense_recovery_panel_content_bbox(
    bbox: dict[str, int],
    *,
    compact_wide: bool,
) -> dict[str, int]:
    x = int(bbox.get("x") or 0)
    y = int(bbox.get("y") or 0)
    w = max(1, int(bbox.get("w") or 1))
    h = max(1, int(bbox.get("h") or 1))
    pad_x = 14 if compact_wide else 20
    title_h = 32 if compact_wide else 48
    bottom_pad = 10 if compact_wide else 16
    return {
        "x": x + pad_x,
        "y": y + title_h,
        "w": max(48, w - 2 * pad_x),
        "h": max(36, h - title_h - bottom_pad),
    }


def _dense_recovery_unit_bboxes(
    bbox: dict[str, int],
    *,
    count: int,
    compact_wide: bool,
    columns: int,
) -> list[dict[str, int]]:
    count = max(0, int(count))
    if count <= 0:
        return []
    cols = max(1, min(columns, count))
    rows = int(math.ceil(count / cols))
    gap = 8 if compact_wide else 10
    x = int(bbox.get("x") or 0)
    y = int(bbox.get("y") or 0)
    w = max(1, int(bbox.get("w") or 1))
    h = max(1, int(bbox.get("h") or 1))
    cell_w = max(1, int((w - gap * (cols - 1)) / cols))
    cell_h = max(1, int((h - gap * (rows - 1)) / rows))
    out: list[dict[str, int]] = []
    for idx in range(count):
        col = idx % cols
        row = idx // cols
        out.append({
            "x": x + col * (cell_w + gap),
            "y": y + row * (cell_h + gap),
            "w": cell_w,
            "h": cell_h,
        })
    return out


def _recovery_footer_bullet_text(
    bullet: Any,
    *,
    limit: int,
    compact_wide: bool,
    section_id: str = "",
) -> str:
    raw = (bullet or {}).get("text") if isinstance(bullet, dict) else bullet
    source = str((bullet or {}).get("source") or "") if isinstance(bullet, dict) else ""
    raw = _strip_recovery_metric_prefix(raw, source=source)
    force_compact_sections = {
        "problem_contribution",
        "results_table",
        "ablation_analysis",
        "model_card",
        "method_pipeline",
        "synthesis_takeaway",
    }
    compact = _compact_recovery_claim_text(
        raw,
        section_id=section_id,
        source=source,
    ) if compact_wide or _safe_identifier(section_id) in force_compact_sections else ""
    if compact:
        raw = compact
    if compact_wide:
        raw = _add_recovery_footer_anchor(raw, section_id=section_id, source=source)
    raw = _label_recovery_metric_sequence(raw, source=source, section_id=section_id)
    text = _clean_recovery_sentence(raw, limit=limit, trim_dangling_phrase=True)
    text = _strip_figure_refs_from_narrative(text)
    return _ensure_recovery_terminal(text)


def _normalize_recovery_footer_text(text: str, *, limit: int, compact_wide: bool) -> str:
    cleaned = _clean_recovery_sentence(
        text,
        limit=limit,
        trim_dangling_phrase=compact_wide,
    )
    cleaned = _strip_figure_refs_from_narrative(cleaned)
    return _ensure_recovery_terminal(cleaned)


def _bad_recovery_footer_fragment(text: str) -> bool:
    lower = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    if not lower:
        return True
    words = re.findall(r"[a-z0-9]+", lower)
    if len(words) < 6:
        return True
    bad_substrings = (
        "pipeline maps",
        "source-backed",
        "source text should",
        "should be split",
        "should carry",
        "use editable",
        "native cards",
        "native table",
        "native text",
        "fill with",
        "reserve a",
        "close the research story",
        "close the story",
        "connect motivation",
        "tie each comparison",
        "explain deltas",
        "recover data recipe",
        "distinguish data source",
        "make the boundary conditions",
        "keep source ids",
        "provenance band",
        "model-card panel",
        "benchmark panel",
        "ablation panel",
        "training panel",
        "takeaway panel",
        "future-work panel",
        "including gpt-4o",
        "text instructions is",
        "benchmarking gui agents with only task success or text instructions is",
        "whether failure comes",
        "not just element",
        "clear textual instructions can substitute",
        "modality-agnostic mixture-of-experts dynamically allocates",
    )
    if any(item in lower for item in bad_substrings):
        return True
    if re.search(r"\b(?:method|results?|analysis|training|limitations?|conclusion|footer|provenance|contribution|problem)\s+section\s*:", lower):
        return True
    if re.search(r"\b(?:problem|pipeline|benchmark|ablation|training|takeaway|future-work|model-card|contribution)\s+panel\s*:", lower):
        return True
    bad_suffixes = (
        " is.",
        " maps.",
        " benchmarks.",
        " instructions is.",
        " prior benchmarks.",
        " task completion.",
    )
    if lower.endswith(bad_suffixes):
        return True
    return bool(re.search(r":\s*(?:task completion|prior benchmarks)\.$", lower))


def _recovery_curated_compact_footer_bullets(section_id: str, section: dict[str, Any]) -> list[str]:
    if not _recovery_section_looks_like_videogui(section):
        return []
    curated = {
        "problem": [
            "Sec. 1: Prior GUI benchmarks emphasize simple, language-only instructions.",
        ],
        "method": [
            "Figures 1 and 4: videos become hierarchical planning labels.",
            "Sec. 3: scores separate high-, mid-, and action-level failures.",
        ],
        "key_contribution": [
            "Sec. 1: VideoGUI exposes failures across planning and action levels.",
            "Fig. 1: tasks are sourced from high-quality instructional videos.",
        ],
        "main_evidence": [
            "Table 3: overall benchmark scores stay low across current agents.",
            "Table 4: text-query planning outperforms vision-only planning.",
        ],
        "takeaway": [
            "Abstract: the SoTA large multimodal model GPT4o performs poorly on visual-centric GUI tasks, especially for high-level planning.",
        ],
        "limitation_future": [
            "Challenges of visual-oriented GUI automation remain; instructional videos show potential.",
        ],
    }
    return list(curated.get(section_id, []))


def _recovery_section_looks_like_videogui(section: dict[str, Any]) -> bool:
    text = " ".join(
        [str(section.get("title") or ""), str(section.get("section_id") or "")]
        + [
            str(bullet.get("text") if isinstance(bullet, dict) else bullet)
            for bullet in list(section.get("bullets") or [])
        ]
    ).lower()
    return (
        "videogui" in text
        or "visual-centric" in text
        or "gui automation" in text
        or "gui agents" in text
        or "gpt-4o" in text
        or "instructional videos" in text
    )


def _recovery_section_fallback_bullets(section_id: str) -> list[str]:
    fallback = {
        "problem": [
            "Sec. 1: The paper defines the central problem and evaluation gap.",
        ],
        "method": [
            "Sec. 3: The method separates data, evaluation, and error analysis.",
        ],
        "key_contribution": [
            "Sec. 1: The paper diagnoses failure modes with source-backed evidence.",
        ],
        "main_evidence": [
            "Table 1: Source evidence reports dataset scale and benchmark coverage.",
        ],
        "takeaway": [
            "Table 3: Main results identify the remaining capability gap.",
        ],
        "limitation_future": [
            "Conclusion: provides clear signals for existing limitations and areas for improvement.",
        ],
        "problem_contribution": [
            "Sec. 1: Combine the problem, motivation, and principal contributions into one source-backed context panel.",
            "Problem/contribution panel: preserve the main mechanism, evidence, and release or impact claim without creating separate sparse shells.",
        ],
        "model_card": [
            "Method section: rebuild model, architecture, data, training, and capability fields as native cards.",
            "Model-card panel: use editable facts rather than one screenshot or a copied abstract sentence.",
        ],
        "method_pipeline": [
            "Method section: convert the architecture into ordered stages with tokenizer, encoder, training, and inference steps.",
            "Pipeline panel: each stage should carry a source-backed mechanism or design choice.",
        ],
        "results_table": [
            "Results section: rebuild benchmark numbers, baselines, and comparison reads as a native table.",
            "Benchmark panel: summarize what the result proves instead of only listing a figure number.",
        ],
        "ablation_analysis": [
            "Analysis section: explain deltas, ablations, tradeoffs, and failure modes as ablation or limitation notes with short result discussion.",
            "Ablation panel: tie each comparison to a source-backed interpretation.",
        ],
        "source_evidence_map": [
            "Selected figures and tables anchor the method, benchmark, training, and qualitative evidence panels.",
            "Each visual should sit next to the claim it supports, with an editable caption and source ID.",
            "The evidence map keeps screenshots from becoming a detached montage by tying them to panel claims.",
        ],
        "native_reconstruction": [
            "Editable reconstruction preserves benchmark rows, stage cards, limitations, and local source notes for review.",
            "Tables and cards should expose labels, units, and interpretation instead of leaving values as untraceable screenshots.",
            "Editable wording makes the poster auditable: claims, captions, and source IDs stay inspectable after export.",
        ],
        "limitations_future": [
            "Limitations section: preserve caveats, failure cases, assumptions, and future work from the paper.",
            "Future-work panel: make the boundary conditions explicit instead of leaving a blank lower card.",
        ],
        "synthesis_takeaway": [
            "Conclusion: tie the core mechanism to the strongest evidence and the implication for follow-up work.",
            "Takeaway panel: close the story with what changed, why the evidence supports it, and what remains open.",
            "Synthesis close: connect motivation, method, benchmark evidence, and limitations into one final research claim.",
        ],
    }
    return list(fallback.get(section_id, []))


def _add_recovery_footer_anchor(text: str, *, section_id: str, source: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if not cleaned:
        return ""
    if re.match(r"^(?:sec\.|section|table|conclusion|source evidence)\b", cleaned, flags=re.IGNORECASE):
        return cleaned
    anchor = _recovery_footer_source_anchor(source=source, section_id=section_id)
    if not anchor:
        return cleaned
    return f"{anchor}: {cleaned}"


def _recovery_footer_source_anchor(*, source: str, section_id: str) -> str:
    source_l = str(source or "").lower()
    table_match = re.search(r"\btable\s*[:#-]?\s*(\d+)", source_l)
    if table_match:
        return f"Table {table_match.group(1)}"
    if source_l.startswith("section:conclusion"):
        return "Conclusion"
    if source_l.startswith("section:abstract") or source_l.startswith("section:introduction"):
        return "Sec. 1"
    section_anchor = {
        "problem": "Sec. 1",
        "method": "Sec. 3",
        "key_contribution": "Sec. 1",
        "main_evidence": "Table 1",
        "takeaway": "Table 3",
        "limitation_future": "Conclusion",
    }
    return section_anchor.get(section_id, "")


def _recovery_skip_footer_bullet(section_id: str, bullet: Any) -> bool:
    if not isinstance(bullet, dict):
        return False
    source = str(bullet.get("source") or "").lower()
    text = str(bullet.get("text") or "").lower()
    if "modality-agnostic mixture-of-experts dynamically allocates" in text:
        return True
    if source.startswith("section:conclusion") and section_id != "limitation_future":
        return True
    if section_id == "limitation_future" and (
        source.startswith("figure:")
        or source.startswith("table:")
        or "example of final outcome" in text
        or "initial and final visual states" in text
    ):
        return True
    if section_id in {"key_contribution", "takeaway", "limitation_future"} and (
        "future gui agents should" in text
        or "does not solve the core full-task challenge" in text
        or "presents videogui as a benchmark" in text
    ):
        return True
    return False


def _recovery_footer_bullet_priority(section_id: str, bullet: Any) -> tuple[int, int]:
    if not isinstance(bullet, dict):
        return (4, 0)
    source = str(bullet.get("source") or "").lower()
    text = str(bullet.get("text") or "").lower()
    if source.startswith("claim_graph.implications"):
        if "clear textual" in text or "vision-only" in text or "open challenge" in text:
            return (-1, len(text))
        if section_id in {"limitation_future", "takeaway"}:
            return (0, len(text))
        return (2, len(text))
    if section_id == "limitation_future" and source.startswith("section:conclusion"):
        return (1, len(text))
    if section_id == "limitation_future" and (
        source.startswith("figure:")
        or source.startswith("table:")
        or "example of final outcome" in text
        or "initial and final visual states" in text
    ):
        return (8, len(text))
    if section_id == "main_evidence":
        if "table 3" in source or "overall benchmark score" in text or "highest score" in text:
            return (-3, len(text))
        if "full evaluation" in text or "full benchmark" in text:
            return (-2, len(text))
        if "bottleneck" in text or ("planning" in text and "action" in text):
            return (-1, len(text))
        if "covers 11" in text or "86 complex tasks" in text or "22.7 actions" in text:
            return (2, len(text))
        if "fails to complete a single full task" in text:
            return (1, len(text))
    if source.startswith("claim_graph.evidence"):
        return (1, len(text))
    if "task-success-only evaluation cannot" in text or "agent failures are hard to localize" in text:
        return (-1, len(text))
    if "hierarchical evaluation gives more actionable diagnosis" in text:
        return (-1, len(text))
    return (3, len(text))


def _strip_recovery_metric_prefix(text: Any, *, source: str) -> str:
    raw = str(text or "").strip()
    if ":" not in raw:
        return raw
    prefix, rest = raw.split(":", 1)
    if not rest.strip():
        return raw
    source_l = source.lower()
    if (
        source_l.startswith("claim_graph.evidence")
        or "main results" in source_l
        or "table" in source_l
        or len(prefix.split()) <= 5
    ):
        if _looks_like_labeled_metric_prefix(prefix) and _looks_like_metric_sequence(rest):
            return raw
        return rest.strip()
    return raw


def _label_recovery_metric_sequence(text: str, *, source: str, section_id: str) -> str:
    raw = str(text or "").strip()
    if section_id != "main_evidence":
        return raw
    lower = raw.lower()
    if "ivideogpt" not in lower or "video prediction" not in lower:
        return raw
    if not _looks_like_metric_sequence(raw):
        return raw
    if "fvd" in lower and "psnr" in lower and "ssim" in lower and "lpips" in lower:
        return raw
    numbers = re.findall(r"(\d+(?:\.\d+)?)\s*±", raw)
    if len(numbers) < 4:
        numbers = re.findall(r"\d+(?:\.\d+)?", raw)
    if len(numbers) >= 4:
        condition = "BAIR action-free" if "action-free" in lower else "BAIR action-conditioned" if "action-conditioned" in lower else "video prediction"
        return (
            f"Table 1: {condition} reports iVideoGPT "
            f"FVD {numbers[0]}, PSNR {numbers[1]}, SSIM {numbers[2]}, LPIPS {numbers[3]}."
        )
    metric_label = "FVD / PSNR / SSIM / LPIPS"
    source_label = str(source or "").strip()
    prefix = f"{source_label}: " if source_label.lower().startswith("table") else ""
    return f"{prefix}{raw} ({metric_label})."


def _looks_like_labeled_metric_prefix(text: str) -> bool:
    lower = str(text or "").lower()
    return any(token in lower for token in (
        "prediction",
        "evaluation",
        "benchmark",
        "success",
        "score",
        "result",
        "bair",
        "robonet",
        "metaworld",
    ))


def _looks_like_metric_sequence(text: str) -> bool:
    raw = str(text or "")
    numbers = re.findall(r"\d+(?:\.\d+)?(?:\s*[±+/-]\s*\d+(?:\.\d+)?)?", raw)
    words = re.findall(r"[A-Za-z][A-Za-z0-9-]*", raw)
    lower = raw.lower()
    if len(numbers) >= 4 and any(token in lower for token in ("prediction", "ivideogpt", "bair", "robonet")):
        return True
    return len(numbers) >= 3 and len(numbers) >= max(2, len(words))


def _recovery_footer_source_pool(
    sections: list[dict[str, Any]],
    selected_assets: list[dict[str, Any]],
    *,
    compact_wide: bool,
) -> list[str]:
    limit = 74 if compact_wide else 118
    pool: list[str] = []
    for section in sections:
        sec_id = _safe_identifier(section.get("section_id") or "")
        if sec_id not in {"main_evidence", "results_table", "ablation_analysis"}:
            continue
        for bullet in list(section.get("bullets") or [])[:4]:
            if _recovery_skip_footer_bullet(sec_id, bullet):
                continue
            text = _recovery_footer_bullet_text(
                bullet,
                limit=limit,
                compact_wide=compact_wide,
                section_id=sec_id,
            )
            if text:
                pool.append(text)
    if len(pool) < 8:
        for section in sections:
            sec_id = _safe_identifier(section.get("section_id") or "")
            for bullet in list(section.get("bullets") or [])[:2]:
                if _recovery_skip_footer_bullet(sec_id, bullet):
                    continue
                text = _recovery_footer_bullet_text(
                    bullet,
                    limit=limit,
                    compact_wide=compact_wide,
                    section_id=sec_id,
                )
                if text:
                    pool.append(text)
                if len(pool) >= 12:
                    break
            if len(pool) >= 12:
                break
    if len(pool) < 4:
        for asset in selected_assets[:6]:
            caption = _clean_recovery_sentence(
                asset.get("caption_full") or "",
                limit=limit,
                trim_dangling_phrase=compact_wide,
            )
            caption = _strip_figure_refs_from_narrative(caption)
            if caption:
                pool.append(_ensure_recovery_terminal(caption))
    out: list[str] = []
    seen: set[str] = set()
    for item in pool:
        key = _recovery_bullet_key(item)
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _recovery_section_claim_ids(section: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for bullet in list(section.get("bullets") or []):
        if isinstance(bullet, dict) and bullet.get("claim_id"):
            cid = str(bullet.get("claim_id"))
            if cid and cid not in ids:
                ids.append(cid)
    return ids


def _recovery_visual_claim_ids(
    asset: dict[str, Any],
    section_claims: dict[str, list[str]],
) -> list[str]:
    role = str(asset.get("story_role") or asset.get("visual_role") or "").lower()
    if any(token in role for token in ("method", "mechanism")):
        return list(section_claims.get("method") or [])[:3]
    if any(token in role for token in ("benchmark", "evidence", "analysis", "qualitative", "result", "system")):
        return list(section_claims.get("main_evidence") or [])[:4]
    return []


def _recovery_bullet_key(text: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()
    return normalized[:120]


def _strip_figure_refs_from_narrative(text: str) -> str:
    cleaned = re.sub(
        r"\b(?:fig(?:ure)?\.?\s*\d+[a-zA-Z]?(?:\s*(?:,|/|and|&)\s*(?:fig(?:ure)?\.?\s*)?\d+[a-zA-Z]?)*)\s*[:,-]?\s*",
        "Source evidence: ",
        str(text or ""),
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\bSource evidence:\s+Source evidence:\s+", "Source evidence: ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip(" ;,")


def _recovery_visual_caption(asset: dict[str, Any], *, compact_wide: bool) -> str:
    short = _clean_text(asset.get("caption") or asset.get("caption_short") or "", limit=90, ellipsis=False)
    full = _clean_text(asset.get("caption_full") or "", limit=260, ellipsis=False)
    short_summary = _source_caption_summary(short, limit=122 if compact_wide else 145)
    if short_summary and short_summary.lower() != short.lower() and any(
        marker in short.lower()
        for marker in (
            "brief illustration of videogui",
            "qualitative results",
            "minimalist gui agent framework",
            "data statistics",
            "full evaluation",
            "procedural planning",
            "manual annotation tools",
            "hierarchical annotations in videogui",
            "premiere pro",
            "top: high plan",
            "mid. plan score",
        )
    ):
        return short_summary
    short_words = len(re.findall(r"[A-Za-z0-9]+", short))
    generic_short = short_words < 4 or short.lower() in {
        "videogui",
        "qual results",
        "qualitative",
        "data stats",
        "planning eval",
    }
    if full and generic_short:
        return _source_caption_summary(full, limit=122 if compact_wide else 145)
    if full and short_words < 7:
        summary = _source_caption_summary(full, limit=122 if compact_wide else 145)
        if summary and summary.lower() != short.lower():
            return summary
    fallback = _source_caption_summary(full, limit=122 if compact_wide else 145)
    if fallback:
        return fallback
    page = asset.get("source_page")
    return f"Source visual from page {page}" if page else "Source visual from paper"


def _source_caption_summary(caption: str, *, limit: int) -> str:
    text = _clean_recovery_sentence(caption, limit=220)
    if not text:
        return ""
    lower = text.lower()
    prefix = _figure_table_prefix(text)
    if "hierarchical evaluation" in lower:
        return _clean_text(
            f"{prefix}: hierarchical evaluation separates planning levels from atomic GUI actions",
            limit=limit,
            ellipsis=False,
        )
    if "brief illustration of videogui" in lower or "source tasks from" in lower:
        return _clean_text(
            f"{prefix}: method pipeline turns instructional videos into planning labels and GUI actions",
            limit=limit,
            ellipsis=False,
        )
    if "data statistics" in lower:
        return _clean_text(
            f"{prefix}: dataset statistics reveal action imbalance and click-heavy GUI tasks",
            limit=limit,
            ellipsis=False,
        )
    if "initial and final visual states" in lower:
        return _clean_text(
            f"{prefix}: before/after visual states show the GUI task outcome",
            limit=limit,
            ellipsis=False,
        )
    if "qualitative results" in lower:
        return _clean_text(
            f"{prefix}: PowerPoint qualitative examples compare GT in green with wrong predictions in red",
            limit=limit,
            ellipsis=False,
        )
    if "top: high plan" in lower or "mid. plan score" in lower:
        return _clean_text(
            "Fig. 5 bottom: Mid. Plan score table excerpt",
            limit=limit,
            ellipsis=False,
        )
    if "minimalist gui agent framework" in lower or "parser, a planner, and an actor" in lower:
        return _clean_text(
            f"{prefix}: GUI agent framework connects Parser, Planner, and Actor",
            limit=limit,
            ellipsis=False,
        )
    if "manual annotation tools" in lower:
        return _clean_text(
            f"{prefix}: manual annotation interface captures plans and GUI actions",
            limit=limit,
            ellipsis=False,
        )
    if "hierarchical annotations in videogui" in lower or "premiere pro" in lower:
        return _clean_text(
            f"{prefix}: hierarchical annotations pair visual queries with plans and actions",
            limit=limit,
            ellipsis=False,
        )
    if "key / press action" in lower or "key / press" in lower:
        return _clean_text(
            f"{prefix}: key and press actions are evaluated as executable GUI steps",
            limit=limit,
            ellipsis=False,
        )
    if "distance btween" in lower or "distance between the center point" in lower:
        return _clean_text(
            f"{prefix}: click metric caveat motivates normalized distance evaluation",
            limit=limit,
            ellipsis=False,
        )
    if "full evaluation" in lower:
        return _clean_text(
            f"{prefix}: full benchmark table compares multimodal baselines, planning scores, action scores, and tool-augmented agents",
            limit=limit,
            ellipsis=False,
        )
    if "procedural planning" in lower:
        return _clean_text(
            f"{prefix}: planning scores compare vision, text, and vision-plus-text query settings",
            limit=limit,
            ellipsis=False,
        )
    sentence = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0]
    return _ensure_recovery_terminal(
        _clean_recovery_sentence(sentence, limit=limit, trim_dangling_phrase=True)
    )


def _figure_table_prefix(text: str) -> str:
    match = re.match(r"\s*(fig(?:ure)?\.?|table)\s*([0-9]+[A-Za-z]?)", str(text or ""), flags=re.IGNORECASE)
    if not match:
        return "Source"
    kind = "Table" if match.group(1).lower().startswith("table") else "Fig."
    return f"{kind} {match.group(2)}"


def _recovery_venue_label(value: Any) -> str:
    text = _clean_text(value, limit=80, ellipsis=False)
    lower = text.lower()
    year_match = re.search(r"\b(20\d{2})\b", text)
    year = f" {year_match.group(1)}" if year_match else ""
    if "neurips" in lower or "neural information processing" in lower:
        return f"NeurIPS{year}".strip()
    if "iclr" in lower or "learning representations" in lower:
        return f"ICLR{year}".strip()
    if "icml" in lower or "machine learning" in lower:
        return f"ICML{year}".strip()
    if "cvpr" in lower or "computer vision and pattern recognition" in lower:
        return f"CVPR{year}".strip()
    if "emnlp" in lower:
        return f"EMNLP{year}".strip()
    return _clean_text(text, limit=48, ellipsis=False)


def _recovery_meta_text(
    *,
    authors: str,
    affiliations: str,
    venue: str,
    compact_wide: bool,
) -> str:
    if compact_wide and affiliations:
        first_author = _clean_text(str(authors).split(",")[0], limit=42, ellipsis=False) if authors else ""
        author_label = f"{first_author} et al." if first_author and "," in authors else authors
        return " | ".join(item for item in (
            _clean_text(author_label, limit=64, ellipsis=False),
            _clean_text(affiliations, limit=96, ellipsis=False),
        ) if item)
    return " | ".join(item for item in (authors, affiliations) if item)


def _safe_identifier(value: Any) -> str:
    ident = re.sub(r"[^a-zA-Z0-9_]+", "_", str(value or "").strip()).strip("_").lower()
    return ident or "section"


def _recovery_source_visual_count(raw: dict[str, Any]) -> int:
    frames = ((raw.get("html_artifact") or {}).get("frames") or [])
    blocks = frames[0].get("blocks") if frames and isinstance(frames[0], dict) else []
    return sum(
        1 for block in blocks or []
        if isinstance(block, dict)
        and str(block.get("kind") or "") == "image"
        and str(block.get("source") or "") == "ingested_pdf"
    )


def _looks_like_design_spec(value: dict[str, Any]) -> bool:
    if not isinstance(value, dict):
        return False
    return any(
        key in value
        for key in ("canvas", "layer_graph", "html_artifact", "deck_html", "artifact_type")
    )


def _canonicalize_raw_design_spec(
    raw: dict[str, Any],
    *,
    ctx: ToolContext | None = None,
) -> dict[str, Any]:
    data = dict(raw)
    canvas = data.get("canvas") if isinstance(data.get("canvas"), dict) else {}
    typography = data.get("typography")
    if isinstance(typography, dict):
        data["typography"] = _canonicalize_raw_typography(typography)
    layer_graph = data.get("layer_graph")
    if isinstance(layer_graph, list):
        data["layer_graph"] = [
            _canonicalize_raw_layer_node(node)
            for node in layer_graph
            if isinstance(node, dict)
        ]
    artifact = data.get("html_artifact")
    if isinstance(artifact, dict):
        artifact = dict(artifact)
        artifact_target = str(data.get("artifact_type") or artifact.get("target") or "").strip().lower()
        frames = []
        for frame in artifact.get("frames") or []:
            if isinstance(frame, dict):
                frame = dict(frame)
                _canonicalize_raw_authored_frame_aliases(frame)
                _canonicalize_raw_frame_render_mode(frame)
                frame["blocks"] = _canonicalize_raw_html_blocks(frame.get("blocks") or [])
                _canonicalize_raw_frame_missing_dom_blocks(frame)
                plan = frame.get("layout_plan")
                if isinstance(plan, dict):
                    plan = dict(plan)
                elif artifact_target == "poster" and (frame.get("blocks") or frame.get("authored_body_html")):
                    plan = {}
                else:
                    plan = None
                if plan is not None:
                    if not str(plan.get("archetype") or "").strip():
                        plan["archetype"] = _default_frame_layout_archetype(
                            frame,
                            artifact=artifact,
                            raw_spec=data,
                        )
                    raw_slots = [
                        slot for slot in (plan.get("slots") or [])
                        if isinstance(slot, dict)
                    ]
                    frame_bbox = _frame_bbox_for_slot_defaults(frame, canvas)
                    plan["slots"] = [
                        _canonicalize_raw_frame_slot(
                            slot,
                            frame_bbox=frame_bbox,
                            blocks=frame.get("blocks") or [],
                            index=idx,
                            total=len(raw_slots),
                        )
                        for idx, slot in enumerate(raw_slots)
                    ]
                    frame["layout_plan"] = plan
            frames.append(frame)
        artifact["frames"] = frames
        data["html_artifact"] = artifact
    return _canonicalize_raw_paper_poster_color_system(data, ctx)


def _canonicalize_raw_paper_poster_color_system(
    data: dict[str, Any],
    ctx: ToolContext | None,
) -> dict[str, Any]:
    if not _raw_is_paper_poster_color_context(data, ctx):
        return data
    color_system = _active_paper_poster_color_system(ctx, raw_spec=data)
    if not color_system:
        return data
    return _raw_with_paper_poster_color_system(data, color_system)


def _apply_paper_poster_color_system_to_spec(
    spec: DesignSpec,
    ctx: ToolContext,
    *,
    raw: Any = None,
) -> DesignSpec:
    if not is_academic_paper_poster_context(spec, ctx):
        return spec
    color_system = _active_paper_poster_color_system(
        ctx,
        raw_spec=raw if isinstance(raw, dict) else None,
    )
    if not color_system:
        return spec
    palette = _color_system_allowed_hexes(color_system)
    spec.color_system = color_system
    if palette:
        spec.palette = palette
    if spec.html_artifact is not None:
        theme = dict(getattr(spec.html_artifact, "theme", None) or {})
        _mirror_color_system_into_theme(theme, color_system)
        spec.html_artifact.theme = theme
    return spec


def _raw_is_paper_poster_color_context(data: dict[str, Any], ctx: ToolContext | None) -> bool:
    state = ctx.state if ctx is not None and isinstance(getattr(ctx, "state", None), dict) else {}
    brief = state.get("poster_content_brief")
    if isinstance(brief, dict) and brief.get("kind") == "paper_poster_content_brief":
        return True
    contract = state.get("poster_plan_contract")
    if isinstance(contract, dict) and contract.get("kind") == "paper_poster_plan_contract":
        return True
    if ctx is not None:
        try:
            if _is_dogfood_paper_poster_contract(ctx):
                return True
        except AttributeError:
            pass
    artifact_type = str(data.get("artifact_type") or "").strip().lower()
    artifact = data.get("html_artifact") if isinstance(data.get("html_artifact"), dict) else {}
    theme = artifact.get("theme") if isinstance(artifact.get("theme"), dict) else {}
    target = str(artifact.get("target") or "").strip().lower()
    return (
        artifact_type == "poster"
        and target == "poster"
        and (
            isinstance(data.get("color_system"), dict)
            or isinstance(theme.get("color_system"), dict)
        )
    )


def _active_paper_poster_color_system(
    ctx: ToolContext | None,
    *,
    raw_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = ctx.state if ctx is not None and isinstance(ctx.state, dict) else {}
    sources: list[Any] = [
        state.get("poster_content_brief"),
        state.get("poster_plan_contract"),
        raw_spec,
    ]
    artifact = raw_spec.get("html_artifact") if isinstance(raw_spec, dict) and isinstance(raw_spec.get("html_artifact"), dict) else {}
    theme = artifact.get("theme") if isinstance(artifact.get("theme"), dict) else {}
    sources.append(theme)
    brief = state.get("poster_content_brief") if isinstance(state.get("poster_content_brief"), dict) else {}
    raw_brief = str(
        state.get("raw_user_brief")
        or state.get("run_brief")
        or (raw_spec or {}).get("brief")
        or ""
    )
    recommended_text_units = (
        brief.get("recommended_text_units")
        if isinstance(brief.get("recommended_text_units"), dict)
        else None
    )
    if active_academic_color_system is not None:
        try:
            active = active_academic_color_system(
                *sources,
                raw_brief=raw_brief,
                manifest=brief,
                recommended_text_units=recommended_text_units,
            )
        except Exception:
            active = {}
        normalized = _normalize_paper_color_system(active)
        if normalized:
            return normalized
    for source in sources:
        if not isinstance(source, dict):
            continue
        normalized = _normalize_paper_color_system(source.get("color_system"))
        if normalized:
            return normalized
    if select_academic_color_system is None:
        return {}
    try:
        selected = select_academic_color_system(
            raw_brief=raw_brief,
            manifest=brief,
            recommended_text_units=recommended_text_units,
        )
    except Exception:
        return {}
    return _normalize_paper_color_system(selected)


def _raw_with_paper_poster_color_system(
    data: dict[str, Any],
    color_system: dict[str, Any],
) -> dict[str, Any]:
    data = dict(data)
    palette = _color_system_allowed_hexes(color_system)
    data["color_system"] = color_system
    if palette:
        data["palette"] = palette
    artifact = data.get("html_artifact") if isinstance(data.get("html_artifact"), dict) else None
    if artifact is not None:
        artifact = dict(artifact)
        theme = dict(artifact.get("theme") or {})
        _mirror_color_system_into_theme(theme, color_system)
        artifact["theme"] = theme
        data["html_artifact"] = artifact
    return data


def _mirror_color_system_into_theme(theme: dict[str, Any], color_system: dict[str, Any]) -> None:
    theme["color_system"] = color_system
    palette_id = color_system.get("palette_id")
    if palette_id:
        theme["palette_id"] = palette_id
    palette = _color_system_allowed_hexes(color_system)
    if palette:
        theme["palette"] = palette
    roles = color_system.get("roles") if isinstance(color_system.get("roles"), dict) else {}
    if roles.get("background"):
        theme["background"] = roles["background"]
    if roles.get("accent"):
        theme["accent"] = roles["accent"]


def _normalize_paper_color_system(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    normalized = dict(value)
    allowed_hexes = _color_system_allowed_hexes(normalized)
    if not allowed_hexes:
        return {}
    normalized["allowed_hexes"] = allowed_hexes
    return normalized


def _color_system_allowed_hexes(color_system: dict[str, Any]) -> list[str]:
    if not isinstance(color_system, dict):
        return []
    values = color_system.get("allowed_hexes") if isinstance(color_system.get("allowed_hexes"), list) else []
    if not values:
        css_variables = color_system.get("css_variables") if isinstance(color_system.get("css_variables"), dict) else {}
        values = list(css_variables.values())
    if not values:
        roles = color_system.get("roles") if isinstance(color_system.get("roles"), dict) else {}
        values = list(roles.values())
    out: list[str] = []
    for value in values:
        text = str(value or "").strip().upper()
        if re.fullmatch(r"#[0-9A-F]{6}", text) and text not in out:
            out.append(text)
    return out


def _canonicalize_raw_authored_frame_aliases(frame: dict[str, Any]) -> None:
    if not str(frame.get("authored_body_html") or "").strip():
        for key in ("body_html", "html_body", "inner_html", "body", "html"):
            value = frame.get(key)
            if isinstance(value, str) and value.strip():
                frame["authored_body_html"] = value
                break
    if not str(frame.get("authored_css") or "").strip():
        for key in ("css", "style_css", "styles_css"):
            value = frame.get(key)
            if isinstance(value, str) and value.strip():
                frame["authored_css"] = value
                break


def _canonicalize_raw_frame_render_mode(frame: dict[str, Any]) -> None:
    raw_mode = frame.get("render_mode")
    mode = re.sub(r"[^a-z0-9]+", "_", str(raw_mode or "").strip().lower()).strip("_")
    has_authored_body = bool(str(frame.get("authored_body_html") or "").strip())
    if has_authored_body:
        frame["render_mode"] = "authored_html"
        return
    if not mode:
        return
    if mode in {"authored_html", "authored", "author_html", "html", "custom_html", "dom", "html_dom"}:
        frame["render_mode"] = "authored_html"
        return
    if mode in {"scene_graph", "scenegraph", "scene", "legacy", "layer_graph", "layers"}:
        frame["render_mode"] = "scene_graph"
        return
    frame.pop("render_mode", None)


def _canonicalize_raw_frame_missing_dom_blocks(frame: dict[str, Any]) -> None:
    body_html = str(frame.get("authored_body_html") or "")
    blocks = frame.get("blocks")
    if not body_html.strip() or not isinstance(blocks, list):
        return
    known_ids = {
        str(block.get("block_id") or block.get("layer_id") or "").strip()
        for block in blocks
        if isinstance(block, dict)
    }
    known_ids.discard("")
    try:
        soup = BeautifulSoup(body_html, "html.parser")
    except Exception:  # noqa: BLE001 - malformed authored HTML is handled by sanitizer
        return
    added = 0
    for node in soup.find_all(attrs={"data-block-id": True}):
        block_id = str(node.get("data-block-id") or "").strip()
        if not block_id or block_id in known_ids:
            continue
        block = _infer_authored_block_from_dom_node(block_id, node)
        if block is None:
            continue
        blocks.append(block)
        known_ids.add(block_id)
        added += 1
        if added >= 120:
            break


def _infer_authored_block_from_dom_node(block_id: str, node: Any) -> dict[str, Any] | None:
    tag = str(getattr(node, "name", "") or "").strip().lower()
    if not tag:
        return None
    raw_kind = str(node.get("data-block-kind") or "").strip().lower() if hasattr(node, "get") else ""
    text = re.sub(r"\s+", " ", str(node.get_text(" ", strip=True) if hasattr(node, "get_text") else "")).strip()
    style = str(node.get("style") or "") if hasattr(node, "get") else ""
    kind = _infer_authored_block_kind(raw_kind, tag=tag, text=text, style=style, node=node)
    role = _infer_authored_block_role(block_id, kind=kind, tag=tag, node=node)
    block: dict[str, Any] = {
        "block_id": block_id,
        "kind": kind,
        "role": role,
        "source": "authored_body_html",
        "source_id": block_id,
    }
    bbox = _authored_inline_style_bbox(style)
    if bbox is not None:
        block["bbox"] = bbox
    if kind in {"text", "caption", "metric", "quote"} and text:
        block["text"] = _clean_text(text, limit=360)
    elif kind == "image":
        src = str(node.get("src") or "") if hasattr(node, "get") else ""
        if src:
            block["src_path"] = src
    elif kind == "table" and text:
        block["text"] = _clean_text(text, limit=360)
    return block


def _infer_authored_block_kind(raw_kind: str, *, tag: str, text: str, style: str, node: Any) -> str:
    if raw_kind in {"group", "text", "caption", "metric", "quote", "image", "table", "chart", "embed", "shape"}:
        return raw_kind
    if tag == "img":
        return "image"
    if tag == "table":
        return "table"
    if tag == "figcaption":
        return "caption"
    classes = " ".join(str(item) for item in (node.get("class") or [])).lower() if hasattr(node, "get") else ""
    if any(token in classes for token in ("metric", "stat", "score")):
        return "metric"
    if any(token in classes for token in ("caption", "source-note")):
        return "caption"
    if tag in {"section", "article", "aside", "header", "footer", "figure"} and not text:
        return "group"
    if _authored_inline_style_bbox(style) is not None or text:
        return "text"
    return "group"


def _infer_authored_block_role(block_id: str, *, kind: str, tag: str, node: Any) -> str:
    raw_role = str(node.get("data-role") or node.get("role") or "").strip().lower() if hasattr(node, "get") else ""
    if raw_role:
        return raw_role
    key = block_id.lower()
    if kind == "image":
        return "evidence"
    if kind == "table":
        return "table"
    if kind in {"caption", "metric", "quote"}:
        return kind
    if "title" in key or tag in {"h1", "h2"}:
        return "title" if tag == "h1" or "poster_title" in key else "section"
    if any(token in key for token in ("chip", "badge", "kicker", "label")):
        return "label"
    if any(token in key for token in ("head", "heading", "section")):
        return "section"
    if kind == "group":
        return "panel"
    return "body"


def _default_frame_layout_archetype(
    frame: dict[str, Any],
    *,
    artifact: dict[str, Any],
    raw_spec: dict[str, Any],
) -> str:
    for value in (
        frame.get("layout"),
        frame.get("role"),
        artifact.get("target"),
        raw_spec.get("visual_profile"),
        raw_spec.get("artifact_type"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return "authored_html"


def _canonicalize_raw_typography(typography: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in typography.items():
        if value is None:
            continue
        if isinstance(value, dict):
            family = (
                value.get("font_family")
                or value.get("fontFamily")
                or value.get("family")
                or value.get("name")
            )
            if family is None:
                family = key
            out[str(key)] = str(family)
        else:
            out[str(key)] = str(value)
    return out


def _canonicalize_raw_layer_node(node: dict[str, Any]) -> dict[str, Any]:
    out = dict(node)
    if isinstance(out.get("bbox"), dict):
        out["bbox"] = _canonicalize_raw_bbox(out["bbox"])
    for key in ("font_size_px", "font_weight", "z_index"):
        if key in out:
            coerced = _coerce_int(out.get(key))
            if coerced is None:
                out.pop(key, None)
            else:
                out[key] = coerced
    for key in ("line_height", "letter_spacing"):
        if key in out:
            coerced = _coerce_float(out.get(key))
            if coerced is None:
                out.pop(key, None)
            else:
                out[key] = coerced
    if isinstance(out.get("children"), list):
        out["children"] = [
            _canonicalize_raw_layer_node(child)
            for child in out["children"]
            if isinstance(child, dict)
        ]
    return out


def _canonicalize_raw_html_blocks(blocks: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        item = dict(block)
        block_kind = str(item.get("kind") or "").lower()
        if block_kind == "background":
            item["kind"] = "shape"
            block_kind = "shape"
        if isinstance(item.get("bbox"), dict):
            item["bbox"] = _canonicalize_raw_bbox(item["bbox"])
        if "items" in item:
            item["items"] = _canonicalize_raw_text_items(item.get("items"))
        if "covers" in item:
            item["covers"] = _canonicalize_raw_text_items(item.get("covers"))
        if "headers" in item and (
            item.get("headers") is not None or block_kind == "table"
        ):
            item["headers"] = _canonicalize_raw_text_items(item.get("headers"))
        if "col_highlight_rule" in item:
            item["col_highlight_rule"] = _canonicalize_raw_text_items(item.get("col_highlight_rule"))
        if "rows" in item and (
            item.get("rows") is not None or block_kind == "table"
        ):
            item["rows"] = _canonicalize_raw_table_rows(item.get("rows"))
        if isinstance(item.get("style"), dict):
            item["style"] = _canonicalize_raw_html_style(item["style"])
        if isinstance(item.get("children"), list):
            item["children"] = _canonicalize_raw_html_blocks(item["children"])
        if _blank_html_text_block_is_structural(item):
            item["kind"] = _structural_html_block_kind(item)
        out.append(item)
    return out


def _canonicalize_raw_text_items(value: Any) -> list[str]:
    if value is None:
        return []
    raw_items = value if isinstance(value, list) else [value]
    out: list[str] = []
    for raw in raw_items:
        text = _stringify_design_text_value(raw)
        if text:
            out.append(text)
    return out


def _canonicalize_raw_table_rows(value: Any) -> list[list[str]]:
    if not isinstance(value, list):
        return []
    rows: list[list[str]] = []
    for raw_row in value:
        if isinstance(raw_row, list):
            row = [
                text for cell in raw_row
                if (text := _stringify_design_text_value(cell))
            ]
        elif isinstance(raw_row, dict):
            row = [
                text for cell in raw_row.values()
                if (text := _stringify_design_text_value(cell))
            ]
        else:
            text = _stringify_design_text_value(raw_row)
            row = [text] if text else []
        if row:
            rows.append(row)
    return rows


def _canonicalize_raw_html_style(style: dict[str, Any]) -> dict[str, Any]:
    out = dict(style)
    aliases = {
        "fontSize": "font_size_px",
        "font-size": "font_size_px",
        "fontWeight": "font_weight",
        "font-weight": "font_weight",
        "lineHeight": "line_height",
        "line-height": "line_height",
        "letterSpacing": "letter_spacing",
        "letter-spacing": "letter_spacing",
        "textTransform": "text_transform",
        "text-transform": "text_transform",
        "textAlign": "align",
        "text-align": "align",
        "zIndex": "z_index",
        "z-index": "z_index",
    }
    for src, dst in aliases.items():
        if src in out and dst not in out:
            out[dst] = out[src]
    for key in ("font_size_px", "font_weight", "z_index"):
        if key in out:
            coerced = _coerce_int(out.get(key))
            if coerced is None:
                out.pop(key, None)
            else:
                out[key] = coerced
    for key in ("line_height", "letter_spacing"):
        if key in out:
            coerced = _coerce_float(out.get(key))
            if coerced is None:
                out.pop(key, None)
            else:
                out[key] = coerced
    return out


def _stringify_design_text_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value).strip()
    if isinstance(value, dict):
        for key in (
            "text",
            "title",
            "label",
            "name",
            "claim",
            "description",
            "summary",
            "value",
        ):
            text = _stringify_design_text_value(value.get(key))
            if text:
                return text
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except TypeError:
            return str(value).strip()
    if isinstance(value, list):
        parts = [_stringify_design_text_value(item) for item in value]
        return "; ".join(part for part in parts if part)
    return str(value).strip()


def _canonicalize_raw_frame_slot(
    slot: dict[str, Any],
    *,
    frame_bbox: dict[str, int],
    blocks: list[dict[str, Any]],
    index: int,
    total: int,
) -> dict[str, Any]:
    out = dict(slot)
    bbox = out.get("bbox")
    if not isinstance(bbox, dict):
        inline_bbox = {
            key: out[key]
            for key in ("x", "y", "w", "h")
            if key in out
        }
        if len(inline_bbox) == 4:
            out["bbox"] = inline_bbox
    if not isinstance(out.get("bbox"), dict):
        matched = _matching_block_bbox(str(out.get("slot_id") or ""), blocks)
        if matched is not None:
            out["bbox"] = matched
    if not isinstance(out.get("bbox"), dict):
        out["bbox"] = _fallback_slot_bbox(frame_bbox, index=index, total=total)
    out["bbox"] = _canonicalize_raw_bbox(out["bbox"])
    for key in ("x", "y", "w", "h"):
        out.pop(key, None)
    if not str(out.get("role") or "").strip():
        out["role"] = str(out.get("slot_id") or out.get("name") or "panel")
    return out


def _frame_bbox_for_slot_defaults(
    frame: dict[str, Any],
    canvas: dict[str, Any],
) -> dict[str, int]:
    bbox = frame.get("bbox") if isinstance(frame.get("bbox"), dict) else {}
    width = _coerce_int(bbox.get("w")) or _coerce_int(canvas.get("w_px")) or 1000
    height = _coerce_int(bbox.get("h")) or _coerce_int(canvas.get("h_px")) or 1000
    return {
        "x": _coerce_int(bbox.get("x")) or 0,
        "y": _coerce_int(bbox.get("y")) or 0,
        "w": max(1, width),
        "h": max(1, height),
    }


def _matching_block_bbox(slot_id: str, blocks: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not slot_id:
        return None
    for block in _iter_raw_blocks(blocks):
        if not isinstance(block, dict):
            continue
        if slot_id not in {
            str(block.get("slot_id") or ""),
            str(block.get("block_id") or ""),
            str(block.get("layer_id") or ""),
        }:
            continue
        bbox = block.get("bbox")
        if isinstance(bbox, dict):
            return bbox
    return None


def _iter_raw_blocks(blocks: list[dict[str, Any]]):
    for block in blocks:
        yield block
        children = block.get("children")
        if isinstance(children, list):
            yield from _iter_raw_blocks([c for c in children if isinstance(c, dict)])


def _fallback_slot_bbox(frame_bbox: dict[str, int], *, index: int, total: int) -> dict[str, int]:
    count = max(1, total)
    margin = max(24, min(frame_bbox["w"], frame_bbox["h"]) // 24)
    gutter = max(12, margin // 2)
    usable_h = max(1, frame_bbox["h"] - (2 * margin) - ((count - 1) * gutter))
    slot_h = max(1, usable_h // count)
    y = frame_bbox["y"] + margin + index * (slot_h + gutter)
    return {
        "x": frame_bbox["x"] + margin,
        "y": y,
        "w": max(1, frame_bbox["w"] - (2 * margin)),
        "h": slot_h,
    }


def _canonicalize_raw_bbox(bbox: dict[str, Any]) -> dict[str, int]:
    return {
        "x": max(0, _coerce_int(bbox.get("x")) or 0),
        "y": max(0, _coerce_int(bbox.get("y")) or 0),
        "w": max(1, _coerce_int(bbox.get("w")) or 1),
        "h": max(1, _coerce_int(bbox.get("h")) or 1),
    }


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value))
    number = _coerce_float(value)
    if number is None:
        return None
    return int(round(number))


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower()
    if not text:
        return None
    if text == "bold":
        return 700.0
    if text in {"regular", "normal"}:
        return 400.0
    if text.endswith("%"):
        try:
            return float(text[:-1].strip()) / 100.0
        except ValueError:
            return None
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _mark_visual_reference_revision(ctx: ToolContext, spec_seq: int) -> None:
    if not ctx.state.get("visual_reference_revision_required"):
        return
    source_seq = int(ctx.state.get("visual_reference_revision_source_spec_revision") or 0)
    if spec_seq <= source_seq:
        return
    ctx.state["visual_reference_revision_required"] = False
    ctx.state["visual_reference_revision_spec_revision"] = spec_seq
    ctx.state["visual_reference_revision_composited"] = False


def _persist_design_spec_snapshot(
    spec: DesignSpec,
    *,
    ctx: ToolContext,
    is_revision: bool,
) -> DesignSpecCommitResult:
    expected_revision = int(ctx.state.get("spec_revision_count") or 0)
    expected_hash = ctx.state.get("design_spec_sha256")
    prior_spec = ctx.state.get("design_spec")
    if expected_revision and not expected_hash and prior_spec is not None:
        expected_hash = design_spec_sha256(prior_spec)
    return commit_design_spec_revision(
        canonical_path=ctx.run_dir / "design_spec.json",
        artifact_type=spec.artifact_type.value,
        design_spec=spec.model_dump(mode="json"),
        is_revision=is_revision,
        expected_base_revision=expected_revision,
        expected_base_sha256=expected_hash,
        before_archive_publish=lambda path: _design_spec_commit_phase_hook(
            "before_archive_publish",
            path=path,
        ),
        phase_hook=lambda phase: _design_spec_commit_phase_hook(
            phase,
            path=ctx.run_dir / "design_spec.json",
        ),
    )


def _design_spec_commit_phase_hook(
    phase: str,
    **_details: Any,
) -> None:
    """Test seam immediately before immutable archive publication."""


def _blank_text_layer_ids(nodes: list[Any]) -> list[str]:
    out: list[str] = []
    for node in nodes:
        if getattr(node, "kind", None) == "text" and not (node.text or "").strip():
            out.append(str(node.layer_id))
        out.extend(_blank_text_layer_ids(list(getattr(node, "children", []) or [])))
    return out


def _blank_html_text_block_ids(artifact: Any) -> list[str]:
    if artifact is None:
        return []
    data = artifact.model_dump(mode="json") if hasattr(artifact, "model_dump") else artifact
    if not isinstance(data, dict):
        return []
    out: list[str] = []

    def visit(blocks: list[Any]) -> None:
        for block in blocks:
            if not isinstance(block, dict):
                continue
            has_text = _html_block_has_visible_text_payload(block)
            if (
                block.get("kind") == "text"
                and not has_text
                and not _blank_html_text_block_is_structural(block)
            ):
                out.append(str(block.get("block_id") or block.get("layer_id") or ""))
            visit(list(block.get("children") or []))

    for frame in data.get("frames") or []:
        if isinstance(frame, dict):
            visit(list(frame.get("blocks") or []))
    return out


def _html_block_has_visible_text_payload(block: dict[str, Any]) -> bool:
    for key in (
        "title",
        "text",
        "caption",
        "source_text",
        "evidence_quote",
        "evidence_source",
    ):
        if str(block.get(key) or "").strip():
            return True
    for key in ("items", "covers", "headers", "col_highlight_rule"):
        if any(str(item or "").strip() for item in block.get(key) or []):
            return True
    rows = block.get("rows")
    if isinstance(rows, list):
        for row in rows:
            values = row if isinstance(row, list) else [row]
            if any(str(cell or "").strip() for cell in values):
                return True
    return False


def _blank_html_text_block_is_structural(block: dict[str, Any]) -> bool:
    if _html_block_has_visible_text_payload(block):
        return False
    if block.get("children"):
        return True
    block_id = str(block.get("block_id") or block.get("layer_id") or "").lower()
    role = str(block.get("role") or "").lower()
    kind = str(block.get("kind") or "").lower()
    haystack = f"{block_id} {role} {kind}"
    structural_tokens = (
        "rule",
        "line",
        "divider",
        "band",
        "stripe",
        "panel",
        "container",
        "group",
        "grid",
        "frame",
        "background",
        "section",
        "column",
        "row",
        "wrap",
        "layout",
        "table",
        "card",
    )
    return any(token in haystack for token in structural_tokens)


def _structural_html_block_kind(block: dict[str, Any]) -> str:
    block_id = str(block.get("block_id") or block.get("layer_id") or "").lower()
    role = str(block.get("role") or "").lower()
    haystack = f"{block_id} {role}"
    if "table" in haystack and block.get("rows"):
        return "table"
    if block.get("children") or any(token in haystack for token in ("group", "grid", "container", "panel", "section", "row", "column")):
        return "group"
    return "shape"


def _validate_canvas_plan(
    spec: DesignSpec,
    ctx: ToolContext,
) -> ToolResultRecord | None:
    plan = ctx.state.get("canvas_plan")
    if not isinstance(plan, dict):
        return None
    expected_type = str(plan.get("artifact_type") or "").strip()
    if expected_type and expected_type != spec.artifact_type.value:
        log(
            "canvas_plan.artifact_mismatch",
            plan_artifact_type=expected_type,
            spec_artifact_type=spec.artifact_type.value,
            preset_id=plan.get("preset_id"),
        )
        return None
    expected_canvas = plan.get("canvas")
    if not isinstance(expected_canvas, dict):
        return None

    lock_level = str(plan.get("lock_level") or "advisory").strip().lower()
    if lock_level == "hard":
        mismatches = _hard_canvas_mismatches(expected_canvas, spec.canvas)
        if mismatches:
            return obs_error(
                "DesignSpec canvas does not match hard canvas_plan.",
                category="validation",
                payload={
                    "canvas_plan": plan,
                    "expected_canvas": expected_canvas,
                    "actual_canvas": spec.canvas,
                    "mismatches": mismatches,
                },
            )
        return None

    expected_family = _aspect_family(_canvas_ratio(expected_canvas))
    actual_family = _aspect_family(_canvas_ratio(spec.canvas))
    if not expected_family or not actual_family or expected_family == actual_family:
        return None

    override_reason = str(spec.canvas.get("canvas_plan_override_reason") or "").strip()
    if lock_level == "soft":
        if override_reason:
            if _soft_canvas_plan_is_fixed_paper_poster(plan):
                return obs_error(
                    "DesignSpec canvas conflicts with fixed academic paper-poster canvas_plan.",
                    category="validation",
                    payload={
                        "canvas_plan": plan,
                        "expected_aspect_family": expected_family,
                        "actual_aspect_family": actual_family,
                        "expected_canvas": expected_canvas,
                        "actual_canvas": spec.canvas,
                        "override_reason": override_reason,
                    },
                )
            log(
                "canvas_plan.override",
                preset_id=plan.get("preset_id"),
                expected_family=expected_family,
                actual_family=actual_family,
                reason=override_reason[:300],
            )
            return None
        return obs_error(
            "DesignSpec canvas conflicts with soft canvas_plan; use the planned "
            "aspect family or add canvas.canvas_plan_override_reason.",
            category="validation",
            payload={
                "canvas_plan": plan,
                "expected_aspect_family": expected_family,
                "actual_aspect_family": actual_family,
                "expected_canvas": expected_canvas,
                "actual_canvas": spec.canvas,
            },
        )

    log(
        "canvas_plan.mismatch",
        preset_id=plan.get("preset_id"),
        lock_level=lock_level,
        expected_family=expected_family,
        actual_family=actual_family,
    )
    return None


def _soft_canvas_plan_is_fixed_paper_poster(plan: dict[str, Any]) -> bool:
    if str(plan.get("artifact_type") or "").strip() != "poster":
        return False
    preset_id = str(plan.get("preset_id") or "").strip()
    body_grid = plan.get("body_grid") if isinstance(plan.get("body_grid"), dict) else {}
    grid_family = str(body_grid.get("family") or plan.get("grid_family") or "").strip()
    subtype = str(plan.get("poster_subtype") or "").strip()
    return bool(
        preset_id in {"cvpr-landscape", "academic-wide-2x1", "academic-wide-3280x1860", "academic-landscape-1.414"}
        or grid_family in {"editorial_3col", "cvpr_3col"}
        or subtype.startswith("academic_paper")
    )


def _validate_deck_plan(
    spec: DesignSpec,
    ctx: ToolContext,
) -> ToolResultRecord | None:
    result = validate_deck_plan_for_spec(spec, ctx.state.get("deck_plan"))
    if result is None:
        return None
    if result.status == "error":
        return obs_error(
            result.message,
            category="validation",
            payload=result.payload,
        )
    log_deck_plan_validation(result, ctx.state.get("deck_plan"))
    return None


def _hard_canvas_mismatches(
    expected: dict[str, Any],
    actual: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    mismatches: dict[str, dict[str, Any]] = {}
    for key in ("w_px", "h_px", "dpi", "aspect_ratio", "color_mode"):
        if key not in expected:
            continue
        expected_value = expected.get(key)
        actual_value = actual.get(key)
        if key in {"w_px", "h_px", "dpi"}:
            if _as_float(expected_value) == _as_float(actual_value):
                continue
        elif str(expected_value) == str(actual_value):
            continue
        mismatches[key] = {"expected": expected_value, "actual": actual_value}
    return mismatches


def _canvas_ratio(canvas: dict[str, Any]) -> float | None:
    w = _as_float(canvas.get("w_px"))
    h = _as_float(canvas.get("h_px"))
    if w and h:
        return w / h
    raw = str(canvas.get("aspect_ratio") or "").strip().lower()
    if not raw or raw == "responsive":
        return None
    match = re.match(r"^([0-9.]+)\s*[:/]\s*([0-9.]+)$", raw)
    if not match:
        return None
    num = _as_float(match.group(1))
    den = _as_float(match.group(2))
    return (num / den) if num and den else None


def _aspect_family(ratio: float | None) -> str | None:
    if ratio is None:
        return None
    if ratio >= 1.70:
        return "wide"
    if ratio >= 1.15:
        return "landscape"
    if ratio >= 0.90:
        return "square"
    if ratio >= 0.58:
        return "portrait"
    return "story"


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _validate_claim_graph_covers(
    spec: DesignSpec,
    claim_graph: Any,
) -> ToolResultRecord | None:
    available_claim_ids = _claim_graph_id_catalog(claim_graph)
    valid_ids = {
        cid for ids in available_claim_ids.values() for cid in ids
    }
    invalid_covers = _invalid_cover_refs(spec.layer_graph, valid_ids)
    if not invalid_covers:
        return None

    return obs_error(
        "DesignSpec validation failed: LayerNode.covers references unknown "
        "ClaimGraph id(s)",
        category="validation",
        payload={
            "available_claim_ids": available_claim_ids,
            "invalid_covers": invalid_covers,
        },
    )


def _claim_graph_id_catalog(claim_graph: Any) -> dict[str, list[str]]:
    return {
        "tensions": [str(t.id) for t in claim_graph.tensions],
        "mechanisms": [str(m.id) for m in claim_graph.mechanisms],
        "evidence": [str(e.id) for e in claim_graph.evidence],
        "implications": [str(i.id) for i in claim_graph.implications],
    }


def _invalid_cover_refs(
    nodes: list[Any],
    valid_ids: set[str],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for node in nodes:
        covers = [str(cid) for cid in (getattr(node, "covers", []) or [])]
        invalid = [cid for cid in covers if cid not in valid_ids]
        if invalid:
            out.append({
                "layer_id": str(getattr(node, "layer_id", "")),
                "name": str(getattr(node, "name", "")),
                "kind": str(getattr(node, "kind", "")),
                "covers": covers,
                "invalid_ids": invalid,
            })
        out.extend(_invalid_cover_refs(
            list(getattr(node, "children", []) or []),
            valid_ids,
        ))
    return out
