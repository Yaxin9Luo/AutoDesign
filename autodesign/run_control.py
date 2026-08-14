"""Durable lifecycle records and logical write guards for generation runs."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Any, BinaryIO, Iterator, Literal
from uuid import uuid4

from .util.io import sha256_file


RunLifecycleState = Literal[
    "reserved", "uploading", "queued", "running", "completing",
    "completed", "cancelling", "cancelled", "failed",
]

_TERMINAL_STATES = frozenset({"completed", "cancelled", "failed"})
_TERMINAL_RECONCILIATION_FIELDS = frozenset({
    "terminal_reconciliation_decision",
    "terminal_reconciliation_phase",
    "terminal_reconciliation_terminal_state",
    "terminal_reconciliation_status",
    "terminal_reconciliation_diagnostic",
})
_ALLOWED_TERMINAL_RECONCILIATIONS = frozenset({
    ("accept", "preflight", "completed"),
    ("accept", "commit", "completed"),
    ("reject", "preflight", "failed"),
    ("reject", "commit", "failed"),
    ("reject", "commit", "cancelled"),
})
_ALLOWED_TRANSITIONS: dict[RunLifecycleState, frozenset[RunLifecycleState]] = {
    "reserved": frozenset({"uploading", "queued", "cancelling", "failed"}),
    "uploading": frozenset({"queued", "cancelling", "failed"}),
    "queued": frozenset({"running", "cancelling", "failed"}),
    "running": frozenset({"completing", "cancelling", "failed"}),
    "completing": frozenset({"completed", "cancelling", "failed"}),
    "cancelling": frozenset(),
    "completed": frozenset(),
    "cancelled": frozenset(),
    "failed": frozenset(),
}
_PAYLOAD_DIRECTORY_ROOTS = (
    "attempt_candidates",
    "attempt_materialization",
    "attempt_selection_work",
    "code_editor",
    "composites",
    "designer_author",
    "exports",
    "final",
    "generated-media",
    "generated_media",
    "html_first",
    "identity_logo_agent",
    "landing_author",
    "layers",
    "manifests",
    "media",
    "openresearch",
    "paper_evidence_packs",
    "panel_polish",
    "pipeline_cache",
    "pipeline_caches",
    "pptx-export",
    "quarantine",
    "reference_poster",
    "runtime_skills",
    "slides_author",
    "specs",
    "trajectory",
    "uploads",
    "video_author",
    "video_renders",
    "visual_refs",
)
_PAYLOAD_ROOT_FILES = frozenset({
    "apply_edits_palette_validation_failure.json",
    "academic_identity_assets.json",
    "author_quick_brief.md",
    "canvas_plan.json",
    "candidate_draft_lineage.json",
    "deck_plan.json",
    "delivery_manifest.json",
    "design_spec.json",
    "landing_trusted_source_hashes.json",
    "manifest.json",
    "paper_memory.json",
    "paper_memory.md",
    "paper_memory_dossier.json",
    "paper_memory_dossier.md",
    "paper_resource_manifest.json",
    "paper_resource_recall_audit.json",
    "paper_visual_provenance.json",
    "paper_visual_storyboard.json",
    "poster_content_brief.json",
    "poster_contract_preflight.json",
    "poster_plan_contract.json",
    "reuse_ingest_preload.json",
    "reference_style_audit.json",
    "reference_style_blueprint.html",
    "reference_style_blueprint_preview.png",
    "reference_style_contract.json",
    "reference_style_raw_blueprint_preview.png",
    "resume_state.json",
    "run_brief.json",
    "run_telemetry_summary.json",
    "run_manifest.json",
    "slides_trusted_source_hashes.json",
    "spec_recovery.json",
})
_UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS = frozenset({
    errno.EINVAL,
    getattr(errno, "ENOTSUP", errno.EINVAL),
    getattr(errno, "EOPNOTSUPP", errno.EINVAL),
})
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def validate_terminal_reconciliation_metadata(
    *,
    decision: object,
    phase: object,
    terminal_state: object,
    status: object,
    diagnostic: object,
) -> None:
    """Validate the one authoritative terminal reconciliation tuple."""
    if status == "invalid":
        valid = (
            decision is None
            and phase is None
            and terminal_state is None
            and isinstance(diagnostic, str)
            and bool(diagnostic)
        )
    else:
        target = (decision, phase, terminal_state)
        valid = (
            all(isinstance(value, str) for value in target)
            and target in _ALLOWED_TERMINAL_RECONCILIATIONS
            and status in {"pending", "succeeded"}
            and (diagnostic is None or isinstance(diagnostic, str))
        )
    if not valid:
        raise ValueError("invalid terminal reconciliation metadata")


class RunControlError(RuntimeError):
    """Base error for durable run lifecycle operations."""


class InvalidRunTransition(RunControlError):
    """The requested compare-and-swap lifecycle transition is invalid."""


class RunWritesFrozen(RunControlError):
    """A cancelled run rejected a new write."""


class RunCancelled(BaseException):
    """Cooperative cancellation interrupted the current phase."""

    def __init__(self, run_id: str, phase: str) -> None:
        self.run_id = str(run_id)
        self.phase = str(phase)
        super().__init__(
            f"run {self.run_id!r} was cancelled during {self.phase!r}",
        )


class CancellationToken:
    """Provider-neutral cooperative cancellation view for one run."""

    def __init__(
        self,
        *,
        store: "RunControlStore | None",
        run_id: str,
        signal_event: threading.Event | None = None,
    ) -> None:
        self._store = store
        self.run_id = str(run_id)
        self._signal_event = signal_event
        self._metadata_lock = threading.Lock()
        self._reason: str | None = None
        self._requested_at: float | None = None

    @classmethod
    def for_run(
        cls,
        store: "RunControlStore",
        run_id: str,
        signal_event: threading.Event | None = None,
    ) -> "CancellationToken":
        return cls(store=store, run_id=run_id, signal_event=signal_event)

    @classmethod
    def never(cls, run_id: str = "") -> "CancellationToken":
        return cls(store=None, run_id=run_id)

    @property
    def reason(self) -> str | None:
        return self._reason

    @property
    def requested_at(self) -> float | None:
        return self._requested_at

    @property
    def can_cancel(self) -> bool:
        return self._store is not None or self._signal_event is not None

    def is_cancelled(self) -> bool:
        if self._reason is not None:
            return True
        event = self._signal_event
        if event is not None and event.is_set():
            self._remember_cancellation("signal", time.time())
            return True
        if self._store is None:
            return False
        try:
            record = self._store.read(self.run_id)
        except Exception:
            self._remember_cancellation(
                "authoritative_control_unavailable",
                time.time(),
            )
            return True
        if record.state not in {"cancelling", "cancelled"}:
            return False
        self._remember_cancellation(
            record.cancellation_pending or "cancellation_requested",
            record.cancellation_requested_at or record.updated_at,
        )
        return True

    def is_cancel_requested(self) -> bool:
        return self.is_cancelled()

    def raise_if_cancelled(self, phase: str) -> None:
        if self.is_cancelled():
            raise RunCancelled(self.run_id, phase)

    def wait(self, timeout: float, poll_interval: float = 0.1) -> bool:
        timeout = max(0.0, float(timeout))
        poll_interval = max(0.001, float(poll_interval))
        deadline = time.monotonic() + timeout
        while True:
            if self.is_cancelled():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            delay = min(poll_interval, remaining)
            event = self._signal_event
            if event is not None:
                event.wait(delay)
            else:
                time.sleep(delay)

    def _remember_cancellation(self, reason: str, requested_at: float) -> None:
        if self._reason is not None:
            return
        with self._metadata_lock:
            if self._reason is None:
                self._reason = str(reason)
                self._requested_at = float(requested_at)


@dataclass(frozen=True)
class RunControlRecord:
    run_id: str
    artifact_type: str
    state: RunLifecycleState
    revision: int
    created_at: float
    updated_at: float
    worker_pid: int | None = None
    worker_pgid: int | None = None
    worker_birth_id: str | None = None
    worker_spawn_nonce: str | None = None
    cancellation_requested_at: float | None = None
    terminal_at: float | None = None
    terminal_event: str | None = None
    accepted_terminal_event_id: str | None = None
    cancellation_pending: str | None = None
    writes_frozen: bool = False
    publishable: bool = False
    parent_job_id: str | None = None
    cancel_snapshot_sha256: str | None = None
    result_digest: str | None = None
    terminal_reconciliation_decision: str | None = None
    terminal_reconciliation_phase: str | None = None
    terminal_reconciliation_terminal_state: str | None = None
    terminal_reconciliation_status: str | None = None
    terminal_reconciliation_diagnostic: str | None = None


def durable_replace_json(path: Path, payload: Any) -> Path:
    """Atomically replace JSON after flushing its bytes and parent directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=os.fspath(path.parent),
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return path


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError as exc:
        if exc.errno in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS:
            return
        raise
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if exc.errno not in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS:
            raise
    finally:
        os.close(descriptor)


class RunControlStore:
    """Owns cross-process lifecycle compare-and-swap operations.

    Lock ordering is ProcessLedger first, then this record lock whenever an
    operation needs both. Run-control methods never acquire a process ledger.
    """

    def __init__(self, runs_dir: Path | str) -> None:
        self.runs_dir = Path(runs_dir)

    def reserve(
        self,
        run_id: str,
        artifact_type: str,
        parent_job_id: str | None = None,
        *,
        initial_state: Literal["reserved", "queued"] = "reserved",
    ) -> RunControlRecord:
        if initial_state not in {"reserved", "queued"}:
            raise ValueError("initial_state must be 'reserved' or 'queued'")
        control_path = self._control_path(run_id)
        with self._record_lock(control_path):
            if control_path.exists():
                raise InvalidRunTransition(f"run {run_id!r} is already reserved")
            now = time.time()
            record = RunControlRecord(
                run_id=run_id,
                artifact_type=artifact_type,
                state=initial_state,
                revision=0,
                created_at=now,
                updated_at=now,
                parent_job_id=parent_job_id,
            )
            self._write_unlocked(control_path, record)
            return record

    def read(self, run_id: str) -> RunControlRecord:
        control_path = self._control_path(run_id)
        with self._record_lock(control_path):
            return self._read_unlocked(control_path)

    def transition(
        self,
        run_id: str,
        expected: RunControlRecord | tuple[RunLifecycleState, int],
        target: RunLifecycleState,
        **updates: Any,
    ) -> RunControlRecord:
        control_path = self._control_path(run_id)
        with self._record_lock(control_path):
            current = self._read_unlocked(control_path)
            self._assert_expected(current, expected)
            if target not in _ALLOWED_TRANSITIONS[current.state]:
                raise InvalidRunTransition(
                    f"cannot transition {run_id!r} from {current.state!r} to {target!r}",
                )
            return self._transition_unlocked(control_path, current, target, updates)

    def request_cancel(self, run_id: str) -> RunControlRecord:
        control_path = self._control_path(run_id)
        with self._record_lock(control_path):
            current = self._read_unlocked(control_path)
            if current.state in _TERMINAL_STATES or current.state == "cancelling":
                return current
            if "cancelling" not in _ALLOWED_TRANSITIONS[current.state]:
                raise InvalidRunTransition(
                    f"cannot request cancellation from {current.state!r}",
                )
            return self._transition_unlocked(
                control_path,
                current,
                "cancelling",
                {"cancellation_requested_at": time.time(), "cancellation_pending": None},
            )

    def update_terminal_reconciliation(
        self,
        run_id: str,
        expected: RunControlRecord,
        *,
        decision: str | None,
        phase: str | None,
        terminal_state: str | None,
        status: str | None,
        diagnostic: str | None,
    ) -> RunControlRecord:
        """CAS one terminal publication decision inside lifecycle authority."""
        try:
            validate_terminal_reconciliation_metadata(
                decision=decision,
                phase=phase,
                terminal_state=terminal_state,
                status=status,
                diagnostic=diagnostic,
            )
        except ValueError as exc:
            raise InvalidRunTransition(str(exc)) from exc
        control_path = self._control_path(run_id)
        with self._record_lock(control_path):
            current = self._read_unlocked(control_path)
            self._assert_expected(current, expected)
            return self._update_unlocked(
                control_path,
                current,
                {
                    "terminal_reconciliation_decision": decision,
                    "terminal_reconciliation_phase": phase,
                    "terminal_reconciliation_terminal_state": terminal_state,
                    "terminal_reconciliation_status": status,
                    "terminal_reconciliation_diagnostic": diagnostic,
                },
                allow_terminal_reconciliation=True,
            )

    def finalize_cancel(
        self,
        run_id: str,
        snapshot: dict[str, Any],
    ) -> RunControlRecord:
        control_path = self._control_path(run_id)
        with self._record_lock(control_path):
            current = self._read_unlocked(control_path)
            if current.state in _TERMINAL_STATES:
                return current
            if current.state != "cancelling":
                raise InvalidRunTransition(
                    f"cannot finalize cancellation from {current.state!r}",
                )
            if not _termination_verified(snapshot):
                pending = str(snapshot.get(
                    "cancellation_pending",
                    "managed_process_liveness_unverified",
                ))
                if current.cancellation_pending == pending:
                    return current
                return self._update_unlocked(
                    control_path,
                    current,
                    {"cancellation_pending": pending},
                )

            snapshot_payload = dict(snapshot)
            accepted_event_id = str(
                snapshot_payload.get("accepted_terminal_event_id")
                or snapshot_payload.get("event_id")
                or uuid4(),
            )
            snapshot_payload.update({
                "accepted_terminal_event_id": accepted_event_id,
                "confirmed_at": time.time(),
                "inventory": self._payload_inventory(control_path.parent),
                "publishable": False,
                "source_run_state": "cancelled",
            })
            snapshot_payload["inventory_sha256"] = _json_sha256(snapshot_payload["inventory"])
            snapshot_path = control_path.parent / "cancel_snapshot.json"
            durable_replace_json(snapshot_path, snapshot_payload)
            return self._transition_unlocked(
                control_path,
                current,
                "cancelled",
                {
                    "accepted_terminal_event_id": accepted_event_id,
                    "cancel_snapshot_sha256": sha256_file(snapshot_path),
                    "cancellation_pending": None,
                    "publishable": False,
                    "terminal_event": "run.cancelled",
                    "writes_frozen": True,
                },
            )

    def assert_writable(self, run_id: str) -> RunControlRecord:
        record = self.read(run_id)
        if record.writes_frozen or record.state == "cancelling":
            raise RunWritesFrozen(f"run {run_id!r} rejects writes after cancellation")
        return record

    def _payload_inventory(self, run_dir: Path) -> dict[str, dict[str, int | str]]:
        inventory: dict[str, dict[str, int | str]] = {}
        for root_name in _PAYLOAD_DIRECTORY_ROOTS:
            root = run_dir / root_name
            self._add_inventory_tree(inventory, run_dir, root)
        for root in sorted(run_dir.iterdir() if run_dir.exists() else ()):
            if _is_hyperframes_project_root(root):
                self._add_inventory_tree(inventory, run_dir, root)
        for file_name in _PAYLOAD_ROOT_FILES:
            path = run_dir / file_name
            if path.is_file():
                self._add_inventory_entry(inventory, run_dir, path)
        return inventory

    def _add_inventory_tree(
        self,
        inventory: dict[str, dict[str, int | str]],
        run_dir: Path,
        root: Path,
    ) -> None:
        if not root.is_dir():
            return
        for path in sorted(root.rglob("*")):
            if path.is_file():
                self._add_inventory_entry(inventory, run_dir, path)

    @staticmethod
    def _add_inventory_entry(
        inventory: dict[str, dict[str, int | str]],
        run_dir: Path,
        path: Path,
    ) -> None:
        stat = path.stat()
        inventory[path.relative_to(run_dir).as_posix()] = {
            "mtime_ns": stat.st_mtime_ns,
            "sha256": sha256_file(path),
            "size": stat.st_size,
        }

    def _transition_unlocked(
        self,
        control_path: Path,
        current: RunControlRecord,
        target: RunLifecycleState,
        updates: dict[str, Any],
    ) -> RunControlRecord:
        values = self._updated_values(current, updates)
        now = time.time()
        values.update({"revision": current.revision + 1, "state": target, "updated_at": now})
        if target in _TERMINAL_STATES:
            values["terminal_at"] = updates.get("terminal_at", now)
            values["terminal_event"] = updates.get("terminal_event", f"run.{target}")
            values["accepted_terminal_event_id"] = updates.get(
                "accepted_terminal_event_id",
                str(uuid4()),
            )
        record = RunControlRecord(**values)
        self._write_unlocked(control_path, record)
        return record

    def _update_unlocked(
        self,
        control_path: Path,
        current: RunControlRecord,
        updates: dict[str, Any],
        *,
        allow_terminal_reconciliation: bool = False,
    ) -> RunControlRecord:
        values = self._updated_values(
            current,
            updates,
            allow_terminal_reconciliation=allow_terminal_reconciliation,
        )
        values.update({"revision": current.revision + 1, "updated_at": time.time()})
        record = RunControlRecord(**values)
        self._write_unlocked(control_path, record)
        return record

    @staticmethod
    def _updated_values(
        current: RunControlRecord,
        updates: dict[str, Any],
        *,
        allow_terminal_reconciliation: bool = False,
    ) -> dict[str, Any]:
        values = asdict(current)
        allowed = set(values) - {"run_id", "artifact_type", "created_at", "revision", "state", "updated_at"}
        if not allow_terminal_reconciliation:
            allowed -= _TERMINAL_RECONCILIATION_FIELDS
        unexpected = set(updates) - allowed
        if unexpected:
            raise InvalidRunTransition(f"unsupported lifecycle updates: {sorted(unexpected)!r}")
        values.update(updates)
        return values

    @staticmethod
    def _assert_expected(
        current: RunControlRecord,
        expected: RunControlRecord | tuple[RunLifecycleState, int],
    ) -> None:
        if isinstance(expected, RunControlRecord):
            expected_state, expected_revision = expected.state, expected.revision
        elif isinstance(expected, tuple) and len(expected) == 2:
            expected_state, expected_revision = expected
        else:
            raise InvalidRunTransition("a transition requires an expected state and revision")
        if current.state != expected_state or current.revision != expected_revision:
            raise InvalidRunTransition(
                "stale lifecycle revision: "
                f"expected ({expected_state!r}, {expected_revision!r}), "
                f"found ({current.state!r}, {current.revision!r})",
            )

    def _read_unlocked(self, control_path: Path) -> RunControlRecord:
        try:
            payload = json.loads(control_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RunControlError(f"run control does not exist: {control_path.parent.name!r}") from exc
        allowed = {field.name for field in fields(RunControlRecord)}
        values = {name: value for name, value in payload.items() if name in allowed}
        try:
            record = RunControlRecord(**values)
        except TypeError as exc:
            raise RunControlError(f"invalid run control record: {control_path}") from exc
        if record.state not in _ALLOWED_TRANSITIONS:
            raise RunControlError(f"unknown lifecycle state: {record.state!r}")
        if record.run_id != control_path.parent.name:
            raise RunControlError(
                f"run control identity does not match its path: {control_path}",
            )
        return record

    @staticmethod
    def _write_unlocked(control_path: Path, record: RunControlRecord) -> None:
        durable_replace_json(control_path, asdict(record))

    def _run_dir(self, run_id: str) -> Path:
        validate_run_id(run_id)
        base = self.runs_dir.resolve()
        candidate = self.runs_dir / run_id
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(base)
        except ValueError as exc:
            raise RunControlError(f"run ID escapes runs directory: {run_id!r}") from exc
        return candidate

    def _control_path(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "run_control.json"

    @contextmanager
    def _record_lock(self, control_path: Path) -> Iterator[None]:
        control_path.parent.mkdir(parents=True, exist_ok=True)
        key = str(control_path.resolve())
        with _LOCKS_GUARD:
            local_lock = _LOCKS.setdefault(key, threading.RLock())
        lock_path = control_path.with_name(".run_control.lock")
        with local_lock:
            with lock_path.open("a+b") as handle:
                _lock_record_file(handle)
                try:
                    yield
                finally:
                    _unlock_record_file(handle)


def _lock_record_file(handle: BinaryIO) -> None:
    if os.name == "posix":
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    else:
        import msvcrt
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)


def _unlock_record_file(handle: BinaryIO) -> None:
    if os.name == "posix":
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    else:
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _termination_verified(snapshot: dict[str, Any]) -> bool:
    for field_name in ("termination_verified", "processes_verified_dead", "verified_dead"):
        if field_name in snapshot:
            return snapshot[field_name] is True
    return False


def _is_hyperframes_project_root(path: Path) -> bool:
    prefix = "hyperframes-"
    name = path.name
    suffix = name[len(prefix):] if name.startswith(prefix) else ""
    return path.is_dir() and bool(suffix) and all(
        character.isascii() and (character.isalnum() or character in "-_")
        for character in suffix
    )


def _json_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


_RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_WINDOWS_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


def validate_run_id(run_id: str) -> str:
    """Validate one cross-platform, single-component durable run identifier."""
    if not isinstance(run_id, str) or not _RUN_ID_PATTERN.fullmatch(run_id):
        raise RunControlError(f"invalid run ID: {run_id!r}")
    if run_id in {".", ".."}:
        raise RunControlError(f"invalid run ID: {run_id!r}")
    if run_id.endswith((".", " ")) or ":" in run_id:
        raise RunControlError(f"invalid run ID: {run_id!r}")
    if run_id.split(".", 1)[0].upper() in _WINDOWS_DEVICE_NAMES:
        raise RunControlError(f"invalid run ID: {run_id!r}")
    return run_id
