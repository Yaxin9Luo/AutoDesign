"""ClaimGraphExtractor — v2.8.0 paper → claim graph sub-agent.

Runs between the v2.4 enhancer and the v2.7 planner whenever the brief
attaches a PDF. Owns its own LLMBackend and turn budget. Consumes the paper
raw_text (from `ingest_document` or the runner's pre-pass) and emits one
accepted `ClaimGraph` via the final-submission `report_claim_graph` tool.

Why a sub-agent instead of an inline call: the extractor needs multiple
LLM turns to walk the paper (peek section-by-section, locate evidence
quotes, double-check substring matches). Same architectural pattern as
`agents/critic_agent.py`.

Hard rules (mirrored in `prompts/claim_graph_extractor.md`):
  - Every `EvidenceNode.raw_quote` MUST be a verbatim substring of
    paper_raw_text. The validator (`util/claim_graph_validator.py`) drops
    the whole graph if any quote fails substring match — so the model is
    instructed to DELETE evidence nodes whose quote it cannot ground.
  - Node ids follow T*/M*/E*/I* (e.g. T1, M2, E5, I3).
  - `report_claim_graph` may return validation feedback; the loop exits only
    after the tool accepts a graph.

On max_turns exhaustion without an accepted graph, the agent returns a
sentinel `ClaimGraph` whose `thesis` carries the failure marker
"<extraction failed: timeout>". The runner treats that exact thesis as
"None" and degrades to v2.7.3 chapter-order behavior; planner never sees
the sentinel.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..config import Settings
from ..llm_backend import LLMBackend, ToolCall, TurnResponse, make_backend
from ..run_control import CancellationToken
from ..schema import ClaimGraph
from ..util.claim_graph_validator import validate_claim_graph
from ..util.logging import log


# Sentinel thesis returned on timeout / API error. The runner checks the
# exact string before passing the graph to the planner; matching graphs
# are dropped to None.
EXTRACT_FAIL_THESIS = "<extraction failed: timeout>"


_EXTRACTOR_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "lookup_paper_section",
        "description": (
            "Pull a ~2000 char excerpt from the paper raw_text by keyword "
            "or substring. Use this to verify that a candidate "
            "EvidenceNode.raw_quote actually appears in the paper before "
            "you emit it via report_claim_graph. Returns empty string when "
            "the query has no match."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "A short phrase, section heading, or candidate "
                        "quote to locate in the paper. Case-insensitive."
                    ),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "report_claim_graph",
        "description": (
            "Final-submission tool. Emit your ClaimGraph. The extractor "
            "exits only after this tool accepts the graph; if it returns "
            "validation errors, fix the graph and call it again. Every "
            "evidence raw_quote MUST be a verbatim substring of the paper "
            "raw_text — if you cannot ground a quote via "
            "lookup_paper_section, DELETE that evidence node, do NOT "
            "fabricate."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "paper_title": {"type": "string"},
                "paper_anchor": {"type": "string"},
                "thesis": {"type": "string"},
                "tensions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                            "evidence_anchor": {
                                "type": ["string", "null"],
                            },
                        },
                        "required": ["id", "name", "description"],
                    },
                },
                "mechanisms": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "name": {"type": "string"},
                            "resolves": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "description": {"type": "string"},
                        },
                        "required": ["id", "name", "description"],
                    },
                },
                "evidence": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "metric": {"type": "string"},
                            "source": {"type": "string"},
                            "raw_quote": {"type": "string"},
                            "supports": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["id", "metric", "source", "raw_quote"],
                    },
                },
                "implications": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "description": {"type": "string"},
                            "derives_from": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["id", "description"],
                    },
                },
            },
            "required": [
                "paper_title", "paper_anchor", "thesis",
                "tensions", "mechanisms", "evidence", "implications",
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


class ClaimGraphExtractor:
    """Forked sub-agent that extracts a `ClaimGraph` from a paper.

    Stateless across calls — instantiate once per paper, call `extract` once,
    then drop.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.backend: LLMBackend = make_backend(
            settings, settings.claim_graph_model,
            role="claim_graph_extractor",
        )
        self._system_prompt: str | None = None

    def _system(self) -> str:
        if self._system_prompt is None:
            path: Path = (
                self.settings.prompts_dir / "claim_graph_extractor.md"
            )
            self._system_prompt = path.read_text(encoding="utf-8")
        return self._system_prompt

    def extract(
        self,
        paper_path: Path,
        paper_raw_text: str,
        cancellation_token: CancellationToken | None = None,
    ) -> ClaimGraph:
        """Run the sub-agent loop and return the final ClaimGraph.

        On max_turns exhaustion or API error returns a sentinel ClaimGraph
        whose thesis equals `EXTRACT_FAIL_THESIS`; the runner treats that
        as a degraded extract and falls back to v2.7.3 behavior.
        """
        _raise_if_cancelled(cancellation_token, "claim_graph.start")
        backend_name = self.backend.name
        model = self.backend.model
        log("claim_graph.start", model=model, backend=backend_name,
            paper=str(paper_path),
            paper_chars=len(paper_raw_text or ""),
            max_turns=self.settings.claim_graph_max_turns)
        wall_start = time.monotonic()

        user_text = _build_user_text(
            paper_path=paper_path,
            paper_raw_text=paper_raw_text,
            max_turns=self.settings.claim_graph_max_turns,
        )
        messages: list[Any] = [{"role": "user", "content": user_text}]

        thinking_budget = self.settings.claim_graph_thinking_budget
        max_tokens = (
            max(2048, thinking_budget + 2048)
            if thinking_budget > 0 else 8192
        )

        terminal_graph: ClaimGraph | None = None
        last_response: TurnResponse | None = None

        for turn in range(self.settings.claim_graph_max_turns):
            _raise_if_cancelled(cancellation_token, "claim_graph.before_model_turn")
            try:
                request_kwargs: dict[str, Any] = {
                    "system": self._system(),
                    "messages": messages,
                    "tools": _EXTRACTOR_TOOL_SCHEMAS,
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
                log("claim_graph.api_error", turn=turn + 1,
                    error=f"{type(e).__name__}: {e}")
                terminal_graph = _build_failsafe_graph(
                    paper_path=paper_path,
                    summary=(
                        f"api_error: {type(e).__name__}: {e}"
                    ),
                )
                break

            _raise_if_cancelled(cancellation_token, "claim_graph.after_model_turn")
            last_response = resp
            self.backend.append_assistant(messages, resp)
            _raise_if_cancelled(cancellation_token, "claim_graph.after_append_assistant")

            tool_results_for_api: list[tuple[str, str, bool]] = []
            for tc in resp.tool_calls:
                _raise_if_cancelled(cancellation_token, "claim_graph.before_tool")
                payload, is_err, terminal = self._dispatch_tool(
                    tc, paper_raw_text=paper_raw_text,
                    paper_path=paper_path,
                )
                _raise_if_cancelled(cancellation_token, "claim_graph.after_tool")
                tool_results_for_api.append((tc.id, payload, is_err))
                if terminal is not None:
                    terminal_graph = terminal

            if terminal_graph is not None:
                break

            if tool_results_for_api:
                _raise_if_cancelled(cancellation_token, "claim_graph.before_append_tool_results")
                self.backend.append_tool_results(
                    messages, tool_results_for_api,
                )
                _raise_if_cancelled(cancellation_token, "claim_graph.after_append_tool_results")
                continue

            if resp.stop_reason == "end_turn":
                if _looks_like_kimi_template_leak(resp) and turn + 1 < (
                    self.settings.claim_graph_max_turns
                ):
                    _raise_if_cancelled(cancellation_token, "claim_graph.validation_retry")
                    log("claim_graph.kimi_template_leak_retry",
                        turn=turn + 1)
                    messages.append({
                        "role": "user",
                        "content": (
                            "Your previous turn emitted "
                            "`<|tool_calls_section_begin|>` as plain "
                            "text inside thinking instead of using the "
                            "structured tool_calls field. Tool calls "
                            "MUST be returned via the API's tool-use "
                            "mechanism, not as template tokens. Retry "
                            "now: either call `lookup_paper_section` "
                            "or call `report_claim_graph` to finish."
                        ),
                    })
                    continue
                log("claim_graph.end_turn_no_report", turn=turn + 1)
                break

        if terminal_graph is None:
            terminal_graph = _build_failsafe_graph(
                paper_path=paper_path,
                summary=(
                    f"max_turns ({self.settings.claim_graph_max_turns}) "
                    "hit without report_claim_graph"
                ),
            )

        wall_s = round(time.monotonic() - wall_start, 2)
        usage = (
            last_response.usage if last_response is not None else {}
        ) or {}
        log("claim_graph.done", model=model,
            thesis_chars=len(terminal_graph.thesis),
            n_tensions=len(terminal_graph.tensions),
            n_mechanisms=len(terminal_graph.mechanisms),
            n_evidence=len(terminal_graph.evidence),
            n_implications=len(terminal_graph.implications),
            input_tokens=usage.get("input", 0),
            output_tokens=usage.get("output", 0),
            wall_s=wall_s,
            failed=terminal_graph.thesis == EXTRACT_FAIL_THESIS)
        _raise_if_cancelled(cancellation_token, "claim_graph.before_result")
        return terminal_graph

    def _dispatch_tool(
        self,
        tc: ToolCall,
        *,
        paper_raw_text: str,
        paper_path: Path,
    ) -> tuple[str, bool, ClaimGraph | None]:
        """Returns (json_payload, is_error, terminal_graph_or_None)."""
        if tc.name == "lookup_paper_section":
            query = str(tc.input.get("query", "")).strip()
            excerpt = _extract_paper_excerpt(paper_raw_text, query)
            return (
                json.dumps({"query": query, "excerpt": excerpt}),
                False, None,
            )

        if tc.name == "report_claim_graph":
            try:
                payload = dict(tc.input)
                payload.setdefault("tensions", [])
                payload.setdefault("mechanisms", [])
                payload.setdefault("evidence", [])
                payload.setdefault("implications", [])
                graph = ClaimGraph.model_validate(payload)
            except ValidationError as e:
                err_msg = (
                    "report_claim_graph failed schema: "
                    f"{e.errors(include_url=False)[:3]}"
                )
                return json.dumps({"error": err_msg}), True, None
            errors = validate_claim_graph(graph, paper_raw_text)
            if errors:
                return (
                    json.dumps({
                        "error": "report_claim_graph failed validation",
                        "validation_errors": errors[:10],
                        "available_ids": _claim_graph_id_catalog(graph),
                        "instruction": (
                            "Fix dangling references or ungrounded quotes, "
                            "then call report_claim_graph again. Use only "
                            "ids from available_ids: resolves -> tensions, "
                            "supports -> mechanisms, derives_from -> "
                            "mechanisms or evidence."
                        ),
                    }),
                    True,
                    None,
                )
            ack = json.dumps({
                "thesis_chars": len(graph.thesis),
                "n_tensions": len(graph.tensions),
                "n_mechanisms": len(graph.mechanisms),
                "n_evidence": len(graph.evidence),
                "n_implications": len(graph.implications),
                "ack": "graph recorded; loop will exit",
            })
            return ack, False, graph

        return (
            json.dumps({"error": f"unknown tool: {tc.name}"}),
            True, None,
        )


def _build_user_text(
    *,
    paper_path: Path,
    paper_raw_text: str,
    max_turns: int,
) -> str:
    paper_chars = len(paper_raw_text or "")
    head = (paper_raw_text or "")[:4000]
    tail_pos = max(paper_chars - 2000, 4000)
    tail = (paper_raw_text or "")[tail_pos:] if paper_chars > 6000 else ""
    return (
        f"## ClaimGraph extraction\n\n"
        f"Paper file: {paper_path.name}\n"
        f"Paper raw_text length: {paper_chars:,} chars\n"
        f"Turn budget: {max_turns}\n\n"
        f"## Head (first 4000 chars)\n```\n{head}\n```\n\n"
        + (
            f"## Tail (last 2000 chars)\n```\n{tail}\n```\n\n"
            if tail else ""
        )
        + (
            "Use `lookup_paper_section(query=...)` to pull excerpts from "
            "the rest of the paper before grounding any evidence quote. "
            "Lookup is whitespace-tolerant (matches across PDF line "
            "wraps) and case-insensitive; if the full phrase misses, it "
            "falls back to the longest single token. Prefer 3-6 word "
            "phrases over single keywords. Empty excerpt = phrase truly "
            "absent — pick a different anchor.\n\n"
            "FINISH by calling `report_claim_graph`. A successful "
            "extraction has exactly one accepted `report_claim_graph` "
            "call; if the tool returns a validation error, fix the graph "
            "and call it again. Every "
            "EvidenceNode.raw_quote MUST be a verbatim substring of the "
            "paper raw_text — if you cannot ground a quote after 2-3 "
            "lookup attempts, DELETE that evidence node, do NOT fabricate."
        )
    )


def _claim_graph_id_catalog(graph: ClaimGraph) -> dict[str, list[str]]:
    """Return the ids the model may reference on a retry."""
    return {
        "tensions": [t.id for t in graph.tensions],
        "mechanisms": [m.id for m in graph.mechanisms],
        "evidence": [e.id for e in graph.evidence],
        "implications": [i.id for i in graph.implications],
    }


_WS_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_]{2,}")


def _extract_paper_excerpt(raw: str | None, query: str) -> str:
    """Whitespace-tolerant substring search with windowed context.

    Returns up to ~3000 chars centered on the first hit, or empty string
    when no match found. Mirrors the validator's `_norm_ws` semantics so
    a query that the model intends to use as a `raw_quote` matches here
    iff it would also pass `validate_claim_graph` — closing the v2.8.0
    Sonnet-failure loop where the lookup tool was stricter than the
    validator (newlines / double-spaces in PDF text broke matches).

    Two-tier fallback when the verbatim-normalized match misses:
      1. Try the longest single token in the query (handles PDF queries
         like "Table 1: comparison" where colons / numerals don't appear
         verbatim). Returns the first match's window.
      2. Empty string — caller (LLM) interprets as "not in paper".
    """
    if not raw or not query:
        return ""

    norm_query = _norm_ws(query).lower()
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
    return f"{prefix}{raw[start:end]}{suffix}"


def _norm_ws(s: str) -> str:
    """Collapse runs of whitespace. Mirrors
    `util.claim_graph_validator._norm_ws` so lookup semantics match the
    validator's substring-match semantics."""
    return _WS_RE.sub(" ", s or "").strip()


def _norm_ws_with_mapping(raw: str) -> tuple[str, list[int]]:
    """Normalize whitespace + return per-char index mapping back to raw.
    `mapping[i]` is the index in `raw` that produced `norm[i]`. The
    mapping lets us locate a normalized hit in the original (unnormalized)
    text so the returned excerpt preserves the paper's real layout."""
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


_KIMI_LEAK_MARKERS: tuple[str, ...] = (
    "<|tool_calls_section_begin|>",
    "<|tool_call_begin|>",
    "<|tool_calls_section_end|>",
)


def _looks_like_kimi_template_leak(resp: TurnResponse) -> bool:
    """True iff the response has no structured tool_calls but the model
    emitted a Kimi/OpenAI-compat tool-use template token in either text
    or thinking blocks. Kimi K2.6 via OpenRouter occasionally serializes
    its tool-call template into the THINKING channel instead of the
    structured `tool_calls` array; this lets us recover with one retry
    rather than burning the whole turn budget on a parse glitch."""
    if resp.tool_calls:
        return False
    haystacks: list[str] = [resp.text or ""]
    for block in resp.thinking_blocks or []:
        try:
            haystacks.append(block.thinking or "")
        except AttributeError:
            haystacks.append(str(block))
    blob = "\n".join(haystacks)
    return any(marker in blob for marker in _KIMI_LEAK_MARKERS)


def _build_failsafe_graph(
    *, paper_path: Path, summary: str,
) -> ClaimGraph:
    """Return a sentinel ClaimGraph whose thesis carries the failure
    marker. Runner checks `graph.thesis == EXTRACT_FAIL_THESIS` to drop
    the graph back to None."""
    log("claim_graph.failsafe", paper=str(paper_path), summary=summary)
    return ClaimGraph(
        paper_title=paper_path.stem,
        paper_anchor="unknown",
        thesis=EXTRACT_FAIL_THESIS,
        tensions=[], mechanisms=[], evidence=[], implications=[],
    )


def _summarize_tool_input(name: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Strip large blobs before logging or echoing."""
    out = dict(raw or {})
    for key, val in list(out.items()):
        if isinstance(val, str) and len(val) > 2000:
            out[key] = val[:2000] + f"…[truncated {len(val) - 2000} chars]"
    return out
