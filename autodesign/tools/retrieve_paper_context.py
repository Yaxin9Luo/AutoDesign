"""retrieve_paper_context — lexical lookup over ingested paper memory."""

from __future__ import annotations

from typing import Any

from ._contract import ToolContext, obs_error, obs_ok
from ..schema import ToolResultRecord
from ..util.paper_memory import retrieve_paper_context as retrieve_from_memory
from ..util.paper_memory_dossier import retrieve_from_paper_memory_dossier


def retrieve_paper_context(args: dict[str, Any], *, ctx: ToolContext) -> ToolResultRecord:
    query = str(args.get("query") or "").strip()
    if not query:
        return obs_error("retrieve_paper_context requires a non-empty query", category="validation")

    def as_str_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item or "").strip()]
        text = str(value).strip()
        return [text] if text else []

    def as_optional_bool(value: Any) -> bool | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "y"}:
            return True
        if text in {"0", "false", "no", "n"}:
            return False
        return None

    top_k: int | None
    try:
        top_k = int(args["top_k"]) if args.get("top_k") not in (None, "") else None
    except Exception:
        top_k = None
    mode = str(args.get("mode") or "hybrid").strip().lower()
    if mode not in {"hybrid", "curated", "raw"}:
        return obs_error("retrieve_paper_context mode must be hybrid, curated, or raw", category="validation")
    needs = as_str_list(args.get("needs"))
    expand_evidence_refs = bool(as_optional_bool(args.get("expand_evidence_refs")))

    memory = ctx.state.get("paper_memory")
    panel_role = str(args.get("panel_role") or "").strip() or None
    source_ids = as_str_list(args.get("source_ids"))
    categories = as_str_list(args.get("categories"))
    evidence_kind = as_str_list(args.get("evidence_kind"))
    safe_to_quote = as_optional_bool(args.get("safe_to_quote"))

    result: dict[str, Any] | None = None
    if mode in {"hybrid", "curated"}:
        curated = retrieve_from_paper_memory_dossier(
            ctx.state.get("paper_memory_dossier"),
            memory,
            query=query,
            panel_role=panel_role,
            source_ids=source_ids,
            categories=categories,
            evidence_kind=evidence_kind,
            safe_to_quote=safe_to_quote,
            needs=needs,
            expand_evidence_refs=expand_evidence_refs,
            top_k=top_k,
        )
        if curated.get("results") or mode == "curated":
            result = curated
    if result is None:
        result = retrieve_from_memory(
            memory,
            query=query,
            panel_role=panel_role,
            source_ids=source_ids,
            categories=categories,
            evidence_kind=evidence_kind,
            safe_to_quote=safe_to_quote,
            top_k=top_k,
        )
        result["mode"] = "raw" if mode == "raw" else "hybrid"
    if result.get("error"):
        return obs_error(
            f"paper memory retrieval failed: {result.get('error')}",
            category="validation",
            payload=result,
        )
    return obs_ok(result)
