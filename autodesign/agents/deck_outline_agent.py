"""DeckOutlineAgent — source-aware deck length and outline designer."""

from __future__ import annotations

import json
import re
import time
from typing import Any

from ..config import Settings
from ..llm_backend import LLMBackend, ToolCall, TurnResponse, make_backend
from ..run_control import CancellationToken
from ..util.deck_planner import (
    DeckPlanDict,
    build_document_signals,
    fallback_deck_plan,
    validate_deck_plan_report,
)
from ..util.logging import log


_DECK_OUTLINE_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "lookup_manifest_item",
        "description": (
            "Look up a compact excerpt from the ingested source manifest, "
            "figure/table catalog, or claim graph by keyword before deciding "
            "slide coverage."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "report_deck_plan",
        "description": (
            "Final-submission tool. Emit exactly one refined DeckPlan. "
            "`outline.length` MUST equal `slide_count`. Visual refs must be "
            "registered ingest layer ids or allowed generated refs such as "
            "`generated:cover`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "artifact_type": {"type": "string", "enum": ["deck"]},
                "deck_subtype": {"type": "string"},
                "talk_profile": {
                    "type": "string",
                    "enum": [
                        "short_overview",
                        "standard_conference",
                        "full_formal",
                    ],
                },
                "slide_count": {"type": "integer", "minimum": 1, "maximum": 60},
                "count_range": {"type": "array", "items": {"type": "integer"}},
                "lock_level": {"type": "string", "enum": ["hard", "soft", "advisory"]},
                "status": {"type": "string", "enum": ["refined", "fallback", "explicit"]},
                "density_budget": {"type": "object"},
                "rationale": {"type": "string"},
                "source": {"type": "string"},
                "outline": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "slide_index": {"type": "integer"},
                            "title": {"type": "string"},
                            "role": {"type": "string"},
                            "chapter": {"type": "string"},
                            "communication_job": {"type": "string"},
                            "assertion_title": {"type": "string"},
                            "scope": {"type": "string"},
                            "layout_family": {"type": "string"},
                            "content": {"type": "string"},
                            "visual_refs": {"type": "array", "items": {"type": "string"}},
                            "evidence_refs": {"type": "array", "items": {"type": "string"}},
                            "speaker_note": {"type": ["string", "null"]},
                            "speaker_note_intent": {"type": "string"},
                        },
                        "required": ["slide_index", "title", "role", "content"],
                    },
                },
                "document_signals": {"type": "object"},
            },
            "required": [
                "artifact_type", "deck_subtype", "talk_profile", "slide_count", "count_range",
                "lock_level", "status", "density_budget", "rationale",
                "source", "outline", "document_signals",
            ],
        },
    },
]


def _raise_if_cancelled(
    cancellation_token: CancellationToken | None,
    phase: str,
) -> None:
    if cancellation_token is not None:
        cancellation_token.raise_if_cancelled(phase)


class DeckOutlineAgent:
    """Forked LLM agent that decides deck length after document ingest."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.backend: LLMBackend = make_backend(
            settings, settings.deck_outline_model, role="deck_outline",
        )
        self._system_prompt: str | None = None

    def _system(self) -> str:
        if self._system_prompt is None:
            self._system_prompt = (
                self.settings.prompts_dir / "deck_outline_agent.md"
            ).read_text(encoding="utf-8")
        return self._system_prompt

    def plan(
        self,
        *,
        raw_brief: str,
        enhanced_brief: str,
        base_plan: DeckPlanDict | None,
        summaries: list[dict[str, Any]],
        rendered_layers: dict[str, dict[str, Any]],
        figures_payload: list[dict[str, Any]],
        tables_payload: list[dict[str, Any]],
        claim_graph: Any | None,
        cancellation_token: CancellationToken | None = None,
    ) -> DeckPlanDict:
        _raise_if_cancelled(cancellation_token, "deck_outline.start")
        signals = build_document_signals(summaries, rendered_layers)
        known_refs = set(signals.get("registered_figure_ids") or [])
        known_refs.update(signals.get("registered_table_ids") or [])
        context = _build_context(
            raw_brief=raw_brief,
            enhanced_brief=enhanced_brief,
            base_plan=base_plan,
            summaries=summaries,
            figures_payload=figures_payload,
            tables_payload=tables_payload,
            claim_graph=claim_graph,
            signals=signals,
        )

        log(
            "deck_outline.start",
            model=self.backend.model,
            backend=self.backend.name,
            n_figures=signals.get("n_registered_figures"),
            n_tables=signals.get("n_registered_tables"),
            n_sections=signals.get("n_sections"),
            max_turns=self.settings.deck_outline_max_turns,
        )
        wall_start = time.monotonic()
        messages: list[Any] = [{"role": "user", "content": context}]
        thinking_budget = self.settings.deck_outline_thinking_budget
        max_tokens = max(8192, thinking_budget + 4096) if thinking_budget > 0 else 12288
        terminal_plan: DeckPlanDict | None = None
        last_response: TurnResponse | None = None

        for turn in range(self.settings.deck_outline_max_turns):
            _raise_if_cancelled(cancellation_token, "deck_outline.before_model_turn")
            try:
                request_kwargs: dict[str, Any] = {
                    "system": self._system(),
                    "messages": messages,
                    "tools": _DECK_OUTLINE_TOOL_SCHEMAS,
                    "thinking_budget": thinking_budget,
                    "max_tokens": max_tokens,
                }
                if (
                    cancellation_token is not None
                    and getattr(cancellation_token, "can_cancel", True)
                ):
                    request_kwargs["cancellation_token"] = cancellation_token
                resp = self.backend.create_turn(**request_kwargs)
            except Exception as e:
                log("deck_outline.api_error", turn=turn + 1, error=f"{type(e).__name__}: {e}")
                break

            _raise_if_cancelled(cancellation_token, "deck_outline.after_model_turn")
            last_response = resp
            self.backend.append_assistant(messages, resp)
            _raise_if_cancelled(cancellation_token, "deck_outline.after_append_assistant")
            tool_results: list[tuple[str, str, bool]] = []
            for tc in resp.tool_calls:
                _raise_if_cancelled(cancellation_token, "deck_outline.before_tool")
                payload, is_error, plan = self._dispatch_tool(
                    tc,
                    context=context,
                    known_visual_refs=known_refs,
                    signals=signals,
                )
                _raise_if_cancelled(cancellation_token, "deck_outline.after_tool")
                tool_results.append((tc.id, payload, is_error))
                if plan is not None:
                    terminal_plan = plan

            if terminal_plan is not None:
                break
            if tool_results:
                _raise_if_cancelled(cancellation_token, "deck_outline.before_append_tool_results")
                self.backend.append_tool_results(messages, tool_results)
                _raise_if_cancelled(cancellation_token, "deck_outline.after_append_tool_results")
                continue
            if resp.stop_reason == "end_turn":
                if _looks_like_kimi_template_leak(resp) and turn + 1 < self.settings.deck_outline_max_turns:
                    _raise_if_cancelled(cancellation_token, "deck_outline.validation_retry")
                    messages.append({
                        "role": "user",
                        "content": (
                            "Your previous turn emitted tool-call template text "
                            "instead of a structured tool call. Retry now with "
                            "`lookup_manifest_item` or `report_deck_plan`."
                        ),
                    })
                    continue
                log("deck_outline.end_turn_no_report", turn=turn + 1)
                break

        if terminal_plan is None:
            terminal_plan = fallback_deck_plan(
                base_plan,
                raw_brief=raw_brief,
                summaries=summaries,
                rendered_layers=rendered_layers,
                claim_graph=claim_graph,
                reason="deck outline agent did not return a valid report",
            )

        usage = (last_response.usage if last_response is not None else {}) or {}
        wall_s = round(time.monotonic() - wall_start, 2)
        log(
            "deck_outline.done",
            model=self.backend.model,
            source=terminal_plan.get("source"),
            status=terminal_plan.get("status"),
            slide_count=terminal_plan.get("slide_count"),
            input_tokens=usage.get("input", 0),
            output_tokens=usage.get("output", 0),
            wall_s=wall_s,
        )
        _raise_if_cancelled(cancellation_token, "deck_outline.before_result")
        return terminal_plan

    def _dispatch_tool(
        self,
        tc: ToolCall,
        *,
        context: str,
        known_visual_refs: set[str],
        signals: dict[str, Any],
    ) -> tuple[str, bool, DeckPlanDict | None]:
        if tc.name == "lookup_manifest_item":
            query = str(tc.input.get("query") or "").strip()
            return json.dumps({
                "query": query,
                "excerpt": _lookup_context(context, query),
            }, ensure_ascii=False), False, None

        if tc.name == "report_deck_plan":
            payload = dict(tc.input)
            payload["artifact_type"] = "deck"
            payload["status"] = payload.get("status") or "refined"
            payload["source"] = "outline_agent"
            document_signals = payload.get("document_signals")
            if not isinstance(document_signals, dict):
                document_signals = {}
            payload["document_signals"] = {**signals, **document_signals}
            model, errors = validate_deck_plan_report(
                payload,
                known_visual_refs=known_visual_refs,
            )
            if errors or model is None:
                return json.dumps({
                    "error": "report_deck_plan failed validation",
                    "validation_errors": errors[:10],
                    "known_visual_ref_examples": sorted(known_visual_refs)[:20],
                    "instruction": (
                        "Fix slide_count/outline length and use only known "
                        "ingest ids or generated:* refs, then report again."
                    ),
                }, ensure_ascii=False), True, None
            plan = model.model_dump(mode="json")
            plan["source"] = "outline_agent"
            plan["status"] = "refined"
            return json.dumps({
                "ack": "deck_plan recorded; loop will exit",
                "slide_count": plan.get("slide_count"),
                "outline_items": len(plan.get("outline") or []),
            }), False, plan

        return json.dumps({"error": f"unknown tool: {tc.name}"}), True, None


def _build_context(
    *,
    raw_brief: str,
    enhanced_brief: str,
    base_plan: DeckPlanDict | None,
    summaries: list[dict[str, Any]],
    figures_payload: list[dict[str, Any]],
    tables_payload: list[dict[str, Any]],
    claim_graph: Any | None,
    signals: dict[str, Any],
) -> str:
    claim = claim_graph.model_dump(mode="json") if hasattr(claim_graph, "model_dump") else claim_graph
    compact = {
        "raw_user_brief": raw_brief,
        "enhanced_brief_excerpt": (enhanced_brief or "")[:5000],
        "base_deck_plan": base_plan or {},
        "document_signals": signals,
        "files": [_compact_summary(s) for s in summaries],
        "shown_figures_for_planner": figures_payload[:30],
        "shown_tables_for_planner": tables_payload[:20],
        "claim_graph": claim,
    }
    return (
        "## Deck outline planning request\n\n"
        "Decide the exact slide count and source-backed outline for this deck. "
        "The raw_user_brief is the only source for explicit hard slide counts; "
        "do not treat enhanced_brief slide counts as user locks.\n\n"
        "```json\n"
        f"{json.dumps(compact, ensure_ascii=False, indent=2)[:28000]}\n"
        "```\n\n"
        "Use lookup_manifest_item if you need context, then finish with "
        "report_deck_plan."
    )


def _compact_summary(summary: dict[str, Any]) -> dict[str, Any]:
    manifest = summary.get("manifest") or {}
    out = {
        "file": summary.get("file"),
        "type": summary.get("type"),
        "n_registered_figures": len(summary.get("registered_figure_ids") or summary.get("registered_layer_ids") or []),
        "n_registered_tables": len(summary.get("registered_table_ids") or []),
        "recommended_figures": summary.get("recommended_figures"),
        "figure_catalog_summary": summary.get("figure_catalog_summary"),
    }
    if isinstance(manifest, dict):
        out["title"] = manifest.get("title")
        out["authors"] = manifest.get("authors")
        out["sections"] = [
            {
                "heading": s.get("heading"),
                "summary": s.get("summary"),
                "key_points": s.get("key_points"),
            }
            for s in (manifest.get("sections") or [])[:20]
            if isinstance(s, dict)
        ]
    return out


def _lookup_context(context: str, query: str) -> str:
    if not query:
        return ""
    haystack = context
    lowered = haystack.lower()
    q = query.lower().strip()
    pos = lowered.find(q)
    if pos < 0:
        tokens = sorted(re.findall(r"[A-Za-z0-9_\-]{3,}", q), key=len, reverse=True)
        for token in tokens[:5]:
            pos = lowered.find(token)
            if pos >= 0:
                break
    if pos < 0:
        return ""
    start = max(0, pos - 1400)
    end = min(len(haystack), pos + 2200)
    return ("..." if start else "") + haystack[start:end] + ("..." if end < len(haystack) else "")


_KIMI_LEAK_MARKERS = (
    "<|tool_calls_section_begin|>",
    "<|tool_call_begin|>",
    "<|tool_calls_section_end|>",
)


def _looks_like_kimi_template_leak(resp: TurnResponse) -> bool:
    if resp.tool_calls:
        return False
    haystacks = [resp.text or ""]
    for block in resp.thinking_blocks or []:
        haystacks.append(getattr(block, "thinking", "") or str(block))
    blob = "\n".join(haystacks)
    return any(marker in blob for marker in _KIMI_LEAK_MARKERS)
