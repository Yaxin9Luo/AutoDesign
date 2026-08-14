"""PaperMemoryAgent — LLM-curated evidence dossier over paper_memory."""

from __future__ import annotations

import json
import time
from typing import Any

from ..config import Settings
from ..llm_backend import LLMBackend, ToolCall, TurnResponse, make_backend
from ..run_control import CancellationToken
from ..util.logging import log
from ..util.paper_memory import paper_memory_markdown, retrieve_paper_context
from ..util.paper_memory_dossier import validate_paper_memory_dossier


_PAPER_MEMORY_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "lookup_paper_memory",
        "description": (
            "Look up canonical paper-memory chunks before writing dossier "
            "sections. Returns chunk ids, quotes, pages, categories, and source ids."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "panel_role": {"type": "string"},
                "categories": {"type": "array", "items": {"type": "string"}},
                "evidence_kind": {"type": "array", "items": {"type": "string"}},
                "top_k": {"type": "integer", "minimum": 1},
            },
            "required": ["query"],
        },
    },
    {
        "name": "report_paper_memory_dossier",
        "description": (
            "Final-submission tool. Emit one validated paper-memory dossier. "
            "Every evidence_refs[].chunk_id must refer to an existing canonical "
            "paper_memory chunk; quote must be copied from that chunk."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["paper_memory_dossier"]},
                "version": {"type": "integer"},
                "source_memory_cache_key": {"type": "string"},
                "model": {"type": "string"},
                "sections": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "panel_role": {"type": "string"},
                            "title": {"type": "string"},
                            "claim": {"type": "string"},
                            "poster_copy_suggestion": {"type": "string"},
                            "visual_ids": {"type": "array", "items": {"type": "string"}},
                            "confidence": {"type": "number"},
                            "evidence_refs": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "chunk_id": {"type": "string"},
                                        "page": {"type": ["integer", "null"]},
                                        "section": {"type": ["string", "null"]},
                                        "source_id": {"type": ["string", "null"]},
                                        "parent_source_id": {"type": ["string", "null"]},
                                        "evidence_kind": {"type": "string"},
                                        "safe_to_quote": {"type": "boolean"},
                                        "quote": {"type": "string"},
                                    },
                                    "required": ["chunk_id", "quote"],
                                },
                            },
                        },
                        "required": [
                            "id", "panel_role", "title", "claim",
                            "poster_copy_suggestion", "evidence_refs",
                        ],
                    },
                },
            },
            "required": ["kind", "version", "source_memory_cache_key", "sections"],
        },
    },
]


def _raise_if_cancelled(
    cancellation_token: CancellationToken | None,
    phase: str,
) -> None:
    if cancellation_token is not None:
        cancellation_token.raise_if_cancelled(phase)


class PaperMemoryAgent:
    """Forked sub-agent that curates panel-ready paper evidence."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.backend: LLMBackend = make_backend(
            settings,
            settings.paper_memory_model,
            role="paper_memory",
        )
        self._system_prompt: str | None = None

    def _system(self) -> str:
        if self._system_prompt is None:
            self._system_prompt = (
                self.settings.prompts_dir / "paper_memory_agent.md"
            ).read_text(encoding="utf-8")
        return self._system_prompt

    def build(
        self,
        *,
        memory: dict[str, Any],
        manifest: dict[str, Any] | None = None,
        visual_provenance: dict[str, Any] | None = None,
        recommended_text_units: dict[str, Any] | None = None,
        recommended_figures: dict[str, Any] | None = None,
        cancellation_token: CancellationToken | None = None,
    ) -> dict[str, Any]:
        _raise_if_cancelled(cancellation_token, "paper_memory.start")
        if not isinstance(memory, dict) or memory.get("kind") != "paper_memory":
            return {}

        log(
            "paper_memory_agent.start",
            model=self.backend.model,
            backend=self.backend.name,
            chunks=memory.get("chunk_count"),
            max_turns=self.settings.paper_memory_max_turns,
        )
        wall_start = time.monotonic()
        context = _build_context(
            memory=memory,
            manifest=manifest or {},
            visual_provenance=visual_provenance or {},
            recommended_text_units=recommended_text_units or {},
            recommended_figures=recommended_figures or {},
        )
        messages: list[Any] = [{"role": "user", "content": context}]
        thinking_budget = self.settings.paper_memory_thinking_budget
        max_tokens = max(6144, thinking_budget + 4096) if thinking_budget > 0 else 12288
        terminal: dict[str, Any] | None = None
        last_response: TurnResponse | None = None

        for turn in range(self.settings.paper_memory_max_turns):
            _raise_if_cancelled(cancellation_token, "paper_memory.before_model_turn")
            try:
                request_kwargs: dict[str, Any] = {
                    "system": self._system(),
                    "messages": messages,
                    "tools": _PAPER_MEMORY_TOOL_SCHEMAS,
                    "thinking_budget": thinking_budget,
                    "max_tokens": max_tokens,
                }
                if (
                    cancellation_token is not None
                    and getattr(cancellation_token, "can_cancel", True)
                ):
                    request_kwargs["cancellation_token"] = cancellation_token
                resp = self.backend.create_turn(**request_kwargs)
            except Exception as e:  # noqa: BLE001
                log(
                    "paper_memory_agent.api_error",
                    turn=turn + 1,
                    error=f"{type(e).__name__}: {e}",
                )
                break

            _raise_if_cancelled(cancellation_token, "paper_memory.after_model_turn")
            last_response = resp
            log(
                "paper_memory_agent.turn_output",
                turn=turn + 1,
                model=self.backend.model,
                backend=self.backend.name,
                stop_reason=resp.stop_reason,
                text_excerpt=_text_excerpt(resp.text, 1800),
                tool_calls=[
                    {
                        "name": tc.name,
                        "summary": _tool_call_summary(tc),
                    }
                    for tc in resp.tool_calls
                ],
                input_tokens=(resp.usage or {}).get("input", 0),
                output_tokens=(resp.usage or {}).get("output", 0),
            )
            self.backend.append_assistant(messages, resp)
            _raise_if_cancelled(cancellation_token, "paper_memory.after_append_assistant")
            tool_results: list[tuple[str, str, bool]] = []
            for tc in resp.tool_calls:
                _raise_if_cancelled(cancellation_token, "paper_memory.before_tool")
                payload, is_error, dossier = self._dispatch_tool(tc, memory=memory)
                _raise_if_cancelled(cancellation_token, "paper_memory.after_tool")
                log(
                    "paper_memory_agent.tool_call",
                    turn=turn + 1,
                    tool=tc.name,
                    summary=_tool_call_summary(tc),
                    result_summary=_tool_result_summary(tc.name, payload),
                    is_error=is_error,
                )
                tool_results.append((tc.id, payload, is_error))
                if dossier is not None:
                    terminal = dossier
            if terminal is not None:
                break
            if tool_results:
                _raise_if_cancelled(cancellation_token, "paper_memory.before_append_tool_results")
                self.backend.append_tool_results(messages, tool_results)
                _raise_if_cancelled(cancellation_token, "paper_memory.after_append_tool_results")
                continue
            if resp.stop_reason == "end_turn":
                if _looks_like_kimi_template_leak(resp) and turn + 1 < self.settings.paper_memory_max_turns:
                    _raise_if_cancelled(cancellation_token, "paper_memory.validation_retry")
                    messages.append({
                        "role": "user",
                        "content": (
                            "Your previous turn emitted tool-call template text "
                            "instead of a structured tool call. Retry now with "
                            "`lookup_paper_memory` or `report_paper_memory_dossier`."
                        ),
                    })
                    continue
                log("paper_memory_agent.end_turn_no_report", turn=turn + 1)
                break

        usage = (last_response.usage if last_response is not None else {}) or {}
        wall_s = round(time.monotonic() - wall_start, 2)
        if terminal is None:
            log(
                "paper_memory_agent.degraded",
                model=self.backend.model,
                reason="no_valid_dossier",
                input_tokens=usage.get("input", 0),
                output_tokens=usage.get("output", 0),
                wall_s=wall_s,
                last_text_excerpt=_text_excerpt(last_response.text if last_response is not None else "", 1200),
            )
            return {}

        terminal["model"] = terminal.get("model") or self.backend.model
        log(
            "paper_memory_agent.done",
            model=self.backend.model,
            sections=len(terminal.get("sections") or []),
            input_tokens=usage.get("input", 0),
            output_tokens=usage.get("output", 0),
            wall_s=wall_s,
            section_summaries=_dossier_section_summaries(terminal),
        )
        _raise_if_cancelled(cancellation_token, "paper_memory.before_result")
        return terminal

    def _dispatch_tool(
        self,
        tc: ToolCall,
        *,
        memory: dict[str, Any],
    ) -> tuple[str, bool, dict[str, Any] | None]:
        if tc.name == "lookup_paper_memory":
            query = str(tc.input.get("query") or "").strip()
            result = retrieve_paper_context(
                memory,
                query=query,
                panel_role=str(tc.input.get("panel_role") or "").strip() or None,
                categories=_as_str_list(tc.input.get("categories")),
                evidence_kind=_as_str_list(tc.input.get("evidence_kind")),
                top_k=int(tc.input["top_k"]) if tc.input.get("top_k") not in (None, "") else None,
            )
            return json.dumps(result, ensure_ascii=False), bool(result.get("error")), None

        if tc.name == "report_paper_memory_dossier":
            payload = dict(tc.input)
            payload["kind"] = "paper_memory_dossier"
            payload["version"] = 1
            payload["source_memory_cache_key"] = memory.get("cache_key")
            payload["model"] = payload.get("model") or self.backend.model
            dossier, errors = validate_paper_memory_dossier(
                memory,
                payload,
                model=self.backend.model,
            )
            if errors:
                return json.dumps({
                    "error": "paper memory dossier failed validation",
                    "validation_errors": errors[:20],
                    "instruction": (
                        "Retry with only existing chunk_id values and quote text "
                        "copied from those chunks. Do not invent pages or sources."
                    ),
                    "known_chunk_examples": _chunk_examples(memory),
                }, ensure_ascii=False), True, None
            return json.dumps({
                "ack": "paper memory dossier recorded; loop will exit",
                "sections": len(dossier.get("sections") or []),
            }, ensure_ascii=False), False, dossier

        return json.dumps({"error": f"unknown tool: {tc.name}"}), True, None


def _build_context(
    *,
    memory: dict[str, Any],
    manifest: dict[str, Any],
    visual_provenance: dict[str, Any],
    recommended_text_units: dict[str, Any],
    recommended_figures: dict[str, Any],
) -> str:
    compact = {
        "memory": {
            "cache_key": memory.get("cache_key"),
            "metadata": memory.get("metadata") or {},
            "chunk_count": memory.get("chunk_count"),
            "categories": memory.get("categories") or {},
            "evidence_kinds": memory.get("evidence_kinds") or {},
        },
        "manifest": _compact_manifest(manifest),
        "recommended_text_units": recommended_text_units,
        "recommended_figures": recommended_figures,
        "visual_provenance": _compact_visual_provenance(visual_provenance),
        "memory_projection": paper_memory_markdown(memory),
    }
    return (
        "## Paper memory dossier request\n\n"
        "Create a panel-ready evidence dossier for an academic paper poster. "
        "Use lookup_paper_memory for targeted checks. Finish with "
        "report_paper_memory_dossier only after every evidence ref uses an "
        "existing chunk_id and copied quote text.\n\n"
        "```json\n"
        f"{json.dumps(compact, ensure_ascii=False, indent=2)[:36000]}\n"
        "```"
    )


def _compact_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        return {}
    return {
        "title": manifest.get("title"),
        "authors": manifest.get("authors"),
        "abstract": manifest.get("abstract"),
        "sections": [
            {
                "title": s.get("title") or s.get("heading"),
                "summary": s.get("summary") or s.get("text") or s.get("description"),
                "page": s.get("page") or s.get("start_page"),
            }
            for s in (manifest.get("sections") or [])[:20]
            if isinstance(s, dict)
        ],
    }


def _compact_visual_provenance(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    return {
        "assets": [
            {
                "layer_id": item.get("layer_id"),
                "source_page": item.get("source_page"),
                "source_ref": item.get("source_ref"),
                "caption": item.get("caption") or item.get("caption_short"),
                "visual_role": item.get("visual_role"),
            }
            for item in (payload.get("assets") or [])[:30]
            if isinstance(item, dict)
        ],
    }


def _chunk_examples(memory: dict[str, Any]) -> list[dict[str, Any]]:
    examples = []
    for chunk in (memory.get("chunks") or [])[:12]:
        if not isinstance(chunk, dict):
            continue
        examples.append({
            "chunk_id": chunk.get("chunk_id") or chunk.get("id"),
            "category": chunk.get("category"),
            "page": chunk.get("page"),
            "source_id": chunk.get("source_id"),
            "quote": chunk.get("quote"),
        })
    return examples


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item or "").strip()]
    text = str(value).strip()
    return [text] if text else []


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


def _text_excerpt(text: str | None, max_chars: int) -> str:
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return ""
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max(0, max_chars - 1)].rstrip() + "…"


def _tool_call_summary(tc: ToolCall) -> str:
    data = tc.input or {}
    if tc.name == "lookup_paper_memory":
        query = _text_excerpt(str(data.get("query") or ""), 220)
        parts = [
            f"query: {query}" if query else "",
            f"role: {data.get('panel_role')}" if data.get("panel_role") else "",
            f"top_k: {data.get('top_k')}" if data.get("top_k") not in (None, "") else "",
        ]
        return " · ".join(part for part in parts if part)
    if tc.name == "report_paper_memory_dossier":
        sections = data.get("sections")
        if isinstance(sections, list):
            titles = [
                str(item.get("title") or item.get("panel_role") or "").strip()
                for item in sections[:5]
                if isinstance(item, dict)
            ]
            suffix = "…" if len(sections) > 5 else ""
            return f"{len(sections)} sections" + (f": {', '.join(t for t in titles if t)}{suffix}" if titles else "")
        return "submitting evidence dossier"
    return _text_excerpt(json.dumps(data, ensure_ascii=False, default=str), 260)


def _tool_result_summary(tool_name: str, payload: str) -> str:
    try:
        data = json.loads(payload)
    except Exception:
        return _text_excerpt(payload, 360)
    if tool_name == "lookup_paper_memory":
        chunks = data.get("chunks")
        if isinstance(chunks, list):
            labels = [
                str(item.get("chunk_id") or item.get("id") or "").strip()
                for item in chunks[:6]
                if isinstance(item, dict)
            ]
            return f"{len(chunks)} chunks" + (f": {', '.join(v for v in labels if v)}" if labels else "")
        if data.get("error"):
            return _text_excerpt(str(data.get("error")), 280)
    if tool_name == "report_paper_memory_dossier":
        if data.get("ack"):
            return str(data.get("ack"))
        errors = data.get("validation_errors")
        if isinstance(errors, list) and errors:
            return _text_excerpt("; ".join(str(item) for item in errors[:3]), 360)
    return _text_excerpt(json.dumps(data, ensure_ascii=False, default=str), 360)


def _dossier_section_summaries(dossier: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in (dossier.get("sections") or [])[:8]:
        if not isinstance(item, dict):
            continue
        out.append({
            "title": _text_excerpt(str(item.get("title") or item.get("panel_role") or ""), 90),
            "claim": _text_excerpt(str(item.get("claim") or ""), 220),
            "poster_copy": _text_excerpt(str(item.get("poster_copy_suggestion") or ""), 180),
        })
    return out
