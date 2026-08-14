"""CriticAgent — v2.7.3 vision critic, forked sub-agent.

Owns its own LLMBackend instance, its own tool loop, and its own turn budget.
Architecturally similar to DesignerLoop but lives outside the planner's loop so
the critic can take as many turns as it needs without consuming
designer.max_designer_turns.

Why: the v2.6 inline `critique_tool` shared the planner's turn budget
(max 2 calls), force-injected the entire DesignSpec into a single LLM
call, and used vision only for posters. Cloud Design's separate
vision-verifier agent showed how much fidelity that costs. v2.7.3 splits
the critic into a peer sub-agent that:
  - sees rendered slide PNGs for ALL artifact types (deck/landing/poster)
  - pulls relevant paper passages on-demand instead of dumping raw_text
  - emits a structured `CritiqueReport` via a terminal `report_verdict` tool

The planner-facing tool (`critique_tool.py`) is now a thin wrapper that
spawns one CriticAgent per `critique` invocation. From the planner's
perspective the tool signature is unchanged: one call returns one
CritiqueReport JSON in `tool_result.payload`.

Hand-written tool loop, no framework. Same pattern as designer.py.
"""

from __future__ import annotations

import base64
import io
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image
from pydantic import ValidationError

from ..config import Settings
from ..llm_backend import LLMBackend, ToolCall, TurnResponse, make_backend
from ..run_control import CancellationToken
from ..schema import (
    ArtifactType, ClaimGraph, CritiqueIssue, CritiqueReport, DesignSpec,
)
from ..util.logging import log


@dataclass
class _VisionAttachment:
    """One pending image to deliver as a follow-up user-role vision block.

    Lives only for the duration of a single turn. Created by the
    `read_slide_render` dispatcher; consumed by `critique()` after
    `append_tool_results`."""
    slide_id: str
    image_b64: str
    media_type: str


_PAPER_EXCERPT_HARD_CAP_CHARS: int = 8000


# ─────────────────────────── Tool schemas ──────────────────────────────────

_CRITIC_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "read_slide_render",
        "description": (
            "Fetch a rendered slide PNG by its slide_id (deck) or by the "
            "synthetic poster/landing render id. Paper Landing pages expose "
            "their desktop render as landing_full. The PNG is delivered "
            "to you as a real vision content block on the next turn — the "
            "tool result itself returns only a small ack JSON, so this "
            "call is cheap to repeat. There is a per-turn cap (see the "
            "first user message); calls beyond the cap return "
            "`{\"deferred\": true}` and you should refetch them on a "
            "later turn after acting on the images you already have."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "slide_id": {
                    "type": "string",
                    "description": (
                        "The slide_id from DesignSpec.layer_graph (deck) or "
                        "a synthetic id like 'poster_full' or 'landing_full' "
                        "for non-deck artifacts. Match exactly — IDs come "
                        "from the user message's slide manifest."
                    ),
                },
            },
            "required": ["slide_id"],
        },
    },
    {
        "name": "read_paper_section",
        "description": (
            "Pull a relevant excerpt from the source paper raw_text by "
            "keyword / section heading. Returns up to ~2000 chars centered "
            "on the first match. Use this to verify quotes / numbers / "
            "terminology before flagging a provenance issue. Returns empty "
            "string when paper_raw_text is None (free-text brief) or when "
            "no match is found."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "A short keyword, phrase, or section heading to "
                        "search in the paper's raw text. Case-insensitive."
                    ),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "lookup_claim_node",
        "description": (
            "v2.8.0+ — fetch a single ClaimGraph node by id (T*/M*/E*/I*). "
            "Returns the node's serialized fields so you can verify "
            "whether a slide actually presents that claim. Returns "
            "{\"error\": ...} when no claim_graph is attached or the id "
            "does not exist. Use this when you suspect a tension / "
            "mechanism / evidence node was dropped from the deck."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "claim_id": {
                    "type": "string",
                    "description": (
                        "ClaimGraph node id. Tensions T1/T2/..., "
                        "mechanisms M1/M2/..., evidence E1/E2/..., "
                        "implications I1/I2/..."
                    ),
                },
            },
            "required": ["claim_id"],
        },
    },
    {
        "name": "report_verdict",
        "description": (
            "TERMINAL TOOL. Emit your final CritiqueReport and exit the "
            "loop. Must be called exactly once per critique invocation. "
            "After this call your loop ends; further tool calls are "
            "ignored."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "score": {
                    "type": "number",
                    "description": (
                        "Aggregate quality score in [0, 1]. pass requires "
                        ">= 0.75; fail < 0.5; otherwise revise."
                    ),
                },
                "verdict": {
                    "type": "string",
                    "enum": ["pass", "revise", "fail"],
                },
                "issues": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "issue_id": {"type": ["string", "null"]},
                            "slide_id": {"type": ["string", "null"]},
                            "layer_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "severity": {
                                "type": "string",
                                "enum": ["blocker", "high", "medium", "low"],
                            },
                            "category": {
                                "type": "string",
                                "enum": [
                                    "provenance", "claim_coverage",
                                    "visual_hierarchy", "typography",
                                    "layout", "narrative_flow",
                                    "factual_error",
                                ],
                            },
                            "description": {"type": "string"},
                            "target": {"type": "object"},
                            "evidence": {"type": "object"},
                            "suggested_action": {"type": "string"},
                            "repair_tool": {
                                "type": ["string", "null"],
                                "enum": [
                                    "propose_design_spec", "edit_layer",
                                    "render_text_layer", "generate_image",
                                    "composite", "none", None,
                                ],
                            },
                            "stage": {
                                "type": ["string", "null"],
                                "enum": [
                                    "content_strategy", "visual_curation",
                                    "layout_storyboard", "typography_system",
                                    "rendering_export", None,
                                ],
                            },
                            "repair_route": {
                                "type": ["string", "null"],
                                "enum": [
                                    "local_refine", "pivot_layout_archetype",
                                    "revise_content_strategy", "revise_visual_curation",
                                    "revise_typography_system", "revise_authored_html",
                                    "none", None,
                                ],
                            },
                            "confidence": {
                                "type": ["number", "null"],
                                "description": "Confidence in [0, 1].",
                            },
                            "evidence_paper_anchor": {
                                "type": ["string", "null"],
                            },
                        },
                        "required": ["severity", "category", "description"],
                    },
                },
                "summary": {"type": "string"},
                "dimension_scores": {
                    "type": "object",
                    "description": (
                        "Optional per-dimension scores in [0, 1]. For posters "
                        "use poster_impact, information_architecture, "
                        "evidence_use, human_effort_saved, typography_craft, "
                        "originality_anti_template, and editability_export."
                    ),
                },
                "review_coverage": {
                    "type": "object",
                    "description": (
                        "Optional compact coverage metadata such as inspected "
                        "slide_ids and whether design_feedback was reviewed."
                    ),
                },
            },
            "required": ["score", "verdict", "summary"],
        },
    },
]


_PROMPT_BY_ARTIFACT: dict[ArtifactType, str] = {
    ArtifactType.POSTER: "critic_vision_poster.md",
    ArtifactType.DECK: "critic_vision_deck.md",
    ArtifactType.LANDING: "critic_vision_landing.md",
}


def _raise_if_cancelled(
    cancellation_token: CancellationToken | None,
    phase: str,
) -> None:
    if cancellation_token is not None:
        cancellation_token.raise_if_cancelled(phase)


class CriticAgent:
    """Forked vision critic with its own backend + loop.

    One instance per planner-side `critique` invocation. The instance is
    stateless across critique calls — the planner spawns a new
    CriticAgent each round, passing `iteration` so the prompt can adjust
    tone (revise → escalate to fail at last iter).
    """

    def __init__(self, settings: Settings, artifact_type: ArtifactType):
        self.settings = settings
        self.artifact_type = artifact_type
        self.backend: LLMBackend = make_backend(
            settings, settings.critic_model, role="critic",
        )
        self._system_prompt: str | None = None

    def _system(self) -> str:
        if self._system_prompt is None:
            fname = _PROMPT_BY_ARTIFACT[self.artifact_type]
            path: Path = self.settings.prompts_dir / fname
            self._system_prompt = path.read_text(encoding="utf-8")
        return self._system_prompt

    def critique(
        self,
        spec: DesignSpec,
        layer_manifest: list[dict[str, Any]],
        slide_renders: list[Path],
        paper_raw_text: str | None,
        claim_graph: ClaimGraph | None = None,
        design_feedback: dict[str, Any] | None = None,
        skill_context: str | None = None,
        iteration: int = 1,
        cancellation_token: CancellationToken | None = None,
    ) -> CritiqueReport:
        """Run the sub-agent loop and return the final CritiqueReport.

        On max_turns exhaustion without `report_verdict` being called we
        synthesize a fail verdict so the planner has a deterministic
        signal to react to (instead of hanging or raising).
        """
        _raise_if_cancelled(cancellation_token, "critic.start")
        slide_index = _index_renders(slide_renders, spec)
        backend_name = self.backend.name
        model = self.backend.model
        log("critic.start", iter=iteration, model=model, backend=backend_name,
            artifact_type=self.artifact_type.value,
            n_renders=len(slide_renders), max_turns=self.settings.critic_max_turns,
            has_paper=paper_raw_text is not None,
            has_claim_graph=claim_graph is not None,
            has_design_feedback=design_feedback is not None)
        wall_start = time.monotonic()

        user_text = _build_user_text(
            spec=spec, layer_manifest=layer_manifest,
            slide_index=slide_index, paper_raw_text=paper_raw_text,
            claim_graph=claim_graph, design_feedback=design_feedback,
            skill_context=skill_context,
            iteration=iteration,
            max_iters=self.settings.max_critique_iters,
            max_images_per_turn=self.settings.critic_max_images_per_turn,
        )
        messages: list[Any] = [{"role": "user", "content": user_text}]

        thinking_budget = self.settings.critic_thinking_budget
        max_tokens = max(2048, thinking_budget + 2048) if thinking_budget > 0 else 4096

        terminal_report: CritiqueReport | None = None
        last_response: TurnResponse | None = None

        for turn in range(self.settings.critic_max_turns):
            _raise_if_cancelled(cancellation_token, "critic.before_model_turn")
            try:
                request_kwargs: dict[str, Any] = {
                    "system": self._system(),
                    "messages": messages,
                    "tools": _CRITIC_TOOL_SCHEMAS,
                    "thinking_budget": thinking_budget,
                    "max_tokens": max_tokens,
                }
                if (
                    cancellation_token is not None
                    and getattr(cancellation_token, "can_cancel", True)
                ):
                    request_kwargs["cancellation_token"] = cancellation_token
                resp: TurnResponse = self.backend.create_turn(**request_kwargs)
            except Exception as e:
                log("critic.api_error", iter=iteration, turn=turn + 1,
                    error=f"{type(e).__name__}: {e}")
                terminal_report = _build_failsafe_report(
                    iteration=iteration,
                    summary=f"critic api error: {type(e).__name__}: {e}",
                )
                break

            _raise_if_cancelled(cancellation_token, "critic.after_model_turn")
            last_response = resp
            self.backend.append_assistant(messages, resp)
            _raise_if_cancelled(cancellation_token, "critic.after_append_assistant")

            tool_results_for_api: list[tuple[str, str, bool]] = []
            pending_vision: list[_VisionAttachment] = []
            image_budget = self.settings.critic_max_images_per_turn
            for tc in resp.tool_calls:
                _raise_if_cancelled(cancellation_token, "critic.before_tool")
                allow_image = (
                    tc.name == "read_slide_render"
                    and len(pending_vision) < image_budget
                )
                payload, is_err, terminal, attachment = self._dispatch_tool(
                    tc, slide_index=slide_index,
                    paper_raw_text=paper_raw_text,
                    claim_graph=claim_graph,
                    iteration=iteration,
                    allow_image=allow_image,
                    image_budget=image_budget,
                )
                _raise_if_cancelled(cancellation_token, "critic.after_tool")
                tool_results_for_api.append((tc.id, payload, is_err))
                if attachment is not None:
                    pending_vision.append(attachment)
                if terminal is not None:
                    terminal_report = terminal

            if terminal_report is not None:
                break

            if tool_results_for_api:
                _raise_if_cancelled(cancellation_token, "critic.before_append_tool_results")
                self.backend.append_tool_results(messages, tool_results_for_api)
                if pending_vision:
                    _append_vision_messages(
                        backend=self.backend,
                        messages=messages,
                        attachments=pending_vision,
                    )
                _raise_if_cancelled(cancellation_token, "critic.after_append_tool_results")
                continue

            if resp.stop_reason == "end_turn":
                log("critic.end_turn_no_verdict",
                    iter=iteration, turn=turn + 1)
                break

        if terminal_report is None:
            terminal_report = _build_failsafe_report(
                iteration=iteration,
                summary=(
                    f"critic max_turns ({self.settings.critic_max_turns}) "
                    "hit without report_verdict; synthesized fail"
                ),
            )

        wall_s = round(time.monotonic() - wall_start, 2)
        usage = (last_response.usage if last_response is not None else {}) or {}
        log("critic.done", iter=iteration, model=model,
            verdict=terminal_report.verdict, score=terminal_report.score,
            n_issues=len(terminal_report.issues),
            input_tokens=usage.get("input", 0),
            output_tokens=usage.get("output", 0),
            wall_s=wall_s)
        _raise_if_cancelled(cancellation_token, "critic.before_result")
        return terminal_report

    def _dispatch_tool(
        self,
        tc: ToolCall,
        *,
        slide_index: dict[str, Path],
        paper_raw_text: str | None,
        claim_graph: ClaimGraph | None,
        iteration: int,
        allow_image: bool = True,
        image_budget: int = 0,
    ) -> tuple[str, bool, CritiqueReport | None, _VisionAttachment | None]:
        """Returns (json_payload, is_error, terminal_report, vision_attachment).

        For `read_slide_render` the JSON payload is a small ack — the
        actual base64 image rides on a separate `_VisionAttachment` so
        the caller can deliver it as a real vision content block on a
        follow-up user message instead of stuffing 12k+ tokens of base64
        into a `tool` role message that subsequent turns must replay.
        """
        if tc.name == "read_slide_render":
            slide_id = str(tc.input.get("slide_id", ""))
            path = slide_index.get(slide_id)
            if path is None or not path.exists():
                msg = (
                    f"slide_id={slide_id!r} not found. Available: "
                    f"{sorted(slide_index.keys())[:20]}"
                )
                return json.dumps({"error": msg}), True, None, None
            if not allow_image:
                ack = json.dumps({
                    "slide_id": slide_id,
                    "deferred": True,
                    "reason": (
                        f"per-turn image budget ({image_budget}) exhausted; "
                        "call read_slide_render again on a later turn to "
                        "fetch this slide"
                    ),
                })
                return ack, False, None, None
            try:
                b64, media_type = _downscale_b64(
                    path, self.settings.critic_preview_max_edge,
                )
            except Exception as e:
                return json.dumps({"error": f"{type(e).__name__}: {e}"}), True, None, None
            ack = json.dumps({
                "slide_id": slide_id,
                "media_type": media_type,
                "delivered_as": "user_image_block",
                "image_b64_len": len(b64),
            })
            return ack, False, None, _VisionAttachment(
                slide_id=slide_id, image_b64=b64, media_type=media_type,
            )

        if tc.name == "read_paper_section":
            query = str(tc.input.get("query", "")).strip()
            excerpt = _extract_paper_excerpt(paper_raw_text, query)
            return (
                json.dumps({"query": query, "excerpt": excerpt}),
                False, None, None,
            )

        if tc.name == "lookup_claim_node":
            claim_id = str(tc.input.get("claim_id", "")).strip()
            if claim_graph is None:
                return (
                    json.dumps({
                        "error": "no claim_graph attached to this run",
                        "claim_id": claim_id,
                    }),
                    True, None, None,
                )
            node = _find_claim_node(claim_graph, claim_id)
            if node is None:
                return (
                    json.dumps({
                        "error": f"unknown claim_id {claim_id!r}",
                        "available_ids": _list_claim_ids(claim_graph),
                    }),
                    True, None, None,
                )
            return (
                json.dumps({
                    "claim_id": claim_id,
                    "kind": node["kind"],
                    "node": node["node"],
                }, ensure_ascii=False),
                False, None, None,
            )

        if tc.name == "report_verdict":
            try:
                payload = dict(tc.input)
                payload.setdefault("iteration", iteration)
                payload.setdefault("issues", [])
                report = CritiqueReport.model_validate(payload)
            except ValidationError as e:
                err_msg = (
                    "report_verdict failed schema: "
                    f"{e.errors(include_url=False)[:3]}"
                )
                return json.dumps({"error": err_msg}), True, None, None
            ack = json.dumps({
                "verdict": report.verdict, "score": report.score,
                "ack": "verdict recorded; loop will exit",
            })
            return ack, False, report, None

        return (
            json.dumps({"error": f"unknown tool: {tc.name}"}),
            True, None, None,
        )


# ─────────────────────────── helpers ───────────────────────────────────────


def _index_renders(slide_renders: list[Path], spec: DesignSpec) -> dict[str, Path]:
    """Map slide_id → PNG path so the read_slide_render tool can resolve.

    Deck: pair each slide_renders[i] with the i-th `kind="slide"` node from
    the spec's layer_graph (composite writes them in order). Poster /
    landing: register the synthetic desktop key; poster uses `poster_full`.
    """
    if not slide_renders:
        return {}
    if spec.artifact_type == ArtifactType.DECK:
        slides = [n for n in (spec.layer_graph or [])
                  if getattr(n, "kind", None) == "slide"]
        idx: dict[str, Path] = {}
        for i, render in enumerate(slide_renders):
            if i < len(slides):
                idx[slides[i].layer_id] = render
            idx[f"slide_{i:02d}"] = render
        return idx
    if spec.artifact_type == ArtifactType.LANDING:
        return {"landing_full": slide_renders[0]}
    return {"poster_full": slide_renders[0]}


def _find_claim_node(
    graph: ClaimGraph, claim_id: str,
) -> dict[str, Any] | None:
    """Locate a node by id across all four lists. Returns
    {"kind": "tension"|"mechanism"|"evidence"|"implication",
     "node": <serialized dict>} or None when no match."""
    for tension in graph.tensions:
        if tension.id == claim_id:
            return {"kind": "tension",
                    "node": tension.model_dump(mode="json")}
    for mech in graph.mechanisms:
        if mech.id == claim_id:
            return {"kind": "mechanism",
                    "node": mech.model_dump(mode="json")}
    for ev in graph.evidence:
        if ev.id == claim_id:
            return {"kind": "evidence",
                    "node": ev.model_dump(mode="json")}
    for impl in graph.implications:
        if impl.id == claim_id:
            return {"kind": "implication",
                    "node": impl.model_dump(mode="json")}
    return None


def _list_claim_ids(graph: ClaimGraph) -> dict[str, list[str]]:
    """Compact id catalog for the lookup_claim_node error path."""
    return {
        "tensions": [t.id for t in graph.tensions],
        "mechanisms": [m.id for m in graph.mechanisms],
        "evidence": [e.id for e in graph.evidence],
        "implications": [i.id for i in graph.implications],
    }


def _build_user_text(
    *,
    spec: DesignSpec,
    layer_manifest: list[dict[str, Any]],
    slide_index: dict[str, Path],
    paper_raw_text: str | None,
    claim_graph: ClaimGraph | None,
    design_feedback: dict[str, Any] | None,
    skill_context: str | None,
    iteration: int,
    max_iters: int,
    max_images_per_turn: int,
) -> str:
    available_ids = sorted(slide_index.keys())
    paper_blurb = (
        f"paper_raw_text available — {len(paper_raw_text):,} chars. "
        "Use `read_paper_section(query)` to pull short excerpts before "
        "flagging provenance issues. The paper itself is NEVER preloaded "
        "into your context — every excerpt costs you one tool call."
        if paper_raw_text
        else "paper_raw_text NOT available (free-text brief)."
    )
    if claim_graph is not None:
        claim_blurb = (
            "claim_graph: present (v2.8.0). thesis="
            f"{claim_graph.thesis!r}. "
            f"tensions={[t.id for t in claim_graph.tensions]}; "
            f"mechanisms={[m.id for m in claim_graph.mechanisms]}; "
            f"evidence={[e.id for e in claim_graph.evidence]}; "
            f"implications={[i.id for i in claim_graph.implications]}. "
            "Cross-check `slide.covers` against these ids — any "
            "tension/mechanism with no slide.covers reference is a "
            "claim_coverage issue. Use `lookup_claim_node(claim_id)` "
            "to inspect a specific node."
        )
    else:
        claim_blurb = "claim_graph: not available (v2.7.3 baseline)."
    feedback_payload = _compact_design_feedback(design_feedback)
    feedback_blurb = (
        "design_feedback: not available for this composite."
        if feedback_payload is None
        else (
            "design_feedback: present. Treat severity='blocker' and P0-derived "
            "findings as hard environment validation failures, not optional "
            "style advice. If unresolved, do not pass the artifact.\n"
            "```json\n"
            f"{json.dumps(feedback_payload, ensure_ascii=False, indent=2)}\n"
            "```"
        )
    )
    spec_json = json.dumps(spec.model_dump(mode="json"),
                           ensure_ascii=False, indent=2)
    manifest_json = json.dumps(layer_manifest, ensure_ascii=False, indent=2)
    skill_block = (
        f"## Runtime skill context\n{skill_context.strip()}\n\n"
        if skill_context and skill_context.strip()
        else ""
    )
    return (
        f"## Critique iteration {iteration} of {max_iters}\n\n"
        f"{skill_block}"
        f"## Brief\n{spec.brief}\n\n"
        f"## Artifact type\n{spec.artifact_type.value}\n\n"
        f"## Renders available\n"
        f"slide_ids you may pass to `read_slide_render`: "
        f"{available_ids}\n"
        f"per-turn image cap: at most {max_images_per_turn} PNGs delivered "
        "per assistant turn. Surplus calls return "
        "`{\"deferred\": true}` and you must refetch them on a later "
        "turn — chunk your inspection across multiple turns rather than "
        "asking for the whole deck at once.\n\n"
        f"## Source material\n{paper_blurb}\n{claim_blurb}\n\n"
        f"## Environment design_feedback\n{feedback_blurb}\n\n"
        f"## DesignSpec snapshot\n```json\n{spec_json}\n```\n\n"
        f"## Composited layer manifest\n```json\n{manifest_json}\n```\n\n"
        "Begin your evaluation. Use `read_slide_render` for each slide you "
        "need to inspect visually, `read_paper_section` to verify any "
        "quotes / numbers, and FINISH with exactly one `report_verdict` "
        "call. Do not emit a verdict in plain text — only via the tool."
    )


def _compact_design_feedback(value: dict[str, Any] | None) -> dict[str, Any] | None:
    """Keep environment feedback small enough for critic context."""
    if not isinstance(value, dict):
        return None
    findings = value.get("findings")
    compact_findings: list[dict[str, Any]] = []
    if isinstance(findings, list):
        for finding in findings[:20]:
            if not isinstance(finding, dict):
                continue
            compact_findings.append({
                "id": finding.get("id"),
                "source": finding.get("source"),
                "severity": finding.get("severity"),
                "message": _truncate_text(finding.get("message"), 500),
                "target": finding.get("target") or {},
                "evidence": _truncate_jsonable(finding.get("evidence") or {}, 800),
                "suggested_action": _truncate_text(
                    finding.get("suggested_action"), 500,
                ),
                "repairable": finding.get("repairable", True),
            })
    compact = {
        "artifact_type": value.get("artifact_type"),
        "iteration": value.get("iteration"),
        "counts": value.get("counts") or {},
        "has_blocking_findings": bool(value.get("has_blocking_findings")),
        "findings": compact_findings,
        "findings_truncated": (
            len(findings) - len(compact_findings)
            if isinstance(findings, list) and len(findings) > len(compact_findings)
            else 0
        ),
    }
    if isinstance(value.get("poster_plan_contract"), dict):
        compact["poster_plan_contract"] = _truncate_jsonable(value.get("poster_plan_contract"), 12000)
    if isinstance(value.get("latest_critic_scorecard"), dict):
        compact["latest_critic_scorecard"] = _truncate_jsonable(value.get("latest_critic_scorecard"), 4000)
    return compact


def _truncate_text(value: Any, max_chars: int) -> str:
    text = "" if value is None else str(value)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"...[truncated {len(text) - max_chars} chars]"


def _truncate_jsonable(value: Any, max_chars: int) -> Any:
    try:
        blob = json.dumps(value, ensure_ascii=False)
    except TypeError:
        return _truncate_text(value, max_chars)
    if len(blob) <= max_chars:
        return value
    return _truncate_text(blob, max_chars)


def _build_failsafe_report(*, iteration: int, summary: str) -> CritiqueReport:
    return CritiqueReport(
        score=0.0,
        verdict="fail",
        issues=[CritiqueIssue(
            slide_id=None,
            severity="blocker",
            category="layout",
            description=summary,
            evidence_paper_anchor=None,
        )],
        summary=summary,
        iteration=iteration,
    )


def _summarize_tool_input(name: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Strip large blobs (paper text, image base64) from tool inputs before
    logging or echoing. Critic tool inputs are small (slide_id / query /
    verdict payload) so this is mostly defensive."""
    out = dict(raw or {})
    for key, val in list(out.items()):
        if isinstance(val, str) and len(val) > 1000:
            out[key] = val[:1000] + f"…[truncated {len(val) - 1000} chars]"
    return out

_WS_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_]{2,}")


def _extract_paper_excerpt(raw: str | None, query: str) -> str:
    """Whitespace-tolerant substring search with windowed context.

    Mirrors `util.claim_graph_validator._norm_ws` semantics so any query
    the critic forms when verifying provenance matches iff the same string
    would pass `validate_claim_graph` — closes the symmetric bug fixed in
    `claim_graph_extractor._extract_paper_excerpt` (PDF newlines / double-
    spaces broke strict substring lookup).

    Two-tier fallback: longest single token, then empty. Hard-capped at
    `_PAPER_EXCERPT_HARD_CAP_CHARS` regardless of window math — defense
    against extreme queries that would exfiltrate the whole paper.
    """
    if not raw or not query:
        return ""

    norm_query = _WS_RE.sub(" ", query).strip().lower()
    if not norm_query:
        return ""

    norm_raw, mapping = _norm_ws_with_mapping(raw)
    haystack_lower = norm_raw.lower()
    pos = haystack_lower.find(norm_query)

    if pos < 0:
        tokens = sorted(
            _TOKEN_RE.findall(norm_query), key=len, reverse=True,
        )
        for tok in tokens[:5]:
            tok_pos = haystack_lower.find(tok)
            if tok_pos >= 0:
                pos = tok_pos
                norm_query = tok
                break

    if pos < 0:
        return ""

    raw_pos = mapping[pos] if pos < len(mapping) else mapping[-1]
    end_norm = pos + len(norm_query)
    raw_end_anchor = (
        mapping[end_norm - 1] + 1 if end_norm - 1 < len(mapping)
        else len(raw)
    )

    window = 1500
    start = max(0, raw_pos - window)
    end = min(len(raw), raw_end_anchor + window)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(raw) else ""
    excerpt = f"{prefix}{raw[start:end]}{suffix}"
    if len(excerpt) > _PAPER_EXCERPT_HARD_CAP_CHARS:
        excerpt = excerpt[:_PAPER_EXCERPT_HARD_CAP_CHARS] + "…"
    return excerpt


def _norm_ws_with_mapping(raw: str) -> tuple[str, list[int]]:
    """Normalize whitespace + return per-char index mapping back to raw."""
    out_chars: list[str] = []
    mapping: list[int] = []
    in_ws = False
    started = False
    for i, ch in enumerate(raw):
        if ch.isspace():
            if started and not in_ws:
                out_chars.append(" ")
                mapping.append(i)
                in_ws = True
        else:
            out_chars.append(ch)
            mapping.append(i)
            in_ws = False
            started = True
    while out_chars and out_chars[-1] == " ":
        out_chars.pop()
        mapping.pop()
    return "".join(out_chars), mapping


def _append_vision_messages(
    *,
    backend: LLMBackend,
    messages: list[Any],
    attachments: list[_VisionAttachment],
) -> None:
    """Deliver every pending PNG as ONE follow-up user message containing
    interleaved (text, image) content blocks — one (slide_id label,
    image) pair per attachment.

    v2.7.4 — collapsed from N separate user messages into a single user
    message. Strict OpenAI-compat upstreams (Alibaba-routed
    `qwen/qwen-vl-max`, observed 2026-04-26) reject sequences of
    adjacent same-role messages once the conversation accumulates four
    turns of `assistant(tool_calls) → tool* → user* → user*` cycles.
    The OpenAI vision spec puts multiple images in one user message via
    a multi-block `content` array; that is now what we emit. The first
    backend.vision_user_message call gives us a canonical single-image
    skeleton; we then extend its content array with the rest of the
    attachments in (text, image) pair order so the model can still tell
    which slide_id each image belongs to.
    """
    if not attachments:
        return
    head = backend.vision_user_message(
        image_b64=attachments[0].image_b64,
        media_type=attachments[0].media_type,
        text=f"[render of slide_id={attachments[0].slide_id}]",
    )
    if len(attachments) == 1:
        messages.append(head)
        return
    if not isinstance(head, dict) or not isinstance(head.get("content"), list):
        for att in attachments:
            messages.append(backend.vision_user_message(
                image_b64=att.image_b64,
                media_type=att.media_type,
                text=f"[render of slide_id={att.slide_id}]",
            ))
        return
    for att in attachments[1:]:
        sibling = backend.vision_user_message(
            image_b64=att.image_b64,
            media_type=att.media_type,
            text=f"[render of slide_id={att.slide_id}]",
        )
        sibling_content = sibling.get("content")
        if isinstance(sibling_content, list):
            head["content"].extend(sibling_content)
        else:
            head["content"].append(
                {"type": "text", "text": f"[render of slide_id={att.slide_id}]"},
            )
    messages.append(head)


def _downscale_b64(path: Path, max_edge: int) -> tuple[str, str]:
    """Open `path`, downscale to `max_edge` longest-side, return base64
    JPEG. Mirrors the legacy `critic._downscale_b64` so the new sub-agent
    has the same OOM-safety contract."""
    img = Image.open(path)
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > max_edge:
        if w >= h:
            new = (max_edge, int(h * max_edge / w))
        else:
            new = (int(w * max_edge / h), max_edge)
        img = img.resize(new, Image.LANCZOS)
    if img.mode == "RGBA":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return base64.b64encode(buf.getvalue()).decode("ascii"), "image/jpeg"
