"""Deck planning helpers.

Deck length should come from user intent or source-document structure, not
from prompt inertia. This module owns the deterministic parts of that contract;
`agents.deck_outline_agent` owns the LLM refinement pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from ..schema import DeckPlan, DesignSpec
from ..util.logging import log


DeckPlanDict = dict[str, Any]

_DECK_TOKENS = (
    "deck", "slide", "slides", "presentation", "talk", "ppt", "pptx",
    "powerpoint", "keynote", "幻灯片", "演示", "演讲稿", "汇报", "答辩",
)
_PITCH_TOKENS = ("pitch", "investor", "fundraising", "roadshow", "融资", "路演")
_REPORT_TOKENS = (
    "report", "报告", "whitepaper", "longer", "long-form", "business",
    "quarterly", "earnings", "revenue", "sales report", "executive update",
    "company profile", "case study", "industry research", "财报", "行业调研",
    "白皮书", "企业介绍",
)
_ACADEMIC_TOKENS = (
    "paper", "academic", "research", "conference", "talk", "neurips",
    "cvpr", "icml", "iclr", "论文", "学术", "会议", "研究",
)
_MOTION_TOKENS = (
    "motion", "video", "frame", "frames", "temporal", "sequence",
    "qualitative", "comparison", "text-to-video", "demo", "视频", "运动",
)
_FULL_FORMAL_TOKENS = (
    "full formal academic conference talk",
    "full formal conference talk",
    "full-length academic talk",
    "full length academic talk",
    "long-form academic talk",
    "long form academic talk",
    "完整正式学术报告",
    "完整正式会议报告",
)
_EXACT_SLIDE_COUNT_PATTERNS = (
    r"(?<![\d:])(\d{1,2})\s*[- ]?\s*(?:slides?\b|幻灯片|页|张|page(?:s)?\b)",
    r"(?:slides?\b|幻灯片|页数|页|张|pages?\b|deck\b)\s*(?:count|数量|数)?\s*[:=：]?\s*(\d{1,2})(?!\d)",
    r"(?:做|生成|create|make)\s*(\d{1,2})\s*(?:页|张|slides?\b|pages?\b)",
)


@dataclass(frozen=True)
class DeckPlanValidation:
    status: Literal["error", "override", "mismatch"]
    message: str
    payload: dict[str, Any]
    event: str


def plan_deck(
    brief: str,
    attachments: list[Path],
    *,
    canvas_plan: dict[str, Any] | None = None,
) -> DeckPlanDict:
    """Return an initial deck plan from raw user wording.

    Only the raw user brief is allowed to create a hard slide-count lock; the
    prompt enhancer may invent useful outline counts, but those are advisory.
    """
    if not _is_deck_intent(brief, canvas_plan):
        return {}
    explicit = parse_explicit_slide_count(brief)
    explicit_range = parse_slide_count_range(brief)
    subtype = _deck_subtype(brief, attachments)
    if explicit is not None:
        count = explicit
        return _plan(
            deck_subtype=subtype,
            slide_count=count,
            count_range=[count, count],
            lock_level="hard",
            status="explicit",
            source="explicit_user",
            rationale=f"User explicitly requested {count} slides/pages.",
            document_signals={"explicit_slide_count": count},
            outline=_default_outline(count, subtype),
        )

    if (
        subtype == "academic-paper-talk"
        and _has_any((brief or "").lower(), _FULL_FORMAL_TOKENS)
    ):
        count = 24
        return _plan(
            deck_subtype=subtype,
            slide_count=count,
            count_range=[20, 26],
            lock_level="soft",
            status="fallback",
            source="academic_full_formal_default",
            rationale=(
                "The user requested a full formal academic conference talk; "
                "use a 20-26 slide evidence-rich narrative."
            ),
            document_signals={"has_source_document": bool(attachments)},
            outline=_default_outline(count, subtype),
        )

    if (
        explicit_range is None
        and subtype == "academic-paper-talk"
        and not _has_non_exact_slide_count_request(brief)
    ):
        return _plan(
            deck_subtype=subtype,
            slide_count=18,
            count_range=[18, 18],
            lock_level="soft",
            status="fallback",
            source="academic_default",
            rationale=(
                "Academic decks default to exactly 18 slides unless the user "
                "explicitly requests another count, a range, or no fixed count."
            ),
            document_signals={"has_source_document": bool(attachments)},
            outline=_default_outline(18, subtype),
        )

    has_source_doc = bool(attachments)
    count_range = explicit_range or _default_count_range(subtype)
    advisory_count = None if has_source_doc else max(1, round(sum(count_range) / 2))
    return _plan(
        deck_subtype=subtype,
        slide_count=advisory_count,
        count_range=count_range,
        lock_level="soft" if has_source_doc else "advisory",
        status="pending" if has_source_doc else "fallback",
        source="explicit_user_range" if explicit_range else "pre_ingest",
        rationale=(
            f"User requested a slide-count range of {count_range[0]}-{count_range[1]}; "
            "exact slide count is deferred until document ingestion exposes "
            "sections, figures, tables, and claim graph."
            if explicit_range and has_source_doc else
            f"User requested a slide-count range of {count_range[0]}-{count_range[1]}; "
            "using the midpoint as an advisory count without a source document."
            if explicit_range else
            "Deck intent was detected; exact slide count is deferred until "
            "document ingestion exposes sections, figures, tables, and claim graph."
            if has_source_doc else
            "Deck intent was detected without a source document; provide an "
            "advisory count and let the planner adapt."
        ),
        document_signals={"has_source_document": has_source_doc},
        outline=[],
    )


def parse_explicit_slide_count(text: str) -> int | None:
    """Extract a user-authored slide/page count from raw wording."""
    raw = text or ""
    range_spans = _slide_count_range_spans(raw)
    lowered = raw.lower()
    for pattern in _EXACT_SLIDE_COUNT_PATTERNS:
        for match in re.finditer(pattern, lowered, flags=re.IGNORECASE):
            if _match_inside_any_span(match, range_spans):
                continue
            if _match_has_non_exact_count_cue(match, lowered):
                continue
            try:
                value = int(match.group(1))
            except (TypeError, ValueError):
                continue
            if 1 <= value <= 60:
                return value
    return None


def _has_non_exact_slide_count_request(text: str) -> bool:
    lowered = (text or "").lower()
    for pattern in _EXACT_SLIDE_COUNT_PATTERNS:
        for match in re.finditer(pattern, lowered, flags=re.IGNORECASE):
            if _match_has_non_exact_count_cue(match, lowered):
                return True
    return False


def parse_slide_count_range(text: str) -> list[int] | None:
    """Extract a user-authored slide/page count range, without making it exact."""
    for start, end, lo, hi in _slide_count_range_spans(text or ""):
        _ = (start, end)
        if 1 <= lo <= hi <= 60:
            return [lo, hi]
    return None


def apply_deck_plan_prologue(brief: str, plan: DeckPlanDict | None) -> str:
    block = deck_plan_prologue_block(plan)
    if not block:
        return brief
    return block + "\n\n---\n\n" + brief


def deck_plan_prologue_block(plan: DeckPlanDict | None) -> str:
    if not plan or plan.get("artifact_type") != "deck":
        return ""
    budget = plan.get("density_budget") or {}
    count = plan.get("slide_count")
    count_text = "pending" if count is None else str(count)
    outline = plan.get("outline") or []
    outline_lines = []
    for item in outline[:24]:
        if not isinstance(item, dict):
            continue
        idx = item.get("slide_index")
        title = str(item.get("title") or "").strip()
        role = str(item.get("role") or "").strip()
        chapter = str(item.get("chapter") or "").strip()
        layout = str(item.get("layout_family") or "").strip()
        visuals = item.get("visual_refs") or []
        visual_text = f"; visuals={visuals[:4]}" if visuals else ""
        metadata = ", ".join(value for value in (role, chapter, layout) if value)
        outline_lines.append(f"    {idx}. {title} ({metadata}){visual_text}")
    outline_block = "\n".join(outline_lines) if outline_lines else "    (pending until document ingest)"
    return (
        "Deck Plan:\n"
        f"  artifact_type: {plan.get('artifact_type')}\n"
        f"  deck_subtype: {plan.get('deck_subtype')}\n"
        f"  talk_profile: {plan.get('talk_profile')}\n"
        f"  slide_count: {count_text}\n"
        f"  count_range: {plan.get('count_range')}\n"
        f"  lock_level: {plan.get('lock_level')}\n"
        f"  status: {plan.get('status')}\n"
        f"  source: {plan.get('source')}\n"
        f"  density_budget: {_compact_kv(budget)}\n"
        f"  rationale: {plan.get('rationale')}\n"
        "  outline:\n"
        f"{outline_block}\n\n"
        "Use this deck_plan for DesignSpec.html_artifact slide count and order. "
        "For lock_level=hard, the exact slide_count is required. For "
        "lock_level=soft, keep the exact slide_count unless you add "
        "`deck_plan_override_reason` to DesignSpec. For pending plans, wait "
        "for ingest_document's returned deck_plan before proposing the deck."
    )


def build_document_signals(
    summaries: list[dict[str, Any]],
    rendered_layers: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    figure_ids: list[str] = []
    table_ids: list[str] = []
    n_sections = 0
    figure_catalog_summary: list[dict[str, Any]] = []
    for summary in summaries:
        figure_ids.extend(list(summary.get("registered_figure_ids") or summary.get("registered_layer_ids") or []))
        table_ids.extend(list(summary.get("registered_table_ids") or []))
        manifest = summary.get("manifest") or {}
        if isinstance(manifest, dict):
            n_sections += len(manifest.get("sections") or [])
        if isinstance(summary.get("figure_catalog_summary"), dict):
            figure_catalog_summary.append(summary["figure_catalog_summary"])

    wide_visuals = 0
    motion_visuals = 0
    for layer_id in figure_ids:
        rec = rendered_layers.get(layer_id) or {}
        if _record_ratio(rec) >= 1.55:
            wide_visuals += 1
        blob = " ".join(str(rec.get(k) or "") for k in (
            "caption", "caption_short", "title", "name", "source_ref",
        )).lower()
        if _has_any(blob, _MOTION_TOKENS):
            motion_visuals += 1
    return {
        "n_documents": len(summaries),
        "n_sections": n_sections,
        "n_registered_figures": len(figure_ids),
        "n_registered_tables": len(table_ids),
        "wide_visuals": wide_visuals,
        "motion_or_sequence_visuals": motion_visuals,
        "registered_figure_ids": figure_ids,
        "registered_table_ids": table_ids,
        "figure_catalog_summary": (
            figure_catalog_summary[0]
            if len(figure_catalog_summary) == 1
            else {"files": figure_catalog_summary}
        ),
    }


def should_refine_deck_plan(plan: DeckPlanDict | None, *, raw_brief: str = "") -> bool:
    if plan and plan.get("artifact_type") == "deck":
        if plan.get("status") in {"explicit", "refined", "fallback"} and plan.get("slide_count"):
            return False
        return True
    return _is_deck_intent(raw_brief, None)


def fallback_deck_plan(
    base_plan: DeckPlanDict | None,
    *,
    raw_brief: str,
    summaries: list[dict[str, Any]],
    rendered_layers: dict[str, dict[str, Any]],
    claim_graph: Any | None = None,
    reason: str = "deck outline agent unavailable",
) -> DeckPlanDict:
    if base_plan and base_plan.get("lock_level") == "hard" and base_plan.get("slide_count"):
        return dict(base_plan)
    signals = build_document_signals(summaries, rendered_layers)
    subtype = str((base_plan or {}).get("deck_subtype") or _deck_subtype(raw_brief, []))
    count = _fallback_slide_count(subtype, signals, claim_graph)
    plan = _plan(
        deck_subtype=subtype,
        slide_count=count,
        count_range=[count, count],
        lock_level="soft",
        status="fallback",
        source="fallback",
        rationale=(
            f"{reason}; deterministic fallback selected {count} slides from "
            f"{signals.get('n_sections')} sections, {signals.get('n_registered_figures')} figures, "
            f"and {signals.get('n_registered_tables')} tables."
        ),
        document_signals=signals,
        outline=_default_outline(count, subtype),
    )
    return plan


def validate_deck_plan_report(
    plan: dict[str, Any],
    *,
    known_visual_refs: set[str] | None = None,
) -> tuple[DeckPlan | None, list[str]]:
    try:
        model = DeckPlan.model_validate(plan)
    except ValidationError as e:
        return None, [f"schema: {e.errors(include_url=False)[:3]}"]
    errors: list[str] = []
    if model.artifact_type != "deck":
        errors.append("artifact_type must be deck")
    if model.slide_count is None:
        errors.append("slide_count is required for a refined deck plan")
    elif not 1 <= int(model.slide_count) <= 60:
        errors.append("slide_count must be between 1 and 60")
    if model.slide_count is not None and len(model.outline) != int(model.slide_count):
        errors.append(
            f"outline length {len(model.outline)} must equal slide_count {model.slide_count}"
        )
    known = known_visual_refs or set()
    for item in model.outline:
        for ref in [*item.visual_refs, *item.evidence_refs]:
            if _allowed_visual_ref(ref, known):
                continue
            errors.append(
                f"slide {item.slide_index} visual_ref {ref!r} is not an ingested or allowed generated ref"
            )
    return model, errors


def validate_deck_plan_for_spec(
    spec: DesignSpec,
    plan: DeckPlanDict | None,
) -> DeckPlanValidation | None:
    if not isinstance(plan, dict) or plan.get("artifact_type") != "deck":
        return None
    if spec.artifact_type.value != "deck":
        return None
    expected = plan.get("slide_count")
    if expected is None:
        return None
    try:
        expected_count = int(expected)
    except (TypeError, ValueError):
        return None
    actual_count = count_spec_slides(spec)
    if actual_count == expected_count:
        return None
    lock_level = str(plan.get("lock_level") or "advisory").strip().lower()
    payload = {
        "deck_plan": plan,
        "expected_slide_count": expected_count,
        "actual_slide_count": actual_count,
    }
    if lock_level == "hard":
        return DeckPlanValidation(
            status="error",
            event="deck_plan.hard_mismatch",
            message="DesignSpec slide count does not match hard deck_plan.",
            payload=payload,
        )
    override_reason = str(getattr(spec, "deck_plan_override_reason", None) or "").strip()
    if lock_level == "soft":
        if override_reason:
            payload["override_reason"] = override_reason
            return DeckPlanValidation(
                status="override",
                event="deck_plan.override",
                message="DesignSpec slide count overrides soft deck_plan.",
                payload=payload,
            )
        return DeckPlanValidation(
            status="error",
            event="deck_plan.soft_mismatch",
            message=(
                "DesignSpec slide count conflicts with soft deck_plan; use the "
                "planned slide_count or add deck_plan_override_reason."
            ),
            payload=payload,
        )
    return DeckPlanValidation(
        status="mismatch",
        event="deck_plan.mismatch",
        message="DesignSpec slide count differs from advisory deck_plan.",
        payload=payload,
    )


def log_deck_plan_validation(result: DeckPlanValidation | None, plan: DeckPlanDict | None = None) -> None:
    if result is None or result.status == "error":
        return
    payload = dict(result.payload)
    payload.setdefault("preset", (plan or {}).get("deck_subtype"))
    log(result.event, **payload)


def count_spec_slides(spec: DesignSpec) -> int:
    artifact = getattr(spec, "html_artifact", None)
    if artifact is not None:
        frames = list(getattr(artifact, "frames", []) or [])
        slide_frames = [f for f in frames if getattr(f, "kind", None) == "slide"]
        if slide_frames:
            return len(slide_frames)
    deck_html = getattr(spec, "deck_html", None)
    if deck_html is not None:
        slides = list(getattr(deck_html, "slides", []) or [])
        if slides:
            return len(slides)
    return _count_legacy_slide_nodes(list(getattr(spec, "layer_graph", []) or []))


def _plan(
    *,
    deck_subtype: str,
    slide_count: int | None,
    count_range: list[int],
    lock_level: str,
    status: str,
    source: str,
    rationale: str,
    document_signals: dict[str, Any],
    outline: list[dict[str, Any]],
) -> DeckPlanDict:
    return {
        "artifact_type": "deck",
        "deck_subtype": deck_subtype,
        "talk_profile": _talk_profile(deck_subtype, slide_count),
        "slide_count": slide_count,
        "count_range": count_range,
        "lock_level": lock_level,
        "status": status,
        "density_budget": _density_budget(deck_subtype, slide_count or max(count_range or [12])),
        "rationale": rationale,
        "source": source,
        "outline": outline,
        "document_signals": document_signals,
    }


def _is_deck_intent(brief: str, canvas_plan: dict[str, Any] | None) -> bool:
    if canvas_plan and canvas_plan.get("artifact_type") == "deck":
        return True
    return _has_any((brief or "").lower(), _DECK_TOKENS)


def _deck_subtype(brief: str, attachments: list[Path]) -> str:
    text = (brief or "").lower()
    has_paper_source = any(path.suffix.lower() == ".pdf" for path in attachments)
    if _has_any(text, _PITCH_TOKENS):
        return "pitch"
    if _has_any(text, _REPORT_TOKENS):
        return "report"
    if has_paper_source or _has_any(text, _ACADEMIC_TOKENS):
        return "academic-paper-talk"
    return "general"


def _default_count_range(subtype: str) -> list[int]:
    if subtype == "pitch":
        return [8, 12]
    if subtype == "report":
        return [15, 25]
    if subtype == "academic-paper-talk":
        return [14, 18]
    return [6, 12]


def _fallback_slide_count(subtype: str, signals: dict[str, Any], claim_graph: Any | None) -> int:
    if subtype == "pitch":
        return 10
    if subtype == "report":
        return 18 if int(signals.get("n_sections") or 0) >= 8 else 15
    figures = int(signals.get("n_registered_figures") or 0)
    tables = int(signals.get("n_registered_tables") or 0)
    sections = int(signals.get("n_sections") or 0)
    motion = int(signals.get("motion_or_sequence_visuals") or 0)
    graph_nodes = 0
    if claim_graph is not None:
        for attr in ("tensions", "mechanisms", "evidence", "implications"):
            graph_nodes += len(getattr(claim_graph, attr, []) or [])
    if motion >= 8 or figures >= 50 or graph_nodes >= 14:
        return 18
    if figures >= 18 or sections >= 7 or tables >= 2 or graph_nodes >= 9:
        return 16
    if figures >= 8 or sections >= 5:
        return 14
    return 12


def _density_budget(subtype: str, count_hint: int) -> dict[str, Any]:
    if subtype == "academic-paper-talk":
        return {
            "max_bullets_per_slide": 4,
            "max_visuals_per_slide": 2,
            "target_visual_slides_min": max(
                4,
                min(max(0, count_hint - 2), round(count_hint * 0.7)),
            ),
            "target_words_per_substantive_slide": [45, 110],
            "role_word_ranges": {
                "cover": [0, 35],
                "outline": [30, 65],
                "problem_and_context": [45, 100],
                "method_and_algorithm": [55, 140],
                "results_and_analysis": [45, 110],
                "closing": [20, 60],
            },
            "max_words_per_slide": 140,
        }
    if subtype == "pitch":
        return {"max_bullets_per_slide": 4, "max_visuals_per_slide": 1, "max_words_per_slide": 45}
    return {"max_bullets_per_slide": 5, "max_visuals_per_slide": 2, "max_words_per_slide": 65}


def _default_outline(count: int, subtype: str) -> list[dict[str, Any]]:
    roles = _outline_roles(count, subtype)
    out: list[dict[str, Any]] = []
    for idx in range(1, count + 1):
        role = roles[idx - 1] if idx - 1 < len(roles) else "content"
        out.append({
            "slide_index": idx,
            "title": _title_for_role(role, idx),
            "role": role,
            "chapter": _chapter_for_role(role),
            "communication_job": _communication_job_for_role(role),
            "assertion_title": _assertion_title_for_role(role, idx),
            "scope": _scope_for_role(role),
            "layout_family": _layout_family_for_role(role),
            "content": "Planner fills this slide from the source document and deck_plan rationale.",
            "visual_refs": [],
            "evidence_refs": [],
            "speaker_note": "Explain the source-backed point succinctly.",
            "speaker_note_intent": _speaker_note_intent_for_role(role),
        })
    return out


def _outline_roles(count: int, subtype: str) -> list[str]:
    if subtype == "pitch":
        base = ["cover", "problem", "solution", "market", "product", "traction", "team", "ask", "closing"]
    elif subtype == "academic-paper-talk":
        compact = [
            "cover", "problem-scope", "motivation", "method-overview", "mechanism",
            "experiment-setup", "primary-results", "secondary-results",
            "ablation-analysis", "limitations", "implications", "closing",
        ]
        standard = [
            "cover", "outline", "problem-scope", "motivation", "prior-work",
            "contributions", "method-overview", "mechanism", "algorithm",
            "experiment-setup", "primary-results", "secondary-results",
            "ablation-analysis", "qualitative-analysis", "limitations",
            "implications", "takeaways", "closing",
        ]
        full_formal = [
            "cover", "outline", "problem-scope", "motivation", "prior-work",
            "contributions", "outline-checkpoint", "method-overview", "mechanism",
            "architecture-detail", "algorithm", "implementation-detail",
            "outline-checkpoint", "experiment-setup", "datasets-metrics",
            "primary-results", "secondary-results", "ablation-analysis",
            "qualitative-analysis", "efficiency-analysis", "limitations",
            "implications", "takeaways", "closing",
        ]
        base = compact if count <= 12 else standard if count <= 18 else full_formal
    else:
        base = ["cover", "context", "main-point", "evidence", "process", "results", "takeaways", "closing"]
    if count <= len(base):
        return base[:count - 1] + ["closing"]
    middle = base[1:-1]
    while len(base) < count:
        insert = middle[(len(base) - 2) % max(1, len(middle))] if middle else "content"
        base.insert(-1, insert)
    return base[:count]


def _talk_profile(subtype: str, slide_count: int | None) -> str:
    if subtype != "academic-paper-talk":
        return "standard_conference"
    count = int(slide_count or 0)
    if count >= 20:
        return "full_formal"
    if count and count <= 10:
        return "short_overview"
    return "standard_conference"


def _chapter_for_role(role: str) -> str:
    if role in {"cover", "outline", "problem-scope", "motivation", "prior-work", "contributions"}:
        return "Motivation"
    if role in {
        "outline-checkpoint", "method-overview", "mechanism",
        "architecture-detail", "algorithm", "implementation-detail",
    }:
        return "Method"
    if role in {
        "experiment-setup", "datasets-metrics", "primary-results",
        "secondary-results", "ablation-analysis", "qualitative-analysis",
        "efficiency-analysis",
    }:
        return "Evaluation"
    return "Synthesis"


def _communication_job_for_role(role: str) -> str:
    jobs = {
        "cover": "Identify the paper and state the talk thesis.",
        "outline": "Preview the narrative chapters without presenting evidence early.",
        "outline-checkpoint": "Orient the audience at a chapter transition.",
        "problem-scope": "Define the precise research question and scope.",
        "motivation": "Establish why the problem matters.",
        "prior-work": "Locate the gap in prior approaches.",
        "contributions": "Separate the paper's supported contributions.",
        "method-overview": "Give one complete system-level method claim.",
        "mechanism": "Explain the causal or computational mechanism.",
        "architecture-detail": "Resolve the architecture at implementation-relevant granularity.",
        "algorithm": "Walk through the algorithm or objective in execution order.",
        "implementation-detail": "State training or implementation details needed to interpret results.",
        "experiment-setup": "Define datasets, baselines, metrics, and evaluation protocol.",
        "datasets-metrics": "Make comparison conditions and metrics explicit.",
        "primary-results": "Establish the strongest supported empirical result.",
        "secondary-results": "Add complementary evidence without repeating the primary result.",
        "ablation-analysis": "Attribute gains using source-backed analysis.",
        "qualitative-analysis": "Show behavior that aggregate metrics do not expose.",
        "efficiency-analysis": "Explain runtime, cost, or scaling evidence.",
        "limitations": "State boundary conditions and failure modes.",
        "implications": "Connect findings to broader consequences without overclaiming.",
        "takeaways": "Compress the talk into distinct, non-redundant conclusions.",
        "closing": "End with the lasting lesson and discussion prompt.",
    }
    return jobs.get(role, "Establish one source-backed point.")


def _assertion_title_for_role(role: str, idx: int) -> str:
    if role in {"cover", "outline", "outline-checkpoint", "closing"}:
        return _title_for_role(role, idx)
    return f"Use source evidence to establish {_title_for_role(role, idx).lower()}."


def _scope_for_role(role: str) -> str:
    if role in {"cover", "outline", "outline-checkpoint", "closing"}:
        return "talk_structure"
    if role in {"method-overview", "mechanism", "architecture-detail", "algorithm", "implementation-detail"}:
        return "method"
    if role in {
        "experiment-setup", "datasets-metrics", "primary-results",
        "secondary-results", "ablation-analysis", "qualitative-analysis",
        "efficiency-analysis",
    }:
        return "evidence"
    return "paper_narrative"


def _layout_family_for_role(role: str) -> str:
    if role == "cover":
        return "identity_cover"
    if role in {"outline", "outline-checkpoint"}:
        return "chapter_outline"
    if role in {"method-overview", "mechanism", "architecture-detail", "algorithm"}:
        return "method_evidence_split"
    if role in {
        "primary-results", "secondary-results", "ablation-analysis",
        "qualitative-analysis", "efficiency-analysis",
    }:
        return "results_evidence"
    if role in {"experiment-setup", "datasets-metrics"}:
        return "protocol_table"
    if role in {"takeaways", "closing"}:
        return "synthesis"
    return "editorial_evidence"


def _speaker_note_intent_for_role(role: str) -> str:
    return (
        f"[Sources] Cite the exact paper evidence for {_title_for_role(role, 0).lower()}. "
        f"[Talk] {_communication_job_for_role(role)}"
    )


def _title_for_role(role: str, idx: int) -> str:
    labels = {
        "cover": "Title and setup",
        "outline": "Talk outline",
        "outline-checkpoint": "Talk checkpoint",
        "closing": "Takeaways and Q&A",
        "problem-scope": "Research problem and scope",
        "motivation": "Why the problem matters",
        "prior-work": "Prior work and unresolved gap",
        "contributions": "Contributions",
        "method-overview": "Method overview",
        "mechanism": "Mechanism",
        "architecture-detail": "Architecture detail",
        "algorithm": "Algorithm and objective",
        "implementation-detail": "Implementation details",
        "experiment-setup": "Experiment setup",
        "datasets-metrics": "Datasets, baselines, and metrics",
        "primary-results": "Primary results",
        "secondary-results": "Secondary results",
        "ablation-analysis": "Ablation and analysis",
        "qualitative-analysis": "Qualitative analysis",
        "efficiency-analysis": "Efficiency and scaling",
        "limitations": "Limits",
        "implications": "Implications",
        "takeaways": "Takeaways",
    }
    return labels.get(role, f"Slide {idx}: {role.replace('-', ' ').title()}")


def _count_legacy_slide_nodes(nodes: list[Any]) -> int:
    total = 0
    for node in nodes:
        kind = getattr(node, "kind", None)
        if kind == "slide":
            total += 1
        total += _count_legacy_slide_nodes(list(getattr(node, "children", []) or []))
    return total


def _allowed_visual_ref(ref: str, known: set[str]) -> bool:
    raw = str(ref or "").strip()
    if not raw:
        return True
    lower = raw.lower()
    if raw in known:
        return True
    if lower in {"none", "text-only", "text_only"}:
        return True
    return lower.startswith(("generated:", "generate_image:", "nbp:", "new:", "katex:", "chart:"))


def _slide_count_range_spans(text: str) -> list[tuple[int, int, int, int]]:
    lowered = (text or "").lower()
    patterns = (
        r"(\d{1,2})\s*(?:-|–|—|~|至|到|to)\s*(\d{1,2})\s*(?:slides?\b|幻灯片|页|张|page(?:s)?\b)",
        r"(?:slides?\b|幻灯片|页数|页|张|pages?\b|deck\b)\s*(?:count|数量|数)?\s*[:=：]?\s*(\d{1,2})\s*(?:-|–|—|~|至|到|to)\s*(\d{1,2})(?!\d)",
    )
    spans: list[tuple[int, int, int, int]] = []
    for pattern in patterns:
        for match in re.finditer(pattern, lowered, flags=re.IGNORECASE):
            try:
                first = int(match.group(1))
                second = int(match.group(2))
            except (TypeError, ValueError):
                continue
            lo, hi = sorted((first, second))
            if 1 <= lo <= hi <= 60:
                spans.append((match.start(), match.end(), lo, hi))
    return spans


def _match_inside_any_span(match: re.Match[str], spans: list[tuple[int, int, int, int]]) -> bool:
    start, end = match.span(1)
    return any(span_start <= start and end <= span_end for span_start, span_end, _lo, _hi in spans)


def _match_has_non_exact_count_cue(match: re.Match[str], lowered_text: str) -> bool:
    start, _end = match.span(1)
    before = lowered_text[max(0, start - 100):start]
    segment = before
    for sep in (".", ";", "\n", "!", "?", "。", "；", "！", "？"):
        idx = segment.rfind(sep)
        if idx >= 0:
            segment = segment[idx + 1:]
    negated = (
        "do not", "don't", "dont", "should not", "shouldn't", "never",
        "avoid", "without", "not ", "not a fixed", "not fixed",
        "no fixed", "不要", "别", "不用", "不必", "避免", "不要默认", "不要固定",
        "别默认", "别固定", "不能",
    )
    approximate = (
        "about", "around", "roughly", "approximately", "approx.", "up to",
        "at least", "at most", "no more than", "less than", "more than",
        "大约", "左右", "最多", "至少", "不超过", "少于", "多于",
    )
    return any(cue in segment for cue in negated + approximate)


def _record_ratio(rec: dict[str, Any]) -> float:
    raw = str(rec.get("image_size") or "")
    match = re.match(r"\s*(\d+)\s*x\s*(\d+)\s*", raw)
    if match:
        w, h = int(match.group(1)), int(match.group(2))
        if h:
            return w / h
    return 1.0


def _has_any(text: str, tokens: tuple[str, ...]) -> bool:
    return any(tok in text for tok in tokens)


def _compact_kv(data: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in data.items())
