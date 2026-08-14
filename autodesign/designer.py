"""DesignerLoop — handwritten tool-use loop, provider-agnostic.

Drives the LLM through propose_design_spec → optional generated imagery /
background tools → render_text_layer* → composite → (critique?) → finalize.

v2.1 (multi-provider): all LLM access goes through `LLMBackend` so the
same loop works with Claude (Anthropic protocol) OR Kimi / DeepSeek /
Doubao / vLLM-served Qwen (OpenAI-compat protocol). The backend handles
the provider-specific quirks: Anthropic's `thinking` blocks with
signatures vs OpenAI-compat's `reasoning_content` string field; tool_use
content blocks vs tool_calls list; etc.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .config import effective_poster_harness_mode
from .llm_backend import LLMBackend, ToolCall, TurnResponse, make_backend
from .schema import ToolResultRecord
from .tools import TOOL_HANDLERS, TOOL_SCHEMAS, ToolContext
from .util.design_feedback import blocking_design_findings
from .util.logging import log
from .util.visual_reference_contract import only_visual_reference_progression_findings

_MAX_REPEATED_VALIDATION_ERRORS = 4
_DOGFOOD_POST_COMPOSITE_THINKING_BUDGET = 4000
_DOGFOOD_DECK_LAYOUT_REPAIR_BUDGET = 2
_DOGFOOD_INLINE_BLOCKING_REPAIR_ENV = "DOGFOOD_INLINE_BLOCKING_REPAIR"


def _is_dogfood_mode(ctx: ToolContext) -> bool:
    return effective_poster_harness_mode(ctx.settings) == "dogfood"


class DesignerLoop:
    """Owns the conversation with the LLM and the trace it produces.

    Does NOT own ctx — that's runner-level state shared across critic too.
    """

    def __init__(self, settings, system_prompt: str):
        self.settings = settings
        self.system_prompt = system_prompt
        self.backend: LLMBackend = make_backend(
            settings, settings.designer_model, role="designer",
        )
        self._total_in = 0
        self._total_out = 0
        self._total_cache_read = 0
        self._total_cache_create = 0

    def run(self, brief: str, ctx: ToolContext) -> None:
        ctx.raise_if_cancelled("designer.start")
        ctx.state.pop("designer_api_error", None)
        ctx.state["runtime_skill_stage"] = (
            "repair" if ctx.state.get("env_repair_attempts") else "plan"
        )
        # `messages` lives in the BACKEND'S native format (Anthropic content
        # blocks vs OpenAI message dicts). Only the backend understands its
        # own layout; we hand it back via append_assistant / append_tool_results.
        messages: list[Any] = [{
            "role": "user",
            "content": _user_prompt(
                brief, claim_graph=ctx.state.get("claim_graph"),
            ),
        }]

        base_thinking_budget = self.settings.designer_thinking_budget
        if base_thinking_budget > 0:
            assert 16384 > base_thinking_budget, (
                f"max_tokens (16384) must exceed designer_thinking_budget ({base_thinking_budget})"
            )

        log("designer.start",
            backend=self.backend.name, model=self.backend.model,
            thinking_budget=base_thinking_budget,
            interleaved=self.settings.enable_interleaved_thinking)

        repeated_validation_signature: tuple[str, str, str] | None = None
        repeated_validation_count = 0
        last_validation_error: dict[str, Any] | None = None
        validation_end_turn_retries = 0
        for turn in range(self.settings.max_designer_turns):
            ctx.raise_if_cancelled("designer.before_model_turn")
            thinking_budget = _designer_thinking_budget_for_ctx(self.settings, ctx)
            if thinking_budget > 0:
                assert 16384 > thinking_budget, (
                    f"max_tokens (16384) must exceed designer_thinking_budget ({thinking_budget})"
                )
            if (
                thinking_budget != base_thinking_budget
                and not ctx.state.get("_designer_thinking_budget_capped_logged")
            ):
                ctx.state["_designer_thinking_budget_capped_logged"] = True
                log(
                    "designer.thinking_budget.capped",
                    from_budget=base_thinking_budget,
                    to_budget=thinking_budget,
                    reason="dogfood_post_composite_repair",
                )
            log("designer.turn", turn=turn + 1, n_messages=len(messages))
            try:
                request_kwargs: dict[str, Any] = {
                    "system": self.system_prompt,
                    "messages": messages,
                    "tools": _tool_schemas_for_context(ctx),
                    "thinking_budget": thinking_budget,
                    "max_tokens": 16384,
                }
                if getattr(ctx.cancellation_token, "can_cancel", True):
                    request_kwargs["cancellation_token"] = ctx.cancellation_token
                resp: TurnResponse = self.backend.create_turn(**request_kwargs)
            except Exception as e:
                log("designer.api_error", turn=turn + 1, error=str(e))
                if _is_auth_or_key_error(e):
                    raise
                ctx.state["designer_api_error"] = {
                    "turn": turn + 1,
                    "error": str(e)[:1000],
                    "error_type": type(e).__name__,
                }
                break

            ctx.raise_if_cancelled("designer.after_model_turn")

            self._total_in += resp.usage.get("input", 0)
            self._total_out += resp.usage.get("output", 0)
            self._total_cache_read += resp.usage.get("cache_read", 0)
            self._total_cache_create += resp.usage.get("cache_create", 0)

            if resp.thinking_blocks:
                log("designer.reasoning",
                    n_blocks=len(resp.thinking_blocks),
                    n_redacted=sum(1 for r in resp.thinking_blocks if r.is_redacted),
                    turn=turn + 1)

            ctx.raise_if_cancelled("designer.before_append_assistant")
            self.backend.append_assistant(messages, resp)
            ctx.raise_if_cancelled("designer.after_append_assistant")

            if not resp.tool_calls:
                if resp.stop_reason == "end_turn":
                    if _should_retry_after_validation_end_turn(
                        ctx,
                        last_validation_error=last_validation_error,
                        retry_count=validation_end_turn_retries,
                    ):
                        ctx.raise_if_cancelled("designer.validation_retry")
                        validation_end_turn_retries += 1
                        retry_prompt = _validation_end_turn_retry_prompt(last_validation_error or {}, ctx=ctx)
                        messages.append({"role": "user", "content": retry_prompt})
                        ctx.state["designer_validation_end_turn_retry"] = {
                            "turn": turn + 1,
                            "retry_count": validation_end_turn_retries,
                            "tool": (last_validation_error or {}).get("tool"),
                            "issue_id": (last_validation_error or {}).get("issue_id"),
                        }
                        log(
                            "designer.validation_end_turn_retry",
                            turn=turn + 1,
                            retry_count=validation_end_turn_retries,
                            tool=(last_validation_error or {}).get("tool"),
                            issue_id=(last_validation_error or {}).get("issue_id"),
                        )
                        continue
                    log("designer.end_turn", turn=turn + 1)
                    break
                log("designer.unexpected_stop", turn=turn + 1, stop_reason=resp.stop_reason)
                break

            tool_results_for_api: list[tuple[str, str, bool]] = []
            abort_after_tools = False
            for tc in resp.tool_calls:
                ctx.raise_if_cancelled("designer.before_tool")
                result = self._invoke(tc.name, tc.input, ctx)
                ctx.raise_if_cancelled("designer.after_tool")
                tool_results_for_api.append((
                    tc.id,
                    json.dumps(result.model_dump(), ensure_ascii=False),
                    result.status == "error",
                ))
                if result.status == "error" and result.error_category == "validation":
                    result_payload = result.payload if isinstance(result.payload, dict) else {}
                    last_validation_error = {
                        "tool": tc.name,
                        "error_message": (result.error_message or "")[:1200],
                        "issue_id": str(result_payload.get("issue_id") or ""),
                        "repair_route": str(result_payload.get("repair_route") or ""),
                        "hint": str(result_payload.get("hint") or "")[:1200],
                        "local_repair_hint": str(result_payload.get("local_repair_hint") or "")[:1200],
                        "candidate_id": str(result_payload.get("candidate_id") or ""),
                        "candidate_relative_dir": str(result_payload.get("candidate_relative_dir") or ""),
                        "candidate_preview_png": str(
                            result_payload.get("candidate_preview_png_relative")
                            or result_payload.get("candidate_preview_png")
                            or ""
                        ),
                        "candidate_measurement_json": str(
                            result_payload.get("candidate_measurement_json_relative")
                            or result_payload.get("candidate_measurement_json")
                            or ""
                        ),
                        "locked_base_candidate_id": str(result_payload.get("locked_base_candidate_id") or ""),
                        "locked_base_candidate_relative_dir": str(result_payload.get("locked_base_candidate_relative_dir") or ""),
                        "locked_base_candidate_preview_png": str(result_payload.get("locked_base_candidate_preview_png") or ""),
                        "locked_base_candidate_measurement_json": str(result_payload.get("locked_base_candidate_measurement_json") or ""),
                        "locked_base_candidate_score": str(result_payload.get("locked_base_candidate_score") or ""),
                        "issues": (
                            result_payload.get("issues")[:4]
                            if isinstance(result_payload.get("issues"), list)
                            else []
                        ),
                    }
                    signature = _validation_error_signature(tc.name, result)
                    if signature == repeated_validation_signature:
                        repeated_validation_count += 1
                    else:
                        repeated_validation_signature = signature
                        repeated_validation_count = 1
                    if repeated_validation_count >= _MAX_REPEATED_VALIDATION_ERRORS:
                        ctx.state["designer_validation_loop_abort"] = {
                            "turn": turn + 1,
                            "tool": tc.name,
                            "error_category": result.error_category,
                            "error_message": (result.error_message or "")[:1000],
                            "repeat_count": repeated_validation_count,
                        }
                        log(
                            "designer.validation_loop_abort",
                            turn=turn + 1,
                            tool=tc.name,
                            repeat_count=repeated_validation_count,
                            error_message=(result.error_message or "")[:800],
                        )
                        abort_after_tools = True
                elif result.status != "error":
                    repeated_validation_signature = None
                    repeated_validation_count = 0
                    last_validation_error = None
                    validation_end_turn_retries = 0
                if _should_stop_after_dogfood_blocking_composite(tc.name, result, ctx):
                    abort_after_tools = True
                result_payload = result.payload if isinstance(result.payload, dict) else {}
                if result_payload.get("dogfood_terminal_critic"):
                    abort_after_tools = True
                if ctx.state.get("designer_contract_abort"):
                    abort_after_tools = True

            ctx.raise_if_cancelled("designer.before_append_tool_results")
            self.backend.append_tool_results(messages, tool_results_for_api)
            ctx.raise_if_cancelled("designer.after_append_tool_results")
            if abort_after_tools:
                break

            if ctx.state.get("finalized"):
                log("designer.finalized", turn=turn + 1)
                break

        return None

    def _invoke(self, name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResultRecord:
        return invoke_designer_tool(name, args, ctx)

    @property
    def token_totals(self) -> tuple[int, int]:
        return self._total_in, self._total_out

    @property
    def cache_totals(self) -> tuple[int, int]:
        return self._total_cache_read, self._total_cache_create


def invoke_designer_tool(
    name: str,
    args: dict[str, Any],
    ctx: ToolContext,
    *,
    handlers: Any = None,
) -> ToolResultRecord:
    """Invoke a designer tool through the same dogfood/finalize gates as DesignerLoop."""

    ctx.raise_if_cancelled(f"tool.{name}.before_start")
    registry = TOOL_HANDLERS if handlers is None else handlers
    try:
        handler = registry[name]
    except KeyError:
        handler = None
    if handler is None:
        log("tool.call", tool=name, status="unknown", **_tool_arg_shape(args))
        result = ToolResultRecord(
            status="error",
            error_message=f"unknown tool: {name}",
            error_category="validation",
            payload={"available_tools": sorted(registry)},
        )
        return _checked_tool_result(ctx, name, result)
    log("tool.call", tool=name, **_tool_arg_shape(args))
    authored_route_blocker = _dogfood_authored_poster_route_tool_blocker(name, ctx)
    if authored_route_blocker is not None:
        ctx.raise_if_cancelled(f"tool.{name}.before_result_log")
        log(
            "tool.result",
            tool=name,
            status=authored_route_blocker.status,
            error_category=authored_route_blocker.error_category,
            error_message=(authored_route_blocker.error_message or "")[:800],
        )
        return _checked_tool_result(ctx, name, authored_route_blocker)
    awaiting_authored_spec_blocker = _dogfood_awaiting_authored_spec_tool_blocker(name, ctx)
    if awaiting_authored_spec_blocker is not None:
        ctx.raise_if_cancelled(f"tool.{name}.before_result_log")
        log(
            "tool.result",
            tool=name,
            status=awaiting_authored_spec_blocker.status,
            error_category=awaiting_authored_spec_blocker.error_category,
            error_message=(awaiting_authored_spec_blocker.error_message or "")[:800],
        )
        return _checked_tool_result(ctx, name, awaiting_authored_spec_blocker)
    html_first_legacy_blocker = _dogfood_authored_html_legacy_layer_tool_blocker(name, ctx)
    if html_first_legacy_blocker is not None:
        ctx.raise_if_cancelled(f"tool.{name}.before_result_log")
        log(
            "tool.result",
            tool=name,
            status=html_first_legacy_blocker.status,
            error_category=html_first_legacy_blocker.error_category,
            error_message=(html_first_legacy_blocker.error_message or "")[:800],
        )
        return _checked_tool_result(ctx, name, html_first_legacy_blocker)
    visual_reference_blocker = _dogfood_visual_reference_tool_blocker(name, ctx)
    if visual_reference_blocker is not None:
        ctx.raise_if_cancelled(f"tool.{name}.before_result_log")
        log(
            "tool.result",
            tool=name,
            status=visual_reference_blocker.status,
            error_category=visual_reference_blocker.error_category,
            error_message=(visual_reference_blocker.error_message or "")[:800],
        )
        return _checked_tool_result(ctx, name, visual_reference_blocker)
    finalize_blocker = _dogfood_finalize_tool_blocker(name, ctx)
    if finalize_blocker is not None:
        ctx.raise_if_cancelled(f"tool.{name}.before_result_log")
        log(
            "tool.result",
            tool=name,
            status=finalize_blocker.status,
            error_category=finalize_blocker.error_category,
            error_message=(finalize_blocker.error_message or "")[:800],
        )
        return _checked_tool_result(ctx, name, finalize_blocker)
    terminal_blocker = _dogfood_max_critique_terminal_blocker(name, ctx)
    if terminal_blocker is not None:
        ctx.raise_if_cancelled(f"tool.{name}.before_result_log")
        log(
            "tool.result",
            tool=name,
            status=terminal_blocker.status,
            error_category=terminal_blocker.error_category,
            error_message=(terminal_blocker.error_message or "")[:800],
        )
        return _checked_tool_result(ctx, name, terminal_blocker)
    try:
        result = handler(args, ctx=ctx)
        ctx.raise_if_cancelled(f"tool.{name}.after_handler")
        log_kwargs: dict[str, Any] = {"tool": name, "status": result.status}
        if result.status == "error":
            log_kwargs["error_category"] = result.error_category
            log_kwargs["error_message"] = (result.error_message or "")[:800]
        log("tool.result", **log_kwargs)
        return _checked_tool_result(ctx, name, result)
    except Exception as e:
        log("tool.exception", tool=name, error=str(e))
        result = ToolResultRecord(
            status="error",
            error_message=f"tool '{name}' raised: {type(e).__name__}: {e}",
            error_category="api",
        )
        return _checked_tool_result(ctx, name, result)


def _checked_tool_result(
    ctx: ToolContext,
    name: str,
    result: ToolResultRecord,
) -> ToolResultRecord:
    ctx.raise_if_cancelled(f"tool.{name}.before_result")
    return result


def _designer_thinking_budget_for_ctx(settings: Any, ctx: ToolContext) -> int:
    budget = int(getattr(settings, "designer_thinking_budget", 0) or 0)
    if budget <= 0:
        return budget
    if not _is_dogfood_mode(ctx):
        return budget
    if not _dogfood_post_composite_repair_context(ctx):
        return budget
    cap = _dogfood_post_composite_thinking_budget_cap()
    if cap <= 0:
        return budget
    return min(budget, cap)


def _dogfood_post_composite_repair_context(ctx: ToolContext) -> bool:
    composite_payload = ctx.state.get("last_composite_payload") or {}
    if not (
        composite_payload.get("preview_sha256")
        or composite_payload.get("preview_relative_path")
        or composite_payload.get("html_relative_path")
    ):
        return False
    feedback = (
        ctx.state.get("last_design_feedback")
        or composite_payload.get("design_feedback")
    )
    return bool(blocking_design_findings(feedback) or ctx.state.get("critique_results"))


def _dogfood_post_composite_thinking_budget_cap() -> int:
    raw = os.getenv(
        "DOGFOOD_POST_COMPOSITE_THINKING_BUDGET",
        str(_DOGFOOD_POST_COMPOSITE_THINKING_BUDGET),
    ).strip()
    try:
        return int(raw)
    except ValueError:
        return _DOGFOOD_POST_COMPOSITE_THINKING_BUDGET


def _user_prompt(brief: str, *, claim_graph: Any | None = None) -> str:
    claim_graph_block = _claim_graph_prompt_block(claim_graph)
    if brief.lstrip().startswith("Automatic environment repair pass"):
        return (
            f"Design brief:\n\n{brief}{claim_graph_block}\n\n"
            "This is a repair pass over an existing artifact. Do not call "
            "`ingest_document`. Inspect the Current DesignSpec and Latest "
            "design_feedback in the brief. Prefer `apply_design_ops` for "
            "local geometry/text fixes. Call `propose_design_spec` only for "
            "a structural rewrite, and when you do, pass a complete top-level "
            "`{\"design_spec\": ...}` object. Then call `composite`; only call "
            "`finalize` after the latest composite has no blocking "
            "`design_feedback`."
        )
    return (
        f"Design brief:\n\n{brief}{claim_graph_block}\n\n"
        "Follow the workflow contract from your system prompt. If the brief "
        "begins with `Attached files:`, begin by calling `ingest_document`; "
        "otherwise declare the artifact type and call `propose_design_spec` "
        "with a complete DesignSpec JSON, then proceed."
    )


def _tool_arg_shape(args: dict[str, Any]) -> dict[str, Any]:
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
    except Exception:  # noqa: BLE001 - diagnostic logging only
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


def _should_stop_after_dogfood_blocking_composite(
    tool_name: str,
    result: ToolResultRecord,
    ctx: ToolContext,
) -> bool:
    if not _is_dogfood_mode(ctx):
        return False
    if tool_name != "composite" or result.status != "ok":
        return False
    payload = result.payload if isinstance(result.payload, dict) else {}
    feedback = payload.get("design_feedback") or ctx.state.get("last_design_feedback")
    blocking = blocking_design_findings(feedback)
    if not blocking:
        ctx.state["dogfood_clean_composite_ready"] = {
            "reason": "dogfood_auto_finalize_after_clean_composite",
            "tool": tool_name,
        }
        log(
            "designer.clean_composite_ready",
            reason="dogfood_auto_finalize_after_clean_composite",
        )
        return True
    if only_visual_reference_progression_findings(blocking):
        ctx.state.pop("dogfood_blocking_composite_report_only", None)
        ctx.state["designer_blocking_composite_feedback"] = {
            "blocking_findings": len(blocking),
            "continue_for_repair": True,
            "system_repair_first": False,
            "visual_reference_progression": True,
        }
        log(
            "designer.visual_reference_progression",
            blockers=[finding.get("id") for finding in blocking],
            action="continue_designer_loop",
        )
        return False
    if _should_continue_repairable_deck_layout(blocking, ctx):
        attempts = int(ctx.state.get("deck_layout_designer_repair_attempts") or 0) + 1
        ctx.state["deck_layout_designer_repair_attempts"] = attempts
        ctx.state.pop("dogfood_blocking_composite_report_only", None)
        ctx.state["designer_blocking_composite_feedback"] = {
            "blocking_findings": len(blocking),
            "continue_for_repair": True,
            "system_repair_first": False,
            "deck_layout_repair": True,
            "repair_attempt": attempts,
            "repair_budget": _DOGFOOD_DECK_LAYOUT_REPAIR_BUDGET,
        }
        log(
            "designer.deck_layout_repair",
            blockers=[finding.get("id") for finding in blocking],
            repair_attempt=attempts,
            repair_budget=_DOGFOOD_DECK_LAYOUT_REPAIR_BUDGET,
            action="continue_designer_loop",
        )
        return False
    if _dogfood_dense_composite_should_stop_for_local_repair(payload):
        ctx.state["designer_blocking_composite_feedback"] = {
            "blocking_findings": len(blocking),
            "continue_for_repair": False,
            "system_repair_first": True,
            "dense_candidate_terminal": True,
        }
        ctx.state["dogfood_blocking_composite_report_only"] = {
            "blocking_findings": len(blocking),
            "reason": "dogfood_dense_candidate_terminal_local_repair",
        }
        log(
            "designer.blocking_composite_feedback",
            reason="dogfood_dense_candidate_terminal_local_repair",
            blocking_findings=len(blocking),
            visible_words=_safe_payload_float(payload, "visible_text_word_count"),
            native_units=_safe_payload_float(payload, "paper_info_unit_count"),
            visual_area_ratio=_safe_payload_float(payload, "visual_area_ratio"),
            dom_p0=payload.get("paper_poster_dom_p0_count"),
        )
        return True
    system_repair_first = _dogfood_blocking_composite_needs_system_repair(blocking)
    continue_for_repair = (
        _dogfood_inline_blocking_repair_enabled()
        and not system_repair_first
    )
    ctx.state["designer_blocking_composite_feedback"] = {
        "blocking_findings": len(blocking),
        "continue_for_repair": continue_for_repair,
        "system_repair_first": system_repair_first,
    }
    if not continue_for_repair:
        reason = (
            "dogfood_system_repair_after_blocking_composite"
            if system_repair_first
            else "dogfood_report_only_after_blocking_composite"
        )
        ctx.state["dogfood_blocking_composite_report_only"] = {
            "blocking_findings": len(blocking),
            "reason": reason,
        }
        log(
            "designer.blocking_composite_feedback",
            reason=reason,
            blocking_findings=len(blocking),
        )
        return True
    log(
        "designer.blocking_composite_feedback",
        reason="dogfood_inline_repair",
        blocking_findings=len(blocking),
    )
    return False


def _dogfood_dense_composite_should_stop_for_local_repair(payload: dict[str, Any]) -> bool:
    """Do not spend another long designer turn on dense local-DOM repairs.

    The outer poster harness needs a comparable rendered candidate. Once a
    dogfood authored poster is already dense enough to judge visually, local
    overflow/overlap should be handled by deterministic repair or by the next
    system patch, not by an open-ended LLM rewrite that often times out and
    erases the candidate.
    """
    if str(payload.get("artifact_type") or "") != "poster":
        return False
    if str(payload.get("render_mode") or "") != "authored_html":
        return False
    has_artifact = bool(
        payload.get("preview_sha256")
        or payload.get("preview_relative_path")
        or payload.get("html_relative_path")
    )
    if not has_artifact:
        return False
    visible_words = max(
        _safe_payload_float(payload, "visible_text_word_count"),
        _safe_payload_float(payload, "leaf_visible_word_count"),
        _safe_payload_float(payload, "authored_visible_text_word_count"),
    )
    native_units = max(
        _safe_payload_float(payload, "paper_info_unit_count"),
        _safe_payload_float(payload, "native_information_unit_count"),
        _safe_payload_float(payload, "authored_native_information_unit_count"),
    )
    visual_area = max(
        _safe_payload_float(payload, "visual_area_ratio"),
        _safe_payload_float(payload, "authored_visual_area_ratio"),
    )
    if visible_words < 950:
        return False
    if native_units < 9:
        return False
    if visual_area < 0.08:
        return False
    return True


def _should_continue_repairable_deck_layout(
    blocking: list[dict[str, Any]],
    ctx: ToolContext,
) -> bool:
    if str(ctx.state.get("artifact_type") or "") != "deck" or not blocking:
        return False
    if int(ctx.state.get("deck_layout_designer_repair_attempts") or 0) >= _DOGFOOD_DECK_LAYOUT_REPAIR_BUDGET:
        return False
    return all(
        isinstance(finding, dict)
        and str(finding.get("source") or "") == "deck_layout"
        and finding.get("repairable") is not False
        for finding in blocking
    )


def _safe_payload_float(payload: dict[str, Any], key: str) -> float:
    try:
        return float(payload.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _dogfood_blocking_composite_needs_system_repair(blocking: list[Any]) -> bool:
    """Let deterministic repair handle mechanical DOM/layout blockers first.

    Post-composite LLM repair is expensive and often times out when the
    feedback is mostly concrete geometry: text overflow, overlap, DOM P0, or
    renderer realization problems. Those are better handled by the runner's
    deterministic repair path so the outer harness gets a fast, comparable
    candidate instead of a 600s designer timeout.
    """
    mechanical_markers = (
        "paper-poster-text-overflow",
        "paper-poster-text-overlap",
        "candidate_dom_audit_p0",
        "dom_audit",
        "paper_poster_dom",
        "authored-html-text-bbox",
        "quality_lint:ai-default-indigo",
    )
    mechanical_count = 0
    for finding in blocking:
        if not isinstance(finding, dict):
            continue
        text = " ".join(
            str(finding.get(key) or "")
            for key in ("id", "source", "message", "repair_route")
        ).lower()
        if any(marker in text for marker in mechanical_markers):
            mechanical_count += 1
    if mechanical_count == 0:
        return False
    if mechanical_count == len(blocking):
        return True
    mostly_mechanical_threshold = max(3, (len(blocking) * 3 + 3) // 4)
    if mechanical_count >= mostly_mechanical_threshold:
        return True
    return False


def _dogfood_inline_repair_blocker_limit() -> int:
    raw = os.getenv("DOGFOOD_INLINE_REPAIR_MAX_BLOCKERS", "5").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 5


def _dogfood_inline_blocking_repair_enabled() -> bool:
    return os.getenv(_DOGFOOD_INLINE_BLOCKING_REPAIR_ENV, "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _validation_error_signature(
    tool_name: str,
    result: ToolResultRecord,
) -> tuple[str, str, str]:
    return (
        tool_name,
        result.error_category or "",
        (result.error_message or "")[:500],
    )


def _should_retry_after_validation_end_turn(
    ctx: ToolContext,
    *,
    last_validation_error: dict[str, Any] | None,
    retry_count: int,
) -> bool:
    if retry_count >= 2:
        return False
    if ctx.state.get("finalized") or ctx.state.get("design_spec") is not None:
        return False
    if not _is_dogfood_mode(ctx):
        return False
    if not (
        isinstance(ctx.state.get("poster_content_brief"), dict)
        or isinstance(ctx.state.get("poster_plan_contract"), dict)
    ):
        return False
    if not isinstance(last_validation_error, dict):
        return False
    tool_name = str(last_validation_error.get("tool") or "")
    return tool_name in {"propose_paper_poster_html", "propose_design_spec"}


def _validation_end_turn_retry_prompt(last_validation_error: dict[str, Any], *, ctx: ToolContext | None = None) -> str:
    tool_name = str(last_validation_error.get("tool") or "propose_paper_poster_html")
    issue_id = str(last_validation_error.get("issue_id") or "validation_error")
    repair_route = str(last_validation_error.get("repair_route") or "repair_and_retry")
    message = str(last_validation_error.get("error_message") or "")[:900]
    hint = str(last_validation_error.get("hint") or "")[:900]
    local_repair_hint = str(last_validation_error.get("local_repair_hint") or "")[:900]
    candidate_id = str(last_validation_error.get("candidate_id") or "")
    candidate_relative_dir = str(last_validation_error.get("candidate_relative_dir") or "")
    candidate_preview_png = str(last_validation_error.get("candidate_preview_png") or "")
    candidate_measurement_json = str(last_validation_error.get("candidate_measurement_json") or "")
    locked_base = _locked_base_candidate_for_retry(last_validation_error, ctx)
    locked_base_block = _locked_base_candidate_retry_block(locked_base, ctx=ctx)
    issues = last_validation_error.get("issues")
    issue_sample = ""
    if isinstance(issues, list) and issues:
        try:
            issue_sample = json.dumps(issues, ensure_ascii=False)[:1200]
        except TypeError:
            issue_sample = str(issues)[:1200]
    required_tool = (
        "propose_paper_poster_html"
        if tool_name in {"propose_paper_poster_html", "propose_design_spec"}
        else tool_name
    )
    editorial_retry = _poster_contract_requests_editorial_flow(last_validation_error, ctx=ctx)
    if editorial_retry:
        repair_instructions = (
            "For `conference_editorial_flow`, do not submit `panel_content_plan`. "
            "Reuse the previous composition and edit only the failing selectors, blocks, "
            "or local text needed to satisfy the validation issue. Preserve section order, "
            "source placements, and the per-asset flow DOM. Keep one compact header, exactly "
            "three `.poster-column` columns, one to three `.poster-section` blocks per column, "
            "and one `.figure-flow-unit`/`.source-flow-unit` per paper figure/table with a "
            "local readout. Do not add visible Fig./Table captions, do not globally compress "
            "the poster, and do not fix overflow by clipping columns/sections or shrinking "
            "source figures/tables into shallow strips."
        )
    else:
        repair_instructions = (
            "For `propose_paper_poster_html`, reuse the previous composition and submit corrected "
            "HTML/CSS with only the failing selectors, blocks, or local text changed. "
            "Preserve the fixed canvas and dense panel interiors. Repair overlaps/out-of-bounds content "
            "by moving boxes, resizing lanes, reducing font size or line-height, splitting text into "
            "columns/table rows, and rebalancing panels. Do not make the candidate pass by deleting "
            "source-backed text, tables, metrics, captions, or figures."
        )
    diagnostic_lines = []
    if locked_base_block:
        diagnostic_lines.append(locked_base_block)
    if candidate_id or candidate_relative_dir:
        diagnostic_lines.append(f"Candidate: `{candidate_id}` at `{candidate_relative_dir}`")
    if candidate_preview_png:
        diagnostic_lines.append(f"Candidate preview: `{candidate_preview_png}`")
    if candidate_measurement_json:
        diagnostic_lines.append(f"Candidate measurement: `{candidate_measurement_json}`")
    if local_repair_hint:
        diagnostic_lines.append(f"Local repair hint: {local_repair_hint}")
    if hint:
        diagnostic_lines.append(f"Tool hint: {hint}")
    if issue_sample:
        diagnostic_lines.append(f"Issue sample: {issue_sample}")
    diagnostics = "\n".join(diagnostic_lines)
    diagnostics_block = f"\n{diagnostics}\n" if diagnostics else ""
    return (
        "The previous designer action ended after a validation error, but this dogfood paper poster "
        "does not yet have an accepted DesignSpec. Do not end the turn. Repair the concrete validation "
        f"failure and call `{required_tool}` again now.\n\n"
        f"Validation issue: `{issue_id}`\n"
        f"Repair route: `{repair_route}`\n"
        f"Tool error: {message}\n\n"
        f"{diagnostics_block}"
        f"{repair_instructions}"
    )


def _locked_base_candidate_for_retry(last_validation_error: dict[str, Any], ctx: ToolContext | None) -> dict[str, Any]:
    if ctx is not None and isinstance(ctx.state, dict):
        locked = ctx.state.get("paper_poster_html_locked_base_candidate")
        if isinstance(locked, dict):
            return dict(locked)
        best = ctx.state.get("paper_poster_html_best_candidate")
        if isinstance(best, dict):
            return dict(best)
    candidate_id = str(last_validation_error.get("locked_base_candidate_id") or "")
    if not candidate_id:
        return {}
    return {
        "candidate_id": candidate_id,
        "candidate_relative_dir": last_validation_error.get("locked_base_candidate_relative_dir") or "",
        "preview_png_relative": last_validation_error.get("locked_base_candidate_preview_png") or "",
        "measurement_json_relative": last_validation_error.get("locked_base_candidate_measurement_json") or "",
        "candidate_score": last_validation_error.get("locked_base_candidate_score") or "",
    }


def _locked_base_candidate_retry_block(locked: dict[str, Any], *, ctx: ToolContext | None) -> str:
    if not locked:
        return ""
    candidate_id = str(locked.get("candidate_id") or "")
    relative_dir = str(locked.get("candidate_relative_dir") or "")
    score = str(locked.get("candidate_score") or "")
    preview = str(locked.get("preview_png_relative") or locked.get("preview_png") or "")
    measurement = str(locked.get("measurement_json_relative") or locked.get("measurement_json") or "")
    body_path = _retry_candidate_path(locked, "body_html", ctx=ctx)
    css_path = _retry_candidate_path(locked, "style_css", ctx=ctx)
    body_excerpt = _retry_file_excerpt(body_path, max_chars=1800)
    css_excerpt = _retry_file_excerpt(css_path, max_chars=1200)
    lines = [
        "Locked base candidate for local patch:",
        f"- base_candidate_id: `{candidate_id}`",
    ]
    if relative_dir:
        lines.append(f"- base_dir: `{relative_dir}`")
    if score:
        lines.append(f"- base_score: `{score}`")
    if preview:
        lines.append(f"- base_preview: `{preview}`")
    if measurement:
        lines.append(f"- base_measurement: `{measurement}`")
    if body_path:
        lines.append(f"- base_body_html: `{body_path}`")
    if css_path:
        lines.append(f"- base_style_css: `{css_path}`")
    lines.append(
        "Patch this locked base. Do not replan the poster. Preserve header, column order, "
        "section order, source ids, and source-flow unit ownership unless the listed issue "
        "names that exact block."
    )
    if body_excerpt:
        lines.append("Base body excerpt:\n```html\n" + body_excerpt + "\n```")
    if css_excerpt:
        lines.append("Base CSS excerpt:\n```css\n" + css_excerpt + "\n```")
    return "\n".join(lines)


def _retry_candidate_path(locked: dict[str, Any], key: str, *, ctx: ToolContext | None) -> str:
    raw = str(locked.get(key) or "")
    if raw:
        return raw
    relative_dir = str(locked.get("candidate_relative_dir") or "")
    if not relative_dir or ctx is None:
        return ""
    suffix = "body.html" if key == "body_html" else "style.css"
    return str(ctx.run_dir / relative_dir / suffix)


def _retry_file_excerpt(path_value: str, *, max_chars: int) -> str:
    if not path_value:
        return ""
    path = Path(path_value)
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n..."


def _poster_contract_requests_editorial_flow(last_validation_error: dict[str, Any], *, ctx: ToolContext | None = None) -> bool:
    if ctx is not None and isinstance(ctx.state, dict):
        for key in ("poster_plan_contract", "poster_content_brief"):
            value = ctx.state.get(key)
            if isinstance(value, dict):
                blob = " ".join(
                    str(value.get(field) or "")
                    for field in ("reference_profile", "mode")
                ).lower()
                if "conference_editorial_flow" in blob or "editorial_flow" in blob:
                    return True
                if key == "poster_plan_contract" and isinstance(value.get("editorial_flow_contract"), dict):
                    return True
    payload = last_validation_error.get("payload")
    if isinstance(payload, dict):
        for key in ("reference_profile", "mode", "repair_route"):
            value = str(payload.get(key) or "").strip().lower()
            if "conference_editorial_flow" in value or "editorial_flow" in value:
                return True
    text = " ".join(
        str(last_validation_error.get(key) or "")
        for key in ("issue_id", "repair_route", "error_message")
    ).lower()
    return "editorial" in text


def _is_auth_or_key_error(error: Exception) -> bool:
    text = f"{type(error).__name__}: {error}".lower()
    auth_markers = (
        "401",
        "403",
        "unauthorized",
        "forbidden",
        "authentication",
        "api key",
        "apikey",
        "invalid key",
        "missing key",
        "permission denied",
    )
    return any(marker in text for marker in auth_markers)


def _tool_schemas_for_context(ctx: ToolContext) -> list[dict[str, Any]]:
    hidden: set[str] = set()
    terminal_critic = _dogfood_terminal_critic_budget_exhausted(ctx)
    if not _has_active_runtime_skill_resources(ctx):
        hidden.add("read_skill_resource")
    if _dogfood_authored_poster_route_blocked(ctx):
        hidden.add("propose_design_spec")
    if _dogfood_awaiting_authored_spec(ctx):
        hidden.update({
            "apply_design_ops",
            "composite",
            "critique",
            "finalize",
            "generate_background",
            "generate_visual_reference",
            "render_text_layer",
        })
    if _dogfood_authored_html_legacy_layer_tools_blocked(ctx):
        hidden.update(_DOGFOOD_HTML_FIRST_LEGACY_LAYER_TOOLS)
    if _dogfood_visual_reference_blocked(ctx):
        hidden.add("generate_visual_reference")
    if _dogfood_finalize_blocked(ctx):
        hidden.add("finalize")
    if terminal_critic:
        hidden.update(
            str(schema.get("name") or "")
            for schema in TOOL_SCHEMAS
            if str(schema.get("name") or "") != "finalize"
        )

    if not hidden:
        return TOOL_SCHEMAS
    if terminal_critic and not ctx.state.get("_dogfood_terminal_critic_schema_hidden"):
        ctx.state["_dogfood_terminal_critic_schema_hidden"] = True
        log(
            "designer.tool_schema.hidden",
            tool="post_critic_mutations",
            reason="dogfood_max_critique_iters_exhausted_finalize_only",
        )
    if "generate_visual_reference" in hidden and not ctx.state.get("_dogfood_visual_reference_schema_hidden"):
        ctx.state["_dogfood_visual_reference_schema_hidden"] = True
        log(
            "designer.tool_schema.hidden",
            tool="generate_visual_reference",
            reason="dogfood_paper_poster_requires_explicit_visual_reference_issue",
        )
    if "finalize" in hidden and not ctx.state.get("_dogfood_finalize_schema_hidden"):
        ctx.state["_dogfood_finalize_schema_hidden"] = True
        log(
            "designer.tool_schema.hidden",
            tool="finalize",
            reason="dogfood_blocking_design_feedback_requires_repair",
            blocking_findings=len(_current_blocking_design_findings(ctx)),
        )
    if "propose_design_spec" in hidden and not ctx.state.get("_dogfood_propose_design_spec_schema_hidden"):
        ctx.state["_dogfood_propose_design_spec_schema_hidden"] = True
        log(
            "designer.tool_schema.hidden",
            tool="propose_design_spec",
            reason="dogfood_paper_poster_requires_authored_html_tool",
        )
    if _dogfood_awaiting_authored_spec(ctx) and not ctx.state.get("_dogfood_awaiting_authored_spec_schema_hidden"):
        ctx.state["_dogfood_awaiting_authored_spec_schema_hidden"] = True
        log(
            "designer.tool_schema.hidden",
            tool="post_authored_spec_tools",
            reason="dogfood_paper_poster_awaiting_propose_paper_poster_html_success",
        )
    if (
        _dogfood_authored_html_legacy_layer_tools_blocked(ctx)
        and not ctx.state.get("_dogfood_html_first_legacy_layer_schema_hidden")
    ):
        ctx.state["_dogfood_html_first_legacy_layer_schema_hidden"] = True
        log(
            "designer.tool_schema.hidden",
            tool="legacy_layer_tools",
            reason="dogfood_paper_poster_html_first_blocks_legacy_layer_repairs",
            blocked_tools=sorted(_DOGFOOD_HTML_FIRST_LEGACY_LAYER_TOOLS),
        )
    return [
        schema for schema in TOOL_SCHEMAS
        if schema.get("name") not in hidden
    ]


def _has_active_runtime_skill_resources(ctx: ToolContext) -> bool:
    stage = str(ctx.state.get("runtime_skill_stage") or "").strip().lower()
    state = ctx.state.get("skills")
    if not stage or not isinstance(state, dict):
        return False
    selected = {str(skill_id) for skill_id in state.get("selected") or []}
    for pack in state.get("packs") or []:
        if not isinstance(pack, dict):
            continue
        if str(pack.get("id") or "") not in selected:
            continue
        if str(pack.get("manifest_version") or 1) != "2":
            continue
        pack_stages = pack.get("stages") or []
        if pack_stages and stage not in pack_stages:
            continue
        for resource in pack.get("resources") or []:
            if isinstance(resource, dict) and stage in (resource.get("stages") or []):
                return True
    return False


def _dogfood_authored_poster_route_tool_blocker(
    tool_name: str,
    ctx: ToolContext,
) -> ToolResultRecord | None:
    if tool_name != "propose_design_spec":
        return None
    if not _dogfood_authored_poster_route_blocked(ctx):
        return None
    return ToolResultRecord(
        status="error",
        error_category="validation",
        error_message=(
            "dogfood academic paper posters must use propose_paper_poster_html. "
            "Re-call propose_paper_poster_html with corrected authored HTML/CSS; "
            "do not fall back to legacy DesignSpec JSON."
        ),
        payload={
            "dogfood_authored_html_route": {
                "blocked_tool": tool_name,
                "required_tool": "propose_paper_poster_html",
                "repair_route": "resubmit_authored_html",
            },
        },
    )


def _dogfood_authored_poster_route_blocked(ctx: ToolContext) -> bool:
    if not _is_dogfood_mode(ctx):
        return False
    return _looks_like_dogfood_paper_poster(ctx)


def _dogfood_awaiting_authored_spec_tool_blocker(
    tool_name: str,
    ctx: ToolContext,
) -> ToolResultRecord | None:
    blocked_tools = {
        "apply_design_ops",
        "composite",
        "critique",
        "finalize",
        "generate_background",
        "generate_visual_reference",
        "render_text_layer",
    }
    if tool_name not in blocked_tools:
        return None
    if not _dogfood_awaiting_authored_spec(ctx):
        return None
    return ToolResultRecord(
        status="error",
        error_category="validation",
        error_message=(
            "dogfood academic paper posters must first compile authored HTML "
            "through propose_paper_poster_html. Fix and re-call "
            "propose_paper_poster_html before using downstream rendering or "
            "repair tools."
        ),
        payload={
            "dogfood_authored_html_route": {
                "blocked_tool": tool_name,
                "required_tool": "propose_paper_poster_html",
                "repair_route": "resubmit_authored_html",
            },
        },
    )


def _dogfood_awaiting_authored_spec(ctx: ToolContext) -> bool:
    if not _dogfood_authored_poster_route_blocked(ctx):
        return False
    return ctx.state.get("design_spec") is None


_DOGFOOD_HTML_FIRST_LEGACY_LAYER_TOOLS = {
    "edit_layer",
    "generate_background",
    "generate_image",
    "render_text_layer",
}


def _dogfood_authored_html_legacy_layer_tool_blocker(
    tool_name: str,
    ctx: ToolContext,
) -> ToolResultRecord | None:
    if tool_name not in _DOGFOOD_HTML_FIRST_LEGACY_LAYER_TOOLS:
        return None
    if not _dogfood_authored_html_legacy_layer_tools_blocked(ctx):
        return None
    return ToolResultRecord(
        status="error",
        error_category="validation",
        error_message=(
            "dogfood HTML-first paper posters cannot be repaired with legacy "
            f"layer tool `{tool_name}`. Use propose_paper_poster_html for a "
            "new authored HTML draft, or apply_design_ops with html_* ops for "
            "local geometry/text repairs, then composite again."
        ),
        payload={
            "dogfood_html_first_legacy_layer_gate": {
                "blocked_tool": tool_name,
                "allowed_tools": [
                    "propose_paper_poster_html",
                    "apply_design_ops",
                    "composite",
                    "critique",
                    "finalize",
                ],
                "repair_route": "authored_html_or_html_design_ops",
            },
        },
    )


def _dogfood_authored_html_legacy_layer_tools_blocked(ctx: ToolContext) -> bool:
    if not _dogfood_authored_poster_route_blocked(ctx):
        return False
    if _dogfood_awaiting_authored_spec(ctx):
        return False
    spec = ctx.state.get("design_spec")
    artifact = getattr(spec, "html_artifact", None)
    if artifact is None and isinstance(spec, dict):
        artifact = spec.get("html_artifact")
    frames = getattr(artifact, "frames", None)
    if frames is None and isinstance(artifact, dict):
        frames = artifact.get("frames")
    if not isinstance(frames, list) or not frames:
        return False
    for frame in frames:
        render_mode = getattr(frame, "render_mode", None)
        target = getattr(frame, "target", None)
        role = getattr(frame, "role", None)
        if isinstance(frame, dict):
            render_mode = frame.get("render_mode")
            target = frame.get("target")
            role = frame.get("role")
        blob = " ".join(str(value or "") for value in (render_mode, target, role)).lower()
        if "authored_html" in blob or "paper_poster" in blob or "poster" in blob:
            return True
    return False


def _dogfood_visual_reference_tool_blocker(
    tool_name: str,
    ctx: ToolContext,
) -> ToolResultRecord | None:
    if tool_name != "generate_visual_reference":
        return None
    if not _dogfood_visual_reference_blocked(ctx):
        return None
    return ToolResultRecord(
        status="error",
        error_category="validation",
        error_message=(
            "dogfood paper-poster mode blocks generic generate_visual_reference "
            "calls. Use source figures/tables, design_feedback, apply_design_ops, "
            "propose_design_spec, composite, and critique unless a finding or "
            "critic issue explicitly asks for visual-reference/image repair."
        ),
        payload={
            "dogfood_visual_reference_gate": {
                "blocked_tool": tool_name,
                "allowed_when": (
                    "design_feedback source/id references visual_reference, "
                    "or the latest critic issue explicitly requests visual-reference "
                    "or reference-image repair"
                ),
                "repair_route": "source_visuals_or_editable_layout",
            },
        },
    )


def _dogfood_visual_reference_blocked(ctx: ToolContext) -> bool:
    if not _is_dogfood_mode(ctx):
        return False
    if not _looks_like_dogfood_paper_poster(ctx):
        return False
    return not _dogfood_visual_reference_explicitly_requested(ctx)


def _dogfood_finalize_tool_blocker(
    tool_name: str,
    ctx: ToolContext,
) -> ToolResultRecord | None:
    if tool_name != "finalize":
        return None
    if not _dogfood_finalize_blocked(ctx):
        return None
    blocking = _current_blocking_design_findings(ctx)
    return ToolResultRecord(
        status="error",
        error_category="validation",
        error_message=(
            "dogfood paper-poster mode cannot finalize while the latest "
            "composite still has blocking design_feedback. Repair the artifact "
            "with apply_design_ops or a complete propose_design_spec, then call "
            "composite again before finalize."
        ),
        payload={
            "dogfood_finalize_gate": {
                "blocking_findings": len(blocking),
                "blocked_tool": tool_name,
                "repair_route": "repair_then_composite",
            },
        },
    )


def _dogfood_finalize_blocked(ctx: ToolContext) -> bool:
    if not _is_dogfood_mode(ctx):
        return False
    if not _looks_like_dogfood_paper_poster(ctx):
        return False
    if _dogfood_terminal_critic_budget_exhausted(ctx):
        return False
    return bool(_current_blocking_design_findings(ctx))


def _current_blocking_design_findings(ctx: ToolContext) -> list[Any]:
    feedback = (
        ctx.state.get("last_design_feedback")
        or (ctx.state.get("last_composite_payload") or {}).get("design_feedback")
    )
    return list(blocking_design_findings(feedback))


def _looks_like_dogfood_paper_poster(ctx: ToolContext) -> bool:
    spec = ctx.state.get("design_spec")
    artifact_type = getattr(spec, "artifact_type", None) or ctx.state.get("artifact_type")
    artifact_value = getattr(artifact_type, "value", artifact_type)
    if artifact_value is not None and str(artifact_value) != "poster":
        return False
    return any(
        isinstance(ctx.state.get(key), dict) and bool(ctx.state.get(key))
        for key in (
            "poster_content_brief",
            "poster_plan_contract",
            "poster_contract_preflight",
        )
    )


def _dogfood_visual_reference_explicitly_requested(ctx: ToolContext) -> bool:
    feedback = (
        ctx.state.get("last_design_feedback")
        or (ctx.state.get("last_composite_payload") or {}).get("design_feedback")
    )
    for finding in _iter_feedback_findings(feedback):
        if _text_mentions_visual_reference(
            " ".join(
                str(finding.get(key) or "")
                for key in ("id", "source", "message", "fix", "repair_route", "stage")
            )
        ):
            return True

    crits = ctx.state.get("critique_results") or []
    if not crits:
        return False
    latest = crits[-1]
    for issue in getattr(latest, "issues", []) or []:
        text = " ".join(
            str(getattr(issue, attr, "") or "")
            for attr in ("issue_id", "category", "description", "suggested_action", "repair_tool", "repair_route")
        )
        if _text_mentions_visual_reference(text):
            return True
    return False


def _iter_feedback_findings(feedback: Any) -> list[dict[str, Any]]:
    if feedback is None:
        return []
    if hasattr(feedback, "model_dump"):
        try:
            feedback = feedback.model_dump(mode="json")
        except TypeError:
            feedback = feedback.model_dump()
    if not isinstance(feedback, dict):
        return []
    findings = feedback.get("findings")
    return [item for item in findings or [] if isinstance(item, dict)]


def _text_mentions_visual_reference(text: str) -> bool:
    lower = str(text or "").lower().replace("-", "_").replace(" ", "_")
    markers = (
        "visual_reference",
        "reference_image",
        "reference_guided",
        "image_repair",
        "generate_visual_reference",
    )
    return any(marker in lower for marker in markers)


def _dogfood_max_critique_terminal_blocker(
    tool_name: str,
    ctx: ToolContext,
) -> ToolResultRecord | None:
    """Prevent unverified repairs after dogfood critique budget is exhausted."""
    if tool_name in {"finalize", "critique"}:
        return None
    terminal = _dogfood_terminal_critic_budget_exhausted(ctx)
    if not terminal:
        return None
    ctx.state["dogfood_terminal_critic_report_only"] = terminal
    return ToolResultRecord(
        status="error",
        error_category="validation",
        error_message=(
            "max_critique_iters reached after a critic pass on the latest "
            "dogfood composite; call finalize without further design mutations"
        ),
        payload={
            "dogfood_terminal_critic": {
                **terminal,
                "blocked_tool": tool_name,
            },
        },
    )


def _dogfood_terminal_critic_budget_exhausted(ctx: ToolContext) -> dict[str, Any] | None:
    if not _is_dogfood_mode(ctx):
        return None
    max_iters = max(0, int(getattr(ctx.settings, "max_critique_iters", 0) or 0))
    if max_iters <= 0:
        return None
    crits = ctx.state.get("critique_results") or []
    if len(crits) < max_iters:
        return None
    composite_payload = ctx.state.get("last_composite_payload") or {}
    current_sha = composite_payload.get("preview_sha256")
    critic_sha = ctx.state.get("last_critique_preview_sha256")
    if not current_sha or critic_sha != current_sha:
        return None
    last = crits[-1]
    return {
        "verdict": getattr(last, "verdict", None),
        "score": getattr(last, "score", None),
        "max_critique_iters": max_iters,
        "latest_preview_sha256": current_sha,
        "repair_route": "finalize",
    }


def _claim_graph_prompt_block(claim_graph: Any | None) -> str:
    if claim_graph is None:
        return ""
    graph_json = json.dumps(
        claim_graph.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        "\n\n## ClaimGraph JSON\n"
        "This graph was extracted and validated before planning. Use it to "
        "order paper decks along the talk arc. `LayerNode.covers` may only "
        "reference ids from the graph's tensions, mechanisms, evidence, and "
        "implications arrays; do not invent ids.\n"
        "```json\n"
        f"{graph_json}\n"
        "```"
    )
