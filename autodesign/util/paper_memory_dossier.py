"""LLM-curated dossier layer over canonical paper memory.

The dossier is not a source of truth. It is a validated, panel-ready projection
over ``paper_memory.json``. Every evidence reference must resolve back to a
canonical chunk before it can be used by designer retrieval.
"""

from __future__ import annotations

from collections import Counter
import json
import math
import re
from pathlib import Path
from typing import Any

from .io import atomic_write_json
from .paper_memory import paper_memory_cache_dir, retrieve_paper_context
from .pipeline_cache import pipeline_cache_enabled


PAPER_MEMORY_DOSSIER_VERSION = 1

_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_+-]*|\d+(?:\.\d+)?%?")
_ALLOWED_NEEDS = {
    "copy",
    "quote",
    "visual",
    "table",
    "limitation",
    "takeaway",
    "expanded_text",
}


def read_paper_memory_dossier_cache(
    settings: Any,
    memory: dict[str, Any],
    *,
    model: str | None = None,
) -> dict[str, Any] | None:
    """Load a validated cached dossier for this paper memory, if available."""
    key = str((memory or {}).get("cache_key") or "")
    if not key or not pipeline_cache_enabled("paper_memory"):
        return None
    path = paper_memory_cache_dir(settings, key) / "dossier.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("kind") != "paper_memory_dossier":
        return None
    if payload.get("source_memory_cache_key") != key:
        return None
    if model and payload.get("model") not in (None, "", model):
        return None
    dossier, errors = validate_paper_memory_dossier(memory, payload)
    if errors:
        return None
    return dossier


def write_paper_memory_dossier_cache(
    settings: Any,
    memory: dict[str, Any],
    dossier: dict[str, Any],
) -> Path | None:
    """Persist dossier sidecars under the paper-memory cache directory."""
    key = str((memory or {}).get("cache_key") or "")
    if not key or not dossier or not pipeline_cache_enabled("paper_memory"):
        return None
    try:
        cache_dir = paper_memory_cache_dir(settings, key)
        payload = atomic_write_json(cache_dir / "dossier.json", dossier)
        (cache_dir / "dossier.md").write_text(
            paper_memory_dossier_markdown(dossier),
            encoding="utf-8",
        )
        return payload
    except Exception:
        return None


def write_paper_memory_dossier_run_artifacts(
    run_dir: Path,
    dossier: dict[str, Any],
) -> dict[str, str]:
    """Persist run-local dossier JSON and Markdown projections."""
    if not dossier:
        return {}
    base = Path(run_dir)
    payload = atomic_write_json(base / "paper_memory_dossier.json", dossier)
    md_path = base / "paper_memory_dossier.md"
    md_path.write_text(paper_memory_dossier_markdown(dossier), encoding="utf-8")
    return {
        "paper_memory_dossier_json": str(payload),
        "paper_memory_dossier_md": str(md_path),
    }


def validate_paper_memory_dossier(
    memory: dict[str, Any],
    dossier: dict[str, Any],
    *,
    model: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Return a sanitized dossier plus validation errors.

    Errors indicate the LLM report should be retried. The sanitized return is
    still useful for diagnostics, but callers should only cache it when
    ``errors`` is empty.
    """
    if not isinstance(memory, dict) or memory.get("kind") != "paper_memory":
        return {}, ["paper_memory_missing"]
    if not isinstance(dossier, dict):
        return {}, ["dossier_not_object"]

    chunk_map = _chunk_map(memory)
    errors: list[str] = []
    sections: list[dict[str, Any]] = []
    for idx, raw_section in enumerate(dossier.get("sections") or [], start=1):
        if not isinstance(raw_section, dict):
            errors.append(f"section_{idx}:not_object")
            continue
        evidence_refs: list[dict[str, Any]] = []
        for ref_idx, raw_ref in enumerate(raw_section.get("evidence_refs") or [], start=1):
            if not isinstance(raw_ref, dict):
                errors.append(f"section_{idx}.ref_{ref_idx}:not_object")
                continue
            chunk_id = _clean(raw_ref.get("chunk_id") or raw_ref.get("id"))
            chunk = chunk_map.get(chunk_id)
            if chunk is None:
                errors.append(f"section_{idx}.ref_{ref_idx}:missing_chunk_id:{chunk_id or '(empty)'}")
                continue
            ref_errors: list[str] = []
            ref = _validated_ref(chunk, raw_ref, ref_errors)
            errors.extend(f"section_{idx}.ref_{ref_idx}:{err}" for err in ref_errors)
            evidence_refs.append(ref)
        if not evidence_refs:
            errors.append(f"section_{idx}:no_valid_evidence_refs")
            continue
        panel_role = _clean(raw_section.get("panel_role")) or _infer_panel_role(raw_section, evidence_refs)
        section = {
            "id": _clean(raw_section.get("id")) or f"pm_section_{len(sections) + 1:02d}",
            "panel_role": panel_role,
            "title": _clip(_clean(raw_section.get("title")) or panel_role.replace("_", " ").title(), 120),
            "claim": _clip(_clean(raw_section.get("claim")), 280),
            "poster_copy_suggestion": _clip(_clean(raw_section.get("poster_copy_suggestion")), 650),
            "evidence_refs": evidence_refs,
            "visual_ids": _dedupe_strings(raw_section.get("visual_ids") or []),
            "confidence": _safe_confidence(raw_section.get("confidence")),
        }
        sections.append({k: v for k, v in section.items() if v not in ("", [], None)})

    if not sections:
        errors.append("dossier_no_valid_sections")
    source_key = str(memory.get("cache_key") or "")
    sanitized = {
        "kind": "paper_memory_dossier",
        "version": PAPER_MEMORY_DOSSIER_VERSION,
        "source_memory_cache_key": source_key,
        "model": _clean(dossier.get("model")) or _clean(model),
        "section_count": len(sections),
        "sections": sections,
    }
    return sanitized, errors


def build_fallback_paper_memory_dossier(
    memory: dict[str, Any],
    *,
    model: str = "deterministic_fallback",
) -> dict[str, Any]:
    """Build a small validated dossier from deterministic retrieval only.

    This is used for tests and optional degraded artifacts, not as a replacement
    for the LLM agent when the agent is available.
    """
    if not isinstance(memory, dict) or memory.get("kind") != "paper_memory":
        return {}
    specs = [
        ("method_pipeline", "method architecture training objective", ["method_unit", "section_summary", "figure_caption"]),
        ("results_table", "benchmark results accuracy ablation table", ["numeric_claim", "result_unit", "table_row", "table_caption"]),
        ("limitations_future", "limitations future work caveat challenge", ["limitation_unit", "takeaway_unit", "section_summary"]),
        ("takeaway", "conclusion demonstrate show takeaway implication", ["takeaway_unit", "result_unit", "key_quote"]),
    ]
    sections = []
    for role, query, categories in specs:
        result = retrieve_paper_context(memory, query=query, panel_role=role, categories=categories)
        refs = []
        for item in result.get("results") or []:
            refs.append({
                "chunk_id": item.get("chunk_id") or item.get("id"),
                "quote": item.get("quote"),
            })
        if not refs:
            continue
        first_text = _clean((result.get("results") or [{}])[0].get("snippet") or (result.get("results") or [{}])[0].get("quote"))
        sections.append({
            "id": f"fallback_{role}",
            "panel_role": role,
            "title": role.replace("_", " ").title(),
            "claim": _clip(first_text, 220),
            "poster_copy_suggestion": _clip(first_text, 420),
            "evidence_refs": refs,
            "visual_ids": [],
            "confidence": 0.45,
        })
    dossier, errors = validate_paper_memory_dossier(
        memory,
        {
            "kind": "paper_memory_dossier",
            "version": PAPER_MEMORY_DOSSIER_VERSION,
            "source_memory_cache_key": memory.get("cache_key"),
            "model": model,
            "sections": sections,
        },
        model=model,
    )
    return dossier if not errors else {}


def retrieve_from_paper_memory_dossier(
    dossier: dict[str, Any] | None,
    memory: dict[str, Any] | None,
    *,
    query: str,
    panel_role: str | None = None,
    source_ids: list[str] | None = None,
    categories: list[str] | None = None,
    evidence_kind: list[str] | None = None,
    safe_to_quote: bool | None = None,
    needs: list[str] | None = None,
    expand_evidence_refs: bool = False,
    top_k: int | None = None,
) -> dict[str, Any]:
    """Return curated, dossier-backed results with canonical evidence refs."""
    if not isinstance(dossier, dict) or dossier.get("kind") != "paper_memory_dossier":
        return {"query": query, "results": [], "error": "paper_memory_dossier_missing"}
    if not isinstance(memory, dict) or memory.get("kind") != "paper_memory":
        return {"query": query, "results": [], "error": "paper_memory_missing"}
    sections = [s for s in (dossier.get("sections") or []) if isinstance(s, dict)]
    if not sections:
        return {"query": query, "results": [], "error": "paper_memory_dossier_empty"}

    chunk_map = _chunk_map(memory)
    wanted_sources = {str(v) for v in (source_ids or []) if str(v or "").strip()}
    wanted_categories = {str(v) for v in (categories or []) if str(v or "").strip()}
    wanted_evidence = {_normalize_kind(v) for v in (evidence_kind or []) if str(v or "").strip()}
    wanted_needs = {str(v).strip() for v in (needs or []) if str(v or "").strip() in _ALLOWED_NEEDS}
    expand_refs = bool(expand_evidence_refs) or "expanded_text" in wanted_needs
    q_tokens = _tokens(" ".join([query or "", panel_role or "", " ".join(wanted_needs)]))
    if not q_tokens and not panel_role and not wanted_sources and not wanted_categories and not wanted_evidence:
        return {"query": query, "results": [], "error": "empty_query"}

    docs: list[tuple[dict[str, Any], list[str], list[dict[str, Any]]]] = []
    for section in sections:
        refs = _resolved_refs(section.get("evidence_refs") or [], chunk_map)
        if not refs:
            continue
        if panel_role and str(section.get("panel_role") or "") != str(panel_role):
            # Soft filter: retain potentially useful sections if query tokens match.
            pass
        filtered_refs = []
        for ref in refs:
            chunk = ref["_chunk"]
            if wanted_categories and str(chunk.get("category") or "") not in wanted_categories:
                continue
            if wanted_evidence and _normalize_kind(chunk.get("evidence_kind")) not in wanted_evidence:
                continue
            if safe_to_quote is not None and bool(chunk.get("safe_to_quote")) != bool(safe_to_quote):
                continue
            if wanted_sources and not _chunk_sources(chunk).intersection(wanted_sources):
                continue
            filtered_refs.append(ref)
        if not filtered_refs:
            continue
        text = " ".join([
            str(section.get("panel_role") or ""),
            str(section.get("title") or ""),
            str(section.get("claim") or ""),
            str(section.get("poster_copy_suggestion") or ""),
            " ".join(str(v) for v in section.get("visual_ids") or []),
            " ".join(str(ref.get("quote") or ref["_chunk"].get("text") or "") for ref in filtered_refs),
        ])
        docs.append((section, _tokens(text), filtered_refs))
    if not docs:
        return {"query": query, "results": [], "error": "no_matching_dossier_sections"}

    avg_len = sum(len(toks) for _section, toks, _refs in docs) / max(1, len(docs))
    df = Counter(tok for _section, toks, _refs in docs for tok in set(toks))
    scored: list[tuple[float, dict[str, Any], list[dict[str, Any]]]] = []
    for section, toks, refs in docs:
        score = _bm25_score(q_tokens, toks, df=df, doc_count=len(docs), avg_len=avg_len)
        if panel_role and str(section.get("panel_role") or "") == str(panel_role):
            score += 2.0
        if wanted_sources:
            score += 1.5
        if wanted_categories or wanted_evidence:
            score += 0.8
        if "copy" in wanted_needs and section.get("poster_copy_suggestion"):
            score += 0.6
        if "visual" in wanted_needs and section.get("visual_ids"):
            score += 0.6
        if score <= 0 and (panel_role or wanted_sources or wanted_categories or wanted_evidence):
            score = 0.1
        if score > 0:
            scored.append((score, section, refs))
    scored.sort(key=lambda item: (-item[0], str(item[1].get("id") or "")))
    results = []
    result_limit = _result_limit(top_k, len(scored))
    for score, section, refs in scored[:result_limit]:
        evidence_refs = [_public_ref(ref, include_chunk_text=expand_refs) for ref in refs]
        first_ref = evidence_refs[0] if evidence_refs else {}
        result = {
            "source": "dossier",
            "id": section.get("id"),
            "section_id": section.get("id"),
            "panel_role": section.get("panel_role"),
            "category": first_ref.get("category"),
            "evidence_kind": first_ref.get("evidence_kind"),
            "safe_to_quote": all(bool(ref.get("safe_to_quote")) for ref in evidence_refs) if evidence_refs else False,
            "score": round(score, 4),
            "page": first_ref.get("page"),
            "section": first_ref.get("section"),
            "source_id": first_ref.get("source_id"),
            "parent_source_id": first_ref.get("parent_source_id"),
            "source_ids": sorted({sid for ref in evidence_refs for sid in ref.get("source_ids", [])}),
            "quote": first_ref.get("quote"),
            "snippet": section.get("poster_copy_suggestion") or section.get("claim") or first_ref.get("quote"),
            "text": section.get("poster_copy_suggestion") or section.get("claim"),
            "poster_copy_suggestion": section.get("poster_copy_suggestion"),
            "claim": section.get("claim"),
            "title": section.get("title"),
            "visual_ids": section.get("visual_ids") or [],
            "confidence": section.get("confidence"),
            "why_selected": _why_selected(section, wanted_needs, panel_role),
            "evidence_refs": evidence_refs,
        }
        if expand_refs:
            result["expanded_text"] = [
                {
                    "chunk_id": ref.get("chunk_id"),
                    "page": ref.get("page"),
                    "section": ref.get("section"),
                    "source_id": ref.get("source_id"),
                    "parent_source_id": ref.get("parent_source_id"),
                    "category": ref.get("category"),
                    "evidence_kind": ref.get("evidence_kind"),
                    "safe_to_quote": ref.get("safe_to_quote"),
                    "text": ref.get("chunk_text"),
                }
                for ref in evidence_refs
                if ref.get("chunk_text")
            ]
        results.append(result)
    return {
        "query": query,
        "source": "dossier",
        "mode": "curated",
        "panel_role": panel_role,
        "source_ids": sorted(wanted_sources),
        "categories": sorted(wanted_categories),
        "evidence_kind": sorted(wanted_evidence),
        "safe_to_quote": safe_to_quote,
        "needs": sorted(wanted_needs),
        "top_k": result_limit,
        "results": results,
    }


def _result_limit(raw: int | None, available: int) -> int:
    if available <= 0:
        return 0
    if raw is None:
        return available
    try:
        requested = int(raw)
    except (TypeError, ValueError):
        return available
    if requested <= 0:
        return available
    return min(requested, available)


def paper_memory_dossier_markdown(dossier: dict[str, Any]) -> str:
    if not isinstance(dossier, dict) or dossier.get("kind") != "paper_memory_dossier":
        return "# Paper Memory Dossier\n\nNo curated dossier is available.\n"
    lines = [
        "# Paper Memory Dossier",
        "",
        f"- Source memory: {dossier.get('source_memory_cache_key') or 'unknown'}",
        f"- Model: {dossier.get('model') or 'unknown'}",
        f"- Sections: {len(dossier.get('sections') or [])}",
        "",
        "Use this as panel-ready guidance. Use evidence refs for pages/source ids; do not render provenance rows in poster panels.",
    ]
    for section in dossier.get("sections") or []:
        if not isinstance(section, dict):
            continue
        lines.extend([
            "",
            f"## {section.get('title') or section.get('panel_role') or section.get('id')}",
            f"- Panel role: {section.get('panel_role') or 'unknown'}",
        ])
        if section.get("claim"):
            lines.append(f"- Claim: {section.get('claim')}")
        if section.get("poster_copy_suggestion"):
            lines.append(f"- Poster copy: {section.get('poster_copy_suggestion')}")
        if section.get("visual_ids"):
            lines.append(f"- Visual ids: {', '.join(str(v) for v in section.get('visual_ids') or [])}")
        refs = section.get("evidence_refs") or []
        if refs:
            lines.append("- Evidence:")
            for ref in refs[:8]:
                if not isinstance(ref, dict):
                    continue
                cite = []
                if ref.get("page"):
                    cite.append(f"p.{ref.get('page')}")
                if ref.get("source_id"):
                    cite.append(str(ref.get("source_id")))
                meta = ", ".join(cite) or str(ref.get("chunk_id") or "")
                safe = "safe" if ref.get("safe_to_quote") else "summary"
                lines.append(f"  - [{meta}; {safe}] {_clip(_clean(ref.get('quote')), 260)}")
    return "\n".join(lines).rstrip() + "\n"


def _validated_ref(
    chunk: dict[str, Any],
    raw_ref: dict[str, Any],
    errors: list[str],
) -> dict[str, Any]:
    chunk_text = _clean(chunk.get("text"))
    chunk_quote = _clean(chunk.get("quote"))
    raw_quote = _clean(raw_ref.get("quote"))
    quote = raw_quote or chunk_quote or chunk_text
    chunk_blob = " ".join([chunk_text, chunk_quote])
    grounded = _contains_norm(chunk_blob, quote)
    safe = bool(chunk.get("safe_to_quote"))
    evidence = _normalize_kind(chunk.get("evidence_kind"))
    if raw_quote and not grounded:
        errors.append("quote_not_grounded")
        if safe and chunk_quote:
            quote = chunk_quote
        else:
            quote = ""
            safe = False
            evidence = "derived_summary"
    if raw_ref.get("page") not in (None, "", chunk.get("page")):
        errors.append("page_mismatch")
    raw_source = _clean(raw_ref.get("source_id"))
    chunk_sources = _chunk_sources(chunk)
    if raw_source and raw_source not in chunk_sources:
        errors.append("source_id_mismatch")
    if bool(raw_ref.get("safe_to_quote")) and not bool(chunk.get("safe_to_quote")):
        errors.append("unsafe_direct_quote")
        safe = False
        evidence = "derived_summary"
    return {
        "chunk_id": chunk.get("chunk_id") or chunk.get("id"),
        "page": chunk.get("page"),
        "section": chunk.get("section"),
        "source_id": chunk.get("source_id"),
        "parent_source_id": chunk.get("parent_source_id"),
        "source_ids": sorted(chunk_sources),
        "category": chunk.get("category"),
        "evidence_kind": evidence,
        "safe_to_quote": safe,
        "quote": _clip(quote, 600),
    }


def _public_ref(ref: dict[str, Any], *, include_chunk_text: bool) -> dict[str, Any]:
    out = {k: v for k, v in ref.items() if k != "_chunk"}
    if include_chunk_text and "_chunk" in ref:
        chunk = ref["_chunk"]
        out["chunk_text"] = _clip(_clean(chunk.get("text")), 1600)
        if chunk.get("quote") and "chunk_quote" not in out:
            out["chunk_quote"] = _clip(_clean(chunk.get("quote")), 600)
    return out


def _resolved_refs(refs: list[Any], chunk_map: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        chunk = chunk_map.get(_clean(ref.get("chunk_id") or ref.get("id")))
        if chunk is None:
            continue
        cloned = dict(ref)
        cloned["_chunk"] = chunk
        out.append(cloned)
    return out


def _chunk_map(memory: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for chunk in memory.get("chunks") or []:
        if not isinstance(chunk, dict):
            continue
        for key in (chunk.get("chunk_id"), chunk.get("id")):
            clean = _clean(key)
            if clean:
                out[clean] = chunk
    return out


def _chunk_sources(chunk: dict[str, Any]) -> set[str]:
    return {
        value for value in {
            _clean(chunk.get("source_id")),
            _clean(chunk.get("parent_source_id")),
            *[_clean(v) for v in (chunk.get("source_ids") or [])],
        }
        if value
    }


def _contains_norm(haystack: str, needle: str) -> bool:
    if not needle:
        return True
    h = re.sub(r"\s+", " ", haystack or "").strip().lower()
    n = re.sub(r"\s+", " ", needle or "").strip().lower()
    return bool(n) and n in h


def _tokens(text: str) -> list[str]:
    return [tok.lower() for tok in _TOKEN_RE.findall(text or "") if len(tok) >= 2]


def _bm25_score(
    query_tokens: list[str],
    doc_tokens: list[str],
    *,
    df: Counter[str],
    doc_count: int,
    avg_len: float,
) -> float:
    tf = Counter(doc_tokens)
    length = max(1, len(doc_tokens))
    score = 0.0
    for tok in query_tokens:
        freq = tf.get(tok, 0)
        if not freq:
            continue
        idf = math.log(1.0 + (doc_count - df[tok] + 0.5) / (df[tok] + 0.5))
        denom = freq + 1.2 * (1 - 0.75 + 0.75 * length / max(1.0, avg_len))
        score += idf * (freq * 2.2 / denom)
    return score


def _infer_panel_role(section: dict[str, Any], refs: list[dict[str, Any]]) -> str:
    text = " ".join([
        _clean(section.get("title")),
        _clean(section.get("claim")),
        _clean(section.get("poster_copy_suggestion")),
        " ".join(_clean(ref.get("category")) for ref in refs),
    ]).lower()
    if any(t in text for t in ("limit", "future", "caveat")):
        return "limitations_future"
    if any(t in text for t in ("table", "benchmark", "accuracy", "result", "ablation")):
        return "results_table"
    if any(t in text for t in ("method", "pipeline", "training", "architecture")):
        return "method_pipeline"
    return "takeaway"


def _why_selected(section: dict[str, Any], needs: set[str], panel_role: str | None) -> str:
    reasons = []
    if panel_role and section.get("panel_role") == panel_role:
        reasons.append(f"panel_role={panel_role}")
    if needs:
        reasons.append("needs=" + ",".join(sorted(needs)))
    if section.get("confidence") is not None:
        reasons.append(f"confidence={section.get('confidence')}")
    return "; ".join(reasons) or "curated dossier match"


def _normalize_kind(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    if raw in {"verbatim", "extracted", "derived_summary", "normalized_table"}:
        return raw
    if raw in {"summary", "derived"}:
        return "derived_summary"
    if raw in {"table", "table_row"}:
        return "normalized_table"
    return raw


def _dedupe_strings(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = _clean(value)[:160]
        if clean and clean not in seen:
            out.append(clean)
            seen.add(clean)
    return out


def _safe_confidence(value: Any) -> float | None:
    try:
        return round(min(1.0, max(0.0, float(value))), 3)
    except Exception:
        return None


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\x00", " ")).strip()


def _clip(text: str, limit: int) -> str:
    clean = _clean(text)
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 1)].rstrip() + "..."
