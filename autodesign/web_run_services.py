"""Transport-neutral reservation, upload, start, and cancellation services.

The service keeps request ``Settings``, the original request payload, and upload
bearer tokens only in process memory until start or a confirmed terminal path.
It then retains only nonsecret idempotency tombstones.  Durable lifecycle state
belongs to :class:`RunControlStore`; worker and terminal-event ownership belongs
to :class:`RunSupervisor`.

HTTP adapters may append one deduplicated, nonterminal cancellation-request
event when ``CancelResult.cancel_request_event_required`` is true.  They must
not append a terminal cancellation event: a supervisor invocation owns terminal
finalization, accepted-event persistence, and broadcast.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
import errno
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import stat
import time
from types import MappingProxyType
from typing import Any, AsyncIterable, AsyncIterator, BinaryIO, Callable, Mapping

from .config import Settings
from .run_control import (
    InvalidRunTransition,
    RunControlError,
    RunControlStore,
    validate_run_id,
)
from .run_supervisor import RunSupervisor, SupervisedRun
from .run_worker_protocol import RunWorkerRequest


_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_TERMINAL_STATES = frozenset({"completed", "cancelled", "failed"})
_HAS_DIRECTORY_FD = (
    os.name == "posix"
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.rename in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
)


class WebRunServiceError(RuntimeError):
    """Base error for transport-neutral Web run orchestration."""


class InvalidReservation(WebRunServiceError, ValueError):
    """Reservation metadata is incomplete or malformed."""


class ReservationConflict(WebRunServiceError):
    """An idempotency key was reused for a different request digest."""


class ReservationNotFound(WebRunServiceError, KeyError):
    """The service process does not own the requested in-memory reservation."""


class InvalidInputSlot(WebRunServiceError, ValueError):
    """A declared upload slot cannot map to one safe file component."""


class UploadAuthorizationError(WebRunServiceError, PermissionError):
    """The opaque upload token does not authorize this reservation."""


class UploadConflict(WebRunServiceError):
    """An upload conflicts with an active or already-completed slot."""


class UploadIntegrityError(WebRunServiceError):
    """An upload does not match its declared byte size and SHA-256 digest."""


class UploadCancelled(WebRunServiceError):
    """A durable cancellation request interrupted a streaming upload."""


class RunNotReady(WebRunServiceError):
    """A run cannot start from its current authoritative lifecycle state."""


@dataclass(frozen=True)
class InputSlot:
    name: str
    expected_sha256: str
    expected_size: int


@dataclass(frozen=True)
class ReservationResult:
    run_id: str
    upload_token: str
    input_slots: tuple[InputSlot, ...]
    state: str
    expires_at: float
    reused: bool = False


@dataclass(frozen=True)
class UploadResult:
    run_id: str
    slot: str
    path: Path
    sha256: str
    size: int
    state: str
    idempotent: bool = False


@dataclass(frozen=True)
class CancelResult:
    run_id: str
    state: str
    confirmed: bool
    already_terminal: bool
    supervisor_invoked: bool
    terminal_event_handled_by_supervisor: bool
    terminated_pids: tuple[int, ...] = ()
    surviving_pids: tuple[int, ...] = ()
    cancel_request_event_required: bool = False


RequestFactory = Callable[
    [str, Settings, Any, Mapping[str, Path]],
    RunWorkerRequest,
]


@dataclass
class _UploadDirectories:
    run_id: str
    runs_fd: int
    run_fd: int
    uploads_fd: int

    def close(self) -> None:
        first_error: OSError | None = None
        for name in ("uploads_fd", "run_fd", "runs_fd"):
            descriptor = getattr(self, name)
            if descriptor >= 0:
                setattr(self, name, -1)
                try:
                    os.close(descriptor)
                except OSError as exc:
                    if exc.errno != errno.EBADF and first_error is None:
                        first_error = exc
        if first_error is not None:
            raise first_error


@dataclass
class _UploadWriter:
    handle: BinaryIO
    device: int
    inode: int
    directories: _UploadDirectories | None = None
    closed: asyncio.Event = field(default_factory=asyncio.Event)

    def close(self) -> None:
        if not self.handle.closed:
            self.handle.close()

    def finish(self) -> None:
        try:
            self.close()
        finally:
            try:
                if self.directories is not None:
                    self.directories.close()
            finally:
                self.closed.set()


@dataclass
class _RunContext:
    reservation: ReservationResult
    request_digest: str
    settings: Settings | None
    payload: Any | None
    slots: dict[str, InputSlot]
    upload_token_digest: bytes
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    abort_event: asyncio.Event = field(default_factory=asyncio.Event)
    writers: dict[str, _UploadWriter] = field(default_factory=dict)
    completed: dict[str, Path] = field(default_factory=dict)
    start_task: asyncio.Task[SupervisedRun] | None = None
    supervised_run: SupervisedRun | None = None
    cancel_task: asyncio.Task[CancelResult] | None = None
    expiry_claimed: bool = False


class WebRunServices:
    """Compose durable run control with in-process streaming ownership."""

    def __init__(
        self,
        runs_dir: str | Path,
        *,
        control_store: RunControlStore,
        supervisor: RunSupervisor,
        upload_close_timeout_s: float = 2.0,
        reservation_ttl_s: float = 15 * 60.0,
    ) -> None:
        self.runs_dir = Path(runs_dir)
        self.control_store = control_store
        self.supervisor = supervisor
        self.upload_close_timeout_s = max(0.0, float(upload_close_timeout_s))
        self.reservation_ttl_s = max(0.001, float(reservation_ttl_s))
        self._registry_lock = asyncio.Lock()
        self._contexts: dict[str, _RunContext] = {}
        self._idempotency: dict[str, _RunContext] = {}
        self._expiry_terminal_pending: set[str] = set()
        self._cancel_request_events_pending: set[str] = set()

    async def reserve(
        self,
        *,
        run_id: str,
        artifact_type: str,
        idempotency_key: str,
        request_digest: str,
        settings: Settings,
        payload: Any,
        input_slots: tuple[InputSlot, ...],
        parent_job_id: str | None = None,
        expires_at: float | None = None,
    ) -> ReservationResult:
        """Durably reserve before returning authority to upload any bytes."""
        slots = self._validated_slots(input_slots)
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise InvalidReservation("idempotency_key must be a non-empty string")
        if len(idempotency_key) > 256:
            raise InvalidReservation("idempotency_key exceeds 256 characters")
        if not isinstance(request_digest, str) or not _SHA256_PATTERN.fullmatch(request_digest):
            raise InvalidReservation("request_digest must be a lowercase SHA-256 digest")
        if not isinstance(artifact_type, str) or not artifact_type.strip():
            raise InvalidReservation("artifact_type must be a non-empty string")
        try:
            validate_run_id(run_id)
        except RunControlError as exc:
            raise InvalidReservation(str(exc)) from exc
        deadline = time.time() + self.reservation_ttl_s if expires_at is None else float(expires_at)
        if not deadline < float("inf"):
            raise InvalidReservation("expires_at must be finite")

        token = secrets.token_urlsafe(32)
        async with self._registry_lock:
            existing = self._idempotency.get(idempotency_key)
            if existing is not None:
                if existing.request_digest != request_digest:
                    raise ReservationConflict(
                        "idempotency key was already used with a different request digest"
                    )
                current = self.control_store.read(existing.reservation.run_id)
                return replace(existing.reservation, state=current.state, reused=True)

            record = self.control_store.reserve(
                run_id,
                artifact_type.strip(),
                parent_job_id=parent_job_id,
                initial_state="queued" if not slots else "reserved",
            )
            reservation = ReservationResult(
                run_id=run_id,
                upload_token=token,
                input_slots=tuple(slots.values()),
                state=record.state,
                expires_at=deadline,
            )
            context = _RunContext(
                reservation=reservation,
                request_digest=request_digest,
                settings=settings,
                payload=payload,
                slots=slots,
                upload_token_digest=self._token_digest(token),
            )
            self._contexts[run_id] = context
            self._idempotency[idempotency_key] = context
            return reservation

    async def recover_queued_derived_reservation(
        self,
        *,
        run_id: str,
        artifact_type: str,
        parent_job_id: str,
        idempotency_key: str,
        request_digest: str,
        descriptor_sha256: str,
        settings: Settings,
        payload: Any,
    ) -> ReservationResult:
        """Recreate one no-upload derived start context after process restart."""
        try:
            validate_run_id(run_id)
            validate_run_id(parent_job_id)
        except RunControlError as exc:
            raise InvalidReservation(str(exc)) from exc
        if idempotency_key != f"derived:{run_id}":
            raise InvalidReservation("derived recovery idempotency key is invalid")
        if not isinstance(artifact_type, str) or not artifact_type.strip():
            raise InvalidReservation("artifact_type must be a non-empty string")
        if not isinstance(request_digest, str) or not _SHA256_PATTERN.fullmatch(
            request_digest
        ):
            raise InvalidReservation("request_digest must be a lowercase SHA-256 digest")
        if not isinstance(descriptor_sha256, str) or not _SHA256_PATTERN.fullmatch(
            descriptor_sha256
        ):
            raise InvalidReservation(
                "descriptor_sha256 must be a lowercase SHA-256 digest"
            )
        descriptor_path = self.runs_dir / run_id / "derived_job.json"
        if descriptor_path.is_symlink() or not descriptor_path.is_file():
            raise ReservationConflict("durable derived descriptor is unavailable")
        try:
            actual_descriptor_sha256 = hashlib.sha256(
                descriptor_path.read_bytes()
            ).hexdigest()
        except OSError as exc:
            raise ReservationConflict("durable derived descriptor is unreadable") from exc
        if not hmac.compare_digest(actual_descriptor_sha256, descriptor_sha256):
            raise ReservationConflict("durable derived descriptor changed")

        token = secrets.token_urlsafe(32)
        async with self._registry_lock:
            existing = self._contexts.get(run_id)
            claimed = self._idempotency.get(idempotency_key)
            if existing is not None or claimed is not None:
                if existing is None or claimed is not existing:
                    raise ReservationConflict(
                        "derived recovery identity conflicts with an active reservation"
                    )
                record = self.control_store.read(run_id)
                if (
                    existing.request_digest != request_digest
                    or existing.slots
                    or record.state != "queued"
                    or record.artifact_type != artifact_type.strip()
                    or record.parent_job_id != parent_job_id
                    or not existing.reservation.upload_token
                ):
                    raise ReservationConflict(
                        "derived recovery context no longer matches durable state"
                    )
                return replace(
                    existing.reservation,
                    state=record.state,
                    reused=True,
                )

            record = self.control_store.read(run_id)
            if (
                record.state != "queued"
                or record.artifact_type != artifact_type.strip()
                or record.parent_job_id != parent_job_id
                or record.writes_frozen
                or record.worker_pid is not None
                or record.worker_pgid is not None
                or record.worker_birth_id is not None
                or record.worker_spawn_nonce is not None
            ):
                raise ReservationConflict(
                    "queued derived reservation does not match durable state"
                )
            reservation = ReservationResult(
                run_id=run_id,
                upload_token=token,
                input_slots=(),
                state=record.state,
                expires_at=time.time() + self.reservation_ttl_s,
                reused=True,
            )
            context = _RunContext(
                reservation=reservation,
                request_digest=request_digest,
                settings=settings,
                payload=payload,
                slots={},
                upload_token_digest=self._token_digest(token),
            )
            self._contexts[run_id] = context
            self._idempotency[idempotency_key] = context
            return reservation

    async def expired_reservation_ids(self, *, now: float | None = None) -> tuple[str, ...]:
        """Return local reservations eligible to claim for expiry."""
        cutoff = time.time() if now is None else float(now)
        async with self._registry_lock:
            contexts = tuple(self._contexts.values())
        expired: list[str] = []
        for context in contexts:
            if context.reservation.expires_at > cutoff:
                continue
            async with context.lock:
                record = self.control_store.read(context.reservation.run_id)
                if (
                    context.reservation.expires_at <= cutoff
                    and record.state in {"reserved", "uploading", "queued"}
                    and context.start_task is None
                    and context.supervised_run is None
                ):
                    expired.append(context.reservation.run_id)
        return tuple(sorted(expired))

    async def reconcile_expired_reservations(
        self,
        *,
        now: float | None = None,
    ) -> tuple[str, ...]:
        """Quiesce and fail abandoned local reservations, then emit terminal events."""
        cutoff = time.time() if now is None else float(now)
        candidates = await self.expired_reservation_ids(now=now)
        transitioned: list[str] = []
        for run_id in candidates:
            context = await self._find_context(run_id)
            if context is None:
                continue
            async with context.lock:
                current = self.control_store.read(run_id)
                if (
                    context.reservation.expires_at > cutoff
                    or current.state not in {"reserved", "uploading", "queued"}
                    or context.start_task is not None
                    or context.supervised_run is not None
                ):
                    continue
                context.expiry_claimed = True
                context.abort_event.set()
                writers = tuple(context.writers.values())
                for writer in writers:
                    writer.close()

            if not await self._wait_for_writer_close(
                tuple(writer.closed for writer in writers)
            ):
                continue

            async with context.lock:
                current = self.control_store.read(run_id)
                if (
                    context.reservation.expires_at > cutoff
                    or current.state not in {"reserved", "uploading", "queued"}
                    or context.start_task is not None
                    or context.supervised_run is not None
                    or any(not writer.closed.is_set() for writer in context.writers.values())
                ):
                    continue
                try:
                    self.control_store.transition(
                        run_id,
                        current,
                        "failed",
                        publishable=False,
                        writes_frozen=True,
                        cancellation_pending="reservation_expired",
                        terminal_event="run.error",
                    )
                except InvalidRunTransition:
                    continue
                self._expiry_terminal_pending.add(run_id)
                transitioned.append(run_id)

        for run_id in tuple(sorted(self._expiry_terminal_pending)):
            await self.supervisor.recover(run_id)
            self._expiry_terminal_pending.discard(run_id)
            context = await self._find_context(run_id)
            if context is not None:
                async with context.lock:
                    self._sanitize_context(context)
        return tuple(transitioned)

    async def upload(
        self,
        run_id: str,
        upload_token: str,
        slot_name: str,
        chunks: AsyncIterable[bytes],
    ) -> UploadResult:
        """Stream a declared slot through a cancellable ``.partial`` file."""
        context = await self._authorized_context(run_id, upload_token)
        slot = context.slots.get(slot_name)
        if slot is None:
            raise UploadConflict(f"upload slot is not declared: {slot_name!r}")
        partial_path = self.runs_dir / run_id / "uploads" / f"{slot_name}.partial"
        final_path = self.runs_dir / run_id / "uploads" / slot_name

        writer: _UploadWriter | None = None
        already_complete = False
        async with context.lock:
            record = self.control_store.read(run_id)
            if context.abort_event.is_set() or record.state in {"cancelling", "cancelled"}:
                raise UploadCancelled(f"run {run_id!r} was cancelled before upload")
            if record.state not in {"reserved", "uploading", "queued"}:
                raise RunNotReady(
                    f"run {run_id!r} cannot accept uploads from {record.state!r}"
                )
            if slot_name in context.writers:
                raise UploadConflict(f"upload slot is already active: {slot_name!r}")
            already_complete = slot_name in context.completed
            if record.state == "queued" and not already_complete:
                raise UploadConflict(f"queued run has no completed slot {slot_name!r}")
            directories = self._prepare_upload_paths(
                run_id,
                partial_path=partial_path,
                final_path=final_path,
                already_complete=already_complete,
                slot=slot,
            )
            writer = self._open_owned_partial(
                partial_path,
                directories=directories,
            )
            context.writers[slot_name] = writer
            if record.state == "reserved":
                try:
                    self.control_store.transition(run_id, record, "uploading")
                except BaseException as exc:
                    writer.finish()
                    context.writers.pop(slot_name, None)
                    if isinstance(exc, InvalidRunTransition):
                        current = self.control_store.read(run_id)
                        if current.state in {"cancelling", "cancelled"}:
                            context.abort_event.set()
                            raise UploadCancelled(
                                f"run {run_id!r} was cancelled during upload registration"
                            ) from exc
                    raise

        digest = hashlib.sha256()
        size = 0
        try:
            iterator = chunks.__aiter__()
            while True:
                try:
                    chunk = await self._next_chunk_or_abort(
                        run_id,
                        iterator,
                        context.abort_event,
                    )
                except StopAsyncIteration:
                    break
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise UploadIntegrityError("upload chunks must be bytes-like")
                value = bytes(chunk)
                if context.abort_event.is_set():
                    raise UploadCancelled(f"run {run_id!r} was cancelled during upload")
                writer.handle.write(value)
                digest.update(value)
                size += len(value)
                if size > slot.expected_size:
                    if already_complete:
                        raise UploadConflict(
                            f"completed upload slot {slot_name!r} received different bytes"
                        )
                    raise UploadIntegrityError(
                        f"upload slot {slot_name!r} exceeds declared size"
                    )

            actual_sha256 = digest.hexdigest()
            matches = size == slot.expected_size and actual_sha256 == slot.expected_sha256
            if not matches:
                if already_complete:
                    raise UploadConflict(
                        f"completed upload slot {slot_name!r} received different bytes"
                    )
                raise UploadIntegrityError(
                    f"upload slot {slot_name!r} does not match declared size and SHA-256"
                )

            async with context.lock:
                record = self.control_store.read(run_id)
                if context.abort_event.is_set() or record.state in {"cancelling", "cancelled"}:
                    raise UploadCancelled(f"run {run_id!r} was cancelled during upload")
                writer.handle.flush()
                os.fsync(writer.handle.fileno())
                opened = os.fstat(writer.handle.fileno())
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or (opened.st_dev, opened.st_ino) != (writer.device, writer.inode)
                    or opened.st_size != size
                ):
                    raise UploadIntegrityError(
                        f"upload slot {slot_name!r} changed before promotion"
                    )
                writer.close()
                if writer.directories is not None:
                    self._verify_upload_directories(writer.directories)
                    self._require_file_entry_identity_at(
                        f"{slot_name}.partial",
                        directory_fd=writer.directories.uploads_fd,
                        display_path=partial_path,
                        device=writer.device,
                        inode=writer.inode,
                        link_count=1,
                    )
                else:
                    self._require_upload_path_tree(run_id)
                    self._require_file_entry_identity(
                        partial_path,
                        device=writer.device,
                        inode=writer.inode,
                        link_count=1,
                    )
                if already_complete:
                    final_size, final_digest = self._regular_file_identity(
                        final_path,
                        directory_fd=(
                            writer.directories.uploads_fd
                            if writer.directories is not None
                            else None
                        ),
                        entry_name=slot_name,
                    )
                    if (
                        final_size != slot.expected_size
                        or final_digest != slot.expected_sha256
                    ):
                        raise UploadIntegrityError(
                            f"completed upload slot {slot_name!r} changed before retry"
                        )
                    if writer.directories is not None:
                        os.unlink(
                            f"{slot_name}.partial",
                            dir_fd=writer.directories.uploads_fd,
                        )
                        self._fsync_directory_fd(writer.directories.uploads_fd)
                        self._verify_upload_directories(writer.directories)
                    else:
                        self._require_upload_path_tree(run_id)
                        partial_path.unlink(missing_ok=True)
                        self._fsync_upload_directory(partial_path.parent)
                        self._require_upload_path_tree(run_id)
                    return UploadResult(
                        run_id=run_id,
                        slot=slot_name,
                        path=final_path,
                        sha256=actual_sha256,
                        size=size,
                        state=record.state,
                        idempotent=True,
                    )
                if self._entry_exists(
                    final_path,
                    directory_fd=(
                        writer.directories.uploads_fd
                        if writer.directories is not None
                        else None
                    ),
                    entry_name=slot_name,
                ):
                    raise UploadIntegrityError(
                        f"upload destination already exists: {final_path}"
                    )
                if writer.directories is not None:
                    os.replace(
                        f"{slot_name}.partial",
                        slot_name,
                        src_dir_fd=writer.directories.uploads_fd,
                        dst_dir_fd=writer.directories.uploads_fd,
                    )
                    self._fsync_directory_fd(writer.directories.uploads_fd)
                    self._verify_upload_directories(writer.directories)
                    self._require_file_entry_identity_at(
                        slot_name,
                        directory_fd=writer.directories.uploads_fd,
                        display_path=final_path,
                        device=writer.device,
                        inode=writer.inode,
                        link_count=1,
                    )
                else:
                    self._require_upload_path_tree(run_id)
                    os.replace(partial_path, final_path)
                    self._fsync_upload_directory(final_path.parent)
                    self._require_upload_path_tree(run_id)
                    self._require_file_entry_identity(
                        final_path,
                        device=writer.device,
                        inode=writer.inode,
                        link_count=1,
                    )
                context.completed[slot_name] = final_path
                if len(context.completed) == len(context.slots):
                    if record.state in {"reserved", "uploading"}:
                        record = self.control_store.transition(run_id, record, "queued")
                return UploadResult(
                    run_id=run_id,
                    slot=slot_name,
                    path=final_path,
                    sha256=actual_sha256,
                    size=size,
                    state=record.state,
                )
        finally:
            if writer is not None:
                writer.finish()
                async with context.lock:
                    if context.writers.get(slot_name) is writer:
                        context.writers.pop(slot_name, None)

    async def start(
        self,
        run_id: str,
        upload_token: str,
        request_factory: RequestFactory,
    ) -> SupervisedRun:
        """Start exactly one supervisor task after every declared slot is queued."""
        context = await self._authorized_context(run_id, upload_token)
        async with context.lock:
            if context.supervised_run is not None:
                return context.supervised_run
            task = context.start_task
            if task is None:
                record = self.control_store.read(run_id)
                if record.state != "queued" or context.abort_event.is_set():
                    raise RunNotReady(
                        f"run {run_id!r} cannot start from {record.state!r}"
                    )
                self._verify_completed_slots(context)
                if context.settings is None or context.payload is None:
                    raise RunNotReady(f"run {run_id!r} no longer retains a start request")
                completed = MappingProxyType(dict(context.completed))
                request = request_factory(
                    run_id,
                    context.settings,
                    context.payload,
                    completed,
                )
                if getattr(request, "run_id", None) != run_id:
                    raise RunNotReady("request factory returned a different run_id")
                if (
                    hasattr(request, "settings")
                    and getattr(request, "settings") is not context.settings
                ):
                    raise RunNotReady("request factory must preserve the exact Settings object")
                task = self._create_observed_task(
                    self._start_and_adopt(context, request)
                )
                context.start_task = task
        return await self._await_observed_task(task)

    async def _start_and_adopt(
        self,
        context: _RunContext,
        request: RunWorkerRequest,
    ) -> SupervisedRun:
        try:
            supervised = await self.supervisor.start(request)
        except BaseException:
            async with context.lock:
                if context.start_task is asyncio.current_task():
                    context.start_task = None
                record = self.control_store.read(context.reservation.run_id)
                if record.state in _TERMINAL_STATES:
                    self._sanitize_context(context)
            raise
        async with context.lock:
            if context.start_task is asyncio.current_task():
                context.start_task = None
                context.supervised_run = supervised
                self._sanitize_context(context)
        return supervised

    async def cancel(self, run_id: str, reason: str) -> CancelResult:
        """Persist, stop uploads, join their writers, then delegate terminal work."""
        context = await self._find_context(run_id)
        before = self.control_store.read(run_id)
        requested = self.control_store.request_cancel(run_id)
        transitioned_to_cancelling = (
            requested.state == "cancelling"
            and before.state not in {"cancelling", "cancelled", "completed", "failed"}
            and requested.revision == before.revision + 1
        )
        if transitioned_to_cancelling:
            self._cancel_request_events_pending.add(run_id)
        if context is None:
            task = self._create_observed_task(
                self._finish_cancel(run_id, reason, ())
            )
            result = await self._await_observed_task(task)
            return replace(
                result,
                cancel_request_event_required=(
                    await self._claim_cancel_request_event(run_id)
                ),
            )

        task = self._create_observed_task(
            self._cancel_with_context(context, run_id, reason)
        )
        result = await self._await_observed_task(task)
        return replace(
            result,
            cancel_request_event_required=(
                await self._claim_cancel_request_event(run_id)
            ),
        )

    async def _cancel_with_context(
        self,
        context: _RunContext,
        run_id: str,
        reason: str,
    ) -> CancelResult:
        async with context.lock:
            context.abort_event.set()
            writers = tuple(context.writers.values())
            for writer in writers:
                writer.close()

            task = context.cancel_task
            retry = False
            if task is not None and task.done():
                try:
                    retry = not task.result().confirmed
                except BaseException:
                    retry = True
            if task is None or retry:
                task = self._create_observed_task(
                    self._finish_cancel_and_adopt(
                        context,
                        run_id,
                        reason,
                        tuple(writer.closed for writer in writers),
                    )
                )
                context.cancel_task = task
        return await task

    async def _finish_cancel_and_adopt(
        self,
        context: _RunContext,
        run_id: str,
        reason: str,
        writer_closed_events: tuple[asyncio.Event, ...],
    ) -> CancelResult:
        try:
            result = await self._finish_cancel(run_id, reason, writer_closed_events)
        except BaseException:
            async with context.lock:
                if context.cancel_task is asyncio.current_task():
                    context.cancel_task = None
                record = self.control_store.read(run_id)
                if record.state in _TERMINAL_STATES:
                    self._sanitize_context(context)
            raise
        if result.confirmed:
            async with context.lock:
                self._sanitize_context(context)
        return result

    async def _claim_cancel_request_event(self, run_id: str) -> bool:
        async with self._registry_lock:
            if run_id not in self._cancel_request_events_pending:
                return False
            self._cancel_request_events_pending.remove(run_id)
            return True

    @staticmethod
    def _create_observed_task(coroutine: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine)
        task.add_done_callback(WebRunServices._observe_task_exception)
        return task

    @staticmethod
    def _observe_task_exception(task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        task.exception()

    @staticmethod
    async def _await_observed_task(task: asyncio.Task[Any]) -> Any:
        done, _ = await asyncio.wait((task,))
        return next(iter(done)).result()

    async def _finish_cancel(
        self,
        run_id: str,
        reason: str,
        writer_closed_events: tuple[asyncio.Event, ...],
    ) -> CancelResult:
        if writer_closed_events:
            waiters = tuple(
                asyncio.create_task(closed.wait()) for closed in writer_closed_events
            )
            try:
                await asyncio.wait_for(
                    asyncio.gather(*waiters),
                    timeout=self.upload_close_timeout_s,
                )
            except asyncio.TimeoutError:
                return CancelResult(
                    run_id=run_id,
                    state="cancelling",
                    confirmed=False,
                    already_terminal=False,
                    supervisor_invoked=False,
                    terminal_event_handled_by_supervisor=False,
                )
            finally:
                for waiter in waiters:
                    if not waiter.done():
                        waiter.cancel()
                await asyncio.gather(*waiters, return_exceptions=True)

        outcome = await self.supervisor.cancel(run_id, reason)
        return CancelResult(
            run_id=run_id,
            state=outcome.state,
            confirmed=(
                outcome.state in {"completed", "cancelled", "failed"}
                and outcome.quiesced
            ),
            already_terminal=outcome.already_terminal,
            supervisor_invoked=True,
            terminal_event_handled_by_supervisor=True,
            terminated_pids=outcome.terminated_pids,
            surviving_pids=outcome.surviving_pids,
        )

    async def _authorized_context(
        self,
        run_id: str,
        upload_token: str,
    ) -> _RunContext:
        context = await self._context(run_id)
        if not isinstance(upload_token, str) or not hmac.compare_digest(
            self._token_digest(upload_token),
            context.upload_token_digest,
        ):
            raise UploadAuthorizationError("upload token is invalid")
        return context

    async def _context(self, run_id: str) -> _RunContext:
        context = await self._find_context(run_id)
        if context is None:
            raise ReservationNotFound(run_id)
        return context

    async def _find_context(self, run_id: str) -> _RunContext | None:
        async with self._registry_lock:
            return self._contexts.get(run_id)

    @staticmethod
    def _token_digest(upload_token: str) -> bytes:
        return hashlib.sha256(upload_token.encode("utf-8")).digest()

    @staticmethod
    def _sanitize_context(context: _RunContext) -> None:
        context.settings = None
        context.payload = None
        if context.reservation.upload_token:
            context.reservation = replace(context.reservation, upload_token="")

    async def _wait_for_writer_close(
        self,
        writer_closed_events: tuple[asyncio.Event, ...],
    ) -> bool:
        if not writer_closed_events:
            return True
        waiters = tuple(
            asyncio.create_task(closed.wait()) for closed in writer_closed_events
        )
        try:
            await asyncio.wait_for(
                asyncio.gather(*waiters),
                timeout=self.upload_close_timeout_s,
            )
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            for waiter in waiters:
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)

    def _prepare_upload_paths(
        self,
        run_id: str,
        *,
        partial_path: Path,
        final_path: Path,
        already_complete: bool,
        slot: InputSlot,
    ) -> _UploadDirectories | None:
        if _HAS_DIRECTORY_FD:
            directories = self._open_upload_directories(run_id)
            try:
                partial_name = f"{slot.name}.partial"
                if already_complete:
                    size, digest = self._regular_file_identity(
                        final_path,
                        directory_fd=directories.uploads_fd,
                        entry_name=slot.name,
                    )
                    if size != slot.expected_size or digest != slot.expected_sha256:
                        raise UploadIntegrityError(
                            f"completed upload slot {slot.name!r} changed before retry"
                        )
                elif self._entry_exists(
                    final_path,
                    directory_fd=directories.uploads_fd,
                    entry_name=slot.name,
                ):
                    raise UploadIntegrityError(
                        f"upload destination already exists: {final_path}"
                    )
                if self._entry_exists(
                    partial_path,
                    directory_fd=directories.uploads_fd,
                    entry_name=partial_name,
                ):
                    metadata = os.stat(
                        partial_name,
                        dir_fd=directories.uploads_fd,
                        follow_symlinks=False,
                    )
                    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                        raise UploadIntegrityError(
                            f"partial upload is not one owned regular file: {partial_path}"
                        )
                self._verify_upload_directories(directories)
                return directories
            except BaseException:
                directories.close()
                raise

        run_dir = self.runs_dir / run_id
        uploads_dir = run_dir / "uploads"
        self._require_upload_path_tree(run_id, require_uploads=False)
        if self._path_entry_exists(uploads_dir):
            self._require_plain_directory(uploads_dir, "uploads directory")
        else:
            try:
                uploads_dir.mkdir(mode=0o700)
            except OSError as exc:
                raise UploadIntegrityError(
                    f"uploads directory could not be created safely: {uploads_dir}"
                ) from exc
            self._require_plain_directory(uploads_dir, "uploads directory")
            self._fsync_upload_directory(run_dir)
        self._require_upload_path_tree(run_id)
        if already_complete:
            size, digest = self._regular_file_identity(final_path)
            if size != slot.expected_size or digest != slot.expected_sha256:
                raise UploadIntegrityError(
                    f"completed upload slot {slot.name!r} changed before retry"
                )
        elif self._path_entry_exists(final_path):
            raise UploadIntegrityError(f"upload destination already exists: {final_path}")

        if self._path_entry_exists(partial_path):
            try:
                metadata = partial_path.lstat()
            except OSError as exc:
                raise UploadIntegrityError(
                    f"partial upload cannot be inspected safely: {partial_path}"
                ) from exc
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise UploadIntegrityError(
                    f"partial upload is not one owned regular file: {partial_path}"
                )
        self._require_upload_path_tree(run_id)
        return None

    def _open_owned_partial(
        self,
        path: Path,
        *,
        directories: _UploadDirectories | None,
    ) -> _UploadWriter:
        common_flags = getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        entry_name = path.name
        existed = self._entry_exists(
            path,
            directory_fd=directories.uploads_fd if directories is not None else None,
            entry_name=entry_name,
        )
        flags = os.O_WRONLY | common_flags
        if not existed:
            flags |= os.O_CREAT | os.O_EXCL
        try:
            if directories is None:
                descriptor = os.open(path, flags, 0o600)
            else:
                descriptor = os.open(
                    entry_name,
                    flags,
                    0o600,
                    dir_fd=directories.uploads_fd,
                )
        except OSError as exc:
            if directories is not None:
                directories.close()
            raise UploadIntegrityError(
                f"partial upload cannot be opened safely: {path}"
            ) from exc
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise UploadIntegrityError(
                    f"partial upload is not one owned regular file: {path}"
                )
            if directories is None:
                self._require_upload_path_tree(path.parent.parent.name)
                current = path.lstat()
            else:
                self._verify_upload_directories(directories)
                current = os.stat(
                    entry_name,
                    dir_fd=directories.uploads_fd,
                    follow_symlinks=False,
                )
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise UploadIntegrityError(f"partial upload changed during open: {path}")
            os.ftruncate(descriptor, 0)
            handle = os.fdopen(descriptor, "wb")
        except BaseException:
            try:
                os.close(descriptor)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise
            if directories is not None:
                directories.close()
            raise
        return _UploadWriter(
            handle=handle,
            device=opened.st_dev,
            inode=opened.st_ino,
            directories=directories,
        )

    def _open_upload_directories(self, run_id: str) -> _UploadDirectories:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        descriptors: list[int] = []
        try:
            runs_fd = os.open(self.runs_dir, flags)
            descriptors.append(runs_fd)
            run_fd = os.open(run_id, flags, dir_fd=runs_fd)
            descriptors.append(run_fd)
            try:
                uploads_fd = os.open("uploads", flags, dir_fd=run_fd)
            except FileNotFoundError:
                os.mkdir("uploads", mode=0o700, dir_fd=run_fd)
                self._fsync_directory_fd(run_fd)
                uploads_fd = os.open("uploads", flags, dir_fd=run_fd)
            descriptors.append(uploads_fd)
            directories = _UploadDirectories(run_id, runs_fd, run_fd, uploads_fd)
            self._verify_upload_directories(directories)
            return directories
        except BaseException as exc:
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if isinstance(exc, UploadIntegrityError):
                raise
            raise UploadIntegrityError(
                f"upload directory tree could not be opened safely for run {run_id!r}"
            ) from exc

    def _verify_upload_directories(self, directories: _UploadDirectories) -> None:
        try:
            checks = (
                (
                    os.fstat(directories.runs_fd),
                    self.runs_dir.lstat(),
                    "runs directory",
                ),
                (
                    os.fstat(directories.run_fd),
                    os.stat(
                        directories.run_id,
                        dir_fd=directories.runs_fd,
                        follow_symlinks=False,
                    ),
                    "run directory",
                ),
                (
                    os.fstat(directories.uploads_fd),
                    os.stat(
                        "uploads",
                        dir_fd=directories.run_fd,
                        follow_symlinks=False,
                    ),
                    "uploads directory",
                ),
            )
        except OSError as exc:
            raise UploadIntegrityError(
                "upload directory tree became unavailable"
            ) from exc
        for opened, current, label in checks:
            if (
                not stat.S_ISDIR(opened.st_mode)
                or not stat.S_ISDIR(current.st_mode)
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            ):
                raise UploadIntegrityError(f"{label} identity changed during upload")

    def _require_upload_path_tree(
        self,
        run_id: str,
        *,
        require_uploads: bool = True,
    ) -> None:
        self._require_plain_directory(self.runs_dir, "runs directory")
        self._require_plain_directory(self.runs_dir / run_id, "run directory")
        if require_uploads:
            self._require_plain_directory(
                self.runs_dir / run_id / "uploads",
                "uploads directory",
            )

    @staticmethod
    def _entry_exists(
        path: Path,
        *,
        directory_fd: int | None,
        entry_name: str,
    ) -> bool:
        if directory_fd is None:
            return WebRunServices._path_entry_exists(path)
        try:
            os.stat(entry_name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise UploadIntegrityError(f"path cannot be inspected safely: {path}") from exc
        return True

    @staticmethod
    def _path_entry_exists(path: Path) -> bool:
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise UploadIntegrityError(f"path cannot be inspected safely: {path}") from exc
        return True

    @staticmethod
    def _require_file_entry_identity(
        path: Path,
        *,
        device: int,
        inode: int,
        link_count: int,
    ) -> None:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise UploadIntegrityError(f"upload file is unavailable: {path}") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != link_count
            or (metadata.st_dev, metadata.st_ino) != (device, inode)
        ):
            raise UploadIntegrityError(f"upload file identity changed: {path}")

    @staticmethod
    def _require_file_entry_identity_at(
        entry_name: str,
        *,
        directory_fd: int,
        display_path: Path,
        device: int,
        inode: int,
        link_count: int,
    ) -> None:
        try:
            metadata = os.stat(
                entry_name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise UploadIntegrityError(
                f"upload file is unavailable: {display_path}"
            ) from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != link_count
            or (metadata.st_dev, metadata.st_ino) != (device, inode)
        ):
            raise UploadIntegrityError(
                f"upload file identity changed: {display_path}"
            )

    @staticmethod
    def _fsync_directory_fd(descriptor: int) -> None:
        unsupported = {
            errno.EINVAL,
            getattr(errno, "ENOTSUP", errno.EINVAL),
            getattr(errno, "EOPNOTSUPP", errno.EINVAL),
        }
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in unsupported:
                raise

    @staticmethod
    def _fsync_upload_directory(directory: Path) -> None:
        if os.name == "nt":
            return
        unsupported = {
            errno.EINVAL,
            getattr(errno, "ENOTSUP", errno.EINVAL),
            getattr(errno, "EOPNOTSUPP", errno.EINVAL),
        }
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            descriptor = os.open(directory, flags)
        except OSError as exc:
            if exc.errno in unsupported:
                return
            raise
        try:
            WebRunServices._fsync_directory_fd(descriptor)
        finally:
            os.close(descriptor)

    def _verify_completed_slots(self, context: _RunContext) -> None:
        if set(context.completed) != set(context.slots):
            raise UploadIntegrityError("not every declared input slot is complete")
        if not context.slots:
            return
        run_id = context.reservation.run_id
        uploads_dir = self.runs_dir / run_id / "uploads"
        directories = self._open_upload_directories(run_id) if _HAS_DIRECTORY_FD else None
        try:
            if directories is None:
                self._require_upload_path_tree(run_id)
            for name, slot in context.slots.items():
                expected_path = uploads_dir / name
                if context.completed[name] != expected_path:
                    raise UploadIntegrityError(f"upload slot {name!r} has an aliased path")
                size, digest = self._regular_file_identity(
                    expected_path,
                    directory_fd=(
                        directories.uploads_fd if directories is not None else None
                    ),
                    entry_name=name,
                )
                if size != slot.expected_size or digest != slot.expected_sha256:
                    raise UploadIntegrityError(
                        f"upload slot {name!r} changed after validation"
                    )
                if directories is not None:
                    self._verify_upload_directories(directories)
                else:
                    self._require_upload_path_tree(run_id)
        finally:
            if directories is not None:
                directories.close()

    @staticmethod
    def _require_plain_directory(path: Path, label: str) -> None:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise UploadIntegrityError(f"{label} is unavailable: {path}") from exc
        if not stat.S_ISDIR(metadata.st_mode):
            raise UploadIntegrityError(f"{label} is not a plain directory: {path}")

    @staticmethod
    def _regular_file_identity(
        path: Path,
        *,
        directory_fd: int | None = None,
        entry_name: str | None = None,
    ) -> tuple[int, str]:
        opened_name: str | Path = (
            path if directory_fd is None else (entry_name or path.name)
        )
        try:
            if directory_fd is None:
                before = path.lstat()
            else:
                before = os.stat(
                    opened_name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
        except OSError as exc:
            raise UploadIntegrityError(f"upload file is unavailable: {path}") from exc
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise UploadIntegrityError(f"upload file is not one owned regular file: {path}")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            if directory_fd is None:
                descriptor = os.open(opened_name, flags)
            else:
                descriptor = os.open(opened_name, flags, dir_fd=directory_fd)
        except OSError as exc:
            raise UploadIntegrityError(f"upload file cannot be opened safely: {path}") from exc
        digest = hashlib.sha256()
        try:
            handle = os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise
        with handle:
            opened = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            ):
                raise UploadIntegrityError(
                    f"upload file changed during secure open: {path}"
                )
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
            after = os.fstat(handle.fileno())
            if (
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
            ):
                raise UploadIntegrityError(f"upload file changed while hashing: {path}")
            return after.st_size, digest.hexdigest()

    @staticmethod
    def _validated_slots(input_slots: tuple[InputSlot, ...]) -> dict[str, InputSlot]:
        if not isinstance(input_slots, tuple):
            raise InvalidInputSlot("input_slots must be a tuple")
        slots: dict[str, InputSlot] = {}
        for slot in input_slots:
            if not isinstance(slot, InputSlot):
                raise InvalidInputSlot("every input slot must be an InputSlot")
            try:
                validate_run_id(slot.name)
            except RunControlError as exc:
                raise InvalidInputSlot(f"unsafe input slot name: {slot.name!r}") from exc
            if slot.name.endswith(".partial"):
                raise InvalidInputSlot("input slot names may not end in .partial")
            if not _SHA256_PATTERN.fullmatch(slot.expected_sha256):
                raise InvalidInputSlot(
                    f"input slot {slot.name!r} needs a lowercase SHA-256 digest"
                )
            if type(slot.expected_size) is not int or slot.expected_size < 0:
                raise InvalidInputSlot(
                    f"input slot {slot.name!r} needs a non-negative integer size"
                )
            if slot.name in slots:
                raise InvalidInputSlot(f"duplicate input slot name: {slot.name!r}")
            slots[slot.name] = slot
        return slots

    @staticmethod
    async def _next_chunk_or_abort(
        run_id: str,
        iterator: AsyncIterator[bytes],
        abort_event: asyncio.Event,
    ) -> bytes:
        next_chunk = asyncio.create_task(anext(iterator))
        aborted = asyncio.create_task(abort_event.wait())
        try:
            done, _ = await asyncio.wait(
                (next_chunk, aborted),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if aborted in done and abort_event.is_set():
                next_chunk.cancel()
                await asyncio.gather(next_chunk, return_exceptions=True)
                raise UploadCancelled(f"run {run_id!r} was cancelled during upload")
            aborted.cancel()
            await asyncio.gather(aborted, return_exceptions=True)
            return await next_chunk
        except BaseException:
            for task in (next_chunk, aborted):
                if not task.done():
                    task.cancel()
            await asyncio.gather(next_chunk, aborted, return_exceptions=True)
            raise
