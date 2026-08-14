"""Deterministic paper memory for paper-poster planning.

The canonical memory is JSON and stays dependency-free. Ingest builds source
chunks from the body-only PDF window, writes them to a stable cache keyed by
PDF sha + body-window + parser/input versions, and tools retrieve snippets with
a lexical BM25-style scorer. Markdown sidecars are generated as LLM-readable
projections, not as the source of truth.
"""

from __future__ import annotations

from collections import Counter
import json
import math
import re
from pathlib import Path
from typing import Any

from .io import atomic_write_json, sha256_file
from .pipeline_cache import pipeline_cache_enabled, stable_cache_key


PAPER_MEMORY_VERSION = 2
PAPER_MEMORY_PARSER_VERSION = "paper-memory-v2"
PAPER_MEMORY_INPUT_VERSION = "paper-memory-input-v1"

_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_+-]*|\d+(?:\.\d+)?%?")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_NUMERIC_RE = re.compile(r"\b\d+(?:\.\d+)?(?:\s*(?:%|x|×|k|m|b|t|gb|mb|tokens?|params?|parameters?|points?))?\b", re.I)

_CATEGORY_QUERY_HINTS = {
    "method": {"method_unit", "section_summary", "figure_caption", "key_quote"},
    "method_pipeline": {"method_unit", "section_summary", "figure_caption", "key_quote"},
    "model_card": {"method_unit", "section_summary", "numeric_claim", "key_quote"},
    "results": {"result_unit", "numeric_claim", "table_row", "table_caption"},
    "results_table": {"result_unit", "numeric_claim", "table_row", "table_caption"},
    "main_evidence": {"result_unit", "numeric_claim", "figure_caption", "table_row"},
    "ablation_analysis": {"result_unit", "numeric_claim", "table_row", "key_quote"},
    "limitations": {"limitation_unit", "section_summary", "key_quote"},
    "limitations_future": {"limitation_unit", "takeaway_unit", "section_summary"},
    "takeaway": {"takeaway_unit", "key_quote", "section_summary"},
}


def paper_memory_cache_key(
    *,
    pdf_path: Path,
    body_window: dict[str, Any] | None,
    manifest: dict[str, Any] | None = None,
    rendered_layers: dict[str, Any] | None = None,
    registered_layer_ids: list[str] | None = None,
    recommended_text_units: dict[str, list[dict[str, Any]]] | None = None,
    parser_version: str = PAPER_MEMORY_PARSER_VERSION,
) -> str:
    """Stable cache key for a PDF/body-window/parser/input tuple."""
    try:
        pdf_sha = sha256_file(pdf_path)
    except Exception:
        pdf_sha = ""
    return stable_cache_key({
        "stage": "paper_memory",
        "version": PAPER_MEMORY_VERSION,
        "parser_version": parser_version,
        "pdf_sha256": pdf_sha,
        "body_window": _body_window_fingerprint(body_window or {}),
        "input_version": PAPER_MEMORY_INPUT_VERSION,
        "input_fingerprint": _paper_memory_input_fingerprint(
            manifest=manifest,
            rendered_layers=rendered_layers,
            registered_layer_ids=registered_layer_ids,
            recommended_text_units=recommended_text_units,
        ),
    })


def paper_memory_cache_dir(settings: Any, key: str) -> Path:
    """Return the unsharded cache dir requested by the paper-memory contract."""
    return Path(settings.out_dir) / "cache" / "paper_memory" / key


def read_paper_memory_cache(settings: Any, key: str) -> dict[str, Any] | None:
    if not key or not pipeline_cache_enabled("paper_memory"):
        return None
    path = paper_memory_cache_dir(settings, key) / "payload.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("kind") != "paper_memory":
        return None
    if str(payload.get("cache_key") or "") != key:
        return None
    return payload


def write_paper_memory_cache(settings: Any, memory: dict[str, Any]) -> Path | None:
    key = str(memory.get("cache_key") or "")
    if not key or not pipeline_cache_enabled("paper_memory"):
        return None
    try:
        cache_dir = paper_memory_cache_dir(settings, key)
        payload_path = atomic_write_json(cache_dir / "payload.json", memory)
        atomic_write_json(cache_dir / "index.json", build_paper_memory_index(memory))
        write_paper_memory_sidecars(cache_dir, memory, markdown_name="memory.md", packs_dir_name="evidence_packs")
        return payload_path
    except Exception:
        return None


def write_paper_memory_run_artifacts(run_dir: Path, memory: dict[str, Any]) -> dict[str, str]:
    """Persist the run-local canonical memory plus LLM-readable projections."""
    paths: dict[str, str] = {}
    payload = atomic_write_json(Path(run_dir) / "paper_memory.json", memory)
    paths["paper_memory_json"] = str(payload)
    paths.update(write_paper_memory_sidecars(Path(run_dir), memory))
    return paths


def write_paper_memory_sidecars(
    base_dir: Path,
    memory: dict[str, Any],
    *,
    markdown_name: str = "paper_memory.md",
    packs_dir_name: str = "paper_evidence_packs",
) -> dict[str, str]:
    """Write Markdown projections for LLM consumption."""
    base = Path(base_dir)
    base.mkdir(parents=True, exist_ok=True)
    md_path = base / markdown_name
    md_path.write_text(paper_memory_markdown(memory), encoding="utf-8")
    packs_dir = base / packs_dir_name
    packs_dir.mkdir(parents=True, exist_ok=True)
    paths = {"paper_memory_md": str(md_path)}
    for filename, text in paper_evidence_packs(memory).items():
        pack_path = packs_dir / filename
        pack_path.write_text(text, encoding="utf-8")
    paths["paper_evidence_packs"] = str(packs_dir)
    return paths


def build_paper_memory(
    *,
    pdf_path: Path,
    page_texts: list[str],
    manifest: dict[str, Any],
    body_window: dict[str, Any],
    rendered_layers: dict[str, Any] | None = None,
    registered_layer_ids: list[str] | None = None,
    recommended_text_units: dict[str, list[dict[str, Any]]] | None = None,
    parser_version: str = PAPER_MEMORY_PARSER_VERSION,
) -> dict[str, Any]:
    """Build memory chunks from body-only PDF text and ingest metadata."""
    key = paper_memory_cache_key(
        pdf_path=pdf_path,
        body_window=body_window,
        manifest=manifest,
        rendered_layers=rendered_layers,
        registered_layer_ids=registered_layer_ids,
        recommended_text_units=recommended_text_units,
        parser_version=parser_version,
    )
    chunks: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    rendered = rendered_layers if isinstance(rendered_layers, dict) else {}
    registered_ids = [str(v) for v in (registered_layer_ids or []) if str(v or "").strip()]
    pdf_sha = _safe_sha256(pdf_path)
    body_fp = _body_window_fingerprint(body_window)

    def add(
        category: str,
        text: Any,
        *,
        page: Any = None,
        section: Any = None,
        source_id: Any = None,
        source_ids: Any = None,
        parent_source_id: Any = None,
        quote: Any = None,
        evidence_kind: str = "derived_summary",
        safe_to_quote: bool | None = None,
        confidence: float | None = None,
        metadata: dict[str, Any] | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> None:
        clean = _clean_text(text)
        if not clean:
            return
        dedupe = (category, clean.lower())
        if dedupe in seen:
            return
        seen.add(dedupe)
        sid = f"pm_{len(chunks) + 1:04d}"
        ids = _source_ids(source_ids)
        if source_id:
            ids = _dedupe([str(source_id), *ids])
        parent_id = _clean_text(parent_source_id)
        if parent_id:
            ids = _dedupe([*ids, parent_id])
        evidence = _normalize_evidence_kind(evidence_kind)
        quote_text = _clean_text(quote) or clean
        safe = _default_safe_to_quote(evidence, category) if safe_to_quote is None else bool(safe_to_quote)
        chunk = {
            "id": sid,
            "chunk_id": f"paper:{sid}",
            "category": category,
            "evidence_kind": evidence,
            "safe_to_quote": safe,
            "text": _clip(clean, 1200),
            "quote": _clip(quote_text, 600),
            "page": _safe_page(page),
            "section": _clip(_clean_text(section), 160) if section else None,
            "source_id": str(source_id)[:160] if source_id else None,
            "parent_source_id": parent_id[:160] if parent_id else None,
            "source_ids": ids,
            "confidence": _safe_confidence(confidence),
            "provenance": {
                "pdf_sha256": pdf_sha,
                "parser_version": parser_version,
                "body_window": body_fp,
                **(_jsonable(provenance) if provenance else {}),
            },
        }
        if metadata:
            chunk["metadata"] = _jsonable(metadata)
        chunks.append({k: v for k, v in chunk.items() if v not in (None, [], {})})

    title = _clean_text(manifest.get("title")) or pdf_path.stem
    authors = [
        _clean_text(author)
        for author in (manifest.get("authors") or [])
        if _clean_text(author)
    ]
    abstract = _clean_text(manifest.get("abstract"))
    add(
        "title_authors_abstract",
        " ".join(part for part in [
            f"Title: {title}" if title else "",
            f"Authors: {', '.join(authors[:20])}" if authors else "",
            f"Abstract: {abstract}" if abstract else "",
        ] if part),
        page=1,
        section="front_matter",
        evidence_kind="derived_summary",
        safe_to_quote=False,
        metadata={"title": title, "authors": authors[:40], "venue": manifest.get("venue")},
    )

    for idx, section in enumerate(manifest.get("sections") or [], start=1):
        if not isinstance(section, dict):
            continue
        heading = _clean_text(section.get("title") or section.get("heading") or f"Section {idx}")
        summary = _clean_text(section.get("summary") or section.get("text") or section.get("description"))
        add(
            "section_summary",
            f"{heading}: {summary}" if summary else heading,
            page=section.get("page") or section.get("start_page"),
            section=heading,
            source_id=f"section:{idx}",
            evidence_kind="derived_summary",
            safe_to_quote=False,
            metadata={k: v for k, v in section.items() if k in {"idx", "page", "start_page"}},
        )

    for idx, item in enumerate(manifest.get("key_quotes") or [], start=1):
        if isinstance(item, dict):
            text = item.get("quote") or item.get("text") or item.get("raw_quote")
            page = item.get("page")
            section = item.get("section")
        else:
            text = item
            page = None
            section = None
        add("key_quote", text, page=page, section=section, source_id=f"quote:{idx}", evidence_kind="verbatim", safe_to_quote=True)

    for idx, fig in enumerate(manifest.get("figures") or [], start=1):
        if not isinstance(fig, dict):
            continue
        caption = fig.get("caption") or fig.get("description")
        add(
            "figure_caption",
            caption,
            page=fig.get("page"),
            section=fig.get("section"),
            source_id=fig.get("source_id") or f"figure:{idx}",
            parent_source_id=fig.get("parent_source_id") or fig.get("parent_id"),
            evidence_kind="extracted",
            safe_to_quote=True,
            metadata={k: v for k, v in fig.items() if k in {"label", "page", "kind", "source_bbox_pdf_points"}},
        )
    for idx, table in enumerate(manifest.get("tables") or [], start=1):
        if not isinstance(table, dict):
            continue
        caption = table.get("caption") or table.get("description") or table.get("title")
        add(
            "table_caption",
            caption,
            page=table.get("page"),
            section=table.get("section"),
            source_id=table.get("source_id") or f"table:{idx}",
            parent_source_id=table.get("parent_source_id") or table.get("parent_id"),
            evidence_kind="extracted",
            safe_to_quote=True,
            metadata={k: v for k, v in table.items() if k in {"label", "page", "kind", "source_bbox_pdf_points"}},
        )

    for layer_id in registered_ids:
        rec = rendered.get(layer_id)
        if not isinstance(rec, dict):
            continue
        caption = rec.get("caption") or rec.get("caption_full") or rec.get("caption_short") or rec.get("title")
        kind = str(rec.get("kind") or rec.get("asset_type") or "")
        category = "table_caption" if layer_id.startswith("ingest_table_") or kind == "table" else "figure_caption"
        parent_id = _parent_source_id(rec)
        add(
            category,
            caption,
            page=rec.get("source_page"),
            source_id=layer_id,
            parent_source_id=parent_id,
            evidence_kind="extracted",
            safe_to_quote=True,
            metadata={
                "source_ref": rec.get("source_ref"),
                "image_size": rec.get("image_size"),
                "visual_role": rec.get("visual_role"),
                "source_bbox_pdf_points": rec.get("source_bbox_pdf_points"),
            },
        )
        if layer_id.startswith("ingest_table_") or kind == "table":
            headers = [_clean_text(v) for v in (rec.get("headers") or []) if _clean_text(v)]
            for row_idx, row in enumerate(rec.get("rows") or [], start=1):
                cells = [_clean_text(v) for v in (row or []) if _clean_text(v)]
                if not cells:
                    continue
                add(
                    "table_row",
                    " | ".join([*headers[:8], *cells[:12]]) if headers else " | ".join(cells[:12]),
                    page=rec.get("source_page"),
                    source_id=layer_id,
                    parent_source_id=parent_id,
                    evidence_kind="normalized_table",
                    safe_to_quote=False,
                    metadata={"row_index": row_idx},
                )

    for bucket, items in (recommended_text_units or {}).items():
        category = _text_unit_category(str(bucket))
        for idx, item in enumerate(items or [], start=1):
            if not isinstance(item, dict):
                continue
            add(
                category,
                item.get("text") or item.get("quote") or item.get("summary"),
                page=item.get("page"),
                section=item.get("section") or item.get("panel_role"),
                source_id=item.get("source") or item.get("claim_id") or f"{bucket}:{idx}",
                source_ids=item.get("source_ids"),
                quote=item.get("quote") or item.get("raw_quote"),
                evidence_kind="verbatim" if (item.get("quote") or item.get("raw_quote")) else "derived_summary",
                safe_to_quote=bool(item.get("quote") or item.get("raw_quote")),
                metadata={k: v for k, v in item.items() if k in {"bucket", "panel_role", "intended_panel_role"}},
            )

    _add_body_text_chunks(add, page_texts)

    return {
        "kind": "paper_memory",
        "version": PAPER_MEMORY_VERSION,
        "parser_version": parser_version,
        "cache_key": key,
        "source_file": str(pdf_path),
        "pdf_sha256": _safe_sha256(pdf_path),
        "body_window": _body_window_fingerprint(body_window),
        "metadata": {
            "title": title,
            "authors": authors[:40],
            "venue": manifest.get("venue"),
            "abstract": abstract,
        },
        "input_fingerprint": _paper_memory_input_fingerprint(
            manifest=manifest,
            rendered_layers=rendered_layers,
            registered_layer_ids=registered_layer_ids,
            recommended_text_units=recommended_text_units,
        ),
        "chunk_count": len(chunks),
        "categories": dict(Counter(str(chunk.get("category")) for chunk in chunks)),
        "evidence_kinds": dict(Counter(str(chunk.get("evidence_kind")) for chunk in chunks)),
        "chunks": chunks,
    }


def compact_paper_memory_for_planner(memory: dict[str, Any] | None, *, max_chunks: int | None = None) -> dict[str, Any]:
    if not isinstance(memory, dict) or memory.get("kind") != "paper_memory":
        return {}
    chunks = [chunk for chunk in (memory.get("chunks") or []) if isinstance(chunk, dict)]
    if max_chunks is not None and max_chunks > 0:
        chunks = chunks[:max_chunks]
    return {
        "kind": "paper_memory",
        "cache_key": memory.get("cache_key"),
        "source_file": memory.get("source_file"),
        "metadata": memory.get("metadata") or {},
        "chunk_count": memory.get("chunk_count"),
        "categories": memory.get("categories") or {},
        "sample_chunks": [
            {
                "id": chunk.get("id"),
                "category": chunk.get("category"),
                "evidence_kind": chunk.get("evidence_kind"),
                "safe_to_quote": chunk.get("safe_to_quote"),
                "page": chunk.get("page"),
                "section": chunk.get("section"),
                "source_id": chunk.get("source_id"),
                "parent_source_id": chunk.get("parent_source_id"),
                "quote": chunk.get("quote"),
            }
            for chunk in chunks
        ],
    }


def merge_paper_memories(memories: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [m for m in memories if isinstance(m, dict) and m.get("kind") == "paper_memory"]
    if not valid:
        return {}
    if len(valid) == 1:
        return valid[0]
    chunks: list[dict[str, Any]] = []
    for idx, memory in enumerate(valid, start=1):
        prefix = f"doc{idx}"
        for chunk in memory.get("chunks") or []:
            if not isinstance(chunk, dict):
                continue
            cloned = dict(chunk)
            cloned["id"] = f"{prefix}_{chunk.get('id')}"
            cloned["document_index"] = idx
            chunks.append(cloned)
    return {
        "kind": "paper_memory",
        "version": PAPER_MEMORY_VERSION,
        "parser_version": PAPER_MEMORY_PARSER_VERSION,
        "cache_key": stable_cache_key({"stage": "paper_memory_collection", "keys": [m.get("cache_key") for m in valid]}),
        "source_file": None,
        "documents": [
            {
                "cache_key": m.get("cache_key"),
                "source_file": m.get("source_file"),
                "metadata": m.get("metadata") or {},
                "chunk_count": m.get("chunk_count"),
            }
            for m in valid
        ],
        "chunk_count": len(chunks),
        "categories": dict(Counter(str(chunk.get("category")) for chunk in chunks)),
        "evidence_kinds": dict(Counter(str(chunk.get("evidence_kind")) for chunk in chunks)),
        "chunks": chunks,
    }


def retrieve_paper_context(
    memory: dict[str, Any] | None,
    *,
    query: str,
    panel_role: str | None = None,
    source_ids: list[str] | None = None,
    categories: list[str] | None = None,
    evidence_kind: list[str] | None = None,
    safe_to_quote: bool | None = None,
    top_k: int | None = None,
) -> dict[str, Any]:
    """Return ranked source chunks from a paper memory payload."""
    if not isinstance(memory, dict) or memory.get("kind") != "paper_memory":
        return {"query": query, "results": [], "error": "paper_memory_missing"}
    chunks = [chunk for chunk in (memory.get("chunks") or []) if isinstance(chunk, dict)]
    if not chunks:
        return {"query": query, "results": [], "error": "paper_memory_empty"}

    wanted_sources = {str(v) for v in (source_ids or []) if str(v or "").strip()}
    wanted_categories = {str(v) for v in (categories or []) if str(v or "").strip()}
    wanted_evidence = {_normalize_evidence_kind(v) for v in (evidence_kind or []) if str(v or "").strip()}
    role_categories = _CATEGORY_QUERY_HINTS.get(str(panel_role or "").strip(), set())
    q_tokens = _tokens(" ".join([query or "", panel_role or "", " ".join(wanted_categories), " ".join(wanted_evidence)]))
    if not q_tokens and not wanted_sources and not wanted_categories and not wanted_evidence:
        return {"query": query, "results": [], "error": "empty_query"}

    docs = []
    for chunk in chunks:
        if wanted_categories and str(chunk.get("category") or "") not in wanted_categories:
            continue
        if wanted_evidence and _normalize_evidence_kind(chunk.get("evidence_kind")) not in wanted_evidence:
            continue
        if safe_to_quote is not None and bool(chunk.get("safe_to_quote")) != bool(safe_to_quote):
            continue
        chunk_sources = _chunk_source_ids(chunk)
        if wanted_sources and not chunk_sources.intersection(wanted_sources):
            continue
        text = " ".join([
            str(chunk.get("category") or ""),
            str(chunk.get("evidence_kind") or ""),
            str(chunk.get("section") or ""),
            str(chunk.get("source_id") or ""),
            str(chunk.get("parent_source_id") or ""),
            str(chunk.get("text") or ""),
            str(chunk.get("quote") or ""),
        ])
        toks = _tokens(text)
        if toks or wanted_sources or wanted_categories:
            docs.append((chunk, toks))
    if not docs:
        return {"query": query, "results": [], "error": "no_matching_chunks"}

    avg_len = sum(len(toks) for _, toks in docs) / max(1, len(docs))
    df = Counter()
    for _, toks in docs:
        for tok in set(toks):
            df[tok] += 1
    scores: list[tuple[float, dict[str, Any]]] = []
    for chunk, toks in docs:
        tf = Counter(toks)
        length = max(1, len(toks))
        score = 0.0
        for tok in q_tokens:
            freq = tf.get(tok, 0)
            if not freq:
                continue
            idf = math.log(1.0 + (len(docs) - df[tok] + 0.5) / (df[tok] + 0.5))
            denom = freq + 1.2 * (1 - 0.75 + 0.75 * length / max(1.0, avg_len))
            score += idf * (freq * 2.2 / denom)
        category = str(chunk.get("category") or "")
        if wanted_categories and category in wanted_categories:
            score += 1.25
        if role_categories and category in role_categories:
            score += 0.8
        if wanted_sources:
            chunk_sources = _chunk_source_ids(chunk)
            if chunk_sources.intersection(wanted_sources):
                score += 2.5
        evidence = _normalize_evidence_kind(chunk.get("evidence_kind"))
        if wanted_evidence and evidence in wanted_evidence:
            score += 1.0
        if bool(chunk.get("safe_to_quote")):
            score += 0.25
        if evidence in {"verbatim", "extracted"}:
            score += 0.2
        if score <= 0 and (wanted_sources or wanted_categories or wanted_evidence):
            score = 0.1
        if score > 0:
            scores.append((score, chunk))
    scores.sort(key=lambda item: (-item[0], str(item[1].get("id") or "")))
    result_limit = _result_limit(top_k, len(scores))
    selected = _select_diverse_chunks(scores, result_limit)
    return {
        "query": query,
        "source": "lexical",
        "mode": "raw",
        "panel_role": panel_role,
        "source_ids": sorted(wanted_sources),
        "categories": sorted(wanted_categories),
        "evidence_kind": sorted(wanted_evidence),
        "safe_to_quote": safe_to_quote,
        "top_k": result_limit,
        "results": [
            {
                "source": "lexical",
                "id": chunk.get("id"),
                "chunk_id": chunk.get("chunk_id"),
                "category": chunk.get("category"),
                "evidence_kind": chunk.get("evidence_kind"),
                "safe_to_quote": bool(chunk.get("safe_to_quote")),
                "score": round(score, 4),
                "page": chunk.get("page"),
                "section": chunk.get("section"),
                "source_id": chunk.get("source_id"),
                "parent_source_id": chunk.get("parent_source_id"),
                "source_ids": chunk.get("source_ids") or [],
                "quote": chunk.get("quote"),
                "snippet": _clip(str(chunk.get("text") or ""), 500),
                "text": chunk.get("text"),
                "metadata": chunk.get("metadata") or {},
            }
            for score, chunk in selected
        ],
    }


def build_paper_memory_index(memory: dict[str, Any]) -> dict[str, Any]:
    """Build a small deterministic lexical index sidecar.

    Retrieval currently recomputes BM25 in memory, but this sidecar makes cache
    contents inspectable and gives us a stable place to move precomputed stats
    later without changing payload.json.
    """
    chunks = [chunk for chunk in (memory.get("chunks") or []) if isinstance(chunk, dict)]
    token_df: Counter[str] = Counter()
    docs: list[dict[str, Any]] = []
    for chunk in chunks:
        toks = _tokens(" ".join([
            str(chunk.get("category") or ""),
            str(chunk.get("evidence_kind") or ""),
            str(chunk.get("section") or ""),
            str(chunk.get("source_id") or ""),
            str(chunk.get("parent_source_id") or ""),
            str(chunk.get("text") or ""),
            str(chunk.get("quote") or ""),
        ]))
        token_df.update(set(toks))
        docs.append({
            "id": chunk.get("id"),
            "category": chunk.get("category"),
            "evidence_kind": chunk.get("evidence_kind"),
            "safe_to_quote": bool(chunk.get("safe_to_quote")),
            "token_count": len(toks),
        })
    return {
        "kind": "paper_memory_index",
        "version": 1,
        "cache_key": memory.get("cache_key"),
        "chunk_count": len(chunks),
        "avg_token_count": round(sum(doc["token_count"] for doc in docs) / max(1, len(docs)), 2),
        "top_terms": [term for term, _count in token_df.most_common(80)],
        "docs": docs,
    }


def paper_memory_markdown(memory: dict[str, Any], *, max_chars: int | None = None) -> str:
    """Return a Markdown projection for LLM readers."""
    if not isinstance(memory, dict) or memory.get("kind") != "paper_memory":
        return "# Paper Memory\n\nNo paper memory is available.\n"
    meta = memory.get("metadata") if isinstance(memory.get("metadata"), dict) else {}
    chunks = [chunk for chunk in (memory.get("chunks") or []) if isinstance(chunk, dict)]
    lines = [
        "# Paper Memory",
        "",
        "## Identity",
        f"- Title: {_clean_text(meta.get('title')) or 'Unknown'}",
        f"- Authors: {_clean_text(', '.join(str(a) for a in (meta.get('authors') or [])[:12])) or 'Unknown'}",
        f"- Venue: {_clean_text(meta.get('venue')) or 'Unknown'}",
        f"- Cache key: {_clean_text(memory.get('cache_key'))[:24]}",
        "",
        "## How To Use",
        "- Treat `verbatim` and `extracted` chunks with `safe_to_quote=true` as directly citable.",
        "- Treat `derived_summary` chunks as planning summaries, not direct quotes.",
        "- Prefer page/section/source metadata when moving text into poster panels.",
        "",
    ]
    sections = [
        ("High-Confidence Quotes", _filter_chunks(chunks, safe=True, categories={"key_quote", "figure_caption", "table_caption", "numeric_claim"})),
        ("Numeric Claims And Results", _filter_chunks(chunks, categories={"numeric_claim", "result_unit", "table_row", "table_caption"})),
        ("Methods", _filter_chunks(chunks, categories={"method_unit", "figure_caption", "section_summary"})),
        ("Figures And Tables", _filter_chunks(chunks, categories={"figure_caption", "table_caption", "table_row"})),
        ("Limitations And Takeaways", _filter_chunks(chunks, categories={"limitation_unit", "takeaway_unit", "section_summary"})),
    ]
    for title, items in sections:
        lines.extend(["", f"## {title}"])
        if not items:
            lines.append("- None captured.")
            continue
        for chunk in items:
            lines.append(_chunk_markdown_bullet(chunk))
    text = "\n".join(lines).rstrip() + "\n"
    if max_chars is None or max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[: max_chars - 80].rstrip() + "\n\n<!-- Paper memory projection truncated. Use paper_memory.json for full canonical memory. -->\n"


def paper_evidence_packs(memory: dict[str, Any], *, max_items: int | None = None) -> dict[str, str]:
    """Return panel-role Markdown packs derived from canonical memory."""
    if not isinstance(memory, dict) or memory.get("kind") != "paper_memory":
        return {}
    chunks = [chunk for chunk in (memory.get("chunks") or []) if isinstance(chunk, dict)]
    pack_specs = {
        "method.md": {"method_unit", "figure_caption", "section_summary", "key_quote"},
        "results.md": {"result_unit", "numeric_claim", "table_row", "table_caption", "key_quote"},
        "benchmark.md": {"numeric_claim", "table_row", "table_caption", "result_unit"},
        "limitations.md": {"limitation_unit", "takeaway_unit", "section_summary", "key_quote"},
        "visuals.md": {"figure_caption", "table_caption", "table_row"},
        "takeaways.md": {"takeaway_unit", "result_unit", "limitation_unit", "key_quote"},
    }
    packs: dict[str, str] = {}
    for filename, categories in pack_specs.items():
        title = filename[:-3].replace("_", " ").title()
        items = _filter_chunks(chunks, categories=categories)
        lines = [
            f"# Paper Evidence Pack: {title}",
            "",
            "Use `safe_to_quote=true` items as direct citations; use summaries only as planning guidance.",
            "",
        ]
        if not items:
            lines.append("- No matching evidence captured.")
        else:
            selected_items = items[:max_items] if max_items is not None and max_items > 0 else items
            for chunk in selected_items:
                lines.append(_chunk_markdown_bullet(chunk))
        packs[filename] = "\n".join(lines).rstrip() + "\n"
    return packs


def _add_body_text_chunks(add: Any, page_texts: list[str]) -> None:
    for page_idx, page_text in enumerate(page_texts, start=1):
        for sentence in _candidate_sentences(page_text):
            category = _sentence_category(sentence)
            if category:
                add(
                    category,
                    sentence,
                    page=page_idx,
                    source_id=f"page:{page_idx}",
                    quote=sentence,
                    evidence_kind="extracted",
                    safe_to_quote=True,
                )


def _candidate_sentences(text: str) -> list[str]:
    clean = _clean_text(text)
    if not clean:
        return []
    out: list[str] = []
    for raw in _SENTENCE_RE.split(clean):
        sent = _clean_text(raw)
        words = sent.split()
        if 7 <= len(words) <= 60:
            out.append(sent)
    return out


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


def _sentence_category(sentence: str) -> str:
    lower = sentence.lower()
    has_num = bool(_NUMERIC_RE.search(lower))
    if has_num and any(t in lower for t in ("result", "benchmark", "accuracy", "score", "table", "evaluation", "improve", "outperform", "baseline", "ablation")):
        return "numeric_claim"
    if any(t in lower for t in ("we propose", "method", "architecture", "framework", "pipeline", "training", "objective", "model")):
        return "method_unit"
    if any(t in lower for t in ("result", "benchmark", "experiment", "evaluation", "ablation", "performance", "outperform")):
        return "result_unit"
    if any(t in lower for t in ("limitation", "future work", "fails", "cannot", "challenge", "caveat")):
        return "limitation_unit"
    if any(t in lower for t in ("conclude", "takeaway", "demonstrate", "show that", "suggests")):
        return "takeaway_unit"
    return ""


def _text_unit_category(bucket: str) -> str:
    return {
        "problem": "problem_unit",
        "method": "method_unit",
        "evidence": "result_unit",
        "limitations": "limitation_unit",
        "takeaways": "takeaway_unit",
    }.get(bucket, "text_unit")


def _paper_memory_input_fingerprint(
    *,
    manifest: dict[str, Any] | None = None,
    rendered_layers: dict[str, Any] | None = None,
    registered_layer_ids: list[str] | None = None,
    recommended_text_units: dict[str, list[dict[str, Any]]] | None = None,
) -> str:
    rendered = rendered_layers if isinstance(rendered_layers, dict) else {}
    registered = [str(v) for v in (registered_layer_ids or []) if str(v or "").strip()]
    visual_records = []
    for layer_id in registered[:160]:
        rec = rendered.get(layer_id)
        if not isinstance(rec, dict):
            continue
        visual_records.append({
            "layer_id": layer_id,
            "kind": rec.get("kind") or rec.get("asset_type"),
            "source_page": rec.get("source_page"),
            "caption": rec.get("caption") or rec.get("caption_full") or rec.get("caption_short") or rec.get("title"),
            "headers": rec.get("headers"),
            "rows": rec.get("rows"),
            "parent_source_id": _parent_source_id(rec),
        })
    m = manifest if isinstance(manifest, dict) else {}
    manifest_fp = {
        "title": m.get("title"),
        "authors": m.get("authors"),
        "abstract": m.get("abstract"),
        "venue": m.get("venue"),
        "sections": [
            {
                "title": sec.get("title") or sec.get("heading"),
                "summary": sec.get("summary") or sec.get("text") or sec.get("description"),
                "page": sec.get("page") or sec.get("start_page"),
            }
            for sec in (m.get("sections") or [])
            if isinstance(sec, dict)
        ],
        "figures": [
            {
                "caption": fig.get("caption") or fig.get("description"),
                "page": fig.get("page"),
                "source_id": fig.get("source_id"),
            }
            for fig in (m.get("figures") or [])
            if isinstance(fig, dict)
        ],
        "tables": [
            {
                "caption": tab.get("caption") or tab.get("description") or tab.get("title"),
                "page": tab.get("page"),
                "source_id": tab.get("source_id"),
            }
            for tab in (m.get("tables") or [])
            if isinstance(tab, dict)
        ],
        "key_quotes": m.get("key_quotes"),
    }
    return stable_cache_key({
        "version": PAPER_MEMORY_INPUT_VERSION,
        "manifest": manifest_fp,
        "registered_layer_ids": registered,
        "visual_records": visual_records,
        "recommended_text_units": recommended_text_units or {},
    })


def _normalize_evidence_kind(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    if raw in {"verbatim", "extracted", "derived_summary", "normalized_table"}:
        return raw
    if raw in {"summary", "derived"}:
        return "derived_summary"
    if raw in {"table", "table_row"}:
        return "normalized_table"
    return "derived_summary"


def _default_safe_to_quote(evidence_kind: str, category: str) -> bool:
    if evidence_kind in {"verbatim", "extracted"}:
        return True
    if category in {"figure_caption", "table_caption"}:
        return True
    return False


def _safe_confidence(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(min(1.0, max(0.0, number)), 3)


def _parent_source_id(record: dict[str, Any]) -> str:
    provenance = record.get("provenance") if isinstance(record.get("provenance"), dict) else {}
    for key in ("parent_source_id", "parent_layer_id", "source_parent_id", "parent_id"):
        value = record.get(key) or provenance.get(key)
        clean = _clean_text(value)
        if clean:
            return clean
    source_ref = _clean_text(record.get("source_ref"))
    if source_ref and source_ref != _clean_text(record.get("layer_id")):
        return source_ref
    return ""


def _chunk_source_ids(chunk: dict[str, Any]) -> set[str]:
    return {
        value for value in {
            str(chunk.get("source_id") or "").strip(),
            str(chunk.get("parent_source_id") or "").strip(),
            *[str(v or "").strip() for v in (chunk.get("source_ids") or [])],
        }
        if value
    }


def _select_diverse_chunks(
    scores: list[tuple[float, dict[str, Any]]],
    top_k: int,
) -> list[tuple[float, dict[str, Any]]]:
    selected: list[tuple[float, dict[str, Any]]] = []
    section_counts: Counter[str] = Counter()
    for score, chunk in scores:
        key = str(chunk.get("section") or chunk.get("category") or "")
        if section_counts[key] >= 2:
            continue
        selected.append((score, chunk))
        section_counts[key] += 1
        if len(selected) >= top_k:
            return selected
    seen = {id(chunk) for _score, chunk in selected}
    for score, chunk in scores:
        if id(chunk) in seen:
            continue
        selected.append((score, chunk))
        if len(selected) >= top_k:
            break
    return selected


def _filter_chunks(
    chunks: list[dict[str, Any]],
    *,
    categories: set[str] | None = None,
    safe: bool | None = None,
) -> list[dict[str, Any]]:
    out = []
    for chunk in chunks:
        if categories and str(chunk.get("category") or "") not in categories:
            continue
        if safe is not None and bool(chunk.get("safe_to_quote")) != safe:
            continue
        out.append(chunk)
    return sorted(
        out,
        key=lambda chunk: (
            0 if bool(chunk.get("safe_to_quote")) else 1,
            _safe_page(chunk.get("page")) or 9999,
            str(chunk.get("id") or ""),
        ),
    )


def _chunk_markdown_bullet(chunk: dict[str, Any]) -> str:
    citation = []
    if chunk.get("page"):
        citation.append(f"p.{chunk.get('page')}")
    if chunk.get("section"):
        citation.append(str(chunk.get("section")))
    if chunk.get("source_id"):
        citation.append(str(chunk.get("source_id")))
    prefix = f"[{', '.join(citation)}] " if citation else ""
    kind = str(chunk.get("evidence_kind") or "derived_summary")
    safe = "safe_to_quote=true" if bool(chunk.get("safe_to_quote")) else "safe_to_quote=false"
    text = _clean_text(chunk.get("quote") or chunk.get("text"))
    return f"- {prefix}({chunk.get('category')}; {kind}; {safe}) {_clip(text, 360)}"


def _tokens(text: str) -> list[str]:
    return [tok.lower() for tok in _TOKEN_RE.findall(text or "") if len(tok) >= 2]


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\x00", " ")).strip()


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _source_ids(value: Any) -> list[str]:
    if isinstance(value, list):
        return _dedupe([str(v) for v in value if str(v or "").strip()])
    if value:
        return [str(value)[:160]]
    return []


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = str(value or "").strip()[:160]
        if clean and clean not in seen:
            out.append(clean)
            seen.add(clean)
    return out


def _safe_page(value: Any) -> int | None:
    try:
        page = int(value)
    except Exception:
        return None
    return page if page > 0 else None


def _safe_sha256(path: Path) -> str:
    try:
        return sha256_file(path)
    except Exception:
        return ""


def _body_window_fingerprint(body_window: dict[str, Any]) -> dict[str, Any]:
    return {
        "total_page_count": body_window.get("total_page_count"),
        "body_page_count": body_window.get("body_page_count"),
        "references_start_page": body_window.get("references_start_page"),
        "appendix_start_page": body_window.get("appendix_start_page"),
        "cutoff_start_page": body_window.get("cutoff_start_page"),
        "cutoff_reason": body_window.get("cutoff_reason"),
        "source_scope": body_window.get("source_scope"),
    }


def _jsonable(value: Any) -> dict[str, Any]:
    try:
        payload = json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}
