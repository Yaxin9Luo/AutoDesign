"""Minimal structured logger — every event is a single JSON-able line.

In addition to the always-on stderr emission, `log()` supports a
per-run pub/sub channel so the FastAPI shim can stream phase progress
to the browser via SSE without bloating the runner with explicit
callbacks.

Usage:

    from .util.logging import log, run_context, subscribe, unsubscribe

    # In a long-running run (e.g. PipelineRunner.run):
    with run_context(run_id):
        log("designer.start")           # auto-tagged with run_id
        log("composite.done", ...)     # ditto

    # In the FastAPI shim:
    q = subscribe(run_id)
    try:
        while True:
            event = q.get(timeout=2)   # thread-safe Queue
            ...
    finally:
        unsubscribe(run_id, q)
"""

from __future__ import annotations

from collections import deque
import codecs
import json
import os
import queue
import sys
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4


# Tags every log emitted within `run_context(run_id)` so subscribers can
# filter by run without touching every log call site. Falls back to the
# `run_id=` kwarg when the contextvar isn't set (e.g. logs outside any
# run, or background threads where the var was never propagated).
_run_id_var: ContextVar[str | None] = ContextVar("autodesign_run_id", default=None)
_run_dir_var: ContextVar[Path | None] = ContextVar("autodesign_run_dir", default=None)
_event_file_var: ContextVar[str | None] = ContextVar("autodesign_event_file", default=None)
_suppress_terminal_var: ContextVar[bool] = ContextVar(
    "autodesign_suppress_terminal_events", default=False,
)
_redaction_values_var: ContextVar[tuple[str, ...]] = ContextVar(
    "autodesign_log_redaction_values", default=(),
)

_subscribers: dict[str, list[queue.Queue]] = {}
_event_history: dict[str, deque[dict[str, Any]]] = {}
_EVENT_HISTORY_LIMIT = 500
_subs_lock = threading.Lock()
_events_lock = threading.Lock()
_event_file_cache: dict[str, tuple[int, int, dict[str, dict[str, Any]]]] = {}


def log(event: str, **kw: Any) -> None:
    if _suppress_terminal_var.get() and event in {"run.done", "run.error", "run.cancelled"}:
        return
    secrets = _redaction_values_var.get()
    if secrets:
        kw = _redact_payload(kw, secrets)
    payload = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "ts_epoch_ms": int(time.time() * 1000),
        "event": event,
        **kw,
    }
    print(json.dumps(payload, ensure_ascii=False, default=str), file=sys.stderr, flush=True)
    rid = kw.get("run_id") or _run_id_var.get()
    if not rid:
        return
    canonical = _append_run_event(rid, payload)
    run_event = {"run_id": rid, **canonical}
    with _subs_lock:
        _event_history.setdefault(rid, deque(maxlen=_EVENT_HISTORY_LIMIT)).append(run_event)
        subs = list(_subscribers.get(rid, ()))
    for q in subs:
        try:
            # Non-blocking: drop events when a subscriber is too slow
            # rather than back-pressuring the agent.
            q.put_nowait(run_event)
        except queue.Full:
            pass


@contextmanager
def run_context(
    run_id: str,
    run_dir: str | Path | None = None,
    *,
    event_file_name: str | None = None,
    suppress_terminal_events: bool | None = None,
    sensitive_values: tuple[str, ...] | None = None,
) -> Iterator[None]:
    """Tag all log events within the block with `run_id`. Reentrant —
    nested context managers just stack and pop."""
    token = _run_id_var.set(run_id)
    dir_token = _run_dir_var.set(Path(run_dir) if run_dir is not None else None)
    inherited_event_file = _event_file_var.get()
    file_token = _event_file_var.set(
        event_file_name or inherited_event_file or "run_events.jsonl"
    )
    inherited_suppression = _suppress_terminal_var.get()
    suppress_token = _suppress_terminal_var.set(
        inherited_suppression
        if suppress_terminal_events is None
        else suppress_terminal_events
    )
    inherited_redactions = _redaction_values_var.get()
    redaction_token = _redaction_values_var.set(
        inherited_redactions if sensitive_values is None else sensitive_values
    )
    try:
        yield
    finally:
        _redaction_values_var.reset(redaction_token)
        _suppress_terminal_var.reset(suppress_token)
        _event_file_var.reset(file_token)
        _run_dir_var.reset(dir_token)
        _run_id_var.reset(token)


@contextmanager
def worker_run_context(
    run_id: str,
    run_dir: str | Path,
    *,
    sensitive_values: tuple[str, ...] = (),
) -> Iterator[None]:
    """Route nested pipeline logs to the nonterminal worker event stream."""
    with run_context(
        run_id,
        run_dir,
        event_file_name="worker_events.jsonl",
        suppress_terminal_events=True,
        sensitive_values=sensitive_values,
    ):
        yield


def subscribe(run_id: str, *, maxsize: int = 1024, replay: bool = True) -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=maxsize)
    with _subs_lock:
        _subscribers.setdefault(run_id, []).append(q)
        replay_events = list(_event_history.get(run_id, ())) if replay else []
    for event in replay_events:
        try:
            q.put_nowait(event)
        except queue.Full:
            break
    return q


def unsubscribe(run_id: str, q: queue.Queue) -> None:
    with _subs_lock:
        if run_id not in _subscribers:
            return
        try:
            _subscribers[run_id].remove(q)
        except ValueError:
            return
        if not _subscribers[run_id]:
            del _subscribers[run_id]


def _append_run_event(run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    run_dir = _run_dir_var.get()
    if run_dir is None:
        return payload
    event = {"run_id": run_id, **payload}
    try:
        path = run_dir / (_event_file_var.get() or "run_events.jsonl")
        return append_jsonl_event(path, event)
    except Exception:
        # Telemetry must never perturb the generation pipeline.
        return event


def append_jsonl_event(
    path: str | Path,
    payload: dict[str, Any],
    *,
    event_id: str | None = None,
) -> dict[str, Any]:
    """Durably append one canonical event, deduplicating its stable ID."""
    destination = Path(path)
    parent_existed = destination.parent.exists()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not parent_existed:
        _fsync_directory(destination.parent.parent)
    stable_id = str(event_id or payload.get("event_id") or uuid4())
    with _events_lock:
        lock_path = destination.with_name(f".{destination.name}.lock")
        with lock_path.open("a+b") as lock_handle:
            _lock_event_file(lock_handle)
            try:
                created = not destination.exists()
                raw = destination.read_bytes() if destination.exists() else b""
                events, valid_bytes = _decode_jsonl_prefix(raw)
                last_sequence = max(
                    (event.get("seq", 0) for event in events if type(event.get("seq")) is int),
                    default=0,
                )
                events_by_id = {
                    str(event["event_id"]): event
                    for event in events
                    if isinstance(event.get("event_id"), str)
                }
                existing = events_by_id.get(stable_id)
                if existing is not None:
                    return existing
                sequence = last_sequence + 1
                canonical = {**payload, "event_id": stable_id, "seq": sequence}
                line = (json.dumps(canonical, ensure_ascii=False, default=str) + "\n").encode("utf-8")
                with destination.open("a+b") as handle:
                    handle.seek(0, os.SEEK_END)
                    if handle.tell() != valid_bytes:
                        handle.truncate(valid_bytes)
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
                if created:
                    _fsync_directory(destination.parent)
                key = str(destination.resolve())
                events_by_id = {**events_by_id, stable_id: canonical}
                _event_file_cache[key] = (valid_bytes + len(line), sequence, events_by_id)
                return canonical
            finally:
                _unlock_event_file(lock_handle)


def _lock_event_file(handle: Any) -> None:
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


def _unlock_event_file(handle: Any) -> None:
    if os.name == "posix":
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    else:
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _fsync_directory(path: Path) -> None:
    if os.name != "posix" or not path.exists():
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_jsonl_events(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    try:
        raw = source.read_bytes()
    except FileNotFoundError:
        return []
    events, _ = _decode_jsonl_prefix(raw)
    return events


def _decode_jsonl_prefix(raw: bytes) -> tuple[list[dict[str, Any]], int]:
    events: list[dict[str, Any]] = []
    offset = 0
    for line in raw.splitlines(keepends=True):
        if not line.endswith((b"\n", b"\r")):
            break
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            break
        if not isinstance(value, dict):
            break
        events.append(value)
        offset += len(line)
    return events, offset


class RedactingLogWriter:
    """Streaming secret redaction with bounded durable diagnostic output."""

    def __init__(
        self,
        path: str | Path,
        sensitive_values: tuple[str, ...] | list[str],
        *,
        max_bytes: int,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("wb")
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._secrets = tuple(
            sorted({value for value in sensitive_values if value}, key=len, reverse=True)
        )
        self._max_secret_length = max((len(value) for value in self._secrets), default=1)
        self._buffer = ""
        self._max_bytes = max(256, int(max_bytes))
        self._written = 0
        self._truncated = False

    def feed(self, chunk: bytes) -> None:
        self._buffer += self._decoder.decode(chunk)
        keep = self._max_secret_length
        emit_end = max(0, len(self._buffer) - keep)
        changed = True
        while changed and emit_end:
            changed = False
            for secret in self._secrets:
                start = self._buffer.rfind(secret, 0, emit_end + len(secret))
                if 0 <= start < emit_end < start + len(secret):
                    emit_end = start
                    changed = True
        if emit_end:
            self._write_redacted(self._buffer[:emit_end])
            self._buffer = self._buffer[emit_end:]

    def close(self) -> None:
        self._buffer += self._decoder.decode(b"", final=True)
        self._write_redacted(self._buffer)
        self._buffer = ""
        if self._truncated:
            marker = b"\n[log truncated after bounded capture]\n"
            remaining = max(0, self._max_bytes - self._written)
            if remaining:
                portion = marker[:remaining]
                self._handle.write(portion)
                self._written += len(portion)
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._handle.close()

    def _write_redacted(self, text: str) -> None:
        for secret in self._secrets:
            text = text.replace(secret, "[REDACTED]")
        encoded = text.encode("utf-8", errors="replace")
        remaining = max(0, self._max_bytes - 64 - self._written)
        if len(encoded) > remaining:
            self._truncated = True
            encoded = encoded[:remaining].decode("utf-8", errors="ignore").encode("utf-8")
        if encoded:
            self._handle.write(encoded)
            self._written += len(encoded)


def _redact_payload(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {key: _redact_payload(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_payload(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_payload(item, secrets) for item in value)
    return value
