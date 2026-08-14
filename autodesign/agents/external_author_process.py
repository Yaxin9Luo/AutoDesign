"""Shared cancellable process runner for external artifact authors."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import shutil
import select
import subprocess
import threading
import time
from typing import Any

from ..process_supervision import (
    ProcessIdentity,
    ProcessLedger,
    ProcessLedgerError,
    descendant_process_identities,
    process_is_alive,
    spawn_registered_process,
    terminate_process_identities,
)
from ..run_control import RunCancelled
from ..util.logging import RedactingLogWriter


ExternalAuthorProcessStatus = str


@dataclass(frozen=True)
class ExternalAuthorProcessRequest:
    run_id: str
    attempt: int
    command: Sequence[str]
    cwd: Path
    prompt: str
    timeout_s: float
    stdout_path: Path
    stderr_path: Path
    env: Mapping[str, str] | None = None
    completion_requested: Callable[[], str | None] | None = None
    interruption_requested: Callable[[], bool] | None = None
    selection_requested: Callable[[], str | None] | None = None
    poll_interval_s: float = 0.05
    run_dir: Path | None = None
    cancellation_token: Any = None
    sensitive_values: Sequence[str] = ()


@dataclass(frozen=True)
class ExternalAuthorProcessResult:
    status: ExternalAuthorProcessStatus
    reason: str
    returncode: int | None
    timed_out: bool
    elapsed_s: float
    stdout: str
    stderr: str
    process_group_id: int | None


@dataclass
class _RegisteredProcess:
    ledger: ProcessLedger
    process: subprocess.Popen[bytes] | None = None
    identity: ProcessIdentity | None = None
    identities: dict[int, ProcessIdentity] = field(default_factory=dict)
    interruption_reason: str | None = None
    tracker_stop: threading.Event = field(default_factory=threading.Event)
    tracker_ready: threading.Event = field(default_factory=threading.Event)
    tracker_thread: threading.Thread | None = None
    tracker_failures: list[BaseException] = field(default_factory=list)


class _AttemptSelected(BaseException):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _PromptDeliveryTimedOut(Exception):
    pass


_REGISTRY: dict[str, _RegisteredProcess] = {}
_REGISTRY_LOCK = threading.Lock()
_POLL_EVENT = threading.Event()
_MAX_CAPTURE_BYTES = 2 * 1024 * 1024
_SENSITIVE_NAME = re.compile(
    r"(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|password|authorization|credential)",
    re.IGNORECASE,
)


def _capture_registered_descendants(registered: _RegisteredProcess) -> None:
    root = registered.identity
    if root is None:
        return
    for identity in descendant_process_identities(root):
        with _REGISTRY_LOCK:
            existing = registered.identities.get(identity.pid)
            if existing is not None:
                continue
            registered.identities[identity.pid] = identity
        try:
            registered.ledger.register_existing(
                identity,
                role="external-author-descendant",
            )
        except ProcessLedgerError as exc:
            snapshot = registered.ledger.read()
            already_registered = any(
                item.identity.pid == identity.pid
                and item.identity.birth_id == identity.birth_id
                for item in snapshot.processes
            )
            if not already_registered and not snapshot.sealed:
                registered.tracker_failures.append(exc)


def _start_descendant_tracker(registered: _RegisteredProcess, *, run_id: str) -> None:
    if os.name != "posix" or registered.tracker_thread is not None:
        return

    def track() -> None:
        started = time.monotonic()
        while True:
            try:
                _capture_registered_descendants(registered)
            except BaseException as exc:
                registered.tracker_failures.append(exc)
                registered.tracker_ready.set()
                return
            registered.tracker_ready.set()
            elapsed = time.monotonic() - started
            delay_s = 0.001 if elapsed < 0.1 else 0.01 if elapsed < 0.5 else 0.25
            if registered.tracker_stop.wait(delay_s):
                return

    thread = threading.Thread(
        target=track,
        name=f"external-author-descendants-{run_id}",
        daemon=False,
    )
    registered.tracker_thread = thread
    thread.start()
    if not registered.tracker_ready.wait(timeout=1.0):
        registered.tracker_stop.set()
        thread.join(timeout=1.0)
        raise RuntimeError("external author descendant tracker did not become ready")


def _stop_descendant_tracker(
    registered: _RegisteredProcess,
    *,
    raise_errors: bool,
) -> None:
    registered.tracker_stop.set()
    thread = registered.tracker_thread
    if thread is not None:
        thread.join(timeout=2.0)
        if thread.is_alive() and raise_errors:
            raise RuntimeError("external author descendant tracker did not stop")
    if raise_errors and registered.tracker_failures:
        raise RuntimeError(
            "external author descendant tracking failed"
        ) from registered.tracker_failures[0]


def context_cancellation_checkpoint(ctx: Any, phase: str) -> None:
    callback = getattr(ctx, "raise_if_cancelled", None)
    if callable(callback):
        callback(phase)


def context_cancellation_callback(ctx: Any) -> Callable[[], bool] | None:
    callback = getattr(ctx, "is_cancelled", None)
    return callback if callable(callback) else None


def context_cancellation_token(ctx: Any) -> Any:
    return getattr(ctx, "cancellation_token", None)


def context_attempt_selection_callback(
    ctx: Any,
) -> Callable[[], str | None] | None:
    run_dir_value = getattr(ctx, "run_dir", None)
    if run_dir_value is None:
        return None
    run_dir = Path(run_dir_value)

    def selection_requested() -> str | None:
        try:
            from ..attempt_candidates import load_selection_journal

            journal = load_selection_journal(run_dir)
        except (OSError, RuntimeError, ValueError):
            return None
        if journal is None or journal.state == "failed":
            return None
        return f"selected_attempt:attempt_{journal.source_attempt:02d}"

    return selection_requested


@dataclass(frozen=True)
class _OutputDrains:
    threads: tuple[threading.Thread, ...]
    streams: tuple[Any, ...]
    stop_event: threading.Event
    failures: list[BaseException]


def derive_external_author_sensitive_values(
    command: Sequence[str],
    env: Mapping[str, str] | None = None,
    explicit_values: Sequence[str] = (),
) -> tuple[str, ...]:
    values = {str(value) for value in explicit_values if str(value)}
    for key, raw_value in (env or {}).items():
        value = str(raw_value or "").strip()
        if _SENSITIVE_NAME.search(str(key)) and len(value) >= 4:
            values.add(value)
    for index, token in enumerate(command):
        name, separator, inline_value = str(token).partition("=")
        normalized_name = name.lstrip("-").replace("_", "-")
        if not (name.startswith("-") or separator) or not _SENSITIVE_NAME.search(
            normalized_name
        ):
            continue
        value = inline_value if separator else (
            str(command[index + 1]) if index + 1 < len(command) else ""
        )
        value = value.strip()
        if len(value) >= 4:
            values.add(value)
    return tuple(sorted(values, key=len, reverse=True))


def _process_sensitive_values(
    request: ExternalAuthorProcessRequest,
    command: Sequence[str],
) -> tuple[str, ...]:
    return derive_external_author_sensitive_values(
        command,
        request.env,
        request.sensitive_values,
    )


def _redact_text(value: str, sensitive_values: Sequence[str]) -> str:
    redacted = value
    for secret in sensitive_values:
        redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _write_redacted_log(
    path: Path,
    value: str,
    sensitive_values: tuple[str, ...],
) -> None:
    writer = RedactingLogWriter(
        path,
        sensitive_values,
        max_bytes=_MAX_CAPTURE_BYTES,
    )
    try:
        writer.feed(value.encode("utf-8", errors="replace"))
    finally:
        writer.close()


def _start_output_drains(
    process: subprocess.Popen[bytes],
    *,
    run_id: str,
    stdout_path: Path,
    stderr_path: Path,
    sensitive_values: tuple[str, ...],
) -> _OutputDrains:
    streams = (process.stdout, process.stderr)
    if any(stream is None for stream in streams):
        raise RuntimeError("external author process output pipes are unavailable")
    writers: list[RedactingLogWriter] = []
    try:
        for path in (stdout_path, stderr_path):
            writers.append(
                RedactingLogWriter(
                    path,
                    sensitive_values,
                    max_bytes=_MAX_CAPTURE_BYTES,
                )
            )
    except BaseException:
        for writer in writers:
            writer.close()
        raise

    failures: list[BaseException] = []
    threads: list[threading.Thread] = []
    stop_event = threading.Event()
    for label, stream, writer in zip(
        ("stdout", "stderr"), streams, writers, strict=True
    ):
        assert stream is not None

        def drain(
            stream=stream,
            writer=writer,
        ) -> None:
            try:
                read_chunk = getattr(stream, "read1", stream.read)
                while True:
                    chunk = read_chunk(64 * 1024)
                    if not chunk:
                        break
                    if stop_event.is_set():
                        break
                    writer.feed(chunk)
            except BaseException as exc:
                failures.append(exc)
            finally:
                try:
                    writer.close()
                except BaseException as exc:
                    failures.append(exc)
                try:
                    stream.close()
                except BaseException as exc:
                    failures.append(exc)

        thread = threading.Thread(
            target=drain,
            name=f"external-author-{label}-{run_id}",
            daemon=True,
        )
        threads.append(thread)
        thread.start()
    return _OutputDrains(tuple(threads), tuple(streams), stop_event, failures)


def _abort_output_drains(drains: _OutputDrains) -> None:
    drains.stop_event.set()
    for stream in drains.streams:
        try:
            os.close(stream.fileno())
        except (OSError, ValueError):
            pass


def _join_output_drains(
    drains: _OutputDrains,
    *,
    request: ExternalAuthorProcessRequest | None,
    raise_errors: bool,
) -> None:
    if drains.stop_event.is_set():
        for thread in drains.threads:
            thread.join(timeout=0.05)
        return
    deadline = time.monotonic() + 2.0
    while True:
        alive = tuple(thread for thread in drains.threads if thread.is_alive())
        if not alive:
            break
        if request is not None:
            _raise_if_run_cancelled(request, "external_author.output_drain")
        if time.monotonic() >= deadline:
            # An instantaneous POSIX double-fork can escape lineage observation.
            # Stop capture and surface the diagnostic instead of hanging forever.
            _abort_output_drains(drains)
            if raise_errors:
                raise RuntimeError("external author output pipes did not close")
            return
        for thread in alive:
            thread.join(timeout=0.01)
    if raise_errors and drains.failures:
        raise RuntimeError("external author output capture failed") from drains.failures[0]


def process_group_is_alive(process_group_id: int | None) -> bool:
    """Compatibility liveness probe; cleanup uses birth-verified identities."""
    if process_group_id is None or process_group_id <= 1 or os.name != "posix":
        return False
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _termination_report(registered: _RegisteredProcess, *, grace_s: float = 0.75):
    identity = registered.identity
    if identity is None:
        return None
    _capture_registered_descendants(registered)
    with _REGISTRY_LOCK:
        identities = tuple(registered.identities.values())
    snapshot = registered.ledger.read()
    owned_identity_keys = {
        (item.pid, item.birth_id)
        for item in identities
    }
    # The shared ledger also records the worker that is performing this cleanup.
    # Restrict owner scans to the external author tree so it cannot kill its parent.
    owner_nonces = tuple(
        record.nonce
        for record in snapshot.processes
        if (record.identity.pid, record.identity.birth_id) in owned_identity_keys
    )
    report = terminate_process_identities(
        identities,
        root_pid=identity.pid,
        grace_s=grace_s,
        owner_nonces=owner_nonces,
    )
    surviving = tuple(item for item in report.survivors if process_is_alive(item))
    if report.unresolved_spawns or surviving or report.stale_identities:
        details: list[str] = []
        if report.unresolved_spawns:
            details.append("unresolved spawn")
        if surviving:
            details.append("managed process survived")
        if report.stale_identities:
            details.append("process identity mismatch")
        raise RuntimeError("external author termination unverified: " + ", ".join(details))
    return report


def _terminate_registered_process(
    registered: _RegisteredProcess,
    *,
    grace_s: float = 0.75,
) -> None:
    process = registered.process
    _termination_report(registered, grace_s=grace_s)
    if process is not None:
        try:
            process.wait(timeout=max(0.1, grace_s))
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("external author process did not exit after termination") from exc


def terminate_registered_author_process(run_id: str, reason: str) -> bool:
    """Request local attempt selection; run cancellation is token-authoritative."""
    with _REGISTRY_LOCK:
        registered = _REGISTRY.get(run_id)
        if registered is None:
            return False
        if registered.interruption_reason is None:
            registered.interruption_reason = reason
        can_terminate = registered.identity is not None
    if can_terminate:
        _terminate_registered_process(registered)
    return True


def _read_output(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _request_run_dir(request: ExternalAuthorProcessRequest) -> Path:
    if request.run_dir is not None:
        return Path(request.run_dir)
    return Path(request.cwd)


def _raise_if_run_cancelled(
    request: ExternalAuthorProcessRequest,
    phase: str,
) -> None:
    token = request.cancellation_token
    if token is not None:
        token.raise_if_cancelled(phase)
    callback = request.interruption_requested
    if callback is not None and callback():
        raise RunCancelled(request.run_id, f"{phase}.run_cancelled")


def _selection_reason(request: ExternalAuthorProcessRequest) -> str | None:
    callback = request.selection_requested
    if callback is None:
        return None
    reason = callback()
    return str(reason).strip() if reason else None


def _raise_if_process_interrupted(
    request: ExternalAuthorProcessRequest,
    phase: str,
) -> None:
    _raise_if_run_cancelled(request, phase)
    reason = _selection_reason(request)
    if reason is not None:
        _raise_if_run_cancelled(request, phase)
        raise _AttemptSelected(reason)


def _wait_for_poll(request: ExternalAuthorProcessRequest, delay_s: float) -> None:
    token = request.cancellation_token
    if token is not None and getattr(token, "can_cancel", True):
        if token.wait(delay_s, poll_interval=min(0.01, delay_s)):
            token.raise_if_cancelled("external_author.process.run_cancelled")
    else:
        _POLL_EVENT.wait(delay_s)
    _raise_if_process_interrupted(
        request,
        "external_author.process.run_cancelled",
    )


def _write_prompt_cancellable(
    process: subprocess.Popen[bytes],
    request: ExternalAuthorProcessRequest,
    *,
    deadline: float,
    abort: Callable[[], None],
) -> None:
    stream = process.stdin
    if stream is None:
        return
    payload = request.prompt.encode("utf-8")
    descriptor = stream.fileno()
    try:
        os.set_blocking(descriptor, False)
    except (AttributeError, OSError):
        _write_prompt_in_blocking_thread(
            process,
            request,
            payload=payload,
            descriptor=descriptor,
            stream=stream,
            deadline=deadline,
            abort=abort,
        )
        return
    offset = 0
    view = memoryview(payload)
    try:
        while offset < len(payload):
            _raise_if_process_interrupted(request, "external_author.prompt_write")
            if process.poll() is not None:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _PromptDeliveryTimedOut()
            if os.name == "posix":
                _, writable, _ = select.select(
                    [], [descriptor], [], min(0.05, remaining)
                )
                if not writable:
                    continue
            _raise_if_process_interrupted(request, "external_author.prompt_write")
            try:
                written = os.write(
                    descriptor,
                    view[offset:offset + 64 * 1024],
                )
            except BlockingIOError:
                _wait_for_poll(request, min(0.01, remaining))
                continue
            except BrokenPipeError:
                break
            if written <= 0:
                raise BrokenPipeError("external author prompt pipe closed")
            offset += written
            _raise_if_process_interrupted(
                request,
                "external_author.after_prompt_chunk",
            )
    finally:
        view.release()
        stream.close()


def _write_prompt_in_blocking_thread(
    process: subprocess.Popen[bytes],
    request: ExternalAuthorProcessRequest,
    *,
    payload: bytes,
    descriptor: int,
    stream: Any,
    deadline: float,
    abort: Callable[[], None],
) -> None:
    """Isolate a blocking pipe write and always reap its single writer thread."""
    completed = threading.Event()
    failure: list[BaseException] = []

    def write_payload() -> None:
        offset = 0
        view = memoryview(payload)
        try:
            while offset < len(payload):
                written = os.write(
                    descriptor,
                    view[offset:offset + 64 * 1024],
                )
                if written <= 0:
                    raise BrokenPipeError("external author prompt pipe closed")
                offset += written
        except BaseException as exc:
            failure.append(exc)
        finally:
            view.release()
            try:
                stream.close()
            except BaseException as exc:
                if not failure:
                    failure.append(exc)
            completed.set()

    writer = threading.Thread(
        target=write_payload,
        name=f"external-author-prompt-{request.run_id}",
        daemon=False,
    )
    writer.start()
    try:
        while not completed.wait(0.01):
            _raise_if_process_interrupted(request, "external_author.prompt_write")
            if time.monotonic() >= deadline:
                raise _PromptDeliveryTimedOut()
            if process.poll() is not None:
                continue
        writer.join()
        if failure and not isinstance(failure[0], BrokenPipeError):
            raise failure[0]
    except BaseException:
        try:
            abort()
        finally:
            writer.join()
        raise


def _command_launch_error(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None,
) -> str | None:
    executable = command[0]
    if os.path.dirname(executable):
        candidate = Path(executable)
        if not candidate.is_absolute():
            candidate = cwd / candidate
        if not candidate.is_file():
            return f"FileNotFoundError: command not found: {executable}"
        return None
    search_path = env.get("PATH") if env is not None else None
    if shutil.which(executable, path=search_path) is None:
        return f"FileNotFoundError: command not found: {executable}"
    return None


def run_external_author_process(
    request: ExternalAuthorProcessRequest,
) -> ExternalAuthorProcessResult:
    started = time.monotonic()
    command = [str(part) for part in request.command]
    _raise_if_run_cancelled(request, "external_author.before_output_paths")
    selection_reason = _selection_reason(request)
    if selection_reason is not None:
        _raise_if_run_cancelled(request, "external_author.before_output_paths")
        return ExternalAuthorProcessResult(
            status="selected",
            reason=selection_reason,
            returncode=None,
            timed_out=False,
            elapsed_s=time.monotonic() - started,
            stdout="",
            stderr="",
            process_group_id=None,
        )
    if not command:
        return ExternalAuthorProcessResult(
            status="spawn_error",
            reason="empty_command",
            returncode=None,
            timed_out=False,
            elapsed_s=0.0,
            stdout="",
            stderr="configured author command is empty",
            process_group_id=None,
        )
    stdout_path = Path(request.stdout_path)
    stderr_path = Path(request.stderr_path)
    run_dir = _request_run_dir(request)
    ledger = ProcessLedger(run_dir)
    sensitive_values = _process_sensitive_values(request, command)
    launch_error = _command_launch_error(
        command,
        cwd=Path(request.cwd),
        env=request.env,
    )
    if launch_error is not None:
        launch_error = _redact_text(launch_error, sensitive_values)
        _raise_if_run_cancelled(request, "external_author.before_spawn_error_log")
        _write_redacted_log(stderr_path, launch_error, sensitive_values)
        _raise_if_run_cancelled(request, "external_author.after_spawn_error_log")
        return ExternalAuthorProcessResult(
            status="spawn_error",
            reason="spawn_error",
            returncode=None,
            timed_out=False,
            elapsed_s=time.monotonic() - started,
            stdout="",
            stderr=launch_error,
            process_group_id=None,
        )

    registered = _RegisteredProcess(ledger=ledger)
    with _REGISTRY_LOCK:
        if request.run_id in _REGISTRY:
            raise RuntimeError(
                f"external author process already registered: {request.run_id}"
            )
        _REGISTRY[request.run_id] = registered

    process: subprocess.Popen[bytes] | None = None
    drains: _OutputDrains | None = None
    status: ExternalAuthorProcessStatus = "error"
    reason = "process_exit"
    timed_out = False
    returncode: int | None = None
    try:
        _raise_if_run_cancelled(request, "external_author.before_raw_log_open")
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        _raise_if_run_cancelled(request, "external_author.before_registered_spawn")
        def capture_identity(identity: ProcessIdentity) -> None:
            registered.identity = identity
            registered.identities[identity.pid] = identity
            _raise_if_process_interrupted(
                request,
                "external_author.after_spawn_registration",
            )
            with _REGISTRY_LOCK:
                selection_reason = registered.interruption_reason
            if selection_reason is not None:
                _raise_if_run_cancelled(
                    request,
                    "external_author.after_spawn_registration",
                )
                raise _AttemptSelected(selection_reason)

        def guard_release(_identity: ProcessIdentity) -> None:
            _raise_if_process_interrupted(
                request,
                "external_author.before_spawn_release",
            )
            with _REGISTRY_LOCK:
                if registered.interruption_reason is not None:
                    _raise_if_run_cancelled(
                        request,
                        "external_author.before_spawn_release",
                    )
                    raise _AttemptSelected(registered.interruption_reason)
            _start_descendant_tracker(registered, run_id=request.run_id)

        try:
            process = spawn_registered_process(
                ledger,
                command,
                role="external-author",
                cwd=request.cwd,
                env=request.env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=(os.name == "posix"),
                registration_hook=capture_identity,
                release_hook=guard_release,
            )
        except _AttemptSelected as selected:
            status = "selected"
            reason = selected.reason
        except OSError as exc:
            message = _redact_text(f"{type(exc).__name__}: {exc}", sensitive_values)
            _write_redacted_log(stderr_path, message, sensitive_values)
            status = "spawn_error"
            reason = "spawn_error"
        if process is not None:
            registered.process = process
            drains = _start_output_drains(
                process,
                run_id=request.run_id,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                sensitive_values=sensitive_values,
            )
            _raise_if_run_cancelled(request, "external_author.after_registered_spawn")
            deadline = started + max(0.01, float(request.timeout_s))
            try:
                _write_prompt_cancellable(
                    process,
                    request,
                    deadline=deadline,
                    abort=lambda: _terminate_registered_process(registered),
                )
                _raise_if_process_interrupted(
                    request,
                    "external_author.after_prompt_write",
                )
            except _PromptDeliveryTimedOut:
                timed_out = True
                _terminate_registered_process(registered)
                status = "timeout"
                reason = "timeout"
                returncode = process.poll()
            except _AttemptSelected as selected:
                _terminate_registered_process(registered)
                status = "selected"
                reason = selected.reason
                returncode = process.poll()

            while not timed_out and status != "selected":
                _raise_if_run_cancelled(
                    request, "external_author.process.run_cancelled"
                )
                durable_selection_reason = _selection_reason(request)
                if durable_selection_reason is not None:
                    _raise_if_run_cancelled(
                        request,
                        "external_author.process.run_cancelled",
                    )
                    _terminate_registered_process(registered)
                    status = "selected"
                    reason = durable_selection_reason
                    returncode = process.poll()
                    break
                returncode = process.poll()
                with _REGISTRY_LOCK:
                    interruption_reason = registered.interruption_reason
                if interruption_reason:
                    _raise_if_run_cancelled(
                        request,
                        "external_author.process.run_cancelled",
                    )
                    _terminate_registered_process(registered)
                    status = "selected"
                    reason = interruption_reason
                    returncode = process.poll()
                    break
                completion_reason = None
                if request.completion_requested is not None:
                    _raise_if_run_cancelled(
                        request, "external_author.before_completion_check"
                    )
                    completion_reason = request.completion_requested()
                    _raise_if_run_cancelled(
                        request, "external_author.after_completion_check"
                    )
                if completion_reason:
                    _terminate_registered_process(registered)
                    status = "ok"
                    reason = completion_reason
                    returncode = process.poll()
                    break
                if returncode is not None:
                    _raise_if_run_cancelled(
                        request, "external_author.after_process_exit"
                    )
                    _terminate_registered_process(registered)
                    status = "ok" if returncode == 0 else "error"
                    reason = "process_exit"
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    _terminate_registered_process(registered)
                    status = "timeout"
                    reason = "timeout"
                    returncode = process.poll()
                    break
                try:
                    _wait_for_poll(
                        request,
                        max(0.01, float(request.poll_interval_s)),
                    )
                except _AttemptSelected as selected:
                    _terminate_registered_process(registered)
                    status = "selected"
                    reason = selected.reason
                    returncode = process.poll()
                    break
        _stop_descendant_tracker(registered, raise_errors=True)
        if drains is not None:
            _join_output_drains(
                drains,
                request=request,
                raise_errors=True,
            )
    except BaseException:
        if process is not None and process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        if registered.identity is not None:
            _terminate_registered_process(registered)
        _stop_descendant_tracker(registered, raise_errors=False)
        if drains is not None:
            _join_output_drains(
                drains,
                request=None,
                raise_errors=False,
            )
        raise
    finally:
        with _REGISTRY_LOCK:
            if _REGISTRY.get(request.run_id) is registered:
                _REGISTRY.pop(request.run_id, None)

    _raise_if_run_cancelled(request, "external_author.before_raw_log_read")
    late_selection_reason = _selection_reason(request)
    if late_selection_reason is not None:
        _raise_if_run_cancelled(request, "external_author.before_raw_log_read")
        status = "selected"
        reason = late_selection_reason
    stdout = _read_output(stdout_path)
    _raise_if_run_cancelled(request, "external_author.after_stdout_read")
    late_selection_reason = _selection_reason(request)
    if late_selection_reason is not None:
        _raise_if_run_cancelled(request, "external_author.after_stdout_read")
        status = "selected"
        reason = late_selection_reason
    stderr = _read_output(stderr_path)
    _raise_if_run_cancelled(request, "external_author.after_stderr_read")
    late_selection_reason = _selection_reason(request)
    if late_selection_reason is not None:
        _raise_if_run_cancelled(request, "external_author.after_stderr_read")
        status = "selected"
        reason = late_selection_reason
    identity = registered.identity
    return ExternalAuthorProcessResult(
        status=status,
        reason=reason,
        returncode=returncode,
        timed_out=timed_out,
        elapsed_s=time.monotonic() - started,
        stdout=stdout,
        stderr=stderr,
        process_group_id=(identity.process_group_id if identity is not None else None),
    )
