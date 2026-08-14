"""Async ownership of one durable, cancellable worker process per run."""

from __future__ import annotations

import asyncio
import codecs
from contextvars import ContextVar
from dataclasses import dataclass, replace
import inspect
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from typing import Any, Callable, Literal, Sequence
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid4, uuid5

from .process_supervision import (
    ProcessIdentity,
    ProcessLedger,
    ProcessLedgerError,
    WindowsJob,
    owned_processes_are_quiescent,
    process_identity,
    process_is_alive,
    resume_windows_process,
    terminate_process_identities,
)
from .run_control import (
    InvalidRunTransition,
    RunControlError,
    RunControlRecord,
    RunControlStore,
    validate_terminal_reconciliation_metadata,
)
from .run_worker_protocol import (
    ProtocolError,
    RunWorkerRequest,
    decode_worker_result,
    encode_request,
    format_worker_error_message,
    parse_worker_result_json,
    sensitive_values,
)
from .util.logging import RedactingLogWriter, append_jsonl_event, read_jsonl_events


_WORKER_EXIT_VERSION = 1
_WORKER_EXIT_TAIL_CHARS = 2_048
_WORKER_EXIT_DETAIL_CHARS = 1_200
_WORKER_EXIT_TEXT_CHARS = 240


@dataclass(frozen=True)
class WorkerExitDiagnostic:
    version: int
    returncode: int
    error_code: str
    error_message: str
    error_detail: str
    protocol_error: str
    last_event: str | None = None
    last_worker_seq: int | None = None
    last_phase: str | None = None
    last_reason: str | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""

    def event_payload(self, *, run_id: str, job_kind: str) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "event": "worker.exit",
            "job_kind": job_kind,
            "version": self.version,
            "returncode": self.returncode,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "error_detail": self.error_detail,
            "protocol_error": self.protocol_error,
            "last_event": self.last_event,
            "last_worker_seq": self.last_worker_seq,
            "last_phase": self.last_phase,
            "last_reason": self.last_reason,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
        }


def _immutable_pointer_cleanup_warnings(raw: Any) -> tuple[str, ...]:
    if type(raw) not in {list, tuple} or not all(
        type(warning) is str for warning in raw
    ):
        return ()
    return tuple(raw)


@dataclass(frozen=True)
class WorkerOutcome:
    run_id: str
    job_kind: str
    returncode: int
    ok: bool
    result: dict[str, Any] | None
    error: str | None
    relayed_events: int
    failure_phase: str | None = None
    pointer_cleanup_warnings: tuple[str, ...] = ()
    exit_diagnostic: WorkerExitDiagnostic | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "pointer_cleanup_warnings",
            _immutable_pointer_cleanup_warnings(
                self.pointer_cleanup_warnings
            ),
        )


@dataclass
class SupervisedRun:
    run_id: str
    process: asyncio.subprocess.Process
    monitor_task: asyncio.Task[WorkerOutcome]
    started_at: float
    stdout_path: Path
    stderr_path: Path
    command: tuple[str, ...] = ()


@dataclass(frozen=True)
class CancellationOutcome:
    run_id: str
    state: str
    terminated_pids: tuple[int, ...]
    surviving_pids: tuple[int, ...]
    already_terminal: bool
    quiesced: bool = True


@dataclass(frozen=True)
class TerminalReconciliation:
    """One terminal-publication step passed to an idempotent callback.

    ``commit`` can be replayed after a crash; implementations must therefore
    converge when invoked more than once for the same terminal decision.
    """

    run_id: str
    decision: Literal["accept", "reject"]
    phase: Literal["preflight", "commit"]
    terminal_state: Literal["completed", "failed", "cancelled"]
    record: RunControlRecord


@dataclass(frozen=True)
class _TerminalReconciliationState:
    run_id: str
    decision: Literal["accept", "reject"] | None
    phase: Literal["preflight", "commit"] | None
    terminal_state: Literal["completed", "failed", "cancelled"] | None
    status: Literal["pending", "succeeded", "invalid"]
    diagnostic: str | None = None


class _TerminalReconciliationPending(RuntimeError):
    def __init__(self, diagnostic: str) -> None:
        self.reconciliation_diagnostic = diagnostic
        super().__init__(diagnostic)


class _TerminalReconciliationReentry(RuntimeError):
    reconciliation_diagnostic = "terminal_reconciliation_reentrant"


_EventSink = Callable[[dict[str, Any]], Any]
_TerminalReconciler = Callable[[TerminalReconciliation], Any]
_CancellationQuiescer = Callable[[str], Any]
_WORKER_ENV_SOURCE = dict(os.environ)
_ACTIVE_TERMINAL_RECONCILIATIONS: ContextVar[
    frozenset[tuple[str, str]]
] = ContextVar("active_terminal_reconciliations", default=frozenset())


class _RedactedTailBuffer:
    """Keep a true bounded tail while preserving split-secret redaction."""

    def __init__(self, secrets: tuple[str, ...], *, max_chars: int) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._secrets = tuple(
            sorted({value for value in secrets if value}, key=len, reverse=True)
        )
        self._max_secret_length = max(
            (len(value) for value in self._secrets),
            default=1,
        )
        self._pending = ""
        self._tail = ""
        self._max_chars = max(256, int(max_chars))

    def feed(self, chunk: bytes) -> None:
        self._pending += self._decoder.decode(chunk)
        self._flush_complete_prefix()

    def close(self) -> str:
        self._pending += self._decoder.decode(b"", final=True)
        self._append_redacted(self._pending)
        self._pending = ""
        return self._tail

    def _flush_complete_prefix(self) -> None:
        keep = self._max_secret_length
        emit_end = max(0, len(self._pending) - keep)
        changed = True
        while changed and emit_end:
            changed = False
            for secret in self._secrets:
                start = self._pending.rfind(
                    secret,
                    0,
                    emit_end + len(secret),
                )
                if 0 <= start < emit_end < start + len(secret):
                    emit_end = start
                    changed = True
        if emit_end:
            self._append_redacted(self._pending[:emit_end])
            self._pending = self._pending[emit_end:]

    def _append_redacted(self, text: str) -> None:
        for secret in self._secrets:
            text = text.replace(secret, "[REDACTED]")
        self._tail = (self._tail + text)[-self._max_chars:]


@dataclass(frozen=True)
class _WorkerResultRead:
    result: dict[str, Any] | None
    error: str | None
    failure_phase: str | None
    pointer_cleanup_warnings: tuple[str, ...]
    protocol_error_code: str | None = None


def _bounded_safe_text(
    value: Any,
    *,
    secrets: tuple[str, ...],
    internal_paths: tuple[str, ...],
    max_chars: int,
) -> str:
    text = str(value or "")
    for secret in sorted({item for item in secrets if item}, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    for path in sorted({item for item in internal_paths if item}, key=len, reverse=True):
        text = text.replace(path, "[internal-path]")
    text = re.sub(
        r"(?i)(authorization:\s*bearer\s+)\S+",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)(api[_-]?key\s*[=:]\s*)\S+",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", text)
    return text[-max(1, int(max_chars)):]


def _last_worker_event_projection(
    path: Path,
    *,
    secrets: tuple[str, ...],
    internal_paths: tuple[str, ...],
) -> tuple[str | None, int | None, str | None, str | None]:
    candidates = [
        event
        for event in read_jsonl_events(path)
        if isinstance(event.get("event"), str)
        and event.get("event") not in {
            "worker.exit",
            "run.done",
            "run.error",
            "run.cancelled",
        }
        and type(event.get("worker_seq")) is int
    ]
    if not candidates:
        return None, None, None, None
    event = max(candidates, key=lambda item: int(item["worker_seq"]))

    def safe_field(name: str) -> str | None:
        value = event.get(name)
        if not isinstance(value, str) or not value:
            return None
        return _bounded_safe_text(
            value,
            secrets=secrets,
            internal_paths=internal_paths,
            max_chars=_WORKER_EXIT_TEXT_CHARS,
        )

    return (
        safe_field("event"),
        int(event["worker_seq"]),
        safe_field("phase"),
        safe_field("reason"),
    )


def _build_worker_exit_diagnostic(
    *,
    returncode: int,
    protocol_error_code: str,
    protocol_error: str,
    stdout_tail: str,
    stderr_tail: str,
    run_events_path: Path,
    secrets: tuple[str, ...],
    internal_paths: tuple[str, ...],
) -> WorkerExitDiagnostic:
    safe_protocol_error = _bounded_safe_text(
        protocol_error,
        secrets=secrets,
        internal_paths=internal_paths,
        max_chars=_WORKER_EXIT_TEXT_CHARS,
    )
    safe_stdout = _bounded_safe_text(
        stdout_tail,
        secrets=secrets,
        internal_paths=internal_paths,
        max_chars=_WORKER_EXIT_TAIL_CHARS,
    )
    safe_stderr = _bounded_safe_text(
        stderr_tail,
        secrets=secrets,
        internal_paths=internal_paths,
        max_chars=_WORKER_EXIT_TAIL_CHARS,
    )
    last_event, last_worker_seq, last_phase, last_reason = (
        _last_worker_event_projection(
            run_events_path,
            secrets=secrets,
            internal_paths=internal_paths,
        )
    )
    if protocol_error_code == "worker_result_missing":
        error_message = (
            "The worker exited before writing its result. Review diagnostics "
            "before retrying."
        )
    elif protocol_error_code == "worker_exit_contradiction":
        error_message = (
            "The worker exit status contradicted its result record. Review "
            "diagnostics before retrying."
        )
    else:
        error_message = (
            "The worker produced an invalid result record. Review diagnostics "
            "before retrying."
        )
    detail_parts = [
        f"Worker exit status: {returncode}.",
        f"Result protocol: {safe_protocol_error}.",
    ]
    if last_event:
        event_summary = f"Last event: {last_event}"
        if last_phase:
            event_summary += f"; phase={last_phase}"
        if last_reason:
            event_summary += f"; reason={last_reason}"
        detail_parts.append(event_summary + ".")
    if safe_stdout:
        detail_parts.append(f"stdout tail: {safe_stdout[-320:]}")
    if safe_stderr:
        detail_parts.append(f"stderr tail: {safe_stderr[-480:]}")
    error_detail = _bounded_safe_text(
        "\n".join(detail_parts),
        secrets=secrets,
        internal_paths=internal_paths,
        max_chars=_WORKER_EXIT_DETAIL_CHARS,
    )
    return WorkerExitDiagnostic(
        version=_WORKER_EXIT_VERSION,
        returncode=returncode,
        error_code=protocol_error_code,
        error_message=error_message,
        error_detail=error_detail,
        protocol_error=safe_protocol_error,
        last_event=last_event,
        last_worker_seq=last_worker_seq,
        last_phase=last_phase,
        last_reason=last_reason,
        stdout_tail=safe_stdout,
        stderr_tail=safe_stderr,
    )


def _proxy_has_credentials(value: str) -> bool:
    candidate = value.strip()
    parsed = urlsplit(candidate if "://" in candidate else f"//{candidate}")
    return parsed.username is not None or parsed.password is not None


def _terminal_decision(record: RunControlRecord) -> Literal["accept", "reject"]:
    if record.state == "completed" and record.publishable:
        return "accept"
    return "reject"


def _recorded_worker_identity(record: RunControlRecord) -> ProcessIdentity | None:
    if (
        not isinstance(record.worker_pid, int)
        or record.worker_pid <= 0
        or not isinstance(record.worker_birth_id, str)
        or not record.worker_birth_id
    ):
        return None
    return ProcessIdentity(
        pid=record.worker_pid,
        birth_id=record.worker_birth_id,
        process_group_id=record.worker_pgid,
        parent_pid=None,
    )


def _merge_recorded_worker_ownership(
    record: RunControlRecord,
    identities: Sequence[ProcessIdentity],
    owner_nonces: Sequence[str],
) -> tuple[tuple[ProcessIdentity, ...], tuple[str, ...], bool]:
    merged = {(identity.pid, identity.birth_id): identity for identity in identities}
    recorded = _recorded_worker_identity(record)
    missing_identity = record.worker_pid is not None and recorded is None
    if recorded is not None:
        merged.setdefault((recorded.pid, recorded.birth_id), recorded)
    nonces = {str(value) for value in owner_nonces if str(value)}
    if record.worker_spawn_nonce:
        nonces.add(record.worker_spawn_nonce)
    return tuple(merged.values()), tuple(sorted(nonces)), missing_identity


class RunSupervisor:
    def __init__(
        self,
        runs_dir: str | Path,
        *,
        control_store: RunControlStore | None = None,
        worker_command: Sequence[str] | None = None,
        event_sink: _EventSink | None = None,
        grace_s: float = 0.75,
        log_max_bytes: int = 2 * 1024 * 1024,
        root_registration_delay_s: float = 0.0,
        root_registration_hook: Callable[[ProcessIdentity], None] | None = None,
        terminal_reconciler: _TerminalReconciler | None = None,
        cancellation_quiescer: _CancellationQuiescer | None = None,
    ) -> None:
        self.runs_dir = Path(runs_dir)
        self.control_store = control_store or RunControlStore(self.runs_dir)
        self._worker_command = tuple(worker_command) if worker_command is not None else None
        self._event_sink = event_sink
        self._grace_s = max(0.0, float(grace_s))
        self._log_max_bytes = max(1024, int(log_max_bytes))
        self._root_registration_delay_s = max(0.0, float(root_registration_delay_s))
        self._root_registration_hook = root_registration_hook
        self._terminal_reconciler = terminal_reconciler
        self._cancellation_quiescer = cancellation_quiescer
        self._active: dict[str, SupervisedRun] = {}
        self._request_kinds: dict[str, str] = {}
        self._operation_locks: dict[str, asyncio.Lock] = {}
        self._event_relay_locks: dict[str, asyncio.Lock] = {}
        self._windows_jobs: dict[str, WindowsJob] = {}
        self._broadcasted_event_ids: set[str] = set()

    def active_run_ids(self) -> tuple[str, ...]:
        return tuple(self._active)

    def is_durably_quiescent(self, run_id: str) -> bool:
        """Synchronously prove process quiescence from durable ownership state."""
        try:
            record = self.control_store.read(run_id)
        except (OSError, RunControlError, ValueError):
            return False
        active = self._active.get(run_id)
        if active is not None and active.process.returncode is None:
            return False
        ledger = ProcessLedger(self.runs_dir / run_id)
        if not ledger.path.is_file():
            identities: tuple[ProcessIdentity, ...] = ()
            owner_nonces: tuple[str, ...] = ()
            identities, owner_nonces, missing_identity = (
                _merge_recorded_worker_ownership(
                    record,
                    identities,
                    owner_nonces,
                )
            )
            return not missing_identity and owned_processes_are_quiescent(
                identities,
                owner_nonces=owner_nonces,
            )
        try:
            snapshot = ledger.read()
        except (OSError, ProcessLedgerError, ValueError):
            return False
        if any(intent.status == "spawning" for intent in snapshot.spawning):
            return False
        identities = tuple(item.identity for item in snapshot.processes)
        identities, owner_nonces, missing_identity = (
            _merge_recorded_worker_ownership(
                record,
                identities,
                tuple(item.nonce for item in snapshot.processes),
            )
        )
        if missing_identity:
            return False
        return owned_processes_are_quiescent(
            identities,
            owner_nonces=owner_nonces,
        )

    async def _quiesce_terminal_process_ownership(
        self,
        run_id: str,
        record: RunControlRecord,
    ) -> tuple[tuple[int, ...], tuple[int, ...], bool]:
        ledger = ProcessLedger(self.runs_dir / run_id)
        try:
            await asyncio.to_thread(
                ledger.reconcile_abandoned_spawns,
                grace_s=self._grace_s,
            )
            with ledger.exclusive():
                snapshot = ledger.seal_unlocked()
                unresolved = tuple(
                    intent.nonce
                    for intent in snapshot.spawning
                    if intent.status == "spawning"
                )
                identities = tuple(item.identity for item in snapshot.processes)
                owner_nonces = tuple(item.nonce for item in snapshot.processes)
            identities, owner_nonces, missing_identity = (
                _merge_recorded_worker_ownership(
                    record,
                    identities,
                    owner_nonces,
                )
            )
            if missing_identity:
                return (), (record.worker_pid,), False
            windows_job = self._windows_jobs.get(run_id)
            if windows_job is not None:
                try:
                    windows_job.terminate()
                except OSError:
                    pass
            report = await asyncio.to_thread(
                terminate_process_identities,
                identities,
                root_pid=record.worker_pid,
                grace_s=self._grace_s,
                unresolved_spawns=unresolved,
                owner_nonces=owner_nonces,
            )
        except (OSError, ProcessLedgerError, ValueError):
            survivors = () if record.worker_pid is None else (record.worker_pid,)
            return (), survivors, False
        survivors = tuple({
            identity.pid: identity
            for identity in (*report.survivors, *report.stale_identities)
        }.values())
        if report.unresolved_spawns or survivors:
            return (
                tuple(sorted(identity.pid for identity in report.terminated)),
                tuple(sorted(identity.pid for identity in survivors)),
                False,
            )
        supervised = self._active.get(run_id)
        if supervised is not None and supervised.process.returncode is None:
            try:
                await asyncio.wait_for(
                    supervised.process.wait(),
                    timeout=max(1.0, self._grace_s + 0.75),
                )
            except asyncio.TimeoutError:
                return (
                    tuple(sorted(identity.pid for identity in report.terminated)),
                    (supervised.process.pid,),
                    False,
                )
        quiesced = self.is_durably_quiescent(run_id)
        return (
            tuple(sorted(identity.pid for identity in report.terminated)),
            (),
            quiesced,
        )

    async def start(self, request: RunWorkerRequest) -> SupervisedRun:
        run_id = request.run_id
        self._assert_not_reentrant_terminal_operation(run_id)
        operation_lock = self._operation_locks.setdefault(run_id, asyncio.Lock())
        async with operation_lock:
            existing = self._active.get(run_id)
            if existing is not None and existing.process.returncode is None:
                return existing
            run_dir = self.runs_dir / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            ledger = ProcessLedger(run_dir)
            nonce = uuid4().hex
            process: asyncio.subprocess.Process | None = None
            identity: ProcessIdentity | None = None
            command: tuple[str, ...] = ()
            windows_job: WindowsJob | None = None
            cancelled_before_release = False
            async with ledger.async_exclusive(timeout_s=5.0):
                record = self.control_store.read(run_id)
                if record.state in {"cancelling", "cancelled", "completed", "failed", "completing"}:
                    raise InvalidRunTransition(
                        f"run {run_id!r} cannot start from {record.state!r}"
                    )
                if record.state in {"reserved", "uploading"}:
                    record = self.control_store.transition(run_id, record, "queued")
                if record.state != "queued":
                    raise InvalidRunTransition(f"run {run_id!r} is not queued")
                ledger.begin_spawn_unlocked("root-worker", nonce)
                try:
                    if self._root_registration_delay_s:
                        await asyncio.sleep(self._root_registration_delay_s)
                    base_command = self._worker_command or (
                        sys.executable, "-m", "autodesign.run_worker",
                    )
                    command = (*base_command, "--run-id", run_id, "--spawn-nonce", nonce)
                    creationflags = 0
                    if os.name == "nt":
                        creationflags = (
                            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                            | getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
                        )
                    process = await asyncio.wait_for(
                        asyncio.create_subprocess_exec(
                            *command,
                            stdin=asyncio.subprocess.PIPE,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            cwd=str(
                                request.settings.repo_root
                                if hasattr(request, "settings")
                                else Path(__file__).resolve().parents[1]
                            ),
                            env=build_worker_environment(owner_nonce=nonce),
                            start_new_session=(os.name == "posix"),
                            creationflags=creationflags,
                        ),
                        timeout=5.0,
                    )
                    ledger.record_shim_pid_unlocked(nonce, process.pid)
                    identity = process_identity(process.pid)
                    ledger.record_shim_unlocked(nonce, identity)
                    if os.name == "nt":
                        windows_job = WindowsJob()
                        windows_job.assign(process.pid)
                    if self._root_registration_hook is not None:
                        self._root_registration_hook(identity)
                    ledger.register_unlocked(
                        identity, role="root-worker", nonce=nonce,
                    )
                    try:
                        self.control_store.transition(
                            run_id,
                            record,
                            "running",
                            worker_pid=identity.pid,
                            worker_pgid=identity.process_group_id,
                            worker_birth_id=identity.birth_id,
                            worker_spawn_nonce=nonce,
                        )
                    except InvalidRunTransition:
                        if self.control_store.read(run_id).state != "cancelling":
                            raise
                        cancelled_before_release = True
                    if windows_job is not None:
                        self._windows_jobs[run_id] = windows_job
                        resume_windows_process(process.pid)
                    if process.stdin is None:
                        raise RuntimeError("worker stdin release pipe was not created")
                    if not cancelled_before_release:
                        process.stdin.write(encode_request(request))
                        await asyncio.wait_for(process.stdin.drain(), timeout=5.0)
                    process.stdin.close()
                    try:
                        await asyncio.wait_for(process.stdin.wait_closed(), timeout=5.0)
                    except (AttributeError, BrokenPipeError, ConnectionResetError):
                        pass
                except BaseException as exc:
                    ledger.fail_spawn_unlocked(nonce, f"{type(exc).__name__}: {exc}")
                    if process is not None:
                        await _kill_unreleased_worker(process)
                    if windows_job is not None:
                        windows_job.close()
                        self._windows_jobs.pop(run_id, None)
                    current = self.control_store.read(run_id)
                    if current.state in {"queued", "running"}:
                        self.control_store.transition(
                            run_id, current, "failed", cancellation_pending="worker_spawn_failed",
                        )
                    raise

            stdout_path = run_dir / "worker_stdout.log"
            stderr_path = run_dir / "worker_stderr.log"
            secrets = sensitive_values(request)
            stdout_task = asyncio.create_task(self._drain(process.stdout, stdout_path, secrets))
            stderr_task = asyncio.create_task(self._drain(process.stderr, stderr_path, secrets))
            relay_task = asyncio.create_task(self._relay_worker_events(run_id, process))
            monitor = asyncio.create_task(
                self._monitor(
                    request,
                    process,
                    stdout_task=stdout_task,
                    stderr_task=stderr_task,
                    relay_task=relay_task,
                )
            )
            supervised = SupervisedRun(
                run_id=run_id,
                process=process,
                monitor_task=monitor,
                started_at=time.time(),
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                command=command,
            )
            self._active[run_id] = supervised
            self._request_kinds[run_id] = request.job_kind
            return supervised

    async def wait(self, run_id: str) -> WorkerOutcome:
        supervised = self._active.get(run_id)
        if supervised is None:
            raise KeyError(f"run is not supervised in this process: {run_id}")
        return await asyncio.shield(supervised.monitor_task)

    async def cancel(self, run_id: str, reason: str) -> CancellationOutcome:
        self._assert_not_reentrant_terminal_operation(run_id)
        relay_lock = self._event_relay_locks.setdefault(run_id, asyncio.Lock())
        async with relay_lock:
            self.control_store.request_cancel(run_id)
        operation_lock = self._operation_locks.setdefault(run_id, asyncio.Lock())
        terminal_event: dict[str, Any] | None = None
        outcome: CancellationOutcome | None = None
        async with operation_lock:
            current = self.control_store.read(run_id)
            if current.state in {"completed", "failed", "cancelled"}:
                terminated_pids, surviving_pids, process_quiesced = (
                    await self._quiesce_terminal_process_ownership(run_id, current)
                )
                if not process_quiesced:
                    return CancellationOutcome(
                        run_id,
                        current.state,
                        terminated_pids,
                        surviving_pids,
                        True,
                        False,
                    )
                if not await self._quiesce_cancellation_owner(run_id):
                    return CancellationOutcome(
                        run_id,
                        current.state,
                        terminated_pids,
                        surviving_pids,
                        True,
                        False,
                    )
                try:
                    if current.state != "cancelled":
                        await self._reconcile_terminal(
                            run_id,
                            decision=_terminal_decision(current),
                            phase="commit",
                            terminal_state=current.state,
                            record=current,
                        )
                    terminal_event = self._append_accepted_terminal_event(
                        run_id,
                        current,
                        reason=reason,
                    )
                finally:
                    self._release_active_run(run_id)
                outcome = CancellationOutcome(
                    run_id,
                    current.state,
                    terminated_pids,
                    (),
                    True,
                )
            else:
                ledger = ProcessLedger(self.runs_dir / run_id)
                await asyncio.to_thread(
                    ledger.reconcile_abandoned_spawns, grace_s=self._grace_s,
                )
                with ledger.exclusive():
                    current = self.control_store.read(run_id)
                    snapshot = ledger.seal_unlocked()
                    unresolved = tuple(
                        intent.nonce for intent in snapshot.spawning if intent.status == "spawning"
                    )
                    identities = tuple(record.identity for record in snapshot.processes)
                    owner_nonces = tuple(record.nonce for record in snapshot.processes)
                identities, owner_nonces, missing_identity = (
                    _merge_recorded_worker_ownership(
                        current,
                        identities,
                        owner_nonces,
                    )
                )
                if missing_identity:
                    self.control_store.finalize_cancel(
                        run_id,
                        {
                            "termination_verified": False,
                            "cancellation_pending": "worker_identity_unavailable",
                            "reason": reason,
                        },
                    )
                    return CancellationOutcome(
                        run_id,
                        "cancelling",
                        (),
                        (current.worker_pid,),
                        False,
                    )
                windows_job = self._windows_jobs.get(run_id)
                if windows_job is not None:
                    try:
                        windows_job.terminate()
                    except OSError:
                        pass
                report = await asyncio.to_thread(
                    terminate_process_identities,
                    identities,
                    root_pid=current.worker_pid,
                    grace_s=self._grace_s,
                    unresolved_spawns=unresolved,
                    owner_nonces=owner_nonces,
                )
                survivors = tuple({
                    identity.pid: identity
                    for identity in (*report.survivors, *report.stale_identities)
                }.values())
                if report.unresolved_spawns or survivors:
                    pending_parts: list[str] = []
                    if report.unresolved_spawns:
                        pending_parts.append("unresolved_spawn_intent")
                    if report.stale_identities:
                        pending_parts.append("stale_process_identity")
                    if report.survivors:
                        pending_parts.append("managed_process_survived")
                    pending = ",".join(pending_parts) or "managed_process_liveness_unverified"
                    self.control_store.finalize_cancel(
                        run_id,
                        {
                            "termination_verified": False,
                            "cancellation_pending": pending,
                            "reason": reason,
                        },
                    )
                    return CancellationOutcome(
                        run_id, "cancelling",
                        tuple(sorted(identity.pid for identity in report.terminated)),
                        tuple(sorted(identity.pid for identity in survivors)),
                        False,
                    )

                supervised = self._active.get(run_id)
                if supervised is not None:
                    try:
                        await asyncio.wait_for(supervised.process.wait(), timeout=max(1.0, self._grace_s + 0.75))
                    except asyncio.TimeoutError:
                        self.control_store.finalize_cancel(
                            run_id,
                            {
                                "termination_verified": False,
                                "cancellation_pending": "worker_monitor_not_joined",
                                "reason": reason,
                            },
                        )
                        return CancellationOutcome(run_id, "cancelling", (), (supervised.process.pid,), False)
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(supervised.monitor_task),
                            timeout=max(0.05, self._grace_s),
                        )
                    except asyncio.TimeoutError:
                        supervised.monitor_task.cancel()
                        try:
                            await asyncio.wait_for(
                                supervised.monitor_task,
                                timeout=max(0.05, self._grace_s),
                            )
                        except asyncio.CancelledError:
                            current_task = asyncio.current_task()
                            if current_task is not None and current_task.cancelling():
                                raise
                        except asyncio.TimeoutError:
                            self.control_store.finalize_cancel(
                                run_id,
                                {
                                    "termination_verified": False,
                                    "cancellation_pending": "worker_monitor_not_joined",
                                    "reason": reason,
                                },
                            )
                            return CancellationOutcome(
                                run_id,
                                "cancelling",
                                (),
                                (supervised.process.pid,),
                                False,
                            )
                        except Exception:
                            pass
                    except asyncio.CancelledError:
                        if not supervised.monitor_task.cancelled():
                            raise
                    except Exception:
                        # The worker is already dead and cancellation still owns
                        # the terminal state. A failed diagnostic/event drain must
                        # not strand the run in cancelling.
                        pass
                final_survivors = tuple(identity for identity in identities if process_is_alive(identity))
                if final_survivors:
                    self.control_store.finalize_cancel(
                        run_id,
                        {
                            "termination_verified": False,
                            "cancellation_pending": "managed_process_liveness_unverified",
                            "reason": reason,
                        },
                    )
                    return CancellationOutcome(
                        run_id, "cancelling",
                        tuple(sorted(identity.pid for identity in report.terminated)),
                        tuple(sorted(identity.pid for identity in final_survivors)),
                        False,
                    )
                current = self.control_store.read(run_id)
                if not await self._quiesce_cancellation_owner(run_id):
                    self.control_store.finalize_cancel(
                        run_id,
                        {
                            "termination_verified": False,
                            "cancellation_pending": "web_completion_monitor_not_joined",
                            "reason": reason,
                        },
                    )
                    return CancellationOutcome(
                        run_id,
                        "cancelling",
                        tuple(sorted(identity.pid for identity in report.terminated)),
                        (),
                        False,
                    )
                self._supersede_terminal_reconciliation_for_cancel(run_id)
                try:
                    await self._reconcile_terminal(
                        run_id,
                        decision="reject",
                        phase="commit",
                        terminal_state="cancelled",
                        record=current,
                    )
                except Exception as exc:
                    pending = getattr(
                        exc,
                        "reconciliation_diagnostic",
                        "terminal_reconciliation_failed",
                    )
                    self.control_store.finalize_cancel(
                        run_id,
                        {
                            "termination_verified": False,
                            "cancellation_pending": pending,
                            "reason": reason,
                        },
                    )
                    return CancellationOutcome(
                        run_id,
                        "cancelling",
                        tuple(sorted(identity.pid for identity in report.terminated)),
                        (),
                        False,
                    )
                try:
                    terminal_event_id = str(
                        uuid5(NAMESPACE_URL, f"autodesign:{run_id}:run.cancelled")
                    )
                    cancelled = self.control_store.finalize_cancel(
                        run_id,
                        {
                            "termination_verified": True,
                            "reason": reason,
                            "terminated_pids": sorted(
                                identity.pid for identity in report.terminated
                            ),
                            "accepted_terminal_event_id": terminal_event_id,
                        },
                    )
                    terminal_event = self._append_accepted_terminal_event(
                        run_id,
                        cancelled,
                        reason=reason,
                    )
                finally:
                    self._release_active_run(run_id)
                outcome = CancellationOutcome(
                    run_id,
                    cancelled.state,
                    tuple(sorted(identity.pid for identity in report.terminated)),
                    (),
                    False,
                )
        if terminal_event is not None:
            await self._broadcast_once(terminal_event)
        assert outcome is not None
        return outcome

    async def _quiesce_cancellation_owner(self, run_id: str) -> bool:
        quiescer = self._cancellation_quiescer
        if quiescer is None:
            return True
        try:
            result = quiescer(run_id)
            if inspect.isawaitable(result):
                result = await result
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        return result is True

    async def recover(self, run_id: str) -> SupervisedRun | None:
        self._assert_not_reentrant_terminal_operation(run_id)
        record = self.control_store.read(run_id)
        existing = self._active.get(run_id)
        if (
            existing is not None
            and record.state not in {"completed", "failed", "cancelled"}
            and existing.process.returncode is None
        ):
            return existing
        if existing is not None:
            monitor_task = getattr(existing, "monitor_task", None)
            if monitor_task is not None and not monitor_task.done():
                try:
                    await asyncio.wait_for(
                        asyncio.shield(monitor_task),
                        timeout=max(0.05, self._grace_s),
                    )
                except asyncio.TimeoutError:
                    monitor_task.cancel()
                    try:
                        await asyncio.wait_for(
                            monitor_task,
                            timeout=max(0.05, self._grace_s),
                        )
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass
                except asyncio.CancelledError:
                    if not monitor_task.cancelled():
                        raise
                except Exception:
                    pass
                record = self.control_store.read(run_id)
                if (
                    record.state == "running"
                    and _has_recoverable_worker_result(
                        self.runs_dir / run_id / "worker_result.json",
                        run_id,
                    )
                ):
                    try:
                        record = self.control_store.transition(
                            run_id,
                            record,
                            "completing",
                        )
                    except InvalidRunTransition:
                        record = self.control_store.read(run_id)
            self._release_active_run(run_id)
        if record.state == "cancelling":
            await self.cancel(run_id, "supervisor_restart_recovery")
            return None
        if record.state in {"reserved", "uploading", "queued", "running", "completing"}:
            verified = await self._terminate_recovered_worker(run_id, record)
            if not verified:
                return None
            if record.state == "completing" and _has_recoverable_worker_result(
                self.runs_dir / run_id / "worker_result.json", run_id,
            ):
                # Web owns artifact validation and the final completion CAS.
                return None
            operation_lock = self._operation_locks.setdefault(run_id, asyncio.Lock())
            terminal_event: dict[str, Any] | None = None
            async with operation_lock:
                current = self.control_store.read(run_id)
                if current.state in {
                    "reserved", "uploading", "queued", "running", "completing",
                }:
                    try:
                        current = await self._prepare_terminal_transition(
                            run_id,
                            decision="reject",
                            terminal_state="failed",
                            record=current,
                        )
                        current = self.control_store.transition(
                            run_id, current, "failed", publishable=False,
                            writes_frozen=True,
                            cancellation_pending="supervisor_restart_interrupted",
                            terminal_event="run.error",
                        )
                    except InvalidRunTransition:
                        current = self.control_store.read(run_id)
                        if current.state in {"cancelling", "cancelled"}:
                            self._supersede_terminal_reconciliation_for_cancel(run_id)
                    except Exception:
                        return None
                if current.state in {"completed", "failed", "cancelled"}:
                    try:
                        if current.state != "cancelled":
                            await self._reconcile_terminal(
                                run_id,
                                decision=_terminal_decision(current),
                                phase="commit",
                                terminal_state=current.state,
                                record=current,
                            )
                        terminal_event = self._append_accepted_terminal_event(
                            run_id,
                            current,
                            reason="supervisor_restart_interrupted",
                            recovered=True,
                        )
                    except Exception:
                        terminal_event = None
                    finally:
                        self._release_active_run(run_id)
            if terminal_event is not None:
                await self._broadcast_once(terminal_event)
            return None
        if record.state in {"completed", "failed", "cancelled"}:
            if not await self._terminate_recovered_worker(run_id, record):
                return None
        if record.state in {"completed", "failed", "cancelled"} and record.accepted_terminal_event_id:
            operation_lock = self._operation_locks.setdefault(run_id, asyncio.Lock())
            async with operation_lock:
                record = self.control_store.read(run_id)
                try:
                    if record.state != "cancelled":
                        await self._reconcile_terminal(
                            run_id,
                            decision=_terminal_decision(record),
                            phase="commit",
                            terminal_state=record.state,
                            record=record,
                        )
                    terminal_event = self._append_accepted_terminal_event(
                        run_id,
                        record,
                        recovered=True,
                    )
                except Exception:
                    terminal_event = None
                finally:
                    self._release_active_run(run_id)
            if terminal_event is not None:
                await self._broadcast_once(terminal_event)
        return None

    async def _terminate_recovered_worker(
        self,
        run_id: str,
        record: RunControlRecord,
    ) -> bool:
        ledger = ProcessLedger(self.runs_dir / run_id)
        if any(item.status == "spawning" for item in ledger.read().spawning):
            await asyncio.to_thread(
                ledger.reconcile_abandoned_spawns,
                grace_s=self._grace_s,
            )
        with ledger.exclusive():
            snapshot = ledger.seal_unlocked()
            identities = tuple(item.identity for item in snapshot.processes)
            owner_nonces = tuple(item.nonce for item in snapshot.processes)
            unresolved = tuple(
                item.nonce for item in snapshot.spawning if item.status == "spawning"
            )
        report = await asyncio.to_thread(
            terminate_process_identities,
            identities,
            root_pid=record.worker_pid,
            grace_s=self._grace_s,
            unresolved_spawns=unresolved,
            owner_nonces=owner_nonces,
        )
        return (
            not report.unresolved_spawns
            and not report.survivors
            and not report.stale_identities
            and not any(process_is_alive(identity) for identity in identities)
        )

    async def accept_completion(
        self,
        run_id: str,
        *,
        terminal_state: Literal["completed", "failed"],
        publishable: bool,
        result_digest: str,
        terminal_event_id: str | None = None,
    ) -> RunControlRecord:
        self._assert_not_reentrant_terminal_operation(run_id)
        operation_lock = self._operation_locks.setdefault(run_id, asyncio.Lock())
        terminal_event: dict[str, Any]
        async with operation_lock:
            current = self.control_store.read(run_id)
            if current.state != "completing":
                raise InvalidRunTransition(
                    f"run {run_id!r} cannot accept completion from {current.state!r}"
                )
            accepted_state: Literal["completed", "failed"] = (
                "completed" if terminal_state == "completed" and publishable else "failed"
            )
            decision: Literal["accept", "reject"] = (
                "accept" if accepted_state == "completed" else "reject"
            )
            current = await self._prepare_terminal_transition(
                run_id,
                decision=decision,
                terminal_state=accepted_state,
                record=current,
            )
            event_id = terminal_event_id or str(uuid4())
            terminal_event_name = "run.done" if accepted_state == "completed" else "run.error"
            try:
                accepted = self.control_store.transition(
                    run_id,
                    current,
                    accepted_state,
                    publishable=accepted_state == "completed",
                    result_digest=str(result_digest),
                    accepted_terminal_event_id=event_id,
                    terminal_event=terminal_event_name,
                )
            except InvalidRunTransition:
                authoritative = self.control_store.read(run_id)
                if authoritative.state in {"cancelling", "cancelled"}:
                    self._supersede_terminal_reconciliation_for_cancel(run_id)
                raise
            try:
                await self._reconcile_terminal(
                    run_id,
                    decision=decision,
                    phase="commit",
                    terminal_state=accepted_state,
                    record=accepted,
                )
                terminal_event = self._append_accepted_terminal_event(run_id, accepted)
            finally:
                self._release_active_run(run_id)
        await self._broadcast_once(terminal_event)
        return accepted

    async def _reconcile_terminal(
        self,
        run_id: str,
        *,
        decision: Literal["accept", "reject"],
        phase: Literal["preflight", "commit"],
        terminal_state: Literal["completed", "failed", "cancelled"],
        record: RunControlRecord,
    ) -> None:
        state = self._prepare_terminal_reconciliation(
            run_id,
            decision=decision,
            phase=phase,
            terminal_state=terminal_state,
        )
        if state is None or state.status == "succeeded":
            return
        reconciler = self._terminal_reconciler
        if reconciler is None:
            self._write_terminal_reconciliation(
                state,
                expected=state,
                diagnostic="terminal_reconciler_missing",
            )
            raise _TerminalReconciliationPending("terminal_reconciler_missing")
        key = self._terminal_reconciliation_key(run_id)
        active = _ACTIVE_TERMINAL_RECONCILIATIONS.get()
        token = _ACTIVE_TERMINAL_RECONCILIATIONS.set(active | {key})
        try:
            result = reconciler(TerminalReconciliation(
                run_id=run_id,
                decision=decision,
                phase=phase,
                terminal_state=terminal_state,
                record=record,
            ))
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            self._write_terminal_reconciliation(
                state,
                expected=state,
                diagnostic="terminal_reconciliation_cancelled",
            )
            raise
        except Exception as exc:
            diagnostic = getattr(
                exc,
                "reconciliation_diagnostic",
                "terminal_reconciliation_failed",
            )
            self._write_terminal_reconciliation(
                state,
                expected=state,
                diagnostic=diagnostic,
            )
            raise
        finally:
            _ACTIVE_TERMINAL_RECONCILIATIONS.reset(token)
        self._write_terminal_reconciliation(
            state,
            expected=state,
            status="succeeded",
            diagnostic=None,
        )

    async def _prepare_terminal_transition(
        self,
        run_id: str,
        *,
        decision: Literal["accept", "reject"],
        terminal_state: Literal["completed", "failed"],
        record: RunControlRecord,
    ) -> RunControlRecord:
        existing = self._read_terminal_reconciliation(run_id)
        if (
            existing is not None
            and decision == "reject"
            and record.state in {"running", "completing"}
            and existing.terminal_state in {"completed", "failed"}
            and (
                existing.phase == "preflight"
                or (existing.phase == "commit" and existing.status == "pending")
            )
        ):
            superseded = _TerminalReconciliationState(
                run_id=run_id,
                decision=decision,
                phase="preflight",
                terminal_state=terminal_state,
                status="pending",
            )
            self._write_terminal_reconciliation(
                superseded,
                expected=existing,
            )
            existing = superseded
        if (
            existing is not None
            and existing.status == "pending"
            and existing.phase == "commit"
        ):
            if (
                existing.decision == decision
                and existing.terminal_state == terminal_state
            ):
                return self.control_store.read(run_id)
        await self._reconcile_terminal(
            run_id,
            decision=decision,
            phase="preflight",
            terminal_state=terminal_state,
            record=record,
        )
        self._prepare_terminal_reconciliation(
            run_id,
            decision=decision,
            phase="commit",
            terminal_state=terminal_state,
        )
        return self.control_store.read(run_id)

    def _prepare_terminal_reconciliation(
        self,
        run_id: str,
        *,
        decision: Literal["accept", "reject"],
        phase: Literal["preflight", "commit"],
        terminal_state: Literal["completed", "failed", "cancelled"],
    ) -> _TerminalReconciliationState | None:
        existing = self._read_terminal_reconciliation(run_id)
        if existing is None:
            if self._terminal_reconciler is None:
                return None
            state = _TerminalReconciliationState(
                run_id=run_id,
                decision=decision,
                phase=phase,
                terminal_state=terminal_state,
                status="pending",
            )
            self._write_terminal_reconciliation(state, expected=None)
            return state
        if existing.status == "invalid":
            raise _TerminalReconciliationPending(
                existing.diagnostic or "terminal_reconciliation_state_corrupt",
            )
        same_target = (
            existing.decision == decision
            and existing.terminal_state == terminal_state
        )
        if same_target and existing.phase == phase:
            return existing
        if (
            same_target
            and phase == "commit"
            and existing.phase == "preflight"
            and existing.status == "succeeded"
        ):
            state = _TerminalReconciliationState(
                run_id=run_id,
                decision=decision,
                phase=phase,
                terminal_state=terminal_state,
                status="pending",
            )
            self._write_terminal_reconciliation(state, expected=existing)
            return state
        invalid = _TerminalReconciliationState(
            run_id=run_id,
            decision=None,
            phase=None,
            terminal_state=None,
            status="invalid",
            diagnostic="terminal_reconciliation_state_mismatch",
        )
        self._write_terminal_reconciliation(
            invalid,
            expected=existing,
            diagnostic=invalid.diagnostic,
        )
        raise _TerminalReconciliationPending(invalid.diagnostic or "terminal_reconciliation_state_mismatch")

    def _read_terminal_reconciliation(
        self,
        run_id: str,
    ) -> _TerminalReconciliationState | None:
        for _ in range(4):
            record = self.control_store.read(run_id)
            try:
                return self._terminal_reconciliation_from_record(record)
            except (TypeError, ValueError):
                invalid = _TerminalReconciliationState(
                    run_id=run_id,
                    decision=None,
                    phase=None,
                    terminal_state=None,
                    status="invalid",
                    diagnostic="terminal_reconciliation_state_corrupt",
                )
                try:
                    self.control_store.update_terminal_reconciliation(
                        run_id,
                        record,
                        decision=None,
                        phase=None,
                        terminal_state=None,
                        status="invalid",
                        diagnostic=invalid.diagnostic,
                    )
                    return invalid
                except InvalidRunTransition:
                    continue
        raise _TerminalReconciliationPending(
            "terminal_reconciliation_state_concurrent_update",
        )

    def _supersede_terminal_reconciliation_for_cancel(self, run_id: str) -> None:
        existing = self._read_terminal_reconciliation(run_id)
        if (
            existing is None
            or existing.status != "pending"
            or existing.phase not in {"preflight", "commit"}
            or existing.terminal_state not in {"completed", "failed"}
        ):
            return
        self._write_terminal_reconciliation(_TerminalReconciliationState(
            run_id=run_id,
            decision="reject",
            phase="commit",
            terminal_state="cancelled",
            status="pending",
        ), expected=existing)

    def _write_terminal_reconciliation(
        self,
        state: _TerminalReconciliationState,
        *,
        expected: _TerminalReconciliationState | None,
        status: Literal["pending", "succeeded", "invalid"] | None = None,
        diagnostic: str | None = None,
    ) -> None:
        desired = replace(
            state,
            status=status or state.status,
            diagnostic=diagnostic,
        )
        for _ in range(4):
            current = self.control_store.read(state.run_id)
            try:
                current_state = self._terminal_reconciliation_from_record(current)
            except (TypeError, ValueError) as exc:
                raise _TerminalReconciliationPending(
                    "terminal_reconciliation_state_corrupt",
                ) from exc
            if current_state != expected:
                raise _TerminalReconciliationPending(
                    "terminal_reconciliation_state_concurrent_update",
                )
            try:
                self.control_store.update_terminal_reconciliation(
                    state.run_id,
                    current,
                    decision=desired.decision,
                    phase=desired.phase,
                    terminal_state=desired.terminal_state,
                    status=desired.status,
                    diagnostic=desired.diagnostic,
                )
                return
            except InvalidRunTransition:
                continue
        raise _TerminalReconciliationPending(
            "terminal_reconciliation_state_concurrent_update",
        )

    @staticmethod
    def _terminal_reconciliation_from_record(
        record: RunControlRecord,
    ) -> _TerminalReconciliationState | None:
        decision = record.terminal_reconciliation_decision
        phase = record.terminal_reconciliation_phase
        terminal_state = record.terminal_reconciliation_terminal_state
        status = record.terminal_reconciliation_status
        diagnostic = record.terminal_reconciliation_diagnostic
        if all(
            value is None
            for value in (decision, phase, terminal_state, status, diagnostic)
        ):
            return None
        validate_terminal_reconciliation_metadata(
            decision=decision,
            phase=phase,
            terminal_state=terminal_state,
            status=status,
            diagnostic=diagnostic,
        )
        if status == "invalid":
            return _TerminalReconciliationState(
                run_id=record.run_id,
                decision=None,
                phase=None,
                terminal_state=None,
                status="invalid",
                diagnostic=diagnostic,
            )
        return _TerminalReconciliationState(
            run_id=record.run_id,
            decision=decision,
            phase=phase,
            terminal_state=terminal_state,
            status=status,
            diagnostic=diagnostic,
        )

    def _terminal_reconciliation_key(self, run_id: str) -> tuple[str, str]:
        return (str(self.runs_dir.resolve()), run_id)

    def _assert_not_reentrant_terminal_operation(self, run_id: str) -> None:
        if self._terminal_reconciliation_key(run_id) in _ACTIVE_TERMINAL_RECONCILIATIONS.get():
            raise _TerminalReconciliationReentry(
                f"terminal reconciler cannot re-enter run {run_id!r}",
            )

    def _append_accepted_terminal_event(
        self,
        run_id: str,
        record: RunControlRecord,
        *,
        reason: str | None = None,
        recovered: bool = False,
    ) -> dict[str, Any]:
        event_id = record.accepted_terminal_event_id
        if not event_id:
            raise RuntimeError(
                f"terminal run {run_id!r} is missing accepted_terminal_event_id",
            )
        event_name = record.terminal_event or {
            "completed": "run.done",
            "failed": "run.error",
            "cancelled": "run.cancelled",
        }.get(record.state, f"run.{record.state}")
        payload: dict[str, Any] = {"run_id": run_id, "event": event_name}
        if reason:
            payload["reason"] = reason
        if recovered:
            payload["recovered"] = True
        return append_jsonl_event(
            self.runs_dir / run_id / "run_events.jsonl",
            payload,
            event_id=event_id,
        )

    def _release_active_run(self, run_id: str) -> None:
        self._active.pop(run_id, None)
        self._request_kinds.pop(run_id, None)
        completed_job = self._windows_jobs.pop(run_id, None)
        if completed_job is not None:
            completed_job.close()

    async def _monitor(
        self,
        request: RunWorkerRequest,
        process: asyncio.subprocess.Process,
        *,
        stdout_task: asyncio.Task[str],
        stderr_task: asyncio.Task[str],
        relay_task: asyncio.Task[int],
    ) -> WorkerOutcome:
        returncode = await process.wait()
        stdout_tail, stderr_tail = await asyncio.gather(stdout_task, stderr_task)
        relayed = await relay_task
        run_dir = self.runs_dir / request.run_id
        result_read = _read_worker_result_detail(
            run_dir / "worker_result.json",
            expected_run_id=request.run_id,
            expected_job_kind=request.job_kind,
        )
        result = result_read.result
        error = result_read.error
        protocol_error_code = result_read.protocol_error_code
        protocol_error = error
        if (
            protocol_error_code is None
            and returncode != 0
            and result is not None
            and error is None
        ):
            protocol_error_code = "worker_exit_contradiction"
            protocol_error = (
                "worker returned a success result but exited with status "
                f"{returncode}"
            )
        exit_diagnostic: WorkerExitDiagnostic | None = None
        current = self.control_store.read(request.run_id)
        if (
            protocol_error_code is not None
            and protocol_error is not None
            and current.state not in {"cancelling", "cancelled"}
        ):
            secrets = sensitive_values(request)
            internal_paths = tuple(
                str(value)
                for value in {
                    Path.home(),
                    Path(request.settings.repo_root),
                    Path(request.settings.out_dir),
                    run_dir,
                }
            )
            exit_diagnostic = _build_worker_exit_diagnostic(
                returncode=returncode,
                protocol_error_code=protocol_error_code,
                protocol_error=protocol_error,
                stdout_tail=stdout_tail,
                stderr_tail=stderr_tail,
                run_events_path=run_dir / "run_events.jsonl",
                secrets=secrets,
                internal_paths=internal_paths,
            )
            try:
                append_jsonl_event(
                    run_dir / "run_events.jsonl",
                    exit_diagnostic.event_payload(
                        run_id=request.run_id,
                        job_kind=request.job_kind,
                    ),
                    event_id=str(uuid5(
                        NAMESPACE_URL,
                        f"autodesign:{request.run_id}:worker.exit:v1",
                    )),
                )
            except OSError:
                pass
        ok = returncode == 0 and result is not None and error is None
        if current.state == "running":
            try:
                self.control_store.transition(request.run_id, current, "completing")
            except InvalidRunTransition:
                pass
        return WorkerOutcome(
            run_id=request.run_id,
            job_kind=request.job_kind,
            returncode=returncode,
            ok=ok,
            result=result,
            error=error or (
                exit_diagnostic.error_message
                if exit_diagnostic is not None
                else None
            ) or (
                None if returncode == 0 else f"worker exited with status {returncode}"
            ),
            relayed_events=relayed,
            failure_phase=result_read.failure_phase,
            pointer_cleanup_warnings=result_read.pointer_cleanup_warnings,
            exit_diagnostic=exit_diagnostic,
        )

    async def _drain(
        self,
        stream: asyncio.StreamReader | None,
        path: Path,
        secrets: tuple[str, ...],
    ) -> str:
        writer = RedactingLogWriter(path, secrets, max_bytes=self._log_max_bytes)
        tail = _RedactedTailBuffer(secrets, max_chars=_WORKER_EXIT_TAIL_CHARS)
        try:
            if stream is not None:
                while True:
                    chunk = await stream.read(64 * 1024)
                    if not chunk:
                        break
                    writer.feed(chunk)
                    tail.feed(chunk)
        finally:
            writer.close()
        return tail.close()

    async def _relay_worker_events(
        self,
        run_id: str,
        process: asyncio.subprocess.Process,
    ) -> int:
        path = self.runs_dir / run_id / "worker_events.jsonl"
        seen: set[str] = set()
        last_worker_seq = 0
        relayed = 0
        relay_lock = self._event_relay_locks.setdefault(run_id, asyncio.Lock())
        while True:
            for event in read_jsonl_events(path):
                event_id = event.get("event_id")
                sequence = event.get("seq")
                event_name = event.get("event")
                if (
                    not isinstance(event_id, str)
                    or not event_id
                    or event.get("run_id") != run_id
                    or type(sequence) is not int
                    or sequence <= last_worker_seq
                    or event_id in seen
                    or event_name in {"run.done", "run.error", "run.cancelled"}
                ):
                    continue
                async with relay_lock:
                    authoritative = self.control_store.read(run_id)
                    if authoritative.state not in {"running", "completing"}:
                        return relayed
                    seen.add(event_id)
                    last_worker_seq = sequence
                    canonical = append_jsonl_event(
                        self.runs_dir / run_id / "run_events.jsonl",
                        {
                            key: value for key, value in event.items()
                            if key not in {"event_id", "seq"}
                        } | {"source_event_id": event_id, "worker_seq": sequence},
                        event_id=str(uuid5(NAMESPACE_URL, f"autodesign:{run_id}:{event_id}")),
                    )
                relayed += 1
                if self._event_sink is not None:
                    await self._broadcast_once(canonical)
            if process.returncode is not None:
                break
            await asyncio.sleep(0.025)
        return relayed

    async def _broadcast_once(self, event: dict[str, Any]) -> None:
        event_id = str(event.get("event_id") or "")
        if not event_id or event_id in self._broadcasted_event_ids or self._event_sink is None:
            return
        self._broadcasted_event_ids.add(event_id)
        try:
            delivered = self._event_sink(event)
            if inspect.isawaitable(delivered):
                await asyncio.wait_for(
                    delivered,
                    timeout=max(0.05, min(0.25, self._grace_s)),
                )
        except asyncio.TimeoutError:
            self._broadcasted_event_ids.discard(event_id)
            return
        except asyncio.CancelledError:
            self._broadcasted_event_ids.discard(event_id)
            raise
        except Exception:
            self._broadcasted_event_ids.discard(event_id)
            return
        except BaseException:
            self._broadcasted_event_ids.discard(event_id)
            raise

def build_worker_environment(*, owner_nonce: str | None = None) -> dict[str, str]:
    """Copy only noncredential runtime variables required by owned tools."""
    exact = {
        "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TEMP", "TMP",
        "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "LANG", "LANGUAGE", "TZ",
        "PYTHONHOME", "PYTHONPATH", "NODE_PATH", "NPM_CONFIG_CACHE", "PLAYWRIGHT_BROWSERS_PATH",
        "FONTCONFIG_PATH", "FONTCONFIG_FILE", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
        "AUTODESIGN_HARNESS_AUTH_DIR", "DESIGN_ANYTHING_HARNESS_AUTH_DIR",
        "AUTODESIGN_HYPERFRAMES_BIN",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    }
    prefixes = ("LC_",)
    environment: dict[str, str] = {}
    for key, value in _WORKER_ENV_SOURCE.items():
        if key in exact or any(key.startswith(prefix) for prefix in prefixes):
            if key.upper() in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}:
                if _proxy_has_credentials(value):
                    continue
            environment[key] = value
    environment["PYTHONUNBUFFERED"] = "1"
    if owner_nonce:
        environment["AUTODESIGN_PROCESS_OWNER"] = str(owner_nonce)
    return environment


def _read_worker_result(
    path: Path,
    *,
    expected_run_id: str,
    expected_job_kind: str,
) -> tuple[
    dict[str, Any] | None,
    str | None,
    str | None,
    tuple[str, ...],
]:
    decoded = _read_worker_result_detail(
        path,
        expected_run_id=expected_run_id,
        expected_job_kind=expected_job_kind,
    )
    return (
        decoded.result,
        decoded.error,
        decoded.failure_phase,
        decoded.pointer_cleanup_warnings,
    )


def _read_worker_result_detail(
    path: Path,
    *,
    expected_run_id: str,
    expected_job_kind: str,
) -> _WorkerResultRead:
    try:
        payload = parse_worker_result_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _WorkerResultRead(
            None,
            "worker_result.json is missing",
            None,
            (),
            "worker_result_missing",
        )
    except ProtocolError as exc:
        return _WorkerResultRead(
            None,
            str(exc),
            None,
            (),
            "worker_result_invalid",
        )
    try:
        payload = decode_worker_result(
            payload,
            expected_run_id=expected_run_id,
            expected_job_kind=expected_job_kind,
        )
    except ProtocolError as exc:
        return _WorkerResultRead(
            None,
            str(exc),
            None,
            (),
            "worker_result_invalid",
        )
    value_key = "result" if payload["ok"] else "error"
    value = payload.get(value_key)
    if not isinstance(value, dict):
        return _WorkerResultRead(
            None,
            f"worker result {value_key} must be an object",
            None,
            (),
            "worker_result_invalid",
        )
    if "run_id" in value and value["run_id"] != expected_run_id:
        return _WorkerResultRead(
            None,
            "worker result run_id does not match request",
            None,
            (),
            "worker_result_invalid",
        )
    pointer_cleanup_warnings = _immutable_pointer_cleanup_warnings(
        value.get("pointer_cleanup_warnings")
    )
    if payload["ok"]:
        return _WorkerResultRead(
            value,
            None,
            None,
            pointer_cleanup_warnings,
        )
    message = value.get("message")
    phase = value.get("phase")
    return _WorkerResultRead(
        result=None,
        error=format_worker_error_message(
            str(message or value.get("type") or "worker failed"),
            pointer_cleanup_warnings,
        ),
        failure_phase=str(phase) if isinstance(phase, str) and phase else None,
        pointer_cleanup_warnings=pointer_cleanup_warnings,
    )


def _has_recoverable_worker_result(path: Path, run_id: str) -> bool:
    try:
        payload = parse_worker_result_json(path.read_text(encoding="utf-8"))
        job_kind = payload.get("job_kind")
        if not isinstance(job_kind, str):
            return False
        decode_worker_result(
            payload, expected_run_id=run_id, expected_job_kind=job_kind,
        )
    except (OSError, ProtocolError, AttributeError):
        return False
    return True


async def _kill_unreleased_worker(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=1.0)
    except asyncio.TimeoutError:
        pass
