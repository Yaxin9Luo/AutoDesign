"""Selection and promotion state for immutable external-author attempts."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import threading
from typing import Callable, Iterator, Literal

from .agents.external_author_process import terminate_registered_author_process
from .attempt_candidates import (
    attempt_promotion_lease,
    clear_selection_adapter_transaction,
    load_attempt_candidate,
    load_attempt_candidates,
    load_selection_adapter_transaction,
    load_selection_journal,
    write_selection_adapter_transaction,
    write_selection_journal,
)
from .schema import AttemptCandidate, AttemptSelectionJournal
from .run_control import RunCancelled
from .tools._contract import ToolContext
from .util.logging import log


SelectionRequestStatus = Literal[
    "selection_accepted",
    "already_selected",
    "candidate_blocked",
    "candidate_changed",
    "run_not_selectable",
]
PromotionOutcome = Literal["none", "in_progress", "complete", "failed"]
ForkCompletionStatus = Literal[
    "completed",
    "already_final",
    "source_unavailable",
    "selection_conflict",
    "candidate_changed",
]


@dataclass(frozen=True)
class SelectionRequestResult:
    status: SelectionRequestStatus
    candidate_id: str | None = None


class AttemptPromotionRejected(RuntimeError):
    """Raised when normal promotion races a selected candidate."""


_SELECTION_LOCKS: dict[str, threading.RLock] = {}
_SELECTION_LOCKS_GUARD = threading.Lock()
_PROMOTION_REQUESTED_RUN_DIR_STATE_KEY = "attempt_selection_requested_run_dir"
_PENDING_STATES = {"requested", "terminating", "promoting", "delivering"}
_TRANSITIONS = {
    "requested": {"terminating", "promoting", "failed"},
    "terminating": {"promoting", "failed"},
    "promoting": {"delivering", "complete", "failed"},
    "delivering": {"complete", "failed"},
    "complete": set(),
    "failed": set(),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _selection_lock(run_dir: Path) -> threading.RLock:
    key = str(run_dir.absolute())
    with _SELECTION_LOCKS_GUARD:
        return _SELECTION_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _promotion_lease(
    run_dir: Path,
    *,
    expected_run_identity: tuple[int, int] | None = None,
) -> Iterator[Path]:
    with _selection_lock(run_dir):
        with attempt_promotion_lease(
            run_dir,
            expected_run_identity=expected_run_identity,
        ) as leased_run_dir:
            yield leased_run_dir


@contextmanager
def normal_promotion_lease(
    *,
    run_dir: Path,
    candidate_id: str,
    expected_run_identity: tuple[int, int] | None = None,
) -> Iterator[Path]:
    """Linearize ordinary final publication against a user selection."""

    with _promotion_lease(
        run_dir,
        expected_run_identity=expected_run_identity,
    ) as leased_run_dir:
        journal = load_selection_journal(run_dir)
        if journal is not None:
            raise AttemptPromotionRejected(
                "normal promotion cannot begin after an attempt selection"
            )
        yield leased_run_dir


@contextmanager
def leased_promotion_tool_context(
    ctx: ToolContext,
    leased_run_dir: Path,
) -> Iterator[None]:
    original_run_dir = Path(ctx.run_dir)
    original_layers_dir = Path(ctx.layers_dir)
    marker_was_present = _PROMOTION_REQUESTED_RUN_DIR_STATE_KEY in ctx.state
    previous_marker = ctx.state.get(_PROMOTION_REQUESTED_RUN_DIR_STATE_KEY)
    try:
        layers_relative = original_layers_dir.relative_to(original_run_dir)
    except ValueError:
        leased_layers_dir = original_layers_dir
    else:
        leased_layers_dir = leased_run_dir / layers_relative
    ctx.run_dir = leased_run_dir
    ctx.layers_dir = leased_layers_dir
    ctx.state[_PROMOTION_REQUESTED_RUN_DIR_STATE_KEY] = os.fspath(original_run_dir)
    try:
        yield
    finally:
        ctx.run_dir = original_run_dir
        ctx.layers_dir = original_layers_dir
        if marker_was_present:
            ctx.state[_PROMOTION_REQUESTED_RUN_DIR_STATE_KEY] = previous_marker
        else:
            ctx.state.pop(_PROMOTION_REQUESTED_RUN_DIR_STATE_KEY, None)


def promotion_requested_run_dir(ctx: ToolContext) -> Path | None:
    value = ctx.state.get(_PROMOTION_REQUESTED_RUN_DIR_STATE_KEY)
    return Path(value) if isinstance(value, str) and value else None


def _has_published_final(run_dir: Path) -> bool:
    final_dir = run_dir / "final"
    return final_dir.is_dir() and any(path.is_file() for path in final_dir.rglob("*"))


def selection_is_pending(run_dir: Path) -> bool:
    journal = load_selection_journal(run_dir)
    return journal is not None and journal.state in _PENDING_STATES


def selected_candidate_for_run(run_dir: Path) -> AttemptCandidate | None:
    journal = load_selection_journal(run_dir)
    if journal is None:
        return None
    candidate = load_attempt_candidate(run_dir, journal.source_attempt)
    if (
        candidate.candidate_id != journal.candidate_id
        or candidate.source_sha256 != journal.candidate_sha256
    ):
        raise ValueError("selected attempt candidate no longer matches its journal")
    return candidate


def ranked_delivery_candidates(
    run_dir: Path,
    *,
    artifact_type: str,
) -> list[AttemptCandidate]:
    """Rank immutable nonblocked candidates without relying on author scores."""

    try:
        candidates = load_attempt_candidates(run_dir)
    except ValueError:
        return []
    eligible = [
        candidate
        for candidate in candidates
        if candidate.artifact_type.value == artifact_type
        and candidate.safety_state != "blocked"
    ]
    if not eligible:
        return []

    def rank(candidate: AttemptCandidate) -> tuple[int, int, int, int, int]:
        summary_path = run_dir / candidate.validation_summary_relative_path
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            summary = {}
        if not isinstance(summary, dict):
            summary = {}
        raw_metrics = summary.get("metrics")
        metrics = raw_metrics if isinstance(raw_metrics, dict) else {}
        browser_backend = str(
            summary.get("browser_audit_backend")
            or metrics.get("browser_audit_backend")
        )
        return (
            int(candidate.safety_state == "ready"),
            int(bool(summary.get("kind") or metrics)),
            -len(candidate.warnings),
            int(
                bool(candidate.preview_relative_paths)
                and bool(browser_backend)
                and browser_backend != "unavailable"
            ),
            candidate.attempt,
        )

    return sorted(eligible, key=rank, reverse=True)


def best_delivery_candidate(
    run_dir: Path,
    *,
    artifact_type: str,
) -> AttemptCandidate | None:
    """Choose the strongest immutable nonblocked candidate without author scores."""

    ranked = ranked_delivery_candidates(run_dir, artifact_type=artifact_type)
    return ranked[0] if ranked else None


def transition_selection(
    run_dir: Path,
    state: Literal[
        "requested",
        "terminating",
        "promoting",
        "delivering",
        "complete",
        "failed",
    ],
    *,
    artifact_id: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> AttemptSelectionJournal:
    with _selection_lock(run_dir):
        journal = load_selection_journal(run_dir)
        if journal is None:
            raise ValueError("attempt selection journal is missing")
        if state != journal.state and state not in _TRANSITIONS[journal.state]:
            raise ValueError(
                f"invalid attempt selection transition: {journal.state} -> {state}"
            )
        updated = journal.model_copy(
            update={
                "state": state,
                "artifact_id": artifact_id,
                "error_code": error_code,
                "error_message": error_message,
                "updated_at": _now_iso(),
            }
        )
        write_selection_journal(run_dir, updated)
        return updated


def request_attempt_selection(
    *,
    run_dir: Path,
    run_id: str,
    attempt: int,
    expected_candidate_sha256: str,
    idempotency_key: str,
    writable_guard: Callable[[], object] | None = None,
) -> SelectionRequestResult:
    run_dir = run_dir.absolute()
    if run_dir.name != run_id:
        return SelectionRequestResult(status="run_not_selectable")
    with _promotion_lease(run_dir):
        if writable_guard is not None:
            writable_guard()
        existing = load_selection_journal(run_dir)
        if existing is not None:
            if (
                existing.source_attempt == attempt
                and existing.candidate_sha256 == expected_candidate_sha256
                and existing.idempotency_key == idempotency_key
            ):
                return SelectionRequestResult(
                    status="already_selected",
                    candidate_id=existing.candidate_id,
                )
            if (
                existing.state == "failed"
                and existing.source_attempt == attempt
                and existing.candidate_sha256 == expected_candidate_sha256
            ):
                if writable_guard is not None:
                    writable_guard()
                clear_selection_adapter_transaction(run_dir)
                retried = existing.model_copy(
                    update={
                        "idempotency_key": idempotency_key,
                        "state": "requested",
                        "artifact_id": None,
                        "error_code": None,
                        "error_message": None,
                        "updated_at": _now_iso(),
                    }
                )
                if writable_guard is not None:
                    writable_guard()
                write_selection_journal(run_dir, retried)
                stopped = terminate_registered_author_process(
                    run_id,
                    reason=f"retry_selected_attempt:{existing.candidate_id}",
                )
                if stopped:
                    if writable_guard is not None:
                        writable_guard()
                    transition_selection(run_dir, "terminating")
                log(
                    "attempt_selection_retried",
                    run_id=run_id,
                    candidate_id=existing.candidate_id,
                    attempt=attempt,
                    process_termination_requested=stopped,
                )
                return SelectionRequestResult(
                    status="selection_accepted",
                    candidate_id=existing.candidate_id,
                )
            return SelectionRequestResult(status="run_not_selectable")
        if _has_published_final(run_dir):
            return SelectionRequestResult(status="run_not_selectable")
        try:
            candidate = load_attempt_candidate(run_dir, attempt)
        except ValueError:
            return SelectionRequestResult(status="candidate_changed")
        if candidate.source_sha256 != expected_candidate_sha256:
            return SelectionRequestResult(status="candidate_changed")
        if candidate.safety_state == "blocked":
            return SelectionRequestResult(
                status="candidate_blocked",
                candidate_id=candidate.candidate_id,
            )
        journal = AttemptSelectionJournal(
            run_id=run_id,
            candidate_id=candidate.candidate_id,
            candidate_sha256=candidate.source_sha256,
            source_attempt=candidate.attempt,
            idempotency_key=idempotency_key,
            state="requested",
            updated_at=_now_iso(),
        )
        if writable_guard is not None:
            writable_guard()
        write_selection_journal(run_dir, journal)
        stopped = terminate_registered_author_process(
            run_id,
            reason=f"selected_attempt:{candidate.candidate_id}",
        )
        if stopped:
            if writable_guard is not None:
                writable_guard()
            transition_selection(run_dir, "terminating")
        log(
            "attempt_selection_requested",
            run_id=run_id,
            candidate_id=candidate.candidate_id,
            attempt=candidate.attempt,
            process_termination_requested=stopped,
        )
        return SelectionRequestResult(
            status="selection_accepted",
            candidate_id=candidate.candidate_id,
        )


def complete_source_run_with_candidate_fork(
    *,
    run_dir: Path,
    run_id: str,
    attempt: int,
    expected_candidate_sha256: str,
    artifact_id: str,
) -> ForkCompletionStatus:
    """Stop source authoring after a validated attempt fork is published."""
    run_dir = run_dir.absolute()
    if run_dir.name != run_id:
        return "candidate_changed"
    if not run_dir.is_dir():
        return "source_unavailable"
    with _promotion_lease(run_dir):
        existing = load_selection_journal(run_dir)
        if existing is not None:
            if (
                existing.state == "complete"
                and existing.artifact_id == artifact_id
            ):
                return "completed"
            return "selection_conflict"
        if _has_published_final(run_dir):
            return "already_final"
        try:
            candidate = load_attempt_candidate(run_dir, attempt)
        except ValueError:
            return "candidate_changed"
        if candidate.source_sha256 != expected_candidate_sha256:
            return "candidate_changed"
        journal = AttemptSelectionJournal(
            run_id=run_id,
            candidate_id=candidate.candidate_id,
            candidate_sha256=candidate.source_sha256,
            source_attempt=candidate.attempt,
            idempotency_key=f"candidate-fork:{artifact_id}",
            state="complete",
            artifact_id=artifact_id,
            updated_at=_now_iso(),
        )
        write_selection_journal(run_dir, journal)
        stopped = terminate_registered_author_process(
            run_id,
            reason=f"candidate_fork_published:{artifact_id}",
        )
        log(
            "attempt_candidate_fork_published",
            run_id=run_id,
            candidate_id=candidate.candidate_id,
            attempt=candidate.attempt,
            artifact_id=artifact_id,
            process_termination_requested=stopped,
        )
        return "completed"


def assert_promotion_allowed(*, run_dir: Path, candidate_id: str) -> None:
    journal = load_selection_journal(run_dir)
    if journal is not None and journal.candidate_id != candidate_id:
        raise AttemptPromotionRejected(
            "normal promotion cannot replace the user-selected attempt"
        )


def _default_promoter(ctx: ToolContext, candidate: AttemptCandidate) -> None:
    if candidate.artifact_type.value == "poster":
        from .agents.external_designer_author import promote_selected_attempt
    elif candidate.artifact_type.value == "landing":
        from .agents.external_landing_author import promote_selected_attempt
    elif candidate.artifact_type.value == "deck":
        from .agents.external_slides_author import promote_selected_attempt
    elif candidate.artifact_type.value == "video":
        from .agents.external_video_author import promote_selected_attempt
    else:
        raise ValueError(
            f"unsupported selected artifact type: {candidate.artifact_type.value}"
        )
    promote_selected_attempt(ctx, candidate)


def materialize_candidate_for_editing(
    ctx: ToolContext,
    *,
    source_run_dir: Path,
    candidate: AttemptCandidate,
) -> Path:
    """Build an editable draft with the artifact's normal final materializer.

    Candidate snapshots remain immutable. A private copy is staged under the
    draft run, then passed through the same promotion path used for a selected
    attempt so Canvas never opens raw authoring output.
    """

    source_run_dir = source_run_dir.resolve()
    source = (source_run_dir / candidate.source_relative_path).resolve()
    try:
        source.relative_to(source_run_dir)
    except ValueError as exc:
        raise ValueError("candidate source escapes its run directory") from exc
    snapshot_root = next(
        (parent for parent in (source.parent, *source.parents) if parent.name == "candidate"),
        None,
    )
    if snapshot_root is None:
        raise ValueError("candidate snapshot root is missing")

    materialization_root = (
        ctx.run_dir / "attempt_materialization" / candidate.candidate_id
    )
    shutil.rmtree(materialization_root, ignore_errors=True)
    shutil.copytree(snapshot_root, materialization_root)

    def local_relative(relative_path: str) -> str:
        original = (source_run_dir / relative_path).resolve()
        try:
            snapshot_relative = original.relative_to(snapshot_root)
        except ValueError as exc:
            raise ValueError(
                "candidate member is outside its immutable snapshot"
            ) from exc
        return (
            materialization_root / snapshot_relative
        ).relative_to(ctx.run_dir).as_posix()

    local_candidate = candidate.model_copy(
        update={
            "run_id": ctx.run_id,
            "source_relative_path": local_relative(
                candidate.source_relative_path
            ),
            "preview_relative_paths": [
                local_relative(path)
                for path in candidate.preview_relative_paths
            ],
            "dependency_relative_paths": [
                local_relative(path)
                for path in candidate.dependency_relative_paths
            ],
            "validation_summary_relative_path": local_relative(
                candidate.validation_summary_relative_path
            ),
        }
    )
    ctx.state["artifact_type"] = candidate.artifact_type.value

    if candidate.artifact_type.value == "poster":
        from .agents.external_designer_author import promote_selected_attempt
    elif candidate.artifact_type.value == "landing":
        from .agents.external_landing_author import promote_selected_attempt
    elif candidate.artifact_type.value == "deck":
        from .agents.external_slides_author import promote_selected_attempt
    elif candidate.artifact_type.value == "video":
        from .agents.external_video_author import (
            materialize_selected_attempt_for_editing,
        )

        materialize_selected_attempt_for_editing(ctx, local_candidate)
        return ctx.run_dir / "final" / "deck.html"
    else:
        raise ValueError(
            f"unsupported candidate artifact type: {candidate.artifact_type.value}"
        )

    promote_selected_attempt(
        ctx,
        local_candidate,
        validate_for_delivery=False,
    )
    return ctx.run_dir / "final" / {
        "poster": "poster.html",
        "landing": "index.html",
        "deck": "deck.html",
    }[candidate.artifact_type.value]


def promote_pending_selection(
    ctx: ToolContext,
    *,
    promoter: Callable[[ToolContext, AttemptCandidate], None] | None = None,
) -> PromotionOutcome:
    ctx.raise_if_cancelled("attempt_selection.before_promotion_lease")
    with _promotion_lease(ctx.run_dir) as leased_run_dir:
        journal = load_selection_journal(ctx.run_dir)
        if journal is None:
            return "none"
        if journal.state == "complete":
            return "complete"
        if journal.state == "failed":
            return "failed"
        transaction = load_selection_adapter_transaction(ctx.run_dir)
        transaction_matches = bool(
            transaction
            and transaction.get("run_id") == journal.run_id
            and transaction.get("candidate_id") == journal.candidate_id
            and transaction.get("candidate_sha256") == journal.candidate_sha256
            and transaction.get("idempotency_key") == journal.idempotency_key
        )
        if journal.state in {"promoting", "delivering"}:
            if transaction_matches and transaction.get("phase") == "committed":
                artifact_id = str(
                    transaction.get("artifact_id") or f"art_{ctx.run_id}"
                )
                transition_selection(
                    ctx.run_dir,
                    "complete",
                    artifact_id=artifact_id,
                )
                return "complete"
            return "in_progress"
        promoter_returned = False
        try:
            ctx.raise_if_cancelled("attempt_selection.before_promotion_ownership")
            candidate = selected_candidate_for_run(ctx.run_dir)
            if candidate is None:
                return "none"
            ctx.raise_if_cancelled("attempt_selection.before_promoting_journal")
            transition_selection(ctx.run_dir, "promoting")
            transaction_payload = {
                "run_id": journal.run_id,
                "candidate_id": journal.candidate_id,
                "candidate_sha256": journal.candidate_sha256,
                "idempotency_key": journal.idempotency_key,
                "artifact_id": f"art_{ctx.run_id}",
                "phase": "started",
                "updated_at": _now_iso(),
            }
            ctx.raise_if_cancelled("attempt_selection.before_transaction_start")
            write_selection_adapter_transaction(
                ctx.run_dir,
                transaction_payload,
            )
            ctx.raise_if_cancelled("attempt_selection.before_promoter")
            with leased_promotion_tool_context(ctx, leased_run_dir):
                (promoter or _default_promoter)(ctx, candidate)
            promoter_returned = True
            ctx.raise_if_cancelled("attempt_selection.after_promoter")
            transaction_payload["phase"] = "committed"
            transaction_payload["updated_at"] = _now_iso()
            write_selection_adapter_transaction(
                ctx.run_dir,
                transaction_payload,
            )
            current = load_selection_journal(ctx.run_dir)
            if current is None:
                raise ValueError("attempt selection journal disappeared during promotion")
            if current.state not in {"complete", "failed"}:
                ctx.raise_if_cancelled("attempt_selection.before_complete_journal")
                transition_selection(
                    ctx.run_dir,
                    "complete",
                    artifact_id=f"art_{ctx.run_id}",
                )
            log(
                "attempt_selection_promoted",
                run_id=ctx.run_id,
                candidate_id=candidate.candidate_id,
                attempt=candidate.attempt,
            )
            return "complete"
        except RunCancelled:
            raise
        except Exception as exc:
            if promoter_returned:
                log(
                    "attempt_selection_reconciliation_required",
                    run_id=ctx.run_id,
                    error_code="attempt_promotion_commit_evidence_unavailable",
                    error_type=type(exc).__name__,
                )
                return "in_progress"
            current = load_selection_journal(ctx.run_dir)
            if current is not None and current.state not in {"complete", "failed"}:
                transition_selection(
                    ctx.run_dir,
                    "failed",
                    error_code="attempt_promotion_failed",
                    error_message="The selected attempt could not be finalized.",
                )
            log(
                "attempt_selection_promotion_failed",
                run_id=ctx.run_id,
                error_code="attempt_promotion_failed",
                error_type=type(exc).__name__,
            )
            return "failed"


def recover_attempt_selection(ctx: ToolContext) -> PromotionOutcome:
    """Resume a durable pending selection without restarting authoring."""
    return promote_pending_selection(ctx)
