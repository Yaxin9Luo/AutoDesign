"""critique — thin wrapper that spawns a forked CriticAgent sub-agent.

v2.7.3 (2026-04-26): the inline `Critic` class is gone. The planner-facing
tool signature is unchanged — it still returns one CritiqueReport JSON in
`tool_result.payload` — but the work happens in
`autodesign/agents/critic_agent.py` which owns its own LLMBackend and turn
budget.

Why split: the inline path shared the planner's max_designer_turns budget,
saw text-only structures for deck/landing (no vision), and capped at 2
calls. The forked sub-agent gets `critic_max_turns` to its own loop, sees
slide PNGs for ALL artifact types, and emits a structured CritiqueReport
that the planner can parse to drive the next action (pass → finalize,
revise → propose_design_spec, fail → terminal).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ._contract import ToolContext, obs_error, obs_ok
from ..schema import ArtifactType, ToolResultRecord
from ..util.design_feedback import design_feedback_to_dict
from ..util.io import atomic_write_json
from ..util.logging import log


def critique(args: dict[str, Any], *, ctx: ToolContext) -> ToolResultRecord:
    spec = ctx.state.get("design_spec")
    if spec is None:
        return obs_error("propose_design_spec must be called first",
                         category="validation")

    prior = len(ctx.state["critique_results"])
    if prior >= ctx.settings.max_critique_iters:
        return obs_error(
            f"max_critique_iters ({ctx.settings.max_critique_iters}) reached",
            category="validation",
            payload={"max_iters": ctx.settings.max_critique_iters,
                     "prior": prior},
        )

    composition = ctx.state.get("composition")
    if composition is None:
        return obs_error(
            "no composition available — call composite first",
            category="not_found",
        )
    requested_preview_path = args.get("preview_path")
    preview_path = _resolve_preview_path(requested_preview_path, composition, ctx)
    if requested_preview_path and preview_path != str(requested_preview_path):
        log(
            "critique.preview_path.fallback",
            requested=str(requested_preview_path),
            fallback=preview_path,
        )
    if not preview_path or not Path(preview_path).exists():
        return obs_error(
            "no preview.png available — call composite first",
            category="not_found",
        )

    slide_renders = _collect_slide_renders(spec, composition, Path(preview_path))
    paper_raw_text = _join_paper_raw_text(ctx)
    claim_graph = ctx.state.get("claim_graph")
    design_feedback = _latest_design_feedback(ctx)
    skill_context = (ctx.state.get("skill_contexts") or {}).get("critique")

    iteration = prior + 1
    from ..agents import CriticAgent
    agent = CriticAgent(ctx.settings, _resolve_artifact_type(spec))
    try:
        report = agent.critique(
            spec=spec,
            layer_manifest=composition.layer_manifest or [],
            slide_renders=slide_renders,
            paper_raw_text=paper_raw_text,
            claim_graph=claim_graph,
            design_feedback=design_feedback,
            skill_context=skill_context,
            iteration=iteration,
        )
    except Exception as e:
        return obs_error(f"critic sub-agent failed: {e}", category="api")

    ctx.state["critique_results"].append(report)
    composite_payload = ctx.state.get("last_composite_payload") or {}
    if isinstance(composite_payload, dict):
        ctx.state["last_critique_preview_sha256"] = composite_payload.get("preview_sha256")
        ctx.state["last_critique_composite_iteration"] = composite_payload.get("iteration")
        ctx.state["last_critique_spec_revision"] = composite_payload.get("spec_revision")

    artifact_path = ctx.run_dir / f"critique_{iteration}.json"
    atomic_write_json(artifact_path, report.model_dump(mode="json"))
    log("critique.done", iter=iteration, verdict=report.verdict,
        score=report.score, n_issues=len(report.issues),
        has_design_feedback=design_feedback is not None)

    return obs_ok(report.model_dump(mode="json"))


def _resolve_preview_path(requested: Any, composition: Any, ctx: ToolContext) -> str:
    candidates: list[Any] = []
    if requested:
        candidates.append(requested)
        requested_path = Path(str(requested))
        if not requested_path.is_absolute():
            candidates.append(ctx.run_dir / requested_path)
            comp_preview = getattr(composition, "preview_path", None)
            if comp_preview:
                candidates.append(Path(str(comp_preview)).parent / requested_path)
    comp_preview = getattr(composition, "preview_path", None)
    if comp_preview:
        candidates.append(comp_preview)

    for candidate in candidates:
        if not candidate:
            continue
        path = Path(str(candidate))
        if path.exists():
            return str(path)
    return str(comp_preview or requested or "")


def _latest_design_feedback(ctx: ToolContext) -> dict[str, Any] | None:
    feedback = (
        ctx.state.get("last_design_feedback")
        or (ctx.state.get("last_composite_payload") or {}).get("design_feedback")
    )
    payload = design_feedback_to_dict(feedback)
    contract = ctx.state.get("poster_plan_contract")
    if isinstance(contract, dict) and contract:
        if payload is None:
            payload = _empty_feedback_payload("poster")
        payload["poster_plan_contract"] = contract
        _prepend_feedback_context(
            payload,
            key="poster_plan_contract",
            value=contract,
            message=(
                "Context only: poster_plan_contract is available; use "
                "target.poster_plan_contract as the paper-poster done-definition."
            ),
        )
    scorecard = _latest_scorecard(ctx)
    if scorecard:
        if payload is None:
            payload = _empty_feedback_payload("unknown")
        payload["latest_critic_scorecard"] = scorecard
        _prepend_feedback_context(
            payload,
            key="latest_critic_scorecard",
            value=scorecard,
            message=(
                "Context only: latest_critic_scorecard is available for "
                "trend-aware poster scoring."
            ),
        )
    return payload


def _empty_feedback_payload(artifact_type: str) -> dict[str, Any]:
    return {
        "artifact_type": artifact_type,
        "iteration": 0,
        "findings": [],
        "counts": {},
        "has_blocking_findings": False,
    }


def _prepend_feedback_context(
    payload: dict[str, Any],
    *,
    key: str,
    value: Any,
    message: str,
) -> None:
    """Keep non-finding context visible after CriticAgent compacts feedback."""
    findings = payload.get("findings")
    if not isinstance(findings, list):
        findings = []
    finding_id = f"context:{key}"
    payload["findings"] = [
        {
            "id": finding_id,
            "source": "context",
            "severity": "low",
            "artifact_type": str(payload.get("artifact_type") or "unknown"),
            "message": message,
            "target": {key: value},
            "evidence": {"context_key": key},
            "suggested_action": (
                "Use this context while critiquing; do not report it as an issue."
            ),
            "repairable": False,
        },
        *[
            finding for finding in findings
            if not (isinstance(finding, dict) and finding.get("id") == finding_id)
        ],
    ]


def _latest_scorecard(ctx: ToolContext) -> dict[str, float]:
    crits = ctx.state.get("critique_results") or []
    if not crits:
        return {}
    last = crits[-1]
    scores = (
        last.get("dimension_scores")
        if isinstance(last, dict)
        else getattr(last, "dimension_scores", None)
    )
    if not isinstance(scores, dict):
        return {}
    out: dict[str, float] = {}
    for name, score in scores.items():
        try:
            out[str(name)] = float(score)
        except (TypeError, ValueError):
            continue
    return out


def _resolve_artifact_type(spec: Any) -> ArtifactType:
    """Return the spec's artifact_type, accepting either an enum instance
    or a string (some test fixtures construct DesignSpec from raw dicts)."""
    at = getattr(spec, "artifact_type", None) or ArtifactType.POSTER
    if isinstance(at, ArtifactType):
        return at
    try:
        return ArtifactType(str(at))
    except ValueError:
        return ArtifactType.POSTER


def _collect_slide_renders(spec: Any, composition: Any,
                           preview_path: Path) -> list[Path]:
    """For deck artifacts, gather the per-slide PNGs that composite wrote
    under `composites/iter_NN/slides/`. Landing uses its desktop full-page
    preview."""
    artifact_type = _resolve_artifact_type(spec)
    if artifact_type == ArtifactType.LANDING:
        return [preview_path]
    if artifact_type != ArtifactType.DECK:
        return [preview_path]
    slides_dir = preview_path.parent / "slides"
    if not slides_dir.exists():
        return [preview_path]
    pngs = sorted(slides_dir.glob("slide_*.png"))
    return list(pngs) if pngs else [preview_path]


def _join_paper_raw_text(ctx: ToolContext) -> str | None:
    """Concatenate raw_text across all ingested documents so the critic
    can substring-search it in one place. Returns None when no ingest
    happened (free-text brief)."""
    ingested = ctx.state.get("ingested") or []
    chunks: list[str] = []
    for entry in ingested:
        text = entry.get("raw_text") if isinstance(entry, dict) else None
        if text:
            chunks.append(text)
    if not chunks:
        return None
    return "\n\n".join(chunks)
