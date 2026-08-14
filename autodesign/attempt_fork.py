"""Supervised materialization of an immutable attempt into an editable draft."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
from typing import Any

from .attempt_candidates import load_attempt_candidate
from .attempt_selection import materialize_candidate_for_editing
from .schema import AttemptCandidate
from .tools._contract import ToolContext
from .util.io import atomic_write_json


_POSTER_CONTEXT_FILES = (
    "paper_visual_provenance.json",
    "paper_memory.json",
    "paper_memory_dossier.json",
    "paper_visual_storyboard.json",
    "poster_content_brief.json",
    "poster_plan_contract.json",
    "poster_contract_preflight.json",
    "canvas_plan.json",
    "paper_memory.md",
    "paper_memory_dossier.md",
)

_TRUSTED_SOURCE_ANCHORS = {
    "deck": "slides_trusted_source_hashes.json",
    "landing": "landing_trusted_source_hashes.json",
    "video": "video_trusted_source_context.json",
}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _poster_palette_id(run_dir: Path) -> str:
    palette_id = str(_read_json(run_dir / "run_brief.json").get("palette_id") or "").strip()
    if palette_id:
        return palette_id
    for name in (
        "code_editor_revision_manifest.json",
        "authored_poster_edit_manifest.json",
        "apply_edits_palette_manifest.json",
    ):
        palette_id = str(_read_json(run_dir / "final" / name).get("palette_id") or "").strip()
        if palette_id:
            return palette_id
    return str(
        _read_json(run_dir / "candidate_draft_lineage.json").get("poster_palette_id")
        or ""
    ).strip()


def _copy_poster_context(source: Path, target: Path, cancellation: Any) -> None:
    for name in _POSTER_CONTEXT_FILES:
        cancellation.raise_if_cancelled("attempt_fork.copy_poster_context")
        source_path = source / name
        if source_path.is_file():
            shutil.copy2(source_path, target / name)
    for name in ("layers", "paper_evidence_packs"):
        cancellation.raise_if_cancelled("attempt_fork.copy_poster_context")
        source_path = source / name
        if source_path.is_dir():
            shutil.copytree(source_path, target / name, dirs_exist_ok=True)


def materialize_attempt_candidate_draft(
    *,
    run_id: str,
    parent_run_id: str,
    conversation_id: str,
    source_run_dir: Path,
    run_dir: Path,
    candidate: AttemptCandidate,
    settings: Any,
    cancellation_token: Any,
) -> dict[str, Any]:
    """Materialize a verified candidate into a caller-owned draft directory."""

    for name in ("canvas_plan.json", "deck_plan.json"):
        cancellation_token.raise_if_cancelled("attempt_fork.copy_plan")
        source_path = source_run_dir / name
        if source_path.is_file():
            shutil.copy2(source_path, run_dir / name)
    if candidate.artifact_type.value == "poster":
        _copy_poster_context(source_run_dir, run_dir, cancellation_token)
    anchor_name = _TRUSTED_SOURCE_ANCHORS.get(candidate.artifact_type.value)
    if anchor_name:
        cancellation_token.raise_if_cancelled("attempt_fork.copy_trusted_source_anchor")
        anchor_path = source_run_dir / anchor_name
        if anchor_path.is_file():
            shutil.copy2(anchor_path, run_dir / anchor_name)

    ctx = ToolContext(
        settings=settings,
        run_dir=run_dir,
        layers_dir=run_dir / "layers",
        run_id=run_id,
        cancellation_token=cancellation_token,
    )
    ctx.state["canvas_plan"] = _read_json(run_dir / "canvas_plan.json") or None
    ctx.state["deck_plan"] = _read_json(run_dir / "deck_plan.json") or None
    ctx.state["slides_author_attempts"] = candidate.attempt
    ctx.state["landing_author_attempts"] = candidate.attempt
    cancellation_token.raise_if_cancelled("attempt_fork.before_materialize")
    source = materialize_candidate_for_editing(
        ctx,
        source_run_dir=source_run_dir,
        candidate=candidate,
    )
    cancellation_token.raise_if_cancelled("attempt_fork.after_materialize")

    lineage: dict[str, Any] = {
        "schema_version": 1,
        "materialization_version": 2,
        "status": "draft",
        "artifact_type": candidate.artifact_type.value,
        "source_run_id": parent_run_id,
        "source_attempt": candidate.attempt,
        "source_candidate_id": candidate.candidate_id,
        "source_candidate_sha256": candidate.source_sha256,
        "published_artifact_id_at_fork": (
            f"art_{parent_run_id}" if (source_run_dir / "final").is_dir() else None
        ),
        "conversation_id": conversation_id,
    }
    if candidate.artifact_type.value == "poster":
        palette_id = _poster_palette_id(source_run_dir)
        if palette_id:
            lineage["poster_palette_id"] = palette_id
    cancellation_token.raise_if_cancelled("attempt_fork.before_lineage")
    atomic_write_json(run_dir / "candidate_draft_lineage.json", lineage)
    cancellation_token.raise_if_cancelled("attempt_fork.after_lineage")
    return {
        "run_id": run_id,
        "artifact_type": candidate.artifact_type.value,
        "source_path": str(source),
        "lineage": lineage,
    }


def run_attempt_fork_job(
    *,
    run_id: str,
    parent_run_id: str,
    attempt: int,
    expected_candidate_sha256: str,
    conversation_id: str,
    settings: Any,
    cancellation_token: Any,
    runs_dir: Path | None = None,
) -> dict[str, Any]:
    runs_dir = (
        Path(runs_dir) if runs_dir is not None else Path(settings.out_dir) / "runs"
    ).resolve()
    source_run_dir = (runs_dir / parent_run_id).resolve()
    run_dir = (runs_dir / run_id).resolve()
    if source_run_dir.parent != runs_dir or run_dir.parent != runs_dir:
        raise ValueError("attempt fork run path escaped the configured runs directory")
    if not source_run_dir.is_dir() or not run_dir.is_dir():
        raise ValueError("attempt fork source or reserved run directory is missing")

    cancellation_token.raise_if_cancelled("attempt_fork.load_candidate")
    candidate = load_attempt_candidate(source_run_dir, attempt)
    if candidate.source_sha256 != expected_candidate_sha256:
        raise ValueError("attempt candidate changed before materialization")

    return materialize_attempt_candidate_draft(
        run_id=run_id,
        parent_run_id=parent_run_id,
        conversation_id=conversation_id,
        source_run_dir=source_run_dir,
        run_dir=run_dir,
        candidate=candidate,
        settings=settings,
        cancellation_token=cancellation_token,
    )
