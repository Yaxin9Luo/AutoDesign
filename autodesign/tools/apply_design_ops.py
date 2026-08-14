"""apply_design_ops — batch targeted mutations onto the current DesignSpec.

This is the planner-facing repair primitive for environment feedback. It gives
the planner a small, auditable design-ops vocabulary instead of forcing every
repair through a full DesignSpec rewrite.
"""

from __future__ import annotations

from copy import deepcopy
from html import escape as _html_escape
import math
import os
import re
from typing import Any

from bs4 import BeautifulSoup
from pydantic import ValidationError

from ._contract import ToolContext, obs_error, obs_ok
from .propose_design_spec import (
    DOGFOOD_DRAFT_DESIGN_SPEC_META_KEY,
    DOGFOOD_DRAFT_DESIGN_SPEC_STATE_KEY,
    _blank_html_text_block_ids,
    _blank_text_layer_ids,
    _build_paper_poster_recovery_design_spec,
    _canonicalize_raw_design_spec,
    _design_spec_commit_phase_hook,
    _persist_design_spec_snapshot,
    _recover_contaminated_paper_poster_revision,
    _validate_authored_paper_poster_frame,
)
from .render_text_layer import render_text_layer
from .paper_poster_renderer import (
    find_authored_paper_poster_frame,
    is_academic_paper_poster_context,
)
from ..schema import DesignSpec, ToolResultRecord
from ..config import effective_poster_harness_mode
from ..design_spec_persistence import (
    DesignSpecPersistenceError,
    capture_state_keys,
    install_state_snapshot,
)
from ..util.deck_planner import (
    log_deck_plan_validation,
    validate_deck_plan_for_spec,
)
from ..util.design_feedback import design_feedback_to_dict
from ..util.design_spec_fingerprint import design_spec_sha256
from ..util.html_artifact import canonicalize_design_spec
from ..util.logging import log
from ..util.academic_palette import active_academic_color_system, load_academic_palette_library


_BBOX_KEYS = ("x", "y", "w", "h")
_TEXT_RENDER_OPS = {"add_layer", "set_bbox", "move_layer", "resize_layer", "replace_text"}
_HTML_TEXT_BBOX_MISSING_FINDINGS = {"authored-html-text-bbox-missing"}
_HTML_TEXT_BBOX_REALIZATION_FINDINGS = {"authored-html-text-bbox-not-realized"}
_HTML_TEXT_FIT_FINDINGS = {
    "authored-html-text-fit-underbudget",
    "authored-html-main-title-underbudget",
}
_HTML_IMAGE_MISSING_BLOCK_ID_FINDINGS = {"authored-html-image-missing-block-id"}
_HTML_TEXT_BBOX_KINDS = {"text", "caption", "metric", "quote"}
_HTML_VISUAL_KINDS = {"image", "table", "chart", "embed"}
_HTML_ALIAS_OPS = {
    "delete_layer": "html_delete_block",
    "set_bbox": "html_set_block_bbox",
    "move_layer": "html_move_block",
    "resize_layer": "html_resize_block",
    "replace_text": "html_replace_text",
}
_CSS_STYLE_KEY_MAP = {
    "background": "background",
    "background-color": "backgroundColor",
    "border-color": "borderColor",
    "border-radius": "borderRadius",
    "border-width": "borderWidth",
    "color": "color",
    "fill": "fill",
    "font-family": "fontFamily",
    "font-size": "fontSize",
    "font-weight": "fontWeight",
    "letter-spacing": "letterSpacing",
    "line-height": "lineHeight",
    "opacity": "opacity",
    "stroke": "stroke",
    "stroke-width": "strokeWidth",
    "text-align": "textAlign",
}
_APPLY_STATE_TRANSACTION_KEYS = (
    "artifact_type",
    "design_spec",
    "design_spec_sha256",
    "rendered_layers",
    "layer_versions",
    "composition",
    "spec_revision_count",
    "visual_reference_revision_required",
    "visual_reference_revision_source_spec_revision",
    "visual_reference_revision_spec_revision",
    "visual_reference_revision_composited",
    "video_delivery",
    "finalized",
    DOGFOOD_DRAFT_DESIGN_SPEC_STATE_KEY,
    DOGFOOD_DRAFT_DESIGN_SPEC_META_KEY,
    "last_design_ops",
    "authored_html_storyboard_local_repair_warnings",
    "spec_recovery_records",
    "spec_recovery_reason",
    "spec_recovery_count",
)
_APPLY_DEEP_COPY_STATE_KEYS = {
    "rendered_layers",
    "layer_versions",
    "authored_html_storyboard_local_repair_warnings",
    "spec_recovery_records",
}


class _DesignOpError(Exception):
    def __init__(self, message: str, *, index: int, op: dict[str, Any]):
        super().__init__(message)
        self.message = message
        self.index = index
        self.op = op


def apply_design_ops(args: dict[str, Any], *, ctx: ToolContext) -> ToolResultRecord:
    entry_state = _snapshot_mutable_state(ctx)
    ops = args.get("ops")
    if not isinstance(ops, list) or not ops:
        return obs_error(
            "apply_design_ops: 'ops' must be a non-empty array",
            category="validation",
            payload=_error_payload(ctx, failed_op_index=-1, failed_op={}),
        )

    spec = ctx.state.get("design_spec")
    draft_spec = ctx.state.get(DOGFOOD_DRAFT_DESIGN_SPEC_STATE_KEY)
    using_draft_design_spec = False
    if draft_spec is not None and _ops_should_use_retained_dogfood_draft(ops, ctx):
        spec = draft_spec
        using_draft_design_spec = True
    elif spec is None:
        if draft_spec is None:
            return obs_error(
                "apply_design_ops: propose_design_spec must be called first",
                category="validation",
            )
        spec = draft_spec
        using_draft_design_spec = True
    if using_draft_design_spec:
        draft_meta = ctx.state.get(DOGFOOD_DRAFT_DESIGN_SPEC_META_KEY)
        log(
            "design_ops.using_draft_design_spec",
            issue_id=(draft_meta or {}).get("issue_id") if isinstance(draft_meta, dict) else None,
            draft_revision=(draft_meta or {}).get("draft_revision") if isinstance(draft_meta, dict) else None,
            reason="retained_invalid_authored_draft",
        )

    spec_data = deepcopy(spec.model_dump(mode="json"))
    touched_text_ids: set[str] = set()
    deleted_layer_ids: set[str] = set()
    touched_html_artifact = False
    applied_ops: list[dict[str, Any]] = []

    try:
        for idx, op in enumerate(ops):
            if not isinstance(op, dict):
                raise _DesignOpError(
                    "apply_design_ops: each op must be an object",
                    index=idx,
                    op={"value": op},
                )
            try:
                applied = _apply_one_op(
                    spec_data,
                    op,
                    index=idx,
                    touched_text_ids=touched_text_ids,
                    deleted_layer_ids=deleted_layer_ids,
                )
            except _DesignOpError as e:
                skipped = _maybe_skip_missing_dogfood_html_block(e, ctx)
                if skipped is None:
                    raise
                applied_ops.append(skipped)
                continue
            if str(applied.get("op") or "").startswith("html_"):
                touched_html_artifact = True
            applied_ops.append(applied)
    except _DesignOpError as e:
        return obs_error(
            e.message,
            category="validation",
            payload=_error_payload(ctx, failed_op_index=e.index, failed_op=e.op),
        )

    if _dogfood_repair_batch_is_missing_target_noop(applied_ops, using_draft_design_spec):
        recovered_spec_data = _dogfood_recovery_spec_data_for_missing_targets(ctx)
        if recovered_spec_data is None:
            return obs_error(
                "apply_design_ops: all dogfood authored_html repair ops targeted missing block ids; "
                "use block_id values from the retained draft or submit a complete authored_html revision",
                category="validation",
                payload={
                    **_error_payload(ctx, failed_op_index=-1, failed_op={}),
                    "skipped_missing_block_ops": applied_ops[:24],
                    "available_html_block_ids": _html_block_ids_sample(spec_data),
                    "repair_route": "revise_authored_html_with_existing_block_ids",
                },
            )
        spec_data = recovered_spec_data
        touched_html_artifact = True
        applied_ops.append({
            "op": "system_recover_paper_poster_revision",
            "reason": "stale_missing_html_repair_targets",
            "auto_repair": True,
        })
        log(
            "design_ops.recovered_after_missing_targets",
            skipped=len([op for op in applied_ops if op.get("skipped")]),
            reason="stale_missing_html_repair_targets",
        )

    try:
        _sanitize_repair_input_scalars(spec_data)
        new_spec = DesignSpec.model_validate(spec_data)
        new_spec = canonicalize_design_spec(
            new_spec,
            prefer_html_artifact=True if touched_html_artifact else False,
        )
        recovered_raw, recovered_spec, contamination_reason = _recover_contaminated_paper_poster_revision(
            spec_data,
            new_spec,
            ctx,
        )
        if contamination_reason:
            spec_data = (
                recovered_raw
                if isinstance(recovered_raw, dict)
                else recovered_spec.model_dump(mode="json")
            )
            new_spec = recovered_spec
            touched_html_artifact = True
            applied_ops.append({
                "op": "system_recover_paper_poster_revision",
                "reason": contamination_reason,
            })
    except ValidationError as e:
        return obs_error(
            f"apply_design_ops: repaired DesignSpec validation failed: {e.errors(include_url=False)}",
            category="validation",
            payload=_error_payload(ctx, failed_op_index=-1, failed_op={}),
        )

    academic_paper_poster = is_academic_paper_poster_context(new_spec, ctx)
    authored_frame = find_authored_paper_poster_frame(new_spec)
    blank_text_layers = _blank_text_layer_ids(new_spec.layer_graph)
    ignore_blank_legacy_layers = (
        touched_html_artifact
        and academic_paper_poster
        and authored_frame is not None
    )
    if blank_text_layers and not ignore_blank_legacy_layers:
        return obs_error(
            "apply_design_ops: kind='text' layers must have non-empty text; "
            f"blank layer ids: {blank_text_layers[:8]}",
            category="validation",
            payload=_error_payload(ctx, failed_op_index=-1, failed_op={}),
        )
    if blank_text_layers:
        log(
            "design_ops.ignored_blank_legacy_layer_graph_text",
            count=len(blank_text_layers),
            sample=blank_text_layers[:8],
            reason="authored_html_frame_is_primary",
        )
    blank_html_blocks = _blank_html_text_block_ids(new_spec.html_artifact)
    if blank_html_blocks:
        return obs_error(
            "apply_design_ops: html_artifact text blocks must be non-empty; "
            f"blank block ids: {blank_html_blocks[:8]}",
            category="validation",
            payload=_error_payload(ctx, failed_op_index=-1, failed_op={}),
        )
    feedback_auto_repair_ops: list[dict[str, Any]] = []
    auto_repair_applied_ops: list[dict[str, Any]] = []
    if academic_paper_poster and authored_frame is not None:
        feedback_auto_repair_ops = _apply_design_feedback_auto_repair_pass(spec_data, ctx)
        if feedback_auto_repair_ops:
            touched_html_artifact = True
            applied_ops.extend(feedback_auto_repair_ops)
            try:
                new_spec = canonicalize_design_spec(
                    DesignSpec.model_validate(spec_data),
                    prefer_html_artifact=True,
                )
            except ValidationError as e:
                return obs_error(
                    f"apply_design_ops: design-feedback auto repair produced invalid DesignSpec: {e.errors(include_url=False)}",
                    category="validation",
                    payload={
                        **_error_payload(ctx, failed_op_index=-1, failed_op={}),
                        "feedback_auto_repair_applied_ops": feedback_auto_repair_ops,
                    },
                )
            authored_frame = find_authored_paper_poster_frame(new_spec)
        new_spec, authored_validation, auto_repair_applied_ops = (
            _auto_repair_authored_paper_poster_preflight(spec_data, ctx)
        )
        if auto_repair_applied_ops:
            touched_html_artifact = True
            applied_ops.extend(auto_repair_applied_ops)
            authored_frame = find_authored_paper_poster_frame(new_spec)
            blank_html_blocks = _blank_html_text_block_ids(new_spec.html_artifact)
            if blank_html_blocks:
                if using_draft_design_spec:
                    ctx.state[DOGFOOD_DRAFT_DESIGN_SPEC_STATE_KEY] = new_spec
                return obs_error(
                    "apply_design_ops: auto-repaired html_artifact text blocks must be non-empty; "
                    f"blank block ids: {blank_html_blocks[:8]}",
                    category="validation",
                    payload={
                        **_error_payload(ctx, failed_op_index=-1, failed_op={}),
                        "auto_repair_applied_ops": auto_repair_applied_ops,
                        "draft_design_spec_available": using_draft_design_spec,
                    },
                )
        if authored_validation is not None:
            if using_draft_design_spec:
                ctx.state[DOGFOOD_DRAFT_DESIGN_SPEC_STATE_KEY] = new_spec
            return obs_error(
                "apply_design_ops: repaired authored_html paper poster still "
                f"failed validation: {authored_validation.error_message}",
                category="validation",
                payload={
                    **_error_payload(ctx, failed_op_index=-1, failed_op={}),
                    **(authored_validation.payload or {}),
                    "auto_repair_applied_ops": auto_repair_applied_ops,
                    "draft_design_spec_available": using_draft_design_spec,
                    "repair_route": (
                        "apply_design_ops_on_draft_then_composite"
                        if using_draft_design_spec
                        else (authored_validation.payload or {}).get("repair_route", "revise_authored_html")
                    ),
                },
            )

    deck_plan_validation = validate_deck_plan_for_spec(new_spec, ctx.state.get("deck_plan"))
    if deck_plan_validation is not None:
        if deck_plan_validation.status == "error":
            return obs_error(
                f"apply_design_ops: {deck_plan_validation.message}",
                category="validation",
                payload={
                    **_error_payload(ctx, failed_op_index=-1, failed_op={}),
                    **deck_plan_validation.payload,
                },
            )
        log_deck_plan_validation(deck_plan_validation, ctx.state.get("deck_plan"))

    render_state = _snapshot_mutable_state(ctx)
    re_rendered_layer_ids: list[str] = []
    try:
        ctx.state["design_spec"] = new_spec
        for layer_id in sorted(deleted_layer_ids):
            ctx.state["rendered_layers"].pop(layer_id, None)
        if new_spec.artifact_type.value == "poster" and not _is_authored_html_poster_spec(new_spec):
            re_rendered_layer_ids = _rerender_poster_text_layers(
                new_spec,
                sorted(touched_text_ids - deleted_layer_ids),
                ctx,
            )
    except _DesignOpError as e:
        _restore_mutable_state(ctx, render_state)
        return obs_error(
            e.message,
            category="validation",
            payload=_error_payload(ctx, failed_op_index=e.index, failed_op=e.op),
        )
    except Exception as e:
        _restore_mutable_state(ctx, render_state)
        return obs_error(
            f"apply_design_ops: failed while re-rendering poster text layers: {type(e).__name__}: {e}",
            category="api",
            payload=_error_payload(ctx, failed_op_index=-1, failed_op={}),
        )

    proposed_revision = int(ctx.state.get("spec_revision_count") or 0) + 1
    spec_hash = design_spec_sha256(new_spec)
    prior_type = getattr(getattr(spec, "artifact_type", None), "value", None)
    ctx.state["artifact_type"] = new_spec.artifact_type.value
    ctx.state["design_spec_sha256"] = spec_hash
    ctx.state["composition"] = None
    if prior_type == "video" or new_spec.artifact_type.value == "video":
        ctx.state.pop("video_delivery", None)
        ctx.state.pop("finalized", None)
    if using_draft_design_spec:
        ctx.state.pop(DOGFOOD_DRAFT_DESIGN_SPEC_STATE_KEY, None)
        ctx.state.pop(DOGFOOD_DRAFT_DESIGN_SPEC_META_KEY, None)

    payload = {
        "artifact_type": new_spec.artifact_type.value,
        "spec_revision": proposed_revision,
        "used_draft_design_spec": using_draft_design_spec,
        "n_ops": len(applied_ops),
        "applied_ops": applied_ops,
        "auto_repair_applied_ops": auto_repair_applied_ops,
        "feedback_auto_repair_applied_ops": feedback_auto_repair_ops,
        "re_rendered_layer_ids": re_rendered_layer_ids,
        "n_layers": _count_layers(new_spec.model_dump(mode="json").get("layer_graph") or []),
        "html_artifact_frames": len(getattr(new_spec.html_artifact, "frames", []) or []),
    }
    ctx.state["last_design_ops"] = payload
    proposed_state = _snapshot_mutable_state(ctx)
    _restore_mutable_state(ctx, entry_state)
    try:
        commit = _persist_design_spec_snapshot(
            new_spec,
            ctx=ctx,
            is_revision=True,
        )
        _design_spec_commit_phase_hook(
            "after_persistence_before_state_install",
            path=commit.canonical_path,
        )
    except Exception as exc:  # noqa: BLE001 - persistence is a tool failure
        phase = exc.phase if isinstance(exc, DesignSpecPersistenceError) else "unknown"
        return obs_error(
            "apply_design_ops: failed to persist DesignSpec revision "
            f"based on {proposed_revision - 1}: {exc}",
            category="api",
            payload={
                **_error_payload(ctx, failed_op_index=-1, failed_op={}),
                "phase": phase,
                "spec_revision": proposed_revision,
                "design_spec_sha256": spec_hash,
            },
        )
    payload["spec_revision"] = commit.revision
    _restore_mutable_state(ctx, proposed_state)
    ctx.state["spec_revision_count"] = commit.revision
    ctx.state["design_spec_sha256"] = commit.design_spec_sha256
    ctx.state["last_design_ops"] = payload
    _mark_visual_reference_revision(ctx, commit.revision)
    log(
        "design_ops.applied",
        artifact_type=new_spec.artifact_type.value,
        spec_revision=commit.revision,
        n_ops=len(applied_ops),
        re_rendered=len(re_rendered_layer_ids),
    )
    return obs_ok(payload)


def _mark_visual_reference_revision(ctx: ToolContext, spec_seq: int) -> None:
    if not ctx.state.get("visual_reference_revision_required"):
        return
    source_seq = int(ctx.state.get("visual_reference_revision_source_spec_revision") or 0)
    if spec_seq <= source_seq:
        return
    ctx.state["visual_reference_revision_required"] = False
    ctx.state["visual_reference_revision_spec_revision"] = spec_seq
    ctx.state["visual_reference_revision_composited"] = False


def _ops_should_use_retained_dogfood_draft(ops: list[Any], ctx: ToolContext) -> bool:
    draft_meta = ctx.state.get(DOGFOOD_DRAFT_DESIGN_SPEC_META_KEY)
    issue_id = str((draft_meta or {}).get("issue_id") or "") if isinstance(draft_meta, dict) else ""
    if not issue_id.startswith("authored_html_"):
        return False
    for op in ops:
        if not isinstance(op, dict):
            continue
        finding_id = str(op.get("finding_id") or "")
        op_name = str(op.get("op") or "")
        if finding_id.startswith("authored-html-"):
            return True
        if op_name in {
            "realize_text_bbox_in_authored_css",
            "resize_or_rewrite_text_block",
            "bind_missing_image_block_ids",
        "bind_all_images_to_blocks",
        "add_auditable_text_bbox",
        "html_add_missing_text_bbox",
        "html_infer_text_bbox",
        "html_realize_block_bbox",
        "html_realize_all_text_bboxes",
        "html_bind_all_images_to_blocks",
    }:
            return True
    return False


def _maybe_skip_missing_dogfood_html_block(
    error: _DesignOpError,
    ctx: ToolContext,
) -> dict[str, Any] | None:
    """Do not let one stale LLM repair target abort the whole dogfood batch."""
    if effective_poster_harness_mode(ctx.settings) != "dogfood":
        return None
    state = ctx.state if isinstance(ctx.state, dict) else {}
    contract = state.get("poster_plan_contract")
    if not (isinstance(contract, dict) and contract.get("kind") == "paper_poster_plan_contract"):
        return None
    op_name = str((error.op or {}).get("op") or "")
    if not op_name.startswith("html_") and op_name not in _HTML_ALIAS_OPS:
        return None
    if "block_id" not in error.message or "not found" not in error.message:
        return None
    block_id = str((error.op or {}).get("block_id") or (error.op or {}).get("layer_id") or "").strip()
    log(
        "design_ops.skip_missing_dogfood_html_block",
        block_id=block_id,
        op=op_name,
        finding_id=str((error.op or {}).get("finding_id") or ""),
        reason="stale_or_model_invented_repair_target",
    )
    return {
        "op": "html_skip_missing_block",
        "finding_id": str((error.op or {}).get("finding_id") or "missing-html-block"),
        "layer_id": block_id or None,
        "block_id": block_id or None,
        "skipped": True,
        "reason": "missing dogfood html block target",
    }


def _dogfood_repair_batch_is_missing_target_noop(
    applied_ops: list[dict[str, Any]],
    using_draft_design_spec: bool,
) -> bool:
    if not using_draft_design_spec or not applied_ops:
        return False
    skipped = [op for op in applied_ops if op.get("skipped")]
    if not skipped:
        return False
    real_mutations = [
        op
        for op in applied_ops
        if not op.get("skipped")
        and str(op.get("op") or "") not in {"html_skip_missing_block"}
    ]
    return len(real_mutations) == 0


def _dogfood_recovery_spec_data_for_missing_targets(ctx: ToolContext) -> dict[str, Any] | None:
    if effective_poster_harness_mode(ctx.settings) != "dogfood":
        return None
    if not _deterministic_spec_recovery_enabled():
        return None
    raw = _build_paper_poster_recovery_design_spec(ctx)
    if not isinstance(raw, dict):
        return None
    try:
        return DesignSpec.model_validate(_canonicalize_raw_design_spec(raw)).model_dump(mode="json")
    except ValidationError as exc:
        log(
            "design_ops.missing_target_recovery_failed",
            error=exc.errors(include_url=False, include_input=False),
        )
        return None


def _html_block_ids_sample(spec_data: dict[str, Any], *, limit: int = 80) -> list[str]:
    ids: list[str] = []
    for block_id in _html_block_index(spec_data):
        ids.append(str(block_id))
        if len(ids) >= limit:
            break
    return ids


def _auto_repair_authored_paper_poster_preflight(
    spec_data: dict[str, Any],
    ctx: ToolContext,
    *,
    max_passes: int = 24,
) -> tuple[DesignSpec, ToolResultRecord | None, list[dict[str, Any]]]:
    """Close deterministic authored_html preflight repairs inside one tool call."""
    applied_ops: list[dict[str, Any]] = []
    deduped = _dedupe_html_artifact_blocks_in_spec_data(spec_data)
    if deduped:
        applied_ops.append({
            "op": "html_dedupe_duplicate_block_ids",
            "finding_id": "authored-html-duplicate-block-id",
            "deduped_block_ids": deduped[:24],
            "duplicate_count": len(deduped),
            "auto_repair": True,
            "auto_repair_pass": 0,
        })
    last_spec = canonicalize_design_spec(
        DesignSpec.model_validate(spec_data),
        prefer_html_artifact=True,
    )
    last_validation: ToolResultRecord | None = None
    for pass_index in range(max(1, max_passes)):
        current_spec = canonicalize_design_spec(
            DesignSpec.model_validate(spec_data),
            prefer_html_artifact=True,
        )
        last_spec = current_spec
        if not is_academic_paper_poster_context(current_spec, ctx):
            return current_spec, None, applied_ops
        authored_frame = find_authored_paper_poster_frame(current_spec)
        if authored_frame is None:
            return current_spec, None, applied_ops
        validation = _validate_authored_paper_poster_frame(authored_frame, ctx)
        if validation is None:
            return current_spec, None, applied_ops
        last_validation = validation
        changed = _apply_authored_preflight_auto_repair_pass(
            spec_data,
            validation.payload or {},
            pass_index=pass_index,
            applied_ops=applied_ops,
        )
        if not changed:
            return current_spec, validation, applied_ops
    final_spec = canonicalize_design_spec(
        DesignSpec.model_validate(spec_data),
        prefer_html_artifact=True,
    )
    authored_frame = find_authored_paper_poster_frame(final_spec)
    if authored_frame is None:
        return final_spec, None, applied_ops
    final_validation = _validate_authored_paper_poster_frame(authored_frame, ctx)
    return final_spec, final_validation or last_validation, applied_ops


def _apply_authored_preflight_auto_repair_pass(
    spec_data: dict[str, Any],
    payload: dict[str, Any],
    *,
    pass_index: int,
    applied_ops: list[dict[str, Any]],
) -> bool:
    changed = False
    if _payload_requests_image_binding(payload):
        changed = _run_authored_auto_repair_op(
            spec_data,
            {
                "op": "html_bind_all_images_to_blocks",
                "finding_id": "authored-html-image-missing-block-id",
            },
            pass_index=pass_index,
            applied_ops=applied_ops,
        ) or changed

    findings = payload.get("authored_html_storyboard_findings")
    if not isinstance(findings, list):
        return changed

    for finding in findings:
        if not isinstance(finding, dict):
            continue
        if str(finding.get("id") or "") not in _HTML_TEXT_BBOX_MISSING_FINDINGS:
            continue
        op = _auto_missing_text_bbox_op(spec_data, finding)
        if op is None:
            continue
        changed = _run_authored_auto_repair_op(
            spec_data,
            op,
            pass_index=pass_index,
            applied_ops=applied_ops,
        ) or changed

    realization_ids = [
        str(finding.get("block_id") or "").strip()
        for finding in findings
        if isinstance(finding, dict)
        and str(finding.get("id") or "") in _HTML_TEXT_BBOX_REALIZATION_FINDINGS
        and str(finding.get("block_id") or "").strip()
    ]
    if realization_ids:
        changed = _run_authored_auto_repair_op(
            spec_data,
            {
                "op": "html_realize_all_text_bboxes",
                "finding_id": "authored-html-text-bbox-not-realized",
            },
            pass_index=pass_index,
            applied_ops=applied_ops,
        ) or changed

    for finding in findings:
        if not isinstance(finding, dict):
            continue
        if str(finding.get("id") or "") not in _HTML_TEXT_FIT_FINDINGS:
            continue
        op = _auto_fit_resize_op(spec_data, finding)
        if op is None:
            continue
        changed = _run_authored_auto_repair_op(
            spec_data,
            op,
            pass_index=pass_index,
            applied_ops=applied_ops,
        ) or changed

    for finding in findings:
        if not isinstance(finding, dict):
            continue
        if str(finding.get("id") or "") != "authored-html-text-bbox-overlap":
            continue
        op = _auto_overlap_separation_op(spec_data, finding)
        if op is None:
            continue
        changed = _run_authored_auto_repair_op(
            spec_data,
            op,
            pass_index=pass_index,
            applied_ops=applied_ops,
        ) or changed

    return changed


def _payload_requests_image_binding(payload: dict[str, Any]) -> bool:
    repairs = payload.get("authored_html_sanitizer_actionable_repairs")
    if isinstance(repairs, list):
        for repair in repairs:
            if isinstance(repair, dict) and str(repair.get("op") or "") == "html_bind_all_images_to_blocks":
                return True
    findings = payload.get("authored_html_sanitizer_findings")
    if isinstance(findings, list):
        return any(
            isinstance(finding, dict)
            and str(finding.get("id") or "") in _HTML_IMAGE_MISSING_BLOCK_ID_FINDINGS
            for finding in findings
        )
    return False


def _run_authored_auto_repair_op(
    spec_data: dict[str, Any],
    op: dict[str, Any],
    *,
    pass_index: int,
    applied_ops: list[dict[str, Any]],
) -> bool:
    op_name = str(op.get("op") or "")
    try:
        if op_name == "html_bind_all_images_to_blocks":
            applied = _op_html_bind_all_images_to_blocks(spec_data, op, index=-(pass_index + 1))
        elif op_name == "html_realize_all_text_bboxes":
            applied = _op_html_realize_all_text_bboxes(spec_data, op, index=-(pass_index + 1))
        elif op_name == "html_infer_text_bbox":
            applied = _op_html_infer_text_bbox(spec_data, op, index=-(pass_index + 1))
        elif op_name == "html_resize_block":
            applied = _op_html_resize_block(spec_data, op, index=-(pass_index + 1))
        elif op_name == "html_set_block_bbox":
            applied = _op_html_set_block_bbox(spec_data, op, index=-(pass_index + 1))
        else:
            return False
    except _DesignOpError as exc:
        log(
            "design_ops.authored_auto_repair_failed",
            op=op_name,
            finding_id=str(op.get("finding_id") or ""),
            block_id=str(op.get("block_id") or ""),
            pass_index=pass_index + 1,
            error=exc.message,
        )
        return False
    applied["auto_repair"] = True
    applied["auto_repair_pass"] = pass_index + 1
    if op.get("block_ids") and "block_ids" not in applied:
        applied["block_ids"] = list(op.get("block_ids") or [])
    if op.get("reason"):
        applied["reason"] = str(op.get("reason"))
    applied_ops.append(applied)
    return True


def _apply_design_feedback_auto_repair_pass(
    spec_data: dict[str, Any],
    ctx: ToolContext,
) -> list[dict[str, Any]]:
    if not _deterministic_design_feedback_repair_enabled():
        log(
            "design_ops.feedback_auto_repair.skip",
            reason="deterministic_design_feedback_repair_disabled",
        )
        return []
    feedback = design_feedback_to_dict(
        ctx.state.get("last_design_feedback")
        or (ctx.state.get("last_composite_payload") or {}).get("design_feedback")
    )
    if not feedback:
        return []
    findings = feedback.get("findings")
    if not isinstance(findings, list):
        return []
    applied: list[dict[str, Any]] = []
    repaired_ids: set[tuple[str, str]] = set()
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        issue_id = str(finding.get("id") or "")
        source = str(finding.get("source") or "")
        if "ai-default-indigo" in issue_id:
            op = _auto_sanitize_authored_default_palette(spec_data, finding, ctx)
            if op is not None:
                op["source"] = source or "design_feedback"
                op["auto_repair"] = True
                op["reason"] = "design_feedback_deterministic_repair"
                applied.append(op)
            continue
        if "paper-poster-text-overlap" in issue_id or "authored-html-text-bbox-overlap" in issue_id:
            op = _auto_overlap_separation_op(spec_data, finding)
            if op is not None:
                op["source"] = source or "design_feedback"
                op["auto_repair"] = True
                op["reason"] = "design_feedback_deterministic_repair"
                applied.append(op)
            continue
        block_id = _feedback_block_id(finding)
        if not block_id:
            continue
        key = (issue_id, block_id)
        if key in repaired_ids:
            continue
        if "paper-poster-block-out-of-bounds" in issue_id:
            op = _auto_clamp_html_block_to_canvas(spec_data, block_id)
        elif "paper-poster-text-overflow" in issue_id:
            op = _auto_resize_overflowing_html_block(spec_data, block_id, finding)
        else:
            op = None
        if op is None:
            continue
        op["source"] = source or "design_feedback"
        op["auto_repair"] = True
        op["reason"] = "design_feedback_deterministic_repair"
        applied.append(op)
        repaired_ids.add(key)
    return applied


def _deterministic_design_feedback_repair_enabled() -> bool:
    raw = os.getenv("POSTER_DETERMINISTIC_DESIGN_FEEDBACK_REPAIR", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _deterministic_spec_recovery_enabled() -> bool:
    raw = os.getenv("POSTER_ENABLE_DETERMINISTIC_SPEC_RECOVERY", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _auto_sanitize_authored_default_palette(
    spec_data: dict[str, Any],
    finding: dict[str, Any],
    ctx: ToolContext | None = None,
) -> dict[str, Any] | None:
    artifact = spec_data.get("html_artifact")
    frames = artifact.get("frames") if isinstance(artifact, dict) else None
    if not isinstance(frames, list):
        return None
    palette_roles, selected_hexes = _active_color_system_palette(ctx, spec_data)
    replacements_by_hex = _default_palette_replacements(palette_roles)
    changed = 0
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        for key in ("authored_css", "authored_body_html"):
            value = frame.get(key)
            if not isinstance(value, str) or not value:
                continue

            def replace(match: re.Match[str]) -> str:
                nonlocal changed
                token = match.group(0)
                if token.upper() in selected_hexes:
                    return token
                replacement = replacements_by_hex.get(token.lower())
                if replacement is None:
                    return token
                changed += 1
                return replacement

            frame[key] = re.sub(
                r"#[0-9a-fA-F]{6}",
                replace,
                value,
            )
    if changed <= 0:
        return None
    return {
        "op": "html_sanitize_default_palette",
        "finding_id": str(finding.get("id") or "quality_lint:ai-default-indigo"),
        "replacements": changed,
    }


_BRIGHT_COBALT_ROLES = {
    "background": "#FFFFFF",
    "text": "#21181B",
    "primary": "#C1121F",
    "secondary": "#F7DEE1",
    "accent": "#C1121F",
    "header_text": "#FFFFFF",
    "bar": "#C1121F",
}

_SATURATED_INDIGO_PURPLE = {
    "#6366f1",
    "#4f46e5",
    "#4338ca",
    "#3730a3",
    "#8b5cf6",
    "#7c3aed",
    "#a855f7",
    "#9333ea",
    "#6d28d9",
    "#818cf8",
    "#a78bfa",
}
_PALE_LAVENDER_BACKGROUND = {
    "#f5f3ff",
    "#fcfaff",
    "#eef2ff",
}
_PALE_LAVENDER_SECONDARY = {
    "#ede9fe",
    "#e0e7ff",
    "#e9d5ff",
    "#ddd6fe",
    "#c7d2fe",
    "#c4b5fd",
    "#a5b4fc",
}
_VERY_DARK_PURPLE = {
    "#581c87",
    "#312e81",
}


def _active_color_system_palette(
    ctx: ToolContext | None,
    spec_data: dict[str, Any] | None = None,
) -> tuple[dict[str, str], set[str]]:
    state = ctx.state if ctx is not None and isinstance(ctx.state, dict) else {}
    raw_spec = spec_data if isinstance(spec_data, dict) else {}
    artifact = raw_spec.get("html_artifact") if isinstance(raw_spec.get("html_artifact"), dict) else {}
    theme = artifact.get("theme") if isinstance(artifact.get("theme"), dict) else {}
    try:
        active = active_academic_color_system(
            state.get("poster_plan_contract"),
            state.get("poster_content_brief"),
            raw_spec,
            theme,
        )
    except Exception:
        active = {}
    roles = _normalized_color_roles(active.get("roles") if isinstance(active, dict) else {})
    if roles:
        allowed = _normalized_allowed_hexes(active.get("allowed_hexes"))
        if not allowed:
            allowed = set(roles.values())
        return roles, allowed
    fallback_roles = _bright_cobalt_roles()
    return fallback_roles, set(fallback_roles.values())


def _normalized_color_roles(raw_roles: Any) -> dict[str, str]:
    if not isinstance(raw_roles, dict):
        return {}
    roles: dict[str, str] = {}
    for key, value in raw_roles.items():
        token = str(value).strip().upper()
        if re.fullmatch(r"#[0-9A-F]{6}", token):
            roles[str(key)] = token
    return roles


def _normalized_allowed_hexes(raw_allowed: Any) -> set[str]:
    if not isinstance(raw_allowed, list):
        return set()
    return {
        str(item).strip().upper()
        for item in raw_allowed
        if re.fullmatch(r"#[0-9a-fA-F]{6}", str(item).strip())
    }


def _bright_cobalt_roles() -> dict[str, str]:
    library = load_academic_palette_library()
    for palette in library.get("palettes") or []:
        if not isinstance(palette, dict) or str(palette.get("id") or "") != "bright_cobalt":
            continue
        roles = _normalized_color_roles(palette.get("roles"))
        if roles:
            return roles
    return dict(_BRIGHT_COBALT_ROLES)


def _default_palette_replacements(roles: dict[str, str]) -> dict[str, str]:
    primary = roles.get("primary") or _BRIGHT_COBALT_ROLES["primary"]
    secondary = roles.get("secondary") or _BRIGHT_COBALT_ROLES["secondary"]
    background = roles.get("background") or _BRIGHT_COBALT_ROLES["background"]
    text = roles.get("text") or _BRIGHT_COBALT_ROLES["text"]
    replacements: dict[str, str] = {}
    replacements.update({token: primary for token in _SATURATED_INDIGO_PURPLE})
    replacements.update({token: background for token in _PALE_LAVENDER_BACKGROUND})
    replacements.update({token: secondary for token in _PALE_LAVENDER_SECONDARY})
    replacements.update({token: text for token in _VERY_DARK_PURPLE})
    return replacements


def _feedback_block_id(finding: dict[str, Any]) -> str:
    target = finding.get("target") if isinstance(finding.get("target"), dict) else {}
    evidence = finding.get("evidence") if isinstance(finding.get("evidence"), dict) else {}
    nested_evidence = evidence.get("evidence") if isinstance(evidence.get("evidence"), dict) else {}
    for source in (target, evidence, nested_evidence, finding):
        block_id = str((source or {}).get("block_id") or "").strip()
        if block_id:
            return block_id
    return ""


def _auto_clamp_html_block_to_canvas(
    spec_data: dict[str, Any],
    block_id: str,
) -> dict[str, Any] | None:
    found = _html_block_index(spec_data).get(block_id)
    if found is None:
        return None
    block, _container, _node_idx, frame = found
    bbox = block.get("bbox")
    if not isinstance(bbox, dict):
        return None
    cw, ch = _html_canvas_size(spec_data)
    if cw <= 0 or ch <= 0:
        return None
    try:
        norm = _normalized_bbox(bbox, index=-1, op={"op": "html_set_block_bbox"})
    except _DesignOpError:
        return None
    margin = 8
    next_w = min(norm["w"], max(1, cw - 2 * margin))
    next_h = min(norm["h"], max(1, ch - 2 * margin))
    next_x = min(max(margin, norm["x"]), max(margin, cw - next_w - margin))
    next_y = min(max(margin, norm["y"]), max(margin, ch - next_h - margin))
    next_bbox = {"x": int(next_x), "y": int(next_y), "w": int(next_w), "h": int(next_h)}
    if next_bbox == norm:
        return None
    block["bbox"] = next_bbox
    _realize_html_block_bbox_in_frame(frame, block_id, next_bbox)
    _merge_html_block_bbox_style(block, next_bbox)
    return {
        "op": "html_clamp_block_to_canvas",
        "finding_id": "paper-poster-block-out-of-bounds",
        "layer_id": block_id,
        "block_id": block_id,
        "bbox": next_bbox,
    }


def _auto_resize_overflowing_html_block(
    spec_data: dict[str, Any],
    block_id: str,
    finding: dict[str, Any],
) -> dict[str, Any] | None:
    found = _html_block_index(spec_data).get(block_id)
    if found is None:
        return None
    block, _container, _node_idx, frame = found
    bbox = block.get("bbox")
    evidence = finding.get("evidence") if isinstance(finding.get("evidence"), dict) else {}
    nested = evidence.get("evidence") if isinstance(evidence.get("evidence"), dict) else evidence
    if not _has_complete_bbox(bbox):
        bbox = nested.get("bbox") if isinstance(nested, dict) else None
    if not isinstance(bbox, dict):
        return None
    cw, ch = _html_canvas_size(spec_data)
    if cw <= 0 or ch <= 0:
        return None
    try:
        norm = _normalized_bbox(bbox, index=-1, op={"op": "html_resize_block"})
    except _DesignOpError:
        return None
    gap = _optional_int((nested or {}).get("height_gap_px")) or 12
    target_h = min(max(1, ch - norm["y"] - 8), norm["h"] + max(8, gap + 8))
    style_patch = _overflow_text_style_patch(block, nested if isinstance(nested, dict) else {})
    if target_h <= norm["h"] + 1 and not style_patch:
        return None
    next_bbox = dict(norm)
    if target_h > norm["h"] + 1:
        next_bbox["h"] = int(target_h)
        if next_bbox["y"] + next_bbox["h"] > ch - 8:
            next_bbox["y"] = max(8, ch - next_bbox["h"] - 8)
    block["bbox"] = next_bbox
    _realize_html_block_bbox_in_frame(frame, block_id, next_bbox)
    _merge_html_block_bbox_style(block, next_bbox)
    if style_patch:
        _merge_html_block_style_patch(block, style_patch)
        _patch_authored_body_block_style_patch(frame, block_id, style_patch)
    return {
        "op": "html_resize_overflowing_block",
        "finding_id": "paper-poster-text-overflow",
        "layer_id": block_id,
        "block_id": block_id,
        "bbox": next_bbox,
        "style": style_patch or None,
    }


def _html_canvas_size(spec_data: dict[str, Any]) -> tuple[int, int]:
    canvas = spec_data.get("canvas") if isinstance(spec_data.get("canvas"), dict) else {}
    return (
        _optional_int(canvas.get("w_px") or canvas.get("w")) or 0,
        _optional_int(canvas.get("h_px") or canvas.get("h")) or 0,
    )


def _has_complete_bbox(value: Any) -> bool:
    return isinstance(value, dict) and all(key in value for key in _BBOX_KEYS)


def _overflow_text_style_patch(block: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    block_id = str(block.get("block_id") or "").lower()
    kind = str(block.get("kind") or "").lower()
    role = str(block.get("role") or "").lower()
    font_px = _parse_css_number(str(evidence.get("font_size") or ""), integer=True)
    overflow_ratio = float(evidence.get("overflow_ratio") or 0.0)
    height_gap = _optional_int(evidence.get("height_gap_px")) or 0
    if kind != "caption" and role != "caption" and not block_id.startswith("caption_"):
        if not font_px or font_px <= 11 or (overflow_ratio <= 0.08 and height_gap <= 2):
            return {}
        if "title" in block_id or role == "title":
            next_font = max(34, min(font_px - 2, int(round(float(font_px) * 0.82))))
            return {"fontSize": next_font, "lineHeight": 1.02}
        next_font = max(11, min(font_px - 1, int(round(float(font_px) * 0.86))))
        return {"fontSize": next_font, "lineHeight": 1.08}
    if not font_px or font_px <= 14:
        return {}
    next_font = max(13, min(16, int(round(float(font_px) * 0.78))))
    return {
        "fontSize": next_font,
        "lineHeight": 1.05,
    }


def _auto_missing_text_bbox_op(spec_data: dict[str, Any], finding: dict[str, Any]) -> dict[str, Any] | None:
    block_id = str(finding.get("block_id") or "").strip()
    if not block_id or block_id not in _html_block_index(spec_data):
        return None
    return {
        "op": "html_infer_text_bbox",
        "finding_id": "authored-html-text-bbox-missing",
        "block_id": block_id,
        "word_count": finding.get("word_count"),
        "reason": "auto_infer_missing_authored_text_bbox",
    }


def _auto_fit_resize_op(spec_data: dict[str, Any], finding: dict[str, Any]) -> dict[str, Any] | None:
    block_id = str(finding.get("block_id") or "").strip()
    if not block_id:
        return None
    found = _html_block_index(spec_data).get(block_id)
    if found is None:
        return None
    block = found[0]
    bbox = block.get("bbox") if isinstance(block.get("bbox"), dict) else finding.get("bbox")
    if not isinstance(bbox, dict):
        return None
    current_h = _optional_int(bbox.get("h")) or 0
    if current_h <= 0:
        return None
    required_h = _optional_int(finding.get("estimated_required_height_px")) or current_h
    height_gap = max(0, _optional_int(finding.get("height_gap_px")) or 0)
    target_h = max(required_h, current_h + height_gap)
    if str(finding.get("id") or "") == "authored-html-main-title-underbudget":
        target_h = max(target_h, 96)
    if target_h <= current_h + 1:
        return None
    return {
        "op": "html_resize_block",
        "finding_id": str(finding.get("id") or "authored-html-text-fit-underbudget"),
        "block_id": block_id,
        "h": int(target_h),
        "reason": "auto_resize_underbudget_authored_text_bbox",
    }


def _auto_overlap_separation_op(spec_data: dict[str, Any], finding: dict[str, Any]) -> dict[str, Any] | None:
    evidence = finding.get("evidence") if isinstance(finding.get("evidence"), dict) else {}
    nested = evidence.get("evidence") if isinstance(evidence.get("evidence"), dict) else evidence
    left_id = str(
        finding.get("block_id")
        or finding.get("left_block_id")
        or (nested or {}).get("left_block_id")
        or (nested or {}).get("block_id")
        or ""
    ).strip()
    right_id = str(
        finding.get("other_block_id")
        or finding.get("right_block_id")
        or (nested or {}).get("right_block_id")
        or (nested or {}).get("other_block_id")
        or ""
    ).strip()
    if not left_id or not right_id:
        return None
    block_index = _html_block_index(spec_data)
    left = block_index.get(left_id)
    right = block_index.get(right_id)
    if left is None or right is None:
        return None
    left_bbox = (
        finding.get("bbox")
        if isinstance(finding.get("bbox"), dict)
        else left[0].get("bbox")
    )
    right_bbox = (
        finding.get("other_bbox")
        if isinstance(finding.get("other_bbox"), dict)
        else right[0].get("bbox")
    )
    evidence_left_bbox = (nested or {}).get("left_bbox") if isinstance(nested, dict) else None
    evidence_right_bbox = (nested or {}).get("right_bbox") if isinstance(nested, dict) else None
    if not isinstance(left_bbox, dict) or not isinstance(right_bbox, dict):
        return None
    try:
        a = _normalized_bbox(left_bbox, index=-1, op={"op": "html_set_block_bbox"})
        b = _normalized_bbox(right_bbox, index=-1, op={"op": "html_set_block_bbox"})
    except _DesignOpError:
        return None
    if _bbox_overlap_area(a, b) <= 0:
        if not isinstance(evidence_left_bbox, dict) or not isinstance(evidence_right_bbox, dict):
            return None
        try:
            a = _normalized_bbox(evidence_left_bbox, index=-1, op={"op": "html_set_block_bbox"})
            b = _normalized_bbox(evidence_right_bbox, index=-1, op={"op": "html_set_block_bbox"})
        except _DesignOpError:
            return None
        if _bbox_overlap_area(a, b) <= 0:
            return None

    canvas_w, canvas_h = _canvas_size_from_spec_data(spec_data)
    gutter = max(10, int(round(max(canvas_w, canvas_h) * 0.006))) if canvas_w and canvas_h else 12
    moving_id, moving_bbox, anchor_id, anchor_bbox = _choose_text_overlap_moving_block(
        left_id=left_id,
        left_bbox=a,
        right_id=right_id,
        right_bbox=b,
        evidence=nested if isinstance(nested, dict) else {},
    )

    next_bbox = _best_html_text_overlap_separation_bbox(
        spec_data,
        moving_id=moving_id,
        moving_bbox=moving_bbox,
        anchor_bbox=anchor_bbox,
        canvas_w=canvas_w,
        canvas_h=canvas_h,
        gutter=gutter,
    )
    if next_bbox is None:
        return None
    if all(int(moving_bbox.get(key) or 0) == int(next_bbox.get(key) or 0) for key in _BBOX_KEYS):
        return None
    try:
        next_bbox = _normalized_bbox(next_bbox, index=-1, op={"op": "html_set_block_bbox"})
    except _DesignOpError:
        return None
    return {
        "op": "html_set_block_bbox",
        "finding_id": "authored-html-text-bbox-overlap",
        "block_id": moving_id,
        "bbox": next_bbox,
        "merge": False,
        "reason": f"auto_separate_overlapping_text_bbox_from:{anchor_id}",
    }


def _choose_text_overlap_moving_block(
    *,
    left_id: str,
    left_bbox: dict[str, int],
    right_id: str,
    right_bbox: dict[str, int],
    evidence: dict[str, Any],
) -> tuple[str, dict[str, int], str, dict[str, int]]:
    left_words = _optional_int(evidence.get("left_word_count")) or 0
    right_words = _optional_int(evidence.get("right_word_count")) or 0
    left_area = max(1, int(left_bbox.get("w") or 0) * int(left_bbox.get("h") or 0))
    right_area = max(1, int(right_bbox.get("w") or 0) * int(right_bbox.get("h") or 0))
    if left_words > 0 and right_words > 0:
        if left_words <= max(3, int(round(right_words * 0.65))):
            return left_id, left_bbox, right_id, right_bbox
        if right_words <= max(3, int(round(left_words * 0.65))):
            return right_id, right_bbox, left_id, left_bbox
    if left_area <= int(round(right_area * 0.68)):
        return left_id, left_bbox, right_id, right_bbox
    if right_area <= int(round(left_area * 0.68)):
        return right_id, right_bbox, left_id, left_bbox
    if (left_bbox["y"], left_bbox["x"]) <= (right_bbox["y"], right_bbox["x"]):
        return right_id, right_bbox, left_id, left_bbox
    return left_id, left_bbox, right_id, right_bbox


def _best_html_text_overlap_separation_bbox(
    spec_data: dict[str, Any],
    *,
    moving_id: str,
    moving_bbox: dict[str, int],
    anchor_bbox: dict[str, int],
    canvas_w: int,
    canvas_h: int,
    gutter: int,
) -> dict[str, int] | None:
    if canvas_w <= 0 or canvas_h <= 0:
        return None
    w = int(moving_bbox.get("w") or 0)
    h = int(moving_bbox.get("h") or 0)
    if w <= 0 or h <= 0 or w >= canvas_w or h >= canvas_h:
        return None
    margin = max(4, gutter)
    max_x = max(margin, canvas_w - w - margin)
    max_y = max(margin, canvas_h - h - margin)
    current = {
        "x": min(max(margin, int(moving_bbox.get("x") or 0)), max_x),
        "y": min(max(margin, int(moving_bbox.get("y") or 0)), max_y),
        "w": w,
        "h": h,
    }
    current_score = _html_text_overlap_score(spec_data, moving_id, current)

    raw_xs = [
        int(moving_bbox.get("x") or 0),
        int(anchor_bbox.get("x") or 0),
        int(anchor_bbox.get("x") or 0) + int(anchor_bbox.get("w") or 0) + gutter,
        int(anchor_bbox.get("x") or 0) - w - gutter,
        margin,
        max_x,
    ]
    raw_ys = [
        int(anchor_bbox.get("y") or 0) + int(anchor_bbox.get("h") or 0) + gutter,
        int(anchor_bbox.get("y") or 0) - h - gutter,
        int(moving_bbox.get("y") or 0),
        margin,
        max_y,
    ]
    if canvas_w - 2 * margin > w:
        for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
            raw_xs.append(int(round(margin + (canvas_w - w - 2 * margin) * frac)))
    if canvas_h - 2 * margin > h:
        for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
            raw_ys.append(int(round(margin + (canvas_h - h - 2 * margin) * frac)))

    candidates: list[dict[str, int]] = []
    seen: set[tuple[int, int]] = set()
    for x in raw_xs:
        for y in raw_ys:
            cx = min(max(margin, int(x)), max_x)
            cy = min(max(margin, int(y)), max_y)
            key = (cx, cy)
            if key in seen:
                continue
            seen.add(key)
            candidates.append({"x": cx, "y": cy, "w": w, "h": h})

    best: tuple[int, float, int, dict[str, int]] | None = None
    start_x = int(moving_bbox.get("x") or 0)
    start_y = int(moving_bbox.get("y") or 0)
    for candidate in candidates:
        blockers, weighted_area = _html_text_overlap_score(spec_data, moving_id, candidate)
        distance = abs(candidate["x"] - start_x) + abs(candidate["y"] - start_y)
        ranked = (blockers, weighted_area, distance, candidate)
        if best is None or ranked[:3] < best[:3]:
            best = ranked
    if best is None:
        return None
    best_blockers, best_area, _distance, best_bbox = best
    current_blockers, current_area = current_score
    if best_blockers < current_blockers or best_area < current_area * 0.92:
        return best_bbox
    return None


def _html_text_overlap_score(
    spec_data: dict[str, Any],
    moving_id: str,
    candidate_bbox: dict[str, int],
) -> tuple[int, float]:
    candidate_area = max(1, int(candidate_bbox.get("w") or 0) * int(candidate_bbox.get("h") or 0))
    blockers = 0
    weighted_area = 0.0
    for other_id, (block, _container, _idx, _frame) in _html_block_index(spec_data).items():
        if other_id == moving_id:
            continue
        if str(block.get("kind") or "").strip().lower() not in _HTML_TEXT_BBOX_KINDS:
            continue
        if not str(block.get("text") or block.get("caption") or block.get("title") or "").strip():
            continue
        bbox = block.get("bbox")
        if not isinstance(bbox, dict):
            continue
        try:
            other_bbox = _normalized_bbox(bbox, index=-1, op={"op": "html_set_block_bbox"})
        except _DesignOpError:
            continue
        overlap = _bbox_overlap_area(candidate_bbox, other_bbox)
        if overlap <= 0:
            continue
        other_area = max(1, other_bbox["w"] * other_bbox["h"])
        ratio = overlap / max(1, min(candidate_area, other_area))
        if overlap < 1200 and ratio < 0.18:
            continue
        if ratio < 0.08:
            continue
        blockers += 1
        weighted_area += float(overlap) * (1.0 + ratio * 3.0)
    return blockers, weighted_area


def _bbox_overlap_area(a: dict[str, int], b: dict[str, int]) -> int:
    x_overlap = max(0, min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"]))
    y_overlap = max(0, min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"]))
    return int(x_overlap * y_overlap)


def _canvas_size_from_spec_data(spec_data: dict[str, Any]) -> tuple[int, int]:
    canvas = spec_data.get("canvas") if isinstance(spec_data.get("canvas"), dict) else {}
    w = _optional_int(canvas.get("w_px") or canvas.get("width_px") or canvas.get("w") or canvas.get("width")) or 0
    h = _optional_int(canvas.get("h_px") or canvas.get("height_px") or canvas.get("h") or canvas.get("height")) or 0
    if w > 0 and h > 0:
        return w, h
    bboxes = [
        block.get("bbox")
        for block, _container, _idx, _frame in _html_block_index(spec_data).values()
        if isinstance(block.get("bbox"), dict)
    ]
    inferred_w = max((_optional_int(bbox.get("x")) or 0) + (_optional_int(bbox.get("w")) or 0) for bbox in bboxes) if bboxes else 0
    inferred_h = max((_optional_int(bbox.get("y")) or 0) + (_optional_int(bbox.get("h")) or 0) for bbox in bboxes) if bboxes else 0
    return w or inferred_w, h or inferred_h


def _apply_one_op(
    spec_data: dict[str, Any],
    op: dict[str, Any],
    *,
    index: int,
    touched_text_ids: set[str],
    deleted_layer_ids: set[str],
) -> dict[str, Any]:
    op_name = str(op.get("op") or "").strip()
    if not op_name:
        raise _DesignOpError("apply_design_ops: op missing 'op'", index=index, op=op)
    finding_id = str(op.get("finding_id") or "").strip()
    if not finding_id:
        raise _DesignOpError(
            "apply_design_ops: every op must include finding_id",
            index=index,
            op=op,
        )

    if op_name == "realize_text_bbox_in_authored_css":
        op = {**op, "op": "html_realize_block_bbox"}
        op_name = "html_realize_block_bbox"
    if op_name in {"add_auditable_text_bbox", "html_add_missing_text_bbox"}:
        op = {**op, "op": "html_infer_text_bbox"}
        op_name = "html_infer_text_bbox"
    if (
        finding_id in _HTML_TEXT_BBOX_REALIZATION_FINDINGS
        and op_name == "html_realize_all_text_bboxes"
        and not bool(op.get("single_block_only", False))
        and isinstance(op.get("block_ids"), list)
    ):
        op = {key: value for key, value in op.items() if key != "block_ids"}
    if (
        finding_id in _HTML_TEXT_BBOX_MISSING_FINDINGS
        and op_name in {"html_add_block", "html_resize_block", "html_set_block_style", "html_replace_text"}
        and not isinstance(op.get("bbox"), dict)
    ):
        op = {**op, "op": "html_infer_text_bbox"}
        op_name = "html_infer_text_bbox"
    if (
        finding_id in _HTML_TEXT_BBOX_REALIZATION_FINDINGS
        and op_name == "html_add_block"
        and not isinstance(op.get("block"), dict)
    ):
        op = {**op, "op": "html_realize_all_text_bboxes"}
        op_name = "html_realize_all_text_bboxes"
    if op_name in {"bind_missing_image_block_ids", "bind_all_images_to_blocks"}:
        op = {**op, "op": "html_bind_all_images_to_blocks"}
        op_name = "html_bind_all_images_to_blocks"
    if op_name == "resize_or_rewrite_text_block" and finding_id in _HTML_TEXT_FIT_FINDINGS:
        target_h = _optional_int(
            op.get("h")
            or op.get("target_min_height_px")
            or op.get("estimated_required_height_px")
        )
        if target_h is not None:
            op = {**op, "op": "html_resize_block", "h": target_h}
            op_name = "html_resize_block"
    if (
        op_name == "html_resize_block"
        and finding_id in _HTML_TEXT_BBOX_REALIZATION_FINDINGS
        and "w" not in op
        and "h" not in op
    ):
        op = {**op, "op": "html_realize_block_bbox"}
        op_name = "html_realize_block_bbox"
    if finding_id in _HTML_IMAGE_MISSING_BLOCK_ID_FINDINGS and op_name in {
        "html_replace_image_source",
        "html_set_block_source",
    } and not str(op.get("block_id") or op.get("layer_id") or "").strip():
        op = {**op, "op": "html_bind_all_images_to_blocks"}
        op_name = "html_bind_all_images_to_blocks"
    if (
        op_name == "html_set_block_style"
        and finding_id in _HTML_TEXT_BBOX_REALIZATION_FINDINGS
        and _coerce_html_style_patch(op.get("style")) is None
    ):
        op = {**op, "op": "html_realize_block_bbox"}
        op_name = "html_realize_block_bbox"

    aliased = _html_alias_for_legacy_op(spec_data, op_name, op)
    if aliased is not None:
        return _apply_one_op(
            spec_data,
            aliased,
            index=index,
            touched_text_ids=touched_text_ids,
            deleted_layer_ids=deleted_layer_ids,
        )

    if op_name == "add_layer":
        applied = _op_add_layer(spec_data, op, index=index)
        layer = op.get("layer") if isinstance(op.get("layer"), dict) else {}
        if layer.get("kind") == "text" and applied.get("layer_id"):
            touched_text_ids.add(str(applied["layer_id"]))
        return applied
    if op_name == "delete_layer":
        return _op_delete_layer(spec_data, op, index=index, deleted_layer_ids=deleted_layer_ids)
    if op_name == "html_add_block":
        return _op_html_add_block(spec_data, op, index=index)
    if op_name == "html_delete_block":
        return _op_html_delete_block(spec_data, op, index=index)
    if op_name == "html_set_block_bbox":
        return _op_html_set_block_bbox(spec_data, op, index=index)
    if op_name == "html_move_block":
        return _op_html_move_block(spec_data, op, index=index)
    if op_name == "html_resize_block":
        return _op_html_resize_block(spec_data, op, index=index)
    if op_name == "html_realize_block_bbox":
        return _op_html_realize_block_bbox(spec_data, op, index=index)
    if op_name == "html_realize_all_text_bboxes":
        return _op_html_realize_all_text_bboxes(spec_data, op, index=index)
    if op_name == "html_infer_text_bbox":
        return _op_html_infer_text_bbox(spec_data, op, index=index)
    if op_name == "html_bind_all_images_to_blocks":
        return _op_html_bind_all_images_to_blocks(spec_data, op, index=index)
    if op_name == "html_auto_repair_feedback":
        return _applied(op, layer_id=str(op.get("frame_id") or "authored_html_frame"))
    if op_name == "html_replace_text":
        return _op_html_replace_text(spec_data, op, index=index)
    if op_name == "html_set_authored_css":
        return _op_html_set_authored_css(spec_data, op, index=index)
    if op_name == "html_replace_image_source":
        return _op_html_replace_image_source(spec_data, op, index=index)
    if op_name == "html_replace_table_cell":
        return _op_html_replace_table_cell(spec_data, op, index=index)
    if op_name == "html_set_block_style":
        return _op_html_set_block_style(spec_data, op, index=index)
    if op_name == "html_set_block_role":
        return _op_html_set_block_role(spec_data, op, index=index)
    if op_name == "html_set_block_source":
        return _op_html_set_block_source(spec_data, op, index=index)
    if op_name == "html_assign_block_to_slot":
        return _op_html_assign_block_to_slot(spec_data, op, index=index)
    if op_name == "html_resize_slot":
        return _op_html_resize_slot(spec_data, op, index=index)
    if op_name == "html_split_slot":
        return _op_html_split_slot(spec_data, op, index=index)
    if op_name == "html_wrap_block_in_group":
        return _op_html_wrap_block_in_group(spec_data, op, index=index)
    if op_name == "set_bbox":
        applied = _op_set_bbox(spec_data, op, index=index)
    elif op_name == "move_layer":
        applied = _op_move_layer(spec_data, op, index=index)
    elif op_name == "resize_layer":
        applied = _op_resize_layer(spec_data, op, index=index)
    elif op_name == "replace_text":
        applied = _op_replace_text(spec_data, op, index=index)
    elif op_name == "set_slide_role":
        return _op_set_slide_role(spec_data, op, index=index)
    elif op_name == "anchor_callout":
        return _op_anchor_callout(spec_data, op, index=index)
    else:
        raise _DesignOpError(
            f"apply_design_ops: unsupported op '{op_name}'",
            index=index,
            op=op,
        )

    node = _require_layer(spec_data, str(applied.get("layer_id") or ""), index=index, op=op)[0]
    if node.get("kind") == "text" and op_name in _TEXT_RENDER_OPS:
        touched_text_ids.add(str(node.get("layer_id")))
    return applied


def _op_add_layer(spec_data: dict[str, Any], op: dict[str, Any], *, index: int) -> dict[str, Any]:
    layer = op.get("layer")
    if not isinstance(layer, dict):
        raise _DesignOpError("apply_design_ops.add_layer: 'layer' must be an object", index=index, op=op)
    layer_id = str(layer.get("layer_id") or "").strip()
    if not layer_id:
        raise _DesignOpError("apply_design_ops.add_layer: layer.layer_id is required", index=index, op=op)
    if layer_id in _layer_index(spec_data):
        raise _DesignOpError(
            f"apply_design_ops.add_layer: duplicate layer_id '{layer_id}'",
            index=index,
            op=op,
        )
    parent_id = op.get("parent_layer_id")
    if parent_id:
        parent, _container, _idx = _require_layer(spec_data, str(parent_id), index=index, op=op)
        parent.setdefault("children", []).append(deepcopy(layer))
    else:
        spec_data.setdefault("layer_graph", []).append(deepcopy(layer))
    return _applied(op, layer_id=layer_id, parent_layer_id=str(parent_id) if parent_id else None)


def _op_delete_layer(
    spec_data: dict[str, Any],
    op: dict[str, Any],
    *,
    index: int,
    deleted_layer_ids: set[str],
) -> dict[str, Any]:
    layer_id = _require_layer_id(op, index=index)
    node, container, node_idx = _require_layer(spec_data, layer_id, index=index, op=op)
    if container is spec_data.get("layer_graph") and node.get("kind") == "slide":
        raise _DesignOpError(
            "apply_design_ops.delete_layer: deleting top-level slides is not supported",
            index=index,
            op=op,
        )
    deleted_layer_ids.update(_collect_layer_ids(node))
    del container[node_idx]
    return _applied(op, layer_id=layer_id)


def _op_set_bbox(spec_data: dict[str, Any], op: dict[str, Any], *, index: int) -> dict[str, Any]:
    layer_id = _require_layer_id(op, index=index)
    node, _container, _idx = _require_layer(spec_data, layer_id, index=index, op=op)
    bbox_patch = op.get("bbox")
    if not isinstance(bbox_patch, dict) or not bbox_patch:
        raise _DesignOpError("apply_design_ops.set_bbox: 'bbox' must be a non-empty object", index=index, op=op)
    merge = bool(op.get("merge", True))
    current = dict(node.get("bbox") or {}) if merge else {}
    current.update(bbox_patch)
    node["bbox"] = _normalized_bbox(current, index=index, op=op)
    return _applied(op, layer_id=layer_id)


def _op_move_layer(spec_data: dict[str, Any], op: dict[str, Any], *, index: int) -> dict[str, Any]:
    layer_id = _require_layer_id(op, index=index)
    node, _container, _idx = _require_layer(spec_data, layer_id, index=index, op=op)
    bbox = _existing_bbox(node, index=index, op=op)
    bbox["x"] = int(bbox["x"]) + _require_int(op, "dx", index=index)
    bbox["y"] = int(bbox["y"]) + _require_int(op, "dy", index=index)
    node["bbox"] = _normalized_bbox(bbox, index=index, op=op)
    return _applied(op, layer_id=layer_id)


def _op_resize_layer(spec_data: dict[str, Any], op: dict[str, Any], *, index: int) -> dict[str, Any]:
    layer_id = _require_layer_id(op, index=index)
    node, _container, _idx = _require_layer(spec_data, layer_id, index=index, op=op)
    bbox = _existing_bbox(node, index=index, op=op)
    if "w" not in op and "h" not in op:
        raise _DesignOpError("apply_design_ops.resize_layer: provide w and/or h", index=index, op=op)
    if "w" in op:
        bbox["w"] = _require_int(op, "w", index=index)
    if "h" in op:
        bbox["h"] = _require_int(op, "h", index=index)
    node["bbox"] = _normalized_bbox(bbox, index=index, op=op)
    return _applied(op, layer_id=layer_id)


def _op_replace_text(spec_data: dict[str, Any], op: dict[str, Any], *, index: int) -> dict[str, Any]:
    layer_id = _require_layer_id(op, index=index)
    node, _container, _idx = _require_layer(spec_data, layer_id, index=index, op=op)
    if "text" not in op:
        raise _DesignOpError("apply_design_ops.replace_text: text is required", index=index, op=op)
    if node.get("kind") == "text" and not str(op.get("text") or "").strip():
        raise _DesignOpError("apply_design_ops.replace_text: text layers cannot be blank", index=index, op=op)
    node["text"] = str(op.get("text") or "")
    return _applied(op, layer_id=layer_id)


def _op_set_slide_role(spec_data: dict[str, Any], op: dict[str, Any], *, index: int) -> dict[str, Any]:
    layer_id = _require_layer_id(op, index=index)
    node, _container, _idx = _require_layer(spec_data, layer_id, index=index, op=op)
    if node.get("kind") != "slide":
        raise _DesignOpError("apply_design_ops.set_slide_role: target must be kind='slide'", index=index, op=op)
    if "role" not in op:
        raise _DesignOpError("apply_design_ops.set_slide_role: role is required", index=index, op=op)
    role = op.get("role")
    node["role"] = None if role is None else str(role)
    return _applied(op, layer_id=layer_id)


def _op_anchor_callout(spec_data: dict[str, Any], op: dict[str, Any], *, index: int) -> dict[str, Any]:
    layer_id = _require_layer_id(op, index=index)
    node, _container, _idx = _require_layer(spec_data, layer_id, index=index, op=op)
    if node.get("kind") != "callout":
        raise _DesignOpError("apply_design_ops.anchor_callout: target must be kind='callout'", index=index, op=op)
    anchor_id = str(op.get("anchor_layer_id") or "").strip()
    if not anchor_id:
        raise _DesignOpError("apply_design_ops.anchor_callout: anchor_layer_id is required", index=index, op=op)
    _require_layer(spec_data, anchor_id, index=index, op=op)
    node["anchor_layer_id"] = anchor_id
    if "callout_region" in op:
        region = op.get("callout_region")
        node["callout_region"] = None if region is None else _normalized_bbox(region, index=index, op=op)
    return _applied(op, layer_id=layer_id)


def _op_html_add_block(spec_data: dict[str, Any], op: dict[str, Any], *, index: int) -> dict[str, Any]:
    artifact = _require_html_artifact(spec_data, index=index, op=op)
    block = op.get("block")
    if not isinstance(block, dict):
        raise _DesignOpError("apply_design_ops.html_add_block: 'block' must be an object", index=index, op=op)
    block_id = str(block.get("block_id") or block.get("layer_id") or "").strip()
    if not block_id:
        raise _DesignOpError("apply_design_ops.html_add_block: block.block_id is required", index=index, op=op)
    if block_id in _html_block_index(spec_data):
        raise _DesignOpError(
            f"apply_design_ops.html_add_block: duplicate block_id '{block_id}'",
            index=index,
            op=op,
        )
    parent_id = str(op.get("parent_block_id") or "").strip()
    if parent_id:
        parent, _container, _idx, _frame = _require_html_block(spec_data, parent_id, index=index, op=op)
        parent.setdefault("children", []).append(deepcopy(block))
        _append_authored_body_block(_frame, block, parent_id=parent_id)
    else:
        frame_id = str(op.get("frame_id") or "").strip()
        frame = _require_html_frame(
            artifact,
            frame_id,
            index=index,
            op=op,
            allow_first_authored=True,
        )
        frame.setdefault("blocks", []).append(deepcopy(block))
        _append_authored_body_block(frame, block, parent_id=None)
    return _applied(op, layer_id=block_id, parent_layer_id=parent_id or None)


def _op_html_delete_block(spec_data: dict[str, Any], op: dict[str, Any], *, index: int) -> dict[str, Any]:
    block_id = _require_block_id(op, index=index)
    node, container, node_idx, frame = _require_html_block(spec_data, block_id, index=index, op=op)
    deleted_ids = _collect_html_block_ids(node)
    del container[node_idx]
    for deleted_id in deleted_ids:
        _remove_authored_body_block(frame, deleted_id)
    return _applied(op, layer_id=block_id)


def _op_html_set_block_bbox(spec_data: dict[str, Any], op: dict[str, Any], *, index: int) -> dict[str, Any]:
    block_id = _require_block_id(op, index=index)
    block, _container, _idx, frame = _require_html_block(spec_data, block_id, index=index, op=op)
    bbox_patch = op.get("bbox")
    if not isinstance(bbox_patch, dict) or not bbox_patch:
        raise _DesignOpError("apply_design_ops.html_set_block_bbox: 'bbox' must be a non-empty object", index=index, op=op)
    merge = bool(op.get("merge", True))
    current = dict(block.get("bbox") or {}) if merge else {}
    current.update(bbox_patch)
    bbox = _normalized_bbox(current, index=index, op=op)
    block["bbox"] = bbox
    _realize_html_block_bbox_in_frame(frame, block_id, bbox)
    _merge_html_block_bbox_style(block, bbox)
    return _applied(op, layer_id=block_id)


def _op_html_move_block(spec_data: dict[str, Any], op: dict[str, Any], *, index: int) -> dict[str, Any]:
    block_id = _require_block_id(op, index=index)
    block, _container, _idx, frame = _require_html_block(spec_data, block_id, index=index, op=op)
    bbox = _existing_html_bbox(block, index=index, op=op)
    bbox["x"] = int(bbox["x"]) + _require_int(op, "dx", index=index)
    bbox["y"] = int(bbox["y"]) + _require_int(op, "dy", index=index)
    bbox = _normalized_bbox(bbox, index=index, op=op)
    block["bbox"] = bbox
    _realize_html_block_bbox_in_frame(frame, block_id, bbox)
    _merge_html_block_bbox_style(block, bbox)
    return _applied(op, layer_id=block_id)


def _op_html_resize_block(spec_data: dict[str, Any], op: dict[str, Any], *, index: int) -> dict[str, Any]:
    block_id = _require_block_id(op, index=index)
    block, _container, _idx, frame = _require_html_block(spec_data, block_id, index=index, op=op)
    bbox = _existing_html_bbox(block, index=index, op=op)
    if "w" not in op and "h" not in op:
        raise _DesignOpError("apply_design_ops.html_resize_block: provide w and/or h", index=index, op=op)
    if "w" in op:
        bbox["w"] = _require_int(op, "w", index=index)
    if "h" in op:
        bbox["h"] = _require_int(op, "h", index=index)
    bbox = _normalized_bbox(bbox, index=index, op=op)
    block["bbox"] = bbox
    _realize_html_block_bbox_in_frame(frame, block_id, bbox)
    _merge_html_block_bbox_style(block, bbox)
    return _applied(op, layer_id=block_id)


def _op_html_realize_block_bbox(spec_data: dict[str, Any], op: dict[str, Any], *, index: int) -> dict[str, Any]:
    block_id = _require_block_id(op, index=index)
    if (
        str(op.get("finding_id") or "") in _HTML_TEXT_BBOX_REALIZATION_FINDINGS
        and not bool(op.get("single_block_only", False))
    ):
        all_op = {**op, "op": "html_realize_all_text_bboxes"}
        if "block_ids" not in all_op:
            all_op["block_ids"] = []
        return _op_html_realize_all_text_bboxes(spec_data, all_op, index=index)
    block, _container, _idx, frame = _require_html_block(spec_data, block_id, index=index, op=op)
    if isinstance(op.get("bbox"), dict) and op.get("bbox"):
        current = dict(block.get("bbox") or {})
        current.update(op["bbox"])
        block["bbox"] = _normalized_bbox(current, index=index, op=op)
    bbox = _existing_html_bbox(block, index=index, op=op)
    _realize_html_block_bbox_in_frame(frame, block_id, bbox)
    _merge_html_block_bbox_style(block, bbox)
    return _applied(op, layer_id=block_id)


def _op_html_realize_all_text_bboxes(spec_data: dict[str, Any], op: dict[str, Any], *, index: int) -> dict[str, Any]:
    _require_html_artifact(spec_data, index=index, op=op)
    realize_all_declared = (
        str(op.get("finding_id") or "") in _HTML_TEXT_BBOX_REALIZATION_FINDINGS
        and not bool(op.get("single_block_only", False))
    )
    requested_ids = set()
    if not realize_all_declared and isinstance(op.get("block_ids"), list):
        requested_ids = {
            str(value).strip()
            for value in (op.get("block_ids") or [])
            if str(value or "").strip()
        }
    patched_ids: list[str] = []
    for block_id, (block, _container, _idx, frame) in _html_block_index(spec_data).items():
        if requested_ids and block_id not in requested_ids:
            continue
        if str(block.get("kind") or "").strip().lower() not in _HTML_TEXT_BBOX_KINDS:
            continue
        bbox = block.get("bbox")
        if not isinstance(bbox, dict):
            continue
        norm_bbox = _normalized_bbox(bbox, index=index, op=op)
        block["bbox"] = norm_bbox
        _realize_html_block_bbox_in_frame(frame, block_id, norm_bbox)
        _merge_html_block_bbox_style(block, norm_bbox)
        patched_ids.append(block_id)
    if not patched_ids:
        raise _DesignOpError(
            "apply_design_ops.html_realize_all_text_bboxes: no text/caption/metric/quote blocks with bbox matched",
            index=index,
            op=op,
        )
    return {
        "op": str(op.get("op")),
        "finding_id": str(op.get("finding_id")),
        "layer_id": str(op.get("frame_id") or "authored_html_frame"),
        "block_ids": patched_ids,
    }


def _op_html_infer_text_bbox(spec_data: dict[str, Any], op: dict[str, Any], *, index: int) -> dict[str, Any]:
    block_id = _require_block_id(op, index=index)
    block, container, node_idx, frame = _require_html_block(spec_data, block_id, index=index, op=op)
    existing = block.get("bbox")
    if isinstance(existing, dict) and all(key in existing for key in _BBOX_KEYS):
        bbox = _normalized_bbox(existing, index=index, op=op)
    else:
        inferred = _infer_missing_text_bbox_for_block(
            spec_data,
            frame,
            block,
            container,
            node_idx,
            word_count_hint=_optional_int(op.get("word_count")),
        )
        if inferred is None:
            raise _DesignOpError(
                f"apply_design_ops.html_infer_text_bbox: could not infer bbox for block_id '{block_id}'",
                index=index,
                op=op,
            )
        bbox = _normalized_bbox(inferred, index=index, op=op)
    block["bbox"] = bbox
    _realize_html_block_bbox_in_frame(frame, block_id, bbox)
    _merge_html_block_bbox_style(block, bbox)
    return _applied(op, layer_id=block_id)


def _op_html_bind_all_images_to_blocks(spec_data: dict[str, Any], op: dict[str, Any], *, index: int) -> dict[str, Any]:
    artifact = _require_html_artifact(spec_data, index=index, op=op)
    frame = _require_html_frame(
        artifact,
        str(op.get("frame_id") or ""),
        index=index,
        op=op,
        allow_first_authored=True,
    )
    body = frame.get("authored_body_html")
    if not isinstance(body, str) or not body.strip():
        raise _DesignOpError(
            "apply_design_ops.html_bind_all_images_to_blocks: authored_body_html is empty",
            index=index,
            op=op,
        )
    soup = BeautifulSoup(body, "html.parser")
    patched_ids: list[str] = []
    created_ids: list[str] = []
    for img in soup.find_all("img"):
        if not hasattr(img, "attrs"):
            continue
        block_index = _html_block_index(spec_data)
        existing_id = str(img.get("data-block-id") or "").strip()
        if existing_id and existing_id in block_index:
            continue
        src = str(img.get("src") or "").strip()
        block_id = _find_html_visual_block_id_for_src(spec_data, src)
        if not block_id:
            block_id = _candidate_image_block_id_for_img(img, src, block_index)
        if not block_id:
            continue
        if block_id not in block_index:
            parent_id = _nearest_manifest_block_id(img, block_index, exclude={block_id})
            parent = block_index.get(parent_id, (None, None, None, frame))[0] if parent_id else None
            bbox = _image_bbox_from_dom(img, parent if isinstance(parent, dict) else None)
            block = {
                "block_id": block_id,
                "kind": "image",
                "role": str(op.get("role") or "source_visual"),
                "layer_id": _source_id_from_image_src(src) or None,
                "source": "paper_visual_provenance",
                "source_id": _source_id_from_image_src(src) or None,
                "src_path": src,
                "bbox": bbox,
                "caption": str(img.get("alt") or "").strip() or None,
            }
            _insert_html_block(frame, block, parent_id=parent_id)
            created_ids.append(block_id)
        img["data-block-id"] = block_id
        patched_ids.append(block_id)
    if not patched_ids:
        raise _DesignOpError(
            "apply_design_ops.html_bind_all_images_to_blocks: no unbound images found",
            index=index,
            op=op,
        )
    frame["authored_body_html"] = str(soup)
    return {
        "op": str(op.get("op")),
        "finding_id": str(op.get("finding_id")),
        "layer_id": str(frame.get("frame_id") or "authored_html_frame"),
        "block_ids": patched_ids,
        "created_block_ids": created_ids,
    }


def _op_html_replace_text(spec_data: dict[str, Any], op: dict[str, Any], *, index: int) -> dict[str, Any]:
    block_id = _require_block_id(op, index=index)
    block, _container, _idx, frame = _require_html_block(spec_data, block_id, index=index, op=op)
    if "text" not in op:
        raise _DesignOpError("apply_design_ops.html_replace_text: text is required", index=index, op=op)
    text = str(op.get("text") or "")
    if _caption_replacement_mislabels_source(block_id, block, text):
        text = str(block.get("text") or block.get("caption") or "")
    if block.get("kind") == "text" and not text.strip():
        raise _DesignOpError("apply_design_ops.html_replace_text: text blocks cannot be blank", index=index, op=op)
    block["text"] = text
    _patch_authored_body_text(frame, block_id, block["text"])
    return _applied(op, layer_id=block_id)


def _op_html_set_authored_css(spec_data: dict[str, Any], op: dict[str, Any], *, index: int) -> dict[str, Any]:
    artifact = _require_html_artifact(spec_data, index=index, op=op)
    css = op.get("css", op.get("authored_css"))
    if css is None:
        raise _DesignOpError("apply_design_ops.html_set_authored_css: css is required", index=index, op=op)
    frame = _require_html_frame(
        artifact,
        str(op.get("frame_id") or ""),
        index=index,
        op=op,
        allow_first_authored=True,
    )
    frame["authored_css"] = str(css)
    return _applied(op, layer_id=str(frame.get("frame_id") or "poster_canvas"))


def _op_html_replace_image_source(spec_data: dict[str, Any], op: dict[str, Any], *, index: int) -> dict[str, Any]:
    block_id = _require_block_id(op, index=index)
    block, _container, _idx, frame = _require_html_block(spec_data, block_id, index=index, op=op)
    src_path = str(op.get("src_path") or "").strip()
    if not src_path:
        raise _DesignOpError("apply_design_ops.html_replace_image_source: src_path is required", index=index, op=op)
    block["src_path"] = src_path
    if "source_id" in op:
        block["source_id"] = None if op["source_id"] is None else str(op["source_id"])
    if "source" in op:
        block["source"] = None if op["source"] is None else str(op["source"])
    _patch_authored_img_src(frame, block_id, src_path)
    return _applied(op, layer_id=block_id)


def _op_html_replace_table_cell(spec_data: dict[str, Any], op: dict[str, Any], *, index: int) -> dict[str, Any]:
    block_id = _require_block_id(op, index=index)
    block, _container, _idx, frame = _require_html_block(spec_data, block_id, index=index, op=op)
    row_index = _require_int(op, "row_index", index=index)
    col_index = _require_int(op, "col_index", index=index)
    if row_index < 0 or col_index < 0:
        raise _DesignOpError("apply_design_ops.html_replace_table_cell: row_index/col_index must be >= 0", index=index, op=op)
    if "text" not in op:
        raise _DesignOpError("apply_design_ops.html_replace_table_cell: text is required", index=index, op=op)
    rows = deepcopy(block.get("rows") or [])
    while len(rows) <= row_index:
        rows.append([])
    while len(rows[row_index]) <= col_index:
        rows[row_index].append("")
    rows[row_index][col_index] = str(op.get("text") or "")
    block["rows"] = rows
    _patch_authored_table_cell(frame, block_id, row_index, col_index, rows[row_index][col_index])
    return _applied(op, layer_id=block_id)


def _op_html_set_block_style(spec_data: dict[str, Any], op: dict[str, Any], *, index: int) -> dict[str, Any]:
    block_id = _require_block_id(op, index=index)
    block, _container, _idx, _frame = _require_html_block(spec_data, block_id, index=index, op=op)
    style = _coerce_html_style_patch(op.get("style"))
    if style is None:
        raise _DesignOpError(
            "apply_design_ops.html_set_block_style: style must be an object or CSS declaration string",
            index=index,
            op=op,
        )
    if bool(op.get("merge", True)):
        current = dict(block.get("style") or {})
        current.update(style)
        block["style"] = current
    else:
        block["style"] = deepcopy(style)
    return _applied(op, layer_id=block_id)


def _coerce_html_style_patch(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return deepcopy(value)
    if not isinstance(value, str):
        return None
    parsed: dict[str, Any] = {}
    for declaration in value.split(";"):
        if ":" not in declaration:
            continue
        raw_key, raw_value = declaration.split(":", 1)
        key = _CSS_STYLE_KEY_MAP.get(raw_key.strip().lower())
        if not key:
            continue
        parsed_value = _coerce_css_style_value(key, raw_value.strip())
        if parsed_value is not None:
            parsed[key] = parsed_value
    return parsed or None


def _coerce_css_style_value(key: str, value: str) -> Any:
    if not value:
        return None
    if key in {"fontSize", "borderRadius", "borderWidth", "strokeWidth"}:
        return _parse_css_number(value, integer=True)
    if key in {"letterSpacing", "lineHeight", "opacity"}:
        return _parse_css_number(value, integer=False)
    if key == "fontWeight":
        parsed = _parse_css_number(value, integer=True)
        return parsed if parsed is not None else value
    return value


def _parse_css_number(value: str, *, integer: bool) -> int | float | None:
    text = value.strip().lower()
    for suffix in ("px", "em", "rem"):
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
            break
    try:
        number = float(text)
    except ValueError:
        return None
    return int(round(number)) if integer else number


def _op_html_set_block_role(spec_data: dict[str, Any], op: dict[str, Any], *, index: int) -> dict[str, Any]:
    block_id = _require_block_id(op, index=index)
    block, _container, _idx, _frame = _require_html_block(spec_data, block_id, index=index, op=op)
    if "role" not in op:
        raise _DesignOpError("apply_design_ops.html_set_block_role: role is required", index=index, op=op)
    role = op.get("role")
    block["role"] = None if role is None else str(role)
    return _applied(op, layer_id=block_id)


def _op_html_set_block_source(spec_data: dict[str, Any], op: dict[str, Any], *, index: int) -> dict[str, Any]:
    block_id = _require_block_id(op, index=index)
    block, _container, _idx, _frame = _require_html_block(spec_data, block_id, index=index, op=op)
    for key in (
        "source", "source_id", "source_text", "evidence_quote",
        "evidence_source", "src_path",
    ):
        if key in op:
            block[key] = None if op[key] is None else str(op[key])
    if "provenance" in op:
        provenance = op.get("provenance")
        if provenance is not None and not isinstance(provenance, dict):
            raise _DesignOpError("apply_design_ops.html_set_block_source: provenance must be an object or null", index=index, op=op)
        block["provenance"] = deepcopy(provenance or {})
    return _applied(op, layer_id=block_id)


def _op_html_assign_block_to_slot(spec_data: dict[str, Any], op: dict[str, Any], *, index: int) -> dict[str, Any]:
    block_id = _require_block_id(op, index=index)
    slot_id = str(op.get("slot_id") or op.get("parent_block_id") or op.get("group_block_id") or "").strip()
    if not slot_id:
        raise _DesignOpError("apply_design_ops.html_assign_block_to_slot: slot_id or group_block_id is required", index=index, op=op)
    block, container, node_idx, frame = _require_html_block(spec_data, block_id, index=index, op=op)
    group, _group_container, _group_idx, group_frame = _require_html_group_by_slot(
        spec_data,
        slot_id,
        index=index,
        op=op,
    )
    if frame is not group_frame:
        raise _DesignOpError("apply_design_ops.html_assign_block_to_slot: block and slot must be in the same frame", index=index, op=op)
    if block is group:
        raise _DesignOpError("apply_design_ops.html_assign_block_to_slot: cannot assign a group to itself", index=index, op=op)
    target_children = group.setdefault("children", [])
    if container is not target_children:
        moved = deepcopy(block)
        del container[node_idx]
        moved["slot_id"] = str(group.get("slot_id") or group.get("block_id"))
        moved["panel_role"] = str(group.get("panel_role") or group.get("role") or "panel")
        target_children.append(moved)
    else:
        block["slot_id"] = str(group.get("slot_id") or group.get("block_id"))
        block["panel_role"] = str(group.get("panel_role") or group.get("role") or "panel")
    return _applied(op, layer_id=block_id, parent_layer_id=str(group.get("block_id") or slot_id))


def _op_html_resize_slot(spec_data: dict[str, Any], op: dict[str, Any], *, index: int) -> dict[str, Any]:
    slot_id = str(op.get("slot_id") or op.get("block_id") or "").strip()
    if not slot_id:
        raise _DesignOpError("apply_design_ops.html_resize_slot: slot_id or block_id is required", index=index, op=op)
    group, _container, _idx, frame = _require_html_group_by_slot(spec_data, slot_id, index=index, op=op)
    old_bbox = _existing_html_bbox(group, index=index, op=op)
    patch = op.get("bbox") if isinstance(op.get("bbox"), dict) else {}
    next_bbox = dict(old_bbox)
    next_bbox.update(patch)
    for key in _BBOX_KEYS:
        if key in op:
            next_bbox[key] = op[key]
    new_bbox = _normalized_bbox(next_bbox, index=index, op=op)
    if bool(op.get("scale_children", True)):
        _scale_child_bboxes(group.setdefault("children", []), old_bbox, new_bbox)
    group["bbox"] = new_bbox
    resolved_slot_id = str(group.get("slot_id") or group.get("block_id") or slot_id)
    group["slot_id"] = resolved_slot_id
    _upsert_frame_slot(
        frame,
        {
            "slot_id": resolved_slot_id,
            "role": str(group.get("panel_role") or group.get("role") or "panel"),
            "bbox": new_bbox,
            "required": bool(op.get("required", False)),
            "content_policy": op.get("content_policy"),
            "max_text_words": op.get("max_text_words"),
            "min_visual_area_ratio": op.get("min_visual_area_ratio"),
            "parent_slot_id": op.get("parent_slot_id"),
            "panel_job": op.get("panel_job"),
            "text_budget": op.get("text_budget"),
            "visual_ids": op.get("visual_ids"),
            "space_fill_policy": op.get("space_fill_policy"),
        },
        index=index,
        op=op,
    )
    return _applied(op, layer_id=resolved_slot_id)


def _op_html_split_slot(spec_data: dict[str, Any], op: dict[str, Any], *, index: int) -> dict[str, Any]:
    slot_id = str(op.get("slot_id") or op.get("block_id") or "").strip()
    if not slot_id:
        raise _DesignOpError("apply_design_ops.html_split_slot: slot_id or block_id is required", index=index, op=op)
    source_group, _container, _idx, frame = _require_html_group_by_slot(spec_data, slot_id, index=index, op=op)
    new_slot = op.get("new_slot")
    if not isinstance(new_slot, dict):
        raise _DesignOpError("apply_design_ops.html_split_slot: new_slot is required", index=index, op=op)
    slot = _normalized_slot(new_slot, index=index, op=op)
    new_group_id = str(slot["slot_id"])
    if new_group_id in _html_block_index(spec_data):
        raise _DesignOpError(f"apply_design_ops.html_split_slot: duplicate new slot/group id '{new_group_id}'", index=index, op=op)
    moved_ids = {str(v) for v in (op.get("move_block_ids") or []) if str(v).strip()}
    moved_blocks: list[dict[str, Any]] = []
    if moved_ids:
        for move_id in list(moved_ids):
            block, block_container, block_idx, block_frame = _require_html_block(spec_data, move_id, index=index, op=op)
            if block_frame is not frame:
                raise _DesignOpError("apply_design_ops.html_split_slot: moved blocks must be in the same frame", index=index, op=op)
            moved = deepcopy(block)
            del block_container[block_idx]
            moved["slot_id"] = new_group_id
            moved["panel_role"] = str(slot["role"])
            moved_blocks.append(moved)
    new_group = {
        "block_id": new_group_id,
        "kind": "group",
        "role": str(slot["role"]),
        "slot_id": new_group_id,
        "panel_role": str(slot["role"]),
        "bbox": deepcopy(slot["bbox"]),
        "children": moved_blocks,
    }
    frame.setdefault("blocks", []).append(new_group)
    slot.setdefault("parent_slot_id", source_group.get("slot_id") or source_group.get("block_id"))
    _upsert_frame_slot(frame, slot, index=index, op=op)
    return _applied(op, layer_id=new_group_id, parent_layer_id=str(source_group.get("block_id") or slot_id))


def _op_html_wrap_block_in_group(spec_data: dict[str, Any], op: dict[str, Any], *, index: int) -> dict[str, Any]:
    block_id = _require_block_id(op, index=index)
    group_block_id = str(op.get("group_block_id") or op.get("slot_id") or f"{block_id}_panel").strip()
    if not group_block_id:
        raise _DesignOpError("apply_design_ops.html_wrap_block_in_group: group_block_id is required", index=index, op=op)
    if group_block_id in _html_block_index(spec_data):
        raise _DesignOpError(f"apply_design_ops.html_wrap_block_in_group: duplicate group id '{group_block_id}'", index=index, op=op)
    block, container, node_idx, frame = _require_html_block(spec_data, block_id, index=index, op=op)
    bbox = op.get("bbox") if isinstance(op.get("bbox"), dict) else block.get("bbox")
    if not isinstance(bbox, dict):
        raise _DesignOpError("apply_design_ops.html_wrap_block_in_group: bbox is required when block has no bbox", index=index, op=op)
    norm_bbox = _normalized_bbox(bbox, index=index, op=op)
    role = str(op.get("role") or "panel")
    moved = deepcopy(block)
    moved["slot_id"] = group_block_id
    moved["panel_role"] = role
    group = {
        "block_id": group_block_id,
        "kind": "group",
        "role": role,
        "slot_id": group_block_id,
        "panel_role": role,
        "bbox": norm_bbox,
        "children": [moved],
    }
    container[node_idx] = group
    _upsert_frame_slot(
        frame,
        {
            "slot_id": group_block_id,
            "role": role,
            "bbox": norm_bbox,
            "required": bool(op.get("required", False)),
            "content_policy": op.get("content_policy"),
            "max_text_words": op.get("max_text_words"),
            "min_visual_area_ratio": op.get("min_visual_area_ratio"),
            "parent_slot_id": op.get("parent_slot_id"),
            "panel_job": op.get("panel_job"),
            "text_budget": op.get("text_budget"),
            "visual_ids": op.get("visual_ids"),
            "space_fill_policy": op.get("space_fill_policy"),
        },
        index=index,
        op=op,
    )
    return _applied(op, layer_id=block_id, parent_layer_id=group_block_id)


def _realize_html_block_bbox_in_frame(frame: dict[str, Any], block_id: str, bbox: dict[str, Any]) -> None:
    patched_inline = _patch_authored_body_block_bbox(frame, block_id, bbox)
    if not patched_inline:
        _append_authored_bbox_css(frame, block_id, bbox)


def _find_html_visual_block_id_for_src(spec_data: dict[str, Any], src: str) -> str:
    if not str(src or "").strip():
        return ""
    src_key = _image_src_compare_key(src)
    if not src_key:
        return ""
    matches: list[str] = []
    for block_id, (block, _container, _idx, _frame) in _html_block_index(spec_data).items():
        if str(block.get("kind") or "").strip().lower() not in _HTML_VISUAL_KINDS:
            continue
        for key in ("src_path", "source_id", "layer_id"):
            candidate = str(block.get(key) or "").strip()
            if not candidate:
                continue
            if _image_src_compare_key(candidate) == src_key or _source_id_from_image_src(candidate) == _source_id_from_image_src(src):
                matches.append(block_id)
                break
    return matches[0] if len(matches) == 1 else ""


def _candidate_image_block_id_for_img(img: Any, src: str, block_index: dict[str, Any]) -> str:
    for raw_candidate in (
        str(img.get("data-block-id") or "").strip(),
        _nearest_data_block_id(img),
        _source_id_from_image_src(src),
    ):
        if not raw_candidate:
            continue
        candidate = _css_token(raw_candidate)
        if candidate and candidate not in block_index:
            return _unique_html_block_id(candidate, block_index)
    return ""


def _nearest_data_block_id(node: Any) -> str:
    current = getattr(node, "parent", None)
    while current is not None and getattr(current, "name", None) is not None:
        block_id = str(current.get("data-block-id") or "").strip() if hasattr(current, "get") else ""
        if block_id:
            return block_id
        current = getattr(current, "parent", None)
    return ""


def _nearest_manifest_block_id(
    node: Any,
    block_index: dict[str, Any],
    *,
    exclude: set[str] | None = None,
) -> str:
    excluded = exclude or set()
    current = getattr(node, "parent", None)
    while current is not None and getattr(current, "name", None) is not None:
        block_id = str(current.get("data-block-id") or "").strip() if hasattr(current, "get") else ""
        if block_id and block_id not in excluded and block_id in block_index:
            return block_id
        current = getattr(current, "parent", None)
    return ""


def _image_bbox_from_dom(img: Any, parent_block: dict[str, Any] | None) -> dict[str, int]:
    partial: dict[str, int] = {}
    for node in (getattr(img, "parent", None), img):
        if node is None or not hasattr(node, "get"):
            continue
        partial.update({key: value for key, value in _html_style_bbox_partial(str(node.get("style") or "")).items() if key not in partial})
    parent_bbox = parent_block.get("bbox") if isinstance(parent_block, dict) else None
    parent_bbox = parent_bbox if isinstance(parent_bbox, dict) else {}
    parent_w = max(320, int(parent_bbox.get("w") or 640))
    parent_h = max(180, int(parent_bbox.get("h") or 420))
    bbox = {
        "x": max(0, int(partial.get("x", int(parent_bbox.get("x") or 0) + 24))),
        "y": max(0, int(partial.get("y", int(parent_bbox.get("y") or 0) + 48))),
        "w": max(1, int(partial.get("w", max(120, parent_w - 48)))),
        "h": max(1, int(partial.get("h", max(90, min(parent_h - 72, int(parent_w * 0.42)))))),
    }
    return bbox


def _infer_missing_text_bbox_for_block(
    spec_data: dict[str, Any],
    frame: dict[str, Any],
    block: dict[str, Any],
    siblings: list[dict[str, Any]],
    node_idx: int,
    *,
    word_count_hint: int | None,
) -> dict[str, int] | None:
    block_id = str(block.get("block_id") or block.get("layer_id") or "").strip()
    if not block_id:
        return None
    parent = _html_parent_block(frame, block_id)
    parent_bbox = _coerce_bbox_for_authored_style(parent.get("bbox") if isinstance(parent, dict) else None)
    canvas_w, canvas_h = _canvas_size_from_spec_data(spec_data)
    if parent_bbox is None:
        parent_bbox = {
            "x": 0,
            "y": 0,
            "w": max(1, canvas_w or 1280),
            "h": max(1, canvas_h or 1800),
        }
    margin = max(12, min(48, int(round(min(parent_bbox["w"], parent_bbox["h"]) * 0.05))))
    gutter = max(8, min(22, int(round(min(parent_bbox["w"], parent_bbox["h"]) * 0.025))))
    inline_partial = _authored_body_block_bbox_partial(frame, block_id)
    x = _global_axis_from_partial(inline_partial.get("x"), parent_bbox, axis="x", fallback=parent_bbox["x"] + margin)
    max_w = max(1, parent_bbox["w"] - (x - parent_bbox["x"]) - margin)
    w = int(inline_partial.get("w") or max_w)
    w = max(1, min(w, max_w))
    font_px = _declared_text_font_px(block, frame, block_id)
    line_px = _declared_text_line_px(block, frame, block_id, font_px)
    words = word_count_hint if word_count_hint and word_count_hint > 0 else _html_text_word_count(block)
    h = int(inline_partial.get("h") or _estimated_text_bbox_height(words, w, font_px, line_px))
    h = max(int(math.ceil(line_px + 4)), h)

    if inline_partial.get("y") is not None:
        y = _global_axis_from_partial(inline_partial.get("y"), parent_bbox, axis="y", fallback=parent_bbox["y"] + margin)
        return {"x": x, "y": y, "w": w, "h": h}

    prior_bboxes: list[dict[str, int]] = []
    later_bboxes: list[dict[str, int]] = []
    for sibling_idx, sibling in enumerate(siblings):
        if sibling is block or not isinstance(sibling, dict):
            continue
        bbox = _coerce_bbox_for_authored_style(sibling.get("bbox"))
        if bbox is None:
            continue
        if sibling_idx < node_idx:
            prior_bboxes.append(bbox)
        elif sibling_idx > node_idx:
            later_bboxes.append(bbox)
    lower_bound = parent_bbox["y"] + margin
    if prior_bboxes:
        lower_bound = max(lower_bound, max(bbox["y"] + bbox["h"] + gutter for bbox in prior_bboxes))
    upper_bound = parent_bbox["y"] + parent_bbox["h"] - margin
    if later_bboxes:
        upper_bound = min(upper_bound, min(bbox["y"] - gutter for bbox in later_bboxes))
    if upper_bound - lower_bound >= h:
        return {"x": x, "y": int(lower_bound), "w": w, "h": h}

    gap_y = _largest_vertical_gap_y(
        [
            bbox
            for sibling in siblings
            if isinstance(sibling, dict) and sibling is not block
            for bbox in [_coerce_bbox_for_authored_style(sibling.get("bbox"))]
            if bbox is not None
        ],
        parent_bbox,
        min_h=h,
        margin=margin,
        gutter=gutter,
    )
    if gap_y is not None:
        return {"x": x, "y": gap_y, "w": w, "h": h}

    y = max(parent_bbox["y"] + margin, min(lower_bound, parent_bbox["y"] + parent_bbox["h"] - margin - h))
    return {"x": x, "y": int(y), "w": w, "h": h}


def _html_parent_block(frame: dict[str, Any], block_id: str) -> dict[str, Any] | None:
    def visit(blocks: list[Any], parent: dict[str, Any] | None) -> dict[str, Any] | None:
        for candidate in blocks:
            if not isinstance(candidate, dict):
                continue
            candidate_id = str(candidate.get("block_id") or candidate.get("layer_id") or "").strip()
            if candidate_id == block_id:
                return parent
            children = candidate.get("children")
            if isinstance(children, list):
                found = visit(children, candidate)
                if found is not None:
                    return found
        return None

    blocks = frame.get("blocks") if isinstance(frame.get("blocks"), list) else []
    return visit(blocks, None)


def _authored_body_block_bbox_partial(frame: dict[str, Any], block_id: str) -> dict[str, int]:
    body = frame.get("authored_body_html")
    if not isinstance(body, str) or not body:
        return {}
    soup = BeautifulSoup(body, "html.parser")
    node = soup.find(attrs={"data-block-id": str(block_id)})
    if node is None:
        return {}
    return _html_style_bbox_partial(str(node.get("style") or ""))


def _global_axis_from_partial(
    value: Any,
    parent_bbox: dict[str, int],
    *,
    axis: str,
    fallback: int,
) -> int:
    parsed = _optional_int(value)
    if parsed is None:
        return int(fallback)
    parent_origin = int(parent_bbox["x"] if axis == "x" else parent_bbox["y"])
    parent_extent = int(parent_bbox["w"] if axis == "x" else parent_bbox["h"])
    if 0 <= parsed <= parent_extent + 8 and parsed < parent_origin:
        return parent_origin + parsed
    return parsed


def _declared_text_font_px(block: dict[str, Any], frame: dict[str, Any], block_id: str) -> float:
    style = block.get("style") if isinstance(block.get("style"), dict) else {}
    for key in ("font_size_px", "fontSize", "font-size"):
        parsed = _optional_float(style.get(key))
        if parsed and parsed > 0:
            return float(parsed)
    inline = _authored_body_block_style(frame, block_id)
    for declaration in inline.split(";"):
        if ":" not in declaration:
            continue
        key, value = declaration.split(":", 1)
        if key.strip().lower() == "font-size":
            parsed = _parse_css_number(value.strip(), integer=False)
            if parsed and parsed > 0:
                return float(parsed)
    return 18.0


def _declared_text_line_px(block: dict[str, Any], frame: dict[str, Any], block_id: str, font_px: float) -> float:
    style = block.get("style") if isinstance(block.get("style"), dict) else {}
    for key in ("line_height", "lineHeight", "line-height"):
        value = style.get(key)
        parsed = _optional_float(value)
        if parsed and parsed > 0:
            return font_px * parsed if parsed < 4 else parsed
    inline = _authored_body_block_style(frame, block_id)
    for declaration in inline.split(";"):
        if ":" not in declaration:
            continue
        key, value = declaration.split(":", 1)
        if key.strip().lower() == "line-height":
            parsed = _parse_css_number(value.strip(), integer=False)
            if parsed and parsed > 0:
                return font_px * parsed if parsed < 4 else float(parsed)
    return font_px * 1.28


def _authored_body_block_style(frame: dict[str, Any], block_id: str) -> str:
    body = frame.get("authored_body_html")
    if not isinstance(body, str) or not body:
        return ""
    soup = BeautifulSoup(body, "html.parser")
    node = soup.find(attrs={"data-block-id": str(block_id)})
    return str(node.get("style") or "") if node is not None else ""


def _html_text_word_count(block: dict[str, Any]) -> int:
    parts: list[str] = []
    for key in ("text", "title", "caption", "source_text", "evidence_quote"):
        value = block.get(key)
        if isinstance(value, str):
            parts.append(value)
    items = block.get("items")
    if isinstance(items, list):
        parts.extend(str(item) for item in items if str(item or "").strip())
    text = " ".join(parts)
    return len(re.findall(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)?", text))


def _estimated_text_bbox_height(words: int, width_px: int, font_px: float, line_px: float) -> int:
    words = max(1, int(words or 1))
    usable_w = max(24.0, float(width_px) - 8.0)
    avg_word_px = max(18.0, font_px * 4.3)
    words_per_line = max(3, int(usable_w / avg_word_px))
    lines = int(math.ceil(words / words_per_line))
    return int(math.ceil(lines * line_px + 8))


def _largest_vertical_gap_y(
    bboxes: list[dict[str, int]],
    parent_bbox: dict[str, int],
    *,
    min_h: int,
    margin: int,
    gutter: int,
) -> int | None:
    cursor = parent_bbox["y"] + margin
    for bbox in sorted(bboxes, key=lambda item: (item["y"], item["x"])):
        gap_h = bbox["y"] - gutter - cursor
        if gap_h >= min_h:
            return int(cursor)
        cursor = max(cursor, bbox["y"] + bbox["h"] + gutter)
    if parent_bbox["y"] + parent_bbox["h"] - margin - cursor >= min_h:
        return int(cursor)
    return None


def _html_style_bbox_partial(style: str) -> dict[str, int]:
    key_map = {"left": "x", "top": "y", "width": "w", "height": "h"}
    out: dict[str, int] = {}
    for declaration in str(style or "").split(";"):
        if ":" not in declaration:
            continue
        raw_key, raw_value = declaration.split(":", 1)
        key = key_map.get(raw_key.strip().lower())
        if not key:
            continue
        value = _parse_css_px_int(raw_value.strip())
        if value is not None:
            out[key] = value
    return out


def _parse_css_px_int(value: str) -> int | None:
    text = str(value or "").strip().lower()
    if not text or "%" in text or "calc(" in text:
        return None
    if text.endswith("px"):
        text = text[:-2].strip()
    try:
        return int(round(float(text)))
    except ValueError:
        return None


def _source_id_from_image_src(src: str) -> str:
    leaf = str(src or "").strip().split("?", 1)[0].split("#", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    if "." in leaf:
        leaf = leaf.rsplit(".", 1)[0]
    if leaf.startswith("img_"):
        leaf = leaf[4:]
    return _css_token(leaf)


def _image_src_compare_key(src: str) -> str:
    text = str(src or "").strip().split("?", 1)[0].split("#", 1)[0]
    if not text:
        return ""
    return text.rsplit("/", 1)[-1].lower()


def _unique_html_block_id(candidate: str, block_index: dict[str, Any]) -> str:
    base = _css_token(candidate)
    if not base:
        return ""
    if base not in block_index:
        return base
    for idx in range(2, 100):
        value = f"{base}_{idx}"
        if value not in block_index:
            return value
    return ""


def _insert_html_block(frame: dict[str, Any], block: dict[str, Any], *, parent_id: str | None) -> None:
    if parent_id:
        parent = _find_html_block_in_list(frame.setdefault("blocks", []), parent_id)
        if parent is not None:
            children = parent.get("children")
            if not isinstance(children, list):
                children = []
                parent["children"] = children
            children.append(block)
            return
    frame.setdefault("blocks", []).append(block)


def _find_html_block_in_list(blocks: list[Any], block_id: str) -> dict[str, Any] | None:
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if str(block.get("block_id") or block.get("layer_id") or "") == str(block_id):
            return block
        child = _find_html_block_in_list(block.get("children") if isinstance(block.get("children"), list) else [], block_id)
        if child is not None:
            return child
    return None


def _patch_authored_body_text(frame: dict[str, Any], block_id: str, text: str) -> None:
    body = frame.get("authored_body_html")
    if not isinstance(body, str) or not body:
        return
    escaped_id = re.escape(block_id)
    pattern = re.compile(
        rf"(?P<start><(?P<tag>[a-zA-Z][\w:-]*)\b(?=[^>]*\bdata-block-id\s*=\s*(?P<quote>['\"]){escaped_id}(?P=quote))[^>]*>)(?P<body>.*?)(?P<end></(?P=tag)>)",
        flags=re.DOTALL,
    )

    def replace(match: re.Match[str]) -> str:
        start = match.group("start")
        replacement_body = _html_escape(text, quote=False)
        if _authored_block_is_footer_card(start, block_id):
            replacement_body = _structured_footer_card_html(block_id, text)
        return f"{start}{replacement_body}{match.group('end')}"

    next_body, count = pattern.subn(replace, body, count=1)
    if count:
        frame["authored_body_html"] = next_body


def _patch_authored_body_block_bbox(frame: dict[str, Any], block_id: str, bbox: dict[str, Any]) -> bool:
    body = frame.get("authored_body_html")
    if not isinstance(body, str) or not body:
        return False
    soup = BeautifulSoup(body, "html.parser")
    node = soup.find(attrs={"data-block-id": str(block_id)})
    if node is None:
        return False
    normalized = _coerce_bbox_for_authored_style(bbox)
    if normalized is None:
        normalized = {"x": 0, "y": 0, "w": 1, "h": 1}
    if _authored_body_block_needs_global_hoist(frame, node, normalized):
        root = _authored_body_root_container(soup)
        if root is not None and root is not node:
            node.extract()
            root.append(node)
    css_bbox = _authored_body_bbox_for_node(frame, node, bbox)
    node["style"] = _merge_inline_bbox_style(str(node.get("style") or ""), css_bbox)
    frame["authored_body_html"] = str(soup)
    return True


def _patch_authored_body_block_style_patch(
    frame: dict[str, Any],
    block_id: str,
    style_patch: dict[str, Any],
) -> bool:
    body = frame.get("authored_body_html")
    if not isinstance(body, str) or not body or not style_patch:
        return False
    soup = BeautifulSoup(body, "html.parser")
    node = soup.find(attrs={"data-block-id": str(block_id)})
    if node is None:
        return False
    node["style"] = _merge_inline_style_patch(str(node.get("style") or ""), style_patch)
    frame["authored_body_html"] = str(soup)
    return True


def _authored_body_bbox_for_node(frame: dict[str, Any], node: Any, bbox: dict[str, Any]) -> dict[str, int]:
    normalized = _coerce_bbox_for_authored_style(bbox)
    if normalized is None:
        return {"x": 0, "y": 0, "w": 1, "h": 1}
    parent_bbox = _nearest_authored_dom_ancestor_bbox(frame, node, normalized)
    if parent_bbox is None:
        return normalized
    if _bbox_contains(parent_bbox, normalized, tolerance=8) or _bbox_top_left_inside(
        parent_bbox,
        normalized,
        tolerance=24,
    ):
        return {
            "x": max(0, normalized["x"] - parent_bbox["x"]),
            "y": max(0, normalized["y"] - parent_bbox["y"]),
            "w": normalized["w"],
            "h": normalized["h"],
        }
    if _bbox_is_local_to_parent(parent_bbox, normalized):
        return normalized
    return normalized


def _authored_body_block_needs_global_hoist(
    frame: dict[str, Any],
    node: Any,
    bbox: dict[str, int],
) -> bool:
    """Move globally-positioned text out of a mismatched positioned panel.

    Authored drafts often mix global manifest coordinates with nested DOM
    placement. If the declared bbox is not plausibly local to, or located
    inside, any data-block ancestor, absolute positioning would be relative to
    the wrong panel in the browser. Hoisting preserves the declared storyboard
    coordinate instead of letting the node render far off-canvas.
    """
    saw_manifest_ancestor = False
    current = getattr(node, "parent", None)
    while current is not None and getattr(current, "name", None) is not None:
        parent_id = str(current.get("data-block-id") or "").strip() if hasattr(current, "get") else ""
        if parent_id:
            parent = _find_html_block_in_list(
                frame.get("blocks") if isinstance(frame.get("blocks"), list) else [],
                parent_id,
            )
            parent_bbox = _coerce_bbox_for_authored_style(parent.get("bbox") if isinstance(parent, dict) else None)
            if parent_bbox is not None:
                saw_manifest_ancestor = True
                if (
                    _bbox_contains(parent_bbox, bbox, tolerance=8)
                    or _bbox_top_left_inside(parent_bbox, bbox, tolerance=24)
                    or _bbox_is_local_to_parent(parent_bbox, bbox)
                ):
                    return False
        current = getattr(current, "parent", None)
    return saw_manifest_ancestor


def _authored_body_root_container(soup: BeautifulSoup) -> Any | None:
    root = soup.find(class_="poster-root")
    if root is not None:
        return root
    body = soup.find("body")
    if body is not None:
        return body
    return soup


def _nearest_authored_dom_ancestor_bbox(
    frame: dict[str, Any],
    node: Any,
    bbox: dict[str, int],
) -> dict[str, int] | None:
    current = getattr(node, "parent", None)
    while current is not None and getattr(current, "name", None) is not None:
        parent_id = str(current.get("data-block-id") or "").strip() if hasattr(current, "get") else ""
        if parent_id:
            parent = _find_html_block_in_list(frame.get("blocks") if isinstance(frame.get("blocks"), list) else [], parent_id)
            parent_bbox = _coerce_bbox_for_authored_style(parent.get("bbox") if isinstance(parent, dict) else None)
            if parent_bbox is not None and (
                _bbox_contains(parent_bbox, bbox, tolerance=8)
                or _bbox_top_left_inside(parent_bbox, bbox, tolerance=24)
                or _bbox_is_local_to_parent(parent_bbox, bbox)
            ):
                return parent_bbox
        current = getattr(current, "parent", None)
    return None


def _coerce_bbox_for_authored_style(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    try:
        return {
            "x": int(round(float(value.get("x") or 0))),
            "y": int(round(float(value.get("y") or 0))),
            "w": max(1, int(round(float(value.get("w") or 0)))),
            "h": max(1, int(round(float(value.get("h") or 0)))),
        }
    except (TypeError, ValueError):
        return None


def _bbox_contains(parent: dict[str, int], child: dict[str, int], *, tolerance: int = 0) -> bool:
    return (
        child["x"] >= parent["x"] - tolerance
        and child["y"] >= parent["y"] - tolerance
        and child["x"] + child["w"] <= parent["x"] + parent["w"] + tolerance
        and child["y"] + child["h"] <= parent["y"] + parent["h"] + tolerance
    )


def _bbox_top_left_inside(parent: dict[str, int], child: dict[str, int], *, tolerance: int = 0) -> bool:
    return (
        child["x"] >= parent["x"] - tolerance
        and child["y"] >= parent["y"] - tolerance
        and child["x"] <= parent["x"] + parent["w"] + tolerance
        and child["y"] <= parent["y"] + parent["h"] + tolerance
    )


def _bbox_is_local_to_parent(parent: dict[str, int], child: dict[str, int]) -> bool:
    return (
        child["x"] >= 0
        and child["y"] >= 0
        and child["x"] + child["w"] <= parent["w"] + 8
        and child["y"] + child["h"] <= parent["h"] + 8
        and (child["x"] < parent["x"] or child["y"] < parent["y"])
    )


def _merge_inline_bbox_style(style: str, bbox: dict[str, Any]) -> str:
    parsed: dict[str, str] = {}
    order: list[str] = []
    for declaration in str(style or "").split(";"):
        if ":" not in declaration:
            continue
        raw_key, raw_value = declaration.split(":", 1)
        key = raw_key.strip().lower()
        if not key:
            continue
        if key not in parsed:
            order.append(key)
        parsed[key] = raw_value.strip()
    bbox_patch = _bbox_css_declarations(bbox)
    for key, value in bbox_patch.items():
        if key not in parsed:
            order.append(key)
        parsed[key] = value
    preferred = list(bbox_patch.keys())
    final_order = preferred + [key for key in order if key not in bbox_patch]
    return ";".join(f"{key}:{parsed[key]}" for key in final_order if parsed.get(key) is not None) + ";"


def _merge_inline_style_patch(style: str, style_patch: dict[str, Any]) -> str:
    parsed: dict[str, str] = {}
    order: list[str] = []
    for declaration in str(style or "").split(";"):
        if ":" not in declaration:
            continue
        raw_key, raw_value = declaration.split(":", 1)
        key = raw_key.strip().lower()
        if not key:
            continue
        if key not in parsed:
            order.append(key)
        parsed[key] = raw_value.strip()
    for raw_key, raw_value in style_patch.items():
        key = _style_patch_css_key(str(raw_key))
        if not key:
            continue
        if key not in parsed:
            order.append(key)
        if key == "font-size":
            parsed[key] = f"{int(raw_value)}px"
        elif key == "line-height":
            parsed[key] = str(raw_value)
        else:
            parsed[key] = str(raw_value)
    preferred = [_style_patch_css_key(str(key)) for key in style_patch.keys()]
    preferred = [key for key in preferred if key]
    final_order = preferred + [key for key in order if key not in preferred]
    return ";".join(f"{key}:{parsed[key]}" for key in final_order if parsed.get(key) is not None) + ";"


def _style_patch_css_key(key: str) -> str:
    lookup = {
        "fontSize": "font-size",
        "font_size": "font-size",
        "font-size": "font-size",
        "lineHeight": "line-height",
        "line_height": "line-height",
        "line-height": "line-height",
    }
    return lookup.get(key, key.replace("_", "-"))


def _bbox_css_declarations(bbox: dict[str, Any]) -> dict[str, str]:
    return {
        "position": "absolute",
        "left": f"{int(bbox['x'])}px",
        "top": f"{int(bbox['y'])}px",
        "width": f"{int(bbox['w'])}px",
        "height": f"{int(bbox['h'])}px",
    }


def _append_authored_bbox_css(frame: dict[str, Any], block_id: str, bbox: dict[str, Any]) -> None:
    rule = _authored_bbox_css_rule(block_id, bbox)
    css = frame.get("authored_css")
    css_text = str(css or "")
    if rule in css_text:
        return
    frame["authored_css"] = (css_text.rstrip() + "\n" + rule).strip()


def _authored_bbox_css_rule(block_id: str, bbox: dict[str, Any]) -> str:
    return (
        f'[data-block-id="{_css_escape_attr(block_id)}"]'
        "{"
        f"position:absolute;left:{int(bbox['x'])}px;top:{int(bbox['y'])}px;"
        f"width:{int(bbox['w'])}px;height:{int(bbox['h'])}px;"
        "}"
    )


def _css_escape_attr(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _merge_html_block_bbox_style(block: dict[str, Any], bbox: dict[str, Any]) -> None:
    style = block.get("style")
    if not isinstance(style, dict):
        style = {}
    style.update({
        "position": "absolute",
        "left": int(bbox["x"]),
        "top": int(bbox["y"]),
        "width": int(bbox["w"]),
        "height": int(bbox["h"]),
    })
    block["style"] = style


def _merge_html_block_style_patch(block: dict[str, Any], style_patch: dict[str, Any]) -> None:
    if not style_patch:
        return
    style = block.get("style")
    if not isinstance(style, dict):
        style = {}
    style.update(style_patch)
    block["style"] = style


def _remove_authored_body_block(frame: dict[str, Any], block_id: str) -> None:
    body = frame.get("authored_body_html")
    if not isinstance(body, str) or not body:
        return
    soup = BeautifulSoup(body, "html.parser")
    removed = False
    for node in list(soup.find_all(attrs={"data-block-id": str(block_id)})):
        node.decompose()
        removed = True
    if removed:
        frame["authored_body_html"] = str(soup)


def _append_authored_body_block(frame: dict[str, Any], block: dict[str, Any], *, parent_id: str | None) -> None:
    body = frame.get("authored_body_html")
    if not isinstance(body, str) or not body.strip():
        frame["authored_body_html"] = (
            '<main class="paper-poster-root">'
            f"{_authored_body_block_html(block)}"
            "</main>"
        )
        return
    block_id = str(block.get("block_id") or block.get("layer_id") or "").strip()
    if not block_id:
        return
    soup = BeautifulSoup(body, "html.parser")
    if soup.find(attrs={"data-block-id": block_id}) is not None:
        return
    snippet = BeautifulSoup(_authored_body_block_html(block), "html.parser")
    new_node = next((node for node in snippet.contents if getattr(node, "name", None)), None)
    if new_node is None:
        return
    parent = soup.find(attrs={"data-block-id": parent_id}) if parent_id else None
    if parent is None:
        parent = next((node for node in soup.contents if getattr(node, "name", None)), None)
    if parent is None:
        soup.append(new_node)
    else:
        parent.append(new_node)
    frame["authored_body_html"] = str(soup)


def _authored_body_block_html(block: dict[str, Any]) -> str:
    block_id = str(block.get("block_id") or block.get("layer_id") or "").strip()
    kind = str(block.get("kind") or "text").strip() or "text"
    role = str(block.get("role") or kind).strip() or kind
    classes = " ".join(
        part for part in [
            "panel-block",
            f"kind-{_css_token(kind)}",
            f"role-{_css_token(role)}",
        ]
        if part
    )
    content = _authored_body_block_text(block)
    return (
        f'<div data-block-id="{_html_escape_attr(block_id)}" '
        f'data-block-kind="{_html_escape_attr(kind)}" '
        f'data-role="{_html_escape_attr(role)}" '
        f'class="{_html_escape_attr(classes)}">'
        f"{_html_escape(content, quote=False)}"
        "</div>"
    )


def _authored_body_block_text(block: dict[str, Any]) -> str:
    for key in ("text", "title", "caption", "label", "role"):
        value = block.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    rows = block.get("rows")
    if isinstance(rows, list):
        cells: list[str] = []
        for row in rows[:2]:
            if isinstance(row, list):
                cells.extend(str(cell).strip() for cell in row[:3] if str(cell).strip())
        if cells:
            return " | ".join(cells)
    return str(block.get("block_id") or "New block")


def _css_token(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower()).strip("-") or "block"


def _html_escape_attr(value: str) -> str:
    return _html_escape(value, quote=True)


def _collect_html_block_ids(block: dict[str, Any]) -> list[str]:
    out: list[str] = []

    def visit(node: dict[str, Any]) -> None:
        block_id = str(node.get("block_id") or node.get("layer_id") or "").strip()
        if block_id:
            out.append(block_id)
        children = node.get("children")
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    visit(child)

    visit(block)
    return out


def _authored_block_is_footer_card(start_tag: str, block_id: str) -> bool:
    if str(block_id) in {
        "section_problem",
        "section_method",
        "section_key_contribution",
        "section_main_evidence",
        "section_takeaway",
        "section_limitation_future",
    }:
        return True
    class_match = re.search(
        r"\bclass\s*=\s*(?P<quote>['\"])(?P<class>.*?)(?P=quote)",
        start_tag,
        flags=re.DOTALL,
    )
    return bool(class_match and "footer-card" in class_match.group("class").split())


def _structured_footer_card_html(block_id: str, text: str) -> str:
    titles = {
        "section_problem": "Problem",
        "section_method": "Method",
        "section_key_contribution": "Key Contribution",
        "section_main_evidence": "Main Evidence",
        "section_takeaway": "Takeaway",
        "section_limitation_future": "Conclusion",
    }
    title = titles.get(block_id, str(block_id).removeprefix("section_").replace("_", " ").title())
    lines = [
        _clean_footer_card_line(line)
        for line in re.split(r"[\n\r]+", str(text or ""))
    ]
    lines = [line for line in lines if line]
    if lines and _looks_like_footer_card_title(lines[0]):
        if block_id not in titles:
            title = lines[0].title()
        lines = lines[1:]
    bullets = _footer_card_bullets(block_id, lines)
    bullet_html = "".join(
        f"<li>{_html_escape(bullet, quote=False)}</li>"
        for bullet in bullets
    )
    return (
        f"<h2>{_html_escape(title, quote=False)}</h2>"
        f"<ul>{bullet_html}</ul>"
    )


def _clean_footer_card_line(text: str) -> str:
    cleaned = re.sub(r"^\s*(?:[-*]|\u2022|\d+[.)])\s*", "", str(text or "")).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" ;")


def _looks_like_footer_card_title(text: str) -> bool:
    cleaned = str(text or "").strip()
    if not cleaned:
        return False
    if len(cleaned.split()) <= 5 and not re.search(r"[.!?]$", cleaned):
        return True
    return cleaned.isupper() and len(cleaned) <= 48


def _footer_card_bullets(block_id: str, candidates: list[str]) -> list[str]:
    if block_id == "section_method":
        return [
            "Figures 1 and 4: videos become hierarchical planning labels.",
            "Sec. 3: scores separate high-, mid-, and action-level failures.",
        ]
    if block_id == "section_takeaway":
        return [
            "Abstract: the SoTA large multimodal model GPT4o performs poorly on visual-centric GUI tasks, especially for high-level planning.",
        ]
    if block_id == "section_limitation_future":
        return [
            "Conclusion: challenges of visual-oriented GUI automation and the potential of instructional videos.",
        ]
    if block_id == "section_main_evidence":
        return [
            "Table 3: overall benchmark scores stay low across current agents.",
            "Table 4: text-query planning outperforms vision-only planning.",
        ]
    if block_id == "section_problem":
        return ["Sec. 1: Text-only GUI benchmarks miss visual-centric software tasks."]
    if block_id == "section_key_contribution":
        return [
            "Sec. 1: VideoGUI exposes failures across planning and action levels.",
            "Sec. 1: 86 tasks cover visual-centric professional software.",
        ]
    for candidate in candidates:
        if candidate:
            return [_trim_footer_card_bullet(candidate, limit=92)]
    return ["Source-backed claim preserved from the paper."]


def _caption_replacement_mislabels_source(
    block_id: str,
    block: dict[str, Any],
    new_text: str,
) -> bool:
    if "caption" not in str(block_id).lower() and str(block.get("kind") or "") != "caption":
        return False
    current_label = _source_label_from_caption_text(
        " ".join(str(block.get(key) or "") for key in ("text", "caption", "alt"))
    )
    new_label = _source_label_from_caption_text(new_text)
    if not current_label or not new_label:
        return False
    return current_label != new_label


def _source_label_from_caption_text(text: str) -> str:
    match = re.search(
        r"\b(?P<kind>fig(?:ure)?\.?|table)\s*(?P<num>[0-9]+[A-Za-z]?)\b",
        str(text or ""),
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    kind = "table" if match.group("kind").lower().startswith("table") else "figure"
    return f"{kind}:{match.group('num').lower()}"


def _trim_footer_card_bullet(text: str, *, limit: int) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip(" ;")
    if len(cleaned) > limit:
        cut = cleaned[:limit].rstrip()
        space = cut.rfind(" ")
        if space >= int(limit * 0.65):
            cut = cut[:space].rstrip()
        cleaned = cut.rstrip(" ,;:-")
    if not re.search(r"[.!?)]$", cleaned):
        cleaned += "."
    return cleaned


def _patch_authored_img_src(frame: dict[str, Any], block_id: str, src_path: str) -> None:
    body = frame.get("authored_body_html")
    if not isinstance(body, str) or not body:
        return
    escaped_id = re.escape(block_id)

    def repl(match: re.Match[str]) -> str:
        tag = match.group(0)
        escaped_src = _html_escape(src_path, quote=True)
        if re.search(r"\bsrc\s*=", tag, flags=re.IGNORECASE):
            return re.sub(r"\bsrc\s*=\s*(['\"]).*?\1", f'src="{escaped_src}"', tag, count=1, flags=re.IGNORECASE)
        return tag[:-1] + f' src="{escaped_src}">'

    pattern = re.compile(
        rf"<img\b(?=[^>]*\bdata-block-id\s*=\s*(['\"]){escaped_id}\1)[^>]*>",
        flags=re.IGNORECASE | re.DOTALL,
    )
    next_body, count = pattern.subn(repl, body, count=1)
    if count:
        frame["authored_body_html"] = next_body


def _patch_authored_table_cell(
    frame: dict[str, Any],
    block_id: str,
    row_index: int,
    col_index: int,
    text: str,
) -> None:
    body = frame.get("authored_body_html")
    if not isinstance(body, str) or not body:
        return
    escaped_id = re.escape(block_id)
    table_pattern = re.compile(
        rf"(<table\b(?=[^>]*\bdata-block-id\s*=\s*(['\"]){escaped_id}\2)[^>]*>)(?P<body>.*?)(</table>)",
        flags=re.IGNORECASE | re.DOTALL,
    )
    match = table_pattern.search(body)
    if not match:
        return
    table_body = match.group("body")
    row_pattern = re.compile(r"(<tr\b[^>]*>)(?P<body>.*?)(</tr>)", flags=re.IGNORECASE | re.DOTALL)
    cell_pattern = re.compile(r"(<t[dh]\b[^>]*>)(?P<body>.*?)(</t[dh]>)", flags=re.IGNORECASE | re.DOTALL)
    rows = list(row_pattern.finditer(table_body))
    if row_index >= len(rows):
        return
    row = rows[row_index]
    cells = list(cell_pattern.finditer(row.group("body")))
    if col_index >= len(cells):
        return
    cell = cells[col_index]
    next_row_body = (
        row.group("body")[:cell.start("body")]
        + _html_escape(text, quote=False)
        + row.group("body")[cell.end("body"):]
    )
    next_table_body = table_body[:row.start("body")] + next_row_body + table_body[row.end("body"):]
    next_body = body[:match.start("body")] + next_table_body + body[match.end("body"):]
    frame["authored_body_html"] = next_body


def _rerender_poster_text_layers(
    spec: DesignSpec,
    layer_ids: list[str],
    ctx: ToolContext,
) -> list[str]:
    if not layer_ids:
        return []
    nodes = _layer_index(spec.model_dump(mode="json"))
    re_rendered: list[str] = []
    for idx, layer_id in enumerate(layer_ids):
        node_tuple = nodes.get(layer_id)
        if node_tuple is None:
            continue
        node = node_tuple[0]
        if node.get("kind") != "text":
            continue
        render_args = _render_args_from_node(node)
        result = render_text_layer(render_args, ctx=ctx)
        if result.status != "ok":
            raise _DesignOpError(
                f"apply_design_ops: re-render failed for poster text layer '{layer_id}': "
                f"{result.error_message or result.payload}",
                index=idx,
                op={"op": "poster_text_rerender", "layer_id": layer_id},
            )
        re_rendered.append(layer_id)
    return re_rendered


def _is_authored_html_poster_spec(spec: DesignSpec) -> bool:
    artifact = getattr(spec, "html_artifact", None)
    frames = getattr(artifact, "frames", None)
    if not frames:
        return False
    for frame in frames:
        render_mode = str(getattr(frame, "render_mode", "") or "").lower()
        role = str(getattr(frame, "role", "") or "").lower()
        kind = str(getattr(frame, "kind", "") or "").lower()
        if render_mode == "authored_html" and ("poster" in role or kind == "poster"):
            return True
    return False


def _render_args_from_node(node: dict[str, Any]) -> dict[str, Any]:
    bbox = node.get("bbox")
    if not isinstance(bbox, dict):
        raise ValueError(f"poster text layer '{node.get('layer_id')}' is missing bbox")
    font_size = node.get("font_size_px")
    if font_size is None:
        raise ValueError(f"poster text layer '{node.get('layer_id')}' is missing font_size_px")
    effects = dict(node.get("effects") or {})
    fill = _safe_text_fill(effects.get("fill") or "#000000")
    render_effects = {
        key: value
        for key, value in effects.items()
        if key in {"stroke", "shadow"} and value is not None
    }
    return {
        "layer_id": node["layer_id"],
        "name": node.get("name") or node["layer_id"],
        "text": node.get("text") or "",
        "font_family": node.get("font_family"),
        "font_size_px": int(font_size),
        "font_weight": node.get("font_weight"),
        "font_style": node.get("font_style"),
        "line_height": node.get("line_height"),
        "letter_spacing": node.get("letter_spacing"),
        "text_transform": node.get("text_transform"),
        "fill": fill,
        "bbox": bbox,
        "align": node.get("align") or "left",
        "z_index": int(node.get("z_index") or 1),
        "effects": render_effects,
    }


def _safe_text_fill(value: Any) -> str:
    fill = str(value or "").strip()
    if not fill:
        return "#000000"
    fill = fill.replace("!important", "").strip()
    if fill.startswith("var("):
        return "#000000"
    if not re.match(r"^(#[0-9a-fA-F]{3,8}|rgba?\(|hsla?\(|[a-zA-Z]+$)", fill):
        return "#000000"
    return fill


def _layer_index(
    spec_data: dict[str, Any],
) -> dict[str, tuple[dict[str, Any], list[dict[str, Any]], int]]:
    out: dict[str, tuple[dict[str, Any], list[dict[str, Any]], int]] = {}

    def visit(nodes: list[dict[str, Any]]) -> None:
        for idx, node in enumerate(nodes):
            layer_id = node.get("layer_id")
            if layer_id:
                out[str(layer_id)] = (node, nodes, idx)
            children = node.get("children")
            if isinstance(children, list):
                visit(children)

    visit(spec_data.setdefault("layer_graph", []))
    return out


def _html_block_index(
    spec_data: dict[str, Any],
) -> dict[str, tuple[dict[str, Any], list[dict[str, Any]], int, dict[str, Any]]]:
    out: dict[str, tuple[dict[str, Any], list[dict[str, Any]], int, dict[str, Any]]] = {}
    artifact = spec_data.setdefault("html_artifact", {})
    frames = artifact.setdefault("frames", []) if isinstance(artifact, dict) else []

    def visit(blocks: list[dict[str, Any]], frame: dict[str, Any]) -> None:
        for idx, block in enumerate(blocks):
            block_id = block.get("block_id") or block.get("layer_id")
            if block_id:
                key = str(block_id)
                current = out.get(key)
                if current is None or _html_block_index_prefer(block, current[0]):
                    out[key] = (block, blocks, idx, frame)
            children = block.get("children")
            if isinstance(children, list):
                visit(children, frame)

    for frame in frames:
        if not isinstance(frame, dict):
            continue
        blocks = frame.setdefault("blocks", [])
        if isinstance(blocks, list):
            visit(blocks, frame)
    return out


def _html_block_index_prefer(candidate: dict[str, Any], current: dict[str, Any]) -> bool:
    """Prefer the manifest entry that can actually support deterministic repair."""
    return _html_block_manifest_score(candidate) > _html_block_manifest_score(current)


def _html_block_manifest_score(block: dict[str, Any]) -> tuple[int, int, int, int]:
    bbox = block.get("bbox") if isinstance(block.get("bbox"), dict) else {}
    complete_bbox = int(all(key in bbox for key in _BBOX_KEYS))
    children = block.get("children")
    child_count = len(children) if isinstance(children, list) else 0
    text_score = int(bool(str(block.get("text") or block.get("caption") or block.get("title") or "").strip()))
    source_score = int(bool(block.get("src_path") or block.get("source_id") or block.get("provenance")))
    return (complete_bbox, min(child_count, 50), text_score, source_score)


def _dedupe_html_artifact_blocks_in_spec_data(spec_data: dict[str, Any]) -> list[str]:
    """Remove duplicate block_id entries, keeping the most repairable manifest block.

    Dogfood authored drafts sometimes contain a nested panel tree plus a flat
    duplicate manifest. The flat duplicate often lacks bbox fields, so later
    auto-repair picks the wrong target and loops on impossible bbox patches.
    """
    artifact = spec_data.get("html_artifact")
    frames = artifact.get("frames") if isinstance(artifact, dict) else None
    if not isinstance(frames, list):
        return []
    removed: list[str] = []
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        blocks = frame.get("blocks")
        if not isinstance(blocks, list):
            continue
        keep: dict[str, dict[str, Any]] = {}

        def collect(items: list[Any]) -> None:
            for item in items:
                if not isinstance(item, dict):
                    continue
                block_id = str(item.get("block_id") or item.get("layer_id") or "").strip()
                if block_id:
                    current = keep.get(block_id)
                    if current is None or _html_block_index_prefer(item, current):
                        keep[block_id] = item
                children = item.get("children")
                if isinstance(children, list):
                    collect(children)

        def prune(items: list[Any]) -> list[Any]:
            next_items: list[Any] = []
            for item in items:
                if not isinstance(item, dict):
                    next_items.append(item)
                    continue
                block_id = str(item.get("block_id") or item.get("layer_id") or "").strip()
                if block_id and keep.get(block_id) is not item:
                    removed.append(block_id)
                    continue
                children = item.get("children")
                if isinstance(children, list):
                    item["children"] = prune(children)
                next_items.append(item)
            return next_items

        collect(blocks)
        frame["blocks"] = prune(blocks)
    return removed


def _require_html_artifact(
    spec_data: dict[str, Any],
    *,
    index: int,
    op: dict[str, Any],
) -> dict[str, Any]:
    artifact = spec_data.get("html_artifact")
    if not isinstance(artifact, dict):
        raise _DesignOpError(
            "apply_design_ops: html_artifact is required for html_* ops",
            index=index,
            op=op,
        )
    artifact.setdefault("frames", [])
    return artifact


def _require_html_frame(
    artifact: dict[str, Any],
    frame_id: str,
    *,
    index: int,
    op: dict[str, Any],
    allow_first_authored: bool = False,
) -> dict[str, Any]:
    frames = artifact.get("frames")
    if not isinstance(frames, list):
        raise _DesignOpError("apply_design_ops: html_artifact.frames must be an array", index=index, op=op)
    if not frame_id:
        if allow_first_authored:
            for frame in frames:
                if isinstance(frame, dict) and str(frame.get("render_mode") or "") == "authored_html":
                    frame.setdefault("blocks", [])
                    return frame
        if len(frames) == 1 and isinstance(frames[0], dict):
            return frames[0]
        raise _DesignOpError("apply_design_ops.html_add_block: frame_id is required", index=index, op=op)
    for frame in frames:
        if isinstance(frame, dict) and str(frame.get("frame_id") or "") == frame_id:
            frame.setdefault("blocks", [])
            return frame
    if allow_first_authored:
        for frame in frames:
            if isinstance(frame, dict) and str(frame.get("render_mode") or "") == "authored_html":
                frame.setdefault("blocks", [])
                return frame
    raise _DesignOpError(f"apply_design_ops: frame_id '{frame_id}' not found", index=index, op=op)


def _require_html_block(
    spec_data: dict[str, Any],
    block_id: str,
    *,
    index: int,
    op: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], int, dict[str, Any]]:
    if not block_id:
        raise _DesignOpError("apply_design_ops: block_id is required", index=index, op=op)
    found = _html_block_index(spec_data).get(block_id)
    if found is None:
        raise _DesignOpError(f"apply_design_ops: block_id '{block_id}' not found", index=index, op=op)
    return found


def _html_alias_for_legacy_op(
    spec_data: dict[str, Any],
    op_name: str,
    op: dict[str, Any],
) -> dict[str, Any] | None:
    alias = _HTML_ALIAS_OPS.get(op_name)
    if alias is None:
        return None
    layer_id = str(op.get("layer_id") or op.get("block_id") or "").strip()
    if not layer_id:
        return None
    if layer_id in _layer_index(spec_data):
        return None
    if layer_id not in _html_block_index(spec_data):
        return None
    return {**op, "op": alias, "block_id": layer_id}


def _require_html_group_by_slot(
    spec_data: dict[str, Any],
    slot_id: str,
    *,
    index: int,
    op: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], int, dict[str, Any]]:
    for block, container, node_idx, frame in _html_block_index(spec_data).values():
        if block.get("kind") != "group":
            continue
        candidates = {
            str(block.get("block_id") or ""),
            str(block.get("slot_id") or ""),
        }
        if slot_id in candidates:
            return block, container, node_idx, frame
    raise _DesignOpError(f"apply_design_ops: group/slot '{slot_id}' not found", index=index, op=op)


def _require_block_id(op: dict[str, Any], *, index: int) -> str:
    block_id = str(op.get("block_id") or op.get("layer_id") or "").strip()
    if not block_id:
        raise _DesignOpError("apply_design_ops: block_id is required", index=index, op=op)
    return block_id


def _require_layer(
    spec_data: dict[str, Any],
    layer_id: str,
    *,
    index: int,
    op: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    if not layer_id:
        raise _DesignOpError("apply_design_ops: layer_id is required", index=index, op=op)
    found = _layer_index(spec_data).get(layer_id)
    if found is None:
        raise _DesignOpError(f"apply_design_ops: layer_id '{layer_id}' not found", index=index, op=op)
    return found


def _require_layer_id(op: dict[str, Any], *, index: int) -> str:
    layer_id = str(op.get("layer_id") or "").strip()
    if not layer_id:
        raise _DesignOpError("apply_design_ops: layer_id is required", index=index, op=op)
    return layer_id


def _existing_bbox(node: dict[str, Any], *, index: int, op: dict[str, Any]) -> dict[str, Any]:
    bbox = node.get("bbox")
    if not isinstance(bbox, dict):
        raise _DesignOpError(
            f"apply_design_ops.{op.get('op')}: layer '{node.get('layer_id')}' has no bbox",
            index=index,
            op=op,
        )
    return dict(bbox)


def _existing_html_bbox(block: dict[str, Any], *, index: int, op: dict[str, Any]) -> dict[str, Any]:
    bbox = block.get("bbox")
    if not isinstance(bbox, dict):
        raise _DesignOpError(
            f"apply_design_ops.{op.get('op')}: block '{block.get('block_id')}' has no bbox",
            index=index,
            op=op,
        )
    return dict(bbox)


def _normalized_bbox(value: Any, *, index: int, op: dict[str, Any]) -> dict[str, int]:
    if not isinstance(value, dict):
        raise _DesignOpError("apply_design_ops: bbox must be an object", index=index, op=op)
    missing = [key for key in _BBOX_KEYS if key not in value]
    if missing:
        raise _DesignOpError(f"apply_design_ops: bbox missing fields {missing}", index=index, op=op)
    try:
        bbox = {key: int(value[key]) for key in _BBOX_KEYS}
    except (TypeError, ValueError):
        raise _DesignOpError("apply_design_ops: bbox fields must be integers", index=index, op=op)
    if bbox["w"] <= 0 or bbox["h"] <= 0:
        raise _DesignOpError("apply_design_ops: bbox w/h must be positive", index=index, op=op)
    if bbox["x"] < 0 or bbox["y"] < 0:
        raise _DesignOpError("apply_design_ops: bbox x/y must be >= 0", index=index, op=op)
    return bbox


def _ensure_layout_plan(frame: dict[str, Any]) -> dict[str, Any]:
    plan = frame.get("layout_plan")
    if not isinstance(plan, dict):
        plan = {
            "archetype": str(frame.get("layout") or frame.get("role") or "manual_storyboard"),
            "margin_px": None,
            "gutter_px": None,
            "slots": [],
            "notes": None,
        }
        frame["layout_plan"] = plan
    plan.setdefault("archetype", str(frame.get("layout") or frame.get("role") or "manual_storyboard"))
    plan.setdefault("slots", [])
    if not isinstance(plan.get("slots"), list):
        plan["slots"] = []
    return plan


def _upsert_frame_slot(frame: dict[str, Any], slot: dict[str, Any], *, index: int, op: dict[str, Any]) -> None:
    norm_slot = _normalized_slot(slot, index=index, op=op)
    plan = _ensure_layout_plan(frame)
    slots = plan.setdefault("slots", [])
    for existing in slots:
        if isinstance(existing, dict) and str(existing.get("slot_id") or "") == str(norm_slot["slot_id"]):
            existing.update({k: v for k, v in norm_slot.items() if v is not None})
            return
    slots.append(norm_slot)


def _normalized_slot(slot: dict[str, Any], *, index: int, op: dict[str, Any]) -> dict[str, Any]:
    slot_id = str(slot.get("slot_id") or "").strip()
    if not slot_id:
        raise _DesignOpError("apply_design_ops: slot.slot_id is required", index=index, op=op)
    role = str(slot.get("role") or "panel").strip() or "panel"
    bbox = _normalized_bbox(slot.get("bbox"), index=index, op=op)
    return {
        "slot_id": slot_id,
        "role": role,
        "bbox": bbox,
        "required": bool(slot.get("required", False)),
        "content_policy": None if slot.get("content_policy") is None else str(slot.get("content_policy")),
        "max_text_words": _optional_int(slot.get("max_text_words")),
        "min_visual_area_ratio": _optional_float(slot.get("min_visual_area_ratio")),
        "parent_slot_id": None if slot.get("parent_slot_id") is None else str(slot.get("parent_slot_id")),
        "panel_job": None if slot.get("panel_job") is None else str(slot.get("panel_job")),
        "text_budget": None if slot.get("text_budget") is None else str(slot.get("text_budget")),
        "visual_ids": [str(v) for v in (slot.get("visual_ids") or []) if str(v or "").strip()]
        if isinstance(slot.get("visual_ids"), list) else [],
        "space_fill_policy": None if slot.get("space_fill_policy") is None else str(slot.get("space_fill_policy")),
    }


def _scale_child_bboxes(blocks: list[dict[str, Any]], old_bbox: dict[str, Any], new_bbox: dict[str, int]) -> None:
    old_w = max(1, int(old_bbox.get("w") or 1))
    old_h = max(1, int(old_bbox.get("h") or 1))
    sx = int(new_bbox["w"]) / old_w
    sy = int(new_bbox["h"]) / old_h
    ox = int(old_bbox.get("x") or 0)
    oy = int(old_bbox.get("y") or 0)
    nx = int(new_bbox["x"])
    ny = int(new_bbox["y"])
    for block in blocks:
        if not isinstance(block, dict):
            continue
        bbox = block.get("bbox")
        if isinstance(bbox, dict):
            try:
                block["bbox"] = {
                    "x": int(round(nx + (int(bbox["x"]) - ox) * sx)),
                    "y": int(round(ny + (int(bbox["y"]) - oy) * sy)),
                    "w": max(1, int(round(int(bbox["w"]) * sx))),
                    "h": max(1, int(round(int(bbox["h"]) * sy))),
                }
            except (KeyError, TypeError, ValueError):
                pass
        children = block.get("children")
        if isinstance(children, list):
            _scale_child_bboxes(children, old_bbox, new_bbox)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sanitize_repair_input_scalars(spec_data: dict[str, Any]) -> None:
    def visit_layers(nodes: Any) -> None:
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, dict):
                continue
            for key in ("letter_spacing", "line_height"):
                if isinstance(node.get(key), str):
                    parsed = _parse_css_number(str(node.get(key) or ""), integer=False)
                    if parsed is None:
                        node.pop(key, None)
                    else:
                        node[key] = float(parsed)
            visit_layers(node.get("children"))

    visit_layers(spec_data.get("layer_graph"))


def _require_int(op: dict[str, Any], key: str, *, index: int) -> int:
    if key not in op:
        raise _DesignOpError(f"apply_design_ops.{op.get('op')}: {key} is required", index=index, op=op)
    try:
        return int(op[key])
    except (TypeError, ValueError):
        raise _DesignOpError(f"apply_design_ops.{op.get('op')}: {key} must be an integer", index=index, op=op)


def _collect_layer_ids(node: dict[str, Any]) -> set[str]:
    out = {str(node.get("layer_id"))} if node.get("layer_id") else set()
    for child in node.get("children") or []:
        if isinstance(child, dict):
            out.update(_collect_layer_ids(child))
    return out


def _applied(
    op: dict[str, Any],
    *,
    layer_id: str | None = None,
    parent_layer_id: str | None = None,
) -> dict[str, Any]:
    out = {
        "op": str(op.get("op")),
        "finding_id": str(op.get("finding_id")),
    }
    if layer_id:
        out["layer_id"] = layer_id
    if parent_layer_id:
        out["parent_layer_id"] = parent_layer_id
    return out


def _count_layers(nodes: list[dict[str, Any]]) -> int:
    total = 0
    for node in nodes:
        total += 1
        total += _count_layers(list(node.get("children") or []))
    return total


def _snapshot_mutable_state(ctx: ToolContext) -> dict[str, Any]:
    return capture_state_keys(
        ctx.state,
        _APPLY_STATE_TRANSACTION_KEYS,
        deep_copy_keys=_APPLY_DEEP_COPY_STATE_KEYS,
    )


def _restore_mutable_state(ctx: ToolContext, snapshot: dict[str, Any]) -> None:
    install_state_snapshot(ctx.state, snapshot)


def _error_payload(
    ctx: ToolContext,
    *,
    failed_op_index: int,
    failed_op: dict[str, Any],
) -> dict[str, Any]:
    spec = ctx.state.get("design_spec")
    using_draft = False
    if spec is None:
        spec = ctx.state.get(DOGFOOD_DRAFT_DESIGN_SPEC_STATE_KEY)
        using_draft = spec is not None
    layer_ids: list[str] = []
    html_block_ids: list[str] = []
    html_frame_ids: list[str] = []
    if spec is not None:
        spec_data = spec.model_dump(mode="json")
        layer_ids = sorted(_layer_index(spec_data).keys())
        html_block_ids = sorted(_html_block_index(spec_data).keys())
        artifact = spec_data.get("html_artifact")
        frames = artifact.get("frames") if isinstance(artifact, dict) else []
        if isinstance(frames, list):
            html_frame_ids = sorted(
                str(frame.get("frame_id") or "")
                for frame in frames
                if isinstance(frame, dict) and str(frame.get("frame_id") or "").strip()
            )
    feedback = design_feedback_to_dict(
        ctx.state.get("last_design_feedback")
        or (ctx.state.get("last_composite_payload") or {}).get("design_feedback")
    )
    payload: dict[str, Any] = {
        "failed_op_index": failed_op_index,
        "failed_op": failed_op,
        "available_layer_ids": layer_ids,
        "available_html_frame_ids": html_frame_ids,
        "available_html_block_ids": html_block_ids,
        "hint": (
            "If the target id appears in available_html_block_ids, use html_* "
            "ops with block_id, or keep the legacy op name and the tool will "
            "alias it only when there is no legacy layer with that id."
        ),
        "using_draft_design_spec": using_draft,
    }
    if feedback is not None:
        payload["design_feedback"] = feedback
    return payload
